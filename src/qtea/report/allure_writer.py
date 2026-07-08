"""Allure 2.x compatible result writer."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.proxy import with_proxy_env
from qtea.report.data_builder import RunReport

log = get_logger(__name__)

_STATUS_MAP = {
    "passed": "passed",
    "failed": "failed",
    "error": "broken",
    "skipped": "skipped",
}

_MIME_BY_TYPE = {
    "screenshot": "image/png",
    "trace": "application/zip",
    "video": "video/webm",
    "log": "text/plain",
}

_FAILURE_STATUSES = frozenset({"failed", "broken"})


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

        allure_attachments: list[dict] = []
        if status in _FAILURE_STATUSES:
            for a in r.get("attachments") or []:
                a_path = a.get("path", "")
                a_type = a.get("type", "other")
                if not a_path:
                    continue
                src = Path(a_path)
                if not src.is_file():
                    continue
                dest_name = f"{result_uuid}-{src.name}"
                try:
                    shutil.copy2(src, out_dir / dest_name)
                except OSError:
                    continue
                mime = _MIME_BY_TYPE.get(a_type, "application/octet-stream")
                allure_attachments.append({
                    "name": a_type,
                    "source": dest_name,
                    "type": mime,
                })

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
        if allure_attachments:
            allure_result["attachments"] = allure_attachments

        path = out_dir / f"{result_uuid}-result.json"
        path.write_text(json.dumps(allure_result, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path)

    return written


def resolve_allure_cmd() -> list[str] | None:
    """Return the allure invocation to use.

    Prefers a system-installed binary; falls back to npx (already a hard
    prerequisite of qtea) so Allure works out of the box without a separate
    global install. Returns None only if neither is available.
    """
    # On Windows allure ships as allure.bat / allure.cmd — shutil.which
    # resolves the full path including extension on every platform.
    allure_bin = shutil.which("allure")
    if allure_bin:
        return [allure_bin]
    npx_bin = shutil.which("npx")
    if npx_bin:
        return [npx_bin, "--yes", "allure-commandline"]
    return None


def generate_allure_html(results_dir: Path, html_dir: Path) -> bool:
    cmd = resolve_allure_cmd()
    if not cmd:
        log.info("report.allure_not_found")
        return False
    try:
        proc = subprocess.run(
            cmd + ["generate", str(results_dir), "-o", str(html_dir), "--clean"],
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
