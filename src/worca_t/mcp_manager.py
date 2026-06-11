"""MCP config management + lightweight server-presence probing.

Worca-t delegates MCP hosting to the `claude` CLI (via the `--mcp-config` flag).
This module's job is to:
  - locate / validate `.mcp.json`
  - render a copy into a step workdir with env variable substitution applied
  - optionally probe whether each MCP server can be spawned (used by doctor)

## Per-call MCP isolation guarantee

worca-t never shares MCP subprocesses across `run_agent()` calls. The
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

from worca_t.config import package_resource_root
from worca_t.proxy import safe_subprocess_env, with_proxy_env

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


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${VAR} tokens with os.environ values; missing -> empty."""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    return value


def load_mcp_config(path: Path | None = None) -> dict[str, McpServer]:
    """Parse .mcp.json into typed McpServer entries with env substitution applied."""
    cfg_path = path or find_mcp_config()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers") or {}
    out: dict[str, McpServer] = {}
    for name, spec in servers_raw.items():
        resolved = _substitute_env(spec)
        out[name] = McpServer(
            name=name,
            command=resolved.get("command", ""),
            args=list(resolved.get("args") or []),
            env={k: str(v) for k, v in (resolved.get("env") or {}).items()},
        )
    return out


def stage_mcp_config(
    target_dir: Path,
    source: Path | None = None,
) -> Path:
    """Copy .mcp.json into `target_dir` (rendered with env substitution).

    The rendered file lets the spawned `claude` CLI read a stable config that
    already has env vars resolved (avoids subprocess-env edge cases).

    Note: worca-t does NOT rewrite the Playwright MCP's `--headless` flag.
    The MCP runs in the background for AOM snapshots / locator discovery and
    its UI is never user-facing. Its head state is controlled entirely by the
    project-local `.mcp.json` (typically `--headless`). The CLI `--headed`
    flag instead controls Step 8's *SUT test execution* (the real tests the
    user wants to watch), not the MCP.
    """
    src = source or find_mcp_config()
    raw = json.loads(src.read_text(encoding="utf-8"))
    rendered = _substitute_env(raw)
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / ".mcp.json"
    dst.write_text(json.dumps(rendered, indent=2), encoding="utf-8")
    return dst


def probe_server(server: McpServer, timeout_s: float = 8.0) -> tuple[bool, str]:
    """Best-effort: try to start the server briefly to verify it spawns.

    We can't speak the MCP handshake without an MCP client, so this is a
    smoke test - we start the process, give it ~timeout_s to either crash
    (FAIL) or stay alive (OK), then kill it.
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
