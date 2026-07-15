"""`qtea doctor` - environment / dependency health checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table

from qtea.config import get_settings, package_resource_root
from qtea.mcp_manager import load_mcp_config, probe_server
from qtea.proxy import detected_proxies, with_proxy_env

Severity = Literal["ok", "warn", "fail", "info"]


@dataclass
class Check:
    name: str
    severity: Severity
    message: str


def _probe_cmd(args: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    # Resolve the executable first so Windows .CMD/.BAT wrappers (e.g. claude.CMD,
    # npx.CMD) are found. subprocess.run with shell=False cannot execute .CMD files
    # via bare name on Windows — CreateProcess needs the full path with extension.
    exe = shutil.which(args[0])
    if exe is None:
        return False, "not found on PATH"
    try:
        proc = subprocess.run(
            [exe, *args[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=with_proxy_env(),
            check=False,
        )
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode == 0, out
    except FileNotFoundError:
        return False, "not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, f"error: {e}"


def check_claude_cli() -> Check:
    settings = get_settings()
    ok, out = _probe_cmd([settings.claude_bin, "--version"])
    if ok:
        return Check("claude CLI", "ok", out.splitlines()[0] if out else "ok")
    return Check("claude CLI", "fail", f"`{settings.claude_bin}` unavailable: {out}")


def check_npx() -> Check:
    if not shutil.which("npx"):
        return Check("npx", "fail", "not found on PATH")
    ok, out = _probe_cmd(["npx", "--version"], timeout=15.0)
    if "timeout" in out:
        return Check("npx", "warn", "found but slow to respond (version check timed out)")
    return Check("npx", "ok" if ok else "fail", out)


def check_anthropic_key() -> Check:
    s = get_settings()
    if s.anthropic_api_key:
        src = (
            "ANTHROPIC_AUTH_TOKEN"
            if os.environ.get("ANTHROPIC_AUTH_TOKEN")
            else "ANTHROPIC_API_KEY"
        )
        return Check("ANTHROPIC_API_KEY", "ok", f"set (via {src})")
    return Check(
        "ANTHROPIC_API_KEY", "warn", "not set (claude CLI may use its own auth)"
    )


def check_proxy() -> Check:
    p = detected_proxies()
    if not p:
        return Check("proxy", "info", "no HTTP(S)_PROXY env vars detected")
    keys = ", ".join(sorted(p.keys()))
    return Check("proxy", "ok", f"detected: {keys}")


def check_mcp_config(target: Path) -> Check:
    cfg = target / ".mcp.json"
    if not cfg.exists():
        # Fall back to packaged default.
        cfg = package_resource_root() / ".mcp.json"
    if not cfg.exists():
        return Check(".mcp.json", "fail", "missing both in cwd and package resources")
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        servers = list((data.get("mcpServers") or {}).keys())
        return Check(".mcp.json", "ok", f"servers: {', '.join(servers)}")
    except Exception as e:
        return Check(".mcp.json", "fail", f"invalid: {e}")


def check_mcp_servers(target: Path) -> list[Check]:
    """Smoke-probe each MCP server (best-effort, ~8s per server).

    A probe failure is a real failure — broken MCPs make the pipeline's
    MCP preflight (run_pipeline) abort with exit code 2. Doctor must
    report the same severity, not hide it behind a softer ``warn``.
    """
    try:
        local = target / ".mcp.json"
        cfg_path = local if local.exists() else (package_resource_root() / ".mcp.json")
        servers = load_mcp_config(cfg_path)
    except Exception as e:
        return [Check("mcp servers", "fail", f"could not load: {e}")]
    out: list[Check] = []
    for name, srv in servers.items():
        ok, detail = probe_server(srv)
        out.append(Check(f"mcp:{name}", "ok" if ok else "fail", detail))
    return out


def check_schemas() -> Check:
    root = package_resource_root() / "schemas"
    if not root.exists():
        return Check(
            "schemas", "warn", "schemas dir missing (will be added in later milestone)"
        )
    files = list(root.glob("*.schema.json"))
    if not files:
        return Check("schemas", "warn", "no *.schema.json files yet")
    bad: list[str] = []
    for f in files:
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append(f"{f.name}: {e}")
    if bad:
        return Check("schemas", "fail", "; ".join(bad))
    return Check("schemas", "ok", f"{len(files)} schemas valid")


def check_workspace_writable(workspace: Path | None) -> Check:
    ws = workspace or Path(
        os.environ.get("QTEA_DEFAULT_WORKSPACE", str(Path.home() / ".qtea"))
    )
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe = ws / ".qtea-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return Check("workspace", "ok", str(ws.resolve()))
    except Exception as e:
        return Check("workspace", "fail", f"{ws}: {e}")


def check_allure() -> Check:
    if shutil.which("allure"):
        ok, out = _probe_cmd(["allure", "--version"])
        return Check("allure CLI", "ok" if ok else "info", out or "present")
    return Check(
        "allure CLI", "info", "not installed (built-in HTML fallback will be used)"
    )


def check_uv() -> Check:
    if shutil.which("uv"):
        ok, out = _probe_cmd(["uv", "--version"])
        return Check("uv", "ok" if ok else "warn", out or "present")
    return Check("uv", "warn", "not installed (only needed for `uv tool install`)")


def _find_venv_exe(name: str) -> str | None:
    """Look for an executable in the project-local .venv (fallback for PATH misses)."""
    venv = Path.cwd() / ".venv"
    candidates = (
        [venv / "Scripts" / f"{name}.exe", venv / "Scripts" / name]
        if os.name == "nt"
        else [venv / "bin" / name]
    )
    return next((str(p) for p in candidates if p.is_file()), None)


def check_ruff() -> Check:
    found = shutil.which("ruff") or _find_venv_exe("ruff")
    if found:
        try:
            proc = subprocess.run(
                [found, "--version"],
                capture_output=True, text=True, timeout=5.0, check=False,
            )
            out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            return Check("ruff", "ok" if proc.returncode == 0 else "warn", out or "present")
        except Exception as e:
            return Check("ruff", "warn", f"found but error: {e}")
    return Check("ruff", "warn", "not installed (dev only)")


def run_all_checks(
    target: Path,
    workspace: Path | None = None,
) -> list[Check]:
    # MCP probes always run. Skipping them by default is how a broken
    # `npx`/MCP setup escapes doctor unnoticed; the per-server probe is
    # the only authoritative signal that the pipeline can actually launch
    # agents on this machine.
    return [
        check_claude_cli(),
        check_npx(),
        check_anthropic_key(),
        check_proxy(),
        check_mcp_config(target),
        *check_mcp_servers(target),
        check_schemas(),
        check_workspace_writable(workspace),
        check_allure(),
        check_uv(),
        check_ruff(),
    ]


_SEVERITY_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "info": "cyan"}


def run_doctor(
    *,
    workspace: Path | None = None,
    console: Console | None = None,
    json_out: bool = False,
) -> int:
    console = console or Console()
    checks = run_all_checks(Path.cwd(), workspace=workspace)

    if json_out:
        console.print_json(data=[asdict(c) for c in checks])
    else:
        table = Table(title="qtea doctor", show_lines=False)
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        for c in checks:
            sev_style = _SEVERITY_STYLE[c.severity]
            table.add_row(c.name, f"[{sev_style}]{c.severity.upper()}[/]", c.message)
        console.print(table)

    has_fail = any(c.severity == "fail" for c in checks)
    return 1 if has_fail else 0
