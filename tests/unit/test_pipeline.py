"""Pipeline orchestrator tests."""

from __future__ import annotations

from pathlib import Path

from worca_t.checkpoints import RunState, save_state
from worca_t.pipeline import PipelineOptions, _select_workspace, run_pipeline
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.workspace import create_workspace


async def test_run_pipeline_completes_with_no_steps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "worca_t.pipeline.STEP_REGISTRY", {},
    )
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
    )
    rc = await run_pipeline(opts)
    assert rc == 0


async def test_run_pipeline_runs_only_step(tmp_path: Path, monkeypatch):
    call_log = []

    class _TrackStep(Step):
        number = 1
        name = "track"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            call_log.append(self.number)
            return StepResult(success=True, status="completed", outputs=[])

    monkeypatch.setattr("worca_t.pipeline.STEP_REGISTRY", {1: _TrackStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1,
    )
    rc = await run_pipeline(opts)
    assert rc == 0
    assert call_log == [1]


async def test_run_pipeline_stops_on_failure(tmp_path: Path, monkeypatch):
    class _FailStep(Step):
        number = 1
        name = "fail"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            return StepResult(success=False, status="failed", outputs=[], error="boom")

    monkeypatch.setattr("worca_t.pipeline.STEP_REGISTRY", {1: _FailStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1,
    )
    rc = await run_pipeline(opts)
    assert rc == 1


def test_select_workspace_default_is_fresh(tmp_path: Path):
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path)
    ws = _select_workspace(opts)
    assert ws.root.exists()


def test_select_workspace_default_ignores_unfinished_prior(tmp_path: Path):
    ws1 = create_workspace(tmp_path)
    state = RunState(run_id=ws1.run_id, workspace=str(ws1.root), spec_source="x", sut_source=".")
    save_state(state, ws1.state_file)

    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path)
    ws2 = _select_workspace(opts)
    assert ws2.run_id != ws1.run_id


def test_select_workspace_resumes_with_run_id(tmp_path: Path):
    ws1 = create_workspace(tmp_path)
    state = RunState(run_id=ws1.run_id, workspace=str(ws1.root), spec_source="x", sut_source=".")
    save_state(state, ws1.state_file)

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, run_id=ws1.run_id,
    )
    ws2 = _select_workspace(opts)
    assert ws2.run_id == ws1.run_id


def test_select_workspace_run_id_missing_raises(tmp_path: Path):
    import pytest
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, run_id="does-not-exist",
    )
    with pytest.raises(FileNotFoundError):
        _select_workspace(opts)


def test_select_workspace_from_step_without_run_id_raises(tmp_path: Path):
    import pytest
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, from_step=3,
    )
    with pytest.raises(RuntimeError, match="requires --run-id"):
        _select_workspace(opts)


async def test_run_pipeline_debug_sets_extras(tmp_path: Path, monkeypatch):
    captured_ctx = {}

    class _CaptureStep(Step):
        number = 1
        name = "capture"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            captured_ctx["debug_live"] = ctx.extras.get("debug_live")
            return StepResult(success=True, status="completed", outputs=[])

    monkeypatch.setattr("worca_t.pipeline.STEP_REGISTRY", {1: _CaptureStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1, debug=True,
    )
    await run_pipeline(opts)
    assert captured_ctx["debug_live"] is True
