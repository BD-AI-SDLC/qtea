# Test Design Template

Use this template when designing test cases for new features (Step 4, Senior SDET persona).

---

## Test Design: [Feature Name]

**Created**: [Date]

---

### Scope

**In scope:**

- [Feature/component 1]
- [Feature/component 2]

**Out of scope:**

- [Out of scope item 1]
- [Out of scope item 2]

---

### Test Cases

#### TC-001: [Test Case Title]

- **Type**: [UI | API | Integration | Performance | Security]
- **Priority**: [P0 | P1 | P2 | P3]
- **Preconditions**:
  - [Precondition 1]
  - [Precondition 2]
- **Steps** (state-changing actions only — never "Verify/Confirm/Observe/Inspect/Monitor/Ensure/Assert/Validate ..."):
  1. [Step 1]
  2. [Step 2]
  3. [Step 3]
- **Expected Result** (every checkable fact the steps must produce, one bullet each — mini verifications first, main verification last; never repeat a fact already worded as a step):
  - [Expected outcome 1]
  - [Expected outcome 2]
- **Tags**: [tag1, tag2]

#### TC-002: [Next Test Case]

[Repeat structure...]

---

### Coverage Notes

> Required when any TC was dropped or excluded — by upstream HITL (`plan.md` / `refined-spec.md` Coverage Notes), or by the test-designer in non-interactive mode when a fact required by a TC was missing. List each dropped TC ID and the reason. Omit ONLY if no drops have been recorded for this run.
>
> **Never** add an Assumptions section. Assumptions cause hallucinations; we test facts only. Missing facts mean missing tests, recorded here — not invented tests.

- **TC-XXX:** Dropped — [reason, e.g. "user skipped clarification on expected aria-label text"]
- **<Topic>:** Excluded — user said "<exact answer>"
