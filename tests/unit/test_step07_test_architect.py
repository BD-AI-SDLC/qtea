"""Step 7 test-automation-architect tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

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
    _validate_assertion_oracle,
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


def _ui_am_with_lifecycle_hooks(hooks: list[dict]) -> dict:
    """`_ui_am()` plus a populated `lifecycle_hooks[]`, for the hook-reuse
    staleness gate."""
    am = _ui_am()
    am["lifecycle_hooks"] = hooks
    return am


def test_hook_reuse_sequence_mismatch_flagged():
    am = _ui_am_with_lifecycle_hooks([
        {
            "event": "before_each",
            "file": "tests/RopaEntitySmoke.spec.ts",
            "calls": ["basePage.openBaseURL", "basePage.logIn",
                      "basePage.goToRopaModule", "basePage.selectLoginOptionByText"],
        },
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/RopaEntitySmoke.spec.ts", "calls": [
                           {"pom": "basePage", "method": "openBaseURL"},
                           {"pom": "basePage", "method": "logIn", "args": ["U", "P"]},
                           {"pom": "basePage", "method": "goToRopaModule"},
                       ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("stale relative to" in v for v in violations)


def test_hook_reuse_sequence_match_passes():
    am = _ui_am_with_lifecycle_hooks([
        {
            "event": "before_each",
            "file": "tests/RopaEntitySmoke.spec.ts",
            "calls": ["basePage.openBaseURL", "basePage.logIn",
                      "basePage.goToRopaModule"],
        },
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/RopaEntitySmoke.spec.ts", "calls": [
                           {"pom": "basePage", "method": "openBaseURL"},
                           {"pom": "basePage", "method": "logIn", "args": ["U", "P"]},
                           {"pom": "basePage", "method": "goToRopaModule"},
                       ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert not any("stale relative to" in v for v in violations)


def test_hook_reuse_from_not_in_inventory_flagged():
    am = _ui_am_with_lifecycle_hooks([
        {
            "event": "before_each",
            "file": "tests/OtherSmoke.spec.ts",
            "calls": ["basePage.openBaseURL"],
        },
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/RopaEntitySmoke.spec.ts", "calls": []}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("does not match any sut_inventory.lifecycle_hooks" in v for v in violations)


def test_hook_reuse_missing_from_flagged():
    am = _ui_am_with_lifecycle_hooks([
        {"event": "before_each", "file": "tests/RopaEntitySmoke.spec.ts", "calls": ["basePage.openBaseURL"]},
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse", "from": "", "calls": []}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any("missing `from`" in v for v in violations)


def test_hook_reuse_drops_args_when_inventory_records_them_is_flagged():
    """Args-preservation gate: when the matched inventory hook records
    positional args on a call (new `{method, args}` shape from the
    deterministic miner) and the plan omits `args`, the gate must flag it —
    otherwise Step-8 codegen emits a zero-arg call and reconcile fails as
    `arity_mismatch`."""
    am = _ui_am_with_lifecycle_hooks([
        {
            "event": "before_each",
            "file": "tests/setup.spec.ts",
            "calls": [
                {"method": "basePage.openBaseURL", "args": []},
                {"method": "basePage.logIn", "args": ["USER", "PASS"]},
                {"method": "basePage.selectMenu", "args": ["MENU.HOME"]},
            ],
        },
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/setup.spec.ts", "calls": [
                           {"pom": "BasePage", "method": "openBaseURL"},
                           {"pom": "BasePage", "method": "logIn", "args": ["USER", "PASS"]},
                           {"pom": "BasePage", "method": "selectMenu"},
                       ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert any(
        "declares 0 args but reused source calls it with 1 arg" in v
        and "selectMenu" in v
        and "MENU.HOME" in v
        for v in violations
    )
    # logIn carries its args in both places, openBaseURL has none in either —
    # neither should trip the args-preservation gate.
    assert not any("logIn" in v and "declares 0 args" in v for v in violations)
    assert not any("openBaseURL" in v and "declares 0 args" in v for v in violations)


def test_hook_reuse_legacy_string_inventory_still_accepted_for_args_gate():
    """Backward-compat: when the inventory has legacy string-form calls
    (no arg information), args-preservation MUST NOT fire — the gate has
    no ground truth to check against and must not synthesize violations."""
    am = _ui_am_with_lifecycle_hooks([
        {
            "event": "before_each",
            "file": "tests/setup.spec.ts",
            "calls": ["basePage.openBaseURL", "basePage.logIn", "basePage.selectMenu"],
        },
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/setup.spec.ts", "calls": [
                           {"pom": "BasePage", "method": "openBaseURL"},
                           {"pom": "BasePage", "method": "logIn"},
                           {"pom": "BasePage", "method": "selectMenu"},
                       ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert not any("declares 0 args" in v for v in violations)


def test_hook_reuse_empty_inventory_calls_skips_sequence_check():
    am = _ui_am_with_lifecycle_hooks([
        {"event": "before_each", "file": "tests/RopaEntitySmoke.spec.ts", "calls": []},
    ])
    plan = {
        "plan_version": "1.0", "active_module": "x", "framework": "playwright-ts",
        "test_cases": [{
            "id": "TC-1", "test_file_target": "tests/qtea_x.spec.ts",
            "hooks": [{"event": "before_each", "source": "reuse",
                       "from": "tests/RopaEntitySmoke.spec.ts", "calls": [
                           {"pom": "basePage", "method": "openBaseURL"},
                       ]}],
            "test_functions": [{"name": "t", "markers": ["qtea_smoke"], "steps": [
                {"order": 1, "phase": "act", "pom": "EntityFormPage", "method": "clickOnSave"},
            ]}],
        }],
    }
    violations = _validate_plan_against_inventory(plan, am)
    assert not any("stale relative to" in v for v in violations)


def test_hook_reuse_gate_skipped_when_no_lifecycle_hooks_data():
    """Regression guard: `_ui_am()` (no lifecycle_hooks key) must still pass
    unaffected — the new gate must not fire without ground truth."""
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
    assert not any(
        "does not match any sut_inventory.lifecycle_hooks" in v
        or "stale relative to" in v
        for v in violations
    )


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


# ---------------------------------------------------------------------------
# Coverage-gap stubs (added 2026-07-24) — see coverage audit for context.
# Each stub documents the exact uncovered code path it should exercise.
# TODO: implement all stubs below.
# ---------------------------------------------------------------------------


def test_validate_assertion_oracle_flags_missing_and_malformed_criteria():
    """src/qtea/steps/s07_test_architect.py:368-419 —
    `_validate_assertion_oracle`.

    Covers the deterministic backstop for assertion-kind `missing_methods`
    (the JSON-schema if/then can't express these cross-field checks):
    (1) `kind != "assertion"` -> returns False, no violations appended;
    (2) `kind == "assertion"` with empty/missing `acceptance_criteria` ->
    violation appended, returns False; (3) a criterion that isn't a dict
    -> violation appended, `all_custom` forced False; (4) a criterion
    whose `check` is in `_ORACLE_CHECKS_NEED_LOCATOR` but has no
    `locator`, or whose `locator` isn't in the test case's declared
    `locators[]` set -> violation appended in each sub-case; (5) same
    pattern for `_ORACLE_CHECKS_NEED_REF_LOCATOR` (`reference_locator`)
    and `_ORACLE_CHECKS_NEED_EXPECTED` (`expected_literal` /
    `expected_symbol`); (6) ALL criteria are `check == "custom"` -> returns
    True (escapes deterministic verification, routed to the Stage-3
    semantic assertion-judge instead of silently passing).
    """
    # (1) kind != "assertion" -> False, no violations.
    violations: list[str] = []
    assert _validate_assertion_oracle(
        "TC-1", "LoginPage", {"kind": "action", "name": "click_login"}, set(), violations,
    ) is False
    assert violations == []

    # (2) kind == "assertion" with no acceptance_criteria -> violation, False.
    violations = []
    assert _validate_assertion_oracle(
        "TC-1", "LoginPage", {"kind": "assertion", "name": "verify_error"}, set(), violations,
    ) is False
    assert len(violations) == 1
    assert "no acceptance_criteria" in violations[0]

    # (3) a criterion that isn't a dict -> violation, all_custom forced False.
    violations = []
    mm = {"kind": "assertion", "name": "verify_error", "acceptance_criteria": ["not-a-dict"]}
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, set(), violations) is False
    assert any("is not an object" in v for v in violations)

    # (4) check needs a locator: missing locator -> violation.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_visible",
        "acceptance_criteria": [{"check": "visible"}],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, set(), violations) is False
    assert any("needs a `locator`" in v for v in violations)

    # (4b) check needs a locator: locator not in declared set -> violation.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_visible",
        "acceptance_criteria": [{"check": "visible", "locator": "#not-declared"}],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, {"#login-btn"}, violations) is False
    assert any("not declared in this test case's locators" in v for v in violations)

    # (5) reference_locator required and missing -> violation.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_below",
        "acceptance_criteria": [{
            "check": "boundingbox_below", "locator": "#a",
            "expected_literal": None, "expected_symbol": None,
        }],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, {"#a"}, violations) is False
    assert any("needs a `reference_locator`" in v for v in violations)

    # (5b) reference_locator not in declared set -> violation.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_below",
        "acceptance_criteria": [{
            "check": "boundingbox_below", "locator": "#a", "reference_locator": "#not-declared",
        }],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, {"#a"}, violations) is False
    assert any("reference_locator `#not-declared`" in v for v in violations)

    # (5c) expected value required and missing (both expected_literal and
    # expected_symbol null/falsy) -> violation.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_text",
        "acceptance_criteria": [{
            "check": "exact_text", "locator": "#a",
            "expected_literal": None, "expected_symbol": None,
        }],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, {"#a"}, violations) is False
    assert any("needs a" in v and "expected value" in v for v in violations)

    # (5d) expected_symbol present satisfies the expected-value requirement,
    # no violation for that sub-check.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_text",
        "acceptance_criteria": [{
            "check": "exact_text", "locator": "#a",
            "expected_literal": None, "expected_symbol": "EXPECTED_TITLE",
        }],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, {"#a"}, violations) is False
    assert not any("expected value" in v for v in violations)

    # (6) ALL criteria are check == "custom" -> True, escapes verification.
    violations = []
    mm = {
        "kind": "assertion", "name": "verify_custom",
        "acceptance_criteria": [{"check": "custom"}, {"check": "custom"}],
    }
    assert _validate_assertion_oracle("TC-1", "LoginPage", mm, set(), violations) is True
    assert violations == []


async def test_step07_auth_prewarm_mode_dispatch_and_fallback_transitions(
    tmp_path: Path, monkeypatch,
):
    """src/qtea/steps/s07_test_architect.py:1452-1509 — Step 7's
    auth-prewarm mode dispatch inside `TestArchitectStep.run` (or its
    extracted pre-pass helper).

    Covers, with `maybe_headed_prewarm` / `maybe_prewarm_auth` /
    `_storage_state.resolve` / `resolve_login_credentials` monkeypatched:
    (1) `_prewarm_mode == "headed"` raising an unexpected exception ->
    caught, logged (`step07.auth_prewarm_unexpected_error`), `_status`
    forced to `"skipped"` instead of propagating and failing Step 7;
    (2) headed mode returning `_status == "fallback_mcp"` (qtea's own
    Playwright missing) -> `_prewarm_mode` is reassigned to `"mcp"` for
    the remainder of the pre-pass; (3) `_prewarm_mode == "script"` raising
    -> caught + logged, Step 7 continues; (4) `_prewarm_mode == "mcp"`
    with an existing resolvable storage-state -> credential resolution is
    skipped entirely (`_creds` never computed) and no `LoginSpec` is
    built; (5) `_prewarm_mode == "mcp"` with no storage-state and no
    resolvable credentials -> logs `step07.mcp_login_skip` with
    `reason="no_credentials"` and `login_spec` stays None; (6) `mcp` mode
    where `_storage_state.resolve` or `resolve_login_credentials` itself
    raises -> caught, logged (`step07.mcp_login_setup_error`), never
    propagates.
    """
    import qtea.steps.s07_auth_prewarm as _prewarm_mod
    import qtea.storage_state as _storage_state_mod

    def _fresh_ctx(mode: str) -> StepContext:
        ctx = _ctx(tmp_path, include_default_inventory=False)
        _seed_upstream(ctx)
        ctx.options.auth_prewarm_mode = mode
        install_fake_anthropic(monkeypatch, text=json.dumps(_GOOD_PLAN))
        return ctx

    # (1) headed mode: maybe_headed_prewarm raises -> caught, logged, Step 7
    # continues to success (not reassigned to mcp, since status != fallback_mcp).
    async def _raise_headed(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_prewarm_mod, "maybe_headed_prewarm", _raise_headed)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("headed")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    assert any(
        c.args and c.args[0] == "step07.auth_prewarm_unexpected_error"
        for c in fake_log.warning.call_args_list
    )

    # (2) headed mode returns "fallback_mcp" -> _prewarm_mode reassigned to
    # "mcp" for the rest of the pre-pass, so the mcp branch's storage-state
    # resolution runs even though the caller only asked for "headed".
    async def _fallback_mcp(**kwargs):
        return "fallback_mcp"

    resolve_mock = Mock(return_value=None)
    creds_mock = Mock(return_value=None)
    monkeypatch.setattr(_prewarm_mod, "maybe_headed_prewarm", _fallback_mcp)
    monkeypatch.setattr(_storage_state_mod, "resolve", resolve_mock)
    monkeypatch.setattr(_prewarm_mod, "resolve_login_credentials", creds_mock)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("headed")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    resolve_mock.assert_called_once()
    creds_mock.assert_called_once()

    # (3) script mode: maybe_prewarm_auth raises -> caught + logged, continues.
    async def _raise_script(**kwargs):
        raise RuntimeError("script boom")

    monkeypatch.setattr(_prewarm_mod, "maybe_prewarm_auth", _raise_script)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("script")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    assert any(
        c.args and c.args[0] == "step07.auth_prewarm_unexpected_error"
        for c in fake_log.warning.call_args_list
    )

    # (4) mcp mode with an existing resolvable storage-state -> credential
    # resolution is skipped entirely (mock never called), no LoginSpec built.
    resolve_mock = Mock(return_value=tmp_path / "storageState.json")
    creds_mock = Mock(return_value=("user", "pass"))
    monkeypatch.setattr(_storage_state_mod, "resolve", resolve_mock)
    monkeypatch.setattr(_prewarm_mod, "resolve_login_credentials", creds_mock)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("mcp")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    resolve_mock.assert_called_once()
    creds_mock.assert_not_called()

    # (5) mcp mode with no storage-state and no resolvable credentials ->
    # logs mcp_login_skip(reason="no_credentials"); login_spec stays None.
    resolve_mock = Mock(return_value=None)
    creds_mock = Mock(return_value=None)
    monkeypatch.setattr(_storage_state_mod, "resolve", resolve_mock)
    monkeypatch.setattr(_prewarm_mod, "resolve_login_credentials", creds_mock)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("mcp")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    assert any(
        c.args and c.args[0] == "step07.mcp_login_skip"
        and c.kwargs.get("reason") == "no_credentials"
        for c in fake_log.info.call_args_list
    )

    # (6) mcp mode where storage-state resolution itself raises -> caught,
    # logged (mcp_login_setup_error), never propagates.
    def _raise_resolve(**kwargs):
        raise RuntimeError("resolve boom")

    monkeypatch.setattr(_storage_state_mod, "resolve", _raise_resolve)
    fake_log = Mock()
    monkeypatch.setattr("qtea.steps.s07_test_architect.log", fake_log)
    ctx = _fresh_ctx("mcp")
    result = await TestArchitectStep().run(ctx)
    assert result.success, result.error
    assert any(
        c.args and c.args[0] == "step07.mcp_login_setup_error"
        for c in fake_log.warning.call_args_list
    )
