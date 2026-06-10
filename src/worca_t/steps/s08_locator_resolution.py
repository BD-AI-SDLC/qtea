"""Step 8: Locator resolution — soft-deleted as of the JIT-runtime refactor.

The previous implementation (~1900 lines) ran a two-agent flow (playwright-tester
for live navigation + polyglot-test-fixer for DOM-truth audit), line-anchored
patcher, snapshot policy, and a 90% apply-rate gate to resolve TBD locators
ahead of test execution.

That responsibility moved to runtime tiers:

  - **Python / TypeScript / JavaScript / Java + Playwright** — Step 7 vendors
    a per-language JIT runtime that intercepts ``tbd("...")`` / ``Tbd.of("...")``
    sentinels at test execution time via dev-locators → cache → in-process
    heuristic → ResolverServer (LLM). Step 9 starts the ResolverServer in
    the trusted parent process; the SUT subprocess never sees ``ANTHROPIC_API_KEY``.

  - **Selenium / Cypress / Robot / other non-Playwright stacks** — Step 9's
    existing ``polyglot-test-fixer`` self-heal flow handles ``TBD_LOCATOR``
    markers on-failure (the agent inspects the live page via Playwright MCP,
    or instructs a one-off native source capture via ``driver.page_source`` /
    ``cy.document()`` / ``Get Source`` when that's more reliable).

This file is preserved for backward compatibility with existing ``state.json``
checkpoints and the ``pipeline.py`` step registration list. It always returns
``status="skipped"`` with a stub artifact so downstream steps see the step
ran but produced no work. Reusable patcher helpers from the old implementation
live in :mod:`worca_t.locator_patching`; auth-context helpers live in
:mod:`worca_t.auth_helpers`.

To fully remove this step (10-step pipeline instead of 11), see the deferred
"renumber" item in the refactor plan — it requires touching every artifact
path, ``state.json`` migration, CLI flag, and doc reference, with zero
behavioural impact, so it's left for a follow-up.
"""

from __future__ import annotations

import json

from worca_t.logging_setup import get_logger
from worca_t.steps.base import Step, StepContext, StepResult

# Re-export auth helpers under their legacy names so any third-party code
# that imported them from this module keeps working.
from worca_t.auth_helpers import (
    auth_relevant_sut_files as _auth_relevant_sut_files,
    auth_summary_for_prompt as _auth_summary_for_prompt,
)

log = get_logger(__name__)


__all__ = [
    "LocatorResolutionStep",
    "_auth_relevant_sut_files",
    "_auth_summary_for_prompt",
]


class LocatorResolutionStep(Step):
    """No-op step. The runtime-tier resolution model (Phases 1-4 of the
    JIT refactor) replaces this step's purpose entirely. We keep it as a
    pass-through so existing ``state.json`` checkpoints resume cleanly and
    so ``pipeline.py``'s step registration doesn't need to renumber.
    """

    number = 8
    name = "locator-resolution"
    timeout_s = 30  # tiny — we just write a stub file

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        stub_path = out_dir / "locator-resolution.json"
        stub_path.write_text(
            json.dumps(
                {
                    "mode": "soft-deleted",
                    "note": (
                        "Step 8 has been replaced by runtime-tier locator "
                        "resolution. See agents/qa-orchestrator.instructions.md "
                        "for the current flow."
                    ),
                    "resolutions": [],
                    "status": "skipped",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        log.info("step08.soft_deleted_stub_written", path=str(stub_path))
        return StepResult(
            success=True,
            status="skipped",
            outputs=[stub_path],
            notes=(
                "Step 8 is soft-deleted. Locator resolution happens at "
                "test runtime (JIT for PW stacks) or as Step 9 on-failure "
                "heal for non-PW stacks."
            ),
        )
