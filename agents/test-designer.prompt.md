# Test Design — Reference Data (Senior SDET persona)

On-demand lookup tables, templates, decision trees, and design principles for the `test-designer.agent.md`. The agent reads specific sections of this file via the Read tool when it needs a template or decision framework — it is NOT loaded into the system prompt.

Persona, mission, workflow steps, decision-making guidance, error handling, and quality gates live in `test-designer.agent.md`.

---

## §1 — Input Classification Table

| Input Type | Detection Keywords | Next Step |
|------------|-------------------|-----------|
| Feature Spec | "feature", "implement", "add", "new functionality", "user story" | Step 2A |
| Bug Report | "bug", "issue", "crash", "error", "defect", "fails when" | Step 2B |
| Test Review | "review", "audit", "assess", "coverage" | Step 2C |

If input doesn't match cleanly, default to Feature Spec but note uncertainty.

---

## §2 — Test Case Template Structure

```
TC-XXX: [Title]
- Type: [UI/API/Integration/Performance/Security]
- Priority: [P0/P1/P2/P3]
- Preconditions: [Required state — describe WHAT, not HOW]
- Steps: [Numbered list — state-changing actions only: open/click/fill/select/submit.
  Never "Verify/Confirm/Observe/Inspect/Monitor/Ensure/Assert/Validate ..." — that's an
  assertion, not a step.]
- Expected Result: [Every checkable fact the steps must produce, one bullet each — the
  "mini verifications" along the way plus the terminal "main verification" last. Never
  repeat a fact that's already worded as a step.]
```

---

## §3 — Bug Severity Decision Tree

```
Does the bug cause system crash, data loss, or security breach?
├─ YES → SEVERITY: CRITICAL
│   ├─ All users affected → PRIORITY: P0 (hours)
│   └─ Some users affected → PRIORITY: P1 (days)
└─ NO → Does core feature not work at all?
    ├─ YES → SEVERITY: MAJOR
    │   ├─ Easy workaround → PRIORITY: P2 (weeks)
    │   └─ No workaround → PRIORITY: P1 (days)
    └─ NO → Does feature work with issues?
        ├─ YES → SEVERITY: MINOR → PRIORITY: P2/P3
        └─ NO → SEVERITY: TRIVIAL → PRIORITY: P3
```

Document for each bug:
- Impact analysis: UX Impact, Business Impact, Frequency, Reproducibility (each High/Medium/Low)
- Root cause hypothesis
- Recommended actions (immediate / short-term / long-term)
- Missing test cases that should catch this bug
- Post-fix verification steps

---

## §4 — Test Case Design Principles

### Independence & Repeatability

- Each test runs standalone; no dependency on execution order
- Clean up test data after each test; avoid shared state
- Same inputs produce same outputs every time
- Use fixed, stable test data; document external dependencies

### Test Data Sourcing — No Fabrication

Never invent concrete-looking test data identifiers not present in the input spec.

- Use **exact labels from the input spec** when they exist
- When the spec has no label, use **parameterized placeholders**: `<pro-user-email>`, `<workspace-id>`
- Never generate fictional identifiers like `pro_user@test.example`, `ws-smoke-001`

### Test Case Quality

- **Titles**: Specific and descriptive (e.g., "TC-001: Verify user with valid credentials can login and is redirected to /dashboard")
- **Expected Results**: Specific and measurable (e.g., "API returns 200 OK with {token: string, userId: uuid, expiresIn: 3600}")
- **Structure**: Use the `TC-XXX` template from §2 consistently

---

## §5 — Configuration Defaults

Apply unless explicitly specified:

```yaml
thresholds:
  api_response_ms: 500
  page_load_ms: 3000
  p95_response_ms: 1000
standards:
  accessibility: "WCAG_2.1_AA"
```
