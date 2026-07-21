"""Universal headed (human-driven) login capture for Step 7 auth prewarm.

qtea is a **local-only** tool, so a human is essentially always present. When a
SUT needs authentication and no valid session exists, we open the SUT's base URL
in a *visible* browser, let the operator log in by any means (MFA / SSO / captcha
— things automated credential-typing cannot complete), and capture the resulting
Playwright ``storage_state`` for reuse.

Unlike :mod:`qtea.auth_capture` (which drives the SUT's *own* sign-in helper in a
subprocess), this path is **helper-independent and SUT-agnostic**: it needs only a
base URL and qtea's own Playwright (the optional ``qtea[auth]`` extra). The
captured file lands at the same convention path
(:data:`qtea.auth_capture.DEFAULT_OUTPUT_REL`), so ``storage_state.resolve()``
finds it and Step 7 live-exploration / Step 9 heal boot authenticated with no
downstream change.

Two entry points:

- :func:`capture_headed_login` — open a headed browser, wait for the human to
  confirm, save ``storage_state``.
- :func:`probe_authenticated` — cheap, headless, **conservative** check of whether
  a resolved session is actually valid. Returns ``"unauthenticated"`` only on a
  high-confidence signal (login/SSO redirect or a visible password field);
  everything else (incl. errors) → proceed, never force a needless re-auth loop.

Security: the human types credentials into a real browser, so nothing reaches the
LLM (a win over ``mcp`` mode). The output is a credential — written owner-only via
:func:`qtea.proxy.set_owner_only_perms`, never logged/committed/embedded.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Literal

from qtea.logging_setup import get_logger

log = get_logger(__name__)

# One-time chromium download timeout (seconds). Generous — a corporate proxy /
# mirror can be slow. Override via QTEA_PLAYWRIGHT_INSTALL_TIMEOUT_S.
_INSTALL_TIMEOUT_S = 900

# Same output convention as the subprocess auth-capture (Use case A — the
# per-SUT persistent file that storage_state.resolve() finds first after any
# explicit CLI/env override).
from qtea.auth_capture import DEFAULT_OUTPUT_REL  # noqa: E402  (re-export path)

# Navigation timeout for goto() in both capture and probe (ms). The human's
# login time itself is NOT bounded here — the HITL confirm waits for the operator.
_NAV_TIMEOUT_MS = 30_000

# Fixed id for the single headed-login confirm question — shared between
# _default_confirm (builds the Question) and the reopen registry (keys the
# live browser-page callback), so callers never hardcode the string twice.
_HEADED_LOGIN_QID = "AUTH-HEADED-LOGIN"


class HeadedLoginSkipped(Exception):
    """Raised when the user explicitly chose to skip authentication rather
    than confirm login completion, instead of just letting the prompt time
    out or return ambiguously.

    Propagates out of :func:`capture_headed_login` (skipping the
    ``storage_state()`` capture) so :func:`qtea.steps.s07_auth_prewarm.maybe_headed_prewarm`
    can log an intentional skip distinctly from a genuine failure.
    """


# Side-channel registry for the "reopen browser window" HITL action. Populated
# by capture_headed_login right before blocking on the human confirm, keyed by
# question id. Deliberately NOT threaded through Question.metadata (a
# "structured payload" of plain data) — this holds a live callable bound to a
# live Playwright page and event loop, so it stays in this pipeline-side
# module instead of crossing into the HITL question schema.
_reopen_registry: dict[str, Callable[[], None]] = {}


def _register_reopen(question_id: str, page) -> None:
    """Capture the CURRENT running loop (the pipeline's own loop, since this
    always runs inside ``capture_headed_login``'s coroutine) + page, and
    register a thread-safe reopen callback."""
    loop = asyncio.get_running_loop()

    def _reopen() -> None:
        async def _do() -> None:
            with contextlib.suppress(Exception):
                await page.bring_to_front()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_do(), loop)

    _reopen_registry[question_id] = _reopen


def _unregister_reopen(question_id: str) -> None:
    _reopen_registry.pop(question_id, None)


def request_browser_reopen(question_id: str) -> bool:
    """Bring the live headed-login browser window to the foreground.

    Safe to call from ANY thread — the Flet UI thread calls this directly;
    it schedules the actual Playwright call onto the pipeline's own event
    loop via ``asyncio.run_coroutine_threadsafe`` internally. Returns ``True``
    if a live callback was found and invoked, ``False`` if there's no active
    headed-login browser (e.g. a stale dialog) — callers should no-op on
    ``False``.
    """
    cb = _reopen_registry.get(question_id)
    if cb is None:
        return False
    cb()
    return True


def _proxy_launch_kwargs() -> dict:
    """Return ``{"proxy": {"server": URL}}`` when a corporate proxy is configured,
    else ``{}``.

    Playwright's bundled Chromium does NOT honor ``HTTPS_PROXY`` env vars — the
    proxy must be passed explicitly to ``launch()``. Without this, navigating to
    an internal host that is only resolvable/reachable via the corporate proxy
    fails with ``net::ERR_NAME_NOT_RESOLVED``. Mirrors the JIT runtime's
    ``_proxy_url_to_inject`` (``qtea_runtime.py.tpl``): ``QTEA_PROXY`` wins, then
    ``HTTPS_PROXY`` / ``https_proxy``; disabled entirely by
    ``QTEA_DISABLE_PROXY_INJECT=1``.
    """
    if os.environ.get("QTEA_DISABLE_PROXY_INJECT") == "1":
        return {}
    url = (
        os.environ.get("QTEA_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
    )
    return {"proxy": {"server": url}} if url else {}

# Path/host substrings that, on the landing page, are a high-confidence "you are
# NOT authenticated" signal. Kept deliberately conservative (see module docstring).
_LOGIN_URL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/sso",
    "/oauth",
    "/auth/",
    "/account/login",
    "/session/new",
    "login.microsoftonline.com",
    "accounts.google.com",
    "okta.com",
    "auth0.com",
)

ProbeResult = Literal["authenticated", "unauthenticated", "ambiguous"]


def _looks_unauthenticated(final_url: str, password_visible: bool) -> bool:
    """Pure, conservative classifier for the landing page after loading a session.

    High-confidence "not authenticated" iff the landing URL matches a login/SSO
    marker OR a password field is visible. Everything else is treated as
    authenticated (the caller never re-logs-in on ambiguity).
    """
    url = (final_url or "").lower()
    if any(m in url for m in _LOGIN_URL_MARKERS):
        return True
    return bool(password_visible)


def is_available() -> bool:
    """True when qtea's own Playwright (the ``qtea[auth]`` extra) is importable.

    The orchestrator uses this to fall back to ``mcp`` mode with a hint rather
    than breaking the run when Playwright isn't installed.
    """
    try:
        import playwright.async_api  # noqa: F401
    except Exception:
        return False
    return True


def install_hint() -> str:
    """One-line hint for a missing chromium *browser build* (the download failed)."""
    return (
        "headed login needs a Playwright chromium build. qtea auto-downloads it "
        "on first use; if that failed, check network/proxy or set "
        "PLAYWRIGHT_DOWNLOAD_HOST to an internal mirror (or run "
        "`playwright install chromium` manually)."
    )


def package_hint() -> str:
    """One-line hint when the Playwright Python *package* isn't importable.

    Distinct from :func:`install_hint` (which is about the browser binary):
    ``playwright`` is a core qtea dependency, so a failed import means the qtea
    install itself is incomplete/broken — not a missing browser. Re-syncing the
    install fixes it; ``playwright install chromium`` would not.
    """
    return (
        "the Playwright Python package isn't importable — the qtea install is "
        "incomplete. Re-sync it (e.g. `uv tool install --editable <qtea-repo> "
        "--reinstall`), then re-run. This is NOT a missing-browser issue."
    )


def _browsers_dir() -> Path:
    """Where Playwright stores its browser builds (respects PLAYWRIGHT_BROWSERS_PATH)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _chromium_installed() -> bool:
    """Fast heuristic: is a chromium build already present? Keeps subsequent runs
    instant (no install subprocess), mirroring node_env's reuse behavior."""
    d = _browsers_dir()
    try:
        return any(d.glob("chromium-*")) or any(d.glob("chromium_headless_shell-*"))
    except OSError:
        return False


def ensure_chromium(console=None) -> bool:
    """Ensure a Playwright chromium build exists; download it once if not.

    Mirrors :func:`qtea.node_env.ensure_node` — detect first (instant on repeat
    runs), otherwise run ``python -m playwright install chromium`` as a subprocess
    with qtea's proxy env so it works behind a corporate proxy / internal mirror.
    Returns ``True`` when chromium is available afterwards, ``False`` on failure
    (caller then falls back to ``mcp``). Never raises.
    """
    if _chromium_installed():
        return True

    from rich.console import Console

    con = console or Console(stderr=True)
    con.print(
        "[dim]Downloading a browser for interactive login (one-time; "
        "this may take a minute)…[/]"
    )
    try:
        from qtea.proxy import with_proxy_env

        env = with_proxy_env()
    except Exception:
        env = dict(os.environ)
    try:
        timeout = int(
            os.environ.get("QTEA_PLAYWRIGHT_INSTALL_TIMEOUT_S", "") or _INSTALL_TIMEOUT_S
        )
    except ValueError:
        timeout = _INSTALL_TIMEOUT_S
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        con.print("[yellow]warn:[/] browser download timed out.")
        return False
    except Exception as e:
        con.print(f"[yellow]warn:[/] browser download failed: {e}")
        return False
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-500:]
        con.print(f"[yellow]warn:[/] browser download failed:\n{tail}")
        log.warning("step07.chromium_install_failed", returncode=result.returncode)
        return False
    con.print("[dim]Browser ready.[/]")
    log.info("step07.chromium_installed")
    return True


async def ensure_chromium_async(console=None) -> bool:
    """Async wrapper — runs the (blocking) :func:`ensure_chromium` off the loop."""
    return await asyncio.to_thread(ensure_chromium, console)


def _default_confirm(base_url: str) -> None:
    """Blocking 'I have finished logging in' acknowledgement.

    Reuses the shared HITL channel (:func:`qtea.hitl.prompt_user`), which renders
    on a TTY (CLI, unchanged) *and* in the Flet UI (a bespoke "headed_login"
    dialog, see :mod:`qtea.ui.components.hitl_dialog`) via the hitl bridge — so
    we never build a parallel dialog. Raises :class:`HeadedLoginSkipped` if the
    user explicitly chose to skip rather than confirm; otherwise returns
    normally (whether answered or the non-TTY/non-UI empty-dict fallback).
    """
    from qtea.hitl import RESOLUTION_HEADED_LOGIN_SKIP, Question, prompt_user

    q = Question(
        id=_HEADED_LOGIN_QID,
        kind="clarification",
        prompt_text=(
            "A browser window has opened. Complete the login (including any "
            "MFA / SSO), then press Enter here to capture the session and "
            "continue."
        ),
        context=f"Waiting for you to finish logging in at {base_url}",
        metadata={
            "type": "headed_login",
            "base_url": base_url,
            "started_at": time.monotonic(),
        },
    )
    result = prompt_user([q], agent_label="Step 7 Headed Login")
    entry = result.get(q.id)
    if entry is not None and entry[0] == RESOLUTION_HEADED_LOGIN_SKIP:
        raise HeadedLoginSkipped("user chose to skip authentication")


async def _try_prefill(page, creds: tuple[str, str]) -> None:
    """Best-effort credential pre-fill so the human only completes MFA/submit.

    Silent no-op if the expected fields aren't found — never raises, never
    submits (the human drives submission so MFA/redirects are handled naturally).
    """
    username, password = creds
    try:
        pw = page.locator("input[type=password]").first
        if await pw.count() == 0 or not await pw.is_visible():
            return
        for sel in (
            "input[type=email]",
            'input[name*="user" i]',
            'input[name*="email" i]',
            'input[id*="user" i]',
            "input[type=text]",
        ):
            field = page.locator(sel).first
            try:
                if await field.count() and await field.is_visible():
                    await field.fill(username)
                    break
            except Exception:
                continue
        await pw.fill(password)
        log.info("step07.headed_login_prefilled")
    except Exception as e:
        log.info("step07.headed_login_prefill_skip", error=str(e))


async def capture_headed_login(
    base_url: str,
    output: Path,
    *,
    creds: tuple[str, str] | None = None,
    nav_timeout_ms: int = _NAV_TIMEOUT_MS,
    confirm: Callable[[], None] | None = None,
) -> Path:
    """Open ``base_url`` in a visible chromium, let the human log in, save state.

    Returns the absolute path of the written ``storage_state`` file. Raises on an
    unrecoverable error (Playwright/browser missing, navigation failure) — the
    caller (:mod:`qtea.steps.s07_auth_prewarm`) wraps this best-effort and never
    lets it break Step 7.

    ``confirm`` is injectable for tests; by default it uses the shared HITL
    channel. It is a *blocking* call, run in a worker thread so the browser stays
    responsive while the human works.
    """
    from playwright.async_api import async_playwright

    out_path = Path(output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if creds is not None:
        # Defensive redaction: creds are typed into a real browser, never logged
        # or sent to a model, but register them so any incidental capture masks.
        try:
            from qtea.logging_setup import register_secret_values

            register_secret_values([creds[0], creds[1]])
        except Exception:
            pass

    _confirm = confirm or (lambda: _default_confirm(base_url))

    log.info("step07.headed_login_start", base_url=base_url)
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, **_proxy_launch_kwargs())
        except Exception as e:
            raise RuntimeError(
                f"could not launch a headed chromium — {install_hint()} ({e})"
            ) from e
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(base_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            if creds is not None:
                await _try_prefill(page, creds)
            _register_reopen(_HEADED_LOGIN_QID, page)
            try:
                # Block on the human (in a worker thread; keeps the browser live).
                await asyncio.to_thread(_confirm)
            except HeadedLoginSkipped:
                log.info("step07.headed_login_skipped_by_user", base_url=base_url)
                raise
            finally:
                _unregister_reopen(_HEADED_LOGIN_QID)
            await context.storage_state(path=str(out_path))
        finally:
            await browser.close()

    _set_owner_only(out_path)
    log.info("step07.headed_login_success", path=_mask(out_path))
    return out_path


async def probe_authenticated(
    base_url: str,
    storage_state_path: Path,
    *,
    nav_timeout_ms: int = _NAV_TIMEOUT_MS,
) -> ProbeResult:
    """Conservatively check whether ``storage_state_path`` is still a live session.

    Returns ``"unauthenticated"`` ONLY on a high-confidence signal — the landing
    page URL matches a login/SSO marker, or a password field is visible on it.
    Any other outcome (normal app page, error, timeout, Playwright missing) →
    ``"authenticated"`` / ``"ambiguous"`` so the caller keeps using the session
    and proceeds. This never forces a needless re-login.
    """
    if not is_available():
        return "ambiguous"
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, **_proxy_launch_kwargs())
            try:
                context = await browser.new_context(
                    storage_state=str(storage_state_path)
                )
                page = await context.new_page()
                await page.goto(
                    base_url, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )
                final_url = page.url or ""
                password_visible = False
                try:
                    pw = page.locator("input[type=password]").first
                    password_visible = bool(await pw.count()) and await pw.is_visible()
                except Exception:
                    password_visible = False
                if _looks_unauthenticated(final_url, password_visible):
                    log.info(
                        "step07.headed_probe_unauth",
                        signal="password_field" if password_visible else "login_url",
                    )
                    return "unauthenticated"
                return "authenticated"
            finally:
                await browser.close()
    except Exception as e:
        log.info("step07.headed_probe_ambiguous", error=str(e))
        return "ambiguous"


# Conservative default thresholds for the launcher-tile pass — a "tile" is a
# card-sized clickable in the body area. Tuned wide enough to catch typical
# dashboard/solution-picker cards without picking up bullets or icon buttons;
# override via env if a SUT's tiles fall outside this range.
_LAUNCHER_MIN_WIDTH_PX = 100
_LAUNCHER_MIN_HEIGHT_PX = 40

# JS run once on the authenticated root to harvest the app's PRIMARY entry-point
# vocabulary. Returns ``{labels, source}``. Two ADDITIVE tiers (both may fire on
# the same app):
#   Tier 1 (landmark) — accessible names of links/menu-items/tabs inside
#     navigation landmarks (nav bar, side menu, tab strip, header): the app's
#     own label for each page.
#   Tier 2 (launcher) — body-area clickable CARDS that act as top-level entry
#     points (a "solution picker" home). Captures BOTH semantic clickables
#     (a/button/[role]) AND roleless clickables (a plain text node inside a
#     styled clickable <div>/<p> with cursor:pointer -- the pattern common in
#     dashboard apps where the tile has no ARIA role, no id, no test-id). AOM
#     alone can't see roleless tiles; cursor:pointer is the only externally-
#     visible signal a JS-only clickable exposes.
# Tier 2 is ADDITIVE (not gated on Tier-1 sparsity) because launcher-home apps
# commonly have BOTH a header nav (My Pages, User menu, period picker) AND a
# body tile grid; the two are not mutually exclusive. Source is
# ``"landmark+launcher"`` when both contributed, else the single tier's name.
# Either way this is vocabulary Step 4's test design and Step 6 page-object
# class names rarely match verbatim -- so it lets the site-explorer map a
# tested feature to the right entry point deterministically instead of guessing
# at runtime. Read-only, returns labels only (never HTML), deduped + capped.
_NAV_HARVEST_JS = (
    r"""({ cap, minW, minH }) => {
  const LANDMARKS = [
    "nav", "[role='navigation']", "[role='menubar']", "[role='menu']",
    "[role='tablist']", "aside", "header"
  ].join(",");
  const ITEMS =
    "a[href], [role='link'], [role='menuitem'], [role='tab'], button, [role='button']";
  // Roleless-tile candidate tags. Kept narrow to avoid computedStyle on every
  // <div>; adjust if a SUT's tile uses an atypical container element.
  const ROLELESS_TAGS = "div, span, li, section, article, p, td";
  const DATA = "table, [role='table'], [role='grid'], [role='row'], [role='listitem'], li";
  const seen = new Set();
  const out = [];
  const nameOf = (el) => {
    let name = (el.getAttribute("aria-label") || "").trim();
    if (!name) name = (el.innerText || el.textContent || "").trim();
    return name.replace(/\s+/g, " ");
  };
  const visible = (el) => {
    if (el.closest("[aria-hidden='true']")) return false;
    const r = el.getBoundingClientRect();
    return !(r.width === 0 && r.height === 0);
  };
  const push = (name) => {
    if (!name || name.length > 60) return false;
    const key = name.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    out.push(name);
    return out.length >= cap;
  };

  // Tier 1 -- navigation landmarks.
  for (const root of document.querySelectorAll(LANDMARKS)) {
    if (root.closest("[aria-hidden='true']")) continue;
    for (const el of root.querySelectorAll(ITEMS)) {
      if (!visible(el)) continue;
      if (push(nameOf(el))) return { labels: out, source: "landmark" };
    }
  }
  const landmarkCount = out.length;

  // Tier 2 -- launcher/landing tiles (ADDITIVE: always runs, not gated on
  // landmark sparsity). A launcher-home may have both a header nav and a body
  // tile grid.
  const main = document.querySelector("main, [role='main']") || document.body;
  const tiles = [];
  const seenNode = new WeakSet();

  // Tier-2a: semantic clickables in the body (a/button/[role=...]).
  for (const el of main.querySelectorAll(ITEMS)) {
    if (!visible(el)) continue;
    if (el.closest(LANDMARKS)) continue;   // not topbar / nav chrome
    if (el.closest(DATA)) continue;        // not content / data links
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < minH) continue;
    const name = nameOf(el);
    if (name.length < 2 || name.length > 40) continue;
    tiles.push(name);
    seenNode.add(el);
  }

  // Tier-2b: roleless clickables (React <div>/<p> with cursor:pointer, no
  // ARIA role). getComputedStyle forces layout, so gate on cheap DOM checks
  // first (tag class, not already an interactive, not inside one, size, text
  // length, visibility) before reading the style.
  for (const el of main.querySelectorAll(ROLELESS_TAGS)) {
    if (seenNode.has(el)) continue;
    if (el.matches(ITEMS)) continue;
    if (el.closest(ITEMS)) continue;
    if (el.closest(LANDMARKS)) continue;
    if (el.closest(DATA)) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < minH) continue;
    const name = nameOf(el);
    if (name.length < 2 || name.length > 40) continue;
    if (getComputedStyle(el).cursor !== "pointer") continue;
    tiles.push(name);
  }

  // Drop the >=2-sibling gate: a single-tile launcher (rare but real -- an
  // app with one solution area) should still be captured, especially when
  // Tier-1 found nothing to trade off against.
  for (const name of tiles) { if (push(name)) break; }

  const source =
    out.length > landmarkCount ? (landmarkCount ? "landmark+launcher" : "launcher") : "landmark";
  return { labels: out, source };
}"""
)


async def harvest_nav_labels(
    base_url: str,
    storage_state_path: Path | None,
    *,
    nav_timeout_ms: int = _NAV_TIMEOUT_MS,
    cap: int = 40,
) -> list[str]:
    """Best-effort, zero-LLM harvest of the app's top-level entry-point labels.

    Boots a headless chromium (authenticated when ``storage_state_path`` is
    given), loads ``base_url``, and returns the app's real page vocabulary via
    two tiers (see :data:`_NAV_HARVEST_JS`): navigation-landmark items first,
    and — when those are sparse — a fallback that harvests body-level launcher
    tiles (a "solution picker" home). Feeds the site-explorer prompt so it can
    map tested features to the correct entry point without runtime guessing.

    Never raises: any failure (Playwright missing, launch/nav error, empty page)
    returns ``[]`` and the explorer falls back to discovering the nav live.
    """
    if not is_available():
        log.info("step07.nav_harvest_skip", reason="playwright_unavailable")
        return []
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, **_proxy_launch_kwargs())
            try:
                context = await browser.new_context(
                    storage_state=str(storage_state_path)
                    if storage_state_path is not None
                    else None
                )
                page = await context.new_page()
                await page.goto(
                    base_url, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )
                # SPAs render nav after hydration — give the network a brief
                # chance to settle, but never block the harvest on it.
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                result = await page.evaluate(
                    _NAV_HARVEST_JS,
                    {
                        "cap": cap,
                        "minW": _LAUNCHER_MIN_WIDTH_PX,
                        "minH": _LAUNCHER_MIN_HEIGHT_PX,
                    },
                )
            finally:
                await browser.close()
    except Exception as e:
        log.info("step07.nav_harvest_error", error=str(e))
        return []

    if isinstance(result, dict):
        labels, source = result.get("labels") or [], result.get("source", "landmark")
    else:  # defensive: tolerate a bare-list return shape
        labels, source = result or [], "landmark"
    clean = [s for s in labels if isinstance(s, str) and s.strip()]
    log.info("step07.nav_harvest_done", label_count=len(clean), source=source)
    return clean


def _set_owner_only(path: Path) -> None:
    try:
        from qtea.proxy import set_owner_only_perms

        set_owner_only_perms(path)
    except Exception:
        pass


def _mask(path: Path) -> str:
    try:
        from qtea.storage_state import mask_path

        return mask_path(path)
    except Exception:
        return Path(path).name


__all__ = [
    "DEFAULT_OUTPUT_REL",
    "HeadedLoginSkipped",
    "ProbeResult",
    "capture_headed_login",
    "ensure_chromium",
    "ensure_chromium_async",
    "harvest_nav_labels",
    "install_hint",
    "is_available",
    "package_hint",
    "probe_authenticated",
    "request_browser_reopen",
]
