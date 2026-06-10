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


# ---------------------------------------------------------------------------
# Context-shrink regression guards (~45 KB / turn savings)
#
# Drop A: stop staging active_module.json — byte-identical duplicate of
#   sut_inventory.json["modules"][active_module]. Saves ~22 KB / turn.
# Drop C: stop staging research.md — every datum the codegen agent needed
#   was already in env_hint / sut_inventory / test-strategy. Saves ~25 KB.
#
# Background: run 20260610-082950-6a887f Step 7 hit a corporate-relay
# retry storm partly amplified by the oversized per-turn upload (~154 KB).
# These guards keep the orchestrator from regressing the shrink.
# ---------------------------------------------------------------------------


async def test_step07_does_not_stage_active_module_json(tmp_path: Path, monkeypatch):
    """active_module.json must NOT be written into the agent workdir — it
    duplicates sut_inventory.json["modules"][active_module] byte-for-byte."""
    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_x.spec.ts"): GOOD_TS_TEST},
    )
    await CodegenStep().run(ctx)

    wd = ctx.workspace.step_dir(7)
    assert not (wd / "active_module.json").exists(), (
        "active_module.json must not be staged — it duplicates "
        "sut_inventory.json[\"modules\"][active_module] byte-for-byte and "
        "wastes ~22 KB / turn."
    )


async def test_step07_does_not_stage_research_md(tmp_path: Path, monkeypatch):
    """research.md must NOT be staged into the agent workdir even when
    step 6 produced it. Every datum the codegen agent needed (env vars,
    frameworks, layout) is already in env_hint / sut_inventory.json."""
    ctx = _ctx(tmp_path, with_research=True)  # fixture seeds research.md
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_y.spec.ts"): GOOD_TS_TEST},
    )
    await CodegenStep().run(ctx)

    wd = ctx.workspace.step_dir(7)
    assert not (wd / "research.md").exists(), (
        "research.md must not be staged into step 7's workdir. The agent "
        "gets env vars via env_hint and frameworks via sut_inventory.json; "
        "the prose was ~25 KB / turn of redundant context."
    )


async def test_step07_prompt_references_sut_inventory_path_not_separate_files(
    tmp_path: Path, monkeypatch,
):
    """The user_prompt must instruct the agent to navigate
    sut_inventory.json["modules"][active_module] and must NOT name the
    dropped files (./active_module.json, ./research.md). Drift here =
    agent tries to Read files that aren't staged → failed turn."""
    captured: dict[str, str] = {}

    def _capture(prompt, options):  # noqa: ARG001
        captured["prompt"] = str(prompt)

    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_z.spec.ts"): GOOD_TS_TEST},
        on_call=_capture,
    )
    await CodegenStep().run(ctx)

    prompt = captured.get("prompt", "")
    assert prompt, "fake query should have captured the user_prompt"

    # Negative guards: no references to the dropped files anywhere in
    # the prompt body. (We allow the substring `active_module` since the
    # JSON-path notation `modules[active_module]` contains it.)
    assert "./active_module.json" not in prompt, (
        "Prompt still references the dropped ./active_module.json file"
    )
    assert "active_module.json" not in prompt, (
        "Prompt still references active_module.json — must use the JSON-"
        "path notation modules[active_module] instead"
    )
    assert "./research.md" not in prompt, (
        "Prompt still references the dropped ./research.md file"
    )

    # Positive guard: the new JSON-path navigation wording is present.
    assert "modules[active_module]" in prompt, (
        "Prompt must tell the agent to navigate "
        "sut_inventory.json[\"modules\"][active_module] for the active "
        "module record"
    )


# ---------------------------------------------------------------------------
# JIT runtime pre-vendoring regression guards
#
# Background: run 20260610-114657-c9c7c3 step 7 burned both 1800s attempts
# (zero Writes total) because the agent's prompt told it to import
# `tests.worca_t_runtime` but the runtime did not exist at agent-invoke
# time — it was vendored AFTER the agent succeeded. The agent spent 80
# turns hunting for the file, including a dedicated subagent named
# "Find worca_t_runtime template". The fix is to vendor BEFORE the agent
# runs so the agent reads the file once by path and gets to writing tests.
# ---------------------------------------------------------------------------


async def test_step07_vendors_jit_runtime_before_agent_runs(
    tmp_path: Path, monkeypatch,
):
    """When `detected_stack` is set, the JIT runtime must be on disk in the
    SUT BEFORE `run_agent` is called. Without this guard the chicken-and-egg
    failure mode (agent searches for a runtime that doesn't exist yet)
    silently re-emerges and step 7 wall-clocks the timeout."""
    captured: dict[str, bool] = {}

    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    expected_runtime = ctx.workspace.sut / "tests" / "worca-t-runtime.js"

    def _capture(prompt, options):  # noqa: ARG001
        # Side-effect runs at the moment the fake query is dispatched, which
        # is the exact moment the real SDK would be invoked — i.e. AFTER
        # any pre-agent vendoring in s07 has finished.
        captured["runtime_present_at_agent_invoke"] = expected_runtime.is_file()

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_z.spec.ts"): GOOD_TS_TEST},
        on_call=_capture,
    )
    await CodegenStep().run(ctx)

    assert captured.get("runtime_present_at_agent_invoke") is True, (
        "JIT runtime must be vendored to the SUT BEFORE run_agent is "
        "invoked. Agent prompt tells it to import the runtime; if the "
        "file is absent at invoke time the agent burns its turn budget "
        "hunting for it (see RCA in run 20260610-114657-c9c7c3 step 7)."
    )


async def test_step07_prompt_contains_runtime_location_when_vendored(
    tmp_path: Path, monkeypatch,
):
    """The user_prompt must tell the agent EXACTLY where the runtime is and
    forbid searching for it. Without this directive the agent (justifiably)
    verifies the runtime's existence before relying on the import."""
    captured: dict[str, str] = {}

    def _capture(prompt, options):  # noqa: ARG001
        captured["prompt"] = str(prompt)

    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_z.spec.ts"): GOOD_TS_TEST},
        on_call=_capture,
    )
    await CodegenStep().run(ctx)

    prompt = captured.get("prompt", "")
    assert "JIT RUNTIME (pre-vendored)" in prompt, (
        "Prompt must include the pre-vendored runtime banner so the agent "
        "knows the file is already on disk and skips the discovery loop"
    )
    assert "Do NOT search for it" in prompt, (
        "Prompt must explicitly forbid searching for the runtime — "
        "otherwise the agent's natural verification step burns turns"
    )


async def test_step07_prompt_contains_discovery_discipline_block(
    tmp_path: Path, monkeypatch,
):
    """The DISCOVERY DISCIPLINE block tells the agent: no Bash for filesystem
    ops, trust sut_inventory.json, ≤5 reads before first Write, batch
    Write calls. Drift here regresses the timeout fix from run
    20260610-114657-c9c7c3."""
    captured: dict[str, str] = {}

    def _capture(prompt, options):  # noqa: ARG001
        captured["prompt"] = str(prompt)

    ctx = _ctx(tmp_path)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_z.spec.ts"): GOOD_TS_TEST},
        on_call=_capture,
    )
    await CodegenStep().run(ctx)

    prompt = captured.get("prompt", "")
    assert "DISCOVERY DISCIPLINE" in prompt
    assert "Do NOT use Bash for filesystem discovery" in prompt
    assert "Trust `sut_inventory.json`" in prompt
    assert "Discovery budget" in prompt
    assert "Batch independent `Write` calls" in prompt


async def test_step07_vendored_runtime_excluded_from_index(
    tmp_path: Path, monkeypatch,
):
    """Pre-vendored runtime files live under worca-prefixed names so the
    rglob walk catches them — but they are infrastructure, not agent
    output, and must NOT be counted in the tbd-index's files/support_files
    totals. Otherwise downstream gates see inflated counts and tests that
    assert `files == 1` after writing one file would suddenly see 2."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_login.spec.ts"): GOOD_TS_TEST},
    )

    result = await CodegenStep().run(ctx)
    assert result.success, result.error

    index = json.loads(
        (ctx.workspace.step_dir(7) / "tbd-index.json").read_text(encoding="utf-8")
    )
    indexed_paths = [f for f in index["files"]]
    # The runtime file exists on disk (rglob will see it) but must not
    # appear in the tbd-index — it's not agent output.
    assert not any("worca-t-runtime" in p for p in indexed_paths), (
        f"Pre-vendored runtime leaked into tbd-index: {indexed_paths}"
    )
    # And the index still has exactly the one test file the agent wrote.
    assert index["totals"]["files"] == 1
    assert index["totals"]["tests"] == 1


async def test_step07_vendored_runtime_included_in_manifest(
    tmp_path: Path, monkeypatch,
):
    """Even though the runtime is excluded from the tbd-index, it MUST
    appear in `generated-files.json` so the per-step commit captures it
    and downstream operators can see what was added to the SUT."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={_sut_file(ctx.workspace, "tests/worca_test_login.spec.ts"): GOOD_TS_TEST},
    )

    result = await CodegenStep().run(ctx)
    assert result.success, result.error

    manifest = json.loads(
        (ctx.workspace.step_dir(7) / "generated-files.json").read_text(encoding="utf-8")
    )
    assert any("worca-t-runtime" in f for f in manifest["files"]), (
        f"Pre-vendored runtime missing from generated-files.json: "
        f"{manifest['files']}"
    )


async def test_step07_pre_vendor_does_not_mask_agent_no_writes(
    tmp_path: Path, monkeypatch,
):
    """Subtle failure mode: pre-vendoring writes runtime files into the SUT.
    The post-agent rglob walk catches them as worca-prefixed files. Without
    the `jit_resolved` subtraction in `agent_produced`, an agent that wrote
    ZERO files would falsely pass the "did the agent produce anything?"
    gate because `produced_in_sut` would be non-empty (containing only the
    runtime files we vendored)."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},  # Agent wrote nothing
    )

    result = await CodegenStep().run(ctx)
    assert not result.success, (
        "Agent wrote ZERO test files; step 7 must still fail even though "
        "the pre-vendored runtime files exist under worca-prefixed names "
        "in the SUT. Pre-vendoring must not mask the no-output failure mode."
    )
