# Refine Specification

Analyze a markdown requirement specification and enrich it so it is **test-plan ready** — a downstream test manager agent (Step 4) must be able to derive test cases without asking clarifying questions.

## Input

A markdown requirement file. Sources:
- Output from `jira-to-ai-spec.agent.md` (Jira intake)
- User-provided file path (local requirement document)
- Attached file in chat context

Any readable markdown file with a requirement description is valid input.

## Pre-clean Pass (run before Step 1)

Some inputs are clean narrative specs. Others are raw Jira/Confluence exports
where the actual requirement is buried under issue metadata, attachment lists,
sub-task tables, issue-link graphs, Figma-link tables, and comment threads.
Refining the noisy version directly poisons every downstream step.

**Decide:** is the input "messy"? Treat it as messy when **two or more** of
these signals are present:

- A leading metadata table whose first row mentions things like *Issue
  summary*, *Status*, *Project*, *Reporter*, *Assignee*, *Resolution*,
  *Sprint*, *Story Points*, *Epic Link*.
- Sections or table rows labelled *Attachments:*, *Issue links:*,
  *Sub-tasks:*, *Comments*, *Zephyr*, or similar tracker-tool boilerplate.
- The substantive requirement content (Description / Acceptance Criteria)
  occupies less than ~25% of the file by line count.
- More than a handful of image links, Figma links, or external URLs
  embedded in tables rather than in prose.

**If messy:** before doing anything else, reduce the file to *only* the
substantive content:

1. Extract the **Description** — whether it's a `## Description` heading or a
   `**Description:**` row inside a metadata table. Keep the prose; drop
   surrounding table chrome.
2. Extract the **Acceptance Criteria** — same rule. The criteria are often a
   single `**Acceptance Criteria:**` cell in a metadata table; lift the
   contents out as a proper `## Acceptance Criteria` section with one bullet
   per criterion.
3. Discard everything else: issue summary tables, attachments, issue links,
   sub-tasks, comments, UX-design tables of Figma links, image-only rows,
   sprint/label/assignee metadata.
4. Overwrite the workdir's `./spec.md` (your local working copy — the canonical
   Step 1 artifact at `artifacts/step01/spec.md` is read-only and is not touched
   by this pass). Then proceed to Step 1 below. The rest of the refinement runs
   on the cleaned text.

**If clean:** skip this pass and go straight to Step 1.

Be conservative: if you're not sure something is garbage, keep it. The goal
is to remove tracker boilerplate, not to second-guess the author.

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
15. Write the refined spec to `./refined-spec.md` in the workdir. The Python pipeline at `src/worca_t/steps/s02_refine.py:120` copies it to `artifacts/step02/refined-spec.md`. Do not overwrite `spec.md`.

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

- Never delete original content — only enrich. **Exception:** during the
  Pre-clean Pass above, tracker boilerplate (Jira/Confluence metadata,
  attachment lists, issue links, sub-tasks, comments, Figma-link tables) may
  and should be stripped so refinement runs on the substantive requirement.
- Mark assumptions explicitly with `[ASSUMPTION]` tag.
- Flag ambiguities with `[CLARIFICATION NEEDED]` tag for upstream resolution. Involve user to answer if needed.
- All acceptance criteria must use Given/When/Then and be tagged with automation feasibility.
- Each AC must trace back to a user flow.
- Requirement ID (`REQ-<slug>`) is mandatory — generate one if the source spec lacks it.
- Emit `READY` verdict only when all Definition-of-Ready items pass. Otherwise emit `NOT READY` with blocker list.
- Each unresolved item must appear in exactly ONE canonical location. If an item is listed in the Blockers table, do NOT also emit a `[CLARIFICATION NEEDED]` inline tag for the same item. Keep the item solely in the Blockers table.
- No GitHub/Jira API calls — this agent works on local markdown files only.
- The refined spec has to be unique and not contain any duplicate content.
- If there are any Blockers or `[CLARIFICATION NEEDED]` tags, the agent must ask the user to resolve them before finalizing the refined spec.

## Non-interactive mode

If the orchestrator runs with `--no-hitl` / `--yes` (HITL disabled — see `src/worca_t/steps/base.py:222`), do not block on user input. Emit the refined spec with `Readiness: NOT READY` and the full blocker list intact. The orchestrator decides whether to halt the pipeline or proceed. Never invent answers to clarifications you would otherwise have asked.