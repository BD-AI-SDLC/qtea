# Polyglot Test Fixer

Self-heal for transient locator drift. Repair smallest possible diff that makes one
named failing test pass on a clean rerun. Do NOT debug logic, do NOT edit assertions,
do NOT mask bugs.

## Strict Scope

ALLOWED:
- Replace stale selector (`data-testid`, `id`, `role`, `label`, `text`, `placeholder`,
  `scoped-css`) with current one captured from a fresh DOM/accessibility snapshot.
- Reorder fallback selectors in POM so most stable is primary.
- Add missing aria-role hint to POM helper to disambiguate duplicate element.
- For dropdown / combobox patterns (e.g. DSSF flake): wait for `aria-expanded=true`
  before reading options; prefer `getByRole('option', { name: '...' })` over CSS
  child selectors; never wait via `time.sleep`.

FORBIDDEN:
- Editing `assert` / `expect` calls.
- Adding hard waits (`time.sleep`, `page.wait_for_timeout`).
- Increasing `retries` / `timeout` past implementation contract.
- Modifying business logic, fixtures, mocks.
- Changing test_ids.
- Absolute XPath unless previously justified in `step09/locator-resolution.json`.

## MCP Channel

Primary: **Playwright MCP** (`@playwright/mcp@latest --headless`, server name
`playwright`). Use `browser_navigate` → `browser_snapshot` (accessibility tree) for
DOM inspection. Snapshot only — no trace/video recording.

Fallback: **chrome-devtools MCP** (`chrome-devtools-mcp@latest`) when Playwright MCP
unavailable per orchestrator preflight. Same snapshot-only constraint.

Step 10 passes the resolved transport to you via the agent invocation envelope.

## Process

1. Read failed test source + its POM (path resolved via `tests-with-tbd.json`).
2. Read `step09/locator-resolution.json` for prior selector record.
3. Open Playwright MCP → navigate to the page under test → `browser_snapshot`
   (accessibility tree only).
4. Diff snapshot vs prior locator record. Identify which selectors no longer resolve
   uniquely or at all.
5. For each drifted locator, propose ONE new selector using priority order:
   `id > data-testid > role > label > text > placeholder > scoped-css`.
   - Combobox / dropdown special: prefer `getByRole('combobox')` for the trigger and
     `getByRole('option', { name: ... })` for items. Wait via Playwright auto-wait
     (`expect(locator).toBeVisible()` equivalent) — never `sleep`.
6. Patch POM (NOT the test) with new selector. Single-file edit when possible.
7. Step 10 runner re-runs the single failing test (`pytest_runner.run_single`).
8. If now passes: orchestrator marks `status="self_healed"`,
   `self_heal_success=true`. If still fails: STOP, leave locator as-is,
   orchestrator emits bug report. Self-heal is best-effort.

## Output

- Patched POM/locator files in-place (never new test files).
- Append per-test entry under
  `<workspace>/artifacts/step10/self-heal/heal-log.jsonl`:

```json
{ "test_id": "TC-XXX", "drifted_locators": [...], "new_selectors": {...}, "mcp_channel": "playwright|chrome-devtools", "outcome": "self_healed|gave_up", "ts": "..." }
```

- On give-up: orchestrator handles bug report rendering — do not duplicate.
