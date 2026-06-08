"""Step 7 codegen tests.

After the SUT-as-branch refactor, the agent writes worca-prefixed files
DIRECTLY into `<workspace>/sut/` on the `worca-t/run-<id>` branch (via
`add_dirs=[ctx.workspace.sut]`). These tests therefore:

- Seed an empty git-repo SUT at `ws.sut` via `seed_sut()`.
- Pass absolute SUT paths in the fake-claude `files` map so the fake
  agent's writes land where the real agent's would.
- Assert against the SUT, not artifacts (which now hold only metadata).
"""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s07_codegen import CodegenStep
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query
from ._sut_setup import seed_sut

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
    seed_inventory: bool = True,
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
    # SUT lives at ws.sut as a git repo on the worca-t branch — same as
    # production. `include_default_inventory` writes a no-active-module
    # stub so Step 7's pre-flight passes without requiring real SUT files.
    seed_sut(ws, include_default_inventory=seed_inventory)
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _sut_file(workspace, rel: str) -> str:
    """Return the absolute path of a file inside the SUT clone, suitable as
    a key in fake_claude's `files` map."""
    return str(workspace.sut / rel)


async def test_step07_requires_strategy(tmp_path: Path):
    ctx = _ctx(tmp_path, with_strategy=False)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "test-strategy.md" in (result.error or "")


async def test_step07_happy_path_indexes_and_validates(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_login.spec.ts"): GOOD_TS_TEST},
    )

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    # Test file lives in the SUT on the worca-t branch, NOT in artifacts/.
    assert (ctx.workspace.sut / "tests" / "worca_test_login.spec.ts").exists()
    out = ctx.workspace.step_dir(7)
    # Artifact dir holds only metadata (no test bytes mirror).
    assert not (out / "tests").exists()
    assert (out / "generated-files.json").exists()
    index = json.loads((out / "tbd-index.json").read_text(encoding="utf-8"))
    assert index["framework"] == "playwright-ts"
    # Indexer filtered to worca-prefixed files only.
    assert index["totals"]["files"] == 1
    assert index["totals"]["tests"] == 1
    assert index["tests"][0]["tc_refs"] == ["TC-LOGIN-001"]
    assert not index["violations"]


async def test_step07_rejects_xpath_violation(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_bad.spec.ts"): BAD_XPATH_TS},
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")
    out = ctx.workspace.step_dir(7)
    assert (out / "violations.log").exists()
    # Index file still written so operators can inspect.
    assert (out / "tbd-index.json").exists()


async def test_step07_rejects_hard_wait_violation(tmp_path: Path, monkeypatch):
    bad = """import time\ndef test_x(page):\n    time.sleep(3)\n"""
    ctx = _ctx(tmp_path, detected_stack="pytest")
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_bad.py"): bad},
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")


async def test_step07_empty_tests_dir_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},  # agent wrote nothing
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    # No worca_* files produced → error mentions worca / agent.
    err = result.error or ""
    assert "worca" in err or "agent did not produce" in err


async def test_step07_zero_indexed_tests_fails(tmp_path: Path, monkeypatch):
    # File exists but contains no recognisable test function.
    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_notatest.spec.ts"): "const x = 1;\n"},
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "0 worca_*-prefixed test functions" in (result.error or "")


async def test_step07_uses_extension_fallback_when_no_stack(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path, detected_stack=None)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={
            _sut_file(ctx.workspace, "tests/worca_test_x.py"):
                "def test_basic():\n    assert True\n",
        },
    )

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    index = json.loads((ctx.workspace.step_dir(7) / "tbd-index.json").read_text(encoding="utf-8"))
    assert index["framework"] == "pytest"


async def test_step07_fails_fast_when_sut_inventory_missing(tmp_path: Path):
    """Regression: step 7 burned 30+ minutes (full 1800s timeout) when called
    on a workspace without sut_inventory.json. New behavior: fail in <1 s
    with an actionable error pointing the user at `--from-step 6`."""
    ctx = _ctx(tmp_path, seed_inventory=False)
    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "sut_inventory.json" in err
    assert "step 6" in err.lower()


async def test_step07_fails_fast_when_inventory_files_unreachable(tmp_path: Path):
    """When sut_inventory says files exist but ZERO of them resolve under
    `<workspace>/sut/`, the clone is incomplete — fail fast instead of
    letting the agent flail."""
    ws_path = tmp_path / ".ws"
    ws = create_workspace(ws_path)
    (ws.step_dir(4) / "test-strategy.md").write_text("# s\n", encoding="utf-8")
    # Inventory references files that do NOT exist in the SUT.
    inventory = {
        "modules": [
            {
                "name": "app",
                "path": ".",
                "existing_page_objects": [
                    {"file": "src/app/pages/missing_page.py"},
                ],
                "existing_helpers": [],
                "existing_fixtures": [],
                "existing_locators": [],
                "auth_flow": {},
            }
        ],
        "active_module": "app",
    }
    seed_sut(ws, inventory=inventory)
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "r", "sections": [], "detected_stack": "playwright-ts",
            "sut_inventory": inventory,
        }),
        encoding="utf-8",
    )
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=ws_path)
    ctx = StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)

    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "0 of them found" in err or "missing_page.py" in err
