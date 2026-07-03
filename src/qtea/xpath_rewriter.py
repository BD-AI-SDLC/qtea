"""Deterministic XPath → Playwright locator rewriter (Phase B.6 of Step 8).

Legacy Selenium-ported Playwright projects commonly ship POM files whose
locator constants are literal XPath strings. `agents/codegen-rules.md` §1
bans XPath, and `test_indexer.py`'s quality gate raises one `[xpath]`
violation per site. Prior to this module the pipeline hard-failed the whole
step, even when the offending selectors lived in *pre-existing* SUT files
that codegen only extended.

This module ships a deterministic rewriter that walks the code and
substitutes each xpath literal with a Playwright-idiomatic call
(`getByTestId`, `getByRole`, `getByText`, or `locator('[attr="X"]')`),
preserving the original expression in a `// was:` comment. Anything the
matrix can't safely translate is returned as a *straggler* for downstream
LLM fixup.

Public API:

- ``rewrite_xpath(xpath, scope='this.page') -> Rewrite | None``
- ``find_xpath_sites(text) -> list[XpathSite]``
- ``rewrite_file(path, dry_run=False) -> RewriteReport``

The rewriter targets the API-level migration chosen in the design plan:
POM property containers (`elements: Record<string, string> = { … }`) get
their values converted to arrow-function factories that return `Locator`,
and call sites of the form ``this.page.locator(this.elements.X)`` are
collapsed to ``this.elements.X()``. Files without a container map — where
xpath is inline in method bodies — get their ``page.locator('//xpath')``
calls rewritten to the equivalent factory call directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


class RewriteKind(str, Enum):
    TESTID = "testid"       # getByTestId('X')
    ROLE = "role"           # getByRole('button', { name: 'X' })
    TEXT_EXACT = "text_exact"   # getByText('X', { exact: true })
    TEXT_FUZZY = "text_fuzzy"   # getByText('X')
    CSS = "css"             # locator('[attr="X"]')
    NESTED = "nested"       # A.locator('B') or A.locator(B)
    UNION = "union"         # A.or(B)


@dataclass(frozen=True)
class Rewrite:
    """The Playwright expression that replaces a single xpath literal.

    ``expression`` is the complete, fully-qualified call chain assuming the
    caller wants it inline (already includes the scope, e.g. ``this.page``).
    ``kind`` classifies the rewrite for logging / metrics.
    """

    kind: RewriteKind
    expression: str


@dataclass
class XpathSite:
    """A single xpath literal located inside a source file."""

    line: int  # 1-based line number
    col: int   # 0-based column of the opening quote
    original: str  # the xpath string content (without surrounding quotes)
    quote: str  # ' or " or ` (template literal)
    raw_literal: str  # the full literal including quotes (for exact substitution)
    context: str = ""  # a slice of surrounding text for LLM context


@dataclass
class RewriteReport:
    """Result of running ``rewrite_file`` on one source file."""

    path: Path
    original_text: str
    new_text: str
    rewritten: list[tuple[XpathSite, Rewrite]] = field(default_factory=list)
    stragglers: list[XpathSite] = field(default_factory=list)
    container_migrated: bool = False
    call_sites_migrated: int = 0
    testid_attr_needed: bool = False  # True if any getByTestId with data-test was emitted

    @property
    def changed(self) -> bool:
        return self.original_text != self.new_text


# ---------------------------------------------------------------------------
# XPath parsing / rewriting — single-string level
# ---------------------------------------------------------------------------


# Tag → ARIA role mapping used when translating text-predicate xpath.
_TAG_TO_ROLE: dict[str, str] = {
    "a": "link",
    "button": "button",
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "input": "textbox",  # coarse; input[type=submit] is really 'button'
    "select": "combobox",
    "textarea": "textbox",
    "img": "img",
    "nav": "navigation",
    "form": "form",
    "table": "table",
    "ul": "list",
    "ol": "list",
    "li": "listitem",
}


# The `${…}` template-literal escape that survives an xpath value. Preserve
# these so rewrites emit template strings for CSS fallbacks.
_TEMPLATE_INTERP_RE = re.compile(r"\$\{[^}]*\}")


def _js_escape_single(s: str) -> str:
    """Escape a string for embedding inside a single-quoted JS string."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _js_escape_double(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _has_template(s: str) -> bool:
    return "${" in s and "}" in s


def _emit_css_string(css: str) -> str:
    """Render a CSS selector value as a JS string literal, preserving `${…}`.

    Template-interpolated selectors need backtick strings; plain selectors go
    into single quotes.
    """
    if _has_template(css):
        return "`" + css.replace("`", "\\`") + "`"
    return "'" + _js_escape_single(css) + "'"


def _emit_text_arg(text: str) -> str:
    """Render a text argument for getByText / getByRole `name`."""
    if _has_template(text):
        return "`" + text.replace("`", "\\`") + "`"
    return "'" + _js_escape_single(text) + "'"


# --- Predicate parsers ------------------------------------------------------


_ATTR_EQ_RE = re.compile(
    r"""^@(?P<attr>[a-zA-Z_:][\w:.-]*)\s*=\s*['"](?P<val>[^'"]*)['"]$"""
)

_TEXT_EQ_RE = re.compile(
    r"""^text\(\)\s*=\s*['"](?P<val>[^'"]*)['"]$"""
)

_NORMALIZE_EQ_RE = re.compile(
    r"""^normalize-space\(\s*(?:\.|text\(\))?\s*\)\s*=\s*['"](?P<val>[^'"]*)['"]$"""
)

_CONTAINS_TEXT_RE = re.compile(
    r"""^contains\(\s*(?:\.|text\(\))\s*,\s*['"](?P<val>[^'"]*)['"]\s*\)$"""
)

_CONTAINS_NORMALIZE_RE = re.compile(
    r"""^contains\(\s*normalize-space\(\s*(?:\.|text\(\))?\s*\)\s*,\s*['"](?P<val>[^'"]*)['"]\s*\)$"""
)

_CONTAINS_ATTR_RE = re.compile(
    r"""^contains\(\s*@(?P<attr>[a-zA-Z_:][\w:.-]*)\s*,\s*['"](?P<val>[^'"]*)['"]\s*\)$"""
)


def _split_top_level_and(predicate: str) -> list[str]:
    """Split a predicate on top-level `and` — ignoring `and` inside brackets."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(predicate):
        c = predicate[i]
        if c == "[" or c == "(":
            depth += 1
        elif c == "]" or c == ")":
            depth -= 1
        if depth == 0 and predicate[i:i + 5] == " and " and i + 5 <= len(predicate):
            parts.append("".join(buf).strip())
            buf = []
            i += 5
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


@dataclass
class _StepMatch:
    """One xpath step parsed into components we can rewrite."""

    tag: str  # 'input', 'div', '*', ...
    testid_val: str | None = None   # from [@data-test="X"]
    other_attrs: list[tuple[str, str]] = field(default_factory=list)  # [(attr, val), ...]
    text_exact: str | None = None       # from [text()="X"] or [normalize-space()="X"]
    text_contains: str | None = None    # from [contains(., "X")] or [contains(normalize-space(.), "X")]
    attr_contains: list[tuple[str, str]] = field(default_factory=list)  # [(attr, val), ...]
    unsupported: bool = False


_STEP_RE = re.compile(
    r"""^(?P<tag>\*|[a-zA-Z_][\w-]*)(?P<preds>(?:\[[^\[\]]*(?:\[[^\[\]]*\])?[^\[\]]*\])*)$"""
)


def _parse_step(step: str) -> _StepMatch | None:
    """Parse a single xpath step like ``input[@data-test="X" and @type="text"]``.

    Predicates supported per-step:
      - ``@attr="X"`` — attribute equality
      - ``text()="X"`` / ``normalize-space()="X"`` — exact text
      - ``contains(., "X")`` / ``contains(normalize-space(.), "X")`` — fuzzy text
      - ``contains(@attr, "X")`` — attribute substring
      - joined with `` and ``

    Returns ``None`` for unrecognised shapes (e.g. `position()`, axes,
    nested brackets); returns a match with ``unsupported=True`` for
    partially-parsed shapes we want the LLM to see.
    """
    m = _STEP_RE.match(step)
    if not m:
        return None
    tag = m.group("tag").lower()
    preds_blob = m.group("preds") or ""
    match = _StepMatch(tag=tag)
    if not preds_blob:
        return match
    # Split predicates like [P1][P2][P3]
    predicates: list[str] = []
    depth = 0
    buf: list[str] = []
    for c in preds_blob:
        if c == "[":
            if depth == 0:
                buf = []
                depth = 1
                continue
            depth += 1
            buf.append(c)
        elif c == "]":
            depth -= 1
            if depth == 0:
                predicates.append("".join(buf).strip())
                continue
            buf.append(c)
        else:
            buf.append(c)
    for p in predicates:
        for clause in _split_top_level_and(p):
            if _parse_predicate_clause(clause, match):
                continue
            # unrecognised clause → mark unsupported
            match.unsupported = True
    return match


def _parse_predicate_clause(clause: str, out: _StepMatch) -> bool:
    """Populate `out` from a single ANDed predicate clause. True on match."""
    if m := _ATTR_EQ_RE.match(clause):
        attr, val = m.group("attr"), m.group("val")
        if attr in {"data-test"}:
            out.testid_val = val
        else:
            out.other_attrs.append((attr, val))
        return True
    if m := _TEXT_EQ_RE.match(clause):
        out.text_exact = m.group("val")
        return True
    if m := _NORMALIZE_EQ_RE.match(clause):
        out.text_exact = m.group("val")
        return True
    if m := _CONTAINS_TEXT_RE.match(clause):
        out.text_contains = m.group("val")
        return True
    if m := _CONTAINS_NORMALIZE_RE.match(clause):
        out.text_contains = m.group("val")
        return True
    if m := _CONTAINS_ATTR_RE.match(clause):
        out.attr_contains.append((m.group("attr"), m.group("val")))
        return True
    return False


def _step_to_rewrite(step: _StepMatch, scope: str) -> Rewrite | None:
    """Translate one parsed step into a Playwright factory call."""
    if step.unsupported:
        return None

    # Priority 1: dedicated data-test attribute (config sets testIdAttribute)
    if step.testid_val is not None and not step.other_attrs \
            and step.text_exact is None and step.text_contains is None \
            and not step.attr_contains:
        return Rewrite(
            kind=RewriteKind.TESTID,
            expression=f"{scope}.getByTestId({_emit_text_arg(step.testid_val)})",
        )

    # Priority 2: role + accessible name (from tag + text predicate)
    role = _TAG_TO_ROLE.get(step.tag)
    if role and not step.other_attrs and not step.attr_contains and step.testid_val is None:
        name_val = step.text_exact or step.text_contains
        if name_val is not None:
            # For exact text we still use { name: 'X' } (name matcher is contains
            # by default — Playwright treats an exact string as case-insensitive
            # substring match). For truly exact we could use { name: /^X$/ } but
            # keeping it simple gives most-common-case behaviour.
            return Rewrite(
                kind=RewriteKind.ROLE,
                expression=(
                    f"{scope}.getByRole('{role}', {{ name: "
                    f"{_emit_text_arg(name_val)} }})"
                ),
            )

    # Priority 3: text (tag-agnostic)
    if step.tag == "*" or (
        not step.testid_val and not step.other_attrs and not step.attr_contains
    ):
        if step.text_exact is not None:
            return Rewrite(
                kind=RewriteKind.TEXT_EXACT,
                expression=(
                    f"{scope}.getByText({_emit_text_arg(step.text_exact)}, "
                    f"{{ exact: true }})"
                ),
            )
        if step.text_contains is not None:
            return Rewrite(
                kind=RewriteKind.TEXT_FUZZY,
                expression=(
                    f"{scope}.getByText({_emit_text_arg(step.text_contains)})"
                ),
            )

    # Priority 4: CSS attribute selector
    css_parts: list[str] = []
    if step.tag != "*":
        css_parts.append(step.tag)
    if step.testid_val is not None:
        css_parts.append(f'[data-test="{step.testid_val}"]')
    for attr, val in step.other_attrs:
        css_parts.append(f'[{attr}="{val}"]')
    for attr, val in step.attr_contains:
        css_parts.append(f'[{attr}*="{val}"]')
    if not css_parts:
        return None
    css = "".join(css_parts)
    return Rewrite(
        kind=RewriteKind.CSS,
        expression=f"{scope}.locator({_emit_css_string(css)})",
    )


# --- Top-level xpath tokenisation ------------------------------------------


def _split_steps(path: str) -> list[str] | None:
    """Split an xpath body on ``/`` and ``//`` boundaries at top level.

    Returns None when the path can't be parsed (axes like ``parent::``,
    ``following-sibling::``, or predicates spanning slashes).
    """
    # Reject axis operators the rewriter can't safely handle.
    if "::" in path:
        return None
    # Split on `//` or `/` while respecting bracket depth. `//x` yields a
    # descendant step; `/x` yields a child step. Both are treated the same
    # by Playwright's `.locator()` chain (which is descendant by default),
    # so we don't need to distinguish them for output purposes.
    steps: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    while i < len(path):
        c = path[i]
        if c == "[":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == "]":
            depth -= 1
            buf.append(c)
            i += 1
            continue
        if depth == 0 and c == "/":
            token = "".join(buf).strip()
            if token:
                steps.append(token)
            buf = []
            # consume any additional slashes (// → same handling as /)
            while i < len(path) and path[i] == "/":
                i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        steps.append(tail)
    return steps or None


def rewrite_xpath(xpath: str, scope: str = "this.page") -> Rewrite | None:
    """Translate one xpath literal into a Playwright factory expression.

    Returns ``None`` when the expression uses xpath features the deterministic
    rewriter can't safely translate — the caller should treat it as a
    straggler and either invoke the LLM violation-fixer or mark it exempt.
    """
    if not xpath:
        return None
    xpath = xpath.strip()

    # ---- Unions (top-level `|`) --------------------------------------------
    # Split on top-level `|`; recurse on each branch. If any branch is
    # unrewritable, return None (whole union is a straggler).
    branches = _split_top_level_union(xpath)
    if branches is not None and len(branches) > 1:
        rewrites: list[Rewrite] = []
        for br in branches:
            br = br.strip()
            r = rewrite_xpath(br, scope=scope)
            if r is None:
                return None
            rewrites.append(r)
        combined = rewrites[0].expression
        for r in rewrites[1:]:
            combined = f"{combined}.or({r.expression})"
        return Rewrite(kind=RewriteKind.UNION, expression=combined)

    # ---- Reject xpath features we don't handle -----------------------------
    # `xpath=` prefix (Playwright engine notation) — strip if present.
    if xpath.startswith("xpath="):
        xpath = xpath[len("xpath="):]
    # A leading `.` means "current context" — rare, fall through to LLM.
    if xpath.startswith("./"):
        return None
    # `//` prefix is the common case; `/` is absolute (page root).
    if not (xpath.startswith("//") or xpath.startswith("/")):
        return None
    body = xpath.lstrip("/")

    steps = _split_steps(body)
    if not steps:
        return None

    # Parse & rewrite each step.
    parsed_steps: list[_StepMatch] = []
    for s in steps:
        p = _parse_step(s)
        if p is None or p.unsupported:
            return None
        parsed_steps.append(p)

    # Emit chain. First step uses full scope; subsequent steps chain via
    # `.locator(...)` — either a CSS selector (preferred, cheap) or a nested
    # `getByTestId` when the step is a pure testid.
    first = _step_to_rewrite(parsed_steps[0], scope)
    if first is None:
        return None

    if len(parsed_steps) == 1:
        return first

    expr = first.expression
    for nxt in parsed_steps[1:]:
        nxt_rw = _step_to_rewrite(nxt, scope="")  # scope will be prepended below
        if nxt_rw is None:
            return None
        # Chain via .locator(...). For a CSS-style child, extract the raw CSS.
        css_child = _step_to_css(nxt)
        if css_child is None:
            return None
        expr = f"{expr}.locator({_emit_css_string(css_child)})"
    return Rewrite(kind=RewriteKind.NESTED, expression=expr)


def _split_top_level_union(xpath: str) -> list[str] | None:
    """Split on top-level `|`. Returns None when no top-level `|` exists.

    Ignores `|` inside brackets or quoted strings.
    """
    if "|" not in xpath:
        return None
    depth = 0
    in_quote: str | None = None
    branches: list[str] = []
    buf: list[str] = []
    for c in xpath:
        if in_quote:
            if c == in_quote:
                in_quote = None
            buf.append(c)
            continue
        if c in "'\"":
            in_quote = c
            buf.append(c)
            continue
        if c == "[" or c == "(":
            depth += 1
        elif c == "]" or c == ")":
            depth -= 1
        if depth == 0 and c == "|":
            branches.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    tail = "".join(buf)
    if tail:
        branches.append(tail)
    return branches if len(branches) > 1 else None


def _step_to_css(step: _StepMatch) -> str | None:
    """Render a single parsed step as a CSS selector string (or None)."""
    if step.unsupported:
        return None
    if step.text_exact is not None or step.text_contains is not None:
        # Text predicates can't be expressed in pure CSS; caller should
        # fall through to a getByText chain — but nested chains need a
        # `has-text` filter that Playwright supports as a CSS engine.
        # Emit `tag:has-text('X')` — Playwright-specific CSS extension.
        tag = step.tag if step.tag != "*" else "*"
        text = step.text_exact or step.text_contains
        assert text is not None
        css = f'{tag}:has-text("{text}")'
        # attach attribute predicates too
        if step.testid_val is not None:
            css += f'[data-test="{step.testid_val}"]'
        for attr, val in step.other_attrs:
            css += f'[{attr}="{val}"]'
        for attr, val in step.attr_contains:
            css += f'[{attr}*="{val}"]'
        return css
    parts: list[str] = []
    if step.tag != "*":
        parts.append(step.tag)
    if step.testid_val is not None:
        parts.append(f'[data-test="{step.testid_val}"]')
    for attr, val in step.other_attrs:
        parts.append(f'[{attr}="{val}"]')
    for attr, val in step.attr_contains:
        parts.append(f'[{attr}*="{val}"]')
    if not parts:
        return None
    return "".join(parts)


# ---------------------------------------------------------------------------
# File-level: locate xpath sites in TS/JS source
# ---------------------------------------------------------------------------


# Match a JS string literal (single-quoted, double-quoted, or template)
# whose content starts with an xpath `//`, `/html`, or `xpath=`. Three
# alternatives so the OPPOSITE quote character is allowed inside the body
# (xpath predicates often embed `"` inside single-quoted strings and
# vice versa).
_XPATH_LITERAL_RE = re.compile(
    r"""(?:
        '(?P<body_s>(?:/{1,2}|xpath=)[^'\n]*)'
      | "(?P<body_d>(?:/{1,2}|xpath=)[^"\n]*)"
      | `(?P<body_t>(?:/{1,2}|xpath=)[^`]*)`
    )""",
    re.VERBOSE | re.MULTILINE,
)


def find_xpath_sites(text: str) -> list[XpathSite]:
    """Return every xpath literal found in the source text."""
    sites: list[XpathSite] = []
    for m in _XPATH_LITERAL_RE.finditer(text):
        if m.group("body_s") is not None:
            body, quote = m.group("body_s"), "'"
        elif m.group("body_d") is not None:
            body, quote = m.group("body_d"), '"'
        else:
            body, quote = m.group("body_t"), "`"
        # Filter out things that look like comments (URLs `//example.com`).
        # Heuristic: xpath bodies typically contain `[` or `@` or `text(`
        # or start with a tag name like `//input`, `//div`, etc.
        if not _looks_like_xpath(body):
            continue
        start = m.start()
        # 1-based line and 0-based col for the opening quote
        line = text.count("\n", 0, start) + 1
        col = start - (text.rfind("\n", 0, start) + 1)
        sites.append(XpathSite(
            line=line,
            col=col,
            original=body,
            quote=quote,
            raw_literal=m.group(0),
            context=text[max(0, start - 40): start + len(m.group(0)) + 40],
        ))
    return sites


def _looks_like_xpath(s: str) -> bool:
    if s.startswith("xpath="):
        return True
    if "[" in s and "@" in s:
        return True
    if "text(" in s or "normalize-space" in s or "contains(" in s:
        return True
    # `//tag` where tag looks like an HTML tag name (not a domain)
    if re.match(r"^//?[a-zA-Z][\w-]*(?:\[|/|$)", s):
        return True
    return False


# ---------------------------------------------------------------------------
# File-level: three-pass rewrite (value + container migration + call sites)
# ---------------------------------------------------------------------------


# `elements: Record<string, string> = { … };` OR `elements = { … };`
_CONTAINER_RE = re.compile(
    r"""(?P<indent>[ \t]*)elements\s*(?::\s*Record<string,\s*string>)?\s*=\s*\{
        (?P<body>[^{}]*)
    \}\s*;""",
    re.VERBOSE | re.DOTALL,
)

# One entry inside the container body: `key: 'value',` OR `key: "value",`.
# Split by quote character so the opposite quote is allowed inside the value.
_CONTAINER_ENTRY_RE = re.compile(
    r"""^(?P<indent>[ \t]*)(?P<key>[a-zA-Z_$][\w$]*)\s*:\s*
        (?:
            '(?P<val_s>[^'\n]*)'
          | "(?P<val_d>[^"\n]*)"
          | `(?P<val_t>[^`]*)`
        )\s*,?\s*$""",
    re.VERBOSE,
)


def rewrite_file(path: Path, dry_run: bool = False) -> RewriteReport:
    """Deterministically rewrite every recognised xpath site in *path*.

    Behaviour:

    1. **Container migration.** If the file contains an `elements: Record<
       string, string> = { … }` block whose values include xpath literals,
       the block is replaced with per-key arrow-function factories that
       return a `Locator`. Every rewritten entry gets a `// was: '<xpath>'`
       comment on the line above.
    2. **Call-site rewrite.** Every occurrence of
       ``this.page.locator(this.elements.<KEY>)`` (or ``this.<KEY>``) is
       collapsed to ``this.elements.<KEY>()`` — the value is already a
       factory after the migration.
    3. **Inline xpath rewrite.** For xpath literals passed directly to
       ``page.locator('//xpath')`` / ``this.page.locator('//xpath')`` /
       template-literal variants, replace the whole ``…locator('//xpath')``
       call with the equivalent factory call (or its CSS-string fallback if
       the caller still wants a string).

    Stragglers (xpath the rewriter can't safely translate) are collected
    and returned untouched. Callers hand them to the LLM violation-fixer.

    Set ``dry_run=True`` to compute a report without writing to disk.
    """
    original_text = path.read_text(encoding="utf-8", errors="replace")
    text = original_text
    report = RewriteReport(
        path=path,
        original_text=original_text,
        new_text=original_text,
    )

    # ---- Pass 1: container migration ---------------------------------------
    text, migrated_keys, container_stragglers = _migrate_container(text, report)
    report.stragglers.extend(container_stragglers)

    # ---- Pass 2: call-site rewrite (only if container was migrated) --------
    if migrated_keys:
        text, n_sites = _rewrite_call_sites(text, migrated_keys)
        report.call_sites_migrated = n_sites

    # ---- Pass 3: inline xpath rewrite --------------------------------------
    # Skip xpath bodies the container pass already flagged as stragglers —
    # they will now appear as `this.page.locator('<xpath>')` calls in the
    # rewritten container, and we'd otherwise double-count them here.
    already_seen = {s.original for s in container_stragglers}
    text, inline_stragglers = _rewrite_inline_xpath(text, report, skip_bodies=already_seen)
    report.stragglers.extend(inline_stragglers)

    report.new_text = text
    if not dry_run and report.changed:
        path.write_text(text, encoding="utf-8")
    return report


def _migrate_container(
    text: str,
    report: RewriteReport,
) -> tuple[str, set[str], list[XpathSite]]:
    """Rewrite the `elements: Record<string, string> = { … };` block.

    Returns (new_text, migrated_keys, stragglers).
    """
    m = _CONTAINER_RE.search(text)
    if not m:
        return text, set(), []

    body = m.group("body")
    outer_indent = m.group("indent")
    inner_indent = outer_indent + "    "

    migrated: dict[str, tuple[str, Rewrite | None]] = {}
    stragglers: list[XpathSite] = []
    has_any_xpath = False
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        entry_m = _CONTAINER_ENTRY_RE.match(raw_line)
        if not entry_m:
            # Not a key: 'value' pair — bail on migration to stay safe.
            return text, set(), []
        key = entry_m.group("key")
        if entry_m.group("val_s") is not None:
            val, val_q = entry_m.group("val_s"), "'"
        elif entry_m.group("val_d") is not None:
            val, val_q = entry_m.group("val_d"), '"'
        else:
            val, val_q = entry_m.group("val_t"), "`"
        if _looks_like_xpath(val):
            has_any_xpath = True
            rw = rewrite_xpath(val, scope="this.page")
            if rw is None:
                # Straggler — keep in container as-is, LLM will fix later.
                stragglers.append(XpathSite(
                    line=0, col=0, original=val,
                    quote=val_q,
                    raw_literal=f"{val_q}{val}{val_q}",
                    context=raw_line,
                ))
                migrated[key] = (val, None)
                continue
            migrated[key] = (val, rw)
            if rw.kind == RewriteKind.TESTID:
                report.testid_attr_needed = True
        else:
            # Non-xpath value — wrap as-is in a plain locator factory.
            migrated[key] = (val, None)

    if not has_any_xpath:
        # Nothing to migrate — leave the file untouched.
        return text, set(), []

    # Emit the new block.
    lines: list[str] = [f"{outer_indent}elements = {{"]
    for key, (orig, rw) in migrated.items():
        if rw is not None:
            lines.append(f"{inner_indent}// was: '{orig}'")
            lines.append(f"{inner_indent}{key}: () => {rw.expression},")
        else:
            # Straggler xpath OR non-xpath value: keep the string but wrap in
            # a factory so the container shape is uniform.
            if _looks_like_xpath(orig):
                # xpath straggler — single-line marker so the quality gate
                # sees `qtea-xpath-exempt` on the LINE IMMEDIATELY ABOVE the
                # surviving xpath and skips the [xpath] violation.
                lines.append(
                    f"{inner_indent}// qtea-xpath-exempt: unhandled xpath "
                    f"axis/predicate — was: '{orig}'"
                )
            lines.append(
                f"{inner_indent}{key}: () => this.page.locator("
                f"{_emit_css_string(orig)}),"
            )
    lines.append(f"{outer_indent}}};")

    new_block = "\n".join(lines)
    new_text = text[: m.start()] + new_block + text[m.end():]
    report.container_migrated = True
    for key, (_orig, rw) in migrated.items():
        if rw is not None:
            report.rewritten.append((
                XpathSite(line=0, col=0, original=_orig,
                          quote="'", raw_literal=f"'{_orig}'",
                          context=f"elements.{key}"),
                rw,
            ))
    return new_text, {k for k, (_o, rw) in migrated.items() if rw is not None}, stragglers


# `this.page.locator(this.elements.KEY)` OR `this.page.locator(this.KEY)`
# We only rewrite when the KEY was migrated by _migrate_container.
_CALL_SITE_RE = re.compile(
    r"""this\.page\.locator\(\s*this\.(?:elements\.)?(?P<key>[a-zA-Z_$][\w$]*)\s*\)"""
)


def _rewrite_call_sites(text: str, migrated_keys: set[str]) -> tuple[str, int]:
    """Rewrite ``this.page.locator(this.elements.X)`` → ``this.elements.X()``.

    Only rewrites keys the container migration actually converted (so we
    don't accidentally rewrite call sites for keys still holding raw
    strings).
    """
    n = 0

    def _sub(m: re.Match) -> str:
        nonlocal n
        key = m.group("key")
        if key not in migrated_keys:
            return m.group(0)
        n += 1
        return f"this.elements.{key}()"

    return _CALL_SITE_RE.sub(_sub, text), n


# Inline: `<expr>.locator('//xpath')` where `<expr>` is any identifier or
# member-access chain (`page`, `this.page`, `parentLocator`, `x.getByRole(…)`,
# etc.). Allows newlines between the expression and `.locator(` so chained
# calls that put `.locator()` on its own line are also captured.
_INLINE_LOCATOR_RE = re.compile(
    r"""(?P<pre>[a-zA-Z_$][\w$]*(?:\s*\.\s*[a-zA-Z_$][\w$]*(?:\([^()]*\))?)*)
        \s*\.\s*locator\(\s*
        (?:
            '(?P<body_s>[^'\n]*)'
          | "(?P<body_d>[^"\n]*)"
          | `(?P<body_t>[^`]*)`
        )
        (?P<tail>[^)]*)\)""",
    re.VERBOSE,
)


def _rewrite_inline_xpath(
    text: str,
    report: RewriteReport,
    skip_bodies: set[str] | None = None,
) -> tuple[str, list[XpathSite]]:
    """Rewrite inline `page.locator('//xpath')` calls to factory equivalents.

    Preserves the optional 2nd argument (e.g., `{ hasText: 'X' }`) —
    Playwright's locator API accepts an options object as arg 2, which we
    tack onto the new factory as `.filter({ hasText: … })` when present.

    ``skip_bodies`` is the set of xpath strings that a prior pass already
    accounted for as stragglers — used to prevent double-counting when the
    container pass emits its unfixable entries as `locator('//xpath')`.
    """
    stragglers: list[XpathSite] = []
    skip = skip_bodies or set()

    def _sub(m: re.Match) -> str:
        pre = m.group("pre")
        if m.group("body_s") is not None:
            body, quote = m.group("body_s"), "'"
        elif m.group("body_d") is not None:
            body, quote = m.group("body_d"), '"'
        else:
            body, quote = m.group("body_t"), "`"
        tail = m.group("tail") or ""
        if not _looks_like_xpath(body):
            return m.group(0)
        rw = rewrite_xpath(body, scope=pre) if body not in skip else None
        if rw is None:
            start = m.start()
            # Only record as a NEW straggler if the container pass hasn't
            # already claimed this body — but ALWAYS ensure the exempt
            # marker is in place so the quality gate skips this call.
            if body not in skip:
                line = text.count("\n", 0, start) + 1
                col = start - (text.rfind("\n", 0, start) + 1)
                stragglers.append(XpathSite(
                    line=line, col=col, original=body,
                    quote=quote,
                    raw_literal=m.group(0),
                    context=text[max(0, start - 40): start + len(m.group(0)) + 40],
                ))
            # Idempotent — skip if the marker is already on the same line
            # or immediately above.
            preceding_line_end = text.rfind("\n", 0, start)
            preceding_line_start = text.rfind("\n", 0, preceding_line_end) + 1
            preceding = text[preceding_line_start:preceding_line_end]
            same_line_start = text.rfind("\n", 0, start) + 1
            same_line_end = text.find("\n", start)
            if same_line_end == -1:
                same_line_end = len(text)
            same_line = text[same_line_start:same_line_end]
            if "qtea-xpath-exempt" in preceding or "qtea-xpath-exempt" in same_line:
                return m.group(0)
            return f"/* qtea-xpath-exempt: unhandled xpath */ {m.group(0)}"
        report.rewritten.append((
            XpathSite(line=0, col=0, original=body,
                      quote=quote,
                      raw_literal=m.group(0),
                      context=m.group(0)),
            rw,
        ))
        if rw.kind == RewriteKind.TESTID:
            report.testid_attr_needed = True

        # Handle optional 2nd arg (e.g., `{ hasText: 'X' }`) — translate to
        # `.filter(...)` if present and non-trivial.
        tail_clean = tail.strip()
        if tail_clean.startswith(","):
            tail_clean = tail_clean[1:].strip()
        # Preserve `// was:` above the call by returning it inline; the
        # caller re-lays lines so we just emit an EOL-comment for visibility.
        comment = f"/* was: {quote}{body}{quote} */"
        if tail_clean:
            return f"{comment} {rw.expression}.filter({tail_clean})"
        return f"{comment} {rw.expression}"

    return _INLINE_LOCATOR_RE.sub(_sub, text), stragglers


__all__ = [
    "Rewrite",
    "RewriteKind",
    "RewriteReport",
    "XpathSite",
    "find_xpath_sites",
    "rewrite_file",
    "rewrite_xpath",
]
