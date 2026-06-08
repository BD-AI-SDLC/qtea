"""Typer-based CLI: `worca-t run | doctor | init | version`."""

from __future__ import annotations

import asyncio
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
    import json as _json

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
    parallelism: int = typer.Option(1, "--parallelism", min=1, max=16),
    headless: bool = typer.Option(True, "--headless/--headed"),
    debug: bool = typer.Option(
        False, "--debug", help="Run with debug agent live from start."
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Critical-thinking + principal-eng after RCA."
    ),
    strict_xray: bool = typer.Option(False, "--strict-xray"),
    skip_step: list[int] = typer.Option([], "--skip-step"),
    report: ReportMode = typer.Option(ReportMode.auto, "--report"),
    report_inline_images: bool = typer.Option(False, "--report-inline-images"),
    open_report: bool = typer.Option(False, "--open-report"),
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
            "Path to a dev-supplied JSON file mapping locator-constant names "
            "to real selectors (`{locators: {LOGIN_BUTTON: {selector: ...}}}`)."
            " When supplied, the JIT runtime plugin consults this file BEFORE "
            "calling the LLM resolver. Verified at first use via Playwright "
            "count(); mismatches fall through to LLM resolution. Highest-"
            "priority discovery channel — overrides $WORCA_T_DEV_LOCATORS and "
            "the `<sut>/.worca-t/dev-locators.json` convention path."
        ),
    ),
) -> None:
    """Run the full SDLC pipeline."""
    from worca_t.node_env import ensure_node
    from worca_t.pipeline import PipelineOptions, run_pipeline

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
    )
    rc = asyncio.run(run_pipeline(opts, console=console))
    raise typer.Exit(code=rc)


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

    try:
        snapshot_text = snapshot.read_text(encoding="utf-8")
    except OSError as e:
        console.print(f"[red]cannot read snapshot {snapshot}: {e}[/]")
        raise typer.Exit(code=2) from e

    result = resolve_one(
        intent=intent,
        snapshot_text=snapshot_text,
        constant_name=constant,
        test_file=test_file,
        page_url=page_url,
        cache_dir=cache,
        model=model,
        run_id=run_id,
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
