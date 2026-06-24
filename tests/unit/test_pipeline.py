"""Pipeline orchestrator tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from qtea.checkpoints import RunState, save_state
from qtea.pipeline import PipelineOptions, _select_workspace, run_pipeline
from qtea.steps.base import Step, StepContext, StepResult
from qtea.workspace import create_workspace


@pytest.fixture(autouse=True)
def _skip_mcp_preflight(monkeypatch):
    """Tests in this file don't exercise MCPs; stub the preflight to a no-op."""
    monkeypatch.setattr("qtea.mcp_manager.load_mcp_config", lambda path=None: {})


async def test_run_pipeline_completes_with_no_steps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "qtea.pipeline.STEP_REGISTRY", {},
    )
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
    )
    rc = await run_pipeline(opts)
    assert rc == 0


async def test_run_pipeline_runs_only_step(tmp_path: Path, monkeypatch):
    call_log = []

    class _TrackStep(Step):
        number = 1
        name = "track"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            call_log.append(self.number)
            return StepResult(success=True, status="completed", outputs=[])

    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {1: _TrackStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1,
    )
    rc = await run_pipeline(opts)
    assert rc == 0
    assert call_log == [1]


async def test_run_pipeline_stops_on_failure(tmp_path: Path, monkeypatch):
    class _FailStep(Step):
        number = 1
        name = "fail"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            return StepResult(success=False, status="failed", outputs=[], error="boom")

    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {1: _FailStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1,
    )
    rc = await run_pipeline(opts)
    assert rc == 1


def test_select_workspace_default_is_fresh(tmp_path: Path):
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path)
    ws = _select_workspace(opts)
    assert ws.root.exists()


def test_select_workspace_default_ignores_unfinished_prior(tmp_path: Path):
    ws1 = create_workspace(tmp_path)
    state = RunState(run_id=ws1.run_id, workspace=str(ws1.root), spec_source="x", sut_source=".")
    save_state(state, ws1.state_file)

    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path)
    ws2 = _select_workspace(opts)
    assert ws2.run_id != ws1.run_id


def test_select_workspace_resumes_with_run_id(tmp_path: Path):
    ws1 = create_workspace(tmp_path)
    state = RunState(run_id=ws1.run_id, workspace=str(ws1.root), spec_source="x", sut_source=".")
    save_state(state, ws1.state_file)

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, run_id=ws1.run_id,
    )
    ws2 = _select_workspace(opts)
    assert ws2.run_id == ws1.run_id


def test_select_workspace_run_id_missing_raises(tmp_path: Path):
    import pytest
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, run_id="does-not-exist",
    )
    with pytest.raises(FileNotFoundError):
        _select_workspace(opts)


def test_select_workspace_from_step_without_run_id_raises(tmp_path: Path):
    import pytest
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path, from_step=3,
    )
    with pytest.raises(RuntimeError, match="requires --run-id"):
        _select_workspace(opts)


async def test_resume_recovers_spec_and_sut_from_state(tmp_path: Path, monkeypatch):
    """On --run-id, missing --spec/--sut should fall back to state.json."""
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    spec_file = tmp_path / "prior-spec.md"
    spec_file.write_text("# prior")
    sut_dir = tmp_path / "prior-sut"
    sut_dir.mkdir()

    ws_prior = create_workspace(tmp_path)

    # Stub SUT materialization + preflight — unrelated to the fallback we're testing.
    def _fake_materialize(src, dest, run_id):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr("qtea.steps.s06_research._materialize_sut", _fake_materialize)
    monkeypatch.setattr("qtea._sut_git.current_branch", lambda root: "stub")
    monkeypatch.setattr("qtea._sut_git.branch_name", lambda rid: "stub")
    save_state(
        RunState(
            run_id=ws_prior.run_id,
            workspace=str(ws_prior.root),
            spec_source=str(spec_file),
            sut_source=str(sut_dir),
        ),
        ws_prior.state_file,
    )

    opts = PipelineOptions(workspace_base=tmp_path, run_id=ws_prior.run_id)
    rc = await run_pipeline(opts)
    assert rc == 0
    assert opts.spec == str(spec_file)
    assert opts.sut == str(sut_dir)


async def test_fresh_run_without_spec_or_sut_fails(tmp_path: Path, monkeypatch):
    """A fresh run (no --run-id) with no --spec/--sut must error, not crash."""
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    opts = PipelineOptions(workspace_base=tmp_path / ".ws")
    rc = await run_pipeline(opts)
    assert rc == 2


async def test_run_pipeline_debug_sets_extras(tmp_path: Path, monkeypatch):
    captured_ctx = {}

    class _CaptureStep(Step):
        number = 1
        name = "capture"
        timeout_s = 10

        async def run(self, ctx: StepContext) -> StepResult:
            captured_ctx["debug_live"] = ctx.extras.get("debug_live")
            return StepResult(success=True, status="completed", outputs=[])

    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {1: _CaptureStep()})
    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws",
        only_step=1, debug=True,
    )
    await run_pipeline(opts)
    assert captured_ctx["debug_live"] is True


async def test_cache_default_none_without_sticky_disables(tmp_path: Path, monkeypatch):
    """Default cache=None without BMF sticky-session header must disable
    prompt caching (DISABLE_PROMPT_CACHING=1)."""
    import os
    monkeypatch.delenv("DISABLE_PROMPT_CACHING", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    assert opts.cache is None  # tri-state default

    await run_pipeline(opts)
    assert os.environ.get("DISABLE_PROMPT_CACHING") == "1"


async def test_cache_default_none_with_sticky_enables(tmp_path: Path, monkeypatch):
    """Default cache=None with BMF sticky-session header must auto-enable
    prompt caching (clear DISABLE_PROMPT_CACHING)."""
    import os
    monkeypatch.setenv("DISABLE_PROMPT_CACHING", "1")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "x-bmf-sticky-session-instance: 01"
    )
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    await run_pipeline(opts)
    assert "DISABLE_PROMPT_CACHING" not in os.environ


async def test_cache_explicit_false_overrides_sticky(tmp_path: Path, monkeypatch):
    """Explicit --no-cache (cache=False) must disable caching even when
    the sticky-session header is present."""
    import os
    monkeypatch.delenv("DISABLE_PROMPT_CACHING", raising=False)
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "x-bmf-sticky-session-instance: 02"
    )
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws", cache=False,
    )
    await run_pipeline(opts)
    assert os.environ.get("DISABLE_PROMPT_CACHING") == "1"


async def test_cache_flag_on_clears_disable_env(tmp_path: Path, monkeypatch):
    """Explicit --cache (cache=True) must clear any pre-existing
    DISABLE_PROMPT_CACHING so the Claude Code CLI's auto-caching is
    restored for this run."""
    import os
    monkeypatch.setenv("DISABLE_PROMPT_CACHING", "1")
    monkeypatch.setattr("qtea.pipeline.STEP_REGISTRY", {})

    opts = PipelineOptions(
        spec="x", sut=".", workspace_base=tmp_path / ".ws", cache=True,
    )
    await run_pipeline(opts)
    assert "DISABLE_PROMPT_CACHING" not in os.environ


def test_claude_runner_forwards_disable_prompt_caching():
    """claude_runner's env-forwarding filter must include the cache-
    disable knobs even though they don't match the WORCA_/ANTHROPIC_/HTTP
    prefix set. Without this forward, setting the var in pipeline.py is
    inert because the subprocess never sees it."""

    # Read the forwarded_env construction block source-level — it lives
    # inside run_agent and only runs end-to-end. The deterministic check:
    # the cache vars must be in the explicit forward list. Find them.
    from qtea import claude_runner
    src = (Path(claude_runner.__file__)).read_text(encoding="utf-8")
    assert '"DISABLE_PROMPT_CACHING"' in src
    assert '"DISABLE_PROMPT_CACHING_OPUS"' in src
    assert '"DISABLE_PROMPT_CACHING_SONNET"' in src
    assert '"DISABLE_PROMPT_CACHING_HAIKU"' in src


def test_parse_custom_headers(monkeypatch):
    from qtea.config import _parse_custom_headers

    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "x-bmf-sticky-session-instance: 01",
    )
    assert _parse_custom_headers() == {"x-bmf-sticky-session-instance": "01"}


def test_parse_custom_headers_empty(monkeypatch):
    from qtea.config import _parse_custom_headers

    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    assert _parse_custom_headers() == {}


def test_auth_kwargs_include_custom_headers(monkeypatch):
    from qtea.config import anthropic_auth_kwargs

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "x-bmf-sticky-session-instance: 02",
    )
    kwargs = anthropic_auth_kwargs()
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["default_headers"] == {
        "x-bmf-sticky-session-instance": "02",
    }


def test_auth_kwargs_no_custom_headers(monkeypatch):
    from qtea.config import anthropic_auth_kwargs

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    kwargs = anthropic_auth_kwargs()
    assert "default_headers" not in kwargs
