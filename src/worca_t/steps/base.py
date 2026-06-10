"""Step base class: shared contract for every pipeline step."""

from __future__ import annotations

import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worca_t.checkpoints import RunState, StepRecord, hash_paths
from worca_t.claude_runner import AgentResult, run_agent
from worca_t.config import package_resource_root
from worca_t.hitl import (
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


def _snapshot_debug_artifacts(step_num: int, ctx: StepContext, attempt: int) -> None:
    dst = ctx.workspace.debug / f"step-{step_num:02d}-attempt{attempt}"
    dst.mkdir(parents=True, exist_ok=True)

    workdir = ctx.workspace.step_workdir(step_num)
    if not workdir.exists():
        return
    # Glob covers both the new per-call numbered names
    # (transcript-00.jsonl, transcript-01.jsonl, ...) and the legacy
    # single-file names from older runs (transcript.jsonl, etc.).
    for pattern in ("transcript*.jsonl", "stderr*.log", "metrics*.json"):
        for src in workdir.glob(pattern):
            shutil.copy2(src, dst / src.name)


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
) -> AgentResult:
    """Run an agent; if its output contains unresolved questions, prompt user
    and re-invoke with answers staged as ``user-answers.md``. Loops until the
    output is clean or ``max_iterations`` is reached.

    Skips the prompt loop when ``--no-hitl`` is set or stdin is not a TTY.
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
            f"or open questions. For skipped items, apply the same "
            f"`[ASSUMPTION]` framing the earlier agent used."
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
        skipped_this_round = [q for q in new_questions if q.id not in answers]
        for q in skipped_this_round:
            skipped_keys.add(question_key(q))
            step_decisions.setdefault(
                question_key(q),
                HitlDecision.from_question(
                    q, step=step or 0, agent_label=agent_label, resolution="skipped"
                ),
            )
        for q in new_questions:
            if q.id in answers:
                step_decisions[question_key(q)] = HitlDecision.from_question(
                    q,
                    step=step or 0,
                    agent_label=agent_label,
                    resolution="answered",
                    answer=answers[q.id],
                )

        # Always re-invoke so the agent can either incorporate answers OR
        # convert skipped clarifications into `[ASSUMPTION]` notes. Skipping
        # the re-run would leave `[CLARIFICATION NEEDED]` tags in the output.
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
            f"answered items, items the user chose to skip, and items "
            f"that were already resolved earlier in this run.\n\n"
            f"- For ANSWERED items: incorporate the answer and remove the "
            f"corresponding `[CLARIFICATION NEEDED]` tag, blocker row, or "
            f"open-question entry.\n"
            f"- For SKIPPED items: make a reasonable assumption, mark it "
            f"inline with `[ASSUMPTION: ...]`, and remove the original "
            f"`[CLARIFICATION NEEDED]` tag / blocker row / open-question "
            f"entry. **Do NOT re-emit `[CLARIFICATION NEEDED]` for skipped "
            f"items** — the user has explicitly opted to defer them.\n"
            f"- For PREVIOUSLY RESOLVED items: apply the prior answer / "
            f"assumption verbatim and remove the duplicate entry. **Do "
            f"NOT re-raise these to the user.**\n\n"
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

        if not result.success and record.attempts < MAX_ATTEMPTS:
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

            if result.success and result.status not in ("skipped",):
                result.status = "warned"
                record.status = "warned"
                record.notes = f"succeeded on retry (attempt {record.attempts})"

        if not result.success and getattr(ctx.options, "fix", False):
            failure_context = (
                f"# Step {self.number} ({self.name}) failure\n\n"
                f"**Attempts:** {record.attempts}\n"
                f"**Error:** {result.error or 'unknown'}\n"
                f"**Notes:** {result.notes or 'none'}\n"
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
                duration_s=record.duration_s,
                outputs=[str(p) for p in result.outputs],
                tokens_input=record.tokens_input,
                tokens_output=record.tokens_output,
                cost_usd=record.cost_usd,
                agent_calls=record.agent_calls,
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
