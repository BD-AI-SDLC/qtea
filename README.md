# QTea

> **Quality** **Testing** **environment** for **agentic** **AI**  
> Fully autonomous QA SDLC orchestrator. Hybrid workflow consists of Python + LLM calls.  
> It uses Python to run the pipeline deterministically, and spins up agents via direct SDK invocation (solely LLM prompt) or Claude Agent SDK (file management, MCP).

`qtea` turns a requirement (Jira ticket or local markdown spec) into a tested
feature, executed tests, self-heal, classified bugs, and a polished report - in 11
deterministic, checkpointed steps.

## Install
clone the repo locally. then, to install it:
```bash
uv tool install <path-to-qtea>
qtea --help
```

Runs on Windows / Linux / macOS / CI from any shell.

## Quickstart

```bash
qtea doctor                     # validate environment
qtea run --spec ./feature.md --sut ./my-app
```

## The pipeline

| # | Step | Agent | Output |
|---|---|---|---|
| 1 | Intake | `jira-to-ai-spec` (or pure copy) | `spec.md` |
| 2 | Refine | `refine-spec` | `refined-spec.{md,json}` |
| 3 | Plan | `polyglot-test-planner` | `plan.{md,json}` |
| 4 | Strategy | `test-manager` | `test-strategy.{md,json}` |
| 5 | Xray | pure code | `xray-mapping.json` |
| 6 | Research | `polyglot-test-researcher` | `research.{md,json}` |
| 7 | Test architect | `test-architect` | `code-modification-plan.json` |
| 8 | TDD codegen | `codegen-violation-fixer` | test files + `tbd-index.json` |
| 9 | Run + heal + verify | pure code + `polyglot-test-fixer` (on failure) | `run-results.json` (+ `locator-cache.json` when JIT) |
| 10 | Bug class. | `bug-report-classifier` | `bug-reports.{md,json}` |
| 11 | Report | pure code | `report/index.html` + Allure (when available) |

See `GETTING_STARTED.md` for the full end-to-end walkthrough, or
`docs/qa-orchestrator.instructions.md` for the operator reference manual.

## Reporting

`--report auto` (default) generates both Allure (when the `allure` CLI is
installed) and a zero-dependency built-in HTML report. The built-in report is
fully offline-viewable and always produced as a fallback.

Force one or the other: `--report allure`, `--report builtin`, `--report both`.
`--report allure` and `--report both` auto-open the Allure UI when generation succeeds — no `--open-report` flag needed.

## Desktop UI

```bash
# Install with the UI extra
uv tool install 'qtea[ui]'

# Launch
qtea ui
```

A Flet-based desktop window that wraps the full CLI: a configuration panel (spec source, SUT path, all run options, skip-step toggles), a live pipeline view (per-step status cards, HITL dialogs, log stream, cost metrics), and a results view. HITL and review-gate prompts surface as dialogs so the pipeline runs fully interactively without a terminal.

`qtea-ui` is also registered as a standalone console script.  
Without the `[ui]` extra, `qtea ui` prints an install hint and exits.

## Resume & debug

```bash
qtea list                               # show all workspaces (run-ids, status, last step)
qtea run --spec ./spec.md --sut ./app   # resumes from last checkpoint
qtea run --run-id <id> --spec ...       # resume a specific workspace by run-id
qtea run --from-step 6 --run-id ...       # skip steps 1-5
qtea run --only-step 11 --spec ...      # regenerate report only
qtea run --force --spec ...             # ignore all checkpoints
qtea run --debug --spec ...             # verbose debug agent from step 1
qtea run --fix --spec ...               # RCA + fix proposal on failure
qtea auth-capture --sut ./app           # one-shot Playwright storageState capture
                                           # (for MFA/SSO SUTs — lets Step 9's
                                           # heal agent skip auth-replay)
```

## Limitations (v1)

- **Single repo only.** `--sut` takes one path or one git URL. Multi-repo / monorepo workspaces and cross-repo dependencies are not currently supported — the SUT must be a single, self-contained repository the pipeline can clone and execute against.
- **`qtea auth-capture` supports Python and Node.js (JS/TS) Playwright SUTs.** Java / .NET / Selenium / Cypress / Robot SUTs that need pre-captured storage state (MFA / SSO) raise `NotImplementedError`. Same-run auto-capture (Step 9 default) still works on any Playwright stack.

## Status

All 11 pipeline steps implemented. Core milestones complete:

- **M0–M3** — skeleton, CLI, claude runner, intake, refine, plan, strategy.
- **M4–M6** — TDD codegen, locator resolution, execute + self-heal.
- **M7–M8** — bug classifier, Xray uploader, reporting (HTML + Allure).
- **M9–M10** — retry/debug/fix flow, checkpoint hash invalidation.
- **M11** — docs polish, markdown size enforcement.
- **M12** — CI (GitHub Actions).

See `CHANGELOG.md` for detailed per-milestone progress.
