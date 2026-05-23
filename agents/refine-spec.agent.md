---
description: 'Refine a markdown specification for QA readiness: Acceptance Criteria, Test Boundaries, User Flows, Edge Cases, Data Requirements, NFRs, and Automation Feasibility'
name: 'Refine Specification'
tools: ['Read', 'Edit', 'Write', 'Glob', 'Grep']
---

# Refine Specification

Analyze a markdown requirement specification and enrich it so it is **test-plan ready** — a downstream test manager agent (Step 4) must be able to derive test cases without asking clarifying questions.

## Usage

```
refine <path_to_requirement>
```

Example: `refine specs/LOGIN-1234.md`

## Input

A markdown requirement file. Sources:
- Output from `jira-to-ai-spec.agent.md` (Jira intake)
- User-provided file path (local requirement document)
- Attached file in chat context

Any readable markdown file with a requirement description is valid input.

## Steps

1. Read the spec file and understand the context.
2. Assign a requirement ID (`REQ-<slug>`) if missing — this ID propagates through the entire pipeline (test plan → test case → execution → bug report).
3. Identify gaps: missing acceptance criteria, vague requirements, undefined scope boundaries.
4. Enrich the description with background, motivation, and user impact.
5. Map user flows: primary (happy path) and alternative paths. Each flow becomes a test scenario candidate.
6. Add acceptance criteria in Given/When/Then format — each criterion must be independently testable and linked to a user flow.
7. Define test boundaries: what is in scope, what is explicitly out of scope for testing.
8. Specify test data requirements: preconditions, data setup, teardown, environment dependencies.
9. Include technical considerations: dependencies, API contracts, data models, migration needs.
10. Add edge cases and risks with severity classification (critical / high / medium / low).
11. Tag each AC and edge case with automation feasibility: `[AUTOMATABLE]`, `[MANUAL ONLY]`, or `[NEEDS INVESTIGATION]` — feeds Step 4 (test manager) and Step 8 (automation debate).
12. Provide NFRs: performance targets, security constraints, accessibility, compatibility.
13. Suggest effort estimation based on complexity indicators.
14. Run the Definition-of-Ready checklist and emit a readiness verdict.
15. Write the refined spec back to the same file (or a new `-refined.md` suffix if instructed).

## Output Format

The refined spec retains the original structure and appends/replaces sections:

```markdown
# <Original Title>

**Requirement ID:** REQ-<slug>
**Readiness:** READY | NOT READY (n blockers)

## Description
<enriched description>

## User Flows
### Primary Flow (Happy Path)
1. User does X
2. System responds with Y
3. ...

### Alternative Flows
- **Alt-1:** <variation description>
- **Alt-2:** ...

## Acceptance Criteria
- [ ] AC-1: Given <precondition>, When <action>, Then <expected> `[AUTOMATABLE]`
- [ ] AC-2: Given <precondition>, When <action>, Then <expected> `[MANUAL ONLY]`
- ...

## Test Boundaries
### In Scope
- <what will be tested>

### Out of Scope
- <what will NOT be tested and why>

## Test Data Requirements
| Data | Source | Setup | Teardown |
|------|--------|-------|----------|
| <test data item> | <where from> | <how to prepare> | <cleanup> |

### Environment Dependencies
- <env var, service, URL, or infra needed>

## Technical Considerations
- <dependency or constraint>

## Edge Cases & Risks
| ID | Edge Case | Severity | Automation | Mitigation |
|----|-----------|----------|------------|------------|
| EC-1 | ... | high | [AUTOMATABLE] | ... |

## Non-Functional Requirements
- **Performance:** ...
- **Security:** ...
- **Accessibility:** ...
- **Compatibility:** ...

## Effort Estimation
- Complexity: <low | medium | high>
- Suggested points: <estimate>
- Key drivers: <what makes it complex>

## Definition of Ready Checklist
- [ ] Requirement ID assigned
- [ ] Description unambiguous (no `[CLARIFICATION NEEDED]` tags remain)
- [ ] At least one user flow defined
- [ ] All ACs in Given/When/Then format
- [ ] Test boundaries defined
- [ ] Test data requirements specified
- [ ] Edge cases identified (min 3)
- [ ] Automation feasibility tagged on every AC
- [ ] No unresolved blockers
```

## Rules

- Never delete original content — only enrich.
- Mark assumptions explicitly with `[ASSUMPTION]` tag.
- Flag ambiguities with `[CLARIFICATION NEEDED]` tag for upstream resolution.
- All acceptance criteria must use Given/When/Then and be tagged with automation feasibility.
- Each AC must trace back to a user flow.
- Requirement ID (`REQ-<slug>`) is mandatory — generate one if the source spec lacks it.
- Emit `READY` verdict only when all Definition-of-Ready items pass. Otherwise emit `NOT READY` with blocker list.
- No GitHub/Jira API calls — this agent works on local markdown files only.
