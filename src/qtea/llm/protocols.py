"""Shared transport contract for LLM calls.

Both transport implementations — direct SDK in :mod:`qtea.llm.reasoning`
and Agent SDK in :mod:`qtea.llm.browser_agent` — return the same
``AgentResult`` shape so step files are transport-agnostic at the call site.

This module is a **re-export hub**. The canonical definitions live in
``claude_runner.py`` (AgentResult) and ``metrics.py`` (AgentMetrics,
CURRENT_STEP_METRICS). Re-exporting here lets new direct-SDK code import
from a single namespace without entangling with the legacy subprocess
module's import surface.
"""

from __future__ import annotations

from qtea.claude_runner import AgentResult
from qtea.metrics import (
    CURRENT_STEP_METRICS,
    AgentMetrics,
    StepMetricsAccumulator,
    extract_agent_metrics,
)

__all__ = [
    "CURRENT_STEP_METRICS",
    "AgentMetrics",
    "AgentResult",
    "StepMetricsAccumulator",
    "extract_agent_metrics",
]
