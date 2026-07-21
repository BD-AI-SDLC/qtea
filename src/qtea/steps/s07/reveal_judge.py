"""Narrow LLM callout for Step 7 progressive-disclosure reveal decisions.

Invoked by :func:`qtea.steps.s07.live_driver.drive_live_exploration` when a
named target from ``test-design.md`` isn't visible on a page's initial paint.
The driver captured the page's current AOM elements; this judge picks ONE
affordance to click next (a button, tab, menu item, disclosure control) so the
target becomes visible.

Bounded, single-turn direct-SDK call — no MCP, no file tools. The judge's
output is a JSON object with a single ``click`` string: the visible label of
the affordance to click, or ``"__none__"`` to indicate no reasonable next click
exists (driver stops revealing on this route).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qtea.config import package_resource_root
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.steps.s07.live_driver import RevealContext

log = get_logger(__name__)


_AGENT_FILENAME = "live-explore-reveal-judge.agent.md"

# Max chars of snapshot + candidates fed to the judge. Kept small — this is a
# narrow decision, not an exploration pass.
_MAX_SNAPSHOT_CHARS = 5000
_MAX_CANDIDATES = 30

# Structured-output schema (used on the standard Anthropic API path; degraded
# gracefully to a schema re-check on Vertex).
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["click"],
    "properties": {
        "click": {
            "type": "string",
            "description": (
                "Visible label of the affordance to click next, or the "
                "sentinel '__none__' when no reasonable click exists."
            ),
        },
    },
}


def _render_candidates(candidates: list[dict[str, Any]]) -> str:
    """Compact list of candidate affordances for the judge prompt."""
    lines: list[str] = []
    for i, c in enumerate(candidates[:_MAX_CANDIDATES]):
        role = str(c.get("role") or "")
        name = str(c.get("name") or "")
        if not name:
            continue
        lines.append(f"  {i}. role={role}, name={name!r}")
    if len(candidates) > _MAX_CANDIDATES:
        lines.append(f"  ... (+{len(candidates) - _MAX_CANDIDATES} more)")
    return "\n".join(lines) or "  (none)"


def _parse_response(text: str) -> str | None:
    """Extract the ``click`` value from the judge's JSON response.

    Tolerant of fences / stray prose. Returns the click label, ``None`` for
    ``"__none__"`` (sentinel meaning: stop revealing), or ``None`` on parse
    failure (treated the same — driver stops for this route).
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", t).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    click = obj.get("click")
    if not isinstance(click, str):
        return None
    click = click.strip()
    if not click or click == "__none__":
        return None
    return click


def _build_prompt(ctx: RevealContext) -> str:
    return (
        f"You are picking the ONE affordance to click next so the deterministic "
        f"driver can reveal a named target that isn't visible on the current "
        f"page's initial paint.\n\n"
        f"ROUTE: {ctx.route_path}\n"
        f"URL: {ctx.route_url}\n\n"
        f"TARGET NAME: {ctx.target_name}\n"
        f"REACH_VIA HINT: {ctx.target_reach_via or '(none)'}\n\n"
        f"CURRENT PAGE ELEMENTS (AOM snapshot excerpt, ≤5KB):\n"
        f"{ctx.snapshot_excerpt[:_MAX_SNAPSHOT_CHARS]}\n\n"
        f"CANDIDATE AFFORDANCES (elements with verified locators — pick ONE by "
        f"visible name, or return \"__none__\"):\n"
        f"{_render_candidates(ctx.candidates)}\n\n"
        f"Respond with ONLY the JSON object described in your instructions."
    )


async def judge_reveal(
    ctx: RevealContext,
    *,
    workdir: Path,
    timeout_s: int | None = None,
) -> str | None:
    """Ask the reveal judge which affordance to click next. Returns the visible
    label to click, or ``None`` when the judge declined (or the callout failed).

    Best-effort: any failure returns ``None`` so the driver stops revealing
    for that route and moves on to the next plan entry. Never raises.
    """
    agent = package_resource_root() / "agents" / _AGENT_FILENAME
    if not agent.is_file():
        log.warning("step07.reveal_judge.agent_missing", path=str(agent))
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        res = await call_reasoning_llm(
            agent,
            workdir=workdir,
            user_prompt=_build_prompt(ctx),
            output_schema=_OUTPUT_SCHEMA,
            inputs={},
            step=7,
            timeout_s=timeout_s,
        )
    except Exception as e:
        log.info("step07.reveal_judge.error", error=str(e))
        return None
    if not res.success or not (res.final_text or "").strip():
        log.info("step07.reveal_judge.no_output", error=res.error)
        return None
    return _parse_response(res.final_text)
