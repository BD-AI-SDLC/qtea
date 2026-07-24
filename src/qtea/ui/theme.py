"""Brand colors, dark theme constants, and status styling."""

from __future__ import annotations

import flet as ft

# ── Brand palette ────────────────────────────────────────────────────────────

PRIMARY = "#6C63FF"
SECONDARY = "#00BFA5"
SURFACE = "#1E1E2E"
BACKGROUND = "#121218"
ON_SURFACE = "#E0E0E0"
ON_SURFACE_DIM = "#9E9E9E"
CARD_BG = "#252536"
DIVIDER = "#2E2E42"

# ── Phase colors ─────────────────────────────────────────────────────────────

PHASE_COLORS: dict[str, str] = {
    "A": "#7C4DFF",
    "B": "#00BFA5",
    "C": "#FF6E40",
}

PHASE_LABELS: dict[str, str] = {
    "A": "Requirements",
    "B": "Research & Codegen",
    "C": "Execute & Report",
}

# ── Step status colors ───────────────────────────────────────────────────────

STATUS_COLORS: dict[str, str] = {
    "pending": "#616161",
    "in_progress": "#00BFA5",
    "completed": "#66BB6A",
    "failed": "#FF5252",
    "warned": "#FFB74D",
    "skipped": "#757575",
}

STATUS_ICONS: dict[str, str] = {
    "pending": ft.Icons.CIRCLE_OUTLINED,
    "in_progress": ft.Icons.PLAY_CIRCLE_FILLED,
    "completed": ft.Icons.CHECK_CIRCLE,
    "failed": ft.Icons.ERROR,
    "warned": ft.Icons.WARNING_ROUNDED,
    "skipped": ft.Icons.SKIP_NEXT,
}

# ── Log level colors ─────────────────────────────────────────────────────────

LOG_LEVEL_COLORS: dict[str, str] = {
    "info": "#66BB6A",
    "debug": "#9E9E9E",
    "warning": "#FFB74D",
    "error": "#FF5252",
    "critical": "#FF1744",
}

# Highlight color for the currently-invoked agent in the log stream.
LOG_AGENT_COLOR = "#00E5FF"
LOG_MODEL_COLOR = "#FFD54F"
LOG_TOKENS_COLOR = "#FF5252"
# Orange highlight for a leading step token (e.g. "step08") in an event name.
LOG_STEP_COLOR = "#FF9800"
# Orange highlight for an `error=...` field on any log line (e.g. step.end
# on a failed step) — draws the eye to the failure reason without changing
# the INFO-level badge color.
LOG_ERROR_FIELD_COLOR = "#FF9800"

# ── Text/icon scale (Ctrl+/Ctrl- zoom) ──────────────────────────────────────

MIN_SCALE = 0.7
MAX_SCALE = 1.8
SCALE_STEP = 0.1

_scale = 1.0


def get_scale() -> float:
    return _scale


def set_scale(value: float) -> float:
    """Clamp ``value`` to [MIN_SCALE, MAX_SCALE], store it, and return it."""
    global _scale
    _scale = round(min(MAX_SCALE, max(MIN_SCALE, value)), 2)
    return _scale


def sz(base: int | float) -> int:
    """Scale a base font/icon size by the current zoom level."""
    return round(base * _scale)


# ── Theme factory ────────────────────────────────────────────────────────────


def build_dark_theme() -> ft.Theme:
    return ft.Theme(
        color_scheme_seed=PRIMARY,
        color_scheme=ft.ColorScheme(
            primary=PRIMARY,
            secondary=SECONDARY,
            error="#FF5252",
            surface=SURFACE,
            on_surface=ON_SURFACE,
        ),
    )
