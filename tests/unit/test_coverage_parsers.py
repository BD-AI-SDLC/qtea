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

from qtea.md_parser import Section, extract_coverage_notes, parse_markdown
from qtea.schemas import is_valid
from qtea.steps.s02_refine import (
    _extract_acceptance_criteria_structured,
    _extract_edge_cases_structured,
    _extract_nfrs_structured,
    _project_to_json,
)
from qtea.steps.s03_plan import _project_plan
from qtea.steps.s04_strategy import (
    _looks_like_test_case,
    _project_strategy,
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


def test_extract_acceptance_criteria_nested_bold_gwt_single_ac() -> None:
    md = """# Spec
## Acceptance Criteria
- [ ] **AC-1:** `[AUTOMATABLE]`
  - **Given** user is on the login page
  - **When** they submit valid credentials
  - **Then** the dashboard is displayed
"""
    root = parse_markdown(md)
    out = _extract_acceptance_criteria_structured(root)
    assert len(out) == 1
    ac = out[0]
    assert ac["id"] == "AC-1"
    assert ac["given"] == "user is on the login page"
    assert ac["when"] == "they submit valid credentials"
    assert ac["then"] == "the dashboard is displayed"
    assert ac["automation"] == "AUTOMATABLE"
    # Header body is just the automation tag; text must be the synthesized
    # sentence, not the raw tag markup.
    assert ac["text"] == (
        "Given user is on the login page, "
        "When they submit valid credentials, "
        "Then the dashboard is displayed"
    )


def test_extract_acceptance_criteria_nested_bold_gwt_multiple_acs() -> None:
    md = """# Spec
## Acceptance Criteria
- [ ] **AC-1:** `[AUTOMATABLE]`
  - **Given** precondition one
  - **When** action one
  - **Then** expected one
- [ ] **AC-2:** `[MANUAL ONLY]`
  - **Given** precondition two
  - **When** action two
  - **Then** expected two
"""
    root = parse_markdown(md)
    out = _extract_acceptance_criteria_structured(root)
    assert [a["id"] for a in out] == ["AC-1", "AC-2"]
    assert out[0]["given"] == "precondition one"
    assert out[0]["when"] == "action one"
    assert out[0]["then"] == "expected one"
    assert out[0]["automation"] == "AUTOMATABLE"
    assert out[1]["given"] == "precondition two"
    assert out[1]["when"] == "action two"
    assert out[1]["then"] == "expected two"
    assert out[1]["automation"] == "MANUAL_ONLY"


def test_extract_acceptance_criteria_mixed_legacy_and_nested_formats() -> None:
    md = """# Spec
## Acceptance Criteria
- [ ] AC-1: Given old style, When submit, Then result shown. `[AUTOMATABLE]`
- [ ] **AC-2:** `[MANUAL ONLY]`
  - **Given** new style precondition
  - **When** new style action
  - **Then** new style expected
"""
    root = parse_markdown(md)
    out = _extract_acceptance_criteria_structured(root)
    assert [a["id"] for a in out] == ["AC-1", "AC-2"]
    assert out[0]["given"] == "old style"
    assert out[0]["when"] == "submit"
    assert out[0]["then"] == "result shown."
    assert out[0]["automation"] == "AUTOMATABLE"
    assert out[1]["given"] == "new style precondition"
    assert out[1]["when"] == "new style action"
    assert out[1]["then"] == "new style expected"
    assert out[1]["automation"] == "MANUAL_ONLY"


def test_s02_full_projection_validates_against_schema_nested_bold_gwt() -> None:
    md = """# Login

**Requirement ID:** REQ-LOGIN

## Acceptance Criteria
- [ ] **AC-1:** `[AUTOMATABLE]`
  - **Given** user
  - **When** login
  - **Then** dashboard.

## Edge Cases & Risks
| ID | Edge Case | Severity | Automation | Mitigation |
|----|-----------|----------|------------|------------|
| EC-1 | drop | high | [AUTOMATABLE] | retry |

## Non-Functional Requirements
- **Performance:** p95 <= 2.5s.

## Coverage Notes
- **AC-7:** Dropped - reason.
"""
    proj = _project_to_json(md)
    ok, err = is_valid(proj, "refined-spec")
    assert ok, err
    ac = proj["acceptance_criteria_structured"][0]
    assert ac["given"] == "user"
    assert ac["when"] == "login"
    assert ac["then"] == "dashboard."


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


def test_coverage_notes_accepted_risk_qualified_and_underscore_forms() -> None:
    """Regression: `Dropped (accepted risk)` and `accepted_risk` must parse as
    accepted_risk. Before the fix the `(accepted risk)` parenthetical (and the
    `_` in `accepted_risk`) broke the regex entirely, silently discarding the
    bullet so the accepted-risk drop never reached the traceability matrix."""
    md = """# Design
## Coverage Notes
- **TC-SVP-014:** Dropped (accepted risk) — perf has no numeric oracle (EC-3 `[NEEDS INVESTIGATION]`).
- **TC-SVP-015:** accepted_risk — `/prices` (EBASBBM-16937) has no automatable oracle.
- **AC-7:** Dropped — genuine unaccepted orphan, no resolution.
- **mobile:** Excluded — dropped the whole area, out of scope.
"""
    root = parse_markdown(md)
    res_by_id = {n["item_id"]: n["resolution"] for n in extract_coverage_notes(root)}
    assert res_by_id == {
        "TC-SVP-014": "accepted_risk",
        "TC-SVP-015": "accepted_risk",
        # plain `Dropped` stays an orphan — the parenthetical is the discriminator
        "AC-7": "dropped",
        # anti-over-match: "dropped" only in the reason text must not flip it
        "mobile": "scope_excluded",
    }


def test_coverage_notes_unrecognized_keyword_defaults_to_dropped() -> None:
    """A structural bullet with an unknown keyword resolves to `dropped` (a
    loud orphan) rather than `accepted_risk`, so parser drift surfaces as an
    audit failure instead of silently exempting a TC."""
    md = """# Design
## Coverage Notes
- **TC-99:** Deferred — some novel keyword the map does not know.
"""
    root = parse_markdown(md)
    out = extract_coverage_notes(root)
    assert out == [
        {"item_id": "TC-99", "reason": "some novel keyword the map does not know.",
         "resolution": "dropped"},
    ]


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
    ok, err = is_valid(proj, "test-design")
    assert ok, err
