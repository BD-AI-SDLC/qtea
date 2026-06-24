"""Back-compat tests: the PR-1 schema deltas are additive; legacy artifacts
from prior pipeline runs MUST continue to validate, parse, and round-trip.

Two layers:
 1. Schema-only: hand-crafted minimal legacy artifacts (no new fields) validate.
 2. Re-parsing: when the new parsers run over legacy markdown that omits IDs,
    they emit empty structured lists rather than raising.
"""

from __future__ import annotations

from qtea.schemas import is_valid
from qtea.steps.s02_refine import _project_to_json
from qtea.steps.s03_plan import _project_plan
from qtea.steps.s04_strategy import _project_strategy

# ---------- 1. Hand-crafted legacy artifacts validate ----------

def test_legacy_refined_spec_without_new_fields_still_validates() -> None:
    """A minimal pre-PR1 refined-spec.json shape — no structured arrays,
    no coverage_notes — must continue to validate."""
    legacy = {
        "requirement_id": "REQ-OLD",
        "title": "Old spec",
        "sections": [],
        "acceptance_criteria": ["legacy bullet"],
        "edge_cases": None,
        "nfrs": None,
    }
    ok, err = is_valid(legacy, "refined-spec")
    assert ok, err


def test_legacy_plan_without_new_fields_still_validates() -> None:
    legacy = {
        "title": "Old plan",
        "phases": [{"number": 1, "title": "Auth", "files": [], "success_criteria": []}],
    }
    ok, err = is_valid(legacy, "plan")
    assert ok, err


def test_legacy_strategy_without_new_fields_still_validates() -> None:
    legacy = {
        "title": "Old strategy",
        "test_cases": [
            {
                "id": "TC-A",
                "title": "An old TC",
                "priority": "P0",
                "type": "ui",
                "preconditions": [],
                "steps": [],
                "expected": "ok",
                "tags": [],
                "raw": "...",
            }
        ],
    }
    ok, err = is_valid(legacy, "test-strategy")
    assert ok, err


# ---------- 2. New parsers gracefully degrade on legacy markdown ----------

def test_legacy_refined_spec_md_parses_without_crashing() -> None:
    md = """# Old Spec

## Acceptance Criteria
- a user can log in
- an error appears on bad creds

## Edge Cases
- network glitch
- session expired

## Non-Functional Requirements
- Performance should be good
"""
    proj = _project_to_json(md)
    assert proj["acceptance_criteria_structured"] == []
    assert all(ec["severity"] == "UNKNOWN" for ec in proj["edge_cases_structured"])
    assert all(n["category"] == "other" for n in proj["nfrs_structured"])
    assert proj["coverage_notes"] == []
    # Legacy free-text array still populated.
    assert len(proj["acceptance_criteria"]) == 2
    ok, err = is_valid(proj, "refined-spec")
    assert ok, err


def test_legacy_plan_md_parses_without_crashing() -> None:
    md = """# Old Plan
## Phase 1: Auth
### Overview
foo
### Files to Test
1. utils/x.ts
"""
    proj = _project_plan(md)
    assert proj["test_cases"] == []
    assert proj["acceptance_criteria_structured"] == []
    assert proj["coverage_notes"] == []
    ok, err = is_valid(proj, "plan")
    assert ok, err


def test_legacy_strategy_md_parses_with_default_derived_from() -> None:
    """A pre-PR1 strategy TC has no `derived_from` field. Parser must default
    to `[<own id>]` so the matrix builder can still trace it 1:1."""
    md = """# Old Strategy
## Test Cases
#### TC-AUTH-001: Login happy
- **Type**: UI
- **Priority**: P0
- **Steps**:
  1. login
- **Expected**: ok
"""
    proj = _project_strategy(md)
    tc = proj["test_cases"][0]
    assert tc["derived_from"] == ["TC-AUTH-001"]
    assert tc["ac_ids"] == []
    assert tc["req_id"] == ""
    assert tc["automation_type"] == "UNKNOWN"
    ok, err = is_valid(proj, "test-strategy")
    assert ok, err
