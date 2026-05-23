"""Step 3: Test plan generation via polyglot-test-planner.

Reads research.md + refined-spec.md, invokes the planner agent, parses its
output into a phase-structured plan.json (best-effort projection).

Outputs (artifacts/step03/):
  - plan.md
  - plan.json
"""

from __future__ import annotations

import json
import re
import shutil

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.md_parser import extract_bullets, extract_tables, parse_markdown
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)

_PHASE_RE = re.compile(r"^Phase\s+(\d+)\s*[:\-]\s*(.+?)$", re.IGNORECASE)
_FILE_HEADING_RE = re.compile(r"^\d+\.\s+(.+?)\s*$")


def _extract_commands(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("build", "test", "lint"):
        m = re.search(rf"\*\*{key}\*\*\s*[:=]\s*`?([^`\n]+)`?", md, re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip().strip("`")
    return out


def _extract_phase_summary_table(md: str) -> list[list[str]]:
    for tbl in extract_tables(md):
        if not tbl:
            continue
        header = [c.lower() for c in tbl[0]]
        if "phase" in header and ("files" in header or "focus" in header):
            return tbl
    return []


def _files_from_section(files_section) -> list[dict]:
    """Given the 'Files to Test' Section, build file records from its children."""
    if files_section is None:
        return []
    out: list[dict] = []
    for child in files_section.children:
        # Title example: "1. utils/login.ts"
        m = _FILE_HEADING_RE.match(child.title.strip())
        title = m.group(1).strip() if m else child.title.strip()
        block = child.content
        src_m = re.search(r"\*\*Source\*\*\s*:\s*`?([^`\n]+)`?", block, re.I)
        tf_m = re.search(r"\*\*Test\s*File\*\*\s*:\s*`?([^`\n]+)`?", block, re.I)
        tc_m = re.search(r"\*\*Test\s*Class\*\*\s*:\s*`?([^`\n]+)`?", block, re.I)
        methods: list[str] = []
        meth_section = re.search(
            r"\*\*Methods to Test\*\*\s*:?\s*\n(.+?)(?=\n\*\*|\Z)", block, re.S | re.I
        )
        if meth_section:
            for line in meth_section.group(1).splitlines():
                ml = re.match(r"\s*\d+\.\s+`?([^`\n-]+?)`?\s*(?:-.*)?$", line)
                if ml:
                    methods.append(ml.group(1).strip())
        out.append(
            {
                "title": title,
                "source": src_m.group(1).strip().strip("`") if src_m else None,
                "test_file": tf_m.group(1).strip().strip("`") if tf_m else None,
                "test_class": tc_m.group(1).strip().strip("`") if tc_m else None,
                "methods": methods,
                "raw": block.strip(),
            }
        )
    return out


def _project_plan(md: str) -> dict:
    root = parse_markdown(md)
    title = root.children[0].title if root.children else "Test Plan"
    overview_sec = root.find("overview")
    phases: list[dict] = []
    for sec in root.walk():
        m = _PHASE_RE.match(sec.title.strip())
        if not m:
            continue
        files_sec = next(
            (c for c in sec.children if c.title.lower().startswith("files")), None
        )
        success_sec = next(
            (c for c in sec.children if "success criteria" in c.title.lower()), None
        )
        overview_inner = next(
            (c for c in sec.children if c.title.lower() == "overview"), None
        )
        phases.append(
            {
                "number": int(m.group(1)),
                "title": m.group(2).strip(),
                "overview": overview_inner.content if overview_inner else "",
                "files": _files_from_section(files_sec),
                "success_criteria": extract_bullets(success_sec.content) if success_sec else [],
            }
        )
    return {
        "title": title,
        "overview": overview_sec.content if overview_sec else "",
        "commands": _extract_commands(md),
        "phase_summary": _extract_phase_summary_table(md),
        "phases": phases,
    }


class PlanStep(Step):
    number = 3
    name = "plan"
    timeout_s = step_timeout(3)

    def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        research = ctx.workspace.step_dir(6) / "research.md"
        refined = ctx.workspace.step_dir(2) / "refined-spec.md"
        inputs: dict = {}
        if research.exists():
            inputs["research.md"] = research
        if refined.exists():
            inputs["refined-spec.md"] = refined
        if not inputs:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="step 3 requires research.md (step 6) and/or refined-spec.md (step 2)",
            )

        agents_root = package_resource_root() / "agents"
        agent = agents_root / "polyglot-test-planner.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        result = run_agent(
            agent,
            workdir=wd,
            inputs=inputs,
            user_prompt=(
                "Read the staged `./research.md` (and `./refined-spec.md` if "
                "present) and produce a phased test implementation plan at "
                "`./plan.md` following the structure in your agent prompt. "
                "Include Build/Test/Lint commands explicitly."
            ),
            timeout_s=self.timeout_s,
            step=3,
            claude_md=claude_md if claude_md.exists() else None,
        )

        produced = wd / "plan.md"
        if not result.success or not produced.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "plan.md not produced",
            )

        md_dst = out_dir / "plan.md"
        shutil.copy2(produced, md_dst)
        projection = _project_plan(md_dst.read_text(encoding="utf-8"))
        json_dst = out_dir / "plan.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, err = is_valid(projection, "plan")
        status = "completed" if ok else "warned"
        notes = f"phases={len(projection['phases'])}"
        if not ok:
            notes += f"; schema_warning={err}"
            log.warning("step03.schema_invalid", error=err)

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )
