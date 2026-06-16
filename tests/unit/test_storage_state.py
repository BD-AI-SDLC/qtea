"""Tests for the Playwright storage-state handover helpers.

Exercises the 4-tier resolution, MCP arg formatting, heal-prompt
summary, and log-path masking. Pure helpers — no Playwright import,
no subprocess.
"""

from __future__ import annotations

from pathlib import Path

from worca_t.storage_state import (
    mask_path,
    resolve,
    summary_for_prompt,
    to_mcp_arg,
)


# ---------------------------------------------------------------------------
# resolve() — 4-tier precedence
# ---------------------------------------------------------------------------


def _make_file(path: Path, content: str = '{"cookies": [], "origins": []}') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_resolve_cli_opt_wins_over_env_and_convention(tmp_path):
    """Explicit --storage-state flag is the operator override; nothing
    should beat it."""
    sut = tmp_path / "sut"
    ws = tmp_path / "ws"
    sut.mkdir()
    ws.mkdir()
    cli_path = _make_file(tmp_path / "explicit.json")
    _make_file(sut / ".worca-t" / "storage-state.json", '{"src":"convention"}')
    _make_file(ws / "storage-state.json", '{"src":"workspace"}')
    env_path = _make_file(tmp_path / "env.json")

    result = resolve(
        sut_root=sut,
        workspace_root=ws,
        cli_opt=cli_path,
        env={"WORCA_T_STORAGE_STATE": str(env_path)},
    )
    assert result == cli_path


def test_resolve_env_wins_over_convention(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _make_file(sut / ".worca-t" / "storage-state.json", '{"src":"convention"}')
    env_path = _make_file(tmp_path / "env.json")

    result = resolve(
        sut_root=sut,
        workspace_root=None,
        cli_opt=None,
        env={"WORCA_T_STORAGE_STATE": str(env_path)},
    )
    assert result == env_path


def test_resolve_convention_path_in_sut(tmp_path):
    """No CLI, no env → SUT convention path wins over workspace."""
    sut = tmp_path / "sut"
    ws = tmp_path / "ws"
    sut.mkdir()
    ws.mkdir()
    sut_path = _make_file(sut / ".worca-t" / "storage-state.json", '{"src":"sut"}')
    _make_file(ws / "storage-state.json", '{"src":"workspace"}')

    result = resolve(sut_root=sut, workspace_root=ws, cli_opt=None, env={})
    assert result == sut_path


def test_resolve_workspace_path_when_sut_path_missing(tmp_path):
    """No CLI, no env, no SUT-side file → workspace auto-capture wins."""
    sut = tmp_path / "sut"
    ws = tmp_path / "ws"
    sut.mkdir()
    ws.mkdir()
    ws_path = _make_file(ws / "storage-state.json", '{"src":"workspace"}')

    result = resolve(sut_root=sut, workspace_root=ws, cli_opt=None, env={})
    assert result == ws_path


def test_resolve_returns_none_when_no_source_has_file(tmp_path):
    sut = tmp_path / "sut"
    ws = tmp_path / "ws"
    sut.mkdir()
    ws.mkdir()
    # Convention paths exist as directories but not as files.
    result = resolve(sut_root=sut, workspace_root=ws, cli_opt=None, env={})
    assert result is None


def test_resolve_env_path_missing_file_falls_through(tmp_path):
    """An env var pointing at a missing file does NOT short-circuit —
    we fall through to the next source. Keeps stale env vars from
    masking a valid SUT-side capture."""
    sut = tmp_path / "sut"
    sut.mkdir()
    sut_path = _make_file(sut / ".worca-t" / "storage-state.json")
    result = resolve(
        sut_root=sut,
        workspace_root=None,
        cli_opt=None,
        env={"WORCA_T_STORAGE_STATE": str(tmp_path / "missing.json")},
    )
    assert result == sut_path


def test_resolve_uses_os_environ_when_env_dict_is_none(tmp_path, monkeypatch):
    """When `env=None`, the resolver falls back to `os.environ` for the
    WORCA_T_STORAGE_STATE lookup."""
    env_path = _make_file(tmp_path / "env.json")
    monkeypatch.setenv("WORCA_T_STORAGE_STATE", str(env_path))
    result = resolve(sut_root=None, workspace_root=None, cli_opt=None, env=None)
    assert result == env_path


# ---------------------------------------------------------------------------
# to_mcp_arg()
# ---------------------------------------------------------------------------


def test_to_mcp_arg_set_returns_storage_state_flag(tmp_path):
    p = _make_file(tmp_path / "s.json")
    arg = to_mcp_arg(p)
    assert arg.startswith("--storage-state=")
    # Absolute path is forced so the MCP subprocess can resolve it from any cwd.
    assert Path(arg.split("=", 1)[1]).is_absolute()


def test_to_mcp_arg_unset_returns_empty_string():
    assert to_mcp_arg(None) == ""


# ---------------------------------------------------------------------------
# summary_for_prompt()
# ---------------------------------------------------------------------------


def test_summary_for_prompt_includes_path_and_mtime_and_directive(tmp_path):
    p = _make_file(tmp_path / "s.json")
    summary = summary_for_prompt(p)
    assert "PRE-LOADED STORAGE STATE" in summary
    assert "browser_navigate" in summary
    assert "DO NOT call the SUT's sign-in helper" in summary
    # mtime stamped (ISO-formatted UTC time)
    assert "T" in summary  # ISO timestamp marker
    # Stale-fallback directive present (so the agent doesn't abort on a
    # legitimate login-page test).
    assert "auth-replay" in summary or "fallback" in summary


def test_summary_for_prompt_empty_when_unset():
    assert summary_for_prompt(None) == ""


def test_summary_for_prompt_handles_missing_file_gracefully(tmp_path):
    """A path that no longer points at a real file should not crash the
    prompt builder — degrade to 'unknown' mtime, keep the directive."""
    summary = summary_for_prompt(tmp_path / "ghost.json")
    assert "unknown" in summary
    assert "browser_navigate" in summary


# ---------------------------------------------------------------------------
# mask_path()
# ---------------------------------------------------------------------------


def test_mask_path_collapses_sut_convention(tmp_path):
    p = tmp_path / "some" / "deep" / "sut" / ".worca-t" / "storage-state.json"
    assert mask_path(p) == "<sut>/.worca-t/storage-state.json"


def test_mask_path_collapses_workspace_convention(tmp_path):
    p = tmp_path / "ws-id" / "storage-state.json"
    assert mask_path(p) == "<workspace>/storage-state.json"


def test_mask_path_falls_back_to_basename(tmp_path):
    p = tmp_path / "explicit-flag-target.json"
    assert mask_path(p) == "explicit-flag-target.json"
