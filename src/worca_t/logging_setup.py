"""Structured logging setup: structlog -> pretty console + JSONL file.

Console output uses ConsoleRenderer with a custom column list so that
specific key-value pairs render with distinct colors:

  - step, attempt          → bold blue  (pipeline position)
  - status                 → green / yellow / red  (outcome)
  - exit_code              → green (0) / red (non-zero)
  - duration_s             → yellow with "s" suffix  (timing)
  - error                  → red  (failure messages)
  - everything else        → cyan key + white value  (default)

The JSONL file is unchanged — one ISO-timestamped JSON object per line.
"""

from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path
from typing import Any

# Python 3.14 flags "\s" etc. as SyntaxWarning in third-party packages loaded
# at import time. These are harmless and unfixable from our side — suppress.
warnings.filterwarnings(
    "ignore",
    message=r'is an invalid escape sequence',
    category=SyntaxWarning,
)

import structlog
from structlog.dev import (
    _NOTHING,
    Column,
    KeyValueColumnFormatter,
    LogLevelColumnFormatter,
)
from structlog.types import EventDict, WrappedLogger

_CONFIGURED = False

_WHITE = "\x1b[37m"

# Header column keys — joined with space; all others joined with ", ".
_HEADER_KEYS = frozenset({"timestamp", "level", "event"})


class _CommaConsoleRenderer(structlog.dev.ConsoleRenderer):
    """ConsoleRenderer that separates key-value pairs with white commas."""

    def __call__(
        self, logger: WrappedLogger, name: str, event_dict: EventDict,
    ) -> str:
        from io import StringIO

        stack = event_dict.pop("stack", None)
        exc = event_dict.pop("exception", None)
        exc_info = event_dict.pop("exc_info", None)

        header_kvs: list[str] = []
        value_kvs: list[str] = []
        for col in self.columns:
            val = event_dict.pop(col.key, _NOTHING)
            if val is _NOTHING:
                continue
            formatted = col.formatter(col.key, val)
            if not formatted:
                continue
            if col.key in _HEADER_KEYS:
                header_kvs.append(formatted)
            else:
                value_kvs.append(formatted)
        for key in (sorted(event_dict) if self._sort_keys else list(event_dict)):
            formatted = self._default_column_formatter(key, event_dict[key])
            if formatted:
                value_kvs.append(formatted)

        use_colors = bool(self._styles.reset)
        sep = f"{_WHITE},{_RESET} " if use_colors else ", "

        sio = StringIO()
        parts = " ".join(header_kvs)
        if value_kvs:
            parts += " " + sep.join(value_kvs)
        sio.write(parts.rstrip())

        if stack is not None:
            sio.write("\n" + stack)
            if exc_info or exc is not None:
                sio.write("\n\n" + "=" * 79 + "\n")
        exc_info = structlog.dev._figure_out_exc_info(exc_info)
        if exc_info:
            self._exception_formatter(sio, exc_info)
        elif exc is not None:
            sio.write("\n" + exc)
        return sio.getvalue()


# Events that get a blank line before them so phase boundaries are visible.
_BLANK_LINE_BEFORE = frozenset({
    "pipeline.start",
    "pipeline.finished",
    "pipeline.aborted",
    "agent.start",
})

# Keys redundant on the console (constant across the run; kept in JSONL).
_CONSOLE_DROP_KEYS = frozenset({"run_id"})

# ---------------------------------------------------------------------------
# ANSI codes (intentionally not using colorama to keep zero extra deps)
# ---------------------------------------------------------------------------
_RESET   = "\x1b[0m"
_BOLD    = "\x1b[1m"
_DIM     = "\x1b[2m"
_RED     = "\x1b[31m"
_GREEN   = "\x1b[32m"
_YELLOW  = "\x1b[33m"
_BLUE    = "\x1b[34m"
_MAGENTA = "\x1b[35m"
_CYAN    = "\x1b[36m"
_ORANGE  = "\x1b[38;5;208m"

# Level badge colors (bold variant mirrors ConsoleRenderer defaults).
_LEVEL_STYLES = {
    "critical":  _RED    + _BOLD,
    "exception": _RED    + _BOLD,
    "error":     _RED    + _BOLD,
    "warn":      _YELLOW + _BOLD,
    "warning":   _YELLOW + _BOLD,
    "info":      _GREEN  + _BOLD,
    "debug":     _GREEN  + _BOLD,
    "notset":    "\x1b[41m" + _BOLD,
}


def _value_repr(val: object) -> str:
    """Quote strings that contain whitespace or special chars; repr everything else."""
    if isinstance(val, str):
        if set(val) & {" ", "\t", "=", "\r", "\n", '"', "'"}:
            return repr(val)
        return val
    return repr(val)


def _build_columns(use_colors: bool) -> list[Column]:
    """Build the ConsoleRenderer column list.

    Named columns are rendered first in declared order; the trailing
    ``Column("")`` catches all remaining keys with the default style.
    """
    C = lambda s: s if use_colors else ""  # noqa: E731

    # ── Value repr helpers (conditional on use_colors) ──────────────────

    def _status_repr(val: object) -> str:
        s = str(val)
        if s == "completed":
            return C(_GREEN) + s
        if s == "warned":
            return C(_YELLOW) + s
        if s == "failed":
            return C(_RED) + s
        return s

    def _exit_code_repr(val: object) -> str:
        return (C(_GREEN) if val == 0 else C(_RED)) + str(val)

    def _duration_repr(val: object) -> str:
        if isinstance(val, (int, float)):
            return f"{val:.1f}s"
        return str(val)

    def _attempt_repr(val: object) -> str:
        return (C(_DIM) if val == 1 else C(_YELLOW + _BOLD)) + str(val)

    KV = KeyValueColumnFormatter  # alias for brevity

    return [
        # ── Fixed header: timestamp · level · event ──────────────────────
        Column("timestamp", KV(
            key_style=None,
            value_style=C(_DIM),
            reset_style=C(_RESET),
            value_repr=str,
        )),
        Column("level", LogLevelColumnFormatter(
            level_styles={k: C(v) for k, v in _LEVEL_STYLES.items()},
            reset_style=C(_RESET),
            width=0,
        )),
        Column("event", KV(
            key_style=None,
            value_style=C(_BOLD),
            reset_style=C(_RESET),
            value_repr=str,
            width=35,
        )),
        # ── Domain keys with semantic colors ─────────────────────────────
        Column("step", KV(
            key_style=C(_CYAN),
            value_style=C(_BOLD + _BLUE),
            reset_style=C(_RESET),
            value_repr=str,
        )),
        Column("attempt", KV(
            key_style=C(_CYAN),
            value_style="",
            reset_style=C(_RESET),
            value_repr=_attempt_repr,
        )),
        Column("name", KV(
            key_style=C(_CYAN),
            value_style=C(_BLUE),
            reset_style=C(_RESET),
            value_repr=str,
        )),
        Column("status", KV(
            key_style=C(_CYAN),
            value_style="",
            reset_style=C(_RESET),
            value_repr=_status_repr,
        )),
        Column("exit_code", KV(
            key_style=C(_CYAN),
            value_style="",
            reset_style=C(_RESET),
            value_repr=_exit_code_repr,
        )),
        Column("duration_s", KV(
            key_style=C(_CYAN),
            value_style=C(_YELLOW),
            reset_style=C(_RESET),
            value_repr=_duration_repr,
        )),
        Column("error", KV(
            key_style=C(_CYAN),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        # ── Token / cost accounting (red — draws attention to LLM spend) ─
        Column("tokens_input", KV(
            key_style=C(_RED),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        Column("tokens_output", KV(
            key_style=C(_RED),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        Column("tokens_cache_read", KV(
            key_style=C(_RED),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        Column("token_cache_write", KV(
            key_style=C(_RED),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        Column("cost_usd", KV(
            key_style=C(_RED),
            value_style=C(_RED),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        # ── Model (orange — shows which LLM model is in use) ─────────────
        Column("model_used", KV(
            key_style=C(_ORANGE),
            value_style=C(_ORANGE + _BOLD),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        # ── Agent (green — shows which LLM agent is active) ──────────────
        Column("agent", KV(
            key_style=C(_GREEN),
            value_style=C(_GREEN + _BOLD),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
        # ── Default: all remaining key-value pairs ────────────────────────
        Column("", KV(
            key_style=C(_CYAN),
            value_style=C(_WHITE),
            reset_style=C(_RESET),
            value_repr=_value_repr,
        )),
    ]


def _make_console_processors(use_colors: bool) -> list[Any]:
    """Return the processor list for the console handler."""

    def _drop_noise(
        logger: Any, method: str, event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        for k in _CONSOLE_DROP_KEYS:
            event_dict.pop(k, None)
        drop = [k for k, v in event_dict.items()
                if v is None or v is False]
        for k in drop:
            event_dict.pop(k)
        return event_dict

    def _blank_line_before_phase(
        logger: Any, method: str, event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event = event_dict.get("event") or ""
        if event in _BLANK_LINE_BEFORE:
            print("", file=sys.stderr, flush=True)
        return event_dict

    return [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        _drop_noise,
        _blank_line_before_phase,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        _CommaConsoleRenderer(
            columns=_build_columns(use_colors),
            sort_keys=False,
        ),
    ]


def configure_logging(
    *,
    level: str = "info",
    jsonl_path: Path | None = None,
    run_id: str | None = None,
) -> structlog.stdlib.BoundLogger:
    """Configure structlog. Idempotent for repeated calls within a process."""
    global _CONFIGURED

    lvl = getattr(logging, level.upper(), logging.INFO)

    # Shared pre-processors: run for every output channel.
    # No renderer here — deferred to per-handler ProcessorFormatter.
    shared_pre: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    structlog.configure(
        processors=shared_pre,
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handlers: list[logging.Handler] = []

    # Console handler: colored, human-readable.
    # Skipped when WORCA_T_UI_MODE is set — the desktop UI owns the screen,
    # so duplicating events into stderr is noise. (The JSONL audit trail is
    # still written below.)
    import os as _os
    if not _os.environ.get("WORCA_T_UI_MODE"):
        use_colors = sys.stderr.isatty()
        console_h = logging.StreamHandler(stream=sys.stderr)
        console_h.setLevel(lvl)
        console_h.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=_make_console_processors(use_colors),
            )
        )
        handlers.append(console_h)

    # JSONL file handler: machine-readable audit trail (unchanged behavior).
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.FileHandler(jsonl_path, encoding="utf-8")
        file_h.setLevel(lvl)
        file_h.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.TimeStamper(fmt="iso", utc=True),
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        handlers.append(file_h)

    root = logging.getLogger()
    for h in list(root.handlers):
        # Preserve handlers that have explicitly opted out of removal
        # (e.g. the desktop-UI event bridge tagged with _worca_t_keep).
        if getattr(h, "_worca_t_keep", False):
            continue
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(lvl)

    # Suppress SDK internal INFO chatter while keeping WARNING/ERROR visible.
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

    log = structlog.get_logger("worca_t")
    if run_id:
        structlog.contextvars.bind_contextvars(run_id=run_id)
    _CONFIGURED = True
    return log


def get_logger(name: str = "worca_t") -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
