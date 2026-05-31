"""Step 2 refine tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s02_refine import RefineStep, _project_to_json
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query

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
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def test_project_to_json_extracts_req_id_and_ac():
    proj = _project_to_json(REFINED_MD)
    assert proj["requirement_id"] == "REQ-login-feature"
    assert proj["title"] == "Login Feature"
    assert "User can sign in with valid credentials" in proj["acceptance_criteria"]
    assert proj["user_flows"] is not None
    assert proj["edge_cases"] is not None


def test_project_falls_back_to_slug_when_no_req_id():
    proj = _project_to_json("# Some Feature\n\nno req here\n\n## Acceptance Criteria\n\n- ok\n")
    assert proj["requirement_id"].startswith("REQ-")
    assert "some-feature" in proj["requirement_id"]


async def test_refine_step_writes_md_and_validated_json(tmp_path: Path, monkeypatch):
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"refined-spec.md": REFINED_MD},
    )

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"  # schema must be valid for our projection

    out = ctx.workspace.step_dir(2)
    assert (out / "refined-spec.md").exists()
    payload = json.loads((out / "refined-spec.json").read_text(encoding="utf-8"))
    assert payload["requirement_id"] == "REQ-login-feature"
    assert payload["acceptance_criteria"]


async def test_refine_step_missing_spec_fails(tmp_path: Path):
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    ctx = StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)
    result = await RefineStep().run(ctx)
    assert not result.success
    assert "missing" in (result.error or "").lower()


async def test_refine_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    install_fake_query(monkeypatch, messages=[{"type": "result", "result": "ok"}], files={})

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert not result.success
    assert "refined-spec.md" in (result.error or "")


REFINED_MD_WITH_CLARIFICATION = """\
# Login Feature

Requirement ID: REQ-login-feature

## Acceptance Criteria

- User can sign in with [CLARIFICATION NEEDED: which IdP?]

## Definition of Ready

NOT READY
"""


async def test_refine_step_hitl_loop_prompts_user_and_reruns(tmp_path: Path, monkeypatch):
    """First agent call returns clarification tags; HITL prompts; second call returns clean."""
    call_count = {"n": 0}
    outputs = [REFINED_MD_WITH_CLARIFICATION, REFINED_MD]

    def on_call(_prompt, options):
        idx = call_count["n"]
        call_count["n"] = idx + 1
        cwd = Path(options.cwd)
        (cwd / "refined-spec.md").write_text(outputs[min(idx, len(outputs) - 1)], encoding="utf-8")

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},
        on_call=on_call,
    )

    # Stub prompt_user so the test doesn't try to read stdin.
    monkeypatch.setattr(
        "worca_t.steps.base.prompt_user",
        lambda questions, *, agent_label: {q.id: "use okta" for q in questions},
    )

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success, result.error
    assert call_count["n"] == 2  # initial + 1 re-invocation
    # user-answers.md was staged into the workdir
    wd = ctx.workspace.step_workdir(2)
    assert (wd / "user-answers.md").exists()


async def test_refine_step_no_hitl_flag_skips_prompt(tmp_path: Path, monkeypatch):
    """With --no-hitl, the loop returns immediately even if clarifications are present."""
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"refined-spec.md": REFINED_MD_WITH_CLARIFICATION},
    )

    def fail(*_a, **_kw):  # pragma: no cover (must NOT be called)
        raise AssertionError("prompt_user must not be called when --no-hitl is set")

    monkeypatch.setattr("worca_t.steps.base.prompt_user", fail)

    ctx = _ctx(tmp_path)
    ctx.options.no_hitl = True
    # First agent run produces the output; without HITL the step accepts it as-is.
    result = await RefineStep().run(ctx)
    assert result.success or result.status == "warned"


async def test_refine_step_skipped_question_is_not_reasked(tmp_path: Path, monkeypatch):
    """If the user skips a question and the agent still emits it, don't re-prompt."""
    call_count = {"n": 0}
    # Both runs emit the SAME clarification — simulating an agent that didn't
    # convert it to an [ASSUMPTION] on the rerun. HITL must not re-ask it.
    def on_call(_prompt, options):
        call_count["n"] += 1
        cwd = Path(options.cwd)
        (cwd / "refined-spec.md").write_text(REFINED_MD_WITH_CLARIFICATION, encoding="utf-8")

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={},
        on_call=on_call,
    )

    prompt_calls = {"n": 0}

    def stub_prompt(questions, *, agent_label):
        prompt_calls["n"] += 1
        return {}  # user skips everything

    monkeypatch.setattr("worca_t.steps.base.prompt_user", stub_prompt)

    ctx = _ctx(tmp_path)
    result = await RefineStep().run(ctx)
    assert result.success or result.status == "warned"
    # Two agent invocations max (initial + one rerun with skip), then loop
    # exits because remaining questions are all already-skipped.
    assert call_count["n"] == 2
    # User was prompted exactly once — the rerun's identical question wasn't re-asked.
    assert prompt_calls["n"] == 1
