"""Step 11: Report generation (built-in HTML + optional Allure)."""

from __future__ import annotations

import shutil
import webbrowser

from worca_t.config import step_timeout
from worca_t.logging_setup import get_logger
from worca_t.report.allure_writer import generate_allure_html, write_allure_results
from worca_t.report.data_builder import build_report, to_dict
from worca_t.report.html_renderer import render_html
from worca_t.schemas import write_validated
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)


class ReportStep(Step):
    number = 11
    name = "report"
    timeout_s = step_timeout(11)

    def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)

        run_results_path = ctx.workspace.step_dir(9) / "run-results.json"
        if not run_results_path.exists():
            return StepResult(
                success=True,
                status="skipped",
                outputs=[],
                notes="step 9 outputs missing",
            )

        report = build_report(ctx.workspace)
        data = to_dict(report)

        data_dir = out_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        write_validated(data_dir / "run.json", data, "report-data")

        mode = ctx.options.report
        outputs = [data_dir / "run.json"]
        notes_parts: list[str] = ["data/run.json=valid"]

        want_builtin = mode in ("auto", "builtin", "both")
        want_allure = mode in ("auto", "allure", "both")

        if want_builtin:
            html = render_html(report, inline_images=ctx.options.report_inline_images)
            html_path = out_dir / "index.html"
            html_path.write_text(html, encoding="utf-8")
            outputs.append(html_path)
            notes_parts.append("html=yes")

        allure_ok = False
        if want_allure:
            allure_present = shutil.which("allure") is not None
            if mode == "auto" and not allure_present:
                notes_parts.append("allure=skipped")
            elif not allure_present:
                notes_parts.append("allure=not-found")
                log.warning("report.allure_requested_but_absent")
            else:
                allure_results_dir = out_dir / "allure-results"
                write_allure_results(report, allure_results_dir)
                allure_html_dir = out_dir / "allure-html"
                allure_ok = generate_allure_html(allure_results_dir, allure_html_dir)
                outputs.append(allure_results_dir)
                if allure_ok:
                    outputs.append(allure_html_dir)
                notes_parts.append(f"allure={'yes' if allure_ok else 'results-only'}")

        status = "completed"
        if mode == "allure" and not allure_ok:
            status = "warned"

        if ctx.options.open_report:
            html_path = out_dir / "index.html"
            if html_path.exists():
                try:
                    webbrowser.open(html_path.as_uri())
                except Exception:
                    log.warning("report.open_failed")

        return StepResult(
            success=True,
            status=status,
            outputs=outputs,
            notes=" ".join(notes_parts),
        )
