"""Step 1 intake tests: local file, URL, jira REST path (pure-code transport)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s01_intake import IntakeStep
from worca_t.workspace import create_workspace


def _ctx(tmp_path: Path, spec_source: str) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source=spec_source, sut_source=".",
    )
    opts = PipelineOptions(spec=spec_source, sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(
        workspace=ws, state=state, spec_source=spec_source,
        sut_source=".", options=opts,
    )


# ---------------------------------------------------------------------------
# Local file path (pure code — unchanged by migration)
# ---------------------------------------------------------------------------


async def test_intake_local_file_copy(tmp_path: Path):
    src = tmp_path / "input.md"
    src.write_text("# Hello\n\nLocal spec.", encoding="utf-8")
    ctx = _ctx(tmp_path, str(src))

    result = await IntakeStep().run(ctx)

    assert result.success is True
    assert result.status == "completed"
    spec = ctx.workspace.step_dir(1) / "spec.md"
    assert spec.exists()
    assert "Local spec." in spec.read_text(encoding="utf-8")
    assert (ctx.workspace.step_dir(1) / "jira-spec.md").exists()


async def test_intake_missing_local_file_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, str(tmp_path / "nope.md"))
    result = await IntakeStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    assert "not found" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Generic URL download (pure code — unchanged by migration)
# ---------------------------------------------------------------------------


async def test_intake_url_downloads(tmp_path: Path):
    ctx = _ctx(tmp_path, "https://example.invalid/spec.md")

    class FakeResp:
        text = "# Remote\n\nbody"

        def raise_for_status(self): ...

    class FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): ...
        def get(self, url): return FakeResp()

    with patch("worca_t.steps.s01_intake.httpx.Client", FakeClient):
        result = await IntakeStep().run(ctx)

    assert result.success
    spec = ctx.workspace.step_dir(1) / "spec.md"
    assert "Remote" in spec.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# JIRA path: deterministic REST → markdown (no LLM call)
# ---------------------------------------------------------------------------


def _fake_jira_payload() -> dict:
    """Minimal Atlassian Cloud REST v3 payload shape."""
    return {
        "key": "PROJ-1",
        "fields": {
            "summary": "Sample issue",
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "Sample description body."},
                    ]},
                ],
            },
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Story"},
            "reporter": {"displayName": "Alice"},
            "labels": ["frontend"],
        },
    }


async def test_intake_jira_via_rest_shorthand(tmp_path: Path, monkeypatch):
    """jira:KEY shorthand uses JIRA_BASE_URL, fetches via REST, renders deterministically."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://bosch-pt.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "user@bosch.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    # Mock fetch_issue at the module boundary used by s01_intake.
    monkeypatch.setattr(
        "worca_t.steps.s01_intake.fetch_issue",
        lambda base_url, ticket_id: _fake_jira_payload(),
    )

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    spec_path = ctx.workspace.step_dir(1) / "spec.md"
    spec_text = spec_path.read_text(encoding="utf-8")
    # Deterministic renderer produces structured headings + content from
    # the JIRA payload.
    assert "Sample issue" in spec_text
    assert "PROJ-1" in spec_text
    assert "Sample description body." in spec_text
    assert "## Description" in spec_text

    # Provenance stub records the source.
    jira_text = (ctx.workspace.step_dir(1) / "jira-spec.md").read_text(encoding="utf-8")
    assert "PROJ-1" in jira_text
    assert "bosch-pt.atlassian.net" in jira_text


async def test_intake_jira_via_rest_url_form(tmp_path: Path, monkeypatch):
    """Full URL form takes base_url from the URL itself, not from env."""
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.setenv("JIRA_EMAIL", "user@bosch.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    captured_args = {}

    def _fake_fetch(base_url, ticket_id):
        captured_args["base_url"] = base_url
        captured_args["ticket_id"] = ticket_id
        return _fake_jira_payload()

    monkeypatch.setattr("worca_t.steps.s01_intake.fetch_issue", _fake_fetch)

    ctx = _ctx(tmp_path, "https://bosch-pt.atlassian.net/browse/MEAS-5490")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    # base_url is extracted from the URL host, NOT from JIRA_BASE_URL.
    assert captured_args["base_url"] == "https://bosch-pt.atlassian.net"
    assert captured_args["ticket_id"] == "MEAS-5490"

    # spec.md records the source URL in the provenance block.
    spec_text = (ctx.workspace.step_dir(1) / "spec.md").read_text(encoding="utf-8")
    assert "https://bosch-pt.atlassian.net/browse/MEAS-5490" in spec_text


async def test_intake_jira_via_rest_dc_url(tmp_path: Path, monkeypatch):
    """DC URL preserves context path in base_url passed to fetch_issue."""
    monkeypatch.setenv("JIRA_PAT", "pat")

    captured_args = {}

    def _fake_fetch(base_url, ticket_id):
        captured_args["base_url"] = base_url
        return _fake_jira_payload()

    monkeypatch.setattr("worca_t.steps.s01_intake.fetch_issue", _fake_fetch)

    ctx = _ctx(tmp_path, "https://rb-tracker.bosch.com/tracker01/browse/DXFAA-14642")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    # Context path preserved.
    assert captured_args["base_url"] == "https://rb-tracker.bosch.com/tracker01"


async def test_intake_jira_no_llm_call(tmp_path: Path, monkeypatch):
    """Regression guard: Step 1 must not invoke the Anthropic SDK.

    The whole point of this simplification is making Step 1 LLM-free.
    If someone re-introduces a call_reasoning_llm import, this test fails.
    """
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(
        "worca_t.steps.s01_intake.fetch_issue",
        lambda *_a, **_kw: _fake_jira_payload(),
    )

    # Tripwire: if anything tries to construct an Anthropic client, blow up.
    def _no_anthropic_calls(*_a, **_kw):
        raise AssertionError(
            "Step 1 must not call anthropic.AsyncAnthropic — "
            "the LLM path was deliberately removed in the simplification."
        )

    monkeypatch.setattr("anthropic.AsyncAnthropic", _no_anthropic_calls)
    monkeypatch.setattr("anthropic.AsyncAnthropicVertex", _no_anthropic_calls)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)
    assert result.success, result.error


async def test_intake_jira_shorthand_without_base_url_fails(tmp_path: Path, monkeypatch):
    """jira:KEY without JIRA_BASE_URL set raises a helpful error."""
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)
    assert not result.success
    assert "JIRA_BASE_URL" in (result.error or "")


async def test_intake_jira_empty_ticket_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, "jira:")
    result = await IntakeStep().run(ctx)
    assert not result.success
    assert "ticket" in (result.error or "").lower() or "missing" in (result.error or "").lower()


async def test_intake_jira_fetch_failure_propagates(tmp_path: Path, monkeypatch):
    """A JiraFetchError (auth / 404 / network) is surfaced as step failure."""
    from worca_t.jira_client import JiraFetchError
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")

    def _raise(*_a, **_kw):
        raise JiraFetchError("token expired", status_code=401)

    monkeypatch.setattr("worca_t.steps.s01_intake.fetch_issue", _raise)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)
    assert not result.success
    assert "token expired" in (result.error or "")
