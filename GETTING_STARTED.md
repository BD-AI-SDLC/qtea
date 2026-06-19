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

### Prompt caching (BMF sticky sessions) — IMPORTANT, set this first

> **Set `ANTHROPIC_CUSTOM_HEADERS` as a USER environment variable before running worca-t.** Single most impactful one-time setup. Without the BMF sticky-session header the relay does not honour `cache_control` — caching becomes a net **cost loss** (25% creation surcharge, zero read-side payback). With it, worca-t auto-enables caching on every step; no `--cache` flag needed.

Pick **one** BMF replica (`01` or `02`) and stick with the same value across runs for cache locality:

```bash
ANTHROPIC_CUSTOM_HEADERS="x-bmf-sticky-session-instance: 01"   # or 02
```

**How to set:**
- **Windows (recommended):** System → Advanced system settings → Environment Variables → **User variables** → New → Name `ANTHROPIC_CUSTOM_HEADERS`, Value `x-bmf-sticky-session-instance: 01`. Open a fresh terminal afterwards.
- **macOS / Linux:** add `export ANTHROPIC_CUSTOM_HEADERS="x-bmf-sticky-session-instance: 01"` to `~/.bashrc` / `~/.zshrc`.
- **Claude Code config (alternative):** add to `~/.claude/settings.json`:
  ```json
  { "env": { "ANTHROPIC_CUSTOM_HEADERS": "x-bmf-sticky-session-instance: 01" } }
  ```

Forwarded to both Claude Code CLI subprocesses and direct Anthropic SDK calls (reasoning, JIT resolver) — every layer benefits automatically.

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

Step 6 scans the SUT for env-var keys (`.env.example`, `process.env.VAR`, `os.environ.get("VAR")`). **Required** keys that aren't already set are prompted interactively; sensitive ones (containing `PASSWORD`, `SECRET`, `TOKEN`, etc.) are masked. Entered values are injected into the process environment for the rest of the run but **never logged or persisted to disk**.

```text
  SUT_BASE_URL: https://qa.askbosch.com
  USERNAME: test_user
  PASSWORD: ********
```

A key is **required** when it appears in `.env.example` or matches `*BASE_URL*`, `SUT_*`, `*DATABASE_URL*`. All other discovered keys are optional — logged, not prompted.

CI / non-interactive: pass `--no-hitl` to skip prompts entirely (assumes vars are pre-set by the pipeline):

```bash
worca-t run --spec ./spec.md --sut ./app --no-hitl
```

### Remote SUT (git URL)

`--sut` accepts git URLs from any major hosting provider (shallow-cloned `--depth=1` into the workspace):

```bash
worca-t run --spec ./spec.md --sut https://github.com/org/app.git                     # GitHub / GitLab / Bitbucket
worca-t run --spec ./spec.md --sut https://org@dev.azure.com/org/project/_git/repo    # Azure DevOps (HTTPS)
worca-t run --spec ./spec.md --sut git@ssh.dev.azure.com:v3/org/project/repo          # Azure DevOps (SSH)
```

`.env` files are gitignored so won't be cloned — use `--env-file` or the interactive prompt for missing values.

### Azure DevOps Variable Groups

In an Azure DevOps pipeline (or any env with REST access), worca-t can pull SUT env vars directly from a Variable Group. Set these:

| Env Var | Purpose |
| --- | --- |
| `AZDO_ORG` | Azure DevOps organization name |
| `AZDO_PROJECT` | Azure DevOps project name |
| `AZDO_VARIABLE_GROUP` | Variable Group name to read from |
| `AZDO_PAT` | PAT with **Variable Groups (Read)** scope |

When all four are set, Step 6 queries the Variable Group and resolves matching SUT keys.

```yaml
variables:
  - group: my-qa-variables                                       # Variable Group in Library
  - { name: AZDO_ORG,            value: MyOrg }
  - { name: AZDO_PROJECT,        value: MyProject }
  - { name: AZDO_VARIABLE_GROUP, value: my-qa-variables }
  - { name: AZDO_PAT,            value: $(System.AccessToken) }  # or a secret-bound PAT
steps:
  - script: worca-t run --spec jira:PROJ-123 --sut $(SUT_REPO_URL) --no-hitl
```

**Note:** Variables marked **secret** in Azure DevOps cannot be retrieved via REST (API returns `null`) — set them in the pipeline definition or use `--env-file`.

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
>>> step 1 intake
step 1 ok  -> 2 outputs
>>> step 2 refine
step 2 ok  -> 2 outputs
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
| `--open-report` | false | Open the built-in report in your browser when the run finishes (not needed with `--report allure` / `both` — those auto-open the Allure UI) |
| `--log-level LEVEL` | info | `info \| debug \| trace` |
| `--env-file PATH` | — | Path to a `.env` file to load (values never appear in logs) |
| `--no-hitl` | false | Disable interactive prompts (CI mode) |
| `--cache / --no-cache` | auto | Prompt caching: auto-enabled when BMF sticky-session header is detected, disabled otherwise. `--cache` forces on, `--no-cache` forces off |
| `--dev-locators PATH` | — | Dev-supplied locator file for JIT resolution (highest-priority Tier 1) |
| `--storage-state PATH` | — | Playwright `storageState.json` injected into the Step 9 heal-agent's Playwright MCP browser so it skips the 10-30 s auth-replay per heal call. Resolution priority: this flag > `WORCA_T_STORAGE_STATE` > `<sut>/.worca-t/storage-state.json` (from `worca-t auth-capture`) > `<workspace>/storage-state.json` (auto-captured by the runtime on first passing test of the current run). |
| `--no-cleanup` | false | Keep step artifacts when using `--from-step` (default cleans target step onward) |
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
│   ├── step07/   code-modification-plan.json
│   ├── step08/   generated test files, tbd-index.json
│   ├── step09/   run-results.json, screenshots/, traces/, locator-cache.json
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

```bash
# Re-run only the report (after editing bug classifications)
worca-t run --spec ./spec.md --sut ./app --only-step 11

# Resume after a failure — pipeline auto-resumes from the last completed step
worca-t run --spec ./spec.md --sut ./app

# Force a clean re-run (ignore checkpoints)
worca-t run --spec ./spec.md --sut ./app --force

# Re-run from a specific step
worca-t run --spec ./spec.md --sut ./app --from-step 7

# Debug a failing step — verbose debug agent from step 1
worca-t run --spec ./spec.md --sut ./app --debug

# Fix proposal when a step fails twice (suggestions only — never auto-edits)
worca-t run --spec ./spec.md --sut ./app --fix
```

Debug artifacts land in `.worca-t/<run-id>/debug/`.

### Skip Xray upload (or enforce it)

```bash
worca-t run --spec ./spec.md --sut ./app                # default: step 5 auto-skips when JIRA_XRAY creds are unset
worca-t run --spec ./spec.md --sut ./app --strict-xray  # enforce: fail the pipeline if Xray upload doesn't succeed
```

### Storage-state reuse (skip auth in Step 9 self-heal)

Step 9's self-heal Playwright MCP browser runs in a separate process from the test runner, so it inherits no cookies and would otherwise replay sign-in per heal call (10-30 s). Two ways to avoid that:

**When do you need `auth-capture`?** When your SUT sits behind a login that can't be automated inline — MFA (Okta push, hardware key, TOTP), SSO/SAML redirects, or CAPTCHA-protected pages. You run it once interactively, complete the challenge manually in the headed browser, and worca-t saves the session for all subsequent runs. If your SUT uses standard username/password auth that tests can replay on their own, you don't need it — the auto-capture default below handles that.

**Auto-capture (default, no setup):** the vendored pytest runtime captures `context.storage_state()` on the first passing test, writes `<workspace>/storage-state.json`, and Step 9 injects `--storage-state=<path>` into Playwright MCP. Works whenever tests can authenticate on their own (no MFA / captcha).

**One-shot capture (for MFA / SSO / captcha):**

```bash
worca-t auth-capture --sut ./path-to-your-app   # interactive headed browser — user completes MFA/SSO once.
                                                # Output: <sut>/.worca-t/storage-state.json (convention path).
worca-t run --spec ./feature-spec.md --sut ./path-to-your-app   # subsequent runs reuse it automatically.
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--sut PATH` | required | SUT root with `.worca-t/sut_inventory.json` from a prior `worca-t run` Step 6 |
| `--output / -o PATH` | `<sut>/.worca-t/storage-state.json` | Output path |
| `--headed / --headless` | headed | Keep headed for interactive MFA |
| `--timeout N` | 600 | Subprocess timeout (seconds) |

**Explicit override:** pass `--storage-state PATH` to `worca-t run` to force a specific file. **`auth-capture` supports Python and Node.js (JS/TS) Playwright SUTs.** Java / .NET / Selenium / Cypress / Robot SUTs raise `NotImplementedError` — produce a Playwright-format `storageState.json` manually and pass it via `--storage-state`.

## 9. The 11 steps explained

| # | Step | What it does |
| --- | --- | --- |
| 1 | **Intake** | Fetches the spec from Jira, URL, or local file |
| 2 | **Refine** | AI refines the spec into structured requirements |
| 3 | **Plan** | AI creates a test plan with phases and success criteria |
| 4 | **Strategy** | AI generates test cases (TC-IDs) with steps and priorities |
| 5 | **Xray** | Uploads test cases to Xray Cloud (auto-skips if no creds) |
| 6 | **Research** | AI analyzes the SUT codebase, detects stack and patterns |
| 7 | **Test Architect** | AI emits `code-modification-plan.json` — per-test-case placement decisions (fixtures/POM methods/locators to reuse vs create) |
| 8 | **Codegen** | AI transpiles the plan into executable test code; emits `tbd(...)` sentinels for unresolved locators |
| 9 | **Execute** | Runs tests; JIT resolves `tbd(...)` at runtime (Playwright stacks) or self-heals via agent on failure (non-Playwright) |
| 10 | **Classify** | AI classifies failures into structured bug reports |
| 11 | **Report** | Generates HTML report + optional Allure report |

## 10. Reporting options

```bash
--report auto       # (default) built-in HTML + Allure when CLI present
--report builtin    # built-in HTML only (zero dependencies)
--report allure     # Allure only (requires allure CLI; auto-opens Allure UI)
--report both       # always generate both (auto-opens Allure UI)

--report-inline-images  # embed screenshots as base64 in HTML
--open-report           # open the built-in report in your browser when done
                        # (not needed with --report allure / both — those auto-open)
```

## Developing worca-t locally

The installed wheel holds **two kinds of frozen content** that don't auto-update on source edits:

| Edited content | How to pick it up |
| --- | --- |
| Markdown resources: `agents/`, `templates/`, `schemas/`, `skills/`, `examples/`, `CLAUDE.md`, `.mcp.json` | Set `WORCA_T_RESOURCE_ROOT=<repo-root>` — runner reads from there instead of the frozen `_resources/` snapshot. **No reinstall needed.** |
| Python code: `src/worca_t/**.py` | Reinstall the tool **or** run the dev venv binary (editable install). The env var does **not** help — Python imports come from site-packages. |

```bash
# A) Resource-root env var (markdown only):
export WORCA_T_RESOURCE_ROOT=/path/to/worca-t       # bash / zsh
$Env:WORCA_T_RESOURCE_ROOT = "C:\path\to\worca-t"   # PowerShell
set WORCA_T_RESOURCE_ROOT=C:\path\to\worca-t        # cmd

# B) Reinstall the tool (covers both markdown + Python):
uv tool install --reinstall --force /path/to/worca-t

# C) Dev venv binary (editable; pair with WORCA_T_RESOURCE_ROOT for markdown):
/path/to/worca-t/.venv/bin/worca-t run ...                       # Unix
C:\path\to\worca-t\.venv\Scripts\worca-t.exe run ...             # Windows
```

CI / non-interactive: pass `--no-hitl` to `worca-t run`.

## Next steps

- See `docs/qa-orchestrator.instructions.md` for the full operator reference
- See `CHANGELOG.md` for version history
- Run `worca-t doctor` any time to diagnose environment issues
