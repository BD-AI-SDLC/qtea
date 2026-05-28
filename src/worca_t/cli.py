"""Typer-based CLI: `worca-t run | doctor | init | version`."""

from __future__ import annotations

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
    probe_mcp: bool = typer.Option(
        False, "--probe-mcp", help="Smoke-spawn each MCP server."
    ),
) -> None:
    """Run health checks (claude CLI, MCP, proxy, env, schemas)."""
    from worca_t.doctor import run_doctor
    from worca_t.node_env import ensure_node

    ensure_node(console=console)
    rc = run_doctor(
        workspace=workspace, console=console, json_out=json_out, probe_mcp=probe_mcp
    )
    raise typer.Exit(code=rc)


@app.command()
def run(
    spec: str = typer.Option(
        ..., "--spec", help="jira:KEY-123 | path/to/spec.md | URL"
    ),
    sut: str = typer.Option(
        ..., "--sut", help="Local path or git URL of System Under Test"
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
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        exists=True,
        help="Path to a .env file whose values are loaded into the process environment (keys only flow to agents, never values).",
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
    )
    rc = run_pipeline(opts, console=console)
    raise typer.Exit(code=rc)


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
