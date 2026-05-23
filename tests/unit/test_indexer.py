"""Tests for the test_indexer module (Step 7 post-processor)."""

from __future__ import annotations

from pathlib import Path

from worca_t.test_indexer import (
    index_tests,
    resolve_framework,
    violations_summary,
)

# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------


def test_resolve_framework_prefers_hint(tmp_path: Path):
    assert resolve_framework("playwright-ts", tmp_path) == "playwright-ts"
    assert resolve_framework("pytest", tmp_path) == "pytest"


def test_resolve_framework_falls_back_to_extension(tmp_path: Path):
    (tmp_path / "test_login.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert resolve_framework(None, tmp_path) == "pytest"


def test_resolve_framework_unknown_when_no_files(tmp_path: Path):
    assert resolve_framework(None, tmp_path) == "unknown"


def test_resolve_framework_ts_extension(tmp_path: Path):
    (tmp_path / "login.spec.ts").write_text("test('a', async () => {});\n", encoding="utf-8")
    assert resolve_framework(None, tmp_path) == "playwright-ts"


# ---------------------------------------------------------------------------
# Indexing - happy paths per framework
# ---------------------------------------------------------------------------


def test_index_playwright_ts_extracts_tests_and_locators(tmp_path: Path):
    f = tmp_path / "login.spec.ts"
    f.write_text(
        """\
import { test, expect } from '@playwright/test';

// @tc TC-LOGIN-001
// @tag smoke
test('should sign in with valid credentials', async ({ page }) => {
  await page.goto('/login');
  await page.getByTestId('username').fill('alice');
  await page.getByLabel('Password').fill(process.env.PW);
  await page.getByRole('button', { name: 'Submit' }).click();
  await expect(page.locator('#dashboard')).toBeVisible();
});

it('should show error on bad password', async ({ page }) => {
  await page.getByPlaceholder('email').fill('x');
  await page.getByText('Invalid').isVisible();
});
""",
        encoding="utf-8",
    )

    result = index_tests(tmp_path, framework="playwright-ts")
    assert result.framework == "playwright-ts"
    assert len(result.files) == 1
    names = [t.name for t in result.tests]
    assert "should sign in with valid credentials" in names
    assert "should show error on bad password" in names

    first = next(t for t in result.tests if "valid credentials" in t.name)
    strategies = {c.strategy for c in first.locator_candidates}
    assert {"data-testid", "label", "role", "id"}.issubset(strategies)
    assert first.tc_refs == ["TC-LOGIN-001"]
    assert "smoke" in first.tags
    assert not result.violations


def test_index_pytest_extracts_test_functions(tmp_path: Path):
    f = tmp_path / "test_login.py"
    f.write_text(
        """\
import os

# @tc TC-LOGIN-002
def test_should_login_with_valid_credentials(page):
    page.goto("/login")
    page.get_by_test_id("username").fill("alice")
    page.get_by_role("button", name="Submit").click()


def helper_not_a_test():
    pass


def test_should_reject_bad_password(page):
    page.get_by_label("Password").fill("nope")
""",
        encoding="utf-8",
    )

    result = index_tests(tmp_path, framework="pytest")
    names = [t.name for t in result.tests]
    assert names == [
        "test_should_login_with_valid_credentials",
        "test_should_reject_bad_password",
    ]
    valid = result.tests[0]
    assert any(c.strategy == "data-testid" for c in valid.locator_candidates)
    assert valid.tc_refs == ["TC-LOGIN-002"]


def test_index_cypress_recognizes_dotcyts(tmp_path: Path):
    f = tmp_path / "login.cy.ts"
    f.write_text(
        """\
describe('login', () => {
  it('logs in', () => {
    cy.get('[data-testid=username]').type('a');
    cy.contains('Submit').click();
  });
});
""",
        encoding="utf-8",
    )

    result = index_tests(tmp_path, framework="cypress")
    assert len(result.tests) == 1
    assert result.tests[0].name == "logs in"


def test_index_robot_treats_each_test_name(tmp_path: Path):
    f = tmp_path / "login.robot"
    f.write_text(
        """\
*** Settings ***
Library    Browser

*** Test Cases ***
Should Login With Valid Credentials
    New Page    /login
    Click    id=submit

Should Reject Bad Password
    New Page    /login
""",
        encoding="utf-8",
    )

    result = index_tests(tmp_path, framework="robot")
    names = [t.name for t in result.tests]
    assert "Should Login With Valid Credentials" in names
    assert "Should Reject Bad Password" in names
    # Robot heuristic must NOT pick up section headers.
    assert not any("***" in n for n in names)


def test_index_tbd_markers_captured(tmp_path: Path):
    f = tmp_path / "todo.spec.ts"
    f.write_text(
        """\
test('todo', async ({ page }) => {
  await page.locator('TBD_LOCATOR').click();
  // <<TBD: replace username field>>
});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert len(result.tests) == 1
    markers = result.tests[0].tbd_markers
    assert len(markers) >= 2
    assert any("TBD_LOCATOR" in m.raw for m in markers)


def test_index_unique_ids_on_duplicate_names(tmp_path: Path):
    f = tmp_path / "dup.spec.ts"
    f.write_text(
        """\
test('same', async () => {});
test('same', async () => {});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    ids = [t.id for t in result.tests]
    assert len(set(ids)) == 2


def test_index_empty_root_returns_empty(tmp_path: Path):
    result = index_tests(tmp_path, framework="playwright-ts")
    assert result.tests == []
    assert result.files == []
    assert result.violations == []


# ---------------------------------------------------------------------------
# Violation detection (each rule independently)
# ---------------------------------------------------------------------------


def test_violation_xpath_locator(tmp_path: Path):
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {\n  await page.locator('xpath=//button').click();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    rules = [v.rule for v in result.violations]
    assert "xpath" in rules


def test_violation_xpath_literal_slashes(tmp_path: Path):
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {\n  await page.locator('//div[@id=\"a\"]').click();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert any(v.rule == "xpath" for v in result.violations)


def test_violation_xpath_selenium_python(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """from selenium.webdriver.common.by import By
def test_x(driver):
    driver.find_element(By.XPATH, "//button[1]").click()
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="selenium-py")
    assert any(v.rule == "xpath" for v in result.violations)


def test_violation_hard_wait_time_sleep(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """import time
def test_x(page):
    time.sleep(2)
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert any(v.rule == "hard-wait" for v in result.violations)


def test_violation_hard_wait_cy_wait_number(tmp_path: Path):
    (tmp_path / "bad.cy.ts").write_text(
        """it('x', () => { cy.wait(1000); });\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="cypress")
    assert any(v.rule == "hard-wait" for v in result.violations)


def test_violation_page_content(tmp_path: Path):
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {\n  const html = await page.content();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert any(v.rule == "page-content" for v in result.violations)


def test_violation_raw_secret_password_literal(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """def test_x(page):
    password = "hunter22"
    page.fill("#pw", password)
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert any(v.rule == "raw-secret" for v in result.violations)


def test_no_false_positive_for_env_password(tmp_path: Path):
    (tmp_path / "good.spec.ts").write_text(
        """test('x', async ({ page }) => {\n  await page.fill('#pw', process.env.PW);\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert not result.violations


def test_violations_summary_format(tmp_path: Path):
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {\n  await page.locator('xpath=//x').click();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    summary = violations_summary(result)
    assert "violation" in summary
    assert "[xpath]" in summary


def test_index_as_dict_matches_schema_shape(tmp_path: Path):
    (tmp_path / "ok.spec.ts").write_text(
        """test('ok', async ({ page }) => {\n  await page.getByRole('button').click();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    d = result.as_dict()
    assert d["framework"] == "playwright-ts"
    assert d["totals"]["files"] == 1
    assert d["totals"]["tests"] == 1
    assert d["tests"][0]["id"].startswith("T-")
    assert "locator_candidates" in d["tests"][0]
