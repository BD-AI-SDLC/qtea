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
    extract_questions,
    prompt_user,
    question_key,
    write_answers_file,
)
from worca_t.logging_setup import get_logger
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
    for name in ("transcript.jsonl", "stderr.log", "metrics.json"):
        src = workdir / name
        if src.exists():
            shutil.copy2(src, dst / name)


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

    hitl_disabled = getattr(ctx.options, "no_hitl", False)
    hitl_dir = (
        workdir.parent / f".hitl-step{step:02d}" if step else workdir.parent / ".hitl"
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
        )

        produced = workdir / output_filename
        if not result.success or not produced.exists():
            return result

        if hitl_disabled:
            return result

        md_text = produced.read_text(encoding="utf-8")
        all_questions = extract_questions(md_text)
        # Don't re-ask questions the user already chose to skip — the agent is
        # expected to have replaced them with `[ASSUMPTION]` notes. If it didn't,
        # we still won't re-prompt; we just stop the loop to avoid annoyance.
        new_questions = [q for q in all_questions if question_key(q) not in skipped_keys]

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

        # Always re-invoke so the agent can either incorporate answers OR
        # convert skipped clarifications into `[ASSUMPTION]` notes. Skipping
        # the re-run would leave `[CLARIFICATION NEEDED]` tags in the output.
        hitl_dir.mkdir(parents=True, exist_ok=True)
        answers_src = write_answers_file(
            hitl_dir, new_questions, answers, skipped=skipped_this_round
        )
        current_inputs = dict(inputs)
        current_inputs["user-answers.md"] = answers_src
        iteration_prompt = (
            f"{user_prompt}\n\n"
            f"The user has reviewed your clarification questions. See "
            f"`./user-answers.md` for their responses, which include both "
            f"answered items and items the user chose to skip.\n\n"
            f"- For ANSWERED items: incorporate the answer and remove the "
            f"corresponding `[CLARIFICATION NEEDED]` tag, blocker row, or "
            f"open-question entry.\n"
            f"- For SKIPPED items: make a reasonable assumption, mark it "
            f"inline with `[ASSUMPTION: ...]`, and remove the original "
            f"`[CLARIFICATION NEEDED]` tag / blocker row / open-question "
            f"entry. **Do NOT re-emit `[CLARIFICATION NEEDED]` for skipped "
            f"items** — the user has explicitly opted to defer them.\n\n"
            f"Rewrite `./{output_filename}` accordingly. Keep the rest of "
            f"the document intact."
        )

    return result  # pragma: no cover (loop always returns inside)


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
        started = time.monotonic()
        try:
            result = await self.run(ctx)
        except Exception as e:
            duration = time.monotonic() - started
            record.status = "failed"
            record.finished_at = datetime.now(UTC).isoformat()
            record.duration_s = round(duration, 3)
            record.notes = f"unhandled exception: {e}"
            log.exception("step.exception", step=self.number, error=str(e))
            return StepResult(success=False, status="failed", outputs=[], error=str(e))

        duration = time.monotonic() - started
        record.status = result.status
        record.finished_at = datetime.now(UTC).isoformat()
        record.duration_s = round(duration, 3)
        record.notes = result.notes
        record.output_hashes = hash_paths(result.outputs)
        log.info(
            "step.end",
            step=self.number,
            name=self.name,
            status=result.status,
            duration_s=record.duration_s,
            outputs=[str(p) for p in result.outputs],
        )
        return result
