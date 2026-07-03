"""Unit tests for the deterministic XPath → Playwright rewriter."""

from __future__ import annotations

from pathlib import Path

import pytest

from qtea.xpath_rewriter import (
    RewriteKind,
    find_xpath_sites,
    rewrite_file,
    rewrite_xpath,
)

# ---------------------------------------------------------------------------
# Single-xpath translation matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("xpath", "expected"), [
    # data-test attribute → getByTestId (config sets testIdAttribute='data-test')
    (
        '//input[@data-test="username-input"]',
        "this.page.getByTestId('username-input')",
    ),
    (
        '//div[@data-test="action-menu-button"]',
        "this.page.getByTestId('action-menu-button')",
    ),
    # data-testid attribute → CSS (we're using data-test as the testid attr)
    (
        '//button[@data-testid="confirm-button"]',
        "this.page.locator('button[data-testid=\"confirm-button\"]')",
    ),
    # arbitrary attribute → CSS
    (
        '//button[@id="userMenuButton"]',
        "this.page.locator('button[id=\"userMenuButton\"]')",
    ),
    (
        '//div[@ref=\'eCheckbox\']',
        "this.page.locator('div[ref=\"eCheckbox\"]')",
    ),
    (
        '//button[@aria-roledescription="button to navigate right"]',
        "this.page.locator('button[aria-roledescription=\"button to navigate right\"]')",
    ),
    # heading role via `<h1>` + normalize-space contains
    (
        '//h1[contains(normalize-space(.), "GRC HOME")]',
        "this.page.getByRole('heading', { name: 'GRC HOME' })",
    ),
    # link role
    (
        '//a[contains(., "Log out")]',
        "this.page.getByRole('link', { name: 'Log out' })",
    ),
    # button role
    (
        '//button[contains(normalize-space(.), "Save")]',
        "this.page.getByRole('button', { name: 'Save' })",
    ),
    # exact text on <p>
    (
        '//p[text()="Record of Processing Activities - ROPA"]',
        "this.page.getByText('Record of Processing Activities - ROPA', { exact: true })",
    ),
    # exact text on <span> via normalize-space()
    (
        '//span[normalize-space()="Logout"]',
        "this.page.getByRole('link', { name: 'Logout' })"
        if False else  # <-- span isn't in role map; this branch is for docs
        "this.page.getByText('Logout', { exact: true })",
    ),
    # fuzzy text on any tag
    (
        '//*[contains(text(), "No matching result")]',
        "this.page.getByText('No matching result')",
    ),
    # nested descendant with mixed predicates
    (
        '//div[@data-test="upload-file-container"]//input[@type="file"]',
        'this.page.getByTestId(\'upload-file-container\').locator(\'input[type="file"]\')',
    ),
    # contains(@attr, "X") → CSS substring selector
    (
        '//a[contains(@data-test, "name-cell-link-x")]',
        "this.page.locator('a[data-test*=\"name-cell-link-x\"]')",
    ),
    # simple `xpath=` prefix stripped
    (
        'xpath=//input[@data-test="username-input"]',
        "this.page.getByTestId('username-input')",
    ),
])
def test_rewrite_xpath_matrix(xpath: str, expected: str) -> None:
    """Every deterministic pattern lands on the expected Playwright call."""
    rw = rewrite_xpath(xpath)
    assert rw is not None, f"expected a rewrite for {xpath!r}"
    assert rw.expression == expected


def test_rewrite_xpath_data_test_uses_testid() -> None:
    rw = rewrite_xpath('//input[@data-test="X"]')
    assert rw is not None
    assert rw.kind == RewriteKind.TESTID


def test_rewrite_xpath_data_testid_uses_css() -> None:
    rw = rewrite_xpath('//button[@data-testid="Y"]')
    assert rw is not None
    assert rw.kind == RewriteKind.CSS


def test_rewrite_xpath_union_simple() -> None:
    xpath = (
        '//h1[contains(normalize-space(.), "GRC HOME")] '
        '| //p[contains(normalize-space(.), "GRC HOME")]'
    )
    rw = rewrite_xpath(xpath)
    assert rw is not None
    assert rw.kind == RewriteKind.UNION
    assert ".or(" in rw.expression
    assert "getByRole('heading'" in rw.expression
    assert "getByText('GRC HOME')" in rw.expression


def test_rewrite_xpath_template_interpolation_preserved() -> None:
    xpath = '//a[contains(@data-test, "name-cell-link-${entityName}")]'
    rw = rewrite_xpath(xpath)
    assert rw is not None
    assert "${entityName}" in rw.expression
    # Template literals use backticks
    assert "`" in rw.expression


# ---------------------------------------------------------------------------
# Straggler detection — unsafe xpath families return None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("xpath", [
    # parent axis
    '//div[@ref="X"]/parent::td',
    # ancestor axis
    '(//label[@data-test="x"]/ancestor::div[4])',
    # following-sibling axis
    '//h1[.="X"]/following-sibling::p',
    # position() predicate
    '//tr[position()=1]/td',
    # relative path (leading .)
    './/div[@class="x"]',
    # bare `text()="X"` with unsupported outer predicate
    '//button[i and .//text()[normalize-space()="Actions"]]',
])
def test_rewrite_xpath_stragglers_return_none(xpath: str) -> None:
    assert rewrite_xpath(xpath) is None


# ---------------------------------------------------------------------------
# find_xpath_sites — locates literals with correct positions and quote types
# ---------------------------------------------------------------------------


def test_find_xpath_sites_single_and_double_quoted() -> None:
    src = (
        "const a = '//input[@data-test=\"x\"]';\n"
        "const b = \"//button[@id='y']\";\n"
    )
    sites = find_xpath_sites(src)
    assert len(sites) == 2
    assert sites[0].line == 1
    assert sites[0].quote == "'"
    assert sites[1].line == 2
    assert sites[1].quote == '"'


def test_find_xpath_sites_template_literal() -> None:
    src = 'const a = `//a[contains(@data-test, "x-${v}")]`;\n'
    sites = find_xpath_sites(src)
    assert len(sites) == 1
    assert sites[0].quote == "`"
    assert "${v}" in sites[0].original


def test_find_xpath_sites_ignores_url_comment() -> None:
    src = "// https://example.com/foo\nconst u = 'https://a.b/c';\n"
    assert find_xpath_sites(src) == []


# ---------------------------------------------------------------------------
# rewrite_file — end-to-end on a synthetic BasePage.ts clone
# ---------------------------------------------------------------------------


_SYNTHETIC_BASEPAGE = """\
import { Page } from '@playwright/test';

export class BasePage {
    page: Page;

    elements: Record<string, string> = {
        inpUsername: '//input[@data-test="username-input"]',
        inpPassword: '//input[@data-test="password-input"]',
        btnLogin: '//input[@data-test="submit-button"]',
        btnHomeHeading: '//h1[contains(normalize-space(.), "GRC HOME")]',
        btnLogout: '//span[text()="Logout"]',
    };

    constructor(page: Page) { this.page = page; }

    async login(user: string, pass: string): Promise<void> {
        await this.page.locator(this.elements.inpUsername).fill(user);
        await this.page.locator(this.elements.inpPassword).fill(pass);
        await this.page.locator(this.elements.btnLogin).click();
    }

    async goHome(): Promise<void> {
        await this.page.locator(this.elements.btnHomeHeading).click();
    }
}
"""


def test_rewrite_file_container_migration(tmp_path: Path) -> None:
    p = tmp_path / "BasePage.ts"
    p.write_text(_SYNTHETIC_BASEPAGE, encoding="utf-8")
    report = rewrite_file(p)

    assert report.changed
    assert report.container_migrated
    assert report.testid_attr_needed
    # 4 rewrites for the 4 xpath keys with data-test / role / text patterns
    # (the 5th key `btnLogout` uses `span` which isn't in the role map, so
    # falls to getByText)
    assert len(report.rewritten) >= 5
    assert report.stragglers == []

    new_text = report.new_text
    # Container migrated from `Record<string, string>` to arrow-factories
    assert "() => this.page.getByTestId('username-input')" in new_text
    assert "// was: '//input[@data-test=\"username-input\"]'" in new_text
    assert "() => this.page.getByRole('heading', { name: 'GRC HOME' })" in new_text
    # Call sites collapsed
    assert "this.elements.inpUsername()" in new_text
    assert "this.elements.btnLogin()" in new_text
    # No lingering `this.page.locator(this.elements.X)` for migrated keys
    assert "this.page.locator(this.elements.inpUsername)" not in new_text


def test_rewrite_file_call_sites_migrated_count(tmp_path: Path) -> None:
    p = tmp_path / "BasePage.ts"
    p.write_text(_SYNTHETIC_BASEPAGE, encoding="utf-8")
    report = rewrite_file(p)
    # 3 call sites in login + 1 in goHome = 4
    assert report.call_sites_migrated == 4


def test_rewrite_file_inline_locator_rewrite(tmp_path: Path) -> None:
    src = (
        "import { Page } from '@playwright/test';\n"
        "export class X {\n"
        "    page: Page;\n"
        "    constructor(p: Page) { this.page = p; }\n"
        "    async click(): Promise<void> {\n"
        "        await this.page.locator('//input[@data-test=\"submit\"]').click();\n"
        "    }\n"
        "}\n"
    )
    p = tmp_path / "X.ts"
    p.write_text(src, encoding="utf-8")
    report = rewrite_file(p)

    assert report.changed
    assert not report.container_migrated  # no elements block
    assert report.testid_attr_needed
    assert "this.page.getByTestId('submit')" in report.new_text
    # A trailing /* was: … */ comment for reviewer reference
    assert "was:" in report.new_text
    assert "'//input[@data-test=\"submit\"]'" in report.new_text  # in comment


def test_rewrite_file_dry_run_does_not_write(tmp_path: Path) -> None:
    p = tmp_path / "BasePage.ts"
    p.write_text(_SYNTHETIC_BASEPAGE, encoding="utf-8")
    report = rewrite_file(p, dry_run=True)
    assert report.changed
    # File on disk is untouched
    assert p.read_text(encoding="utf-8") == _SYNTHETIC_BASEPAGE


def test_rewrite_file_no_change_when_no_xpath(tmp_path: Path) -> None:
    src = (
        "export class Clean {\n"
        "    x = () => this.page.getByTestId('already-idiomatic');\n"
        "}\n"
    )
    p = tmp_path / "Clean.ts"
    p.write_text(src, encoding="utf-8")
    report = rewrite_file(p)
    assert not report.changed
    assert report.rewritten == []
    assert report.stragglers == []


def test_rewrite_file_container_with_straggler_kept_exempt(tmp_path: Path) -> None:
    """When an xpath value inside `elements` can't be translated, keep the
    key with an exempt marker so the quality gate doesn't fail."""
    src = (
        "export class X {\n"
        "    page: any;\n"
        "    elements: Record<string, string> = {\n"
        "        goodKey: '//input[@data-test=\"a\"]',\n"
        "        badKey: '//div[@ref=\"b\"]/parent::td',\n"
        "    };\n"
        "}\n"
    )
    p = tmp_path / "Mixed.ts"
    p.write_text(src, encoding="utf-8")
    report = rewrite_file(p)
    assert report.container_migrated
    assert len(report.rewritten) == 1
    assert len(report.stragglers) == 1
    assert "qtea-xpath-exempt" in report.new_text
    # Good key still migrated
    assert "getByTestId('a')" in report.new_text
