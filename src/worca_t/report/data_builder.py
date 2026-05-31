"""Build a normalized RunReport from prior step outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worca_t.checkpoints import RunState, load_state
from worca_t.logging_setup import get_logger
from worca_t.workspace import Workspace

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
class ReportSummary:
    total_tests: int
    passed: int
    failed: int
    skipped: int
    errors: int
    total_bugs: int
    duration_s: float | None
    pass_rate: float
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
) -> ReportSummary:
    steps_summary = steps_summary or []
    totals = run_results.get("totals")
    results = run_results.get("results", [])

    if totals:
        total = totals.get("tests", len(results))
        passed = totals.get("passed", 0)
        failed = totals.get("failed", 0)
        skipped = totals.get("skipped", 0)
        errors = totals.get("errors", 0)
    else:
        total = len(results)
        passed = sum(1 for r in results if r.get("status") == "passed")
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        errors = sum(1 for r in results if r.get("status") == "error")

    total_bugs = len(bug_reports.get("bugs", []))
    duration_s = run_results.get("duration_s")
    pass_rate = (passed / total) if total > 0 else 1.0

    pipeline_duration = sum(
        st.duration_s for st in steps_summary if st.duration_s is not None
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
        pipeline_duration_s=round(pipeline_duration, 3),
        total_tokens_input=sum(st.tokens_input for st in steps_summary),
        total_tokens_output=sum(st.tokens_output for st in steps_summary),
        total_tokens_cache_creation=sum(st.tokens_cache_creation for st in steps_summary),
        total_tokens_cache_read=sum(st.tokens_cache_read for st in steps_summary),
        total_cost_usd=round(sum(st.cost_usd for st in steps_summary), 6),
        total_agent_calls=sum(st.agent_calls for st in steps_summary),
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


def build_report(ws: Workspace) -> RunReport:
    run_results = _load_json(ws.step_dir(9) / "run-results.json") or _empty_run_results()
    bug_reports = _load_json(ws.step_dir(10) / "bug-reports.json") or _empty_bug_reports(ws.run_id)
    plan = _load_json(ws.step_dir(3) / "plan.json")
    strategy = _load_json(ws.step_dir(4) / "test-strategy.json")
    steps_summary = _steps_summary_from_state(load_state(ws.state_file))

    return RunReport(
        run_id=ws.run_id,
        generated_at=datetime.now(UTC).isoformat(),
        plan=plan,
        strategy=strategy,
        run_results=run_results,
        bug_reports=bug_reports,
        summary=_compute_summary(run_results, bug_reports, steps_summary),
        steps_summary=steps_summary,
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
    }
