"""Proxy detection + propagation to subprocesses.

On Windows, also merges user-level registry environment variables so that
credentials stored by ``claude login`` are available to child processes even
when the parent shell was started before login.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from worca_t.config import PROXY_ENV_KEYS, SECRET_ENV_KEYS


def _windows_user_env() -> dict[str, str]:
    """Read user-level env vars from HKEY_CURRENT_USER\\Environment.

    Returns an empty dict on non-Windows or if the registry is unreadable.
    """
    if sys.platform != "win32":
        return {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            env: dict[str, str] = {}
            idx = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, idx)
                    env[name] = value
                    idx += 1
                except OSError:
                    break
            return env
    except Exception:
        return {}


def detected_proxies() -> dict[str, str]:
    """Return proxy-related env vars currently set in the process."""
    return {k: os.environ[k] for k in PROXY_ENV_KEYS if k in os.environ}


def with_proxy_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build an env dict containing the current process env + proxy vars + extras.

    Use this when constructing subprocesses to guarantee proxy + credential
    propagation under corporate networks.

    On Windows, user-level registry variables are merged first so that
    credentials stored by ``claude login`` are picked up even when the current
    process inherited an older environment.  Process-level vars take precedence.
    """
    # Start with registry vars (Windows only) so process env can override.
    env = _windows_user_env()
    env.update(os.environ)

    # Ensure both upper- and lower-case proxy keys agree. When process env sets
    # one spelling, it's authoritative for both (overrides the Windows registry
    # fallback). Falls back to copy-the-only-present-spelling when neither is in
    # process env.
    proxy_pairs = (
        ("HTTP_PROXY", "http_proxy"),
        ("HTTPS_PROXY", "https_proxy"),
        ("NO_PROXY", "no_proxy"),
    )
    for upper, lower in proxy_pairs:
        upper_proc = os.environ.get(upper)
        lower_proc = os.environ.get(lower)
        if upper_proc is not None:
            env[upper] = upper_proc
            env[lower] = upper_proc
        elif lower_proc is not None:
            env[upper] = lower_proc
            env[lower] = lower_proc
        elif upper in env and lower not in env:
            env[lower] = env[upper]
        elif lower in env and upper not in env:
            env[upper] = env[lower]
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def safe_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    isolate_venv: bool = False,
) -> dict[str, str]:
    """Like ``with_proxy_env``, but strips inherited ``SECRET_ENV_KEYS``.

    Use for any subprocess that runs SUT-provided code (test commands, install
    commands, MCP server probes).  Trusted tooling (Claude CLI, git, allure)
    should continue using ``with_proxy_env`` directly.

    Keys present in ``extra`` are honored even if they appear in
    ``SECRET_ENV_KEYS``: ``extra`` is the caller's explicit declaration of
    what the child needs (e.g. an MCP server's ``env`` block in ``.mcp.json``).
    Only secrets *inherited* from the parent process are scrubbed.

    When ``isolate_venv=True``, ``VIRTUAL_ENV`` and ``POETRY_ACTIVE`` are also
    stripped (unless overridden via ``extra``). Use this for subprocesses
    targeting a Python SUT managed by poetry / uv: if worca-t itself runs from
    a venv (e.g. ``uv tool install --editable``), that venv leaks into the
    child via ``VIRTUAL_ENV`` and poetry happily reuses it as the SUT's venv
    when the Python version satisfies the SUT's constraint. The result is a
    contaminated environment where ``poetry install`` reports "in sync" yet
    pytest fails on missing SUT-specific deps. Stripping forces poetry to
    create / use a clean SUT-specific venv.
    """
    explicit = {str(k) for k in (extra or {})}
    env = with_proxy_env(extra)
    for key in SECRET_ENV_KEYS:
        if key in explicit:
            continue
        env.pop(key, None)
    if isolate_venv:
        for key in ("VIRTUAL_ENV", "POETRY_ACTIVE"):
            if key in explicit:
                continue
            env.pop(key, None)
    return env


def mask_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of env with secret values redacted (for logging)."""
    return {k: ("***REDACTED***" if k in SECRET_ENV_KEYS else v) for k, v in env.items()}
