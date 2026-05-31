"""Drive a single agent via the Claude Agent SDK in an isolated workdir.

This is the single execution path for every agent in worca-t. All step modules
funnel through `run_agent()`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    query,
)

from worca_t.config import CLAUDE_SESSION_KEYS, SECRET_ENV_KEYS, get_settings, model_for_agent, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.mcp_manager import stage_mcp_config
from worca_t.metrics import CURRENT_STEP_METRICS, AgentMetrics, extract_agent_metrics
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
    metrics: AgentMetrics = field(default_factory=AgentMetrics)


# MCP tool allowlist. The SDK's `permission_mode="acceptEdits"` does NOT
# auto-approve MCP tools (only file edits and filesystem Bash) — the CLI behaved
# differently. Pre-approve every MCP server we ship with so steps that touch
# Playwright / Chrome DevTools / Atlassian don't stall on permission prompts.
_MCP_ALLOWLIST: tuple[str, ...] = (
    "mcp__playwright__*",
    "mcp__chrome-devtools__*",
    "mcp__atlassian__*",
)

# Sanity threshold for an unusually large agent file (informational only).
_AGENT_PROMPT_WARN_BYTES = 30_000


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


def _mask_env(env: dict[str, str]) -> dict[str, str]:
    return {k: ("***REDACTED***" if k in SECRET_ENV_KEYS else v) for k, v in env.items()}


def _message_to_dict(message: Any) -> dict[str, Any]:
    """Best-effort serialization of an SDK Message to a JSON-safe dict.

    Preserves the audit trail in `transcript.jsonl`. Shape differs from the
    pre-SDK stream-json events but contains the same information.
    """
    out: dict[str, Any] = {"type": message.__class__.__name__}
    for attr in ("subtype", "session_id", "result", "data", "content",
                 "parent_tool_use_id", "stop_reason", "usage", "total_cost_usd"):
        if hasattr(message, attr):
            value = getattr(message, attr)
            if value is None:
                continue
            try:
                out[attr] = _coerce_json(value)
            except Exception:
                out[attr] = repr(value)
    return out


def _coerce_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _coerce_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_json(v) for v in value]
    if is_dataclass(value):
        return {f.name: _coerce_json(getattr(value, f.name))
                for f in value.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    if hasattr(value, "__dict__"):
        return {k: _coerce_json(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _extract_text_from_blocks(content: Any) -> str:
    """Walk an AssistantMessage's `content` and return the last text block."""
    if not isinstance(content, list):
        return ""
    last = ""
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            if block.get("type") == "text":
                text = block.get("text")
        if isinstance(text, str) and text:
            last = text
    return last


async def _drive_query(
    *,
    user_prompt: str,
    options: ClaudeAgentOptions,
    transcript_path: Path,
    on_event: Any | None,
) -> tuple[list[dict[str, Any]], str, AgentMetrics]:
    """Iterate the SDK query, write transcript, return (events, final_text, metrics)."""
    events: list[dict[str, Any]] = []
    final_text = ""
    # ResultMessage typically appears once at end-of-turn, but if the SDK ever
    # emits more than one we sum them so caller never under-counts.
    totals = AgentMetrics()

    with transcript_path.open("w", encoding="utf-8") as transcript_fp:
        async for message in query(prompt=user_prompt, options=options):
            evt = _message_to_dict(message)
            transcript_fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
            transcript_fp.flush()
            events.append(evt)

            if isinstance(message, SystemMessage) and getattr(message, "subtype", None) == "init":
                data = getattr(message, "data", {}) or {}
                servers = data.get("mcp_servers") or []
                # SDK status enum: connected | failed | needs-auth | pending | disabled.
                # `pending` is transient (server still starting at init-message time)
                # and `disabled` is intentional, so only flag the genuinely bad states.
                bad = [s for s in servers
                       if isinstance(s, dict) and s.get("status") in ("failed", "needs-auth")]
                if bad:
                    log.warning("agent.mcp.failed_servers", failed=bad)
            elif isinstance(message, AssistantMessage):
                text = _extract_text_from_blocks(getattr(message, "content", None))
                if text:
                    final_text = text
            elif isinstance(message, ResultMessage):
                result = getattr(message, "result", None)
                if isinstance(result, str) and result:
                    final_text = result
                m = extract_agent_metrics(
                    getattr(message, "usage", None),
                    getattr(message, "total_cost_usd", None),
                )
                turns = getattr(message, "num_turns", None)
                if isinstance(turns, int):
                    m.num_turns = turns
                totals.input_tokens += m.input_tokens
                totals.output_tokens += m.output_tokens
                totals.cache_creation_input_tokens += m.cache_creation_input_tokens
                totals.cache_read_input_tokens += m.cache_read_input_tokens
                totals.cost_usd += m.cost_usd
                totals.num_turns += m.num_turns
            elif isinstance(message, UserMessage):
                # User-turn echo from the SDK (e.g., tool-result feedback).
                pass

            if on_event:
                try:
                    on_event(evt)
                except Exception:
                    pass

    return events, final_text, totals


async def run_agent(
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
    """Run a single agent via the Claude Agent SDK in an isolated workdir.

    The ``workdir`` passed here is a staging-only directory. ``inputs``
    and ``extra_paths`` are copied IN at the start of each call; the
    agent's outputs land here and the caller is expected to move/copy
    the relevant ones into ``artifacts/stepNN/`` for publication. No
    file in the workdir should be assumed to persist meaningfully across
    ``run_agent`` invocations -- callers must re-stage everything they
    need on every call. See ``worca_t.steps.base.Step`` docstring for
    the full workdir-vs-out_dir contract.

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
    on_event: optional callback invoked for each parsed event.
    debug_live: accepted for API compatibility; currently no-op.
    """
    del debug_live  # accepted for API compat
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

    try:
        agent_size = agent_in_wd.stat().st_size
    except OSError as e:
        raise RuntimeError(
            f"failed to stat agent file: {agent_in_wd} ({e})"
        ) from e
    if agent_size > _AGENT_PROMPT_WARN_BYTES:
        log.warning("agent.system_prompt.large", agent=agent_in_wd.name, bytes=agent_size)

    agent_md_text = agent_in_wd.read_text(encoding="utf-8")

    # The SDK still shells out to the `claude` Code binary internally. Keep the
    # missing-binary precheck so the failure mode is identical to today.
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

    # Build env: proxy + filtered Anthropic/WORCA keys, with Claude session
    # keys stripped so the SDK doesn't think it's running nested.
    full_env = with_proxy_env()
    for key in CLAUDE_SESSION_KEYS:
        full_env.pop(key, None)
    forwarded_env = {
        k: full_env[k]
        for k in full_env
        if k.startswith(("WORCA_", "ANTHROPIC_", "HTTP", "HTTPS", "NO_PROXY"))
    }

    sdk_options_kwargs: dict[str, Any] = {
        "cwd": str(workdir),
        # Append our agent .md to the Claude Code preset system prompt — same
        # semantic as the deprecated `--append-system-prompt-file` flag.
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": agent_md_text,
        },
        "permission_mode": permission_mode,
        # Load .mcp.json + CLAUDE.md staged into the workdir.
        "setting_sources": ["project"],
        # Pre-approve our MCP tools so steps don't stall on permission prompts.
        "allowed_tools": list(_MCP_ALLOWLIST),
        "env": forwarded_env,
    }
    if resolved_model:
        sdk_options_kwargs["model"] = resolved_model
    if max_turns is not None:
        sdk_options_kwargs["max_turns"] = max_turns

    options = ClaudeAgentOptions(**sdk_options_kwargs)

    log.info(
        "agent.start",
        agent=agent_path.name,
        model=resolved_model,
        workdir=str(workdir),
        timeout_s=resolved_timeout,
        permission_mode=permission_mode,
        max_turns=max_turns,
    )
    log.debug("agent.env", env=_mask_env(forwarded_env))

    started = time.monotonic()
    events: list[dict[str, Any]] = []
    final_text = ""
    agent_metrics = AgentMetrics()
    timed_out = False
    error: str | None = None
    exit_code = -1

    # Initialise stderr file (SDK doesn't surface a stderr stream the way the
    # subprocess did; we still create the file for forensic-artifact contract).
    stderr_path.write_text("", encoding="utf-8")

    try:
        events, final_text, agent_metrics = await asyncio.wait_for(
            _drive_query(
                user_prompt=user_prompt,
                options=options,
                transcript_path=transcript_path,
                on_event=on_event,
            ),
            timeout=resolved_timeout,
        )
        exit_code = 0
    except asyncio.TimeoutError:
        timed_out = True
        exit_code = -9
        error = f"timeout after {resolved_timeout}s"
        log.error("agent.timeout", agent=agent_path.name, timeout_s=resolved_timeout)
    except Exception as e:
        error = f"sdk error: {e}"
        log.exception("agent.sdk_error", agent=agent_path.name, error=str(e))

    duration = time.monotonic() - started
    success = (exit_code == 0) and not timed_out and error is None

    # Push into the active step accumulator (if any). Token/cost counts a
    # partial run too -- the SDK can emit a ResultMessage even on early
    # termination, and the user has already been billed for the tokens.
    accumulator = CURRENT_STEP_METRICS.get()
    if accumulator is not None:
        accumulator.record(agent_metrics)

    metrics = {
        "agent": agent_path.name,
        "model": resolved_model,
        "success": success,
        "exit_code": exit_code,
        "duration_s": round(duration, 3),
        "timed_out": timed_out,
        "error": error,
        "event_count": len(events),
        "tokens_input": agent_metrics.input_tokens,
        "tokens_output": agent_metrics.output_tokens,
        "tokens_cache_creation": agent_metrics.cache_creation_input_tokens,
        "tokens_cache_read": agent_metrics.cache_read_input_tokens,
        "cost_usd": round(agent_metrics.cost_usd, 6),
        "num_turns": agent_metrics.num_turns,
    }
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if error and not stderr_path.read_text(encoding="utf-8"):
        stderr_path.write_text(error, encoding="utf-8")

    log.info(
        "agent.end",
        agent=agent_path.name,
        success=success,
        exit_code=exit_code,
        duration_s=metrics["duration_s"],
        timed_out=timed_out,
        tokens_input=agent_metrics.input_tokens,
        tokens_output=agent_metrics.output_tokens,
        cost_usd=round(agent_metrics.cost_usd, 6),
    )

    return AgentResult(
        success=success,
        exit_code=exit_code,
        duration_s=duration,
        transcript_path=transcript_path,
        stderr_path=stderr_path,
        metrics_path=metrics_path,
        final_text=final_text,
        timed_out=timed_out,
        error=error,
        events=events,
        metrics=agent_metrics,
    )
