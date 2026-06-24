"""Step 8.5 — semantic preflight.

Runs after the Step 8 violation gate but before the commit. Catches three
classes of defect that would otherwise surface only at Step 9 attempt-1
collection-time inside the full test runner:

  1. ``ast.parse`` failures in any generated Python test file (syntax /
     indentation errors that slipped past the agent and the code-fence stripper).
  2. Fixture-dependency graph defects: cycles in ``depends_on`` chains, or
     ``depends_on`` references that resolve to nothing in the plan + inventory.
  3. Sentinel-constant existence: for each ``LocatorClass.CONSTANT`` reference
     in a generated test file, the constant must be defined in the locator
     class file from the inventory.

Each defect is returned as a :class:`qtea.test_indexer.Violation` with
``rule="preflight-error"`` and ``severity="error"`` so the existing Step 8
reject machinery in ``s08_codegen.py`` handles them uniformly. Preflight is
language-aware — sub-check (1) and (3) only run on Python-family stacks
(pytest / playwright-py / selenium-py); sub-check (2) is plan-driven and
runs on every stack.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from qtea.test_indexer import Violation

_PYTHON_FAMILY_FRAMEWORKS: frozenset[str] = frozenset({
    "pytest", "playwright-py", "selenium-py",
})

# Pytest-injected and conftest-injected fixtures we never want to flag as
# missing — they are not declared in the plan because they are provided by
# the framework itself. Conservative list: the universally-shipped pytest
# / playwright-pytest fixtures plus a few common conftest names. Anything
# else is checked normally.
_BUILTIN_FIXTURE_ALLOWLIST: frozenset[str] = frozenset({
    # pytest core
    "tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory",
    "monkeypatch", "capsys", "capsysbinary", "capfd", "capfdbinary",
    "caplog", "request", "pytestconfig", "record_property",
    "record_xml_attribute", "record_testsuite_property", "recwarn",
    "doctest_namespace", "cache",
    # playwright-pytest
    "page", "context", "browser", "browser_type", "playwright",
    "browser_name", "browser_channel", "is_chromium", "is_firefox",
    "is_webkit", "browser_type_launch_args", "browser_context_args",
    "new_page", "new_context", "launch_browser",
})

# Reference: "ClassName.CONSTANT_NAME" (uppercase constant after dot).
_CONSTANT_REF_PATTERN = re.compile(
    r"\b(?P<cls>[A-Z][A-Za-z0-9_]*)\.(?P<const>[A-Z][A-Z0-9_]+)\b"
)


def _ast_parse_python_tests(
    sut_root: Path, generated_files: set[str], framework: str,
) -> list[Violation]:
    """Sub-check (1): every generated `.py` test file must parse.

    Catches the run-20260611-075728 class of defect where Phase B emitted
    a reasoning preamble before the file's first import, breaking
    SyntaxError-at-collection.
    """
    if framework not in _PYTHON_FAMILY_FRAMEWORKS:
        return []
    out: list[Violation] = []
    for rel in sorted(generated_files):
        if not rel.endswith(".py"):
            continue
        abs_path = (sut_root / rel)
        if not abs_path.is_file():
            continue
        try:
            source = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            ast.parse(source, filename=str(abs_path))
        except SyntaxError as e:
            out.append(
                Violation(
                    rule="preflight-error",
                    file=rel,
                    line=e.lineno or 1,
                    snippet=f"ast.parse: {e.msg}",
                    severity="error",
                )
            )
    return out


def _build_fixture_index(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect every fixture entry from the plan keyed by name. Last write
    wins on duplicates — the plan's reuse/create classification typically
    repeats fixtures across test cases."""
    out: dict[str, dict[str, Any]] = {}
    for tc in plan.get("test_cases") or []:
        for fix in tc.get("fixtures") or []:
            name = (fix.get("name") or "").strip()
            if name:
                out[name] = fix
    return out


def _known_inventory_fixtures(inventory: dict[str, Any] | None) -> set[str]:
    """Names of fixtures the SUT already provides per ``sut_inventory.json``.
    Plan fixtures whose ``source="reuse"`` should resolve to one of these.
    """
    out: set[str] = set()
    if not isinstance(inventory, dict):
        return out
    for module in inventory.get("modules") or []:
        if not isinstance(module, dict):
            continue
        for fix in module.get("existing_fixtures") or []:
            name = (fix.get("name") if isinstance(fix, dict) else "") or ""
            if name:
                out.add(name)
    return out


def _check_fixture_graph(
    plan: dict[str, Any], inventory: dict[str, Any] | None,
) -> list[Violation]:
    """Sub-check (2): the plan's fixture dependency graph must be acyclic and
    every ``depends_on`` reference must resolve.

    Violations are reported against ``code-modification-plan.json`` so the
    reviewer knows the fix target is the plan, not generated test code.
    """
    fixtures = _build_fixture_index(plan)
    if not fixtures:
        return []
    inventory_names = _known_inventory_fixtures(inventory)
    out: list[Violation] = []

    # Missing-reference check: every `depends_on` name must resolve.
    for name, fix in fixtures.items():
        deps = fix.get("depends_on") or []
        if not isinstance(deps, list):
            continue
        for dep in deps:
            dep_name = (dep or "").strip() if isinstance(dep, str) else ""
            if not dep_name:
                continue
            if dep_name in fixtures or dep_name in inventory_names \
                    or dep_name in _BUILTIN_FIXTURE_ALLOWLIST:
                continue
            out.append(
                Violation(
                    rule="preflight-error",
                    file="code-modification-plan.json",
                    line=1,
                    snippet=(
                        f"fixture {name!r} depends_on {dep_name!r}, which is "
                        f"not declared in the plan, the SUT inventory, or "
                        f"the pytest builtin allowlist"
                    ),
                    severity="error",
                )
            )

    # Cycle check: DFS with a recursion stack. Reports the first cycle found
    # for each starting node; deduplicates cycles by canonical edge set so
    # a 3-node cycle isn't reported 3 times.
    seen_cycles: set[frozenset[tuple[str, str]]] = set()
    for start in fixtures:
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for dep in (fixtures.get(node, {}).get("depends_on") or []):
                if not isinstance(dep, str) or dep not in fixtures:
                    continue
                if dep in path:
                    # Found a cycle.
                    cycle_path = path[path.index(dep):] + [dep]
                    edges = frozenset(
                        (cycle_path[i], cycle_path[i + 1])
                        for i in range(len(cycle_path) - 1)
                    )
                    if edges in seen_cycles:
                        continue
                    seen_cycles.add(edges)
                    out.append(
                        Violation(
                            rule="preflight-error",
                            file="code-modification-plan.json",
                            line=1,
                            snippet=(
                                f"fixture dependency cycle: "
                                f"{' -> '.join(cycle_path)}"
                            ),
                            severity="error",
                        )
                    )
                    continue
                stack.append((dep, path + [dep]))
    return out


def _collect_locator_class_files(
    plan: dict[str, Any], inventory: dict[str, Any] | None,
) -> dict[str, str]:
    """Map LocatorClassName -> SUT-relative file path.

    Joins three sources in priority order: (a) plan's `locators[].class`
    annotations, (b) inventory's existing_locators, (c) inferred via plan's
    `page_objects[]` (POMs typically pair with a `<Name>Locators` class in
    the same dir).
    """
    out: dict[str, str] = {}
    if isinstance(inventory, dict):
        for module in inventory.get("modules") or []:
            if not isinstance(module, dict):
                continue
            for loc in module.get("existing_locators") or []:
                if not isinstance(loc, dict):
                    continue
                cls = loc.get("class_name") or loc.get("name")
                f = loc.get("file")
                if cls and f and cls not in out:
                    out[cls] = f
    # The plan's `page_objects` carries POM/locator pairings; harvest them.
    for tc in plan.get("test_cases") or []:
        for po in tc.get("page_objects") or []:
            if not isinstance(po, dict):
                continue
            name = po.get("name") or ""
            locator_cls = f"{name}Locators" if name else ""
            locator_file = po.get("locator_file") or po.get("locators_file")
            if locator_cls and locator_file and locator_cls not in out:
                out[locator_cls] = locator_file
    return out


def _check_sentinel_constants(
    sut_root: Path,
    generated_files: set[str],
    plan: dict[str, Any],
    inventory: dict[str, Any] | None,
    framework: str,
) -> list[Violation]:
    """Sub-check (3): every `LocatorClass.CONSTANT` reference in a generated
    Python test file must resolve to a definition in the matching locator file.

    Tolerant: when we cannot locate the locator file or read it, the
    reference is silently skipped. False-positive avoidance is more
    important than completeness here — a missing constant will still
    surface at runtime via the JIT runtime's TBD-not-found path.
    """
    if framework not in _PYTHON_FAMILY_FRAMEWORKS:
        return []
    class_to_file = _collect_locator_class_files(plan, inventory)
    if not class_to_file:
        return []

    out: list[Violation] = []
    locator_source_cache: dict[str, str] = {}
    for rel in sorted(generated_files):
        if not rel.endswith(".py"):
            continue
        abs_path = sut_root / rel
        if not abs_path.is_file():
            continue
        try:
            src = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _CONSTANT_REF_PATTERN.finditer(src):
            cls = m.group("cls")
            const = m.group("const")
            loc_file = class_to_file.get(cls)
            if not loc_file:
                continue
            loc_abs = sut_root / loc_file
            if not loc_abs.is_file():
                continue
            if loc_file not in locator_source_cache:
                try:
                    locator_source_cache[loc_file] = loc_abs.read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeDecodeError):
                    locator_source_cache[loc_file] = ""
            loc_src = locator_source_cache[loc_file]
            if not loc_src:
                continue
            # The constant must appear as an LHS assignment somewhere in the
            # locator file — match either class-attribute (`CONST = ...`) or
            # instance-attribute (`self.CONST = ...`).
            pat = re.compile(
                rf"(?:^|\s|self\.){re.escape(const)}\s*=",
                re.MULTILINE,
            )
            if pat.search(loc_src):
                continue
            # Compute the test-file line number for the violation.
            line_no = src.count("\n", 0, m.start()) + 1
            out.append(
                Violation(
                    rule="preflight-error",
                    file=rel,
                    line=line_no,
                    snippet=(
                        f"sentinel constant {cls}.{const} referenced in test "
                        f"but not defined in {loc_file}"
                    ),
                    severity="error",
                )
            )
    return out


_NAVIGATION_PHRASES = (
    "navigates to", "navigate to", "leads to", "lead to",
    "points to", "point to", "redirects to", "redirect to",
    "lands on", "land on", "goes to",
)


def _tcs_with_navigation_expected_results(
    strategy_md: str,
) -> set[str]:
    """Parse the strategy markdown and return the set of TC-IDs whose Expected
    Result text contains a "navigates to" / "leads to" / etc. phrase.

    Format assumption (matches `test-manager` output): each TC block starts
    with a ``#### TC-...`` heading and contains an ``Expected:`` /
    ``Expected Result:`` line or section. Tolerant of variations — when the
    parse fails on a block, that TC is silently skipped (zero false
    positives is more important than completeness)."""
    if not strategy_md:
        return set()
    out: set[str] = set()
    # Split on TC headings (also tolerate `### TC-...` and `## TC-...`).
    block_pattern = re.compile(
        r"^#{2,4}\s+(?P<tc>TC-[A-Za-z0-9_\-]+)\b(?P<body>.*?)(?=^#{2,4}\s+TC-|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    for m in block_pattern.finditer(strategy_md):
        tc = m.group("tc")
        body = m.group("body") or ""
        # Extract Expected text — handles `Expected:`, `Expected Result:`,
        # and section headings.
        expected_match = re.search(
            r"(?im)expected(?:\s+result)?\s*:?\s*(.+?)(?=^(?:steps|priority|tags|test\s*type|phase|owner)\s*:|^#{2,4}\s|\Z)",
            body, re.DOTALL,
        )
        expected_text = expected_match.group(1) if expected_match else body
        low = expected_text.lower()
        if any(phrase in low for phrase in _NAVIGATION_PHRASES):
            out.add(tc)
    return out


def _check_href_when_navigates(
    sut_root: Path,
    generated_files: set[str],
    strategy_md: str,
    framework: str,
) -> list[Violation]:
    """For tests whose `@tc TC-XYZ` ref points at a strategy TC whose Expected
    Result says "navigates to" / "leads to" / etc., reject any
    ``to_have_attribute("href", ...)`` assertion. Enterprise apps commonly
    rewrite hrefs to gateway URLs that differ from the final destination, so
    href-equality silently passes against a broken UX flow.

    Only runs for Python+pytest-family stacks for now — TS strategy parsing
    is symmetric but the test-file globs differ; tracked separately.
    """
    if framework not in _PYTHON_FAMILY_FRAMEWORKS:
        return []
    nav_tcs = _tcs_with_navigation_expected_results(strategy_md)
    if not nav_tcs:
        return []

    out: list[Violation] = []
    href_pattern = re.compile(
        r"""to_have_attribute\s*\(\s*(['"])href\1\s*,"""
    )
    tc_ref_pattern = re.compile(r"@tc\s+(TC-[A-Za-z0-9_\-]+)", re.IGNORECASE)
    for rel in sorted(generated_files):
        if not rel.endswith(".py"):
            continue
        abs_path = sut_root / rel
        if not abs_path.is_file():
            continue
        try:
            src = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # File-level TC refs — if any match a navigation TC, this file is in scope.
        file_tcs = {m.group(1) for m in tc_ref_pattern.finditer(src)}
        if not (file_tcs & nav_tcs):
            continue
        for m in href_pattern.finditer(src):
            line_no = src.count("\n", 0, m.start()) + 1
            out.append(
                Violation(
                    rule="href-when-navigates",
                    file=rel,
                    line=line_no,
                    snippet=(
                        f"to_have_attribute(\"href\", ...) used in a test "
                        f"whose strategy Expected Result says navigates/leads/"
                        f"points to a URL — prefer click-then-"
                        f"expect(page).to_have_url(...) (the click captures "
                        f"actual destination after any redirect/gateway). "
                        f"Matching TC(s): {sorted(file_tcs & nav_tcs)}"
                    ),
                    severity="error",
                )
            )
    return out


def _auth_fixture_name(inventory: dict[str, Any] | None) -> str | None:
    """Extract the auth fixture name from the active module's auth_flow.

    Format on disk: ``"<file>:<func>"`` or just ``"<func>"``. Returns the
    bare function name, or None when no auth flow is declared.
    """
    if not isinstance(inventory, dict):
        return None
    active = inventory.get("active_module")
    for module in inventory.get("modules") or []:
        if not isinstance(module, dict):
            continue
        if active and module.get("name") != active:
            continue
        auth = module.get("auth_flow") or {}
        entry = auth.get("fixture_entry") or auth.get("entry_method") or ""
        if not entry:
            continue
        # `<file>:<func>` or `<file>:<Class.method>` → take the part after `:`,
        # then the last segment.
        tail = entry.split(":", 1)[-1] if ":" in entry else entry
        return tail.split(".")[-1].strip() or None
    return None


def _auth_scoped_pom_classes(inventory: dict[str, Any] | None) -> set[str]:
    """Page-object class names whose inventory `scope` is auth or navigation.

    A test importing any of these is presumed to interact with an
    authenticated route; it must consume the auth fixture (directly or via
    a plan-declared `depends_on` chain).
    """
    out: set[str] = set()
    if not isinstance(inventory, dict):
        return out
    active = inventory.get("active_module")
    for module in inventory.get("modules") or []:
        if not isinstance(module, dict):
            continue
        if active and module.get("name") != active:
            continue
        for po in module.get("existing_page_objects") or []:
            if not isinstance(po, dict):
                continue
            scope = (po.get("scope") or "").lower()
            if scope in ("auth", "navigation"):
                name = po.get("name") or po.get("class_name")
                if name:
                    out.add(name)
    return out


def _transitive_fixture_chain(
    fixture_name: str, plan_fixtures: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> set[str]:
    """Walk the plan's depends_on chain from `fixture_name` and return the
    set of all fixtures reachable (including the seed). Used to determine
    whether the auth fixture is consumed transitively by a test's direct
    fixture params."""
    seen = seen or set()
    if fixture_name in seen:
        return seen
    seen.add(fixture_name)
    fix = plan_fixtures.get(fixture_name)
    if not fix:
        return seen
    for dep in (fix.get("depends_on") or []):
        if isinstance(dep, str):
            _transitive_fixture_chain(dep, plan_fixtures, seen)
    return seen


def _check_auth_fixture_missing(
    sut_root: Path,
    generated_files: set[str],
    plan: dict[str, Any],
    inventory: dict[str, Any] | None,
    framework: str,
) -> list[Violation]:
    """Advisory: flag tests that touch an auth/navigation-scoped POM but
    don't consume the auth fixture (directly or via the plan's depends_on
    chain). Ships severity=warning; promotion to error after FP baselining."""
    if framework not in _PYTHON_FAMILY_FRAMEWORKS:
        return []
    auth_fix = _auth_fixture_name(inventory)
    if not auth_fix:
        return []
    auth_scoped_poms = _auth_scoped_pom_classes(inventory)
    if not auth_scoped_poms:
        return []

    plan_fixtures = _build_fixture_index(plan)
    out: list[Violation] = []
    import ast as _ast

    for rel in sorted(generated_files):
        if not rel.endswith(".py"):
            continue
        abs_path = sut_root / rel
        if not abs_path.is_file():
            continue
        try:
            src = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = _ast.parse(src)
        except SyntaxError:
            continue

        # Build the set of POM class names imported in this file.
        imported_names: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
        relevant_poms = imported_names & auth_scoped_poms
        if not relevant_poms:
            continue

        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            params = {arg.arg for arg in node.args.args}
            # Walk every direct param through the plan's depends_on chain;
            # if any chain reaches the auth fixture, the test is wired.
            reachable: set[str] = set()
            for p in params:
                reachable |= _transitive_fixture_chain(p, plan_fixtures)
            if auth_fix in reachable or auth_fix in params:
                continue
            out.append(
                Violation(
                    rule="auth-fixture-missing",
                    file=rel,
                    line=node.lineno,
                    snippet=(
                        f"def {node.name}(...) imports auth-scoped POM(s) "
                        f"{sorted(relevant_poms)} but does not consume the "
                        f"auth fixture {auth_fix!r} (direct or via plan's "
                        f"depends_on chain)"
                    ),
                    severity="warning",
                )
            )
    return out


def _check_missing_reuse_imports(
    sut_root: Path,
    plan: dict[str, Any],
    framework: str,
) -> list[Violation]:
    """Advisory: every plan entry marked ``source: "reuse"`` with a ``from``
    field must appear as a ``from ... import ...`` line in the matching
    generated test file. Silent re-implementations are the dev-pool's worst
    enemy — they create shadow code paths that look in-scope but bypass the
    SUT's actual primitives.

    Ships severity=warning to baseline FP rate; promotion to error after one
    release of telemetry.
    """
    if framework not in _PYTHON_FAMILY_FRAMEWORKS:
        return []
    import ast as _ast

    out: list[Violation] = []
    for tc in plan.get("test_cases") or []:
        if not isinstance(tc, dict):
            continue
        target = tc.get("test_file_target")
        if not target or not isinstance(target, str):
            continue
        if not target.endswith(".py"):
            continue
        abs_path = sut_root / target
        if not abs_path.is_file():
            continue
        try:
            src = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = _ast.parse(src)
        except SyntaxError:
            continue

        # Collect imported names. For aliased imports we record BOTH the
        # alias (the local binding) AND the original name so the plan's
        # reuse claim is honored regardless of stylistic alias choice.
        imported: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
                    if alias.asname:
                        imported.add(alias.asname)
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
                    if alias.asname:
                        imported.add(alias.asname)

        # Walk each reuse bucket and verify expected symbol is imported.
        for bucket in ("page_objects", "helpers", "fixtures", "locators"):
            for entry in tc.get(bucket) or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("source") != "reuse":
                    continue
                from_ref = entry.get("from") or ""
                if not from_ref or ":" not in from_ref:
                    # Cannot extract a symbol → silent skip (don't false-flag
                    # entries that the architect referenced by file path only).
                    continue
                _, symbol = from_ref.split(":", 1)
                # `<file>:<Class.method>` → take the leftmost dotted segment
                # as the importable symbol.
                symbol_root = symbol.strip().split(".")[0]
                if not symbol_root:
                    continue
                # Fixtures aren't imported in test files — pytest discovers
                # them by parameter name. Skip the bucket entirely for
                # fixtures; the auth-fixture-missing rule already covers
                # the most important missing-fixture defect class.
                if bucket == "fixtures":
                    continue
                if symbol_root in imported:
                    continue
                out.append(
                    Violation(
                        rule="missing-reuse-import",
                        file=target,
                        line=1,
                        snippet=(
                            f"plan marks {bucket}.{entry.get('name', symbol_root)!r} "
                            f"as reuse from {from_ref!r}, but the generated "
                            f"test file does not import {symbol_root!r}"
                        ),
                        severity="warning",
                    )
                )
    return out


def run_preflight(
    sut_root: Path,
    *,
    framework: str,
    generated_files: set[str],
    plan: dict[str, Any],
    inventory: dict[str, Any] | None = None,
    strategy_md: str = "",
) -> list[Violation]:
    """Run all preflight sub-checks. Returns the combined Violation list
    (empty when everything passes). Caller appends the result to
    ``IndexResult.violations`` so the existing reject machinery handles it.

    ``strategy_md`` is the raw test-strategy markdown from Step 4 — required
    for the ``href-when-navigates`` check. Pass an empty string to skip it.
    """
    out: list[Violation] = []
    out.extend(_ast_parse_python_tests(sut_root, generated_files, framework))
    out.extend(_check_fixture_graph(plan, inventory))
    out.extend(_check_sentinel_constants(
        sut_root, generated_files, plan, inventory, framework,
    ))
    out.extend(_check_href_when_navigates(
        sut_root, generated_files, strategy_md, framework,
    ))
    out.extend(_check_auth_fixture_missing(
        sut_root, generated_files, plan, inventory, framework,
    ))
    out.extend(_check_missing_reuse_imports(sut_root, plan, framework))
    return out
