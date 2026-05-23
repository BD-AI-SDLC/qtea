"""Step 3 plan tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s03_plan import (
    PlanStep,
    _extract_commands,
    _project_plan,
)
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude

PLAN_MD = """\
# Test Implementation Plan

## Overview

This is the plan overview.

## Commands

- **Build**: `npm run build`
- **Test**: `npx playwright test`
- **Lint**: `npm run lint`

## Phase Summary

| Phase | Focus           | Files | Est. Tests |
|-------|-----------------|-------|------------|
| 1     | Core utilities  | 2     | 10-15      |
| 2     | Business logic  | 3     | 15-20      |

---

## Phase 1: Core Utilities

### Overview

Foundation tests first.

### Files to Test

#### 1. utils/login.ts
- **Source**: `src/utils/login.ts`
- **Test File**: `tests/utils/login.spec.ts`
- **Test Class**: `LoginTests`

**Methods to Test**:
1. `validate` - validates input
2. `sanitize` - sanitizes payload

#### 2. utils/session.ts
- **Source**: `src/utils/session.ts`
- **Test File**: `tests/utils/session.spec.ts`

### Success Criteria
- [ ] All test files created
- [ ] Tests pass

---

## Phase 2: Business Logic

### Files to Test

#### 1. service/auth.ts
- **Source**: `src/service/auth.ts`

### Success Criteria
- [ ] All scenarios covered
"""


def test_extract_commands():
    cmds = _extract_commands(PLAN_MD)
    assert cmds["build"] == "npm run build"
    assert cmds["test"] == "npx playwright test"
    assert cmds["lint"] == "npm run lint"


def test_parse_phase_files_extracts_source_and_methods():
    # Verified via the public _project_plan projection on the canonical PLAN_MD.
    proj = _project_plan(PLAN_MD)
    files = proj["phases"][0]["files"]
    titles = {f["title"] for f in files}
    assert "utils/login.ts" in titles
    login = next(f for f in files if f["title"] == "utils/login.ts")
    assert login["source"] == "src/utils/login.ts"
    assert login["test_file"] == "tests/utils/login.spec.ts"
    assert login["methods"] == ["validate", "sanitize"]


def test_project_plan_full_shape():
    proj = _project_plan(PLAN_MD)
    assert proj["title"] == "Test Implementation Plan"
    assert proj["commands"]["build"] == "npm run build"
    assert len(proj["phases"]) == 2
    p1 = proj["phases"][0]
    assert p1["number"] == 1
    assert p1["title"] == "Core Utilities"
    assert any(f["title"] == "utils/login.ts" for f in p1["files"])
    assert any("All test files" in c for c in p1["success_criteria"])
    # Phase summary table extracted
    assert len(proj["phase_summary"]) == 3  # header + 2 rows
    assert proj["phase_summary"][0][0].lower() == "phase"


def _ctx(tmp_path: Path, *, with_research: bool = True, with_refined: bool = True) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if with_refined:
        (ws.step_dir(2) / "refined-spec.md").write_text("# refined\n", encoding="utf-8")
    if with_research:
        (ws.step_dir(6) / "research.md").write_text("# research\n", encoding="utf-8")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def test_plan_step_writes_md_and_json(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(
        bin_dir,
        events=[{"type": "result", "result": "ok"}],
        files={"plan.md": PLAN_MD},
    )
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = PlanStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(3)
    assert (out / "plan.md").exists()
    proj = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert len(proj["phases"]) == 2


def test_plan_step_requires_inputs(tmp_path: Path):
    ctx = _ctx(tmp_path, with_research=False, with_refined=False)
    result = PlanStep().run(ctx)
    assert not result.success
    assert "step 3 requires" in (result.error or "")


def test_plan_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_path = write_fake_claude(bin_dir, events=[{"type": "result", "result": "ok"}], files={})
    install_on_path(monkeypatch, bin_path)

    ctx = _ctx(tmp_path)
    result = PlanStep().run(ctx)
    assert not result.success
    assert "plan.md" in (result.error or "")
