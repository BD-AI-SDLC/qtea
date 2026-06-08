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
| 7 | TDD Codegen | B | `s07_codegen.py` | `ui-test-automation` | `tbd-index` | abort |
| 8 | Locator Discovery (HITL-aware) + DOM-truth audit. **For Python+pytest+Playwright SUTs**: short-circuits to `status: skipped, mode: jit` — locator resolution defers to Step 9's vendored runtime plugin. | B | `s08_locator_resolution.py` | `playwright-tester` (8a, resolve; AOM-first, HITL escape via `./clarifications.md` for unresolvable TBDs) + `polyglot-test-fixer` (8b, audit-only) | `locator-resolution` (+ `dom-comparison` when not JIT) | abort |
| 9 | Execute + Self-Heal. **For Python+pytest+Playwright SUTs**: also runs the vendored `tests/worca_t_runtime.py` plugin which intercepts `tbd("…")` sentinels at runtime, consults the optional dev-supplied locator file (`--dev-locators` / `WORCA_T_DEV_LOCATORS` / `<sut>/.worca-t/dev-locators.json`), then the runtime cache, then the LLM resolver (`worca-t resolve` subprocess) — caching results to `artifacts/step09/locator-cache.json`. | C | `s09_execute.py` | `polyglot-test-tester` + `polyglot-test-fixer` | `run-results` (+ `locator-cache` when JIT) | abort |
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
- **Snapshot discipline (two-tier rule).**
  - **In generated test code (Step 7 output):** AOM only (via the framework's accessibility-tree API, e.g. Playwright `page.accessibility.snapshot()`). Raw page-source dumps (`page.content()`, `driver.page_source`, equivalents) are forbidden in tests.
  - **In Step 8a (playwright-tester runtime exploration via Playwright MCP):** AOM-first. Every distinct URL opened in the session is captured via `browser_snapshot` and persisted to `page-snapshot-NN.json`. Raw-DOM capture (`browser_evaluate(() => document.documentElement.outerHTML)` → `page-snapshot-NN-raw.html`) is permitted ONLY as a scoped fallback when the target element is missing from the AOM, non-semantic (no role/label/ARIA), or hidden from screen readers — and each fallback must be annotated per-item in `locator-resolution.json` with `snapshot_source="raw_dom_fallback"` plus a `fallback_reason`. Element-scoped queries only within an already-captured page. Audit in 8b reads the persisted files.
- **No hard waits** in generated tests (`time.sleep`, `cy.wait(<n>)`, etc.).
- **No secrets in code.** Env vars only. Masked in logs: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.
- **Self-heal scope** (step 9): locators only — never assertions, never business logic.
- **F.I.R.S.T.** test principles.
- **Markdown size:** ~200 lines target, 500 hard cap (CI-enforced).
- **Retry policy:** `MAX_ATTEMPTS=2`. Debug agent co-runs on attempt 2.
- **Max step timeout:** 1800 s. *This file is the single source of the timeout number; every agent file should defer to `src/worca_t/config.py` (`MAX_STEP_TIMEOUT_S`) rather than restating it.*

---

## JIT Locator Resolution (Python + pytest + Playwright)

For SUTs where the active module is Python + pytest + Playwright, Step 7 vendors a `tests/worca_t_runtime.py` plugin into the SUT and codegen emits unresolved locators as `LOGIN_BUTTON = tbd("primary submit button on the login form")` instead of bare `TBD_LOCATOR` strings. At test runtime, the plugin monkey-patches `Page.locator` to intercept the sentinel and resolve it against the live page (already authenticated, already on the right URL because the test's own POMs navigated there). Resolution order: dev-supplied locator file → runtime cache → `worca-t resolve` LLM subprocess → HITL. Step 8 short-circuits in JIT mode (`status: skipped`). For all other frameworks (TS, Java, Robot, …), Step 8 keeps the agent-navigation flow described above.

The resolver uses a **direct Anthropic SDK call** (not Playwright MCP) with the AOM snapshot the runtime captured in-process via `page.accessibility.snapshot()`. Playwright MCP is only used by the legacy Step 8a flow for non-Python frameworks.

**Cache-invalidate-and-retry on TimeoutError.** Every returned `Locator` is wrapped in a `_RetryingLocator` proxy. When an action method (click / fill / hover / etc.) raises `TimeoutError` (Playwright couldn't find the element), the proxy:
1. Invalidates the cache entry for that constant
2. Re-resolves via the LLM (skipping the dev file + the just-invalidated cache so a fresh selector is produced from the current page state)
3. Replays the action once with the new selector

If the second attempt also fails, the original `TimeoutError` propagates and Step 9's `polyglot-test-fixer` self-heal agent picks it up (a slower path that edits POM source files). This means dev-supplied selectors that are stale or wrong get auto-corrected inline without falling through to the fixer agent.

Dev-supplied locator file (the parent-worca handover protocol):
- CLI flag: `worca-t run --dev-locators /path/to/file.json …`
- Env var: `WORCA_T_DEV_LOCATORS=/path` (in the worca-t child process env)
- Convention path: `<sut>/.worca-t/dev-locators.json`

Discovery is first-match-wins. Dev selectors are returned without verification — Playwright's own action retry catches a stale selector via `TimeoutError`, triggering the cache-invalidate-and-retry path described above. The same JSON format will be produced by any future external-LLM handover tool.

**JIT runtime env vars** (all set automatically by Step 9 — listed here for debugging / opt-out):
- `WORCA_T_CACHE_DIR` — directory for `locator-cache.json` (auto-set to `<workspace>/locator-cache/`)
- `WORCA_T_DEV_LOCATORS` — dev-supplied locator file path (when `--dev-locators` or env is set)
- `WORCA_T_RESOLVER_CMD` — defaults to `worca-t resolve`
- `WORCA_T_RESOLVER_MODEL` — defaults to `claude-sonnet-4-6`; override for cost/quality trade-offs
- `WORCA_T_DEFAULT_TIMEOUT_MS` — Playwright default timeout the plugin inflates to (default 60000)
- `WORCA_T_INFLATE_TIMEOUTS` — set to `0` to opt out of the 60s timeout inflation
- `WORCA_T_DISABLE_JIT` — set to `1` to disable the runtime monkey-patch entirely (tests then run with raw sentinels, which Playwright will treat as CSS selectors and fail)

## MCP Servers

| Server | Used by | Purpose |
| --- | --- | --- |
| `playwright` | Steps 8, 9 | AOM snapshots, locator discovery, test browser control |
| `atlassian` | Step 1 | Jira ticket intake (optional) |

All MCPs are launched by the `claude` CLI per project-local `.mcp.json`.

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
