"""Test-file indexer + non-negotiable-rule enforcer used by Step 7.

Scans a directory of generated test files (any of the supported frameworks),
identifies individual test functions, extracts locator-creation calls and TBD
markers, and detects forbidden patterns (XPath, hard waits, page.content,
raw secrets) into a single structured result.

Rule set (single source of truth for the enforcement layer):

  xpath        -> any XPath-flavoured selector or `By.XPATH`/`xpath=` API call
  hard-wait    -> sleep/wait-N calls with a numeric argument
  page-content -> `page.content(` / `await page.content(` style calls
  raw-secret   -> obvious inline credentials (password = "...", token = "...")

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
_TEST_FILE_GLOBS: dict[str, tuple[str, ...]] = {
    "playwright-ts": ("**/*.spec.ts", "**/*.test.ts"),
    "playwright-py": ("**/test_*.py", "**/*_test.py"),
    "pytest": ("**/test_*.py", "**/*_test.py"),
    "cypress": ("**/*.cy.ts", "**/*.cy.js"),
    "selenium-java": ("**/*Test.java", "**/*Tests.java"),
    "selenium-py": ("**/test_*.py", "**/*_test.py"),
    "robot": ("**/*.robot",),
    "jest": ("**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js"),
    "vitest": ("**/*.test.ts", "**/*.test.js"),
    "mocha": ("**/*.test.ts", "**/*.test.js"),
    "wdio": ("**/*.test.ts", "**/*.test.js"),
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

# Forbidden patterns -> rule label.
_VIOLATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # XPath: literal `//tag`-style strings or explicit xpath APIs.
    (
        "xpath",
        re.compile(
            r"""(?P<snippet>(?:By\.XPATH|by_xpath|find_element\s*\(\s*By\.XPATH|xpath\s*=\s*['"]|['"]//[a-zA-Z*\[]|locator\s*\(\s*['"]xpath=))"""
        ),
    ),
    # Hard waits: numeric arg (allow `wait_for_selector` etc.).
    (
        "hard-wait",
        re.compile(
            r"""(?P<snippet>(?:time\.sleep|Thread\.sleep|setTimeout|page\.wait_for_timeout|waitForTimeout|cy\.wait)\s*\(\s*\d+)"""
        ),
    ),
    # AOM only: page.content / page_source.
    (
        "page-content",
        re.compile(
            r"""(?P<snippet>(?:await\s+)?page\.content\s*\(|driver\.page_source\b)"""
        ),
    ),
    # Raw secret heuristic: assignment to a credential-like name with a string literal.
    (
        "raw-secret",
        re.compile(
            r"""(?P<snippet>(?:password|passwd|api_?key|apiKey|secret|token)\s*[:=]\s*['"][^'"\n]{4,}['"])""",
            re.I,
        ),
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

    def as_dict(self) -> dict:
        return {"line": self.line, "raw": self.raw, "context": self.context}


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

    def as_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "line": self.line, "snippet": self.snippet}


@dataclass
class IndexResult:
    framework: str
    test_root: str
    files: list[str]
    tests: list[TestEntry]
    violations: list[Violation]

    def as_dict(self) -> dict:
        return {
            "framework": self.framework,
            "test_root": self.test_root,
            "totals": {
                "files": len(self.files),
                "tests": len(self.tests),
                "tbd_locators": sum(len(t.tbd_markers) for t in self.tests),
            },
            "files": self.files,
            "tests": [t.as_dict() for t in self.tests],
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
    out: list[Path] = []
    seen: set[Path] = set()
    for g in globs:
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


def _scan_tbd(block: str, base_offset: int, file_text: str) -> list[TBDMarker]:
    out: list[TBDMarker] = []
    for m in _TBD_PATTERN.finditer(block):
        absolute = base_offset + m.start()
        out.append(
            TBDMarker(
                line=_line_of(file_text, absolute),
                raw=m.group("raw").strip(),
                context=_snippet_at(file_text, absolute),
            )
        )
    return out


def _scan_tc_refs_and_tags(block: str) -> tuple[list[str], list[str]]:
    refs = [m.group("id") for m in _TC_REF_PATTERN.finditer(block)]
    tags = [m.group("tag") for m in _TAG_PATTERN.finditer(block)]
    # Dedup, preserve order.
    return list(dict.fromkeys(refs)), list(dict.fromkeys(tags))


def _scan_violations(file_text: str, rel_path: str) -> list[Violation]:
    out: list[Violation] = []
    for rule, pat in _VIOLATION_PATTERNS:
        for m in pat.finditer(file_text):
            out.append(
                Violation(
                    rule=rule,
                    file=rel_path,
                    line=_line_of(file_text, m.start()),
                    snippet=_snippet_at(file_text, m.start()),
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

        blocks = _split_test_blocks(text, family)
        if not blocks:
            # Robot frequently has only a single section; treat the whole file as 1 test.
            if family == "_robot":
                blocks = [(1, path.stem, 0, len(text))]
            else:
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
                    tbd_markers=_scan_tbd(block, start, text),
                )
            )

    return IndexResult(
        framework=framework,
        test_root=tests_root.as_posix(),
        files=rel_files,
        tests=entries,
        violations=violations,
    )


def violations_summary(result: IndexResult) -> str:
    if not result.violations:
        return ""
    lines = [f"{len(result.violations)} violation(s):"]
    for v in result.violations[:20]:
        lines.append(f"  [{v.rule}] {v.file}:{v.line}  {v.snippet.strip()[:120]}")
    if len(result.violations) > 20:
        lines.append(f"  ... and {len(result.violations) - 20} more")
    return "\n".join(lines)
