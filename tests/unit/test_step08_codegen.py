"""Step 8 codegen tests.

After the phased-codegen refactor, step 8 uses call_reasoning_llm (direct
Anthropic SDK) instead of run_agent (Claude CLI subprocess) for the main
code generation. The Python orchestrator writes generated files directly
into `<workspace>/sut/` from the reasoning response text.

run_agent is still used for the violation-fix phase (Phase C).
"""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s08_codegen import CodegenStep, _strip_code_fences
from worca_t.workspace import create_workspace

from ._fake_anthropic import disable_vertex_env, install_fake_anthropic
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

GOOD_PY_TEST = """\
import pytest

@pytest.mark.worca_smoke
def test_basic(page):
    assert True
"""

_MINIMAL_CODE_MOD_PLAN = {
    "plan_version": "1.0",
    "active_module": "test-module",
    "language": "typescript",
    "framework": "playwright-test",
    "test_cases": [{
        "id": "TC-STUB",
        "test_file_target": "tests/worca_login.spec.ts",
        "test_functions": [{"name": "test_stub", "markers": ["worca_smoke"]}],
        "fixtures": [],
        "page_objects": [],
        "locators": [],
    }],
}


def _ctx(
    tmp_path: Path,
    *,
    detected_stack: str | None = "playwright-ts",
    with_research: bool = True,
    with_plan: bool = True,
    with_refined: bool = True,
    with_strategy: bool = True,
    with_code_mod_plan: bool = True,
    seed_inventory: bool = True,
    plan_override: dict | None = None,
) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if with_strategy:
        (ws.step_dir(4) / "test-strategy.md").write_text(
            "# Strategy\n#### TC-STUB:\nSteps: go to /\nExpected: page loads\n",
            encoding="utf-8",
        )
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
    if with_code_mod_plan:
        (ws.step_dir(7) / "code-modification-plan.json").write_text(
            json.dumps(plan_override or _MINIMAL_CODE_MOD_PLAN), encoding="utf-8",
        )
    seed_sut(ws, include_default_inventory=seed_inventory)
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


async def test_step08_requires_strategy(tmp_path: Path):
    ctx = _ctx(tmp_path, with_strategy=False)
    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "test-strategy.md" in (result.error or "")


async def test_step08_happy_path_indexes_and_validates(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    assert (ctx.workspace.sut / "tests" / "worca_login.spec.ts").exists()
    out = ctx.workspace.step_dir(8)
    assert not (out / "tests").exists()
    assert (out / "generated-files.json").exists()
    index = json.loads((out / "tbd-index.json").read_text(encoding="utf-8"))
    assert index["framework"] == "playwright-ts"
    assert index["totals"]["files"] == 1
    assert index["totals"]["tests"] == 1
    assert index["tests"][0]["tc_refs"] == ["TC-LOGIN-001"]
    assert not index["violations"]


async def test_step08_rejects_xpath_violation(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=BAD_XPATH_TS)
    # Violation-fix run_agent call needs the fake query infrastructure.
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")
    out = ctx.workspace.step_dir(8)
    assert (out / "violations.log").exists()
    assert (out / "tbd-index.json").exists()


async def test_step08_rejects_hard_wait_violation(tmp_path: Path, monkeypatch):
    bad = """import time\ndef test_x(page):\n    time.sleep(3)\n"""
    plan = {**_MINIMAL_CODE_MOD_PLAN, "language": "python", "framework": "pytest"}
    plan["test_cases"] = [{
        "id": "TC-STUB",
        "test_file_target": "tests/worca_bad_test.py",
        "test_functions": [{"name": "test_x", "markers": ["worca_smoke"]}],
        "fixtures": [], "page_objects": [], "locators": [],
    }]
    ctx = _ctx(tmp_path, detected_stack="pytest", plan_override=plan)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=bad)
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "violation" in (result.error or "")


async def test_step08_empty_output_fails(tmp_path: Path, monkeypatch):
    """Reasoning call returns empty text → no test file written → step fails."""
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text="")

    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "worca" in err or "codegen" in err or "failed" in err


async def test_step08_zero_indexed_tests_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text="const x = 1;\n")

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "0 worca_*-prefixed test functions" in (result.error or "")


async def test_step08_uses_extension_fallback_when_no_stack(tmp_path: Path, monkeypatch):
    plan = {**_MINIMAL_CODE_MOD_PLAN, "language": "python", "framework": "pytest"}
    plan["test_cases"] = [{
        "id": "TC-STUB",
        "test_file_target": "tests/worca_x_test.py",
        "test_functions": [{"name": "test_basic", "markers": ["worca_smoke"]}],
        "fixtures": [], "page_objects": [], "locators": [],
    }]
    ctx = _ctx(tmp_path, detected_stack=None, plan_override=plan)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_PY_TEST)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    index = json.loads((ctx.workspace.step_dir(8) / "tbd-index.json").read_text(encoding="utf-8"))
    assert index["framework"] == "pytest"


async def test_step08_fails_fast_when_sut_inventory_missing(tmp_path: Path):
    ctx = _ctx(tmp_path, seed_inventory=False)
    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "sut_inventory.json" in err
    assert "step 6" in err.lower()


async def test_step08_fails_fast_when_inventory_files_unreachable(tmp_path: Path):
    ws_path = tmp_path / ".ws"
    ws = create_workspace(ws_path)
    (ws.step_dir(4) / "test-strategy.md").write_text("# s\n", encoding="utf-8")
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
    (ws.step_dir(7) / "code-modification-plan.json").write_text(
        json.dumps(_MINIMAL_CODE_MOD_PLAN), encoding="utf-8",
    )
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=ws_path)
    ctx = StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)

    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "0 of them found" in err or "missing_page.py" in err


# ---------------------------------------------------------------------------
# Phased codegen regression guards
# ---------------------------------------------------------------------------


async def test_step08_plan_parsed_and_used(tmp_path: Path, monkeypatch):
    """The plan is parsed by Python and drives the phased orchestration.
    Verify the reasoning call receives plan content in its inputs."""
    captured: list[dict] = []

    def _capture(kwargs):
        captured.append(kwargs)

    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST, on_call=_capture)
    await CodegenStep().run(ctx)

    assert captured, "reasoning call should have been made"
    # The test-writer call should have plan content in its messages
    last_call = captured[-1]
    messages = last_call.get("messages", [])
    user_msg = messages[-1]["content"] if messages else ""
    assert "TC-STUB" in user_msg, (
        "The test-writer reasoning call must receive the plan's test case IDs"
    )


async def test_step08_strategy_filtered_to_relevant_tcs(tmp_path: Path, monkeypatch):
    """The strategy text passed to the test writer should be filtered to
    the relevant TC sections."""
    captured: list[dict] = []

    def _capture(kwargs):
        captured.append(kwargs)

    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST, on_call=_capture)
    await CodegenStep().run(ctx)

    assert captured, "reasoning call should have been made"
    last_call = captured[-1]
    messages = last_call.get("messages", [])
    user_msg = messages[-1]["content"] if messages else ""
    assert "strategy.md" in user_msg or "Expected" in user_msg


# ---------------------------------------------------------------------------
# JIT runtime pre-vendoring regression guards
# ---------------------------------------------------------------------------


async def test_step08_vendors_jit_runtime_before_codegen(tmp_path: Path, monkeypatch):
    """When `detected_stack` is set, the JIT runtime must be on disk BEFORE
    the reasoning calls run."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    expected_runtime = ctx.workspace.sut / "tests" / "worca-t-runtime.js"

    runtime_present: dict[str, bool] = {}

    def _capture(kwargs):
        runtime_present["at_call_time"] = expected_runtime.is_file()

    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST, on_call=_capture)
    await CodegenStep().run(ctx)

    assert runtime_present.get("at_call_time") is True, (
        "JIT runtime must be vendored to the SUT BEFORE reasoning calls fire."
    )


async def test_step08_vendored_runtime_excluded_from_index(tmp_path: Path, monkeypatch):
    """Pre-vendored runtime files must NOT appear in the tbd-index."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error

    index = json.loads(
        (ctx.workspace.step_dir(8) / "tbd-index.json").read_text(encoding="utf-8")
    )
    indexed_paths = list(index["files"])
    assert not any("worca-t-runtime" in p for p in indexed_paths), (
        f"Pre-vendored runtime leaked into tbd-index: {indexed_paths}"
    )
    assert index["totals"]["files"] == 1
    assert index["totals"]["tests"] == 1


async def test_step08_vendored_runtime_included_in_manifest(tmp_path: Path, monkeypatch):
    """Runtime files must appear in generated-files.json for the commit."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error

    manifest = json.loads(
        (ctx.workspace.step_dir(8) / "generated-files.json").read_text(encoding="utf-8")
    )
    assert any("worca-t-runtime" in f for f in manifest["files"]), (
        f"Pre-vendored runtime missing from generated-files.json: {manifest['files']}"
    )


async def test_step08_pre_vendor_does_not_mask_no_writes(tmp_path: Path, monkeypatch):
    """An empty reasoning response should not be masked by pre-vendored runtime."""
    ctx = _ctx(tmp_path, detected_stack="playwright-ts")
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text="")

    result = await CodegenStep().run(ctx)
    assert not result.success, (
        "Reasoning returned empty text; step must still fail."
    )


# ---------------------------------------------------------------------------
# _strip_code_fences unit tests
# ---------------------------------------------------------------------------


def test_strip_code_fences_no_fences():
    src = 'def hello():\n    return "hi"\n'
    assert _strip_code_fences(src) == src.strip()


def test_strip_code_fences_python():
    fenced = '```python\ndef hello():\n    return "hi"\n```'
    assert _strip_code_fences(fenced) == 'def hello():\n    return "hi"'


def test_strip_code_fences_py():
    fenced = '```py\nclass Foo:\n    pass\n```'
    assert _strip_code_fences(fenced) == "class Foo:\n    pass"


def test_strip_code_fences_bare():
    fenced = '```\nsome code\n```'
    assert _strip_code_fences(fenced) == "some code"


def test_strip_code_fences_with_prose_preamble():
    """Run 20260611-075728 repro: LLM wrote reasoning BEFORE the fence and
    the .py file ended up with prose at line 1 → parse_error in Phase B.5."""
    response = (
        "Looking at the plan, I need to generate a test file for TC-X.\n"
        "\n"
        "Key observations:\n"
        "1. The test uses chat_page fixture\n"
        "2. ChatPage POM has been extended with method foo\n"
        "\n"
        "```python\n"
        "import pytest\n"
        "\n"
        "def test_basic():\n"
        "    assert True\n"
        "```\n"
    )
    out = _strip_code_fences(response)
    assert not out.startswith("Looking"), (
        f"preamble leaked: {out[:60]!r}"
    )
    assert out.startswith("import pytest"), out[:60]
    assert "def test_basic" in out


def test_strip_code_fences_with_prose_preamble_and_postamble():
    response = (
        "Here's the test file:\n\n"
        "```python\n"
        "def foo(): pass\n"
        "```\n"
        "\nHope that works!"
    )
    out = _strip_code_fences(response)
    assert out == "def foo(): pass"


def test_strip_code_fences_unclosed_fence_returns_body():
    """If the LLM truncated and forgot the closing fence, salvage the body
    rather than writing the opening fence into the file."""
    response = "```python\nimport pytest\ndef test_x(): pass\n"
    out = _strip_code_fences(response)
    assert out.startswith("import pytest")
    assert "```" not in out


def test_strip_code_fences_empty_input():
    assert _strip_code_fences("") == ""
    assert _strip_code_fences("   \n  \n") == ""


async def test_step08_strips_fences_from_generated_files(tmp_path: Path, monkeypatch):
    """LLM output wrapped in markdown fences must be stripped before writing."""
    fenced_test = f"```python\n{GOOD_PY_TEST}```\n"
    plan = {**_MINIMAL_CODE_MOD_PLAN, "language": "python", "framework": "pytest"}
    plan["test_cases"] = [{
        "id": "TC-STUB",
        "test_file_target": "tests/worca_x_test.py",
        "test_functions": [{"name": "test_basic", "markers": ["worca_smoke"]}],
        "fixtures": [], "page_objects": [], "locators": [],
    }]
    ctx = _ctx(tmp_path, detected_stack="pytest", plan_override=plan)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=fenced_test)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    written = (ctx.workspace.sut / "tests" / "worca_x_test.py").read_text(encoding="utf-8")
    assert not written.startswith("```"), (
        f"Markdown fences leaked into generated file: {written[:40]!r}"
    )


# ---------------------------------------------------------------------------
# Phase B.5 — static reconciliation + auto-patch integration tests
# ---------------------------------------------------------------------------
#
# These exercise the B.5 block in s08_codegen.run() that walks generated test
# files, cross-checks <obj>.<method>() calls against POMs on disk, and (on
# mismatch) re-invokes `_extend_poms` once. Each test:
#   - Seeds a POM file into the SUT before the step runs.
#   - Configures a code-modification-plan with one POM reference so the
#     manifest carries it into B.5 (where pom_files is the input).
#   - Returns a known test file body via the fake Anthropic client.
#   - Stubs `_extend_poms` (the autopatch entry point) to control the
#     simulated outcome of the autopatch round-trip.

_B5_POM_BASE = '''\
class LoginPage:
    def __init__(self, page):
        self.page = page

    def click_login(self):
        self.page.click("#login")
'''

_B5_PLAN_WITH_POM: dict = {
    "plan_version": "1.0",
    "active_module": "test-module",
    "language": "python",
    "framework": "pytest",
    "test_cases": [{
        "id": "TC-B5",
        "test_file_target": "tests/worca_login_test.py",
        "test_functions": [{"name": "test_b5_login", "markers": ["worca_smoke"]}],
        "fixtures": [],
        "page_objects": [{
            "name": "LoginPage",
            "source": "reuse",
            "from": "pages/login_page.py",
            "missing_methods": [],
        }],
        "locators": [],
    }],
}


def _seed_pom(ctx: StepContext, rel: str, content: str) -> Path:
    """Write a POM file into the SUT clone after `_ctx` has seeded it."""
    target = ctx.workspace.sut / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


_B5_TEST_HAPPY = '''\
import pytest
from pages.login_page import LoginPage


@pytest.mark.worca_smoke
def test_b5_login(page):
    login_page = LoginPage(page)
    login_page.click_login()
'''

_B5_TEST_MISSING_METHOD = '''\
import pytest
from pages.login_page import LoginPage


@pytest.mark.worca_smoke
def test_b5_login(page):
    login_page = LoginPage(page)
    login_page.click_save()
'''


async def test_step08_b5_happy_path_no_mismatches(tmp_path: Path, monkeypatch):
    """Plan + generated test align with the POM on disk → B.5 emits zero
    mismatches, no auto-patch fires, step completes through Phase C."""
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",
        plan_override=_B5_PLAN_WITH_POM,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    extend_calls: list[dict] = []
    from worca_t.steps import s08_codegen as _s08

    real_extend = _s08._extend_poms

    async def _spy_extend(pom_tasks, *a, **kw):
        extend_calls.append({"count": len(pom_tasks)})
        return await real_extend(pom_tasks, *a, **kw)

    monkeypatch.setattr(_s08, "_extend_poms", _spy_extend)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_HAPPY)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error

    out = ctx.workspace.step_dir(8)
    reconcile_path = out / "reconcile-result.json"
    assert reconcile_path.exists(), "B.5 must always persist reconcile-result.json"
    recon = json.loads(reconcile_path.read_text(encoding="utf-8"))
    assert recon["mismatches"] == [], (
        f"Happy path expects zero mismatches, got: {recon['mismatches']!r}"
    )
    assert "b5_autopatched=False" in (result.notes or ""), (
        f"Happy path must NOT auto-patch; notes={result.notes!r}"
    )


async def test_step08_b5_autopatch_succeeds(tmp_path: Path, monkeypatch):
    """Test calls a method missing from the POM → B.5 detects the mismatch,
    `_extend_poms` is stubbed to add it on disk, second reconcile passes,
    step succeeds and notes record `b5_autopatched=True`."""
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",
        plan_override=_B5_PLAN_WITH_POM,
    )
    pom_path = _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    from worca_t.steps import s08_codegen as _s08

    extend_invocations: list[int] = []

    async def _stub_extend_adds_method(pom_tasks, *a, **kw):
        extend_invocations.append(len(pom_tasks))
        # Append the method the test calls so the second reconcile passes.
        existing = pom_path.read_text(encoding="utf-8")
        if "def click_save" not in existing:
            patched = existing.rstrip() + (
                "\n\n    def click_save(self):\n"
                "        self.page.click(\"#save\")\n"
            )
            pom_path.write_text(patched, encoding="utf-8")
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _stub_extend_adds_method)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_MISSING_METHOD)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    assert extend_invocations, (
        "B.5 must invoke `_extend_poms` when a mismatch is detected"
    )

    out = ctx.workspace.step_dir(8)
    reconcile_path = out / "reconcile-result.json"
    assert reconcile_path.exists()
    recon = json.loads(reconcile_path.read_text(encoding="utf-8"))
    assert recon["mismatches"] == [], (
        f"After successful auto-patch reconcile must show zero mismatches, "
        f"got: {recon['mismatches']!r}"
    )
    assert "b5_autopatched=True" in (result.notes or ""), (
        f"notes must record the auto-patch firing; got: {result.notes!r}"
    )


async def test_step08_b5_autopatch_still_fails(tmp_path: Path, monkeypatch):
    """Test calls a method missing from the POM and the stubbed `_extend_poms`
    does NOT add it → second reconcile still finds the mismatch → step fails
    with a structured error containing the unresolved call site anchor."""
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",
        plan_override=_B5_PLAN_WITH_POM,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    from worca_t.steps import s08_codegen as _s08

    invocations: list[int] = []

    async def _stub_extend_noop(pom_tasks, *a, **kw):
        invocations.append(len(pom_tasks))
        # Intentionally write nothing — the missing method stays missing.
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _stub_extend_noop)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_MISSING_METHOD)

    result = await CodegenStep().run(ctx)
    assert not result.success, (
        f"Auto-patch did not resolve mismatch — step must fail; got {result!r}"
    )
    err = result.error or ""
    assert "Phase B.5 reconciliation failed" in err, (
        f"Error must name the B.5 phase; got: {err!r}"
    )
    # Anchor: <test_file>:<line> calls <Pom>.<method>() — match the unresolved
    # call site so a human reading the failure knows what to fix.
    assert "worca_login_test.py" in err and "click_save" in err, (
        f"Error must surface the unresolved call site anchor; got: {err!r}"
    )

    out = ctx.workspace.step_dir(8)
    reconcile_path = out / "reconcile-result.json"
    assert reconcile_path.exists(), (
        "reconcile-result.json must persist even on B.5 failure (audit trail)"
    )
    recon = json.loads(reconcile_path.read_text(encoding="utf-8"))
    assert recon["mismatches"], (
        "Final reconcile must report the persistent mismatch for the audit log"
    )


async def test_step08_b5_skipped_for_unsupported_language(tmp_path: Path, monkeypatch):
    """When the plan declares Java (out of B.5 v1 scope), the step must
    complete and stamp `b5_skipped=java` on notes so a green B.5 line
    cannot be misread as Java being covered. The default _ctx writes
    no `sut_inventory`, so the language resolution falls through to
    plan_data["language"] — exactly the path Java SUTs hit today."""
    plan = json.loads(json.dumps(_B5_PLAN_WITH_POM))  # deep copy
    plan["language"] = "java"
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",  # framework resolution still works
        plan_override=plan,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    from worca_t.steps import s08_codegen as _s08

    extend_invocations: list[int] = []

    async def _spy_extend(pom_tasks, *a, **kw):
        extend_invocations.append(len(pom_tasks))
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _spy_extend)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_MISSING_METHOD)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    assert "b5_skipped=java" in (result.notes or ""), (
        f"Unsupported language must be surfaced in notes; got: {result.notes!r}"
    )
    # When B.5 is skipped no auto-patch round should fire.
    assert extend_invocations == [], (
        f"B.5 must not autopatch when skipped; extend_invocations={extend_invocations!r}"
    )
    recon_path = ctx.workspace.step_dir(8) / "reconcile-result.json"
    recon = json.loads(recon_path.read_text(encoding="utf-8"))
    assert recon["test_files_scanned"] == 0
    assert recon["call_sites_checked"] == 0


_B5_POM_WITH_SUBMIT = '''\
class LoginPage:
    def __init__(self, page):
        self.page = page

    def submit_form(self):
        self.page.click("#submit")
'''

_B5_TEST_WITH_TYPO = '''\
import pytest
from pages.login_page import LoginPage


@pytest.mark.worca_smoke
def test_b5_login(page):
    login_page = LoginPage(page)
    login_page.sumbit_form()
'''


async def test_step08_b5_likely_typo_does_not_autopatch_and_fails_with_suggestion(
    tmp_path: Path, monkeypatch,
):
    """Test typoes `submit_form` as `sumbit_form`. Reconciler must emit a
    `likely_typo` mismatch (not method_not_found), autopatch must NOT fire
    (no stub method added), step must hard-fail with the 'did you mean'
    anchor visible in the error so the human can fix the test."""
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",
        plan_override=_B5_PLAN_WITH_POM,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_WITH_SUBMIT)

    from worca_t.steps import s08_codegen as _s08

    extend_invocations: list[int] = []

    async def _spy_extend(pom_tasks, *a, **kw):
        extend_invocations.append(len(pom_tasks))
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _spy_extend)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_WITH_TYPO)

    result = await CodegenStep().run(ctx)
    assert not result.success, (
        f"likely_typo must fail loudly so the human fixes the test; "
        f"got result={result!r}"
    )
    err = result.error or ""
    assert "likely_typo" in err, (
        f"Error must classify the mismatch kind; got: {err!r}"
    )
    assert "did you mean `submit_form`" in err, (
        f"Error must surface the typo suggestion; got: {err!r}"
    )
    # Autopatch must NOT have fired — that's the whole point. The stub
    # would have added `sumbit_form` to the POM and silently passed,
    # masking the bug.
    assert extend_invocations == [], (
        f"likely_typo must NOT trigger autopatch; got {extend_invocations!r}"
    )
    # Audit artifact must carry the typo classification.
    recon_path = ctx.workspace.step_dir(8) / "reconcile-result.json"
    recon = json.loads(recon_path.read_text(encoding="utf-8"))
    typos = [m for m in recon["mismatches"] if m["kind"] == "likely_typo"]
    assert len(typos) == 1
    assert typos[0]["suggested_method"] == "submit_form"


async def test_step08_b5_autopatch_crash_returns_clean_step_result(
    tmp_path: Path, monkeypatch,
):
    """If `_extend_poms` raises during auto-patch (transport error, OSError,
    cancellation), the step must convert it to a structured StepResult.failed
    instead of letting the exception propagate. The pre-crash mismatches
    must still be in the reconcile-result.json audit artifact."""
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",
        plan_override=_B5_PLAN_WITH_POM,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    from worca_t.steps import s08_codegen as _s08

    extend_invocations: list[int] = []

    async def _stub_extend_crashes(pom_tasks, *a, **kw):
        extend_invocations.append(len(pom_tasks))
        # Simulate Phase A3 working (first call from the orchestrator's normal
        # POM-extension flow), then crash specifically when B.5 calls it for
        # autopatch. The stub differentiates by inspecting the patch task's
        # `purpose` field which only B.5 populates.
        first_task = next(iter(pom_tasks.values()))
        purpose = (first_task.missing_methods[0]["purpose"]
                   if first_task.missing_methods else "")
        if "Auto-inferred from test call" in purpose:
            raise RuntimeError("simulated upstream API failure")
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _stub_extend_crashes)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_MISSING_METHOD)

    result = await CodegenStep().run(ctx)
    assert not result.success, "B.5 autopatch crash must yield a failed StepResult"
    err = result.error or ""
    assert "Phase B.5 auto-patch crashed" in err, (
        f"Error must name the crash phase; got: {err!r}"
    )
    assert "simulated upstream API failure" in err, (
        f"Error must surface the underlying exception text; got: {err!r}"
    )
    # Audit artifact must persist with the pre-crash mismatch.
    recon_path = ctx.workspace.step_dir(8) / "reconcile-result.json"
    assert recon_path.exists()
    recon = json.loads(recon_path.read_text(encoding="utf-8"))
    assert recon["mismatches"], (
        "Pre-crash mismatches must persist in reconcile-result.json for triage"
    )
