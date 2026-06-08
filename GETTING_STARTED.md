# Getting Started with worca-t

> End-to-end guide: from installation to your first fully autonomous QA run.

## Prerequisites

| Tool | Required? | Check | Install |
| --- | --- | --- | --- |
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

# Required if your spec comes from Jira (step 1) — also used by the
# Atlassian MCP server that .mcp.json spawns
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your-jira-api-token

# Optional — Xray Cloud (step 5 auto-skips if unset)
JIRA_XRAY_CLIENT_ID=
JIRA_XRAY_CLIENT_SECRET=

# Optional — corporate proxy (also forwarded to MCP servers)
HTTP_PROXY=http://proxy:3128
HTTPS_PROXY=http://proxy:3128

# Required — URL where your app runs (for browser-based testing)
SUT_BASE_URL=http://localhost:3000
```

**Minimum for a local run:** only `ANTHROPIC_API_KEY` is required. Jira
and Xray credentials are optional — steps 1 and 5 auto-adapt.

**MCP servers:** `.mcp.json` references `JIRA_BASE_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`, `HTTP_PROXY`, and `HTTPS_PROXY` via `${VAR}` syntax.
The Claude CLI resolves these from your environment at launch time, so
make sure they are set in your `.env` file or exported in your shell.

Alternatively, pass a custom env file at runtime (the file can live
anywhere on disk — it does not need to be inside the SUT):

```bash
worca-t run --spec ./spec.md --sut ./app --env-file /path/to/.env.prod
```

### SUT environment variables (interactive resolution)

When the pipeline reaches Step 6 (Research), it scans the SUT codebase for
environment variable keys used by the application (e.g., in `.env.example`,
`process.env.VAR`, `os.environ.get("VAR")`). If any **required** keys are
not yet set in your environment, worca-t prompts you to enter them one by
one:

```text
╭─ SUT environment input required ──────────────────────╮
│ Step 6 discovered 3 required SUT environment           │
│ variable(s) that are not yet set.                      │
│ Enter a value for each, or press Enter to skip.        │
╰───────────────────────────────────────────────────────╯

  SUT_BASE_URL: https://qa.askbosch.com
  USERNAME: test_user
  PASSWORD: ********
```

Sensitive keys (containing `PASSWORD`, `SECRET`, `TOKEN`, etc.) are
entered with masked input. Entered values are injected into the process
environment for the rest of the run but **never logged or persisted to
disk**.

To skip interactive prompts (e.g., in CI pipelines where variables are
already set by the pipeline), pass `--no-hitl`:

```bash
worca-t run --spec ./spec.md --sut ./app --no-hitl
```

A key is classified as **required** when it appears in `.env.example` or
matches a critical pattern (`*BASE_URL*`, `SUT_*`, `*DATABASE_URL*`).
All other discovered keys are **optional** — they are logged but not
prompted for.

### Remote SUT (git URL)

`--sut` accepts git URLs from any major hosting provider:

```bash
# GitHub / GitLab / Bitbucket
worca-t run --spec ./spec.md --sut https://github.com/org/app.git

# Azure DevOps (HTTPS)
worca-t run --spec ./spec.md \
  --sut https://org@dev.azure.com/org/project/_git/repo

# Azure DevOps (SSH)
worca-t run --spec ./spec.md \
  --sut git@ssh.dev.azure.com:v3/org/project/repo
```

The repository is shallow-cloned (`--depth=1`) into the workspace. Since
`.env` files are gitignored, they won't be part of the clone. Use
`--env-file` to point to a local env file, or let the interactive prompt
ask for missing values.

### Azure DevOps Variable Groups

When running in an Azure DevOps pipeline (or any environment with access
to the Azure DevOps REST API), worca-t can pull SUT environment variables
directly from a Variable Group. Set these environment variables:

| Env Var | Purpose |
| --- | --- |
| `AZDO_ORG` | Azure DevOps organization name |
| `AZDO_PROJECT` | Azure DevOps project name |
| `AZDO_VARIABLE_GROUP` | Name of the Variable Group to read from |
| `AZDO_PAT` | Personal Access Token with **Variable Groups (Read)** scope |

When all four are set, Step 6 automatically queries the Variable Group and
resolves any matching SUT keys. Example pipeline YAML:

```yaml
variables:
  - group: my-qa-variables        # the Variable Group in Library
  - name: AZDO_ORG
    value: MyOrg
  - name: AZDO_PROJECT
    value: MyProject
  - name: AZDO_VARIABLE_GROUP
    value: my-qa-variables
  - name: AZDO_PAT
    value: $(System.AccessToken)  # or a PAT stored as a secret

steps:
  - script: worca-t run --spec jira:PROJ-123 --sut $(SUT_REPO_URL) --no-hitl
```

**Note:** Variables marked as **secret** in Azure DevOps cannot be
retrieved via the REST API (the API returns `null`). For those, set them
directly in your pipeline definition or use `--env-file`.

## 3. Validate your setup

```bash
worca-t doctor
```

Expected output: all checks `OK` or `INFO`. Fix any `FAIL` items before
proceeding. Common issues:

| Check | Fix |
| --- | --- |
| claude CLI: FAIL | Install Claude Code CLI or set `WORCA_T_CLAUDE_BIN=claude` |
| npx: FAIL | Install Node.js (includes npx) |
| ANTHROPIC_API_KEY: WARN | Add it to your `.env` file |
| proxy: INFO | Safe to ignore if you're not behind a corporate proxy |
| allure CLI: INFO | Optional — built-in HTML report is always generated |

Doctor flags:

| Flag | Purpose |
| --- | --- |
| `--probe-mcp` | Smoke-spawn each MCP server to verify they start |
| `--json` | Emit results as JSON (useful for CI integration) |

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

**With an Azure DevOps repo and a separate env file:**

```bash
worca-t run --spec jira:PROJ-123 \
  --sut https://org@dev.azure.com/org/project/_git/repo \
  --env-file ./qa.env
```

**With the report opening automatically:**

```bash
worca-t run --spec ./feature-spec.md --sut ./app --open-report
```

The pipeline runs all 11 steps. You'll see console output like:

```text
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

All `run` flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--spec` | required | `jira:KEY-123`, path to spec file, or URL |
| `--sut` | required | Local path or git URL of the System Under Test |
| `--run-id` | auto | Resume an existing workspace by its run-id |
| `--from-step N` | — | Start from step N (skips already-completed steps) |
| `--only-step N` | — | Run exactly one step |
| `--skip-step N` | — | Skip step N (repeatable) |
| `--force` | false | Ignore checkpoints and re-run everything |
| `--parallelism N` | 1 | Number of parallel test workers (1–16) |
| `--headless / --headed` | headless | Run browser tests headless or with a visible window |
| `--debug` | false | Run with debug agent live from step 1 |
| `--fix` | false | Generate a fix proposal after retry exhaustion |
| `--strict-xray` | false | Fail the pipeline if Xray upload doesn't succeed |
| `--report MODE` | auto | `auto \| builtin \| allure \| both` |
| `--report-inline-images` | false | Embed screenshots as base64 in the HTML report |
| `--open-report` | false | Open the report in your browser when the run finishes |
| `--log-level LEVEL` | info | `info \| debug \| trace` |
| `--env-file PATH` | — | Path to a `.env` file to load (values never appear in logs) |
| `--no-hitl` | false | Disable interactive prompts (CI mode) |
| `-w / --workspace PATH` | `~/.worca-t` | Override the workspace base directory |

## 6. List and inspect workspaces

```bash
worca-t list
```

Shows all workspaces under `~/.worca-t`, newest first:

```text
 Workspaces under /home/you/.worca-t
 run-id                    status    last  steps  started              spec
 20260523-143012-a1b2c3   finished  11    11     2026-05-23 14:30:12  feature-spec.md
 20260522-091500-b3c4d5   failed    7     7      2026-05-22 09:15:00  jira-PROJ-123
```

List flags:

| Flag | Purpose |
| --- | --- |
| `--all / -a` | Include stale/empty workspaces (zero steps done) |
| `--limit N` | Max workspaces to display (default 20, max 500) |
| `--json` | Emit JSON instead of a table |
| `-w / --workspace PATH` | Override workspace base directory |

Use the `run-id` column with `--run-id` to resume a specific run:

```bash
worca-t run --spec ./spec.md --sut ./app --run-id 20260522-091500-b3c4d5
```

## 7. Find your results

All artifacts are under `.worca-t/<run-id>/artifacts/`:

```text
.worca-t/<run-id>/
├── artifacts/
│   ├── step01/   spec.md, jira-spec.md
│   ├── step02/   refined-spec.md, refined-spec.json
│   ├── step03/   plan.md, plan.json
│   ├── step04/   test-strategy.md, test-strategy.json
│   ├── step05/   xray-mapping.json (skipped if no creds)
│   ├── step06/   research.md, research.json
│   ├── step07/   generated test files, tbd-index.json
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
| --- | --- | --- |
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

## Developing worca-t locally

The installed wheel contains **two kinds of frozen content** that don't
auto-update when you edit the source tree:

| What you edited | How to pick it up |
| --- | --- |
| Markdown resources: `agents/`, `templates/`, `schemas/`, `skills/`, `examples/`, `CLAUDE.md`, `.mcp.json` | Set `WORCA_T_RESOURCE_ROOT=<repo-root>` — the runner reads from there instead of the frozen `_resources/` snapshot. **No reinstall needed.** |
| Python code: anything under `src/worca_t/**.py` | Either reinstall the tool, or run the dev venv binary (editable install). The env var does **not** help here — Python imports come from site-packages, not from `WORCA_T_RESOURCE_ROOT`. |

```bash
# A) Set the resource-root env var (works only for markdown edits).
export WORCA_T_RESOURCE_ROOT=/path/to/worca-t      # bash / zsh
$Env:WORCA_T_RESOURCE_ROOT = "C:\path\to\worca-t"  # PowerShell
set WORCA_T_RESOURCE_ROOT=C:\path\to\worca-t       # cmd

# B) Reinstall the tool (covers BOTH markdown and Python changes).
uv tool install --reinstall --force /path/to/worca-t

# C) Run the dev venv binary directly (editable install — picks up Python
#    edits live; pair with WORCA_T_RESOURCE_ROOT for markdown edits).
/path/to/worca-t/.venv/bin/worca-t run ...
C:\path\to\worca-t\.venv\Scripts\worca-t.exe run ...
```

To disable interactive prompts (e.g., for CI runs), pass `--no-hitl`:

```bash
worca-t run --spec ... --sut ... --no-hitl
```

## Next steps

- See `agents/qa-orchestrator.instructions.md` for the full operator reference
- See `CHANGELOG.md` for version history
- Run `worca-t doctor` any time to diagnose environment issues
