# Getting Started with qtea

> End-to-end guide: from installation to your first fully autonomous QA run.

## Prerequisites

| Tool | Required? | Check | Install |
| --- | --- | --- | --- |
| uv | Yes | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python 3.11+ | Yes | `python --version` | python.org or `uv python install` (latest) |
| Claude Code CLI | Yes | `claude --version` | `npm install -g @anthropic-ai/claude-code` |
| npx | Yes | `npx --version` | Bundled with Node.js (install nodejs.org) |
| Allure CLI | Auto (via npx) | `allure --version` | Bundled automatically via npx — no manual install needed |

## 1. Install qtea

```bash
git clone https://github.com/BD-AI-SDLC/qtea.git
cd qtea
uv tool install '.[ui]'
qtea --help
qtea ui          # opens the desktop configuration window
```

This installs both the CLI (`qtea run`, `qtea doctor`, …) and the Flet-based desktop UI (`qtea ui`).

**CLI only** (headless / CI environments):

```bash
uv tool install .
```

### Quick example of using QTea CLI

```bash
qtea run --spec ./feature-spec.md --sut ./my-app
```

## 2. Configure environment

### User / system environment variables

Set these as persistent **user** variables, not in a `.env` file.

**Windows:** System → Advanced system settings → Environment Variables → **User variables** → New. Open a fresh terminal afterwards.
**macOS / Linux:** add `export VAR=value` to `~/.bashrc` or `~/.zshrc`.

**LLM backend (BMF / Vertex proxy):**

```env
ANTHROPIC_API_KEY=sk-ant-api03-... 
ANTHROPIC_VERTEX_BASE_URL=https://aoai-farm.bosch-temp.com/api/google/v1
ANTHROPIC_VERTEX_PROJECT_ID=_
CLOUD_ML_REGION=_
CLAUDE_CODE_USE_VERTEX=1
CLAUDE_CODE_SKIP_VERTEX_AUTH=1
```

**Prompt caching — IMPORTANT (BMF only), set this first:**

> Without the BMF sticky-session header the relay does not honour `cache_control` — caching becomes a net **cost loss** (25% creation surcharge, zero read-side payback). With it, qtea auto-enables caching on every step; no `--cache` flag needed.

Pick **one** replica (`01` or `02`) and stick with it across runs for cache locality. Set the below environment variables:

```env
ANTHROPIC_CUSTOM_HEADERS=x-bmf-sticky-session-instance: 01
```

Forwarded to both Claude Code CLI subprocesses and direct Anthropic SDK calls (reasoning, JIT resolver) — every layer benefits automatically.

**Corporate proxy (if applicable):**

```env
HTTP_PROXY=http://localhost:3128
HTTPS_PROXY=http://localhost:3128
```

**Jira** (needed when `--spec` is `jira:KEY` or a Jira URL):

```env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@company.com           # Cloud only
JIRA_API_TOKEN=your-jira-api-token   # Cloud only
JIRA_PAT=your-personal-access-token  # Server / Data Center only
```

**Azure DevOps** (needed when `--spec` is `ado:ID` shorthand; not needed for full URLs or `ado:ORG/PROJECT/ID`):

```env
AZDO_ORG=MyOrg
AZDO_PROJECT=MyProject
AZDO_PAT=your-personal-access-token    # optional if logged in via `az login`
```

**Xray Cloud** (optional — step 5 auto-skips if unset):

```env
JIRA_XRAY_CLIENT_ID=
JIRA_XRAY_CLIENT_SECRET=
```

**Minimum for a local run:** only the LLM backend vars are required. Jira, Azure DevOps, and Xray are optional — steps 1 and 5 auto-adapt.

> **Multiple projects / different credentials per run:** use `--env-file /path/to/.env` instead of user variables.

```bash
qtea run --spec ./spec.md --sut ./app --env-file /path/to/.env.prod
```

### Remote SUT (git URL)

`--sut` accepts git URLs from any major hosting provider (shallow-cloned `--depth=1` into the workspace). Example when using CLI version:

```bash
qtea run --spec ./spec.md --sut https://github.com/org/app.git                     # GitHub / GitLab / Bitbucket
qtea run --spec ./spec.md --sut https://org@dev.azure.com/org/project/_git/repo    # Azure DevOps (HTTPS)
qtea run --spec ./spec.md --sut git@ssh.dev.azure.com:v3/org/project/repo          # Azure DevOps (SSH)
```

`.env` files are gitignored so won't be cloned — use `--env-file` or the interactive prompt for missing values.

### Azure DevOps integration

`AZDO_PAT` is shared across two features: **work item intake** (Step 1) and **Variable Group env resolution** (Step 6). If you're logged in via `az login`, the PAT is optional — qtea falls back to Azure CLI OAuth tokens automatically.

> **`az login` covers authentication only** — it provides the access token but not the org/project name. `AZDO_ORG` and `AZDO_PROJECT` are needed only for the bare `ado:ID` shorthand. The self-contained forms (`ado:Org/Project/ID` or a full URL) work without any env vars beyond auth.

**Finding your org and project names:** look at any work item URL — the two path segments after `dev.azure.com` are your values:

```
https://dev.azure.com/BoschGPT/MyProject/_workitems/edit/9370
                       ^^^^^^^^ ^^^^^^^^^
                       AZDO_ORG AZDO_PROJECT
```

```bash
AZDO_ORG=BoschGPT
AZDO_PROJECT=MyProject
```

| Env Var | Used by | Purpose |
| --- | --- | --- |
| `AZDO_ORG` | Step 1 (`ado:ID` shorthand only), Step 6 | Default organization — only needed when using `ado:9370` bare shorthand or Variable Groups |
| `AZDO_PROJECT` | Step 1 (`ado:ID` shorthand only), Step 6 | Default project — same conditions as `AZDO_ORG` |
| `AZDO_PAT` | Step 1 (work item fetch), Step 6 | PAT with **Work Items (Read)** + **Variable Groups (Read)** scopes. Optional if `az login` is active. |
| `AZDO_VARIABLE_GROUP` | Step 6 only | Variable Group name to read from |

When `AZDO_ORG`, `AZDO_PROJECT`, `AZDO_VARIABLE_GROUP`, and a valid auth (PAT or `az login`) are present, Step 6 queries the Variable Group and resolves matching SUT keys.

```yaml
variables:
  - group: my-qa-variables                                       # Variable Group in Library
  - { name: AZDO_ORG,            value: MyOrg }
  - { name: AZDO_PROJECT,        value: MyProject }
  - { name: AZDO_VARIABLE_GROUP, value: my-qa-variables }
  - { name: AZDO_PAT,            value: $(System.AccessToken) }  # or a secret-bound PAT
steps:
  - script: qtea run --spec jira:PROJ-123 --sut $(SUT_REPO_URL) --no-hitl
```

**Note:** Variables marked **secret** in Azure DevOps cannot be retrieved via REST (API returns `null`) — set them in the pipeline definition or use `--env-file`.

> **`--no-hitl` skips the review gates at steps 4, 7, and 8** (test strategy, code-modification plan, TBD intents). qtea is designed for interactive, operator-driven use — those gates are where you catch AI mistakes before they generate or run code. Only use `--no-hitl` for the SUT env-var prompt suppression use case (pre-set all vars via `--env-file`) or in unattended scenarios where you accept the output without review.

### Jira integration

| Env Var | Required when | Auth scheme | Purpose |
| --- | --- | --- | --- |
| `JIRA_BASE_URL` | `jira:KEY` shorthand only | — | Base URL of your Jira instance. Not needed when passing a full Jira URL. |
| `JIRA_EMAIL` | Atlassian Cloud (`*.atlassian.net`) | Basic | Your Atlassian account email. Used together with `JIRA_API_TOKEN`. |
| `JIRA_API_TOKEN` | Atlassian Cloud (`*.atlassian.net`) | Basic | API token from `id.atlassian.com/manage-profile/security/api-tokens`. |
| `JIRA_PAT` | Jira Server / Data Center (on-prem) | Bearer | Personal Access Token from Jira → Profile → Personal Access Tokens. Not used on Cloud. |

qtea auto-detects the scheme from the hostname: `*.atlassian.net` → Cloud (Basic); any other host → Server/DC (Bearer). Override with `JIRA_AUTH_TYPE=cloud` or `JIRA_AUTH_TYPE=datacenter` if auto-detection is wrong.

## 3. Validate your setup

```bash
qtea doctor
```

Expected output: all checks `OK` or `INFO`. Fix any `FAIL` items before
proceeding. Common issues:

| Check | Fix |
| --- | --- |
| claude CLI: FAIL | Install Claude Code CLI or set `QTEA_CLAUDE_BIN=claude` |
| npx: FAIL | Install Node.js (includes npx) |
| ANTHROPIC_API_KEY: WARN | Add it to your `.env` file |
| proxy: INFO | Safe to ignore if you're not behind a corporate proxy |
| allure CLI: INFO | Resolved automatically via npx — no manual install needed |

## 4. Run the full pipeline via CLI

**With a local spec and local SUT:**

```bash
qtea run --spec ./feature-spec.md --sut ./path-to-your-app
```

**With a Jira ticket and a Git repo:**

```bash
qtea run --spec jira:PROJ-123 --sut https://github.com/org/app.git
```

**With an Azure DevOps work item:**

```bash
qtea run --spec ado:9370 --sut ./path-to-your-app                            # shorthand (needs AZDO_ORG + AZDO_PROJECT)
qtea run --spec ado:MyOrg/MyProject/9370 --sut ./path-to-your-app            # self-contained
qtea run --spec "https://dev.azure.com/Org/Proj/_workitems/edit/9370" --sut ./app   # full URL
```

**With an Azure DevOps repo and a separate env file:**

```bash
qtea run --spec jira:PROJ-123 \
  --sut https://org@dev.azure.com/org/project/_git/repo \
  --env-file ./qa.env
```

Common flags:

| Flag | Purpose |
| --- | --- |
| `--spec` | required — `jira:KEY`, `ado:ID`, `ado:ORG/PROJECT/ID`, Azure DevOps URL, or path to spec file |
| `--sut` | required — local path or git URL of the app under test |
| `--run-id` | resume an existing run |
| `--from-step N` | re-run from a specific step |
| `--force` | ignore checkpoints, re-run everything |
| `--headed` | show the browser window (useful for debugging) |
| `--open-report` | open the report automatically when the run finishes |
| `--env-file PATH` | load credentials from a file instead of user env vars |

For the full flag reference: `qtea run --help`

## 5. List and inspect workspaces

```bash
qtea list
```

Shows all workspaces under `~/.qtea`, newest first:

```text
 Workspaces under /home/you/.qtea
 run-id                    status    last  steps  started              spec
 20260523-143012-a1b2c3   finished  11    11     2026-05-23 14:30:12  feature-spec.md
 20260522-091500-b3c4d5   failed    7     7      2026-05-22 09:15:00  jira-PROJ-123
```

Use the `run-id` column with `--run-id` to resume a specific run:

```bash
qtea run --spec ./spec.md --sut ./app --run-id 20260522-091500-b3c4d5
```

## 6. Find your results

All artifacts are under `.qtea/<run-id>/artifacts/`:

```text
.qtea/<run-id>/
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

An Allure report is generated and opened automatically at the end. You can also open manually the Built-in HTML:

```bash
open .qtea/<run-id>/artifacts/step11/index.html
```

## 7. Common workflows

```bash
# Re-run only the report (after editing bug classifications)
qtea run --spec ./spec.md --sut ./app --only-step 11

# Resume after a failure — pipeline auto-resumes from the last completed step
qtea run --spec ./spec.md --sut ./app

# Force a clean re-run (ignore checkpoints)
qtea run --spec ./spec.md --sut ./app --force

# Re-run from a specific step
qtea run --spec ./spec.md --sut ./app --from-step 7

# Also produce debug RCA on attempt-1 failures (attempt-2 always gets it)
qtea run --spec ./spec.md --sut ./app --debug

# Suppress the auto fix-proposal chain (RCA still writes; use for cost-sensitive CI)
qtea run --spec ./spec.md --sut ./app --no-fix
```

Debug artifacts land in `.qtea/<run-id>/debug/`. When a step fails twice, the fix-proposal chain (`debug` → `critical-thinking` → `principal-software-engineer`) auto-fires and writes `step-NN-fix-proposal.md` alongside the aggregated `step-NN-rca.md` — suggestions only, never auto-edits.

### Human-in-the-loop (HITL)

On TTY runs (not `--no-hitl`), the pipeline pauses at several points to ask for your input. There are four kinds:

**Clarification questions — Steps 2 & 3**
The agent may surface a `[CLARIFICATION NEEDED]` question mid-generation when it encounters a missing required fact. You answer inline; the answer is recorded in `user-answers.md` and propagated to all later steps so the same question is never asked twice.

**Missing SUT env vars — Step 6**
If the SUT requires environment variables that are not set (credentials, base URLs, feature flags), the pipeline prompts for them one by one. Answers are stored and re-used on resume. Pre-set them via `--env-file` to skip this entirely.

**Review gates — Steps 4, 7 & 8**
The pipeline pauses after generating a key artifact and asks you to approve before proceeding:

| Gate | Artifact reviewed | Options |
| --- | --- | --- |
| Step 4 — Test Strategy | `test-strategy.md` | `[y]` approve, `[n]` reject (abort), `[f]` open in `$EDITOR` to revise |
| Step 7 — Code-modification plan | `code-modification-plan.md` | `[y]` approve, `[n]` reject (abort), `[f]` open in `$EDITOR` to revise |
| Step 8 — TBD intents | `tbd-index.json` intent quality | `[y]` approve, `[n]` reject (abort) |

Edits made via `[f]` are fed back to the agent for a revision pass before continuing.

**Runtime prompts — Step 9**
During test execution the pipeline may pause for:

- **Unresolvable locators** — a `tbd("intent")` sentinel could not be resolved automatically; you supply the selector once and it is cached for all future runs.
- **Overlay / popup handling** — an unexpected overlay blocked an action; you choose whether to persist the dismiss handler for future runs.
- **Dependency install** — a missing test dependency is detected; you confirm before the package manager runs.

`--no-hitl` bypasses all four kinds. Only use it when you deliberately accept unreviewed output — e.g. a repeat run of a previously approved spec against a known-stable SUT, or a fully pre-configured CI pipeline.

### Skip Xray upload (or enforce it)

```bash
qtea run --spec ./spec.md --sut ./app                # default: step 5 auto-skips when JIRA_XRAY creds are unset
qtea run --spec ./spec.md --sut ./app --strict-xray  # enforce: fail the pipeline if Xray upload doesn't succeed
```

### Storage-state reuse (skip auth in Step 9 self-heal)

Step 9's self-heal Playwright MCP browser runs in a separate process from the test runner, so it inherits no cookies and would otherwise replay sign-in per heal call (10-30 s). Two ways to avoid that:

**When do you need `auth-capture`?** When your SUT sits behind a login that can't be automated inline — MFA (Okta push, hardware key, TOTP), SSO/SAML redirects, or CAPTCHA-protected pages. You run it once interactively, complete the challenge manually in the headed browser, and qtea saves the session for all subsequent runs. If your SUT uses standard username/password auth that tests can replay on their own, you don't need it — the auto-capture default below handles that.

**Auto-capture (default, no setup):** the vendored pytest runtime captures `context.storage_state()` on the first passing test, writes `<workspace>/storage-state.json`, and Step 9 injects `--storage-state=<path>` into Playwright MCP. Works whenever tests can authenticate on their own (no MFA / captcha).

**One-shot capture (for MFA / SSO / captcha):**

```bash
qtea auth-capture --sut ./path-to-your-app   # interactive headed browser — user completes MFA/SSO once.
                                                # Output: <sut>/.qtea/storage-state.json (convention path).
qtea run --spec ./feature-spec.md --sut ./path-to-your-app   # subsequent runs reuse it automatically.
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--sut PATH` | required | SUT root with `.qtea/sut_inventory.json` from a prior `qtea run` Step 6 |
| `--output / -o PATH` | `<sut>/.qtea/storage-state.json` | Output path |
| `--headed / --headless` | headed | Keep headed for interactive MFA |
| `--timeout N` | 600 | Subprocess timeout (seconds) |

**Explicit override:** pass `--storage-state PATH` to `qtea run` to force a specific file. **`auth-capture` supports Python and Node.js (JS/TS) Playwright SUTs.** Java / .NET / Selenium / Cypress / Robot SUTs raise `NotImplementedError` — produce a Playwright-format `storageState.json` manually and pass it via `--storage-state`.

## Next steps

- See `docs/qa-orchestrator.instructions.md` for the full operator reference
- Run `qtea doctor` any time to diagnose environment issues
