"""Narrow LLM callout for Step 7 locator disambiguation.

Invoked by :func:`qtea.steps.s07.live_driver.drive_live_exploration` when the
DOM probe found 2+ elements sharing the same role + accessible name but could
not verify a single unique locator. The judge picks the candidate whose
locator should be treated as authoritative for the intent, or reports
``"__unresolvable__"`` (driver preserves the honest ambiguity gap).

Bounded, single-turn direct-SDK call — no MCP, no file tools.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from qtea.config import package_resource_root
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.steps.s07.live_driver import AmbiguityContext

log = get_logger(__name__)


_AGENT_FILENAME = "live-explore-ambiguity-judge.agent.md"

_MAX_CANDIDATES = 10

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pick_index"],
    "properties": {
        "pick_index": {
            "type": "integer",
            "description": (
                "Zero-based index into the CANDIDATES list to pick, or -1 to "
                "indicate '__unresolvable__' (driver keeps the ambiguity gap)."
            ),
        },
    },
}


def _render_candidates(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, c in enumerate(candidates[:_MAX_CANDIDATES]):
        role = str(c.get("role") or "")
        name = str(c.get("name") or "")
        # Include any partial locator + testid hints the probe DID emit.
        loc = c.get("locator")
        loc_str = (
            f", locator={loc}" if isinstance(loc, dict) else ""
        )
        test_id = c.get("test_id")
        tid_str = f", test_id={test_id!r}" if test_id else ""
        reason = c.get("ambiguity_reason") or ""
        reason_str = f", reason={reason!r}" if reason else ""
        lines.append(
            f"  {i}. role={role}, name={name!r}{loc_str}{tid_str}{reason_str}"
        )
    if len(candidates) > _MAX_CANDIDATES:
        lines.append(f"  ... (+{len(candidates) - _MAX_CANDIDATES} more)")
    return "\n".join(lines) or "  (none)"


def _parse_response(text: str) -> int | None:
    """Extract ``pick_index`` from the JSON response. Returns an int index or
    ``None`` when the judge picked ``-1`` (unresolvable) or the parse failed.
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
    idx = obj.get("pick_index")
    if not isinstance(idx, int):
        return None
    if idx < 0:
        return None
    return idx


def _build_prompt(ctx: AmbiguityContext) -> str:
    return (
        f"You are disambiguating between multiple candidates that share the same "
        f"role + accessible name on ONE route. The DOM probe could not verify a "
        f"unique locator for any of them. Pick the one that most likely IS the "
        f"tested intent, or return pick_index=-1 to preserve the ambiguity gap.\n\n"
        f"ROUTE: {ctx.route_path}\n"
        f"INTENT: {ctx.intent}\n\n"
        f"CANDIDATES:\n"
        f"{_render_candidates(ctx.candidates)}\n\n"
        f"Respond with ONLY the JSON object described in your instructions."
    )


async def judge_ambiguity(
    ctx: AmbiguityContext,
    *,
    workdir: Path,
    timeout_s: int | None = None,
) -> dict[str, Any] | None:
    """Ask the ambiguity judge to pick a candidate. Returns the chosen candidate
    dict (a slice of the input CANDIDATES list) or ``None`` when unresolvable /
    the callout failed.

    Best-effort: any failure returns ``None`` so the driver leaves the elements
    marked ``locator_ambiguous`` and moves on. Never raises.
    """
    agent = package_resource_root() / "agents" / _AGENT_FILENAME
    if not agent.is_file():
        log.warning("step07.ambiguity_judge.agent_missing", path=str(agent))
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
        log.info("step07.ambiguity_judge.error", error=str(e))
        return None
    if not res.success or not (res.final_text or "").strip():
        log.info("step07.ambiguity_judge.no_output", error=res.error)
        return None
    idx = _parse_response(res.final_text)
    if idx is None:
        return None
    if 0 <= idx < len(ctx.candidates):
        return ctx.candidates[idx]
    return None
