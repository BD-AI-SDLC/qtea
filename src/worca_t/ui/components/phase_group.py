"""Phase group container — groups step cards under A/B/C labels."""

from __future__ import annotations

import flet as ft

from worca_t.ui.state import AppState
from worca_t.ui.theme import DIVIDER, ON_SURFACE, ON_SURFACE_DIM, PHASE_COLORS, PHASE_LABELS

from .step_card import build_step_card


def build_phase_group(phase: str, state: AppState, on_step_click=None) -> ft.Container:
    """Build a phase group (A, B, or C) with its step cards.

    Args:
        phase: Phase letter ('A', 'B', or 'C').
        state: AppState instance.
        on_step_click: Optional ``callable(step)`` invoked when a step
            card is clicked.
    """

    phase_color = PHASE_COLORS.get(phase, "#616161")
    phase_label = PHASE_LABELS.get(phase, phase)

    # Gather steps for this phase
    phase_steps = sorted(
        [s for s in state.steps.values() if s.phase == phase],
        key=lambda s: s.number,
    )

    completed = sum(
        1 for s in phase_steps if s.status in ("completed", "skipped", "warned")
    )
    total = len(phase_steps)

    # Phase header
    header = ft.Row(
        controls=[
            ft.Container(
                content=ft.Text(
                    phase,
                    size=12,
                    weight=ft.FontWeight.BOLD,
                    color="#FFFFFF",
                    text_align=ft.TextAlign.CENTER,
                ),
                width=26,
                height=26,
                bgcolor=phase_color,
                border_radius=13,
                alignment=ft.Alignment.CENTER,
            ),
            ft.Text(
                phase_label,
                size=13,
                weight=ft.FontWeight.W_600,
                color=ON_SURFACE,
            ),
            ft.Container(expand=True),
            ft.Text(
                f"{completed}/{total}",
                size=12,
                color=ON_SURFACE_DIM,
            ),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    # Step cards
    cards = [build_step_card(s, on_click=on_step_click) for s in phase_steps]

    return ft.Container(
        content=ft.Column(
            controls=[header, *cards],
            spacing=6,
        ),
        padding=ft.Padding.only(bottom=12),
        border=ft.Border.only(bottom=ft.BorderSide(1, DIVIDER)),
    )
