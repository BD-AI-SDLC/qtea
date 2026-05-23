"""Step 7: TDD codegen via ui-test-automation.

Inputs: test-strategy.md, plan.md, research.md (+ refined-spec.md).
Behavior:
  1. Stage inputs + the matching skill (`playwright-generate-test` if pw stack,
     else `webapp-testing`) plus the agent prompt.md sidecar.
  2. Run the agent; it MUST write generated tests under `./tests/`.
  3. Copy `tests/` into artifacts/step07/tests/.
  4. Index the result via `test_indexer.index_tests`.
  5. Enforce non-negotiable rules: if any violation -> FAIL the step.

Outputs (artifacts/step07/):
  - tests/...                 (mirrored test source files)
  - tests-with-tbd.json       (index + violations)
  - violations.log            (only when violations exist)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.test_indexer import index_tests, resolve_framework, violations_summary

log = get_logger(__name__)


def _read_detected_stack(ctx: StepContext) -> str | None:
    research_json = ctx.workspace.step_dir(6) / "research.json"
    if not research_json.exists():
        return None
    try:
        return json.loads(research_json.read_text(encoding="utf-8")).get("detected_stack")
    except (OSError, json.JSONDecodeError):
        return None


def _select_skills(detected_stack: str | None) -> list[str]:
    if not detected_stack:
        return ["webapp-testing"]
    if "playwright" in detected_stack:
        return ["playwright-generate-test", "webapp-testing"]
    return ["webapp-testing"]


class CodegenStep(Step):
    number = 7
    name = "codegen"
    timeout_s = step_timeout(7)

    def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        strategy_md = ctx.workspace.step_dir(4) / "test-strategy.md"
        plan_md = ctx.workspace.step_dir(3) / "plan.md"
        research_md = ctx.workspace.step_dir(6) / "research.md"
        refined_md = ctx.workspace.step_dir(2) / "refined-spec.md"

        if not strategy_md.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"missing {strategy_md}; run step 4 first",
            )

        inputs = {"test-strategy.md": strategy_md}
        if plan_md.exists():
            inputs["plan.md"] = plan_md
        if research_md.exists():
            inputs["research.md"] = research_md
        if refined_md.exists():
            inputs["refined-spec.md"] = refined_md

        detected_stack = _read_detected_stack(ctx)

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "ui-test-automation.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in _select_skills(detected_stack):
            p = skills_root / skill
            if p.exists():
                extras.append(p)

        stack_hint = f"Detected stack: `{detected_stack}`. " if detected_stack else ""
        result = run_agent(
            agent,
            workdir=wd,
            inputs=inputs,
            user_prompt=(
                f"{stack_hint}Read the staged `./test-strategy.md` (and `./plan.md`, "
                f"`./research.md`, `./refined-spec.md` if present). Generate "
                f"executable test code under `./tests/` according to your "
                f"agent prompt (ui-test-automation.prompt.md). "
                f"Use the framework matching the detected stack. "
                f"Hard rules: locator priority `id > data-testid > role > "
                f"label > text > placeholder > scoped css`; NO XPath; NO hard "
                f"waits (no `time.sleep`, no `cy.wait(<number>)`, no "
                f"`waitForTimeout`); NO `page.content()` - use AOM snapshots; "
                f"no inline credentials. Mark unresolved selectors with the "
                f"literal `TBD_LOCATOR` so the locator-resolution step can "
                f"replace them."
            ),
            extra_paths=extras,
            timeout_s=self.timeout_s,
            step=7,
            max_turns=60,
            claude_md=claude_md if claude_md.exists() else None,
        )

        produced = wd / "tests"
        if not result.success or not produced.exists() or not any(produced.rglob("*")):
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "agent did not produce any files under ./tests/",
            )

        # Mirror tests into the artifact dir for stable downstream references.
        tests_dst = out_dir / "tests"
        if tests_dst.exists():
            shutil.rmtree(tests_dst)
        shutil.copytree(produced, tests_dst)

        framework = resolve_framework(detected_stack, tests_dst)
        index = index_tests(tests_dst, framework=framework)
        payload = index.as_dict()

        index_path = out_dir / "tests-with-tbd.json"
        index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        ok_schema, schema_err = is_valid(payload, "tests-with-tbd")
        if not ok_schema:
            log.warning("step07.schema_invalid", error=schema_err)

        if index.violations:
            summary = violations_summary(index)
            (out_dir / "violations.log").write_text(summary, encoding="utf-8")
            log.error(
                "step07.violations",
                count=len(index.violations),
                framework=framework,
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, out_dir / "violations.log"],
                error=f"non-negotiable rule violations: {len(index.violations)}",
                notes=summary[:500],
            )

        if not index.tests:
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path],
                error="indexer found 0 tests in produced output",
            )

        notes = (
            f"framework={framework} files={len(index.files)} "
            f"tests={len(index.tests)} tbd={sum(len(t.tbd_markers) for t in index.tests)}"
        )
        if not ok_schema:
            notes += f"; schema_warning={schema_err}"
        return StepResult(
            success=True,
            status="completed" if ok_schema else "warned",
            outputs=[index_path, tests_dst],
            notes=notes,
        )
