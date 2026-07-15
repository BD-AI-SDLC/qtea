"""Step 5 Xray upload tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.schemas import is_valid
from qtea.steps.base import StepContext
from qtea.steps.s05_xray import (
    XrayClient,
    XrayUploadStep,
    _build_mappings,
    _build_xray_tests,
    _extract_project_key,
    _map_priority,
)
from qtea.workspace import create_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **opts_kw) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    defaults = {"spec": "x", "sut": ".", "workspace_base": tmp_path / ".ws"}
    defaults.update(opts_kw)
    opts = PipelineOptions(**defaults)
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _sample_strategy():
    return {
        "title": "Login tests",
        "test_cases": [
            {"id": "TC-login-01", "title": "User can log in", "priority": "P1",
             "steps": ["Open login page", "Enter credentials", "Click submit"],
             "preconditions": ["User exists"]},
            {"id": "TC-login-02", "title": "Invalid password shows error", "priority": "P2",
             "steps": ["Open login page", "Enter wrong password"]},
        ],
    }


def _seed_strategy(ctx: StepContext, strategy=None):
    s4 = ctx.workspace.step_dir(4)
    s4.mkdir(parents=True, exist_ok=True)
    (s4 / "test-design.json").write_text(
        json.dumps(strategy or _sample_strategy()), encoding="utf-8",
    )


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=MagicMock(), response=MagicMock(status_code=self.status_code),
            )

    def json(self):
        return self._json


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_extract_project_key_from_jira_spec():
    assert _extract_project_key("jira:PROJ-123") == "PROJ"
    assert _extract_project_key("jira:ABC-1") == "ABC"


def test_extract_project_key_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "MYPROJ")
    assert _extract_project_key("some-spec.md") == "MYPROJ"


def test_extract_project_key_returns_none_for_local_spec(monkeypatch):
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    assert _extract_project_key("spec.md") is None


def test_map_priority():
    assert _map_priority("P0") == "Highest"
    assert _map_priority("P1") == "High"
    assert _map_priority("P2") == "Medium"
    assert _map_priority("P3") == "Low"
    assert _map_priority(None) == "Medium"


def test_build_xray_tests():
    tcs = _sample_strategy()["test_cases"]
    xray = _build_xray_tests(tcs)
    assert len(xray) == 2
    assert "[TC-login-01]" in xray[0]["summary"]
    assert xray[0]["priority"] == "High"
    assert len(xray[0]["steps"]) == 3
    assert "precondition" in xray[0]


def test_build_mappings_success():
    tcs = [{"id": "TC-1"}, {"id": "TC-2"}]
    resp = [{"key": "PROJ-10"}, {"key": "PROJ-11"}]
    mappings = _build_mappings(tcs, resp)
    assert len(mappings) == 2
    assert mappings[0]["xray_key"] == "PROJ-10"
    assert mappings[0]["status"] == "created"


def test_build_mappings_partial_failure():
    tcs = [{"id": "TC-1"}, {"id": "TC-2"}]
    resp = [{"key": "PROJ-10"}]
    mappings = _build_mappings(tcs, resp)
    assert mappings[0]["status"] == "created"
    assert mappings[1]["status"] == "failed"
    assert mappings[1]["xray_key"] is None


# ---------------------------------------------------------------------------
# Auto-skip tests
# ---------------------------------------------------------------------------


async def test_step05_skips_when_no_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("JIRA_XRAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("JIRA_XRAY_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("JIRA_XRAY_API_KEY", raising=False)
    ctx = _ctx(tmp_path)
    result = await XrayUploadStep().run(ctx)
    assert result.success
    assert result.status == "skipped"
    out = ctx.workspace.step_dir(5) / "xray-mapping.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "skipped"
    ok, err = is_valid(data, "xray-mapping")
    assert ok, err


async def test_step05_skips_when_only_client_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_CLIENT_ID", "id-only")
    monkeypatch.delenv("JIRA_XRAY_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("JIRA_XRAY_API_KEY", raising=False)
    ctx = _ctx(tmp_path)
    result = await XrayUploadStep().run(ctx)
    assert result.status == "skipped"


async def test_step05_skips_when_strategy_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_API_KEY", "test-key")
    ctx = _ctx(tmp_path)
    result = await XrayUploadStep().run(ctx)
    assert result.status == "skipped"
    assert "step 4" in (result.notes or "")


# ---------------------------------------------------------------------------
# XrayClient tests
# ---------------------------------------------------------------------------


def test_xray_client_auth_with_api_key():
    client = XrayClient(api_key="pre-generated-token")
    token = client.authenticate()
    assert token == "pre-generated-token"


def test_xray_client_auth_via_oauth(monkeypatch):
    def mock_post(self, url, **kwargs):
        return _FakeResponse(200, text='"bearer-token-123"')

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    client = XrayClient(client_id="cid", client_secret="csec")
    token = client.authenticate()
    assert token == "bearer-token-123"


def test_xray_client_auth_failure(monkeypatch):
    def mock_post(self, url, **kwargs):
        return _FakeResponse(401)

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    client = XrayClient(client_id="cid", client_secret="csec")
    try:
        client.authenticate.retry.retry = lambda *a, **k: False
    except Exception:
        pass
    # With retries disabled, auth should fail
    raised = False
    try:
        client.authenticate()
    except httpx.HTTPStatusError:
        raised = True
    assert raised


def test_xray_client_import_success(monkeypatch):
    call_count = {"auth": 0, "import": 0}

    def mock_post(self, url, **kwargs):
        if "authenticate" in url:
            call_count["auth"] += 1
            return _FakeResponse(200, text='"token"')
        call_count["import"] += 1
        return _FakeResponse(200, json_data=[{"key": "PROJ-1"}, {"key": "PROJ-2"}])

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    client = XrayClient(client_id="cid", client_secret="csec")
    client.authenticate()
    resp = client.import_tests("PROJ", [{"summary": "t1"}, {"summary": "t2"}])
    assert len(resp) == 2
    assert resp[0]["key"] == "PROJ-1"


# ---------------------------------------------------------------------------
# Step integration tests
# ---------------------------------------------------------------------------


async def test_step05_happy_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_API_KEY", "test-key")
    monkeypatch.delenv("JIRA_XRAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("JIRA_XRAY_CLIENT_SECRET", raising=False)

    def mock_post(self, url, **kwargs):
        if "import" in url:
            return _FakeResponse(200, json_data=[{"key": "PROJ-10"}, {"key": "PROJ-11"}])
        return _FakeResponse(200, text='"token"')

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    ctx = _ctx(tmp_path, spec="jira:PROJ-42")
    ctx = StepContext(
        workspace=ctx.workspace, state=ctx.state,
        spec_source="jira:PROJ-42", sut_source=".", options=ctx.options,
    )
    _seed_strategy(ctx)

    result = await XrayUploadStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    out = ctx.workspace.step_dir(5)
    data = json.loads((out / "xray-mapping.json").read_text(encoding="utf-8"))
    ok, err = is_valid(data, "xray-mapping")
    assert ok, err
    assert data["project_key"] == "PROJ"
    assert len(data["mappings"]) == 2
    assert all(m["status"] == "created" for m in data["mappings"])


async def test_step05_auth_failure_strict(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_CLIENT_ID", "cid")
    monkeypatch.setenv("JIRA_XRAY_CLIENT_SECRET", "csec")
    monkeypatch.delenv("JIRA_XRAY_API_KEY", raising=False)

    def mock_post(self, url, **kwargs):
        return _FakeResponse(401)

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    ctx = _ctx(tmp_path, strict_xray=True)
    _seed_strategy(ctx)

    result = await XrayUploadStep().run(ctx)
    assert not result.success
    assert result.status == "failed"


async def test_step05_auth_failure_non_strict(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_CLIENT_ID", "cid")
    monkeypatch.setenv("JIRA_XRAY_CLIENT_SECRET", "csec")
    monkeypatch.delenv("JIRA_XRAY_API_KEY", raising=False)

    def mock_post(self, url, **kwargs):
        return _FakeResponse(401)

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    ctx = _ctx(tmp_path)
    _seed_strategy(ctx)

    result = await XrayUploadStep().run(ctx)
    assert result.success
    assert result.status == "warned"


async def test_step05_partial_failure_strict(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JIRA_XRAY_API_KEY", "test-key")
    monkeypatch.delenv("JIRA_XRAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("JIRA_XRAY_CLIENT_SECRET", raising=False)

    def mock_post(self, url, **kwargs):
        if "import" in url:
            return _FakeResponse(200, json_data=[{"key": "PROJ-10"}])
        return _FakeResponse(200, text='"token"')

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    ctx = _ctx(tmp_path, strict_xray=True, spec="jira:PROJ-1")
    ctx = StepContext(
        workspace=ctx.workspace, state=ctx.state,
        spec_source="jira:PROJ-1", sut_source=".", options=ctx.options,
    )
    _seed_strategy(ctx)

    result = await XrayUploadStep().run(ctx)
    assert not result.success
    assert result.status == "failed"
