"""Shared transport contract for LLM calls.

Both transport implementations — direct SDK in :mod:`worca_t.llm.reasoning`
and Agent SDK in :mod:`worca_t.llm.browser_agent` — return the same
``AgentResult`` shape so step files are transport-agnostic at the call site.

This module is a **re-export hub**. The canonical definitions live in
``claude_runner.py`` (AgentResult) and ``metrics.py`` (AgentMetrics,
CURRENT_STEP_METRICS). Re-exporting here lets new direct-SDK code import
from a single namespace without entangling with the legacy subprocess
module's import surface.
"""

from __future__ import annotations

from worca_t.claude_runner import AgentResult
from worca_t.metrics import (
    CURRENT_STEP_METRICS,
    AgentMetrics,
    StepMetricsAccumulator,
    extract_agent_metrics,
)

__all__ = [
    "AgentResult",
    "AgentMetrics",
    "StepMetricsAccumulator",
    "CURRENT_STEP_METRICS",
    "extract_agent_metrics",
]
