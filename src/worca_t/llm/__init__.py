"""LLM transport modules.

Two implementations for different agent use cases:
- ``reasoning``: pure JSON-in/JSON-out, no file tools, no MCP. Direct SDK.
- ``browser_agent``: full Agent SDK + Playwright MCP (legacy ``run_agent``).

All transports return the same ``AgentResult`` shape so step files are
transport-agnostic at the call site. Choosing a transport is an import
decision, not a runtime branch.
"""

from __future__ import annotations

from worca_t.llm.browser_agent import run_agent
from worca_t.llm.protocols import AgentResult
from worca_t.llm.reasoning import call_reasoning_llm, call_reasoning_llm_with_hitl

__all__ = [
    "AgentResult",
    "call_reasoning_llm",
    "call_reasoning_llm_with_hitl",
    "run_agent",
]
