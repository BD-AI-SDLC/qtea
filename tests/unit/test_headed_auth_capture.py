"""Tests for the universal headed (human-driven) login capture.

The pure landing-page classifier and the best-effort orchestration are exercised
here; no real browser is launched.
"""

from __future__ import annotations

import pytest

from qtea import headed_auth_capture as hac


# --- _looks_unauthenticated (conservative classifier) -----------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://sut.example/login",
        "https://sut.example/account/login?next=/home",
        "https://sut.example/signin",
        "https://sut.example/sign-in",
        "https://login.microsoftonline.com/common/oauth2/authorize",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://dev-123.okta.com/app/foo",
        "https://SUT.example/SSO/start",  # case-insensitive
    ],
)
def test_login_url_marks_unauthenticated(url):
    assert hac._looks_unauthenticated(url, password_visible=False) is True


def test_password_field_marks_unauthenticated():
    assert hac._looks_unauthenticated("https://sut.example/home", True) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://sut.example/",
        "https://sut.example/dashboard",
        "https://sut.example/projects/42",
        "",
    ],
)
def test_normal_page_is_authenticated(url):
    # No login marker + no password field ⇒ treated as authenticated (proceed).
    assert hac._looks_unauthenticated(url, password_visible=False) is False


# --- is_available / install_hint --------------------------------------------


def test_is_available_returns_bool():
    assert isinstance(hac.is_available(), bool)


def test_install_hint_is_actionable():
    hint = hac.install_hint()
    assert "chromium" in hint.lower()
    assert "PLAYWRIGHT_DOWNLOAD_HOST" in hint


def test_package_hint_points_at_install_not_browser():
    hint = hac.package_hint()
    assert "package" in hint.lower()
    assert "reinstall" in hint.lower()
    assert "chromium" not in hint.lower()


# --- ensure_chromium (auto-download bootstrap) ------------------------------


def test_ensure_chromium_skips_when_already_installed(monkeypatch):
    monkeypatch.setattr(hac, "_chromium_installed", lambda: True)

    def _no_run(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("must not shell out when chromium already present")

    monkeypatch.setattr(hac.subprocess, "run", _no_run)
    assert hac.ensure_chromium() is True


def test_ensure_chromium_installs_when_missing(monkeypatch):
    monkeypatch.setattr(hac, "_chromium_installed", lambda: False)
    calls: dict = {}

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(hac.subprocess, "run", _fake_run)
    assert hac.ensure_chromium() is True
    assert calls["cmd"][1:] == ["-m", "playwright", "install", "chromium"]


def test_ensure_chromium_returns_false_on_install_failure(monkeypatch):
    monkeypatch.setattr(hac, "_chromium_installed", lambda: False)

    class _Result:
        returncode = 1
        stderr = "download blocked by proxy"

    monkeypatch.setattr(hac.subprocess, "run", lambda *a, **k: _Result())
    assert hac.ensure_chromium() is False


async def test_ensure_chromium_async_delegates(monkeypatch):
    monkeypatch.setattr(hac, "_chromium_installed", lambda: True)
    assert await hac.ensure_chromium_async() is True


# --- probe_authenticated fallback -------------------------------------------


async def test_probe_ambiguous_when_playwright_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(hac, "is_available", lambda: False)
    ss = tmp_path / "ss.json"
    ss.write_text("{}", encoding="utf-8")
    verdict = await hac.probe_authenticated("https://sut.example", ss)
    assert verdict == "ambiguous"


# --- _default_confirm / HeadedLoginSkipped -----------------------------------


def test_default_confirm_raises_on_skip_resolution(monkeypatch):
    from qtea.hitl import RESOLUTION_HEADED_LOGIN_SKIP

    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda questions, **kw: {questions[0].id: (RESOLUTION_HEADED_LOGIN_SKIP, "")},
    )
    with pytest.raises(hac.HeadedLoginSkipped):
        hac._default_confirm("https://sut.example")


def test_default_confirm_returns_normally_on_confirm(monkeypatch):
    from qtea.hitl import RESOLUTION_ANSWERED

    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda questions, **kw: {questions[0].id: (RESOLUTION_ANSWERED, "")},
    )
    hac._default_confirm("https://sut.example")  # must not raise


def test_default_confirm_returns_normally_on_empty_dict(monkeypatch):
    # Matches prompt_user()'s non-TTY / CI early-return contract.
    monkeypatch.setattr("qtea.hitl.prompt_user", lambda questions, **kw: {})
    hac._default_confirm("https://sut.example")  # must not raise


# --- request_browser_reopen registry -----------------------------------------


def test_request_browser_reopen_returns_false_when_unregistered():
    assert hac.request_browser_reopen("no-such-question") is False


async def test_request_browser_reopen_invokes_registered_callback():
    calls: list[str] = []

    class _FakePage:
        async def bring_to_front(self) -> None:
            calls.append("brought_to_front")

    hac._register_reopen("Q-TEST", _FakePage())
    try:
        assert hac.request_browser_reopen("Q-TEST") is True
        # The callback schedules the coroutine onto the current loop via
        # run_coroutine_threadsafe; yield control so it actually runs.
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == ["brought_to_front"]
    finally:
        hac._unregister_reopen("Q-TEST")


# --- capture_headed_login skip propagation -----------------------------------


async def test_capture_headed_login_propagates_skip_without_storage_state(
    monkeypatch, tmp_path,
):
    storage_calls: list[str] = []

    class _FakePage:
        async def goto(self, *a, **kw):
            pass

        async def bring_to_front(self):
            pass

    class _FakeContext:
        async def new_page(self):
            return _FakePage()

        async def storage_state(self, path):
            storage_calls.append(path)

    class _FakeBrowser:
        async def new_context(self):
            return _FakeContext()

        async def close(self):
            pass

    class _FakeChromium:
        async def launch(self, **kw):
            return _FakeBrowser()

    class _FakePlaywrightCtx:
        async def __aenter__(self):
            return type("P", (), {"chromium": _FakeChromium()})()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: _FakePlaywrightCtx(),
    )

    def _skip_confirm():
        raise hac.HeadedLoginSkipped("user chose to skip authentication")

    with pytest.raises(hac.HeadedLoginSkipped):
        await hac.capture_headed_login(
            base_url="https://sut.example",
            output=tmp_path / "storageState.json",
            confirm=_skip_confirm,
        )
    assert storage_calls == []
