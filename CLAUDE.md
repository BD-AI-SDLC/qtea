# CLAUDE.md - Mandatory load for any Claude session in this repo

> Every Claude session in this repository **MUST** read this file first.

`worca-t` — 11-step autonomous QA SDLC pipeline. Entry point: `worca-t run --spec <source> --sut <path>`.

---

## Source of Truth

| Purpose | File |
| --- | --- |
| Operational playbook (all 11 steps, phases, protocols) | `agents/qa-orchestrator.instructions.md` |
| Orchestrator agent definition | `agents/qa-orchestrator.agent.md` |
| Debug agent (RCA on failure) | `agents/debug.agent.md` |
| Python pipeline entry | `src/worca_t/pipeline.py` |
| Step implementations | `src/worca_t/steps/s01_intake.py` → `s11_report.py` |
| Agent → model map | `src/worca_t/agent_models.yaml` |
| CLI flags | `src/worca_t/cli.py` |

---

## Two-Layer Architecture

- **Layer 1 — Python state machine** (`pipeline.py`, `claude_runner.py`, `checkpoints.py`):
  drives step sequencing, retry (`MAX_ATTEMPTS=2`), checkpoint persistence, schema
  validation. Deterministic. No reasoning.
- **Debug agent** (`agents/debug.agent.md`): co-runs automatically on attempt 2 for RCA.
- **Fix-proposal flow** (`critical-thinking` → `principal-software-engineer`): invoked
  after retry exhaustion when `--fix` is set. Writes `fix-proposal.md`. Never auto-edits.

**Boundary: Python never reasons. Agents never checkpoint.**

---

## The 11-Step Pipeline

**Phases:** A = Requirements & Planning (1–4) · B = Research & Implementation (5–8) · C = Execution & Reporting (9–11)

| # | Name | Phase | Step File | Agent | Schema | On Failure |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Intake | A | `s01_intake.py` | `jira-to-ai-spec` / file-copy | — | abort |
| 2 | Spec Refinement | A | `s02_refine.py` | `refine-spec` | `refined-spec` | abort |
| 3 | Test Planning | A | `s03_plan.py` | `polyglot-test-planner` | `plan` | abort |
| 4 | Test Strategy | A | `s04_strategy.py` | `test-manager` | `test-strategy` | abort |
| 5 | Xray Upload | B | `s05_xray.py` | None (pure code) | `xray-mapping` | compensate |
| 6 | Repo Discovery | B | `s06_research.py` | `polyglot-test-researcher` | `research` | abort |
| 7 | TDD Codegen. After the phase gate, `pipeline.py` runs a lightweight human review gate (`src/worca_t/review_gate.py`) that surfaces test names + descriptions + counts and offers `[a]pprove` / `[e]dit files` / `[q]uit`. Auto-skipped in non-TTY / `--no-hitl`. On `edit`, the SUT is re-indexed in place so step 8 sees fresh line numbers. | B | `s07_codegen.py` | `ui-test-automation` | `tbd-index` | abort |
| 8 | **Soft-deleted as of the JIT-runtime refactor.** No-op stub that always returns `status: skipped` with a minimal `locator-resolution.json` payload. Locator resolution now happens at runtime (Step 9) for all stacks. The full 11→10 pipeline renumber is deferred — see refactor plan. | B | `s08_locator_resolution.py` (stub) | none | `locator-resolution` (minimal stub only) | skip |
| 9 | Execute + Self-Heal. **For Playwright stacks (Python/TS/JS/Java)**: starts a parent-side `ResolverServer` on a loopback TCP port and exports `WORCA_T_RESOLVER_PORT`/`WORCA_T_RESOLVER_TOKEN` into the pytest env. The vendored runtime plugin intercepts `tbd("…")` / `Tbd.of("…")` sentinels via the tier ladder: dev-locators → cache → in-process heuristic → ResolverServer (LLM) → HITL/fail-fast. ANTHROPIC_API_KEY stays in the parent process — never enters the SUT subprocess. Unresolved TBDs flow into `bug-candidates.json` as `locator-unresolvable` entries for Step 10, or get prompted on a TTY (answer → `.worca-t/dev-locators.json` for next run). **For non-Playwright stacks (Selenium/Cypress/Robot)**: existing `polyglot-test-fixer` on-failure heal handles `TBD_LOCATOR` markers via Playwright MCP observation (or a one-off native source-capture path per stack — `driver.page_source` / `cy.document()` / `Get Source` — when MCP can't reach the page state). `WORCA_T_NO_LLM_RESOLVE=1` disables both the runtime LLM tier AND the heal agent for symmetric zero-LLM-spend in CI. | C | `s09_execute.py` | `polyglot-test-tester` + `polyglot-test-fixer` | `run-results` (+ `locator-cache` when JIT) | abort |
| 10 | Bug Classification | C | `s10_bug_classifier.py` | `bug-report-classifier` | `bug-reports` | compensate |
| 11 | Report | C | `s11_report.py` | None (pure code) | `report-data` | warn + continue |

---

## Execution Flow

1. `worca-t run --spec <source> --sut <path>` — `cli.py` parses flags into `PipelineOptions`
2. `run_pipeline()` creates workspace `~/.worca-t/<run-id>/`, loads or creates `RunState`
3. **SUT preflight.** SUT is materialized eagerly (cloned/linked into `<workspace>/sut/`) and put on the worca-t isolation branch before any step runs.
4. **MCP preflight.** Every server in `.mcp.json` is cold-started via `mcp_manager.probe_server()`. If any fails, an interactive HITL prompt offers retry; non-TTY / `--no-hitl` / `--yes` fail fast with exit code 2. Side effect: warms the npx cache so the first agent call doesn't pay the bootstrap cost.
5. `_select_steps()` builds the step list (honoring `--from-step`, `--only-step`, `--skip-step`, `--force`).
6. **Steps run strictly sequentially: 1 → 2 → ... → 11.** No background tasks, no cross-step concurrency. For each step: instantiate from `steps/sNN_*.py`, call `step.execute(StepContext)`, validate outputs, persist checkpoint, move on.
7. Step invokes agent via `claude_runner.run_agent()` (subprocess: `claude` CLI) or runs pure code.
8. Agent writes artifacts to `~/.worca-t/<run-id>/artifacts/stepNN/`.
9. Step validates output via `schemas.py`, returns `StepResult`.
10. On failure: retry (attempt 2) with `debug.agent.md` co-running.
11. Retries exhausted + `--fix`: `critical-thinking` → `principal-software-engineer` → `fix-proposal.md`.
12. Retries exhausted without `--fix`: mark step `failed`, abort pipeline.

---

## Key Constraints

- **Schema-first.** Every artifact validated against its JSON Schema before hand-off.
- **Locator priority:** `id > data-testid > role > label > text > placeholder > scoped CSS`. **Never XPath.**
- **Snapshot discipline.**
  - **In generated test code (Step 7 output):** AOM only (via the framework's accessibility-tree API, e.g. Playwright `page.accessibility.snapshot()`). Raw page-source dumps (`page.content()`, `driver.page_source`, equivalents) are forbidden in tests.
  - **In Step 9 runtime (JIT ResolverServer + non-PW self-heal):** the AOM (`page.accessibility.snapshot()` for Playwright stacks, Playwright MCP `browser_snapshot` for the non-PW heal agent) is the primary truth source for locator resolution. Raw-DOM capture (`browser_evaluate(() => document.documentElement.outerHTML)`, `driver.page_source`, `cy.document()`, `Get Source`) is a scoped fallback ONLY when the target is missing from the AOM, non-semantic, or screen-reader-hidden — and each fallback resolution must record `snapshot_source="raw_dom_fallback"` plus a `fallback_reason` in `locator-cache.json` or the heal agent's diff log.
- **No hard waits** in generated tests (`time.sleep`, `cy.wait(<n>)`, etc.).
- **No secrets in code.** Env vars only. Masked in logs: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.
- **Self-heal scope** (step 9): locators only — never assertions, never business logic.
- **F.I.R.S.T.** test principles.
- **Markdown size:** ~200 lines target, 500 hard cap (CI-enforced).
- **Retry policy:** `MAX_ATTEMPTS=2`. Debug agent co-runs on attempt 2.
- **Max step timeout:** 1800 s. *This file is the single source of the timeout number; every agent file should defer to `src/worca_t/config.py` (`MAX_STEP_TIMEOUT_S`) rather than restating it.*

---

## JIT Locator Resolution (Playwright stacks — Python / TS / JS / Java)

For SUTs where the active module's framework is Playwright (Python+pytest, TS/JS+Playwright Test / Jest / Vitest, Java+JUnit5 / TestNG), Step 7 vendors a per-language runtime into the SUT and codegen emits unresolved locators using the appropriate sentinel helper. Python/TS/JS use `tbd("intent")` (returns `__WORCA_T_TBD__::<intent>`); Java uses `Tbd.of("intent")`. At test runtime, the runtime patches `Page.locator` / `Frame.locator` / `Locator.locator` (Python + TS/JS, on the sync API) or wraps `Page` via `WorcaT.wrap(page)` returning a dynamic-proxy (Java) to intercept sentinels against the live page (already authenticated, already on the right URL because the test's own POMs navigated there).

**Resolution tier ladder (all stacks):**

1. Dev-supplied locator file (zero LLM, zero tokens)
2. Runtime cache (zero LLM)
3. In-process AOM heuristic — exact `role + name` match against `page.accessibility.snapshot()`. Zero LLM, free at runtime; conservative thresholds (≥0.9 confidence, no near-tie) so false positives fall through cleanly. Typically resolves 50-70% of conventional CRUD/auth UIs without any LLM call.
4. LLM via parent-side `ResolverServer` over loopback TCP (one LLM call per cold miss)
5. HITL prompt on TTY / fail-fast with `locator-unresolvable` bug-candidate entry for Step 10 on non-TTY / `--no-hitl`

**Security: parent-side ResolverServer.** Step 9 spawns a `ResolverServer` (TCP loopback, per-run shared secret) BEFORE invoking pytest. The pytest plugin connects to the server using `WORCA_T_RESOLVER_PORT` + `WORCA_T_RESOLVER_TOKEN` env vars and ships AOM + intent over the wire. The server makes the Anthropic API call in the trusted parent process. **`ANTHROPIC_API_KEY` never enters the SUT subprocess** — `safe_subprocess_env()` strips it. Leaked tokens are useless after the run completes (the server is bound to the Step 9 context manager).

**Cache-invalidate-and-retry on TimeoutError.** Every returned `Locator` is wrapped in a retry proxy. When an action (click / fill / hover / etc.) raises `TimeoutError`, the proxy invalidates the cache, re-resolves via the LLM (skipping the dev file + cache + heuristic that produced the stale selector), and replays the action once. If the second attempt also fails, the original `TimeoutError` propagates and Step 9's `polyglot-test-fixer` self-heal agent picks it up (a slower path that edits POM source files).

**Dev-supplied locator file** (the parent-worca handover protocol):

- CLI flag: `worca-t run --dev-locators /path/to/file.json …`
- Env var: `WORCA_T_DEV_LOCATORS=/path` (in the worca-t child process env)
- Convention path: `<sut>/.worca-t/dev-locators.json`

HITL answers from Tier 5 prompts are merged into the same `dev-locators.json` so the next run's Tier 1 picks them up without re-prompting.

**Async Python Playwright (`playwright.async_api`)** is fully patched alongside the sync API. The async path returns an `_AsyncLazyLocator` synchronously from `page.locator(SENTINEL)` whose action methods (`.click()`, `.fill()`, …) await resolution + the underlying action on first call. Codegen mirrors the SUT's existing API style — sync if the SUT uses `pytest-playwright`'s sync fixture, async if it uses `pytest-asyncio` + `playwright.async_api`.

**JIT runtime env vars** (set automatically by Step 9 — listed here for debugging / opt-out):

- `WORCA_T_CACHE_DIR` — directory for `locator-cache.json` (auto-set to `<workspace>/locator-cache/`)
- `WORCA_T_DEV_LOCATORS` — dev-supplied locator file path (when `--dev-locators` or env is set)
- `WORCA_T_RESOLVER_PORT` / `WORCA_T_RESOLVER_TOKEN` — loopback TCP coordinates for the parent ResolverServer (preferred LLM path)
- `WORCA_T_RESOLVER_CMD` — legacy subprocess fallback, defaults to `worca-t resolve`; only used when `WORCA_T_RESOLVER_PORT` is not set
- `WORCA_T_RESOLVER_MODEL` — defaults to `claude-sonnet-4-6`; override for cost/quality trade-offs
- `WORCA_T_DEFAULT_TIMEOUT_MS` — Playwright default timeout the plugin inflates to (default 60000)
- `WORCA_T_INFLATE_TIMEOUTS` — set to `0` to opt out of the 60s timeout inflation
- `WORCA_T_DISABLE_JIT` — set to `1` to disable the runtime monkey-patch entirely
- `WORCA_T_NO_LLM_RESOLVE` — set to `1` to disable Tier 4 (LLM) AND the self-heal agent symmetrically; cache + dev-locators + heuristic only. CI default for zero-LLM-spend determinism.

## MCP Servers

| Server | Used by | Purpose |
| --- | --- | --- |
| `playwright` | Step 9 (`polyglot-test-fixer` heal mode, non-Playwright stacks only) | AOM snapshots + locator discovery during on-failure self-heal |

All MCPs are launched by the `claude` CLI per project-local `.mcp.json`. JIT runtime resolution (Playwright stacks) does NOT use Playwright MCP — it consumes the live page's AOM in-process and dispatches to the parent `ResolverServer` over loopback TCP. Step 1 Jira intake uses direct REST (`worca_t.jira_client.fetch_issue`), not the retired Atlassian MCP.

---

## File Map

| Directory / File | Contents |
| --- | --- |
| `src/worca_t/pipeline.py` | `run_pipeline()` — main orchestration loop |
| `src/worca_t/claude_runner.py` | `run_agent()` — spawns `claude` CLI subprocess |
| `src/worca_t/steps/` | 11 step files (`s01_intake.py` → `s11_report.py`) + `base.py` |
| `src/worca_t/checkpoints.py` | `RunState`, `StepRecord`, `load_state()`, `save_state()` |
| `src/worca_t/cli.py` | Typer CLI: `run`, `doctor`, `version` |
| `src/worca_t/config.py` | Settings, timeouts, proxy |
| `src/worca_t/schemas.py` | JSON Schema loading + validation |
| `src/worca_t/workspace.py` | Workspace paths, `generate_run_id()` |
| `src/worca_t/report/` | HTML and Allure report generation |
| `agents/` | All agent `.md` files |
| `schemas/` | JSON Schema files for every artifact hand-off |
| `skills/` | Composed skill directories referenced by pipeline steps |

---

## Guardrails

When executing `worca-t run` or any pipeline step:

- Do NOT perform Explore, Grep, Read, or codebase analysis before launching the runner — the pipeline has built-in steps for code discovery, requirement intake, and analysis.
- Trust the runner's design — execute the command as given.
- Only perform additional operations if explicitly requested by the user, or if the runner fails and needs troubleshooting.
- When echoing environment variables or reading from `.env`, never expose the real value of any key in the terminal or in any output — mask or omit the value entirely.
- Resources (`agents/`, `templates/`, `schemas/`, `skills/`, `examples/`, `CLAUDE.md`, `.mcp.json`) are baked into the installed wheel as a frozen `_resources/` snapshot. Markdown edits propagate when `WORCA_T_RESOURCE_ROOT=<repo-root>` is set. **Python code edits (`src/worca_t/**.py`) require a tool reinstall** (`uv tool install --reinstall --force <repo-root>`) or running from the dev `.venv` (editable install) — the env var does not help with Python.
