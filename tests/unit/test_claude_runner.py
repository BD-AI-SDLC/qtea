"""Tests for the SDK-backed claude runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qtea.claude_runner import (
    _agent_key,
    _build_destructive_op_deny_hook,
    _destructive_bash_reason,
    _force_cleanup,
    _is_model_unavailable,
    _stage_inputs,
    run_agent,
)
from qtea.config import get_model_chain
from qtea.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator

from ._fake_claude import install_fake_query


def test_agent_key_strips_suffixes():
    assert _agent_key(Path("refine-spec.agent.md")) == "refine-spec"
    assert _agent_key(Path("test-designer.prompt.md")) == "test-designer"
    assert _agent_key(Path("plain.md")) == "plain"


def test_stage_inputs_skips_same_file_copy(tmp_path: Path):
    """Regression: if a caller pre-writes a file into the workdir AND adds it
    to `inputs`, `_stage_inputs` must NOT crash with `[WinError 32]` (Windows)
    or `SameFileError` (POSIX). The src-equals-dst case is a no-op.
    """
    workdir = tmp_path / "wd"
    workdir.mkdir()
    pre_written = workdir / "active_module.json"
    pre_written.write_text('{"name": "sut"}', encoding="utf-8")
    # `src` and `dst` resolve to the same absolute path → previously raised.
    _stage_inputs(workdir, {"active_module.json": pre_written})
    # File still there, contents unchanged.
    assert pre_written.read_text(encoding="utf-8") == '{"name": "sut"}'


def test_stage_inputs_still_copies_external_files(tmp_path: Path):
    """The same-file guard must not break the normal staging path."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    external = tmp_path / "elsewhere" / "spec.md"
    external.parent.mkdir()
    external.write_text("# spec", encoding="utf-8")
    _stage_inputs(workdir, {"spec.md": external})
    staged = workdir / "spec.md"
    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == "# spec"


def test_stage_inputs_raises_on_missing_source(tmp_path: Path):
    """The missing-source error path stays intact."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    with pytest.raises(FileNotFoundError):
        _stage_inputs(workdir, {"spec.md": tmp_path / "does-not-exist.md"})


def test_agent_result_carries_mcp_servers_failed():
    """Regression: AgentResult must expose `mcp_servers_failed` so Step 8 can
    fail-fast when Playwright MCP didn't start (otherwise the agent aborts
    silently and the user gets confusing 'warned, 0 resolutions' output)."""
    from qtea.claude_runner import AgentResult

    r = AgentResult(
        success=True, exit_code=0, duration_s=0.0,
        transcript_path=Path("x"), stderr_path=Path("y"), metrics_path=Path("z"),
        mcp_servers_failed=["playwright"],
    )
    assert r.mcp_servers_failed == ["playwright"]


def test_agent_result_mcp_servers_failed_defaults_empty():
    from qtea.claude_runner import AgentResult

    r = AgentResult(
        success=True, exit_code=0, duration_s=0.0,
        transcript_path=Path("x"), stderr_path=Path("y"), metrics_path=Path("z"),
    )
    assert r.mcp_servers_failed == []


async def test_run_agent_happy_path(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[
            {"type": "system", "subtype": "init", "data": {"mcp_servers": []}},
            {"type": "assistant",
             "content": [{"type": "text", "text": "hello world"}]},
            {"type": "result", "result": "done"},
        ],
    )

    agent = tmp_path / "demo.agent.md"
    agent.write_text("---\nname: demo\n---\nbe brief", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    workdir = tmp_path / "wd"

    result = await run_agent(
        agent,
        workdir=workdir,
        inputs={},
        user_prompt="say hi",
        timeout_s=10,
        max_turns=1,
        mcp_source=mcp,
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.final_text == "done"
    assert result.transcript_path.exists()
    assert result.metrics_path.exists()
    transcript = result.transcript_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(transcript) == 3
    assert (workdir / "demo.agent.md").exists()
    assert (workdir / ".mcp.json").exists()


async def test_run_agent_defaults_to_empty_mcp(tmp_path: Path, monkeypatch):
    """Default `enable_mcp=False` writes an empty `{"mcpServers": {}}` into
    the workdir regardless of what `mcp_source` references. This is the
    no-spawn default — most steps audit-confirmed they don't need MCP, so
    paying the spawn cost everywhere was waste.
    """
    install_fake_query(monkeypatch)
    agent = tmp_path / "demo.agent.md"; agent.write_text("x", encoding="utf-8")
    # A non-empty source — proves we IGNORE it when enable_mcp is off.
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}),
                   encoding="utf-8")
    workdir = tmp_path / "wd-default"

    result = await run_agent(
        agent,
        workdir=workdir,
        inputs={},
        user_prompt="hi",
        timeout_s=10,
        mcp_source=mcp,
    )
    assert result.success
    staged = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
    assert staged == {"mcpServers": {}}


async def test_run_agent_enable_mcp_stages_project_config(tmp_path: Path, monkeypatch):
    """Explicit `enable_mcp=True` stages the project's real `.mcp.json` so
    the SDK spawns the declared servers. Step 9 is the only caller that
    uses this path today.
    """
    install_fake_query(monkeypatch)
    agent = tmp_path / "heal.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"playwright": {"command": "npx"}}}),
                   encoding="utf-8")
    workdir = tmp_path / "wd-enabled"

    result = await run_agent(
        agent,
        workdir=workdir,
        inputs={},
        user_prompt="hi",
        timeout_s=10,
        mcp_source=mcp,
        enable_mcp=True,
    )
    assert result.success
    staged = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
    assert "playwright" in staged["mcpServers"]


async def test_run_agent_stages_inputs(tmp_path: Path, monkeypatch):
    install_fake_query(monkeypatch)
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    src = tmp_path / "src-spec.md"; src.write_text("SPEC", encoding="utf-8")
    workdir = tmp_path / "wd2"

    result = await run_agent(
        agent,
        workdir=workdir,
        inputs={"spec.md": src},
        user_prompt="go",
        timeout_s=10,
        mcp_source=mcp,
    )
    assert result.success
    assert (workdir / "spec.md").read_text(encoding="utf-8") == "SPEC"


async def test_run_agent_timeout(tmp_path: Path, monkeypatch):
    install_fake_query(monkeypatch, delay_s=5)
    agent = tmp_path / "slow.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd3",
        inputs={},
        user_prompt="hang",
        timeout_s=1,
        mcp_source=mcp,
    )
    assert result.success is False
    assert result.timed_out is True
    assert "timeout" in (result.error or "")


async def test_run_agent_missing_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude on PATH
    monkeypatch.setenv("QTEA_CLAUDE_BIN", "definitely-not-claude-xyz")
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd4",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is False
    assert "not found" in (result.error or "")


async def test_run_agent_sdk_exception(tmp_path: Path, monkeypatch):
    install_fake_query(monkeypatch, raises=RuntimeError("sdk blew up"))
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd5",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is False
    assert "sdk blew up" in (result.error or "")


async def test_run_agent_missing_input_raises(tmp_path: Path, monkeypatch):
    install_fake_query(monkeypatch)
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        await run_agent(
            agent,
            workdir=tmp_path / "wd6",
            inputs={"missing.md": tmp_path / "nonexistent.md"},
            user_prompt="go",
            timeout_s=5,
            mcp_source=mcp,
        )


async def test_run_agent_captures_token_usage_and_cost(tmp_path: Path, monkeypatch):
    """ResultMessage.usage + total_cost_usd land on AgentResult.metrics."""
    install_fake_query(
        monkeypatch,
        messages=[
            {
                "type": "result",
                "result": "done",
                "usage": {
                    "input_tokens": 1234,
                    "output_tokens": 567,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 5000,
                },
                "total_cost_usd": 0.0421,
                "num_turns": 3,
            },
        ],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-tokens",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is True
    assert result.metrics.input_tokens == 1234
    assert result.metrics.output_tokens == 567
    assert result.metrics.cache_creation_input_tokens == 200
    assert result.metrics.cache_read_input_tokens == 5000
    assert result.metrics.cost_usd == pytest.approx(0.0421)
    assert result.metrics.num_turns == 3

    # metrics.json on disk also includes the new fields.
    on_disk = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    assert on_disk["tokens_input"] == 1234
    assert on_disk["tokens_output"] == 567
    assert on_disk["cost_usd"] == pytest.approx(0.0421)


async def test_run_agent_pushes_into_active_accumulator(tmp_path: Path, monkeypatch):
    """When CURRENT_STEP_METRICS is set, run_agent records into it."""
    install_fake_query(
        monkeypatch,
        messages=[
            {
                "type": "result",
                "result": "done",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "total_cost_usd": 0.002,
            },
        ],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    acc = StepMetricsAccumulator()
    token = CURRENT_STEP_METRICS.set(acc)
    try:
        # Two calls in the same context should aggregate.
        for i in range(2):
            await run_agent(
                agent,
                workdir=tmp_path / f"wd-acc-{i}",
                inputs={},
                user_prompt="go",
                timeout_s=5,
                mcp_source=mcp,
            )
    finally:
        CURRENT_STEP_METRICS.reset(token)

    assert acc.agent_calls == 2
    assert acc.totals.input_tokens == 20
    assert acc.totals.output_tokens == 10
    assert acc.totals.cost_usd == pytest.approx(0.004)


async def test_run_agent_tolerates_missing_usage(tmp_path: Path, monkeypatch):
    """Old SDK responses without usage/total_cost_usd should still succeed."""
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-nousage",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is True
    assert result.metrics.input_tokens == 0
    assert result.metrics.cost_usd == 0.0


async def test_run_agent_captures_session_id(tmp_path: Path, monkeypatch):
    """session_id from init SystemMessage lands on AgentResult."""
    install_fake_query(
        monkeypatch,
        messages=[
            {"type": "system", "subtype": "init",
             "data": {"mcp_servers": [], "session_id": "sess-abc-123"}},
            {"type": "result", "result": "done"},
        ],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-sess",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is True
    assert result.session_id == "sess-abc-123"


async def test_run_agent_passes_resume_to_sdk(tmp_path: Path, monkeypatch):
    """resume=<session_id> is propagated to ClaudeAgentOptions."""
    captured: dict = {}

    def _capture(prompt, options):
        captured["resume"] = getattr(options, "resume", None)

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        on_call=_capture,
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    await run_agent(
        agent,
        workdir=tmp_path / "wd-resume",
        inputs={},
        user_prompt="continue",
        timeout_s=5,
        mcp_source=mcp,
        resume="sess-xyz-789",
    )
    assert captured["resume"] == "sess-xyz-789"


async def test_run_agent_does_not_overwrite_audit_files(tmp_path: Path, monkeypatch):
    """Two calls in the same workdir produce two numbered transcript/metrics/stderr."""
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")
    wd = tmp_path / "wd-multi"

    r1 = await run_agent(agent, workdir=wd, inputs={}, user_prompt="one",
                         timeout_s=5, mcp_source=mcp)
    r2 = await run_agent(agent, workdir=wd, inputs={}, user_prompt="two",
                         timeout_s=5, mcp_source=mcp)

    # Paths must differ, both must exist on disk.
    assert r1.transcript_path != r2.transcript_path
    assert r1.metrics_path != r2.metrics_path
    assert r1.stderr_path != r2.stderr_path
    assert r1.transcript_path.exists()
    assert r2.transcript_path.exists()
    # Numbered naming.
    assert r1.transcript_path.name == "transcript-00.jsonl"
    assert r2.transcript_path.name == "transcript-01.jsonl"
    assert r1.metrics_path.name == "metrics-00.json"
    assert r2.metrics_path.name == "metrics-01.json"


async def test_run_agent_dumps_user_prompt_to_logs(tmp_path: Path, monkeypatch):
    """run_agent must write the literal user_prompt to
    <workdir>/logs/user-prompt-XX.md alongside the transcript.

    Transcripts only log SDK→client events (init / thinking / assistant /
    result); they never echo the client→SDK user_prompt. Without this
    dump, post-mortem debugging requires re-deriving the runtime-
    substituted f-string (stack_hint, env_hint, reuse_hint, JIT hint,
    sut_root path, ...) from the source — a nuisance and easy to get
    wrong. Numbered to match the transcript so HITL / storm-wait / heal
    re-invocations stay side-by-side.
    """
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")
    wd = tmp_path / "wd-prompt"

    sentinel = "## Step contract\n\nFollow the plan in `./plan.json` literally."
    r = await run_agent(agent, workdir=wd, inputs={},
                        user_prompt=sentinel, timeout_s=5, mcp_source=mcp)

    dumped = wd / "logs" / "user-prompt-00.md"
    assert dumped.exists(), (
        f"expected user_prompt dump at {dumped}; "
        f"logs/ contents: {list((wd / 'logs').iterdir())}"
    )
    assert dumped.read_text(encoding="utf-8") == sentinel
    # Numbered to align with transcript-00.jsonl for the same call.
    assert r.transcript_path.name == "transcript-00.jsonl"
    assert dumped.name == "user-prompt-00.md"


async def test_run_agent_audit_files_land_in_logs_subdir(tmp_path: Path, monkeypatch):
    """Audit files (transcript / stderr / metrics) MUST live under
    ``<workdir>/logs/``, not the workdir root.

    Regression guard for the workdir-cleanup refactor: dropping these
    files at the workdir root cluttered the human-scannable view of
    staged inputs / agent / claude_md / .mcp.json. The logs subdir
    keeps the root tidy and groups per-call audit data together.
    """
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
    )
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")
    wd = tmp_path / "wd-logs"

    r = await run_agent(agent, workdir=wd, inputs={}, user_prompt="hi",
                        timeout_s=5, mcp_source=mcp)

    logs_dir = wd / "logs"
    assert logs_dir.is_dir()
    assert r.transcript_path.parent == logs_dir
    assert r.stderr_path.parent == logs_dir
    assert r.metrics_path.parent == logs_dir
    # No audit files should have leaked to the workdir root.
    assert not (wd / "transcript-00.jsonl").exists()
    assert not (wd / "stderr-00.log").exists()
    assert not (wd / "metrics-00.json").exists()


async def test_run_agent_timeout_preserves_partial_metrics(tmp_path: Path, monkeypatch):
    """Tokens / session_id captured before timeout survive on AgentResult.

    Regression: previously when ``asyncio.wait_for`` raised TimeoutError, the
    locals holding metrics/session_id were never assigned, so all billing
    information was lost even when a ResultMessage had been written before
    the timeout fired.
    """
    import asyncio as _asyncio

    from claude_agent_sdk import ResultMessage, SystemMessage

    async def _slow_query(*, prompt, options=None, transport=None):
        # First yield an init with a session_id (captured instantly).
        sysmsg = SystemMessage.__new__(SystemMessage)
        sysmsg.subtype = "init"
        sysmsg.data = {"mcp_servers": [], "session_id": "sess-survived"}
        yield sysmsg
        # Then a ResultMessage with usage data.
        rmsg = ResultMessage.__new__(ResultMessage)
        rmsg.result = "partial"
        rmsg.usage = {"input_tokens": 42, "output_tokens": 7}
        rmsg.total_cost_usd = 0.003
        rmsg.num_turns = 1
        yield rmsg
        # Now hang forever — the timeout must fire while we're stuck here.
        await _asyncio.sleep(3600)

    monkeypatch.setattr(
        "qtea.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )
    monkeypatch.setattr("qtea.claude_runner.query", _slow_query)

    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-partial",
        inputs={},
        user_prompt="hang after work",
        timeout_s=1,
        mcp_source=mcp,
    )

    # Timed out — but partial metrics survived through _DriveState.
    assert result.success is False
    assert result.timed_out is True
    assert result.session_id == "sess-survived"
    assert result.metrics.input_tokens == 42
    assert result.metrics.output_tokens == 7
    assert result.metrics.cost_usd == pytest.approx(0.003)


async def test_force_cleanup_is_bounded_by_grace_s(tmp_path: Path, monkeypatch):
    """_force_cleanup must return within grace_s even when task ignores cancel.

    Direct unit test of the helper. Uses a shielded sleep to make the task
    genuinely uncancellable, and asserts cleanup gives up after grace_s.
    """
    import asyncio as _asyncio

    async def _uncancellable():
        # Shield so the cancel cannot interrupt this sleep.
        await _asyncio.shield(_asyncio.sleep(30))

    task = _asyncio.create_task(_uncancellable())

    # No psutil children to clean up — patch to return empty list.
    class FakeMe:
        def children(self, recursive=True):
            return []
    monkeypatch.setattr(
        "qtea.claude_runner.psutil.Process",
        lambda pid: FakeMe(),
    )

    start = _asyncio.get_event_loop().time()
    await _force_cleanup(task, set(), grace_s=0.5)
    elapsed = _asyncio.get_event_loop().time() - start

    # Cleanup must respect the grace bound, not hang for 30s.
    assert elapsed < 2.0, f"_force_cleanup hung {elapsed:.1f}s (grace=0.5s)"

    # Best effort: cancel the stray task so it doesn't leak into other tests.
    task.cancel()
    try:
        await task
    except (_asyncio.CancelledError, Exception):
        pass


async def test_force_cleanup_spares_pre_existing_children(tmp_path: Path, monkeypatch):
    """_force_cleanup must not terminate processes that existed before agent.start."""
    import asyncio as _asyncio

    killed: list[int] = []

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def terminate(self):
            killed.append(self.pid)
        def kill(self):
            killed.append(self.pid)
        def wait(self, timeout=None):
            return 0

    class FakeMe:
        def __init__(self, all_children):
            self._all = all_children
        def children(self, recursive=True):
            return self._all

    all_children = [FakeProc(101), FakeProc(102), FakeProc(103)]
    monkeypatch.setattr(
        "qtea.claude_runner.psutil.Process",
        lambda pid: FakeMe(all_children),
    )
    monkeypatch.setattr(
        "qtea.claude_runner.psutil.wait_procs",
        lambda procs, timeout=None: (procs, []),
    )

    # Pre-existing PIDs include 101, 102. Only 103 should be killed.
    pre_existing = {101, 102}

    async def _noop():
        pass

    task = _asyncio.create_task(_noop())
    await task  # let it finish

    await _force_cleanup(task, pre_existing, grace_s=0.1)

    assert killed == [103]


def test_is_model_unavailable_detects_outage_errors():
    assert _is_model_unavailable("Error: 529 overloaded") is True
    assert _is_model_unavailable("model_not_available for claude-sonnet-4-6") is True
    assert _is_model_unavailable("service_unavailable: try again later") is True
    assert _is_model_unavailable("503 Service Unavailable") is True
    assert _is_model_unavailable("insufficient capacity for model") is True
    assert _is_model_unavailable("Command failed with exit code 15 (exit code: 15)") is True
    assert _is_model_unavailable(
        "There's an issue with the selected model (claude-haiku-4-5). "
        "It may not exist or you may not have access to it."
    ) is True


def test_is_model_unavailable_ignores_other_errors():
    assert _is_model_unavailable("sdk blew up") is False
    assert _is_model_unavailable("timeout after 300s") is False
    assert _is_model_unavailable("FileNotFoundError: spec.md missing") is False


def test_get_model_chain_returns_primary_plus_fallbacks():
    chain = get_model_chain("claude-sonnet-4-6")
    assert chain[0] == "claude-sonnet-4-6"
    assert "claude-opus-4-6" in chain
    assert "claude-haiku-4-5@20251001" in chain
    assert len(chain) == 3


def test_get_model_chain_unknown_model_returns_singleton():
    chain = get_model_chain("claude-unknown-99")
    assert chain == ["claude-unknown-99"]


async def test_run_agent_falls_back_on_model_unavailable(tmp_path: Path, monkeypatch):
    """When the primary model is unavailable, run_agent retries with the fallback."""

    from claude_agent_sdk import ResultMessage

    call_count = 0
    models_seen: list[str | None] = []

    async def _query_with_fallback(*, prompt, options=None, transport=None):
        nonlocal call_count
        call_count += 1
        model = getattr(options, "model", None)
        models_seen.append(model)
        if call_count == 1:
            raise RuntimeError("529 overloaded")
        rmsg = ResultMessage.__new__(ResultMessage)
        rmsg.result = "ok"
        yield rmsg

    monkeypatch.setattr(
        "qtea.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )
    monkeypatch.setattr("qtea.claude_runner.query", _query_with_fallback)

    agent = tmp_path / "a.agent.md"
    agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-fallback",
        inputs={},
        user_prompt="go",
        timeout_s=10,
        model="claude-sonnet-4-6",
        mcp_source=mcp,
    )

    assert result.success is True
    assert call_count == 2
    assert models_seen[0] == "claude-sonnet-4-6"
    assert models_seen[1] == "claude-opus-4-6"

    on_disk = json.loads(result.metrics_path.read_text(encoding="utf-8"))
    assert on_disk["model"] == "claude-opus-4-6"
    assert on_disk["model_requested"] == "claude-sonnet-4-6"
    assert on_disk["models_attempted"] == ["claude-sonnet-4-6", "claude-opus-4-6"]


async def test_run_agent_no_fallback_on_non_model_error(tmp_path: Path, monkeypatch):
    """Non-model errors should NOT trigger the fallback chain."""
    call_count = 0

    async def _query_that_fails(*, prompt, options=None, transport=None):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("some random sdk error")
        yield  # unreachable -- make this an async generator

    monkeypatch.setattr(
        "qtea.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )
    monkeypatch.setattr("qtea.claude_runner.query", _query_that_fails)

    agent = tmp_path / "a.agent.md"
    agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")

    result = await run_agent(
        agent,
        workdir=tmp_path / "wd-no-fallback",
        inputs={},
        user_prompt="go",
        timeout_s=10,
        model="claude-sonnet-4-6",
        mcp_source=mcp,
    )

    assert result.success is False
    assert call_count == 1


# ---------------------------------------------------------------------------
# Destructive-op deny hook — mechanical enforcement of CLAUDE.md's
# git-safety hard rules (defense-in-depth against prompt-injected Bash
# commands, not just prose in agent instructions).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git push --force origin main",
    "git push -f origin qtea/run-1",
    "git branch -D feature/old",
    "git checkout main",
    "git checkout master",
    "git rebase -i HEAD~3",
    "git filter-branch --tree-filter 'rm secrets'",
    "git clean -fdx",
    "rm -rf /some/path",
    "rm -rf ./build",
])
def test_destructive_bash_reason_denies_known_patterns(command: str):
    assert _destructive_bash_reason(command) is not None


@pytest.mark.parametrize("command", [
    "git status",
    "git add -A",
    'git commit -m "wip"',
    "git log --oneline -5",
    "git diff",
    "npm install",
    "npm run build",
    "pytest -x tests/unit",
    "python -m pytest",
    "npx playwright test",
])
def test_destructive_bash_reason_allows_legitimate_commands(command: str):
    assert _destructive_bash_reason(command) is None


@pytest.mark.asyncio
async def test_destructive_op_deny_hook_denies_bash_reset_hard():
    hooks = _build_destructive_op_deny_hook()
    callback = hooks["PreToolUse"][0].hooks[0]
    result = await callback(
        {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}},
        "tool-use-1",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "git-safety" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_destructive_op_deny_hook_allows_normal_bash():
    hooks = _build_destructive_op_deny_hook()
    callback = hooks["PreToolUse"][0].hooks[0]
    result = await callback(
        {"tool_name": "Bash", "tool_input": {"command": "npm install"}},
        "tool-use-2",
        None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_destructive_op_deny_hook_ignores_non_bash_tools():
    """A non-Bash tool call is never inspected, even if its input happens to
    contain destructive-looking text (e.g. a Write tool writing a file whose
    CONTENT mentions `git reset --hard` in a comment)."""
    hooks = _build_destructive_op_deny_hook()
    callback = hooks["PreToolUse"][0].hooks[0]
    result = await callback(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "x.md", "content": "git reset --hard"},
        },
        "tool-use-3",
        None,
    )
    assert result == {}


# Keep a reference to asyncio so unused-import linters don't strip it.
_ = asyncio
