# QA Orchestrator - Step-by-Step Instructions

> **Documentation only.** This file describes what `src/qtea/pipeline.py` does — no Python code loads it at runtime. It is the canonical human/AI-facing specification of the orchestration contract; keep it in sync when the contract changes.

> Read `CLAUDE.md` first -- it is the source of truth for pipeline structure,
> agent-model map, MCP servers, and non-negotiable rules.
>
> **Single sources of truth** (this file points at them; never duplicate):
> - Agent → model map: [`src/qtea/agent_models.yaml`](../src/qtea/agent_models.yaml)
> - MCP servers: [`.mcp.json`](../.mcp.json)
> - Per-step timeouts: [`src/qtea/config.py`](../src/qtea/config.py)
> - Schemas: [`schemas/*.schema.json`](../schemas/)
>
> Model names and timeout numbers are deliberately omitted from the step
> tables below to prevent drift. Look them up in the files above.

## Architecture at a Glance

Two layers cooperate to run the pipeline:

- **`pipeline.py`** — deterministic orchestrator. Drives steps in order, loads/saves
  checkpoints via `checkpoints.py`, validates schemas via `schemas.py`. No reasoning.
- **Two LLM transports:**
  - **`claude_runner.py`** (`run_agent`) — agent executor via Claude Agent SDK. Multi-turn with tool access (Read/Write/Grep/Glob). Used by steps 6, 9, and step 8's violation-fix phase. Context grows with each turn.
  - **`llm/reasoning.py`** (`call_reasoning_llm`) — direct Anthropic SDK. Single API call, inputs inlined into prompt. Used by steps 2-4, 7, 10, and step 8's codegen phases (A/B). Bounded context, no growth.
- **This agent (QA Orchestrator)** — semantic reasoning only. Decides what inputs to
  pass, interprets failures, and hands the persistent-failure path off to the
  auto-firing fix-proposal chain.

**The boundary is clean: `pipeline.py` never reasons. This agent never checkpoints.**

---

## 1. Initialize the run

1. Generate a `run_id` (ISO-8601 timestamp or UUID).
2. Create the workspace directory tree:
   ```
   .qtea/<run-id>/
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
   link) and put it on the qtea isolation branch before any step runs.
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
2. Look up the model for that agent in `src/qtea/agent_models.yaml`.
3. Invoke the agent via `claude_runner.py` (`run_agent()`), which spawns the
   `claude` CLI with:
   - The model resolved from the agent-model map.
   - The curated input bundle (files listed in section 3 below).
   - MCP servers as configured in [`.mcp.json`](../.mcp.json).
   - A per-step timeout cap from [`config.py`](../src/qtea/config.py)
     (`step_timeout(N)`; the global cap is `MAX_STEP_TIMEOUT_S`, overridable via `QTEA_MAX_STEP_TIMEOUT_S`).
4. Stream agent progress. Write each event to `run.log.jsonl` with fields:
   `run_id`, `step`, `agent`, `attempt`, `correlation_id`, `timestamp`.
5. Mask secrets in all log output: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`,
   `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.

### 2.3 Validate

1. Confirm every expected output artifact exists (see section 3).
2. Validate each JSON output against its schema in `schemas/` via `schemas.py`.
3. For step 8 (codegen), additionally scan `tbd-index.json` for `violations[]`.
   Each violation carries `severity: "error" | "warning"`. Any violation with
   `severity == "error"` is a hard failure -- reject the output. Rules currently
   shipping as errors: `xpath`, `hard-wait`, `page-content`, `raw-secret`,
   `empty-handler`. `warning`-severity rules flow to `violations.log` for
   audit but do not fail the step (advisory mode for rules being baselined).
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
  --> --debug set? co-run debug.agent.md for attempt-1 RCA (observability only)
  --> Attempt 2: re-run same inputs
        --> Attempt 2 fails (retry exhaustion):
              1. debug.agent.md runs — writes step-NN-attempt2-debug-rca.md
              2. --no-fix set? SKIP the rest and abort with the RCA.
              3. otherwise auto-fire the fix-proposal chain:
                   critical-thinking (given the debug RCA) -> fix-strategy.md
                   principal-software-engineer (given RCA + strategy) -> fix-proposal.md
              4. mark step "failed", surface aggregated step-NN-rca.md +
                 step-NN-fix-proposal.md, abort pipeline.
```

Nothing auto-edits — `fix-proposal.md` is a hand-off to the operator.
Models for `debug`, `critical-thinking`, and `principal-software-engineer`
live in `src/qtea/agent_models.yaml`.

1. On first failure, increment `attempts` to 2, set `status: "in_progress"`.
2. Re-invoke the same agent with the same inputs. If `--debug` was set, an
   attempt-1 debug RCA was already written.
3. If attempt 2 also fails, `debug.agent.md` runs unconditionally and its RCA
   is stashed on `ctx.extras[f"step{n}_rca_path"]`.
4. Unless `--no-fix` is set, `_run_fix_proposal` auto-fires: promotes the debug
   RCA into the aggregated `<ws>/debug/step-NN-rca.md` slot, calls
   `critical-thinking` to produce `fix-strategy.md`, then calls
   `principal-software-engineer` to produce `<ws>/debug/step-NN-fix-proposal.md`.
5. Mark step `"failed"` and abort the pipeline (or `"warned"` if attempt 2
   succeeded — no fix chain fires in that case).

---

## 3. Step input/output contracts

### Phase A — Requirements & Planning

#### Step 1 -- Intake

| Field | Value |
|---|---|
| Agent | `ticket-to-ai-spec`, or pure file copy |
| Input | `--spec` flag (Jira ticket, Azure DevOps work item, or local markdown path) |
| Output | `artifacts/step01/spec.md` (required), `jira-spec.md` (provenance stub) |
| Schema | n/a (non-empty file check only) |

**Procedure:**
1. If `--spec` starts with `jira:` or is a full `https://.../browse/KEY` URL,
   fetch the issue via direct Jira REST (`qtea.jira_client.fetch_issue`),
   slim the payload, and invoke `ticket-to-ai-spec` with the JSON inlined
   under the `jira-issue.json` header.
2. If `--spec` starts with `ado:` or is a full
   `https://dev.azure.com/{org}/{project}/_workitems/edit/{id}` URL, fetch
   the work item via Azure DevOps REST (`qtea.ado_client.fetch_work_item`),
   slim the payload, and invoke `ticket-to-ai-spec` with the JSON inlined
   under the `ado-workitem.json` header.
3. If `--spec` is a local file, copy its content verbatim to
   `artifacts/step01/spec.md` (no LLM call at step 1).
4. If `--spec` is a non-ticket URL, download its body and write it verbatim to
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
| Transport | `qtea.llm.reasoning.call_reasoning_llm_with_hitl` (direct Anthropic SDK, multi-turn HITL) |

**HITL loop.** The transport extracts `[CLARIFICATION NEEDED]` tags,
Blockers table rows, and Open Questions bullets from the agent's output
via `qtea.hitl.extract_questions`. Skipped items are deduped across
iterations (see `qtea.hitl._dedup`) so the user is never re-prompted
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
1. Clone `--sut` to `.qtea/<run-id>/sut/`.
2. Pass the `--sut` directory to `polyglot-test-researcher`.
3. The agent discovers the test automation stack using 3-signal detection:
   dependency files + imports + config files.
4. **Critical output**: `detected_stack` determines which polyglot codegen
   path step 8 takes. If `null`, step 8 falls back to HITL.

**Phase gate:** `sections` array is non-empty. If `detected_stack` is
null, involve HITL.

---

#### Step 7 -- Test Architect

| Field | Value |
|---|---|
| Agent | `test-architect` |
| Input | `artifacts/step04/test-strategy.md`, `artifacts/step06/sut_inventory.json`, `artifacts/step06/research.md` |
| Output | `artifacts/step07/code-modification-plan.json`, `code-modification-plan.md` |
| Schema | `schemas/code-modification-plan.schema.json` |

**Procedure.** Read `test-strategy.md` + `sut_inventory.json`. For each test
case, emit explicit structural decisions: `test_file_target`, per-test
`test_functions[]` (with markers + uses_fixtures), fixtures classified as
`reuse` (with `from: "<file>:<symbol>"`) or `create` (with `at:`, `yields`,
`scope`), page_objects classified the same plus optional `missing_methods[]`
with signatures, and locators classified as `reuse` or `create_tbd` (with an
`intent` string ≤120 chars). The plan is structural — no method bodies, no
assertion text, no selector strings (those belong to Step 8 and the Step 9
JIT resolver respectively).

**Phase gate.**
- Plan validates against `schemas/code-modification-plan.schema.json`.
- Every `reuse` reference's `from` field points to a file:symbol that exists
  in `sut_inventory.json`.
- Every `create` / `create_tbd` `at` target lands in an inventory-approved
  directory (matches `test_directory_layout` / `src_directory_layout`).
- Every `missing_methods` entry has a `signature`.
- Every `create_tbd` locator has an `intent` of ≤120 chars.
- Marker names match `qtea_<phase>` convention exactly.
- **Auth chaining:** when `auth_flow.fixture_entry` exists, any `source=create`
  fixture whose `yields` is a non-primitive type must include the auth fixture
  name in `depends_on`. Prevents generated fixtures from bypassing authentication.

**Human review gate (post-step-7).** After the phase gate passes,
`pipeline.py` invokes `review_step_7_plan` (`src/qtea/review_gate.py`):
- Renders a table per test case: target file, function names + markers,
  fixtures (reuse vs create), page objects (reuse vs create + count of
  missing methods), locators (reuse vs create_tbd with intent). Footer
  shows totals + reuse-vs-create breakdown per category.
- Prompts: `[a]pprove` / `[e]dit plan` / `[q]uit`.
- On `edit`: prints the plan JSON path, blocks on `Enter`, then re-validates
  the (potentially edited) plan against the schema and re-renders.
  `record.output_hashes` is refreshed so a later `--resume` does not treat
  the manual edits as drift.
- On `quit`: pipeline aborts with exit code 1.
- Auto-approves (no prompt) when stdin is not a TTY or `--no-hitl` is set.

---

#### Step 8 -- TDD codegen (phased)

| Field | Value |
|---|---|
| Sub-agents | `codegen-pom-extender` (Phase A), `codegen-test-writer` (Phase B), `codegen-violation-fixer` (Phase C violation fix only) |
| Shared rules | `agents/codegen-rules.md` — canonical quality rules (locator priority, no hard waits, TBD conventions, assertion fidelity, naming). Injected as `inputs["codegen-rules.md"]` into Phase A/B reasoning calls; Phase C agent reads it via tools. |
| Transport | `call_reasoning_llm` (Phases A/B — single API call per invocation, no multi-turn), `run_agent` (Phase C only) |
| Input | `artifacts/step07/code-modification-plan.json` (authoritative), `artifacts/step04/test-strategy.md` (assertion values), `artifacts/step06/sut_inventory.json` (style mimicry + dedup), `--sut` path |
| Output | Test source files in `sut/`, `artifacts/step08/tbd-index.json`, `generated-files.json` |
| Schema | `schemas/tbd-index.schema.json` |

**Procedure (phased).** Python decomposes the plan into three phases using
`call_reasoning_llm` (bounded context, ~5-10K tokens per call):
**A** — extend POMs (`codegen-pom-extender`, parallel), declare TBD locators
(pure Python), create fixtures (with auth-chain context injected when
`depends_on` is set), create helpers (`source=create` helper entries from the
plan);
**B** — generate test files (`codegen-test-writer`, one call per `test_file_target`, strategy filtered to relevant TCs);
**C** — quality gate: index tests, fix violations via `run_agent`/`codegen-violation-fixer` if found, commit.

Step 8 also vendors the per-language JIT runtime into the SUT (Playwright
stacks only — Python/TS/JS/Java) BEFORE any reasoning call, so that
generated imports resolve and Step 9 can intercept sentinels at test runtime.

**Phase gate:**
- `framework` is a recognized enum value.
- `tests` array is non-empty.
- `violations[]` must be **empty**. Any `xpath`, `hard-wait`, `page-content`,
  or `raw-secret` violation is a **hard rejection** -- return to agent for correction.
- Every test has `tc_refs` linking back to `TC-*` IDs from step 4.

Locator resolution happens at Step 9 runtime via the vendored JIT runtime
(Playwright stacks — Python, TS, JS, Java) or via the on-failure
`polyglot-test-fixer` heal flow (Selenium, Cypress, Robot, etc.). There is
no separate locator-resolution step.

---

### Phase C -- Execution & Reporting

#### Step 9 -- Execute and self-heal

| Field | Value |
|---|---|
| Agents | `polyglot-test-fixer` for self-heal (test execution itself is pure code via `qtea.test_runner.run_tests`) |
| Input | `artifacts/step08/tbd-index.json` (TBDs resolved), `--sut` path, `--parallelism N`, `--headless\|--headed` |
| Output | `artifacts/step09/run-results.json`, screenshots, traces, `bugs/*.md` candidates, `locator-cache.json` (when JIT runtime ran) |
| Schema | `schemas/run-results.schema.json` (+ `schemas/locator-cache.schema.json` for the JIT cache) |

**JIT runtime (Playwright stacks — Python, TypeScript, JavaScript, Java).**
For SUTs whose active module is a Playwright stack (Python+pytest, TS/JS+Playwright Test / Jest / Vitest, Java+JUnit5 / TestNG), Step 8 has vendored a per-language runtime into the SUT. Before launching the test command, Step 9:

- Starts a parent-side `ResolverServer` (TCP loopback, per-run shared secret) and exports `QTEA_RESOLVER_PORT` + `QTEA_RESOLVER_TOKEN` into the test subprocess env.
- Sets the rest of the `QTEA_*` env vars: `QTEA_CACHE_DIR`, `QTEA_RUN_ID`, `QTEA_RESOLVER_MODEL`, `QTEA_DEFAULT_TIMEOUT_MS`, optionally `QTEA_DEV_LOCATORS` (when `--dev-locators` or env is set), `QTEA_NO_LLM_RESOLVE=1` (when CI opts out of LLM spend).
- **Strips `ANTHROPIC_API_KEY` from the subprocess env via `safe_subprocess_env`** — the key stays in the trusted parent process where `ResolverServer` makes the Anthropic API call. Leaked tokens from the SUT cannot exfiltrate the key.

At test runtime, the vendored runtime intercepts sentinels — `tbd("intent")` (Python/TS/JS) or `Tbd.of("intent")` (Java) — and resolves them via the tier ladder defined in CLAUDE.md § JIT:
1. Dev-supplied locator file (`<sut>/.qtea/dev-locators.json` or `QTEA_DEV_LOCATORS`)
2. Runtime cache
3. In-process AOM heuristic (`role + name` exact match, ≥0.9 confidence)
4. ResolverServer over loopback TCP (preferred LLM path; legacy `qtea resolve` subprocess is the fallback when `QTEA_RESOLVER_PORT` is unset)
5. HITL prompt on TTY / `locator-unresolvable` bug-candidate entry for Step 10 on non-TTY

Each returned `Locator` is wrapped in a retry proxy. On `TimeoutError`, the proxy walks any in-bundle fallback candidates first (zero LLM cost), then invalidates the failing cache entry, re-resolves via the LLM (skipping dev file + cache + heuristic), and replays the action once. If the retry also fails, the `TimeoutError` propagates and `polyglot-test-fixer` self-heal takes over. **Dev-pool quarantine:** when the stale resolution came from tier-1b, the proxy marks the cache entry `quarantined: true`, appends to `<workspace>/locator-cache/dev-pool-quarantine.jsonl`, re-resolves with `skip_pool=True`, and stores the LLM fallback under `_shadow:<key>` — preserving the dev-supplied selector. Step 9 emits a `dev-locator-drifted` bug-candidate per drift.

Resolutions are cached to `<workspace>/locator-cache/locator-cache.json`; Step 9 copies it to `artifacts/step09/locator-cache.json`. HITL answers from Tier 5 prompts merge into `<sut>/.qtea/dev-locators.json`. **TBD promotion gate:** after each attempt Step 9 freezes cache entries into SUT source via `_promote_resolved_tbds`, but ONLY when (a) `passing_witnesses` is non-empty (the runtime's teardown hook records nodeids of passing tests) AND (b) `validate_selector_payload` accepts. CSS payloads emit quoted strings; structured payloads (`role`/`text`/`label`/`placeholder`/`test_id`) emit `role_locator(...)` / `text_locator(...)` / etc. helper calls (the promoter extends the runtime import line). Blocked entries surface as `promotion-blocked` bug-candidates alongside `locator-unresolvable` and `dev-locator-drifted`.

**Non-Playwright stacks (Selenium, Cypress, Robot, etc.):** JIT does not apply; `polyglot-test-fixer` heal mode handles `TBD_LOCATOR` markers on failure as the procedure below describes. `QTEA_NO_LLM_RESOLVE=1` disables both the runtime LLM tier AND the heal agent symmetrically — zero LLM spend in CI.

**Procedure (all stacks):**
1. Run the test command via `qtea.test_runner.run_tests` (pure code, no
   agent). The command comes from `research.json.commands.test`, passed by
   `s09_execute.py:_detected_command()`. Falls back to a per-framework
   default only when `research.json` has no `commands.test`.
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
Step 4  --> test-strategy.md/json ---------> Steps 7, 8, 9, 10, 11
Step 5  --> xray-mapping.json            (stand-alone; no downstream dependency)
Step 6  --> research.md/json --------------> Steps 7, 8, 9
         -> sut_inventory.json ------------> Steps 7, 8
Step 7  --> code-modification-plan.json --> Step 8
         -> code-modification-plan.md       (review-gate surface)
Step 8  --> tbd-index.json ----------------> Step 9
         -> test source files in sut/
         -> vendored JIT runtime in sut/    (Playwright stacks only)
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
      --> code-modification-plan entry (step 7: test-architect maps TC → code)
          --> test files with tc_refs  (step 8: codegen-violation-fixer transpiles)
              --> run-results per test  (step 9: execute + self-heal)
                  --> BUG-<run-id>-<seq> with requirement_id  (step 10)
                      --> report links all layers  (step 11)
```

If a requirement ID is missing or broken at any step, flag it and halt.

---

## 6. Quality gates

These are non-negotiable. Violations trigger step rejection and the retry/fix cycle.

| Gate | Enforced at | Constraint |
|---|---|---|
| Locator priority | Step 9 agent | `id > data-testid > role > label > text > placeholder > css` |
| No XPath | Steps 8, 9 | `xpath` in `violations[]` is a hard rejection |
| No XPath (self-heal) | Step 9 | Any XPath selector introduced by `polyglot-test-fixer` in heal mode (`By.XPATH`, `xpath=`, `//`, `getByXPath(`) is rejected; the heal patch is reverted and the test stays `status: failed`. |
| No hard waits | Step 8 | `hard-wait` in `violations[]` is a hard rejection |
| No `page.content()` | Steps 9, 10 | `page-content` in `violations[]` is a hard rejection |
| No raw secrets | Step 8 | `raw-secret` in `violations[]` is a hard rejection |
| No empty exception handlers | Step 8 | `empty-handler` in `violations[]` is a hard rejection. Mirrors the Step 9 heal-gate `_count_empty_handlers`; promoted from heal-only to codegen-side so write-time defects don't ship. |
| Framework/command consistency | Step 8 pre-flight | `research.json.detected_stack` must be consistent with `research.json.commands.test` argv head; mismatch is a hard rejection before any agent runs. |
| Semantic preflight | Step 8.5 | `preflight-error` rules: generated Python tests must `ast.parse`; plan fixture-graph must be acyclic with all `depends_on` resolving; every `LocatorClass.CONSTANT` referenced in tests must exist in the locator file. |
| Assertion coverage | Step 8 | `zero-assertions` (Python+pytest): every `def test_*` must contain ≥1 assertion (`assert`, `expect(...)`, `pytest.raises`, `should`) unless tagged `@pytest.mark.qtea_setup`. Hard rejection. |
| Href vs. navigation | Step 8.5 | `href-when-navigates`: when the strategy's Expected Result says navigates/leads/points/redirects to a URL, generated tests must use click-then-`to_have_url(...)` not `to_have_attribute("href", ...)`. Hard rejection. |
| Bare-assert advisory | Step 8 | `bare-assert-where-expect-available` (warning): `assert loc.text_content() == ...`, `assert loc.is_visible()`, `assert page.url == ...` surface as advisory warnings; promotion to hard-reject pending FP baseline. |
| TC traceability | Step 8 | Every test must have `tc_refs` linking to `TC-*` IDs |
| REQ traceability | Steps 2, 11 | Every requirement links to a `REQ-*` ID. Every bug whose `test_id` resolves to a known TC links to that TC's `requirement_id`; orphan failures (test_id not in strategy) may omit it but must include `rationale: "orphan failure"`. |
| Self-heal scope | Step 9 | Locators ONLY -- never assertions, never business logic |
| Plan reuse-vs-create | Step 7 | Every `reuse` reference points to an inventory entry; every `create`/`create_tbd` target is in an inventory-approved directory |
| Auth-chaining | Step 7 | `source=create` fixtures yielding non-primitive types must declare `depends_on` with the auth fixture from `auth_flow.fixture_entry` |
| Schema-first | Every step | Every JSON artifact validated via `schemas.py` before hand-off |
| AOM snapshot only | Steps 9, 10 | Generated test code: AOM only — `page.content()` / raw page-source dumps are forbidden. Step 9 runtime: AOM via `browser_snapshot` is the default; raw-DOM via `browser_evaluate(... outerHTML)` is permitted ONLY when the target element is missing from the AOM, non-semantic, or screen-reader-hidden, AND the fallback is annotated per-item with `snapshot_source="raw_dom_fallback"` + a `fallback_reason`. Unjustified raw captures are logged as advisory violations. |

---

## 7. Observability

Every `run.log.jsonl` entry: `run_id`, `step` (1-11), `agent`, `attempt` (1-2), `correlation_id`, `timestamp` (ISO-8601), `level`, `message`. Secrets are always masked.

---

## 8. Finalization

After step 11 completes (or after abort):
1. Set `finished_at` in `state.json` and persist via `save_state()`.
2. Log a summary line: total steps completed, total duration, pass rate.
3. Success = all 11 steps `completed` (or `skipped` for step 5 / step 9).
   Any `failed` step → run is partial.

---

## 9. Environment Variables Reference

All variables are optional unless marked **required**. Defaults are applied in `src/qtea/config.py` unless noted.

### Authentication

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** (unless `ANTHROPIC_AUTH_TOKEN` or Vertex is used). Masked in all logs. |
| `ANTHROPIC_AUTH_TOKEN` | — | Bearer-token alternative; takes priority over `ANTHROPIC_API_KEY` when set. |
| `ANTHROPIC_BASE_URL` | Anthropic default | Override the Anthropic API base URL (e.g. BMF/proxy relay endpoint). |
| `ANTHROPIC_CUSTOM_HEADERS` | — | Semicolon-separated `Key: Value` headers injected into every API call. Prompt caching is auto-enabled when this contains the BMF sticky-session header (`x-bmf-sticky-session-instance`). |
| `ANTHROPIC_VERTEX_BASE_URL` | — | Vertex AI proxy endpoint. Set alongside `CLAUDE_CODE_USE_VERTEX=1`. |
| `ANTHROPIC_VERTEX_PROJECT_ID` | — | GCP project for Vertex AI routing. |
| `CLOUD_ML_REGION` | `us-east5` | GCP region for Vertex AI. |
| `CLAUDE_CODE_USE_VERTEX` | `0` | Set `1` to route `claude` CLI calls through Vertex AI. |
| `DISABLE_PROMPT_CACHING` | — | Set `1` to globally disable prompt caching; forwarded to every `claude` subprocess. |

### Jira / Xray (Steps 1, 5)

| Variable | Default | Description |
|---|---|---|
| `JIRA_BASE_URL` | — | Jira instance URL (e.g. `https://company.atlassian.net`). Required for `jira:` specs. |
| `JIRA_EMAIL` | — | Jira Cloud user email for Basic auth. |
| `JIRA_API_TOKEN` | — | Jira Cloud API token. Masked in logs. |
| `JIRA_PAT` | — | Personal Access Token for Jira Server / Data Center (Bearer auth). Masked in logs. |
| `JIRA_AUTH_TYPE` | auto-detect | Override auth scheme: `basic` or `bearer`. Auto-detected from which credential vars are present. |
| `JIRA_PROJECT_KEY` | — | Jira project key used as Xray upload fallback when the test-strategy omits one. |
| `JIRA_XRAY_CLIENT_ID` | — | Xray Cloud OAuth2 client ID. Masked in logs. |
| `JIRA_XRAY_CLIENT_SECRET` | — | Xray Cloud OAuth2 client secret. Masked in logs. |
| `JIRA_XRAY_API_KEY` | — | Xray DC/Server API key (alternative to client ID/secret). Masked in logs. |

### Azure DevOps (Step 1)

| Variable | Default | Description |
|---|---|---|
| `AZDO_ORG` | — | Azure DevOps organisation slug. Required for `ado:` specs. |
| `AZDO_PROJECT` | — | Azure DevOps project name. |
| `AZDO_PAT` | — | Personal Access Token for ADO REST. Masked in logs. |
| `AZDO_VARIABLE_GROUP` | — | Variable group name to pull SUT env vars from during Step 6 research. |

### Proxy

| Variable | Default | Description |
|---|---|---|
| `HTTPS_PROXY` / `https_proxy` | — | Standard HTTPS proxy. Picked up by HTTP clients and by the runtime's Playwright proxy-injection patch. |
| `HTTP_PROXY` / `http_proxy` | — | HTTP proxy for non-TLS traffic. |
| `NO_PROXY` / `no_proxy` | — | Comma-separated hosts to bypass the proxy. |
| `QTEA_PROXY` | — | QTEA-specific proxy override; wins over `HTTPS_PROXY` in the runtime subprocess. |
| `QTEA_DISABLE_PROXY_INJECT` | `0` | Set `1` to disable the runtime's automatic `proxy=` injection into `BrowserType.launch`. |

### Pipeline Configuration

| Variable | Default | Description |
|---|---|---|
| `QTEA_DEFAULT_WORKSPACE` | `~/.qtea` | Root directory for all run workspaces. |
| `QTEA_MAX_STEP_TIMEOUT_S` | `1800` | Per-step hard timeout (seconds). Overrides the `MAX_STEP_TIMEOUT_S` constant. |
| `QTEA_CLAUDE_BIN` | `claude` | Name or path of the `claude` CLI binary to invoke. |
| `QTEA_RESOURCE_ROOT` | (wheel snapshot) | Point at the live repository root so edited Markdown/YAML agent files take effect without reinstalling. Python edits still require reinstall. |
| `SUT_BASE_URL` | — | Web root of the SUT. Used by Steps 6 and 7 live-explore when not inferrable from research. |

### Debug / Fix-Chain Tuning

| Variable | Default | Description |
|---|---|---|
| `QTEA_DEBUG_AGENT_MAX_TURNS` | `40` | Turn budget for the debug RCA agent. |
| `QTEA_DEBUG_AGENT_TIMEOUT_S` | `900` | Wall-clock timeout for the debug agent (seconds). |
| `QTEA_FIX_AGENT_MAX_TURNS` | `25` | Turn budget for the `critical-thinking` fix-strategy agent. |
| `QTEA_FIX_AGENT_TIMEOUT_S` | `600` | Wall-clock timeout for the fix-strategy agent (seconds). |
| `QTEA_AUTOFIX_MAX_TURNS` | `40` | Turn budget for the `principal-software-engineer` fix-proposal agent. |

### Step 7 — Live Explore

| Variable | Default | Description |
|---|---|---|
| `QTEA_LIVE_EXPLORE` | `1` | Set `0` to skip the live browser exploration pass in Step 7. |
| `QTEA_LIVE_EXPLORE_MAX_ROUTES` | unlimited | Cap the number of routes explored per live-explore pass. |
| `QTEA_LIVE_EXPLORE_MAX_TURNS` | `40` | Turn budget for the live-explore agent. |
| `QTEA_LIVE_EXPLORE_TIMEOUT_S` | (step timeout) | Wall-clock timeout for the live-explore pass (seconds). |
| `QTEA_REUSE_SOURCE_BUDGET` | unlimited | Max source files scanned when classifying locators as `reuse` vs `create_tbd`. |

### Step 8 — Codegen Quality Gates

| Variable | Default | Description |
|---|---|---|
| `QTEA_CODEGEN_CONCURRENCY` | `3` | Number of parallel `call_reasoning_llm` calls during Step 8 Phase A/B. |
| `QTEA_SKIP_PARSE_CHECK` / `QTEA_NO_PARSE_CHECK` | — | Set `1` to skip Python `ast.parse` validation of generated test files. Emergency bypass only. |
| `QTEA_SKIP_STATIC_CHECK` / `QTEA_NO_STATIC_CHECK` | — | Set `1` to skip the Ruff/mypy static-check gate. Emergency bypass only. |
| `QTEA_STATIC_CHECK_TIMEOUT_S` | `120` | Timeout for static-check invocations (seconds). |
| `QTEA_SKIP_INTENT_SCORE` | — | Set `1` to skip the intent-matching quality score gate. |
| `QTEA_INTENT_FAIL_AS_WARN` | — | Set `1` to demote intent-score failures to warnings (non-blocking). |

### Step 9 — Execution & Self-Heal

| Variable | Default | Description |
|---|---|---|
| `QTEA_MAX_HEAL_ITERS` | `3` | Maximum heal→re-run rounds per attempt before reporting a test as `failed`. |
| `QTEA_MAX_HEAL` | `15` | Maximum failing tests submitted to the heal agent per round. |
| `QTEA_HEAL_CONCURRENCY` | `3` | Parallel heal worker slots. |
| `QTEA_HEAL_MAX_TURNS` | `40` | Turn budget per `polyglot-test-fixer` invocation. |
| `QTEA_HEAL_ALL` | — | Set `1` to attempt healing on failures the classifier would normally skip. |
| `QTEA_DEFAULT_TIMEOUT_MS` | `60000` | Default Playwright action timeout injected into the SUT subprocess (milliseconds). |
| `QTEA_INFLATE_TIMEOUTS` | `1` | Set `0` to disable the runtime's automatic timeout inflation on first-run TBD sentinels. |
| `QTEA_STORAGE_STATE` | — | Path to a Playwright `storageState.json` to inject into the MCP browser. Overrides `<sut>/.qtea/storage-state.json`. |
| `QTEA_PYTEST_MARKER` | — | Extra pytest marker expression appended to the test command. |
| `QTEA_NO_LLM_RESOLVE` | — | Set `1` to disable LLM resolver tiers (4–5) and the heal agent symmetrically. Standard CI default for zero-LLM-spend runs. |

### JIT Locator Resolution (Playwright stacks only)

| Variable | Default | Description |
|---|---|---|
| `QTEA_DEV_LOCATORS` | `<workspace>/locator-cache/dev-locators.json` | Path to the operator-curated dev-locator file. |
| `QTEA_DEV_POOL_THRESHOLD` | `0.65` | Minimum token-set-ratio score for a tier-1b intent-pool match. |
| `QTEA_DEV_POOL_MARGIN` | `0.10` | Minimum margin between top and second-best pool match (prevents near-tie false positives). |
| `QTEA_DEV_POOL_PAGE_PENALTY` | `0.15` | Score penalty when a pool entry's page differs from the current page context. |
| `QTEA_AOM_DEPTH` | unlimited | Maximum depth for `aria_snapshot` traversal. Cap to control snapshot size on deep DOMs. |
| `QTEA_AOM_BOXES` | `auto` | Bounding-box mode: `auto` (use when Playwright ≥1.60 detected), `off`, or `force`. |
| `QTEA_AOM_LEGACY_OK` | `1` | Set `0` to hard-fail when Playwright is too old to support `aria_snapshot`. |
| `QTEA_DISABLE_JIT` | — | Set `1` to disable the JIT monkey-patch entirely (sentinels pass through as literal strings). |
| `QTEA_VERIFY_UNIQUE` | `1` | Set `0` to skip the uniqueness check on promotable selectors. |

### Overlay Handling

| Variable | Default | Description |
|---|---|---|
| `QTEA_OVERLAY_HANDLING` | `1` | Set `0` to disable overlay auto-dismissal and the `page.add_locator_handler` registration path. |

### Set by Pipeline (read-only for operators)

These are injected into the SUT subprocess by `s09_execute.py`. Do not set manually — listed here for observability and troubleshooting.

| Variable | Description |
|---|---|
| `QTEA_RUN_ID` | Unique run identifier (ISO-8601 timestamp or UUID). |
| `QTEA_CACHE_DIR` | Directory where the JIT runtime reads/writes `locator-cache.json`. |
| `QTEA_TESTS_DIR` | Absolute path to the SUT's test root (from `research.json`). |
| `QTEA_WORKSPACE_DIR` | Absolute path to `.qtea/<run-id>/`. |
| `QTEA_RESOLVER_PORT` | Port of the parent-side `ResolverServer` for tier-4 LLM resolution over loopback. |
| `QTEA_RESOLVER_TOKEN` | Per-run HMAC secret authenticating resolver requests. `ANTHROPIC_API_KEY` is stripped from the subprocess env — the key stays in the trusted resolver process. |
| `QTEA_RESOLVER_MODEL` | Model identifier used by `ResolverServer` for tier-4 LLM calls (from `agent_models.yaml`). |
| `QTEA_RESOLVER_CMD` | Legacy subprocess fallback resolver command (used when `QTEA_RESOLVER_PORT` is unset). |
| `QTEA_STORAGE_STATE_ARG` | `--storage-state=<path>` fragment injected into the Playwright MCP server command. |
| `QTEA_MCP_USER_DATA_DIR_ARG` | `--user-data-dir=<path>` fragment for the MCP browser user-data directory. |
| `QTEA_INTERCEPTORS` | Path to the persisted overlay interceptors file (`<sut>/.qtea/interceptors.json`). |
