"""Unit tests for Step 8.5 semantic preflight."""

from __future__ import annotations

from pathlib import Path

from worca_t.preflight import (
    _ast_parse_python_tests,
    _check_auth_fixture_missing,
    _check_fixture_graph,
    _check_href_when_navigates,
    _check_missing_reuse_imports,
    _check_sentinel_constants,
    _tcs_with_navigation_expected_results,
    run_preflight,
)


_INVENTORY_WITH_AUTH = {
    "active_module": "app",
    "modules": [{
        "name": "app",
        "auth_flow": {
            "fixture_entry": "tests/fixtures/auth.py:authenticated_page",
        },
        "existing_page_objects": [
            {"name": "DashboardPage", "scope": "navigation"},
            {"name": "PublicHomePage", "scope": "generic"},
        ],
    }],
}


_NAV_STRATEGY = """\
#### TC-NAV-001
Steps:
- click the link
Expected: link navigates to "https://example.com/dashboard"

#### TC-NOTNAV-001
Steps:
- check the page
Expected: count equals 1
"""


# ---------------------------------------------------------------------------
# Sub-check (1): ast.parse on generated Python tests
# ---------------------------------------------------------------------------


def test_ast_parse_clean_file_no_violation(tmp_path: Path):
    rel = "tests/worca_clean_test.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / rel).write_text(
        "def test_clean():\n    assert True\n",
        encoding="utf-8",
    )
    violations = _ast_parse_python_tests(tmp_path, {rel}, "pytest")
    assert violations == []


def test_ast_parse_syntax_error_flagged(tmp_path: Path):
    rel = "tests/worca_broken_test.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / rel).write_text(
        "Looking at the plan, I need to write:\ndef test(): pass\n",
        encoding="utf-8",
    )
    violations = _ast_parse_python_tests(tmp_path, {rel}, "pytest")
    assert len(violations) == 1
    assert violations[0].rule == "preflight-error"
    assert violations[0].file == rel
    assert "ast.parse" in violations[0].snippet


def test_ast_parse_skipped_on_non_python_framework(tmp_path: Path):
    rel = "tests/worca_x.spec.ts"
    (tmp_path / "tests").mkdir()
    (tmp_path / rel).write_text("this is not valid python", encoding="utf-8")
    # Even though the file is invalid Python, TS-stack preflight ignores it.
    violations = _ast_parse_python_tests(tmp_path, {rel}, "playwright-ts")
    assert violations == []


# ---------------------------------------------------------------------------
# Sub-check (2): fixture dependency graph
# ---------------------------------------------------------------------------


def test_fixture_graph_clean(tmp_path: Path):
    plan = {
        "test_cases": [{
            "id": "TC-1",
            "fixtures": [
                {"name": "auth", "source": "reuse"},
                {"name": "page_with_auth", "source": "create", "depends_on": ["auth"]},
            ],
        }],
    }
    inventory = {"modules": [{"name": "m", "existing_fixtures": [{"name": "auth"}]}]}
    assert _check_fixture_graph(plan, inventory) == []


def test_fixture_graph_missing_dependency_flagged():
    plan = {
        "test_cases": [{
            "id": "TC-1",
            "fixtures": [
                {"name": "dashboard", "source": "create", "depends_on": ["missing_fixture"]},
            ],
        }],
    }
    violations = _check_fixture_graph(plan, None)
    assert len(violations) == 1
    assert "missing_fixture" in violations[0].snippet
    assert violations[0].file == "code-modification-plan.json"


def test_fixture_graph_builtin_dependency_ok():
    plan = {
        "test_cases": [{
            "fixtures": [
                {"name": "f", "source": "create", "depends_on": ["page", "tmp_path"]},
            ],
        }],
    }
    assert _check_fixture_graph(plan, None) == []


def test_fixture_graph_cycle_detected():
    plan = {
        "test_cases": [{
            "fixtures": [
                {"name": "a", "depends_on": ["b"]},
                {"name": "b", "depends_on": ["c"]},
                {"name": "c", "depends_on": ["a"]},
            ],
        }],
    }
    violations = _check_fixture_graph(plan, None)
    assert any("cycle" in v.snippet for v in violations)


def test_fixture_graph_self_loop_detected():
    plan = {
        "test_cases": [{
            "fixtures": [{"name": "self_ref", "depends_on": ["self_ref"]}],
        }],
    }
    violations = _check_fixture_graph(plan, None)
    assert any("cycle" in v.snippet and "self_ref" in v.snippet for v in violations)


# ---------------------------------------------------------------------------
# Sub-check (3): sentinel constant existence
# ---------------------------------------------------------------------------


def test_sentinel_constant_resolved_no_violation(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pages").mkdir()
    (tmp_path / "tests" / "worca_login_test.py").write_text(
        "from pages.locators import LoginLocators\n"
        "def test_x(page):\n"
        "    page.locator(LoginLocators.LOGIN_BUTTON).click()\n",
        encoding="utf-8",
    )
    (tmp_path / "pages" / "locators.py").write_text(
        "class LoginLocators:\n"
        "    LOGIN_BUTTON = 'tbd'\n",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "page_objects": [
                {"name": "Login", "locator_file": "pages/locators.py"},
            ],
        }],
    }
    violations = _check_sentinel_constants(
        tmp_path,
        {"tests/worca_login_test.py"},
        plan, None, "pytest",
    )
    assert violations == []


def test_sentinel_constant_missing_flagged(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pages").mkdir()
    (tmp_path / "tests" / "worca_login_test.py").write_text(
        "def test_x(page):\n"
        "    page.locator(LoginLocators.PASSWORD_FIELD).click()\n",
        encoding="utf-8",
    )
    (tmp_path / "pages" / "locators.py").write_text(
        "class LoginLocators:\n"
        "    LOGIN_BUTTON = 'tbd'\n",  # PASSWORD_FIELD intentionally missing
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "page_objects": [
                {"name": "Login", "locator_file": "pages/locators.py"},
            ],
        }],
    }
    violations = _check_sentinel_constants(
        tmp_path,
        {"tests/worca_login_test.py"},
        plan, None, "pytest",
    )
    assert len(violations) == 1
    assert "PASSWORD_FIELD" in violations[0].snippet
    assert violations[0].rule == "preflight-error"


def test_sentinel_constant_skipped_when_class_unknown(tmp_path: Path):
    """Unknown locator class → silent skip (no false positive)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_x_test.py").write_text(
        "def test_x(): UnknownThing.CONST  # noqa\n",
        encoding="utf-8",
    )
    violations = _check_sentinel_constants(
        tmp_path,
        {"tests/worca_x_test.py"},
        {}, None, "pytest",
    )
    assert violations == []


# ---------------------------------------------------------------------------
# Integration: run_preflight aggregates all three sub-checks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# href-when-navigates check (Change 4b)
# ---------------------------------------------------------------------------


def test_strategy_parser_picks_navigation_tc():
    nav = _tcs_with_navigation_expected_results(_NAV_STRATEGY)
    assert nav == {"TC-NAV-001"}


def test_href_when_navigates_flagged(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_nav_test.py").write_text(
        """from playwright.sync_api import expect

# @tc TC-NAV-001
def test_link_destination(page):
    link = page.get_by_role("link", name="Dashboard")
    expect(link).to_have_attribute("href", "https://example.com/dashboard")
""",
        encoding="utf-8",
    )
    violations = _check_href_when_navigates(
        tmp_path, {"tests/worca_nav_test.py"}, _NAV_STRATEGY, "pytest",
    )
    assert len(violations) == 1
    assert violations[0].rule == "href-when-navigates"
    assert violations[0].severity == "error"


def test_href_when_navigates_not_flagged_for_count_tc(tmp_path: Path):
    """When the strategy says `count equals 1`, an href assertion is fine."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_count_test.py").write_text(
        """from playwright.sync_api import expect

# @tc TC-NOTNAV-001
def test_count(page):
    link = page.get_by_role("link")
    expect(link).to_have_attribute("href", "https://example.com")
""",
        encoding="utf-8",
    )
    violations = _check_href_when_navigates(
        tmp_path, {"tests/worca_count_test.py"}, _NAV_STRATEGY, "pytest",
    )
    assert violations == []


def test_href_when_navigates_skipped_on_non_python(tmp_path: Path):
    violations = _check_href_when_navigates(
        tmp_path, {"tests/x.spec.ts"}, _NAV_STRATEGY, "playwright-ts",
    )
    assert violations == []


# ---------------------------------------------------------------------------
# auth-fixture-missing advisory (Change 5)
# ---------------------------------------------------------------------------


def test_auth_fixture_missing_flagged_on_direct_omission(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_dash_test.py").write_text(
        """from pages.dashboard import DashboardPage

def test_dashboard_loads(page):
    dash = DashboardPage(page)
    assert dash is not None
""",
        encoding="utf-8",
    )
    violations = _check_auth_fixture_missing(
        tmp_path,
        {"tests/worca_dash_test.py"},
        {}, _INVENTORY_WITH_AUTH, "pytest",
    )
    assert len(violations) == 1
    assert violations[0].rule == "auth-fixture-missing"
    assert violations[0].severity == "warning"
    assert "authenticated_page" in violations[0].snippet


def test_auth_fixture_missing_satisfied_by_direct_param(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_dash_test.py").write_text(
        """from pages.dashboard import DashboardPage

def test_dashboard_loads(authenticated_page):
    dash = DashboardPage(authenticated_page)
    assert dash is not None
""",
        encoding="utf-8",
    )
    violations = _check_auth_fixture_missing(
        tmp_path,
        {"tests/worca_dash_test.py"},
        {}, _INVENTORY_WITH_AUTH, "pytest",
    )
    assert violations == []


def test_auth_fixture_missing_satisfied_via_transitive_chain(tmp_path: Path):
    """Test consumes `dash_page` fixture, which the plan declares as
    depends_on=[authenticated_page] — auth is wired transitively."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_dash_test.py").write_text(
        """from pages.dashboard import DashboardPage

def test_dashboard_loads(dash_page):
    assert dash_page is not None
""",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "fixtures": [{
                "name": "dash_page",
                "source": "create",
                "depends_on": ["authenticated_page"],
            }],
        }],
    }
    violations = _check_auth_fixture_missing(
        tmp_path,
        {"tests/worca_dash_test.py"},
        plan, _INVENTORY_WITH_AUTH, "pytest",
    )
    assert violations == []


def test_auth_fixture_missing_skipped_for_non_auth_poms(tmp_path: Path):
    """Test imports only generic-scope POMs → no auth requirement."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_public_test.py").write_text(
        """from pages.public import PublicHomePage

def test_home_loads(page):
    home = PublicHomePage(page)
    assert home is not None
""",
        encoding="utf-8",
    )
    violations = _check_auth_fixture_missing(
        tmp_path,
        {"tests/worca_public_test.py"},
        {}, _INVENTORY_WITH_AUTH, "pytest",
    )
    assert violations == []


def test_auth_fixture_missing_silent_when_no_auth_flow(tmp_path: Path):
    """Inventory without auth_flow → skip the check entirely."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_x_test.py").write_text(
        """def test_x(page):
    assert page is not None
""",
        encoding="utf-8",
    )
    violations = _check_auth_fixture_missing(
        tmp_path,
        {"tests/worca_x_test.py"},
        {}, {"modules": [{"name": "m"}]}, "pytest",
    )
    assert violations == []


# ---------------------------------------------------------------------------
# missing-reuse-import advisory (Change 6)
# ---------------------------------------------------------------------------


def test_missing_reuse_import_flagged(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_login_test.py").write_text(
        """def test_login(page):
    # plan said reuse LoginPage from pages/login_page.py:LoginPage,
    # but the test forgot to import it.
    assert page is not None
""",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "test_file_target": "tests/worca_login_test.py",
            "page_objects": [{
                "source": "reuse",
                "from": "pages/login_page.py:LoginPage",
                "name": "LoginPage",
            }],
        }],
    }
    violations = _check_missing_reuse_imports(tmp_path, plan, "pytest")
    assert len(violations) == 1
    assert violations[0].rule == "missing-reuse-import"
    assert violations[0].severity == "warning"
    assert "LoginPage" in violations[0].snippet


def test_missing_reuse_import_satisfied(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_login_test.py").write_text(
        """from pages.login_page import LoginPage

def test_login(page):
    lp = LoginPage(page)
    assert lp is not None
""",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "test_file_target": "tests/worca_login_test.py",
            "page_objects": [{
                "source": "reuse",
                "from": "pages/login_page.py:LoginPage",
                "name": "LoginPage",
            }],
        }],
    }
    violations = _check_missing_reuse_imports(tmp_path, plan, "pytest")
    assert violations == []


def test_missing_reuse_import_alias_accepted(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_login_test.py").write_text(
        """from pages.login_page import LoginPage as LP

def test_login(page):
    assert LP(page) is not None
""",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "test_file_target": "tests/worca_login_test.py",
            "page_objects": [{
                "source": "reuse",
                "from": "pages/login_page.py:LoginPage",
                "name": "LoginPage",
            }],
        }],
    }
    # `LoginPage` is imported (the actual name) — the alias `LP` is what's
    # used locally. Our check verifies the *imported* name matches the plan,
    # so this is correctly NOT flagged.
    violations = _check_missing_reuse_imports(tmp_path, plan, "pytest")
    assert violations == []


def test_missing_reuse_import_skips_fixtures(tmp_path: Path):
    """Fixtures aren't imported (pytest discovers by name) → skip the bucket."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_x_test.py").write_text(
        """def test_x(authenticated_page):
    assert authenticated_page is not None
""",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "test_file_target": "tests/worca_x_test.py",
            "fixtures": [{
                "source": "reuse",
                "from": "tests/fixtures/auth.py:authenticated_page",
                "name": "authenticated_page",
            }],
        }],
    }
    violations = _check_missing_reuse_imports(tmp_path, plan, "pytest")
    assert violations == []


def test_run_preflight_aggregates(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "worca_broken_test.py").write_text(
        "Looking at:\ndef test_x(): pass\n",
        encoding="utf-8",
    )
    plan = {
        "test_cases": [{
            "fixtures": [
                {"name": "f", "depends_on": ["does_not_exist"]},
            ],
        }],
    }
    result = run_preflight(
        tmp_path,
        framework="pytest",
        generated_files={"tests/worca_broken_test.py"},
        plan=plan,
        inventory=None,
    )
    # One AST violation + one missing-fixture violation.
    rules = [v.rule for v in result]
    assert len(rules) == 2
    assert all(r == "preflight-error" for r in rules)
