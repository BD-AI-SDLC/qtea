"""Phase B.6.5 (language-native parse gate) unit tests.

Covers:
  - Python ast.parse backend (always available, catches SyntaxError).
  - TypeScript / JavaScript / Java backends: presence-detection + fallback ladder.
  - Regex smoke check: catches the `# Stack:` regression from run
    20260701-114656-9394eb when no native TS parser is available.
  - Degraded-mode gate: a regex-smoke violation on a language where every real
    parser was absent triggers `has_degraded_violations() == True` so the
    caller can hard-fail with a "install X" message.
  - Aggregate ParseCheckResult schema-validates against
    ``parse-check-result.schema.json``.

Subprocess invocations of tsc/node/javac are mocked so tests are hermetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from qtea.parse_check import (
    PARSE_ERROR_RULE,
    ParseCheckResult,
    ParseFileResult,
    _check_python,
    format_for_fixer,
    has_degraded_violations,
    run_parse_check,
)
from qtea.schemas import is_valid


# ---------------------------------------------------------------------------
# Python: ast.parse (always available)
# ---------------------------------------------------------------------------


def test_python_valid_source_ok(tmp_path: Path):
    p = tmp_path / "test_foo.py"
    p.write_text("import os\n\ndef f(): return 1\n", encoding="utf-8")
    r = _check_python(p, "test_foo.py")
    assert r.ran and r.ok
    assert r.backend_used == "ast.parse"
    assert r.error_line is None


def test_python_syntax_error_surfaces_line(tmp_path: Path):
    p = tmp_path / "test_bad.py"
    p.write_text("def f(:\n    pass\n", encoding="utf-8")
    r = _check_python(p, "test_bad.py")
    assert r.ran and not r.ok
    assert r.error_line == 1
    assert r.error_message is not None


# ---------------------------------------------------------------------------
# Regex smoke check: the run 20260701-114656-9394eb regression
# ---------------------------------------------------------------------------


def test_ts_hash_header_caught_by_smoke_when_no_native_backend(tmp_path: Path):
    """The canonical regression: a `.spec.ts` file whose first line is
    `# Stack: typescript+playwright`. With tsc, node, and npx all unavailable,
    the regex smoke check must catch it and the aggregate result must flag
    the language as degraded so the caller hard-fails."""
    sut = tmp_path
    tests = sut / "tests" / "regression"
    tests.mkdir(parents=True)
    bad = tests / "qtea_ropa_approval_test.spec.ts"
    bad.write_text(
        "# Stack: typescript+playwright\n"
        "\n"
        "import { test, expect } from \"../../src/fixtures/pageFixtures\";\n",
        encoding="utf-8",
    )

    with patch("qtea.parse_check.shutil.which", return_value=None):
        result = run_parse_check(sut, qtea_files={bad})

    assert result.ran
    assert result.files_checked == 1
    assert result.in_scope_errors == 1
    assert "typescript" in result.degraded_languages
    assert set(result.missing_tools) >= {"tsc", "node"}
    v = result.violations[0]
    assert v.rule == PARSE_ERROR_RULE
    assert v.line == 1
    assert "Python-style" in v.snippet
    assert has_degraded_violations(result) is True


def test_ts_valid_file_smoke_ok_when_no_native_backend(tmp_path: Path):
    """A well-formed .spec.ts must pass the smoke check even when tsc/node are
    absent — the degraded mode should not create false positives."""
    sut = tmp_path
    tests = sut / "tests"
    tests.mkdir()
    good = tests / "qtea_smoke.spec.ts"
    good.write_text(
        "// Stack: typescript+playwright\n\n"
        "import { test, expect } from '@playwright/test';\n"
        "test('smoke', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    with patch("qtea.parse_check.shutil.which", return_value=None):
        result = run_parse_check(sut, qtea_files={good})

    assert result.ran
    assert result.files_checked == 1
    assert result.in_scope_errors == 0
    # Language degrades because no real backend ran, but no violation → the
    # aggregate hard-fail gate does NOT trip.
    assert "typescript" in result.degraded_languages
    assert has_degraded_violations(result) is False


def test_python_files_bypass_missing_native_backends(tmp_path: Path):
    """Python check runs via stdlib ast.parse regardless of which subprocess
    tools are on PATH. Missing node/tsc must NOT flag Python as degraded."""
    sut = tmp_path
    p = sut / "tests" / "test_ok.py"
    p.parent.mkdir(parents=True)
    p.write_text("def f(): return 1\n", encoding="utf-8")

    with patch("qtea.parse_check.shutil.which", return_value=None):
        result = run_parse_check(sut, qtea_files={p})

    assert result.ran
    assert result.in_scope_errors == 0
    assert "python" not in result.degraded_languages
    assert result.file_results[0].backend_used == "ast.parse"


# ---------------------------------------------------------------------------
# TypeScript: real tsc backend (mocked subprocess)
# ---------------------------------------------------------------------------


def test_ts_uses_tsc_when_available(tmp_path: Path):
    sut = tmp_path
    (sut / "tests").mkdir()
    good = sut / "tests" / "qtea_good.spec.ts"
    good.write_text("// Stack: typescript+playwright\nexport const X = 1;\n",
                    encoding="utf-8")

    def _which(name: str):
        return "/usr/bin/tsc" if name == "tsc" else None

    fake_run = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with (
        patch("qtea.parse_check.shutil.which", side_effect=_which),
        patch("qtea.parse_check.subprocess.run", return_value=fake_run),
    ):
        result = run_parse_check(sut, qtea_files={good})

    assert result.ran
    assert result.in_scope_errors == 0
    assert result.file_results[0].backend_used == "tsc"
    assert result.degraded_languages == []


def test_ts_tsc_syntax_error_reported(tmp_path: Path):
    sut = tmp_path
    (sut / "tests").mkdir()
    bad = sut / "tests" / "qtea_bad.spec.ts"
    bad.write_text("garbage\n", encoding="utf-8")

    def _which(name: str):
        return "/usr/bin/tsc" if name == "tsc" else None

    # tsc emits `path.ts(LINE,COL): error TS1005: <message>`
    fake_stdout = f"{bad}(1,1): error TS1005: ';' expected.\n"
    fake_run = type("R", (), {
        "returncode": 1, "stdout": fake_stdout, "stderr": "",
    })()
    with (
        patch("qtea.parse_check.shutil.which", side_effect=_which),
        patch("qtea.parse_check.subprocess.run", return_value=fake_run),
    ):
        result = run_parse_check(sut, qtea_files={bad})

    assert result.ran
    assert result.in_scope_errors == 1
    v = result.violations[0]
    assert v.rule == PARSE_ERROR_RULE
    assert v.line == 1


# ---------------------------------------------------------------------------
# Env opt-outs
# ---------------------------------------------------------------------------


def test_env_no_parse_check_short_circuits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QTEA_NO_PARSE_CHECK", "1")
    p = tmp_path / "foo.py"
    p.write_text("!!! not python !!!\n", encoding="utf-8")
    result = run_parse_check(tmp_path, qtea_files={p})
    assert result.ran is False
    assert result.skipped_reason == "flag_skip"
    assert result.in_scope_errors == 0


def test_env_skip_parse_check_short_circuits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QTEA_SKIP_PARSE_CHECK", "1")
    p = tmp_path / "foo.py"
    p.write_text("!!! not python !!!\n", encoding="utf-8")
    result = run_parse_check(tmp_path, qtea_files={p})
    assert result.ran is False
    assert result.skipped_reason == "env_skip"


# ---------------------------------------------------------------------------
# Schema + formatter
# ---------------------------------------------------------------------------


def test_parse_check_result_serialises_and_schema_validates(tmp_path: Path):
    p = tmp_path / "test_ok.py"
    p.write_text("x = 1\n", encoding="utf-8")
    result = run_parse_check(tmp_path, qtea_files={p})
    payload = result.as_dict()
    # Round-trips through JSON cleanly.
    json.dumps(payload)
    ok, err = is_valid(payload, "parse-check-result")
    assert ok, f"schema validation failed: {err}"


def test_format_for_fixer_renders_violation_lines():
    result = ParseCheckResult(
        ran=True, skipped_reason=None, duration_s=0.01,
        files_checked=1, in_scope_errors=1,
    )
    from qtea.test_indexer import Violation
    result.violations = [Violation(
        rule=PARSE_ERROR_RULE, file="tests/x.spec.ts", line=1,
        snippet="line 1 uses Python-style `#` comment",
    )]
    rendered = format_for_fixer(result)
    assert "1 parse error(s)" in rendered
    assert "tests/x.spec.ts:1" in rendered
    assert PARSE_ERROR_RULE in rendered


# ---------------------------------------------------------------------------
# has_degraded_violations
# ---------------------------------------------------------------------------


def test_has_degraded_violations_true_when_regex_smoke_fires():
    result = ParseCheckResult(
        ran=True, skipped_reason=None, duration_s=0.01,
        files_checked=1, in_scope_errors=1,
        degraded_languages=["typescript"], missing_tools=["tsc", "node"],
        file_results=[ParseFileResult(
            file="tests/x.spec.ts", language="typescript",
            backend_used="regex-smoke", ran=True, ok=False,
            error_line=1, error_message="hash header",
            skipped_reason=None,
        )],
    )
    from qtea.test_indexer import Violation
    result.violations = [Violation(
        rule=PARSE_ERROR_RULE, file="tests/x.spec.ts", line=1,
        snippet="hash header",
    )]
    assert has_degraded_violations(result) is True


def test_has_degraded_violations_false_when_real_backend_used():
    """A violation surfaced by tsc / node / javac is fully trusted."""
    result = ParseCheckResult(
        ran=True, skipped_reason=None, duration_s=0.01,
        files_checked=1, in_scope_errors=1,
        file_results=[ParseFileResult(
            file="tests/x.spec.ts", language="typescript",
            backend_used="tsc", ran=True, ok=False,
            error_line=1, error_message="TS1005",
            skipped_reason=None,
        )],
    )
    from qtea.test_indexer import Violation
    result.violations = [Violation(
        rule=PARSE_ERROR_RULE, file="tests/x.spec.ts", line=1,
        snippet="TS1005",
    )]
    assert has_degraded_violations(result) is False
