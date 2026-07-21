"""Pre-run operator context capture — shown between config and launch.

Optional free-text guidance about the spec that the operator supplies at run
start. It is inlined as trusted guidance into Step 1 ticket enrichment and
Step 2 refinement (see ``PipelineOptions.operator_context``). Skipping leaves
it empty, in which case pipeline behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from qtea.ui.state import AppState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE_DIM,
    PRIMARY,
    SECONDARY,
    sz,
)

_PLACEHOLDER = (
    "Optional. Anything not in the ticket that helps sharpen the spec:\n"
    "  • Environment / URLs — e.g. staging is at https://stg.example\n"
    "  • Scope focus — e.g. concentrate on the checkout path\n"
    "  • Known-flaky areas — e.g. the card-number field is slow to load\n"
    "  • Out of scope — e.g. ignore the legacy admin panel\n"
    "  • Domain terms — e.g. what 'SKU' means here"
)


def build_context_capture_view(
    page: ft.Page,
    state: AppState,
    on_skip: Callable[[], None],
    on_continue: Callable[[str], None],
) -> ft.Container:
    """Build the pre-run operator-context capture view.

    ``on_skip`` launches the run with no context; ``on_continue`` receives the
    entered text and launches the run with it.
    """

    context_field = ft.TextField(
        label="Context about the requirement (optional)",
        hint_text=_PLACEHOLDER,
        value=state.operator_context,
        multiline=True,
        min_lines=8,
        max_lines=18,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=sz(13),
        expand=True,
        on_change=lambda e: setattr(state, "operator_context", e.data or ""),
    )

    skip_button = ft.TextButton(
        content=ft.Text("Skip", size=sz(15)),
        style=ft.ButtonStyle(
            color=ON_SURFACE_DIM,
            padding=ft.Padding.symmetric(horizontal=24, vertical=16),
        ),
        on_click=lambda _: on_skip(),
    )

    continue_button = ft.ElevatedButton(
        content="Continue",
        icon=ft.Icons.ARROW_FORWARD,
        bgcolor=PRIMARY,
        color="#FFFFFF",
        style=ft.ButtonStyle(
            padding=ft.Padding.symmetric(horizontal=32, vertical=16),
            text_style=ft.TextStyle(size=sz(16), weight=ft.FontWeight.BOLD),
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
        on_click=lambda _: on_continue(context_field.value or ""),
    )

    card = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.LIGHTBULB_OUTLINE, color=PRIMARY, size=sz(28)),
                        ft.Text(
                            "Add context before the run",
                            size=sz(24),
                            weight=ft.FontWeight.BOLD,
                            color=PRIMARY,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=12,
                ),
                ft.Text(
                    "Trusted guidance that sharpens spec refinement. It "
                    "augments the ticket — it never overrides the acceptance "
                    "criteria. Leave blank to skip.",
                    size=sz(13),
                    color=ON_SURFACE_DIM,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Container(height=12),
                context_field,
                ft.Container(height=16),
                ft.Row(
                    controls=[skip_button, continue_button],
                    alignment=ft.MainAxisAlignment.END,
                    spacing=12,
                ),
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        ),
        width=760,
        padding=32,
        bgcolor=CARD_BG,
        border_radius=16,
        border=ft.Border.all(1, DIVIDER),
    )

    return ft.Container(
        content=card,
        expand=True,
        alignment=ft.Alignment.CENTER,
        bgcolor=BACKGROUND,
        padding=ft.Padding.symmetric(vertical=20),
    )
