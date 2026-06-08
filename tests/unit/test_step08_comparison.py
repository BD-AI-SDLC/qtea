"""Tests dedicated to Step 8b — the dom-comparison audit pass.

Coverage focus:
  - `dom-comparison.json` schema validation (the contract between the
    fixer-audit agent and the pipeline post-processor).
  - The fixture-driven reproduction of today's `20260604-205311-e2de04`
    GEMINI_* failure: the askbosch SUT exposed one element where codegen
    invented three constants (button / link / tooltip). Under the new
    design these get verdicts matched / duplicate / ghost respectively
    and only the matched one counts against the apply-rate gate.

The end-to-end integration test lives in `test_step08_locator_resolution.py`
(`test_step08_excuses_ghost_verdicts_from_apply_rate_gate`); this file
tests the supporting pieces in isolation so a schema break or helper
regression is caught with a sharper error message.
"""

from __future__ import annotations

from pathlib import Path

from worca_t.schemas import is_valid
from worca_t.steps.s08_locator_resolution import (
    _apply_comparison_verdict,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_dom_comparison_schema_accepts_minimal_payload():
    """The minimum required shape: expected_elements + summary."""
    payload = {
        "expected_elements": [],
        "summary": {"matched": 0, "ghost": 0, "duplicate": 0,
                    "low_confidence": 0, "should_exist_total": 0},
    }
    ok, err = is_valid(payload, "dom-comparison")
    assert ok, err


def test_dom_comparison_schema_accepts_full_payload():
    """The realistic shape produced for the askbosch GEMINI_* scenario."""
    payload = {
        "snapshots_consumed": [
            {"file": "page-snapshot-01.html", "kind": "html",
             "url": "https://askbosch-q.ai.bosch.com/auth/signin"},
            {"file": "page-snapshot-02.html", "kind": "html",
             "url": "https://askbosch-q.ai.bosch.com/en/"},
        ],
        "expected_elements": [
            {"tbd_constant": "GEMINI_BUTTON",
             "file": "src/pages/worca_gemini_nav_locators.py", "line": 6,
             "inferred_intent": "Side-nav link for Gemini Enterprise",
             "verdict": "matched",
             "matched_selector": "[data-testid='Layout-GeminiEnterprise']",
             "snapshot": "page-snapshot-02.html", "confidence": 0.95},
            {"tbd_constant": "GEMINI_LINK",
             "file": "src/pages/worca_gemini_nav_locators.py", "line": 7,
             "verdict": "duplicate", "duplicate_of": "GEMINI_BUTTON",
             "explanation": "same anchor as GEMINI_BUTTON"},
            {"tbd_constant": "GEMINI_TOOLTIP",
             "file": "src/pages/worca_gemini_nav_locators.py", "line": 8,
             "verdict": "ghost",
             "explanation": ("no tooltip element exists in either snapshot; "
                             "link carries a native title attribute only")},
        ],
        "extra_dom_elements": [],
        "summary": {"matched": 1, "ghost": 1, "duplicate": 1,
                    "low_confidence": 0, "should_exist_total": 1},
    }
    ok, err = is_valid(payload, "dom-comparison")
    assert ok, err


def test_dom_comparison_schema_rejects_unknown_verdict():
    payload = {
        "expected_elements": [
            {"tbd_constant": "X", "verdict": "questionable"},
        ],
        "summary": {"matched": 0, "ghost": 0, "duplicate": 0,
                    "low_confidence": 0, "should_exist_total": 0},
    }
    ok, _ = is_valid(payload, "dom-comparison")
    assert ok is False


def test_dom_comparison_schema_rejects_missing_summary():
    payload = {"expected_elements": []}
    ok, _ = is_valid(payload, "dom-comparison")
    assert ok is False


# ---------------------------------------------------------------------------
# Fixture-driven reproduction of today's askbosch failure
# ---------------------------------------------------------------------------


def _askbosch_locator_resolution_payload(file_rel: str) -> dict:
    """The shape Step 8a (playwright-tester) emits for the GEMINI_* file:
    1 applied + 2 honest skips."""
    return {
        "base_url": "https://askbosch-q.ai.bosch.com/en/",
        "resolutions": [{
            "test_id": "S-worca_gemini_nav_locators",
            "file": file_rel,
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 6, "applied": True,
                 "strategy": "data-testid", "confidence": 0.95,
                 "replacement": "[data-testid='Layout-GeminiEnterprise']",
                 "applied_via": "line:6"},
                {"tbd": "TBD_LOCATOR", "line": 7, "applied": False,
                 "strategy": None, "replacement": None, "confidence": 0.0,
                 "skip_reason": "same element as GEMINI_BUTTON"},
                {"tbd": "TBD_LOCATOR", "line": 8, "applied": False,
                 "strategy": None, "replacement": None, "confidence": 0.0,
                 "skip_reason": "no DOM element matched: no tooltip exists"},
            ],
        }],
    }


def _askbosch_dom_comparison(file_rel: str) -> dict:
    """The shape Step 8b (fixer-audit) emits for the same scenario:
    one matched, one duplicate, one ghost."""
    return {
        "snapshots_consumed": [
            {"file": "page-snapshot-01.html", "kind": "html",
             "url": "https://askbosch-q.ai.bosch.com/auth/signin"},
            {"file": "page-snapshot-02.html", "kind": "html",
             "url": "https://askbosch-q.ai.bosch.com/en/"},
        ],
        "expected_elements": [
            {"tbd_constant": "GEMINI_BUTTON", "file": file_rel, "line": 6,
             "verdict": "matched",
             "matched_selector": "[data-testid='Layout-GeminiEnterprise']",
             "snapshot": "page-snapshot-02.html", "confidence": 0.95},
            {"tbd_constant": "GEMINI_LINK", "file": file_rel, "line": 7,
             "verdict": "duplicate", "duplicate_of": "GEMINI_BUTTON",
             "explanation": "same anchor"},
            {"tbd_constant": "GEMINI_TOOLTIP", "file": file_rel, "line": 8,
             "verdict": "ghost",
             "explanation": "no tooltip element in any snapshot"},
        ],
        "summary": {"matched": 1, "ghost": 1, "duplicate": 1,
                    "low_confidence": 0, "should_exist_total": 1},
    }


def test_askbosch_repro_verdict_apply_excuses_two_of_three():
    """End-to-end on just the verdict-apply helper: feeding today's actual
    8a payload + 8b comparison into `_apply_comparison_verdict` should
    leave 1 item applied and stamp the 2 skipped items with the correct
    verdicts so the gate excuses them."""
    file_rel = "src/pages/worca_gemini_nav_locators.py"
    payload = _askbosch_locator_resolution_payload(file_rel)
    comparison = _askbosch_dom_comparison(file_rel)
    out = _apply_comparison_verdict(payload, comparison)

    items = out["resolutions"][0]["items"]
    by_line = {it["line"]: it for it in items}

    # GEMINI_BUTTON survives the audit unchanged (matched).
    assert by_line[6]["applied"] is True
    assert by_line[6]["comparison_verdict"] == "matched"
    assert by_line[6]["strategy"] == "data-testid"

    # GEMINI_LINK was already skipped by the resolver; the auditor
    # confirms it as duplicate.
    assert by_line[7]["applied"] is False
    assert by_line[7]["comparison_verdict"] == "duplicate"
    assert "skip_reason" in by_line[7]

    # GEMINI_TOOLTIP: ghost confirmed; skip_reason rewritten by the
    # comparison helper so downstream tooling sees the audit's diagnostic.
    assert by_line[8]["applied"] is False
    assert by_line[8]["comparison_verdict"] == "ghost"
    assert "no tooltip" in by_line[8]["skip_reason"].lower()


def test_askbosch_repro_gate_math_under_new_rule():
    """Pure-arithmetic check on the gate denominator change. With the
    old rule: applied=1, total=3, rate=33% (FAILED). With the new rule:
    excused=2, denominator=1, rate=100% (PASSED)."""
    file_rel = "src/pages/worca_gemini_nav_locators.py"
    payload = _askbosch_locator_resolution_payload(file_rel)
    comparison = _askbosch_dom_comparison(file_rel)
    out = _apply_comparison_verdict(payload, comparison)

    items = [it for r in out["resolutions"] for it in r["items"]]
    applied = sum(1 for it in items if it.get("applied"))
    skipped = sum(1 for it in items if not it.get("applied"))
    excused = sum(
        1 for it in items
        if it.get("comparison_verdict") in ("ghost", "duplicate")
    )

    assert applied == 1
    assert skipped == 2
    assert excused == 2

    # Old gate (pre-audit):
    old_rate = applied / (applied + skipped)
    assert old_rate < 0.9  # would have failed under the old rule

    # New gate (post-audit):
    new_rate = applied / max((applied + skipped - excused), 1)
    assert new_rate == 1.0  # passes with flying colours
