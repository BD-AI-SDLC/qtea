"""Pre-codegen live exploration (Gap A).

A real automation engineer, before writing tests, opens the web app and
confirms the pages named in the manual test cases actually exist and looks at
their structure. The qtea architect (Step 7) is otherwise forbidden to touch
the live app (it plans from the static inventory), so a strategy that assumes a
page/route which doesn't exist is only discovered at Step 9 runtime — as a
hard-to-classify failure.

This module adds that missing step: before the architect reasons, it navigates
each distinct route referenced in ``test-strategy.md`` via the Playwright MCP
browser (authenticated with the resolved storage-state), captures a light AOM
digest per route, and writes ``artifacts/step07/live-map.json``. A route that
404s or unexpectedly redirects to login is flagged so the architect can raise a
``[CLARIFICATION NEEDED]`` instead of planning blind.

Best-effort and fully gated: any failure (no base URL, MCP unavailable, agent
error, unparseable output) returns ``None`` and the pipeline proceeds exactly
as before. Toggle with ``QTEA_LIVE_EXPLORE`` (default on); auto-skips when
``QTEA_NO_LLM_RESOLVE=1`` (zero-LLM CI mode) or no base URL is resolvable.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from qtea import storage_state as _storage_state
from qtea.claude_runner import run_agent
from qtea.config import package_resource_root
from qtea.logging_setup import get_logger

log = get_logger(__name__)

# Bound the number of routes we probe so exploration cost stays predictable on
# a large strategy. Override with QTEA_LIVE_EXPLORE_MAX_ROUTES.
_DEFAULT_MAX_ROUTES = 12

# Per-run exploration timeout (seconds). One navigate+snapshot is a few seconds;
# the whole pass over ~12 routes fits comfortably. Override via
# QTEA_LIVE_EXPLORE_TIMEOUT_S.
_DEFAULT_TIMEOUT_S = 300

_URL_RE = re.compile(r"https?://[^\s`'\"<>)\]]+")
# Path-like tokens: a leading slash + at least one path char, stopping at
# whitespace/quotes/backticks. Excludes bare "/" (handled separately) and
# obvious file globs.
_PATH_RE = re.compile(r"(?<![\w/])/[A-Za-z0-9][A-Za-z0-9\-_/]*")


def _live_explore_enabled() -> bool:
    if os.environ.get("QTEA_LIVE_EXPLORE", "1") == "0":
        return False
    # Symmetric with the JIT LLM dial: zero-LLM CI runs skip live exploration.
    return os.environ.get("QTEA_NO_LLM_RESOLVE") != "1"


def _resolve_base_url(research: dict | None) -> str | None:
    """Base URL for the SUT. Step 6 mirrors the resolved value into
    ``SUT_BASE_URL``; fall back to ``research.json``'s ``url_resolution.value``."""
    env = (os.environ.get("SUT_BASE_URL") or "").strip()
    if env:
        return env
    if isinstance(research, dict):
        ur = research.get("url_resolution")
        if isinstance(ur, dict):
            val = (ur.get("value") or "").strip()
            if val:
                return val
    return None


def _extract_routes(strategy_text: str, base_url: str, max_routes: int) -> list[str]:
    """Distinct route paths referenced by the strategy, resolved against the
    SUT origin. Absolute URLs off the SUT origin are dropped (we only probe the
    app under test — never navigate off-origin while authenticated). Always
    includes the site root ``/``.
    """
    base = urlparse(base_url)
    base_origin = f"{base.scheme}://{base.netloc}".rstrip("/")
    paths: list[str] = ["/"]

    def _add(path: str) -> None:
        p = path.rstrip("/") or "/"
        # Strip querystrings/fragments — probing the bare path is enough and
        # avoids leaking session-ish query params into the map.
        p = p.split("?", 1)[0].split("#", 1)[0]
        if p and p not in paths:
            paths.append(p)

    for m in _URL_RE.findall(strategy_text or ""):
        u = urlparse(m)
        origin = f"{u.scheme}://{u.netloc}".rstrip("/")
        if origin == base_origin and u.path:
            _add(u.path)
    for mo in _PATH_RE.finditer(strategy_text or ""):
        m = mo.group(0)
        # Skip file paths in prose: the char right after the matched path is a
        # dot (the regex stops before `.`), so "/tests/foo.py" would otherwise
        # leak as "/tests/foo". Also skip common non-route source dirs.
        nxt = (strategy_text[mo.end():mo.end() + 1] or "")
        if nxt == ".":
            continue
        first_seg = m.strip("/").split("/", 1)[0].lower()
        if first_seg in {"tests", "test", "src", "node_modules", "dist", "build"}:
            continue
        _add(m)

    return paths[:max_routes]


def _build_explore_prompt(base_url: str, routes: list[str]) -> str:
    route_lines = "\n".join(f"  - {r}" for r in routes)
    return (
        f"Confirm which of the following routes exist on the SUT and capture a "
        f"light structural digest of each, so the test architect can plan "
        f"against reality instead of assumptions.\n\n"
        f"SUT base URL: `{base_url}`\n"
        f"Routes to probe (paths are relative to the base URL):\n{route_lines}\n\n"
        f"For each route: call `mcp__playwright__browser_navigate` to the full "
        f"URL (base + path), then `mcp__playwright__browser_snapshot` to read "
        f"the accessibility tree. Record: whether the app page rendered "
        f"(`exists`), whether it bounced to a LOGIN/SSO page because the "
        f"browser is not authenticated (`auth_required`), the final URL if it "
        f"redirected (`redirected_to`), and up to ~8 salient interactive "
        f"roles/names you observe (`notable_roles`, e.g. \"button: Sign in\", "
        f"\"link: New Chat\"). Do NOT dump the full DOM; summarise.\n\n"
        f"CRITICAL — distinguish three outcomes:\n"
        f"  - App page rendered → `exists: true`, `auth_required: false`.\n"
        f"  - Bounced to a login / SSO / identity-provider page → "
        f"`exists: false`, `auth_required: true`. The page almost certainly "
        f"EXISTS; it is just gated. Do NOT report it as non-existent.\n"
        f"  - 404 / error / genuinely-missing page → `exists: false`, "
        f"`auth_required: false`.\n\n"
        f"Respond with ONLY a JSON object (first char `{{`, last char `}}`, no "
        f"prose, no fences) of shape:\n"
        f'{{"base_url": "<base>", "routes": [{{"path": "/foo", "exists": true, '
        f'"auth_required": false, "redirected_to": null, '
        f'"notable_roles": ["button: Sign in"]}}]}}'
    )


async def explore_strategy_routes(
    *,
    strategy_text: str,
    research: dict | None,
    sut_root: Path,
    workspace_root: Path,
    out_dir: Path,
    workdir: Path,
    cli_storage_state: str | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any] | None:
    """Run the live-exploration pass. Returns the parsed live-map dict (also
    written to ``out_dir/live-map.json``) or ``None`` when skipped/failed.

    Never raises — exploration is a best-effort enhancement, not a gate.
    """
    if not _live_explore_enabled():
        log.info("step07.live_explore_disabled")
        return None

    base_url = _resolve_base_url(research)
    if not base_url:
        log.info("step07.live_explore_skip_no_base_url")
        return None

    try:
        max_routes = int(
            os.environ.get("QTEA_LIVE_EXPLORE_MAX_ROUTES", "")
            or _DEFAULT_MAX_ROUTES
        )
    except ValueError:
        max_routes = _DEFAULT_MAX_ROUTES
    routes = _extract_routes(strategy_text, base_url, max_routes)
    if not routes:
        log.info("step07.live_explore_skip_no_routes")
        return None

    agent = package_resource_root() / "agents" / "site-explorer.agent.md"
    if not agent.is_file():
        log.warning("step07.live_explore_agent_missing", path=str(agent))
        return None

    workdir.mkdir(parents=True, exist_ok=True)

    # Boot the MCP browser already authenticated when a storage-state is
    # available (auth-capture or a prior run). Same resolution the heal flow
    # uses. Missing state → the agent lands on public pages / login, still
    # informative for existence checks.
    storage_state_path = _storage_state.resolve(
        sut_root=sut_root,
        workspace_root=workspace_root,
        cli_opt=cli_storage_state,
    )
    mcp_env = {
        "QTEA_STORAGE_STATE_ARG": _storage_state.to_mcp_arg(storage_state_path),
        "QTEA_MCP_USER_DATA_DIR_ARG": (
            f"--user-data-dir={workdir / 'playwright-mcp'}"
        ),
    }

    try:
        timeout = timeout_s or int(
            os.environ.get("QTEA_LIVE_EXPLORE_TIMEOUT_S", "")
            or _DEFAULT_TIMEOUT_S
        )
    except ValueError:
        timeout = _DEFAULT_TIMEOUT_S

    log.info(
        "step07.live_explore_start",
        base_url=base_url,
        route_count=len(routes),
        authenticated=storage_state_path is not None,
    )
    try:
        res = await run_agent(
            agent,
            workdir=workdir,
            inputs={},
            user_prompt=_build_explore_prompt(base_url, routes),
            extra_paths=[
                package_resource_root() / "skills" / "playwright-explore-website",
            ],
            timeout_s=timeout,
            step=7,
            max_turns=int(os.environ.get("QTEA_LIVE_EXPLORE_MAX_TURNS", "40")),
            enable_mcp=True,
            mcp_env=mcp_env,
        )
    except Exception as e:  # best-effort; never break Step 7
        log.warning("step07.live_explore_agent_error", error=str(e))
        return None

    if not res.success or not (res.final_text or "").strip():
        log.warning("step07.live_explore_no_output", error=res.error)
        return None

    live_map = _parse_live_map(res.final_text)
    if live_map is None:
        log.warning("step07.live_explore_unparseable")
        return None

    try:
        (out_dir / "live-map.json").write_text(
            json.dumps(live_map, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except OSError as e:
        log.warning("step07.live_explore_write_failed", error=str(e))

    # A route behind login (auth_required) EXISTS — it is not "missing". Only
    # genuine 404/error routes count as missing so the architect isn't told to
    # skip real-but-gated pages on an unauthenticated exploration run.
    routes = [r for r in live_map.get("routes", []) if isinstance(r, dict)]
    missing = [
        r.get("path") for r in routes
        if r.get("exists") is False and not r.get("auth_required")
    ]
    auth_gated = [r.get("path") for r in routes if r.get("auth_required")]
    log.info(
        "step07.live_explore_done",
        routes_probed=len(routes),
        missing=missing,
        auth_gated=auth_gated,
    )
    return live_map


def _parse_live_map(text: str) -> dict[str, Any] | None:
    """Parse the agent's JSON response, tolerating stray prose/fences."""
    t = text.strip()
    # Strip markdown fences if present.
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", t).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...} span.
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("routes"), list):
        return None
    return obj


def render_live_map_for_prompt(live_map: dict[str, Any] | None) -> str:
    """Compact, architect-facing summary of the live map for inlining into the
    Step 7 prompt. Empty string when there's nothing to say."""
    if not live_map or not isinstance(live_map.get("routes"), list):
        return ""
    lines: list[str] = []
    for r in live_map["routes"]:
        if not isinstance(r, dict):
            continue
        path = r.get("path") or "?"
        if r.get("auth_required"):
            # Gated by login/SSO on this (unauthenticated) run — the page
            # EXISTS, it just couldn't be explored. Must NOT be treated as
            # missing, or the architect would drop a real page.
            lines.append(
                f"  - `{path}` — EXISTS but behind login/SSO; not explored on "
                f"this unauthenticated run. Plan it normally from the static "
                f"inventory — do NOT treat as missing."
            )
        elif r.get("exists") is False:
            dest = r.get("redirected_to")
            dest_s = f" (redirected to {dest})" if dest else ""
            lines.append(f"  - `{path}` — DOES NOT EXIST / error{dest_s}")
        else:
            roles = r.get("notable_roles") or []
            roles_s = "; ".join(str(x) for x in roles[:8])
            lines.append(f"  - `{path}` — exists. Observed: {roles_s or '(no detail)'}")
    if not lines:
        return ""
    return (
        "\n\nLIVE PAGE MAP (captured from the running SUT before planning — "
        "prefer this over inventory guesses when they disagree). Status "
        "legend: a route 'behind login/SSO' EXISTS (it was only gated on an "
        "unauthenticated exploration run) — plan against it normally. ONLY a "
        "route marked DOES NOT EXIST / error is truly absent: for those, do "
        "NOT plan locators against it — add an Open Question / "
        "[CLARIFICATION NEEDED] note in the plan `notes`:\n"
        + "\n".join(lines)
    )


__all__ = [
    "explore_strategy_routes",
    "render_live_map_for_prompt",
]
