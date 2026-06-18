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
import os
import re

from worca_t.config import package_resource_root, step_timeout
from worca_t.coverage_audit import _format_violations_for_agent, audit_refined_spec
from worca_t.llm.reasoning import call_reasoning_llm_with_hitl
from worca_t.logging_setup import get_logger
from worca_t.md_parser import (
    Section,
    extract_bullets,
    extract_coverage_notes,
    extract_tables,
    parse_markdown,
    section_to_dict,
    slugify,
)
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult


def _coverage_audit_enabled() -> bool:
    """The audit is opt-in via env var for one release cycle.
    Default off; flip on after soak per the roll-out plan."""
    return os.environ.get("WORCA_T_COVERAGE_AUDIT", "0") == "1"

log = get_logger(__name__)

_REQ_ID_RE = re.compile(r"\bREQ-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_AC_ID_RE = re.compile(r"\bAC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_EC_ID_RE = re.compile(r"\bEC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_NFR_ID_RE = re.compile(r"\bNFR-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_AUTO_TAG_RE = re.compile(
    r"\[(AUTOMATABLE|MANUAL\s*ONLY|NEEDS\s*INVESTIGATION)\]", re.I
)
_GWT_RE = re.compile(
    r"Given\s+(.+?)\s*[,;.]\s*When\s+(.+?)\s*[,;.]\s*Then\s+(.+)",
    re.S | re.I,
)
_THRESHOLD_RE = re.compile(
    r"\[hard\s+threshold\]"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|fps|%|MB|KB|GB|req/s|rps|qps)\b"
    r"|\bWCAG\s+(?:AA|AAA)\b"
    r"|\b(?:Chrome|Firefox|Safari|Edge|iOS|Android)\b\s*\d",
    re.I,
)
_PROMOTED_TO_AC_RE = re.compile(r"promoted\s+to\s+(AC-[A-Za-z0-9][A-Za-z0-9\-_]*)", re.I)


def _extract_req_id(text: str, fallback_title: str) -> str:
    m = _REQ_ID_RE.search(text)
    if m:
        return m.group(0)
    return f"REQ-{slugify(fallback_title)}"


def _normalize_automation(tag: str | None) -> str:
    if not tag:
        return "UNKNOWN"
    t = re.sub(r"\s+", "_", tag.strip().upper())
    if t in {"AUTOMATABLE", "MANUAL_ONLY", "NEEDS_INVESTIGATION"}:
        return t
    return "UNKNOWN"


def _extract_acceptance_criteria_structured(root: Section) -> list[dict]:
    section = root.find("acceptance criteria")
    if section is None:
        return []
    bullets: list[str] = list(extract_bullets(section.content))
    for child in section.children:
        bullets.extend(extract_bullets(child.content))
    out: list[dict] = []
    for bullet in bullets:
        text = bullet.strip()
        # Strip checkbox marker `[ ]` / `[x]` if present.
        text = re.sub(r"^\[\s*[xX ]?\s*\]\s*", "", text)
        ac_m = _AC_ID_RE.search(text)
        if not ac_m:
            continue
        ac_id = ac_m.group(0)
        # Strip the ID + a trailing colon from the body for cleaner field parsing.
        body = text.replace(ac_id, "", 1).strip().lstrip(":").strip()
        auto_m = _AUTO_TAG_RE.search(body)
        automation = _normalize_automation(auto_m.group(1) if auto_m else None)
        # Strip the automation tag from the body before pulling GWT so the
        # tag's trailing punctuation doesn't break the Given/When/Then split.
        body_for_gwt = _AUTO_TAG_RE.sub("", body).strip()
        gwt_m = _GWT_RE.search(body_for_gwt)
        given = when = then = None
        if gwt_m:
            given = gwt_m.group(1).strip()
            when = gwt_m.group(2).strip()
            then = gwt_m.group(3).strip().rstrip("`").strip()
        out.append({
            "id": ac_id,
            "text": body or text,
            "given": given,
            "when": when,
            "then": then,
            "priority": "UNKNOWN",
            "automation": automation,
            "user_flow": None,
            "requires_tc": True,
            "promoted_from_nfr": ac_id.upper().startswith("AC-NFR"),
        })
    return out


def _extract_edge_cases_structured(root: Section) -> list[dict]:
    section = root.find("edge case")
    if section is None:
        return []
    out: list[dict] = []
    seen_ids: set[str] = set()
    blocks: list[str] = [section.content or ""]
    for child in section.children:
        blocks.append(child.content or "")
    counter = 0
    for block in blocks:
        for table in extract_tables(block):
            if not table:
                continue
            header_lower = [c.lower() for c in table[0]]
            has_id_col = any(h == "id" or h.startswith("id ") for h in header_lower)
            has_ec_col = any("edge" in h for h in header_lower)
            if not (has_id_col or has_ec_col):
                continue
            idx = {col: header_lower.index(col) for col in header_lower}

            def col_for(*names: str, _idx: dict = idx) -> int | None:
                for n in names:
                    if n in _idx:
                        return _idx[n]
                for n in names:
                    for h, i in _idx.items():
                        if n in h:
                            return i
                return None

            id_i = col_for("id")
            ec_i = col_for("edge case", "edge")
            sev_i = col_for("severity")
            auto_i = col_for("automation", "automation tag")
            mit_i = col_for("mitigation", "notes")
            for row in table[1:]:
                if all(not c.strip() for c in row):
                    continue
                raw_id = row[id_i].strip() if id_i is not None and id_i < len(row) else ""
                ec_text_cell = row[ec_i].strip() if ec_i is not None and ec_i < len(row) else ""
                sev_cell = (
                    row[sev_i].strip().lower()
                    if sev_i is not None and sev_i < len(row) else ""
                )
                auto_cell = row[auto_i].strip() if auto_i is not None and auto_i < len(row) else ""
                mit_cell = row[mit_i].strip() if mit_i is not None and mit_i < len(row) else ""
                ec_match = _EC_ID_RE.search(raw_id) or _EC_ID_RE.search(ec_text_cell)
                counter += 1
                ec_id = ec_match.group(0) if ec_match else f"EC-{counter}"
                if ec_id in seen_ids:
                    continue
                seen_ids.add(ec_id)
                severity = (
                    sev_cell
                    if sev_cell in {"critical", "high", "medium", "low"}
                    else "UNKNOWN"
                )
                automation_tag = None
                tag_m = _AUTO_TAG_RE.search(auto_cell)
                if tag_m:
                    automation_tag = tag_m.group(1)
                out.append({
                    "id": ec_id,
                    "text": ec_text_cell or raw_id,
                    "severity": severity,
                    "automation": _normalize_automation(automation_tag),
                    "mitigation": mit_cell or None,
                })
    if not out:
        for block in blocks:
            for bullet in extract_bullets(block):
                counter += 1
                ec_match = _EC_ID_RE.search(bullet)
                ec_id = ec_match.group(0) if ec_match else f"EC-{counter}"
                if ec_id in seen_ids:
                    continue
                seen_ids.add(ec_id)
                auto_m = _AUTO_TAG_RE.search(bullet)
                out.append({
                    "id": ec_id,
                    "text": bullet,
                    "severity": "UNKNOWN",
                    "automation": _normalize_automation(auto_m.group(1) if auto_m else None),
                    "mitigation": None,
                })
    return out


_NFR_BULLET_RE = re.compile(
    r"\*\*\s*(?P<head>[^*]+?)\s*\*\*\s*:?\s*(?P<body>.*)",
)
_CATEGORY_PREFIX_MAP = {
    "performance": "performance",
    "perf": "performance",
    "security": "security",
    "sec": "security",
    "accessibility": "accessibility",
    "a11y": "accessibility",
    "compatibility": "compatibility",
    "compat": "compatibility",
    "browser": "compatibility",
}


def _extract_nfrs_structured(root: Section) -> list[dict]:
    section = root.find("non-functional") or root.find("nfr")
    if section is None:
        return []
    blocks: list[str] = [section.content or ""]
    for child in section.children:
        blocks.append(child.content or "")
    out: list[dict] = []
    seen_ids: set[str] = set()
    counters: dict[str, int] = {}
    for block in blocks:
        for bullet in extract_bullets(block):
            text = bullet.strip()
            m = _NFR_BULLET_RE.match(text)
            head = m.group("head") if m else ""
            body = (m.group("body") if m else text).strip()
            full = f"{head} {body}".strip() if head else text
            id_match = _NFR_ID_RE.search(head) or _NFR_ID_RE.search(text)
            head_lower = head.lower()
            category = "other"
            for prefix, cat in _CATEGORY_PREFIX_MAP.items():
                if prefix in head_lower:
                    category = cat
                    break
            if id_match:
                nfr_id = id_match.group(0)
            else:
                short = {
                    "performance": "PERF",
                    "security": "SEC",
                    "accessibility": "A11Y",
                    "compatibility": "COMPAT",
                    "other": "GEN",
                }[category]
                counters[short] = counters.get(short, 0) + 1
                nfr_id = f"NFR-{short}-{counters[short]}"
            if nfr_id in seen_ids:
                continue
            seen_ids.add(nfr_id)
            has_threshold = bool(_THRESHOLD_RE.search(full))
            promoted = _PROMOTED_TO_AC_RE.search(full)
            out.append({
                "id": nfr_id,
                "text": body or full,
                "category": category,
                "has_threshold": has_threshold,
                "promoted_to_ac": promoted.group(1) if promoted else None,
            })
    return out


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
        "acceptance_criteria_structured": _extract_acceptance_criteria_structured(root),
        "edge_cases_structured": _extract_edge_cases_structured(root),
        "nfrs_structured": _extract_nfrs_structured(root),
        "coverage_notes": extract_coverage_notes(root),
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

        base_user_prompt = (
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
        )
        # Coverage-audit retry feedback: read attempt N-1's violation log
        # (if any) and prepend so attempt N sees what was wrong. Read-then-
        # delete so a successful retry leaves a clean workspace.
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

        if ok and _coverage_audit_enabled():
            violations = audit_refined_spec(projection)
            if violations:
                prior_log.write_text("\n".join(violations), encoding="utf-8")
                log.warning(
                    "step02.audit_violations",
                    count=len(violations),
                    first=violations[0] if violations else "",
                )
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[md_dst, json_dst],
                    error=_format_violations_for_agent("refined-spec", violations),
                    notes=notes + f"; audit_violations={len(violations)}",
                )

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )
