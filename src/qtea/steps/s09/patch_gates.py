"""Patch-content quality gates for Step 9 self-heal.

Reject heal patches that quietly downgrade to XPath, alter existing
assertions, or introduce exception-swallowing anti-patterns. Called from
``_apply_fixer_outputs`` in the parent Step 9 module (``s09_execute``).

Kept intentionally free of pipeline imports — every function here takes
bytes / str and returns bool / int / list. This keeps the gates cheap to
unit-test and safe to import from anywhere in the pipeline.
"""

from __future__ import annotations

import re

# Literal XPath markers checked by ``_count_xpath_markers``.
_XPATH_PATTERNS: tuple[str, ...] = (
    "By.XPATH",
    "xpath=",
    "getByXPath(",
    ".xpath(",
    "By.xpath(",
    "XPATH:",
)

# Regex catches string literals whose first char after the quote is ``//``
# (the raw XPath shorthand: ``page.locator('//div')``).
_XPATH_LITERAL_RE = re.compile(r"""['"]//[^'"\n]+['"]""")


def _count_xpath_markers(source: str) -> int:
    """Count XPath-marker occurrences in a source blob. Combines literal-pattern
    matches with a regex that catches string literals beginning with `//` (the
    raw XPath shorthand)."""
    count = sum(source.count(p) for p in _XPATH_PATTERNS)
    count += len(_XPATH_LITERAL_RE.findall(source))
    return count


def _patch_introduces_xpath(pre: bytes | None, post: bytes | None) -> bool:
    """True iff the post-heal source contains MORE XPath markers than the
    pre-heal source. We count rather than detect-any so an existing XPath in
    the SUT (legitimate or grandfathered) doesn't false-trigger the gate; only
    a NEW introduction is rejected.

    When ``pre is None`` the heal CREATED a new file — any XPath in the new
    file is by definition introduced."""
    if post is None:
        return False
    try:
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return False
    post_count = _count_xpath_markers(post_src)
    if pre is None:
        return post_count > 0
    try:
        pre_src = pre.decode("utf-8", errors="replace")
    except Exception:
        return False
    return post_count > _count_xpath_markers(pre_src)


# ---------------------------------------------------------------------------
# Assertion-faithfulness gate (Gap F)
#
# The heal agent MAY correct a mis-transcribed assertion so it matches the
# Step-4 expected result, but MUST NOT *weaken* one: no removal, and no
# downgrade of a "strong" assertion (a concrete comparison / matcher) into a
# "weak" one (a bare-truthy `assert <expr>`) to force a green over a real
# value mismatch. This preserves the DEV-bug signal while letting the fixer
# repair genuine codegen transcription errors — the exact intent of the
# scope relaxation. (Replaces the former blanket assertion-immutability gate,
# which rejected ALL assertion edits including legitimate corrections.)
# ---------------------------------------------------------------------------

_ASSERTION_LINE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*assert\b"),
    re.compile(r"^\s*expect\s*\("),
    re.compile(r"^\s*with\s+pytest\.raises\b"),
    re.compile(r"\.should\s*\("),
    re.compile(
        r"^\s*assert(?:Equals|True|False|Null|NotNull|That|Same|Throws)\s*\(",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*Should\s+(?:Be|Contain|Match|Not)", re.IGNORECASE),
)

# Operators / call-forms that make a Python `assert` a CONCRETE (strong)
# check rather than a bare-truthy one.
_STRONG_ASSERT_SIGNAL_RE = re.compile(
    r"==|!=|>=|<=|<|>|\bis\b|\bin\b|\bnot\b"
)


def _extract_assertion_lines(source: str) -> list[str]:
    """Extract normalised assertion lines from source (stripped + lowered)."""
    out: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if any(p.search(stripped) for p in _ASSERTION_LINE_PATTERNS):
            out.append(stripped.lower())
    return out


def _is_strong_assertion(line: str) -> bool:
    """True iff a (normalised, lower-cased) assertion line makes a CONCRETE
    check rather than a bare-truthy one.

    Strong: Playwright `expect(...).to_*`, `.should(...)`, JUnit-style
    `assertequals/asserttrue/...(...)`, Robot `should be/contain/...`, and any
    Python `assert` carrying a comparison/membership/identity operator.
    Weak (bare-truthy): `assert loc.is_visible()`, `assert value` — the classic
    softening target when downgrading a real comparison to force a pass."""
    stripped = line.strip()
    if (
        stripped.startswith("expect(")
        or ".should(" in stripped
        or stripped.startswith("with pytest.raises")
        or stripped.startswith("should ")
        or re.match(
            r"assert(?:equals|true|false|null|notnull|that|same|throws)\s*\(",
            stripped,
        )
    ):
        return True
    if stripped.startswith("assert"):
        # Python bare `assert`: strong only when it carries a real operator.
        body = stripped[len("assert"):]
        return bool(_STRONG_ASSERT_SIGNAL_RE.search(body))
    # Unknown assertion-ish line — treat as strong (conservative: don't let an
    # unrecognised form be silently dropped/weakened).
    return True


def _count_strong_assertions(assertion_lines: list[str]) -> int:
    return sum(1 for ln in assertion_lines if _is_strong_assertion(ln))


# ---------------------------------------------------------------------------
# Oracle-directional gate (finding 28/29): a healed assertion may be CORRECTED
# toward the Step-4 pinned value, but must never SWAP a pinned value for one
# the design never sanctioned (bug-masking), nor re-point an assertion at a
# different literal element. Conservative + fail-open: only fires when a
# literal on an assertion line DISAPPEARED and was replaced by a literal that
# is NOT in the oracle set. Pure additions and oracle-sanctioned corrections
# pass; empty oracle → no-op.
# ---------------------------------------------------------------------------

_ASSERT_LINEISH_RE = re.compile(
    r"expect\s*\(|\.\s*should\b|(?:^|\s)assert\b|to_have_|toHave|to_be_|toBe|"
    r"assert(?:Equals|True|False|Null|NotNull|That|Same)",
    re.IGNORECASE,
)
_LITERAL_TOKEN_RE = re.compile(
    r"""(['"])(?P<s>.*?)\1|(?<![\w.])(?P<n>\d+(?:\.\d+)?)(?![\w.])""",
)


def _assertion_literals(source: str) -> set[str]:
    """String + numeric literals appearing on assertion-shaped lines."""
    out: set[str] = set()
    for line in source.splitlines():
        if not _ASSERT_LINEISH_RE.search(line):
            continue
        for m in _LITERAL_TOKEN_RE.finditer(line):
            if m.group("s") is not None:
                out.add(m.group("s"))
            elif m.group("n") is not None:
                out.add(m.group("n"))
    return out


def _patch_diverges_from_oracle(
    pre: bytes | None, post: bytes | None, oracle_values: set[str] | None,
) -> bool:
    """True iff the heal SWAPPED an assertion literal for a value the Step-4
    oracle never sanctioned.

    Fail-open: returns False when there is no pre content, no oracle, or the
    heal only ADDED literals (no disappearance = not a swap). A disappeared
    literal replaced by a non-oracle literal is the bug-masking pattern the
    count-based gate cannot see (equal counts, still "strong"). Also catches an
    inline assertion locator/value re-point to an unsanctioned literal.
    """
    if pre is None or post is None or not oracle_values:
        return False
    try:
        pre_src = pre.decode("utf-8", errors="replace")
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return False
    pre_lits = _assertion_literals(pre_src)
    post_lits = _assertion_literals(post_src)
    disappeared = pre_lits - post_lits
    if not disappeared:
        return False  # pure addition / no swap
    appeared = post_lits - pre_lits
    # A swap is only suspicious when the NEW value is not oracle-sanctioned.
    return any(v not in oracle_values for v in appeared)


def _patch_weakens_assertions(pre: bytes | None, post: bytes | None) -> bool:
    """True iff the post-heal source WEAKENS the test's assertions relative to
    pre-heal — i.e. it removed an assertion, or downgraded a strong assertion
    into a weak (bare-truthy) one.

    Correcting an assertion's expected value (strong → strong, same count) is
    ALLOWED — that's a legitimate fix for a codegen transcription error.
    Adding assertions is allowed. When *pre* is ``None`` the file was created
    by the heal, so there were no prior assertions to protect."""
    if pre is None or post is None:
        return False
    try:
        pre_src = pre.decode("utf-8", errors="replace")
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return False
    pre_assertions = _extract_assertion_lines(pre_src)
    if not pre_assertions:
        return False
    post_assertions = _extract_assertion_lines(post_src)
    # Removal: fewer assertion lines after the heal.
    if len(post_assertions) < len(pre_assertions):
        return True
    # Downgrade: fewer CONCRETE assertions after the heal (a strong check was
    # turned into a bare-truthy one to force a pass).
    return _count_strong_assertions(post_assertions) < _count_strong_assertions(
        pre_assertions
    )


# ---------------------------------------------------------------------------
# Anti-pattern gate — rejects heals that introduce exception-swallowing
# ---------------------------------------------------------------------------

_EMPTY_HANDLER_PATTERNS: tuple[re.Pattern, ...] = (
    # Python: except ...: pass / except: pass (single or multi-line)
    re.compile(
        r"except\b[^:]*:\s*(?:#[^\n]*)?\n\s*pass\b",
        re.MULTILINE,
    ),
    # JS/TS: catch (...) { } or catch { } with empty/whitespace-only body
    re.compile(
        r"catch\s*(?:\([^)]*\))?\s*\{\s*\}",
    ),
    # Java/C#: catch (...) { } with empty/whitespace-only body
    re.compile(
        r"catch\s*\([^)]+\)\s*\{\s*\}",
    ),
)


def _count_empty_handlers(source: str) -> int:
    """Count exception handlers with empty/no-op bodies across stacks."""
    return sum(len(p.findall(source)) for p in _EMPTY_HANDLER_PATTERNS)


def _patch_has_anti_patterns(pre: bytes | None, post: bytes | None) -> list[str]:
    """Return a list of anti-pattern violations INTRODUCED by the heal.

    Only flags patterns that are NEW (post count > pre count) so
    pre-existing SUT code doesn't trigger false positives.
    Returns an empty list when clean."""
    if post is None:
        return []
    try:
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return []
    post_count = _count_empty_handlers(post_src)
    if post_count == 0:
        return []
    pre_count = 0
    if pre is not None:
        try:
            pre_src = pre.decode("utf-8", errors="replace")
            pre_count = _count_empty_handlers(pre_src)
        except Exception:
            pass
    if post_count > pre_count:
        return [
            f"exception swallowing: {post_count - pre_count} new empty "
            f"exception handler(s) (except/catch with no-op body)"
        ]
    return []


__all__ = [
    "_ASSERTION_LINE_PATTERNS",
    "_EMPTY_HANDLER_PATTERNS",
    "_XPATH_PATTERNS",
    "_assertion_literals",
    "_count_empty_handlers",
    "_count_strong_assertions",
    "_count_xpath_markers",
    "_extract_assertion_lines",
    "_is_strong_assertion",
    "_patch_diverges_from_oracle",
    "_patch_has_anti_patterns",
    "_patch_introduces_xpath",
    "_patch_weakens_assertions",
]
