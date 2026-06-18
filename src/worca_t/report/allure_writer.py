"""Allure 2.x compatible result writer."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from worca_t.logging_setup import get_logger
from worca_t.proxy import with_proxy_env
from worca_t.report.data_builder import RunReport

log = get_logger(__name__)

_STATUS_MAP = {
    "passed": "passed",
    "failed": "failed",
    "error": "broken",
    "skipped": "skipped",
}


def _to_epoch_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def write_allure_results(report: RunReport, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    framework = report.run_results.get("framework", "unknown")
    started = _to_epoch_ms(report.run_results.get("started_at"))
    written: list[Path] = []

    for r in report.run_results.get("results", []):
        test_id = r.get("id", "")
        name = r.get("name", "")
        file = r.get("file", "")
        status = _STATUS_MAP.get(r.get("status", ""), "unknown")

        history_id = hashlib.md5(test_id.encode()).hexdigest()  # noqa: S324
        result_uuid = str(uuid.uuid4())

        dur_ms = int((r.get("duration_s") or 0) * 1000)
        start_ms = started or 0
        stop_ms = start_ms + dur_ms

        status_details = {}
        msg = r.get("message")
        tb = r.get("traceback")
        if msg:
            status_details["message"] = msg
        if tb:
            status_details["trace"] = tb

        allure_result = {
            "uuid": result_uuid,
            "historyId": history_id,
            "name": name,
            "fullName": f"{file}::{name}" if file else name,
            "status": status,
            "start": start_ms,
            "stop": stop_ms,
            "labels": [
                {"name": "framework", "value": framework},
                {"name": "suite", "value": file},
            ],
        }
        if status_details:
            allure_result["statusDetails"] = status_details

        path = out_dir / f"{result_uuid}-result.json"
        path.write_text(json.dumps(allure_result, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path)

    return written


def generate_allure_html(results_dir: Path, html_dir: Path) -> bool:
    # On Windows `allure` is installed as `allure.bat` / `allure.cmd`. Without
    # shell=True, subprocess.run(["allure", ...]) fails with [WinError 2] —
    # CreateProcess doesn't auto-append extensions. shutil.which() resolves
    # the full path including extension on every platform.
    allure_bin = shutil.which("allure")
    if not allure_bin:
        log.info("report.allure_not_found")
        return False
    try:
        proc = subprocess.run(
            [allure_bin, "generate", str(results_dir), "-o", str(html_dir), "--clean"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=with_proxy_env(),
        )
        if proc.returncode != 0:
            log.warning("report.allure_generate_failed", stderr=proc.stderr[:500])
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("report.allure_generate_error", error=str(e))
        return False
