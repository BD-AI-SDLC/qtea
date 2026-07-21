"""Tests for the Playwright storage-state handover helpers.

Exercises the 4-tier resolution, MCP arg formatting, heal-prompt
summary, and log-path masking. Pure helpers — no Playwright import,
no subprocess.
"""

from __future__ import annotations

from pathlib import Path

from qtea.storage_state import (
    ensure_gitignored,
    mask_path,
    mcp_browser_env,
    resolve,
    summary_for_prompt,
    to_mcp_arg,
    write_target,
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
    _make_file(sut / ".qtea" / "storage-state.json", '{"src":"convention"}')
    _make_file(ws / "storage-state.json", '{"src":"workspace"}')
    env_path = _make_file(tmp_path / "env.json")

    result = resolve(
        sut_root=sut,
        workspace_root=ws,
        cli_opt=cli_path,
        env={"QTEA_STORAGE_STATE": str(env_path)},
    )
    assert result == cli_path


def test_resolve_env_wins_over_convention(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _make_file(sut / ".qtea" / "storage-state.json", '{"src":"convention"}')
    env_path = _make_file(tmp_path / "env.json")

    result = resolve(
        sut_root=sut,
        workspace_root=None,
        cli_opt=None,
        env={"QTEA_STORAGE_STATE": str(env_path)},
    )
    assert result == env_path


def test_resolve_convention_path_in_sut(tmp_path):
    """No CLI, no env → SUT convention path wins over workspace."""
    sut = tmp_path / "sut"
    ws = tmp_path / "ws"
    sut.mkdir()
    ws.mkdir()
    sut_path = _make_file(sut / ".qtea" / "storage-state.json", '{"src":"sut"}')
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
    sut_path = _make_file(sut / ".qtea" / "storage-state.json")
    result = resolve(
        sut_root=sut,
        workspace_root=None,
        cli_opt=None,
        env={"QTEA_STORAGE_STATE": str(tmp_path / "missing.json")},
    )
    assert result == sut_path


def test_resolve_uses_os_environ_when_env_dict_is_none(tmp_path, monkeypatch):
    """When `env=None`, the resolver falls back to `os.environ` for the
    QTEA_STORAGE_STATE lookup."""
    env_path = _make_file(tmp_path / "env.json")
    monkeypatch.setenv("QTEA_STORAGE_STATE", str(env_path))
    result = resolve(sut_root=None, workspace_root=None, cli_opt=None, env=None)
    assert result == env_path


# ---------------------------------------------------------------------------
# write_target() — where a fresh capture is WRITTEN (existence-independent)
# ---------------------------------------------------------------------------


def test_write_target_cli_opt_wins(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    cli_path = tmp_path / "explicit.json"  # does NOT exist yet
    result = write_target(
        sut_root=sut,
        cli_opt=cli_path,
        env={"QTEA_STORAGE_STATE": str(tmp_path / "env.json")},
    )
    assert result == cli_path


def test_write_target_env_wins_over_convention(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    env_path = tmp_path / "env.json"  # does NOT exist yet
    result = write_target(
        sut_root=sut, cli_opt=None, env={"QTEA_STORAGE_STATE": str(env_path)}
    )
    assert result == env_path


def test_write_target_falls_back_to_convention(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    result = write_target(sut_root=sut, cli_opt=None, env={})
    assert result == sut / ".qtea" / "storage-state.json"


def test_write_target_is_existence_independent(tmp_path):
    """Unlike resolve(), write_target returns the override even when nothing
    exists on disk yet — the first capture must have somewhere to land."""
    target = tmp_path / "nowhere" / "s.json"
    assert not target.exists()
    assert write_target(sut_root=tmp_path, cli_opt=target, env={}) == target


def test_write_target_raises_without_override_or_sut():
    import pytest

    with pytest.raises(ValueError):
        write_target(sut_root=None, cli_opt=None, env={})


# ---------------------------------------------------------------------------
# ensure_gitignored()
# ---------------------------------------------------------------------------


def test_ensure_gitignored_adds_custom_path_inside_sut(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    target = sut / "auth" / "session.json"
    ensure_gitignored(sut, target)
    assert (sut / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        "auth/session.json"
    ]


def test_ensure_gitignored_is_noop_outside_sut(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    outside = tmp_path / "home" / "s.json"
    ensure_gitignored(sut, outside)
    assert not (sut / ".gitignore").exists()


def test_ensure_gitignored_noop_when_sut_root_none(tmp_path):
    # Must not raise.
    ensure_gitignored(None, tmp_path / "s.json")


def test_ensure_gitignored_is_idempotent(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    target = sut / "auth" / "session.json"
    ensure_gitignored(sut, target)
    ensure_gitignored(sut, target)
    lines = (sut / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count("auth/session.json") == 1


def test_ensure_gitignored_skips_when_dir_prefix_already_covers(tmp_path):
    """The Step-6-seeded `.qtea/` directory entry already covers the default
    convention path, so we must not append a redundant line."""
    sut = tmp_path / "sut"
    sut.mkdir()
    (sut / ".gitignore").write_text(".qtea/\n", encoding="utf-8")
    ensure_gitignored(sut, sut / ".qtea" / "storage-state.json")
    assert (sut / ".gitignore").read_text(encoding="utf-8") == ".qtea/\n"


def test_ensure_gitignored_skips_when_basename_pattern_already_covers(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    (sut / ".gitignore").write_text("storage-state.json\n", encoding="utf-8")
    ensure_gitignored(sut, sut / ".qtea" / "storage-state.json")
    assert (sut / ".gitignore").read_text(encoding="utf-8") == "storage-state.json\n"


def test_ensure_gitignored_appends_with_newline_when_missing(tmp_path):
    sut = tmp_path / "sut"
    sut.mkdir()
    (sut / ".gitignore").write_text("node_modules/", encoding="utf-8")  # no trailing \n
    ensure_gitignored(sut, sut / "auth" / "session.json")
    assert (sut / ".gitignore").read_text(encoding="utf-8") == (
        "node_modules/\nauth/session.json\n"
    )


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
# mcp_browser_env()
# ---------------------------------------------------------------------------


def test_mcp_browser_env_with_session_is_isolated_no_user_data_dir(tmp_path):
    """A resolved session must run isolated + storage-state and DROP
    --user-data-dir (a persistent profile makes @playwright/mcp ignore
    --storage-state -- observed as auth-gated routes silently redirecting to
    login when both flags were passed)."""
    p = _make_file(tmp_path / "s.json")
    env = mcp_browser_env(p, tmp_path / "profile")
    assert env["QTEA_MCP_ISOLATED_ARG"] == "--isolated"
    assert env["QTEA_STORAGE_STATE_ARG"].startswith("--storage-state=")
    # user-data-dir dropped (empty → filtered out before the subprocess spawns)
    assert env["QTEA_MCP_USER_DATA_DIR_ARG"] == ""


def test_mcp_browser_env_without_session_uses_persistent_profile(tmp_path):
    """No session → persistent --user-data-dir, no --isolated, no
    --storage-state."""
    env = mcp_browser_env(None, tmp_path / "profile")
    assert env["QTEA_MCP_ISOLATED_ARG"] == ""
    assert env["QTEA_STORAGE_STATE_ARG"] == ""
    assert env["QTEA_MCP_USER_DATA_DIR_ARG"].startswith("--user-data-dir=")
    assert str(tmp_path / "profile") in env["QTEA_MCP_USER_DATA_DIR_ARG"]


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
    p = tmp_path / "some" / "deep" / "sut" / ".qtea" / "storage-state.json"
    assert mask_path(p) == "<sut>/.qtea/storage-state.json"


def test_mask_path_collapses_workspace_convention(tmp_path):
    p = tmp_path / "ws-id" / "storage-state.json"
    assert mask_path(p) == "<workspace>/storage-state.json"


def test_mask_path_falls_back_to_basename(tmp_path):
    p = tmp_path / "explicit-flag-target.json"
    assert mask_path(p) == "explicit-flag-target.json"
