"""Playwright storage-state handover between the SUT pytest runtime, the
Step 9 heal agent's Playwright MCP browser, and the optional one-shot
``worca-t auth-capture`` CLI command.

Two production paths feed this module:

- **Use case B (same-run, primary).** The vendored pytest runtime
  (``worca_t_runtime.py``) captures ``context.storage_state()`` on the
  first passing test and writes it to ``<workspace>/storage-state.json``.
  Step 9 reads that file and injects ``--storage-state=<path>`` into
  Playwright MCP so the heal-agent's browser boots already authenticated
  — no manual auth-replay against the SUT's sign-in flow.

- **Use case A (cross-run, secondary).** A one-shot
  ``worca-t auth-capture --sut <path>`` invocation runs the SUT's
  sign-in helper headed, lets the user complete MFA / SSO, and writes
  ``<sut>/.worca-t/storage-state.json``. The file persists across runs
  and unblocks heal on SUTs whose auth flow cannot be fully automated.

Everything in this module is a pure helper — no Playwright import, no
network, no subprocess. The two production paths above wire the helpers
into ``s09_execute.py`` and the runtime template; ``auth_capture.py``
spawns the SUT venv to produce the file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

# Convention paths for the two capture sources. The 4-tier resolver below
# walks them in priority order; an explicit CLI flag / env var wins, then
# the per-SUT persistent file (Use case A), then the per-run file
# (Use case B).
_SUT_CONVENTION_REL = Path(".worca-t") / "storage-state.json"
_WORKSPACE_CONVENTION_NAME = "storage-state.json"


def resolve(
    sut_root: Path | None,
    workspace_root: Path | None,
    cli_opt: Path | None,
    env: dict[str, str] | None = None,
) -> Path | None:
    """Resolve which storage-state file Step 9 should inject into Playwright
    MCP. Walks 4 sources in priority order, returning the first that points
    at an existing file on disk:

      1. ``cli_opt`` — ``--storage-state <path>`` Typer flag (explicit
         operator override; wins unconditionally).
      2. ``WORCA_T_STORAGE_STATE`` env var (host- or CI-scoped override).
      3. ``<sut_root>/.worca-t/storage-state.json`` (Use case A — the
         ``auth-capture`` output, persistent across runs).
      4. ``<workspace_root>/storage-state.json`` (Use case B — the per-run
         auto-capture from the pytest runtime).

    Returns ``None`` when no source points at a readable file (Step 9 then
    proceeds without storage state — heal-agent falls back to the manual
    auth-replay path).

    ``env`` is an optional explicit env dict (defaults to ``os.environ``).
    Threaded through so tests can exercise the precedence rules without
    mutating process env.
    """
    import os as _os

    sources: list[Path] = []
    if cli_opt is not None:
        sources.append(Path(cli_opt))
    src_env = env if env is not None else _os.environ
    env_path = src_env.get("WORCA_T_STORAGE_STATE")
    if env_path:
        sources.append(Path(env_path))
    if sut_root is not None:
        sources.append(Path(sut_root) / _SUT_CONVENTION_REL)
    if workspace_root is not None:
        sources.append(Path(workspace_root) / _WORKSPACE_CONVENTION_NAME)

    for p in sources:
        try:
            if p.is_file():
                return p
        except OSError:
            # Permission errors / weird path semantics — treat as miss.
            continue
    return None


def to_mcp_arg(path: Path | None) -> str:
    """Render the ``@playwright/mcp`` CLI flag for a resolved storage-state
    path. Empty string when ``path`` is ``None`` so the ``.mcp.json``
    template can substitute the token unconditionally — empty args are
    filtered out by ``mcp_manager.load_mcp_config`` before they reach the
    MCP subprocess.

    Absolute path is forced so the MCP subprocess (whose cwd we don't
    control) resolves the file correctly.
    """
    if path is None:
        return ""
    return f"--storage-state={Path(path).resolve()}"


def summary_for_prompt(path: Path | None) -> str:
    """Compose the heal-prompt snippet describing the pre-loaded storage
    state. Returns an empty string when no state is loaded — the prompt
    builder concatenates unconditionally.

    The directive is precise: skip the SUT's sign-in helper, navigate
    directly to the failing-page URL, and on a login-screen redirect
    fall back to the normal auth-replay path rather than aborting (same-
    run captures should never be stale; cross-run captures might be
    expired and the replay path is the right fallback).
    """
    if path is None:
        return ""
    try:
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        mtime = "unknown"
    return (
        "\n--- PRE-LOADED STORAGE STATE ---\n"
        f"Storage state is pre-loaded into the Playwright MCP browser "
        f"context (captured {mtime}). The browser is already authenticated.\n"
        f"- DO NOT call the SUT's sign-in helper.\n"
        f"- Use `browser_navigate` to go directly to the failing page URL.\n"
        f"- If the page redirects to a login screen / 401 / 403 / auth-"
        f"domain, the storage state may be stale: log a one-line note "
        f"and proceed with a normal auth-replay attempt as a fallback "
        f"(do NOT abort the heal).\n"
        f"- Caveat: if the failing test ITSELF targets a login page, a "
        f"redirect to login is the expected page state, not stale state."
    )


def mask_path(p: Path) -> str:
    """Collapse common user-specific path prefixes for log lines.

    Logs may end up in shared diagnostics / bug-report artifacts. The
    storage-state filename itself is not sensitive but the absolute path
    can leak Windows usernames and SUT clone roots. We mask conservatively:
    keep the trailing ``.worca-t/storage-state.json`` or ``storage-
    state.json`` segment and replace the prefix with the relevant marker.

    NEVER log the file CONTENTS — those are session tokens. Callers that
    handle storage state are expected to respect that invariant.
    """
    parts = Path(p).parts
    if not parts:
        return str(p)
    # Per-SUT convention: ".worca-t/storage-state.json"
    for i in range(len(parts) - 1):
        if parts[i] == ".worca-t" and parts[i + 1] == "storage-state.json":
            return "<sut>/.worca-t/storage-state.json"
    # Per-workspace convention: "storage-state.json" directly under workspace
    if parts[-1] == "storage-state.json":
        return "<workspace>/storage-state.json"
    return Path(p).name


__all__ = ["mask_path", "resolve", "summary_for_prompt", "to_mcp_arg"]
