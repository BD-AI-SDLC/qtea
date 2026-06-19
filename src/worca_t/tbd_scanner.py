"""Scanner for `tbd("intent")` / `Tbd.of("intent")` sentinel calls.

The Step 8 codegen agents emit TBD sentinels for every unresolved UI locator
so the Step 9 JIT resolver can bind them at runtime against the live AOM.
This module enumerates those sentinels from generated source so:

  - Step 8 Phase D can score intent quality before tests ever run.
  - Step 9 / Step 10 can audit which intents made it to runtime.
  - The post-Phase-D review gate can show the human reviewer file:line for
    every WARN/FAIL intent and rewrite them in place after edit.

The function is intentionally tiny and pure: take some file/dir paths, return
a flat list of `TbdIntent` records. No I/O beyond reading files. No logging.
Callers control which files are in scope.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Language = Literal["python", "typescript", "javascript", "java"]


@dataclass(frozen=True)
class TbdIntent:
    """A single TBD-sentinel call-site discovered in generated source."""

    file: Path
    """SUT-relative path when ``scan_tbd_intents`` was given ``sut_root``."""

    line: int
    """1-based line number of the call-site."""

    constant_name: str | None
    """The LHS name when the sentinel is on the right of an assignment
    (e.g. ``LOGIN_BUTTON = tbd("sign in button")``). ``None`` for inline
    calls like ``page.locator(tbd("submit")).click()``."""

    intent: str
    """The intent string the agent emitted between the parens."""

    language: Language


# --- Language extension map ------------------------------------------------

_EXT_TO_LANG: dict[str, Language] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
}


# --- Call-site patterns ----------------------------------------------------
#
# Each pattern captures: optional leading-LHS name (constant_name), then the
# intent string. Backslashes inside the intent string are accepted but the
# overall match stops at the first non-escaped matching quote. The intent
# string itself MUST be a literal (`"…"`) — variable-passed intents are
# nonsensical for the JIT resolver (the resolver needs the literal text to
# query the AOM).

# Python: `NAME = tbd("intent")` or bare `tbd("intent")`.
_PY_PATTERN = re.compile(
    r"""
    (?:                             # optional LHS:
        ^                           #   line start
        \s*                         #   indent
        (?P<lhs>[A-Z][A-Z0-9_]*)    #   ALL_CAPS constant name
        \s*=\s*                     #   =
    )?
    \btbd\s*\(                      # tbd(
    \s*
    (?P<quote>['"])                 # opening quote
    (?P<intent>(?:\\.|(?!(?P=quote)).)*?)  # intent body (lazy, escape-aware)
    (?P=quote)                      # matching close quote
    \s*\)
    """,
    re.VERBOSE,
)

# TS/JS: `const NAME = tbd("...")`, `const NAME = worca.tbd("...")`,
# or bare `tbd("...")` / `worca.tbd("...")`. Also matches template literals
# delimited by backticks (the JIT resolver accepts them; mark them as
# language-native).
_TS_PATTERN = re.compile(
    r"""
    (?:
        (?:const|let|var)\s+
        (?P<lhs>[A-Z][A-Z0-9_]*)
        \s*=\s*
    )?
    \b(?:worca\.)?tbd\s*\(
    \s*
    (?P<quote>['"`])
    (?P<intent>(?:\\.|(?!(?P=quote)).)*?)
    (?P=quote)
    \s*\)
    """,
    re.VERBOSE,
)

# Java: `Tbd.of("intent")`. The LHS form is
# `public static final By NAME = Tbd.of("intent");` — capture the constant.
_JAVA_PATTERN = re.compile(
    r"""
    (?:
        (?:public\s+|private\s+|protected\s+|static\s+|final\s+|\w+\s+)+
        (?P<lhs>[A-Z][A-Z0-9_]*)
        \s*=\s*
    )?
    \bTbd\.of\s*\(
    \s*
    "(?P<intent>(?:\\.|[^"])*?)"
    \s*\)
    """,
    re.VERBOSE,
)


_PATTERNS: dict[Language, re.Pattern[str]] = {
    "python": _PY_PATTERN,
    "typescript": _TS_PATTERN,
    "javascript": _TS_PATTERN,
    "java": _JAVA_PATTERN,
}


# --- Comment / string-literal stripping ------------------------------------
#
# Pragmatic line-based pass: drop content after a `#` (Python) or `//`
# (TS/JS/Java) provided the marker isn't inside a string literal. We DON'T
# attempt full lexical analysis — the rare case of a `tbd(...)` call buried
# inside a string literal or a multi-line docstring is harmless because the
# codegen agents are forbidden from doing that. The price of catching that
# edge case is a full tokenizer per language, which isn't worth it.

_PY_COMMENT_RE = re.compile(
    r"""(?P<keep>(?:[^#'"\n]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')*)#.*"""
)
_C_LINE_COMMENT_RE = re.compile(
    r"""(?P<keep>(?:[^/'"`\n]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/[^/])*)//.*"""
)


def _strip_line_comment(line: str, language: Language) -> str:
    pat = _PY_COMMENT_RE if language == "python" else _C_LINE_COMMENT_RE
    m = pat.match(line)
    if m is None:
        return line
    return m.group("keep")


# --- Block-comment stripping (Java / TS / JS) ------------------------------
#
# Triple-quoted Python docstrings are NOT stripped — they're rare enough as a
# source of false positives and the regex cost is high. Java/TS/JS block
# comments `/* ... */` are stripped because Java POMs frequently use Javadoc
# blocks that may contain example call-sites.

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_block_comments(text: str, language: Language) -> str:
    if language == "python":
        return text
    # Replace each block comment with its newline count preserved so 1-based
    # line numbers in the stripped text still match the original file's lines.
    return _BLOCK_COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), text,
    )


# --- Public API ------------------------------------------------------------


def detect_language(path: Path) -> Language | None:
    return _EXT_TO_LANG.get(path.suffix.lower())


def scan_file(path: Path, sut_root: Path | None = None) -> list[TbdIntent]:
    """Scan one file for TBD sentinels.

    Returns ``[]`` for unsupported languages, unreadable files, or files
    containing no sentinels. The returned ``TbdIntent.file`` is the SUT-
    relative path when ``sut_root`` is provided and the file lives inside
    it; otherwise the absolute path.
    """
    language = detect_language(path)
    if language is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _scan_text(text, language, path, sut_root)


def scan_tbd_intents(
    paths: Iterable[Path],
    sut_root: Path | None = None,
) -> list[TbdIntent]:
    """Walk paths (files or directories) and return all TBD sentinels.

    Directories are walked recursively. Hidden directories (starting with
    ``.``) and common vendor/build directories (``node_modules``, ``.git``,
    ``.venv``, ``__pycache__``, ``target``, ``build``, ``dist``) are
    skipped. Files with unsupported extensions are silently ignored.

    Stable ordering: results are sorted by ``(file, line)`` so the output is
    deterministic across runs — important for the scorer agent's prompt
    caching and for the review-gate diff.
    """
    out: list[TbdIntent] = []
    seen: set[Path] = set()
    for p in paths:
        for f in _iter_source_files(p):
            resolved = f.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.extend(scan_file(f, sut_root))
    out.sort(key=lambda t: (str(t.file), t.line))
    return out


_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "target", "build", "dist", "site-packages", "dist-packages", ".tox",
})


def _iter_source_files(p: Path) -> Iterable[Path]:
    if p.is_file():
        yield p
        return
    if not p.is_dir():
        return
    # Resolve the scan root once so we can take relative paths cheaply.
    # We filter ``_EXCLUDE_DIRS`` and hidden-prefixed components only on
    # parts INSIDE the scan root, not on the absolute path. Without this,
    # a worca-t workspace at ``~/.worca-t/<run>/sut/...`` trips the
    # hidden-dir filter on every file because ``.worca-t`` is one of the
    # path's ancestors — and the scanner returns zero hits, silently
    # disabling TBD promotion across every run.
    p_resolved = p.resolve()
    for child in p.rglob("*"):
        if not child.is_file():
            continue
        try:
            relative_parts = child.resolve().relative_to(p_resolved).parts
        except ValueError:
            # Child is outside the scan root (symlink edge case) — fall
            # back to checking the bare name only.
            relative_parts = (child.name,)
        if any(part in _EXCLUDE_DIRS or (part.startswith(".") and part != ".")
               for part in relative_parts):
            continue
        if detect_language(child) is None:
            continue
        yield child


# --- Implementation: per-text scan -----------------------------------------


def _scan_text(
    text: str,
    language: Language,
    file: Path,
    sut_root: Path | None,
) -> list[TbdIntent]:
    """Run the language pattern against text, dropping comments first.

    Implementation note: we strip block comments before splitting into lines,
    which means a `/* ... tbd("x") ... */` spanning multiple lines no longer
    contributes spurious matches. Then per-line we strip line comments
    (Python `#`, C-family `//`) and run the regex. Line numbers reported are
    1-based against the ORIGINAL text, not the stripped version (the regex
    consumes line offsets from a parallel walk).
    """
    pat = _PATTERNS[language]
    rel_file = file
    if sut_root is not None:
        try:
            rel_file = file.resolve().relative_to(sut_root.resolve())
        except ValueError:
            rel_file = file

    # Block-comment stripping (Java/TS/JS only) preserves each comment's
    # newline count so 1-based line numbers in the stripped text still
    # match the original. See `_strip_block_comments`.
    stripped_blocks = _strip_block_comments(text, language)

    results: list[TbdIntent] = []
    for line_no, raw_line in enumerate(stripped_blocks.splitlines(), start=1):
        line = _strip_line_comment(raw_line, language)
        if "tbd" not in line.lower() and "Tbd.of" not in line:
            continue
        for m in pat.finditer(line):
            intent = m.group("intent")
            if not intent:
                continue
            lhs = m.groupdict().get("lhs") or None
            results.append(TbdIntent(
                file=rel_file,
                line=line_no,
                constant_name=lhs,
                intent=intent,
                language=language,
            ))
    return results


__all__ = [
    "Language",
    "TbdIntent",
    "detect_language",
    "scan_file",
    "scan_tbd_intents",
]
