"""Spawn the `claude` CLI per agent, stream stream-json output, enforce timeout.

This is the single execution path for every agent in worca-t. All step modules
funnel through `run_agent()`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worca_t.config import CLAUDE_SESSION_KEYS, SECRET_ENV_KEYS, get_settings, model_for_agent, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.mcp_manager import stage_mcp_config
from worca_t.proxy import with_proxy_env

log = get_logger(__name__)


@dataclass
class AgentResult:
    success: bool
    exit_code: int
    duration_s: float
    transcript_path: Path
    stderr_path: Path
    metrics_path: Path
    final_text: str = ""
    timed_out: bool = False
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def _agent_key(agent_path: Path) -> str:
    """Derive the agent->model lookup key from filename, e.g. 'refine-spec'."""
    name = agent_path.name
    for suffix in (".agent.md", ".prompt.md", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _stage_inputs(workdir: Path, inputs: dict[str, Path]) -> None:
    """Copy each input artifact into the agent workdir under its target name."""
    for target_name, src in inputs.items():
        if not src.exists():
            raise FileNotFoundError(f"Input artifact missing: {src} (label: {target_name})")
        dst = workdir / target_name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _stage_resources(
    workdir: Path,
    *,
    agent_path: Path,
    extra_paths: list[Path],
    mcp_source: Path | None,
    claude_md: Path | None,
) -> Path:
    """Copy agent file + skills/docs + CLAUDE.md + .mcp.json into workdir.

    Returns the destination path of the agent file inside the workdir.
    """
    agent_dst = workdir / agent_path.name
    shutil.copy2(agent_path, agent_dst)

    # Stage paired .prompt.md if it exists next to the agent.
    prompt_sibling = agent_path.with_name(agent_path.name.replace(".agent.md", ".prompt.md"))
    if prompt_sibling.exists() and prompt_sibling != agent_path:
        shutil.copy2(prompt_sibling, workdir / prompt_sibling.name)

    if claude_md and claude_md.exists():
        shutil.copy2(claude_md, workdir / "CLAUDE.md")

    for extra in extra_paths:
        if not extra.exists():
            log.warning("extra_path.missing", path=str(extra))
            continue
        dst = workdir / extra.name
        if extra.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(extra, dst)
        else:
            shutil.copy2(extra, dst)

    stage_mcp_config(workdir, source=mcp_source)
    return agent_dst


def _build_command(
    *,
    claude_bin: str,
    agent_path_in_workdir: Path,
    user_prompt: str,
    model: str | None,
    max_turns: int | None,
    permission_mode: str,
) -> list[str]:
    """Build the `claude` CLI argv."""
    cmd: list[str] = [
        claude_bin,
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--append-system-prompt",
        f"@{agent_path_in_workdir.name}",
        "--mcp-config",
        ".mcp.json",
        "--permission-mode",
        permission_mode,
    ]
    if model:
        cmd.extend(["--model", model])
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    cmd.append(user_prompt)
    return cmd


def _mask_env(env: dict[str, str]) -> dict[str, str]:
    return {k: ("***REDACTED***" if k in SECRET_ENV_KEYS else v) for k, v in env.items()}


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
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


def _drain_stream(
    stream: Any,
    transcript_fp: Any,
    events: list[dict[str, Any]],
    final_text_holder: list[str],
    on_event: Any | None,
) -> None:
    """Read newline-delimited JSON from claude's stdout, persist + parse."""
    for raw_line in iter(stream.readline, b""):
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        transcript_fp.write(line + "\n")
        transcript_fp.flush()
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            evt = {"type": "raw", "text": line}
        events.append(evt)
        # Best-effort: capture final assistant text for downstream parsing.
        etype = evt.get("type")
        if etype == "result" and isinstance(evt.get("result"), str):
            final_text_holder[0] = evt["result"]
        elif etype == "assistant":
            msg = evt.get("message") or {}
            for block in msg.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    final_text_holder[0] = block.get("text", "")
        if on_event:
            try:
                on_event(evt)
            except Exception:
                pass


def run_agent(
    agent_path: Path,
    *,
    workdir: Path,
    inputs: dict[str, Path],
    user_prompt: str,
    timeout_s: int | None = None,
    step: int | None = None,
    model: str | None = None,
    max_turns: int | None = 25,
    permission_mode: str = "acceptEdits",
    extra_paths: list[Path] | None = None,
    mcp_source: Path | None = None,
    claude_md: Path | None = None,
    on_event: Any | None = None,
    debug_live: bool = False,
) -> AgentResult:
    """Run a single agent via the `claude` CLI in an isolated workdir.

    Parameters
    ----------
    agent_path: path to the *.agent.md file (paired .prompt.md is auto-staged).
    workdir: dedicated directory for this agent run (created if missing).
    inputs: {filename_inside_workdir: source_path}.
    user_prompt: the user-turn message sent to claude.
    timeout_s: hard timeout; defaults to step_timeout(step) or 1800.
    model: explicit model id; otherwise resolved from agent_models.yaml.
    max_turns: cap on assistant turns (None to disable).
    permission_mode: claude permission mode for tool use.
    extra_paths: dirs/files to copy into the workdir (skills, docs, ...).
    mcp_source: override .mcp.json source path.
    claude_md: path to CLAUDE.md to stage at the workdir root.
    on_event: optional callback invoked for each parsed stream-json event.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    transcript_path = workdir / "transcript.jsonl"
    stderr_path = workdir / "stderr.log"
    metrics_path = workdir / "metrics.json"

    settings = get_settings()
    resolved_model = model or model_for_agent(_agent_key(agent_path))
    resolved_timeout = timeout_s if timeout_s is not None else step_timeout(step or 0)

    # Stage all files into the workdir.
    agent_in_wd = _stage_resources(
        workdir,
        agent_path=agent_path,
        extra_paths=list(extra_paths or []),
        mcp_source=mcp_source,
        claude_md=claude_md,
    )
    _stage_inputs(workdir, inputs)

    cmd = _build_command(
        claude_bin=settings.claude_bin,
        agent_path_in_workdir=agent_in_wd,
        user_prompt=user_prompt,
        model=resolved_model,
        max_turns=max_turns,
        permission_mode=permission_mode,
    )
    env = with_proxy_env()
    for key in CLAUDE_SESSION_KEYS:
        env.pop(key, None)

    safe_cmd = [*cmd[:6], "<system-prompt>", "...", "<user-prompt>"]
    relevant_env = {
        k: env[k]
        for k in env
        if k.startswith(("WORCA_", "ANTHROPIC_", "HTTP", "HTTPS", "NO_PROXY"))
    }
    log.info(
        "agent.start",
        agent=agent_path.name,
        model=resolved_model,
        workdir=str(workdir),
        timeout_s=resolved_timeout,
        cmd=safe_cmd,
    )
    log.debug("agent.env", env=_mask_env(relevant_env))

    started = time.monotonic()
    events: list[dict[str, Any]] = []
    final_text: list[str] = [""]
    timed_out = False
    error: str | None = None
    exit_code = -1

    if not shutil.which(settings.claude_bin):
        msg = f"`{settings.claude_bin}` not found on PATH"
        stderr_path.write_text(msg, encoding="utf-8")
        transcript_path.write_text("", encoding="utf-8")
        metrics_path.write_text(
            json.dumps(
                {"success": False, "exit_code": -1, "duration_s": 0.0, "error": msg},
                indent=2,
            ),
            encoding="utf-8",
        )
        log.error("agent.missing_binary", agent=agent_path.name, error=msg)
        return AgentResult(
            success=False,
            exit_code=-1,
            duration_s=0.0,
            transcript_path=transcript_path,
            stderr_path=stderr_path,
            metrics_path=metrics_path,
            error=msg,
        )

    transcript_fp = transcript_path.open("w", encoding="utf-8")
    stderr_fp = stderr_path.open("wb")
    # On Windows, .cmd/.bat wrappers must be invoked via cmd.exe.
    spawn_cmd = cmd
    if os.name == "nt":
        resolved = shutil.which(cmd[0])
        if resolved and resolved.lower().endswith((".cmd", ".bat")):
            spawn_cmd = ["cmd", "/c"] + cmd

    try:
        proc = subprocess.Popen(
            spawn_cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_fp,
            env=env,
            bufsize=0,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        )

        reader = threading.Thread(
            target=_drain_stream,
            args=(proc.stdout, transcript_fp, events, final_text, on_event),
            daemon=True,
        )
        reader.start()

        try:
            exit_code = proc.wait(timeout=resolved_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"timeout after {resolved_timeout}s"
            log.error("agent.timeout", agent=agent_path.name, timeout_s=resolved_timeout)
            _terminate_process(proc)
            exit_code = proc.wait(timeout=10)
        finally:
            reader.join(timeout=5)
    except Exception as e:
        error = f"spawn error: {e}"
        log.error("agent.spawn_error", agent=agent_path.name, error=str(e))
    finally:
        try:
            transcript_fp.close()
        except Exception:
            pass
        try:
            stderr_fp.close()
        except Exception:
            pass

    duration = time.monotonic() - started
    success = (exit_code == 0) and not timed_out and error is None

    metrics = {
        "agent": agent_path.name,
        "model": resolved_model,
        "success": success,
        "exit_code": exit_code,
        "duration_s": round(duration, 3),
        "timed_out": timed_out,
        "error": error,
        "event_count": len(events),
    }
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    log.info(
        "agent.end",
        agent=agent_path.name,
        success=success,
        exit_code=exit_code,
        duration_s=metrics["duration_s"],
        timed_out=timed_out,
    )

    return AgentResult(
        success=success,
        exit_code=exit_code,
        duration_s=duration,
        transcript_path=transcript_path,
        stderr_path=stderr_path,
        metrics_path=metrics_path,
        final_text=final_text[0],
        timed_out=timed_out,
        error=error,
        events=events,
    )
