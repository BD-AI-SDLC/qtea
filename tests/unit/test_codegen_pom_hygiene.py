"""Unit tests for the Phase A3.5 / B.5.5 hygiene gates in
`codegen_pom_hygiene`.

Regression coverage for the two shipping defects observed on run
20260708-121117-99f5ed:

  * `verifyMarketingConsentPositionAndLabel` in `TrialPage.ts` contains
    `expect(marketingCheckbox).toBeAttached(...)` inside the POM body
    (assertions belong in tests, not POMs).
  * The generated spec calls `await trialPage.verifyMarketingConsentPositionAndLabel();`
    with no consumption of the returned `{isBelowLegalProtection, labelText}`,
    so the German-label match the plan required never executes.

All fixtures use `tmp_path` with inline text — no external files —
matching the style of `tests/unit/test_codegen_body_verify.py`.
"""

from __future__ import annotations

from pathlib import Path

from qtea.codegen_pom_hygiene import (
    HygieneViolation,
    _is_call_consumed,
    _js_method_return_type,
    _non_void_agent_methods,
    find_pom_assertion_violations,
    find_return_consumption_violations,
    find_undefined_locator_ref_violations,
)
from qtea.codegen_reconcile import _js_strip


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 2 — find_pom_assertion_violations
# ---------------------------------------------------------------------------


def test_pom_assertion_gate_flags_expect_in_agent_method(tmp_path: Path):
    """Regression for run 20260708: the exact
    `expect(marketingCheckbox).toBeAttached(...)` shape must be flagged
    when the enclosing method was agent-authored."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "import { expect, Locator, Page } from '@playwright/test';\n"
        "export class TrialPage {\n"
        "  constructor(readonly page: Page) {}\n"
        "  async verifyMarketingConsentPositionAndLabel(): "
        "Promise<{ isBelowLegalProtection: boolean; labelText: string | null }> {\n"
        "    const marketingCheckbox = this.page.locator('input#mc');\n"
        "    await expect(marketingCheckbox).toBeAttached({ timeout: 10000 });\n"
        "    return { isBelowLegalProtection: true, labelText: 'x' };\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage",
        {"verifyMarketingConsentPositionAndLabel"},
        language="typescript",
    )
    assert len(vs) == 1
    v = vs[0]
    assert isinstance(v, HygieneViolation)
    assert v.rule == "pom-assertion"
    assert v.method == "verifyMarketingConsentPositionAndLabel"
    assert "expect(" in v.message


def test_pom_assertion_gate_passes_locator_probe(tmp_path: Path):
    """The prescribed probe shape: a kind:"assertion" method that returns the
    Locator with NO expect() in the body must pass the pom-assertion gate.
    The matcher lives in the test — this is what makes the two gates
    (pom-assertion + body-verify) jointly satisfiable, closing the apparent
    deadlock from run 20260708."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "import { Locator, Page } from '@playwright/test';\n"
        "export class TrialPage {\n"
        "  constructor(readonly page: Page) {}\n"
        "  getMandatoryCheckboxes(): Locator {\n"
        "    return this.page.locator(sel.TrialPageWarnings);\n"
        "  }\n"
        "  getMarketingConsentCheckbox(): Locator {\n"
        "    return this.page.locator(sel.CHECKBOX_MARKETING_CONSENT);\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage",
        {"getMandatoryCheckboxes", "getMarketingConsentCheckbox"},
        language="typescript",
    )
    assert vs == []


def test_pom_assertion_gate_ignores_expect_in_comments(tmp_path: Path):
    """`_js_strip` neutralises comments before the regex battery runs,
    so a `// use expect(...)` note in the POM must not false-positive."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getLabel(): Promise<string> {\n"
        "    // TODO: replace with expect(loc).toHaveText(...) in the test\n"
        "    return await this.page.locator('#l').textContent();\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"getLabel"}, language="typescript",
    )
    assert vs == []


def test_pom_assertion_gate_ignores_pre_existing_methods(tmp_path: Path):
    """Method not in agent_authored_methods → not flagged. Matches the
    severity model of `_scan_pom_assertions` (warning vs error split)."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async legacyMethod(): Promise<void> {\n"
        "    expect(this.page.locator('x')).toBeVisible();\n"
        "  }\n"
        "  async newAuthoredMethod(): Promise<string> {\n"
        "    return await this.page.locator('y').textContent() ?? '';\n"
        "  }\n"
        "}\n",
    )
    # Only `newAuthoredMethod` is authored by qtea. legacyMethod has an
    # `expect()` but is pre-existing SUT code — this gate skips it.
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"newAuthoredMethod"}, language="typescript",
    )
    assert vs == []


def test_pom_assertion_gate_catches_assertthat(tmp_path: Path):
    """`assertThat(` pattern must be caught in a TS POM (Playwright users
    occasionally reach for AssertJ-style helpers imported from a wrapper)."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async foo(): Promise<void> {\n"
        "    assertThat(this.page.url()).contains('trial');\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"foo"}, language="typescript",
    )
    assert len(vs) == 1
    assert vs[0].rule == "pom-assertion"


def test_pom_assertion_gate_catches_cypress_should(tmp_path: Path):
    """`.should(` chainable pattern must be caught."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async bar(): Promise<void> {\n"
        "    cy.get('.btn').should('be.visible');\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"bar"}, language="typescript",
    )
    assert len(vs) == 1


def test_pom_assertion_gate_skips_python(tmp_path: Path):
    """Python POMs are not wired into this EARLY gate — it relies on
    `test_indexer._scan_pom_assertions` at Phase C instead (see the
    docstring) — the language filter must short-circuit."""
    pom = tmp_path / "pages" / "trial.py"
    _write(pom, "class TrialPage:\n    def foo(self):\n        assert True\n")
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"foo"}, language="python",
    )
    assert vs == []


# ---------------------------------------------------------------------------
# Fix 2 — find_pom_assertion_violations (Java)
# ---------------------------------------------------------------------------


def test_pom_assertion_gate_flags_assertthat_in_java_method(tmp_path: Path):
    """Playwright-Java `assertThat(...)` inside an agent-authored POM
    method is the exact Java analogue of the TS regression — must be
    flagged."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public boolean verifyMarketingConsentPosition() {\n"
        "        Locator marketingCheckbox = page.locator(\"input#mc\");\n"
        "        assertThat(marketingCheckbox).isAttached();\n"
        "        return true;\n"
        "    }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"verifyMarketingConsentPosition"}, language="java",
    )
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "pom-assertion"
    assert v.method == "verifyMarketingConsentPosition"
    assert "assertThat(" in v.message


def test_pom_assertion_gate_flags_junit_assertequals_in_java_method(tmp_path: Path):
    """`Assertions.assertEquals(...)` (JUnit 5 / TestNG) inside a POM
    method must also be flagged."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public String getLabel() {\n"
        "        String text = page.locator(\"#l\").textContent();\n"
        "        Assertions.assertEquals(\"Expected\", text);\n"
        "        return text;\n"
        "    }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"getLabel"}, language="java",
    )
    assert len(vs) == 1
    assert vs[0].rule == "pom-assertion"


def test_pom_assertion_gate_ignores_pre_existing_java_methods(tmp_path: Path):
    """Method not in agent_authored_methods → not flagged, same severity
    model as TS."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public void legacyMethod() {\n"
        "        assertThat(page.locator(\"x\")).isVisible();\n"
        "    }\n"
        "    public String newAuthoredMethod() {\n"
        "        return page.locator(\"y\").textContent();\n"
        "    }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", {"newAuthoredMethod"}, language="java",
    )
    assert vs == []


def test_pom_assertion_gate_empty_authored_set_returns_empty(tmp_path: Path):
    """No agent-authored methods → nothing to check → empty list."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async legacy(): Promise<void> {\n"
        "    expect(x).toBeTruthy();\n"
        "  }\n"
        "}\n",
    )
    vs = find_pom_assertion_violations(
        pom, "TrialPage", set(), language="typescript",
    )
    assert vs == []


# ---------------------------------------------------------------------------
# Fix 3 — _js_method_return_type
# ---------------------------------------------------------------------------


def test_return_type_extractor_handles_bare_type():
    src = "async foo(x: number): Promise<string> {\n  return 'x';\n}"
    close = src.index(")")
    assert _js_method_return_type(src, close) == "Promise<string>"


def test_return_type_extractor_handles_nested_generics():
    """The failing run's exact shape: Promise<{a: boolean; b: string|null}>."""
    src = (
        "async verifyX(): Promise<{ isBelowLegalProtection: boolean; "
        "labelText: string | null }> {\n"
        "  return { isBelowLegalProtection: true, labelText: null };\n"
        "}"
    )
    close = src.index(")")
    rt = _js_method_return_type(src, close)
    assert rt is not None
    assert rt.startswith("Promise<")
    assert rt.endswith(">")
    assert "isBelowLegalProtection" in rt


def test_return_type_extractor_handles_void():
    src = "async foo(): Promise<void> {\n  return;\n}"
    close = src.index(")")
    assert _js_method_return_type(src, close) == "Promise<void>"


def test_return_type_extractor_returns_none_when_absent():
    src = "async foo() {\n  return;\n}"
    close = src.index(")")
    assert _js_method_return_type(src, close) is None


def test_return_type_extractor_handles_object_literal_return_type():
    src = "async foo(): { a: number; b: string } {\n  return { a: 1, b: 'x' };\n}"
    close = src.index(")")
    rt = _js_method_return_type(src, close)
    assert rt is not None
    assert rt.startswith("{")
    assert rt.endswith("}")


def test_return_type_extractor_handles_union_type():
    src = "async foo(): Promise<string | null> {\n  return null;\n}"
    close = src.index(")")
    assert _js_method_return_type(src, close) == "Promise<string | null>"


# ---------------------------------------------------------------------------
# Fix 3 — _non_void_agent_methods
# ---------------------------------------------------------------------------


def test_non_void_classification_ignores_void_and_promise_void():
    pom_src = (
        "export class P {\n"
        "  async a(): Promise<void> { return; }\n"
        "  async b(): void { }\n"
        "  async c(): Promise<string> { return 'x'; }\n"
        "  async d(): Promise<{k: number}> { return {k: 1}; }\n"
        "  async e() { return; }\n"  # no annotation → treated as void
        "}\n"
    )
    authored = {"a", "b", "c", "d", "e"}
    non_void = _non_void_agent_methods(pom_src, "P", authored)
    assert non_void == {"c", "d"}


def test_non_void_classification_treats_any_and_unknown_as_void():
    """`any` / `unknown` are too weak to bind — treat as void so the gate
    doesn't over-fail on deliberate probe methods."""
    pom_src = (
        "export class P {\n"
        "  async a(): Promise<any> { return 1; }\n"
        "  async b(): Promise<unknown> { return 1; }\n"
        "}\n"
    )
    assert _non_void_agent_methods(pom_src, "P", {"a", "b"}) == set()


# ---------------------------------------------------------------------------
# Fix 3 — _is_call_consumed
# ---------------------------------------------------------------------------


def test_is_call_consumed_flags_bare_await_statement():
    src = "async () => {\n  await pom.foo();\n}"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is False


def test_is_call_consumed_accepts_assignment():
    src = "const x = await pom.foo();"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is True


def test_is_call_consumed_accepts_destructuring():
    src = "const { a, b } = await pom.foo();"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is True


def test_is_call_consumed_accepts_expect_wrapper():
    src = "expect(await pom.foo()).toBe('x');"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is True


def test_is_call_consumed_accepts_return_statement():
    src = "async () => { return await pom.foo(); }"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is True


def test_is_call_consumed_accepts_call_argument():
    src = "helper(await pom.foo(), 42);"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is True


def test_is_call_consumed_flags_bare_call_after_semicolon():
    src = "foo();\nawait pom.foo();"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is False


def test_is_call_consumed_flags_bare_call_after_block_open():
    src = "{\n  await pom.foo();\n}"
    call_start = src.index("await pom.foo(")
    assert _is_call_consumed(_js_strip(src), call_start) is False


# ---------------------------------------------------------------------------
# Fix 3 — find_return_consumption_violations (integration-ish)
# ---------------------------------------------------------------------------


def test_return_consumption_flags_discarded_await_regression(tmp_path: Path):
    """Regression for the exact `await trialPage.verifyMarketingConsentPositionAndLabel();`
    shape from run 20260708."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyMarketingConsentPositionAndLabel(): "
        "Promise<{ isBelowLegalProtection: boolean; labelText: string | null }> {\n"
        "    return { isBelowLegalProtection: true, labelText: 'x' };\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_marketing_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('mc position', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  await trialPage.verifyMarketingConsentPositionAndLabel();\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage",
        {"verifyMarketingConsentPositionAndLabel"},
        [test], language="typescript",
    )
    assert len(vs) == 1
    v = vs[0]
    assert v.rule == "return-consumption"
    assert v.method == "verifyMarketingConsentPositionAndLabel"
    assert "TrialPage" in v.message


def test_return_consumption_accepts_expect_wrapped_call(tmp_path: Path):
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getLabel(): Promise<string> { return 'x'; }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_label_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('label', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  expect(await trialPage.getLabel()).toBe('Expected');\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", {"getLabel"}, [test], language="typescript",
    )
    assert vs == []


def test_return_consumption_accepts_destructured_call(tmp_path: Path):
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getStuff(): Promise<{ a: number; b: string }> {\n"
        "    return { a: 1, b: 'x' };\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_stuff_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('stuff', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  const { a, b } = await trialPage.getStuff();\n"
        "  expect(a).toBe(1);\n"
        "  expect(b).toBe('x');\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", {"getStuff"}, [test], language="typescript",
    )
    assert vs == []


def test_return_consumption_ignores_promise_void_signature(tmp_path: Path):
    """When the POM legitimately returns `Promise<void>` (assertion lives
    inside), no consumption check applies — bare `await pom.foo()` is fine."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async doThing(): Promise<void> { return; }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_void_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('void', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  await trialPage.doThing();\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", {"doThing"}, [test], language="typescript",
    )
    assert vs == []


def test_return_consumption_ignores_pre_existing_methods(tmp_path: Path):
    """Method not in agent_authored_methods → not scanned."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async legacyProbe(): Promise<string> { return 'x'; }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_legacy_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('legacy', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  await trialPage.legacyProbe();\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", set(), [test], language="typescript",
    )
    assert vs == []


def test_return_consumption_ignores_tests_that_dont_import_class(tmp_path: Path):
    """A test that never imports `TrialPage` cannot have a valid receiver
    bound to it, so no violations should be produced from such files."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getX(): Promise<string> { return 'x'; }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_other_test.spec.ts"
    _write(test,
        "import { test } from '@playwright/test';\n"
        "test('other', async () => {\n"
        "  const trialPage: any = { getX: async () => 'x' };\n"
        "  await trialPage.getX();\n"  # obj_name matches but no import
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", {"getX"}, [test], language="typescript",
    )
    assert vs == []


def test_return_consumption_flags_multiple_call_sites(tmp_path: Path):
    """One test may call the same POM method twice — both discards must
    surface, not just the first."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getX(): Promise<string> { return 'x'; }\n"
        "}\n",
    )
    test = tmp_path / "src" / "tests" / "qtea_multi_test.spec.ts"
    _write(test,
        "import { test, expect } from '@playwright/test';\n"
        "import { TrialPage } from '../pages/TrialPage';\n"
        "test('multi', async ({ page }) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  await trialPage.getX();\n"
        "  await trialPage.getX();\n"
        "});\n",
    )
    vs = find_return_consumption_violations(
        pom, "TrialPage", {"getX"}, [test], language="typescript",
    )
    assert len(vs) == 2
    assert {v.line for v in vs} == {5, 6}


# ---------------------------------------------------------------------------
# find_undefined_locator_ref_violations
# ---------------------------------------------------------------------------


def test_undefined_locator_ref_flags_dangling_bag_reference(tmp_path: Path):
    """Regression: the extender references
    `BASE_LOCATORS.NOTIFICATION_INBOX_ITEMS` from an agent-authored method but
    the key is defined in neither the POM nor the locator bag -> must flag."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "import { BASE_LOCATORS } from './locators/BasePage.locators';\n"
        "export class BasePage {\n"
        "  getNotificationInboxItemsLocator(): Locator {\n"
        "    return this.page.locator(BASE_LOCATORS.NOTIFICATION_INBOX_ITEMS);\n"
        "  }\n"
        "}\n",
    )
    locators = tmp_path / "src" / "pages" / "locators" / "BasePage.locators.ts"
    _write(locators,
        "export const BASE_LOCATORS = {\n"
        "  btnLogin: '//input[@id=\"login\"]',\n"
        "};\n",
    )
    vs = find_undefined_locator_ref_violations(
        pom, "BasePage", {"getNotificationInboxItemsLocator"},
        {"NOTIFICATION_INBOX_ITEMS"},
        language="typescript", definition_files=[locators],
    )
    assert len(vs) == 1
    assert vs[0].rule == "undefined-locator-ref"
    assert "NOTIFICATION_INBOX_ITEMS" in vs[0].message
    assert vs[0].method == "getNotificationInboxItemsLocator"


def test_undefined_locator_ref_passes_when_defined_in_bag(tmp_path: Path):
    """When the key IS defined in the locator bag, no violation."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "export class BasePage {\n"
        "  getItems(): Locator {\n"
        "    return this.page.locator(BASE_LOCATORS.NOTIFICATION_INBOX_ITEMS);\n"
        "  }\n"
        "}\n",
    )
    locators = tmp_path / "src" / "pages" / "locators" / "BasePage.locators.ts"
    _write(locators,
        "export const BASE_LOCATORS = {\n"
        "  NOTIFICATION_INBOX_ITEMS: '[data-test=\"inbox-item\"]',\n"
        "};\n",
    )
    assert find_undefined_locator_ref_violations(
        pom, "BasePage", {"getItems"}, {"NOTIFICATION_INBOX_ITEMS"},
        language="typescript", definition_files=[locators],
    ) == []


def test_undefined_locator_ref_passes_when_inlined(tmp_path: Path):
    """A create_tbd locator the extender legitimately INLINED (constant name
    absent, selector literal in the body) is never referenced as `.NAME` and
    must not be flagged."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "export class BasePage {\n"
        "  openInbox(): Locator {\n"
        "    return this.page.getByRole('button', { name: 'Inbox' });\n"
        "  }\n"
        "}\n",
    )
    assert find_undefined_locator_ref_violations(
        pom, "BasePage", {"openInbox"}, {"NOTIFICATION_INBOX_ICON"},
        language="typescript", definition_files=[],
    ) == []


def test_undefined_locator_ref_ignores_non_agent_methods(tmp_path: Path):
    """A dangling reference in a PRE-EXISTING (non-agent-authored) method is
    out of scope — only methods qtea just wrote are checked."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "export class BasePage {\n"
        "  preExisting(): Locator {\n"
        "    return this.page.locator(BASE_LOCATORS.LEGACY_KEY);\n"
        "  }\n"
        "}\n",
    )
    assert find_undefined_locator_ref_violations(
        pom, "BasePage", {"someOtherMethod"}, {"LEGACY_KEY"},
        language="typescript", definition_files=[],
    ) == []


def test_undefined_locator_ref_python_dangling(tmp_path: Path):
    """Python analogue: `Locators.INBOX_ITEMS` referenced, never defined."""
    pom = tmp_path / "pages" / "base_page.py"
    _write(pom,
        "class BasePage:\n"
        "    def notification_items(self):\n"
        "        return self.page.locator(Locators.INBOX_ITEMS)\n",
    )
    vs = find_undefined_locator_ref_violations(
        pom, "BasePage", {"notification_items"}, {"INBOX_ITEMS"},
        language="python", definition_files=[],
    )
    assert len(vs) == 1
    assert "INBOX_ITEMS" in vs[0].message
