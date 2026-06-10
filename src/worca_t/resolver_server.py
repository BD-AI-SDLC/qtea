"""Parent-side TCP resolver server for the JIT locator runtime.

Why this exists
---------------

The vendored pytest runtime plugin runs *inside the SUT's pytest
subprocess*. That subprocess is launched via
:func:`worca_t.proxy.safe_subprocess_env`, which deliberately strips
``ANTHROPIC_API_KEY`` (and other secrets) from the inherited env to
prevent malicious SUT test code from exfiltrating the key via
``os.environ``.

Consequence: the previous design — having the pytest plugin shell out to
``worca-t resolve`` — silently failed at LLM time because the resolver
subprocess inherited the same scrubbed env. Resolution worked on cache
hits + dev-locators only; cold runs hit ``AuthenticationError`` (see
the comment block at ``steps/s09_execute.py:578-584``).

The bridge
----------

Step 9 starts a :class:`ResolverServer` *before* spawning pytest. The
server runs in the trusted parent process and HAS ``ANTHROPIC_API_KEY``
available. Pytest gets only two harmless env vars: ``WORCA_T_RESOLVER_PORT``
and ``WORCA_T_RESOLVER_TOKEN``. The pytest plugin connects to the loopback
port, authenticates with the per-run token, sends the resolution request,
and reads back a JSON result. The API key never enters the SUT subprocess.

Threat model
------------

- The token is one-time per run, valid only while the parent's
  ``ResolverServer`` is alive. Leaked tokens are useless after the run.
- The server binds 127.0.0.1 only — never reachable from off-host.
- Token comparison is constant-time (``hmac.compare_digest``).
- The server caps request size, per-connection time, and concurrent
  connections to bound resource use against a hostile SUT.

Wire protocol
-------------

Newline-delimited JSON. One request, one response, connection closes.
Same on-the-wire format will serve TS/Java runtimes in Phase 3.2.

Request::

    {
      "token": "<per-run secret>",
      "intent": "primary submit button on the login form",
      "constant_name": "LOGIN_BUTTON",
      "snapshot_text": "<AOM JSON as text>",
      "test_file": "tests/auth/test_login.py",
      "page_url": "https://app.example.com/login",
      "source_type": "aom"        // or "html" for non-Playwright stacks
    }

Response (success)::

    {
      "ok": true,
      "selector": "role=button[name=\"Sign in\"]",
      "strategy": "role",
      "confidence": 0.92,
      "source": "agent",
      "input_tokens": 1247,
      "output_tokens": 38,
      "duration_ms": 1830,
      "snapshot_hash": "9b1a..."
    }

Response (auth failure or error)::

    {"ok": false, "error": "<short reason>"}
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("worca_t.resolver_server")

# Wire-protocol safety caps. A hostile SUT could try to wedge the server
# by sending unbounded data or holding a connection open forever.
_MAX_REQUEST_BYTES: int = 4 * 1024 * 1024  # 4 MiB — enough for large AOM snapshots
_PER_CONN_TIMEOUT_S: float = 180.0
_ACCEPT_TIMEOUT_S: float = 0.5  # how often the accept loop checks the shutdown flag
_MAX_CONCURRENT_CONNS: int = 16


def _read_line(sock: socket.socket, max_bytes: int) -> bytes:
    """Read up to a single newline or ``max_bytes``, whichever comes first."""
    buf = bytearray()
    while len(buf) < max_bytes:
        chunk = sock.recv(min(8192, max_bytes - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl != -1:
            return bytes(buf[:nl])
    if len(buf) >= max_bytes:
        raise ValueError(f"request exceeded {max_bytes} bytes without newline")
    return bytes(buf)


class ResolverServer:
    """Single-port TCP bridge between the SUT pytest subprocess and the
    Anthropic-API-bearing parent process.

    Use as a context manager — the server starts on ``__enter__`` and is
    drained + shut down on ``__exit__`` so the API key cannot outlive the
    step that needed it::

        with ResolverServer(cache_dir=cache_dir, run_id=run_id) as srv:
            env["WORCA_T_RESOLVER_PORT"] = str(srv.port)
            env["WORCA_T_RESOLVER_TOKEN"] = srv.token
            subprocess.run(["pytest", ...], env=env, ...)
    """

    def __init__(
        self,
        *,
        cache_dir: Path | None,
        run_id: str | None = None,
        model: str | None = None,
        host: str = "127.0.0.1",
        # Tests inject these; production code uses the defaults.
        port: int = 0,
        token: str | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.run_id = run_id
        self.model = model
        self.host = host
        self._requested_port = port
        self.token: str = token or secrets.token_urlsafe(32)
        self._sock: socket.socket | None = None
        self.port: int = 0
        self._shutdown = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._conn_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_CONNS)
        self._stats_lock = threading.Lock()
        self._request_count: int = 0
        self._auth_failures: int = 0
        self._error_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> ResolverServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        """Bind the listening socket and start the accept thread."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self._requested_port))
        s.listen(_MAX_CONCURRENT_CONNS)
        s.settimeout(_ACCEPT_TIMEOUT_S)
        self._sock = s
        self.port = s.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="worca-t-resolver-accept", daemon=True,
        )
        self._accept_thread.start()
        log.info(
            "worca_t.resolver_server_started host=%s port=%d cache_dir=%s",
            self.host, self.port, self.cache_dir,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal shutdown, close the socket, drain the accept thread."""
        self._shutdown.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=timeout)
            self._accept_thread = None
        log.info(
            "worca_t.resolver_server_stopped requests=%d auth_failures=%d errors=%d",
            self._request_count, self._auth_failures, self._error_count,
        )

    # ------------------------------------------------------------------
    # Accept loop + per-connection handler
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._shutdown.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Socket closed during shutdown — exit cleanly.
                return
            if not self._conn_semaphore.acquire(blocking=False):
                # Too many in-flight; drop the connection rather than queue.
                log.warning("worca_t.resolver_server_overload — connection dropped")
                with contextlib.suppress(OSError):
                    conn.close()
                continue
            t = threading.Thread(
                target=self._handle_connection, args=(conn,),
                name="worca-t-resolver-conn", daemon=True,
            )
            t.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_PER_CONN_TIMEOUT_S)
            self._handle_request(conn)
        except Exception as e:  # noqa: BLE001 - bound to log + close
            log.warning("worca_t.resolver_server_conn_error %s: %s", type(e).__name__, e)
        finally:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
            self._conn_semaphore.release()

    def _handle_request(self, conn: socket.socket) -> None:
        try:
            line = _read_line(conn, _MAX_REQUEST_BYTES)
        except (ValueError, socket.timeout) as e:
            self._send(conn, {"ok": False, "error": f"read: {e}"})
            return
        if not line:
            self._send(conn, {"ok": False, "error": "empty request"})
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            self._send(conn, {"ok": False, "error": f"bad json: {e}"})
            return

        # Constant-time token comparison.
        supplied = str(req.get("token", ""))
        if not hmac.compare_digest(supplied, self.token):
            with self._stats_lock:
                self._auth_failures += 1
            log.warning("worca_t.resolver_server_auth_fail")
            self._send(conn, {"ok": False, "error": "auth"})
            return

        with self._stats_lock:
            self._request_count += 1

        try:
            response = self._dispatch(req)
        except Exception as e:  # noqa: BLE001 - return as error
            with self._stats_lock:
                self._error_count += 1
            log.warning(
                "worca_t.resolver_server_dispatch_error %s: %s",
                type(e).__name__, e,
            )
            self._send(conn, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._send(conn, response)

    def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        """Run the actual resolver. Lazy import to avoid loading Anthropic
        SDK on parent-process boot."""
        from worca_t import jit_resolver

        intent = str(req.get("intent") or "")
        constant_name = str(req.get("constant_name") or "")
        snapshot_text = str(req.get("snapshot_text") or "{}")
        test_file = req.get("test_file") or None
        page_url = req.get("page_url") or None
        source_type = str(req.get("source_type") or "aom")
        if source_type not in ("aom", "html"):
            return {"ok": False, "error": f"unknown source_type: {source_type}"}
        if not intent:
            return {"ok": False, "error": "missing intent"}
        if not constant_name:
            return {"ok": False, "error": "missing constant_name"}

        # `source_type` is reserved for Phase 4's on-failure heal flow where
        # non-Playwright stacks supply raw HTML; jit_resolver.resolve_one()
        # already treats the payload opaquely (it's just text to the LLM), so
        # for now we route both shapes through the same call and tag the
        # prompt variant in the response for telemetry.
        t0 = time.monotonic()
        result = jit_resolver.resolve_one(
            intent=intent,
            snapshot_text=snapshot_text,
            constant_name=constant_name,
            test_file=test_file,
            page_url=page_url,
            cache_dir=self.cache_dir,
            model=self.model,
            run_id=self.run_id,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        return {
            "ok": True,
            "selector": result.selector,
            "strategy": result.strategy,
            "confidence": result.confidence,
            "source": result.source,
            "snapshot_hash": result.snapshot_hash,
            "reason": result.reason,
            "resolved_at": result.resolved_at,
            # Telemetry — per-call cost data for Phase 6 aggregation.
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "model": result.model,
            "duration_ms": duration_ms,
            "source_type": source_type,
        }

    def _send(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            conn.sendall(line)
        except OSError as e:
            log.debug("worca_t.resolver_server_send_failed %s", e)

    # ------------------------------------------------------------------
    # Stats (test + introspection aid)
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "requests": self._request_count,
                "auth_failures": self._auth_failures,
                "errors": self._error_count,
            }


# ---------------------------------------------------------------------------
# Client helpers (used by Python runtime plugin AND by ad-hoc tests)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _client_socket(host: str, port: int, timeout: float) -> Iterator[socket.socket]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        yield s
    finally:
        with contextlib.suppress(OSError):
            s.close()


def call_resolver(
    *,
    port: int,
    token: str,
    intent: str,
    constant_name: str,
    snapshot_text: str,
    test_file: str | None = None,
    page_url: str | None = None,
    source_type: str = "aom",
    host: str = "127.0.0.1",
    timeout: float = 180.0,
) -> dict[str, Any] | None:
    """Send one resolution request to the local ResolverServer.

    Returns the parsed response dict (with ``ok: True``) on success, or
    ``None`` on transport / auth / server-side error. Caller treats
    ``None`` the same as ``source="unresolvable"`` (test fails fast).
    """
    request = {
        "token": token,
        "intent": intent,
        "constant_name": constant_name,
        "snapshot_text": snapshot_text,
        "test_file": test_file,
        "page_url": page_url,
        "source_type": source_type,
    }
    try:
        with _client_socket(host, port, timeout) as s:
            s.sendall(json.dumps(request).encode("utf-8") + b"\n")
            body = _read_line(s, _MAX_REQUEST_BYTES)
    except (OSError, ValueError) as e:
        log.warning("worca_t.resolver_client_transport_error %s", e)
        return None
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("worca_t.resolver_client_bad_json %s", e)
        return None
    if not payload.get("ok"):
        log.warning(
            "worca_t.resolver_client_server_error %s",
            payload.get("error"),
        )
        return None
    return payload
