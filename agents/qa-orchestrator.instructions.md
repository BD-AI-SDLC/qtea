# QA Orchestrator - Step-by-Step Instructions

> HOW to execute what `agents/qa-orchestrator.agent.md` defines.
> Read `CLAUDE.md` first -- it is the source of truth for pipeline structure,
> agent-model map, MCP servers, and non-negotiable rules.
>
> **Single sources of truth** (this file points at them; never duplicate):
> - Agent → model map: [`src/worca_t/agent_models.yaml`](../src/worca_t/agent_models.yaml)
> - MCP servers: [`.mcp.json`](../.mcp.json)
> - Per-step timeouts: [`src/worca_t/config.py`](../src/worca_t/config.py)
> - Schemas: [`schemas/*.schema.json`](../schemas/)
>
> Model names and timeout numbers are deliberately omitted from the step
> tables below to prevent drift. Look them up in the files above.

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
6. **SUT preflight.** Materialize `--sut` into `<workspace>/sut/` (clone or
   link) and put it on the worca-t isolation branch before any step runs.
7. **MCP preflight.** Cold-start every server in `.mcp.json` via
   `mcp_manager.probe_server()`. On failure, prompt the user (TTY) to retry;
   non-TTY / `--no-hitl` / `--yes` fail fast with exit code 2. Side effect:
   warms the npx cache so the first agent call doesn't pay the bootstrap cost.

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
   - The model resolved from the agent-model map.
   - The curated input bundle (files listed in section 3 below).
   - MCP servers as configured in [`.mcp.json`](../.mcp.json).
   - A per-step timeout cap from [`config.py`](../src/worca_t/config.py)
     (`step_timeout(N)`; the global cap is `MAX_STEP_TIMEOUT`).
4. Stream agent progress. Write each event to `run.log.jsonl` with fields:
   `run_id`, `step`, `agent`, `attempt`, `correlation_id`, `timestamp`.
5. Mask secrets in all log output: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`,
   `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.

### 2.3 Validate

1. Confirm every expected output artifact exists (see section 3).
2. Validate each JSON output against its schema in `schemas/` via `schemas.py`.
3. For step 7, additionally scan `tbd-index.json` for `violations[]`.
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
  --> Attempt 2: re-run same inputs + co-run debug.agent.md
        --> Attempt 2 fails:
              if --fix: invoke critical-thinking for RCA
                        --> feed analysis to principal-software-engineer
                        --> produce fix-proposal.md and auto-edit code (no auto-edits if --fix not set)
              else: ABORT pipeline, mark step "failed" in state.json
```

(Models for `debug`, `critical-thinking`, and `principal-software-engineer`
live in `src/worca_t/agent_models.yaml`.)

1. On first failure, increment `attempts` to 2, set `status: "in_progress"`.
2. Re-invoke the same agent with the same inputs, but also co-run
   `debug.agent.md` for verbose diagnostics.
   If `--debug` was set, `debug.agent.md` was already running from attempt 1.
3. If attempt 2 also fails and `--fix` is set:
   a. Invoke `critical-thinking` with the failure context.
   b. Feed the critical-thinking output to `principal-software-engineer`.
   c. Write `fix-proposal.md` to `artifacts/stepNN/`and auto-edit code.
4. If attempt 2 fails and `--fix` is NOT set: mark step `"failed"`, abort.

---

## 3. Step input/output contracts

### Phase A — Requirements & Planning

#### Step 1 -- Intake

| Field | Value |
|---|---|
| Agent | `jira-to-ai-spec`, or pure file copy |
| Input | `--spec` flag (Jira ticket ID/URL or local markdown path) |
| Output | `artifacts/step01/spec.md` (required), `jira-spec.md` (if Jira) |
| Schema | n/a (non-empty file check only) |

**Procedure:**
1. If `--spec` starts with `jira:` or is a full `https://.../browse/KEY` URL,
   fetch the issue via direct Jira REST (`worca_t.jira_client.fetch_issue` —
   the Atlassian MCP was retired in commit `a36dbbe`), slim the payload, and
   invoke `jira-to-ai-spec` with the JSON inlined under the `jira-issue.json`
   header. Output both `jira-spec.md` (provenance stub) and `spec.md` (the
   agent's enriched 10-section output).
2. If `--spec` is a local file, copy its content verbatim to
   `artifacts/step01/spec.md` (no LLM call at step 1).
3. If `--spec` is a non-JIRA URL, download its body and write it verbatim to
   `artifacts/step01/spec.md` (no LLM call at step 1).

**Phase gate:** `spec.md` exists and is non-empty.

---

#### Step 2 -- Refine specification

| Field | Value |
|---|---|
| Agent | `refine-spec` |
| Input | `artifacts/step01/spec.md` |
| Output | `artifacts/step02/refined-spec.md`, `refined-spec.json` |
| Schema | `schemas/refined-spec.schema.json` |
| Transport | `worca_t.llm.reasoning.call_reasoning_llm_with_hitl` (direct Anthropic SDK, multi-turn HITL) |

**HITL loop.** The transport extracts `[CLARIFICATION NEEDED]` tags,
Blockers table rows, and Open Questions bullets from the agent's output
via `worca_t.hitl.extract_questions`. Skipped items are deduped across
iterations (see `worca_t.hitl._dedup`) so the user is never re-prompted
for the same concern. Capped at `HITL_MAX_ITERATIONS` (3). Auto-skips
when `--no-hitl` is set or stdin is not a TTY.

**Phase gate:** `requirement_id` matches `^REQ-[A-Za-z0-9][A-Za-z0-9\-_]*$`.
`acceptance_criteria` array is non-empty. These `REQ-*` IDs are the
traceability backbone -- they propagate through every downstream artifact.

---

#### Step 3 -- Test plan

| Field | Value |
|---|---|
| Agent | `polyglot-test-planner` |
| Input | `artifacts/step02/refined-spec.md`, `refined-spec.json` |
| Output | `artifacts/step03/plan.md`, `plan.json` |
| Schema | `schemas/plan.schema.json` |
| Transport | `call_reasoning_llm_with_hitl` (same multi-turn HITL contract as Step 2) |

**Phase gate:** `phases` array is non-empty. Each phase has `number` and `title`.

---

#### Step 4 -- Test strategy

| Field | Value |
|---|---|
| Agent | `test-manager` |
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
| Agent | `polyglot-test-researcher` |
| Input | `--sut` (local path or git URL) |
| Output | `artifacts/step06/research.md`, `research.json` |
| Schema | `schemas/research.schema.json` |

**Procedure:**
1. Clone `--sut` to `.worca-t/<run-id>/sut/`.
2. Pass the `--sut` directory to `polyglot-test-researcher`.
3. The agent discovers the test automation stack using 3-signal detection:
   dependency files + imports + config files.
4. **Critical output**: `detected_stack` determines which polyglot codegen
   path step 7 takes. If `null`, step 7 falls back to HITL.

**Phase gate:** `sections` array is non-empty. If `detected_stack` is
null, involve HITL.

---

#### Step 7 -- TDD codegen

| Field | Value |
|---|---|
| Agent | `ui-test-automation` |
| Input | `artifacts/step04/test-strategy.json`, `artifacts/step06/research.json`, `--sut` path |
| Output | Test source files in `sut/`, `artifacts/step07/tbd-index.json` |
| Schema | `schemas/tbd-index.schema.json` |

**Phase gate:**
- `framework` is a recognized enum value.
- `tests` array is non-empty.
- `violations[]` must be **empty**. Any `xpath`, `hard-wait`, `page-content`,
  or `raw-secret` violation is a **hard rejection** -- return to agent for correction.
- Every test has `tc_refs` linking back to `TC-*` IDs from step 4.

**Human review gate (post-step-7).** After the phase gate passes,
`pipeline.py` invokes `review_step_7_tests` (`src/worca_t/review_gate.py`):
- Renders a lightweight table — one row per test (name, `tc_refs`,
  `file:line`, one-line description extracted from the test's docstring /
  leading comment / Javadoc / Robot `[Documentation]` line) plus a footer
  with totals (tests, support files, tbd locators, violations). Per-locator
  detail is deliberately omitted — that belongs to step 8.
- Prompts: `[a]pprove` / `[e]dit files` / `[q]uit`.
- On `edit`: prints the SUT root, blocks on `Enter`, then re-runs
  `index_tests` + `_filter_index_to_worca` to rewrite `tbd-index.json`
  in place and refresh `record.output_hashes` (so a later `--resume` does
  not treat the manual edits as drift). Re-renders and re-prompts.
- On `quit`: pipeline aborts with exit code 1.
- Auto-approves (no prompt) when stdin is not a TTY or `--no-hitl` is set.

No agent re-invocation here — manual edits flow through to Step 9 because
the JIT runtime (Playwright stacks) and the `polyglot-test-fixer` heal
agent (other stacks) read test bytes from the SUT disk at execution time.

---

#### Step 8 -- Locator resolution (soft-deleted)

| Field | Value |
|---|---|
| Agent | None |
| Input | (n/a) |
| Output | `artifacts/step08/locator-resolution.json` — stub `{ "mode": "soft-deleted", "status": "skipped", "resolutions": [] }` |
| Schema | `schemas/locator-resolution.schema.json` (stub-only payload) |

**Soft-deleted as of the JIT-runtime refactor.** `s08_locator_resolution.py`
is a no-op stub that always writes the stub artifact above and returns
`status: skipped`. Locator resolution moved to Step 9 runtime for every
stack:

- **Playwright stacks (Python, TypeScript, JavaScript, Java):** Step 7
  vendors a per-language JIT runtime (`tests/worca_t_runtime.py` for
  Python+pytest; `worca-t-runtime.js` for TS/JS; `Tbd.java` +
  `WorcaT.java` + `WorcaTResolver.java` for Java). The runtime intercepts
  `tbd("intent")` / `Tbd.of("intent")` sentinels against the live page at
  test time via the tier ladder defined in CLAUDE.md § JIT.
- **Non-Playwright stacks (Selenium, Cypress, Robot, etc.):** Step 9's
  on-failure `polyglot-test-fixer` self-heal flow handles `TBD_LOCATOR`
  markers — either via Playwright MCP observation or a one-off native
  source capture (`driver.page_source` / `cy.document()` / `Get Source`)
  when MCP can't reach the page state.

The step is kept registered so existing `state.json` checkpoints resume
cleanly and `pipeline.py`'s 11-step list doesn't need to renumber.
Removing it (10-step pipeline) is a deferred follow-up; see the
soft-delete docstring in `s08_locator_resolution.py`.

**Phase gate:** stub artifact exists; `status: "skipped"` is always
accepted.

---

### Phase C -- Execution & Reporting

#### Step 9 -- Execute and self-heal

| Field | Value |
|---|---|
| Agents | `polyglot-test-tester` for execution, `polyglot-test-fixer` for self-heal |
| Input | `artifacts/step07/tbd-index.json` (TBDs resolved), `--sut` path, `--parallelism N`, `--headless\|--headed` |
| Output | `artifacts/step09/run-results.json`, screenshots, traces, `bugs/*.md` candidates, `locator-cache.json` (when JIT runtime ran) |
| Schema | `schemas/run-results.schema.json` (+ `schemas/locator-cache.schema.json` for the JIT cache) |

**JIT runtime (Playwright stacks — Python, TypeScript, JavaScript, Java).**
For SUTs whose active module is a Playwright stack (Python+pytest, TS/JS+Playwright Test / Jest / Vitest, Java+JUnit5 / TestNG), Step 7 has vendored a per-language runtime into the SUT. Before launching the test command, Step 9:

- Starts a parent-side `ResolverServer` (TCP loopback, per-run shared secret) and exports `WORCA_T_RESOLVER_PORT` + `WORCA_T_RESOLVER_TOKEN` into the test subprocess env.
- Sets the rest of the `WORCA_T_*` env vars: `WORCA_T_CACHE_DIR`, `WORCA_T_RUN_ID`, `WORCA_T_RESOLVER_MODEL`, `WORCA_T_DEFAULT_TIMEOUT_MS`, optionally `WORCA_T_DEV_LOCATORS` (when `--dev-locators` or env is set), `WORCA_T_NO_LLM_RESOLVE=1` (when CI opts out of LLM spend).
- **Strips `ANTHROPIC_API_KEY` from the subprocess env via `safe_subprocess_env`** — the key stays in the trusted parent process where `ResolverServer` makes the Anthropic API call. Leaked tokens from the SUT cannot exfiltrate the key.

At test runtime, the vendored runtime intercepts sentinels — `tbd("intent")` (Python/TS/JS) or `Tbd.of("intent")` (Java) — and resolves them via the tier ladder defined in CLAUDE.md § JIT:
1. Dev-supplied locator file (`<sut>/.worca-t/dev-locators.json` or `WORCA_T_DEV_LOCATORS`)
2. Runtime cache
3. In-process AOM heuristic (`role + name` exact match, ≥0.9 confidence)
4. ResolverServer over loopback TCP (preferred LLM path; legacy `worca-t resolve` subprocess is the fallback when `WORCA_T_RESOLVER_PORT` is unset)
5. HITL prompt on TTY / `locator-unresolvable` bug-candidate entry for Step 10 on non-TTY

Each returned `Locator` is wrapped in a retry proxy. On `TimeoutError` during an action (click / fill / hover / etc.), the proxy invalidates the failing cache entry, re-resolves via the LLM (skipping dev file + cache + heuristic so a fresh selector is produced from the current page state), and replays the action once. If the retry also fails, the original `TimeoutError` propagates and the standard `polyglot-test-fixer` self-heal flow takes over.

Resolutions are cached to `<workspace>/locator-cache/locator-cache.json`; Step 9 copies it to `artifacts/step09/locator-cache.json` after the run. HITL answers from Tier 5 prompts are merged into `<sut>/.worca-t/dev-locators.json` so the next run's Tier 1 picks them up without re-prompting.

**Non-Playwright stacks (Selenium, Cypress, Robot, etc.):** JIT does not apply; `polyglot-test-fixer` heal mode handles `TBD_LOCATOR` markers on failure as the procedure below describes. `WORCA_T_NO_LLM_RESOLVE=1` disables both the runtime LLM tier AND the heal agent symmetrically — zero LLM spend in CI.

**Procedure (all stacks):**
1. Invoke `polyglot-test-tester` to run the test command. The command comes from
   `research.json.commands.test`, passed by `s09_execute.py:_detected_command()`.
   The tester does NOT self-discover the command from project files (that path is
   a fallback only when `research.json` has no `commands.test`).
2. Collect results, screenshots (on failure), and traces.
3. If tests fail due to locator drift, invoke `polyglot-test-fixer`
   to self-heal: patch POM/page-object files with current selectors
   using the Playwright MCP. **Playwright stacks**: the JIT runtime's cache-invalidate-and-re-resolve path runs first; self-heal only fires for failures the runtime couldn't resolve. **Non-Playwright stacks**: heal mode is the only resolution path.
4. Re-run healed tests. Record heal attempts in `self_heal` fields.
5. Write failed-test candidates to `bugs/*.md` for step 10.

**Phase gate:** `results` array is non-empty. Every result has a valid
`status` (passed|failed|skipped|error). Screenshots attached for every
failed test.

---

#### Step 10 -- Bug classification

| Field | Value |
|---|---|
| Agent | `bug-report-classifier` |
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
Step 6  --> research.md/json --------------> Steps 7, 9
Step 7  --> tbd-index.json ----------------> Step 9
         -> test source files in sut/
         -> vendored JIT runtime in sut/    (Playwright stacks only)
Step 8  --> locator-resolution.json (stub, soft-deleted)
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
| No XPath (self-heal) | Step 9 | Any XPath selector introduced by `polyglot-test-fixer` in heal mode (`By.XPATH`, `xpath=`, `//`, `getByXPath(`) is rejected; the heal patch is reverted and the test stays `status: failed`. |
| No hard waits | Step 7 | `hard-wait` in `violations[]` is a hard rejection |
| No `page.content()` | Steps 8, 9 | `page-content` in `violations[]` is a hard rejection |
| No raw secrets | Step 7 | `raw-secret` in `violations[]` is a hard rejection |
| TC traceability | Step 7 | Every test must have `tc_refs` linking to `TC-*` IDs |
| REQ traceability | Steps 2, 10 | Every requirement links to a `REQ-*` ID. Every bug whose `test_id` resolves to a known TC links to that TC's `requirement_id`; orphan failures (test_id not in strategy) may omit it but must include `rationale: "orphan failure"`. |
| Self-heal scope | Step 9 | Locators ONLY -- never assertions, never business logic |
| Schema-first | Every step | Every JSON artifact validated via `schemas.py` before hand-off |
| AOM snapshot only | Steps 8, 9 | Generated test code: AOM only — `page.content()` / raw page-source dumps are forbidden. Step 8a runtime: AOM via `browser_snapshot` is the default; raw-DOM via `browser_evaluate(... outerHTML)` is permitted ONLY when the target element is missing from the AOM, non-semantic, or screen-reader-hidden, AND the fallback is annotated per-item with `snapshot_source="raw_dom_fallback"` + a `fallback_reason`. Unjustified raw captures are logged as advisory violations. |

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
1. Set `finished_at` in `state.json` and persist via `save_state()`.
2. Log a summary line: total steps completed, total duration, pass rate.
3. Success = all 11 steps `completed` (or `skipped` for step 5 / step 8).
   Any `failed` step → run is partial.
