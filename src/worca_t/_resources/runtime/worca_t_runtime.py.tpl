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
- ``WORCA_T_PROXY``             — proxy URL to inject into Chromium launches
                                  (``proxy={'server': URL}``). Overrides
                                  ``HTTPS_PROXY`` when both are set.
- ``HTTPS_PROXY`` / ``https_proxy`` — fallback proxy source when
                                  ``WORCA_T_PROXY`` is unset. Worca-t already
                                  propagates these into the subprocess via
                                  ``with_proxy_env`` (which reads
                                  ``HKCU:\\Environment`` on Windows). Required
                                  on corporate networks where the SUT's target
                                  hostname is only resolvable via the corp
                                  proxy (e.g. ``*.bosch.com`` via px@3128).
                                  An SUT that explicitly passes ``proxy=`` to
                                  ``launch()`` wins — the injection is a
                                  "default-when-absent" only.
- ``WORCA_T_DISABLE_PROXY_INJECT`` — set to ``1`` to disable the proxy
                                  injection patch entirely (locator JIT patch
                                  is unaffected — the two patches are
                                  orthogonal).
- ``WORCA_T_WORKSPACE_DIR``     — worca-t run workspace directory. When set,
                                  the runtime captures Playwright
                                  ``context.storage_state(path=<dir>/storage-
                                  state.json)`` on the first passing test
                                  (Use case B for the Step 9 heal flow —
                                  same-run storage-state handover). Auto-set
                                  by Step 9; unset in standalone pytest runs
                                  (no capture happens).

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

# Lazy Playwright Locator bases for the proxy subclassing. We import at module
# scope (not lazily inside `_install_monkey_patch`) so the proxy classes can
# inherit from the real Locator — this is what makes
# ``isinstance(proxy, playwright.sync_api.Locator)`` return True, which is the
# discriminator Playwright's ``expect._dispatch`` uses before reaching for
# ``actual._impl_obj``. Without that, ``expect(page.locator(tbd_sentinel))``
# raises ``ValueError: Unsupported type``. Falling back to ``object`` keeps the
# template importable on non-Playwright SUTs (sentinel codepaths simply never
# fire in that case).
try:
    from playwright.sync_api import Locator as _SyncLocatorBase  # type: ignore[import-untyped]
except ImportError:
    _SyncLocatorBase = object  # type: ignore[assignment,misc]

try:
    from playwright.async_api import Locator as _AsyncLocatorBase  # type: ignore[import-untyped]
except ImportError:
    _AsyncLocatorBase = object  # type: ignore[assignment,misc]

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
    page_url: str | None = None


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
                page_url=entry.get("page_url") if isinstance(entry.get("page_url"), str) else None,
            )
        pool_count = sum(1 for d in out.values() if d.intent)
        log.info(
            "worca_t.dev_locators_loaded path=%s count=%d pool_entries=%d",
            p, len(out), pool_count,
        )
        return out
    return {}


# ---------------------------------------------------------------------------
# Tier 1b: intent-based pool match (vendored pure-stdlib fuzzy matcher)
# ---------------------------------------------------------------------------
#
# When a dev-locator entry carries an ``intent`` field, the runtime can
# match it against the ``tbd("...")`` intent string instead of requiring an
# exact constant-name match. This lets frontend devs ship dev-locators.json
# with their own arbitrary key names; the match key becomes the human
# description, not the JSON key.
#
# Why token-set-ratio and not rapidfuzz/embeddings: the runtime template is
# vendored into the SUT and must run on the SUT's pure-stdlib Python. Adding
# an external dep (rapidfuzz) would force SUT-side installs; embeddings need
# a model download. Token-set-ratio is good enough for short descriptive
# strings and is deterministic + reproducible across runs.

# Tokenizer: split on non-alphanumeric, lowercase, drop common English
# stop-words and length-1 tokens (noise after splitting). Stop-word list is
# deliberately tiny — only the words that appear in nearly every UI element
# description without adding signal. Light stemming (trailing s/es/ed/ing)
# normalizes "submit"/"submits", "click"/"clicks", etc.
_TOKEN_SPLIT_RE = None  # lazy-compiled
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "for", "on", "in", "at", "is", "are",
    "and", "or", "with", "by", "that", "this", "it", "be",
})


def _stem(tok: str) -> str:
    # Cheap suffix strip — good enough for verb tense + plural normalization
    # on short English descriptors. Skips short tokens to avoid mutilating them.
    if len(tok) <= 3:
        return tok
    for suf in ("ing", "ies", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _tokenize(text: str) -> set[str]:
    global _TOKEN_SPLIT_RE
    if _TOKEN_SPLIT_RE is None:
        import re
        _TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
    raw = _TOKEN_SPLIT_RE.split((text or "").lower())
    return {
        _stem(t) for t in raw
        if len(t) >= 2 and t not in _STOPWORDS
    }


def _token_set_ratio(a: str, b: str) -> float:
    """Symmetric token-set similarity in [0.0, 1.0].

    Computes ``2 * |A ∩ B| / (|A| + |B|)`` over the tokenized sets — the
    Sørensen-Dice coefficient. Robust to word reordering and partial
    overlaps, which are the dominant variation between two human
    descriptions of the same UI element.
    """
    sa, sb = _tokenize(a), _tokenize(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return (2.0 * inter) / (len(sa) + len(sb))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _pool_match(
    intent: str, page_url: str | None, pool: dict[str, _DevLocator],
) -> tuple[_DevLocator | None, float, str, list[tuple[str, float]]]:
    """Return ``(winner, score, reason, top_candidates)``.

    ``reason`` is one of ``accept`` | ``reject_low_score`` | ``reject_tie``
    | ``reject_empty_pool``. ``top_candidates`` lists the top 3
    ``(constant_name, score)`` pairs for telemetry/tuning. ``winner`` is
    non-None only when ``reason == "accept"``.

    Thresholds (env-overridable for tuning without rebuilding):
      - ``WORCA_T_DEV_POOL_THRESHOLD`` — min accepted score (default 0.65).
        Tuned for short descriptive intents after stop-word + light stemming
        normalization. Lower = more matches but more false positives; higher
        = stricter. The margin requirement is the primary safety net.
      - ``WORCA_T_DEV_POOL_MARGIN``    — required gap to second-best (default 0.10).
        This is what protects against ambiguous matches when multiple pool
        entries describe similar elements.
      - ``WORCA_T_DEV_POOL_PAGE_PENALTY`` — score subtracted when entry.page_url
                                            is set and differs from current page
                                            (default 0.15; soft penalty, not a filter).
    """
    threshold = _env_float("WORCA_T_DEV_POOL_THRESHOLD", 0.65)
    margin = _env_float("WORCA_T_DEV_POOL_MARGIN", 0.10)
    page_penalty = _env_float("WORCA_T_DEV_POOL_PAGE_PENALTY", 0.15)

    pool_entries = [d for d in pool.values() if d.intent]
    if not pool_entries:
        return None, 0.0, "reject_empty_pool", []

    scored: list[tuple[_DevLocator, float]] = []
    for entry in pool_entries:
        score = _token_set_ratio(intent, entry.intent or "")
        if (
            page_penalty > 0.0 and entry.page_url and page_url
            and entry.page_url != page_url
        ):
            score = max(0.0, score - page_penalty)
        scored.append((entry, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    top = [(e.constant_name, round(s, 3)) for e, s in scored[:3]]
    best_entry, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0

    if best_score < threshold:
        return None, best_score, "reject_low_score", top
    if (best_score - second_score) < margin:
        return None, best_score, "reject_tie", top
    return best_entry, best_score, "accept", top


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
    # the runtime doesn't need to change. `candidates` is included when
    # present — newer ResolverServer responses carry a ranked bundle that
    # the retry proxy uses as zero-cost fallback alternates.
    return {
        "selector": payload.get("selector"),
        "strategy": payload.get("strategy"),
        "confidence": payload.get("confidence"),
        "source": payload.get("source"),
        "reason": payload.get("reason"),
        "snapshot_hash": payload.get("snapshot_hash"),
        "candidates": payload.get("candidates"),
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
        "model": payload.get("model"),
        "duration_ms": payload.get("duration_ms"),
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


def _call_aria_snapshot_sync(body_locator: Any) -> str:
    """Call ``Locator.aria_snapshot()`` preferring ``mode="ai"`` (added in
    Playwright 1.59 — returns an LLM-optimized YAML tree). Falls back to
    the no-mode call when the SUT pins an older Playwright that rejects the
    kwarg (``TypeError`` from the generated stub signature).

    Returns the empty string on a falsy / unexpected return.
    """
    try:
        return body_locator.aria_snapshot(mode="ai") or ""
    except TypeError:
        # Older Playwright (1.40-1.58): no `mode` parameter.
        return body_locator.aria_snapshot() or ""


async def _call_aria_snapshot_async(body_locator: Any) -> str:
    """Async counterpart of :func:`_call_aria_snapshot_sync`."""
    try:
        return await body_locator.aria_snapshot(mode="ai") or ""
    except TypeError:
        return await body_locator.aria_snapshot() or ""


def _snapshot_page(page: Any) -> tuple[str, dict[str, Any]]:
    """Capture the page AOM as ``(text, parsed_dict_tree)``.

    Strategy:
      1. ``page.locator('body').aria_snapshot(mode="ai")`` — Playwright 1.59+,
         LLM-optimized YAML tree. ``mode="ai"`` was added in v1.59 (verified
         against the Python docs); the wrapper falls back to a no-mode call
         on ``TypeError`` so SUTs on 1.40-1.58 still get a snapshot.
      2. ``page.accessibility.snapshot()`` — pre-1.40 API, returns a dict
         directly. Removed in Playwright 1.40+; only reached when locator-
         based capture has already failed.

    Returns ``("", {})`` on total failure so the resolver still receives a
    well-formed input (the LLM tier then cleanly returns "no candidates"
    instead of crashing). Errors are logged but never propagate.
    """
    # ---- Primary: Locator.aria_snapshot (Playwright 1.40+) ----
    try:
        body = page.locator("body")
        snapshot_text = _call_aria_snapshot_sync(body)
        snapshot_dict = _parse_aria_snapshot_yaml(snapshot_text)
        return snapshot_text, snapshot_dict
    except AttributeError:
        # `Page.locator` or `Locator.aria_snapshot` not present — fall through.
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed_aria %s", e)
        # Fall through to legacy API — older Playwright might still work.

    # ---- Legacy: page.accessibility.snapshot() (Playwright <1.40) ----
    try:
        ax = page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed %s", e)
        return "", {}


# YAML-ish ARIA tree parser. Playwright's ``Locator.aria_snapshot()`` emits
# a structured but non-standard YAML format. We parse it into the same
# ``{role, name, children}`` shape ``_aom_walk`` expects so the tier-3
# heuristic works without re-implementing for two formats.
#
# Format examples (indentation = 2 spaces per level):
#   - button "Next"                  → role=button, name="Next"
#   - heading "Sign in" [level=1]    → role=heading, name="Sign in"
#   - alert                          → role=alert, name=""
#   - alert: Error message           → role=alert, name="Error message"
#   - main:                          → role=main, name="", has children
#     - paragraph: Welcome           →   child of main
#   - /url: /help                    → attribute metadata, SKIPPED (not a node)


def _parse_aria_snapshot_yaml(yaml_text: str) -> dict[str, Any]:
    """Parse a ``Locator.aria_snapshot()`` YAML body into a dict tree
    ``{role, name, children: [...]}`` compatible with :func:`_aom_walk`.

    Returns ``{}`` for empty input so callers can rely on truthy checks.

    Pure-function: no Playwright import, no I/O. Unit-testable standalone.
    """
    import re as _re

    if not yaml_text or not yaml_text.strip():
        return {}

    # Regexes anchored once, not per-line.
    _RE_QUOTED = _re.compile(r'^([A-Za-z][A-Za-z0-9_-]*)\s+"((?:[^"\\]|\\.)*)"(.*)$')
    _RE_INLINE = _re.compile(r'^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$')
    _RE_ROLE_ONLY = _re.compile(r'^([A-Za-z][A-Za-z0-9_-]*).*$')

    root_children: list[dict[str, Any]] = []
    # Stack: list of (indent, child_list_to_append_into).
    stack: list[tuple[int, list[dict[str, Any]]]] = [(-1, root_children)]

    for raw_line in yaml_text.split("\n"):
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        # Skip attribute metadata lines (start with '/' after the dash).
        if body.startswith("/"):
            continue
        # Strip trailing ":" — it just indicates the node has children/attrs;
        # we infer that from indent-level changes anyway.
        had_trailing_colon = body.endswith(":")
        if had_trailing_colon:
            body = body[:-1].rstrip()

        # Try in order: quoted name, inline text after ":", bare role.
        name = ""
        m = _RE_QUOTED.match(body)
        if m:
            role = m.group(1)
            name = m.group(2)
        else:
            m2 = _RE_INLINE.match(body)
            if m2:
                role = m2.group(1)
                inline_text = m2.group(2).strip()
                if inline_text:
                    name = inline_text
            else:
                m3 = _RE_ROLE_ONLY.match(body)
                if not m3:
                    continue
                role = m3.group(1)

        node: dict[str, Any] = {"role": role, "name": name, "children": []}
        # Find parent: pop until we hit something with a strictly smaller indent.
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent_children = stack[-1][1]
        parent_children.append(node)
        stack.append((indent, node["children"]))

    return {"role": "document", "name": "", "children": root_children}


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
    selector turns out to be stale at action time.

    ``candidates`` carries the LLM's ranked bundle (primary + optional
    fallback) for tier 4 / cached-tier 4 resolutions. ``selector`` mirrors
    ``candidates[0]['selector']`` when the bundle is present, but the
    retry proxy uses ``candidates[1:]`` as zero-cost fallback alternates
    on ``TimeoutError`` before invalidating the cache and re-calling the
    resolver. ``None`` for dev / heuristic / failed resolutions.
    """

    selector: str | None
    source: str  # "dev" | "dev-pool" | "cached" | "heuristic" | "agent" | "none"
    constant_name: str
    intent: str
    test_file: str | None
    candidates: tuple[dict[str, Any], ...] | None = None


def _resolve_tiers_1_2(
    intent: str, constant_name: str, test_file: str | None,
    page_url: str | None,
    *, skip_dev: bool, skip_cache: bool,
) -> _Resolution | None:
    """Check dev-locators (tier 1a exact + 1b intent pool) and cache (tier 2).
    Returns a Resolution on hit, or None when all miss (caller proceeds to
    tier 3/4 with a fresh AOM snapshot). Pure sync — no page touch.

    ``page_url`` is the current page URL (best-effort; may be None on the
    first navigation). Used as a soft disambiguator in Tier 1b pool match.
    """
    global _dev_locators_cache
    if _dev_locators_cache is None:
        _dev_locators_cache = _load_dev_locators()

    # Tier 1a: exact constant-name match. Preserves HITL-replay behavior and
    # the fast path for devs who happen to use the codegen-produced names.
    if not skip_dev and constant_name in _dev_locators_cache:
        dev = _dev_locators_cache[constant_name]
        log.info("worca_t.dev_locator_used constant=%s selector=%s",
                 constant_name, _sanitize_for_log(dev.selector))
        _append_spend_line({"tier": 1, "source": "dev", "constant": constant_name,
                            "input_tokens": 0, "output_tokens": 0, "success": True})
        return _Resolution(dev.selector, "dev", constant_name, intent, test_file)

    # Tier 1b: intent-based pool match. Activates when the file contains
    # entries with an ``intent`` field (frontend-dev-supplied selector pool).
    # Deterministic + zero-LLM; honors WORCA_T_NO_LLM_RESOLVE=1 semantics.
    if not skip_dev and _dev_locators_cache:
        winner, score, reason, top = _pool_match(
            intent, page_url, _dev_locators_cache,
        )
        if reason == "accept" and winner is not None:
            log.info(
                "worca_t.dev_pool_match constant=%s matched=%s score=%.3f selector=%s",
                constant_name, winner.constant_name, score,
                _sanitize_for_log(winner.selector),
            )
            _append_spend_line({
                "tier": 1, "source": "dev-pool", "constant": constant_name,
                "matched_constant": winner.constant_name, "score": round(score, 3),
                "input_tokens": 0, "output_tokens": 0, "success": True,
            })
            # Write to Tier 2 cache so subsequent resolutions of the same
            # constant skip fuzzy work entirely.
            try:
                cache_for_write = _read_cache()
                key_for_write = _cache_key(test_file, constant_name, intent)
                cache_for_write[key_for_write] = {
                    "key": key_for_write,
                    "test_file": test_file,
                    "constant_name": constant_name,
                    "intent": intent,
                    "selector": winner.selector,
                    "strategy": winner.strategy,
                    "source": "dev-pool",
                    "page_url": page_url,
                    "matched_constant": winner.constant_name,
                    "pool_score": round(score, 3),
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                _write_cache(cache_for_write)
            except OSError as e:
                log.warning("worca_t.dev_pool_cache_write_failed %s", e)
            return _Resolution(
                winner.selector, "dev-pool", constant_name, intent, test_file,
            )
        elif reason in ("reject_low_score", "reject_tie"):
            # Structured rejection telemetry — feeds threshold tuning. No
            # spend line (no successful resolution) but log surface mirrors
            # the dev_pool_match log shape for grep parity.
            log.info(
                "worca_t.dev_pool_reject constant=%s reason=%s best=%.3f top=%s",
                constant_name, reason, score, top,
            )

    cache = _read_cache()
    key = _cache_key(test_file, constant_name, intent)
    if not skip_cache:
        cached = cache.get(key)
        if cached and cached.get("selector"):
            log.info("worca_t.cache_hit constant=%s selector=%s",
                     constant_name, _sanitize_for_log(cached["selector"]))
            cached_bundle = cached.get("candidates")
            bundle_tuple = (
                tuple(cached_bundle)
                if isinstance(cached_bundle, list) and cached_bundle
                else None
            )
            _append_spend_line({"tier": 2, "source": "cached",
                                "constant": constant_name,
                                "candidates_count": len(bundle_tuple) if bundle_tuple else 1,
                                "input_tokens": 0, "output_tokens": 0, "success": True})
            return _Resolution(
                cached["selector"], "cached", constant_name, intent, test_file,
                candidates=bundle_tuple,
            )
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
    raw_candidates = result.get("candidates")
    bundle_tuple = (
        tuple(raw_candidates)
        if isinstance(raw_candidates, list) and raw_candidates
        else None
    )
    spend_entry = {
        "tier": 4, "source": result.get("source") or "agent",
        "constant": constant_name,
        "candidates_count": len(bundle_tuple) if bundle_tuple else (1 if selector else 0),
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
        "worca_t.resolver_ok constant=%s selector=%s source=%s confidence=%s candidates=%d",
        constant_name, _sanitize_for_log(selector),
        result.get("source"), result.get("confidence"),
        len(bundle_tuple) if bundle_tuple else 1,
    )
    _append_spend_line(spend_entry)
    return _Resolution(
        selector, "agent", constant_name, intent, test_file,
        candidates=bundle_tuple,
    )


async def _snapshot_page_async(page: Any) -> tuple[str, dict[str, Any]]:
    """Async counterpart of :func:`_snapshot_page`. Same fallback chain:
    aria_snapshot(mode="ai") → aria_snapshot() → legacy
    page.accessibility.snapshot().
    """
    # ---- Primary: Locator.aria_snapshot (Playwright 1.40+) ----
    try:
        body = page.locator("body")
        snapshot_text = await _call_aria_snapshot_async(body)
        snapshot_dict = _parse_aria_snapshot_yaml(snapshot_text)
        return snapshot_text, snapshot_dict
    except AttributeError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed_aria_async %s", e)

    # ---- Legacy: page.accessibility.snapshot() (Playwright <1.40) ----
    try:
        ax = await page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("worca_t.snapshot_failed_async %s", e)
        return "", {}


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
    current_url = _safe_page_url(page)

    early = _resolve_tiers_1_2(
        intent, constant_name, test_file, current_url,
        skip_dev=skip_dev, skip_cache=skip_cache,
    )
    if early is not None:
        return early

    snapshot_text, snapshot_dict = _snapshot_page(page)
    return _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, current_url,
        skip_heuristic=skip_heuristic,
    )


async def _resolve_sentinel_async(
    page: Any, sentinel: str, *,
    constant_name: str | None = None,
    skip_dev: bool = False,
    skip_cache: bool = False,
    skip_heuristic: bool = False,
) -> _Resolution:
    """Async sentinel resolver. Same tier ladder as the sync version, but
    awaits the snapshot. Used by the async API (``playwright.async_api``)
    patch.

    ``constant_name`` is captured EAGERLY at ``.locator()`` call time by the
    async wrapper and threaded through here, because on the async path the
    actual resolution is deferred to action time — by which point the
    ``.locator()`` call frame (the only place a ``LOCATOR``-style local points
    at the sentinel) has already returned and the stack walk would miss it,
    falling back to ``intent[:64]`` and breaking dev-locator/HITL keying.
    """
    intent = parse_sentinel(sentinel)
    if constant_name is None:
        constant_name = _walk_stack_for_constant_name()
    constant_name = constant_name or intent[:64]
    test_file = os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None
    current_url = _safe_page_url(page)

    early = _resolve_tiers_1_2(
        intent, constant_name, test_file, current_url,
        skip_dev=skip_dev, skip_cache=skip_cache,
    )
    if early is not None:
        return early

    snapshot_text, snapshot_dict = await _snapshot_page_async(page)
    return _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, current_url,
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


def _promote_candidate_in_cache(
    constant_name: str,
    intent: str,
    test_file: str | None,
    working: dict[str, Any],
) -> None:
    """Rewrite the cache entry so the working fallback becomes the primary
    (and only) candidate. Called after a fallback survives an action that
    timed out under the original primary — the failed primary is dropped
    on the theory that it timed out under the inflated 60s timeout and
    is therefore broken rather than slow. Best-effort; failures don't
    block the test."""
    key = _cache_key(test_file, constant_name, intent)
    cache = _read_cache()
    entry = cache.get(key)
    if not entry:
        return
    entry["selector"] = working.get("selector")
    entry["strategy"] = working.get("strategy")
    entry["confidence"] = working.get("confidence")
    entry["candidates"] = [working]
    cache[key] = entry
    try:
        _write_cache(cache)
        _append_spend_line({
            "tier": 2, "source": "promoted", "constant": constant_name,
            "input_tokens": 0, "output_tokens": 0, "success": True,
            "fallback_promoted": True,
        })
        log.info(
            "worca_t.fallback_promoted constant=%s selector=%s",
            constant_name, _sanitize_for_log(str(working.get("selector"))),
        )
    except OSError as e:
        log.warning("worca_t.fallback_promote_failed %s", e)


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


class _RetryingLocator(_SyncLocatorBase):
    """Thin wrapper around a Playwright Locator that, on ``TimeoutError``,
    first walks any remaining LLM-supplied fallback candidates against the
    live page (zero token cost) and only falls back to invalidating the
    cache + re-resolving via the LLM if every candidate in the bundle has
    been exhausted.

    Works against BOTH sync and async Playwright APIs. Detection is
    per-method: when the wrapped Locator's action method is a coroutine
    function (async API), we return an async wrapper that awaits the
    call and uses :func:`_resolve_sentinel_async` for the (terminal)
    re-resolve; otherwise we return the sync wrapper. Same class serves
    both surfaces.

    Bundle promotion: when a fallback candidate succeeds, the cache entry
    is rewritten with the working candidate as the sole entry (the failed
    primary is dropped). Next test reusing the same cache key picks the
    fallback up directly as the primary — no second-attempt cost.

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

    Subclasses ``playwright.sync_api.Locator`` so that
    ``isinstance(proxy, Locator)`` is True — Playwright's
    ``expect._dispatch`` uses that check before reaching for
    ``actual._impl_obj`` to build a ``LocatorAssertionsImpl``. We mirror
    ``_impl_obj`` from the wrapped real Locator so the assertion impl
    talks to the right element. ``__slots__`` is intentionally absent:
    the parent class has a ``__dict__``, so slots in the child are
    decorative and complicate the subclass story for no real win.
    """

    def __init__(
        self, *, real, page, sentinel, resolution, rebuild_locator,
    ):
        # Mirror Playwright's internal handle so isinstance-gated callers
        # (notably ``expect._dispatch`` and any pytest-playwright fixture)
        # can pull our proxy through their normal codepath. We bypass
        # ``_SyncLocatorBase.__init__`` to avoid touching Playwright's
        # internal ``_loop`` / ``_dispatcher_fiber`` plumbing — every
        # method call on the proxy delegates through ``__getattr__`` to
        # ``self._real``, which has its own correctly-initialised state.
        # ``getattr`` with default keeps test fakes (SimpleNamespace) that
        # have no ``_impl_obj`` working — the attribute simply stays unset.
        _impl = getattr(real, "_impl_obj", None)
        if _impl is not None:
            object.__setattr__(self, "_impl_obj", _impl)
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_resolution", resolution)
        object.__setattr__(self, "_rebuild_locator", rebuild_locator)
        object.__setattr__(self, "_retried", False)
        # candidates[0] is what's already wrapped in `_real`; everything
        # past it is a fallback the retry path can try without a new
        # resolver call. None / single-entry bundles → empty list, which
        # means the proxy falls straight through to the existing LLM
        # re-resolve path on TimeoutError (no behaviour change).
        bundle = resolution.candidates
        remaining: list[dict[str, Any]] = (
            list(bundle[1:])
            if bundle is not None and len(bundle) > 1
            else []
        )
        object.__setattr__(self, "_remaining_candidates", remaining)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<worca-t RetryingLocator wrapping {self._real!r}>"

    def _swap_real(self, fresh_real):
        """Swap the wrapped real Locator and re-mirror its ``_impl_obj``.

        Necessary because a later ``expect(proxy)`` call dispatches against
        whatever ``_impl_obj`` is on the proxy at that moment — if we
        rebuilt ``_real`` (via fallback candidate or LLM re-resolve)
        without re-mirroring, ``expect`` would still talk to the original
        stale element. ``getattr(..., None)`` keeps test fakes that lack
        ``_impl_obj`` working: the slot simply retains its prior value."""
        object.__setattr__(self, "_real", fresh_real)
        _impl = getattr(fresh_real, "_impl_obj", None)
        if _impl is not None:
            object.__setattr__(self, "_impl_obj", _impl)

    def _try_next_candidate(self):
        """Pop and apply the next fallback candidate, swapping `_real` to
        a locator built from its selector. Returns the candidate dict so
        the caller can promote it in the cache on success, or ``None``
        when the bundle is exhausted."""
        if not self._remaining_candidates:
            return None
        nxt = self._remaining_candidates.pop(0)
        sel = nxt.get("selector")
        if not isinstance(sel, str) or not sel.strip():
            return None
        fresh_real = self._rebuild_locator(sel)
        self._swap_real(fresh_real)
        log.info(
            "worca_t.fallback_candidate_try constant=%s selector=%s strategy=%s",
            self._resolution.constant_name,
            _sanitize_for_log(sel),
            nxt.get("strategy"),
        )
        return nxt

    def __getattribute__(self, name):
        # We subclass ``_SyncLocatorBase`` for the isinstance contract, which
        # means inherited ``Locator.click`` / ``Locator.count`` / ``Locator.nth``
        # would normally win attribute lookup BEFORE ``__getattr__`` ever
        # fires — and they would route into ``self._impl_obj`` directly,
        # bypassing our retry wrapping and breaking proxies built around
        # test fakes that have no ``_impl_obj``. Override
        # ``__getattribute__`` so the proxy keeps its bare-delegation
        # semantics regardless of what the parent class defines.
        #
        # Routing rules:
        #   - underscored names (our state, private helpers, all dunders)
        #     → normal MRO lookup via ``object.__getattribute__``.
        #   - ``_RETRIABLE_METHODS`` → fall through to ``__getattr__``
        #     so the existing retry wrapper builds around ``self._real``.
        #   - everything else (chainable ``.nth`` / ``.filter`` / misc)
        #     → delegate transparently to ``self._real``, matching the
        #     original bare-proxy behaviour the docstring documents.
        if name.startswith('_'):
            return object.__getattribute__(self, name)
        if name in _RETRIABLE_METHODS:
            # ``type(self).__getattr__(self, name)`` accesses the method on
            # the class (not via instance ``__getattribute__``), so no
            # recursion. ``__getattr__`` then uses ``object.__getattribute__``
            # internally to reach our slots.
            return type(self).__getattr__(self, name)
        real = object.__getattribute__(self, '_real')
        return getattr(real, name)

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr) or name not in _RETRIABLE_METHODS:
            return attr

        # Once the LLM re-resolve has fired and its replacement also fails,
        # we propagate — no further retries (matches the historical
        # "only retries once" invariant; the candidate-walk happens BEFORE
        # the re-resolve and is bounded by bundle size).
        if self._retried:
            return attr

        import asyncio

        if asyncio.iscoroutinefunction(attr):
            async def _async_retry_wrapper(*args, **kwargs):
                # Walk any in-bundle fallbacks first (zero-cost resilience).
                while True:
                    try:
                        result = await getattr(self._real, name)(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        if not _is_playwright_timeout(exc):
                            raise
                        stale = self._resolution
                        log.info(
                            "worca_t.retry_on_timeout_async constant=%s source=%s method=%s remaining=%d",
                            stale.constant_name, stale.source, name,
                            len(self._remaining_candidates),
                        )
                        nxt = self._try_next_candidate()
                        if nxt is not None:
                            continue  # retry against the fallback candidate
                        # Bundle exhausted (or never existed) → LLM re-resolve.
                        object.__setattr__(self, "_retried", True)
                        _invalidate_cache_entry(
                            stale.constant_name, stale.intent, stale.test_file,
                        )
                        fresh = await _resolve_sentinel_async(
                            self._page, self._sentinel,
                            constant_name=stale.constant_name,
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
                        self._swap_real(fresh_real)
                        object.__setattr__(self, "_resolution", fresh)
                        fresh_method = getattr(fresh_real, name)
                        return await fresh_method(*args, **kwargs)
                    # success — if it came from a fallback candidate, promote
                    # it so subsequent tests skip the failed primary entirely.
                    stale = self._resolution
                    if (
                        stale.candidates
                        and len(self._remaining_candidates) < len(stale.candidates) - 1
                    ):
                        # _remaining_candidates shrank, meaning a fallback was used.
                        used_idx = len(stale.candidates) - 1 - len(self._remaining_candidates)
                        _promote_candidate_in_cache(
                            stale.constant_name, stale.intent, stale.test_file,
                            stale.candidates[used_idx],
                        )
                    return result
            return _async_retry_wrapper

        def _retry_wrapper(*args, **kwargs):
            while True:
                try:
                    result = getattr(self._real, name)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if not _is_playwright_timeout(exc):
                        raise
                    stale = self._resolution
                    log.info(
                        "worca_t.retry_on_timeout constant=%s source=%s method=%s remaining=%d",
                        stale.constant_name, stale.source, name,
                        len(self._remaining_candidates),
                    )
                    nxt = self._try_next_candidate()
                    if nxt is not None:
                        continue
                    object.__setattr__(self, "_retried", True)
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
                    self._swap_real(fresh_real)
                    object.__setattr__(self, "_resolution", fresh)
                    fresh_method = getattr(fresh_real, name)
                    return fresh_method(*args, **kwargs)
                stale = self._resolution
                if (
                    stale.candidates
                    and len(self._remaining_candidates) < len(stale.candidates) - 1
                ):
                    used_idx = len(stale.candidates) - 1 - len(self._remaining_candidates)
                    _promote_candidate_in_cache(
                        stale.constant_name, stale.intent, stale.test_file,
                        stale.candidates[used_idx],
                    )
                return result

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


class _AsyncLazyLocator(_AsyncLocatorBase):
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

    Subclasses ``playwright.async_api.Locator`` so that
    ``isinstance(lazy, Locator)`` is True (Playwright's ``expect._dispatch``
    discriminates on that). ``_impl_obj`` is populated by
    :meth:`_ensure_resolved` once the awaited resolve completes. If user
    code does ``expect(lazy)`` BEFORE any action triggers resolution, the
    ``_impl_obj`` access inside ``expect._dispatch`` will hit
    ``__getattr__`` and raise — that's a clearer failure than the bare
    ``ValueError: Unsupported type`` we shipped before, and the correct
    async pattern is to either await an action first or fold the
    sentinel inline (``await expect(page.locator(SENTINEL)).to_*()``
    after an action elsewhere has populated the cache).
    """

    def __init__(self, *, page, sentinel, rebuild_locator, constant_name=None):
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_rebuild_locator", rebuild_locator)
        object.__setattr__(self, "_constant_name", constant_name)
        object.__setattr__(self, "_resolved", False)
        object.__setattr__(self, "_resolved_real", None)
        object.__setattr__(self, "_resolved_resolution", None)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<worca-t AsyncLazyLocator sentinel={parse_sentinel(self._sentinel)!r}>"

    async def _ensure_resolved(self):
        if self._resolved:
            return
        resolution = await _resolve_sentinel_async(
            self._page, self._sentinel, constant_name=self._constant_name,
        )
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
        resolved_real = self._rebuild_locator(resolution.selector)
        object.__setattr__(self, "_resolved_real", resolved_real)
        object.__setattr__(self, "_resolved_resolution", resolution)
        object.__setattr__(self, "_resolved", True)
        # Late-mirror ``_impl_obj`` so any subsequent ``expect(lazy)`` call
        # that dispatches via ``isinstance(lazy, Locator)`` finds a real
        # impl handle. Pre-resolution ``expect`` is still unsupported on
        # the async surface — see class docstring.
        _impl = getattr(resolved_real, "_impl_obj", None)
        if _impl is not None:
            object.__setattr__(self, "_impl_obj", _impl)

    def __getattribute__(self, name):
        # Same rationale as ``_RetryingLocator.__getattribute__``: now that
        # we subclass ``_AsyncLocatorBase`` for the isinstance contract,
        # inherited ``AsyncLocator.click`` / ``.nth`` / etc. would win
        # attribute lookup before ``__getattr__`` fires. Force
        # retriable-method lookups through ``__getattr__`` so the lazy
        # resolve-then-act flow still runs. Underscored names and dunders
        # use normal MRO (our state, ``_ensure_resolved``, ``__repr__``,
        # etc.). Non-retriable public names fall through to ``__getattr__``
        # which raises the helpful "call an action first" AttributeError.
        if name.startswith('_'):
            return object.__getattribute__(self, name)
        if name in _RETRIABLE_METHODS:
            return type(self).__getattr__(self, name)
        # Defer to default lookup; if neither the proxy nor inherited
        # class defines it, ``__getattr__`` raises with guidance.
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return type(self).__getattr__(self, name)

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

# Originals for the BrowserType.launch / launch_persistent_context patches
# that inject proxy={'server': URL} from env vars. Keys: "sync.launch",
# "sync.launch_persistent_context", and the async equivalents.
_original_browsertype_methods: dict[str, Any] = {}


def _resolve_page_from_receiver(receiver: Any) -> Any:
    """Find the Page object that owns ``receiver`` (which may be a Page,
    Frame, or Locator). Sub-objects expose a ``.page`` property / method
    that walks to the owning Page; the Page itself does not have ``.page``.

    Probe: ``main_frame`` is Page-only across all Playwright versions
    (pre- and post- accessibility-API removal). Prior implementations
    probed ``accessibility``, which was removed in Playwright 1.40 — that
    check now silently mis-classifies every Page as "not a Page" and falls
    through to the ``.page`` walk (which returns ``receiver`` anyway, so
    the bug was harmless, but the probe was wrong).
    """
    # Page itself — `main_frame` is a Page-only attribute that survives
    # across the 1.40 accessibility API removal.
    if hasattr(receiver, "main_frame"):
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
        # Capture the constant name NOW, while the `.locator()` call-site frame
        # is still live on the stack. Resolution itself is deferred to the
        # first awaited action, by which point this frame is gone — see
        # `_resolve_sentinel_async` for why eager capture is required.
        constant_name = _walk_stack_for_constant_name()
        return _AsyncLazyLocator(
            page=page, sentinel=selector,
            rebuild_locator=lambda new_sel: original(self, new_sel, *args, **kwargs),
            constant_name=constant_name,
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


def _proxy_url_to_inject() -> str | None:
    """Return the proxy URL to inject into ``BrowserType.launch`` calls, or
    None if injection should be skipped on this call.

    Resolution order:
      1. ``WORCA_T_PROXY`` — worca-specific override, wins over standard vars.
      2. ``HTTPS_PROXY`` / ``https_proxy`` — standard env-var path. Worca-t
         propagates these into the subprocess via ``with_proxy_env`` which
         reads ``HKCU:\\Environment`` on Windows; users on corporate networks
         (Bosch px, cntlm, etc.) typically have them set there.

    Returns None when ``WORCA_T_DISABLE_PROXY_INJECT=1`` (explicit opt-out)
    or when none of the above env vars are set.

    The function is called PER LAUNCH so a test can flip the env mid-session
    (set in conftest.py before the browser fixture, etc.).
    """
    if os.environ.get("WORCA_T_DISABLE_PROXY_INJECT") == "1":
        return None
    return (
        os.environ.get("WORCA_T_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or None
    )


def _maybe_inject_proxy_kwarg(kwargs: dict) -> dict:
    """Mutate-and-return ``kwargs`` to add ``proxy={'server': URL}`` when:
      - the env says we should inject (``_proxy_url_to_inject()`` returns a URL),
      - AND the caller did NOT pass ``proxy=`` (SUT's explicit choice wins).

    A SUT-passed ``proxy=None`` is treated as "no proxy" — we respect it.
    """
    if "proxy" in kwargs:
        return kwargs
    url = _proxy_url_to_inject()
    if not url:
        return kwargs
    kwargs["proxy"] = {"server": url}
    log.info("worca_t.proxy_injected url=%s", url)
    return kwargs


def _wrap_sync_launch(original):
    """Wrap a sync ``BrowserType.launch`` / ``launch_persistent_context``.

    Bound to the module-level helper so unit tests can construct a stub
    BrowserType class, call the wrapped method, and assert on the kwargs
    forwarded to ``original`` without spawning a real browser.
    """

    def wrapper(self, *args, **kwargs):
        kwargs = _maybe_inject_proxy_kwarg(kwargs)
        return original(self, *args, **kwargs)

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


def _wrap_async_launch(original):
    """Async counterpart of :func:`_wrap_sync_launch`."""

    async def wrapper(self, *args, **kwargs):
        kwargs = _maybe_inject_proxy_kwarg(kwargs)
        return await original(self, *args, **kwargs)

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


def _install_proxy_patch() -> None:
    """Monkey-patch ``BrowserType.launch`` and ``launch_persistent_context``
    on both sync and async APIs so that ``proxy={'server': URL}`` is injected
    from ``HTTPS_PROXY`` / ``WORCA_T_PROXY`` when the SUT didn't pass its own.

    Why this exists: Playwright's Python ``chromium.launch()`` does NOT
    auto-pickup ``HTTPS_PROXY`` env var (verified empirically), and the
    typical SUT's browser fixture builds ``launch(**{"args": [...], "headless"
    : ...})`` without a ``proxy=`` kwarg. On corporate networks where the
    target hostname is only resolvable via the corp proxy (e.g. ``*.bosch.com``
    behind px@localhost:3128), tests then fail with ``net::ERR_NAME_NOT_
    RESOLVED`` even though the user's other tools (Chrome, VS Code) work fine.

    Idempotent. Skipped entirely when ``WORCA_T_DISABLE_PROXY_INJECT=1``.
    Tolerates either API surface being absent (sync-only or async-only SUT).
    """
    if _original_browsertype_methods:
        return
    if os.environ.get("WORCA_T_DISABLE_PROXY_INJECT") == "1":
        log.info("worca_t.proxy_inject_disabled_via_env")
        return
    patched_any = False
    # ---- Sync API ----
    try:
        from playwright.sync_api import BrowserType  # type: ignore[import-untyped]
    except ImportError:
        pass
    else:
        for method_name in ("launch", "launch_persistent_context"):
            if not hasattr(BrowserType, method_name):
                continue
            original = getattr(BrowserType, method_name)
            _original_browsertype_methods[f"sync.{method_name}"] = original
            setattr(BrowserType, method_name, _wrap_sync_launch(original))
            log.info("worca_t.proxy_patched class=sync.BrowserType.%s", method_name)
            patched_any = True
    # ---- Async API ----
    try:
        from playwright.async_api import BrowserType as AsyncBrowserType  # type: ignore[import-untyped]
    except ImportError:
        pass
    else:
        for method_name in ("launch", "launch_persistent_context"):
            if not hasattr(AsyncBrowserType, method_name):
                continue
            original = getattr(AsyncBrowserType, method_name)
            _original_browsertype_methods[f"async.{method_name}"] = original
            setattr(AsyncBrowserType, method_name, _wrap_async_launch(original))
            log.info("worca_t.proxy_patched class=async.BrowserType.%s", method_name)
            patched_any = True
    if not patched_any:
        log.info("worca_t.proxy_inject_no_playwright")


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

    The proxy-injection patch is installed alongside via
    :func:`_install_proxy_patch`. Locator and launch patches are
    orthogonal — disabling one (via its dedicated env var) leaves the
    other intact.
    """
    _install_proxy_patch()
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
    """Register ``worca_<phase>`` markers at collection time.

    Marker registration is cheap (no Playwright import) and prevents
    ``--strict-markers`` rejections. The monkey-patch is deferred to
    ``pytest_runtest_setup`` so that xdist workers don't all import
    Playwright and patch classes simultaneously during collection —
    a race that crashes worker processes on resource-constrained systems.
    """
    for phase in _WORCA_PHASE_MARKERS:
        config.addinivalue_line(
            "markers",
            f"worca_{phase}: worca-t generated {phase} test",
        )


def pytest_runtest_setup(item):  # noqa: D401, ARG001 - pytest hook signature
    """Lazy-install the monkey-patch on first test setup.

    Deferred from ``pytest_configure`` to avoid concurrent Playwright
    imports across xdist workers during collection. The idempotent guard
    inside ``_install_monkey_patch()`` makes this safe to call on every
    test — the first call installs, all subsequent calls are a no-op.
    """
    _install_monkey_patch()


# ---------------------------------------------------------------------------
# Storage-state auto-capture (Use case B — same-run reuse for Step 9 heal)
# ---------------------------------------------------------------------------
#
# When ``WORCA_T_WORKSPACE_DIR`` is set by Step 9, this hook captures
# ``context.storage_state(path=<workspace>/storage-state.json)`` on the
# first passing test. Step 9 then injects the file into Playwright MCP via
# ``--storage-state=<path>`` so the heal-agent boots already authenticated
# (skips the 10-30 s auth-replay cost per heal invocation).
#
# Single capture per session — once the file is written, the flag stays
# True for the rest of the run. SUTs with separate auth + post-auth tests
# capture from the first test that authenticated successfully (typically
# the smoke test); subsequent tests don't re-overwrite even if their state
# is slightly different.
#
# Best-effort: missing context fixture (non-Playwright SUT), missing env
# var (running outside worca-t), or exception during capture all degrade
# silently to "no capture this session". Step 9 falls through to manual
# auth-replay in those cases.

_storage_state_captured: bool = False


def pytest_runtest_makereport(item, call):  # noqa: D401, ARG001 - pytest hook signature
    """Stash per-phase reports on the item so :func:`pytest_runtest_teardown`
    can tell whether the test passed.

    pytest does not expose a built-in "did this test pass" query at
    teardown time; the canonical pattern is for plugins to stash reports
    in ``pytest_runtest_makereport``. The SUT's own conftest may also
    define this hook — pytest invokes ALL registered hookimpls, not just
    the first, so our stash is additive (it does not conflict).
    """
    setattr(item, f"rep_{call.when}", None)
    # We need the actual report object; pytest supplies it as the hook's
    # return value when this impl is a hookwrapper. Without hookwrapper
    # semantics, the simplest path is to recompute pass-state from
    # ``call.excinfo`` (None means the phase completed without exception).
    # That's sufficient for our "did the call phase pass" check.
    if call.when == "call":
        passed = call.excinfo is None
        item.rep_call = type("_RepStub", (), {"passed": passed})()


def pytest_runtest_teardown(item, nextitem):  # noqa: D401, ARG001 - pytest hook signature
    """Capture Playwright storage state on the first passing test.

    Conditions for capture:
      - ``WORCA_T_WORKSPACE_DIR`` env var is set (Step 9 sets it)
      - this hook hasn't captured yet in the current session
      - the test we're tearing down PASSED (i.e. authenticated + ran assertions)
      - the test has a ``context`` fixture in scope (Playwright SUT)

    All failures degrade silently — capture is a best-effort optimization,
    not a correctness invariant. The heal flow still works without it.
    """
    global _storage_state_captured
    if _storage_state_captured:
        return
    workspace_dir = os.environ.get("WORCA_T_WORKSPACE_DIR")
    if not workspace_dir:
        return
    # Only capture on a passing test. The pytest report status of the
    # most-recent call phase lives on the item via stash; older pytest
    # versions used ``item.rep_call`` (set by user conftest). Be defensive
    # — when status can't be determined, skip rather than capture from a
    # potentially-broken context.
    rep_call = getattr(item, "rep_call", None)
    if rep_call is not None and getattr(rep_call, "passed", False) is False:
        return
    # Get the test's context fixture. Different SUTs may rename the
    # fixture (e.g. "playwright_context", "browser_context"); we try the
    # standard name first, then fall back to scanning funcargs for an
    # object with a ``storage_state`` method.
    funcargs = getattr(item, "funcargs", None) or {}
    context = funcargs.get("context")
    if context is None or not hasattr(context, "storage_state"):
        for value in funcargs.values():
            if hasattr(value, "storage_state") and callable(value.storage_state):
                context = value
                break
    if context is None or not hasattr(context, "storage_state"):
        return
    try:
        out_path = Path(workspace_dir) / "storage-state.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(out_path))
        _storage_state_captured = True
        log.info(
            "worca_t.storage_state_captured path=%s test=%s",
            out_path, item.nodeid,
        )
    except Exception as e:  # noqa: BLE001 - capture is best-effort
        log.warning("worca_t.storage_state_capture_failed %s", e)


def pytest_sessionfinish(session, exitstatus):  # noqa: D401, ARG001 - pytest hook signature
    """Restore the originals on sync + async Page/Frame/Locator.locator AND
    BrowserType.launch / launch_persistent_context (best-effort housekeeping)."""
    global _original_page_locator
    # Locator patches
    if _original_locator_methods:
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
    # BrowserType.launch patches (proxy injection)
    if _original_browsertype_methods:
        try:
            from playwright.sync_api import BrowserType  # type: ignore[import-untyped]
            for method_name in ("launch", "launch_persistent_context"):
                original = _original_browsertype_methods.get(f"sync.{method_name}")
                if original is not None and hasattr(BrowserType, method_name):
                    setattr(BrowserType, method_name, original)
        except ImportError:
            pass
        try:
            from playwright.async_api import BrowserType as AsyncBrowserType  # type: ignore[import-untyped]
            for method_name in ("launch", "launch_persistent_context"):
                original = _original_browsertype_methods.get(f"async.{method_name}")
                if original is not None and hasattr(AsyncBrowserType, method_name):
                    setattr(AsyncBrowserType, method_name, original)
        except ImportError:
            pass
        _original_browsertype_methods.clear()
