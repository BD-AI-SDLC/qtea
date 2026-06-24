"""Step 9 execute-and-self-heal tests."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import StepContext
from qtea.steps.s09_execute import (
    ExecuteStep,
    _apply_fixer_outputs,
    _attempt_state_path,
    _build_bug_candidates,
    _build_fixer_prompt,
    _classify_failure,
    _compute_install_sig,
    _count_xpath_markers,
    _filter_command_for_tests,
    _lazy_probe_heal_mcp,
    _load_attempt_state,
    _partition_failures,
    _patch_introduces_xpath,
    _run_dep_install,
    _save_attempt_state,
)
from qtea.test_runner import TestRunEntry
from qtea.workspace import create_workspace

from ._fake_claude import install_fake_query
from ._sut_setup import seed_sut

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_filter_command_for_tests_pytest_adds_k_expr():
    failing = [
        TestRunEntry(id="T-login-1", name="test_login", file="t.py", status="failed"),
        TestRunEntry(id="T-logout-2", name="test_logout", file="t.py", status="failed"),
    ]
    cmd = _filter_command_for_tests("pytest tests/", failing)
    assert cmd.startswith("pytest tests/ -k ")
    assert "test_login" in cmd and "test_logout" in cmd


def test_filter_command_for_tests_strips_parametrization_suffix():
    """`-k` matches on the base function name, so a parametrized id like
    `test_x[case-1]` must collapse to `test_x` (and dedupe across params)."""
    failing = [
        TestRunEntry(id="T-1", name="test_x[case-1]", file="t.py", status="failed"),
        TestRunEntry(id="T-2", name="test_x[case-2]", file="t.py", status="failed"),
    ]
    cmd = _filter_command_for_tests("pytest tests/", failing)
    assert cmd == 'pytest tests/ -k "test_x"'


def test_filter_command_for_tests_preserves_existing_k_selector():
    """An explicit `-k` already on the command must not be clobbered."""
    failing = [TestRunEntry(id="T-1", name="test_x", file="t.py", status="failed")]
    cmd = _filter_command_for_tests('pytest tests/ -k "smoke"', failing)
    assert cmd == 'pytest tests/ -k "smoke"'


def test_filter_command_for_tests_playwright_adds_grep():
    failing = [
        TestRunEntry(id="T-a", name="logs in", file="t.spec.ts", status="failed"),
        TestRunEntry(id="T-b", name="searches", file="t.spec.ts", status="failed"),
    ]
    cmd = _filter_command_for_tests("npx playwright test", failing)
    assert "--grep" in cmd
    assert "logs\\ in" in cmd and "searches" in cmd


def test_filter_command_for_tests_playwright_preserves_existing_grep():
    failing = [TestRunEntry(id="T-a", name="logs in", file="t.spec.ts", status="failed")]
    cmd = _filter_command_for_tests('npx playwright test --grep "smoke"', failing)
    assert cmd == 'npx playwright test --grep "smoke"'


def test_filter_command_for_tests_other_frameworks_unchanged():
    failing = [TestRunEntry(id="T-a", name="logs in", file="t.spec.ts", status="failed")]
    cmd = _filter_command_for_tests("npx cypress run", failing)
    assert cmd == "npx cypress run"


def test_filter_command_for_tests_empty_returns_unchanged():
    assert _filter_command_for_tests("anything", []) == "anything"


def test_run_dep_install_poetry_noop_treated_as_failure(tmp_path, monkeypatch):
    """`poetry add <pkg>` returns exit 0 even when <pkg> is already declared
    in pyproject.toml — the stdout reads 'already present ... Nothing to add'
    and nothing gets installed. We must catch that and report failure so the
    caller (Step 8 runtime dep-recovery) doesn't claim success and re-run the
    same broken test suite expecting a different outcome."""
    import subprocess as sp

    poetry_noop_stdout = (
        "The following packages are already present in the pyproject.toml "
        "and will be skipped:\n\n  - pydantic_settings\n\nNothing to add.\n"
    )

    def fake_run(argv, **kwargs):
        return sp.CompletedProcess(argv, returncode=0, stdout=poetry_noop_stdout, stderr="")

    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
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

    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
    ok, summary = _run_dep_install(
        "poetry", "pytest-asyncio", tmp_path, tmp_path / "install.log"
    )
    assert ok is True
    assert "poetry add --group test pytest-asyncio" in summary


def test_run_dep_install_passes_isolate_venv_for_poetry(tmp_path, monkeypatch):
    """The subprocess env handed to poetry must NOT inherit VIRTUAL_ENV —
    otherwise poetry reuses qtea's parent venv as the SUT's venv (the
    original bug that motivated `isolate_venv`)."""
    import subprocess as sp

    captured_env = {}

    def fake_run(argv, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

    monkeypatch.setenv("VIRTUAL_ENV", "/qtea/.venv")
    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
    _run_dep_install("poetry", "pkg", tmp_path, tmp_path / "log")
    assert "VIRTUAL_ENV" not in captured_env


def test_run_dep_install_pip_uses_venv_pip_from_profile(tmp_path, monkeypatch):
    """pip auto-install must target the SUT's own .venv (via venv_bin from
    the profile), NOT bare `pip` from PATH. Without the path prefix the
    install would land in qtea's parent venv when VIRTUAL_ENV leaks
    (defeating the install) or in the system Python when it doesn't
    (polluting the host)."""
    import subprocess as sp

    from qtea.stack_profile import StackProfile

    captured_argv = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
    profile = StackProfile(
        language="python", package_manager="pip", wrapper_prefix=".venv/bin",
    )
    ok, _summary = _run_dep_install(
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

    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
    ok, summary = _run_dep_install("pip", "requests", tmp_path, tmp_path / "log")
    assert ok is False
    assert "pip auto-install requires" in summary


def test_run_dep_install_isolates_for_all_python_venv_managers(tmp_path, monkeypatch):
    """uv / pdm / pipenv exhibit the same VIRTUAL_ENV inheritance issue as
    poetry — all four must strip the parent venv from the subprocess env."""
    import subprocess as sp

    monkeypatch.setenv("VIRTUAL_ENV", "/qtea/.venv")
    monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", lambda argv, **kw:
        (kw.setdefault("_seen", kw.get("env", {})),
         sp.CompletedProcess(argv, 0, "installed", ""))[1])

    for pm in ("poetry", "uv", "pdm", "pipenv"):
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs.get("env") or {})
            return sp.CompletedProcess(argv, returncode=0, stdout="installed", stderr="")

        monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
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

        monkeypatch.setattr("qtea.steps.s09_execute.subprocess.run", fake_run)
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
    prompt = _build_fixer_prompt(entry, Path("/sut/qteaests"))
    assert "T-x" in prompt
    assert "tests/login.spec.ts" in prompt
    assert "locator missing" in prompt
    assert "NEVER XPath" in prompt
    assert "long\ntb" in prompt


def test_build_fixer_prompt_uses_fully_qualified_mcp_tool_names():
    """The heal prompt must reference Playwright MCP tools by their full
    `mcp__playwright__<name>` form. Bare `browser_navigate` doesn't resolve
    against the SDK's tool registry and caused the agent (in run
    20260616-083235-165ecf) to burn 9 turns guessing prefixes.

    The prompt must NOT instruct the agent to poll `WaitForMcpServers` —
    Step 9 pre-warms MCP via `_lazy_probe_heal_mcp` before invoking the
    heal agent, so the agent should never see the boot-gate instruction.

    Live-diagnosis only fires when `sut_base_url` is set — without it,
    the live_block is empty and these directives don't apply.
    """
    entry = TestRunEntry(
        id="T-x", name="logs in", file="tests/login.spec.ts", status="failed",
        message="locator missing", traceback="tb",
    )
    prompt = _build_fixer_prompt(
        entry, Path("/sut/qteaests"), sut_base_url="http://app.example",
    )
    assert "mcp__playwright__browser_navigate" in prompt, (
        "Step 9 heal prompt must reference Playwright MCP tools by their "
        "fully-qualified `mcp__playwright__*` names so the agent doesn't "
        "waste turns guessing tool-name prefixes."
    )
    assert "mcp__playwright__browser_snapshot" in prompt
    assert "WaitForMcpServers" not in prompt, (
        "Step 9 pre-warms MCP before invoking the heal agent; the agent "
        "must not be instructed to poll WaitForMcpServers."
    )


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
    sut_tests = tmp_path / "sut" / "qteaests"
    sut_tests.mkdir(parents=True)
    (sut_tests / "login.spec.ts").write_text("OLD\n", encoding="utf-8")
    wd = tmp_path / "heal"
    wd.mkdir()
    (wd / "login.spec.ts").write_text("NEW\n", encoding="utf-8")

    applied = _apply_fixer_outputs(wd, sut_tests, "login.spec.ts")
    assert applied is True
    assert (sut_tests / "login.spec.ts").read_text(encoding="utf-8") == "NEW\n"


def test_apply_fixer_outputs_handles_basename_fallback(tmp_path: Path):
    sut_tests = tmp_path / "sut" / "qteaests"
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
# Integration: step 8 end-to-end with fake pytest + fake fixer agent
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

# What a scoped `-k test_bad` heal re-run reports: ONLY the previously-failing
# test, now passing. test_ok is absent because the narrowed command (see
# `_filter_command_for_tests`) targets just the healed subset. Step 9 must MERGE
# this by id back into the first run's results (preserving test_ok).
_JUNIT_RERUN_PASS = """<testsuites><testsuite name="s" file="tests/a.py">
  <testcase name="test_bad" file="tests/a.py" time="0.02"/>
</testsuite></testsuites>"""


def _make_fake_pytest(
    script_path: Path,
    *,
    junit_xml: str,
    exit_code: int,
) -> str:
    """Write a python script that pretends to be pytest: writes qtea-junit.xml
    into its CWD then exits with the given code. Returns a command string."""
    body = (
        "import sys, os\n"
        f"open(os.path.join(os.getcwd(), 'qtea-junit.xml'), 'w', encoding='utf-8').write({junit_xml!r})\n"
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
        f"    junit = {_JUNIT_RERUN_PASS!r}\n"
        "    code = 0\n"
        "open(os.path.join(os.getcwd(), 'qtea-junit.xml'), 'w', encoding='utf-8').write(junit)\n"
        "sys.exit(code)\n"
    )
    script_path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script_path.as_posix()}"


def _ctx(tmp_path: Path, *, seed_sut_repo: bool = True) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    if seed_sut_repo:
        # Step 8 now requires `<workspace>/sut/` to be a git repo on the
        # qtea branch — pipeline.py + _materialize_sut do this in
        # production. seed_sut() mirrors that end-state for tests.
        seed_sut(ws)
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _seed_minimal_inputs(ctx: StepContext, *, command: str, framework: str = "pytest") -> None:
    # Step 7 manifest (the new contract; replaces the old tests/ mirror).
    step8 = ctx.workspace.step_dir(8)
    step8.mkdir(parents=True, exist_ok=True)
    (step8 / "generated-files.json").write_text(
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
    step8 = ctx.workspace.step_dir(8)
    step8.mkdir(parents=True, exist_ok=True)
    (step8 / "generated-files.json").write_text(
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

    # Stub the lazy MCP probe so the heal loop runs (no real `.mcp.json` in tests).
    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp",
        lambda server, env=None: (True, "ok", 0.0),
    )

    # Fake claude that returns no usable patch (no files).
    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "no fix"}],
        files={},
    )

    result = await ExecuteStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    assert result.sub_status == "bugs_found"
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

    # Stub the lazy MCP probe so the heal loop runs in unit tests (no real
    # `.mcp.json` / `npx` in the test environment).
    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp",
        lambda server, env=None: (True, "ok", 0.0),
    )

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
    # After the scoped `-k test_bad` heal re-run, the flaky pytest reports only
    # `test_bad` (now passing). Step 9 MERGES that by id into the first run's
    # results: `test_bad` flips to passed while `test_ok` is preserved — so the
    # merged totals show 2 passed / 0 failed (a wholesale replace would lose
    # test_ok and report only 1 passed).
    assert payload["totals"]["failed"] == 0
    assert payload["totals"]["passed"] == 2
    assert result.status == "completed"


_JUNIT_ALL_ERROR = """<testsuites><testsuite name="s" file="tests/a.py">
  <testcase name="test_a" file="tests/a.py" time="0.01">
    <error message="DNS failed">setup failed: net::ERR_NAME_NOT_RESOLVED</error>
  </testcase>
  <testcase name="test_b" file="tests/a.py" time="0.01">
    <error message="DNS failed">setup failed: net::ERR_NAME_NOT_RESOLVED</error>
  </testcase>
</testsuite></testsuites>"""


async def test_step09_fails_when_all_tests_error_none_pass(tmp_path: Path, monkeypatch):
    """When every test errors (e.g. DNS unreachable, auth fixture crash) and
    none pass, no assertion was evaluated. Previously this yielded `warned` +
    `success=True`, masking total environment failures. The new contract:
    `failed` + `success=False` so the pipeline halts and the report doesn't
    claim partial success."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_ALL_ERROR, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "no fix"}],
        files={},
    )

    result = await ExecuteStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    assert "all" in (result.error or "").lower()
    assert "zero passing" in (result.error or "").lower()
    # Artifacts still written for downstream inspection.
    payload = json.loads(
        (ctx.workspace.step_dir(9) / "run-results.json").read_text(encoding="utf-8")
    )
    assert payload["totals"]["passed"] == 0
    assert payload["totals"]["errors"] == 2


async def test_step09_fails_when_only_runner_failure_results(tmp_path: Path):
    """Regression: when pytest aborts in conftest (missing dep, syntax error,
    exit code 4) it produces no junit XML and the test_runner emits a single
    synthesised `T-runner-failure` entry. Previously that yielded `warned`
    status and Step 9/11 ran on garbage. The new contract: this is an
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
# Auto-install of missing test deps (Step 8 runtime recovery)
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
    step8 = ctx.workspace.step_dir(8)
    step8.mkdir(parents=True, exist_ok=True)
    (step8 / "generated-files.json").write_text(
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
        f"open(os.path.join(os.getcwd(), 'qtea-junit.xml'), 'w', encoding='utf-8').write({_JUNIT_PASS!r})\n"
        "sys.exit(0)\n"
    )
    script_path.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script_path.as_posix()}"


async def test_step09_recovery_auto_installs_known_missing_dep(
    tmp_path: Path, monkeypatch
):
    """Default path: runner fails with missing_module on a name in the curated
    table → Step 8 runs the install (stubbed), commits, re-runs once, and the
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

    monkeypatch.setattr("qtea.steps.s09_execute._run_dep_install", fake_install)

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

    monkeypatch.setattr("qtea.steps.s09_execute._run_dep_install", fake_install)

    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert install_calls == []


async def test_step09_install_failure_falls_through_to_heal_skip(
    tmp_path: Path, monkeypatch
):
    """When the install command itself fails, Step 8 must NOT loop — fall
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

    monkeypatch.setattr("qtea.steps.s09_execute._run_dep_install", failing_install)

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
        "qtea.steps.s09_execute._run_dep_install",
        lambda *a, **kw: (install_calls.append(a), (True, "x"))[1],
    )
    # Force non-TTY so the HITL branch is skipped without manual interaction.
    monkeypatch.setattr("qtea.steps.s09_execute.sys.stdin.isatty", lambda: False)

    result = await ExecuteStep().run(ctx)
    assert not result.success
    assert install_calls == []


# ---------------------------------------------------------------------------
# B-3: Step 8 XPath rejection in the self-heal flow
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

    # Stub the lazy MCP probe so the heal loop runs (no real `.mcp.json` in tests).
    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp",
        lambda server, env=None: (True, "ok", 0.0),
    )

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
    # have created `sut/qteaests/a.py` from scratch; the gate's revert
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
    2 starts with a clean slate. Step 9 reads only the current heal-log;
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


# ---------------------------------------------------------------------------
# Lazy Playwright MCP probe (only fires when heal is actually needed)
# ---------------------------------------------------------------------------


def test_execute_step_mcp_servers_required_is_empty():
    """Pipeline-level MCP preflight must skip Step 9 — the probe is lazy.
    Eager preflight would pay the 5-15s warmup on every Step 9 run, even
    when no test fails and the heal agent never spawns."""
    assert ExecuteStep.mcp_servers_required == frozenset()
    assert ExecuteStep._LAZY_MCP_SERVER == "playwright"


def test_lazy_probe_returns_failure_when_mcp_json_missing(monkeypatch):
    """A missing .mcp.json yields a descriptive failure detail — the
    caller logs and skips heal without crashing the whole step."""
    def fake_load(path=None, env=None):
        raise FileNotFoundError(".mcp.json not found")
    monkeypatch.setattr(
        "qtea.mcp_manager.load_mcp_config", fake_load,
    )
    ok, detail, _warmup_s = _lazy_probe_heal_mcp("playwright")
    assert ok is False
    assert ".mcp.json" in detail


def test_lazy_probe_returns_failure_when_server_not_declared(monkeypatch):
    """When the server name isn't declared in .mcp.json, fail cleanly."""
    monkeypatch.setattr(
        "qtea.mcp_manager.load_mcp_config", lambda env=None: {},
    )
    ok, detail, _warmup_s = _lazy_probe_heal_mcp("playwright")
    assert ok is False
    assert "playwright" in detail
    assert "not declared" in detail


def test_lazy_probe_returns_success_when_server_probes_ok(monkeypatch):
    """Happy path: server is declared + probes OK."""
    fake_server = object()
    monkeypatch.setattr(
        "qtea.mcp_manager.load_mcp_config",
        lambda path=None, env=None: {"playwright": fake_server},
    )
    monkeypatch.setattr(
        "qtea.mcp_manager.probe_server",
        lambda srv, timeout_s=30.0: (True, "ok"),
    )
    ok, _detail, _warmup_s = _lazy_probe_heal_mcp("playwright")
    assert ok is True


def test_lazy_probe_returns_failure_when_probe_fails(monkeypatch):
    """Probe failure (e.g. npx missing, server crash) surfaces the detail."""
    fake_server = object()
    monkeypatch.setattr(
        "qtea.mcp_manager.load_mcp_config",
        lambda path=None, env=None: {"playwright": fake_server},
    )
    monkeypatch.setattr(
        "qtea.mcp_manager.probe_server",
        lambda srv, timeout_s=30.0: (False, "npx not on PATH"),
    )
    ok, detail, _warmup_s = _lazy_probe_heal_mcp("playwright")
    assert ok is False
    assert "npx" in detail


async def test_step09_skips_heal_when_mcp_probe_fails(tmp_path: Path, monkeypatch):
    """When the lazy MCP probe fails just before the heal loop, the heal
    is skipped (best-effort), per-test heal-log entries record the skip
    reason, and the failing tests still flow to bug-candidates.json so
    Step 10 sees them."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Probe always fails — no Playwright MCP available.
    probe_calls: list[str] = []

    def fake_probe(server_name, env=None):
        probe_calls.append(server_name)
        return False, "npx not on PATH", 0.0

    monkeypatch.setattr("qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe)

    # Fake claude — should NEVER be called when MCP probe fails.
    claude_calls: list = []

    async def fake_run_agent(*args, **kwargs):
        claude_calls.append((args, kwargs))
        from qtea.claude_runner import RunResult as _RR
        return _RR(success=False, error="should not run", workdir=kwargs.get("workdir"))

    monkeypatch.setattr("qtea.steps.s09_execute.run_agent", fake_run_agent)

    result = await ExecuteStep().run(ctx)
    # Step still completes (heal is best-effort). Real failures flow downstream.
    assert result.success
    assert result.status == "completed"
    assert result.sub_status == "bugs_found"
    # Probe was attempted exactly once with the "playwright" server name.
    assert probe_calls == ["playwright"]
    # Heal agent was NEVER invoked.
    assert claude_calls == []
    # Heal-log records the skip with the probe failure reason.
    heal_log = ctx.workspace.step_dir(9) / "self-heal" / "heal-log.jsonl"
    lines = [
        json.loads(ln)
        for ln in heal_log.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 1  # one per failing test (_JUNIT_FAIL has 1 fail)
    assert lines[0]["applied"] is False
    assert "Playwright MCP probe failed" in lines[0]["agent_error"]
    assert "npx not on PATH" in lines[0]["agent_error"]
    # Bug-candidates still emitted so Step 10 sees the failure.
    bugs = json.loads(
        (ctx.workspace.step_dir(9) / "bug-candidates.json").read_text(encoding="utf-8")
    )
    assert len(bugs["candidates"]) >= 1


async def test_step09_skips_probe_when_no_failing_tests(tmp_path: Path, monkeypatch):
    """Green run (all tests pass): lazy probe must NOT fire at all — the
    whole point of moving it inside `run()`. Validates the optimization."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_PASS, exit_code=0)
    _seed_minimal_inputs(ctx, command=cmd)

    probe_calls: list[str] = []

    def fake_probe(server_name, env=None):
        probe_calls.append(server_name)
        return True, "ok", 0.0

    monkeypatch.setattr("qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe)

    result = await ExecuteStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    # The optimization: probe was NEVER called because no test failed.
    assert probe_calls == []


async def test_step09_skips_probe_when_no_llm_resolve_set(
    tmp_path: Path, monkeypatch,
):
    """QTEA_NO_LLM_RESOLVE=1 short-circuits the heal loop BEFORE the
    lazy probe. Verify the probe is skipped — symmetric with the env flag's
    "no LLM spend" contract (probing MCP also burns time/cache)."""
    monkeypatch.setenv("QTEA_NO_LLM_RESOLVE", "1")
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    probe_calls: list[str] = []

    def fake_probe(server_name, env=None):
        probe_calls.append(server_name)
        return True, "ok", 0.0

    monkeypatch.setattr("qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe)

    result = await ExecuteStep().run(ctx)
    # Tests failed but heal was disabled by the env flag.
    assert result.success
    assert result.status == "completed"
    assert result.sub_status == "bugs_found"
    # Probe skipped — the env flag is the single "no extra cost" dial.
    assert probe_calls == []


# ---------------------------------------------------------------------------
# Storage-state injection (Playwright MCP --storage-state= flag)
# ---------------------------------------------------------------------------


async def test_step09_storage_state_arg_threaded_into_mcp_env_when_resolved(
    tmp_path: Path, monkeypatch,
):
    """When a storage-state file exists at the SUT convention path,
    Step 9 resolves it, computes the ``--storage-state=<path>`` CLI flag,
    and threads it into the MCP env overlay passed to
    ``_lazy_probe_heal_mcp`` (and downstream to ``stage_mcp_config`` for
    the agent SDK)."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Drop a storage-state file at the SUT convention path so the
    # 4-tier resolver picks it up.
    storage_path = ctx.workspace.sut / ".qtea" / "storage-state.json"
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    captured_env: dict[str, dict[str, str] | None] = {"env": None}

    def fake_probe(server, env=None):
        captured_env["env"] = env
        # Probe fails so we don't try to actually run the heal agent (no
        # MCP available in unit test env). The env-capture is what matters.
        return False, "stub", 0.0

    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe,
    )

    await ExecuteStep().run(ctx)

    assert captured_env["env"] is not None
    arg = captured_env["env"]["QTEA_STORAGE_STATE_ARG"]
    assert arg.startswith("--storage-state=")
    assert "storage-state.json" in arg


async def test_step09_storage_state_arg_picked_up_after_same_run_auto_capture(
    tmp_path: Path, monkeypatch,
):
    """**The whole point of Use case B**: storage state file does NOT exist
    at Step 9 start. Test runner writes it (simulating the runtime plugin's
    auto-capture on first passing test). The heal loop re-resolves AFTER
    run_tests returns and now sees the workspace path — the MCP env passed
    to the lazy probe carries the freshly-captured file.

    Without the post-run re-resolve this test would fail with an empty
    ARG (the early resolve, which ran before the test runner wrote the
    file, would have returned None).
    """
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)

    # Verify the workspace file does NOT exist before the step starts.
    expected_path = ctx.workspace.root / "storage-state.json"
    assert not expected_path.exists()

    # Wrap run_tests so it also writes the workspace storage-state.json
    # before returning — this is exactly what the runtime plugin does inside
    # the pytest subprocess on the first passing test.
    from qtea import test_runner as _tr_mod
    real_run_tests = _tr_mod.run_tests

    def fake_run_tests(*args, **kwargs):
        result = real_run_tests(*args, **kwargs)
        # Simulate the runtime plugin's pytest_runtest_teardown capture.
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(
            '{"cookies":[{"name":"session"}],"origins":[]}',
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr("qtea.steps.s09_execute.run_tests", fake_run_tests)

    captured_env: dict[str, dict[str, str] | None] = {"env": None}

    def fake_probe(server, env=None):
        captured_env["env"] = dict(env or {})  # snapshot at probe time
        return False, "stub", 0.0

    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe,
    )

    await ExecuteStep().run(ctx)

    # File was indeed written by the fake runner.
    assert expected_path.is_file()
    # MCP env at probe time carries the freshly-captured file's path
    # (this would be "" without the post-run re-resolve).
    assert captured_env["env"] is not None
    arg = captured_env["env"]["QTEA_STORAGE_STATE_ARG"]
    assert arg.startswith("--storage-state=")
    assert "storage-state.json" in arg


async def test_step09_storage_state_arg_empty_when_no_source(
    tmp_path: Path, monkeypatch,
):
    """No CLI flag, no env var, no convention file, no workspace
    auto-capture → arg is the empty string (mcp_manager filters it out
    before reaching the MCP subprocess)."""
    ctx = _ctx(tmp_path)
    cmd = _make_fake_pytest(tmp_path / "pt.py", junit_xml=_JUNIT_FAIL, exit_code=1)
    _seed_minimal_inputs(ctx, command=cmd)
    # Make sure no env var leaks in.
    monkeypatch.delenv("QTEA_STORAGE_STATE", raising=False)

    captured_env: dict[str, dict[str, str] | None] = {"env": None}

    def fake_probe(server, env=None):
        captured_env["env"] = env
        return False, "stub", 0.0

    monkeypatch.setattr(
        "qtea.steps.s09_execute._lazy_probe_heal_mcp", fake_probe,
    )

    await ExecuteStep().run(ctx)

    assert captured_env["env"] is not None
    assert captured_env["env"]["QTEA_STORAGE_STATE_ARG"] == ""


def test_build_fixer_prompt_includes_storage_state_directive_when_path_set(
    tmp_path: Path,
):
    """The heal prompt must surface the storage-state directive when a
    path is provided — agent skips the SUT's sign-in helper and goes
    direct to the failing page URL."""
    storage_path = tmp_path / ".qtea" / "storage-state.json"
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    entry = TestRunEntry(
        id="T-x", name="logs in", file="tests/login.spec.ts", status="failed",
        message="locator missing", traceback="tb",
    )
    prompt = _build_fixer_prompt(
        entry, Path("/sut/qteaests"),
        sut_base_url="http://app.example",
        storage_state_path=storage_path,
    )
    assert "PRE-LOADED STORAGE STATE" in prompt
    assert "DO NOT call the SUT's sign-in helper" in prompt
    # Workflow step (1) variant for storage-state path
    assert "pre-loaded storage state" in prompt.lower()
    assert "do NOT call the SUT's sign-in helper" in prompt


def test_build_fixer_prompt_omits_storage_state_block_when_unset():
    """No storage-state path → no directive in the prompt. Agent follows
    the SUT's auth flow as before."""
    entry = TestRunEntry(
        id="T-x", name="logs in", file="tests/login.spec.ts", status="failed",
        message="locator missing", traceback="tb",
    )
    prompt = _build_fixer_prompt(
        entry, Path("/sut/qteaests"),
        sut_base_url="http://app.example",
        storage_state_path=None,
    )
    assert "PRE-LOADED STORAGE STATE" not in prompt
    # And the standard auth-replay workflow IS present.
    assert "follow the SUT's auth flow" in prompt


# ---------------------------------------------------------------------------
# Failure-class-aware heal prompt (Fix 2A)
# ---------------------------------------------------------------------------


def test_build_fixer_prompt_includes_failure_class_assertion_value():
    """When failure_class='assertion_value', the prompt contains the class
    label and the assertion-value strategy hint."""
    entry = TestRunEntry(
        id="T-x", name="checks aria", file="tests/a11y.spec.ts",
        status="failed",
        message="assert None == 'true'",
        traceback="AssertionError: assert None == 'true'",
    )
    prompt = _build_fixer_prompt(
        entry, Path("/sut/qteaests"),
        failure_class="assertion_value",
    )
    assert "Failure class: `assertion_value`" in prompt
    assert "Diagnose from the traceback first" in prompt
    assert "OUT_OF_SCOPE: assertion-attribute-defect" in prompt


def test_build_fixer_prompt_includes_failure_class_locator_timeout():
    """When failure_class='locator_timeout', the prompt contains the class
    label but NOT the assertion-value strategy hint."""
    entry = TestRunEntry(
        id="T-x", name="clicks btn", file="tests/nav.spec.ts",
        status="failed",
        message="TimeoutError waiting for locator",
        traceback="playwright._impl._errors.TimeoutError",
    )
    prompt = _build_fixer_prompt(
        entry, Path("/sut/qteaests"),
        failure_class="locator_timeout",
    )
    assert "Failure class: `locator_timeout`" in prompt
    assert "Diagnose from the traceback first" not in prompt


def test_build_fixer_prompt_omits_failure_class_when_none():
    """Backward compat: failure_class=None (default) produces no class line."""
    entry = TestRunEntry(
        id="T-x", name="logs in", file="tests/login.spec.ts",
        status="failed",
        message="locator missing", traceback="tb",
    )
    prompt = _build_fixer_prompt(entry, Path("/sut/qteaests"))
    assert "Failure class:" not in prompt


# ---------------------------------------------------------------------------
# Cross-attempt state (Tasks 2 + 4 + 5)
# ---------------------------------------------------------------------------


def test_save_and_load_attempt_state_round_trip(tmp_path):
    """Persistence helper roundtrips failing pairs, no_patch_ids, and
    install_sig so attempt 2's pre-run logic can read attempt 1's
    outcomes without depending on in-memory state."""
    _save_attempt_state(
        tmp_path, attempt=1,
        failing=[("T-a", "test_a"), ("T-b", "test_b")],
        no_patch_ids=["T-b"],
        install_sig="abc123",
    )
    state = _load_attempt_state(tmp_path, 1)
    assert state is not None
    assert state["attempt"] == 1
    assert state["failing"] == [
        {"id": "T-a", "name": "test_a"},
        {"id": "T-b", "name": "test_b"},
    ]
    assert state["no_patch_ids"] == ["T-b"]
    assert state["install_sig"] == "abc123"


def test_load_attempt_state_missing_file_returns_none(tmp_path):
    """Cold attempt 1 has no prior state — load returns None and
    callers must treat that as 'no narrowing' (cold-run semantics)."""
    assert _load_attempt_state(tmp_path, 1) is None


def test_load_attempt_state_corrupt_json_returns_none(tmp_path):
    """A partial-write or hand-edit shouldn't crash the retry path —
    the loader must swallow JSON errors and return None so attempt 2
    falls back to running cold rather than aborting."""
    _attempt_state_path(tmp_path, 1).write_text("not json", encoding="utf-8")
    assert _load_attempt_state(tmp_path, 1) is None


def test_install_sig_changes_when_lockfile_mtime_changes(tmp_path, monkeypatch):
    """Same lockfile content but a different mtime/size → different sig.
    This is the correct behavior: ``poetry install`` re-runs whenever a
    dependency changes, and mtime+size is a cheap-enough proxy without
    hashing every byte of large lockfiles."""
    from dataclasses import dataclass
    @dataclass
    class _Profile:
        package_manager: str = "poetry"
    lock = tmp_path / "poetry.lock"
    lock.write_text("contents-v1\n", encoding="utf-8")
    sig1 = _compute_install_sig(tmp_path, _Profile())
    # Touch with a different size — sig must differ.
    lock.write_text("contents-v2-longer\n", encoding="utf-8")
    sig2 = _compute_install_sig(tmp_path, _Profile())
    assert sig1 is not None
    assert sig2 is not None
    assert sig1 != sig2


def test_install_sig_returns_none_when_no_lockfile(tmp_path):
    """No lockfile → no signature → never skip install. Defensively
    forces a re-install rather than risk a stale env."""
    from dataclasses import dataclass
    @dataclass
    class _Profile:
        package_manager: str = "poetry"
    assert _compute_install_sig(tmp_path, _Profile()) is None


def test_install_sig_returns_none_when_profile_is_none(tmp_path):
    """No stack profile (non-Python stack, broken detection) →
    signature is None → install logic falls through naturally."""
    (tmp_path / "poetry.lock").write_text("x\n", encoding="utf-8")
    assert _compute_install_sig(tmp_path, None) is None


# ---------------------------------------------------------------------------
# Failure classifier (`_classify_failure` / `_partition_failures`)
#
# Test data lifted verbatim from run 20260621-213751-ee0fef
# (`artifacts/step09/run-results.json`) so the classifier is anchored on
# real Playwright + pytest output, not synthetic strings.
# ---------------------------------------------------------------------------


def _mk_entry(*, id_: str = "T-x", message: str = "", traceback: str = ""):
    return TestRunEntry(
        id=id_, name=id_.lstrip("T-"), file="t.py",
        status="failed", message=message, traceback=traceback,
    )


def test_classify_locator_timeout_playwright_module_path():
    """The canonical Playwright TimeoutError on Locator.get_attribute —
    the exact shape that caused 4+ failures in run 20260621-213751-ee0fef."""
    entry = _mk_entry(
        message=(
            "playwright._impl._errors.TimeoutError: Locator.get_attribute: "
            "Timeout 60000ms exceeded.\nCall log:\n  - waiting for "
            "locator(\"a[href*='vertexaisearch'] img\")"
        ),
    )
    assert _classify_failure(entry) == "locator_timeout"


def test_classify_locator_timeout_click():
    entry = _mk_entry(
        message="playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.",
    )
    assert _classify_failure(entry) == "locator_timeout"


def test_classify_locator_timeout_waiting_for_event():
    """Timeout waiting for a page event (e.g. new tab on click). Different
    surface form — no `Locator.` prefix — but still locator/interaction
    driven and curable by heal."""
    entry = _mk_entry(
        message='Timeout 30000ms exceeded while waiting for event "page"',
    )
    assert _classify_failure(entry) == "locator_timeout"


def test_classify_tbd_unresolvable():
    """JIT runtime gave up after bundle exhaustion + LLM re-resolve.
    Real example: tooltip-on-hover (not visible in initial AOM)."""
    entry = _mk_entry(
        message=(
            "Failed: qtea JIT runtime: could not resolve locator "
            "'tooltip element shown on hover over Gemini Enterprise button'."
        ),
    )
    assert _classify_failure(entry) == "tbd_unresolvable"


def test_classify_fixture_missing():
    entry = _mk_entry(
        message="fixture 'snapshot' not found",
    )
    assert _classify_failure(entry) == "fixture_missing"


def test_classify_import_error():
    entry = _mk_entry(message="ModuleNotFoundError: No module named 'dotenv'")
    assert _classify_failure(entry) == "import_error"


def test_classify_wcag_violation():
    entry = _mk_entry(
        message=(
            "AssertionError: Expected zero WCAG 2.1 AA violations, got 4: "
            "['button-name', 'color-contrast', 'link-name', 'select-name']"
        ),
    )
    assert _classify_failure(entry) == "wcag_violation"


def test_classify_tti_budget():
    entry = _mk_entry(
        message="AssertionError: p95 TTI 330.6ms exceeds budget of 50ms",
    )
    assert _classify_failure(entry) == "tti_budget"


def test_classify_dom_order():
    entry = _mk_entry(
        message=(
            "AssertionError: Gemini button should appear before New Chat "
            "in DOM order\nassert False is True"
        ),
    )
    assert _classify_failure(entry) == "dom_order"


def test_classify_assertion_value_falls_through_to_healable():
    """Bare AssertionError without a real-bug signature — typically a
    downstream symptom of locator drift (wrong element → wrong value).
    Treated as healable so re-targeting the locator can fix it."""
    entry = _mk_entry(message="AssertionError: assert None == 'noopener noreferrer'")
    assert _classify_failure(entry) == "assertion_value"


def test_classify_unknown_when_no_message():
    """Defaults to ``unknown`` (which the partition treats as healable)
    so a classifier gap never loses a fix opportunity."""
    assert _classify_failure(_mk_entry()) == "unknown"


def test_classify_unknown_when_no_pattern_matches():
    entry = _mk_entry(message="some weird non-pytest-shaped output")
    assert _classify_failure(entry) == "unknown"


def test_classify_tti_pattern_does_not_match_settings_word():
    """Regression: an earlier classifier matched `TTI` inside `seTTIngs`
    in AOM dumps, falsely categorizing locator-timeout failures as
    `tti_budget`. Word boundary requirement prevents this."""
    entry = _mk_entry(
        message=(
            "AssertionError: Locator expected to be visible\n"
            'Actual value: - button "Settings"\n- link "Go to Gemini Enterprise"\n'
        ),
    )
    # Should classify as assertion_value (healable), not tti_budget.
    assert _classify_failure(entry) == "assertion_value"


def test_partition_splits_healable_from_real_bugs():
    """The full 11-failure decomposition from run 20260621-213751-ee0fef
    — 7 healable + 3 real bugs + 1 codegen bug (fixture_missing → real)."""
    failing = [
        _mk_entry(id_="T-1", message="playwright._impl._errors.TimeoutError: Locator.get_attribute: Timeout 60000ms exceeded."),
        _mk_entry(id_="T-2", message="AssertionError: assert None == 'noopener noreferrer'"),
        _mk_entry(id_="T-3", message="fixture 'snapshot' not found"),
        _mk_entry(id_="T-4", message="playwright._impl._errors.TimeoutError: Locator.select_option: Timeout 60000ms exceeded."),
        _mk_entry(id_="T-5", message="AssertionError: Locator expected to be visible"),
        _mk_entry(id_="T-6", message="Failed: qtea JIT runtime: could not resolve locator 'tooltip ...'"),
        _mk_entry(id_="T-7", message="playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded."),
        _mk_entry(id_="T-8", message="playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded."),
        _mk_entry(id_="T-9", message="AssertionError: Expected zero WCAG 2.1 AA violations, got 4"),
        _mk_entry(id_="T-10", message='Timeout 30000ms exceeded while waiting for event "page"'),
        _mk_entry(id_="T-11", message="AssertionError: p95 TTI 330.6ms exceeds budget of 50ms"),
        _mk_entry(id_="T-12", message="AssertionError: Gemini button should appear before New Chat in DOM order"),
    ]
    healable, real_bugs = _partition_failures(failing)
    # Use sets so the assertion isn't sensitive to lexicographic ordering
    # (e.g. "T-10" < "T-2" by string compare). The classifier itself
    # doesn't promise order; the partition just preserves input order.
    healable_ids = {e.id for e in healable}
    real_bug_ids = {e.id for e, _ in real_bugs}
    # 7 locator/timeout + assertion-value + tbd_unresolvable → healable
    assert healable_ids == {"T-1", "T-2", "T-4", "T-5", "T-6", "T-7", "T-8", "T-10"}
    # fixture-missing + WCAG + TTI + dom-order → real bugs
    assert real_bug_ids == {"T-3", "T-9", "T-11", "T-12"}
    # Each real-bug row carries its classifier label for the heal-log audit.
    by_id = dict((e.id, cls) for e, cls in real_bugs)
    assert by_id["T-3"] == "fixture_missing"
    assert by_id["T-9"] == "wcag_violation"
    assert by_id["T-11"] == "tti_budget"
    assert by_id["T-12"] == "dom_order"


def test_partition_heal_all_escape_hatch_skips_classifier(monkeypatch):
    """QTEA_HEAL_ALL=1 short-circuits the classifier so the operator
    can debug heal coverage when the classifier is suspected of false
    exclusion."""
    monkeypatch.setenv("QTEA_HEAL_ALL", "1")
    failing = [
        _mk_entry(id_="T-wcag", message="AssertionError: Expected zero WCAG 2.1 AA violations"),
        _mk_entry(id_="T-tti", message="AssertionError: p95 TTI exceeds budget of 50ms"),
    ]
    healable, real_bugs = _partition_failures(failing)
    assert [e.id for e in healable] == ["T-wcag", "T-tti"]
    assert real_bugs == []


def test_partition_empty_input_returns_empty_lists():
    healable, real_bugs = _partition_failures([])
    assert healable == []
    assert real_bugs == []
