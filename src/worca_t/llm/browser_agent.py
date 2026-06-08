"""Re-export of the Claude Agent SDK runner for Steps 6, 7, 8, 9.

This is a thin wrapper that names the transport explicitly. Callers
should use ``from worca_t.llm.browser_agent import run_agent`` instead
of importing directly from :mod:`worca_t.claude_runner` — this makes the
transport choice visible at the import site, which is the whole point of
the ``worca_t.llm`` package.

The underlying implementation is unchanged: ``claude_runner.run_agent``
spawns the ``claude`` CLI subprocess with full Agent SDK semantics
(MCP servers, file tools, agentic loop). Use this for steps that need
Playwright MCP (8, 9) or file-editing tools with Claude Code's calibrated
Edit/Read/Glob/Grep semantics (6, 7) until Phase D of the SDK migration
introduces a direct-SDK alternative for the file-editing case.
"""

from __future__ import annotations

from worca_t.claude_runner import run_agent

__all__ = ["run_agent"]
