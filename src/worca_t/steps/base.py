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
from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root
from worca_t.logging_setup import get_logger
from worca_t.workspace import Workspace

log = get_logger(__name__)

MAX_ATTEMPTS = 2


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


def _run_fix_proposal(step_num: int, ctx: StepContext, failure_context: str) -> Path | None:
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
            rca_result = run_agent(
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
            eng_result = run_agent(
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
    def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover (abstract)
        ...

    def out_dir(self, ws: Workspace) -> Path:
        return ws.step_dir(self.number)

    def workdir(self, ws: Workspace) -> Path:
        return ws.step_workdir(self.number)

    def execute(self, ctx: StepContext) -> StepResult:
        """Wraps `run()` with timing, state-record updates, retry, and fix-proposal."""
        # Each attempt below re-invokes self.run(ctx) from scratch. run() is
        # responsible for repopulating the workdir from external sources on
        # every call; nothing in agent-work/stepNN/ survives semantically
        # between attempts. See the Step docstring "Workdir contract" for
        # the full invariant.
        record = ctx.state.steps.get(self.number) or StepRecord(step=self.number, name=self.name)
        ctx.state.steps[self.number] = record

        result = self._attempt(ctx, record)

        if not result.success and record.attempts < MAX_ATTEMPTS:
            _snapshot_debug_artifacts(self.number, ctx, record.attempts)
            ctx.extras["debug_live"] = True
            log.info("step.retry", step=self.number, name=self.name)
            result = self._attempt(ctx, record)

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
            _run_fix_proposal(self.number, ctx, failure_context)

        return result

    def _attempt(self, ctx: StepContext, record: StepRecord) -> StepResult:
        record.attempts += 1
        record.status = "in_progress"
        record.started_at = datetime.now(UTC).isoformat()

        log.info("step.start", step=self.number, name=self.name, attempt=record.attempts)
        started = time.monotonic()
        try:
            result = self.run(ctx)
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
