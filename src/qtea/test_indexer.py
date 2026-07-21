"""Test-file indexer + non-negotiable-rule enforcer used by Step 7.

Scans a directory of generated test files (any of the supported frameworks),
identifies individual test functions, extracts locator-creation calls and TBD
markers, and detects forbidden patterns (XPath, hard waits, page.content,
raw secrets) into a single structured result.

Rule set (single source of truth for the enforcement layer):

  xpath          -> any XPath-flavoured selector or `By.XPATH`/`xpath=` API call
  hard-wait      -> sleep/wait-N calls with a numeric argument
  page-content   -> `page.content(` / `await page.content(` style calls
  raw-secret     -> obvious inline credentials (password = "...", token = "...")
  empty-handler  -> try/except or catch{} blocks with a no-op body
  invalid-escape -> ``\\s``, ``\\d``, ``\\w`` etc. in non-raw Python string literals
                    (SyntaxWarning 3.12+, SyntaxError 3.14+). Tokenize-based so
                    ``r"..."`` / ``rb"..."`` / ``rf"..."`` are correctly exempted.

Each rule carries a `severity` of `error` (hard-rejects Step 8) or
`warning` (logged to violations.log only — advisory mode). Step 8's reject
logic in `s08_codegen.py` filters `violations[]` on severity before deciding
whether to hard-fail.

The indexer is intentionally language-agnostic: per-rule patterns are precise
enough that false positives in non-test files are unlikely, and tests files
already group by extension under `tests/` so scanning is fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from qtea.md_parser import slugify

# ---------------------------------------------------------------------------
# Framework detection (from explicit hint or repo file shape)
# ---------------------------------------------------------------------------

# Map detected_stack values (from Step 6) onto canonical framework labels.
_STACK_MAP = {
    "playwright-ts": "playwright-ts",
    "playwright-py": "playwright-py",
    "pytest": "pytest",
    "jest": "jest",
    "cypress": "cypress",
    "selenium-java": "selenium-java",
    "selenium-py": "selenium-py",
    "robot": "robot",
    "vitest": "vitest",
    "mocha": "mocha",
    "wdio": "wdio",
}

# Files-extension -> default fallback framework when no hint is provided.
_EXT_FALLBACK = {
    ".ts": "playwright-ts",
    ".tsx": "playwright-ts",
    ".js": "jest",
    ".jsx": "jest",
    ".py": "pytest",
    ".java": "selenium-java",
    ".robot": "robot",
}

# Test-file glob predicates per framework.
#
# `qtea_*test*` patterns are deliberately included alongside the standard
# `test_*` / `*_test` / `*.spec` / `*.test` conventions: the Step 8 codegen
# agent prefixes every generated file with `qtea_` (see
# `agents/codegen-rules.md`) to avoid colliding with the
# SUT's own tests when integrating into the SUT's test folder. Without these
# extra globs, files like `qtea_login_test.py` are invisible to the indexer,
# Step 7 reports `tests=0` for the actual test file, and Step 8's TBD detection
# falls back to the locator-module misclassification path.
#
# Canonical Python name is `qtea_<feature>_test.py` (matches pytest's default
# `*_test.py` discovery). The legacy `qteaest_*.py` glob is retained ONLY so
# re-runs over older clones still index; it is NOT pytest-collectable under a
# stock `python_files` config and must not be emitted by new codegen.
_TEST_FILE_GLOBS: dict[str, tuple[str, ...]] = {
    "playwright-ts": (
        "**/*.spec.ts", "**/*.test.ts",
        "**/qtea_*.spec.ts", "**/qtea_*.test.ts",
    ),
    "playwright-py": (
        "**/test_*.py", "**/*_test.py",
        "**/qtea_*_test.py", "**/qteaest_*.py",
    ),
    "pytest": (
        "**/test_*.py", "**/*_test.py",
        "**/qtea_*_test.py", "**/qteaest_*.py",
    ),
    "cypress": (
        "**/*.cy.ts", "**/*.cy.js",
        "**/qtea_*.cy.ts", "**/qtea_*.cy.js",
    ),
    "selenium-java": ("**/*Test.java", "**/*Tests.java", "**/Qtea*Test.java"),
    "selenium-py": (
        "**/test_*.py", "**/*_test.py",
        "**/qtea_*_test.py", "**/qteaest_*.py",
    ),
    "robot": ("**/*.robot", "**/qtea_*.robot"),
    "jest": (
        "**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js",
        "**/qtea_*.test.ts", "**/qtea_*.test.js",
        "**/qtea_*.spec.ts", "**/qtea_*.spec.js",
    ),
    "vitest": (
        "**/*.test.ts", "**/*.test.js",
        "**/qtea_*.test.ts", "**/qtea_*.test.js",
    ),
    "mocha": (
        "**/*.test.ts", "**/*.test.js",
        "**/qtea_*.test.ts", "**/qtea_*.test.js",
    ),
    "wdio": (
        "**/*.test.ts", "**/*.test.js",
        "**/qtea_*.test.ts", "**/qtea_*.test.js",
    ),
}

# Supplementary globs for Page-Object / locator modules that carry TBD markers
# but contain no test functions. Only python-family frameworks use POM patterns.
_SUPPORT_FILE_GLOBS: dict[str, tuple[str, ...]] = {
    "playwright-py": ("**/pages/**/*.py", "**/locators/**/*.py"),
    "pytest": ("**/pages/**/*.py", "**/locators/**/*.py"),
    "selenium-py": ("**/pages/**/*.py", "**/locators/**/*.py"),
    "playwright-ts": ("**/pages/**/*.ts", "**/locators/**/*.ts"),
    "cypress": ("**/pages/**/*.ts", "**/pages/**/*.js"),
}


def resolve_framework(detected_stack: str | None, tests_root: Path) -> str:
    """Pick the canonical framework label.

    Preference: explicit `detected_stack` from Step 6 → first matching extension
    in `tests_root` → 'unknown'.
    """
    if detected_stack and detected_stack in _STACK_MAP:
        return _STACK_MAP[detected_stack]
    if not tests_root.exists():
        return "unknown"
    seen_exts: list[str] = []
    for p in tests_root.rglob("*"):
        if p.is_file():
            seen_exts.append(p.suffix.lower())
    for ext, fw in _EXT_FALLBACK.items():
        if ext in seen_exts:
            return fw
    return "unknown"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Test-function discovery (one per framework family). Captures the test name.
_TEST_DEFINITION_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    # Playwright / Jest / Vitest / Mocha / Cypress: `test('...')` / `it('...')`.
    "_js_ts": [
        re.compile(r"""\b(?:test|it)\s*\(\s*(['"`])(?P<name>(?:\\\1|(?!\1).)+)\1""", re.M),
    ],
    # Python: `def test_<name>(`
    "_python": [
        re.compile(r"^\s*def\s+(?P<name>test_[A-Za-z0-9_]+)\s*\(", re.M),
    ],
    # Java: `public void <name>()` annotated with @Test (heuristic).
    "_java": [
        re.compile(
            r"@Test[^\n]*\n\s*public\s+void\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
            re.M,
        ),
    ],
    # Robot Framework: each non-indented line that is not a section header.
    "_robot": [re.compile(r"^(?P<name>[A-Z][^\n]{2,})\s*$", re.M)],
}

# Locator-creation calls per framework family (yields locator strategy + value).
# Built programmatically so each entry comfortably fits on one line.
def _compile_locator_pat(api: str, value_re: str = r".+?") -> re.Pattern[str]:
    return re.compile(rf"""(?:{api})\s*\(\s*(['"])(?P<value>{value_re})\1""")


_LOCATOR_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("data-testid", _compile_locator_pat(r"getByTestId|get_by_test_id"), "data-testid"),
    ("role", _compile_locator_pat(r"getByRole|get_by_role"), "role"),
    ("label", _compile_locator_pat(r"getByLabel|get_by_label"), "label"),
    ("text", _compile_locator_pat(r"getByText|get_by_text"), "text"),
    ("placeholder", _compile_locator_pat(r"getByPlaceholder|get_by_placeholder"), "placeholder"),
    ("id", _compile_locator_pat(r"locator|\$", r"#[A-Za-z][A-Za-z0-9_\-]*"), "id"),
    (
        "css",
        _compile_locator_pat(r"locator|\$|querySelector", r"(?!//|xpath=).+?"),
        "css",
    ),
    (
        "id",
        re.compile(r"""By\.(?:id|ID)\s*(?:\(|,\s*)\s*(['"])(?P<value>.+?)\1"""),
        "id",
    ),
]

# TBD markers operators leave in tests for Step 8 to resolve.
_TBD_PATTERN = re.compile(
    r"(?P<raw>TBD(?:_LOCATOR)?\b[^\n]*|<<\s*TBD[^\n>]*>>|/\*\s*TBD[^*]*\*/)"
)

# JIT-runtime sentinels — every supported language has a sentinel-producing
# helper that takes the intent as its sole string argument. The TS/JS form
# (`tbd("intent")`) shares the Python pattern verbatim, so one regex covers
# rule 3a (Python) + rule 3b (TS/JS). Java uses `Tbd.of("intent")` and gets
# its own pattern. The intent string IS the marker's `description`; no
# adjacent comment needed in any of the JIT styles.
_TBD_CALL_PATTERN = re.compile(
    r"""\btbd\s*\(\s*(?P<q>['"])(?P<intent>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)""",
    re.DOTALL,
)
_TBD_JAVA_PATTERN = re.compile(
    r"""\bTbd\s*\.\s*of\s*\(\s*(?P<q>['"])(?P<intent>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)""",
    re.DOTALL,
)

# Adjacent `TBD_INTENT: ...` comment that codegen attaches to a TBD marker so
# Step 8a's resolver knows what element to look for. Polyglot matcher: `#`
# (Python / Ruby / shell / Robot) and `//` (JS / TS / Java / C#). The intent
# string runs to end-of-line. Used for non-Python frameworks (rule 3b) where
# the `tbd(...)` runtime helper isn't available.
_INTENT_PATTERN = re.compile(
    r"(?:#|//)\s*TBD_INTENT\s*:\s*(?P<intent>.+?)\s*$",
    re.MULTILINE,
)
_INTENT_WINDOW = 2

# Forbidden patterns -> (rule label, pattern, severity).
#
# `severity`:
#   "error"   — hard-rejects the step (existing behavior for the original 4 rules).
#   "warning" — surfaces in violations.log + tbd-index but does NOT fail the step.
#               Reserved for rules being baselined (e.g. style preferences,
#                join-rules whose false-positive rate is being measured).
_VIOLATION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # XPath: literal `//tag`-style strings or explicit xpath APIs.
    (
        "xpath",
        re.compile(
            r"""(?P<snippet>(?:By\.XPATH|by_xpath|find_element\s*\(\s*By\.XPATH|xpath\s*=\s*['"]|['"]//[a-zA-Z*\[]|locator\s*\(\s*['"]xpath=))"""
        ),
        "error",
    ),
    # Hard waits: only flag the listed callables with a NUMERIC argument.
    # Intentionally NOT flagged (already-allowed polling primitives — see
    # `agents/codegen-violation-fixer.agent.md` §4 for positive guidance to the
    # agent on which to use when):
    #   - page.wait_for_function("…", timeout=N)
    #   - page.wait_for_selector("…", timeout=N)
    #   - page.wait_for_url(...)
    #   - page.expect_response(...)
    #   - expect(locator).to_be_visible(timeout=N) / .toBeVisible({timeout:N})
    #   - expect.poll(callable, timeout=N).to_*(...)
    #   - cy.wait('@aliasName')  — alias-based, not a numeric arg
    # The numeric-arg requirement (`\d+`) keeps every legitimate
    # condition-poll silent and catches only the unconditional sleeps.
    (
        "hard-wait",
        re.compile(
            r"""(?P<snippet>(?:time\.sleep|Thread\.sleep|setTimeout|page\.wait_for_timeout|waitForTimeout|cy\.wait)\s*\(\s*\d+)"""
        ),
        "error",
    ),
    # AOM only: page.content / page_source.
    (
        "page-content",
        re.compile(
            r"""(?P<snippet>(?:await\s+)?page\.content\s*\(|driver\.page_source\b)"""
        ),
        "error",
    ),
    # Raw secret heuristic: assignment to a credential-like name with a string literal.
    (
        "raw-secret",
        re.compile(
            r"""(?P<snippet>(?:password|passwd|api_?key|apiKey|secret|token)\s*[:=]\s*['"][^'"\n]{4,}['"])""",
            re.I,
        ),
        "error",
    ),
    # Empty exception handler — exception-swallowing across stacks.
    # Mirrors the Step 9 heal-gate `_EMPTY_HANDLER_PATTERNS` in
    # `s09_execute.py`: catches `except X: pass`, `try { } catch { }`,
    # `try { } catch (e) { }` with no-op body. Promoted from heal-only to
    # codegen-side so write-time defects don't ship.
    (
        "empty-handler",
        re.compile(
            r"""(?P<snippet>except\b[^:]*:\s*(?:#[^\n]*)?\n\s*pass\b)""",
            re.MULTILINE,
        ),
        "error",
    ),
    (
        "empty-handler",
        re.compile(r"""(?P<snippet>catch\s*(?:\([^)]*\))?\s*\{\s*\})"""),
        "error",
    ),
    # Dangerous code: os.system, subprocess, eval, exec, __import__, importlib
    # in generated test files. Prevents prompt-injection → RCE via codegen.
    (
        "dangerous-code",
        re.compile(
            r"""(?P<snippet>(?:os\.system|subprocess\.(?:run|Popen|call|check_output|check_call)|(?<!\w)eval\s*\(|(?<!\w)exec\s*\(|__import__\s*\(|importlib\.(?:import_module|util\.spec_from_file_location)))""",
        ),
        "error",
    ),
    # NOTE: `invalid-escape` is NOT in this list. A line-regex scan can't
    # distinguish raw strings (`r"\s+"` is valid Python) from non-raw, so it
    # produces false positives the violation-fixer cannot satisfy. The rule
    # is enforced by `_scan_invalid_escape_python` (tokenize-based) for
    # Python-family frameworks only — see that function for details.
    #
    # Assertion-fidelity advisory (Change 4c). Bare `assert` on Playwright
    # objects when the auto-retrying `expect()` API is available — these
    # tests pass on transient state and miss the polling guarantee. Ships
    # as severity=warning while we baseline the false-positive rate against
    # custom POM helpers that return plain Python types.
    #
    # Boolean-returning methods (is_visible, etc.) are commonly used in
    # truthy form (`assert btn.is_visible()`) — comparator is optional.
    (
        "bare-assert-where-expect-available",
        re.compile(
            r"""(?P<snippet>assert\s+[A-Za-z_][A-Za-z0-9_.\[\]()]*\.(?:is_visible|is_hidden|is_enabled|is_disabled|is_checked)\s*\([^)]*\))"""
        ),
        "warning",
    ),
    # Value-returning methods require a comparator (otherwise the call is
    # likely a side-effect, not an assertion target).
    (
        "bare-assert-where-expect-available",
        re.compile(
            r"""(?P<snippet>assert\s+[A-Za-z_][A-Za-z0-9_.\[\]()]*\.(?:text_content|inner_text|input_value|get_attribute|count)\s*\([^)]*\)\s*(?:==|!=|in\b|is\b))"""
        ),
        "warning",
    ),
    (
        "bare-assert-where-expect-available",
        re.compile(
            r"""(?P<snippet>assert\s+page\.url\s*(?:==|!=))"""
        ),
        "warning",
    ),
    (
        "bare-assert-where-expect-available",
        re.compile(
            r"""(?P<snippet>assert\s+page\.title\s*\(\s*\)\s*(?:==|!=))"""
        ),
        "warning",
    ),
]

# Pattern for `// @tc TC-LOGIN-001` style refs and tag annotations.
_TC_REF_PATTERN = re.compile(r"@tc\s+(?P<id>TC-[A-Za-z0-9\-_]+)", re.I)
_TAG_PATTERN = re.compile(r"@tag\s+(?P<tag>[A-Za-z0-9_\-]+)", re.I)


# ---------------------------------------------------------------------------
# Data classes (mirror the JSON schema)
# ---------------------------------------------------------------------------


@dataclass
class LocatorCandidate:
    raw: str
    strategy: str  # see schema enum
    value: str | None = None
    line: int | None = None

    def as_dict(self) -> dict:
        return {"raw": self.raw, "strategy": self.strategy, "value": self.value, "line": self.line}


@dataclass
class TBDMarker:
    line: int
    raw: str
    context: str | None = None
    description: str | None = None
    test_function: str | None = None

    def as_dict(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "context": self.context,
            "description": self.description,
            "test_function": self.test_function,
        }


@dataclass
class TestEntry:
    id: str
    name: str
    file: str
    line: int | None = None
    status: str = "pending"
    tags: list[str] = field(default_factory=list)
    tc_refs: list[str] = field(default_factory=list)
    locator_candidates: list[LocatorCandidate] = field(default_factory=list)
    tbd_markers: list[TBDMarker] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "status": self.status,
            "tags": self.tags,
            "tc_refs": self.tc_refs,
            "locator_candidates": [c.as_dict() for c in self.locator_candidates],
            "tbd_markers": [m.as_dict() for m in self.tbd_markers],
        }


@dataclass
class Violation:
    rule: str
    file: str
    line: int
    snippet: str
    severity: str = "error"  # "error" hard-rejects Step 8; "warning" advises only.

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "snippet": self.snippet,
            "severity": self.severity,
        }


@dataclass
class SupportFileEntry:
    """A file that contains TBD markers but is NOT a test function file
    (e.g. page object, locators module, helper, fixture). Kept separate
    from `TestEntry` so `tbd-index.json` honestly distinguishes
    "test function file" from "support file with TBDs". Step 8 patches
    TBDs in both `tests[]` and `support_files[]` uniformly."""

    name: str
    file: str
    kind: str = "other"  # "locators" | "page_object" | "helper" | "fixture" | "other"
    tbd_markers: list[TBDMarker] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "kind": self.kind,
            "tbd_markers": [m.as_dict() for m in self.tbd_markers],
        }


def _classify_support_kind(rel_path: str) -> str:
    """Derive a `SupportFileEntry.kind` label from the file's path."""
    low = rel_path.lower().replace("\\", "/")
    if "/locators/" in low or low.endswith("_locators.py") or "locators." in low:
        return "locators"
    if "/object/" in low or "/page_objects/" in low or low.endswith("_page.py") \
            or "/pages/" in low:
        return "page_object"
    if "/fixtures/" in low or low.endswith("/conftest.py") or "fixture" in low:
        return "fixture"
    if "/helpers/" in low or "/utils/" in low or low.endswith("_helper.py"):
        return "helper"
    return "other"


@dataclass
class IndexResult:
    framework: str
    test_root: str
    files: list[str]
    tests: list[TestEntry]
    violations: list[Violation]
    support_files: list[SupportFileEntry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "framework": self.framework,
            "test_root": self.test_root,
            "totals": {
                "files": len(self.files),
                # `tests` retained as legacy alias of `total_tests` so older
                # consumers that key on `totals.tests` keep working.
                "tests": len(self.tests),
                "total_tests": len(self.tests),
                "total_support_files": len(self.support_files),
                # `tbd_locators` counts markers across BOTH tests AND support
                # files so Step 8's apply-rate gate sees the true total.
                "tbd_locators": (
                    sum(len(t.tbd_markers) for t in self.tests)
                    + sum(len(s.tbd_markers) for s in self.support_files)
                ),
            },
            "files": self.files,
            "tests": [t.as_dict() for t in self.tests],
            "support_files": [s.as_dict() for s in self.support_files],
            "violations": [v.as_dict() for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Core scanning
# ---------------------------------------------------------------------------


def _family_for(framework: str) -> str:
    if framework in ("playwright-ts", "cypress", "jest", "vitest", "mocha", "wdio"):
        return "_js_ts"
    if framework in ("playwright-py", "pytest", "selenium-py"):
        return "_python"
    if framework == "selenium-java":
        return "_java"
    if framework == "robot":
        return "_robot"
    return "_js_ts"


def _iter_test_files(framework: str, root: Path) -> list[Path]:
    globs = _TEST_FILE_GLOBS.get(framework, ("**/*",))
    support = _SUPPORT_FILE_GLOBS.get(framework, ())
    out: list[Path] = []
    seen: set[Path] = set()
    for g in globs + support:
        for p in root.glob(g):
            if p.is_file() and p not in seen:
                seen.add(p)
                out.append(p)
    return sorted(out)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _snippet_at(text: str, idx: int, length: int = 80) -> str:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    if end == -1:
        end = len(text)
    return text[start:end][:length]


def _split_test_blocks(text: str, family: str) -> list[tuple[int, str, int, int]]:
    """Return [(line, name, start_idx, end_idx)] sorted by position.

    The `start_idx` is rewound to include leading comment lines (which often
    carry `@tc`/`@tag` annotations) up to the previous test block or the start
    of the file.
    """
    patterns = _TEST_DEFINITION_PATTERNS[family]
    matches: list[tuple[int, str, int]] = []
    for pat in patterns:
        for m in pat.finditer(text):
            name = m.group("name")
            if family == "_robot" and name.startswith("***"):
                continue
            matches.append((m.start(), name, _line_of(text, m.start())))
    matches.sort()
    blocks: list[tuple[int, str, int, int]] = []
    prev_end = 0
    for i, (start, name, line) in enumerate(matches):
        # Rewind start over contiguous comment / decorator / blank lines so
        # leading `@tc`/`@tag` annotations are attached to the right test.
        rewound = start
        line_start = text.rfind("\n", 0, rewound) + 1
        while line_start > prev_end:
            preceding = text[text.rfind("\n", 0, line_start - 1) + 1 : line_start]
            stripped = preceding.strip()
            if not stripped:
                line_start = text.rfind("\n", 0, line_start - 1) + 1
                continue
            if stripped.startswith(("//", "#", "/*", "*", "@")):
                line_start = text.rfind("\n", 0, line_start - 1) + 1
                continue
            break
        block_start = max(prev_end, line_start)
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        # End the previous block before the new start; rewind only affects
        # current block's prefix capture, not the next prev_end pointer.
        blocks.append((line, name, block_start, end))
        prev_end = end
    return blocks


def _scan_locators(block: str, base_offset: int, file_text: str) -> list[LocatorCandidate]:
    out: list[LocatorCandidate] = []
    for label, pat, strategy in _LOCATOR_PATTERNS:
        for m in pat.finditer(block):
            value = m.group("value")
            absolute = base_offset + m.start()
            out.append(
                LocatorCandidate(
                    raw=_snippet_at(file_text, absolute),
                    strategy=strategy or label,
                    value=value,
                    line=_line_of(file_text, absolute),
                )
            )
    return out


def _find_intent_near(file_text: str, marker_line: int) -> str | None:
    """Search ±`_INTENT_WINDOW` lines around `marker_line` for a `TBD_INTENT: ...`
    comment. Returns the intent text (stripped) or None.

    Codegen attaches an adjacent comment so Step 8a has the locator's semantic
    intent without re-deriving it. The window is intentionally narrow — a TBD
    marker without a nearby intent line is treated as legacy (no description).
    """
    all_lines = file_text.splitlines()
    if not all_lines:
        return None
    idx = marker_line - 1  # 1-based -> 0-based
    start = max(0, idx - _INTENT_WINDOW)
    end = min(len(all_lines), idx + _INTENT_WINDOW + 1)
    for i in range(start, end):
        m = _INTENT_PATTERN.search(all_lines[i])
        if m:
            return m.group("intent").strip()
    return None


def _scan_tbd(
    block: str,
    base_offset: int,
    file_text: str,
    *,
    test_function: str | None = None,
) -> list[TBDMarker]:
    out: list[TBDMarker] = []
    # Track which lines we already produced markers for so the same physical
    # `tbd("…")` call isn't double-counted by both patterns.
    seen_lines: set[int] = set()

    # JIT-runtime styles. `tbd("intent")` covers Python (rule 3a) + TS/JS
    # (rule 3b); `Tbd.of("intent")` covers Java (rule 3c). Both produce
    # markers whose `description` IS the intent — no adjacent comment needed.
    for pattern in (_TBD_CALL_PATTERN, _TBD_JAVA_PATTERN):
        for m in pattern.finditer(block):
            absolute = base_offset + m.start()
            line_no = _line_of(file_text, absolute)
            if line_no in seen_lines:
                continue
            intent = (m.group("intent") or "").strip()
            if not intent:
                continue
            out.append(
                TBDMarker(
                    line=line_no,
                    raw=block[m.start():m.end()].strip()[:120],
                    context=_snippet_at(file_text, absolute),
                    description=intent,
                    test_function=test_function,
                )
            )
            seen_lines.add(line_no)

    # Legacy style: bare `TBD_LOCATOR` + adjacent `TBD_INTENT:` comment.
    for m in _TBD_PATTERN.finditer(block):
        absolute = base_offset + m.start()
        line_no = _line_of(file_text, absolute)
        if line_no in seen_lines:
            continue
        out.append(
            TBDMarker(
                line=line_no,
                raw=m.group("raw").strip(),
                context=_snippet_at(file_text, absolute),
                description=_find_intent_near(file_text, line_no),
                test_function=test_function,
            )
        )
    # Stable sort by line for deterministic output (both patterns may
    # interleave on a file containing both styles).
    out.sort(key=lambda m: m.line)
    return out


def _scan_tc_refs_and_tags(block: str) -> tuple[list[str], list[str]]:
    refs = [m.group("id") for m in _TC_REF_PATTERN.finditer(block)]
    tags = [m.group("tag") for m in _TAG_PATTERN.finditer(block)]
    # Dedup, preserve order.
    return list(dict.fromkeys(refs)), list(dict.fromkeys(tags))


def _is_in_comment(file_text: str, match_start: int) -> bool:
    """True if the match position is inside a comment.

    Detects three comment styles:
      - single-line ``#`` (Python/YAML/Ruby/shell) on the same line before match
      - single-line ``//`` (JS/TS/Java/Go/C-family) on the same line before match
      - block ``/* … */`` (JS/TS/Java/C) — either on the same line (a `/*` that
        opens before match without a matching `*/` in between) OR spanning
        multiple lines (an open `/*` at some earlier point in the file with
        no intervening `*/` between it and match_start).

    Block-comment support was added specifically for Phase B.5.5's inline
    `/* was: '<original xpath>' */` breadcrumbs — without it the rewriter's
    own reference comments would re-trigger the `[xpath]` gate. Triple-quoted
    Python strings are still NOT handled (rare case, and the closing `\"\"\"`
    would require a lexer to detect reliably).

    String-literal parsing is still deliberately skipped — treating
    ``"// not a comment"`` as a comment is a missed-violation false negative
    on a contrived snippet, while NOT doing the check causes real false
    positives on legitimate documentation comments that mention banned APIs
    by name (e.g. the standard header ``# never use page.content()``).
    """
    line_start = file_text.rfind("\n", 0, match_start) + 1
    prefix = file_text[line_start:match_start]
    if "#" in prefix:
        return True
    if "//" in prefix:
        return True

    # Same-line `/* … */` block comment.
    open_star = prefix.rfind("/*")
    if open_star != -1 and prefix.rfind("*/", open_star) == -1:
        return True

    # Multi-line block comment: the most recent `/*` before match_start
    # not yet closed by a `*/`.
    open_star_file = file_text.rfind("/*", 0, match_start)
    if open_star_file != -1:
        close_star_file = file_text.rfind("*/", open_star_file, match_start)
        if close_star_file == -1:
            return True
    return False


def _has_xpath_exempt_marker(file_text: str, match_start: int) -> bool:
    """True when the match is covered by a ``qtea-xpath-exempt:`` marker.

    Phase B.5.5's deterministic rewriter (``qtea.xpath_rewriter``) stamps
    this marker on xpath entries it CANNOT safely translate. The marker
    suppresses the ``[xpath]`` violation for that specific line so the
    quality gate doesn't kill the whole step over a handful of unfixable
    legacy predicates (``parent::``, ``ancestor::``, complex nested unions).

    Coverage rules (intentionally scoped so the marker can't silence xpath
    elsewhere in the file):
      - the SAME line as the match carries ``qtea-xpath-exempt`` (typical for
        the container-migration output where the marker and the offending
        assignment share a line via inline comment), OR
      - the immediately-PRECEDING non-blank line carries the marker.
    """
    # Same-line check.
    line_start = file_text.rfind("\n", 0, match_start) + 1
    line_end = file_text.find("\n", match_start)
    if line_end == -1:
        line_end = len(file_text)
    current_line = file_text[line_start:line_end]
    if "qtea-xpath-exempt" in current_line:
        return True

    # Preceding non-blank line check.
    scan_end = line_start
    while scan_end > 0:
        prev_line_end = scan_end - 1  # the '\n' terminator of the prior line
        prev_line_start = file_text.rfind("\n", 0, prev_line_end) + 1
        prev = file_text[prev_line_start:prev_line_end]
        if prev.strip():
            return "qtea-xpath-exempt" in prev
        scan_end = prev_line_start
    return False


def _scan_violations(file_text: str, rel_path: str) -> list[Violation]:
    out: list[Violation] = []
    for rule, pat, severity in _VIOLATION_PATTERNS:
        for m in pat.finditer(file_text):
            if _is_in_comment(file_text, m.start()):
                continue
            if rule == "xpath" and _has_xpath_exempt_marker(file_text, m.start()):
                continue
            out.append(
                Violation(
                    rule=rule,
                    file=rel_path,
                    line=_line_of(file_text, m.start()),
                    snippet=_snippet_at(file_text, m.start()),
                    severity=severity,
                )
            )
    return out


# ---------------------------------------------------------------------------
# AST-based: zero-assertions check (Python+pytest only)
# ---------------------------------------------------------------------------

# Method-call names that count as assertions for the zero-assertions rule.
# Includes Playwright `expect(...).<method>()`, pytest.raises, and unittest-
# style assertEqual/assertTrue/etc. via name match.
_ASSERTION_CALL_NAMES: frozenset[str] = frozenset({
    "raises",  # pytest.raises (context manager)
    "warns", "deprecated_call",  # pytest helpers
})


def _function_has_assertion(node) -> bool:
    """True iff the AST function body contains at least one assert / expect /
    raises / should call. Walks all descendants (so asserts inside loops,
    helper-call return values, etc. count)."""
    import ast as _ast

    for child in _ast.walk(node):
        if isinstance(child, _ast.Assert):
            return True
        if isinstance(child, _ast.Call):
            func = child.func
            # `expect(...)` call form (Playwright's sync_api.expect /
            # async_api.expect — bare Name).
            if isinstance(func, _ast.Name) and func.id == "expect":
                return True
            # `obj.method(...)` form: assertEqual / assertTrue / should*.
            if isinstance(func, _ast.Attribute):
                name = func.attr
                if name.startswith("assert") or name.startswith("should") \
                        or name in _ASSERTION_CALL_NAMES:
                    return True
                # `.expect(...)` chain (e.g. `await expect(...).to_have_text(...)`).
                if name == "expect":
                    return True
        # `with pytest.raises(...)` form — wraps a Call we already catch.
        if isinstance(child, _ast.With):
            for item in child.items:
                expr = item.context_expr
                if isinstance(expr, _ast.Call):
                    func = expr.func
                    if isinstance(func, _ast.Attribute) and func.attr == "raises":
                        return True
                    if isinstance(func, _ast.Name) and func.id == "raises":
                        return True
    return False


def _function_has_opt_out_marker(node) -> bool:
    """True iff the AST function carries ``@pytest.mark.qtea_setup``."""
    import ast as _ast

    for deco in node.decorator_list:
        # `@pytest.mark.qtea_setup` -> Attribute(Attribute(Name("pytest"), "mark"), "qtea_setup")
        attr = deco
        if isinstance(attr, _ast.Call):
            attr = attr.func
        if isinstance(attr, _ast.Attribute) and attr.attr == "qtea_setup":
            return True
    return False


def _is_get_by_role_call(node, role: str | tuple[str, ...]) -> bool:
    """True iff `node` is `<obj>.get_by_role("<role>", ...)` where role
    matches the given name (or one of the names in a tuple)."""
    import ast as _ast

    if not isinstance(node, _ast.Call):
        return False
    func = node.func
    if not isinstance(func, _ast.Attribute) or func.attr != "get_by_role":
        return False
    if not node.args:
        return False
    arg0 = node.args[0]
    if not isinstance(arg0, _ast.Constant) or not isinstance(arg0.value, str):
        return False
    needed = (role,) if isinstance(role, str) else role
    return arg0.value in needed


def _has_preceding_combobox_open(
    statements, target_lineno: int,
) -> bool:
    """Scan `statements` (a function body) for a `.click()` on a
    `get_by_role("combobox"|"listbox")` that occurs at a line BEFORE
    `target_lineno`. Returns True when found."""
    import ast as _ast

    for stmt in statements:
        if stmt.lineno >= target_lineno:
            continue
        for sub in _ast.walk(stmt):
            if not isinstance(sub, _ast.Call):
                continue
            func = sub.func
            if not isinstance(func, _ast.Attribute) or func.attr != "click":
                continue
            # `<expr>.click()` — `<expr>` should be (or contain) a
            # get_by_role("combobox"|"listbox") call.
            target = func.value
            for w in _ast.walk(target):
                if _is_get_by_role_call(w, ("combobox", "listbox")):
                    return True
    return False


def _scan_interaction_patterns(
    text: str, rel_path: str, framework: str,
) -> list[Violation]:
    """Two narrow AST-based interaction-pattern checks:

      ``combobox-without-open``: ``get_by_role("option", ...)`` referenced
      without a preceding ``.click()`` on a combobox/listbox trigger in the
      same function.

      ``popup-assert-on-original-page``: ``expect(page).to_have_url(...)``
      called inside a ``with page.expect_popup() as ...:`` block where the
      asserted ``page`` is the outer page variable (should be the popup).
    """
    import ast as _ast

    if framework not in ("pytest", "playwright-py", "selenium-py"):
        return []
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return []

    out: list[Violation] = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue

        # Rule: combobox-without-open. Walk the function body; for each
        # get_by_role("option", ...) usage, require a preceding combobox
        # click in the same function.
        for node in _ast.walk(fn):
            if not _is_get_by_role_call(node, "option"):
                continue
            if _has_preceding_combobox_open(fn.body, node.lineno):
                continue
            out.append(
                Violation(
                    rule="combobox-without-open",
                    file=rel_path,
                    line=node.lineno,
                    snippet=(
                        "get_by_role(\"option\", ...) called without a "
                        "preceding .click() on a get_by_role(\"combobox\") "
                        "or get_by_role(\"listbox\") trigger in the same "
                        "function. Open the dropdown first."
                    ),
                    severity="error",
                )
            )

        # Rule: popup-assert-on-original-page. Find every `with
        # page.expect_popup() as <popup>:` statement; inside the with-body,
        # flag `expect(page).to_have_url(...)` calls where `page` is the
        # outer page variable (the one expect_popup was called on).
        for node in _ast.walk(fn):
            if not isinstance(node, (_ast.With, _ast.AsyncWith)):
                continue
            for item in node.items:
                expr = item.context_expr
                if not isinstance(expr, _ast.Call):
                    continue
                func = expr.func
                if not isinstance(func, _ast.Attribute) or func.attr != "expect_popup":
                    continue
                # Extract the outer page variable name (e.g. `page` in
                # `page.expect_popup()`).
                outer = func.value
                if not isinstance(outer, _ast.Name):
                    continue
                outer_name = outer.id
                # Walk the with-body for offending expect(outer).to_have_url.
                for sub in _ast.walk(node):
                    if not isinstance(sub, _ast.Call):
                        continue
                    f2 = sub.func
                    if not isinstance(f2, _ast.Attribute):
                        continue
                    if f2.attr not in ("to_have_url", "to_have_title"):
                        continue
                    # `<expect(<arg>)>.<to_have_url>(...)` → the receiver of
                    # to_have_url should be a Call to `expect` with our outer.
                    recv = f2.value
                    if not isinstance(recv, _ast.Call):
                        continue
                    rf = recv.func
                    if not (isinstance(rf, _ast.Name) and rf.id == "expect"):
                        continue
                    if not recv.args:
                        continue
                    a0 = recv.args[0]
                    if isinstance(a0, _ast.Name) and a0.id == outer_name:
                        out.append(
                            Violation(
                                rule="popup-assert-on-original-page",
                                file=rel_path,
                                line=sub.lineno,
                                snippet=(
                                    f"expect({outer_name}).{f2.attr}(...) called "
                                    f"inside `with {outer_name}.expect_popup() "
                                    f"as <popup>:` — the assertion should "
                                    f"target the popup page, not the outer "
                                    f"page that triggered it."
                                ),
                                severity="error",
                            )
                        )
    return out


_INVALID_ESCAPE_RE = re.compile(r"(?<!\\)\\[sdwSDWpP]")


def _scan_invalid_escape_python(text: str, rel_path: str, framework: str) -> list[Violation]:
    """Flag ``\\s``/``\\d``/``\\w`` etc. in non-raw Python string literals.

    Uses ``tokenize`` so raw strings (``r"…"``, ``rb"…"``, ``rf"…"``) are
    correctly exempted — the previous line-regex scan re-flagged the
    raw-string fix as still invalid, an unsatisfiable rule that wedged the
    codegen-violation-fixer in a retry loop. SyntaxWarning in Python 3.12+,
    SyntaxError in 3.14+.
    """
    import io
    import tokenize as _tok

    if framework not in ("pytest", "playwright-py", "selenium-py"):
        return []

    out: list[Violation] = []
    try:
        tokens = list(_tok.generate_tokens(io.StringIO(text).readline))
    except (_tok.TokenizeError, IndentationError, SyntaxError):
        # Other gates (pyright, AST parse in zero-assertion check) surface
        # the underlying syntax problem; don't double-report here.
        return out

    file_lines = text.splitlines()
    fstart_t = getattr(_tok, "FSTRING_START", None)
    fmid_t = getattr(_tok, "FSTRING_MIDDLE", None)
    fend_t = getattr(_tok, "FSTRING_END", None)
    in_raw_fstring = False

    def _emit(snippet_src: str, base_line: int) -> None:
        for hit in _INVALID_ESCAPE_RE.finditer(snippet_src):
            line_offset = snippet_src[: hit.start()].count("\n")
            line = base_line + line_offset
            line_text = (
                file_lines[line - 1]
                if 0 < line <= len(file_lines)
                else snippet_src
            )
            out.append(
                Violation(
                    rule="invalid-escape",
                    file=rel_path,
                    line=line,
                    snippet=line_text[:120],
                    severity="error",
                )
            )

    for tok in tokens:
        if fstart_t is not None and tok.type == fstart_t:
            prefix = re.match(r"^[a-zA-Z]*", tok.string).group(0).lower()
            in_raw_fstring = "r" in prefix
            continue
        if fend_t is not None and tok.type == fend_t:
            in_raw_fstring = False
            continue
        if fmid_t is not None and tok.type == fmid_t:
            if not in_raw_fstring:
                _emit(tok.string, tok.start[0])
            continue
        if tok.type == _tok.STRING:
            prefix = re.match(r"^[bBrRuUfF]*", tok.string).group(0).lower()
            if "r" in prefix:
                continue
            _emit(tok.string, tok.start[0])
    return out


def _scan_zero_assertion_tests(
    text: str, rel_path: str, framework: str,
) -> list[Violation]:
    """For Python pytest-family stacks, parse the file and flag every
    ``def test_*`` function whose body contains no assertion AND no
    ``@pytest.mark.qtea_setup`` opt-out marker."""
    import ast as _ast

    if framework not in ("pytest", "playwright-py", "selenium-py"):
        return []
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        # Preflight's AST check reports this defect separately; suppress here
        # to avoid duplicate noise.
        return []

    out: list[Violation] = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        if _function_has_opt_out_marker(node):
            continue
        if _function_has_assertion(node):
            continue
        out.append(
            Violation(
                rule="zero-assertions",
                file=rel_path,
                line=node.lineno,
                snippet=(
                    f"def {node.name}(...) has no assert / expect / raises / "
                    f"should call. Add an assertion or apply "
                    f"@pytest.mark.qtea_setup to opt out."
                ),
                severity="error",
            )
        )
    return out


# JS/TS zero-assertion scan (finding 9). Playwright-ts / jest / vitest / mocha
# / cypress had NO assertion enforcement — an `expect()`-less spec passed the
# quality gate as a meaningless green. Best-effort, token-based, error-tier,
# with a `qtea-setup` opt-out mirroring @pytest.mark.qtea_setup. Deliberately
# does NOT strip comments/strings: that biases toward UNDER-detection
# (a commented-out expect is ignored) rather than false-reds on real tests.
_JSTS_TEST_DECL_RE = re.compile(r"\b(?:test|it)\s*(?:\.\s*(?P<method>\w+)\s*)?\(")
_JSTS_ASSERT_RE = re.compile(r"\bexpect\s*\(|\.\s*should\s*[\.\(]|\bassert(?:\.\w+)?\s*\(")
_JSTS_SETUP_OPT_OUT = re.compile(r"qtea[-:]setup", re.IGNORECASE)
_JSTS_SKIP_DECL_RE = re.compile(r"\b(?:test|it)\s*\.\s*(?:skip|fixme|todo)\s*\(")
# `test.<method>(` forms that are NOT executable test blocks and therefore
# legitimately carry no assertion: lifecycle hooks (Playwright/Jest/Mocha use
# `test.beforeEach` etc.), grouping (`describe`), in-test step grouping
# (`step`), and config (`use`, `slow`). Excluding these keeps the gate from
# being structurally unpassable on any hook-bearing spec. Real test blocks
# (`test(`, `it(`) and `test.only(` deliberately stay IN scope — an
# assertion-less `test.only()` is a genuine bug.
_JSTS_NON_TEST_METHODS = frozenset({
    "beforeEach", "afterEach", "beforeAll", "afterAll", "before", "after",
    "describe", "step", "use", "slow",
})


def _find_balanced_paren(s: str, open_idx: int) -> int:
    """Index of the `)` matching the `(` at ``open_idx``, or -1."""
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _scan_zero_assertion_tests_jsts(
    text: str, rel_path: str, framework: str,
) -> list[Violation]:
    """Flag JS/TS ``test(...)`` / ``it(...)`` blocks with no assertion.

    An `expect()`-less spec would otherwise pass the Step-8 quality gate and
    run green in Step 9 while verifying nothing (finding 9). Opt out with a
    ``qtea-setup`` marker (comment or title) for legitimate setup/navigation
    smoke tests, mirroring the Python ``@pytest.mark.qtea_setup`` escape.
    """
    if _family_for(framework) != "_js_ts":
        return []
    out: list[Violation] = []
    for m in _JSTS_TEST_DECL_RE.finditer(text):
        # Lifecycle hooks / grouping / config (`test.beforeEach(`, `describe(`,
        # `test.step(`, ...) are not executable test blocks — never require an
        # assertion. Excluding them is why hook-bearing specs stay passable.
        if (m.group("method") or "") in _JSTS_NON_TEST_METHODS:
            continue
        open_idx = m.end() - 1
        close = _find_balanced_paren(text, open_idx)
        if close == -1:
            continue
        span = text[open_idx : close + 1]
        if _JSTS_SETUP_OPT_OUT.search(span):
            continue
        # `test.skip(...)` etc. are intentionally not executed — skip.
        if _JSTS_SKIP_DECL_RE.match(text[m.start() : m.end()]):
            continue
        if _JSTS_ASSERT_RE.search(span):
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append(
            Violation(
                rule="zero-assertions",
                file=rel_path,
                line=line,
                snippet=(
                    "test/it block has no expect()/should/assert call. Add an "
                    "assertion or mark it with a `qtea-setup` comment to opt out."
                ),
                severity="error",
            )
        )
    return out


# Escape-hatch scan (finding 26). The violation-fixer decides pass/fail on the
# type/parse checker's error COUNT — which any of these hatches zeroes without
# fixing the defect: `# type: ignore` / `@ts-nocheck` silence the type gate,
# and the skip/only family removes a test from execution entirely (evading even
# Step 9). qtea's own generated tests must never carry them. Scoped to
# qtea-generated files so the SUT's own legitimate skips are untouched.
_ESCAPE_HATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("type-ignore", re.compile(r"#\s*type:\s*ignore\b")),
    ("pyright-ignore", re.compile(r"#\s*pyright:\s*ignore\b")),
    ("ts-nocheck", re.compile(r"//\s*@ts-nocheck\b")),
    ("ts-ignore", re.compile(r"//\s*@ts-(?:ignore|expect-error)\b")),
    ("pytest-skip", re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\s*\(")),
    ("test-only", re.compile(r"\b(?:it|test|describe|context)\s*\.\s*only\s*\(")),
    ("test-skip", re.compile(r"\b(?:it|test|describe|context)\s*\.\s*(?:skip|todo|fixme)\s*\(")),
    ("java-disabled", re.compile(r"@Disabled\b|@Ignore\b")),
)


def _is_qtea_generated_file(rel_path: str) -> bool:
    base = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    return base.startswith("qtea_") or base.startswith("Qtea")


def _scan_escape_hatches(text: str, rel_path: str) -> list[Violation]:
    """Flag gate-silencing / test-skipping escape hatches in GENERATED tests.

    These let the violation-fixer clear the type/parse gate (or evade Step 9
    execution) without fixing the defect — a false-green channel (finding 26).
    Only fires on qtea-generated files so the SUT's own tests are untouched.
    Comments/strings are not stripped: err toward under-detection over a
    false-red on the SUT's own code (which we don't scan anyway).
    """
    if not _is_qtea_generated_file(rel_path):
        return []
    out: list[Violation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pat in _ESCAPE_HATCH_PATTERNS:
            if pat.search(line):
                out.append(
                    Violation(
                        rule=f"escape-hatch-{name}",
                        file=rel_path,
                        line=line_no,
                        snippet=(
                            f"generated test uses a gate-silencing / "
                            f"test-skipping hatch ({name!r}): "
                            f"{line.strip()[:120]}. Fix the underlying defect "
                            f"instead of suppressing the gate."
                        ),
                        severity="error",
                    )
                )
    return out


def _id_for(rel_path: str, name: str, occurrence: int) -> str:
    base = slugify(f"{Path(rel_path).stem}-{name}")
    return f"T-{base}" if occurrence == 0 else f"T-{base}-{occurrence + 1}"


# ---------------------------------------------------------------------------
# pom-assertion rule (RCA-D): assertions must live in TEST methods, never in
# page-object support files. Path-scoped + method-aware — severity depends on
# whether the enclosing method was added by qtea codegen (per the plan's
# `missing_methods`) or pre-existed on the SUT's own POM.
# ---------------------------------------------------------------------------

# Path fragments that identify a page-object support file. Matched against
# the POSIX-style relative path (leading + trailing `/` on both sides so that
# `pages` at the top of the tree still catches).
_POM_PATH_FRAGMENTS: tuple[str, ...] = (
    "/pages/", "/pageobjects/", "/page_objects/", "/page-objects/",
    "/pom/", "/poms/",
)

# Assertion-shaped call patterns. Kept intentionally narrow — false positives
# on this rule (which can fail an agent-authored method) are worse than
# occasional under-detection (the pre-existing warning tier catches those).
_POM_ASSERTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexpect\s*\("),                     # Playwright / Jest / Vitest
    re.compile(r"^\s*assert\b", re.MULTILINE),        # Python bare assert
    re.compile(r"\bassertThat\s*\("),                 # AssertJ / Hamcrest
    re.compile(r"\bAssertions\s*\.\s*assert\w+\("),   # JUnit 5 / TestNG
    re.compile(r"\.should\s*\("),                     # Cypress chainable
)

# Method-header patterns per family. Reused shape from codegen_reconcile —
# each captures the method name in group 1. `_JS_METHOD_HEAD_RE_MULTILINE`
# is our own copy scoped to indented (class-body) methods only.
_JS_METHOD_HEAD_RE_LOCAL = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*"
    r"(?:(?:<[^<>]*>)?\s*\(|=\s*(?:async\s+)?\()",
    re.MULTILINE,
)
_PY_METHOD_HEAD_RE_LOCAL = re.compile(
    r"^[ \t]+(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(",
    re.MULTILINE,
)
_JAVA_METHOD_HEAD_RE_LOCAL = re.compile(
    r"^[ \t]*(?:public|private|protected)\s+"
    r"(?:(?:static|final|abstract|synchronized|native|default)\s+)*"
    r"(?:<[^<>]*>\s+)?"
    r"(?:void|[\w$][\w$.]*(?:<[^<>]*>)?(?:\[\])*)\s+"
    r"([\w$]+)\s*\(",
    re.MULTILINE,
)


def _looks_like_pom_file(rel_path: str) -> bool:
    """True when the file's path indicates a page-object module."""
    p = "/" + rel_path.replace("\\", "/").strip("/") + "/"
    return any(frag in p for frag in _POM_PATH_FRAGMENTS)


def _method_head_re_for(rel_path: str) -> re.Pattern[str] | None:
    """Pick the right method-header regex based on the file's extension."""
    lower = rel_path.lower()
    if lower.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        return _JS_METHOD_HEAD_RE_LOCAL
    if lower.endswith(".py"):
        return _PY_METHOD_HEAD_RE_LOCAL
    if lower.endswith(".java"):
        return _JAVA_METHOD_HEAD_RE_LOCAL
    return None


def _enclosing_method_name(
    text: str, offset: int, head_re: re.Pattern[str],
) -> str | None:
    """Return the name of the method whose body contains ``offset``, or
    None if the position is at module scope (outside any method)."""
    last_name: str | None = None
    for m in head_re.finditer(text):
        if m.start() > offset:
            break
        last_name = m.group(1)
    return last_name


def _scan_pom_assertions(
    text: str, rel_path: str,
    *,
    agent_authored_methods: set[str] | None = None,
) -> list[Violation]:
    """Flag every ``expect()``/``assert``-shaped call inside a page-object
    support file. Assertions belong in TEST methods, not POMs.

    Severity split:

    - **error**: the enclosing method name is in ``agent_authored_methods``
      (the current plan's ``missing_methods``) — this is code qtea just
      wrote and MUST comply. Hard-fails Step 8.
    - **warning**: the enclosing method is pre-existing SUT code (or the
      enclosing method can't be determined). Logged to violations.log for
      migration triage without breaking the build.
    """
    if not _looks_like_pom_file(rel_path):
        return []
    head_re = _method_head_re_for(rel_path)
    if head_re is None:
        return []
    agent_methods = agent_authored_methods or set()

    out: list[Violation] = []
    seen: set[tuple[int, str]] = set()  # dedup by (line, method-name)
    for pat in _POM_ASSERTION_PATTERNS:
        for m in pat.finditer(text):
            line = _line_of(text, m.start())
            method_name = _enclosing_method_name(text, m.start(), head_re)
            key = (line, method_name or "")
            if key in seen:
                continue
            seen.add(key)
            snippet_head = _snippet_at(text, m.start(), length=80).strip()
            if method_name and method_name in agent_methods:
                severity = "error"
                msg = (
                    f"{method_name}(...) contains assertion in POM. "
                    f"Assertions belong in test methods, not page objects "
                    f"(RCA-D). Rewrite as a getter/probe returning the raw "
                    f"value; move the assertion to the test."
                )
            else:
                severity = "warning"
                msg = (
                    f"{(method_name or '<module>')}(...) contains assertion "
                    f"in POM (pre-existing SUT code; advisory only). Consider "
                    f"migrating to a getter + test-side assertion."
                )
            out.append(Violation(
                rule="pom-assertion",
                file=rel_path,
                line=line,
                snippet=f"{msg} [{snippet_head}]",
                severity=severity,
            ))
    return out


def index_tests(
    tests_root: Path,
    *,
    framework: str,
    agent_authored_methods: set[str] | None = None,
) -> IndexResult:
    """Walk `tests_root`, return a populated IndexResult.

    ``agent_authored_methods`` — set of method names that qtea codegen
    just added to POM files (the union of
    ``code-modification-plan.missing_methods[*].name`` across all POMs).
    Enables the ``pom-assertion`` rule to distinguish assertions the
    agent wrote (error) from pre-existing SUT code (warning). Pass
    ``None`` to skip the rule entirely — used by legacy callers that
    don't have a current plan in scope.

    Errors during file I/O are surfaced as violations with rule=`raw-secret`-
    style noise are NOT swallowed here; callers should treat any returned
    violation as a hard failure for Step 7's enforcement contract.
    """
    family = _family_for(framework)
    files = _iter_test_files(framework, tests_root) if tests_root.exists() else []
    rel_files: list[str] = []
    entries: list[TestEntry] = []
    support_entries: list[SupportFileEntry] = []
    violations: list[Violation] = []
    counts: dict[tuple[str, str], int] = {}

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(tests_root).as_posix()
        rel_files.append(rel)

        violations.extend(_scan_violations(text, rel))
        # zero-assertions + interaction-pattern + invalid-escape checks are
        # Python-only (AST / tokenize based). Skipped silently on other stacks.
        violations.extend(_scan_zero_assertion_tests(text, rel, framework))
        # JS/TS analogue of the zero-assertion gate (finding 9) — an
        # expect()-less spec would otherwise ship as a meaningless green.
        violations.extend(_scan_zero_assertion_tests_jsts(text, rel, framework))
        # Gate-silencing / test-skipping escape hatches in generated files
        # (finding 26): type:ignore, ts-nocheck, pytest.skip/xfail, it.only, ...
        violations.extend(_scan_escape_hatches(text, rel))
        violations.extend(_scan_interaction_patterns(text, rel, framework))
        violations.extend(_scan_invalid_escape_python(text, rel, framework))
        # pom-assertion rule (RCA-D) — only runs when the caller passed
        # the plan's authored-method set. Path-scoped internally so
        # test files themselves are skipped.
        if agent_authored_methods is not None:
            violations.extend(_scan_pom_assertions(
                text, rel,
                agent_authored_methods=agent_authored_methods,
            ))

        blocks = _split_test_blocks(text, family)
        if not blocks:
            # Robot frequently has only a single section; treat the whole file as 1 test.
            if family == "_robot":
                blocks = [(1, path.stem, 0, len(text))]
            else:
                # File matched the support-file glob (page object / locators /
                # helper / fixture) AND has no test functions. If it carries
                # TBD markers, route it into `support_files[]` so consumers can
                # tell "1 support file with 13 TBDs" apart from "1 test function
                # with 13 TBDs". Step 8's patcher reads both lists.
                tbd_markers = _scan_tbd(text, 0, text)
                if tbd_markers:
                    support_entries.append(
                        SupportFileEntry(
                            name=path.stem,
                            file=rel,
                            kind=_classify_support_kind(rel),
                            tbd_markers=tbd_markers,
                        )
                    )
                continue

        for line, name, start, end in blocks:
            block = text[start:end]
            tc_refs, tags = _scan_tc_refs_and_tags(block)
            key = (rel, name)
            occurrence = counts.get(key, 0)
            counts[key] = occurrence + 1
            entries.append(
                TestEntry(
                    id=_id_for(rel, name, occurrence),
                    name=name,
                    file=rel,
                    line=line,
                    status="pending",
                    tags=tags,
                    tc_refs=tc_refs,
                    locator_candidates=_scan_locators(block, start, text),
                    tbd_markers=_scan_tbd(block, start, text, test_function=name),
                )
            )

    return IndexResult(
        framework=framework,
        test_root=tests_root.as_posix(),
        files=rel_files,
        tests=entries,
        support_files=support_entries,
        violations=violations,
    )


def violations_summary(result: IndexResult) -> str:
    if not result.violations:
        return ""
    errors = [v for v in result.violations if v.severity == "error"]
    warnings = [v for v in result.violations if v.severity == "warning"]
    parts: list[str] = []
    if errors:
        parts.append(f"{len(errors)} error(s):")
        for v in errors[:20]:
            parts.append(f"  [{v.rule}] {v.file}:{v.line}  {v.snippet.strip()[:120]}")
        if len(errors) > 20:
            parts.append(f"  ... and {len(errors) - 20} more")
    if warnings:
        if parts:
            parts.append("")
        parts.append(f"{len(warnings)} warning(s):")
        for v in warnings[:20]:
            parts.append(f"  [{v.rule}] {v.file}:{v.line}  {v.snippet.strip()[:120]}")
        if len(warnings) > 20:
            parts.append(f"  ... and {len(warnings) - 20} more")
    return "\n".join(parts)


def blocking_violations(result: IndexResult) -> list[Violation]:
    """Return only the violations that should hard-fail Step 8 (severity=error)."""
    return [v for v in result.violations if v.severity == "error"]
