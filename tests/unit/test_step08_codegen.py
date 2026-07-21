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

import pytest

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.runtime.dev_locators import DevLocator
from qtea.steps.base import StepContext
from qtea.steps.s08_codegen import (
    CodegenStep,
    _b5_filter_test_files,
    _build_all_codegen_files,
    _build_regen_feedback_hint,
    _compose_playwright_global_setup,
    _detect_init_placement,
    _filter_index_to_qtea,
    _framework_mismatch_message,
    _LocatorTask,
    _match_dev_locator,
    _PomTask,
    _normalize_runtime_import_in_file,
    _normalize_runtime_imports,
    _parse_test_command_head,
    _register_playwright_test_global_setup,
    _run_phase_b55_xpath_normalisation,
    _strip_code_fences,
    _units_by_file,
    _write_tbd_locators,
)
from qtea.workspace import create_workspace

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

@pytest.mark.qtea_smoke
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
        "test_file_target": "tests/qtea_login.spec.ts",
        "test_functions": [{"name": "test_stub", "markers": ["qtea_smoke"]}],
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
        (ws.step_dir(4) / "test-design.md").write_text(
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
    assert "test-design.md" in (result.error or "")


async def test_step08_happy_path_indexes_and_validates(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=GOOD_TS_TEST)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    assert (ctx.workspace.sut / "tests" / "qtea_login.spec.ts").exists()
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
        "test_file_target": "tests/qtea_bad_test.py",
        "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
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
    assert "qtea" in err or "codegen" in err or "failed" in err


async def test_step08_zero_indexed_tests_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text="const x = 1;\n")

    result = await CodegenStep().run(ctx)
    assert not result.success
    assert "0 qtea_*-prefixed test functions" in (result.error or "")


async def test_step08_uses_extension_fallback_when_no_stack(tmp_path: Path, monkeypatch):
    plan = {**_MINIMAL_CODE_MOD_PLAN, "language": "python", "framework": "pytest"}
    plan["test_cases"] = [{
        "id": "TC-STUB",
        "test_file_target": "tests/qtea_x_test.py",
        "test_functions": [{"name": "test_basic", "markers": ["qtea_smoke"]}],
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
    (ws.step_dir(4) / "test-design.md").write_text("# s\n", encoding="utf-8")
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
    expected_runtime = ctx.workspace.sut / "tests" / "qtea-runtime.js"

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
    assert not any("qtea-runtime" in p for p in indexed_paths), (
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
    assert any("qtea-runtime" in f for f in manifest["files"]), (
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


def test_b5_filter_recognises_playwright_spec_ts_convention(tmp_path: Path):
    """Regression guard for run 20260701-114656-9394eb: the emitted test
    file was ``qtea_entity_approval_test.spec.ts`` (Playwright's ``.spec.ts``
    convention). The prior filter required ``stem.endswith("_test")``, and
    the stem here is ``qtea_entity_approval_test.spec`` — so the file was
    silently skipped by B.5 (0 test files scanned).
    """
    files = [
        tmp_path / "qtea_entity_approval_test.spec.ts",       # Playwright .spec.ts
        tmp_path / "qtea_login.spec.ts",                    # Playwright bare .spec.ts
        tmp_path / "qtea_dashboard.spec.js",                # Playwright .spec.js
        tmp_path / "qtea_setup_page.ts",                    # POM — must NOT be picked
        tmp_path / "qtea_login_test.py",                    # Python — legacy _test suffix
    ]
    for p in files:
        p.write_text("", encoding="utf-8")
    picked = _b5_filter_test_files(files, language="typescript")
    picked_names = {p.name for p in picked}
    assert "qtea_entity_approval_test.spec.ts" in picked_names
    assert "qtea_login.spec.ts" in picked_names
    assert "qtea_dashboard.spec.js" in picked_names
    assert "qtea_setup_page.ts" not in picked_names, "POM leaked into test set"
    assert "qtea_login_test.py" in picked_names


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
        "test_file_target": "tests/qtea_x_test.py",
        "test_functions": [{"name": "test_basic", "markers": ["qtea_smoke"]}],
        "fixtures": [], "page_objects": [], "locators": [],
    }]
    ctx = _ctx(tmp_path, detected_stack="pytest", plan_override=plan)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=fenced_test)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    written = (ctx.workspace.sut / "tests" / "qtea_x_test.py").read_text(encoding="utf-8")
    assert not written.startswith("```"), (
        f"Markdown fences leaked into generated file: {written[:40]!r}"
    )


# ---------------------------------------------------------------------------
# Phase B.5.5 — legacy XPath normalisation
# ---------------------------------------------------------------------------


def test_phase_b55_normalises_legacy_xpath_in_pom(tmp_path: Path):
    """Pre-existing POM with xpath locators is rewritten before the gate
    sees it. Playwright config picks up testIdAttribute automatically."""
    sut = tmp_path / "sut"
    (sut / "src" / "pages").mkdir(parents=True)
    pom = sut / "src" / "pages" / "BasePage.ts"
    pom.write_text(
        "import { Page } from '@playwright/test';\n"
        "export class BasePage {\n"
        "  page: Page;\n"
        "  elements: Record<string, string> = {\n"
        "    inpUser: '//input[@data-test=\"username-input\"]',\n"
        "    btnGo: '//button[contains(normalize-space(.), \"Go\")]',\n"
        "  };\n"
        "  constructor(p: Page) { this.page = p; }\n"
        "  async go() { await this.page.locator(this.elements.btnGo).click(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (sut / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        "  testDir: './tests',\n"
        "  use: { baseURL: 'https://example.com' },\n"
        "});\n",
        encoding="utf-8",
    )
    reports, stragglers, touched_files = _run_phase_b55_xpath_normalisation(
        sut_root=sut,
        candidates={pom.resolve()},
    )
    assert stragglers == []
    assert len(reports) == 1
    r = reports[0]
    assert r.container_migrated
    assert r.call_sites_migrated == 1
    assert r.testid_attr_needed
    new = pom.read_text(encoding="utf-8")
    assert "getByTestId('username-input')" in new
    assert "getByRole('button', { name: 'Go' })" in new
    assert "this.elements.btnGo()" in new
    # config touched
    cfg = (sut / "playwright.config.ts").read_text(encoding="utf-8")
    assert "testIdAttribute: 'data-test'" in cfg
    # touched_files reports both the rewritten POM AND the config edit, so
    # callers building a codegen-scope set can union it in (bug 4).
    assert pom.resolve() in {p.resolve() for p in touched_files}
    assert (sut / "playwright.config.ts").resolve() in {
        p.resolve() for p in touched_files
    }


def test_phase_b55_collects_stragglers_for_llm(tmp_path: Path):
    """Xpath the deterministic rewriter can't safely translate lands in
    the straggler list and gets an exempt marker in the source."""
    sut = tmp_path / "sut"
    (sut / "src" / "pages").mkdir(parents=True)
    pom = sut / "src" / "pages" / "Hairy.ts"
    pom.write_text(
        "export class Hairy {\n"
        "  page: any;\n"
        "  elements: Record<string, string> = {\n"
        "    goodOne: '//input[@data-test=\"x\"]',\n"
        "    axisOne: '//div[@ref=\"y\"]/parent::td',\n"
        "  };\n"
        "}\n",
        encoding="utf-8",
    )
    _reports, stragglers, _touched = _run_phase_b55_xpath_normalisation(
        sut_root=sut,
        candidates={pom.resolve()},
    )
    assert len(stragglers) == 1
    assert "parent::td" in stragglers[0].original
    new = pom.read_text(encoding="utf-8")
    # Marker present so the gate skips the surviving xpath
    assert "qtea-xpath-exempt" in new
    assert "getByTestId('x')" in new


def test_phase_b55_skips_non_ts_js_files(tmp_path: Path):
    """Python / Java files should never enter this pass."""
    sut = tmp_path / "sut"
    sut.mkdir()
    py = sut / "test_x.py"
    py.write_text("assert True\n", encoding="utf-8")
    java = sut / "Base.java"
    java.write_text("class Base {}\n", encoding="utf-8")
    reports, stragglers, touched_files = _run_phase_b55_xpath_normalisation(
        sut_root=sut,
        candidates={py.resolve(), java.resolve()},
    )
    assert reports == []
    assert stragglers == []
    assert touched_files == []


def test_phase_b55_gate_gets_zero_violations_after_rewrite(tmp_path: Path):
    """End-to-end check: after Phase B.5.5 runs, the xpath quality-gate
    reports zero violations for the modified files (except those explicitly
    marked exempt)."""
    from qtea.test_indexer import index_tests

    sut = tmp_path / "sut"
    tests_dir = sut / "tests"
    pages_dir = sut / "src" / "pages"
    tests_dir.mkdir(parents=True)
    pages_dir.mkdir(parents=True)
    pom = pages_dir / "LoginPage.ts"
    pom.write_text(
        "import { Page } from '@playwright/test';\n"
        "export class LoginPage {\n"
        "  page: Page;\n"
        "  elements: Record<string, string> = {\n"
        "    inpUser: '//input[@data-test=\"user\"]',\n"
        "    inpPass: '//input[@data-test=\"pass\"]',\n"
        "  };\n"
        "  constructor(p: Page) { this.page = p; }\n"
        "  async login(u: string, p: string) {\n"
        "    await this.page.locator(this.elements.inpUser).fill(u);\n"
        "    await this.page.locator(this.elements.inpPass).fill(p);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (tests_dir / "qtea_login_test.spec.ts").write_text(
        "import { test, expect } from '@playwright/test';\n"
        "// @tc TC-1\n"
        "test('login', async ({ page }) => { await expect(page).toHaveTitle(/.*/); });\n",
        encoding="utf-8",
    )

    # Pre-normalisation: the gate WOULD flag xpath in LoginPage.ts
    pre = index_tests(sut, framework="playwright-ts")
    assert any(v.rule == "xpath" for v in pre.violations)

    # Run Phase B.5.5
    _run_phase_b55_xpath_normalisation(
        sut_root=sut, candidates={pom.resolve()},
    )

    # Post-normalisation: no more xpath violations
    post = index_tests(sut, framework="playwright-ts")
    xpath_hits = [v for v in post.violations if v.rule == "xpath"]
    assert xpath_hits == [], f"expected zero xpath, got: {xpath_hits}"


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
        "test_file_target": "tests/qtea_login_test.py",
        "test_functions": [{"name": "test_b5_login", "markers": ["qtea_smoke"]}],
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


@pytest.mark.qtea_smoke
def test_b5_login(page):
    login_page = LoginPage(page)
    login_page.click_login()
    assert page is not None
'''

_B5_TEST_MISSING_METHOD = '''\
import pytest
from pages.login_page import LoginPage


@pytest.mark.qtea_smoke
def test_b5_login(page):
    login_page = LoginPage(page)
    login_page.click_save()
    assert page is not None
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
    from qtea.steps import s08_codegen as _s08

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

    from qtea.steps import s08_codegen as _s08

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

    from qtea.steps import s08_codegen as _s08

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
    assert "qtea_login_test.py" in err and "click_save" in err, (
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
    """When the plan declares a language outside `_B5_SUPPORTED_LANGUAGES`,
    the step must complete and stamp `b5_skipped=<lang>` on notes so a
    green B.5 line cannot be misread as "that language was covered."

    Currently supported: python, typescript, javascript, java. This test
    uses ``csharp`` as a genuinely out-of-scope example.
    """
    plan = json.loads(json.dumps(_B5_PLAN_WITH_POM))  # deep copy
    plan["language"] = "csharp"
    ctx = _ctx(
        tmp_path,
        detected_stack="pytest",  # framework resolution still works
        plan_override=plan,
    )
    _seed_pom(ctx, "pages/login_page.py", _B5_POM_BASE)

    from qtea.steps import s08_codegen as _s08

    extend_invocations: list[int] = []

    async def _spy_extend(pom_tasks, *a, **kw):
        extend_invocations.append(len(pom_tasks))
        return [(t.pom_file, True) for t in pom_tasks.values()]

    monkeypatch.setattr(_s08, "_extend_poms", _spy_extend)
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text=_B5_TEST_MISSING_METHOD)

    result = await CodegenStep().run(ctx)
    assert result.success, result.error
    assert "b5_skipped=csharp" in (result.notes or ""), (
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


@pytest.mark.qtea_smoke
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

    from qtea.steps import s08_codegen as _s08

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

    from qtea.steps import s08_codegen as _s08

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


# ---------------------------------------------------------------------------
# Dev-locator matching
# ---------------------------------------------------------------------------


def test_match_dev_locator_exact_key():
    dev = {"MY_BUTTON": DevLocator(constant_name="MY_BUTTON", selector="#btn")}
    task = _LocatorTask(constant_name="MY_BUTTON", intent="click button", owning_page="Home")
    assert _match_dev_locator(task, dev) is not None
    assert _match_dev_locator(task, dev).selector == "#btn"


def test_match_dev_locator_intent_fallback():
    dev = {"OTHER": DevLocator(constant_name="OTHER", selector="#btn", intent="click button")}
    task = _LocatorTask(constant_name="MY_BUTTON", intent="Click Button", owning_page="Home")
    hit = _match_dev_locator(task, dev)
    assert hit is not None
    assert hit.selector == "#btn"


def test_match_dev_locator_no_match():
    dev = {"OTHER": DevLocator(constant_name="OTHER", selector="#btn", intent="submit form")}
    task = _LocatorTask(constant_name="MY_BUTTON", intent="click button", owning_page="Home")
    assert _match_dev_locator(task, dev) is None


def test_match_dev_locator_empty():
    task = _LocatorTask(constant_name="MY_BUTTON", intent="click", owning_page="Home")
    assert _match_dev_locator(task, {}) is None
    assert _match_dev_locator(task, None) is None


# ---------------------------------------------------------------------------
# Instance-attribute placement detection
# ---------------------------------------------------------------------------


_LOCATORS_WITH_INIT = '''\
class ChatPageLocators:
    DEFAULT_X = "[data-testid='x']"

    def __init__(self):
        self.PROMPT_FIELD = "[data-testid='PromptInput']"
        self.SEND_BUTTON = "[data-testid='Submit']"

    def reset(self):
        self.__init__()
'''.strip().splitlines()


_LOCATORS_CLASS_LEVEL = '''\
class LoginLocators:
    LOGIN_BUTTON = "#login"
    PASSWORD_INPUT = "#password"
'''.strip().splitlines()


def test_detect_init_placement_with_self_attrs():
    use_self, indent, idx = _detect_init_placement(_LOCATORS_WITH_INIT)
    assert use_self is True
    assert indent == "        "
    assert idx > 0


def test_detect_init_placement_class_level():
    use_self, _indent, _idx = _detect_init_placement(_LOCATORS_CLASS_LEVEL)
    assert use_self is False


# ---------------------------------------------------------------------------
# _write_tbd_locators with dev-locators + instance placement
# ---------------------------------------------------------------------------


def test_write_tbd_locators_dev_match(tmp_path: Path):
    loc_file = tmp_path / "locators.py"
    loc_file.write_text(
        'class Loc:\n    EXISTING = "#e"\n',
        encoding="utf-8",
    )
    dev = {"NEW_BTN": DevLocator(constant_name="NEW_BTN", selector="[data-testid='btn']")}
    tasks = [_LocatorTask(
        constant_name="NEW_BTN",
        intent="new button",
        owning_page="Page",
        locator_file=str(loc_file.relative_to(tmp_path)),
    )]
    count = _write_tbd_locators(tasks, tmp_path, "python", dev_locators=dev)
    assert count == 1
    content = loc_file.read_text(encoding="utf-8")
    assert "[data-testid='btn']" in content
    assert "tbd(" not in content


def test_write_tbd_locators_no_dev_match(tmp_path: Path):
    loc_file = tmp_path / "locators.py"
    loc_file.write_text(
        'class Loc:\n    EXISTING = "#e"\n',
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="NEW_BTN",
        intent="new button",
        owning_page="Page",
        locator_file=str(loc_file.relative_to(tmp_path)),
    )]
    count = _write_tbd_locators(tasks, tmp_path, "python", dev_locators={})
    assert count == 1
    content = loc_file.read_text(encoding="utf-8")
    assert 'tbd("new button")' in content


def test_write_tbd_locators_instance_placement(tmp_path: Path):
    loc_file = tmp_path / "locators.py"
    loc_file.write_text(
        'from tests.qtea_runtime import tbd\n'
        '\n'
        'class ChatLocators:\n'
        '    def __init__(self):\n'
        '        self.FIELD_A = "[data-testid=\'a\']"\n'
        '        self.FIELD_B = "[data-testid=\'b\']"\n'
        '\n'
        '    def reset(self):\n'
        '        self.__init__()\n',
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="NEW_FIELD",
        intent="new input field",
        owning_page="Chat",
        locator_file=str(loc_file.relative_to(tmp_path)),
    )]
    count = _write_tbd_locators(tasks, tmp_path, "python")
    assert count == 1
    content = loc_file.read_text(encoding="utf-8")
    assert 'self.NEW_FIELD = tbd("new input field")' in content
    for line in content.splitlines():
        if "NEW_FIELD" in line:
            assert "self." in line, f"must use self. prefix: {line}"
            break


def test_write_tbd_locators_writes_inline_object_property(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """When the SUT uses ``inline_object_property`` convention
    (``elements`` dict on POM class), Step 8 MUST now mechanically
    insert the new constant as ``KEY: tbd("intent")`` before the
    object's closing ``}`` — closing the coherence trap that let the
    pom-extender invent selectors when the sentinel wasn't pre-written.

    Prior behaviour DEFERRED to the extender; that path led to
    fabricated XPath strings (see fix-batch RCA-B).
    """
    import logging

    pom_file = tmp_path / "src" / "pages" / "EntityFormPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text(
        "export class EntityFormPage {\n"
        "    elements = { btnCreate: '//button' };\n"
        "}\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="btnSendForApproval",
        intent="send for approval button",
        owning_page="EntityFormPage",
        locator_file="src/pages/EntityFormPage.ts",
        location_pattern="inline_object_property",
        container_name="elements",
        container_class_name="EntityFormPage",
    )]
    caplog.set_level(logging.INFO)
    count = _write_tbd_locators(tasks, tmp_path, "typescript")
    assert count == 1
    content = pom_file.read_text(encoding="utf-8")
    assert 'btnSendForApproval: tbd("send for approval button")' in content
    # Original entry preserved:
    assert 'btnCreate' in content
    # Written INFO log fires (not the legacy defer log):
    messages = [rec.message for rec in caplog.records]
    assert any(
        "tbd_locators_written_object_literal" in m for m in messages
    ), f"expected object-literal-written INFO log; got {messages!r}"


def test_write_tbd_locators_export_const_object_appends(tmp_path: Path):
    """``export const FooSelectors = { ... }`` — new entries inserted
    before the object's closing ``}``, preserving existing keys AND
    importing ``tbd`` with a specifier relative to the file (the runtime is
    vendored at ``<sut>/tests/qtea-runtime.js``, so from ``src/pages/`` that
    is ``../../tests/qtea-runtime``)."""
    pom_file = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text(
        'import { Page } from "@playwright/test";\n'
        "\n"
        "export const TrialPageSelectors = {\n"
        '  ExistingBtn: "//button[@id=\'go\']",\n'
        '  ExistingInp: "#name",\n'
        "};\n"
        "\n"
        "export class TrialPage {\n"
        "  constructor(page: Page) {}\n"
        "}\n",
        encoding="utf-8",
    )
    tasks = [
        _LocatorTask(
            constant_name="CHECKBOX_MARKETING_CONSENT",
            intent="marketing consent checkbox on trial form",
            owning_page="TrialPage",
            locator_file="src/pages/TrialPage.ts",
            location_pattern="export_const_object",
            container_class_name="TrialPageSelectors",
        ),
    ]
    count = _write_tbd_locators(tasks, tmp_path, "typescript")
    assert count == 1
    content = pom_file.read_text(encoding="utf-8")
    assert (
        'CHECKBOX_MARKETING_CONSENT: tbd("marketing consent checkbox on trial form")'
        in content
    )
    # Existing entries preserved:
    assert 'ExistingBtn' in content
    assert 'ExistingInp' in content
    # New entry lands inside the object, before the closing brace of the
    # TrialPageSelectors const — not somewhere else in the file.
    obj_open = content.find("TrialPageSelectors = {")
    obj_close = content.find("};", obj_open)
    new_entry_pos = content.find("CHECKBOX_MARKETING_CONSENT")
    assert obj_open < new_entry_pos < obj_close, (
        "new entry must be inside the TrialPageSelectors object body"
    )
    # tbd import injected once, with a file-relative specifier:
    assert content.count("qtea-runtime") == 1
    assert 'import { tbd } from "../../tests/qtea-runtime";' in content


def test_write_tbd_locators_export_const_object_dev_match(tmp_path: Path):
    """When a dev-locator matches, its raw selector is written into the
    object literal INSTEAD of a ``tbd()`` sentinel — and the tbd import
    is NOT injected because no sentinel was emitted."""
    pom_file = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text(
        "export const TrialPageSelectors = {\n"
        '  Existing: "#x",\n'
        "};\n",
        encoding="utf-8",
    )
    dev = {"CHECKBOX_MARKETING_CONSENT": DevLocator(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        selector="[data-testid='marketing-consent']",
    )}
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent checkbox",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="export_const_object",
        container_class_name="TrialPageSelectors",
    )]
    count = _write_tbd_locators(tasks, tmp_path, "typescript", dev_locators=dev)
    assert count == 1
    content = pom_file.read_text(encoding="utf-8")
    assert (
        "CHECKBOX_MARKETING_CONSENT: \"[data-testid='marketing-consent']\""
        in content
    )
    assert "tbd(" not in content
    assert "qtea-runtime" not in content


def test_write_tbd_locators_promotes_mislabeled_separate_class_ts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Durable TD-1 fix (run 20260708-121117-99f5ed): a TS file whose
    ``export const XSelectors = {…}`` object was mislabeled ``separate_class``
    by Step-6 must NOT be deferred to the LLM extender. Step 8 detects the
    real object-literal shape from the file content (via container_class_name)
    and writes the sentinel + a file-relative import deterministically."""
    import logging

    pom_file = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text(
        'import { Page } from "@playwright/test";\n'
        "\n"
        "export const TrialPageSelectors = {\n"
        '  Existing: "#x",\n'
        "};\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent checkbox on the trial form",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="separate_class",  # the Step-6 mislabel
        container_class_name="TrialPageSelectors",
    )]
    caplog.set_level(logging.INFO)
    count = _write_tbd_locators(tasks, tmp_path, "typescript")
    assert count == 1
    content = pom_file.read_text(encoding="utf-8")
    # Sentinel written INTO the object literal, before its closing brace:
    assert (
        'CHECKBOX_MARKETING_CONSENT: tbd("marketing consent checkbox on the '
        'trial form")' in content
    )
    obj_open = content.find("TrialPageSelectors = {")
    obj_close = content.find("};", obj_open)
    assert obj_open < content.find("CHECKBOX_MARKETING_CONSENT") < obj_close
    # File-relative import, not the hardcoded ./qtea-runtime:
    assert 'import { tbd } from "../../tests/qtea-runtime";' in content
    # And it was NOT deferred to the extender:
    messages = [rec.message for rec in caplog.records]
    assert not any(
        "tbd_locator_deferred_to_extender" in m for m in messages
    ), f"mislabeled object literal must be written, not deferred; got {messages!r}"


def test_write_tbd_locators_still_defers_ts_when_no_container(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression guard: a non-object-literal TS task whose container is
    genuinely absent from the file still defers to the extender and writes
    nothing (the promotion path must not swallow truly-unknown shapes)."""
    import logging

    pom_file = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text(
        'import { Page } from "@playwright/test";\n'
        "export class TrialPage {}\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent checkbox",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="separate_class",
        container_class_name="TrialPageSelectors",  # not present in the file
    )]
    caplog.set_level(logging.INFO)
    count = _write_tbd_locators(tasks, tmp_path, "typescript")
    assert count == 0
    assert "tbd(" not in pom_file.read_text(encoding="utf-8")
    messages = [rec.message for rec in caplog.records]
    assert any(
        "tbd_locator_deferred_to_extender" in m for m in messages
    ), f"expected defer log for unresolvable container; got {messages!r}"


def test_write_tbd_locators_defers_when_no_locator_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """When the SUT has no locator source for a POM at all (neither
    inline nor separate), defer to POM extender for inline method-body
    placement instead of warning noisily."""
    import logging

    tasks = [_LocatorTask(
        constant_name="btnSubmit",
        intent="submit button",
        owning_page="UnknownPage",
        locator_file=None,
        location_pattern=None,
    )]
    caplog.set_level(logging.INFO)
    count = _write_tbd_locators(tasks, tmp_path, "typescript")
    assert count == 0
    messages = [rec.message for rec in caplog.records]
    assert any(
        "tbd_locator_no_source_defer" in m for m in messages
    ), f"expected no-source-defer INFO log; got {messages!r}"


def test_write_tbd_locators_dedupes_deferral_across_invocations(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A shared ``deferral_seen`` set suppresses the
    ``tbd_locator_deferred_to_extender`` log for a repeated (constant, file)
    across the multiple times ``_write_tbd_locators`` runs per run (Phase A2
    + Phase A3.25 re-assert × MAX_ATTEMPTS). Without the set, every call
    re-logs — preserving legacy behaviour for callers that don't pass one."""
    import logging

    pom_file = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom_file.parent.mkdir(parents=True, exist_ok=True)
    pom_file.write_text("export class TrialPage {}\n", encoding="utf-8")
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent checkbox",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="separate_class",
    )]

    caplog.set_level(logging.INFO)
    seen: set[tuple[str, str]] = set()
    # Simulate the 4 per-run invocations (2 phases × 2 attempts).
    for _ in range(4):
        _write_tbd_locators(
            tasks, tmp_path, "typescript", deferral_seen=seen,
        )
    deferrals = [
        r for r in caplog.records
        if "tbd_locator_deferred_to_extender" in r.message
    ]
    assert len(deferrals) == 1, (
        f"expected the deferral logged once with a shared set; "
        f"got {len(deferrals)}"
    )

    # No set → legacy behaviour: every invocation re-logs.
    caplog.clear()
    for _ in range(4):
        _write_tbd_locators(tasks, tmp_path, "typescript")
    deferrals = [
        r for r in caplog.records
        if "tbd_locator_deferred_to_extender" in r.message
    ]
    assert len(deferrals) == 4


def test_build_locator_tasks_matches_inline_by_owning_pom(tmp_path: Path):
    """`_build_locator_tasks` resolves via `owning_pom` when the SUT uses
    inline patterns — NOT just `{Page}Locators` name-lookup."""
    from qtea.steps.s08_codegen import _build_locator_tasks

    plan = {
        "test_cases": [{
            "id": "TC-1",
            "locators": [{
                "source": "create_tbd",
                "name": "btnSendForApproval",
                "intent": "send for approval button",
                "owning_page": "EntityFormPage",
            }],
        }],
    }
    inventory = {
        "modules": [{
            "name": "sut", "path": ".", "language": "typescript",
            "existing_locators": [{
                "class_name": "EntityFormPage",
                "file": "src/pages/EntityFormPage.ts",
                "location_pattern": "inline_object_property",
                "owning_pom": "EntityFormPage",
                "container_name": "elements",
            }],
        }],
        "active_module": "sut",
    }
    tasks = _build_locator_tasks(plan, inventory)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.location_pattern == "inline_object_property"
    assert t.locator_file == "src/pages/EntityFormPage.ts"
    assert t.container_name == "elements"


def test_build_locator_tasks_backwards_compat_separate_class(tmp_path: Path):
    """Legacy `{Page}Locators` name-lookup still works for SUTs using the
    Python-Selenium convention."""
    from qtea.steps.s08_codegen import _build_locator_tasks

    plan = {
        "test_cases": [{
            "id": "TC-1",
            "locators": [{
                "source": "create_tbd",
                "name": "EMAIL_INPUT",
                "intent": "email input",
                "owning_page": "LoginPage",
            }],
        }],
    }
    inventory = {
        "modules": [{
            "name": "sut", "path": ".", "language": "python",
            "existing_locators": [{
                "class_name": "LoginPageLocators",
                "file": "src/locators/login.py",
            }],
        }],
        "active_module": "sut",
    }
    tasks = _build_locator_tasks(plan, inventory)
    assert len(tasks) == 1
    assert tasks[0].locator_file == "src/locators/login.py"


def test_verify_tbd_compliance_passes_on_tbd_sentinel(tmp_path: Path):
    """The happy path: extender wrote ``KEY: tbd("intent")`` — no violations."""
    from qtea.steps.s08_codegen import _verify_tbd_compliance

    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        'import { tbd } from "./qtea-runtime";\n'
        "export const TrialPageSelectors = {\n"
        '  Existing: "//x",\n'
        '  CHECKBOX_MARKETING_CONSENT: tbd("marketing consent"),\n'
        "};\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="export_const_object",
        container_class_name="TrialPageSelectors",
    )]
    violations = _verify_tbd_compliance(tasks, tmp_path)
    assert violations == []


def test_verify_tbd_compliance_passes_on_dev_locator_match(tmp_path: Path):
    """A raw string matching a dev-locator selector is compliant."""
    from qtea.steps.s08_codegen import _verify_tbd_compliance

    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "export const TrialPageSelectors = {\n"
        '  Existing: "//x",\n'
        "  CHECKBOX_MARKETING_CONSENT: \"[data-testid='marketing-consent']\",\n"
        "};\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        intent="marketing consent",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="export_const_object",
        container_class_name="TrialPageSelectors",
    )]
    dev = {"CHECKBOX_MARKETING_CONSENT": DevLocator(
        constant_name="CHECKBOX_MARKETING_CONSENT",
        selector="[data-testid='marketing-consent']",
    )}
    violations = _verify_tbd_compliance(tasks, tmp_path, dev_locators=dev)
    assert violations == []


def test_verify_tbd_compliance_fails_on_invented_xpath(tmp_path: Path):
    """Regression for the exact 20260708-121117-99f5ed failure — the
    pom-extender wrote a raw XPath under the constant name instead of
    a tbd() sentinel. Gate MUST flag it with both the constant name
    and (truncated) selector value in the message."""
    from qtea.steps.s08_codegen import _verify_tbd_compliance

    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "export const TrialPageSelectors = {\n"
        '  Existing: "//x",\n'
        "  MarketingConsentCheckbox: \"//div[contains(@class,'m-form-field "
        "m-form-field--checkbox')]//input[@type='checkbox'][last()]\",\n"
        "};\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="MarketingConsentCheckbox",
        intent="marketing consent checkbox on trial form",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="export_const_object",
        container_class_name="TrialPageSelectors",
    )]
    violations = _verify_tbd_compliance(tasks, tmp_path)
    assert len(violations) == 1
    msg = violations[0]
    assert "MarketingConsentCheckbox" in msg
    assert "//div" in msg  # raw selector value visible in the message
    assert "RCA-B" in msg  # points reader at the fix batch context


def test_verify_tbd_compliance_flags_missing_constant(tmp_path: Path):
    """When the pom-extender simply omitted the constant, still fail loud."""
    from qtea.steps.s08_codegen import _verify_tbd_compliance

    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "export const TrialPageSelectors = { Existing: \"//x\" };\n",
        encoding="utf-8",
    )
    tasks = [_LocatorTask(
        constant_name="MISSING_KEY",
        intent="something",
        owning_page="TrialPage",
        locator_file="src/pages/TrialPage.ts",
        location_pattern="export_const_object",
        container_class_name="TrialPageSelectors",
    )]
    violations = _verify_tbd_compliance(tasks, tmp_path)
    assert len(violations) == 1
    assert "not found" in violations[0]


def test_verify_tbd_compliance_skips_tasks_without_locator_file(tmp_path: Path):
    """Tasks with no inventory-resolved file are handled by the extender's
    inline-in-method-body path — the compliance gate skips them here."""
    from qtea.steps.s08_codegen import _verify_tbd_compliance

    tasks = [_LocatorTask(
        constant_name="INLINE_ONLY",
        intent="inline",
        owning_page="UnknownPage",
        locator_file=None,
    )]
    assert _verify_tbd_compliance(tasks, tmp_path) == []


def test_build_locator_tasks_resolves_page_to_selectors_const_by_name(
    tmp_path: Path,
):
    """Fix batch 2026-07: ``_resolve()`` must match ``{OwningPage}Selectors``
    (and ``…Elements``) in addition to the historical ``…Locators``. The
    failing run 20260708-121117-99f5ed had ``TrialPage`` in the plan and
    ``TrialPageSelectors`` in the inventory but no ``owning_pom`` field on
    the entry — the old resolver returned None and every marketing-consent
    task got deferred to the LLM, which invented raw XPath.
    """
    from qtea.steps.s08_codegen import _build_locator_tasks

    plan = {
        "test_cases": [{
            "id": "TC-1",
            "locators": [{
                "source": "create_tbd",
                "name": "CHECKBOX_MARKETING_CONSENT",
                "intent": "marketing consent checkbox",
                "owning_page": "TrialPage",
            }],
        }],
    }
    inventory = {
        "modules": [{
            "name": "sut", "path": ".", "language": "typescript",
            "existing_locators": [{
                "class_name": "TrialPageSelectors",
                "file": "src/pages/TrialPage.ts",
                "location_pattern": "export_const_object",
                # NOTE: no owning_pom — this is the failing-run shape
            }],
        }],
        "active_module": "sut",
    }
    tasks = _build_locator_tasks(plan, inventory)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.locator_file == "src/pages/TrialPage.ts"
    assert t.location_pattern == "export_const_object"
    assert t.container_class_name == "TrialPageSelectors"


# ---------------------------------------------------------------------------
# _filter_index_to_qtea with include parameter
# ---------------------------------------------------------------------------


def test_filter_index_include_non_qtea(tmp_path: Path):
    from qtea.test_indexer import IndexResult, SupportFileEntry, TBDMarker

    idx = IndexResult(
        framework="pytest",
        test_root=str(tmp_path),
        files=["qteaest.py", "chat_page_locators.py"],
        tests=[],
        violations=[],
        support_files=[
            SupportFileEntry(
                name="chat_page_locators",
                file="chat_page_locators.py",
                kind="locators",
                tbd_markers=[TBDMarker(line=10, raw="tbd(x)", context="...", description="x")],
            ),
        ],
    )
    # Without include: non-qtea support file is dropped
    filtered = _filter_index_to_qtea(idx, tmp_path)
    assert len(filtered.support_files) == 0
    assert len(filtered.files) == 1

    # With include: non-qtea support file is kept
    inc = {(tmp_path / "chat_page_locators.py").resolve()}
    filtered2 = _filter_index_to_qtea(idx, tmp_path, include=inc)
    assert len(filtered2.support_files) == 1
    assert len(filtered2.files) == 2


# ---------------------------------------------------------------------------
# Framework ↔ test-command consistency check (Change 2)
# ---------------------------------------------------------------------------


def test_parse_test_command_head_strips_wrappers():
    assert _parse_test_command_head("uv run pytest -x") == "pytest"
    assert _parse_test_command_head("poetry run pytest tests/") == "pytest"
    assert _parse_test_command_head("npx playwright test") == "playwright test"
    assert _parse_test_command_head("npm run test") is None  # `test` script name; cannot classify
    assert _parse_test_command_head("./mvnw test") == "mvnw"
    assert _parse_test_command_head("./gradlew test") == "gradlew"
    assert _parse_test_command_head("mvn test") == "mvn"
    assert _parse_test_command_head("robot tests/") == "robot"
    assert _parse_test_command_head("cypress run --headless") == "cypress run"
    assert _parse_test_command_head("") is None
    assert _parse_test_command_head(None) is None


def test_framework_mismatch_message_consistent():
    assert _framework_mismatch_message("pytest", "pytest") is None
    assert _framework_mismatch_message("playwright-py", "pytest") is None
    assert _framework_mismatch_message("playwright-ts", "playwright test") is None
    assert _framework_mismatch_message("selenium-java", "mvn") is None


def test_framework_mismatch_message_skips_when_unverifiable():
    assert _framework_mismatch_message(None, "pytest") is None
    assert _framework_mismatch_message("pytest", None) is None
    # Unknown command head → skip (no false positive).
    assert _framework_mismatch_message("pytest", "make") is None


def test_framework_mismatch_message_detects_obvious_misdetection():
    msg = _framework_mismatch_message("pytest", "playwright test")
    assert msg is not None
    assert "pytest" in msg and "playwright test" in msg
    msg2 = _framework_mismatch_message("playwright-ts", "robot")
    assert msg2 is not None


async def test_step08_fails_fast_on_framework_mismatch(tmp_path: Path):
    """Integration: when research.json says detected_stack=pytest but
    commands.test runs `playwright test`, Step 8 must abort in pre-flight
    rather than vendor the wrong runtime."""
    ctx = _ctx(tmp_path, detected_stack="pytest")
    # Overwrite research.json with a deliberately inconsistent command.
    (ctx.workspace.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "r", "sections": [], "detected_stack": "pytest",
            "commands": {"test": "npx playwright test"},
        }),
        encoding="utf-8",
    )

    result = await CodegenStep().run(ctx)
    assert not result.success
    err = result.error or ""
    assert "detected_stack" in err
    assert "playwright test" in err or "playwright-ts" in err


def test_units_by_file_groups_all_classes_per_file() -> None:
    """Regression: multiple reusable units targeting one file must all be kept.

    The old dedup-by-`at` collapsed a file to its first unit, so a file that
    should define 5 Task classes emitted only 1 — and the test imported symbols
    that were never generated. Grouping must retain every distinct class and
    dedup only exact (file, name) repeats across test cases.
    """
    plan = {
        "test_cases": [
            {
                "id": "TC-1",
                "reusable_units": [
                    {"name": "QteaOpenPlansCatalog", "at": "framework/tasks/x.py",
                     "category": "task", "source": "create"},
                    {"name": "QteaSelectSkill", "at": "framework/tasks/x.py",
                     "category": "task", "source": "create"},
                    {"name": "QteaUploadImportFile", "at": "framework/tasks/x.py",
                     "category": "task", "source": "create"},
                    {"name": "QteaImportFunctionAvailable",
                     "at": "framework/questions/x.py",
                     "category": "question", "source": "create"},
                ],
            },
            {
                "id": "TC-2",
                "reusable_units": [
                    # Repeat of a TC-1 unit (must dedup) + a new one.
                    {"name": "QteaOpenPlansCatalog", "at": "framework/tasks/x.py",
                     "category": "task", "source": "create"},
                    {"name": "QteaPlanItemCreated",
                     "at": "framework/questions/x.py",
                     "category": "question", "source": "create"},
                    # `use` units are not generated, only `create`.
                    {"name": "Login", "at": "framework/tasks/login.py",
                     "category": "task", "source": "use"},
                ],
            },
        ],
    }

    by_at = _units_by_file(plan)

    assert set(by_at) == {"framework/tasks/x.py", "framework/questions/x.py"}
    task_names = [u["name"] for u in by_at["framework/tasks/x.py"]]
    assert task_names == [
        "QteaOpenPlansCatalog", "QteaSelectSkill", "QteaUploadImportFile",
    ]
    question_names = {u["name"] for u in by_at["framework/questions/x.py"]}
    assert question_names == {"QteaImportFunctionAvailable", "QteaPlanItemCreated"}


# ---------------------------------------------------------------------------
# TS/JS runtime-import path normalization (Bug 1+2, run 20260709-083909-223772)
# ---------------------------------------------------------------------------


def test_normalize_runtime_import_fixes_nested_pom_hardcoded_path(tmp_path: Path):
    """A POM two directories deep from `tests/` with a hardcoded (wrong)
    `./qtea-runtime` import must be rewritten to the correct relative path.
    This is the exact H2 defect class from run 20260708-121117-99f5ed /
    20260709-083909-223772: a hardcoded example in the agent prompt leaks
    into LLM-authored inline `tbd()` usage regardless of file nesting."""
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    (sut_root / "tests" / "qtea-runtime.js").write_text("", encoding="utf-8")
    pom_dir = sut_root / "src" / "pages"
    pom_dir.mkdir(parents=True)
    pom_path = pom_dir / "LoginPage.ts"
    pom_path.write_text(
        'import { tbd } from "./qtea-runtime";\n\n'
        "export class LoginPage {\n"
        '  readonly submit = tbd("submit button");\n'
        "}\n",
        encoding="utf-8",
    )

    changed = _normalize_runtime_import_in_file(pom_path, sut_root)

    assert changed
    new_text = pom_path.read_text(encoding="utf-8")
    assert 'from "../../tests/qtea-runtime"' in new_text
    assert './qtea-runtime"' not in new_text


def test_normalize_runtime_import_noop_when_already_correct(tmp_path: Path):
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    pom_path = sut_root / "tests" / "LoginPage.ts"
    pom_path.write_text(
        'import { tbd } from "./qtea-runtime";\n', encoding="utf-8",
    )

    changed = _normalize_runtime_import_in_file(pom_path, sut_root)

    assert not changed


def test_normalize_runtime_import_noop_for_non_ts_js_files(tmp_path: Path):
    sut_root = tmp_path
    py_path = sut_root / "pom.py"
    py_path.write_text(
        'from tests.qtea_runtime import tbd\n', encoding="utf-8",
    )

    assert not _normalize_runtime_import_in_file(py_path, sut_root)


def test_normalize_runtime_imports_sweeps_multiple_files(tmp_path: Path):
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    nested = sut_root / "src" / "pages"
    nested.mkdir(parents=True)
    a = nested / "A.ts"
    a.write_text('import { tbd } from "./qtea-runtime";\n', encoding="utf-8")
    b = nested / "B.ts"
    b.write_text('const { tbd } = require("./qtea-runtime");\n', encoding="utf-8")

    fixed = _normalize_runtime_imports([a, b], sut_root)

    assert fixed == 2
    assert '"../../tests/qtea-runtime"' in a.read_text(encoding="utf-8")
    assert '"../../tests/qtea-runtime"' in b.read_text(encoding="utf-8")


def test_register_global_setup_composes_with_existing_setup(tmp_path: Path):
    """If the SUT's own playwright.config.ts already declares a
    `globalSetup`, qtea must compose (wrap both) rather than inject a
    second `globalSetup` key — a duplicate key in a JS object literal
    silently shadows the first (last-key-wins), which would make the
    qtea JIT runtime never load with no error."""
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    auth_setup = sut_root / "auth-setup.ts"
    auth_setup.write_text(
        "export default async function globalSetup() {}\n", encoding="utf-8",
    )
    cfg = sut_root / "playwright.config.ts"
    cfg.write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        '  globalSetup: "./auth-setup",\n'
        "  testDir: './tests',\n"
        "});\n",
        encoding="utf-8",
    )

    result = _register_playwright_test_global_setup(sut_root, "tests/qtea-runtime.js")

    assert result == cfg
    new_text = cfg.read_text(encoding="utf-8")
    # Exactly one globalSetup key remains — pointing at the composed wrapper.
    assert new_text.count("globalSetup:") == 1
    assert "qtea-composed-global-setup" in new_text
    wrapper = sut_root / "tests" / "qtea-composed-global-setup.js"
    assert wrapper.is_file()
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "require(\"../auth-setup\")" in wrapper_text
    assert "require(\"./qtea-runtime\")" in wrapper_text
    # Both requires MUST be cast to `any`, with the JSDoc on the line directly
    # above the declaration whose initializer is the require call — otherwise
    # `tsc --checkJs` re-flags `.default` (TS2339) / arity (TS2554) and the
    # Phase B.6 gate deterministically fails (run 20260709-083909-223772).
    assert wrapper_text.count("/** @type {any} */") == 2
    assert '/** @type {any} */\nconst existing = require("../auth-setup");' in wrapper_text
    assert '/** @type {any} */\nconst qteaRuntime = require("./qtea-runtime");' in wrapper_text
    # config is still forwarded to both callables (the `any` cast makes the
    # extra arg tsc-clean; at runtime the 0-arg qtea runtime ignores it).
    assert "await existingFn(config)" in wrapper_text
    assert "await qteaFn(config)" in wrapper_text


def test_register_global_setup_injects_when_no_existing_key(tmp_path: Path):
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    cfg = sut_root / "playwright.config.ts"
    cfg.write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        "  testDir: './tests',\n"
        "});\n",
        encoding="utf-8",
    )

    result = _register_playwright_test_global_setup(sut_root, "tests/qtea-runtime.js")

    assert result == cfg
    new_text = cfg.read_text(encoding="utf-8")
    assert new_text.count("globalSetup:") == 1
    assert 'globalSetup: "./tests/qtea-runtime"' in new_text


def test_register_global_setup_idempotent_when_already_registered(tmp_path: Path):
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    cfg = sut_root / "playwright.config.ts"
    original = (
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        '  globalSetup: "./tests/qtea-runtime",\n'
        "  testDir: './tests',\n"
        "});\n"
    )
    cfg.write_text(original, encoding="utf-8")

    result = _register_playwright_test_global_setup(sut_root, "tests/qtea-runtime.js")

    assert result == cfg
    assert cfg.read_text(encoding="utf-8") == original


def test_register_global_setup_idempotent_when_composed_wrapper_registered(tmp_path: Path):
    """Regression (run 20260709-083909-223772 Step 8 attempt 2): on a within-run
    retry the config already points at the composed wrapper (the SUT working
    tree is not reset between attempts). The guard must treat that as
    already-registered and NOT re-enter the compose branch — otherwise the
    wrapper is composed against ITSELF (`require("./qtea-composed-global-setup")`),
    which drops the SUT's real globalSetup at runtime and adds a spurious tsc
    error. The old guard keyed only on `"qtea-runtime"`, which the composed path
    does not contain."""
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    cfg = sut_root / "playwright.config.ts"
    original = (
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        '  globalSetup: "./tests/qtea-composed-global-setup",\n'
        "  testDir: './tests',\n"
        "});\n"
    )
    cfg.write_text(original, encoding="utf-8")
    # Post-first-run wrapper on disk, correctly requiring the SUT's real setup.
    wrapper = sut_root / "tests" / "qtea-composed-global-setup.js"
    wrapper.write_text(
        '/** @type {any} */\nconst existing = require("../auth-setup");\n',
        encoding="utf-8",
    )

    result = _register_playwright_test_global_setup(sut_root, "tests/qtea-runtime.js")

    assert result == cfg
    # Config untouched — still one key, still the wrapper.
    assert cfg.read_text(encoding="utf-8") == original
    # Wrapper NOT recomposed against itself; the real setup require survives.
    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert 'require("./qtea-composed-global-setup")' not in wrapper_text
    assert 'require("../auth-setup")' in wrapper_text


# ---------------------------------------------------------------------------
# Bug 3 regression: Step 9->8 regen feedback must not coach a naming fix
# when the real defect was a compile/collection error.
# ---------------------------------------------------------------------------


def test_build_regen_feedback_hint_includes_naming_coaching_for_naming_defect():
    hint = _build_regen_feedback_hint("zero tests matched the filter", "naming_defect")
    assert "qtea_" in hint
    assert "zero tests matched the filter" in hint


def test_build_regen_feedback_hint_omits_naming_coaching_for_collection_error():
    hint = _build_regen_feedback_hint(
        "broken local import: './qtea-runtime' could not be resolved",
        "collection_error",
    )
    assert "qtea_" not in hint
    assert "@pytest.mark" not in hint
    assert "broken local import" in hint


# ---------------------------------------------------------------------------
# Bug 4 regression: a POM regenerated byte-identical to HEAD (so `git diff`
# shows no change) AND not `qtea_`-prefixed (so the filename glob misses it
# too) must still land in the codegen-scope set, or a violation in it never
# reaches the quality gate (sibling of the incident class in run
# 20260708-121117-99f5ed).
# ---------------------------------------------------------------------------


def test_build_all_codegen_files_includes_byte_identical_pom_via_pom_tasks(
    tmp_path: Path,
):
    sut_root = tmp_path
    (sut_root / "src" / "pages").mkdir(parents=True)
    pom_path = sut_root / "src" / "pages" / "LoginPage.ts"
    pom_path.write_text(
        "export class LoginPage {\n"
        "  loc = { user: '//input[@id=\"user\"]' };\n"  # in-scope xpath violation
        "}\n",
        encoding="utf-8",
    )
    pom_tasks = {
        "LoginPage": _PomTask(
            pom_name="LoginPage",
            pom_file="src/pages/LoginPage.ts",
            source="reuse",
        ),
    }

    all_files = _build_all_codegen_files(
        sut_root=sut_root,
        produced_in_sut=[],  # not qtea_-prefixed — glob misses it
        codegen_modified=set(),  # byte-identical to HEAD — git diff misses it
        pom_tasks=pom_tasks,
        test_results=[],
        b55_touched_files=[],
        jit_resolved=set(),
    )

    assert pom_path.resolve() in {p.resolve() for p in all_files}


def test_build_all_codegen_files_includes_test_result_targets_and_b55_touched(
    tmp_path: Path,
):
    sut_root = tmp_path
    (sut_root / "tests").mkdir()
    test_path = sut_root / "tests" / "qtea_login_test.spec.ts"
    test_path.write_text("test('x', async () => {});\n", encoding="utf-8")
    cfg_path = sut_root / "playwright.config.ts"
    cfg_path.write_text("export default {};\n", encoding="utf-8")

    all_files = _build_all_codegen_files(
        sut_root=sut_root,
        produced_in_sut=[],
        codegen_modified=set(),
        pom_tasks={},
        test_results=[("tests/qtea_login_test.spec.ts", True)],
        b55_touched_files=[cfg_path],
        jit_resolved=set(),
    )

    resolved = {p.resolve() for p in all_files}
    assert test_path.resolve() in resolved
    assert cfg_path.resolve() in resolved


def test_build_all_codegen_files_excludes_jit_resolved_paths(tmp_path: Path):
    sut_root = tmp_path
    (sut_root / "src" / "pages").mkdir(parents=True)
    pom_path = sut_root / "src" / "pages" / "Runtime.ts"
    pom_path.write_text("export const x = 1;\n", encoding="utf-8")
    pom_tasks = {
        "Runtime": _PomTask(
            pom_name="Runtime", pom_file="src/pages/Runtime.ts", source="reuse",
        ),
    }

    all_files = _build_all_codegen_files(
        sut_root=sut_root,
        produced_in_sut=[],
        codegen_modified=set(),
        pom_tasks=pom_tasks,
        test_results=[],
        b55_touched_files=[],
        jit_resolved={pom_path.resolve()},
    )

    assert pom_path.resolve() not in {p.resolve() for p in all_files}
