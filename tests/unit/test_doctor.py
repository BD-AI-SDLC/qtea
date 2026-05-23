"""Tests for `worca-t doctor` health checks."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.doctor import (
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
    monkeypatch.setattr("worca_t.doctor.package_resource_root", lambda: tmp_path / "nope")
    c = check_mcp_config(tmp_path)
    assert c.severity == "fail"


def test_check_schemas_valid(tmp_path: Path, monkeypatch):
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "test.schema.json").write_text('{"type": "object"}', encoding="utf-8")
    monkeypatch.setattr("worca_t.doctor.package_resource_root", lambda: tmp_path)
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
    monkeypatch.setenv("WORCA_T_CLAUDE_BIN", "nonexistent-claude-binary")
    checks = run_all_checks(tmp_path, workspace=tmp_path / "ws")
    assert isinstance(checks, list)
    assert all(isinstance(c, Check) for c in checks)
    assert len(checks) >= 8
