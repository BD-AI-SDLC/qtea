"""Unit tests for src/worca_t/test_runner.py."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from worca_t.test_runner import (
    _normalize_id,
    audit_missing_deps,
    classify_runner_failure,
    execute_command,
    fallback_status_from_stdout,
    install_command_for,
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
# resolve_command with StackProfile (wrapping)
# ---------------------------------------------------------------------------


def test_resolve_command_wraps_default_with_poetry(tmp_path: Path) -> None:
    from worca_t.stack_profile import StackProfile

    profile = StackProfile(package_manager="poetry", wrapper_prefix="poetry run")
    cmd, parser = resolve_command(
        "playwright-py", detected=None, cwd=tmp_path, profile=profile,
    )
    assert cmd.startswith("poetry run pytest")
    assert "--junitxml=" in cmd
    assert parser == "junit"


def test_resolve_command_wraps_default_with_npx(tmp_path: Path) -> None:
    from worca_t.stack_profile import StackProfile

    profile = StackProfile(package_manager="npm", wrapper_prefix="npx")
    cmd, parser = resolve_command(
        "playwright-ts", detected=None, cwd=tmp_path, profile=profile,
    )
    assert cmd.startswith("npx npx playwright test") or cmd.startswith("npx playwright test")
    assert parser == "playwright-json"


def test_resolve_command_detected_overrides_wrapping(tmp_path: Path) -> None:
    """When the researcher gives an explicit command, we use it verbatim."""
    from worca_t.stack_profile import StackProfile

    profile = StackProfile(package_manager="poetry", wrapper_prefix="poetry run")
    cmd, _ = resolve_command(
        "playwright-py",
        detected="poetry run pytest -m smoke",
        cwd=tmp_path,
        profile=profile,
    )
    assert cmd.startswith("poetry run pytest -m smoke")
    # No double wrapping.
    assert cmd.count("poetry run") == 1


def test_resolve_command_unknown_framework_wrapped(tmp_path: Path) -> None:
    """Bare-pytest fallback also gets wrapped when a profile is provided."""
    from worca_t.stack_profile import StackProfile

    profile = StackProfile(package_manager="poetry", wrapper_prefix="poetry run")
    cmd, _ = resolve_command("xyz", detected=None, cwd=tmp_path, profile=profile)
    assert cmd.startswith("poetry run pytest")


def test_prepare_sut_no_profile_is_noop(tmp_path: Path) -> None:
    from worca_t.test_runner import prepare_sut

    result = prepare_sut(None, cwd=tmp_path)
    assert result.ran is False
    assert result.ok() is True
    assert result.skip_reason


def test_prepare_sut_no_install_command_is_noop(tmp_path: Path) -> None:
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    profile = StackProfile(package_manager="custom", wrapper_prefix="x")
    result = prepare_sut(profile, cwd=tmp_path)
    assert result.ran is False
    assert result.ok() is True


def test_prepare_sut_runs_command(tmp_path: Path) -> None:
    """A command that exists and exits 0 yields ran=True, ok=True."""
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    profile = StackProfile(
        package_manager="custom",
        install_command=f"{sys.executable} -c \"pass\"",
    )
    result = prepare_sut(profile, cwd=tmp_path, timeout_s=30)
    assert result.ran is True
    assert result.exit_code == 0, f"stderr={result.stderr!r}"
    assert result.ok() is True


def test_prepare_sut_runs_pre_install_then_install(tmp_path: Path) -> None:
    """When profile has pre_install_command + install_command, both run in order."""
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    marker = tmp_path / "pre_ran.txt"
    pre_script = _write_py_script(
        tmp_path / "pre.py",
        f"open(r'{marker}', 'w').write('ok')",
    )
    check_script = _write_py_script(
        tmp_path / "check.py",
        f"import sys; sys.exit(0 if open(r'{marker}').read() == 'ok' else 1)",
    )
    profile = StackProfile(
        package_manager="custom",
        pre_install_command=f"{sys.executable} {pre_script.as_posix()}",
        install_command=f"{sys.executable} {check_script.as_posix()}",
    )
    result = prepare_sut(profile, cwd=tmp_path, timeout_s=30)
    assert result.ran is True
    assert result.exit_code == 0, f"stderr={result.stderr!r}"
    assert marker.read_text() == "ok"


# ---------------------------------------------------------------------------
# _strip_headless_flag + run_tests headless routing
# ---------------------------------------------------------------------------


def test_strip_headless_bare_flag():
    from worca_t.test_runner import _strip_headless_flag
    assert _strip_headless_flag("pytest -m smoke --headless --ci") == "pytest -m smoke --ci"


def test_strip_headless_with_equals_value():
    from worca_t.test_runner import _strip_headless_flag
    assert _strip_headless_flag("pytest --headless=true -k foo") == "pytest -k foo"
    assert _strip_headless_flag("pytest --headless=0 -k foo") == "pytest -k foo"


def test_strip_headless_with_space_value():
    from worca_t.test_runner import _strip_headless_flag
    assert _strip_headless_flag("pytest --headless true -k foo") == "pytest -k foo"


def test_strip_headless_at_end_of_command():
    from worca_t.test_runner import _strip_headless_flag
    assert _strip_headless_flag("pytest --ci --headless") == "pytest --ci"


def test_strip_headless_no_op_when_absent():
    from worca_t.test_runner import _strip_headless_flag
    cmd = "pytest -m smoke --ci --junitxml=x.xml"
    assert _strip_headless_flag(cmd) == cmd


def test_strip_headless_does_not_match_substring():
    """Defensive: must not strip `--headless-mode`, `--headless-foo`, etc."""
    from worca_t.test_runner import _strip_headless_flag
    cmd = "pytest --headless-mode --ci"
    assert _strip_headless_flag(cmd) == cmd


def test_run_tests_headless_default_sets_env_and_keeps_flag(tmp_path: Path, monkeypatch):
    """`headless=True` (default) sets HEADLESS=1 and leaves `--headless` alone."""
    from worca_t.test_runner import run_tests

    seen_env: dict[str, str] = {}
    seen_cmd: str = ""

    def fake_execute(command, *, cwd, timeout_s, env_extra=None, **_kw):
        nonlocal seen_cmd
        seen_cmd = command
        if env_extra:
            seen_env.update(env_extra)
        return 0, "", "", 0.1

    monkeypatch.setattr("worca_t.test_runner.execute_command", fake_execute)
    run_tests("pytest", cwd=tmp_path,
              detected_command="pytest -m smoke --headless")
    assert seen_env.get("HEADLESS") == "1"
    assert "--headless" in seen_cmd


def test_run_tests_headed_sets_env_zero_and_strips_flag(tmp_path: Path, monkeypatch):
    """`headless=False` sets HEADLESS=0 AND strips `--headless` from the cmd."""
    from worca_t.test_runner import run_tests

    seen_env: dict[str, str] = {}
    seen_cmd: str = ""

    def fake_execute(command, *, cwd, timeout_s, env_extra=None, **_kw):
        nonlocal seen_cmd
        seen_cmd = command
        if env_extra:
            seen_env.update(env_extra)
        return 0, "", "", 0.1

    monkeypatch.setattr("worca_t.test_runner.execute_command", fake_execute)
    run_tests("pytest", cwd=tmp_path,
              detected_command="pytest -m smoke --headless",
              headless=False)
    assert seen_env.get("HEADLESS") == "0"
    assert "--headless" not in seen_cmd
    # The rest of the command must survive intact.
    assert "pytest" in seen_cmd
    assert "-m smoke" in seen_cmd


def test_run_tests_headed_preserves_caller_env_extra(tmp_path: Path, monkeypatch):
    """The caller's `env_extra` must not be mutated; HEADLESS gets added on top."""
    from worca_t.test_runner import run_tests

    seen_env: dict[str, str] = {}

    def fake_execute(command, *, cwd, timeout_s, env_extra=None, **_kw):
        if env_extra:
            seen_env.update(env_extra)
        return 0, "", "", 0.1

    monkeypatch.setattr("worca_t.test_runner.execute_command", fake_execute)
    caller_env = {"WORCA_T_TESTS_DIR": "/some/path"}
    run_tests("pytest", cwd=tmp_path, detected_command="pytest",
              env_extra=caller_env, headless=False)
    assert seen_env.get("WORCA_T_TESTS_DIR") == "/some/path"
    assert seen_env.get("HEADLESS") == "0"
    # Caller's dict is not mutated.
    assert "HEADLESS" not in caller_env


def test_prepare_sut_captures_failure(tmp_path: Path) -> None:
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    script = _write_py_script(tmp_path / "fail.py", "import sys; sys.exit(7)")
    profile = StackProfile(
        package_manager="custom",
        install_command=f"{sys.executable} {script.as_posix()}",
    )
    result = prepare_sut(profile, cwd=tmp_path, timeout_s=30)
    assert result.ran is True
    assert result.exit_code == 7
    assert result.ok() is False


def test_prepare_sut_pre_install_failure_aborts(tmp_path: Path) -> None:
    """If pre_install_command fails, install_command must not run."""
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    marker = tmp_path / "should_not_exist.txt"
    pre_script = _write_py_script(
        tmp_path / "pre_fail.py", "import sys; sys.exit(3)",
    )
    install_script = _write_py_script(
        tmp_path / "install.py",
        f"open(r'{marker}', 'w').write('leaked')",
    )
    profile = StackProfile(
        package_manager="custom",
        pre_install_command=f"{sys.executable} {pre_script.as_posix()}",
        install_command=f"{sys.executable} {install_script.as_posix()}",
    )
    result = prepare_sut(profile, cwd=tmp_path, timeout_s=30)
    assert result.ran is True
    assert result.exit_code == 3
    assert not marker.exists(), "install_command ran despite pre_install failure"


def test_prepare_sut_does_not_leak_secrets(tmp_path: Path, monkeypatch) -> None:
    from worca_t.config import SECRET_ENV_KEYS
    from worca_t.stack_profile import StackProfile
    from worca_t.test_runner import prepare_sut

    for key in SECRET_ENV_KEYS:
        monkeypatch.setenv(key, f"FAKE_{key}")
    script = _write_py_script(
        tmp_path / "check_env.py",
        "import os, sys; sys.exit(1 if 'ANTHROPIC_API_KEY' in os.environ else 0)",
    )
    profile = StackProfile(
        package_manager="custom",
        install_command=f"{sys.executable} {script.as_posix()}",
    )
    result = prepare_sut(profile, cwd=tmp_path, timeout_s=10)
    assert result.exit_code == 0, "SECRET_ENV_KEYS leaked to install subprocess"


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
# Security: SECRET_ENV_KEYS must not leak to child processes
# ---------------------------------------------------------------------------


def test_execute_command_does_not_leak_secrets(tmp_path: Path, monkeypatch) -> None:
    from worca_t.config import SECRET_ENV_KEYS

    for key in SECRET_ENV_KEYS:
        monkeypatch.setenv(key, f"FAKE_{key}")
    script = _write_py_script(
        tmp_path / "dump_env.py",
        "import os, json, sys; json.dump(dict(os.environ), sys.stdout)",
    )
    cmd = f"{sys.executable} {script.as_posix()}"
    code, stdout, _stderr, _dur = execute_command(cmd, cwd=tmp_path, timeout_s=10)
    assert code == 0
    child_env = json.loads(stdout)
    for key in SECRET_ENV_KEYS:
        assert key not in child_env, f"{key} leaked to child process"


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


# ---------------------------------------------------------------------------
# classify_runner_failure — detects collection/import failures in stderr so
# step 9 can skip the self-heal loop on the synthetic T-runner-failure entry
# ---------------------------------------------------------------------------


_ALLURE_MISSING_STDERR = (
    "ImportError while loading conftest 'C:\\sut\\tests\\conftest.py'.\n"
    "tests\\conftest.py:3: in <module>\n"
    "    import allure\n"
    "E   ModuleNotFoundError: No module named 'allure'\n"
)


def test_classify_runner_failure_detects_missing_module_with_poetry_hint() -> None:
    rf = classify_runner_failure(_ALLURE_MISSING_STDERR, package_manager="poetry")
    assert rf is not None
    assert rf["kind"] == "missing_module"
    assert rf["module"] == "allure"
    # Poetry hint + allure-pytest provider mapping
    assert rf["hint"] == "poetry add --group test allure-pytest"
    assert "allure" in rf["summary"]


def test_classify_runner_failure_uses_npm_hint_for_node_pm() -> None:
    stderr = (
        "Test suite failed to run\n"
        "E   ModuleNotFoundError: No module named 'pytest_xdist'\n"
    )
    rf = classify_runner_failure(stderr, package_manager="npm")
    assert rf is not None
    # Plugin-name → install-name mapping still applies; package manager
    # template just picks the npm command shape.
    assert rf["hint"] == "npm install --save-dev pytest-xdist"


def test_classify_runner_failure_unknown_pm_falls_back_generically() -> None:
    rf = classify_runner_failure(_ALLURE_MISSING_STDERR, package_manager=None)
    assert rf is not None
    assert rf["module"] == "allure"
    # No PM → generic "install ..." template
    assert rf["hint"] == "install allure-pytest"


def test_classify_runner_failure_unknown_module_uses_module_name_verbatim() -> None:
    stderr = "E   ModuleNotFoundError: No module named 'somecorporatemodule'\n"
    rf = classify_runner_failure(stderr, package_manager="poetry")
    assert rf is not None
    assert rf["module"] == "somecorporatemodule"
    assert rf["hint"] == "poetry add --group test somecorporatemodule"


def test_classify_runner_failure_collection_error_without_specific_module() -> None:
    # Broken conftest with a SyntaxError / fixture resolution failure — no
    # ModuleNotFoundError, but the "ImportError while loading conftest"
    # signature still tells us this is a collection failure.
    stderr = (
        "ImportError while loading conftest 'tests/conftest.py'.\n"
        "tests/conftest.py:42: in <module>\n"
        "    raise RuntimeError('fixture init failed')\n"
        "E   RuntimeError: fixture init failed\n"
    )
    rf = classify_runner_failure(stderr, package_manager="poetry")
    assert rf is not None
    assert rf["kind"] == "collection_error"
    assert rf["module"] is None
    assert "conftest" in rf["hint"] or "collection" in rf["hint"]


def test_classify_runner_failure_returns_none_for_normal_test_failures() -> None:
    # A real test assertion failure must NOT trigger the classifier — step 9
    # should still self-heal in that case.
    stderr = (
        "FAILED tests/test_login.py::test_login - AssertionError: 1 != 2\n"
        "1 failed in 0.42s\n"
    )
    assert classify_runner_failure(stderr, package_manager="poetry") is None


def test_classify_runner_failure_returns_none_for_empty_stderr() -> None:
    assert classify_runner_failure("", package_manager="poetry") is None
    assert classify_runner_failure(None, package_manager="poetry") is None  # type: ignore[arg-type]


def test_run_tests_attaches_runner_failure_to_synthetic_entry(tmp_path: Path) -> None:
    # Drive a real subprocess that exits non-zero with a ModuleNotFoundError
    # on stderr; verify run_tests synthesises the T-runner-failure entry
    # and attaches the classifier dict.
    script = _write_py_script(
        tmp_path / "boom.py",
        "import sys\n"
        "sys.stderr.write(\"E   ModuleNotFoundError: No module named 'allure'\\n\")\n"
        "sys.exit(2)\n",
    )
    cmd = f'{sys.executable} {script.as_posix()}'
    result = run_tests("pytest", cwd=tmp_path, detected_command=cmd, timeout_s=10)
    assert len(result.results) == 1
    entry = result.results[0]
    assert entry.id == "T-runner-failure"
    assert entry.runner_failure is not None
    assert entry.runner_failure["kind"] == "missing_module"
    assert entry.runner_failure["module"] == "allure"
    # Message echoes the classifier summary + hint so the operator sees
    # the actionable fix on the run-results.json one-liner.
    assert "allure-pytest" in (entry.message or "")


# ---------------------------------------------------------------------------
# install_command_for
# ---------------------------------------------------------------------------


def test_install_command_for_poetry_returns_argv_list():
    cmd = install_command_for("poetry", "allure-pytest")
    assert cmd == ["poetry", "add", "--group", "test", "allure-pytest"]


def test_install_command_for_pip_requires_venv_bin():
    """Bare `pip` is unsafe: it would target whatever pip is first on PATH
    (worca-t's venv when VIRTUAL_ENV leaks, system Python otherwise) —
    neither matches the venv the test runner actually uses. Without
    `venv_bin`, the call must return None so the caller falls back to the
    prose hint instead of silently mis-installing."""
    assert install_command_for("pip", "requests") is None
    assert install_command_for("pip", "requests", venv_bin=".venv/bin") == [
        ".venv/bin/pip", "install", "requests",
    ]


def test_install_command_for_pip_windows_venv_bin():
    """Windows venvs put binaries in .venv/Scripts. Caller passes the
    platform-specific path verbatim; install_command_for doesn't try to
    second-guess it."""
    assert install_command_for("pip", "pytest", venv_bin=".venv/Scripts") == [
        ".venv/Scripts/pip", "install", "pytest",
    ]


def test_install_command_for_uv_pdm_pipenv_argv():
    """Sanity-check the rest of the Python manager argv set so we catch
    accidental table edits."""
    assert install_command_for("uv", "x") == ["uv", "add", "--dev", "x"]
    assert install_command_for("pdm", "x") == ["pdm", "add", "--dev", "x"]
    assert install_command_for("pipenv", "x") == ["pipenv", "install", "--dev", "x"]


def test_install_command_for_node_managers_argv():
    """npm/yarn/pnpm install into local node_modules — no venv_bin needed."""
    assert install_command_for("npm", "x") == ["npm", "install", "--save-dev", "x"]
    assert install_command_for("yarn", "x") == ["yarn", "add", "--dev", "x"]
    assert install_command_for("pnpm", "x") == ["pnpm", "add", "--save-dev", "x"]


def test_install_command_for_unknown_manager_returns_none():
    # maven/gradle/hatch can't be reduced to a safe one-shot argv — caller
    # falls back to the prose hint.
    assert install_command_for("maven", "junit") is None
    assert install_command_for("gradle", "junit") is None
    assert install_command_for("hatch", "pytest") is None
    assert install_command_for(None, "x") is None
    assert install_command_for("", "x") is None


# ---------------------------------------------------------------------------
# audit_missing_deps
# ---------------------------------------------------------------------------


def _write_sut(root: Path, *, pyproject: str, conftest: str = "", test_a: str = "") -> Path:
    """Build a minimal SUT layout (pyproject + tests/conftest.py + tests/test_a.py)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(textwrap.dedent(pyproject), encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    if conftest:
        (tests / "conftest.py").write_text(textwrap.dedent(conftest), encoding="utf-8")
    if test_a:
        (tests / "test_a.py").write_text(textwrap.dedent(test_a), encoding="utf-8")
    return root


def test_audit_detects_allure_gap_as_known_confidence(tmp_path: Path):
    sut = _write_sut(
        tmp_path / "sut",
        pyproject="""
            [tool.poetry]
            name = "demo"
            [tool.poetry.dependencies]
            python = "^3.11"
            pytest = "^8"
        """,
        conftest="import allure\nimport pytest\n",
    )

    warnings = audit_missing_deps(sut, package_manager="poetry")
    by_module = {w["module"]: w for w in warnings}
    assert "allure" in by_module
    w = by_module["allure"]
    assert w["suggested_package"] == "allure-pytest"
    assert w["confidence"] == "known"
    assert "poetry add" in w["suggested_install"]
    assert w["source_file"].endswith("tests/conftest.py")
    # Already declared — never surface.
    assert "pytest" not in by_module


def test_audit_unmapped_module_is_guessed_not_known(tmp_path: Path):
    sut = _write_sut(
        tmp_path / "sut",
        pyproject="""
            [project]
            name = "demo"
            requires-python = ">=3.11"
            dependencies = []
        """,
        test_a="import some_random_module_not_in_table\n",
    )

    warnings = audit_missing_deps(sut, package_manager="poetry")
    by_module = {w["module"]: w for w in warnings}
    w = by_module["some_random_module_not_in_table"]
    assert w["confidence"] == "guessed"
    assert w["suggested_package"] == "some_random_module_not_in_table"


def test_audit_ignores_stdlib_relative_and_own_package(tmp_path: Path):
    sut = _write_sut(
        tmp_path / "sut",
        pyproject="""
            [project]
            name = "my-cool-pkg"
            dependencies = []
        """,
        conftest=(
            "import os\n"            # stdlib — skip
            "import sys\n"           # stdlib — skip
            "from . import helpers\n"  # relative — skip
            "from my_cool_pkg import thing\n"  # own package — skip
            "from src.helpers import x\n"      # src.* layout — skip
        ),
    )
    assert audit_missing_deps(sut, package_manager="poetry") == []


def test_audit_recognizes_underscore_vs_hyphen_in_declared_deps(tmp_path: Path):
    # `import pytest_asyncio` should match declared `pytest-asyncio`
    # via PEP 503 normalization.
    sut = _write_sut(
        tmp_path / "sut",
        pyproject="""
            [tool.poetry]
            name = "demo"
            [tool.poetry.group.test.dependencies]
            pytest-asyncio = "^0.23"
        """,
        conftest="import pytest_asyncio\n",
    )
    assert audit_missing_deps(sut, package_manager="poetry") == []


def test_audit_returns_empty_when_no_tests_dir(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    (sut / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=[]\n")
    assert audit_missing_deps(sut, package_manager="poetry") == []
