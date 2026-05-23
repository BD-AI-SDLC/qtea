"""Step 1 intake tests: local file, URL, jira agent path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s01_intake import IntakeStep
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude


def _ctx(tmp_path: Path, spec_source: str) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source=spec_source, sut_source=".")
    opts = PipelineOptions(spec=spec_source, sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source=spec_source, sut_source=".", options=opts)


def test_intake_local_file_copy(tmp_path: Path):
    src = tmp_path / "input.md"
    src.write_text("# Hello\n\nLocal spec.", encoding="utf-8")
    ctx = _ctx(tmp_path, str(src))

    result = IntakeStep().run(ctx)

    assert result.success is True
    assert result.status == "completed"
    spec = ctx.workspace.step_dir(1) / "spec.md"
    assert spec.exists()
    assert "Local spec." in spec.read_text(encoding="utf-8")
    assert (ctx.workspace.step_dir(1) / "jira-spec.md").exists()


def test_intake_missing_local_file_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, str(tmp_path / "nope.md"))
    result = IntakeStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    assert "not found" in (result.error or "").lower()


def test_intake_url_downloads(tmp_path: Path):
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
        result = IntakeStep().run(ctx)

    assert result.success
    spec = ctx.workspace.step_dir(1) / "spec.md"
    assert "Remote" in spec.read_text(encoding="utf-8")


def test_intake_jira_via_agent(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(
        bin_dir,
        events=[{"type": "result", "result": "ok"}],
        files={
            "spec.md": "# REQ-PROJ-1\n\nFrom Jira\n",
            "jira-spec.md": "# Raw\n\nticket dump\n",
        },
    )
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path, "jira:PROJ-1")
    result = IntakeStep().run(ctx)

    assert result.success, result.error
    spec = ctx.workspace.step_dir(1) / "spec.md"
    jira = ctx.workspace.step_dir(1) / "jira-spec.md"
    assert "From Jira" in spec.read_text(encoding="utf-8")
    assert "ticket dump" in jira.read_text(encoding="utf-8")


def test_intake_jira_agent_no_output_fails(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(bin_dir, events=[{"type": "result", "result": "ok"}], files={})
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path, "jira:PROJ-2")
    result = IntakeStep().run(ctx)
    assert not result.success
    assert "spec.md" in (result.error or "")


def test_intake_jira_empty_ticket_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, "jira:")
    result = IntakeStep().run(ctx)
    assert not result.success
    assert "ticket" in (result.error or "").lower() or "missing" in (result.error or "").lower()
