"""SUT inventory: language-agnostic, monorepo-aware introspection.

Produces a structured snapshot of the SUT(s) that Steps 7/8/9 consume so they
can integrate with — rather than ignore — the SUT's existing test layout,
page objects, helpers, fixtures, and authentication flow.

Architecture: three-tier detection. Each module under the SUT root is walked
through:

  Tier 1 — Deterministic Python AST     (this module, native)
  Tier 2 — Regex heuristics for TS/JS   (this module, native)
  Tier 3 — LLM-augmentation             (parsed from researcher agent output
                                         in src/qtea/steps/s06_research.py)

Tier 3 is what makes the inventory language-agnostic: anything the
deterministic tiers can't cover (Java, Robot, Ruby, Go, Kotlin, C#) is filled
in from the researcher agent's structured `## SUT Inventory` YAML block.

Monorepos: a separate monorepo detector enumerates module paths (pnpm
workspaces, npm/yarn workspaces, lerna, nx, Maven `<modules>`, Gradle
`include`, Cargo workspaces, go.work, Poetry/uv/hatch/pdm workspaces). Each
discovered module is processed independently — language and package manager
can differ per module. Single-module SUTs collapse to a one-element
`modules[]` list with `path == "."`.

The dataclasses in this file are the single source of truth for the
`sut_inventory` schema block (`schemas/research.schema.json`).
"""

from __future__ import annotations

import ast
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qtea._ast_utils import (
    iter_python_files,
    literal_str,
    parse_file,
    relative_posix,
)
from qtea.logging_setup import get_logger
from qtea.stack_profile import StackProfile, detect_stack_profile

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses (one-to-one with schemas/research.schema.json sut_inventory block)
# ---------------------------------------------------------------------------


@dataclass
class TestDirSubdir:
    name: str
    kind: str  # "type" | "page" | "support" | "other"
    path: str


@dataclass
class TestDirectoryLayout:
    base_dir: str | None = None
    convention: str = "unknown"  # "by_type" | "by_page" | "flat" | "unknown"
    subdirs: list[TestDirSubdir] = field(default_factory=list)
    default_target: str | None = None


@dataclass
class SrcDirectoryLayout:
    """Where the SUT puts production (non-test) code: page objects, locators,
    helpers. Step 7's codegen places generated page objects + locators here
    (NOT under tests/) so the SUT's existing src/tests split is preserved.

    Detected by:
      1. pyproject.toml `packages = [{ include = "...", from = "src" }]`
         → `package_root`.
      2. Common parent of `existing_page_objects[].file` paths matching
         `*/object/*` or `*/pages/*` → `pages_object_dir`.
      3. Common parent of `existing_page_objects[].file` paths matching
         `*/locators/*` → `pages_locators_dir`.
      4. Common parent of `existing_helpers[].file` → `helpers_dir`.
      5. Greenfield fallback when none of the above resolve: build from
         `package_root` (or `src/<module_name>`) with conventional subdirs.
    """

    package_root: str | None = None       # e.g. "src/askbosch_automation_frontend_sync"
    pages_object_dir: str | None = None   # e.g. "<package_root>/pages/object"
    pages_locators_dir: str | None = None # e.g. "<package_root>/pages/locators"
    helpers_dir: str | None = None        # e.g. "<package_root>/helpers"
    convention_source: str = "detected"   # "detected" | "fallback" | "llm_only"


@dataclass
class PageObject:
    name: str
    file: str
    class_name: str
    methods: list[str] = field(default_factory=list)
    scope: str = "generic"  # "auth" | "navigation" | "form" | "generic"
    import_path: str | None = None
    # True when the POM class body defines an inline locator container
    # (e.g. ``elements: Record<string, string> = {...}``, ``selectors = {...}``,
    # or ``readonly submitBtn = this.page.getByRole(...)``). Feeds Step 8's
    # `_write_tbd_locators` placement decision so new TBD constants land in
    # the same convention the SUT already uses.
    has_inline_locators: bool = False


@dataclass
class Helper:
    name: str
    file: str
    signature: str = ""
    purpose: str = ""


@dataclass
class LocatorConstant:
    """One selector constant from a SUT locator class.

    `selector` is the raw string value as it appears in source — e.g.
    `[data-testid='LanguageSelect-Select']`. `line` is the 1-based source
    line of the assignment, for traceability in agent reasoning ("the
    existing `LANGUAGE_DROP_DOWN` at chat_page_locators.py:73").
    """

    name: str
    selector: str
    line: int = 0


@dataclass
class LocatorClass:
    """A SUT class or object that holds locator constants.

    The codegen step uses this to prevent the agent from inventing
    byte-identical duplicates of locators that already exist in the SUT.
    `constants` is bounded by `_LOCATOR_CONSTANT_CAP` per class — agents
    only need enough samples to dedup, not the entire file.

    ``location_pattern`` records the SUT convention so Step 8's
    ``_write_tbd_locators`` can add new TBD constants in the same style
    the SUT already uses (rather than imposing a Python-Selenium
    ``class FooLocators`` file on every stack).
    """

    name: str
    file: str
    class_name: str
    constants: list[LocatorConstant] = field(default_factory=list)
    import_path: str | None = None
    truncated_count: int = 0  # >0 when source had more than the cap allows
    # One of: "separate_class" (default, backwards-compatible) |
    #         "export_const_object" | "inline_object_property" |
    #         "readonly_locator_props" | "module_const_bag"
    location_pattern: str = "separate_class"
    # For inline_object_property / readonly_locator_props: the POM class
    # these locators belong to. None for separate_class / module-level.
    owning_pom: str | None = None
    # For inline_object_property: the property name on the POM
    # (e.g. "elements", "selectors", "locators"). None otherwise.
    container_name: str | None = None


@dataclass
class Fixture:
    name: str
    file: str
    scope: str = "function"  # function | class | module | session
    yields: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class AuthFlow:
    type: str = "unknown"  # sso | oauth | basic | none | unknown
    entry_method: str | None = None  # "<file>:<Class>.<method>" or "<file>:<func>"
    credentials_env_vars: list[str] = field(default_factory=list)
    fixture_entry: str | None = None  # "<file>:<func>"
    # The method that navigates to the app base URL (e.g. `page.goto('/')`) —
    # the mandatory FIRST step of any UI test, before login. Same
    # "<file>:<Class>.<method>" shape as `entry_method`. None when no such
    # method is found in the SUT's page objects.
    open_method: str | None = None


@dataclass
class LifecycleHook:
    """A SUT test setup/teardown hook, classified by canonical trigger event.

    Framework keywords are normalized to one of four canonical events so
    Step 7 (planning) and Step 8 (codegen) stay framework-agnostic:

    | event        | JS/TS              | pytest                          | unittest       | JUnit5       |
    | ------------ | ------------------ | ------------------------------- | -------------- | ------------ |
    | before_all   | beforeAll/before   | @fixture(scope="module")        | setUpClass     | @BeforeAll   |
    | after_all    | afterAll/after     | @fixture(scope="module")+yield  | tearDownClass  | @AfterAll    |
    | before_each  | beforeEach         | @fixture(scope="function")/auto | setUp          | @BeforeEach  |
    | after_each   | afterEach          | @fixture+yield                  | tearDown       | @AfterEach   |

    `calls` is the ordered list of call expressions in the hook body (e.g.
    ``["basePage.openBaseURL", "basePage.logIn", "basePage.goToEntityModule"]``)
    so Step 7 can replay the SUT's canonical sequence and Step 8 can
    regenerate it. `framework_construct` records the raw keyword/decorator for
    traceability. A single yielding pytest fixture can produce TWO hooks (a
    before_* from its pre-yield body and an after_* from its post-yield body).
    """

    event: str  # before_all | after_all | before_each | after_each
    file: str
    framework_construct: str = ""  # e.g. "beforeEach", "@pytest.fixture(scope=module)", "setUp", "@BeforeEach"
    scope: str | None = None
    calls: list[str] = field(default_factory=list)


@dataclass
class NavigationPrecondition:
    """A POM method with no in-code guard that is only valid after another
    call has already run — a pure calling convention, visible only across
    real test-body call sites, not in the method's own source.

    Unlike `LifecycleHook`, there is no deterministic scanner for this field:
    hook bodies are a narrow, structurally predictable region to scan, but
    detecting "every real call site of X is preceded by Y" requires judgment
    across arbitrarily-shaped test bodies. Populated by the LLM researcher
    only, with the same anti-fabrication discipline as `auth_flow` —
    only recorded when call sites agree and at least one is evidenced.

    `method` / `requires_call` are plain `ClassName.methodName` strings (same
    convention as `LifecycleHook.calls` entries), so Step 7 (planning) can
    match them against its own `pom`/`method` choreography fields without any
    language-specific parsing.
    """

    method: str  # e.g. "DirectoryPage.selectFilteredEntity"
    requires_call: str  # e.g. "BasePage.selectLoginOptionByText"
    requires_args_hint: str | None = None  # e.g. "NAV_OPTIONS.DIRECTORY"
    evidence: str = ""  # e.g. "tests/EntityFormSmoke.spec.ts:104"


@dataclass
class PatternExemplar:
    """A verbatim source slice of one of the SUT's own reusable automation
    units, captured so Step 8's exemplar lane can generate new code by
    *imitation* rather than by imposing Page Object Model on every SUT.

    Only populated for non-POM patterns (e.g. Screenplay Task/Question/
    Interaction). For POM / inline / none the list stays empty and the mature
    POM codegen path is used unchanged.

    ``excerpt`` is the raw class (or Target-module) body sliced via the same
    ``lineno``/``end_lineno`` approach ``_extract_locator_constants`` uses,
    hard-capped at ``_EXEMPLAR_CHAR_CAP`` so the s07/s08 prompt stays bounded.
    ``dir`` (the excerpt file's parent dir) is what Step 7's ``_approved_dirs``
    adds to the create-target allowlist so new units land beside the SUT's own.
    """

    file: str
    category: str  # task | question | interaction | ability | target | page_object | other
    class_name: str | None
    excerpt: str
    line_start: int = 0
    line_end: int = 0
    dir: str | None = None


@dataclass
class ModuleInventory:
    name: str
    path: str  # relative to SUT root; "." for single-module
    language: str = "unknown"
    package_manager: str | None = None
    test_directory_layout: TestDirectoryLayout = field(default_factory=TestDirectoryLayout)
    src_directory_layout: SrcDirectoryLayout = field(default_factory=SrcDirectoryLayout)
    existing_page_objects: list[PageObject] = field(default_factory=list)
    existing_helpers: list[Helper] = field(default_factory=list)
    existing_fixtures: list[Fixture] = field(default_factory=list)
    existing_locators: list[LocatorClass] = field(default_factory=list)
    # Test setup/teardown hooks discovered in the SUT's own tests, classified
    # by canonical trigger event. Step 7 reuses these; Step 8 regenerates them.
    lifecycle_hooks: list[LifecycleHook] = field(default_factory=list)
    # POM methods with an undeclared navigation precondition, mined from real
    # call sites across the SUT's own tests. LLM-only (no deterministic
    # producer) — Step 7's phase gate cross-checks planned steps against this.
    navigation_preconditions: list[NavigationPrecondition] = field(default_factory=list)
    auth_flow: AuthFlow = field(default_factory=AuthFlow)
    custom_test_id_attribute: str | None = None
    # Detected test-automation design pattern. Drives Step 8 lane selection:
    # "pom" | "inline" | "none" | "unknown" → mature POM codegen lane;
    # anything else (e.g. "screenplay") → exemplar-imitation lane.
    architecture_pattern: str = "unknown"
    # Verbatim reusable-unit snippets for the exemplar lane (non-POM only).
    pattern_exemplars: list[PatternExemplar] = field(default_factory=list)
    source: str = "deterministic"  # "deterministic" | "llm_augmented" | "llm_only"

    def as_dict(self) -> dict[str, Any]:
        return _asdict(self)


@dataclass
class SutInventory:
    is_monorepo: bool = False
    monorepo_signal: str | None = None
    modules: list[ModuleInventory] = field(default_factory=list)
    active_module: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return _asdict(self)

    def module_by_name(self, name: str) -> ModuleInventory | None:
        for m in self.modules:
            if m.name == name:
                return m
        return None

    def active(self) -> ModuleInventory | None:
        if self.active_module is None:
            return None
        return self.module_by_name(self.active_module)


def _asdict(obj: Any) -> dict[str, Any]:
    """`dataclasses.asdict` with stable key order and no-None pruning at top level.

    Post-processes the result to strip purely-informational fields whose
    bytes are dead weight on the wire to LLM agents:

    - ``LocatorConstant.line``: source line of the constant assignment.
      Kept on the in-memory dataclass for any future debugging consumer,
      but excluded from the serialized JSON. The codegen agent uses
      ``constants`` only for byte-match dedup (name + selector); the
      ``line`` field is never consulted programmatically anywhere in the
      codebase (verified via grep) and the deserializer at the LLM-merge
      site (`merge_llm_inventory`) already defaults missing `line` to 0.
      On the 20260611-075728-0aa560 SUT this trims ~7-10% off the
      `existing_locators` block (~10% of the whole inventory).
    """
    d = asdict(obj)
    _strip_locator_constant_lines(d)
    return d


def _strip_locator_constant_lines(d: Any) -> None:
    """In-place: drop ``line`` from every ``LocatorConstant`` in *d*.

    Walks the inventory shape (single module OR full ``SutInventory`` with
    a ``modules`` list). Mutating in-place is cheap and avoids a full
    deep-copy of the (already large) dict.
    """
    if isinstance(d, dict):
        # Locator constants live two levels down: existing_locators[].constants[]
        for lc in d.get("existing_locators") or ():
            if not isinstance(lc, dict):
                continue
            for c in lc.get("constants") or ():
                if isinstance(c, dict):
                    c.pop("line", None)
        # Top-level SutInventory wraps modules; recurse into each.
        for mod in d.get("modules") or ():
            _strip_locator_constant_lines(mod)


# ---------------------------------------------------------------------------
# Monorepo detection
# ---------------------------------------------------------------------------


def detect_monorepo(sut_path: Path) -> tuple[bool, str | None, list[str]]:
    """Return (is_monorepo, signal_filename, module_paths) for the SUT root.

    `module_paths` are POSIX-style relative paths from `sut_path`. When the
    SUT is not a monorepo, returns `(False, None, ["."])` so callers can treat
    it uniformly as a single-module SUT.
    """
    if not sut_path.exists() or not sut_path.is_dir():
        return False, None, ["."]

    # Order matters: pnpm/yarn/npm-workspaces > lerna > nx >
    # pyproject workspaces > maven > gradle > cargo > go.work.
    for fn in (
        _detect_pnpm_workspace,
        _detect_npm_yarn_workspaces,
        _detect_lerna,
        _detect_nx,
        _detect_pyproject_workspaces,
        _detect_maven_modules,
        _detect_gradle_include,
        _detect_cargo_workspace,
        _detect_go_work,
    ):
        signal, modules = fn(sut_path)
        if signal:
            # Filter to existing dirs, dedupe, sort.
            existing = sorted({p for p in modules if (sut_path / p).is_dir()})
            if existing:
                return True, signal, existing

    return False, None, ["."]


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _expand_workspace_glob(sut_path: Path, pattern: str) -> list[str]:
    """Expand a workspace glob (e.g. `packages/*`) to a list of POSIX paths."""
    pattern = pattern.strip().strip('"').strip("'")
    if not pattern:
        return []
    try:
        return [
            relative_posix(p, sut_path)
            for p in sut_path.glob(pattern)
            if p.is_dir()
        ]
    except (OSError, ValueError):
        return []


def _detect_pnpm_workspace(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "pnpm-workspace.yaml"
    if not p.exists():
        return None, []
    text = _read_text(p)
    # Cheap YAML-list parse: we look for `- <pattern>` lines after `packages:`.
    out: list[str] = []
    in_packages = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("packages:"):
            in_packages = True
            continue
        if in_packages:
            if stripped.startswith("- "):
                out.extend(_expand_workspace_glob(sut, stripped[2:].strip()))
            elif stripped and not stripped.startswith("#") and ":" in stripped:
                # Next top-level key encountered.
                break
    return "pnpm-workspace.yaml", out


def _detect_npm_yarn_workspaces(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "package.json"
    if not p.exists():
        return None, []
    try:
        data = json.loads(_read_text(p))
    except json.JSONDecodeError:
        return None, []
    ws = data.get("workspaces")
    if ws is None:
        return None, []
    # `workspaces` may be a list[str] OR an object with `packages: [...]`.
    patterns: list[str] = []
    if isinstance(ws, list):
        patterns = [str(x) for x in ws]
    elif isinstance(ws, dict):
        patterns = [str(x) for x in ws.get("packages", [])]
    out: list[str] = []
    for pat in patterns:
        out.extend(_expand_workspace_glob(sut, pat))
    if out:
        return "package.json:workspaces", out
    return None, []


def _detect_lerna(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "lerna.json"
    if not p.exists():
        return None, []
    try:
        data = json.loads(_read_text(p))
    except json.JSONDecodeError:
        return None, []
    pkgs = data.get("packages") or []
    out: list[str] = []
    for pat in pkgs:
        out.extend(_expand_workspace_glob(sut, str(pat)))
    return ("lerna.json", out) if out else (None, [])


def _detect_nx(sut: Path) -> tuple[str | None, list[str]]:
    if not (sut / "nx.json").exists():
        return None, []
    # Enumerate project.json files (Nx convention).
    out: list[str] = []
    for proj in sut.glob("**/project.json"):
        if any(p in {".git", "node_modules", "dist"} for p in proj.parts):
            continue
        out.append(relative_posix(proj.parent, sut))
    return ("nx.json", sorted(set(out))) if out else ("nx.json", [])


def _detect_pyproject_workspaces(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "pyproject.toml"
    if not p.exists():
        return None, []
    text = _read_text(p)
    # Cheap detection: look for `[tool.uv.workspace]` / `[tool.hatch.workspaces]` /
    # `[tool.poetry.workspaces]` followed by a `members` list.
    sections = (
        "[tool.uv.workspace]",
        "[tool.hatch.workspaces]",
        "[tool.poetry.workspaces]",
        "[tool.rye.workspace]",
    )
    found_section: str | None = None
    for sec in sections:
        if sec in text:
            found_section = sec
            break
    if not found_section:
        return None, []
    # Pull a `members = [...]` list out of the section.
    m = re.search(r"members\s*=\s*\[([^\]]+)\]", text, re.S)
    if not m:
        return found_section, []
    items = re.findall(r'"([^"]+)"|\'([^\']+)\'', m.group(1))
    patterns = [a or b for a, b in items]
    out: list[str] = []
    for pat in patterns:
        out.extend(_expand_workspace_glob(sut, pat))
    return found_section, out


def _detect_maven_modules(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "pom.xml"
    if not p.exists():
        return None, []
    try:
        tree = ET.parse(p)  # noqa: S314 — pom.xml is trusted local file
    except ET.ParseError:
        return None, []
    root = tree.getroot()
    # Strip default namespace.
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"
    modules_el = root.find(f"{ns}modules")
    if modules_el is None:
        return None, []
    out: list[str] = []
    for module in modules_el.findall(f"{ns}module"):
        text = (module.text or "").strip()
        if text:
            out.append(text)
    return ("pom.xml:modules", out) if out else (None, [])


def _detect_gradle_include(sut: Path) -> tuple[str | None, list[str]]:
    for fn in ("settings.gradle.kts", "settings.gradle"):
        p = sut / fn
        if not p.exists():
            continue
        text = _read_text(p)
        # Match `include("...")` / `include ":foo"` / `include('foo', 'bar')`.
        names = re.findall(r"""include\s*[(\s]\s*['"]([^'"]+)['"]""", text)
        if names:
            # Gradle ":foo:bar" → "foo/bar".
            out = [n.lstrip(":").replace(":", "/") for n in names]
            return (fn, out)
    return None, []


def _detect_cargo_workspace(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "Cargo.toml"
    if not p.exists():
        return None, []
    text = _read_text(p)
    if "[workspace]" not in text:
        return None, []
    m = re.search(r"members\s*=\s*\[([^\]]+)\]", text, re.S)
    if not m:
        return None, []
    items = re.findall(r'"([^"]+)"|\'([^\']+)\'', m.group(1))
    patterns = [a or b for a, b in items]
    out: list[str] = []
    for pat in patterns:
        out.extend(_expand_workspace_glob(sut, pat))
    return ("Cargo.toml:workspace", out) if out else (None, [])


def _detect_go_work(sut: Path) -> tuple[str | None, list[str]]:
    p = sut / "go.work"
    if not p.exists():
        return None, []
    text = _read_text(p)
    # Match `use ./mod` / `use ( ./mod1 ./mod2 )`.
    out: list[str] = []
    for m in re.finditer(r"use\s*\(([^)]*)\)", text, re.S):
        for line in m.group(1).split():
            stripped = line.strip().strip(",")
            if stripped:
                out.append(stripped.lstrip("./"))
    for m in re.finditer(r"^\s*use\s+([^\s(]+)\s*$", text, re.M):
        out.append(m.group(1).lstrip("./"))
    return ("go.work", out) if out else (None, [])


# ---------------------------------------------------------------------------
# Test directory layout detection
# ---------------------------------------------------------------------------


_TEST_DIR_CANDIDATES = (
    "tests", "test", "e2e", "cypress/e2e", "cypress/integration",
    "__tests__", "spec", "src/test/java", "src/test",
)

_SUBDIR_KIND_BY_NAME = {
    "smoke": "type", "regression": "type", "integration": "type",
    "unit": "type", "api": "type", "e2e": "type", "system": "type",
    "fixtures": "support", "helpers": "support", "utils": "support",
    "lib": "support", "support": "support", "data": "support",
    "conftest": "support", "common": "support",
}


def _classify_subdir(name: str) -> str:
    low = name.lower()
    if low in _SUBDIR_KIND_BY_NAME:
        return _SUBDIR_KIND_BY_NAME[low]
    if low.endswith(("_page", "_pages", "page", "_test", "_tests")):
        return "page"
    return "other"


def detect_test_directory_layout(module_root: Path) -> TestDirectoryLayout:
    """Find the test base directory + classify its immediate subdirs."""
    base_dir: Path | None = None
    for candidate in _TEST_DIR_CANDIDATES:
        p = module_root / candidate
        if p.is_dir():
            base_dir = p
            break
    if base_dir is None:
        return TestDirectoryLayout()

    rel_base = relative_posix(base_dir, module_root)
    subdirs: list[TestDirSubdir] = []
    type_subdirs: list[str] = []
    page_subdirs: list[str] = []
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        kind = _classify_subdir(child.name)
        subdirs.append(TestDirSubdir(
            name=child.name,
            kind=kind,
            path=relative_posix(child, module_root),
        ))
        if kind == "type":
            type_subdirs.append(child.name)
        elif kind == "page":
            page_subdirs.append(child.name)

    if type_subdirs:
        convention = "by_type"
        # Prefer regression > integration > smoke > first type subdir.
        for pref in ("regression", "integration", "smoke", "e2e"):
            if pref in type_subdirs:
                default = f"{rel_base}/{pref}"
                break
        else:
            default = f"{rel_base}/{type_subdirs[0]}"
    elif page_subdirs:
        convention = "by_page"
        default = f"{rel_base}/{page_subdirs[0]}"
    elif not subdirs:
        # Flat layout with files directly under base_dir.
        convention = "flat"
        default = rel_base
    else:
        convention = "unknown"
        default = rel_base

    return TestDirectoryLayout(
        base_dir=rel_base,
        convention=convention,
        subdirs=subdirs,
        default_target=default,
    )


# ---------------------------------------------------------------------------
# Tier 1: Python AST detection of page objects, helpers, fixtures, auth flow
# ---------------------------------------------------------------------------


_PAGE_DIR_NAMES = (
    "pages", "pageobjects", "page-objects", "page_objects",
    "po", "poms", "pom",
    "screens", "views", "flows",
)
_HELPER_ROOTS_PY = ("tests/helpers", "tests/utils", "tests/support",
                    "lib/utils", "helpers", "utils", "src/utils")
_AUTH_FILE_RE = re.compile(r"(sign[_-]?in|sign[_-]?on|sso|login|auth|authenticat)", re.I)
_PAGE_CLASS_HINT_RE = re.compile(r"(Page|PageObject|PO)$")


def _scope_for_name(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("signin", "sign_in", "login", "auth", "sso")):
        return "auth"
    if any(k in low for k in ("nav", "menu", "home", "sidebar", "topbar", "header")):
        return "navigation"
    if any(k in low for k in ("form", "input", "field", "modal", "dialog")):
        return "form"
    return "generic"


def _public_methods(class_def: ast.ClassDef) -> list[str]:
    out: list[str] = []
    for stmt in class_def.body:
        if (
            isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not stmt.name.startswith("_")
        ):
            out.append(stmt.name)
    return out


def _python_module_import_path(file_rel: str) -> str:
    """Convert e.g. `src/x/y/page.py` → `src.x.y.page` (best-effort)."""
    no_ext = file_rel[:-3] if file_rel.endswith(".py") else file_rel
    return no_ext.replace("/", ".")


def _find_page_dirs(module_root: Path) -> list[Path]:
    """Recursively find directories named `pages`/`pageobjects`/`po`."""
    out: list[Path] = []
    if not module_root.exists():
        return out
    for p in module_root.glob("**/*"):
        if not p.is_dir():
            continue
        if _skip_path_part(p.parts):
            continue
        if p.name.lower() in _PAGE_DIR_NAMES:
            out.append(p)
    return out


def scan_python_page_objects(module_root: Path) -> list[PageObject]:
    """Walk likely page-object roots and AST-extract class names + methods."""
    out: list[PageObject] = []
    seen_files: set[Path] = set()
    for root in _find_page_dirs(module_root):
        for src in iter_python_files(root):
            if src in seen_files:
                continue
            seen_files.add(src)
            tree = parse_file(src)
            if tree is None:
                continue
            rel = relative_posix(src, module_root)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not _PAGE_CLASS_HINT_RE.search(node.name):
                    continue
                methods = _public_methods(node)
                if not methods:
                    continue
                out.append(PageObject(
                    name=node.name,
                    file=rel,
                    class_name=node.name,
                    methods=methods,
                    scope=_scope_for_name(node.name),
                    import_path=_python_module_import_path(rel),
                ))
    return out


def scan_python_helpers(module_root: Path) -> list[Helper]:
    out: list[Helper] = []
    for root_rel in _HELPER_ROOTS_PY:
        root = module_root / root_rel
        if not root.is_dir():
            continue
        for src in iter_python_files(root):
            tree = parse_file(src)
            if tree is None:
                continue
            rel = relative_posix(src, module_root)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if node.name.startswith("_"):
                    continue
                # Cheap signature reconstruction.
                args = [a.arg for a in node.args.args]
                signature = f"{node.name}({', '.join(args)})"
                out.append(Helper(
                    name=node.name,
                    file=rel,
                    signature=signature,
                    purpose=ast.get_docstring(node) or "",
                ))
    return out


# Per-class cap on the number of constants returned by the locator scanners.
# The codegen agent only needs enough samples to recognise byte-identical
# duplicates; the cap keeps the staged inventory and the s07 prompt bounded
# on SUTs with several-hundred-constant locator classes. When the cap fires,
# constants are sorted alphabetically before truncation so the kept slice is
# stable across runs.
_LOCATOR_CONSTANT_CAP = 80
_LOCATOR_CLASS_HINT = "locator"  # case-insensitive substring match


def _looks_like_selector(value: str) -> bool:
    """True when a string literal looks like a UI selector (CSS / role / id).

    The locator-class scanners use this to filter out random string constants
    (env-var names, sentinel values, URLs, format strings, blank locators
    such as `FAQ = ""`) that happen to live alongside real selectors.
    """
    s = value.strip()
    if not s:
        return False
    if s.startswith(("http://", "https://", "ws://", "wss://")):
        return False
    # Selector-ish prefixes / shapes. Keep the bar low so SUT idioms we
    # haven't seen still surface (Playwright `text=`, Robot `css=`, etc.).
    if s.startswith(("[", "#", ".", "//", "*", ":", "@", "(")):
        return True
    if s.startswith(("text=", "role=", "css=", "id=", "xpath=", "name=", "label=")):
        return True
    # `getByRole('button', { name: 'X' })`-style fragments aren't constants.
    # Bare attribute selectors like `value="en"` would have an `=` sign.
    if "=" in s and ('"' in s or "'" in s):
        return True
    # Single tokens — only accept if they look like a CSS class/id token
    # (alphanumeric + hyphens / underscores), not a freeform sentence.
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", s))


def _truncate_constants(consts: list[LocatorConstant]) -> tuple[list[LocatorConstant], int]:
    """Sort + truncate to `_LOCATOR_CONSTANT_CAP`. Return (kept, dropped_count)."""
    if len(consts) <= _LOCATOR_CONSTANT_CAP:
        return consts, 0
    consts_sorted = sorted(consts, key=lambda c: c.name)
    return consts_sorted[:_LOCATOR_CONSTANT_CAP], len(consts) - _LOCATOR_CONSTANT_CAP


def _is_locator_candidate_file(src: Path) -> bool:
    """Heuristic: `<anything>locator<anything>.py` OR a file under a dir named
    `locators`. The cheap name test runs first; the dir test handles the
    common AskBosch convention (`pages/locators/chat_page_locators.py`)
    where the filename alone is sufficient but the dir test catches outliers
    (`pages/locators/buttons.py` etc.)."""
    name_low = src.name.lower()
    if _LOCATOR_CLASS_HINT in name_low:
        return True
    return any(p.lower() == "locators" for p in src.parts)


def scan_python_locators(module_root: Path) -> list[LocatorClass]:
    """AST-extract locator constant classes from the SUT.

    Walks every `.py` file matching `_is_locator_candidate_file`, finds
    every `ClassDef` whose name contains "Locator", and extracts both:

      - Class-level `Assign` nodes (`NAME = "selector"`).
      - `self.NAME = "selector"` assignments inside `__init__`, the
        AskBosch convention seen in chat_page_locators.py:14-79.

    Selectors are filtered through `_looks_like_selector` to skip non-DOM
    string constants. Constants per class are capped at `_LOCATOR_CONSTANT_CAP`.
    """
    out: list[LocatorClass] = []
    seen_files: set[Path] = set()
    # Cheap content prefilter: skip files that don't even mention "Locator"
    # to avoid AST-parsing every helper / settings / config under tests/.
    for src in iter_python_files(module_root, contains_hint=b"Locator"):
        if src in seen_files:
            continue
        seen_files.add(src)
        if not _is_locator_candidate_file(src):
            continue
        tree = parse_file(src)
        if tree is None:
            continue
        rel = relative_posix(src, module_root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if "Locator" not in node.name:
                continue
            constants = list(_extract_locator_constants(node))
            if not constants:
                continue
            kept, dropped = _truncate_constants(constants)
            out.append(LocatorClass(
                name=node.name,
                file=rel,
                class_name=node.name,
                constants=kept,
                import_path=_python_module_import_path(rel),
                truncated_count=dropped,
            ))
    return out


def _extract_locator_constants(class_def: ast.ClassDef) -> Iterator[LocatorConstant]:
    """Yield string-constant locators from a class body.

    Picks up two patterns:
      1. `NAME = "..."` (or `NAME: ClassVar[str] = "..."`) — class-level.
      2. `self.NAME = "..."` inside any method (typically `__init__`) —
         the AskBosch / setUp-style convention.

    Class-level constants are emitted first to preserve a deterministic
    order in the rare case where the same NAME appears in both forms
    (class-level wins, the self-assignment is skipped).
    """
    seen: set[str] = set()

    # 1. Class-level assignments (typed and untyped).
    for stmt in class_def.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if not isinstance(tgt, ast.Name) or not tgt.id.isupper():
                    continue
                lit = literal_str(stmt.value)
                if lit is None or not _looks_like_selector(lit):
                    continue
                if tgt.id in seen:
                    continue
                seen.add(tgt.id)
                yield LocatorConstant(name=tgt.id, selector=lit, line=stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if not stmt.target.id.isupper() or stmt.value is None:
                continue
            lit = literal_str(stmt.value)
            if lit is None or not _looks_like_selector(lit):
                continue
            if stmt.target.id in seen:
                continue
            seen.add(stmt.target.id)
            yield LocatorConstant(name=stmt.target.id, selector=lit, line=stmt.lineno)

    # 2. `self.NAME = "..."` inside any method.
    for stmt in class_def.body:
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(stmt):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "self"
                    and tgt.attr.isupper()
                ):
                    lit = literal_str(sub.value)
                    if lit is None or not _looks_like_selector(lit):
                        continue
                    if tgt.attr in seen:
                        continue
                    seen.add(tgt.attr)
                    yield LocatorConstant(
                        name=tgt.attr, selector=lit, line=sub.lineno,
                    )


# TS/JS locator patterns — pattern-agnostic recognition.
#
# The scanner runs SEVERAL detectors per file and unions the results, so
# a SUT is not forced into one convention. Real projects vary widely:
#   1. `class EntityLocators { EMAIL = "..." }`         → separate_class
#   2. `export const EntityLocators = { EMAIL: "..." }` → export_const_object
#   3. `class EntityPage { elements = { btnX: "..." } }` → inline_object_property
#   4. `class EntityPage { readonly submitBtn = page.getByRole(...) }` → readonly_locator_props
#   5. `export const EMAIL_INPUT = "..."` at module scope  → module_const_bag
#
# All accept both UPPERCASE (`BTN_SEND`) and camelCase (`btnSend`) names,
# and any of the three quote styles (single, double, template literal).

_TS_LOCATOR_FIELD_RE = re.compile(
    r"""(?:^|[\s,{])(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+)*"""
    r"""(?P<name>[A-Z][A-Z0-9_]*)\s*[:=]\s*"""
    r"""(?P<quote>["'`])(?P<value>(?:(?!(?P=quote))[^\\]|\\.)*)(?P=quote)""",
    re.M,
)
# Same as _TS_LOCATOR_FIELD_RE but accepts camelCase / snake_case / PascalCase
# names. Used by inline-object and export-const patterns where SUT
# convention often uses camelCase (`btnCreate`, `emailInput`) instead of
# the Python-Selenium UPPERCASE convention.
_TS_LOCATOR_FIELD_ANY_CASE_RE = re.compile(
    r"""(?:^|[\s,{])(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+)*"""
    r"""(?P<name>[A-Za-z_$][\w$]*)\s*[:=]\s*"""
    r"""(?P<quote>["'`])(?P<value>(?:(?!(?P=quote))[^\\]|\\.)*)(?P=quote)""",
    re.M,
)
# Class/const declarations whose IDENTIFIER ends with the Locators/Selectors/
# Elements suffix (PascalCase and UPPER_SNAKE both allowed via re.I). Anchored
# to the end of the name so decorative words BEFORE the suffix count but the
# suffix itself must be present (e.g. `BasePageLocators`, `LOGIN_SELECTORS`,
# `PageElements` all match; `defaultLocatorStrategy` -- suffix followed by
# `Strategy` -- does NOT).
_TS_LOCATOR_CLASS_RE = re.compile(
    r"""(?:export\s+)?(?:class|const)\s+(?P<name>\w*(?:Locators?|Selectors?|Elements?|LOCATORS?|SELECTORS?|ELEMENTS?))\b""",
)
# Exported const whose IDENTIFIER ends with the same suffix family, e.g.
# `BASE_LOCATORS`, `PageLocators`, `App_Selectors`. Case-insensitive but
# ANCHORED to the tail of the name via `$` inside `\b` -- excludes names
# where the suffix is a prefix or middle substring
# (`defaultLocatorStrategy`, `elementCounter`, `selectorTests` do NOT match).
# Non-locator objects that still pass the tail-suffix filter are filtered
# downstream by `_looks_like_selector` on their values.
_TS_EXPORT_CONST_LOCATOR_RE = re.compile(
    r"""export\s+const\s+(?P<name>[A-Za-z_$][\w$]*?(?:locators?|selectors?|elements?))\s*"""
    r"""(?::\s*[\w<>\[\],\s.]+?)?\s*=\s*\{""",
    re.I,
)
# `<Pom>.locators.ts` / `<Pom>.selectors.tsx` / ... filename convention: the
# stem before the `.locators`/`.selectors`/`.elements` infix names the POM that
# owns the bag (e.g. `BasePage.locators.ts` -> `BasePage`). Lets the export-
# const scanner set `owning_pom` so Step 8's locator resolver can match a
# create_tbd locator (owning_page) to the bag even when the bag identifier
# (`BASE_LOCATORS`) doesn't follow the `{Pom}Locators` naming fallback.
_TS_LOCATOR_FILE_POM_RE = re.compile(
    r"""^(?P<pom>\w+?)\.(?:locators?|selectors?|elements?)\.\w+$""",
    re.I,
)


def _infer_owning_pom_from_filename(src: Path) -> str | None:
    """Derive the owning POM from a ``<Pom>.locators.ts``-style filename.

    Returns None unless the file follows the
    ``<Pom>.<locators|selectors|elements>.<ext>`` dot-infix convention, so
    bare ``locators.ts`` or PascalCase ``EntityLocators.ts`` (no dot infix)
    keep ``owning_pom=None`` and fall back to the ``{Pom}Locators`` class-name
    resolution — no behavioural change for those shapes."""
    m = _TS_LOCATOR_FILE_POM_RE.match(src.name)
    return m.group("pom") if m else None
# `class Foo { elements: ... = { ... }` — inline object property inside a
# POM class body. `container` captures the property name (elements,
# selectors, locators, etc.) — used as owning_pom + container_name to
# route new TBD constants back to the same place.
_TS_INLINE_LOCATOR_PROP_RE = re.compile(
    r"""(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+)*"""
    r"""(?P<container>elements|selectors|locators|selectorsMap|elems)\s*"""
    r"""(?::\s*(?:Record<[^>]*>|\{[^}]*\}|[\w<>\[\],\s.]+?))?\s*=\s*\{""",
)
# `readonly submitBtn = this.page.getByRole('button', { name: 'Submit' })`
# — Playwright-idiomatic Locator properties. The scanner records the
# property name; the "selector" is left blank because the actual value is
# a Locator object, not a string. Enough to prevent Step 8 from
# planning a duplicate.
_TS_READONLY_LOCATOR_PROP_RE = re.compile(
    r"""(?:public\s+|private\s+|protected\s+|static\s+)*readonly\s+"""
    r"""(?P<name>[\w$]+)\s*(?::\s*Locator)?\s*=\s*"""
    r"""(?:this\.)?page\.(?:locator|getBy\w+)\(""",
)


def _scan_ts_separate_class(
    src: Path, text: str, module_root: Path,
) -> list[LocatorClass]:
    """Detector #1 + #5: separate `class *Locators*` or module-level
    UPPERCASE `const X = "selector"` bag under `locators/` dirs.

    Mirrors the historical scanner's behaviour to keep Python-Selenium /
    original-TS-Locators SUTs working identically.
    """
    name_low = src.name.lower()
    in_locators_dir = any(p.lower() == "locators" for p in src.parts)
    if _LOCATOR_CLASS_HINT not in name_low and not in_locators_dir:
        return []
    class_match = _TS_LOCATOR_CLASS_RE.search(text)
    class_name = class_match.group("name") if class_match else src.stem
    constants: list[LocatorConstant] = []
    seen_names: set[str] = set()
    for m in _TS_LOCATOR_FIELD_RE.finditer(text):
        name = m.group("name")
        if name in seen_names:
            continue
        value = m.group("value")
        if not _looks_like_selector(value):
            continue
        seen_names.add(name)
        line_no = text.count("\n", 0, m.start("name")) + 1
        constants.append(LocatorConstant(name=name, selector=value, line=line_no))
    if not constants:
        return []
    kept, dropped = _truncate_constants(constants)
    rel = relative_posix(src, module_root)
    return [LocatorClass(
        name=class_name, file=rel, class_name=class_name,
        constants=kept, import_path=None, truncated_count=dropped,
        location_pattern="separate_class",
    )]


def _scan_ts_export_const_object(
    src: Path, text: str, module_root: Path,
) -> list[LocatorClass]:
    """Detector #2: `export const *Locators*/*Selectors*/*Elements* = {...}`.

    Accepts both UPPERCASE and camelCase keys. Fires on any file
    regardless of directory — the export-const convention is common in
    files named after the page (e.g. `LoginPage.ts` alongside a POM).
    """
    out: list[LocatorClass] = []
    for cm in _TS_EXPORT_CONST_LOCATOR_RE.finditer(text):
        open_brace = text.find("{", cm.end() - 1)
        if open_brace == -1:
            continue
        close = _find_matching_brace(text, open_brace)
        if close == -1:
            continue
        body = text[open_brace + 1: close]
        constants = _extract_locator_fields_any_case(body, text, open_brace + 1)
        if not constants:
            continue
        kept, dropped = _truncate_constants(constants)
        rel = relative_posix(src, module_root)
        name = cm.group("name")
        out.append(LocatorClass(
            name=name, file=rel, class_name=name,
            constants=kept, import_path=None, truncated_count=dropped,
            location_pattern="export_const_object",
            owning_pom=_infer_owning_pom_from_filename(src),
        ))
    return out


def _scan_ts_inline_locator_prop(
    src: Path, text: str, module_root: Path,
) -> list[LocatorClass]:
    """Detector #3: `class Foo { elements = {...} }` — the user's SUT
    convention. `class Foo { selectors: Record<...> = {...} }` too.

    Records `owning_pom` + `container_name` so Step 8 knows to APPEND new
    TBD constants to the same inline object instead of creating a
    separate `FooLocators.ts` file.
    """
    out: list[LocatorClass] = []
    class_re = re.compile(r"(?:export\s+)?class\s+([\w$]+)")
    for cls_m in class_re.finditer(text):
        cls_name = cls_m.group(1)
        # Find the class body (first `{` after the header).
        class_body_open = text.find("{", cls_m.end())
        if class_body_open == -1:
            continue
        class_body_close = _find_matching_brace(text, class_body_open)
        if class_body_close == -1:
            continue
        class_body = text[class_body_open + 1: class_body_close]
        for prop_m in _TS_INLINE_LOCATOR_PROP_RE.finditer(class_body):
            container = prop_m.group("container")
            # Locate the opening `{` of the container's object literal.
            open_rel = class_body.find("{", prop_m.end() - 1)
            if open_rel == -1:
                continue
            # Convert to absolute offset in `text` for _find_matching_brace.
            open_abs = class_body_open + 1 + open_rel
            close_abs = _find_matching_brace(text, open_abs)
            if close_abs == -1:
                continue
            obj_body = text[open_abs + 1: close_abs]
            constants = _extract_locator_fields_any_case(
                obj_body, text, open_abs + 1,
            )
            if not constants:
                continue
            kept, dropped = _truncate_constants(constants)
            rel = relative_posix(src, module_root)
            out.append(LocatorClass(
                name=f"{cls_name}.{container}",
                file=rel,
                class_name=cls_name,
                constants=kept, import_path=None, truncated_count=dropped,
                location_pattern="inline_object_property",
                owning_pom=cls_name,
                container_name=container,
            ))
    return out


def _scan_ts_readonly_locator_props(
    src: Path, text: str, module_root: Path,
) -> list[LocatorClass]:
    """Detector #4: `readonly submitBtn = this.page.getByRole(...)` etc.

    The property value is a Playwright ``Locator`` object, not a string,
    so ``constants[].selector`` is left empty (existence dedup only).
    """
    out: list[LocatorClass] = []
    class_re = re.compile(r"(?:export\s+)?class\s+([\w$]+)")
    for cls_m in class_re.finditer(text):
        cls_name = cls_m.group(1)
        class_body_open = text.find("{", cls_m.end())
        if class_body_open == -1:
            continue
        class_body_close = _find_matching_brace(text, class_body_open)
        if class_body_close == -1:
            continue
        class_body = text[class_body_open + 1: class_body_close]
        constants: list[LocatorConstant] = []
        seen: set[str] = set()
        for prop_m in _TS_READONLY_LOCATOR_PROP_RE.finditer(class_body):
            name = prop_m.group("name")
            if name in seen:
                continue
            seen.add(name)
            line_no = text.count(
                "\n", 0, class_body_open + 1 + prop_m.start("name"),
            ) + 1
            constants.append(LocatorConstant(
                name=name, selector="", line=line_no,
            ))
        if not constants:
            continue
        kept, dropped = _truncate_constants(constants)
        rel = relative_posix(src, module_root)
        out.append(LocatorClass(
            name=cls_name, file=rel, class_name=cls_name,
            constants=kept, import_path=None, truncated_count=dropped,
            location_pattern="readonly_locator_props",
            owning_pom=cls_name,
        ))
    return out


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return the index of the `}` matching the `{` at *open_idx*, or -1."""
    if open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 0
    # Track quotes so `{` inside string literals doesn't count.
    quote: str | None = None
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _extract_locator_fields_any_case(
    body: str, full_text: str, body_offset: int,
) -> list[LocatorConstant]:
    """Extract `key: "value"` / `key = "value"` pairs from *body*.

    Accepts any-case identifiers; filters values via ``_looks_like_selector``.
    Returns constants with line numbers relative to ``full_text``.
    """
    out: list[LocatorConstant] = []
    seen: set[str] = set()
    for m in _TS_LOCATOR_FIELD_ANY_CASE_RE.finditer(body):
        name = m.group("name")
        if name in seen:
            continue
        # Filter TypeScript / JavaScript reserved words that could
        # accidentally match (`return`, `if`, `class`, ...).
        if name in _TS_RESERVED_WORDS:
            continue
        value = m.group("value")
        if not _looks_like_selector(value):
            continue
        seen.add(name)
        line_no = full_text.count("\n", 0, body_offset + m.start("name")) + 1
        out.append(LocatorConstant(name=name, selector=value, line=line_no))
    return out


_TS_RESERVED_WORDS: frozenset[str] = frozenset({
    "if", "else", "for", "while", "do", "return", "throw", "try",
    "catch", "finally", "switch", "case", "default", "break", "continue",
    "class", "extends", "implements", "interface", "type", "enum",
    "const", "let", "var", "function", "async", "await", "new", "this",
    "super", "import", "export", "from", "as", "in", "of", "instanceof",
    "typeof", "void", "null", "undefined", "true", "false",
    "public", "private", "protected", "static", "readonly", "abstract",
})


def scan_ts_locators(module_root: Path) -> list[LocatorClass]:
    """Regex-extract locator constants from `.ts`/`.tsx`/`.js`/`.jsx`/`.mjs`.

    Runs FIVE detectors per file and unions the results — SUTs can freely
    mix conventions or use any single one:

    1. **separate_class** — historical Python-Selenium-style ``class
       FooLocators { X = "..."; }`` in files whose name/dir contains
       "locator".
    2. **export_const_object** — ``export const FooLocators = { X: "..."};``
       (or ``Selectors``, ``Elements``). Any file.
    3. **inline_object_property** — ``class Foo { elements = {btnX: "..."} }``
       inline on a POM class. Common Playwright-TS convention.
    4. **readonly_locator_props** — ``readonly submitBtn = page.getByRole(...)``
       Playwright ``Locator`` properties.
    5. Module-level UPPERCASE bag (folded into detector #1's file filter).

    Each result records ``location_pattern`` so Step 8 can add new TBD
    constants in the same style. Duplicates across detectors are deduped
    by ``(file, class_name, constant_name)``.
    """
    if not module_root.exists():
        return []
    out: list[LocatorClass] = []
    seen_files: set[Path] = set()
    detectors = (
        _scan_ts_separate_class,
        _scan_ts_export_const_object,
        _scan_ts_inline_locator_prop,
        _scan_ts_readonly_locator_props,
    )
    for src in _iter_ts_files(module_root):
        if src in seen_files:
            continue
        seen_files.add(src)
        text = _read_text(src)
        for detector in detectors:
            try:
                out.extend(detector(src, text, module_root))
            except Exception:
                # A malformed file shouldn't take down inventory —
                # skip and continue with the next detector.
                continue
    return _dedup_locator_classes(out)


def _dedup_locator_classes(entries: list[LocatorClass]) -> list[LocatorClass]:
    """Dedup by (file, class_name, constant name).

    When two detectors produce overlapping results (e.g. an export-const
    object detected by both #1 and #2), keep the entry from the MORE
    specific detector — priority: inline_object_property >
    readonly_locator_props > export_const_object > separate_class.
    """
    priority = {
        "inline_object_property": 4,
        "readonly_locator_props": 3,
        "export_const_object": 2,
        "separate_class": 1,
    }
    best_by_key: dict[tuple[str, str], LocatorClass] = {}
    for e in entries:
        key = (e.file, e.class_name)
        current = best_by_key.get(key)
        if current is None or priority.get(e.location_pattern, 0) > priority.get(
            current.location_pattern, 0,
        ):
            best_by_key[key] = e
    return list(best_by_key.values())


def _is_pytest_fixture_decorator(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Attribute):
        return dec.attr == "fixture"
    if isinstance(dec, ast.Call):
        return _is_pytest_fixture_decorator(dec.func)
    if isinstance(dec, ast.Name):
        return dec.id == "fixture"
    return False


def _fixture_scope(dec: ast.expr) -> str:
    """Extract `scope=` kwarg from a `@pytest.fixture(...)` call."""
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "scope":
                lit = literal_str(kw.value)
                if lit is not None:
                    return lit
    return "function"


def scan_python_fixtures(module_root: Path) -> list[Fixture]:
    out: list[Fixture] = []
    # Fixtures live in tests/, conftest.py, or *fixtures*.py anywhere under tests/.
    tests_root = module_root / "tests"
    if not tests_root.is_dir():
        return out
    for src in iter_python_files(tests_root):
        tree = parse_file(src)
        if tree is None:
            continue
        rel = relative_posix(src, module_root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            scope = "function"
            is_fixture = False
            for dec in node.decorator_list:
                if _is_pytest_fixture_decorator(dec):
                    is_fixture = True
                    scope = _fixture_scope(dec)
                    break
            if not is_fixture:
                continue
            depends = [a.arg for a in node.args.args if a.arg != "self"]
            # Try to infer `yields` from return annotation or first yield literal.
            yields: str | None = None
            if node.returns is not None:
                yields = ast.unparse(node.returns)
            out.append(Fixture(
                name=node.name,
                file=rel,
                scope=scope,
                yields=yields,
                depends_on=depends,
            ))
    return out


def scan_python_auth_flow(
    module_root: Path,
    page_objects: list[PageObject],
    fixtures: list[Fixture],
) -> AuthFlow:
    """Identify the SUT's auth entry point + the fixture wiring it (if any)."""
    auth_pages = [po for po in page_objects if po.scope == "auth"]
    # Pick the entry method: prefer a class whose name matches sign_in / login.
    entry_method: str | None = None
    entry_type = "unknown"
    if auth_pages:
        page = auth_pages[0]
        # Find a method whose name implies the public sign-in entry.
        for candidate in ("sign_in", "signin", "login", "authenticate", "do_login"):
            if candidate in page.methods:
                entry_method = f"{page.file}:{page.class_name}.{candidate}"
                break
        if entry_method is None and page.methods:
            entry_method = f"{page.file}:{page.class_name}.{page.methods[0]}"
        # SSO vs OAuth vs basic — best-guess by name.
        low = page.name.lower() + page.file.lower()
        if "sso" in low:
            entry_type = "sso"
        elif "oauth" in low:
            entry_type = "oauth"
        else:
            entry_type = "sso"  # default for sign-in pages with SSO-like patterns
    else:
        # Fallback: grep for auth files outside the page-object roots.
        for src in iter_python_files(module_root, contains_hint=b"def "):
            if not _AUTH_FILE_RE.search(src.name):
                continue
            tree = parse_file(src)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name in ("sign_in", "signin", "login", "authenticate")
                ):
                    rel = relative_posix(src, module_root)
                    entry_method = f"{rel}:{node.name}"
                    entry_type = "sso"
                    break
            if entry_method:
                break

    fixture_entry: str | None = None
    if entry_method:
        # The first fixture whose body references the auth page class or one of
        # its methods wins. Cheap approximation: any fixture whose `depends_on`
        # mentions "page" + whose source file is under tests/fixtures or tests/.
        for f in fixtures:
            depends_str = " ".join(f.depends_on)
            if (
                "page" in depends_str.lower()
                or "sign" in f.name.lower()
                or "auth" in f.name.lower()
            ):
                fixture_entry = f"{f.file}:{f.name}"
                break

    # Credentials env-vars: heuristic — common names.
    candidates_env = ["SSO_USER", "SSO_PASSWORD", "USER", "PASSWORD",
                      "API_USER", "API_PASSWORD", "AUTH_USER", "AUTH_PASSWORD"]
    found_env: list[str] = []
    if entry_method:
        # Read the entry file and look for `os.environ.get("X")` / `os.getenv("X")` / `settings.x`.
        file_part = entry_method.split(":")[0]
        src = module_root / file_part
        if src.exists():
            text = _read_text(src)
            for cand in candidates_env:
                if cand in text:
                    found_env.append(cand)

    prefer = entry_method.split(":")[0] if entry_method else None
    return AuthFlow(
        type=entry_type if entry_method else "unknown",
        entry_method=entry_method,
        credentials_env_vars=found_env,
        fixture_entry=fixture_entry,
        open_method=_detect_open_method(page_objects, prefer_file=prefer),
    )


# ---------------------------------------------------------------------------
# Open-base-URL method detection (language-agnostic; operates on PageObject)
# ---------------------------------------------------------------------------

# Normalized (lowercased, separators stripped) method-name signals for the
# "navigate to the app base URL" step — the mandatory FIRST action of any UI
# test, before login. Kept language-agnostic so it works on Python snake_case
# and TS camelCase alike after normalization.
_OPEN_METHOD_NAMES: frozenset[str] = frozenset({
    "open", "openbaseurl", "openurl", "openpage", "openapp", "openhome",
    "openbasepage", "opensite", "openhomepage", "openloginpage",
    "goto", "gotobaseurl", "gotohome", "navigate", "navigateto",
    "navigatetobaseurl", "visit", "load", "loadapp", "gohome",
})


def _norm_method_name(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


def _is_open_method_name(name: str) -> bool:
    n = _norm_method_name(name)
    if n in _OPEN_METHOD_NAMES:
        return True
    # Strong prefix signal: openBaseURL, openHomePage, openLoginPage, ...
    if n.startswith("open") and any(
        t in n for t in ("url", "base", "app", "home", "page", "site")
    ):
        return True
    return False


def _detect_open_method(
    pages: list[PageObject], prefer_file: str | None = None
) -> str | None:
    """Find a page-object method that navigates to the app base URL.

    Returns ``"<file>:<Class>.<method>"`` or ``None``. When several methods
    match, prefers one on ``prefer_file`` (usually the auth entry's file /
    BasePage), else the first found. Searches ALL page objects — the open/
    navigate method commonly lives on a BasePage, not the auth page.
    """
    matches: list[tuple[PageObject, str]] = []
    for po in pages:
        for m in po.methods:
            if _is_open_method_name(m):
                matches.append((po, f"{po.file}:{po.class_name}.{m}"))
    if not matches:
        return None
    if prefer_file:
        for po, ref in matches:
            if po.file == prefer_file:
                return ref
    return matches[0][1]


# ---------------------------------------------------------------------------
# Lifecycle-hook discovery (setup / teardown), classified by trigger event
# ---------------------------------------------------------------------------

_HOOK_CALLS_CAP = 30

_UNITTEST_HOOK_EVENTS: dict[str, str] = {
    "setUpClass": "before_all",
    "tearDownClass": "after_all",
    "setUp": "before_each",
    "tearDown": "after_each",
}
_PYTEST_MODULE_HOOK_EVENTS: dict[str, str] = {
    "pytest_runtest_setup": "before_each",
    "pytest_runtest_teardown": "after_each",
}


def _stmt_outermost_call(stmt: ast.stmt) -> str | None:
    """The primary call expression of a statement, as dotted source text.

    Unwraps ``Expr`` / ``Await`` / ``Assign`` / ``Return`` so that a body of
    ``await base.openBaseURL()`` / ``x = await make()`` yields the readable
    high-level callee (``base.openBaseURL``), not the inner sub-calls.
    """
    node: ast.AST = stmt
    if isinstance(node, ast.Expr):
        node = node.value
    elif isinstance(node, ast.Assign):
        node = node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        node = node.value
    elif isinstance(node, ast.Return) and node.value is not None:
        node = node.value
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call):
        try:
            return ast.unparse(node.func)
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _ordered_calls_from_body(body: list[ast.stmt]) -> list[str]:
    out: list[str] = []
    for stmt in body:
        c = _stmt_outermost_call(stmt)
        if c:
            out.append(c)
        if len(out) >= _HOOK_CALLS_CAP:
            break
    return out


def _fixture_autouse(dec: ast.expr) -> bool:
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "autouse" and isinstance(kw.value, ast.Constant):
                return bool(kw.value.value)
    return False


def _split_body_at_yield(
    body: list[ast.stmt],
) -> tuple[list[ast.stmt], list[ast.stmt]]:
    """Split a fixture body into pre-yield (setup) and post-yield (teardown)."""
    pre: list[ast.stmt] = []
    post: list[ast.stmt] = []
    seen = False
    for stmt in body:
        is_yield = isinstance(stmt, ast.Expr) and isinstance(
            stmt.value, ast.Yield | ast.YieldFrom
        )
        if is_yield:
            seen = True
            continue
        (post if seen else pre).append(stmt)
    return pre, post


def scan_python_lifecycle_hooks(module_root: Path) -> list[LifecycleHook]:
    """Discover pytest/unittest setup-teardown hooks under ``tests/``.

    - unittest ``setUpClass``/``tearDownClass``/``setUp``/``tearDown``
    - pytest module functions ``pytest_runtest_setup``/``_teardown``
    - autouse ``@pytest.fixture`` (scope maps to before_all vs before_each; a
      yielding fixture also yields a matching after_* hook)
    """
    out: list[LifecycleHook] = []
    tests_root = module_root / "tests"
    if not tests_root.is_dir():
        return out
    for src in iter_python_files(tests_root):
        tree = parse_file(src)
        if tree is None:
            continue
        rel = relative_posix(src, module_root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name in _UNITTEST_HOOK_EVENTS:
                out.append(LifecycleHook(
                    event=_UNITTEST_HOOK_EVENTS[node.name],
                    file=rel,
                    framework_construct=node.name,
                    scope="class" if "Class" in node.name else "function",
                    calls=_ordered_calls_from_body(node.body),
                ))
                continue
            if node.name in _PYTEST_MODULE_HOOK_EVENTS:
                out.append(LifecycleHook(
                    event=_PYTEST_MODULE_HOOK_EVENTS[node.name],
                    file=rel,
                    framework_construct=node.name,
                    calls=_ordered_calls_from_body(node.body),
                ))
                continue
            fixture_dec = next(
                (d for d in node.decorator_list if _is_pytest_fixture_decorator(d)),
                None,
            )
            # Only autouse fixtures act as implicit setup hooks; explicit
            # (non-autouse) fixtures are already captured in existing_fixtures.
            if fixture_dec is None or not _fixture_autouse(fixture_dec):
                continue
            scope = _fixture_scope(fixture_dec)
            before_event = (
                "before_all"
                if scope in ("module", "session", "package")
                else "before_each"
            )
            after_event = (
                "after_all"
                if scope in ("module", "session", "package")
                else "after_each"
            )
            pre, post = _split_body_at_yield(node.body)
            construct = f"@pytest.fixture(scope={scope}, autouse=True)"
            if pre:
                out.append(LifecycleHook(
                    event=before_event, file=rel,
                    framework_construct=construct, scope=scope,
                    calls=_ordered_calls_from_body(pre),
                ))
            if post:
                out.append(LifecycleHook(
                    event=after_event, file=rel,
                    framework_construct=construct, scope=scope,
                    calls=_ordered_calls_from_body(post),
                ))
    return out


# ---------------------------------------------------------------------------
# Tier 2: TS/JS regex heuristics
# ---------------------------------------------------------------------------


_TS_GLOBS = ("**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs")
_TS_CLASS_RE = re.compile(
    # Named `*Page*` / `*PageObject` / `*PO` classes (positive-signal path).
    # Allow `extends`/`implements` between the name and the body — the
    # original `[{<]` requirement missed every subclass in the wild.
    r"\bclass\s+(\w+(?:Page|PageObject|PO))\b"
)
_TS_ANY_CLASS_RE = re.compile(
    # Any class declaration — used for structural POM detection when the
    # naming heuristic is absent (the SUT uses e.g. `class Login` or
    # `class DashboardScreen` instead of `class DashboardPage`).
    r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([\w$]+)\b"
)
_TS_METHOD_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|async\s+)*(\w+)\s*\([^)]*\)\s*[:{]",
    re.M,
)
# Structural POM signals — a class whose body calls one of these APIs is
# almost certainly a page object regardless of what it's named.
_TS_POM_CALL_HINTS: tuple[str, ...] = (
    "page.locator", "page.getBy", "page.click", "page.fill",
    "page.goto", "page.waitFor", "page.evaluate",
    "this.page.locator", "this.page.getBy", "this.page.click",
    "this.page.fill", "this.page.goto", "this.page.waitFor",
    "driver.findElement", "driver.get", "driver.wait",
    "cy.get", "cy.contains", "cy.visit",
    "browser.$", "browser.url",
)
_TS_EXPORT_FUNC_RE = re.compile(r"export\s+(?:async\s+)?function\s+(\w+)\s*\(")
_TS_FIXTURE_RE = re.compile(r"\w+\.extend\s*(?:<[^>]*>\s*)?\(\s*\{")
_TS_AUTH_NAME_RE = re.compile(r"\b(login|signIn|signOn|authenticate)\b")


def _skip_path_part(parts: tuple[str, ...]) -> bool:
    from qtea._ast_utils import SKIP_DIR_NAMES
    return any(p in SKIP_DIR_NAMES for p in parts)


def _iter_ts_files(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    for pat in _TS_GLOBS:
        for p in root.glob(pat):
            if not p.is_file() or _skip_path_part(p.parts):
                continue
            try:
                if p.stat().st_size > 512_000:
                    continue
            except OSError:
                continue
            out.append(p)
    return out


def scan_ts_page_objects(module_root: Path) -> list[PageObject]:
    """Detect Playwright / Cypress / WebdriverIO page-object classes.

    Two-signal recognition:

    - **Naming heuristic** (positive signal): class name ends with ``Page``,
      ``PageObject``, or ``PO``. Fast and precise for the common case.
    - **Structural intent** (fallback): any class whose body calls
      ``page.locator`` / ``page.getBy*`` / ``driver.findElement`` / ``cy.get``
      / ``browser.$`` is treated as a POM even without a matching name.

    Also populates ``has_inline_locators`` when the class body defines an
    ``elements`` / ``selectors`` / ``locators`` inline object — Step 8
    uses this to place new TBD constants in the same convention.
    """
    out: list[PageObject] = []
    seen: set[Path] = set()

    # Broadened search: page-object dirs (any name in _PAGE_DIR_NAMES) +
    # module-level `src/` where SUTs often colocate POMs with production
    # code. We still restrict to files where at least one POM signal
    # fires to avoid indexing every TS class in the repo.
    candidate_dirs: list[Path] = list(_find_page_dirs(module_root))
    # Add well-known top-level source roots when they exist — POMs in
    # `src/` at the root are common in Playwright-TS SUTs.
    for candidate in ("src", "test", "tests", "e2e"):
        p = module_root / candidate
        if p.is_dir() and p not in candidate_dirs:
            candidate_dirs.append(p)

    for page_dir in candidate_dirs:
        for src in _iter_ts_files(page_dir):
            if src in seen:
                continue
            seen.add(src)
            text = _read_text(src)
            # Skip files that don't even declare a class.
            all_classes = _TS_ANY_CLASS_RE.findall(text)
            if not all_classes:
                continue

            # Positive-signal names win — they get every class in the
            # file. If no positive signals, fall back to structural
            # detection: a class body with POM-shaped API calls.
            named_pom = set(_TS_CLASS_RE.findall(text))
            has_structural_signal = any(
                hint in text for hint in _TS_POM_CALL_HINTS
            )

            if not named_pom and not has_structural_signal:
                continue

            # Which classes qualify? Union of the named set + all classes
            # in a structural-signal file.
            qualifying = set(all_classes) if has_structural_signal else set()
            qualifying.update(named_pom)

            methods = sorted({
                m for m in _TS_METHOD_RE.findall(text)
                if m not in {"if", "for", "while", "switch", "return",
                             "function", "constructor"}
                and not m.startswith("_")
            })
            has_inline = _ts_class_has_inline_locator_container(text)

            rel = relative_posix(src, module_root)
            for cls in sorted(qualifying):
                out.append(PageObject(
                    name=cls,
                    file=rel,
                    class_name=cls,
                    methods=methods,
                    scope=_scope_for_name(cls),
                    has_inline_locators=has_inline,
                ))
    # A class can be picked up twice when a file lives under BOTH
    # `pages/` and `src/` (nested). Dedup on (file, class_name).
    return _dedup_page_objects(out)


def _ts_class_has_inline_locator_container(text: str) -> bool:
    """True when *text* contains ``elements``/``selectors``/``locators`` = {...}.

    Fast substring check first, then confirm with the property regex.
    Used to set ``PageObject.has_inline_locators``.
    """
    if not any(hint in text for hint in (
        "elements", "selectors", "locators", "selectorsMap", "elems",
    )):
        return False
    return bool(_TS_INLINE_LOCATOR_PROP_RE.search(text))


def _dedup_page_objects(entries: list[PageObject]) -> list[PageObject]:
    """Dedup by (file, class_name). Preserve the first-seen entry."""
    seen: set[tuple[str, str]] = set()
    out: list[PageObject] = []
    for e in entries:
        key = (e.file, e.class_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def scan_ts_helpers(module_root: Path) -> list[Helper]:
    out: list[Helper] = []
    for sub in ("tests/helpers", "tests/utils", "tests/support", "helpers", "utils"):
        for src in _iter_ts_files(module_root / sub):
            text = _read_text(src)
            for fn_name in _TS_EXPORT_FUNC_RE.findall(text):
                if fn_name.startswith("_"):
                    continue
                rel = relative_posix(src, module_root)
                out.append(Helper(
                    name=fn_name, file=rel,
                    signature=f"{fn_name}(...)", purpose="",
                ))
    return out


def scan_ts_fixtures(module_root: Path) -> list[Fixture]:
    """Playwright `*.extend<...>({ ... })` fixture blocks.

    Accepts both ``test.extend<T>({...})`` and ``baseTest.extend({...})``
    forms (any receiver name, generic param optional), and both async
    (``key: async ({page}, use) => ...``) and sync (``key: ({page}, use)
    => ...``) fixture bodies. See ``codegen_reconcile._scan_ts_fixture_symbols``
    for the twin scanner used during Phase B.5 reconciliation.
    """
    out: list[Fixture] = []
    tests_root = module_root / "tests"
    if not tests_root.exists():
        return out
    for src in _iter_ts_files(tests_root):
        text = _read_text(src)
        if not _TS_FIXTURE_RE.search(text):
            continue
        rel = relative_posix(src, module_root)
        # Extract the keys inside the first `{ ... }` after `<receiver>.extend`.
        m = re.search(
            r"\w+\.extend\s*(?:<[^>]*>\s*)?\(\s*\{(.+?)\}\s*\)",
            text,
            re.S,
        )
        if not m:
            continue
        body = m.group(1)
        for key_match in re.finditer(
            r"^\s*(\w+)\s*:\s*(?:async\s*)?\(", body, re.M,
        ):
            out.append(Fixture(
                name=key_match.group(1), file=rel,
                scope="function", yields=None, depends_on=[],
            ))
    return out


def scan_ts_auth_flow(module_root: Path, pages: list[PageObject]) -> AuthFlow:
    open_method = _detect_open_method(pages)
    auth_pages = [p for p in pages if p.scope == "auth"]
    if auth_pages:
        page = auth_pages[0]
        entry = (
            f"{page.file}:{page.class_name}.{page.methods[0]}"
            if page.methods
            else f"{page.file}:{page.class_name}"
        )
        return AuthFlow(type="sso", entry_method=entry,
                        credentials_env_vars=[], fixture_entry=None,
                        open_method=open_method)
    # Fallback: grep export login/signIn functions in *auth*.ts files.
    for src in _iter_ts_files(module_root):
        if not _AUTH_FILE_RE.search(src.name):
            continue
        text = _read_text(src)
        m = re.search(
            r"export\s+(?:async\s+)?function\s+(login|signIn|signOn|authenticate)\s*\(",
            text,
        )
        if m:
            rel = relative_posix(src, module_root)
            return AuthFlow(
                type="sso", entry_method=f"{rel}:{m.group(1)}",
                credentials_env_vars=[], fixture_entry=None,
                open_method=open_method,
            )
    return AuthFlow(open_method=open_method)


_TS_HOOK_EVENT: dict[str, str] = {
    "beforeAll": "before_all",
    "afterAll": "after_all",
    "beforeEach": "before_each",
    "afterEach": "after_each",
    "before": "before_all",   # Mocha
    "after": "after_all",     # Mocha
}
# `test.beforeEach(` / bare `beforeEach(` / `before(` (Mocha). We only scan
# files under tests/, so bare before/after false positives are unlikely.
_TS_HOOK_RE = re.compile(
    r"(?:\btest\s*\.\s*)?\b(beforeAll|afterAll|beforeEach|afterEach|before|after)\s*\(",
)
_TS_CALL_CHAIN_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*\(",
)


def _ts_balanced_parens_after(text: str, after_open: int) -> str | None:
    """Return the text between a hook's ``(`` and its matching ``)``.

    ``after_open`` is the index just past the opening paren (``m.end()`` of the
    hook regex, which ends in ``\\(``). Balancing parens over the ENTIRE
    callback — signature + body — sidesteps the arrow-param-vs-body brace
    ambiguity (``async ({ basePage }) => { ... }``): the call-chain regex only
    matches ``x.y(`` patterns, which never appear in a destructured signature.
    """
    depth = 1
    for j in range(after_open, len(text)):
        ch = text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[after_open:j]
    return None


def _ts_call_chains(block: str) -> list[str]:
    out: list[str] = []
    for m in _TS_CALL_CHAIN_RE.finditer(block):
        out.append(m.group(1))
        if len(out) >= _HOOK_CALLS_CAP:
            break
    return out


def scan_ts_lifecycle_hooks(module_root: Path) -> list[LifecycleHook]:
    """Discover JS/TS ``beforeAll``/``afterAll``/``beforeEach``/``afterEach``
    (and Mocha ``before``/``after``) hooks under ``tests/``, capturing the
    ordered method-call chains in each hook body."""
    out: list[LifecycleHook] = []
    tests_root = module_root / "tests"
    if not tests_root.exists():
        return out
    for src in _iter_ts_files(tests_root):
        text = _read_text(src)
        rel = relative_posix(src, module_root)
        for m in _TS_HOOK_RE.finditer(text):
            kw = m.group(1)
            event = _TS_HOOK_EVENT.get(kw)
            if not event:
                continue
            block = _ts_balanced_parens_after(text, m.end())
            calls = _ts_call_chains(block) if block else []
            out.append(LifecycleHook(
                event=event, file=rel, framework_construct=kw, calls=calls,
            ))
    return out


# ---------------------------------------------------------------------------
# Language detection for a module
# ---------------------------------------------------------------------------


def _detect_module_language(module_root: Path, profile: StackProfile | None) -> str:
    """Best-guess language for the module.

    Strategy: count source-file extensions in the module. The profile's
    `language` is a generic hint (e.g. `_npm_build` always says "javascript"
    even on TS projects), so we use the counts as the primary signal and the
    profile as a fallback when the module has no obvious source files.
    """
    counts: dict[str, int] = {}
    for p in module_root.rglob("*"):
        if not p.is_file() or _skip_path_part(p.parts):
            continue
        ext = p.suffix.lower()
        counts[ext] = counts.get(ext, 0) + 1

    py = counts.get(".py", 0)
    ts = counts.get(".ts", 0) + counts.get(".tsx", 0)
    js = counts.get(".js", 0) + counts.get(".jsx", 0) + counts.get(".mjs", 0)
    java = counts.get(".java", 0)
    robot = counts.get(".robot", 0)
    rb = counts.get(".rb", 0)
    go = counts.get(".go", 0)
    kt = counts.get(".kt", 0) + counts.get(".kts", 0)
    cs = counts.get(".cs", 0)
    rs = counts.get(".rs", 0)

    # Prefer the language with the highest source-file count.
    candidates = [
        ("python", py),
        ("typescript", ts),
        ("javascript", js),
        ("java", java),
        ("robot", robot),
        ("ruby", rb),
        ("go", go),
        ("kotlin", kt),
        ("csharp", cs),
        ("rust", rs),
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    if candidates[0][1] > 0:
        return candidates[0][0]

    # No source files yet — fall back to the profile hint.
    if profile and profile.language:
        return profile.language
    return "unknown"


# ---------------------------------------------------------------------------
# Per-module detection (Tier 1 + Tier 2)
# ---------------------------------------------------------------------------


_PYPROJECT_PACKAGE_RE = re.compile(
    r"""packages\s*=\s*\[\s*\{[^}]*?\binclude\s*=\s*['"](?P<inc>[^'"]+)['"]"""
    r"""[^}]*?\bfrom\s*=\s*['"](?P<from>[^'"]+)['"]""",
    re.S,
)
_PYPROJECT_SIMPLE_PACKAGE_RE = re.compile(
    r"""packages\s*=\s*\[\s*['"](?P<pkg>[^'"]+)['"]""",
    re.S,
)
_SETUPTOOLS_PACKAGE_DIR_RE = re.compile(
    r"""package[_-]dir\s*=\s*\{\s*['"]?['"]?\s*[:=]\s*['"](?P<dir>[^'"]+)['"]""",
    re.S,
)


def _common_parent_posix(paths: list[str]) -> str | None:
    """Return the POSIX-style common parent directory of `paths`, or None."""
    if not paths:
        return None
    try:
        parts_list = [Path(p).parts for p in paths if p]
        if not parts_list:
            return None
        common: list[str] = []
        for tup in zip(*parts_list, strict=False):
            if len(set(tup)) == 1:
                common.append(tup[0])
            else:
                break
        if not common:
            return None
        # Strip the last segment IF it's a file (heuristic: contains a '.').
        last = common[-1]
        if "." in last and not last.startswith("."):
            common = common[:-1]
        return "/".join(common) if common else None
    except (TypeError, ValueError):
        return None


def _detect_python_package_root(module_root: Path) -> str | None:
    """Parse pyproject.toml / setup.cfg / setup.py for the package root path
    (e.g. `src/askbosch_automation_frontend_sync`)."""
    pp = module_root / "pyproject.toml"
    if pp.exists():
        text = _read_text(pp)
        m = _PYPROJECT_PACKAGE_RE.search(text)
        if m:
            return f"{m.group('from')}/{m.group('inc')}".replace("\\", "/")
        m2 = _PYPROJECT_SIMPLE_PACKAGE_RE.search(text)
        if m2:
            return m2.group("pkg")
    cfg = module_root / "setup.cfg"
    if cfg.exists():
        text = _read_text(cfg)
        m = _SETUPTOOLS_PACKAGE_DIR_RE.search(text)
        if m:
            return m.group("dir")
    return None


_PAGES_OBJECT_HINT_RE = re.compile(
    r"/(pages/object|pageobjects|pages|po)(/|$)", re.I,
)
_PAGES_LOCATORS_HINT_RE = re.compile(
    r"/(pages/locators|locators)(/|$)", re.I,
)


def _find_dirs_named(module_root: Path, names: set[str]) -> list[Path]:
    """Recursively find directories whose name matches `names` (case-insens.),
    skipping the standard noise (.git, .venv, node_modules, ...)."""
    out: list[Path] = []
    if not module_root.exists():
        return out
    lower_names = {n.lower() for n in names}
    for p in module_root.glob("**/*"):
        if not p.is_dir():
            continue
        if _skip_path_part(p.parts):
            continue
        if p.name.lower() in lower_names:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Architecture pattern detection + exemplar capture (drives Step 8 lane select)
# ---------------------------------------------------------------------------

# Directory names that structurally signal the Screenplay pattern (from the
# researcher agent's §6 globs). A very strong, language-agnostic signal.
_SCREENPLAY_DIR_NAMES = {"tasks", "questions", "interactions", "abilities", "actors"}

# Base class → exemplar category. A ClassDef whose base resolves to one of
# these is a reusable Screenplay unit worth capturing verbatim.
_SCREENPLAY_BASE_CATEGORY = {
    "Task": "task",
    "Performable": "task",
    "Question": "question",
    "Interaction": "interaction",
    "Ability": "ability",
}

# Bespoke frameworks name their base classes with a category keyword rather than
# the upstream screenpy identifiers (e.g. ``BaseTask``, ``BaseQuestion``,
# ``SearchPanelInteractions``). Match the keyword as a suffix of the base name.
# Checked only after the exact-name map; ordered so plurals/longer keywords win.
_SCREENPLAY_BASE_SUFFIX = (
    ("Performable", "task"),
    ("Task", "task"),
    ("Question", "question"),
    ("Interactions", "interaction"),
    ("Interaction", "interaction"),
    ("Ability", "ability"),
)


def _screenplay_category(base_names: list[str]) -> str | None:
    """Map a class's base names to a Screenplay exemplar category.

    Recognises both the upstream screenpy identifiers (``Task``, ``Question`` …)
    and bespoke framework conventions (``BaseTask``, ``BaseQuestion`` …) so the
    exemplar lane grounds writers in the SUT's real base classes instead of the
    generic upstream library.
    """
    for b in base_names:
        if b in _SCREENPLAY_BASE_CATEGORY:
            return _SCREENPLAY_BASE_CATEGORY[b]
    for b in base_names:
        for keyword, category in _SCREENPLAY_BASE_SUFFIX:
            if b.endswith(keyword):
                return category
    return None

# Code-level fallback signals when a SUT keeps Screenplay units in one file
# rather than per-category dirs.
_SCREENPLAY_PY_CODE_RE = re.compile(
    rb"attempts_to\b|perform_as\b|answered_by\b|from\s+screenpy\b"
    rb"|class\s+\w+\s*\(\s*(?:Task|Question|Ability|Interaction|Performable)\b"
)
_SCREENPLAY_TS_CODE_RE = re.compile(
    rb"@serenity-js/core|actorCalled|\.attemptsTo\(|Task\.where|Question\.about"
    rb"|\bperformAs\b|\bansweredBy\b|\battemptsTo\b"
)

# Per-exemplar hard cap and the max number of exemplars captured per module.
_EXEMPLAR_CHAR_CAP = 4000
_EXEMPLAR_MAX = 3
# Category ordering when trimming to _EXEMPLAR_MAX — a Task + a Question convey
# the pattern's shape best; targets show the locator convention.
_EXEMPLAR_CATEGORY_ORDER = ["task", "question", "interaction", "target", "ability", "other"]

# Files under {ts,tsx,js,mjs,cjs,mts,cts} — bounded glob for the TS code signal.
_TS_SOURCE_GLOBS = ("**/*.ts", "**/*.tsx", "**/*.js", "**/*.mjs")
# Bound the file scan for the code-level signal / TS exemplar capture.
_ARCH_SCAN_FILE_CAP = 600


def _base_names(class_def: ast.ClassDef) -> list[str]:
    """Return the trailing identifier of each base.

    Handles plain names (``BaseTask``), dotted attributes (``screenpy.Task``),
    and subscripted generics (``BaseQuestion[str]`` → ``BaseQuestion``). The
    generic case is common for typed Question/Task bases and was previously
    dropped — silently losing every generic-based unit (e.g. an entire
    ``question`` category) from exemplar capture.
    """
    names: list[str] = []
    for base in class_def.bases:
        node = base.value if isinstance(base, ast.Subscript) else base
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
    return names


def _has_screenplay_code_signal(module_root: Path, language: str) -> bool:
    """Cheap content scan for Screenplay idioms when the dir signal is absent."""
    if language == "python":
        for n, src in enumerate(iter_python_files(module_root)):
            if n >= _ARCH_SCAN_FILE_CAP:
                break
            try:
                if _SCREENPLAY_PY_CODE_RE.search(src.read_bytes()):
                    return True
            except OSError:
                continue
        return False
    if language in ("typescript", "javascript"):
        n = 0
        for pat in _TS_SOURCE_GLOBS:
            for src in module_root.glob(pat):
                if n >= _ARCH_SCAN_FILE_CAP:
                    return False
                if not src.is_file() or _skip_path_part(src.parts):
                    continue
                n += 1
                try:
                    if _SCREENPLAY_TS_CODE_RE.search(src.read_bytes()):
                        return True
                except OSError:
                    continue
        return False
    return False


def detect_architecture_pattern(
    module_root: Path,
    *,
    page_objects: list[PageObject] | None = None,
    locators: list[LocatorClass] | None = None,
    language: str = "unknown",
) -> str:
    """Classify the SUT's test-automation design pattern.

    Returns one of ``pom | screenplay | inline | none | unknown``.

    Signal precedence:
      1. Screenplay-named dirs (``tasks/``, ``questions/`` …) are authoritative —
         a very strong structural signal, honoured even if a stray page object
         exists.
      2. Otherwise POM wins ties: any detected page object → ``pom`` (or
         ``inline`` when POMs carry inline locators and no separate locator
         classes exist).
      3. A code-only Screenplay signal (single-file units) → ``screenplay``.
      4. Locator classes but no page objects → ``pom``.
      5. Nothing detected → ``none``.
    """
    page_objects = page_objects or []
    locators = locators or []

    if _find_dirs_named(module_root, _SCREENPLAY_DIR_NAMES):
        return "screenplay"

    if page_objects:
        inline_only = not locators and any(
            getattr(p, "has_inline_locators", False) for p in page_objects
        )
        return "inline" if inline_only else "pom"

    if _has_screenplay_code_signal(module_root, language):
        return "screenplay"

    if locators:
        return "pom"

    return "none"


def _slice_source(text: str, line_start: int, line_end: int) -> str:
    """Slice 1-based inclusive [line_start, line_end] and hard-cap the result."""
    lines = text.splitlines()
    snippet = "\n".join(lines[max(0, line_start - 1):line_end])
    if len(snippet) > _EXEMPLAR_CHAR_CAP:
        snippet = snippet[:_EXEMPLAR_CHAR_CAP] + "\n# ... [truncated by qtea] ..."
    return snippet


def _capture_python_exemplars(module_root: Path) -> list[PatternExemplar]:
    # (category, PatternExemplar, size) candidates — keep the smallest per category.
    best: dict[str, tuple[int, PatternExemplar]] = {}
    for n, src in enumerate(iter_python_files(module_root)):
        if n >= _ARCH_SCAN_FILE_CAP:
            break
        tree = parse_file(src)
        if tree is None:
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = relative_posix(src, module_root)
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            category = _screenplay_category(_base_names(node))
            if category is None:
                continue
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            excerpt = _slice_source(text, node.lineno, end)
            size = len(excerpt)
            if category not in best or size < best[category][0]:
                best[category] = (size, PatternExemplar(
                    file=rel, category=category, class_name=node.name,
                    excerpt=excerpt, line_start=node.lineno, line_end=end,
                    dir=rel_dir,
                ))
    return _trim_exemplars([e for _, e in best.values()])


def _capture_ts_exemplars(module_root: Path) -> list[PatternExemplar]:
    """Brace-matched slice of the first class in each Screenplay-signal TS file.

    TS has no AST here (regex-only tier), so this uses a small local brace
    matcher rather than importing Step 8 utilities (wrong dependency direction).
    Recognises both upstream Serenity/JS bases (``implements Task``) and bespoke
    frameworks (``extends BaseTask``) by matching the shared Screenplay category
    keywords against every heritage identifier — never a single hard-coded base
    name — so it stays framework- and stack-agnostic.
    """
    best: dict[str, tuple[int, PatternExemplar]] = {}
    # Capture a class declaration and its heritage clause (everything up to the
    # opening brace of the body): `class Foo extends BaseTask implements X {`.
    class_re = re.compile(
        r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)"
        r"(?P<heritage>[^{;]*)\{",
    )
    ident_re = re.compile(r"[A-Za-z_]\w*")
    n = 0
    seen: set[Path] = set()
    for pat in _TS_SOURCE_GLOBS:
        for src in module_root.glob(pat):
            if n >= _ARCH_SCAN_FILE_CAP:
                break
            if src in seen or not src.is_file() or _skip_path_part(src.parts):
                continue
            seen.add(src)
            n += 1
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = relative_posix(src, module_root)
            rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
            for m in class_re.finditer(text):
                heritage = m.group("heritage")
                if "extends" not in heritage and "implements" not in heritage:
                    continue
                category = _screenplay_category(ident_re.findall(heritage))
                if category is None:
                    continue
                excerpt = _brace_matched_slice(text, m.start())
                size = len(excerpt)
                if category not in best or size < best[category][0]:
                    best[category] = (size, PatternExemplar(
                        file=rel, category=category, class_name=m.group("name"),
                        excerpt=excerpt, dir=rel_dir,
                    ))
    return _trim_exemplars([e for _, e in best.values()])


def _brace_matched_slice(text: str, start: int) -> str:
    """Slice from *start* to the matching close brace, hard-capped."""
    depth = 0
    started = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                snippet = text[start:i + 1]
                break
    else:
        snippet = text[start:start + _EXEMPLAR_CHAR_CAP]
    if len(snippet) > _EXEMPLAR_CHAR_CAP:
        snippet = snippet[:_EXEMPLAR_CHAR_CAP] + "\n// ... [truncated by qtea] ..."
    return snippet


def _trim_exemplars(exemplars: list[PatternExemplar]) -> list[PatternExemplar]:
    """Order by category priority and cap the count."""
    def _rank(e: PatternExemplar) -> int:
        try:
            return _EXEMPLAR_CATEGORY_ORDER.index(e.category)
        except ValueError:
            return len(_EXEMPLAR_CATEGORY_ORDER)
    return sorted(exemplars, key=_rank)[:_EXEMPLAR_MAX]


def capture_pattern_exemplars(
    module_root: Path,
    *,
    pattern: str,
    language: str = "unknown",
) -> list[PatternExemplar]:
    """Capture 1-2 verbatim reusable-unit snippets for the non-POM exemplar lane.

    Returns ``[]`` for pom / inline / none / unknown so the mature POM path is
    byte-identical.
    """
    if pattern != "screenplay":
        return []
    if language == "python":
        return _capture_python_exemplars(module_root)
    if language in ("typescript", "javascript"):
        return _capture_ts_exemplars(module_root)
    return []


def detect_src_directory_layout(
    module_root: Path,
    *,
    page_objects: list[PageObject] | None = None,
    helpers: list[Helper] | None = None,
    language: str = "unknown",
    architecture_pattern: str = "unknown",
) -> SrcDirectoryLayout:
    """Derive where the SUT puts production (non-test) code.

    Strategy (each step independent; failures fall through to fallback):
      1. `package_root` from pyproject.toml / setup.cfg (Python).
      2. `pages_object_dir` / `pages_locators_dir`: scan the filesystem
         directly for directories literally named `object` and `locators`
         (case-insensitive) — locator FILES typically don't define
         `*Page` classes (they hold `*Locators` constant classes), so
         they may not appear in `existing_page_objects`. We can't rely
         on the AST scan to surface them.
      3. As a complementary signal, also look at the common parent of
         actual page-object class files for `pages_object_dir`.
      4. `helpers_dir`: common parent of helper files (the AST helper
         scanner already covers `helpers/` and `tests/utils/`).
      5. Greenfield Python fallback when nothing resolves: build paths
         under `package_root` (or `src/<module>`).

    `convention_source`:
      - `"detected"` when ANY field came from a real on-disk directory.
      - `"fallback"` when only the greenfield Python defaults filled.
      - `"llm_only"` is set later by Tier 3 merge if needed.
    """
    page_objects = page_objects or []
    helpers = helpers or []

    package_root = _detect_python_package_root(module_root) if language == "python" else None

    # Tier-2.5: filesystem scan for `object/` and `locators/` directories.
    # These are the canonical sibling-dirs in a POM-with-extracted-locators
    # SUT (which is what AskBosch and most mature Python+Playwright projects
    # use). Ranking preferences (best → worst):
    #   1. under `src/<pkg>/pages/` — the canonical Python POM location
    #   2. under any `pages/` dir somewhere else (e.g. `app/pages/`)
    #   3. anywhere else
    # Within each tier, prefer paths under `src/` over `tests/` — a SUT that
    # ran a previous qtea codegen may have copies under `tests/pages/...`
    # which must NOT shadow the real src/.
    def _score(d: Path) -> tuple[int, int, int]:
        rel = (
            d.relative_to(module_root).as_posix()
            if d.is_relative_to(module_root)
            else d.as_posix()
        )
        parts = [a.lower() for a in rel.split("/")]
        ancestry = [a.name.lower() for a in d.parents][:5]
        # Tier 1: src/ + pages/ in ancestry → score 0 (best)
        # Tier 2: pages/ in ancestry but NOT under tests/ → score 1
        # Tier 3: under tests/ → score 2 (we don't want codegen-shadow copies)
        # Tier 4: nothing → score 3
        in_src = "src" in parts
        in_tests = "tests" in parts
        near_pages = "pages" in ancestry
        if in_src and near_pages:
            tier = 0
        elif near_pages and not in_tests:
            tier = 1
        elif not in_tests:
            tier = 2
        else:
            tier = 3
        # Tiebreak: shorter relative path wins (more canonical).
        return (tier, len(parts), len(rel))

    def _pick_under_pages(candidate_dirs: list[Path]) -> str | None:
        if not candidate_dirs:
            return None
        ranked = sorted(candidate_dirs, key=_score)
        chosen = ranked[0]
        try:
            rel = chosen.relative_to(module_root).as_posix()
        except ValueError:
            rel = chosen.as_posix()
        return rel

    object_dirs = _find_dirs_named(module_root, {"object", "objects"})
    locator_dirs = _find_dirs_named(module_root, {"locators"})

    pages_object_dir = _pick_under_pages(object_dirs)
    pages_locators_dir = _pick_under_pages(locator_dirs)

    # Complementary signal: if we still don't have pages_object_dir but the
    # AST scanner found page-object class files, derive from their common
    # parent.
    if not pages_object_dir and page_objects:
        po_paths = [
            (p.file or "").replace("\\", "/")
            for p in page_objects
            if _PAGES_OBJECT_HINT_RE.search((p.file or "").replace("\\", "/"))
        ]
        pages_object_dir = _common_parent_posix(po_paths)

    helpers_dir = _common_parent_posix([h.file for h in helpers])

    convention_source = (
        "detected"
        if (pages_object_dir or pages_locators_dir or helpers_dir)
        else "unknown"
    )

    if convention_source == "unknown":
        # Greenfield fallback. ONLY synthesise the POM-shaped `pages/object`
        # + `pages/locators` layout for POM-family patterns. For non-POM
        # patterns (e.g. Screenplay) a `<pkg>/pages/object` path is a fiction
        # that poisons Step 7/8 placement (root cause of run
        # 20260715-075512-f2dbad) — leave the POM dir fields None; the
        # exemplar lane derives placement from `pattern_exemplars[].dir`.
        if language == "python" and architecture_pattern in ("pom", "inline", "unknown"):
            pkg = package_root or f"src/{module_root.name.lower().replace('-', '_')}"
            package_root = package_root or pkg
            pages_object_dir = f"{pkg}/pages/object"
            pages_locators_dir = f"{pkg}/pages/locators"
            helpers_dir = f"{pkg}/helpers"
            convention_source = "fallback"
        else:
            # Non-POM, or non-Python: keep any detected package_root, leave
            # POM dir fields None.
            convention_source = "fallback"

    return SrcDirectoryLayout(
        package_root=package_root,
        pages_object_dir=pages_object_dir,
        pages_locators_dir=pages_locators_dir,
        helpers_dir=helpers_dir,
        convention_source=convention_source,
    )


_PW_CONFIG_EXTS = ("ts", "mts", "cts", "js", "mjs", "cjs")
_TESTID_ATTR_PW_RE = re.compile(r"testIdAttribute\s*:\s*['\"]([^'\"]+)['\"]")
_TESTID_ATTR_PY_RE = re.compile(r"set_test_id_attribute\s*\(\s*['\"]([^'\"]+)['\"]")


def detect_custom_test_id_attribute(module_root: Path) -> str | None:
    """Detect a non-default test-id attribute configured in the SUT.

    Returns the attribute name (e.g. ``"data-test"``) when the SUT explicitly
    overrides Playwright's default ``data-testid``, or ``None`` when the
    default is in effect.
    """
    for ext in _PW_CONFIG_EXTS:
        cfg = module_root / f"playwright.config.{ext}"
        if not cfg.is_file():
            continue
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _TESTID_ATTR_PW_RE.search(text)
        if m:
            val = m.group(1).strip()
            return val if val != "data-testid" else None

    for src in iter_python_files(module_root, contains_hint=b"set_test_id_attribute"):
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _TESTID_ATTR_PY_RE.search(text)
        if m:
            val = m.group(1).strip()
            return val if val != "data-testid" else None

    return None


def detect_module_inventory(sut_root: Path, module_rel: str) -> ModuleInventory:
    """Run Tier 1 (Python AST) + Tier 2 (TS regex) detection on one module."""
    module_root = sut_root / module_rel
    profile = detect_stack_profile(module_root)
    language = _detect_module_language(module_root, profile)
    layout = detect_test_directory_layout(module_root)

    pages: list[PageObject] = []
    helpers: list[Helper] = []
    fixtures: list[Fixture] = []
    locators: list[LocatorClass] = []
    lifecycle_hooks: list[LifecycleHook] = []
    auth = AuthFlow()

    if language == "python":
        pages = scan_python_page_objects(module_root)
        helpers = scan_python_helpers(module_root)
        fixtures = scan_python_fixtures(module_root)
        locators = scan_python_locators(module_root)
        lifecycle_hooks = scan_python_lifecycle_hooks(module_root)
        auth = scan_python_auth_flow(module_root, pages, fixtures)
    elif language in ("typescript", "javascript"):
        pages = scan_ts_page_objects(module_root)
        helpers = scan_ts_helpers(module_root)
        fixtures = scan_ts_fixtures(module_root)
        locators = scan_ts_locators(module_root)
        lifecycle_hooks = scan_ts_lifecycle_hooks(module_root)
        auth = scan_ts_auth_flow(module_root, pages)
    # Other languages: leave empty for Tier 3 (LLM) to fill in.

    # Detect the design pattern BEFORE the src layout so the POM-shaped
    # fallback is suppressed for non-POM SUTs (Screenplay etc.).
    architecture_pattern = detect_architecture_pattern(
        module_root, page_objects=pages, locators=locators, language=language,
    )
    src_layout = detect_src_directory_layout(
        module_root, page_objects=pages, helpers=helpers, language=language,
        architecture_pattern=architecture_pattern,
    )
    pattern_exemplars = capture_pattern_exemplars(
        module_root, pattern=architecture_pattern, language=language,
    )
    custom_test_id = detect_custom_test_id_attribute(module_root)

    name = "sut" if module_rel == "." else Path(module_rel).name
    source = (
        "deterministic"
        if (pages or helpers or fixtures or locators or auth.entry_method)
        else "llm_only"
    )

    return ModuleInventory(
        name=name,
        path=module_rel,
        language=language,
        package_manager=profile.package_manager,
        test_directory_layout=layout,
        src_directory_layout=src_layout,
        existing_page_objects=pages,
        existing_helpers=helpers,
        existing_fixtures=fixtures,
        existing_locators=locators,
        lifecycle_hooks=lifecycle_hooks,
        auth_flow=auth,
        custom_test_id_attribute=custom_test_id,
        architecture_pattern=architecture_pattern,
        pattern_exemplars=pattern_exemplars,
        source=source,
    )


# ---------------------------------------------------------------------------
# Tier 3 merge: parse `## SUT Inventory` YAML blocks from researcher output
# ---------------------------------------------------------------------------


_YAML_BLOCK_RE = re.compile(
    r"^\s*```ya?ml\s*\n(?P<body>.*?)\n```\s*$",
    re.M | re.S,
)
_INVENTORY_HEADER_RE = re.compile(
    r"sut_inventory_module\s*:\s*\n(?P<body>(?:[ \t]+.*\n?)+)",
)


def parse_llm_inventory_yaml(md_text: str) -> list[dict[str, Any]]:
    """Extract `sut_inventory_module:` YAML blocks from researcher markdown.

    We use a deliberately small subset of YAML parsing (top-level key/value
    plus simple lists of dicts) to avoid adding a YAML dependency. Any block
    that doesn't match the strict template is skipped silently — the
    deterministic inventory still holds.
    """
    out: list[dict[str, Any]] = []
    # Look inside fenced ```yaml blocks first; fall back to bare patterns.
    candidates: list[str] = []
    for m in _YAML_BLOCK_RE.finditer(md_text):
        candidates.append(m.group("body"))
    if not candidates:
        # No fenced blocks; scan whole document for the header.
        candidates = [md_text]
    for body in candidates:
        for hit in _INVENTORY_HEADER_RE.finditer(body):
            parsed = _parse_simple_yaml(hit.group("body"))
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML subset: `key: value`, nested `key:` then indented children,
    `- { k: v, ... }` flow-style list items, and `- value` simple list items.

    Returns a plain dict. Indentation-sensitive but tolerant of mixed widths.
    """
    lines = text.splitlines()
    return _parse_block(lines, 0, _leading_indent(lines))[0]


def _leading_indent(lines: list[str]) -> int:
    for ln in lines:
        if ln.strip():
            return len(ln) - len(ln.lstrip())
    return 0


def _parse_block(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        cur_indent = len(ln) - len(ln.lstrip())
        if cur_indent < indent:
            break
        if cur_indent > indent:
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            # Inline value (may be a flow-style list or scalar).
            out[key] = _parse_scalar_or_flow(rest)
            i += 1
        else:
            # Child block.
            i += 1
            if i < len(lines) and lines[i].lstrip().startswith("- "):
                # List value.
                list_items, i = _parse_list(lines, i, cur_indent + 2)
                out[key] = list_items
            else:
                child, i = _parse_block(lines, i, cur_indent + 2)
                out[key] = child
    return out, i


def _parse_list(lines: list[str], i: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        cur_indent = len(ln) - len(ln.lstrip())
        if cur_indent < indent - 2:
            break
        if not stripped.startswith("- "):
            break
        item_body = stripped[2:].strip()
        if item_body.startswith("{") and item_body.endswith("}"):
            out.append(_parse_flow_mapping(item_body))
            i += 1
        elif ":" in item_body and not item_body.startswith("{"):
            # Inline `- key: value` mapping seed.
            seed: dict[str, Any] = {}
            k, _, v = item_body.partition(":")
            seed[k.strip()] = _parse_scalar_or_flow(v.strip()) if v.strip() else None
            i += 1
            # Pull any further child lines.
            child, i = _parse_block(lines, i, cur_indent + 2)
            seed.update(child)
            out.append(seed)
        else:
            out.append(_parse_scalar_or_flow(item_body))
            i += 1
    return out, i


def _parse_flow_mapping(text: str) -> dict[str, Any]:
    inner = text.strip()[1:-1].strip()
    out: dict[str, Any] = {}
    # Split on top-level commas (no nesting support beyond simple lists).
    parts = _split_flow(inner)
    for part in parts:
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = _parse_scalar_or_flow(v.strip())
    return out


def _split_flow(text: str) -> list[str]:
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [p for p in out if p]


def _parse_scalar_or_flow(text: str) -> Any:
    s = text.strip()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_scalar_or_flow(x) for x in _split_flow(inner)]
    if s.startswith("{") and s.endswith("}"):
        return _parse_flow_mapping(s)
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    if s.lower() in ("null", "none", "~"):
        return None
    return s


def _clone_module_inventory(src: ModuleInventory) -> ModuleInventory:
    """Deep copy a ModuleInventory while preserving nested dataclass types."""
    return ModuleInventory(
        name=src.name,
        path=src.path,
        language=src.language,
        package_manager=src.package_manager,
        test_directory_layout=TestDirectoryLayout(
            base_dir=src.test_directory_layout.base_dir,
            convention=src.test_directory_layout.convention,
            subdirs=[
                TestDirSubdir(name=s.name, kind=s.kind, path=s.path)
                for s in src.test_directory_layout.subdirs
            ],
            default_target=src.test_directory_layout.default_target,
        ),
        src_directory_layout=SrcDirectoryLayout(
            package_root=src.src_directory_layout.package_root,
            pages_object_dir=src.src_directory_layout.pages_object_dir,
            pages_locators_dir=src.src_directory_layout.pages_locators_dir,
            helpers_dir=src.src_directory_layout.helpers_dir,
            convention_source=src.src_directory_layout.convention_source,
        ),
        existing_page_objects=[
            PageObject(name=p.name, file=p.file, class_name=p.class_name,
                       methods=list(p.methods), scope=p.scope,
                       import_path=p.import_path)
            for p in src.existing_page_objects
        ],
        existing_helpers=[
            Helper(name=h.name, file=h.file, signature=h.signature, purpose=h.purpose)
            for h in src.existing_helpers
        ],
        existing_fixtures=[
            Fixture(name=f.name, file=f.file, scope=f.scope, yields=f.yields,
                    depends_on=list(f.depends_on))
            for f in src.existing_fixtures
        ],
        existing_locators=[
            LocatorClass(
                name=lc.name, file=lc.file, class_name=lc.class_name,
                constants=[
                    LocatorConstant(name=c.name, selector=c.selector, line=c.line)
                    for c in lc.constants
                ],
                import_path=lc.import_path,
                truncated_count=lc.truncated_count,
            )
            for lc in src.existing_locators
        ],
        lifecycle_hooks=[
            LifecycleHook(
                event=hk.event, file=hk.file,
                framework_construct=hk.framework_construct,
                scope=hk.scope, calls=list(hk.calls),
            )
            for hk in src.lifecycle_hooks
        ],
        auth_flow=AuthFlow(
            type=src.auth_flow.type,
            entry_method=src.auth_flow.entry_method,
            credentials_env_vars=list(src.auth_flow.credentials_env_vars),
            fixture_entry=src.auth_flow.fixture_entry,
            open_method=src.auth_flow.open_method,
        ),
        custom_test_id_attribute=src.custom_test_id_attribute,
        architecture_pattern=src.architecture_pattern,
        pattern_exemplars=[
            PatternExemplar(
                file=e.file, category=e.category, class_name=e.class_name,
                excerpt=e.excerpt, line_start=e.line_start, line_end=e.line_end,
                dir=e.dir,
            )
            for e in src.pattern_exemplars
        ],
        source=src.source,
    )


def merge_llm_inventory(
    deterministic: ModuleInventory,
    llm: dict[str, Any],
) -> ModuleInventory:
    """Per-field merge. Deterministic values win where present; LLM fills gaps."""
    if not llm:
        return deterministic
    out = _clone_module_inventory(deterministic)

    # Scalars: only overwrite if deterministic is unknown / None.
    if (out.language in ("unknown", None)) and llm.get("language"):
        out.language = str(llm["language"])
    if not out.package_manager and llm.get("package_manager"):
        out.package_manager = str(llm["package_manager"])
    if not out.custom_test_id_attribute and llm.get("custom_test_id_attribute"):
        out.custom_test_id_attribute = str(llm["custom_test_id_attribute"])

    # Layout: fill nulls only.
    layout_llm = llm.get("test_directory_layout") or {}
    if isinstance(layout_llm, dict):
        if not out.test_directory_layout.base_dir and layout_llm.get("base_dir"):
            out.test_directory_layout.base_dir = str(layout_llm["base_dir"])
        if out.test_directory_layout.convention == "unknown" and layout_llm.get("convention"):
            out.test_directory_layout.convention = str(layout_llm["convention"])
        if not out.test_directory_layout.default_target and layout_llm.get("default_target"):
            out.test_directory_layout.default_target = str(layout_llm["default_target"])
        llm_subdirs = layout_llm.get("subdirs") or []
        if not out.test_directory_layout.subdirs and isinstance(llm_subdirs, list):
            for item in llm_subdirs:
                if not isinstance(item, dict):
                    continue
                if not item.get("name") or not item.get("path"):
                    continue
                out.test_directory_layout.subdirs.append(TestDirSubdir(
                    name=str(item["name"]),
                    kind=str(item.get("kind", "other")),
                    path=str(item["path"]),
                ))

    # src_directory_layout: same fill-nulls-only policy. When the deterministic
    # tier hit the greenfield fallback path (convention_source=="fallback") AND
    # the LLM proposes real detected paths, prefer the LLM values.
    src_llm = llm.get("src_directory_layout") or {}
    if isinstance(src_llm, dict):
        det_is_fallback = out.src_directory_layout.convention_source == "fallback"
        def _fill(field: str) -> None:
            current = getattr(out.src_directory_layout, field)
            proposed = src_llm.get(field)
            if proposed and (not current or det_is_fallback):
                setattr(out.src_directory_layout, field, str(proposed))
        for f in ("package_root", "pages_object_dir", "pages_locators_dir", "helpers_dir"):
            _fill(f)
        if src_llm.get("convention_source"):
            out.src_directory_layout.convention_source = str(src_llm["convention_source"])
        elif det_is_fallback and any(
            src_llm.get(f) for f in ("pages_object_dir", "pages_locators_dir")
        ):
            out.src_directory_layout.convention_source = "llm_augmented"

    # Lists: append LLM items that don't collide on (name, file).
    def _existing_keys(items: list[Any]) -> set[tuple[str, str]]:
        return {(getattr(i, "name", ""), getattr(i, "file", "")) for i in items}

    for po in llm.get("existing_page_objects") or []:
        if not isinstance(po, dict) or not po.get("name") or not po.get("file"):
            continue
        key = (str(po["name"]), str(po["file"]))
        if key in _existing_keys(out.existing_page_objects):
            continue
        methods = po.get("methods") or []
        out.existing_page_objects.append(PageObject(
            name=str(po["name"]), file=str(po["file"]),
            class_name=str(po.get("class_name") or po["name"]),
            methods=[str(m) for m in methods if isinstance(m, (str, int))],
            scope=str(po.get("scope", "generic")),
            import_path=po.get("import_path"),
        ))

    for h in llm.get("existing_helpers") or []:
        if not isinstance(h, dict) or not h.get("name") or not h.get("file"):
            continue
        key = (str(h["name"]), str(h["file"]))
        if key in _existing_keys(out.existing_helpers):
            continue
        out.existing_helpers.append(Helper(
            name=str(h["name"]), file=str(h["file"]),
            signature=str(h.get("signature", "")),
            purpose=str(h.get("purpose", "")),
        ))

    for f in llm.get("existing_fixtures") or []:
        if not isinstance(f, dict) or not f.get("name") or not f.get("file"):
            continue
        key = (str(f["name"]), str(f["file"]))
        if key in _existing_keys(out.existing_fixtures):
            continue
        depends = f.get("depends_on") or []
        out.existing_fixtures.append(Fixture(
            name=str(f["name"]), file=str(f["file"]),
            scope=str(f.get("scope", "function")),
            yields=f.get("yields"),
            depends_on=[str(x) for x in depends if isinstance(x, (str, int))],
        ))

    # Locator classes: same dedup-by-(name, file) policy. Constants merge
    # by name within a class — LLM-supplied constants only fill gaps the
    # deterministic AST scan didn't see (e.g. constants computed at import
    # time that the literal extractor can't read).
    existing_locator_keys = _existing_keys(out.existing_locators)
    for lc in llm.get("existing_locators") or []:
        if not isinstance(lc, dict) or not lc.get("name") or not lc.get("file"):
            continue
        key = (str(lc["name"]), str(lc["file"]))
        consts_in = lc.get("constants") or []
        const_objs: list[LocatorConstant] = []
        seen_const_names: set[str] = set()
        for c in consts_in:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            cname = str(c["name"])
            if cname in seen_const_names:
                continue
            seen_const_names.add(cname)
            const_objs.append(LocatorConstant(
                name=cname,
                selector=str(c.get("selector", "")),
                line=int(c.get("line") or 0),
            ))
        if key in existing_locator_keys:
            # Class already known deterministically — graft any new constants
            # the LLM surfaced (e.g. dynamic patterns the AST scan missed).
            for existing in out.existing_locators:
                if (existing.name, existing.file) != key:
                    continue
                known = {c.name for c in existing.constants}
                for c in const_objs:
                    if c.name not in known and len(existing.constants) < _LOCATOR_CONSTANT_CAP:
                        existing.constants.append(c)
                break
            continue
        out.existing_locators.append(LocatorClass(
            name=str(lc["name"]), file=str(lc["file"]),
            class_name=str(lc.get("class_name") or lc["name"]),
            constants=const_objs[:_LOCATOR_CONSTANT_CAP],
            import_path=lc.get("import_path"),
            truncated_count=max(0, len(const_objs) - _LOCATOR_CONSTANT_CAP),
        ))

    # Auth flow: fill only unset fields.
    auth_llm = llm.get("auth_flow") or {}
    if isinstance(auth_llm, dict):
        if out.auth_flow.type in ("unknown", None) and auth_llm.get("type"):
            out.auth_flow.type = str(auth_llm["type"])
        if not out.auth_flow.entry_method and auth_llm.get("entry_method"):
            out.auth_flow.entry_method = str(auth_llm["entry_method"])
        if not out.auth_flow.credentials_env_vars:
            envs = auth_llm.get("credentials_env_vars") or []
            out.auth_flow.credentials_env_vars = [str(x) for x in envs if isinstance(x, (str, int))]
        if not out.auth_flow.fixture_entry and auth_llm.get("fixture_entry"):
            out.auth_flow.fixture_entry = str(auth_llm["fixture_entry"])
        if not out.auth_flow.open_method and auth_llm.get("open_method"):
            out.auth_flow.open_method = str(auth_llm["open_method"])

    # Lifecycle hooks: append LLM-supplied hooks that don't collide on
    # (event, file). Deterministic Tier-1/2 covers Python + TS/JS; Java and
    # other stacks arrive only via the LLM (Tier 3).
    existing_hook_keys = {
        (hk.event, hk.file) for hk in out.lifecycle_hooks
    }
    _VALID_HOOK_EVENTS = {"before_all", "after_all", "before_each", "after_each"}
    for hk in llm.get("lifecycle_hooks") or []:
        if not isinstance(hk, dict):
            continue
        event = str(hk.get("event") or "")
        if event not in _VALID_HOOK_EVENTS or not hk.get("file"):
            continue
        key = (event, str(hk["file"]))
        if key in existing_hook_keys:
            continue
        existing_hook_keys.add(key)
        calls = hk.get("calls") or []
        out.lifecycle_hooks.append(LifecycleHook(
            event=event, file=str(hk["file"]),
            framework_construct=str(hk.get("framework_construct", "")),
            scope=hk.get("scope"),
            calls=[str(c) for c in calls if isinstance(c, (str, int))],
        ))

    # Navigation preconditions: LLM-only field, no deterministic producer
    # (unlike hooks, there's no narrow structural region to scan — this is
    # mined from judgment across arbitrarily-shaped test bodies). Append,
    # deduped on (method, requires_call).
    existing_nav_keys = {
        (np_.method, np_.requires_call) for np_ in out.navigation_preconditions
    }
    for np_entry in llm.get("navigation_preconditions") or []:
        if not isinstance(np_entry, dict):
            continue
        method = str(np_entry.get("method") or "")
        requires_call = str(np_entry.get("requires_call") or "")
        if not method or not requires_call:
            continue
        key = (method, requires_call)
        if key in existing_nav_keys:
            continue
        existing_nav_keys.add(key)
        out.navigation_preconditions.append(NavigationPrecondition(
            method=method,
            requires_call=requires_call,
            requires_args_hint=np_entry.get("requires_args_hint"),
            evidence=str(np_entry.get("evidence") or ""),
        ))

    # Update source tag.
    if deterministic.source == "llm_only":
        out.source = "llm_only"
    elif llm:
        out.source = "llm_augmented"

    return out


# ---------------------------------------------------------------------------
# Active module resolution
# ---------------------------------------------------------------------------


def resolve_active_module(
    inventory: SutInventory,
    *,
    explicit: str | None,
    spec_text: str | None = None,
) -> tuple[str | None, str | None]:
    """Return (active_module_name, error_message). On success, error is None."""
    names = [m.name for m in inventory.modules]
    if not names:
        return None, "no modules discovered in SUT"

    if explicit:
        if explicit in names:
            return explicit, None
        return None, (
            f"--module {explicit!r} not found. "
            f"Available modules: {', '.join(names)}"
        )

    if len(names) == 1:
        return names[0], None

    # Heuristic auto-detect from spec text.
    if spec_text:
        spec_low = spec_text.lower()
        scored: list[tuple[int, str]] = []
        for m in inventory.modules:
            score = 0
            if m.name.lower() in spec_low:
                score += 10
            for po in m.existing_page_objects:
                if po.name.lower().replace("page", "") in spec_low:
                    score += 3
            scored.append((score, m.name))
        scored.sort(reverse=True)
        if scored and scored[0][0] > scored[1][0]:
            return scored[0][1], None

    return None, (
        f"multiple modules ({', '.join(names)}); cannot auto-detect target. "
        "Re-run with --module <name>."
    )


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------


def detect_sut_inventory(
    sut_path: Path,
    *,
    module_hint: str | None = None,
    spec_text: str | None = None,
) -> SutInventory:
    """Run the deterministic tiers (1 + 2) and produce a populated `SutInventory`.

    Tier 3 (LLM-augmentation) is applied later in `s06_research.py` after the
    researcher agent has produced its markdown — see `merge_llm_inventory`.

    `module_hint` is the explicit `--module` CLI value; `spec_text` is the
    refined-spec / spec content used for auto-detection when there's no hint
    and more than one module exists.
    """
    inv = SutInventory()
    if not sut_path.exists() or not sut_path.is_dir():
        inv.notes.append(f"sut path does not exist: {sut_path}")
        return inv

    is_mono, signal, module_paths = detect_monorepo(sut_path)
    inv.is_monorepo = is_mono
    inv.monorepo_signal = signal

    for rel in module_paths:
        try:
            inv.modules.append(detect_module_inventory(sut_path, rel))
        except Exception as e:
            log.warning("sut_inventory.module_failed", module=rel, error=str(e))
            inv.modules.append(ModuleInventory(
                name="sut" if rel == "." else Path(rel).name,
                path=rel,
                source="llm_only",
            ))

    active, err = resolve_active_module(inv, explicit=module_hint, spec_text=spec_text)
    inv.active_module = active
    if err and not active:
        inv.notes.append(err)

    log.info(
        "sut_inventory.detected",
        is_monorepo=is_mono,
        modules=[m.name for m in inv.modules],
        active=active,
    )
    return inv


__all__ = [
    "AuthFlow",
    "Fixture",
    "Helper",
    "LifecycleHook",
    "NavigationPrecondition",
    "LocatorClass",
    "LocatorConstant",
    "ModuleInventory",
    "PageObject",
    "SutInventory",
    "TestDirSubdir",
    "TestDirectoryLayout",
    "detect_module_inventory",
    "detect_monorepo",
    "detect_sut_inventory",
    "detect_test_directory_layout",
    "merge_llm_inventory",
    "parse_llm_inventory_yaml",
    "resolve_active_module",
    "scan_python_auth_flow",
    "scan_python_fixtures",
    "scan_python_helpers",
    "scan_python_lifecycle_hooks",
    "scan_python_locators",
    "scan_python_page_objects",
    "scan_ts_auth_flow",
    "scan_ts_lifecycle_hooks",
    "scan_ts_locators",
    "scan_ts_page_objects",
]
