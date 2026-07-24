"""Step base class: shared contract for every pipeline step."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from rich.console import Console
from rich.prompt import Prompt

from qtea.checkpoints import AuxiliaryAgentRecord, RunState, StepRecord, hash_paths
from qtea.claude_runner import AgentResult, run_agent
from qtea.config import (
    DEBUG_AGENT_MAX_TURNS,
    DEBUG_AGENT_TIMEOUT_S,
    FIX_AGENT_MAX_TURNS,
    FIX_AGENT_TIMEOUT_S,
    package_resource_root,
)
from qtea.hitl import (
    RESOLUTION_SKIPPED_DROP,
    HitlDecision,
    append_ledger,
    extract_questions,
    load_ledger,
    prompt_user,
    question_key,
    render_prior_decisions_md,
    resolve_against_ledger,
    write_answers_file,
)
from qtea.incident_memory import (
    incident_memory_enabled,
    query_similar,
    record_incident,
    render_prior_incidents_md,
    sut_fingerprint,
)
from qtea.logging_setup import get_logger
from qtea.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator
from qtea.workspace import Workspace

log = get_logger(__name__)

_T = TypeVar("_T")

MAX_ATTEMPTS = max(1, min(5, int(os.environ.get("QTEA_MAX_ATTEMPTS", "2"))))
HITL_MAX_ITERATIONS = 3

# Sentinel string that the claude_runner.`_ApiRetryStorm` exception text
# always starts with. Matching by prefix lets `Step.execute` distinguish a
# transient upstream incident from a deterministic agent failure, even
# though `StepResult` only carries the error text (not the exit_code).
# Tightly coupled to the message format in `claude_runner._ApiRetryStorm`
# — keep these in sync.
_API_RETRY_STORM_PREFIX = "SDK api_retry storm"
_API_FATAL_ERROR_PREFIX = "API fatal error: HTTP"

# Wall-clock wait when the user picks "wait then retry" on a storm prompt.
# 5 min is the empirically-observed median duration of a Vertex transient
# incident window (run 20260611-075728-0aa560 was the longer end of the
# tail at ~30 min, but most clear within 2-5 min). Short enough that the
# user doesn't lose context, long enough that attempt 2 isn't landing
# inside the same incident as attempt 1.
_STORM_RETRY_WAIT_S = 300


def _is_api_retry_storm(error: str | None) -> bool:
    """True when `error` looks like the api_retry_storm sentinel from claude_runner."""
    return bool(error) and error.lstrip().startswith(_API_RETRY_STORM_PREFIX)


def _is_api_fatal_error(error: str | None) -> bool:
    """True when `error` is a non-retryable HTTP 4xx/5xx from claude_runner."""
    return bool(error) and error.lstrip().startswith(_API_FATAL_ERROR_PREFIX)


async def _prompt_storm_retry_decision(
    *, step_num: int, step_name: str, attempt: int, error: str, console: Console
) -> str:
    """Block until the user picks how to handle a transient-upstream storm.

    Returns one of: ``"retry"``, ``"wait"``, ``"abort"``. Caller decides
    what to do — this function only collects the answer. Caller is also
    responsible for the TTY / --no-hitl / --yes gate; this function
    unconditionally prompts.

    Async because the "wait" branch in the caller sleeps inside the
    event loop and we want a consistent await-able shape.
    """
    console.print()
    console.print(
        f"[yellow]step {step_num:02d} {step_name} attempt {attempt} "
        f"hit an upstream API storm.[/]"
    )
    console.print(f"[dim]  {error.splitlines()[0]}[/]")
    console.print(
        "[dim]  The harness can retry now, wait "
        f"{_STORM_RETRY_WAIT_S // 60} min for the upstream to recover, "
        "or abort.[/]"
    )
    choice = Prompt.ask(
        "[bold]How do you want to proceed?[/] "
        r"[dim](\[r\]etry now / \[w\]ait & retry / \[a\]bort)[/]",
        choices=["r", "w", "a"],
        default="w",
        show_choices=False,
    )
    return {"r": "retry", "w": "wait", "a": "abort"}[choice]


@dataclass
class StepContext:
    """Per-run context passed to every Step.run()."""

    workspace: Workspace
    state: RunState
    spec_source: str
    sut_source: str
    options: Any  # PipelineOptions (avoid circular import in typing)
    # Optional operator-supplied free-text context about the spec (trusted
    # guidance). None/empty when not provided; consumed by Steps 1 and 2.
    operator_context: str | None = None
    # Optional operator-supplied context images (absolute paths under
    # <workspace>/operator-context/images/). Trusted supplementary context
    # consumed by Step 2 refinement. Empty when none provided.
    operator_context_images: list[Path] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    success: bool
    status: str  # "completed" | "skipped" | "failed" | "warned"
    outputs: list[Path]
    notes: str | None = None
    error: str | None = None
    sub_status: str | None = None  # "all_passed" | "bugs_found" | None


def _snapshot_debug_artifacts(step_num: int, ctx: StepContext, attempt: int) -> None:
    dst = ctx.workspace.debug / f"step-{step_num:02d}-attempt{attempt}"
    dst.mkdir(parents=True, exist_ok=True)

    workdir = ctx.workspace.step_workdir(step_num)
    if not workdir.exists():
        return
    # Per-call audit files live under <workdir>/logs/ now; older runs (and
    # any test scaffold that drops files directly into the workdir root)
    # may still have them at the legacy top-level path, so glob both.
    search_dirs = [workdir / "logs", workdir]
    for pattern in ("transcript*.jsonl", "stderr*.log", "metrics*.json"):
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for src in search_dir.glob(pattern):
                shutil.copy2(src, dst / src.name)


def _build_failure_context(
    step_num: int, step_name: str, record: StepRecord, result: StepResult
) -> str:
    """Render a small Markdown blob describing a failed attempt.

    Shared by the debug-RCA flow and the fix-proposal flow so both
    agents see the same framing.
    """
    return (
        f"# Step {step_num} ({step_name}) failure\n\n"
        f"**Attempts:** {record.attempts}\n"
        f"**Status:** {record.status}\n"
        f"**Error:** {result.error or 'unknown'}\n"
        f"**Notes:** {result.notes or 'none'}\n"
    )


def _agent_failure_placeholder(
    *,
    agent_label: str,
    result: AgentResult,
    failure_context: str,
) -> str:
    """Diagnostic placeholder for a debug/fix agent that never wrote its file.

    The previous fallback (``out_path.write_text(result.final_text, ...)``)
    treated the SDK's last ``AssistantMessage`` block as the agent's final
    answer, which promoted pre-tool-call thinking prose (\"Let me check
    X...\") to disk as if it were the RCA / strategy / proposal — and the
    downstream fix chain then ran on 150 bytes of half-a-sentence
    (regression: run 20260701-114656-9394eb, both debug.agent and
    principal-software-engineer hit ``max_turns`` mid-investigation).

    Instead, when the SDK returned ``success=False`` we make the failure
    loud: header names the agent, error line surfaces the SDK reason
    (turn cap / timeout / api storm), the truncated ``final_text`` is
    embedded as a *thinking snippet* (blockquoted so downstream agents
    don't mistake it for a heading), and the raw failure_context is
    inlined so consumers still have concrete material to reason about
    (the fix chain won't fall through to ``failure_context`` on its own
    because the placeholder is non-empty).
    """
    lines = [
        f"# {agent_label} — agent failed to produce artifact",
        "",
        f"**Error:** {result.error or 'unknown'}",
    ]
    transcript = getattr(result, "transcript_path", None)
    if transcript is not None:
        lines.append(f"**Transcript:** `{transcript}`")
    lines.append("")
    if result.final_text:
        snippet = result.final_text.strip()
        # Keep the placeholder scannable — the transcript link above lets
        # the operator dig into the full final message if needed.
        if len(snippet) > 2000:
            snippet = snippet[:2000] + " ...[truncated]"
        quoted = "> " + snippet.replace("\n", "\n> ")
        lines.extend([
            "## Last agent message (pre-truncation)",
            "",
            "> _The SDK cut the agent off before it wrote its output file._",
            "> _The text below is the last ``AssistantMessage`` block —",
            "> _typically pre-tool-call thinking, not the agent's actual answer._",
            "",
            quoted,
            "",
        ])
    lines.extend([
        "## Raw failure context",
        "",
        failure_context.rstrip(),
        "",
    ])
    return "\n".join(lines)


# Finalizer: bounded second sub-invocation used to salvage an agent output
# when the primary invocation exhausted its ``max_turns`` budget without
# writing the expected file. Turn cap is deliberately tight — this pass is
# synthesis over the prior transcript, NOT fresh investigation. A synthesis
# call that thinks-and-writes rarely needs more than 3-4 turns; 8 is
# generous headroom. Timeout matches: 3 min covers a slow model + one long
# tool result. Overridable so operators can dial it up on very large
# transcripts. Kept separate from DEBUG/FIX agent budgets because those
# govern the *primary* investigation pass; this governs the salvage.
FINALIZER_MAX_TURNS: int = int(os.environ.get("QTEA_FINALIZER_MAX_TURNS", "8"))
FINALIZER_TIMEOUT_S: int = int(os.environ.get("QTEA_FINALIZER_TIMEOUT_S", "180"))

# Cap on the prior-investigation.md payload handed to the finalizer.
# ~200 kB keeps the synthesis prompt tractable while retaining the most
# recent thinking + tool activity. Older turns are dropped tail-first
# (the agent's LAST N turns are what it was closest to concluding on).
_PRIOR_INVESTIGATION_MAX_BYTES: int = 200_000


def _extract_agent_prior_investigation(
    workdir: Path, *, max_bytes: int = _PRIOR_INVESTIGATION_MAX_BYTES
) -> str | None:
    """Distill the highest-numbered transcript in ``workdir/logs/`` into a
    compact markdown record of what the truncated agent thought, tried, and
    saw before it ran out of turns.

    The primary consumer is :func:`_finalize_truncated_agent` — the finalizer
    reads this file as its evidence base so it can synthesize the agent's
    output without repeating the investigation. Structure prioritizes
    *recency* (the agent was closest to concluding on the final turns)
    and drops older material tail-first when the size cap bites.

    Returns None when no transcript exists or the file is empty/unparseable
    — the caller then falls through to the labelled placeholder path.
    """
    logs_dir = workdir / "logs"
    if not logs_dir.exists():
        return None
    transcripts = sorted(logs_dir.glob("transcript-*.jsonl"))
    if not transcripts:
        return None
    transcript = transcripts[-1]

    events: list[dict[str, Any]] = []
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    if not events:
        return None

    # Walk in reverse so we keep the newest turns when the budget bites.
    entries: list[str] = []
    for evt in reversed(events):
        etype = evt.get("type")
        if etype == "AssistantMessage":
            msg = evt.get("message", {}) or {}
            content = msg.get("content", [])
            blocks: list[str] = []
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ctype = c.get("type")
                    if ctype == "text":
                        text = str(c.get("text", "")).strip()
                        if text:
                            blocks.append(text)
                    elif ctype == "thinking":
                        text = str(c.get("thinking", "")).strip()
                        if text:
                            blocks.append(f"_[thinking]_ {text}")
                    elif ctype == "tool_use":
                        name = c.get("name", "?")
                        raw_input = c.get("input", {})
                        try:
                            input_repr = json.dumps(raw_input)[:500]
                        except (TypeError, ValueError):
                            input_repr = str(raw_input)[:500]
                        blocks.append(f"**→ tool_use `{name}`**: `{input_repr}`")
            elif isinstance(content, str):
                text = content.strip()
                if text:
                    blocks.append(text)
            if blocks:
                entries.append("### Assistant\n\n" + "\n\n".join(blocks))
        elif etype == "UserMessage":
            msg = evt.get("message", {}) or {}
            content = msg.get("content", [])
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        raw = c.get("content", "")
                        if isinstance(raw, list):
                            parts = []
                            for sub in raw:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    parts.append(str(sub.get("text", "")))
                            text = "\n".join(parts).strip()
                        else:
                            text = str(raw).strip()
                        if text:
                            snippet = text if len(text) < 1500 else (text[:1500] + " …[truncated]")
                            entries.append(f"### Tool result\n\n```\n{snippet}\n```")
        elif etype == "ResultMessage":
            result = evt.get("subtype") or evt.get("result", "")
            entries.append(f"### Terminal event\n\n`{result}`")

    if not entries:
        return None

    # entries are newest-first; reverse to chronological for readability.
    entries.reverse()
    header = (
        f"# Prior investigation transcript summary\n\n"
        f"Source: `{transcript}`  \n"
        f"Total events: {len(events)}  \n"
        f"Entries preserved: {len(entries)}\n\n"
        f"_The primary agent invocation hit its `max_turns` budget before "
        f"writing its expected output file. The turns below are the record "
        f"of what it thought, tried, and saw. Use them as your evidence "
        f"base — do NOT restart the investigation._\n\n"
        f"---\n\n"
    )
    body = "\n\n".join(entries)
    combined = header + body
    if len(combined.encode("utf-8")) <= max_bytes:
        return combined
    # Trim body tail-first (keep newest turns) until under cap.
    header_bytes = len(header.encode("utf-8"))
    while entries and len(("\n\n".join(entries)).encode("utf-8")) + header_bytes > max_bytes:
        entries.pop(0)
    if not entries:
        return header + "_(All entries dropped — transcript too large to include.)_\n"
    body = "\n\n".join(entries)
    return header + (
        f"_Note: {len(events)} total events; oldest entries dropped to fit "
        f"the {max_bytes // 1000} kB synthesis budget._\n\n---\n\n{body}"
    )


async def _finalize_truncated_agent(
    *,
    agent_path: Path,
    parent_workdir: Path,
    expected_filename: str,
    output_path: Path,
    agent_label: str,
    failure_context: str,
    add_dirs: list[Path] | None,
    extra_inputs: dict[str, Path] | None = None,
) -> Path | None:
    """Salvage the artifact from a max_turns-exhausted agent invocation.

    Fires a bounded second call to the SAME agent with the prior run's
    transcript summary as its sole evidence base and a single mandate: read
    the summary, synthesize the expected output, do NOT re-investigate.

    This decouples INVESTIGATION budget from WRITE-UP budget — the primary
    call blew its budget exploring the failure; the finalizer needs only
    2-4 turns to consolidate what was found into the expected file.
    Guarantees an artifact from tokens already spent on the primary pass
    rather than collapsing to a labelled placeholder.

    Returns the output path when the finalizer produces the expected file
    (or a usable ``final_text``); returns None when the finalizer itself
    fails or the prior transcript is unrecoverable — the caller then
    falls back to :func:`_agent_failure_placeholder`.
    """
    if not agent_path.exists():
        return None

    prior_investigation = _extract_agent_prior_investigation(parent_workdir)
    if not prior_investigation:
        log.warning(
            "finalizer.no_prior_transcript",
            agent=agent_label,
            workdir=str(parent_workdir),
        )
        return None

    finalizer_workdir = parent_workdir / "finalize"
    finalizer_workdir.mkdir(parents=True, exist_ok=True)

    prior_file = finalizer_workdir / "prior-investigation.md"
    prior_file.write_text(prior_investigation, encoding="utf-8")

    context_file = finalizer_workdir / "failure-context.md"
    context_file.write_text(failure_context, encoding="utf-8")

    inputs: dict[str, Path] = {
        "prior-investigation.md": prior_file,
        "failure-context.md": context_file,
    }
    if extra_inputs:
        for name, path in extra_inputs.items():
            if path.exists():
                inputs[name] = path

    synthesis_prompt = (
        "SALVAGE MODE — your previous invocation on this same failure hit "
        f"its `max_turns` budget before writing `./{expected_filename}`. "
        "Read `./prior-investigation.md` (your own chronological transcript "
        "of that run — thinking, tool calls, tool results) and "
        "`./failure-context.md` (the framing). Synthesize what you found "
        f"into `./{expected_filename}` using the report structure from your "
        "agent contract.\n\n"
        "Hard rules for this pass:\n"
        f"  • You have ONLY {FINALIZER_MAX_TURNS} turns — do not start a new "
        "investigation. One or two targeted follow-up reads are acceptable if "
        "the transcript points to a specific unread file; otherwise write "
        "directly.\n"
        f"  • If evidence for a required section is thin, say so explicitly "
        "(\"Evidence insufficient — see prior-investigation.md above\") "
        "rather than fabricating. A partial-but-honest report with named "
        "unknowns is more useful than a plausible-sounding fabrication.\n"
        f"  • Your output MUST land at `./{expected_filename}`. Do not "
        "inline it in your final message."
    )

    try:
        result = await run_agent(
            agent_path,
            workdir=finalizer_workdir,
            inputs=inputs,
            user_prompt=synthesis_prompt,
            add_dirs=add_dirs,
            timeout_s=FINALIZER_TIMEOUT_S,
            max_turns=FINALIZER_MAX_TURNS,
        )
    except Exception as e:
        log.warning(
            "finalizer.invocation_failed",
            agent=agent_label,
            error=str(e),
        )
        return None

    produced = finalizer_workdir / expected_filename
    if produced.exists() and produced.stat().st_size > 0:
        shutil.copy2(produced, output_path)
        log.info(
            "finalizer.artifact_written",
            agent=agent_label,
            path=str(output_path),
            source="expected_file",
        )
        return output_path

    if result.success and result.final_text:
        output_path.write_text(result.final_text, encoding="utf-8")
        log.info(
            "finalizer.artifact_written",
            agent=agent_label,
            path=str(output_path),
            source="final_text",
        )
        return output_path

    log.warning(
        "finalizer.no_output",
        agent=agent_label,
        error=result.error,
        hit_max_turns=result.hit_max_turns,
    )
    return None


def _should_run_debug_rca(ctx: StepContext, has_more_attempts: bool) -> bool:
    """Gate the debug-RCA invocation.

    - Always fires on a FINAL failure (last attempt or no retry available):
      gives the user a diagnosis artifact and supplies the auto-firing
      fix-proposal chain with structured RCA input.
    - On non-final failures (the retry will run regardless), only fires when
      ``--debug`` is set. Keeps token cost at zero for the common case where
      attempt 2 succeeds and the user never needs the intermediate RCA.
    """
    if not has_more_attempts:
        return True
    return bool(getattr(ctx.options, "debug", False))


def _back_edge_pending(ctx: StepContext) -> bool:
    """True when the just-failed step queued a back-edge replay that the
    pipeline has not yet consumed.

    A step signals a back-edge by setting ``ctx.extras["rerun_step"] = N`` —
    currently only Step 9 does this, when it detects a structural codegen
    defect (zero tests collected, missing generated import) that no self-heal
    can fix and the pipeline needs to regenerate Step N and replay downward.
    The pipeline consumes that request once per run and sets
    ``_rerunN_used=True`` as a cycle guard.

    The back-edge IS the fix attempt. Firing the debug agent + critical-
    thinking + principal-software-engineer fix chain BEFORE the replay wastes
    tokens on a diagnosis the operator does not need and cannot act on. Reserve
    those aux agents for terminal failures — no back-edge queued, or the
    queued one has already been used once and the replay also failed.
    """
    target = ctx.extras.get("rerun_step")
    if not target:
        return False
    return not ctx.extras.get(f"_rerun{target}_used")


def _pipeline_and_workspace_dirs(ctx: StepContext, opt_out_env: str) -> list[Path]:
    """Read-only ``add_dirs`` grant shared by the debug agent and the
    fix-proposal chain: the run's workspace root (covers ``ctx.workspace.sut``,
    ``artifacts/``, and other steps' output) plus the qtea package source,
    unless withheld via ``opt_out_env``.

    Scoped to the package dir (not the repo root) so the add_dirs quarantine
    never touches qtea's own CLAUDE.md/.claude. Callers use distinct env vars
    (``QTEA_DEBUG_NO_PIPELINE_SRC``, ``QTEA_FIX_NO_PIPELINE_SRC``) so an
    operator can gate the debug step and the fix chain independently.
    """
    qtea_pkg_src = Path(__file__).resolve().parent.parent
    dirs = [ctx.workspace.root]
    if os.environ.get(opt_out_env, "").strip() not in ("1", "true", "yes"):
        dirs.append(qtea_pkg_src)
    return [d for d in dirs if d.exists()]


async def _run_debug_rca(
    step_num: int, ctx: StepContext, failure_context: str, attempt: int
) -> Path | None:
    """Invoke ``debug.agent.md`` for structured root-cause analysis.

    Read-only diagnosis — the agent's own contract forbids editing source,
    fixtures, or env (see ``agents/debug.agent.md``). The RCA artifact
    lands at ``<workspace>/debug/step-NN-attemptM-debug-rca.md`` and is
    stashed on ``ctx.extras`` so the ``--fix`` chain can pick it up.
    Returns the artifact path on success, ``None`` on failure (logged).
    """
    agent = package_resource_root() / "agents" / "debug.agent.md"
    if not agent.exists():
        log.warning("debug.agent_missing", path=str(agent))
        return None

    rca_workdir = ctx.workspace.debug / f"step-{step_num:02d}-attempt{attempt}-rca"
    rca_workdir.mkdir(parents=True, exist_ok=True)

    context_file = rca_workdir / "failure-context.md"
    context_file.write_text(failure_context, encoding="utf-8")

    # Grant read access to (a) the failing step's agent scratchpad
    # `<workspace>/step-NN/` (transcripts, stderr, metrics), (b) the step's
    # artefact directory `<workspace>/artifacts/stepNN/` (run-results.json,
    # bug-candidates.json, install.log — the real evidence for Step 9 and
    # other pure-code steps that have no scratchpad), and (c) the workspace
    # root so the agent can cross-reference earlier steps' artefacts if its
    # agent.md prompt tells it to.
    #
    # Prior to run 20260701-114656-9394eb only `step_workdir` was granted.
    # Step 9 has no agent scratchpad (no `step-09/` dir), so add_dirs
    # collapsed to None and the debug agent could not read
    # `artifacts/step09/run-results.json` — where Playwright's real error
    # lived. The agent's own prompt already tells it to read
    # `artifacts/stepNN/`; widening add_dirs to include it makes that
    # instruction actually possible to execute.
    step_workdir = ctx.workspace.step_workdir(step_num)
    step_artifacts = ctx.workspace.step_dir_path(step_num)
    # (d) the qtea pipeline package source (read-only). Many failures are
    # pipeline defects — a shallow gate matcher, an over-broad regex, a broken
    # contract — not SUT/test bugs. Without source access the agent guesses the
    # file/symbol (e.g. attributing the zero-assertions gate to s08_codegen.py
    # when it lives in test_indexer.py) and downstream fix-proposals inherit the
    # wrong location. Granting read of the package dir lets the RCA confirm the
    # exact gate logic. Read-only by the agent's diagnosis-only contract — same
    # add_dirs-as-read-only pattern Step 6 already uses. `QTEA_DEBUG_NO_PIPELINE_SRC=1`
    # opts out (see `_pipeline_and_workspace_dirs`).
    qtea_pkg_src = Path(__file__).resolve().parent.parent
    _dir_candidates = [step_workdir, step_artifacts] + _pipeline_and_workspace_dirs(
        ctx, "QTEA_DEBUG_NO_PIPELINE_SRC"
    )
    add_dirs = [d for d in _dir_candidates if d.exists()] or None

    out_path = ctx.workspace.debug / f"step-{step_num:02d}-attempt{attempt}-debug-rca.md"

    # Cross-run incident memory: stage a shortlist of similar past incidents
    # on THIS SUT so the agent can reuse a known root cause rather than
    # re-diagnosing from scratch. Retrieval never raises — a missing/corrupt
    # store degrades to "no prior incidents", never blocks the investigation.
    inputs = {"failure-context.md": context_file}
    prior_note = ""
    if incident_memory_enabled(ctx):
        similar = query_similar(
            fingerprint=sut_fingerprint(ctx.sut_source or ""),
            step_num=step_num,
            failure_signature=(
                failure_context.splitlines()[0] if failure_context.strip() else ""
            ),
            limit=5,
        )
        if similar:
            prior_incidents = rca_workdir / "prior-incidents.md"
            prior_incidents.write_text(
                render_prior_incidents_md(similar), encoding="utf-8"
            )
            inputs["prior-incidents.md"] = prior_incidents
            prior_note = (
                " Before investigating from scratch, read `./prior-incidents.md` "
                "— it lists past incidents on THIS SUT that may share the same "
                "root cause; use it as a lead to confirm or rule out, not as "
                "ground truth (your own investigation is authoritative)."
            )
            log.info(
                "incident_memory.prior_found",
                step=step_num,
                count=len(similar),
            )

    try:
        result = await run_agent(
            agent,
            workdir=rca_workdir,
            inputs=inputs,
            user_prompt=(
                f"Step {step_num} attempt {attempt} failed. Read "
                f"`./failure-context.md` first, then the step's artefacts "
                f"under `{step_artifacts}/` "
                f"(look for `run-results.json`, `test-output.log`, "
                f"`install.log`, `bug-candidates.json`; for Playwright "
                f"failures inspect `results[i].stdout` — the JSON reporter "
                f"emits its structured errors there, NOT to stderr) and any "
                f"transcripts under `{step_workdir / 'logs'}/`. If the failure "
                f"looks like a qtea pipeline defect (a quality-gate false "
                f"positive, an over-broad matcher, a broken contract) rather "
                f"than a SUT/test bug, read the qtea pipeline source under "
                f"`{qtea_pkg_src}/` (read-only) to confirm the EXACT file, "
                f"symbol, and logic before naming it — do not guess the "
                f"location. Produce a "
                f"structured root-cause analysis at `./debug-rca.md` "
                f"following the Phase 1-3 protocol in your agent.md. "
                f"Diagnosis only — do NOT edit source, fixtures, or env.\n\n"
                f"**Turn budget: {DEBUG_AGENT_MAX_TURNS} turns total.** "
                f"By turn ~{int(DEBUG_AGENT_MAX_TURNS * 0.75)}, stop investigating "
                f"and start writing `./debug-rca.md` with what you have — a "
                f"complete report with a named unknown ('Evidence insufficient "
                f"for X, need to read Y') is worth far more than a truncated "
                f"one. If you hit the cap without writing the file, the "
                f"orchestrator's salvage pass loses much of the evidence you "
                f"gathered."
                f"{prior_note}"
            ),
            add_dirs=add_dirs,
            timeout_s=DEBUG_AGENT_TIMEOUT_S,
            max_turns=DEBUG_AGENT_MAX_TURNS,
        )
    except Exception as e:
        log.warning("debug.rca_failed", step=step_num, error=str(e))
        return None

    produced = rca_workdir / "debug-rca.md"
    if produced.exists():
        shutil.copy2(produced, out_path)
        log.info(
            "debug.rca_written",
            step=step_num,
            attempt=attempt,
            path=str(out_path),
        )
        return out_path
    # Agent didn't write ./debug-rca.md. Two paths:
    #   * success=True + final_text present → the agent inlined the RCA in
    #     its final message instead of writing the file. Trust it.
    #   * success=False → SDK cut off (turn cap, timeout, storm, ...).
    #     Do NOT promote final_text — it's almost always pre-tool-call
    #     thinking ("Let me check X..."), not the real RCA. Write a
    #     labelled placeholder so the operator sees the failure clearly
    #     and the downstream fix chain has structured input.
    if result.success and result.final_text:
        out_path.write_text(result.final_text, encoding="utf-8")
        log.info(
            "debug.rca_final_text",
            step=step_num,
            attempt=attempt,
            path=str(out_path),
        )
        return out_path
    # Turn-cap salvage: the primary invocation exhausted its max_turns
    # without writing debug-rca.md. Rather than collapse to a placeholder
    # (which loses everything the agent already thought about), fire a
    # bounded finalizer that synthesizes the transcript into the artifact.
    if result.hit_max_turns:
        log.info(
            "debug.rca_finalizer_start",
            step=step_num,
            attempt=attempt,
        )
        finalized = await _finalize_truncated_agent(
            agent_path=agent,
            parent_workdir=rca_workdir,
            expected_filename="debug-rca.md",
            output_path=out_path,
            agent_label="debug.agent",
            failure_context=failure_context,
            add_dirs=add_dirs,
            extra_inputs={"failure-context.md": context_file},
        )
        if finalized is not None:
            log.info(
                "debug.rca_finalized",
                step=step_num,
                attempt=attempt,
                path=str(finalized),
            )
            return finalized
    placeholder = _agent_failure_placeholder(
        agent_label="debug.agent",
        result=result,
        failure_context=failure_context,
    )
    out_path.write_text(placeholder, encoding="utf-8")
    log.warning(
        "debug.rca_placeholder_written",
        step=step_num,
        attempt=attempt,
        agent_error=result.error,
        path=str(out_path),
    )
    return out_path


async def _run_fix_proposal(
    step_num: int,
    ctx: StepContext,
    failure_context: str,
    result: StepResult,
    debug_rca_path: Path | None = None,
) -> Path | None:
    """Auto-fires on retry exhaustion (unless ``--no-fix``).

    Chain: consumes the debug agent's RCA (already written on final-failure
    via ``_run_debug_rca``) → critical-thinking agent reasons about fix
    approach → principal-software-engineer produces concrete fix proposal.

    ``result`` is the final (retry-exhausting) ``StepResult`` — used to
    classify the incident against the failure that actually triggered this
    chain (not attempt 1's) when recording to cross-run incident memory.

    ``debug_rca_path`` should point at ``<ws>/debug/step-NN-attemptM-debug-rca.md``
    (path stashed on ``ctx.extras[f"step{n}_rca_path"]``). If ``None`` /
    unreadable, falls back to ``failure_context`` so the chain still runs.
    """
    agents_root = package_resource_root() / "agents"
    fix_workdir = ctx.workspace.debug / f"step-{step_num:02d}-fix"
    fix_workdir.mkdir(parents=True, exist_ok=True)

    context_file = fix_workdir / "failure-context.md"
    context_file.write_text(failure_context, encoding="utf-8")

    # Read-only grant so critical-thinking/principal-software-engineer can
    # verify the debug RCA's "Affected Surface" against the real pipeline
    # source and SUT clone instead of trusting it blindly — a documented
    # incident had the fix-proposal name the wrong file (s08_codegen.py
    # instead of test_indexer.py) because neither agent could check.
    # `QTEA_FIX_NO_PIPELINE_SRC=1` opts out (see `_pipeline_and_workspace_dirs`).
    qtea_pkg_src = Path(__file__).resolve().parent.parent
    fix_add_dirs = _pipeline_and_workspace_dirs(ctx, "QTEA_FIX_NO_PIPELINE_SRC")

    debug_rca_text = ""
    if debug_rca_path is not None:
        try:
            debug_rca_text = debug_rca_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning(
                "fix.debug_rca_read_failed",
                step=step_num,
                path=str(debug_rca_path),
                error=str(e),
            )

    if not debug_rca_text:
        debug_rca_text = failure_context

    # Aggregated final RCA (CLAUDE.md § Guardrails: debug-directory reads) = debug agent's RCA.
    # Belt-and-braces guard: don't clobber a substantive prior artifact
    # with a shorter one — a shorter payload here is almost always a
    # placeholder from a truncated debug run, and a well-formed prior RCA
    # (from a previous invocation of the same workspace) is more useful
    # to the operator than the placeholder that replaces it. Same-size /
    # larger content overwrites unchanged.
    rca_output = ctx.workspace.debug / f"step-{step_num:02d}-rca.md"
    new_bytes = len(debug_rca_text.encode("utf-8"))
    if rca_output.exists() and rca_output.stat().st_size > new_bytes:
        log.info(
            "fix.aggregated_rca_kept_prior",
            step=step_num,
            existing_bytes=rca_output.stat().st_size,
            new_bytes=new_bytes,
        )
    else:
        rca_output.write_text(debug_rca_text, encoding="utf-8")

    # Step 1: critical-thinking reasons about HOW to fix, given the RCA
    ct_agent = agents_root / "critical-thinking.agent.md"
    strategy_text = ""
    if ct_agent.exists():
        thinking_workdir = fix_workdir / "thinking"
        thinking_workdir.mkdir(parents=True, exist_ok=True)
        ct_debug_rca = thinking_workdir / "debug-rca.md"
        ct_debug_rca.write_text(debug_rca_text, encoding="utf-8")
        try:
            ct_result = await _record_aux_agent(
                ctx,
                step_num,
                "critical_thinking",
                "critical-thinking.agent.md",
                run_agent(
                    ct_agent,
                    workdir=thinking_workdir,
                    inputs={
                        "debug-rca.md": ct_debug_rca,
                        "failure-context.md": context_file,
                    },
                    user_prompt=(
                        "The debug agent has identified the root cause of a "
                        "test/step failure in ./debug-rca.md (raw failure context "
                        "in ./failure-context.md). Its \"Affected Surface\" "
                        "(file/symbol) can be wrong — before reasoning about fix "
                        "approaches, confirm it against the real code: you have "
                        f"read-only access to the qtea pipeline source under "
                        f"{qtea_pkg_src}/ and the SUT clone under "
                        f"{ctx.workspace.sut}/. Think critically about HOW to "
                        "fix this problem: challenge assumptions about the fix "
                        "approach, consider alternative fixes and their tradeoffs, "
                        "and identify risks. Write your fix-strategy to "
                        "./fix-strategy.md\n\n"
                        f"**Turn budget: {FIX_AGENT_MAX_TURNS} turns total.** "
                        f"By turn ~{int(FIX_AGENT_MAX_TURNS * 0.75)}, stop "
                        f"exploring and write ./fix-strategy.md — a strategy that "
                        f"names what it couldn't verify is worth more than a "
                        f"truncated one that never landed on disk."
                    ),
                    add_dirs=fix_add_dirs,
                    timeout_s=FIX_AGENT_TIMEOUT_S,
                    max_turns=FIX_AGENT_MAX_TURNS,
                ),
            )
            strategy_file = thinking_workdir / "fix-strategy.md"
            if strategy_file.exists():
                strategy_text = strategy_file.read_text(encoding="utf-8")
            elif ct_result.success and ct_result.final_text:
                strategy_text = ct_result.final_text
            elif ct_result.hit_max_turns:
                # Turn-cap salvage: synthesize the transcript into
                # fix-strategy.md via a bounded finalizer before falling
                # through to the placeholder path.
                log.info("fix.strategy_finalizer_start", step=step_num)
                finalized = await _finalize_truncated_agent(
                    agent_path=ct_agent,
                    parent_workdir=thinking_workdir,
                    expected_filename="fix-strategy.md",
                    output_path=thinking_workdir / "fix-strategy.md",
                    agent_label="critical-thinking.agent",
                    failure_context=failure_context,
                    add_dirs=fix_add_dirs,
                    extra_inputs={
                        "debug-rca.md": ct_debug_rca,
                        "failure-context.md": context_file,
                    },
                )
                if finalized is not None and finalized.exists():
                    strategy_text = finalized.read_text(encoding="utf-8")
                    log.info(
                        "fix.strategy_finalized",
                        step=step_num,
                        path=str(finalized),
                    )
                else:
                    log.warning(
                        "fix.strategy_placeholder_written",
                        step=step_num,
                        agent_error=ct_result.error,
                        finalizer_failed=True,
                    )
                    strategy_text = _agent_failure_placeholder(
                        agent_label="critical-thinking.agent",
                        result=ct_result,
                        failure_context=failure_context,
                    )
            else:
                # Non-turn-cap failure (timeout / storm / crash): salvaging
                # from an empty or aborted transcript isn't productive.
                # Emit the labelled placeholder so the eng agent sees a
                # real diagnosis of the CT failure rather than a
                # "Let me check X..." stub masquerading as analysis.
                log.warning(
                    "fix.strategy_placeholder_written",
                    step=step_num,
                    agent_error=ct_result.error,
                )
                strategy_text = _agent_failure_placeholder(
                    agent_label="critical-thinking.agent",
                    result=ct_result,
                    failure_context=failure_context,
                )
        except Exception as e:
            log.warning("fix.strategy_failed", step=step_num, error=str(e))
            strategy_text = (
                f"Critical-thinking agent failed: {e}\n\n"
                f"Debug RCA:\n{debug_rca_text}"
            )

    if not strategy_text:
        strategy_text = debug_rca_text

    fix_agent = agents_root / "principal-software-engineer.agent.md"
    proposal_path = ctx.workspace.debug / f"step-{step_num:02d}-fix-proposal.md"

    if fix_agent.exists():
        eng_workdir = fix_workdir / "eng"
        eng_workdir.mkdir(parents=True, exist_ok=True)

        eng_debug_rca = eng_workdir / "debug-rca.md"
        eng_debug_rca.write_text(debug_rca_text, encoding="utf-8")
        eng_strategy = eng_workdir / "fix-strategy.md"
        eng_strategy.write_text(strategy_text, encoding="utf-8")

        try:
            eng_result = await _record_aux_agent(
                ctx,
                step_num,
                "principal_engineer",
                "principal-software-engineer.agent.md",
                run_agent(
                    fix_agent,
                    workdir=eng_workdir,
                    inputs={
                        "debug-rca.md": eng_debug_rca,
                        "fix-strategy.md": eng_strategy,
                    },
                    user_prompt=(
                        "The debug agent's root-cause analysis is in "
                        "./debug-rca.md — its \"Affected Surface\" section is "
                        "the file/symbol list the investigation already "
                        "identified, but it can be wrong. You have read-only "
                        f"access to the qtea pipeline source under "
                        f"{qtea_pkg_src}/ and the SUT clone under "
                        f"{ctx.workspace.sut}/ — use it to confirm the exact "
                        "file and symbol before citing the Affected Surface in "
                        "your proposal; if it doesn't hold up, correct it and "
                        "say so. The critical-thinking analysis of fix "
                        "approaches is in ./fix-strategy.md. Produce a concrete "
                        "fix proposal at ./fix-proposal.md. Do NOT edit any "
                        "source code directly — this is read-only "
                        "investigation, and the proposal is a hand-off to the "
                        "operator.\n\n"
                        f"**Turn budget: {FIX_AGENT_MAX_TURNS} turns total.** "
                        f"By turn ~{int(FIX_AGENT_MAX_TURNS * 0.75)}, stop "
                        f"verifying and write ./fix-proposal.md — a proposal "
                        f"that flags a remaining verification step is worth "
                        f"more than a truncated one that never landed on disk."
                    ),
                    add_dirs=fix_add_dirs,
                    timeout_s=FIX_AGENT_TIMEOUT_S,
                    max_turns=FIX_AGENT_MAX_TURNS,
                ),
            )
            produced = eng_workdir / "fix-proposal.md"
            if produced.exists():
                shutil.copy2(produced, proposal_path)
            elif eng_result.success and eng_result.final_text:
                # Agent finished cleanly but inlined the proposal in its
                # final message instead of writing the file. Trust it.
                proposal_path.write_text(eng_result.final_text, encoding="utf-8")
            elif eng_result.hit_max_turns:
                # Turn-cap salvage: fire a bounded finalizer that
                # synthesizes the PSE transcript into fix-proposal.md
                # before collapsing to a placeholder-only proposal.
                log.info("fix.eng_finalizer_start", step=step_num)
                finalized = await _finalize_truncated_agent(
                    agent_path=fix_agent,
                    parent_workdir=eng_workdir,
                    expected_filename="fix-proposal.md",
                    output_path=proposal_path,
                    agent_label="principal-software-engineer.agent",
                    failure_context=failure_context,
                    add_dirs=fix_add_dirs,
                    extra_inputs={
                        "debug-rca.md": eng_debug_rca,
                        "fix-strategy.md": eng_strategy,
                    },
                )
                if finalized is None:
                    log.warning(
                        "fix.eng_placeholder_written",
                        step=step_num,
                        agent_error=eng_result.error,
                        finalizer_failed=True,
                    )
                    header = _agent_failure_placeholder(
                        agent_label="principal-software-engineer.agent",
                        result=eng_result,
                        failure_context=failure_context,
                    )
                    proposal_path.write_text(
                        f"{header}\n\n## Upstream Debug RCA\n\n{debug_rca_text}\n\n"
                        f"## Upstream Fix Strategy\n\n{strategy_text}\n",
                        encoding="utf-8",
                    )
                else:
                    log.info(
                        "fix.eng_finalized",
                        step=step_num,
                        path=str(finalized),
                    )
            elif not eng_result.success:
                # Non-turn-cap failure (timeout / storm / crash) — nothing
                # useful in the transcript to salvage. Emit a labelled
                # placeholder with the upstream RCA + strategy inlined so
                # the operator can still take the hand-off manually.
                log.warning(
                    "fix.eng_placeholder_written",
                    step=step_num,
                    agent_error=eng_result.error,
                )
                header = _agent_failure_placeholder(
                    agent_label="principal-software-engineer.agent",
                    result=eng_result,
                    failure_context=failure_context,
                )
                proposal_path.write_text(
                    f"{header}\n\n## Upstream Debug RCA\n\n{debug_rca_text}\n\n"
                    f"## Upstream Fix Strategy\n\n{strategy_text}\n",
                    encoding="utf-8",
                )
            else:
                proposal_path.write_text(
                    f"# Fix Proposal (auto-generated)\n\n"
                    f"Engineering agent did not produce a proposal.\n\n"
                    f"## RCA\n\n{debug_rca_text}\n\n"
                    f"## Fix Strategy\n\n{strategy_text}",
                    encoding="utf-8",
                )
        except Exception as e:
            log.warning("fix.eng_failed", step=step_num, error=str(e))
            proposal_path.write_text(
                f"# Fix Proposal (auto-generated)\n\n"
                f"Engineering agent failed: {e}\n\n"
                f"## RCA\n\n{debug_rca_text}\n\n"
                f"## Fix Strategy\n\n{strategy_text}",
                encoding="utf-8",
            )
    else:
        proposal_path.write_text(
            f"# Fix Proposal (auto-generated)\n\n"
            f"No engineering agent available.\n\n"
            f"## RCA\n\n{debug_rca_text}\n\n"
            f"## Fix Strategy\n\n{strategy_text}",
            encoding="utf-8",
        )

    log.info("fix.proposal_written", step=step_num, path=str(proposal_path))

    # Persist to cross-run incident memory so a future run against this SUT
    # can retrieve this diagnosis instead of re-investigating. Never raises.
    if incident_memory_enabled(ctx):
        from qtea.failure_classifiers import FailureCategory, classify_failure
        try:
            category = classify_failure(result, ctx).category
        except Exception:  # noqa: BLE001 — classification must not block record
            category = FailureCategory.UNKNOWN
        step_record = ctx.state.steps.get(step_num)
        record_incident(
            ctx=ctx,
            step_num=step_num,
            step_name=step_record.name if step_record else f"step-{step_num:02d}",
            attempt=step_record.attempts if step_record else 0,
            category=category,
            failure_context=failure_context,
            debug_rca_text=debug_rca_text,
            strategy_text=strategy_text,
            fix_proposal_path=proposal_path,
        )

    return proposal_path


async def run_agent_with_hitl(
    *,
    ctx: StepContext,
    agent_path: Path,
    workdir: Path,
    inputs: dict[str, Path],
    user_prompt: str,
    output_filename: str,
    agent_label: str,
    extra_paths: list[Path] | None = None,
    timeout_s: int | None = None,
    step: int | None = None,
    max_turns: int | None = 25,
    claude_md: Path | None = None,
    max_iterations: int = HITL_MAX_ITERATIONS,
    enable_mcp: bool = False,
) -> AgentResult:
    """Run an agent; if its output contains unresolved questions, prompt user
    and re-invoke with answers staged as ``user-answers.md``. Loops until the
    output is clean or ``max_iterations`` is reached.

    Skips the prompt loop when ``--no-hitl`` is set or stdin is not a TTY.

    ``enable_mcp`` mirrors the `run_agent` flag and forwards as-is. Default
    False matches the `run_agent` default; pass True only when the agent
    invoked through HITL actually uses MCP tools.
    """
    extras = list(extra_paths or [])
    current_inputs = dict(inputs)
    iteration_prompt = user_prompt
    result: AgentResult | None = None
    skipped_keys: set[str] = set()  # questions the user opted to skip
    # Resume the SDK session on iteration 2+ so the cached system prompt and
    # conversation prefix from iteration 1 hit cache_read instead of paying
    # the 25% cache_creation premium again. Empirically this halves the cost
    # of a HITL re-invocation. None on iteration 1 (no session to resume).
    resume_session: str | None = None

    hitl_disabled = getattr(ctx.options, "no_hitl", False)
    hitl_dir = (
        workdir.parent / f".hitl-step{step:02d}" if step else workdir.parent / ".hitl"
    )

    # Cross-step ledger (see hitl.py for the design). In-memory list on
    # ctx.extras, mirrored to <workspace>/.hitl-ledger.jsonl so resumed runs
    # don't re-prompt for already-decided items.
    workspace_root = ctx.workspace.root
    if "hitl_ledger" not in ctx.extras:
        ctx.extras["hitl_ledger"] = load_ledger(workspace_root)
    ledger: list[HitlDecision] = ctx.extras["hitl_ledger"]
    step_decisions: dict[str, HitlDecision] = {}

    # Inject prior-decisions.md as a staged input so the agent sees the
    # ledger from turn 1 and doesn't paraphrase already-resolved items in
    # the first place.
    if ledger:
        prior_md_path = hitl_dir / "prior-decisions.md"
        hitl_dir.mkdir(parents=True, exist_ok=True)
        prior_md_path.write_text(
            render_prior_decisions_md(ledger), encoding="utf-8"
        )
        current_inputs.setdefault("prior-decisions.md", prior_md_path)
        iteration_prompt = (
            f"{user_prompt}\n\n"
            f"**Note:** `./prior-decisions.md` lists items the user already "
            f"addressed in earlier steps of this run. Treat each entry as "
            f"final — do NOT re-emit them as new blockers, clarifications, "
            f"or open questions. Each entry carries its own directive on "
            f"how to honor it (answered → apply verbatim; dropped → remove "
            f"coverage and record in `## Coverage Notes`; scope-exclusion → "
            f"exclude the named scope; legacy-skipped → preserve "
            f"`[ASSUMPTION]` framing)."
        )

    for iteration in range(1, max_iterations + 1):
        result = await run_agent(
            agent_path,
            workdir=workdir,
            inputs=current_inputs,
            user_prompt=iteration_prompt,
            extra_paths=extras,
            timeout_s=timeout_s,
            step=step,
            max_turns=max_turns,
            claude_md=claude_md,
            resume=resume_session,
            enable_mcp=enable_mcp,
        )
        # Capture session for the next iteration, regardless of whether we
        # actually re-invoke -- harmless if we don't.
        if result.session_id:
            resume_session = result.session_id

        produced = workdir / output_filename
        if not result.success or not produced.exists():
            _flush_step_decisions(ledger, step_decisions, workspace_root)
            return result

        if hitl_disabled:
            _flush_step_decisions(ledger, step_decisions, workspace_root)
            return result

        md_text = produced.read_text(encoding="utf-8")
        all_questions = extract_questions(md_text)

        # Cross-step ledger filter: paraphrases of already-decided items
        # bypass the user prompt entirely.
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

        # Don't re-ask questions the user already chose to skip — the agent is
        # expected to have replaced them with `[ASSUMPTION]` notes. If it didn't,
        # we still won't re-prompt; we just stop the loop to avoid annoyance.
        new_questions = [
            q for q in novel_questions if question_key(q) not in skipped_keys
        ]

        if not new_questions and not ledger_resolved:
            if all_questions:
                log.info(
                    "hitl.only_previously_skipped",
                    agent=agent_label,
                    total=len(all_questions),
                    skipped=len(skipped_keys),
                )
            _flush_step_decisions(ledger, step_decisions, workspace_root)
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
            _flush_step_decisions(ledger, step_decisions, workspace_root)
            return result

        answers = prompt_user(new_questions, agent_label=agent_label) if new_questions else {}
        # prompt_user now returns dict[str, tuple[str, str]] — skipped items
        # absent from the dict; answered/scope-exclusion items carry
        # (resolution, text).
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

        # Always re-invoke so the agent can either incorporate answers OR
        # drop skipped clarifications. Skipping the re-run would leave
        # `[CLARIFICATION NEEDED]` tags in the output.
        # Mirrors the iteration prompt in
        # `call_reasoning_llm_with_hitl` (src/qtea/llm/reasoning.py) —
        # semantics must stay aligned across both HITL paths.
        hitl_dir.mkdir(parents=True, exist_ok=True)
        answers_src = write_answers_file(
            hitl_dir,
            new_questions,
            answers,
            skipped=skipped_this_round,
            ledger_resolved=ledger_resolved,
        )
        current_inputs = dict(inputs)
        current_inputs["user-answers.md"] = answers_src
        if ledger:
            # Re-stage prior-decisions.md on each iteration too — the agent's
            # working directory is rebuilt per turn in the file-staging path.
            current_inputs.setdefault("prior-decisions.md", prior_md_path)
        iteration_prompt = (
            f"{user_prompt}\n\n"
            f"The user has reviewed your clarification questions. See "
            f"`./user-answers.md` for their responses, which include "
            f"answered items, items the user chose to skip (drop), items "
            f"the user excluded by scope, and items already resolved "
            f"earlier in this run.\n\n"
            f"- For ANSWERED items: incorporate the answer and remove the "
            f"corresponding `[CLARIFICATION NEEDED]` tag, blocker row, or "
            f"open-question entry.\n"
            f"- For SKIPPED items: REMOVE the entire AC / TC / sub-item "
            f"the question was attached to from the document body. Append "
            f"an entry to a `## Coverage Notes` section at the end of the "
            f"document recording the dropped ID and the reason. **Do NOT "
            f"write `[ASSUMPTION: ...]`** — the user's intent is to drop "
            f"this coverage, not test it under an invented value. Do NOT "
            f"re-emit `[CLARIFICATION NEEDED]` for skipped items.\n"
            f"- For SCOPE-EXCLUDED items: interpret the user's answer as "
            f"a scope-exclusion (e.g. \"mobile isn't in scope\" → "
            f"exclude mobile). Remove ACs / TCs / sub-bullets that depend "
            f"solely on the excluded scope; keep the in-scope portions. "
            f"Append an entry to `## Coverage Notes` recording the "
            f"exclusion and the user's exact answer. Do NOT include the "
            f"typed answer as a literal value in the document body.\n"
            f"- For PREVIOUSLY RESOLVED items: follow the per-item "
            f"directive in `./user-answers.md` (answered → apply verbatim; "
            f"skipped-drop → drop; scope-exclusion → exclude). Do NOT "
            f"re-raise these to the user.\n\n"
            f"**Preserve `## Coverage Notes` across iterations.** If the "
            f"document already has a `## Coverage Notes` section from a "
            f"prior iteration, preserve its entries verbatim and only "
            f"append new ones.\n\n"
            f"Rewrite `./{output_filename}` accordingly. Keep the rest of "
            f"the document intact."
        )

    _flush_step_decisions(ledger, step_decisions, workspace_root)
    return result  # pragma: no cover (loop always returns inside)


def _flush_step_decisions(
    ledger: list[HitlDecision],
    step_decisions: dict[str, HitlDecision],
    workspace_root: Path,
) -> None:
    """Append this step's decisions to in-memory + on-disk ledger.

    Idempotent across early returns from the iteration loop: clears the
    accumulator after appending so a second call is a no-op.
    """
    if not step_decisions:
        return
    new_entries = list(step_decisions.values())
    ledger.extend(new_entries)
    append_ledger(workspace_root, new_entries)
    step_decisions.clear()


class Step(ABC):
    """All pipeline steps subclass this.

    ## Workdir contract

    Every step has two directories on the workspace, and the distinction
    between them is load-bearing:

      - ``out_dir()`` -> ``artifacts/stepNN/``: PUBLISHED outputs. The
        hand-off surface to downstream steps. Written by the step on
        success, read by later steps as their ``inputs``. Treat as
        immutable once written.

      - ``workdir()`` -> ``agent-work/stepNN/``: AGENT SCRATCHPAD. A
        throwaway staging area where ``run_agent`` copies the agent
        prompt, skills, prior-step outputs, and other ``extra_paths`` so
        the ``claude`` CLI subprocess can see them at its cwd. Files
        here have no semantic value beyond the current attempt.

    INVARIANT: a step must NEVER read its own workdir as a source of
    truth across attempts. Every input must be re-sourced from outside
    the workdir on every attempt -- from package resources
    (``package_resource_root()``), from ``ctx.sut_source``, from the
    ``artifacts/`` directories of prior steps, etc. (Reading a file
    within the same ``run()`` call that wrote it is fine; that is
    within-attempt staging, not cross-attempt state.)

    This invariant guarantees:

      - Reruns and retries are deterministic regardless of any residue
        left in the workdir by a prior attempt.
      - Fixes to package resources (skills, agent prompts) are picked
        up on the very next attempt with no cache invalidation needed.
      - Workdir contents may be wiped at any time without affecting
        correctness; the forensic audit trail lives in
        ``workspace.debug/`` via ``_snapshot_debug_artifacts``.

    Downstream-step idempotency markers (e.g. "skip if already posted")
    belong in ``out_dir()``, not in ``workdir()``: the published
    artifact directory is the only place where cross-attempt state has
    meaning.
    """

    number: int = 0
    name: str = ""
    timeout_s: int | None = None
    # Names of MCP servers (from `.mcp.json`) this step's agent calls require.
    # The pipeline runs `probe_server()` JUST for these names, just before
    # the step executes, so the npx cache + lazy server init complete
    # contiguously with the SDK spawn — fixing the "playwright reports
    # pending at SDK init" race observed in run 20260611-184450 step 9.
    # Empty (default) means the step doesn't use MCP and preflight is
    # skipped entirely. See `pipeline._mcp_preflight_for_step`.
    mcp_servers_required: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    async def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover (abstract)
        ...

    def out_dir(self, ws: Workspace) -> Path:
        return ws.step_dir(self.number)

    def workdir(self, ws: Workspace) -> Path:
        return ws.step_workdir(self.number)

    def pre_attempt_cleanup(self, ctx: StepContext, attempt: int) -> None:
        """Hook fired BEFORE attempt 2+ starts. Override in steps with
        cross-attempt side effects to wipe or rotate stale artifacts so the
        next attempt sees a clean slate. ``attempt`` is the upcoming attempt
        number (2 or higher). Default is no-op."""
        return None

    async def execute(self, ctx: StepContext) -> StepResult:
        """Wraps `run()` with timing, state-record updates, retry, and fix-proposal."""
        # Each attempt below re-invokes self.run(ctx) from scratch. run() is
        # responsible for repopulating the workdir from external sources on
        # every call; nothing in agent-work/stepNN/ survives semantically
        # between attempts. See the Step docstring "Workdir contract" for
        # the full invariant.
        record = ctx.state.steps.get(self.number) or StepRecord(step=self.number, name=self.name)
        ctx.state.steps[self.number] = record

        result = await self._attempt(ctx, record)

        # Debug RCA on attempt-1 failure. Sidecar — fires before any retry
        # decision and never blocks the retry path. Default gating fires it
        # only when this is the FINAL failure (last attempt); --debug
        # promotes it to fire on every failed attempt. See
        # `_should_run_debug_rca`.
        #
        # Skipped when the step queued a back-edge replay (`_back_edge_pending`):
        # the pipeline is about to regenerate an upstream step and re-enter
        # this one, so a diagnosis now is premature. If the replay also fails,
        # the guard flips and the aux agents fire on the terminal attempt.
        if (
            not result.success
            and _should_run_debug_rca(
                ctx, has_more_attempts=record.attempts < MAX_ATTEMPTS
            )
            and not _back_edge_pending(ctx)
        ):
            fc = _build_failure_context(self.number, self.name, record, result)
            rca = await _record_aux_agent(
                ctx,
                self.number,
                "debug",
                "debug.agent.md",
                _run_debug_rca(self.number, ctx, fc, attempt=record.attempts),
            )
            if rca:
                ctx.extras[f"step{self.number}_rca_path"] = str(rca)
        elif not result.success and _back_edge_pending(ctx):
            log.info(
                "step.aux_agents_skipped_back_edge",
                step=self.number,
                phase="debug_attempt1",
                target=ctx.extras.get("rerun_step"),
            )

        if not result.success and record.attempts < MAX_ATTEMPTS:
            # Classify the failure for audit + hint propagation. The
            # category is logged for the audit trail; when the classifier
            # returns a fix_hint dict it gets merged into ctx.extras so
            # the next attempt's run() can pick it up (e.g. a prompt
            # clarification for schema violations). Pure-Python; no LLM
            # call. See qtea/failure_classifiers.py for category list.
            from qtea.failure_classifiers import classify_failure
            classification = classify_failure(result, ctx)
            log.info(
                "step.failure_classified",
                step=self.number,
                category=classification.category.value,
                safe_to_auto_retry=classification.safe_to_auto_retry,
                explanation=classification.explanation,
            )
            if classification.fix_hint:
                ctx.extras.update(classification.fix_hint)
                log.info(
                    "step.fix_hint_applied",
                    step=self.number,
                    hint_keys=list(classification.fix_hint.keys()),
                )
            # Stash the classification for downstream consumers and
            # audit-log inspection on the final-failure path.
            ctx.extras[f"step{self.number}_failure_category"] = (
                classification.category.value
            )

            # Non-retryable HTTP errors (4xx auth/quota, 5xx outage):
            # skip retry entirely — no debug agent, no attempt 2. The
            # error won't resolve by retrying; surface it immediately.
            if _is_api_fatal_error(result.error):
                log.error(
                    "step.api_fatal_no_retry",
                    step=self.number,
                    error=result.error,
                )
                return result

            # API-retry-storm classification gate. For transient upstream
            # failures (exit_code -10 inside the runner, surfaced as
            # error text matching `_API_RETRY_STORM_PREFIX`), the default
            # "immediate retry" lands attempt 2 inside the same incident
            # window — observed in run 20260611-075728-0aa560 step 8
            # where both attempts hit the storm at the same trajectory
            # point ~16 s apart. Prompt the user for a smarter choice
            # when we have a TTY. Non-interactive paths
            # (--no-hitl / --yes / no-TTY) keep the immediate-retry
            # behavior so CI / batch runs are unaffected.
            storm = _is_api_retry_storm(result.error)
            decision = "retry"
            interactive = (
                storm
                and (sys.stdin.isatty() or getattr(ctx.options, "ui_mode", False))
                and not getattr(ctx.options, "no_hitl", False)
                and not getattr(ctx.options, "yes", False)
            )
            if interactive:
                console = Console()
                decision = await _prompt_storm_retry_decision(
                    step_num=self.number,
                    step_name=self.name,
                    attempt=record.attempts,
                    error=result.error or "",
                    console=console,
                )
                log.info(
                    "step.storm_decision",
                    step=self.number,
                    decision=decision,
                )

            if decision == "abort":
                console_abort = Console()
                console_abort.print(
                    f"[dim]step {self.number:02d} aborted by user "
                    "after upstream storm.[/]"
                )
                # Skip retry. Caller treats !success as failure and stops.
                return result

            if decision == "wait":
                console_wait = Console()
                console_wait.print(
                    f"[dim]waiting {_STORM_RETRY_WAIT_S} s for the "
                    "upstream to recover before retrying…[/]"
                )
                log.info(
                    "step.storm_wait",
                    step=self.number,
                    wait_s=_STORM_RETRY_WAIT_S,
                )
                try:
                    await asyncio.sleep(_STORM_RETRY_WAIT_S)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # User Ctrl-C during the wait — treat as abort, not
                    # an exception that crashes the pipeline.
                    console_wait.print("[dim]wait interrupted; aborting step.[/]")
                    return result

            # Retry-classification gate: should attempt 2 RESUME the prior
            # SDK session, or START FRESH?
            #
            # - Transient transport failure (api_retry_storm): RESUME is
            #   the right call. The agent already read its inputs and made
            #   progress; resume skips the re-Read cost and picks up at
            #   the turn that the relay dropped.
            # - Content / validation failure (everything else): START
            #   FRESH. Resuming would re-play the agent's prior reasoning
            #   path and reproduce the same flawed output — observed in
            #   run 20260611-075728-0aa560 step 8 where both attempts
            #   resumed the same Haiku session and emitted the same 5
            #   `wait_for_timeout` violations.
            #
            # Steps that opt into resume-on-retry stash their session id
            # at `ctx.extras["step{N}_resume_session"]`. We clear that key
            # for non-transient failures so attempt 2's run() reads None
            # and starts a fresh conversation.
            if not _is_api_retry_storm(result.error):
                resume_key = f"step{self.number}_resume_session"
                if ctx.extras.pop(resume_key, None) is not None:
                    log.info(
                        "step.resume_cleared",
                        step=self.number,
                        reason="non_transient_failure",
                    )

            _snapshot_debug_artifacts(self.number, ctx, record.attempts)
            ctx.extras["debug_live"] = True
            try:
                self.pre_attempt_cleanup(ctx, record.attempts + 1)
            except Exception as e:
                log.warning(
                    "step.pre_attempt_cleanup_failed",
                    step=self.number,
                    attempt=record.attempts + 1,
                    error=str(e),
                )
            log.info("step.retry", step=self.number, name=self.name)
            result = await self._attempt(ctx, record)

            # Debug RCA on attempt-2 failure. This is always the FINAL
            # failure path (MAX_ATTEMPTS=2), so `has_more_attempts=False`.
            # Same back-edge guard as attempt 1: if the step queued a replay,
            # the pipeline will handle the fix — no diagnosis needed yet.
            if (
                not result.success
                and _should_run_debug_rca(ctx, has_more_attempts=False)
                and not _back_edge_pending(ctx)
            ):
                fc = _build_failure_context(self.number, self.name, record, result)
                rca = await _record_aux_agent(
                    ctx,
                    self.number,
                    "debug",
                    "debug.agent.md",
                    _run_debug_rca(self.number, ctx, fc, attempt=record.attempts),
                )
                if rca:
                    ctx.extras[f"step{self.number}_rca_path"] = str(rca)
            elif not result.success and _back_edge_pending(ctx):
                log.info(
                    "step.aux_agents_skipped_back_edge",
                    step=self.number,
                    phase="debug_attempt2",
                    target=ctx.extras.get("rerun_step"),
                )

            if result.success and result.status not in ("skipped",):
                result.status = "warned"
                record.status = "warned"
                record.notes = f"succeeded on retry (attempt {record.attempts})"

        # Fix-proposal chain auto-fires on final failure (chain: debug RCA
        # from the just-completed _run_debug_rca → critical-thinking →
        # principal-software-engineer). Suppressed by ``--no-fix``.
        # No outer cost wrapper here — `_run_fix_proposal` wraps each
        # constituent agent (critical-thinking, principal-engineer) with
        # `_record_aux_agent` individually so each surfaces as its own row
        # in the summary table.
        #
        # Same back-edge guard as the debug RCA above: the fix chain has no
        # useful input when the pipeline is about to attempt its own fix by
        # regenerating an upstream step. It re-fires on the terminal attempt
        # if the replay also fails.
        if (
            not result.success
            and not getattr(ctx.options, "no_fix", False)
            and not _back_edge_pending(ctx)
        ):
            failure_context = _build_failure_context(
                self.number, self.name, record, result
            )
            rca_path_str = ctx.extras.get(f"step{self.number}_rca_path")
            debug_rca_path = Path(rca_path_str) if rca_path_str else None
            await _run_fix_proposal(
                self.number, ctx, failure_context, result,
                debug_rca_path=debug_rca_path,
            )
        elif not result.success and _back_edge_pending(ctx):
            log.info(
                "step.aux_agents_skipped_back_edge",
                step=self.number,
                phase="fix_proposal",
                target=ctx.extras.get("rerun_step"),
            )

        return result

    async def _attempt(self, ctx: StepContext, record: StepRecord) -> StepResult:
        record.attempts += 1
        record.status = "in_progress"
        record.started_at = datetime.now(UTC).isoformat()

        log.info("step.start", step=self.number, name=self.name, attempt=record.attempts)
        # Fresh accumulator per attempt; we accumulate the result onto the
        # StepRecord cumulatively so retries reflect total billing, not just
        # the final attempt. First attempt starts from zero implicitly because
        # StepRecord defaults the fields to 0.
        accumulator = StepMetricsAccumulator()
        token = CURRENT_STEP_METRICS.set(accumulator)
        started = time.monotonic()
        try:
            try:
                result = await self.run(ctx)
            except Exception as e:
                duration = time.monotonic() - started
                record.status = "failed"
                # A prior attempt may have left e.g. sub_status="bugs_found"
                # (Step 9). Clear it so a checkpoint read after this failed
                # attempt can't be misread as the earlier attempt's outcome.
                record.sub_status = None
                record.finished_at = datetime.now(UTC).isoformat()
                record.duration_s = round(duration, 3)
                record.notes = f"unhandled exception: {e}"
                _accumulate_metrics_into_record(record, accumulator)
                log.exception("step.exception", step=self.number, error=str(e))
                log.info(
                    "step.end",
                    step=self.number,
                    name=self.name,
                    status="failed",
                    sub_status=None,
                    duration_s=record.duration_s,
                    outputs=[],
                    tokens_input=record.tokens_input,
                    tokens_output=record.tokens_output,
                    tokens_cache_read=record.tokens_cache_read,
                    tokens_cache_write=record.tokens_cache_creation,
                    agent_calls=record.agent_calls,
                    error=str(e),
                )
                return StepResult(success=False, status="failed", outputs=[], error=str(e))

            duration = time.monotonic() - started
            record.status = result.status
            record.sub_status = result.sub_status
            record.finished_at = datetime.now(UTC).isoformat()
            record.duration_s = round(duration, 3)
            record.notes = result.notes
            record.output_hashes = hash_paths(result.outputs)
            _accumulate_metrics_into_record(record, accumulator)
            log.info(
                "step.end",
                step=self.number,
                name=self.name,
                status=result.status,
                sub_status=result.sub_status,
                duration_s=record.duration_s,
                outputs=[str(p) for p in result.outputs],
                tokens_input=record.tokens_input,
                tokens_output=record.tokens_output,
                tokens_cache_read=record.tokens_cache_read,
                tokens_cache_write=record.tokens_cache_creation,
                agent_calls=record.agent_calls,
                # Surface error + notes so structured-log consumers can see
                # *why* a step failed without grepping the console transcript.
                # Run 20260614-190647-ab7dac: Step 7 failed twice with no
                # error info in run.log.jsonl because of this omission.
                error=result.error,
                notes=result.notes,
                **{f"step{self.number:02d}_total_cost_usd": record.cost_usd},
            )
            return result
        finally:
            CURRENT_STEP_METRICS.reset(token)


def _accumulate_metrics_into_record(
    record: StepRecord, accumulator: StepMetricsAccumulator
) -> None:
    """Add the attempt's metrics onto the step record's running totals.

    Cumulative across attempts so retry billing is visible to the user.
    """
    t = accumulator.totals
    record.tokens_input += t.input_tokens
    record.tokens_output += t.output_tokens
    record.tokens_cache_creation += t.cache_creation_input_tokens
    record.tokens_cache_read += t.cache_read_input_tokens
    record.cost_usd = round(record.cost_usd + t.cost_usd, 6)
    record.agent_calls += accumulator.agent_calls


async def _record_aux_agent(
    ctx: StepContext,
    step_num: int,
    phase: str,
    agent: str,
    coro: Awaitable[_T],
) -> _T:
    """Run a helper-agent coroutine (debug RCA / critical-thinking / PSE)
    under its own metrics accumulator and persist the spend as its OWN row
    in ``ctx.state.auxiliary_records`` — NOT folded into the parent step's
    ``StepRecord``.

    Before this split, ``_run_cost_tracked`` added the aux spend onto the
    parent step's cost cell, so a $3.75 Spec-Refinement failure showed as
    a monolithic $3.75 with no way to tell that $1.76 was the debug agent,
    $0.76 critical-thinking, and $0.62 principal-software-engineer. The
    numbers were only in raw log lines. Now each helper appears as its own
    row in the pipeline summary table (after Step 11) and totals still
    reconcile because the summary layer sums steps + aux.

    ``_attempt`` tears down ``CURRENT_STEP_METRICS`` in its ``finally``
    before returning to ``execute()``, so we set our own accumulator here;
    the runner writes into it while ``coro`` runs, and the totals are
    materialised into an ``AuxiliaryAgentRecord`` in the ``finally`` block
    — including on exception, so a failed agent still shows up as a row
    with ``status="failed"`` (its partial billing is still real spend).

    Also emits an ``aux_agent.recorded`` structured log event so the live
    desktop UI can pick up the delta — the UI only reacts to log events,
    not the final state file, and ``step.end`` was already emitted by
    ``_attempt`` before this chain runs.
    """
    accumulator = StepMetricsAccumulator()
    token = CURRENT_STEP_METRICS.set(accumulator)
    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    status = "completed"
    try:
        try:
            result = await coro
            return result
        except Exception:
            status = "failed"
            raise
    finally:
        CURRENT_STEP_METRICS.reset(token)
        duration = round(time.monotonic() - start, 3)
        t = accumulator.totals
        aux = AuxiliaryAgentRecord(
            step=step_num,
            agent=agent,
            phase=phase,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            duration_s=duration,
            tokens_input=t.input_tokens,
            tokens_output=t.output_tokens,
            tokens_cache_creation=t.cache_creation_input_tokens,
            tokens_cache_read=t.cache_read_input_tokens,
            cost_usd=round(t.cost_usd, 6),
            agent_calls=accumulator.agent_calls,
        )
        ctx.state.auxiliary_records.append(aux)
        log.info(
            "aux_agent.recorded",
            step=step_num,
            phase=phase,
            agent=agent,
            status=status,
            duration_s=duration,
            tokens_input=aux.tokens_input,
            tokens_output=aux.tokens_output,
            tokens_cache_read=aux.tokens_cache_read,
            tokens_cache_write=aux.tokens_cache_creation,
            agent_calls=aux.agent_calls,
            cost_usd=aux.cost_usd,
        )
