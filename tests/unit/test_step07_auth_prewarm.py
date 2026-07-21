"""Tests for the pre-Step-7 auth prewarm (s07_auth_prewarm).

Exercises the gating matrix + best-effort behavior. cmd_auth_capture is mocked
— no real SUT venv / Playwright spawn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qtea.steps import s07_auth_prewarm as ap

_PW_MODULE = {
    "language": "python",
    "auth_flow": {"entry_method": "tests/fixtures/auth.py:sign_in"},
}


def _opts(**kw):
    kw.setdefault("no_auth_capture", False)
    kw.setdefault("auth_prewarm_mode", None)
    return SimpleNamespace(**kw)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "QTEA_AUTH_CAPTURE",
        "QTEA_NO_LLM_RESOLVE",
        "QTEA_AUTH_CAPTURE_TIMEOUT_S",
        "QTEA_AUTH_CAPTURE_HEADED",
        "QTEA_AUTH_PREWARM_MODE",
        "QTEA_AUTH_USERNAME_VAR",
        "QTEA_AUTH_PASSWORD_VAR",
        "QTEA_AUTH_IDENTITY_PROVIDER",
    ):
        monkeypatch.delenv(k, raising=False)


# --- auth_prewarm_mode ------------------------------------------------------


def test_mode_default_is_headed():
    assert ap.auth_prewarm_mode(_opts()) == "headed"


def test_mode_flag_wins():
    assert ap.auth_prewarm_mode(_opts(auth_prewarm_mode="script")) == "script"


def test_mode_env_applies_when_no_flag(monkeypatch):
    monkeypatch.setenv("QTEA_AUTH_PREWARM_MODE", "script")
    assert ap.auth_prewarm_mode(_opts()) == "script"


def test_mode_forced_off_by_no_auth_capture():
    assert ap.auth_prewarm_mode(_opts(no_auth_capture=True)) == "off"


def test_mode_forced_off_in_zero_llm(monkeypatch):
    monkeypatch.setenv("QTEA_NO_LLM_RESOLVE", "1")
    assert ap.auth_prewarm_mode(_opts(auth_prewarm_mode="mcp")) == "off"


def test_mode_invalid_falls_back_to_headed():
    assert ap.auth_prewarm_mode(_opts(auth_prewarm_mode="bogus")) == "headed"


def test_auth_headed_flag_forces_headed_mode():
    # --auth-headed overrides an explicit --auth-prewarm-mode.
    assert (
        ap.auth_prewarm_mode(_opts(auth_prewarm_mode="mcp", auth_headed=True))
        == "headed"
    )


def test_auth_headed_env_forces_headed_mode(monkeypatch):
    monkeypatch.setenv("QTEA_AUTH_CAPTURE_HEADED", "1")
    assert ap.auth_prewarm_mode(_opts(auth_prewarm_mode="script")) == "headed"


# --- resolve_login_credentials ----------------------------------------------


def test_resolve_credentials_by_name_heuristic(monkeypatch):
    monkeypatch.setenv("USERNAME_A", "alice")
    monkeypatch.setenv("PASSWORD_A", "s3cret-pw")
    mod = {"auth_flow": {"credentials_env_vars": ["USERNAME_A", "PASSWORD_A"]}}
    assert ap.resolve_login_credentials(mod) == ("alice", "s3cret-pw")


def test_resolve_credentials_override_var(monkeypatch):
    monkeypatch.setenv("USERNAME_A", "alice")
    monkeypatch.setenv("PASSWORD_A", "pw-alice")
    monkeypatch.setenv("USERNAME_B", "bob")
    monkeypatch.setenv("PASSWORD_B", "pw-bob")
    monkeypatch.setenv("QTEA_AUTH_USERNAME_VAR", "USERNAME_B")
    monkeypatch.setenv("QTEA_AUTH_PASSWORD_VAR", "PASSWORD_B")
    mod = {"auth_flow": {"credentials_env_vars": ["USERNAME_A", "PASSWORD_A"]}}
    assert ap.resolve_login_credentials(mod) == ("bob", "pw-bob")


def test_resolve_credentials_none_when_unset(monkeypatch):
    mod = {"auth_flow": {"credentials_env_vars": ["USERNAME_X", "PASSWORD_X"]}}
    assert ap.resolve_login_credentials(mod) is None


def test_resolve_credentials_none_when_no_matching_names():
    mod = {"auth_flow": {"credentials_env_vars": ["TOKEN_ONLY"]}}
    assert ap.resolve_login_credentials(mod) is None


def test_identity_provider_hint(monkeypatch):
    assert ap.login_identity_provider() is None
    monkeypatch.setenv("QTEA_AUTH_IDENTITY_PROVIDER", "Internal")
    assert ap.login_identity_provider() == "Internal"


def _no_storage(monkeypatch):
    monkeypatch.setattr(ap._storage_state, "resolve", lambda **kw: None)


# --- auth_capture_enabled ---------------------------------------------------


def test_enabled_by_default():
    assert ap.auth_capture_enabled() is True


def test_disabled_by_flag():
    assert ap.auth_capture_enabled(no_auth_capture=True) is False


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("QTEA_AUTH_CAPTURE", "0")
    assert ap.auth_capture_enabled() is False


def test_disabled_in_zero_llm_mode(monkeypatch):
    monkeypatch.setenv("QTEA_NO_LLM_RESOLVE", "1")
    assert ap.auth_capture_enabled() is False


# --- is_applicable ----------------------------------------------------------


def test_applicable_happy_path(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
    )
    assert ok is True
    assert reason == "applicable"


def test_skip_when_storage_state_already_present(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ap._storage_state, "resolve", lambda **kw: tmp_path / "ss.json",
    )
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
    )
    assert ok is False
    assert reason == "storage_state_present"


def test_skip_when_no_auth_flow(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module={"language": "python"},
    )
    assert ok is False
    assert reason == "no_auth_flow_entry_method"


def test_skip_non_playwright_language(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module={"language": "java", "auth_flow": {"entry_method": "a:b"}},
    )
    assert ok is False
    assert reason.startswith("non_playwright_lang")


def test_skip_when_entry_is_class_method(monkeypatch, tmp_path):
    """A dotted symbol (POM method like `BasePage.logIn`) can't be invoked by
    the module-level capture wrapper — skip cleanly rather than fail confusingly.
    """
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module={
            "language": "typescript",
            "auth_flow": {"entry_method": "src/pages/BasePage.ts:BasePage.logIn"},
        },
    )
    assert ok is False
    assert reason == "auth_flow_entry_is_class_method"


def test_module_level_function_entry_is_applicable(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module={
            "language": "typescript",
            "auth_flow": {"entry_method": "tests/auth.ts:signIn"},
        },
    )
    assert ok is True
    assert reason == "applicable"


def test_skip_when_no_active_module(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=None,
    )
    assert ok is False
    assert reason == "no_active_module"


def test_disabled_flag_beats_everything(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, no_auth_capture=True,
    )
    assert ok is False
    assert reason == "disabled"


# --- load_active_module -----------------------------------------------------


def test_load_active_module(tmp_path):
    step6 = tmp_path / "step6"
    step6.mkdir()
    (step6 / "sut_inventory.json").write_text(
        json.dumps(
            {"active_module": "fe", "modules": [{"name": "fe", "language": "python"}]}
        ),
        encoding="utf-8",
    )
    mod = ap.load_active_module(step6)
    assert mod is not None
    assert mod["name"] == "fe"


def test_load_active_module_missing_file(tmp_path):
    assert ap.load_active_module(tmp_path) is None


# --- maybe_prewarm_auth -----------------------------------------------------


async def test_maybe_prewarm_skips_when_not_applicable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ap._storage_state, "resolve", lambda **kw: tmp_path / "ss.json",
    )
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
    )
    assert out is None


async def test_maybe_prewarm_calls_capture_headless(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    called: dict = {}

    def fake_capture(*, sut, output, headed, timeout_s, active_module=None):
        called.update(
            sut=sut, output=output, headed=headed, timeout_s=timeout_s,
            active_module=active_module,
        )
        return ss

    import qtea.auth_capture as acmod

    monkeypatch.setattr(acmod, "cmd_auth_capture", fake_capture)
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
    )
    assert out == ss
    assert called["headed"] is False  # unattended runs never open a headed browser
    # No override configured -> explicit per-SUT convention path (write_target).
    assert called["output"] == tmp_path / ".qtea" / "storage-state.json"
    # The loaded module is passed through so cmd_auth_capture does not depend on
    # <sut>/.qtea/ (which the pipeline never creates).
    assert called["active_module"] is _PW_MODULE


async def test_maybe_prewarm_writes_to_override_and_gitignores_when_inside_sut(
    monkeypatch, tmp_path
):
    """A configured --storage-state target inside the SUT is used as the write
    location AND added to the SUT's .gitignore (custom name not covered by the
    Step-6-seeded `.qtea/` / `storage-state.json` entries)."""
    _no_storage(monkeypatch)
    override = tmp_path / "auth" / "session.json"
    called: dict = {}

    def fake_capture(*, sut, output, headed, timeout_s, active_module=None):
        called["output"] = output
        return output

    import qtea.auth_capture as acmod

    monkeypatch.setattr(acmod, "cmd_auth_capture", fake_capture)
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path,
        workspace_root=tmp_path,
        active_module=_PW_MODULE,
        cli_storage_state=override,
    )
    assert out == override
    assert called["output"] == override
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        "auth/session.json"
    ]


async def test_maybe_prewarm_never_raises_on_capture_failure(monkeypatch, tmp_path):
    _no_storage(monkeypatch)

    def boom(**kw):
        raise RuntimeError("no venv / MFA required")

    import qtea.auth_capture as acmod

    monkeypatch.setattr(acmod, "cmd_auth_capture", boom)
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
    )
    assert out is None  # degrades silently to unauthenticated exploration


# --- headed / MFA handling --------------------------------------------------


def test_headed_mode_requested_env(monkeypatch):
    monkeypatch.setenv("QTEA_AUTH_CAPTURE_HEADED", "1")
    assert ap.headed_mode_requested() is True
    monkeypatch.setenv("QTEA_AUTH_CAPTURE_HEADED", "0")
    assert ap.headed_mode_requested() is False


def test_headed_mode_requested_option():
    from types import SimpleNamespace
    assert ap.headed_mode_requested(SimpleNamespace(auth_headed=True)) is True
    assert ap.headed_mode_requested(SimpleNamespace(auth_headed=False)) is False


def test_headed_non_interactive_is_not_applicable(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
        headed_requested=True, interactive=False,
    )
    assert ok is False
    assert reason == "headed_required_non_interactive"


def test_headed_interactive_is_applicable(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ok, reason = ap.is_applicable(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
        headed_requested=True, interactive=True,
    )
    assert ok is True
    assert reason == "applicable"


async def test_maybe_prewarm_runs_headed_when_interactive(monkeypatch, tmp_path):
    _no_storage(monkeypatch)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    called: dict = {}

    def fake_capture(*, sut, output, headed, timeout_s, active_module=None):
        called.update(headed=headed, timeout_s=timeout_s)
        return ss

    import qtea.auth_capture as acmod

    monkeypatch.setattr(acmod, "cmd_auth_capture", fake_capture)
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
        headed_requested=True, interactive=True,
    )
    assert out == ss
    assert called["headed"] is True
    assert called["timeout_s"] == 600  # generous headed default for human MFA


async def test_maybe_prewarm_skips_headed_when_non_interactive(monkeypatch, tmp_path):
    _no_storage(monkeypatch)

    def fake_capture(**kw):  # pragma: no cover - must not be called
        raise AssertionError("capture must not run headed without a TTY")

    import qtea.auth_capture as acmod

    monkeypatch.setattr(acmod, "cmd_auth_capture", fake_capture)
    out = await ap.maybe_prewarm_auth(
        sut_root=tmp_path, workspace_root=tmp_path, active_module=_PW_MODULE,
        headed_requested=True, interactive=False,
    )
    assert out is None


# --- maybe_headed_prewarm (headed, human-driven login) ----------------------


def _stub_headed(
    monkeypatch, *, available=True, browser_ok=True, captured=True, skip=False,
):
    """Patch qtea.headed_auth_capture so no real browser launches / downloads.
    Returns a dict recording whether capture_headed_login was called + creds."""
    import qtea.headed_auth_capture as hac

    seen: dict = {"capture_called": False, "creds": None}

    async def fake_capture(*, base_url, output, creds=None):
        seen["capture_called"] = True
        seen["creds"] = creds
        if skip:
            raise hac.HeadedLoginSkipped("user chose to skip authentication")
        if not captured:
            raise RuntimeError("user closed the browser")
        from pathlib import Path as _P
        _P(output).parent.mkdir(parents=True, exist_ok=True)
        _P(output).write_text("{}", encoding="utf-8")
        return _P(output)

    async def fake_ensure(console=None):
        return browser_ok

    monkeypatch.setattr(hac, "is_available", lambda: available)
    monkeypatch.setattr(hac, "ensure_chromium_async", fake_ensure)
    monkeypatch.setattr(hac, "capture_headed_login", fake_capture)
    return seen


async def test_headed_prewarm_skips_without_auth_flow(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module={"language": "python"}, base_url="https://sut.example",
        interactive=True,
    )
    assert status == "skipped"
    assert seen["capture_called"] is False


async def test_headed_prewarm_skips_without_base_url(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url=None, interactive=True,
    )
    assert status == "skipped"
    assert seen["capture_called"] is False


async def test_headed_prewarm_skips_when_non_interactive(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=False,
    )
    assert status == "skipped"
    assert seen["capture_called"] is False


async def test_headed_prewarm_falls_back_to_mcp_when_playwright_missing(
    monkeypatch, tmp_path,
):
    seen = _stub_headed(monkeypatch, available=False)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "fallback_mcp"
    assert seen["capture_called"] is False


async def test_headed_prewarm_falls_back_to_mcp_when_browser_download_fails(
    monkeypatch, tmp_path,
):
    seen = _stub_headed(monkeypatch, browser_ok=False)
    _no_storage(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "fallback_mcp"
    assert seen["capture_called"] is False


async def test_headed_prewarm_reuses_valid_session(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ap._storage_state, "resolve", lambda **kw: ss)

    import qtea.headed_auth_capture as hac

    async def fake_probe(base_url, path, **kw):
        return "authenticated"

    monkeypatch.setattr(hac, "probe_authenticated", fake_probe)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "reused"
    assert seen["capture_called"] is False


async def test_headed_prewarm_recaptures_when_session_stale(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ap._storage_state, "resolve", lambda **kw: ss)

    import qtea.headed_auth_capture as hac

    async def fake_probe(base_url, path, **kw):
        return "unauthenticated"

    monkeypatch.setattr(hac, "probe_authenticated", fake_probe)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "captured"
    assert seen["capture_called"] is True


async def test_headed_prewarm_ambiguous_probe_reuses(monkeypatch, tmp_path):
    """Conservative rule: ambiguity ⇒ trust the existing session (no re-login)."""
    seen = _stub_headed(monkeypatch)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ap._storage_state, "resolve", lambda **kw: ss)

    import qtea.headed_auth_capture as hac

    async def fake_probe(base_url, path, **kw):
        return "ambiguous"

    monkeypatch.setattr(hac, "probe_authenticated", fake_probe)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "reused"
    assert seen["capture_called"] is False


async def test_headed_prewarm_captures_fresh_when_no_session(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    _no_storage(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "captured"
    assert seen["capture_called"] is True


async def test_headed_prewarm_never_raises_on_capture_failure(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch, captured=False)
    _no_storage(monkeypatch)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "skipped"  # degrades to unauthenticated exploration
    assert seen["capture_called"] is True


async def test_headed_prewarm_returns_skipped_and_logs_info_when_user_skips(
    monkeypatch, tmp_path, caplog,
):
    """User-initiated skip must be distinguishable in logs from a genuine
    capture failure — info level, not a warning suggesting a retry is needed."""
    import logging

    seen = _stub_headed(monkeypatch, skip=True)
    _no_storage(monkeypatch)
    caplog.set_level(logging.INFO)
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=_PW_MODULE, base_url="https://sut.example", interactive=True,
    )
    assert status == "skipped"
    assert seen["capture_called"] is True
    assert any(
        "headed_prewarm_user_skipped" in rec.message for rec in caplog.records
    )
    assert not any(
        "headed_prewarm_failed" in rec.message for rec in caplog.records
    )


async def test_headed_prewarm_prefills_credentials(monkeypatch, tmp_path):
    seen = _stub_headed(monkeypatch)
    _no_storage(monkeypatch)
    monkeypatch.setenv("SUT_USER", "alice")
    monkeypatch.setenv("SUT_PASS", "pw")
    module = {
        "language": "python",
        "auth_flow": {
            "entry_method": "tests/auth.py:sign_in",
            "credentials_env_vars": ["SUT_USER", "SUT_PASS"],
        },
    }
    status = await ap.maybe_headed_prewarm(
        sut_root=tmp_path, workspace_root=tmp_path,
        active_module=module, base_url="https://sut.example", interactive=True,
    )
    assert status == "captured"
    assert seen["creds"] == ("alice", "pw")
