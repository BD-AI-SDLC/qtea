"""Playwright storage-state handover between the SUT pytest runtime, the
Step 9 heal agent's Playwright MCP browser, and the optional one-shot
``qtea auth-capture`` CLI command.

Two production paths feed this module:

- **Use case B (same-run, primary).** The vendored pytest runtime
  (``qtea_runtime.py``) captures ``context.storage_state()`` on the
  first passing test and writes it to ``<workspace>/storage-state.json``.
  Step 9 reads that file and injects ``--storage-state=<path>`` into
  Playwright MCP so the heal-agent's browser boots already authenticated
  — no manual auth-replay against the SUT's sign-in flow.

- **Use case A (cross-run, secondary).** A one-shot
  ``qtea auth-capture --sut <path>`` invocation runs the SUT's
  sign-in helper headed, lets the user complete MFA / SSO, and writes
  ``<sut>/.qtea/storage-state.json``. The file persists across runs
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
_SUT_CONVENTION_REL = Path(".qtea") / "storage-state.json"
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
      2. ``QTEA_STORAGE_STATE`` env var (host- or CI-scoped override).
      3. ``<sut_root>/.qtea/storage-state.json`` (Use case A — the
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
    env_path = src_env.get("QTEA_STORAGE_STATE")
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


def write_target(
    sut_root: Path | None,
    cli_opt: Path | None,
    env: dict[str, str] | None = None,
) -> Path:
    """Where a *fresh* capture should be WRITTEN — the counterpart to
    :func:`resolve` (which finds an existing file to READ).

    Mirrors ``resolve``'s operator-controlled precedence so a configured
    location is both written to AND read from — log in once, reuse across
    runs. Unlike ``resolve`` this is existence-independent: it returns the
    override even when the file doesn't exist yet (so the first capture lands
    there) and even when it's stale (so a re-login overwrites it in place).

      1. ``cli_opt`` — ``--storage-state <path>``
      2. ``QTEA_STORAGE_STATE`` env var
      3. ``<sut_root>/.qtea/storage-state.json`` (per-SUT convention)

    Raises ``ValueError`` if no override is set and ``sut_root`` is None (the
    convention path is SUT-relative, so there'd be nowhere to write).
    """
    import os as _os

    if cli_opt is not None:
        return Path(cli_opt)
    src_env = env if env is not None else _os.environ
    env_path = src_env.get("QTEA_STORAGE_STATE")
    if env_path:
        return Path(env_path)
    if sut_root is not None:
        return Path(sut_root) / _SUT_CONVENTION_REL
    raise ValueError("write_target needs a storage-state override or a sut_root")


def _gitignore_covers(text: str, entry: str) -> bool:
    """True when an existing ``.gitignore`` already ignores *entry* (a
    SUT-relative POSIX path). Recognizes exact lines, directory prefixes
    (``.qtea/`` covers ``.qtea/storage-state.json``) and bare basename
    patterns (``storage-state.json`` matches the file in any directory).
    Comment/blank lines are skipped.
    """
    name = entry.rsplit("/", 1)[-1]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == entry:
            return True
        if line.endswith("/") and (entry == line[:-1] or entry.startswith(line)):
            return True
        if "/" not in line and line == name:
            return True
    return False


def ensure_gitignored(sut_root: Path | None, target: Path) -> None:
    """Ensure *target* (a storage-state credential) is git-ignored in the SUT.

    storage-state holds live session cookies; without this, ``commit_step``'s
    ``git add -A`` would stage it into the qtea branch a human reviews / opens
    a PR from. The per-SUT convention path is already seeded at Step 6, but a
    custom ``--storage-state`` target inside the SUT would not be — this closes
    that gap for any write location.

    No-op when *target* is outside ``sut_root`` (e.g. a stable path in the
    user's home dir — nothing in the SUT to leak) or when ``sut_root`` is None.
    Idempotent and best-effort — never raises.
    """
    if sut_root is None:
        return
    try:
        rel = Path(target).resolve().relative_to(Path(sut_root).resolve())
    except (ValueError, OSError):
        return  # outside the SUT tree → nothing to ignore
    entry = rel.as_posix()
    gitignore = Path(sut_root) / ".gitignore"
    try:
        text = (
            gitignore.read_text(encoding="utf-8", errors="replace")
            if gitignore.exists()
            else ""
        )
        if _gitignore_covers(text, entry):
            return
        if text and not text.endswith("\n"):
            text += "\n"
        gitignore.write_text(text + entry + "\n", encoding="utf-8")
    except OSError:
        pass


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


def mcp_browser_env(
    storage_state_path: Path | None, user_data_dir: Path | str,
) -> dict[str, str]:
    """Build the ``@playwright/mcp`` browser-mode env args for ``.mcp.json``
    token substitution (``QTEA_MCP_ISOLATED_ARG`` / ``QTEA_STORAGE_STATE_ARG``
    / ``QTEA_MCP_USER_DATA_DIR_ARG``).

    The critical rule: ``@playwright/mcp`` treats ``--storage-state`` as a seed
    for an ISOLATED (in-memory) context — a persistent ``--user-data-dir``
    launches a persistent profile that IGNORES ``--storage-state`` entirely. So
    a captured session (headed manual login, auth-replay, prior run) only
    actually loads when we pass ``--isolated`` and DROP ``--user-data-dir``
    (Observed failure mode: the live-explore crawl bounced straight to SSO
    because both flags were passed and the storage-state was silently ignored.)

    - storage-state present → ``--isolated`` + ``--storage-state`` (no
      user-data-dir; in-memory profiles are independent, so this also avoids
      the "browser already in use" profile-lock contention that motivated a
      per-caller user-data-dir).
    - storage-state absent → persistent ``--user-data-dir`` (nothing to seed;
      a stable profile is fine and gives each caller its own dir).

    Empty values are dropped by ``mcp_manager._filter_empty_args`` before the
    subprocess spawns, so the unset flags never reach the CLI as stray args."""
    if storage_state_path is not None:
        return {
            "QTEA_MCP_ISOLATED_ARG": "--isolated",
            "QTEA_STORAGE_STATE_ARG": to_mcp_arg(storage_state_path),
            "QTEA_MCP_USER_DATA_DIR_ARG": "",
        }
    return {
        "QTEA_MCP_ISOLATED_ARG": "",
        "QTEA_STORAGE_STATE_ARG": "",
        "QTEA_MCP_USER_DATA_DIR_ARG": f"--user-data-dir={user_data_dir}",
    }


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
    keep the trailing ``.qtea/storage-state.json`` or ``storage-
    state.json`` segment and replace the prefix with the relevant marker.

    NEVER log the file CONTENTS — those are session tokens. Callers that
    handle storage state are expected to respect that invariant.
    """
    parts = Path(p).parts
    if not parts:
        return str(p)
    # Per-SUT convention: ".qtea/storage-state.json"
    for i in range(len(parts) - 1):
        if parts[i] == ".qtea" and parts[i + 1] == "storage-state.json":
            return "<sut>/.qtea/storage-state.json"
    # Per-workspace convention: "storage-state.json" directly under workspace
    if parts[-1] == "storage-state.json":
        return "<workspace>/storage-state.json"
    return Path(p).name


__all__ = [
    "ensure_gitignored",
    "mask_path",
    "resolve",
    "summary_for_prompt",
    "to_mcp_arg",
    "write_target",
]
