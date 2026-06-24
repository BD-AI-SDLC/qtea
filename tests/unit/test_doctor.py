"""Tests for `qtea doctor` health checks."""

from __future__ import annotations

import json
from pathlib import Path

from qtea.doctor import (
    Check,
    check_allure,
    check_mcp_config,
    check_proxy,
    check_schemas,
    check_workspace_writable,
    run_all_checks,
)


def test_check_proxy_detects_vars(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:3128")
    c = check_proxy()
    assert c.severity == "ok"
    assert "HTTP_PROXY" in c.message


def test_check_proxy_reports_none(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    c = check_proxy()
    assert c.severity == "info"


def test_check_mcp_config_valid(tmp_path: Path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"pw": {}}}), encoding="utf-8")
    c = check_mcp_config(tmp_path)
    assert c.severity == "ok"
    assert "pw" in c.message


def test_check_mcp_config_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("qtea.doctor.package_resource_root", lambda: tmp_path / "nope")
    c = check_mcp_config(tmp_path)
    assert c.severity == "fail"


def test_check_schemas_valid(tmp_path: Path, monkeypatch):
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "test.schema.json").write_text('{"type": "object"}', encoding="utf-8")
    monkeypatch.setattr("qtea.doctor.package_resource_root", lambda: tmp_path)
    c = check_schemas()
    assert c.severity == "ok"
    assert "1 schemas" in c.message


def test_check_workspace_writable(tmp_path: Path):
    c = check_workspace_writable(tmp_path / "ws")
    assert c.severity == "ok"


def test_check_allure_not_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    c = check_allure()
    assert c.severity == "info"
    assert "not installed" in c.message


def test_run_all_checks_returns_list(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QTEA_CLAUDE_BIN", "nonexistent-claude-binary")
    checks = run_all_checks(tmp_path, workspace=tmp_path / "ws")
    assert isinstance(checks, list)
    assert all(isinstance(c, Check) for c in checks)
    assert len(checks) >= 8


def test_run_all_checks_always_includes_mcp_probes(tmp_path: Path, monkeypatch):
    """MCP probes must always run — they are the only signal that the
    pipeline can actually launch agents on this machine. Hiding them
    behind a flag is how broken MCP setups escape doctor unnoticed.
    """
    from qtea.doctor import run_all_checks

    monkeypatch.setattr("qtea.doctor.probe_server", lambda srv, timeout_s=8.0: (True, "stub ok"))
    checks = run_all_checks(tmp_path, workspace=tmp_path / "ws")
    mcp_checks = [c for c in checks if c.name.startswith("mcp:")]
    assert mcp_checks, "expected at least one mcp:* check in run_all_checks output"


def test_check_mcp_servers_reports_failure_as_fail(tmp_path: Path, monkeypatch):
    """A probe failure must surface as `fail`, not `warn`. The run_pipeline
    cold-start treats this as fatal; doctor must agree.
    """
    from qtea.doctor import check_mcp_servers

    cfg = tmp_path / ".mcp.json"
    cfg.write_text(
        '{"mcpServers": {"x": {"command": "npx", "args": []}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("qtea.doctor.probe_server", lambda srv, timeout_s=8.0: (False, "spawn error: [WinError 2]"))
    checks = check_mcp_servers(tmp_path)
    assert checks, "expected at least one mcp check"
    assert all(c.severity == "fail" for c in checks), [
        (c.name, c.severity, c.message) for c in checks
    ]
