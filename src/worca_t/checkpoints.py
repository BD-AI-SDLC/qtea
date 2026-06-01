"""Checkpoint state machine - resume from last successful step."""

from __future__ import annotations

import asyncio
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
    timed_out: bool = False
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
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "spec_source": self.spec_source,
            "sut_source": self.sut_source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": {str(k): asdict(v) for k, v in self.steps.items()},
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
        for k, v in (d.get("steps") or {}).items():
            rs.steps[int(k)] = StepRecord(**v)
        return rs


_state_lock: asyncio.Lock | None = None


def get_state_lock() -> asyncio.Lock:
    global _state_lock
    if _state_lock is None:
        _state_lock = asyncio.Lock()
    return _state_lock


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


async def save_state_async(state: RunState, path: Path) -> None:
    """Serialize checkpoint writes when steps run concurrently."""
    async with get_state_lock():
        save_state(state, path)


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
    rec = state.steps.get(step)
    return bool(rec and rec.status in ("completed", "skipped"))


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
