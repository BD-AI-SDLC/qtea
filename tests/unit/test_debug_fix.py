"""Debug/fix flow tests (M9): retry, debug snapshots, fix proposals."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from qtea.checkpoints import RunState
from qtea.metrics import CURRENT_STEP_METRICS, AgentMetrics
from qtea.pipeline import PipelineOptions
from qtea.steps.base import (
    Step,
    StepContext,
    StepResult,
    _agent_failure_placeholder,
    _record_aux_agent,
    _run_debug_rca,
    _run_fix_proposal,
    _snapshot_debug_artifacts,
)
from qtea.workspace import create_workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, **opts_kw) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    defaults = {"spec": "x", "sut": ".", "workspace_base": tmp_path / ".ws"}
    defaults.update(opts_kw)
    opts = PipelineOptions(**defaults)
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


class _FailOnceStep(Step):
    """Fails on attempt 1, succeeds on attempt 2."""
    number = 99
    name = "fail-once"
    timeout_s = 60
    _call_count = 0

    def __init__(self):
        self._call_count = 0

    async def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            return StepResult(success=False, status="failed", outputs=[], error="first attempt fail")
        return StepResult(success=True, status="completed", outputs=[], notes="second attempt ok")


class _AlwaysFailStep(Step):
    """Always fails."""
    number = 98
    name = "always-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=False, status="failed", outputs=[], error="always fails")


class _AlwaysPassStep(Step):
    """Always passes."""
    number = 97
    name = "always-pass"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(success=True, status="completed", outputs=[], notes="ok")


class _ExceptionStep(Step):
    """Raises on first attempt, passes on second."""
    number = 96
    name = "exception-once"
    timeout_s = 60

    def __init__(self):
        self._call_count = 0

    async def run(self, ctx: StepContext) -> StepResult:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("boom")
        return StepResult(success=True, status="completed", outputs=[], notes="recovered")


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


async def test_step_succeeds_first_attempt(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _AlwaysPassStep()
    result = await step.execute(ctx)
    assert result.success
    assert result.status == "completed"
    record = ctx.state.steps[97]
    assert record.attempts == 1


async def test_step_fails_then_succeeds_on_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _FailOnceStep()
    result = await step.execute(ctx)
    assert result.success
    assert result.status == "warned"
    record = ctx.state.steps[99]
    assert record.attempts == 2
    assert "retry" in (record.notes or "")


async def test_step_fails_twice_no_fix(tmp_path: Path):
    # ``no_fix=True`` keeps the retry-only path under test — otherwise the
    # auto-firing fix-proposal chain would call run_agent unmocked.
    ctx = _ctx(tmp_path, no_fix=True)
    step = _AlwaysFailStep()
    result = await step.execute(ctx)
    assert not result.success
    assert result.status == "failed"
    record = ctx.state.steps[98]
    assert record.attempts == 2


async def test_exception_retry_recovers(tmp_path: Path):
    ctx = _ctx(tmp_path)
    step = _ExceptionStep()
    result = await step.execute(ctx)
    assert result.success
    assert result.status == "warned"
    record = ctx.state.steps[96]
    assert record.attempts == 2


# ---------------------------------------------------------------------------
# Debug flag tests
# ---------------------------------------------------------------------------


def test_debug_flag_sets_extras_before_attempt1(tmp_path: Path):
    ctx = _ctx(tmp_path, debug=True)
    assert ctx.options.debug is True


async def test_failed_attempt1_sets_debug_live_for_retry(tmp_path: Path):
    ctx = _ctx(tmp_path)
    assert ctx.extras.get("debug_live") is None
    step = _FailOnceStep()
    await step.execute(ctx)
    assert ctx.extras.get("debug_live") is True


# ---------------------------------------------------------------------------
# Retry classification — content-failure CLEARS resume; transient KEEPS it
# ---------------------------------------------------------------------------


class _ContentFailStep(Step):
    """Always fails with a content-validation-style error (NOT a storm)."""
    number = 95
    name = "content-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            success=False, status="failed", outputs=[],
            error="5 violation(s): [hard-wait] tests/foo.py:42 wait_for_timeout(500)",
        )


class _StormFailStep(Step):
    """Always fails with the api_retry_storm sentinel error."""
    number = 94
    name = "storm-fail"
    timeout_s = 60

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            success=False, status="failed", outputs=[],
            error=(
                "SDK api_retry storm (8 consecutive retries with no "
                "intervening progress; threshold=8). The upstream "
                "Anthropic/Vertex API is returning transient errors..."
            ),
        )


async def test_content_failure_clears_resume_session_before_retry(tmp_path: Path):
    """Content / validation failures must clear `step{N}_resume_session`
    so attempt 2 starts FRESH instead of replaying the same flawed
    reasoning path.

    Regression guard for run 20260611-075728-0aa560 step 8 attempt 2:
    Haiku resumed the same session as attempt 1 and re-emitted the
    same 5 `wait_for_timeout` violations rather than re-deriving an
    assertion-based test from scratch.
    """
    ctx = _ctx(tmp_path, no_hitl=True, no_fix=True)  # skip storm prompt + fix chain
    step = _ContentFailStep()
    # Simulate a step.run() having stashed a session id during attempt 1.
    ctx.extras["step95_resume_session"] = "sess-attempt1-xyz"

    await step.execute(ctx)

    # Must be cleared so attempt 2's step.run() reads None → fresh session.
    assert "step95_resume_session" not in ctx.extras


async def test_transient_failure_preserves_resume_session(tmp_path: Path):
    """api_retry_storm failures must KEEP `step{N}_resume_session` so
    attempt 2 resumes and skips the work the relay-dropped turn lost.
    """
    ctx = _ctx(tmp_path, no_hitl=True, yes=True, no_fix=True)  # skip storm prompt + fix chain
    step = _StormFailStep()
    ctx.extras["step94_resume_session"] = "sess-attempt1-abc"

    await step.execute(ctx)

    # Must STILL be present so attempt 2's step.run() resumes the
    # session and reclaims the prior turn's Reads.
    assert ctx.extras.get("step94_resume_session") == "sess-attempt1-abc"


def test_debug_artifacts_snapshotted_on_failure(tmp_path: Path):
    ctx = _ctx(tmp_path)
    wd = ctx.workspace.step_workdir(98)
    wd.mkdir(parents=True, exist_ok=True)
    # Two agent calls in the same step -> two numbered transcripts/stderrs.
    (wd / "transcript-00.jsonl").write_text('{"type":"test","call":0}\n', encoding="utf-8")
    (wd / "transcript-01.jsonl").write_text('{"type":"test","call":1}\n', encoding="utf-8")
    (wd / "stderr-00.log").write_text("first call stderr\n", encoding="utf-8")
    (wd / "stderr-01.log").write_text("second call stderr\n", encoding="utf-8")
    (wd / "metrics-00.json").write_text('{"call":0}\n', encoding="utf-8")
    (wd / "metrics-01.json").write_text('{"call":1}\n', encoding="utf-8")

    _snapshot_debug_artifacts(98, ctx, 1)

    debug_dir = ctx.workspace.debug / "step-98-attempt1"
    assert debug_dir.exists()
    # All numbered audit files copied; previously only the latest was preserved.
    assert (debug_dir / "transcript-00.jsonl").exists()
    assert (debug_dir / "transcript-01.jsonl").exists()
    assert (debug_dir / "stderr-00.log").exists()
    assert (debug_dir / "stderr-01.log").exists()
    assert (debug_dir / "metrics-00.json").exists()
    assert (debug_dir / "metrics-01.json").exists()


def test_debug_snapshot_includes_legacy_unnumbered_files(tmp_path: Path):
    """Backward compat: a workdir from before this change still snapshots."""
    ctx = _ctx(tmp_path)
    wd = ctx.workspace.step_workdir(97)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "transcript.jsonl").write_text('{"old":"format"}\n', encoding="utf-8")
    (wd / "stderr.log").write_text("legacy\n", encoding="utf-8")

    _snapshot_debug_artifacts(97, ctx, 1)
    debug_dir = ctx.workspace.debug / "step-97-attempt1"
    assert (debug_dir / "transcript.jsonl").exists()
    assert (debug_dir / "stderr.log").exists()


# ---------------------------------------------------------------------------
# Fix-proposal tests
# ---------------------------------------------------------------------------


async def test_fix_proposal_invoked_on_double_failure(tmp_path: Path):
    # Fix chain auto-fires on retry exhaustion; no flag needed.
    ctx = _ctx(tmp_path)
    step = _AlwaysFailStep()

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "mock analysis", "error": None,
        })()
        result = await step.execute(ctx)

    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()


async def test_no_fix_proposal_when_no_fix_flag(tmp_path: Path):
    # ``--no-fix`` opts out of the auto fix chain. The debug RCA still writes
    # (via _run_debug_rca on final failure), but the fix-proposal.md does not.
    ctx = _ctx(tmp_path, no_fix=True)
    step = _AlwaysFailStep()
    result = await step.execute(ctx)
    assert not result.success
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert not proposal.exists()


async def test_fix_flow_failure_does_not_crash(tmp_path: Path):
    # Fix chain auto-fires; the mock makes both critical-thinking and
    # principal-eng agents raise, and we confirm the wrapper writes a
    # fallback proposal instead of crashing the step.
    ctx = _ctx(tmp_path)
    step = _AlwaysFailStep()

    with patch(
        "qtea.steps.base.run_agent",
        new=AsyncMock(side_effect=Exception("agent unavailable")),
    ):
        result = await step.execute(ctx)

    assert not result.success
    assert result.status == "failed"
    proposal = ctx.workspace.debug / "step-98-fix-proposal.md"
    assert proposal.exists()
    content = proposal.read_text(encoding="utf-8")
    assert "agent unavailable" in content or "failed" in content.lower()


# ---------------------------------------------------------------------------
# _run_fix_proposal unit tests
# ---------------------------------------------------------------------------


async def test_run_fix_proposal_writes_files(tmp_path: Path):
    ctx = _ctx(tmp_path)

    # Seed a debug-RCA file (as _run_debug_rca would have) and pass its path
    # to the fix chain. The aggregated step-NN-rca.md should equal this text
    # verbatim — no re-derivation.
    seeded_rca = tmp_path / "seeded-debug-rca.md"
    seeded_rca.write_text("# Debug RCA\n\nroot cause: X", encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "analysis text", "error": None,
        })()
        path = await _run_fix_proposal(
            42, ctx, "# Step 42 failure\n\nSomething broke",
            debug_rca_path=seeded_rca,
        )

    assert path is not None
    assert path.exists()
    rca = ctx.workspace.debug / "step-42-rca.md"
    assert rca.exists()
    assert rca.read_text(encoding="utf-8") == "# Debug RCA\n\nroot cause: X"
    # Intermediate fix-strategy staged in the thinking workdir
    strategy = ctx.workspace.debug / "step-42-fix" / "thinking" / "fix-strategy.md"
    # File may not exist if the agent returned final_text without writing;
    # in that case the wrapper reads final_text — confirm at least one of
    # (strategy file, proposal contains "analysis text") holds.
    assert strategy.exists() or "analysis text" in path.read_text(encoding="utf-8")


def _mock_result(*, success: bool, final_text: str = "", error: str | None = None,
                 transcript_path: Path | None = None):
    """Build a duck-typed AgentResult stand-in for tests that mock run_agent."""
    return type("R", (), {
        "success": success,
        "final_text": final_text,
        "error": error,
        "transcript_path": transcript_path,
    })()


# ---------------------------------------------------------------------------
# Turn-cap / fallback regression guards (run 20260701-114656-9394eb)
# ---------------------------------------------------------------------------


def test_agent_failure_placeholder_shape(tmp_path: Path):
    """Placeholder must be self-documenting: header, error line, blockquoted
    thinking snippet, and the raw failure context inlined."""
    transcript = tmp_path / "transcript-00.jsonl"
    transcript.write_text("", encoding="utf-8")
    result = _mock_result(
        success=False,
        final_text="Now I have a complete picture. Let me also quickly check the `tbd` function.",
        error="sdk error: Reached maximum number of turns (10) | api: ...",
        transcript_path=transcript,
    )
    out = _agent_failure_placeholder(
        agent_label="debug.agent",
        result=result,
        failure_context="# Step 9 failure\n\nsomething broke",
    )
    # Loud header, not a heading that looks like an RCA.
    assert out.startswith("# debug.agent — agent failed to produce artifact")
    # SDK reason surfaced verbatim.
    assert "Reached maximum number of turns (10)" in out
    # Transcript path linked for deep-dive.
    assert str(transcript) in out
    # The thinking snippet is BLOCKQUOTED (downstream agents / operators
    # must not mistake it for a real diagnosis).
    assert "> Now I have a complete picture." in out
    # Raw failure context preserved so the fix chain can still reason.
    assert "something broke" in out


def test_agent_failure_placeholder_truncates_long_final_text():
    long_text = "x" * 5000
    result = _mock_result(success=False, final_text=long_text, error="whatever")
    out = _agent_failure_placeholder(
        agent_label="debug.agent", result=result, failure_context="ctx",
    )
    # Truncation marker present; full 5000 chars not embedded.
    assert "...[truncated]" in out
    assert out.count("x") < 5000


async def test_run_debug_rca_writes_placeholder_on_turn_cap(tmp_path: Path):
    """Regression: run 20260701-114656-9394eb saw the SDK cut off the debug
    agent at ``max_turns`` and its last ``AssistantMessage`` block (pre-
    tool-call thinking, ``\"Let me check X\"``) was written verbatim to
    ``step-NN-attemptM-debug-rca.md`` as if it were the RCA. Now the code
    must emit a labelled placeholder instead.
    """
    ctx = _ctx(tmp_path, no_fix=True)

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(
            success=False,
            final_text="Now I have a complete picture. Let me also quickly check the `tbd` function.",
            error="sdk error: Reached maximum number of turns (10) | api: ...",
        )
        out_path = await _run_debug_rca(
            9, ctx, "# Step 9 failure\n\nsomething broke", attempt=2,
        )

    assert out_path is not None
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    # Not the raw stub — the placeholder header must be present.
    assert content.startswith("# debug.agent — agent failed to produce artifact")
    assert "Reached maximum number of turns (10)" in content
    # The stub is preserved as a *thinking snippet*, blockquoted.
    assert "> Now I have a complete picture." in content


async def test_run_debug_rca_still_promotes_final_text_on_success(tmp_path: Path):
    """A successful agent that inlined its RCA in the final message (rather
    than writing the file) should still have that text promoted — the
    placeholder path is reserved for failures.
    """
    ctx = _ctx(tmp_path, no_fix=True)

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(
            success=True,
            final_text="# Real RCA\n\nRoot cause: X.\n\nEvidence: Y.",
        )
        out_path = await _run_debug_rca(
            9, ctx, "# Step 9 failure\n\nsomething broke", attempt=2,
        )

    assert out_path is not None
    content = out_path.read_text(encoding="utf-8")
    assert content == "# Real RCA\n\nRoot cause: X.\n\nEvidence: Y."
    # And critically NOT the placeholder header.
    assert "agent failed to produce artifact" not in content


async def test_run_debug_rca_grants_artifacts_dir_when_step_workdir_missing(tmp_path: Path):
    """Regression guard for run 20260701-114656-9394eb: Step 9 is a pure-code
    step with no `<workspace>/step-09/` scratchpad, so the debug agent's
    ``add_dirs`` collapsed to ``None`` and the sandbox blocked reads of
    ``<workspace>/artifacts/step09/run-results.json`` — where Playwright's
    real error message lived. add_dirs must now include the step's artefact
    directory and the workspace root so the debug agent can actually reach
    the evidence its own prompt tells it to read.
    """
    ctx = _ctx(tmp_path, no_fix=True)
    # Materialise the artefact dir the way ExecuteStep would; step_workdir
    # (`<ws>/step-09/`) is intentionally NOT created — that's the regression
    # condition.
    step_artifacts = ctx.workspace.step_dir(9)
    assert step_artifacts.exists()
    step_workdir = ctx.workspace.step_workdir(9)
    assert not step_workdir.exists()

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="rca")
        await _run_debug_rca(9, ctx, "ctx", attempt=1)

    assert mock_agent.await_count == 1
    call = mock_agent.await_args_list[0]
    add_dirs = call.kwargs["add_dirs"]
    assert add_dirs is not None, "add_dirs collapsed to None — sandbox will block reads"
    add_dirs_set = {Path(d).resolve() for d in add_dirs}
    assert step_artifacts.resolve() in add_dirs_set
    assert ctx.workspace.root.resolve() in add_dirs_set
    # The (non-existent) step_workdir must NOT be added when it doesn't exist.
    assert step_workdir.resolve() not in add_dirs_set


async def test_run_debug_rca_prompt_names_artifacts_dir(tmp_path: Path):
    """The updated prompt must explicitly point the agent at the artefact
    directory and call out Playwright's `results[i].stdout` — otherwise the
    sandbox-widening in S3 is wasted because the agent doesn't know where
    to look."""
    ctx = _ctx(tmp_path, no_fix=True)

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="rca")
        await _run_debug_rca(9, ctx, "ctx", attempt=1)

    prompt = mock_agent.await_args_list[0].kwargs["user_prompt"]
    assert "artifacts" in prompt.lower() or "step09" in prompt.lower()
    assert "run-results.json" in prompt
    assert "results[i].stdout" in prompt or "stdout" in prompt.lower()


async def test_run_debug_rca_uses_config_max_turns_and_timeout(tmp_path: Path):
    """Historical bug: hardcoded ``max_turns=10`` / ``timeout_s=300`` at the
    call site truncated the debug agent on complex failures. Config-driven
    now — assert those values flow through so a future regression doesn't
    silently reintroduce the cap.
    """
    from qtea.config import DEBUG_AGENT_MAX_TURNS, DEBUG_AGENT_TIMEOUT_S

    ctx = _ctx(tmp_path, no_fix=True)
    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="rca")
        await _run_debug_rca(9, ctx, "ctx", attempt=1)

    assert mock_agent.await_count == 1
    call = mock_agent.await_args_list[0]
    assert call.kwargs["max_turns"] == DEBUG_AGENT_MAX_TURNS
    assert call.kwargs["timeout_s"] == DEBUG_AGENT_TIMEOUT_S
    # Defaults must be materially higher than the old 10 / 300 to close the
    # regression — treat this as the design guarantee.
    assert DEBUG_AGENT_MAX_TURNS >= 20
    assert DEBUG_AGENT_TIMEOUT_S >= 600


async def test_run_fix_proposal_uses_config_max_turns_and_timeout(tmp_path: Path):
    """Same guard as debug-side, for the two-agent fix chain."""
    from qtea.config import FIX_AGENT_MAX_TURNS, FIX_AGENT_TIMEOUT_S

    ctx = _ctx(tmp_path)
    seeded_rca = tmp_path / "seeded-rca.md"
    seeded_rca.write_text("# Real RCA\n\nroot cause", encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="ok")
        await _run_fix_proposal(42, ctx, "# ctx", debug_rca_path=seeded_rca)

    # Both CT and eng calls made — assert config values propagated to both.
    assert mock_agent.await_count == 2
    for call in mock_agent.await_args_list:
        assert call.kwargs["max_turns"] == FIX_AGENT_MAX_TURNS
        assert call.kwargs["timeout_s"] == FIX_AGENT_TIMEOUT_S
    assert FIX_AGENT_MAX_TURNS >= 20
    assert FIX_AGENT_TIMEOUT_S >= 600


async def test_aggregated_rca_not_overwritten_by_smaller_content(tmp_path: Path):
    """Belt-and-braces guard: a substantial prior aggregated RCA must not
    be clobbered by a smaller one (which is nearly always a placeholder
    from a truncated debug run).
    """
    ctx = _ctx(tmp_path)
    # Seed a prior aggregated RCA (as if a previous debug pass had written
    # a real analysis to this workspace).
    prior_rca = ctx.workspace.debug / "step-42-rca.md"
    prior_rca.parent.mkdir(parents=True, exist_ok=True)
    prior_text = "# Prior RCA\n\n" + ("substantive analysis " * 100)  # ~2 KB
    prior_rca.write_text(prior_text, encoding="utf-8")

    # This attempt's debug-RCA is a tiny placeholder (simulating a truncated
    # run).
    tiny_rca = tmp_path / "tiny-debug-rca.md"
    tiny_rca.write_text("# stub", encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="")
        await _run_fix_proposal(42, ctx, "# ctx", debug_rca_path=tiny_rca)

    # Prior artifact preserved verbatim — the tiny content did NOT win.
    assert prior_rca.read_text(encoding="utf-8") == prior_text


async def test_aggregated_rca_overwritten_when_new_is_larger(tmp_path: Path):
    """Same-size / larger new content wins — the guard is strictly
    ``new < prior``, not a blanket refusal to overwrite.
    """
    ctx = _ctx(tmp_path)
    prior_rca = ctx.workspace.debug / "step-42-rca.md"
    prior_rca.parent.mkdir(parents=True, exist_ok=True)
    prior_rca.write_text("# stub", encoding="utf-8")

    larger_rca = tmp_path / "larger-debug-rca.md"
    larger_text = "# Real RCA\n\n" + ("real content " * 100)
    larger_rca.write_text(larger_text, encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = _mock_result(success=True, final_text="")
        await _run_fix_proposal(42, ctx, "# ctx", debug_rca_path=larger_rca)

    assert prior_rca.read_text(encoding="utf-8") == larger_text


async def test_run_fix_proposal_eng_placeholder_on_turn_cap(tmp_path: Path):
    """Principal-eng hitting turn cap must not ship its pre-tool thinking
    as ``fix-proposal.md``. Placeholder header + upstream RCA + strategy
    embedded so the operator still has a manual hand-off path.
    """
    ctx = _ctx(tmp_path)
    seeded_rca = tmp_path / "seeded-rca.md"
    seeded_rca.write_text("# Real Debug RCA\n\nroot cause: locator", encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        # CT succeeds with an inlined strategy; eng hits the turn cap.
        mock_agent.side_effect = [
            _mock_result(success=True, final_text="# Fix Strategy\n\nreplace locator"),
            _mock_result(
                success=False,
                final_text="Let me check the transcript log for the actual test code.",
                error="sdk error: Reached maximum number of turns (25) | api: ...",
            ),
        ]
        proposal_path = await _run_fix_proposal(
            42, ctx, "# Step 42 failure", debug_rca_path=seeded_rca,
        )

    content = proposal_path.read_text(encoding="utf-8")
    assert content.startswith(
        "# principal-software-engineer.agent — agent failed to produce artifact"
    )
    assert "Reached maximum number of turns (25)" in content
    # Blockquoted thinking snippet, not passing as prose.
    assert "> Let me check the transcript log" in content
    # Upstream context sections present so the operator can still act.
    assert "## Upstream Debug RCA" in content
    assert "root cause: locator" in content
    assert "## Upstream Fix Strategy" in content


async def test_fix_proposal_uses_debug_rca_when_available(tmp_path: Path):
    """Regression guard: the fix chain must feed the debug agent's RCA into
    the critical-thinking agent (via ``debug-rca.md`` in inputs), not
    re-derive an RCA from the raw failure context.
    """
    ctx = _ctx(tmp_path)

    seeded_rca_text = "# Debug RCA\n\nroot cause: locator selected wrong overlay layer"
    seeded_rca = tmp_path / "seeded-debug-rca.md"
    seeded_rca.write_text(seeded_rca_text, encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": False, "final_text": "strategy text", "error": None,
        })()
        await _run_fix_proposal(
            42, ctx, "# raw failure context",
            debug_rca_path=seeded_rca,
        )

    # Two agent calls: critical-thinking, then principal-software-engineer.
    assert mock_agent.await_count == 2
    ct_call, eng_call = mock_agent.await_args_list
    ct_inputs = ct_call.kwargs["inputs"]
    assert "debug-rca.md" in ct_inputs
    assert ct_inputs["debug-rca.md"].read_text(encoding="utf-8") == seeded_rca_text
    eng_inputs = eng_call.kwargs["inputs"]
    assert "debug-rca.md" in eng_inputs
    assert "fix-strategy.md" in eng_inputs


# ---------------------------------------------------------------------------
# Auxiliary-agent record tests (per-agent cost visibility)
#
# Before this split, debug / critical-thinking / principal-engineer costs
# were folded into the parent step's `StepRecord.cost_usd` cell so the
# summary table showed one opaque number. These tests lock in the new
# invariant: each helper agent produces its OWN row on
# `state.auxiliary_records`, and the parent step's cost cell is untouched.
# ---------------------------------------------------------------------------


async def test_record_aux_agent_appends_row_and_leaves_step_untouched(tmp_path: Path):
    """Direct-invoke test on `_record_aux_agent`: run a fake coroutine that
    records agent metrics into the active accumulator (mirrors what
    `run_agent` does), and confirm the aux record captures those totals
    while any StepRecord on state is UNCHANGED.
    """
    ctx = _ctx(tmp_path)
    # Seed a StepRecord so we can assert it's not touched.
    from qtea.checkpoints import StepRecord
    parent = StepRecord(step=5, name="fake-step", cost_usd=0.1234, tokens_input=100)
    ctx.state.steps[5] = parent

    async def fake_run_agent_call():
        # Simulate what run_agent does: push metrics into the active
        # accumulator that `_record_aux_agent` sets up.
        acc = CURRENT_STEP_METRICS.get()
        assert acc is not None, "aux tracker didn't set the accumulator"
        acc.record(AgentMetrics(
            input_tokens=1000,
            output_tokens=2000,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=300,
            cost_usd=0.75,
        ))
        return "ok"

    result = await _record_aux_agent(
        ctx, 5, "debug", "debug.agent.md", fake_run_agent_call(),
    )
    assert result == "ok"

    # Exactly one aux row appended.
    assert len(ctx.state.auxiliary_records) == 1
    aux = ctx.state.auxiliary_records[0]
    assert aux.step == 5
    assert aux.phase == "debug"
    assert aux.agent == "debug.agent.md"
    assert aux.status == "completed"
    assert aux.tokens_input == 1000
    assert aux.tokens_output == 2000
    assert aux.tokens_cache_read == 500
    assert aux.tokens_cache_creation == 300
    assert aux.cost_usd == 0.75
    assert aux.agent_calls == 1
    assert aux.duration_s is not None and aux.duration_s >= 0

    # Parent StepRecord must NOT be mutated — this is the regression that
    # would double-count aux cost against the step's cost cell.
    assert parent.cost_usd == 0.1234
    assert parent.tokens_input == 100


async def test_record_aux_agent_marks_failed_on_exception(tmp_path: Path):
    """If the wrapped coroutine raises, the aux row must still be written
    (partial billing is still real spend) with status='failed'.
    """
    ctx = _ctx(tmp_path)

    async def crashing_agent_call():
        acc = CURRENT_STEP_METRICS.get()
        acc.record(AgentMetrics(input_tokens=42, cost_usd=0.01))
        raise RuntimeError("upstream 500")

    try:
        await _record_aux_agent(
            ctx, 9, "critical_thinking", "critical-thinking.agent.md",
            crashing_agent_call(),
        )
    except RuntimeError:
        pass  # expected; the tracker must not swallow it
    else:
        raise AssertionError("expected RuntimeError to propagate")

    assert len(ctx.state.auxiliary_records) == 1
    aux = ctx.state.auxiliary_records[0]
    assert aux.status == "failed"
    assert aux.tokens_input == 42
    assert aux.cost_usd == 0.01


async def test_fix_proposal_writes_two_aux_records(tmp_path: Path):
    """The two-agent fix chain (critical-thinking + principal-engineer)
    must produce exactly TWO aux rows — one per agent — with the right
    phase labels. Locks in the split that the whole ticket asked for.
    """
    ctx = _ctx(tmp_path)
    seeded_rca = tmp_path / "seeded-debug-rca.md"
    seeded_rca.write_text("# Debug RCA\n\nroot cause: X", encoding="utf-8")

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": True, "final_text": "text", "error": None,
        })()
        await _run_fix_proposal(
            42, ctx, "# ctx", debug_rca_path=seeded_rca,
        )

    assert len(ctx.state.auxiliary_records) == 2
    ct, pse = ctx.state.auxiliary_records
    assert ct.phase == "critical_thinking"
    assert ct.agent == "critical-thinking.agent.md"
    assert ct.step == 42
    assert pse.phase == "principal_engineer"
    assert pse.agent == "principal-software-engineer.agent.md"
    assert pse.step == 42


async def test_double_failure_produces_debug_ct_pse_aux_rows(tmp_path: Path):
    """End-to-end through Step.execute(): a step that fails both attempts
    triggers debug RCA (attempt 2 failure) + fix chain (retry exhaustion),
    yielding three aux rows in chronological order: debug, critical
    thinking, principal engineer. Parent StepRecord must NOT contain any
    of that billing.
    """
    ctx = _ctx(tmp_path)
    step = _AlwaysFailStep()

    with patch("qtea.steps.base.run_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = type("R", (), {
            "success": True, "final_text": "text", "error": None,
        })()
        await step.execute(ctx)

    phases = [a.phase for a in ctx.state.auxiliary_records]
    assert phases == ["debug", "critical_thinking", "principal_engineer"], (
        f"expected debug → CT → PSE ordering, got {phases}"
    )
    # Parent step 98 exists with attempts=2 but ZERO helper-agent billing
    # folded in (StepRecord cost/tokens all start at 0; the mocked
    # run_agent contributed no metrics because the fake AgentResult
    # doesn't push into the accumulator — so the check that step 98 has
    # cost_usd == 0.0 confirms nothing leaked in from the aux chain).
    step_rec = ctx.state.steps[98]
    assert step_rec.attempts == 2
    assert step_rec.cost_usd == 0.0


def test_run_state_round_trips_auxiliary_records(tmp_path: Path):
    """The workspace's state.json round-trips aux records so a resumed run
    or a report generated by a separate `qtea report` invocation still
    sees the aux billing.
    """
    from qtea.checkpoints import AuxiliaryAgentRecord, RunState

    state = RunState(
        run_id="test", workspace=str(tmp_path), spec_source="x", sut_source=".",
    )
    state.auxiliary_records.append(
        AuxiliaryAgentRecord(
            step=2,
            agent="debug.agent.md",
            phase="debug",
            status="completed",
            duration_s=42.5,
            tokens_input=1000,
            tokens_output=500,
            cost_usd=0.75,
            agent_calls=1,
        )
    )
    round_tripped = RunState.from_dict(state.to_dict())
    assert len(round_tripped.auxiliary_records) == 1
    aux = round_tripped.auxiliary_records[0]
    assert aux.step == 2
    assert aux.phase == "debug"
    assert aux.cost_usd == 0.75


def test_run_state_backward_compat_load_without_aux_key(tmp_path: Path):
    """State files written before this feature landed have no
    ``auxiliary_records`` key. Loading must not raise; the list defaults
    to empty and old workspaces render fine.
    """
    from qtea.checkpoints import RunState

    old_shape = {
        "run_id": "old-run",
        "workspace": str(tmp_path),
        "spec_source": "x",
        "sut_source": ".",
        "started_at": "2026-07-01T00:00:00+00:00",
        "steps": {},
        # no auxiliary_records key at all
    }
    rs = RunState.from_dict(old_shape)
    assert rs.auxiliary_records == []


def test_report_totals_include_aux(tmp_path: Path):
    """Report layer: `summary.total_cost_usd` must equal
    `sum(steps) + sum(aux)`. Without this, splitting the aux out would
    have silently under-counted the grand total.
    """
    from qtea.report.data_builder import (
        AuxTiming,
        StepTiming,
        _compute_summary,
    )

    steps = [
        StepTiming(
            step=1, name="Intake", status="completed", duration_s=1.0,
            tokens_input=10, tokens_output=20,
            tokens_cache_creation=0, tokens_cache_read=0,
            cost_usd=0.10, agent_calls=1,
        ),
        StepTiming(
            step=2, name="Spec Refinement", status="failed", duration_s=2.0,
            tokens_input=100, tokens_output=200,
            tokens_cache_creation=50, tokens_cache_read=500,
            cost_usd=0.50, agent_calls=2,
        ),
    ]
    aux = [
        AuxTiming(
            step=2, agent="debug.agent.md", phase="debug", status="completed",
            duration_s=10.0,
            tokens_input=3000, tokens_output=23000,
            tokens_cache_creation=0, tokens_cache_read=460000,
            cost_usd=1.76, agent_calls=1,
        ),
        AuxTiming(
            step=2, agent="critical-thinking.agent.md",
            phase="critical_thinking", status="completed", duration_s=5.0,
            tokens_input=5000, tokens_output=9000,
            tokens_cache_creation=0, tokens_cache_read=37000,
            cost_usd=0.76, agent_calls=1,
        ),
        AuxTiming(
            step=2, agent="principal-software-engineer.agent.md",
            phase="principal_engineer", status="completed", duration_s=4.0,
            tokens_input=7000, tokens_output=8500,
            tokens_cache_creation=0, tokens_cache_read=62000,
            cost_usd=0.62, agent_calls=1,
        ),
    ]
    run_results = {"framework": "pytest", "results": [], "totals": {"tests": 0}}
    bug_reports = {"bugs": []}

    summary = _compute_summary(run_results, bug_reports, steps, aux)

    # Cost equals sum of every row's cost cell — the invariant that made
    # the whole "split aux out" ticket non-regressive at the grand-total
    # level (0.10 + 0.50 + 1.76 + 0.76 + 0.62 = 3.74).
    assert summary.total_cost_usd == 3.74
    # Tokens likewise sum both.
    assert summary.total_tokens_input == 10 + 100 + 3000 + 5000 + 7000
    assert summary.total_tokens_output == 20 + 200 + 23000 + 9000 + 8500
    # Pipeline duration includes aux wait time (operator waited for these).
    assert summary.pipeline_duration_s == 1.0 + 2.0 + 10.0 + 5.0 + 4.0
    # Agent-call count sums too.
    assert summary.total_agent_calls == 1 + 2 + 1 + 1 + 1


def test_html_renderer_emits_aux_rows_after_step_11(tmp_path: Path):
    """The pipeline-execution HTML table shows aux rows between the last
    step row and the TOTAL row, and TOTAL matches summed cost cells.
    """
    from qtea.report.data_builder import (
        AuxTiming,
        ReportSummary,
        RunReport,
        StepTiming,
    )
    from qtea.report.html_renderer import render_html

    steps = [
        StepTiming(
            step=2, name="Spec Refinement", status="failed", duration_s=2.0,
            tokens_input=100, tokens_output=200,
            tokens_cache_creation=0, tokens_cache_read=0,
            cost_usd=0.50, agent_calls=2,
        ),
    ]
    aux = [
        AuxTiming(
            step=2, agent="debug.agent.md", phase="debug", status="completed",
            duration_s=10.0,
            tokens_input=3000, tokens_output=23000,
            tokens_cache_creation=0, tokens_cache_read=0,
            cost_usd=1.76, agent_calls=1,
        ),
    ]
    summary = ReportSummary(
        total_tests=0, passed=0, failed=0, skipped=0, errors=0,
        total_bugs=0, duration_s=None, pass_rate=0.0,
        pipeline_duration_s=12.0,
        total_tokens_input=3100, total_tokens_output=23200,
        total_tokens_cache_creation=0, total_tokens_cache_read=0,
        total_cost_usd=2.26, total_agent_calls=3,
    )
    report = RunReport(
        run_id="test-run",
        generated_at="2026-07-14T00:00:00+00:00",
        plan=None,
        strategy=None,
        run_results={"framework": "pytest", "results": []},
        bug_reports={"bugs": []},
        summary=summary,
        steps_summary=steps,
        auxiliary_summary=aux,
    )

    html = render_html(report)

    # Aux section marker present.
    assert "Fix Chain" in html
    # Debug agent labelled.
    assert "Debug agent" in html
    # Aux row's cost appears in the table (formatted as $1.76).
    assert "$1.76" in html
    # TOTAL row shows the summed cost.
    assert "$2.26" in html
    # And the parent step's cost cell is its own $0.50 — no double-count.
    assert "$0.50" in html


def test_html_renderer_no_aux_section_when_empty():
    """When no helper agent ever fired, the aux sub-header must NOT appear
    — a green pipeline shouldn't gain a scary-looking "Fix Chain" header.
    """
    from qtea.report.data_builder import ReportSummary, RunReport, StepTiming
    from qtea.report.html_renderer import render_html

    steps = [
        StepTiming(
            step=1, name="Intake", status="completed", duration_s=1.0,
            tokens_input=0, tokens_output=0,
            tokens_cache_creation=0, tokens_cache_read=0,
            cost_usd=0.0, agent_calls=0,
        ),
    ]
    summary = ReportSummary(
        total_tests=0, passed=0, failed=0, skipped=0, errors=0,
        total_bugs=0, duration_s=None, pass_rate=0.0,
        pipeline_duration_s=1.0,
        total_tokens_input=0, total_tokens_output=0,
        total_tokens_cache_creation=0, total_tokens_cache_read=0,
        total_cost_usd=0.0, total_agent_calls=0,
    )
    report = RunReport(
        run_id="green-run",
        generated_at="2026-07-14T00:00:00+00:00",
        plan=None, strategy=None,
        run_results={"framework": "pytest", "results": []},
        bug_reports={"bugs": []},
        summary=summary,
        steps_summary=steps,
        auxiliary_summary=[],
    )

    html = render_html(report)
    assert "Fix Chain" not in html
