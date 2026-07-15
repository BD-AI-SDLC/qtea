"""Central application state shared across all UI views and components."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Step definition table ────────────────────────────────────────────────────

STEP_DEFINITIONS: list[tuple[int, str, str]] = [
    (1, "Intake", "A"),
    (2, "Spec Refinement", "A"),
    (3, "Test Planning", "A"),
    (4, "Test Design", "A"),
    (5, "Xray Upload", "B"),
    (6, "Repo Discovery", "B"),
    (7, "Test Automation Architect", "B"),
    (8, "TDD Codegen", "B"),
    (9, "Execute + Heal", "C"),
    (10, "Bug Classification", "C"),
    (11, "Report", "C"),
]


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class LogLine:
    timestamp: str
    level: str
    event: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepUIState:
    number: int
    name: str
    phase: str
    status: str = "pending"
    started_at: float | None = None
    elapsed_s: float = 0.0
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    sub_status: str | None = None
    agent_calls: int = 0
    agent_name: str | None = None
    notes: str | None = None
    error: str | None = None


@dataclass
class AuxAgentUIState:
    """UI-side mirror of ``checkpoints.AuxiliaryAgentRecord``.

    One entry per helper agent (debug / critical-thinking /
    principal-software-engineer) that fires on retry exhaustion. Rendered
    in the results-view table after the 11 pipeline steps so the operator
    can see where the money went in a failed run instead of a black-box
    "step 2 cost = $3.75" that bundled all three helpers together.
    """

    step: int  # parent step
    phase: str  # "debug" | "critical_thinking" | "principal_engineer"
    agent: str
    status: str = "completed"
    duration_s: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    agent_calls: int = 0


@dataclass
class HitlRequest:
    """A pending human-in-the-loop request from the pipeline."""

    step: int
    agent_label: str
    questions: list[dict[str, Any]]
    answers: dict[str, Any] = field(default_factory=dict)
    completion_event: asyncio.Event | None = None


@dataclass
class ReviewGateRequest:
    """A pending review gate (steps 4, 7, 8)."""

    step: int
    title: str
    summary: str
    completion_event: asyncio.Event | None = None
    decision: str = ""  # "approve" | "edit" | "reject"
    edit_instructions: str = ""
    # Optional structured payload so the dialog can render a real Flet
    # table/list instead of dumping a monospace ASCII summary. ``kind`` is
    # one of: "strategy" (Step 4 test_cases), "plan" (Step 7 plan), or
    # "intents" (Step 8 warnings). When ``data`` is None or ``kind`` is
    # unknown, the dialog falls back to ``summary`` rendered in monospace.
    data: Any = None
    kind: str = ""


# ── Preferences persistence ─────────────────────────────────────────────────

_PREFS_FILE = Path.home() / ".qtea" / ".ui-prefs.json"


def _load_prefs() -> dict[str, Any]:
    if _PREFS_FILE.exists():
        try:
            return json.loads(_PREFS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_prefs(prefs: dict[str, Any]) -> None:
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


# ── Application state ────────────────────────────────────────────────────────


@dataclass
class AppState:
    """Single source of truth for the entire UI."""

    # ── Run configuration (populated from config_view form) ──────────────
    spec: str = ""
    sut: str = ""
    headless: bool = True
    parallel_run: int = 2
    report: str = "auto"
    cache: str = "auto"  # "auto" | "on" | "off"
    log_level: str = "info"
    skip_steps: set[int] = field(default_factory=set)
    storage_state: str = ""
    dev_locators: str = ""
    text_scale: float = 1.0

    # ── Resume from a prior workspace (UI mirror of CLI --run-id / --from-step) ──
    # Empty string + None ⇒ fresh run. When ``resume_run_id`` is set, ``from_step``
    # MUST also be set (and >= 1); the pipeline validator enforces "all prior steps
    # completed or skipped" and aborts otherwise. Not persisted in prefs — these
    # are per-run choices.
    resume_run_id: str = ""
    from_step: int | None = None

    # ── Runtime state (updated by event_bridge) ──────────────────────────
    run_status: str = "idle"  # idle | running | completed | failed
    run_id: str | None = None
    workspace_path: str | None = None
    current_step: int | None = None
    steps: dict[int, StepUIState] = field(default_factory=dict)
    # Helper agents (debug / critical-thinking / principal-engineer) that
    # fire on retry exhaustion. Appended in chronological order by the
    # event bridge; rendered as their own rows in the results view.
    auxiliary_records: list[AuxAgentUIState] = field(default_factory=list)
    pipeline_started_at: float | None = None
    total_cost: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_agent_calls: int = 0
    elapsed_s: float = 0.0
    log_lines: list[LogLine] = field(default_factory=list)

    # ── HITL ─────────────────────────────────────────────────────────────
    pending_hitl: HitlRequest | None = None
    pending_review_gate: ReviewGateRequest | None = None

    # ── Open-dialog tracking (Ctrl+/Ctrl- live-resizes the open popup) ────
    # HITL/review-gate dialogs already carry their own ``_dialog_open`` flag
    # on the request object, so only the step-details dialog (which has no
    # backing "pending" request) needs a field here.
    active_step_dialog_num: int | None = None

    # ── Results ──────────────────────────────────────────────────────────
    exit_code: int | None = None
    report_data: dict[str, Any] | None = None

    # ── Pipeline worker control (set by app.py when the run starts; used
    # by the Stop button to actually cancel the running pipeline rather
    # than just flipping UI state) ──────────────────────────────────────
    pipeline_loop: Any = None
    pipeline_task: Any = None
    cancel_requested: bool = False
    # Set True by the Stop button after it snap-navigates to /results, so
    # the pipeline worker's finally block does NOT run its own redundant
    # `_build_views_for_route("/results")` a moment later. Two full
    # page.views.clear()+rebuild sequences hitting the Flutter client in
    # rapid succession leave button widget ids orphaned — the summary
    # renders but every button is dead. Reset in `reset_run()`.
    results_navigated: bool = False
    # Child PIDs that existed BEFORE the pipeline started (notably the
    # flet-desktop Flutter GUI process, which is a child of this Python
    # process). The Stop button kills only children NOT in this set, so it
    # tears down pipeline subprocesses (pytest, npx/MCP, browsers, allure)
    # WITHOUT killing the GUI window — the old "kill all children" closed qtea.
    baseline_child_pids: set[int] = field(default_factory=set)

    # ── Stopwatch pause accounting (HITL waits should not count) ────────
    paused_total_s: float = 0.0
    pause_started_at: float | None = None

    # ── Observer callbacks ───────────────────────────────────────────────
    _listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False,
    )

    # ── Methods ──────────────────────────────────────────────────────────

    def init_steps(self) -> None:
        self.steps = {
            num: StepUIState(number=num, name=name, phase=phase)
            for num, name, phase in STEP_DEFINITIONS
        }

    def reset_run(self) -> None:
        self.run_status = "idle"
        self.run_id = None
        self.workspace_path = None
        self.current_step = None
        self.pipeline_started_at = None
        self.total_cost = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.total_agent_calls = 0
        self.elapsed_s = 0.0
        self.log_lines.clear()
        self.auxiliary_records.clear()
        self.pending_hitl = None
        self.pending_review_gate = None
        self.active_step_dialog_num = None
        self.exit_code = None
        self.report_data = None
        self.pipeline_loop = None
        self.pipeline_task = None
        self.cancel_requested = False
        self.results_navigated = False
        self.baseline_child_pids = set()
        self.paused_total_s = 0.0
        self.pause_started_at = None
        self.init_steps()

    def recalculate_totals(self) -> None:
        # Steps + aux — the aux rows are their own line items in the
        # results table, so the header banner's total must sum both or it
        # will disagree with the visible per-row cells.
        self.total_cost = sum(s.cost_usd for s in self.steps.values()) + sum(
            a.cost_usd for a in self.auxiliary_records
        )
        self.total_tokens_in = sum(s.tokens_in for s in self.steps.values()) + sum(
            a.tokens_in for a in self.auxiliary_records
        )
        self.total_tokens_out = sum(s.tokens_out for s in self.steps.values()) + sum(
            a.tokens_out for a in self.auxiliary_records
        )
        self.total_cache_read = sum(s.cache_read for s in self.steps.values()) + sum(
            a.cache_read for a in self.auxiliary_records
        )
        self.total_cache_write = sum(s.cache_write for s in self.steps.values()) + sum(
            a.cache_write for a in self.auxiliary_records
        )
        self.total_agent_calls = sum(
            s.agent_calls for s in self.steps.values()
        ) + sum(a.agent_calls for a in self.auxiliary_records)

    def _active_seconds_from(self, started_at: float) -> float:
        """Wall-clock seconds since ``started_at`` minus any paused windows."""
        now = time.monotonic()
        active = (now - started_at) - self.paused_total_s
        if self.pause_started_at is not None:
            active -= now - self.pause_started_at
        return max(0.0, active)

    def update_elapsed(self) -> None:
        if self.pipeline_started_at is not None:
            self.elapsed_s = self._active_seconds_from(self.pipeline_started_at)
        # Update elapsed for the currently running step (also pause-aware).
        if self.current_step and self.current_step in self.steps:
            s = self.steps[self.current_step]
            if s.started_at is not None and s.status == "in_progress":
                s.elapsed_s = self._active_seconds_from(s.started_at)

    def pause_clock(self) -> None:
        """Mark the start of a paused window (e.g. a HITL wait)."""
        if self.pause_started_at is None:
            self.pause_started_at = time.monotonic()

    def resume_clock(self) -> None:
        """End the current paused window and roll its duration into the total."""
        if self.pause_started_at is not None:
            self.paused_total_s += time.monotonic() - self.pause_started_at
            self.pause_started_at = None

    def completed_step_count(self) -> int:
        return sum(
            1
            for s in self.steps.values()
            if s.status in ("completed", "skipped", "warned")
        )

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def notify(self) -> None:
        for cb in self._listeners:
            cb()

    def save_prefs(self) -> None:
        _save_prefs(
            {
                "spec": self.spec,
                "sut": self.sut,
                "headless": self.headless,
                "parallel_run": self.parallel_run,
                "report": self.report,
                "cache": self.cache,
                "log_level": self.log_level,
                "skip_steps": sorted(self.skip_steps),
                "storage_state": self.storage_state,
                "dev_locators": self.dev_locators,
                "text_scale": self.text_scale,
            }
        )

    def load_prefs(self) -> None:
        prefs = _load_prefs()
        if not prefs:
            return
        self.spec = prefs.get("spec", self.spec)
        self.sut = prefs.get("sut", self.sut)
        self.headless = prefs.get("headless", self.headless)
        self.parallel_run = prefs.get("parallel_run", self.parallel_run)
        self.report = prefs.get("report", self.report)
        self.cache = prefs.get("cache", self.cache)
        self.log_level = prefs.get("log_level", self.log_level)
        raw_skip = prefs.get("skip_steps")
        if isinstance(raw_skip, list):
            self.skip_steps = {int(n) for n in raw_skip if isinstance(n, (int, str)) and str(n).isdigit()}
        self.storage_state = prefs.get("storage_state", self.storage_state)
        self.dev_locators = prefs.get("dev_locators", self.dev_locators)
        self.text_scale = prefs.get("text_scale", self.text_scale)
