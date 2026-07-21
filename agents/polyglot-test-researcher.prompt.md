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

The full stack catalog (framework indicators, deps, config, imports, and code examples for all supported languages) is in `./stack-catalog/SKILL.md`. Read it when you need to identify or match a specific framework.

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

## §8 — Locator Source Inventory (`existing_locators`)

Populate `existing_locators` in the inventory block. **Never leave it `[]` when the SUT keeps locators in files, classes, or constants** — downstream codegen relies on this array to *extend/import existing constants* instead of inventing byte-identical duplicates or dangling references. A miss here caused a Step-8 hard-fail (run 20260709): the SUT's shared bag lived in a separately-imported file, the array shipped empty, and the code generator emitted references to constants that were never defined.

### What counts as a locator source

Record every one of these:

- **Object-literal bag** — `export const BASE_LOCATORS = {…}` / `const Selectors = {…}` (TS/JS), including UPPERCASE_SNAKE names. `location_pattern: export_const_object`.
- **Inline class-property map** — a `{…}` object assigned to a class property (e.g. `readonly elements = {…}`). `location_pattern: inline_object_property`.
- **Readonly Locator properties** — `readonly submitBtn = () => this.page.getByRole(...)`. `location_pattern: readonly_locator_props`.
- **Constant/locator class** — `class ChatPageLocators { … }` (Python-Selenium `class LoginLocators:`, Java constant class). `location_pattern: separate_class`.
- **Module of constants** — a `locators.py` / `selectors.ts` module of module-level `NAME = "…"` constants. `location_pattern: module_const_bag`.

### Critical: follow imports to separate files

A POM often keeps NO inline locators and instead imports a shared bag:

```ts
import { BASE_LOCATORS } from './locators/BasePage.locators';
// ... await this.page.locator(BASE_LOCATORS.btnLogin)
```

That imported file **is** a locator source — catalogue it even though the POM itself has `has_inline_locators: false`. Trace each POM's imports; any symbol used as `SYM.<key>` (or `self.locators.<KEY>`) resolves to a locator source you must record.

### Fields per entry

- `file` — SUT-relative path to the file that DEFINES the bag/class/constants.
- `class_name` — the bag/const/class identifier (e.g. `BASE_LOCATORS`, `ChatPageLocators`).
- `location_pattern` — one of the enum values above.
- `owning_pom` — the POM this source belongs to. Infer from a `<Pom>.locators.ts` filename, or from the POM that imports+uses it. A bag shared by many POMs: use the POM whose name it matches, else the base page.

---

## §9 — Auth Flow Detection

Populate `auth_flow` in the inventory block. **Do this from code, not from docs** — the auth method's implementation is the ground truth.

### Steps

1. **Locate the login helper.** Search for a method named `logIn`, `login`, `signIn`, `authenticate`, or similar in page-object base classes, global setup files (`global-setup.ts`, `conftest.py`), or test fixture files. Grep: `logIn|login|signIn|authenticate|performLogin`.

2. **Read its implementation.** Open the file and read the full method body. Look for every place it reads a credential from the environment:
   - **TS/JS:** `process.env.VARNAME` or `process.env['VARNAME']`
   - **Python:** `os.environ['VARNAME']`, `os.environ.get('VARNAME')`, `os.getenv('VARNAME')`
   - **Java:** `System.getenv("VARNAME")`

3. **Collect credential env var names.** From step 2, extract only the vars that hold username/email/account and password/token/secret values. Do NOT include infrastructure vars (TIMEOUT, BROWSER, BASE_URL, etc.).

4. **Set `credentials_env_vars`** to the list of those env var names — e.g. `["USERNAME_FOO", "PASSWORD_FOO"]`. Include ALL role-specific variants if the method accepts different users (e.g. `["USERNAME_OWNER", "PASSWORD_OWNER", "USERNAME_APPROVER", "PASSWORD_APPROVER"]`). The downstream credential resolver picks the first USER-pattern and first PASS-pattern var from this list.

5. **Set `entry_method`** to `<relative-file-path>:<ClassName.methodName>` for a class method (e.g. `src/pages/BasePage.ts:BasePage.logIn`) or `<relative-file-path>:<funcname>` for a module-level function. Use the path relative to the SUT root.

6. **Set `type`** to `basic` (username+password form), `sso` (redirect to external IdP), `oauth` (token exchange), or `none` / `unknown` when ambiguous.

7. **Set `open_method`** to the page-object method that NAVIGATES to the app base URL — the mandatory FIRST step of any UI test, *before* login. This is the method whose body does `page.goto('/')` / `page.goto(baseURL)` / `driver.get(...)` / `cy.visit(...)` / `browser.url(...)`, commonly named `openBaseURL`, `open`, `goto`, `navigate`, `visit`, or `load` on a BasePage. Use the same `<relative-file-path>:<ClassName.methodName>` shape as `entry_method`. Emit `null` when no such method exists. This is separate from `entry_method`: a POM that only logs in does NOT navigate.

### Rules

- If the login method reads `process.env.USERNAME` for the form field but the test data file has named constants (`USERNAME_OWNER = process.env.USERNAME_OWNER`), prefer the named constants — they are more specific.
- If multiple role-specific env vars exist (e.g. 3 user roles each with their own USERNAME/PASSWORD pair), list **all** of them in `credentials_env_vars`. The pipeline picks the first resolved pair.
- If no login helper exists (static credentials hardcoded, or auth is handled externally), emit `entry_method: null` and `credentials_env_vars: []`.
- `entry_method` must be a dotted `ClassName.methodName` for class methods or a bare `funcname` for module-level functions — the pipeline checks for a `.` to decide whether it can auto-invoke it.

---

## §9b — Lifecycle Hook Detection (setup / teardown)

Populate `lifecycle_hooks` in the inventory block. These are the SUT's per-test (and per-file) setup/teardown routines — the things that run before/after every test, distinct from data fixtures. Deterministic scanning already covers Python and TS/JS; **you MUST fill this in for stacks that deterministic tiers can't parse (Java, Kotlin, C#, Robot, Ruby).**

**Classify each hook by canonical trigger event** (framework-agnostic), then record its ordered body calls:

| `event` | JS/TS (Jest/Playwright/Mocha) | Pytest | unittest | JUnit 5 |
| --- | --- | --- | --- | --- |
| `before_all` | `beforeAll` / `before` | `@pytest.fixture(scope="module")` | `setUpClass` | `@BeforeAll` |
| `after_all` | `afterAll` / `after` | `@pytest.fixture(scope="module")` + `yield` | `tearDownClass` | `@AfterAll` |
| `before_each` | `beforeEach` | `@pytest.fixture(scope="function")` / `autouse=True` | `setUp` | `@BeforeEach` |
| `after_each` | `afterEach` | `@pytest.fixture` + `yield` | `tearDown` | `@AfterEach` |

For each hook emit `{event, framework_construct, file, scope, calls}` where `calls` is the ORDERED list of method-call expressions in the hook body (e.g. `["basePage.openBaseURL", "basePage.logIn", "basePage.goToEntityModule"]` for a before_each; `["basePage.logout"]` for an after_each). A yielding pytest fixture produces TWO entries (a before_* from pre-yield, an after_* from post-yield). These let Step 7 reuse the SUT's canonical pre-test sequence and Step 8 regenerate the hooks — critically, the open-base-URL → login sequence a UI test needs before it can do anything.

## §9c — Navigation-Precondition Detection

Populate `navigation_preconditions` in the inventory block. This is a DIFFERENT class of setup than §9b: some POM methods act on a specific already-active view (a data grid, a table filter, a tab-scoped panel, a search box) but have **no in-code guard** for that — they just assume the view is already open. Calling them from the wrong screen fails or times out, but nothing in the method's own source signals the requirement; it's a pure calling convention only visible by reading real call sites in the SUT's own test files.

**How to detect it:** for POM methods that read/manipulate a specific view (heuristics: name suggests search/filter/table/grid/tab scope, e.g. `selectFilteredEntity`, `searchInTable`, `selectRowByName`; or the method's own body references a locator that only exists on one screen), grep every real call site of that method across the test suite. If:
1. There are 2+ real call sites (or 1, if it's the only usage in the SUT), AND
2. Every call site is consistently preceded — within the same test function, not necessarily adjacent — by the same other method call (typically a menu/tile/tab navigation call), AND
3. That preceding call is NOT already fully captured by a `lifecycle_hooks` `before_each` entry for that same test file (if it's already in the hook, it's redundant to also flag here)

...then emit `{method, requires_call, requires_args_hint, evidence}` where `method`/`requires_call` are `ClassName.methodName`, `requires_args_hint` is the literal/constant argument observed at call sites (e.g. `"NAV_OPTIONS.DIRECTORY"`), and `evidence` is one real call site as `file:line`.

**Do not fabricate.** Skip entirely when call sites disagree (different preceding calls across different test files/scenarios — that's contextual, not a hard precondition), when you can't find any real call site to point at as `evidence`, or when you're inferring from the method's *name* alone without confirming against actual usage. A missed precondition degrades to a Step 9 test failure (recoverable); a fabricated one blocks a legitimate plan (worse). This is a small, high-precision list — most methods will have none.

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

# Echo these verbatim from the pre-computed artifacts (rule 8 in .agent.md).
# If the artifact is missing or the field is unset, write `null` — do not invent.
Package manager: {value-from-stack_profile.json}     # e.g. poetry, npm, maven
Wrapper prefix: {value-from-stack_profile.json}      # e.g. `poetry run`, ``
Install command: {value-from-stack_profile.json}     # e.g. `poetry install`
Canonical URL: {value-from-url_resolution.json}      # SUT base URL chosen by upstream resolver
Auth flow type: {value-from-sut_inventory.json}      # sso | oauth | basic | none | unknown
Default test target: {value-from-sut_inventory.json} # e.g. tests/regression/
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
