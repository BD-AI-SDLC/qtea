"""Step base class: shared contract for every pipeline step."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Console
from rich.prompt import Prompt

from worca_t.checkpoints import RunState, StepRecord, hash_paths
from worca_t.claude_runner import AgentResult, run_agent
from worca_t.config import package_resource_root
from worca_t.hitl import (
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
from worca_t.logging_setup import get_logger
from worca_t.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator
from worca_t.workspace import Workspace

log = get_logger(__name__)

MAX_ATTEMPTS = 2
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


def _should_run_debug_rca(ctx: StepContext, has_more_attempts: bool) -> bool:
    """Gate the debug-RCA invocation.

    - Always fires on a FINAL failure (last attempt or no retry available):
      gives the user a diagnosis artifact and supplies the ``--fix`` flow
      with structured RCA input.
    - On non-final failures (the retry will run regardless), only fires when
      ``--debug`` is set. Keeps token cost at zero for the common case where
      attempt 2 succeeds and the user never needs the intermediate RCA.
    """
    if not has_more_attempts:
        return True
    return bool(getattr(ctx.options, "debug", False))


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

    # Grant read access to the failing step's workdir (transcripts, stderr,
    # metrics under <workdir>/logs/) without copying. The agent decides
    # what to ingest based on what its agent.md prompt tells it to read.
    step_workdir = ctx.workspace.step_workdir(step_num)
    add_dirs = [step_workdir] if step_workdir.exists() else None

    out_path = ctx.workspace.debug / f"step-{step_num:02d}-attempt{attempt}-debug-rca.md"

    try:
        result = await run_agent(
            agent,
            workdir=rca_workdir,
            inputs={"failure-context.md": context_file},
            user_prompt=(
                f"Step {step_num} attempt {attempt} failed. Read "
                f"`./failure-context.md` plus any transcripts under "
                f"`{step_workdir / 'logs'}/` you need. Produce a structured "
                f"root-cause analysis at `./debug-rca.md` following the "
                f"Phase 1-3 protocol in your agent.md. Diagnosis only — "
                f"do NOT edit source, fixtures, or env."
            ),
            add_dirs=add_dirs,
            timeout_s=300,
            max_turns=10,
        )
    except Exception as e:
        log.warning("debug.rca_failed", step=step_num, error=str(e))
        return None

    produced = rca_workdir / "debug-rca.md"
    if result.success and produced.exists():
        shutil.copy2(produced, out_path)
        log.info(
            "debug.rca_written",
            step=step_num,
            attempt=attempt,
            path=str(out_path),
        )
        return out_path
    if result.final_text:
        out_path.write_text(result.final_text, encoding="utf-8")
        log.info(
            "debug.rca_final_text",
            step=step_num,
            attempt=attempt,
            path=str(out_path),
        )
        return out_path
    log.warning(
        "debug.rca_empty",
        step=step_num,
        attempt=attempt,
        agent_error=result.error,
    )
    return None


async def _run_fix_proposal(step_num: int, ctx: StepContext, failure_context: str) -> Path | None:
    agents_root = package_resource_root() / "agents"
    fix_workdir = ctx.workspace.debug / f"step-{step_num:02d}-fix"
    fix_workdir.mkdir(parents=True, exist_ok=True)

    context_file = fix_workdir / "failure-context.md"
    context_file.write_text(failure_context, encoding="utf-8")

    rca_agent = agents_root / "critical-thinking.agent.md"
    rca_text = ""
    if rca_agent.exists():
        rca_workdir = fix_workdir / "rca"
        rca_workdir.mkdir(parents=True, exist_ok=True)
        try:
            rca_result = await run_agent(
                rca_agent,
                workdir=rca_workdir,
                inputs={"failure-context.md": context_file},
                user_prompt=(
                    "Analyze the following test/step failure. "
                    "Identify the root cause and challenge assumptions. "
                    "Write your analysis to ./rca.md"
                ),
                timeout_s=300,
                max_turns=10,
            )
            rca_file = rca_workdir / "rca.md"
            if rca_result.success and rca_file.exists():
                rca_text = rca_file.read_text(encoding="utf-8")
            elif rca_result.final_text:
                rca_text = rca_result.final_text
        except Exception as e:
            log.warning("fix.rca_failed", step=step_num, error=str(e))
            rca_text = f"RCA agent failed: {e}\n\nOriginal failure:\n{failure_context}"

    if not rca_text:
        rca_text = failure_context

    rca_output = ctx.workspace.debug / f"step-{step_num:02d}-rca.md"
    rca_output.write_text(rca_text, encoding="utf-8")

    fix_agent = agents_root / "principal-software-engineer.agent.md"
    proposal_path = ctx.workspace.debug / f"step-{step_num:02d}-fix-proposal.md"

    if fix_agent.exists():
        eng_workdir = fix_workdir / "eng"
        eng_workdir.mkdir(parents=True, exist_ok=True)

        rca_staged = eng_workdir / "rca.md"
        rca_staged.write_text(rca_text, encoding="utf-8")

        try:
            eng_result = await run_agent(
                fix_agent,
                workdir=eng_workdir,
                inputs={"rca.md": rca_staged},
                user_prompt=(
                    "Based on the root cause analysis in ./rca.md, "
                    "propose a fix. Write your proposal to ./fix-proposal.md. "
                    "Do NOT edit any source code directly."
                ),
                timeout_s=300,
                max_turns=10,
            )
            produced = eng_workdir / "fix-proposal.md"
            if eng_result.success and produced.exists():
                shutil.copy2(produced, proposal_path)
            elif eng_result.final_text:
                proposal_path.write_text(eng_result.final_text, encoding="utf-8")
            else:
                proposal_path.write_text(
                    f"# Fix Proposal (auto-generated)\n\n"
                    f"Engineering agent did not produce a proposal.\n\n"
                    f"## RCA\n\n{rca_text}",
                    encoding="utf-8",
                )
        except Exception as e:
            log.warning("fix.eng_failed", step=step_num, error=str(e))
            proposal_path.write_text(
                f"# Fix Proposal (auto-generated)\n\n"
                f"Engineering agent failed: {e}\n\n## RCA\n\n{rca_text}",
                encoding="utf-8",
            )
    else:
        proposal_path.write_text(
            f"# Fix Proposal (auto-generated)\n\n"
            f"No engineering agent available.\n\n## RCA\n\n{rca_text}",
            encoding="utf-8",
        )

    log.info("fix.proposal_written", step=step_num, path=str(proposal_path))
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

        if new_questions:
            answers = prompt_user(new_questions, agent_label=agent_label)
        else:
            answers = {}
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
        # `call_reasoning_llm_with_hitl` (src/worca_t/llm/reasoning.py) —
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
        if not result.success and _should_run_debug_rca(
            ctx, has_more_attempts=record.attempts < MAX_ATTEMPTS
        ):
            fc = _build_failure_context(self.number, self.name, record, result)
            rca = await _run_debug_rca(
                self.number, ctx, fc, attempt=record.attempts
            )
            if rca:
                ctx.extras[f"step{self.number}_rca_path"] = str(rca)

        if not result.success and record.attempts < MAX_ATTEMPTS:
            # Classify the failure for audit + hint propagation. The
            # category is logged for the audit trail; when the classifier
            # returns a fix_hint dict it gets merged into ctx.extras so
            # the next attempt's run() can pick it up (e.g. a prompt
            # clarification for schema violations). Pure-Python; no LLM
            # call. See worca_t/failure_classifiers.py for category list.
            from worca_t.failure_classifiers import classify_failure
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
            # Stash the classification for downstream consumers (pipeline.py
            # uses it to gate the fix-proposal chain on the final failure).
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
                and sys.stdin.isatty()
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
            if not result.success and _should_run_debug_rca(
                ctx, has_more_attempts=False
            ):
                fc = _build_failure_context(self.number, self.name, record, result)
                rca = await _run_debug_rca(
                    self.number, ctx, fc, attempt=record.attempts
                )
                if rca:
                    ctx.extras[f"step{self.number}_rca_path"] = str(rca)

            if result.success and result.status not in ("skipped",):
                result.status = "warned"
                record.status = "warned"
                record.notes = f"succeeded on retry (attempt {record.attempts})"

        if not result.success and getattr(ctx.options, "fix", False):
            failure_context = _build_failure_context(
                self.number, self.name, record, result
            )
            await _run_fix_proposal(self.number, ctx, failure_context)

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
                record.finished_at = datetime.now(UTC).isoformat()
                record.duration_s = round(duration, 3)
                record.notes = f"unhandled exception: {e}"
                _accumulate_metrics_into_record(record, accumulator)
                log.exception("step.exception", step=self.number, error=str(e))
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
