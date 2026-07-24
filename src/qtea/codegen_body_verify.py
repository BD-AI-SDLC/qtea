"""Phase A3.5 body verifier — RCA-C from the 2026-07 fix batch.

Verifies that each POM method the pom-extender wrote for an
``kind: "assertion"`` entry actually returns / probes the value that the
plan's ``acceptance_criteria`` demands. Runs after Phase A3 (pom
extender) and Phase A3.5 TBD compliance, before Phase B.5 reconciliation.

Design notes
------------

- **POM-side vs test-side split.** Fix 5 (`pom-assertion`) enforces that
  assertion CALLS live only in test methods. So an assertion-kind method
  on a POM should be a **getter/probe** that RETURNS the raw value; the
  test then calls `expect(await pom.getX()).toBe(EXPECTED)`. This
  verifier therefore has two responsibilities:

    1. Confirm the POM method **references the named locator constants**
       from the criteria (so the getter actually inspects the right
       element).
    2. Confirm the corresponding TEST file contains the expected
       `expect(...)` pattern with the criteria's expected literal or
       symbol.

- **Anti-pattern detection.** Beyond positive-pattern matching, the
  verifier flags shapes we saw drift on the failing run:
    * ``count_drift`` — ``toBeGreaterThanOrEqual(<n+1>)`` when the
      contract says exact ``n``. (The infamous ``>= 4`` when the
      strategy said "3 checkboxes".)
    * ``empty_text_tautology`` — ``.length > 0`` on a text-returning
      call, in a method whose purpose is exact-text assertion.
    * ``nth_arithmetic`` — ``.nth(count - 1)`` / ``.nth(0)`` when a
      criterion names a specific locator constant.

- **Reuses ``codegen_reconcile._js_class_body``** for TS/JS AND Java body
  extraction — brace/paren matching is language-neutral once
  strings/comments are neutralised by ``_js_strip``; only the method-head
  grammar (``_JS_METHOD_HEAD_RE`` vs ``_JAVA_METHOD_HEAD_RE``) differs.
  Depends on the RCA-A ``_js_strip`` fix landing (Fix 1) because unfixed
  extraction would falsely report empty method bodies.

- **Runs AFTER Phase B2 (test-file generation) for every stack**,
  Python included. An earlier revision ran the TS/JS half of this gate at
  Phase A3.5 — before Phase B2 had written the companion test file — so
  the "assertion may live in the POM OR the test" design (Fix 5) could
  never see the test half, and a POM-side assertion would then always
  fail the sibling `pom-assertion` structural gate. Moving the call to
  run once every stack's test files exist (mirroring where Python's half
  already ran) closes that dead end.

- **No retry.** Contract violations of this shape aren't transient —
  the extender needs a different prompt to produce different output.
  The gate returns a violation list; the caller hard-fails Step 8 with
  a diagnostic log so the operator can review the extender persona /
  test-automation-architect criteria.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from qtea.codegen_reconcile import (
    _find_balanced,
    _js_class_body,
    _js_strip,
    _JAVA_LIFECYCLE_NAMES,
    _JAVA_METHOD_HEAD_RE,
    _JS_METHOD_HEAD_RE,
    _LIFECYCLE_NAMES,
)
from qtea.logging_setup import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Positive assertion patterns (Playwright TS/JS test-side)
# ---------------------------------------------------------------------------

# Playwright asserters. We match the ``.<matcher>(...)`` shape directly
# rather than requiring the ``expect(...)`` prefix on the same regex —
# nested parens in ``expect(this.page.locator(...))`` broke a
# ``[^)]+`` capture, and the matcher name alone is a strong-enough
# signal (there is no legitimate reason to see ``.toHaveCount(3)`` in
# a POM/test body EXCEPT as an assertion).
_TS_ASSERT_PATTERNS: dict[str, re.Pattern[str]] = {
    "exact_text": re.compile(
        r"""\.\s*toHaveText\s*\(\s*"""
        r"""(?:(?P<q>['"`])(?P<val>[^\n]*?)(?P=q)|(?P<sym>\w[\w$]*))""",
    ),
    "exact_count": re.compile(
        r"""\.\s*toHaveCount\s*\(\s*"""
        r"""(?:(?P<n>\d+)|(?P<sym>\w[\w$]*))\s*\)""",
    ),
    "exact_attribute": re.compile(
        r"""\.\s*toHaveAttribute\s*\("""
        r"""\s*['"`](?P<attr>\w[-\w]*)['"`]\s*,\s*"""
        r"""(?:['"`](?P<val>[^\n]*?)['"`]|(?P<sym>\w[\w$]*))""",
    ),
    "visible": re.compile(r"""\.\s*toBeVisible\s*\("""),
    "focusable": re.compile(r"""\.\s*toBeFocused\s*\("""),
    "url_matches": re.compile(r"""\.\s*toHaveURL\s*\("""),
    "value_equals": re.compile(
        r"""\.\s*toHaveValue\s*\("""
        r"""\s*(?:['"`](?P<val>[^\n]*?)['"`]|(?P<sym>\w[\w$]*))""",
    ),
    # boundingbox_* checked with a *predicate* rather than a single
    # regex — see ``_verify_boundingbox`` below. Real code separates the
    # ``boundingBox()`` capture from the y-comparison across lines,
    # which a single regex can't cover without over-matching.
    "boundingbox_below": re.compile(r"""boundingBox\s*\(\s*\)"""),
    "boundingbox_above": re.compile(r"""boundingBox\s*\(\s*\)"""),
}

# Bare Jest/Vitest fallback — value-binding fallback when the POM probe
# returns a raw (non-Locator) value and the test asserts on it directly
# (`expect(await pom.getX()).toBe(EXPECTED)`) instead of a Playwright-locator
# matcher. Mirrors Python's bare `assert x == y` / Java's `assertEquals(...)`
# fallback below — TS/JS previously had none, despite the module's own
# design notes describing exactly this pattern.
_TS_ASSERT_EQ_STR = re.compile(
    r"""\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*"""
    r"""(?:(?P<q>['"`])(?P<val>[^\n]*?)(?P=q)|(?P<sym>\w[\w$]*))\s*\)""",
)
_TS_ASSERT_EQ_NUM = re.compile(
    r"""\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*(?P<n>\d+)\s*\)""",
)

# Presence-check regex used ONLY inside the boundingbox verifier.
_BOUNDING_Y_COMPARE = re.compile(
    r"""\.y\s*[<>]|toBeGreaterThan\s*\([^)]*\.y|toBeLessThan\s*\([^)]*\.y"""
)
# Split-probe fallback (TS/JS): when the two element reads live in separate
# Locator probes and the test compares extracted geometry, the `.y` may be
# bound to locals first (`expect(marketingTop).toBeGreaterThan(legalTop)`),
# so the strict `.y`-adjacent pattern above misses it. Accepted ONLY after
# the strict pattern fails AND both boundingBox() calls + both named locators
# are already confirmed present (see the boundingbox branch).
_BOUNDING_Y_COMPARE_LOOSE = re.compile(
    r"""toBeGreaterThan\s*\(|toBeLessThan\s*\("""
)


# ---------------------------------------------------------------------------
# Anti-patterns (defense in depth)
# ---------------------------------------------------------------------------

_TS_ANTI_PATTERNS: dict[str, re.Pattern[str]] = {
    "count_drift_gte": re.compile(
        r"""toBeGreaterThanOrEqual\s*\(\s*(?P<n>\d+)\s*\)""",
    ),
    "count_drift_gt": re.compile(
        r"""toBeGreaterThan\s*\(\s*(?P<n>\d+)\s*\)""",
    ),
    "empty_text_tautology": re.compile(
        r"""\.length\s*[><=]{1,3}\s*0\b""",
    ),
    "nth_arithmetic": re.compile(
        r"""\.nth\s*\(\s*(?:0\b|\w+\s*-\s*\d+)""",
    ),
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BodyViolation:
    """One breach of the acceptance-criteria contract."""

    method: str
    criterion_index: int
    check: str
    message: str

    def format(self, *, pom_file: str | None = None) -> str:
        prefix = f"{pom_file}:" if pom_file else ""
        return (
            f"{prefix}{self.method}(crit#{self.criterion_index},{self.check}): "
            f"{self.message}"
        )


# ---------------------------------------------------------------------------
# Method-body extraction
# ---------------------------------------------------------------------------


def _js_method_bodies(src: str, class_name: str) -> dict[str, str]:
    """Return ``{method_name: body_text}`` for every method in ``class_name``.

    Mirrors ``codegen_reconcile._js_pom_methods`` but returns the body
    text (between ``{`` and ``}``) rather than a signature. Benefits
    directly from the Fix 1 ``_js_strip`` patch.
    """
    stripped = _js_strip(src)
    body = _js_class_body(stripped, class_name)
    if body is None:
        return {}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for m in _JS_METHOD_HEAD_RE.finditer(body):
        name = m.group(1)
        if name in _LIFECYCLE_NAMES or name in seen:
            continue
        open_paren = m.end() - 1
        close_paren = _find_balanced(body, open_paren)
        if close_paren == -1:
            continue
        tail = body[close_paren + 1: close_paren + 32].lstrip()
        if not tail or (tail[0] not in "{:" and not tail.startswith("=>")):
            continue
        # Find the method-body opening `{`. Skip past a return-type
        # annotation of shape `: Promise<{a: b}> {` — a naive first-`{`
        # search would land inside the `Promise<...>` generic. Angle-
        # depth-aware walk: only accept a `{` at angle-depth 0.
        j = _find_method_body_open(body, close_paren)
        if j == -1:
            continue
        body_close = _find_balanced_brace(body, j)
        if body_close == -1:
            continue
        seen.add(name)
        # Return the ORIGINAL src slice, not the stripped one — verifier
        # patterns work on real text (strings + comments) rather than the
        # blanked-out reconcile view. Line offsets between src and stripped
        # are identical after the Fix 1 patch (newlines preserved), so the
        # relative offset inside `body` maps 1:1 into `src`.
        offset = src.find(body[j:body_close + 1])
        if offset == -1:
            # Fallback: use stripped slice — rare when class contains
            # duplicate braces / substrings.
            out[name] = body[j + 1: body_close]
        else:
            out[name] = src[offset + 1: offset + (body_close - j)]
    return out


def _java_method_bodies(src: str, class_name: str) -> dict[str, str]:
    """Return ``{method_name: body_text}`` for every method in ``class_name``.

    Dispatches on the Java method-head grammar (``_JAVA_METHOD_HEAD_RE`` —
    explicit visibility modifier, return type before the name, no arrow-
    function alternative) but reuses the same language-neutral brace/paren
    matching as ``_js_method_bodies``. Unlike TS, Java has no return-type-
    after-colon ambiguity to skip past; the only thing between the params
    `)` and the body `{` is an optional `throws X, Y` clause (or a bare
    `;` for an abstract/interface declaration, which is skipped).
    """
    stripped = _js_strip(src)
    body = _js_class_body(stripped, class_name)
    if body is None:
        return {}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for m in _JAVA_METHOD_HEAD_RE.finditer(body):
        name = m.group(1)
        if name in _JAVA_LIFECYCLE_NAMES or name in seen:
            continue
        open_paren = m.end() - 1
        close_paren = _find_balanced(body, open_paren)
        if close_paren == -1:
            continue
        j = close_paren + 1
        while j < len(body) and body[j] not in "{;":
            j += 1
        if j >= len(body) or body[j] != "{":
            continue  # abstract/interface signature (`;`) — no body
        body_close = _find_balanced_brace(body, j)
        if body_close == -1:
            continue
        seen.add(name)
        # Return the ORIGINAL src slice — see the identical comment in
        # `_js_method_bodies` for why (verifier patterns need real text).
        offset = src.find(body[j:body_close + 1])
        if offset == -1:
            out[name] = body[j + 1: body_close]
        else:
            out[name] = src[offset + 1: offset + (body_close - j)]
    return out


def _find_balanced_brace(src: str, open_idx: int) -> int:
    """Index of the `}` matching the `{` at ``open_idx``, or -1.

    Assumes strings/comments have been neutralised by ``_js_strip``.
    """
    if open_idx >= len(src) or src[open_idx] != "{":
        return -1
    depth = 0
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _find_method_body_open(src: str, params_close_idx: int) -> int:
    """Index of the method-body opening `{` that follows the params `)` at
    ``params_close_idx``, or -1 if the walk falls off the end.

    Skips past a TS return-type annotation of the form ``: Promise<{...}>``
    by tracking angle-bracket depth — a naive scan for the first `{` would
    land inside the ``Promise<{...}>`` generic and treat the return-type
    body as the method body. Also handles bare `: {a: b} {` object-literal
    return types by brace-matching the type before continuing.

    Assumes strings/comments have been neutralised by ``_js_strip``.
    """
    i = params_close_idx + 1
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    if i >= len(src):
        return -1
    # Fast path: no return-type annotation.
    if src[i] == "{":
        return i
    if src[i] != ":":
        return -1
    i += 1
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    # Object-literal return type: `: {a: b} {` — skip past the matching `}`
    # first, otherwise the walker below would exit too early.
    if i < len(src) and src[i] == "{":
        close = _find_balanced_brace(src, i)
        if close == -1:
            return -1
        i = close + 1
    angle_depth = 0
    paren_depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "<":
            angle_depth += 1
        elif ch == ">":
            if angle_depth > 0:
                angle_depth -= 1
        elif ch == "(":
            paren_depth += 1
        elif ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
        elif ch == "{" and angle_depth == 0 and paren_depth == 0:
            return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# Per-criterion checkers
# ---------------------------------------------------------------------------


def _match_expected(
    m: re.Match[str], expected_literal, expected_symbol,
) -> bool:
    """Does the regex match's value/symbol group match either the
    expected literal or the expected symbol constant?"""
    val = m.groupdict().get("val")
    sym = m.groupdict().get("sym")
    if expected_symbol and sym == expected_symbol:
        return True
    if expected_literal is not None and val == str(expected_literal):
        return True
    return False


# `NAME = <int>` declaration — covers TS/JS `const NAME = 1`, Python
# `NAME = 1`, and Java `... NAME = 1` (any leading type keyword is ignored
# because we anchor on the name). Used to const-fold a named count constant.
_COUNT_CONST_DECL_TMPL = r"\b{name}\s*=\s*(\d+)\b"


def _resolve_count_literal(text: str, symbol: str) -> int | None:
    """Fold a named numeric constant to its int value by scanning ``text``
    for a ``symbol = <int>`` declaration. Returns None when the symbol is
    not declared as a bare integer.

    Deliberately narrow (top-level ``NAME = <literal>`` only): imported
    constants, arithmetic, and enum members are out of scope — a resolved
    match is a strong positive signal, an unresolved one falls through to
    the normal "missing/wrong count" path rather than passing silently."""
    m = re.search(_COUNT_CONST_DECL_TMPL.format(name=re.escape(symbol)), text)
    return int(m.group(1)) if m else None


def _count_matches_expected(
    matches: list[re.Match[str]], combined: str,
    expected: int, expected_symbol,
) -> bool:
    """True when any count-matcher match resolves to ``expected``.

    A match carries either a numeric group ``n`` (``toHaveCount(1)``) or a
    symbol group ``sym`` (``toHaveCount(EXPECTED_COUNT)``). A symbol resolves
    by expected_symbol name-match first, else by const-folding it against
    ``combined`` (POM body + test text). This closes the false-green where a
    correct assertion written with a named constant was misreported as
    "missing toHaveCount(N)" — the count matcher was the only check lacking
    the symbol branch that exact_text / exact_attribute / value_equals have."""
    for m in matches:
        gd = m.groupdict()
        n = gd.get("n")
        if n is not None:
            if int(n) == expected:
                return True
            continue
        sym = gd.get("sym")
        if not sym:
            continue
        if expected_symbol and sym == expected_symbol:
            return True
        folded = _resolve_count_literal(combined, sym)
        if folded is not None and folded == expected:
            return True
    return False


def _count_match_text(m: re.Match[str]) -> str:
    """Render a count match's argument (literal or symbol) for diagnostics."""
    gd = m.groupdict()
    return gd.get("n") or gd.get("sym") or "?"


def _verify_criterion(
    check: str, crit: dict, pom_body: str, test_body: str,
    sibling_pom_text: str = "",
) -> str | None:
    """Return an error message, or None when the criterion is satisfied.

    The verifier looks in BOTH the POM method body and the test body,
    because Fix 5 pushes assertions to the test layer while the POM
    keeps the locator reference. Either half satisfying the criterion
    is enough (the pom-assertion rule separately enforces where
    ``expect()`` lives).

    ``sibling_pom_text`` (positional checks only): the concatenated bodies
    of the OTHER agent-authored POM probes this test calls. A positional
    check may split its two element reads across a ``kind:"assertion"``
    anchor probe and a ``kind:"query"`` sibling probe (both returning
    ``Locator``); the anchor body then references only one of the two named
    locators. Unioning the sibling body lets the positional oracle see both
    named locators + both ``boundingBox()`` calls across the pair."""
    expected_literal = crit.get("expected_literal")
    expected_symbol = crit.get("expected_symbol")
    locator = crit.get("locator")
    ref_locator = crit.get("reference_locator")

    pat = _TS_ASSERT_PATTERNS.get(check)
    if pat is None:
        # `custom` and any future kinds — cannot machine-verify. Trust the
        # test-automation-architect / extender output; the operator reviews HITL if
        # ``kind: custom`` fires.
        return None

    combined = pom_body + "\n" + test_body

    if check == "exact_count":
        expected = int(expected_literal) if expected_literal is not None else None
        drift = _TS_ANTI_PATTERNS["count_drift_gte"].search(combined) or \
                _TS_ANTI_PATTERNS["count_drift_gt"].search(combined)
        if drift and expected is not None:
            found_n = int(drift.group("n"))
            if found_n > expected:
                return (
                    f"contract requires exact count {expected} but body emits "
                    f"{drift.group(0)} — count-drift hallucination "
                    f"(anti-pattern from run 20260708 marketing consent)"
                )
        matches = list(pat.finditer(combined))
        num_asserts = list(_TS_ASSERT_EQ_NUM.finditer(combined))
        if not matches and not num_asserts:
            return f"missing toHaveCount({expected}) assertion"
        if expected is not None and not (
            _count_matches_expected(matches, combined, expected, expected_symbol)
            or any(int(m.group("n")) == expected for m in num_asserts)
        ):
            actual = ", ".join(_count_match_text(m) for m in matches) or "bare assert"
            return f"expected toHaveCount({expected}) — got toHaveCount({actual})"
        return None

    if check == "exact_text":
        matches = list(pat.finditer(combined))
        eq_matches = list(_TS_ASSERT_EQ_STR.finditer(combined))
        if not matches and not eq_matches:
            return (
                f"missing toHaveText assertion for expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        if not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"toHaveText present but does not reference "
                f"{expected_symbol or expected_literal!r}"
            )
        # Anti-pattern: `.length > 0` on the text — tautology
        if _TS_ANTI_PATTERNS["empty_text_tautology"].search(pom_body):
            return (
                "POM body uses `.length > 0` tautology alongside the exact-"
                "text check — remove the tautology"
            )
        return None

    if check == "exact_attribute":
        matches = list(pat.finditer(combined))
        eq_matches = list(_TS_ASSERT_EQ_STR.finditer(combined))
        if not matches and not eq_matches:
            return (
                f"missing toHaveAttribute for expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        # Bind the expected value: a toHaveAttribute that asserts a DIFFERENT
        # value (or no value) does not satisfy an exact-attribute oracle
        # (finding 27 — presence-only checks that ignore the expected value).
        if (expected_literal is not None or expected_symbol) and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"toHaveAttribute present but does not reference expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        return None

    if check == "value_equals":
        matches = list(pat.finditer(combined))
        eq_matches = list(_TS_ASSERT_EQ_STR.finditer(combined))
        if not matches and not eq_matches:
            return "missing toHaveValue assertion"
        if (expected_literal is not None or expected_symbol) and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"toHaveValue present but does not reference expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        return None

    if check in ("visible", "focusable"):
        if pat.search(combined):
            return None
        return f"missing {check} assertion on {locator or '<locator>'}"

    if check == "url_matches":
        if pat.search(combined):
            return None
        return "missing toHaveURL assertion on page"

    if check in ("boundingbox_below", "boundingbox_above"):
        # Positional check: across the POM probe(s) + test, BOTH named
        # locators must be referenced AND a bounding-box y-comparison must
        # exist. `nth_arithmetic` is the anti-pattern (using `.nth(count-1)`
        # instead of the named constant). Include sibling probe bodies so a
        # split anchor+sibling Locator-probe pair is seen as a whole.
        combined_bbox = pom_body + "\n" + sibling_pom_text + "\n" + test_body
        missing_locators = []
        if locator and locator not in combined_bbox:
            missing_locators.append(f"locator={locator}")
        if ref_locator and ref_locator not in combined_bbox:
            missing_locators.append(f"reference_locator={ref_locator}")
        if missing_locators:
            return (
                f"body doesn't reference required locators: "
                f"{', '.join(missing_locators)}. Likely using nth() or "
                f"index arithmetic — use the named constants instead."
            )
        # Two boundingBox() calls AND a y-comparison somewhere. Doesn't
        # require them interleaved — real code assigns the boxes to
        # locals first, compares afterward.
        bbox_calls = _TS_ASSERT_PATTERNS[check].findall(combined_bbox)
        if len(bbox_calls) < 2:
            return (
                f"missing boundingBox().y comparison between "
                f"{locator} and {ref_locator}"
            )
        y_compare = _BOUNDING_Y_COMPARE.search(combined_bbox)
        # Split-probe fallback: geometry extracted into locals, compared
        # without an adjacent `.y`. Safe now that both boxes + both named
        # locators are confirmed present.
        if not y_compare and not _BOUNDING_Y_COMPARE_LOOSE.search(test_body):
            return (
                f"missing boundingBox().y comparison between "
                f"{locator} and {ref_locator}"
            )
        return None

    return None  # unknown check — skip


# ---------------------------------------------------------------------------
# Python (pytest / playwright-python) assertion patterns
#
# Mirrors the TS dispatch but for the snake_case Playwright-Python expect API
# plus bare `assert ... == EXPECTED`. Closes findings 4 & 5: Python was a
# first-class stack with NO semantic assertion verification — only a
# presence-only "has an assert" gate that `assert True` satisfied.
# ---------------------------------------------------------------------------

_PY_ASSERT_PATTERNS: dict[str, re.Pattern[str]] = {
    "exact_text": re.compile(
        r"""\.\s*to_have_text\s*\(\s*"""
        r"""(?:(?P<q>['"])(?P<val>[^\n]*?)(?P=q)|(?P<sym>\w[\w$]*))""",
    ),
    "exact_count": re.compile(
        r"""\.\s*to_have_count\s*\(\s*"""
        r"""(?:(?P<n>\d+)|(?P<sym>\w[\w$]*))\s*\)""",
    ),
    "exact_attribute": re.compile(
        r"""\.\s*to_have_attribute\s*\("""
        r"""\s*['"](?P<attr>\w[-\w]*)['"]\s*,\s*"""
        r"""(?:['"](?P<val>[^\n]*?)['"]|(?P<sym>\w[\w$]*))""",
    ),
    "visible": re.compile(r"""\.\s*to_be_visible\s*\("""),
    "focusable": re.compile(r"""\.\s*to_be_focused\s*\("""),
    "url_matches": re.compile(r"""\.\s*to_have_url\s*\("""),
    "value_equals": re.compile(
        r"""\.\s*to_have_value\s*\("""
        r"""\s*(?:['"](?P<val>[^\n]*?)['"]|(?P<sym>\w[\w$]*))""",
    ),
    "boundingbox_below": re.compile(r"""bounding_box\s*\(\s*\)"""),
    "boundingbox_above": re.compile(r"""bounding_box\s*\(\s*\)"""),
}

# Bare `assert <expr> == <value>` — value-binding fallback when the writer used
# a plain assert instead of a Playwright matcher.
_PY_ASSERT_EQ_STR = re.compile(
    r"""assert\b[^\n=]*==\s*(?:(?P<q>['"])(?P<val>[^\n]*?)(?P=q)|(?P<sym>[A-Za-z_]\w*))""",
)
_PY_ASSERT_EQ_NUM = re.compile(r"""assert\b[^\n=]*==\s*(?P<n>\d+)\b""")
_PY_COUNT_DRIFT = re.compile(r"""(?:>=|>)\s*(?P<n>\d+)\b""")
_PY_TAUTOLOGY = re.compile(
    r"""len\s*\([^)]*\)\s*[><]=?\s*0\b|assert\s+\w[\w.]*\s*$""",
    re.MULTILINE,
)
_PY_BOUNDING_Y = re.compile(
    r"""\[\s*['"]y['"]\s*\]|\.y\b""",
)
# Split-probe fallback (Python): geometry compared via locals, e.g.
# `assert marketing_top > legal_top`. Gated identically to the TS loose form.
_PY_BOUNDING_Y_LOOSE = re.compile(
    r"""assert\b[^\n]*[<>]|to_be_greater_than\s*\(|to_be_less_than\s*\(""",
)
_PY_PW_SEP = "\n\n@@QTEA-PW-SEP@@\n\n"  # placeholder; unused, kept for parity


def _py_method_bodies(src: str, class_name: str) -> dict[str, str]:
    """Return ``{method_name: source_segment}`` for methods of ``class_name``.

    AST-based (robust to nested braces/strings that regex can't handle). Used
    only to feed the same criterion dispatch the TS path uses.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seg = ast.get_source_segment(src, item)
                    if seg:
                        out[item.name] = seg
    return out


def _verify_criterion_py(
    check: str, crit: dict, pom_body: str, test_body: str,
    sibling_pom_text: str = "",
) -> str | None:
    """Python analogue of ``_verify_criterion``. Returns an error or None.

    ``sibling_pom_text`` — see ``_verify_criterion``: sibling probe bodies
    unioned for split-probe positional checks."""
    expected_literal = crit.get("expected_literal")
    expected_symbol = crit.get("expected_symbol")
    locator = crit.get("locator")
    ref_locator = crit.get("reference_locator")
    has_expected = expected_literal is not None or bool(expected_symbol)

    pat = _PY_ASSERT_PATTERNS.get(check)
    if pat is None:
        return None  # custom / unknown — routed to the semantic judge
    combined = pom_body + "\n" + test_body

    if check == "exact_count":
        expected = int(expected_literal) if expected_literal is not None else None
        # Count-drift anti-pattern: `>= n+1` / `> n` when contract says exact n.
        if expected is not None:
            for dm in _PY_COUNT_DRIFT.finditer(combined):
                if int(dm.group("n")) > expected:
                    return (
                        f"contract requires exact count {expected} but body uses "
                        f"'{dm.group(0)}' — count-drift (weaker than exact)"
                    )
        matches = list(pat.finditer(combined))
        num_asserts = list(_PY_ASSERT_EQ_NUM.finditer(combined))
        if not matches and not num_asserts:
            return f"missing to_have_count({expected}) / `== {expected}` assertion"
        if expected is not None:
            ok = _count_matches_expected(
                matches, combined, expected, expected_symbol
            ) or any(int(m.group("n")) == expected for m in num_asserts)
            if not ok:
                return f"count assertion does not check exact value {expected}"
        return None

    if check == "exact_text":
        matches = list(pat.finditer(combined))
        eq_matches = list(_PY_ASSERT_EQ_STR.finditer(combined))
        if not matches and not eq_matches:
            return (
                f"missing to_have_text / `== {expected_symbol or expected_literal!r}` "
                f"assertion"
            )
        if has_expected and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"text assertion present but does not reference expected value "
                f"{expected_symbol or expected_literal!r} (tautology / wrong value)"
            )
        return None

    if check in ("exact_attribute", "value_equals"):
        matches = list(pat.finditer(combined))
        eq_matches = list(_PY_ASSERT_EQ_STR.finditer(combined))
        if not matches and not eq_matches:
            return f"missing {check} assertion for {expected_symbol or expected_literal!r}"
        if has_expected and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"{check} present but does not reference expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        return None

    if check in ("visible", "focusable"):
        if pat.search(combined):
            return None
        return f"missing {check} assertion on {locator or '<locator>'}"

    if check == "url_matches":
        return None if pat.search(combined) else "missing to_have_url assertion"

    if check in ("boundingbox_below", "boundingbox_above"):
        combined_bbox = pom_body + "\n" + sibling_pom_text + "\n" + test_body
        missing_locators = []
        if locator and locator not in combined_bbox:
            missing_locators.append(f"locator={locator}")
        if ref_locator and ref_locator not in combined_bbox:
            missing_locators.append(f"reference_locator={ref_locator}")
        if missing_locators:
            return (
                f"body doesn't reference required locators: "
                f"{', '.join(missing_locators)} — use the named constants"
            )
        bbox_calls = _PY_ASSERT_PATTERNS[check].findall(combined_bbox)
        if len(bbox_calls) < 2:
            return (
                f"missing bounding_box() y-comparison between "
                f"{locator} and {ref_locator}"
            )
        if not _PY_BOUNDING_Y.search(combined_bbox) and not \
                _PY_BOUNDING_Y_LOOSE.search(test_body):
            return (
                f"missing bounding_box() y-comparison between "
                f"{locator} and {ref_locator}"
            )
        return None

    return None


# ---------------------------------------------------------------------------
# Java (Playwright-Java assertThat API + JUnit/TestNG/AssertJ bare asserts)
#
# Mirrors the TS/Python dispatch. Playwright-Java's `assertThat(locator)`
# fluent API is the direct analogue of TS `expect(locator)` / Python
# `expect(locator)`: `hasText` / `hasCount` / `hasAttribute` / `isVisible` /
# `isFocused` / `hasURL` / `hasValue` map 1:1 onto `toHaveText` / `to_have_text`
# etc. Bare `assertEquals(...)` (JUnit 4/5 static import, or `Assertions.
# assertEquals(...)` / `Assert.assertEquals(...)`) is the value-binding
# fallback, mirroring Python's bare `assert x == y`.
# ---------------------------------------------------------------------------

_JAVA_ASSERT_PATTERNS: dict[str, re.Pattern[str]] = {
    "exact_text": re.compile(
        r"""\.\s*hasText\s*\(\s*"""
        r"""(?:"(?P<val>[^\n]*?)"|(?P<sym>[A-Za-z_]\w*))""",
    ),
    "exact_count": re.compile(
        r"""\.\s*hasCount\s*\(\s*"""
        r"""(?:(?P<n>\d+)|(?P<sym>\w[\w$]*))\s*\)""",
    ),
    "exact_attribute": re.compile(
        r"""\.\s*hasAttribute\s*\("""
        r"""\s*"(?P<attr>[\w-]+)"\s*,\s*"""
        r"""(?:"(?P<val>[^\n]*?)"|(?P<sym>[A-Za-z_]\w*))""",
    ),
    "visible": re.compile(r"""\.\s*isVisible\s*\("""),
    "focusable": re.compile(r"""\.\s*isFocused\s*\("""),
    "url_matches": re.compile(r"""\.\s*hasURL\s*\("""),
    "value_equals": re.compile(
        r"""\.\s*hasValue\s*\("""
        r"""\s*(?:"(?P<val>[^\n]*?)"|(?P<sym>[A-Za-z_]\w*))""",
    ),
    "boundingbox_below": re.compile(r"""boundingBox\s*\(\s*\)"""),
    "boundingbox_above": re.compile(r"""boundingBox\s*\(\s*\)"""),
}

# Bare `assertEquals(expected, actual)` / `Assertions.assertEquals(...)` /
# `Assert.assertEquals(...)` — value-binding fallback. Argument order isn't
# enforced (JUnit and TestNG disagree on it); we only need to know one of
# the arguments matches the expected literal/symbol.
_JAVA_ASSERT_EQ_STR = re.compile(
    r"""[Aa]ssert(?:Equals)?\s*\([^;]*?"""
    r"""(?:"(?P<val>[^\n]*?)"|(?P<sym>[A-Za-z_]\w*))[^;]*?\)""",
)
_JAVA_ASSERT_EQ_NUM = re.compile(
    r"""[Aa]ssert(?:Equals)?\s*\([^;]*?(?P<n>\d+)[^;]*?\)""",
)
# AssertJ fluent fallback — `assertThat(actual).isEqualTo(expected)`. The
# comment above claims "AssertJ" is covered, but `_JAVA_ASSERT_EQ_STR`/
# `_JAVA_ASSERT_EQ_NUM` only match the JUnit/TestNG `assertEquals(...)` call
# shape: AssertJ's expected value lives inside a separate `.isEqualTo(...)`
# call chained off `assertThat(...)`, which `[Aa]ssert(?:Equals)?\(` cannot
# match (`assertThat(` is not `assert(` or `assertEquals(`). This closes
# that gap for AssertJ's fluent idiom specifically.
_JAVA_ASSERTTHAT_EQ_STR = re.compile(
    r"""assertThat\s*\([^;]*?\)\s*\.\s*isEqualTo\s*\(\s*"""
    r"""(?:"(?P<val>[^\n]*?)"|(?P<sym>[A-Za-z_]\w*))\s*\)""",
)
_JAVA_ASSERTTHAT_EQ_NUM = re.compile(
    r"""assertThat\s*\([^;]*?\)\s*\.\s*isEqualTo\s*\(\s*(?P<n>\d+)\s*\)""",
)
_JAVA_COUNT_DRIFT = re.compile(r"""(?:>=|>)\s*(?P<n>\d+)\b""")
_JAVA_TAUTOLOGY = re.compile(
    r"""\.size\(\)\s*[><]=?\s*0\b|\.length\s*[><]=?\s*0\b""",
)
_JAVA_BOUNDING_Y = re.compile(r"""\.y\b""")
# Split-probe fallback (Java): geometry compared via locals, e.g.
# `assertThat(marketingTop).isGreaterThan(legalTop)` or `assertTrue(a > b)`.
_JAVA_BOUNDING_Y_LOOSE = re.compile(
    r"""isGreaterThan\s*\(|isLessThan\s*\(|assert\w*\s*\([^;]*[<>]""",
)


def _verify_criterion_java(
    check: str, crit: dict, pom_body: str, test_body: str,
    sibling_pom_text: str = "",
) -> str | None:
    """Java analogue of ``_verify_criterion`` / ``_verify_criterion_py``.

    ``sibling_pom_text`` — see ``_verify_criterion``: sibling probe bodies
    unioned for split-probe positional checks."""
    expected_literal = crit.get("expected_literal")
    expected_symbol = crit.get("expected_symbol")
    locator = crit.get("locator")
    ref_locator = crit.get("reference_locator")
    has_expected = expected_literal is not None or bool(expected_symbol)

    pat = _JAVA_ASSERT_PATTERNS.get(check)
    if pat is None:
        return None  # custom / unknown — routed to the semantic judge
    combined = pom_body + "\n" + test_body

    if check == "exact_count":
        expected = int(expected_literal) if expected_literal is not None else None
        if expected is not None:
            for dm in _JAVA_COUNT_DRIFT.finditer(combined):
                if int(dm.group("n")) > expected:
                    return (
                        f"contract requires exact count {expected} but body uses "
                        f"'{dm.group(0)}' — count-drift (weaker than exact)"
                    )
        matches = list(pat.finditer(combined))
        num_asserts = (
            list(_JAVA_ASSERT_EQ_NUM.finditer(combined))
            + list(_JAVA_ASSERTTHAT_EQ_NUM.finditer(combined))
        )
        if not matches and not num_asserts:
            return f"missing hasCount({expected}) / assertEquals({expected}, ...) assertion"
        if expected is not None:
            ok = _count_matches_expected(
                matches, combined, expected, expected_symbol
            ) or any(int(m.group("n")) == expected for m in num_asserts)
            if not ok:
                return f"count assertion does not check exact value {expected}"
        return None

    if check == "exact_text":
        matches = list(pat.finditer(combined))
        eq_matches = (
            list(_JAVA_ASSERT_EQ_STR.finditer(combined))
            + list(_JAVA_ASSERTTHAT_EQ_STR.finditer(combined))
        )
        if not matches and not eq_matches:
            return (
                f"missing hasText / assertEquals(\"{expected_symbol or expected_literal}\") "
                f"assertion"
            )
        if has_expected and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"text assertion present but does not reference expected value "
                f"{expected_symbol or expected_literal!r} (tautology / wrong value)"
            )
        return None

    if check in ("exact_attribute", "value_equals"):
        matches = list(pat.finditer(combined))
        eq_matches = (
            list(_JAVA_ASSERT_EQ_STR.finditer(combined))
            + list(_JAVA_ASSERTTHAT_EQ_STR.finditer(combined))
        )
        if not matches and not eq_matches:
            return f"missing {check} assertion for {expected_symbol or expected_literal!r}"
        if has_expected and not (
            any(_match_expected(m, expected_literal, expected_symbol) for m in matches)
            or any(_match_expected(m, expected_literal, expected_symbol) for m in eq_matches)
        ):
            return (
                f"{check} present but does not reference expected value "
                f"{expected_symbol or expected_literal!r}"
            )
        return None

    if check in ("visible", "focusable"):
        if pat.search(combined):
            return None
        return f"missing {check} assertion on {locator or '<locator>'}"

    if check == "url_matches":
        return None if pat.search(combined) else "missing hasURL assertion"

    if check in ("boundingbox_below", "boundingbox_above"):
        combined_bbox = pom_body + "\n" + sibling_pom_text + "\n" + test_body
        missing_locators = []
        if locator and locator not in combined_bbox:
            missing_locators.append(f"locator={locator}")
        if ref_locator and ref_locator not in combined_bbox:
            missing_locators.append(f"reference_locator={ref_locator}")
        if missing_locators:
            return (
                f"body doesn't reference required locators: "
                f"{', '.join(missing_locators)} — use the named constants"
            )
        bbox_calls = _JAVA_ASSERT_PATTERNS[check].findall(combined_bbox)
        if len(bbox_calls) < 2:
            return (
                f"missing boundingBox().y comparison between "
                f"{locator} and {ref_locator}"
            )
        if not _JAVA_BOUNDING_Y.search(combined_bbox) and not \
                _JAVA_BOUNDING_Y_LOOSE.search(test_body):
            return (
                f"missing boundingBox().y comparison between "
                f"{locator} and {ref_locator}"
            )
        return None

    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_method_bodies(
    pom_file: Path,
    class_name: str,
    missing_methods: list[dict],
    *,
    test_files: list[Path] | None = None,
    language: str = "typescript",
    owning_test_text_by_method: dict[str, str] | None = None,
) -> list[BodyViolation]:
    """For each ``kind='assertion'`` entry in ``missing_methods``, verify
    the POM method + companion test bodies encode every
    ``acceptance_criteria`` entry. Returns violation list.

    TypeScript / JavaScript, Python (pytest / playwright-python), and Java
    (Playwright-Java + JUnit/TestNG/AssertJ) are all wired.

    ``owning_test_text_by_method`` (optional): maps a method name to the source
    of ONLY the test function(s) that call it (derived by the caller from the
    Step-7 choreography). When provided, a criterion is matched against the POM
    body + that method's OWNING test — not every generated test concatenated —
    which stops an unrelated matcher elsewhere from satisfying an
    element/value-specific criterion (finding 27 cross-contamination).
    """
    lang = (language or "").lower()
    is_py = lang in ("python", "pytest", "playwright-py", "selenium-py")
    is_jsts = lang in ("typescript", "javascript")
    is_java = lang in ("java", "selenium-java", "playwright-java")
    if not (is_py or is_jsts or is_java):
        return []

    try:
        pom_src = pom_file.read_text(encoding="utf-8")
    except OSError:
        return []

    if is_py:
        pom_bodies = _py_method_bodies(pom_src, class_name)
    elif is_java:
        pom_bodies = _java_method_bodies(pom_src, class_name)
    else:
        pom_bodies = _js_method_bodies(pom_src, class_name)
    test_texts: list[str] = []
    for tf in test_files or []:
        try:
            test_texts.append(tf.read_text(encoding="utf-8"))
        except OSError:
            continue
    combined_test_text = "\n\n".join(test_texts)
    if is_py:
        verify = _verify_criterion_py
    elif is_java:
        verify = _verify_criterion_java
    else:
        verify = _verify_criterion

    violations: list[BodyViolation] = []
    for m in missing_methods:
        if not isinstance(m, dict):
            continue
        if m.get("kind") != "assertion":
            continue
        criteria = m.get("acceptance_criteria") or []
        name = m.get("name", "")
        pom_body = pom_bodies.get(name, "")
        if not pom_body:
            violations.append(BodyViolation(
                method=name, criterion_index=-1, check="missing",
                message=f"method {name!r} not found in {pom_file.name}",
            ))
            continue
        # Scope the test-side match to this method's OWNING test when the
        # caller resolved it from the choreography; else fall back to all tests.
        scoped_test_text = combined_test_text
        if owning_test_text_by_method and name in owning_test_text_by_method:
            scoped_test_text = owning_test_text_by_method[name]
        # Positional checks may split the two element reads across an anchor
        # probe (this method) and a sibling probe. Union the bodies of the
        # OTHER POM methods this test actually calls so the positional oracle
        # sees both named locators + both boundingBox() calls across the pair.
        sibling_pom_text = "\n".join(
            body for other, body in pom_bodies.items()
            if other != name and other in scoped_test_text
        )
        for i, crit in enumerate(criteria):
            check = crit.get("check", "")
            err = verify(check, crit, pom_body, scoped_test_text, sibling_pom_text)
            if err:
                violations.append(BodyViolation(
                    method=name, criterion_index=i, check=check,
                    message=err,
                ))
    if violations:
        log.warning(
            "codegen_body_verify.violations",
            pom_file=str(pom_file),
            class_name=class_name,
            language=lang,
            count=len(violations),
        )
    return violations
