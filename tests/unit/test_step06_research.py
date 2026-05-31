"""Step 6 research tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s06_research import (
    ResearchStep,
    _detect_stack,
    _extract_commands,
    _project_research,
)
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query

RESEARCH_MD = """\
# Repository Discovery

## Detected Stack

@playwright/test detected (TypeScript)

## Commands

- Build: `npm run build`
- Test: `npx playwright test`
- Lint: `npm run lint`

## Notes

- monorepo
"""


def test_detect_stack_recognizes_playwright_ts():
    assert _detect_stack("we use @playwright/test here") == "playwright-ts"


def test_detect_stack_pytest_when_only_pytest():
    assert _detect_stack("uses pytest for testing") == "pytest"


def test_extract_commands_parses_build_test_lint():
    cmds = _extract_commands("Build: `npm run build`\nTest: `npx playwright test`\nLint: `npm run lint`")
    assert cmds["build"] == "npm run build"
    assert cmds["test"] == "npx playwright test"
    assert cmds["lint"] == "npm run lint"


def test_project_research_full_shape():
    proj = _project_research(RESEARCH_MD, scan_text=None)
    assert proj["title"] == "Repository Discovery"
    assert proj["detected_stack"] == "playwright-ts"
    assert proj["commands"]["build"] == "npm run build"
    assert any("Detected Stack" in s["title"] for s in proj["sections"][0]["children"])


def _ctx(tmp_path: Path, sut: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=str(sut))
    opts = PipelineOptions(spec="x", sut=str(sut), workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=str(sut), options=opts)


async def test_research_step_local_sut_and_agent_output(tmp_path: Path, monkeypatch):
    # Create a local SUT directory.
    sut = tmp_path / "my-sut"
    sut.mkdir()
    (sut / "package.json").write_text('{"name":"x"}', encoding="utf-8")

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"research.md": RESEARCH_MD},
    )

    ctx = _ctx(tmp_path, sut)
    result = await ResearchStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(6)
    assert (out / "research.md").exists()
    proj = json.loads((out / "research.json").read_text(encoding="utf-8"))
    assert proj["detected_stack"] == "playwright-ts"
    # SUT materialized
    assert (ctx.workspace.sut / "package.json").exists()


async def test_research_step_missing_sut_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, tmp_path / "does-not-exist")
    result = await ResearchStep().run(ctx)
    assert not result.success
    assert "sut" in (result.error or "").lower()


async def test_research_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    sut = tmp_path / "sut2"
    sut.mkdir()
    install_fake_query(monkeypatch, messages=[{"type": "result", "result": "ok"}], files={})

    ctx = _ctx(tmp_path, sut)
    result = await ResearchStep().run(ctx)
    assert not result.success
    assert "research.md" in (result.error or "")
