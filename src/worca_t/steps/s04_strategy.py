"""Step 4: Test strategy generation via test-manager agent.

Reads plan.md + refined-spec.md, invokes test-manager (with the
test-strategy template + edge-case checklist + schema staged), parses
output into test-strategy.json with extracted test cases.

Outputs (artifacts/step04/):
  - test-strategy.md
  - test-strategy.json
"""

from __future__ import annotations

import json
import re
import shutil

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.md_parser import Section, extract_bullets, parse_markdown
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)

_TC_ID_RE = re.compile(r"\bTC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "tc"


def _extract_field(content: str, field: str) -> str | None:
    """Extract `**Field**: value` or `Field: value` from a section content."""
    pat = re.compile(rf"(?:\*\*)?{re.escape(field)}(?:\*\*)?\s*[:=]\s*(.+?)(?:\n|$)", re.I)
    m = pat.search(content)
    return m.group(1).strip() if m else None


def _extract_list_field(content: str, field: str) -> list[str]:
    """Extract a bulleted-list field: looks for `**Field**:\\n- a\\n- b`."""
    pat = re.compile(
        rf"(?:\*\*)?{re.escape(field)}(?:\*\*)?\s*[:=]?\s*\n((?:\s*[-*\d.].+\n?)+)",
        re.I,
    )
    m = pat.search(content)
    if not m:
        return []
    return extract_bullets(m.group(1))


def _looks_like_test_case(section: Section) -> bool:
    title = section.title
    if _TC_ID_RE.search(title):
        return True
    return bool(re.match(r"^(test\s*case|tc\b|scenario)", title, re.I))


def _project_test_case(section: Section) -> dict:
    title = section.title
    tc_match = _TC_ID_RE.search(title)
    raw = section.content
    tc_id = tc_match.group(0) if tc_match else f"TC-{_slug(title)}"
    # Clean title of TC id prefix:
    clean_title = _TC_ID_RE.sub("", title).strip(" :-")
    pri_text = _extract_field(raw, "priority") or ""
    pri_m = _PRIORITY_RE.search(pri_text) or _PRIORITY_RE.search(title)
    priority = pri_m.group(1) if pri_m else "UNKNOWN"
    return {
        "id": tc_id,
        "title": clean_title or title,
        "priority": priority,
        "type": _extract_field(raw, "type"),
        "preconditions": _extract_list_field(raw, "preconditions"),
        "steps": _extract_list_field(raw, "steps"),
        "expected": _extract_field(raw, "expected") or _extract_field(raw, "expected result"),
        "tags": [t.strip() for t in (_extract_field(raw, "tags") or "").split(",") if t.strip()],
        "raw": raw.strip(),
    }


def _project_strategy(md: str) -> dict:
    root = parse_markdown(md)
    title = root.children[0].title if root.children else "Test Strategy"
    cases: list[dict] = []
    seen_ids: set[str] = set()
    for sec in root.walk():
        if _looks_like_test_case(sec):
            tc = _project_test_case(sec)
            # De-duplicate by id, appending -<n> suffix on collision.
            base_id = tc["id"]
            n = 2
            while tc["id"] in seen_ids:
                tc["id"] = f"{base_id}-{n}"
                n += 1
            seen_ids.add(tc["id"])
            cases.append(tc)

    scope_sec = root.find("scope")
    scope = scope_sec.content if scope_sec else None

    return {
        "title": title,
        "scope": scope,
        "test_cases": cases,
    }


class StrategyStep(Step):
    number = 4
    name = "strategy"
    timeout_s = step_timeout(4)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        plan_md = ctx.workspace.step_dir(3) / "plan.md"
        refined_md = ctx.workspace.step_dir(2) / "refined-spec.md"
        if not plan_md.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"missing {plan_md}; run step 3 first",
            )

        inputs = {"plan.md": plan_md}
        if refined_md.exists():
            inputs["refined-spec.md"] = refined_md

        agents_root = package_resource_root() / "agents"
        docs_root = package_resource_root()
        agent = agents_root / "test-manager.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras = []
        for doc in ("templates/test-strategy-template.md", "templates/edge-case-checklist.md"):
            p = docs_root / doc
            if p.exists():
                extras.append(p)
        schema = package_resource_root() / "schemas" / "test-strategy.schema.json"
        if schema.exists():
            extras.append(schema)

        result = await run_agent(
            agent,
            workdir=wd,
            inputs=inputs,
            user_prompt=(
                "Read `./plan.md` (and `./refined-spec.md` if present). Follow "
                "your authoritative workflow in `test-manager.prompt.md` and "
                "produce a focused test strategy document at "
                "`./test-strategy.md`. Every test case must have an id of the "
                "form `TC-<slug>` and a priority (`P0`-`P3`)."
            ),
            extra_paths=extras,
            timeout_s=self.timeout_s,
            step=4,
            max_turns=25,
            claude_md=claude_md if claude_md.exists() else None,
        )

        produced = wd / "test-strategy.md"
        if not result.success or not produced.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "test-strategy.md not produced",
            )

        md_dst = out_dir / "test-strategy.md"
        shutil.copy2(produced, md_dst)
        projection = _project_strategy(md_dst.read_text(encoding="utf-8"))
        json_dst = out_dir / "test-strategy.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, err = is_valid(projection, "test-strategy")
        status = "completed" if ok else "warned"
        notes = f"test_cases={len(projection['test_cases'])}"
        if not ok:
            notes += f"; schema_warning={err}"
            log.warning("step04.schema_invalid", error=err)

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )
