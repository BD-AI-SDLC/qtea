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
- ``WORCA_T_RESOLVER_PORT``     — TCP port of the parent-side ResolverServer
                                  (preferred LLM path; avoids leaking
                                  ANTHROPIC_API_KEY into the SUT subprocess)
- ``WORCA_T_RESOLVER_TOKEN``    — per-run shared secret authenticating to the
                                  ResolverServer; valid only while the parent
                                  process holds the server context manager open
- ``WORCA_T_RESOLVER_CMD``      — legacy subprocess fallback, defaults to
                                  ``worca-t resolve``; only used when
                                  WORCA_T_RESOLVER_PORT is not set
- ``WORCA_T_RESOLVER_MODEL``    — passed through to the resolver
- ``WORCA_T_RUN_ID``            — stamped into cache entries
- ``WORCA_T_DEFAULT_TIMEOUT_MS``— Playwright default timeout in ms (default 60000)
- ``WORCA_T_INFLATE_TIMEOUTS``  — set to ``0`` to opt out of timeout inflation
- ``WORCA_T_DISABLE_JIT``       — set to ``1`` to disable the monkey-patch entirely
- ``WORCA_T_NO_LLM_RESOLVE``    — set to ``1`` to disable the LLM resolver
                                  (tier 4); cache + dev-locators + in-process
                                  heuristic only. Unresolvable TBDs fail fast
                                  with a structured diagnostic instead of
                                  silently spending tokens. CI default.

Resolution tier order (highest precedence first):
  1. Dev-locators file
  2. Runtime cache
  3. In-process heuristic (AOM role+name match — zero tokens)
  4. LLM via ``worca-t resolve`` subprocess
  5. Unresolvable → ``pytest.fail`` with structured diagnostic

The plugin is a no-op on locator arguments that aren't sentinels, so SUT-
native tests in the same session run unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
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
# Resolver client
# ---------------------------------------------------------------------------
#
# The plugin talks to a small TCP server that the worca-t parent starts in
# Step 8 (``worca_t.resolver_server.ResolverServer``). The server runs in
# the TRUSTED parent process and has access to ``ANTHROPIC_API_KEY``;
# pytest itself never sees the key. Connection details are passed in via
# two env vars set by s09_execute:
#
#   - ``WORCA_T_RESOLVER_PORT``   — loopback TCP port (127.0.0.1)
#   - ``WORCA_T_RESOLVER_TOKEN``  — per-run shared secret
#
# Falls back to the legacy ``worca-t resolve`` subprocess command when
# those env vars are absent (e.g. tests run outside the worca-t pipeline,
# or against an older Step 8 that doesn't start the server). The
# subprocess path is BROKEN for first-time TBDs in worca-t-managed runs
# because ``safe_subprocess_env`` strips ``ANTHROPIC_API_KEY``; it is
# retained ONLY as an escape hatch for ad-hoc local debugging.

_SOCKET_RECV_BUFFER = 8192
_SOCKET_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB cap on server reply
_SOCKET_TIMEOUT_S = 180.0


def _read_line_from_socket(sock: socket.socket, max_bytes: int) -> bytes:
    """Read up to a newline or ``max_bytes``. Mirror of the server-side
    reader so the wire protocol stays in lockstep."""
    buf = bytearray()
    while len(buf) < max_bytes:
        chunk = sock.recv(min(_SOCKET_RECV_BUFFER, max_bytes - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl != -1:
            return bytes(buf[:nl])
    return bytes(buf)


def _call_resolver_socket(
    *,
    port: int,
    token: str,
    intent: str,
    snapshot_text: str,
    constant_name: str,
    test_file: str | None,
    page_url: str | None,
) -> dict[str, Any] | None:
    """TCP roundtrip to the parent-side ResolverServer.

    Returns the parsed response dict on success, or None on any
    transport / auth / server-side failure (caller treats None as
    unresolvable).
    """
    request = {
        "token": token,
        "intent": intent,
        "constant_name": constant_name,
        "snapshot_text": snapshot_text,
        "test_file": test_file,
        "page_url": page_url,
        "source_type": "aom",
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_SOCKET_TIMEOUT_S)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        body = _read_line_from_socket(sock, _SOCKET_MAX_RESPONSE_BYTES)
    except (OSError, ValueError) as e:
        log.warning("worca_t.resolver_socket_error %s", e)
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("worca_t.resolver_socket_bad_json %s body=%s", e, body[:500])
        return None
    if not payload.get("ok"):
        log.warning("worca_t.resolver_socket_server_error %s", payload.get("error"))
        return None
    # Project onto the legacy subprocess response shape so the rest of
    # the runtime doesn't need to change.
    return {
        "selector": payload.get("selector"),
        "strategy": payload.get("strategy"),
        "confidence": payload.get("confidence"),
        "source": payload.get("source"),
        "reason": payload.get("reason"),
        "snapshot_hash": payload.get("snapshot_hash"),
    }


def _call_resolver_subprocess(
    *,
    intent: str,
    snapshot_text: str,
    constant_name: str,
    test_file: str | None,
    page_url: str | None,
) -> dict[str, Any] | None:
    """Legacy fallback: shell out to ``worca-t resolve``.

    Retained as an escape hatch for ad-hoc debugging outside the worca-t
    pipeline. Inside the worca-t pipeline this path fails on first-time
    TBDs because ``safe_subprocess_env`` strips ``ANTHROPIC_API_KEY``
    from the inherited env — that's why we now prefer the socket bridge.
    """
    cmd = os.environ.get("WORCA_T_RESOLVER_CMD", "worca-t resolve")
    cache_dir = os.environ.get("WORCA_T_CACHE_DIR")
    if not cache_dir:
        log.warning("worca_t.no_cache_dir — resolver subprocess skipped")
        return None
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


def _call_resolver(
    *,
    intent: str,
    snapshot_text: str,
    constant_name: str,
    test_file: str | None,
    page_url: str | None,
) -> dict[str, Any] | None:
    """Dispatch to the socket bridge when available, else legacy subprocess."""
    port_env = os.environ.get("WORCA_T_RESOLVER_PORT")
    token_env = os.environ.get("WORCA_T_RESOLVER_TOKEN")
    if port_env and token_env:
        try:
            port = int(port_env)
        except ValueError:
            log.warning("worca_t.resolver_bad_port %r — falling back to subprocess", port_env)
        else:
            return _call_resolver_socket(
                port=port,
                token=token_env,
                intent=intent,
                snapshot_text=snapshot_text,
                constant_name=constant_name,
                test_file=test_file,
                page_url=page_url,
            )
    return _call_resolver_subprocess(
        intent=intent,
        snapshot_text=snapshot_text,
        constant_name=constant_name,
        test_file=test_file,
        page_url=page_url,
    )


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


def _snapshot_page(page: Any) -> tuple[str, dict[str, Any]]:
    """Capture the page AOM as ``(json_text, parsed_dict)``.

    The parsed dict feeds the in-process heuristic (tier 3) without a
    re-parse; the JSON text feeds the LLM subprocess (tier 4) without a
    re-serialize. Falls back to ``("{}", {})`` on failure so the resolver
    can still receive a well-formed input.
    """
    try:
        ax = page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed %s", e)
        return "{}", {}


# ---------------------------------------------------------------------------
# Tier-3 heuristic resolver
# ---------------------------------------------------------------------------
#
# Walks the AOM tree looking for a single high-confidence role+name match
# against the intent string. Fires BEFORE the LLM subprocess (tier 4); a
# successful match means zero tokens spent. Falls through to LLM on any
# ambiguity (multiple high-confidence candidates, no role keyword found,
# no name match), so false positives are strictly bounded.
#
# Expected hit rate: 50-70% on conventional CRUD/auth UIs where ARIA
# labelling is reasonable; 20-30% on heavy SPAs or canvas-rendered UIs.
# When in doubt, return None — the cost of one extra LLM call is far less
# than a wrong selector silently masking a real DOM issue.

# Intent words → ARIA role. Order doesn't matter; lookup is O(1).
_ROLE_KEYWORDS: dict[str, str] = {
    "button": "button", "submit": "button", "btn": "button",
    "link": "link", "anchor": "link",
    "tab": "tab",
    "input": "textbox", "field": "textbox", "textbox": "textbox", "textfield": "textbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "dropdown": "combobox", "select": "combobox", "combobox": "combobox",
    "menu": "menu", "menuitem": "menuitem",
    "heading": "heading", "title": "heading", "header": "heading",
    "image": "img", "icon": "img", "img": "img",
    "form": "form",
    "dialog": "dialog", "modal": "dialog",
    "alert": "alert", "banner": "banner",
    "list": "list", "listitem": "listitem",
    "row": "row", "cell": "cell", "columnheader": "columnheader",
    "tooltip": "tooltip",
    "tree": "tree", "treeitem": "treeitem",
    "switch": "switch", "toggle": "switch",
    "slider": "slider",
    "spinbutton": "spinbutton",
    "search": "search", "searchbox": "searchbox",
    "navigation": "navigation", "nav": "navigation",
}

# Words that bring no signal — stripped from the intent before name matching.
_NAME_FILLERS: frozenset[str] = frozenset({
    "the", "a", "an", "on", "in", "of", "for", "to", "with", "by",
    "primary", "main", "secondary",
})

# Heuristic safety thresholds:
#   - winner must score >= _HEURISTIC_MIN_SCORE
#   - runner-up (if any) must score < _HEURISTIC_TIE_GAP below winner
# These are tight on purpose — heuristic false positives produce wrong
# selectors that pass the immediate Playwright call and then mask DOM
# issues silently. Cost of being too strict: one extra LLM call (cheap).
_HEURISTIC_MIN_SCORE: float = 0.9
_HEURISTIC_TIE_GAP: float = 0.1


def _parse_intent(intent: str) -> tuple[str | None, list[str], str]:
    """Split an intent string into ``(role, name_tokens, name_hint)``.

    ``role`` is the first ARIA role keyword found (None if no keyword
    matches). ``name_tokens`` is the remaining content words (lower-case,
    fillers stripped). ``name_hint`` is the joined token string for
    substring matching.
    """
    import re as _re
    tokens = [t for t in _re.split(r"\W+", (intent or "").lower()) if t]
    role: str | None = None
    name_tokens: list[str] = []
    for t in tokens:
        if role is None and t in _ROLE_KEYWORDS:
            role = _ROLE_KEYWORDS[t]
            continue
        if t in _NAME_FILLERS:
            continue
        if t in _ROLE_KEYWORDS:
            continue
        name_tokens.append(t)
    name_hint = " ".join(name_tokens)
    return role, name_tokens, name_hint


def _aom_walk(node: Any, depth: int = 0) -> Any:
    """Depth-first walk over an AOM tree. Caps at depth 50 (AOM trees are
    shallow; the cap is a runaway-loop backstop, not a feature limit)."""
    if not isinstance(node, dict) or depth > 50:
        return
    yield node
    for child in node.get("children") or ():
        yield from _aom_walk(child, depth + 1)


def _format_role_selector(role: str, name: str) -> str:
    """Build a Playwright role-engine selector string. Escapes ``"`` in the
    name field so quoted name fragments don't break the selector parser.
    """
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'role={role}[name="{escaped}"]'


def _heuristic_resolve(intent: str, snapshot: dict[str, Any]) -> str | None:
    """Walk the AOM looking for a single high-confidence role+name match.

    Returns a Playwright ``role=<role>[name="<name>"]`` selector when a
    clear winner exists, else None. Falls through to LLM on:

    - intent has no recognised role keyword
    - no node matches with score >= :data:`_HEURISTIC_MIN_SCORE`
    - multiple candidates score within :data:`_HEURISTIC_TIE_GAP`
    - no AOM (empty snapshot)
    """
    if not snapshot:
        return None
    role, name_tokens, name_hint = _parse_intent(intent)
    if role is None or not name_tokens:
        return None

    candidates: list[tuple[float, str]] = []
    for node in _aom_walk(snapshot):
        if node.get("role") != role:
            continue
        node_name = (node.get("name") or "").lower()
        if not node_name:
            continue
        if name_hint and name_hint in node_name:
            candidates.append((1.0, node.get("name") or ""))
        elif name_tokens and all(t in node_name for t in name_tokens):
            candidates.append((0.95, node.get("name") or ""))
        elif name_tokens and any(t in node_name for t in name_tokens):
            candidates.append((0.6, node.get("name") or ""))

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    top_score, top_name = candidates[0]
    if top_score < _HEURISTIC_MIN_SCORE:
        return None
    if len(candidates) > 1 and (top_score - candidates[1][0]) < _HEURISTIC_TIE_GAP:
        return None
    return _format_role_selector(role, top_name)


@dataclass(frozen=True)
class _Resolution:
    """Result of resolving one sentinel. Carries the source so the retry
    proxy knows whether to skip the dev file / cache / heuristic when the
    selector turns out to be stale at action time."""

    selector: str | None
    source: str  # "dev" | "cached" | "heuristic" | "agent" | "none"
    constant_name: str
    intent: str
    test_file: str | None


def _resolve_tiers_1_2(
    intent: str, constant_name: str, test_file: str | None,
    *, skip_dev: bool, skip_cache: bool,
) -> _Resolution | None:
    """Check dev-locators (tier 1) and cache (tier 2). Returns a Resolution
    on hit, or None when both miss (caller proceeds to tier 3/4 with a
    fresh AOM snapshot). Pure sync — no page touch.
    """
    global _dev_locators_cache
    if _dev_locators_cache is None:
        _dev_locators_cache = _load_dev_locators()

    if not skip_dev and constant_name in _dev_locators_cache:
        dev = _dev_locators_cache[constant_name]
        log.info("worca_t.dev_locator_used constant=%s selector=%s",
                 constant_name, _sanitize_for_log(dev.selector))
        _append_spend_line({"tier": 1, "source": "dev", "constant": constant_name,
                            "input_tokens": 0, "output_tokens": 0, "success": True})
        return _Resolution(dev.selector, "dev", constant_name, intent, test_file)

    cache = _read_cache()
    key = _cache_key(test_file, constant_name, intent)
    if not skip_cache:
        cached = cache.get(key)
        if cached and cached.get("selector"):
            log.info("worca_t.cache_hit constant=%s selector=%s",
                     constant_name, _sanitize_for_log(cached["selector"]))
            _append_spend_line({"tier": 2, "source": "cached",
                                "constant": constant_name,
                                "input_tokens": 0, "output_tokens": 0, "success": True})
            return _Resolution(cached["selector"], "cached", constant_name, intent, test_file)
    return None


def _resolve_tiers_3_4(
    intent: str, constant_name: str, test_file: str | None,
    snapshot_text: str, snapshot_dict: dict[str, Any], page_url: str | None,
    *, skip_heuristic: bool,
) -> _Resolution:
    """Check heuristic (tier 3) and LLM (tier 4). Caller must have already
    captured the AOM snapshot (via either the sync or async API). Pure
    sync — the LLM call is a blocking TCP roundtrip to the parent
    ResolverServer, which is acceptable even from an async context
    because resolver latency is short (~1-2s) and Playwright itself
    blocks similarly on the same scale.
    """
    if not skip_heuristic:
        heuristic_selector = _heuristic_resolve(intent, snapshot_dict)
        if heuristic_selector:
            log.info(
                "worca_t.heuristic_hit constant=%s selector=%s",
                constant_name, _sanitize_for_log(heuristic_selector),
            )
            _append_spend_line({"tier": 3, "source": "heuristic",
                                "constant": constant_name,
                                "input_tokens": 0, "output_tokens": 0, "success": True})
            return _Resolution(
                heuristic_selector, "heuristic", constant_name, intent, test_file,
            )

    if os.environ.get("WORCA_T_NO_LLM_RESOLVE") == "1":
        log.warning(
            "worca_t.no_llm_resolve_active constant=%s intent=%s — tiers 1-3 missed",
            constant_name, intent,
        )
        return _Resolution(None, "none", constant_name, intent, test_file)

    result = _call_resolver(
        intent=intent, snapshot_text=snapshot_text,
        constant_name=constant_name, test_file=test_file, page_url=page_url,
    )
    if result is None:
        log.warning("worca_t.resolver_failed constant=%s intent=%s",
                    constant_name, intent)
        _append_spend_line({"tier": 4, "source": "none", "constant": constant_name,
                            "input_tokens": 0, "output_tokens": 0, "success": False,
                            "reason": "resolver_call_failed"})
        return _Resolution(None, "none", constant_name, intent, test_file)
    selector = result.get("selector")
    spend_entry = {
        "tier": 4, "source": result.get("source") or "agent",
        "constant": constant_name,
        "input_tokens": result.get("input_tokens") or 0,
        "output_tokens": result.get("output_tokens") or 0,
        "model": result.get("model"), "duration_ms": result.get("duration_ms"),
        "success": bool(selector),
    }
    if not selector:
        log.warning("worca_t.resolver_no_selector constant=%s reason=%s",
                    constant_name, result.get("reason"))
        spend_entry["reason"] = result.get("reason")
        _append_spend_line(spend_entry)
        return _Resolution(None, "none", constant_name, intent, test_file)
    log.info(
        "worca_t.resolver_ok constant=%s selector=%s source=%s confidence=%s",
        constant_name, _sanitize_for_log(selector),
        result.get("source"), result.get("confidence"),
    )
    _append_spend_line(spend_entry)
    return _Resolution(selector, "agent", constant_name, intent, test_file)


async def _snapshot_page_async(page: Any) -> tuple[str, dict[str, Any]]:
    """Async counterpart of ``_snapshot_page``. Awaits Playwright's
    ``page.accessibility.snapshot()`` coroutine, returns
    ``(json_text, parsed_dict)``."""
    try:
        ax = await page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed_async %s", e)
        return "{}", {}


def _safe_page_url(page: Any) -> str | None:
    """Best-effort URL probe. Async Page.url is a property (not awaitable);
    sync Page.url is also a property. We call it the same way and absorb
    any exception (e.g. page closed mid-call).
    """
    try:
        return getattr(page, "url", None)
    except Exception:  # noqa: BLE001
        return None


def _resolve_sentinel(
    page: Any, sentinel: str, *,
    skip_dev: bool = False,
    skip_cache: bool = False,
    skip_heuristic: bool = False,
) -> _Resolution:
    """Sync sentinel resolver. Tier order: dev → cache → heuristic → LLM → fail.
    Used by the sync API (``playwright.sync_api``) patch.
    """
    intent = parse_sentinel(sentinel)
    constant_name = _walk_stack_for_constant_name() or intent[:64]
    test_file = os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None

    early = _resolve_tiers_1_2(
        intent, constant_name, test_file,
        skip_dev=skip_dev, skip_cache=skip_cache,
    )
    if early is not None:
        return early

    snapshot_text, snapshot_dict = _snapshot_page(page)
    return _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, _safe_page_url(page),
        skip_heuristic=skip_heuristic,
    )


async def _resolve_sentinel_async(
    page: Any, sentinel: str, *,
    skip_dev: bool = False,
    skip_cache: bool = False,
    skip_heuristic: bool = False,
) -> _Resolution:
    """Async sentinel resolver. Same tier ladder as the sync version, but
    awaits the snapshot. Used by the async API (``playwright.async_api``)
    patch.
    """
    intent = parse_sentinel(sentinel)
    constant_name = _walk_stack_for_constant_name() or intent[:64]
    test_file = os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None

    early = _resolve_tiers_1_2(
        intent, constant_name, test_file,
        skip_dev=skip_dev, skip_cache=skip_cache,
    )
    if early is not None:
        return early

    snapshot_text, snapshot_dict = await _snapshot_page_async(page)
    return _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, _safe_page_url(page),
        skip_heuristic=skip_heuristic,
    )


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

    Works against BOTH sync and async Playwright APIs. Detection is
    per-method: when the wrapped Locator's action method is a coroutine
    function (async API), we return an async wrapper that awaits the
    call and uses :func:`_resolve_sentinel_async` for retry; otherwise
    we return the sync wrapper. Same class serves both surfaces.

    Non-action attributes pass through transparently — chainable methods
    like ``nth(0)`` / ``filter(has_text=...)`` return new Locators which
    are NOT wrapped (chained access drops back to bare Playwright). That
    keeps the proxy small and avoids over-engineering nested chains; if a
    chained action fails, the user sees the underlying Playwright error
    and the existing step-9 self-heal flow still catches it.

    ``rebuild_locator`` is a callable ``(new_selector: str) -> Locator``
    that knows how to construct a fresh Locator with the same parent
    scope (Page / Frame / parent Locator). This lets one proxy class
    serve all three locator entry points without duplicating retry logic.
    """

    __slots__ = (
        "_real", "_page", "_sentinel", "_resolution",
        "_rebuild_locator", "_retried",
    )

    def __init__(
        self, *, real, page, sentinel, resolution, rebuild_locator,
    ):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_resolution", resolution)
        object.__setattr__(self, "_rebuild_locator", rebuild_locator)
        object.__setattr__(self, "_retried", False)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<worca-t RetryingLocator wrapping {self._real!r}>"

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr) or name not in _RETRIABLE_METHODS or self._retried:
            return attr

        import asyncio

        if asyncio.iscoroutinefunction(attr):
            async def _async_retry_wrapper(*args, **kwargs):
                try:
                    return await attr(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if not _is_playwright_timeout(exc):
                        raise
                    object.__setattr__(self, "_retried", True)
                    stale = self._resolution
                    log.info(
                        "worca_t.retry_on_timeout_async constant=%s source=%s method=%s",
                        stale.constant_name, stale.source, name,
                    )
                    _invalidate_cache_entry(
                        stale.constant_name, stale.intent, stale.test_file,
                    )
                    fresh = await _resolve_sentinel_async(
                        self._page, self._sentinel,
                        skip_dev=(stale.source == "dev"),
                        skip_cache=True,
                        skip_heuristic=(stale.source == "heuristic"),
                    )
                    if fresh.selector is None:
                        log.warning(
                            "worca_t.retry_unresolvable constant=%s",
                            stale.constant_name,
                        )
                        raise
                    fresh_real = self._rebuild_locator(fresh.selector)
                    object.__setattr__(self, "_real", fresh_real)
                    object.__setattr__(self, "_resolution", fresh)
                    fresh_method = getattr(fresh_real, name)
                    return await fresh_method(*args, **kwargs)
            return _async_retry_wrapper

        def _retry_wrapper(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not _is_playwright_timeout(exc):
                    raise
                object.__setattr__(self, "_retried", True)
                stale = self._resolution
                log.info(
                    "worca_t.retry_on_timeout constant=%s source=%s method=%s",
                    stale.constant_name, stale.source, name,
                )
                _invalidate_cache_entry(
                    stale.constant_name, stale.intent, stale.test_file,
                )
                fresh = _resolve_sentinel(
                    self._page, self._sentinel,
                    skip_dev=(stale.source == "dev"),
                    skip_cache=True,
                    skip_heuristic=(stale.source == "heuristic"),
                )
                if fresh.selector is None:
                    log.warning(
                        "worca_t.retry_unresolvable constant=%s",
                        stale.constant_name,
                    )
                    raise
                fresh_real = self._rebuild_locator(fresh.selector)
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


class _AsyncLazyLocator:
    """Returned synchronously from ``async_api`` Page/Frame/Locator
    ``.locator(SENTINEL)`` calls. Defers the actual sentinel resolution
    (which needs an awaited AOM snapshot) until the first ACTION method
    call — at which point the method itself is awaited, so we can await
    resolution naturally.

    Action methods (click / fill / hover / etc.) are returned as async
    callables: they ``await _ensure_resolved()``, then forward to a
    :class:`_RetryingLocator` wrapping the real Locator (so the
    cache-invalidate-and-retry-on-TimeoutError loop fires identically
    to the sync path).

    Chainable non-action methods like ``.nth(0)`` / ``.first()`` /
    ``.filter(...)`` cannot be supported here without blocking the event
    loop on resolution, so they raise. Worca-t codegen agent is instructed
    to not chain those onto a TBD constant directly; instead, chain off a
    resolved parent (``page.locator(BUTTON).nth(0)`` works because the
    outer ``page.locator`` is the sentinel intercept and ``.nth`` runs on
    the realized child).
    """

    __slots__ = ("_page", "_sentinel", "_rebuild_locator",
                 "_resolved", "_resolved_real", "_resolved_resolution")

    def __init__(self, *, page, sentinel, rebuild_locator):
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_rebuild_locator", rebuild_locator)
        object.__setattr__(self, "_resolved", False)
        object.__setattr__(self, "_resolved_real", None)
        object.__setattr__(self, "_resolved_resolution", None)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<worca-t AsyncLazyLocator sentinel={parse_sentinel(self._sentinel)!r}>"

    async def _ensure_resolved(self):
        if self._resolved:
            return
        resolution = await _resolve_sentinel_async(self._page, self._sentinel)
        if resolution.selector is None:
            _write_hitl_pending(
                resolution.intent, resolution.constant_name,
                resolution.test_file, _safe_page_url(self._page),
            )
            import pytest
            pytest.fail(
                f"worca-t JIT runtime (async): could not resolve locator "
                f"{parse_sentinel(self._sentinel)!r}. The parent process will "
                f"surface this via HITL on the next interactive run, or as a "
                f"`locator-unresolvable` bug candidate for Step 9."
            )
        object.__setattr__(self, "_resolved_real",
                           self._rebuild_locator(resolution.selector))
        object.__setattr__(self, "_resolved_resolution", resolution)
        object.__setattr__(self, "_resolved", True)

    def __getattr__(self, name):
        if name in _RETRIABLE_METHODS:
            async def _async_action(*args, **kwargs):
                await self._ensure_resolved()
                retrying = _RetryingLocator(
                    real=self._resolved_real,
                    page=self._page,
                    sentinel=self._sentinel,
                    resolution=self._resolved_resolution,
                    rebuild_locator=self._rebuild_locator,
                )
                method = getattr(retrying, name)
                return await method(*args, **kwargs)
            return _async_action
        raise AttributeError(
            f"_AsyncLazyLocator: cannot access {name!r} before resolution. "
            f"Either call an action method first (.click/.fill/etc — those "
            f"trigger async resolve), or chain off a non-sentinel parent."
        )


# All originals captured at install time. Keys are the wrapped-class names
# so the sessionfinish hook can restore each. The Page entry is also bound
# to the legacy `_original_page_locator` module global because the retry
# proxy used to call it directly; we keep the global alias for back-compat.
_original_locator_methods: dict[str, Any] = {}


def _resolve_page_from_receiver(receiver: Any) -> Any:
    """Find the Page object that owns ``receiver`` (which may be a Page,
    Frame, or Locator). Playwright's accessibility snapshot lives on
    ``page.accessibility``; sub-objects expose a ``.page`` property /
    method that walks to the owning Page.
    """
    # Page itself — the accessibility attribute is the cheapest probe.
    if hasattr(receiver, "accessibility"):
        return receiver
    page_attr = getattr(receiver, "page", None)
    if callable(page_attr):
        try:
            return page_attr()
        except Exception:  # noqa: BLE001
            return receiver
    return page_attr or receiver


def _sanitize_for_log(s: str | None) -> str:
    """Mask selector / log values that look like secrets.

    Detects: JWT (``eyJ...`` prefix + dots), API-key-like prefixes
    (``sk-``, ``pk_``, ``Bearer ``), and long hex/base64 runs (24+
    contiguous chars of [A-Za-z0-9_/+-]). Replaces matches with
    ``***REDACTED***`` so log lines remain useful but don't leak.

    Applied at log emission sites, NEVER on values returned to the
    caller (callers need the real selector to call Playwright with).
    """
    if not s:
        return s or ""
    import re as _re
    out = s
    # JWT-like
    out = _re.sub(r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]{4,}){1,2}",
                  "***REDACTED-JWT***", out)
    # Anthropic / OpenAI / Stripe-style key prefixes
    out = _re.sub(r"\b(?:sk-|pk_|sk_|Bearer\s+)[A-Za-z0-9_\-]{16,}\b",
                  "***REDACTED-KEY***", out)
    # Long contiguous base64-ish run
    out = _re.sub(r"\b[A-Za-z0-9_/+\-]{32,}\b(?!\])",
                  "***REDACTED-BLOB***", out)
    return out


def _append_spend_line(entry: dict[str, Any]) -> None:
    """Append one resolution event to ``<cache_dir>/resolver-spend.jsonl``.

    Telemetry only — never includes the selector string, page URL, or
    snapshot body (all of which could leak secrets / tenant IDs / PII).
    Step 8 reads this file at end-of-step and rolls it up into
    ``run-results.json``'s ``resolver_spend`` summary.
    """
    cache_dir = os.environ.get("WORCA_T_CACHE_DIR")
    if not cache_dir:
        return
    path = Path(cache_dir) / "resolver-spend.jsonl"
    line = {
        "run_id": os.environ.get("WORCA_T_RUN_ID"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        log.debug("worca_t.spend_write_failed %s", e)


def _write_hitl_pending(intent: str, constant_name: str,
                        test_file: str | None, page_url: str | None) -> None:
    """Drop a ``hitl-pending-<key>.json`` file in the cache dir for the
    parent process (Step 8) to pick up after pytest exits. The parent
    prompts the user on a TTY or emits a structured ``locator-unresolvable``
    bug-candidate entry for Step 9 on non-TTY / ``--no-hitl`` runs.

    Best-effort — failure to write the file just means the parent can't
    surface the unresolved TBD as a HITL prompt; pytest will still fail
    the test with a clear diagnostic.
    """
    cache_dir = os.environ.get("WORCA_T_CACHE_DIR")
    if not cache_dir:
        return
    import hashlib
    key = hashlib.sha256(
        f"{test_file or ''}::{constant_name}::{intent}".encode("utf-8")
    ).hexdigest()[:16]
    path = Path(cache_dir) / f"hitl-pending-{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "intent": intent,
                "constant_name": constant_name,
                "test_file": test_file,
                "page_url": page_url,
                "run_id": os.environ.get("WORCA_T_RUN_ID"),
                "ts": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("worca_t.hitl_pending_written path=%s", path)
    except OSError as e:
        log.warning("worca_t.hitl_pending_write_failed %s", e)


def _wrap_async_locator_method(original: Any, _kind: str):
    """Build an async-API wrapper for ``Page.locator`` / ``Frame.locator`` /
    ``Locator.locator``. Sentinel selectors return an
    :class:`_AsyncLazyLocator` (defers resolution to first awaited action);
    non-sentinel selectors pass straight through to the original method.

    The ``.locator()`` call itself is SYNC even on async Playwright (it
    just constructs a Locator object). What's async is the snapshot call
    needed for resolution — that's deferred into the lazy-locator's
    action methods, which ARE awaited by the caller.
    """
    def wrapper(self, selector, *args, **kwargs):
        if not is_sentinel(selector):
            return original(self, selector, *args, **kwargs)
        page = _resolve_page_from_receiver(self)
        _inflate_timeouts_for_page(page)
        return _AsyncLazyLocator(
            page=page, sentinel=selector,
            rebuild_locator=lambda new_sel: original(self, new_sel, *args, **kwargs),
        )
    wrapper.__name__ = f"_wrapped_async_{_kind}_locator"
    return wrapper


def _wrap_locator_method(original: Any, _kind: str):
    """Build a wrapper for ``Page.locator`` / ``Frame.locator`` /
    ``Locator.locator``. Sentinel selectors return a :class:`_RetryingLocator`;
    non-sentinel selectors pass straight through.

    The ``rebuild_locator`` callback closes over the receiver so the retry
    proxy can construct a fresh Locator with the same parent scope.
    """
    def wrapper(self, selector, *args, **kwargs):
        page = _resolve_page_from_receiver(self)
        _inflate_timeouts_for_page(page)
        if not is_sentinel(selector):
            return original(self, selector, *args, **kwargs)
        resolution = _resolve_sentinel(page, selector)
        if resolution.selector is None:
            # Drop a hitl-pending file so the parent (Step 8) can surface
            # the unresolved TBD via HITL prompt or as a structured bug
            # candidate. Failing fast here keeps the test result clean;
            # the parent does the recovery.
            page_url = None
            try:
                page_url = getattr(page, "url", None)
                if callable(page_url):
                    page_url = page_url()
            except Exception:  # noqa: BLE001
                page_url = None
            _write_hitl_pending(
                resolution.intent, resolution.constant_name,
                resolution.test_file, page_url,
            )
            import pytest
            pytest.fail(
                f"worca-t JIT runtime: could not resolve locator "
                f"{parse_sentinel(selector)!r}. The parent process will "
                f"surface this via HITL on the next interactive run, or "
                f"as a `locator-unresolvable` bug candidate for Step 9. "
                f"See run.log for the resolution-tier trace."
            )
        real = original(self, resolution.selector, *args, **kwargs)
        return _RetryingLocator(
            real=real,
            page=page,
            sentinel=selector,
            resolution=resolution,
            rebuild_locator=lambda new_sel: original(self, new_sel, *args, **kwargs),
        )
    wrapper.__name__ = f"_wrapped_{_kind}_locator"
    return wrapper


# Kept as a legacy alias — the retry proxy used to call this directly before
# we generalised to the rebuild_locator callback. Some debugging tools / tests
# may still reference it; preserve the name so they don't break.
_original_page_locator = None  # populated by _install_monkey_patch()


def _wrapped_page_locator(self, selector, *args, **kwargs):
    """Legacy bound wrapper retained for any test that imports it by name."""
    if _original_page_locator is None:
        return self.locator(selector, *args, **kwargs)
    return _wrap_locator_method(_original_page_locator, "page")(
        self, selector, *args, **kwargs,
    )


def _install_monkey_patch() -> None:
    """Install JIT wrappers on Page.locator, Frame.locator, Locator.locator
    for BOTH sync (``playwright.sync_api``) and async (``playwright.async_api``)
    Playwright APIs. Idempotent.

    Sync path: ``_RetryingLocator`` wraps the real Locator immediately;
    action methods retry once on TimeoutError.

    Async path: ``_AsyncLazyLocator`` is returned synchronously from
    ``page.locator(SENTINEL)``; its action methods are coroutines that
    await resolution + the underlying action, with the same retry loop.

    Either API surface can be absent (depending on what the SUT installs)
    — both ``ImportError`` branches are tolerated independently.
    """
    global _original_page_locator
    if _original_locator_methods:
        return
    if os.environ.get("WORCA_T_DISABLE_JIT") == "1":
        log.info("worca_t.disabled_via_env")
        return
    patched_any = False
    # ---- Sync API ----
    try:
        from playwright.sync_api import Page, Frame, Locator  # type: ignore[import-untyped]
    except ImportError:
        log.info("worca_t.sync_api_unavailable — sync JIT inactive")
    else:
        for cls_name, cls, kind in (
            ("Page", Page, "page"), ("Frame", Frame, "frame"),
            ("Locator", Locator, "locator"),
        ):
            if not hasattr(cls, "locator"):
                continue
            original = cls.locator
            _original_locator_methods[cls_name] = original
            cls.locator = _wrap_locator_method(original, kind)  # type: ignore[assignment]
            log.info("worca_t.locator_patched class=sync.%s", cls_name)
            patched_any = True
        _original_page_locator = _original_locator_methods.get("Page")

    # ---- Async API ----
    try:
        from playwright.async_api import (  # type: ignore[import-untyped]
            Page as AsyncPage,
            Frame as AsyncFrame,
            Locator as AsyncLocator,
        )
    except ImportError:
        log.info("worca_t.async_api_unavailable — async JIT inactive")
    else:
        for cls_name, cls, kind in (
            ("AsyncPage", AsyncPage, "page"), ("AsyncFrame", AsyncFrame, "frame"),
            ("AsyncLocator", AsyncLocator, "locator"),
        ):
            if not hasattr(cls, "locator"):
                continue
            original = cls.locator
            _original_locator_methods[cls_name] = original
            cls.locator = _wrap_async_locator_method(original, kind)  # type: ignore[assignment]
            log.info("worca_t.locator_patched class=async.%s", cls_name)
            patched_any = True

    if not patched_any:
        log.warning(
            "worca_t.playwright_not_importable — JIT runtime inactive "
            "(neither sync_api nor async_api importable)"
        )
    else:
        log.info("worca_t.installed classes=%s", list(_original_locator_methods))


# ---------------------------------------------------------------------------
# pytest plugin hooks
# ---------------------------------------------------------------------------


_WORCA_PHASE_MARKERS = ("smoke", "regression", "e2e", "exploratory")


def pytest_configure(config):  # noqa: D401 - pytest hook signature
    """Install the runtime when pytest starts up.

    Also registers ``worca_<phase>`` markers so SUTs running with
    ``--strict-markers`` don't reject the attribution decorators that
    Step 7 codegen applies to every generated test. Step 8 uses these
    markers to scope pytest selection to worca-generated tests only
    (``-m "worca_smoke or worca_regression or ..."``) without dragging
    in the SUT's native suite.
    """
    _install_monkey_patch()
    for phase in _WORCA_PHASE_MARKERS:
        config.addinivalue_line(
            "markers",
            f"worca_{phase}: worca-t generated {phase} test",
        )


def pytest_sessionfinish(session, exitstatus):  # noqa: D401, ARG001 - pytest hook signature
    """Restore the originals on sync + async Page/Frame/Locator.locator
    (best-effort housekeeping)."""
    global _original_page_locator
    if not _original_locator_methods:
        return
    # Sync API
    try:
        from playwright.sync_api import Page, Frame, Locator  # type: ignore[import-untyped]
        for cls_name, cls in (("Page", Page), ("Frame", Frame), ("Locator", Locator)):
            original = _original_locator_methods.get(cls_name)
            if original is not None and hasattr(cls, "locator"):
                cls.locator = original  # type: ignore[assignment]
    except ImportError:
        pass
    # Async API
    try:
        from playwright.async_api import (  # type: ignore[import-untyped]
            Page as AsyncPage, Frame as AsyncFrame, Locator as AsyncLocator,
        )
        for cls_name, cls in (
            ("AsyncPage", AsyncPage), ("AsyncFrame", AsyncFrame),
            ("AsyncLocator", AsyncLocator),
        ):
            original = _original_locator_methods.get(cls_name)
            if original is not None and hasattr(cls, "locator"):
                cls.locator = original  # type: ignore[assignment]
    except ImportError:
        pass
    _original_locator_methods.clear()
    _original_page_locator = None
