"""Proxy detection and propagation tests."""

from __future__ import annotations

from worca_t.proxy import detected_proxies, mask_secrets, safe_subprocess_env, with_proxy_env


def test_detected_proxies_returns_set_keys(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    p = detected_proxies()
    assert "HTTP_PROXY" in p
    assert "HTTPS_PROXY" in p
    assert "NO_PROXY" not in p


def test_detected_proxies_empty_when_unset(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    assert detected_proxies() == {}


def test_with_proxy_env_includes_proxy_keys(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    env = with_proxy_env()
    has_proxy = any(v == "http://p:1" for v in env.values())
    assert has_proxy


def test_with_proxy_env_merges_extra(monkeypatch):
    env = with_proxy_env({"CUSTOM_VAR": "hello"})
    assert env["CUSTOM_VAR"] == "hello"


def test_safe_subprocess_env_strips_inherited_secrets(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "from-parent")
    env = safe_subprocess_env()
    assert "JIRA_API_TOKEN" not in env


def test_safe_subprocess_env_preserves_explicitly_supplied_secret(monkeypatch):
    """Regression: MCP servers declare required secrets in `extra`; scrubbing
    must not strip them, or e.g. the atlassian MCP exits with 'JIRA_API_TOKEN
    is required'."""
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    env = safe_subprocess_env({"JIRA_API_TOKEN": "explicit-value"})
    assert env["JIRA_API_TOKEN"] == "explicit-value"


def test_safe_subprocess_env_explicit_overrides_inherited_secret(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "from-parent")
    env = safe_subprocess_env({"JIRA_API_TOKEN": "explicit-value"})
    assert env["JIRA_API_TOKEN"] == "explicit-value"


def test_safe_subprocess_env_isolate_venv_strips_virtualenv(monkeypatch):
    """A worca-t process started from a venv (e.g. via `uv tool install
    --editable`) inherits VIRTUAL_ENV. When that env leaks into a poetry
    subprocess, poetry happily reuses worca-t's venv as the SUT's venv
    whenever the Python versions agree — and then `poetry install` reports
    "in sync" while pytest fails on SUT-only imports. `isolate_venv=True`
    must strip VIRTUAL_ENV and POETRY_ACTIVE so poetry creates a fresh
    SUT-specific venv."""
    monkeypatch.setenv("VIRTUAL_ENV", "/path/to/worca-t/.venv")
    monkeypatch.setenv("POETRY_ACTIVE", "1")
    env_leaked = safe_subprocess_env()
    assert env_leaked.get("VIRTUAL_ENV") == "/path/to/worca-t/.venv"
    env_isolated = safe_subprocess_env(isolate_venv=True)
    assert "VIRTUAL_ENV" not in env_isolated
    assert "POETRY_ACTIVE" not in env_isolated


def test_safe_subprocess_env_isolate_venv_preserves_explicit(monkeypatch):
    """Caller-supplied VIRTUAL_ENV via `extra` is the explicit declaration of
    intent — must survive the strip just like an explicit secret does."""
    monkeypatch.setenv("VIRTUAL_ENV", "/path/from/parent")
    env = safe_subprocess_env({"VIRTUAL_ENV": "/explicit/path"}, isolate_venv=True)
    assert env["VIRTUAL_ENV"] == "/explicit/path"


def test_safe_subprocess_env_isolate_venv_default_off(monkeypatch):
    """Default behavior is unchanged — only opt-in callers (poetry SUT
    subprocesses) get the strip."""
    monkeypatch.setenv("VIRTUAL_ENV", "/path/to/venv")
    env = safe_subprocess_env()
    assert env["VIRTUAL_ENV"] == "/path/to/venv"


def test_mask_secrets_redacts_keys():
    env = {
        "ANTHROPIC_API_KEY": "sk-secret",
        "PATH": "/usr/bin",
        "JIRA_XRAY_API_KEY": "token",
    }
    masked = mask_secrets(env)
    assert masked["ANTHROPIC_API_KEY"] == "***REDACTED***"
    assert masked["JIRA_XRAY_API_KEY"] == "***REDACTED***"
    assert masked["PATH"] == "/usr/bin"
