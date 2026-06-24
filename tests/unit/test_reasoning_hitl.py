"""Tests for :func:`qtea.llm.reasoning.call_reasoning_llm_with_hitl`.

Covers the multi-turn HITL re-invoke pattern in isolation:
  * No-questions short-circuit (single iteration, no prompt)
  * One-round resolution (iteration 2 produces clean output)
  * Skipped-question dedup across rounds (user-skipped items don't re-prompt)
  * ``ctx.options.no_hitl`` bypass
  * ``max_iterations`` cap
  * Conversation history shape (iteration 2 carries the iteration-1 turn)

These complement the step-level tests in test_step02_refine / test_step03_plan
which exercise the helper through the production step files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from tests.unit._fake_anthropic import (
    FakeResponse,
    FakeTextBlock,
    FakeUsage,
)
from qtea.llm.reasoning import call_reasoning_llm_with_hitl


def _write_agent_file(tmp_path: Path) -> Path:
    p = tmp_path / "refine-spec.agent.md"
    p.write_text("# Refine spec\nYou are a spec refiner.", encoding="utf-8")
    return p


def _fake_ctx(
    no_hitl: bool = False, workspace_root: Path | None = None
) -> SimpleNamespace:
    """Minimal StepContext stand-in.

    The HITL wrapper reads:
      * ``ctx.options.no_hitl`` — bypass flag
      * ``ctx.extras`` — dict that carries the cross-step HITL ledger
      * ``ctx.workspace.root`` — workspace dir for the on-disk ledger mirror
    """
    return SimpleNamespace(
        options=SimpleNamespace(no_hitl=no_hitl),
        extras={},
        workspace=SimpleNamespace(root=workspace_root or Path()),
    )


@dataclass
class _ScriptedAnthropic:
    """Anthropic stand-in that returns canned responses in order, capturing
    the messages list each time so tests can assert on the conversation."""
    responses: list[str]
    calls: list[dict] = field(default_factory=list)

    def install(self, monkeypatch) -> None:
        scripted = self

        async def _create(**kwargs):
            scripted.calls.append({"messages": list(kwargs.get("messages", []))})
            idx = min(len(scripted.calls) - 1, len(scripted.responses) - 1)
            return FakeResponse(
                content=[FakeTextBlock(text=scripted.responses[idx])],
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

        # Patch BOTH client classes so the test works whether the backend
        # selector picks the standard or Vertex branch (developer machines
        # with Bosch model-farm env vars set globally take the Vertex path).
        monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
        monkeypatch.setattr("anthropic.AsyncAnthropicVertex", FakeClient)


# ---------------------------------------------------------------------------
# Happy path: no clarifications => single iteration
# ---------------------------------------------------------------------------

async def test_no_questions_returns_first_iteration(tmp_path, monkeypatch):
    scripted = _ScriptedAnthropic(responses=["# Clean spec\n\nNo questions here."])
    scripted.install(monkeypatch)

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "# Original"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    assert result.success
    assert "Clean spec" in result.final_text
    # Single iteration — no re-invoke.
    assert len(scripted.calls) == 1
    # Output persisted to workdir for downstream consumers.
    assert (tmp_path / "wd" / "refined-spec.md").exists()


# ---------------------------------------------------------------------------
# HITL re-invoke: clarification on iteration 1, clean on iteration 2
# ---------------------------------------------------------------------------

_MD_WITH_QUESTION = (
    "# Login\n\nRequirement ID: REQ-login\n\n"
    "## Acceptance Criteria\n\n"
    "- Use [CLARIFICATION NEEDED: which IdP?] for sign-in\n"
)

_MD_RESOLVED = (
    "# Login\n\nRequirement ID: REQ-login\n\n"
    "## Acceptance Criteria\n\n"
    "- Use Okta for sign-in\n"
)


async def test_one_round_resolution_prompts_user_and_reruns(tmp_path, monkeypatch):
    scripted = _ScriptedAnthropic(responses=[_MD_WITH_QUESTION, _MD_RESOLVED])
    scripted.install(monkeypatch)

    # Stub the user prompt: answer every question with "use okta".
    # Note: prompt_user now returns dict[str, tuple[resolution, text]].
    from qtea.hitl import RESOLUTION_ANSWERED
    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda qs, *, agent_label: {
            q.id: (RESOLUTION_ANSWERED, "use okta") for q in qs
        },
    )

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "# Login (raw)"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    assert result.success
    assert "Okta" in result.final_text
    assert len(scripted.calls) == 2

    # Iteration 2's message list must include the iteration-1 conversation:
    # [user_iter1, assistant_iter1, user_iter2_with_answers]
    iter2_msgs = scripted.calls[1]["messages"]
    assert len(iter2_msgs) == 3
    assert iter2_msgs[0]["role"] == "user"
    assert "# Login (raw)" in iter2_msgs[0]["content"]  # iteration-1 prompt
    assert iter2_msgs[1]["role"] == "assistant"
    assert "[CLARIFICATION NEEDED:" in iter2_msgs[1]["content"]
    assert iter2_msgs[2]["role"] == "user"
    assert "use okta" in iter2_msgs[2]["content"].lower()

    # User-answers file persisted for audit.
    hitl_dir = tmp_path / ".hitl-step02"
    assert hitl_dir.exists()
    assert any(hitl_dir.iterdir())


# ---------------------------------------------------------------------------
# no_hitl flag bypass
# ---------------------------------------------------------------------------

async def test_no_hitl_flag_returns_first_iteration(tmp_path, monkeypatch):
    """With ``--no-hitl``, even clarification-bearing output is accepted as-is."""
    scripted = _ScriptedAnthropic(responses=[_MD_WITH_QUESTION])
    scripted.install(monkeypatch)

    def fail_prompt(*_a, **_kw):  # pragma: no cover - must NOT be called
        raise AssertionError("prompt_user must not be called with no_hitl=True")

    monkeypatch.setattr("qtea.hitl.prompt_user", fail_prompt)

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(no_hitl=True, workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "x"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    assert result.success
    assert len(scripted.calls) == 1
    assert "[CLARIFICATION NEEDED:" in result.final_text


# ---------------------------------------------------------------------------
# Skipped-question dedup
# ---------------------------------------------------------------------------

async def test_skipped_question_is_not_reasked(tmp_path, monkeypatch):
    """Same clarification on rerun must not re-prompt — it was already skipped."""
    scripted = _ScriptedAnthropic(
        responses=[_MD_WITH_QUESTION, _MD_WITH_QUESTION],
    )
    scripted.install(monkeypatch)

    prompt_calls = {"n": 0}

    def stub_prompt(qs, *, agent_label):
        prompt_calls["n"] += 1
        return {}  # user skips everything

    monkeypatch.setattr("qtea.hitl.prompt_user", stub_prompt)

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "x"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    assert result.success
    # Two agent calls (iter 1 + iter 2 after skip), but only ONE user prompt:
    # the iter-2 questions are all in skipped_keys so the loop returns
    # without re-prompting.
    assert len(scripted.calls) == 2
    assert prompt_calls["n"] == 1


# ---------------------------------------------------------------------------
# max_iterations cap
# ---------------------------------------------------------------------------

async def test_max_iterations_cap_terminates_loop(tmp_path, monkeypatch):
    """Agent emits NEW clarification every time; helper stops at max_iterations."""
    # Three different clarifications so the dedup doesn't short-circuit.
    responses = [
        _MD_WITH_QUESTION.replace("which IdP?", "which IdP variant A?"),
        _MD_WITH_QUESTION.replace("which IdP?", "which IdP variant B?"),
        _MD_WITH_QUESTION.replace("which IdP?", "which IdP variant C?"),
    ]
    scripted = _ScriptedAnthropic(responses=responses)
    scripted.install(monkeypatch)

    from qtea.hitl import RESOLUTION_ANSWERED
    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda qs, *, agent_label: {
            q.id: (RESOLUTION_ANSWERED, f"answer-{q.id}") for q in qs
        },
    )

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "x"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
        max_iterations=3,
    )

    # Loop bounded at max_iterations=3 even with pending questions.
    assert result.success
    assert len(scripted.calls) == 3
    # Result reflects the LAST iteration's output (still has clarifications).
    assert "[CLARIFICATION NEEDED:" in result.final_text


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------

async def test_failed_llm_call_returns_immediately(tmp_path, monkeypatch):
    """Empty response from the LLM short-circuits the loop."""
    scripted = _ScriptedAnthropic(responses=[""])  # empty text triggers failure
    scripted.install(monkeypatch)

    def fail_prompt(*_a, **_kw):  # pragma: no cover - must NOT be called
        raise AssertionError("prompt_user must not be called when LLM returns empty")

    monkeypatch.setattr("qtea.hitl.prompt_user", fail_prompt)

    result = await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="x",
        inputs={"spec.md": "x"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    # call_reasoning_llm returns success=True for an empty content list but
    # final_text=="" — the HITL helper treats empty final_text as "nothing
    # to validate" and returns early without prompting.
    assert len(scripted.calls) == 1
    assert result.final_text == ""


# ---------------------------------------------------------------------------
# Skip-as-drop semantics (replaces skip-as-assumption)
# ---------------------------------------------------------------------------


async def test_skip_produces_drop_directive_in_iteration_2_prompt(
    tmp_path, monkeypatch
):
    """When the user skips, the iteration-2 prompt the agent sees must
    instruct DROP semantics — not "make a reasonable assumption" framing.

    Note: the prompt explicitly mentions `[ASSUMPTION]` in a prohibition
    ("Do NOT write [ASSUMPTION]"), so the literal substring IS in the
    prompt — we assert on the old INSTRUCTIONAL wording instead.
    """
    scripted = _ScriptedAnthropic(responses=[_MD_WITH_QUESTION, _MD_RESOLVED])
    scripted.install(monkeypatch)

    # User skips everything (returns empty dict).
    monkeypatch.setattr(
        "qtea.hitl.prompt_user", lambda qs, *, agent_label: {}
    )

    await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "# Login (raw)"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    assert len(scripted.calls) == 2
    iter2_user_msg = scripted.calls[1]["messages"][-1]["content"]
    lowered = iter2_user_msg.lower()
    assert "drop" in lowered
    assert "remove" in lowered
    assert "Coverage Notes" in iter2_user_msg
    # The old INSTRUCTIONAL phrasing must be gone — these were the
    # active directives that made the agent invent assumptions.
    assert "make a reasonable assumption" not in lowered
    assert "mark it inline with" not in lowered


async def test_scope_exclusion_passes_answer_through_with_exclusion_framing(
    tmp_path, monkeypatch
):
    """A scope-exclusion answer ("mobile isn't in scope") must appear in
    the iteration-2 prompt with explicit "interpret as scope-exclusion"
    framing so the agent removes the named scope rather than including
    the typed text as a literal value."""
    scripted = _ScriptedAnthropic(responses=[_MD_WITH_QUESTION, _MD_RESOLVED])
    scripted.install(monkeypatch)

    from qtea.hitl import RESOLUTION_SCOPE_EXCLUSION
    monkeypatch.setattr(
        "qtea.hitl.prompt_user",
        lambda qs, *, agent_label: {
            q.id: (RESOLUTION_SCOPE_EXCLUSION, "mobile isn't in scope") for q in qs
        },
    )

    await call_reasoning_llm_with_hitl(
        agent_path=_write_agent_file(tmp_path),
        ctx=_fake_ctx(workspace_root=tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="Refine this:",
        inputs={"spec.md": "# Login (raw)"},
        output_filename="refined-spec.md",
        step=2,
        agent_label="refine-spec",
        model="claude-sonnet-4-6",
    )

    iter2_user_msg = scripted.calls[1]["messages"][-1]["content"]
    lowered = iter2_user_msg.lower()
    assert "Scope Exclusions" in iter2_user_msg
    assert "mobile isn't in scope" in iter2_user_msg
    assert "scope-exclusion" in lowered
    assert "Coverage Notes" in iter2_user_msg
    # The old INSTRUCTIONAL phrasing for the skip→assumption fallback must be
    # gone (the prompt mentions [ASSUMPTION] only in a prohibition).
    assert "make a reasonable assumption" not in lowered
    assert "mark it inline with" not in lowered
