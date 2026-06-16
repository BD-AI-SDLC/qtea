# CLAUDE.md - Mandatory load for any Claude session in this repo

> Every Claude session in this repository **MUST** read this file first.

`worca-t` — 11-step autonomous QA SDLC pipeline. Entry point: `worca-t run --spec <source> --sut <path>`.

---

## Where to look (read on demand, not eagerly)

| For | File |
| --- | --- |
| Operational playbook (all 11 steps, gates, protocols, env vars) | `docs/qa-orchestrator.instructions.md` |
| Orchestrator agent definition | `docs/qa-orchestrator.agent.md` |
| Runtime agent definitions (debug, heal, codegen, etc.) | `agents/*.md` |
| Pipeline + step code | `src/worca_t/pipeline.py`, `src/worca_t/steps/sNN_*.py` |
| CLI flags | `src/worca_t/cli.py` |
| Agent → model map | `src/worca_t/agent_models.yaml` |
| JSON schemas | `schemas/` |
| JIT runtime (vendored into SUT for Playwright stacks) | `src/worca_t/_resources/runtime/worca_t_runtime.py.tpl` |

---

## Architecture

- **Python state machine** drives sequencing, retry (`MAX_ATTEMPTS=2`), checkpoints, schema validation. Two LLM transports: `run_agent` (Agent SDK, multi-turn with tools) and `call_reasoning_llm` (direct SDK, single-turn, bounded).
- **Boundary:** Python never reasons. Agents never checkpoint.
- **Debug agent** runs after a failed attempt (last only by default; every attempt with `--debug`). Diagnosis-only — output at `<workspace>/debug/step-NN-attemptM-debug-rca.md`.
- **Fix-proposal flow** (`--fix`) writes `fix-proposal.md` after retry exhaustion. Never auto-edits.
- **Prompt caching** is tri-state (`--cache` / `--no-cache` / auto): auto-enabled when `ANTHROPIC_CUSTOM_HEADERS` contains the BMF sticky-session header (`x-bmf-sticky-session-instance`), disabled otherwise. Without sticky sessions the BMF relay does not honour `cache_control` (25% creation surcharge, zero read-side payback). Detail: `GETTING_STARTED.md` §"Prompt caching (BMF sticky sessions)".

---

## The 11-Step Pipeline

Phases: A = Requirements (1–4) · B = Research & Codegen (5–8) · C = Execute & Report (9–11). Per-step protocol detail (gates, env handling, status semantics) lives in `docs/qa-orchestrator.instructions.md`.

| # | Name | Step File | Agent | On Failure |
| --- | --- | --- | --- | --- |
| 1 | Intake | `s01_intake.py` | `jira-to-ai-spec` / file-copy | abort |
| 2 | Spec Refinement | `s02_refine.py` | `refine-spec` | abort |
| 3 | Test Planning | `s03_plan.py` | `polyglot-test-planner` | abort |
| 4 | Test Strategy | `s04_strategy.py` | `test-manager` | abort |
| 5 | Xray Upload | `s05_xray.py` | pure code | compensate |
| 6 | Repo Discovery | `s06_research.py` | `polyglot-test-researcher` | abort |
| 7 | Test Architect | `s07_test_architect.py` | `test-architect` | abort |
| 8 | TDD Codegen (phased: POM → tests → quality gate) | `s08_codegen.py` | `codegen-pom-extender`, `codegen-test-writer`, `ui-test-automation` | abort |
| 9 | Execute + Self-Heal | `s09_execute.py` | `polyglot-test-fixer` (heal only) | abort |
| 10 | Bug Classification | `s10_bug_classifier.py` | `bug-report-classifier` | compensate |
| 11 | Report | `s11_report.py` | pure code | warn + continue |

---

## Hard Rules (every step, every agent)

- **Schema-first.** Every artifact validated against its JSON Schema in `schemas/` before hand-off.
- **Locator priority:** `id > data-testid > role > label > text > placeholder > scoped CSS`. **Never XPath.**
- **Snapshot discipline.** AOM only. Playwright Python: `page.locator("body").aria_snapshot(mode="ai")` (v1.59+) with graceful fallback to no-mode (v1.40-1.58) and legacy `page.accessibility.snapshot()` (pre-v1.40). Raw page-source (`page.content()`, `driver.page_source`, etc.) forbidden in generated tests. Raw-DOM fallback is scoped only when target is AOM-invisible — record `snapshot_source="raw_dom_fallback"` + `fallback_reason`.
- **No hard waits** in generated tests (`time.sleep`, `cy.wait(<n>)`, etc.).
- **No secrets in code.** Env vars only. Masked in logs: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA_XRAY_*`.
- **Self-heal scope** (Step 9): POM/locator source + codegen-generated test files' *interaction patterns* (e.g. method calls, navigation, dropdown-open before option select). Assertions are immutable — enforced by the Step 9 assertion-immutability gate. Never edit business logic, fixtures, or `conftest.py`. Full allowed/forbidden matrix: `agents/polyglot-test-fixer.agent.md`.
- **Step 9 status semantics:** `completed` (no fails) / `warned` (mix of pass + fail; Step 10 classifies) / `failed` (runner produced no parseable output OR all tests errored with zero passes — environment failure modes that must halt the pipeline rather than mask as "warned").
- **Retry:** `MAX_ATTEMPTS=2`.
- **Max step timeout:** 1800 s. Single source: `src/worca_t/config.py:MAX_STEP_TIMEOUT_S`.
- **Markdown size:** 200 lines soft, 500 lines hard. Enforced by `tools/check_md_size.py`.
- **F.I.R.S.T.** test principles.

---

## JIT Locator Resolution (Playwright stacks)

Step 8 emits unresolved locators as `tbd("intent")` / `Tbd.of("intent")` sentinels. Step 9 vendors a pytest plugin into the SUT that resolves sentinels via this tier ladder:

1. Dev-supplied locator file (`.worca-t/dev-locators.json` or `--dev-locators`)
2. Runtime cache (`<workspace>/locator-cache/locator-cache.json`)
3. In-process AOM heuristic (`role + name` ≥0.9 confidence, no near-tie)
4. LLM via parent-side `ResolverServer` (loopback TCP + per-run shared secret; `ANTHROPIC_API_KEY` never enters the SUT subprocess)
5. HITL on TTY / fail-fast with `locator-unresolvable` bug-candidate on non-TTY

Action-time `TimeoutError` → cache invalidate → re-resolve once → replay → fall through to `polyglot-test-fixer` heal agent. `WORCA_T_NO_LLM_RESOLVE=1` disables tiers 4-5 + the heal agent symmetrically (CI default for zero-LLM-spend). Async Playwright is fully patched alongside sync. Full env-var list + implementation: the runtime template docstring.

---

## MCP & Playwright

Single server: `playwright` (`@playwright/mcp`), used ONLY by Step 9's `polyglot-test-fixer` heal agent for live browser control. Probed lazily inside `s09_execute.py` (green runs skip the 5-15 s npx warmup). JIT runtime resolution does NOT use Playwright MCP — it consumes AOM in-process via `Locator.aria_snapshot()`. Step 1 Jira intake uses direct REST.

**Storage-state injection.** `.mcp.json` carries `${WORCA_T_STORAGE_STATE_ARG}`. Step 9 resolves a Playwright `storageState.json` and threads `--storage-state=<path>` into the MCP server via the per-call env overlay (`mcp_manager.load_mcp_config(env=...)`). Resolution priority: `--storage-state` flag > `WORCA_T_STORAGE_STATE` env > `<sut>/.worca-t/storage-state.json` (from `worca-t auth-capture`) > `<workspace>/storage-state.json` (auto-captured by the runtime on the first passing test). Heal agent's browser boots already authenticated, skipping the 10-30 s auth-replay per heal call.

**Proxy injection.** Runtime monkey-patches `BrowserType.launch` to inject `proxy={"server": URL}` from `HTTPS_PROXY` / `WORCA_T_PROXY` when the SUT did not pass one. Required because Playwright Python's `chromium.launch()` does not auto-pickup `HTTPS_PROXY`.

---

## Guardrails (Claude session behavior)

- Do NOT pre-explore, grep, or read the codebase before launching `worca-t run` — the pipeline has built-in discovery steps. Trust the runner.
- Only perform additional operations on explicit user request OR when the runner fails and needs troubleshooting.
- Never echo real env-var / `.env` values in any output. Mask or omit.
- **Resources** (`agents/`, `templates/`, `schemas/`, `skills/`, `examples/`, `CLAUDE.md`, `.mcp.json`) are baked into the installed wheel as a frozen `_resources/` snapshot. Markdown edits propagate when `WORCA_T_RESOURCE_ROOT=<repo-root>` is set. **Python code edits require a tool reinstall** (`uv tool install --reinstall --force <repo-root>`) or running from the dev `.venv` — the env var does not help with Python.
