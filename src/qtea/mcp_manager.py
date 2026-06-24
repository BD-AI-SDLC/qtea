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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qtea.config import package_resource_root
from qtea.proxy import safe_subprocess_env

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class McpServer:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]


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
        out[name] = McpServer(
            name=name,
            command=resolved.get("command", ""),
            args=_filter_empty_args(list(resolved.get("args") or [])),
            env={k: str(v) for k, v in (resolved.get("env") or {}).items()},
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
        for spec in servers.values():
            if isinstance(spec, dict) and "args" in spec:
                spec["args"] = _filter_empty_args(list(spec["args"] or []))
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
    """
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
