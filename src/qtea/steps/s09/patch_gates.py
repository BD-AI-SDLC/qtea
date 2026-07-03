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
from collections import Counter

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
# Assertion-immutability gate (mirrors XPath gate above)
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


def _extract_assertion_lines(source: str) -> list[str]:
    """Extract normalised assertion lines from source (stripped + lowered)."""
    out: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if any(p.search(stripped) for p in _ASSERTION_LINE_PATTERNS):
            out.append(stripped.lower())
    return out


def _patch_modifies_assertions(pre: bytes | None, post: bytes | None) -> bool:
    """True iff the post-heal source REMOVED or ALTERED any assertion line
    that existed in the pre-heal source.

    Adding new assertions is allowed. When *pre* is ``None`` the file was
    created by the heal, so there were no prior assertions to protect."""
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
    pre_counts = Counter(pre_assertions)
    post_counts = Counter(post_assertions)
    return any(
        post_counts.get(assertion, 0) < count
        for assertion, count in pre_counts.items()
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
    "_count_empty_handlers",
    "_count_xpath_markers",
    "_extract_assertion_lines",
    "_patch_has_anti_patterns",
    "_patch_introduces_xpath",
    "_patch_modifies_assertions",
]
