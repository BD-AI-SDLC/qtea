# Changelog

All notable changes to `worca-t` are tracked here. Format: Keep a Changelog;
auto-appended by `worca-t` runs and the `tools/hooks/update-changelog.py`
pre-commit hook.

## [Unreleased]

### Fixed (production hardening)
- `checkpoints.py`: `load_state()` now catches corrupted `state.json`
  (JSONDecodeError, missing keys) and returns `None` instead of crashing.
  `save_state()` uses atomic write (`.tmp` → rename) to prevent
  truncation on disk-full.
- `claude_runner.py`: secrets masking unified to `***REDACTED***`
  (was `***`).
- Removed unused `gitpython` dependency.
- Added tests for core modules: `test_proxy.py` (5), `test_init_cmd.py`
  (3), `test_doctor.py` (8), `test_pipeline.py` (7),
  + 3 corrupted-state tests in `test_checkpoints.py`.
  **245/245 passing**, ruff clean.

### Added (M12 - CI)
- `.github/workflows/ci.yml`: GitHub Actions CI.
  - Matrix: Ubuntu + Windows × Python 3.11 / 3.12.
  - Jobs: `test` (uv sync, ruff check, pytest, markdown size check)
    and `package` (uv tool install + `worca-t version` smoke test).
  - Triggers on push/PR to main/master.

### Added (M11 - docs polish)
- `tools/check_md_size.py`: standalone markdown size enforcement script.
  Scans `*.md` files against soft (200) / hard (500) line caps.
  Excludes `agents/`, `skills/`, `candidate_agents/`, and
  `final_plan_implementation.md` (historical). `--strict` flag fails on
  soft-limit violations.
- `tools/hooks/update_changelog.py`: pre-commit hook validating
  CHANGELOG.md format (`## [Unreleased]` section, bullet points per
  milestone entry).
- `README.md`: polished with resume/debug/fix CLI examples and full
  milestone status (M0–M11).
- Dropped vestigial `suite_parent` assignment in `test_runner.py`.
- Tests: `test_md_size_check.py` (8). **219/219 passing**, ruff clean.

### Added (M10 - checkpoints/resume polish)
- `src/worca_t/checkpoints.py`: `outputs_match()` — content-hash
  invalidation. Compares on-disk output files against recorded SHA256
  hashes. Steps whose outputs changed since completion are re-run on
  resume instead of silently skipped.
- `src/worca_t/pipeline.py`: wired `outputs_match()` into the skip
  logic. When a completed step's outputs don't match, it re-runs with
  a `[yellow]outputs changed` console message.
- Tests: `test_checkpoints.py` (12 — 5 `outputs_match` unit,
  3 `_select_steps`, 4 pipeline integration). **211/211 passing**,
  ruff clean.

### Added (M9 - debug/fix flow)
- `src/worca_t/steps/base.py`: retry-once policy in `Step.execute()`.
  - On first failure, snapshots debug artifacts
    (`transcript.jsonl`, `stderr.log`) to `<ws>/debug/step-NN-attempt1/`,
    sets `debug_live=True`, and retries once.
  - Successful retry returns `status="warned"` (not "completed").
  - Max 2 attempts per step.
- `--fix` flag: after 2nd failure, invokes `critical-thinking` agent (RCA)
  then `principal-software-engineer` agent (fix proposal). Writes
  `<ws>/debug/step-NN-rca.md` and `<ws>/debug/step-NN-fix-proposal.md`.
  **Never auto-edits source code.**
- `--debug` flag: sets `debug_live=True` from attempt 1 (not just retry).
- `src/worca_t/pipeline.py`: wires `--debug` into `ctx.extras["debug_live"]`.
- Tests: `test_debug_fix.py` (11). **199/199 passing**, ruff clean.

### Added (M8 - step 5 Xray uploader)
- `src/worca_t/steps/s05_xray.py`: `XrayUploadStep` + `XrayClient`.
  - Auto-skips when `JIRA_XRAY_CLIENT_ID`/`JIRA_XRAY_CLIENT_SECRET` (or
    `JIRA_XRAY_API_KEY`) are not set — emits
    `xray-mapping.json` with `status="skipped"`.
  - Authenticates via Xray Cloud REST API v2 (OAuth2 client credentials or
    pre-generated API key).
  - Bulk-imports test cases from step 4 `test-strategy.json` with tenacity
    retry (3 attempts, exponential backoff).
  - Project key from `JIRA_PROJECT_KEY` env var or extracted from
    `jira:PROJ-123` spec source.
  - `--strict-xray`: partial upload failures become hard failures.
  - Uses `httpx` with proxy support via `with_proxy_env()`.
- `schemas/xray-mapping.schema.json`.
- Tests: `test_step05_xray.py` (18). **188/188 passing**, ruff clean.

### Added (M7.5 - step 10 reporting)
- `src/worca_t/report/` package: `data_builder.py`, `html_renderer.py`,
  `allure_writer.py`.
  - `data_builder.py`: joins step 8 `run-results.json` + step 9
    `bug-reports.json` + optional step 3 `plan.json` + step 4
    `test-strategy.json` into a normalized `RunReport` dataclass.
    Serialises to `data/run.json` (validated against `report-data` schema).
  - `html_renderer.py`: stdlib-only (`string.Template` + f-strings, NO
    Jinja2) self-contained HTML. Summary dashboard, per-test result rows
    (filterable by status), per-bug cards, attachments. Supports
    `--report-inline-images` (base64 PNG embed). Works with `file://` open.
  - `allure_writer.py`: writes Allure-compatible `*-result.json` under
    `allure-results/`; shells out to `allure generate` when CLI present.
    Auto-skips gracefully when `allure` not on PATH.
- `src/worca_t/steps/s11_report.py`: `ReportStep` (pure code, no agent).
  Short-circuits with `status="skipped"` when step 8 outputs missing.
  Respects `--report {auto|allure|builtin|both}`, `--open-report`,
  `--report-inline-images`.
- `schemas/report-data.schema.json`.
- Tests: `test_step11_report.py` (18). **170/170 passing**, ruff clean.

### Added (M7 - step 9 bug classifier)
- `src/worca_t/steps/s10_bug_classifier.py`: `BugClassifierStep`.
  - Short-circuits with an empty, schema-valid report when step 8 produced
    no `bug-candidates`.
  - Stages `run-results.json`, `bug-candidates.json`, `heal-log.jsonl`,
    `test-strategy.json`, and the bug-report template/example/edge-case
    docs into the agent workdir; invokes `bug-report-classifier`.
  - Validates the agent's `bug-reports.json` against the canonical schema
    AND checks `len(bugs) == len(candidates)`. On any failure, falls back
    to a deterministic `_synthesize` that builds a schema-valid report
    from `bug-candidates` (every fallback bug marked
    `rationale: "auto-classified (agent output unusable)"`).
  - Renders `bug-reports.md` from the JSON when the agent omits it.
  - Step status: `completed` when agent output is used, `warned` when
    fallback is used.
- Tests: `test_step10_bug_classifier.py` (11 - 6 unit, 5 integration).
  **152/152 passing**, ruff clean.

### Added (M6 - step 8 execution + self-heal)
- `src/worca_t/test_runner.py`: framework-agnostic subprocess runner with
  parsers for JUnit XML, Playwright `--reporter=json`, Jest JSON, Mocha JSON,
  Robot `output.xml`, and Maven Surefire reports. Default commands per
  framework + `resolve_command` precedence (`detected > default > pytest`).
  Cross-platform `execute_command` honouring `WORCA_T_*` proxy env. Synthetic
  `T-runner-failure` entry when the runner crashes without producing output.
- `src/worca_t/steps/s09_execute.py`: `ExecuteStep`.
  - Mirrors patched tests from step 8 (or step 7 fallback) into
    `<sut>/worca-tests/`, runs the framework command, captures per-test
    status + attachments (screenshots/traces/videos discovered post-run).
  - On failure: invokes `polyglot-test-fixer` per failing test (capped at
    `WORCA_T_MAX_HEAL=5`). Stages the original test file in a `_orig/`
    subdir so unchanged copies are not mis-detected as patches. Accepts
    only files matching the target basename with content different from
    the staged original; copies the winning candidate back into the SUT
    tests dir. Records every attempt in `self-heal/heal-log.jsonl`.
  - Re-runs the full suite once when any patch landed; second run is
    authoritative when it produces parseable results.
  - Emits `run-results.json` (incl. `self_heal` totals) and
    `bug-candidates.json`. Step status is `completed` on a clean run and
    `warned` when failures remain - test failures are surfaced via step 9,
    not as orchestrator failures.
- Schemas: `run-results.schema.json`, `bug-candidates.schema.json`.
- Pipeline registry now includes step 8.
- Tests: `test_test_runner.py` (21) + `test_step09_execute.py` (13).
  **141/141 passing**, ruff clean.

### Added (M5 - step 8 locator resolution)
- `src/worca_t/steps/s08_locator_resolution.py`: `LocatorResolutionStep`.
  - Reads step 7 index, builds a targeted user prompt for `playwright-tester`
    listing exactly which tests/markers need resolution + `SUT_BASE_URL`.
  - Agent writes `locator-resolution.json`; we deterministically patch test
    files in-place. Hard-rejects XPath replacements (`xpath=`, `//x`,
    `By.XPATH`), unknown strategies, missing tokens, and missing files -
    each rejection annotated with `skip_reason`.
  - Auto-fills `file` field from `test_id` when the agent omits it.
  - Short-circuits with no agent call when no TBD markers exist in step 7.
  - Re-indexes patched tests; any new rule violation fails the step.
  - Warns (rather than fails) on unresolved TBD markers - downstream step 8
    surfaces test failures.
- `schemas/locator-resolution.schema.json`.
- Tests: `test_step08_locator_resolution.py` (17 - 4 unit, 5 patcher,
  8 integration). **107/107 passing**, ruff clean.

### Added (M4 - step 7 codegen + strict TDD indexer)
- `src/worca_t/test_indexer.py`: language-agnostic test indexer.
  - Framework detection from explicit hint or extension fallback (8 frameworks).
  - Per-family test-block discovery with leading-comment rewind so
    `@tc TC-LOGIN-001` / `@tag smoke` annotations attach to the right test.
  - Locator strategy extraction (`getByTestId`, `getByRole`, `getByLabel`,
    `getByText`, `getByPlaceholder`, `#id`, css, `By.ID`).
  - TBD marker discovery (`TBD_LOCATOR`, `<<TBD ...>>`, `/* TBD */`).
  - Non-negotiable rule violations: `xpath`, `hard-wait`, `page-content`,
    `raw-secret`. Step 7 fails on any violation.
- `src/worca_t/steps/s07_codegen.py`: `CodegenStep` - invokes
  `ui-test-automation` agent with the matching skill staged
  (`playwright-generate-test` for Playwright stacks, `webapp-testing`
  otherwise), copies `tests/` into the artifact dir, indexes + validates,
  REJECTS the step on any rule violation (writes `violations.log`).
- `schemas/tbd-index.schema.json`.
- Tests: `test_indexer.py` (21 - 8 frameworks + every violation rule),
  `test_step07_codegen.py` (7). **90/90 passing**, ruff clean.

### Added (M3 - steps 3, 4 planner + strategy)
- `src/worca_t/steps/s03_plan.py`: invokes `polyglot-test-planner`, projects
  the phased plan into `plan.json` with extracted commands, phase summary
  table, per-phase files (source/test_file/test_class/methods), and success
  criteria. Schema-validated.
- `src/worca_t/steps/s04_strategy.py`: invokes `test-manager` (with
  `quality-playbook` + `breakdown-test` skills + test-strategy template
  staged), projects the strategy into `test-strategy.json` with extracted
  test cases (id, priority, type, preconditions, steps, expected, tags),
  edge cases, exit criteria. Auto-suffixes duplicate TC ids.
- `schemas/plan.schema.json`, `schemas/test-strategy.schema.json`.
- Tests: `test_step03_plan.py` (6), `test_step04_strategy.py` (7).

### Added (M2 - steps 1, 2, 6 + parsers + schemas)
- `src/worca_t/md_parser.py`: heading-tree markdown parser with table/bullet
  extraction, recursive dict projection, slug helper.
- `src/worca_t/schemas.py`: JSON Schema loader (package-resources + dev-tree),
  `validate`/`is_valid`/`write_validated` helpers.
- `src/worca_t/steps/base.py`: `Step` ABC + `StepContext`/`StepResult`; wraps
  `run()` with timing, attempts, state-record updates.
- `src/worca_t/steps/s01_intake.py`: intake supporting `file`, `http(s)://`,
  and `jira:KEY` (via `jira-to-ai-spec` agent). Writes `spec.md` + `jira-spec.md`.
- `src/worca_t/steps/s02_refine.py`: invokes `refine-spec` agent then
  deterministically projects to `refined-spec.json` (extracts REQ id, AC,
  user flows, edge cases, NFRs, readiness). Schema-validated.
- `src/worca_t/steps/s06_research.py`: SUT materialize (local copy or
  `git clone --depth=1`), optional `acquire-codebase-knowledge` skill prerun,
  invokes `polyglot-test-researcher`, projects to `research.json` with
  detected stack + build/test/lint commands. Schema-validated.
- `src/worca_t/pipeline.py`: real orchestrator - workspace selection (resume
  vs. fresh), state hydration, step registry, per-step execute + persist.
- `schemas/refined-spec.schema.json`, `schemas/research.schema.json`.
- Tests: `test_md_parser.py` (7), `test_schemas.py` (7),
  `test_step01_intake.py` (6), `test_step02_refine.py` (5),
  `test_step06_research.py` (7). Shared `_fake_claude.py` shim writes both
  events and output files. **49/49 tests passing**, ruff clean.

### Added (M1 - claude runner + MCP manager)
- `src/worca_t/mcp_manager.py`: `.mcp.json` loader with `${VAR}` env substitution,
  staging into a step workdir, and best-effort `probe_server()` smoke test.
- `src/worca_t/claude_runner.py`: single execution path for every agent.
  Spawns `claude --print --output-format stream-json --append-system-prompt
  @<agent> --mcp-config .mcp.json`, streams + persists JSON events to
  `transcript.jsonl`, captures stderr, enforces timeout (Windows
  `taskkill /T /F` fallback), writes per-run `metrics.json`, propagates
  proxy env, masks secrets in logs.
- `worca-t doctor --probe-mcp` flag to smoke-spawn each MCP server.
- Test fixtures: cross-platform fake `claude` CLI shim; unit coverage for
  the runner (happy path, input staging, timeout, missing binary, missing
  inputs) + MCP loader + core utilities. 17/17 tests passing.

### Added (M0 - skeleton)
- Project skeleton: `pyproject.toml` (uv-managed), `ruff.toml`, `.gitignore`.
- Package: `src/worca_t/` with `cli` (Typer), `config`, `proxy`, `logging_setup`,
  `workspace`, `checkpoints`, `init_cmd`, `doctor`, and a `pipeline` scaffold.
- Agent->model map at `src/worca_t/agent_models.yaml`.
- Resource files: `.mcp.json`, `.env.example`, `CLAUDE.md`,
  `qa-orchestrator.agent.md`, `qa-orchestrator.instructions.md`, `README.md`.
- Curated `agents/` directory (copied from `candidate_agents/`) + new
  `bug-report-classifier.agent.md`.
- `docs/templates/`, `docs/examples/`, `docs/edge-case-checklist.md`.
- `schemas/bug-reports.schema.json` (first JSON Schema; more land per milestone).
- `worca-t` console script registered via `[project.scripts]`.
- CLI commands: `run` (scaffold), `doctor`, `init`, `version`.
