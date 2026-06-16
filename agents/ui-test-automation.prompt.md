# UI Test Automation Specialist — Reference Data

On-demand lookup tables, code templates, locator strategy, production examples, and worked scenarios for the `ui-test-automation.agent.md`. The agent reads specific sections of this file via the Read tool when it needs framework-specific detail — it is NOT loaded into the system prompt.

Persona, mission, non-negotiable rules, and the high-level workflow (including the stack-resolution reading procedure) live in `ui-test-automation.agent.md`.

---

## §1 — Per-Language Idiom Table

Use the value of `language` from `./sut_inventory.json` at `modules[active_module]` to select naming and framework:

| `language` value | Test file naming | Common framework |
|---|---|---|
| `python` | `test_*.py` (worca prefix: `worca_*_test.py`) | pytest + pytest-playwright OR selenium |
| `typescript` | `*.spec.ts` (worca: `worca_*.spec.ts`) | Playwright OR Cypress OR WebdriverIO |
| `javascript` | `*.spec.js` (worca: `worca_*.spec.js`) | same as TS |
| `java` | `*Test.java` (worca: `Worca*Test.java`) | TestNG / JUnit + Selenium OR Playwright-Java |
| `csharp` | `*Tests.cs` (worca: `Worca*Tests.cs`) | NUnit / xUnit + Playwright OR Selenium |
| `robot` | `*.robot` (worca: `worca_*.robot`) | RF + Browser Library OR SeleniumLibrary |

Detailed per-framework patterns (imports, fixtures, test syntax): see `agents/polyglot-test-researcher.prompt.md` §2 — it's the canonical catalog, do not duplicate here.

---

## §2 — Owning-Page Heuristic (when to extend vs create a new POM)

A new feature very often lives **inside an existing page** of the SUT — not in a brand-new page that needs its own POM, locators, fixtures, and `open()` method. A common mistake is creating a parallel `<NewFeature>Page` class with its own locator constants, its own collapse/toggle/locale helpers, and its own `open(base_url)` — when the feature is actually one or two widgets on a page the SUT already models.

**The signal — selector prefix family.** Scan `sut_inventory.json` → `modules[active].existing_locators[].constants`. If the SUT's existing constants share a structural prefix (a `data-testid` namespace, an `id=` family, a Robot/CSS group) with the selectors you'd write for the new feature, the feature lives on the page that owns those locators.

Examples of "structural prefix" — all hypothetical, the rule applies whenever you see this shape regardless of the actual prefix or SUT:

- Existing constants all use `data-testid="Header-*"` and your new feature's selectors are `data-testid="Header-NewBadge"` → it lives on the page that owns `*HeaderLocators`.
- Existing constants all use `id="checkout-*"` and your new selector is `id="checkout-promo-code"` → extend the checkout-page POM.
- Existing constants share a parent container selector (`.app-shell .sidebar > *`) and your new widget renders inside the same container → extend the page that owns that container.

**The signal — overlapping helpers.** If the SUT's existing page object already has methods you'd otherwise need to re-implement (e.g. `toggle_side_bar`, `switch_locale`, `open_settings`, navigation helpers, fixture chains that authenticate and land on the page), the new feature lives there. **Never** redefine an existing helper under a new name — import and call it.

### When the signal fires, follow these three rules:

1. **Reuse locator constants — never byte-duplicate.** If the existing locator class is the canonical owner and editable, append your new constants there. If you can't (or shouldn't) modify the SUT's file, create a thin `Worca<NewFeature>Locators` that **imports / subclasses the existing class** and only adds the genuinely new constants:
   ```python
   # in src/<pkg>/pages/locators/worca_<new_feature>_locators.py (new)
   from .<existing>_locators import <Existing>Locators

   class Worca<NewFeature>Locators(<Existing>Locators):
       """New: only adds <NewFeature>-specific constants; reuses everything in <Existing>Locators."""
       def __init__(self):
           super().__init__()
           self.<NEW_CONSTANT> = "<selector you genuinely added>"
           # NO redeclaration of selectors that already exist in <Existing>Locators.
   ```
   Equivalent patterns in other languages: TS `extends`, Java inheritance, C# `: <Existing>Locators`, Robot `Resource <existing>.resource`.

2. **Reuse page methods — extend or compose, don't fork.** When you need behavior the existing page already provides (collapse/expand, locale switch, navigation, modal open/close), call its method. If you must add new behavior, either subclass or compose:
   ```python
   # Subclass — when the new feature is a strict superset of the existing page's surface
   class Worca<NewFeature>Page(<Existing>Page):
       """New: only adds <NewFeature>-specific actions; reuses every <Existing>Page method."""
       def <new_action>(self) -> None:
           self.click_on_element(self.locators.<NEW_CONSTANT>)
   ```
   ```python
   # Composition — when the new feature is a collaborator that operates on the existing page
   class Worca<NewFeature>Page:
       def __init__(self, existing: <Existing>Page) -> None:
           self.existing = existing
           self.locators = Worca<NewFeature>Locators()
       def <new_action>(self) -> None:
           self.existing.click_on_element(self.locators.<NEW_CONSTANT>)
   ```

3. **Reuse the existing fixture chain.** Don't write a new `open()` that calls `page.goto(base_url)` if the SUT already has an authenticated fixture (`chat_setup`, `dashboard_setup`, whatever the SUT calls it) that lands you on the page. Compose your fixture from the existing one:
   ```python
   @pytest.fixture()
   def <new_feature>_page(<existing>_page: <Existing>Page) -> Worca<NewFeature>Page:
       return Worca<NewFeature>Page(<existing>_page)
   ```

### When NOT to extend

Create a brand-new POM only when **all** of these are true:

- The new feature is on a distinct URL or DOM root from any existing page object.
- No existing locator class's constants share a prefix family with your selectors.
- No existing page object's methods overlap with the helpers you'd need (auth, navigation, common widget controls).

Document the call with a one-line docstring on the new class: `"""New: <Feature> is a standalone <modal|page|widget>, not part of <ExistingPage>."""`.

---

## §3 — Framework Templates (per-stack starting points)

Use the catalog in `agents/polyglot-test-researcher.prompt.md` §2 as the canonical reference for per-framework idioms (imports, fixtures, test syntax). Add the following UI-specific additions per stack:

- **Playwright (TS/JS):** prefer `getByRole`, `getByLabel`, `getByTestId`. Auto-wait built in — no explicit waits needed except for custom conditions.
- **Playwright (Python):** synchronous mode by default (`pytest-playwright` fixture); use async only if the existing repo already uses it.
- **Cypress:** prefer `[data-testid]` selectors; chain via `cy.get(...).find(...)`. Never `cy.wait(<number>)`.
- **Selenium (any language):** wrap `find_element` calls in `WebDriverWait(...).until(...)` — never bare. Use `expected_conditions.element_to_be_clickable`.
- **Robot Framework + Browser:** locator prefixes `id=`, `css=`, `role=`, `text=`. Resource files in `Resources/`, test files in `Tests/`.
- **Robot Framework + Selenium:** locator prefixes `id:`, `css:`, `xpath:`. Same directory convention.

---

## §4 — Locator Strategy (Strict Priority)

Authoritative order — must match `ui-test-automation.agent.md` rule 1, `docs/qa-orchestrator.instructions.md` §6, and `CLAUDE.md`. Keep these in sync; any change here must be mirrored in all three.

When defining elements, use the **first available** option from this list:

1. **ID** — `id="submit-btn"` (if stable)
2. **Test ID** — `data-testid`, `data-cy`, `data-qa` (if present)
3. **Accessible Role** — `getByRole('button', { name: 'Submit' })`
4. **Label** — `getByLabel('Email Address')`
5. **Text Content** — `getByText('Welcome')` (only if unique)
6. **Placeholder** — `getByPlaceholder('Email')` (if present)
7. **CSS** — short, component-scoped chains only (e.g., `.card > .submit`)
8. **Shadow DOM** — Playwright/Cypress shadow-piercing selectors (e.g., `button >> text=Submit`)

**FORBIDDEN:** Absolute XPath (e.g., `/div/div[2]/span`) or brittle CSS chains (e.g., `div > div > button`).

---

## §5 — Retry Policy (CI only)

| Failure type | Retries | Notes |
|---|---|---|
| Flaky network/timing | up to 2 | Exponential backoff (1s, 2s, 4s) |
| Assertion failures | 0 | Real bug — surface immediately |
| Infrastructure (browser crash) | 1 | |
| Slow CI timeout | 1 | Extend timeout and retry |

---

## §6 — Production Requirements

### CI/CD Integration

- Generate pipeline configs (GitHub Actions, Jenkins, GitLab CI).
- Parallel execution strategies (split by spec file).
- Test sharding for large suites.
- Failure threshold policies (fail-fast vs continue-on-error).
- Artifacts: JUnit XML, HTML reports, video recordings, test result webhooks.

**Example — GitHub Actions:**
```yaml
name: E2E Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2, 3, 4]
    steps:
      - uses: actions/checkout@v3
      - run: npm ci
      - run: npx playwright test --shard=${{ matrix.shard }}/4
      - uses: actions/upload-artifact@v3
        if: failure()
        with:
          name: test-results
          path: test-results/
```

### Test Data Management

- Data isolation: each test creates its own data (no shared state).
- Unique identifiers (UUID, timestamp).
- Database seeding via API/SQL before test execution.
- Cleanup hooks (`afterEach` / teardown).
- Never use production data in tests.
- Synthetic data generation (Faker.js, Bogus).
- PII redaction in screenshots/videos.
- GDPR compliance for test data storage.

**Example — Python (Playwright):**
```python
# conftest.py — pytest-playwright provides built-in fixtures:
#   playwright (session), browser_type (session), browser (session),
#   context (function), page (function).
# Only override when you need custom launch or context options.

@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"args": ["--disable-gpu"]}


@pytest.fixture
def browser_context_args():
    return {"viewport": {"width": 1280, "height": 720}}


# Tests use the built-in page fixture — isolated per test automatically.
def test_login(page: Page):
    page.goto("/login")
    page.get_by_label("Email").fill("user@example.com")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url("**/dashboard")
```

### Accessibility Testing (WCAG Compliance)

- Integrate axe-core for every page/state.
- Target: WCAG 2.1 Level AA (minimum).
- Fail tests on critical violations (missing alt text, no keyboard nav).
- Cover keyboard navigation, screen reader compatibility, color contrast, form label associations.

**Example — Playwright (TS):**
```typescript
import { AxeBuilder } from '@axe-core/playwright';

test('should have no accessibility violations', async ({ page }) => {
  await page.goto('/dashboard');
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2aa'])
    .analyze();
  expect(results.violations).toEqual([]);
});
```

### Parallel Execution & Sharding

- Worker count: CPU cores - 1 by default.
- Test isolation: each worker gets an independent browser context.
- No shared state between parallel tests.
- Sharding splits tests across CI machines.
- Dynamic load balancing (Playwright built-in).

**Example — Playwright config:**
```typescript
export default defineConfig({
  workers: process.env.CI ? 4 : undefined,
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'on-first-retry',
  },
  reporter: [
    ['html'],
    ['junit', { outputFile: 'results.xml' }],
    ['list']
  ]
});
```

### Failure Artifacts

- **Screenshot** — full page at failure
- **Video** — recording of entire test
- **Trace** — Playwright trace with DOM / network / console
- **Logs** — browser console errors/warnings
- **HAR file** — network activity
- **HTML snapshot** — final DOM state
- **Storage** — local (`./test-results/`) or CI (S3/GCS) with retention policy

---

## §7 — Example Scenarios

All scenarios assume the normal worca-t pipeline path: `./sut_inventory.json` is staged in your workdir by Step 7's orchestration code, and `modules[active_module]` holds the active module record.

**Scenario A (Python + Playwright):** `sut_inventory.json["modules"][active]` shows `language: "python"`, `package_manager: "poetry"`, and lists existing page objects under `pages/object/`.
→ Generate `worca_<feature>_test.py` placed in `sut_inventory.modules[active].test_directory_layout.default_target` (e.g., `tests/regression/`). Import existing `SignInPage` / `<Feature>Page` from the SUT instead of redefining them. Use `pytest-playwright` synchronous fixtures. **Before creating a new `*Page` / `*Locators` class, run the §2 Owning-Page check** — if the SUT's existing locator constants share a `data-testid` prefix family with the selectors you'd write, extend the owning page object instead of forking.

**Scenario B (Java + Selenium):** `sut_inventory.json["modules"][active]` shows `language: "java"`, `package_manager: "maven"`.
→ Generate `Worca<Feature>Test.java` under `src/test/java/`. Use TestNG `@Test` annotations and `@FindBy` page-object pattern. Reuse any existing `*Page.java` from `sut_inventory.existing_page_objects`.

**Scenario C (`active_module` null or `modules` empty — rare):** Step 6 hard-failed and operator pushed through anyway.
→ The fallback language/framework/pattern prompt is presented by the agent (see `ui-test-automation.agent.md` workflow). **WAIT** for explicit selection. Do NOT scan the SUT root yourself.

**Scenario D (User explicitly overrides):** User states "Generate Python pytest tests" in the task prompt even though `sut_inventory.json["modules"][active]` says TypeScript.
→ Trust the explicit override but document it: top-of-file comment `# Stack: python+pytest (user override; sut_inventory.json detected typescript)`. This is rare and intentional.

**Scenario E (Robot Framework):** `sut_inventory.json["modules"][active]` shows `language: "robot"`, `existing_page_objects` may be empty (Tier 3 LLM-augmented from researcher).
→ Generate `Tests/worca_<feature>.robot` and `Resources/<feature>_keywords.resource` using Browser Library locators (`id=`, `css=`, `role=`, `text=`). Reuse existing `*.resource` keywords listed in `sut_inventory.existing_helpers`.
