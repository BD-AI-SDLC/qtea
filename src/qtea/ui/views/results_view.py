"""Post-run results summary dashboard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import flet as ft

from qtea.ui.state import STEP_DEFINITIONS, AppState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PHASE_COLORS,
    PRIMARY,
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


def _fmt_tokens(n: int) -> str:
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

    # ── Status banner ────────────────────────────────────────────────────
    banner_icon = ft.Icons.CHECK_CIRCLE if is_success else ft.Icons.ERROR
    banner_color = "#66BB6A" if is_success else "#FF5252"
    banner_text = "Pipeline Completed Successfully" if is_success else "Pipeline Failed"

    banner = ft.Container(
        content=ft.Row(
            controls=[
                ft.Icon(banner_icon, color=banner_color, size=36),
                ft.Column(
                    controls=[
                        ft.Text(
                            banner_text,
                            size=22,
                            weight=ft.FontWeight.BOLD,
                            color=banner_color,
                        ),
                        ft.Row(
                            controls=[
                                ft.Text(
                                    f"Run: {state.run_id or 'N/A'}",
                                    size=12,
                                    color=ON_SURFACE_DIM,
                                    font_family="Courier New",
                                ),
                                ft.Text("|", size=12, color=DIVIDER),
                                ft.Text(
                                    f"Duration: {_fmt_elapsed(state.elapsed_s)}",
                                    size=12,
                                    color=ON_SURFACE_DIM,
                                ),
                                ft.Text("|", size=12, color=DIVIDER),
                                ft.Text(
                                    f"Cost: ${state.total_cost:.2f}",
                                    size=12,
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
        phase_color = PHASE_COLORS.get(phase, ON_SURFACE_DIM)

        table_rows.append(
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(str(num), size=12, color=ON_SURFACE)),
                    ft.DataCell(ft.Text(name, size=12, color=ON_SURFACE)),
                    ft.DataCell(
                        ft.Container(
                            content=ft.Text(
                                s.status,
                                size=10,
                                color="#FFFFFF",
                                weight=ft.FontWeight.BOLD,
                            ),
                            bgcolor=status_color,
                            border_radius=4,
                            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                        )
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_elapsed(s.elapsed_s), size=12, color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_tokens(s.tokens_in), size=12, color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(_fmt_tokens(s.tokens_out), size=12, color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(str(s.agent_calls), size=12, color=ON_SURFACE_DIM)
                    ),
                    ft.DataCell(
                        ft.Text(
                            f"${s.cost_usd:.2f}" if s.cost_usd > 0 else "-",
                            size=12,
                            color="#FF5252" if s.cost_usd > 0 else ON_SURFACE_DIM,
                        )
                    ),
                ],
            )
        )

    # Totals row
    table_rows.append(
        ft.DataRow(
            cells=[
                ft.DataCell(ft.Text("", size=12)),
                ft.DataCell(
                    ft.Text("TOTAL", size=12, weight=ft.FontWeight.BOLD, color=ON_SURFACE)
                ),
                ft.DataCell(ft.Text("", size=12)),
                ft.DataCell(
                    ft.Text(
                        _fmt_elapsed(state.elapsed_s),
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_tokens_in),
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        _fmt_tokens(state.total_tokens_out),
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        str(state.total_agent_calls),
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    )
                ),
                ft.DataCell(
                    ft.Text(
                        f"${state.total_cost:.2f}",
                        size=12,
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
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                ft.Container(height=8),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("#", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Step", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Status", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Duration", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Tok In", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Tok Out", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Agents", size=11, color=ON_SURFACE_DIM)),
                        ft.DataColumn(ft.Text("Cost", size=11, color=ON_SURFACE_DIM)),
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
                                    size=10,
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
                                        ft.Text("Pass Rate", size=13, color=ON_SURFACE_DIM),
                                        ft.Container(expand=True),
                                        ft.Text(
                                            f"{pass_rate:.1f}%",
                                            size=18,
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
                except (json.JSONDecodeError, OSError):
                    pass

    # ── Action buttons ───────────────────────────────────────────────────
    actions: list[ft.Control] = []

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
                summary_table,
                ft.Container(height=12),
                test_results_section,
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


def _stat_card(label: str, value: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(
                    value,
                    size=24,
                    weight=ft.FontWeight.BOLD,
                    color=color,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    label,
                    size=11,
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
