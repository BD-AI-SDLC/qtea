"""Step 11: Report generation (built-in HTML + optional Allure)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import webbrowser

from qtea.config import step_timeout
from qtea.logging_setup import get_logger
from qtea.report.allure_writer import generate_allure_html, write_allure_results
from qtea.report.data_builder import build_report, to_dict
from qtea.report.html_renderer import render_html
from qtea.schemas import write_validated
from qtea.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)


class ReportStep(Step):
    number = 11
    name = "report"
    timeout_s = step_timeout(11)

    async def run(self, ctx: StepContext) -> StepResult:
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

        # --report allure / both: open the Allure UI automatically via a
        # background server process (allure open requires a server; file://
        # doesn't work for allure's XHR-based data loading). Use the full
        # path from shutil.which so Windows .bat shims resolve correctly.
        #
        # The child must be DETACHED from our process group, otherwise on
        # Windows our pipeline exit closes the console session and sends
        # CTRL_CLOSE to the half-started Java HTTP server — the browser
        # tab ends up at a port that nothing is bound to. POSIX has the
        # same risk via SIGHUP propagation, addressed by start_new_session.
        # We additionally sleep ~2 s so Jetty has time to bind the port
        # before our process exits (cold-start ~1.5–3 s).
        if allure_ok and mode in ("auto", "allure", "both"):
            allure_bin = shutil.which("allure")
            if allure_bin:
                stderr_log = out_dir / "allure-open.log"
                # Pipe stderr to a file so users have a diagnostic when the
                # Java side fails (missing JAVA_HOME, port collision, etc.).
                # Open in append-binary to avoid stomping prior attempts.
                stderr_fp = stderr_log.open("ab")
                popen_kwargs: dict = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": stderr_fp,
                }
                if sys.platform == "win32":
                    # ``CREATE_NO_WINDOW`` suppresses the Java/Jetty
                    # console that ``allure open`` would otherwise show.
                    # ``CREATE_NEW_PROCESS_GROUP`` keeps the child alive
                    # after our pipeline exits (no CTRL_C propagation).
                    # NOTE: ``DETACHED_PROCESS`` is intentionally absent —
                    # it is mutually exclusive with ``CREATE_NO_WINDOW``
                    # per the Windows API; combining them lets DETACHED
                    # win and Java allocates a visible console anyway.
                    popen_kwargs["creationflags"] = (
                        subprocess.CREATE_NO_WINDOW
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    )
                    popen_kwargs["close_fds"] = True
                else:
                    popen_kwargs["start_new_session"] = True
                try:
                    subprocess.Popen(
                        [allure_bin, "open", "--host", "127.0.0.1",
                         str(allure_html_dir)],
                        **popen_kwargs,
                    )
                    # Give Jetty a moment to bind before we exit. Without
                    # this the detach is necessary but insufficient — the
                    # Java process survives but the browser may race ahead
                    # and hit ERR_CONNECTION_REFUSED before the port is up.
                    time.sleep(2.0)
                    log.info(
                        "report.allure_opened",
                        path=str(allure_html_dir),
                        stderr_log=str(stderr_log),
                    )
                except Exception as e:
                    log.warning("report.allure_open_failed", error=str(e))
                finally:
                    # Close our handle to the stderr log; the child has its
                    # own duplicated fd (Popen dup'd before close_fds=True
                    # took effect on our side).
                    try:
                        stderr_fp.close()
                    except OSError:
                        pass
        elif ctx.options.open_report:
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
