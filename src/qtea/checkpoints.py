"""Checkpoint state machine - resume from last successful step."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

StepStatus = Literal["pending", "in_progress", "completed", "skipped", "failed", "warned"]


@dataclass
class StepRecord:
    step: int
    name: str
    status: StepStatus = "pending"
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    output_hashes: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    sub_status: str | None = None
    timed_out: bool = False
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_creation: int = 0
    tokens_cache_read: int = 0
    cost_usd: float = 0.0
    agent_calls: int = 0


@dataclass
class AuxiliaryAgentRecord:
    """Per-agent billing row for the debug/fix-proposal chain.

    Kept OUTSIDE ``StepRecord`` so the parent step's cost cell shows only
    the cost of its own attempts — the debug/critical-thinking/PSE agents
    that fire on retry exhaustion are surfaced as their own rows in the
    pipeline summary table (after Step 11). This is what makes it possible
    to actually read where the money went in a failed run.
    """

    step: int
    agent: str
    phase: str  # "debug" | "critical_thinking" | "principal_engineer"
    status: str = "completed"
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_creation: int = 0
    tokens_cache_read: int = 0
    cost_usd: float = 0.0
    agent_calls: int = 0


@dataclass
class RunState:
    run_id: str
    workspace: str
    spec_source: str | None
    sut_source: str | None
    steps: dict[int, StepRecord] = field(default_factory=dict)
    auxiliary_records: list[AuxiliaryAgentRecord] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    # PID (+ process create-time) of the qtea process that owns this run.
    # Lets a reader tell a live run from one that died without cleanup; the
    # create-time guards against PID reuse. See ``process_alive``.
    pid: int | None = None
    pid_create_time: float | None = None
    # Why the run ended, when we caught it: "interrupted" | "crashed".
    # None means either a clean exit or a hard-kill we never observed.
    end_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "spec_source": self.spec_source,
            "sut_source": self.sut_source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "pid_create_time": self.pid_create_time,
            "end_reason": self.end_reason,
            "steps": {str(k): asdict(v) for k, v in self.steps.items()},
            "auxiliary_records": [asdict(a) for a in self.auxiliary_records],
        }

    @classmethod
    def from_dict(cls, d: dict) -> RunState:
        rs = cls(
            run_id=d["run_id"],
            workspace=d["workspace"],
            spec_source=d.get("spec_source"),
            sut_source=d.get("sut_source"),
        )
        rs.started_at = d.get("started_at", rs.started_at)
        rs.finished_at = d.get("finished_at")
        rs.pid = d.get("pid")
        rs.pid_create_time = d.get("pid_create_time")
        rs.end_reason = d.get("end_reason")
        for k, v in (d.get("steps") or {}).items():
            rs.steps[int(k)] = StepRecord(**v)
        # Missing key = pre-aux-tracking workspace; empty list is the right
        # default. Guard against `null` too (some writers emit it).
        for a in (d.get("auxiliary_records") or []):
            rs.auxiliary_records.append(AuxiliaryAgentRecord(**a))
        return rs


def save_state(state: RunState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> RunState | None:
    if not path.exists():
        return None
    try:
        return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        structlog.get_logger(__name__).warning(
            "checkpoint.load_failed", path=str(path), error=str(e),
        )
        return None


def hash_file(p: Path, chunk: int = 65536) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def hash_paths(paths: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        if p.exists() and p.is_file():
            out[p.name] = hash_file(p)
    return out


def is_step_complete(state: RunState, step: int) -> bool:
    # "warned" = the step FAILED attempt 1 but PASSED on retry (base.py
    # downgrades a recovered failure to "warned"). It is a successful,
    # complete step and must be skipped on resume — excluding it (finding 18)
    # needlessly re-ran already-green steps (regenerating code, burning LLM
    # budget) and, combined with the SUT re-materialize, participated in the
    # deliverable-wipe. This matches _validate_resume_prerequisites and the
    # SUT-reuse guard, which already treat "warned" as done.
    rec = state.steps.get(step)
    return bool(rec and rec.status in ("completed", "skipped", "warned"))


def outputs_match(state: RunState, step: int, step_dir: Path) -> bool:
    """Check if a completed step's output files still match their recorded hashes."""
    rec = state.steps.get(step)
    if not rec or not rec.output_hashes:
        return True
    current = hash_paths([step_dir / name for name in rec.output_hashes])
    return current == rec.output_hashes


def next_pending_step(state: RunState, total: int = 11) -> int:
    for i in range(1, total + 1):
        if not is_step_complete(state, i):
            return i
    return total + 1  # all done


def process_alive(pid: int | None, create_time: float | None = None) -> bool:
    """True iff ``pid`` names a live process that is the one we recorded.

    ``create_time`` is the process start time captured at run start; when
    provided it guards against PID reuse (the OS may hand the same PID to an
    unrelated process after ours dies). A mismatch — or a missing/dead PID —
    is treated as not-alive.
    """
    if pid is None:
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        if create_time is not None and proc.create_time() != create_time:
            return False
        return True
    except Exception:
        # NoSuchProcess / AccessDenied / psutil unavailable -> treat as dead.
        return False


def derive_status(state: RunState) -> str:
    """Single source of truth for a run's coarse status.

    Returns one of: ``running`` (process actually alive), ``finished``,
    ``failed``, ``interrupted`` (Ctrl-C / UI Stop), ``crashed`` (uncaught
    exception), ``aborted`` (PID dead with no clean exit — hard-kill, power
    loss, etc.), or ``empty`` (no steps recorded yet).
    """
    if state.finished_at is not None:
        if state.end_reason in ("interrupted", "crashed"):
            return state.end_reason
        if any(s.status == "failed" for s in state.steps.values()):
            return "failed"
        return "finished"
    if not state.steps:
        return "empty"
    # No finished_at and steps exist: either genuinely running or died without
    # cleanup. The PID tells us which. NOTE: a run started before pid tracking
    # existed has no pid and will read "aborted" even if still alive — a
    # one-time transitional edge; all new runs record a pid.
    if process_alive(state.pid, state.pid_create_time):
        return "running"
    return "aborted"
