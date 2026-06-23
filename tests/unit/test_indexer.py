"""Tests for the test_indexer module (Step 7 post-processor)."""

from __future__ import annotations

from pathlib import Path

from worca_t.test_indexer import (
    blocking_violations,
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


def test_violation_empty_handler_python_except_pass(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """\
def test_x(page):
    try:
        page.click("#submit")
    except Exception:
        pass
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert any(v.rule == "empty-handler" for v in result.violations)
    # Severity must be "error" so it hard-rejects Step 8.
    handlers = [v for v in result.violations if v.rule == "empty-handler"]
    assert all(v.severity == "error" for v in handlers)


def test_violation_empty_handler_typescript_catch(tmp_path: Path):
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {
  try {
    await page.click('#submit');
  } catch (e) {}
});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert any(v.rule == "empty-handler" for v in result.violations)


def test_empty_handler_not_flagged_when_body_present(tmp_path: Path):
    (tmp_path / "ok.spec.ts").write_text(
        """test('x', async ({ page }) => {
  try {
    await page.click('#submit');
  } catch (e) {
    console.error(e);
  }
});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert not any(v.rule == "empty-handler" for v in result.violations)


def test_zero_assertions_flagged_on_assertless_pytest(tmp_path: Path):
    (tmp_path / "test_empty.py").write_text(
        """def test_navigates(page):
    page.goto("/login")
    page.click("#submit")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    zeros = [v for v in result.violations if v.rule == "zero-assertions"]
    assert len(zeros) == 1
    assert zeros[0].severity == "error"
    assert "test_navigates" in zeros[0].snippet


def test_zero_assertions_opt_out_marker_respected(tmp_path: Path):
    (tmp_path / "test_setup.py").write_text(
        """import pytest

@pytest.mark.worca_setup
def test_seed_state(page):
    page.goto("/admin")
    page.click("#seed")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "zero-assertions" for v in result.violations)


def test_zero_assertions_satisfied_by_bare_assert(tmp_path: Path):
    (tmp_path / "test_ok.py").write_text(
        """def test_x(page):
    assert page is not None
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "zero-assertions" for v in result.violations)


def test_zero_assertions_satisfied_by_expect_call(tmp_path: Path):
    (tmp_path / "test_ok.py").write_text(
        """from playwright.sync_api import expect

def test_x(page):
    expect(page).to_have_url("/foo")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "zero-assertions" for v in result.violations)


def test_zero_assertions_satisfied_by_pytest_raises(tmp_path: Path):
    (tmp_path / "test_ok.py").write_text(
        """import pytest

def test_x(page):
    with pytest.raises(ValueError):
        raise ValueError("nope")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "zero-assertions" for v in result.violations)


def test_zero_assertions_skipped_on_non_python_stack(tmp_path: Path):
    (tmp_path / "x.spec.ts").write_text(
        "test('x', async ({ page }) => { await page.click('#a'); });\n",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert not any(v.rule == "zero-assertions" for v in result.violations)


def test_bare_assert_warning_for_locator_textcontent(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """def test_x(page):
    btn = page.get_by_role("button")
    assert btn.text_content() == "Submit"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    bares = [v for v in result.violations if v.rule == "bare-assert-where-expect-available"]
    assert len(bares) >= 1
    assert all(v.severity == "warning" for v in bares)


def test_bare_assert_warning_for_page_url(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text(
        """def test_x(page):
    page.goto("/login")
    assert page.url == "https://example.com/login"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert any(
        v.rule == "bare-assert-where-expect-available" for v in result.violations
    )


def test_bare_assert_warning_does_not_block_step(tmp_path: Path):
    """The advisory rule must NOT appear in blocking_violations()."""
    (tmp_path / "test_bad.py").write_text(
        """def test_x(page):
    btn = page.get_by_role("button")
    assert btn.is_visible()
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    warnings = [v for v in result.violations if v.severity == "warning"]
    blocking = blocking_violations(result)
    assert warnings, "expected at least one advisory warning"
    assert not any(v.rule == "bare-assert-where-expect-available" for v in blocking)


def test_combobox_without_open_flagged(tmp_path: Path):
    (tmp_path / "test_dropdown.py").write_text(
        """def test_select(page):
    page.get_by_role("option", name="Berlin").click()
    assert page is not None
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    flagged = [v for v in result.violations if v.rule == "combobox-without-open"]
    assert len(flagged) == 1
    assert flagged[0].severity == "error"


def test_combobox_with_preceding_open_ok(tmp_path: Path):
    (tmp_path / "test_dropdown.py").write_text(
        """def test_select(page):
    page.get_by_role("combobox", name="City").click()
    page.get_by_role("option", name="Berlin").click()
    assert page is not None
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "combobox-without-open" for v in result.violations)


def test_combobox_with_listbox_open_ok(tmp_path: Path):
    """`listbox` is equivalent dropdown trigger semantics."""
    (tmp_path / "test_dropdown.py").write_text(
        """def test_select(page):
    page.get_by_role("listbox").click()
    page.get_by_role("option", name="Berlin").click()
    assert page is not None
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "combobox-without-open" for v in result.violations)


def test_popup_assert_on_original_page_flagged(tmp_path: Path):
    (tmp_path / "test_popup.py").write_text(
        """from playwright.sync_api import expect

def test_link_opens_new_tab(page):
    with page.expect_popup() as popup_info:
        page.get_by_role("link", name="Docs").click()
    expect(page).to_have_url("https://example.com/docs")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    # The assertion is OUTSIDE the with-block in this minimal repro, so
    # this should NOT flag — let's flip to inside.
    assert not any(v.rule == "popup-assert-on-original-page" for v in result.violations)


def test_popup_assert_inside_with_flagged(tmp_path: Path):
    (tmp_path / "test_popup.py").write_text(
        """from playwright.sync_api import expect

def test_link_opens_new_tab(page):
    with page.expect_popup() as popup_info:
        page.get_by_role("link", name="Docs").click()
        expect(page).to_have_url("https://example.com/docs")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    flagged = [v for v in result.violations if v.rule == "popup-assert-on-original-page"]
    assert len(flagged) == 1
    assert "expect_popup" in flagged[0].snippet


def test_popup_assert_on_popup_var_ok(tmp_path: Path):
    """Asserting on the popup page is the correct pattern."""
    (tmp_path / "test_popup.py").write_text(
        """from playwright.sync_api import expect

def test_link_opens_new_tab(page):
    with page.expect_popup() as popup_info:
        page.get_by_role("link", name="Docs").click()
    new_page = popup_info.value
    expect(new_page).to_have_url("https://example.com/docs")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert not any(v.rule == "popup-assert-on-original-page" for v in result.violations)


def test_blocking_violations_filters_to_errors(tmp_path: Path):
    """`blocking_violations()` returns only severity=error rows so advisory
    rules can be added without changing the hard-fail decision."""
    (tmp_path / "bad.spec.ts").write_text(
        """test('x', async ({ page }) => {
  await page.locator('xpath=//button').click();
});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert len(result.violations) >= 1
    assert len(blocking_violations(result)) == len(
        [v for v in result.violations if v.severity == "error"]
    )


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
    assert "error" in summary
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


# ---------------------------------------------------------------------------
# Regression: indexer must see `worca_`-prefixed test files (Layer B convention).
# ---------------------------------------------------------------------------


def test_indexer_finds_worca_prefixed_pytest_files(tmp_path: Path):
    """Step 7 codegen prefixes every generated test file with `worca_`
    (e.g. `worca_test_login.py`) to avoid colliding with the SUT's own tests.
    Without explicit `worca_test_*.py` globs, the indexer reports tests=0 for
    the actual test file and Step 8 misses every TBD marker.
    """
    smoke = tmp_path / "smoke"
    smoke.mkdir()
    (smoke / "worca_test_login.py").write_text(
        "def test_should_login_when_valid_creds():\n    pass\n",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-py")
    assert len(result.tests) == 1
    assert any("worca_test_login.py" in f for f in result.files)
    assert result.tests[0].name.startswith("test_should_login")


def test_indexer_finds_worca_prefixed_playwright_ts_files(tmp_path: Path):
    pages = tmp_path / "tests"
    pages.mkdir()
    (pages / "worca_login.spec.ts").write_text(
        """test('should login', async ({page}) => {\n  await page.getByRole('button').click();\n});\n""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    assert len(result.tests) == 1
    assert any("worca_login.spec.ts" in f for f in result.files)


def test_indexer_still_finds_standard_test_files_alongside_worca(tmp_path: Path):
    """Adding worca_ globs must NOT exclude standard test_ files."""
    smoke = tmp_path / "smoke"
    smoke.mkdir()
    (smoke / "test_native.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (smoke / "worca_test_added.py").write_text("def test_b(): pass\n", encoding="utf-8")
    result = index_tests(tmp_path, framework="playwright-py")
    files = " ".join(result.files)
    assert "test_native.py" in files
    assert "worca_test_added.py" in files
    assert len(result.tests) == 2


# ---------------------------------------------------------------------------
# TBD_INTENT comment parsing — semantic-intent capture for Step 8a
# ---------------------------------------------------------------------------


def test_indexer_attaches_tbd_intent_python_comment(tmp_path: Path):
    """A `# TBD_INTENT: <text>` comment above a TBD_LOCATOR marker is
    captured as the marker's `description`. Also: the surrounding test
    function name lands on `test_function`."""
    f = tmp_path / "worca_test_login.py"
    f.write_text(
        """\
def test_login_with_valid_credentials(page):
    # TBD_INTENT: primary submit button on the login form
    LOGIN_BUTTON = "TBD_LOCATOR"
    page.locator(LOGIN_BUTTON).click()
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    assert len(result.tests) == 1
    markers = result.tests[0].tbd_markers
    assert len(markers) >= 1
    target = next(m for m in markers if "TBD_LOCATOR" in m.raw)
    assert target.description == "primary submit button on the login form"
    assert target.test_function == "test_login_with_valid_credentials"


def test_indexer_attaches_tbd_intent_js_comment(tmp_path: Path):
    """JS/TS `// TBD_INTENT: <text>` comment style is also recognized."""
    f = tmp_path / "worca_login.spec.ts"
    f.write_text(
        """\
test('should login', async ({ page }) => {
  // TBD_INTENT: email input on the sign-in form
  const EMAIL = 'TBD_LOCATOR';
  await page.locator(EMAIL).fill('a@b.c');
});
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-ts")
    target = next(m for m in result.tests[0].tbd_markers if "TBD_LOCATOR" in m.raw)
    assert target.description == "email input on the sign-in form"
    assert target.test_function == "should login"


def test_indexer_legacy_marker_without_intent_has_null_description(tmp_path: Path):
    """A TBD marker with no adjacent TBD_INTENT comment leaves `description`
    as None — older runs degrade gracefully, no schema breakage."""
    f = tmp_path / "worca_test_legacy.py"
    f.write_text(
        """\
def test_legacy(page):
    LOGIN_BUTTON = "TBD_LOCATOR"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    target = next(m for m in result.tests[0].tbd_markers if "TBD_LOCATOR" in m.raw)
    assert target.description is None
    assert target.test_function == "test_legacy"


def test_indexer_tbd_intent_search_window_is_narrow(tmp_path: Path):
    """An intent comment 5 lines away from the marker is NOT attached —
    the search window is ±2 lines so far-away comments don't bleed into
    unrelated markers."""
    f = tmp_path / "worca_test_far.py"
    f.write_text(
        """\
def test_far(page):
    # TBD_INTENT: this is far away
    pass
    pass
    pass
    pass
    LOGIN_BUTTON = "TBD_LOCATOR"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    target = next(m for m in result.tests[0].tbd_markers if "TBD_LOCATOR" in m.raw)
    assert target.description is None  # 5 lines away → out of window


def test_indexer_tbd_intent_persists_through_as_dict(tmp_path: Path):
    """The new fields round-trip through the serialised output."""
    f = tmp_path / "worca_test_serialize.py"
    f.write_text(
        """\
def test_serialize(page):
    # TBD_INTENT: search box on the homepage
    SEARCH = "TBD_LOCATOR"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="pytest")
    d = result.as_dict()
    marker = next(
        m for m in d["tests"][0]["tbd_markers"] if "TBD_LOCATOR" in m["raw"]
    )
    assert marker["description"] == "search box on the homepage"
    assert marker["test_function"] == "test_serialize"


# ---------------------------------------------------------------------------
# JIT-runtime tbd() call parsing — Python+pytest+Playwright codegen path
# ---------------------------------------------------------------------------


def test_indexer_recognizes_tbd_call_in_support_file(tmp_path: Path):
    """`LOGIN_BUTTON = tbd("intent")` is the JIT-runtime emission style.
    The indexer extracts intent directly from the call argument."""
    pages = tmp_path / "pages" / "locators"
    pages.mkdir(parents=True)
    (pages / "worca_login_locators.py").write_text(
        """\
from tests.worca_t_runtime import tbd

class LoginLocators:
    LOGIN_BUTTON = tbd("primary submit button on the login form")
    PASSWORD = tbd("password input on the sign-in form")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-py")
    assert len(result.support_files) == 1
    markers = result.support_files[0].tbd_markers
    descriptions = {m.description for m in markers}
    assert descriptions == {
        "primary submit button on the login form",
        "password input on the sign-in form",
    }
    # raw captures the actual tbd(...) call text
    assert any('tbd("primary submit button' in m.raw for m in markers)


def test_indexer_mixed_tbd_styles_no_double_count(tmp_path: Path):
    """A file with both `tbd("...")` calls AND legacy `# TBD_INTENT: ...` +
    `TBD_LOCATOR` markers should index each marker exactly once."""
    pages = tmp_path / "pages" / "locators"
    pages.mkdir(parents=True)
    (pages / "worca_mixed_locators.py").write_text(
        """\
from tests.worca_t_runtime import tbd

class MixedLocators:
    NEW_STYLE = tbd("new style locator")
    # TBD_INTENT: legacy style locator
    LEGACY = "TBD_LOCATOR"
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-py")
    markers = result.support_files[0].tbd_markers
    assert len(markers) == 2
    descriptions = sorted(m.description for m in markers)
    assert descriptions == ["legacy style locator", "new style locator"]


def test_indexer_tbd_call_with_empty_intent_is_skipped(tmp_path: Path):
    """`tbd("")` with no intent is rejected by the runtime helper at
    test time; the indexer also drops it to avoid producing empty markers."""
    pages = tmp_path / "pages" / "locators"
    pages.mkdir(parents=True)
    (pages / "worca_empty_locators.py").write_text(
        """\
from tests.worca_t_runtime import tbd

class EmptyLocators:
    BAD = tbd("")
    GOOD = tbd("real intent")
""",
        encoding="utf-8",
    )
    result = index_tests(tmp_path, framework="playwright-py")
    markers = result.support_files[0].tbd_markers
    assert len(markers) == 1
    assert markers[0].description == "real intent"
