"""Failure-class heuristics used by the heal gate + bug-candidates writer.

Splits raw ``TestRunEntry`` rows into healable vs. real-bug buckets based on
regex matches over ``message`` + ``traceback``. Pure functions — safe to
unit-test in isolation. The ``QTEA_HEAL_ALL=1`` operator escape bypasses
classification entirely so bugs in the classifier itself can't block a
heal attempt.

Also owns ``_failing_tests`` (a one-liner filter over ``RunResult``) and
``_build_bug_candidates`` (the canonical bug-candidates.json shape).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from qtea.test_runner import RunResult, TestRunEntry

log = logging.getLogger(__name__)


def _failing_tests(run: RunResult) -> list[TestRunEntry]:
    return [r for r in run.results if r.status in ("failed", "error")]


# ---------------------------------------------------------------------------
# Failure classification (used by the heal-gate to skip un-healable rows)
# ---------------------------------------------------------------------------
#
# Run 20260621-213751-ee0fef hit the canonical recurring failure: 11/13 tests
# failed, the heal-skip cap (`len(failing) > _MAX_HEAL_TESTS`) blocked the
# entire heal flow, and TBD-promotion stayed blocked on `no_passing_witness`
# — so the user saw 11 mixed failures with no recovery path. Decomposition
# of the 11:
#   - 7 locator/timeout issues (Playwright TimeoutError, action-mediated
#     assertion-on-None) — heal can fix these via live MCP browser inspection
#   - 3 real bugs (WCAG violations, TTI budget, DOM-order assertion) — heal
#     cannot fix these; they are app-behaviour defects
#   - 1 codegen bug (`fixture 'snapshot' not found`) — needs Step 8 retry,
#     not heal
#
# The classifier below splits a `TestRunEntry` into one of:
#   - locator_timeout    — Playwright TimeoutError on locator action
#   - tbd_unresolvable   — JIT runtime exhausted bundle + LLM and gave up
#   - assertion_value    — bare assertion mismatch (e.g. `assert None == 'x'`,
#                          typically downstream of a locator finding the wrong
#                          element); treated as healable because the cause is
#                          usually upstream locator drift
#   - wcag_violation     — axe-core / WCAG audit reported issues
#   - tti_budget         — performance budget assertion
#   - fixture_missing    — pytest fixture lookup failure (codegen drift)
#   - import_error       — ModuleNotFoundError / ImportError at collection
#   - dom_order          — order-sensitive DOM assertion (e.g. `is_above is True`)
#   - unknown            — defaults to healable so we never lose a fix
#                          opportunity to a classifier gap
#
# The classifier is a PURE FUNCTION over `entry.message` + `entry.traceback`
# strings. No side effects, easy to unit-test. Anything classified as
# locator_timeout / tbd_unresolvable / assertion_value / unknown counts
# toward the heal queue; everything else flows directly to bug-candidates
# as a "real bug" without consuming heal budget.
#
# Operator escape: set `QTEA_HEAL_ALL=1` to bypass the classifier and
# heal every failure (useful for debugging the classifier itself).

# ``fixture_missing`` is healable since the heal scope was relaxed to permit
# editing fixtures / conftest / test infrastructure: a `fixture 'X' not
# found` error is a codegen/setup defect the fixer can repair in-loop
# (create the fixture, fix its name, wire the dependency) rather than a
# real product bug. ``import_error`` stays OUT of the healable set — a
# missing generated module is a structural codegen gap best handled by the
# Step 9->8 back-edge, not a per-test heal.
_FAILURE_CLASS_HEALABLE = frozenset({
    "locator_timeout", "tbd_unresolvable", "assertion_value",
    "fixture_missing", "unknown",
})

# Positive signal: Playwright confirmed the locator resolved to a DOM node
# before timing out (element found but not yet visible/enabled/stable).
# Absence of this line in a Playwright Locator.* timeout means the element
# was never found — classified as element_not_in_dom, not locator_timeout.
_ELEMENT_IN_DOM_RE = re.compile(r"locator resolved to\s*<", re.I)

# Guard: only apply the element_not_in_dom refinement when the timeout was
# raised by a Locator method call, not a page-level event / navigation wait
# (e.g. "Timeout Xms exceeded while waiting for event 'load'").
# Case-insensitive so it matches both Playwright Python ("Locator.click:")
# and Playwright TypeScript/JS ("locator.click:").
_LOCATOR_METHOD_RE = re.compile(r"\bLocator\.", re.I)

_CLASSIFY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # JIT runtime fail-fast — the bundle was exhausted, LLM re-resolve gave
    # up. Heal with MCP can interact (hover/click) then snapshot to find the
    # right selector for elements not visible in initial AOM.
    ("tbd_unresolvable", re.compile(
        r"qtea JIT runtime: could not resolve locator", re.I,
    )),
    # Element genuinely absent from the DOM — frameworks that raise a
    # dedicated "not found" exception rather than timing out. Covers
    # Selenium/WebDriver/Appium (all languages), Cypress, and WebdriverIO.
    # Playwright TimeoutError cases are refined separately in
    # _classify_failure using the call-log "locator resolved to" signal.
    ("element_not_in_dom", re.compile(
        r"NoSuchElementException"                                # Selenium/Appium — Java, Python, Ruby, C#
        r"|no\s+such\s+element"                                  # WebDriver wire-protocol prose
        r"|Unable\s+to\s+locate\s+element"                       # Selenium error text
        r"|element\s+not\s+found"                                # WebdriverIO / generic stacks
        r"|Expected\s+to\s+find\s+element.*but\s+never\s+found"  # Cypress
        r"|querying\s+.+yielded\s+0\s+elements",                 # Cypress alternate form
        re.I,
    )),
    # Playwright TimeoutError on any Locator action (get_attribute, click,
    # select_option, etc.). The runtime template's bundle-fallback already
    # tried alternatives + re-resolved; reaching this stage means we need
    # MCP-driven live inspection. _classify_failure refines this class to
    # element_not_in_dom when the call log lacks "locator resolved to".
    ("locator_timeout", re.compile(
        r"playwright[\._]+_impl[\._]+_errors\.TimeoutError"  # Python module path
        r"|TimeoutError:\s*Locator\."                        # Python: "TimeoutError: Locator.click:"
        r"|locator\.\w+:\s*Timeout\s+\d+ms\s+exceeded",     # TypeScript/JS: "locator.click: Timeout Xms"
        re.I,
    )),
    ("locator_timeout", re.compile(
        r"Timeout\s+\d+ms\s+exceeded.*while\s+waiting", re.I,
    )),
    # Pytest fixture lookup failure — codegen referenced a fixture that
    # isn't available (e.g. pytest-snapshot not installed). Heal cannot
    # fix this; needs a Step 8 codegen retry with a corrected test.
    ("fixture_missing", re.compile(
        r"fixture '[^']+' not found|fixture \".+?\" not found", re.I,
    )),
    # Import errors at collection time. Heal scope forbids touching
    # imports / fixtures / conftest. Word boundaries guard against false
    # positives in AOM snapshots that might quote module names verbatim.
    ("import_error", re.compile(
        r"\bModuleNotFoundError\b|\bImportError\b|\bNo module named\b", re.I,
    )),
    # WCAG / accessibility audit. axe-core results are app behaviour;
    # rewriting the test won't change the violation count.
    ("wcag_violation", re.compile(
        r"WCAG\s*[\d\.]+|wcag\d|axe-core|accessibility violation",
        re.I,
    )),
    # Performance budget. A heal pass can't make the SUT faster.
    # `\bTTI\b` requires word boundaries — bare `TTI` matched inside
    # words like "settings" (seTTIngs) and false-flagged any test whose
    # AOM dump contained UI text with that substring.
    ("tti_budget", re.compile(
        r"\bTTI\b|exceeds budget of \d+ms|p9[05] (?:latency|tti|response)",
        re.I,
    )),
    # Order-sensitive DOM assertion (typically `is_above`, `is_before`).
    # These are app-behaviour assertions — heal cannot reorder the DOM.
    ("dom_order", re.compile(
        r"(?:appear\s+(?:before|above)|DOM\s+order|is_above|is_before)",
        re.I,
    )),
    # Bare assertion mismatch — usually a downstream symptom of locator
    # drift (wrong element found → wrong value). Treat as healable: if
    # heal can re-target the locator, the assertion will pass.
    ("assertion_value", re.compile(
        r"^\s*AssertionError|assert\s+\S+\s*(?:==|is|!=)",
        re.I | re.MULTILINE,
    )),
)


def _classify_failure(entry: TestRunEntry) -> str:
    """Return one of the classes above based on entry.message + entry.traceback.

    First matching pattern wins. Order matters — more-specific patterns
    (e.g. `qtea JIT runtime`) come before more-general ones (e.g. bare
    AssertionError). Returns ``"unknown"`` when nothing matches; the heal
    gate treats unknown as healable so a classifier gap never blocks a
    fix opportunity.

    Playwright locator_timeout refinement (Layer 1):
    When a Playwright Locator.* timeout matches, the call log is inspected
    for "locator resolved to <" — Playwright's signal that the element WAS
    found in the DOM (it just wasn't in the required state yet). If that
    signal is absent, the element was never found and the failure is
    reclassified as element_not_in_dom so the heal agent is not invoked.
    The refinement only fires for Locator-method timeouts (_LOCATOR_METHOD_RE),
    not page-level event / navigation waits which lack a locator resolution
    step entirely.
    """
    haystack = "\n".join(filter(None, (entry.message, entry.traceback)))
    if not haystack:
        return "unknown"
    for label, pat in _CLASSIFY_PATTERNS:
        if pat.search(haystack):
            if (
                label == "locator_timeout"
                and _LOCATOR_METHOD_RE.search(haystack)
                and not _ELEMENT_IN_DOM_RE.search(haystack)
            ):
                return "element_not_in_dom"
            return label
    return "unknown"


# ---------------------------------------------------------------------------
# Layer 2: AOM-at-failure cross-check (bi-directional)
#
# Layer 2 fires on either ``element_not_in_dom`` OR ``locator_timeout``
# tentative classifications. The ambiguity to resolve:
#   (a) element genuinely absent from the DOM  →  real DEV bug, no heal
#   (b) locator is wrong / stale but element exists  →  heal can fix
#
# Reads the AOM snapshot captured by the runtime plugin at the moment of
# failure (<workspace>/aom-at-failure/<entry_id>.txt) and searches for a
# distinctive token extracted from the Playwright call-log locator
# expression.
#
# Behavior matrix:
#   element_not_in_dom + token in AOM  →  locator_timeout    (upgrade — healable)
#   element_not_in_dom + token absent  →  element_not_in_dom (unchanged — real bug)
#   locator_timeout    + token in AOM  →  locator_timeout    (unchanged — healable)
#   locator_timeout    + token absent  →  element_not_in_dom (downgrade — real bug)
#
# The locator_timeout downgrade branch is short-circuited in
# ``_partition_failures`` when Playwright's own call log contains
# ``"locator resolved to <"`` (positive DOM-presence signal) — that
# signal is more authoritative than an AOM substring search.
# ---------------------------------------------------------------------------

# Regex to pull the locator expression out of a Playwright call log.
_WAITING_FOR_LOCATOR_RE = re.compile(
    r"waiting\s+for\s+locator\(\s*['\"](.+?)['\"]\s*\)",
    re.I,
)
# Extract the value of a data-testid attribute from a CSS selector.
_TESTID_RE = re.compile(r'data-testid\s*=\s*["\']([^"\']+)["\']', re.I)
# Extract a role= value (Playwright engine or ARIA).
_ROLE_RE = re.compile(r'\brole\s*=\s*["\']([^"\']+)["\']', re.I)

# Generic UI vocabulary that appears in almost every AOM and provides no
# discriminating signal when searching for a specific element.
_GENERIC_UI_TOKENS = frozenset({
    "btn", "button", "link", "input", "text", "form", "icon",
    "img", "image", "div", "span", "label", "item", "list",
    "nav", "header", "footer", "main", "section", "container",
    "wrapper", "inner", "outer",
})


def _extract_locator_search_term(haystack: str) -> str | None:
    """Return a distinctive search token from a Playwright call-log entry.

    Priority:
      1. ``data-testid`` value — split by ``-/_``, return first token >3
         chars that is not in the generic UI vocabulary.
      2. ``role=`` value — the ARIA role name.
      3. Any quoted string ≥4 chars in the locator expression.
    Returns ``None`` when no useful token can be extracted.
    """
    m = _WAITING_FOR_LOCATOR_RE.search(haystack)
    if not m:
        return None
    expr = m.group(1)

    # data-testid → most reliable; pick the first non-generic token
    m2 = _TESTID_RE.search(expr)
    if m2:
        val = m2.group(1)
        for tok in re.split(r"[-_\s]+", val):
            if len(tok) > 3 and tok.lower() not in _GENERIC_UI_TOKENS:
                return tok
        return val  # full value as fallback

    # role= → ARIA role name
    m2 = _ROLE_RE.search(expr)
    if m2:
        return m2.group(1)

    # Any quoted string fragment ≥4 chars (covers text= / name= locators)
    m2 = re.search(r'["\']([^"\']{4,})["\']', expr)
    if m2:
        return m2.group(1)

    return None


def _refine_locator_absence(
    entry: TestRunEntry,
    aom_dir: Path | None,
    *,
    tentative: str = "element_not_in_dom",
) -> str:
    """Cross-check a locator-related classification against the AOM snapshot
    captured at failure time (Layer 2). Bi-directional refinement.

    Behavior matrix (``tentative`` × AOM search result):

    - ``element_not_in_dom`` + token in AOM → ``locator_timeout`` (healable)
    - ``locator_timeout``    + token in AOM → ``locator_timeout`` (healable)
    - either                 + token absent → ``element_not_in_dom`` (real bug)
    - no signal (no AOM, no file, no token) → ``tentative`` unchanged

    Playwright positive-signal short-circuit: when ``tentative`` is
    ``locator_timeout`` and the call log contains ``"locator resolved to <"``,
    Layer 2 is skipped — Playwright's own DOM query is more authoritative
    than an AOM substring search.
    """
    if aom_dir is None:
        return tentative

    haystack = "\n".join(filter(None, (entry.message, entry.traceback)))

    # Playwright positive signal: call log confirms element was in the DOM
    # (just not yet visible/enabled/stable). Trust that over AOM search.
    if tentative == "locator_timeout" and _ELEMENT_IN_DOM_RE.search(haystack):
        return tentative

    aom_path = Path(aom_dir) / f"{entry.id}.txt"
    if not aom_path.exists():
        return tentative
    try:
        aom_text = aom_path.read_text(encoding="utf-8")
    except OSError:
        return tentative
    if not aom_text.strip():
        return tentative

    term = _extract_locator_search_term(haystack)
    if not term:
        return tentative

    if term.lower() in aom_text.lower():
        return "locator_timeout"

    if tentative == "locator_timeout":
        log.info(
            "qtea.step9.layer2.downgrade_locator_timeout test_id=%s token=%s",
            entry.id,
            term,
        )
    return "element_not_in_dom"


# Backwards-compat alias — preserves imports of the old name.
_refine_element_not_in_dom = _refine_locator_absence


def _partition_failures(
    failing: list[TestRunEntry],
    aom_dir: Path | None = None,
) -> tuple[list[TestRunEntry], list[tuple[TestRunEntry, str]]]:
    """Split ``failing`` into (healable, real_bugs).

    ``real_bugs`` carries (entry, class_label) so the caller can record
    the rationale in heal-log.jsonl without re-classifying.

    Operator escape: ``QTEA_HEAL_ALL=1`` returns ``(failing, [])`` —
    skips classification and heals everything. Use when the classifier
    itself is suspected of false-positively excluding a real heal target.

    ``aom_dir`` — when provided (``<workspace>/aom-at-failure``), Layer 2
    AOM cross-check fires bi-directionally for ``element_not_in_dom``
    and ``locator_timeout`` starting classes (see
    :func:`_refine_locator_absence` for the behavior matrix and
    Playwright positive-signal short-circuit).
    """
    if os.environ.get("QTEA_HEAL_ALL") == "1":
        return list(failing), []
    healable: list[TestRunEntry] = []
    real_bugs: list[tuple[TestRunEntry, str]] = []
    for entry in failing:
        cls = _classify_failure(entry)
        if aom_dir is not None and cls in ("element_not_in_dom", "locator_timeout"):
            cls = _refine_locator_absence(entry, aom_dir, tentative=cls)
        if cls in _FAILURE_CLASS_HEALABLE:
            healable.append(entry)
        else:
            real_bugs.append((entry, cls))
    return healable, real_bugs


def _build_bug_candidates(failing: list[TestRunEntry]) -> dict:
    now = datetime.now(UTC).isoformat()
    out = {"candidates": []}
    for f in failing:
        out["candidates"].append({
            "id": f"BC-{f.id}",
            "test_id": f.id,
            "title": f.name,
            "file": f.file,
            "status": f.status,
            "message": f.message,
            "traceback": f.traceback,
            "tc_refs": [],
            "attachments": f.attachments,
            "first_seen": now,
        })
    return out


__all__ = [
    "_CLASSIFY_PATTERNS",
    "_ELEMENT_IN_DOM_RE",
    "_FAILURE_CLASS_HEALABLE",
    "_GENERIC_UI_TOKENS",
    "_LOCATOR_METHOD_RE",
    "_ROLE_RE",
    "_TESTID_RE",
    "_WAITING_FOR_LOCATOR_RE",
    "_build_bug_candidates",
    "_classify_failure",
    "_extract_locator_search_term",
    "_failing_tests",
    "_partition_failures",
    "_refine_element_not_in_dom",  # backwards-compat alias for _refine_locator_absence
    "_refine_locator_absence",
]
