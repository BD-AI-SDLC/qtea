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
        test_file="tests/worca_login_test.py",
    )


def test_retrying_locator_passes_through_non_retriable_attrs(runtime):
    real = SimpleNamespace(count=lambda: 1, some_attr="hello")
    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("submit button"),
        resolution=_make_resolution(runtime),
        rebuild_locator=lambda sel: real,
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
        rebuild_locator=lambda sel: real,
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

    # _resolve_sentinel under retry must return a fresh resolution. Patch it
    # to assert we're called with skip_dev=True + skip_cache=True (the
    # invariant the retry proxy depends on).
    captured: dict = {}

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False, skip_heuristic=False):
        captured["skip_dev"] = skip_dev
        captured["skip_cache"] = skip_cache
        return runtime._Resolution(
            selector="[data-testid='fresh-login']",
            source="agent",
            constant_name="LOGIN_BUTTON",
            intent="primary submit button",
            test_file="tests/worca_login_test.py",
        )

    monkeypatch.setattr(runtime, "_resolve_sentinel", _fake_resolve)

    proxy = runtime._RetryingLocator(
        real=first_real, page=page,
        sentinel=runtime.tbd("primary submit button"),
        resolution=_make_resolution(runtime, selector="[data-testid='stale-dev']", source="dev"),
        # rebuild_locator builds a fresh real Locator from the re-resolved
        # selector — the retry path calls `self._rebuild_locator(fresh.selector)`.
        rebuild_locator=lambda sel: second_real,
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
    fresh_real = SimpleNamespace(click=lambda timeout=None: "ok")

    captured: dict = {}

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False, skip_heuristic=False):
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
        rebuild_locator=lambda sel: fresh_real,
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
        rebuild_locator=lambda sel: real,
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
        rebuild_locator=lambda sel: fresh_real,
    )
    with pytest.raises(_FakeTimeoutError):
        proxy.click()


def test_retrying_locator_does_not_retry_non_timeout_errors(runtime, monkeypatch):
    """Non-timeout errors (e.g. ValueError from a bad fill value) propagate
    immediately — the retry path is selector-staleness-specific."""
    def _fill(value, timeout=None):
        raise ValueError("invalid input")

    real = SimpleNamespace(fill=_fill)

    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime),
        rebuild_locator=lambda sel: real,
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


# ---------------------------------------------------------------------------
# Multi-candidate bundle: fallback walk before LLM re-resolve
# ---------------------------------------------------------------------------


def _make_bundled_resolution(runtime, *, candidates, source="agent"):
    """Build a _Resolution whose `selector` mirrors candidates[0] and whose
    `candidates` tuple lets the proxy walk fallback alternates."""
    primary = candidates[0]
    return runtime._Resolution(
        selector=primary["selector"],
        source=source,
        constant_name="GO",
        intent="go button",
        test_file="tests/test_go.py",
        candidates=tuple(candidates),
    )


def test_retrying_locator_walks_fallback_candidate_on_timeout(runtime, tmp_path, monkeypatch):
    """Primary candidate times out → proxy tries fallback from the same
    bundle WITHOUT invalidating the cache or calling _resolve_sentinel."""
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))
    # Seed the cache so the promotion path has an entry to rewrite.
    key = runtime._cache_key("tests/test_go.py", "GO", "go button")
    cache_path = tmp_path / "locator-cache.json"
    cache_path.write_text(
        json.dumps({"entries": [{
            "key": key,
            "test_file": "tests/test_go.py",
            "constant_name": "GO",
            "intent": "go button",
            "selector": "[data-testid='go']",
            "strategy": "data-testid",
            "confidence": 0.9,
            "candidates": [
                {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
                {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
            ],
            "source": "agent",
        }]}),
        encoding="utf-8",
    )

    call_log: list[str] = []

    def _primary_click(timeout=None):
        call_log.append("primary")
        raise _FakeTimeoutError()

    def _fallback_click(timeout=None):
        call_log.append("fallback")
        return "ok"

    primary_real = SimpleNamespace(click=_primary_click)
    fallback_real = SimpleNamespace(click=_fallback_click)

    # Fail the test if _resolve_sentinel is called — the fallback walk
    # must NOT trigger an LLM round-trip.
    def _forbidden_resolve(*args, **kwargs):
        raise AssertionError("_resolve_sentinel called even though a fallback candidate exists")

    monkeypatch.setattr(runtime, "_resolve_sentinel", _forbidden_resolve)

    candidates = [
        {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
        {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
    ]
    proxy = runtime._RetryingLocator(
        real=primary_real, page=None,
        sentinel=runtime.tbd("go button"),
        resolution=_make_bundled_resolution(runtime, candidates=candidates),
        rebuild_locator=lambda sel: fallback_real,
    )

    assert proxy.click() == "ok"
    assert call_log == ["primary", "fallback"]

    # Cache was rewritten: failed primary dropped, fallback promoted to sole entry.
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = after["entries"][0]
    assert entry["selector"] == "text=Go"
    assert entry["strategy"] == "text"
    assert len(entry["candidates"]) == 1
    assert entry["candidates"][0]["selector"] == "text=Go"


def test_retrying_locator_falls_through_to_llm_when_all_candidates_exhausted(runtime, tmp_path, monkeypatch):
    """Primary AND fallback both time out → proxy invalidates cache and
    re-resolves via _resolve_sentinel (the existing path)."""
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))

    call_log: list[str] = []

    def _timeout_click(timeout=None):
        call_log.append("timeout")
        raise _FakeTimeoutError()

    def _llm_resolved_click(timeout=None):
        call_log.append("llm-resolved")
        return "ok"

    primary_real = SimpleNamespace(click=_timeout_click)
    fallback_real = SimpleNamespace(click=_timeout_click)
    llm_resolved_real = SimpleNamespace(click=_llm_resolved_click)

    rebuild_calls = {"n": 0}

    def _rebuild(sel):
        rebuild_calls["n"] += 1
        # 1st rebuild = fallback candidate; 2nd rebuild = LLM-resolved selector.
        return fallback_real if rebuild_calls["n"] == 1 else llm_resolved_real

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False, skip_heuristic=False):
        call_log.append("llm-resolve-called")
        assert skip_cache is True  # the existing invariant
        return runtime._Resolution(
            selector="#llm-fresh", source="agent",
            constant_name="GO", intent="go button",
            test_file="tests/test_go.py",
        )

    monkeypatch.setattr(runtime, "_resolve_sentinel", _fake_resolve)

    candidates = [
        {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
        {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
    ]
    proxy = runtime._RetryingLocator(
        real=primary_real, page=None,
        sentinel=runtime.tbd("go button"),
        resolution=_make_bundled_resolution(runtime, candidates=candidates),
        rebuild_locator=_rebuild,
    )
    assert proxy.click() == "ok"
    # Sequence: primary timeout, fallback timeout, LLM resolve called, fresh click ok.
    assert call_log == ["timeout", "timeout", "llm-resolve-called", "llm-resolved"]


def test_retrying_locator_without_bundle_unchanged_behavior(runtime, tmp_path, monkeypatch):
    """Resolution with no candidates field (cached non-LLM, dev, heuristic)
    behaves identically to the pre-bundle code path: TimeoutError → invalidate
    + LLM re-resolve immediately."""
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))

    call_log: list[str] = []

    def _click(timeout=None):
        call_log.append("primary")
        raise _FakeTimeoutError()

    real = SimpleNamespace(click=_click)
    fresh = SimpleNamespace(click=lambda timeout=None: "ok")

    def _fake_resolve(p, sentinel, *, skip_dev=False, skip_cache=False, skip_heuristic=False):
        call_log.append("llm")
        return runtime._Resolution(
            selector="#fresh", source="agent",
            constant_name="X", intent="x", test_file=None,
        )

    monkeypatch.setattr(runtime, "_resolve_sentinel", _fake_resolve)

    proxy = runtime._RetryingLocator(
        real=real, page=None,
        sentinel=runtime.tbd("x"),
        resolution=_make_resolution(runtime),  # no candidates
        rebuild_locator=lambda sel: fresh,
    )
    assert proxy.click() == "ok"
    # No fallback walk — LLM re-resolve fires after the single primary timeout.
    assert call_log == ["primary", "llm"]


def test_promote_candidate_in_cache_rewrites_entry(runtime, tmp_path, monkeypatch):
    """The helper rewrites the cache entry so the working candidate becomes
    the sole entry. Verifies promotion semantics in isolation from the proxy."""
    monkeypatch.setenv("WORCA_T_CACHE_DIR", str(tmp_path))
    key = runtime._cache_key("tests/x.py", "GO", "go button")
    cache_path = tmp_path / "locator-cache.json"
    cache_path.write_text(
        json.dumps({"entries": [{
            "key": key,
            "test_file": "tests/x.py",
            "constant_name": "GO",
            "intent": "go button",
            "selector": "[data-testid='broken']",
            "strategy": "data-testid",
            "confidence": 0.9,
            "candidates": [
                {"selector": "[data-testid='broken']", "strategy": "data-testid", "confidence": 0.9},
                {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
            ],
            "source": "agent",
        }]}),
        encoding="utf-8",
    )
    working = {"selector": "text=Go", "strategy": "text", "confidence": 0.7}
    runtime._promote_candidate_in_cache("GO", "go button", "tests/x.py", working)
    after = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = after["entries"][0]
    assert entry["selector"] == "text=Go"
    assert entry["strategy"] == "text"
    assert entry["candidates"] == [working]


# ---------------------------------------------------------------------------
# Proxy injection patch — _proxy_url_to_inject / _maybe_inject_proxy_kwarg
# ---------------------------------------------------------------------------


def test_proxy_url_to_inject_returns_none_when_no_env(runtime, monkeypatch):
    for key in ("WORCA_T_PROXY", "HTTPS_PROXY", "https_proxy",
                "WORCA_T_DISABLE_PROXY_INJECT"):
        monkeypatch.delenv(key, raising=False)
    assert runtime._proxy_url_to_inject() is None


def test_proxy_url_to_inject_reads_https_proxy(runtime, monkeypatch):
    for key in ("WORCA_T_PROXY", "WORCA_T_DISABLE_PROXY_INJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    assert runtime._proxy_url_to_inject() == "http://localhost:3128"


def test_proxy_url_to_inject_worca_env_wins_over_https_proxy(runtime, monkeypatch):
    monkeypatch.delenv("WORCA_T_DISABLE_PROXY_INJECT", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://other:8080")
    monkeypatch.setenv("WORCA_T_PROXY", "http://localhost:3128")
    assert runtime._proxy_url_to_inject() == "http://localhost:3128"


def test_proxy_url_to_inject_disabled_via_env(runtime, monkeypatch):
    """WORCA_T_DISABLE_PROXY_INJECT=1 wins even when HTTPS_PROXY is set."""
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    monkeypatch.setenv("WORCA_T_DISABLE_PROXY_INJECT", "1")
    assert runtime._proxy_url_to_inject() is None


def test_maybe_inject_proxy_kwarg_injects_when_absent(runtime, monkeypatch):
    monkeypatch.delenv("WORCA_T_DISABLE_PROXY_INJECT", raising=False)
    monkeypatch.delenv("WORCA_T_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    out = runtime._maybe_inject_proxy_kwarg({"headless": True, "args": ["--no-sandbox"]})
    assert out["proxy"] == {"server": "http://localhost:3128"}
    # Original kwargs preserved.
    assert out["headless"] is True
    assert out["args"] == ["--no-sandbox"]


def test_maybe_inject_proxy_kwarg_respects_sut_proxy(runtime, monkeypatch):
    """An SUT that explicitly passed proxy= must win — we never override."""
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    sut_proxy = {"server": "http://corp-proxy:8080", "username": "u"}
    out = runtime._maybe_inject_proxy_kwarg({"proxy": sut_proxy})
    assert out["proxy"] is sut_proxy


def test_maybe_inject_proxy_kwarg_respects_sut_proxy_none(runtime, monkeypatch):
    """SUT passing ``proxy=None`` is an explicit "no proxy" decision — respect it."""
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    out = runtime._maybe_inject_proxy_kwarg({"proxy": None})
    assert out["proxy"] is None


def test_maybe_inject_proxy_kwarg_noop_without_env(runtime, monkeypatch):
    for key in ("WORCA_T_PROXY", "HTTPS_PROXY", "https_proxy",
                "WORCA_T_DISABLE_PROXY_INJECT"):
        monkeypatch.delenv(key, raising=False)
    out = runtime._maybe_inject_proxy_kwarg({"headless": True})
    assert "proxy" not in out


def test_wrap_sync_launch_forwards_args_and_injects_proxy(runtime, monkeypatch):
    """End-to-end on a stub: the wrapper forwards self + positional + kwargs
    to the original after injecting proxy."""
    monkeypatch.delenv("WORCA_T_DISABLE_PROXY_INJECT", raising=False)
    monkeypatch.delenv("WORCA_T_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")

    captured = {}

    def fake_launch(self, *args, **kwargs):
        captured["self"] = self
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "browser-handle"

    wrapped = runtime._wrap_sync_launch(fake_launch)
    self_stub = object()
    result = wrapped(self_stub, "first-positional", headless=True)
    assert result == "browser-handle"
    assert captured["self"] is self_stub
    assert captured["args"] == ("first-positional",)
    assert captured["kwargs"]["headless"] is True
    assert captured["kwargs"]["proxy"] == {"server": "http://localhost:3128"}


def test_wrap_sync_launch_preserves_wrapped_reference(runtime):
    """``__wrapped__`` is set so introspection tools / our own sessionfinish
    restore logic can find the original. (Sessionfinish restores from the
    ``_original_browsertype_methods`` dict, but the attribute is still useful
    for debugging and for callers that inspect the wrapper.)"""
    def original(self, **kwargs):
        return None
    wrapped = runtime._wrap_sync_launch(original)
    assert wrapped.__wrapped__ is original


def test_install_proxy_patch_skipped_when_disabled(runtime, monkeypatch):
    """WORCA_T_DISABLE_PROXY_INJECT=1 means no patching happens — the
    BrowserType originals are untouched and the registry stays empty."""
    monkeypatch.setenv("WORCA_T_DISABLE_PROXY_INJECT", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    # Ensure clean state.
    runtime._original_browsertype_methods.clear()
    runtime._install_proxy_patch()
    assert runtime._original_browsertype_methods == {}


def test_install_proxy_patch_idempotent(runtime, monkeypatch):
    """Calling install twice is a no-op the second time — we shouldn't
    re-wrap the wrapper. The registry's presence is the idempotency guard."""
    monkeypatch.delenv("WORCA_T_DISABLE_PROXY_INJECT", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:3128")
    # Pre-seed the registry to simulate "already installed".
    runtime._original_browsertype_methods["sync.launch"] = lambda self: None
    snapshot = dict(runtime._original_browsertype_methods)
    runtime._install_proxy_patch()
    assert runtime._original_browsertype_methods == snapshot


# ---------------------------------------------------------------------------
# AOM snapshot — aria_snapshot() YAML parser + _snapshot_page fallback chain
# ---------------------------------------------------------------------------


def test_parse_aria_snapshot_empty(runtime):
    assert runtime._parse_aria_snapshot_yaml("") == {}
    assert runtime._parse_aria_snapshot_yaml("   \n  \n") == {}
    assert runtime._parse_aria_snapshot_yaml(None) == {}


def test_parse_aria_snapshot_flat_nodes(runtime):
    """Flat list of top-level nodes — each becomes a root child."""
    yaml = '- button "Submit"\n- heading "Title"\n- alert'
    tree = runtime._parse_aria_snapshot_yaml(yaml)
    assert tree["role"] == "document"
    assert len(tree["children"]) == 3
    assert tree["children"][0] == {"role": "button", "name": "Submit", "children": []}
    assert tree["children"][1] == {"role": "heading", "name": "Title", "children": []}
    assert tree["children"][2] == {"role": "alert", "name": "", "children": []}


def test_parse_aria_snapshot_nested(runtime):
    """Indented children attach to the prior node's children list."""
    yaml = (
        "- navigation:\n"
        '  - link "Home"\n'
        '  - link "About"\n'
        '- button "Submit"'
    )
    tree = runtime._parse_aria_snapshot_yaml(yaml)
    nav = tree["children"][0]
    assert nav["role"] == "navigation"
    assert len(nav["children"]) == 2
    assert nav["children"][0] == {"role": "link", "name": "Home", "children": []}
    assert nav["children"][1] == {"role": "link", "name": "About", "children": []}
    # The button is a sibling of navigation (same indent).
    assert tree["children"][1]["role"] == "button"


def test_parse_aria_snapshot_inline_text_after_colon(runtime):
    """``- alert: Error message`` → role=alert, name='Error message'."""
    tree = runtime._parse_aria_snapshot_yaml("- alert: Error message\n- paragraph: Welcome")
    assert tree["children"][0] == {"role": "alert", "name": "Error message", "children": []}
    assert tree["children"][1] == {"role": "paragraph", "name": "Welcome", "children": []}


def test_parse_aria_snapshot_skips_attribute_metadata(runtime):
    """Lines like ``- /url: /help`` are attribute metadata, not nodes — skip."""
    yaml = (
        '- link "Help":\n'
        '  - /url: /help\n'
        '  - /target: _blank\n'
        '- button "Next"'
    )
    tree = runtime._parse_aria_snapshot_yaml(yaml)
    # The link node should have NO children (attributes were skipped).
    assert tree["children"][0]["role"] == "link"
    assert tree["children"][0]["name"] == "Help"
    assert tree["children"][0]["children"] == []
    assert tree["children"][1]["role"] == "button"


def test_parse_aria_snapshot_real_world_signin_page(runtime):
    """Round-trip a representative ARIA tree from a real signin page."""
    yaml = (
        '- img "Organization banner logo"\n'
        "- main:\n"
        '  - heading "Sign in" [level=1]\n'
        "  - alert\n"
        '  - textbox "user@example.com"\n'
        '  - button "Next"\n'
        "  - paragraph: Logon to tenant\n"
        '- button "Sign-in options"\n'
        "- contentinfo:\n"
        '  - link "Privacy":\n'
        '    - /url: /privacy'
    )
    tree = runtime._parse_aria_snapshot_yaml(yaml)
    # Walk + collect (role, name) pairs.
    pairs = [(n["role"], n["name"]) for n in runtime._aom_walk(tree)]
    # Document root is also yielded by _aom_walk.
    assert ("img", "Organization banner logo") in pairs
    assert ("main", "") in pairs
    assert ("heading", "Sign in") in pairs
    assert ("textbox", "user@example.com") in pairs
    assert ("button", "Next") in pairs
    assert ("button", "Sign-in options") in pairs
    assert ("link", "Privacy") in pairs


def test_parse_aria_snapshot_heuristic_finds_signin_button(runtime):
    """The parsed tree should drive the tier-3 heuristic to a clean
    ``role=button[name="Sign in"]`` selector for an intent like
    ``'sign in button'``."""
    yaml = (
        '- heading "Sign in" [level=1]\n'
        '- textbox "Email"\n'
        '- button "Sign in"\n'
        '- link "Forgot password"'
    )
    tree = runtime._parse_aria_snapshot_yaml(yaml)
    selector = runtime._heuristic_resolve("sign in button", tree)
    assert selector == 'role=button[name="Sign in"]'


def test_snapshot_page_prefers_aria_snapshot_with_mode_ai(runtime):
    """``_snapshot_page`` should try ``aria_snapshot(mode="ai")`` first
    (LLM-optimized, Playwright 1.59+). The legacy ``page.accessibility``
    path must NOT be invoked."""

    class _FakeBodyLocator:
        def __init__(self):
            self.calls: list[dict] = []

        def aria_snapshot(self, **kwargs):
            self.calls.append(kwargs)
            return '- button "OK"'

    class _FakePage:
        def __init__(self):
            self.legacy_called = False
            self.body = _FakeBodyLocator()

        def locator(self, selector):
            assert selector == "body"
            return self.body

        @property
        def accessibility(self):
            self.legacy_called = True
            raise AssertionError("legacy accessibility path must not run when aria_snapshot succeeds")

    page = _FakePage()
    text, tree = runtime._snapshot_page(page)
    assert text == '- button "OK"'
    assert tree["children"][0] == {"role": "button", "name": "OK", "children": []}
    assert page.legacy_called is False
    # mode="ai" attempted on the first call.
    assert page.body.calls == [{"mode": "ai"}]


def test_snapshot_page_falls_back_when_mode_kwarg_unsupported(runtime):
    """Older Playwright (1.40-1.58) doesn't know the ``mode`` kwarg and
    raises ``TypeError``. The wrapper must retry with no kwargs and the
    snapshot capture must still succeed."""

    class _FakeBodyLocator:
        def __init__(self):
            self.calls: list[dict] = []

        def aria_snapshot(self, **kwargs):
            self.calls.append(kwargs)
            if "mode" in kwargs:
                raise TypeError("unexpected keyword argument 'mode'")
            return '- link "Help"'

    class _FakePage:
        def __init__(self):
            self.body = _FakeBodyLocator()

        def locator(self, selector):
            return self.body

    page = _FakePage()
    text, tree = runtime._snapshot_page(page)
    assert text == '- link "Help"'
    assert tree["children"][0]["role"] == "link"
    # Both attempts were made — mode="ai" first, then no-mode fallback.
    assert page.body.calls == [{"mode": "ai"}, {}]


def test_snapshot_page_falls_back_to_legacy_accessibility(runtime):
    """When ``Locator.aria_snapshot`` is missing (older Playwright), fall
    back to the pre-1.40 ``page.accessibility.snapshot()`` API."""

    class _FakeBodyLocator:
        # No aria_snapshot method — AttributeError on access.
        pass

    class _FakeAxNode:
        def snapshot(self):
            return {"role": "document", "name": "legacy", "children": []}

    class _FakePage:
        def locator(self, selector):
            return _FakeBodyLocator()

        accessibility = _FakeAxNode()

    text, tree = runtime._snapshot_page(_FakePage())
    assert tree == {"role": "document", "name": "legacy", "children": []}
    # JSON-serialized snapshot for the LLM.
    assert "legacy" in text


def test_snapshot_page_returns_empty_on_total_failure(runtime):
    """When BOTH APIs blow up, return ``("", {})`` so the resolver still
    receives a well-formed input (LLM then says "no candidates" cleanly)."""

    class _FakePage:
        def locator(self, selector):
            raise RuntimeError("page closed")

        @property
        def accessibility(self):
            raise RuntimeError("also broken")

    text, tree = runtime._snapshot_page(_FakePage())
    assert text == ""
    assert tree == {}


def test_resolve_page_from_receiver_detects_page_via_main_frame(runtime):
    """Probe is ``main_frame`` (Page-only, survives the 1.40 accessibility
    removal). A Page-like object with ``main_frame`` returns itself."""

    class _FakePage:
        main_frame = object()

    page = _FakePage()
    assert runtime._resolve_page_from_receiver(page) is page


def test_resolve_page_from_receiver_walks_locator_to_page(runtime):
    """A Locator-shaped object with a ``.page`` callable returns that page."""

    class _FakePage:
        main_frame = object()

    target_page = _FakePage()

    class _FakeLocator:
        # No main_frame — not a Page.
        def page(self):
            return target_page

    assert runtime._resolve_page_from_receiver(_FakeLocator()) is target_page


# ---------------------------------------------------------------------------
# Storage-state auto-capture (Use case B — same-run handover for Step 9 heal)
# ---------------------------------------------------------------------------


class _FakeContext:
    """Stand-in for a Playwright BrowserContext with a storage_state method."""

    def __init__(self):
        self.calls: list[dict] = []

    def storage_state(self, path: str | None = None):
        # Mimic Playwright signature: when ``path`` is provided, write the
        # state to disk and return None; otherwise return a dict.
        self.calls.append({"path": path})
        if path is not None:
            from pathlib import Path as _Path
            _Path(path).parent.mkdir(parents=True, exist_ok=True)
            _Path(path).write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
            return None
        return {"cookies": [], "origins": []}


class _FakeItem:
    """Stand-in for a pytest Item with funcargs + a stashed rep_call."""

    def __init__(self, funcargs: dict, passed: bool = True, nodeid: str = "tests/t.py::t"):
        self.funcargs = funcargs
        self.nodeid = nodeid
        self.rep_call = type("_RepStub", (), {"passed": passed})()


def test_storage_state_auto_capture_on_first_passing_test(runtime, tmp_path, monkeypatch):
    """When WORCA_T_WORKSPACE_DIR is set and a test passes with a `context`
    fixture in scope, the runtime captures storage_state to the workspace
    file. Subsequent tests do NOT re-capture (single-capture-per-session)."""
    monkeypatch.setenv("WORCA_T_WORKSPACE_DIR", str(tmp_path))
    # Reset the module-level flag (fresh `runtime` fixture per test should
    # already give us False, but be explicit).
    runtime._storage_state_captured = False

    ctx1 = _FakeContext()
    item1 = _FakeItem(funcargs={"context": ctx1}, passed=True, nodeid="t.py::a")
    runtime.pytest_runtest_teardown(item1, nextitem=None)
    assert (tmp_path / "storage-state.json").is_file()
    assert ctx1.calls == [{"path": str(tmp_path / "storage-state.json")}]
    assert runtime._storage_state_captured is True

    # Second passing test: no further capture (idempotent, single-shot).
    ctx2 = _FakeContext()
    item2 = _FakeItem(funcargs={"context": ctx2}, passed=True, nodeid="t.py::b")
    runtime.pytest_runtest_teardown(item2, nextitem=None)
    assert ctx2.calls == []


def test_storage_state_auto_capture_skipped_on_failing_test(runtime, tmp_path, monkeypatch):
    """A FAILED test must not trigger capture — capturing from a context
    whose auth might have aborted mid-flow would persist a half-broken
    session."""
    monkeypatch.setenv("WORCA_T_WORKSPACE_DIR", str(tmp_path))
    runtime._storage_state_captured = False

    ctx = _FakeContext()
    item = _FakeItem(funcargs={"context": ctx}, passed=False)
    runtime.pytest_runtest_teardown(item, nextitem=None)
    assert ctx.calls == []
    assert runtime._storage_state_captured is False
    assert not (tmp_path / "storage-state.json").exists()


def test_storage_state_auto_capture_skipped_when_env_unset(runtime, tmp_path, monkeypatch):
    """No WORCA_T_WORKSPACE_DIR → standalone pytest run, not under worca-t.
    Capture is a no-op (we don't want to scribble files in random cwds)."""
    monkeypatch.delenv("WORCA_T_WORKSPACE_DIR", raising=False)
    runtime._storage_state_captured = False

    ctx = _FakeContext()
    item = _FakeItem(funcargs={"context": ctx}, passed=True)
    runtime.pytest_runtest_teardown(item, nextitem=None)
    assert ctx.calls == []
    assert runtime._storage_state_captured is False


def test_storage_state_auto_capture_no_context_fixture(runtime, tmp_path, monkeypatch):
    """SUT has no `context` fixture (non-Playwright stack) — skip silently."""
    monkeypatch.setenv("WORCA_T_WORKSPACE_DIR", str(tmp_path))
    runtime._storage_state_captured = False

    item = _FakeItem(funcargs={"something_else": object()}, passed=True)
    runtime.pytest_runtest_teardown(item, nextitem=None)
    assert runtime._storage_state_captured is False
    assert not (tmp_path / "storage-state.json").exists()


def test_storage_state_auto_capture_falls_back_to_funcarg_scan(runtime, tmp_path, monkeypatch):
    """When the fixture is renamed (not 'context'), scan funcargs for any
    value with a callable storage_state method."""
    monkeypatch.setenv("WORCA_T_WORKSPACE_DIR", str(tmp_path))
    runtime._storage_state_captured = False

    ctx = _FakeContext()
    item = _FakeItem(funcargs={"playwright_context": ctx}, passed=True)
    runtime.pytest_runtest_teardown(item, nextitem=None)
    assert ctx.calls == [{"path": str(tmp_path / "storage-state.json")}]
    assert runtime._storage_state_captured is True


def test_storage_state_auto_capture_swallows_exceptions(runtime, tmp_path, monkeypatch):
    """A broken context.storage_state() must NOT crash the test session —
    capture is best-effort, failures degrade silently with a log warning."""
    monkeypatch.setenv("WORCA_T_WORKSPACE_DIR", str(tmp_path))
    runtime._storage_state_captured = False

    class _BrokenContext:
        def storage_state(self, path=None):
            raise RuntimeError("page closed mid-capture")

    item = _FakeItem(funcargs={"context": _BrokenContext()}, passed=True)
    # Should NOT raise.
    runtime.pytest_runtest_teardown(item, nextitem=None)
    # Flag stays False so a later passing test can retry the capture.
    assert runtime._storage_state_captured is False


def test_pytest_runtest_makereport_sets_rep_call_passed(runtime):
    """The makereport hook must stash a rep_call object with a `passed`
    attribute on the item, mirroring the pattern the teardown hook relies on.
    """
    class _Item:
        pass

    class _Call:
        when = "call"
        excinfo = None

    item = _Item()
    runtime.pytest_runtest_makereport(item, _Call())
    assert hasattr(item, "rep_call")
    assert item.rep_call.passed is True


def test_pytest_runtest_makereport_sets_rep_call_failed_on_excinfo(runtime):
    class _Item:
        pass

    class _Call:
        when = "call"
        excinfo = "<some exception info>"  # truthy

    item = _Item()
    runtime.pytest_runtest_makereport(item, _Call())
    assert item.rep_call.passed is False
