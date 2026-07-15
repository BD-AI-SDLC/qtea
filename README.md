# QTea

> **Quality** **Testing** **environment** for **agentic** **AI**  
> Fully autonomous QA SDLC orchestrator. Hybrid workflow consists of Python + LLM calls.  
> It uses Python to run the pipeline deterministically, and spins up agents via direct SDK invocation (solely LLM prompt) or Claude Agent SDK (file management, MCP).

`qtea` turns a requirement (Jira ticket, Azure DevOps work item, or local
markdown spec) into executed tests, self-heal, classified bugs, and a polished
report — in 11 deterministic, checkpointed steps.
QTea was tested on Windows BCNC.

## Install

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js (for npx). See `GETTING_STARTED.md` for the full prerequisites list.

```bash
git clone https://github.com/BD-AI-SDLC/qtea.git
cd <path_to_qtea>
uv tool install ./qtea[ui]
qtea --help
```

For headless / CI environments (CLI only, no desktop UI):

```bash
uv tool install ./qtea
```

## Quickstart for CLI

```bash
qtea doctor                     # validate environment
qtea run --spec <path_to_spec> --sut <path_to_sut>
```

## The pipeline

| # | Step | Agent | Output |
|---|---|---|---|
| 1 | Intake | `ticket-to-ai-spec` (or pure copy) | `spec.md` |
| 2 | Refine | `refine-spec` | `refined-spec.{md,json}` |
| 3 | Plan | `polyglot-test-planner` | `plan.{md,json}` |
| 4 | Design | `test-designer` (Senior SDET persona) | `test-design.{md,json}` |
| 5 | Xray | pure code | `xray-mapping.json` |
| 6 | Research | `polyglot-test-researcher` | `research.{md,json}` |
| 7 | Test architect | `test-automation-architect` (+ `site-explorer` live-explore pre-pass) | `code-modification-plan.json` |
| 8 | TDD codegen | `codegen-pom-extender` (Phase A), `codegen-test-writer` (Phase B), `codegen-violation-fixer` (Phase C) | test files + `tbd-index.json` |
| 9 | Run + heal + verify | pure code + `polyglot-test-fixer` (on failure) | `run-results.json` (+ `locator-cache.json` when JIT) |
| 10 | Bug class. | `bug-report-classifier` | `bug-reports.{md,json}` |
| 11 | Report | pure code | `report/index.html` + Allure |

See `GETTING_STARTED.md` for the full end-to-end walkthrough, or
`docs/qa-orchestrator.instructions.md` for the operator reference manual.

## Reporting

Allure report is auto generated at the end of the run, as well as the built-in HTML report in step 11.

## Desktop UI

```bash
# Launch
qtea ui
```

A Flet-based desktop window that wraps the full CLI: a configuration panel (spec source, SUT path, all run options, skip-step toggles), a live pipeline view (per-step status cards, HITL dialogs, log stream, cost metrics), and a results view. HITL and review-gate prompts surface as dialogs so the pipeline runs fully interactively without a terminal.

## Resume & debug

```bash
qtea list                               # show all workspaces (run-ids, status, last step)
qtea run --spec ./spec.md --sut ./app   # resumes from last checkpoint
qtea run --run-id <id> --spec ...       # resume a specific workspace by run-id
qtea run --from-step 6 --run-id ...       # skip steps 1-5
qtea run --only-step 11 --spec ...      # regenerate report only
qtea run --force --spec ...             # ignore all checkpoints
qtea run --debug --spec ...             # RCA on attempt 1 too (default: final-failure only)
qtea run --no-fix --spec ...            # suppress the auto fix-proposal chain (RCA still writes)
qtea auth-capture --sut ./app           # one-shot Playwright storageState capture
                                           # (for MFA/SSO SUTs — lets Step 9's
                                           # heal agent skip auth-replay)
```

## Limitations (v1)

- **Single repo only.** `--sut` takes one path or one git URL. Multi-repo / monorepo workspaces and cross-repo dependencies are not currently supported — the SUT must be a single, self-contained repository the pipeline can clone and execute against.
- **`qtea auth-capture` supports Python and Node.js (JS/TS) Playwright SUTs.** Java / Selenium / Cypress / Robot SUTs that need pre-captured storage state (MFA / SSO) raise `NotImplementedError`. Same-run auto-capture (Step 9 default) still works on any Playwright stack.
