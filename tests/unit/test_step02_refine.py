"""Step 2 refine tests (direct-SDK transport)."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s02_refine import RefineStep, _project_to_json
from worca_t.workspace import create_workspace

from ._fake_anthropic import (
    FakeResponse,
    FakeTextBlock,
    FakeUsage,
    install_fake_anthropic,
)

REFINED_MD = """\
# Login Feature

Requirement ID: REQ-login-feature

## Acceptance Criteria

- User can sign in with valid credentials
- Invalid credentials show error

## User Flow

step 1, step 2

## Test Boundaries

in/out scope

## Edge Cases

- empty password

## Definition of Ready

READY
"""


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    # Seed step 1 output.
    (ws.step_dir(1) / "spec.md").write_text("# Login\n\nstub", encoding="utf-8")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source="."
    )
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


# ---------------------------------------------------------------------------
# Pure helpers (unchanged from pre-migration)
# ---------------------------------------------------------------------------


def test_project_to_json_extracts_req_id_and_ac():
    proj = _project_to_json(REFINED_MD)
    assert proj["requirement_id"] == "REQ-login-feature"
    assert proj["title"] == "Login Feature"
    assert "User can sign in with valid credentials" in proj["acceptance_criteria"]
    assert proj["user_flows"] is not None
    assert proj["edge_cases"] is not None


def test_project_falls_back_to_slug_when_no_req_id():
    proj = _project_to_json(
        "# Some Feature\n\nno req here\n\n## Acceptance Criteria\n\n- ok\n"
    )
    assert proj["requirement_id"].startswith("REQ-")
    assert "some-feature" in proj["requirement_id"]


# ---------------------------------------------------------------------------
# Step integration (direct-SDK transport)
# ---------------------------------------------------------------------------


async def test_refine_step_writes_md_and_validated_json(tmp_path: Path, monkeypatch):
    """Agent returns the refined markdown; step parses + validates JSON projection."""
    install_fake_anthropic(monkeypatch, text=REFINED_MD)

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"  # schema must be valid for our projection

    out = ctx.workspace.step_dir(2)
    assert (out / "refined-spec.md").exists()
    payload = json.loads((out / "refined-spec.json").read_text(encoding="utf-8"))
    assert payload["requirement_id"] == "REQ-login-feature"
    assert payload["acceptance_criteria"]


async def test_refine_step_inlines_spec_into_user_prompt(tmp_path: Path, monkeypatch):
    """The spec.md content must reach the LLM via the user message inputs."""
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text=REFINED_MD, on_call=captured.update)

    ctx = _ctx(tmp_path)
    # Overwrite the seeded spec with a distinctive marker we can spot in the prompt.
    (ctx.workspace.step_dir(1) / "spec.md").write_text(
        "# Login\n\nSPEC_INLINE_MARKER_XYZ\n", encoding="utf-8"
    )

    result = await RefineStep().run(ctx)
    assert result.success

    user_content = captured["messages"][-1]["content"]
    assert "SPEC_INLINE_MARKER_XYZ" in user_content
    assert "spec.md" in user_content  # the filename header from _inline_inputs


async def test_refine_step_missing_spec_fails(tmp_path: Path):
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source="."
    )
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    ctx = StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)
    result = await RefineStep().run(ctx)
    assert not result.success
    assert "missing" in (result.error or "").lower()


async def test_refine_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    """Empty response from the LLM short-circuits with failure."""
    install_fake_anthropic(monkeypatch, text="")

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert not result.success
    assert "refined-spec.md" in (result.error or "")


# ---------------------------------------------------------------------------
# HITL re-invoke flow (multi-turn conversation via direct SDK)
# ---------------------------------------------------------------------------

REFINED_MD_WITH_CLARIFICATION = """\
# Login Feature

Requirement ID: REQ-login-feature

## Acceptance Criteria

- User can sign in with [CLARIFICATION NEEDED: which IdP?]

## Definition of Ready

NOT READY
"""


def _install_scripted_anthropic(monkeypatch, texts: list[str], on_call=None):
    """Install an AsyncAnthropic stand-in that returns ``texts[i]`` on call i."""
    calls = {"n": 0}

    async def _create(**kwargs):
        i = calls["n"]
        calls["n"] = i + 1
        if on_call is not None:
            on_call(kwargs)
        return FakeResponse(
            content=[FakeTextBlock(text=texts[min(i, len(texts) - 1)])],
            usage=FakeUsage(),
        )

    class FakeMessages:
        create = staticmethod(_create)

    class FakeClient:
        def __init__(self, **_kw):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    # Patch BOTH client classes — Vertex env may be set globally on the dev machine.
    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
    monkeypatch.setattr("anthropic.AsyncAnthropicVertex", FakeClient)
    return calls


async def test_refine_step_hitl_loop_prompts_user_and_reruns(
    tmp_path: Path, monkeypatch
):
    """First call returns clarification; HITL prompts user; second call returns clean."""
    calls = _install_scripted_anthropic(
        monkeypatch, [REFINED_MD_WITH_CLARIFICATION, REFINED_MD]
    )

    # Stub the user prompt so the test doesn't try to read stdin.
    # prompt_user returns dict[str, tuple[resolution, text]].
    from worca_t.hitl import RESOLUTION_ANSWERED
    monkeypatch.setattr(
        "worca_t.hitl.prompt_user",
        lambda questions, *, agent_label: {
            q.id: (RESOLUTION_ANSWERED, "use okta") for q in questions
        },
    )

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success, result.error
    assert calls["n"] == 2  # initial + 1 re-invocation

    # User-answers file persisted to .hitl-step02 for audit.
    hitl_dir = ctx.workspace.step_workdir(2).parent / ".hitl-step02"
    assert hitl_dir.exists() and any(hitl_dir.iterdir())


async def test_refine_step_no_hitl_flag_skips_prompt(tmp_path: Path, monkeypatch):
    """With --no-hitl, the loop returns immediately even with clarifications."""
    install_fake_anthropic(monkeypatch, text=REFINED_MD_WITH_CLARIFICATION)

    def fail(*_a, **_kw):  # pragma: no cover - must NOT be called
        raise AssertionError("prompt_user must not be called when --no-hitl is set")

    monkeypatch.setattr("worca_t.hitl.prompt_user", fail)

    ctx = _ctx(tmp_path)
    ctx.options.no_hitl = True
    result = await RefineStep().run(ctx)
    # Schema invalid (no AC because the only AC has a clarification stub)
    # is OK -> "warned"; what matters is no_hitl was honored.
    assert result.success or result.status == "warned"


async def test_refine_step_skipped_question_is_not_reasked(
    tmp_path: Path, monkeypatch
):
    """If the user skips a question and the agent still emits it, don't re-prompt."""
    # Both calls emit the SAME clarification — simulating an agent that
    # didn't convert to [ASSUMPTION] on rerun. HITL must not re-ask it.
    calls = _install_scripted_anthropic(
        monkeypatch,
        [REFINED_MD_WITH_CLARIFICATION, REFINED_MD_WITH_CLARIFICATION],
    )

    prompt_calls = {"n": 0}

    def stub_prompt(questions, *, agent_label):
        prompt_calls["n"] += 1
        return {}  # user skips everything

    monkeypatch.setattr("worca_t.hitl.prompt_user", stub_prompt)

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success or result.status == "warned"
    # Two agent invocations max (initial + one rerun with skip), then loop
    # exits because remaining questions are all already-skipped.
    assert calls["n"] == 2
    # User was prompted exactly once — the rerun's identical question wasn't re-asked.
    assert prompt_calls["n"] == 1
