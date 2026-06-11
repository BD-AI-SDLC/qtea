"""Step 7 test-architect tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s07_test_architect import (
    TestArchitectStep,
    _active_module_dict,
    _approved_dirs,
    _inventory_symbols,
    _path_under_approved,
    _render_plan_markdown,
    _validate_plan_against_inventory,
)
from worca_t.workspace import create_workspace

from ._fake_anthropic import (
    disable_vertex_env,
    enable_vertex_env,
    install_fake_anthropic,
)
from ._sut_setup import seed_sut


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_active_module_dict_returns_matching_entry():
    inv = {
        "active_module": "frontend",
        "modules": [
            {"name": "backend"},
            {"name": "frontend", "language": "typescript"},
        ],
    }
    am = _active_module_dict(inv)
    assert am is not None
    assert am["language"] == "typescript"


def test_active_module_dict_none_when_unresolved():
    assert _active_module_dict({"active_module": None, "modules": []}) is None
    assert _active_module_dict({"modules": [{"name": "x"}]}) is None


def test_inventory_symbols_indexes_all_categories():
    am = {
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py"}],
        "existing_page_objects": [
            {"name": "LoginPage", "file": "src/pages/login.py", "methods": ["submit"]}
        ],
        "existing_helpers": [{"name": "wait_for", "file": "tests/helpers.py"}],
        "existing_locators": [
            {
                "class_name": "LoginLocators",
                "file": "src/pages/locators/login.py",
                "constants": [{"name": "LOGIN_BUTTON", "selector": "#submit"}],
            }
        ],
    }
    syms = _inventory_symbols(am)
    assert "tests/conftest.py:auth" in syms["fixtures"]
    assert "auth" in syms["fixtures"]
    assert "LoginPage" in syms["page_objects"]
    assert "src/pages/login.py" in syms["page_objects"]
    assert "wait_for" in syms["helpers"]
    assert "LOGIN_BUTTON" in syms["locators"]
    assert "LoginLocators" in syms["locators"]


def test_approved_dirs_pulls_from_test_and_src_layouts():
    am = {
        "test_directory_layout": {
            "base_dir": "tests",
            "default_target": "tests/e2e",
            "subdirs": [{"path": "tests/unit"}],
        },
        "src_directory_layout": {
            "pages_object_dir": "src/pages",
            "pages_locators_dir": "src/pages/locators",
            "helpers_dir": "src/helpers",
        },
    }
    dirs = _approved_dirs(am)
    assert "tests" in dirs
    assert "tests/e2e" in dirs
    assert "tests/unit" in dirs
    assert "src/pages" in dirs


def test_path_under_approved_handles_separators_and_dot_prefix():
    approved = {"tests/e2e", "src/pages"}
    assert _path_under_approved("tests/e2e/worca_test_login.py", approved)
    assert _path_under_approved("./src/pages/worca_login_page.py", approved)
    assert _path_under_approved("tests\\e2e\\worca_test.py", approved)
    assert not _path_under_approved("garden/of_evil.py", approved)


def test_path_under_approved_empty_set_short_circuits_true():
    # When no layout was detected, the gate must not block — better to ship
    # a plan than to crash because the SUT has an unusual structure.
    assert _path_under_approved("anywhere/at/all.py", set())


def test_validate_plan_rejects_unknown_reuse_reference():
    am = {
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py"}],
        "test_directory_layout": {"default_target": "tests"},
    }
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-LOGIN-1",
            "test_file_target": "tests/worca_test_login.py",
            "test_functions": [{"name": "test_login", "markers": ["worca_smoke"]}],
            "fixtures": [{"name": "phantom", "source": "reuse", "from": "tests/conftest.py:phantom"}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("phantom" in v and "not found" in v for v in violations)


def test_validate_plan_rejects_bad_marker():
    am = {"test_directory_layout": {"default_target": "tests"}}
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["worca_wrong"]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("worca_wrong" in v for v in violations)


def test_validate_plan_rejects_oversize_intent():
    am = {"test_directory_layout": {"default_target": "tests"}}
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x"}],
            "locators": [{
                "name": "L1", "owning_page": "P", "source": "create_tbd",
                "intent": "x" * 130,
            }],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("exceeds" in v and "120" in v for v in violations)


def test_validate_plan_rejects_missing_method_without_signature():
    am = {"test_directory_layout": {"default_target": "tests"}}
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x"}],
            "page_objects": [{
                "name": "Page", "source": "create", "at": "tests/page.py",
                "missing_methods": [{"name": "submit"}],
            }],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("no signature" in v for v in violations)


def test_validate_plan_passes_on_well_formed_plan():
    am = {
        "test_directory_layout": {"default_target": "tests"},
        "src_directory_layout": {"pages_object_dir": "src/pages"},
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py"}],
        "existing_page_objects": [
            {"name": "LoginPage", "file": "src/pages/login.py", "methods": ["submit"]}
        ],
    }
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-LOGIN-1",
            "test_file_target": "tests/worca_test_login.py",
            "test_functions": [{"name": "test_login", "markers": ["worca_smoke"], "uses_fixtures": ["auth"]}],
            "fixtures": [{"name": "auth", "source": "reuse", "from": "tests/conftest.py:auth"}],
            "page_objects": [{"name": "LoginPage", "source": "reuse", "from": "src/pages/login.py"}],
            "locators": [{
                "name": "LOGIN_BTN", "owning_page": "LoginPage",
                "source": "create_tbd", "intent": "sign in button",
            }],
        }],
    }
    assert _validate_plan_against_inventory(plan, am) == []


# ---------------------------------------------------------------------------
# Step integration tests
# ---------------------------------------------------------------------------


_GOOD_PLAN = {
    "plan_version": "1.0",
    "active_module": "frontend",
    "language": "python",
    "framework": "pytest",
    "test_cases": [{
        "id": "TC-LOGIN-1",
        "title": "User can log in",
        "test_file_target": "tests/worca_test_login.py",
        "test_functions": [{
            "name": "test_login_with_valid_credentials",
            "markers": ["worca_smoke"],
            "uses_fixtures": ["auth"],
        }],
        "fixtures": [{
            "name": "auth", "source": "reuse",
            "from": "tests/conftest.py:auth",
        }],
        "page_objects": [{
            "name": "LoginPage", "source": "reuse",
            "from": "src/pages/login.py",
        }],
        "locators": [{
            "name": "LOGIN_BTN", "owning_page": "LoginPage",
            "source": "create_tbd", "intent": "sign in button",
        }],
    }],
}


_INVENTORY = {
    "active_module": "frontend",
    "modules": [{
        "name": "frontend",
        "path": ".",
        "language": "python",
        "package_manager": "pip",
        "test_directory_layout": {"default_target": "tests", "base_dir": "tests"},
        "src_directory_layout": {"pages_object_dir": "src/pages"},
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py", "scope": "function"}],
        "existing_page_objects": [{"name": "LoginPage", "file": "src/pages/login.py"}],
    }],
}


def _ctx(tmp_path: Path, *, include_default_inventory: bool = True) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=str(ws.sut),
    )
    opts = PipelineOptions(spec="x", sut=str(ws.sut), workspace_base=tmp_path / ".ws")
    seed_sut(ws, include_default_inventory=include_default_inventory)
    return StepContext(
        workspace=ws, state=state,
        spec_source="x", sut_source=str(ws.sut), options=opts,
    )


def _seed_strategy(ctx: StepContext) -> None:
    step4 = ctx.workspace.step_dir(4)
    step4.mkdir(parents=True, exist_ok=True)
    (step4 / "test-strategy.md").write_text(
        "# Test Strategy\n\n## TC-LOGIN-1 — Log in\n", encoding="utf-8",
    )


def _seed_inventory(ctx: StepContext, inventory: dict | None = None) -> None:
    step6 = ctx.workspace.step_dir(6)
    step6.mkdir(parents=True, exist_ok=True)
    (step6 / "sut_inventory.json").write_text(
        json.dumps(inventory if inventory is not None else _INVENTORY), encoding="utf-8",
    )
    (step6 / "research.md").write_text("# Research\n", encoding="utf-8")


def _seed_upstream(ctx: StepContext) -> None:
    _seed_strategy(ctx)
    _seed_inventory(ctx)


async def test_step07_fails_without_strategy(tmp_path: Path):
    # No strategy seeded — should fail at strategy_md check.
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_inventory(ctx)
    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "test-strategy" in (result.error or "")


async def test_step07_fails_without_sut_inventory(tmp_path: Path):
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_strategy(ctx)
    # No sut_inventory.json seeded.
    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "sut_inventory" in (result.error or "")


async def test_step07_fails_without_active_module(tmp_path: Path):
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_strategy(ctx)
    _seed_inventory(ctx, inventory={"active_module": None, "modules": []})
    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "active_module" in (result.error or "")


async def test_step07_happy_path_writes_plan_and_validates(
    tmp_path: Path, monkeypatch,
):
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    # Direct SDK returns the JSON as response text (structured outputs).
    install_fake_anthropic(monkeypatch, text=json.dumps(_GOOD_PLAN))

    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(7)
    plan = json.loads((out / "code-modification-plan.json").read_text(encoding="utf-8"))
    assert plan["plan_version"] == "1.0"
    assert plan["test_cases"][0]["id"] == "TC-LOGIN-1"
    # Markdown summary is now ALWAYS rendered locally from the JSON.
    md = (out / "code-modification-plan.md").read_text(encoding="utf-8")
    assert "TC-LOGIN-1" in md
    assert "Code Modification Plan" in md


async def test_step07_passes_plan_schema_to_reasoning_llm(
    tmp_path: Path, monkeypatch,
):
    """On the standard Anthropic API, Step 7 enables structured outputs by
    passing the plan schema via ``output_config.format``."""
    disable_vertex_env(monkeypatch)
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(_GOOD_PLAN), on_call=captured.update
    )

    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error

    assert "output_config" in captured, (
        "step 7 must pass a JSON schema to enable structured outputs"
    )
    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert set(schema.get("required", [])) >= {
        "plan_version", "active_module", "test_cases"
    }


async def test_step07_skips_structured_outputs_on_vertex(
    tmp_path: Path, monkeypatch,
):
    """On Vertex backends (Bosch model farm), `output_config` must NOT be
    sent — the org policy blocks the ``structured_outputs`` feature for
    partner Anthropic models. Fallback: prompt-only JSON + local validation.
    """
    enable_vertex_env(monkeypatch)
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(_GOOD_PLAN), on_call=captured.update
    )

    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    assert "output_config" not in captured, (
        "Vertex backend disallows structured outputs; output_config "
        "must be omitted to avoid 400 FAILED_PRECONDITION"
    )


async def test_step07_tolerates_fenced_json_response_on_vertex(
    tmp_path: Path, monkeypatch,
):
    """When the Vertex fallback is in effect, the model may wrap the JSON
    in ```json ... ``` fences despite the prompt instruction. The reasoning
    module strips these before returning, so the step still succeeds."""
    enable_vertex_env(monkeypatch)
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    fenced = f"```json\n{json.dumps(_GOOD_PLAN)}\n```"
    install_fake_anthropic(monkeypatch, text=fenced)

    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(7)
    plan = json.loads((out / "code-modification-plan.json").read_text(encoding="utf-8"))
    assert plan["test_cases"][0]["id"] == "TC-LOGIN-1"


async def test_step07_inlines_inputs_into_user_prompt(
    tmp_path: Path, monkeypatch,
):
    """Inputs (test-strategy.md, sut_inventory.json) are inlined into the
    user message, not staged in a workdir."""
    disable_vertex_env(monkeypatch)
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(_GOOD_PLAN), on_call=captured.update
    )

    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error

    user_content = captured["messages"][-1]["content"]
    # Distinctive marker from _seed_strategy.
    assert "TC-LOGIN-1" in user_content
    assert "sut_inventory.json" in user_content
    assert "test-strategy.md" in user_content


async def test_step07_rejects_schema_invalid_plan(
    tmp_path: Path, monkeypatch,
):
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    # Missing test_cases — the local belt-and-suspenders is_valid check
    # rejects this even if the (mocked) SDK lets it through.
    bad_plan = {"plan_version": "1.0", "active_module": "x"}
    install_fake_anthropic(monkeypatch, text=json.dumps(bad_plan))

    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "schema" in (result.error or "").lower()


async def test_step07_rejects_unparseable_json(
    tmp_path: Path, monkeypatch,
):
    """If the response isn't parseable JSON (e.g. SDK regression bypasses
    structured outputs), the step fails cleanly."""
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    install_fake_anthropic(monkeypatch, text="not json {")

    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "unparseable" in (result.error or "").lower()


async def test_step07_rejects_phase_gate_violation(
    tmp_path: Path, monkeypatch,
):
    ctx = _ctx(tmp_path, include_default_inventory=False)
    _seed_upstream(ctx)

    # Plan with an orphan reuse reference (phantom fixture).
    bad_plan = {
        "plan_version": "1.0",
        "active_module": "frontend",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["worca_smoke"]}],
            "fixtures": [{
                "name": "phantom", "source": "reuse",
                "from": "tests/conftest.py:phantom",
            }],
        }],
    }
    install_fake_anthropic(monkeypatch, text=json.dumps(bad_plan))

    result = await TestArchitectStep().run(ctx)
    assert not result.success
    assert "phase-gate" in (result.error or "")
    log = (ctx.workspace.step_dir(7) / "plan-violations.log").read_text(encoding="utf-8")
    assert "phantom" in log


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def test_render_plan_markdown_includes_test_cases_and_sources():
    md = _render_plan_markdown(_GOOD_PLAN)
    assert "Code Modification Plan" in md
    assert "frontend" in md
    assert "TC-LOGIN-1" in md
    assert "User can log in" in md
    assert "tests/worca_test_login.py" in md
    # Reuse + create_tbd lines render with their source semantics.
    assert "reuse from" in md
    assert "create_tbd" in md
    assert "sign in button" in md


def test_render_plan_markdown_handles_empty_test_cases():
    md = _render_plan_markdown({
        "plan_version": "1.0", "active_module": "x",
        "language": "python", "framework": "pytest",
        "test_cases": [],
    })
    assert "No test cases planned" in md
