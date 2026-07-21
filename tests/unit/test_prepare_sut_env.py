"""Tests for the hoisted SUT env preparation (test_runner.prepare_sut_env)
and the pre-Step-7 install-signature marker consumed by Step 9's fast-path.

prepare_sut + execute_command are mocked — no real package-manager subprocess.
"""

from __future__ import annotations

from qtea import test_runner
from qtea.stack_profile import StackProfile
from qtea.test_runner import (
    PrepareResult,
    prepare_sut_env,
    read_env_prep_marker,
    write_env_prep_marker,
)

# --- marker round-trip ------------------------------------------------------


def test_marker_roundtrip(tmp_path):
    write_env_prep_marker(tmp_path, "sig-abc123")
    assert read_env_prep_marker(tmp_path) == "sig-abc123"


def test_marker_absent_returns_none(tmp_path):
    assert read_env_prep_marker(tmp_path) is None


def test_marker_write_none_is_noop(tmp_path):
    write_env_prep_marker(tmp_path, None)
    assert read_env_prep_marker(tmp_path) is None


# --- prepare_sut_env --------------------------------------------------------


def _patch_prepare(monkeypatch, result: PrepareResult):
    monkeypatch.setattr(
        test_runner, "prepare_sut",
        lambda profile, *, cwd, timeout_s: result,
    )


def test_install_failure_returns_not_ok(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch, PrepareResult(
        ran=True, command="npm ci", exit_code=1, duration_s=1.0,
        stdout="", stderr="boom",
    ))
    res = prepare_sut_env(
        StackProfile(install_command="npm ci", package_manager="npm"),
        cwd=tmp_path, framework="playwright-ts",
        install_log_path=tmp_path / "install.log",
    )
    assert res.ok is False
    assert "install failed" in (res.error or "")


def test_install_failure_but_env_present_proceeds(monkeypatch, tmp_path):
    """A strict `npm ci` failing on a drifted lockfile must NOT block the
    best-effort prewarm when a usable node_modules/playwright already exists."""
    _patch_prepare(monkeypatch, PrepareResult(
        ran=True, command="npm ci", exit_code=1, duration_s=1.0,
        stdout="", stderr="lockfile out of sync",
    ))
    (tmp_path / "node_modules" / "playwright").mkdir(parents=True)
    monkeypatch.setattr(
        test_runner, "execute_command", lambda *a, **k: (0, "", "", 1.0),
    )
    res = prepare_sut_env(
        StackProfile(install_command="npm ci", package_manager="npm"),
        cwd=tmp_path, framework="playwright-ts",
    )
    assert res.ok is True  # proceeded on the existing env


def test_success_playwright_installs_browser(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch, PrepareResult(
        ran=True, command="npm ci", exit_code=0, duration_s=1.0,
    ))
    calls: list[str] = []

    def fake_exec(cmd, *, cwd, timeout_s, env_extra=None, isolate_venv=False):
        calls.append(cmd)
        return (0, "ok", "", 1.0)

    monkeypatch.setattr(test_runner, "execute_command", fake_exec)
    res = prepare_sut_env(
        StackProfile(install_command="npm ci", package_manager="npm"),
        cwd=tmp_path, framework="playwright-ts",
    )
    assert res.ok is True
    assert any("playwright install chromium" in c for c in calls)


def test_success_non_playwright_skips_browser(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch, PrepareResult(
        ran=True, command="poetry install", exit_code=0, duration_s=1.0,
    ))
    calls: list = []
    monkeypatch.setattr(
        test_runner, "execute_command",
        lambda *a, **k: (calls.append(a), (0, "", "", 1.0))[1],
    )
    res = prepare_sut_env(
        StackProfile(install_command="poetry install", package_manager="poetry"),
        cwd=tmp_path, framework="pytest",
    )
    assert res.ok is True
    assert calls == []  # no browser install for a non-Playwright framework


def test_venv_swap_when_venv_present(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch, PrepareResult(
        ran=True, command="poetry install", exit_code=0, duration_s=1.0,
    ))
    # Simulate a created venv so the wrapper swap fires.
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    monkeypatch.setattr(
        test_runner, "execute_command", lambda *a, **k: (0, "", "", 1.0),
    )
    res = prepare_sut_env(
        StackProfile(
            install_command="poetry install", package_manager="poetry",
            venv_path=".venv",
        ),
        cwd=tmp_path, framework="playwright-py",
    )
    assert res.ok is True
    # After swap the profile targets the venv bin dir directly via pip.
    assert res.stack_profile.package_manager == "pip"
    assert res.stack_profile.wrapper_prefix is not None
