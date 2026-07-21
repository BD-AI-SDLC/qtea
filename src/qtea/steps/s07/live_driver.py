"""Deterministic parent-side Playwright driver for Step 7 live exploration.

Promotes the existing ``_deterministic_live_map`` fallback (in
:mod:`qtea.steps.s07_live_explore`) into the primary exploration path. Reuses
the same ``_DOM_PROBE_JS`` locator probe (verbatim), the same
``_proxy_launch_kwargs`` proxy pattern, and the same live-map JSON contract, so
downstream consumers (Step 7 architect, Step 8 codegen) see an identical shape.

**What this replaces.** The site-explorer LLM agent driving Playwright MCP for
the mechanical work of: navigate → snapshot → probe → serialize. The agent's
turn budget, dollar ceiling, and PreToolUse anti-rat-hole hooks all exist to
bound loops that a deterministic loop cannot enter in the first place.

**What stays LLM-driven.** Two narrow judgment calls, invoked as callbacks:

  * ``on_reveal_needed`` — when a named target from ``test-design.md`` isn't
    found on initial paint, decide which affordance (menu, tab, disclosure
    button) to click to reveal it. The driver clicks and re-probes.
  * ``on_ambiguity`` — when the DOM probe finds a matching element but no
    verified-unique locator, pick the best candidate from the visible probe
    output. Falls through to ``locator_ambiguous=true`` when the judge is
    absent or returns "unresolvable".

MCP-mode auth login stays in the site-explorer agent (see
:func:`qtea.steps.s07_live_explore.explore_strategy_routes`); this driver never
drives credentials. It boots pre-authenticated via ``storage_state_path`` and
degrades to unauthenticated when no session is available.

Best-effort: returns ``None`` on any hard failure (Playwright unavailable,
browser launch error, all routes fail) so the dispatcher can fall back to the
agent path.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from qtea.logging_setup import get_logger

log = get_logger(__name__)


# Default per-nav timeout; matches the agent-path fallback's own default.
# Override via QTEA_LIVE_EXPLORE_FALLBACK_NAV_TIMEOUT_MS (kept for continuity
# with the existing knob).
_DEFAULT_NAV_TIMEOUT_MS = 20_000

# Cap on progressive-disclosure reveals per page: mirrors the agent path's
# _DEFAULT_MAX_REVEALS_PER_PAGE so callouts stay bounded even if the driver
# encounters many "target not found on paint" cases.
_DEFAULT_MAX_REVEALS_PER_PAGE = 4

# Login-URL markers used to detect an auth redirect (mirrors headed_auth_capture).
_LOGIN_URL_MARKERS = (
    "/login", "/signin", "/sign-in", "/sign_in",
    "/sso", "/oauth", "/auth/",
    "login.microsoftonline.com", "accounts.google.com",
    "okta.com", "auth0.com",
)


@dataclass
class RevealContext:
    """Argument to the reveal judge — a single decision request for one target."""

    target_name: str
    target_reach_via: str
    route_path: str
    route_url: str
    # AOM snapshot excerpt of the current page (bounded, ≤5KB by convention).
    snapshot_excerpt: str
    # Candidate affordances the driver observed but didn't yet click.
    # Each dict: {role, name, locator|None} — same shape as an element.
    candidates: list[dict[str, Any]]


@dataclass
class AmbiguityContext:
    """Argument to the ambiguity judge — the element intent plus tie candidates."""

    intent: str  # target name or nav label being resolved
    route_path: str
    # Probe entries with the same role+name that couldn't be disambiguated.
    candidates: list[dict[str, Any]]


@dataclass
class DriverTelemetry:
    """Per-run counters surfaced back to the caller (folded into live-map._telemetry)."""

    routes_requested: int = 0
    routes_explored: int = 0
    routes_truncated_by_cap: int = 0
    routes_skipped_auth_required: int = 0
    reveal_callouts: int = 0
    ambiguity_callouts: int = 0
    llm_tokens_total: int = 0
    elapsed_ms: int = 0
    # Judge callouts may fail silently; count for observability.
    reveal_callouts_failed: int = 0
    ambiguity_callouts_failed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "routes_requested_by_plan": self.routes_requested,
            "routes_explored": self.routes_explored,
            "routes_truncated_by_cap": self.routes_truncated_by_cap,
            "routes_skipped_auth_required": self.routes_skipped_auth_required,
            "reveal_callouts": self.reveal_callouts,
            "reveal_callouts_failed": self.reveal_callouts_failed,
            "ambiguity_callouts": self.ambiguity_callouts,
            "ambiguity_callouts_failed": self.ambiguity_callouts_failed,
            "llm_tokens_total": self.llm_tokens_total,
            "elapsed_ms": self.elapsed_ms,
        }


def _looks_like_auth_redirect(final_url: str, target_url: str) -> bool:
    """True when a navigation ended on a login/SSO screen instead of the target."""
    if not final_url or final_url == target_url:
        return False
    low = final_url.lower()
    return any(marker in low for marker in _LOGIN_URL_MARKERS)


def _make_visit_plan(
    routes: list[str],
    reconciled_targets: list[dict],
    max_pages: int,
) -> tuple[list[dict[str, str]], int]:
    """Build the ordered visit plan and count truncation losses.

    Each plan entry: ``{"path": "/foo", "nav_label": "..."}``. When
    ``nav_label`` is non-empty, the driver navigates to "/" then clicks the
    nav label (in-app navigation for SPA/launcher apps whose targets sit under
    a single route). Returns ``(plan, truncated_count)`` where
    ``truncated_count`` is the number of entries dropped because the plan
    exceeded ``max_pages``.
    """
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for rp in routes:
        p = str(rp or "").strip()
        if p and p.startswith("/") and p not in seen:
            seen.add(p)
            plan.append({"path": p, "nav_label": ""})
    for t in reconciled_targets or []:
        if not isinstance(t, dict):
            continue
        nav_label = str(t.get("nav_label") or "").strip()
        if nav_label:
            key = f"nav::{nav_label.lower()}"
            if key not in seen:
                seen.add(key)
                plan.append({
                    "path": "/",
                    "nav_label": nav_label,
                    "target_name": str(t.get("name") or "").strip(),
                    "reach_via": str(t.get("reach_via") or "").strip(),
                })
    if not plan:
        plan = [{"path": "/", "nav_label": ""}]
    if len(plan) <= max_pages:
        return plan, 0
    return plan[:max_pages], len(plan) - max_pages


def _snapshot_excerpt(elements: list[dict[str, Any]], *, cap: int = 5000) -> str:
    """Compact textual summary of the current page's captured elements, for
    the reveal judge. Bounded so the callout prompt stays small."""
    lines: list[str] = []
    total = 0
    for e in elements:
        role = str(e.get("role") or "")
        name = str(e.get("name") or "")
        loc = e.get("locator")
        loc_str = (
            f" [{loc.get('strategy')}={loc.get('value')!r}]"
            if isinstance(loc, dict) and loc.get("verified_unique") else ""
        )
        line = f"- {role}: {name!r}{loc_str}"
        if total + len(line) + 1 > cap:
            lines.append("- ... (truncated)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


async def _try_nav_label_click(
    page: Any, nav_label: str, *, timeout_ms: int,
) -> tuple[bool, str | None]:
    """Click a nav label using a Playwright role-locator chain, same shape as
    the existing deterministic fallback (kept identical so behavior matches).

    Returns ``(clicked, error_message)``.
    """
    try:
        loc = (
            page.get_by_role("link", name=nav_label, exact=False)
            .or_(page.get_by_role("menuitem", name=nav_label, exact=False))
            .or_(page.get_by_role("button", name=nav_label, exact=False))
            .or_(page.get_by_text(nav_label, exact=False))
        ).first
        await loc.click(timeout=timeout_ms)
        return True, None
    except Exception as e:
        return False, str(e)


async def _wait_networkidle_best_effort(page: Any, *, timeout_ms: int = 5000) -> None:
    """Wait for network to settle, but never block on it (SPAs sometimes never
    fully idle). Swallow any timeout/exception silently."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        return


async def _probe_page(
    page: Any, dom_probe_js: str,
) -> list[dict[str, Any]]:
    """Run the DOM probe on the current page and convert to element dicts.

    Delegates conversion to the existing ``_probe_output_to_elements`` in the
    parent module so the shape is identical to the agent-path fallback.
    """
    from qtea.steps.s07_live_explore import _probe_output_to_elements

    try:
        raw = await page.evaluate(dom_probe_js)
    except Exception as e:
        log.info("step07.driver.probe_error", error=str(e))
        return []
    return _probe_output_to_elements(raw)


async def _try_reveal(
    page: Any,
    *,
    dom_probe_js: str,
    target_name: str,
    reach_via: str,
    route_path: str,
    route_url: str,
    current_elements: list[dict[str, Any]],
    on_reveal_needed: Callable[[RevealContext], Awaitable[str | None]] | None,
    telemetry: DriverTelemetry,
    max_reveals: int,
    timeout_ms: int,
) -> tuple[list[dict[str, Any]], int]:
    """Attempt bounded progressive-disclosure reveals to surface a target.

    If a callout is configured, ask it for a locator string to click; click via
    Playwright text/role fallback ladder; re-probe. Returns
    ``(updated_elements, reveals_used)``.
    """
    if on_reveal_needed is None or max_reveals <= 0:
        return current_elements, 0
    elements = list(current_elements)
    used = 0
    for _ in range(max_reveals):
        try:
            ctx = RevealContext(
                target_name=target_name,
                target_reach_via=reach_via,
                route_path=route_path,
                route_url=route_url,
                snapshot_excerpt=_snapshot_excerpt(elements),
                candidates=[e for e in elements if e.get("locator")],
            )
            telemetry.reveal_callouts += 1
            locator_hint = await on_reveal_needed(ctx)
        except Exception as e:
            telemetry.reveal_callouts_failed += 1
            log.info("step07.driver.reveal_callout_error", error=str(e))
            break
        if not locator_hint:
            # Judge declined to click further.
            break
        clicked, err = await _try_reveal_click(page, locator_hint, timeout_ms=timeout_ms)
        if not clicked:
            log.info(
                "step07.driver.reveal_click_failed",
                target=target_name, hint=locator_hint, error=err,
            )
            break
        used += 1
        await _wait_networkidle_best_effort(page)
        elements = await _probe_page(page, dom_probe_js)
        # If the target name is now surfaced with a name-match, stop.
        if _target_in_elements(target_name, elements):
            break
    return elements, used


async def _try_reveal_click(
    page: Any, locator_hint: str, *, timeout_ms: int,
) -> tuple[bool, str | None]:
    """Click a locator identified by the judge. Interprets ``locator_hint`` as
    a free-form label or role+name string and tries the same role/text fallback
    ladder used elsewhere. The judge is expected to return a NAME (the visible
    label of the affordance) — not a CSS selector — to keep the API narrow.
    """
    try:
        loc = (
            page.get_by_role("button", name=locator_hint, exact=False)
            .or_(page.get_by_role("tab", name=locator_hint, exact=False))
            .or_(page.get_by_role("menuitem", name=locator_hint, exact=False))
            .or_(page.get_by_role("link", name=locator_hint, exact=False))
            .or_(page.get_by_text(locator_hint, exact=False))
        ).first
        await loc.click(timeout=timeout_ms)
        return True, None
    except Exception as e:
        return False, str(e)


def _target_in_elements(name: str, elements: list[dict[str, Any]]) -> bool:
    """Does any captured element have a visible name matching the target?"""
    n = (name or "").strip().lower()
    if not n:
        return False
    for e in elements:
        en = str(e.get("name") or "").strip().lower()
        if en and (en == n or n in en or en in n):
            return True
    return False


async def _resolve_ambiguities(
    elements: list[dict[str, Any]],
    *,
    route_path: str,
    on_ambiguity: Callable[[AmbiguityContext], Awaitable[dict | None]] | None,
    telemetry: DriverTelemetry,
) -> list[dict[str, Any]]:
    """For each element flagged ``locator_ambiguous``, ask the judge to pick.

    Groups ambiguities by (role, name) so we make ONE callout per tie-set, not
    one per element. The judge returns either a candidate dict (verbatim from
    the probe output) or ``None`` — on None we leave ``locator_ambiguous`` set
    (the honest-gap contract).
    """
    if on_ambiguity is None:
        return elements
    # Group by (role, name).
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in elements:
        if e.get("locator_ambiguous") is True:
            key = (str(e.get("role") or ""), str(e.get("name") or ""))
            groups.setdefault(key, []).append(e)
    if not groups:
        return elements
    for (role, name), group in groups.items():
        # Only one element in the group means the ambiguity is intrinsic (no
        # peer to disambiguate against); skip the callout.
        if len(group) < 2:
            continue
        try:
            telemetry.ambiguity_callouts += 1
            picked = await on_ambiguity(AmbiguityContext(
                intent=f"{role}: {name}" if role else name,
                route_path=route_path,
                candidates=list(group),
            ))
        except Exception as e:
            telemetry.ambiguity_callouts_failed += 1
            log.info("step07.driver.ambiguity_callout_error", error=str(e))
            continue
        if not isinstance(picked, dict):
            continue
        # Attach the picked locator to the first group element; mark the rest
        # as still ambiguous (they weren't chosen).
        loc = picked.get("locator")
        if isinstance(loc, dict) and loc.get("verified_unique"):
            head = group[0]
            head["locator"] = loc
            head.pop("locator_ambiguous", None)
            head.pop("ambiguity_reason", None)
    return elements


async def _visit_one_route(
    page: Any,
    *,
    origin: str,
    item: dict[str, str],
    dom_probe_js: str,
    nav_timeout_ms: int,
    max_reveals_per_page: int,
    on_reveal_needed: Callable[[RevealContext], Awaitable[str | None]] | None,
    on_ambiguity: Callable[[AmbiguityContext], Awaitable[dict | None]] | None,
    telemetry: DriverTelemetry,
) -> dict[str, Any]:
    """Navigate to one plan entry, probe, run reveal + ambiguity resolution,
    and return one live-map ``route`` entry."""
    path = item["path"]
    nav_label = item.get("nav_label") or ""
    target_url = f"{origin}{path}" if path != "/" else origin
    display_path = f"{path} (nav: {nav_label})" if nav_label else path
    entry: dict[str, Any] = {
        "path": display_path,
        "exists": True,
        "auth_required": False,
        "redirected_to": None,
        "discovered_from": None,
        "elements": [],
    }
    try:
        await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=nav_timeout_ms,
        )
        await _wait_networkidle_best_effort(page)

        # In-app nav: click the reconciled nav label to reach the target.
        if nav_label:
            clicked, err = await _try_nav_label_click(
                page, nav_label, timeout_ms=nav_timeout_ms,
            )
            if not clicked:
                entry["fallback_reason"] = (
                    f"could not click nav label {nav_label!r}: {err}"
                )
            else:
                await _wait_networkidle_best_effort(page)

        # Auth-gate detection.
        final = page.url or target_url
        if _looks_like_auth_redirect(final, target_url):
            entry["exists"] = False
            entry["auth_required"] = True
            entry["redirected_to"] = final
            telemetry.routes_skipped_auth_required += 1
            return entry
        if final != target_url:
            entry["url"] = final

        # Primary probe.
        elements = await _probe_page(page, dom_probe_js)

        # Progressive-disclosure reveals if a target name was requested.
        target_name = item.get("target_name") or ""
        if target_name and not _target_in_elements(target_name, elements):
            reach_via = item.get("reach_via") or ""
            elements, _used = await _try_reveal(
                page,
                dom_probe_js=dom_probe_js,
                target_name=target_name,
                reach_via=reach_via,
                route_path=display_path,
                route_url=final,
                current_elements=elements,
                on_reveal_needed=on_reveal_needed,
                telemetry=telemetry,
                max_reveals=max_reveals_per_page,
                timeout_ms=nav_timeout_ms,
            )

        # Ambiguity resolution.
        elements = await _resolve_ambiguities(
            elements,
            route_path=display_path,
            on_ambiguity=on_ambiguity,
            telemetry=telemetry,
        )

        entry["elements"] = elements
        telemetry.routes_explored += 1
    except Exception as e:
        entry["exists"] = False
        entry["fallback_reason"] = f"navigate/probe failed: {e}"
    return entry


async def drive_live_exploration(
    *,
    base_url: str,
    routes: list[str],
    reconciled_targets: list[dict],
    storage_state_path: Path | None,
    max_pages: int,
    max_reveals_per_page: int = _DEFAULT_MAX_REVEALS_PER_PAGE,
    nav_timeout_ms: int | None = None,
    on_reveal_needed: Callable[[RevealContext], Awaitable[str | None]] | None = None,
    on_ambiguity: Callable[[AmbiguityContext], Awaitable[dict | None]] | None = None,
) -> dict[str, Any] | None:
    """Drive headless Playwright over the plan's target routes and probe each.

    Returns a live-map dict matching ``schemas/live-map.schema.json`` (with a
    ``_telemetry`` block), or ``None`` on Playwright unavailability / hard
    launch failure.

    Never raises: every per-route error is folded into that route's entry with
    ``exists: false`` + ``fallback_reason`` so partial success still returns a
    usable map.
    """
    try:
        from qtea.headed_auth_capture import _proxy_launch_kwargs, is_available
        from qtea.steps.s07_live_explore import _DOM_PROBE_JS
    except Exception:
        return None
    if not is_available():
        log.info("step07.driver.skip", reason="playwright_unavailable")
        return None
    from playwright.async_api import async_playwright

    if nav_timeout_ms is None:
        try:
            nav_timeout_ms = int(
                os.environ.get("QTEA_LIVE_EXPLORE_FALLBACK_NAV_TIMEOUT_MS", "")
                or _DEFAULT_NAV_TIMEOUT_MS
            )
        except ValueError:
            nav_timeout_ms = _DEFAULT_NAV_TIMEOUT_MS

    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}".rstrip("/")
    plan, truncated = _make_visit_plan(routes, reconciled_targets, max_pages)

    telemetry = DriverTelemetry(
        routes_requested=len(routes) + len(
            [t for t in (reconciled_targets or []) if t.get("nav_label")],
        ),
        routes_truncated_by_cap=truncated,
    )
    if truncated:
        log.warning(
            "step07.driver.plan_truncated",
            requested=telemetry.routes_requested,
            explored_cap=max_pages,
            truncated=truncated,
        )

    started = time.time()
    captured: list[dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, **_proxy_launch_kwargs(),
            )
            try:
                context = await browser.new_context(
                    storage_state=str(storage_state_path)
                    if storage_state_path is not None else None,
                )
                page = await context.new_page()
                for item in plan:
                    entry = await _visit_one_route(
                        page,
                        origin=origin,
                        item=item,
                        dom_probe_js=_DOM_PROBE_JS,
                        nav_timeout_ms=nav_timeout_ms,
                        max_reveals_per_page=max_reveals_per_page,
                        on_reveal_needed=on_reveal_needed,
                        on_ambiguity=on_ambiguity,
                        telemetry=telemetry,
                    )
                    captured.append(entry)
            finally:
                await browser.close()
    except Exception as e:
        log.warning("step07.driver.launch_error", error=str(e))
        return None

    telemetry.elapsed_ms = int((time.time() - started) * 1000)

    if not captured:
        return None
    live_map: dict[str, Any] = {
        "base_url": origin,
        "routes": captured,
        "_telemetry": telemetry.as_dict(),
    }
    log.info(
        "step07.driver.done",
        routes=len(captured),
        elements=_count_elements(live_map),
        elapsed_ms=telemetry.elapsed_ms,
        truncated=truncated,
        reveal_callouts=telemetry.reveal_callouts,
        ambiguity_callouts=telemetry.ambiguity_callouts,
    )
    return live_map


def _count_elements(live_map: dict[str, Any]) -> int:
    total = 0
    for r in live_map.get("routes") or []:
        if isinstance(r, dict) and r.get("exists") is not False:
            total += len(r.get("elements") or [])
    return total
