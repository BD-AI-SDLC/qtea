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
