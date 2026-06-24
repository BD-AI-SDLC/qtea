"""Event bridge: connects pipeline structlog events to UI state updates.

Two channels:
  1. A custom ``logging.Handler`` that intercepts structured events and updates
     ``AppState`` in real-time.
  2. HITL callback injection placeholder — the actual isatty patches live in
     the pipeline code (``ui_mode`` flag on ``PipelineOptions``).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import flet as ft

from worca_t.ui.state import AppState, LogLine

if TYPE_CHECKING:
    pass


class _UILogHandler(logging.Handler):
    """Intercept structlog records and push updates to AppState."""

    def __init__(self, state: AppState, page: ft.Page) -> None:
        super().__init__()
        self.state = state
        self.page = page
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

        elif event == "pipeline.finished":
            exit_code = event_dict.get("exit_code", 1)
            state.run_status = "completed" if exit_code == 0 else "failed"
            state.exit_code = exit_code
            state.update_elapsed()
            self._notify()

        elif event == "pipeline.aborted":
            state.run_status = "failed"
            state.exit_code = 2
            self._notify()

        # ── Step lifecycle ───────────────────────────────────────────────
        elif event == "step.start":
            step_num = event_dict.get("step")
            if step_num and step_num in state.steps:
                s = state.steps[step_num]
                s.status = "in_progress"
                s.attempts = event_dict.get("attempt", 1)
                s.started_at = time.monotonic()
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
                s.cache_write = event_dict.get("token_cache_write", s.cache_write)
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
        """Push state change + page update."""
        self.state.notify()
        try:
            self.page.update()
        except Exception:
            pass

    def _throttled_update(self) -> None:
        """Rate-limited page update for high-frequency log lines."""
        now = time.monotonic() * 1000
        if now - self._last_update > self._throttle_ms:
            self._last_update = now
            self.state.notify()
            try:
                self.page.update()
            except Exception:
                pass

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

        # Fall back: treat the formatted message as the event name.
        return {
            "event": str(record.msg),
            "level": record.levelname.lower(),
        }


class UIEventBridge:
    """Installs/uninstalls the UI log handler on the root logger."""

    def __init__(self, state: AppState, page: ft.Page) -> None:
        self.state = state
        self.page = page
        self._handler: _UILogHandler | None = None

    def install(self) -> None:
        self._handler = _UILogHandler(self.state, self.page)
        self._handler.setLevel(logging.DEBUG)
        # Tag so configure_logging() doesn't strip us when the pipeline
        # reconfigures structlog at startup.
        self._handler._worca_t_keep = True
        logging.getLogger().addHandler(self._handler)

    def uninstall(self) -> None:
        if self._handler:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
