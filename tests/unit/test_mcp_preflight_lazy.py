"""Tests for the lazy per-step MCP preflight (Defect A + B fix).

Until run 20260611-184450, `pipeline.run_pipeline` always probed every
server in `.mcp.json` at startup — even when no selected step used MCP —
and the warmup happened 10+ minutes before the consumer step ran, so the
SDK's MCP init reported `pending` because the actual server connection
was a cold start.

The fix moves preflight into a per-step hook gated by
`Step.mcp_servers_required`. These tests pin:
  - Steps that declare no MCP requirement skip preflight entirely.
  - Steps that declare a requirement get only those servers probed,
    just before the step runs.
  - Missing-server-in-.mcp.json fails fast with exit code 2.
  - Probe failures in non-TTY mode fail fast (no HITL retry loop).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from worca_t.mcp_manager import McpServer
from worca_t.pipeline import (
    PipelineOptions,
    _mcp_preflight_for_step,
    run_pipeline,
)
from worca_t.steps.base import Step, StepContext, StepResult


class _NoMcpStep(Step):
    number = 1
    name = "nomcp"
    timeout_s = 5
    # mcp_servers_required intentionally not set — inherits empty default.

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=True, status="completed", outputs=[])


class _PlaywrightStep(Step):
    number = 2
    name = "needs-pw"
    timeout_s = 5
    mcp_servers_required = frozenset({"playwright"})

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=True, status="completed", outputs=[])


@pytest.fixture
def _console():
    from rich.console import Console
    return Console(file=open(__file__ + ".consolelog", "w", encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-step preflight helper
# ---------------------------------------------------------------------------


def test_preflight_skipped_when_step_declares_no_mcp(_console):
    """A step with empty mcp_servers_required must not call probe_server."""
    opts = PipelineOptions(workspace_base=Path("."))
    step = _NoMcpStep()

    with patch("worca_t.mcp_manager.load_mcp_config") as load_mock, \
         patch("worca_t.mcp_manager.probe_server") as probe_mock:
        result = _mcp_preflight_for_step(step, opts=opts, console=_console)

    assert result is True
    load_mock.assert_not_called()
    probe_mock.assert_not_called()


def test_preflight_probes_only_declared_servers(_console):
    """Only servers in `mcp_servers_required` should be probed — not every
    server in `.mcp.json`."""
    opts = PipelineOptions(workspace_base=Path("."))
    step = _PlaywrightStep()

    fake_config = {
        "playwright": McpServer(name="x", command="echo", args=[], env={}),
        "filesystem": McpServer(name="x", command="echo", args=[], env={}),
        "atlassian": McpServer(name="x", command="echo", args=[], env={}),
    }
    probed: list[str] = []

    def _fake_probe(server, timeout_s=30.0):
        # Find which name was passed by reverse-looking up the server obj.
        for name, srv in fake_config.items():
            if srv is server:
                probed.append(name)
                break
        return True, "spawned ok"

    with patch("worca_t.mcp_manager.load_mcp_config", return_value=fake_config), \
         patch("worca_t.mcp_manager.probe_server", side_effect=_fake_probe):
        result = _mcp_preflight_for_step(step, opts=opts, console=_console)

    assert result is True
    assert probed == ["playwright"], (
        f"preflight probed {probed} — should only touch declared servers"
    )


def test_preflight_fails_when_required_server_missing_from_config(_console):
    """If `.mcp.json` doesn't declare a server the step needs, fail fast."""
    opts = PipelineOptions(workspace_base=Path("."), no_hitl=True)
    step = _PlaywrightStep()

    with patch(
        "worca_t.mcp_manager.load_mcp_config", return_value={},
    ), patch("worca_t.mcp_manager.probe_server") as probe_mock:
        result = _mcp_preflight_for_step(step, opts=opts, console=_console)

    assert result is False
    probe_mock.assert_not_called()


def test_preflight_fails_fast_on_probe_failure_in_non_tty(_console):
    """Non-TTY / --no-hitl / --yes must NOT enter the retry-prompt loop."""
    opts = PipelineOptions(workspace_base=Path("."), no_hitl=True)
    step = _PlaywrightStep()
    fake_config = {"playwright": McpServer(name="x", command="echo", args=[], env={})}

    with patch(
        "worca_t.mcp_manager.load_mcp_config", return_value=fake_config,
    ), patch(
        "worca_t.mcp_manager.probe_server", return_value=(False, "spawn error"),
    ):
        result = _mcp_preflight_for_step(step, opts=opts, console=_console)

    assert result is False


# ---------------------------------------------------------------------------
# End-to-end: full pipeline never preflights when no step requires MCP
# ---------------------------------------------------------------------------


async def test_pipeline_no_preflight_when_no_step_uses_mcp(
    tmp_path: Path, monkeypatch,
):
    """Run a 1-step pipeline whose only step declares no MCP requirement.
    `probe_server` and `load_mcp_config` must NOT be called even once."""
    call_log: list[str] = []

    monkeypatch.setattr(
        "worca_t.pipeline.STEP_REGISTRY", {1: _NoMcpStep()},
    )
    monkeypatch.setattr(
        "worca_t.mcp_manager.load_mcp_config",
        lambda path=None: call_log.append("load") or {},
    )
    monkeypatch.setattr(
        "worca_t.mcp_manager.probe_server",
        lambda *a, **k: call_log.append("probe") or (True, "ok"),
    )

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
    )
    rc = await run_pipeline(opts)
    assert rc == 0
    assert call_log == [], (
        f"pipeline called MCP manager {call_log} — should be silent when "
        f"no step declares mcp_servers_required"
    )


async def test_pipeline_preflights_only_when_required_step_runs(
    tmp_path: Path, monkeypatch,
):
    """Mix a non-MCP step with a Playwright-requiring step — preflight fires
    exactly once, for the Playwright step's slot."""
    call_log: list[tuple[str, int | None]] = []

    monkeypatch.setattr(
        "worca_t.pipeline.STEP_REGISTRY",
        {1: _NoMcpStep(), 2: _PlaywrightStep()},
    )
    monkeypatch.setattr(
        "worca_t.mcp_manager.load_mcp_config",
        lambda path=None: (
            call_log.append(("load", None))
            or {"playwright": McpServer(name="x", command="echo", args=[], env={})}
        ),
    )

    def _probe(server, timeout_s=30.0):
        call_log.append(("probe", None))
        return True, "ok"

    monkeypatch.setattr("worca_t.mcp_manager.probe_server", _probe)

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
    )
    rc = await run_pipeline(opts)
    assert rc == 0
    # load + probe happens exactly once, for step 2.
    assert call_log.count(("load", None)) == 1, (
        f"load_mcp_config called {call_log.count(('load', None))}x — expected 1"
    )
    assert call_log.count(("probe", None)) == 1, (
        f"probe_server called {call_log.count(('probe', None))}x — expected 1"
    )


# ---------------------------------------------------------------------------
# Step 9 actually declares the requirement
# ---------------------------------------------------------------------------


def test_execute_step_probes_playwright_lazily_not_via_preflight():
    """Step 9 must NOT declare playwright in `mcp_servers_required` — that
    would force the pipeline-level preflight to probe Playwright MCP on
    every Step 9 run (5-15s npx warmup), even on green runs where the
    heal agent never spawns. The actual probe is now lazy: inside
    `ExecuteStep.run()`, only when failing tests warrant a heal attempt.

    The class still owns the server name as `_LAZY_MCP_SERVER` so the
    lazy probe knows which server to start.
    """
    from worca_t.steps.s09_execute import ExecuteStep
    step = ExecuteStep()
    assert step.mcp_servers_required == frozenset(), (
        "Step 9 must use the lazy probe path (mcp_servers_required = "
        "frozenset()), not the eager pipeline-level preflight"
    )
    assert step._LAZY_MCP_SERVER == "playwright", (
        "Lazy probe must still target Playwright MCP for the heal agent"
    )


def test_other_steps_declare_no_mcp_requirement():
    """Sanity: only Step 9 currently uses MCP. If this fails, audit whether
    the newly-added step actually needs MCP at runtime."""
    from worca_t.steps.s01_intake import IntakeStep
    from worca_t.steps.s02_refine import RefineStep
    from worca_t.steps.s06_research import ResearchStep
    from worca_t.steps.s08_codegen import CodegenStep
    from worca_t.steps.s10_bug_classifier import BugClassifierStep
    from worca_t.steps.s11_report import ReportStep

    for cls in (
        IntakeStep, RefineStep, ResearchStep,
        CodegenStep, BugClassifierStep, ReportStep,
    ):
        assert cls().mcp_servers_required == frozenset(), (
            f"{cls.__name__} unexpectedly declares MCP servers — "
            f"only Step 9 should today"
        )
