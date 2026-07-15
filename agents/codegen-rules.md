# Codegen Quality Rules (Shared Reference)

> **Single source of truth** for all Step 8 codegen sub-agents (`codegen-pom-extender`, `codegen-test-writer`, `codegen-violation-fixer` violation fix). This file is injected as input context by `s08_codegen.py` — do not duplicate these rules in individual agent files.

---

## 1. Locator Priority (fixed, enforced)

`id` > `data-testid` > `role` > `label` > `text` > `placeholder` > scoped CSS. **Never XPath.** Full ranking in `codegen-violation-fixer.prompt.md` §4.

### 1.a — Playwright locator API (TS/JS stacks)

When the SUT uses Playwright TS/JS, express locators via the built-in API rather than raw selector strings:

| Predicate | Use |
|---|---|
| `[@data-test="X"]` or `[@data-testid="X"]` | `page.getByTestId('X')` — requires `testIdAttribute: '<attr>'` in `playwright.config.ts` when the SUT's attribute is not the Playwright default `data-testid`. Phase B.5.5 adds this line automatically when it emits a `getByTestId` call. |
| `<h1..h6>` with text | `page.getByRole('heading', { name: 'X' })` |
| `<a>` / `<button>` with text | `page.getByRole('link' \| 'button', { name: 'X' })` |
| Exact text `text()="X"` | `page.getByText('X', { exact: true })` |
| Fuzzy text `contains(., "X")` | `page.getByText('X')` |
| Other attribute `[@attr="X"]` | `page.locator('[attr="X"]')` — plain CSS |
| Nested `//A//B` | `page.getByRole('...').locator('B')` (chain) |

**POM container shape** — when a POM stores locators, express them as arrow-function factories returning `Locator`:

```ts
export class LoginPage {
  page: Page;
  constructor(p: Page) { this.page = p; }

  // was: '//input[@data-test="username"]'   ← breadcrumb from Phase B.5.5
  readonly inpUsername = () => this.page.getByTestId('username');
  readonly btnSubmit = () => this.page.getByRole('button', { name: 'Submit' });
}
```

Consumers call the factory directly: `await this.inpUsername().fill(user)` (never `this.page.locator(...)` on a locator string). Phase B.5.5 rewrites legacy `elements: Record<string, string>` containers into this shape and updates matching call sites automatically.

## 2. Snapshot Strategy in Generated Test Code

When generated test code inspects page state, choose the snapshot method based on DOM structure:

| DOM structure | Snapshot method | Rationale |
|---|---|---|
| Regular DOM / Shadow DOM | AOM — `aria_snapshot()` | AOM naturally pierces shadow roots and captures semantic structure without token waste. |
| Iframes | Full DOM — `page.content()` / `frame.content()` | Iframe content lives in a separate document; AOM snapshots may miss elements or fail to traverse cross-origin frames reliably. |

**Default (no iframes):** use the accessibility tree (e.g. Playwright `Locator.aria_snapshot()` in Python, the equivalent in your target framework). Shadow-root elements are fully visible to AOM — no special handling needed.

**Iframe exception:** when the test interacts with elements inside an `<iframe>`, use full DOM snapshots (`page.content()`, `frame.content()`, `driver.page_source`) scoped to the target frame. This is the ONE case where raw-DOM inspection is permitted in generated tests.

**Do not emit `boxes=True` or `mode="ai"` literals in generated test code.** Those kwargs (Playwright 1.60+ and 1.59+ respectively) are runtime-only affordances used by the JIT resolver. Generated tests target the 1.40+ floor and must call `aria_snapshot()` plain so they work on any supported Playwright version. The runtime's capability ladder handles richer kwargs internally and gracefully degrades.

## 3. TBD Locator Marker Convention

Every **new** locator constant added by codegen MUST use `tbd("intent")` (or the stack-specific equivalent below). The codegen agent MUST NEVER hardcode a selector string — not from the AOM snapshot, not from the strategy, not from any other source. The JIT runtime resolves intents at test execution time against the live page. Hardcoding selectors bypasses the SUT's POM API compatibility layer (e.g. `wait_for_selector` vs `page.locator`) and the locator-priority gate.

**Exception — dev-locator match:** When the pipeline pre-populates a locator constant with a dev-supplied selector (from `--dev-locators`), that value is authoritative and must not be overwritten with `tbd()`. The pipeline handles this substitution in Phase A2 — the agent never needs to read or reference the dev-locators file.

**Four emission styles**, chosen by the active module's stack — pick exactly one branch based on `sut_inventory.json["modules"][active_module].language` + framework:

**3a — Python + pytest + Playwright (JIT runtime path).** Import the qtea runtime helper and use `tbd("intent")` in place of the bare `TBD_LOCATOR` literal. The Step 8 codegen step vendors a `tests/qtea_runtime.py` plugin into the SUT; the helper produces a sentinel that the plugin intercepts at runtime via the live Playwright page. The plugin patches `Page.locator`, `Frame.locator`, AND `Locator.locator` on BOTH the sync (`playwright.sync_api`) AND async (`playwright.async_api`) API surfaces. **Mirror the SUT's existing Playwright API style** (sync vs async).

```python
# In the locators module:
from tests.qtea_runtime import tbd

class LoginLocators:
    LOGIN_BUTTON = tbd("primary submit button on the login form")
    PASSWORD_INPUT = tbd("password input on the sign-in form")
```

The POM access pattern stays unchanged: `self.page.locator(self.locators.LOGIN_BUTTON).click()`. The runtime plugin transparently resolves the sentinel against the live page when the test executes.

**3b — TypeScript / JavaScript + Playwright (Playwright Test, Jest, Vitest) — JIT runtime path.** Import the vendored `tbd()` helper from `./qtea-runtime`.

```typescript
import { tbd } from "./qtea-runtime";

export const LoginLocators = {
  LOGIN_BUTTON: tbd("primary submit button on the login form"),
  PASSWORD_INPUT: tbd("password input on the sign-in form"),
};
```

**3c — Java + Playwright (JUnit5 OR TestNG) — JIT runtime path.** Import `com.qtea.runtime.Tbd` and use `Tbd.of("intent")`.

```java
import com.qtea.runtime.Tbd;

public final class LoginLocators {
    public static final String LOGIN_BUTTON   = Tbd.of("primary submit button on the login form");
    public static final String PASSWORD_INPUT = Tbd.of("password input on the sign-in form");
}
```

**Java-specific constraint:** declare `Page` and `Locator` via interface types, never concrete impl classes (the dynamic-proxy mechanism only works through interfaces).

**3d — All other stacks (Selenium / Robot / Cypress / WebdriverIO / C# / etc.).** Emit the literal `TBD_LOCATOR` placeholder paired with an adjacent `TBD_INTENT: <one-line description>` comment on the line immediately above the marker. Polyglot comment styles:

- Python (non-pytest+Playwright) / Ruby / shell / Robot: `# TBD_INTENT: <text>`
- JS / TS / Java / C#: `// TBD_INTENT: <text>`

```java
// TBD_INTENT: email input field on the sign-in page
public static final String EMAIL_INPUT = "TBD_LOCATOR";
```

**Heuristic-friendly intent style (saves LLM cost).** When you can name the element by its visible role + label, do so — e.g. `tbd("sign in button")` over `tbd("primary submit on the login form")`. The runtime's in-process heuristic resolves the former without an LLM call by walking the accessibility tree for an exact role+name match. When semantic context is genuinely needed for disambiguation, the longer form is correct. **Spatial qualifiers help when two same-role+name elements coexist** (e.g. `tbd("top sign in button")`, `tbd("header search button")`) — on Playwright 1.60+, the runtime captures bounding boxes and breaks ties by visual position, and the LLM resolver tier is prompted to honor those spatial hints.

**TBD sentinel strings are NOT raw selectors.** A `tbd("…")` return value is an opaque sentinel (`__QTEA_TBD__::<intent>`). It only becomes a real selector when the framework's locator API resolves it — `page.locator(<sentinel>)` for Playwright, `driver.find_element(<sentinel>)` for Selenium, etc. NEVER pass a TBD sentinel into:

- `page.evaluate("(selector) => document.querySelector(selector)", self.locators.X)`
- `browser.executeScript("return document.querySelector(arguments[0])", self.locators.X)`
- `cy.window().then(win => win.document.querySelector(self.locators.X))`
- any other raw-DOM helper that bypasses the framework's locator layer.

Doing so always returns `null` / `''` because `document.querySelector` sees the literal sentinel string. For computed-style or other DOM-API queries, resolve the locator first and pass the ELEMENT HANDLE:

```python
# Correct — resolve via page.locator, then hand the element_handle to evaluate
handle = self.get_locator(self.locators.GEMINI_LINK).element_handle()
return self.page.evaluate(
    "(el, prop) => window.getComputedStyle(el).getPropertyValue(prop)",
    handle, property_name,
)
```

```python
# WRONG — `self.locators.GEMINI_LINK` is `__QTEA_TBD__::gemini link`
return self.page.evaluate(
    "([sel, prop]) => window.getComputedStyle(document.querySelector(sel)).getPropertyValue(prop)",
    [self.locators.GEMINI_LINK, property_name],
)
```

**Locator class hygiene (instance vs class attributes).** When extending a locators class whose existing constants live inside `__init__` as `self.X = "..."` (the common pattern when the class also defines a `reset()` method that calls `self.__init__()`), append new TBD constants INSIDE `__init__` too. Bare class attributes added after `def reset(self):` will NOT be restored by `reset()` and will desync from the instance state. Read the locators file before extending — match the existing pattern (instance attributes vs class attributes).

## 4. No Hard Waits — Use Polling Instead

Reject `time.sleep`, `Thread.sleep`, `cy.wait(<number>)`, `page.wait_for_timeout`, `setTimeout(_, N)` in tests. **No exceptions.** When you'd reach for `wait_for_timeout`, use one of these polling primitives instead:

- **Wait for a visible element:** `expect(locator).to_be_visible(timeout=N)` / `await expect(locator).toBeVisible({ timeout: N })` / `Assertions.assertThat(locator).isVisible(...)`
- **Wait for a value/count/attribute:** `expect.poll(getter_callable, timeout=N).to_have_length(...)` / `.to_equal(...)` / `.to_match(...)`
- **Wait for a JS condition:** `page.wait_for_function("...js expr...", timeout=N)`
- **Wait for a network response:** `page.expect_response(url_or_predicate, timeout=N)` as a context manager around the click that triggers it

If you genuinely cannot express the wait condition as a poll (vanishingly rare), open an `[ASSUMPTION]` comment and the human reviewer will decide — do NOT silently insert a hard wait.

## 5. No Secrets in Code

Credentials and API keys come from environment variables only (`process.env`, `os.environ`, `System.getenv`, `os.getenv`).

## 6. F.I.R.S.T. Tests

Fast, Independent, Repeatable, Self-validating, Timely. Each test creates and tears down its own data; no shared state.

## 7. Reuse Is the Default

The active module's inventory record is provided to you as context — inlined into your prompt for the phased codegen reasoning calls (POM extension, test writing), or staged as `./sut_inventory.json` in the workdir for the quality-gate (violation-fix) agent. It lives at `modules[active_module]` and lists the SUT's existing page objects, helpers, fixtures, **locator constants**, and auth flow. Before writing any class/helper/fixture/locator:

- If an existing entry covers the behavior you need, **import and extend it** — never redefine.
- **Locators specifically:** before defining any constant, scan `existing_locators` in `sut_inventory.json`. A SUT constant whose selector string matches yours byte-for-byte is **always** a reuse violation — import the existing constant. If none matches but your new feature's selectors share a `data-testid` prefix family with an existing locator class, the new feature likely lives on that page and your POM should extend it, not fork.
- If you must write new code (no existing equivalent), add a one-line docstring justification on the new class/method.
- Match the active module's `language` — never emit Python tests for a TypeScript module or vice versa.
- **Mirror the SUT's existing src/tests split** — never put production code under `tests/`. Use the placement contract:
  - **Test files** go under `./tests/<subdir>/` where `<subdir>` matches the active module's `test_directory_layout.subdirs` (prefer `default_target`).
  - **Page objects, locators, and helpers** go under the active module's `src_directory_layout.{pages_object_dir, pages_locators_dir, helpers_dir}`.
  - **Test data and fixtures** stay under `./tests/` since they are test-only assets.
- Prefix every generated filename with `qtea_` to avoid collisions. **Test file naming:** `qtea_<feature>_test.py` (start with `qtea_`, then the feature, end with `_test.py`). Page objects and locators: `qtea_<feature>_page.py` / `qtea_<feature>_locators.py`. **Never** `qteaest_*.py`.
- **NEVER modify existing SUT test files.** qtea always writes TDD tests from scratch into new `qtea_*_test.py` files. Existing test files (e.g. `test_chat_page.py`, `test_login.py`) belong to the SUT team and must not be touched. The "reuse" principle applies to page objects, locators, helpers, and fixtures — not to test files. If you find yourself about to add a `def test_*` function to an existing file, stop: create a new `qtea_<feature>_test.py` instead.

### Prefer POM/BasePage helper methods over raw `page.*` / `driver.*` calls

When the active module's POM already wraps a common Playwright/Selenium/framework action in a helper method, **always call that helper instead of the raw framework API**. The POM layer encapsulates retry logic, wait strategies, timeout defaults, and error handling that a direct `page.goto()` or `driver.find_element()` bypasses.

**Common examples (inspect the SUT's `BasePage` / base POM before writing):**

| Raw framework call | Prefer POM helper (if it exists) |
| --- | --- |
| `page.goto(url); page.wait_for_load_state("networkidle")` | `chat_page.open_url(url)` |
| `page.locator(selector).click()` | `chat_page.click_element(selector)` |
| `page.locator(selector).fill(text)` | `chat_page.enter_text(selector, text)` |
| `driver.find_element(By.ID, "x").click()` | `login_page.click(locator_constant)` |

**How to check:** Before writing any `page.goto`, `page.locator(...).click()`, `page.locator(...).fill()`, `driver.find_element`, or other low-level navigation/interaction call in a test, grep the active module's base page object (often `base_page.py`, `BasePage.java`, `base-page.ts`) for methods like `open_url`, `click_element`, `enter_text`, `navigate_to`, `hover_on_element`, `is_element_visible`. If a matching helper exists, use it — the test inherits the POM's hardened wait/retry behavior and stays maintainable when the SUT team refactors the base layer.

**When raw calls are acceptable:**

- The POM genuinely lacks a helper for this action AND the strategy does not prescribe extending the POM (write the raw call in the test, note the gap in an `[ASSUMPTION]` comment).
- The action is POM-internal (inside a new POM method you're writing) and you're implementing the primitive that the test will call — raw framework API is correct here.
- The test is exercising framework edge-case behavior (e.g. testing that a `page.goto` with `wait_until="commit"` differs from `"networkidle"`) where the POM abstraction would hide what's under test.

## 8. Pre-existing Locator Preservation

When reusing a locator constant from the SUT inventory, preserve its selector value **exactly as-is** — even if it uses XPath, bare CSS, or any strategy below the priority ladder (§1). Rewriting a pre-existing locator risks breaking the SUT team's own tests and invalidates their established selectors.

**Allowed:** Adding a comment next to the constant definition recommending migration:
```typescript
// RECOMMENDATION: consider migrating to getByRole('button', {name: 'Actions'}) for resilience
readonly btnAction = '[data-testid="action-menu-button"]//button[contains(@class,"primary")]';
```

**Forbidden:**
- Changing the value of the locator constant
- Replacing the selector strategy (e.g. XPath → CSS, CSS → role)
- Wrapping the value in a different Playwright/Selenium API call
- Removing the locator and replacing it with a new one

The §1 locator priority (`id > data-testid > role > … > scoped CSS; Never XPath`) applies only to **new locators created by qtea** (emitted as `tbd("intent")` sentinels). Pre-existing SUT locators are pass-through.

## 9. DOM Attribute Diagnostics for POM Methods (Python + pytest + Playwright)

Any POM method whose sole purpose is to **read a DOM attribute** (i.e. it calls `.get_attribute("attr_name")` and returns the result) MUST capture the element's opening HTML tag as an Allure attachment immediately before the `return`. This makes the raw attribute value visible in the Allure report without the noise of the full page source.

**Pattern — always split into a named locator variable first:**

```python
import contextlib  # top of file, alongside other imports
import allure      # top of file, alongside other imports

def get_gemini_button_rel_attribute(self) -> str | None:
    loc = self.get_locator(self.locators.GEMINI_ENTERPRISE_LINK)
    with contextlib.suppress(Exception):
        allure.attach(
            loc.evaluate("el => el.outerHTML.split('>')[0] + '>'"),
            name="element-html",
            attachment_type=allure.attachment_type.TEXT,
        )
    return loc.get_attribute("rel")
```

Rules (Python + pytest + Playwright):

- Extract the locator into a local variable `loc` before both the attachment and the `return` — never inline `self.get_locator(...)` twice.
- Wrap the attachment in `with contextlib.suppress(Exception):` so a missing `allure` package or an element-not-found error never breaks the test. **Never** use `try: ... except Exception: pass` — the codegen quality gate flags empty exception handlers as the `empty-handler` violation and fails Step 8. The `contextlib.suppress` form is shorter, expresses intent better, and is what every linter expects.
- The signature returns `str | None` because `Locator.get_attribute(...)` returns `str | None` — declaring `-> str` triggers the Phase B.6 type-checker (see "Respect Playwright optional-return signatures" in `codegen-pom-extender.agent.md`).
- The JavaScript expression `el.outerHTML.split('>')[0] + '>'` returns only the **opening tag** (e.g. `<a rel="noopener noreferrer" href="...">`) — one line, all attributes, no inner content.
- Skip this rule for methods that: return `inner_text()`, `text_content()`, `is_visible()`, `count()`, or any non-attribute value — it only applies to `.get_attribute()` calls.
- Skip on Selenium, Cypress, WebdriverIO, and Robot stacks — those lack a synchronous `evaluate()` bridge to the element handle.

**TypeScript / JavaScript + Playwright** — use the same diagnostic pattern via `allure-playwright`:

```typescript
import * as allure from "allure-playwright";  // or: import { allure } from "allure-playwright";

async getGeminiButtonRelAttribute(): Promise<string | null> {
    const loc = this.page.locator(this.locators.GEMINI_ENTERPRISE_LINK);
    await loc.evaluate(el => el.outerHTML.split(">")[0] + ">")
        .then(tag => allure.attachment("element-html", tag, { contentType: "text/plain" }))
        .catch(() => undefined);  // suppress — missing allure or element-not-found must not fail the test
    return loc.getAttribute("rel");
}
```

TS/JS rules:

- Use `.catch(() => undefined)` (not an empty `catch` block) to silence attachment errors without masking real failures.
- Return type is `Promise<string | null>` — `Locator.getAttribute()` returns `string | null` in TS; never widen to `Promise<string>`.
- `allure-playwright` is a peer dep of the SUT's test setup; if the SUT does not have it installed, omit the attachment rather than adding the dependency.

## 9. Qtea-t Attribution Markers (pytest stacks only)

Every test function in a qtea-generated test file MUST carry a `@pytest.mark.qtea_<phase>` decorator. The phase comes from the test design entry for that TC (`smoke`, `regression`, `e2e`, or `exploratory`); default to `smoke` when absent. The markers are auto-registered by the vendored `tests/qtea_runtime.py` plugin — without them the qtea runner's marker filter will collect zero tests. Skip this rule on non-pytest stacks.

```python
import pytest

@pytest.mark.qtea_smoke
def test_should_open_chat_when_landing_page_loads(chat_page):
    ...
```

**Opt-out marker `@pytest.mark.qtea_setup`** — apply ONLY to tests that legitimately perform setup-only work (state-mutation under a fixture, smoke probes whose verification lives inside a fixture, etc.). The Step 8 `zero-assertions` gate skips functions carrying this marker, but every use is audited; the rule is "almost never needed — if you're tempted, add an assertion instead."

## 10. Network Egress Restrictions

Generated tests, fixtures, and POMs MUST NOT issue HTTP(S) requests, websocket connections, or DNS lookups to any host other than the SUT origin (`base_url` from `research.json`) and explicitly approved test-data services declared in the strategy. No `requests.get("https://…")`, no `fetch("https://attacker…")`, no telemetry pings, no `page.goto(<URL not derived from base_url>)`. The Step 9 runtime monkey-patches `BrowserType.launch` for proxy injection — assume the proxy logs all egress. A prompt-injected codegen call could otherwise emit a test that quietly POSTs captured AOM (internal company UI text) to an external host; this rule is the docs-side counterpart to the selector allowlist.

## 11. Subprocess & Shell Hygiene in Generated Code

Generated tests, fixtures, and POMs MUST NOT use `subprocess.run(..., shell=True)`, `subprocess.Popen(..., shell=True)`, `os.system(...)`, `os.popen(...)`, `eval(...)`, `exec(...)`, `pickle.loads`, Node backtick command substitution, `child_process.exec(<string>)`, or any string-concatenated command construction. When a test genuinely needs to spawn a process, use list-form argv (`subprocess.run(["cmd", arg1, arg2], shell=False, check=True)` / `child_process.execFile("cmd", [args])`) and never interpolate test-derived data into the command. Shell injection in test code is just as exploitable as in production — tests often run under broader credentials (CI tokens, deploy keys) than the SUT itself.

**Subprocess env scrubbing.** When a generated test spawns a child process, pass `env=` explicitly with only the variables the child genuinely needs — never inherit the parent process env wholesale (the default when `env=` is omitted). The parent env contains `QTEA_RESOLVER_TOKEN`, `QTEA_RESOLVER_PORT`, JIRA tokens, `ANTHROPIC_API_KEY` (in the parent qtea process, not the SUT), and any CI deploy keys; leaking these into a third-party binary's env (or its log output, or its own subprocess fan-out) is a credential exfiltration path. Pattern: `subprocess.run(["tool", arg], env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}, shell=False, check=True)` — start from an empty dict and copy in only what's required.

## 12. Filesystem Scope for Generated Code

Generated tests, fixtures, and POMs may read files only from inside `<sut>/` and write files only into the SUT's test-artifact dirs (e.g. `<sut>/tests/_artifacts/`, `<sut>/test-results/`) or paths derived from `tmp_path` / `tmpdir` fixtures. Never read `~/.ssh/`, `~/.aws/`, `~/.config/`, `~/.netrc`, `/etc/`, `.env` files, the user's git config, browser profile dirs, sibling repos, or any absolute path outside `<sut>/`. Never write outside the two roots above. Forbidden patterns: `open("/tmp/...")`, `Path.home() / ".secret"`, `os.path.expanduser("~/...")` for read, `os.environ` dumps to disk, `shutil.copy(<credential-file>, …)`. A test that quietly reads `~/.aws/credentials` and posts it via a form field is a one-line exfiltration — closing this surface is non-negotiable.

**Never delete or rename pre-existing SUT files.** Codegen only CREATES new `qtea_*_test.py` / `qtea_*_page.py` / `qtea_*_locators.py` files and EXTENDS existing classes via import. You may not `os.remove`, `Path.unlink`, `shutil.rmtree`, `shutil.move`, or `Path.rename` any file the SUT team authored — tests, POMs, fixtures, configs, lockfiles, `conftest.py`, `.gitignore`, CI workflow files. If you believe an existing file is broken, raise a bug-candidate via the strategy's blocker channel; do not "clean it up". Step 9's git working-tree diff detects removals and reverts the change.

---

## Assertions Belong in Test Methods, Not POMs (NON-NEGOTIABLE)

Assertions (`expect(...)`, bare `assert`, `assertThat(...)`, `Assertions.assert*(...)`, Cypress `.should(...)`) may live ONLY in test methods, never in page-object support files (`**/pages/**`, `**/pageobjects/**`, `**/page_objects/**`, `**/pom/**`). Page objects encapsulate ACTIONS and PROBES — they return raw values that tests then assert against. **This applies to `kind: "assertion"` POM methods too:** a `kind: "assertion"` method is a *probe*, not a self-grading verdict. It NEVER calls `expect()`/`assert`, NEVER computes a `true`/`false` "did it pass" and `return`s it, and NEVER ends in `return true`. It returns the RAW thing the test's matcher operates on; the test holds the `expect(...)`. This is enforced by the `pom-assertion` rule in `test_indexer.py` (and its Phase A3.5 sibling `codegen_pom_hygiene.find_pom_assertion_violations`):

- **error** severity when the enclosing method is one that qtea codegen added this run (present in the plan's `missing_methods`). Hard-fails Step 8.
- **warning** severity when the enclosing method is pre-existing SUT code — logged for migration triage but does not block the build.

**What the probe returns (pick by the criterion's `check`):**

| Criterion `check` | POM probe returns | Test asserts with (auto-retry) |
|---|---|---|
| `exact_text` | the **`Locator`** | `expect(loc).toHaveText(EXPECTED)` |
| `exact_count` | the **`Locator`** | `expect(loc).toHaveCount(n)` |
| `exact_attribute` | the **`Locator`** | `expect(loc).toHaveAttribute(k, v)` |
| `value_equals` | the **`Locator`** | `expect(loc).toHaveValue(v)` |
| `visible` / `focusable` | the **`Locator`** | `expect(loc).toBeVisible()` / `.toBeFocused()` |
| `url_matches` | — (assert on `page`) | `expect(page).toHaveURL(u)` |
| `boundingbox_below` / `_above` | the **`Locator`** — **one probe per element**; the test extracts `.boundingBox()` and compares | `expect(aBox.y).toBeGreaterThan(bBox.y)` |

**Always return a `Locator`; never a resolved string/count/geometry.** This is the single most important codegen rule for POM methods and it is **language- and execution-model-agnostic** — it holds identically for TypeScript, Python (sync AND async), and Java. `getMarketingConsentLabel(): Locator { return this.page.locator(SEL.MARKETING_CONSENT_LABEL); }` beats `getMarketingConsentLabelText(): Promise<string|null>`. Returning the Locator and passing it straight into `expect(...)` keeps Playwright's auto-retry (the matcher polls the DOM), works uniformly across all stacks, keeps the probe reusable for any matcher, and is what the Step-8 body-verifier's oracle patterns look for. Even a positional check returns Locators — the *test* calls `.boundingBox()` on them and compares the geometry. Reserve a resolved-value return (a plain `number`/`string`) only for values that can never be a Playwright object — a parsed JSON field, a computed length over a collected list — where a bare `assert`/`expect(value)` in the test is correct (see §"Assertion Fidelity").

**Correct shape (Locator probe + test-side matcher):**

```typescript
// POM — pure getter, no expect(), no boolean verdict:
getMarketingConsentLabel(): Locator {
  return this.page.locator(TrialPageSelectors.CHECKBOX_MARKETING_CONSENT_LABEL);
}
getMandatoryCheckboxErrors(): Locator {
  return this.page.locator(TrialPageSelectors.TrialPageWarnings);
}

// TEST — every assertion lives here, referencing the strategy's expected constant:
test("marketing consent label matches strategy copy", async ({ page }) => {
  const trialPage = new TrialPage(page);
  await expect(trialPage.getMarketingConsentLabel()).toHaveText(EXPECTED_MARKETING_CONSENT_LABEL);
  await expect(trialPage.getMandatoryCheckboxErrors()).toHaveCount(3);
});
```

**Correct shape (positional check — one Locator probe per element, geometry + comparison in the test):**

```typescript
// POM — two Locator getters, no expect(), no geometry math:
getMarketingConsentCheckbox(): Locator {
  return this.page.locator(TrialPageSelectors.CHECKBOX_MARKETING_CONSENT);
}
getLegalProtectionCheckbox(): Locator {
  return this.page.locator(TrialPageSelectors.CHECKBOX_LEGAL_PROTECTION);
}

// TEST — extract geometry and make the one final assertion.
// A missing element yields null at boundingBox(); the `!`/comparison surfaces it — no mini-guard needed.
const marketingBox = await trialPage.getMarketingConsentCheckbox().boundingBox();
const legalBox = await trialPage.getLegalProtectionCheckbox().boundingBox();
expect(marketingBox!.y).toBeGreaterThan(legalBox!.y); // "below" ⇒ larger y
```

**Separate concerns.** Several small single-purpose probes that a test composes are better than one `verify*` method that reads several elements, compares them, and self-grades. Each probe does ONE DOM read and returns ONE value; the test orchestrates the reads and makes the assertions. This is more readable, gives real failure diagnostics (a failed `toHaveCount(3)` says "got 2", a failed `.toBe(true)` says nothing), and keeps the POM reusable.

**Why this is enforced:** when a SUT POM already contains `expect()` calls (pre-existing drift), or when a plan names a method `verify*`, the pom-extender is tempted to write the assertion *inside* the POM (or compute a boolean verdict and `return true`) — propagating the anti-pattern into qtea-authored code and producing dead `expect(await pom.verify()).toBe(true)` checks in the test. The agent's instructions (this file and its persona) are the authority for new methods, not the existing code in the file being extended, and not the `verify` in a method name. The `pom-assertion` rule enforces this mechanically: Phase A3.5 scans every agent-authored method body immediately after the extender writes it and hard-fails Step 8 on any assertion-shaped call. The later `index_tests` pass (Phase C) flags pre-existing calls as warnings for migration triage without blocking the build.

## One Coherent Final Assertion, Not One Per Mini-Verification (NON-NEGOTIABLE)

A strategy's `Expected Result:` block enumerates verification facts in a fixed order: mini verifications (checkable facts along the way) first, ending with the terminal/main verification last (test-designer.agent.md's convention). **Do not translate every bullet into its own `expect()`/`assert`.** The plan (`code-modification-plan.json`) already decided which bullets are assertable — only the terminal/main verification's `kind: "assertion"` missing_method(s) become real assertions (test-automation-architect.agent.md steps 3/3a/3b). This file's job is to transpile that decision faithfully, not re-derive it from prose.

- **Terminal verification → the assertion(s).** For each `kind: "assertion"` probe the plan lists, emit its `expect(<probe>).<matcher>(EXPECTED)` in the test, appended after the choreographed actions, using the probe's `acceptance_criteria` as the oracle for the matcher + expected value. The `expect()` lives in the test, never in the probe (see §"Assertions Belong in Test Methods, Not POMs"). A positional check's two single-value probes are called, the numbers compared, and ONE final `expect(...)` made — all in the test.
- **Mid-flow mini verifications → nothing, or a wait, never an assertion.** If the plan has no method for a mini-verification bullet, Step 7 judged it implied by the next action's own auto-wait — emit nothing. If the plan lists a `kind: "action"` sync method (name pattern `waitFor<Condition>`), call it in choreography order, and its body must use a polling wait primitive — `locator.wait_for(state="visible"|"attached"|"hidden")` (Python) / `.waitFor({state: ...})` (TS) — never `expect()`/`assert`. This is the same poll-don't-sleep discipline as §4 "No Hard Waits", applied to POM-body synchronization instead of test-body timeouts.

**Bad** (one assertion per Expected-Result bullet — the anti-pattern this rule forbids):

```python
def test_should_save_entry_when_form_is_valid(page, entry_page, entry_list_page):
    ...
    expect(entry_page.locators.SUCCESS_TOAST).to_be_visible()               # mini verification
    expect(entry_page.locators.SAVE_BUTTON).to_be_disabled()                # mini verification
    expect(entry_list_page.locators.NEW_ENTRY_STATUS).to_have_text("Draft") # terminal verification
```

**Good** (the mid-flow fact that actually gates the next step becomes a wait, not a check; the fact that gates nothing is dropped; one coherent final assertion against a probe's return remains):

```python
def test_should_save_entry_when_form_is_valid(page, entry_page, entry_list_page):
    ...
    entry_page.wait_for_success_toast_visible()   # kind: action — polling wait, not an assertion
    entry_list_page.click_view_entries()
    # kind: assertion probe returns the Locator; the one terminal check lives here:
    expect(entry_list_page.new_entry_status_row("My Entry")).to_have_text("Draft")
```

This caps HOW MANY / WHICH bullets become assertions — it does not lower the floor. §9's zero-assertions gate (every test needs ≥1 assertion unless `qtea_setup`) still applies.

## Assertion Fidelity (NON-NEGOTIABLE)

The single most common defect in machine-generated tests is **weak assertions**: tests that pass against any non-broken SUT instead of verifying a specific expected behavior. Eliminate them at write time.

### Prefer Playwright `expect()` over bare `assert` (Python + Playwright stacks)

Playwright's `expect()` API provides **auto-retry with configurable timeout** — the assertion polls the DOM until the condition is met or the timeout expires. Bare `assert` evaluates once and fails immediately on transient states (element not yet rendered, text still loading, navigation in progress). **Always prefer `expect()` when the assertion target is a Playwright object** (page, locator, API response).

| Assertion target | Use | Do NOT use |
| --- | --- | --- |
| Locator text content | `expect(loc).to_have_text("Expected")` | `assert loc.text_content() == "Expected"` |
| Locator attribute | `expect(loc).to_have_attribute("href", expected)` | `assert loc.get_attribute("href") == expected` |
| Locator visibility | `expect(loc).to_be_visible()` | `assert loc.is_visible()` |
| Locator focus state | `expect(loc).to_be_focused()` (Python) / `await expect(loc).toBeFocused()` (TS) | `assert loc.evaluate("el => el === document.activeElement")` |
| Locator count (present) | `expect(loc).to_have_count(3)` | `assert loc.count() == 3` |
| Locator count (absent) | `expect(loc).to_have_count(0)` — e.g. "no validation errors shown" | `assert loc.count() == 0` |
| Locator CSS class | `expect(loc).to_have_class(re.compile(r"active"))` | `assert "active" in loc.get_attribute("class")` |
| Locator value (input) | `expect(loc).to_have_value("hello")` | `assert loc.input_value() == "hello"` |
| Page URL | `expect(page).to_have_url("https://example.com")` | `assert page.url == "https://example.com"` |
| Page title | `expect(page).to_have_title("Dashboard")` | `assert page.title() == "Dashboard"` |

**Fall back to bare `assert`** only when the value under test is NOT a Playwright object — e.g. a computed Python value, a parsed JSON field, a length comparison on a collected list, or a return value from a POM helper method that returns a plain Python type:

```python
# OK — plain Python value, no Playwright expect() available
items = cart_page.get_item_names()
assert len(items) == 3
assert items[0] == "Widget A"
```

### URL destination assertions: click-then-assert, not href-check

When the strategy says a link "navigates to" / "leads to" / "points to" a URL, **prefer clicking the element and asserting the resulting page URL** over reading the `href` attribute. Enterprise apps commonly use redirect/gateway URLs in `href` that differ from the final destination — an `href` assertion fails even though the user lands on the correct page.

```python
# PREFERRED — tests the actual user experience
loc.click()
expect(page).to_have_url(EXPECTED_DESTINATION_URL)

# ACCEPTABLE — only when the strategy explicitly says "href equals ..."
expect(loc).to_have_attribute("href", EXPECTED_HREF)
```

When the link opens in a new tab (`target="_blank"`), use `expect_popup` to capture the new page:

```python
with page.expect_popup() as popup_info:
    loc.click()
new_page = popup_info.value
expect(new_page).to_have_url(EXPECTED_DESTINATION_URL)
```

### Exact-value rules

For every test case, walk the strategy's `Steps:` and `Expected Result:` sections and apply these rules:

| When the strategy says... | You MUST emit... | You MUST NOT emit |
| --- | --- | --- |
| `Link navigates to "https://example.com/foo"` | `loc.click()` + `expect(page).to_have_url("https://example.com/foo")` | `assert loc.get_attribute("href") == ...` (href may be a redirect) |
| `Assert href equals "https://example.com/foo"` | `expect(loc).to_have_attribute("href", "https://example.com/foo")` | `assert actual` (truthy); `assert "http" in actual` (substring) |
| `Label displays "Zu Gemini Enterprise wechseln"` | `expect(loc).to_have_text("Zu Gemini Enterprise wechseln")` | `assert actual`; `assert "Gemini" in actual` |
| `count equals 1` | `expect(loc).to_have_count(1)` | `assert actual >= 1`; `assert actual` |
| `target equals "_blank"` | `expect(loc).to_have_attribute("target", "_blank")` | `assert actual in ("_blank", "_self")` |
| `rel equals "noopener noreferrer"` | `expect(loc).to_have_attribute("rel", "noopener noreferrer")` | `assert "noopener" in actual` |
| `aria-label is "X, opens in new tab"` (full string given) | `expect(loc).to_have_attribute("aria-label", "X, opens in new tab")` | substring / truthy check |
| Localized parametrized values (en/de/...) | Parametrize with `@pytest.mark.parametrize` (or framework equivalent) and assert exact equality per locale | a single non-empty / substring check |

**Substring / truthy / range assertions are ONLY acceptable when the strategy explicitly uses non-exact language** (e.g. "label is non-empty", "count is at least 1", "contains the word Gemini").

When the strategy's expected value is a long literal (URL, multi-line string, JSON), declare it as a module-level constant at the top of the test file with a clear name (e.g. `GEMINI_ENTERPRISE_HREF = "https://..."`) and reference it in the assertion.

---

## Naming Standards

- Functional: `test_should_<action>_when_<condition>_then_<outcome>`
- Negative: `test_should_reject_<action>_when_<invalid_condition>`
- Robot Framework: sentence-case, space-separated
- BDD: native `Given`/`When`/`Then` keyword prefix where the framework supports it
