"""Step 3 plan tests."""

from __future__ import annotations

import json
from pathlib import Path

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import StepContext
from qtea.steps.s03_plan import (
    PlanStep,
    _extract_commands,
    _project_plan,
)
from qtea.workspace import create_workspace

from ._fake_anthropic import (
    FakeResponse,
    FakeTextBlock,
    FakeUsage,
    install_fake_anthropic,
)

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


async def test_plan_step_writes_md_and_json(tmp_path: Path, monkeypatch):
    install_fake_anthropic(monkeypatch, text=PLAN_MD)

    ctx = _ctx(tmp_path)
    result = await PlanStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(3)
    assert (out / "plan.md").exists()
    proj = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert len(proj["phases"]) == 2


async def test_plan_step_inlines_refined_spec_into_user_prompt(
    tmp_path: Path, monkeypatch
):
    """The refined-spec content must reach the LLM via the user message inputs."""
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text=PLAN_MD, on_call=captured.update)

    ctx = _ctx(tmp_path)
    # Overwrite the seeded refined-spec with a distinctive marker.
    (ctx.workspace.step_dir(2) / "refined-spec.md").write_text(
        "# refined\n\nREFINED_INLINE_MARKER_ABC\n", encoding="utf-8"
    )

    result = await PlanStep().run(ctx)
    assert result.success

    user_content = captured["messages"][-1]["content"]
    assert "REFINED_INLINE_MARKER_ABC" in user_content
    assert "refined-spec.md" in user_content


async def test_plan_step_requires_inputs(tmp_path: Path):
    ctx = _ctx(tmp_path, with_research=False, with_refined=False)
    result = await PlanStep().run(ctx)
    assert not result.success
    assert "step 3 requires" in (result.error or "")


async def test_plan_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="")

    ctx = _ctx(tmp_path)
    result = await PlanStep().run(ctx)
    assert not result.success
    assert "plan.md" in (result.error or "")


PLAN_MD_WITH_BLOCKERS = """\
# Test Implementation Plan

## Overview

Plan overview.

## Blockers

| Blocker | Affected TCs | Severity |
|---------|--------------|----------|
| SSO config unavailable | TC-AUTH-001 | high |

## Phase Summary

| Phase | Focus           | Files | Est. Tests |
|-------|-----------------|-------|------------|
| 1     | Core utilities  | 1     | 5          |

---

## Phase 1: Core Utilities

### Overview

Foundation tests.

### Files to Test

#### 1. utils/login.ts
- **Source**: `src/utils/login.ts`
- **Test File**: `tests/utils/login.spec.ts`
"""


def _install_scripted_anthropic(monkeypatch, texts: list[str]):
    """Install an AsyncAnthropic that returns ``texts[i]`` on call i."""
    calls = {"n": 0}

    async def _create(**_kwargs):
        i = calls["n"]
        calls["n"] = i + 1
        return FakeResponse(
            content=[FakeTextBlock(text=texts[min(i, len(texts) - 1)])],
            usage=FakeUsage(),
        )

    class _Stream:
        def __init__(self, kwargs):
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def get_final_message(self):
            return await _create(**self._kwargs)

    class FakeMessages:
        create = staticmethod(_create)

        @staticmethod
        def stream(**kwargs):
            return _Stream(kwargs)

    class FakeClient:
        def __init__(self, **_kw):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    # Patch BOTH client classes — Vertex env may be set globally on the dev machine.
    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
    monkeypatch.setattr("anthropic.AsyncAnthropicVertex", FakeClient)
    return calls


async def test_plan_step_hitl_loop_prompts_user_and_reruns(
    tmp_path: Path, monkeypatch
):
    calls = _install_scripted_anthropic(monkeypatch, [PLAN_MD_WITH_BLOCKERS, PLAN_MD])

    # prompt_user returns dict[str, tuple[resolution, text]].
    from qtea.hitl import RESOLUTION_ANSWERED
    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda questions, *, agent_label: {
            q.id: (RESOLUTION_ANSWERED, "use mock IdP") for q in questions
        },
    )

    ctx = _ctx(tmp_path)
    result = await PlanStep().run(ctx)
    assert result.success, result.error
    assert calls["n"] == 2

    hitl_dir = ctx.workspace.step_workdir(3).parent / ".hitl-step03"
    assert hitl_dir.exists() and any(hitl_dir.iterdir())
