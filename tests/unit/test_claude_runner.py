"""Tests for the core claude runner using a fake `claude` shim."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from worca_t.claude_runner import _agent_key, run_agent


def _write_fake_claude(tmp_path: Path, *, events: list[dict], exit_code: int = 0,
                      sleep_s: float = 0.0) -> Path:
    """Write a cross-platform fake `claude` CLI that emits stream-json events."""
    payload_file = tmp_path / "fake_payload.json"
    payload_file.write_text(json.dumps({"events": events, "sleep_s": sleep_s, "exit_code": exit_code}),
                            encoding="utf-8")

    py_script = tmp_path / "fake_claude_impl.py"
    py_script.write_text(dedent(f"""
        import json, sys, time
        payload = json.loads(open(r"{payload_file}", "r", encoding="utf-8").read())
        for evt in payload["events"]:
            sys.stdout.write(json.dumps(evt) + "\\n")
            sys.stdout.flush()
        time.sleep(payload.get("sleep_s", 0))
        sys.exit(payload.get("exit_code", 0))
    """), encoding="utf-8")

    if os.name == "nt":
        bin_path = tmp_path / "claude.cmd"
        bin_path.write_text(f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n', encoding="utf-8")
    else:
        bin_path = tmp_path / "claude"
        bin_path.write_text(f"#!/usr/bin/env bash\nexec \"{sys.executable}\" \"{py_script}\" \"$@\"\n",
                            encoding="utf-8")
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


@pytest.fixture
def fake_claude_env(tmp_path: Path, monkeypatch):
    """Install a fake `claude` on PATH and a minimal agent + mcp config."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def install(events: list[dict], **kwargs) -> Path:
        bin_path = _write_fake_claude(bin_dir, events=events, **kwargs)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("WORCA_T_CLAUDE_BIN", "claude.cmd" if os.name == "nt" else "claude")
        return bin_path

    return install


def test_agent_key_strips_suffixes():
    assert _agent_key(Path("refine-spec.agent.md")) == "refine-spec"
    assert _agent_key(Path("test-manager.prompt.md")) == "test-manager"
    assert _agent_key(Path("plain.md")) == "plain"


def test_run_agent_happy_path(tmp_path: Path, fake_claude_env):
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello world"}]}},
        {"type": "result", "result": "done"},
    ]
    fake_claude_env(events)

    agent = tmp_path / "demo.agent.md"
    agent.write_text("---\nname: demo\n---\nbe brief", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    workdir = tmp_path / "wd"

    result = run_agent(
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


def test_run_agent_stages_inputs(tmp_path: Path, fake_claude_env):
    fake_claude_env([{"type": "result", "result": "ok"}])
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    src = tmp_path / "src-spec.md"; src.write_text("SPEC", encoding="utf-8")
    workdir = tmp_path / "wd2"

    result = run_agent(
        agent,
        workdir=workdir,
        inputs={"spec.md": src},
        user_prompt="go",
        timeout_s=10,
        mcp_source=mcp,
    )
    assert result.success
    assert (workdir / "spec.md").read_text(encoding="utf-8") == "SPEC"


def test_run_agent_timeout(tmp_path: Path, fake_claude_env):
    fake_claude_env([{"type": "system"}], sleep_s=5)
    agent = tmp_path / "slow.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = run_agent(
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


def test_run_agent_missing_binary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude on PATH
    monkeypatch.setenv("WORCA_T_CLAUDE_BIN", "definitely-not-claude-xyz")
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    result = run_agent(
        agent,
        workdir=tmp_path / "wd4",
        inputs={},
        user_prompt="go",
        timeout_s=5,
        mcp_source=mcp,
    )
    assert result.success is False
    assert "not found" in (result.error or "")


def test_run_agent_stream_done_grace_period(tmp_path: Path, fake_claude_env):
    """After a result event, the runner should force-kill after the grace period, not hang."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "result": "all done"},
    ]
    # Process emits result then hangs for 120s (simulates Windows pipe-handle hang).
    fake_claude_env(events, sleep_s=120)

    agent = tmp_path / "hang.agent.md"
    agent.write_text("---\nname: hang\n---\ntest", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    import time
    start = time.monotonic()
    result = run_agent(
        agent,
        workdir=tmp_path / "wd_grace",
        inputs={},
        user_prompt="hang test",
        timeout_s=300,
        max_turns=1,
        mcp_source=mcp,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 60, f"took {elapsed:.1f}s — grace period did not kick in"
    assert result.final_text == "all done"


def test_run_agent_missing_input_raises(tmp_path: Path, fake_claude_env):
    fake_claude_env([{"type": "result", "result": "ok"}])
    agent = tmp_path / "a.agent.md"; agent.write_text("x", encoding="utf-8")
    mcp = tmp_path / ".mcp.json"; mcp.write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        run_agent(
            agent,
            workdir=tmp_path / "wd5",
            inputs={"missing.md": tmp_path / "nonexistent.md"},
            user_prompt="go",
            timeout_s=5,
            mcp_source=mcp,
        )
