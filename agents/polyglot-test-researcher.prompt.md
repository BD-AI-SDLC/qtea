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
