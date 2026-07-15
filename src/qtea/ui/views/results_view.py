"""Post-run results summary dashboard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import flet as ft

from qtea.ui.components.log_viewer import build_log_viewer
from qtea.ui.state import STEP_DEFINITIONS, AppState, AuxAgentUIState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PHASE_COLORS,
    PRIMARY,
    STATUS_COLORS,
    sz,
)


def _fmt_elapsed(seconds: float | None) -> str:
    # None-safe: a step that errored/never-emitted may carry None elapsed;
    # `None < 60` raises TypeError, which used to blow up the whole results
    # view and strand the user on the initial screen.
    seconds = seconds or 0
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _fmt_tokens(n: int | None) -> str:
    # None-safe (see _fmt_elapsed): step.end may carry null token counts.
    n = n or 0
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def build_results_view(
    page: ft.Page,
    state: AppState,
    on_new_run: Callable[[], None],
) -> ft.Container:
    """Build the post-run results dashboard."""

    is_success = state.exit_code == 0
    # A user-initiated Stop (exit 130 / cancel_requested) is not a failure —
    # label it distinctly so the summary reads as "stopped at step N", not a
    # crash.
    is_stopped = (
        not is_success
        and (state.exit_code == 130 or getattr(state, "cancel_requested", False))
    )

    # ── Status banner ────────────────────────────────────────────────────
    if is_success:
        banner_icon, banner_color, banner_text = (
            ft.Icons.CHECK_CIRCLE, "#66BB6A", "Pipeline Completed Successfully"
        )
    elif is_stopped:
        banner_icon, banner_color, banner_text = (
            ft.Icons.STOP_CIRCLE_OUTLINED, "#FFB74D", "Pipeline Stopped"
        )
    else:
        banner_icon, banner_color, banner_text = (
            ft.Icons.ERROR, "#FF5252", "Pipeline Failed"
        )

    banner = ft.Container(
        content=ft.Row(
            controls=[
                ft.Icon(banner_icon, color=banner_color, size=sz(36)),
                ft.Column(
                    controls=[
                        ft.Text(
                            banner_text,
                            size=sz(22),
                            weight=ft.FontWeight.BOLD,
                            color=banner_color,
                        ),
                        ft.Row(
                            controls=[
                                ft.Text(
                                    f"Run: {state.run_id or 'N/A'}",
                                    size=sz(12),
                                    color=ON_SURFACE_DIM,
                                    font_family="Courier New",
                                ),
                                ft.Text("|", size=sz(12), color=DIVIDER),
                                ft.Text(
                                    f"Duration: {_fmt_elapsed(state.elapsed_s)}",
                                    size=sz(12),
                                    color=ON_SURFACE_DIM,
                                ),
                                ft.Text("|", size=sz(12), color=DIVIDER),
                                ft.Text(
                                    f"Cost: ${state.total_cost or 0:.2f}",
                                    size=sz(12),
                                    color="#FF5252",
                                ),
                            ],
                            spacing=8,
                        ),
                    ],
                    spacing=4,
                ),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=24,
        bgcolor=CARD_BG,
        border_radius=12,
        border=ft.Border.all(1, banner_color + "40"),
    )

    # ── Steps summary table ──────────────────────────────────────────────
    table_rows: list[ft.DataRow] = []
    for num, name, phase in STEP_DEFINITIONS:
        s = state.steps.get(num)
        if not s:
            continue
        status_color = STATUS_COLORS.get(s.status, ON_SURFACE_DIM)
        PHASE_COLORS.get(phase, ON_SURFACE_DIM)

        table_rows.append(
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(str(num), size=sz(12), color=ON_SURFACE)),
                    ft.DataCell(ft.Text(name, size=sz(12), color=ON_SURFACE)),
                    ft.DataCell(
                        ft.Container(
                            content=ft.Text(
                                s.status,
                                size=sz(10),
                                color="#FFFFFF",
                                weight=ft.FontWeight.BOLD,
                            ),
                            bgcolor=status_color,
                            border_radius=4,
                            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                        )
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_elapsed(s.elapsed_s), size=sz(12), color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_tokens(s.tokens_in), size=sz(12), color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_tokens(s.tokens_out), size=sz(12), color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(
                            _fmt_tokens(s.cache_read) if s.cache_read else "-",
                            size=sz(12),
                            color=ON_SURFACE_DIM,
                        )
                    ),
                    ft.DataCell(
                        ft.Text(
                            _fmt_tokens(s.cache_write) if s.cache_write else "-",
                            size=sz(12),
                            color=ON_SURFACE_DIM,
                        )
                    ),
                    ft.DataCell(
                        ft.Text(str(s.agent_calls), size=sz(12), color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(
                            f"${s.cost_usd:.2f}" if (s.cost_usd or 0) > 0 else "-",
                            size=sz(12),
                            color="#FF5252" if (s.cost_usd or 0) > 0 else ON_SURFACE_DIM,
                        )
                    ),
                ],
            )
        )

    # Aux rows — one per helper agent (debug / critical-thinking /
    # principal-engineer) that fired on retry exhaustion. Sit between the
    # step rows and TOTAL so the TOTAL visibly sums both groups.
    for aux in state.auxiliary_records:
        table_rows.append(_aux_row(aux))

    # Totals row
    table_rows.append(
        ft.DataRow(
            cells=[
                ft.DataCell(ft.Text("", size=sz(12))),
                ft.DataCell(
                    ft.Text("TOTAL", size=sz(12), weight=ft.FontWeight.BOLD, color=ON_SURFACE)
                ),
                ft.DataCell(ft.Text("", size=sz(12))),
                ft.DataCell(
                    ft.Text(
                        _fmt_elapsed(state.elapsed_s),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_tokens_in),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_tokens_out),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_cache_read),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_cache_write),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        str(state.total_agent_calls),
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        f"${state.total_cost or 0:.2f}",
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color="#FF5252",
                    )
                ),
            ],
        )
    )

    summary_table = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(
                    "STEP SUMMARY",
                    size=sz(10),
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                ft.Container(height=8),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("#", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Step", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Status", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Duration", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Tok In", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Tok Out", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Cache R", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Cache W", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Agents", size=sz(11), color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Cost", size=sz(11), color=ON_SURFACE_DIM)),
                    ],
                    rows=table_rows,
                    heading_row_height=32,
                    data_row_min_height=36,
                    data_row_max_height=40,
                    column_spacing=16,
                    horizontal_lines=ft.BorderSide(1, DIVIDER),
                ),
            ],
        ),
        padding=20,
        bgcolor=CARD_BG,
        border_radius=12,
        border=ft.Border.all(1, DIVIDER),
    )

    # ── Test results (if Step 9 completed) ───────────────────────────────
    test_results_section = ft.Container()
    step9 = state.steps.get(9)
    if step9 and step9.status in ("completed", "warned"):
        ws_path = state.workspace_path
        if ws_path:
            results_file = Path(ws_path) / "artifacts" / "step09" / "run-results.json"
            if results_file.exists():
                try:
                    data = json.loads(results_file.read_text(encoding="utf-8"))
                    totals = data.get("totals", {})
                    tests = totals.get("tests", 0)
                    passed = totals.get("passed", 0)
                    failed = totals.get("failed", 0)
                    skipped = totals.get("skipped", 0)
                    errors = totals.get("errors", 0)
                    pass_rate = (passed / tests * 100) if tests else 0

                    rate_color = "#66BB6A" if pass_rate >= 80 else "#FFB74D" if pass_rate >= 50 else "#FF5252"

                    test_results_section = ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "TEST RESULTS",
                                    size=sz(10),
                                    weight=ft.FontWeight.BOLD,
                                    color=ON_SURFACE_DIM,
                                ),
                                ft.Container(height=8),
                                ft.Row(
                                    controls=[
                                        _stat_card("Total", str(tests), ON_SURFACE),
                                        _stat_card("Passed", str(passed), "#66BB6A"),
                                        _stat_card("Failed", str(failed), "#FF5252"),
                                        _stat_card("Skipped", str(skipped), "#FFB74D"),
                                        _stat_card("Errors", str(errors), "#FF1744"),
                                    ],
                                    spacing=12,
                                ),
                                ft.Container(height=8),
                                ft.Row(
                                    controls=[
                                        ft.Text("Pass Rate", size=sz(13), color=ON_SURFACE_DIM),
                                        ft.Container(expand=True),
                                        ft.Text(
                                            f"{pass_rate:.1f}%",
                                            size=sz(18),
                                            weight=ft.FontWeight.BOLD,
                                            color=rate_color,
                                        ),
                                    ],
                                ),
                                ft.ProgressBar(
                                    value=pass_rate / 100,
                                    color=rate_color,
                                    bgcolor=DIVIDER,
                                    bar_height=8,
                                    border_radius=4,
                                ),
                            ],
                            spacing=6,
                        ),
                        padding=20,
                        bgcolor=CARD_BG,
                        border_radius=12,
                        border=ft.Border.all(1, DIVIDER),
                    )
                except (json.JSONDecodeError, OSError, AttributeError, TypeError):
                    # Malformed run-results.json (e.g. unexpected root shape)
                    # should only drop the TEST RESULTS section, not the
                    # whole results view via the caller's fallback path.
                    pass

    # ── Collapsible log viewer ──────────────────────────────────────────
    # The summary table (+ test results) and the log panel share a fixed
    # vertical budget once logs are opened, split by a drag handle between
    # them — dragging up shrinks the top area and grows the log panel,
    # dragging down does the opposite. Before logs are shown, the top area
    # keeps its natural (unconstrained) height.
    _split: dict[str, float] = {"top": 480.0, "log": 400.0}
    _MIN_PANE = 150.0

    # `scroll` starts as None: with logs hidden, top_section.height is None
    # (unbounded), and a vertically-scrollable Column with unbounded height
    # trips Flutter's "Vertical viewport was given unbounded height" render
    # assertion — which crashes the flet-desktop client, reconnects, rebuilds
    # this same view, and crashes again (an endless flicker loop). The OUTER
    # Column already scrolls the whole view, so no inner scroll is needed
    # here. Inner scroll is only turned on when logs are shown, at which point
    # top_section gets a bounded height (see _toggle_logs) and scrolling is
    # safe.
    top_column = ft.Column(
        controls=[summary_table, ft.Container(height=12), test_results_section],
        spacing=0,
        scroll=None,
    )
    top_section = ft.Container(content=top_column, height=None)

    log_section = ft.Container(
        content=build_log_viewer(page, state),
        height=_split["log"],
        visible=False,
        border_radius=ft.BorderRadius(0, 0, 12, 12),
    )

    def _on_split_drag(e: ft.DragUpdateEvent) -> None:
        delta = e.local_delta.y if e.local_delta else (e.primary_delta or 0.0)
        total = _split["top"] + _split["log"]
        new_top = max(_MIN_PANE, min(total - _MIN_PANE, _split["top"] + delta))
        new_log = total - new_top
        _split["top"] = new_top
        _split["log"] = new_log
        top_section.height = new_top
        log_section.height = new_log
        import contextlib as _cl
        with _cl.suppress(Exception):
            page.update()

    resize_handle = ft.GestureDetector(
        content=ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=48,
                        height=4,
                        bgcolor=DIVIDER,
                        border_radius=2,
                    )
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            height=14,
            bgcolor=CARD_BG,
            border=ft.Border.all(1, DIVIDER),
            border_radius=ft.BorderRadius(12, 12, 0, 0),
            tooltip="Drag to resize the summary / logs split",
        ),
        mouse_cursor=ft.MouseCursor.RESIZE_ROW,
        drag_interval=10,
        on_vertical_drag_update=_on_split_drag,
        visible=False,
    )

    # ── Action buttons ───────────────────────────────────────────────────
    actions: list[ft.Control] = []

    if state.log_lines:
        log_btn = ft.OutlinedButton(
            "View Logs",
            icon=ft.Icons.TERMINAL,
        )

        def _toggle_logs(e: ft.ControlEvent) -> None:
            log_section.visible = not log_section.visible
            resize_handle.visible = log_section.visible
            # Only constrain the top area's height (and make it scrollable)
            # once the split is active — otherwise let it size naturally. The
            # inner scroll must follow the bounded height: enabling scroll on
            # an unbounded (height=None) Column crashes the Flutter renderer
            # (see the top_column comment above).
            top_section.height = _split["top"] if log_section.visible else None
            top_column.scroll = ft.ScrollMode.AUTO if log_section.visible else None
            log_section.height = _split["log"]
            log_btn.text = "Hide Logs" if log_section.visible else "View Logs"
            page.update()

        log_btn.on_click = _toggle_logs
        actions.append(log_btn)

    if state.workspace_path:
        def open_workspace(e: ft.ControlEvent) -> None:
            ws = state.workspace_path
            if ws and Path(ws).exists():
                if sys.platform == "win32":
                    os.startfile(ws)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", ws])
                else:
                    subprocess.Popen(["xdg-open", ws])

        actions.append(
            ft.OutlinedButton(
                "Open Workspace",
                icon=ft.Icons.FOLDER_OPEN,
                on_click=open_workspace,
            )
        )

        report_html = Path(state.workspace_path) / "artifacts" / "step11" / "index.html"
        if report_html.exists():
            def open_report(e: ft.ControlEvent) -> None:
                if sys.platform == "win32":
                    os.startfile(str(report_html))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(report_html)])
                else:
                    subprocess.Popen(["xdg-open", str(report_html)])

            actions.append(
                ft.OutlinedButton(
                    "Open Report",
                    icon=ft.Icons.DESCRIPTION,
                    on_click=open_report,
                )
            )

    actions.append(
        ft.ElevatedButton(
            "New Run",
            icon=ft.Icons.REPLAY,
            bgcolor=PRIMARY,
            color="#FFFFFF",
            on_click=lambda _: on_new_run(),
        )
    )

    actions_row = ft.Row(
        controls=actions,
        spacing=12,
        alignment=ft.MainAxisAlignment.CENTER,
    )

    # ── Assemble ─────────────────────────────────────────────────────────
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Container(height=16),
                banner,
                ft.Container(height=12),
                top_section,
                resize_handle,
                log_section,
                ft.Container(height=16),
                actions_row,
                ft.Container(height=24),
            ],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        expand=True,
        padding=ft.Padding.symmetric(horizontal=40),
        bgcolor=BACKGROUND,
    )


_AUX_PHASE_CODES = {
    "debug": "D",
    "critical_thinking": "C",
    "principal_engineer": "P",
}

_AUX_PHASE_LABELS = {
    "debug": "Debug agent",
    "critical_thinking": "Critical thinking",
    "principal_engineer": "Principal SW engineer",
}


def _aux_row(aux: AuxAgentUIState) -> ft.DataRow:
    """One data row per helper agent, styled distinctly (dim + italic) so
    it reads as a sub-item of the parent step rather than a real pipeline
    step."""
    code = _AUX_PHASE_CODES.get(aux.phase, "?")
    label = _AUX_PHASE_LABELS.get(aux.phase, aux.phase or aux.agent)
    status_color = STATUS_COLORS.get(aux.status, ON_SURFACE_DIM)
    return ft.DataRow(
        cells=[
            ft.DataCell(
                ft.Text(
                    f"{code}{aux.step}",
                    size=sz(11),
                    color=ON_SURFACE_DIM,
                    italic=True,
                )
            ),
            ft.DataCell(
                ft.Text(
                    f"{label} (step {aux.step:02d})",
                    size=sz(12),
                    color=ON_SURFACE_DIM,
                    italic=True,
                )
            ),
            ft.DataCell(
                ft.Container(
                    content=ft.Text(
                        aux.status,
                        size=sz(10),
                        color="#FFFFFF",
                        weight=ft.FontWeight.BOLD,
                    ),
                    bgcolor=status_color,
                    border_radius=4,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                )
            ),
            ft.DataCell(
                ft.Text(_fmt_elapsed(aux.duration_s), size=sz(12), color=ON_SURFACE_DIM)
            ),
            ft.DataCell(
                ft.Text(_fmt_tokens(aux.tokens_in), size=sz(12), color=ON_SURFACE_DIM)
            ),
            ft.DataCell(
                ft.Text(_fmt_tokens(aux.tokens_out), size=sz(12), color=ON_SURFACE_DIM)
            ),
            ft.DataCell(
                ft.Text(
                    _fmt_tokens(aux.cache_read) if aux.cache_read else "-",
                    size=sz(12),
                    color=ON_SURFACE_DIM,
                )
            ),
            ft.DataCell(
                ft.Text(
                    _fmt_tokens(aux.cache_write) if aux.cache_write else "-",
                    size=sz(12),
                    color=ON_SURFACE_DIM,
                )
            ),
            ft.DataCell(
                ft.Text(str(aux.agent_calls), size=sz(12), color=ON_SURFACE_DIM)
            ),
            ft.DataCell(
                ft.Text(
                    f"${aux.cost_usd:.2f}" if (aux.cost_usd or 0) > 0 else "-",
                    size=sz(12),
                    color="#FF5252" if (aux.cost_usd or 0) > 0 else ON_SURFACE_DIM,
                )
            ),
        ],
    )


def _stat_card(label: str, value: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(
                    value,
                    size=sz(24),
                    weight=ft.FontWeight.BOLD,
                    color=color,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    label,
                    size=sz(11),
                    color=ON_SURFACE_DIM,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=2,
        ),
        padding=ft.Padding.symmetric(horizontal=20, vertical=12),
        bgcolor=CARD_BG,
        border_radius=10,
        border=ft.Border.all(1, DIVIDER),
        expand=True,
    )
