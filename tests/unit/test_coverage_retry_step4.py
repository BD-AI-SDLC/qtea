"""Retry-loop integration tests for Step 4 — the riskiest chunk of the
coverage-audit roll-out.

Step 4 is non-HITL (`call_reasoning_llm`, not `_with_hitl`), so the
prior-attempt prepend is the ONLY feedback channel the LLM has on retry.

Covers:
- gate OFF preserves existing behavior (matrix not written, no audit)
- gate ON + clean strategy: matrix written, schema-valid, no orphans
- gate ON + orphan plan TC in strategy: audit fails → log written
- gate ON + accepted-risk Coverage Note: matrix entry uses
  resolution=`dropped_accepted_risk`, no violations
- audit log consumed-and-deleted on next run (prepend mechanism)
- cross-priority consolidation triggers audit failure
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit._fake_anthropic import install_fake_anthropic
from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s04_strategy import StrategyStep
from worca_t.workspace import create_workspace

# ---------- canned markdown fixtures ----------

_REFINED_MD = """# Login
**Requirement ID:** REQ-LOGIN

## Acceptance Criteria
- [ ] AC-1: Given user, When login, Then dashboard. `[AUTOMATABLE]`
- [ ] AC-2: Given bad pass, When login, Then error. `[AUTOMATABLE]`
"""

_PLAN_MD = """# Test Plan: Login

## Phase 1: Auth

### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | Automation |
|-------|-------|------|----------|--------|-----|------------|
| TC-PLAN-001 | Login happy | smoke | P0 | REQ-LOGIN | AC-1 | automation |
| TC-PLAN-002 | Login bad creds | smoke | P1 | REQ-LOGIN | AC-2 | automation |
"""

_STRATEGY_CLEAN = """# Test Strategy

## Scope
Login.

## Test Cases

#### TC-S-001: Login happy path
- **Type**: UI
- **Priority**: P0
- **Req ID**: REQ-LOGIN
- **ACs**: AC-1
- **Derived from**: TC-PLAN-001
- **Automation Type**: ui
- **Steps**:
  1. visit /login
- **Expected**: dashboard

#### TC-S-002: Login bad creds
- **Type**: UI
- **Priority**: P1
- **Req ID**: REQ-LOGIN
- **ACs**: AC-2
- **Derived from**: TC-PLAN-002
- **Automation Type**: ui
- **Steps**:
  1. visit /login
- **Expected**: error
"""

_STRATEGY_DROPS_PLAN_002 = """# Test Strategy

## Scope
Login.

## Test Cases

#### TC-S-001: Login happy path
- **Type**: UI
- **Priority**: P0
- **Req ID**: REQ-LOGIN
- **ACs**: AC-1
- **Derived from**: TC-PLAN-001
- **Automation Type**: ui
- **Steps**:
  1. visit /login
- **Expected**: dashboard
"""

_STRATEGY_ACCEPTS_RISK_FOR_PLAN_002 = """# Test Strategy

## Scope
Login.

## Test Cases

#### TC-S-001: Login happy path
- **Type**: UI
- **Priority**: P0
- **Req ID**: REQ-LOGIN
- **ACs**: AC-1, AC-2
- **Derived from**: TC-PLAN-001
- **Automation Type**: ui
- **Steps**:
  1. visit /login
- **Expected**: dashboard

## Coverage Notes
- **TC-PLAN-002:** Accepted risk — coverage by TC-S-001's negative path branch.
"""

_STRATEGY_CROSS_PRIORITY_MERGE = """# Test Strategy

## Test Cases

#### TC-S-MERGE: Consolidated
- **Type**: UI
- **Priority**: P0
- **Req ID**: REQ-LOGIN
- **ACs**: AC-1, AC-2
- **Derived from**: TC-PLAN-001, TC-PLAN-002
- **Automation Type**: ui
- **Steps**:
  1. x
- **Expected**: y
"""


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=".",
    )
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(
        workspace=ws, state=state, spec_source="x", sut_source=".", options=opts,
    )


def _seed_upstream(ctx: StepContext) -> None:
    """Populate Step 2 + Step 3 artifacts (md + json) that Step 4 reads."""
    from worca_t.steps.s02_refine import _project_to_json
    from worca_t.steps.s03_plan import _project_plan

    step2 = ctx.workspace.step_dir(2)
    (step2 / "refined-spec.md").write_text(_REFINED_MD, encoding="utf-8")
    (step2 / "refined-spec.json").write_text(
        json.dumps(_project_to_json(_REFINED_MD), indent=2), encoding="utf-8",
    )
    step3 = ctx.workspace.step_dir(3)
    (step3 / "plan.md").write_text(_PLAN_MD, encoding="utf-8")
    (step3 / "plan.json").write_text(
        json.dumps(_project_plan(_PLAN_MD), indent=2), encoding="utf-8",
    )


# ============================================================
# Gate OFF — back-compat
# ============================================================

async def test_step4_gate_off_preserves_existing_behavior(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.delenv("WORCA_T_COVERAGE_AUDIT", raising=False)
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    install_fake_anthropic(monkeypatch, text=_STRATEGY_DROPS_PLAN_002)
    result = await StrategyStep().run(ctx)
    assert result.success, result.error
    # Matrix is NOT written when the gate is off.
    assert not (ctx.workspace.step_dir(4) / "traceability-matrix.json").exists()


# ============================================================
# Clean happy path — matrix emitted
# ============================================================

async def test_step4_clean_strategy_emits_traceability_matrix(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    install_fake_anthropic(monkeypatch, text=_STRATEGY_CLEAN)
    result = await StrategyStep().run(ctx)
    assert result.success, result.error
    matrix_path = ctx.workspace.step_dir(4) / "traceability-matrix.json"
    assert matrix_path.exists()
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    assert matrix["requirement_id"] == "REQ-LOGIN"
    assert matrix["summary"]["plan_tc_count"] == 2
    assert matrix["summary"]["strategy_tc_count"] == 2
    assert matrix["summary"]["orphan_plan_tcs"] == []
    assert matrix["summary"]["orphan_acs"] == []
    resolutions = [e["resolution"] for e in matrix["entries"]]
    assert resolutions == ["mapped", "mapped"]


# ============================================================
# Audit failure paths
# ============================================================

async def test_step4_orphan_plan_tc_triggers_audit_failure(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    install_fake_anthropic(monkeypatch, text=_STRATEGY_DROPS_PLAN_002)
    result = await StrategyStep().run(ctx)
    assert not result.success
    assert result.status == "failed"
    log_path = ctx.workspace.step_dir(4) / "audit-violations.log"
    assert log_path.exists()
    log_body = log_path.read_text(encoding="utf-8")
    assert "TC-PLAN-002" in log_body
    # Matrix still emitted so users can inspect the gap.
    assert (ctx.workspace.step_dir(4) / "traceability-matrix.json").exists()


async def test_step4_accepted_risk_coverage_note_suppresses_orphan(
    tmp_path: Path, monkeypatch,
):
    """Plan TC dropped + Coverage Notes accepted_risk entry → matrix records
    `dropped_accepted_risk` and audit passes."""
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    install_fake_anthropic(monkeypatch, text=_STRATEGY_ACCEPTS_RISK_FOR_PLAN_002)
    result = await StrategyStep().run(ctx)
    assert result.success, result.error
    matrix_path = ctx.workspace.step_dir(4) / "traceability-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    plan_002 = next(e for e in matrix["entries"] if e["plan_tc_id"] == "TC-PLAN-002")
    assert plan_002["resolution"] == "dropped_accepted_risk"
    assert plan_002["strategy_tc_id"] is None
    assert matrix["summary"]["accepted_risk_drops"] == ["TC-PLAN-002"]


async def test_step4_cross_priority_consolidation_triggers_audit_failure(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    install_fake_anthropic(monkeypatch, text=_STRATEGY_CROSS_PRIORITY_MERGE)
    result = await StrategyStep().run(ctx)
    assert not result.success
    log_body = (ctx.workspace.step_dir(4) / "audit-violations.log").read_text(
        encoding="utf-8",
    )
    assert "mixed priorities" in log_body
    assert "TC-S-MERGE" in log_body


# ============================================================
# Retry: audit log consumed-and-deleted on next run
# ============================================================

async def test_step4_audit_log_consumed_on_retry_and_succeeds(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    captured: list[dict] = []
    install_fake_anthropic(
        monkeypatch,
        texts=[_STRATEGY_DROPS_PLAN_002, _STRATEGY_CLEAN],
        on_call=captured.append,
    )
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)

    r1 = await StrategyStep().run(ctx)
    assert not r1.success
    log_path = ctx.workspace.step_dir(4) / "audit-violations.log"
    assert log_path.exists()

    r2 = await StrategyStep().run(ctx)
    assert r2.success, r2.error
    assert not log_path.exists()

    second_call = captured[1]
    user_msg = "".join(
        block["text"] if isinstance(block, dict) else block.text
        for msg in second_call["messages"]
        if msg["role"] == "user"
        for block in (
            msg["content"] if isinstance(msg["content"], list)
            else [{"text": msg["content"]}]
        )
    )
    assert "Your previous attempt FAILED the coverage audit" in user_msg
    assert "TC-PLAN-002" in user_msg


async def test_step4_two_consecutive_audit_failures_hard_fail(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    install_fake_anthropic(
        monkeypatch,
        texts=[_STRATEGY_DROPS_PLAN_002, _STRATEGY_DROPS_PLAN_002],
    )
    ctx = _ctx(tmp_path)
    _seed_upstream(ctx)
    r1 = await StrategyStep().run(ctx)
    assert not r1.success
    r2 = await StrategyStep().run(ctx)
    assert not r2.success
    assert r2.status == "failed"
    log_path = ctx.workspace.step_dir(4) / "audit-violations.log"
    assert log_path.exists()
