"""Overall progress bar and run status banner."""

from __future__ import annotations

import flet as ft

from worca_t.ui.state import AppState, STEP_DEFINITIONS
from worca_t.ui.theme import (
    BACKGROUND,
    CARD_BG,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PRIMARY,
    SECONDARY,
    STATUS_COLORS,
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
            size=11,
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
        size=11,
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
            size=14,
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
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass

        # Kill child processes (pytest workers, MCP/npx server, allure, etc.)
        # so blocking subprocess.run / Popen calls in the worker return.
        try:
            import psutil

            me = psutil.Process()
            for child in me.children(recursive=True):
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

        # If the worker is parked inside HitlBridge.prompt_user waiting on a
        # threading.Event, release it now so the worker thread can exit.
        pending = state.pending_hitl
        if pending is not None and pending.completion_event is not None:
            try:
                pending.completion_event.set()
            except Exception:
                pass
        pending_rg = state.pending_review_gate
        if pending_rg is not None and pending_rg.completion_event is not None:
            try:
                pending_rg.completion_event.set()
            except Exception:
                pass

        state.notify()
        try:
            page.update()
        except Exception:
            pass

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
            ft.Icon(ft.Icons.ROCKET_LAUNCH, color=PRIMARY, size=22),
            ft.Text(
                "worca-t",
                size=18,
                weight=ft.FontWeight.BOLD,
                color=PRIMARY,
            ),
            ft.Container(width=16),
            status_badge,
            run_id_badge,
            ft.Container(expand=True),
            ft.Icon(ft.Icons.TIMER_OUTLINED, color=ON_SURFACE_DIM, size=18),
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
                        size=12,
                        color=ON_SURFACE_DIM,
                        weight=ft.FontWeight.W_500,
                    ),
                    ft.Container(expand=True),
                    ft.Text(
                        f"{completed}/{total} steps",
                        size=12,
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
