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
  invalid-escape -> ``\\s``, ``\\d``, ``\\w`` etc. in non-raw Python strings (SyntaxWarning 3.12+, SyntaxError 3.14+)

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

from worca_t.md_parser import slugify

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
# `worca_*test*` patterns are deliberately included alongside the standard
# `test_*` / `*_test` / `*.spec` / `*.test` conventions: the Step 8 codegen
# agent prefixes every generated file with `worca_` (see
# `agents/codegen-rules.md`) to avoid colliding with the
# SUT's own tests when integrating into the SUT's test folder. Without these
# extra globs, files like `worca_login_test.py` are invisible to the indexer,
# Step 7 reports `tests=0` for the actual test file, and Step 8's TBD detection
# falls back to the locator-module misclassification path.
#
# Canonical Python name is `worca_<feature>_test.py` (matches pytest's default
# `*_test.py` discovery). The legacy `worca_test_*.py` glob is retained ONLY so
# re-runs over older clones still index; it is NOT pytest-collectable under a
# stock `python_files` config and must not be emitted by new codegen.
_TEST_FILE_GLOBS: dict[str, tuple[str, ...]] = {
    "playwright-ts": (
        "**/*.spec.ts", "**/*.test.ts",
        "**/worca_*.spec.ts", "**/worca_*.test.ts",
    ),
    "playwright-py": (
        "**/test_*.py", "**/*_test.py",
        "**/worca_*_test.py", "**/worca_test_*.py",
    ),
    "pytest": (
        "**/test_*.py", "**/*_test.py",
        "**/worca_*_test.py", "**/worca_test_*.py",
    ),
    "cypress": (
        "**/*.cy.ts", "**/*.cy.js",
        "**/worca_*.cy.ts", "**/worca_*.cy.js",
    ),
    "selenium-java": ("**/*Test.java", "**/*Tests.java", "**/Worca*Test.java"),
    "selenium-py": (
        "**/test_*.py", "**/*_test.py",
        "**/worca_*_test.py", "**/worca_test_*.py",
    ),
    "robot": ("**/*.robot", "**/worca_*.robot"),
    "jest": (
        "**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js",
        "**/worca_*.test.ts", "**/worca_*.test.js",
        "**/worca_*.spec.ts", "**/worca_*.spec.js",
    ),
    "vitest": (
        "**/*.test.ts", "**/*.test.js",
        "**/worca_*.test.ts", "**/worca_*.test.js",
    ),
    "mocha": (
        "**/*.test.ts", "**/*.test.js",
        "**/worca_*.test.ts", "**/worca_*.test.js",
    ),
    "wdio": (
        "**/*.test.ts", "**/*.test.js",
        "**/worca_*.test.ts", "**/worca_*.test.js",
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
    # Python invalid escape sequence — \s, \d, \w etc. in non-raw strings.
    # SyntaxWarning in 3.12+, becomes SyntaxError in 3.14+.
    (
        "invalid-escape",
        re.compile(r"""(?P<snippet>(?<!\\)\\[sdwSDWpP])"""),
        "error",
    ),
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
    """True if the match position is inside a single-line comment.

    Walks back from ``match_start`` to the previous newline (or start of file)
    and looks for an unescaped ``#`` (Python/YAML/Ruby/shell) or ``//``
    (JS/TS/Java/Go/C-family) comment marker. We deliberately do not parse
    string literals — the cost of treating ``"// not a comment"`` as a
    comment is a missed-violation false negative on a contrived snippet,
    while the cost of NOT doing this check is real false positives on
    legitimate documentation comments that mention banned APIs by name
    (e.g. the standard header ``# never use page.content()``).

    Block comments (``/* ... */``, triple-quoted Python strings) are not
    handled here; matches inside those will still flag. Acceptable tradeoff:
    the upside is bounded (only the most common comment style is honored)
    and the rules are intended to catch executable code, which is rarely
    nested inside block comments.
    """
    line_start = file_text.rfind("\n", 0, match_start) + 1
    prefix = file_text[line_start:match_start]
    if "#" in prefix:
        return True
    return "//" in prefix


def _scan_violations(file_text: str, rel_path: str) -> list[Violation]:
    out: list[Violation] = []
    for rule, pat, severity in _VIOLATION_PATTERNS:
        for m in pat.finditer(file_text):
            if _is_in_comment(file_text, m.start()):
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
    """True iff the AST function carries ``@pytest.mark.worca_setup``."""
    import ast as _ast

    for deco in node.decorator_list:
        # `@pytest.mark.worca_setup` -> Attribute(Attribute(Name("pytest"), "mark"), "worca_setup")
        attr = deco
        if isinstance(attr, _ast.Call):
            attr = attr.func
        if isinstance(attr, _ast.Attribute) and attr.attr == "worca_setup":
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


def _scan_zero_assertion_tests(
    text: str, rel_path: str, framework: str,
) -> list[Violation]:
    """For Python pytest-family stacks, parse the file and flag every
    ``def test_*`` function whose body contains no assertion AND no
    ``@pytest.mark.worca_setup`` opt-out marker."""
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
                    f"@pytest.mark.worca_setup to opt out."
                ),
                severity="error",
            )
        )
    return out


def _id_for(rel_path: str, name: str, occurrence: int) -> str:
    base = slugify(f"{Path(rel_path).stem}-{name}")
    return f"T-{base}" if occurrence == 0 else f"T-{base}-{occurrence + 1}"


def index_tests(tests_root: Path, *, framework: str) -> IndexResult:
    """Walk `tests_root`, return a populated IndexResult.

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
        # zero-assertions + interaction-pattern checks are AST-based and only
        # meaningful for the Python family. Skipped silently on other stacks.
        violations.extend(_scan_zero_assertion_tests(text, rel, framework))
        violations.extend(_scan_interaction_patterns(text, rel, framework))

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
