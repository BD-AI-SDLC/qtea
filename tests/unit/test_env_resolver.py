"""Tests for env_resolver — AzDO URL construction safety + essential-key HITL."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_azdo_url_encodes_special_chars():
    """org, project, and group with special characters must be percent-encoded."""
    from worca_t.env_resolver import AzureDevOpsStrategy

    strategy = AzureDevOpsStrategy(
        org="my org",
        project="proj/ect",
        variable_group="group&inject=1",
        pat="fake-pat",
    )

    captured_url: str | None = None

    def mock_urlopen(request, *, timeout=None):
        nonlocal captured_url
        captured_url = request.full_url
        raise OSError("mocked")

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        strategy._fetch_variables()

    assert captured_url is not None
    assert "my%20org" in captured_url
    assert "proj%2Fect" in captured_url
    assert "group%26inject%3D1" in captured_url
    assert "&inject=1" not in captured_url.split("?", 1)[-1].replace("group%26inject%3D1", "")


# ---------------------------------------------------------------------------
# Essential-key classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "BASE_URL", "QA_URL", "API_URL", "SUT_BASE_URL", "DATABASE_URL",
    "USER", "USERNAME", "SSO_USER", "EMAIL", "LOGIN",
    "PASSWORD", "APP_PASSWORD", "BROWSER_PASSWORD",
    "HOST", "ENDPOINT",
])
def test_is_essential_key_recognises_runtime_creds_and_endpoints(key):
    from worca_t.env_resolver import _is_essential_key
    assert _is_essential_key(key), f"{key!r} should be essential"


@pytest.mark.parametrize("key", [
    "TIMEOUT", "RETRY_COUNT", "BROWSER_NAME", "HEADLESS",
    "WORKERS", "PARALLEL", "LOG_LEVEL", "DEBUG", "ENV",
    "VIEWPORT_WIDTH", "DEVICE_SCALE",
])
def test_is_essential_key_rejects_infrastructure_keys(key):
    from worca_t.env_resolver import _is_essential_key
    assert not _is_essential_key(key), f"{key!r} should not be essential"


# ---------------------------------------------------------------------------
# resolve_sut_env interactive scope
# ---------------------------------------------------------------------------


def test_resolve_sut_env_prompts_only_for_essentials(tmp_path: Path, monkeypatch):
    """Interactive prompt must fire for essential keys (BASE_URL, USERNAME,
    PASSWORD) and skip infrastructure keys (TIMEOUT, BROWSER_NAME) even
    when they're listed in .env.example.
    """
    from worca_t import env_resolver
    from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

    (tmp_path / ".env.example").write_text(
        "BASE_URL=\nUSERNAME=\nPASSWORD=\nTIMEOUT=\nBROWSER_NAME=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    captured: dict = {}

    class FakeStrategy(env_resolver.InteractivePromptStrategy):
        def __init__(self, defaults=None):
            super().__init__(defaults)
            captured["init_defaults"] = dict(self._defaults)

        def resolve(self, keys, already_resolved):
            captured["keys"] = list(keys)
            return {}

    monkeypatch.setattr(env_resolver, "InteractivePromptStrategy", FakeStrategy)

    cfg = EnvResolverConfig(env_file=None, sut_path=tmp_path, no_hitl=False)
    resolve_sut_env(
        cfg,
        ["BASE_URL", "USERNAME", "PASSWORD", "TIMEOUT", "BROWSER_NAME"],
        tmp_path,
    )

    assert set(captured["keys"]) == {"BASE_URL", "USERNAME", "PASSWORD"}
    assert "TIMEOUT" not in captured["keys"]
    assert "BROWSER_NAME" not in captured["keys"]


def test_resolve_sut_env_passes_discovered_value_as_default(tmp_path: Path, monkeypatch):
    """When a value is already discovered (e.g. from .env), it must be passed
    to the interactive strategy as the default so the user can confirm with
    Enter or override by typing.
    """
    from worca_t import env_resolver
    from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

    (tmp_path / ".env").write_text("BASE_URL=https://staging.example.com\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    captured: dict = {}

    class FakeStrategy(env_resolver.InteractivePromptStrategy):
        def __init__(self, defaults=None):
            super().__init__(defaults)
            captured["defaults"] = dict(self._defaults)

        def resolve(self, keys, already_resolved):
            return {}

    monkeypatch.setattr(env_resolver, "InteractivePromptStrategy", FakeStrategy)

    cfg = EnvResolverConfig(env_file=None, sut_path=tmp_path, no_hitl=False)
    resolve_sut_env(cfg, ["BASE_URL"], tmp_path)

    assert captured["defaults"] == {"BASE_URL": "https://staging.example.com"}


def test_resolve_sut_env_no_hitl_skips_interactive(tmp_path: Path, monkeypatch):
    """`--no-hitl` must suppress the interactive prompt even for essentials."""
    from worca_t import env_resolver
    from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

    called = False

    class FakeStrategy(env_resolver.InteractivePromptStrategy):
        def resolve(self, keys, already_resolved):
            nonlocal called
            called = True
            return {}

    monkeypatch.setattr(env_resolver, "InteractivePromptStrategy", FakeStrategy)
    cfg = EnvResolverConfig(env_file=None, sut_path=tmp_path, no_hitl=True)
    resolve_sut_env(cfg, ["BASE_URL", "PASSWORD"], tmp_path)
    assert not called


def test_resolve_sut_env_interactive_override_changes_source(tmp_path: Path, monkeypatch):
    """If the user supplies a value through the prompt that differs from the
    silently-discovered one, the source should be relabelled 'interactive'.
    """
    from worca_t import env_resolver
    from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

    (tmp_path / ".env").write_text("BASE_URL=https://old.example.com\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    class FakeStrategy(env_resolver.InteractivePromptStrategy):
        def resolve(self, keys, already_resolved):
            return {"BASE_URL": "https://new.example.com"}

    monkeypatch.setattr(env_resolver, "InteractivePromptStrategy", FakeStrategy)
    cfg = EnvResolverConfig(env_file=None, sut_path=tmp_path, no_hitl=False)
    result = resolve_sut_env(cfg, ["BASE_URL"], tmp_path)

    assert result.values["BASE_URL"] == "https://new.example.com"
    assert result.sources["BASE_URL"] == "interactive"
