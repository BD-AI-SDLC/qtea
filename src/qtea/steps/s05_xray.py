"""Step 5: Upload test cases from test-design.json to Xray Cloud.

Auto-skips when Xray credentials (JIRA_XRAY_CLIENT_ID + JIRA_XRAY_CLIENT_SECRET,
or JIRA_XRAY_API_KEY) are not set. Uses direct HTTPS via httpx with proxy
support; the Atlassian MCP server is NOT required for this step.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from qtea.config import step_timeout
from qtea.logging_setup import get_logger
from qtea.proxy import with_proxy_env
from qtea.schemas import write_validated
from qtea.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)

XRAY_CLOUD_BASE = "https://xray.cloud.getxray.app/api/v2"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("step05.load_failed", path=str(path), error=str(e))
        return None


def _extract_project_key(spec_source: str) -> str | None:
    env_key = os.environ.get("JIRA_PROJECT_KEY")
    if env_key:
        return env_key
    m = re.match(r"jira:([A-Z][A-Z0-9]+)-\d+", spec_source, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _skipped_mapping(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason}


def _proxy_url() -> str | None:
    env = with_proxy_env()
    return env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or None


class XrayClient:
    """Minimal Xray Cloud REST API v2 client."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_key: str | None = None,
        base_url: str = XRAY_CLOUD_BASE,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_key = api_key
        self._base_url = base_url
        self._token: str | None = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def authenticate(self) -> str:
        if self._api_key:
            self._token = self._api_key
            return self._token
        proxy = _proxy_url()
        with httpx.Client(timeout=30.0, proxy=proxy) as client:
            resp = client.post(
                f"{self._base_url}/authenticate",
                json={"client_id": self._client_id, "client_secret": self._client_secret},
            )
            resp.raise_for_status()
            token = resp.text.strip().strip('"')
            self._token = token
            return token

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def import_tests(self, project_key: str, tests: list[dict]) -> list[dict]:
        if not self._token:
            self.authenticate()
        proxy = _proxy_url()
        with httpx.Client(timeout=60.0, proxy=proxy) as client:
            resp = client.post(
                f"{self._base_url}/import/test/bulk",
                json={"projectKey": project_key, "tests": tests},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            return resp.json()


def _build_xray_tests(test_cases: list[dict]) -> list[dict]:
    xray_tests: list[dict] = []
    for tc in test_cases:
        steps = []
        for i, s in enumerate(tc.get("steps") or [], start=1):
            steps.append({"action": s, "result": "", "index": i})
        xray_test: dict[str, Any] = {
            "testtype": "Manual",
            "summary": f"[{tc['id']}] {tc.get('title', '')}",
            "priority": _map_priority(tc.get("priority")),
        }
        if steps:
            xray_test["steps"] = steps
        if tc.get("preconditions"):
            xray_test["precondition"] = "; ".join(tc["preconditions"])
        xray_tests.append(xray_test)
    return xray_tests


def _map_priority(priority: str | None) -> str:
    mapping = {"P0": "Highest", "P1": "High", "P2": "Medium", "P3": "Low"}
    return mapping.get(priority or "", "Medium")


def _build_mappings(
    test_cases: list[dict],
    response: list[dict],
) -> list[dict]:
    mappings: list[dict] = []
    for i, tc in enumerate(test_cases):
        if i < len(response):
            entry = response[i]
            key = entry.get("key") or entry.get("self") or entry.get("id")
            mappings.append({
                "tc_id": tc["id"],
                "xray_key": str(key) if key else None,
                "status": "created" if key else "failed",
            })
        else:
            mappings.append({
                "tc_id": tc["id"],
                "xray_key": None,
                "status": "failed",
                "error": "no response entry",
            })
    return mappings


def _skip_result(mapping_path: Path, reason: str, notes: str) -> StepResult:
    write_validated(mapping_path, _skipped_mapping(reason), "xray-mapping")
    return StepResult(success=True, status="skipped", outputs=[mapping_path], notes=notes)


def _error_result(
    mapping_path: Path, phase: str, exc: Exception, strict: bool,
) -> StepResult:
    status = "failed" if strict else "warned"
    write_validated(
        mapping_path,
        {"status": status, "reason": f"{phase} failed: {exc}"},
        "xray-mapping",
    )
    if strict:
        return StepResult(
            success=False, status="failed", outputs=[mapping_path],
            error=f"Xray {phase} failed: {exc}",
        )
    return StepResult(
        success=True, status="warned", outputs=[mapping_path],
        notes=f"xray {phase} failed: {exc}",
    )


class XrayUploadStep(Step):
    number = 5
    name = "xray-upload"
    timeout_s = step_timeout(5)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping_path = out_dir / "xray-mapping.json"

        client_id = os.environ.get("JIRA_XRAY_CLIENT_ID")
        client_secret = os.environ.get("JIRA_XRAY_CLIENT_SECRET")
        api_key = os.environ.get("JIRA_XRAY_API_KEY")

        has_oauth = bool(client_id and client_secret)
        has_api_key = bool(api_key)

        if not has_oauth and not has_api_key:
            return _skip_result(
                mapping_path, "no JIRA_XRAY credentials set",
                "xray credentials not configured; skipped",
            )

        strategy = _load_json(ctx.workspace.step_dir(4) / "test-design.json")
        if not strategy:
            return _skip_result(
                mapping_path, "test-design.json not found",
                "step 4 outputs missing; skipped",
            )

        test_cases = strategy.get("test_cases", [])
        if not test_cases:
            return _skip_result(mapping_path, "no test cases in strategy", "no test cases; skipped")

        project_key = _extract_project_key(ctx.spec_source)
        if not project_key:
            project_key = "DEFAULT"
            log.warning("step05.no_project_key", fallback=project_key)

        client = XrayClient(
            client_id=client_id, client_secret=client_secret, api_key=api_key,
        )

        try:
            client.authenticate()
        except Exception as exc:
            log.error("step05.auth_failed", error=str(exc))
            return _error_result(mapping_path, "auth", exc, ctx.options.strict_xray)

        xray_tests = _build_xray_tests(test_cases)
        try:
            response = client.import_tests(project_key, xray_tests)
        except Exception as exc:
            log.error("step05.import_failed", error=str(exc))
            return _error_result(mapping_path, "import", exc, ctx.options.strict_xray)

        if not isinstance(response, list):
            response = []

        mappings = _build_mappings(test_cases, response)
        failed_count = sum(1 for m in mappings if m["status"] == "failed")

        if failed_count > 0 and ctx.options.strict_xray:
            result_status = "failed"
            step_success = False
        elif failed_count > 0:
            result_status = "warned"
            step_success = True
        else:
            result_status = "completed"
            step_success = True

        result_data: dict[str, Any] = {
            "status": result_status,
            "uploaded_at": datetime.now(UTC).isoformat(),
            "project_key": project_key,
            "mappings": mappings,
        }
        write_validated(mapping_path, result_data, "xray-mapping")

        return StepResult(
            success=step_success,
            status=result_status,
            outputs=[mapping_path],
            notes=f"uploaded={len(test_cases)} failed={failed_count}",
            error=f"{failed_count} test(s) failed to upload" if not step_success else None,
        )
