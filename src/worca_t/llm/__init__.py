"""LLM transport modules.

Three implementations for different agent use cases:
- ``reasoning``: pure JSON-in/JSON-out, no file tools, no MCP. Direct SDK.
- ``browser_agent``: full Agent SDK + Playwright MCP (legacy ``run_agent``).
- ``file_agent``: **STUB** — file-editing agents (Phase D, not yet implemented).

All transports return the same ``AgentResult`` shape so step files are
transport-agnostic at the call site. Choosing a transport is an import
decision, not a runtime branch.
"""

from __future__ import annotations

from worca_t.llm.browser_agent import run_agent
from worca_t.llm.file_agent import call_file_editing_agent
from worca_t.llm.protocols import AgentResult
from worca_t.llm.reasoning import call_reasoning_llm

__all__ = [
    "AgentResult",
    "call_reasoning_llm",
    "call_file_editing_agent",
    "run_agent",
]
