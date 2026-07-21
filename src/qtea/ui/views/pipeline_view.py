"""Live pipeline visualization — the centerpiece view."""

from __future__ import annotations

import contextlib

import flet as ft

from qtea.ui.components.log_viewer import build_log_viewer
from qtea.ui.components.metrics_panel import build_metrics_panel
from qtea.ui.components.phase_group import build_phase_group
from qtea.ui.components.progress_header import build_progress_header
from qtea.ui.state import AppState
from qtea.ui.theme import BACKGROUND, CARD_BG, DIVIDER, sz


def find_live_step_widget(controls: list[ft.Control]) -> ft.Text | None:
    """Walk the control tree for the in-progress step card's elapsed widget."""
    for ctrl in controls:
        if isinstance(ctrl, ft.Text) and getattr(ctrl, "data", None) == "live_step_elapsed":
            return ctrl
        for attr in ("controls", "content"):
            child = getattr(ctrl, attr, None)
            if child is None:
                continue
            if isinstance(child, list):
                found = find_live_step_widget(child)
                if found:
                    return found
            elif isinstance(child, ft.Control):
                found = find_live_step_widget([child])
                if found:
                    return found
    return None


def build_pipeline_view(page: ft.Page, state: AppState) -> ft.Container:
    """Build the three-panel pipeline monitoring view.

    Layout::

        +-----------------------------------------------------+
        | Progress Header (full width)                         |
        +-----------------------------------------------------+
        | Step Flow   |  Log Viewer     |  Metrics Sidebar     |
        | (flex 3)    |  (flex 3)       |  (flex 1.5)          |
        +-----------------------------------------------------+
    """

    # ── Click handler: open step details dialog ─────────────────────────
    def _on_step_click(step):
        from qtea.ui.components.step_details_dialog import show_step_details_dialog
        show_step_details_dialog(page, state, step)

    # ── Step flow (left panel) ───────────────────────────────────────────
    phase_groups = ft.Column(
        controls=[
            build_phase_group("A", state, on_step_click=_on_step_click),
            build_phase_group("B", state, on_step_click=_on_step_click),
            build_phase_group("C", state, on_step_click=_on_step_click),
        ],
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
    # Publish the persistent Column so app.py's tick loop can walk its
    # (rebuilt-in-place) .controls every second and find the current
    # in-progress step's elapsed widget directly — rather than caching a
    # resolved widget reference that only gets refreshed when a log line
    # happens to trigger on_state_change(). A long single-call step (one
    # reasoning.start ... reasoning.end with nothing in between) could
    # otherwise leave the cached reference stale for the step's whole
    # duration, freezing its clock.
    if page.data is None:
        page.data = {}
    if isinstance(page.data, dict):
        page.data["phase_groups_ref"] = phase_groups

    # Panel widths in pixels. The middle log panel fills the remaining
    # space (expand=True); the left and right panels have explicit widths
    # that the splitters mutate on drag.
    widths = {"step": 420, "metrics": 320}

    step_panel = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(
                    "PIPELINE",
                    size=sz(10),
                    weight=ft.FontWeight.BOLD,
                    color="#9E9E9E",
                ),
                ft.Container(height=4),
                phase_groups,
            ],
            spacing=4,
            expand=True,
        ),
        padding=16,
        bgcolor=CARD_BG,
        border_radius=12,
        border=ft.Border.all(1, DIVIDER),
        width=widths["step"],
    )

    # ── Log viewer (center panel) ────────────────────────────────────────
    log_panel = ft.Container(
        content=build_log_viewer(page, state),
        expand=True,
    )

    # ── Metrics sidebar (right panel) ────────────────────────────────────
    metrics_panel = ft.Container(
        content=build_metrics_panel(state),
        width=widths["metrics"],
    )

    # ── Draggable splitters ──────────────────────────────────────────────
    def _make_splitter(side: str) -> ft.GestureDetector:
        """Vertical splitter that resizes the adjacent fixed-width panel.

        Dragging the LEFT splitter changes ``step_panel.width``; dragging
        the RIGHT splitter changes ``metrics_panel.width`` (with sign
        flipped, since dragging right shrinks the right panel).
        The middle ``log_panel`` is ``expand=True`` and absorbs the
        difference automatically.
        """
        MIN, MAX = 220, 900

        def _on_drag(e: ft.DragUpdateEvent) -> None:
            delta = e.local_delta.x if e.local_delta else (e.primary_delta or 0.0)
            if side == "step":
                new_w = max(MIN, min(MAX, widths["step"] + delta))
                widths["step"] = new_w
                step_panel.width = new_w
            else:
                new_w = max(MIN, min(MAX, widths["metrics"] - delta))
                widths["metrics"] = new_w
                metrics_panel.width = new_w
            with contextlib.suppress(Exception):
                page.update()

        handle = ft.Container(
            width=6,
            bgcolor=DIVIDER,
            border_radius=3,
        )
        return ft.GestureDetector(
            content=handle,
            mouse_cursor=ft.MouseCursor.RESIZE_COLUMN,
            drag_interval=10,
            on_horizontal_drag_update=_on_drag,
        )

    splitter_left = _make_splitter("step")
    splitter_right = _make_splitter("metrics")

    # ── Main layout ──────────────────────────────────────────────────────
    # Single live elapsed-text widget. Reused across header rebuilds so the
    # 1-Hz tick loop's updates to its .value persist between state.notify()s.
    from qtea.ui.components.progress_header import fmt_elapsed

    live_elapsed = ft.Text(
        fmt_elapsed(state.elapsed_s),
        size=sz(14),
        weight=ft.FontWeight.W_600,
    )
    # Publish so app.py's tick task can find it without importing this view.
    if page.data is None:
        page.data = {}
    if isinstance(page.data, dict):
        page.data["live_elapsed"] = live_elapsed

    progress_header = build_progress_header(
        page, state, live_elapsed_text=live_elapsed,
    )

    body = ft.Row(
        controls=[
            step_panel,
            splitter_left,
            log_panel,
            splitter_right,
            metrics_panel,
        ],
        spacing=6,
        expand=True,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    # Track which HITL / review-gate request has already had its dialog shown,
    # so the streaming log -> notify -> rebuild loop doesn't recreate the
    # dialog on every event (which would wipe the user's in-progress answers).
    shown: dict[str, object | None] = {"hitl": None, "review": None}

    # Subscribe to state changes to rebuild the view
    def on_state_change() -> None:
        # Rebuild step flow
        phase_groups.controls = [
            build_phase_group("A", state, on_step_click=_on_step_click),
            build_phase_group("B", state, on_step_click=_on_step_click),
            build_phase_group("C", state, on_step_click=_on_step_click),
        ]
        # Rebuild metrics
        metrics_panel.content = build_metrics_panel(state)
        # Rebuild progress header (reusing the live elapsed widget)
        progress_header_container.content = build_progress_header(
            page, state, live_elapsed_text=live_elapsed,
        )
        # Update log viewer
        log_viewer = log_panel.content
        if hasattr(log_viewer, "content") and hasattr(log_viewer.content, "controls"):
            cols = log_viewer.content.controls
            if len(cols) >= 2:
                log_list = cols[1]
                if hasattr(log_list, "controls"):
                    from qtea.ui.components.log_viewer import (
                        MAX_DISPLAY_LINES,
                        _build_log_line,
                        scroll_to_end,
                    )

                    recent = state.log_lines[-MAX_DISPLAY_LINES:]
                    log_list.controls = [_build_log_line(l) for l in recent]
                    scroll_to_end(log_list)
                    header_row = cols[0]
                    if hasattr(header_row, "controls") and len(header_row.controls) >= 5:
                        header_row.controls[-1].value = f"{len(state.log_lines)} lines"

        # Show HITL dialog if needed — but only ONCE per request. notify()
        # fires on every log line, and re-showing the dialog rebuilds its
        # TextField widgets and discards anything the user typed.
        if state.pending_hitl is not None:
            if shown["hitl"] is not state.pending_hitl:
                from qtea.ui.components.hitl_dialog import show_hitl_dialog

                shown["hitl"] = state.pending_hitl
                show_hitl_dialog(page, state)
        else:
            shown["hitl"] = None

        if state.pending_review_gate is not None:
            if shown["review"] is not state.pending_review_gate:
                from qtea.ui.components.hitl_dialog import show_review_gate_dialog

                shown["review"] = state.pending_review_gate
                show_review_gate_dialog(page, state)
        else:
            shown["review"] = None

    state.subscribe(on_state_change)

    progress_header_container = ft.Container(content=progress_header)

    return ft.Container(
        content=ft.Column(
            controls=[progress_header_container, body],
            spacing=0,
            expand=True,
        ),
        expand=True,
        padding=ft.Padding.only(left=12, right=12, bottom=12),
        bgcolor=BACKGROUND,
    )
