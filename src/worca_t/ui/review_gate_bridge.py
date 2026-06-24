"""Review-gate bridge: route ``review_gate.review_step_*`` prompts into UI dialogs.

The pipeline's review gates (Steps 4, 7, 8) historically used
``rich.prompt.Prompt.ask`` to prompt the operator on a TTY. In UI mode that
prompt would otherwise leak to the terminal where the user can't see (and
can't answer) it, freezing the run forever.

This bridge installs a callable on ``review_gate._UI_PROMPT_HOOK``. When a
gate fires, the hook (called from the pipeline worker thread) posts a
``ReviewGateRequest`` to ``AppState``, blocks on a ``threading.Event``, and
returns the user's Approve/Reject decision once the UI dialog completes.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import flet as ft

import worca_t.review_gate as review_gate_module
from worca_t.ui.state import AppState, ReviewGateRequest


class ReviewGateBridge:
    """Monkey-patch ``review_gate._UI_PROMPT_HOOK`` to route through the UI."""

    def __init__(self, state: AppState, page: ft.Page) -> None:
        self.state = state
        self.page = page
        self._loop: asyncio.AbstractEventLoop | None = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        review_gate_module.set_ui_prompt_hook(self._prompt)

    def uninstall(self) -> None:
        review_gate_module.set_ui_prompt_hook(None)
        self._loop = None

    # ── Worker-thread side (called from pipeline thread) ────────────────

    def _prompt(
        self,
        *,
        step: int,
        title: str,
        summary_text: str,
        kind: str = "",
        data: Any = None,
    ) -> str:
        """Return ``"approve"`` or ``"reject"`` (blocks worker thread)."""
        if self._loop is None:
            # Bridge uninstalled mid-flight — fail safe by approving so
            # the pipeline doesn't deadlock.
            return "approve"

        decision: dict[str, str] = {"value": "approve"}
        done = threading.Event()

        self._loop.call_soon_threadsafe(
            asyncio.create_task,
            self._show_in_ui(step, title, summary_text, kind, data, decision, done),
        )

        # 1-hour ceiling matches HitlBridge; review gates can sit for a
        # while if the user steps away.
        done.wait(timeout=3600)
        return decision["value"]

    # ── Main-thread side (runs on Flet event loop) ──────────────────────

    async def _show_in_ui(
        self,
        step: int,
        title: str,
        summary_text: str,
        kind: str,
        data: Any,
        decision: dict[str, str],
        done: threading.Event,
    ) -> None:
        completion_event = asyncio.Event()
        req = ReviewGateRequest(
            step=step,
            title=title,
            summary=summary_text,
            completion_event=completion_event,
            kind=kind,
            data=data,
        )

        # Pause the stopwatch — same rationale as HitlBridge: review-gate
        # waits are the user's clock, not the pipeline's.
        self.state.pause_clock()
        self.state.pending_review_gate = req
        self.state.notify()

        try:
            try:
                self.page.update()
            except Exception:
                pass

            await completion_event.wait()

            # Map UI dialog decision -> worker-side response.
            raw = req.decision or "approve"
            if raw == "reject":
                decision["value"] = "reject"
            elif raw == "edit":
                # Edit-by-text in UI not yet supported end-to-end (the
                # terminal flow asks for instructions and applies via LLM
                # in a multi-step sub-prompt). Treat as approve so the
                # pipeline progresses; review_gate logs a warning.
                decision["value"] = "edit"
            else:
                decision["value"] = "approve"
        finally:
            try:
                req._dialog_open = False  # type: ignore[attr-defined]
            except Exception:
                pass
            self.state.pending_review_gate = None
            self.state.resume_clock()
            self.state.notify()
            try:
                self.page.update()
            except Exception:
                pass
            done.set()
