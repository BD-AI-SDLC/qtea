"""Phase B.6 unit tests.

Covers:
  - ``run_static_check`` orchestration: dispatch lookup, argv composition,
    auto-install fallback, output parsing, scope filtering.
  - ``_run_phase_b6`` happy / autofix / escalate / env-skip / flag-skip paths.

Subprocess and fixer calls are mocked — no real pyright / tsc / Claude
invocations happen here. End-to-end coverage lives in
``tests/unit/test_step08_codegen.py`` (full step run) and the manual
regression described in the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from qtea.static_check import (
    StaticCheckResult,
    TYPE_ERROR_RULE,
    _filter_to_scope,
    _parse_pyright_json,
    _parse_tsc_text,
    format_for_fixer,
    run_static_check,
)
from qtea.steps.s08_codegen import _run_phase_b6
from qtea.test_indexer import Violation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pyright_payload(items: list[dict]) -> str:
    return json.dumps({"version": "1.1.300", "generalDiagnostics": items})


def _diag(file: str, line: int, msg: str, rule: str = "reportAttributeAccessIssue",
          severity: str = "error") -> dict:
    return {
        "file": file,
        "severity": severity,
        "message": msg,
        "rule": rule,
        "range": {"start": {"line": line - 1, "character": 0},
                  "end": {"line": line - 1, "character": 10}},
    }


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def test_pyright_json_parser_keeps_only_errors(tmp_path: Path):
    sut = tmp_path
    (sut / "test_foo.py").write_text("", encoding="utf-8")
    stdout = _make_pyright_payload([
        _diag(str(sut / "test_foo.py"), 3, "No attribute X", severity="error"),
        _diag(str(sut / "test_foo.py"), 5, "Unused import", severity="warning"),
    ])
    out = _parse_pyright_json(stdout, sut)
    assert len(out) == 1
    assert out[0].line == 3
    assert out[0].rule == TYPE_ERROR_RULE
    assert "reportAttributeAccessIssue" in out[0].snippet


def test_tsc_text_parser_extracts_file_line_code(tmp_path: Path):
    sut = tmp_path
    (sut / "tests" / "qtea_foo.test.ts").parent.mkdir(parents=True, exist_ok=True)
    (sut / "tests" / "qtea_foo.test.ts").write_text("", encoding="utf-8")
    abs_file = str(sut / "tests" / "qtea_foo.test.ts")
    stdout = "\n".join([
        f"{abs_file}(12,5): error TS2339: Property 'FOO' does not exist on type 'Bar'.",
        "irrelevant noise line",
        f"{abs_file}(20,1): error TS2304: Cannot find name 'undefinedSym'.",
    ])
    out = _parse_tsc_text(stdout, sut)
    assert len(out) == 2
    assert [v.line for v in out] == [12, 20]
    assert "TS2339" in out[0].snippet
    assert "TS2304" in out[1].snippet


# ---------------------------------------------------------------------------
# Scope filter
# ---------------------------------------------------------------------------


def test_scope_filter_splits_in_and_out(tmp_path: Path):
    sut = tmp_path
    tests_dir = sut / "tests"
    tests_dir.mkdir()
    in_file = tests_dir / "qtea_foo_test.py"
    app_file = sut / "src" / "app" / "foo.py"
    app_file.parent.mkdir(parents=True)
    in_file.write_text("", encoding="utf-8")
    app_file.write_text("", encoding="utf-8")

    violations = [
        Violation(rule=TYPE_ERROR_RULE, file="tests/qtea_foo_test.py",
                  line=3, snippet="boom", severity="error"),
        Violation(rule=TYPE_ERROR_RULE, file="src/app/foo.py",
                  line=99, snippet="user code", severity="error"),
    ]
    qteaouched = {in_file}
    in_scope, out_of_scope = _filter_to_scope(violations, qteaouched, sut)
    assert len(in_scope) == 1
    assert in_scope[0].file == "tests/qtea_foo_test.py"
    assert in_scope[0].severity == "error"
    assert len(out_of_scope) == 1
    assert out_of_scope[0].severity == "out_of_scope"


# ---------------------------------------------------------------------------
# run_static_check — autoinstall path
# ---------------------------------------------------------------------------


def test_run_static_check_autoinstalls_missing_pyright(tmp_path: Path, monkeypatch):
    """When pyright is absent and a programmatic install argv exists, the
    autoinstall path runs and the checker is retried."""
    sut = tmp_path
    (sut / "pyproject.toml").write_text(
        "[tool.poetry]\nname = 'fake'\nversion = '0'\n", encoding="utf-8"
    )
    (sut / "poetry.lock").write_text("", encoding="utf-8")
    test_file = sut / "tests" / "qtea_login_test.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("", encoding="utf-8")

    # First execute_command -> tool not found via wrapped probe; then
    # install succeeds; then the actual checker runs and reports zero errors.
    call_log = []

    def fake_execute(command, *, cwd, timeout_s, env_extra=None, isolate_venv=False):
        call_log.append(command)
        if "--version" in command:
            return 127, "", "command not found", 0.1
        if "poetry add" in command or "uv add" in command or "pip install" in command:
            return 0, "installed", "", 0.5
        # The real checker call — return a clean run.
        return 0, _make_pyright_payload([]), "", 0.2

    monkeypatch.setattr("qtea.static_check.execute_command", fake_execute)
    # _tool_available falls back to shutil.which when wrapper_prefix is None.
    # Force it through the wrapped-probe path by ensuring detect_stack_profile
    # returns a profile with a wrapper_prefix.
    from qtea.static_check import StackProfile

    def fake_detect(_):
        return StackProfile(
            language="python", package_manager="poetry",
            wrapper_prefix="poetry run", venv_path=".venv",
        )
    monkeypatch.setattr("qtea.static_check.detect_stack_profile", fake_detect)

    result = run_static_check(
        sut, framework="pytest", qteaouched={test_file}, timeout_s=30,
    )
    assert result.ran is True
    assert result.in_scope_errors == 0
    # Verify the autoinstall command actually fired.
    assert any("poetry add" in c or "uv add" in c or "pip install" in c
               for c in call_log), f"no install command in {call_log}"


# ---------------------------------------------------------------------------
# _run_phase_b6 orchestration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_b6_happy_path_no_violations(tmp_path: Path, monkeypatch):
    """Clean checker output -> no fixer invocation, ran=True, errors=0."""
    sut = tmp_path
    clean = StaticCheckResult(
        tool="pyright", stack="pytest", ran=True, skipped_reason=None,
        duration_s=0.2, exit_code=0, in_scope_errors=0, out_of_scope_errors=0,
        autofix_attempted=False, post_fix_errors=0,
    )
    monkeypatch.setattr(
        "qtea.steps.s08_codegen.run_static_check", lambda *a, **kw: clean,
    )
    fix_agent_call = AsyncMock()
    monkeypatch.setattr("qtea.steps.s08_codegen.run_agent", fix_agent_call)

    result = await _run_phase_b6(
        sut_root=sut, framework="pytest", qteaouched=set(),
        agents_root=tmp_path / "agents", workdir=tmp_path / "wd",
        timeout_s=300,
    )
    assert result.ran is True
    assert result.in_scope_errors == 0
    assert result.autofix_attempted is False
    fix_agent_call.assert_not_called()


@pytest.mark.asyncio
async def test_phase_b6_one_retry_then_escalates(tmp_path: Path, monkeypatch):
    """Errors -> fixer runs ONCE -> re-check still has errors ->
    post_fix_errors > 0, autofix_attempted True. Caller will fail the step."""
    sut = tmp_path
    initial = StaticCheckResult(
        tool="pyright", stack="pytest", ran=True, skipped_reason=None,
        duration_s=0.2, exit_code=1, in_scope_errors=2, out_of_scope_errors=0,
        autofix_attempted=False, post_fix_errors=0,
        violations=[
            Violation(rule=TYPE_ERROR_RULE, file="tests/qtea_foo.py",
                      line=3, snippet="boom1", severity="error"),
            Violation(rule=TYPE_ERROR_RULE, file="tests/qtea_foo.py",
                      line=7, snippet="boom2", severity="error"),
        ],
    )
    after_fix = StaticCheckResult(
        tool="pyright", stack="pytest", ran=True, skipped_reason=None,
        duration_s=0.1, exit_code=1, in_scope_errors=2, out_of_scope_errors=0,
        autofix_attempted=False, post_fix_errors=0,
        violations=initial.violations,
    )
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return initial if call_count["n"] == 1 else after_fix

    monkeypatch.setattr("qtea.steps.s08_codegen.run_static_check", fake_run)
    fix_agent_call = AsyncMock()
    monkeypatch.setattr("qtea.steps.s08_codegen.run_agent", fix_agent_call)

    result = await _run_phase_b6(
        sut_root=sut, framework="pytest", qteaouched=set(),
        agents_root=tmp_path / "agents", workdir=tmp_path / "wd",
        timeout_s=300,
    )
    # Fixer must run exactly once (B6_MAX_AUTOPATCH_RETRIES = 1).
    fix_agent_call.assert_awaited_once()
    assert result.autofix_attempted is True
    assert result.post_fix_errors == 2  # still failing -> caller will fail step
    assert call_count["n"] == 2          # checker invoked twice (initial + recheck)


@pytest.mark.asyncio
async def test_phase_b6_skipped_when_env_set(tmp_path: Path, monkeypatch):
    """QTEA_SKIP_STATIC_CHECK=1 -> phase short-circuits, no checker call."""
    monkeypatch.setenv("QTEA_SKIP_STATIC_CHECK", "1")
    run_call = AsyncMock()
    monkeypatch.setattr("qtea.steps.s08_codegen.run_static_check", run_call)

    result = await _run_phase_b6(
        sut_root=tmp_path, framework="pytest", qteaouched=set(),
        agents_root=tmp_path / "agents", workdir=tmp_path / "wd",
        timeout_s=300,
    )
    assert result.ran is False
    assert result.skipped_reason == "env_skip"
    run_call.assert_not_called()


@pytest.mark.asyncio
async def test_phase_b6_skipped_by_flag(tmp_path: Path, monkeypatch):
    """--no-static-check sets QTEA_NO_STATIC_CHECK=1 -> short-circuit."""
    monkeypatch.delenv("QTEA_SKIP_STATIC_CHECK", raising=False)
    monkeypatch.setenv("QTEA_NO_STATIC_CHECK", "1")
    run_call = AsyncMock()
    monkeypatch.setattr("qtea.steps.s08_codegen.run_static_check", run_call)

    result = await _run_phase_b6(
        sut_root=tmp_path, framework="pytest", qteaouched=set(),
        agents_root=tmp_path / "agents", workdir=tmp_path / "wd",
        timeout_s=300,
    )
    assert result.ran is False
    assert result.skipped_reason == "flag_skip"
    run_call.assert_not_called()


# ---------------------------------------------------------------------------
# Cross-language coverage smoke
# ---------------------------------------------------------------------------


def test_dispatch_covers_python_typescript_and_javascript():
    """Regression guard for the JS-coverage extension: dispatch must include
    at least one pure-JS stack (playwright-js) alongside TS-capable stacks,
    so jest/mocha/cypress projects targeting .js test files are gated too."""
    from qtea.static_check import _DISPATCH

    py_stacks = {"pytest", "playwright-py", "selenium-py"}
    js_ts_stacks = {"playwright-ts", "playwright-js", "jest", "vitest",
                    "mocha", "wdio", "cypress"}
    assert py_stacks.issubset(_DISPATCH.keys())
    assert js_ts_stacks.issubset(_DISPATCH.keys())
    # All Python stacks must dispatch to pyright; all JS/TS to tsc.
    for s in py_stacks:
        assert _DISPATCH[s][0] == "pyright"
    for s in js_ts_stacks:
        assert _DISPATCH[s][0] == "tsc"
