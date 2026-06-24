"""Streaming, color-coded log viewer panel."""

from __future__ import annotations

import flet as ft

from worca_t.ui.state import AppState, LogLine
from worca_t.ui.theme import CARD_BG, DIVIDER, LOG_LEVEL_COLORS, ON_SURFACE, ON_SURFACE_DIM

MAX_DISPLAY_LINES = 500


def _build_log_line(line: LogLine) -> ft.Container:
    level_color = LOG_LEVEL_COLORS.get(line.level, ON_SURFACE_DIM)

    parts: list[ft.TextSpan] = []

    # Timestamp
    if line.timestamp:
        parts.append(
            ft.TextSpan(
                f"{line.timestamp}  ",
                style=ft.TextStyle(size=11, color=ON_SURFACE_DIM),
            )
        )

    # Level badge
    parts.append(
        ft.TextSpan(
            f"{line.level.upper():8s}",
            style=ft.TextStyle(
                size=11,
                color=level_color,
                weight=ft.FontWeight.BOLD,
            ),
        )
    )

    # Event name
    parts.append(
        ft.TextSpan(
            f"{line.event:36s}",
            style=ft.TextStyle(
                size=11,
                color=ON_SURFACE,
                weight=ft.FontWeight.W_600,
            ),
        )
    )

    # Message / fields
    if line.message:
        parts.append(
            ft.TextSpan(
                line.message,
                style=ft.TextStyle(size=11, color=ON_SURFACE_DIM),
            )
        )

    # Wrap text so overflow flows onto the next visual line instead of
    # generating a per-row horizontal scrollbar (which ends up rendering
    # directly on top of the line the cursor is over). With the columns
    # now resizable, the user can widen the Logs panel for less wrapping.
    return ft.Container(
        content=ft.Text(
            spans=parts,
            font_family="Courier New",
            no_wrap=False,
            selectable=True,
        ),
        padding=ft.Padding.symmetric(horizontal=8, vertical=1),
    )


def build_log_viewer(page: ft.Page, state: AppState) -> ft.Container:
    """Build the log viewer panel with filter controls."""

    # Filter state
    current_filter = {"level": "all", "search": ""}

    # Filtered lines
    def _get_filtered_lines() -> list[LogLine]:
        lines = state.log_lines[-MAX_DISPLAY_LINES:]
        lvl = current_filter["level"]
        search = current_filter["search"].lower()
        if lvl != "all":
            lines = [l for l in lines if l.level == lvl]
        if search:
            lines = [
                l
                for l in lines
                if search in l.event.lower() or search in l.message.lower()
            ]
        return lines

    # Log list
    log_list = ft.ListView(
        auto_scroll=True,
        spacing=0,
        padding=ft.Padding.symmetric(vertical=4),
        expand=True,
    )

    def refresh_log_list() -> None:
        filtered = _get_filtered_lines()
        log_list.controls = [_build_log_line(l) for l in filtered]

    refresh_log_list()

    # Filter dropdown
    level_dropdown = ft.Dropdown(
        value="all",
        options=[
            ft.dropdown.Option("all", "All Levels"),
            ft.dropdown.Option("info", "INFO"),
            ft.dropdown.Option("warning", "WARNING"),
            ft.dropdown.Option("error", "ERROR"),
            ft.dropdown.Option("debug", "DEBUG"),
        ],
        width=140,
        height=36,
        text_size=12,
        content_padding=ft.Padding.symmetric(horizontal=8, vertical=0),
        border_color=DIVIDER,
        on_select=lambda e: (
            current_filter.__setitem__("level", e.data),
            refresh_log_list(),
            page.update(),
        ),
    )

    # Search field
    search_field = ft.TextField(
        hint_text="Filter logs...",
        width=180,
        height=36,
        text_size=12,
        content_padding=ft.Padding.symmetric(horizontal=8, vertical=0),
        border_color=DIVIDER,
        prefix_icon=ft.Icons.SEARCH,
        on_change=lambda e: (
            current_filter.__setitem__("search", e.data or ""),
            refresh_log_list(),
            page.update(),
        ),
    )

    # Line count
    count_text = ft.Text(
        f"{len(state.log_lines)} lines",
        size=11,
        color=ON_SURFACE_DIM,
    )

    # Header
    header = ft.Row(
        controls=[
            ft.Text(
                "LOGS",
                size=10,
                weight=ft.FontWeight.BOLD,
                color=ON_SURFACE_DIM,
            ),
            ft.Container(expand=True),
            level_dropdown,
            search_field,
            count_text,
        ],
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    return ft.Container(
        content=ft.Column(
            controls=[header, log_list],
            spacing=8,
            expand=True,
        ),
        padding=12,
        bgcolor=CARD_BG,
        border_radius=12,
        border=ft.Border.all(1, DIVIDER),
        expand=True,
    )
