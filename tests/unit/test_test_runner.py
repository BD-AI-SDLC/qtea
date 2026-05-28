"""Unit tests for src/worca_t/test_runner.py."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from worca_t.test_runner import (
    _normalize_id,
    execute_command,
    fallback_status_from_stdout,
    parse_jest_json,
    parse_junit_xml,
    parse_mocha_json,
    parse_playwright_json,
    parse_robot_xml,
    parse_surefire_dir,
    resolve_command,
    run_tests,
)

# ---------------------------------------------------------------------------
# resolve_command
# ---------------------------------------------------------------------------


def test_resolve_command_prefers_detected(tmp_path: Path) -> None:
    cmd, parser = resolve_command("pytest", detected="pytest -k smoke", cwd=tmp_path)
    assert cmd.startswith("pytest -k smoke")
    assert "--junitxml=" in cmd
    assert parser == "junit"


def test_resolve_command_uses_default(tmp_path: Path) -> None:
    cmd, parser = resolve_command("jest", detected=None, cwd=tmp_path)
    assert "jest" in cmd
    assert parser == "jest-json"
    assert "worca-results.json" in cmd


def test_resolve_command_fallback_for_unknown(tmp_path: Path) -> None:
    cmd, parser = resolve_command("nodescript", detected=None, cwd=tmp_path)
    expected = str((tmp_path / "worca-junit.xml").as_posix())
    assert cmd == f"pytest --junitxml={expected}"
    assert parser == "junit"


# ---------------------------------------------------------------------------
# parse_junit_xml
# ---------------------------------------------------------------------------


_JUNIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="suite_a" file="tests/test_a.py">
    <testcase classname="tests.test_a" name="test_passes" time="0.123"/>
    <testcase classname="tests.test_a" name="test_fails" time="0.234">
      <failure message="bad value">Traceback (most recent call last):
  File "tests/test_a.py", line 12, in test_fails
    assert False
AssertionError</failure>
    </testcase>
    <testcase classname="tests.test_a" name="test_skipped">
      <skipped message="needs network"/>
    </testcase>
    <testcase classname="tests.test_a" name="test_errored">
      <error message="boom">tb here</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_xml_handles_all_statuses(tmp_path: Path) -> None:
    p = tmp_path / "junit.xml"
    p.write_text(_JUNIT_XML, encoding="utf-8")
    entries = parse_junit_xml(p)
    by_name = {e.name: e for e in entries}
    assert by_name["test_passes"].status == "passed"
    assert by_name["test_passes"].duration_s == pytest.approx(0.123)
    assert by_name["test_fails"].status == "failed"
    assert by_name["test_fails"].message == "bad value"
    assert "AssertionError" in (by_name["test_fails"].traceback or "")
    assert by_name["test_skipped"].status == "skipped"
    assert by_name["test_errored"].status == "error"
    # File is propagated from suite when not on the case itself.
    assert all(e.file for e in entries)
    # IDs are stable + namespaced.
    assert by_name["test_passes"].id.startswith("T-")


def test_parse_junit_xml_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_junit_xml(tmp_path / "nope.xml") == []


def test_parse_junit_xml_invalid_xml_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "junit.xml"
    p.write_text("<not really xml", encoding="utf-8")
    assert parse_junit_xml(p) == []


def test_parse_junit_xml_single_testsuite_root(tmp_path: Path) -> None:
    xml = """<testsuite name="solo" file="t.py">
      <testcase name="ok" time="0.1"/>
    </testsuite>"""
    p = tmp_path / "junit.xml"
    p.write_text(xml, encoding="utf-8")
    entries = parse_junit_xml(p)
    assert len(entries) == 1
    assert entries[0].status == "passed"


# ---------------------------------------------------------------------------
# parse_playwright_json
# ---------------------------------------------------------------------------


def test_parse_playwright_json_basic() -> None:
    payload = {
        "suites": [
            {
                "file": "tests/login.spec.ts",
                "specs": [
                    {
                        "title": "logs in",
                        "file": "tests/login.spec.ts",
                        "tests": [
                            {"results": [{"status": "expected", "duration": 250}]},
                        ],
                    },
                    {
                        "title": "shows error",
                        "tests": [
                            {
                                "results": [
                                    {
                                        "status": "unexpected",
                                        "duration": 100,
                                        "error": {"message": "bad", "stack": "stk"},
                                        "attachments": [
                                            {"name": "screenshot", "path": "/tmp/s.png"},
                                            {"name": "trace", "path": "/tmp/t.zip"},
                                        ],
                                    }
                                ]
                            }
                        ],
                    },
                ],
                "suites": [
                    {
                        "specs": [
                            {
                                "title": "nested ok",
                                "tests": [{"results": [{"status": "skipped"}]}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    entries = parse_playwright_json(json.dumps(payload))
    by = {e.name: e for e in entries}
    assert by["logs in"].status == "passed"
    assert by["logs in"].duration_s == pytest.approx(0.25)
    assert by["shows error"].status == "failed"
    assert by["shows error"].message == "bad"
    kinds = {a["type"] for a in by["shows error"].attachments}
    assert kinds == {"screenshot", "trace"}
    assert by["nested ok"].status == "skipped"
    # Nested spec inherits parent file
    assert by["nested ok"].file == "tests/login.spec.ts"


def test_parse_playwright_json_handles_garbage() -> None:
    assert parse_playwright_json("not json") == []
    assert parse_playwright_json("{}") == []


# ---------------------------------------------------------------------------
# parse_jest_json + parse_mocha_json
# ---------------------------------------------------------------------------


def test_parse_jest_json(tmp_path: Path) -> None:
    payload = {
        "testResults": [
            {
                "name": "src/__tests__/foo.test.ts",
                "assertionResults": [
                    {"fullName": "foo bar", "status": "passed", "duration": 50},
                    {
                        "fullName": "foo baz",
                        "status": "failed",
                        "duration": 75,
                        "failureMessages": ["Error: bad\n    at line"],
                    },
                    {"title": "qux", "status": "skipped"},
                    {"title": "weird", "status": "garbage"},
                ],
            }
        ]
    }
    p = tmp_path / "jest.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    entries = parse_jest_json(p)
    statuses = {e.name: e.status for e in entries}
    assert statuses == {"foo bar": "passed", "foo baz": "failed", "qux": "skipped", "weird": "error"}
    fail = next(e for e in entries if e.name == "foo baz")
    assert fail.message == "Error: bad"
    assert "at line" in (fail.traceback or "")


def test_parse_jest_json_missing(tmp_path: Path) -> None:
    assert parse_jest_json(tmp_path / "nope.json") == []


def test_parse_mocha_json(tmp_path: Path) -> None:
    payload = {
        "passes": [{"fullTitle": "ok", "file": "t.js", "duration": 10}],
        "failures": [
            {
                "fullTitle": "broken",
                "file": "t.js",
                "duration": 20,
                "err": {"message": "nope", "stack": "tb"},
            }
        ],
        "pending": [{"title": "later", "file": "t.js"}],
    }
    p = tmp_path / "mocha.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    entries = parse_mocha_json(p)
    by = {e.name: e for e in entries}
    assert by["ok"].status == "passed"
    assert by["broken"].status == "failed"
    assert by["later"].status == "skipped"
    assert by["broken"].message == "nope"


# ---------------------------------------------------------------------------
# parse_robot_xml
# ---------------------------------------------------------------------------


def test_parse_robot_xml(tmp_path: Path) -> None:
    xml = """<robot>
      <suite>
        <test name="logs in"><status status="PASS">ok</status></test>
        <test name="errors"><status status="FAIL">oops</status></test>
        <test name="skipped"><status status="SKIP">later</status></test>
      </suite>
    </robot>"""
    p = tmp_path / "robot.xml"
    p.write_text(xml, encoding="utf-8")
    entries = parse_robot_xml(p)
    statuses = {e.name: e.status for e in entries}
    assert statuses == {"logs in": "passed", "errors": "failed", "skipped": "skipped"}


# ---------------------------------------------------------------------------
# parse_surefire_dir
# ---------------------------------------------------------------------------


def test_parse_surefire_dir(tmp_path: Path) -> None:
    d = tmp_path / "reports"
    d.mkdir()
    (d / "TEST-foo.xml").write_text(_JUNIT_XML, encoding="utf-8")
    (d / "TEST-bar.xml").write_text(_JUNIT_XML, encoding="utf-8")
    entries = parse_surefire_dir(d)
    # Each file has 4 cases -> 8 total
    assert len(entries) == 8


# ---------------------------------------------------------------------------
# fallback_status_from_stdout
# ---------------------------------------------------------------------------


def test_fallback_status_from_stdout_picks_failures() -> None:
    log = textwrap.dedent(
        """
        ok 1 - test_a
        FAIL test_b
        ERROR test_c
        """
    )
    out = fallback_status_from_stdout(log)
    assert "test_b" in out
    assert out["test_b"] == "failed"


# ---------------------------------------------------------------------------
# execute_command (real subprocess via current python)
# ---------------------------------------------------------------------------


def _write_py_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_execute_command_runs_python_one_liner(tmp_path: Path) -> None:
    script = _write_py_script(
        tmp_path / "say_hi.py",
        "import sys; sys.stdout.write('hi'); sys.exit(0)",
    )
    cmd = f'{sys.executable} {script.as_posix()}'
    code, stdout, _stderr, dur = execute_command(cmd, cwd=tmp_path, timeout_s=10)
    assert code == 0
    assert "hi" in stdout
    assert dur >= 0


def test_execute_command_missing_returns_127(tmp_path: Path) -> None:
    code, _stdout, stderr, _dur = execute_command(
        "definitely-not-a-command-xyz123", cwd=tmp_path, timeout_s=5
    )
    assert code == 127
    assert "command not found" in stderr


def test_execute_command_timeout(tmp_path: Path) -> None:
    script = _write_py_script(
        tmp_path / "slow.py", "import time; time.sleep(5)"
    )
    cmd = f'{sys.executable} {script.as_posix()}'
    code, _stdout, stderr, _dur = execute_command(cmd, cwd=tmp_path, timeout_s=1)
    assert code == 124
    assert "timeout" in stderr.lower()


# ---------------------------------------------------------------------------
# run_tests end-to-end with a fake junit-emitting "test command"
# ---------------------------------------------------------------------------


def test_run_tests_junit_end_to_end(tmp_path: Path) -> None:
    out_file = (tmp_path / "worca-junit.xml").as_posix()
    body = (
        "import sys\n"
        f"open(r'{out_file}', 'w', encoding='utf-8').write({_JUNIT_XML!r})\n"
        "sys.exit(1)\n"
    )
    script = _write_py_script(tmp_path / "fake_pytest.py", body)
    cmd = f'{sys.executable} {script.as_posix()}'
    result = run_tests("pytest", cwd=tmp_path, detected_command=cmd, timeout_s=15)
    assert result.exit_code == 1
    assert len(result.results) == 4
    statuses = sorted(r.status for r in result.results)
    assert statuses == ["error", "failed", "passed", "skipped"]
    assert result.totals["tests"] == 4
    assert result.totals["failed"] == 1


def test_run_tests_synthesises_runner_failure_when_no_output(tmp_path: Path) -> None:
    script = _write_py_script(tmp_path / "boom.py", "import sys; sys.exit(2)")
    cmd = f'{sys.executable} {script.as_posix()}'
    result = run_tests("pytest", cwd=tmp_path, detected_command=cmd, timeout_s=10)
    assert result.exit_code == 2
    assert len(result.results) == 1
    assert result.results[0].status == "error"
    assert result.results[0].id == "T-runner-failure"


def test_normalize_id_is_stable() -> None:
    a = _normalize_id("tests/test_login.py", "logs in successfully")
    b = _normalize_id("tests/test_login.py", "logs in successfully")
    assert a == b
    assert a.startswith("T-test-login-")
