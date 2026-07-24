"""Regression test: an unhandled exception on a retry attempt must not leave
a STALE `sub_status` from a prior attempt on the persisted StepRecord.

Before this fix, `Step._attempt`'s `except Exception` branch set
`record.status = "failed"` but never touched `record.sub_status` — so if
attempt 1 completed with e.g. `sub_status="bugs_found"` (Step 9's
completed-with-bugs outcome) and attempt 2 then raised, the checkpoint
persisted `status="failed"` alongside a stale `sub_status="bugs_found"`
left over from attempt 1. Anything reading the checkpoint independently of
`status` (report builder, UI) could misread the run's true outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import Step, StepContext, StepResult
from qtea.workspace import create_workspace


def _make_ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=str(ws.sut),
    )
    opts = PipelineOptions(
        spec="x", sut=str(ws.sut), workspace_base=tmp_path / ".ws",
    )
    return StepContext(
        workspace=ws, state=state,
        spec_source="x", sut_source=str(ws.sut), options=opts,
    )


@dataclass
class _FlakyStep(Step):
    """Attempt 1 completes with `sub_status="bugs_found"` (still retryable,
    success=False); attempt 2 raises an unhandled exception."""

    number: int = 98
    name: str = "test-flaky-step"
    timeout_s: int | None = 60
    _attempts_seen: int = 0

    async def run(self, ctx: StepContext) -> StepResult:
        self._attempts_seen += 1
        if self._attempts_seen == 1:
            return StepResult(
                success=False, status="failed", sub_status="bugs_found",
                outputs=[], error="attempt 1: bugs found, retrying",
            )
        raise RuntimeError("attempt 2: unhandled explosion")


async def test_exception_on_retry_clears_stale_sub_status(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    step = _FlakyStep()

    result = await step.execute(ctx)

    assert result.success is False
    assert step._attempts_seen == 2
    record = ctx.state.steps[step.number]
    assert record.status == "failed"
    assert record.sub_status is None
