# QA Test Automation Researcher — Workflow Reference

Authoritative procedural reference for the `polyglot-test-researcher` agent. The companion `.agent.md` file holds the persona, mission, non-negotiable rules, and high-level procedure. This file holds the detail: glob patterns, stack catalog, signal regexes, fallback recipe, output template, and the exact discovery summary block.

The orchestrator wires this file in as a referenced input (read on demand, not inlined) so its bulk does not burn tokens unless the agent actually needs a specific section.

---

## §1 — Discover Project Structure

### Test file globs

| Language / Ecosystem | Globs |
|---|---|
| TS/JS | `**/*.{spec,test}.{ts,tsx,js,jsx,mjs,cjs}`, `**/*.cy.{ts,js}`, `**/*-spec.{ts,js}`, `**/__tests__/**` |
| Python | `**/test_*.py`, `**/*_test.py`, `**/tests/**`, `**/test/**` |
| Java/Kotlin | `**/*{Test,Tests,IT,Spec}.{java,kt}`, `**/src/test/{java,kotlin}/**`, `**/src/androidTest/**` |
| C#/.NET | `**/*{Test,Tests,Spec,Specs}.cs`, `**/test/**/*.cs`, `**/tests/**/*.cs` |
| Ruby | `**/spec/**/*_spec.rb`, `**/test/**/*_test.rb`, `**/features/**/*.rb` |
| Swift (XCUITest) | `**/*UITests/**/*.swift`, `**/*Tests/**/*.swift` |
| Dart/Flutter | `**/test/**/*_test.dart`, `**/integration_test/**/*.dart` |
| Go (API) | `**/*_test.go` |
| PHP | `**/tests/**/*Test.php`, `**/tests/**/*Cest.php` |
| Universal BDD | `**/*.feature` |
| Robot | `**/*.robot`, `**/*.resource` |

### Dependency files

`package.json`, `pyproject.toml`, `setup.cfg`, `setup.py`, `requirements*.txt`, `Pipfile`, `poetry.lock`, `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle{,.kts}`, `*.csproj`, `*.fsproj`, `*.sln`, `packages.config`, `paket.dependencies`, `Gemfile`, `*.gemspec`, `Package.swift`, `Podfile`, `pubspec.yaml`, `go.mod`, `go.sum`, `composer.json`

### Config files

`pytest.ini`, `conftest.py`, `tox.ini`, `playwright.config.*`, `cypress.config.*`, `wdio.conf.*`, `jest.config.*`, `vitest.config.*`, `tsconfig.json`, `behave.ini`, `robot.{yaml,toml}`, `testng.xml`, `cucumber.{yml,js,json}`, `.mocharc.*`, `jasmine.json`, `karma.conf.*`, `phpunit.xml{,.dist}`, `codeception.yml`, `behat.yml`, `.rspec`, `spec_helper.rb`, `Rakefile`, `cucumber.yml` (Ruby), `xunit.runner.json`, `nunit.config`, `*.runsettings`, `specflow.json`, `reqnroll.json`, `dart_test.yaml`, `flutter_test_config.dart`, `xcodebuild`/`*.xcconfig`, `*.xctestplan`

### CI/CD and infra

CI/CD: `.github/workflows/*.yml`, `Jenkinsfile`, `.gitlab-ci.yml`, `azure-pipelines.yml`, `bitbucket-pipelines.yml`

Env/Infra: `.env*`, `docker-compose*.yml`, `Dockerfile*`, `README*`, `CONTRIBUTING*`

---

## §2 — Stack Catalog

Each entry: indicators (deps + config + imports) + minimal example showing structure.

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

### Kotlin

**JUnit 5 (Kotlin)** — deps: `org.junit.jupiter:junit-jupiter`, `kotlin-test-junit5` · pattern: `*Test.kt` in `src/test/kotlin/`
```kotlin
import org.junit.jupiter.api.*
import org.junit.jupiter.api.Assertions.*
class CalculatorTest {
    @Test fun `adds two numbers`() { assertEquals(5, Calculator().add(2, 3)) }
}
```

**Kotest** — deps: `io.kotest:kotest-runner-junit5` · style: `StringSpec`, `FunSpec`, `BehaviorSpec` · matchers `shouldBe`, `shouldHave`
```kotlin
import io.kotest.core.spec.style.StringSpec
import io.kotest.matchers.shouldBe
class CalcSpec : StringSpec({ "add" { Calculator().add(2,3) shouldBe 5 } })
```

**Espresso (Android UI)** — deps: `androidx.test.espresso:espresso-core` · location: `src/androidTest/` · imports: `androidx.test.espresso.Espresso.onView`, `androidx.test.espresso.matcher.ViewMatchers.*`
```kotlin
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.matcher.ViewMatchers.*
@Test fun login() {
    onView(withId(R.id.username)).perform(typeText("admin"))
    onView(withId(R.id.login)).perform(click())
    onView(withId(R.id.welcome)).check(matches(isDisplayed()))
}
```

### C# / .NET

**xUnit + Selenium .NET** — deps `xunit`, `Selenium.WebDriver`, `Selenium.Support` (`*.csproj` `<PackageReference>`) · pattern: `*Tests.cs` · `[Fact]`, `[Theory]`/`[InlineData]`
```csharp
using Xunit;
using OpenQA.Selenium;
using OpenQA.Selenium.Chrome;
public class LoginTests : IDisposable {
    private readonly IWebDriver _driver = new ChromeDriver();
    [Fact] public void ValidLogin_RedirectsToDashboard() {
        _driver.Navigate().GoToUrl("/login");
        _driver.FindElement(By.Id("username")).SendKeys("admin");
        _driver.FindElement(By.Id("login")).Click();
        Assert.Contains("/dashboard", _driver.Url);
    }
    public void Dispose() => _driver.Quit();
}
```

**NUnit + Selenium .NET** — deps `NUnit`, `NUnit3TestAdapter`, `Selenium.WebDriver` · `[TestFixture]`, `[Test]`, `[SetUp]`, `[TearDown]`, `[TestCase(...)]`
```csharp
using NUnit.Framework;
[TestFixture] public class SearchTests {
    [SetUp] public void Init() => _driver = new ChromeDriver();
    [TestCase("laptop", 10)]
    public void Search_ReturnsResults(string term, int min) { ... Assert.GreaterOrEqual(results.Count, min); }
}
```

**MSTest** — deps `MSTest.TestFramework`, `MSTest.TestAdapter` · `[TestClass]`, `[TestMethod]`, `[DataRow]`
```csharp
using Microsoft.VisualStudio.TestTools.UnitTesting;
[TestClass] public class CalculatorTests {
    [TestMethod] public void Add_TwoNumbers() => Assert.AreEqual(5, new Calculator().Add(2,3));
}
```

**Playwright (.NET)** — deps `Microsoft.Playwright`, `Microsoft.Playwright.NUnit` (or `.MSTest`/`.Xunit`) · base `PageTest`
```csharp
using Microsoft.Playwright.NUnit;
public class LoginTests : PageTest {
    [Test] public async Task ValidLogin() {
        await Page.GotoAsync("/login");
        await Page.GetByLabel("Username").FillAsync("admin");
        await Page.GetByRole(AriaRole.Button, new() { Name = "Sign in" }).ClickAsync();
        await Expect(Page).ToHaveURLAsync(new Regex(".*/dashboard"));
    }
}
```

**SpecFlow / Reqnroll (BDD .NET)** — deps `SpecFlow` (legacy) or `Reqnroll` (successor) · files: `*.feature` + `[Binding]` step classes · config: `specflow.json` / `reqnroll.json`
```csharp
[Binding] public class LoginSteps {
    [Given(@"the user is on the login page")] public void GivenOnLoginPage() => _driver.Navigate().GoToUrl("/login");
    [When(@"the user enters ""(.*)""")] public void WhenEnters(string user) => _driver.FindElement(By.Id("username")).SendKeys(user);
    [Then(@"the dashboard is shown")] public void ThenDashboard() => Assert.Contains("/dashboard", _driver.Url);
}
```

### Ruby

**RSpec + Capybara** — deps (in `Gemfile`): `rspec`, `capybara`, `selenium-webdriver` · config: `.rspec`, `spec/spec_helper.rb`, `spec/rails_helper.rb` · pattern: `spec/**/*_spec.rb`
```ruby
require 'rails_helper'
RSpec.feature 'Login', type: :feature, js: true do
  scenario 'valid credentials' do
    visit '/login'
    fill_in 'Username', with: 'admin'
    click_button 'Sign in'
    expect(page).to have_current_path('/dashboard')
  end
end
```

**Minitest** — stdlib (Ruby) / gem `minitest` · pattern: `test/**/*_test.rb` · class `Minitest::Test`
```ruby
require 'minitest/autorun'
class CalculatorTest < Minitest::Test
  def test_add; assert_equal 5, Calculator.new.add(2, 3); end
end
```

**Cucumber (Ruby)** — deps `cucumber`, `cucumber-rails` · structure: `features/*.feature` + `features/step_definitions/*.rb` + `features/support/env.rb`
```ruby
Given('the user is on the login page') { visit '/login' }
When('the user logs in as {string}') { |user| fill_in 'Username', with: user; click_button 'Sign in' }
Then('the dashboard appears') { expect(page).to have_content('Welcome') }
```

### Swift (XCUITest — iOS)

**XCUITest** — bundled with Xcode (`XCTest` framework) · location: `*UITests/` target · imports: `import XCTest` · launch via `XCUIApplication`
```swift
import XCTest
final class LoginUITests: XCTestCase {
    func testValidLogin() {
        let app = XCUIApplication(); app.launch()
        app.textFields["username"].tap()
        app.textFields["username"].typeText("admin")
        app.buttons["Sign in"].tap()
        XCTAssertTrue(app.staticTexts["Welcome"].waitForExistence(timeout: 5))
    }
}
```
Run via `xcodebuild test -scheme <Scheme> -destination 'platform=iOS Simulator,name=iPhone 15'`. Test plans: `*.xctestplan`.

### Dart / Flutter

**flutter_test (widget tests)** — bundled with Flutter SDK (`flutter_test`) · location: `test/` · pattern: `*_test.dart` · `WidgetTester`
```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter/material.dart';
void main() {
  testWidgets('login button enabled when fields filled', (tester) async {
    await tester.pumpWidget(const MyApp());
    await tester.enterText(find.byKey(const Key('username')), 'admin');
    await tester.tap(find.byKey(const Key('login')));
    await tester.pumpAndSettle();
    expect(find.text('Welcome'), findsOneWidget);
  });
}
```

**integration_test (end-to-end Flutter)** — dep `integration_test` (Flutter SDK) · location: `integration_test/` · run via `flutter test integration_test/`
```dart
import 'package:integration_test/integration_test.dart';
import 'package:flutter_test/flutter_test.dart';
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('end-to-end login', (tester) async { /* ... */ });
}
```

**Patrol (Flutter native automation)** — dep `patrol` · adds native Android/iOS automation on top of `integration_test` · `patrolTest((tester) async { ... })`.

### Go (API testing)

**`testing` stdlib + `httptest`** — file pattern `*_test.go` · run `go test ./...` · table-driven tests are idiomatic
```go
package api_test
import (
    "net/http"
    "net/http/httptest"
    "testing"
)
func TestGetUser(t *testing.T) {
    srv := httptest.NewServer(NewHandler())
    defer srv.Close()
    cases := []struct{ id int; want int }{{1, 200}, {0, 404}}
    for _, c := range cases {
        r, _ := http.Get(srv.URL + "/users/" + fmt.Sprint(c.id))
        if r.StatusCode != c.want { t.Errorf("id=%d got %d want %d", c.id, r.StatusCode, c.want) }
    }
}
```

**testify + httpexpect** — deps (in `go.mod`): `github.com/stretchr/testify`, `github.com/gavv/httpexpect/v2` · `assert`/`require` matchers, fluent HTTP API
```go
import (
    "testing"
    "github.com/stretchr/testify/assert"
    "github.com/gavv/httpexpect/v2"
)
func TestUsersAPI(t *testing.T) {
    e := httpexpect.Default(t, "https://api.example.com")
    e.GET("/users/1").Expect().Status(200).JSON().Object().Value("id").Equal(1)
    assert.Equal(t, 1, 1)
}
```

**Ginkgo + Gomega (BDD)** — deps: `github.com/onsi/ginkgo/v2`, `github.com/onsi/gomega` · `Describe`/`Context`/`It` blocks
```go
var _ = Describe("Auth", func() {
    It("returns token for valid creds", func() {
        token, err := auth.Login("admin", "secret")
        Expect(err).NotTo(HaveOccurred())
        Expect(token).NotTo(BeEmpty())
    })
})
```

### PHP

**PHPUnit** — dep (in `composer.json` `require-dev`): `phpunit/phpunit` · config: `phpunit.xml{,.dist}` · pattern: `tests/**/*Test.php` · class extends `PHPUnit\Framework\TestCase`
```php
use PHPUnit\Framework\TestCase;
final class CalculatorTest extends TestCase {
    public function testAdd(): void { $this->assertSame(5, (new Calculator())->add(2, 3)); }
}
```

**Codeception** — dep `codeception/codeception` · config `codeception.yml` · suites: `Acceptance`, `Functional`, `Unit` · pattern: `tests/**/*Cest.php`
```php
class LoginCest {
    public function loginSuccessfully(AcceptanceTester $I) {
        $I->amOnPage('/login');
        $I->fillField('username', 'admin');
        $I->click('Sign in');
        $I->seeInCurrentUrl('/dashboard');
    }
}
```

**Behat (BDD PHP)** — dep `behat/behat`, often with `behat/mink` · config `behat.yml` · `*.feature` + `FeatureContext.php`
```php
/** @Given /^I am on the login page$/ */
public function iAmOnLoginPage() { $this->visit('/login'); }
/** @Then /^I should see "([^"]*)"$/ */
public function iShouldSee($text) { $this->assertSession()->pageTextContains($text); }
```

### Universal Fallback (any language outside the catalog)

When detected language has no dedicated catalog entry, **do not fail**. Apply this minimal recipe so downstream pipeline keeps a viable signal:

1. **Manifest match** — use `skills/acquire-codebase-knowledge/references/stack-detection.md` to identify ecosystem from manifest file. Record `language`, `package_manager`, `runtime_version`.
2. **Test directory heuristics** — look for any of: `test/`, `tests/`, `spec/`, `specs/`, `__tests__/`, `t/` (Perl-style), `features/`. Treat as test root.
3. **Filename heuristics** — flag files matching: `test_*`, `*_test.*`, `*Test.*`, `*Tests.*`, `*Spec.*`, `*Specs.*`, `*.test.*`, `*.spec.*`, `*-test.*`, `*-spec.*`, `*.feature`, `*.robot`.
4. **Build/test command discovery** — read `Makefile`, `justfile`, `Taskfile.yml`, README "Testing" / "How to run tests" section, CI pipeline files (`.github/workflows/*.yml`, `Jenkinsfile`, `.gitlab-ci.yml`).
5. **Record handoff** — emit `framework: null`, `confidence: low`, `language: <detected>`, `discoveryMethod: universal-fallback` plus any test files / commands found. Downstream agents (planner, implementer, fixer) treat this as "polyglot best-effort" mode and rely on file-pattern recognition rather than framework idioms.
6. **Do not invent patterns** — if no test files match heuristics, emit empty inventory + warning `"No tests detected; framework unknown — manual scaffolding required."` Do **not** synthesize fake examples.

---

## §6 — Architecture Pattern Detection

Quick architecture detect via path globs:

| Pattern | Signal globs | Signal deps/imports |
|---------|-------------|---------------------|
| POM (Page Object Model) | `**/{pages,page_objects,page-objects,components,helpers,utils,fixtures,locators}/**` | base page class extending common parent |
| Screenplay | `**/{actors,tasks,questions,abilities,interactions}/**` | Java: `net.serenitybdd.screenplay` · TS/JS: `@serenity-js/core`, `@serenity-js/web` · Python: `screenpy` |
| Inline / flat | none of the above | locators inlined in test bodies |

Screenplay extra import signals (`Grep`):
- Java: `import net.serenitybdd.screenplay.{Actor,Task,Question,Ability}`, `actor.attemptsTo(...)`, `actor.asksFor(...)`
- TS/JS: `import { Actor, Task } from '@serenity-js/core'`, `actorCalled(...).attemptsTo(...)`
- Python: `from screenpy import Actor, Task`, `Actor.named(...).who_can(...)`

Mixed = signals from multiple patterns coexist (common in legacy → Screenplay migration).

Then deep-analyze:
- **POM:** page classes, locator files (JSON/YAML/constants), base page class, action methods
- **Screenplay:** actors, tasks (composable user goals), questions (state queries), abilities (interaction capabilities — BrowseTheWeb, CallAnApi)
- **Locator strategies (both):** `data-testid`, CSS, XPath, accessibility roles, text-based

---

## §7 — Pattern Signals for Downstream Consumers

Regex scan. Output flags for downstream consumers. **Discovery only — do not flag findings.**

- **Security:** keywords `password|apiKey|API_KEY|token|secret|credential|auth|jwt|login|session|bearer|oauth` · code `process\.env\.|os\.environ|System\.getenv|eval\(|exec\(` · output `securityPatterns: { detected, files, keywords }`
- **UI / a11y:** framework markers (Playwright/Cypress/Selenium/WebdriverIO/Puppeteer/Appium) · interactions `page\.click|page\.fill|\.type\(|driver\.findElement|cy\.get\(` · locator strategies · output `uiPatterns: { detected, framework, locatorStrategies }`
- **Anti-patterns/flakiness:** hard sleeps `time\.sleep|Thread\.sleep|cy\.wait\([0-9]|page\.wait_for_timeout` · bare navigation (goto/get/navigate without expect/assert) · skipped `@pytest\.mark\.skip|test\.skip|xit\(|xdescribe\(|@Disabled|@Ignore` · output `qualitySignals: { hardSleeps, bareNavigation, skippedTests }`

---

## §10 — Discovery Summary Block (Stdout)

Print exactly this block on stdout before writing the research document — it's the machine-readable handoff to the orchestrator.

```
**Discovery Summary**
Workspace: {path}
Languages: [python, typescript, java]
Frameworks: [{name, version, source, signals: {dep, import, config}, confidence: high|medium|low}, ...]
Test runner(s): [pytest, jest, ...]
BDD: [behave, cucumber-js, none]
Test files: {count} (truncated: yes/no, cap=1000)
Architecture: {POM | Screenplay | inline | mixed | none}
Architecture dirs: [paths]  # pages/, actors/, tasks/, etc.
Locator strategies: [data-testid, css, xpath, role]
Security patterns: {yes/no}
UI patterns: {yes/no}
Quality signals: hardSleeps={n}, bareNavigation={n}, skipped={n}
CI/CD: [github-actions, jenkins, ...]
Reporting: [allure, mochawesome, none]
```

Abort conditions:
- No test files → `"No test files found. Check patterns and exclusions."`
- No framework → continue + warn: `"No automation framework detected — downstream uses universal patterns."`

---

## §11 — Research Document Sections

Write `.testagent/research.md`. Required sections (each populated from discovery):

- **Project Overview** — path, language(s), framework(s), test runner(s), BDD framework, reporting
- **Build & Test Commands** — install, run all/smoke/e2e/specific, generate report, lint
- **Project Structure** — paths to: pages, locators, tests, fixtures, config, reports, utilities
- **Test Inventory — By Type** — table: type | count | location | notes (smoke/regression/e2e/integration/unit)
- **Test Inventory — By Feature/Module** — table: feature | test files | test count | coverage assessment
- **Architecture & Locators** — pattern (POM / Screenplay / inline / mixed), locator strategy, base classes, structure table — POM: page-object (name | file | locators | methods); Screenplay: actors / tasks / questions / abilities (name | file | type | composed-of)
- **Existing Test Patterns** — fixtures/hooks, data-driven, assertions, wait strategies, screenshot/video on failure, tagging conventions
- **Environment & Infrastructure** — base URL config, auth/SSO, browsers, parallel, CI/CD, Docker
- **Quality Assessment** — strengths · issues found (flaky, anti-patterns, missing handling, outdated locators, dead/skipped) · coverage gaps table (gap | priority | recommendation)
- **Recommendations** — priority order for new tests, framework/pattern improvements, locator improvements, flaky fixes, test data improvements, blockers
