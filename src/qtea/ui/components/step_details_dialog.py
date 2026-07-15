"""Modal dialog showing detailed info for a selected step."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import flet as ft

from qtea.ui.state import AppState, StepUIState
from qtea.ui.theme import (
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PHASE_COLORS,
    SECONDARY,
    STATUS_COLORS,
    STATUS_ICONS,
    sz,
)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
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


def _open_in_explorer(path: Path) -> None:
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _metric_row(label: str, value: str, value_color: str = ON_SURFACE) -> ft.Row:
    return ft.Row(
        controls=[
            ft.Text(label, size=sz(12), color=ON_SURFACE_DIM, width=120),
            ft.Text(value, size=sz(13), weight=ft.FontWeight.W_500, color=value_color),
        ],
        spacing=8,
    )


def show_step_details_dialog(
    page: ft.Page,
    state: AppState,
    step: StepUIState,
) -> None:
    """Open a modal dialog with all details for one step."""
    status_color = STATUS_COLORS.get(step.status, ON_SURFACE_DIM)
    phase_color = PHASE_COLORS.get(step.phase, ON_SURFACE_DIM)
    icon_name = STATUS_ICONS.get(step.status, ft.Icons.CIRCLE_OUTLINED)

    # ── Header ──────────────────────────────────────────────────────────
    header = ft.Row(
        controls=[
            ft.Icon(icon_name, color=status_color, size=sz(28)),
            ft.Column(
                controls=[
                    ft.Text(
                        f"Step {step.number}: {step.name}",
                        size=sz(18),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE,
                    ),
                    ft.Row(
                        controls=[
                            ft.Container(
                                content=ft.Text(
                                    step.phase,
                                    size=sz(10),
                                    weight=ft.FontWeight.BOLD,
                                    color="#FFFFFF",
                                ),
                                bgcolor=phase_color,
                                border_radius=4,
                                padding=ft.Padding.symmetric(
                                    horizontal=6, vertical=2
                                ),
                            ),
                            ft.Container(
                                content=ft.Text(
                                    step.status.upper(),
                                    size=sz(10),
                                    weight=ft.FontWeight.BOLD,
                                    color="#FFFFFF",
                                ),
                                bgcolor=status_color,
                                border_radius=4,
                                padding=ft.Padding.symmetric(
                                    horizontal=6, vertical=2
                                ),
                            ),
                        ],
                        spacing=6,
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    # ── Metrics ─────────────────────────────────────────────────────────
    metrics_controls: list[ft.Control] = [
        _metric_row("Duration", _fmt_elapsed(step.elapsed_s)),
        _metric_row("Attempts", str(step.attempts)),
    ]
    if step.agent_name:
        metrics_controls.append(
            _metric_row("Agent", step.agent_name, SECONDARY)
        )
    if step.sub_status:
        sub_color = "#66BB6A" if step.sub_status == "all_passed" else "#FFB74D"
        metrics_controls.append(
            _metric_row("Sub-status", step.sub_status.replace("_", " "), sub_color)
        )
    if step.tokens_in or step.tokens_out:
        metrics_controls.extend([
            _metric_row("Tokens In", _fmt_tokens(step.tokens_in)),
            _metric_row("Tokens Out", _fmt_tokens(step.tokens_out)),
        ])
    if step.cache_read or step.cache_write:
        metrics_controls.extend([
            _metric_row("Cache Read", _fmt_tokens(step.cache_read)),
            _metric_row("Cache Write", _fmt_tokens(step.cache_write)),
        ])
    # Always render cost — the sidebar shows $0.00 unconditionally, the
    # step-details dialog should match so users don't wonder where it went.
    cost_color = "#FF5252" if step.cost_usd > 0 else ON_SURFACE_DIM
    cost_str = (
        f"${step.cost_usd:.4f}" if step.cost_usd > 0 else "$0.00"
    )
    metrics_controls.append(_metric_row("Cost", cost_str, cost_color))
    if step.agent_calls:
        metrics_controls.append(_metric_row("Agent Calls", str(step.agent_calls)))

    metrics_section = ft.Container(
        content=ft.Column(controls=metrics_controls, spacing=6),
        padding=12,
        bgcolor=CARD_BG,
        border_radius=8,
        border=ft.Border.all(1, DIVIDER),
    )

    # ── Notes / Error ───────────────────────────────────────────────────
    extra_sections: list[ft.Control] = []

    if step.notes:
        extra_sections.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "NOTES",
                            size=sz(10),
                            weight=ft.FontWeight.BOLD,
                            color=ON_SURFACE_DIM,
                        ),
                        ft.Text(
                            step.notes,
                            size=sz(12),
                            color=ON_SURFACE,
                            selectable=True,
                        ),
                    ],
                    spacing=4,
                ),
                padding=12,
                bgcolor=CARD_BG,
                border_radius=8,
                border=ft.Border.all(1, DIVIDER),
            )
        )

    if step.error and step.status == "failed":
        extra_sections.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "ERROR",
                            size=sz(10),
                            weight=ft.FontWeight.BOLD,
                            color="#FF5252",
                        ),
                        ft.Text(
                            step.error,
                            size=sz(12),
                            color="#FF5252",
                            selectable=True,
                        ),
                    ],
                    spacing=4,
                ),
                padding=12,
                bgcolor="#2A1818",
                border_radius=8,
                border=ft.Border.all(1, "#552020"),
            )
        )

    # ── Artifact actions ────────────────────────────────────────────────
    action_buttons: list[ft.Control] = []

    if state.workspace_path:
        ws = Path(state.workspace_path)
        artifact_dir = ws / "artifacts" / f"step{step.number:02d}"
        workdir = ws / f"step-{step.number:02d}"
        debug_dir = ws / "debug"

        if artifact_dir.exists():
            action_buttons.append(
                ft.OutlinedButton(
                    content="Open Artifacts",
                    icon=ft.Icons.FOLDER_OPEN,
                    on_click=lambda e, p=artifact_dir: _open_in_explorer(p),
                )
            )

        if workdir.exists():
            action_buttons.append(
                ft.OutlinedButton(
                    content="Open Workdir",
                    icon=ft.Icons.FOLDER_SPECIAL,
                    on_click=lambda e, p=workdir: _open_in_explorer(p),
                )
            )

        # Debug RCA
        if debug_dir.exists():
            rca_files = sorted(debug_dir.glob(
                f"step-{step.number:02d}-attempt*-debug-rca.md"
            ))
            if rca_files:
                latest_rca = rca_files[-1]
                action_buttons.append(
                    ft.OutlinedButton(
                        content="Open Debug RCA",
                        icon=ft.Icons.BUG_REPORT,
                        style=ft.ButtonStyle(color="#FFB74D"),
                        on_click=lambda e, p=latest_rca: _open_in_explorer(p),
                    )
                )

            fix_files = sorted(debug_dir.glob(
                f"step-{step.number:02d}-fix-proposal.md"
            ))
            if fix_files:
                action_buttons.append(
                    ft.OutlinedButton(
                        content="Open Fix Proposal",
                        icon=ft.Icons.AUTO_FIX_HIGH,
                        style=ft.ButtonStyle(color=SECONDARY),
                        on_click=lambda e, p=fix_files[0]: _open_in_explorer(p),
                    )
                )

        # Artifact file list
        if artifact_dir.exists():
            files = sorted(artifact_dir.iterdir())
            if files:
                file_rows: list[ft.Control] = []
                for f in files[:20]:  # cap at 20 items
                    file_rows.append(
                        ft.Row(
                            controls=[
                                ft.Icon(
                                    ft.Icons.INSERT_DRIVE_FILE,
                                    size=sz(14),
                                    color=ON_SURFACE_DIM,
                                ),
                                ft.Text(
                                    f.name,
                                    size=sz(12),
                                    color=ON_SURFACE,
                                    expand=True,
                                    selectable=True,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.OPEN_IN_NEW,
                                    icon_size=sz(14),
                                    tooltip="Open",
                                    on_click=lambda e, p=f: _open_in_explorer(p),
                                ),
                            ],
                            spacing=6,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    )
                if len(files) > 20:
                    file_rows.append(
                        ft.Text(
                            f"... +{len(files) - 20} more",
                            size=sz(11),
                            color=ON_SURFACE_DIM,
                            italic=True,
                        )
                    )
                extra_sections.append(
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "ARTIFACTS",
                                    size=sz(10),
                                    weight=ft.FontWeight.BOLD,
                                    color=ON_SURFACE_DIM,
                                ),
                                *file_rows,
                            ],
                            spacing=4,
                        ),
                        padding=12,
                        bgcolor=CARD_BG,
                        border_radius=8,
                        border=ft.Border.all(1, DIVIDER),
                    )
                )

    # ── Dialog ──────────────────────────────────────────────────────────
    def on_close(e):
        state.active_step_dialog_num = None
        page.pop_dialog()

    dlg_content = ft.Column(
        controls=[
            header,
            ft.Container(height=8),
            metrics_section,
            *extra_sections,
        ],
        spacing=10,
        scroll=ft.ScrollMode.AUTO,
        tight=True,
    )

    dlg = ft.AlertDialog(
        modal=True,
        title=None,
        content=ft.Container(
            content=dlg_content,
            width=620,
            height=min(560, 200 + len(extra_sections) * 120),
        ),
        actions=[
            *action_buttons,
            ft.TextButton("Close", on_click=on_close),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    state.active_step_dialog_num = step.number
    page.show_dialog(dlg)
