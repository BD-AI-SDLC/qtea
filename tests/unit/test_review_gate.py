"""Tests for the Step-7 HITL hook-reuse phase gate in review_gate.py.

Covers the fix for the "stale calls[] after a HITL from-edit" defect: both
edit paths (`_apply_nlp_edit` free-text LLM edit, `_sync_md_to_json` file
edit) must (a) supply `sut_inventory.lifecycle_hooks.json` as extra LLM
context when available, and (b) run the same `_validate_plan_against_inventory`
phase gate the original Step 7 generate-validate-retry loop uses, never
silently persisting a plan with an unresolved hook-reuse violation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.review_gate import (
    _apply_nlp_edit,
    _check_hook_gate_violations,
    _load_active_module_for_gate,
    _sync_md_to_json,
    review_step_7_plan,
    set_ui_prompt_hook,
)
from qtea.steps.base import StepContext, StepResult
from qtea.workspace import create_workspace

from ._fake_anthropic import install_fake_anthropic

_LIFECYCLE_HOOKS = [
    {
        "event": "before_each",
        "file": "tests/RopaEntitySmoke.spec.ts",
        "calls": ["basePage.openBaseURL", "basePage.logIn",
                  "basePage.selectLoginOptionByText"],
    },
]

_GOOD_HOOK_CALLS = [
    {"pom": "basePage", "method": "openBaseURL"},
    {"pom": "basePage", "method": "logIn", "args": ["U", "P"]},
    {"pom": "basePage", "method": "selectLoginOptionByText"},
]

_STALE_HOOK_CALLS = [
    {"pom": "basePage", "method": "openBaseURL"},
    {"pom": "basePage", "method": "logIn", "args": ["U", "P"]},
]


def _plan_with_hook(calls: list[dict]) -> dict:
    return {
        "plan_version": "1.0",
        "active_module": "frontend",
        "test_cases": [{
            "id": "TC-1",
            "test_file_target": "tests/qtea_x.spec.ts",
            "test_functions": [{"name": "t"}],
            "hooks": [{
                "event": "before_each", "source": "reuse",
                "from": "tests/RopaEntitySmoke.spec.ts",
                "calls": calls,
            }],
        }],
    }


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(
        run_id=ws.run_id, workspace=str(ws.root),
        spec_source="x", sut_source=str(ws.sut),
    )
    opts = PipelineOptions(spec="x", sut=str(ws.sut), workspace_base=tmp_path / ".ws")
    return StepContext(
        workspace=ws, state=state,
        spec_source="x", sut_source=str(ws.sut), options=opts,
    )


def _seed_inventory(ctx: StepContext, lifecycle_hooks: list[dict] | None) -> None:
    step6 = ctx.workspace.step_dir(6)
    module: dict = {"name": "frontend", "language": "typescript"}
    if lifecycle_hooks is not None:
        module["lifecycle_hooks"] = lifecycle_hooks
    inv = {"active_module": "frontend", "modules": [module]}
    (step6 / "sut_inventory.json").write_text(json.dumps(inv), encoding="utf-8")


def _console() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# _load_active_module_for_gate / _check_hook_gate_violations
# ---------------------------------------------------------------------------


def test_load_active_module_for_gate_reads_step6_inventory(tmp_path):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    am = _load_active_module_for_gate(ctx)
    assert am is not None
    assert am["lifecycle_hooks"] == _LIFECYCLE_HOOKS


def test_load_active_module_for_gate_missing_step6_returns_none(tmp_path):
    ctx = _ctx(tmp_path)
    assert _load_active_module_for_gate(ctx) is None


def test_check_hook_gate_violations_none_active_module_returns_empty():
    assert _check_hook_gate_violations(_plan_with_hook(_STALE_HOOK_CALLS), None) == []


def test_check_hook_gate_violations_flags_stale_calls():
    am = {"lifecycle_hooks": _LIFECYCLE_HOOKS}
    violations = _check_hook_gate_violations(_plan_with_hook(_STALE_HOOK_CALLS), am)
    assert any("stale relative to" in v for v in violations)


def test_check_hook_gate_violations_passes_matching_calls():
    am = {"lifecycle_hooks": _LIFECYCLE_HOOKS}
    violations = _check_hook_gate_violations(_plan_with_hook(_GOOD_HOOK_CALLS), am)
    assert violations == []


# ---------------------------------------------------------------------------
# _apply_nlp_edit — lifecycle_hooks context + gate wiring
# ---------------------------------------------------------------------------


async def test_apply_nlp_edit_includes_lifecycle_hooks_in_prompt(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(plan),
        on_call=lambda kwargs: captured.update(kwargs),
    )

    result = await _apply_nlp_edit(
        plan, plan_path, ctx, _console(), instructions="no-op edit",
    )

    assert result is not None
    messages = captured.get("messages") or []
    joined = json.dumps(messages)
    assert "sut_inventory.lifecycle_hooks.json" in joined
    assert "selectLoginOptionByText" in joined


async def test_apply_nlp_edit_no_lifecycle_hooks_data_omits_context(tmp_path, monkeypatch):
    """When Step 6 never populated lifecycle_hooks, the extra input is
    simply omitted — never fabricated, never sent as an empty stub."""
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, lifecycle_hooks=None)
    plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(plan),
        on_call=lambda kwargs: captured.update(kwargs),
    )

    await _apply_nlp_edit(plan, plan_path, ctx, _console(), instructions="no-op edit")

    joined = json.dumps(captured.get("messages") or [])
    assert "sut_inventory.lifecycle_hooks.json" not in joined


async def test_apply_nlp_edit_gate_violation_ui_mode_returns_unchanged(tmp_path, monkeypatch):
    """ui_mode (instructions passed directly, as the desktop UI does) must
    never drop into the interactive Prompt.ask recovery flow — it degrades
    to 'plan unchanged' so the dialog can loop back, AND surfaces why via
    the third tuple element so the rejection isn't silently swallowed
    (regression: the UI previously had no way to see this at all)."""
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    original_plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    stale_plan = _plan_with_hook(_STALE_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(original_plan), encoding="utf-8")

    install_fake_anthropic(monkeypatch, text=json.dumps(stale_plan))

    def fail_prompt(*_a, **_kw):  # pragma: no cover - must NOT be called
        raise AssertionError("Prompt.ask must not be called in ui_mode")
    monkeypatch.setattr("qtea.review_gate.Prompt.ask", fail_prompt)

    result = await _apply_nlp_edit(
        original_plan, plan_path, ctx, _console(),
        instructions="redirect the hook to RopaEntitySmoke.spec.ts",
    )

    assert result is not None
    new_plan, diff_text, error_reason = result
    assert new_plan == original_plan
    assert diff_text is None
    assert error_reason is not None
    assert "stale relative to" in error_reason
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original_plan


async def test_apply_nlp_edit_gate_violation_cli_retry_leaves_plan_unchanged(
    tmp_path, monkeypatch,
):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    original_plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    stale_plan = _plan_with_hook(_STALE_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(original_plan), encoding="utf-8")

    install_fake_anthropic(monkeypatch, text=json.dumps(stale_plan))

    prompts = iter(["redirect the hook", "r"])
    monkeypatch.setattr(
        "qtea.review_gate.Prompt.ask", lambda *a, **kw: next(prompts),
    )

    result = await _apply_nlp_edit(original_plan, plan_path, ctx, _console())

    assert result == (original_plan, None, None)
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original_plan


async def test_apply_nlp_edit_gate_violation_cli_approve_anyway_persists(
    tmp_path, monkeypatch,
):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    original_plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    stale_plan = _plan_with_hook(_STALE_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(original_plan), encoding="utf-8")

    install_fake_anthropic(monkeypatch, text=json.dumps(stale_plan))

    prompts = iter(["redirect the hook", "a"])
    monkeypatch.setattr(
        "qtea.review_gate.Prompt.ask", lambda *a, **kw: next(prompts),
    )

    result = await _apply_nlp_edit(original_plan, plan_path, ctx, _console())

    assert result is not None
    new_plan, _diff, _err = result
    assert new_plan == stale_plan
    assert json.loads(plan_path.read_text(encoding="utf-8")) == stale_plan


# ---------------------------------------------------------------------------
# _sync_md_to_json — same gate, file-edit path (no ui_mode branch)
# ---------------------------------------------------------------------------


async def test_sync_md_to_json_gate_violation_retry_leaves_plan_unchanged(
    tmp_path, monkeypatch,
):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    original_plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    stale_plan = _plan_with_hook(_STALE_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(original_plan), encoding="utf-8")

    install_fake_anthropic(monkeypatch, text=json.dumps(stale_plan))
    monkeypatch.setattr("qtea.review_gate.Prompt.ask", lambda *a, **kw: "r")

    result = await _sync_md_to_json(
        "old markdown\n", "new markdown\n", original_plan, plan_path, ctx, _console(),
    )

    assert result == original_plan
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original_plan


async def test_sync_md_to_json_includes_lifecycle_hooks_in_prompt(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(plan),
        on_call=lambda kwargs: captured.update(kwargs),
    )

    await _sync_md_to_json(
        "old markdown\n", "new markdown\n", plan, plan_path, ctx, _console(),
    )

    joined = json.dumps(captured.get("messages") or [])
    assert "sut_inventory.lifecycle_hooks.json" in joined


# ---------------------------------------------------------------------------
# review_step_7_plan (UI mode) — the gate-violation rejection reason must
# reach the dialog. Regression: previously computed but only ever
# console.print'd (terminal-only, invisible in the desktop UI), so a
# rejected hook-reuse edit looked identical to a silently-ignored one.
# ---------------------------------------------------------------------------


async def test_review_step_7_plan_ui_surfaces_gate_violation_on_next_render(
    tmp_path, monkeypatch,
):
    ctx = _ctx(tmp_path)
    ctx.options.ui_mode = True
    _seed_inventory(ctx, _LIFECYCLE_HOOKS)
    original_plan = _plan_with_hook(_GOOD_HOOK_CALLS)
    stale_plan = _plan_with_hook(_STALE_HOOK_CALLS)
    plan_path = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    plan_path.write_text(json.dumps(original_plan), encoding="utf-8")

    install_fake_anthropic(monkeypatch, text=json.dumps(stale_plan))

    hook_calls: list[dict] = []

    def fake_hook(*, step, title, summary_text, kind="", data=None, edit_error=None):
        hook_calls.append({"edit_error": edit_error})
        if len(hook_calls) == 1:
            # First render: no prior edit yet — submit the edit that will
            # be rejected by the hook-reuse phase gate.
            return "edit", "redirect the hook to RopaEntitySmoke.spec.ts"
        # Second render: the gate-violation reason from the first edit
        # attempt must be visible here.
        return "reject", ""

    set_ui_prompt_hook(fake_hook)
    try:
        ok = await review_step_7_plan(
            ctx, StepResult(success=True, status="completed", outputs=[]), _console(),
        )
    finally:
        set_ui_prompt_hook(None)

    assert ok is False  # rejected on the second render
    assert len(hook_calls) == 2
    assert hook_calls[0]["edit_error"] is None
    assert hook_calls[1]["edit_error"] is not None
    assert "stale relative to" in hook_calls[1]["edit_error"]
    # The gate-violation edit must never have been persisted.
    assert json.loads(plan_path.read_text(encoding="utf-8")) == original_plan
