---
name: webapp-testing
description: Polyglot toolkit for interacting with and testing web applications using Playwright. Covers TypeScript, Python (pytest-playwright), and Java. Supports verifying frontend functionality, debugging UI behavior, capturing screenshots, and fixing hard-wait and XPath violations.
---

# Web Application Testing

This skill provides Playwright patterns for all three supported test languages: **TypeScript**, **Python (pytest-playwright)**, and **Java**. Use the section matching your target language.

## When to Use This Skill

- Replacing `time.sleep()` / `Thread.sleep()` / `cy.wait(n)` with proper wait strategies
- Replacing XPath locators with preferred Playwright locator APIs
- Generating or fixing test interactions (navigation, form fill, click, assertion)
- Debugging UI behavior with screenshots or console inspection

## Core Capabilities

### 1. Browser Automation
- Navigate to URLs
- Click buttons and links
- Fill form fields
- Select dropdowns
- Handle dialogs and alerts

### 2. Verification
- Assert element presence and visibility (web-first assertions — preferred)
- Verify text content
- Validate URLs and page titles
- Check element counts

### 3. Debugging
- Capture screenshots (full-page or element-scoped)
- Inspect browser console logs
- Debug failed locator interactions

---

## Usage Examples

### Example 1: Navigation + Title Assertion

**TypeScript**
```typescript
await page.goto("http://localhost:3000");
await expect(page).toHaveTitle("My App");
```

**Python**
```python
page.goto("http://localhost:3000")
expect(page).to_have_title("My App")
```

**Java**
```java
page.navigate("http://localhost:3000");
assertThat(page).hasTitle("My App");
```

---

### Example 2: Form Interaction + URL Assertion

**TypeScript**
```typescript
await page.getByLabel("Username").fill("testuser");
await page.getByLabel("Password").fill("password123");
await page.getByRole("button", { name: "Sign in" }).click();
await expect(page).toHaveURL("**/dashboard");
```

**Python**
```python
page.get_by_label("Username").fill("testuser")
page.get_by_label("Password").fill("password123")
page.get_by_role("button", name="Sign in").click()
expect(page).to_have_url("**/dashboard")
```

**Java**
```java
page.getByLabel("Username").fill("testuser");
page.getByLabel("Password").fill("password123");
page.getByRole(AriaRole.BUTTON, new Page.GetByRoleOptions().setName("Sign in")).click();
assertThat(page).hasURL(Pattern.compile(".*/dashboard"));
```

---

### Example 3: Screenshot Capture

**TypeScript**
```typescript
await page.screenshot({ path: "debug.png", fullPage: true });
```

**Python**
```python
page.screenshot(path="debug.png", full_page=True)
```

**Java**
```java
page.screenshot(new Page.ScreenshotOptions()
    .setPath(Paths.get("debug.png"))
    .setFullPage(true));
```

---

## Common Patterns

### Pattern: Web-First Assertion (preferred over explicit waits)

Use web-first assertions — they auto-wait for the element to satisfy the condition. **Never use `time.sleep()`, `Thread.sleep()`, or `cy.wait(n)`.**

**TypeScript**
```typescript
await expect(page.getByText("Welcome")).toBeVisible();
await expect(page.getByRole("button", { name: "Submit" })).toBeEnabled();
await expect(page.getByTestId("error-banner")).toBeHidden();
```

**Python**
```python
expect(page.get_by_text("Welcome")).to_be_visible()
expect(page.get_by_role("button", name="Submit")).to_be_enabled()
expect(page.get_by_test_id("error-banner")).to_be_hidden()
```

**Java**
```java
assertThat(page.getByText("Welcome")).isVisible();
assertThat(page.getByRole(AriaRole.BUTTON, new Page.GetByRoleOptions().setName("Submit"))).isEnabled();
assertThat(page.getByTestId("error-banner")).isHidden();
```

---

### Pattern: Wait for Element State (when web-first assertion is not available)

**TypeScript**
```typescript
await page.getByRole("heading", { name: "Results" }).waitFor({ state: "visible" });
```

**Python**
```python
page.get_by_role("heading", name="Results").wait_for(state="visible")
```

**Java**
```java
page.getByRole(AriaRole.HEADING, new Page.GetByRoleOptions().setName("Results"))
    .waitFor(new Locator.WaitForOptions().setState(WaitForSelectorState.VISIBLE));
```

---

### Pattern: Check if Element Exists

**TypeScript**
```typescript
const exists = await page.getByTestId("error-banner").count() > 0;
```

**Python**
```python
exists = page.get_by_test_id("error-banner").count() > 0
```

**Java**
```java
boolean exists = page.getByTestId("error-banner").count() > 0;
```

---

### Pattern: Locator Priority (generated code — in order of preference)

| Priority | Strategy | TypeScript | Python | Java |
|---|---|---|---|---|
| 1 | id | `page.locator("#submit")` | `page.locator("#submit")` | `page.locator("#submit")` |
| 2 | data-testid | `page.getByTestId("submit-btn")` | `page.get_by_test_id("submit-btn")` | `page.getByTestId("submit-btn")` |
| 3 | role | `page.getByRole("button", { name: "Submit" })` | `page.get_by_role("button", name="Submit")` | `page.getByRole(AriaRole.BUTTON, ...)` |
| 4 | text | `page.getByText("Submit")` | `page.get_by_text("Submit")` | `page.getByText("Submit")` |
| 5 | label | `page.getByLabel("Email")` | `page.get_by_label("Email")` | `page.getByLabel("Email")` |
| 6 | placeholder | `page.getByPlaceholder("Enter email")` | `page.get_by_placeholder("Enter email")` | `page.getByPlaceholder("Enter email")` |

**Never use XPath in new or generated locators.**

---

## Guidelines

1. **Verify the app is running** before navigating — check the server is accessible.
2. **Use web-first assertions** (`toBeVisible`, `to_be_visible`, `isVisible`) instead of explicit waits. Never use `time.sleep()`, `Thread.sleep()`, or `cy.wait(n)`.
3. **Prefer semantic locators** (`getByRole`, `getByLabel`, `getByTestId`) over CSS selectors. Never use XPath in new locators.
4. **Capture screenshots on failure** for debugging — use `page.screenshot()` in error handlers.
5. **Test incrementally** — start with simple navigation before complex flows.
6. **No hard waits** — use `locator.waitFor(state="visible")` when you must wait, or restructure to use a web-first assertion.
