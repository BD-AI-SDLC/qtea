"""Step 2: Spec refinement.

Invokes the `refine-spec` agent on the step01 spec.md via the direct-SDK
HITL transport. Then deterministically parses the refined markdown into a
JSON projection used downstream.

Outputs (artifacts/step02/):
  - refined-spec.md
  - refined-spec.json   (parsed sections + extracted REQ id + AC bullets)

Transport: ``qtea.llm.reasoning.call_reasoning_llm_with_hitl`` (direct
Anthropic SDK, no subprocess, no MCP). Multi-turn HITL conversation
replaces the previous file-staging re-invoke pattern.
"""

from __future__ import annotations

import json
import os
import re

from qtea.config import package_resource_root, step_timeout
from qtea.coverage_audit import _format_violations_for_agent, audit_refined_spec
from qtea.llm.reasoning import call_reasoning_llm, call_reasoning_llm_with_hitl
from qtea.logging_setup import get_logger
from qtea.md_parser import (
    Section,
    extract_bullets,
    extract_coverage_notes,
    extract_tables,
    parse_markdown,
    section_to_dict,
    slugify,
)
from qtea.schemas import is_valid
from qtea.steps.base import Step, StepContext, StepResult


def _coverage_audit_enabled() -> bool:
    """Cross-artifact traceability/coverage audit. Finding 21: default ON.
    It is the only backstop that a requirement actually maps to a test —
    leaving it off silently shipped dropped ACs / omitted P0 flows. Set
    QTEA_COVERAGE_AUDIT=0 to disable for a deliberately partial run."""
    return os.environ.get("QTEA_COVERAGE_AUDIT", "1") == "1"


def _format_fixer_enabled() -> bool:
    """The on-audit-failure format-fixer rescue (a cheap Haiku pass that
    reshapes the markdown to satisfy the audit). Default ON. Set
    QTEA_NO_FORMAT_FIXER=1 to disable — used when a run (or a test) needs
    to exercise the raw refine → audit → retry path without the rescue
    layer intercepting the first failure."""
    return os.environ.get("QTEA_NO_FORMAT_FIXER", "0") != "1"

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
_GWT_LINE_RE = re.compile(
    r"^[-*+]?\s*\*\*\s*(Given|When|Then)\s*:?\s*\*\*\s*:?\s*(.+)$", re.I
)
_TOP_BULLET_RE = re.compile(r"^([-*+])\s+")
_THRESHOLD_RE = re.compile(
    r"\[hard\s+threshold\]"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|fps|%|MB|KB|GB|req/s|rps|qps)\b"
    r"|\bWCAG\s+(?:AA|AAA)\b"
    r"|\b(?:Chrome|Firefox|Safari|Edge|iOS|Android)\b\s*\d",
    re.I,
)
_PROMOTED_TO_AC_RE = re.compile(r"promoted\s+to\s+(AC-[A-Za-z0-9][A-Za-z0-9\-_]*)", re.I)


# The coverage-audit's format contract, expressed as producer-facing rules.
# Given to (a) the refine-spec agent on retry, and (b) the format-fixer
# rescue agent, so both know what shape the parser expects — not just
# "which symptom the parser saw" (the failure mode that previously caused
# non-convergent retries even after semantically correct output).
_AUDIT_PARSER_CONTRACT = (
    "Parser contract — produce markdown that satisfies these rules:\n\n"
    "ACCEPTANCE CRITERIA. Each AC is ONE top-level bullet that begins "
    "with a bold AC id:\n"
    "    - [ ] **AC-N**: `[TAG]`\n"
    "  where N is a number or DOMAIN-N (e.g. `**AC-COMPAT-1**`) and "
    "TAG is one of `[AUTOMATABLE]` / `[MANUAL ONLY]` / `[NEEDS "
    "INVESTIGATION]`. The colon may sit inside or outside the bold "
    "(`**AC-1:**` and `**AC-1**:` both parse) — just keep the id itself "
    "intact.\n"
    "  Given/When/Then may be written in EITHER form — pick whichever is "
    "natural, both parse identically:\n"
    "    • Inline on one line:  `Given <pre>, When <act>, Then <exp>`\n"
    "    • Multi-line: each of `**Given**` / `**When**` / `**Then**` on "
    "its own indented continuation line under the AC header (blank lines "
    "between the three clauses are optional).\n\n"
    "COVERAGE (Alternative Flows, In Scope). Every bullet — INCLUDING "
    "out-of-scope alternative flows — must be traceable via ONE of:\n"
    "  (a) an inline id reference, in any phrasing: `(AC-10)`, "
    "`(see EC-1)`, `(covered by NFR-PERF-1)`;\n"
    "  (b) a `[requires TC: AC-N, EC-M, NFR-K]` marker citing any "
    "mix of ids you defined in this spec;\n"
    "  (c) the bare `[requires TC]` escape hatch (genuinely deferred);\n"
    "  (d) an entry in `## Coverage Notes` naming the item id.\n"
    "  An alt-flow that is out of scope still needs a marker — cite the "
    "EC that captures it (`(see EC-N)`) or use the bare `[requires TC]` "
    "hatch; do NOT leave it bare.\n\n"
    "NFR. Every NFR with `[hard threshold]` MUST be promoted to an "
    "AC: add `- [ ] **AC-NFR-...**: ...` under Acceptance Criteria "
    "AND set that NFR's `promoted_to_ac` field to the new AC id.\n\n"
    "EC. Every edge case needs a `severity: critical|high|medium|low` "
    "line and an automation tag (same tag set as ACs)."
)


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


def _extract_ac_blocks(content: str) -> list[str]:
    """Group each top-level AC bullet with its indented continuation/
    sub-bullet lines (e.g. nested `**Given**`/`**When**`/`**Then**`) into
    one block, so the multi-line bold GWT format parses as a single
    acceptance criterion. Mirrors `extract_bullets`'s marker-stripping for
    the header line."""
    blocks: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        if not line[:1].isspace() and _TOP_BULLET_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [_TOP_BULLET_RE.sub("", line, count=1).strip()]
        elif current:
            current.append(line.strip())
    if current:
        blocks.append("\n".join(current))
    return blocks


def _extract_acceptance_criteria_structured(root: Section) -> list[dict]:
    section = root.find("acceptance criteria")
    if section is None:
        return []
    blocks: list[str] = list(_extract_ac_blocks(section.content))
    for child in section.children:
        blocks.extend(_extract_ac_blocks(child.content))
    out: list[dict] = []
    for block in blocks:
        lines = block.splitlines()
        text = lines[0].strip()
        # Strip checkbox marker `[ ]` / `[x]` if present.
        text = re.sub(r"^\[\s*[xX ]?\s*\]\s*", "", text)
        ac_m = _AC_ID_RE.search(text)
        if not ac_m:
            continue
        ac_id = ac_m.group(0)
        # Strip the ID, any bold markers wrapping it (`**AC-1:**`), and a
        # trailing colon from the body for cleaner field parsing.
        body = text.replace(ac_id, "", 1).replace("**", "").strip().lstrip(":").strip()
        auto_m = _AUTO_TAG_RE.search(block)
        automation = _normalize_automation(auto_m.group(1) if auto_m else None)

        # Canonical format: Given/When/Then each bolded on their own nested
        # bullet line beneath the AC header.
        given = when = then = None
        for line in lines[1:]:
            gwt_line_m = _GWT_LINE_RE.match(line)
            if not gwt_line_m:
                continue
            kw = gwt_line_m.group(1).lower()
            val = _AUTO_TAG_RE.sub("", gwt_line_m.group(2)).strip()
            if kw == "given":
                given = val
            elif kw == "when":
                when = val
            elif kw == "then":
                then = val.rstrip("`").strip()

        if given is None and when is None and then is None:
            # Legacy single-line format: `Given X, When Y, Then Z` inline.
            # Strip the automation tag first so its trailing punctuation
            # doesn't break the Given/When/Then split.
            body_for_gwt = _AUTO_TAG_RE.sub("", body).strip()
            gwt_m = _GWT_RE.search(body_for_gwt)
            if gwt_m:
                given = gwt_m.group(1).strip()
                when = gwt_m.group(2).strip()
                then = gwt_m.group(3).strip().rstrip("`").strip()

        # `body` may be just the automation tag (new nested format, where
        # the header carries no inline description) — fall back to a
        # synthesized GWT sentence so `text` stays meaningful for
        # coverage_audit's fuzzy AC<->flow-step matching.
        body_wo_tag = _AUTO_TAG_RE.sub("", body).replace("`", "").strip()
        if body_wo_tag:
            display_text = body_wo_tag
        elif given or when or then:
            display_text = f"Given {given}, When {when}, Then {then}".strip()
        else:
            display_text = text

        out.append({
            "id": ac_id,
            "text": display_text,
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
    # Group each top-level AC bullet with its nested Given/When/Then sub-
    # bullets into ONE legacy entry (via the same grouper the structured
    # extractor uses). Emitting the first line only keeps the flat list
    # aligned with `coverage_audit.audit_refined_spec`'s per-bullet
    # AC-ID check, which expects each legacy bullet to carry an AC-N
    # token in its header — not to be flooded with G/W/T detail lines.
    def _ac_headers(content: str) -> list[str]:
        return [b.splitlines()[0].strip() for b in _extract_ac_blocks(content)]
    acceptance_criteria = _ac_headers(ac_section.content) if ac_section else []
    for child in (ac_section.children if ac_section else []):
        acceptance_criteria.extend(_ac_headers(child.content))

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


async def _run_format_fixer(
    *,
    ctx: StepContext,
    md_text: str,
    violations: list[str],
    timeout_s: int,
) -> str | None:
    """Invoke the refine-format-fixer sub-agent to reshape ``md_text`` so
    the Step 2 coverage audit passes without changing semantics.

    Returns the corrected markdown on success, or ``None`` if the agent
    call failed for any reason (LLM error, empty output). The caller is
    responsible for re-auditing the returned markdown and deciding
    whether to adopt it; this helper does not gate on that.
    """
    agent = package_resource_root() / "agents" / "refine-format-fixer.agent.md"
    if not agent.exists():
        log.warning("step02.format_fixer_missing", agent=str(agent))
        return None
    workdir = ctx.workspace.step_dir(2) / "format-fixer"
    workdir.mkdir(parents=True, exist_ok=True)

    user_prompt = (
        "The `refined-spec.md` below FAILED the Step 2 coverage audit. "
        "Reshape ONLY the format so every listed violation is resolved. "
        "Preserve every acceptance criterion, edge case, NFR, boundary, "
        "coverage-notes entry, and test-data note verbatim. Return the "
        "complete corrected markdown — no preamble, no code fences.\n\n"
        f"AUDIT VIOLATIONS ({len(violations)}):\n- "
        + "\n- ".join(violations)
        + "\n\n"
        + _AUDIT_PARSER_CONTRACT
    )
    try:
        result = await call_reasoning_llm(
            agent,
            workdir=workdir,
            user_prompt=user_prompt,
            inputs={"refined-spec.md": md_text},
            output_schema=None,
            timeout_s=timeout_s,
            step=2,
        )
    except Exception as e:
        log.warning("step02.format_fixer_call_failed", error=str(e))
        return None
    if not result.success or not result.final_text:
        log.warning(
            "step02.format_fixer_no_output",
            error=result.error or "empty",
        )
        return None
    return result.final_text


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

        # Optional operator context: a trusted, free-text note the operator
        # supplied at run start (CLI --context / UI screen). Inline it as a
        # distinct input so the agent treats it as guidance, not as the spec
        # itself. It AUGMENTS the spec — it must not silently override the
        # acceptance criteria; genuine conflicts get a [CLARIFICATION NEEDED].
        inputs = {"spec.md": spec_text}
        if ctx.operator_context:
            inputs["user-context.md"] = ctx.operator_context
            base_user_prompt += (
                "\n\nAn operator has supplied additional context in "
                "`./user-context.md`. Treat it as TRUSTED guidance to "
                "disambiguate and sharpen the spec — apply environmental "
                "facts, scope emphasis, and domain clarifications it "
                "provides. It AUGMENTS the requirement; it does NOT replace "
                "the acceptance criteria in `spec.md`. If it genuinely "
                "conflicts with a stated requirement, do NOT silently "
                "override — raise a `[CLARIFICATION NEEDED]` describing the "
                "conflict."
            )

        # Optional operator context images: trusted supplementary visuals
        # (mockups, wireframes, screenshots of the feature or of an error) the
        # operator attached at run start. Encode as Anthropic image blocks so
        # the vision-capable refine-spec agent can reason over them alongside
        # the spec and text context.
        context_image_blocks: list[dict] = []
        if ctx.operator_context_images:
            from qtea.context_images import ContextImageError, encode_image_block

            attached: list[str] = []
            for img in ctx.operator_context_images:
                try:
                    context_image_blocks.append(encode_image_block(img))
                    attached.append(img.name)
                except ContextImageError as e:
                    log.warning("refine.context_image_skipped", reason=str(e))
            if context_image_blocks:
                names = ", ".join(attached)
                base_user_prompt += (
                    "\n\nThe operator also attached "
                    f"{len(context_image_blocks)} image(s) ({names}) as TRUSTED "
                    "supplementary context (e.g. mockups, wireframes, or "
                    "screenshots of the feature or an error). Assess each "
                    "image's relevance to THIS requirement. Use only the "
                    "relevant ones to disambiguate and sharpen the spec; they "
                    "AUGMENT the requirement and do NOT replace the acceptance "
                    "criteria in `spec.md`. If an image genuinely conflicts "
                    "with a stated requirement, raise a `[CLARIFICATION "
                    "NEEDED]` instead of silently overriding. End the document "
                    "with a brief `## Context Images` note recording which "
                    "images you used and which you disregarded (one-line "
                    "reason each)."
                )

        result = await call_reasoning_llm_with_hitl(
            agent,
            ctx=ctx,
            workdir=wd,
            user_prompt=base_user_prompt,
            inputs=inputs,
            output_filename="refined-spec.md",
            output_schema=None,  # markdown output; schema validates projection only
            timeout_s=self.timeout_s,
            step=2,
            agent_label="refine-spec",
            images=context_image_blocks or None,
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
                # Format-fixer rescue: a cheap Haiku sub-agent reshapes the
                # markdown to satisfy the audit without changing semantics.
                # Adopted whenever it makes MONOTONIC progress (strictly
                # fewer violations than the pre-fix spec) — not only on a
                # clean zero. A partial improvement (e.g. 13 -> 1) is a
                # better artifact to hand the retry loop than the original,
                # and discarding it silently masked progress and wasted a
                # retry. If it reaches zero, the step passes outright.
                rescued = None
                if _format_fixer_enabled():
                    rescued = await _run_format_fixer(
                        ctx=ctx,
                        md_text=md_dst.read_text(encoding="utf-8"),
                        violations=violations,
                        timeout_s=max(60, self.timeout_s // 3),
                    )
                if rescued is not None:
                    try:
                        rescued_projection = _project_to_json(rescued)
                        rescued_violations = audit_refined_spec(rescued_projection)
                    except Exception as e:
                        log.warning("step02.format_fixer_parse_failed", error=str(e))
                        rescued_projection = None
                        rescued_violations = violations
                    if (
                        rescued_projection is not None
                        and len(rescued_violations) < len(violations)
                    ):
                        # Adopt the strictly-better artifact.
                        md_dst.write_text(rescued, encoding="utf-8")
                        json_dst.write_text(
                            json.dumps(rescued_projection, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        projection = rescued_projection
                        if not rescued_violations:
                            log.info(
                                "step02.format_fixer_rescued",
                                violations_before=len(violations),
                            )
                            notes += (
                                f"; format-fixer rescued {len(violations)} "
                                "audit violation(s)"
                            )
                        else:
                            log.info(
                                "step02.format_fixer_improved",
                                violations_before=len(violations),
                                violations_after=len(rescued_violations),
                            )
                            notes += (
                                f"; format-fixer reduced {len(violations)}->"
                                f"{len(rescued_violations)} violation(s)"
                            )
                        violations = rescued_violations
                    else:
                        log.info(
                            "step02.format_fixer_no_op",
                            violations_before=len(violations),
                            violations_after=len(rescued_violations),
                        )

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
                    error=_format_violations_for_agent(
                        "refined-spec",
                        violations,
                        hint=_AUDIT_PARSER_CONTRACT,
                    ),
                    notes=notes + f"; audit_violations={len(violations)}",
                )

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )
