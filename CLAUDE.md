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
| 7 | TDD Codegen | B | `s07_codegen.py` | `ui-test-automation` | `tests-with-tbd` | abort |
| 8 | Locator Discovery | B | `s08_locator_resolution.py` | `playwright-tester` | `locator-resolution` | abort |
| 9 | Execute + Self-Heal | C | `s09_execute.py` | `polyglot-test-tester` + `polyglot-test-fixer` | `run-results` | abort |
| 10 | Bug Classification | C | `s10_bug_classifier.py` | `bug-report-classifier` | `bug-reports` | compensate |
| 11 | Report | C | `s11_report.py` | None (pure code) | `report-data` | warn + continue |

---

## Execution Flow

1. `worca-t run --spec <source> --sut <path>` — `cli.py` parses flags into `PipelineOptions`
2. `run_pipeline()` creates workspace `~/.worca-t/<run-id>/`, loads or creates `RunState`
3. SUT is materialized eagerly (cloned/linked into `<workspace>/sut/`) **before** the step loop, so Step 6 has no data dependency on Steps 1–5.
4. `_select_steps()` builds the step list (honoring `--from-step`, `--only-step`, `--skip-step`, `--force`)
5. **Step 6 (Repo Discovery) runs concurrently with Steps 2–5 in the background** when (a) Step 6 is selected, (b) `--only-step` is not used, and (c) at least one of Steps 2–5 is also selected (`_should_parallelize_research` in `pipeline.py`). The main loop runs 2→5 sequentially in the foreground while Step 6 executes in parallel. **Rendezvous is at the start of Step 7** — the loop awaits the background task there, since Step 7 (codegen) requires `research.md`. As a consequence, Step 6 may finish wall-clock-before Steps 3/4/5 — this is intentional, not a bug. Step 3's research input is best-effort under parallelization (Step 3 falls back to `refined-spec.md` alone if `research.md` is not yet available).
6. For each foreground step: instantiate step class from `steps/sNN_*.py`, call `step.run(StepContext)`
7. Step invokes agent via `claude_runner.run_agent()` (subprocess: `claude` CLI) or runs pure code
8. Agent writes artifacts to `~/.worca-t/<run-id>/artifacts/stepNN/`
9. Step validates output via `schemas.py`, returns `StepResult`
10. On failure: retry (attempt 2) with `debug.agent.md` co-running
11. Retries exhausted + `--fix`: `critical-thinking` → `principal-software-engineer` → `fix-proposal.md`
12. Retries exhausted without `--fix`: mark step `failed`, abort pipeline

Concurrent checkpoint writes from the foreground loop and the background Step 6 task are serialized by an `asyncio.Lock` in `save_state_async` (`checkpoints.py`).

---

## Key Constraints

- **Schema-first.** Every artifact validated against its JSON Schema before hand-off.
- **Locator priority:** `id > data-testid > role > label > text > placeholder > scoped CSS`. **Never XPath.**
- **AOM snapshots only** (`page.accessibility.snapshot()`). Never `page.content()`.
- **No hard waits** in generated tests (`time.sleep`, `cy.wait(<n>)`, etc.).
- **No secrets in code.** Env vars only. Masked in logs: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA_XRAY_API_KEY`, `JIRA_XRAY_CLIENT_ID`, `JIRA_XRAY_CLIENT_SECRET`.
- **Self-heal scope** (step 9): locators only — never assertions, never business logic.
- **F.I.R.S.T.** test principles.
- **Markdown size:** ~200 lines target, 500 hard cap (CI-enforced).
- **Retry policy:** `MAX_ATTEMPTS=2`. Debug agent co-runs on attempt 2.
- **Max step timeout:** 1800 s.

---

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
