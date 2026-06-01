"""Drive a single agent via the Claude Agent SDK in an isolated workdir.

This is the single execution path for every agent in worca-t. All step modules
funnel through `run_agent()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

import psutil
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    query,
)

from worca_t.config import CLAUDE_SESSION_KEYS, SECRET_ENV_KEYS, get_model_chain, get_settings, model_for_agent, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.mcp_manager import stage_mcp_config
from worca_t.metrics import CURRENT_STEP_METRICS, AgentMetrics, extract_agent_metrics
from worca_t.proxy import with_proxy_env

log = get_logger(__name__)


@dataclass
class _DriveState:
    """Live state of an in-flight SDK query.

    Mutated by ``_drive_query`` as each message streams in. Holding state in
    a shared container (rather than relying on the coroutine's return value)
    means that everything captured before a timeout/cancellation survives —
    token billing for partial runs no longer collapses to zero.
    """
    events: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    metrics: AgentMetrics = field(default_factory=AgentMetrics)
    session_id: str | None = None
    # PIDs that were children of our process at agent.start, so cleanup can
    # avoid killing siblings if `run_agent` is ever invoked concurrently.
    pre_existing_children: set[int] = field(default_factory=set)


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
    # SDK session_id from the init SystemMessage. Pass this back via the
    # `resume=` parameter on a follow-up run_agent call to continue the same
    # conversation and benefit from cache_read on the already-cached prefix.
    session_id: str | None = None


# MCP tool allowlist. The SDK's `permission_mode="acceptEdits"` does NOT
# auto-approve MCP tools (only file edits and filesystem Bash) — the CLI behaved
# differently. Pre-approve every MCP server we ship with so steps that touch
# Playwright / Atlassian don't stall on permission prompts.
_MCP_ALLOWLIST: tuple[str, ...] = (
    "mcp__playwright__*",
    "mcp__atlassian__*",
)

# Sanity threshold for an unusually large agent file (informational only).
_AGENT_PROMPT_WARN_BYTES = 30_000

_MODEL_UNAVAILABLE_INDICATORS = (
    "overloaded",
    "529",
    "model_not_available",
    "model not found",
    "capacity",
    "service_unavailable",
    "503",
    "exit code 15",
    "issue with the selected model",
    "may not exist or you may not have access",
)


def _is_model_unavailable(error: str) -> bool:
    """Return True when the error indicates the model itself is unreachable."""
    lower = error.lower()
    return any(ind in lower for ind in _MODEL_UNAVAILABLE_INDICATORS)


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
    state: _DriveState,
    user_prompt: str,
    options: ClaudeAgentOptions,
    transcript_path: Path,
    on_event: Any | None,
) -> None:
    """Iterate the SDK query, mutate *state* in place, write transcript.

    Mutating a shared state object rather than returning locals means partial
    progress (events, tokens, session_id) survives if this coroutine is
    cancelled mid-stream — e.g. by a timeout.
    """
    with transcript_path.open("w", encoding="utf-8") as transcript_fp:
        async for message in query(prompt=user_prompt, options=options):
            evt = _message_to_dict(message)
            transcript_fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
            transcript_fp.flush()
            state.events.append(evt)

            if state.session_id is None:
                sid = getattr(message, "session_id", None)
                if isinstance(sid, str) and sid:
                    state.session_id = sid
                else:
                    data = getattr(message, "data", None) or {}
                    if isinstance(data, dict):
                        sid = data.get("session_id")
                        if isinstance(sid, str) and sid:
                            state.session_id = sid

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
                    state.final_text = text
            elif isinstance(message, ResultMessage):
                result = getattr(message, "result", None)
                if isinstance(result, str) and result:
                    state.final_text = result
                m = extract_agent_metrics(
                    getattr(message, "usage", None),
                    getattr(message, "total_cost_usd", None),
                )
                turns = getattr(message, "num_turns", None)
                if isinstance(turns, int):
                    m.num_turns = turns
                state.metrics.input_tokens += m.input_tokens
                state.metrics.output_tokens += m.output_tokens
                state.metrics.cache_creation_input_tokens += m.cache_creation_input_tokens
                state.metrics.cache_read_input_tokens += m.cache_read_input_tokens
                state.metrics.cost_usd += m.cost_usd
                state.metrics.num_turns += m.num_turns
            elif isinstance(message, UserMessage):
                # User-turn echo from the SDK (e.g., tool-result feedback).
                pass

            if on_event:
                try:
                    on_event(evt)
                except Exception:
                    pass


def _current_child_pids() -> set[int]:
    """Snapshot of our process's direct + transitive children. Tolerant of races."""
    try:
        return {c.pid for c in psutil.Process(os.getpid()).children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


async def _force_cleanup(
    task: asyncio.Task,
    pre_existing_children: set[int],
    grace_s: float,
) -> None:
    """Cancel *task* and kill any SDK subprocess tree it spawned.

    The Claude Agent SDK's ``query()`` async generator wraps a ``claude`` CLI
    subprocess, which itself may spawn MCP server children (e.g. ``npx``).
    On ``CancelledError``, the generator's ``__aexit__`` awaits the
    subprocess to exit — which can block indefinitely if an MCP child is
    hung (e.g. mid-``npx``-install).

    The fix: cancel the task with a bounded wait, then forcibly terminate
    any process tree that appeared while the task was running. Only NEW
    children (i.e. PIDs not in ``pre_existing_children``) are killed, so
    concurrent sibling agents — if any are ever introduced — are spared.

    All cleanup failures are swallowed and logged. Cleanup must never raise
    into the caller's error path.
    """
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_s)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("agent.cleanup_task_error", error=str(e))

    try:
        me = psutil.Process(os.getpid())
        # Snapshot AFTER cancel so we catch any children spawned during the
        # cancellation grace window too.
        children = [
            c for c in me.children(recursive=True)
            if c.pid not in pre_existing_children
        ]
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        log.warning("agent.cleanup_enum_error", error=str(e))
        return

    if not children:
        return

    log.info("agent.cleanup_kill", count=len(children),
             pids=[c.pid for c in children])

    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            child.terminate()
    try:
        _gone, alive = psutil.wait_procs(children, timeout=3)
    except Exception as e:  # noqa: BLE001
        log.warning("agent.cleanup_wait_error", error=str(e))
        alive = children
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()


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
    resume: str | None = None,
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
    resume: optional SDK session_id from a prior run_agent call. When set,
        the SDK resumes that conversation instead of starting a new one,
        letting the cached system-prompt/conversation prefix hit cache_read
        instead of paying the 25% cache_creation premium again. Capture it
        from a prior call's ``AgentResult.session_id``.
    """
    del debug_live  # accepted for API compat
    workdir.mkdir(parents=True, exist_ok=True)
    # Number each call's audit files so multi-call steps (HITL retries,
    # step 9 self-heal) don't overwrite each other's transcript/metrics/stderr.
    # `transcript-00.jsonl` is call 0, `transcript-01.jsonl` is call 1, etc.
    call_idx = len(list(workdir.glob("transcript-*.jsonl")))
    suffix = f"-{call_idx:02d}"
    transcript_path = workdir / f"transcript{suffix}.jsonl"
    stderr_path = workdir / f"stderr{suffix}.log"
    metrics_path = workdir / f"metrics{suffix}.json"

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
    if max_turns is not None:
        sdk_options_kwargs["max_turns"] = max_turns
    if resume:
        sdk_options_kwargs["resume"] = resume

    # Build the model fallback chain for resilience against model outages.
    model_chain = get_model_chain(resolved_model) if resolved_model else [resolved_model]
    models_attempted: list[str | None] = []

    log.debug("agent.env", env=_mask_env(forwarded_env))

    started = time.monotonic()
    timed_out = False
    error: str | None = None
    exit_code = -1

    # Initialise stderr file (SDK doesn't surface a stderr stream the way the
    # subprocess did; we still create the file for forensic-artifact contract).
    stderr_path.write_text("", encoding="utf-8")

    # Declare state outside the loop so post-loop code always has a reference.
    state = _DriveState()

    for model_idx, current_model in enumerate(model_chain):
        opts = dict(sdk_options_kwargs)
        if current_model:
            opts["model"] = current_model
        options = ClaudeAgentOptions(**opts)
        models_attempted.append(current_model)

        log.info(
            "agent.start",
            agent=agent_path.name,
            model=current_model,
            workdir=str(workdir),
            timeout_s=resolved_timeout,
            permission_mode=permission_mode,
            max_turns=max_turns,
            resume=resume,
            call_idx=call_idx,
            fallback_attempt=model_idx,
        )

        timed_out = False
        error = None
        exit_code = -1
        state = _DriveState(pre_existing_children=_current_child_pids())

        drive_task = asyncio.create_task(_drive_query(
            state=state,
            user_prompt=user_prompt,
            options=options,
            transcript_path=transcript_path,
            on_event=on_event,
        ))
        try:
            async with asyncio.timeout(resolved_timeout):
                await drive_task
            exit_code = 0
        except TimeoutError:
            timed_out = True
            exit_code = -9
            error = f"timeout after {resolved_timeout}s"
            log.error("agent.timeout", agent=agent_path.name, timeout_s=resolved_timeout)
            await _force_cleanup(drive_task, state.pre_existing_children, grace_s=5.0)
        except Exception as e:
            error = f"sdk error: {e}"
            log.exception("agent.sdk_error", agent=agent_path.name, error=str(e))
            await _force_cleanup(drive_task, state.pre_existing_children, grace_s=2.0)

            error_context = f"{e} {state.final_text}"
            if _is_model_unavailable(error_context) and model_idx < len(model_chain) - 1:
                next_model = model_chain[model_idx + 1]
                log.warning(
                    "agent.model_fallback",
                    agent=agent_path.name,
                    from_model=current_model,
                    to_model=next_model,
                )
                continue

        break

    # Restore the variables the rest of run_agent expects. Whatever was
    # captured before cancellation is preserved through `state`.
    events = state.events
    final_text = state.final_text
    agent_metrics = state.metrics
    session_id = state.session_id
    used_model = models_attempted[-1] if models_attempted else resolved_model

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
        "model": used_model,
        "model_requested": resolved_model,
        "models_attempted": models_attempted,
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
        "session_id": session_id,
        "resume": resume,
        "call_idx": call_idx,
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
        tokens_cache_read=agent_metrics.cache_read_input_tokens,
        cost_usd=round(agent_metrics.cost_usd, 6),
        session_id=session_id,
        resumed=resume is not None,
        model_used=used_model,
        model_requested=resolved_model,
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
        session_id=session_id,
    )
