"""Tests for config module — resource-root override + Anthropic auth dispatch."""

from __future__ import annotations

from pathlib import Path

from worca_t.config import (
    anthropic_auth_kwargs,
    anthropic_vertex_kwargs,
    package_resource_root,
    use_vertex_backend,
)


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


# ---------------------------------------------------------------------------
# use_vertex_backend + anthropic_vertex_kwargs — Vertex AI / model-farm routing
# ---------------------------------------------------------------------------


def test_use_vertex_backend_via_claude_code_use_vertex(monkeypatch):
    """CLAUDE_CODE_USE_VERTEX=1 alone selects the Vertex client."""
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.delenv("ANTHROPIC_VERTEX_BASE_URL", raising=False)
    assert use_vertex_backend() is True


def test_use_vertex_backend_via_vertex_base_url(monkeypatch):
    """ANTHROPIC_VERTEX_BASE_URL alone (no explicit opt-in) selects Vertex too."""
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://farm.example/v1")
    assert use_vertex_backend() is True


def test_use_vertex_backend_false_when_neither_signal_set(monkeypatch):
    """Standard Anthropic path is the default when no Vertex signals are set."""
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_BASE_URL", raising=False)
    assert use_vertex_backend() is False


def test_use_vertex_backend_ignores_non_1_value(monkeypatch):
    """Only exactly "1" enables Vertex via CLAUDE_CODE_USE_VERTEX — match CLI."""
    monkeypatch.delenv("ANTHROPIC_VERTEX_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "true")
    assert use_vertex_backend() is False
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "0")
    assert use_vertex_backend() is False


def test_anthropic_vertex_kwargs_bosch_farm_shape(monkeypatch):
    """Exercise the Bosch model farm env shape exactly."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://aoai-farm.bosch-temp.com/api/google/v1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "farm-token-xyz")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "_")
    monkeypatch.setenv("CLOUD_ML_REGION", "_")
    assert anthropic_vertex_kwargs() == {
        "base_url": "https://aoai-farm.bosch-temp.com/api/google/v1",
        "access_token": "farm-token-xyz",
        "project_id": "_",
        "region": "_",
    }


def test_anthropic_vertex_kwargs_default_region_when_proxy_base_url(monkeypatch):
    """Custom base_url but no CLOUD_ML_REGION → fall back to placeholder region."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://farm.example/v1")
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    kwargs = anthropic_vertex_kwargs()
    assert kwargs.get("base_url") == "https://farm.example/v1"
    assert kwargs.get("region") == "us-east5"  # placeholder for proxy setups


def test_anthropic_vertex_kwargs_omits_unset_fields(monkeypatch):
    """Unset env vars → corresponding kwargs omitted (SDK fills defaults)."""
    monkeypatch.delenv("ANTHROPIC_VERTEX_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("CLOUD_ML_REGION", raising=False)
    assert anthropic_vertex_kwargs() == {}
