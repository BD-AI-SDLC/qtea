"""STUB: Direct-SDK transport for file-editing agents (Steps 6, 7).

Phase D of the SDK migration. Not yet implemented — Steps 6 and 7
continue to use :func:`worca_t.llm.browser_agent.run_agent` (which wraps
the Agent SDK and inherits Claude Code's calibrated Edit/Read/Glob/Grep
tool surface).

A follow-up plan will populate this module with a direct-SDK function
that:
  - Exposes custom Read/Glob/Grep/Edit/Write tools via the ``@beta_tool``
    decorator on the Anthropic Python SDK
  - Runs the agentic loop via ``client.beta.messages.tool_runner(...)``
  - Implements Edit-tool staleness checks (compare file hash from when
    the model last read it; reject stale writes)
  - Grants filesystem access only to caller-specified roots (parallel
    to today's ``add_dirs`` parameter on ``run_agent``)

Until that lands, importing :func:`call_file_editing_agent` and calling
it raises ``NotImplementedError`` with a pointer to the correct
transport for the current phase.
"""

from __future__ import annotations


async def call_file_editing_agent(*args, **kwargs):  # noqa: ARG001
    """Not yet implemented. Use ``browser_agent.run_agent`` for Steps 6-7."""
    raise NotImplementedError(
        "Direct-SDK file-editing agent is deferred to Phase D of the SDK "
        "migration. For Steps 6 and 7 today, use "
        "`from worca_t.llm.browser_agent import run_agent` instead."
    )


__all__ = ["call_file_editing_agent"]
