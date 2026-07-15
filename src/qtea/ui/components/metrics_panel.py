"""Live metrics sidebar — cost, tokens, elapsed, per-step breakdown."""

from __future__ import annotations

import flet as ft

from qtea.ui.state import STEP_DEFINITIONS, AppState
from qtea.ui.theme import (
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PHASE_COLORS,
    SECONDARY,
    sz,
)


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def _fmt_cost(usd: float) -> str:
    return f"${usd:.2f}"


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _metric_row(label: str, value: str, color: str = ON_SURFACE) -> ft.Row:
    return ft.Row(
        controls=[
            ft.Text(label, size=sz(12), color=ON_SURFACE_DIM),
            ft.Container(expand=True),
            ft.Text(value, size=sz(13), weight=ft.FontWeight.W_600, color=color),
        ],
        spacing=4,
    )


def _build_cost_bars(state: AppState) -> ft.Column:
    """Per-step cost breakdown as horizontal bars."""
    max_cost = max((s.cost_usd for s in state.steps.values()), default=0.01)
    max_cost = max(max_cost, 0.01)

    bars: list[ft.Control] = []
    for num, _name, phase in STEP_DEFINITIONS:
        s = state.steps.get(num)
        if not s or s.cost_usd < 0.001:
            continue
        pct = min(s.cost_usd / max_cost, 1.0)
        phase_color = PHASE_COLORS.get(phase, "#616161")
        bars.append(
            ft.Row(
                controls=[
                    ft.Text(f"{num:02d}", size=sz(10), color=ON_SURFACE_DIM, width=20),
                    ft.Container(
                        content=ft.Container(
                            bgcolor=phase_color,
                            border_radius=2,
                            width=max(pct * 100, 2),
                            height=10,
                        ),
                        expand=True,
                        alignment=ft.Alignment.CENTER_LEFT,
                    ),
                    ft.Text(
                        f"${s.cost_usd:.2f}",
                        size=sz(10),
                        color=ON_SURFACE_DIM,
                        width=50,
                        text_align=ft.TextAlign.RIGHT,
                    ),
                ],
                spacing=4,
            )
        )

    if not bars:
        return ft.Column()

    return ft.Column(
        controls=[
            ft.Container(height=6),
            ft.Text(
                "COST PER STEP",
                size=sz(10),
                weight=ft.FontWeight.BOLD,
                color=ON_SURFACE_DIM,
            ),
            ft.Container(height=4),
            *bars,
        ],
        spacing=3,
    )


def build_metrics_panel(state: AppState) -> ft.Container:
    """Build the right-side metrics sidebar."""

    # Current step info
    current_info: list[ft.Control] = []
    if state.current_step and state.current_step in state.steps:
        s = state.steps[state.current_step]
        current_info = [
            ft.Text(
                f"Step {s.number}: {s.name}",
                size=sz(14),
                weight=ft.FontWeight.W_600,
                color=SECONDARY,
            ),
            ft.Text(
                f"Phase {s.phase}",
                size=sz(12),
                color=PHASE_COLORS.get(s.phase, ON_SURFACE_DIM),
            ),
            ft.Container(height=4),
            ft.Divider(height=1, color=DIVIDER),
            ft.Container(height=4),
        ]
    elif state.run_status == "running":
        current_info = [
            ft.Text("Initializing...", size=sz(14), color=ON_SURFACE_DIM, italic=True),
            ft.Container(height=8),
        ]

    # Metrics
    metrics = ft.Column(
        controls=[
            ft.Text(
                "METRICS",
                size=sz(10),
                weight=ft.FontWeight.BOLD,
                color=ON_SURFACE_DIM,
            ),
            ft.Container(height=6),
            _metric_row("Elapsed", _fmt_elapsed(state.elapsed_s)),
            _metric_row("Total Cost", _fmt_cost(state.total_cost), "#FF5252"),
            _metric_row("Tokens In", _fmt_tokens(state.total_tokens_in)),
            _metric_row("Tokens Out", _fmt_tokens(state.total_tokens_out)),
            _metric_row("Cache Read", _fmt_tokens(state.total_cache_read)),
            _metric_row("Cache Write", _fmt_tokens(state.total_cache_write)),
            _metric_row("Agent Calls", str(state.total_agent_calls)),
        ],
        spacing=6,
    )

    # Cost breakdown
    cost_bars = _build_cost_bars(state)

    return ft.Container(
        content=ft.Column(
            controls=[
                *current_info,
                metrics,
                cost_bars,
            ],
            spacing=4,
            scroll=ft.ScrollMode.AUTO,
        ),
        padding=16,
        bgcolor=CARD_BG,
        border_radius=12,
        border=ft.Border.all(1, DIVIDER),
    )
