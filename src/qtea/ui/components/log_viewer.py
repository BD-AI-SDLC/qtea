"""Streaming, color-coded log viewer panel."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re

import flet as ft

from qtea.ui.state import AppState, LogLine
from qtea.ui.theme import (
    CARD_BG,
    DIVIDER,
    LOG_AGENT_COLOR,
    LOG_ERROR_FIELD_COLOR,
    LOG_LEVEL_COLORS,
    LOG_MODEL_COLOR,
    LOG_STEP_COLOR,
    LOG_TOKENS_COLOR,
    ON_SURFACE,
    ON_SURFACE_DIM,
    sz,
)

MAX_DISPLAY_LINES = 500

# Matches a leading step token (e.g. "step08", "step1") so it can be tinted
# orange while everything after the first '.' keeps the default event color.
_STEP_PREFIX_RE = re.compile(r"^(step\d+)(.*)$", re.IGNORECASE)


def scroll_to_end(scrollable: ft.Control) -> None:
    """Best-effort scroll to end. In newer Flet versions `scroll_to` is a
    coroutine — calling it synchronously leaves an unawaited coroutine and
    triggers a RuntimeWarning. Handle both APIs.

    Skips unmounted controls (``.page`` raises, not None, before the control
    is added to ``page.views``) and cancels any still-pending previous
    scroll_to task before starting a new one — on a long, chatty run these
    invoke_method() calls (no timeout) were piling up into hundreds of
    never-resolving awaits, and a burst of them right as the run finished
    destabilized the Flet session, dropping the UI back to the config screen.
    """
    try:
        if not scrollable.page:
            return
    except Exception:
        return

    prev_task: asyncio.Task | None = getattr(scrollable, "_qtea_scroll_task", None)
    if prev_task is not None and not prev_task.done():
        prev_task.cancel()

    try:
        result = scrollable.scroll_to(offset=-1, duration=0)
    except Exception:
        return
    if inspect.iscoroutine(result):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            result.close()
            return
        task = loop.create_task(result)
        with contextlib.suppress(Exception):
            scrollable._qtea_scroll_task = task


def _build_log_line(line: LogLine) -> ft.Container:
    level_color = LOG_LEVEL_COLORS.get(line.level, ON_SURFACE_DIM)

    parts: list[ft.TextSpan] = []

    # Timestamp
    if line.timestamp:
        parts.append(
            ft.TextSpan(
                f"{line.timestamp}  ",
                style=ft.TextStyle(size=sz(11), color=ON_SURFACE_DIM),
            )
        )

    # Level badge
    parts.append(
        ft.TextSpan(
            f"{line.level.upper():8s}",
            style=ft.TextStyle(
                size=sz(11),
                color=level_color,
                weight=ft.FontWeight.BOLD,
            ),
        )
    )

    # Event name. A leading step token (e.g. "step08") is tinted orange; the
    # rest ("...b55.started") keeps the default color. Pad to 36 chars first so
    # the split preserves the column width and the following fields stay aligned.
    event_text = f"{line.event:36s}"
    step_match = _STEP_PREFIX_RE.match(event_text)
    if step_match:
        step_tok, rest = step_match.group(1), step_match.group(2)
        parts.append(
            ft.TextSpan(
                step_tok,
                style=ft.TextStyle(
                    size=sz(11),
                    color=LOG_STEP_COLOR,
                    weight=ft.FontWeight.W_600,
                ),
            )
        )
        parts.append(
            ft.TextSpan(
                rest,
                style=ft.TextStyle(
                    size=sz(11),
                    color=ON_SURFACE,
                    weight=ft.FontWeight.W_600,
                ),
            )
        )
    else:
        parts.append(
            ft.TextSpan(
                event_text,
                style=ft.TextStyle(
                    size=sz(11),
                    color=ON_SURFACE,
                    weight=ft.FontWeight.W_600,
                ),
            )
        )

    # Message / fields. If the event carries an `agent` field, extract it and
    # render it as a cyan span so the human can see at a glance which agent is
    # driving the current turn. If the event carries an `error` field (e.g.
    # a step.end on a failed step) we also take the field-iterating path so
    # the error value can be tinted orange — otherwise it would be buried in
    # the flat dim message.
    _SKIP_KEYS = {"event", "timestamp", "level", "run_id", "agent"}
    _MODEL_KEYS = {"model"}
    _TOKEN_KEYS = {"tokens_input", "tokens_output"}
    _ERROR_KEYS = {"error"}

    agent_name = line.fields.get("agent") if line.fields else None
    has_error = bool(line.fields.get("error")) if line.fields else False
    if agent_name or has_error:
        need_comma = False
        if agent_name:
            parts.append(
                ft.TextSpan(
                    f"agent={agent_name}",
                    style=ft.TextStyle(
                        size=sz(11),
                        color=LOG_AGENT_COLOR,
                        weight=ft.FontWeight.BOLD,
                    ),
                )
            )
            need_comma = True
        remaining = [
            (k, v)
            for k, v in line.fields.items()
            if k not in _SKIP_KEYS
            and v is not None and v is not False
        ]
        for k, v in remaining:
            prefix = ", " if need_comma else ""
            if k in _MODEL_KEYS:
                color = LOG_MODEL_COLOR
                weight = None
            elif k in _ERROR_KEYS:
                color = LOG_ERROR_FIELD_COLOR
                weight = ft.FontWeight.W_600
            elif k in _TOKEN_KEYS:
                color = LOG_TOKENS_COLOR
                weight = None
            else:
                color = ON_SURFACE_DIM
                weight = None
            parts.append(
                ft.TextSpan(
                    f"{prefix}{k}={v}",
                    style=ft.TextStyle(size=sz(11), color=color, weight=weight),
                )
            )
            need_comma = True
    elif line.message:
        parts.append(
            ft.TextSpan(
                line.message,
                style=ft.TextStyle(size=sz(11), color=ON_SURFACE_DIM),
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

    # Log list — ft.Column with scroll=ALWAYS gives a permanently visible
    # scrollbar. ft.ListView suppresses the bar when content fits or when
    # the widget is inside a scrollable ancestor, which is the case in the
    # results view. scroll_to(offset=-1) replaces ListView's auto_scroll.
    log_list = ft.Column(
        scroll=ft.ScrollMode.ALWAYS,
        spacing=0,
        expand=True,
    )

    def refresh_log_list() -> None:
        filtered = _get_filtered_lines()
        log_list.controls = [_build_log_line(l) for l in filtered]
        scroll_to_end(log_list)

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
        text_size=sz(12),
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
        text_size=sz(12),
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
        size=sz(11),
        color=ON_SURFACE_DIM,
    )

    # Header
    header = ft.Row(
        controls=[
            ft.Text(
                "LOGS",
                size=sz(10),
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
