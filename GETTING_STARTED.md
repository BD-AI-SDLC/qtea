# Getting Started with worca-t

> End-to-end guide: from installation to your first fully autonomous QA run.

## Prerequisites

| Tool | Required? | Check | Install |
|------|-----------|-------|---------|
| Python 3.11+ | Yes | `python --version` | python.org or `uv python install 3.12` |
| uv | Yes | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Claude Code CLI | Yes | `claude --version` | `npm install -g @anthropic-ai/claude-code` |
| npx | Yes | `npx --version` | Bundled with Node.js (install nodejs.org) |
| Allure CLI | Optional | `allure --version` | `npm install -g allure-commandline` |

## 1. Install worca-t

```bash
uv tool install /path/to/worca-t
worca-t --help
```

Or for development:

```bash
cd /path/to/worca-t
uv sync
uv run worca-t --help
```

## 2. Configure environment

Create a `.env` file in your working directory with your values:

```bash
# Required — Anthropic API key for Claude agents
ANTHROPIC_API_KEY=sk-ant-api03-...

# Required if your spec comes from Jira (step 1)
ATLASSIAN_URL=https://yourcompany.atlassian.net
ATLASSIAN_EMAIL=you@company.com
ATLASSIAN_API_TOKEN=your-atlassian-api-token

# Optional — Xray Cloud (step 5 auto-skips if unset)
JIRA_XRAY_CLIENT_ID=
JIRA_XRAY_CLIENT_SECRET=

# Optional — corporate proxy
HTTP_PROXY=http://proxy:3128
HTTPS_PROXY=http://proxy:3128

# Required — URL where your app runs (for browser-based testing)
SUT_BASE_URL=http://localhost:3000
```

**Minimum for a local run:** only `ANTHROPIC_API_KEY` is required. Jira
and Xray credentials are optional — steps 1 and 5 auto-adapt.

## 3. Validate your setup

```bash
worca-t doctor
```

Expected output: all checks `OK` or `INFO`. Fix any `FAIL` items before
proceeding. Common issues:

| Check | Fix |
|-------|-----|
| claude CLI: FAIL | Install Claude Code CLI or set `WORCA_T_CLAUDE_BIN=claude` |
| npx: FAIL | Install Node.js (includes npx) |
| ANTHROPIC_API_KEY: WARN | Add it to your `.env` file |
| proxy: INFO | Safe to ignore if you're not behind a corporate proxy |
| allure CLI: INFO | Optional — built-in HTML report is always generated |

## 4. Write your spec (or use Jira)

Create a markdown file describing the feature to test:

```markdown
# Login Feature Spec

## Overview
Users should be able to log in with email and password.

## Requirements
- REQ-01: Login form has email and password fields
- REQ-02: Valid credentials redirect to the dashboard
- REQ-03: Invalid credentials show an error message
- REQ-04: Empty fields show validation errors
```

Save as `feature-spec.md`.

## 5. Run the full pipeline

**With a local spec and local SUT:**

```bash
worca-t run --spec ./feature-spec.md --sut ./path-to-your-app
```

**With a Jira ticket and a Git repo:**

```bash
worca-t run --spec jira:PROJ-123 --sut https://github.com/org/app.git
```

**With the report opening automatically:**

```bash
worca-t run --spec ./feature-spec.md --sut ./app --open-report
```

The pipeline runs all 11 steps. You'll see console output like:

```
workspace .worca-t/20260523-143012-a1b2c3
run_id    20260523-143012-a1b2c3
>>> step 01 intake
step 01 ok  -> 2 outputs
>>> step 02 refine
step 02 ok  -> 2 outputs
...
>>> step 11 report
step 11 ok  -> 2 outputs
```

## 7. Find your results

All artifacts are under `.worca-t/<run-id>/artifacts/`:

```
.worca-t/<run-id>/
├── artifacts/
│   ├── step01/   spec.md, jira-spec.md
│   ├── step02/   refined-spec.md, refined-spec.json
│   ├── step03/   plan.md, plan.json
│   ├── step04/   test-strategy.md, test-strategy.json
│   ├── step05/   xray-mapping.json (skipped if no creds)
│   ├── step06/   research.md, research.json
│   ├── step07/   generated test files, tests-with-tbd.json
│   ├── step08/   locator-resolution.json
│   ├── step09/   run-results.json, screenshots/, traces/
│   ├── step10/   bug-reports.md, bug-reports.json
│   └── step11/   index.html, data/run.json, allure-results/
├── state.json    checkpoint state
└── run.log.jsonl structured log
```

**Open the report:**

```bash
# Built-in HTML (always generated)
open .worca-t/<run-id>/artifacts/step11/index.html

# Or use the --open-report flag to auto-open at end of run
```

## 8. Common workflows

### Re-run only the report (after editing bug classifications)

```bash
worca-t run --spec ./spec.md --sut ./app --only-step 11
```

### Resume after a failure

The pipeline auto-resumes from the last completed step:

```bash
# First run fails at step 7
worca-t run --spec ./spec.md --sut ./app
# step 07 FAILED: ...

# Fix the issue, then just re-run — steps 1-6 are skipped
worca-t run --spec ./spec.md --sut ./app
```

### Force a clean re-run (ignore checkpoints)

```bash
worca-t run --spec ./spec.md --sut ./app --force
```

### Re-run from a specific step

```bash
worca-t run --spec ./spec.md --sut ./app --from-step 7
```

### Debug a failing step

```bash
# Verbose debug logging from step 1
worca-t run --spec ./spec.md --sut ./app --debug

# Get a fix proposal when a step fails twice
worca-t run --spec ./spec.md --sut ./app --fix
```

Debug artifacts are written to `.worca-t/<run-id>/debug/`.
Fix proposals are suggestions only — worca-t never auto-edits your code.

### Skip Xray upload (or enforce it)

```bash
# Default: step 5 auto-skips when JIRA_XRAY credentials are unset
worca-t run --spec ./spec.md --sut ./app

# Enforce: fail the pipeline if Xray upload doesn't succeed
worca-t run --spec ./spec.md --sut ./app --strict-xray
```

## 9. The 11 steps explained

| # | Step | What it does |
|---|------|-------------|
| 1 | **Intake** | Fetches the spec from Jira, URL, or local file |
| 2 | **Refine** | AI refines the spec into structured requirements |
| 3 | **Plan** | AI creates a test plan with phases and success criteria |
| 4 | **Strategy** | AI generates test cases (TC-IDs) with steps and priorities |
| 5 | **Xray** | Uploads test cases to Xray Cloud (auto-skips if no creds) |
| 6 | **Research** | AI analyzes the SUT codebase, detects stack and patterns |
| 7 | **Codegen** | AI generates test code (Playwright/Cypress/etc.) |
| 8 | **Locators** | AI resolves TBD locators using AOM snapshots (never XPath) |
| 9 | **Execute** | Runs the tests, self-heals failing locators (max 5 tests) |
| 10 | **Classify** | AI classifies failures into structured bug reports |
| 11 | **Report** | Generates HTML report + optional Allure report |

## 10. Reporting options

```bash
--report auto       # (default) built-in HTML + Allure when CLI present
--report builtin    # built-in HTML only (zero dependencies)
--report allure     # Allure only (requires allure CLI)
--report both       # always generate both

--report-inline-images  # embed screenshots as base64 in HTML
--open-report           # open the report in your browser when done
```

## Next steps

- See `agents/qa-orchestrator.instructions.md` for the full operator reference
- See `CHANGELOG.md` for version history
- Run `worca-t doctor` any time to diagnose environment issues
