"""Pipeline orchestrator.

Runs the 11-step QA SDLC pipeline. Each step is a `Step` subclass registered in
`STEP_REGISTRY`. Steps not yet implemented are skipped gracefully so partial
milestones remain runnable end-to-end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from worca_t.checkpoints import RunState, StepRecord, is_step_complete, load_state, outputs_match, save_state
from worca_t.config import load_env
from worca_t.logging_setup import configure_logging, get_logger
from worca_t.metrics import format_cost, format_tokens
from worca_t.review_gate import review_step_7_tests
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
from worca_t.workspace import Workspace, create_workspace

TOTAL_STEPS = 11


@dataclass
class PipelineOptions:
    workspace_base: Path
    spec: str | None = None
    sut: str | None = None
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
    run_id: str | None = None
    env_file: Path | None = None
    no_hitl: bool = False
    module: str | None = None
    isolated_tests: bool = False
    yes: bool = False
    no_auto_deps: bool = False
    dev_locators: Path | None = None
    # Claude Code prompt-cache toggle. Default is OFF because on the Bosch
    # Vertex relay (aoai-farm.bosch-temp.com) cache_read never fires across
    # requests — measured net cost ~+$1.30/run on Step 7 Opus turns from the
    # 25% cache-creation surcharge with no read-side payback (probe data in
    # run 20260610-082950-6a887f RCA). Set --cache to opt in when running
    # against a relay or backend that does serve cross-request cache reads.
    cache: bool = False


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


def _select_workspace(opts: PipelineOptions, console: Console | None = None) -> Workspace:
    """Pick the workspace to operate against.

    Resume is opt-in via ``--run-id``. Without it, every invocation gets a
    fresh workspace — even if a prior in-progress run exists.

    Precedence:
      1. ``--run-id`` set -> resume that workspace (must already exist).
      2. Otherwise        -> create a fresh workspace. ``--from-step`` without
                              ``--run-id`` is an error, since there is nothing
                              to resume into.
    """
    if opts.run_id is not None:
        candidate = opts.workspace_base / opts.run_id
        if not candidate.exists():
            raise FileNotFoundError(
                f"run-id '{opts.run_id}' not found under {opts.workspace_base}"
            )
        return Workspace(root=candidate.resolve(), run_id=opts.run_id)

    if opts.from_step is not None:
        raise RuntimeError(
            f"--from-step {opts.from_step} requires --run-id to identify the "
            "workspace to resume into. Pass --run-id <id> (see `worca-t list`)."
        )

    return create_workspace(opts.workspace_base, run_id=None)


def _validate_resume_prerequisites(
    state: RunState, from_step: int, console: Console
) -> None:
    """Ensure all steps before ``from_step`` have completed-or-skipped status.

    Raises ``RuntimeError`` with an actionable message if any are missing,
    so the user isn't stuck debugging an empty workdir on a downstream step.
    """
    missing: list[int] = []
    for prior in range(1, from_step):
        rec = state.steps.get(prior)
        if not rec or rec.status not in ("completed", "skipped"):
            missing.append(prior)
    if missing:
        listing = ", ".join(str(n) for n in missing)
        raise RuntimeError(
            f"cannot run --from-step {from_step}: prior step(s) [{listing}] "
            "did not complete in this workspace. Re-run from an earlier step, "
            "or use --run-id to target a different workspace."
        )


def _reset_steps_from(state: RunState, from_step: int) -> None:
    """Drop checkpoint records for steps >= ``from_step`` so they re-execute.

    Without this, ``is_step_complete`` would short-circuit the very step the
    user asked to re-run if it had previously reached 'completed' / 'failed'.
    """
    for k in list(state.steps.keys()):
        if k >= from_step:
            del state.steps[k]
    # Re-open the run so pipeline.end can stamp a new finished_at.
    state.finished_at = None


def _select_steps(opts: PipelineOptions) -> list[int]:
    if opts.only_step is not None:
        return [opts.only_step]
    start = opts.from_step or 1
    return [i for i in range(start, TOTAL_STEPS + 1) if i not in opts.skip_steps]


async def run_pipeline(opts: PipelineOptions, *, console: Console | None = None) -> int:
    console = console or Console()

    # Translate the --cache toggle into the Claude Code subprocess env knob.
    # Setting DISABLE_PROMPT_CACHING=1 in the parent suppresses the CLI's
    # automatic cache_control breakpoints on the system prompt + tools +
    # CLAUDE.md for every agent invocation in this run. claude_runner
    # forwards this var into the subprocess env explicitly (see its
    # forwarded_env block). Default off because cross-request cache_read
    # never fires on the Bosch Vertex relay — paying the 25% cache-
    # creation surcharge for zero read-side payback is net cost-negative
    # there. Pass --cache to opt back in when the backend supports it.
    if not opts.cache:
        os.environ["DISABLE_PROMPT_CACHING"] = "1"
    else:
        # Re-enable explicitly: if a parent process or prior session set
        # the disable flag, --cache should clear it so the CLI's auto-
        # caching is restored for this run.
        os.environ.pop("DISABLE_PROMPT_CACHING", None)

    try:
        ws = _select_workspace(opts, console=console)
    except (FileNotFoundError, RuntimeError) as e:
        (console or Console()).print(f"[red]workspace error:[/] {e}")
        return 2

    # On resume (--run-id), recover --spec / --sut from state.json when the
    # user didn't repeat them on the command line. The workspace already
    # knows what was run; asking again is noise.
    prior_state = load_state(ws.state_file)
    if opts.run_id is not None and prior_state is not None:
        if opts.spec is None:
            opts.spec = prior_state.spec_source
        if opts.sut is None:
            opts.sut = prior_state.sut_source

    missing = [n for n, v in (("--spec", opts.spec), ("--sut", opts.sut)) if not v]
    if missing:
        hint = (
            " (resume found no prior value in state.json)"
            if opts.run_id is not None
            else ""
        )
        (console or Console()).print(
            f"[red]missing required option(s):[/] {', '.join(missing)}{hint}"
        )
        return 2

    if opts.env_file:
        load_env(opts.env_file)
        try:
            from dotenv import dotenv_values
            loaded = {k: v for k, v in dotenv_values(opts.env_file).items() if v}
            (console or Console()).print(
                f"[dim]env-file:[/] loaded [bold]{len(loaded)}[/] key(s) from "
                f"[cyan]{opts.env_file}[/cyan]"
            )
        except Exception:
            pass
    else:
        sut_path = Path(opts.sut).expanduser().resolve()
        if sut_path.is_dir():
            sut_dotenv = sut_path / ".env"
            if sut_dotenv.is_file():
                load_env(sut_dotenv)

    log = configure_logging(level=opts.log_level, jsonl_path=ws.run_log, run_id=ws.run_id)

    state = prior_state or RunState(
        run_id=ws.run_id,
        workspace=str(ws.root),
        spec_source=opts.spec,
        sut_source=opts.sut,
    )
    # Refresh source pointers if user changed them.
    state.spec_source = opts.spec
    state.sut_source = opts.sut

    # If user asked to re-enter mid-pipeline, validate prereqs and clear
    # downstream checkpoints so the requested step actually re-executes.
    if opts.from_step is not None:
        try:
            _validate_resume_prerequisites(state, opts.from_step, console)
        except RuntimeError as e:
            console.print(f"[red]resume error:[/] {e}")
            return 2
        _reset_steps_from(state, opts.from_step)
        save_state(state, ws.state_file)

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

    # Early SUT materialization — fail fast before spending time on steps 1-5.
    # No clone-confirmation prompt: worca-t's entire purpose is to fetch the
    # SUT, install its dependencies, and run its tests. The user supplied the
    # URL on the command line — that IS the consent. Asking again is noise.
    import subprocess
    import sys

    from worca_t.steps.s06_research import _materialize_sut

    console.print("[dim]sut:[/] materializing…")
    try:
        _materialize_sut(opts.sut, ws.sut, run_id=ws.run_id)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace").strip()
        console.print(f"[red]sut error:[/] failed to clone [cyan]{opts.sut}[/cyan]")
        if stderr:
            console.print(f"[red]  {stderr}[/]")
        log.error("pipeline.sut_failed", sut=opts.sut, error=stderr)
        return 2
    except FileNotFoundError as e:
        console.print(f"[red]sut error:[/] {e}")
        log.error("pipeline.sut_failed", sut=opts.sut, error=str(e))
        return 2
    except Exception as e:
        console.print(f"[red]sut error:[/] {e}")
        log.error("pipeline.sut_failed", sut=opts.sut, error=str(e))
        return 2

    # Post-materialize preflight: catch the silent-failure shape where the
    # clone "succeeded" but didn't put a usable repo on disk. Both checks fire
    # in well under a second; without them, a missing/branchless SUT manifests
    # downstream as a 1800s step-7 timeout (see run 20260603-205851-2d359f).
    from worca_t._sut_git import branch_name as _branch_name
    from worca_t._sut_git import current_branch as _current_branch

    if not (ws.sut / ".git").exists():
        msg = (
            f"sut not a git repo at {ws.sut} — materialization left no .git/ "
            f"directory. Re-materialize via `worca-t run` without --run-id, "
            f"or report this bug."
        )
        console.print(f"[red]sut preflight:[/] {msg}")
        log.error("pipeline.sut_preflight_failed", sut=str(ws.sut), reason="no_git")
        return 2

    expected_branch = _branch_name(ws.run_id)
    actual_branch = _current_branch(ws.sut)
    if actual_branch != expected_branch:
        msg = (
            f"sut on wrong branch at {ws.sut}: expected `{expected_branch}`, "
            f"got `{actual_branch}`. The worca-t isolation branch was not "
            f"created — re-materialize via `worca-t run` without --run-id."
        )
        console.print(f"[red]sut preflight:[/] {msg}")
        log.error(
            "pipeline.sut_preflight_failed",
            sut=str(ws.sut),
            reason="wrong_branch",
            expected=expected_branch,
            actual=actual_branch,
        )
        return 2

    console.print(
        f"[dim]sut:[/] ready at [cyan]{ws.sut}[/cyan] "
        f"(branch [cyan]{expected_branch}[/cyan])"
    )

    # MCP preflight — cold-start every server in .mcp.json once up front.
    # Doing this here (instead of letting each step's `claude` subprocess
    # bootstrap its own MCPs in parallel) avoids the npx/node contention
    # that previously starved Step 2 into a 300s timeout, and warms the
    # npx cache for all downstream agent calls.
    from worca_t.mcp_manager import load_mcp_config, probe_server

    while True:
        console.print("[dim]mcp:[/] verifying servers…")
        try:
            servers = load_mcp_config()
        except (FileNotFoundError, OSError, ValueError) as e:
            console.print(f"[red]mcp preflight:[/] could not load .mcp.json: {e}")
            log.error("pipeline.mcp_preflight_failed", error=str(e))
            return 2

        results = [(name, *probe_server(server)) for name, server in servers.items()]
        failed = [(n, msg) for n, ok, msg in results if not ok]

        if not failed:
            ok_names = [n for n, ok, _ in results if ok]
            console.print(
                "[dim]mcp:[/] " + ", ".join(f"{n} ok" for n in ok_names)
            )
            log.info("pipeline.mcp_preflight_ok", servers=ok_names)
            break

        console.print("[red]mcp preflight:[/] one or more servers failed to start:")
        for name, msg in failed:
            console.print(f"  [red]{name}[/]: {msg}")
        log.error(
            "pipeline.mcp_preflight_failed",
            failed=[{"name": n, "error": m} for n, m in failed],
        )

        if not sys.stdin.isatty() or opts.no_hitl or opts.yes:
            console.print(
                "[yellow]Non-interactive mode: fix MCP setup and re-run "
                "(or omit --no-hitl / --yes to enable the retry prompt).[/yellow]"
            )
            return 2

        from rich.prompt import Confirm

        if not Confirm.ask(
            "Retry MCP initialization?", default=True, console=console
        ):
            console.print("[dim]Aborted by user.[/]")
            return 2

    # Replay env resolution from existing Step 6 artifacts (if any).
    # Step 6's `resolve_sut_env()` writes into `os.environ` in-process only;
    # those writes are gone on a fresh `worca-t run` invocation. Without this
    # replay, re-running `--from-step 7+` leaves SUT_BASE_URL unset and the
    # locator-resolution agent (Step 8) aborts with BASE_URL_UNRESOLVED.
    try:
        from worca_t.steps.s06_research import replay_env_from_artifacts
        replay_env_from_artifacts(ws, opts)
    except Exception as e:  # noqa: BLE001 — defensive; never block the pipeline
        log.warning("pipeline.env_replay_failed", error=str(e))

    ctx = StepContext(
        workspace=ws,
        state=state,
        spec_source=opts.spec,
        sut_source=opts.sut,
        options=opts,
    )

    if opts.debug:
        ctx.extras["debug_live"] = True

    selected_steps = _select_steps(opts)

    exit_code = 0
    for step_num in selected_steps:
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
        result = await step.execute(ctx)
        record = state.steps.get(step_num)

        if not result.success:
            save_state(state, ws.state_file)
            console.print(f"[red]step {step_num:02d} FAILED:[/] {result.error or result.notes}")
            if record is not None:
                console.print(f"   {_format_step_metrics_line(record)}")
            exit_code = 1
            break

        # Lightweight human review of the generated TDD before step 8 patches
        # locators in place. Skipped automatically in non-TTY / `--no-hitl`
        # contexts. On manual edits the gate re-indexes the SUT in-place so
        # downstream steps see fresh line numbers and refreshed hashes.
        if step_num == 7 and not review_step_7_tests(ctx, result, console):
            save_state(state, ws.state_file)
            console.print("[yellow]step 07 rejected by reviewer — aborting[/]")
            exit_code = 1
            break

        save_state(state, ws.state_file)

        marker = "warned" if result.status == "warned" else "ok"
        line = f"[green]step {step_num:02d} {marker}[/]  -> {len(result.outputs)} outputs"
        if record is not None:
            line += f"  [dim]{_format_step_metrics_line(record)}[/]"
        console.print(line)

    state.finished_at = datetime.now(UTC).isoformat()
    save_state(state, ws.state_file)
    _render_summary_table(state, console)
    log.info(
        "pipeline.end",
        status="ok" if exit_code == 0 else "failed",
        **_pipeline_totals(state),
    )
    return exit_code


def _format_step_metrics_line(record: StepRecord) -> str:
    """One-line summary appended to per-step console output."""
    duration = f"{record.duration_s:.1f}s" if record.duration_s is not None else "-"
    tokens_in = format_tokens(record.tokens_input)
    tokens_out = format_tokens(record.tokens_output)
    cost = format_cost(record.cost_usd)
    return f"[elapsed {duration} | {tokens_in}->{tokens_out} tok | {cost}]"


def _pipeline_totals(state: RunState) -> dict[str, float | int]:
    total_duration = 0.0
    total_in = 0
    total_out = 0
    total_cost = 0.0
    total_calls = 0
    for rec in state.steps.values():
        if rec.duration_s is not None:
            total_duration += rec.duration_s
        total_in += rec.tokens_input
        total_out += rec.tokens_output
        total_cost += rec.cost_usd
        total_calls += rec.agent_calls
    return {
        "total_duration_s": round(total_duration, 3),
        "total_tokens_input": total_in,
        "total_tokens_output": total_out,
        "total_cost_usd": round(total_cost, 6),
        "total_agent_calls": total_calls,
    }


def _render_summary_table(state: RunState, console: Console) -> None:
    """Print a per-step + totals table after the pipeline finishes."""
    if not state.steps:
        return

    table = Table(title="Pipeline Summary", title_style="bold cyan", show_lines=False)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Step", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("In tok", justify="right", no_wrap=True)
    table.add_column("Out tok", justify="right", no_wrap=True)
    table.add_column("Calls", justify="right", no_wrap=True)
    table.add_column("Cost", justify="right", no_wrap=True)

    status_color = {
        "completed": "green",
        "skipped": "dim",
        "warned": "yellow",
        "failed": "red",
        "in_progress": "yellow",
        "pending": "dim",
    }

    for step_num in sorted(state.steps):
        rec = state.steps[step_num]
        color = status_color.get(rec.status, "white")
        duration = f"{rec.duration_s:.1f}s" if rec.duration_s is not None else "-"
        table.add_row(
            f"{step_num:02d}",
            rec.name or "",
            f"[{color}]{rec.status}[/]",
            duration,
            format_tokens(rec.tokens_input),
            format_tokens(rec.tokens_output),
            str(rec.agent_calls),
            format_cost(rec.cost_usd),
        )

    totals = _pipeline_totals(state)
    table.add_section()
    table.add_row(
        "",
        "[bold]TOTAL[/]",
        "",
        f"[bold]{totals['total_duration_s']:.1f}s[/]",
        f"[bold]{format_tokens(int(totals['total_tokens_input']))}[/]",
        f"[bold]{format_tokens(int(totals['total_tokens_output']))}[/]",
        f"[bold]{totals['total_agent_calls']}[/]",
        f"[bold]{format_cost(float(totals['total_cost_usd']))}[/]",
    )
    console.print(table)
