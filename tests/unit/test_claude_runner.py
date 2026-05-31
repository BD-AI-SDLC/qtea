"""Tests for the SDK-backed claude runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from worca_t.claude_runner import _agent_key, run_agent
from worca_t.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator

from ._fake_claude import install_fake_query


def test_agent_key_strips_suffixes():
    assert _agent_key(Path("refine-spec.agent.md")) == "refine-spec"
    assert _agent_key(Path("test-manager.prompt.md")) == "test-manager"
    assert _agent_key(Path("plain.md")) == "plain"


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
    monkeypatch.setenv("WORCA_T_CLAUDE_BIN", "definitely-not-claude-xyz")
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


# Keep a reference to asyncio so unused-import linters don't strip it.
_ = asyncio
