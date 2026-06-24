"""Step 4: Test strategy generation via test-manager agent.

Reads plan.md + refined-spec.md, invokes test-manager via the direct-SDK
transport with the test-strategy template + edge-case checklist inlined
into the prompt. Parses output into test-strategy.json with extracted
test cases.

Outputs (artifacts/step04/):
  - test-strategy.md
  - test-strategy.json

Transport: ``qtea.llm.reasoning.call_reasoning_llm`` (direct SDK, no
HITL — Step 4 does not currently emit clarification questions).
"""

from __future__ import annotations

import json
import os
import re

from qtea.config import package_resource_root, step_timeout
from qtea.coverage_audit import (
    _format_violations_for_agent,
    audit_strategy,
    audit_traceability_matrix,
    build_traceability_matrix,
)
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.md_parser import (
    Section,
    extract_bullets,
    extract_coverage_notes,
    parse_markdown,
)
from qtea.schemas import is_valid
from qtea.steps.base import Step, StepContext, StepResult


def _coverage_audit_enabled() -> bool:
    return os.environ.get("QTEA_COVERAGE_AUDIT", "0") == "1"

log = get_logger(__name__)

_TC_ID_RE = re.compile(r"\bTC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_AC_ID_RE = re.compile(r"\bAC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_EC_ID_RE = re.compile(r"\bEC-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_NFR_ID_RE = re.compile(r"\bNFR-[A-Za-z0-9][A-Za-z0-9\-_]*\b")
_PRIORITY_RE = re.compile(r"\b(P[0-3])\b")
_AUTOMATION_TYPES = frozenset({
    "ui", "api", "integration", "unit", "performance",
    "accessibility", "contract", "visual", "manual",
})


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


def _extract_id_list(content: str, field: str, pattern: re.Pattern[str]) -> list[str]:
    """Extract ID tokens from either an inline `**Field:** ID-1, ID-2` line or a
    bulleted list `**Field:**\\n- ID-1\\n- ID-2`. Deduplicated, order-preserving."""
    inline = _extract_field(content, field) or ""
    bulleted = _extract_list_field(content, field)
    text = inline + "\n" + "\n".join(bulleted)
    ids: list[str] = []
    for m in pattern.finditer(text):
        v = m.group(0)
        if v not in ids:
            ids.append(v)
    return ids


def _normalize_automation_type(raw: str | None) -> str:
    if not raw:
        return "UNKNOWN"
    s = re.sub(r"[\[\]`*]", "", raw).strip().lower()
    if s in _AUTOMATION_TYPES:
        return s
    for t in _AUTOMATION_TYPES:
        if t in s:
            return t
    return "UNKNOWN"


# Section headers the test-manager agent uses to organise the markdown
# (Scope / Test Cases / Assumptions / etc.). Without this guard, the
# permissive `^(test\s*case|tc\b|scenario)` regex below matches the
# literal "Test Cases" header and emits a noise `TC-test-cases` entry
# with empty steps. Match against the normalised title (lowercase,
# trimmed). See run 20260611-184450 strategy artifact for the incident.
_RESERVED_SECTION_TITLES: frozenset[str] = frozenset({
    "test cases", "test case list", "scope", "out of scope",
    "assumptions", "preconditions", "test data", "appendix",
    "summary", "overview", "notes", "coverage notes",
})

# Minimum signal a TC body must carry. A heading whose body contains
# none of these structural markers is treated as a section organiser,
# not a test case — even when the title matches the permissive regex.
# Matches both `**Field**: value` (bold-close before colon) and
# `**Field:** value` (colon inside bold), with arbitrary whitespace.
_TC_BODY_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:\*\*)?\s*type\s*(?:\*\*)?\s*:\s*", re.I),
    re.compile(r"(?:\*\*)?\s*priority\s*(?:\*\*)?\s*:\s*", re.I),
    re.compile(r"(?:\*\*)?\s*steps\s*(?:\*\*)?\s*:\s*", re.I),
    re.compile(r"(?:\*\*)?\s*expected(?:\s*result)?\s*(?:\*\*)?\s*:\s*", re.I),
    re.compile(r"(?:\*\*)?\s*preconditions?\s*(?:\*\*)?\s*:\s*", re.I),
)


def _looks_like_test_case(section: Section) -> bool:
    title = section.title
    norm = re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()
    if norm in _RESERVED_SECTION_TITLES:
        return False
    if _TC_ID_RE.search(title):
        return True
    if not re.match(r"^(test\s*case|tc\b|scenario)", title, re.I):
        return False
    # Generic-titled candidates ("Test Case", "Scenario") must have a
    # body that looks like a TC. Section organisers fall through to
    # False here even when they slip past the reserved-name list.
    return any(p.search(section.content or "") for p in _TC_BODY_MARKERS)


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
    req_id = (
        _extract_field(raw, "req id")
        or _extract_field(raw, "req_id")
        or _extract_field(raw, "requirement id")
        or ""
    )
    derived_from = _extract_id_list(raw, "derived from", _TC_ID_RE) or [tc_id]
    automation_type_raw = (
        _extract_field(raw, "automation type")
        or _extract_field(raw, "automation_type")
    )
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
        "req_id": req_id,
        "ac_ids": _extract_id_list(raw, "acs", _AC_ID_RE)
                  or _extract_id_list(raw, "ac ids", _AC_ID_RE)
                  or _extract_id_list(raw, "ac_ids", _AC_ID_RE),
        "ec_ids": _extract_id_list(raw, "ecs", _EC_ID_RE)
                  or _extract_id_list(raw, "ec ids", _EC_ID_RE)
                  or _extract_id_list(raw, "ec_ids", _EC_ID_RE),
        "nfr_ids": _extract_id_list(raw, "nfrs", _NFR_ID_RE)
                   or _extract_id_list(raw, "nfr ids", _NFR_ID_RE)
                   or _extract_id_list(raw, "nfr_ids", _NFR_ID_RE),
        "derived_from": derived_from,
        "automation_type": _normalize_automation_type(automation_type_raw),
    }


def _project_strategy(md: str) -> dict:
    root = parse_markdown(md)
    title = root.children[0].title if root.children else "Test Strategy"
    cases: list[dict] = []
    seen_ids: set[str] = set()
    duplicates: list[str] = []
    for sec in root.walk():
        if _looks_like_test_case(sec):
            tc = _project_test_case(sec)
            if tc["id"] in seen_ids:
                duplicates.append(tc["id"])
            seen_ids.add(tc["id"])
            cases.append(tc)

    scope_sec = root.find("scope")
    scope = scope_sec.content if scope_sec else None

    return {
        "title": title,
        "scope": scope,
        "test_cases": cases,
        "coverage_notes": extract_coverage_notes(root),
        "_duplicate_tc_ids": duplicates,
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

        # Inline plan + refined-spec + reference template docs into the prompt.
        # Replaces the file-staging extras pattern from the Agent SDK era.
        # The schema file is NOT inlined — schema validation is local-only and
        # happens on the post-LLM projection, not on the LLM output itself.
        inputs: dict[str, str] = {"plan.md": plan_md.read_text(encoding="utf-8")}
        if refined_md.exists():
            inputs["refined-spec.md"] = refined_md.read_text(encoding="utf-8")

        docs_root = package_resource_root()
        for doc in (
            "templates/test-strategy-template.md",
            "templates/edge-case-checklist.md",
        ):
            p = docs_root / doc
            if p.exists():
                inputs[p.name] = p.read_text(encoding="utf-8")

        agents_root = package_resource_root() / "agents"
        agent = agents_root / "test-manager.agent.md"

        base_user_prompt = (
            "The plan (and refined spec, if present) are provided in the "
            "inputs section below, along with the test-strategy template "
            "and edge-case checklist. Follow your workflow and "
            "decision-making guidance, then consult "
            "`test-manager.prompt.md` for TC templates and decision trees. "
            "Produce a focused test strategy document. Every test case "
            "must have an id of the form `TC-<slug>` and a priority "
            "(`P0`-`P3`). Return only the test-strategy markdown body — "
            "no preamble, no code fences."
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

        result = await call_reasoning_llm(
            agent,
            workdir=wd,
            user_prompt=base_user_prompt,
            inputs=inputs,
            output_schema=None,  # markdown output; schema validates projection only
            timeout_s=self.timeout_s,
            step=4,
        )

        if not result.success or not result.final_text:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "test-strategy.md not produced",
            )

        md_dst = out_dir / "test-strategy.md"
        md_dst.write_text(result.final_text, encoding="utf-8")
        projection = _project_strategy(md_dst.read_text(encoding="utf-8"))

        dup_ids = projection.pop("_duplicate_tc_ids", [])
        if dup_ids:
            return StepResult(
                success=False, status="failed",
                outputs=[md_dst],
                error=(
                    f"duplicate TC IDs in strategy: {', '.join(dup_ids)}. "
                    f"The test-manager agent must produce unique IDs."
                ),
            )

        json_dst = out_dir / "test-strategy.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, err = is_valid(projection, "test-strategy")
        status = "completed" if ok else "warned"
        notes = f"test_cases={len(projection['test_cases'])}"
        outputs: list = [md_dst, json_dst]
        if not ok:
            notes += f"; schema_warning={err}"
            log.warning("step04.schema_invalid", error=err)

        # TC budget enforcement: ceiling is 1.5x the AC count (min 8).
        if ok:
            refined_json_path = ctx.workspace.step_dir(2) / "refined-spec.json"
            if refined_json_path.exists():
                try:
                    rspec = json.loads(
                        refined_json_path.read_text(encoding="utf-8"),
                    )
                    ac_count = len(
                        rspec.get("acceptance_criteria_structured")
                        or rspec.get("acceptance_criteria")
                        or [],
                    )
                    max_tcs = max(int(ac_count * 1.5) + 1, 8)
                    tc_count = len(projection["test_cases"])
                    if tc_count > max_tcs:
                        return StepResult(
                            success=False, status="failed",
                            outputs=outputs,
                            error=(
                                f"TC count {tc_count} exceeds budget "
                                f"{max_tcs} (1.5x {ac_count} ACs). "
                                f"Re-run step 4."
                            ),
                        )
                except (json.JSONDecodeError, OSError):
                    pass

        if ok and _coverage_audit_enabled():
            plan_json_path = ctx.workspace.step_dir(3) / "plan.json"
            refined_json_path = ctx.workspace.step_dir(2) / "refined-spec.json"
            plan_json = (
                json.loads(plan_json_path.read_text(encoding="utf-8"))
                if plan_json_path.exists() else {}
            )
            refined_spec = (
                json.loads(refined_json_path.read_text(encoding="utf-8"))
                if refined_json_path.exists() else {}
            )

            # 1. Audit the strategy against plan + refined-spec.
            raw_md = md_dst.read_text(encoding="utf-8")
            strategy_violations = audit_strategy(
                projection, plan_json, refined_spec, raw_md=raw_md,
            )

            # 2. Build the traceability matrix; persist + audit it.
            matrix_violations: list[str] = []
            matrix_path = out_dir / "traceability-matrix.json"
            try:
                matrix = build_traceability_matrix(
                    refined_spec, plan_json, projection,
                    run_id=ctx.workspace.run_id,
                )
                matrix_path.write_text(
                    json.dumps(matrix, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                outputs.append(matrix_path)
                ok_matrix, err_matrix = is_valid(matrix, "traceability-matrix")
                if not ok_matrix:
                    matrix_violations.append(
                        f"traceability-matrix schema validation failed: {err_matrix}"
                    )
                else:
                    matrix_violations.extend(audit_traceability_matrix(matrix))
            except Exception as e:
                log.warning("step04.matrix_build_failed", error=str(e))
                matrix_violations.append(
                    f"traceability matrix could not be built: {e}"
                )

            all_violations = strategy_violations + matrix_violations
            if all_violations:
                prior_log.write_text("\n".join(all_violations), encoding="utf-8")
                log.warning(
                    "step04.audit_violations",
                    strategy=len(strategy_violations),
                    matrix=len(matrix_violations),
                    first=all_violations[0] if all_violations else "",
                )
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=outputs,
                    error=_format_violations_for_agent(
                        "test-strategy", all_violations,
                    ),
                    notes=notes + f"; audit_violations={len(all_violations)}",
                )

        return StepResult(
            success=True,
            status=status,
            outputs=outputs,
            notes=notes,
        )
