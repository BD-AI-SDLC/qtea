"""Debug/fix flow tests (M9): retry, debug snapshots, fix proposals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import (
    Step,
    StepContext,
    StepResult,
    _run_fix_proposal,
    _snapshot_debug_artifacts,
)
from worca_t.workspace import create_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **opts_kw) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    defaults = {"spec": "x", "sut": ".", "workspace_base": tmp_path / ".ws"}
    defaults.update(opts_kw)
    opts = PipelineOptions(**defaults)
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


class _FailOnceStep(Step):
    """Fails on attempt 1, succeeds on attempt 2."""
    number = 99
    name = "fail-once"
    timeout_s = 60
    _call_count = 0

    def __init__(self):
        self._call_count = 0

    def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            return StepResult(success=False, status="failed", outputs=[], error="first attempt fail")
        return StepResult(success=True, status="completed", outputs=[], notes="second attempt ok")


class _AlwaysFailStep(Step):
    """Always fails."""
    number = 98
    name = "always-fail"
    timeout_s = 60

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=False, status="failed", outputs=[], error="always fails")


class _AlwaysPassStep(Step):
    """Always passes."""
    number = 97
    name = "always-pass"
    timeout_s = 60

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=True, status="completed", outputs=[], notes="ok")


class _ExceptionStep(Step):
    """Raises on first attempt, passes on second."""
    number = 96
    name = "exception-once"
    timeout_s = 60

    def __init__(self):
        self._call_count = 0

    def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("boom")
        return StepResult(success=True, status="completed", outputs=[], notes="recovered")


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


def test_step_succeeds_first_attempt(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _AlwaysPassStep()
    result = step.execute(ctx)
    assert result.success
    assert result.status == "completed"
    record = ctx.state.steps[97]
    assert record.attempts == 1


def test_step_fails_then_succeeds_on_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _FailOnceStep()
    result = step.execute(ctx)
    assert result.success
    assert result.status == "warned"
    record = ctx.state.steps[99]
    assert record.attempts == 2
    assert "retry" in (record.notes or "")


def test_step_fails_twice_no_fix(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _AlwaysFailStep()
    result = step.execute(ctx)
    assert not result.success
    assert result.status == "failed"
    record = ctx.state.steps[98]
    assert record.attempts == 2


def test_exception_retry_recovers(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _ExceptionStep()
    result = step.execute(ctx)
    assert result.success
    assert result.status == "warned"
    record = ctx.state.steps[96]
    assert record.attempts == 2


# ---------------------------------------------------------------------------
# Debug flag tests
# ---------------------------------------------------------------------------


def test_debug_flag_sets_extras_before_attempt1(tmp_path: Path):
    ctx = _ctx(tmp_path, debug=True)
    assert ctx.options.debug is True


def test_failed_attempt1_sets_debug_live_for_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.extras.get("debug_live") is None
    step = _FailOnceStep()
    step.execute(ctx)
    assert ctx.extras.get("debug_live") is True


def test_debug_artifacts_snapshotted_on_failure(tmp_path: Path):
    ctx = _ctx(tmp_path)
    wd = ctx.workspace.step_workdir(98)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "transcript.jsonl").write_text('{"type":"test"}\n', encoding="utf-8")
    (wd / "stderr.log").write_text("error output\n", encoding="utf-8")

    _snapshot_debug_artifacts(98, ctx, 1)

    debug_dir = ctx.workspace.debug / "step-98-attempt1"
    assert debug_dir.exists()
    assert (debug_dir / "transcript.jsonl").exists()
    assert (debug_dir / "stderr.log").exists()


# ---------------------------------------------------------------------------
# Fix-proposal tests
# ---------------------------------------------------------------------------


def test_fix_proposal_invoked_on_double_failure(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=True)
    step = _AlwaysFailStep()

    with patch("worca_t.steps.base.run_agent") as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "mock analysis", "error": None,
        })()
        result = step.execute(ctx)

    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()


def test_no_fix_proposal_without_flag(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=False)
    step = _AlwaysFailStep()
    result = step.execute(ctx)
    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert not proposal.exists()


def test_fix_flow_failure_does_not_crash(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=True)
    step = _AlwaysFailStep()

    with patch("worca_t.steps.base.run_agent", side_effect=Exception("agent unavailable")):
        result = step.execute(ctx)

    assert not result.success
    assert result.status == "failed"
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()
    content = proposal.read_text(encoding="utf-8")
    assert "agent unavailable" in content or "failed" in content.lower()


# ---------------------------------------------------------------------------
# _run_fix_proposal unit tests
# ---------------------------------------------------------------------------


def test_run_fix_proposal_writes_files(tmp_path: Path):
    ctx = _ctx(tmp_path)

    with patch("worca_t.steps.base.run_agent") as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "analysis text", "error": None,
        })()
        path = _run_fix_proposal(42, ctx, "# Step 42 failure\n\nSomething broke")

    assert path is not None
    assert path.exists()
    rca = ctx.workspace.debug / "step-42-rca.md"
    assert rca.exists()
