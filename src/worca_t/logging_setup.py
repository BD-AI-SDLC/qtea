"""Structured logging setup: structlog -> pretty console + JSONL file."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(
    *,
    level: str = "info",
    jsonl_path: Path | None = None,
    run_id: str | None = None,
) -> structlog.stdlib.BoundLogger:
    """Configure structlog. Idempotent for repeated calls within a process."""
    global _CONFIGURED

    lvl = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []

    console_h = logging.StreamHandler(stream=sys.stderr)
    console_h.setLevel(lvl)
    handlers.append(console_h)

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.FileHandler(jsonl_path, encoding="utf-8")
        file_h.setLevel(lvl)
        handlers.append(file_h)

    # Reset root logger's handlers each call so reconfiguration works in long-lived processes.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(lvl)

    # Suppress SDK internal INFO chatter (e.g. "Using bundled Claude Code CLI")
    # while keeping WARNING/ERROR visible for diagnostics.
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    log = structlog.get_logger("worca_t")
    if run_id:
        structlog.contextvars.bind_contextvars(run_id=run_id)
    _CONFIGURED = True
    return log


def get_logger(name: str = "worca_t") -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
