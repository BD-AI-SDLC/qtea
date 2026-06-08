"""Tests for the vendored pytest runtime plugin (`worca_t_runtime.py.tpl`).

The template ships as a `.py.tpl` file (not importable by name), so the
test loads it via `importlib.util.spec_from_file_location` into a private
namespace. This lets us exercise:
  - `tbd()` helper + sentinel detection
  - `_resolve_sentinel` (cache + dev-locator + LLM dispatch)
  - `_RetryingLocator` proxy (action-method retry on `TimeoutError`)
  - `_is_playwright_timeout` heuristic
  - cache invalidation on retry

Real Playwright isn't available here, so the wrapped `Locator` is a
fake whose methods raise `TimeoutError`-shaped exceptions on demand.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Load the template as a module
# ---------------------------------------------------------------------------


def _load_runtime():
    """Import the template (a .py.tpl file) as a module. The `.tpl` extension
    isn't recognized by spec_from_file_location's default loader picker, so
    construct a SourceFileLoader explicitly. The module must be registered
    in sys.modules BEFORE exec — `@dataclass` looks up `cls.__module__` in
    sys.modules during decoration and chokes on None."""
    import sys
    from importlib.machinery import SourceFileLoader

    tpl = (
        Path(__file__).resolve().parents[2]
        / "src" / "worca_t" / "_resources" / "runtime" / "worca_t_runtime.py.tpl"
    )
    loader = SourceFileLoader("worca_t_runtime_under_test", str(tpl))
    spec = importlib.util.spec_from_loader("worca_t_runtime_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["worca_t_runtime_under_test"] = mod
    try:
        loader.exec_module(mod)
    except Exception:
        sys.modules.pop("worca_t_runtime_under_test", None)
        raise
    return mod


@pytest.fixture
def runtime():
    """Fresh module per test — avoids cross-test state leakage in module-globals."""
    return _load_runtime()


# ---------------------------------------------------------------------------
# tbd() + sentinel helpers
# ---------------------------------------------------------------------------


def test_tbd_returns_sentinel_with_intent(runtime):
    sentinel = runtime.tbd("primary submit button")
    assert runtime.is_sentinel(sentinel)
    assert runtime.parse_sentinel(sentinel) == "primary submit button"


def test_tbd_rejects_empty_intent(runtime):
    with pytest.raises(ValueError):
        runtime.tbd("")
    with pytest.raises(ValueError):
        runtime.tbd("   ")
    with pytest.raises(ValueError):
        runtime.tbd(None)  # type: ignore[arg-type]


def test_is_sentinel_rejects_normal_selectors(runtime):
    assert not runtime.is_sentinel("#login")
    assert not runtime.is_sentinel("[data-testid='x']")
    assert not runtime.is_sentinel(42)
    assert not runtime.is_sentinel(None)


# ---------------------------------------------------------------------------
# _is_playwright_timeout — best-effort detector
# ---------------------------------------------------------------------------


def test_is_playwright_timeout_detects_by_class_module(runtime):
    # Simulate a Playwright TimeoutError via a class living in playwright._impl.
    cls = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
    assert runtime._is_playwright_timeout(cls("x"))


def test_is_playwright_timeout_detects_by_message(runtime):
    # Generic Exception with the Playwright-shaped message.
    assert runtime._is_playwright_timeout(Exception("Timeout 30000ms exceeded."))


def test_is_playwright_timeout_rejects_unrelated(runtime):
    assert not runtime._is_playwright_timeout(ValueError("boom"))
    assert not runtime._is_playwright_timeout(Exception("some other error"))


# ---------------------------------------------------------------------------
# _RetryingLocator — passthrough for non-action attributes
# ---------------------------------------------------------------------------


def _make_resolution(runtime, selector="#x", source="dev"):
    return runtime._Resolution(
        selector=selector,
        source=source,
        constant_name="LOGIN_BUTTON",
        intent="primary submit button",
        test_file="tests/worca_test_login.py",
    )


def test_retrying_locator_passes_through_non_retriable_attrs(runtime):
    real = SimpleNamespace(count=lambda: 1, some_attr="hello")
    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("submit button"),
        resolution=_make_resolution(runtime),
        locator_args=(), locator_kwargs={},
    )
    # `count` is not in _RETRIABLE_METHODS — passes through unwrapped.
    assert proxy.count() == 1
    assert proxy.some_attr == "hello"


def test_retrying_locator_passes_through_when_no_timeout(runtime):
    """A successful action method returns its value unchanged — no retry."""
    real = SimpleNamespace(click=lambda timeout=None: "clicked")
    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("submit button"),
        resolution=_make_resolution(runtime),
        locator_args=(), locator_kwargs={},
    )
    assert proxy.click() == "clicked"


# ---------------------------------------------------------------------------
# _RetryingLocator — retry on TimeoutError
# ---------------------------------------------------------------------------


class _FakeTimeoutError(Exception):
    """Stand-in for playwright's TimeoutError — detected by message shape."""

    def __str__(self):
        return "Timeout 30000ms exceeded."


def test_retrying_locator_invalidates_cache_and_retries_on_timeout(runtime, tmp_path, monkeypatch):
    """Dev selector fails → cache invalidated → re-resolves via LLM
    (skipping dev) → retries the same action with the new selector."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(cache_dir))

    call_log: list[tuple[str, str]] = []

    def _first_real_click(timeout=None):
        call_log.append(("first_real_click", "stale"))
        raise _FakeTimeoutError()

    def _second_real_click(timeout=None):
        call_log.append(("second_real_click", "fresh"))
        return "clicked successfully"

    first_real = SimpleNamespace(click=_first_real_click)
    second_real = SimpleNamespace(click=_second_real_click)

    page = SimpleNamespace()

    # Original Page.locator factory — must be present on the module since
    # the retry path calls `_original_page_locator(self._page, fresh.selector, ...)`.
    monkeypatch.setattr(runtime, "_original_page_locator", lambda _page, sel, *a, **k: second_real)

    # _resolve_sentinel under retry must return a fresh resolution. Patch it
    # to assert we're called with skip_dev=True + skip_cache=True (the
    # invariant the retry proxy depends on).
    captured: dict = {}

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False):
        captured["skip_dev"] = skip_dev
        captured["skip_cache"] = skip_cache
        return runtime._Resolution(
            selector="[data-testid='fresh-login']",
            source="agent",
            constant_name="LOGIN_BUTTON",
            intent="primary submit button",
            test_file="tests/worca_test_login.py",
        )

    monkeypatch.setattr(runtime, "_resolve_sentinel", _fake_resolve)

    proxy = runtime._RetryingLocator(
        real=first_real, page=page,
        sentinel=runtime.tbd("primary submit button"),
        resolution=_make_resolution(runtime, selector="[data-testid='stale-dev']", source="dev"),
        locator_args=(), locator_kwargs={},
    )

    result = proxy.click()
    assert result == "clicked successfully"
    assert call_log == [("first_real_click", "stale"), ("second_real_click", "fresh")]
    # When the failing selector came from `dev`, retry MUST skip the dev file.
    # In all cases, retry must skip the cache (the stale entry was just invalidated).
    assert captured["skip_dev"] is True
    assert captured["skip_cache"] is True


def test_retrying_locator_retry_skip_dev_false_when_source_is_cached(runtime, tmp_path, monkeypatch):
    """When the failing selector came from the CACHE (not the dev file),
    retry should NOT skip dev — the dev file might still be valid for
    other constants, and on the constant-specific level the cache invalidation
    is what matters."""
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))

    def _click(timeout=None):
        raise _FakeTimeoutError()

    real = SimpleNamespace(click=_click)
    monkeypatch.setattr(runtime, "_original_page_locator", lambda *a, **k: SimpleNamespace(click=lambda timeout=None: "ok"))

    captured: dict = {}

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False):
        captured["skip_dev"] = skip_dev
        return runtime._Resolution(
            selector="#fresh", source="agent",
            constant_name="X", intent="x", test_file=None,
        )

    monkeypatch.setattr(runtime, "_resolve_sentinel", _fake_resolve)

    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime, source="cached"),
        locator_args=(), locator_kwargs={},
    )
    proxy.click()
    # source="cached" → dev file might still help; don't skip it on retry.
    assert captured["skip_dev"] is False


def test_retrying_locator_propagates_original_timeout_when_re_resolve_fails(runtime, monkeypatch):
    """If re-resolution also fails (LLM can't find the element), the
    original TimeoutError surfaces so the existing self-heal flow picks
    it up — we don't swallow it silently."""
    def _click(timeout=None):
        raise _FakeTimeoutError()

    real = SimpleNamespace(click=_click)
    monkeypatch.setattr(runtime, "_original_page_locator", lambda *a, **k: real)
    monkeypatch.setattr(
        runtime, "_resolve_sentinel",
        lambda *a, **k: runtime._Resolution(
            selector=None, source="none",
            constant_name="X", intent="x", test_file=None,
        ),
    )

    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime),
        locator_args=(), locator_kwargs={},
    )
    with pytest.raises(_FakeTimeoutError):
        proxy.click()


def test_retrying_locator_only_retries_once(runtime, monkeypatch):
    """Even if the second attempt also raises TimeoutError, the proxy
    propagates it instead of looping. `_retried` guards against
    runaway recursion if the resolver keeps returning bad selectors."""
    fresh_real = SimpleNamespace(click=lambda timeout=None: (_ for _ in ()).throw(_FakeTimeoutError()))

    def _first_click(timeout=None):
        raise _FakeTimeoutError()

    first_real = SimpleNamespace(click=_first_click)
    monkeypatch.setattr(runtime, "_original_page_locator", lambda *a, **k: fresh_real)
    monkeypatch.setattr(
        runtime, "_resolve_sentinel",
        lambda *a, **k: runtime._Resolution(
            selector="#fresh", source="agent",
            constant_name="X", intent="x", test_file=None,
        ),
    )

    proxy = runtime._RetryingLocator(
        real=first_real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime),
        locator_args=(), locator_kwargs={},
    )
    with pytest.raises(_FakeTimeoutError):
        proxy.click()


def test_retrying_locator_does_not_retry_non_timeout_errors(runtime, monkeypatch):
    """Non-timeout errors (e.g. ValueError from a bad fill value) propagate
    immediately — the retry path is selector-staleness-specific."""
    def _fill(value, timeout=None):
        raise ValueError("invalid input")

    real = SimpleNamespace(fill=_fill)
    monkeypatch.setattr(runtime, "_original_page_locator", lambda *a, **k: real)

    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime),
        locator_args=(), locator_kwargs={},
    )
    with pytest.raises(ValueError, match="invalid input"):
        proxy.fill("nope")


# ---------------------------------------------------------------------------
# Cache invalidation helper
# ---------------------------------------------------------------------------


def test_invalidate_cache_entry_removes_key(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))
    key = runtime._cache_key("tests/x.py", "LOGIN", "submit")
    cache_path = tmp_path / "locator-cache.json"
    cache_path.write_text(
        json.dumps({
            "entries": [{"key": key, "selector": "#stale", "constant_name": "LOGIN"}],
        }),
        encoding="utf-8",
    )
    runtime._invalidate_cache_entry("LOGIN", "submit", "tests/x.py")
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    keys = [e["key"] for e in after["entries"]]
    assert key not in keys


def test_invalidate_cache_entry_missing_key_is_noop(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))
    # Cache file doesn't exist → safely returns.
    runtime._invalidate_cache_entry("LOGIN", "submit", "tests/x.py")
