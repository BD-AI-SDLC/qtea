"""Tests for config module — resource-root override + Anthropic auth dispatch."""

from __future__ import annotations

from pathlib import Path

from worca_t.config import anthropic_auth_kwargs, package_resource_root


def test_package_resource_root_respects_env_override(tmp_path: Path, monkeypatch):
    custom = tmp_path / "custom-resources"
    custom.mkdir()
    monkeypatch.setenv("WORCA_T_RESOURCE_ROOT", str(custom))
    assert package_resource_root() == custom


def test_package_resource_root_ignores_missing_override_path(tmp_path: Path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("WORCA_T_RESOURCE_ROOT", str(missing))
    # Falls back to the next candidate (either _resources or dev tree) — both
    # are valid in this test environment. Just assert we got *something* and
    # that it is not the missing path.
    result = package_resource_root()
    assert result.exists()
    assert result != missing


def test_package_resource_root_no_override_uses_fallback(monkeypatch):
    monkeypatch.delenv("WORCA_T_RESOURCE_ROOT", raising=False)
    result = package_resource_root()
    assert result.exists()


# ---------------------------------------------------------------------------
# anthropic_auth_kwargs — Bearer (model farm) vs x-api-key (raw API) dispatch
# ---------------------------------------------------------------------------


def test_anthropic_auth_kwargs_prefers_auth_token(monkeypatch):
    """When ANTHROPIC_AUTH_TOKEN is set, return auth_token= (Bearer header).

    This is the model-farm path: a proxy that fronts the Anthropic API and
    expects ``Authorization: Bearer <token>``. The token is NOT a raw
    Anthropic API key and must NOT be sent as x-api-key.
    """
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "farm-bearer-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_auth_kwargs() == {"auth_token": "farm-bearer-xyz"}


def test_anthropic_auth_kwargs_falls_back_to_api_key(monkeypatch):
    """When only ANTHROPIC_API_KEY is set, return api_key= (x-api-key header)."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    assert anthropic_auth_kwargs() == {"api_key": "sk-ant-abc"}


def test_anthropic_auth_kwargs_auth_token_wins_when_both_set(monkeypatch):
    """When both are set, AUTH_TOKEN takes precedence — same as the claude CLI."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "farm-bearer-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    assert anthropic_auth_kwargs() == {"auth_token": "farm-bearer-xyz"}


def test_anthropic_auth_kwargs_returns_empty_when_neither_set(monkeypatch):
    """No env vars set → empty dict; let the SDK raise its own clear error."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert anthropic_auth_kwargs() == {}


def test_anthropic_auth_kwargs_treats_empty_string_as_unset(monkeypatch):
    """Empty AUTH_TOKEN should fall through to API_KEY, not return empty Bearer."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    # Current behaviour: empty string is falsy → falls through to api_key.
    assert anthropic_auth_kwargs() == {"api_key": "sk-ant-abc"}
