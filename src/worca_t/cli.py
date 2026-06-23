"""Typer-based CLI: `worca-t run | doctor | init | version`."""

from __future__ import annotations

import asyncio
import os
import sys
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console

from worca_t import __version__
from worca_t.config import get_settings, load_env

app = typer.Typer(
    name="worca-t",
    help="Worca-T - fully autonomous QA SDLC orchestrator.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
console = Console()


class ReportMode(StrEnum):
    auto = "auto"
    allure = "allure"
    builtin = "builtin"
    both = "both"


class LogLevel(StrEnum):
    info = "info"
    debug = "debug"
    trace = "trace"


@app.callback()
def _root() -> None:
    """Worca-T entry point."""
    load_env()


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"worca-t {__version__}")


@app.command(name="list")
def list_workspaces(
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Workspace base dir (defaults to ~/.worca-t)."
    ),
    limit: int = typer.Option(
        20, "--limit", min=1, max=500, help="Max workspaces to display."
    ),
    all_: bool = typer.Option(
        False, "--all", "-a", help="Show stale/empty workspaces too."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of table."),
) -> None:
    """List workspaces under the base dir, newest first.

    For each workspace shows: run-id, status (running/finished/failed/empty),
    last completed step (1-11), step count, started timestamp, and source spec.
    By default, empty workspaces (zero completed steps) are hidden; pass
    --all to include them. Use the run-id with `worca-t run --run-id ...`.
    """

    from rich.table import Table

    from worca_t.checkpoints import load_state
    from worca_t.config import get_settings

    base = workspace or get_settings().default_workspace
    if not base.exists():
        console.print(f"[yellow]no workspaces found under {base}[/]")
        raise typer.Exit(code=0)

    candidates = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    rows: list[dict[str, object]] = []
    for ws_dir in candidates:
        state_file = ws_dir / "state.json"
        state = load_state(state_file) if state_file.exists() else None
        if state is None:
            row = {
                "run_id": ws_dir.name,
                "status": "no-state",
                "last_step": None,
                "step_count": 0,
                "started_at": None,
                "spec": None,
                "stale": True,
            }
        else:
            completed = sorted(
                k for k, v in state.steps.items()
                if v.status in ("completed", "skipped")
            )
            last_step = completed[-1] if completed else None
            any_failed = any(v.status == "failed" for v in state.steps.values())
            if state.finished_at is None and state.steps:
                status = "running"
            elif any_failed:
                status = "failed"
            elif state.finished_at is not None:
                status = "finished"
            else:
                status = "empty"
            row = {
                "run_id": state.run_id,
                "status": status,
                "last_step": last_step,
                "step_count": len(state.steps),
                "started_at": state.started_at,
                "spec": state.spec_source,
                "stale": last_step is None,
            }
        rows.append(row)

    if not all_:
        rows = [r for r in rows if not r["stale"]]

    rows = rows[:limit]

    if json_out:
        console.print_json(data=rows)
        raise typer.Exit(code=0)

    if not rows:
        console.print(
            f"[yellow]no workspaces with completed steps under {base} "
            "(use --all to show stale ones)[/]"
        )
        raise typer.Exit(code=0)

    table = Table(title=f"Workspaces under {base}", show_lines=False)
    table.add_column("run-id", style="cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("last", justify="right", no_wrap=True)
    table.add_column("steps", justify="right", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("spec", no_wrap=True, overflow="ellipsis")

    status_color = {
        "running": "yellow",
        "finished": "green",
        "failed": "red",
        "empty": "dim",
        "no-state": "dim",
    }
    for r in rows:
        color = status_color.get(str(r["status"]), "white")
        spec_str = str(r["spec"] or "-")
        # Show only the leaf of file/URL paths to keep the column narrow.
        if spec_str not in ("-", ""):
            leaf = spec_str.replace("\\", "/").rsplit("/", 1)[-1] or spec_str
            spec_str = leaf
        table.add_row(
            str(r["run_id"]),
            f"[{color}]{r['status']}[/]",
            "-" if r["last_step"] is None else str(r["last_step"]),
            str(r["step_count"]),
            (str(r["started_at"]) or "-")[:19],
            spec_str,
        )
    console.print(table)


@app.command()
def doctor(
    workspace: Path | None = typer.Option(None, "--workspace", "-w"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON report."),
) -> None:
    """Run health checks (claude CLI, MCP, proxy, env, schemas)."""
    from worca_t.doctor import run_doctor
    from worca_t.node_env import ensure_node

    ensure_node(console=console)
    rc = run_doctor(workspace=workspace, console=console, json_out=json_out)
    raise typer.Exit(code=rc)


@app.command()
def run(
    spec: str | None = typer.Option(
        None,
        "--spec",
        help=(
            "jira:KEY-123 | https://*.atlassian.net/browse/KEY-123 "
            "| path/to/spec.md | URL. Required for a fresh run; optional "
            "with --run-id (falls back to the prior run's stored value)."
        ),
    ),
    sut: str | None = typer.Option(
        None,
        "--sut",
        help=(
            "Local path or git URL of System Under Test. Required for a "
            "fresh run; optional with --run-id (falls back to the prior "
            "run's stored value)."
        ),
    ),
    workspace: Path | None = typer.Option(None, "--workspace", "-w"),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help="Target an existing workspace by run-id (e.g. 20260525-201007-e28a51).",
    ),
    from_step: int | None = typer.Option(None, "--from-step", min=1, max=11),
    only_step: int | None = typer.Option(None, "--only-step", min=1, max=11),
    force: bool = typer.Option(
        False, "--force", help="Ignore checkpoints; re-run everything."
    ),
    parallelism: int = typer.Option(2, "--parallel-run", min=0, max=16, help="Number of parallel test workers (0 = in-process)."),
    headless: bool = typer.Option(True, "--headless/--headed"),
    debug: bool = typer.Option(
        False,
        "--debug",
        help=(
            "Invoke the debug agent on EVERY failed step attempt (not just "
            "the final failure). Diagnosis-only RCA — never edits source. "
            "Output: <workspace>/debug/step-NN-attemptM-debug-rca.md."
        ),
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Critical-thinking + principal-eng after RCA."
    ),
    strict_xray: bool = typer.Option(False, "--strict-xray"),
    skip_step: list[int] = typer.Option([], "--skip-step"),
    report: ReportMode = typer.Option(ReportMode.auto, "--report"),
    report_inline_images: bool = typer.Option(False, "--report-inline-images"),
    open_report: bool = typer.Option(
        False,
        "--open-report",
        help=(
            "Open the built-in worca-t HTML report in the browser after the run. "
            "Not needed with --report allure or --report both — those modes open "
            "the Allure UI automatically when allure generation succeeds."
        ),
    ),
    log_level: LogLevel = typer.Option(LogLevel.info, "--log-level"),
    no_hitl: bool = typer.Option(
        False,
        "--no-hitl",
        help="Disable interactive prompts for blockers/clarifications (CI mode).",
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        exists=True,
        help="Path to a .env file whose values are loaded into the process environment (keys only flow to agents, never values).",
    ),
    module: str | None = typer.Option(
        None,
        "--module",
        help="For monorepo SUTs: name of the module to target (must match a discovered module). When omitted, a single-module SUT is auto-selected and multi-module SUTs trigger an auto-detect heuristic against the refined spec.",
    ),
    isolated_tests: bool = typer.Option(
        False,
        "--isolated-tests",
        help="Escape hatch: mirror generated tests into a `worca-tests/` subdir (under the active module's path) instead of integrating into the SUT's existing test folder convention.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip interactive confirmation when cloning a remote SUT repository.",
    ),
    no_auto_deps: bool = typer.Option(
        False,
        "--no-auto-deps",
        help=(
            "Disable automatic install of missing test dependencies detected "
            "at pytest collection time. By default, known-safe missing deps "
            "(e.g. allure-pytest) are installed and committed to the worca-t "
            "isolation branch; unknown ones trigger a HITL prompt."
        ),
    ),
    dev_locators: Path | None = typer.Option(
        None,
        "--dev-locators",
        help=(
            "Path to a dev-supplied JSON file with selectors the runtime "
            "consults BEFORE the LLM resolver. Two match modes:\n"
            "  • Tier 1a (exact key) — JSON keys equal worca-t's generated "
            "constant names (used by HITL-replay).\n"
            "  • Tier 1b (intent pool) — entries carry an `intent` description "
            "(plus optional `page_url`); the runtime fuzzy-matches `tbd(...)` "
            "intents against the pool. Frontend devs can use arbitrary keys.\n"
            "Tuning env vars: WORCA_T_DEV_POOL_THRESHOLD (default 0.65), "
            "WORCA_T_DEV_POOL_MARGIN (0.10), WORCA_T_DEV_POOL_PAGE_PENALTY (0.15). "
            "Overrides $WORCA_T_DEV_LOCATORS and the "
            "`<workspace>/locator-cache/dev-locators.json` default."
        ),
    ),
    storage_state: Path | None = typer.Option(
        None,
        "--storage-state",
        help=(
            "Path to a Playwright `storageState.json` file (cookies + "
            "localStorage). When supplied, Step 9 injects "
            "`--storage-state=<path>` into Playwright MCP so the heal-agent's "
            "browser boots already authenticated — skips the auth-replay "
            "cost (10-30s per heal call). Resolution priority: this flag > "
            "$WORCA_T_STORAGE_STATE > `<sut>/.worca-t/storage-state.json` "
            "(produced by `worca-t auth-capture`) > `<workspace>/storage-"
            "state.json` (auto-captured by the runtime plugin on the first "
            "passing test of the current run)."
        ),
    ),
    cache: bool | None = typer.Option(
        None,
        "--cache/--no-cache",
        help=(
            "Toggle Claude Code prompt caching for this run. Default is "
            "auto-detect: enabled when BMF sticky-session routing is active "
            "(ANTHROPIC_CUSTOM_HEADERS contains x-bmf-sticky-session-instance), "
            "disabled otherwise. Pass --cache to force on (e.g. direct "
            "Anthropic API or Vertex AI). Pass --no-cache to force off."
        ),
    ),
    no_cleanup: bool = typer.Option(
        False,
        "--no-cleanup",
        help="Disable automatic cleanup of step artifacts and debug directories when using --from-step. By default, --from-step cleans step-NN/, artifacts/stepNN/, and debug/step-NN-attempt* directories from the target step onward.",
    ),
    no_static_check: bool = typer.Option(
        False,
        "--no-static-check",
        help=(
            "Disable Step 8 Phase B.6 (native static-check gate). By default, "
            "the SUT stack's own type-checker (pyright for Python; tsc with "
            "--allowJs --checkJs for JS/TS) runs against worca-generated test "
            "code BEFORE Step 9 executes them, with one bounded autofix pass "
            "via codegen-violation-fixer. Pass this flag to skip the gate "
            "entirely (e.g. CI runs where the SUT's tooling is not available "
            "or for stacks outside the v1 dispatch). Equivalent to setting "
            "WORCA_T_NO_STATIC_CHECK=1."
        ),
    ),
) -> None:
    """Run the full SDLC pipeline."""
    from worca_t.node_env import ensure_node
    from worca_t.pipeline import PipelineOptions, run_pipeline

    if no_static_check:
        # Phase B.6 reads this env var directly (matches the WORCA_T_SKIP_*
        # opt-out precedent used by Phase D's intent scorer). Setting it
        # here from the flag means the flag and the env var are symmetric;
        # a user can drive the same behavior from either side.
        os.environ["WORCA_T_NO_STATIC_CHECK"] = "1"

    ensure_node(console=console)
    settings = get_settings()
    opts = PipelineOptions(
        spec=spec,
        sut=sut,
        workspace_base=workspace or settings.default_workspace,
        run_id=run_id,
        from_step=from_step,
        only_step=only_step,
        force=force,
        parallelism=parallelism,
        headless=headless,
        debug=debug,
        fix=fix,
        strict_xray=strict_xray,
        skip_steps=set(skip_step),
        report=report.value,
        report_inline_images=report_inline_images,
        open_report=open_report,
        log_level=log_level.value,
        env_file=env_file,
        no_hitl=no_hitl,
        module=module,
        isolated_tests=isolated_tests,
        yes=yes,
        no_auto_deps=no_auto_deps,
        dev_locators=dev_locators,
        storage_state=storage_state,
        cache=cache,
        no_cleanup=no_cleanup,
    )
    rc = asyncio.run(run_pipeline(opts, console=console))
    raise typer.Exit(code=rc)


@app.command(name="auth-capture")
def auth_capture(
    sut: Path = typer.Option(
        ...,
        "--sut",
        exists=True,
        file_okay=False,
        dir_okay=True,
        help=(
            "Path to the SUT root. Must contain `.worca-t/sut_inventory.json` "
            "(produced by a prior `worca-t run` Step 6) and a usable `.venv/` "
            "with Playwright installed."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Where to write the storageState.json. Defaults to "
            "`<sut>/.worca-t/storage-state.json` (the convention path that "
            "Step 9's storage-state resolver picks up automatically)."
        ),
    ),
    headed: bool = typer.Option(
        True,
        "--headed/--headless",
        help=(
            "Browser visibility. Default --headed so you can complete MFA / "
            "SSO / captcha interactively. --headless is only useful for SUTs "
            "whose auth flow is fully automatable (in which case Use case B "
            "auto-capture during a normal `worca-t run` is simpler)."
        ),
    ),
    timeout: int = typer.Option(
        600,
        "--timeout",
        help=(
            "Subprocess timeout in seconds. Default 600 (10 min) — generous "
            "to accommodate interactive MFA. Raise for SUTs with complex "
            "SSO flows; lower for headless CI capture."
        ),
    ),
) -> None:
    """One-shot Playwright storageState capture for cross-run reuse (Use case A).

    Runs the SUT's sign-in helper (resolved from `sut_inventory.json`
    `auth_flow.entry_method`) in a HEADED Chromium so you can complete
    interactive auth (MFA, SSO, captcha) once. Saves
    `context.storage_state(path=<output>)` for Step 9's heal agent to
    reuse via Playwright MCP's `--storage-state` flag.

    For SUTs whose tests already auth successfully (no MFA), you don't
    need this — Use case B (auto-capture by the runtime plugin during a
    normal `worca-t run`) handles it automatically. Use this when the
    SUT's tests cannot fully automate the auth flow.
    """
    from worca_t.auth_capture import cmd_auth_capture
    from worca_t.storage_state import mask_path

    try:
        out_path = cmd_auth_capture(
            sut=sut, output=output, headed=headed, timeout_s=timeout,
        )
    except (FileNotFoundError, ValueError, NotImplementedError, RuntimeError) as e:
        console.print(f"[red]auth-capture failed:[/] {e}")
        raise typer.Exit(code=1) from e
    console.print(
        f"[green]auth-capture[/] saved storage state to "
        f"[bold]{mask_path(out_path)}[/] (absolute: {out_path})"
    )
    console.print(
        "[dim]Subsequent `worca-t run` calls will reuse this file "
        "automatically (Use case A — SUT convention path).[/]"
    )


@app.command()
def resolve(
    intent: str = typer.Option(..., "--intent", help="Semantic intent of the locator (from the `tbd(...)` call in codegen)."),
    snapshot: Path = typer.Option(..., "--snapshot", exists=True, help="Path to the AOM snapshot JSON the runtime captured."),
    constant: str = typer.Option(..., "--constant", help="Locator constant name (e.g. LOGIN_BUTTON), for cache keying + provenance."),
    cache: Path | None = typer.Option(None, "--cache", help="Cache directory; resolver checks `<cache>/locator-cache.json` first and writes the resolved entry back on success."),
    test_file: str | None = typer.Option(None, "--test-file", help="SUT-relative path of the test/POM file that owns the constant (extra cache-key entropy)."),
    page_url: str | None = typer.Option(None, "--page-url", help="URL the snapshot came from (informational, stored in the cache entry)."),
    model: str | None = typer.Option(None, "--model", help="Override the LLM model id. Defaults to $WORCA_T_RESOLVER_MODEL or claude-sonnet-4-6."),
    run_id: str | None = typer.Option(None, "--run-id", help="Run id stamped into the cache file (defaults to $WORCA_T_RUN_ID)."),
) -> None:
    """Resolve one TBD locator. Invoked as a subprocess by the JIT pytest plugin.

    Reads the AOM snapshot, checks the cache, falls through to a single
    Anthropic API call on miss, and writes a one-line JSON object to stdout
    describing the result. Exits 0 on success (including the `unresolvable`
    source); 2 on input errors.
    """
    import json as _json

    from worca_t.jit_resolver import resolve_one
    from worca_t.runtime.dev_locators import load_dev_locators

    try:
        snapshot_text = snapshot.read_text(encoding="utf-8")
    except OSError as e:
        console.print(f"[red]cannot read snapshot {snapshot}: {e}[/]")
        raise typer.Exit(code=2) from e

    # Tier 4 LLM prior: pass any dev-locator entries with `intent` so the
    # model can prefer them over freshly-derived selectors. Discovery uses
    # the same precedence as the runtime plugin (CLI > env > convention).
    pool_locators, _src, _warnings = load_dev_locators()
    dev_pool = [
        {"selector": e.selector, "intent": e.intent, "page_url": e.page_url}
        for e in pool_locators.values() if e.intent
    ] or None

    result = resolve_one(
        intent=intent,
        snapshot_text=snapshot_text,
        constant_name=constant,
        test_file=test_file,
        page_url=page_url,
        cache_dir=cache,
        model=model,
        run_id=run_id,
        dev_pool=dev_pool,
    )
    # Single-line JSON on stdout for the plugin to capture.
    print(_json.dumps(result.as_dict(), ensure_ascii=False))
    raise typer.Exit(code=0)


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
