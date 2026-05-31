"""Tests for the SDK-backed claude runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from worca_t.claude_runner import _agent_key, run_agent

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


# Keep a reference to asyncio so unused-import linters don't strip it.
_ = asyncio
