"""Direct Anthropic SDK transport for pure-reasoning steps.

Designed for steps that:
  * Don't need MCP servers (no Playwright, no Atlassian)
  * Don't need file tools (no Read/Edit/Grep — inputs are inlined into the prompt)
  * Produce either structured JSON output (validated by ``output_config.format``)
    or freeform markdown/text

Used by: Steps 1 (JIRA intake reformat), 2 (refine spec), 3 (plan),
4 (strategy), 10 (bug classifier). Steps 6-9 continue to use
:func:`worca_t.llm.browser_agent.run_agent`.

The return type and metrics integration match
:func:`worca_t.claude_runner.run_agent` so step files can switch
transports with minimal code change.

**Auth:** :class:`anthropic.AsyncAnthropic` auto-reads ``ANTHROPIC_BASE_URL``,
``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_API_KEY`` from env via
``get_settings()`` — model farm proxy works identically to the Agent SDK
path today.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from worca_t.config import (
    anthropic_auth_kwargs,
    get_model_chain,
    get_settings,
    model_for_agent,
    step_timeout,
)
from worca_t.llm.protocols import (
    CURRENT_STEP_METRICS,
    AgentMetrics,
    AgentResult,
    extract_agent_metrics,
)
from worca_t.logging_setup import get_logger

log = get_logger(__name__)


# Indicators that the model itself is unavailable (vs request-level error).
# Mirrors ``claude_runner._MODEL_UNAVAILABLE_INDICATORS`` so model-fallback
# semantics are identical across both transports.
_MODEL_UNAVAILABLE_INDICATORS = (
    "overloaded",
    "529",
    "model_not_available",
    "model not found",
    "capacity",
    "service_unavailable",
    "503",
    "issue with the selected model",
    "may not exist or you may not have access",
)


def _is_model_unavailable(error: str) -> bool:
    lower = error.lower()
    return any(ind in lower for ind in _MODEL_UNAVAILABLE_INDICATORS)


def _agent_key(agent_path: Path) -> str:
    """Derive the agent->model lookup key from filename (mirrors claude_runner)."""
    name = agent_path.name
    for suffix in (".agent.md", ".prompt.md", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _normalize_model_id(model: str) -> str:
    """Convert agent_models.yaml's ``'@<date>'`` suffix to SDK-expected form.

    ``agent_models.yaml`` uses Vertex-style ``claude-haiku-4-5@20251001`` for
    some entries; the standard Anthropic SDK expects
    ``claude-haiku-4-5-20251001`` (hyphen, not @).
    """
    return model.replace("@", "-") if "@" in model else model


def _inline_inputs(user_prompt: str, inputs: dict[str, str] | None) -> str:
    """Append staged inputs as fenced markdown sections to the user prompt.

    Replaces the workdir-file-staging pattern used by ``run_agent`` (where
    files were copied into the agent's cwd for it to read via the Read
    tool). With direct SDK + no file tools, callers pass the content
    directly and we embed it in the user message.
    """
    if not inputs:
        return user_prompt
    parts = [user_prompt, ""]
    for name, content in inputs.items():
        ext = Path(name).suffix.lstrip(".") or ""
        fence_lang = {"json": "json", "md": "markdown", "yaml": "yaml", "yml": "yaml"}.get(ext, ext)
        parts.append(f"--- {name} ---")
        parts.append(f"```{fence_lang}")
        parts.append(content)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def _write_audit(
    workdir: Path,
    *,
    transcript: list[dict[str, Any]],
    metrics: dict[str, Any],
    error: str | None,
) -> tuple[Path, Path, Path]:
    """Persist the same shape of audit files as ``run_agent``: numbered transcripts.

    ``transcript-NN.jsonl`` / ``stderr-NN.log`` / ``metrics-NN.json`` —
    matches the file naming scheme at :func:`claude_runner.run_agent` so
    multi-call steps (HITL retries, step-9 self-heal) don't overwrite
    each other's audit files, and existing log/report renderers see the
    same shape regardless of transport.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    call_idx = len(list(workdir.glob("transcript-*.jsonl")))
    suffix = f"-{call_idx:02d}"
    transcript_path = workdir / f"transcript{suffix}.jsonl"
    stderr_path = workdir / f"stderr{suffix}.log"
    metrics_path = workdir / f"metrics{suffix}.json"

    with transcript_path.open("w", encoding="utf-8") as fp:
        for evt in transcript:
            fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
    stderr_path.write_text(error or "", encoding="utf-8")
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    return transcript_path, stderr_path, metrics_path


async def call_reasoning_llm(
    agent_path: Path,
    *,
    workdir: Path,
    user_prompt: str,
    output_schema: dict | None = None,
    inputs: dict[str, str] | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
    timeout_s: int | None = None,
    step: int | None = None,
    hitl_history: list[dict] | None = None,
) -> AgentResult:
    """Direct-SDK transport for pure-reasoning steps.

    Replaces :func:`claude_runner.run_agent` for steps without MCP /
    file-tool needs.

    Parameters
    ----------
    agent_path:
        Path to the ``.agent.md`` file. Its content is loaded as the
        ``system`` prompt of the request.
    workdir:
        Where to write transcript / metrics / stderr audit files.
        Mirrors the workdir contract of ``run_agent``.
    user_prompt:
        The user-turn message body. ``inputs`` (if any) are appended as
        fenced markdown sections.
    output_schema:
        JSON schema dict to enforce via ``output_config.format``. ``None``
        = freeform text/markdown output (no schema enforcement).
    inputs:
        Optional ``{filename: content}`` dict to inline into the user
        prompt. Replaces the file-staging pattern from ``run_agent``.
    model:
        Explicit model id override. Otherwise resolved via
        :func:`worca_t.config.model_for_agent` against
        ``agent_models.yaml``.
    max_tokens:
        Response cap. Default 16000.
    timeout_s:
        Per-call timeout. Defaults to ``step_timeout(step)`` or 600s.
    step:
        Step number — used only for timeout resolution and metrics
        accumulation tagging. Does not affect request shape.
    hitl_history:
        Prior conversation turns for HITL re-invoke. Each entry is a
        ``{"role": ..., "content": ...}`` message dict.

    Returns
    -------
    AgentResult
        Same dataclass as ``run_agent`` returns. Subprocess-only fields
        (``mcp_servers_failed``, ``session_id``) are populated with
        empty / ``None`` defaults.
    """
    settings = get_settings()
    resolved_timeout = timeout_s if timeout_s is not None else step_timeout(step or 0)

    if not agent_path.exists():
        raise FileNotFoundError(f"agent file not found: {agent_path}")

    # Model resolution: explicit arg → agent_models.yaml → error.
    requested_model = model or model_for_agent(_agent_key(agent_path))
    if not requested_model:
        raise ValueError(
            f"No model resolved for agent {agent_path.name}. Pass "
            f"`model=` explicitly or add an entry in agent_models.yaml."
        )
    model_chain = get_model_chain(requested_model)

    system_prompt = agent_path.read_text(encoding="utf-8")
    full_prompt = _inline_inputs(user_prompt, inputs)

    # Messages list: optional HITL history + new user turn.
    messages: list[dict[str, Any]] = list(hitl_history or [])
    messages.append({"role": "user", "content": full_prompt})

    # Lazy import: don't pay anthropic SDK import cost for non-LLM steps.
    import anthropic  # type: ignore[import-untyped]

    create_kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if output_schema is not None:
        create_kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}
        }

    log.info(
        "reasoning.start",
        agent=agent_path.name,
        model=requested_model,
        max_tokens=max_tokens,
        timeout_s=resolved_timeout,
        has_schema=output_schema is not None,
        hitl_history_len=len(hitl_history or []),
    )

    started = time.monotonic()
    transcript: list[dict[str, Any]] = []
    final_text = ""
    accumulated = AgentMetrics()
    error: str | None = None
    success = False
    models_attempted: list[str] = []
    used_model: str | None = None

    # Dispatch between auth_token (Bearer — model farm / OAuth) and api_key
    # (x-api-key — raw Anthropic API) based on which env var is set. The
    # claude CLI does the same dispatch; without this, a model-farm token
    # set via ANTHROPIC_AUTH_TOKEN gets sent as x-api-key and the proxy
    # rejects with 401 "invalid x-api-key".
    async with anthropic.AsyncAnthropic(
        **anthropic_auth_kwargs(),
        base_url=settings.anthropic_base_url,
        timeout=float(resolved_timeout),
    ) as client:
        for model_idx, candidate_model in enumerate(model_chain):
            create_kwargs["model"] = _normalize_model_id(candidate_model)
            models_attempted.append(candidate_model)
            used_model = candidate_model

            try:
                response = await client.messages.create(**create_kwargs)
                transcript.append({
                    "type": "request",
                    "model": create_kwargs["model"],
                    "messages_count": len(messages),
                    "has_schema": output_schema is not None,
                })
                transcript.append({
                    "type": "response",
                    "stop_reason": getattr(response, "stop_reason", None),
                    "id": getattr(response, "id", None),
                    "model": getattr(response, "model", None),
                })

                # Extract final text from content blocks. Last text block wins
                # (mirrors ``_extract_text_from_blocks`` in claude_runner).
                for block in getattr(response, "content", []) or []:
                    if getattr(block, "type", None) == "text":
                        final_text = getattr(block, "text", "") or final_text

                # Token / cost extraction.
                agent_metrics = extract_agent_metrics(
                    getattr(response, "usage", None),
                    None,  # AsyncAnthropic doesn't expose total_cost_usd
                )
                agent_metrics.num_turns = 1
                accumulated.input_tokens += agent_metrics.input_tokens
                accumulated.output_tokens += agent_metrics.output_tokens
                accumulated.cache_creation_input_tokens += agent_metrics.cache_creation_input_tokens
                accumulated.cache_read_input_tokens += agent_metrics.cache_read_input_tokens
                accumulated.cost_usd += agent_metrics.cost_usd
                accumulated.num_turns += agent_metrics.num_turns

                success = True
                error = None
                break

            except Exception as e:  # noqa: BLE001
                error_text = f"{type(e).__name__}: {e}"
                log.warning(
                    "reasoning.attempt_failed",
                    agent=agent_path.name,
                    model=candidate_model,
                    error=error_text,
                )
                error = error_text
                if _is_model_unavailable(error_text) and model_idx < len(model_chain) - 1:
                    log.info(
                        "reasoning.model_fallback",
                        from_model=candidate_model,
                        to_model=model_chain[model_idx + 1],
                    )
                    continue
                break

    duration = time.monotonic() - started

    # Push into step-metrics accumulator (matches run_agent's contract).
    accumulator = CURRENT_STEP_METRICS.get()
    if accumulator is not None:
        accumulator.record(accumulated)

    metrics_dict = {
        "agent": agent_path.name,
        "model": used_model,
        "model_requested": requested_model,
        "models_attempted": models_attempted,
        "success": success,
        "exit_code": 0 if success else -1,
        "duration_s": round(duration, 3),
        "timed_out": False,
        "error": error,
        "tokens_input": accumulated.input_tokens,
        "tokens_output": accumulated.output_tokens,
        "tokens_cache_creation": accumulated.cache_creation_input_tokens,
        "tokens_cache_read": accumulated.cache_read_input_tokens,
        "cost_usd": round(accumulated.cost_usd, 6),
        "num_turns": accumulated.num_turns,
        "transport": "direct-sdk-reasoning",
    }
    transcript_path, stderr_path, metrics_path = _write_audit(
        workdir,
        transcript=transcript,
        metrics=metrics_dict,
        error=error,
    )

    log.info(
        "reasoning.end",
        agent=agent_path.name,
        success=success,
        duration_s=metrics_dict["duration_s"],
        tokens_input=accumulated.input_tokens,
        tokens_output=accumulated.output_tokens,
        model_used=used_model,
    )

    return AgentResult(
        success=success,
        exit_code=0 if success else -1,
        duration_s=duration,
        transcript_path=transcript_path,
        stderr_path=stderr_path,
        metrics_path=metrics_path,
        final_text=final_text,
        timed_out=False,
        error=error,
        events=transcript,
        metrics=accumulated,
        mcp_servers_failed=[],
        session_id=None,
    )


# Matches ``base.HITL_MAX_ITERATIONS`` — kept in sync deliberately so both
# transports cap HITL at the same iteration count.
_HITL_MAX_ITERATIONS = 3


async def call_reasoning_llm_with_hitl(
    agent_path: Path,
    *,
    ctx: Any,
    workdir: Path,
    user_prompt: str,
    inputs: dict[str, str] | None = None,
    output_filename: str,
    output_schema: dict | None = None,
    model: str | None = None,
    timeout_s: int | None = None,
    step: int | None = None,
    agent_label: str,
    max_iterations: int = _HITL_MAX_ITERATIONS,
) -> AgentResult:
    """HITL-aware wrapper around :func:`call_reasoning_llm`.

    Direct-SDK replacement for :func:`worca_t.steps.base.run_agent_with_hitl`.
    Where the old wrapper re-invoked the agent fresh on each iteration and
    staged ``user-answers.md`` as a file input, this version conducts a
    proper multi-turn conversation: iteration 1's user message + assistant
    response are appended to ``hitl_history`` and replayed on iteration 2,
    with the user's answers as a new user turn instructing the agent to
    rewrite the document.

    Behaviour matches :func:`base.run_agent_with_hitl` closely:
      * Skips the prompt loop when ``ctx.options.no_hitl`` is set
      * Tracks ``question_key()``-deduped skipped questions across rounds
        so the user is never re-prompted with the same item
      * Persists each round's user-answers to ``<workspace>/.hitl-stepNN/``
        for audit
      * Caps at ``max_iterations`` (default 3)

    The agent's response text is also written to ``workdir/output_filename``
    on every iteration so callers can inspect intermediate state and so
    the audit trail matches today's debug-artifact contract.
    """
    # Lazy import — ``worca_t.hitl`` is heavy (rich) and pulls in modules
    # we'd otherwise not need for non-HITL reasoning calls.
    from worca_t.hitl import (
        extract_questions,
        format_answers_md,
        prompt_user,
        question_key,
        write_answers_file,
    )

    hitl_disabled = bool(getattr(ctx.options, "no_hitl", False))
    hitl_dir = (
        workdir.parent / f".hitl-step{step:02d}" if step
        else workdir.parent / ".hitl"
    )

    # Pre-render the iteration-1 user message (prompt + inlined inputs) so
    # we can later replay it verbatim in ``hitl_history``. After this, we
    # pass it as a fully-rendered prompt with inputs=None so call_reasoning_llm
    # does not re-inline.
    first_user_content = _inline_inputs(user_prompt, inputs)
    current_prompt = first_user_content
    current_history: list[dict[str, Any]] | None = None
    skipped_keys: set[str] = set()
    result: AgentResult | None = None

    for iteration in range(1, max_iterations + 1):
        result = await call_reasoning_llm(
            agent_path=agent_path,
            workdir=workdir,
            user_prompt=current_prompt,
            inputs=None,  # already inlined into current_prompt for iter 1; absent for iter 2+
            output_schema=output_schema,
            model=model,
            timeout_s=timeout_s,
            step=step,
            hitl_history=current_history,
        )

        if not result.success or not result.final_text:
            return result

        # Persist this iteration's output for downstream inspection and so
        # step files that still want to read the file artifact can do so.
        produced = workdir / output_filename
        try:
            produced.write_text(result.final_text, encoding="utf-8")
        except OSError as e:
            log.warning("hitl.persist_failed", path=str(produced), error=str(e))

        if hitl_disabled:
            return result

        all_questions = extract_questions(result.final_text)
        new_questions = [
            q for q in all_questions if question_key(q) not in skipped_keys
        ]

        if not new_questions:
            if all_questions:
                log.info(
                    "hitl.only_previously_skipped",
                    agent=agent_label,
                    total=len(all_questions),
                    skipped=len(skipped_keys),
                )
            return result

        log.info(
            "hitl.questions_found",
            agent=agent_label,
            iteration=iteration,
            new=len(new_questions),
            already_skipped=len(all_questions) - len(new_questions),
        )

        if iteration >= max_iterations:
            log.warning(
                "hitl.max_iterations_reached",
                agent=agent_label,
                pending=len(new_questions),
            )
            return result

        answers = prompt_user(new_questions, agent_label=agent_label)
        skipped_this_round = [q for q in new_questions if q.id not in answers]
        for q in skipped_this_round:
            skipped_keys.add(question_key(q))

        # Persist user answers for audit (mirrors run_agent_with_hitl).
        hitl_dir.mkdir(parents=True, exist_ok=True)
        write_answers_file(
            hitl_dir, new_questions, answers, skipped=skipped_this_round
        )

        # Build the next iteration's conversation: append the rendered
        # user message we sent + the assistant's response, then a new
        # user turn with the answers.
        if current_history is None:
            current_history = []
        current_history.append({"role": "user", "content": current_prompt})
        current_history.append({"role": "assistant", "content": result.final_text})

        answers_md = format_answers_md(
            new_questions, answers, skipped=skipped_this_round
        )
        current_prompt = (
            f"The user has reviewed your clarification questions. Their "
            f"responses (and any items they chose to skip) are below.\n\n"
            f"{answers_md}\n\n"
            f"- For ANSWERED items: incorporate the answer and remove the "
            f"corresponding `[CLARIFICATION NEEDED]` tag, blocker row, or "
            f"open-question entry.\n"
            f"- For SKIPPED items: make a reasonable assumption, mark it "
            f"inline with `[ASSUMPTION: ...]`, and remove the original "
            f"`[CLARIFICATION NEEDED]` tag. **Do NOT re-emit "
            f"`[CLARIFICATION NEEDED]` for skipped items.**\n\n"
            f"Rewrite the document above accordingly. Keep the rest of "
            f"the document intact. Return only the updated document."
        )

    return result  # pragma: no cover (loop always returns inside)


__all__ = ["call_reasoning_llm", "call_reasoning_llm_with_hitl"]
