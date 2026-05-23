# QA Orchestrator - Step-by-Step Instructions

> HOW to execute what `agents/qa-orchestrator.agent.md` defines.
> Read `CLAUDE.md` first -- it is the source of truth for pipeline structure,
> agent-model map, MCP servers, and non-negotiable rules.

## Architecture at a Glance

Two layers cooperate to run the pipeline:

- **`pipeline.py`** — deterministic orchestrator. Drives steps in order, loads/saves
  checkpoints via `checkpoints.py`, validates schemas via `schemas.py`. No reasoning.
- **`claude_runner.py`** — agent executor. Spawns `claude` CLI subprocesses, streams
  output, enforces per-step timeouts. Returns artifacts to `pipeline.py` for validation.
- **This agent (QA Orchestrator)** — semantic reasoning only. Decides what inputs to
  pass, interprets failures, and drives the fix-proposal flow on persistent failure.

**The boundary is clean: `pipeline.py` never reasons. This agent never checkpoints.**

---

## 1. Initialize the run

1. Generate a `run_id` (ISO-8601 timestamp or UUID).
2. Create the workspace directory tree:
   ```
   .worca-t/<run-id>/
     state.json          # checkpoint state (managed by checkpoints.py)
     run.log.jsonl       # structured log (structlog)
     debug/
     artifacts/step01/ ... artifacts/step11/
     step-01/ ... step-11/
     sut/
   ```
3. Write the initial `state.json` with all 11 steps set to `pending`, `attempts: 0`.
4. If `--force` is set, ignore any existing `state.json` and start fresh.
5. If resuming, call `load_state()` and `next_pending_step()` to find the
   first non-completed step. Verify output hashes of completed steps via
   `outputs_match()` -- if any hash mismatches, mark that step `pending`.

---

## 2. Per-step operating loop

For each step in `_select_steps()` (respecting `--from-step`, `--only-step`, `--skip`), execute this loop:

### 2.1 Pre-flight

1. Confirm every required input artifact exists on disk (see section 3).
2. Validate each JSON input against its schema in `schemas/` via `schemas.py`.
3. If any input is missing or invalid: **refuse to proceed**. Log the error
   to `run.log.jsonl` with `step`, `agent`, `correlation_id`. Abort the step.

### 2.2 Dispatch

1. Look up the agent for this step in `CLAUDE.md` section 2.
2. Look up the model for that agent in `src/worca_t/agent_models.yaml`.
3. Invoke the agent via `claude_runner.py` (`run_agent()`), which spawns the
   `claude` CLI with:
   - The correct model from the agent-model map.
   - The curated input bundle (files listed in section 3 below).
   - MCP servers from `.mcp.json` (Playwright, Chrome DevTools, Atlassian).
   - A per-step timeout cap (see `config.py`; max **1800 seconds**).
4. Stream agent progress. Write each event to `run.log.jsonl` with fields:
   `run_id`, `step`, `agent`, `attempt`, `correlation_id`, `timestamp`.
5. Mask secrets in all log output: `ANTHROPIC_API_KEY`, `ATLASSIAN_API_TOKEN`,
   `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.

### 2.3 Validate

1. Confirm every expected output artifact exists (see section 3).
2. Validate each JSON output against its schema in `schemas/` via `schemas.py`.
3. For step 7, additionally scan `tests-with-tbd.json` for `violations[]`.
   Any violation with rule `xpath`, `hard-wait`, `page-content`, or
   `raw-secret` is a hard failure -- reject the output.
4. If validation fails, treat as a step failure (go to 2.5).

### 2.4 Checkpoint

1. Update the `StepRecord` in `state.json` (`checkpoints.py`):
   - `status: "completed"` (or `"skipped"` for step 5 without Xray creds).
   - `attempts`, `started_at`, `finished_at`, `duration_s`, `output_hashes`.
3. Persist atomically via `save_state()` (write `.tmp`, then rename).
4. Proceed to the next step.

### 2.5 On failure

```
Attempt 1 fails  (steps/base.py MAX_ATTEMPTS=2)
  --> Attempt 2: re-run same inputs + co-run debug.agent.md (claude-sonnet-4-6)
        --> Attempt 2 fails:
              if --fix: invoke critical-thinking (claude-opus-4-6) for RCA
                        --> feed analysis to principal-software-engineer (claude-opus-4-6)
                        --> produce fix-proposal.md  (NEVER auto-edit code)
              else: ABORT pipeline, mark step "failed" in state.json
```

1. On first failure, increment `attempts` to 2, set `status: "in_progress"`.
2. Re-invoke the same agent with the same inputs, but also co-run
   `debug.agent.md` (claude-sonnet-4-6) for verbose diagnostics.
   If `--debug` was set, `debug.agent.md` was already running from attempt 1.
3. If attempt 2 also fails and `--fix` is set:
   a. Invoke `critical-thinking` (claude-opus-4-6) with the failure context.
      It challenges assumptions and performs root-cause analysis.
      It does NOT produce fixes -- only questions and analysis.
   b. Feed the critical-thinking output to `principal-software-engineer`
      (claude-opus-4-6). It produces `fix-proposal.md` with engineering
      recommendations. It NEVER auto-edits source code.
   c. Write `fix-proposal.md` to `artifacts/stepNN/`.
4. If attempt 2 fails and `--fix` is NOT set: mark step `"failed"`, abort.

---

## 3. Step input/output contracts

### Phase A — Requirements & Planning

#### Step 1 -- Intake

| Field | Value |
|---|---|
| Agent | `jira-to-ai-spec` (claude-sonnet-4-6), or pure file copy |
| Input | `--spec` flag (Jira ticket ID/URL or local markdown path) |
| Output | `artifacts/step01/spec.md` (required), `jira-spec.md` (if Jira) |
| Schema | n/a (non-empty file check only) |

**Procedure:**
1. If `--spec` starts with `jira:`, invoke `jira-to-ai-spec` with the
   Atlassian MCP to extract ticket data. Output both `jira-spec.md`
   (raw extraction) and `spec.md` (formatted for pipeline).
2. If `--spec` is a local file, copy it to `artifacts/step01/spec.md`.

**Phase gate:** `spec.md` exists and is non-empty.

---

#### Step 2 -- Refine specification

| Field | Value |
|---|---|
| Agent | `refine-spec` (claude-sonnet-4-6) |
| Input | `artifacts/step01/spec.md` |
| Output | `artifacts/step02/refined-spec.md`, `refined-spec.json` |
| Schema | `schemas/refined-spec.schema.json` |

**Phase gate:** `requirement_id` matches `^REQ-[A-Za-z0-9][A-Za-z0-9\-_]*$`.
`acceptance_criteria` array is non-empty. These `REQ-*` IDs are the
traceability backbone -- they propagate through every downstream artifact.

---

#### Step 3 -- Test plan

| Field | Value |
|---|---|
| Agent | `polyglot-test-planner` (claude-sonnet-4-6) |
| Input | `artifacts/step02/refined-spec.md`, `refined-spec.json` |
| Output | `artifacts/step03/plan.md`, `plan.json` |
| Schema | `schemas/plan.schema.json` |

**Phase gate:** `phases` array is non-empty. Each phase has `number` and `title`.

---

#### Step 4 -- Test strategy

| Field | Value |
|---|---|
| Agent | `test-manager` (claude-sonnet-4-6) |
| Input | `artifacts/step03/plan.json` |
| Output | `artifacts/step04/test-strategy.md`, `test-strategy.json` |
| Schema | `schemas/test-strategy.schema.json` |

**Phase gate:** `test_cases` array is non-empty. Each `id` matches
`^TC-[A-Za-z0-9\-_]+$`. Every `priority` is one of P0-P3 or UNKNOWN.

---

### Phase B -- Research & Implementation

#### Step 5 -- Xray upload (optional)

| Field | Value |
|---|---|
| Agent | None (pure code) |
| Input | `artifacts/step04/test-strategy.json`, Xray env vars |
| Output | `artifacts/step05/xray-mapping.json` |
| Schema | `schemas/xray-mapping.schema.json` |

**Procedure:**
1. Check for `JIRA_XRAY_API_KEY` (or client ID/secret).
2. If credentials are absent: write `xray-mapping.json` with
   `status: "skipped"`, mark step `"skipped"` in state, proceed.
3. If `--strict-xray` is set and credentials are absent: hard-fail.
4. If credentials present: upload test cases to Xray, record mappings.

**Phase gate:** `status` is one of `completed | skipped | warned | failed`.

---

#### Step 6 -- Research the SUT

| Field | Value |
|---|---|
| Agent | `polyglot-test-researcher` (claude-haiku-4-5) |
| Input | `--sut` (local path or git URL) |
| Output | `artifacts/step06/research.md`, `research.json` |
| Schema | `schemas/research.schema.json` |

**Procedure:**
1. If `--sut` is a git URL, clone to `.worca-t/<run-id>/sut/`.
2. Pass the SUT directory to `polyglot-test-researcher`.
3. The agent discovers the test automation stack using 3-signal detection:
   dependency files + imports + config files.
4. **Critical output**: `detected_stack` determines which polyglot codegen
   path step 7 takes. If `null`, step 7 falls back to universal patterns.

**Phase gate:** `sections` array is non-empty. Warn if `detected_stack` is
null but do not fail.

---

#### Step 7 -- TDD codegen

| Field | Value |
|---|---|
| Agent | `ui-test-automation` (claude-opus-4-6) |
| Input | `artifacts/step04/test-strategy.json`, `artifacts/step06/research.json`, `--sut` path |
| Output | Test source files in `sut/`, `artifacts/step07/tests-with-tbd.json` |
| Schema | `schemas/tests-with-tbd.schema.json` |

**Phase gate:**
- `framework` is a recognized enum value.
- `tests` array is non-empty.
- `violations[]` must be **empty**. Any `xpath`, `hard-wait`, `page-content`,
  or `raw-secret` violation is a **hard rejection** -- return to agent for correction.
- Every test has `tc_refs` linking back to `TC-*` IDs from step 4.

---

#### Step 8 -- Locator discovery

| Field | Value |
|---|---|
| Agent | `playwright-tester` (claude-sonnet-4-6) |
| Input | `artifacts/step07/tests-with-tbd.json`, `artifacts/step06/research.json`, `SUT_BASE_URL` env var |
| Output | `artifacts/step08/locator-resolution.json` |
| Schema | `schemas/locator-resolution.schema.json` |

**Procedure:**
1. Navigate the live application via the Playwright MCP
   (AOM snapshots only -- never `page.content()`).
2. For each TBD marker, discover the real locator using the priority
   chain: `id > data-testid > role > label > text > placeholder > css`.
   **Never XPath.**
3. Patch the test source files, replacing TBD markers with real
   locators, and record each resolution.

**Phase gate:**
- `resolutions` array is non-empty.
- Every `strategy` value is in the allowed enum (no `xpath`).
- Check `totals.skipped` -- warn if any TBDs remain unresolved.

---

### Phase C -- Execution & Reporting

#### Step 9 -- Execute and self-heal

| Field | Value |
|---|---|
| Agents | `polyglot-test-tester` (claude-haiku-4-5) for execution, `polyglot-test-fixer` (claude-haiku-4-5) for self-heal |
| Input | `artifacts/step07/tests-with-tbd.json` (TBDs resolved), `--sut` path, `--parallelism N`, `--headless\|--headed` |
| Output | `artifacts/step09/run-results.json`, screenshots, traces, `bugs/*.md` candidates |
| Schema | `schemas/run-results.schema.json` |

**Procedure:**
1. Invoke `polyglot-test-tester` to run the test command from
   `research.json.commands`.
2. Collect results, screenshots (on failure), and traces.
3. If tests fail due to locator drift, invoke `polyglot-test-fixer`
   to self-heal: patch POM/page-object files with current selectors
   using the Playwright or Chrome DevTools MCP.
4. Re-run healed tests. Record heal attempts in `self_heal` fields.
5. Write failed-test candidates to `bugs/*.md` for step 10.

**Phase gate:** `results` array is non-empty. Every result has a valid
`status` (passed|failed|skipped|error). Screenshots attached for every
failed test.

---

#### Step 10 -- Bug classification

| Field | Value |
|---|---|
| Agent | `bug-report-classifier` (claude-sonnet-4-6) |
| Input | `artifacts/step09/run-results.json`, `artifacts/step04/test-strategy.json`, `bugs/*.md` candidates |
| Output | `artifacts/step10/bug-reports.md`, `bug-reports.json` |
| Schema | `schemas/bug-reports.schema.json` |

**Phase gate:**
- `bugs[].id` matches `^BUG-.+-\d+$`.
- Every bug has `severity`, `priority`, `category`.
- Each bug links back to `requirement_id` (REQ-*) for traceability.
- `summary.total_failures` matches the actual count of bugs.

---

#### Step 11 -- Report generation

| Field | Value |
|---|---|
| Agent | None (pure code via `report/`) |
| Input | `artifacts/step09/run-results.json`, `artifacts/step10/bug-reports.json`, `artifacts/step04/test-strategy.json` |
| Output | `artifacts/step11/report/index.html`, optionally `allure-report/`, `allure-summary.json` |
| Schema | `schemas/report-data.schema.json` |

**Procedure:**
1. Assemble `report-data.json` combining run results, bug reports, plan,
   strategy, and summary stats.
2. Generate built-in HTML report at `artifacts/step11/report/index.html`.
   Zero-dependency and viewable offline.
3. If `allure` CLI is available and `--report` is `auto|allure|both`:
   generate Allure report at `artifacts/step11/allure-report/`.
4. If `--report-inline-images`: base64-embed screenshots into HTML.
5. If `--open-report`: open the report in the default browser.

**Phase gate:** `report/index.html` exists. `pass_rate = passed / total_tests`.

---

## 4. Artifact dependency graph

```
Step 1  --> spec.md -----------------------> Step 2
Step 2  --> refined-spec.md/json ----------> Step 3
Step 3  --> plan.md/json ------------------> Step 4
Step 4  --> test-strategy.md/json ---------> Steps 7, 9, 10, 11
Step 5  --> xray-mapping.json            (stand-alone; no downstream dependency)
Step 6  --> research.md/json --------------> Steps 7, 8, 9
Step 7  --> tests-with-tbd.json -----------> Steps 8, 9
         -> test source files in sut/
Step 8  --> locator-resolution.json -------> Step 9
Step 9  --> run-results.json --------------> Steps 10, 11
         -> bugs/*.md
Step 10 --> bug-reports.md/json -----------> Step 11
Step 11 --> report/index.html
         -> allure-report/ (optional)
```

---

## 5. Traceability chain

Every artifact links back to the requirement that spawned it:

```
REQ-<slug>  (step 2: refine-spec assigns)
  --> TC-<slug>  (step 4: test-manager creates test cases)
      --> test files with tc_refs  (step 7: ui-test-automation)
          --> run-results per test  (step 9: polyglot-test-tester)
              --> BUG-<run-id>-<seq> with requirement_id  (step 10)
                  --> report links all layers  (step 11)
```

If a requirement ID is missing or broken at any step, flag it and halt.

---

## 6. Quality gates

These are non-negotiable. Violations trigger step rejection and the retry/fix cycle.

| Gate | Enforced at | Constraint |
|---|---|---|
| Locator priority | Step 8 agent | `id > data-testid > role > label > text > placeholder > css` |
| No XPath | Steps 7, 8 | `xpath` in `violations[]` is a hard rejection |
| No hard waits | Step 7 | `hard-wait` in `violations[]` is a hard rejection |
| No `page.content()` | Steps 8, 9 | `page-content` in `violations[]` is a hard rejection |
| No raw secrets | Step 7 | `raw-secret` in `violations[]` is a hard rejection |
| TC traceability | Step 7 | Every test must have `tc_refs` linking to `TC-*` IDs |
| REQ traceability | Steps 2, 10 | Every requirement and bug links to a `REQ-*` ID |
| Self-heal scope | Step 9 | Locators ONLY -- never assertions, never business logic |
| Schema-first | Every step | Every JSON artifact validated via `schemas.py` before hand-off |
| AOM snapshot only | Steps 8, 9 | Never `page.content()` -- AOM only |

---

## 7. Observability

Every log entry in `run.log.jsonl` must include:

| Field | Description |
|---|---|
| `run_id` | Current run identifier |
| `step` | Integer 1-11 |
| `agent` | Agent name from `CLAUDE.md` section 2 |
| `attempt` | 1 or 2 |
| `correlation_id` | Unique per dispatch, for tracing |
| `timestamp` | ISO-8601 |
| `level` | `info`, `warn`, or `error` |
| `message` | What happened |

Secrets are always masked before writing to any log or artifact.

---

## 8. Finalization

After step 11 completes (or after abort):
1. Set `finished_at` in `state.json`.
2. Persist final state via `save_state()`.
3. Log a summary line: total steps completed, total duration, pass rate.
4. If the run completed successfully, all 11 steps show `completed`
   (or `skipped` for step 5). If any step is `failed`, the run is partial.
