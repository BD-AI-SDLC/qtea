"""Direct Anthropic SDK transport for pure-reasoning steps.

Designed for steps that:
  * Don't need MCP servers (no Playwright, no Atlassian)
  * Don't need file tools (no Read/Edit/Grep — inputs are inlined into the prompt)
  * Produce either structured JSON output (validated by ``output_config.format``)
    or freeform markdown/text

Used by: Steps 1 (JIRA intake reformat), 2 (refine spec), 3 (plan),
4 (strategy), 10 (bug classifier). Steps 6-9 continue to use
:func:`qtea.llm.browser_agent.run_agent`.

The return type and metrics integration match
:func:`qtea.claude_runner.run_agent` so step files can switch
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

from qtea.config import (
    anthropic_auth_kwargs,
    anthropic_vertex_kwargs,
    get_model_chain,
    get_settings,
    model_for_agent,
    step_timeout,
    use_vertex_backend,
)
from qtea.llm.protocols import (
    CURRENT_STEP_METRICS,
    AgentMetrics,
    AgentResult,
    extract_agent_metrics,
)
from qtea.logging_setup import get_logger
from qtea.pricing import PRICING_BASIS, estimate_cost

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


# Process-level latch for the "structured outputs skipped on Vertex" notice.
# We surface it once per `qtea run` invocation so the user knows the
# Vertex fallback is in effect, then demote subsequent occurrences to debug
# so the same banner doesn't repeat 5-10× across a pipeline. Reset in
# tests via ``reset_vertex_structured_outputs_warning_latch()``.
_VERTEX_STRUCTURED_OUTPUTS_WARNED: bool = False


def reset_vertex_structured_outputs_warning_latch() -> None:
    """Reset the once-per-run warning latch — test-only hook."""
    global _VERTEX_STRUCTURED_OUTPUTS_WARNED
    _VERTEX_STRUCTURED_OUTPUTS_WARNED = False


def _agent_key(agent_path: Path) -> str:
    """Derive the agent->model lookup key from filename (mirrors claude_runner)."""
    name = agent_path.name
    for suffix in (".agent.md", ".prompt.md", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _normalize_model_id(model: str, *, for_vertex: bool) -> str:
    """Conditionally normalise the ``@<date>`` suffix in agent_models.yaml entries.

    Two model-id conventions are in play across Anthropic backends:

    * Vertex AI (Google Cloud) expects ``claude-haiku-4-5@20251001`` (@-form)
    * Standard Anthropic API expects ``claude-haiku-4-5-20251001`` (dash-form)

    ``agent_models.yaml`` uses the @-form as canonical. When the active
    backend is Vertex (or a Vertex-mimicking proxy like Bosch's model farm),
    we pass the id through unchanged. When it's the standard Anthropic API,
    we convert ``@`` to ``-``.
    """
    if for_vertex:
        return model
    return model.replace("@", "-") if "@" in model else model


def _strip_json_wrappers(text: str) -> str:
    """Strip markdown fences a model may wrap JSON in when structured outputs is off.

    On the standard Anthropic API we use ``output_config.format=json_schema``
    (structured outputs), so the response is the raw JSON object — no prose,
    no fences. On Vertex backends (Google Cloud / Bosch model farm) the
    ``structured_outputs`` feature is sometimes blocked by org policy (the
    ``constraints/vertexai.allowedPartnerModelFeatures`` constraint). In
    that fallback path we rely on prompt instructions ("respond with JSON
    only"), which Claude generally honors — but it occasionally still wraps
    the response in ```json ... ``` fences. This helper makes downstream
    ``json.loads`` tolerant of that without each step having to handle it.
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the opening fence (which may be ``` or ```json or ```JSON).
    first_nl = s.find("\n")
    if first_nl == -1:
        return s
    body = s[first_nl + 1:]
    # Drop the closing fence if present.
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3].rstrip()
    return body


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


def _redact_images_in_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with base64 image data blanked out.

    Keeps ``transcript-NN.jsonl`` small and free of opaque base64 blobs while
    preserving the message shape (role, block types, media_type) for audit.
    """
    redacted: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            redacted.append(msg)
            continue
        new_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                src = block.get("source") or {}
                data = src.get("data") or ""
                new_content.append({
                    "type": "image",
                    "source": {
                        "type": src.get("type"),
                        "media_type": src.get("media_type"),
                        "data": f"<redacted:image bytes={len(data)}>",
                    },
                })
            else:
                new_content.append(block)
        redacted.append({**msg, "content": new_content})
    return redacted


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
    max_tokens: int = 64000,
    timeout_s: int | None = None,
    step: int | None = None,
    hitl_history: list[dict] | None = None,
    images: list[dict] | None = None,
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
        :func:`qtea.config.model_for_agent` against
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
    images:
        Optional Anthropic image content blocks
        (``{"type": "image", "source": {...}}``) to attach to the new user
        turn. When present, the user message becomes a ``text + image[]``
        content-block list instead of a bare string. Base64 image data is
        redacted from the audit transcript.

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
    agent_config = model_for_agent(_agent_key(agent_path))
    requested_model = model or (agent_config.model if agent_config else None)
    resolved_thinking = agent_config.thinking if agent_config else None
    # `effort` is an Agent-SDK (run_agent) control only — it is forwarded to the
    # `claude` CLI as `--effort`. The direct-SDK path here has no equivalent
    # Messages-API param wired in, so an `effort` set on a direct-SDK agent is
    # silently dropped. Warn so the misconfiguration surfaces instead of
    # no-op'ing (both transports read the same agent_models.yaml). Use
    # `thinking` to control reasoning depth on this path.
    if agent_config and agent_config.effort:
        log.warning(
            "reasoning.effort_ignored",
            agent=agent_path.name,
            effort=agent_config.effort,
            hint=(
                "`effort` is honored only on the Agent SDK (run_agent) path; "
                "call_reasoning_llm ignores it — use `thinking` here instead."
            ),
        )
    if not requested_model:
        raise ValueError(
            f"No model resolved for agent {agent_path.name}. Pass "
            f"`model=` explicitly or add an entry in agent_models.yaml."
        )
    model_chain = get_model_chain(requested_model)

    system_prompt = agent_path.read_text(encoding="utf-8")
    full_prompt = _inline_inputs(user_prompt, inputs)

    # Messages list: optional HITL history + new user turn. When images are
    # attached, the user turn is a text + image[] content-block list; otherwise
    # a bare string (unchanged behavior).
    messages: list[dict[str, Any]] = list(hitl_history or [])
    if images:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": full_prompt}]
        user_content.extend(images)
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": full_prompt})

    # Lazy import: don't pay anthropic SDK import cost for non-LLM steps.
    import anthropic  # type: ignore[import-untyped]

    # Backend selection happens once here (was previously inside the
    # ``async with`` block) so we can also use it to decide whether to
    # send the structured-outputs `output_config`. Bosch's model farm
    # (and bare Vertex AI) enforce
    # ``constraints/vertexai.allowedPartnerModelFeatures`` which usually
    # does NOT include ``structured_outputs`` for partner Anthropic
    # models — sending it causes a 400 FAILED_PRECONDITION. On Vertex
    # we degrade to prompt-only JSON mode and rely on the local
    # ``is_valid()`` re-check (which every reasoning-step caller
    # already runs) for schema enforcement.
    is_vertex = use_vertex_backend()

    create_kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    schema_enforced_server_side = False
    if output_schema is not None:
        if is_vertex:
            global _VERTEX_STRUCTURED_OUTPUTS_WARNED
            if not _VERTEX_STRUCTURED_OUTPUTS_WARNED:
                log.warning(
                    "reasoning.structured_outputs_skipped_vertex",
                    agent=agent_path.name,
                    model=requested_model,
                    reason=(
                        "Vertex backend disallows the `structured_outputs` "
                        "feature for partner Anthropic models via "
                        "`constraints/vertexai.allowedPartnerModelFeatures`. "
                        "Falling back to prompt-only JSON mode; "
                        "schema is still enforced locally by the caller. "
                        "(This banner is suppressed for subsequent calls "
                        "in this run; debug-level events still emit.)"
                    ),
                )
                _VERTEX_STRUCTURED_OUTPUTS_WARNED = True
            else:
                log.debug(
                    "reasoning.structured_outputs_skipped_vertex",
                    agent=agent_path.name,
                    model=requested_model,
                )
        else:
            create_kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }
            schema_enforced_server_side = True

    if resolved_thinking:
        create_kwargs["thinking"] = resolved_thinking
        budget = resolved_thinking.get("budget_tokens")
        if budget and isinstance(budget, int):
            create_kwargs["max_tokens"] = max(max_tokens, budget + 1024)

    log.info(
        "reasoning.start",
        agent=agent_path.name,
        model=requested_model,
        max_tokens=create_kwargs["max_tokens"],
        timeout_s=resolved_timeout,
        has_schema=output_schema is not None,
        schema_enforced_server_side=schema_enforced_server_side,
        backend="vertex" if is_vertex else "anthropic",
        hitl_history_len=len(hitl_history or []),
        thinking=resolved_thinking,
    )

    started = time.monotonic()
    transcript: list[dict[str, Any]] = []
    final_text = ""
    accumulated = AgentMetrics()
    error: str | None = None
    success = False
    models_attempted: list[str] = []
    used_model: str | None = None
    stop_reason: str | None = None

    # Backend selection: Vertex AI (or Vertex-mimicking proxy like Bosch's
    # model farm) when CLAUDE_CODE_USE_VERTEX=1 or ANTHROPIC_VERTEX_BASE_URL
    # is set; standard Anthropic API otherwise. The two client classes
    # construct URLs / accept auth differently — must match the backend.
    # ``is_vertex`` was already computed above to gate ``output_config``.
    if is_vertex:
        client_ctx = anthropic.AsyncAnthropicVertex(
            **anthropic_vertex_kwargs(),
            timeout=float(resolved_timeout),
        )
    else:
        # Dispatch between auth_token (Bearer) and api_key (x-api-key) per
        # which env var is set — mirrors the claude CLI.
        client_ctx = anthropic.AsyncAnthropic(
            **anthropic_auth_kwargs(),
            base_url=settings.anthropic_base_url,
            timeout=float(resolved_timeout),
        )

    async with client_ctx as client:
        for model_idx, candidate_model in enumerate(model_chain):
            create_kwargs["model"] = _normalize_model_id(
                candidate_model, for_vertex=is_vertex
            )
            models_attempted.append(candidate_model)
            used_model = candidate_model

            try:
                # Non-streaming single-shot request (Vertex `:rawPredict`).
                #
                # We deliberately do NOT use client.messages.stream()
                # (`:streamRawPredict`) here. Streaming was tried to keep the
                # connection warm against a corporate proxy idle timeout (BCNC
                # `px` default `idle=300`) that severed long non-streaming calls
                # as APIConnectionError (the 5.4M-token/max_tokens=32000 opus
                # incident). But on the Bosch model-farm Vertex gateway,
                # `:streamRawPredict` does NOT stream through incrementally: the
                # gateway buffers, then returns an HTTP 500 status line at
                # ~285-290s (proven by run 20260701-114656 — the 500 arrives as
                # the response STATUS, i.e. no bytes ever flowed). So streaming
                # gained nothing AND turned a survivable-under-300s call into a
                # hard 500. Non-streaming `:rawPredict` works fine for the same
                # durations (169-235s observed, incl. sonnet-5). The residual
                # px-idle risk only bites pathologically long (>300s-to-first-
                # byte) generations — the correct mitigation for those is
                # shrinking the prompt / max_tokens (keep the call under ~300s),
                # NOT re-introducing the streaming endpoint that 500s on BMF.
                response = await client.messages.create(**create_kwargs)
                transcript.append({
                    "type": "request",
                    "model": create_kwargs["model"],
                    "messages_count": len(messages),
                    "has_schema": output_schema is not None,
                    "messages": _redact_images_in_messages(messages),
                })
                stop_reason = getattr(response, "stop_reason", None)

                # Extract final text from content blocks. Last text block wins
                # (mirrors ``_extract_text_from_blocks`` in claude_runner).
                for block in getattr(response, "content", []) or []:
                    if getattr(block, "type", None) == "text":
                        final_text = getattr(block, "text", "") or final_text

                transcript.append({
                    "type": "response",
                    "stop_reason": stop_reason,
                    "id": getattr(response, "id", None),
                    "model": getattr(response, "model", None),
                    "text": final_text,
                })

                # When a JSON schema was requested but server-side enforcement
                # was unavailable (Vertex policy), the model may have wrapped
                # the JSON in ```json fences despite our instructions. Strip
                # them here so downstream ``json.loads`` works uniformly
                # regardless of backend.
                if output_schema is not None and not schema_enforced_server_side:
                    final_text = _strip_json_wrappers(final_text)

                # Token / cost extraction.
                # The direct Anthropic SDK doesn't return a cost field in
                # the response (unlike the Agent SDK's ResultMessage which
                # carries total_cost_usd computed from a built-in pricing
                # table). We compute the equivalent estimate here from
                # token counts × public list prices in qtea.pricing.
                # See pricing.py for accuracy caveats — the estimate is
                # informational, not authoritative billing.
                agent_metrics = extract_agent_metrics(
                    getattr(response, "usage", None),
                    None,
                )
                agent_metrics.cost_usd = estimate_cost(
                    candidate_model,
                    input_tokens=agent_metrics.input_tokens,
                    output_tokens=agent_metrics.output_tokens,
                    cache_creation_input_tokens=agent_metrics.cache_creation_input_tokens,
                    cache_read_input_tokens=agent_metrics.cache_read_input_tokens,
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

            except Exception as e:
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
        "cost_estimation_basis": PRICING_BASIS,
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
        stop_reason=stop_reason,
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
    images: list[dict] | None = None,
) -> AgentResult:
    """HITL-aware wrapper around :func:`call_reasoning_llm`.

    Direct-SDK replacement for :func:`qtea.steps.base.run_agent_with_hitl`.
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
    # Lazy import — ``qtea.hitl`` is heavy (rich) and pulls in modules
    # we'd otherwise not need for non-HITL reasoning calls.
    from qtea.hitl import (
        RESOLUTION_SKIPPED_DROP,
        HitlDecision,
        append_ledger,
        extract_questions,
        format_answers_md,
        load_ledger,
        prompt_user,
        question_key,
        render_prior_decisions_md,
        resolve_against_ledger,
        write_answers_file,
    )

    hitl_disabled = bool(getattr(ctx.options, "no_hitl", False))
    hitl_dir = (
        workdir.parent / f".hitl-step{step:02d}" if step
        else workdir.parent / ".hitl"
    )

    # Cross-step ledger: in-memory list on ctx.extras, mirrored to disk so
    # `--from-step` resumes don't lose it. On first access this run, hydrate
    # from the on-disk file (covers the resume case).
    workspace_root = ctx.workspace.root
    if "hitl_ledger" not in ctx.extras:
        ctx.extras["hitl_ledger"] = load_ledger(workspace_root)
    ledger: list[HitlDecision] = ctx.extras["hitl_ledger"]

    # If we have prior decisions, weave them into the agent's first input
    # so it knows not to re-raise them in the first place. Belt + suspenders
    # with the post-extract filter below.
    augmented_inputs = dict(inputs or {})
    augmented_prompt = user_prompt
    if ledger:
        augmented_inputs.setdefault(
            "prior-decisions.md", render_prior_decisions_md(ledger)
        )
        augmented_prompt = (
            f"{user_prompt}\n\n"
            f"**Note:** The file `prior-decisions.md` (below) lists items the "
            f"user already addressed in earlier steps of this run. Treat each "
            f"entry as final — do NOT re-emit them as new blockers, "
            f"clarifications, or open questions in your output. Each entry "
            f"carries its own directive on how to honor it: answered items "
            f"apply verbatim; dropped items REMOVE the corresponding "
            f"coverage (record in `## Coverage Notes`, no `[ASSUMPTION]`); "
            f"scope-exclusions remove the named scope; legacy-skipped items "
            f"(pre-rework runs) still use `[ASSUMPTION]` framing."
        )

    # Pre-render the iteration-1 user message (prompt + inlined inputs) so
    # we can later replay it verbatim in ``hitl_history``. After this, we
    # pass it as a fully-rendered prompt with inputs=None so call_reasoning_llm
    # does not re-inline.
    first_user_content = _inline_inputs(augmented_prompt, augmented_inputs)
    current_prompt = first_user_content
    current_history: list[dict[str, Any]] | None = None
    skipped_keys: set[str] = set()
    # Decisions accumulated this step — appended to the run ledger once we
    # return successfully. Keyed by question_key to deduplicate across the
    # iteration loop.
    step_decisions: dict[str, HitlDecision] = {}
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
            # Attach images to iteration 1's user turn only; on later
            # iterations they are already carried in current_history.
            images=images if iteration == 1 else None,
        )

        if not result.success or not result.final_text:
            _flush_step_decisions_to_ledger(
                ledger, step_decisions, workspace_root, append_ledger
            )
            return result

        # Persist this iteration's output for downstream inspection and so
        # step files that still want to read the file artifact can do so.
        produced = workdir / output_filename
        try:
            produced.write_text(result.final_text, encoding="utf-8")
        except OSError as e:
            log.warning("hitl.persist_failed", path=str(produced), error=str(e))

        if hitl_disabled:
            _flush_step_decisions_to_ledger(
                ledger, step_decisions, workspace_root, append_ledger
            )
            return result

        all_questions = extract_questions(result.final_text)

        # Cross-step ledger filter: any question paraphrasing an already
        # decided item is silently resolved with the prior answer/skip.
        novel_questions, ledger_resolved = resolve_against_ledger(
            all_questions, ledger
        )
        if ledger_resolved:
            log.info(
                "hitl.ledger_suppressed",
                agent=agent_label,
                iteration=iteration,
                count=len(ledger_resolved),
                ids=[q.id for q, _ in ledger_resolved],
            )

        # Within-iteration skip dedup (existing behavior).
        new_questions = [
            q for q in novel_questions if question_key(q) not in skipped_keys
        ]

        # Nothing left to ask AND no ledger-paraphrases to coach the agent
        # away from — we're done.
        if not new_questions and not ledger_resolved:
            if all_questions:
                log.info(
                    "hitl.only_previously_skipped",
                    agent=agent_label,
                    total=len(all_questions),
                    skipped=len(skipped_keys),
                )
            _flush_step_decisions_to_ledger(
                ledger, step_decisions, workspace_root, append_ledger
            )
            return result

        log.info(
            "hitl.questions_found",
            agent=agent_label,
            iteration=iteration,
            new=len(new_questions),
            already_skipped=len(all_questions) - len(new_questions) - len(ledger_resolved),
            ledger_resolved=len(ledger_resolved),
        )

        if iteration >= max_iterations:
            log.warning(
                "hitl.max_iterations_reached",
                agent=agent_label,
                pending=len(new_questions),
                ledger_resolved=len(ledger_resolved),
            )
            _flush_step_decisions_to_ledger(
                ledger, step_decisions, workspace_root, append_ledger
            )
            return result

        # Only prompt the user about questions we couldn't resolve from the
        # ledger. ledger_resolved go straight into the answers_md so the
        # agent picks up the prior answer/skip on the next turn.
        answers = prompt_user(new_questions, agent_label=agent_label) if new_questions else {}
        # prompt_user now returns dict[str, tuple[str, str]] — skipped items
        # are absent from the dict (matching the historical convention),
        # answered items carry (RESOLUTION_ANSWERED, text), scope-exclusion
        # items carry (RESOLUTION_SCOPE_EXCLUSION, text).
        skipped_this_round = [q for q in new_questions if q.id not in answers]
        for q in skipped_this_round:
            skipped_keys.add(question_key(q))
            step_decisions.setdefault(
                question_key(q),
                HitlDecision.from_question(
                    q,
                    step=step or 0,
                    agent_label=agent_label,
                    resolution=RESOLUTION_SKIPPED_DROP,
                ),
            )
        for q in new_questions:
            entry = answers.get(q.id)
            if entry is None:
                continue
            resolution, text = entry
            step_decisions[question_key(q)] = HitlDecision.from_question(
                q,
                step=step or 0,
                agent_label=agent_label,
                resolution=resolution,
                answer=text,
            )

        # Persist user answers for audit (mirrors run_agent_with_hitl).
        hitl_dir.mkdir(parents=True, exist_ok=True)
        write_answers_file(
            hitl_dir,
            new_questions,
            answers,
            skipped=skipped_this_round,
            ledger_resolved=ledger_resolved,
        )

        # Build the next iteration's conversation: append the rendered
        # user message we sent + the assistant's response, then a new
        # user turn with the answers.
        if current_history is None:
            current_history = []
        # Preserve iteration-1's images in the replayed history so later
        # iterations keep seeing them (call_reasoning_llm only attaches them on
        # iteration 1's live turn).
        if iteration == 1 and images:
            current_history.append({
                "role": "user",
                "content": [{"type": "text", "text": current_prompt}, *images],
            })
        else:
            current_history.append({"role": "user", "content": current_prompt})
        current_history.append({"role": "assistant", "content": result.final_text})

        answers_md = format_answers_md(
            new_questions,
            answers,
            skipped=skipped_this_round,
            ledger_resolved=ledger_resolved,
        )
        current_prompt = (
            f"The user has reviewed your clarification questions. Their "
            f"responses (and any items they chose to skip or exclude) are "
            f"below — along with any items that were already resolved "
            f"earlier in this run.\n\n"
            f"{answers_md}\n\n"
            f"- For ANSWERED items: incorporate the answer and remove the "
            f"corresponding `[CLARIFICATION NEEDED]` tag, blocker row, or "
            f"open-question entry.\n"
            f"- For SKIPPED items: REMOVE the entire AC / TC / sub-item "
            f"the question was attached to from the document body. Append "
            f"an entry to a `## Coverage Notes` section at the end of the "
            f"document recording the dropped ID and the reason. **Do NOT "
            f"write `[ASSUMPTION: ...]`** — the user's intent is to drop "
            f"this coverage, not to test it under an invented value. Do "
            f"NOT re-emit `[CLARIFICATION NEEDED]` for skipped items.\n"
            f"- For SCOPE-EXCLUDED items: interpret the user's answer as a "
            f"scope-exclusion (e.g. \"mobile isn't in scope\" → exclude "
            f"mobile). Remove ACs / TCs / sub-bullets that depend solely on "
            f"the excluded scope; keep the in-scope portions. Append an "
            f"entry to `## Coverage Notes` recording the exclusion and the "
            f"user's exact answer. Do NOT include the typed answer as a "
            f"literal value anywhere in the document body.\n"
            f"- For PREVIOUSLY RESOLVED items: follow the per-item "
            f"directive in `## Previously Resolved` above (answered → "
            f"apply verbatim; skipped-drop → drop; scope-exclusion → "
            f"exclude). Do NOT re-raise these to the user.\n\n"
            f"**Preserve `## Coverage Notes` across iterations.** If the "
            f"document already has a `## Coverage Notes` section from a "
            f"prior iteration, preserve its entries verbatim and only "
            f"append new ones for this iteration's drops / exclusions.\n\n"
            f"Rewrite the document above accordingly. Keep the rest of "
            f"the document intact. Return only the updated document."
        )

    _flush_step_decisions_to_ledger(
        ledger, step_decisions, workspace_root, append_ledger
    )
    return result  # pragma: no cover (loop always returns inside)


def _flush_step_decisions_to_ledger(
    ledger: list,
    step_decisions: dict,
    workspace_root: Path,
    append_ledger_fn,
) -> None:
    """Append this step's accumulated decisions to both the in-memory and
    on-disk ledger. Idempotent across early returns inside the loop."""
    if not step_decisions:
        return
    new_entries = list(step_decisions.values())
    ledger.extend(new_entries)
    append_ledger_fn(workspace_root, new_entries)
    step_decisions.clear()


__all__ = ["call_reasoning_llm", "call_reasoning_llm_with_hitl"]
