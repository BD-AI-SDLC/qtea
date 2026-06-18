# worca-t

> **W**orkflow **O**rchestrated **R**ealtime **C**laude **A**utonomous **T**esting.  
> Fully autonomous QA SDLC orchestrator. Hybrid workflow consists of Python + LLM calls.  
> It uses Python to run the pipeline deterministically, and spins up agents via direct SDK invocation (solely LLM prompt) or Claude Agent SDK (file management, MCP).

`worca-t` turns a requirement (Jira ticket or local markdown spec) into a tested
feature, executed tests, self-heal, classified bugs, and a polished report - in 11
deterministic, checkpointed steps.

## Install
clone the repo locally. then, to install it:
```bash
uv tool install <path-to-worca-t>
worca-t --help
```

Runs on Windows / Linux / macOS / CI from any shell.

## Quickstart

```bash
worca-t doctor                     # validate environment
worca-t run --spec ./feature.md --sut ./my-app
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
| 8 | TDD codegen | `ui-test-automation` | test files + `tbd-index.json` |
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

## Resume & debug

```bash
worca-t list                               # show all workspaces (run-ids, status, last step)
worca-t run --spec ./spec.md --sut ./app   # resumes from last checkpoint
worca-t run --run-id <id> --spec ...       # resume a specific workspace by run-id
worca-t run --from-step 6 --spec ...       # skip steps 1-5
worca-t run --only-step 11 --spec ...      # regenerate report only
worca-t run --force --spec ...             # ignore all checkpoints
worca-t run --debug --spec ...             # verbose debug agent from step 1
worca-t run --fix --spec ...               # RCA + fix proposal on failure
worca-t auth-capture --sut ./app           # one-shot Playwright storageState capture
                                           # (for MFA/SSO SUTs — lets Step 9's
                                           # heal agent skip auth-replay)
```

## Status

All 11 pipeline steps implemented. Core milestones complete:

- **M0–M3** — skeleton, CLI, claude runner, intake, refine, plan, strategy.
- **M4–M6** — TDD codegen, locator resolution, execute + self-heal.
- **M7–M8** — bug classifier, Xray uploader, reporting (HTML + Allure).
- **M9–M10** — retry/debug/fix flow, checkpoint hash invalidation.
- **M11** — docs polish, markdown size enforcement.
- **M12** — CI (GitHub Actions).

See `CHANGELOG.md` for detailed per-milestone progress.
