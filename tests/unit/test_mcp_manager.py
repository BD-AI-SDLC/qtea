"""Tests for MCP config loader + env substitution."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.mcp_manager import _substitute_env, load_mcp_config, stage_mcp_config


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv("EMPTY", "")
    out = _substitute_env({"a": "${FOO}", "b": ["x", "${MISSING}"], "c": {"d": "p${FOO}q"}})
    assert out == {"a": "bar", "b": ["x", ""], "c": {"d": "pbarq"}}


def test_load_mcp_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_URL", "https://example.atlassian.net")
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "atlassian": {
                "command": "npx",
                "args": ["-y", "atlassian-mcp"],
                "env": {"ATLASSIAN_URL": "${ATLASSIAN_URL}"},
            }
        }
    }), encoding="utf-8")
    servers = load_mcp_config(cfg)
    assert servers["atlassian"].env["ATLASSIAN_URL"] == "https://example.atlassian.net"
    assert servers["atlassian"].command == "npx"


def test_stage_mcp_config(tmp_path: Path):
    src = tmp_path / ".mcp.json"
    src.write_text(json.dumps({"mcpServers": {"x": {"command": "echo"}}}), encoding="utf-8")
    target = tmp_path / "wd"
    out = stage_mcp_config(target, source=src)
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "x" in data["mcpServers"]
