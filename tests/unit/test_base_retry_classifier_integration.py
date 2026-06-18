"""Integration test: base.py invokes the classifier on failed attempts and
propagates fix_hint into ctx.extras BEFORE the retry fires.

This is a thin behavioural check — we don't re-test category-detection
(that's in test_failure_classifiers.py) and we don't re-test smart-retry
consumption (that's in test_step08_smart_retry.py). We just verify the
wiring at the seam: a Step that fails with a known recoverable error
sees the corresponding fix_hint applied on attempt 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.workspace import create_workspace

# --- Test scaffolding ------------------------------------------------------


def _make_ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=str(ws.sut),
    )
    opts = PipelineOptions(
        spec="x", sut=str(ws.sut), workspace_base=tmp_path / ".ws",
    )
    return StepContext(
        workspace=ws, state=state,
        spec_source="x", sut_source=str(ws.sut), options=opts,
    )


@dataclass
class _RecordingStep(Step):
    """A Step that fails attempt 1 with a canned error, succeeds on attempt 2.

    Records the value of ``ctx.extras["prompt_clarification"]`` it observes
    on each attempt so the test can assert the classifier's fix_hint
    propagated correctly between attempts.
    """

    number: int = 99
    name: str = "test-recording-step"
    timeout_s: int | None = 60
    error_on_first_attempt: str = ""
    observed_clarifications: list[str | None] = field(default_factory=list)
    _attempts_seen: int = 0

    async def run(self, ctx: StepContext) -> StepResult:
        self._attempts_seen += 1
        # Snapshot what the step sees on this attempt — the test asserts
        # that attempt 2 sees the classifier's clarification while
        # attempt 1 sees nothing.
        self.observed_clarifications.append(
            ctx.extras.get("prompt_clarification")
        )
        if self._attempts_seen == 1:
            return StepResult(
                success=False, status="failed", outputs=[],
                error=self.error_on_first_attempt,
            )
        return StepResult(
            success=True, status="completed", outputs=[],
        )


# --- Tests -----------------------------------------------------------------


async def test_schema_type_mismatch_propagates_clarification_to_retry(
    tmp_path: Path,
):
    """When attempt 1 fails with schema-type-mismatch, the classifier's
    fix_hint must land on ctx.extras before attempt 2's run() executes."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "plan failed schema validation: 'a string here' is not of type 'array'"
        ),
    )

    result = await step.execute(ctx)

    # Step succeeded (attempt 2 returned success=True).
    assert result.success is True
    # Both attempts ran.
    assert step._attempts_seen == 2
    # Attempt 1 saw nothing; attempt 2 saw the clarification.
    assert step.observed_clarifications[0] is None
    assert step.observed_clarifications[1] is not None
    assert "type 'array'" in step.observed_clarifications[1]
    # The classifier also stashes the category label.
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "schema_type_mismatch"
    )


async def test_unknown_failure_propagates_no_hint(tmp_path: Path):
    """An unmatched error string classifies as UNKNOWN; no fix_hint is set,
    so attempt 2 sees an empty ctx.extras for prompt_clarification."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(error_on_first_attempt="something we don't recognize")

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[0] is None
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == "unknown"


async def test_truncation_propagates_label_but_no_hint(tmp_path: Path):
    """TRUNCATION_RECOVERABLE is labelled but produces no classifier-side
    fix_hint — the step owns its own override key (see s08_codegen)."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt="step08.pom_syntax_invalid at line 585 of 585",
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    # No clarification because the truncation classifier doesn't produce one.
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "truncation_recoverable"
    )


async def test_api_fatal_does_not_retry(tmp_path: Path):
    """API_FATAL is classified but the existing _is_api_fatal_error early-
    return still wins — attempt 2 never runs (preserves the original
    no-retry-on-fatal behaviour)."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt="API fatal error: HTTP 401 Unauthorized",
    )

    result = await step.execute(ctx)

    assert result.success is False
    # Critical: only ONE attempt ran. The classifier labelled it and base.py
    # honored the no-retry semantics.
    assert step._attempts_seen == 1
    assert ctx.extras.get(f"step{step.number}_failure_category") == "api_fatal"


# --- Recoverable categories: fix_hint propagation -------------------------


async def test_schema_missing_required_field_propagates_clarification(
    tmp_path: Path,
):
    """SCHEMA_MISSING_REQUIRED_FIELD propagates a prompt_clarification that
    names the missing field — same wiring as schema_type_mismatch but
    triggered by jsonschema's "is a required property" message."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "plan failed schema validation: "
            "'test_file_target' is a required property"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[0] is None
    assert step.observed_clarifications[1] is not None
    assert "test_file_target" in step.observed_clarifications[1]
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "schema_missing_required_field"
    )


async def test_json_unparseable_propagates_clarification(tmp_path: Path):
    """JSON_UNPARSEABLE propagates a generic JSON clarification — the agent
    is told to respond with a JSON object only, no prose."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "plan JSON unparseable: Expecting value: line 1 column 1 (char 0)"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[0] is None
    assert step.observed_clarifications[1] is not None
    assert "JSON" in step.observed_clarifications[1]
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "json_unparseable"
    )


# --- Non-recoverable categories: label set, no hint, retry still runs -----


async def test_agent_no_output_labels_but_no_hint(tmp_path: Path):
    """AGENT_NO_OUTPUT is non-recoverable (safe_to_auto_retry=False) but
    base.py's only early-return is API_FATAL — so attempt 2 still runs,
    just without any classifier-provided hint."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(error_on_first_attempt="agent produced no output")

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "agent_no_output"
    )


async def test_locator_resolution_timeout_labels_but_no_hint(tmp_path: Path):
    """LOCATOR_RESOLUTION_TIMEOUT is labelled; no fix_hint (deferred to
    JIT/heal flows). Retry still runs because there's no early-return for
    this category."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "TBD locator unresolved after 30s timeout: 'sign in button'"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "locator_resolution_timeout"
    )


async def test_sut_git_failure_labels_but_no_hint(tmp_path: Path):
    """SUT_GIT_FAILURE is labelled; no fix_hint. Retry still runs."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "git commit failed: nothing to commit, working tree clean"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "sut_git_failure"
    )


async def test_test_runner_error_labels_but_no_hint(tmp_path: Path):
    """TEST_RUNNER_ERROR is labelled; no fix_hint. Retry still runs even
    though the classifier marks it non-recoverable — base.py only honors
    the no-retry semantics for API_FATAL."""
    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "pytest collection failed: E ImportError: "
            "cannot import name 'X' from 'tests.fixtures'"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    assert step._attempts_seen == 2
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "test_runner_error"
    )


async def test_api_retry_storm_labels_and_retries_on_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """API_RETRY_STORM is labelled. On a non-TTY (CI / tests), the
    interactive storm-prompt branch in base.py is skipped and the default
    `decision="retry"` lets attempt 2 fire immediately. The category label
    still lands on ctx.extras for downstream consumers."""
    # Force the non-interactive path regardless of where the test runs.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    ctx = _make_ctx(tmp_path)
    step = _RecordingStep(
        error_on_first_attempt=(
            "SDK api_retry storm: 5 consecutive 502s on Vertex partner endpoint"
        ),
    )

    result = await step.execute(ctx)

    assert result.success is True
    # Retry fired despite the storm — the interactive prompt is gated to TTY.
    assert step._attempts_seen == 2
    assert step.observed_clarifications[1] is None
    assert ctx.extras.get(f"step{step.number}_failure_category") == (
        "api_retry_storm"
    )
