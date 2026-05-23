"""Workspace + artifact path layout."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def generate_run_id() -> str:
    """Sortable, human-friendly run id: YYYYMMDD-HHMMSS-<6hex>."""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Workspace:
    """All canonical paths for a single run."""

    root: Path  # ./.worca-t/<run-id>
    run_id: str

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def debug(self) -> Path:
        return self.root / "debug"

    @property
    def sut(self) -> Path:
        return self.root / "sut"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def run_log(self) -> Path:
        return self.root / "run.log.jsonl"

    @property
    def doctor_report(self) -> Path:
        return self.root / "doctor-report.md"

    def step_dir(self, step: int) -> Path:
        return self.artifacts / f"step{step:02d}"

    def step_workdir(self, step: int) -> Path:
        return self.root / f"step-{step:02d}"

    def ensure_layout(self) -> None:
        for p in (self.artifacts, self.debug, self.sut):
            p.mkdir(parents=True, exist_ok=True)
        for i in range(1, 12):
            self.step_dir(i).mkdir(parents=True, exist_ok=True)


def create_workspace(base: Path | None = None, run_id: str | None = None) -> Workspace:
    """Create (or reuse) a workspace under <base>/<run-id>."""
    base = base or Path(os.environ.get("WORCA_T_DEFAULT_WORKSPACE", str(Path.home() / ".worca-t")))
    rid = run_id or generate_run_id()
    root = (base / rid).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root=root, run_id=rid)
    ws.ensure_layout()
    return ws


def find_latest_workspace(base: Path | None = None) -> Workspace | None:
    """Find most recently modified workspace under base (for resume)."""
    base = base or Path(os.environ.get("WORCA_T_DEFAULT_WORKSPACE", str(Path.home() / ".worca-t")))
    if not base.exists():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return Workspace(root=latest.resolve(), run_id=latest.name)
