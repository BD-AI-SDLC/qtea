"""Tests for `worca_t.coverage_audit` — the 3 audits + matrix generator
introduced in PR 2. Module is unused at this point in the roll-out; PR 3
wires it into Steps 2/3 and PR 4 into Step 4.

Each rule is exercised with one fail case and one clean case where it
adds value (clean cases are wrapped into the broader projections).
"""

from __future__ import annotations

from worca_t.coverage_audit import (
    _format_violations_for_agent,
    audit_plan,
    audit_refined_spec,
    audit_strategy,
    audit_traceability_matrix,
    build_traceability_matrix,
)
from worca_t.schemas import is_valid

# ---------- helpers ----------

def _spec(
    *,
    acs=None,
    legacy_acs=None,
    ecs=None,
    nfrs=None,
    coverage_notes=None,
    sections=None,
) -> dict:
    return {
        "requirement_id": "REQ-X",
        "title": "X",
        "sections": sections or [],
        "acceptance_criteria": legacy_acs or [],
        "acceptance_criteria_structured": acs or [],
        "edge_cases_structured": ecs or [],
        "nfrs_structured": nfrs or [],
        "coverage_notes": coverage_notes or [],
    }


def _ac(ac_id: str, *, automation="AUTOMATABLE", text="text", priority="P1", user_flow=None):
    return {
        "id": ac_id, "text": text, "automation": automation,
        "priority": priority, "user_flow": user_flow,
        "requires_tc": True, "promoted_from_nfr": False,
    }


def _ec(ec_id: str, *, severity="high", automation="AUTOMATABLE"):
    return {
        "id": ec_id, "text": "ec text", "severity": severity,
        "automation": automation, "mitigation": None,
    }


def _nfr(nfr_id: str, *, has_threshold=True, promoted_to_ac=None, category="performance"):
    return {
        "id": nfr_id, "text": "nfr text", "category": category,
        "has_threshold": has_threshold, "promoted_to_ac": promoted_to_ac,
    }


def _plan(*, tcs=None, coverage_notes=None) -> dict:
    return {
        "title": "Plan",
        "phases": [{"number": 1, "title": "P1", "files": [], "success_criteria": []}],
        "test_cases": tcs or [],
        "coverage_notes": coverage_notes or [],
    }


def _ptc(tc_id: str, *, req_id="REQ-X", priority="P1", ac_ids=(), ec_ids=(), automation="automation"):
    return {
        "id": tc_id, "title": tc_id, "priority": priority, "type": "smoke",
        "req_id": req_id, "ac_ids": list(ac_ids), "ec_ids": list(ec_ids),
        "nfr_ids": [], "automation": automation, "phase": 1,
        "parametrized_over": [],
    }


def _strategy(*, tcs=None, coverage_notes=None) -> dict:
    return {
        "title": "Strategy",
        "test_cases": tcs or [],
        "coverage_notes": coverage_notes or [],
    }


def _stc(tc_id: str, *, derived_from=None, priority="P0", automation_type="ui",
         ac_ids=(), ec_ids=()):
    return {
        "id": tc_id, "title": tc_id, "priority": priority, "type": "ui",
        "preconditions": [], "steps": [], "expected": "x", "tags": [], "raw": "",
        "req_id": "REQ-X", "ac_ids": list(ac_ids), "ec_ids": list(ec_ids),
        "nfr_ids": [], "derived_from": derived_from or [tc_id],
        "automation_type": automation_type,
    }


# ============================================================
# audit_refined_spec
# ============================================================

def test_audit_spec_clean_passes() -> None:
    spec = _spec(acs=[_ac("AC-1"), _ac("AC-2")])
    assert audit_refined_spec(spec) == []


def test_audit_spec_orphan_legacy_bullet_flagged() -> None:
    spec = _spec(legacy_acs=["a bullet with no ID"])
    v = audit_refined_spec(spec)
    assert any("has no AC-ID" in line for line in v)


def test_audit_spec_orphan_legacy_bullet_suppressed_by_coverage_note() -> None:
    spec = _spec(
        legacy_acs=["a bullet with no ID"],
        coverage_notes=[{"item_id": "a bullet with no ID",
                         "reason": "deferred", "resolution": "accepted_risk"}],
    )
    v = audit_refined_spec(spec)
    assert not any("has no AC-ID" in line for line in v)


def test_audit_spec_missing_automation_tag_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1", automation="UNKNOWN")])
    v = audit_refined_spec(spec)
    assert any("missing automation tag" in line and "AC-1" in line for line in v)


def test_audit_spec_nfr_with_threshold_not_promoted_flagged() -> None:
    spec = _spec(nfrs=[_nfr("NFR-PERF-1", has_threshold=True, promoted_to_ac=None)])
    v = audit_refined_spec(spec)
    assert any("NFR-PERF-1" in line and "promoted" in line for line in v)


def test_audit_spec_nfr_promoted_to_existing_ac_passes() -> None:
    spec = _spec(
        acs=[_ac("AC-PERF-1")],
        nfrs=[_nfr("NFR-PERF-1", has_threshold=True, promoted_to_ac="AC-PERF-1")],
    )
    v = audit_refined_spec(spec)
    assert not any("NFR-PERF-1" in line for line in v)


def test_audit_spec_nfr_promoted_to_missing_ac_flagged() -> None:
    spec = _spec(
        acs=[_ac("AC-1")],
        nfrs=[_nfr("NFR-PERF-1", has_threshold=True, promoted_to_ac="AC-PERF-1")],
    )
    v = audit_refined_spec(spec)
    assert any("AC-PERF-1" in line and "not in" in line for line in v)


def test_audit_spec_alt_flow_bullet_uncovered_flagged() -> None:
    spec = _spec(
        acs=[_ac("AC-1", user_flow="happy path")],
        sections=[{
            "title": "User Flows",
            "level": 2,
            "content": "",
            "bullets": [],
            "tables": [],
            "children": [{
                "title": "Alternative Flow",
                "level": 3,
                "content": "",
                "bullets": ["expired session redirects to /login"],
                "tables": [],
                "children": [],
            }],
        }],
    )
    v = audit_refined_spec(spec)
    assert any("expired session" in line for line in v)


def test_audit_spec_alt_flow_bullet_with_requires_tc_marker_passes() -> None:
    spec = _spec(
        acs=[_ac("AC-1")],
        sections=[{
            "title": "User Flows",
            "level": 2,
            "content": "",
            "bullets": [],
            "tables": [],
            "children": [{
                "title": "Alternative Flow",
                "level": 3,
                "content": "",
                "bullets": ["expired session redirects [requires TC]"],
                "tables": [],
                "children": [],
            }],
        }],
    )
    v = audit_refined_spec(spec)
    assert not any("expired session" in line for line in v)


# ============================================================
# audit_plan
# ============================================================

def test_audit_plan_clean_passes() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1"], priority="P0")])
    assert audit_plan(plan, spec) == []


def test_audit_plan_ac_missing_tc_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1"), _ac("AC-2")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1"])])
    v = audit_plan(plan, spec)
    assert any("AC-2" in line and "no covering test case" in line for line in v)


def test_audit_plan_high_severity_ec_missing_tc_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1")], ecs=[_ec("EC-1", severity="high")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1"])])
    v = audit_plan(plan, spec)
    assert any("EC-1" in line and "no covering TC" in line for line in v)


def test_audit_plan_low_severity_ec_not_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1")], ecs=[_ec("EC-1", severity="low")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1"])])
    v = audit_plan(plan, spec)
    assert not any("EC-1" in line for line in v)


def test_audit_plan_priority_mixed_bundle_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0"), _ac("AC-2", priority="P2")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1", "AC-2"], priority="P2")])
    v = audit_plan(plan, spec)
    assert any("priority" in line and "TC-1" in line for line in v)


def test_audit_plan_priority_inheritance_passes_when_tc_at_correct_priority() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0"), _ac("AC-2", priority="P2")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1", "AC-2"], priority="P0")])
    v = audit_plan(plan, spec)
    assert not any("priority" in line for line in v)


def test_audit_plan_missing_req_id_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-1"], req_id="")])
    v = audit_plan(plan, spec)
    assert any("TC-1" in line and "missing req_id" in line for line in v)


def test_audit_plan_dangling_ac_reference_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-1", ac_ids=["AC-99"])])
    v = audit_plan(plan, spec)
    assert any("AC-99" in line and "does not exist" in line for line in v)


def test_audit_plan_dropped_ac_must_be_in_plan_coverage_notes() -> None:
    spec = _spec(
        acs=[_ac("AC-1")],
        coverage_notes=[{"item_id": "AC-1", "reason": "out", "resolution": "dropped"}],
    )
    plan = _plan()  # no TCs, no coverage_notes
    v = audit_plan(plan, spec)
    assert any("AC-1" in line and "carry the drop" in line for line in v)


def test_audit_plan_drop_carried_forward_passes() -> None:
    spec = _spec(
        acs=[_ac("AC-1")],
        coverage_notes=[{"item_id": "AC-1", "reason": "out", "resolution": "dropped"}],
    )
    plan = _plan(coverage_notes=[
        {"item_id": "AC-1", "reason": "dropped upstream", "resolution": "dropped"},
    ])
    v = audit_plan(plan, spec)
    assert not any("AC-1" in line for line in v)


# ============================================================
# audit_strategy
# ============================================================

def test_audit_strategy_clean_passes() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"], priority="P0")])
    strategy = _strategy(tcs=[_stc("TC-S-001", derived_from=["TC-PLAN-001"], priority="P0")])
    assert audit_strategy(strategy, plan, spec, raw_md="") == []


def test_audit_strategy_orphan_plan_tc_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[])
    v = audit_strategy(strategy, plan, spec, raw_md="")
    assert any("TC-PLAN-001" in line and "no corresponding strategy TC" in line for line in v)


def test_audit_strategy_orphan_plan_tc_suppressed_by_accepted_risk_note() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(
        tcs=[],
        coverage_notes=[{"item_id": "TC-PLAN-001", "reason": "duplicate",
                         "resolution": "accepted_risk"}],
    )
    v = audit_strategy(strategy, plan, spec, raw_md="")
    assert not any("TC-PLAN-001" in line for line in v)


def test_audit_strategy_cross_priority_consolidation_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0"), _ac("AC-2", priority="P2")])
    plan = _plan(tcs=[
        _ptc("TC-PLAN-001", ac_ids=["AC-1"], priority="P0"),
        _ptc("TC-PLAN-002", ac_ids=["AC-2"], priority="P2"),
    ])
    strategy = _strategy(tcs=[
        _stc("TC-MERGE", derived_from=["TC-PLAN-001", "TC-PLAN-002"], priority="P0"),
    ])
    v = audit_strategy(strategy, plan, spec, raw_md="")
    assert any("TC-MERGE" in line and "mixed priorities" in line for line in v)


def test_audit_strategy_cross_automation_type_consolidation_flagged() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0"), _ac("AC-2", priority="P0")])
    plan = _plan(tcs=[
        _ptc("TC-PLAN-001", ac_ids=["AC-1"], priority="P0", automation="automation"),
        _ptc("TC-PLAN-002", ac_ids=["AC-2"], priority="P0", automation="manual"),
    ])
    strategy = _strategy(tcs=[
        _stc("TC-MERGE", derived_from=["TC-PLAN-001", "TC-PLAN-002"], priority="P0"),
    ])
    v = audit_strategy(strategy, plan, spec, raw_md="")
    assert any("TC-MERGE" in line and "mixed" in line and "automation" in line for line in v)


def test_audit_strategy_forbidden_assumptions_section_flagged_via_raw_md() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[_stc("TC-S", derived_from=["TC-PLAN-001"])])
    raw = "# Strategy\n## Test Cases\n#### TC-S: foo\n\n## Assumptions\n- mobile out of scope\n"
    v = audit_strategy(strategy, plan, spec, raw_md=raw)
    assert any("Assumptions" in line and "forbidden" in line for line in v)


# ============================================================
# build_traceability_matrix + audit_traceability_matrix
# ============================================================

def test_build_matrix_mapped_1_to_1() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"], priority="P0")])
    strategy = _strategy(tcs=[_stc("TC-S-001", derived_from=["TC-PLAN-001"])])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    assert m["entries"] == [{
        "plan_tc_id": "TC-PLAN-001",
        "strategy_tc_id": "TC-S-001",
        "ac_ids": ["AC-1"],
        "ec_ids": [],
        "nfr_ids": [],
        "priority": "P0",
        "automation_type": "ui",
        "resolution": "mapped",
    }]
    assert m["summary"]["consolidated_count"] == 0
    assert m["summary"]["orphan_acs"] == []
    assert is_valid(m, "traceability-matrix") == (True, None)


def test_build_matrix_merged_collapses_multiple_plan_tcs() -> None:
    spec = _spec(acs=[_ac("AC-1", priority="P0"), _ac("AC-2", priority="P0")])
    plan = _plan(tcs=[
        _ptc("TC-PLAN-001", ac_ids=["AC-1"], priority="P0"),
        _ptc("TC-PLAN-002", ac_ids=["AC-2"], priority="P0"),
    ])
    strategy = _strategy(tcs=[
        _stc("TC-MERGE", derived_from=["TC-PLAN-001", "TC-PLAN-002"], priority="P0"),
    ])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    resolutions = [e["resolution"] for e in m["entries"]]
    assert resolutions == ["merged", "merged"]
    assert m["summary"]["consolidated_count"] == 1
    assert is_valid(m, "traceability-matrix") == (True, None)


def test_build_matrix_split_when_one_plan_covered_by_multiple_strategy_tcs() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[
        _stc("TC-S-A", derived_from=["TC-PLAN-001"]),
        _stc("TC-S-B", derived_from=["TC-PLAN-001"]),
    ])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    resolutions = [e["resolution"] for e in m["entries"]]
    assert resolutions == ["split", "split"]


def test_build_matrix_dropped_accepted_risk_when_coverage_note_marks_it() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(
        tcs=[],
        coverage_notes=[{"item_id": "TC-PLAN-001", "reason": "dup",
                         "resolution": "accepted_risk"}],
    )
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    assert m["entries"][0]["resolution"] == "dropped_accepted_risk"
    assert m["summary"]["accepted_risk_drops"] == ["TC-PLAN-001"]
    assert m["summary"]["orphan_plan_tcs"] == []


def test_build_matrix_dropped_without_note_is_a_hard_orphan() -> None:
    spec = _spec(acs=[_ac("AC-1")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    assert m["entries"][0]["resolution"] == "dropped"
    assert m["summary"]["orphan_plan_tcs"] == ["TC-PLAN-001"]


def test_build_matrix_summary_lists_orphan_acs() -> None:
    spec = _spec(acs=[_ac("AC-1"), _ac("AC-2")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[_stc("TC-S-001", derived_from=["TC-PLAN-001"])])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    assert m["summary"]["orphan_acs"] == ["AC-2"]


def test_audit_matrix_flags_orphans() -> None:
    spec = _spec(acs=[_ac("AC-1"), _ac("AC-2")])
    plan = _plan(tcs=[_ptc("TC-PLAN-001", ac_ids=["AC-1"])])
    strategy = _strategy(tcs=[])
    m = build_traceability_matrix(spec, plan, strategy, run_id="r1")
    v = audit_traceability_matrix(m)
    assert any("TC-PLAN-001" in line for line in v)
    assert any("AC-2" in line for line in v)


# ---------- helper formatting ----------

def test_format_violations_for_agent_includes_count_and_lines() -> None:
    out = _format_violations_for_agent("plan", ["TC-1: x", "AC-2: y"])
    assert "Coverage audit failed for plan: 2 violation(s)" in out
    assert "TC-1: x" in out and "AC-2: y" in out
    assert "## Coverage Notes" in out  # instruction included
