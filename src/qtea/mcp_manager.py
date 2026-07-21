"""MCP config management + lightweight server-presence probing.

Qtea-t delegates MCP hosting to the `claude` CLI (via the `--mcp-config` flag).
This module's job is to:
  - locate / validate `.mcp.json`
  - render a copy into a step workdir with env variable substitution applied
  - optionally probe whether each MCP server can be spawned (used by doctor)

## Per-call MCP isolation guarantee

qtea never shares MCP subprocesses across `run_agent()` calls. The
isolation comes from two independent mechanisms:

1. **Config staging is per-workdir.** `stage_mcp_config()` writes a fresh
   `.mcp.json` into the agent's workdir on every `run_agent()` invocation
   (see `claude_runner._stage_resources`). Each step has its own workdir,
   each agent dispatch has its own staged config.

2. **Subprocess lifecycle is per-call.** Each `run_agent()` spawns its own
   `claude` CLI subprocess via `claude_agent_sdk.query()`. That subprocess
   spawns its own MCP server children (e.g. `npx @playwright/mcp` →
   Playwright browser). The SDK's async-context-manager teardown closes the
   subprocess on normal completion; `claude_runner._force_cleanup` kills
   the subprocess tree explicitly on timeout / cancellation (only NEW
   PIDs not in `pre_existing_children`, so concurrent siblings are spared).

Consequence: Step 8a's Playwright browser does NOT leak into Step 8b or
Step 8. Step 8's first heal does not share a session with the last call
of Step 8. Each call's MCP server children die with that call's subprocess
tree.

Do not add cross-call MCP state to this module. If you find yourself
wanting to "reuse" a Playwright browser across steps for performance,
spawn the MCP server out-of-band and configure it as a long-lived remote
endpoint in `.mcp.json` — do NOT introduce shared state here.

**Read-only file paths are allowed.** Step 9 passes
``--storage-state=<path>`` to Playwright MCP via a token in `.mcp.json`
args (rendered via the optional ``env`` parameter on
``load_mcp_config`` / ``stage_mcp_config``). That is *not* shared
subprocess state — it's a per-run file path injected into the spawn
arguments. The MCP server still spawns fresh, dies with its caller's
subprocess tree, and shares no live connection across calls. Keep the
distinction tight: read-only file paths YES, live subprocess / browser
handles NO.

## MCP staging is opt-in per `run_agent` call

`run_agent` defaults to staging an empty `.mcp.json` via
`stage_empty_mcp_config` — no MCP servers spawn. Callers that audited
their agent and confirmed it uses an MCP tool (currently only step 9's
`polyglot-test-fixer` heal flow needs Playwright) must pass
`enable_mcp=True` to opt back into `stage_mcp_config`. This eliminates
the wasted MCP boot cost on the 5+ call sites that never use MCP and
silences the cosmetic `agent.mcp.pending_at_init` warning on those
calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qtea.config import package_resource_root
from qtea.proxy import safe_subprocess_env, with_proxy_env

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class McpServer:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    type: str = "stdio"
    url: str = ""


def find_mcp_config(start: Path | None = None) -> Path:
    """Locate .mcp.json. Prefers the supplied dir / cwd, falls back to package resource."""
    candidates = []
    if start:
        candidates.append(start / ".mcp.json")
    candidates.append(Path.cwd() / ".mcp.json")
    candidates.append(package_resource_root() / ".mcp.json")
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"No .mcp.json found in: {[str(c) for c in candidates]}")


def _substitute_env(
    value: Any,
    env: dict[str, str] | None = None,
) -> Any:
    """Recursively replace ``${VAR}`` tokens with environment values.

    Lookup order per token:
      1. ``env`` dict (when provided) — for per-call MCP env injection
         (e.g. ``QTEA_STORAGE_STATE_ARG`` from Step 9).
      2. ``os.environ`` — fallback for tokens not in ``env``.

    Missing tokens collapse to the empty string. Empty-string args are
    NOT filtered here — that's the caller's job (see
    ``_filter_empty_args``). Keeping this function purely substitutive
    makes it easier to test and reason about.
    """
    if isinstance(value, str):
        def _lookup(m: re.Match[str]) -> str:
            name = m.group(1)
            if env is not None and name in env:
                return env[name]
            return os.environ.get(name, "")
        return _ENV_VAR_PATTERN.sub(_lookup, value)
    if isinstance(value, list):
        return [_substitute_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_env(v, env) for k, v in value.items()}
    return value


def _filter_empty_args(args: list[Any]) -> list[str]:
    """Drop empty-string entries from a resolved args list.

    Templates use ``${OPTIONAL_TOKEN}`` for conditionally-present flags.
    When the token resolves to an empty string we don't want to forward
    a stray ``""`` to the MCP subprocess — argparse-style CLIs (including
    ``@playwright/mcp``) treat empty args as positional placeholders and
    error out. Filter once, here, so every consumer benefits.
    """
    return [str(a) for a in args if a not in ("", None)]


# ---------------------------------------------------------------------------
# Playwright MCP launch optimisation: bypass npx via a pinned local install
# ---------------------------------------------------------------------------
#
# ``npx @playwright/mcp@<v>`` re-resolves the package tree and re-spawns node on
# EVERY invocation. On corporate Windows hosts (real-time AV scanning the npm
# cache) that overhead can be enormous -- on corporate networks with AV
# scanning of the npm cache, spawn times of ~2-3 MINUTES have been observed
# vs ~4-6 s for a direct ``node cli.js``. The Agent SDK freezes an agent's
# tool list once ``MCP_TIMEOUT`` (60 s) elapses, so a slow npx spawn leaves
# the Playwright server ``pending`` for the whole run (surfaced as
# ``step07.live_explore_mcp_unavailable`` -- 0 routes captured).
#
# Fix: install the pinned version ONCE into a qtea-managed dir and invoke its
# ``cli.js`` directly with node. The committed ``.mcp.json`` keeps the npx form
# as a zero-setup fallback; ``_rewrite_npx_playwright_to_node`` only swaps it
# for the node form when a pinned install is actually present. Still stdio,
# still per-call isolation — this only changes HOW the same server is launched.

PLAYWRIGHT_SERVER_NAME = "playwright"
_PLAYWRIGHT_PKG = "@playwright/mcp"
# Fallback only — the authoritative version is the ``@playwright/mcp@<v>`` pin
# in ``.mcp.json`` (parsed by ``pinned_playwright_version``). Keep in sync.
_DEFAULT_PLAYWRIGHT_MCP_VERSION = "0.0.78"


def playwright_mcp_install_dir() -> Path:
    """Stable, qtea-managed dir holding the pinned ``@playwright/mcp`` install.

    Override with ``QTEA_MCP_INSTALL_DIR``; defaults to ``~/.qtea/mcp``. This is
    pipeline-managed state (like ``~/.qtea/incident-memory``), never written by
    agents — it holds the ``node_modules`` for the direct-node launch.
    """
    override = os.environ.get("QTEA_MCP_INSTALL_DIR")
    if override:
        return Path(override)
    return Path.home() / ".qtea" / "mcp"


def resolve_playwright_cli() -> Path | None:
    """Path to a pinned ``@playwright/mcp`` ``cli.js``, or ``None``.

    Order: ``QTEA_PLAYWRIGHT_MCP_CLI`` env override, then the qtea-managed
    install dir. ``None`` means no pinned install → callers keep the npx form.
    """
    override = os.environ.get("QTEA_PLAYWRIGHT_MCP_CLI")
    if override and Path(override).is_file():
        return Path(override)
    cli = (
        playwright_mcp_install_dir()
        / "node_modules" / "@playwright" / "mcp" / "cli.js"
    )
    return cli if cli.is_file() else None


def pinned_playwright_version(source: Path | None = None) -> str:
    """Return the ``@playwright/mcp`` version pinned in ``.mcp.json``.

    Single source of truth: parses ``@playwright/mcp@<v>`` from the playwright
    server's args so the managed install and the config never drift. Falls back
    to :data:`_DEFAULT_PLAYWRIGHT_MCP_VERSION` when unreadable / unpinned.
    """
    try:
        cfg = source or find_mcp_config()
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        spec = (raw.get("mcpServers") or {}).get(PLAYWRIGHT_SERVER_NAME) or {}
        for arg in spec.get("args") or []:
            if isinstance(arg, str) and arg.startswith(_PLAYWRIGHT_PKG + "@"):
                return arg.split("@", 2)[-1]
    except (OSError, ValueError, KeyError):
        pass
    return _DEFAULT_PLAYWRIGHT_MCP_VERSION


def _rewrite_npx_playwright_to_node(
    name: str, spec: dict[str, Any],
) -> dict[str, Any]:
    """Swap an npx-based playwright spec for a direct ``node cli.js`` launch.

    No-op unless (a) this is the playwright server, (b) it's the npx form, and
    (c) a pinned install exists. Keeps every arg AFTER the package spec
    (``--headless``, storage-state, …) so the launch is behaviourally identical
    — only the launcher changes. Returns a NEW dict; never mutates ``spec``.
    """
    if name != PLAYWRIGHT_SERVER_NAME or not isinstance(spec, dict):
        return spec
    if "npx" not in str(spec.get("command", "")).lower():
        return spec  # already node/other — leave untouched
    args = list(spec.get("args") or [])
    pkg_idx = next(
        (i for i, a in enumerate(args)
         if isinstance(a, str) and a.startswith(_PLAYWRIGHT_PKG)),
        None,
    )
    if pkg_idx is None:
        return spec
    cli = resolve_playwright_cli()
    if cli is None:
        return spec  # no pinned install → keep the npx fallback
    return {**spec, "command": "node", "args": [str(cli), *args[pkg_idx + 1:]]}


def ensure_playwright_mcp_installed(
    version: str | None = None, timeout_s: float = 900.0,
) -> tuple[bool, str]:
    """Ensure a pinned ``@playwright/mcp`` is installed in the managed dir.

    Idempotent: a fast no-op when :func:`resolve_playwright_cli` already finds
    the ``cli.js``. Otherwise runs ``npm install --prefix <dir>
    @playwright/mcp@<v>`` ONCE — the setup step that unlocks the direct-node
    launch. Best-effort: returns ``(False, reason)`` on any failure so callers
    can transparently fall back to npx.
    """
    existing = resolve_playwright_cli()
    if existing is not None:
        return True, f"present: {existing}"
    if os.environ.get("QTEA_MCP_NO_AUTO_INSTALL"):
        # Managed/air-gapped hosts provision the install out-of-band; callers
        # then fall back to npx. Also the switch tests use to stay hermetic.
        return False, "auto-install disabled (QTEA_MCP_NO_AUTO_INSTALL)"
    npm = shutil.which("npm")
    if not npm:
        return False, "npm not on PATH"
    version = version or pinned_playwright_version()
    install_dir = playwright_mcp_install_dir()
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"cannot create install dir: {e}"
    cmd = [
        npm, "install", "--prefix", str(install_dir),
        f"{_PLAYWRIGHT_PKG}@{version}",
        "--no-audit", "--no-fund", "--prefer-offline", "--loglevel=error",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            env=safe_subprocess_env(), timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"npm install timed out after {timeout_s:.0f}s"
    except OSError as e:
        return False, f"npm install spawn error: {e}"
    cli = resolve_playwright_cli()
    if cli is not None:
        return True, f"installed {version}: {cli}"
    return False, (
        f"npm install rc={proc.returncode}: "
        f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
    )


def _playwright_browsers_path() -> Path:
    """Default Playwright browser cache dir (honours ``PLAYWRIGHT_BROWSERS_PATH``)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _has_chromium_build() -> bool:
    """Cheap heuristic: any ``chromium*`` build present in the browser cache.

    A heuristic, not a guarantee — the exact revision @playwright/mcp needs may
    still be absent (the server then downloads it on first navigate). Used to
    skip the (slower) install on the hot path; ``doctor`` runs the authoritative
    ``install chromium`` regardless.
    """
    try:
        return any(
            c.is_dir() and c.name.startswith("chromium")
            for c in _playwright_browsers_path().iterdir()
        )
    except OSError:
        return False


def ensure_playwright_mcp_browser(
    timeout_s: float = 600.0, *, force: bool = False,
) -> tuple[bool, str]:
    """Ensure the Chromium build @playwright/mcp uses is installed.

    Uses the *version-matched* Playwright bundled inside the managed install
    (``node <dir>/node_modules/playwright/cli.js install chromium``) so the
    revision matches what the pinned server expects — ``npx playwright install
    chromium`` would fetch an unrelated standard build. Idempotent (Playwright's
    own install no-ops when the revision is present).

    On the hot path pass ``force=False`` (default): a cheap ``chromium*``
    presence check short-circuits so warm runs pay nothing. ``doctor`` passes
    ``force=True`` for an authoritative, revision-exact install.
    """
    if not force and _has_chromium_build():
        return True, "chromium present (fs check)"
    if os.environ.get("QTEA_MCP_NO_AUTO_INSTALL"):
        return False, "auto-install disabled (QTEA_MCP_NO_AUTO_INSTALL)"
    node = shutil.which("node")
    pw_cli = (
        playwright_mcp_install_dir()
        / "node_modules" / "playwright" / "cli.js"
    )
    if not node or not pw_cli.is_file():
        return False, "managed playwright not installed (run ensure_playwright_mcp_installed first)"
    try:
        proc = subprocess.run(
            [node, str(pw_cli), "install", "chromium"],
            capture_output=True, text=True,
            env=safe_subprocess_env(), timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"browser install timed out after {timeout_s:.0f}s"
    except OSError as e:
        return False, f"browser install spawn error: {e}"
    if proc.returncode == 0:
        return True, "chromium installed (version-matched)"
    return False, (
        f"browser install rc={proc.returncode}: "
        f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
    )


def load_mcp_config(
    path: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, McpServer]:
    """Parse ``.mcp.json`` into typed ``McpServer`` entries.

    ``env`` overlays ``os.environ`` for token substitution — used by
    Step 9 to inject the storage-state CLI flag into Playwright MCP's
    args without mutating process env. Empty-string args produced by
    optional tokens are dropped via ``_filter_empty_args``.
    """
    cfg_path = path or find_mcp_config()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers") or {}
    out: dict[str, McpServer] = {}
    for name, spec in servers_raw.items():
        resolved = _substitute_env(spec, env)
        resolved = _rewrite_npx_playwright_to_node(name, resolved)
        out[name] = McpServer(
            name=name,
            command=resolved.get("command", ""),
            args=_filter_empty_args(list(resolved.get("args") or [])),
            env={k: str(v) for k, v in (resolved.get("env") or {}).items()},
            type=resolved.get("type", "stdio"),
            url=resolved.get("url", ""),
        )
    return out


def stage_mcp_config(
    target_dir: Path,
    source: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Copy ``.mcp.json`` into ``target_dir`` (rendered with env substitution).

    The rendered file lets the spawned ``claude`` CLI read a stable config
    that already has env vars resolved (avoids subprocess-env edge cases).
    ``env`` overlays ``os.environ`` — same semantics as
    :func:`load_mcp_config`. Empty-string args left over from optional
    tokens are filtered out before writing.

    Note: qtea does NOT rewrite the Playwright MCP's ``--headless`` flag.
    The MCP runs in the background for AOM snapshots / locator discovery and
    its UI is never user-facing. Its head state is controlled entirely by the
    project-local ``.mcp.json`` (typically ``--headless``). The CLI
    ``--headed`` flag instead controls Step 8's *SUT test execution* (the
    real tests the user wants to watch), not the MCP.
    """
    src = source or find_mcp_config()
    raw = json.loads(src.read_text(encoding="utf-8"))
    rendered = _substitute_env(raw, env)
    # Apply the empty-arg filter per server so the staged file matches what
    # `load_mcp_config(env=...)` would produce when the claude CLI re-reads
    # it. Without this, the staged file carries spurious "" args that the
    # MCP subprocess will choke on.
    if isinstance(rendered, dict):
        servers = rendered.get("mcpServers") or {}
        for name, spec in list(servers.items()):
            if not isinstance(spec, dict):
                continue
            # Rewrite BEFORE filtering: the npx→node swap drops `-y` + the
            # package spec, and the empty-arg filter then removes any unset
            # optional tokens (e.g. storage-state) in the surviving tail.
            spec = _rewrite_npx_playwright_to_node(name, spec)
            if "args" in spec:
                spec["args"] = _filter_empty_args(list(spec.get("args") or []))
            servers[name] = spec
        rendered["mcpServers"] = servers
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / ".mcp.json"
    dst.write_text(json.dumps(rendered, indent=2), encoding="utf-8")
    return dst


def stage_empty_mcp_config(target_dir: Path) -> Path:
    """Write an explicitly empty .mcp.json so the SDK reads no MCP servers.

    This is the no-spawn variant used by `run_agent` when `enable_mcp=False`
    (the default). An explicit `{"mcpServers": {}}` file is safer than file
    absence: `claude_runner` passes `setting_sources=["project"]` to the SDK,
    which makes the workdir's `.mcp.json` authoritative. A missing file is
    ambiguous (depending on SDK version, may either skip MCPs or surface a
    config-error warning); an explicit empty config is unambiguous and yields
    `mcp_servers: []` in the agent's init message.

    CLAUDE.md and other project settings still load normally — only the MCP
    server list is empty.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / ".mcp.json"
    dst.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    return dst


def _probe_http(url: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Probe an HTTP/SSE MCP endpoint. Any HTTP response means the server is up.

    Uses the same proxy env as subprocess callers (``with_proxy_env``) so
    that Windows-registry proxy settings — which may not be in ``os.environ``
    in all shell contexts — are honoured.
    """
    if not url:
        return False, "no url configured"
    import urllib.error
    import urllib.request

    env = with_proxy_env()
    proxies: dict[str, str] = {}
    for key, val in env.items():
        if key.lower() == "http_proxy" and "http" not in proxies:
            proxies["http"] = val
        elif key.lower() == "https_proxy" and "https" not in proxies:
            proxies["https"] = val
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    try:
        with opener.open(url, timeout=timeout_s) as resp:
            return True, f"http endpoint reachable (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        # 4xx/5xx still means the endpoint answered — it's reachable.
        return True, f"http endpoint reachable (HTTP {e.code})"
    except OSError as e:
        return False, f"http endpoint unreachable: {e}"
    except Exception as e:
        return False, f"http endpoint unreachable: {e}"


def probe_server(server: McpServer, timeout_s: float = 30.0) -> tuple[bool, str]:
    """Best-effort: try to start the server briefly to verify it spawns.

    We can't speak the MCP handshake without an MCP client, so this is a
    smoke test - we start the process, give it ~timeout_s to either crash
    (FAIL) or stay alive (OK), then kill it.

    The probe doubles as a warmup: leaving the server running for longer
    fills the npx cache more thoroughly and lets the server complete more
    of its lazy init (e.g. Playwright's browser binary check). When the
    agent's `claude` subprocess later spawns its own copy of the server,
    that copy reaches `connected` status faster — eliminating the
    `mcp_servers: [{status: "pending"}]` race observed at step 8 init
    (see run 20260611-075728-0aa560 RCA).

    Default raised from 15 s to 30 s after run 20260611-184450 showed
    Playwright still reporting `pending` at SDK init even with the 15 s
    warmup — the first-run Chromium binary download / verify can blow
    past 15 s on a cold cache. Combined with the move to per-step lazy
    preflight (see `pipeline._mcp_preflight_for_step`), the 30 s window
    now runs contiguously with the SDK spawn instead of 18 minutes
    before it, so the warmup actually sticks.

    HTTP-type servers (``"type": "http"`` in ``.mcp.json``) are probed
    via a lightweight HTTP GET rather than a subprocess spawn.
    """
    if server.type == "http":
        return _probe_http(server.url, timeout_s=min(timeout_s, 10.0))
    if not server.command:
        return False, "no command"
    resolved = shutil.which(server.command)
    if not resolved:
        return False, f"`{server.command}` not on PATH"

    # Use the resolved path (not the bare name) so Windows CreateProcess can
    # spawn .cmd / .bat wrappers like `npx.CMD` without needing shell=True.
    env = safe_subprocess_env(server.env)
    try:
        proc = subprocess.Popen(
            [resolved, *server.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        return False, f"spawn error: {e}"

    try:
        # Wait briefly; if it exits within timeout it's a failure to start.
        try:
            rc = proc.wait(timeout=timeout_s)
            err_bytes = proc.stderr.read() if proc.stderr else b""
            stderr = (err_bytes or b"").decode("utf-8", errors="replace")
            return False, f"exited rc={rc}: {stderr.strip()[:200]}"
        except subprocess.TimeoutExpired:
            # Still running -> consider it healthy enough for a smoke probe.
            return True, "spawned ok"
    finally:
        _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Windows: kill the whole tree.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _mcp_initialize_handshake(
    proc: subprocess.Popen, timeout_s: float,
) -> tuple[bool, str]:
    """Drive one MCP ``initialize`` request over stdio and await the response.

    Unlike ``probe_server``'s "did it crash?" smoke test, a completed handshake
    proves the server can actually speak MCP — the strongest readiness signal we
    can get without a full client. Version-tolerant: any well-formed JSON-RPC
    response to our ``id`` counts (the server echoes its own protocolVersion),
    so a protocol mismatch still reads as "up". Non-JSON stdout lines (log
    banners) are skipped. A background thread drains stderr so a chatty server
    can't deadlock on a full pipe.
    """
    import queue
    import threading
    import time as _time

    out_q: queue.Queue[str | None] = queue.Queue()
    err_lines: list[str] = []

    def _drain(stream, sink, is_stdout: bool) -> None:
        try:
            for line in stream:
                sink(line)
        except Exception:
            pass
        finally:
            if is_stdout:
                out_q.put(None)  # stdout EOF sentinel — unblocks the reader

    threading.Thread(
        target=_drain, args=(proc.stdout, out_q.put, True), daemon=True,
    ).start()
    threading.Thread(
        target=_drain, args=(proc.stderr, err_lines.append, False), daemon=True,
    ).start()

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qtea-warmup", "version": "1.0"},
        },
    }
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
    except OSError as e:
        return False, f"stdin write failed: {e}"

    deadline = _time.monotonic() + timeout_s
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return False, f"no initialize response within {timeout_s:.0f}s"
        try:
            line = out_q.get(timeout=remaining)
        except queue.Empty:
            return False, f"no initialize response within {timeout_s:.0f}s"
        if line is None:  # stdout closed before responding
            detail = "".join(err_lines).strip()[:200]
            return False, f"server closed before responding: {detail}"
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # skip non-JSON log output
        if isinstance(msg, dict) and msg.get("id") == 1:
            if "result" in msg:
                return True, "mcp initialize ok"
            return False, f"initialize error: {msg.get('error')}"


def warm_mcp_server(server: McpServer, timeout_s: float = 60.0) -> tuple[bool, str]:
    """Warm + readiness-probe a stdio MCP server via a real MCP handshake.

    Supersedes ``probe_server`` on the warm paths (Step 7 live-explore, Step 9
    heal): it returns as soon as the server answers ``initialize`` — typically a
    few seconds with the direct-node launch — instead of always burning the full
    ``timeout_s`` like the smoke probe. A ``True`` here means the copy the Agent
    SDK spawns moments later will reach ``connected`` before the tool list
    freezes. HTTP servers still route through the lightweight HTTP probe.
    """
    if server.type == "http":
        return _probe_http(server.url, timeout_s=min(timeout_s, 10.0))
    if not server.command:
        return False, "no command"
    resolved = shutil.which(server.command)
    if not resolved:
        return False, f"`{server.command}` not on PATH"
    env = safe_subprocess_env(server.env)
    try:
        proc = subprocess.Popen(
            [resolved, *server.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        return False, f"spawn error: {e}"
    try:
        return _mcp_initialize_handshake(proc, timeout_s)
    finally:
        _terminate(proc)
