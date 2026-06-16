# Codegen Quality Rules (Shared Reference)

> **Single source of truth** for all Step 8 codegen sub-agents (`codegen-pom-extender`, `codegen-test-writer`, `ui-test-automation` violation fix). This file is injected as input context by `s08_codegen.py` — do not duplicate these rules in individual agent files.

---

## 1. Locator Priority (fixed, enforced)

`id` > `data-testid` > `role` > `label` > `text` > `placeholder` > scoped CSS. **Never XPath.** Full ranking in `ui-test-automation.prompt.md` §4.

## 2. AOM Snapshot Only in Generated Test Code

When generated test code inspects page state, it MUST use the accessibility tree (e.g. Playwright `page.accessibility.snapshot()` in Python, the equivalent in your target framework). **Never** raw-DOM dumps (`page.content()`, `driver.page_source`) inside tests — they waste tokens and ignore semantic structure.

## 3. TBD Locator Marker Convention

Every unresolved locator placeholder MUST carry the locator's semantic intent so the downstream resolver knows what DOM element to find. **Four emission styles**, chosen by the active module's stack — pick exactly one branch based on `sut_inventory.json["modules"][active_module].language` + framework:

**3a — Python + pytest + Playwright (JIT runtime path).** Import the worca-t runtime helper and use `tbd("intent")` in place of the bare `TBD_LOCATOR` literal. The Step 8 codegen step vendors a `tests/worca_t_runtime.py` plugin into the SUT; the helper produces a sentinel that the plugin intercepts at runtime via the live Playwright page. The plugin patches `Page.locator`, `Frame.locator`, AND `Locator.locator` on BOTH the sync (`playwright.sync_api`) AND async (`playwright.async_api`) API surfaces. **Mirror the SUT's existing Playwright API style** (sync vs async).

```python
# In the locators module:
from tests.worca_t_runtime import tbd

class LoginLocators:
    LOGIN_BUTTON = tbd("primary submit button on the login form")
    PASSWORD_INPUT = tbd("password input on the sign-in form")
```

The POM access pattern stays unchanged: `self.page.locator(self.locators.LOGIN_BUTTON).click()`. The runtime plugin transparently resolves the sentinel against the live page when the test executes.

**3b — TypeScript / JavaScript + Playwright (Playwright Test, Jest, Vitest) — JIT runtime path.** Import the vendored `tbd()` helper from `./worca-t-runtime`.

```typescript
import { tbd } from "./worca-t-runtime";

export const LoginLocators = {
  LOGIN_BUTTON: tbd("primary submit button on the login form"),
  PASSWORD_INPUT: tbd("password input on the sign-in form"),
};
```

**3c — Java + Playwright (JUnit5 OR TestNG) — JIT runtime path.** Import `com.worca.runtime.Tbd` and use `Tbd.of("intent")`.

```java
import com.worca.runtime.Tbd;

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

**Heuristic-friendly intent style (saves LLM cost).** When you can name the element by its visible role + label, do so — e.g. `tbd("sign in button")` over `tbd("primary submit on the login form")`. The runtime's in-process heuristic resolves the former without an LLM call by walking the accessibility tree for an exact role+name match. When semantic context is genuinely needed for disambiguation, the longer form is correct.

**TBD sentinel strings are NOT raw selectors.** A `tbd("…")` return value is an opaque sentinel (`__WORCA_T_TBD__::<intent>`). It only becomes a real selector when the framework's locator API resolves it — `page.locator(<sentinel>)` for Playwright, `driver.find_element(<sentinel>)` for Selenium, etc. NEVER pass a TBD sentinel into:
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
# WRONG — `self.locators.GEMINI_LINK` is `__WORCA_T_TBD__::gemini link`
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
- Prefix every generated filename with `worca_` to avoid collisions. **Test file naming:** `worca_<feature>_test.py` (start with `worca_`, then the feature, end with `_test.py`). Page objects and locators: `worca_<feature>_page.py` / `worca_<feature>_locators.py`. **Never** `worca_test_*.py`.

## 8. Worca-t Attribution Markers (pytest stacks only)

Every generated test function MUST carry a `@pytest.mark.worca_<phase>` decorator where `<phase>` is the test's planning phase (`smoke`, `regression`, `e2e`, or `exploratory` — read from the test strategy entry for that TC; default to `smoke` when absent). The markers are auto-registered by the vendored `tests/worca_t_runtime.py` plugin. Skip this rule on non-pytest stacks.

```python
import pytest

@pytest.mark.worca_smoke
def test_should_open_chat_when_landing_page_loads(chat_page):
    ...
```

---

## Assertion Fidelity (NON-NEGOTIABLE)

The single most common defect in machine-generated tests is **weak assertions**: tests that pass against any non-broken SUT instead of verifying a specific expected behavior. Eliminate them at write time.

For every test case, walk the strategy's `Steps:` and `Expected Result:` sections and apply these rules:

| When the strategy says... | You MUST emit... | You MUST NOT emit |
| --- | --- | --- |
| `Assert href equals "https://example.com/foo"` | `assert actual == "https://example.com/foo"` | `assert actual` (truthy); `assert "http" in actual` (substring) |
| `Label displays "Zu Gemini Enterprise wechseln"` | `assert actual == "Zu Gemini Enterprise wechseln"` | `assert actual`; `assert "Gemini" in actual` |
| `count equals 1` | `assert actual == 1` | `assert actual >= 1`; `assert actual` |
| `target equals "_blank"` | `assert actual == "_blank"` | `assert actual in ("_blank", "_self")` |
| `rel equals "noopener noreferrer"` | `assert actual == "noopener noreferrer"` | `assert "noopener" in actual` |
| `aria-label is "X, opens in new tab"` (full string given) | `assert actual == "X, opens in new tab"` | substring / truthy check |
| Localized parametrized values (en/de/...) | Parametrize with `@pytest.mark.parametrize` (or framework equivalent) and assert exact equality per locale | a single non-empty / substring check |

**Substring / truthy / range assertions are ONLY acceptable when the strategy explicitly uses non-exact language** (e.g. "label is non-empty", "count is at least 1", "contains the word Gemini").

When the strategy's expected value is a long literal (URL, multi-line string, JSON), declare it as a module-level constant at the top of the test file with a clear name (e.g. `GEMINI_ENTERPRISE_HREF = "https://..."`) and reference it in the assertion.

---

## Naming Standards

- Functional: `test_should_<action>_when_<condition>_then_<outcome>`
- Negative: `test_should_reject_<action>_when_<invalid_condition>`
- Robot Framework: sentence-case, space-separated
- BDD: native `Given`/`When`/`Then` keyword prefix where the framework supports it
