"""Step 4 test-strategy tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.md_parser import parse_markdown
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s04_strategy import (
    StrategyStep,
    _looks_like_test_case,
    _project_strategy,
    _project_test_case,
)
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude

STRATEGY_MD = """\
# Test Strategy: Login

## Scope

Cover login UI and API for the Login feature.

## Objectives

- Validate happy path
- Validate error handling

## Test Types

- Functional
- Negative

## Risks

- Account lockout

## Test Cases

### TC-LOGIN-001: Successful login with valid credentials

- **Priority**: P0
- **Type**: Functional
- **Preconditions**:
  - User exists in the database
  - App is reachable
- **Steps**:
  - Navigate to /login
  - Enter valid credentials
  - Click Submit
- **Expected**: User is redirected to /dashboard
- **Tags**: smoke, auth

### TC-LOGIN-002: Invalid password shows error

- **Priority**: P1
- **Type**: Negative
- **Steps**:
  - Navigate to /login
  - Enter invalid password
  - Click Submit
- **Expected**: Error message "Invalid credentials" is shown

### Scenario: missing id should still parse

- **Priority**: P2
- **Steps**:
  - do something

## Edge Cases

- Empty username
- Very long password

## Exit Criteria

- All P0 cases pass
- No P0/P1 defects open
"""


def test_looks_like_test_case_detects_tc_id_and_keywords():
    root = parse_markdown(STRATEGY_MD)
    titles = [s.title for s in root.walk() if _looks_like_test_case(s)]
    assert "TC-LOGIN-001: Successful login with valid credentials" in titles
    assert any("Scenario" in t for t in titles)


def test_project_test_case_parses_fields():
    root = parse_markdown(STRATEGY_MD)
    sec = next(s for s in root.walk() if "TC-LOGIN-001" in s.title)
    tc = _project_test_case(sec)
    assert tc["id"] == "TC-LOGIN-001"
    assert tc["priority"] == "P0"
    assert tc["type"] == "Functional"
    assert "User exists in the database" in tc["preconditions"]
    assert "Navigate to /login" in tc["steps"]
    assert "redirected to /dashboard" in tc["expected"]
    assert "smoke" in tc["tags"]


def test_project_strategy_full_shape():
    proj = _project_strategy(STRATEGY_MD)
    assert proj["title"] == "Test Strategy: Login"
    assert "happy path" in (proj["objectives"][0])
    assert "Functional" in proj["types"]
    assert "Account lockout" in proj["risks"]
    ids = [tc["id"] for tc in proj["test_cases"]]
    assert "TC-LOGIN-001" in ids
    assert "TC-LOGIN-002" in ids
    # auto-generated id for the scenario without a TC- prefix
    assert any(tc["id"].startswith("TC-") and "scenario" in tc["id"] for tc in proj["test_cases"])
    assert "Empty username" in proj["edge_cases"]
    assert any("P0" in c for c in proj["exit_criteria"])


def test_project_handles_duplicate_ids():
    md = """\
# T

## TC-DUP-1
- **Priority**: P1

## TC-DUP-1
- **Priority**: P2
"""
    proj = _project_strategy(md)
    ids = [tc["id"] for tc in proj["test_cases"]]
    assert ids == ["TC-DUP-1", "TC-DUP-1-2"]


def _ctx(tmp_path: Path, *, with_plan: bool = True) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if with_plan:
        (ws.step_dir(3) / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (ws.step_dir(2) / "refined-spec.md").write_text("# refined\n", encoding="utf-8")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def test_strategy_step_writes_md_and_validated_json(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(
        bin_dir,
        events=[{"type": "result", "result": "ok"}],
        files={"test-strategy.md": STRATEGY_MD},
    )
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = StrategyStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(4)
    proj = json.loads((out / "test-strategy.json").read_text(encoding="utf-8"))
    assert len(proj["test_cases"]) >= 2


def test_strategy_step_requires_plan(tmp_path: Path):
    ctx = _ctx(tmp_path, with_plan=False)
    result = StrategyStep().run(ctx)
    assert not result.success
    assert "plan.md" in (result.error or "")


def test_strategy_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(bin_dir, events=[{"type": "result", "result": "ok"}], files={})
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = StrategyStep().run(ctx)
    assert not result.success
    assert "test-strategy.md" in (result.error or "")
