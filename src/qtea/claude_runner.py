"""Drive a single agent via the Claude Agent SDK in an isolated workdir.

This is the single execution path for every agent in qtea. All step modules
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

from qtea.config import (
    CLAUDE_SESSION_KEYS,
    SECRET_ENV_KEYS,
    get_model_chain,
    get_settings,
    model_for_agent,
    step_timeout,
)
from qtea.logging_setup import get_logger
from qtea.mcp_manager import stage_empty_mcp_config, stage_mcp_config
from qtea.metrics import CURRENT_STEP_METRICS, AgentMetrics, extract_agent_metrics
from qtea.proxy import with_proxy_env

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
    # Names of MCP servers whose status was reported as `failed` / `needs-auth`
    # in the SDK init message. Steps that depend on a specific MCP (e.g. Step 8
    # on `playwright`) check this to fail fast rather than burning agent budget
    # on an agent that can't see its primary tools.
    mcp_servers_failed: list[str] = field(default_factory=list)
    # Names of MCP servers whose status was `pending` at init time. The agent's
    # tool list is frozen at init, so a `pending` MCP means the agent CANNOT
    # see that MCP's tools for this run, even if the server connects shortly
    # after. Steps that strictly need a specific MCP can check this list and
    # fail-fast rather than burning agent budget on a tool-less invocation;
    # steps that don't (e.g. step 8 codegen, which doesn't browse) can ignore.
    mcp_servers_pending: list[str] = field(default_factory=list)
    # Consecutive SDK `api_retry` SystemMessages with no intervening
    # AssistantMessage / ResultMessage / UserMessage. When this hits the
    # active circuit-breaker threshold (`_api_retry_storm_threshold()`,
    # default 5, env-overridable via QTEA_API_RETRY_THRESHOLD),
    # `_drive_query` raises `_ApiRetryStorm`.
    api_retry_count: int = 0
    # Last stop_reason observed from a ResultMessage / AssistantMessage. The
    # SDK exposes this directly when the underlying Anthropic API populates
    # it (`"max_tokens"`, `"end_turn"`, `"tool_use"`, `"stop_sequence"`).
    # Used by Step 8 smart-retry to detect truncation deterministically
    # rather than inferring from a downstream syntax error.
    stop_reason: str | None = None


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
    # MCP server names that failed to start (status `failed` / `needs-auth`
    # in the SDK init message). Callers that depend on a specific MCP should
    # check this to distinguish "agent produced empty output" from "agent had
    # no tools to do its job".
    mcp_servers_failed: list[str] = field(default_factory=list)
    # MCP server names whose status was still `pending` when the agent's
    # tool list was frozen at init. The agent CANNOT see these MCPs' tools
    # for the duration of this run. Callers that strictly need a specific
    # MCP (e.g. step 9 heal flow needing `playwright`) should fail-fast.
    mcp_servers_pending: list[str] = field(default_factory=list)
    # SDK session_id from the init SystemMessage. Pass this back via the
    # `resume=` parameter on a follow-up run_agent call to continue the same
    # conversation and benefit from cache_read on the already-cached prefix.
    session_id: str | None = None
    # Stop reason from the LLM response. `"max_tokens"` is the strong signal
    # for truncation (the model wanted to keep generating but hit the budget
    # cap). Other common values: `"end_turn"`, `"tool_use"`, `"stop_sequence"`.
    # Optional with default `None` because not every transport / SDK version
    # populates it; callers checking for truncation should test both this AND
    # any post-hoc parse-failure signal (e.g. ast.parse).
    stop_reason: str | None = None


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

# Abort the agent after this many consecutive SDK `api_retry` events with
# no intervening AssistantMessage / ResultMessage / UserMessage. Insurance
# against the silent-retry-storm failure mode: run 20260603-205851-2d359f
# burned ~95 s of SDK-internal exponential backoff before colliding with
# the 1800 s step timeout — diagnostics from that run pinned the cumulative
# backoff at each retry as: 1→0.5 s, 2→1.7 s, 3→4.0 s, 4→8.6 s, 5→17.9 s,
# 6→35.9 s, 7→67.9 s, 8→100.8 s (Anthropic SDK exp-backoff w/ jitter,
# default `max_retries=10`).
#
# Beware: the backoff above is only the *sleep between retries*. Each
# actual API call on a hanging upstream can itself burn ~3 minutes before
# the SDK gives up and bumps the retry counter (observed in run
# 20260611-075728-0aa560: 5 retries → ~15 min wall-clock waste even
# though cumulative backoff was only ~18 s). With per-retry hang on the
# order of minutes, the threshold dominates wall-clock waste — too low
# and a 30 s Vertex blip kills a 14-turn step that was 90% done.
#
# Threshold sizing:
#   - too low (e.g. 3 → 4 s backoff, ~9 min wall) aborts on routine
#     Anthropic/Vertex transient 529s that normally recover within 2-4
#     retries; observed as false-positive aborts on long-running steps
#     where the agent had made hundreds of real tool calls (run
#     20260603 step-07 attempt 1: 434 events of real progress, then API
#     stuttered; run 20260611 step-08 attempt 1: full plan-reading and
#     POM-loading done, then API hung).
#   - too high (e.g. 10 → 5 min backoff, ~30 min wall) re-creates the
#     original silent-burn pattern this guard exists to prevent and can
#     collide with the 1800 s step timeout.
#   - 8 → ~100 s cumulative backoff, ~24 min wall worst-case. Gives the
#     SDK enough headroom to ride out a ~5-10 min Vertex incident
#     (observed pattern) without crossing the step timeout, while still
#     bailing out before a true sustained outage burns the full timeout.
#
# Overridable via env: QTEA_API_RETRY_THRESHOLD. Useful when:
#   - ops sees a sustained-flake window in upstream and wants to ride longer;
#   - a developer is debugging an agent loop and wants to abort sooner.
# Values are clamped to [1, 10] (above 10 the SDK has already given up).
_API_RETRY_STORM_THRESHOLD_DEFAULT = 8


def _api_retry_storm_threshold() -> int:
    """Resolve the active circuit-breaker threshold (env override + clamp).

    Returns an int in [1, 10]. Invalid env values (non-numeric, <1, >10)
    fall back to the default with a warning log so misconfiguration is
    visible at agent start rather than silently masked.
    """
    raw = os.environ.get("QTEA_API_RETRY_THRESHOLD")
    if raw is None or raw == "":
        return _API_RETRY_STORM_THRESHOLD_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            "agent.api_retry_threshold.invalid",
            value=raw,
            default=_API_RETRY_STORM_THRESHOLD_DEFAULT,
        )
        return _API_RETRY_STORM_THRESHOLD_DEFAULT
    if n < 1 or n > 10:
        log.warning(
            "agent.api_retry_threshold.out_of_range",
            value=n,
            allowed="[1, 10]",
            default=_API_RETRY_STORM_THRESHOLD_DEFAULT,
        )
        return _API_RETRY_STORM_THRESHOLD_DEFAULT
    return n


class _ApiRetryStorm(Exception):
    """Raised inside `_drive_query` when SDK api_retry events spam without progress."""

    def __init__(self, count: int, threshold: int) -> None:
        super().__init__(
            f"SDK api_retry storm ({count} consecutive retries with no "
            f"intervening progress; threshold={threshold}). The upstream "
            f"Anthropic/Vertex API is returning transient errors that did "
            f"not recover within the configured retry budget. "
            f"This is usually a temporary upstream incident — re-run the "
            f"step (`qtea run --from-step <N> --run-id <id>`). If it "
            f"persists across re-runs, the agent may be stuck in a "
            f"tool-error feedback loop; inspect the transcript's last "
            f"~10 tool calls before the retries began. To tolerate longer "
            f"transient windows, set QTEA_API_RETRY_THRESHOLD=<1..10> "
            f"(default 8)."
        )
        self.count = count
        self.threshold = threshold


class _ApiFatalError(Exception):
    """Raised when an api_retry event carries a non-retryable HTTP status.

    4xx errors (auth failures, quota exhaustion, bad requests) are never
    retryable by HTTP semantics. 5xx errors indicate a sustained API outage
    that won't recover within the step timeout budget. In both cases,
    burning the SDK's internal retry loop (up to 10 attempts with
    exponential backoff, ~24 min wall-clock) wastes time that could be
    spent surfacing a clear error and letting the user act.
    """

    def __init__(self, status: int, error: str, data: dict[str, Any]) -> None:
        super().__init__(
            f"API fatal error: HTTP {status} — {error or 'unknown'}. "
            f"Non-retryable; aborting immediately."
        )
        self.status = status
        self.error = error
        self.data = data

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
    """Copy each input artifact into the agent workdir under its target name.

    When `src` already equals `dst` (caller pre-wrote the file into the
    workdir and then also registered it in `inputs`), this is a no-op rather
    than an error. Without this guard, `shutil.copy2(x, x)` raises on
    Windows (`[WinError 32]: file in use`) and `SameFileError` on POSIX —
    both surface as unhandled exceptions that fail the step on attempt 1
    with no useful diagnosis.
    """
    for target_name, src in inputs.items():
        if not src.exists():
            raise FileNotFoundError(f"Input artifact missing: {src} (label: {target_name})")
        dst = workdir / target_name
        try:
            same = src.resolve() == dst.resolve()
        except OSError:
            same = False
        if same:
            log.debug(
                "stage_inputs.skip_same_file",
                target=target_name, path=str(src),
            )
            continue
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
    enable_mcp: bool,
    mcp_env: dict[str, str] | None = None,
) -> Path:
    """Copy agent file + skills/docs + CLAUDE.md + .mcp.json into workdir.

    When ``enable_mcp`` is False (the `run_agent` default), the staged
    `.mcp.json` is an explicitly empty `{"mcpServers": {}}` so the SDK
    spawns no MCP server children for this call. When True, the project's
    real `.mcp.json` is staged (with env substitution) and the SDK spawns
    every server it lists.

    ``mcp_env`` is an optional per-call env overlay forwarded to
    :func:`qtea.mcp_manager.stage_mcp_config`. Step 9 uses this to
    inject ``QTEA_STORAGE_STATE_ARG`` (the resolved storage-state CLI
    flag for Playwright MCP) without mutating process env. Ignored when
    ``enable_mcp`` is False.

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

    if enable_mcp:
        stage_mcp_config(workdir, source=mcp_source, env=mcp_env)
    else:
        # Empty config means the SDK reads no MCP servers and spawns no
        # subprocesses. Saves the ~3-10 s npx boot cost and silences the
        # cosmetic `agent.mcp.pending_at_init` warning on every step that
        # doesn't actually call an `mcp__*` tool — which is all of them
        # today except step 9's heal flow (Playwright).
        stage_empty_mcp_config(workdir)
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
        if text is None and isinstance(block, dict) and block.get("type") == "text":
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
    storm_threshold = _api_retry_storm_threshold()
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

            if isinstance(message, SystemMessage):
                subtype = getattr(message, "subtype", None)
                if subtype == "init":
                    data = getattr(message, "data", {}) or {}
                    servers = data.get("mcp_servers") or []
                    # SDK status enum: connected | failed | needs-auth | pending | disabled.
                    # `failed` / `needs-auth` are hard errors; `disabled` is
                    # intentional. `pending` is the tricky one — the tool list
                    # is frozen at init, so a server still pending at this
                    # moment is functionally unavailable to the agent even if
                    # it connects 200 ms later. We record both bad and pending
                    # so step-side code can choose to fail-fast.
                    bad = [s for s in servers
                           if isinstance(s, dict) and s.get("status") in ("failed", "needs-auth")]
                    if bad:
                        log.warning("agent.mcp.failed_servers", failed=bad)
                        state.mcp_servers_failed = [
                            s.get("name", "") for s in bad if s.get("name")
                        ]
                    pending = [s for s in servers
                               if isinstance(s, dict) and s.get("status") == "pending"]
                    if pending:
                        # Warning (not info) so it's visible in the structured
                        # log without trawling the transcript. Steps that
                        # strictly need these MCPs should fail-fast on
                        # `AgentResult.mcp_servers_pending`.
                        log.warning("agent.mcp.pending_at_init", pending=pending)
                        state.mcp_servers_pending = [
                            s.get("name", "") for s in pending if s.get("name")
                        ]
                elif subtype == "api_retry":
                    state.api_retry_count += 1
                    # Always log so a transient flake is visible in real
                    # time, not just buried in the post-mortem transcript.
                    data = getattr(message, "data", {}) or {}
                    # Dump the FULL data dict (not just cherry-picked fields)
                    # so opaque "error: unknown / error_status: None" cases
                    # surface whatever the SDK actually populated — exception
                    # class, response body, request id, attempt metadata.
                    # The cherry-picked fields stay for any structured-log
                    # consumer that already reads them.
                    log.warning(
                        "agent.api_retry",
                        count=state.api_retry_count,
                        threshold=storm_threshold,
                        attempt=data.get("attempt"),
                        retry_delay_ms=data.get("retry_delay_ms"),
                        error_status=data.get("error_status"),
                        error=data.get("error"),
                        data_keys=sorted(data.keys()) if isinstance(data, dict) else None,
                        data=data,
                    )
                    error_status = data.get("error_status")
                    if isinstance(error_status, int) and error_status >= 400:
                        log.error(
                            "agent.api_fatal",
                            http_status=error_status,
                            error=data.get("error"),
                        )
                        raise _ApiFatalError(
                            error_status,
                            str(data.get("error", "")),
                            data,
                        )
                    if state.api_retry_count >= storm_threshold:
                        log.error(
                            "agent.api_retry_storm",
                            count=state.api_retry_count,
                            threshold=storm_threshold,
                        )
                        raise _ApiRetryStorm(state.api_retry_count, storm_threshold)
            elif isinstance(message, AssistantMessage):
                state.api_retry_count = 0
                text = _extract_text_from_blocks(getattr(message, "content", None))
                if text:
                    state.final_text = text
            elif isinstance(message, ResultMessage):
                state.api_retry_count = 0
                result = getattr(message, "result", None)
                if isinstance(result, str) and result:
                    state.final_text = result
                # Capture stop_reason (best-effort — older SDKs may not
                # surface it). Last write wins, which is what we want:
                # the final ResultMessage carries the most relevant value.
                sr = getattr(message, "stop_reason", None)
                if isinstance(sr, str) and sr:
                    state.stop_reason = sr
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
                state.api_retry_count = 0
                # User-turn echo from the SDK (e.g., tool-result feedback).
                pass

            if on_event:
                with contextlib.suppress(Exception):
                    on_event(evt)


def _current_child_pids() -> set[int]:
    """Snapshot of our process's direct + transitive children. Tolerant of races."""
    try:
        return {c.pid for c in psutil.Process(os.getpid()).children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


def _kill_children(pre_existing_children: set[int]) -> None:
    """Terminate then kill child processes spawned after *pre_existing_children*.

    Runs on a thread (via ``run_in_executor``) so it never blocks the
    asyncio event loop — even when ``psutil.wait_procs`` stalls on
    Windows waiting for a Playwright/browser process tree to exit.
    """
    try:
        me = psutil.Process(os.getpid())
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
    except Exception as e:
        log.warning("agent.cleanup_wait_error", error=str(e))
        alive = children
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()


async def _force_cleanup(
    task: asyncio.Task,
    pre_existing_children: set[int],
    grace_s: float,
) -> None:
    """Kill the SDK subprocess tree, then cancel *task*.

    The Claude Agent SDK's ``query()`` async generator wraps a ``claude`` CLI
    subprocess, which itself may spawn MCP server children (e.g. ``npx``,
    Playwright browser).  On ``CancelledError``, the generator's
    ``__aexit__`` awaits the subprocess to exit — which can block the
    event loop indefinitely if the subprocess is hung on an MCP child.

    The fix: kill the process tree FIRST (on a thread so the event loop
    stays responsive), then cancel the task.  With its subprocess already
    dead, the SDK cleanup completes promptly.  Only NEW children (PIDs
    not in ``pre_existing_children``) are killed, so concurrent sibling
    agents are spared.

    All cleanup failures are swallowed and logged. Cleanup must never raise
    into the caller's error path.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _kill_children, pre_existing_children)

    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_s)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            log.warning("agent.cleanup_task_error", error=str(e))


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
    add_dirs: list[Path] | None = None,
    mcp_source: Path | None = None,
    claude_md: Path | None = None,
    on_event: Any | None = None,
    debug_live: bool = False,
    resume: str | None = None,
    enable_mcp: bool = False,
    mcp_env: dict[str, str] | None = None,
) -> AgentResult:
    """Run a single agent via the Claude Agent SDK in an isolated workdir.

    The ``workdir`` passed here is a staging-only directory. ``inputs``
    and ``extra_paths`` are copied IN at the start of each call; the
    agent's outputs land here and the caller is expected to move/copy
    the relevant ones into ``artifacts/stepNN/`` for publication. No
    file in the workdir should be assumed to persist meaningfully across
    ``run_agent`` invocations -- callers must re-stage everything they
    need on every call. See ``qtea.steps.base.Step`` docstring for
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
    add_dirs: extra directories the agent may read/write outside its cwd.
        Mapped to ``ClaudeAgentOptions.add_dirs``. Use this to grant the agent
        access to ``<workspace>/sut/`` (or any other dir) without copying its
        contents into the workdir — the SDK widens the filesystem sandbox in
        place. Paths must be absolute. None / empty list leaves the agent
        confined to ``cwd``.
    mcp_source: override .mcp.json source path.
    claude_md: path to CLAUDE.md to stage at the workdir root.
    on_event: optional callback invoked for each parsed event.
    debug_live: accepted for API compatibility; currently no-op.
    resume: optional SDK session_id from a prior run_agent call. When set,
        the SDK resumes that conversation instead of starting a new one,
        letting the cached system-prompt/conversation prefix hit cache_read
        instead of paying the 25% cache_creation premium again. Capture it
        from a prior call's ``AgentResult.session_id``.
    enable_mcp: opt-in flag for MCP server staging. Default False: stage an
        empty `{"mcpServers": {}}` so the SDK spawns no MCP server children,
        saving ~3-10 s of npx boot per call and silencing the cosmetic
        `agent.mcp.pending_at_init` warning. Pass True only when the agent
        actually invokes `mcp__*` tools (audit-verified: today only step
        9's polyglot-test-fixer heal flow needs Playwright MCP). Inverting
        the historic default reflects the audit: 5+ caller sites never
        used the MCPs they were paying to spawn.
    """
    del debug_live  # accepted for API compat
    workdir.mkdir(parents=True, exist_ok=True)
    # Per-call audit files live under <workdir>/logs/ so the workdir root
    # stays human-scannable — agents, inputs, and outputs at the top level;
    # transcripts / stderr / metrics tucked away. Each call's files are
    # numbered (transcript-00.jsonl is call 0, -01 is call 1, ...) so
    # multi-call steps (HITL retries, the API-storm wait/retry, step 8
    # self-heal) don't overwrite each other.
    logs_dir = workdir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    call_idx = len(list(logs_dir.glob("transcript-*.jsonl")))
    suffix = f"-{call_idx:02d}"
    transcript_path = logs_dir / f"transcript{suffix}.jsonl"
    stderr_path = logs_dir / f"stderr{suffix}.log"
    metrics_path = logs_dir / f"metrics{suffix}.json"
    # Persist the literal user_prompt the SDK receives, so post-mortem
    # debugging doesn't require re-deriving runtime-substituted values
    # (stack_hint, env_hint, reuse_hint, JIT runtime hint, ...) from the
    # source f-string. The transcript only logs what the SDK streams
    # BACK to us; the input we sent is otherwise opaque. Numbered to
    # match the transcript for the same call.
    prompt_path = logs_dir / f"user-prompt{suffix}.md"
    try:
        prompt_path.write_text(user_prompt, encoding="utf-8")
    except OSError as e:
        # Best-effort: a failure to dump the prompt should never block
        # the actual agent call. Log + continue.
        log.warning("agent.user_prompt_dump_failed",
                    path=str(prompt_path), error=str(e))

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
        enable_mcp=enable_mcp,
        mcp_env=mcp_env,
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
    # Claude Code's prompt-cache disable knobs (DISABLE_PROMPT_CACHING and
    # per-model DISABLE_PROMPT_CACHING_{OPUS,SONNET,HAIKU}) don't match the
    # prefix filter above but must reach the subprocess to take effect.
    # `pipeline.run_pipeline` sets DISABLE_PROMPT_CACHING=1 by default
    # (cleared by --cache); forward whichever variants are set so the CLI
    # honours them.
    for k in (
        "DISABLE_PROMPT_CACHING",
        "DISABLE_PROMPT_CACHING_OPUS",
        "DISABLE_PROMPT_CACHING_SONNET",
        "DISABLE_PROMPT_CACHING_HAIKU",
    ):
        if k in full_env:
            forwarded_env[k] = full_env[k]
    # Vertex routing / auth signals. These don't match the prefix filter
    # above (CLAUDE_CODE_*, CLOUD_ML_*) but the claude.exe CLI reads them
    # directly to decide transport + auth path. They're set at the Windows
    # user-registry level on Bosch workstations; without explicit forwarding
    # we'd inherit them by accident via env merging today, but a future SDK
    # change could break that. Forward explicitly so the subprocess sees a
    # complete, predictable configuration. (`ANTHROPIC_VERTEX_PROJECT_ID`
    # already matches the ANTHROPIC_ prefix above — listed here for
    # documentation completeness; the assignment is idempotent.)
    #
    # NOT forwarded on purpose: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
    # `CLAUDE_CODE_EXECPATH` — those are nesting-detection signals the SDK
    # uses to refuse to start when it thinks it's inside another Claude Code
    # session (see CLAUDE_SESSION_KEYS in qtea.config). Forwarding them
    # would re-trigger the nesting check we already strip above.
    for k in (
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLOUD_ML_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
    ):
        if k in full_env:
            forwarded_env[k] = full_env[k]

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
    if add_dirs:
        # Widen the SDK's filesystem sandbox beyond `cwd` to these absolute
        # paths. Used by steps 7-9 to grant read/write access to
        # `<workspace>/sut/` (where generated tests are written in place) and
        # by step 6 to grant read-only access without a copytree duplicate.
        sdk_options_kwargs["add_dirs"] = [str(Path(p).resolve()) for p in add_dirs]

    # Build the model fallback chain for resilience against model outages.
    model_chain = get_model_chain(resolved_model) if resolved_model else [resolved_model]
    models_attempted: list[str | None] = []

    log.debug("agent.env", env=_mask_env(forwarded_env))

    started = time.monotonic()
    timed_out = False
    error: str | None = None
    exit_code = -1

    # Capture stderr from the `claude` CLI subprocess into stderr_path. The
    # CLI always runs with `--verbose` (see
    # claude_agent_sdk/_internal/transport/subprocess_cli.py:225), so it
    # emits the underlying error behind each `api_retry` event, the raw
    # HTTP status / body when an API call fails, MCP-server boot diagnostics,
    # and Node-side stack traces. Without a registered callback the transport
    # discards stderr (line 472 of the same file: `stderr_dest = PIPE if
    # self._options.stderr is not None else None`) — leaving us blind to
    # root causes when retries / failures happen. Wiring this changes the
    # post-mortem signature for runs like 20260603-205851-2d359f from
    # `error: "unknown"` to the actual exception text.
    #
    # `buffering=1` is line-buffered: every line is flushed to disk
    # immediately, so a SIGKILL / OOM still leaves a complete log. The
    # file is closed by Python's GC when `run_agent` returns; the SDK
    # transport may invoke the callback after `query()` exits during
    # async cleanup, and the try/except absorbs the resulting ValueError
    # on a closed file rather than crashing the SDK's stderr-reader task.
    stderr_fp = stderr_path.open("w", encoding="utf-8", buffering=1)

    def _stderr_sink(line: str) -> None:
        """Forward one stderr line from the claude CLI subprocess to disk."""
        with contextlib.suppress(OSError, ValueError):
            stderr_fp.write(line if line.endswith("\n") else line + "\n")

    sdk_options_kwargs["stderr"] = _stderr_sink

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
        except _ApiRetryStorm as e:
            # SDK retried `_API_RETRY_STORM_THRESHOLD` consecutive times with
            # no intervening progress. Bail out fast rather than waiting for
            # the full step timeout to fire.
            exit_code = -10
            error = str(e)
            await _force_cleanup(drive_task, state.pre_existing_children, grace_s=2.0)
        except _ApiFatalError as e:
            exit_code = -11
            error = str(e)
            log.error(
                "agent.api_fatal",
                agent=agent_path.name,
                http_status=e.status,
                error_detail=e.error,
            )
            await _force_cleanup(drive_task, state.pre_existing_children, grace_s=2.0)
        except Exception as e:
            # The SDK's exception text is often opaque (e.g.
            # "Claude Code returned an error result: success"). The real
            # cause — Anthropic API 4xx, quota exhaustion, OAuth refresh
            # failure, ... — is in `state.final_text` (the `ResultMessage.result`
            # body). Surface both so users don't have to grep transcripts.
            api_detail = (state.final_text or "").strip()
            error = f"sdk error: {e} | api: {api_detail[:500]}" if api_detail else f"sdk error: {e}"
            log.exception(
                "agent.sdk_error",
                agent=agent_path.name,
                error=str(e),
                api_detail=api_detail[:500] if api_detail else None,
            )
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
    mcp_servers_failed = list(state.mcp_servers_failed)
    mcp_servers_pending = list(state.mcp_servers_pending)
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
    if error:
        # Close the CLI-stderr sink first so the append below isn't racing
        # with the still-open write handle (matters on Windows). Late
        # writes from the SDK's stderr-reader task during async cleanup
        # are absorbed by the sink's try/except.
        with contextlib.suppress(OSError, ValueError):
            stderr_fp.close()
        # Append the runner's diagnostic banner as a footer so it doesn't
        # overwrite the CLI's own --verbose stderr (which contains the
        # underlying exception text behind `api_retry` events). When the
        # CLI was silent the banner is all we have; when it wrote
        # something the banner sits below it as added context, not as a
        # replacement. Prior behavior dropped the banner whenever the CLI
        # had written anything, and dropped the CLI text whenever it
        # hadn't — see RCA in run 20260610-082950-6a887f.
        cli_stderr = stderr_path.read_text(encoding="utf-8")
        separator = "\n--- qtea runner ---\n" if cli_stderr else ""
        stderr_path.write_text(cli_stderr + separator + error, encoding="utf-8")

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
        mcp_servers_failed=mcp_servers_failed,
        mcp_servers_pending=mcp_servers_pending,
        stop_reason=state.stop_reason,
    )
