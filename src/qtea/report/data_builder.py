"""Build a normalized RunReport from prior step outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qtea.checkpoints import RunState, load_state
from qtea.logging_setup import get_logger
from qtea.workspace import Workspace

log = get_logger(__name__)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("report.load_failed", path=str(path), error=str(e))
        return None


@dataclass
class StepTiming:
    """Per-step row for the report's pipeline-execution table."""

    step: int
    name: str
    status: str
    duration_s: float | None
    tokens_input: int
    tokens_output: int
    tokens_cache_creation: int
    tokens_cache_read: int
    cost_usd: float
    agent_calls: int


@dataclass
class AuxTiming:
    """Per-helper-agent row appended after Step 11.

    The debug agent (RCA) + critical-thinking + principal-software-engineer
    all fire on retry exhaustion; they were previously invisible in the
    summary table because their billing was folded into the parent step's
    cost cell. This dataclass gives each one its own row so the operator
    can see where the money went in a failed run.
    """

    step: int  # parent step this aux agent ran under
    agent: str
    phase: str  # "debug" | "critical_thinking" | "principal_engineer"
    status: str
    duration_s: float | None
    tokens_input: int
    tokens_output: int
    tokens_cache_creation: int
    tokens_cache_read: int
    cost_usd: float
    agent_calls: int


@dataclass
class ReportSummary:
    total_tests: int
    passed: int
    failed: int
    skipped: int
    errors: int
    total_bugs: int
    duration_s: float | None
    pass_rate: float
    infrastructure_errors: int = 0
    pipeline_duration_s: float = 0.0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_tokens_cache_creation: int = 0
    total_tokens_cache_read: int = 0
    total_cost_usd: float = 0.0
    total_agent_calls: int = 0


@dataclass
class RunReport:
    run_id: str
    generated_at: str
    plan: dict[str, Any] | None
    strategy: dict[str, Any] | None
    run_results: dict[str, Any]
    bug_reports: dict[str, Any]
    summary: ReportSummary
    steps_summary: list[StepTiming] = field(default_factory=list)
    auxiliary_summary: list[AuxTiming] = field(default_factory=list)
    bug_classification_fallback: bool = False


def _empty_run_results() -> dict[str, Any]:
    return {
        "framework": "unknown",
        "command": "",
        "started_at": "",
        "finished_at": "",
        "results": [],
    }


def _empty_bug_reports(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total_failures": 0,
            "by_severity": {"critical": 0, "major": 0, "minor": 0, "cosmetic": 0},
            "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "by_category": {
                "functional": 0, "ui": 0, "performance": 0, "security": 0,
                "accessibility": 0, "integration": 0, "flaky": 0, "environment": 0,
            },
        },
        "bugs": [],
    }


def _compute_summary(
    run_results: dict,
    bug_reports: dict,
    steps_summary: list[StepTiming] | None = None,
    auxiliary_summary: list[AuxTiming] | None = None,
) -> ReportSummary:
    steps_summary = steps_summary or []
    auxiliary_summary = auxiliary_summary or []
    totals = run_results.get("totals")
    results = run_results.get("results", [])

    if totals:
        total = totals.get("tests", len(results))
        passed = totals.get("passed", 0)
        failed = totals.get("failed", 0)
        skipped = totals.get("skipped", 0)
        errors = totals.get("errors", 0)
        infra_errors = totals.get("infrastructure_errors", 0)
    else:
        total = len(results)
        passed = sum(1 for r in results if r.get("status") == "passed")
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        errors = sum(1 for r in results if r.get("status") == "error")
        infra_errors = 0

    total_bugs = len(bug_reports.get("bugs", []))
    duration_s = run_results.get("duration_s")
    # Finding 20: a run that executed ZERO real tests must NOT render as 100%
    # pass — that is the report-layer false-green (the old `else 1.0`). A
    # zero-test run is a non-result; show 0%. And infrastructure_errors
    # (collection/import failures, synthetic T-runner-failure entries) are
    # excluded from `tests`, so they must be surfaced explicitly rather than
    # silently vanishing behind a green headline.
    pass_rate = (passed / total) if total > 0 else 0.0

    # Debug/critical-thinking/PSE runs are wall-clock time the operator
    # waited too, so their duration counts toward the pipeline duration
    # even though they run outside the main step (post `step.end`).
    pipeline_duration = sum(
        st.duration_s for st in steps_summary if st.duration_s is not None
    ) + sum(
        aux.duration_s for aux in auxiliary_summary if aux.duration_s is not None
    )

    # Totals sum steps + aux so the TOTAL row equals the sum of every
    # visible cost cell. The parent step no longer bundles the aux cost —
    # that split was the whole point of the aux-row change; keeping the
    # totals correct is what makes it non-regressive at the pipeline level.
    def _sum(field: str) -> int | float:
        return sum(getattr(st, field) for st in steps_summary) + sum(
            getattr(aux, field) for aux in auxiliary_summary
        )

    return ReportSummary(
        total_tests=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        total_bugs=total_bugs,
        duration_s=duration_s,
        pass_rate=pass_rate,
        infrastructure_errors=infra_errors,
        pipeline_duration_s=round(pipeline_duration, 3),
        total_tokens_input=int(_sum("tokens_input")),
        total_tokens_output=int(_sum("tokens_output")),
        total_tokens_cache_creation=int(_sum("tokens_cache_creation")),
        total_tokens_cache_read=int(_sum("tokens_cache_read")),
        total_cost_usd=round(float(_sum("cost_usd")), 6),
        total_agent_calls=int(_sum("agent_calls")),
    )


def _steps_summary_from_state(state: RunState | None) -> list[StepTiming]:
    if state is None:
        return []
    rows: list[StepTiming] = []
    for step_num in sorted(state.steps):
        rec = state.steps[step_num]
        rows.append(
            StepTiming(
                step=rec.step,
                name=rec.name,
                status=rec.status,
                duration_s=rec.duration_s,
                tokens_input=rec.tokens_input,
                tokens_output=rec.tokens_output,
                tokens_cache_creation=rec.tokens_cache_creation,
                tokens_cache_read=rec.tokens_cache_read,
                cost_usd=rec.cost_usd,
                agent_calls=rec.agent_calls,
            )
        )
    return rows


def _aux_summary_from_state(state: RunState | None) -> list[AuxTiming]:
    if state is None:
        return []
    # Preserve insertion order (debug → critical_thinking → principal_engineer)
    # from base.py's execute() flow; that's the natural chronology and the
    # order operators expect to read.
    return [
        AuxTiming(
            step=a.step,
            agent=a.agent,
            phase=a.phase,
            status=a.status,
            duration_s=a.duration_s,
            tokens_input=a.tokens_input,
            tokens_output=a.tokens_output,
            tokens_cache_creation=a.tokens_cache_creation,
            tokens_cache_read=a.tokens_cache_read,
            cost_usd=a.cost_usd,
            agent_calls=a.agent_calls,
        )
        for a in state.auxiliary_records
    ]


def build_report(ws: Workspace) -> RunReport:
    run_results = _load_json(ws.step_dir(9) / "run-results.json") or _empty_run_results()
    bug_reports = _load_json(ws.step_dir(10) / "bug-reports.json") or _empty_bug_reports(ws.run_id)
    plan = _load_json(ws.step_dir(3) / "plan.json")
    strategy = _load_json(ws.step_dir(4) / "test-design.json")
    state = load_state(ws.state_file)
    steps_summary = _steps_summary_from_state(state)
    auxiliary_summary = _aux_summary_from_state(state)

    # Detect step 10 fallback from checkpoint notes
    step10_rec = state.steps.get(10) if state else None
    fallback = bool(
        step10_rec
        and step10_rec.notes
        and "fallback=True" in step10_rec.notes
    )

    return RunReport(
        run_id=ws.run_id,
        generated_at=datetime.now(UTC).isoformat(),
        plan=plan,
        strategy=strategy,
        run_results=run_results,
        bug_reports=bug_reports,
        summary=_compute_summary(
            run_results, bug_reports, steps_summary, auxiliary_summary
        ),
        steps_summary=steps_summary,
        auxiliary_summary=auxiliary_summary,
        bug_classification_fallback=fallback,
    )


def to_dict(report: RunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "generated_at": report.generated_at,
        "plan": report.plan,
        "strategy": report.strategy,
        "run_results": report.run_results,
        "bug_reports": report.bug_reports,
        "summary": {
            "total_tests": report.summary.total_tests,
            "passed": report.summary.passed,
            "failed": report.summary.failed,
            "skipped": report.summary.skipped,
            "errors": report.summary.errors,
            "infrastructure_errors": report.summary.infrastructure_errors,
            "total_bugs": report.summary.total_bugs,
            "duration_s": report.summary.duration_s,
            "pass_rate": report.summary.pass_rate,
            "pipeline_duration_s": report.summary.pipeline_duration_s,
            "total_tokens_input": report.summary.total_tokens_input,
            "total_tokens_output": report.summary.total_tokens_output,
            "total_tokens_cache_creation": report.summary.total_tokens_cache_creation,
            "total_tokens_cache_read": report.summary.total_tokens_cache_read,
            "total_cost_usd": report.summary.total_cost_usd,
            "total_agent_calls": report.summary.total_agent_calls,
        },
        "steps_summary": [
            {
                "step": st.step,
                "name": st.name,
                "status": st.status,
                "duration_s": st.duration_s,
                "tokens_input": st.tokens_input,
                "tokens_output": st.tokens_output,
                "tokens_cache_creation": st.tokens_cache_creation,
                "tokens_cache_read": st.tokens_cache_read,
                "cost_usd": st.cost_usd,
                "agent_calls": st.agent_calls,
            }
            for st in report.steps_summary
        ],
        "auxiliary_summary": [
            {
                "step": aux.step,
                "agent": aux.agent,
                "phase": aux.phase,
                "status": aux.status,
                "duration_s": aux.duration_s,
                "tokens_input": aux.tokens_input,
                "tokens_output": aux.tokens_output,
                "tokens_cache_creation": aux.tokens_cache_creation,
                "tokens_cache_read": aux.tokens_cache_read,
                "cost_usd": aux.cost_usd,
                "agent_calls": aux.agent_calls,
            }
            for aux in report.auxiliary_summary
        ],
    }
