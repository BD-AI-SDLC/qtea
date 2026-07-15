"""Overall progress bar and run status banner."""

from __future__ import annotations

import contextlib

import flet as ft

from qtea.ui.state import STEP_DEFINITIONS, AppState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PRIMARY,
    SECONDARY,
    STATUS_COLORS,
    sz,
)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_elapsed(seconds: float) -> str:
    """Public alias so the tick loop in app.py can format without importing
    a private helper."""
    return _fmt_elapsed(seconds)


def build_progress_header(
    page: ft.Page,
    state: AppState,
    *,
    live_elapsed_text: ft.Text | None = None,
) -> ft.Container:
    """Build the top progress bar + status banner."""

    completed = state.completed_step_count()
    total = len(STEP_DEFINITIONS)
    progress = completed / total if total else 0

    # Current step name
    current_name = "Initializing..."
    if state.current_step and state.current_step in state.steps:
        s = state.steps[state.current_step]
        current_name = f"Step {s.number}: {s.name}"
        if s.agent_name:
            current_name += f"  —  {s.agent_name}"

    status_color = STATUS_COLORS.get(state.run_status, SECONDARY)
    if state.run_status == "running":
        status_color = SECONDARY
    elif state.run_status == "completed":
        status_color = "#66BB6A"
    elif state.run_status == "failed":
        status_color = "#FF5252"

    # Status badge
    status_text = state.run_status.upper()
    status_badge = ft.Container(
        content=ft.Text(
            status_text,
            size=sz(11),
            weight=ft.FontWeight.BOLD,
            color="#FFFFFF",
        ),
        bgcolor=status_color,
        border_radius=4,
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
    )

    # Run ID badge
    run_id_badge = ft.Text(
        state.run_id or "",
        size=sz(11),
        color=ON_SURFACE_DIM,
        font_family="Courier New",
    )

    # Elapsed clock. When a live widget is supplied, reuse it (so the
    # tick loop's per-second updates aren't thrown away each time the
    # header is rebuilt by on_state_change). Otherwise create a one-off.
    if live_elapsed_text is not None:
        live_elapsed_text.value = _fmt_elapsed(state.elapsed_s)
        elapsed_text = live_elapsed_text
    else:
        elapsed_text = ft.Text(
            _fmt_elapsed(state.elapsed_s),
            size=sz(14),
            weight=ft.FontWeight.W_600,
            color=ON_SURFACE,
        )

    # Stop button (visible only while running)
    def _on_stop(e):
        # Mark cancellation up-front so any blocked HITL wait or in-flight
        # subprocess polling sees it immediately.
        state.cancel_requested = True
        state.run_status = "failed"
        state.exit_code = 130

        # Cancel the asyncio task driving run_pipeline. This unwinds
        # awaited steps cleanly; sync work (subprocesses) is killed below.
        loop = state.pipeline_loop
        task = state.pipeline_task
        if loop is not None and task is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(task.cancel)

        # Kill ONLY the pipeline's subprocesses (pytest workers, MCP/npx
        # server, browsers, allure, etc.) so blocking subprocess.run / Popen
        # calls in the worker return. Critically, do NOT kill children that
        # existed before the run — above all the flet-desktop Flutter GUI
        # process, which is a child of this Python process. The previous
        # "kill all children recursively" killed the GUI too, so Stop *closed*
        # qtea instead of stopping the run. We diff against the pre-pipeline
        # snapshot (state.baseline_child_pids) and spare anything in it.
        try:
            import psutil

            baseline = state.baseline_child_pids or set()
            me = psutil.Process()
            for child in me.children(recursive=True):
                if child.pid in baseline:
                    continue  # GUI / pre-existing — never kill
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.kill()
        except Exception:
            pass

        # If the worker is parked inside HitlBridge.prompt_user waiting on a
        # threading.Event, release it now so the worker thread can exit.
        # Unlike the normal Submit/Cancel path, this bypasses the dialog's
        # own button handlers entirely — so we must pop its AlertDialog
        # ourselves. Dialogs live on page._dialogs, a stack completely
        # separate from page.views; the "/results" rebuild that follows
        # never touches it, so a stuck dialog would otherwise sit on top of
        # the summary screen forever.
        pending = state.pending_hitl
        if pending is not None and pending.completion_event is not None:
            with contextlib.suppress(Exception):
                pending.completion_event.set()
            if getattr(pending, "_dialog_open", False):
                with contextlib.suppress(Exception):
                    page.pop_dialog()
        pending_rg = state.pending_review_gate
        if pending_rg is not None and pending_rg.completion_event is not None:
            with contextlib.suppress(Exception):
                pending_rg.completion_event.set()
            if getattr(pending_rg, "_dialog_open", False):
                with contextlib.suppress(Exception):
                    page.pop_dialog()

        state.notify()

        # Snap to the results view NOW — don't wait for the still-cancelling
        # pipeline task to hit its finally block. Otherwise every trailing
        # log event (step.end from the cancelled step, bridge cleanup, etc.)
        # keeps calling state.notify(), which rebuilds pipeline_view's
        # phase groups / metrics / header / log in place. The user sees a
        # visible flicker of the /run view mutating for the duration of
        # cancellation instead of an immediate summary. Clearing the state
        # listeners first cuts the old pipeline_view's on_state_change
        # subscription so those trailing notify()s no-op. The worker's own
        # finally block will re-navigate to /results — same route, so the
        # rebuild is idempotent.
        navigate_to = None
        if isinstance(page.data, dict):
            navigate_to = page.data.get("navigate_to")
        with contextlib.suppress(Exception):
            if navigate_to is not None:
                state._listeners.clear()
                page.route = "/results"
                navigate_to("/results")
                # Signal to the pipeline worker's finally block (in
                # app.py's _run_pipeline) that the results view is
                # already up — it should skip its own rebuild instead
                # of doing a second page.views.clear()+build. Two
                # rebuilds in rapid succession orphan the Flet client's
                # widget-id map, leaving the summary rendered but every
                # button dead-on-click.
                state.results_navigated = True
            page.update()

    stop_button = ft.OutlinedButton(
        content="Stop",
        icon=ft.Icons.STOP_CIRCLE_OUTLINED,
        style=ft.ButtonStyle(color="#FF5252"),
        on_click=_on_stop,
        visible=state.run_status == "running",
    )

    # Top bar
    top_row = ft.Row(
        controls=[
            ft.Icon(ft.Icons.ROCKET_LAUNCH, color=PRIMARY, size=sz(22)),
            ft.Text(
                "qtea",
                size=sz(18),
                weight=ft.FontWeight.BOLD,
                color=PRIMARY,
            ),
            ft.Container(width=16),
            status_badge,
            run_id_badge,
            ft.Container(expand=True),
            ft.Icon(ft.Icons.TIMER_OUTLINED, color=ON_SURFACE_DIM, size=sz(18)),
            elapsed_text,
            ft.Container(width=8),
            stop_button,
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=8,
    )

    # Progress bar row
    progress_row = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Text(
                        current_name,
                        size=sz(12),
                        color=ON_SURFACE_DIM,
                        weight=ft.FontWeight.W_500,
                    ),
                    ft.Container(expand=True),
                    ft.Text(
                        f"{completed}/{total} steps",
                        size=sz(12),
                        color=ON_SURFACE_DIM,
                    ),
                ],
            ),
            ft.ProgressBar(
                value=progress,
                color=SECONDARY,
                bgcolor=CARD_BG,
                bar_height=6,
                border_radius=3,
            ),
        ],
        spacing=4,
    )

    return ft.Container(
        content=ft.Column(
            controls=[top_row, progress_row],
            spacing=10,
        ),
        padding=ft.Padding.symmetric(horizontal=24, vertical=14),
        bgcolor=BACKGROUND,
        border=ft.Border.only(bottom=ft.BorderSide(1, "#2E2E42")),
    )
