"""Tests for config module — focused on the resource-root override."""

from __future__ import annotations

from pathlib import Path

from worca_t.config import package_resource_root


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
