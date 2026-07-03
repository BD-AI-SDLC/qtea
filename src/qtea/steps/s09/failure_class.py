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

import os
import re
from datetime import UTC, datetime

from qtea.test_runner import RunResult, TestRunEntry


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

_FAILURE_CLASS_HEALABLE = frozenset({
    "locator_timeout", "tbd_unresolvable", "assertion_value", "unknown",
})

_CLASSIFY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # JIT runtime fail-fast — the bundle was exhausted, LLM re-resolve gave
    # up. Heal with MCP can interact (hover/click) then snapshot to find the
    # right selector for elements not visible in initial AOM.
    ("tbd_unresolvable", re.compile(
        r"qtea JIT runtime: could not resolve locator", re.I,
    )),
    # Playwright TimeoutError on any Locator action (get_attribute, click,
    # select_option, etc.). The runtime template's bundle-fallback already
    # tried alternatives + re-resolved; reaching this stage means we need
    # MCP-driven live inspection.
    ("locator_timeout", re.compile(
        r"playwright[\._]+_impl[\._]+_errors\.TimeoutError"
        r"|TimeoutError:\s*Locator\.",
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
    """
    haystack = "\n".join(filter(None, (entry.message, entry.traceback)))
    if not haystack:
        return "unknown"
    for label, pat in _CLASSIFY_PATTERNS:
        if pat.search(haystack):
            return label
    return "unknown"


def _partition_failures(
    failing: list[TestRunEntry],
) -> tuple[list[TestRunEntry], list[tuple[TestRunEntry, str]]]:
    """Split ``failing`` into (healable, real_bugs).

    ``real_bugs`` carries (entry, class_label) so the caller can record
    the rationale in heal-log.jsonl without re-classifying.

    Operator escape: ``QTEA_HEAL_ALL=1`` returns ``(failing, [])`` —
    skips classification and heals everything. Use when the classifier
    itself is suspected of false-positively excluding a real heal target.
    """
    if os.environ.get("QTEA_HEAL_ALL") == "1":
        return list(failing), []
    healable: list[TestRunEntry] = []
    real_bugs: list[tuple[TestRunEntry, str]] = []
    for entry in failing:
        cls = _classify_failure(entry)
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
    "_FAILURE_CLASS_HEALABLE",
    "_build_bug_candidates",
    "_classify_failure",
    "_failing_tests",
    "_partition_failures",
]
