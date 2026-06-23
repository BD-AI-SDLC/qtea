---
name: diagnose-test-failure
description: 'Classify test failure tracebacks and route to the correct remediation action'
---

# Diagnose Test Failure

Classify a test failure from its traceback and determine whether the heal agent should attempt a fix. Read this skill BEFORE parsing the traceback. Apply the decision tree top-to-bottom; stop at the first match.

## Failure Classification Decision Tree

Work through steps A-E in order. First match wins.

### Step A: Locator Drift

The element the test targets has changed in the DOM (renamed, moved, removed). **Healable.**

Indicators:
- Error message references a specific selector string (CSS, testid, role, `[data-testid=...]`)
- Timeout is scoped to a single element find/action (`locator.click`, `waitForSelector`, `find_element`)
- Failure site is in a POM / page-object / locator file, not in the test body
- Error class is element-not-found or stale-element (see pattern tables)

### Step B: Timeout (Non-Locator)

Page load, navigation, or global timeout — NOT about finding a specific element. **Not healable.**

Indicators:
- Error references `page.goto`, `page.waitForURL`, `page.waitForLoadState`, `navigate`
- No selector string in the error message
- Timeout value is large (30s+, navigation-level)
- Error class is navigation timeout or network timeout

### Step C: Assertion Mismatch

Test found the element but the value doesn't match expectations. **Usually not healable** (business logic), but see nuance below.

Indicators:
- `AssertionError`, `expect(...).toBe(...)`, `assertEqual`, `assert actual == expected`
- Error shows both `actual` and `expected` values
- Failure site is in the test body, not POM

**Nuance: locator-drift-mediated assertion failures.** The orchestrator classifies some assertion failures as `assertion_value` and sends them to the heal agent when the mismatch pattern suggests upstream locator drift (e.g. `assert None == 'true'` where `get_attribute` returned `None` because the locator found a different element than intended). When you see `Failure class: assertion_value` in the user prompt, do NOT immediately abort. Instead:
1. Read the traceback's assertion line. Identify the locator and the attribute/value being checked.
2. If `get_attribute` / `text_content` / `inner_text` returned `None` or an obviously-wrong value, the locator likely resolved to the wrong element. This IS healable — proceed with live diagnosis to find the correct locator.
3. If the element was found correctly (locator is specific, e.g. `data-testid`) but the attribute genuinely has a different value, this is an app defect. Emit `OUT_OF_SCOPE: assertion-attribute-defect` and stop.

### Step D: Auth Failure

The test couldn't authenticate or was redirected to a login page. **Not healable.**

Indicators:
- HTTP 401 / 403 in error or response
- URL in traceback contains `/login`, `/signin`, `/auth`, `/sso`
- Credential-related error (`invalid_grant`, `AADSTS`, `unauthorized`)
- `AUTH_PATH_UNAVAILABLE` token present

### Step E: Navigation Error

The SUT is unreachable or returning server errors. **Not healable.**

Indicators:
- `ERR_CONNECTION_REFUSED`, `ERR_NAME_NOT_RESOLVED`, `ECONNREFUSED`
- HTTP 404 / 500 / 502 / 503 in response
- DNS resolution failure
- `net::ERR_` prefix in error

### Step F: Other

None of the above matched. **Not healable.** Abort and let the bug report flow handle it.

## Pattern Tables

### Python + Playwright (pytest)

| Pattern | Category |
|---|---|
| `TimeoutError.*waiting for locator` | locator_drift |
| `TimeoutError.*locator\.(click\|fill\|hover)` | locator_drift |
| `playwright._impl._errors.TimeoutError` + selector in msg | locator_drift |
| `page.goto.*Timeout` | timeout |
| `page.wait_for_url.*Timeout` | timeout |
| `AssertionError` | assertion_mismatch |
| `assert .* == .*` with two values shown | assertion_mismatch |
| `expect\(.*\)\.to_` with mismatch | assertion_mismatch |
| `401\|403\|Unauthorized` | auth_failure |
| `ERR_CONNECTION_REFUSED` | navigation_error |

### TypeScript / JavaScript + Playwright

| Pattern | Category |
|---|---|
| `locator\.(click\|fill\|hover): Timeout` | locator_drift |
| `waiting for locator\(` | locator_drift |
| `page\.goto.*Timeout exceeded` | timeout |
| `page\.waitForURL.*Timeout` | timeout |
| `expect\(received\)\.toBe\(expected\)` | assertion_mismatch |
| `expect\(.*\)\.toEqual\(` | assertion_mismatch |
| `net::ERR_` | navigation_error |

### Java (JUnit / TestNG + Selenium / Playwright)

| Pattern | Category |
|---|---|
| `NoSuchElementException` | locator_drift |
| `StaleElementReferenceException` | locator_drift |
| `ElementNotInteractableException` | locator_drift |
| `TimeoutException.*locator` | locator_drift |
| `TimeoutException.*navigate` | timeout |
| `WebDriverException.*ERR_CONNECTION` | navigation_error |
| `AssertionError\|AssertionFailedError` | assertion_mismatch |

### Robot Framework

| Pattern | Category |
|---|---|
| `Element .* not found` | locator_drift |
| `Element .* not visible` | locator_drift |
| `Timeout.*waiting for element` | locator_drift |
| `Navigation timed out` | timeout |
| `FAIL :: .* != .*` | assertion_mismatch |

## Healability Matrix

| Category | Healable? | Agent Action |
|---|---|---|
| `locator_drift` | Yes | Proceed with live diagnosis + POM patch |
| `timeout` | No | Abort — infrastructure issue |
| `assertion_mismatch` | Conditional | If `Failure class: assertion_value` is in the prompt, diagnose whether it's locator-drift-mediated (healable) or a genuine app defect (abort). Otherwise abort. |
| `auth_failure` | No | Abort — emit `AUTH_PATH_UNAVAILABLE` |
| `navigation_error` | No | Abort — SUT unreachable |
| `other` | No | Abort — unknown category |

## Locator Drift vs Non-Locator Timeout

These are the most commonly confused categories. Key disambiguators:

| Signal | Locator Drift | Non-Locator Timeout |
|---|---|---|
| Selector string in error | Yes (CSS, testid, role) | No |
| Error origin | POM method / locator action | `page.goto` / navigation |
| Timeout scope | Single element action | Whole page / URL |
| Typical timeout value | 5-30s (action-level) | 30-90s (navigation-level) |
| Fix available | Yes (update selector) | No (SUT/infra problem) |

## Usage

1. Read this skill when you receive a failing test traceback.
2. Walk through Step A → E in order. Stop at the first category that matches.
3. If the category is `locator_drift`, proceed with live diagnosis and POM patching per your agent rules.
4. If the category is anything else, abort the heal attempt immediately. The orchestrator handles downstream bug reporting.
