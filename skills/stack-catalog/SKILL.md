# Stack Catalog

Each entry: indicators (deps + config + imports) + minimal example showing structure. Read on demand when you need to identify or match a specific framework.

### Python

**Playwright (Python)** — deps: `playwright`, `pytest-playwright` · config: `pytest.ini`, `conftest.py` (page fixture) · imports: `from playwright.sync_api import Page, expect` · pattern: `test_*.py`
```python
from playwright.sync_api import Page, expect
def test_login(page: Page):
    page.goto("/login")
    page.get_by_label("Username").fill("admin")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_text("Welcome")).to_be_visible()
```

**Selenium (Python)** — deps: `selenium` · imports: `from selenium import webdriver`, `from selenium.webdriver.common.by import By`
```python
from selenium.webdriver.common.by import By
def test_search(driver):
    driver.get("https://example.com")
    driver.find_element(By.ID, "search-input").send_keys("automation")
    assert len(driver.find_elements(By.CLASS_NAME, "result-item")) > 0
```

**pytest** — deps: `pytest` · config: `pytest.ini` or `[tool.pytest.ini_options]` in `pyproject.toml` · `conftest.py`
```python
import pytest
@pytest.fixture
def client(): return APIClient(base_url="https://api.example.com")
@pytest.mark.parametrize("status_code", [400, 401, 404])
def test_errors(client, status_code, requests_mock): ...
```

**unittest** — stdlib · imports: `import unittest`, `class X(unittest.TestCase)`
```python
import unittest
class TestCalculator(unittest.TestCase):
    def setUp(self): self.calc = Calculator()
    def test_add(self): self.assertEqual(self.calc.add(2, 3), 5)
```

**Robot Framework** — files: `*.robot` · deps: `robotframework` · config: `robot.{yaml,toml}`
```robot
*** Settings ***
Library    SeleniumLibrary
*** Test Cases ***
Valid Login
    [Tags]    smoke
    Input Text    id:username    admin
    Click Button    id:login-button
    Wait Until Page Contains    Dashboard
```

**Behave (BDD)** — deps: `behave` · structure: `features/*.feature` + `features/steps/*.py` · config: `behave.ini`
```gherkin
Feature: Login
  Scenario: Valid login
    Given the login page is open
    When I enter username "admin"
    Then I should see the dashboard
```
```python
from behave import given, when, then
@given('the login page is open')
def step(context): context.browser.get(context.base_url + "/login")
```

### TypeScript / JavaScript

**Playwright (TS/JS)** — deps: `@playwright/test` · config: `playwright.config.{ts,js}` · pattern: `*.spec.{ts,js}` in `tests/` or `e2e/`
```typescript
import { test, expect } from '@playwright/test';
test('login redirects to dashboard', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Username').fill('admin');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).toHaveURL('/dashboard');
});
```

**Cypress** — deps: `cypress` · config: `cypress.config.{ts,js}` · dir: `cypress/` · pattern: `*.cy.{ts,js}`
```typescript
describe('Login', () => {
  it('valid credentials', () => {
    cy.visit('/login');
    cy.get('[data-testid="username"]').type('admin');
    cy.get('[data-testid="login-button"]').click();
    cy.url().should('include', '/dashboard');
  });
});
```

**Jest** — deps: `jest` · config: `jest.config.*` or `"jest"` in `package.json` · pattern: `*.test.{ts,js}`, `__tests__/`
```typescript
describe('ApiClient', () => {
  test('fetches user', async () => {
    const user = await client.getUser(1);
    expect(user).toHaveProperty('id', 1);
  });
});
```

**Mocha + Chai** — deps: `mocha`, `chai` · config: `.mocharc.*` · dir: `test/`
```typescript
import { expect } from 'chai';
describe('AuthService', () => {
  it('authenticates', async () => {
    const r = await auth.login('admin', 'secret');
    expect(r).to.have.property('token');
  });
});
```

**Jasmine** — deps: `jasmine` · config: `jasmine.json` or `spec/support/jasmine.json` · pattern: `*-spec.{ts,js}` in `spec/`
```typescript
describe('formatDate', () => {
  it('formats ISO', () => { expect(formatDate('2024-01-15')).toBe('January 15, 2024'); });
});
```

**Cucumber.js (BDD)** — deps: `@cucumber/cucumber` · files: `*.feature` + `features/steps/*.{ts,js}` · config: `cucumber.{js,yml}`
```typescript
import { Given, When, Then } from '@cucumber/cucumber';
Given('I am on the homepage', async function () { await this.page.goto('/'); });
When('I search for {string}', async function (q) { await this.page.getByRole('searchbox').fill(q); });
```

### Java

**JUnit 5 + Selenium** — deps: `junit-jupiter`, `selenium-java` (in `pom.xml`/`build.gradle`) · pattern: `*Test.java` in `src/test/java/`
```java
import org.junit.jupiter.api.*;
import org.openqa.selenium.By;
import static org.junit.jupiter.api.Assertions.*;
class LoginTest {
    @BeforeAll static void setUp() { driver = new ChromeDriver(); }
    @Test void testValidLogin() {
        driver.get("/login");
        driver.findElement(By.id("username")).sendKeys("admin");
        assertTrue(driver.getCurrentUrl().contains("/dashboard"));
    }
}
```

**Selenide** — deps: `com.codeborne:selenide` · imports: `import static com.codeborne.selenide.Selenide.*`, `import com.codeborne.selenide.Condition` · markers: `$(...)`, `$x(...)`, `$$(...)` selectors, `open(...)`, fluent `.shouldBe(visible)` / `.shouldHave(text(...))` · config: optional `Configuration.*` static or system properties
```java
import static com.codeborne.selenide.Selenide.*;
import static com.codeborne.selenide.Condition.*;
class LoginTest {
    @Test void testLogin() {
        open("/login");
        $("#username").setValue("admin");
        $("#login-button").click();
        $(".dashboard").shouldBe(visible).shouldHave(text("Welcome"));
    }
}
```

**TestNG + Selenium** — deps: `testng` · config: `testng.xml` · annotations: `@Test(groups=...)`
```java
import org.testng.annotations.*;
import static org.testng.Assert.*;
public class SearchTest extends BaseTest {
    @Test(groups = {"smoke"}, priority = 1)
    public void testSearch() { ... assertTrue(results.size() > 0); }
    @DataProvider(name = "terms")
    public Object[][] data() { return new Object[][] {{"laptop", 10}}; }
}
```

**Cucumber-JVM (BDD)** — deps: `cucumber-java` · files: `*.feature` in `src/test/resources/features/` + step defs in `src/test/java/`
```java
import io.cucumber.java.en.*;
public class LoginSteps {
    @Given("the user is on the login page")
    public void userOnLoginPage() { driver.get(baseUrl + "/login"); }
    @When("the user enters username {string} and password {string}")
    public void enter(String u, String p) { ... }
}
```

**Appium (Java)** — deps: `appium`, `io.appium:java-client` · class: `AppiumDriver` / `AndroidDriver` / `IOSDriver`
```java
import io.appium.java_client.android.AndroidDriver;
class LoginMobileTest {
    @BeforeEach void setUp() {
        DesiredCapabilities caps = new DesiredCapabilities();
        caps.setCapability("platformName", "Android");
        driver = new AndroidDriver<>(new URL("http://localhost:4723/wd/hub"), caps);
    }
    @Test void testLogin() { driver.findElementByAccessibilityId("login-button").click(); }
}
```

### Universal Fallback (any language outside the catalog)

When detected language has no dedicated catalog entry, **do not fail**. Apply this minimal recipe so downstream pipeline keeps a viable signal:

1. **Manifest match** — see the **Manifest → Ecosystem Lookup** appendix at the end of this file to identify ecosystem from manifest file. Record `language`, `package_manager`, `runtime_version`.
2. **Test directory heuristics** — look for any of: `test/`, `tests/`, `spec/`, `specs/`, `__tests__/`, `t/` (Perl-style), `features/`. Treat as test root.
3. **Filename heuristics** — flag files matching: `test_*`, `*_test.*`, `*Test.*`, `*Tests.*`, `*Spec.*`, `*Specs.*`, `*.test.*`, `*.spec.*`, `*-test.*`, `*-spec.*`, `*.feature`, `*.robot`.
4. **Build/test command discovery** — read `Makefile`, `justfile`, `Taskfile.yml`, README "Testing" / "How to run tests" section, CI pipeline files (`.github/workflows/*.yml`, `Jenkinsfile`, `.gitlab-ci.yml`).
5. **Record handoff** — emit `framework: null`, `confidence: low`, `language: <detected>`, `discoveryMethod: universal-fallback` plus any test files / commands found. Downstream agents (planner, implementer, fixer) treat this as "polyglot best-effort" mode and rely on file-pattern recognition rather than framework idioms.
6. **Do not invent patterns** — if no test files match heuristics, emit empty inventory + warning `"No tests detected; framework unknown — manual scaffolding required."` Do **not** synthesize fake examples.

---

## Appendix: Manifest → Ecosystem Lookup

### Manifest File → Ecosystem

| File | Ecosystem | Key fields to read |
|------|-----------|--------------------|
| `package.json` | Node.js / JavaScript / TypeScript | `dependencies`, `devDependencies`, `scripts`, `main`, `type`, `engines` |
| `go.mod` | Go | Module path, Go version, `require` block |
| `requirements.txt` | Python (pip) | Package list with pinned versions |
| `Pipfile` | Python (pipenv) | `[packages]`, `[dev-packages]`, `[requires]` python version |
| `pyproject.toml` | Python (poetry / uv / hatch) | `[tool.poetry.dependencies]`, `[project]`, `[build-system]` |
| `setup.py` / `setup.cfg` | Python (setuptools, legacy) | `install_requires`, `python_requires` |
| `Cargo.toml` | Rust | `[dependencies]`, `[[bin]]`, `[lib]` |
| `pom.xml` | Java / Kotlin (Maven) | `<dependencies>`, `<artifactId>`, `<groupId>`, `<java.version>` |
| `build.gradle` / `build.gradle.kts` | Java / Kotlin (Gradle) | `dependencies {}`, `sourceCompatibility` |
| `composer.json` | PHP | `require`, `require-dev` |
| `Gemfile` | Ruby | `gem` declarations, `ruby` version constraint |
| `mix.exs` | Elixir | `deps/0`, `elixir: "~> X.Y"` |
| `pubspec.yaml` | Dart / Flutter | `dependencies`, `dev_dependencies`, `environment.sdk` |
| `*.csproj` | .NET / C# | `<PackageReference>`, `<TargetFramework>` |
| `*.sln` | .NET solution | References multiple `.csproj` projects |
| `deno.json` / `deno.jsonc` | Deno (TypeScript runtime) | `imports`, `tasks` |
| `bun.lockb` | Bun (JavaScript runtime) | Binary lockfile — check `package.json` for deps |

### Language Runtime Version Detection

| Language | Where to find the version |
|----------|--------------------------|
| Node.js | `.nvmrc`, `.node-version`, `engines.node` in `package.json`, Docker `FROM node:X` |
| Python | `.python-version`, `pyproject.toml [requires-python]`, Docker `FROM python:X` |
| Go | First line of `go.mod` (`go 1.21`) |
| Java | `<java.version>` in `pom.xml`, `sourceCompatibility` in `build.gradle`, Docker `FROM eclipse-temurin:X` |
| Ruby | `.ruby-version`, `Gemfile` `ruby 'X.Y.Z'` |
| Rust | `rust-toolchain.toml`, `rust-toolchain` file |
| .NET | `<TargetFramework>` in `.csproj` (e.g., `net8.0`) |
