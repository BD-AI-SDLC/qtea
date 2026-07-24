"""Unit tests for the Phase A3.5 body verifier (`codegen_body_verify`).

Regression coverage for the RCA-C defects observed on run
20260708-121117-99f5ed:

  * `verifyMarketingConsentBelowLegalProtection` invented
    ``toBeGreaterThanOrEqual(4)`` when the strategy said "3 checkboxes"
  * `verifyMarketingConsentLabelText` used ``.length > 0`` instead of
    an exact-text match against ``EXPECTED_MARKETING_CONSENT_LABEL``
  * Both used ``.nth(count - 1)`` instead of the named marketing-consent
    locator constant

All fixtures use ``tmp_path`` with inline text — no external files.
"""

from __future__ import annotations

from pathlib import Path

from qtea.codegen_body_verify import (
    BodyViolation,
    verify_method_bodies,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# exact_count
# ---------------------------------------------------------------------------


def test_body_verify_flags_count_drift_regression(tmp_path: Path):
    """Regression for the exact ``>= 4`` hallucination from run 20260708.
    Contract says exact count 3; body emits ``toBeGreaterThanOrEqual(4)``
    → violation must call out count-drift explicitly."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyMarketingConsentBelowLegalProtection() {\n"
        "    const checkboxes = this.page.locator(sel.CHECKBOXES);\n"
        "    const count = await checkboxes.count();\n"
        "    expect(count).toBeGreaterThanOrEqual(4);\n"
        "  }\n"
        "}\n",
    )
    missing_methods = [{
        "name": "verifyMarketingConsentBelowLegalProtection",
        "signature": "verifyMarketingConsentBelowLegalProtection(): Promise<void>",
        "kind": "assertion",
        "purpose": "There are exactly 3 mandatory checkboxes on the trial form.",
        "acceptance_criteria": [
            {"check": "exact_count", "locator": "TrialPageCheckboxes",
             "expected_literal": 3, "source_tc": "TC-TRCB-004"},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing_methods)
    assert len(vs) == 1
    v = vs[0]
    assert v.check == "exact_count"
    assert "count-drift" in v.message
    assert "3" in v.message
    assert "toBeGreaterThanOrEqual(4)" in v.message


def test_body_verify_passes_on_exact_count_match(tmp_path: Path):
    """Happy path — body uses ``toHaveCount(3)`` matching the contract."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyCount() {\n"
        "    await expect(this.page.locator(sel.CHECKBOXES)).toHaveCount(3);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "verifyCount", "signature": "()", "kind": "assertion",
        "purpose": "Exactly three mandatory checkboxes rendered on the form.",
        "acceptance_criteria": [
            {"check": "exact_count", "expected_literal": 3},
        ],
    }]
    assert verify_method_bodies(pom, "TrialPage", missing) == []


def test_body_verify_flags_wrong_exact_count(tmp_path: Path):
    """Non-drift version — body uses ``toHaveCount(5)`` but contract says 3."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyCount() {\n"
        "    await expect(this.page.locator(sel.CHECKBOXES)).toHaveCount(5);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "verifyCount", "signature": "()", "kind": "assertion",
        "purpose": "Exactly three mandatory checkboxes rendered on the form.",
        "acceptance_criteria": [
            {"check": "exact_count", "expected_literal": 3},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing)
    assert len(vs) == 1
    assert "toHaveCount(3)" in vs[0].message
    assert "toHaveCount(5)" in vs[0].message


def test_body_verify_passes_on_exact_count_named_constant(tmp_path: Path):
    """Regression against exact_count false-green: the spec asserts
    ``toHaveCount(EXPECTED_NOTIFICATION_COUNT)`` where the constant equals the
    contract literal (1). The getter-style POM returns the Locator; the
    assertion lives at the spec call site. Before the fix, the exact_count
    matcher had no symbol branch and misreported this correct assertion as
    "missing toHaveCount(1)"."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "export class BasePage {\n"
        "  getNotificationInboxItemsLocator(): Locator {\n"
        "    return this.page.locator(BASE_LOCATORS.NOTIFICATION_INBOX_ITEMS);\n"
        "  }\n"
        "}\n",
    )
    spec = tmp_path / "tests" / "qtea_entity.spec.ts"
    _write(spec,
        "const EXPECTED_NOTIFICATION_COUNT = 1;\n"
        "test('entity', async () => {\n"
        "  await expect(basePage.getNotificationInboxItemsLocator()).toHaveCount(\n"
        "    EXPECTED_NOTIFICATION_COUNT\n"
        "  );\n"
        "});\n",
    )
    missing = [{
        "name": "getNotificationInboxItemsLocator",
        "signature": "getNotificationInboxItemsLocator(): Locator",
        "kind": "assertion",
        "purpose": "Inbox contains exactly one new notification.",
        "acceptance_criteria": [
            {"check": "exact_count", "locator": "NOTIFICATION_INBOX_ITEMS",
             "expected_literal": 1, "source_tc": "TC-ENTITY-001"},
        ],
    }]
    assert verify_method_bodies(
        pom, "BasePage", missing, test_files=[spec], language="typescript",
    ) == []


def test_body_verify_flags_named_constant_wrong_value(tmp_path: Path):
    """A named count constant that folds to the WRONG value must still fail —
    the symbol branch resolves the value, it does not blindly accept any
    identifier."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    _write(pom,
        "export class BasePage {\n"
        "  getItemsLocator(): Locator {\n"
        "    return this.page.locator(sel.ITEMS);\n"
        "  }\n"
        "}\n",
    )
    spec = tmp_path / "tests" / "qtea_x.spec.ts"
    _write(spec,
        "const EXPECTED = 2;\n"
        "test('x', async () => {\n"
        "  await expect(basePage.getItemsLocator()).toHaveCount(EXPECTED);\n"
        "});\n",
    )
    missing = [{
        "name": "getItemsLocator", "signature": "getItemsLocator(): Locator",
        "kind": "assertion", "purpose": "Exactly one item.",
        "acceptance_criteria": [{"check": "exact_count", "expected_literal": 1}],
    }]
    vs = verify_method_bodies(
        pom, "BasePage", missing, test_files=[spec], language="typescript",
    )
    assert len(vs) == 1
    assert "toHaveCount(1)" in vs[0].message
    assert "EXPECTED" in vs[0].message


def test_body_verify_passes_on_expected_symbol_name_match(tmp_path: Path):
    """When the plan supplies ``expected_symbol``, a matching symbol name at the
    call site passes even when the declaration is imported (not foldable in
    view) — mirrors the exact_text/exact_attribute symbol path."""
    pom = tmp_path / "src" / "pages" / "P.ts"
    _write(pom,
        "export class P {\n"
        "  getItems(): Locator { return this.page.locator(sel.ITEMS); }\n"
        "}\n",
    )
    spec = tmp_path / "tests" / "qtea_y.spec.ts"
    _write(spec,
        "import { ROW_COUNT } from './consts';\n"
        "test('y', async () => {\n"
        "  await expect(p.getItems()).toHaveCount(ROW_COUNT);\n"
        "});\n",
    )
    missing = [{
        "name": "getItems", "signature": "getItems(): Locator",
        "kind": "assertion", "purpose": "row count",
        "acceptance_criteria": [
            {"check": "exact_count", "expected_literal": 3,
             "expected_symbol": "ROW_COUNT"},
        ],
    }]
    assert verify_method_bodies(
        pom, "P", missing, test_files=[spec], language="typescript",
    ) == []


def test_body_verify_python_passes_named_constant(tmp_path: Path):
    """Python analogue: ``to_have_count(EXPECTED_NOTIFICATION_COUNT)`` with the
    constant declared in the test module folds to the contract literal."""
    pom = tmp_path / "pages" / "base_page.py"
    _write(pom,
        "class BasePage:\n"
        "    def notification_items(self):\n"
        "        return self.page.locator(ITEMS)\n",
    )
    test = tmp_path / "tests" / "qtea_entity.py"
    _write(test,
        "EXPECTED_NOTIFICATION_COUNT = 1\n"
        "def test_entity(page):\n"
        "    expect(BasePage(page).notification_items())"
        ".to_have_count(EXPECTED_NOTIFICATION_COUNT)\n",
    )
    missing = [{
        "name": "notification_items", "signature": "()", "kind": "assertion",
        "purpose": "exactly one notification",
        "acceptance_criteria": [{"check": "exact_count", "expected_literal": 1}],
    }]
    assert verify_method_bodies(
        pom, "BasePage", missing, test_files=[test], language="python",
    ) == []


def test_body_verify_java_passes_named_constant(tmp_path: Path):
    """Java analogue: ``hasCount(EXPECTED_NOTIFICATION_COUNT)`` with the
    constant declared ``static final int`` folds to the contract literal."""
    pom = tmp_path / "pages" / "BasePage.java"
    _write(pom,
        "public class BasePage {\n"
        "  public Locator notificationItems() {\n"
        "    return page.locator(ITEMS);\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "QteaRopaTest.java"
    _write(test,
        "public class QteaRopaTest {\n"
        "  static final int EXPECTED_NOTIFICATION_COUNT = 1;\n"
        "  @Test void entity() {\n"
        "    assertThat(basePage.notificationItems())"
        ".hasCount(EXPECTED_NOTIFICATION_COUNT);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "notificationItems",
        "signature": "Locator notificationItems()",
        "kind": "assertion", "purpose": "exactly one notification",
        "acceptance_criteria": [{"check": "exact_count", "expected_literal": 1}],
    }]
    assert verify_method_bodies(
        pom, "BasePage", missing, test_files=[test], language="java",
    ) == []


# ---------------------------------------------------------------------------
# exact_text (with tautology detection)
# ---------------------------------------------------------------------------


def test_body_verify_flags_length_gt_zero_tautology(tmp_path: Path):
    """Regression for ``verifyMarketingConsentLabelText`` — body checks
    ``labelText?.trim().length > 0`` instead of matching the expected
    German label. The rule requires an exact-text match; the tautology
    that "any non-empty label passes" is called out separately."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyMarketingConsentLabelText() {\n"
        "    const label = this.page.locator(sel.MARKETING_CONSENT_LABEL);\n"
        "    const text = await label.textContent();\n"
        "    expect(text?.trim().length).toBeGreaterThan(0);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "verifyMarketingConsentLabelText", "signature": "()",
        "kind": "assertion",
        "purpose": "The marketing consent label text exactly matches the "
                   "German copy from the strategy.",
        "acceptance_criteria": [
            {"check": "exact_text",
             "locator": "MARKETING_CONSENT_LABEL",
             "expected_symbol": "EXPECTED_MARKETING_CONSENT_LABEL",
             "source_tc": "TC-TRCB-001"},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing)
    # First violation should be "no toHaveText assertion" — the length>0
    # form does NOT satisfy the exact_text contract.
    assert any(
        v.check == "exact_text" and
        "EXPECTED_MARKETING_CONSENT_LABEL" in v.message
        for v in vs
    )


def test_body_verify_passes_when_test_contains_exact_expect(tmp_path: Path):
    """The happy shape after Fix 5 (assertions in test, not POM):
    POM returns raw value; test asserts against expected symbol."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getMarketingConsentLabelText() {\n"
        "    return this.page.locator(sel.MARKETING_CONSENT_LABEL).textContent();\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "trial.spec.ts"
    _write(test,
        "test('label', async ({page}) => {\n"
        "  const pom = new TrialPage(page);\n"
        "  const label = pom.page.locator(sel.MARKETING_CONSENT_LABEL);\n"
        "  await expect(label).toHaveText(EXPECTED_MARKETING_CONSENT_LABEL);\n"
        "});\n",
    )
    missing = [{
        "name": "getMarketingConsentLabelText", "signature": "()",
        "kind": "assertion",
        "purpose": "Get the marketing consent label so tests can assert "
                   "exact text.",
        "acceptance_criteria": [
            {"check": "exact_text",
             "locator": "MARKETING_CONSENT_LABEL",
             "expected_symbol": "EXPECTED_MARKETING_CONSENT_LABEL"},
        ],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test],
    ) == []


# ---------------------------------------------------------------------------
# boundingbox_below (with nth-arithmetic detection)
# ---------------------------------------------------------------------------


def test_body_verify_flags_missing_locator_reference(tmp_path: Path):
    """Regression for ``verifyMarketingConsentBelowLegalProtection`` — the
    body uses ``.nth(count - 1)`` / ``.nth(count - 2)`` instead of the
    named locators the criterion asked for."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyBelow() {\n"
        "    const checkboxes = this.page.locator(sel.CHECKBOXES);\n"
        "    const count = await checkboxes.count();\n"
        "    const last = checkboxes.nth(count - 1);\n"
        "    const secondLast = checkboxes.nth(count - 2);\n"
        "    const a = await last.boundingBox();\n"
        "    const b = await secondLast.boundingBox();\n"
        "    expect(a!.y).toBeGreaterThan(b!.y);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "verifyBelow", "signature": "()",
        "kind": "assertion",
        "purpose": "Marketing consent checkbox appears strictly below the "
                   "legal-protection checkbox in DOM order.",
        "acceptance_criteria": [
            {"check": "boundingbox_below",
             "locator": "CHECKBOX_MARKETING_CONSENT",
             "reference_locator": "CHECKBOX_LEGAL_PROTECTION"},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing)
    assert len(vs) == 1
    v = vs[0]
    assert v.check == "boundingbox_below"
    assert "CHECKBOX_MARKETING_CONSENT" in v.message
    assert "CHECKBOX_LEGAL_PROTECTION" in v.message


def test_body_verify_passes_on_boundingbox_below_with_named_locators(
    tmp_path: Path,
):
    """Happy path — body references BOTH named locators and does the
    y-comparison."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async verifyBelow() {\n"
        "    const marketing = this.page.locator(sel.CHECKBOX_MARKETING_CONSENT);\n"
        "    const legal = this.page.locator(sel.CHECKBOX_LEGAL_PROTECTION);\n"
        "    const a = await marketing.boundingBox();\n"
        "    const b = await legal.boundingBox();\n"
        "    expect(a!.y).toBeGreaterThan(b!.y);\n"
        "  }\n"
        "}\n",
    )
    missing = [{
        "name": "verifyBelow", "signature": "()", "kind": "assertion",
        "purpose": "Marketing consent checkbox appears strictly below the "
                   "legal-protection checkbox in DOM order.",
        "acceptance_criteria": [
            {"check": "boundingbox_below",
             "locator": "CHECKBOX_MARKETING_CONSENT",
             "reference_locator": "CHECKBOX_LEGAL_PROTECTION"},
        ],
    }]
    assert verify_method_bodies(pom, "TrialPage", missing) == []


def test_body_verify_passes_split_locator_probes_boundingbox(tmp_path: Path):
    """Split-probe positional check (the model the docs now prescribe):
    two Locator getters — a kind:"assertion" anchor + a kind:"query" sibling
    — and the test extracts boundingBox() + compares .y. The anchor body only
    references ONE locator; the sibling body supplies the other. The verifier
    must union the sibling probe (called by the test) to see both locators +
    both boundingBox() calls, and find the `.y` compare in the test."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  getMarketingConsentCheckbox(): Locator {\n"
        "    return this.page.locator(sel.CHECKBOX_MARKETING_CONSENT);\n"
        "  }\n"
        "  getLegalProtectionCheckbox(): Locator {\n"
        "    return this.page.locator(sel.CHECKBOX_LEGAL_PROTECTION);\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "trial.spec.ts"
    _write(test,
        "test('below', async ({page}) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  const marketingBox = await trialPage.getMarketingConsentCheckbox().boundingBox();\n"
        "  const legalBox = await trialPage.getLegalProtectionCheckbox().boundingBox();\n"
        "  expect(marketingBox!.y).toBeGreaterThan(legalBox!.y);\n"
        "});\n",
    )
    missing = [
        {"name": "getMarketingConsentCheckbox", "signature": "(): Locator",
         "kind": "assertion",
         "purpose": "Marketing-consent checkbox renders strictly below the "
                    "legal-protection checkbox in DOM order.",
         "acceptance_criteria": [
             {"check": "boundingbox_below",
              "locator": "CHECKBOX_MARKETING_CONSENT",
              "reference_locator": "CHECKBOX_LEGAL_PROTECTION"}]},
        {"name": "getLegalProtectionCheckbox", "signature": "(): Locator",
         "kind": "query"},
    ]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test],
    ) == []


def test_body_verify_passes_split_number_probes_boundingbox(tmp_path: Path):
    """Defense-in-depth: even the number-returning split form (probes return
    the extracted `.y`, test compares plain locals without an adjacent `.y`)
    passes via the loose fallback, since both boxes + both locators are
    confirmed present across the probe pair."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getMarketingConsentTop(): Promise<number> {\n"
        "    const b = await this.page.locator(sel.CHECKBOX_MARKETING_CONSENT).boundingBox();\n"
        "    return b!.y;\n"
        "  }\n"
        "  async getLegalProtectionTop(): Promise<number> {\n"
        "    const b = await this.page.locator(sel.CHECKBOX_LEGAL_PROTECTION).boundingBox();\n"
        "    return b!.y;\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "trial.spec.ts"
    _write(test,
        "test('below', async ({page}) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  const marketingTop = await trialPage.getMarketingConsentTop();\n"
        "  const legalTop = await trialPage.getLegalProtectionTop();\n"
        "  expect(marketingTop).toBeGreaterThan(legalTop);\n"
        "});\n",
    )
    missing = [
        {"name": "getMarketingConsentTop", "signature": "(): Promise<number>",
         "kind": "assertion",
         "purpose": "Marketing-consent checkbox renders strictly below the "
                    "legal-protection checkbox in DOM order.",
         "acceptance_criteria": [
             {"check": "boundingbox_below",
              "locator": "CHECKBOX_MARKETING_CONSENT",
              "reference_locator": "CHECKBOX_LEGAL_PROTECTION"}]},
        {"name": "getLegalProtectionTop", "signature": "(): Promise<number>",
         "kind": "query"},
    ]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test],
    ) == []


def test_body_verify_flags_split_probe_missing_sibling_call(tmp_path: Path):
    """If the test never calls the sibling probe, the reference element is
    genuinely absent — the positional oracle must still flag it (the sibling
    union only kicks in for probes the test actually calls)."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  getMarketingConsentCheckbox(): Locator {\n"
        "    return this.page.locator(sel.CHECKBOX_MARKETING_CONSENT);\n"
        "  }\n"
        "  getLegalProtectionCheckbox(): Locator {\n"
        "    return this.page.locator(sel.CHECKBOX_LEGAL_PROTECTION);\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "trial.spec.ts"
    _write(test,
        "test('below', async ({page}) => {\n"
        "  const trialPage = new TrialPage(page);\n"
        "  const marketingBox = await trialPage.getMarketingConsentCheckbox().boundingBox();\n"
        "  expect(marketingBox).not.toBeNull();\n"
        "});\n",
    )
    missing = [
        {"name": "getMarketingConsentCheckbox", "signature": "(): Locator",
         "kind": "assertion",
         "purpose": "Marketing-consent checkbox renders strictly below the "
                    "legal-protection checkbox in DOM order.",
         "acceptance_criteria": [
             {"check": "boundingbox_below",
              "locator": "CHECKBOX_MARKETING_CONSENT",
              "reference_locator": "CHECKBOX_LEGAL_PROTECTION"}]},
        {"name": "getLegalProtectionCheckbox", "signature": "(): Locator",
         "kind": "query"},
    ]
    vs = verify_method_bodies(pom, "TrialPage", missing, test_files=[test])
    assert len(vs) == 1
    assert vs[0].check == "boundingbox_below"
    assert "CHECKBOX_LEGAL_PROTECTION" in vs[0].message


# ---------------------------------------------------------------------------
# kind filtering
# ---------------------------------------------------------------------------


def test_body_verify_ignores_non_assertion_kinds(tmp_path: Path):
    """Action + query kinds are not verified — no criteria expected."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async clickStart() { await this.page.click(sel.START); }\n"
        "  async getFirstName() { return this.page.locator(sel.FN).textContent(); }\n"
        "}\n",
    )
    missing = [
        {"name": "clickStart", "signature": "()", "kind": "action"},
        {"name": "getFirstName", "signature": "()", "kind": "query"},
    ]
    assert verify_method_bodies(pom, "TrialPage", missing) == []


def test_body_verify_flags_missing_method(tmp_path: Path):
    """When the extender didn't emit the method at all, flag it."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async other() {}\n"
        "}\n",
    )
    missing = [{
        "name": "missingMethod", "signature": "()", "kind": "assertion",
        "purpose": "A method the extender was supposed to write but omitted.",
        "acceptance_criteria": [
            {"check": "visible", "locator": "X"},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing)
    assert len(vs) == 1
    assert "not found" in vs[0].message


# ---------------------------------------------------------------------------
# language dispatch
# ---------------------------------------------------------------------------


def test_body_verify_python_flags_missing_assertion(tmp_path: Path):
    """Python IS now verified (findings 4/5). A kind=assertion getter with no
    to_be_visible matcher anywhere must be flagged, not silently passed."""
    pom = tmp_path / "src" / "pages" / "trial_page.py"
    _write(pom, "class TrialPage:\n    def verify(self):\n        return self.page\n")
    missing = [{
        "name": "verify", "signature": "()", "kind": "assertion",
        "purpose": "sufficiently long purpose text for schema validation",
        "acceptance_criteria": [{"check": "visible", "locator": "X"}],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing, language="python")
    assert len(vs) == 1 and vs[0].check == "visible"


def test_body_verify_python_passes_with_matcher(tmp_path: Path):
    """A Python getter+test that actually asserts the oracle passes."""
    pom = tmp_path / "src" / "pages" / "trial_page.py"
    _write(
        pom,
        "class TrialPage:\n"
        "    def error_banner(self):\n"
        "        return self.page.locator(ERROR_BANNER)\n",
    )
    test = tmp_path / "tests" / "qtea_login.py"
    _write(
        test,
        "def test_x(page):\n"
        "    expect(TrialPage(page).error_banner()).to_be_visible()\n",
    )
    missing = [{
        "name": "error_banner", "signature": "()", "kind": "assertion",
        "purpose": "sufficiently long purpose text for schema validation",
        "acceptance_criteria": [{"check": "visible", "locator": "ERROR_BANNER"}],
    }]
    vs = verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test], language="python",
    )
    assert vs == []


def test_body_verify_python_flags_count_drift(tmp_path: Path):
    """`>= n+1` when the oracle says exact n is count-drift (weaker), flagged."""
    pom = tmp_path / "src" / "pages" / "trial_page.py"
    _write(pom, "class TrialPage:\n    def rows(self):\n        return self.page.locator(ROWS)\n")
    test = tmp_path / "tests" / "qtea_rows.py"
    _write(test, "def test_x(page):\n    assert TrialPage(page).rows().count() >= 4\n")
    missing = [{
        "name": "rows", "signature": "()", "kind": "assertion",
        "purpose": "sufficiently long purpose text for schema validation",
        "acceptance_criteria": [{"check": "exact_count", "locator": "ROWS", "expected_literal": 3}],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing, test_files=[test], language="python")
    assert len(vs) == 1 and "count-drift" in vs[0].message


# ---------------------------------------------------------------------------
# Java dispatch
# ---------------------------------------------------------------------------


def test_body_verify_java_flags_missing_assertion(tmp_path: Path):
    """A kind=assertion getter with no Playwright-Java / JUnit matcher
    anywhere must be flagged, not silently passed."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public Locator errorBanner() {\n"
        "        return page.locator(\"#error\");\n"
        "    }\n"
        "}\n",
    )
    missing = [{
        "name": "errorBanner", "signature": "()", "kind": "assertion",
        "purpose": "sufficiently long purpose text for schema validation",
        "acceptance_criteria": [{"check": "visible", "locator": "ERROR_BANNER"}],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing, language="java")
    assert len(vs) == 1 and vs[0].check == "visible"


def test_body_verify_java_passes_when_test_contains_exact_expect(tmp_path: Path):
    """Happy shape: POM returns a raw Locator/value; the JUnit test asserts
    against it via Playwright-Java's `assertThat(...)` fluent API."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public Locator marketingConsentLabel() {\n"
        "        return page.locator(sel.MARKETING_CONSENT_LABEL);\n"
        "    }\n"
        "}\n",
    )
    test = tmp_path / "src" / "test" / "java" / "QteaTrialTest.java"
    _write(test,
        "public class QteaTrialTest {\n"
        "    @Test\n"
        "    void marketingConsentLabelMatchesStrategy() {\n"
        "        TrialPage trialPage = new TrialPage(page);\n"
        "        assertThat(trialPage.marketingConsentLabel())\n"
        "            .hasText(EXPECTED_MARKETING_CONSENT_LABEL);\n"
        "    }\n"
        "}\n",
    )
    missing = [{
        "name": "marketingConsentLabel", "signature": "()", "kind": "assertion",
        "purpose": "Get the marketing consent label so tests can assert exact text.",
        "acceptance_criteria": [{
            "check": "exact_text",
            "locator": "MARKETING_CONSENT_LABEL",
            "expected_symbol": "EXPECTED_MARKETING_CONSENT_LABEL",
        }],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test], language="java",
    ) == []


def test_body_verify_java_flags_count_drift(tmp_path: Path):
    """`assertTrue(count >= 4)` when the oracle says exact 3 is count-drift."""
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public Locator checkboxes() {\n"
        "        return page.locator(sel.CHECKBOXES);\n"
        "    }\n"
        "}\n",
    )
    test = tmp_path / "src" / "test" / "java" / "QteaCheckboxTest.java"
    _write(test,
        "public class QteaCheckboxTest {\n"
        "    @Test\n"
        "    void hasMandatoryCheckboxes() {\n"
        "        assertTrue(trialPage.checkboxes().count() >= 4);\n"
        "    }\n"
        "}\n",
    )
    missing = [{
        "name": "checkboxes", "signature": "()", "kind": "assertion",
        "purpose": "Exactly three mandatory checkboxes rendered on the form.",
        "acceptance_criteria": [
            {"check": "exact_count", "locator": "CHECKBOXES", "expected_literal": 3},
        ],
    }]
    vs = verify_method_bodies(pom, "TrialPage", missing, test_files=[test], language="java")
    assert len(vs) == 1 and "count-drift" in vs[0].message


def test_body_verify_java_ignores_non_assertion_kinds(tmp_path: Path):
    pom = tmp_path / "src" / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public void clickStart() { page.click(sel.START); }\n"
        "    public Locator firstName() { return page.locator(sel.FN); }\n"
        "}\n",
    )
    missing = [
        {"name": "clickStart", "signature": "()", "kind": "action"},
        {"name": "firstName", "signature": "()", "kind": "query"},
    ]
    assert verify_method_bodies(pom, "TrialPage", missing, language="java") == []


# ---------------------------------------------------------------------------
# Bare-value assertion fallback (exact_attribute / value_equals) + AssertJ
#
# Regression coverage: exact_attribute/value_equals previously had NO
# bare-assert fallback in ANY of the three stacks (only exact_count/
# exact_text did), and Java's existing bare-fallback regex could not match
# AssertJ's fluent `assertThat(x).isEqualTo(y)` despite the module comment
# claiming AssertJ coverage. TS/JS had no bare-value fallback at all.
# ---------------------------------------------------------------------------


def test_body_verify_ts_passes_value_equals_via_bare_toBe(tmp_path: Path):
    """A probe returning a raw (non-Locator) value, asserted via bare Jest
    `toBe(...)`, satisfies `value_equals` — TS/JS previously had no
    bare-value fallback at all."""
    pom = tmp_path / "src" / "pages" / "TrialPage.ts"
    _write(pom,
        "export class TrialPage {\n"
        "  async getDiscountRate(): Promise<number> {\n"
        "    return Number(await this.page.locator(sel.DISCOUNT).textContent());\n"
        "  }\n"
        "}\n",
    )
    test = tmp_path / "tests" / "trial.spec.ts"
    _write(test,
        "test('discount', async ({page}) => {\n"
        "  const pom = new TrialPage(page);\n"
        "  expect(await pom.getDiscountRate()).toBe(EXPECTED_DISCOUNT_RATE);\n"
        "});\n",
    )
    missing = [{
        "name": "getDiscountRate", "signature": "(): Promise<number>",
        "kind": "assertion",
        "purpose": "Discount rate shown on the trial page matches the strategy.",
        "acceptance_criteria": [
            {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
        ],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test],
    ) == []


def test_body_verify_python_passes_value_equals_via_bare_assert(tmp_path: Path):
    """Python analogue: a raw-value probe asserted via bare `assert x == y`
    satisfies `value_equals` — previously only exact_count/exact_text had
    this fallback in Python."""
    pom = tmp_path / "pages" / "trial_page.py"
    _write(pom,
        "class TrialPage:\n"
        "    def discount_rate(self):\n"
        "        return int(self.page.locator(DISCOUNT).text_content())\n",
    )
    test = tmp_path / "tests" / "qtea_trial.py"
    _write(test,
        "def test_discount(page):\n"
        "    assert TrialPage(page).discount_rate() == EXPECTED_DISCOUNT_RATE\n",
    )
    missing = [{
        "name": "discount_rate", "signature": "()", "kind": "assertion",
        "purpose": "Discount rate shown on the trial page matches the strategy.",
        "acceptance_criteria": [
            {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
        ],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test], language="python",
    ) == []


def test_body_verify_java_passes_value_equals_via_bare_assert_equals(tmp_path: Path):
    """Java analogue: JUnit `assertEquals(EXPECTED, actual)` satisfies
    `value_equals` — previously exact_attribute/value_equals had no bare
    fallback in Java either, only exact_count/exact_text did."""
    pom = tmp_path / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public int discountRate() {\n"
        "        return Integer.parseInt(page.locator(sel.DISCOUNT).textContent());\n"
        "    }\n"
        "}\n",
    )
    test = tmp_path / "src" / "test" / "java" / "QteaTrialTest.java"
    _write(test,
        "public class QteaTrialTest {\n"
        "    @Test\n"
        "    void discountMatchesStrategy() {\n"
        "        assertEquals(EXPECTED_DISCOUNT_RATE, trialPage.discountRate());\n"
        "    }\n"
        "}\n",
    )
    missing = [{
        "name": "discountRate", "signature": "()", "kind": "assertion",
        "purpose": "Discount rate shown on the trial page matches the strategy.",
        "acceptance_criteria": [
            {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
        ],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test], language="java",
    ) == []


def test_body_verify_java_passes_exact_text_via_assertj_fluent(tmp_path: Path):
    """Regression: the module comment claims AssertJ bare-asserts are
    covered, but the pre-fix regex only matched JUnit/TestNG's
    `assertEquals(...)` call shape — AssertJ's fluent
    `assertThat(x).isEqualTo(y)` was silently NOT matched. Confirms it now
    is."""
    pom = tmp_path / "pages" / "TrialPage.java"
    _write(pom,
        "public class TrialPage {\n"
        "    public String marketingConsentLabel() {\n"
        "        return page.locator(sel.MARKETING_CONSENT_LABEL).textContent();\n"
        "    }\n"
        "}\n",
    )
    test = tmp_path / "src" / "test" / "java" / "QteaTrialTest.java"
    _write(test,
        "public class QteaTrialTest {\n"
        "    @Test\n"
        "    void marketingConsentLabelMatchesStrategy() {\n"
        "        assertThat(trialPage.marketingConsentLabel())\n"
        "            .isEqualTo(EXPECTED_MARKETING_CONSENT_LABEL);\n"
        "    }\n"
        "}\n",
    )
    missing = [{
        "name": "marketingConsentLabel", "signature": "()", "kind": "assertion",
        "purpose": "Get the marketing consent label so tests can assert exact text.",
        "acceptance_criteria": [{
            "check": "exact_text",
            "locator": "MARKETING_CONSENT_LABEL",
            "expected_symbol": "EXPECTED_MARKETING_CONSENT_LABEL",
        }],
    }]
    assert verify_method_bodies(
        pom, "TrialPage", missing, test_files=[test], language="java",
    ) == []


# ---------------------------------------------------------------------------
# BodyViolation formatting
# ---------------------------------------------------------------------------


def test_body_violation_format_includes_file(tmp_path: Path):
    v = BodyViolation(
        method="foo", criterion_index=2, check="exact_text",
        message="missing text",
    )
    s = v.format(pom_file="src/pages/TrialPage.ts")
    assert "TrialPage.ts:" in s
    assert "foo(crit#2,exact_text)" in s
    assert "missing text" in s
