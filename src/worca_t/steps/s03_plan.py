"""Step 3: Test plan generation via polyglot-test-planner.

Reads refined-spec.md, invokes the planner agent via the direct-SDK HITL
transport, parses its output into a phase-structured plan.json (best-effort
projection).

Outputs (artifacts/step03/):
  - plan.md
  - plan.json

Transport: ``worca_t.llm.reasoning.call_reasoning_llm_with_hitl`` (direct
Anthropic SDK, no subprocess, no MCP).
"""

from __future__ import annotations

import json
import os
import re

from worca_t.config import package_resource_root, step_timeout
from worca_t.coverage_audit import _format_violations_for_agent, audit_plan
from worca_t.llm.reasoning import call_reasoning_llm_with_hitl
from worca_t.logging_setup import get_logger
from worca_t.md_parser import (
    Section,
    extract_bullets,
    extract_coverage_notes,
    extract_tables,
    parse_markdown,
)
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.steps.s02_refine import _extract_acceptance_criteria_structured


def _coverage_audit_enabled() -> bool:
    return os.environ.get("WORCA_T_COVERAGE_AUDIT", "0") == "1"

log = get_logger(__name__)

_PHASE_RE = re.compile(r"^Phase\s+(\d+)\s*[:\-]\s*(.+?)$", re.IGNORECASE)
_FILE_HEADING_RE = re.compile(r"^\d+\.\s+(.+?)\s*$")
_TC_ID_RE = re.compile(r"\bTC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_AC_ID_RE = re.compile(r"\bAC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_EC_ID_RE = re.compile(r"\bEC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_NFR_ID_RE = re.compile(r"\bNFR-[A-Za-z0-9][A-Za-z0-9\-_]*\b")

_PRIORITY_WORD_MAP = {
    "critical": "P0",
    "p0": "P0",
    "high": "P1",
    "p1": "P1",
    "medium": "P2",
    "med": "P2",
    "p2": "P2",
    "low": "P3",
    "p3": "P3",
}
_AUTOMATION_WORD_MAP = {
    "automation": "automation",
    "automated": "automation",
    "automatable": "automation",
    "manual": "manual",
    "manual_only": "manual",
    "manual only": "manual",
    "needs_investigation": "needs_investigation",
    "needs investigation": "needs_investigation",
    "investigation": "needs_investigation",
}


def _normalize_priority(cell: str) -> str:
    s = cell.strip().lower()
    if s in _PRIORITY_WORD_MAP:
        return _PRIORITY_WORD_MAP[s]
    for word, mapped in _PRIORITY_WORD_MAP.items():
        if word in s:
            return mapped
    return "UNKNOWN"


def _normalize_automation_cell(cell: str) -> str:
    s = re.sub(r"[\[\]`*]", "", cell).strip().lower()
    if s in _AUTOMATION_WORD_MAP:
        return _AUTOMATION_WORD_MAP[s]
    for word, mapped in _AUTOMATION_WORD_MAP.items():
        if word in s:
            return mapped
    return "UNKNOWN"


def _split_id_cell(cell: str, pattern: re.Pattern[str]) -> list[str]:
    ids: list[str] = []
    for m in pattern.finditer(cell):
        v = m.group(0)
        if v not in ids:
            ids.append(v)
    return ids


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


def _extract_tc_roster_from_phase(phase_section: Section, phase_number: int) -> list[dict]:
    roster_sec = next(
        (c for c in phase_section.children if "tc roster" in c.title.lower()
         or "test case roster" in c.title.lower()),
        None,
    )
    if roster_sec is None:
        return []
    out: list[dict] = []
    for table in extract_tables(roster_sec.content):
        if not table or len(table) < 2:
            continue
        header_lower = [c.strip().lower() for c in table[0]]

        def col_idx(*names: str) -> int | None:
            for n in names:
                for i, h in enumerate(header_lower):
                    if h == n:
                        return i
            for n in names:
                for i, h in enumerate(header_lower):
                    if n in h:
                        return i
            return None

        id_i = col_idx("tc id", "id", "tc")
        title_i = col_idx("title", "name")
        type_i = col_idx("type", "kind")
        pri_i = col_idx("priority", "pri")
        req_i = col_idx("req id", "requirement id", "req")
        ac_i = col_idx("acs", "ac ids", "ac")
        ec_i = col_idx("ecs", "ec ids", "ec")
        nfr_i = col_idx("nfrs", "nfr ids", "nfr")
        auto_i = col_idx("automation", "auto")
        if id_i is None:
            continue
        for row in table[1:]:
            if all(not c.strip() for c in row):
                continue
            cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""
            tc_match = _TC_ID_RE.search(cell(id_i))
            if not tc_match:
                continue
            tc = {
                "id": tc_match.group(0),
                "title": cell(title_i) or tc_match.group(0),
                "type": cell(type_i) or None,
                "priority": _normalize_priority(cell(pri_i)),
                "req_id": cell(req_i),
                "ac_ids": _split_id_cell(cell(ac_i), _AC_ID_RE),
                "ec_ids": _split_id_cell(cell(ec_i), _EC_ID_RE),
                "nfr_ids": _split_id_cell(cell(nfr_i), _NFR_ID_RE),
                "automation": _normalize_automation_cell(cell(auto_i)),
                "phase": phase_number,
                "parametrized_over": [],
            }
            out.append(tc)
    return out


def _project_plan(md: str) -> dict:
    root = parse_markdown(md)
    title = root.children[0].title if root.children else "Test Plan"
    overview_sec = root.find("overview")
    phases: list[dict] = []
    all_test_cases: list[dict] = []
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
        phase_number = int(m.group(1))
        phases.append(
            {
                "number": phase_number,
                "title": m.group(2).strip(),
                "overview": overview_inner.content if overview_inner else "",
                "files": _files_from_section(files_sec),
                "success_criteria": extract_bullets(success_sec.content) if success_sec else [],
            }
        )
        all_test_cases.extend(_extract_tc_roster_from_phase(sec, phase_number))
    # De-duplicate TC IDs across phases (preserve first occurrence).
    seen: set[str] = set()
    deduped: list[dict] = []
    for tc in all_test_cases:
        if tc["id"] in seen:
            continue
        seen.add(tc["id"])
        deduped.append(tc)
    return {
        "title": title,
        "overview": overview_sec.content if overview_sec else "",
        "commands": _extract_commands(md),
        "phase_summary": _extract_phase_summary_table(md),
        "phases": phases,
        "test_cases": deduped,
        "acceptance_criteria_structured": _extract_acceptance_criteria_structured(root),
        "coverage_notes": extract_coverage_notes(root),
    }


class PlanStep(Step):
    number = 3
    name = "plan"
    timeout_s = step_timeout(3)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        refined = ctx.workspace.step_dir(2) / "refined-spec.md"
        if not refined.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="step 3 requires refined-spec.md (step 2)",
            )

        agents_root = package_resource_root() / "agents"
        agent = agents_root / "polyglot-test-planner.agent.md"

        # Inline refined-spec into the user prompt (replaces file staging).
        refined_text = refined.read_text(encoding="utf-8")

        base_user_prompt = (
            "The refined spec is provided in the inputs section below. "
            "Produce a phased test implementation plan following the "
            "structure in your agent prompt. Return only the plan "
            "markdown body — no preamble, no code fences."
        )
        prior_log = out_dir / "audit-violations.log"
        prior_violations = (
            prior_log.read_text(encoding="utf-8") if prior_log.exists() else ""
        )
        prior_log.unlink(missing_ok=True)
        if prior_violations:
            base_user_prompt = (
                "Your previous attempt FAILED the coverage audit. Fix every "
                "item below before resubmitting:\n\n"
                f"{prior_violations}\n\n---\n\n" + base_user_prompt
            )

        result = await call_reasoning_llm_with_hitl(
            agent,
            ctx=ctx,
            workdir=wd,
            user_prompt=base_user_prompt,
            inputs={"refined-spec.md": refined_text},
            output_filename="plan.md",
            output_schema=None,  # markdown output; schema validates projection only
            timeout_s=self.timeout_s,
            step=3,
            agent_label="polyglot-test-planner",
        )

        if not result.success or not result.final_text:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "plan.md not produced",
            )

        md_dst = out_dir / "plan.md"
        md_dst.write_text(result.final_text, encoding="utf-8")
        projection = _project_plan(md_dst.read_text(encoding="utf-8"))
        json_dst = out_dir / "plan.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, err = is_valid(projection, "plan")
        notes = f"phases={len(projection['phases'])}"
        if not ok:
            # CLAUDE.md classifies Step 3 failure as "abort": a malformed
            # plan (no extractable phases or missing required fields)
            # silently propagates to Step 4's test-manager agent, which
            # then plans against garbage. Fail-fast so the retry path
            # (attempt 2 with debug agent co-running) gets a chance.
            log.warning("step03.schema_invalid", error=err)
            return StepResult(
                success=False,
                status="failed",
                outputs=[md_dst, json_dst],
                error=f"plan.json schema validation failed: {err}",
                notes=notes + f"; schema_error={err}",
            )

        if _coverage_audit_enabled():
            refined_json_path = ctx.workspace.step_dir(2) / "refined-spec.json"
            if refined_json_path.exists():
                refined_spec = json.loads(
                    refined_json_path.read_text(encoding="utf-8")
                )
                violations = audit_plan(projection, refined_spec)
                if violations:
                    prior_log.write_text("\n".join(violations), encoding="utf-8")
                    log.warning(
                        "step03.audit_violations",
                        count=len(violations),
                        first=violations[0] if violations else "",
                    )
                    return StepResult(
                        success=False,
                        status="failed",
                        outputs=[md_dst, json_dst],
                        error=_format_violations_for_agent("plan", violations),
                        notes=notes + f"; audit_violations={len(violations)}",
                    )
            else:
                log.warning(
                    "step03.audit_skipped_no_refined_spec",
                    reason="refined-spec.json not found in step02",
                )

        return StepResult(
            success=True,
            status="completed",
            outputs=[md_dst, json_dst],
            notes=notes,
        )
