"""HITL bridge: route ``qtea.hitl.prompt_user`` calls into UI dialogs.

The pipeline runs in a worker thread (so its blocking calls don't freeze the
Flet event loop). When an agent emits clarifications, ``prompt_user`` posts a
``HitlRequest`` to ``AppState`` and blocks on a ``threading.Event`` until the
UI dialog (running on the Flet event loop) collects the user's answers and
fires the event.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import flet as ft

import qtea.hitl as hitl_module
from qtea.hitl import RESOLUTION_ANSWERED, Question
from qtea.ui.state import AppState, HitlRequest


@dataclass
class _PendingHitl:
    questions: list[Question]
    agent_label: str
    response: dict[str, tuple[str, str]] = field(default_factory=dict)
    event: threading.Event = field(default_factory=threading.Event)


class HitlBridge:
    """Monkey-patch ``hitl.prompt_user`` to route through the UI."""

    def __init__(self, state: AppState, page: ft.Page) -> None:
        self.state = state
        self.page = page
        self._loop: asyncio.AbstractEventLoop | None = None
        self._original: Any = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """Replace ``hitl.prompt_user`` with the UI-aware version.

        ``loop`` is the Flet event loop, captured from the main thread so
        the worker-thread replacement can schedule UI work back on it.
        """
        self._loop = loop
        self._original = hitl_module.prompt_user
        hitl_module.prompt_user = self._prompt_user  # type: ignore[assignment]

    def uninstall(self) -> None:
        if self._original is not None:
            hitl_module.prompt_user = self._original  # type: ignore[assignment]
            self._original = None
        self._loop = None

    # ── Worker-thread side (called from pipeline thread) ────────────────

    def _prompt_user(
        self, questions: list[Question], *, agent_label: str,
    ) -> dict[str, tuple[str, str]]:
        """Drop-in replacement for ``hitl.prompt_user`` that uses the UI."""
        if not questions or self._loop is None:
            return {}

        pending = _PendingHitl(questions=list(questions), agent_label=agent_label)

        # Schedule the UI work on the Flet event loop and DON'T await it
        # from this thread — that would deadlock if we shared a loop.
        # We use call_soon_threadsafe to fire the coroutine into the loop
        # without blocking the worker.
        self._loop.call_soon_threadsafe(
            asyncio.create_task, self._show_in_ui(pending),
        )

        # Block this worker thread until the UI dialog completes.
        pending.event.wait(timeout=3600)  # 1-hour ceiling

        return pending.response

    # ── Main-thread side (runs on Flet event loop) ──────────────────────

    async def _show_in_ui(self, pending: _PendingHitl) -> None:
        # Build a HitlRequest the existing dialog component understands.
        # Note: we do NOT show the dialog directly here. ``state.notify()``
        # fires ``on_state_change`` in pipeline_view, which is the single
        # owner of dialog display. Calling show_hitl_dialog here too would
        # stack two AlertDialogs onto page._dialogs.controls and the user's
        # Submit click would only pop the topmost — they'd then see the
        # (empty) one underneath and conclude their answers were lost.
        completion_event = asyncio.Event()

        req = HitlRequest(
            step=self.state.current_step or 0,
            agent_label=pending.agent_label,
            questions=[
                {
                    "id": q.id,
                    "text": q.prompt_text,
                    "context": q.context or "",
                    "type": q.kind,
                }
                for q in pending.questions
            ],
            answers={},
            completion_event=completion_event,
        )
        # Pause the elapsed-time stopwatch while we wait on the user — HITL
        # is the user's clock, not the pipeline's.
        self.state.pause_clock()
        self.state.pending_hitl = req
        # notify() runs subscribers inline; pipeline_view's on_state_change
        # will create exactly one dialog (guarded by req._dialog_open).
        self.state.notify()

        try:
            try:
                self.page.update()
            except Exception:
                pass

            # Wait for the user to submit / skip.
            await completion_event.wait()

            # Marshal answers into the format prompt_user returns:
            #   { question_id: (resolution, answer_text) }
            for q in pending.questions:
                raw = req.answers.get(q.id)
                if raw is None:
                    continue
                if isinstance(raw, tuple) and len(raw) == 2:
                    # Dialog already stored it as (resolution, text).
                    pending.response[q.id] = raw
                else:
                    pending.response[q.id] = (RESOLUTION_ANSWERED, str(raw))
        finally:
            try:
                req._dialog_open = False  # type: ignore[attr-defined]
            except Exception:
                pass
            self.state.pending_hitl = None
            self.state.resume_clock()
            self.state.notify()
            try:
                self.page.update()
            except Exception:
                pass
            # Unblock the worker thread.
            pending.event.set()
