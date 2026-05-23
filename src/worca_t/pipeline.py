"""Pipeline orchestrator.

Runs the 11-step QA SDLC pipeline. Each step is a `Step` subclass registered in
`STEP_REGISTRY`. Steps not yet implemented are skipped gracefully so partial
milestones remain runnable end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from worca_t.checkpoints import RunState, is_step_complete, load_state, outputs_match, save_state
from worca_t.logging_setup import configure_logging
from worca_t.steps.base import Step, StepContext
from worca_t.steps.s01_intake import IntakeStep
from worca_t.steps.s02_refine import RefineStep
from worca_t.steps.s03_plan import PlanStep
from worca_t.steps.s04_strategy import StrategyStep
from worca_t.steps.s05_xray import XrayUploadStep
from worca_t.steps.s06_research import ResearchStep
from worca_t.steps.s07_codegen import CodegenStep
from worca_t.steps.s08_locator_resolution import LocatorResolutionStep
from worca_t.steps.s09_execute import ExecuteStep
from worca_t.steps.s10_bug_classifier import BugClassifierStep
from worca_t.steps.s11_report import ReportStep
from worca_t.workspace import Workspace, create_workspace, find_latest_workspace

TOTAL_STEPS = 11


@dataclass
class PipelineOptions:
    spec: str
    sut: str
    workspace_base: Path
    from_step: int | None = None
    only_step: int | None = None
    force: bool = False
    parallelism: int = 1
    headless: bool = True
    debug: bool = False
    fix: bool = False
    strict_xray: bool = False
    skip_steps: set[int] = field(default_factory=set)
    report: str = "auto"
    report_inline_images: bool = False
    open_report: bool = False
    log_level: str = "info"
    resume: bool = True
    run_id: str | None = None


def _build_registry() -> dict[int, Step]:
    """Map step number -> Step instance. Unregistered steps are skipped."""
    steps: list[Step] = [
        IntakeStep(),
        RefineStep(),
        PlanStep(),
        StrategyStep(),
        XrayUploadStep(),
        ResearchStep(),
        CodegenStep(),
        LocatorResolutionStep(),
        ExecuteStep(),
        BugClassifierStep(),
        ReportStep(),
    ]
    return {s.number: s for s in steps}


STEP_REGISTRY: dict[int, Step] = _build_registry()


def _select_workspace(opts: PipelineOptions) -> Workspace:
    if opts.resume and opts.run_id is None:
        latest = find_latest_workspace(opts.workspace_base)
        if latest is not None:
            state = load_state(latest.state_file)
            if state and state.finished_at is None:
                return latest
    return create_workspace(opts.workspace_base, run_id=opts.run_id)


def _select_steps(opts: PipelineOptions) -> list[int]:
    if opts.only_step is not None:
        return [opts.only_step]
    start = opts.from_step or 1
    return [i for i in range(start, TOTAL_STEPS + 1) if i not in opts.skip_steps]


def run_pipeline(opts: PipelineOptions, *, console: Console | None = None) -> int:
    console = console or Console()
    ws = _select_workspace(opts)
    log = configure_logging(level=opts.log_level, jsonl_path=ws.run_log, run_id=ws.run_id)

    state = load_state(ws.state_file) or RunState(
        run_id=ws.run_id,
        workspace=str(ws.root),
        spec_source=opts.spec,
        sut_source=opts.sut,
    )
    # Refresh source pointers if user changed them.
    state.spec_source = opts.spec
    state.sut_source = opts.sut

    log.info(
        "pipeline.start",
        spec=opts.spec,
        sut=opts.sut,
        workspace=str(ws.root),
        from_step=opts.from_step,
        only_step=opts.only_step,
        force=opts.force,
        debug=opts.debug,
        fix=opts.fix,
        report=opts.report,
    )
    console.print(f"[green]workspace[/] {ws.root}")
    console.print(f"[green]run_id[/]    {ws.run_id}")

    ctx = StepContext(
        workspace=ws,
        state=state,
        spec_source=opts.spec,
        sut_source=opts.sut,
        options=opts,
    )

    if opts.debug:
        ctx.extras["debug_live"] = True

    exit_code = 0
    for step_num in _select_steps(opts):
        if not opts.force and is_step_complete(state, step_num):
            if not outputs_match(state, step_num, ws.step_dir(step_num)):
                log.info("step.invalidated", step=step_num)
                console.print(f"[yellow]step {step_num:02d} outputs changed - re-running[/]")
            else:
                log.info("step.skip_complete", step=step_num)
                console.print(f"[dim]step {step_num:02d} already complete - skipping[/]")
                continue

        step = STEP_REGISTRY.get(step_num)
        if step is None:
            log.info("step.skip_unimplemented", step=step_num)
            console.print(f"[yellow]step {step_num:02d} not yet implemented - skipping[/]")
            continue

        console.print(f"[cyan]>>> step {step_num:02d} {step.name}[/]")
        result = step.execute(ctx)
        save_state(state, ws.state_file)

        if not result.success:
            console.print(f"[red]step {step_num:02d} FAILED:[/] {result.error or result.notes}")
            exit_code = 1
            break

        marker = "warned" if result.status == "warned" else "ok"
        console.print(f"[green]step {step_num:02d} {marker}[/]  -> {len(result.outputs)} outputs")

    state.finished_at = datetime.now(UTC).isoformat()
    save_state(state, ws.state_file)
    log.info("pipeline.end", status="ok" if exit_code == 0 else "failed")
    return exit_code
