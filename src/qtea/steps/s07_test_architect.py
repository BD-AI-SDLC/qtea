"""Step 7: Test Architect — produces the code-modification plan.

Inputs: test-strategy.md (step 4) + sut_inventory.json (step 6) + research.md
(step 6, for narrative context).

Output (artifacts/step07/):
  - code-modification-plan.json   (structured plan, schema-validated)
  - code-modification-plan.md     (human-readable summary for review gate)

Behavior:
  1. Pre-flight: SUT materialized, test-strategy.md present, sut_inventory.json
     present with a resolved active_module. Any miss → fail in <1s.
  2. Inline the upstream artifacts into the agent's user prompt.
  3. Invoke the `test-architect` agent via direct Anthropic SDK with the
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

    The test-architect agent has no file tools; it can only justify reuse
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
            for mm in po.get("missing_methods") or []:
                if not mm.get("signature"):
                    violations.append(
                        f"{tc_id}: page_object `{po.get('name')}` missing_method "
                        f"`{mm.get('name')}` has no signature"
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

        # Choreography gate: every steps[] entry must reference a POM and
        # (optionally) a locator planned within the SAME test case. `pom` +
        # `locator` are hard-checked against the TC's own page_objects /
        # locators (a mismatch means the writer would emit a call on a class
        # or constant that doesn't exist in this file). `method` is soft-
        # checked: it may be an existing reused method (not enumerated in the
        # plan) OR a missing_methods entry — so a miss only logs a warning.
        tc_pom_names = {
            po.get("name") for po in (tc.get("page_objects") or [])
            if isinstance(po, dict) and po.get("name")
        }
        tc_locator_names = {
            lc.get("name") for lc in (tc.get("locators") or [])
            if isinstance(lc, dict) and lc.get("name")
        }
        tc_missing_methods: dict[str, set[str]] = {}
        for po in tc.get("page_objects") or []:
            if not isinstance(po, dict) or not po.get("name"):
                continue
            tc_missing_methods[po["name"]] = {
                mm.get("name") for mm in (po.get("missing_methods") or [])
                if isinstance(mm, dict) and mm.get("name")
            }
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
        lines.append("")

    if not test_cases:
        lines.append("_No test cases planned._")
        lines.append("")
    return "\n".join(lines)


class TestArchitectStep(Step):
    number = 7
    name = "test-architect"
    timeout_s = step_timeout(7)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)
        sut_root = ctx.workspace.sut.resolve()

        # --- Pre-flight (fail in <1s) -------------------------------------
        strategy_md = ctx.workspace.step_dir(4) / "test-strategy.md"
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
            "test-strategy.md": strategy_md.read_text(encoding="utf-8"),
            "sut_inventory.json": sut_inv_json.read_text(encoding="utf-8"),
        }
        research_md = ctx.workspace.step_dir(6) / "research.md"
        if research_md.exists():
            inputs["research.md"] = research_md.read_text(encoding="utf-8")

        # --- Pre-codegen live exploration (Gap A) ---
        # Before the architect plans, open the SUT and confirm the routes named
        # in the strategy actually exist + capture a light structural digest.
        # Best-effort + gated (QTEA_LIVE_EXPLORE); on skip/failure live_map is
        # None and planning proceeds from the static inventory as before.
        research_dict: dict | None = None
        research_json = ctx.workspace.step_dir(6) / "research.json"
        if research_json.exists():
            try:
                research_dict = json.loads(research_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                research_dict = None
        live_map = None
        try:
            live_map = await explore_strategy_routes(
                strategy_text=inputs["test-strategy.md"],
                research=research_dict,
                sut_root=sut_root,
                workspace_root=ctx.workspace.root,
                out_dir=out_dir,
                workdir=wd / "live-explore",
                cli_storage_state=getattr(ctx.options, "storage_state", None),
            )
        except Exception as e:  # never let exploration break Step 7
            log.warning("step07.live_explore_unexpected_error", error=str(e))
        live_map_clause = render_live_map_for_prompt(live_map)
        if live_map is not None:
            inputs["live-map.json"] = json.dumps(live_map, indent=2, ensure_ascii=False)

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

        agent = package_resource_root() / "agents" / "test-architect.agent.md"

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

        user_prompt = (
            f"{clarification_block}"
            f"The inputs below are inlined: `sut_inventory.json` "
            f"(top-level `active_module` = `{active_name}`, language "
            f"`{language}`), `test-strategy.md`, and optionally `research.md`. "
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
