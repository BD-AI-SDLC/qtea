"""Step 9 execute-and-self-heal tests."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s09_execute import (
    ExecuteStep,
    _apply_fixer_outputs,
    _build_bug_candidates,
    _build_fixer_prompt,
    _filter_command_for_tests,
)
from worca_t.test_runner import TestRunEntry
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_filter_command_for_tests_pytest_adds_k_expr():
    cmd = _filter_command_for_tests("pytest tests/", ["T-login-1", "T-logout-2"])
    assert cmd.startswith("pytest tests/ -k ")
    assert "1" in cmd or "login" in cmd


def test_filter_command_for_tests_other_frameworks_unchanged():
    cmd = _filter_command_for_tests("npx playwright test", ["T-a"])
    assert cmd == "npx playwright test"


def test_filter_command_for_tests_empty_returns_unchanged():
    assert _filter_command_for_tests("anything", []) == "anything"


def test_build_fixer_prompt_includes_required_fields():
    entry = TestRunEntry(
        id="T-x", name="logs in", file="tests/login.spec.ts", status="failed",
        message="locator missing", traceback="long\ntb",
    )
    prompt = _build_fixer_prompt(entry, Path("/sut/worca-tests"))
    assert "T-x" in prompt
    assert "tests/login.spec.ts" in prompt
    assert "locator missing" in prompt
    assert "NEVER XPath" in prompt
    assert "long\ntb" in prompt


def test_build_bug_candidates_shape():
    entries = [
        TestRunEntry(id="T-1", name="a", file="t.py", status="failed", message="m"),
        TestRunEntry(id="T-2", name="b", file="t.py", status="error"),
    ]
    payload = _build_bug_candidates(entries)
    assert len(payload["candidates"]) == 2
    c = payload["candidates"][0]
    assert c["id"] == "BC-T-1"
    assert c["test_id"] == "T-1"
    assert c["first_seen"]
    assert isinstance(c["attachments"], list)


def test_apply_fixer_outputs_copies_file(tmp_path: Path):
    sut_tests = tmp_path / "sut" / "worca-tests"
    sut_tests.mkdir(parents=True)
    (sut_tests / "login.spec.ts").write_text("OLD\n", encoding="utf-8")
    wd = tmp_path / "heal"
    wd.mkdir()
    (wd / "login.spec.ts").write_text("NEW\n", encoding="utf-8")

    applied = _apply_fixer_outputs(wd, sut_tests, "login.spec.ts")
    assert applied is True
    assert (sut_tests / "login.spec.ts").read_text(encoding="utf-8") == "NEW\n"


def test_apply_fixer_outputs_handles_basename_fallback(tmp_path: Path):
    sut_tests = tmp_path / "sut" / "worca-tests"
    sut_tests.mkdir(parents=True)
    (sut_tests / "login.spec.ts").write_text("OLD\n", encoding="utf-8")
    wd = tmp_path / "heal"
    nested = wd / "nested" / "dir"
    nested.mkdir(parents=True)
    (nested / "login.spec.ts").write_text("DEEP\n", encoding="utf-8")

    applied = _apply_fixer_outputs(wd, sut_tests, "login.spec.ts")
    assert applied is True
    assert (sut_tests / "login.spec.ts").read_text(encoding="utf-8") == "DEEP\n"


def test_apply_fixer_outputs_no_match_returns_false(tmp_path: Path):
    sut_tests = tmp_path / "tests"
    sut_tests.mkdir()
    wd = tmp_path / "heal"
    wd.mkdir()
    assert _apply_fixer_outputs(wd, sut_tests, "missing.spec.ts") is False


# ---------------------------------------------------------------------------
# Integration: step 9 end-to-end with fake pytest + fake fixer agent
# ---------------------------------------------------------------------------


_JUNIT_PASS = """<testsuites><testsuite name="s" file="tests/a.py">
  <testcase name="test_ok" file="tests/a.py" time="0.01"/>
</testsuite></testsuites>"""

_JUNIT_FAIL = """<testsuites><testsuite name="s" file="tests/a.py">
  <testcase name="test_ok" file="tests/a.py" time="0.01"/>
  <testcase name="test_bad" file="tests/a.py" time="0.02">
    <failure message="boom">tb</failure>
  </testcase>
</testsuite></testsuites>"""


def _make_fake_pytest(
    script_path: Path,
    *,
    junit_xml: str,
    exit_code: int,
) -> str:
    """Write a python script that pretends to be pytest: writes worca-junit.xml
    into its CWD then exits with the given code. Returns a command string."""
    body = (
        "import sys, os\n"
        f"open(os.path.join(os.getcwd(), 'worca-junit.xml'), 'w', encoding='utf-8').write({junit_xml!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script_path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script_path.as_posix()}"


def _make_flaky_pytest(script_path: Path, marker_path: Path) -> str:
    """Pytest that fails on first invocation, passes on subsequent ones,
    using a marker file to remember state."""
    body = (
        "import sys, os\n"
        f"marker = r'{marker_path.as_posix()}'\n"
        "first = not os.path.exists(marker)\n"
        "if first:\n"
        "    open(marker, 'w').write('1')\n"
        f"    junit = {_JUNIT_FAIL!r}\n"
        "    code = 1\n"
        "else:\n"
        f"    junit = {_JUNIT_PASS!r}\n"
        "    code = 0\n"
        "open(os.path.join(os.getcwd(), 'worca-junit.xml'), 'w', encoding='utf-8').write(junit)\n"
        "sys.exit(code)\n"
    )
    script_path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script_path.as_posix()}"


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _seed_minimal_inputs(ctx: StepContext, *, command: str, framework: str = "pytest") -> None:
    # Step 8 tests dir (one trivial test file)
    s8_tests = ctx.workspace.step_dir(8) / "tests"
    s8_tests.mkdir(parents=True, exist_ok=True)
    (s8_tests / "a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # Step 8 index
    (ctx.workspace.step_dir(8) / "tests-with-tbd.json").write_text(
        json.dumps({"framework": framework, "tests": []}), encoding="utf-8"
    )
    # Step 6 research with detected command
    (ctx.workspace.step_dir(6) / "research.json").write_text(
        json.dumps({
            "detected_stack": framework,
            "commands": {"test": command},
        }),
        encoding="utf-8",
    )
    # SUT root must exist.
    ctx.workspace.sut.mkdir(parents=True, exist_ok=True)


def test_step09_requires_step78_outputs(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = ExecuteStep().run(ctx)
    assert not result.success
    assert "patched tests" in (result.error or "")


def test_step09_requires_sut(tmp_path: Path):
    ctx = _ctx(tmp_path)
    # Provide step-8 outputs but remove SUT.
    s8_tests = ctx.workspace.step_dir(8) / "tests"
    s8_tests.mkdir(parents=True)
    (s8_tests / "a.py").write_text("pass\n", encoding="utf-8")
    shutil.rmtree(ctx.workspace.sut)
    result = ExecuteStep().run(ctx)
    assert not result.success
    assert "SUT" in (result.error or "")


def test_step09_all_pass_completes(tmp_path: Path):
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_PASS, exit_code=0)
    _seed_minimal_inputs(ctx, command=cmd)

    result = ExecuteStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["totals"]["passed"] == 1
    assert payload["totals"]["failed"] == 0
    bugs = json.loads((ctx.workspace.step_dir(9) / "bug-candidates.json").read_text(encoding="utf-8"))
    assert bugs["candidates"] == []
    # SUT got the mirrored tests dir.
    assert (ctx.workspace.sut / "worca-tests" / "a.py").exists()


def test_step09_failures_without_heal_yield_warned_and_bugs(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Fake claude that returns no usable patch (no files).
    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "no fix"}],
        files={},
    )
    install_on_path(monkeypatch, bin_path)

    result = ExecuteStep().run(ctx)
    assert result.success
    assert result.status == "warned"
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["totals"]["failed"] == 1
    assert payload["self_heal"]["attempts"] == 1
    assert payload["self_heal"]["patches_applied"] == 0
    bugs = json.loads((ctx.workspace.step_dir(9) / "bug-candidates.json").read_text(encoding="utf-8"))
    assert len(bugs["candidates"]) == 1
    assert bugs["candidates"][0]["status"] == "failed"
    # Heal log captured the attempt.
    heal_log = (ctx.workspace.step_dir(9) / "self-heal" / "heal-log.jsonl").read_text(encoding="utf-8")
    assert "T-" in heal_log


def test_step09_self_heal_repairs_and_passes(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    marker = tmp_path / "marker.txt"
    cmd = _make_flaky_pytest(tmp_path / "pt.py", marker)
    _seed_minimal_inputs(ctx, command=cmd)

    # Fake fixer that DOES write a replacement file - this triggers the rerun.
    # Use distinct content so the patch detector sees an actual change.
    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "patched"}],
        files={"a.py": "def test_ok():  # patched\n    assert True\n"},
    )
    install_on_path(monkeypatch, bin_path)

    result = ExecuteStep().run(ctx)
    assert result.success, result.error
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["self_heal"]["attempts"] == 2
    assert payload["self_heal"]["patches_applied"] >= 1
    # After re-run with the marker present, the flaky pytest reports only the
    # passing junit; the merged result should have no failures.
    assert payload["totals"]["failed"] == 0
    assert result.status == "completed"
