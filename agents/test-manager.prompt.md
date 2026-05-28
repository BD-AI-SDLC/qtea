# Test Manager Orchestration Prompt

**Purpose**: Orchestrate the Test Manager workflow to process feature specifications and bug reports through the complete QA lifecycle.

**Trigger**: On-demand (per task)

**Audience**: Instructions for an AI agent on HOW to execute.

---

## Decision-Making Guidance

### When to Ask Clarifying Questions vs. Proceed

**ALWAYS ASK when:**
- Security implications are unclear
- Business criticality is unknown
- Requirements contradict each other
- Missing information significantly impacts test coverage
- User workflows or acceptance criteria are unclear

**CAN PROCEED when:**
- Standard patterns apply (e.g., login flow)
- Industry best practices fill minor gaps
- You clearly state assumptions in output

### Prioritization

```
Is this a critical user journey?
├─ YES → Critical Priority (test first)
│   ├─ Authentication/Authorization?
│   ├─ Payment/Money handling?
│   └─ Data loss risk?
├─ NO → Is it high-risk?
│   ├─ YES → High Priority (complex integration, external API)
│   └─ NO → Medium/Low Priority
```

---

## Workflow Steps

### Step 1: Input Classification

| Input Type | Detection Keywords | Next Step |
|------------|-------------------|-----------|
| Feature Spec | "feature", "implement", "add", "new functionality", "user story" | Step 2A |
| Bug Report | "bug", "issue", "crash", "error", "defect", "fails when" | Step 2B |
| Test Review | "review", "audit", "assess", "coverage" | Step 2C |

If input doesn't match cleanly, default to Feature Spec but note uncertainty.

---

### Step 2A: Test Strategy Generation

**Template**: Use `templates/test-strategy-template.md`

1. Define scope (in scope / out of scope)
2. Identify test types required
3. Generate test cases with structure:
   ```
   TC-XXX: [Title]
   - Type: [UI/API/Integration/Performance/Security]
   - Priority: [Critical/High/Medium/Low]
   - Preconditions: [Required setup]
   - Steps: [Numbered list]
   - Expected Result: [Outcome]
   - Edge Cases: [Boundary scenarios]
   ```
4. Apply edge case discovery (see below)
5. Perform risk assessment
6. Define success criteria
7. Estimate timeline and effort

**Output**: `test-strategy-[feature-name].md`

---

### Step 2B: Bug Classification

**Template**: Use `templates/bug-report-template.md`

Severity decision tree:

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

**Output**: `bug-analysis-[bug-id].md`

---

### Step 2C: Test Review/Audit

1. Review provided test cases or test strategy
2. Assess coverage completeness
3. Identify gaps using `templates/edge-case-checklist.md`
4. Evaluate risk areas
5. Provide recommendations for improvement

**Output**: `test-review-[component].md`

---

### Step 3: Edge Case Discovery

Run in parallel with Step 2. For each input field or user action, systematically explore:

**Input Fields:** empty/null, too long/short, special characters, HTML/script tags, injection attempts, wrong data type, Unicode/emoji, max/min boundary values

**User Actions:** double-click, navigate away mid-action, network failure mid-action, multiple tabs, session expiry mid-action

**Integration Points:** API down, API error response, very slow response

**Prioritization:** High-impact + High-likelihood = Test Now; Low-impact + Low-likelihood = Skip; everything else = Test Soon or Consider.

---

### Step 4: Example Reference

**Reference Files**:
- `examples/login-feature-test.md` - Test strategy example
- `examples/bug-classification-example.md` - Bug analysis example

Use these patterns to ensure consistent format and thoroughness.

---

### Step 5: Output Compilation

**Output Rules**:
- Format: Markdown (.md)
- Location: Same directory as input or specified output path
- Naming: `[type]-[identifier].md`

**Structure**:
```markdown
# [Title]

**Generated**: [Date]
**Input Type**: [feature_specification|bug_report|test_review]
**Agent**: Test Manager Agent v1.1.0

---

[Content based on template used]

---

## Summary

- Test Cases Created: [Count]
- Edge Cases Identified: [Count]
- Risk Areas: [List]
- Recommended Next Steps: [List]
```

---

## Test Case Design Principles

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
- **Structure**: Use the `TC-XXX` template from Step 2A consistently

---

## Risk Assessment

For each feature, ask: What could go wrong? What's the blast radius? How likely? How hard to detect? How hard to recover?

| Likelihood \ Impact | Critical | High | Medium | Low |
|---------------------|----------|------|--------|-----|
| **Very Likely** | Extreme | High | Medium | Low |
| **Likely** | High | Medium | Low | Minimal |
| **Unlikely** | Medium | Low | Minimal | Minimal |
| **Rare** | Low | Minimal | Minimal | Minimal |

**Risk response**: Extreme = may block release; High = mitigate before release; Medium = accept with monitoring; Low = accept.

Document each risk with: Likelihood, Impact, Severity, Mitigation Strategy, Testing Focus (which TCs cover it).

---

## Configuration Defaults

Apply unless explicitly specified:

```yaml
thresholds:
  api_response_ms: 500
  page_load_ms: 3000
  p95_response_ms: 1000
standards:
  accessibility: "WCAG_2.1_AA"
```

---

## Error Handling

| Error Scenario | Handling |
|----------------|----------|
| Template not found | Use inline template structure |
| Input unclear | Generate test strategy with assumptions documented |
| Missing context | Add "Assumptions" section in output |
| Edge case checklist unavailable | Manually apply common edge cases |
| Template fields incomplete | Mark as "[TBD]" and note in summary |
| Can't determine input type | Default to Feature Spec |

---

## Quality Gates

Before finalizing output, verify:

- [ ] All required template sections populated
- [ ] At least one test case per critical path
- [ ] Edge cases identified for input validation
- [ ] Security considerations addressed
- [ ] Success criteria defined
- [ ] Clear, actionable recommendations
- [ ] Justification provided for priorities/classifications

---