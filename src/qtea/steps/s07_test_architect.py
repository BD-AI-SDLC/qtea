"""Step 7: Test Automation Architect — produces the code-modification plan.

Inputs: test-design.md (step 4) + sut_inventory.json (step 6) + research.md
(step 6, for narrative context).

Output (artifacts/step07/):
  - code-modification-plan.json   (structured plan, schema-validated)
  - code-modification-plan.md     (human-readable summary for review gate)

Behavior:
  1. Pre-flight: SUT materialized, test-design.md present, sut_inventory.json
     present with a resolved active_module. Any miss → fail in <1s.
  2. Inline the upstream artifacts into the agent's user prompt.
  3. Invoke the `test-automation-architect` agent via direct Anthropic SDK with the
     `code-modification-plan` schema enforced via structured outputs — the
     response IS the JSON object (no prose, no fences).
  4. Schema-validate (belt-and-suspenders) and parse the JSON.
  5. Run the phase gate: every `reuse` references an inventory entry; every
     `create`/`create_tbd` target lands in an inventory-approved directory;
     missing-method signatures present; intents within 120 chars; markers valid.
  6. Render the markdown summary locally from the validated JSON; persist
     both outputs; commit on the qtea branch.

Transport: this step uses `qtea.llm.reasoning.call_reasoning_llm` (direct
SDK, no subprocess, no MCP, no file tools). Inputs arrive inlined in the user
prompt; the markdown view is always rendered locally from the JSON for
consistency, matching the Step 10 (bug-classifier) pattern.

Failure mode: abort. Without a plan, Step 8 (codegen) has no placement
authority and would fall back to ad-hoc inference — defeating the architectural
purpose of inserting this step.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from qtea._sut_git import commit_step
from qtea.config import package_resource_root, step_timeout
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.schemas import is_valid, load_schema, normalize_arrays
from qtea.steps.base import Step, StepContext, StepResult
from qtea.steps.s07_live_explore import (
    LoginSpec,
    _resolve_base_url,
    explore_strategy_routes,
    render_live_map_for_prompt,
)

log = get_logger(__name__)


_VALID_MARKERS = {"qtea_smoke", "qtea_regression", "qtea_e2e", "qtea_exploratory"}

# Arrange-coverage gate: a page object whose name looks like a login/auth page
# exists to be driven by an explicit login call. If one is planned but no step
# invokes a login method, the generated test starts unauthenticated — the
# "missing login" defect. `switchUser`/`switchRole` are identity SWITCHES, not
# the initial login, so they deliberately do NOT match the method pattern.
# The POM pattern is token-boundary anchored so `AuthorPage` / `AuthorityPage`
# (which merely contain "auth") do NOT false-match — only login/signin/auth(n).
# Token match is case-insensitive (scoped `(?i:...)`), but the trailing
# boundary is case-SENSITIVE: a following lowercase letter means the token is
# part of a larger word ("auth" in "Author") → no match; a following uppercase
# letter is a CamelCase segment break ("Login" in "LoginPage") → match.
_LOGIN_POM_RE = re.compile(
    r"(?i:log[_-]?in|sign[_-]?in|auth(?:n|entication)?)(?![a-z])"
)
_LOGIN_METHOD_RE = re.compile(
    r"log[_-]?in|sign[_-]?in|log[_-]?on|authenticate", re.IGNORECASE
)
# Open-before-login gate: a method that navigates to the app base URL. A UI
# test that logs in on a fresh about:blank page (no open/navigate first) makes
# every locator time out — the "blank page" defect. Lenient on purpose (catch
# the missing-open case without false-rejecting valid plans).
_OPEN_METHOD_RE = re.compile(r"open|goto|navigate|visit", re.IGNORECASE)
_BROWSER_FRAMEWORKS = frozenset({
    "playwright-ts", "playwright-js", "playwright-py", "cypress",
    "selenium-py", "selenium-java", "wdio", "protractor", "nightwatch",
})

# Default budget (chars) for inlined POM/fixture/helper source. Sonnet 4.7 has a
# 200K window; we leave headroom for sut_inventory (30-80K typical), strategy,
# schema, and response. Override with QTEA_REUSE_SOURCE_BUDGET.
_DEFAULT_REUSE_SOURCE_BUDGET = 120_000


def _active_module_dict(sut_inventory_dict: dict) -> dict | None:
    """Pull the active module entry out of a raw `sut_inventory` dict."""
    active = sut_inventory_dict.get("active_module")
    if not active:
        return None
    for mod in sut_inventory_dict.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == active:
            return mod
    return None


def _inline_reuse_sources(
    active_module: dict | None,
    sut_root: Path,
    budget: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Read POM/fixture/helper source files for the active module up to budget.

    The test-automation-architect agent has no file tools; it can only justify reuse
    decisions against material that is inlined into its prompt. Without source
    visibility it can verify a symbol *exists* (the inventory tells it that)
    but not whether the symbol's actual behaviour *fits* the test case.

    Enumerates unique ``file`` paths across ``existing_page_objects``,
    ``existing_fixtures``, ``existing_helpers`` for the active module, resolves
    each to ``sut_root / module.path / file``, sorts alphabetically by the
    SUT-relative key for determinism, then reads files in order. Stops as soon
    as the NEXT file would push total bytes past ``budget``. Files that don't
    exist, can't be read, or would exceed the budget are appended to the
    ``skipped`` list (with reason for missing/unreadable; budget-skip files
    just appear in the list).

    Returns ``(sources, skipped)`` where ``sources`` is a dict keyed by
    ``"reuse-source/<sut-relative-posix-path>"`` (the prefix prevents collision
    with the canonical input keys like ``sut_inventory.json``).
    """
    sources: dict[str, str] = {}
    skipped: list[str] = []
    if not active_module:
        return sources, skipped

    if budget is None:
        try:
            budget = int(os.environ.get("QTEA_REUSE_SOURCE_BUDGET", "")
                         or _DEFAULT_REUSE_SOURCE_BUDGET)
        except ValueError:
            budget = _DEFAULT_REUSE_SOURCE_BUDGET

    module_path = active_module.get("path") or "."
    module_root = sut_root / module_path

    # Collect unique file paths across all three reuse-candidate categories.
    rel_paths: set[str] = set()
    for key in ("existing_page_objects", "existing_fixtures", "existing_helpers"):
        for entry in active_module.get(key) or []:
            if isinstance(entry, dict):
                f = entry.get("file")
                if isinstance(f, str) and f:
                    rel_paths.add(f.replace("\\", "/"))

    consumed = 0
    for rel in sorted(rel_paths):
        abs_path = module_root / rel
        if not abs_path.is_file():
            skipped.append(f"{rel} (not found)")
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append(f"{rel} (read error: {e})")
            continue
        size = len(text)
        if consumed + size > budget:
            skipped.append(rel)
            continue
        # Key by SUT-root-relative path (module_path + rel) for unambiguous
        # cross-module audit, even though we only inline the active module.
        sut_rel = (
            rel if module_path in (".", "")
            else f"{module_path.rstrip('/')}/{rel}"
        )
        sources[f"reuse-source/{sut_rel}"] = text
        consumed += size

    return sources, skipped


def _lifecycle_hook_index(active_module: dict | None) -> dict[str, list[dict]]:
    """Group active_module.lifecycle_hooks by canonical `event`.

    Returns ``{event: [hook_dict, ...]}`` preserving sut_inventory order.
    Used by the hook-reuse existence + sequence gate below.
    """
    out: dict[str, list[dict]] = {}
    if not active_module:
        return out
    for entry in active_module.get("lifecycle_hooks") or []:
        if isinstance(entry, dict) and entry.get("event"):
            out.setdefault(entry["event"], []).append(entry)
    return out


def _inventory_symbols(active_module: dict | None) -> dict[str, set[str]]:
    """Index reuse-target symbols by category for phase-gate validation.

    Returns a dict with keys ``fixtures``, ``page_objects``, ``helpers``,
    ``locators``. Each value is a set of ``"file:name"`` strings drawn from the
    active module's existing_* lists. Used to check that every `reuse`
    reference in the plan points at something real.
    """
    out: dict[str, set[str]] = {
        "fixtures": set(),
        "page_objects": set(),
        "helpers": set(),
        "locators": set(),
    }
    if not active_module:
        return out
    for entry in active_module.get("existing_fixtures") or []:
        if isinstance(entry, dict):
            f = entry.get("file") or ""
            n = entry.get("name") or ""
            if n:
                out["fixtures"].add(f"{f}:{n}" if f else n)
                out["fixtures"].add(n)  # also accept name-only match
                if f:
                    out["fixtures"].add(f)  # file-only ref
                bare = re.sub(r"\s*\(.*?\)\s*$", "", n).strip()
                if bare and bare != n:
                    out["fixtures"].add(f"{f}:{bare}" if f else bare)
                    out["fixtures"].add(bare)
    auth_flow = active_module.get("auth_flow")
    if isinstance(auth_flow, dict):
        fe = (auth_flow.get("fixture_entry") or "").strip()
        if fe:
            out["fixtures"].add(fe)
    for entry in active_module.get("existing_page_objects") or []:
        if isinstance(entry, dict):
            f = entry.get("file") or ""
            n = entry.get("name") or ""
            if n:
                out["page_objects"].add(f"{f}:{n}" if f else n)
                out["page_objects"].add(n)
                if f:
                    out["page_objects"].add(f)  # file-only ref also valid
    for entry in active_module.get("existing_helpers") or []:
        if isinstance(entry, dict):
            f = entry.get("file") or ""
            n = entry.get("name") or ""
            if n:
                out["helpers"].add(f"{f}:{n}" if f else n)
                out["helpers"].add(n)
    for lc in active_module.get("existing_locators") or []:
        if isinstance(lc, dict):
            f = lc.get("file") or ""
            cls = lc.get("class_name") or ""
            if cls:
                out["locators"].add(f"{f}:{cls}" if f else cls)
                out["locators"].add(cls)
                if f:
                    out["locators"].add(f)
            for const in lc.get("constants") or []:
                if isinstance(const, dict) and const.get("name"):
                    out["locators"].add(const["name"])
    return out


def _approved_dirs(active_module: dict | None) -> set[str]:
    """Inventory-approved relative directories for create targets."""
    if not active_module:
        return set()
    dirs: set[str] = set()
    test_layout = active_module.get("test_directory_layout") or {}
    src_layout = active_module.get("src_directory_layout") or {}
    for key in ("base_dir", "default_target"):
        v = test_layout.get(key)
        if isinstance(v, str) and v:
            dirs.add(v.rstrip("/"))
    for s in test_layout.get("subdirs") or []:
        if isinstance(s, dict) and s.get("path"):
            dirs.add(str(s["path"]).rstrip("/"))
    for key in ("package_root", "pages_object_dir", "pages_locators_dir",
                "helpers_dir"):
        v = src_layout.get(key)
        if isinstance(v, str) and v:
            dirs.add(v.rstrip("/"))
    # Exemplar lane (non-POM): the POM src-layout dirs are None for Screenplay
    # etc., so approve every captured exemplar's directory (e.g. framework/tasks)
    # AND its parent (e.g. framework/) so new reusable units can land beside the
    # SUT's own — the exemplar's `dir` is the only reliable placement signal
    # when there is no `pages_object_dir`.
    for ex in active_module.get("pattern_exemplars") or []:
        if not isinstance(ex, dict):
            continue
        ex_dir = ex.get("dir")
        if isinstance(ex_dir, str) and ex_dir and ex_dir != ".":
            norm = ex_dir.replace("\\", "/").rstrip("/")
            dirs.add(norm)
            parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
            if parent:
                dirs.add(parent)
    return dirs


def _path_under_approved(path: str, approved: set[str]) -> bool:
    """True when ``path`` lives under any approved relative directory.

    Accepts both forward and back slashes. An empty approved set short-circuits
    to True so SUTs without a detected layout don't blow up the gate.
    """
    if not approved:
        return True
    norm = path.replace("\\", "/").lstrip("./").rstrip("/")
    for d in approved:
        dn = d.replace("\\", "/").lstrip("./").rstrip("/")
        if not dn:
            continue
        if norm == dn or norm.startswith(dn + "/"):
            return True
    return False


_GENERATED_PREFIXES = ("qtea_", "Qtea")


def _is_generated_name(name: str) -> bool:
    """Return True if the name follows the qtea pipeline naming convention."""
    return any(name.startswith(p) for p in _GENERATED_PREFIXES)


def _normalize_ref(ref: str) -> str:
    """Normalise a reuse ``from`` reference to POSIX paths for matching."""
    return ref.replace("\\", "/")


# Per-`check` field requirements for acceptance_criteria (assertion oracles).
# The JSON schema enforces key PRESENCE; the phase gate below enforces the
# stronger semantics the schema can't express — non-null expected values, and
# that a criterion's locator resolves to a locator DECLARED in the same test
# case. Together these close finding 3 (false-green via an unpinned/guessed
# assertion oracle): an assertion method can no longer reach Step 8 without a
# machine-checkable oracle bound to real, planned locators + values.
_ORACLE_CHECKS_NEED_LOCATOR = frozenset({
    "exact_text", "exact_count", "exact_attribute", "value_equals",
    "visible", "focusable", "boundingbox_below", "boundingbox_above",
})
_ORACLE_CHECKS_NEED_EXPECTED = frozenset({
    "exact_text", "exact_count", "exact_attribute", "value_equals", "url_matches",
})
_ORACLE_CHECKS_NEED_REF_LOCATOR = frozenset({
    "boundingbox_below", "boundingbox_above",
})


def _validate_assertion_oracle(
    tc_id: str,
    po_name: str,
    mm: dict,
    declared_locators: set,
    violations: list[str],
) -> bool:
    """Enforce that an assertion ``missing_method`` carries a usable oracle.

    Every ``kind=='assertion'`` method must have ≥1 acceptance_criterion, and
    each criterion must bind the locator / expected value its ``check``
    requires (non-null), with the locator resolving to a locator declared in
    the same test case. This is the deterministic backstop for the JSON-schema
    if/then — the schema can't require non-null values or cross-reference the
    TC's own ``locators[]``.

    Returns True when the method is an assertion whose criteria are ENTIRELY
    ``custom`` — those escape deterministic body-verification, so the caller
    routes them to the semantic assertion-judge (Stage 3) rather than letting
    them silent-pass (the ``custom`` bypass the SDET flagged).
    """
    name = mm.get("name") or "<unnamed>"
    if mm.get("kind") != "assertion":
        return False
    criteria = mm.get("acceptance_criteria") or []
    if not criteria:
        violations.append(
            f"{tc_id}: {po_name}.{name} kind=assertion has no acceptance_criteria "
            f"(the oracle the generated test must verify)"
        )
        return False
    all_custom = True
    for i, crit in enumerate(criteria):
        if not isinstance(crit, dict):
            violations.append(
                f"{tc_id}: {po_name}.{name} acceptance_criteria[{i}] is not an object"
            )
            all_custom = False
            continue
        check = crit.get("check") or ""
        if check != "custom":
            all_custom = False
        loc = crit.get("locator")
        if check in _ORACLE_CHECKS_NEED_LOCATOR:
            if not loc:
                violations.append(
                    f"{tc_id}: {po_name}.{name} criterion[{i}] check={check} "
                    f"needs a `locator`"
                )
            elif declared_locators and loc not in declared_locators:
                violations.append(
                    f"{tc_id}: {po_name}.{name} criterion[{i}] locator `{loc}` is "
                    f"not declared in this test case's locators[]"
                )
        if check in _ORACLE_CHECKS_NEED_REF_LOCATOR:
            ref = crit.get("reference_locator")
            if not ref:
                violations.append(
                    f"{tc_id}: {po_name}.{name} criterion[{i}] check={check} "
                    f"needs a `reference_locator`"
                )
            elif declared_locators and ref not in declared_locators:
                violations.append(
                    f"{tc_id}: {po_name}.{name} criterion[{i}] reference_locator "
                    f"`{ref}` is not declared in this test case's locators[]"
                )
        if check in _ORACLE_CHECKS_NEED_EXPECTED:
            if crit.get("expected_literal") is None and not crit.get("expected_symbol"):
                violations.append(
                    f"{tc_id}: {po_name}.{name} criterion[{i}] check={check} needs a "
                    f"non-null expected value (expected_literal or expected_symbol)"
                )
    return bool(criteria) and all_custom


def _validate_kind_acceptance_criteria_coherence(
    tc_id: str, po_name: str, mm: dict, violations: list[str],
) -> None:
    """Reject a `missing_methods` entry whose `kind` and `acceptance_criteria`
    disagree about whether it's an assertion.

    `acceptance_criteria` exists for exactly one reason: it's the oracle
    Step 8's body-verifier (`codegen_body_verify.py`) checks the generated
    code against — and that verifier only runs for `kind == "assertion"`
    entries (`if m.get("kind") != "assertion": continue`). That makes
    relabeling a `kind: "assertion"` entry to `kind: "query"` (e.g. to
    dodge the void-signature gate above) a way to make its
    `acceptance_criteria` invisible to EVERY downstream check — not just
    this one — while the oracle metadata sits there looking legitimate. A
    genuine fix either stays `assertion` (oracle still enforced) or drops
    to `query` and sheds its `acceptance_criteria` (the actual expect()
    call then lives in the test, per the existing phase="assert"
    choreography check). Anything else is a fix that satisfies the gate's
    letter without doing the work.
    """
    if mm.get("kind") == "assertion":
        return  # covered by _validate_assertion_oracle
    criteria = mm.get("acceptance_criteria") or []
    if criteria:
        violations.append(
            f"{tc_id}: page_object `{po_name}` missing_method "
            f"`{mm.get('name')}` is kind={mm.get('kind')!r} but still carries "
            f"{len(criteria)} `acceptance_criteria` entr{'y' if len(criteria) == 1 else 'ies'}. "
            f"acceptance_criteria is the assertion oracle — Step 8's body-verifier "
            f"only checks it for kind=\"assertion\" methods, so leaving it here "
            f"means that oracle is now checked by nothing. Either keep "
            f"kind=\"assertion\" (with a non-void signature) so the oracle stays "
            f"enforced, or genuinely make this a query: remove `acceptance_criteria` "
            f"and `purpose`, and ensure the actual expect()/assert against these "
            f"facts appears in the test function's phase=\"assert\" step instead."
        )


# Language-specific "this signature reports nothing" detectors for the
# void-assertion gate below. A `kind: "assertion"` method can only be a
# missing_method on a POM (the schema has no other bucket for it), and
# `codegen-rules.md` §"Assertions Belong in Test Methods, Not POMs" bans
# `expect()`/`assert` inside POM bodies unconditionally. A void return type
# leaves the pom-extender no way to report the checked fact except by
# embedding the forbidden assert/expect call directly in the POM method —
# exactly how run `20260708-121117-99f5ed` shipped
# `expect(marketingCheckbox).toBeAttached(...)` inside
# `verifyMarketingConsentPositionAndLabel`. Catching the void-signature shape
# here, at plan time, is cheaper than waiting for Step 8's `pom-assertion`
# gate to hard-fail on the generated code.
_PY_VOID_RETURN_RE = re.compile(r"->\s*None\b")
_JS_VOID_RETURN_RE = re.compile(
    r":\s*(Promise<\s*)?(void|undefined|any|unknown)\s*>?\s*;?\s*$", re.IGNORECASE,
)
_JAVA_VOID_RETURN_RE = re.compile(
    r"^\s*(public|private|protected)?\s*void\s+\w+\s*\(", re.IGNORECASE,
)
_PY_LANGUAGES = frozenset({"python", "pytest", "playwright-py", "selenium-py"})
_JS_LANGUAGES = frozenset({"typescript", "javascript"})


def _assertion_signature_is_void(signature: str, language: str) -> bool:
    """True when a `kind: "assertion"` method's signature reports nothing.

    Python: no `->` annotation at all defaults to `None`, same as an
    explicit `-> None`. TS/JS: `void`/`undefined`/`any`/`unknown`, bare or
    `Promise<...>`-wrapped (mirrors `_VOID_RETURN_TYPES` in
    `codegen_pom_hygiene.py`, which checks the same shapes on the EMITTED
    code at Step 8 — this is the plan-time counterpart). Java: a leading
    `void` return type. Unknown languages are not checked (returns False)
    rather than risk false positives on a signature style we don't model.
    """
    sig = (signature or "").strip()
    if not sig:
        return False
    lang = (language or "").lower()
    if lang in _PY_LANGUAGES:
        return "->" not in sig or bool(_PY_VOID_RETURN_RE.search(sig))
    if lang in _JS_LANGUAGES:
        return bool(_JS_VOID_RETURN_RE.search(sig))
    if lang == "java":
        return bool(_JAVA_VOID_RETURN_RE.match(sig))
    return False


def _validate_plan_against_inventory(
    plan: dict,
    active_module: dict | None,
) -> list[str]:
    """Phase-gate checks beyond the JSON schema.

    Returns a list of human-readable violation strings; empty list means OK.
    """
    violations: list[str] = []
    symbols = _inventory_symbols(active_module)
    approved = _approved_dirs(active_module)
    language = (
        plan.get("language") or (active_module or {}).get("language") or ""
    ).lower()

    # Auth-chaining: extract the auth fixture name once for the
    # depends_on check below.  "tests/conftest.py:chat_page" → "chat_page"
    _auth_flow = (active_module or {}).get("auth_flow") or {}
    _auth_ref = _auth_flow.get("fixture_entry") or ""
    _auth_fixture_name = _auth_ref.rsplit(":", 1)[-1] if ":" in _auth_ref else _auth_ref
    _PRIMITIVE_YIELDS = frozenset(
        {"", "dict", "str", "int", "list", "tuple", "bool", "none", "path"}
    )

    for tc in plan.get("test_cases") or []:
        tc_id = tc.get("id") or "<no-id>"

        target = tc.get("test_file_target")
        if (
            isinstance(target, str)
            and target
            and not _path_under_approved(target, approved)
        ):
            violations.append(
                f"{tc_id}: test_file_target `{target}` is not under any "
                f"inventory-approved directory (approved: "
                f"{sorted(approved) or 'none-detected'})"
            )

        for fn in tc.get("test_functions") or []:
            for m in fn.get("markers") or []:
                if m not in _VALID_MARKERS:
                    violations.append(
                        f"{tc_id}: marker `{m}` is not one of "
                        f"{sorted(_VALID_MARKERS)}"
                    )

        for f in tc.get("fixtures") or []:
            src = f.get("source")
            fname = f.get("name") or ""
            if src == "reuse":
                if _is_generated_name(fname):
                    violations.append(
                        f"{tc_id}: fixture `{fname}` source=reuse but "
                        f"name has qtea_ prefix (generated artifact). "
                        f"Use source=create instead."
                    )
                ref = f.get("from")
                if not ref:
                    violations.append(
                        f"{tc_id}: fixture `{fname}` source=reuse "
                        f"missing `from` field"
                    )
                else:
                    nref = _normalize_ref(ref)
                    if (
                        nref not in symbols["fixtures"]
                        and not any(
                            s == nref or s.startswith(nref + ":")
                            for s in symbols["fixtures"]
                        )
                    ):
                        violations.append(
                            f"{tc_id}: fixture `{fname}` reuse-from "
                            f"`{ref}` not found in sut_inventory"
                        )
                if not (f.get("reuse_justification") or "").strip():
                    violations.append(
                        f"{tc_id}: fixture `{f.get('name')}` source=reuse "
                        f"missing `reuse_justification` (one-sentence fit rationale)"
                    )
            elif src == "create":
                at = f.get("at")
                if not at:
                    violations.append(
                        f"{tc_id}: fixture `{f.get('name')}` source=create "
                        f"missing `at` field"
                    )

            if src == "create" and _auth_fixture_name:
                yields_type = (f.get("yields") or "").strip()
                if yields_type and yields_type.lower() not in _PRIMITIVE_YIELDS:
                    deps = f.get("depends_on") or []
                    if _auth_fixture_name not in deps:
                        violations.append(
                            f"{tc_id}: fixture `{f.get('name')}` source=create "
                            f"yields `{yields_type}` but does not declare "
                            f"`depends_on: [\"{_auth_fixture_name}\"]`. "
                            f"Fixtures yielding authenticated objects must "
                            f"chain with the auth fixture from "
                            f"auth_flow.fixture_entry."
                        )

        for po in tc.get("page_objects") or []:
            src = po.get("source")
            poname = po.get("name") or ""
            if src == "reuse":
                if _is_generated_name(poname):
                    violations.append(
                        f"{tc_id}: page_object `{poname}` source=reuse but "
                        f"name has Qtea/qtea_ prefix (generated artifact). "
                        f"Use source=create instead."
                    )
                ref = po.get("from")
                if not ref:
                    violations.append(
                        f"{tc_id}: page_object `{poname}` source=reuse "
                        f"missing `from` field"
                    )
                elif _normalize_ref(ref) not in symbols["page_objects"]:
                    violations.append(
                        f"{tc_id}: page_object `{poname}` reuse-from "
                        f"`{ref}` not found in sut_inventory"
                    )
                if not (po.get("reuse_justification") or "").strip():
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` source=reuse "
                        f"missing `reuse_justification` (one-sentence fit rationale)"
                    )
            elif src == "create":
                at = po.get("at")
                if not at:
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` source=create "
                        f"missing `at` field"
                    )
                elif not _path_under_approved(at, approved):
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` create target "
                        f"`{at}` not under an inventory-approved directory"
                    )
            declared_locators = {
                lc.get("name") for lc in (tc.get("locators") or [])
                if isinstance(lc, dict) and lc.get("name")
            }
            for mm in po.get("missing_methods") or []:
                if not mm.get("signature"):
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` missing_method "
                        f"`{mm.get('name')}` has no signature"
                    )
                if not mm.get("kind"):
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` missing_method "
                        f"`{mm.get('name')}` has no `kind` (action|assertion|query)"
                    )
                # Assertion methods must carry a machine-checkable oracle bound
                # to declared locators + non-null expected values (finding 3).
                only_custom = _validate_assertion_oracle(
                    tc_id, po.get("name") or "<pom>", mm,
                    declared_locators, violations,
                )
                # A void-returning `kind: "assertion"` method targeting a POM
                # has no way to report its result except an embedded
                # expect()/assert — the NON-NEGOTIABLE violation Step 8's
                # pom-assertion gate hard-fails on. Catch the shape here,
                # before any codegen agent runs.
                if (
                    mm.get("kind") == "assertion"
                    and mm.get("signature")
                    and _assertion_signature_is_void(mm["signature"], language)
                ):
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` missing_method "
                        f"`{mm.get('name')}` is kind=assertion with a void-shaped "
                        f"signature (`{mm['signature']}`). A POM method can't "
                        f"report a fact through a void return without embedding "
                        f"the actual expect()/assert call in the POM body, which "
                        f"is forbidden (codegen-rules.md §\"Assertions Belong in "
                        f"Test Methods, Not POMs\"). Make it a probe that returns "
                        f"the `Locator` the test asserts on (`getX(): Locator` / "
                        f"`get_x(self) -> Locator` / `Locator getX()`) — the test "
                        f"passes it into `expect(...)`. Never a `bool`/"
                        f"`Promise<boolean>` verdict (that pushes the assertion "
                        f"into the POM and yields a dead `.toBe(true)`)."
                    )
                # Kind/oracle coherence — catches a relabel-to-dodge fix that
                # leaves assertion-oracle metadata on a non-assertion entry
                # (see docstring: this is invisible to Step 8's body-verifier).
                _validate_kind_acceptance_criteria_coherence(
                    tc_id, po.get("name") or "<pom>", mm, violations,
                )
                if only_custom:
                    # Escapes deterministic body-verify — flag for the Stage-3
                    # semantic judge instead of letting it silent-pass.
                    log.warning(
                        "step07.assertion_oracle_all_custom",
                        tc_id=tc_id,
                        pom=po.get("name"),
                        method=mm.get("name"),
                        hint="custom-only oracle; routed to assertion-judge (shadow)",
                    )

        for h in tc.get("helpers") or []:
            src = h.get("source")
            if src == "reuse":
                if not h.get("from"):
                    violations.append(
                        f"{tc_id}: helper `{h.get('name')}` source=reuse "
                        f"missing `from` field"
                    )
                if not (h.get("reuse_justification") or "").strip():
                    violations.append(
                        f"{tc_id}: helper `{h.get('name')}` source=reuse "
                        f"missing `reuse_justification` (one-sentence fit rationale)"
                    )
            elif src == "create":
                if not h.get("at"):
                    violations.append(
                        f"{tc_id}: helper `{h.get('name')}` source=create "
                        f"missing `at` field"
                    )

        for loc in tc.get("locators") or []:
            src = loc.get("source")
            if src == "create_tbd":
                intent = loc.get("intent") or ""
                if not intent:
                    violations.append(
                        f"{tc_id}: locator `{loc.get('name')}` source=create_tbd "
                        f"missing intent"
                    )
                elif len(intent) > 120:
                    violations.append(
                        f"{tc_id}: locator `{loc.get('name')}` intent exceeds "
                        f"120 chars ({len(intent)})"
                    )
            elif src == "reuse":
                ref = loc.get("from") or loc.get("name")
                if ref and _normalize_ref(ref) not in symbols["locators"]:
                    # Locator reuse can reference either the constant name or
                    # the owning file. A miss here is genuinely soft: Step 6's
                    # locator enumeration is often incomplete (it can't always
                    # parse every constant out of a dynamically-built locator
                    # module), so a "not found" does NOT mean the reference is
                    # invalid. Codegen (Step 8) fails loudly if the import
                    # truly doesn't resolve, so we warn rather than abort the
                    # whole plan on what is frequently a false positive.
                    log.warning(
                        "step07.locator_reuse_not_in_inventory",
                        tc_id=tc_id,
                        locator=loc.get("name"),
                        reference=ref,
                    )
                if not (loc.get("reuse_justification") or "").strip():
                    violations.append(
                        f"{tc_id}: locator `{loc.get('name')}` source=reuse "
                        f"missing `reuse_justification` (one-sentence fit rationale)"
                    )

        # Exemplar lane (non-POM): validate reusable_units placement + shape.
        # Intentionally does NOT apply the POM-specific assertion-probe / void
        # signature rules — the exemplar lane imitates the SUT's own idiom
        # (decision: pattern-agnostic gates only).
        for ru in tc.get("reusable_units") or []:
            if not isinstance(ru, dict):
                continue
            runame = ru.get("name") or "<unit>"
            if ru.get("source") == "create":
                at = ru.get("at")
                if not at:
                    violations.append(
                        f"{tc_id}: reusable_unit `{runame}` source=create "
                        f"missing `at` field"
                    )
                elif not _path_under_approved(at, approved):
                    violations.append(
                        f"{tc_id}: reusable_unit `{runame}` create target `{at}` "
                        f"not under an inventory-approved directory (approved: "
                        f"{sorted(approved) or 'none-detected'})"
                    )
            for mb in ru.get("missing_behaviors") or []:
                if not isinstance(mb, dict):
                    continue
                if not mb.get("signature"):
                    violations.append(
                        f"{tc_id}: reusable_unit `{runame}` behavior "
                        f"`{mb.get('name')}` has no signature"
                    )
                if not mb.get("kind"):
                    violations.append(
                        f"{tc_id}: reusable_unit `{runame}` behavior "
                        f"`{mb.get('name')}` has no `kind` (action|assertion|query)"
                    )

        # Choreography gate: every steps[] entry must reference a planned unit
        # and (optionally) a locator planned within the SAME test case. `pom` +
        # `locator` are hard-checked against the TC's own page_objects /
        # reusable_units / locators (a mismatch means the writer would emit a
        # call on a class or constant that doesn't exist in this file). `method`
        # is soft-checked: it may be an existing reused method (not enumerated in
        # the plan) OR a missing_methods entry — so a miss only logs a warning.
        #
        # Architecture-agnostic: the `pom` slot is a GENERIC unit-reference. For
        # non-POM SUTs (Screenplay) the planned units live in `reusable_units[]`
        # (category task/question/…), not `page_objects[]` — fold both in, keyed
        # on the union of {name, class_name}, so a Screenplay plan whose steps
        # reference Tasks/Questions is not falsely rejected as "planned: none"
        # (root cause of run 20260715-075512-f2dbad).
        tc_pom_names: set[str] = {
            po.get("name") for po in (tc.get("page_objects") or [])
            if isinstance(po, dict) and po.get("name")
        }
        for ru in tc.get("reusable_units") or []:
            if not isinstance(ru, dict):
                continue
            if ru.get("name"):
                tc_pom_names.add(ru["name"])
            if ru.get("class_name"):
                tc_pom_names.add(ru["class_name"])
        tc_locator_names = {
            lc.get("name") for lc in (tc.get("locators") or [])
            if isinstance(lc, dict) and lc.get("name")
        }
        tc_missing_methods: dict[str, set[str]] = {}
        tc_method_kinds: dict[str, dict[str, str]] = {}
        for po in tc.get("page_objects") or []:
            if not isinstance(po, dict) or not po.get("name"):
                continue
            tc_missing_methods[po["name"]] = {
                mm.get("name") for mm in (po.get("missing_methods") or [])
                if isinstance(mm, dict) and mm.get("name")
            }
            tc_method_kinds[po["name"]] = {
                mm.get("name"): mm.get("kind")
                for mm in (po.get("missing_methods") or [])
                if isinstance(mm, dict) and mm.get("name")
            }
        # Fold reusable_units' behaviours into the same method maps (keyed by
        # both name and class_name so a step referencing either resolves).
        for ru in tc.get("reusable_units") or []:
            if not isinstance(ru, dict) or not ru.get("name"):
                continue
            behaviours = {
                mb.get("name") for mb in (ru.get("missing_behaviors") or [])
                if isinstance(mb, dict) and mb.get("name")
            }
            kinds = {
                mb.get("name"): mb.get("kind")
                for mb in (ru.get("missing_behaviors") or [])
                if isinstance(mb, dict) and mb.get("name")
            }
            for key in (ru.get("name"), ru.get("class_name")):
                if key:
                    tc_missing_methods[key] = behaviours
                    tc_method_kinds[key] = kinds
        for fn in tc.get("test_functions") or []:
            for st in fn.get("steps") or []:
                if not isinstance(st, dict):
                    continue
                pom = st.get("pom")
                if pom and pom not in tc_pom_names:
                    violations.append(
                        f"{tc_id}: choreography step order={st.get('order')} "
                        f"references pom `{pom}` not planned in this test case "
                        f"(planned: {sorted(n for n in tc_pom_names if n) or 'none'})"
                    )
                loc_ref = st.get("locator")
                if loc_ref and loc_ref not in tc_locator_names:
                    violations.append(
                        f"{tc_id}: choreography step order={st.get('order')} "
                        f"references locator `{loc_ref}` not planned in this "
                        f"test case"
                    )
                method = st.get("method")
                if (
                    method and pom in tc_missing_methods
                    and method not in tc_missing_methods[pom]
                ):
                    # Soft: could be an existing reused method (plan doesn't
                    # enumerate those). Codegen fails loudly if the method
                    # truly doesn't resolve on the POM.
                    log.warning(
                        "step07.choreography_method_not_in_missing",
                        tc_id=tc_id,
                        pom=pom,
                        method=method,
                    )
                # An 'assert'-phase step must invoke a method that actually
                # verifies something — kind assertion (probe+assert) or query
                # (returns a value the test asserts on). An 'action' method in
                # an assert step means the "specific check" is never performed
                # (finding 3 / the user's "fulfil a specific check" rule). Only
                # enforced for planned missing_methods (reused methods carry no
                # kind in the plan).
                if (
                    st.get("phase") == "assert"
                    and method
                    and pom in tc_method_kinds
                    and method in tc_method_kinds[pom]
                    and tc_method_kinds[pom][method] not in ("assertion", "query")
                ):
                    violations.append(
                        f"{tc_id}: choreography step order={st.get('order')} is "
                        f"phase='assert' but calls `{pom}.{method}` whose kind="
                        f"{tc_method_kinds[pom][method]!r} — an assert step must "
                        f"call a kind=assertion or kind=query method"
                    )

        # Arrange-coverage gate (the "missing login" defect). When a test case
        # plans a login/auth PAGE OBJECT but the choreography drives OTHER pages
        # without ever invoking a login method, the generated test runs
        # unauthenticated. Only fires when: (a) a login POM is planned, (b) at
        # least one Act step targets a non-login page (so the test genuinely
        # needs a session), and (c) no step invokes a login method. Tests whose
        # steps stay entirely on the login page (e.g. an invalid-login case) are
        # exempt. Skipped when the TC has no choreography (can't introspect).
        login_pom_names = {
            po.get("name") for po in (tc.get("page_objects") or [])
            if isinstance(po, dict) and po.get("name")
            and _LOGIN_POM_RE.search(po["name"])
        }
        if login_pom_names:
            all_steps = [
                st
                for fn in (tc.get("test_functions") or [])
                for st in (fn.get("steps") or [])
                if isinstance(st, dict)
            ]
            # Exempt tests whose session is established by a reused auto-auth
            # fixture (the auth_flow fixture): they legitimately need no login
            # step even when they plan a login POM (e.g. to assert a redirect
            # back to the login screen after logout).
            tc_fixture_names: set[str] = set()
            for fn in tc.get("test_functions") or []:
                tc_fixture_names.update(fn.get("uses_fixtures") or [])
            for fx in tc.get("fixtures") or []:
                if isinstance(fx, dict) and fx.get("name"):
                    tc_fixture_names.add(fx["name"])
            fixture_authed = bool(
                _auth_fixture_name and _auth_fixture_name in tc_fixture_names
            )
            if all_steps and not fixture_authed:
                login_invoked = any(
                    st.get("method") and _LOGIN_METHOD_RE.search(st["method"])
                    for st in all_steps
                )
                acts_on_other_page = any(
                    st.get("pom") and st["pom"] not in login_pom_names
                    for st in all_steps
                )
                if acts_on_other_page and not login_invoked:
                    violations.append(
                        f"{tc_id}: plans login page object(s) "
                        f"{sorted(login_pom_names)} and drives other pages, but "
                        f"no choreography step invokes a login method — the "
                        f"generated test would run unauthenticated. Add an "
                        f"Arrange login step (phase='arrange') or drop the "
                        f"unused login page object."
                    )

        # UI open-before-login gate (the "blank page" defect). A UI test that
        # logs in MUST first navigate to the app base URL — logging in on a
        # fresh about:blank page makes every locator time out. The open call
        # lives in the before_each hook (preferred) or leading arrange steps.
        # Fires only for UI SUTs (the SUT exposes an open/navigate method, or a
        # browser framework is in use). A reused before_each hook replays the
        # SUT's own open→login sequence and is trusted.
        open_ref = _auth_flow.get("open_method") or ""
        open_method_name = (
            open_ref.rsplit(".", 1)[-1]
            if ":" in open_ref and "." in open_ref.split(":", 1)[1]
            else ""
        )
        _framework = (plan.get("framework") or "").lower()
        if open_ref or _framework in _BROWSER_FRAMEWORKS:
            hooks = tc.get("hooks") or []
            reused_before_each = any(
                isinstance(h, dict)
                and h.get("event") == "before_each"
                and h.get("source") == "reuse"
                for h in hooks
            )
            if not reused_before_each:
                seq: list[dict] = []
                for h in hooks:
                    if isinstance(h, dict) and h.get("event") == "before_each":
                        seq.extend(
                            c for c in (h.get("calls") or []) if isinstance(c, dict)
                        )
                for fn in tc.get("test_functions") or []:
                    seq.extend(
                        st for st in (fn.get("steps") or []) if isinstance(st, dict)
                    )
                login_idx = next(
                    (
                        i for i, c in enumerate(seq)
                        if c.get("method") and _LOGIN_METHOD_RE.search(c["method"])
                    ),
                    None,
                )
                if login_idx is not None:
                    def _is_open_call(c: dict) -> bool:
                        m = c.get("method") or ""
                        if open_method_name and m == open_method_name:
                            return True
                        return bool(_OPEN_METHOD_RE.search(m))
                    open_before = any(_is_open_call(seq[i]) for i in range(login_idx))
                    if not open_before:
                        violations.append(
                            f"{tc_id}: UI test logs in "
                            f"(`{seq[login_idx].get('method')}`) but no "
                            f"open/navigate-to-base-URL call precedes it "
                            f"(expected `{open_method_name or 'openBaseURL'}` in a "
                            f"before_each hook or leading arrange step). Login on a "
                            f"blank page times out — add the open-base-URL call "
                            f"before login."
                        )

        # Hook-reuse staleness gate (the "stale calls[] after a HITL
        # from-edit" defect). Only runs when sut_inventory actually has
        # mined lifecycle_hooks data for this module — skip entirely
        # otherwise (no ground truth to check against).
        hook_index = _lifecycle_hook_index(active_module)
        if hook_index:
            def _bare_method(ref: str) -> str:
                return ref.rsplit(":", 1)[-1].rsplit(".", 1)[-1]

            for h in tc.get("hooks") or []:
                if not isinstance(h, dict) or h.get("source") != "reuse":
                    continue
                event = h.get("event") or ""
                ref = (h.get("from") or "").strip()
                if not ref:
                    violations.append(
                        f"{tc_id}: hook event={event or '?'} source=reuse "
                        f"missing `from` field"
                    )
                    continue
                ref_file = _normalize_ref(ref.split(":", 1)[0])
                candidates = hook_index.get(event) or []
                matched = next(
                    (e for e in candidates
                     if _normalize_ref(e.get("file") or "") == ref_file),
                    None,
                )
                if matched is None:
                    violations.append(
                        f"{tc_id}: hook event={event} source=reuse `from` "
                        f"`{ref}` does not match any sut_inventory."
                        f"lifecycle_hooks entry for this event"
                    )
                    continue
                # Normalize both plan and inventory hook calls to a common
                # {method, args} shape so downstream comparisons are uniform.
                # Inventory calls arrive as either bare strings (legacy shape,
                # kept for backward-compat with pre-existing sut_inventory.json
                # files) or {method, args} dicts (new shape emitted by the
                # deterministic Python + TS miners in sut_inventory.py). Plan
                # calls always arrive as {pom, method, args?} dicts.
                def _normalize_inv_call(raw: Any) -> dict[str, Any]:
                    if isinstance(raw, str):
                        return {"method": raw, "args": []}
                    if isinstance(raw, dict):
                        args = raw.get("args")
                        return {
                            "method": str(raw.get("method") or ""),
                            "args": [str(a) for a in args] if isinstance(args, list) else [],
                        }
                    return {"method": "", "args": []}

                plan_calls_norm = [
                    {
                        "method": str(c.get("method") or ""),
                        "args": [
                            str(a) for a in (c.get("args") or [])
                            if isinstance(a, (str, int, float, bool))
                        ] if isinstance(c.get("args"), list) else [],
                    }
                    for c in (h.get("calls") or [])
                    if isinstance(c, dict) and c.get("method")
                ]
                inv_calls_norm = [
                    _normalize_inv_call(raw)
                    for raw in (matched.get("calls") or [])
                    if raw
                ]
                inv_calls_norm = [c for c in inv_calls_norm if c["method"]]

                plan_seq = [_bare_method(c["method"]) for c in plan_calls_norm]
                inv_seq = [_bare_method(c["method"]) for c in inv_calls_norm]
                if inv_seq and plan_seq != inv_seq:
                    violations.append(
                        f"{tc_id}: hook event={event} source=reuse from "
                        f"`{ref}` has calls[] {plan_seq} but "
                        f"sut_inventory.lifecycle_hooks records {inv_seq} — "
                        f"stale relative to its `from` pointer. Resync "
                        f"calls[] to match the reused hook's real "
                        f"sequence, preserving args for methods that "
                        f"still appear."
                    )
                elif inv_seq and plan_seq == inv_seq:
                    # Args-preservation check: when method sequences agree,
                    # cross-check that any call whose inventory entry carries
                    # positional args also carries args in the plan. Dropping
                    # args here is the exact defect that surfaces downstream
                    # as codegen `arity_mismatch` — Step 8 has no oracle to
                    # backfill a missing argument value.
                    #
                    # We deliberately do NOT compare arg *expressions*: the
                    # architect may legitimately reparameterize (e.g. per-role
                    # credentials differ from the reused hook's role). The
                    # gate rejects only the total-drop signal: inventory has
                    # args, plan has none.
                    for i, (plan_c, inv_c) in enumerate(zip(plan_calls_norm, inv_calls_norm)):
                        if inv_c["args"] and not plan_c["args"]:
                            args_verbatim = ", ".join(inv_c["args"])
                            violations.append(
                                f"{tc_id}: hook event={event} source=reuse "
                                f"from `{ref}` call [{i}]:{plan_c['method']} "
                                f"declares 0 args but reused source calls it "
                                f"with {len(inv_c['args'])} arg(s): "
                                f"[{args_verbatim}]. Preserve args verbatim "
                                f"from sut_inventory.lifecycle_hooks — "
                                f"dropping them produces a zero-arg call "
                                f"that fails codegen reconciliation as "
                                f"arity_mismatch."
                            )

        # Navigation-precondition gate (the "wrong screen" defect). Some reused
        # POM methods act on a specific already-active view (grid/table/tab)
        # with no in-code guard for it — the requirement is a pure calling
        # convention, only visible in sut_inventory.navigation_preconditions[]
        # (mined by Step 6 from the SUT's own real call sites). Unlike the
        # open-before-login gate above, this ALWAYS builds the full sequence
        # regardless of whether the before_each hook is reused or created: a
        # trusted reused hook can legitimately satisfy a precondition, but the
        # gap this catches is typically in a LATER steps[] entry, not the hook
        # itself (a common shape: the hook is a verbatim, correct reuse; the
        # missing call belonged before a later arrange step reusing a grid/
        # filter POM method whose precondition wasn't declared in the hook).
        nav_preconditions = [
            np_ for np_ in (active_module or {}).get("navigation_preconditions") or []
            if isinstance(np_, dict) and np_.get("method") and np_.get("requires_call")
        ]
        if nav_preconditions:
            def _split_class_method(ref: str) -> tuple[str, str]:
                if "." in ref:
                    cls, _, meth = ref.rpartition(".")
                    return cls, meth
                return "", ref

            def _call_matches(call: dict, cls: str, meth: str) -> bool:
                call_method = call.get("method") or ""
                if call_method != meth:
                    return False
                call_pom = call.get("pom") or ""
                return not cls or not call_pom or call_pom == cls

            full_seq: list[dict] = []
            for h in tc.get("hooks") or []:
                if isinstance(h, dict) and h.get("event") == "before_each":
                    full_seq.extend(
                        c for c in (h.get("calls") or []) if isinstance(c, dict)
                    )
            for fn in tc.get("test_functions") or []:
                full_seq.extend(
                    st for st in (fn.get("steps") or []) if isinstance(st, dict)
                )

            for np_ in nav_preconditions:
                dep_cls, dep_method = _split_class_method(str(np_["method"]))
                req_cls, req_method = _split_class_method(str(np_["requires_call"]))
                for i, call in enumerate(full_seq):
                    if not _call_matches(call, dep_cls, dep_method):
                        continue
                    satisfied = any(
                        _call_matches(full_seq[j], req_cls, req_method)
                        for j in range(i)
                    )
                    if satisfied:
                        continue
                    args_hint = np_.get("requires_args_hint")
                    hint = f" (typically with `{args_hint}`)" if args_hint else ""
                    evidence = np_.get("evidence") or "no evidence recorded"
                    violations.append(
                        f"{tc_id}: choreography calls "
                        f"`{call.get('pom')}.{call.get('method')}` but its "
                        f"required prior call `{np_['requires_call']}`{hint} does "
                        f"not appear earlier in this test function's before_each "
                        f"hook or steps[] (sut_inventory.navigation_preconditions "
                        f"evidence: {evidence}). Add an arrange step invoking it "
                        f"before this step."
                    )

    return violations


def _render_plan_markdown(plan: dict) -> str:
    """Deterministic human-readable view of the code-modification plan.

    Surfaced by the post-step-7 review gate so a senior tester can scan the
    architect's decisions in ~30s before authorizing Step 8.
    """
    lines: list[str] = []
    active = plan.get("active_module") or "?"
    lang = plan.get("language") or "?"
    framework = plan.get("framework") or "?"
    test_cases = plan.get("test_cases") or []
    lines.append(f"# Code Modification Plan - {active}")
    lines.append("")
    lines.append(
        f"- Plan version: `{plan.get('plan_version', '?')}` | "
        f"Language: `{lang}` | Framework: `{framework}` | "
        f"Test cases: **{len(test_cases)}**"
    )
    if plan.get("notes"):
        lines.append(f"- Notes: {plan['notes']}")
    lines.append("")

    for tc in test_cases:
        tc_id = tc.get("id") or "<no-id>"
        title = tc.get("title") or ""
        target = tc.get("test_file_target") or "?"
        lines.append(f"## {tc_id}{(' - ' + title) if title else ''}")
        lines.append("")
        lines.append(f"- Target file: `{target}`")

        funcs = tc.get("test_functions") or []
        if funcs:
            for fn in funcs:
                name = fn.get("name") or "?"
                markers = ", ".join(fn.get("markers") or []) or "-"
                uses = ", ".join(fn.get("uses_fixtures") or []) or "-"
                lines.append(
                    f"  - `{name}` markers=[{markers}] fixtures=[{uses}]"
                )
                steps = sorted(
                    (s for s in (fn.get("steps") or []) if isinstance(s, dict)),
                    key=lambda s: s.get("order") or 0,
                )
                for st in steps:
                    pom = st.get("pom") or "?"
                    method = st.get("method") or "?"
                    loc = st.get("locator")
                    loc_s = f" via `{loc}`" if loc else ""
                    lines.append(
                        f"    {st.get('order', '?')}. `{pom}.{method}(...)`{loc_s}"
                    )

        hooks = tc.get("hooks") or []
        if hooks:
            lines.append("- Hooks:")
            for h in hooks:
                event = h.get("event") or "?"
                src = h.get("source") or "?"
                calls = ", ".join(
                    f"`{c.get('pom')}.{c.get('method')}(...)`"
                    for c in (h.get("calls") or [])
                    if isinstance(c, dict)
                ) or "-"
                if src == "reuse":
                    ref = h.get("from") or "?"
                    lines.append(
                        f"  - `{event}` - reuse from `{ref}` calls=[{calls}]"
                    )
                else:
                    lines.append(f"  - `{event}` - {src} calls=[{calls}]")

        for label, key in (
            ("Fixtures", "fixtures"),
            ("Page objects", "page_objects"),
            ("Helpers", "helpers"),
            ("Locators", "locators"),
        ):
            entries = tc.get(key) or []
            if not entries:
                continue
            lines.append(f"- {label}:")
            for e in entries:
                name = e.get("name") or "?"
                src = e.get("source") or "?"
                if src in ("reuse",):
                    ref = e.get("from") or "?"
                    lines.append(f"  - `{name}` - reuse from `{ref}`")
                elif src == "create":
                    at = e.get("at") or "?"
                    extra = ""
                    missing = e.get("missing_methods") or []
                    if missing:
                        sigs = ", ".join(
                            f"`{m.get('name')}{m.get('signature') or ''}`"
                            for m in missing
                        )
                        extra = f" + missing: {sigs}"
                    lines.append(f"  - `{name}` - create at `{at}`{extra}")
                elif src == "create_tbd":
                    intent = e.get("intent") or "?"
                    owner = e.get("owning_page") or ""
                    owner_s = f" ({owner})" if owner else ""
                    lines.append(
                        f"  - `{name}`{owner_s} - create_tbd intent: \"{intent}\""
                    )
                else:
                    lines.append(f"  - `{name}` - {src}")

        # Exemplar (non-POM) lane: render reusable_units in place of
        # page_objects, and surface their deferred_targets[] as TBD locators.
        units = tc.get("reusable_units") or []
        if units:
            lines.append("- Reusable units:")
            for u in units:
                name = u.get("name") or "?"
                src = u.get("source") or "?"
                cat = u.get("category")
                cat_s = f" [{cat}]" if cat else ""
                if src == "reuse":
                    ref = u.get("from") or "?"
                    lines.append(f"  - `{name}`{cat_s} - reuse from `{ref}`")
                else:
                    at = u.get("at") or "?"
                    extra = ""
                    mb = u.get("missing_behaviors") or []
                    if mb:
                        # reusable_units signatures already include the method
                        # name (e.g. "answered_by(self, actor) -> bool"), unlike
                        # POM missing_methods — render the signature verbatim.
                        sigs = ", ".join(
                            f"`{m.get('signature') or m.get('name') or '?'}`"
                            for m in mb
                        )
                        extra = f" + behaviors: {sigs}"
                    lines.append(f"  - `{name}`{cat_s} - create at `{at}`{extra}")
            tbd = [
                (dt.get("name") or "?", dt.get("intent") or "?", u.get("name") or "?")
                for u in units
                for dt in (u.get("deferred_targets") or [])
            ]
            if tbd and not (tc.get("locators") or []):
                lines.append("- Locators (TBD):")
                for n, intent, owner in tbd:
                    lines.append(
                        f"  - `{n}` ({owner}) - create_tbd intent: \"{intent}\""
                    )
        lines.append("")

    if not test_cases:
        lines.append("_No test cases planned._")
        lines.append("")
    return "\n".join(lines)


class TestArchitectStep(Step):
    number = 7
    name = "test-automation-architect"
    timeout_s = step_timeout(7)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)
        sut_root = ctx.workspace.sut.resolve()

        # --- Pre-flight (fail in <1s) -------------------------------------
        strategy_md = ctx.workspace.step_dir(4) / "test-design.md"
        if not strategy_md.exists():
            return StepResult(
                success=False, status="failed", outputs=[],
                error=f"missing {strategy_md}; run step 4 first",
            )

        sut_inv_json = ctx.workspace.step_dir(6) / "sut_inventory.json"
        if not sut_inv_json.exists():
            return StepResult(
                success=False, status="failed", outputs=[],
                error=(
                    "step 7 requires sut_inventory.json from step 6. "
                    "Run step 6 first (drop --only-step 7, or use --from-step 6)."
                ),
            )

        try:
            sut_inventory = json.loads(sut_inv_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return StepResult(
                success=False, status="failed", outputs=[],
                error=f"unreadable sut_inventory.json: {e}",
            )

        active_module = _active_module_dict(sut_inventory)
        if not active_module:
            return StepResult(
                success=False, status="failed", outputs=[],
                error=(
                    "sut_inventory has no resolved active_module. Either step 6 "
                    "hard-failed and was force-skipped, or --module needs to "
                    "be passed for monorepo SUTs."
                ),
            )

        # --- Inline inputs ----------------------------------------------
        # Direct-SDK transport: inputs are embedded as fenced markdown
        # sections in the user prompt by call_reasoning_llm, not staged
        # as workdir files.
        inputs: dict[str, str] = {
            "test-design.md": strategy_md.read_text(encoding="utf-8"),
            "sut_inventory.json": sut_inv_json.read_text(encoding="utf-8"),
        }
        research_md = ctx.workspace.step_dir(6) / "research.md"
        if research_md.exists():
            inputs["research.md"] = research_md.read_text(encoding="utf-8")

        # --- Pre-exploration authentication (mode-switchable) ------------
        # mode=headed (default): open the base URL in a VISIBLE browser and let
        #   the human log in by any means (MFA/SSO) — session captured to the SUT
        #   convention path, which explore picks up via resolve() (no login_spec,
        #   no SUT env). Falls back to mcp if qtea's Playwright isn't installed.
        # mode=mcp: the site-explorer logs in via Playwright MCP and explores in
        #   the SAME session — `login_spec` is passed to the explore call below.
        # mode=script: run the SUT's own sign-in helper in a subprocess to
        #   produce a storage-state (best-effort; needs the SUT env prewarmed).
        # mode=off: explore unauthenticated.
        from qtea import storage_state as _storage_state
        from qtea.steps.s07_auth_prewarm import (
            auth_prewarm_mode,
            headed_mode_requested,
            is_interactive_session,
            login_identity_provider,
            maybe_headed_prewarm,
            maybe_prewarm_auth,
            resolve_login_credentials,
        )

        # research.json feeds both mcp-login credential resolution (below) and
        # live exploration (further down), so load it before the auth block.
        research_dict: dict | None = None
        research_json = ctx.workspace.step_dir(6) / "research.json"
        if research_json.exists():
            try:
                research_dict = json.loads(research_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                research_dict = None

        _prewarm_mode = auth_prewarm_mode(ctx.options)
        login_spec: LoginSpec | None = None
        if _prewarm_mode == "headed":
            # Headed (human-driven) login is the default: open the base URL in a
            # visible browser and let the operator complete MFA/SSO, then capture
            # the session. Falls back to mcp when qtea's Playwright isn't
            # installed. On success the storage-state lands at the SUT convention
            # path and explore_strategy_routes picks it up via resolve() — so no
            # login_spec is needed (creds never reach the model here).
            try:
                _status = await maybe_headed_prewarm(
                    sut_root=sut_root,
                    workspace_root=ctx.workspace.root,
                    active_module=active_module,
                    base_url=_resolve_base_url(research_dict),
                    research=research_dict,
                    cli_storage_state=getattr(ctx.options, "storage_state", None),
                    no_auth_capture=getattr(ctx.options, "no_auth_capture", False),
                    interactive=is_interactive_session(ctx.options),
                )
            except Exception as e:  # never let auth prewarm break Step 7
                log.warning("step07.auth_prewarm_unexpected_error", error=str(e))
                _status = "skipped"
            if _status == "fallback_mcp":
                _prewarm_mode = "mcp"  # qtea Playwright missing → automated login

        if _prewarm_mode == "script":
            try:
                await maybe_prewarm_auth(
                    sut_root=sut_root,
                    workspace_root=ctx.workspace.root,
                    active_module=active_module,
                    cli_storage_state=getattr(ctx.options, "storage_state", None),
                    no_auth_capture=getattr(ctx.options, "no_auth_capture", False),
                    headed_requested=headed_mode_requested(ctx.options),
                    interactive=is_interactive_session(ctx.options),
                )
            except Exception as e:  # never let auth prewarm break Step 7
                log.warning("step07.auth_prewarm_unexpected_error", error=str(e))
        elif _prewarm_mode == "mcp":
            # Build a login only when no session already resolves (reuse first)
            # and credentials are available. explore_strategy_routes registers
            # the credentials for redaction before they enter the prompt.
            try:
                _has_state = _storage_state.resolve(
                    sut_root=sut_root,
                    workspace_root=ctx.workspace.root,
                    cli_opt=getattr(ctx.options, "storage_state", None),
                ) is not None
                _creds = None if _has_state else resolve_login_credentials(active_module, research_dict)
                if _creds is not None:
                    login_spec = LoginSpec(
                        username=_creds[0], password=_creds[1],
                        provider=login_identity_provider(),
                    )
                    log.info("step07.mcp_login_enabled")
                elif not _has_state:
                    log.info("step07.mcp_login_skip", reason="no_credentials")
            except Exception as e:  # never let login setup break Step 7
                log.warning("step07.mcp_login_setup_error", error=str(e))

        # --- Pre-codegen live exploration (Gap A) ---
        # Before the architect plans, open the SUT and confirm the routes named
        # in the strategy actually exist + capture a light structural digest.
        # Best-effort + gated (QTEA_LIVE_EXPLORE); on skip/failure live_map is
        # None and planning proceeds from the static inventory as before.
        # Reuse across attempts: live exploration depends only on test-design.md
        # (fixed for the whole step) and the running SUT, neither of which a
        # retry changes — only the planning prompt does. A prior attempt's
        # live-map.json is therefore still valid, so reload it instead of
        # re-booting the Playwright MCP browser and re-probing every route
        # (a full site-explorer agent run — the expensive part of Step 7).
        live_map = None
        cached_map_path = out_dir / "live-map.json"
        if cached_map_path.exists():
            try:
                live_map = json.loads(cached_map_path.read_text(encoding="utf-8"))
                log.info(
                    "step07.live_explore_reused",
                    path=str(cached_map_path),
                    routes=len(live_map.get("routes") or [])
                    if isinstance(live_map, dict) else 0,
                )
            except (OSError, json.JSONDecodeError) as e:
                log.warning("step07.live_explore_cache_unreadable", error=str(e))
                live_map = None
        if live_map is None:
            try:
                live_map = await explore_strategy_routes(
                    strategy_text=inputs["test-design.md"],
                    research=research_dict,
                    sut_root=sut_root,
                    workspace_root=ctx.workspace.root,
                    out_dir=out_dir,
                    workdir=wd / "live-explore",
                    cli_storage_state=getattr(ctx.options, "storage_state", None),
                    login=login_spec,
                    auth_mode=_prewarm_mode,
                )
            except Exception as e:  # never let exploration break Step 7
                log.warning("step07.live_explore_unexpected_error", error=str(e))
        live_map_clause = render_live_map_for_prompt(live_map)
        if live_map is not None:
            inputs["live-map.json"] = json.dumps(live_map, indent=2, ensure_ascii=False)
            # Surface silent coverage loss when the driver truncated the plan.
            # The plan asked for more targets than the run-time cap allowed —
            # the operator should know so they can raise the cap or trim the
            # plan rather than silently missing target pages.
            _tel = live_map.get("_telemetry") if isinstance(live_map, dict) else None
            if isinstance(_tel, dict):
                _truncated = int(_tel.get("routes_truncated_by_cap") or 0)
                if _truncated > 0:
                    log.warning(
                        "step07.live_explore_plan_truncated",
                        truncated=_truncated,
                        requested=_tel.get("routes_requested_by_plan"),
                        explored=_tel.get("routes_explored"),
                    )

        skill_path = (
            package_resource_root() / "skills"
            / "analyze-sut-structure" / "SKILL.md"
        )
        if skill_path.is_file():
            inputs["analyze-sut-structure.md"] = skill_path.read_text(
                encoding="utf-8"
            )

        # Inline the source of every existing POM / fixture / helper for the
        # active module so the architect can verify reuse FIT (not just
        # existence) when deciding `source: reuse` and writing the required
        # `reuse_justification`. Budget-capped; skipped files are surfaced in
        # the prompt so the architect knows what it can't see.
        reuse_sources, reuse_skipped = _inline_reuse_sources(
            active_module, sut_root,
        )
        inputs.update(reuse_sources)
        log.info(
            "step07.reuse_sources_inlined",
            count=len(reuse_sources),
            skipped=len(reuse_skipped),
            total_chars=sum(len(v) for v in reuse_sources.values()),
        )

        agent = package_resource_root() / "agents" / "test-automation-architect.agent.md"

        active_name = active_module.get("name") or sut_inventory.get("active_module") or "?"
        language = active_module.get("language") or "unknown"

        skipped_clause = (
            f"Files skipped (over reuse-source budget): {', '.join(reuse_skipped)}."
            if reuse_skipped
            else "All POM/fixture/helper sources for the active module were inlined."
        )

        # When the previous attempt failed with a category that has a
        # prompt clarification hint (schema-type-mismatch, schema-missing-
        # field, json-unparseable — see failure_classifiers.py), prepend
        # the clarification verbatim so the architect sees the specific
        # guidance for the second attempt rather than re-running the
        # identical prompt that just failed.
        prompt_clarification = ctx.extras.pop("prompt_clarification", None)
        clarification_block = (
            f"**Smart-retry guidance from the previous failed attempt:** "
            f"{prompt_clarification}\n\n"
            if isinstance(prompt_clarification, str) and prompt_clarification.strip()
            else ""
        )

        # Pattern-aware branch. For POM-family SUTs the mature POM contract
        # (page_objects[] + missing_methods + locators[]) applies unchanged.
        # For non-POM SUTs (e.g. Screenplay) the inventory carries
        # `pattern_exemplars[]` — verbatim snippets of the SUT's OWN reusable
        # units — and the architect must plan `reusable_units[]` shaped like
        # them instead of forcing POM.
        arch_pattern = active_module.get("architecture_pattern") or "unknown"
        exemplar_count = len(active_module.get("pattern_exemplars") or [])
        if arch_pattern not in ("pom", "inline", "none", "unknown"):
            pattern_clause = (
                f"\n\n**ARCHITECTURE PATTERN = `{arch_pattern}` (NON-POM).** This "
                f"SUT does NOT use Page Object Model. Do NOT emit `page_objects[]` "
                f"or POM `missing_methods`. Instead, for each test case emit "
                f"`reusable_units[]`: new or reused units (Tasks/Questions/"
                f"Interactions/etc.) SHAPED LIKE the {exemplar_count} verbatim "
                f"`pattern_exemplars[]` in `sut_inventory.json` (each has "
                f"`category`, `class_name`, `dir`, and an `excerpt`). Place new "
                f"units (`source: create`, `at: <path>`) in the SAME directory as "
                f"the exemplar of the matching `category` (use its `dir`); set "
                f"`shaped_like` to that exemplar's index. Describe behaviours the "
                f"test needs in `missing_behaviors[]` (name, signature, kind). For "
                f"element locators the unit needs, list `deferred_targets[]` "
                f"(name + one-line intent) — Step 8 backs each with qtea's JIT "
                f"resolver; do NOT hardcode selectors. Use `reusable_units` — "
                f"NOT `page_objects` — for all reusable code in this plan."
            )
        else:
            pattern_clause = ""

        user_prompt = (
            f"{clarification_block}"
            f"The inputs below are inlined: `sut_inventory.json` "
            f"(top-level `active_module` = `{active_name}`, language "
            f"`{language}`), `test-design.md`, and optionally `research.md`. "
            f"For every test case in the strategy, decide where new code "
            f"goes and which existing fixtures / page objects / helpers / "
            f"locators get reused. Respond with the `code-modification-plan` "
            f"JSON object only — the schema is enforced via structured "
            f"outputs and the pipeline renders the human-readable summary "
            f"locally. Do NOT scan the SUT — trust the inventory. Do NOT "
            f"include method bodies or selector strings — only structural "
            f"decisions (paths, names, signatures, intents). "
            f"The input `analyze-sut-structure.md` provides a procedure for "
            f"interpreting the inventory — follow its POM ownership tree and "
            f"reuse-first checklist when making placement decisions. "
            f"\n\nThe inputs keyed under `reuse-source/<path>` contain the "
            f"FULL source text of every existing POM, fixture, and helper "
            f"file the inventory lists for the active module. Read these "
            f"BEFORE writing any `source: reuse` entry — the inventory tells "
            f"you what exists, but only the source tells you whether the "
            f"existing symbol's behaviour FITS this test case. Every "
            f"`source: reuse` entry MUST include a `reuse_justification` "
            f"field (one sentence, ≤200 chars) that names the concrete "
            f"matching dimension you observed in the source. If you cannot "
            f"name one, emit `source: create` instead. {skipped_clause}"
            f"{live_map_clause}"
            f"{pattern_clause}"
        )

        result = await call_reasoning_llm(
            agent,
            workdir=wd,
            user_prompt=user_prompt,
            inputs=inputs,
            output_schema=load_schema("code-modification-plan"),
            timeout_s=self.timeout_s,
            step=7,
            max_tokens=32000,
        )

        if not result.success or not result.final_text:
            log.error(
                "step07.agent_produced_no_output",
                error=result.error,
            )
            return StepResult(
                success=False, status="failed", outputs=[],
                error=result.error or "agent produced no output",
            )

        # --- Validate ----------------------------------------------------
        # Persist the raw agent output BEFORE attempting to parse/validate.
        # This guarantees a human can always inspect what the architect
        # actually returned when something downstream rejects it — the
        # alternative (which we lived with on run 20260614-190647-ab7dac)
        # is a silent failure with no artifact and no log entry naming the
        # reason.
        raw_dump_path = out_dir / "agent-output-raw.txt"
        try:
            raw_dump_path.write_text(result.final_text, encoding="utf-8")
        except OSError as e:
            log.warning("step07.raw_dump_failed", error=str(e))

        try:
            plan: dict[str, Any] = json.loads(result.final_text)
        except json.JSONDecodeError as e:
            log.error(
                "step07.plan_unparseable",
                error=str(e),
                raw_dump=str(raw_dump_path),
                text_len=len(result.final_text),
            )
            return StepResult(
                success=False, status="failed",
                outputs=[raw_dump_path] if raw_dump_path.exists() else [],
                error=f"plan JSON unparseable: {e}",
            )

        plan = normalize_arrays(plan, "code-modification-plan")

        # The design pattern is a deterministic Step-6 fact, not the agent's to
        # invent — stamp it onto the plan authoritatively so Step 8 selects the
        # right codegen lane. (The agent may echo it; the inventory wins.)
        plan["architecture_pattern"] = (
            active_module.get("architecture_pattern")
            or plan.get("architecture_pattern")
            or "pom"
        )

        ok_schema, schema_err = is_valid(plan, "code-modification-plan")
        if not ok_schema:
            # Also persist the PARSED plan so the human can diff against
            # the schema rather than re-parse the raw text.
            rejected_path = out_dir / "plan-rejected.json"
            try:
                rejected_path.write_text(
                    json.dumps(plan, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                rejected_path = raw_dump_path
            log.error(
                "step07.plan_schema_invalid",
                error=schema_err,
                rejected=str(rejected_path),
                test_case_count=len(plan.get("test_cases") or []),
            )
            outs = [p for p in (raw_dump_path, rejected_path) if p.exists()]
            return StepResult(
                success=False, status="failed", outputs=outs,
                error=f"plan failed schema validation: {schema_err}",
                notes=f"see {rejected_path.name}",
            )

        gate_violations = _validate_plan_against_inventory(plan, active_module)
        if gate_violations:
            (out_dir / "plan-violations.log").write_text(
                "\n".join(gate_violations), encoding="utf-8",
            )
            log.error(
                "step07.plan_gate_violations",
                count=len(gate_violations),
                first=gate_violations[0],
            )
            # Arm the FULL violation list (not the 5-entry/500-char summary
            # in `notes`, which is sized for human display) so attempt 2's
            # `run()` can inject it verbatim via `clarification_block`
            # (picked up through `ctx.extras["prompt_clarification"]` — see
            # `classify_failure`'s PLAN_GATE_VIOLATION category). Capped at
            # 60 entries as a defensive bound on prompt size; that ceiling
            # has never been approached in practice.
            _shown = gate_violations[:60]
            _clarification = (
                "On the previous attempt, your plan passed schema "
                "validation but the phase gate rejected it for the "
                "following specific rule violation(s). Fix ONLY these "
                "issues — every other placement/reuse/classification "
                "decision that didn't trigger a violation was correct and "
                "should be preserved as-is:\n\n"
                + "\n".join(f"- {v}" for v in _shown)
                + (
                    f"\n\n(+{len(gate_violations) - 60} more violation(s) "
                    f"omitted for length — fixing the above will likely "
                    f"resolve the same root cause repeated across cases.)"
                    if len(gate_violations) > 60 else ""
                )
            )
            ctx.extras["prompt_clarification"] = _clarification
            return StepResult(
                success=False, status="failed",
                outputs=[out_dir / "plan-violations.log"],
                error=f"plan phase-gate failed: {len(gate_violations)} violation(s)",
                notes="\n".join(gate_violations[:5])[:500],
            )

        # --- Persist + commit -------------------------------------------
        json_dst = out_dir / "code-modification-plan.json"
        json_dst.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        md_dst = out_dir / "code-modification-plan.md"
        md_dst.write_text(_render_plan_markdown(plan), encoding="utf-8")

        # Commit the plan artifact onto the qtea branch alongside the
        # other per-step commits. This step doesn't touch SUT source files,
        # but committing keeps the audit trail consistent.
        tc_count = len(plan.get("test_cases") or [])
        sha = commit_step(
            sut_root, self.number, self.name,
            message_detail=f"{tc_count} test cases planned",
        )

        notes = f"test_cases={tc_count} active_module={active_name}"
        if sha:
            notes += f" commit={sha}"

        return StepResult(
            success=True, status="completed",
            outputs=[json_dst] + ([md_dst] if md_dst.exists() else []),
            notes=notes,
        )
