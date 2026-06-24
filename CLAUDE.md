# CLAUDE.md - Mandatory load for any Claude session in this repo

> Every Claude session in this repository **MUST** read this file first.

`qtea` — 11-step autonomous QA SDLC pipeline. Entry point: `qtea run --spec <source> --sut <path>`.

---

## Where to look (read on demand, not eagerly)

| For | File |
| --- | --- |
| Operational playbook (all 11 steps, gates, protocols, env vars) | `docs/qa-orchestrator.instructions.md` |
| Orchestrator agent definition | `docs/qa-orchestrator.agent.md` |
| Runtime agent definitions (debug, heal, codegen, etc.) | `agents/*.md` |
| Pipeline + step code | `src/qtea/pipeline.py`, `src/qtea/steps/sNN_*.py` |
| CLI flags | `src/qtea/cli.py` |
| Agent → model map | `src/qtea/agent_models.yaml` |
| JSON schemas | `schemas/` |
| JIT runtime (vendored into SUT for Playwright stacks) | `src/qtea/_resources/runtime/qtea_runtime.py.tpl` |

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
| 8 | TDD Codegen (phased: POM → tests → quality gate) | `s08_codegen.py` | `codegen-pom-extender`, `codegen-test-writer`, `codegen-violation-fixer` | abort |
| 9 | Execute + Self-Heal | `s09_execute.py` | `polyglot-test-fixer` (heal only) | abort |
| 10 | Bug Classification | `s10_bug_classifier.py` | `bug-report-classifier` | compensate |
| 11 | Report | `s11_report.py` | pure code | warn + continue |

---

## Hard Rules (every step, every agent)

- **Schema-first.** Every artifact validated against its JSON Schema in `schemas/` before hand-off.
- **Locator priority:** `id > data-testid > role > text > label > placeholder > alt text > title > scoped CSS`. **Never XPath.**
- **Snapshot discipline.** AOM only. Playwright Python capability ladder (probed once per process, cached on the runtime's `_AOM_CAPS`): `aria_snapshot(mode="ai", boxes=True)` (v1.60+) → `mode="ai"` only (v1.59) → no-options (v1.40-1.58) → legacy `page.accessibility.snapshot()` (pre-v1.40). When boxes are present, the parser strips `[box=x,y,w,h]` from element names and retains the coords on the node for the heuristic tie-breaker (smaller-y wins on ties); the LLM tier also receives a prompt rule to use position for spatial-hint disambiguation. Tunable via `QTEA_AOM_DEPTH` (no cap by default), `QTEA_AOM_BOXES` (`auto`/`off`/`force`), `QTEA_AOM_LEGACY_OK` (`1` default). Raw page-source (`page.content()`, `driver.page_source`, etc.) forbidden in generated tests. Raw-DOM fallback is scoped only when target is AOM-invisible — record `snapshot_source="raw_dom_fallback"` + `fallback_reason`.
- **No hard waits** in generated tests (`time.sleep`, `cy.wait(<n>)`, etc.).
- **No secrets in code.** Env vars only. Masked in logs: `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN`, `JIRA_XRAY_*`.
- **No PII / runtime secrets in artifacts.** Debug RCA markdown, bug reports, allure attachments, heal-log entries, and `bugs/*.md` candidates MUST NOT include captured form values (passwords, tokens, emails typed during runs), HTTP request/response bodies, cookies, `Authorization` headers, full storage-state contents, or query-string parameters that may carry session IDs. Redact to `<redacted:<reason>>`. The env-var mask list covers env vars only — runtime-captured values need this rule.
- **No credentials in visual artifacts.** Screenshots and video recordings can capture typed passwords, JWTs printed in the UI, OAuth callback URLs in the address bar, and MFA codes. Generated tests that touch credential fields MUST mask via Playwright's `screenshot(mask=[locator])` / `{ mask: [...] }` before capture, and SHOULD disable video on login-flow tests. If a third-party widget renders a token inline that can't be masked, omit the failure screenshot rather than ship it.
- **Filesystem containment.** All agent writes MUST stay inside `<sut>/` (test/POM/locator/fixture files the heal and codegen scopes allow) or `<workspace>/` (artifacts, logs, caches). Writes outside these two roots — including the qtea repo itself, the user's home directory, `/etc`, sibling repos, or temp dirs not under `<workspace>` — are out of scope. The per-run qtea isolation branch is the only branch any agent may commit to; never check out, create, or push to `main` / `master` / `develop` / user branches.
- **Git safety.** Agents may only commit to the per-run qtea isolation branch. Forbidden everywhere: `git push --force` / `--force-with-lease`, `git reset --hard`, `git branch -D`, `git checkout main|master|develop|<user-branch>`, `git rebase -i`, `git filter-branch`, `git clean -fdx`, deleting `.git/`. Never amend or rewrite commits the user authored. The atomic-write-then-commit discipline assumes the working tree is yours alone — preserve it.
- **Self-heal scope** (Step 9): POM/locator source + codegen-generated test files' *interaction patterns* (e.g. method calls, navigation, dropdown-open before option select). Assertions are immutable — enforced by the Step 9 assertion-immutability gate. Never edit business logic, fixtures, or `conftest.py`. Full allowed/forbidden matrix: `agents/polyglot-test-fixer.agent.md`. Heal detection covers changes to ANY SUT file (test, POM, or locator) — detected via git working-tree diff, not just test-file bytes. When at least one heal patch is applied, Step 9 re-runs the healed tests to verify the fix before reporting the outcome.
- **Step 9 status semantics:** `completed/all_passed` (all tests pass) / `completed/bugs_found` (some tests fail — bugs are expected QA output; Step 10 classifies) / `warned` (not emitted by Step 9 itself; set by `base.py` retry logic when Step 9 fails attempt 1 and succeeds attempt 2 — sub_status from the passing attempt is preserved) / `failed` (environment failure: runner produced no parseable output OR all tests errored with zero passes).
- **Retry:** `MAX_ATTEMPTS=2`.
- **Max step timeout:** 1800 s. Single source: `src/qtea/config.py:MAX_STEP_TIMEOUT_S`.
- **Markdown size:** 200 lines soft, 500 lines hard. Enforced by `tools/check_md_size.py`.
- **F.I.R.S.T.** test principles.

---

## JIT Locator Resolution (Playwright stacks)

Step 8 emits unresolved locators as `tbd("intent")` / `Tbd.of("intent")` sentinels. Step 9 vendors a pytest plugin into the SUT that resolves sentinels via this tier ladder:

1. Dev-supplied locator file (`--dev-locators` flag, `QTEA_DEV_LOCATORS` env, or `<workspace>/locator-cache/dev-locators.json` default). Two match modes: **1a exact constant-name** (HITL-replay) → **1b intent pool** (token-set-ratio match against entries with an `intent` field; thresholds via `QTEA_DEV_POOL_THRESHOLD`/`MARGIN`/`PAGE_PENALTY`). Tier 1b accepts write to the cache so subsequent runs skip fuzzy work.
2. Runtime cache (`<workspace>/locator-cache/locator-cache.json`)
3. In-process AOM heuristic (`role + name` ≥0.9 confidence, no near-tie)
4. LLM via parent-side `ResolverServer` (loopback TCP + per-run shared secret; `ANTHROPIC_API_KEY` never enters the SUT subprocess). When the dev-locator pool exists, its entries are passed in as a prior so the LLM prefers dev-supplied selectors over freshly-derived ones.
5. HITL on TTY / fail-fast with `locator-unresolvable` bug-candidate on non-TTY

Action-time `TimeoutError` → bundle-fallback walk → cache invalidate (or **dev-pool quarantine**, see below) → re-resolve once → replay → fall through to `polyglot-test-fixer` heal agent. `QTEA_NO_LLM_RESOLVE=1` disables tiers 4-5 + the heal agent symmetrically (CI default for zero-LLM-spend). Async Playwright is fully patched alongside sync. Full env-var list + implementation: the runtime template docstring.

**Structured payload kinds (post-run-20260621).** Resolver responses carry a `payload` discriminated by `kind` (`role` / `text` / `label` / `placeholder` / `test_id` / `css`). The runtime dispatches role/text/label/placeholder/test_id payloads to the matching `scope.get_by_*` API instead of `scope.locator(string)`. The LLM is prompted to emit the structured form, and `validate_selector_payload` rejects malformed selectors (e.g. Playwright AOM debug-print syntax like `link "..."`) at cache write AND at TBD-promotion. This closes the run-20260621 regression where a role-strategy response was cached as a CSS string, fed to `page.locator()`, and failed to parse.

**TBD promotion (gated).** After every test attempt Step 9 scans the SUT for remaining `tbd("intent")` sentinels and cross-references `locator-cache.json`. An entry is promoted into source ONLY when (a) it has a non-empty `passing_witnesses` list (at least one passing test exercised it — recorded by the runtime's `pytest_runtest_teardown` hook), AND (b) it passes `validate_selector_payload`. CSS / no-payload entries are substituted as quoted strings; structured payloads emit `role_locator(...)` / `text_locator(...)` / `label_locator(...)` / `placeholder_locator(...)` / `test_id_locator(...)` calls (helpers defined in the runtime template, dispatched at action time via a "resolved sentinel" string the wrapper recognises). Blocked entries surface as `promotion-blocked` bug-candidates so reviewers see what the JIT runtime is still chewing on between runs.

**Dev-pool quarantine.** When a tier-1b dev-pool selector fails at action time the runtime: (1) sets `quarantined: true` on the cache entry (the dev-supplied selector is preserved for provenance, but tier-2 reads skip it), (2) appends a JSONL record to `<workspace>/locator-cache/dev-pool-quarantine.jsonl`, (3) re-resolves with `skip_pool=True` (avoids bouncing back to the same fuzzy answer), and (4) stores the LLM fallback under a `_shadow:<key>` cache slot — so a single browser hiccup cannot silently overwrite a dev-curated entry with an LLM guess. Step 9 reads the JSONL at end-of-run and emits one `dev-locator-drifted` bug-candidate per drift, pointing the user at the dev-locators entry to update.

**Selector allowlist (resolver + TBD promotion).** `validate_selector_payload` is the single enforcement point. For string-form selectors: accepts CSS (`#id`, `[data-testid=…]`, `.cls`, scoped CSS, pseudo-classes) + Playwright engine forms (`role=...`, `text=...`, `css=...`); rejects newlines, `<script`, `javascript:`, XPath, Playwright debug-print syntax (`link "..."`), and unbalanced brackets/quotes. For structured payloads: validates the discriminated union (`kind` + required fields per kind). Wired into `_normalise_candidates` (resolver-response intake), `write_cache` (last line of defence before disk), and `_promote_resolved_tbds` (last line of defence before SUT source). Older `is_unsafe_selector` is preserved for the injection-marker check and remains the inner gate inside the validator.

---

## MCP & Playwright

Single server: `playwright` (`@playwright/mcp`), used ONLY by Step 9's `polyglot-test-fixer` heal agent for live browser control. Probed lazily inside `s09_execute.py` (green runs skip the 5-15 s npx warmup). JIT runtime resolution does NOT use Playwright MCP — it consumes AOM in-process via `Locator.aria_snapshot()`. Step 1 Jira intake uses direct REST.

**Storage-state injection.** `.mcp.json` carries `${QTEA_STORAGE_STATE_ARG}`. Step 9 resolves a Playwright `storageState.json` and threads `--storage-state=<path>` into the MCP server via the per-call env overlay (`mcp_manager.load_mcp_config(env=...)`). Resolution priority: `--storage-state` flag > `QTEA_STORAGE_STATE` env > `<sut>/.qtea/storage-state.json` (from `qtea auth-capture`) > `<workspace>/storage-state.json` (auto-captured by the runtime on the first passing test). Heal agent's browser boots already authenticated, skipping the 10-30 s auth-replay per heal call.

**Storage-state files are credentials.** `storageState.json` contains live session cookies and localStorage equivalent to a logged-in user. Reference by path only — never log its contents, embed it in `run.log.jsonl` / debug RCA / bug reports / Allure attachments, or commit it to the qtea branch. The auto-capture path lives under `<workspace>/`, not `<sut>/`, so it stays out of the user's repo by default — preserve that boundary.

**Proxy injection.** Runtime monkey-patches `BrowserType.launch` to inject `proxy={"server": URL}` from `HTTPS_PROXY` / `QTEA_PROXY` when the SUT did not pass one. Required because Playwright Python's `chromium.launch()` does not auto-pickup `HTTPS_PROXY`.

---

## Guardrails (Claude session behavior)

- Do NOT pre-explore, grep, or read the codebase before launching `qtea run` — the pipeline has built-in discovery steps. Trust the runner.
- Only perform additional operations on explicit user request OR when the runner fails and needs troubleshooting.
- Never echo real env-var / `.env` values in any output. Mask or omit.
- **Stop and ask, don't guess.** When any agent (any step, any sub-agent) encounters a missing required fact for which no sensible default exists in code, it MUST surface the gap via the current step's HITL channel (`[CLARIFICATION NEEDED]` tag picked up by `call_reasoning_llm_with_hitl`, Blockers-table row, or Open Questions bullet — whichever the step's output schema defines) rather than invent a value. Conversely: never prompt the user for a value that already has a sensible default in code — apply the default and proceed. Once the user has answered a clarification in step N, no later step may re-ask the same concern even paraphrased; the answer propagates via `user-answers.md` and the artifact's `## Coverage Notes` section.
- **Resources** (`agents/`, `templates/`, `schemas/`, `skills/`, `examples/`, `CLAUDE.md`, `.mcp.json`) are baked into the installed wheel as a frozen `_resources/` snapshot. Markdown edits propagate when `QTEA_RESOURCE_ROOT=<repo-root>` is set. **Python code edits require a tool reinstall** (`uv tool install --reinstall --force <repo-root>`) or running from the dev `.venv` — the env var does not help with Python.
