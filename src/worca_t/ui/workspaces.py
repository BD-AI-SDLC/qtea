"""Workspace discovery helpers for the UI resume flow.

Mirrors the listing logic in ``worca_t.cli.workspaces`` but returns plain
dicts the Flet config view can render into a dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from worca_t.checkpoints import load_state
from worca_t.config import get_settings


@dataclass(frozen=True)
class WorkspaceEntry:
    run_id: str
    status: str          # "running" | "finished" | "failed" | "empty" | "no-state"
    last_step: int | None
    step_count: int
    started_at: str | None
    spec_source: str | None
    sut_source: str | None


def list_workspaces(
    base: Path | None = None,
    *,
    include_empty: bool = False,
    limit: int = 50,
) -> list[WorkspaceEntry]:
    """Return runs under ``base`` (default: settings.default_workspace), newest first.

    Empty workspaces (zero completed steps) are filtered out unless
    ``include_empty=True``. Result is capped at ``limit`` entries — the
    dropdown gets unwieldy past ~50.
    """
    base = base or get_settings().default_workspace
    if not base.exists():
        return []

    candidates = sorted(
        (p for p in base.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    out: list[WorkspaceEntry] = []
    for ws_dir in candidates:
        state_file = ws_dir / "state.json"
        state = load_state(state_file) if state_file.exists() else None
        if state is None:
            entry = WorkspaceEntry(
                run_id=ws_dir.name,
                status="no-state",
                last_step=None,
                step_count=0,
                started_at=None,
                spec_source=None,
                sut_source=None,
            )
        else:
            completed = sorted(
                k for k, v in state.steps.items()
                if v.status in ("completed", "skipped")
            )
            last_step = completed[-1] if completed else None
            any_failed = any(v.status == "failed" for v in state.steps.values())
            if state.finished_at is None and state.steps:
                status = "running"
            elif any_failed:
                status = "failed"
            elif state.finished_at is not None:
                status = "finished"
            else:
                status = "empty"
            entry = WorkspaceEntry(
                run_id=state.run_id,
                status=status,
                last_step=last_step,
                step_count=len(state.steps),
                started_at=state.started_at,
                spec_source=state.spec_source,
                sut_source=state.sut_source,
            )

        if not include_empty and entry.last_step is None:
            continue
        out.append(entry)
        if len(out) >= limit:
            break

    return out


def get_workspace(run_id: str, base: Path | None = None) -> WorkspaceEntry | None:
    """Look up a specific workspace by run-id. Returns None if missing."""
    base = base or get_settings().default_workspace
    state_file = base / run_id / "state.json"
    if not state_file.exists():
        return None
    state = load_state(state_file)
    if state is None:
        return None
    completed = sorted(
        k for k, v in state.steps.items()
        if v.status in ("completed", "skipped")
    )
    last_step = completed[-1] if completed else None
    any_failed = any(v.status == "failed" for v in state.steps.values())
    if state.finished_at is None and state.steps:
        status = "running"
    elif any_failed:
        status = "failed"
    elif state.finished_at is not None:
        status = "finished"
    else:
        status = "empty"
    return WorkspaceEntry(
        run_id=state.run_id,
        status=status,
        last_step=last_step,
        step_count=len(state.steps),
        started_at=state.started_at,
        spec_source=state.spec_source,
        sut_source=state.sut_source,
    )
