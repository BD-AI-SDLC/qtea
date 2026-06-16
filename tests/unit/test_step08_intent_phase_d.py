"""Tests for Step 8 Phase D (TBD intent quality gate)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from worca_t.review_gate import _replace_intent_at_line, review_step_8_intents
from worca_t.steps.s08_codegen import _phase_d_score_intents

from ._fake_anthropic import install_fake_anthropic


# ---------------------------------------------------------------------------
# _phase_d_score_intents
# ---------------------------------------------------------------------------


def _seed_locator_file(tmp_path: Path, intents: list[str]) -> Path:
    """Write a Python locator file with the given intents and return its path.

    Each intent gets its own ALL_CAPS constant on a separate line so the
    scanner's line numbers are predictable.
    """
    src = tmp_path / "worca_login_locators.py"
    body = ["from tests.worca_t_runtime import tbd", ""]
    for i, intent in enumerate(intents):
        body.append(f"L{i} = tbd(\"{intent}\")")
    src.write_text("\n".join(body) + "\n", encoding="utf-8")
    return src


def _agents_root_with_scorer(tmp_path: Path) -> Path:
    """Stage a fake agents/ dir with the scorer agent file present."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "tbd-intent-scorer.agent.md").write_text(
        "---\nname: tbd-intent-scorer\nmodel: claude-haiku-4-5\n"
        "transport: reasoning\n---\nstub agent for tests\n",
        encoding="utf-8",
    )
    return agents


async def test_phase_d_skip_via_env(tmp_path: Path, monkeypatch):
    """WORCA_T_SKIP_INTENT_SCORE=1 short-circuits with no LLM call."""
    monkeypatch.setenv("WORCA_T_SKIP_INTENT_SCORE", "1")
    src = _seed_locator_file(tmp_path, ["sign in button"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    # Hard fail if any LLM call sneaks through.
    fake = install_fake_anthropic(monkeypatch, text="should never be called")

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert summary == {"skipped": True, "reason": "env_skip"}
    assert warnings == []
    assert error is None
    assert fake.call_count == 0


async def test_phase_d_no_intents_short_circuits(tmp_path: Path, monkeypatch):
    """When the source contains no TBD sentinels, Phase D is a no-op."""
    # Empty support file — no tbd() calls.
    src = tmp_path / "worca_helpers.py"
    src.write_text("def util(): pass\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    fake = install_fake_anthropic(monkeypatch, text="unused")

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert summary["summary"]["total"] == 0
    assert warnings == []
    assert error is None
    assert fake.call_count == 0


async def test_phase_d_excludes_jit_runtime_files(tmp_path: Path, monkeypatch):
    """JIT runtime files (e.g. worca_t_runtime.py) must be excluded from the
    scan even when they appear in produced_in_sut via the worca_* rglob.
    The runtime template contains docstring examples with tbd() calls that
    would otherwise produce false-positive intent entries."""
    # Seed a real test file with one tbd() call.
    real_test = tmp_path / "worca_login_test.py"
    real_test.write_text(
        'from tests.worca_t_runtime import tbd\n'
        'SUBMIT = tbd("submit button on login form")\n',
        encoding="utf-8",
    )
    # Seed a JIT runtime file with docstring examples (mirrors the template).
    jit_file = tmp_path / "worca_t_runtime.py"
    jit_file.write_text(
        'def tbd(intent: str) -> str:\n'
        '    """Usage::\n'
        '\n'
        '        class LoginLocators:\n'
        '            LOGIN_BUTTON = tbd("primary submit button on the login form")\n'
        '            PASSWORD_INPUT = tbd("password input on the sign-in form")\n'
        '    """\n'
        '    return f"__WORCA_T_TBD__::{intent}"\n',
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    scorer_response = json.dumps({"results": [
        {"intent": "submit button on login form", "score": "PASS",
         "rationale": "role+label"},
    ]})
    install_fake_anthropic(monkeypatch, text=scorer_response)

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[real_test, jit_file],
        jit_files_added=[jit_file],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    # Only the real test's intent should appear — not LOGIN_BUTTON / PASSWORD_INPUT.
    assert summary["summary"]["total"] == 1
    assert summary["results"][0]["intent"] == "submit button on login form"


async def test_phase_d_all_pass_returns_success_no_warnings(
    tmp_path: Path, monkeypatch,
):
    src = _seed_locator_file(tmp_path, ["sign in button", "username input"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    scorer_response = json.dumps({"results": [
        {"intent": "sign in button", "score": "PASS",
         "rationale": "role+label"},
        {"intent": "username input", "score": "PASS",
         "rationale": "role+label"},
    ]})
    install_fake_anthropic(monkeypatch, text=scorer_response)

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert summary["summary"] == {"pass": 2, "warn": 0, "fail": 0, "total": 2}
    assert warnings == []
    assert error is None


async def test_phase_d_any_fail_fails_step(tmp_path: Path, monkeypatch):
    src = _seed_locator_file(tmp_path, ["sign in", "#login-btn"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    scorer_response = json.dumps({"results": [
        {"intent": "sign in", "score": "PASS", "rationale": "ok"},
        {"intent": "#login-btn", "score": "FAIL",
         "rationale": "literal CSS selector"},
    ]})
    install_fake_anthropic(monkeypatch, text=scorer_response)

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is False
    assert "FAIL" in (error or "")
    assert summary["summary"]["fail"] == 1
    # Both FAIL and WARN flow to warnings_list so a follow-up review can see them.
    assert any(w["intent"] == "#login-btn" for w in warnings)


async def test_phase_d_warn_only_succeeds_with_stash(
    tmp_path: Path, monkeypatch,
):
    src = _seed_locator_file(tmp_path, ["submit"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    scorer_response = json.dumps({"results": [
        {"intent": "submit", "score": "WARN", "rationale": "ambiguous"},
    ]})
    install_fake_anthropic(monkeypatch, text=scorer_response)

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert summary["summary"] == {"pass": 0, "warn": 1, "fail": 0, "total": 1}
    assert len(warnings) == 1
    assert warnings[0]["intent"] == "submit"
    assert warnings[0]["score"] == "WARN"


async def test_phase_d_fail_as_warn_env_downgrades(
    tmp_path: Path, monkeypatch,
):
    """WORCA_T_INTENT_FAIL_AS_WARN=1 — FAIL no longer blocks; flows as WARN."""
    monkeypatch.setenv("WORCA_T_INTENT_FAIL_AS_WARN", "1")
    src = _seed_locator_file(tmp_path, ["#login"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    scorer_response = json.dumps({"results": [
        {"intent": "#login", "score": "FAIL",
         "rationale": "literal selector"},
    ]})
    install_fake_anthropic(monkeypatch, text=scorer_response)

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert error is None
    assert summary["summary"]["fail"] == 1
    # FAIL still surfaces in warnings_list so the review gate can render it.
    assert len(warnings) == 1
    assert warnings[0]["score"] == "FAIL"


async def test_phase_d_scorer_failure_does_not_block(
    tmp_path: Path, monkeypatch,
):
    """If the scorer agent itself errors out, Phase D returns a soft success
    so a transient Anthropic API hiccup doesn't tank the whole pipeline."""
    src = _seed_locator_file(tmp_path, ["a", "b"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    install_fake_anthropic(
        monkeypatch, text="", raises=RuntimeError("API blew up"),
    )

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert "scorer_error" in summary
    assert warnings == []
    assert error is None


async def test_phase_d_handles_unparseable_scorer_response(
    tmp_path: Path, monkeypatch,
):
    src = _seed_locator_file(tmp_path, ["a"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    install_fake_anthropic(monkeypatch, text="this is not json {")

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    assert "scorer_error" in summary


async def test_phase_d_pads_missing_scorer_entries_to_warn(
    tmp_path: Path, monkeypatch,
):
    """If the scorer drops entries, the gap is filled with WARN so the
    pipeline still has 1:1 anchors. Surfaces in a count-mismatch log line."""
    src = _seed_locator_file(tmp_path, ["a", "b", "c"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()
    agents = _agents_root_with_scorer(tmp_path)

    # Only 2 of 3 returned.
    install_fake_anthropic(monkeypatch, text=json.dumps({"results": [
        {"intent": "a", "score": "PASS", "rationale": "ok"},
        {"intent": "b", "score": "PASS", "rationale": "ok"},
    ]}))

    ok, summary, warnings, error = await _phase_d_score_intents(
        produced_in_sut=[src],
        jit_files_added=[],
        sut_root=tmp_path,
        out_dir=out_dir,
        workdir=wd,
        agents_root=agents,
    )
    assert ok is True
    # Pad: third entry defaults to WARN.
    assert summary["summary"]["total"] == 3
    assert summary["summary"]["warn"] >= 1
    assert any(w["intent"] == "c" for w in warnings)


# ---------------------------------------------------------------------------
# _replace_intent_at_line (pure)
# ---------------------------------------------------------------------------


def test_replace_intent_preserves_quote_style():
    src = "L0 = tbd(\"old intent\")\n"
    out, ok = _replace_intent_at_line(src, 1, "old intent", "new intent")
    assert ok is True
    assert out == "L0 = tbd(\"new intent\")\n"


def test_replace_intent_handles_single_quotes():
    src = "L0 = tbd('old')\n"
    out, ok = _replace_intent_at_line(src, 1, "old", "new")
    assert ok is True
    assert out == "L0 = tbd('new')\n"


def test_replace_intent_handles_backticks():
    src = "const X = tbd(`old`);\n"
    out, ok = _replace_intent_at_line(src, 1, "old", "new")
    assert ok is True
    assert out == "const X = tbd(`new`);\n"


def test_replace_intent_out_of_range_returns_false():
    src = "L0 = tbd(\"x\")\n"
    out, ok = _replace_intent_at_line(src, 5, "x", "y")
    assert ok is False
    assert out == src


def test_replace_intent_not_found_on_line_returns_false():
    src = "L0 = tbd(\"x\")\nL1 = tbd(\"y\")\n"
    out, ok = _replace_intent_at_line(src, 1, "y", "z")
    assert ok is False
    assert out == src


def test_replace_intent_only_touches_target_line():
    src = "L0 = tbd(\"x\")\nL1 = tbd(\"x\")\nL2 = tbd(\"x\")\n"
    out, ok = _replace_intent_at_line(src, 2, "x", "y")
    assert ok is True
    lines = out.splitlines()
    assert lines == ["L0 = tbd(\"x\")", "L1 = tbd(\"y\")", "L2 = tbd(\"x\")"]


# ---------------------------------------------------------------------------
# review_step_8_intents — non-TTY auto-approves
# ---------------------------------------------------------------------------


async def test_review_gate_auto_approves_in_no_hitl(tmp_path: Path):
    """The gate must NOT prompt or call any LLM when --no-hitl is active."""
    ctx = SimpleNamespace(
        options=SimpleNamespace(no_hitl=True),
        extras={"step8_intent_warnings": [
            {"file": "x.py", "line": 1, "constant_name": "X",
             "intent": "submit", "score": "WARN", "rationale": "vague"},
        ]},
        workspace=SimpleNamespace(sut=tmp_path),
    )
    result = SimpleNamespace(success=True, outputs=[])
    console = MagicMock()
    ok = await review_step_8_intents(ctx, result, console)
    assert ok is True
    # No prompt was rendered.
    console.print.assert_not_called()


async def test_review_gate_returns_true_when_no_warnings(tmp_path: Path):
    """Empty warnings list should bypass the gate entirely (no prompt)."""
    # Make stdin appear to be a TTY so the no_hitl check isn't the one
    # short-circuiting; the empty warnings list itself must be the bypass.
    ctx = SimpleNamespace(
        options=SimpleNamespace(no_hitl=False),
        extras={"step8_intent_warnings": []},
        workspace=SimpleNamespace(sut=tmp_path),
    )
    result = SimpleNamespace(success=True, outputs=[])
    console = MagicMock()

    # Pretend TTY by monkey-patching sys.stdin.isatty — must NOT prompt because
    # the warnings list is empty.
    original_isatty = sys.stdin.isatty
    try:
        sys.stdin.isatty = lambda: True  # type: ignore[method-assign]
        ok = await review_step_8_intents(ctx, result, console)
    finally:
        sys.stdin.isatty = original_isatty  # type: ignore[method-assign]
    assert ok is True
