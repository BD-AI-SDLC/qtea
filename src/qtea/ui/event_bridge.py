"""Event bridge: connects pipeline structlog events to UI state updates.

Two channels:
  1. A custom ``logging.Handler`` that intercepts structured events and updates
     ``AppState`` in real-time.
  2. HITL callback injection placeholder — the actual isatty patches live in
     the pipeline code (``ui_mode`` flag on ``PipelineOptions``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import flet as ft

from qtea.ui.state import AppState, AuxAgentUIState, LogLine

if TYPE_CHECKING:
    pass


class _UILogHandler(logging.Handler):
    """Intercept structlog records and push updates to AppState."""

    def __init__(
        self, state: AppState, page: ft.Page, loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self.state = state
        self.page = page
        # The pipeline runs on a worker thread (see app.py's _worker()); this
        # is the Flet event loop, captured on the main thread, so every
        # state.notify()/page.update() call below can be marshaled back onto
        # it via call_soon_threadsafe instead of running unsynchronized on
        # the worker thread — matching hitl_bridge.py / review_gate_bridge.py.
        # Without this, a page.update() landing concurrently with a main-
        # thread page.views.clear()+rebuild (as happens in _run_pipeline's
        # finally block) can corrupt Flet's rendered state, dropping the
        # summary view and leaving only the initial "/" view on screen.
        self._loop = loop
        self._last_update = 0.0
        self._throttle_ms = 250  # min ms between page.update() calls

    def emit(self, record: logging.LogRecord) -> None:
        # structlog records carry the event dict under `msg` (after formatting)
        # or as structured attributes when using ProcessorFormatter.
        # We try to extract the structured data.
        event_dict = self._extract_event_dict(record)
        event = event_dict.get("event", "")
        state = self.state

        # ── Pipeline lifecycle ───────────────────────────────────────────
        if event == "pipeline.start":
            state.run_status = "running"
            state.run_id = event_dict.get("run_id")
            state.workspace_path = event_dict.get("workspace")
            state.pipeline_started_at = time.monotonic()
            self._notify()

        elif event == "pipeline.end":
            # pipeline.py emits "pipeline.end" with a `status` field
            # ("ok"/"failed") — not "pipeline.finished" with an `exit_code`,
            # which this branch used to (incorrectly) match on and therefore
            # never fired. app.py's _run_pipeline sets state.run_status /
            # exit_code independently once the worker thread returns, so
            # this branch is a secondary path — kept for early UI feedback
            # (status badge) before that happens.
            status = event_dict.get("status", "failed")
            state.run_status = "completed" if status == "ok" else "failed"
            state.update_elapsed()
            self._notify()

        # ── Step lifecycle ───────────────────────────────────────────────
        elif event == "step.start":
            step_num = event_dict.get("step")
            if step_num and step_num in state.steps:
                s = state.steps[step_num]
                s.status = "in_progress"
                s.attempts = event_dict.get("attempt", 1)
                s.started_at = time.monotonic()
                # Snapshot cumulative pause time so far — see
                # AppState._active_seconds_from() for why the per-step clock
                # must not re-subtract pauses that happened before this step
                # started (e.g. an earlier step's HITL/review-gate wait).
                s.paused_at_start = state.paused_total_s
                state.current_step = step_num
                self._notify()

        elif event == "step.end":
            step_num = event_dict.get("step")
            if step_num and step_num in state.steps:
                s = state.steps[step_num]
                s.status = event_dict.get("status", "completed")
                s.elapsed_s = event_dict.get("duration_s", s.elapsed_s)
                s.tokens_in = event_dict.get("tokens_input", s.tokens_in)
                s.tokens_out = event_dict.get("tokens_output", s.tokens_out)
                s.cache_read = event_dict.get("tokens_cache_read", s.cache_read)
                s.cache_write = event_dict.get("tokens_cache_write", s.cache_write)
                # steps/base.py emits cost under a step-numbered key
                # (e.g. ``step03_total_cost_usd``) — not a plain
                # ``cost_usd``. Look up by the correct key, otherwise the
                # value stays at 0 forever and the sidebar always shows
                # $0.00 even when tokens have clearly been spent.
                cost_key = f"step{int(step_num):02d}_total_cost_usd"
                s.cost_usd = event_dict.get(
                    cost_key, event_dict.get("cost_usd", s.cost_usd)
                )
                s.agent_calls = event_dict.get("agent_calls", s.agent_calls)
                s.sub_status = event_dict.get("sub_status", s.sub_status)
                s.notes = event_dict.get("notes", s.notes)
                s.error = event_dict.get("error", s.error)
                state.recalculate_totals()
                self._notify()

        elif event == "aux_agent.recorded":
            # Emitted by steps/base.py's `_record_aux_agent` when a helper
            # agent (debug / critical-thinking / principal-engineer) fires
            # on retry exhaustion. That chain runs strictly after
            # `step.end` (see base.py's `execute()`), so without this
            # event the sidebar would never reflect its billed cost — the
            # UI only reacts to structured log events, not the final state
            # file.
            #
            # Aux costs are NO LONGER folded into the parent step's cost
            # cell (unlike the pre-split `step.debug_fix_cost` event this
            # replaced) — updating the step would double-count against the
            # aux row that will render in results_view.
            step_num = event_dict.get("step")
            phase = event_dict.get("phase", "")
            agent = event_dict.get("agent", "")
            if step_num is not None:
                aux = AuxAgentUIState(
                    step=int(step_num),
                    phase=str(phase),
                    agent=str(agent),
                    status=str(event_dict.get("status", "completed")),
                    duration_s=event_dict.get("duration_s"),
                    tokens_in=int(event_dict.get("tokens_input", 0) or 0),
                    tokens_out=int(event_dict.get("tokens_output", 0) or 0),
                    cache_read=int(event_dict.get("tokens_cache_read", 0) or 0),
                    cache_write=int(event_dict.get("tokens_cache_write", 0) or 0),
                    cost_usd=float(event_dict.get("cost_usd", 0.0) or 0.0),
                    agent_calls=int(event_dict.get("agent_calls", 0) or 0),
                )
                state.auxiliary_records.append(aux)
                state.recalculate_totals()
                self._notify()

        elif event == "step.retry":
            step_num = event_dict.get("step")
            if step_num and step_num in state.steps:
                s = state.steps[step_num]
                s.attempts = event_dict.get("attempt", s.attempts)
                self._notify()

        # ── Agent lifecycle ──────────────────────────────────────────────
        elif event == "agent.start":
            agent_name = event_dict.get("agent", "")
            if state.current_step and state.current_step in state.steps:
                state.steps[state.current_step].agent_name = agent_name
                self._notify()

        elif event == "agent.end":
            if state.current_step and state.current_step in state.steps:
                state.steps[state.current_step].agent_name = None

        # ── Always append to log viewer ──────────────────────────────────
        ts = event_dict.get("timestamp", "")
        level = event_dict.get("level", record.levelname.lower())
        msg_parts = []
        skip_keys = {"event", "timestamp", "level", "run_id"}
        for k, v in event_dict.items():
            if k not in skip_keys and v is not None and v is not False:
                msg_parts.append(f"{k}={v}")
        message = ", ".join(msg_parts)

        state.log_lines.append(
            LogLine(
                timestamp=str(ts),
                level=level,
                event=event,
                message=message,
                fields=event_dict,
            )
        )

        if len(state.log_lines) > 2000:
            state.log_lines[:] = state.log_lines[-1500:]

        self._throttled_update()

    def _notify(self) -> None:
        """Schedule state change + page update on the Flet event loop
        (never call it directly — this handler runs on the pipeline's
        worker thread)."""
        self._loop.call_soon_threadsafe(self._do_notify)

    def _do_notify(self) -> None:
        # Once Stop is requested, the UI has already snap-navigated to
        # /results and cleared state listeners. The cancelled task still
        # unwinds through a burst of step.end/log events, each scheduled
        # here; a page.update() from that burst landing while
        # navigate_to("/results") is mid clear-and-rebuild corrupts the
        # Flutter tree (SESSION_CRASHED -> reconnect flicker). Go silent the
        # instant Stop fires — reset_run() clears cancel_requested for the
        # next run.
        if self.state.cancel_requested:
            return
        self.state.notify()
        with contextlib.suppress(Exception):
            self.page.update()

    def _throttled_update(self) -> None:
        """Rate-limited page update for high-frequency log lines."""
        now = time.monotonic() * 1000
        if now - self._last_update > self._throttle_ms:
            self._last_update = now
            self._notify()

    def _extract_event_dict(self, record: logging.LogRecord) -> dict[str, Any]:
        """Extract the structured event dict from a structlog LogRecord.

        With structlog + ProcessorFormatter the pre-rendered event dict is
        stored directly on ``record.msg`` (as a dict). Older configurations
        used ``record._event_dict`` or ``record.args[0]``.
        """
        if isinstance(record.msg, dict):
            return dict(record.msg)

        if hasattr(record, "_event_dict"):
            return dict(record._event_dict)

        if record.args and isinstance(record.args, tuple) and isinstance(record.args[0], dict):
            return dict(record.args[0])

        # Fall back: foreign (stdlib) records — e.g. httpx's
        # ``log.info("HTTP Request: %s %s ...", method, url, ...)`` — arrive
        # with the ``%s`` format string on ``record.msg`` and the values in
        # ``record.args``, UN-interpolated. ``getMessage()`` performs the
        # deferred ``msg % args`` so the log line shows the real values
        # instead of literal ``%s`` placeholders.
        try:
            event = record.getMessage()
        except Exception:
            event = str(record.msg)
        return {
            "event": event,
            "level": record.levelname.lower(),
        }


class UIEventBridge:
    """Installs/uninstalls the UI log handler on the root logger."""

    def __init__(
        self, state: AppState, page: ft.Page, loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.state = state
        self.page = page
        self._loop = loop
        self._handler: _UILogHandler | None = None

    def install(self) -> None:
        self._handler = _UILogHandler(self.state, self.page, self._loop)
        self._handler.setLevel(logging.DEBUG)
        # Tag so configure_logging() doesn't strip us when the pipeline
        # reconfigures structlog at startup.
        self._handler._qtea_keep = True
        logging.getLogger().addHandler(self._handler)

    def uninstall(self) -> None:
        if self._handler:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
