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
1. If `--spec` starts with `jira:`, invoke `jira-to-ai-spec` with the
   Atlassian MCP to extract ticket data. Output both `jira-spec.md`
   (raw extraction) and `spec.md` (formatted for pipeline).
2. If `--spec` is a local file, copy it to `artifacts/step01/spec.md`.

**Phase gate:** `spec.md` exists and is non-empty.

---

#### Step 2 -- Refine specification

| Field | Value |
|---|---|
| Agent | `refine-spec` |
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
| Agent | `polyglot-test-planner` |
| Input | `artifacts/step02/refined-spec.md`, `refined-spec.json` |
| Output | `artifacts/step03/plan.md`, `plan.json` |
| Schema | `schemas/plan.schema.json` |

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

---

#### Step 8 -- Locator discovery + DOM-truth audit (two agents)

| Field | Value |
|---|---|
| Agents | **8a** `playwright-tester` (live DOM, resolve TBDs) + **8b** `polyglot-test-fixer` in audit-only mode (compare expected vs actual, no edits) |
| Input | `artifacts/step07/tbd-index.json`, `artifacts/step06/research.json`, `SUT_BASE_URL` env var |
| Output | `artifacts/step08/locator-resolution.json` + `artifacts/step08/tbd-index.json` (re-indexed) + `artifacts/step08/dom-comparison.json` (8b auditor verdicts) |
| Schemas | `schemas/locator-resolution.schema.json`, `schemas/dom-comparison.schema.json` |

**JIT short-circuit (Python + pytest + Playwright only).** When the active module's framework is `pytest` or `playwright-py` AND `tests/worca_t_runtime.py` exists in the SUT (vendored by Step 7), Step 8 writes a minimal `artifacts/step08/locator-resolution.json` with `mode: "jit"` and returns `status: skipped`. Resolution happens at Step 9 runtime via the vendored pytest plugin — it intercepts `tbd("…")` sentinels against the live Playwright page (already authenticated, already on the right URL because the test's own POMs navigated there). For all other frameworks, the procedure below runs as written.

**Procedure — 8a (playwright-tester) [non-JIT frameworks]:**
1. Navigate the live application via the Playwright MCP. **AOM-first snapshot policy:**
   - Every distinct URL opened in the session → capture AOM via
     `browser_snapshot`, persist to `./page-snapshot-NN.json` (NN
     zero-padded, starting at 01) BEFORE further work with that page.
   - Raw-DOM is a scoped fallback only — permitted when the target element
     is missing from the AOM, non-semantic (div/span without ARIA), or
     hidden from screen readers. When triggered: capture via
     `browser_evaluate(() => document.documentElement.outerHTML)`, persist
     to `./page-snapshot-NN-raw.html`, AND record
     `snapshot_source: "raw_dom_fallback"` + a `fallback_reason`
     (`not_in_aom` | `non_semantic` | `aria_hidden` | <free text>) on every
     resolution item that cited the raw capture.
   - Within a captured page, element-scoped queries only (never re-capture
     the whole page).
2. For each TBD marker, read the `description` field from
   `tbd-index.json` (semantic intent captured by the codegen agent's
   `TBD_INTENT:` comment) and discover the real locator using the priority
   chain: `id > data-testid > role > label > text > placeholder > css`.
   **Never XPath.** When `description` is missing on legacy runs, infer
   intent from the constant name + surrounding test code.
3. Patch the test source files via the orchestrator's anchored line-targeted
   patcher: the patcher requires a `<CONST_NAME> = <tbd_token>` assignment
   line within ±10 lines of the agent-supplied `line` and refuses to
   fall back to global first-occurrence replacement (which previously
   scrambled assignments when `TBD_LOCATOR` appeared in a comment).
   Items that don't match an anchor are marked `applied: false` with a
   drift `skip_reason` and surfaced via HITL / 8b. Record each resolution
   in `locator-resolution.json` with `applied`/`skip_reason` per item.
   Honest skips (`strategy: null` + diagnostic `skip_reason`) are
   preserved — the pipeline no longer overwrites them.
4. **HITL escalation for unresolvable TBDs.** When the agent sets
   `applied: false` because an element genuinely cannot be located, it
   appends a `[CLARIFICATION NEEDED: <CONST> @ <file>:<line>]` block to
   `./clarifications.md`. The orchestrator extracts those blocks (via
   `extract_questions` in `src/worca_t/hitl.py`), prompts the user once
   per block on a TTY, and splices the answer into
   `./locator-resolution.json`:
     - User pastes a selector → orchestrator validates (XPath is rejected),
       infers strategy from shape, sets `applied: true, source: "hitl"`.
     - User confirms spec gap → orchestrator sets `applied: false,
       comparison_verdict: "ghost", source: "hitl"`; the gate's
       `excused_count` then absorbs it.
     - Non-TTY / `--no-hitl` runs skip the prompt entirely; items stay
       `applied: false` and fall through to the apply-rate gate exactly
       as today (no CI regression).

**Procedure — 8b (polyglot-test-fixer in DOM-COMPARISON-AUDIT mode):**
- Reads `./tbd-index.json`, `./locator-resolution.json`, every
  `./page-snapshot-*` persisted by 8a, plus the codegen-produced test
  files inside the SUT (via `add_dirs=[sut_root]`).
- Emits `./dom-comparison.json` with one verdict per TBD constant:
  `matched` / `ghost` / `duplicate` / `low_confidence` / `unevaluated`.
- **Forbidden:** SUT edits, Playwright MCP calls, new snapshots.
- The pipeline stamps each verdict into `locator-resolution.json` (via
  `comparison_verdict`) and forces `applied: false` + clears
  `strategy`/`replacement` on `ghost`/`duplicate` items.

**Phase gate (post-audit):**
- `resolutions` array is non-empty.
- Every applied `strategy` value is in the allowed enum (no `xpath`); `null`
  is allowed when `applied: false`.
- Apply rate is now `applied / (applied + skipped - excused)`, where
  `excused` counts items marked `ghost` or `duplicate` by 8b. Threshold
  remains 90%. Honest skips for non-existent or duplicate elements no longer
  penalise the run.
- `remaining_tbd > 0` with apply rate ≥ 90% → `warned` (Step 9 may pass a
  subset of tests; unresolved markers stay as bug candidates).
- Snapshot-policy violations (8a persisted a `*-raw.html` capture without
  a corresponding `fallback_reason` recorded on any resolution item, or
  produced zero AOM captures) are logged as
  `step08.snapshot_policy_violation` warnings only — they do not fail
  the step.

---

### Phase C -- Execution & Reporting

#### Step 9 -- Execute and self-heal

| Field | Value |
|---|---|
| Agents | `polyglot-test-tester` for execution, `polyglot-test-fixer` for self-heal |
| Input | `artifacts/step07/tbd-index.json` (TBDs resolved), `--sut` path, `--parallelism N`, `--headless\|--headed` |
| Output | `artifacts/step09/run-results.json`, screenshots, traces, `bugs/*.md` candidates, `locator-cache.json` (when JIT runtime ran) |
| Schema | `schemas/run-results.schema.json` (+ `schemas/locator-cache.schema.json` for the JIT cache) |

**JIT runtime (Python + pytest + Playwright only).** When Step 8 returned `mode: jit`, the vendored `tests/worca_t_runtime.py` plugin runs alongside the test command. It:
- Sets `WORCA_T_*` env vars on the test subprocess: `WORCA_T_CACHE_DIR`, `WORCA_T_RUN_ID`, `ANTHROPIC_API_KEY` (explicitly re-exported through `safe_subprocess_env`'s secret allowlist), `WORCA_T_RESOLVER_MODEL`, `WORCA_T_DEFAULT_TIMEOUT_MS`, and optionally `WORCA_T_DEV_LOCATORS` (when the user passed `--dev-locators` or it's already in env).
- Monkey-patches `Page.locator` to detect `tbd("…")` sentinels. On sentinel access: consults dev file → runtime cache → `worca-t resolve` subprocess (single Anthropic SDK call, `temperature=0`, prefilled JSON), returning a real `Locator` the test can use. **No Playwright MCP is involved in this path** — the resolver consumes the live page's AOM snapshot captured in-process and makes a direct LLM call.
- Inflates `page.set_default_timeout` to 60s (configurable via `WORCA_T_DEFAULT_TIMEOUT_MS`; opt-out via `WORCA_T_INFLATE_TIMEOUTS=0`) so resolver latency doesn't bump into per-action timers.
- Wraps each returned `Locator` in a `_RetryingLocator` proxy. When an action method (`click` / `fill` / `hover` / `text_content` / `wait_for` / etc.) raises `TimeoutError`, the proxy invalidates the failing entry from the cache, re-resolves via the LLM (skipping the dev file + cache so a fresh selector is produced from the current page state), and replays the same action once. This is the **dev-locator-staleness and DOM-drift safety net** — stale dev selectors auto-correct inline before falling through to the heavier polyglot-test-fixer self-heal agent. If the retry also fails, the original `TimeoutError` propagates and the standard self-heal flow takes over.
- Caches resolutions to `<workspace>/locator-cache/locator-cache.json`; Step 9 copies it to `artifacts/step09/locator-cache.json` after the run.

**Procedure (non-JIT and JIT alike):**
1. Invoke `polyglot-test-tester` to run the test command. The command comes from
   `research.json.commands.test`, passed by `s09_execute.py:_detected_command()`.
   The tester does NOT self-discover the command from project files (that path is
   a fallback only when `research.json` has no `commands.test`).
2. Collect results, screenshots (on failure), and traces.
3. If tests fail due to locator drift, invoke `polyglot-test-fixer`
   to self-heal: patch POM/page-object files with current selectors
   using the Playwright MCP (see [`.mcp.json`](../.mcp.json) for the
   current server list). **JIT note:** under JIT, locator drift triggers the runtime plugin's cache-invalidate-and-re-resolve path BEFORE this self-heal flow runs; self-heal only fires for failures the JIT runtime couldn't resolve.
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
Step 6  --> research.md/json --------------> Steps 7, 8, 9
Step 7  --> tbd-index.json -----------> Steps 8, 9
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
1. Set `finished_at` in `state.json`.
2. Persist final state via `save_state()`.
3. Log a summary line: total steps completed, total duration, pass rate.
4. If the run completed successfully, all 11 steps show `completed`
   (or `skipped` for step 5). If any step is `failed`, the run is partial.
