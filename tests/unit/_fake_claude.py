"""Shared SDK-fake helpers for step / runner tests.

Replaces the old subprocess-based `claude` CLI shim. Now we monkeypatch
`claude_agent_sdk.query` (as imported by `worca_t.claude_runner`) to yield
fake SDK Message objects. Optionally writes files into the agent's workdir
(matching the CWD-write side-effect the old shim provided).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage


def _make_message(spec: dict) -> Any:
    """Construct a real SDK Message instance with the given attributes set.

    Uses ``__new__`` so we don't depend on the SDK's constructor signatures.
    Recognised ``type`` values: ``system``, ``assistant``, ``result``.
    """
    cls_map = {
        "system": SystemMessage,
        "assistant": AssistantMessage,
        "result": ResultMessage,
    }
    t = spec.get("type")
    if t not in cls_map:
        raise ValueError(f"unknown fake message type: {t!r}")
    cls = cls_map[t]
    m = cls.__new__(cls)
    for key, val in spec.items():
        if key == "type":
            continue
        setattr(m, key, val)
    return m


def install_fake_query(
    monkeypatch,
    *,
    messages: list[dict] | None = None,
    files: dict[str, str] | None = None,
    raises: Exception | None = None,
    delay_s: float = 0.0,
    on_call=None,
) -> None:
    """Replace ``worca_t.claude_runner.query`` with an async iterator factory.

    Parameters
    ----------
    messages: list of message specs. Default is a single ``result`` message.
    files: ``{relpath: content}`` written into the agent's cwd (workdir).
    raises: exception to raise mid-iteration (after writing files).
    delay_s: sleep this many seconds before yielding (for timeout tests).
    on_call: callable invoked with ``(prompt, options)`` per query call.
    """
    import asyncio as _asyncio

    msgs = messages or [{"type": "result", "result": "ok"}]
    file_map = files or {}

    async def _fake_query(*, prompt, options=None, transport=None):
        if on_call is not None:
            on_call(prompt, options)
        cwd = Path(options.cwd) if options and getattr(options, "cwd", None) else Path.cwd()
        for rel, content in file_map.items():
            # Absolute paths land where they say (used by steps that now
            # write into `<workspace>/sut/` via add_dirs); relative paths
            # still resolve against the agent's cwd (the step workdir).
            p = Path(rel) if Path(rel).is_absolute() else (cwd / rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if delay_s:
            await _asyncio.sleep(delay_s)
        if raises is not None:
            raise raises
        for spec in msgs:
            yield _make_message(spec)

    # Bypass the missing-binary precheck so tests don't need a real `claude`.
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )
    monkeypatch.setattr("worca_t.claude_runner.query", _fake_query)


def fake_playwright_mcp_call(tool: str = "browser_snapshot") -> dict:
    """Build a fake AssistantMessage spec that simulates a Playwright MCP tool use.

    Step 8's MCP-usage gate counts ``mcp__playwright__*`` tool names in the
    written transcript and fails the step when the agent returns
    ``success=True`` without ever having invoked Playwright MCP. Tests that
    exercise step 8's downstream paths (patching, schema validation, scope
    guard) must inject at least one such message into the fake stream — pass
    the dict this returns alongside the ``{"type": "result", ...}`` terminator.
    """
    return {
        "type": "assistant",
        "content": [{"name": f"mcp__playwright__{tool}", "input": {}}],
    }
