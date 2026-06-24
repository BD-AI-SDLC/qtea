"""Pipeline orchestrator.

Runs the 11-step QA SDLC pipeline. Each step is a `Step` subclass registered in
`STEP_REGISTRY`. Steps not yet implemented are skipped gracefully so partial
milestones remain runnable end-to-end.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from qtea.checkpoints import (
    RunState,
    StepRecord,
    is_step_complete,
    load_state,
    outputs_match,
    save_state,
)
from qtea.config import load_env
from qtea.logging_setup import configure_logging, get_logger

# Module-level logger for helpers that run outside the `run_pipeline` scope
# where the run-id-bound `log` local is constructed. Helpers inside
# `run_pipeline` may continue to use the local for run-id-bound output.
_log = get_logger(__name__)
from qtea.metrics import format_cost, format_tokens
from qtea.review_gate import review_step_4_strategy, review_step_7_plan, review_step_8_intents
from qtea.steps.base import Step, StepContext
from qtea.steps.s01_intake import IntakeStep
from qtea.steps.s02_refine import RefineStep
from qtea.steps.s03_plan import PlanStep
from qtea.steps.s04_strategy import StrategyStep
from qtea.steps.s05_xray import XrayUploadStep
from qtea.steps.s06_research import ResearchStep
from qtea.steps.s07_test_architect import TestArchitectStep
from qtea.steps.s08_codegen import CodegenStep
from qtea.steps.s09_execute import ExecuteStep
from qtea.steps.s10_bug_classifier import BugClassifierStep
from qtea.steps.s11_report import ReportStep
from qtea.workspace import Workspace, create_workspace

TOTAL_STEPS = 11


@dataclass
class PipelineOptions:
    workspace_base: Path
    spec: str | None = None
    sut: str | None = None
    from_step: int | None = None
    only_step: int | None = None
    force: bool = False
    parallelism: int = 2
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
    # Playwright `storageState.json` for Step 9 heal-agent reuse. CLI
    # override; lower-priority sources (env var, SUT convention path,
    # workspace auto-capture) are resolved inside Step 9 via
    # `qtea.storage_state.resolve()`.
    storage_state: Path | None = None
    # Claude Code prompt-cache toggle.  None = auto-detect (enabled when
    # BMF sticky-session routing is active, disabled otherwise); True =
    # force on; False = force off.
    cache: bool | None = None
    # Disable automatic cleanup of step artifacts and debug directories when
    # using --from-step. Default is ON (--from-step cleans step-NN/,
    # artifacts/stepNN/, and debug/step-NN-attempt* directories from the
    # target step onward).
    no_cleanup: bool = False
    # Desktop UI mode — stdin is not a TTY but HITL should still be active.
    ui_mode: bool = False


def _build_registry() -> dict[int, Step]:
    """Map step number -> Step instance. Unregistered steps are skipped."""
    steps: list[Step] = [
        IntakeStep(),
        RefineStep(),
        PlanStep(),
        StrategyStep(),
        XrayUploadStep(),
        ResearchStep(),
        TestArchitectStep(),
        CodegenStep(),
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
            "workspace to resume into. Pass --run-id <id> (see `qtea list`)."
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
        if not rec or rec.status not in ("completed", "skipped", "warned"):
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


def _kill_allure_for_workspace(ws: Workspace) -> None:
    """Kill any allure server process whose command line references this workspace.

    ``allure open`` spawns a detached Java/Jetty server that holds
    ``allure-open.log`` open via stderr. On Windows the open file handle
    blocks ``shutil.rmtree`` — we must terminate the process first.
    The process may be named ``allure``, ``java``, or ``javaw`` depending
    on the platform and allure distribution.
    """
    import psutil

    ws_str = str(ws.root)
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            cmd_joined = " ".join(cmdline)
            if "allure" not in cmd_joined:
                continue
            if ws_str in cmd_joined:
                proc.terminate()
                proc.wait(timeout=5)
                _log.info("cleanup.allure_killed", pid=proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass


def _cleanup_step_artifacts(ws: Workspace, from_step: int, console: Console | None = None) -> None:
    """Delete step-specific directories from from_step onward to ensure clean state.

    Removes both work directories (step-NN/) and artifact directories 
    (artifacts/stepNN/) for the specified step and all subsequent steps,
    plus debug files and directories for each step. This prevents artifact pollution
    when re-running with --from-step and ensures accurate log analysis.

    Args:
        ws: Workspace containing the directories to clean
        from_step: First step number to clean (inclusive), all later steps also cleaned
        console: Optional console for user prompts (hitl confirmation)
    """
    if console is None:
        console = Console()

    # Collect items to clean first for confirmation
    items_to_clean = []
    
    for step in range(from_step, TOTAL_STEPS + 1):
        work_dir = ws.root / f"step-{step:02d}"
        artifact_dir = ws.artifacts / f"step{step:02d}"

        if work_dir.exists():
            items_to_clean.append(("dir", work_dir))
        if artifact_dir.exists():
            items_to_clean.append(("dir", artifact_dir))

        # Collect debug items for this step (both files and directories)
        debug_dir = ws.debug
        if debug_dir.exists():
            for debug_entry in debug_dir.glob(f"step-{step:02d}-attempt*"):
                if debug_entry.is_file():
                    items_to_clean.append(("file", debug_entry))
                elif debug_entry.is_dir():
                    items_to_clean.append(("dir", debug_entry))

    # When step 9 (execute) will rerun, also clear JIT-cache and SUT test
    # outputs that accumulate across runs and would otherwise mix stale data
    # with the fresh run's results.
    if from_step <= 9:
        jit_cache_dir = ws.root / "locator-cache"
        cache_json = jit_cache_dir / "locator-cache.json"
        if cache_json.exists():
            items_to_clean.append(("file", cache_json))
        if jit_cache_dir.exists():
            for pending in jit_cache_dir.glob("hitl-pending-*.json"):
                items_to_clean.append(("file", pending))
        for sut_subdir in ("screenshots", "reports"):
            p = ws.sut / sut_subdir
            if p.exists():
                items_to_clean.append(("dir", p))

    # Skip if nothing to clean
    if not items_to_clean:
        console.print("[dim]cleanup:[/] no step-specific artifacts to remove")
        return

    # Confirm with user
    console.print(
        f"[yellow]cleanup:[/] found {len(items_to_clean)} artifact(s)"
        f" from step {from_step:02d} onward:"
    )
    for item_type, path in items_to_clean[:10]:
        relative = path.relative_to(ws.root)
        console.print(f"  [dim]-[/] {item_type}: {relative}")
    if len(items_to_clean) > 10:
        console.print(f"  [dim]... and {len(items_to_clean) - 10} more[/]")

    response = console.input("[yellow]Proceed with deletion? [Y/n]: ").strip().lower()
    if response not in ("", "y", "yes"):
        console.print("[yellow]cleanup:[/] cancelled by user")
        return

    # Kill any allure server holding files open in this workspace.
    _kill_allure_for_workspace(ws)

    # Perform cleanup
    deleted_count = 0
    failed_count = 0

    for item_type, path in items_to_clean:
        try:
            if item_type == "dir":
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted_count += 1
        except (OSError, PermissionError) as e:
            failed_count += 1
            _log.warning(
                "cleanup.delete_failed",
                item_type=item_type,
                path=str(path.relative_to(ws.root)),
                error=str(e),
            )

    if deleted_count > 0:
        _log.info(
            "cleanup.completed",
            from_step=from_step,
            deleted_count=deleted_count,
            failed_count=failed_count,
        )
        console.print(
            f"[green]cleanup:[/] removed {deleted_count} artifact(s)"
            f" from step {from_step:02d} onward"
        )

    if failed_count > 0:
        console.print(
            f"[yellow]cleanup:[/] failed to remove {failed_count}"
            " artifact(s) (see logs for details)"
        )


def _select_steps(opts: PipelineOptions) -> list[int]:
    if opts.only_step is not None:
        return [opts.only_step]
    start = opts.from_step or 1
    return [i for i in range(start, TOTAL_STEPS + 1) if i not in opts.skip_steps]


def _mcp_preflight_for_step(
    step: Step,
    *,
    opts: PipelineOptions,
    console: Console,
) -> bool:
    """Probe the MCP servers this step declares it needs, just before it runs.

    Lazy replacement for the legacy pipeline-start preflight. Skipped silently
    when the step's `mcp_servers_required` is empty (most steps). Probing
    contiguously with the step's agent invocation also fixes the
    "playwright reports `pending` at SDK init" race — the npx cache and
    server-side lazy init stay warm long enough for the next SDK spawn to
    catch a connected server. Returns False to abort the pipeline.
    """
    required = getattr(step, "mcp_servers_required", frozenset()) or frozenset()
    if not required:
        return True

    import sys

    from qtea.mcp_manager import load_mcp_config, probe_server

    while True:
        try:
            all_servers = load_mcp_config()
        except (FileNotFoundError, OSError, ValueError) as e:
            console.print(
                f"[red]mcp preflight (step {step.number:02d}):[/] "
                f"could not load .mcp.json: {e}"
            )
            _log.error(
                "step.mcp_preflight_failed",
                step=step.number,
                error=str(e),
            )
            return False

        scoped = {n: s for n, s in all_servers.items() if n in required}
        missing = sorted(required - scoped.keys())
        if missing:
            console.print(
                f"[red]mcp preflight (step {step.number:02d}):[/] "
                f"required server(s) not declared in .mcp.json: "
                f"{', '.join(missing)}"
            )
            _log.error(
                "step.mcp_preflight_missing",
                step=step.number,
                missing=missing,
            )
            return False

        console.print(
            f"[dim]mcp:[/] warming "
            f"{', '.join(sorted(scoped.keys()))} for step "
            f"{step.number:02d}…"
        )
        results = [(name, *probe_server(server)) for name, server in scoped.items()]
        failed = [(n, msg) for n, ok, msg in results if not ok]

        if not failed:
            ok_names = sorted(n for n, ok, _ in results if ok)
            console.print("[dim]mcp:[/] " + ", ".join(f"{n} ok" for n in ok_names))
            _log.info(
                "step.mcp_preflight_ok",
                step=step.number,
                servers=ok_names,
            )
            return True

        console.print(
            f"[red]mcp preflight (step {step.number:02d}):[/] "
            f"one or more required servers failed to start:"
        )
        for name, msg in failed:
            console.print(f"  [red]{name}[/]: {msg}")
        _log.error(
            "step.mcp_preflight_failed",
            step=step.number,
            failed=[{"name": n, "error": m} for n, m in failed],
        )

        if not (sys.stdin.isatty() or opts.ui_mode) or opts.no_hitl or opts.yes:
            console.print(
                "[yellow]Non-interactive mode: fix MCP setup and re-run "
                "(or omit --no-hitl / --yes to enable the retry prompt).[/yellow]"
            )
            return False

        from rich.prompt import Confirm

        if not Confirm.ask(
            "Retry MCP initialization?", default=True, console=console
        ):
            console.print("[dim]Aborted by user.[/]")
            return False


async def run_pipeline(opts: PipelineOptions, *, console: Console | None = None) -> int:
    console = console or Console()

    # Resolve the tri-state cache toggle (None / True / False) into a
    # concrete boolean.  When the user didn't pass --cache or --no-cache
    # (None), auto-detect: enable caching iff BMF sticky-session routing is
    # active (the header that makes prompt-cache reads actually hit).
    # Without sticky sessions the BMF relay does not honour cache_control —
    # callers pay the 25% creation surcharge with zero read-side payback.
    cache_enabled = opts.cache
    if cache_enabled is None:
        headers = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        cache_enabled = "x-bmf-sticky-session-instance" in headers
        if cache_enabled:
            console.print(
                "[dim]prompt caching auto-enabled "
                "(BMF sticky-session header detected)[/]"
            )
        else:
            console.print("[dim]prompt caching disabled (no sticky-session header)[/]")
    elif cache_enabled:
        console.print("[dim]prompt caching force-enabled (--cache)[/]")
    else:
        console.print("[dim]prompt caching disabled (--no-cache)[/]")

    if not cache_enabled:
        os.environ["DISABLE_PROMPT_CACHING"] = "1"
    else:
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

        # Clean up step artifacts and debug directories to prevent pollution
        # across multiple --from-step runs. Can be disabled with --no-cleanup.
        if not opts.no_cleanup:
            _cleanup_step_artifacts(ws, opts.from_step, console)

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
    # No clone-confirmation prompt: qtea's entire purpose is to fetch the
    # SUT, install its dependencies, and run its tests. The user supplied the
    # URL on the command line — that IS the consent. Asking again is noise.
    #
    # Guard: skip materialization on --from-step resume when the SUT is
    # already on the expected branch with step 8's commits intact.
    # Re-materializing would wipe those commits (the generated test files).
    import subprocess

    from qtea._sut_git import branch_name as _branch_name_early
    from qtea._sut_git import current_branch as _current_branch_early
    from qtea.steps.s06_research import _materialize_sut

    sut_git_exists = (ws.sut / ".git").exists()
    is_step_resume = (
        opts.from_step is not None
        and opts.from_step > 1
        and sut_git_exists
    )
    step8_rec = state.steps.get(8)
    step8_done = step8_rec is not None and step8_rec.status in (
        "completed", "warned",
    )
    skip_materialize = is_step_resume and step8_done

    if skip_materialize:
        expected = _branch_name_early(ws.run_id)
        try:
            actual = _current_branch_early(ws.sut)
        except Exception:
            actual = None
        if actual == expected:
            console.print(
                f"[dim]sut:[/] reusing (branch [cyan]{expected}[/cyan])"
            )
            log.info("pipeline.sut_reuse", branch=expected)
        else:
            console.print(
                f"[yellow]sut:[/] branch mismatch "
                f"({actual} != {expected}), re-materializing"
            )
            skip_materialize = False

    if not skip_materialize:
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
    from qtea._sut_git import branch_name as _branch_name
    from qtea._sut_git import current_branch as _current_branch

    if not (ws.sut / ".git").exists():
        msg = (
            f"sut not a git repo at {ws.sut} — materialization left no .git/ "
            f"directory. Re-materialize via `qtea run` without --run-id, "
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
            f"got `{actual_branch}`. The qtea isolation branch was not "
            f"created — re-materialize via `qtea run` without --run-id."
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

    # MCP preflight is now lazy: each step declares its `mcp_servers_required`
    # (see `Step` base class) and `_mcp_preflight_for_step` probes those
    # servers just before the step runs (see the step loop below). Steps
    # that don't use MCP pay zero preflight cost. The previous always-on
    # preflight was wasteful (all 11 steps paid the cost when only Step 9
    # uses MCP) and the warmup was 18 minutes stale by the time the
    # consumer ran. See the `mcp_servers_required` docstring in
    # `steps/base.py`.

    # Replay env resolution from existing Step 6 artifacts (if any).
    # Step 6's `resolve_sut_env()` writes into `os.environ` in-process only;
    # those writes are gone on a fresh `qtea run` invocation. Without this
    # replay, re-running `--from-step 7+` leaves SUT_BASE_URL unset and the
    # JIT resolver (Step 8 runtime) aborts with BASE_URL_UNRESOLVED.
    try:
        from qtea.steps.s06_research import replay_env_from_artifacts
        replay_env_from_artifacts(ws, opts)
    except Exception as e:
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
    _after_step_status = False  # True right after printing a step ok/warned/FAILED line
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

        # Blank line before the banner. When a step-status line was just printed
        # ("step NN ok" / "FAILED"), that line already opened the gap — skip the
        # extra blank so the status and the next banner stay visually grouped.
        if not _after_step_status:
            console.print()
        _after_step_status = False
        console.print(f"[cyan]>>> step {step_num:02d} {step.name}[/]")
        console.print()
        if not _mcp_preflight_for_step(step, opts=opts, console=console):
            log.error("step.mcp_preflight_abort", step=step_num)
            exit_code = 2
            break
        result = await step.execute(ctx)
        record = state.steps.get(step_num)

        if not result.success:
            save_state(state, ws.state_file)
            console.print()
            console.print(f"[red]step {step_num:02d} FAILED:[/] {result.error or result.notes}")
            if record is not None:
                console.print(f"   {_format_step_metrics_line(record)}")
            exit_code = 1
            # Step 9 failures still have test results worth reporting.
            # Let steps 10 (bug classification) and 11 (allure report)
            # run so the operator gets a full report with findings.
            if step_num == 9:
                _after_step_status = True
                continue
            break

        if step_num == 4 and not await review_step_4_strategy(ctx, result, console):
            save_state(state, ws.state_file)
            console.print("[yellow]step 04 rejected by reviewer — aborting[/]")
            exit_code = 1
            break

        if step_num == 7 and not await review_step_7_plan(ctx, result, console):
            save_state(state, ws.state_file)
            console.print("[yellow]step 07 rejected by reviewer — aborting[/]")
            exit_code = 1
            break

        # Phase-D follow-up: when Step 8 stashed WARN/FAIL intent entries on
        # ctx.extras, surface them for human review on TTY. FAILs that should
        # block already aborted Step 8 itself; what reaches here is the
        # WARN-tier (plus FAILs when QTEA_INTENT_FAIL_AS_WARN=1).
        if (
            step_num == 8
            and ctx.extras.get("step8_intent_warnings")
            and not await review_step_8_intents(ctx, result, console)
        ):
            save_state(state, ws.state_file)
            console.print(
                "[yellow]step 08 rejected at intent-review gate — aborting[/]"
            )
            exit_code = 1
            break

        save_state(state, ws.state_file)

        if result.status == "warned":
            sub = f" ({result.sub_status.replace('_', ' ')})" if result.sub_status else ""
            marker = f"warned{sub}"
        elif result.sub_status and result.sub_status != "all_passed":
            marker = f"ok ({result.sub_status.replace('_', ' ')})"
        else:
            marker = "ok"
        line = f"[green]step {step_num:02d} {marker}[/]  -> {len(result.outputs)} outputs"
        if record is not None:
            line += f"  [dim]{_format_step_metrics_line(record)}[/]"
        console.print()
        console.print(line)
        _after_step_status = True

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
    cache_part = ""
    if record.tokens_cache_read or record.tokens_cache_creation:
        parts = []
        if record.tokens_cache_read:
            parts.append(f"{format_tokens(record.tokens_cache_read)} read")
        if record.tokens_cache_creation:
            parts.append(f"{format_tokens(record.tokens_cache_creation)} write")
        cache_part = f" | cache: {', '.join(parts)}"
    return f"[elapsed {duration} | {tokens_in}->{tokens_out} tok{cache_part} | {cost}]"


def _pipeline_totals(state: RunState) -> dict[str, float | int]:
    total_duration = 0.0
    total_in = 0
    total_out = 0
    total_cache_create = 0
    total_cache_read = 0
    total_cost = 0.0
    total_calls = 0
    for rec in state.steps.values():
        if rec.duration_s is not None:
            total_duration += rec.duration_s
        total_in += rec.tokens_input
        total_out += rec.tokens_output
        total_cache_create += rec.tokens_cache_creation
        total_cache_read += rec.tokens_cache_read
        total_cost += rec.cost_usd
        total_calls += rec.agent_calls
    return {
        "total_duration_s": round(total_duration, 3),
        "total_tokens_input": total_in,
        "total_tokens_output": total_out,
        "total_tokens_cache_creation": total_cache_create,
        "total_tokens_cache_read": total_cache_read,
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
    table.add_column("Cache Read", justify="right", no_wrap=True)
    table.add_column("Cache Write", justify="right", no_wrap=True)
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
        cache_read = format_tokens(rec.tokens_cache_read) if rec.tokens_cache_read else "-"
        cache_write = format_tokens(rec.tokens_cache_creation) if rec.tokens_cache_creation else "-"
        status_text = rec.status
        if rec.sub_status and rec.sub_status != "all_passed":
            status_text = f"{rec.status} ({rec.sub_status.replace('_', ' ')})"
        table.add_row(
            f"{step_num:02d}",
            rec.name or "",
            f"[{color}]{status_text}[/]",
            duration,
            format_tokens(rec.tokens_input),
            format_tokens(rec.tokens_output),
            cache_read,
            cache_write,
            str(rec.agent_calls),
            format_cost(rec.cost_usd),
        )

    totals = _pipeline_totals(state)
    total_cache_read = int(totals["total_tokens_cache_read"])
    total_cache_write = int(totals["total_tokens_cache_creation"])
    table.add_section()
    table.add_row(
        "",
        "[bold]TOTAL[/]",
        "",
        f"[bold]{totals['total_duration_s']:.1f}s[/]",
        f"[bold]{format_tokens(int(totals['total_tokens_input']))}[/]",
        f"[bold]{format_tokens(int(totals['total_tokens_output']))}[/]",
        f"[bold]{format_tokens(total_cache_read)}[/]" if total_cache_read else "-",
        f"[bold]{format_tokens(total_cache_write)}[/]" if total_cache_write else "-",
        f"[bold]{totals['total_agent_calls']}[/]",
        f"[bold]{format_cost(float(totals['total_cost_usd']))}[/]",
    )
    console.print(table)
