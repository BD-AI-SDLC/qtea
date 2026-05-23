"""Step 2 refine tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s02_refine import RefineStep, _project_to_json
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude

REFINED_MD = """\
# Login Feature

Requirement ID: REQ-login-feature

## Acceptance Criteria

- User can sign in with valid credentials
- Invalid credentials show error

## User Flow

step 1, step 2

## Test Boundaries

in/out scope

## Edge Cases

- empty password

## Definition of Ready

READY
"""


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    # Seed step 1 output.
    (ws.step_dir(1) / "spec.md").write_text("# Login\n\nstub", encoding="utf-8")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def test_project_to_json_extracts_req_id_and_ac():
    proj = _project_to_json(REFINED_MD)
    assert proj["requirement_id"] == "REQ-login-feature"
    assert proj["title"] == "Login Feature"
    assert "User can sign in with valid credentials" in proj["acceptance_criteria"]
    assert proj["user_flows"] is not None
    assert proj["edge_cases"] is not None


def test_project_falls_back_to_slug_when_no_req_id():
    proj = _project_to_json("# Some Feature\n\nno req here\n\n## Acceptance Criteria\n\n- ok\n")
    assert proj["requirement_id"].startswith("REQ-")
    assert "some-feature" in proj["requirement_id"]


def test_refine_step_writes_md_and_validated_json(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(
        bin_dir,
        events=[{"type": "result", "result": "ok"}],
        files={"refined-spec.md": REFINED_MD},
    )
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = RefineStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"  # schema must be valid for our projection

    out = ctx.workspace.step_dir(2)
    assert (out / "refined-spec.md").exists()
    payload = json.loads((out / "refined-spec.json").read_text(encoding="utf-8"))
    assert payload["requirement_id"] == "REQ-login-feature"
    assert payload["acceptance_criteria"]


def test_refine_step_missing_spec_fails(tmp_path: Path):
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    ctx = StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)
    result = RefineStep().run(ctx)
    assert not result.success
    assert "missing" in (result.error or "").lower()


def test_refine_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(bin_dir, events=[{"type": "result", "result": "ok"}], files={})
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = RefineStep().run(ctx)
    assert not result.success
    assert "refined-spec.md" in (result.error or "")
