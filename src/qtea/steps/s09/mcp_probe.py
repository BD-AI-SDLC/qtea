"""Playwright MCP server lazy probe — warms the npx cache before first heal.

Called from ``ExecuteStep.run()`` just before the first heal-agent
invocation. ``probe_server`` spawns the server, waits ~30s for it to reach
``connected``, then kills the process. Side effect: warm npx cache +
completed Playwright binary check, so when the Agent SDK later spawns its
own copy of the server it comes up faster and the heal agent doesn't burn
turns on ``WaitForMcpServers`` calls.

Centralised in a module-level helper so unit tests can monkey-patch
``qtea.steps.s09_execute._lazy_probe_heal_mcp`` without touching the MCP
plumbing (see ``tests/unit/test_mcp_preflight_lazy.py`` for the exact
monkey-patch path). Inline imports of ``mcp_manager`` and ``time`` MUST
stay inline — they participate in the lazy-warm behaviour.
"""

from __future__ import annotations


def _lazy_probe_heal_mcp(
    server_name: str,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, float]:
    """Warm + probe one MCP server just before the first heal-agent invocation.

    ``probe_server`` spawns the server and lets it run for ~30 s, then kills
    it. The side effect is a warm npx cache and a completed Playwright
    binary check, so when the Agent SDK later spawns its own copy of the
    server it reaches `connected` faster — eliminating the race where the
    heal agent burns turns calling ``WaitForMcpServers`` before MCP is up.

    ``env`` is an optional per-call MCP env overlay (e.g.
    ``{"QTEA_STORAGE_STATE_ARG": "--storage-state=/abs/path"}``).
    Threaded through to ``load_mcp_config`` so the rendered MCP server
    args reflect per-run substitutions (e.g. the storage-state file path
    Step 9 just resolved) without mutating ``os.environ``.

    Returns ``(ok, detail, elapsed_s)``. On failure the caller logs + skips
    the heal loop (heal is best-effort — a missing Playwright MCP shouldn't
    fail the whole Step 9 run; the failing tests still flow to Step 10 as
    bug candidates).

    Centralised in a module-level helper so unit tests can monkey-patch
    ``s09_execute._lazy_probe_heal_mcp`` without touching the MCP plumbing.
    """
    import time as _time

    from qtea.mcp_manager import (
        PLAYWRIGHT_SERVER_NAME,
        ensure_playwright_mcp_browser,
        ensure_playwright_mcp_installed,
        load_mcp_config,
        warm_mcp_server,
    )

    started = _time.monotonic()

    # Bypass npx via a pinned local install (direct `node cli.js`): npx spawns
    # cost ~186 s on AV-scanned Windows hosts vs ~4-6 s for node, which
    # otherwise leaves Playwright `pending` past the SDK's MCP_TIMEOUT. Both
    # calls are idempotent no-ops once the managed install / browser exist;
    # `load_mcp_config` then rewrites the npx spec to the node form.
    if server_name == PLAYWRIGHT_SERVER_NAME:
        ensure_playwright_mcp_installed()
        ensure_playwright_mcp_browser()

    try:
        all_servers = load_mcp_config(env=env)
    except (FileNotFoundError, OSError, ValueError) as e:
        return False, f"could not load .mcp.json: {e}", 0.0

    server = all_servers.get(server_name)
    if server is None:
        return False, f"{server_name!r} not declared in .mcp.json", 0.0

    # A real MCP `initialize` handshake — returns as soon as the server answers
    # (a few seconds) instead of always burning the full smoke-probe window.
    ok, detail = warm_mcp_server(server)
    elapsed = round(_time.monotonic() - started, 2)
    return ok, detail or "", elapsed


__all__ = ["_lazy_probe_heal_mcp"]
