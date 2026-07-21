"""Phase B.5 — static reconciliation of generated tests against POM signatures.

Parses every freshly generated test file, finds each `<obj>.<method>(args...)`
call whose receiver resolves to a POM in the codegen manifest, reads the
post-extension POM from disk to enumerate which methods exist, and emits a
structured `Mismatch` for every gap. The orchestrator synthesises `_PomTask`
entries via `mismatches_to_pom_tasks`, re-invokes `_extend_poms` once, and
re-verifies — avoiding the Step 9 round-trip that would otherwise surface the
same `AttributeError`. Java is out of scope for v1.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qtea._ast_utils import MAX_FILE_BYTES, parse_file
from qtea.logging_setup import get_logger

log = get_logger(__name__)


# Receiver names that are Playwright fixtures, not POMs. Without this guard
# the lowercase→TitleCase heuristic in `_resolve_receiver` would map a bare
# `page` to a `Page` class if any POM happened to be named `Page`, causing
# every `page.click()` in every test to flag as `method_not_found`.
_PLAYWRIGHT_FIXTURES: frozenset[str] = frozenset({
    "page", "request", "context", "browser", "browser_context",
    "playwright", "live_server", "live_browser", "expect",
})


@dataclass
class CallSite:
    """A `<obj>.<method>(args...)` call discovered in a generated test file."""

    test_file: str
    line: int
    obj_name: str
    method_name: str
    arity: int
    kw_names: list[str] = field(default_factory=list)
    snippet: str = ""
    has_spread: bool = False

    def as_dict(self) -> dict:
        return {
            "test_file": self.test_file, "line": self.line,
            "obj_name": self.obj_name, "method_name": self.method_name,
            "arity": self.arity, "kw_names": list(self.kw_names),
            "snippet": self.snippet, "has_spread": self.has_spread,
        }


@dataclass
class Mismatch:
    """A call site that does not align with what the resolved POM exposes."""

    kind: str  # "method_not_found" | "arity_mismatch" | "parse_error" | "likely_typo"
    call_site: CallSite
    resolved_pom: str
    pom_file: str
    existing_methods: list[str] = field(default_factory=list)
    # Populated when kind == "likely_typo": the existing method name the
    # called name most likely typoes. None for every other kind. The
    # orchestrator surfaces this in the failure anchor ("did you mean X?")
    # and never autopatches typos — the human must edit the test.
    suggested_method: str | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "kind": self.kind, "call_site": self.call_site.as_dict(),
            "resolved_pom": self.resolved_pom, "pom_file": self.pom_file,
            "existing_methods": list(self.existing_methods),
        }
        if self.suggested_method is not None:
            out["suggested_method"] = self.suggested_method
        return out


@dataclass
class FixtureMismatch:
    """A fixture declared in the plan that is missing on disk after Phase A4.

    Catches the failure mode where `_create_fixtures` writes N fixtures' worth
    of LLM calls into the same file but the parallel writes race so only one
    survives (see run 20260611-184450-1fbf3d). Without this check, the broken
    fixtures file passes reconciliation and Step 9 fails at pytest collection.
    """

    kind: str  # "fixture_symbol_missing" | "fixture_file_missing"
    name: str
    expected_file: str  # SUT-relative path
    source: str  # "create" | "reuse"
    referenced_by: list[str] = field(default_factory=list)  # test_case IDs
    existing_symbols: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "expected_file": self.expected_file,
            "source": self.source,
            "referenced_by": list(self.referenced_by),
            "existing_symbols": list(self.existing_symbols),
        }


@dataclass
class ReconciliationResult:
    """Top-level report; serialised to `reconcile-result.json`."""

    test_files_scanned: int
    call_sites_checked: int
    pom_files_scanned: int
    mismatches: list[Mismatch] = field(default_factory=list)
    fixture_files_scanned: int = 0
    fixture_mismatches: list[FixtureMismatch] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "test_files_scanned": self.test_files_scanned,
            "call_sites_checked": self.call_sites_checked,
            "pom_files_scanned": self.pom_files_scanned,
            "mismatches": [m.as_dict() for m in self.mismatches],
            "fixture_files_scanned": self.fixture_files_scanned,
            "fixture_mismatches": [
                fm.as_dict() for fm in self.fixture_mismatches
            ],
        }


@dataclass
class _Sig:
    name: str
    arity: int  # total declared parameter count (informational)
    arg_names: list[str]
    # True when the POM method has *args / **kwargs (Python) or rest params
    # (JS/Java varargs) — i.e. an UNBOUNDED max arity. Retained for callers
    # that predate the min/max range fields below.
    flexible: bool = False
    # Accepted positional-arity RANGE. ``min_arity`` counts required params
    # (excludes those with a default value or a TS ``?`` optional marker);
    # ``max_arity`` is the largest accepted count, or ``None`` when unbounded
    # (rest/varargs). When populated, the reconciler range-checks a call site
    # against [min_arity, max_arity] instead of exact-matching ``arity`` — this
    # both stops false positives on all-optional signatures (e.g.
    # ``sendForReview(reviewer?, dueDate?)`` called with 0 args) and stops
    # genuine zero-arg calls to required-param methods from slipping through
    # the old coarse "has a default → skip the check entirely" behaviour.
    min_arity: int | None = None
    max_arity: int | None = None
    # Raw parameter list source (e.g. ``"(username: string, password: string)"``)
    # so Step 8 can hand the test writer real signatures for PRE-EXISTING POM
    # methods, not just the ones being newly created. Empty when unknown.
    params: str = ""


# ---------------------------------------------------------------------------
# Typo detection (defends auto-patch against synthesising stubs for
# misspelled method names — e.g. `pom.sumbit_form()` getting a stub that
# masks the test bug). When an unknown method name is within edit distance
# of an existing one, the reconciler emits `likely_typo` instead of
# `method_not_found`; the orchestrator's autopatch step then skips it
# (because `mismatches_to_pom_tasks` only patches the two patch-able kinds)
# and hard-fails with a "did you mean X?" anchor.
# ---------------------------------------------------------------------------

# Names shorter than this are skipped — at length 3, edit distance 2 is
# essentially "any other 3-letter name," so the signal is too noisy.
_TYPO_MIN_NAME_LEN = 5
# Levenshtein distance threshold for a typo claim. 1 = single-char swap /
# insert / delete; 2 = transposition + nearby key. >2 is "different method".
_TYPO_MAX_DIST = 2


def _levenshtein(a: str, b: str) -> int:
    """Standard Levenshtein edit distance. Pure stdlib, O(n*m)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(
                prev[j] + 1,         # deletion
                cur[j - 1] + 1,      # insertion
                prev[j - 1] + cost,  # substitution
            ))
        prev = cur
    return prev[-1]


def _find_typo_match(called: str, existing: list[str]) -> str | None:
    """Closest unique existing name within edit distance, or None.

    Returns None when (a) the called name is too short for a meaningful
    typo distinction, (b) no existing method falls within `_TYPO_MAX_DIST`,
    or (c) two or more existing methods tie at the best distance — that's
    ambiguous and we prefer to surface the raw `method_not_found` rather
    than guess.
    """
    if len(called) < _TYPO_MIN_NAME_LEN or not existing:
        return None
    scored: list[tuple[int, str]] = []
    for name in existing:
        if len(name) < _TYPO_MIN_NAME_LEN:
            continue
        d = _levenshtein(called, name)
        if d <= _TYPO_MAX_DIST:
            scored.append((d, name))
    if not scored:
        return None
    scored.sort()
    best_d = scored[0][0]
    best = [name for d, name in scored if d == best_d]
    if len(best) > 1:
        return None  # ambiguous tie
    return best[0]


# ---------------------------------------------------------------------------
# Python extraction
# ---------------------------------------------------------------------------


def _py_pom_methods(tree: ast.AST, class_name: str) -> list[_Sig]:
    out: list[_Sig] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if stmt.name.startswith("_"):
                continue
            args = [a.arg for a in stmt.args.args]
            had_receiver = bool(args and args[0] in ("self", "cls"))
            if had_receiver:
                args = args[1:]
            has_vararg = stmt.args.vararg is not None
            flexible = bool(
                has_vararg
                or stmt.args.kwarg is not None
                or stmt.args.kwonlyargs
                or stmt.args.defaults
                or stmt.args.kw_defaults
            )
            # Positional-arity range: trailing params with defaults are optional.
            # `*args` makes the max unbounded. `**kwargs`/kwonly only affect
            # keyword args (the reconciler already skips calls that use them),
            # so they don't change the positional range.
            n_pos = len(args)
            min_arity = max(0, n_pos - len(stmt.args.defaults))
            max_arity = None if has_vararg else n_pos
            try:
                raw = ast.unparse(stmt.args)
                # Strip the leading self/cls receiver so the params string
                # matches the JS/Java (receiver-free) convention and the
                # self-stripped arity range.
                if had_receiver:
                    raw = re.sub(r"^(self|cls)\b\s*,?\s*", "", raw)
                params = "(" + raw + ")"
            except Exception:  # pragma: no cover - ast.unparse edge cases
                params = ""
            out.append(_Sig(
                stmt.name, n_pos, args, flexible=flexible,
                min_arity=min_arity, max_arity=max_arity, params=params,
            ))
    return out


def _py_imports(tree: ast.AST, known: set[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in known:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tail = alias.name.rsplit(".", 1)[-1]
                if tail in known:
                    aliases[alias.asname or tail] = tail
    return aliases


def _attr_chain(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Attribute):
        return None
    method = node.attr
    val = node.value
    if isinstance(val, ast.Name):
        return val.id, method
    if (isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name)
            and val.value.id == "self"):
        return val.attr, method
    return None


def _py_call_sites(tree: ast.AST, lines: list[str], rel: str) -> list[CallSite]:
    out: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _attr_chain(node.func)
        if resolved is None:
            continue
        obj_name, method_name = resolved
        # Spread / **kwargs: arity cannot be statically determined.
        has_spread = bool(
            any(isinstance(a, ast.Starred) for a in node.args)
            or any(kw.arg is None for kw in node.keywords)
        )
        kw_names = [kw.arg for kw in node.keywords if kw.arg]
        # Count only concrete positional args + named kwargs; spread entries
        # are ignored because their runtime arity is unknown.
        positional = sum(1 for a in node.args if not isinstance(a, ast.Starred))
        named_kwargs = sum(1 for kw in node.keywords if kw.arg is not None)
        arity = positional + named_kwargs
        line = getattr(node, "lineno", 0) or 0
        snippet = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        out.append(CallSite(
            rel, line, obj_name, method_name, arity, kw_names, snippet,
            has_spread=has_spread,
        ))
    return out


# ---------------------------------------------------------------------------
# JS/TS extraction
# ---------------------------------------------------------------------------

_JS_CLASS_HEADER_RE = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")
# `class Sub extends Base` / `class Sub extends ns.Base` (TS/JS/Java). Group 2
# is the base name (namespace prefix stripped by the caller). Used to walk the
# inheritance closure so a call to an inherited base method resolves.
_JS_EXTENDS_RE = re.compile(
    r"\bclass\s+([A-Za-z_$][\w$]*)\s+extends\s+([A-Za-z_$][\w$.]*)"
)
_JS_IMPORT_RE = re.compile(
    r"import\s*(?:type\s*)?\{([^}]+)\}\s*from\s*['\"][^'\"]+['\"]"
)
# Allow optional `: Type` between var name and `=` for TS-typed declarations.
_JS_NEW_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;]+?)?=\s*new\s+([A-Za-z_$][\w$]*)"
)
# Call HEAD: receiver, optional `?.`, whitespace/newlines around the dot,
# method name, then `(`. Closing paren is found by depth-aware walk below
# (so nested calls like `page.foo(bar())` parse correctly).
_JS_CALL_HEAD_RE = re.compile(
    r"(?:await\s+)?([A-Za-z_$][\w$]*)\s*\??\.\s*([A-Za-z_$][\w$]*)\s*\(",
)
# Method definition HEAD inside a class body. Two forms:
#   1. Classic method:        `[modifiers] name<T>(...)` — name immediately
#      followed by (optional generics and) the params `(`.
#   2. Arrow-fn class property: `[modifiers] name = (async )?(...) => ...` —
#      common in modern TS POMs (`logIn = async (u, p) => {...}`). The `(`
#      matched is the PARAMS paren in both cases; the closing paren / generic
#      close are located by depth-aware walks rather than by regex, and the
#      def-vs-call disambiguation in `_js_pom_methods` accepts a `=>` tail.
_JS_METHOD_HEAD_RE = re.compile(
    r"^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*"
    r"(?:"
    r"(?:<[^<>]*>)?\s*\("        # classic:  name(  /  name<T>(
    r"|"
    r"=\s*(?:async\s+)?\("       # arrow property:  name = (  /  name = async (
    r")",
    re.MULTILINE,
)
_JS_KW_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:")
_LIFECYCLE_NAMES: frozenset[str] = frozenset({
    "constructor", "beforeAll", "beforeEach", "afterAll", "afterEach",
    "if", "for", "while", "switch", "return", "do", "catch",
})

# Java method definition HEAD: required visibility (excludes package-private,
# which safely dodges false positives from `return foo(...)` / `throw x(...)`),
# optional modifiers + method-level generics, return type, name, `(`.
_JAVA_METHOD_HEAD_RE = re.compile(
    r"^[ \t]*"
    r"(?:(?:public|private|protected)\s+)"
    r"(?:(?:static|final|abstract|synchronized|native|default)\s+)*"
    r"(?:<[^<>]*>\s+)?"
    r"(?:void|[\w$][\w$.]*(?:<[^<>]*>)?(?:\[\])*)\s+"
    r"([\w$]+)\s*\(",
    re.MULTILINE,
)
_JAVA_LIFECYCLE_NAMES: frozenset[str] = frozenset({
    "setUp", "tearDown",
    "setUpBeforeClass", "tearDownAfterClass",
    "beforeEach", "afterEach", "beforeAll", "afterAll",
    "beforeMethod", "afterMethod", "beforeClass", "afterClass",
    "beforeTest", "afterTest", "beforeSuite", "afterSuite",
})


def _blank_js_template(src: str, out: list[str], i: int) -> int:
    """Blank a template literal starting at ``src[i] == '`'`` in-place on ``out``.

    Returns the index just past the closing backtick (or ``len(src)`` if the
    template is unterminated). Backticks, ``${…}`` interpolations, and any
    strings / nested templates inside those interpolations are all blanked to
    spaces (newlines preserved), so no delimiter or brace/paren leaks out to
    confuse the downstream brace/paren walks. Interpolation expressions are
    blanked wholesale — they never contain a class-level method DEFINITION,
    which is all the extractor cares about — but their ``{ }`` depth IS tracked
    so the correct closing backtick is found even when an interpolation contains
    braces, quotes, or a nested template.
    """
    n = len(src)
    out[i] = " "  # opening backtick
    i += 1
    while i < n:
        ch = src[i]
        if ch == "\\" and i + 1 < n:  # escaped char (e.g. \` or \$)
            out[i] = " "
            if src[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if ch == "`":
            out[i] = " "
            return i + 1
        if ch == "$" and i + 1 < n and src[i + 1] == "{":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            depth = 1
            while i < n and depth > 0:
                c = src[i]
                if c == "{":
                    depth += 1
                    out[i] = " "
                    i += 1
                elif c == "}":
                    depth -= 1
                    out[i] = " "
                    i += 1
                elif c == "`":
                    i = _blank_js_template(src, out, i)
                elif c in ("'", '"'):
                    i = _blank_js_string(src, out, i, keep_delims=False)
                else:
                    if c != "\n":
                        out[i] = " "
                    i += 1
            continue
        if ch != "\n":
            out[i] = " "
        i += 1
    return i  # unterminated — consumed to EOF


def _blank_js_string(
    src: str, out: list[str], i: int, *, keep_delims: bool,
) -> int:
    """Blank a ``'…'`` / ``"…"`` string starting at ``src[i]`` on ``out``.

    ``keep_delims`` leaves the opening/closing quote characters intact (used at
    code level, matching the historical behaviour where quotes were preserved);
    when False the quotes are blanked too (used inside template interpolations,
    where a stray quote must not leak). Returns the index past the closing
    quote. A valid JS/TS string cannot span a raw newline, so an unterminated
    string stops at end-of-line rather than eating the rest of the file.
    """
    n = len(src)
    quote = src[i]
    if not keep_delims:
        out[i] = " "
    i += 1
    while i < n:
        c = src[i]
        if c == "\n":  # unterminated string (source bug) — do not overrun
            return i
        if c == "\\" and i + 1 < n:
            out[i] = " "
            if src[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if c == quote:
            if not keep_delims:
                out[i] = " "
            return i + 1
        out[i] = " "
        i += 1
    return i


def _js_strip(src: str) -> str:
    """Blank out string literals + comments while preserving length AND newlines.

    Single-pass, context-aware scanner. Walks the source character by character
    tracking exactly one lexical context at a time — code / line-comment /
    block-comment / single-quoted / double-quoted / template-literal (with
    ``${…}`` interpolation nesting) — and blanks the interior of every
    non-code region to spaces, leaving newlines (and the code skeleton, incl.
    every ``{`` / ``}`` / ``(`` / ``)`` outside a literal) untouched. Output is
    byte-for-byte the same length as the input so downstream ``re.MULTILINE``
    anchors, ``str.find`` offsets, and the brace/paren depth walks all stay
    honest.

    Why a scanner and not ordered regex passes: no ordering of independent
    passes is safe, because the lexical contexts can each contain the others'
    delimiters. The bug this replaces: a ``//``-prefixed XPath inside a template
    literal (e.g. ``page.locator(`//a[@id="${x}"]`)``) was eaten by the
    line-comment pass (which ran before the template pass), which consumed the
    template's closing backtick; the template pass then blanked forward across
    method boundaries, silently dropping every method whose header fell in the
    swallowed span. A scanner that recognises the template context first never
    misreads that ``//`` as a comment.

    Regex literals (``/…/flags``) are NOT tokenised — a ``/`` that is not part
    of ``//`` or ``/*`` is treated as the division operator and left as code.
    POM method bodies rarely use regex literals with embedded quotes/backticks;
    the Part-2 inventory corroboration net is the backstop if one ever desyncs.
    """
    n = len(src)
    out = list(src)
    i = 0
    while i < n:
        ch = src[i]
        # Line comment: blank `//` to end-of-line (newline preserved).
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        # Block comment: blank `/* … */` across newlines.
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:  # blank the closing `*/`
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        # Single / double quoted string: blank interior, keep delimiters.
        if ch in ("'", '"'):
            i = _blank_js_string(src, out, i, keep_delims=True)
            continue
        # Template literal (may span newlines; may nest via `${…}`).
        if ch == "`":
            i = _blank_js_template(src, out, i)
            continue
        i += 1
    return "".join(out)


def _find_balanced(src: str, open_idx: int) -> int:
    """Index of the `)` matching the `(` at `open_idx`, or -1 if unbalanced.

    Assumes `_js_strip` has already neutralised strings + comments, so naive
    char counting is safe.
    """
    if open_idx >= len(src) or src[open_idx] != "(":
        return -1
    depth = 0
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _js_class_body(src: str, class_name: str) -> str | None:
    """Body (between outer braces) of the first class with the given name."""
    for m in _JS_CLASS_HEADER_RE.finditer(src):
        if m.group(1) != class_name:
            continue
        brace = src.find("{", m.end())
        if brace == -1:
            continue
        depth = 0
        for i in range(brace, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[brace + 1: i]
        # Brace opened but never closed — skip this class, try the next match.
    return None


def _split_arg_parts(blob: str, open_chars: str, close_chars: str) -> list[str]:
    """Comma-split a param/arg blob at top level, respecting bracket nesting.

    Shared by the JS and Java splitters. ``open_chars`` / ``close_chars`` let
    Java include ``<>`` for generics (``List<Map<K,V>>``) while JS does not
    (``=>`` arrows and comparisons would confuse depth tracking).
    """
    blob = blob.strip()
    if not blob:
        return []
    depth = 0
    parts: list[str] = []
    cur = ""
    for ch in blob:
        if ch in open_chars:
            depth += 1
        elif ch in close_chars:
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _split_js_args(blob: str) -> tuple[int, list[str], bool]:
    """Split a JS CALL-SITE arg blob into (count, keyword_names, flexible).

    `flexible=True` when any arg is a rest/spread (`...args`) or carries a
    default (`a = 1`). Used for call sites (argument count) and — via the
    legacy path — nothing else now that POM signatures go through
    ``_js_param_range``.
    """
    parts = _split_arg_parts(blob, "([{", ")]}")
    flexible = any(p.startswith("...") or "=" in p for p in parts)
    kw_names = [m.group(1) for p in parts if (m := _JS_KW_RE.match(p))]
    return len(parts), kw_names, flexible


def _param_range(parts: list[str], *, ts_optional: bool) -> tuple[int, int | None, int]:
    """Compute (min_required, max_allowed|None, total) for a param declaration.

    A param is OPTIONAL (excluded from ``min_required``) when it has a default
    (`a = 1` / `a: T = 1`) or — when ``ts_optional`` — a TS optional marker
    (`name?: T`). A rest/varargs param (`...args` / `String... xs`) makes
    ``max_allowed`` unbounded (``None``) and does not itself count toward min.
    """
    min_req = 0
    max_allowed = 0
    unbounded = False
    for p in parts:
        if not p:
            continue
        # Rest/varargs detection on the param HEAD only (before any default),
        # so a spread inside a default value (`arr = [...DEFAULTS]`) is not
        # mistaken for a rest param. `...args` (JS) → head startswith; Java
        # `String... xs` → `...` in the type-before-name region.
        head = p.split("=", 1)[0]
        if head.strip().startswith("...") or "..." in head.split(":", 1)[0]:
            unbounded = True
            continue
        max_allowed += 1
        name_part = p.split(":", 1)[0].strip() if ":" in p else p
        optional = ("=" in p) or (ts_optional and name_part.endswith("?"))
        if not optional:
            min_req += 1
    return min_req, (None if unbounded else max_allowed), len(parts)


def _js_pom_methods(src: str, class_name: str) -> list[_Sig]:
    body = _js_class_body(_js_strip(src), class_name)
    if body is None:
        return []
    out: list[_Sig] = []
    seen: set[str] = set()
    for m in _JS_METHOD_HEAD_RE.finditer(body):
        name = m.group(1)
        if name in _LIFECYCLE_NAMES or name in seen:
            continue
        # m.end() points just after the matched `(`. Find the matching `)`.
        open_paren = m.end() - 1
        close = _find_balanced(body, open_paren)
        if close == -1:
            continue
        # Disambiguate from a call site at file scope: a method definition
        # is followed by `{`, `:` (return-type annotation), or `=>` (arrow).
        tail = body[close + 1: close + 32].lstrip()
        if not tail or (tail[0] not in "{:" and not tail.startswith("=>")):
            continue
        blob = body[m.end(): close]
        min_req, max_allowed, total = _param_range(
            _split_arg_parts(blob, "([{", ")]}"), ts_optional=True,
        )
        # Param names omitted on the JS side — they aren't used downstream,
        # but the raw param blob feeds Step 8's writer manifest.
        seen.add(name)
        out.append(_Sig(
            name, total, [], flexible=(max_allowed is None),
            min_arity=min_req, max_arity=max_allowed,
            params=f"({blob.strip()})",
        ))
    return out


def _java_pom_methods(src: str, class_name: str) -> list[_Sig]:
    """Extract method signatures from the named class body in a .java file.

    Reuses ``_js_class_body`` (brace-matching is language-neutral) after
    stripping strings/comments with ``_js_strip``. Requires visibility
    (public/private/protected) to skip false positives from `return foo(...)`,
    `throw new X(...)`, and similar statements at line start.
    """
    body = _js_class_body(_js_strip(src), class_name)
    if body is None:
        return []
    out: list[_Sig] = []
    seen: set[str] = set()
    for m in _JAVA_METHOD_HEAD_RE.finditer(body):
        name = m.group(1)
        if name in _JAVA_LIFECYCLE_NAMES or name in seen:
            continue
        open_paren = m.end() - 1
        close = _find_balanced(body, open_paren)
        if close == -1:
            continue
        # Java method definitions end with `{` (body), `;` (abstract /
        # interface), or `throws ExceptionType`. Anything else means we
        # matched a call site by accident.
        tail = body[close + 1: close + 32].lstrip()
        if not tail or (tail[0] not in "{;" and not tail.startswith("throws")):
            continue
        blob = body[m.end(): close]
        # Java: no optional/default params; `String... xs` is varargs.
        min_req, max_allowed, total = _param_range(
            _split_arg_parts(blob, "([{<", ")]}>"), ts_optional=False,
        )
        seen.add(name)
        out.append(_Sig(
            name, total, [], flexible=(max_allowed is None),
            min_arity=min_req, max_arity=max_allowed,
            params=f"({blob.strip()})",
        ))
    return out


def pom_method_signatures(
    pom_text: str, class_name: str, language: str,
) -> dict[str, str]:
    """Return ``{method_name: "(param, param, ...)"}`` for ``class_name``.

    Step 8 uses this to give the test writer real signatures for POM methods
    that ALREADY EXIST in the SUT (the codegen plan only carries signatures for
    methods being newly created, so pre-existing methods referenced by the
    choreography would otherwise reach the writer as bare names — which is how
    the writer ended up emitting zero-arg stub calls). Reuses the same parsers
    the reconciler uses, so what the writer sees matches what the arity gate
    later checks. Best-effort: returns ``{}`` on parse failure.
    """
    lang = (language or "").lower()
    sigs: list[_Sig]
    if lang in ("python", "py"):
        try:
            tree = ast.parse(pom_text)
        except SyntaxError:
            return {}
        sigs = _py_pom_methods(tree, class_name)
    elif lang == "java":
        sigs = _java_pom_methods(pom_text, class_name)
    else:  # typescript / javascript / tsx / jsx / ...
        sigs = _js_pom_methods(pom_text, class_name)
    return {s.name: s.params for s in sigs if s.params}


def _js_imports(src: str, known: set[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for m in _JS_IMPORT_RE.finditer(src):
        for part in m.group(1).split(","):
            token = part.strip()
            if not token:
                continue
            if " as " in token:
                orig, alias = (x.strip() for x in token.split(" as ", 1))
            else:
                orig = alias = token
            if orig in known:
                aliases[alias] = orig
    for m in _JS_NEW_RE.finditer(src):
        inst, cls = m.group(1), m.group(2)
        if cls in known:
            aliases[inst] = cls
    return aliases


def _js_call_sites(src: str, rel: str) -> list[CallSite]:
    sanitized = _js_strip(src)
    lines = src.splitlines()
    out: list[CallSite] = []
    for m in _JS_CALL_HEAD_RE.finditer(sanitized):
        obj_name, method_name = m.group(1), m.group(2)
        open_paren = m.end() - 1
        close = _find_balanced(sanitized, open_paren)
        if close == -1:
            continue
        blob = sanitized[m.end(): close]
        arity, kw_names, has_spread = _split_js_args(blob)
        # JS callers can spread arrays (`page.foo(...xs)`) — that's
        # equivalent to Python's *args at the call site and the runtime
        # arity is unknown statically.
        line = sanitized.count("\n", 0, m.start()) + 1
        snippet = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        out.append(CallSite(
            rel, line, obj_name, method_name, arity, kw_names, snippet,
            has_spread=has_spread,
        ))
    return out


# ---------------------------------------------------------------------------
# Resolution + orchestration
# ---------------------------------------------------------------------------


def _resolve_receiver(
    obj: str, aliases: dict[str, str], known: set[str],
) -> str | None:
    if obj in aliases:
        return aliases[obj]
    if obj in _PLAYWRIGHT_FIXTURES:
        return None
    # Heuristic v1: `login_page` / `loginPage` → `LoginPage`.
    if "_" in obj:
        cand = "".join(part.capitalize() for part in obj.split("_"))
    else:
        cand = obj[:1].upper() + obj[1:]
    return cand if cand in known else None


def _scan_pom(
    pom: dict, sut_root: Path, language: str,
) -> tuple[Path | None, list[_Sig]]:
    rel = pom.get("file") or ""
    cls = pom.get("class_name") or ""
    if not rel or not cls:
        return None, []
    path = sut_root / rel
    if not path.is_file():
        return None, []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning("reconcile.pom_oversize", file=rel)
            return None, []
    except OSError:
        return None, []
    if language == "python":
        tree = parse_file(path)
        return (path, _py_pom_methods(tree, cls) if tree is not None else [])
    if language == "java":
        try:
            return path, _java_pom_methods(
                path.read_text(encoding="utf-8", errors="replace"), cls,
            )
        except OSError:
            return None, []
    try:
        return path, _js_pom_methods(
            path.read_text(encoding="utf-8", errors="replace"), cls,
        )
    except OSError:
        return None, []


_MAX_INHERITANCE_DEPTH = 8


def inventory_method_index(sut_inventory: dict | None) -> dict[str, set[str]]:
    """Map ``class_name -> {method names}`` from a Step-6 ``sut_inventory`` dict.

    This is the corroboration source for `reconcile_codegen`: a method the
    on-disk parser fails to enumerate but that the Step-6 inventory recorded is
    treated as *present* (with a logged disagreement) rather than hard-failed —
    so no single method-extractor bug can block the gate on a method that
    demonstrably exists. Aggregated across all modules; keyed on both
    ``class_name`` and ``name`` so either receiver resolution hits.
    """
    out: dict[str, set[str]] = {}
    if not isinstance(sut_inventory, dict):
        return out
    for module in sut_inventory.get("modules") or []:
        if not isinstance(module, dict):
            continue
        for po in module.get("existing_page_objects") or []:
            if not isinstance(po, dict):
                continue
            methods = {m for m in (po.get("methods") or []) if isinstance(m, str)}
            if not methods:
                continue
            for key in (po.get("class_name"), po.get("name")):
                if key:
                    out.setdefault(key, set()).update(methods)
    return out


def _class_parent(src_or_tree: Any, class_name: str, language: str) -> str | None:
    """Immediate ``extends`` base-class name of ``class_name``, or None.

    Python: reads ``ClassDef.bases`` (first Name/Attribute base). JS/TS/Java:
    parses ``class Sub extends Base`` from the (string-stripped) source. Only
    single inheritance is followed — sufficient for the POM/Screenplay layouts
    this gate targets.
    """
    if language == "python":
        for node in ast.walk(src_or_tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        return base.id
                    if isinstance(base, ast.Attribute):
                        return base.attr
                return None
        return None
    for m in _JS_EXTENDS_RE.finditer(_js_strip(src_or_tree)):
        if m.group(1) == class_name:
            return m.group(2).rsplit(".", 1)[-1]
    return None


def _augment_with_inheritance(
    pom_signatures: dict[str, list[_Sig]],
    pom_by_class: dict[str, dict],
    sut_root: Path,
    language: str,
) -> dict[str, list[_Sig]]:
    """Union each POM's own methods with those inherited via its ``extends``
    closure (subclass methods win on name conflict).

    Without this, a call to a base-class method through a subclass receiver
    false-flags ``method_not_found`` because the reconciler only ever parsed the
    subclass's own body (the latent inheritance bug -- e.g.
    ``EntityFormPage.someInheritedMethod`` inherited from ``BasePage``).
    Ancestors are resolved via the manifest first, then a sibling-file heuristic
    (``<dir>/<Base>.<ext>``); unresolvable ancestors are skipped (best-effort).
    """

    def own_and_parent(cls: str, rel: str) -> tuple[list[_Sig], str | None]:
        path = sut_root / rel
        if not path.is_file():
            return [], None
        if language == "python":
            tree = parse_file(path)
            if tree is None:
                return [], None
            return _py_pom_methods(tree, cls), _class_parent(tree, cls, "python")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], None
        if language == "java":
            return _java_pom_methods(text, cls), _class_parent(text, cls, "java")
        return _js_pom_methods(text, cls), _class_parent(text, cls, language)

    def file_for(cls: str, from_rel: str) -> str | None:
        if cls in pom_by_class:
            return pom_by_class[cls].get("file")
        if language == "python":
            return None  # module naming unpredictable; rely on the manifest
        parent_dir = Path(from_rel).parent.as_posix()
        for ext in (".ts", ".tsx", ".js", ".jsx", ".java"):
            cand = f"{cls}{ext}" if parent_dir in ("", ".") else f"{parent_dir}/{cls}{ext}"
            if (sut_root / cand).is_file():
                return cand
        return None

    def closure(cls: str, rel: str, seen: set[str], depth: int) -> list[_Sig]:
        if depth > _MAX_INHERITANCE_DEPTH or cls in seen or not rel:
            return []
        seen.add(cls)
        own, parent = own_and_parent(cls, rel)
        by_name: dict[str, _Sig] = {s.name: s for s in own}
        if parent and parent != cls:
            prel = file_for(parent, rel)
            if prel:
                for s in closure(parent, prel, seen, depth + 1):
                    by_name.setdefault(s.name, s)  # subclass override wins
        return list(by_name.values())

    augmented: dict[str, list[_Sig]] = {}
    for cls, pom in pom_by_class.items():
        rel = pom.get("file") or ""
        # Only augment classes that scanned successfully; leave others as-is.
        augmented[cls] = (
            closure(cls, rel, set(), 0) if cls in pom_signatures
            else pom_signatures.get(cls, [])
        )
    return augmented


def reconcile_codegen(
    test_files: list[Path],
    pom_files: list[dict],
    sut_root: Path,
    language: str,
    inventory_methods: dict[str, set[str]] | None = None,
) -> ReconciliationResult:
    """Cross-check call sites in generated tests against POM signatures on disk.

    Extracts every `<obj>.<method>(...)` call whose receiver resolves (via
    imports or naming heuristic) to a POM listed in `pom_files`, and verifies
    method existence + arity compatibility. Calls on objects that don't resolve
    to any known POM are silently ignored (likely SUT-native helpers).

    A method is considered PRESENT if it is found via any of three sources, in
    order: (1) the resolved class's own methods parsed from disk, (2) methods
    inherited through the class's ``extends`` closure, or (3) ``inventory_methods``
    — the Step-6 ``sut_inventory`` map (see `inventory_method_index`). Source 3
    is the corroboration net: it guarantees a method the inventory recorded can
    never be hard-failed by a lone parser miss (a disagreement is logged
    instead). ``method_not_found`` is therefore reserved for methods genuinely
    absent from every source.
    """
    lang = (language or "").lower()
    if lang not in {"python", "typescript", "javascript", "java"}:
        log.warning("reconcile.unsupported_language", language=language)
        return ReconciliationResult(0, 0, 0, [])

    pom_by_class: dict[str, dict] = {
        p["class_name"]: p for p in pom_files if p.get("class_name")
    }
    pom_signatures: dict[str, list[_Sig]] = {}
    pom_scanned = 0
    for pom in pom_files:
        path, sigs = _scan_pom(pom, sut_root, lang)
        if path is None:
            continue
        pom_scanned += 1
        pom_signatures[pom["class_name"]] = sigs

    # Fold in methods reachable through each class's `extends` closure so
    # inherited base-class calls resolve instead of false-flagging.
    pom_signatures = _augment_with_inheritance(
        pom_signatures, pom_by_class, sut_root, lang,
    )
    inv_methods = inventory_methods or {}

    known = set(pom_by_class.keys())
    mismatches: list[Mismatch] = []
    files_scanned = 0
    calls_checked = 0

    for tf in test_files:
        if not tf.is_file():
            continue
        try:
            if tf.stat().st_size > MAX_FILE_BYTES:
                log.warning("reconcile.test_oversize", file=str(tf))
                continue
        except OSError:
            continue
        try:
            rel = tf.relative_to(sut_root).as_posix()
        except ValueError:
            rel = tf.as_posix()
        files_scanned += 1

        if lang == "python":
            tree = parse_file(tf)
            if tree is None:
                # line=1 (not 0) so the artifact satisfies the schema's
                # `minimum: 1` constraint without per-error special-casing.
                mismatches.append(Mismatch(
                    kind="parse_error",
                    call_site=CallSite(
                        rel, 1, "", "", 0, [], "<syntax error>",
                    ),
                    resolved_pom="", pom_file=rel, existing_methods=[],
                ))
                continue
            lines = tf.read_text(encoding="utf-8", errors="replace").splitlines()
            aliases = _py_imports(tree, known)
            sites = _py_call_sites(tree, lines, rel)
        else:
            try:
                src = tf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            aliases = _js_imports(src, known)
            sites = _js_call_sites(src, rel)

        for site in sites:
            resolved = _resolve_receiver(site.obj_name, aliases, known)
            if resolved is None:
                continue
            calls_checked += 1
            sigs = pom_signatures.get(resolved, [])
            pom_file_rel = pom_by_class.get(resolved, {}).get("file", "")
            existing = [s.name for s in sigs]
            match = next((s for s in sigs if s.name == site.method_name), None)
            if match is None:
                # Corroboration net: a method the on-disk parser missed but the
                # Step-6 inventory recorded is present — trust the second source
                # rather than hard-fail on a lone parser miss. Arity is
                # unverifiable here (no parsed signature), so it is not checked.
                if site.method_name in inv_methods.get(resolved, frozenset()):
                    log.info(
                        "reconcile.parser_disagreement",
                        pom=resolved,
                        method=site.method_name,
                        file=pom_file_rel,
                    )
                    continue
                suggested = _find_typo_match(site.method_name, existing)
                if suggested is not None:
                    mismatches.append(Mismatch(
                        kind="likely_typo", call_site=site,
                        resolved_pom=resolved, pom_file=pom_file_rel,
                        existing_methods=existing,
                        suggested_method=suggested,
                    ))
                    continue
                mismatches.append(Mismatch(
                    kind="method_not_found", call_site=site,
                    resolved_pom=resolved, pom_file=pom_file_rel,
                    existing_methods=existing,
                ))
                continue
            # Skip the arity check when the CALLER's shape is dynamic — kwargs
            # or spread/`...` args mean the runtime positional count is unknown.
            if site.kw_names or site.has_spread:
                continue
            # Range check against the POM signature's [min_arity, max_arity].
            # This is precise about optional/default params and rest/varargs:
            #   - all-optional method called with 0 args → OK (no false positive)
            #   - required-param method called with too few → flagged (the
            #     zero-arg-stub bug the old "defaults → skip" logic masked)
            if match.min_arity is not None:
                too_few = site.arity < match.min_arity
                too_many = (
                    match.max_arity is not None and site.arity > match.max_arity
                )
                if too_few or too_many:
                    mismatches.append(Mismatch(
                        kind="arity_mismatch", call_site=site,
                        resolved_pom=resolved, pom_file=pom_file_rel,
                        existing_methods=existing,
                    ))
            else:
                # Legacy fallback (extractor didn't compute a range): keep the
                # old flexible-skip + exact-match behaviour.
                if match.flexible:
                    continue
                if site.arity != match.arity:
                    mismatches.append(Mismatch(
                        kind="arity_mismatch", call_site=site,
                        resolved_pom=resolved, pom_file=pom_file_rel,
                        existing_methods=existing,
                    ))

    return ReconciliationResult(
        test_files_scanned=files_scanned,
        call_sites_checked=calls_checked,
        pom_files_scanned=pom_scanned,
        mismatches=mismatches,
    )


def mismatches_to_pom_tasks(
    mismatches: list[Mismatch],
    original_pom_tasks: dict,
    *,
    manifest_pom_files: list[dict] | None = None,
) -> dict:
    """Group `Mismatch` records into `_PomTask`s ready for `_extend_poms`.

    One task per POM file; missing methods deduplicated by name. Signature
    inference from call sites is necessarily degraded — `(self, *args)` is the
    safest placeholder; the POM extender fills in the real body from purpose.

    `manifest_pom_files` (the orchestrator's `manifest["pom_files"]` list)
    is consulted for POMs not present in `original_pom_tasks` (the test
    called a POM not in the original plan) so the synthesised task still
    carries the right `locator_file` / `locator_class`. Without this lookup
    the second pass loses access to the locator constants and the auto-
    patched method body either inlines selectors (rule violation) or fails.
    """
    # Deferred import to dodge the s08_codegen → codegen_reconcile → s08_codegen
    # cycle at module load.
    from qtea.steps.s08_codegen import _PomTask

    manifest_by_file: dict[str, dict] = {}
    for entry in (manifest_pom_files or []):
        f = entry.get("file")
        if f:
            manifest_by_file[f] = entry

    grouped: dict[str, list[Mismatch]] = {}
    for m in mismatches:
        if m.kind not in ("method_not_found", "arity_mismatch") or not m.pom_file:
            continue
        grouped.setdefault(m.pom_file, []).append(m)

    out: dict[str, Any] = {}
    for pom_file, items in grouped.items():
        base = original_pom_tasks.get(pom_file)
        seen: set[str] = set()
        missing: list[dict[str, Any]] = []
        for m in items:
            name = m.call_site.method_name
            if name in seen:
                continue
            seen.add(name)
            missing.append({
                "name": name,
                "signature": "(self, *args) -> None",
                "purpose": (
                    f"Auto-inferred from test call at {m.call_site.test_file}:"
                    f"{m.call_site.line} — {m.call_site.snippet}"
                ),
            })
        if base is not None:
            out[pom_file] = _PomTask(
                pom_name=base.pom_name, pom_file=base.pom_file,
                source=base.source, from_path=base.from_path,
                at_path=base.at_path,
                missing_methods=missing,
                locator_file=base.locator_file,
                locator_class=base.locator_class,
            )
            continue
        first = items[0]
        manifest_entry = manifest_by_file.get(pom_file, {})
        out[pom_file] = _PomTask(
            pom_name=first.resolved_pom, pom_file=pom_file,
            source="reuse", from_path=pom_file, at_path=pom_file,
            missing_methods=missing,
            locator_file=manifest_entry.get("locator_file"),
            locator_class=manifest_entry.get("locator_class"),
        )
    return out


# Sentinel appended when a file exposes a fixture wrapper via `export default
# <expr>.extend(...)` (or default `mergeTests(...)`) — the exported name is
# arbitrary at the import site, so the reuse check must accept any `:symbol`
# reference against a file that carries this sentinel.
DEFAULT_EXPORT_SENTINEL = "__default_export_extend__"


def _scan_ts_fixture_symbols(path: Path) -> list[str]:
    """Extract Playwright fixture names from a TS/JS Playwright fixture file.

    Covers the standard idioms a Playwright user would write:

    - Inner `.extend<T>({ name: async ({}, use) => ... })` parameter names.
    - Outer wrapper: `(export )?(const|let|var) NAME[:Type] = <expr>.extend(...)`
      or `mergeTests(...)`.
    - CommonJS: `(module.)?exports.NAME = <expr>.extend(...) | mergeTests(...)`.
    - Separate named exports: `export { a, b as c };` (both re-exports of local
      symbols and `export { a } from './other';` re-exports).
    - Default export of an extend/mergeTests: emits DEFAULT_EXPORT_SENTINEL so
      the reuse check treats any `:symbol` reference against the file as valid
      (the consumer's `import test from '...'` name is arbitrary).

    Without these, plan `reuse` references to the wrapper resolve against the
    inner-only symbols and the reconciler falsely reports fixture_symbol_missing.

    Returns an empty list when the file exists but exposes no fixture surface.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    names: list[str] = []

    # Inner .extend({...}) params
    m = re.search(
        r"\w+\.extend\s*(?:<[^>]*>\s*)?\(\s*\{(.+?)\}\s*\)",
        text,
        re.S,
    )
    if m:
        body = m.group(1)
        for km in re.finditer(r"^\s*(\w+)\s*:\s*(?:async\s*)?\(", body, re.M):
            names.append(km.group(1))

    # `(export )?(const|let|var) NAME[:Type] = <expr>.extend(...) | mergeTests(...)`
    for outer in re.finditer(
        r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*"
        r"(?:\w+\.extend|mergeTests)\b",
        text,
    ):
        names.append(outer.group(1))

    # CommonJS: `(module.)?exports.NAME = <expr>.extend(...) | mergeTests(...)`
    for outer in re.finditer(
        r"(?:module\.)?exports\.(\w+)\s*=\s*(?:\w+\.extend|mergeTests)\b",
        text,
    ):
        names.append(outer.group(1))

    # Separate `export { a, b as c };` — also matches `export { a } from './x';`
    # re-exports; the exposed name (post-`as`, or the identifier itself) is
    # what a consumer's `import { … } from` would see.
    for exp in re.finditer(r"export\s*\{([^}]+)\}", text):
        for item in exp.group(1).split(","):
            item = item.strip().rstrip(";").strip()
            if not item:
                continue
            m2 = re.match(r"\w+\s+as\s+(\w+)$", item)
            if m2:
                names.append(m2.group(1))
            else:
                m3 = re.match(r"(\w+)$", item)
                if m3:
                    names.append(m3.group(1))

    # `export default <expr>.extend(...) | mergeTests(...)` — arbitrary name at
    # import site; register a sentinel and let the reuse check wildcard-match.
    if re.search(
        r"export\s+default\s+(?:\w+\.extend|mergeTests)\b",
        text,
    ):
        names.append(DEFAULT_EXPORT_SENTINEL)

    return names


# JUnit 4/5 + TestNG lifecycle annotation → method-name regex. Neutralised
# strings/comments via _js_strip before scanning.
_JAVA_FIXTURE_RE = re.compile(
    r"@Before(?:Each|All|Class|Method)?\b[^\n]*\n"
    r"(?:\s*(?:@\w+(?:\([^)]*\))?)\s*\n?)*"
    r"\s*(?:public|private|protected)?\s*"
    r"(?:(?:static|final|synchronized|native)\s+)*"
    r"(?:<[^<>]*>\s+)?"
    r"(?:void|[\w$][\w$.]*(?:<[^<>]*>)?(?:\[\])*)\s+"
    r"([\w$]+)\s*\(",
)


def _scan_java_fixture_symbols(path: Path) -> list[str]:
    """Extract JUnit/TestNG lifecycle method names from a Java file.

    Detects methods annotated with ``@Before``, ``@BeforeEach``, ``@BeforeAll``,
    ``@BeforeClass``, ``@BeforeMethod`` (JUnit 4/5 + TestNG). Returns an empty
    list when the file exists but defines no such methods.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = _js_strip(text)
    return [m.group(1) for m in _JAVA_FIXTURE_RE.finditer(text)]


def _scan_fixture_symbols(path: Path) -> list[str] | None:
    """Return fixture names defined in *path*.

    For ``.py`` files, scans for ``@pytest.fixture``-decorated functions.
    For ``.ts``/``.js``/``.mts``/``.mjs`` files, scans for Playwright
    ``*.extend<T>({...})`` blocks.
    For ``.java`` files, scans for ``@Before*``-annotated methods
    (JUnit 4/5 + TestNG).

    Returns ``None`` when the file does not exist or cannot be read
    (treated as "file missing" upstream).  An empty list means the file
    exists but defines no recognisable fixtures.
    """
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning("reconcile.fixture_file_oversize", file=str(path))
            return None
    except OSError:
        return None

    suffix = path.suffix.lower()

    if suffix in {".ts", ".js", ".mts", ".mjs"}:
        return _scan_ts_fixture_symbols(path)

    if suffix == ".java":
        return _scan_java_fixture_symbols(path)

    if suffix == ".py":
        tree = parse_file(path)
        if tree is None:
            return None
        names: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if _decorator_is_pytest_fixture(dec):
                    names.append(node.name)
                    break
        return names

    return None


def _decorator_is_pytest_fixture(dec: ast.expr) -> bool:
    """True for `@pytest.fixture` / `@fixture` / `@pytest.fixture(...)` etc."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute) and target.attr == "fixture":
        return True
    return bool(isinstance(target, ast.Name) and target.id == "fixture")


def reconcile_fixtures(
    plan: dict,
    sut_root: Path,
) -> tuple[int, list[FixtureMismatch]]:
    """Verify every fixture declared in the plan resolves on disk.

    For `source == "create"`: the target file must exist and define a
    `@pytest.fixture`-decorated function with the declared name.
    For `source == "reuse"`: the `from` reference (path:symbol form or bare
    path) must exist. If a symbol is named, it must be defined in that file.

    Returns (files_scanned, mismatches).
    """
    if not isinstance(plan, dict):
        return 0, []

    by_file: dict[str, list[tuple[str, str, str]]] = {}
    # value tuple: (fixture_name, source, tc_id)
    reuse_refs: list[tuple[str, str, str]] = []
    # value tuple: (fixture_name, from_ref, tc_id)

    for tc in plan.get("test_cases") or []:
        tc_id = tc.get("id", "")
        for fix in tc.get("fixtures") or []:
            name = (fix.get("name") or "").strip()
            source = fix.get("source")
            if not name:
                continue
            if source == "create":
                at = (fix.get("at") or "").strip()
                if not at:
                    continue
                by_file.setdefault(at, []).append((name, source, tc_id))
            elif source == "reuse":
                from_ref = (fix.get("from") or "").strip()
                if from_ref:
                    reuse_refs.append((name, from_ref, tc_id))

    mismatches: list[FixtureMismatch] = []
    refs_by_name: dict[tuple[str, str], list[str]] = {}
    # key: (file, name) → list of tc_ids that referenced this fixture

    # ---- create checks ----
    for file_rel, items in by_file.items():
        for name, _src, tc_id in items:
            refs_by_name.setdefault((file_rel, name), []).append(tc_id)
        target = sut_root / file_rel
        defined = _scan_fixture_symbols(target)
        if defined is None:
            seen_names: set[str] = set()
            for name, _src, _tc in items:
                if name in seen_names:
                    continue
                seen_names.add(name)
                mismatches.append(FixtureMismatch(
                    kind="fixture_file_missing",
                    name=name,
                    expected_file=file_rel,
                    source="create",
                    referenced_by=list(refs_by_name.get((file_rel, name), [])),
                    existing_symbols=[],
                ))
            continue
        defined_set = set(defined)
        seen_names = set()
        for name, _src, _tc in items:
            if name in seen_names:
                continue
            seen_names.add(name)
            if name in defined_set:
                continue
            # Never surface the internal sentinel in mismatch output.
            mismatches.append(FixtureMismatch(
                kind="fixture_symbol_missing",
                name=name,
                expected_file=file_rel,
                source="create",
                referenced_by=list(refs_by_name.get((file_rel, name), [])),
                existing_symbols=sorted(
                    s for s in defined_set if s != DEFAULT_EXPORT_SENTINEL
                ),
            ))

    # ---- reuse checks ----
    # Accept either "tests/fixtures/foo.py" or "tests/fixtures/foo.py:bar".
    reuse_dedup: dict[tuple[str, str], FixtureMismatch | None] = {}
    for name, from_ref, tc_id in reuse_refs:
        file_part, _, symbol_part = from_ref.partition(":")
        file_part = file_part.strip()
        symbol_part = symbol_part.strip() or name
        key = (file_part, symbol_part)
        refs_by_name.setdefault(key, []).append(tc_id)
        if key in reuse_dedup:
            mm = reuse_dedup[key]
            if mm is not None:
                mm.referenced_by = list(refs_by_name[key])
            continue
        target = sut_root / file_part
        defined = _scan_fixture_symbols(target)
        if defined is None:
            mm = FixtureMismatch(
                kind="fixture_file_missing",
                name=symbol_part,
                expected_file=file_part,
                source="reuse",
                referenced_by=list(refs_by_name[key]),
                existing_symbols=[],
            )
            mismatches.append(mm)
            reuse_dedup[key] = mm
            continue
        defined_set = set(defined)
        # `export default <expr>.extend(...)` — consumer's import name is
        # arbitrary, so any `:symbol` reference against this file is valid.
        if DEFAULT_EXPORT_SENTINEL in defined_set:
            reuse_dedup[key] = None
            continue
        if symbol_part not in defined_set:
            mm = FixtureMismatch(
                kind="fixture_symbol_missing",
                name=symbol_part,
                expected_file=file_part,
                source="reuse",
                referenced_by=list(refs_by_name[key]),
                existing_symbols=sorted(
                    s for s in defined_set if s != DEFAULT_EXPORT_SENTINEL
                ),
            )
            mismatches.append(mm)
            reuse_dedup[key] = mm
            continue
        reuse_dedup[key] = None

    files_scanned = len(set(by_file.keys()) | {f for f, _ in reuse_dedup})
    return files_scanned, mismatches


def fixture_mismatches_to_fixture_tasks(
    fixture_mismatches: list[FixtureMismatch],
    plan: dict,
) -> list:
    """Synthesise `_FixtureTask` repairs for `source == "create"` misses.

    Reuse mismatches are NOT auto-patched: a missing reused fixture is a plan
    defect or a stale architect inventory, not something codegen can synthesise
    without inventing test data. The orchestrator surfaces those as hard
    failures.
    """
    from qtea.steps.s08_codegen import _FixtureTask

    plan_lookup: dict[tuple[str, str], dict] = {}
    for tc in plan.get("test_cases") or []:
        for fix in tc.get("fixtures") or []:
            if fix.get("source") != "create":
                continue
            key = (fix.get("at") or "", fix.get("name") or "")
            plan_lookup.setdefault(key, fix)

    tasks: list = []
    seen: set[tuple[str, str]] = set()
    for mm in fixture_mismatches:
        if mm.source != "create":
            continue
        key = (mm.expected_file, mm.name)
        if key in seen:
            continue
        seen.add(key)
        original = plan_lookup.get(key, {})
        tasks.append(_FixtureTask(
            name=mm.name,
            at=mm.expected_file,
            yields=original.get("yields"),
            scope=original.get("scope", "function"),
            depends_on=original.get("depends_on") or [],
        ))
    return tasks


__all__ = [
    "CallSite", "FixtureMismatch", "Mismatch", "ReconciliationResult",
    "fixture_mismatches_to_fixture_tasks",
    "mismatches_to_pom_tasks", "pom_method_signatures",
    "reconcile_codegen", "reconcile_fixtures",
]
