"""MCP config management + lightweight server-presence probing.

Worca-t delegates MCP hosting to the `claude` CLI (via the `--mcp-config` flag).
This module's job is to:
  - locate / validate `.mcp.json`
  - render a copy into a step workdir with env variable substitution applied
  - optionally probe whether each MCP server can be spawned (used by doctor)
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
from worca_t.proxy import with_proxy_env

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


def stage_mcp_config(target_dir: Path, source: Path | None = None) -> Path:
    """Copy .mcp.json into `target_dir` (rendered with env substitution).

    The rendered file lets the spawned `claude` CLI read a stable config that
    already has env vars resolved (avoids subprocess-env edge cases).
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
    if not shutil.which(server.command):
        return False, f"`{server.command}` not on PATH"

    env = with_proxy_env(server.env)
    try:
        proc = subprocess.Popen(
            [server.command, *server.args],
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
