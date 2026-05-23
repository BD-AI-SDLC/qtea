"""Build a normalized RunReport from prior step outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
class ReportSummary:
    total_tests: int
    passed: int
    failed: int
    skipped: int
    errors: int
    total_bugs: int
    duration_s: float | None
    pass_rate: float


@dataclass
class RunReport:
    run_id: str
    generated_at: str
    plan: dict[str, Any] | None
    strategy: dict[str, Any] | None
    run_results: dict[str, Any]
    bug_reports: dict[str, Any]
    summary: ReportSummary


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


def _compute_summary(run_results: dict, bug_reports: dict) -> ReportSummary:
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

    return ReportSummary(
        total_tests=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        total_bugs=total_bugs,
        duration_s=duration_s,
        pass_rate=pass_rate,
    )


def build_report(ws: Workspace) -> RunReport:
    run_results = _load_json(ws.step_dir(9) / "run-results.json") or _empty_run_results()
    bug_reports = _load_json(ws.step_dir(10) / "bug-reports.json") or _empty_bug_reports(ws.run_id)
    plan = _load_json(ws.step_dir(3) / "plan.json")
    strategy = _load_json(ws.step_dir(4) / "test-strategy.json")

    return RunReport(
        run_id=ws.run_id,
        generated_at=datetime.now(UTC).isoformat(),
        plan=plan,
        strategy=strategy,
        run_results=run_results,
        bug_reports=bug_reports,
        summary=_compute_summary(run_results, bug_reports),
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
        },
    }
