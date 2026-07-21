"""Pre-Step-7 authentication prewarm.

Before the architect plans and the site-explorer opens the SUT, drive the
SUT's OWN sign-in helper once (headless) to produce a Playwright
``storage-state.json``. Step 7's live-exploration and Step 9's heal agent then
boot their Playwright MCP browser already authenticated — so exploration sees
the real post-login application instead of stalling at the login gate.

This reuses :func:`qtea.auth_capture.cmd_auth_capture` (the same engine behind
the manual ``qtea auth-capture`` CLI). Credentials never enter an agent/LLM
context — capture runs the SUT's own code in a subprocess.

Best-effort and fully gated: any failure (no ``auth_flow``, non-Playwright
stack, missing SUT env, subprocess error, interactive MFA needed) logs a
warning + actionable hint and returns ``None`` — the pipeline proceeds exactly
as before (unauthenticated exploration). Toggle with ``QTEA_AUTH_CAPTURE``
(default on); auto-skips when ``QTEA_NO_LLM_RESOLVE=1`` (symmetric with live
exploration) or when a storage-state already resolves (reuse, never re-login).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from qtea import storage_state as _storage_state
from qtea.logging_setup import get_logger

log = get_logger(__name__)

# Stacks cmd_auth_capture can drive (storage-state is Playwright-specific).
_PLAYWRIGHT_LANGUAGES = frozenset({"python", "javascript", "typescript"})

# Default HEADLESS capture timeout (seconds). Short — a fully-automatable login
# (incl. SSO with a dedicated service user) is quick. Override via
# QTEA_AUTH_CAPTURE_TIMEOUT_S.
_DEFAULT_TIMEOUT_S = 180

# Default HEADED capture timeout (seconds). Generous — a human is completing an
# interactive MFA/SSO challenge in the visible browser. Override via
# QTEA_AUTH_CAPTURE_HEADED_TIMEOUT_S.
_DEFAULT_HEADED_TIMEOUT_S = 600

_TRUTHY = frozenset({"1", "true", "True", "yes", "on"})


def auth_capture_enabled(no_auth_capture: bool = False) -> bool:
    """Feature gate: on by default; off via flag, ``QTEA_AUTH_CAPTURE=0``, or
    zero-LLM CI mode (``QTEA_NO_LLM_RESOLVE=1``, symmetric with live-explore)."""
    if no_auth_capture:
        return False
    if os.environ.get("QTEA_AUTH_CAPTURE", "1") == "0":
        return False
    return os.environ.get("QTEA_NO_LLM_RESOLVE") != "1"


def headed_mode_requested(options: object | None = None) -> bool:
    """Whether the operator asked for HEADED auth capture (interactive MFA).

    Use headed only for logins that need a human (MFA / captcha). SSO with a
    dedicated service user is non-interactive — leave it headless and supply
    the SSO user's credentials via ``auth_flow.credentials_env_vars``.
    Sources: ``--auth-headed`` (``options.auth_headed``) or
    ``QTEA_AUTH_CAPTURE_HEADED=1``.
    """
    if options is not None and getattr(options, "auth_headed", False):
        return True
    return os.environ.get("QTEA_AUTH_CAPTURE_HEADED", "0") in _TRUTHY


def is_interactive_session(options: object | None = None) -> bool:
    """True when a human can complete an interactive (headed) login — a TTY, or
    the desktop UI (which drives a visible browser with a person present)."""
    if options is not None and getattr(options, "ui_mode", False):
        return True
    try:
        return bool(sys.stdin.isatty())
    except (ValueError, OSError):
        return False


_VALID_MODES = ("headed", "mcp", "script", "off")


def auth_prewarm_mode(options: object | None = None) -> str:
    """Resolve the auth-prewarm strategy: ``headed`` (default) | ``mcp`` | ``script`` | ``off``.

    Forced ``off`` by ``--no-auth-capture`` / ``QTEA_AUTH_CAPTURE=0`` / zero-LLM
    CI mode (``QTEA_NO_LLM_RESOLVE=1``). ``--auth-headed`` (``options.auth_headed``
    / ``QTEA_AUTH_CAPTURE_HEADED=1``) forces ``headed``. Otherwise the CLI flag
    (``options.auth_prewarm_mode``) wins, then ``QTEA_AUTH_PREWARM_MODE``, then
    the ``headed`` default.

    - ``headed`` (default): open the SUT's base URL in a VISIBLE browser and let
      the human log in by any means (MFA / SSO / captcha), then capture the
      session. Helper-independent; credentials never reach the model. qtea is
      local-only, so a human is present — see :mod:`qtea.headed_auth_capture`.
    - ``mcp``: the Step-7 site-explorer logs in via Playwright MCP, then explores
      in the same session (pattern-agnostic; credentials reach the model).
    - ``script``: run the SUT's own sign-in helper in a subprocess to produce a
      storage-state (credentials never touch the model; needs the SUT env).
    - ``off``: no prewarm — explore unauthenticated.
    """
    no_flag = bool(getattr(options, "no_auth_capture", False)) if options else False
    if not auth_capture_enabled(no_flag):
        return "off"
    if headed_mode_requested(options):
        return "headed"
    mode = ""
    if options is not None:
        mode = (getattr(options, "auth_prewarm_mode", None) or "")
    mode = (mode or os.environ.get("QTEA_AUTH_PREWARM_MODE", "") or "headed").strip().lower()
    return mode if mode in _VALID_MODES else "headed"


def resolve_login_credentials(
    active_module: dict | None,
    research: dict | None = None,
) -> tuple[str, str] | None:
    """Resolve ``(username, password)`` for an automated login.

    Primary source: ``auth_flow.credentials_env_vars`` in the active module.
    Fallback when that list is empty: scan ``sut_env_keys`` from ``research``
    (Step 6 output) for vars whose names match credential patterns — this
    covers the case where the researcher found the vars in the SUT code but
    didn't populate the structured inventory field.

    Pairing heuristic: username = first var whose name contains ``USER``;
    password = first whose name contains ``PASS``. Override the chosen var
    names with ``QTEA_AUTH_USERNAME_VAR`` / ``QTEA_AUTH_PASSWORD_VAR``.
    Returns ``None`` when either can't be resolved to a non-empty value.
    """
    if not isinstance(active_module, dict):
        return None
    auth = active_module.get("auth_flow") or {}
    names = auth.get("credentials_env_vars") if isinstance(auth, dict) else None
    names = [n for n in (names or []) if isinstance(n, str)]

    # Fallback: derive candidate names from sut_env_keys in research.json
    # when the inventory list is empty (researcher populated prose but not JSON).
    if not names and isinstance(research, dict):
        sut_env_keys = research.get("sut_env_keys") or []
        _CRED_PATTERNS = ("USER", "PASS", "PASSWD", "PWD", "EMAIL", "LOGIN", "ACCOUNT")
        names = [
            k for k in sut_env_keys
            if isinstance(k, str) and any(p in k.upper() for p in _CRED_PATTERNS)
        ]
        if names:
            log.info(
                "step07.credentials_from_sut_env_keys",
                count=len(names),
                hint="credentials_env_vars was empty in inventory; derived from sut_env_keys",
            )

    user_var = os.environ.get("QTEA_AUTH_USERNAME_VAR") or next(
        (n for n in names if "USER" in n.upper()), None
    )
    pass_var = os.environ.get("QTEA_AUTH_PASSWORD_VAR") or next(
        (n for n in names if "PASS" in n.upper()), None
    )
    if not user_var or not pass_var:
        return None
    username = (os.environ.get(user_var) or "").strip()
    password = os.environ.get(pass_var) or ""
    if not username or not password:
        return None
    return username, password


def login_identity_provider() -> str | None:
    """Optional hint for which identity-provider / business-unit option to pick
    on a login chooser (e.g. ``Internal``). Avoids SSO/MFA providers by default.
    Set via ``QTEA_AUTH_IDENTITY_PROVIDER``."""
    v = (os.environ.get("QTEA_AUTH_IDENTITY_PROVIDER") or "").strip()
    return v or None


def load_active_module(step6_dir: Path) -> dict | None:
    """Read ``sut_inventory.json`` from a Step 6 artifact dir and return its
    resolved ``active_module`` entry, or ``None`` when absent/unreadable."""
    p = Path(step6_dir) / "sut_inventory.json"
    if not p.is_file():
        return None
    try:
        inv = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(inv, dict):
        return None
    name = inv.get("active_module")
    if not name:
        return None
    for mod in inv.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == name:
            return mod
    return None


def is_applicable(
    *,
    sut_root: Path,
    workspace_root: Path,
    active_module: dict | None,
    cli_storage_state: Path | None = None,
    no_auth_capture: bool = False,
    headed_requested: bool = False,
    interactive: bool = False,
) -> tuple[bool, str]:
    """Decide whether an auth prewarm should run. Returns ``(applicable,
    reason)`` — ``reason`` names the skip cause when not applicable so callers
    (pipeline env-prewarm gate + Step 7) share one decision.
    """
    if not auth_capture_enabled(no_auth_capture):
        return False, "disabled"
    # Reuse an existing session — never re-drive login when one resolves.
    if _storage_state.resolve(
        sut_root=sut_root, workspace_root=workspace_root, cli_opt=cli_storage_state,
    ) is not None:
        return False, "storage_state_present"
    if not isinstance(active_module, dict):
        return False, "no_active_module"
    auth = active_module.get("auth_flow") or {}
    entry = auth.get("entry_method") if isinstance(auth, dict) else None
    if not entry or ":" not in str(entry):
        return False, "no_auth_flow_entry_method"
    lang = (active_module.get("language") or "").lower()
    if lang not in _PLAYWRIGHT_LANGUAGES:
        return False, f"non_playwright_lang:{lang or 'unknown'}"
    # The capture wrapper can only invoke a module-level sign-in FUNCTION that
    # accepts a Page/Context. A dotted symbol (e.g. `BasePage.logIn`) is a class
    # method it cannot instantiate/call — skip cleanly instead of launching a
    # browser that will fail with "export not found".
    symbol = str(entry).split(":", 1)[1]
    if "." in symbol:
        return False, "auth_flow_entry_is_class_method"
    # Headed (interactive MFA) needs a human at the browser. Unattended runs
    # can't complete it — skip rather than hang the pipeline on a hidden prompt.
    if headed_requested and not interactive:
        return False, "headed_required_non_interactive"
    return True, "applicable"


async def maybe_prewarm_auth(
    *,
    sut_root: Path,
    workspace_root: Path,
    active_module: dict | None,
    cli_storage_state: Path | None = None,
    no_auth_capture: bool = False,
    headed_requested: bool = False,
    interactive: bool = False,
    timeout_s: int | None = None,
) -> Path | None:
    """Drive the SUT's sign-in helper to produce a storage-state.

    Runs HEADLESS by default (fast; works for automatable logins incl. SSO with
    a dedicated service user). When ``headed_requested`` is set AND the session
    is ``interactive``, opens a visible browser so a human can complete an MFA /
    captcha challenge. Returns the written path, or ``None`` when skipped/failed.
    Never raises — auth prewarm is a best-effort enhancement, not a gate.
    """
    ok, reason = is_applicable(
        sut_root=sut_root,
        workspace_root=workspace_root,
        active_module=active_module,
        cli_storage_state=cli_storage_state,
        no_auth_capture=no_auth_capture,
        headed_requested=headed_requested,
        interactive=interactive,
    )
    if not ok:
        log.info("step07.auth_prewarm_skip", reason=reason)
        if reason == "headed_required_non_interactive":
            log.warning(
                "step07.auth_prewarm_headed_needs_tty",
                hint=(
                    "Interactive (headed) auth was requested but this run has no "
                    "TTY/UI. Run `qtea auth-capture --sut <path>` (headed) in a "
                    "terminal to complete MFA once — its storage-state is reused."
                ),
            )
        elif reason == "auth_flow_entry_is_class_method":
            log.warning(
                "step07.auth_prewarm_entry_unsupported",
                hint=(
                    "auth_flow.entry_method is a class method (e.g. "
                    "`BasePage.logIn`), which auto auth-capture cannot invoke — "
                    "it needs a module-level sign-in function taking a "
                    "Page/Context. Either add a thin module-level login wrapper "
                    "to the SUT, or capture a storage-state once and pass it via "
                    "`--storage-state` (site-exploration + Step 9 reuse it)."
                ),
            )
        return None

    headed = bool(headed_requested and interactive)
    default_timeout = _DEFAULT_HEADED_TIMEOUT_S if headed else _DEFAULT_TIMEOUT_S
    env_key = (
        "QTEA_AUTH_CAPTURE_HEADED_TIMEOUT_S" if headed
        else "QTEA_AUTH_CAPTURE_TIMEOUT_S"
    )
    try:
        timeout = timeout_s or int(os.environ.get(env_key, "") or default_timeout)
    except ValueError:
        timeout = default_timeout

    from qtea.auth_capture import cmd_auth_capture

    # Honor a configured override so the capture is reusable across runs; else
    # the per-SUT convention path. Gitignore it when it lands inside the SUT.
    output = _storage_state.write_target(sut_root, cli_storage_state)
    _storage_state.ensure_gitignored(sut_root, output)

    log.info("step07.auth_prewarm_start", headed=headed, timeout_s=timeout)
    try:
        out_path = await asyncio.to_thread(
            cmd_auth_capture,
            sut=sut_root,
            output=output,
            headed=headed,
            timeout_s=timeout,
            # Pass the module we already loaded from the workspace step-6 dir —
            # cmd_auth_capture otherwise looks under <sut>/.qtea/, which the
            # pipeline never creates.
            active_module=active_module,
        )
    except Exception as e:  # best-effort; never break Step 7
        hint = (
            "auth-capture failed. If the SUT login needs interactive MFA, "
            "re-run with `--auth-headed` in a terminal (or run "
            "`qtea auth-capture --sut <path>` headed once). For SSO, ensure the "
            "dedicated SSO user's credentials are in the SUT's "
            "auth_flow.credentials_env_vars."
            if not headed else
            "headed auth-capture failed or was not completed in time. Re-run "
            "`qtea auth-capture --sut <path>` headed and complete the MFA "
            "prompt — its output is reused automatically."
        )
        log.warning("step07.auth_prewarm_failed", error=str(e), headed=headed, hint=hint)
        return None

    log.info(
        "step07.auth_prewarm_success",
        path=_storage_state.mask_path(out_path), headed=headed,
    )
    return out_path


async def _session_is_trustworthy(
    *,
    sut_root: Path,
    workspace_root: Path,
    base_url: str,
    cli_storage_state: Path | None,
) -> tuple[bool, Path | None]:
    """Resolve any existing storage-state and CONSERVATIVELY decide whether to
    trust it. Returns ``(trustworthy, resolved_path)``.

    A resolved session is trusted unless the probe returns a high-confidence
    "not authenticated" signal (login/SSO redirect or a visible password field on
    the landing page). Ambiguity / errors → trust it (proceed) — we never force a
    needless re-login. No resolved session → ``(False, None)``.
    """
    from qtea import headed_auth_capture

    resolved = _storage_state.resolve(
        sut_root=sut_root, workspace_root=workspace_root, cli_opt=cli_storage_state,
    )
    if resolved is None:
        return False, None
    try:
        verdict = await headed_auth_capture.probe_authenticated(base_url, resolved)
    except Exception as e:
        log.info("step07.headed_probe_error", error=str(e))
        verdict = "ambiguous"
    trustworthy = verdict != "unauthenticated"
    log.info(
        "step07.headed_session_probe",
        verdict=verdict, trustworthy=trustworthy,
        path=_storage_state.mask_path(resolved),
    )
    return trustworthy, resolved


async def maybe_headed_prewarm(
    *,
    sut_root: Path,
    workspace_root: Path,
    active_module: dict | None,
    base_url: str | None,
    research: dict | None = None,
    cli_storage_state: Path | None = None,
    no_auth_capture: bool = False,
    interactive: bool = False,
) -> str:
    """Headed (human-driven) auth prewarm. Returns a status the caller acts on:

    - ``"captured"`` — a fresh session was captured; proceed authenticated.
    - ``"reused"``   — an existing session probed valid; proceed authenticated.
    - ``"skipped"``  — not applicable (disabled, no base URL, no ``auth_flow``,
      or non-interactive); proceed unauthenticated.
    - ``"fallback_mcp"`` — qtea's Playwright isn't installed; the caller should
      fall back to ``mcp`` mode.

    Best-effort and never raises — mirrors :func:`maybe_prewarm_auth`.
    """
    if not auth_capture_enabled(no_auth_capture):
        log.info("step07.headed_prewarm_skip", reason="disabled")
        return "skipped"
    if not base_url:
        log.info("step07.headed_prewarm_skip", reason="no_base_url")
        return "skipped"
    # The SUT needs auth only if Step 6 discovered a login flow.
    auth = (active_module or {}).get("auth_flow") if isinstance(active_module, dict) else None
    entry = auth.get("entry_method") if isinstance(auth, dict) else None
    if not entry:
        log.info("step07.headed_prewarm_skip", reason="no_auth_flow")
        return "skipped"
    if not interactive:
        log.warning(
            "step07.headed_prewarm_non_interactive",
            hint=(
                "Headed login needs a human at the browser but this run has no "
                "TTY/UI. Run `qtea` in a terminal or the desktop UI, or set "
                "`--auth-prewarm-mode mcp` for automated credential login."
            ),
        )
        return "skipped"

    from qtea import headed_auth_capture

    if not headed_auth_capture.is_available():
        log.warning(
            "step07.headed_prewarm_unavailable",
            hint=headed_auth_capture.package_hint(),
        )
        return "fallback_mcp"

    # Auto-download the chromium build on first use (one-time, proxy-aware). If it
    # can't be fetched (offline / blocked mirror), fall back to automated login.
    if not await headed_auth_capture.ensure_chromium_async():
        log.warning(
            "step07.headed_prewarm_no_browser",
            hint=headed_auth_capture.install_hint(),
        )
        return "fallback_mcp"

    # Reuse a valid session; only re-login on a high-confidence stale signal.
    trustworthy, resolved = await _session_is_trustworthy(
        sut_root=sut_root,
        workspace_root=workspace_root,
        base_url=base_url,
        cli_storage_state=cli_storage_state,
    )
    if trustworthy and resolved is not None:
        log.info("step07.headed_prewarm_reuse", path=_storage_state.mask_path(resolved))
        return "reused"

    creds = resolve_login_credentials(active_module, research)
    # Write to the configured override path (if any) so a stable location becomes
    # a "log in once, reuse across runs" store; else the per-SUT convention path.
    output = _storage_state.write_target(sut_root, cli_storage_state)
    _storage_state.ensure_gitignored(sut_root, output)
    log.info("step07.headed_prewarm_start", base_url=base_url, prefill=creds is not None)
    try:
        await headed_auth_capture.capture_headed_login(
            base_url=base_url, output=output, creds=creds,
        )
    except headed_auth_capture.HeadedLoginSkipped:
        log.info(
            "step07.headed_prewarm_user_skipped",
            hint=(
                "User chose to skip authentication for this run — proceeding "
                "unauthenticated. Re-run and confirm login to capture a session."
            ),
        )
        return "skipped"
    except Exception as e:
        log.warning(
            "step07.headed_prewarm_failed",
            error=str(e),
            hint=(
                "Headed login capture failed or was not completed. Re-run and "
                "finish the login in the browser, or set `--auth-prewarm-mode "
                "mcp` for automated credential login."
            ),
        )
        return "skipped"
    log.info("step07.headed_prewarm_success", path=_storage_state.mask_path(output))
    return "captured"


__all__ = [
    "auth_capture_enabled",
    "auth_prewarm_mode",
    "headed_mode_requested",
    "is_applicable",
    "is_interactive_session",
    "load_active_module",
    "login_identity_provider",
    "maybe_headed_prewarm",
    "maybe_prewarm_auth",
    "resolve_login_credentials",
]
