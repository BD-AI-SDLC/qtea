# UI Test Automation Specialist — Workflow Reference

Authoritative procedural reference for the `ui-test-automation.agent.md`, which holds the persona, mission, non-negotiable rules, scope, and high-level workflow. This file holds the detail: stack detection table, user-confirmation prompt, locator priority, AOM snapshot protocol, production CI/CD examples, accessibility playbook, parallel-execution configs, retry policy.

The orchestrator wires this file in as a referenced input (read on demand, not inlined) so its bulk does not burn tokens unless the agent actually needs a specific section.

---

## §1 — Stack Detection ("Polyglot Check")

Before generating code, scan the root directory to determine the language and framework:

| Manifest signal | Detected framework | Mode |
|---|---|---|
| `pyproject.toml` / `requirements.txt` contains `pytest-playwright` | Python + Playwright | Python + Playwright |
| `pyproject.toml` / `requirements.txt` contains `selenium` | Python + Selenium | Python + Selenium |
| `*.robot`, `*.resource`, `robot.yaml` present | Robot Framework | (see Browser/Selenium sub-check) |
| Above + `robotframework-browser` | Robot + Browser Library | RF + Browser (Playwright-based) |
| Above + `robotframework-seleniumlibrary` | Robot + SeleniumLibrary | RF + SeleniumLibrary |
| `pom.xml` / `build.gradle` with `testng` + `selenium-java` | Java + Selenium | Java + Selenium |
| `package.json` / `playwright.config.ts` with `playwright` | TS/JS + Playwright | TypeScript + Playwright |
| `package.json` with `cypress` | TS/JS + Cypress | Cypress |
| `*.csproj` with `Microsoft.Playwright` | C# + Playwright | C# + Playwright |

### §1B — User Confirmation (REQUIRED when no stack detected)

If NO existing codebase is found (no `package.json`, `pom.xml`, `requirements.txt`, `*.csproj`, `*.py`, `*.ts`, `*.js` files):

- **MUST ASK** the user the following question before generating ANY code.
- **DO NOT** proceed with any default — wait for user response.

### Language & Framework Selection

In case language and\or framework couldn't be detected, ask the user what is his stack to use for implementation from below choices:

**Preferred Language:**
- [ ] JavaScript
- [ ] TypeScript
- [ ] Python
- [ ] Java

**Preferred Framework:**
- [ ] Playwright (Modern, auto-waiting, cross-browser)
- [ ] Selenium (Legacy, widely used)
- [ ] Cypress (JavaScript-only)
- [ ] WebdriverIO (JavaScript/TypeScript)
- [ ] Robot Framework + Browser Library (Playwright-based)
- [ ] Robot Framework + SeleniumLibrary

**Design Pattern:**
- [ ] Page Object Model (POM)
- [ ] Screenplay Pattern
- [ ] Component Objects
- [ ] Keyword-Driven Testing (Robot Framework)


**Critical rules:**
- ALWAYS wait for user input when no stack is detected.
- NEVER assume / default to any language/framework without explicit user confirmation.
- If the user explicitly specifies (e.g., "Generate Python pytest tests"), skip detection and use that directly.
- Document the user's choice in the generated code comments at the top of each file.

---

## §2 — Framework Templates (per-stack starting points)

Use the catalog in `agents/polyglot-test-researcher.prompt.md` §2 as the canonical reference for per-framework idioms (imports, fixtures, test syntax). Add the following UI-specific additions per stack:

- **Playwright (TS/JS):** prefer `getByRole`, `getByLabel`, `getByTestId`. Auto-wait built in — no explicit waits needed except for custom conditions.
- **Playwright (Python):** synchronous mode by default (`pytest-playwright` fixture); use async only if the existing repo already uses it.
- **Cypress:** prefer `[data-testid]` selectors; chain via `cy.get(...).find(...)`. Never `cy.wait(<number>)`.
- **Selenium (any language):** wrap `find_element` calls in `WebDriverWait(...).until(...)` — never bare. Use `expected_conditions.element_to_be_clickable`.
- **Robot Framework + Browser:** locator prefixes `id=`, `css=`, `role=`, `text=`. Resource files in `Resources/`, test files in `Tests/`.
- **Robot Framework + Selenium:** locator prefixes `id:`, `css:`, `xpath:`. Same directory convention.

---

## §3 — Locator Strategy (Strict Priority)

When defining elements, use the **first available** option from this list:

1. **Test ID** — `data-testid`, `data-cy`, `data-qa` (if present)
2. **ID** — `id="submit-btn"` (if stable)
3. **Accessible Role** — `getByRole('button', { name: 'Submit' })`
4. **Label** — `getByLabel('Email Address')`
5. **Placeholder** — `getByPlaceholder('Email')` (if present)
6. **Text Content** — `getByText('Welcome')` (only if unique)
7. **CSS** — short, component-scoped chains only (e.g., `.card > .submit`)
8. **Shadow DOM** — Playwright/Cypress shadow-piercing selectors (e.g., `button >> text=Submit`)

**FORBIDDEN:** Absolute XPath (e.g., `/div/div[2]/span`) or brittle CSS chains (e.g., `div > div > button`).

---

## §4 — Snapshot Strategy (Token Optimization)

When analyzing a page state (for static analysis or post-execution healing):

- **Action:** Request `page.accessibility.snapshot()`.
- **Reasoning:** Returns condensed JSON Tree (AOM) showing only interactive elements and labels.
- **Prohibition:** **NEVER** request `page.content()` (raw HTML) unless explicitly asked for deep debugging. Wastes tokens, confuses the model.

---

## §5 — Quality Gates

### Assertion Best Practices

Always provide clear assertion messages.

- ❌ `expect(element).toBeVisible()`
- ✅ `expect(element, 'Submit button should be visible after form validation').toBeVisible()`

### Naming Standards

- Functional: `test_should_<action>_when_<condition>_then_<outcome>`
  - Example: `test_should_display_error_when_password_too_short()`
- Negative: `test_should_reject_<action>_when_<invalid_condition>`
  - Example: `test_should_reject_login_when_account_locked()`
- BDD: native `Given/When/Then` in test descriptions when using Cucumber/SpecFlow.
- Robot Framework: sentence-case, space-separated (no underscores).
  - Functional: `Should <Action> When <Condition>` → `Should Display Error When Password Is Too Short`
  - Negative: `Should Reject <Action> When <Condition>` → `Should Reject Login When Account Is Locked`
  - BDD: native `Given`/`When`/`Then` keyword prefix support.

### Retry Policy (CI only)

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

## §7 — Cross-Browser Testing Strategy

- **Tier 1 (every commit):** Chromium
- **Tier 2 (nightly):** Chrome, Firefox, Edge
- **Tier 3 (pre-release):** Safari, Mobile Safari

Handling browser-specific issues:
- Feature detection over browser detection.
- Polyfills for older browsers (if required).
- Separate test suites for browser-specific behaviors.
- Tag tests: `@chromium-only`, `@firefox-only`, `@webkit-only`.

---

## §8 — Example Scenarios

**Scenario A (Python):** User uploads `requirements.txt` with `pytest-playwright`.
→ Generate `conftest.py` with fixtures and `test_login.py` using synchronous or async Playwright based on existing code patterns.

**Scenario B (Java):** User uploads `pom.xml` with `selenium-java`.
→ Generate `LoginPage.java` using WebDriver and `@FindBy` annotations.

**Scenario C (No existing stack detected):** User provides only a requirement document with no codebase files.
→ **MUST WAIT** for user to explicitly specify language/framework preference before generating ANY code. Present the §1B prompt and wait.

**Scenario D (User explicitly specifies):** User states "Generate Python pytest tests".
→ Skip detection and generate Python/pytest code directly.

**Scenario E (Robot Framework):** User has `robotframework-browser` in `requirements.txt`.
→ Generate `Resources/keywords.resource` (reusable keywords) and `Tests/login_tests.robot` using Browser Library locators (`id=`, `css=`, `role=`, `text=`). Include `robot.yaml` if Robocorp/RCC is detected. CI artifact step uploads `output.xml`, `log.html`, `report.html`.
