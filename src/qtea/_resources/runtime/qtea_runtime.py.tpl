"""qtea JIT locator runtime — vendored into the SUT at codegen time.

Single-file pytest plugin. The Step 7 codegen step copies this file into
``<sut>/tests/qtea_runtime.py`` and registers it via the generated
``conftest.py``'s ``pytest_plugins`` list. The plugin then:

1. Monkey-patches ``playwright.sync_api.Page.locator`` (and async, if
   available) to detect sentinel strings produced by :func:`tbd`.
2. On sentinel access: consults the dev-supplied locator file → runtime
   cache → ``qtea resolve`` subprocess → HITL, in that priority order.
3. Inflates default Playwright timeouts to absorb resolver latency.
4. Wraps the returned Locator in a thin proxy that, on TimeoutError,
   invalidates the cache entry and re-resolves once before failing.

ENV VARS read by this plugin (set by ``s09_execute.py``):

- ``QTEA_CACHE_DIR``         — directory for ``locator-cache.json`` (required)
- ``QTEA_DEV_LOCATORS``      — optional path to a dev-supplied locator file
- ``QTEA_RESOLVER_PORT``     — TCP port of the parent-side ResolverServer
                                  (preferred LLM path; avoids leaking
                                  ANTHROPIC_API_KEY into the SUT subprocess)
- ``QTEA_RESOLVER_TOKEN``    — per-run shared secret authenticating to the
                                  ResolverServer; valid only while the parent
                                  process holds the server context manager open
- ``QTEA_RESOLVER_CMD``      — legacy subprocess fallback, defaults to
                                  ``qtea resolve``; only used when
                                  QTEA_RESOLVER_PORT is not set
- ``QTEA_RESOLVER_MODEL``    — passed through to the resolver
- ``QTEA_RUN_ID``            — stamped into cache entries
- ``QTEA_DEFAULT_TIMEOUT_MS``— Playwright default timeout in ms (default 60000)
- ``QTEA_INFLATE_TIMEOUTS``  — set to ``0`` to opt out of timeout inflation
- ``QTEA_DISABLE_JIT``       — set to ``1`` to disable the monkey-patch entirely
- ``QTEA_NO_LLM_RESOLVE``    — set to ``1`` to disable the LLM resolver
                                  (tier 4); cache + dev-locators + in-process
                                  heuristic only. Unresolvable TBDs fail fast
                                  with a structured diagnostic instead of
                                  silently spending tokens. CI default.
- ``QTEA_PROXY``             — proxy URL to inject into Chromium launches
                                  (``proxy={'server': URL}``). Overrides
                                  ``HTTPS_PROXY`` when both are set.
- ``HTTPS_PROXY`` / ``https_proxy`` — fallback proxy source when
                                  ``QTEA_PROXY`` is unset. Qtea-t already
                                  propagates these into the subprocess via
                                  ``with_proxy_env`` (which reads
                                  ``HKCU:\\Environment`` on Windows). Required
                                  on corporate networks where the SUT's target
                                  hostname is only resolvable via the corp
                                  proxy (e.g. ``*.bosch.com`` via px@3128).
                                  An SUT that explicitly passes ``proxy=`` to
                                  ``launch()`` wins — the injection is a
                                  "default-when-absent" only.
- ``QTEA_DISABLE_PROXY_INJECT`` — set to ``1`` to disable the proxy
                                  injection patch entirely (locator JIT patch
                                  is unaffected — the two patches are
                                  orthogonal).
- ``QTEA_WORKSPACE_DIR``     — qtea run workspace directory. When set,
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
  4. LLM via ``qtea resolve`` subprocess
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

log = logging.getLogger("qtea.runtime")

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

# Sentinel layout: ``__QTEA_TBD__::<constant_or_intent>``. The prefix is
# unique enough that no real CSS selector can collide with it. ``tbd()``
# constructs the sentinel from the intent string only; the constant name
# is recovered at runtime by walking the call stack (cheap one-frame
# inspection — Python's ``sys._getframe`` is O(1)).
_SENTINEL_PREFIX = "__QTEA_TBD__::"


def tbd(intent: str) -> str:
    """Mark a locator constant as unresolved. The intent string describes
    what the element is supposed to be, in plain English.

    Usage::

        from tests.qtea_runtime import tbd

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
# Resolved sentinels — structured locator factories for promoted entries.
# ---------------------------------------------------------------------------
#
# When TBD promotion freezes a resolution into source, CSS-string results
# can be substituted directly (`self.X = "[data-testid='x']"`). Role / text /
# label / placeholder / test_id results need to reach `page.get_by_role(...)`
# instead of `page.locator(...)`. Rather than introduce a new object type that
# every SUT POM base class would need to handle, we encode the payload into a
# specially-prefixed string. The runtime wrapper recognizes it and dispatches
# via `_apply_resolution` WITHOUT going through the resolver tier ladder
# (already resolved by definition).
#
# Why a sentinel string and not a custom class:
#   - SUT POM base classes already accept strings and pass them to
#     `page.locator(...)`. A string flows through any existing path
#     unchanged; a custom class would require every base class to gain a
#     dispatch.
#   - JSON-encoding the payload keeps the format readable in diffs / logs
#     and is safe under `repr()` / log redaction.
_RESOLVED_SENTINEL_PREFIX = "__QTEA_RESOLVED__::"


def _make_resolved_sentinel(payload: dict) -> str:
    """Encode a structured payload as a resolved-sentinel string."""
    return f"{_RESOLVED_SENTINEL_PREFIX}{json.dumps(payload, sort_keys=True)}"


def is_resolved_sentinel(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_RESOLVED_SENTINEL_PREFIX)


def parse_resolved_sentinel(value: str) -> dict | None:
    """Return the payload dict embedded in a resolved-sentinel string."""
    if not is_resolved_sentinel(value):
        return None
    try:
        decoded = json.loads(value[len(_RESOLVED_SENTINEL_PREFIX):])
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def role_locator(role: str, *, name: str | None = None, exact: bool = False) -> str:
    """Resolved-sentinel factory for role locators.

    Written into POM source by Step 9 TBD promotion when the resolver chose
    `strategy="role"`. At action time the runtime calls
    ``page.get_by_role(role, name=name, exact=exact)`` — the CSS string form
    of a role match (`link "Go to Gemini Enterprise"`) is never used, which
    fixes the run-20260621 regression where that string flowed into
    ``page.locator(...)`` and Playwright's CSS parser blew up.

    Direct hand-use in test/POM code is also valid::

        from tests.qtea_runtime import role_locator
        class NavLocators:
            GEMINI = role_locator("link", name="Go to Gemini Enterprise")
    """
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role_locator() requires a non-empty role string")
    payload: dict = {"kind": "role", "role": role.strip()}
    if name is not None:
        if not isinstance(name, str) or not name:
            raise ValueError("role_locator(name=...) must be a non-empty string when set")
        payload["name"] = name
    if exact:
        payload["exact"] = True
    return _make_resolved_sentinel(payload)


def text_locator(text: str, *, exact: bool = False) -> str:
    """Resolved-sentinel factory for text locators (dispatches to ``get_by_text``)."""
    if not isinstance(text, str) or not text:
        raise ValueError("text_locator() requires a non-empty text string")
    payload: dict = {"kind": "text", "text": text}
    if exact:
        payload["exact"] = True
    return _make_resolved_sentinel(payload)


def label_locator(text: str, *, exact: bool = False) -> str:
    """Resolved-sentinel factory for label locators (``get_by_label``)."""
    if not isinstance(text, str) or not text:
        raise ValueError("label_locator() requires a non-empty text string")
    payload: dict = {"kind": "label", "text": text}
    if exact:
        payload["exact"] = True
    return _make_resolved_sentinel(payload)


def placeholder_locator(text: str, *, exact: bool = False) -> str:
    """Resolved-sentinel factory for placeholder locators (``get_by_placeholder``)."""
    if not isinstance(text, str) or not text:
        raise ValueError("placeholder_locator() requires a non-empty text string")
    payload: dict = {"kind": "placeholder", "text": text}
    if exact:
        payload["exact"] = True
    return _make_resolved_sentinel(payload)


def test_id_locator(value: str) -> str:
    """Resolved-sentinel factory for data-testid locators (``get_by_test_id``)."""
    if not isinstance(value, str) or not value:
        raise ValueError("test_id_locator() requires a non-empty value string")
    return _make_resolved_sentinel({"kind": "test_id", "value": value})


# ---------------------------------------------------------------------------
# Cache (mirrors qtea.jit_resolver.read_cache / write_cache)
# ---------------------------------------------------------------------------


def _cache_path() -> Path | None:
    base = os.environ.get("QTEA_CACHE_DIR")
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
        "run_id": os.environ.get("QTEA_RUN_ID"),
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
# Dev-locators (vendored mini-copy of qtea.runtime.dev_locators)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DevLocator:
    constant_name: str
    selector: str
    strategy: str | None = None
    intent: str | None = None
    page_url: str | None = None
    # Structured payload (role/text/label/placeholder/test_id/css). When
    # present, the runtime calls page.get_by_role(...) etc. at action time
    # instead of page.locator(selector). Optional — string-form entries
    # (payload=None) use the legacy locator() path.
    payload: dict | None = None


def _is_xpath(s: str) -> bool:
    t = (s or "").strip()
    return t.startswith("//") or t.startswith("xpath=") or "By.XPATH" in t


def _load_dev_locators() -> dict[str, _DevLocator]:
    """Discover via env var or convention path. SUT root inferred as cwd
    (pytest runs from the SUT root by s09 convention)."""
    candidates: list[Path] = []
    env = os.environ.get("QTEA_DEV_LOCATORS")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / ".qtea" / "dev-locators.json")
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
                payload=entry.get("payload") if isinstance(entry.get("payload"), dict) else None,
            )
        pool_count = sum(1 for d in out.values() if d.intent)
        log.info(
            "qtea.dev_locators_loaded path=%s count=%d pool_entries=%d",
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
      - ``QTEA_DEV_POOL_THRESHOLD`` — min accepted score (default 0.65).
        Tuned for short descriptive intents after stop-word + light stemming
        normalization. Lower = more matches but more false positives; higher
        = stricter. The margin requirement is the primary safety net.
      - ``QTEA_DEV_POOL_MARGIN``    — required gap to second-best (default 0.10).
        This is what protects against ambiguous matches when multiple pool
        entries describe similar elements.
      - ``QTEA_DEV_POOL_PAGE_PENALTY`` — score subtracted when entry.page_url
                                            is set and differs from current page
                                            (default 0.15; soft penalty, not a filter).
    """
    threshold = _env_float("QTEA_DEV_POOL_THRESHOLD", 0.65)
    margin = _env_float("QTEA_DEV_POOL_MARGIN", 0.10)
    page_penalty = _env_float("QTEA_DEV_POOL_PAGE_PENALTY", 0.15)

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
# The plugin talks to a small TCP server that the qtea parent starts in
# Step 8 (``qtea.resolver_server.ResolverServer``). The server runs in
# the TRUSTED parent process and has access to ``ANTHROPIC_API_KEY``;
# pytest itself never sees the key. Connection details are passed in via
# two env vars set by s09_execute:
#
#   - ``QTEA_RESOLVER_PORT``   — loopback TCP port (127.0.0.1)
#   - ``QTEA_RESOLVER_TOKEN``  — per-run shared secret
#
# Falls back to the legacy ``qtea resolve`` subprocess command when
# those env vars are absent (e.g. tests run outside the qtea pipeline,
# or against an older Step 8 that doesn't start the server). The
# subprocess path is BROKEN for first-time TBDs in qtea-managed runs
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
        log.warning("qtea.resolver_socket_error %s", e)
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
        log.warning("qtea.resolver_socket_bad_json %s body=%s", e, body[:500])
        return None
    if not payload.get("ok"):
        log.warning("qtea.resolver_socket_server_error %s", payload.get("error"))
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
    """Legacy fallback: shell out to ``qtea resolve``.

    Retained as an escape hatch for ad-hoc debugging outside the qtea
    pipeline. Inside the qtea pipeline this path fails on first-time
    TBDs because ``safe_subprocess_env`` strips ``ANTHROPIC_API_KEY``
    from the inherited env — that's why we now prefer the socket bridge.
    """
    cmd = os.environ.get("QTEA_RESOLVER_CMD", "qtea resolve")
    cache_dir = os.environ.get("QTEA_CACHE_DIR")
    if not cache_dir:
        log.warning("qtea.no_cache_dir — resolver subprocess skipped")
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
        log.warning("qtea.resolver_subprocess_error %s", e)
        return None
    finally:
        try:
            snap_path.unlink()
        except OSError:
            pass
    if proc.returncode != 0:
        log.warning("qtea.resolver_subprocess_exit %d stderr=%s", proc.returncode, proc.stderr[:500])
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        log.warning("qtea.resolver_subprocess_bad_json %s stdout=%s", e, proc.stdout[:500])
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
    port_env = os.environ.get("QTEA_RESOLVER_PORT")
    token_env = os.environ.get("QTEA_RESOLVER_TOKEN")
    if port_env and token_env:
        try:
            port = int(port_env)
        except ValueError:
            log.warning("qtea.resolver_bad_port %r — falling back to subprocess", port_env)
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
    if os.environ.get("QTEA_INFLATE_TIMEOUTS") == "0":
        return
    pid = id(page)
    if pid in _timeouts_inflated_for_page:
        return
    _timeouts_inflated_for_page.add(pid)
    try:
        timeout_ms = int(os.environ.get("QTEA_DEFAULT_TIMEOUT_MS", "60000"))
    except ValueError:
        timeout_ms = 60000
    try:
        page.set_default_timeout(timeout_ms)
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.timeout_inflate_skip %s", e)


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


# Capability cache for ``Locator.aria_snapshot`` kwargs. Probed once per
# process via ``TypeError``-catch (signature introspection is unreliable —
# Playwright's sync stubs are generated from the async API and have lied
# about supported kwargs in the past). ``None`` = unprobed; ``True`` /
# ``False`` = proven supported / unsupported. ``mode_ai`` covers both
# ``mode="ai"`` and ``depth=`` (both added in Playwright 1.59); ``boxes``
# covers ``boxes=True`` (added in 1.60). Reset in test fixtures.
_AOM_CAPS: dict[str, bool | None] = {"mode_ai": None, "boxes": None}


def _read_aom_env() -> tuple[int | None, bool, bool, bool]:
    """Return ``(depth, want_boxes, force_boxes, legacy_ok)`` from env.

    - ``QTEA_AOM_DEPTH`` (unset = no cap): passed as ``depth=`` when
      the runtime is on Playwright 1.59+. Truncating the snapshot can hide
      the target element and force an LLM round-trip, so the default is
      no cap; set this only when token budget is tight on huge SPAs.
    - ``QTEA_AOM_BOXES`` (``"auto"`` default | ``"off"`` | ``"force"``):
      ``auto`` probes & caches; ``off`` never requests boxes (cheaper
      snapshots, no tie-breaking signal); ``force`` ignores a cached
      negative result and re-attempts on every call (debug only).
    - ``QTEA_AOM_LEGACY_OK`` (``"1"`` default): set to ``"0"`` to
      disable the pre-1.40 ``page.accessibility.snapshot()`` fallback for
      SUTs that should never silently degrade.
    """
    depth: int | None = None
    raw_depth = os.environ.get("QTEA_AOM_DEPTH")
    if raw_depth:
        try:
            parsed = int(raw_depth)
            depth = parsed if parsed > 0 else None
        except ValueError:
            depth = None
    mode = (os.environ.get("QTEA_AOM_BOXES") or "auto").strip().lower()
    want_boxes = mode != "off"
    force_boxes = mode == "force"
    legacy_ok = os.environ.get("QTEA_AOM_LEGACY_OK", "1") != "0"
    return depth, want_boxes, force_boxes, legacy_ok


def _aom_kwargs_ladder(
    *, depth: int | None, want_boxes: bool, force_boxes: bool,
) -> list[dict[str, Any]]:
    """Return the kwarg-sets to try in order, richest first, honouring the
    capability cache. Rungs proven unsupported are skipped on subsequent
    calls so each process pays the probe cost at most once.
    """
    rungs: list[dict[str, Any]] = []
    # Rung A: mode="ai" + boxes=True (+depth) — Playwright 1.60+
    if (
        want_boxes
        and _AOM_CAPS["mode_ai"] is not False
        and (force_boxes or _AOM_CAPS["boxes"] is not False)
    ):
        kw: dict[str, Any] = {"mode": "ai", "boxes": True}
        if depth is not None:
            kw["depth"] = depth
        rungs.append(kw)
    # Rung B: mode="ai" (+depth) — Playwright 1.59
    if _AOM_CAPS["mode_ai"] is not False:
        kw = {"mode": "ai"}
        if depth is not None:
            kw["depth"] = depth
        rungs.append(kw)
    # Rung C: no kwargs — Playwright 1.40-1.58
    rungs.append({})
    return rungs


def _update_aom_caps_from_failure(kwargs: dict[str, Any]) -> None:
    """Cache a ``TypeError`` outcome. We can't reliably string-match the
    error across Python/Playwright versions, so we attribute the failure
    to the most-recently-added kwarg in the rung that raised: ``boxes``
    (v1.60 addition) before ``mode`` (v1.59 addition).
    """
    if kwargs.get("boxes") is True:
        _AOM_CAPS["boxes"] = False
        return
    if kwargs.get("mode") == "ai":
        _AOM_CAPS["mode_ai"] = False


def _update_aom_caps_from_success(kwargs: dict[str, Any]) -> None:
    """Cache a successful kwarg-set's capabilities."""
    if kwargs.get("mode") == "ai":
        _AOM_CAPS["mode_ai"] = True
    if kwargs.get("boxes") is True:
        _AOM_CAPS["boxes"] = True


def _call_aria_snapshot_sync(
    body_locator: Any, *,
    depth: int | None = None,
    want_boxes: bool = True,
    force_boxes: bool = False,
) -> str:
    """Call ``Locator.aria_snapshot()`` with the richest supported kwarg-set.

    Ladder: ``mode="ai", boxes=True`` (Playwright 1.60+) → ``mode="ai"``
    (1.59) → no-kwargs (1.40-1.58). Capabilities are probed once per
    process via :data:`_AOM_CAPS` and cached; subsequent calls skip
    proven-unsupported rungs. Returns the empty string on a falsy return.
    """
    last_err: TypeError | None = None
    for kw in _aom_kwargs_ladder(
        depth=depth, want_boxes=want_boxes, force_boxes=force_boxes,
    ):
        try:
            result = body_locator.aria_snapshot(**kw)
        except TypeError as e:
            _update_aom_caps_from_failure(kw)
            last_err = e
            continue
        _update_aom_caps_from_success(kw)
        return result or ""
    # All rungs raised TypeError — re-raise so the caller logs it. Cannot
    # happen on Playwright 1.40+, which accepts the no-kwargs call.
    if last_err is not None:
        raise last_err
    return ""


async def _call_aria_snapshot_async(
    body_locator: Any, *,
    depth: int | None = None,
    want_boxes: bool = True,
    force_boxes: bool = False,
) -> str:
    """Async counterpart of :func:`_call_aria_snapshot_sync`."""
    last_err: TypeError | None = None
    for kw in _aom_kwargs_ladder(
        depth=depth, want_boxes=want_boxes, force_boxes=force_boxes,
    ):
        try:
            result = await body_locator.aria_snapshot(**kw)
        except TypeError as e:
            _update_aom_caps_from_failure(kw)
            last_err = e
            continue
        _update_aom_caps_from_success(kw)
        return result or ""
    if last_err is not None:
        raise last_err
    return ""


def _snapshot_page(page: Any) -> tuple[str, dict[str, Any]]:
    """Capture the page AOM as ``(text, parsed_dict_tree)``.

    Capability ladder (probed once per process, cached on :data:`_AOM_CAPS`):
      1. ``aria_snapshot(mode="ai", boxes=True)`` — Playwright 1.60+,
         LLM-optimized YAML with ``[box=x,y,w,h]`` per element (CSS
         pixels, viewport-relative). Boxes are stripped from element
         names by :func:`_parse_aria_snapshot_yaml` and retained on the
         parsed node dict as ``node["box"]`` for tie-breaking.
      2. ``aria_snapshot(mode="ai")`` — Playwright 1.59.
      3. ``aria_snapshot()`` — Playwright 1.40-1.58 (no kwargs).
      4. ``page.accessibility.snapshot()`` — pre-1.40 fallback. Gated by
         ``QTEA_AOM_LEGACY_OK`` (default on).

    Returns ``("", {})`` on total failure so the resolver still receives a
    well-formed input (the LLM tier then cleanly returns "no candidates"
    instead of crashing). Errors are logged but never propagate.
    """
    depth, want_boxes, force_boxes, legacy_ok = _read_aom_env()
    # ---- Primary: Locator.aria_snapshot (Playwright 1.40+) ----
    try:
        body = page.locator("body")
        snapshot_text = _call_aria_snapshot_sync(
            body, depth=depth, want_boxes=want_boxes, force_boxes=force_boxes,
        )
        snapshot_dict = _parse_aria_snapshot_yaml(snapshot_text)
        return snapshot_text, snapshot_dict
    except AttributeError:
        # `Page.locator` or `Locator.aria_snapshot` not present — fall through.
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("qtea.snapshot_failed_aria %s", e)
        # Fall through to legacy API — older Playwright might still work.

    # ---- Legacy: page.accessibility.snapshot() (Playwright <1.40) ----
    if not legacy_ok:
        return "", {}
    try:
        ax = page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("qtea.snapshot_failed %s", e)
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

    Annotations emitted by Playwright are stripped from element names
    BEFORE the role/name matchers run so they don't pollute heuristic
    matching:

    - ``[box=x,y,w,h]`` (v1.60+): retained as ``node["box"] = (x,y,w,h)``
      tuple of floats for the heuristic tie-breaker.
    - ``[ref=eN]`` (v1.59+ ``mode="ai"``): dropped (ephemeral, not addressable).
    - ``[level=N]`` and other ``[key=val]`` attributes: dropped from the name.

    Returns ``{}`` for empty input so callers can rely on truthy checks.

    Pure-function: no Playwright import, no I/O. Unit-testable standalone.
    """
    import re as _re

    if not yaml_text or not yaml_text.strip():
        return {}

    # Annotation regexes — stripped from each line before role/name parsing.
    # ``box`` retains a tuple for the heuristic tie-breaker; ``ref`` and
    # generic ``[key=val]`` attrs are dropped.
    _RE_BOX = _re.compile(r'\s*\[box=([\d.,\-]+)\]')
    _RE_REF = _re.compile(r'\s*\[ref=e?\d+\]')
    _RE_ATTR = _re.compile(r'\s*\[[A-Za-z_][A-Za-z0-9_-]*=[^\]]*\]')

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

        # Extract and strip annotations before role/name parsing.
        box: tuple[float, float, float, float] | None = None
        m_box = _RE_BOX.search(body)
        if m_box:
            parts = m_box.group(1).split(",")
            if len(parts) == 4:
                try:
                    box = (
                        float(parts[0]), float(parts[1]),
                        float(parts[2]), float(parts[3]),
                    )
                except ValueError:
                    box = None
            body = _RE_BOX.sub("", body)
        body = _RE_REF.sub("", body)
        # Generic ``[key=val]`` attrs (e.g. ``[level=1]``, ``[disabled]``-form
        # not covered — these are role-only suffixes that ``_RE_ROLE_ONLY``
        # already discards). Strip so quoted-name matcher sees a clean tail.
        body = _RE_ATTR.sub("", body).strip()

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
        if box is not None:
            node["box"] = box
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
    - multiple candidates score within :data:`_HEURISTIC_TIE_GAP` AND
      no box-based tie-breaker can choose between them
    - no AOM (empty snapshot)

    Box tie-breaker: when ``boxes=True`` was requested (Playwright 1.60+)
    and two candidates tie within ``_HEURISTIC_TIE_GAP``, prefer the one
    with the smaller ``y`` coordinate (visually higher on the page —
    common case is "primary CTA above secondary"). Skipped when only one
    candidate has a box, to avoid an asymmetric bias.
    """
    if not snapshot:
        return None
    role, name_tokens, name_hint = _parse_intent(intent)
    if role is None or not name_tokens:
        return None

    candidates: list[tuple[float, str, tuple[float, float, float, float] | None]] = []
    for node in _aom_walk(snapshot):
        if node.get("role") != role:
            continue
        node_name = (node.get("name") or "").lower()
        if not node_name:
            continue
        box = node.get("box") if isinstance(node.get("box"), tuple) else None
        if name_hint and name_hint in node_name:
            candidates.append((1.0, node.get("name") or "", box))
        elif name_tokens and all(t in node_name for t in name_tokens):
            candidates.append((0.95, node.get("name") or "", box))
        elif name_tokens and any(t in node_name for t in name_tokens):
            candidates.append((0.6, node.get("name") or "", box))

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    top_score, top_name, top_box = candidates[0]
    if top_score < _HEURISTIC_MIN_SCORE:
        return None
    if len(candidates) > 1 and (top_score - candidates[1][0]) < _HEURISTIC_TIE_GAP:
        # Tie within the gap — collect all tied candidates and try a
        # box-based tie-break. Both/all must have boxes to participate;
        # asymmetric box availability is treated as no tie-break.
        tied = [c for c in candidates if (top_score - c[0]) < _HEURISTIC_TIE_GAP]
        if all(c[2] is not None for c in tied):
            tied.sort(key=lambda c: c[2][1])  # smaller y first
            top_name = tied[0][1]
        else:
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
    # Structured payload for role/text/label/placeholder/test_id strategies.
    # None for CSS-string resolutions (the legacy path: runtime calls
    # `scope.locator(selector)` exactly as before). When present, the locator
    # wrapper calls `scope.get_by_role(...)` / `.get_by_text(...)` etc. via
    # :func:`_apply_resolution`. Required to fix the run-20260621 regression
    # where `link "Go to Gemini Enterprise"` was cached as a CSS string,
    # fed to Playwright's CSS parser, and exploded at action time.
    payload: dict | None = None


def _apply_resolution(scope, resolution, original_locator, args, kwargs):
    """Build a Playwright Locator for ``resolution`` against ``scope``.

    Dispatch rules:
      - ``payload is None`` → call ``original_locator(scope, selector, *args, **kwargs)``
        (legacy path; preserves chained-locator kwargs like ``has_text=...``).
      - ``payload["kind"] == "css"`` → same as legacy, using ``payload["selector"]``.
      - ``payload["kind"] in {"role","text","label","placeholder","test_id"}``
        → call the corresponding ``scope.get_by_*`` method directly with
        ``payload``-derived arguments. ``original_locator``/``args``/``kwargs``
        are intentionally ignored — those are ``locator()``-specific options
        that have no analogue on the strongly-typed Playwright getters.

    Returns the raw Playwright Locator (the caller wraps it in
    :class:`_RetryingLocator`).
    """
    payload = resolution.payload if isinstance(resolution.payload, dict) else None
    if payload is None:
        return original_locator(scope, resolution.selector, *args, **kwargs)
    kind = payload.get("kind")
    if kind == "css":
        sel = payload.get("selector") or resolution.selector
        return original_locator(scope, sel, *args, **kwargs)
    if kind == "role":
        kw: dict[str, Any] = {}
        if isinstance(payload.get("name"), str) and payload["name"]:
            kw["name"] = payload["name"]
        if payload.get("exact") is True:
            kw["exact"] = True
        return scope.get_by_role(payload["role"], **kw)
    if kind == "text":
        kw = {"exact": True} if payload.get("exact") is True else {}
        return scope.get_by_text(payload["text"], **kw)
    if kind == "label":
        kw = {"exact": True} if payload.get("exact") is True else {}
        return scope.get_by_label(payload["text"], **kw)
    if kind == "placeholder":
        kw = {"exact": True} if payload.get("exact") is True else {}
        return scope.get_by_placeholder(payload["text"], **kw)
    if kind == "test_id":
        return scope.get_by_test_id(payload["value"])
    # Unknown kind — fall back to the legacy string path with whatever the
    # telemetry selector happens to be. The validator at write-time prevents
    # this in practice; this branch is defence in depth.
    log.warning("qtea.apply_resolution_unknown_kind kind=%s", kind)
    return original_locator(scope, resolution.selector, *args, **kwargs)


def _resolution_from_candidate(
    candidate: dict[str, Any], stale: "_Resolution",
) -> "_Resolution":
    """Build a synthetic ``_Resolution`` from one fallback-candidate dict so
    the retry path can reuse :func:`_apply_resolution`. Inherits constant /
    intent / test_file from the stale parent; carries the candidate's payload
    (when structured) or selector (when string-only).
    """
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else None
    sel = candidate.get("selector")
    return _Resolution(
        selector=sel,
        source=stale.source,
        constant_name=stale.constant_name,
        intent=stale.intent,
        test_file=stale.test_file,
        candidates=None,
        payload=payload,
    )


def _resolve_tiers_1_2(
    intent: str, constant_name: str, test_file: str | None,
    page_url: str | None,
    *, skip_dev: bool, skip_cache: bool, skip_pool: bool = False,
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
        log.info("qtea.dev_locator_used constant=%s selector=%s",
                 constant_name, _sanitize_for_log(dev.selector))
        _append_spend_line({"tier": 1, "source": "dev", "constant": constant_name,
                            "input_tokens": 0, "output_tokens": 0, "success": True})
        return _Resolution(
            dev.selector, "dev", constant_name, intent, test_file,
            payload=dev.payload,
        )

    # Tier 1b: intent-based pool match. Activates when the file contains
    # entries with an ``intent`` field (frontend-dev-supplied selector pool).
    # Deterministic + zero-LLM; honors QTEA_NO_LLM_RESOLVE=1 semantics.
    # ``skip_pool`` is the tier-1b-specific suppressor — set by the retry path
    # when a dev-pool selector failed at action time, so re-resolve does not
    # bounce back to the same fuzzy answer.
    if not skip_dev and not skip_pool and _dev_locators_cache:
        winner, score, reason, top = _pool_match(
            intent, page_url, _dev_locators_cache,
        )
        if reason == "accept" and winner is not None:
            log.info(
                "qtea.dev_pool_match constant=%s matched=%s score=%.3f selector=%s",
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
                    "payload": winner.payload,
                    "source": "dev-pool",
                    "page_url": page_url,
                    "matched_constant": winner.constant_name,
                    "pool_score": round(score, 3),
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                _write_cache(cache_for_write)
            except OSError as e:
                log.warning("qtea.dev_pool_cache_write_failed %s", e)
            return _Resolution(
                winner.selector, "dev-pool", constant_name, intent, test_file,
                payload=winner.payload,
            )
        elif reason in ("reject_low_score", "reject_tie"):
            # Structured rejection telemetry — feeds threshold tuning. No
            # spend line (no successful resolution) but log surface mirrors
            # the dev_pool_match log shape for grep parity.
            log.info(
                "qtea.dev_pool_reject constant=%s reason=%s best=%.3f top=%s",
                constant_name, reason, score, top,
            )

    cache = _read_cache()
    key = _cache_key(test_file, constant_name, intent)
    if not skip_cache:
        cached = cache.get(key)
        # Quarantined entries (dev-pool selectors that failed at action time
        # in this session) are kept on disk for provenance but ignored by
        # tier-2 reads — the LLM fallback under the `_shadow:<key>` entry
        # gets first dibs instead.
        if cached and cached.get("quarantined"):
            shadow = cache.get(f"_shadow:{key}")
            if shadow and (shadow.get("selector") or shadow.get("payload")):
                cached = shadow
            else:
                cached = None
        if cached and (cached.get("selector") or cached.get("payload")):
            log.info("qtea.cache_hit constant=%s selector=%s",
                     constant_name, _sanitize_for_log(cached.get("selector") or ""))
            cached_bundle = cached.get("candidates")
            bundle_tuple = (
                tuple(cached_bundle)
                if isinstance(cached_bundle, list) and cached_bundle
                else None
            )
            cached_payload = (
                cached["payload"] if isinstance(cached.get("payload"), dict) else None
            )
            _append_spend_line({"tier": 2, "source": "cached",
                                "constant": constant_name,
                                "candidates_count": len(bundle_tuple) if bundle_tuple else 1,
                                "input_tokens": 0, "output_tokens": 0, "success": True})
            return _Resolution(
                cached.get("selector"), "cached", constant_name, intent, test_file,
                candidates=bundle_tuple, payload=cached_payload,
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
                "qtea.heuristic_hit constant=%s selector=%s",
                constant_name, _sanitize_for_log(heuristic_selector),
            )
            _append_spend_line({"tier": 3, "source": "heuristic",
                                "constant": constant_name,
                                "input_tokens": 0, "output_tokens": 0, "success": True})
            return _Resolution(
                heuristic_selector, "heuristic", constant_name, intent, test_file,
            )

    if os.environ.get("QTEA_NO_LLM_RESOLVE") == "1":
        log.warning(
            "qtea.no_llm_resolve_active constant=%s intent=%s — tiers 1-3 missed",
            constant_name, intent,
        )
        return _Resolution(None, "none", constant_name, intent, test_file)

    result = _call_resolver(
        intent=intent, snapshot_text=snapshot_text,
        constant_name=constant_name, test_file=test_file, page_url=page_url,
    )
    if result is None:
        log.warning("qtea.resolver_failed constant=%s intent=%s",
                    constant_name, intent)
        _append_spend_line({"tier": 4, "source": "none", "constant": constant_name,
                            "input_tokens": 0, "output_tokens": 0, "success": False,
                            "reason": "resolver_call_failed"})
        return _Resolution(None, "none", constant_name, intent, test_file)
    selector = result.get("selector")
    result_payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    raw_candidates = result.get("candidates")
    bundle_tuple = (
        tuple(raw_candidates)
        if isinstance(raw_candidates, list) and raw_candidates
        else None
    )
    has_resolution = bool(selector) or result_payload is not None
    spend_entry = {
        "tier": 4, "source": result.get("source") or "agent",
        "constant": constant_name,
        "candidates_count": len(bundle_tuple) if bundle_tuple else (1 if has_resolution else 0),
        "input_tokens": result.get("input_tokens") or 0,
        "output_tokens": result.get("output_tokens") or 0,
        "model": result.get("model"), "duration_ms": result.get("duration_ms"),
        "success": has_resolution,
    }
    if not has_resolution:
        log.warning("qtea.resolver_no_selector constant=%s reason=%s",
                    constant_name, result.get("reason"))
        spend_entry["reason"] = result.get("reason")
        _append_spend_line(spend_entry)
        return _Resolution(None, "none", constant_name, intent, test_file)
    log.info(
        "qtea.resolver_ok constant=%s selector=%s source=%s confidence=%s candidates=%d",
        constant_name, _sanitize_for_log(selector or ""),
        result.get("source"), result.get("confidence"),
        len(bundle_tuple) if bundle_tuple else 1,
    )
    _append_spend_line(spend_entry)
    return _Resolution(
        selector, "agent", constant_name, intent, test_file,
        candidates=bundle_tuple, payload=result_payload,
    )


async def _snapshot_page_async(page: Any) -> tuple[str, dict[str, Any]]:
    """Async counterpart of :func:`_snapshot_page`. Same capability ladder
    and env-var contract; differs only in the ``await`` on the snapshot
    call and on the legacy ``accessibility.snapshot()``.
    """
    depth, want_boxes, force_boxes, legacy_ok = _read_aom_env()
    # ---- Primary: Locator.aria_snapshot (Playwright 1.40+) ----
    try:
        body = page.locator("body")
        snapshot_text = await _call_aria_snapshot_async(
            body, depth=depth, want_boxes=want_boxes, force_boxes=force_boxes,
        )
        snapshot_dict = _parse_aria_snapshot_yaml(snapshot_text)
        return snapshot_text, snapshot_dict
    except AttributeError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("qtea.snapshot_failed_aria_async %s", e)

    # ---- Legacy: page.accessibility.snapshot() (Playwright <1.40) ----
    if not legacy_ok:
        return "", {}
    try:
        ax = await page.accessibility.snapshot() or {}
        return json.dumps(ax, ensure_ascii=False), ax if isinstance(ax, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("qtea.snapshot_failed_async %s", e)
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
    skip_pool: bool = False,
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
        skip_dev=skip_dev, skip_cache=skip_cache, skip_pool=skip_pool,
    )
    if early is not None:
        _record_resolution_use(early)
        return early

    snapshot_text, snapshot_dict = _snapshot_page(page)
    res = _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, current_url,
        skip_heuristic=skip_heuristic,
    )
    _record_resolution_use(res)
    return res


async def _resolve_sentinel_async(
    page: Any, sentinel: str, *,
    constant_name: str | None = None,
    skip_dev: bool = False,
    skip_cache: bool = False,
    skip_heuristic: bool = False,
    skip_pool: bool = False,
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
        skip_dev=skip_dev, skip_cache=skip_cache, skip_pool=skip_pool,
    )
    if early is not None:
        _record_resolution_use(early)
        return early

    snapshot_text, snapshot_dict = await _snapshot_page_async(page)
    res = _resolve_tiers_3_4(
        intent, constant_name, test_file,
        snapshot_text, snapshot_dict, current_url,
        skip_heuristic=skip_heuristic,
    )
    _record_resolution_use(res)
    return res


def _invalidate_cache_entry(constant_name: str, intent: str, test_file: str | None) -> None:
    """Remove a stale entry from the runtime cache so the next resolution
    forces a fresh LLM call. Best-effort; failures don't block the test."""
    key = _cache_key(test_file, constant_name, intent)
    cache = _read_cache()
    if key in cache:
        del cache[key]
        try:
            _write_cache(cache)
            log.info("qtea.cache_invalidated constant=%s", constant_name)
        except OSError as e:
            log.warning("qtea.cache_invalidate_failed %s", e)


def _quarantine_dev_pool_entry(
    stale: "_Resolution", *, page_url: str | None, exception: BaseException,
) -> None:
    """Mark a dev-pool cache entry as quarantined and append to the
    session-scoped quarantine log.

    Effects:
      - Sets ``quarantined: True`` on the cache entry under the standard key
        (preserves the dev-supplied selector for provenance; tier-2 reads
        now skip it and prefer the shadow LLM fallback).
      - Appends a JSONL record to ``<cache_dir>/dev-pool-quarantine.jsonl``.
        Step 9 reads this at end-of-run and emits a ``dev-locator-drifted``
        bug-candidate per record.

    Best-effort: file I/O errors degrade silently (the next read will see
    the still-quarantined entry; the bug-candidate gets emitted on the
    following run after a retry).
    """
    key = _cache_key(stale.test_file, stale.constant_name, stale.intent)
    cache = _read_cache()
    entry = cache.get(key)
    if entry is None:
        # Nothing to quarantine — race with another worker, or entry already
        # purged. Still log the action-time failure for telemetry.
        entry = {
            "key": key, "intent": stale.intent,
            "constant_name": stale.constant_name,
            "test_file": stale.test_file, "source": "dev-pool",
            "selector": stale.selector,
        }
    entry["quarantined"] = True
    cache[key] = entry
    try:
        _write_cache(cache)
    except OSError as e:
        log.warning("qtea.quarantine_cache_write_failed %s", e)

    cache_dir = _cache_path()
    if cache_dir is None:
        return
    log_path = cache_dir.parent / "dev-pool-quarantine.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "intent": stale.intent,
            "constant_name": stale.constant_name,
            "test_file": stale.test_file,
            "page_url": page_url,
            "stale_selector": stale.selector,
            "matched_constant": entry.get("matched_constant"),
            "pool_score": entry.get("pool_score"),
            "exception": f"{type(exception).__name__}: {exception}"[:500],
            "run_id": os.environ.get("QTEA_RUN_ID"),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("qtea.quarantine_log_write_failed %s", e)


def _shadow_dev_pool_fallback(stale: "_Resolution", fresh: "_Resolution") -> None:
    """Move the fresh LLM resolution to the ``_shadow:<key>`` cache slot so
    the dev-pool primary key keeps its quarantined record and the LLM answer
    becomes the active tier-2 hit for the rest of the session.

    Called AFTER ``_resolve_sentinel`` returns from the re-resolve path. The
    parent ResolverServer wrote the fresh entry under the standard key; we
    relocate it and restore the quarantined dev-pool entry in its place.
    """
    key = _cache_key(stale.test_file, stale.constant_name, stale.intent)
    cache = _read_cache()
    fresh_entry = cache.get(key)
    if not fresh_entry or fresh_entry.get("source") not in ("agent", "cached"):
        # No fresh entry to relocate (resolver was skipped, or wrote
        # somewhere unexpected). Leave the quarantined entry as-is.
        return
    # Restore the quarantined dev-pool entry under the standard key. The
    # session-scoped tier-2-skip-quarantined rule routes future tier-2 reads
    # to the shadow slot we're about to populate.
    quarantined_entry = {
        "key": key,
        "intent": stale.intent,
        "constant_name": stale.constant_name,
        "test_file": stale.test_file,
        "selector": stale.selector,
        "payload": stale.payload,
        "source": "dev-pool",
        "quarantined": True,
        # Preserve any provenance fields that were on the original entry.
        "matched_constant": fresh_entry.get("matched_constant"),
        "pool_score": fresh_entry.get("pool_score"),
    }
    shadow_entry = dict(fresh_entry)
    shadow_entry["key"] = f"_shadow:{key}"
    cache[key] = quarantined_entry
    cache[f"_shadow:{key}"] = shadow_entry
    try:
        _write_cache(cache)
    except OSError as e:
        log.warning("qtea.shadow_cache_write_failed %s", e)


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
            "qtea.fallback_promoted constant=%s selector=%s",
            constant_name, _sanitize_for_log(str(working.get("selector"))),
        )
    except OSError as e:
        log.warning("qtea.fallback_promote_failed %s", e)


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
        rebuild_from_resolution=None,
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
        # New callback: rebuild from a full `_Resolution` so structured
        # payload kinds (role/text/label/…) reach the right `get_by_*` API.
        # Falls back to the legacy string-only path when callers don't pass one.
        object.__setattr__(
            self, "_rebuild_from_resolution",
            rebuild_from_resolution or (lambda r: rebuild_locator(r.selector)),
        )
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
        return f"<qtea RetryingLocator wrapping {self._real!r}>"

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
        a locator built from its selector or structured payload. Returns the
        candidate dict so the caller can promote it in the cache on success,
        or ``None`` when the bundle is exhausted."""
        if not self._remaining_candidates:
            return None
        nxt = self._remaining_candidates.pop(0)
        # Structured payload takes precedence over the string `selector`
        # (which for structured kinds is telemetry-only).
        synthetic = _resolution_from_candidate(nxt, self._resolution)
        if synthetic.selector is None and synthetic.payload is None:
            return None
        fresh_real = self._rebuild_from_resolution(synthetic)
        self._swap_real(fresh_real)
        log.info(
            "qtea.fallback_candidate_try constant=%s selector=%s strategy=%s kind=%s",
            self._resolution.constant_name,
            _sanitize_for_log(synthetic.selector or ""),
            nxt.get("strategy"),
            (synthetic.payload or {}).get("kind"),
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
                _overlay_retried = False
                while True:
                    try:
                        result = await getattr(self._real, name)(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        if not _overlay_retried and _is_overlay_intercept_error(exc):
                            _overlay_retried = True
                            try:
                                if await _try_overlay_dismiss_async(
                                    self._page, self._real, self._resolution,
                                ):
                                    continue
                            except Exception as ov_exc:  # noqa: BLE001
                                log.debug(
                                    "qtea.overlay_dismiss_wrapper_failed_async %s",
                                    ov_exc,
                                )
                            raise
                        if not _is_playwright_timeout(exc):
                            raise
                        stale = self._resolution
                        log.info(
                            "qtea.retry_on_timeout_async constant=%s source=%s method=%s remaining=%d",
                            stale.constant_name, stale.source, name,
                            len(self._remaining_candidates),
                        )
                        nxt = self._try_next_candidate()
                        if nxt is not None:
                            continue  # retry against the fallback candidate
                        # Bundle exhausted (or never existed) → LLM re-resolve.
                        object.__setattr__(self, "_retried", True)
                        is_dev_pool = stale.source == "dev-pool"
                        if is_dev_pool:
                            _quarantine_dev_pool_entry(
                                stale,
                                page_url=_safe_page_url(self._page),
                                exception=exc,
                            )
                        else:
                            _invalidate_cache_entry(
                                stale.constant_name, stale.intent, stale.test_file,
                            )
                        fresh = await _resolve_sentinel_async(
                            self._page, self._sentinel,
                            constant_name=stale.constant_name,
                            skip_dev=(stale.source == "dev"),
                            skip_pool=is_dev_pool,
                            skip_cache=True,
                            skip_heuristic=(stale.source == "heuristic"),
                        )
                        if fresh.selector is None and fresh.payload is None:
                            log.warning(
                                "qtea.retry_unresolvable constant=%s",
                                stale.constant_name,
                            )
                            raise
                        if is_dev_pool:
                            _shadow_dev_pool_fallback(stale, fresh)
                        fresh_real = self._rebuild_from_resolution(fresh)
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
            _overlay_retried = False  # bounded: one dismiss attempt per action
            while True:
                try:
                    result = getattr(self._real, name)(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    # Overlay intercept detection (L1). Distinct from timeout
                    # retry — if a safe-class heuristic dismiss succeeds, we
                    # retry the SAME action once. Never enters the LLM
                    # re-resolve path (that's for locator-not-found, not
                    # element-blocked-by-overlay).
                    if not _overlay_retried and _is_overlay_intercept_error(exc):
                        _overlay_retried = True
                        try:
                            if _try_overlay_dismiss_sync(
                                self._page, self._real, self._resolution,
                            ):
                                continue  # dismissed — retry original action
                        except Exception as ov_exc:  # noqa: BLE001
                            log.debug("qtea.overlay_dismiss_wrapper_failed %s", ov_exc)
                        # Heuristic failed / risky-only — propagate original
                        # error. Event is recorded by _try_overlay_dismiss_sync
                        # so the parent-side sweep can HITL-prompt.
                        raise
                    if not _is_playwright_timeout(exc):
                        raise
                    stale = self._resolution
                    log.info(
                        "qtea.retry_on_timeout constant=%s source=%s method=%s remaining=%d",
                        stale.constant_name, stale.source, name,
                        len(self._remaining_candidates),
                    )
                    nxt = self._try_next_candidate()
                    if nxt is not None:
                        continue
                    object.__setattr__(self, "_retried", True)
                    is_dev_pool = stale.source == "dev-pool"
                    if is_dev_pool:
                        _quarantine_dev_pool_entry(
                            stale,
                            page_url=_safe_page_url(self._page),
                            exception=exc,
                        )
                    else:
                        _invalidate_cache_entry(
                            stale.constant_name, stale.intent, stale.test_file,
                        )
                    fresh = _resolve_sentinel(
                        self._page, self._sentinel,
                        skip_dev=(stale.source == "dev"),
                        skip_pool=is_dev_pool,
                        skip_cache=True,
                        skip_heuristic=(stale.source == "heuristic"),
                    )
                    if fresh.selector is None and fresh.payload is None:
                        log.warning(
                            "qtea.retry_unresolvable constant=%s",
                            stale.constant_name,
                        )
                        raise
                    if is_dev_pool:
                        _shadow_dev_pool_fallback(stale, fresh)
                    fresh_real = self._rebuild_from_resolution(fresh)
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
    loop on resolution, so they raise. Qtea-t codegen agent is instructed
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

    def __init__(
        self, *, page, sentinel, rebuild_locator,
        rebuild_from_resolution=None, constant_name=None,
    ):
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_rebuild_locator", rebuild_locator)
        object.__setattr__(
            self, "_rebuild_from_resolution",
            rebuild_from_resolution or (lambda r: rebuild_locator(r.selector)),
        )
        object.__setattr__(self, "_constant_name", constant_name)
        object.__setattr__(self, "_resolved", False)
        object.__setattr__(self, "_resolved_real", None)
        object.__setattr__(self, "_resolved_resolution", None)

    def __repr__(self):  # pragma: no cover (cosmetic)
        return f"<qtea AsyncLazyLocator sentinel={parse_sentinel(self._sentinel)!r}>"

    async def _ensure_resolved(self):
        if self._resolved:
            return
        resolution = await _resolve_sentinel_async(
            self._page, self._sentinel, constant_name=self._constant_name,
        )
        if resolution.selector is None and resolution.payload is None:
            _write_hitl_pending(
                resolution.intent, resolution.constant_name,
                resolution.test_file, _safe_page_url(self._page),
            )
            import pytest
            pytest.fail(
                f"qtea JIT runtime (async): could not resolve locator "
                f"{parse_sentinel(self._sentinel)!r}. The parent process will "
                f"surface this via HITL on the next interactive run, or as a "
                f"`locator-unresolvable` bug candidate for Step 9."
            )
        resolved_real = self._rebuild_from_resolution(resolution)
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
                    rebuild_from_resolution=self._rebuild_from_resolution,
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
    cache_dir = os.environ.get("QTEA_CACHE_DIR")
    if not cache_dir:
        return
    path = Path(cache_dir) / "resolver-spend.jsonl"
    line = {
        "run_id": os.environ.get("QTEA_RUN_ID"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        log.debug("qtea.spend_write_failed %s", e)


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
    cache_dir = os.environ.get("QTEA_CACHE_DIR")
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
                "run_id": os.environ.get("QTEA_RUN_ID"),
                "ts": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("qtea.hitl_pending_written path=%s", path)
    except OSError as e:
        log.warning("qtea.hitl_pending_write_failed %s", e)


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
        # Pre-resolved structured sentinel: dispatch directly. Sync because
        # the corresponding Playwright getters (get_by_role/text/...) are
        # sync constructors even on the async API surface; the awaited part
        # is the action method on the returned Locator.
        if is_resolved_sentinel(selector):
            payload = parse_resolved_sentinel(selector)
            if payload is None:
                return original(self, selector, *args, **kwargs)
            synthetic = _Resolution(
                selector=None, source="resolved-inline",
                constant_name=_walk_stack_for_constant_name() or "<inline>",
                intent=f"resolved:{payload.get('kind')}",
                test_file=(os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None),
                candidates=None, payload=payload,
            )
            return _apply_resolution(self, synthetic, original, args, kwargs)
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
            rebuild_from_resolution=lambda r: _apply_resolution(self, r, original, args, kwargs),
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
        # Pre-resolved structured sentinel (role_locator / text_locator / …
        # from a promoted POM entry): skip the tier ladder, dispatch directly.
        if is_resolved_sentinel(selector):
            payload = parse_resolved_sentinel(selector)
            if payload is None:
                return original(self, selector, *args, **kwargs)
            synthetic = _Resolution(
                selector=None, source="resolved-inline",
                constant_name=_walk_stack_for_constant_name() or "<inline>",
                intent=f"resolved:{payload.get('kind')}",
                test_file=(os.environ.get("PYTEST_CURRENT_TEST", "").split("::", 1)[0] or None),
                candidates=None, payload=payload,
            )
            return _apply_resolution(self, synthetic, original, args, kwargs)
        if not is_sentinel(selector):
            return original(self, selector, *args, **kwargs)
        resolution = _resolve_sentinel(page, selector)
        if resolution.selector is None and resolution.payload is None:
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
                f"qtea JIT runtime: could not resolve locator "
                f"{parse_sentinel(selector)!r}. The parent process will "
                f"surface this via HITL on the next interactive run, or "
                f"as a `locator-unresolvable` bug candidate for Step 9. "
                f"See run.log for the resolution-tier trace."
            )
        real = _apply_resolution(self, resolution, original, args, kwargs)
        return _RetryingLocator(
            real=real,
            page=page,
            sentinel=selector,
            resolution=resolution,
            rebuild_locator=lambda new_sel: original(self, new_sel, *args, **kwargs),
            rebuild_from_resolution=lambda r: _apply_resolution(self, r, original, args, kwargs),
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
      1. ``QTEA_PROXY`` — qtea-specific override, wins over standard vars.
      2. ``HTTPS_PROXY`` / ``https_proxy`` — standard env-var path. Qtea-t
         propagates these into the subprocess via ``with_proxy_env`` which
         reads ``HKCU:\\Environment`` on Windows; users on corporate networks
         (Bosch px, cntlm, etc.) typically have them set there.

    Returns None when ``QTEA_DISABLE_PROXY_INJECT=1`` (explicit opt-out)
    or when none of the above env vars are set.

    The function is called PER LAUNCH so a test can flip the env mid-session
    (set in conftest.py before the browser fixture, etc.).
    """
    if os.environ.get("QTEA_DISABLE_PROXY_INJECT") == "1":
        return None
    return (
        os.environ.get("QTEA_PROXY")
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
    log.info("qtea.proxy_injected url=%s", url)
    return kwargs


# ---------------------------------------------------------------------------
# Overlay/popup auto-dismiss — runtime side.
# ---------------------------------------------------------------------------
#
# Companion to ``src/qtea/overlay_handling.py`` (parent-side). This section
# implements Layers 1, 2, 5, 6 of the overlay auto-dismiss system:
#
#   L1: detect "element intercepts pointer events" errors at action time,
#       walking DOM/frames to identify the overlay causing the intercept.
#   L2: heuristic dismiss — two-token-class scoring (safe/risky); only
#       safe-class buttons (Close, Dismiss, ×, Skip) auto-fire.
#   L5: register handlers from <sut>/.qtea/interceptors.json via Playwright's
#       native page.add_locator_handler() so known overlays are invisible on
#       every future run.
#   L6: filter consent/GDPR cookies from Playwright storage_state auto-
#       capture so dismissed banners don't leak into persisted state.
#
# Feature-flag ``QTEA_OVERLAY_HANDLING=1`` (default on). Set to ``0`` to
# disable all overlay code paths (rollback to pre-feature thrash behavior).
#
# This section CANNOT import from qtea (the runtime is vendored into the
# SUT subprocess). The token classes and cookie patterns below are
# duplicated from qtea.overlay_handling; both sides must agree on them
# for the parent's HITL candidate list to match what the runtime saw.

_OVERLAY_ENABLED = os.environ.get("QTEA_OVERLAY_HANDLING", "1") != "0"

# Dismiss-SAFE tokens: heuristic MAY auto-fire on these. Pure dismissal
# semantics — clicking would essentially never mask a real bug.
_OVERLAY_SAFE_TOKENS = (
    "close", "dismiss", "not now", "skip", "later",
    "maybe later", "remind me later", "×", "✕",
)

# Dismiss-RISKY tokens: heuristic MUST NOT auto-fire. They reach HITL as
# candidates the operator can pick, but never zero-touch. "Accept" could
# be terms-consent OR destructive-payment; two-class separation guardrails.
_OVERLAY_RISKY_TOKENS = (
    "accept", "agree", "continue", "ok", "got it",
    "understand", "confirm", "proceed",
)

# Consent/GDPR cookie name/domain fragments filtered from storage_state
# auto-capture (Layer 6). Prevents the consent flow's own regression
# tests from silently passing on subsequent runs.
_OVERLAY_CONSENT_COOKIE_PATTERNS = (
    "consent", "cookie", "gdpr", "banner",
    "onetrust", "trustarc", "cookiebot", "osano",
)

# Roles considered overlay-shaped when walking the DOM up from the
# intercept point. Chosen to match interceptors.json schema.
_OVERLAY_ROLES = ("dialog", "alertdialog", "banner", "region",
                  "complementary", "status", "alert")

# Pages we've registered locator handlers on. Set of Python id() values
# so we don't re-register on every new_page call for the same page object.
_overlay_registered_page_ids: set[int] = set()

# Interceptor entries loaded once from interceptors.json at first use.
# None = not yet loaded; [] = loaded and empty (or file missing).
_overlay_interceptors_cache: list[dict[str, Any]] | None = None


def _overlay_events_path() -> Path | None:
    """Where the runtime writes OverlayEvent JSONL lines. None when
    QTEA_WORKSPACE_DIR is unset (standalone pytest run, not from Step 9)."""
    ws = os.environ.get("QTEA_WORKSPACE_DIR")
    if not ws:
        return None
    return Path(ws) / "overlay-events.jsonl"


def _overlay_screenshots_dir() -> Path | None:
    ws = os.environ.get("QTEA_WORKSPACE_DIR")
    if not ws:
        return None
    return Path(ws) / "overlay-screenshots"


def _interceptors_json_path() -> Path | None:
    """Where interceptors.json lives. Priority: QTEA_INTERCEPTORS env
    (set by Step 9) > <cwd>/.qtea/interceptors.json (convention when
    running pytest standalone in a SUT that has the file)."""
    env = os.environ.get("QTEA_INTERCEPTORS")
    if env:
        return Path(env)
    conv = Path.cwd() / ".qtea" / "interceptors.json"
    if conv.exists():
        return conv
    return None


def _is_overlay_intercept_error(exc: BaseException) -> bool:
    """Multi-signal detector for 'element intercepts pointer events' failures.

    Playwright's error text has shifted across versions — we look for both
    the modern message ('intercepts pointer events') and legacy patterns
    ('is not clickable' + 'another element'). Both must be tolerated
    without over-matching genuine timeouts (which are handled elsewhere).
    """
    if not exc:
        return False
    msg = str(exc)
    if "intercepts pointer events" in msg:
        return True
    if "is not clickable" in msg and "another element" in msg:
        return True
    return False


def _classify_dismiss_name(name: str) -> str:
    """Return "safe" / "risky" / "unknown" for a candidate's accessible name.
    Mirrors qtea.overlay_handling.classify_dismiss_name."""
    if not name:
        return "unknown"
    norm = name.strip().lower()
    if not norm:
        return "unknown"
    tokens = set(_OVERLAY_TOKEN_SPLIT.split(norm))
    tokens.discard("")
    for phrase in _OVERLAY_SAFE_TOKENS:
        if " " in phrase:
            if phrase in norm:
                return "safe"
        elif phrase in tokens:
            return "safe"
    for phrase in _OVERLAY_RISKY_TOKENS:
        if " " in phrase:
            if phrase in norm:
                return "risky"
        elif phrase in tokens:
            return "risky"
    return "unknown"


import re as _re_overlay
_OVERLAY_TOKEN_SPLIT = _re_overlay.compile(r"[\s\-_/,.:;·|()\[\]{}]+")


# JS returned to the parent by page.evaluate(). Introspects the DOM at
# the target coordinates, walks up to find an overlay-shaped element, and
# returns a JSON-serializable description including candidate dismiss
# buttons. Kept as a single string constant so it can be shipped intact
# into the browser without formatting surprises.
_OVERLAY_INSPECT_JS = r"""
({x, y, roles}) => {
    function accessibleName(el) {
        if (!el) return "";
        const aria = el.getAttribute && el.getAttribute("aria-label");
        if (aria) return aria.trim();
        const labelled = el.getAttribute && el.getAttribute("aria-labelledby");
        if (labelled) {
            const l = document.getElementById(labelled);
            if (l) return (l.textContent || "").trim();
        }
        return ((el.innerText || el.textContent) || "").trim().slice(0, 200);
    }
    function elementRole(el) {
        if (!el || !el.getAttribute) return "";
        const explicit = el.getAttribute("role");
        if (explicit) return explicit;
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "button") return "button";
        if (tag === "a" && el.hasAttribute("href")) return "link";
        if (tag === "dialog") return "dialog";
        return "";
    }
    function bboxOf(el) {
        if (!el || !el.getBoundingClientRect) return null;
        const r = el.getBoundingClientRect();
        return [r.left, r.top, r.width, r.height];
    }
    // Sample corners + center to be robust to large targets w/ small overlay
    const samples = [
        [x, y],
        [x - 4, y - 4], [x + 4, y - 4],
        [x - 4, y + 4], [x + 4, y + 4],
    ];
    let intercepting = null;
    for (const [sx, sy] of samples) {
        const el = document.elementFromPoint(sx, sy);
        if (el) { intercepting = el; break; }
    }
    if (!intercepting) return null;
    // Walk up to find an overlay-shaped ancestor.
    let overlay = intercepting;
    let hops = 0;
    while (overlay && hops < 20) {
        const r = elementRole(overlay);
        if (roles.includes(r)) break;
        overlay = overlay.parentElement;
        hops += 1;
    }
    if (!overlay || !roles.includes(elementRole(overlay))) return null;
    const overlayName = accessibleName(overlay);
    const overlayBbox = bboxOf(overlay);
    // Extract candidate buttons INSIDE the overlay
    const buttons = Array.from(
        overlay.querySelectorAll('button, [role="button"], a[href], [role="link"], [role="menuitem"]')
    );
    const cands = buttons.map(b => {
        const r = elementRole(b) || "button";
        const n = accessibleName(b);
        const bb = bboxOf(b);
        return { role: r, name: n, bbox: bb };
    }).filter(c => c.name);
    return {
        overlay_role: elementRole(overlay),
        overlay_name: overlayName,
        overlay_bbox: overlayBbox,
        candidates: cands,
    };
}
"""


def _target_bbox_sync(target_locator) -> tuple[float, float, float, float] | None:
    try:
        b = target_locator.bounding_box()
        if b and "x" in b and "y" in b and "width" in b and "height" in b:
            return (b["x"], b["y"], b["width"], b["height"])
    except Exception:  # noqa: BLE001
        pass
    return None


async def _target_bbox_async(target_locator) -> tuple[float, float, float, float] | None:
    try:
        b = await target_locator.bounding_box()
        if b and "x" in b and "y" in b and "width" in b and "height" in b:
            return (b["x"], b["y"], b["width"], b["height"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _inspect_overlay_sync(page, target_bbox) -> dict[str, Any] | None:
    """Run the DOM-introspection JS to find an overlay covering target_bbox."""
    if target_bbox is None:
        return None
    cx = target_bbox[0] + target_bbox[2] / 2
    cy = target_bbox[1] + target_bbox[3] / 2
    try:
        return page.evaluate(
            _OVERLAY_INSPECT_JS, {"x": cx, "y": cy, "roles": list(_OVERLAY_ROLES)},
        )
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.overlay_inspect_failed_sync %s", e)
        return None


async def _inspect_overlay_async(page, target_bbox) -> dict[str, Any] | None:
    if target_bbox is None:
        return None
    cx = target_bbox[0] + target_bbox[2] / 2
    cy = target_bbox[1] + target_bbox[3] / 2
    try:
        return await page.evaluate(
            _OVERLAY_INSPECT_JS, {"x": cx, "y": cy, "roles": list(_OVERLAY_ROLES)},
        )
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.overlay_inspect_failed_async %s", e)
        return None


def _score_overlay_candidates(
    cands: list[dict[str, Any]],
    overlay_bbox: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    """Score candidates and sort best-first. Same rules as
    qtea.overlay_handling.score_candidates."""
    scored: list[dict[str, Any]] = []
    for c in cands or []:
        role = str(c.get("role") or "").strip()
        name = str(c.get("name") or "").strip()
        bbox_raw = c.get("bbox")
        bbox = tuple(bbox_raw) if bbox_raw and len(bbox_raw) == 4 else None
        cls = _classify_dismiss_name(name)
        role_ok = role.lower() in ("button", "link", "menuitem")
        if not role_ok and cls == "unknown":
            continue
        score = 0
        if cls in ("safe", "risky"):
            score += 3
        if role.lower() == "button":
            score += 1
        if bbox is not None and overlay_bbox is not None:
            bx, by, bw, bh = bbox
            ox, oy, ow, oh = overlay_bbox
            cx = bx + bw / 2
            cy = by + bh / 2
            if cx > ox + ow / 2 and cy < oy + oh / 3 and cx <= ox + ow:
                score += 2
        if score == 0:
            continue
        scored.append({
            "role": role, "name": name, "safe": cls == "safe",
            "score": score, "bbox": list(bbox) if bbox else None,
        })
    scored.sort(key=lambda c: (-int(c["score"]), not c["safe"], c["name"].lower()))
    return scored


def _pick_safe_candidate(scored: list[dict[str, Any]]) -> dict[str, Any] | None:
    for c in scored:
        if c.get("safe"):
            return c
    return None


def _capture_overlay_screenshot_sync(
    page, overlay_bbox: tuple[float, float, float, float] | None, path: Path,
) -> None:
    """Cropped screenshot of overlay with password/email inputs masked.

    Best-effort. Any exception degrades to no-screenshot (parent-side
    HITL renders a "no screenshot captured" placeholder).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"path": str(path)}
        if overlay_bbox is not None:
            ox, oy, ow, oh = overlay_bbox
            # Guard against off-viewport bboxes (some banners are absolutely
            # positioned outside the visible area). If invalid, fall back to
            # a full-page shot.
            if ow > 0 and oh > 0:
                kwargs["clip"] = {
                    "x": max(0.0, ox),
                    "y": max(0.0, oy),
                    "width": ow,
                    "height": oh,
                }
        # Mask password/email inputs — CLAUDE.md rule: no credentials in
        # visual artifacts. Best-effort — if no such inputs exist, mask
        # list stays empty and Playwright ignores it.
        masks: list[Any] = []
        for sel in ('input[type="password"]', 'input[type="email"]'):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    masks.append(loc)
            except Exception:  # noqa: BLE001
                pass
        if masks:
            kwargs["mask"] = masks
        page.screenshot(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.overlay_screenshot_failed %s", e)


async def _capture_overlay_screenshot_async(
    page, overlay_bbox: tuple[float, float, float, float] | None, path: Path,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"path": str(path)}
        if overlay_bbox is not None:
            ox, oy, ow, oh = overlay_bbox
            if ow > 0 and oh > 0:
                kwargs["clip"] = {
                    "x": max(0.0, ox), "y": max(0.0, oy),
                    "width": ow, "height": oh,
                }
        masks: list[Any] = []
        for sel in ('input[type="password"]', 'input[type="email"]'):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    masks.append(loc)
            except Exception:  # noqa: BLE001
                pass
        if masks:
            kwargs["mask"] = masks
        await page.screenshot(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.overlay_screenshot_failed_async %s", e)


def _safe_page_url(page) -> str:
    try:
        u = page.url
        return u() if callable(u) else str(u or "")
    except Exception:  # noqa: BLE001
        return ""


def _write_overlay_event(event: dict[str, Any]) -> None:
    """Atomic append to overlay-events.jsonl. Best-effort."""
    path = _overlay_events_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        log.debug("qtea.overlay_event_write_failed %s", e)


def _build_overlay_event(
    inspect_result: dict[str, Any] | None,
    resolution: "_Resolution" | None,
    heuristic_attempted: bool,
    heuristic_succeeded: bool,
    scored_candidates: list[dict[str, Any]],
    page_url: str,
    screenshot_path: str,
) -> dict[str, Any]:
    """Assemble the JSONL event dict. Field names must match qtea.overlay_handling.OverlayEvent."""
    nodeid = _current_test_nodeid() or ""
    overlay_role = str((inspect_result or {}).get("overlay_role") or "")
    overlay_name = str((inspect_result or {}).get("overlay_name") or "")
    overlay_bbox = (inspect_result or {}).get("overlay_bbox")
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "test_id": nodeid,
        "target_intent": (resolution.intent if resolution else "") or "",
        "overlay_role": overlay_role,
        "overlay_name": overlay_name,
        "page_url": page_url,
        "screenshot_path": screenshot_path,
        "overlay_frame": "top",
        "overlay_bbox": overlay_bbox,
        "heuristic_attempted": heuristic_attempted,
        "heuristic_succeeded": heuristic_succeeded,
        "candidates": scored_candidates,
    }


def _screenshot_key(overlay_role: str, overlay_name: str, page_url: str) -> str:
    import hashlib
    h = hashlib.sha256(
        f"{overlay_role}::{overlay_name}::{page_url}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{h}.png"


def _try_overlay_dismiss_sync(page, target_locator, resolution) -> bool:
    """L1 detection + L2 heuristic dismiss (sync path).

    Returns True when the safe-class heuristic dismissed something and the
    caller should retry the original action. Returns False when nothing
    could be safely dismissed (event is recorded in either case).
    """
    if not _OVERLAY_ENABLED:
        return False
    target_bbox = _target_bbox_sync(target_locator)
    page_url = _safe_page_url(page)
    inspect = _inspect_overlay_sync(page, target_bbox)
    if inspect is None:
        # Not overlay-shaped — record so parent-side reclassifier still marks it
        _write_overlay_event(_build_overlay_event(
            None, resolution, False, False, [], page_url, "",
        ))
        return False
    overlay_bbox = inspect.get("overlay_bbox")
    scored = _score_overlay_candidates(inspect.get("candidates") or [], overlay_bbox)
    # Screenshot AFTER inspection so the overlay is still visible when captured.
    ss_path = ""
    ss_dir = _overlay_screenshots_dir()
    if ss_dir is not None:
        key = _screenshot_key(
            inspect.get("overlay_role") or "",
            inspect.get("overlay_name") or "",
            page_url,
        )
        candidate_path = ss_dir / key
        _capture_overlay_screenshot_sync(page, overlay_bbox, candidate_path)
        if candidate_path.exists():
            ss_path = str(candidate_path)
    safe = _pick_safe_candidate(scored)
    if safe is None:
        # Only risky/unknown candidates — record event, let it propagate.
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, False, scored, page_url, ss_path,
        ))
        return False
    # Try to click the safe candidate.
    try:
        overlay_loc = page.get_by_role(
            inspect.get("overlay_role") or "dialog",
            name=inspect.get("overlay_name") or "",
        )
        overlay_loc.get_by_role(
            safe["role"] or "button", name=safe["name"],
        ).first.click()
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, True, scored, page_url, ss_path,
        ))
        log.info(
            "qtea.overlay_heuristic_dismissed overlay=%s dismiss=%s",
            inspect.get("overlay_name"), safe["name"],
        )
        return True
    except Exception as e:  # noqa: BLE001 — heuristic is best-effort
        log.debug("qtea.overlay_heuristic_click_failed %s", e)
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, False, scored, page_url, ss_path,
        ))
        return False


async def _try_overlay_dismiss_async(page, target_locator, resolution) -> bool:
    """Async twin of :func:`_try_overlay_dismiss_sync`."""
    if not _OVERLAY_ENABLED:
        return False
    target_bbox = await _target_bbox_async(target_locator)
    page_url = _safe_page_url(page)
    inspect = await _inspect_overlay_async(page, target_bbox)
    if inspect is None:
        _write_overlay_event(_build_overlay_event(
            None, resolution, False, False, [], page_url, "",
        ))
        return False
    overlay_bbox = inspect.get("overlay_bbox")
    scored = _score_overlay_candidates(inspect.get("candidates") or [], overlay_bbox)
    ss_path = ""
    ss_dir = _overlay_screenshots_dir()
    if ss_dir is not None:
        key = _screenshot_key(
            inspect.get("overlay_role") or "",
            inspect.get("overlay_name") or "",
            page_url,
        )
        candidate_path = ss_dir / key
        await _capture_overlay_screenshot_async(page, overlay_bbox, candidate_path)
        if candidate_path.exists():
            ss_path = str(candidate_path)
    safe = _pick_safe_candidate(scored)
    if safe is None:
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, False, scored, page_url, ss_path,
        ))
        return False
    try:
        overlay_loc = page.get_by_role(
            inspect.get("overlay_role") or "dialog",
            name=inspect.get("overlay_name") or "",
        )
        await overlay_loc.get_by_role(
            safe["role"] or "button", name=safe["name"],
        ).first.click()
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, True, scored, page_url, ss_path,
        ))
        log.info(
            "qtea.overlay_heuristic_dismissed_async overlay=%s dismiss=%s",
            inspect.get("overlay_name"), safe["name"],
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("qtea.overlay_heuristic_click_failed_async %s", e)
        _write_overlay_event(_build_overlay_event(
            inspect, resolution, True, False, scored, page_url, ss_path,
        ))
        return False


# ---------------------------------------------------------------------------
# L5: register add_locator_handler for persisted interceptors.
# ---------------------------------------------------------------------------


def _load_interceptors_once() -> list[dict[str, Any]]:
    """Load + validate interceptors.json. Cached per-process."""
    global _overlay_interceptors_cache
    if _overlay_interceptors_cache is not None:
        return _overlay_interceptors_cache
    if not _OVERLAY_ENABLED:
        _overlay_interceptors_cache = []
        return _overlay_interceptors_cache
    path = _interceptors_json_path()
    if path is None or not path.exists():
        _overlay_interceptors_cache = []
        return _overlay_interceptors_cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("qtea.interceptors_read_failed path=%s error=%s", path, e)
        _overlay_interceptors_cache = []
        return _overlay_interceptors_cache
    if raw.get("schema_version") != 1:
        log.warning("qtea.interceptors_wrong_version got=%r want=1", raw.get("schema_version"))
        _overlay_interceptors_cache = []
        return _overlay_interceptors_cache
    entries_raw = raw.get("entries") or []
    if not isinstance(entries_raw, list):
        _overlay_interceptors_cache = []
        return _overlay_interceptors_cache
    # Whitelist check — must mirror qtea.overlay_handling.load_interceptors.
    valid: list[dict[str, Any]] = []
    for idx, e in enumerate(entries_raw):
        if not isinstance(e, dict):
            continue
        dismiss = e.get("dismiss") or {}
        if dismiss.get("kind") not in ("click", "press_escape"):
            log.warning(
                "qtea.interceptor_dismiss_kind_forbidden idx=%d kind=%r",
                idx, dismiss.get("kind"),
            )
            continue
        if dismiss.get("kind") == "click":
            target = dismiss.get("target") or {}
            if not target.get("name") or not target.get("role"):
                continue
        overlay = e.get("overlay") or {}
        if not overlay.get("role") or not overlay.get("name"):
            continue
        valid.append(e)
    _overlay_interceptors_cache = valid
    log.info(
        "qtea.interceptors_loaded path=%s count=%d",
        path, len(valid),
    )
    return valid


def _register_overlay_handlers_on_page_sync(page) -> None:
    """Install page.add_locator_handler for each interceptor entry.
    Idempotent per page — id() tracking prevents double-registration."""
    if not _OVERLAY_ENABLED:
        return
    pid = id(page)
    if pid in _overlay_registered_page_ids:
        return
    entries = _load_interceptors_once()
    if not entries:
        _overlay_registered_page_ids.add(pid)
        return
    handler_available = hasattr(page, "add_locator_handler")
    if not handler_available:
        # Playwright < 1.42 — feature unavailable, log once.
        log.info("qtea.add_locator_handler_unavailable_page")
        _overlay_registered_page_ids.add(pid)
        return
    for entry in entries:
        overlay = entry.get("overlay") or {}
        dismiss = entry.get("dismiss") or {}
        hc = entry.get("handler_config") or {}
        times = int(hc.get("times") or 100)
        no_wait = bool(hc.get("no_wait_after", True))
        try:
            overlay_loc = page.get_by_role(
                overlay.get("role"), name=overlay.get("name"),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_loc_build_failed %s", e)
            continue

        def _make_handler(_dismiss=dismiss):
            def _handler(_overlay):
                try:
                    if _dismiss.get("kind") == "press_escape":
                        page.keyboard.press("Escape")
                    else:
                        target = _dismiss.get("target") or {}
                        page.get_by_role(
                            target.get("role") or "button",
                            name=target.get("name") or "",
                        ).first.click()
                except Exception as ex:  # noqa: BLE001
                    log.debug("qtea.overlay_handler_action_failed %s", ex)
            return _handler

        try:
            page.add_locator_handler(
                overlay_loc, _make_handler(),
                times=times, no_wait_after=no_wait,
            )
            log.info(
                "qtea.overlay_handler_registered overlay=%s dismiss=%s",
                overlay.get("name"), dismiss.get("kind"),
            )
        except TypeError:
            # Older Playwright versions may not support no_wait_after kwarg.
            try:
                page.add_locator_handler(overlay_loc, _make_handler(), times=times)
            except Exception as e:  # noqa: BLE001
                log.debug("qtea.overlay_handler_register_failed %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_handler_register_failed %s", e)
    _overlay_registered_page_ids.add(pid)


def _register_overlay_handlers_on_page_async(page) -> None:
    """Async page — the add_locator_handler call itself is sync-safe on async
    Playwright (the handler callback needs to be async).

    Playwright's async page.add_locator_handler expects an async callable.
    We wrap the same dismiss logic in an async closure.
    """
    if not _OVERLAY_ENABLED:
        return
    pid = id(page)
    if pid in _overlay_registered_page_ids:
        return
    entries = _load_interceptors_once()
    if not entries:
        _overlay_registered_page_ids.add(pid)
        return
    if not hasattr(page, "add_locator_handler"):
        log.info("qtea.add_locator_handler_unavailable_async_page")
        _overlay_registered_page_ids.add(pid)
        return
    for entry in entries:
        overlay = entry.get("overlay") or {}
        dismiss = entry.get("dismiss") or {}
        hc = entry.get("handler_config") or {}
        times = int(hc.get("times") or 100)
        no_wait = bool(hc.get("no_wait_after", True))
        try:
            overlay_loc = page.get_by_role(
                overlay.get("role"), name=overlay.get("name"),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_loc_build_failed_async %s", e)
            continue

        def _make_handler(_dismiss=dismiss):
            async def _handler(_overlay):
                try:
                    if _dismiss.get("kind") == "press_escape":
                        await page.keyboard.press("Escape")
                    else:
                        target = _dismiss.get("target") or {}
                        await page.get_by_role(
                            target.get("role") or "button",
                            name=target.get("name") or "",
                        ).first.click()
                except Exception as ex:  # noqa: BLE001
                    log.debug("qtea.overlay_handler_action_failed_async %s", ex)
            return _handler

        try:
            page.add_locator_handler(
                overlay_loc, _make_handler(),
                times=times, no_wait_after=no_wait,
            )
            log.info(
                "qtea.overlay_handler_registered_async overlay=%s dismiss=%s",
                overlay.get("name"), dismiss.get("kind"),
            )
        except TypeError:
            try:
                page.add_locator_handler(overlay_loc, _make_handler(), times=times)
            except Exception as e:  # noqa: BLE001
                log.debug("qtea.overlay_handler_register_failed_async %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_handler_register_failed_async %s", e)
    _overlay_registered_page_ids.add(pid)


def _wrap_sync_new_page(original):
    def wrapper(self, *args, **kwargs):
        page = original(self, *args, **kwargs)
        try:
            _register_overlay_handlers_on_page_sync(page)
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_register_wrapper_sync_failed %s", e)
        return page
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


def _wrap_async_new_page(original):
    async def wrapper(self, *args, **kwargs):
        page = await original(self, *args, **kwargs)
        try:
            _register_overlay_handlers_on_page_async(page)
        except Exception as e:  # noqa: BLE001
            log.debug("qtea.overlay_register_wrapper_async_failed %s", e)
        return page
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    return wrapper


_original_new_page_methods: dict[str, Any] = {}


def _install_overlay_handler_patch() -> None:
    """Monkey-patch BrowserContext.new_page (sync + async) to install
    add_locator_handler entries on every fresh page. Idempotent.

    Skipped entirely when QTEA_OVERLAY_HANDLING=0. Tolerates either API
    surface being absent.
    """
    if not _OVERLAY_ENABLED:
        return
    if _original_new_page_methods:
        return
    # Sync
    try:
        from playwright.sync_api import BrowserContext as SyncCtx  # type: ignore[import-untyped]
    except ImportError:
        pass
    else:
        if hasattr(SyncCtx, "new_page"):
            _original_new_page_methods["sync"] = SyncCtx.new_page
            SyncCtx.new_page = _wrap_sync_new_page(SyncCtx.new_page)  # type: ignore[assignment]
            log.info("qtea.overlay_patched class=sync.BrowserContext.new_page")
    # Async
    try:
        from playwright.async_api import BrowserContext as AsyncCtx  # type: ignore[import-untyped]
    except ImportError:
        pass
    else:
        if hasattr(AsyncCtx, "new_page"):
            _original_new_page_methods["async"] = AsyncCtx.new_page
            AsyncCtx.new_page = _wrap_async_new_page(AsyncCtx.new_page)  # type: ignore[assignment]
            log.info("qtea.overlay_patched class=async.BrowserContext.new_page")


# ---------------------------------------------------------------------------
# L6: storage-state consent cookie filter.
# ---------------------------------------------------------------------------


def _filter_consent_cookies_from_state(state: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Strip consent/GDPR/tracking cookies from a Playwright storageState.

    Returns (filtered, removed_count). Non-destructive (works on a shallow
    copy). Mirrors qtea.overlay_handling.filter_consent_cookies.
    """
    if not _OVERLAY_ENABLED:
        return state, 0
    filtered = dict(state)
    cookies = list(filtered.get("cookies") or [])
    if not cookies:
        return filtered, 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for c in cookies:
        name = str(c.get("name") or "").lower()
        domain = str(c.get("domain") or "").lower()
        combined = f"{name} {domain}"
        if any(pat in combined for pat in _OVERLAY_CONSENT_COOKIE_PATTERNS):
            removed += 1
            continue
        kept.append(c)
    filtered["cookies"] = kept
    return filtered, removed


# ---------------------------------------------------------------------------
# End of overlay auto-dismiss section.
# ---------------------------------------------------------------------------


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
    from ``HTTPS_PROXY`` / ``QTEA_PROXY`` when the SUT didn't pass its own.

    Why this exists: Playwright's Python ``chromium.launch()`` does NOT
    auto-pickup ``HTTPS_PROXY`` env var (verified empirically), and the
    typical SUT's browser fixture builds ``launch(**{"args": [...], "headless"
    : ...})`` without a ``proxy=`` kwarg. On corporate networks where the
    target hostname is only resolvable via the corp proxy (e.g. ``*.bosch.com``
    behind px@localhost:3128), tests then fail with ``net::ERR_NAME_NOT_
    RESOLVED`` even though the user's other tools (Chrome, VS Code) work fine.

    Idempotent. Skipped entirely when ``QTEA_DISABLE_PROXY_INJECT=1``.
    Tolerates either API surface being absent (sync-only or async-only SUT).
    """
    if _original_browsertype_methods:
        return
    if os.environ.get("QTEA_DISABLE_PROXY_INJECT") == "1":
        log.info("qtea.proxy_inject_disabled_via_env")
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
            log.info("qtea.proxy_patched class=sync.BrowserType.%s", method_name)
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
            log.info("qtea.proxy_patched class=async.BrowserType.%s", method_name)
            patched_any = True
    if not patched_any:
        log.info("qtea.proxy_inject_no_playwright")


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
    _install_overlay_handler_patch()
    global _original_page_locator
    if _original_locator_methods:
        return
    if os.environ.get("QTEA_DISABLE_JIT") == "1":
        log.info("qtea.disabled_via_env")
        return
    patched_any = False
    # ---- Sync API ----
    try:
        from playwright.sync_api import Page, Frame, Locator  # type: ignore[import-untyped]
    except ImportError:
        log.info("qtea.sync_api_unavailable — sync JIT inactive")
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
            log.info("qtea.locator_patched class=sync.%s", cls_name)
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
        log.info("qtea.async_api_unavailable — async JIT inactive")
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
            log.info("qtea.locator_patched class=async.%s", cls_name)
            patched_any = True

    if not patched_any:
        log.warning(
            "qtea.playwright_not_importable — JIT runtime inactive "
            "(neither sync_api nor async_api importable)"
        )
    else:
        log.info("qtea.installed classes=%s", list(_original_locator_methods))


# ---------------------------------------------------------------------------
# pytest plugin hooks
# ---------------------------------------------------------------------------


_QTEA_PHASE_MARKERS = ("smoke", "regression", "e2e", "exploratory")
# Opt-out marker for the Step 8 zero-assertions gate. Apply to tests that
# legitimately perform setup-only work (state mutation under a fixture, smoke
# probes that rely on fixture-internal asserts, etc.). Use sparingly — the
# gate logs every qtea_setup-marked function for audit.
_QTEA_OPT_OUT_MARKERS = ("setup",)


def pytest_configure(config):  # noqa: D401 - pytest hook signature
    """Register ``qtea_<phase>`` markers at collection time.

    Marker registration is cheap (no Playwright import) and prevents
    ``--strict-markers`` rejections. The monkey-patch is deferred to
    ``pytest_runtest_setup`` so that xdist workers don't all import
    Playwright and patch classes simultaneously during collection —
    a race that crashes worker processes on resource-constrained systems.
    """
    for phase in _QTEA_PHASE_MARKERS:
        config.addinivalue_line(
            "markers",
            f"qtea_{phase}: qtea generated {phase} test",
        )
    for marker in _QTEA_OPT_OUT_MARKERS:
        config.addinivalue_line(
            "markers",
            f"qtea_{marker}: qtea generated {marker} test (opts out of "
            f"the zero-assertions gate)",
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
# When ``QTEA_WORKSPACE_DIR`` is set by Step 9, this hook captures
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
# var (running outside qtea), or exception during capture all degrade
# silently to "no capture this session". Step 9 falls through to manual
# auth-replay in those cases.

_storage_state_captured: bool = False


# ---------------------------------------------------------------------------
# Passing-test witnesses for TBD promotion
# ---------------------------------------------------------------------------
#
# Step 9 freezes resolved cache entries back into SUT source via
# `_promote_resolved_tbds`. To avoid promoting an LLM guess that has never
# been validated by a real assertion, the promoter requires each candidate
# entry to carry at least one passing-test witness. This module records
# which sentinel resolutions each test consumed; at teardown we append the
# test's nodeid to `passing_witnesses` on every touched cache entry IF the
# test passed. Failing tests contribute nothing — their selectors don't get
# a vote on promotion.
#
# Implemented as in-process state (not in the cache) so the recording cost
# is one set insert per resolution; the actual cache update happens once
# per test at teardown.

_test_resolutions: dict[str, set] = {}


def _current_test_nodeid() -> str | None:
    """Strip the phase suffix pytest appends (`(call)` / `(setup)`)."""
    raw = os.environ.get("PYTEST_CURRENT_TEST", "")
    if not raw:
        return None
    nodeid = raw.rsplit(" (", 1)[0]
    return nodeid or None


def _record_resolution_use(resolution: "_Resolution") -> None:
    """Note that the currently-running test consumed this resolution.

    No-op when no test is active (e.g. fixture-stage resolution), or when
    the resolution failed (source="none"). Idempotent — uses a set per
    nodeid so repeat resolutions within one test count once.
    """
    nodeid = _current_test_nodeid()
    if not nodeid:
        return
    if resolution.source in (None, "none"):
        return
    bucket = _test_resolutions.setdefault(nodeid, set())
    bucket.add((resolution.intent, resolution.constant_name, resolution.test_file))


def _record_passing_witnesses(nodeid: str) -> None:
    """Append `nodeid` to `passing_witnesses` on each cache entry the test used.

    Called from `pytest_runtest_teardown` ONLY when the test passed. Updates
    are atomic via `_write_cache` (temp + rename), so concurrent xdist
    workers won't corrupt the file — last-writer-wins on overlap, which is
    fine because all writers are appending the same nodeid.
    """
    bucket = _test_resolutions.pop(nodeid, None)
    if not bucket:
        return
    try:
        cache = _read_cache()
    except OSError:
        return
    dirty = False
    for intent, constant_name, test_file in bucket:
        key = _cache_key(test_file, constant_name, intent)
        entry = cache.get(key)
        if not entry:
            continue
        witnesses = entry.get("passing_witnesses")
        if not isinstance(witnesses, list):
            witnesses = []
        if nodeid in witnesses:
            continue
        witnesses.append(nodeid)
        entry["passing_witnesses"] = witnesses
        cache[key] = entry
        dirty = True
    if dirty:
        try:
            _write_cache(cache)
        except OSError as e:
            log.warning("qtea.passing_witness_write_failed %s", e)


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
      - ``QTEA_WORKSPACE_DIR`` env var is set (Step 9 sets it)
      - this hook hasn't captured yet in the current session
      - the test we're tearing down PASSED (i.e. authenticated + ran assertions)
      - the test has a ``context`` fixture in scope (Playwright SUT)

    All failures degrade silently — capture is a best-effort optimization,
    not a correctness invariant. The heal flow still works without it.
    """
    # Record passing-test witnesses for TBD promotion. Independent from
    # storage-state capture: runs even when workspace_dir is unset (the
    # cache file may live elsewhere), and runs even after one test has
    # already captured storage state (we want every passing test to vote).
    rep_call = getattr(item, "rep_call", None)
    test_passed = (
        rep_call is None or getattr(rep_call, "passed", False) is True
    )
    if test_passed:
        _record_passing_witnesses(item.nodeid)
    else:
        _test_resolutions.pop(item.nodeid, None)

    global _storage_state_captured
    if _storage_state_captured:
        return
    workspace_dir = os.environ.get("QTEA_WORKSPACE_DIR")
    if not workspace_dir:
        return
    if not test_passed:
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
        # Route the state through the consent-cookie filter (Layer 6) before
        # persisting — dismissed cookie banners must NOT bake their acceptance
        # into the persisted state, or the consent flow's own regression tests
        # silently pass on subsequent runs. Filter is a no-op when
        # QTEA_OVERLAY_HANDLING=0.
        try:
            raw_state = context.storage_state()
        except TypeError:
            # Very old Playwright signatures accept only ``path=`` — fall back
            # to write-then-filter round-trip below.
            raw_state = None
        if isinstance(raw_state, dict):
            filtered_state, removed = _filter_consent_cookies_from_state(raw_state)
            out_path.write_text(
                json.dumps(filtered_state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            if removed:
                log.info(
                    "qtea.storage_state_consent_cookies_filtered "
                    "path=%s removed=%d",
                    out_path, removed,
                )
        else:
            # Fallback: write via Playwright (unfiltered), then filter-in-place.
            context.storage_state(path=str(out_path))
            try:
                on_disk = json.loads(out_path.read_text(encoding="utf-8"))
                if isinstance(on_disk, dict):
                    filtered_state, removed = _filter_consent_cookies_from_state(on_disk)
                    if removed:
                        out_path.write_text(
                            json.dumps(filtered_state, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        log.info(
                            "qtea.storage_state_consent_cookies_filtered "
                            "path=%s removed=%d (post-write)",
                            out_path, removed,
                        )
            except (OSError, json.JSONDecodeError) as e:
                log.debug("qtea.storage_state_post_filter_failed %s", e)
        _storage_state_captured = True
        log.info(
            "qtea.storage_state_captured path=%s test=%s",
            out_path, item.nodeid,
        )
    except Exception as e:  # noqa: BLE001 - capture is best-effort
        log.warning("qtea.storage_state_capture_failed %s", e)


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
