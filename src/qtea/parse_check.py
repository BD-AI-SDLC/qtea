"""Language-native parse gate for Step 8 codegen (Phase B.6.5).

Runs a cheap "does this file tokenise at all" check on every qtea-generated
source file BEFORE the static-check gate (Phase B.6). Complements B.6 by
catching a class of failure the type-checker cannot even reach: the file's
first byte isn't valid for the target language, so tsc/pyright never get a
chance to type-check it.

Motivating incident: run 20260701-114656-9394eb Step 9 aborted because
`qtea_ropa_approval_test.spec.ts` started with `# Stack: typescript+playwright`
— a Python-style comment. Playwright's TS parser refused with
``Unexpected token (1:0)`` and 0 tests ran. B.6 tsc was silently skipped
(``ran=false, exit_code=127``) because tsc wasn't on PATH, so the invalid file
reached Step 9 unchallenged. This gate closes that hole: it uses ``ast.parse``
for Python (stdlib; never skips) and shells to a ladder of language-native
tools for the others.

Design contract:

* Per-language backend ladder (first working backend wins).
* When ALL backends for a required language are absent AND the regex smoke
  check fires a violation, the step fails LOUD with the missing-tool name —
  contrast B.6, where tool-absence is indistinguishable from "no errors
  found" downstream.
* When ALL backends are absent AND the regex smoke check passes, the check
  is marked "degraded" and passes with a WARN log naming the missing tools.
* Regex smoke check catches the most common leak patterns (Python `#`
  header in a `//`-comment language, unclosed fence, leaked prose).
* Env opt-outs: ``QTEA_NO_PARSE_CHECK=1`` (mirrors ``QTEA_NO_STATIC_CHECK=1``),
  ``QTEA_SKIP_PARSE_CHECK=1``.

Result artifact: ``artifacts/step08/parse-check-result.json`` — validated
against ``schemas/parse-check-result.schema.json``.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.test_indexer import Violation

log = get_logger(__name__)


PARSE_ERROR_RULE = "parse-error"

# ---------------------------------------------------------------------------
# File-extension → language mapping
# ---------------------------------------------------------------------------

_LANG_BY_EXT: dict[str, str] = {
    ".py":   "python",
    ".pyi":  "python",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".mts":  "typescript",
    ".cts":  "typescript",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".java": "java",
    ".robot": "robot",
}


def _language_of(path: Path) -> str | None:
    return _LANG_BY_EXT.get(path.suffix.lower())


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ParseFileResult:
    """Per-file outcome. Serialised into ``parse-check-result.json``."""

    file: str          # SUT-relative posix path
    language: str
    backend_used: str  # e.g. "ast.parse", "node --check", "tsc", "regex-smoke"
    ran: bool          # true iff any backend (including regex) produced a verdict
    ok: bool           # true iff the file parses / smoke-check found no issue
    error_line: int | None
    error_message: str | None
    skipped_reason: str | None  # populated when ran=false

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "language": self.language,
            "backend_used": self.backend_used,
            "ran": self.ran,
            "ok": self.ok,
            "error_line": self.error_line,
            "error_message": self.error_message,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class ParseCheckResult:
    """Aggregate outcome of a Phase B.6.5 invocation."""

    ran: bool
    skipped_reason: str | None
    duration_s: float
    files_checked: int
    in_scope_errors: int
    degraded_languages: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    file_results: list[ParseFileResult] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    autofix_attempted: bool = False
    post_fix_errors: int = 0

    def as_dict(self) -> dict:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "duration_s": round(self.duration_s, 3),
            "files_checked": self.files_checked,
            "in_scope_errors": self.in_scope_errors,
            "degraded_languages": sorted(set(self.degraded_languages)),
            "missing_tools": sorted(set(self.missing_tools)),
            "autofix_attempted": self.autofix_attempted,
            "post_fix_errors": self.post_fix_errors,
            "file_results": [f.as_dict() for f in self.file_results],
            "violations": [v.as_dict() for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
#
# Each backend returns a ParseFileResult. On backend-unavailable it returns
# None so the caller can try the next backend in the ladder.


def _run_subprocess(argv: list[str], *, cwd: Path | None = None,
                    timeout_s: int = 30) -> tuple[int, str, str] | None:
    """Return (exit_code, stdout, stderr) or None if the executable isn't found."""
    try:
        result = subprocess.run(
            argv, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout_s}s"
    return result.returncode, result.stdout, result.stderr


def _check_python(path: Path, rel: str) -> ParseFileResult:
    """Python: ast.parse — always available (stdlib)."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return ParseFileResult(
            file=rel, language="python", backend_used="ast.parse",
            ran=True, ok=False, error_line=None,
            error_message=f"read failed: {e}", skipped_reason=None,
        )
    try:
        ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return ParseFileResult(
            file=rel, language="python", backend_used="ast.parse",
            ran=True, ok=False, error_line=e.lineno,
            error_message=f"{e.msg} (line {e.lineno})", skipped_reason=None,
        )
    return ParseFileResult(
        file=rel, language="python", backend_used="ast.parse",
        ran=True, ok=True, error_line=None,
        error_message=None, skipped_reason=None,
    )


# tsc emits: `path.ts(LINE,COL): error TS<code>: <message>`
_TSC_ERROR_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+TS(?P<code>\d+):\s+(?P<msg>.+)$",
    re.MULTILINE,
)

# node --check emits: `path.js:<LINE>\n<snippet>\n^^^^\n<Error>: <message>`
_NODE_ERROR_LINE_RE = re.compile(r":(\d+)\b")


def _check_with_tsc(path: Path, rel: str, language: str) -> ParseFileResult | None:
    """Try `tsc --noEmit --isolatedModules --skipLibCheck <file>`.

    Returns None when tsc isn't on PATH (caller falls through to next backend).
    `--isolatedModules` makes tsc treat each file as an isolated module and
    lifts many cross-file resolution requirements; combined with `--skipLibCheck`
    this makes tsc usable as a pure syntax checker without a tsconfig.
    """
    if not shutil.which("tsc"):
        # Try npx as a secondary entry point (tsc shipped via node_modules).
        npx = shutil.which("npx")
        if not npx:
            return None
        argv = ["npx", "--no-install", "tsc", "--noEmit",
                "--isolatedModules", "--skipLibCheck", "--pretty", "false",
                "--allowJs", str(path)]
    else:
        argv = ["tsc", "--noEmit", "--isolatedModules", "--skipLibCheck",
                "--pretty", "false", "--allowJs", str(path)]
    res = _run_subprocess(argv, cwd=path.parent, timeout_s=45)
    if res is None:
        return None
    exit_code, stdout, stderr = res
    if exit_code == 0:
        return ParseFileResult(
            file=rel, language=language, backend_used="tsc",
            ran=True, ok=True, error_line=None,
            error_message=None, skipped_reason=None,
        )
    # tsc found errors. Only treat as a parse-check failure when the error
    # is a syntax-level one (TS1xxx). Type errors (TS2xxx+) belong to
    # Phase B.6, not us.
    combined = f"{stdout}\n{stderr}"
    for m in _TSC_ERROR_RE.finditer(combined):
        msg = m.group("msg").strip()
        ts_code = m.group("code")
        # Only surface TS1xxx (syntax) errors as parse-check violations.
        # Type errors (TS2xxx+) belong to Phase B.6; letting them bleed
        # into B.6.5 would cause double-fixer invocations. Also surface
        # the canonical "Unexpected token" wording regardless of code.
        if ts_code.startswith("1") or "Unexpected token" in msg:
            return ParseFileResult(
                file=rel, language=language, backend_used="tsc",
                ran=True, ok=False, error_line=int(m.group("line")),
                error_message=f"TS{ts_code}: {msg}"[:280], skipped_reason=None,
            )
    # No syntax-class error surfaced (tsc found only type errors) — treat
    # as parse-check pass; Phase B.6 will pick up the type errors.
    return ParseFileResult(
        file=rel, language=language, backend_used="tsc",
        ran=True, ok=True, error_line=None,
        error_message=None, skipped_reason=None,
    )


def _check_with_node(path: Path, rel: str, language: str) -> ParseFileResult | None:
    """Try `node --check <file>` — pure syntax check, ships with any Node install."""
    node = shutil.which("node")
    if not node:
        return None
    res = _run_subprocess([node, "--check", str(path)], timeout_s=30)
    if res is None:
        return None
    exit_code, stdout, stderr = res
    if exit_code == 0:
        return ParseFileResult(
            file=rel, language=language, backend_used="node --check",
            ran=True, ok=True, error_line=None,
            error_message=None, skipped_reason=None,
        )
    error_output = stderr.strip() or stdout.strip() or f"node exit {exit_code}"
    line_match = _NODE_ERROR_LINE_RE.search(error_output.split("\n", 1)[0])
    line_no = int(line_match.group(1)) if line_match else None
    # Node's error format lists the file:line on the first line, then a
    # snippet, then a caret, then the error class. Grab the last non-empty
    # line as the message.
    lines = [ln.strip() for ln in error_output.splitlines() if ln.strip()]
    msg = lines[-1] if lines else error_output[:200]
    return ParseFileResult(
        file=rel, language=language, backend_used="node --check",
        ran=True, ok=False, error_line=line_no,
        error_message=msg[:280], skipped_reason=None,
    )


def _check_with_javac(path: Path, rel: str) -> ParseFileResult | None:
    """Try `javac -Xlint:none -implicit:none -d <tempdir> <file>`."""
    javac = shutil.which("javac")
    if not javac:
        return None
    with tempfile.TemporaryDirectory(prefix="qtea-parse-check-") as td:
        argv = [javac, "-Xlint:none", "-implicit:none", "-d", td, str(path)]
        res = _run_subprocess(argv, timeout_s=60)
        if res is None:
            return None
        exit_code, stdout, stderr = res
        if exit_code == 0:
            return ParseFileResult(
                file=rel, language="java", backend_used="javac",
                ran=True, ok=True, error_line=None,
                error_message=None, skipped_reason=None,
            )
        # javac error format: `path.java:LINE: error: <message>`
        line_no = None
        msg = stderr.strip() or stdout.strip() or f"javac exit {exit_code}"
        m = re.search(r":(\d+):\s+error:\s+(.+)", msg)
        if m:
            line_no = int(m.group(1))
            msg = m.group(2)
        return ParseFileResult(
            file=rel, language="java", backend_used="javac",
            ran=True, ok=False, error_line=line_no,
            error_message=msg[:280], skipped_reason=None,
        )


# Regex smoke check: catches the highest-frequency leak patterns.
#
# Rule 1: for `.ts/.tsx/.js/.jsx/.java`, line 1 (after leading blanks) must
#   NOT start with a bare `# ` (Python-style comment). Shebangs (`#!`) and
#   TypeScript `#private` fields (which appear inside class bodies, never at
#   line 1) are allowed.
# Rule 2: line 1 must NOT look like unclosed markdown prose ("Here is the
#   file", "```<lang>" left over from a code fence, or a bare " ``` ").
_MARKDOWN_LEAK_RE = re.compile(r"^(```|Here (is|are) the|Below (is|are) the)", re.IGNORECASE)


def _smoke_check_non_python(path: Path, rel: str, language: str,
                            missing: list[str]) -> ParseFileResult:
    """Best-effort regex smoke check when no native backend is available."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for _, ln in zip(range(20), f, strict=False)]
    except OSError as e:
        return ParseFileResult(
            file=rel, language=language, backend_used="regex-smoke",
            ran=True, ok=False, error_line=None,
            error_message=f"read failed: {e}", skipped_reason=None,
        )
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is None:
        return ParseFileResult(
            file=rel, language=language, backend_used="regex-smoke",
            ran=True, ok=False, error_line=1,
            error_message="file is empty", skipped_reason=None,
        )
    first = lines[first_idx].lstrip()
    # Python-style single-hash header on a non-Python file (the run
    # 20260701-114656-9394eb regression).
    if first.startswith("# ") and not first.startswith("#!"):
        return ParseFileResult(
            file=rel, language=language, backend_used="regex-smoke",
            ran=True, ok=False, error_line=first_idx + 1,
            error_message=(
                f"line 1 uses Python-style `#` comment in a "
                f"{language} file — expected `//` (missing tools: "
                f"{', '.join(missing)})"
            ),
            skipped_reason=None,
        )
    if _MARKDOWN_LEAK_RE.match(first):
        return ParseFileResult(
            file=rel, language=language, backend_used="regex-smoke",
            ran=True, ok=False, error_line=first_idx + 1,
            error_message=(
                f"line looks like leaked markdown prose / unclosed code "
                f"fence (missing tools: {', '.join(missing)})"
            ),
            skipped_reason=None,
        )
    return ParseFileResult(
        file=rel, language=language, backend_used="regex-smoke",
        ran=True, ok=True, error_line=None,
        error_message=None, skipped_reason=None,
    )


# ---------------------------------------------------------------------------
# Backend ladder
# ---------------------------------------------------------------------------


def _check_typescript(path: Path, rel: str) -> ParseFileResult:
    """TS ladder: tsc → node → regex-smoke."""
    missing: list[str] = []
    for backend in (_check_with_tsc, _check_with_node):
        res = backend(path, rel, "typescript")
        if res is not None:
            return res
        missing.append(backend.__name__.replace("_check_with_", ""))
    return _smoke_check_non_python(path, rel, "typescript", missing)


def _check_javascript(path: Path, rel: str) -> ParseFileResult:
    """JS ladder: node --check → tsc → regex-smoke."""
    missing: list[str] = []
    for backend in (_check_with_node, _check_with_tsc):
        res = backend(path, rel, "javascript")
        if res is not None:
            return res
        missing.append(backend.__name__.replace("_check_with_", ""))
    return _smoke_check_non_python(path, rel, "javascript", missing)


def _check_java(path: Path, rel: str) -> ParseFileResult:
    """Java ladder: javac → regex-smoke."""
    res = _check_with_javac(path, rel)
    if res is not None:
        return res
    return _smoke_check_non_python(path, rel, "java", ["javac"])


def _check_robot(path: Path, rel: str) -> ParseFileResult:
    """Robot: regex-smoke only. Robot syntax is line-oriented and we don't
    ship a parser dependency for it; the smoke check only catches obvious
    leaked-prose patterns (Python-style headers are legitimate in Robot)."""
    try:
        first_line = ""
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    first_line = line.strip()
                    break
    except OSError as e:
        return ParseFileResult(
            file=rel, language="robot", backend_used="regex-smoke",
            ran=True, ok=False, error_line=None,
            error_message=f"read failed: {e}", skipped_reason=None,
        )
    if _MARKDOWN_LEAK_RE.match(first_line):
        return ParseFileResult(
            file=rel, language="robot", backend_used="regex-smoke",
            ran=True, ok=False, error_line=1,
            error_message="line looks like leaked markdown prose / unclosed fence",
            skipped_reason=None,
        )
    return ParseFileResult(
        file=rel, language="robot", backend_used="regex-smoke",
        ran=True, ok=True, error_line=None,
        error_message=None, skipped_reason=None,
    )


_CHECKERS = {
    "python":     _check_python,
    "typescript": _check_typescript,
    "javascript": _check_javascript,
    "java":       _check_java,
    "robot":      _check_robot,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_parse_check(
    sut_root: Path,
    *,
    qtea_files: set[Path],
) -> ParseCheckResult:
    """Run the parse-check ladder against every qtea-generated file.

    Pure function (subprocess + file I/O only; no LLM). Never raises on
    backend failure — every failure surfaces on a ``ParseFileResult`` or on
    the aggregate ``ParseCheckResult.skipped_reason``.
    """
    started = datetime.now(UTC)

    if os.environ.get("QTEA_SKIP_PARSE_CHECK") == "1":
        return ParseCheckResult(
            ran=False, skipped_reason="env_skip", duration_s=0.0,
            files_checked=0, in_scope_errors=0,
        )
    if os.environ.get("QTEA_NO_PARSE_CHECK") == "1":
        return ParseCheckResult(
            ran=False, skipped_reason="flag_skip", duration_s=0.0,
            files_checked=0, in_scope_errors=0,
        )

    file_results: list[ParseFileResult] = []
    violations: list[Violation] = []
    degraded_languages: set[str] = set()
    missing_tools: set[str] = set()

    for path in sorted(qtea_files):
        if not path.exists() or not path.is_file():
            continue
        language = _language_of(path)
        if language is None:
            continue
        checker = _CHECKERS.get(language)
        if checker is None:
            continue
        try:
            rel = str(path.resolve().relative_to(sut_root)).replace("\\", "/")
        except (ValueError, OSError):
            rel = str(path).replace("\\", "/")

        file_result = checker(path, rel)
        file_results.append(file_result)

        # Track which languages fell back to the regex smoke check — that
        # means no real parser was available. If the smoke check ALSO fired
        # a violation, the caller must fail loud (see gate semantics).
        if file_result.backend_used == "regex-smoke" and language != "robot":
            degraded_languages.add(language)
            if language == "typescript":
                missing_tools.update({"tsc", "node"})
            elif language == "javascript":
                missing_tools.update({"node", "tsc"})
            elif language == "java":
                missing_tools.add("javac")

        if not file_result.ok:
            violations.append(Violation(
                rule=PARSE_ERROR_RULE,
                file=file_result.file,
                line=file_result.error_line or 1,
                snippet=(file_result.error_message or "parse failed")[:280],
                severity="error",
            ))

    duration_s = (datetime.now(UTC) - started).total_seconds()
    result = ParseCheckResult(
        ran=True,
        skipped_reason=None,
        duration_s=duration_s,
        files_checked=len(file_results),
        in_scope_errors=len(violations),
        degraded_languages=sorted(degraded_languages),
        missing_tools=sorted(missing_tools),
        file_results=file_results,
        violations=violations,
    )
    # Emit an audit-trail log so silent-skip becomes impossible: even a
    # fully-passing degraded run leaves a WARN naming the missing tools.
    if degraded_languages:
        log.warning(
            "parse_check.degraded",
            degraded_languages=sorted(degraded_languages),
            missing_tools=sorted(missing_tools),
            in_scope_errors=len(violations),
        )
    return result


def has_degraded_violations(result: ParseCheckResult) -> bool:
    """True iff at least one violation came from the regex-smoke backend
    (i.e. no real parser was available). Callers use this to escalate to a
    hard step failure with a "please install X" message, since we can't be
    sure the regex smoke check is a full substitute for a real parser."""
    if not result.violations:
        return False
    smoke_files = {
        fr.file for fr in result.file_results
        if fr.backend_used == "regex-smoke" and not fr.ok
    }
    return any(v.file in smoke_files for v in result.violations)


def format_for_fixer(result: ParseCheckResult) -> str:
    """Render violations into the same freeform-markdown shape the
    ``codegen-violation-fixer`` agent expects (matches ``static_check.format_for_fixer``)."""
    if not result.violations:
        return ""
    lines = [f"{len(result.violations)} parse error(s) reported by parse-check gate:"]
    for v in result.violations[:50]:
        lines.append(f"  [{v.rule}] {v.file}:{v.line}  {v.snippet.strip()[:200]}")
    if len(result.violations) > 50:
        lines.append(f"  ... and {len(result.violations) - 50} more")
    return "\n".join(lines)
