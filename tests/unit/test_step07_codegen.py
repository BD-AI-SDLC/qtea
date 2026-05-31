"""Step 7 codegen tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s07_codegen import CodegenStep
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query

GOOD_TS_TEST = """\
import { test, expect } from '@playwright/test';

// @tc TC-LOGIN-001
test('should sign in with valid credentials', async ({ page }) => {
  await page.goto('/login');
  await page.getByTestId('username').fill('alice');
  await page.getByLabel('Password').fill(process.env.PW);
  await page.getByRole('button', { name: 'Submit' }).click();
  await expect(page.locator('#dashboard')).toBeVisible();
});
"""

BAD_XPATH_TS = """\
test('xpath bad', async ({ page }) => {
  await page.locator('xpath=//button').click();
});
"""


def _ctx(
    tmp_path: Path,
    *,
    detected_stack: str | None = "playwright-ts",
    with_research: bool = True,
    with_plan: bool = True,
    with_refined: bool = True,
    with_strategy: bool = True,
) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if with_strategy:
        (ws.step_dir(4) / "test-strategy.md").write_text("# Strategy\n", encoding="utf-8")
    if with_plan:
        (ws.step_dir(3) / "plan.md").write_text("# Plan\n", encoding="utf-8")
    if with_research:
        (ws.step_dir(6) / "research.md").write_text("# Research\n", encoding="utf-8")
        if detected_stack is not None:
            (ws.step_dir(6) / "research.json").write_text(
                json.dumps({"title": "r", "sections": [], "detected_stack": detected_stack}),
                encoding="utf-8",
            )
    if with_refined:
        (ws.step_dir(2) / "refined-spec.md").write_text("# refined\n", encoding="utf-8")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


async def test_step07_requires_strategy(tmp_path: Path):
    ctx = _ctx(tmp_path, with_strategy=False)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "test-strategy.md" in (result.error or "")


async def test_step07_happy_path_indexes_and_validates(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"tests/login.spec.ts": GOOD_TS_TEST},
    )

    ctx = _ctx(tmp_path)
    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(7)
    assert (out / "tests" / "login.spec.ts").exists()
    index = json.loads((out / "tests-with-tbd.json").read_text(encoding="utf-8"))
    assert index["framework"] == "playwright-ts"
    assert index["totals"]["files"] == 1
    assert index["totals"]["tests"] == 1
    assert index["tests"][0]["tc_refs"] == ["TC-LOGIN-001"]
    assert not index["violations"]


async def test_step07_rejects_xpath_violation(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"tests/bad.spec.ts": BAD_XPATH_TS},
    )

    ctx = _ctx(tmp_path)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")
    out = ctx.workspace.step_dir(7)
    assert (out / "violations.log").exists()
    # Index file still written so operators can inspect.
    assert (out / "tests-with-tbd.json").exists()


async def test_step07_rejects_hard_wait_violation(tmp_path: Path, monkeypatch):
    bad = """import time\ndef test_x(page):\n    time.sleep(3)\n"""
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"tests/test_bad.py": bad},
    )

    ctx = _ctx(tmp_path, detected_stack="pytest")
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")


async def test_step07_empty_tests_dir_fails(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},  # writes no tests/
    )

    ctx = _ctx(tmp_path)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "tests" in (result.error or "")


async def test_step07_zero_indexed_tests_fails(tmp_path: Path, monkeypatch):
    # File exists but contains no recognisable test function.
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"tests/notatest.spec.ts": "const x = 1;\n"},
    )

    ctx = _ctx(tmp_path)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "0 tests" in (result.error or "")


async def test_step07_uses_extension_fallback_when_no_stack(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"tests/test_x.py": "def test_basic():\n    assert True\n"},
    )

    ctx = _ctx(tmp_path, detected_stack=None)
    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    index = json.loads((ctx.workspace.step_dir(7) / "tests-with-tbd.json").read_text(encoding="utf-8"))
    assert index["framework"] == "pytest"
