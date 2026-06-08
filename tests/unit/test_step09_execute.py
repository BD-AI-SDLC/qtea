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
    _count_xpath_markers,
    _filter_command_for_tests,
    _patch_introduces_xpath,
    _run_dep_install,
)
from worca_t.test_runner import TestRunEntry
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query
from ._sut_setup import seed_sut

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


def test_run_dep_install_poetry_noop_treated_as_failure(tmp_path, monkeypatch):
    """`poetry add <pkg>` returns exit 0 even when <pkg> is already declared
    in pyproject.toml — the stdout reads 'already present ... Nothing to add'
    and nothing gets installed. We must catch that and report failure so the
    caller (Step 9 runtime dep-recovery) doesn't claim success and re-run the
    same broken test suite expecting a different outcome."""
    import subprocess as sp

    poetry_noop_stdout = (
        "The following packages are already present in the pyproject.toml "
        "and will be skipped:\n\n  - pydantic_settings\n\nNothing to add.\n"
    )

    def fake_run(argv, **kwargs):
        return sp.CompletedProcess(argv, returncode=0, stdout=poetry_noop_stdout, stderr="")

    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
    install_log = tmp_path / "install.log"
    ok, summary = _run_dep_install("poetry", "pydantic_settings", tmp_path, install_log)
    assert ok is False
    assert "no-op" in summary
    assert "already declared" in summary
    # And the log still captured the full transcript for forensics.
    log_text = install_log.read_text(encoding="utf-8")
    assert "Nothing to add" in log_text
    assert "exit_code: 0" in log_text


def test_run_dep_install_poetry_real_install_succeeds(tmp_path, monkeypatch):
    """Sanity-check: when poetry actually installs (non-no-op stdout), we
    still return success. Guards against the no-op detection over-firing on
    legitimate poetry output."""
    import subprocess as sp

    def fake_run(argv, **kwargs):
        return sp.CompletedProcess(
            argv,
            returncode=0,
            stdout="Resolving dependencies...\n\nPackage operations: 1 install, 0 updates, 0 removals\n",
            stderr="",
        )

    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
    ok, summary = _run_dep_install(
        "poetry", "pytest-asyncio", tmp_path, tmp_path / "install.log"
    )
    assert ok is True
    assert "poetry add --group test pytest-asyncio" in summary


def test_run_dep_install_passes_isolate_venv_for_poetry(tmp_path, monkeypatch):
    """The subprocess env handed to poetry must NOT inherit VIRTUAL_ENV —
    otherwise poetry reuses worca-t's parent venv as the SUT's venv (the
    original bug that motivated `isolate_venv`)."""
    import subprocess as sp

    captured_env = {}

    def fake_run(argv, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

    monkeypatch.setenv("VIRTUAL_ENV", "/worca-t/.venv")
    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
    _run_dep_install("poetry", "pkg", tmp_path, tmp_path / "log")
    assert "VIRTUAL_ENV" not in captured_env


def test_run_dep_install_pip_uses_venv_pip_from_profile(tmp_path, monkeypatch):
    """pip auto-install must target the SUT's own .venv (via venv_bin from
    the profile), NOT bare `pip` from PATH. Without the path prefix the
    install would land in worca-t's parent venv when VIRTUAL_ENV leaks
    (defeating the install) or in the system Python when it doesn't
    (polluting the host)."""
    import subprocess as sp

    from worca_t.stack_profile import StackProfile

    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
    profile = StackProfile(
        language="python", package_manager="pip", wrapper_prefix=".venv/bin",
    )
    ok, summary = _run_dep_install(
        "pip", "requests", tmp_path, tmp_path / "log", profile=profile,
    )
    assert ok is True
    assert captured_argv[:3] == [".venv/bin/pip", "install", "requests"]


def test_run_dep_install_pip_without_profile_refuses(tmp_path, monkeypatch):
    """When no profile is available for a pip SUT we can't safely build a
    venv-targeted install command — surface a clear refusal instead of
    silently invoking bare `pip`."""
    import subprocess as sp

    def fake_run(argv, **kwargs):
        return sp.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
    ok, summary = _run_dep_install("pip", "requests", tmp_path, tmp_path / "log")
    assert ok is False
    assert "pip auto-install requires" in summary


def test_run_dep_install_isolates_for_all_python_venv_managers(tmp_path, monkeypatch):
    """uv / pdm / pipenv exhibit the same VIRTUAL_ENV inheritance issue as
    poetry — all four must strip the parent venv from the subprocess env."""
    import subprocess as sp

    monkeypatch.setenv("VIRTUAL_ENV", "/worca-t/.venv")
    monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", lambda argv, **kw:
        (kw.setdefault("_seen", kw.get("env", {})),  # noqa: ARG005
         sp.CompletedProcess(argv, 0, "installed", ""))[1])

    for pm in ("poetry", "uv", "pdm", "pipenv"):
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

        monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
        _run_dep_install(pm, "x", tmp_path, tmp_path / f"log-{pm}")
        assert "VIRTUAL_ENV" not in captured, f"{pm} did not strip VIRTUAL_ENV"


def test_run_dep_install_keeps_virtualenv_for_node_managers(tmp_path, monkeypatch):
    """Node managers (npm/yarn/pnpm) install into local node_modules and
    don't read VIRTUAL_ENV — isolation is a no-op for them; we don't strip
    so the child env stays a faithful copy of the parent (PATH unchanged,
    proxy / CA-bundle env vars preserved)."""
    import subprocess as sp

    monkeypatch.setenv("VIRTUAL_ENV", "/some/venv")
    for pm in ("npm", "yarn", "pnpm"):
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            return sp.CompletedProcess(argv, returncode=0, stdout="added 1 package", stderr="")

        monkeypatch.setattr("worca_t.steps.s09_execute.subprocess.run", fake_run)
        ok, _ = _run_dep_install(pm, "lodash", tmp_path, tmp_path / f"log-{pm}")
        assert ok is True
        assert captured.get("VIRTUAL_ENV") == "/some/venv", (
            f"{pm} unexpectedly stripped VIRTUAL_ENV"
        )


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


def _ctx(tmp_path: Path, *, seed_sut_repo: bool = True) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if seed_sut_repo:
        # Step 9 now requires `<workspace>/sut/` to be a git repo on the
        # worca-t branch — pipeline.py + _materialize_sut do this in
        # production. seed_sut() mirrors that end-state for tests.
        seed_sut(ws)
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _seed_minimal_inputs(ctx: StepContext, *, command: str, framework: str = "pytest") -> None:
    # Step 7 manifest (the new contract; replaces the old tests/ mirror).
    step7 = ctx.workspace.step_dir(7)
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "generated-files.json").write_text(
        json.dumps({"sut_root": str(ctx.workspace.sut), "files": ["tests/a.py"]}),
        encoding="utf-8",
    )
    # Step 8 index (optional fallback; step 7 manifest is required).
    (ctx.workspace.step_dir(8) / "tbd-index.json").write_text(
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
    # The test file lives in the SUT (where step 7/8 would have put it).
    sut_tests = ctx.workspace.sut / "tests"
    sut_tests.mkdir(parents=True, exist_ok=True)
    (sut_tests / "a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")


async def test_step09_requires_step78_outputs(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await ExecuteStep().run(ctx)
    assert not result.success
    # New error message references the step-7 manifest contract.
    assert "step 7" in (result.error or "").lower() or "generated-files" in (result.error or "")


async def test_step09_requires_sut(tmp_path: Path):
    # Build a workspace WITHOUT the SUT git seed, then drop the SUT dir.
    ctx = _ctx(tmp_path, seed_sut_repo=False)
    step7 = ctx.workspace.step_dir(7)
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "generated-files.json").write_text(
        json.dumps({"sut_root": str(ctx.workspace.sut), "files": []}),
        encoding="utf-8",
    )
    if ctx.workspace.sut.exists():
        shutil.rmtree(ctx.workspace.sut)
    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert "SUT" in (result.error or "")


async def test_step09_all_pass_completes(tmp_path: Path):
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_PASS, exit_code=0)
    _seed_minimal_inputs(ctx, command=cmd)

    result = await ExecuteStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["totals"]["passed"] == 1
    assert payload["totals"]["failed"] == 0
    bugs = json.loads((ctx.workspace.step_dir(9) / "bug-candidates.json").read_text(encoding="utf-8"))
    assert bugs["candidates"] == []
    # Test file lives in the SUT — already there from _seed_minimal_inputs,
    # no mirror step needed.
    assert (ctx.workspace.sut / "tests" / "a.py").exists()


async def test_step09_failures_without_heal_yield_warned_and_bugs(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Fake claude that returns no usable patch (no files).
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "no fix"}],
        files={},
    )

    result = await ExecuteStep().run(ctx)
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


async def test_step09_self_heal_repairs_and_passes(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    marker = tmp_path / "marker.txt"
    cmd = _make_flaky_pytest(tmp_path / "pt.py", marker)
    _seed_minimal_inputs(ctx, command=cmd)

    # Fake fixer that DOES write a replacement file - this triggers the rerun.
    # Use distinct content so the patch detector sees an actual change.
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "patched"}],
        files={"a.py": "def test_ok():  # patched\n    assert True\n"},
    )

    result = await ExecuteStep().run(ctx)
    assert result.success, result.error
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["self_heal"]["attempts"] == 2
    assert payload["self_heal"]["patches_applied"] >= 1
    # After re-run with the marker present, the flaky pytest reports only the
    # passing junit; the merged result should have no failures.
    assert payload["totals"]["failed"] == 0
    assert result.status == "completed"


async def test_step09_fails_when_only_runner_failure_results(tmp_path: Path):
    """Regression: when pytest aborts in conftest (missing dep, syntax error,
    exit code 4) it produces no junit XML and the test_runner emits a single
    synthesised `T-runner-failure` entry. Previously that yielded `warned`
    status and Step 10/11 ran on garbage. The new contract: this is an
    environment failure → step `failed` → pipeline halts.
    """
    ctx = _ctx(tmp_path)
    # Fake "pytest" that exits 4 WITHOUT writing junit (mimics conftest abort).
    # Stderr doesn't match the classifier patterns (no `No module named 'X'`)
    # so the unclassified error path is exercised here.
    script = tmp_path / "broken_pytest.py"
    script.write_text(
        "import sys\nsys.stderr.write('ModuleNotFoundError: allure\\n')\n"
        "sys.exit(4)\n",
        encoding="utf-8",
    )
    cmd = f"{sys.executable} {script.as_posix()}"
    _seed_minimal_inputs(ctx, command=cmd)

    result = await ExecuteStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    assert "test runner produced no parseable test results" in (result.error or "")
    # Artifacts still written so downstream debug can inspect.
    payload = json.loads((ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8"))
    assert payload["results"][0]["id"] == "T-runner-failure"


async def test_step09_missing_module_surfaces_install_hint_and_skips_heal(
    tmp_path: Path,
):
    """When the runner failure matches the missing-module classifier, the
    final error message must surface the install hint, the heal loop must
    NOT run (no per-test patch site to fix), and the heal-log must record
    the skip with a clear reason instead of going silent.
    """
    ctx = _ctx(tmp_path)
    # Fake pytest that exits 4 with a real ModuleNotFoundError-shaped
    # stderr — the classifier will pick this up.
    script = tmp_path / "broken_pytest.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write(\"ImportError while loading conftest 'tests/conftest.py'.\\n\")\n"
        "sys.stderr.write(\"E   ModuleNotFoundError: No module named 'allure'\\n\")\n"
        "sys.exit(4)\n",
        encoding="utf-8",
    )
    cmd = f"{sys.executable} {script.as_posix()}"
    _seed_minimal_inputs(ctx, command=cmd)

    result = await ExecuteStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    # Actionable hint must appear in the error message.
    err = result.error or ""
    assert "allure" in err
    assert "install" in err.lower() or "poetry add" in err.lower() or "pip" in err.lower()
    # Heal-log records the skip; this is what prevents the audit trail
    # from going silent on a runner failure.
    heal_log = ctx.workspace.step_dir(9) / "self-heal" / "heal-log.jsonl"
    assert heal_log.exists()
    lines = [
        json.loads(ln)
        for ln in heal_log.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["test_id"] == "T-runner-failure"
    assert lines[0]["applied"] is False
    assert "skipped" in lines[0]["agent_error"].lower()
    # Verify the classifier dict made it into run-results.json so the
    # report (and any future report-side rendering) can use it.
    payload = json.loads(
        (ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8")
    )
    rf = payload["results"][0].get("runner_failure")
    assert rf is not None
    assert rf["kind"] == "missing_module"
    assert rf["module"] == "allure"


# ---------------------------------------------------------------------------
# Auto-install of missing test deps (Step 9 runtime recovery)
# ---------------------------------------------------------------------------


def _seed_with_stack_profile(
    ctx: StepContext,
    *,
    command: str,
    package_manager: str = "poetry",
    framework: str = "pytest",
    dep_warnings: list[dict] | None = None,
) -> None:
    """Like _seed_minimal_inputs but adds a stack_profile + (optional)
    dependency_warnings to research.json so the auto-install path engages."""
    step7 = ctx.workspace.step_dir(7)
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "generated-files.json").write_text(
        json.dumps({"sut_root": str(ctx.workspace.sut), "files": ["tests/a.py"]}),
        encoding="utf-8",
    )
    (ctx.workspace.step_dir(8) / "tbd-index.json").write_text(
        json.dumps({"framework": framework, "tests": []}), encoding="utf-8"
    )
    research = {
        "detected_stack": framework,
        "commands": {"test": command},
        "stack_profile": {
            "language": "python",
            "package_manager": package_manager,
        },
    }
    if dep_warnings is not None:
        research["dependency_warnings"] = dep_warnings
    (ctx.workspace.step_dir(6) / "research.json").write_text(
        json.dumps(research), encoding="utf-8"
    )
    # Also write the dedicated stack_profile.json that _load_stack_profile reads.
    (ctx.workspace.step_dir(6) / "stack_profile.json").write_text(
        json.dumps({
            "language": "python",
            "package_manager": package_manager,
            "install_command": None,
            "test_command": None,
        }),
        encoding="utf-8",
    )
    sut_tests = ctx.workspace.sut / "tests"
    sut_tests.mkdir(parents=True, exist_ok=True)
    (sut_tests / "a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")


def _make_dep_recovery_pytest(script_path: Path, marker_path: Path) -> str:
    """Fake pytest that on first call emits a `ModuleNotFoundError: allure`
    on stderr and exits 4 (no junit). On subsequent calls (after the
    auto-install path has written the marker), behaves like a clean pytest
    that writes a passing junit and exits 0."""
    body = (
        "import sys, os\n"
        f"marker = r'{marker_path.as_posix()}'\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').write('1')\n"
        "    sys.stderr.write(\"ImportError while loading conftest 'tests/conftest.py'.\\n\")\n"
        "    sys.stderr.write(\"E   ModuleNotFoundError: No module named 'allure'\\n\")\n"
        "    sys.exit(4)\n"
        f"open(os.path.join(os.getcwd(), 'worca-junit.xml'), 'w', encoding='utf-8').write({_JUNIT_PASS!r})\n"
        "sys.exit(0)\n"
    )
    script_path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script_path.as_posix()}"


async def test_step09_recovery_auto_installs_known_missing_dep(
    tmp_path: Path, monkeypatch
):
    """Default path: runner fails with missing_module on a name in the curated
    table → Step 9 runs the install (stubbed), commits, re-runs once, and the
    re-run passes. Step completes successfully."""
    ctx = _ctx(tmp_path)
    marker = tmp_path / "marker"
    cmd = _make_dep_recovery_pytest(tmp_path / "rec_pt.py", marker)
    _seed_with_stack_profile(ctx, command=cmd, package_manager="poetry")

    install_calls: list[tuple[str | None, str]] = []

    def fake_install(pm, pkg, sut_root, log_path, *, timeout_s=600, profile=None):
        install_calls.append((pm, pkg))
        # touch log so the file exists; flow does not require any content.
        log_path.write_text(log_path.read_text(encoding="utf-8") if log_path.exists() else "", encoding="utf-8")
        return True, f"fake: {pm} add {pkg}"

    monkeypatch.setattr("worca_t.steps.s09_execute._run_dep_install", fake_install)

    result = await ExecuteStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    # The install attempt happened with the right (pm, package) pair.
    assert ("poetry", "allure-pytest") in install_calls
    # And the re-run produced a real test result (not a runner failure).
    payload = json.loads(
        (ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8")
    )
    assert payload["totals"]["passed"] == 1
    assert payload["totals"]["errors"] == 0


async def test_step09_no_auto_deps_flag_disables_recovery(
    tmp_path: Path, monkeypatch
):
    """--no-auto-deps: even a known-safe missing dep must not auto-install."""
    ctx = _ctx(tmp_path)
    ctx.options.no_auto_deps = True
    marker = tmp_path / "marker2"
    cmd = _make_dep_recovery_pytest(tmp_path / "rec_pt2.py", marker)
    _seed_with_stack_profile(ctx, command=cmd, package_manager="poetry")

    install_calls: list = []

    def fake_install(*a, **kw):
        install_calls.append(a)
        return True, "should not be called"

    monkeypatch.setattr("worca_t.steps.s09_execute._run_dep_install", fake_install)

    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert install_calls == []


async def test_step09_install_failure_falls_through_to_heal_skip(
    tmp_path: Path, monkeypatch
):
    """When the install command itself fails, Step 9 must NOT loop — fall
    through to today's heal_skip behavior with both the install error and
    the original hint visible in artifacts."""
    ctx = _ctx(tmp_path)
    marker = tmp_path / "marker3"
    cmd = _make_dep_recovery_pytest(tmp_path / "rec_pt3.py", marker)
    _seed_with_stack_profile(ctx, command=cmd, package_manager="poetry")

    call_count = {"n": 0}

    def failing_install(pm, pkg, sut_root, log_path, *, timeout_s=600, profile=None):
        call_count["n"] += 1
        return False, "fake install failure: 401 from registry"

    monkeypatch.setattr("worca_t.steps.s09_execute._run_dep_install", failing_install)

    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert result.status == "failed"
    # Exactly one install attempt — no retry loop.
    assert call_count["n"] == 1
    # The original missing-module hint still surfaces.
    assert "allure" in (result.error or "")


async def test_step09_unknown_dep_non_tty_skips_install(
    tmp_path: Path, monkeypatch
):
    """A module name NOT in _PYTEST_PLUGIN_PROVIDERS is `guessed` confidence.
    In non-interactive runs the install must not fire — fall through to the
    existing heal_skip path."""
    ctx = _ctx(tmp_path)

    # Fake pytest that errors with an unmapped module name.
    script = tmp_path / "unmapped_pytest.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write(\"ImportError while loading conftest 'tests/conftest.py'.\\n\")\n"
        "sys.stderr.write(\"E   ModuleNotFoundError: No module named 'some_random_pkg_xyz'\\n\")\n"
        "sys.exit(4)\n",
        encoding="utf-8",
    )
    cmd = f"{sys.executable} {script.as_posix()}"
    _seed_with_stack_profile(ctx, command=cmd, package_manager="poetry")

    install_calls: list = []
    monkeypatch.setattr(
        "worca_t.steps.s09_execute._run_dep_install",
        lambda *a, **kw: (install_calls.append(a), (True, "x"))[1],
    )
    # Force non-TTY so the HITL branch is skipped without manual interaction.
    monkeypatch.setattr("worca_t.steps.s09_execute.sys.stdin.isatty", lambda: False)

    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert install_calls == []


# ---------------------------------------------------------------------------
# B-3: Step 9 XPath rejection in the self-heal flow
# ---------------------------------------------------------------------------


def test_count_xpath_markers_finds_known_patterns():
    src = (
        "from selenium.webdriver.common.by import By\n"
        "el = driver.find_element(By.XPATH, '//button[@id=\"a\"]')\n"
        "el2 = page.locator('xpath=//div')\n"
        "el3 = page.getByXPath('//span')\n"
    )
    assert _count_xpath_markers(src) >= 3


def test_count_xpath_markers_ignores_clean_source():
    src = (
        "page.get_by_role('button', name='Submit').click()\n"
        "page.locator('[data-testid=submit]').click()\n"
    )
    assert _count_xpath_markers(src) == 0


def test_patch_introduces_xpath_true_when_xpath_added():
    pre = b"page.get_by_role('button').click()\n"
    post = b"page.locator('xpath=//button').click()\n"
    assert _patch_introduces_xpath(pre, post) is True


def test_patch_introduces_xpath_false_when_no_new_xpath():
    pre = b"page.locator('//a').click()\n"  # already has 1 XPath
    post = b"page.locator('//a').click()  # rewritten comment\n"  # still 1
    assert _patch_introduces_xpath(pre, post) is False


def test_patch_introduces_xpath_handles_none_bytes():
    # Post is None → no file to check, no violation.
    assert _patch_introduces_xpath(b"//x", None) is False
    # Pre is None → heal CREATED a file. Any XPath in the new file counts.
    assert _patch_introduces_xpath(None, b"page.locator('//x').click()\n") is True
    assert _patch_introduces_xpath(None, b"page.get_by_role('btn').click()\n") is False


async def test_step09_rejects_xpath_heal_and_reverts(tmp_path: Path, monkeypatch):
    """A heal that introduces an XPath selector is reverted; patches_rejected
    increments; heal-log records `rejected: xpath`. When the heal created a
    new file (no pre-heal bytes), the revert deletes it; when it modified an
    existing file, the revert restores the original bytes. We assert no XPath
    survives in the SUT under either flow."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Fake fixer writes a "patched" file laced with XPath into the heal workdir.
    # s09's _apply_fixer_outputs copies it into the SUT's tests dir, then our
    # XPath gate must catch and revert.
    xpath_patched = (
        "def test_ok():\n"
        "    el = driver.find_element(By.XPATH, '//button[@id=\"submit\"]')\n"
        "    assert el\n"
    )
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "patched with xpath"}],
        files={"a.py": xpath_patched},
    )

    result = await ExecuteStep().run(ctx)
    assert result.success  # step completes; the heal was rejected, not the step
    payload = json.loads(
        (ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8")
    )
    assert payload["self_heal"]["patches_applied"] == 0
    assert payload["self_heal"]["patches_rejected"] >= 1

    # No XPath survives anywhere in the resolved sut tests dir. The heal may
    # have created `sut/worca-tests/a.py` from scratch; the gate's revert
    # deletes it. Walk every .py under sut and confirm the marker is gone.
    sut_root = ctx.workspace.sut
    for py in sut_root.rglob("*.py"):
        body = py.read_text(encoding="utf-8")
        assert "By.XPATH" not in body, f"XPath survived in {py}"
        assert "xpath=" not in body, f"XPath survived in {py}"

    # Heal-log records the XPath rejection.
    heal_log_text = (
        ctx.workspace.step_dir(9) / "self-heal" / "heal-log.jsonl"
    ).read_text(encoding="utf-8")
    assert '"rejected": "xpath"' in heal_log_text
    assert '"applied": false' in heal_log_text


# ---------------------------------------------------------------------------
# B-1: pre_attempt_cleanup rotates heal-log.jsonl between attempts
# ---------------------------------------------------------------------------


def test_pre_attempt_cleanup_rotates_heal_log(tmp_path: Path):
    """ExecuteStep.pre_attempt_cleanup archives a populated heal-log so attempt
    2 starts with a clean slate. Step 10 reads only the current heal-log;
    prior attempts move to heal-log.attempt-N.jsonl."""
    ctx = _ctx(tmp_path, seed_sut_repo=False)
    step = ExecuteStep()
    out_dir = step.out_dir(ctx.workspace)
    heal_dir = out_dir / "self-heal"
    heal_dir.mkdir(parents=True, exist_ok=True)
    heal_log = heal_dir / "heal-log.jsonl"
    heal_log.write_text('{"test_id":"T-attempt1","applied":false}\n', encoding="utf-8")

    step.pre_attempt_cleanup(ctx, attempt=2)

    assert not heal_log.exists()
    archive = heal_dir / "heal-log.attempt-1.jsonl"
    assert archive.exists()
    assert "T-attempt1" in archive.read_text(encoding="utf-8")


def test_pre_attempt_cleanup_noop_when_heal_log_empty(tmp_path: Path):
    """The hook must be safe when there's nothing to rotate (e.g. attempt 1
    failed before any heal was attempted)."""
    ctx = _ctx(tmp_path, seed_sut_repo=False)
    step = ExecuteStep()
    step.pre_attempt_cleanup(ctx, attempt=2)  # no heal-log at all
    out_dir = step.out_dir(ctx.workspace)
    (out_dir / "self-heal").mkdir(parents=True, exist_ok=True)
    (out_dir / "self-heal" / "heal-log.jsonl").write_text("", encoding="utf-8")
    step.pre_attempt_cleanup(ctx, attempt=2)  # empty heal-log
    assert not (out_dir / "self-heal" / "heal-log.attempt-1.jsonl").exists()
