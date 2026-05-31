"""Step 11 report tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.report.allure_writer import generate_allure_html, write_allure_results
from worca_t.report.data_builder import RunReport, _compute_summary, build_report, to_dict
from worca_t.report.html_renderer import render_html
from worca_t.schemas import is_valid
from worca_t.steps.base import StepContext
from worca_t.steps.s11_report import ReportStep
from worca_t.workspace import create_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC).isoformat()


def _ctx(tmp_path: Path, **opts_kw) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    defaults = {"spec": "x", "sut": ".", "workspace_base": tmp_path / ".ws"}
    defaults.update(opts_kw)
    opts = PipelineOptions(**defaults)
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _run_results(
    results: list[dict] | None = None,
    totals: dict | None = None,
) -> dict:
    base = {
        "framework": "pytest",
        "command": "pytest tests/",
        "started_at": _NOW,
        "finished_at": _NOW,
        "results": results or [],
    }
    if totals:
        base["totals"] = totals
    return base


def _bug_reports(run_id: str = "r", bugs: list[dict] | None = None) -> dict:
    bug_list = bugs or []
    return {
        "run_id": run_id,
        "generated_at": _NOW,
        "summary": {
            "total_failures": len(bug_list),
            "by_severity": {"critical": 0, "major": len(bug_list), "minor": 0, "cosmetic": 0},
            "by_priority": {"P0": 0, "P1": 0, "P2": len(bug_list), "P3": 0},
            "by_category": {
                "functional": len(bug_list), "ui": 0, "performance": 0, "security": 0,
                "accessibility": 0, "integration": 0, "flaky": 0, "environment": 0,
            },
        },
        "bugs": bug_list,
    }


def _sample_bug(run_id: str = "r", idx: int = 1) -> dict:
    return {
        "id": f"BUG-{run_id}-{idx:03d}",
        "test_id": f"T-{idx}",
        "title": f"Bug number {idx}",
        "severity": "major",
        "priority": "P2",
        "category": "functional",
        "component": "",
        "requirement_id": "",
        "rationale": "auto-classified",
        "expected": "test should pass",
        "actual": "assertion failed",
        "recommended_action": {
            "immediate": "triage",
            "short_term": "fix",
            "long_term": "coverage",
        },
    }


def _seed(ctx: StepContext, *, results=None, totals=None, bugs=None, plan=None, strategy=None):
    s9 = ctx.workspace.step_dir(9)
    s9.mkdir(parents=True, exist_ok=True)
    (s9 / "run-results.json").write_text(
        json.dumps(_run_results(results, totals)), encoding="utf-8",
    )
    s10 = ctx.workspace.step_dir(10)
    s10.mkdir(parents=True, exist_ok=True)
    (s10 / "bug-reports.json").write_text(
        json.dumps(_bug_reports(ctx.workspace.run_id, bugs)), encoding="utf-8",
    )
    if plan is not None:
        s3 = ctx.workspace.step_dir(3)
        s3.mkdir(parents=True, exist_ok=True)
        (s3 / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    if strategy is not None:
        s4 = ctx.workspace.step_dir(4)
        s4.mkdir(parents=True, exist_ok=True)
        (s4 / "test-strategy.json").write_text(json.dumps(strategy), encoding="utf-8")


def _sample_results():
    return [
        {"id": "t1", "name": "test_login", "file": "test_auth.py", "status": "passed", "duration_s": 1.2},
        {"id": "t2", "name": "test_signup", "file": "test_auth.py", "status": "failed", "duration_s": 0.5, "message": "AssertionError"},
        {"id": "t3", "name": "test_logout", "file": "test_auth.py", "status": "skipped"},
    ]


# ---------------------------------------------------------------------------
# data_builder tests
# ---------------------------------------------------------------------------


def test_build_report_happy_path(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed(ctx, results=_sample_results(), bugs=[_sample_bug(ctx.workspace.run_id)])
    report = build_report(ctx.workspace)
    assert report.run_id == ctx.workspace.run_id
    assert report.summary.total_tests == 3
    assert report.summary.passed == 1
    assert report.summary.failed == 1
    assert report.summary.skipped == 1
    assert report.summary.total_bugs == 1


def test_build_report_missing_plan_strategy(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed(ctx, results=_sample_results())
    report = build_report(ctx.workspace)
    assert report.plan is None
    assert report.strategy is None
    assert report.summary.total_tests == 3


def test_build_report_zero_results(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed(ctx, results=[])
    report = build_report(ctx.workspace)
    assert report.summary.total_tests == 0
    assert report.summary.pass_rate == 1.0


def test_to_dict_validates_against_schema(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed(ctx, results=_sample_results(), bugs=[_sample_bug(ctx.workspace.run_id)])
    report = build_report(ctx.workspace)
    data = to_dict(report)
    ok, err = is_valid(data, "report-data")
    assert ok, err


def test_summary_uses_totals_when_present(tmp_path: Path):
    ctx = _ctx(tmp_path)
    totals = {"tests": 10, "passed": 7, "failed": 2, "skipped": 1, "errors": 0}
    _seed(ctx, results=_sample_results(), totals=totals)
    report = build_report(ctx.workspace)
    assert report.summary.total_tests == 10
    assert report.summary.passed == 7
    assert report.summary.pass_rate == 0.7


# ---------------------------------------------------------------------------
# html_renderer tests
# ---------------------------------------------------------------------------


def _make_report(
    results=None, bugs=None, plan=None, strategy=None, run_id="test-run",
) -> RunReport:
    rr = _run_results(results)
    br = _bug_reports(run_id, bugs)
    return RunReport(
        run_id=run_id,
        generated_at=_NOW,
        plan=plan,
        strategy=strategy,
        run_results=rr,
        bug_reports=br,
        summary=_compute_summary(rr, br),
    )


def test_render_html_valid_structure():
    report = _make_report(results=_sample_results())
    html = render_html(report)
    assert "<!DOCTYPE html>" in html
    assert "<html" in html
    assert "</html>" in html
    assert "test-run" in html
    assert "Summary" in html


def test_render_html_zero_failures():
    results = [{"id": "t1", "name": "t", "file": "f.py", "status": "passed"}]
    report = _make_report(results=results)
    html = render_html(report)
    assert "100%" in html
    assert "#22c55e" in html


def test_render_html_includes_bug_cards():
    bug = _sample_bug()
    report = _make_report(bugs=[bug])
    html = render_html(report)
    assert "BUG-r-001" in html
    assert "Bug number 1" in html
    assert "Bug Reports" in html


def test_render_html_inline_images(tmp_path: Path):
    img_path = tmp_path / "screen.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    results = [{
        "id": "t1", "name": "t", "file": "f.py", "status": "failed",
        "attachments": [{"path": str(img_path), "type": "screenshot"}],
    }]
    report = _make_report(results=results)
    html = render_html(report, inline_images=True)
    assert "data:image/png;base64," in html


def test_render_html_without_plan_strategy():
    report = _make_report(plan=None, strategy=None)
    html = render_html(report)
    assert "Plan" not in html or "Strategy" not in html
    assert "<!DOCTYPE html>" in html


# ---------------------------------------------------------------------------
# allure_writer tests
# ---------------------------------------------------------------------------


def test_write_allure_results_creates_files(tmp_path: Path):
    report = _make_report(results=_sample_results())
    out = tmp_path / "allure-results"
    written = write_allure_results(report, out)
    assert len(written) == 3
    for p in written:
        assert p.exists()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "uuid" in data
        assert data["status"] in ("passed", "failed", "skipped", "broken")


def test_generate_allure_html_skips_when_not_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert generate_allure_html(Path("a"), Path("b")) is False


def test_generate_allure_html_invokes_subprocess(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/allure")
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = generate_allure_html(tmp_path / "in", tmp_path / "out")
    assert result is True
    assert len(calls) == 1
    assert "allure" in calls[0][0]


# ---------------------------------------------------------------------------
# Step integration tests
# ---------------------------------------------------------------------------


async def test_step11_happy_path(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed(ctx, results=_sample_results(), bugs=[_sample_bug(ctx.workspace.run_id)])
    result = await ReportStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    out = ctx.workspace.step_dir(11)
    assert (out / "index.html").exists()
    assert (out / "data" / "run.json").exists()
    data = json.loads((out / "data" / "run.json").read_text(encoding="utf-8"))
    ok, err = is_valid(data, "report-data")
    assert ok, err


async def test_step11_skips_when_step9_missing(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await ReportStep().run(ctx)
    assert result.success
    assert result.status == "skipped"
    assert "missing" in (result.notes or "")


async def test_step11_report_builtin_no_allure(tmp_path: Path):
    ctx = _ctx(tmp_path, report="builtin")
    _seed(ctx, results=_sample_results())
    result = await ReportStep().run(ctx)
    assert result.success
    out = ctx.workspace.step_dir(11)
    assert (out / "index.html").exists()
    assert not (out / "allure-results").exists()


async def test_step11_report_auto_builtin_always(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    ctx = _ctx(tmp_path, report="auto")
    _seed(ctx, results=_sample_results())
    result = await ReportStep().run(ctx)
    assert result.success
    out = ctx.workspace.step_dir(11)
    assert (out / "index.html").exists()
    assert "html=yes" in (result.notes or "")


async def test_step11_open_report_flag(tmp_path: Path):
    ctx = _ctx(tmp_path, open_report=True)
    _seed(ctx, results=_sample_results())
    with patch("worca_t.steps.s11_report.webbrowser.open") as mock_open:
        result = await ReportStep().run(ctx)
        assert result.success
        assert mock_open.called
