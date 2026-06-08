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


def test_probe_server_strips_secrets(monkeypatch):
    """SECRET_ENV_KEYS must not appear in the env passed to MCP server probes."""
    import subprocess

    from worca_t.config import SECRET_ENV_KEYS
    from worca_t.mcp_manager import McpServer, probe_server

    for key in SECRET_ENV_KEYS:
        monkeypatch.setenv(key, f"FAKE_{key}")

    captured_env: dict[str, str] | None = None

    class FakeProc:
        returncode = 0
        pid = 99999
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)
        def kill(self):
            pass
        def terminate(self):
            pass
        def poll(self):
            return 0

    def fake_popen(cmd, *, stdin, stdout, stderr, env, **kw):
        nonlocal captured_env
        captured_env = dict(env)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    server = McpServer(name="test", command="echo", args=[], env={})
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/echo")
    probe_server(server)
    assert captured_env is not None
    for key in SECRET_ENV_KEYS:
        assert key not in captured_env, f"{key} leaked to MCP server probe"


def test_probe_server_passes_resolved_path_to_popen(monkeypatch):
    """Regression: on Windows `npx` resolves to `npx.CMD`; passing the bare
    name to subprocess.Popen fails with WinError 2 because CreateProcess
    doesn't resolve .cmd / .bat wrappers. probe_server must hand Popen the
    full path returned by shutil.which.
    """
    import subprocess

    from worca_t.mcp_manager import McpServer, probe_server

    captured_argv: list[str] | None = None

    class FakeProc:
        returncode = 0
        pid = 99998
        stderr = None
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)
        def kill(self): pass
        def terminate(self): pass
        def poll(self): return 0

    def fake_popen(cmd, *, stdin, stdout, stderr, env, **kw):
        nonlocal captured_argv
        captured_argv = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("shutil.which", lambda _: "C:\\nvm4w\\nodejs\\npx.CMD")
    ok, _msg = probe_server(McpServer(name="t", command="npx", args=["-y", "x"], env={}))
    assert ok
    assert captured_argv == ["C:\\nvm4w\\nodejs\\npx.CMD", "-y", "x"]


def test_stage_mcp_config_isolates_per_workdir(tmp_path: Path):
    """Per-call MCP isolation regression: two consecutive stage_mcp_config
    calls into distinct workdirs MUST produce distinct staged config files
    with no shared mutable state. This is what guarantees Step 8a's
    Playwright browser doesn't leak into Step 8b or Step 9 (see
    `mcp_manager` module docstring 'Per-call MCP isolation guarantee')."""
    src = tmp_path / ".mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"playwright": {
            "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"],
        }}}),
        encoding="utf-8",
    )
    wd_a = tmp_path / "step08-workdir"
    wd_b = tmp_path / "step09-workdir"

    staged_a = stage_mcp_config(wd_a, source=src)
    staged_b = stage_mcp_config(wd_b, source=src)

    assert staged_a != staged_b
    assert staged_a.parent == wd_a
    assert staged_b.parent == wd_b
    # Mutating one staged file must not affect the other (proves they're
    # independent files on disk, not a shared symlink or cached object).
    staged_a.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert json.loads(staged_b.read_text(encoding="utf-8"))["mcpServers"]["playwright"]["command"] == "npx"


def test_stage_mcp_config_does_not_rewrite_playwright_args(tmp_path: Path):
    """Regression: worca-t must NOT touch the Playwright MCP's `--headless`
    flag based on the CLI `--headed` option. The MCP is a background tool;
    its head state is controlled solely by `.mcp.json`. The CLI flag instead
    controls Step 9's SUT test execution (see `test_runner._strip_headless_flag`).

    Earlier versions appended `--headed` to the MCP args when the CLI flag
    was set, which made `@playwright/mcp` exit with
    `error: unknown option '--headed'` and Step 8 produced empty results.
    """
    src = tmp_path / ".mcp.json"
    base_args = ["-y", "@playwright/mcp@latest", "--headless"]
    src.write_text(
        json.dumps({"mcpServers": {"playwright": {
            "command": "npx", "args": base_args,
        }}}),
        encoding="utf-8",
    )
    out = stage_mcp_config(tmp_path / "wd", source=src)
    args = json.loads(out.read_text(encoding="utf-8"))["mcpServers"]["playwright"]["args"]
    assert args == base_args
    assert "--headed" not in args
