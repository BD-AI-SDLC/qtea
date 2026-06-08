"""Step 1 intake tests: local file, URL, jira REST path (direct-SDK transport)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s01_intake import IntakeStep
from worca_t.workspace import create_workspace

from ._fake_anthropic import install_fake_anthropic


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
# JIRA path: jira:KEY shorthand → direct REST + reasoning agent
# ---------------------------------------------------------------------------


_AGENT_SPEC_MD = (
    "# Jira Ticket: PROJ-1\n\n## 1. Overview\n\n### 1.1 Summary\nFrom Jira\n"
)


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
        },
    }


async def test_intake_jira_via_rest_shorthand(tmp_path: Path, monkeypatch):
    """jira:KEY shorthand uses JIRA_BASE_URL, fetches via REST, reformats via agent."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://bosch-pt.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "user@bosch.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    # Mock fetch_issue at the module boundary used by s01_intake.
    monkeypatch.setattr(
        "worca_t.steps.s01_intake.fetch_issue",
        lambda base_url, ticket_id: _fake_jira_payload(),
    )
    install_fake_anthropic(monkeypatch, text=_AGENT_SPEC_MD)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    spec = ctx.workspace.step_dir(1) / "spec.md"
    jira = ctx.workspace.step_dir(1) / "jira-spec.md"
    assert "From Jira" in spec.read_text(encoding="utf-8")
    # Provenance stub records the source.
    jira_text = jira.read_text(encoding="utf-8")
    assert "PROJ-1" in jira_text
    assert "bosch-pt.atlassian.net" in jira_text
    assert "not retained" in jira_text


async def test_intake_jira_via_rest_url_form(tmp_path: Path, monkeypatch):
    """Full URL form takes base_url from the URL itself, not from env."""
    # Deliberately NOT setting JIRA_BASE_URL — URL form must self-describe.
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.setenv("JIRA_EMAIL", "user@bosch.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    captured_args = {}

    def _fake_fetch(base_url, ticket_id):
        captured_args["base_url"] = base_url
        captured_args["ticket_id"] = ticket_id
        return _fake_jira_payload()

    monkeypatch.setattr("worca_t.steps.s01_intake.fetch_issue", _fake_fetch)
    install_fake_anthropic(monkeypatch, text=_AGENT_SPEC_MD)

    ctx = _ctx(tmp_path, "https://bosch-pt.atlassian.net/browse/MEAS-5490")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    # base_url is extracted from the URL host, NOT from JIRA_BASE_URL.
    assert captured_args["base_url"] == "https://bosch-pt.atlassian.net"
    assert captured_args["ticket_id"] == "MEAS-5490"


async def test_intake_jira_via_rest_dc_url(tmp_path: Path, monkeypatch):
    """DC URL preserves context path in base_url passed to fetch_issue."""
    monkeypatch.setenv("JIRA_PAT", "pat")

    captured_args = {}

    def _fake_fetch(base_url, ticket_id):
        captured_args["base_url"] = base_url
        return _fake_jira_payload()

    monkeypatch.setattr("worca_t.steps.s01_intake.fetch_issue", _fake_fetch)
    install_fake_anthropic(monkeypatch, text=_AGENT_SPEC_MD)

    ctx = _ctx(tmp_path, "https://rb-tracker.bosch.com/tracker01/browse/DXFAA-14642")
    result = await IntakeStep().run(ctx)

    assert result.success, result.error
    # Context path preserved.
    assert captured_args["base_url"] == "https://rb-tracker.bosch.com/tracker01"


async def test_intake_jira_inlines_payload_into_user_prompt(
    tmp_path: Path, monkeypatch
):
    """The fetched JIRA payload must reach the LLM via inlined inputs."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    payload = _fake_jira_payload()
    payload["fields"]["summary"] = "PAYLOAD_INLINE_MARKER_XYZ"
    monkeypatch.setattr(
        "worca_t.steps.s01_intake.fetch_issue",
        lambda base_url, ticket_id: payload,
    )

    captured: dict = {}
    install_fake_anthropic(monkeypatch, text=_AGENT_SPEC_MD, on_call=captured.update)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = await IntakeStep().run(ctx)
    assert result.success

    user_content = captured["messages"][-1]["content"]
    assert "PAYLOAD_INLINE_MARKER_XYZ" in user_content
    assert "jira-issue.json" in user_content


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


async def test_intake_jira_agent_no_output_fails(tmp_path: Path, monkeypatch):
    """Reasoning agent returning empty text marks the step as failed."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(
        "worca_t.steps.s01_intake.fetch_issue",
        lambda *_a, **_kw: _fake_jira_payload(),
    )
    install_fake_anthropic(monkeypatch, text="")

    ctx = _ctx(tmp_path, "jira:PROJ-2")
    result = await IntakeStep().run(ctx)
    assert not result.success
    assert "jira-to-ai-spec failed" in (result.error or "") or "no output" in (result.error or "")
