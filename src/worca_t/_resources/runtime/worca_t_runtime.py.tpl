"""worca-t JIT locator runtime — vendored into the SUT at codegen time.

Single-file pytest plugin. The Step 7 codegen step copies this file into
``<sut>/tests/worca_t_runtime.py`` and registers it via the generated
``conftest.py``'s ``pytest_plugins`` list. The plugin then:

1. Monkey-patches ``playwright.sync_api.Page.locator`` (and async, if
   available) to detect sentinel strings produced by :func:`tbd`.
2. On sentinel access: consults the dev-supplied locator file → runtime
   cache → ``worca-t resolve`` subprocess → HITL, in that priority order.
3. Inflates default Playwright timeouts to absorb resolver latency.
4. Wraps the returned Locator in a thin proxy that, on TimeoutError,
   invalidates the cache entry and re-resolves once before failing.

ENV VARS read by this plugin (set by ``s09_execute.py``):

- ``WORCA_T_CACHE_DIR``         — directory for ``locator-cache.json`` (required)
- ``WORCA_T_DEV_LOCATORS``      — optional path to a dev-supplied locator file
- ``WORCA_T_RESOLVER_CMD``      — defaults to ``worca-t resolve``
- ``WORCA_T_RESOLVER_MODEL``    — passed through to the resolver subprocess
- ``WORCA_T_RUN_ID``            — stamped into cache entries
- ``WORCA_T_DEFAULT_TIMEOUT_MS``— Playwright default timeout in ms (default 60000)
- ``WORCA_T_INFLATE_TIMEOUTS``  — set to ``0`` to opt out of timeout inflation
- ``WORCA_T_DISABLE_JIT``       — set to ``1`` to disable the monkey-patch entirely

The plugin is a no-op on locator arguments that aren't sentinels, so SUT-
native tests in the same session run unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("worca_t.runtime")

# Sentinel layout: ``__WORCA_T_TBD__::<constant_or_intent>``. The prefix is
# unique enough that no real CSS selector can collide with it. ``tbd()``
# constructs the sentinel from the intent string only; the constant name
# is recovered at runtime by walking the call stack (cheap one-frame
# inspection — Python's ``sys._getframe`` is O(1)).
_SENTINEL_PREFIX = "__WORCA_T_TBD__::"


def tbd(intent: str) -> str:
    """Mark a locator constant as unresolved. The intent string describes
    what the element is supposed to be, in plain English.

    Usage::

        from tests.worca_t_runtime import tbd

        class LoginLocators:
            LOGIN_BUTTON = tbd("primary submit button on the login form")
            PASSWORD_INPUT = tbd("password input on the sign-in form")

    The returned string is a sentinel; the JIT runtime intercepts it when
    it reaches ``page.locator(...)``.
    """
    if not isinstance(intent, str) or not intent.strip():
        raise ValueError("tbd() requires a non-empty intent string")
    return f"{_SENTINEL_PREFIX}{intent.strip()}"


def is_sentinel(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_SENTINEL_PREFIX)


def parse_sentinel(value: str) -> str:
    """Return the intent embedded in a sentinel string."""
    return value[len(_SENTINEL_PREFIX):]


# ---------------------------------------------------------------------------
# Cache (mirrors worca_t.jit_resolver.read_cache / write_cache)
# ---------------------------------------------------------------------------


def _cache_path() -> Path | None:
    base = os.environ.get("WORCA_T_CACHE_DIR")
    if not base:
        return None
    return Path(base) / "locator-cache.json"


def _read_cache() -> dict[str, dict[str, Any]]:
    p = _cache_path()
    if p is None or not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    return {e["key"]: e for e in entries if isinstance(e, dict) and e.get("key")}


def _write_cache(entries: dict[str, dict[str, Any]]) -> None:
    p = _cache_path()
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": os.environ.get("WORCA_T_RUN_ID"),
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "entries": list(entries.values()),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _cache_key(test_file: str | None, constant_name: str, intent: str) -> str:
    import hashlib
    import re as _re
    norm = _re.sub(r"\s+", " ", (intent or "").strip().lower())
    payload = f"{test_file or ''}::{constant_name}::{norm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dev-locators (vendored mini-copy of worca_t.runtime.dev_locators)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DevLocator:
    constant_name: str
    selector: str
    strategy: str | None = None
    intent: str | None = None


def _is_xpath(s: str) -> bool:
    t = (s or "").strip()
    return t.startswith("//") or t.startswith("xpath=") or "By.XPATH" in t


def _load_dev_locators() -> dict[str, _DevLocator]:
    """Discover via env var or convention path. SUT root inferred as cwd
    (pytest runs from the SUT root by s09 convention)."""
    candidates: list[Path] = []
    env = os.environ.get("WORCA_T_DEV_LOCATORS")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / ".worca-t" / "dev-locators.json")
    for p in candidates:
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        block = raw.get("locators") if isinstance(raw, dict) else None
        if not isinstance(block, dict):
            continue
        out: dict[str, _DevLocator] = {}
        for name, entry in block.items():
            if not isinstance(entry, dict):
                continue
            sel = entry.get("selector")
            if not isinstance(sel, str) or not sel.strip() or _is_xpath(sel):
                continue
            out[name] = _DevLocator(
                constant_name=name,
                selector=sel.strip(),
                strategy=entry.get("strategy") if isinstance(entry.get("strategy"), str) else None,
                intent=entry.get("intent") if isinstance(entry.get("intent"), str) else None,
            )
        log.info("worca_t.dev_locators_loaded path=%s count=%d", p, len(out))
        return out
    return {}


# ---------------------------------------------------------------------------
# Resolver subprocess
# ---------------------------------------------------------------------------


def _call_resolver(
    *,
    intent: str,
    snapshot_text: str,
    constant_name: str,
    test_file: str | None,
    page_url: str | None,
) -> dict[str, Any] | None:
    """Shell out to ``worca-t resolve``. Returns the parsed JSON, or None
    on subprocess failure (caller then treats the TBD as unresolvable)."""
    cmd = os.environ.get("WORCA_T_RESOLVER_CMD", "worca-t resolve")
    cache_dir = os.environ.get("WORCA_T_CACHE_DIR")
    if not cache_dir:
        log.warning("worca_t.no_cache_dir — resolver subprocess skipped")
        return None
    # Write the snapshot to a tempfile inside the cache dir.
    snap_path = Path(cache_dir) / f"snap-{_cache_key(test_file, constant_name, intent)}.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(snapshot_text, encoding="utf-8")
    args = shlex.split(cmd) + [
        "--intent", intent,
        "--snapshot", str(snap_path),
        "--constant", constant_name,
        "--cache", cache_dir,
    ]
    if test_file:
        args += ["--test-file", test_file]
    if page_url:
        args += ["--page-url", page_url]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("worca_t.resolver_subprocess_error %s", e)
        return None
    finally:
        try:
            snap_path.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        log.warning("worca_t.resolver_subprocess_exit %d stderr=%s", proc.returncode, proc.stderr[:500])
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("worca_t.resolver_subprocess_bad_json %s stdout=%s", e, proc.stdout[:500])
        return None


# ---------------------------------------------------------------------------
# Page.locator monkey-patch
# ---------------------------------------------------------------------------


_dev_locators_cache: dict[str, _DevLocator] | None = None
_original_page_locator = None
_timeouts_inflated_for_page: set[int] = set()


def _inflate_timeouts_for_page(page: Any) -> None:
    """One-shot per page: bump default + expect timeout. Idempotent."""
    if os.environ.get("WORCA_T_INFLATE_TIMEOUTS") == "0":
        return
    pid = id(page)
    if pid in _timeouts_inflated_for_page:
        return
    _timeouts_inflated_for_page.add(pid)
    try:
        timeout_ms = int(os.environ.get("WORCA_T_DEFAULT_TIMEOUT_MS", "60000"))
    except ValueError:
        timeout_ms = 60000
    try:
        page.set_default_timeout(timeout_ms)
    except Exception as e:  # noqa: BLE001
        log.debug("worca_t.timeout_inflate_skip %s", e)


def _walk_stack_for_constant_name() -> str | None:
    """Best-effort: find a Locators-class attribute whose value would
    return this sentinel. Walks 5 frames up looking for a Python attribute
    access pattern. Returns None if not recoverable — callers degrade to
    the intent string itself as the cache-key constant component."""
    import sys
    for depth in range(2, 8):
        try:
            frame = sys._getframe(depth)
        except ValueError:
            return None
        # Look at local variables: any LOCATOR-style upper-case name pointing
        # at our sentinel is a hit.
        for name, val in list(frame.f_locals.items())[:50]:
            if name.isupper() and isinstance(val, str) and val.startswith(_SENTINEL_PREFIX):
                return name
        # Also peek at `self.locators.<NAME>` access via the source's last
        # opcode — not portable across CPython versions, so we skip.
    return None


def _snapshot_page(page: Any) -> str:
    """Capture the page AOM as JSON text. Falls back to an empty object on
    failure so the resolver can still receive a well-formed input."""
    try:
        ax = page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed %s", e)
        return "{}"


@dataclass(frozen=True)
class _Resolution:
    """Result of resolving one sentinel. Carries the source so the retry
    proxy knows whether to skip the dev file / cache when the selector
    turns out to be stale at action time."""

    selector: str | None
    source: str  # "dev" | "cached" | "agent" | "none"
    constant_name: str
    intent: str
    test_file: str | None


def _resolve_sentinel(
    page: Any, sentinel: str, *, skip_dev: bool = False, skip_cache: bool = False,
) -> _Resolution:
    """Resolve a sentinel to a real selector, consulting in order:
    dev-locators → runtime cache → resolver subprocess.

    ``skip_dev=True`` / ``skip_cache=True`` are used by the retry proxy
    when a previously-returned selector failed at action time — those
    sources are skipped so the LLM gets a fresh shot against the live
    page (and the bad entry is invalidated from the cache).
    """
    global _dev_locators_cache
    if _dev_locators_cache is None:
        _dev_locators_cache = _load_dev_locators()

    intent = parse_sentinel(sentinel)
    constant_name = _walk_stack_for_constant_name() or intent[:64]
    test_file = os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None

    # Dev-locators short-circuit (no LLM call, no cache write — verification
    # happens implicitly when the test acts on the returned locator; if the
    # action times out, the retry proxy re-enters this function with
    # skip_dev=True to fall through to LLM resolution).
    if not skip_dev and constant_name in _dev_locators_cache:
        dev = _dev_locators_cache[constant_name]
        log.info("worca_t.dev_locator_used constant=%s selector=%s", constant_name, dev.selector)
        return _Resolution(dev.selector, "dev", constant_name, intent, test_file)

    cache = _read_cache()
    key = _cache_key(test_file, constant_name, intent)
    if not skip_cache:
        cached = cache.get(key)
        if cached and cached.get("selector"):
            log.info("worca_t.cache_hit constant=%s selector=%s", constant_name, cached["selector"])
            return _Resolution(cached["selector"], "cached", constant_name, intent, test_file)

    # LLM resolution via subprocess.
    snapshot_text = _snapshot_page(page)
    page_url = None
    try:
        page_url = getattr(page, "url", None)
    except Exception:  # noqa: BLE001
        pass
    result = _call_resolver(
        intent=intent,
        snapshot_text=snapshot_text,
        constant_name=constant_name,
        test_file=test_file,
        page_url=page_url,
    )
    if result is None:
        log.warning("worca_t.resolver_failed constant=%s intent=%s", constant_name, intent)
        return _Resolution(None, "none", constant_name, intent, test_file)
    selector = result.get("selector")
    if not selector:
        log.warning(
            "worca_t.resolver_no_selector constant=%s reason=%s",
            constant_name, result.get("reason"),
        )
        return _Resolution(None, "none", constant_name, intent, test_file)
    log.info(
        "worca_t.resolver_ok constant=%s selector=%s source=%s confidence=%s",
        constant_name, selector, result.get("source"), result.get("confidence"),
    )
    return _Resolution(selector, "agent", constant_name, intent, test_file)


def _invalidate_cache_entry(constant_name: str, intent: str, test_file: str | None) -> None:
    """Remove a stale entry from the runtime cache so the next resolution
    forces a fresh LLM call. Best-effort; failures don't block the test."""
    key = _cache_key(test_file, constant_name, intent)
    cache = _read_cache()
    if key in cache:
        del cache[key]
        try:
            _write_cache(cache)
            log.info("worca_t.cache_invalidated constant=%s", constant_name)
        except OSError as e:
            log.warning("worca_t.cache_invalidate_failed %s", e)


# Locator action methods that can raise TimeoutError when the selector
# doesn't resolve to an element. Covers both Playwright's actions
# (click/fill/etc.) and its query/wait methods that block on an element
# being present. Methods not in this set pass through transparently
# (e.g. `count`, `nth`, `filter` — chainable / immediate methods).
_RETRIABLE_METHODS = frozenset({
    "click", "dblclick", "tap", "hover",
    "fill", "press", "press_sequentially", "type",
    "check", "uncheck", "set_checked", "set_input_files",
    "select_option", "select_text",
    "drag_to", "screenshot", "focus", "blur",
    "scroll_into_view_if_needed", "clear", "dispatch_event",
    "wait_for", "text_content", "inner_text", "inner_html",
    "input_value", "get_attribute", "evaluate", "evaluate_handle",
    "is_visible", "is_hidden", "is_enabled", "is_disabled",
    "is_checked", "is_editable",
})


class _RetryingLocator:
    """Thin wrapper around a Playwright Locator that retries once on
    ``TimeoutError`` by re-resolving the sentinel against the live page
    (skipping the dev file and the cache that produced the stale selector).

    Non-action attributes pass through transparently — chainable methods
    like ``nth(0)`` / ``filter(has_text=...)`` return new Locators which
    are NOT wrapped (chained access drops back to bare Playwright). That
    keeps the proxy small and avoids over-engineering nested chains; if a
    chained action fails, the user sees the underlying Playwright error
    and the existing step-9 self-heal flow still catches it.
    """

    __slots__ = (
        "_real", "_page", "_sentinel", "_resolution",
        "_locator_args", "_locator_kwargs", "_retried",
    )

    def __init__(
        self, *, real, page, sentinel, resolution,
        locator_args, locator_kwargs,
    ):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_resolution", resolution)
        object.__setattr__(self, "_locator_args", locator_args)
        object.__setattr__(self, "_locator_kwargs", locator_kwargs)
        object.__setattr__(self, "_retried", False)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<worca-t RetryingLocator wrapping {self._real!r}>"

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr) or name not in _RETRIABLE_METHODS or self._retried:
            return attr

        def _retry_wrapper(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - Playwright TimeoutError isn't always picklable to import
                if not _is_playwright_timeout(exc):
                    raise
                object.__setattr__(self, "_retried", True)
                stale = self._resolution
                log.info(
                    "worca_t.retry_on_timeout constant=%s source=%s method=%s",
                    stale.constant_name, stale.source, name,
                )
                # If the failed selector came from the dev file or cache,
                # invalidate the cache and re-resolve via LLM. Dev selectors
                # aren't in the cache, but invalidate_cache_entry is a no-op
                # in that case — safe.
                _invalidate_cache_entry(
                    stale.constant_name, stale.intent, stale.test_file,
                )
                fresh = _resolve_sentinel(
                    self._page, self._sentinel,
                    skip_dev=(stale.source == "dev"),
                    skip_cache=True,  # any cache entry would be the just-invalidated one
                )
                if fresh.selector is None:
                    log.warning(
                        "worca_t.retry_unresolvable constant=%s reason=could_not_re_resolve",
                        stale.constant_name,
                    )
                    raise  # propagate original TimeoutError
                # Build a fresh real Locator from the new selector and retry
                # the same action with the same args/kwargs.
                fresh_real = _original_page_locator(
                    self._page, fresh.selector,
                    *self._locator_args, **self._locator_kwargs,
                )
                object.__setattr__(self, "_real", fresh_real)
                object.__setattr__(self, "_resolution", fresh)
                fresh_method = getattr(fresh_real, name)
                return fresh_method(*args, **kwargs)

        return _retry_wrapper


def _is_playwright_timeout(exc: BaseException) -> bool:
    """Best-effort detector: Playwright's TimeoutError lives at
    ``playwright._impl._errors.TimeoutError`` (and ``playwright.sync_api``
    re-exports it). Checking the class name avoids brittle imports."""
    cls = type(exc)
    if cls.__name__ == "TimeoutError" and "playwright" in cls.__module__:
        return True
    # Some Playwright versions wrap timeouts in Error subclasses; the
    # message reliably contains "Timeout" + "exceeded" or "ms exceeded".
    msg = str(exc)
    if "Timeout" in msg and "exceeded" in msg:
        return True
    return False


def _wrapped_page_locator(self, selector, *args, **kwargs):
    """Replacement for ``Page.locator``. Sentinel-aware; passthrough otherwise.

    For sentinel selectors, returns a :class:`_RetryingLocator` that
    re-resolves once on ``TimeoutError`` (closes the dev-locator-staleness
    and DOM-drift gap without going through the heavier step-9 fixer agent).
    Non-sentinel selectors pass straight through to native Playwright.
    """
    _inflate_timeouts_for_page(self)
    if not is_sentinel(selector):
        return _original_page_locator(self, selector, *args, **kwargs)
    resolution = _resolve_sentinel(self, selector)
    if resolution.selector is None:
        import pytest
        pytest.fail(
            f"worca-t JIT runtime: could not resolve locator "
            f"{parse_sentinel(selector)!r}. See run.log for details."
        )
    real = _original_page_locator(self, resolution.selector, *args, **kwargs)
    return _RetryingLocator(
        real=real,
        page=self,
        sentinel=selector,
        resolution=resolution,
        locator_args=args,
        locator_kwargs=kwargs,
    )


def _install_monkey_patch() -> None:
    """Install the Page.locator wrapper. Idempotent."""
    global _original_page_locator
    if _original_page_locator is not None:
        return
    if os.environ.get("WORCA_T_DISABLE_JIT") == "1":
        log.info("worca_t.disabled_via_env")
        return
    try:
        from playwright.sync_api import Page  # type: ignore[import-untyped]
    except ImportError:
        log.warning("worca_t.playwright_not_importable — JIT runtime inactive")
        return
    _original_page_locator = Page.locator
    Page.locator = _wrapped_page_locator  # type: ignore[assignment]
    log.info("worca_t.installed")


# ---------------------------------------------------------------------------
# pytest plugin hooks
# ---------------------------------------------------------------------------


def pytest_configure(config):  # noqa: D401 - pytest hook signature
    """Install the runtime when pytest starts up."""
    _install_monkey_patch()


def pytest_sessionfinish(session, exitstatus):  # noqa: D401, ARG001 - pytest hook signature
    """Restore the original Page.locator (best-effort housekeeping)."""
    global _original_page_locator
    if _original_page_locator is not None:
        try:
            from playwright.sync_api import Page  # type: ignore[import-untyped]
            Page.locator = _original_page_locator  # type: ignore[assignment]
        except ImportError:
            pass
        _original_page_locator = None
