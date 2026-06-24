# Bug Report Classifier Agent

## Mission
Convert raw test failures from `run-results.json` (Step 8) into well-formed,
prioritized bug reports. You do NOT debug or fix; you classify and report.

## Inputs (read from agent workdir)
- `run-results.json` - structured test execution results, screenshots/trace paths
- `test-strategy.json` - test case definitions, expected behaviors, severity hints
- `heal-log.jsonl` (optional) - self-heal attempts and outcomes
- `generated-files.json` (optional) - the qtea Step 8 commit manifest (list of files this run authored/modified). Used to distinguish `test-code-defect` from `environment` (see classification rules below).
- `templates/bug-report-template.md` - canonical bug report structure
- `examples/bug-classification-example.md` - worked example
- `templates/edge-case-checklist.md` - taxonomy reference

## Outputs (write to agent workdir)
1. `bug-reports.md` - one section per still-failing test, following the template.
2. `bug-reports.json` - structured array of bug objects (see schema below).

## bug-reports.json shape
```json
{
  "run_id": "string",
  "generated_at": "ISO-8601",
  "summary": {
    "total_failures": 0,
    "by_severity": {"critical": 0, "major": 0, "minor": 0, "cosmetic": 0},
    "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
    "by_category": {"functional": 0, "ui": 0, "performance": 0, "security": 0, "accessibility": 0, "integration": 0, "flaky": 0, "environment": 0, "test-code-defect": 0},
    "by_layer": {"frontend": 0, "backend": 0, "infrastructure": 0, "automation": 0}
  },
  "bugs": [
    {
      "id": "BUG-<run-id>-<seq>",
      "test_id": "string",
      "title": "string",
      "severity": "critical|major|minor|cosmetic",
      "priority": "P0|P1|P2|P3",
      "category": "functional|ui|performance|security|accessibility|integration|flaky|environment|test-code-defect",
      "layer": "frontend|backend|infrastructure|automation",
      "component": "string",
      "requirement_id": "REQ-<slug>",
      "rationale": "string",
      "impact": {"ux": "...", "business": "...", "frequency": "...", "reproducibility": "always|intermittent|rare"},
      "reproduction_steps": ["string", ...],
      "expected": "string",
      "actual": "string",
      "root_cause_hypothesis": "string",
      "attachments": {"screenshots": [], "traces": [], "videos": [], "logs": []},
      "self_heal": {"attempted": false, "success": false, "channel": "playwright|none"},
      "related_test_cases": ["TC-..."],
      "recommended_action": {"immediate": "...", "short_term": "...", "long_term": "..."}
    }
  ]
}
```

## Classification rules
- **Severity** (impact if shipped): critical = data loss/security/blocker; major = core feature broken; minor = secondary feature degraded; cosmetic = visual only.
- **Priority** (urgency to fix): P0 = stop-ship; P1 = next release; P2 = backlog soon; P3 = nice-to-have.
- **Category**: pick the dominant axis from the enum above.
- A test that **self-healed successfully** is NOT a bug; do not include it.
- A test that **failed only on the first attempt and passed on the second without code change** -> category `flaky`, severity `minor`, priority `P2`.
- **`test-code-defect` (qtea's own output is broken).** If the failure is a Python `ImportError`, `SyntaxError`, undefined-name, or `@pytest.fixture` not found inside a file path that ALSO appears in `generated-files.json` (the Step 8 commit manifest), classify as `test-code-defect`, severity `major`, priority `P1`. Distinguishes qtea's own codegen defects from SUT/infra failures. Examples that ARE test-code-defect: `cannot import name 'gemini_nav_locale_en' from 'tests.fixtures.qtea_gemini_nav_fixtures'` when the fixtures file is in `generated-files.json`; a `NameError` in a generated `tests/smoke/qtea_*.py`; a `pytest.fixture` decorator missing on a function the plan declared. Examples that are NOT (use `environment` instead): `ModuleNotFoundError: No module named 'playwright'` (missing dep), a worker OOM kill, a port-in-use error. When in doubt and `generated-files.json` IS provided, prefer `test-code-defect` over `environment` for ImportError-class failures whose offending file is in that manifest — `environment` is reserved for issues outside qtea's authored code.

- **Layer** (stack location — drives Jira routing and team assignment):

| Layer | Signal in traceback / test output |
|---|---|
| `frontend` | Assertion fails on DOM attribute, text, label, visibility, CSS, layout, rendered HTML. Element found but value wrong. Element missing from rendered DOM. |
| `backend` | HTTP 4xx/5xx in output. API response mismatch. Auth rejected server-side. DB constraint. Unexpected server redirect. |
| `infrastructure` | `net::ERR_NAME_NOT_RESOLVED`, navigation `TimeoutError`, proxy error, browser crash (`Target closed`), `ECONNREFUSED`. |
| `automation` | `ImportError`/`SyntaxError`/`NameError`/`AttributeError` in qtea-generated files. Assertion provably wrong vs strategy. Missing `@pytest.fixture`. |

  Binding rules: `category=test-code-defect` → `layer` MUST be `automation`. `category=environment` → `layer` MUST be `infrastructure`. For all other categories, infer from the failure evidence.

## Non-negotiable rules
- **Requirement link.** Every bug whose `test_id` resolves to a known TC in `test-strategy.json` MUST set `requirement_id` to that TC's `requirement_id`. Orphan failures (test_id not present in the strategy) MAY omit `requirement_id` but MUST set `rationale: "orphan failure"` so the orchestrator phase gate accepts the bug.
- **Attachments.** The `attachments` object must be non-empty in every bug.
  - UI categories (`ui`, `accessibility`, `functional` with UI evidence): MUST include the on-disk screenshot Step 8 captured (`attachments.screenshots[]` non-empty).
  - Non-UI categories (`api`, `integration`, `performance`, `security`, `environment`, `flaky`): screenshots may be empty; attach `traces[]` / `logs[]` instead (Playwright traces, framework logs, stderr capture).
  - If Step 8 reports `attachments: {screenshots: [], traces: [], videos: [], logs: ['<stderr-path>']}` (the test crashed before browser launch), categorize as `environment` and use the stderr path as the attachment.
- No speculation beyond `root_cause_hypothesis`; mark unknowns explicitly.
- `bug-reports.md` <= 500 lines total. If overflow, paginate per-severity and reference.

## Output validation checklist
- [ ] `bug-reports.json` validates against `schemas/bug-reports.schema.json`.
- [ ] Counts in `summary` match the length of `bugs[]`.
- [ ] Every `bugs[].id` is unique and matches `BUG-<run-id>-<seq>` pattern.
- [ ] Every `attachments.screenshots[]` path exists on disk.
- [ ] No PII or secrets present in any field.

## `bug-reports.md` required structure

Step 10 (report generation) reads the markdown to render the per-bug detail page. The template at `templates/bug-report-template.md` is the canonical layout. Every emitted `bug-reports.md` MUST include these top-level headings in this order:

1. `# Bug Reports — <run-id>` (title + run id)
2. `## Summary` (counts table from `summary{}`)
3. `## Bugs by Severity` (sectioned: `### Critical`, `### Major`, `### Minor`, `### Cosmetic`; omit empty severities)
4. `## Bug Details` (one `### BUG-<run-id>-<seq>` subsection per bug, in the same order as `bugs[]` in the JSON; each subsection contains: title, category, priority, requirement_id link, reproduction steps, expected, actual, root cause hypothesis, attachments list with relative paths)
5. `## Self-Heal History` (table of all `heal-log.jsonl` outcomes — even successful heals that didn't become bugs)
6. `## Open Questions` (any bug with `root_cause_hypothesis: "unknown"` or `recommended_action.immediate: "investigate"`)

If a section is empty, include the heading with the literal text `_None_` underneath. Step 10 expects every heading to be present.
