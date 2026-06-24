"""Coverage + traceability audits across Steps 2 → 3 → 4.

Each `audit_*` returns a list of human-readable, agent-readable violation
strings. Empty list = clean. Strings prefix with the failing item ID and
end with the corrective action — these strings are fed verbatim back to
the LLM on retry via the prior-attempt prepend in each step's `run()`.

Mirrors the shape of
`qtea.steps.s07_test_architect._validate_plan_against_inventory`:
pure-Python, side-effect free, list-of-strings return.

Built in PR 2 of the coverage-audit roll-out. Unused until PR 3 wires
the validators into Steps 2/3, and PR 4 into Step 4.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

# ---------------------------------------------------------------------------
# Constants + helpers
# ---------------------------------------------------------------------------

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "UNKNOWN": 99}
_AUTOMATION_TAGS = frozenset({"AUTOMATABLE", "MANUAL_ONLY", "NEEDS_INVESTIGATION"})
_HIGH_SEVERITIES = frozenset({"critical", "high"})
_ASSUMPTIONS_HEADER_RE = re.compile(r"^##\s+Assumptions\b", re.MULTILINE)

_REQUIRES_TC_RE = re.compile(r"\[requires\s+TC\]", re.I)


def _coverage_note_ids(coverage_notes: list[dict] | None) -> set[str]:
    if not coverage_notes:
        return set()
    return {n.get("item_id", "") for n in coverage_notes if n.get("item_id")}


def _coverage_note_ids_with_resolution(
    coverage_notes: list[dict] | None,
    resolution: str,
) -> set[str]:
    if not coverage_notes:
        return set()
    return {
        n.get("item_id", "")
        for n in coverage_notes
        if n.get("item_id") and n.get("resolution") == resolution
    }


def _ec_severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
        severity.lower(), 99
    )


def _format_violations_for_agent(artifact: str, violations: list[str]) -> str:
    """Wrap the violation list with instructions that the agent re-prompts on
    retry. The wording is calibrated for an LLM that has just produced the
    failing artifact and is being given a second attempt."""
    return (
        f"Coverage audit failed for {artifact}: {len(violations)} violation(s). "
        "On retry, fix EACH item below by editing the markdown to add the "
        "missing field or correct the cited issue, then resubmit. "
        "Do NOT drop a TC unless you also add a `## Coverage Notes` entry "
        "explaining why. Violations:\n- " + "\n- ".join(violations)
    )


# ---------------------------------------------------------------------------
# Step 2: refined-spec audit
# ---------------------------------------------------------------------------

def audit_refined_spec(spec: dict[str, Any]) -> list[str]:
    """Audit a refined-spec JSON projection. Returns violations."""
    violations: list[str] = []
    coverage_ids = _coverage_note_ids(spec.get("coverage_notes"))

    # 1. Every AC bullet in the legacy free-text list should also have a
    #    structured counterpart (i.e. carry an AC-ID).
    legacy = spec.get("acceptance_criteria") or []
    structured = spec.get("acceptance_criteria_structured") or []
    structured_texts = {(a.get("text") or "").strip().lower() for a in structured}
    structured_ids = {a.get("id") for a in structured if a.get("id")}
    for bullet in legacy:
        text = (bullet or "").strip()
        # If a structured entry's `text` contains this bullet verbatim
        # (or vice versa), treat it as covered.
        norm = text.lower()
        if any(norm in t or t in norm for t in structured_texts if t):
            continue
        # Cited in Coverage Notes? Exempt.
        if any(cid and cid in text for cid in coverage_ids):
            continue
        snippet = text[:60] + ("..." if len(text) > 60 else "")
        violations.append(
            f"AC-?: bullet '{snippet}' has no AC-ID; add one (AC-N or AC-DOMAIN-N)"
        )

    # 2. Every structured AC must have a real automation tag.
    for ac in structured:
        ac_id = ac.get("id", "AC-?")
        if ac_id in coverage_ids:
            continue
        auto = ac.get("automation", "UNKNOWN")
        if auto == "UNKNOWN" or auto not in _AUTOMATION_TAGS:
            violations.append(
                f"{ac_id}: missing automation tag — append one of "
                "`[AUTOMATABLE]`, `[MANUAL ONLY]`, `[NEEDS INVESTIGATION]`"
            )

    # 3. Every structured EC must carry severity + automation.
    for ec in spec.get("edge_cases_structured") or []:
        ec_id = ec.get("id", "EC-?")
        if ec_id in coverage_ids:
            continue
        sev = (ec.get("severity") or "UNKNOWN").lower()
        if sev not in {"critical", "high", "medium", "low"}:
            violations.append(
                f"{ec_id}: severity is unknown — set to critical|high|medium|low"
            )
        auto = ec.get("automation", "UNKNOWN")
        if auto == "UNKNOWN" or auto not in _AUTOMATION_TAGS:
            violations.append(
                f"{ec_id}: missing automation tag — append one of "
                "`[AUTOMATABLE]`, `[MANUAL ONLY]`, `[NEEDS INVESTIGATION]`"
            )

    # 4. Every NFR with a hard threshold must be promoted to an AC.
    ac_id_set = structured_ids
    for nfr in spec.get("nfrs_structured") or []:
        nfr_id = nfr.get("id", "NFR-?")
        if nfr_id in coverage_ids:
            continue
        if not nfr.get("has_threshold"):
            continue
        promoted = nfr.get("promoted_to_ac")
        if not promoted:
            violations.append(
                f"{nfr_id} has a hard threshold but is not promoted to an AC; "
                "add an AC-NFR-... entry under ## Acceptance Criteria and set "
                "its NFR `promoted_to_ac` field"
            )
        elif promoted not in ac_id_set:
            violations.append(
                f"{nfr_id} declares promoted_to_ac={promoted!r} but {promoted} "
                "is not in acceptance_criteria_structured; create that AC or "
                "fix the promotion target"
            )

    # 5. Alt-flow steps + in-scope bullets must be covered by an AC OR
    #    carry an explicit `[requires TC]` marker. Walk the sections tree.
    ac_user_flows = {
        (ac.get("user_flow") or "").strip().lower()
        for ac in structured
        if ac.get("user_flow")
    }
    ac_texts = {(ac.get("text") or "").strip().lower() for ac in structured}

    def _bullet_covered(bullet: str) -> bool:
        norm = bullet.strip().lower()
        if not norm:
            return True
        if _REQUIRES_TC_RE.search(bullet):
            return True
        if any(cid and cid in bullet for cid in coverage_ids):
            return True
        if any(norm in t or (t and t in norm) for t in ac_texts):
            return True
        return bool(any(norm in f or (f and f in norm) for f in ac_user_flows))

    def _walk_sections(sections: list[dict] | None) -> Iterable[dict]:
        if not sections:
            return
        stack = list(sections)
        while stack:
            s = stack.pop()
            yield s
            children = s.get("children") or []
            stack.extend(reversed(children))

    for section in _walk_sections(spec.get("sections")):
        title = (section.get("title") or "").lower()
        is_alt = "alternative flow" in title or "alt flow" in title or "alt-flow" in title
        is_scope = "in scope" in title or title == "test boundaries"
        if not (is_alt or is_scope):
            continue
        for bullet in section.get("bullets") or []:
            if _bullet_covered(bullet):
                continue
            snippet = bullet[:60] + ("..." if len(bullet) > 60 else "")
            kind = "alt-flow step" if is_alt else "in-scope bullet"
            violations.append(
                f"{kind} '{snippet}' is not covered by any AC and has no "
                "[requires TC] marker; promote to an AC or annotate"
            )

    return violations


# ---------------------------------------------------------------------------
# Step 3: plan audit
# ---------------------------------------------------------------------------

def audit_plan(plan: dict[str, Any], refined_spec: dict[str, Any]) -> list[str]:
    """Audit plan.json against refined-spec.json. Returns violations."""
    violations: list[str] = []
    plan_tcs = plan.get("test_cases") or []
    plan_coverage = _coverage_note_ids(plan.get("coverage_notes"))
    spec_coverage = _coverage_note_ids(refined_spec.get("coverage_notes"))

    refined_acs = refined_spec.get("acceptance_criteria_structured") or []
    refined_ecs = refined_spec.get("edge_cases_structured") or []
    ac_priority: dict[str, str] = {
        a.get("id", ""): a.get("priority", "UNKNOWN") for a in refined_acs
    }
    ac_ids_in_spec = set(ac_priority.keys()) - {""}
    ec_priority: dict[str, str] = {}
    ec_severity: dict[str, str] = {}
    for ec in refined_ecs:
        ec_id = ec.get("id", "")
        if not ec_id:
            continue
        sev = (ec.get("severity") or "UNKNOWN").lower()
        ec_severity[ec_id] = sev
        # Map severity → priority for inheritance comparisons.
        ec_priority[ec_id] = {
            "critical": "P0", "high": "P1", "medium": "P2", "low": "P3",
        }.get(sev, "UNKNOWN")
    ec_ids_in_spec = set(ec_severity.keys())

    covered_ac_ids: set[str] = set()
    covered_ec_ids: set[str] = set()

    # 1. Per-TC structural checks.
    for tc in plan_tcs:
        tc_id = tc.get("id", "TC-?")
        if not tc.get("req_id"):
            violations.append(
                f"{tc_id} is missing req_id; copy from the refined-spec "
                "requirement_id"
            )
        for ac in tc.get("ac_ids") or []:
            covered_ac_ids.add(ac)
            if ac_ids_in_spec and ac not in ac_ids_in_spec:
                violations.append(
                    f"{tc_id} references {ac} which does not exist in the "
                    "refined-spec acceptance_criteria_structured"
                )
        for ec in tc.get("ec_ids") or []:
            covered_ec_ids.add(ec)
            if ec_ids_in_spec and ec not in ec_ids_in_spec:
                violations.append(
                    f"{tc_id} references {ec} which does not exist in the "
                    "refined-spec edge_cases_structured"
                )
        # 1c. Priority inheritance.
        tc_priority = tc.get("priority", "UNKNOWN")
        ranks: list[tuple[int, str]] = []
        for ac in tc.get("ac_ids") or []:
            p = ac_priority.get(ac, "UNKNOWN")
            if p in _PRIORITY_RANK:
                ranks.append((_PRIORITY_RANK[p], p))
        for ec in tc.get("ec_ids") or []:
            p = ec_priority.get(ec, "UNKNOWN")
            if p in _PRIORITY_RANK:
                ranks.append((_PRIORITY_RANK[p], p))
        if ranks:
            best_rank, best_priority = min(ranks, key=lambda r: r[0])
            if (
                best_priority != "UNKNOWN"
                and _PRIORITY_RANK.get(tc_priority, 99) > best_rank
            ):
                violations.append(
                    f"{tc_id} has priority {tc_priority} but covers items at "
                    f"priority {best_priority}; inherit the highest-priority "
                    f"underlying item (set priority {best_priority})"
                )

    # 2. ≥1 TC per AC.
    for ac_id in ac_ids_in_spec:
        if ac_id in covered_ac_ids:
            continue
        if ac_id in plan_coverage or ac_id in spec_coverage:
            continue
        violations.append(
            f"{ac_id} has no covering test case; add a plan TC with "
            f"ac_ids: [{ac_id}] (or add a ## Coverage Notes entry recording why)"
        )

    # 3. ≥1 TC per high-severity EC.
    for ec_id, sev in ec_severity.items():
        if sev not in _HIGH_SEVERITIES:
            continue
        if ec_id in covered_ec_ids:
            continue
        if ec_id in plan_coverage or ec_id in spec_coverage:
            continue
        violations.append(
            f"{ec_id} ({sev}-severity edge case) has no covering TC; add a "
            "plan TC referencing it in ec_ids or record an accepted-risk drop "
            "in ## Coverage Notes"
        )

    # 4. Coverage Notes inheritance: AC dropped in spec must be carried in plan.
    for ac_id in spec_coverage:
        if not ac_id.startswith("AC-"):
            continue
        if ac_id in covered_ac_ids or ac_id in plan_coverage:
            continue
        violations.append(
            f"{ac_id} was dropped in refined-spec ## Coverage Notes but the "
            "plan has no TC and no Coverage Notes entry for it; either add a "
            "TC or carry the drop forward"
        )

    return violations


# ---------------------------------------------------------------------------
# Step 4: strategy audit
# ---------------------------------------------------------------------------

def audit_strategy(
    strategy: dict[str, Any],
    plan: dict[str, Any],
    refined_spec: dict[str, Any],
    *,
    raw_md: str = "",
) -> list[str]:
    """Audit test-strategy against plan + refined-spec. Returns violations."""
    violations: list[str] = []
    strategy_tcs = strategy.get("test_cases") or []
    plan_tcs = plan.get("test_cases") or []
    strategy_coverage = _coverage_note_ids(strategy.get("coverage_notes"))
    accepted_risk_ids = _coverage_note_ids_with_resolution(
        strategy.get("coverage_notes"), "accepted_risk"
    )

    plan_tc_by_id = {tc.get("id", ""): tc for tc in plan_tcs}
    plan_to_strategy: dict[str, list[dict]] = defaultdict(list)
    for stc in strategy_tcs:
        for src in stc.get("derived_from") or [stc.get("id", "")]:
            if src:
                plan_to_strategy[src].append(stc)

    # 1. 1:1 trace per plan TC (or accepted-risk drop).
    for plan_tc_id in plan_tc_by_id:
        if plan_tc_id in plan_to_strategy:
            continue
        if plan_tc_id in accepted_risk_ids:
            continue
        if plan_tc_id in strategy_coverage:
            # Recorded but not accepted_risk — still a leak unless explicit.
            continue
        violations.append(
            f"plan {plan_tc_id} has no corresponding strategy TC "
            "(derived_from missing in all strategy TCs); add a strategy TC "
            "with derived_from: ["
            f"{plan_tc_id}] or record an accepted_risk drop in ## Coverage Notes"
        )

    # 2. Legitimate consolidation only.
    for stc in strategy_tcs:
        derived = stc.get("derived_from") or []
        if len(derived) <= 1:
            continue
        stc_id = stc.get("id", "TC-?")
        sources = [plan_tc_by_id.get(s) for s in derived]
        sources = [s for s in sources if s is not None]
        if len(sources) <= 1:
            continue
        priorities = {s.get("priority", "UNKNOWN") for s in sources}
        # Automation: plan uses {automation, manual, needs_investigation};
        # strategy uses ui|api|... — they don't have to match each other, but
        # the SOURCES must share whatever automation flavor was declared.
        autos = {s.get("automation", "UNKNOWN") for s in sources}
        if len(priorities) > 1:
            detail = ", ".join(f"{s['id']}={s.get('priority','?')}" for s in sources)
            violations.append(
                f"strategy {stc_id} consolidates plan TCs with mixed "
                f"priorities ({detail}); split into separate strategy TCs"
            )
        if len(autos) > 1:
            detail = ", ".join(f"{s['id']}={s.get('automation','?')}" for s in sources)
            violations.append(
                f"strategy {stc_id} consolidates plan TCs with mixed "
                f"automation types ({detail}); split into separate strategy TCs"
            )

    # 3. Section naming enforcement.
    if raw_md and _ASSUMPTIONS_HEADER_RE.search(raw_md):
        violations.append(
            "strategy uses forbidden `## Assumptions` section; rename to "
            "`## Coverage Notes` and ensure dropped items have an "
            "explicit resolution (dropped|scope_excluded|accepted_risk)"
        )

    return violations


# ---------------------------------------------------------------------------
# Traceability matrix
# ---------------------------------------------------------------------------

def build_traceability_matrix(
    refined_spec: dict[str, Any],
    plan: dict[str, Any],
    strategy: dict[str, Any],
    *,
    run_id: str = "",
) -> dict[str, Any]:
    """Materialize the plan→strategy→AC→EC matrix.

    Resolution per entry:
      - mapped: 1 strategy TC, derived_from len == 1
      - split: >1 strategy TCs cover the same plan TC
      - merged: 1 strategy TC, derived_from len > 1 (one row per source)
      - dropped_accepted_risk: 0 strategy TCs + Coverage Notes accepted_risk
      - dropped: 0 strategy TCs + no Coverage Note (audit will flag)
    """
    plan_tcs = plan.get("test_cases") or []
    strategy_tcs = strategy.get("test_cases") or []
    plan_tc_by_id = {tc.get("id", ""): tc for tc in plan_tcs}
    accepted_risk_ids = _coverage_note_ids_with_resolution(
        strategy.get("coverage_notes"), "accepted_risk"
    )

    plan_to_strategy: dict[str, list[dict]] = defaultdict(list)
    for stc in strategy_tcs:
        for src in stc.get("derived_from") or [stc.get("id", "")]:
            if src:
                plan_to_strategy[src].append(stc)

    entries: list[dict[str, Any]] = []

    def _entry_for(plan_tc: dict, strategy_tc: dict | None, resolution: str) -> dict:
        ac_ids = list(plan_tc.get("ac_ids") or [])
        ec_ids = list(plan_tc.get("ec_ids") or [])
        nfr_ids = list(plan_tc.get("nfr_ids") or [])
        if strategy_tc is not None:
            for src in strategy_tc.get("ac_ids") or []:
                if src not in ac_ids:
                    ac_ids.append(src)
            for src in strategy_tc.get("ec_ids") or []:
                if src not in ec_ids:
                    ec_ids.append(src)
            for src in strategy_tc.get("nfr_ids") or []:
                if src not in nfr_ids:
                    nfr_ids.append(src)
        return {
            "plan_tc_id": plan_tc.get("id", ""),
            "strategy_tc_id": strategy_tc.get("id") if strategy_tc else None,
            "ac_ids": ac_ids,
            "ec_ids": ec_ids,
            "nfr_ids": nfr_ids,
            "priority": plan_tc.get("priority", "UNKNOWN"),
            "automation_type": (
                strategy_tc.get("automation_type") if strategy_tc else None
            ),
            "resolution": resolution,
        }

    consolidated_count = 0
    for plan_tc_id, plan_tc in plan_tc_by_id.items():
        covers = plan_to_strategy.get(plan_tc_id, [])
        if not covers:
            if plan_tc_id in accepted_risk_ids:
                entries.append(_entry_for(plan_tc, None, "dropped_accepted_risk"))
            else:
                entries.append(_entry_for(plan_tc, None, "dropped"))
            continue
        if len(covers) > 1:
            for stc in covers:
                entries.append(_entry_for(plan_tc, stc, "split"))
        else:
            stc = covers[0]
            derived_len = len(stc.get("derived_from") or [stc.get("id", "")])
            if derived_len > 1:
                entries.append(_entry_for(plan_tc, stc, "merged"))
                consolidated_count += 1
            else:
                entries.append(_entry_for(plan_tc, stc, "mapped"))

    # Avoid double-counting consolidated_count when split + merged co-occur.
    # The standard definition: count strategy TCs that consolidate >1 plan TC.
    consolidated_count = sum(
        1 for stc in strategy_tcs
        if len(stc.get("derived_from") or [stc.get("id", "")]) > 1
    )

    # Orphan ACs: in refined-spec but in no plan TC AND no Coverage Note.
    spec_acs = {
        a.get("id") for a in refined_spec.get("acceptance_criteria_structured") or []
        if a.get("id")
    }
    plan_covered_acs: set[str] = set()
    for tc in plan_tcs:
        plan_covered_acs.update(tc.get("ac_ids") or [])
    spec_coverage = _coverage_note_ids(refined_spec.get("coverage_notes"))
    plan_coverage = _coverage_note_ids(plan.get("coverage_notes"))
    orphan_acs = sorted(
        a for a in spec_acs
        if a not in plan_covered_acs
        and a not in spec_coverage
        and a not in plan_coverage
    )

    orphan_plan_tcs = sorted(
        e["plan_tc_id"] for e in entries if e["resolution"] == "dropped"
    )
    accepted_risk_drops = sorted(
        e["plan_tc_id"] for e in entries if e["resolution"] == "dropped_accepted_risk"
    )

    fake_now = os.environ.get("QTEA_FAKE_NOW")
    generated_at = fake_now or run_id or ""

    return {
        "requirement_id": refined_spec.get("requirement_id", "REQ-UNKNOWN"),
        "generated_at": generated_at,
        "run_id": run_id,
        "entries": entries,
        "summary": {
            "plan_tc_count": len(plan_tcs),
            "strategy_tc_count": len(strategy_tcs),
            "consolidated_count": consolidated_count,
            "orphan_plan_tcs": orphan_plan_tcs,
            "orphan_acs": orphan_acs,
            "accepted_risk_drops": accepted_risk_drops,
        },
    }


def audit_traceability_matrix(matrix: dict[str, Any]) -> list[str]:
    """Audit the materialized matrix. Mirrors Step 3 + Step 4 audit findings
    on the consolidated view — defense in depth."""
    violations: list[str] = []
    summary = matrix.get("summary") or {}
    for plan_tc_id in summary.get("orphan_plan_tcs") or []:
        violations.append(
            f"matrix orphan: plan {plan_tc_id} has no strategy TC and no "
            "accepted-risk drop; add a strategy TC or record the drop in "
            "## Coverage Notes"
        )
    for ac_id in summary.get("orphan_acs") or []:
        violations.append(
            f"matrix orphan AC: {ac_id} is in the refined-spec but has no "
            "plan TC and no Coverage Notes entry; add a TC or carry the drop"
        )
    return violations
