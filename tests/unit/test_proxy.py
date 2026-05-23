"""Proxy detection and propagation tests."""

from __future__ import annotations

from worca_t.proxy import detected_proxies, mask_secrets, with_proxy_env


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
