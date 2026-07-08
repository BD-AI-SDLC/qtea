"""Tests for the ``qtea auth-capture`` Use case A capture path.

Mocks the SUT venv subprocess (no real Playwright spawn) and exercises
the inventory-loading, wrapper-script generation, and error paths.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from qtea.auth_capture import (
    DEFAULT_OUTPUT_REL,
    AuthFlowSpec,
    _active_module,
    _build_auth_flow_spec,
    _find_sut_inventory,
    _resolve_sut_python,
    _wrapper_script,
    cmd_auth_capture,
)

# ---------------------------------------------------------------------------
# _find_sut_inventory + _active_module
# ---------------------------------------------------------------------------


def _make_sut_with_inventory(tmp_path: Path, inventory: dict) -> Path:
    """Create a fake SUT with the canonical .qtea/sut_inventory.json."""
    sut = tmp_path / "sut"
    (sut / ".qtea").mkdir(parents=True)
    (sut / ".qtea" / "sut_inventory.json").write_text(
        json.dumps(inventory), encoding="utf-8",
    )
    return sut


def test_find_sut_inventory_canonical_path(tmp_path):
    sut = _make_sut_with_inventory(tmp_path, {"active_module": "frontend"})
    found = _find_sut_inventory(sut)
    assert found == {"active_module": "frontend"}


def test_find_sut_inventory_returns_none_when_missing(tmp_path):
    sut = tmp_path / "empty-sut"
    sut.mkdir()
    assert _find_sut_inventory(sut) is None


def test_active_module_resolves_by_name():
    inventory = {
        "active_module": "frontend",
        "modules": [
            {"name": "backend", "language": "python"},
            {"name": "frontend", "language": "python", "auth_flow": {"type": "sso"}},
        ],
    }
    active = _active_module(inventory)
    assert active["name"] == "frontend"


def test_active_module_returns_none_when_unset_or_mismatched():
    assert _active_module({"modules": []}) is None
    assert _active_module({"active_module": "ghost", "modules": []}) is None


# ---------------------------------------------------------------------------
# _build_auth_flow_spec
# ---------------------------------------------------------------------------


def test_build_auth_flow_spec_extracts_required_fields():
    module = {
        "language": "python",
        "auth_flow": {
            "entry_method": "tests/fixtures/auth.py:sign_in",
            "fixture_entry": "tests/fixtures/auth.py:authed_page",
            "credentials_env_vars": ["BASIC_SSO_USER", "BASIC_PROFILE_PASSWORD"],
        },
    }
    spec = _build_auth_flow_spec(module)
    assert spec.entry_method == "tests/fixtures/auth.py:sign_in"
    assert spec.fixture_entry == "tests/fixtures/auth.py:authed_page"
    assert spec.credentials_env_vars == ("BASIC_SSO_USER", "BASIC_PROFILE_PASSWORD")
    assert spec.language == "python"


def test_build_auth_flow_spec_rejects_missing_entry_method():
    with pytest.raises(ValueError, match="entry_method must be set"):
        _build_auth_flow_spec({"language": "python", "auth_flow": {}})


def test_build_auth_flow_spec_rejects_malformed_entry_method():
    with pytest.raises(ValueError, match="entry_method must be set"):
        _build_auth_flow_spec(
            {"language": "python", "auth_flow": {"entry_method": "no-colon-here"}},
        )


# ---------------------------------------------------------------------------
# _resolve_sut_python
# ---------------------------------------------------------------------------


def test_resolve_sut_python_finds_windows_venv(tmp_path):
    sut = tmp_path / "sut"
    venv_python = sut / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    assert _resolve_sut_python(sut) == venv_python


def test_resolve_sut_python_finds_posix_venv(tmp_path):
    sut = tmp_path / "sut"
    venv_python = sut / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    assert _resolve_sut_python(sut) == venv_python


def test_resolve_sut_python_raises_when_missing(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    with pytest.raises(FileNotFoundError, match="No venv Python found"):
        _resolve_sut_python(sut)


# ---------------------------------------------------------------------------
# _wrapper_script — content correctness
# ---------------------------------------------------------------------------


def test_wrapper_script_contains_required_calls(tmp_path):
    spec = AuthFlowSpec(
        entry_method="tests/fixtures/auth.py:sign_in",
        fixture_entry=None,
        credentials_env_vars=(),
        language="python",
    )
    output = tmp_path / "out.json"
    sut_root = tmp_path / "sut"
    sut_root.mkdir()
    src = _wrapper_script(spec, output, sut_root, headed=True)
    assert "from playwright.sync_api import sync_playwright" in src
    assert "p.chromium.launch(headless=False)" in src
    assert "context.storage_state" in src or ".storage_state(" in src
    # The symbol from entry_method ends up referenced via getattr.
    assert "'sign_in'" in src


def test_wrapper_script_headless_mode(tmp_path):
    spec = AuthFlowSpec(
        entry_method="x.py:y",
        fixture_entry=None,
        credentials_env_vars=(),
        language="python",
    )
    src = _wrapper_script(spec, tmp_path / "o.json", tmp_path, headed=False)
    assert "launch(headless=True)" in src


# ---------------------------------------------------------------------------
# cmd_auth_capture — end-to-end with subprocess mocked
# ---------------------------------------------------------------------------


def _seed_sut_for_capture(
    tmp_path: Path,
    *,
    language: str = "python",
    entry_method: str = "tests/fixtures/auth.py:sign_in",
) -> Path:
    """Materialize a fake SUT layout: inventory + venv stub + sign-in helper."""
    sut = tmp_path / "sut"
    (sut / ".qtea").mkdir(parents=True)
    inventory = {
        "active_module": "frontend",
        "modules": [{
            "name": "frontend",
            "path": ".",
            "language": language,
            "auth_flow": {
                "entry_method": entry_method,
                "credentials_env_vars": [],
            },
        }],
    }
    (sut / ".qtea" / "sut_inventory.json").write_text(
        json.dumps(inventory), encoding="utf-8",
    )
    # Fake venv python (windows or posix layout — pick by current OS).
    if os.name == "nt":
        py = sut / ".venv" / "Scripts" / "python.exe"
    else:
        py = sut / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    # Sign-in helper at the path entry_method points to.
    helper_rel = entry_method.split(":", 1)[0]
    helper_abs = sut / helper_rel
    helper_abs.parent.mkdir(parents=True, exist_ok=True)
    helper_abs.write_text("def sign_in(context):\n    pass\n", encoding="utf-8")
    return sut


def test_cmd_auth_capture_writes_output_at_default_path(tmp_path, monkeypatch):
    sut = _seed_sut_for_capture(tmp_path)

    # Fake the subprocess: write the expected output file then exit 0.
    expected_output = sut / DEFAULT_OUTPUT_REL

    def fake_run(argv, **kwargs):
        expected_output.parent.mkdir(parents=True, exist_ok=True)
        expected_output.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("qtea.auth_capture.subprocess.run", fake_run)

    out = cmd_auth_capture(sut=sut, output=None, headed=True)
    assert out.resolve() == expected_output.resolve()
    assert out.is_file()


def test_cmd_auth_capture_honors_explicit_output(tmp_path, monkeypatch):
    sut = _seed_sut_for_capture(tmp_path)
    explicit = tmp_path / "elsewhere" / "state.json"

    def fake_run(argv, **kwargs):
        explicit.parent.mkdir(parents=True, exist_ok=True)
        explicit.write_text('{}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("qtea.auth_capture.subprocess.run", fake_run)

    out = cmd_auth_capture(sut=sut, output=explicit, headed=True)
    assert out.resolve() == explicit.resolve()


def test_cmd_auth_capture_raises_when_no_inventory(tmp_path):
    sut = tmp_path / "sut-no-inventory"
    sut.mkdir()
    with pytest.raises(FileNotFoundError, match=r"No sut_inventory\.json"):
        cmd_auth_capture(sut=sut)


def test_cmd_auth_capture_raises_when_unsupported_language(tmp_path):
    # Python and Node.js (JS/TS) Playwright SUTs are supported; other stacks
    # (e.g. Java/C#) raise NotImplementedError since storageState is
    # Playwright-specific.
    sut = _seed_sut_for_capture(tmp_path, language="java")
    with pytest.raises(NotImplementedError, match=r"Python and Node\.js"):
        cmd_auth_capture(sut=sut)


def test_cmd_auth_capture_raises_when_subprocess_fails(tmp_path, monkeypatch):
    sut = _seed_sut_for_capture(tmp_path)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, returncode=2, stdout="",
            stderr="Traceback...\nE   ModuleNotFoundError: playwright",
        )

    monkeypatch.setattr("qtea.auth_capture.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="exit code 2"):
        cmd_auth_capture(sut=sut)


def test_cmd_auth_capture_raises_when_no_file_written(tmp_path, monkeypatch):
    """Subprocess returns 0 but didn't actually call storage_state(path=...).
    We must catch this rather than reporting false success."""
    sut = _seed_sut_for_capture(tmp_path)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("qtea.auth_capture.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="no file was written"):
        cmd_auth_capture(sut=sut)


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only chmod check")
def test_cmd_auth_capture_sets_owner_only_perms_on_posix(tmp_path, monkeypatch):
    sut = _seed_sut_for_capture(tmp_path)
    expected = sut / DEFAULT_OUTPUT_REL

    def fake_run(argv, **kwargs):
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text('{}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("qtea.auth_capture.subprocess.run", fake_run)

    out = cmd_auth_capture(sut=sut)
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600
