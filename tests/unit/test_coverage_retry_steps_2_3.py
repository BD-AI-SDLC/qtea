"""Retry-loop integration tests for the coverage audit on Steps 2 + 3.

Verifies the PR 3 wiring:
- audit fires after schema validation (only when WORCA_T_COVERAGE_AUDIT=1)
- a violation writes `audit-violations.log` and returns `status="failed"`
- the next `run()` reads the log, prepends to the user prompt, deletes it,
  and succeeds when the agent emits a clean artifact
- two consecutive failures hard-fail (caller-driven simulation of the
  `Step.execute` retry contract — keeps these tests subprocess-free)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s02_refine import RefineStep
from worca_t.steps.s03_plan import PlanStep
from worca_t.workspace import create_workspace
from tests.unit._fake_anthropic import install_fake_anthropic


# ---------- canned markdown fixtures ----------

_REFINED_BROKEN = """# Login
**Requirement ID:** REQ-LOGIN

## Acceptance Criteria
- The user can log in
"""

_REFINED_CLEAN = """# Login
**Requirement ID:** REQ-LOGIN

## Acceptance Criteria
- [ ] AC-1: Given user on /login, When valid creds, Then dashboard shows. `[AUTOMATABLE]`
"""

_PLAN_BROKEN = """# Test Plan: Login

## Phase 1: Auth

### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | Automation |
|-------|-------|------|----------|--------|-----|------------|
| TC-AUTH-001 | Login | smoke | P0 | REQ-LOGIN |  | automation |
"""

_PLAN_CLEAN = """# Test Plan: Login

## Phase 1: Auth

### TC Roster
| TC ID | Title | Type | Priority | Req ID | ACs | Automation |
|-------|-------|------|----------|--------|-----|------------|
| TC-AUTH-001 | Login | smoke | P0 | REQ-LOGIN | AC-1 | automation |
"""


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    (ws.step_dir(1) / "spec.md").write_text("# Login\n\nstub", encoding="utf-8")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=".",
    )
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(
        workspace=ws, state=state, spec_source="x", sut_source=".", options=opts,
    )


# ============================================================
# Step 2 retry semantics
# ============================================================

async def test_step2_passes_without_audit_when_gate_off(tmp_path: Path, monkeypatch):
    """Default (gate OFF) preserves existing behavior — broken markdown is
    accepted via the warned-only schema path."""
    monkeypatch.delenv("WORCA_T_COVERAGE_AUDIT", raising=False)
    install_fake_anthropic(monkeypatch, text=_REFINED_BROKEN)
    result = await RefineStep().run(_ctx(tmp_path))
    assert result.success, result.error


async def test_step2_audit_failure_writes_log_and_returns_failed(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    install_fake_anthropic(monkeypatch, text=_REFINED_BROKEN)
    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert not result.success
    assert result.status == "failed"
    log_path = ctx.workspace.step_dir(2) / "audit-violations.log"
    assert log_path.exists()
    body = log_path.read_text(encoding="utf-8")
    assert "AC-?" in body or "AC-ID" in body
    assert "Coverage audit failed" in (result.error or "")


async def test_step2_audit_log_consumed_and_deleted_on_next_run(
    tmp_path: Path, monkeypatch,
):
    """The next `run()` reads the prior log, prepends it to the user prompt,
    deletes it, then succeeds when the agent emits a clean artifact."""
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    captured: list[dict] = []
    install_fake_anthropic(
        monkeypatch,
        texts=[_REFINED_BROKEN, _REFINED_CLEAN],
        on_call=captured.append,
    )
    ctx = _ctx(tmp_path)

    # Attempt 1 fails the audit, writes the log.
    r1 = await RefineStep().run(ctx)
    assert not r1.success
    log_path = ctx.workspace.step_dir(2) / "audit-violations.log"
    assert log_path.exists()

    # Attempt 2 reads it, deletes it, and succeeds.
    r2 = await RefineStep().run(ctx)
    assert r2.success, r2.error
    assert not log_path.exists()

    # The retry call's user message was prepended with the violation report.
    second_call = captured[1]
    user_msg = "".join(
        block["text"] if isinstance(block, dict) else block.text
        for msg in second_call["messages"]
        if msg["role"] == "user"
        for block in (
            msg["content"] if isinstance(msg["content"], list) else [{"text": msg["content"]}]
        )
    )
    assert "Your previous attempt FAILED the coverage audit" in user_msg
    assert "AC-?" in user_msg or "no AC-ID" in user_msg


async def test_step2_two_consecutive_audit_failures_hard_fail(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    install_fake_anthropic(monkeypatch, texts=[_REFINED_BROKEN, _REFINED_BROKEN])
    ctx = _ctx(tmp_path)
    r1 = await RefineStep().run(ctx)
    assert not r1.success
    r2 = await RefineStep().run(ctx)
    assert not r2.success
    assert r2.status == "failed"
    # Log re-written with attempt 2's violations — survives for downstream tools.
    log_path = ctx.workspace.step_dir(2) / "audit-violations.log"
    assert log_path.exists()


# ============================================================
# Step 3 retry semantics
# ============================================================

async def _seed_refined_spec(ctx: StepContext, *, clean: bool = True) -> None:
    """Step 3 needs refined-spec.md + refined-spec.json present from Step 2."""
    refined_md = _REFINED_CLEAN if clean else _REFINED_BROKEN
    md_path = ctx.workspace.step_dir(2) / "refined-spec.md"
    md_path.write_text(refined_md, encoding="utf-8")
    # Build the JSON projection the audit needs.
    from worca_t.steps.s02_refine import _project_to_json
    import json
    projection = _project_to_json(refined_md)
    (ctx.workspace.step_dir(2) / "refined-spec.json").write_text(
        json.dumps(projection, indent=2), encoding="utf-8",
    )


async def test_step3_passes_without_audit_when_gate_off(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WORCA_T_COVERAGE_AUDIT", raising=False)
    ctx = _ctx(tmp_path)
    await _seed_refined_spec(ctx, clean=True)
    install_fake_anthropic(monkeypatch, text=_PLAN_BROKEN)
    result = await PlanStep().run(ctx)
    assert result.success, result.error


async def test_step3_audit_failure_writes_log_and_returns_failed(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    await _seed_refined_spec(ctx, clean=True)
    install_fake_anthropic(monkeypatch, text=_PLAN_BROKEN)
    result = await PlanStep().run(ctx)
    assert not result.success
    assert result.status == "failed"
    log_path = ctx.workspace.step_dir(3) / "audit-violations.log"
    assert log_path.exists()
    body = log_path.read_text(encoding="utf-8")
    # AC-1 is in the refined spec; the broken plan has TC-AUTH-001 with empty
    # ac_ids, so AC-1 has no covering TC.
    assert "AC-1" in body


async def test_step3_audit_log_consumed_on_retry_and_succeeds(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    captured: list[dict] = []
    install_fake_anthropic(
        monkeypatch,
        texts=[_PLAN_BROKEN, _PLAN_CLEAN],
        on_call=captured.append,
    )
    ctx = _ctx(tmp_path)
    await _seed_refined_spec(ctx, clean=True)

    r1 = await PlanStep().run(ctx)
    assert not r1.success
    log_path = ctx.workspace.step_dir(3) / "audit-violations.log"
    assert log_path.exists()

    r2 = await PlanStep().run(ctx)
    assert r2.success, r2.error
    assert not log_path.exists()

    second_call = captured[1]
    user_msg = "".join(
        block["text"] if isinstance(block, dict) else block.text
        for msg in second_call["messages"]
        if msg["role"] == "user"
        for block in (
            msg["content"] if isinstance(msg["content"], list) else [{"text": msg["content"]}]
        )
    )
    assert "Your previous attempt FAILED the coverage audit" in user_msg


async def test_step3_audit_skipped_when_refined_spec_json_missing(
    tmp_path: Path, monkeypatch,
):
    """Defensive: if Step 2's JSON is missing (legacy workspace), the audit
    is skipped with a warning rather than crashing."""
    monkeypatch.setenv("WORCA_T_COVERAGE_AUDIT", "1")
    ctx = _ctx(tmp_path)
    (ctx.workspace.step_dir(2) / "refined-spec.md").write_text(
        _REFINED_CLEAN, encoding="utf-8",
    )
    # Intentionally no refined-spec.json.
    install_fake_anthropic(monkeypatch, text=_PLAN_BROKEN)
    result = await PlanStep().run(ctx)
    assert result.success, result.error
