"""Animated step card component — the core building block of the pipeline view."""

from __future__ import annotations

import flet as ft

from qtea.ui.state import StepUIState
from qtea.ui.theme import (
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PHASE_COLORS,
    STATUS_COLORS,
    STATUS_ICONS,
)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _fmt_cost(usd: float) -> str:
    if usd < 0.01:
        return ""
    return f"${usd:.2f}"


def build_step_card(step: StepUIState, on_click=None) -> ft.Container:
    """Build a single animated step card.

    Args:
        step: The step state to render.
        on_click: Optional callback. When provided, the card becomes
            interactive and invokes ``on_click(step)`` when clicked.
    """

    status_color = STATUS_COLORS.get(step.status, "#616161")
    icon_name = STATUS_ICONS.get(step.status, ft.Icons.CIRCLE_OUTLINED)
    phase_color = PHASE_COLORS.get(step.phase, "#616161")
    is_active = step.status == "in_progress"

    # Status icon
    status_icon = ft.Icon(icon_name,
        color=status_color,
        size=22,
    )

    # Step number + name
    step_label = ft.Text(
        f"{step.number:02d}  {step.name}",
        size=14,
        weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.W_400,
        color=ON_SURFACE if step.status != "pending" else ON_SURFACE_DIM,
    )

    # Phase badge
    phase_badge = ft.Container(
        content=ft.Text(
            step.phase,
            size=10,
            weight=ft.FontWeight.BOLD,
            color="#FFFFFF",
        ),
        bgcolor=phase_color,
        border_radius=4,
        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
    )

    # Top row: icon + name + phase badge
    top_row = ft.Row(
        controls=[status_icon, step_label, ft.Container(expand=True), phase_badge],
        alignment=ft.MainAxisAlignment.START,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=8,
    )

    # Detail row: elapsed, attempts, cost
    detail_parts: list[ft.Control] = []

    if step.status in ("in_progress", "completed", "failed", "warned"):
        elapsed_text = ft.Text(
            _fmt_elapsed(step.elapsed_s),
            size=12,
            color=ON_SURFACE_DIM,
            data="live_step_elapsed" if step.status == "in_progress" else None,
        )
        detail_parts.append(elapsed_text)

    if step.attempts > 1:
        detail_parts.append(
            ft.Container(
                content=ft.Text(
                    f"attempt {step.attempts}",
                    size=10,
                    color="#FFB74D",
                ),
                bgcolor="#3E2E00",
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
            )
        )

    cost_str = _fmt_cost(step.cost_usd)
    if cost_str:
        detail_parts.append(
            ft.Text(cost_str, size=12, color="#FF5252"),
        )

    if step.sub_status:
        label = step.sub_status.replace("_", " ")
        sub_color = "#66BB6A" if step.sub_status == "all_passed" else "#FFB74D"
        detail_parts.append(
            ft.Container(
                content=ft.Text(label, size=10, color=sub_color),
                bgcolor="#1B2E1B" if step.sub_status == "all_passed" else "#3E2E00",
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=6, vertical=1),
            )
        )

    if step.agent_name and is_active:
        detail_parts.append(
            ft.Text(
                step.agent_name,
                size=11,
                color="#00BFA5",
                italic=True,
            )
        )

    detail_row = ft.Row(
        controls=detail_parts,
        spacing=10,
        alignment=ft.MainAxisAlignment.START,
    ) if detail_parts else ft.Container(height=0)

    # Error display
    error_row = ft.Container(height=0)
    if step.error and step.status == "failed":
        error_row = ft.Text(
            step.error[:120],
            size=11,
            color="#FF5252",
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

    # Card container with animated border
    border_color = status_color if is_active else DIVIDER
    border_width = 2 if is_active else 1

    card = ft.Container(
        content=ft.Column(
            controls=[top_row, detail_row, error_row],
            spacing=4,
            tight=True,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        bgcolor=CARD_BG,
        border=ft.Border.all(border_width, border_color),
        border_radius=10,
        animate=ft.Animation(400, ft.AnimationCurve.EASE_IN_OUT),
        animate_scale=ft.Animation(300, ft.AnimationCurve.BOUNCE_OUT),
        scale=1.0,
        ink=on_click is not None,
        on_click=(lambda e: on_click(step)) if on_click else None,
        tooltip="Click for details" if on_click else None,
    )

    return card
