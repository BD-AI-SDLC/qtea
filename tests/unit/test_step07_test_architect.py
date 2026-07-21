"""Step 7 test-automation-architect tests."""

from __future__ import annotations

import json
from pathlib import Path

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import StepContext
from qtea.steps.s07_test_architect import (
    TestArchitectStep,
    _active_module_dict,
    _approved_dirs,
    _inline_reuse_sources,
    _inventory_symbols,
    _path_under_approved,
    _render_plan_markdown,
    _validate_plan_against_inventory,
)
from qtea.workspace import create_workspace

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
    assert "tests/conftest.py" in syms["fixtures"]  # file-only ref


def test_inventory_symbols_strips_parenthetical_annotations():
    am = {
        "existing_fixtures": [
            {"name": "test (extended)", "file": "src/fixtures/pageFixtures.ts"},
        ],
        "auth_flow": {"fixture_entry": "src/fixtures/pageFixtures.ts:test"},
    }
    syms = _inventory_symbols(am)
    assert "src/fixtures/pageFixtures.ts:test (extended)" in syms["fixtures"]
    assert "test (extended)" in syms["fixtures"]
    assert "src/fixtures/pageFixtures.ts:test" in syms["fixtures"]
    assert "test" in syms["fixtures"]
    assert "src/fixtures/pageFixtures.ts" in syms["fixtures"]


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
    assert _path_under_approved("tests/e2e/qtea_login_test.py", approved)
    assert _path_under_approved("./src/pages/qtea_login_page.py", approved)
    assert _path_under_approved("tests\\e2e\\qtea_e2e_test.py", approved)
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
            "test_file_target": "tests/qtea_login_test.py",
            "test_functions": [{"name": "test_login", "markers": ["qtea_smoke"]}],
            "fixtures": [{"name": "phantom", "source": "reuse", "from": "tests/conftest.py:phantom"}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("phantom" in v and "not found" in v for v in violations)


def _ui_am() -> dict:
    """Active-module inventory for a UI SUT that exposes an open + login method."""
    return {
        "language": "typescript",
        "test_directory_layout": {"default_target": "tests"},
        "auth_flow": {
            "open_method": "src/pages/BasePage.ts:BasePage.openBaseURL",
            "entry_method": "src/pages/BasePage.ts:BasePage.logIn",
            "fixture_entry": None,
        },
    }


def test_ui_gate_rejects_login_without_preceding_open():
    """A UI test that logs in with no open/navigate call before it is the
    'blank page' defect — the gate must flag it."""
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
                {"order": 2, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _ui_am())
    assert any("open/navigate" in v for v in violations)


def test_ui_gate_accepts_open_then_login_in_arrange_steps():
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "BasePage", "method": "openBaseURL"},
                {"order": 2, "phase": "arrange", "pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
                {"order": 3, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _ui_am())
    assert not any("open/navigate" in v for v in violations)


def test_ui_gate_accepts_before_each_hook_with_open_then_login():
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "create", "calls": [
                {"pom": "BasePage", "method": "openBaseURL"},
                {"pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
            ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _ui_am())
    assert not any("open/navigate" in v for v in violations)


def test_ui_gate_trusts_reused_before_each_hook():
    """A reused before_each replays the SUT's own open+login sequence; the gate
    trusts it even when calls[] is not spelled out."""
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/EntityFormSmoke.spec.ts", "calls": []}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
                {"order": 2, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _ui_am())
    assert not any("open/navigate" in v for v in violations)


def _nav_precondition_am() -> dict:
    """Active-module inventory with a navigation precondition on a reused
    grid/filter POM method, mirroring the entity-form/directory-page
    real-SUT case that motivated this gate."""
    am = _ui_am()
    am["navigation_preconditions"] = [
        {
            "method": "DirectoryPage.selectFilteredEntity",
            "requires_call": "BasePage.selectLoginOptionByText",
            "requires_args_hint": "NAV_OPTIONS.DIRECTORY",
            "evidence": "tests/EntityFormSmoke.spec.ts:104",
        }
    ]
    return am


def test_nav_precondition_gate_rejects_missing_required_call():
    """Reusing a flagged method with no prior required call is the 'wrong
    screen' defect — the gate must flag it."""
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse", "calls": [
                {"pom": "BasePage", "method": "openBaseURL"},
                {"pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
            ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "DirectoryPage",
                 "method": "selectFilteredEntity", "args": ["foo"]},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _nav_precondition_am())
    assert any("navigation_preconditions" in v for v in violations)


def test_nav_precondition_gate_accepts_required_call_in_steps():
    """The required call earlier in steps[] (same test function) satisfies
    the precondition — no violation."""
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse", "calls": [
                {"pom": "BasePage", "method": "openBaseURL"},
                {"pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
            ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "BasePage",
                 "method": "selectLoginOptionByText", "args": ["NAV_OPTIONS.DIRECTORY"]},
                {"order": 2, "phase": "arrange", "pom": "DirectoryPage",
                 "method": "selectFilteredEntity", "args": ["foo"]},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _nav_precondition_am())
    assert not any("navigation_preconditions" in v for v in violations)


def test_nav_precondition_gate_accepts_required_call_in_before_each_hook():
    """The required call already present in the before_each hook satisfies
    the precondition — no violation."""
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse", "calls": [
                {"pom": "BasePage", "method": "openBaseURL"},
                {"pom": "BasePage", "method": "logIn", "args": ["U", "P"]},
                {"pom": "BasePage", "method": "selectLoginOptionByText", "args": ["NAV_OPTIONS.DIRECTORY"]},
            ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "arrange", "pom": "DirectoryPage",
                 "method": "selectFilteredEntity", "args": ["foo"]},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, _nav_precondition_am())
    assert not any("navigation_preconditions" in v for v in violations)


def test_validate_plan_rejects_bad_marker():
    am = {"test_directory_layout": {"default_target": "tests"}}
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_wrong"]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("qtea_wrong" in v for v in violations)


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
            "test_file_target": "tests/qtea_login_test.py",
            "test_functions": [{"name": "test_login", "markers": ["qtea_smoke"], "uses_fixtures": ["auth"]}],
            "fixtures": [{
                "name": "auth", "source": "reuse",
                "from": "tests/conftest.py:auth",
                "reuse_justification": "yields authenticated Page on /dashboard",
            }],
            "page_objects": [{
                "name": "LoginPage", "source": "reuse",
                "from": "src/pages/login.py",
                "reuse_justification": "models /login route with submit()",
            }],
            "locators": [{
                "name": "LOGIN_BTN", "owning_page": "LoginPage",
                "source": "create_tbd", "intent": "sign in button",
            }],
        }],
    }
    assert _validate_plan_against_inventory(plan, am) == []


def _screenplay_plan() -> dict:
    """A Screenplay plan whose choreography references reusable_units (Tasks/
    Questions), not page_objects — the shape that broke run
    20260715-075512-f2dbad."""
    return {
        "plan_version": "1.0",
        "active_module": "sut",
        "language": "python",
        "architecture_pattern": "screenplay",
        "test_cases": [{
            "id": "TC-IMPCOST-001",
            "test_file_target": "framework/tests/qtea_import_cost_test.py",
            "test_functions": [{
                "name": "test_import_cost",
                "markers": ["qtea_regression"],
                "steps": [
                    {"order": 1, "pom": "OpenPlansCatalog", "method": "perform_as", "phase": "act"},
                    {"order": 2, "pom": "CountCreatedPlanItems", "method": "answered_by", "phase": "assert"},
                ],
            }],
            "reusable_units": [
                {"name": "OpenPlansCatalog", "source": "create", "category": "task",
                 "at": "framework/tasks/open_plans_catalog.py",
                 "missing_behaviors": [{"name": "perform_as", "signature": "perform_as(self, actor)", "kind": "action"}]},
                {"name": "CountCreatedPlanItems", "source": "create", "category": "question",
                 "at": "framework/questions/count_created_plan_items.py",
                 "missing_behaviors": [{"name": "answered_by", "signature": "answered_by(self, actor)", "kind": "query"}]},
            ],
        }],
    }


def test_choreography_gate_accepts_screenplay_reusable_units():
    """Regression: steps referencing reusable_units (task/question) must NOT be
    rejected as `planned: none` just because page_objects is empty."""
    am = {
        "architecture_pattern": "screenplay",
        "existing_page_objects": [],
        "test_directory_layout": {"base_dir": "framework/tests"},
        "src_directory_layout": {},
        "pattern_exemplars": [
            {"category": "task", "dir": "framework/tasks", "file": "framework/tasks/login.py"},
            {"category": "question", "dir": "framework/questions", "file": "framework/questions/title.py"},
        ],
    }
    assert _validate_plan_against_inventory(_screenplay_plan(), am) == []


def test_choreography_gate_still_flags_dangling_screenplay_ref():
    """The gate must still catch a step referencing a unit absent from
    reusable_units — the fix broadens the planned set, it doesn't disable it."""
    am = {
        "architecture_pattern": "screenplay",
        "existing_page_objects": [],
        "test_directory_layout": {"base_dir": "framework/tests"},
        "src_directory_layout": {},
        "pattern_exemplars": [{"category": "task", "dir": "framework/tasks", "file": "framework/tasks/login.py"}],
    }
    plan = _screenplay_plan()
    plan["test_cases"][0]["test_functions"][0]["steps"][0]["pom"] = "GhostTask"
    violations = _validate_plan_against_inventory(plan, am)
    assert any("GhostTask" in v and "not planned" in v for v in violations)


def test_validate_plan_rejects_missing_reuse_justification():
    """The phase gate must flag a reuse entry that omits reuse_justification."""
    am = {
        "test_directory_layout": {"default_target": "tests"},
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py"}],
    }
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
            "fixtures": [{
                "name": "auth", "source": "reuse",
                "from": "tests/conftest.py:auth",
                # reuse_justification intentionally absent
            }],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("reuse_justification" in v and "auth" in v for v in violations)


def test_validate_plan_rejects_whitespace_reuse_justification():
    """Whitespace-only justification counts as missing — empty rationale is
    indistinguishable from no rationale for review purposes."""
    am = {
        "test_directory_layout": {"default_target": "tests"},
        "src_directory_layout": {"pages_object_dir": "src/pages"},
        "existing_page_objects": [{"name": "LoginPage", "file": "src/pages/login.py"}],
    }
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
            "page_objects": [{
                "name": "LoginPage", "source": "reuse",
                "from": "src/pages/login.py",
                "reuse_justification": "   \n  ",
            }],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("reuse_justification" in v and "LoginPage" in v for v in violations)


def test_validate_plan_rejects_locator_reuse_missing_justification():
    am = {
        "test_directory_layout": {"default_target": "tests"},
        "src_directory_layout": {"pages_object_dir": "src/pages"},
        "existing_locators": [{
            "class_name": "LoginLocators",
            "file": "src/pages/locators/login.py",
            "constants": [{"name": "EMAIL_INPUT", "selector": "#email"}],
        }],
    }
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
            "locators": [{
                "name": "EMAIL_INPUT", "owning_page": "LoginPage",
                "source": "reuse",
                "from": "src/pages/locators/login.py",
                # reuse_justification absent
            }],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any(
        "reuse_justification" in v and "EMAIL_INPUT" in v
        for v in violations
    )


def test_inline_reuse_sources_reads_pom_fixture_helper(tmp_path: Path):
    sut = tmp_path / "sut"
    (sut / "src" / "pages").mkdir(parents=True)
    (sut / "tests").mkdir(parents=True)
    (sut / "src" / "pages" / "login.py").write_text("class LoginPage: pass\n", encoding="utf-8")
    (sut / "tests" / "conftest.py").write_text("def auth(): pass\n", encoding="utf-8")
    (sut / "tests" / "helpers.py").write_text("def wait_for(): pass\n", encoding="utf-8")

    am = {
        "path": ".",
        "existing_page_objects": [{"name": "LoginPage", "file": "src/pages/login.py"}],
        "existing_fixtures": [{"name": "auth", "file": "tests/conftest.py"}],
        "existing_helpers": [{"name": "wait_for", "file": "tests/helpers.py"}],
    }
    sources, skipped = _inline_reuse_sources(am, sut, budget=10_000)
    assert "reuse-source/src/pages/login.py" in sources
    assert "reuse-source/tests/conftest.py" in sources
    assert "reuse-source/tests/helpers.py" in sources
    assert "class LoginPage" in sources["reuse-source/src/pages/login.py"]
    assert skipped == []


def test_inline_reuse_sources_alphabetical_and_budget_capped(tmp_path: Path):
    """Files are read in alphabetical order; anything past budget is skipped."""
    sut = tmp_path / "sut"
    (sut / "src").mkdir(parents=True)
    # Each file ~600 chars; budget of 1000 should fit ONE (600) and skip the
    # remaining two. Alphabetical order: a.py, b.py, c.py.
    (sut / "src" / "a.py").write_text("a" * 600, encoding="utf-8")
    (sut / "src" / "b.py").write_text("b" * 600, encoding="utf-8")
    (sut / "src" / "c.py").write_text("c" * 600, encoding="utf-8")

    am = {
        "path": ".",
        "existing_page_objects": [
            {"name": "B", "file": "src/b.py"},
            {"name": "C", "file": "src/c.py"},
            {"name": "A", "file": "src/a.py"},
        ],
    }
    sources, skipped = _inline_reuse_sources(am, sut, budget=1000)
    assert list(sources.keys()) == ["reuse-source/src/a.py"]
    assert sorted(skipped) == ["src/b.py", "src/c.py"]


def test_inline_reuse_sources_handles_module_subpath(tmp_path: Path):
    """For monorepo modules with path != '.', file paths are joined correctly."""
    sut = tmp_path / "sut"
    module_root = sut / "frontend"
    (module_root / "src" / "pages").mkdir(parents=True)
    (module_root / "src" / "pages" / "login.py").write_text(
        "class LoginPage: pass\n", encoding="utf-8",
    )
    am = {
        "path": "frontend",
        "existing_page_objects": [
            {"name": "LoginPage", "file": "src/pages/login.py"},
        ],
    }
    sources, skipped = _inline_reuse_sources(am, sut)
    assert "reuse-source/frontend/src/pages/login.py" in sources
    assert skipped == []


def test_inline_reuse_sources_marks_missing_files_in_skipped(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    am = {
        "path": ".",
        "existing_page_objects": [{"name": "Ghost", "file": "src/ghost.py"}],
    }
    sources, skipped = _inline_reuse_sources(am, sut)
    assert sources == {}
    assert any("ghost" in s.lower() and "not found" in s for s in skipped)


def test_inline_reuse_sources_returns_empty_when_no_active_module():
    sources, skipped = _inline_reuse_sources(None, Path("/nonexistent"))
    assert sources == {}
    assert skipped == []


def test_inline_reuse_sources_respects_env_budget(tmp_path: Path, monkeypatch):
    sut = tmp_path / "sut"
    (sut / "src").mkdir(parents=True)
    (sut / "src" / "big.py").write_text("x" * 5000, encoding="utf-8")
    am = {
        "path": ".",
        "existing_page_objects": [{"name": "Big", "file": "src/big.py"}],
    }
    monkeypatch.setenv("QTEA_REUSE_SOURCE_BUDGET", "100")
    sources, skipped = _inline_reuse_sources(am, sut)
    assert sources == {}
    assert skipped == ["src/big.py"]


def test_validate_plan_helper_reuse_requires_justification_and_from():
    am = {"test_directory_layout": {"default_target": "tests"}}
    plan = {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
            "helpers": [{"name": "wait_for", "source": "reuse"}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    msgs = " | ".join(violations)
    assert "wait_for" in msgs
    assert "from" in msgs
    assert "reuse_justification" in msgs


# --- auth-chaining depends_on tests ----------------------------------------

def _am_with_auth(fixture_entry="tests/conftest.py:chat_page"):
    return {
        "test_directory_layout": {"default_target": "tests"},
        "auth_flow": {"type": "sso", "fixture_entry": fixture_entry},
        "existing_fixtures": [{"name": "chat_page", "file": "tests/fixtures/chat_setup.py"}],
    }


def _plan_with_create_fixture(yields="ChatPage", depends_on=None):
    fix = {
        "name": "mobile_chat_page", "source": "create",
        "at": "tests/fixtures/qtea_fixtures.py",
    }
    if yields is not None:
        fix["yields"] = yields
    if depends_on is not None:
        fix["depends_on"] = depends_on
    return {
        "plan_version": "1.0",
        "active_module": "x",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/x.py",
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
            "fixtures": [fix],
        }],
    }


def test_validate_plan_rejects_create_fixture_missing_auth_depends_on():
    violations = _validate_plan_against_inventory(
        _plan_with_create_fixture(yields="ChatPage"),
        _am_with_auth(),
    )
    assert any("depends_on" in v and "chat_page" in v for v in violations)


def test_validate_plan_passes_create_fixture_with_auth_depends_on():
    violations = _validate_plan_against_inventory(
        _plan_with_create_fixture(yields="ChatPage", depends_on=["chat_page"]),
        _am_with_auth(),
    )
    assert not any("depends_on" in v for v in violations)


def test_validate_plan_skips_auth_check_when_no_auth_flow():
    am = {"test_directory_layout": {"default_target": "tests"}}
    violations = _validate_plan_against_inventory(
        _plan_with_create_fixture(yields="ChatPage"),
        am,
    )
    assert not any("depends_on" in v for v in violations)


def test_validate_plan_skips_auth_check_for_primitive_yields():
    violations = _validate_plan_against_inventory(
        _plan_with_create_fixture(yields="dict"),
        _am_with_auth(),
    )
    assert not any("depends_on" in v for v in violations)


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
        "test_file_target": "tests/qtea_login_test.py",
        "test_functions": [{
            "name": "test_login_with_valid_credentials",
            "markers": ["qtea_smoke"],
            "uses_fixtures": ["auth"],
        }],
        "fixtures": [{
            "name": "auth", "source": "reuse",
            "from": "tests/conftest.py:auth",
            "reuse_justification": "yields authenticated Page on /dashboard",
        }],
        "page_objects": [{
            "name": "LoginPage", "source": "reuse",
            "from": "src/pages/login.py",
            "reuse_justification": "models /login with submit()",
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
    (step4 / "test-design.md").write_text(
        "# Test Design\n\n## TC-LOGIN-1 — Log in\n", encoding="utf-8",
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
    assert "test-design" in (result.error or "")


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
    """Inputs (test-design.md, sut_inventory.json) are inlined into the
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
    assert "test-design.md" in user_content


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
            "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
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
    assert "tests/qtea_login_test.py" in md
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


# ---------------------------------------------------------------------------
# _validate_plan_against_inventory — path normalization (Bug 3)
# ---------------------------------------------------------------------------


def _make_tc(*, fixtures=None, page_objects=None, locators=None):
    """Minimal test-case dict for validation tests."""
    return {
        "id": "TC-1",
        "test_file_target": "tests/qtea_x_test.py",
        "test_functions": [{"name": "test_x", "markers": ["qtea_smoke"]}],
        "fixtures": fixtures or [],
        "page_objects": page_objects or [],
        "locators": locators or [],
    }


def _make_plan(tc):
    return {"plan_version": "1.0", "active_module": "sut", "test_cases": [tc]}


def _make_am(*, fixtures=None, page_objects=None):
    am = {
        "test_directory_layout": {"default_target": "tests"},
        "existing_fixtures": fixtures or [],
        "existing_page_objects": page_objects or [],
        "existing_helpers": [],
        "existing_locators": [],
    }
    return am


def test_validate_backslash_ref_matches_posix_inventory():
    """Backslash refs from LLM should match POSIX-stored inventory entries."""
    am = _make_am(page_objects=[
        {"name": "LoginPage", "file": "src/pages/LoginPage.ts"},
    ])
    tc = _make_tc(page_objects=[{
        "name": "LoginPage", "source": "reuse",
        "from": "src\\pages\\LoginPage.ts:LoginPage",
        "reuse_justification": "models /login route",
    }])
    violations = _validate_plan_against_inventory(_make_plan(tc), am)
    reuse_violations = [v for v in violations if "not found in sut_inventory" in v]
    assert not reuse_violations, f"backslash ref should match: {reuse_violations}"


def test_validate_posix_ref_still_works():
    """Forward-slash refs continue to work as before."""
    am = _make_am(page_objects=[
        {"name": "LoginPage", "file": "src/pages/LoginPage.ts"},
    ])
    tc = _make_tc(page_objects=[{
        "name": "LoginPage", "source": "reuse",
        "from": "src/pages/LoginPage.ts:LoginPage",
        "reuse_justification": "models /login route",
    }])
    violations = _validate_plan_against_inventory(_make_plan(tc), am)
    reuse_violations = [v for v in violations if "not found in sut_inventory" in v]
    assert not reuse_violations


# ---------------------------------------------------------------------------
# _validate_plan_against_inventory — Qtea-prefix guard (Bug 4)
# ---------------------------------------------------------------------------


def test_validate_rejects_qtea_prefixed_page_object_as_reuse():
    am = _make_am()
    tc = _make_tc(page_objects=[{
        "name": "QteaNotificationPage", "source": "reuse",
        "from": "src/pages/QteaNotificationPage.ts",
        "reuse_justification": "models notifications",
    }])
    violations = _validate_plan_against_inventory(_make_plan(tc), am)
    assert any("Qtea" in v and "prefix" in v for v in violations)


def test_validate_rejects_qtea_prefixed_fixture_as_reuse():
    am = _make_am()
    tc = _make_tc(fixtures=[{
        "name": "qtea_custom_login", "source": "reuse",
        "from": "tests/conftest.py:qtea_custom_login",
        "reuse_justification": "yields auth session",
    }])
    violations = _validate_plan_against_inventory(_make_plan(tc), am)
    assert any("qtea_" in v and "prefix" in v for v in violations)


def test_validate_allows_qtea_prefixed_with_create():
    am = _make_am()
    tc = _make_tc(page_objects=[{
        "name": "QteaNotificationPage", "source": "create",
        "at": "tests/pages/qtea_notification_page.py",
    }])
    violations = _validate_plan_against_inventory(_make_plan(tc), am)
    prefix_violations = [v for v in violations if "prefix" in v]
    assert not prefix_violations
