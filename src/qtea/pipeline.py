"""Pipeline orchestrator.

Runs the 11-step QA SDLC pipeline. Each step is a `Step` subclass registered in
`STEP_REGISTRY`. Steps not yet implemented are skipped gracefully so partial
milestones remain runnable end-to-end.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
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
    no_fix: bool = False
    no_incident_memory: bool = False
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
    # Disable the pre-Step-7 headless auth prewarm (driving the SUT's sign-in
    # helper so site-exploration authenticates). Default off = prewarm enabled.
    no_auth_capture: bool = False
    # Run the auth prewarm HEADED (visible browser) so a human can complete an
    # interactive MFA / captcha challenge. Only effective in an interactive
    # session (TTY or UI). SSO with a dedicated service user stays headless.
    auth_headed: bool = False
    # Auth-prewarm strategy: "headed" (human logs in via a visible browser,
    # default) | "mcp" (site-explorer logs in via Playwright MCP) | "script"
    # (run the SUT's sign-in helper in a subprocess) | "off".
    # None → resolved from QTEA_AUTH_PREWARM_MODE, else "headed". See
    # s07_auth_prewarm.auth_prewarm_mode.
    auth_prewarm_mode: str | None = None
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
    # Optional operator-supplied free-text context about the spec, captured at
    # run start (CLI --context/--context-file or the UI pre-run screen). Flows
    # to Step 1 ticket enrichment and Step 2 refinement as trusted guidance.
    # None/empty = no context; behavior is unchanged.
    operator_context: str | None = None
    # Optional operator-supplied context images (local source paths) attached on
    # the UI pre-run screen. Copied into <workspace>/operator-context/images/ at
    # run start and fed to Step 2 refinement. None = no images (or a resume that
    # reuses whatever was copied on the original run).
    operator_context_images: list[str] | None = None


def _materialize_context_images(
    opts: PipelineOptions,
    ws: Any,
    state: RunState,
    log: Any,
) -> list[Path]:
    """Copy operator context images into the workspace; return absolute paths.

    Fresh run (``opts.operator_context_images`` set): validate + copy each
    source into ``<workspace>/operator-context/images/`` (deduping name
    collisions), capping at ``MAX_CONTEXT_IMAGES``, and record workspace-relative
    paths on ``state``. Resume (``opts.operator_context_images is None``): reuse
    the paths already recorded on ``state`` from the original run.
    """
    from qtea.context_images import (
        MAX_CONTEXT_IMAGES,
        ContextImageError,
        validate_image_file,
    )

    if opts.operator_context_images is None:
        # Resume: files were copied on the original run. Keep only survivors.
        kept: list[Path] = []
        rels: list[str] = []
        for rel in state.operator_context_images or []:
            p = ws.root / rel
            if p.is_file():
                kept.append(p)
                rels.append(rel)
        state.operator_context_images = rels
        return kept

    sources = list(opts.operator_context_images)[:MAX_CONTEXT_IMAGES]
    images_dir = ws.root / "operator-context" / "images"
    rels = []
    abs_paths: list[Path] = []
    if sources:
        images_dir.mkdir(parents=True, exist_ok=True)
    for src in sources:
        srcp = Path(src).expanduser()
        try:
            validate_image_file(srcp)
        except ContextImageError as e:
            log.warning("pipeline.context_image_skipped", reason=str(e))
            continue
        dest = images_dir / srcp.name
        n = 1
        while dest.exists():
            dest = images_dir / f"{srcp.stem}-{n}{srcp.suffix}"
            n += 1
        shutil.copy2(srcp, dest)
        rels.append(str(dest.relative_to(ws.root)).replace("\\", "/"))
        abs_paths.append(dest)
    state.operator_context_images = rels
    return abs_paths


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


def _purge_step_aux(state: RunState, step: int) -> None:
    """Drop auxiliary (debug/critical-thinking/PSE) records for one ``step``.

    Aux rows are created only during a step's own ``execute()`` on failure, so
    removing them by step number before that step re-executes only ever drops
    records from a prior, superseded run — never the current one. Without this,
    each resume of a still-failing step stacks another fix-chain trio, inflating
    the pipeline summary (regression seen on run 20260709-083909-223772).
    """
    state.auxiliary_records = [
        a for a in state.auxiliary_records if a.step != step
    ]


def _reset_steps_from(state: RunState, from_step: int) -> None:
    """Drop checkpoint records for steps >= ``from_step`` so they re-execute.

    Without this, ``is_step_complete`` would short-circuit the very step the
    user asked to re-run if it had previously reached 'completed' / 'failed'.
    """
    for k in list(state.steps.keys()):
        if k >= from_step:
            del state.steps[k]
    # Aux records are keyed by step; drop them for the re-run range too so a
    # --from-step resume doesn't stack a fresh fix-chain trio on the prior
    # run's (the loop-level purge handles the plain-resume path).
    state.auxiliary_records = [
        a for a in state.auxiliary_records if a.step < from_step
    ]
    # Re-open the run so pipeline.end can stamp a new finished_at.
    state.finished_at = None
    state.end_reason = None


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


_CODE_WRITING_STEPS = frozenset({7, 8, 9})


def _rollback_sut_to_before_step(
    sut_root: Path, from_step: int,
) -> str | None:
    """Reset the SUT isolation branch to just before ``from_step``'s commits.

    Commit subjects follow the pattern ``qtea/step-NN: <detail>``. We walk
    the log backwards, find the oldest commit whose subject starts with
    ``qtea/step-{from_step:02d}:`` (or any later step), and reset to its
    parent. Returns the sha we reset to, or ``None`` when no rollback was
    needed (no matching commits found).
    """
    import subprocess

    if not (sut_root / ".git").exists():
        return None

    # Subjects that belong to from_step or later
    prefixes = tuple(
        f"qtea/step-{s:02d}:" for s in range(from_step, 12)
    )

    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H %s", "--reverse"],
            cwd=str(sut_root), capture_output=True, text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None

    first_sha: str | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        if subject.startswith(prefixes):
            first_sha = sha
            break

    if first_sha is None:
        return None

    try:
        subprocess.run(
            ["git", "reset", "--hard", f"{first_sha}~1"],
            cwd=str(sut_root), capture_output=True, text=True,
            check=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(sut_root), capture_output=True, text=True,
            check=True,
        )
        new_sha = head.stdout.strip()
        _log.info(
            "cleanup.sut_rollback",
            from_step=from_step,
            reset_to=new_sha,
        )
        return new_sha
    except subprocess.CalledProcessError as e:
        _log.warning(
            "cleanup.sut_rollback_failed",
            from_step=from_step,
            error=(e.stderr or "").strip()[:300],
        )
        return None


def _cleanup_step_artifacts(
    ws: Workspace,
    from_step: int,
    console: Console | None = None,
    *,
    auto_confirm: bool = False,
) -> None:
    """Delete step-specific directories from from_step onward to ensure clean state.

    Removes both work directories (step-NN/) and artifact directories
    (artifacts/stepNN/) for the specified step and all subsequent steps,
    plus debug files and directories for each step. This prevents artifact pollution
    when re-running with --from-step and ensures accurate log analysis.

    Args:
        ws: Workspace containing the directories to clean
        from_step: First step number to clean (inclusive), all later steps also cleaned
        console: Optional console for user prompts (hitl confirmation)
        auto_confirm: Skip the interactive [Y/n] confirmation and proceed. Set
            when stdin is unreachable (UI mode, non-TTY/CI) or the user opted
            into auto-confirm (--yes / --no-hitl). The structlog event
            ``cleanup.auto_confirmed`` records the list of items wiped so the
            UI log panel and JSONL run log preserve the trail even though the
            user never saw the rich-formatted prompt.
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

        # Collect debug items for this step (both files and directories).
        # The glob is intentionally broad: `step-NN-*` covers per-attempt RCAs
        # (`step-NN-attempt*`), aggregated files (`step-NN-rca.md`,
        # `step-NN-fix-proposal.md`), and the fix workdir (`step-NN-fix/`).
        # Prior to run-20260701-114656-9394eb this only globbed
        # `step-NN-attempt*`, leaving stale aggregated RCAs to mislead the
        # next debug pass.
        debug_dir = ws.debug
        if debug_dir.exists():
            for debug_entry in debug_dir.glob(f"step-{step:02d}-*"):
                if debug_entry.is_file():
                    items_to_clean.append(("file", debug_entry))
                elif debug_entry.is_dir():
                    items_to_clean.append(("dir", debug_entry))

    # Sweep any remaining debug/ entries so `<workspace>/debug/` is empty on
    # resume, regardless of which step's failure produced them or whether
    # their naming matches the per-step pattern. Only fires when resuming
    # (from_step >= 1) — a fresh run has no debug entries to sweep.
    debug_dir = ws.debug
    if debug_dir.exists():
        already_queued = {p for _, p in items_to_clean}
        for entry in debug_dir.iterdir():
            if entry in already_queued:
                continue
            kind = "dir" if entry.is_dir() else "file"
            items_to_clean.append((kind, entry))

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

    if auto_confirm:
        # Non-interactive context (UI mode / CI / --yes / --no-hitl). The
        # console.print summary above lands in the silent UI console, so
        # mirror the item list through structlog where the UI log panel
        # (and the run.log.jsonl) can pick it up.
        _log.info(
            "cleanup.auto_confirmed",
            from_step=from_step,
            item_count=len(items_to_clean),
            items=[
                f"{kind}:{path.relative_to(ws.root)}"
                for kind, path in items_to_clean
            ],
        )
    else:
        response = console.input(
            "[yellow]Proceed with deletion? [Y/n]: "
        ).strip().lower()
        if response not in ("", "y", "yes"):
            console.print("[yellow]cleanup:[/] cancelled by user")
            return

    # Kill any allure server holding files open in this workspace.
    _kill_allure_for_workspace(ws)

    # Roll back SUT isolation branch to before from_step's commits so
    # stale generated files don't survive into the re-run.
    if from_step in _CODE_WRITING_STEPS or any(
        s in _CODE_WRITING_STEPS for s in range(from_step, 12)
    ):
        sha = _rollback_sut_to_before_step(ws.sut, from_step)
        if sha:
            console.print(
                f"[green]cleanup:[/] rolled SUT back to [cyan]{sha}[/cyan]"
                f" (before step {from_step:02d} commits)"
            )

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


async def _prewarm_sut_env_for_auth(ctx: StepContext, console: Console) -> None:
    """After Step 6, install the SUT test env early IF an auth prewarm will use
    it, so Step 7's site-explorer can authenticate.

    The SUT's Playwright env isn't installed until Step 9 bootstrap, but
    ``qtea auth-capture`` (which the Step 7 auth prewarm drives) needs it to run
    the SUT's own sign-in helper. This hoists the install so a storage-state can
    be produced before exploration. Gated on an auth prewarm actually being
    applicable (enabled, no existing session, Playwright stack with an
    ``auth_flow.entry_method``) so runs that won't authenticate pay no early
    install cost. Best-effort: any failure logs a warning and the pipeline
    proceeds — Step 9 installs as usual.
    """
    try:
        from qtea.steps.s07_auth_prewarm import (
            auth_prewarm_mode,
            headed_mode_requested,
            is_applicable,
            is_interactive_session,
            load_active_module,
        )

        # Only the `script` strategy runs the SUT's own code and thus needs its
        # test env early. `headed` drives qtea's OWN Playwright (human-driven
        # login), `mcp` drives the bundled Playwright MCP, and `off` does nothing
        # — none need the SUT env, so all skip the early install entirely.
        if auth_prewarm_mode(ctx.options) != "script":
            _log.info("pipeline.env_prewarm_skip", reason="mode_not_script")
            return

        active_module = load_active_module(ctx.workspace.step_dir(6))
        applicable, reason = is_applicable(
            sut_root=ctx.workspace.sut.resolve(),
            workspace_root=ctx.workspace.root,
            active_module=active_module,
            cli_storage_state=getattr(ctx.options, "storage_state", None),
            no_auth_capture=getattr(ctx.options, "no_auth_capture", False),
            headed_requested=headed_mode_requested(ctx.options),
            interactive=is_interactive_session(ctx.options),
        )
        if not applicable:
            _log.info("pipeline.env_prewarm_skip", reason=reason)
            return

        from qtea.steps.s09.attempt_state import _compute_install_sig
        from qtea.steps.s09.context_loaders import (
            _framework,
            _load_stack_profile,
            _research_payload,
        )
        from qtea.test_runner import prepare_sut_env, write_env_prep_marker

        profile = _load_stack_profile(ctx)
        if profile is None or not profile.install_command:
            _log.info("pipeline.env_prewarm_skip", reason="no_stack_profile")
            return
        framework = _framework(_research_payload(ctx), {})
        console.print(
            "[dim]preparing SUT environment early so site-exploration can "
            "authenticate…[/]"
        )
        result = await asyncio.to_thread(
            prepare_sut_env,
            profile,
            cwd=ctx.workspace.sut,
            framework=framework,
            install_log_path=ctx.workspace.root / "env-prep.log",
        )
        if not result.ok:
            _log.warning("pipeline.env_prewarm_install_failed", error=result.error)
            console.print(
                "[yellow]early SUT env prep failed — continuing; Step 9 will "
                "install as usual (site-exploration may be unauthenticated)[/]"
            )
            return
        # Record the install signature so Step 9's first attempt skips a
        # redundant re-install of the now-prepared environment.
        write_env_prep_marker(
            ctx.workspace.root, _compute_install_sig(ctx.workspace.sut, profile),
        )
        _log.info("pipeline.env_prewarm_done", ran_install=result.ran_install)
    except Exception as e:  # never let env prewarm break the pipeline
        _log.warning("pipeline.env_prewarm_unexpected_error", error=str(e))


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

        # UI mode joins non-TTY / --no-hitl / --yes on the bail path:
        # `Confirm.ask` reads from stdin, which the Flet worker thread
        # cannot reach — falling through would hang the run with no UI
        # affordance to recover. Abort cleanly; the UI log panel will
        # surface the structured event and the user can re-run after
        # fixing MCP from a terminal.
        if not sys.stdin.isatty() or opts.ui_mode or opts.no_hitl or opts.yes:
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
        if opts.operator_context is None:
            opts.operator_context = prior_state.operator_context

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
        if sut_path.is_symlink():
            log.warning("pipeline.sut_is_symlink", path=str(sut_path), target=str(sut_path.resolve()))
        if sut_path.is_dir():
            sut_dotenv = sut_path / ".env"
            if sut_dotenv.is_file():
                load_env(sut_dotenv)

    # On a --from-step rerun, discard the prior run's JSONL log so the fresh
    # run starts with a clean audit trail — configure_logging opens the
    # FileHandler in append mode, so without this the log accumulates stale
    # entries from earlier attempts. Must happen BEFORE configure_logging, or
    # the handler would hold the file open (Windows would refuse the unlink).
    # Gated by no_cleanup to stay consistent with _cleanup_step_artifacts.
    if opts.from_step is not None and not opts.no_cleanup and ws.run_log.exists():
        try:
            ws.run_log.unlink()
        except OSError as e:
            (console or Console()).print(
                f"[yellow]cleanup:[/] could not remove prior run log "
                f"{ws.run_log.name}: {e}"
            )

    log = configure_logging(level=opts.log_level, jsonl_path=ws.run_log, run_id=ws.run_id)

    state = prior_state or RunState(
        run_id=ws.run_id,
        workspace=str(ws.root),
        spec_source=opts.spec,
        sut_source=opts.sut,
        operator_context=opts.operator_context,
    )
    # Refresh source pointers if user changed them.
    state.spec_source = opts.spec
    state.sut_source = opts.sut
    state.operator_context = opts.operator_context

    # Materialize operator context images into the workspace (fresh sources are
    # copied in; a resume reuses whatever was copied on the original run). Sets
    # state.operator_context_images (workspace-relative) and yields absolute
    # paths for the StepContext.
    context_image_paths = _materialize_context_images(opts, ws, state, log)

    # Claim this run for the current process. On resume this overwrites the
    # prior (now-dead) pid — correct, since we are the live owner now. Lets
    # `qtea list` tell a live run from one that died without cleanup.
    # Re-open the run: resuming an interrupted/crashed run must clear the prior
    # terminal stamp, else derive_status would report finished/failed while the
    # resumed run is actively executing. The end-of-run path re-stamps both.
    state.pid = os.getpid()
    state.finished_at = None
    state.end_reason = None
    try:
        import psutil
        state.pid_create_time = psutil.Process().create_time()
    except Exception:
        state.pid_create_time = None
    save_state(state, ws.state_file)

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
        # Skip the interactive [Y/n] confirmation when stdin isn't reachable
        # (UI mode swallows the prompt — the worker thread would hang forever
        # at "Initializing... 0/11 steps") or the user opted into auto-yes.
        if not opts.no_cleanup:
            auto_confirm = (
                opts.ui_mode
                or opts.no_hitl
                or opts.yes
                or not sys.stdin.isatty()
            )
            _cleanup_step_artifacts(
                ws, opts.from_step, console, auto_confirm=auto_confirm,
            )

    log.info(
        "pipeline.start",
        spec=opts.spec,
        sut=opts.sut,
        workspace=str(ws.root),
        from_step=opts.from_step,
        only_step=opts.only_step,
        force=opts.force,
        debug=opts.debug,
        no_fix=opts.no_fix,
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
    # A resume is ANY invocation targeting an existing workspace by --run-id
    # (with or without --from-step). Finding 1: previously only `--from-step>1`
    # resumes preserved the SUT; a plain `qtea run --run-id X` re-materialized
    # (wiped) the tree AND then skipped the already-complete codegen steps,
    # leaving Step 9 to run against an empty tree — silently destroying the
    # generated-test branch, the stated deliverable. Now ANY resume whose SUT
    # branch already carries Step-8's commits preserves it; `--from-step`
    # keeps its clean-regenerate behaviour via _rollback_sut_to_before_step.
    resuming = opts.run_id is not None
    is_step_resume = (
        resuming
        and (opts.from_step is None or opts.from_step > 1)
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

    # Invariant (finding 1): the pipeline must NEVER re-materialize (wipe) the
    # SUT and then skip a code-writing step. When resuming an existing run and
    # the SUT WAS freshly materialized (skip_materialize is False — e.g. a
    # branch mismatch forced a re-clone), any previously-"complete" Step 7/8/9
    # records now point at code that no longer exists on disk. Drop those
    # records so they regenerate against the fresh tree instead of being
    # skipped, which would leave Step 9 running on an empty test tree. (A fresh
    # run has no such records; a --from-step resume already reset them.)
    if resuming and not skip_materialize and opts.from_step is None:
        wiped_code_steps = [
            s for s in sorted(_CODE_WRITING_STEPS)
            if s in state.steps
        ]
        if wiped_code_steps:
            for s in wiped_code_steps:
                del state.steps[s]
            save_state(state, ws.state_file)
            log.warning(
                "pipeline.resume_regenerate_after_rematerialize",
                steps=wiped_code_steps,
                reason=(
                    "SUT was re-materialized on resume; code-writing steps "
                    "reset so they regenerate rather than being skipped "
                    "against an empty tree (finding 1 invariant)"
                ),
            )
            console.print(
                f"[yellow]resume:[/] SUT re-materialized — steps "
                f"{wiped_code_steps} will regenerate (not skipped)"
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
        operator_context=opts.operator_context,
        operator_context_images=context_image_paths,
        options=opts,
    )

    if opts.debug:
        ctx.extras["debug_live"] = True

    selected_steps = _select_steps(opts)

    async def _drive_steps() -> int:
        exit_code = 0
        _after_step_status = False  # True right after printing a step ok/warned/FAILED line
        # Index-based iteration so the Step 9->8 back-edge (Gap C) can rewind to
        # Step 8 and replay 8->9 once. ``_i`` advances at the top of each turn, so
        # every existing bare ``continue`` moves to the next step unchanged; the
        # back-edge overrides ``_i`` explicitly before its own ``continue``.
        _step_list = list(selected_steps)
        _i = 0
        while _i < len(_step_list):
            step_num = _step_list[_i]
            _i += 1
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
            # Purge any aux (debug/critical-thinking/PSE) records from a PRIOR
            # execution of this step so the summary reflects only THIS run's
            # fix-chain spend. Aux rows are created solely during a step's own
            # execute() on failure, so filtering by step number is safe — it
            # only drops superseded records. Covers plain --run-id resume,
            # --from-step, and the Step 9->8 back-edge (all re-enter this loop).
            _purge_step_aux(state, step_num)
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
                    # Step 9->8 back-edge (Gap C): a structural codegen defect
                    # (zero tests collected, missing generated import) is not a
                    # heal target — Step 9 asks to regenerate. Replay 8->9 exactly
                    # once per run (guard prevents cycles), passing the reason to
                    # Step 8 so it fixes the specific gap.
                    if (
                        ctx.extras.pop("rerun_step", None) == 8
                        and not ctx.extras.get("_rerun8_used")
                    ):
                        ctx.extras["_rerun8_used"] = True
                        _reason = ctx.extras.get("rerun_reason", "codegen defect")
                        _rerun_kind = ctx.extras.pop("rerun_kind", "naming_defect")
                        ctx.extras["step8_defect_feedback"] = _reason
                        ctx.extras["step8_defect_kind"] = _rerun_kind
                        log.info(
                            "pipeline.step_rerun_requested",
                            from_step=9, target_step=8, reason=_reason,
                            kind=_rerun_kind,
                        )
                        console.print(
                            f"[yellow]step 09 requested Step 8 regeneration "
                            f"({_reason}) — replaying steps 8->9[/]"
                        )
                        # Reset checkpoints so 8 and 9 re-run, and rewind the
                        # step cursor to Step 8 (when present in this run's plan).
                        for _s in (8, 9):
                            state.steps.pop(_s, None)
                        if 8 in _step_list:
                            _i = _step_list.index(8)
                            exit_code = 0
                            continue
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

            # After Step 6 (repo discovery), optionally install the SUT test
            # env early so the Step 7 site-explorer can authenticate. Gated +
            # best-effort — see _prewarm_sut_env_for_auth.
            if step_num == 6:
                await _prewarm_sut_env_for_auth(ctx, console)

        return exit_code

    # Always leave the run in a terminal, truthful state. A clean return leaves
    # ``end_reason`` None (derive_status computes finished/failed from steps).
    # Ctrl-C / UI Stop surfaces as "interrupted"; any other exception as
    # "crashed". A hard-kill that bypasses these handlers is caught later by the
    # PID liveness check (derive_status -> "aborted").
    try:
        exit_code = await _drive_steps()
    except (KeyboardInterrupt, asyncio.CancelledError):
        state.end_reason = "interrupted"
        state.finished_at = datetime.now(UTC).isoformat()
        save_state(state, ws.state_file)
        raise
    except Exception:
        state.end_reason = "crashed"
        state.finished_at = datetime.now(UTC).isoformat()
        save_state(state, ws.state_file)
        raise

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
    # Aux records (debug / critical-thinking / principal-engineer, fired on
    # retry exhaustion) are their own rows in the summary — their totals
    # must roll into the grand TOTAL or the row cost cells won't reconcile
    # with the header cost figure.
    for aux in state.auxiliary_records:
        if aux.duration_s is not None:
            total_duration += aux.duration_s
        total_in += aux.tokens_input
        total_out += aux.tokens_output
        total_cache_create += aux.tokens_cache_creation
        total_cache_read += aux.tokens_cache_read
        total_cost += aux.cost_usd
        total_calls += aux.agent_calls
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

    # Aux rows — one per helper agent (debug / critical-thinking /
    # principal-engineer) that fired on retry exhaustion. Kept between the
    # step rows and TOTAL so the TOTAL is a visible sum of both groups.
    _aux_phase_code = {
        "debug": "D",
        "critical_thinking": "C",
        "principal_engineer": "P",
    }
    _aux_phase_label = {
        "debug": "Debug agent",
        "critical_thinking": "Critical thinking",
        "principal_engineer": "Principal SW engineer",
    }
    if state.auxiliary_records:
        table.add_section()
        for aux in state.auxiliary_records:
            color = status_color.get(aux.status, "dim")
            code = _aux_phase_code.get(aux.phase, "?")
            label = _aux_phase_label.get(aux.phase, aux.phase or aux.agent)
            duration = f"{aux.duration_s:.1f}s" if aux.duration_s is not None else "-"
            cache_read = format_tokens(aux.tokens_cache_read) if aux.tokens_cache_read else "-"
            cache_write = format_tokens(aux.tokens_cache_creation) if aux.tokens_cache_creation else "-"
            table.add_row(
                f"[dim]{code}{aux.step}[/]",
                f"[dim]{label} (step {aux.step:02d})[/]",
                f"[{color}]{aux.status}[/]",
                duration,
                format_tokens(aux.tokens_input),
                format_tokens(aux.tokens_output),
                cache_read,
                cache_write,
                str(aux.agent_calls),
                format_cost(aux.cost_usd),
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
