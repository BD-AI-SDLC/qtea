"""Step 2: Spec refinement.

Invokes the `refine-spec` agent on the step01 spec.md via the direct-SDK
HITL transport. Then deterministically parses the refined markdown into a
JSON projection used downstream.

Outputs (artifacts/step02/):
  - refined-spec.md
  - refined-spec.json   (parsed sections + extracted REQ id + AC bullets)

Transport: ``worca_t.llm.reasoning.call_reasoning_llm_with_hitl`` (direct
Anthropic SDK, no subprocess, no MCP). Multi-turn HITL conversation
replaces the previous file-staging re-invoke pattern.
"""

from __future__ import annotations

import json
import re

from worca_t.config import package_resource_root, step_timeout
from worca_t.llm.reasoning import call_reasoning_llm_with_hitl
from worca_t.logging_setup import get_logger
from worca_t.md_parser import extract_bullets, parse_markdown, section_to_dict, slugify
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)

_REQ_ID_RE = re.compile(r"\bREQ-[A-Za-z0-9][A-Za-z0-9\-_]*\b")


def _extract_req_id(text: str, fallback_title: str) -> str:
    m = _REQ_ID_RE.search(text)
    if m:
        return m.group(0)
    return f"REQ-{slugify(fallback_title)}"


def _project_to_json(md_text: str) -> dict:
    root = parse_markdown(md_text)
    title = root.children[0].title if root.children else "untitled"
    req_id = _extract_req_id(md_text, title)

    def find(needle: str) -> dict | None:
        s = root.find(needle)
        return section_to_dict(s) if s else None

    ac_section = root.find("acceptance criteria")
    acceptance_criteria = extract_bullets(ac_section.content) if ac_section else []
    for child in (ac_section.children if ac_section else []):
        acceptance_criteria.extend(extract_bullets(child.content))

    md_upper = md_text.upper()
    is_ready = "DEFINITION OF READY" in md_upper and "READY" in md_upper
    return {
        "requirement_id": req_id,
        "title": title,
        "sections": [section_to_dict(c) for c in root.children],
        "acceptance_criteria": acceptance_criteria,
        "user_flows": find("user flow"),
        "test_boundaries": find("test boundar"),
        "test_data": find("test data"),
        "environment": find("environment"),
        "technical_considerations": find("technical"),
        "edge_cases": find("edge case"),
        "nfrs": find("non-functional") or find("nfr"),
        "definition_of_ready": find("definition of ready"),
        "readiness": "READY" if is_ready else "UNKNOWN",
    }


class RefineStep(Step):
    number = 2
    name = "refine"
    timeout_s = step_timeout(2)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        spec_in = ctx.workspace.step_dir(1) / "spec.md"
        if not spec_in.exists():
            return StepResult(
                success=False, status="failed", outputs=[], error=f"missing {spec_in}"
            )

        agents_root = package_resource_root() / "agents"
        agent = agents_root / "refine-spec.agent.md"

        # Spec contents are inlined into the user prompt (replaces the old
        # file-staging into the agent workdir).
        spec_text = spec_in.read_text(encoding="utf-8")

        result = await call_reasoning_llm_with_hitl(
            agent,
            ctx=ctx,
            workdir=wd,
            user_prompt=(
                "The current `spec.md` content is provided in the inputs "
                "section below. Produce a refined specification following "
                "the structure in your agent instructions. First run the "
                "Pre-clean Pass: if the spec is a noisy Jira/Confluence "
                "export, mentally strip it down to just the Description and "
                "Acceptance Criteria before refining. If it's already a "
                "clean narrative spec, skip the pre-clean. Ensure a "
                "`Requirement ID: REQ-<slug>` line is present near the top "
                "of the refined spec. Return only the refined-spec markdown "
                "body — no preamble, no code fences."
            ),
            inputs={"spec.md": spec_text},
            output_filename="refined-spec.md",
            output_schema=None,  # markdown output; schema validates projection only
            timeout_s=self.timeout_s,
            step=2,
            agent_label="refine-spec",
        )

        if not result.success or not result.final_text:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "refined-spec.md not produced",
            )

        md_dst = out_dir / "refined-spec.md"
        md_dst.write_text(result.final_text, encoding="utf-8")

        try:
            projection = _project_to_json(md_dst.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("step02.parse_failed", error=str(e))
            return StepResult(success=False, status="failed", outputs=[md_dst], error=f"parse: {e}")

        json_dst = out_dir / "refined-spec.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, err = is_valid(projection, "refined-spec")
        status = "completed" if ok else "warned"
        notes = f"requirement_id={projection['requirement_id']}"
        if not ok:
            notes += f"; schema_warning={err}"
            log.warning("step02.schema_invalid", error=err)

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )
