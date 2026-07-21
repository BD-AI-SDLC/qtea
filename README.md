# QTea

> **Q**uality **T**esting **e**nvironment for **a**gentic **AI**
> One requirement in — executed tests, self-healed automation, classified bugs, and a shareable report out.

**QTea** is a fully autonomous QA SDLC orchestrator. Hand it a single requirement — a Jira ticket, an Azure DevOps work item, or a local markdown spec — and it drives that requirement through the entire testing lifecycle on its own: it refines the spec, plans and designs the tests, studies your codebase, generates the automation, runs it, heals what breaks, classifies the bugs it finds, and hands back a polished report. Eleven deterministic, checkpointed steps — no babysitting required.

Under the hood, QTea is a hybrid. **Python** runs the pipeline deterministically — sequencing, retries, checkpoints, and schema validation — while **LLM agents** do the reasoning, invoked either as single-turn prompts (direct SDK) or as multi-turn, tool-using agents (Claude Agent SDK). The boundary is strict and deliberate: *Python never reasons, agents never checkpoint.* That is what makes every run repeatable, inspectable, and safe to resume.

## Who it's for

- **Product owners & requirement authors** — start from the ticket you already wrote. QTea turns it into real, executed tests and a traceable report, mapping requirements through to Xray along the way.
- **QA engineers** — QTea writes automation the way a senior SDET would: Page Object Model (or your repo's own pattern), accessibility-first locators (never XPath), no hard waits, F.I.R.S.T. principles throughout. When a locator or test breaks, it self-heals the *test-side* code — correcting assertions to the expected value, never weakening them.
- **Developers** — everything is deterministic, schema-validated, and checkpointed, so a run resumes exactly where it left off and every decision is an inspectable artifact. Failures come with automated root-cause analysis and a hand-off fix proposal — QTea never silently edits your application code.
- **Team leads & management** — one command turns backlog items into an evidence-backed quality report, with a full audit trail of what was tested and why, plus per-run cost metrics in the desktop UI.

## How it works — the 11-step pipeline

Every step is checkpointed and produces a schema-validated artifact, so the pipeline can stop, resume, and be audited at any point.

| # | Step | Agent | Output |
|---|---|---|---|
| 1 | Intake | `ticket-to-ai-spec` (or pure copy) | `spec.md` |
| 2 | Refine | `refine-spec` | `refined-spec.{md,json}` |
| 3 | Plan | `polyglot-test-planner` | `plan.{md,json}` |
| 4 | Design | `test-designer` (Senior SDET persona) | `test-design.{md,json}` |
| 5 | Xray | pure code | `xray-mapping.json` |
| 6 | Research | `polyglot-test-researcher` | `research.{md,json}` |
| 7 | Test architect | `test-automation-architect` (+ `site-explorer` authenticated-exploration pre-pass) | `code-modification-plan.json` |
| 8 | TDD codegen | POM lane: `codegen-pom-extender`, `codegen-test-writer`; non-POM: `codegen-exemplar-writer`; both: `codegen-violation-fixer` | test files + `tbd-index.json` |
| 9 | Run + heal + verify | pure code + `polyglot-test-fixer` (on failure) | `run-results.json` (+ `locator-cache.json` when JIT) |
| 10 | Bug classification | `bug-report-classifier` | `bug-reports.{md,json}` |
| 11 | Report | pure code | `report/index.html` + Allure |

See `GETTING_STARTED.md` for the full end-to-end walkthrough, or
`docs/qa-orchestrator.instructions.md` for the operator reference manual.

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

## Quickstart

```bash
qtea doctor                     # validate environment
qtea run --spec <path_to_spec> --sut <path_to_sut>
```

## Desktop UI

Prefer a window to a terminal? `qtea ui` wraps the entire CLI in a Flet-based desktop app:

```bash
qtea ui
```

It gives you a configuration panel (spec source, SUT path, all run options, skip-step toggles), a live pipeline view (per-step status cards, HITL dialogs, a streaming log, and cost metrics), and a results view. HITL and review-gate prompts surface as dialogs, so the pipeline runs fully interactively — no terminal needed.

> **Note (Windows, first launch):** the first `qtea ui` run downloads the Flet desktop client once. If it fails with `PermissionError: [WinError 5] Access is denied` (antivirus briefly locking the unpacked files), just run `qtea ui` again — see [GETTING_STARTED.md](GETTING_STARTED.md) for a permanent Defender-exclusion fix.

## Reporting

Every run ends with two reports, both generated automatically: a built-in interactive HTML report (Step 11) and a full Allure report.

## Resume & debug

Runs are checkpointed, so you can stop, resume, replay a single step, or dig into a failure:

```bash
qtea list                               # show all workspaces (run-ids, status, last step)
qtea run --spec ./spec.md --sut ./app   # resumes from last checkpoint
qtea run --run-id <id> --spec ...       # resume a specific workspace by run-id
qtea run --from-step 6 --run-id ...     # skip steps 1-5
qtea run --only-step 11 --spec ...      # regenerate report only
qtea run --force --spec ...             # ignore all checkpoints
qtea run --debug --spec ...             # RCA on attempt 1 too (default: final-failure only)
qtea run --no-fix --spec ...            # suppress the auto fix-proposal chain (RCA still writes)
qtea run --auth-prewarm-mode mcp ...    # Step-7 auth: headed (default) | mcp | script | off
qtea auth-capture --sut ./app           # one-shot Playwright storageState capture
                                        #   (for MFA/SSO SUTs — lets Step 7/9
                                        #    browsers skip auth-replay)
```

## Authentication (login-gated SUTs)

Step 7 explores the running app and Step 9 self-heals in a browser — both need a logged-in session on gated SUTs. How the Step-7 `site-explorer` authenticates is **mode-switchable** via `--auth-prewarm-mode` (or the `QTEA_AUTH_PREWARM_MODE` env var):

| Mode | How it logs in | Creds reach the model? | Needs SUT env? |
|---|---|---|---|
| `headed` (default) | A human logs in through a visible browser, then QTea explores that authenticated session. Handles MFA / SSO. Interactive sessions only. | No — typed straight into the browser. | No |
| `mcp` | The `site-explorer` drives the login UI via Playwright MCP (types the credentials, submits), then explores in the same session. Pattern-agnostic — works for POM, Screenplay, etc. | Yes — but **masked** (`***REDACTED***`) in on-disk prompts, transcripts, and logs. | No |
| `script` | Runs the SUT's own sign-in helper in a subprocess → `storage-state.json`. | No — creds stay in the subprocess. | Yes |
| `off` | Explore unauthenticated. | — | — |

For `mcp` and `script` modes, credentials come from the SUT's `auth_flow.credentials_env_vars` (resolved from your environment or `--env-file`). Pick a specific pair with `QTEA_AUTH_USERNAME_VAR` / `QTEA_AUTH_PASSWORD_VAR`, and steer the identity-provider choice with `QTEA_AUTH_IDENTITY_PROVIDER` (e.g. `Internal`, to avoid an SSO/MFA option). For **MFA / SSO** that can't be automated, use the default `headed` mode, `script` mode with `--auth-headed`, or capture a session up front with `qtea auth-capture` + `--storage-state`. Because `headed` needs a human at a visible browser, headless / CI runs should use `mcp`, `script`, or `off`. In the **desktop UI**, select the mode via the `QTEA_AUTH_PREWARM_MODE` env var (there is no dedicated panel control).

## Scope & limitations (v1)

QTea is young and honest about where it has been proven. Everything below is on the roadmap, not a permanent boundary.

### Tested configurations

- **Operating system** — tested **only on Windows** (a Bosch BCNC machine). macOS and Linux are untested.
- **Automation stack** — tested **only with Playwright**, on **Python, TypeScript, and JavaScript** SUTs. Other frameworks and languages are untested.

### Current boundaries

- **Spec sources (Step 1 intake).** Only **Jira / Atlassian** and **Azure DevOps** work items can be retrieved automatically (via their REST APIs), alongside local markdown specs. Other test-management tools (e.g. Soulman, SignalView) are **not** yet supported.
- **Single repo only.** `--sut` takes one local path or one git URL — GitHub, GitLab, Bitbucket, Azure DevOps, Codeberg, Gitea, sr.ht, or any `.git` URL (shallow-cloned with your ambient git credentials). Multi-repo / monorepo workspaces and cross-repo dependencies are not currently supported — the SUT must be a single, self-contained repository the pipeline can clone and execute against.
- **`script`-mode auth stacks.** `qtea auth-capture` and `--auth-prewarm-mode script` support **Python and Node.js (JS/TS)** Playwright SUTs only; Java / Selenium / Cypress / Robot SUTs that need pre-captured storage state (MFA / SSO) raise `NotImplementedError`. The `headed` (default) and `mcp` modes are **stack-agnostic** — they drive the login UI directly rather than the SUT's own code — and Step 9's same-run auto-capture still works on any Playwright stack.
