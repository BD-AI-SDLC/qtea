"""Checkpoint and resume tests (M10): outputs_match, --from-step, --only-step, --force."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import (
    RunState,
    StepRecord,
    hash_paths,
    is_step_complete,
    load_state,
    outputs_match,
    save_state,
)
from worca_t.pipeline import PipelineOptions, _select_steps
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.workspace import create_workspace

# ---------------------------------------------------------------------------
# outputs_match unit tests
# ---------------------------------------------------------------------------


def test_outputs_match_returns_true_when_hashes_match(tmp_path: Path):
    step_dir = tmp_path / "step01"
    step_dir.mkdir()
    f = step_dir / "out.json"
    f.write_text('{"x":1}', encoding="utf-8")

    hashes = hash_paths([f])
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    state.steps[1] = StepRecord(step=1, name="t", status="completed", output_hashes=hashes)

    assert outputs_match(state, 1, step_dir) is True


def test_outputs_match_returns_false_when_file_modified(tmp_path: Path):
    step_dir = tmp_path / "step01"
    step_dir.mkdir()
    f = step_dir / "out.json"
    f.write_text('{"x":1}', encoding="utf-8")

    hashes = hash_paths([f])
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    state.steps[1] = StepRecord(step=1, name="t", status="completed", output_hashes=hashes)

    f.write_text('{"x":2}', encoding="utf-8")
    assert outputs_match(state, 1, step_dir) is False


def test_outputs_match_returns_false_when_file_deleted(tmp_path: Path):
    step_dir = tmp_path / "step01"
    step_dir.mkdir()
    f = step_dir / "out.json"
    f.write_text('{"x":1}', encoding="utf-8")

    hashes = hash_paths([f])
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    state.steps[1] = StepRecord(step=1, name="t", status="completed", output_hashes=hashes)

    f.unlink()
    assert outputs_match(state, 1, step_dir) is False


def test_outputs_match_returns_true_when_no_hashes_recorded(tmp_path: Path):
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    state.steps[1] = StepRecord(step=1, name="t", status="completed")
    assert outputs_match(state, 1, tmp_path) is True


def test_outputs_match_returns_true_for_missing_step(tmp_path: Path):
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    assert outputs_match(state, 99, tmp_path) is True


# ---------------------------------------------------------------------------
# _select_steps tests
# ---------------------------------------------------------------------------


def test_select_steps_from_step():
    opts = PipelineOptions(spec="x", sut=".", workspace_base=Path(), from_step=3)
    steps = _select_steps(opts)
    assert steps == [3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_select_steps_only_step():
    opts = PipelineOptions(spec="x", sut=".", workspace_base=Path(), only_step=7)
    steps = _select_steps(opts)
    assert steps == [7]


def test_select_steps_skip_steps():
    opts = PipelineOptions(spec="x", sut=".", workspace_base=Path(), skip_steps={5, 8})
    steps = _select_steps(opts)
    assert 5 not in steps
    assert 8 not in steps
    assert 1 in steps


# ---------------------------------------------------------------------------
# Corrupted state handling
# ---------------------------------------------------------------------------


def test_load_state_returns_none_on_corrupted_json(tmp_path: Path):
    bad = tmp_path / "state.json"
    bad.write_text("not valid json{{{", encoding="utf-8")
    assert load_state(bad) is None


def test_load_state_returns_none_on_missing_keys(tmp_path: Path):
    bad = tmp_path / "state.json"
    bad.write_text('{"oops": true}', encoding="utf-8")
    assert load_state(bad) is None


def test_save_state_atomic_write(tmp_path: Path):
    state = RunState(run_id="r", workspace=str(tmp_path), spec_source="x", sut_source=".")
    path = tmp_path / "state.json"
    save_state(state, path)
    assert path.exists()
    assert not (tmp_path / "state.tmp").exists()


# ---------------------------------------------------------------------------
# Pipeline resume integration tests
# ---------------------------------------------------------------------------


class _CountingStep(Step):
    """Tracks how many times run() is called across all instances sharing the counter."""

    number = 1
    name = "counting"
    timeout_s = 60

    def __init__(self, num: int, counter: dict):
        self.number = num
        self._counter = counter

    def run(self, ctx: StepContext) -> StepResult:
        self._counter[self.number] = self._counter.get(self.number, 0) + 1
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"step{self.number}.json"
        out_file.write_text(json.dumps({"step": self.number}), encoding="utf-8")
        return StepResult(success=True, status="completed", outputs=[out_file])


def _make_pipeline_ctx(tmp_path: Path, **opts_kw):
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    defaults = {"spec": "x", "sut": ".", "workspace_base": tmp_path / ".ws"}
    defaults.update(opts_kw)
    opts = PipelineOptions(**defaults)
    return ws, state, opts, StepContext(
        workspace=ws, state=state, spec_source="x", sut_source=".", options=opts,
    )


def test_completed_step_is_skipped_on_resume(tmp_path: Path):
    _ws, state, _opts, ctx = _make_pipeline_ctx(tmp_path)
    counter = {}
    step = _CountingStep(1, counter)

    step.execute(ctx)
    assert counter[1] == 1
    assert is_step_complete(state, 1)

    step.execute(ctx)
    assert counter[1] == 2


def test_force_reruns_completed_step(tmp_path: Path):
    _ws, _state, _opts, ctx = _make_pipeline_ctx(tmp_path, force=True)
    counter = {}
    step = _CountingStep(1, counter)

    step.execute(ctx)
    assert counter[1] == 1


def test_hash_invalidation_triggers_rerun(tmp_path: Path):
    ws, state, _opts, ctx = _make_pipeline_ctx(tmp_path)
    counter = {}
    step = _CountingStep(1, counter)

    step.execute(ctx)
    assert is_step_complete(state, 1)
    assert outputs_match(state, 1, ws.step_dir(1))

    out_file = ws.step_dir(1) / "step1.json"
    out_file.write_text('{"modified": true}', encoding="utf-8")
    assert not outputs_match(state, 1, ws.step_dir(1))


def test_state_roundtrip_preserves_hashes(tmp_path: Path):
    ws, state, _opts, ctx = _make_pipeline_ctx(tmp_path)
    counter = {}
    step = _CountingStep(1, counter)

    step.execute(ctx)
    save_state(state, ws.state_file)

    loaded = load_state(ws.state_file)
    assert loaded is not None
    assert loaded.steps[1].output_hashes == state.steps[1].output_hashes
    assert outputs_match(loaded, 1, ws.step_dir(1))
