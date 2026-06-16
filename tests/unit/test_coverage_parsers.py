"""Parser tests for the new structured-coverage fields added in PR 1.

Covers:
- s02_refine: acceptance_criteria_structured, edge_cases_structured,
  nfrs_structured, coverage_notes (via md_parser.extract_coverage_notes).
- s03_plan: test_cases (TC roster table), acceptance_criteria_structured,
  coverage_notes.
- s04_strategy: req_id / ac_ids / ec_ids / nfr_ids / derived_from /
  automation_type on test_cases; top-level coverage_notes; reservation of
  "coverage notes" heading.
"""

from __future__ import annotations

from worca_t.md_parser import extract_coverage_notes, parse_markdown, Section
from worca_t.schemas import is_valid
from worca_t.steps.s02_refine import (
    _extract_acceptance_criteria_structured,
    _extract_edge_cases_structured,
    _extract_nfrs_structured,
    _project_to_json,
)
from worca_t.steps.s03_plan import _project_plan
from worca_t.steps.s04_strategy import (
    _looks_like_test_case,
    _project_strategy,
    _project_test_case,
)


# ---------- s02 refine ----------

def test_extract_acceptance_criteria_with_ids_parses_id_gwt_and_tag() -> None:
    md = """# Spec
## Acceptance Criteria
- [ ] AC-1: Given user on /login, When valid creds, Then dashboard shows. `[AUTOMATABLE]`
- [ ] AC-2: Given invalid pass, When submit, Then error shown. `[MANUAL ONLY]`
"""
    root = parse_markdown(md)
    out = _extract_acceptance_criteria_structured(root)
    assert [a["id"] for a in out] == ["AC-1", "AC-2"]
    assert out[0]["given"] == "user on /login"
    assert out[0]["when"] == "valid creds"
    assert out[0]["then"] == "dashboard shows."
    assert out[0]["automation"] == "AUTOMATABLE"
    assert out[1]["automation"] == "MANUAL_ONLY"


def test_extract_acceptance_criteria_legacy_no_id_falls_through() -> None:
    """Bullets without an AC-ID are NOT promoted to structured; they remain
    available via the legacy `acceptance_criteria` projection."""
    md = """# Spec
## Acceptance Criteria
- The user can log in with valid creds
- An error is shown on invalid creds
"""
    root = parse_markdown(md)
    structured = _extract_acceptance_criteria_structured(root)
    assert structured == []
    proj = _project_to_json(md)
    assert len(proj["acceptance_criteria"]) == 2  # legacy preserved


def test_extract_edge_cases_from_table_normalizes_severity_and_automation() -> None:
    md = """# Spec
## Edge Cases & Risks
| ID | Edge Case | Severity | Automation | Mitigation |
|----|-----------|----------|------------|------------|
| EC-1 | network drops | high | [AUTOMATABLE] | retry |
| EC-2 | locked account | medium | [MANUAL ONLY] | unlock |
| EC-3 | clock skew | low | [NEEDS INVESTIGATION] | tbd |
"""
    root = parse_markdown(md)
    out = _extract_edge_cases_structured(root)
    assert [(e["id"], e["severity"], e["automation"]) for e in out] == [
        ("EC-1", "high", "AUTOMATABLE"),
        ("EC-2", "medium", "MANUAL_ONLY"),
        ("EC-3", "low", "NEEDS_INVESTIGATION"),
    ]
    assert out[0]["mitigation"] == "retry"


def test_extract_edge_cases_falls_back_to_bullets_with_unknown_severity() -> None:
    md = """# Spec
## Edge Cases
- network drops mid-submit
- locked account
"""
    root = parse_markdown(md)
    out = _extract_edge_cases_structured(root)
    assert [(e["id"], e["severity"]) for e in out] == [
        ("EC-1", "UNKNOWN"),
        ("EC-2", "UNKNOWN"),
    ]


def test_extract_nfrs_detects_hard_thresholds_and_promotion() -> None:
    md = """# Spec
## Non-Functional Requirements
- **Performance:** Page load p95 ≤ 2.5s on cold cache. → promoted to AC-PERF-1
- **Security:** TLS 1.3 only.
- **Accessibility:** WCAG AA.
- **Compatibility:** Chrome 120+, Firefox 119+.
"""
    root = parse_markdown(md)
    out = _extract_nfrs_structured(root)
    by_id = {n["id"]: n for n in out}
    assert by_id["NFR-PERF-1"]["has_threshold"] is True
    assert by_id["NFR-PERF-1"]["promoted_to_ac"] == "AC-PERF-1"
    assert by_id["NFR-SEC-1"]["has_threshold"] is False
    assert by_id["NFR-A11Y-1"]["has_threshold"] is True
    assert by_id["NFR-COMPAT-1"]["has_threshold"] is True
    assert by_id["NFR-COMPAT-1"]["promoted_to_ac"] is None


def test_extract_nfrs_with_explicit_ids_preserved() -> None:
    md = """# Spec
## Non-Functional Requirements
- **NFR-PERF-9 [hard threshold]:** custom budget 500ms. → promoted to AC-PERF-9
"""
    root = parse_markdown(md)
    out = _extract_nfrs_structured(root)
    assert out[0]["id"] == "NFR-PERF-9"
    assert out[0]["has_threshold"] is True
    assert out[0]["promoted_to_ac"] == "AC-PERF-9"


def test_extract_coverage_notes_maps_resolutions() -> None:
    md = """# Spec
## Coverage Notes
- **AC-7:** Dropped — user skipped clarification.
- **mobile:** Excluded — user said out of scope.
- **EC-12:** Accepted risk — duplicate coverage by EC-1.
"""
    root = parse_markdown(md)
    out = extract_coverage_notes(root)
    res_by_id = {n["item_id"]: n["resolution"] for n in out}
    assert res_by_id == {
        "AC-7": "dropped",
        "mobile": "scope_excluded",
        "EC-12": "accepted_risk",
    }


def test_s02_full_projection_validates_against_schema() -> None:
    md = """# Login

**Requirement ID:** REQ-LOGIN

## Acceptance Criteria
- [ ] AC-1: Given user, When login, Then dashboard. `[AUTOMATABLE]`

## Edge Cases & Risks
| ID | Edge Case | Severity | Automation | Mitigation |
|----|-----------|----------|------------|------------|
| EC-1 | drop | high | [AUTOMATABLE] | retry |

## Non-Functional Requirements
- **Performance:** p95 ≤ 2.5s.

## Coverage Notes
- **AC-7:** Dropped — reason.
"""
    proj = _project_to_json(md)
    ok, err = is_valid(proj, "refined-spec")
    assert ok, err


# ---------- s03 plan ----------

def test_extract_tc_roster_table_parses_full_row() -> None:
    md = """# Plan

## Phase 1: Auth

### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | ECs | Automation |
|-------|-------|------|----------|--------|-----|-----|------------|
| TC-AUTH-001 | Login | smoke | critical | REQ-LOGIN | AC-1, AC-2 | EC-1 | automation |
| TC-AUTH-002 | Bad creds | smoke | high | REQ-LOGIN | AC-3 | - | manual |
"""
    proj = _project_plan(md)
    tcs = proj["test_cases"]
    assert tcs[0]["id"] == "TC-AUTH-001"
    assert tcs[0]["priority"] == "P0"
    assert tcs[0]["ac_ids"] == ["AC-1", "AC-2"]
    assert tcs[0]["ec_ids"] == ["EC-1"]
    assert tcs[0]["automation"] == "automation"
    assert tcs[0]["phase"] == 1
    assert tcs[1]["priority"] == "P1"
    assert tcs[1]["automation"] == "manual"
    assert tcs[1]["ec_ids"] == []


def test_extract_tc_roster_table_tolerates_legacy_columns_without_ecs() -> None:
    md = """# Plan
## Phase 1: Auth
### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | Automation |
|-------|-------|------|----------|--------|-----|------------|
| TC-A-001 | x | smoke | critical | REQ-X | AC-1 | automation |
"""
    proj = _project_plan(md)
    assert proj["test_cases"][0]["ec_ids"] == []
    assert proj["test_cases"][0]["ac_ids"] == ["AC-1"]


def test_legacy_plan_without_roster_yields_empty_test_cases() -> None:
    md = """# Plan
## Phase 1: Auth
### Overview
Auth tests.
### Files to Test
1. utils/login.ts
"""
    proj = _project_plan(md)
    assert proj["test_cases"] == []
    assert proj["acceptance_criteria_structured"] == []
    assert proj["coverage_notes"] == []
    # Legacy phases still parsed.
    assert len(proj["phases"]) == 1


def test_s03_full_projection_validates_against_schema() -> None:
    md = """# Plan
## Acceptance Criteria
- [ ] AC-1: Given a, When b, Then c. `[AUTOMATABLE]`

## Phase 1: Auth

### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | Automation |
|-------|-------|------|----------|--------|-----|------------|
| TC-AUTH-001 | Login | smoke | critical | REQ-X | AC-1 | automation |
"""
    proj = _project_plan(md)
    ok, err = is_valid(proj, "plan")
    assert ok, err


# ---------- s04 strategy ----------

def test_project_test_case_extracts_req_id_ac_ids_and_derived_from() -> None:
    md = """## Test Cases
#### TC-AUTH-001: Login happy
- **Type**: UI
- **Priority**: P0
- **Req ID**: REQ-LOGIN
- **ACs**: AC-1, AC-2
- **ECs**: EC-1
- **Derived from**: TC-PLAN-001
- **Automation Type**: ui
- **Steps**:
  1. Visit /login
- **Expected**: Land on /dashboard
"""
    proj = _project_strategy(md)
    tc = proj["test_cases"][0]
    assert tc["req_id"] == "REQ-LOGIN"
    assert tc["ac_ids"] == ["AC-1", "AC-2"]
    assert tc["ec_ids"] == ["EC-1"]
    assert tc["derived_from"] == ["TC-PLAN-001"]
    assert tc["automation_type"] == "ui"


def test_project_test_case_derived_from_defaults_to_self_when_missing() -> None:
    md = """## Test Cases
#### TC-AUTH-001: Login
- **Priority**: P0
"""
    proj = _project_strategy(md)
    assert proj["test_cases"][0]["derived_from"] == ["TC-AUTH-001"]


def test_project_test_case_derived_from_with_multiple_ids() -> None:
    md = """## Test Cases
#### TC-AUTH-001: Consolidated
- **Priority**: P0
- **Derived from**: TC-PLAN-001, TC-PLAN-002, TC-PLAN-003
"""
    proj = _project_strategy(md)
    assert proj["test_cases"][0]["derived_from"] == [
        "TC-PLAN-001", "TC-PLAN-002", "TC-PLAN-003",
    ]


def test_coverage_notes_section_not_classified_as_test_case() -> None:
    sec = Section(
        level=2,
        title="Coverage Notes",
        content="- **TC-PLAN-001:** Dropped — reason.",
    )
    assert _looks_like_test_case(sec) is False


def test_project_strategy_extracts_top_level_coverage_notes() -> None:
    md = """# Strategy
## Test Cases
#### TC-A: Foo
- **Priority**: P0

## Coverage Notes
- **TC-PLAN-004:** Accepted risk — duplicate coverage.
"""
    proj = _project_strategy(md)
    assert proj["coverage_notes"] == [
        {"item_id": "TC-PLAN-004", "reason": "duplicate coverage.",
         "resolution": "accepted_risk"},
    ]


def test_s04_full_projection_validates_against_schema() -> None:
    md = """# Strategy
## Test Cases
#### TC-AUTH-001: Login
- **Priority**: P0
- **Req ID**: REQ-X
- **ACs**: AC-1
- **Derived from**: TC-PLAN-001
- **Automation Type**: ui

## Coverage Notes
- **TC-PLAN-002:** Accepted risk — dup.
"""
    proj = _project_strategy(md)
    ok, err = is_valid(proj, "test-strategy")
    assert ok, err
