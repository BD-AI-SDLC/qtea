"""Debug/fix flow tests (M9): retry, debug snapshots, fix proposals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import (
    Step,
    StepContext,
    StepResult,
    _run_fix_proposal,
    _snapshot_debug_artifacts,
)
from qtea.workspace import create_workspace

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

    async def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            return StepResult(success=False, status="failed", outputs=[], error="first attempt fail")
        return StepResult(success=True, status="completed", outputs=[], notes="second attempt ok")


class _AlwaysFailStep(Step):
    """Always fails."""
    number = 98
    name = "always-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=False, status="failed", outputs=[], error="always fails")


class _AlwaysPassStep(Step):
    """Always passes."""
    number = 97
    name = "always-pass"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=True, status="completed", outputs=[], notes="ok")


class _ExceptionStep(Step):
    """Raises on first attempt, passes on second."""
    number = 96
    name = "exception-once"
    timeout_s = 60

    def __init__(self):
        self._call_count = 0

    async def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("boom")
        return StepResult(success=True, status="completed", outputs=[], notes="recovered")


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


async def test_step_succeeds_first_attempt(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _AlwaysPassStep()
    result = await step.execute(ctx)
    assert result.success
    assert result.status == "completed"
    record = ctx.state.steps[97]
    assert record.attempts == 1


async def test_step_fails_then_succeeds_on_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _FailOnceStep()
    result = await step.execute(ctx)
    assert result.success
    assert result.status == "warned"
    record = ctx.state.steps[99]
    assert record.attempts == 2
    assert "retry" in (record.notes or "")


async def test_step_fails_twice_no_fix(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _AlwaysFailStep()
    result = await step.execute(ctx)
    assert not result.success
    assert result.status == "failed"
    record = ctx.state.steps[98]
    assert record.attempts == 2


async def test_exception_retry_recovers(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _ExceptionStep()
    result = await step.execute(ctx)
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


async def test_failed_attempt1_sets_debug_live_for_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.extras.get("debug_live") is None
    step = _FailOnceStep()
    await step.execute(ctx)
    assert ctx.extras.get("debug_live") is True


# ---------------------------------------------------------------------------
# Retry classification — content-failure CLEARS resume; transient KEEPS it
# ---------------------------------------------------------------------------


class _ContentFailStep(Step):
    """Always fails with a content-validation-style error (NOT a storm)."""
    number = 95
    name = "content-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            success=False, status="failed", outputs=[],
            error="5 violation(s): [hard-wait] tests/foo.py:42 wait_for_timeout(500)",
        )


class _StormFailStep(Step):
    """Always fails with the api_retry_storm sentinel error."""
    number = 94
    name = "storm-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            success=False, status="failed", outputs=[],
            error=(
                "SDK api_retry storm (8 consecutive retries with no "
                "intervening progress; threshold=8). The upstream "
                "Anthropic/Vertex API is returning transient errors..."
            ),
        )


async def test_content_failure_clears_resume_session_before_retry(tmp_path: Path):
    """Content / validation failures must clear `step{N}_resume_session`
    so attempt 2 starts FRESH instead of replaying the same flawed
    reasoning path.

    Regression guard for run 20260611-075728-0aa560 step 8 attempt 2:
    Haiku resumed the same session as attempt 1 and re-emitted the
    same 5 `wait_for_timeout` violations rather than re-deriving an
    assertion-based test from scratch.
    """
    ctx = _ctx(tmp_path, no_hitl=True)  # skip the storm-decision prompt
    step = _ContentFailStep()
    # Simulate a step.run() having stashed a session id during attempt 1.
    ctx.extras["step95_resume_session"] = "sess-attempt1-xyz"

    await step.execute(ctx)

    # Must be cleared so attempt 2's step.run() reads None → fresh session.
    assert "step95_resume_session" not in ctx.extras


async def test_transient_failure_preserves_resume_session(tmp_path: Path):
    """api_retry_storm failures must KEEP `step{N}_resume_session` so
    attempt 2 resumes and skips the work the relay-dropped turn lost.
    """
    ctx = _ctx(tmp_path, no_hitl=True, yes=True)  # skip storm prompt
    step = _StormFailStep()
    ctx.extras["step94_resume_session"] = "sess-attempt1-abc"

    await step.execute(ctx)

    # Must STILL be present so attempt 2's step.run() resumes the
    # session and reclaims the prior turn's Reads.
    assert ctx.extras.get("step94_resume_session") == "sess-attempt1-abc"


def test_debug_artifacts_snapshotted_on_failure(tmp_path: Path):
    ctx = _ctx(tmp_path)
    wd = ctx.workspace.step_workdir(98)
    wd.mkdir(parents=True, exist_ok=True)
    # Two agent calls in the same step -> two numbered transcripts/stderrs.
    (wd / "transcript-00.jsonl").write_text('{"type":"test","call":0}\n', encoding="utf-8")
    (wd / "transcript-01.jsonl").write_text('{"type":"test","call":1}\n', encoding="utf-8")
    (wd / "stderr-00.log").write_text("first call stderr\n", encoding="utf-8")
    (wd / "stderr-01.log").write_text("second call stderr\n", encoding="utf-8")
    (wd / "metrics-00.json").write_text('{"call":0}\n', encoding="utf-8")
    (wd / "metrics-01.json").write_text('{"call":1}\n', encoding="utf-8")

    _snapshot_debug_artifacts(98, ctx, 1)

    debug_dir = ctx.workspace.debug / "step-98-attempt1"
    assert debug_dir.exists()
    # All numbered audit files copied; previously only the latest was preserved.
    assert (debug_dir / "transcript-00.jsonl").exists()
    assert (debug_dir / "transcript-01.jsonl").exists()
    assert (debug_dir / "stderr-00.log").exists()
    assert (debug_dir / "stderr-01.log").exists()
    assert (debug_dir / "metrics-00.json").exists()
    assert (debug_dir / "metrics-01.json").exists()


def test_debug_snapshot_includes_legacy_unnumbered_files(tmp_path: Path):
    """Backward compat: a workdir from before this change still snapshots."""
    ctx = _ctx(tmp_path)
    wd = ctx.workspace.step_workdir(97)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "transcript.jsonl").write_text('{"old":"format"}\n', encoding="utf-8")
    (wd / "stderr.log").write_text("legacy\n", encoding="utf-8")

    _snapshot_debug_artifacts(97, ctx, 1)
    debug_dir = ctx.workspace.debug / "step-97-attempt1"
    assert (debug_dir / "transcript.jsonl").exists()
    assert (debug_dir / "stderr.log").exists()


# ---------------------------------------------------------------------------
# Fix-proposal tests
# ---------------------------------------------------------------------------


async def test_fix_proposal_invoked_on_double_failure(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=True)
    step = _AlwaysFailStep()

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "mock analysis", "error": None,
        })()
        result = await step.execute(ctx)

    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()


async def test_no_fix_proposal_without_flag(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=False)
    step = _AlwaysFailStep()
    result = await step.execute(ctx)
    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert not proposal.exists()


async def test_fix_flow_failure_does_not_crash(tmp_path: Path):
    ctx = _ctx(tmp_path, fix=True)
    step = _AlwaysFailStep()

    with patch(
        "qtea.steps.base.run_agent",
        new=AsyncMock(side_effect=Exception("agent unavailable")),
    ):
        result = await step.execute(ctx)

    assert not result.success
    assert result.status == "failed"
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()
    content = proposal.read_text(encoding="utf-8")
    assert "agent unavailable" in content or "failed" in content.lower()


# ---------------------------------------------------------------------------
# _run_fix_proposal unit tests
# ---------------------------------------------------------------------------


async def test_run_fix_proposal_writes_files(tmp_path: Path):
    ctx = _ctx(tmp_path)

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "analysis text", "error": None,
        })()
        path = await _run_fix_proposal(42, ctx, "# Step 42 failure\n\nSomething broke")

    assert path is not None
    assert path.exists()
    rca = ctx.workspace.debug / "step-42-rca.md"
    assert rca.exists()
