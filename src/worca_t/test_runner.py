"""Framework-agnostic test runner used by Step 9.

Resolves the test-run command for the detected framework, executes it as a
subprocess (with the corporate proxy + masked secrets), and parses framework-
specific output into a normalized `RunResult` dataclass.

Supported result formats (priority order, per framework):

  playwright-ts / playwright-py / jest / vitest / mocha / cypress / wdio
      -> JSON reporter file (configurable)
      -> JUnit XML fallback
  pytest / selenium-py
      -> JUnit XML (we always pass `--junitxml`)
  selenium-java
      -> Surefire XML under `target/surefire-reports/`
  robot
      -> `output.xml`

For tests not present in the parsed output we emit a synthetic `error` entry
so the run-results.json stays a faithful join with tbd-index.json.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from worca_t.logging_setup import get_logger
from worca_t.md_parser import slugify
from worca_t.proxy import safe_subprocess_env, with_proxy_env
from worca_t.stack_profile import PYTHON_VENV_MANAGERS, StackProfile, wrap_command

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Default commands per framework. Each tuple = (cmd_template, parser_id).
# ---------------------------------------------------------------------------

_DEFAULT_COMMANDS: dict[str, tuple[str, str]] = {
    "playwright-ts": (
        "npx playwright test --reporter=json",
        "playwright-json",
    ),
    "playwright-py": (
        "pytest --junitxml={junit}",
        "junit",
    ),
    "pytest": (
        "pytest --junitxml={junit}",
        "junit",
    ),
    "selenium-py": (
        "pytest --junitxml={junit}",
        "junit",
    ),
    "cypress": (
        "npx cypress run --reporter json",
        "mocha-json",
    ),
    "jest": (
        "npx jest --json --outputFile={json_out}",
        "jest-json",
    ),
    "vitest": (
        "npx vitest run --reporter=json --outputFile={json_out}",
        "jest-json",
    ),
    "mocha": (
        "npx mocha --reporter json --reporter-options output={json_out}",
        "mocha-json",
    ),
    "wdio": (
        "npx wdio run wdio.conf.js",
        "junit",
    ),
    "selenium-java": ("mvn -B test", "surefire"),
    "robot": ("robot --output {robot_xml} tests/", "robot-xml"),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TestRunEntry:
    """Normalized result for a single test execution."""

    __test__ = False  # tell pytest this is not a test class

    id: str
    name: str
    file: str
    status: str  # passed | failed | skipped | error
    duration_s: float | None = None
    message: str | None = None
    traceback: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    attachments: list[dict] = field(default_factory=list)
    # Set ONLY on synthetic `T-runner-failure` entries when the runner blew
    # up at collection time (missing module, broken conftest, etc.). Tells
    # step 9 to skip the self-heal loop — there's no test to patch — and
    # carries the actionable hint surfaced to the user. Shape:
    #   {"kind": "missing_module" | "collection_error",
    #    "module": str | None, "hint": str, "summary": str}
    runner_failure: dict | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "file": self.file,
            "status": self.status,
            "duration_s": self.duration_s,
            "message": self.message,
            "traceback": self.traceback,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "attachments": self.attachments,
            "runner_failure": self.runner_failure,
        }


@dataclass
class RunResult:
    __test__ = False

    framework: str
    command: str
    cwd: str
    started_at: str
    finished_at: str
    duration_s: float
    exit_code: int
    results: list[TestRunEntry]
    stdout: str = ""
    stderr: str = ""

    @property
    def totals(self) -> dict[str, int]:
        out = {"tests": len(self.results), "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        for r in self.results:
            if r.status == "passed":
                out["passed"] += 1
            elif r.status == "failed":
                out["failed"] += 1
            elif r.status == "skipped":
                out["skipped"] += 1
            else:
                out["errors"] += 1
        return out

    def as_dict(self) -> dict:
        return {
            "framework": self.framework,
            "command": self.command,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "totals": self.totals,
            "results": [r.as_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Command resolution
# ---------------------------------------------------------------------------


def resolve_command(
    framework: str,
    *,
    detected: str | None,
    cwd: Path,
    profile: StackProfile | None = None,
) -> tuple[str, str]:
    """Pick command + parser id.

    Resolution order (first non-empty wins):
      1. `detected` — the command the researcher agent extracted from
         README / CI / pyproject. Used verbatim; we only append the junit
         flag when the parser expects it and the command doesn't already
         have one. The researcher is expected to include the package-manager
         wrapper itself (e.g. ``poetry run pytest …``).
      2. Framework default from `_DEFAULT_COMMANDS`, wrapped with the
         package-manager prefix from `profile` (e.g. ``pytest …`` becomes
         ``poetry run pytest …``). This is the load-bearing fallback when
         the researcher fails to extract a runnable command.
      3. Bare ``pytest --junitxml=…`` as the universal fallback.
    """
    if detected:
        parser = _DEFAULT_COMMANDS.get(framework, ("", "auto"))[1]
        if parser == "junit" and "--junitxml" not in detected:
            detected = f"{detected} --junitxml={(cwd / 'worca-junit.xml').as_posix()}"
        return detected, parser
    if framework in _DEFAULT_COMMANDS:
        template, parser = _DEFAULT_COMMANDS[framework]
        bare = _expand_command(template, cwd)
        return wrap_command(profile, bare), parser
    bare = _expand_command("pytest --junitxml={junit}", cwd)
    return wrap_command(profile, bare), "junit"


def _expand_command(template: str, cwd: Path) -> str:
    return template.format(
        junit=str((cwd / "worca-junit.xml").as_posix()),
        json_out=str((cwd / "worca-results.json").as_posix()),
        robot_xml=str((cwd / "worca-output.xml").as_posix()),
    )


# Match `--headless`, `--headless=true`, and `--headless true` forms. Bounded
# on the right by whitespace, `=`, or end-of-string so we don't accidentally
# strip a flag like `--headless-mode` (hypothetical, but defensive).
_HEADLESS_FLAG_RE = re.compile(
    r"\s+--headless(?:=(?:true|false|0|1))?(?=\s|$)",
    re.IGNORECASE,
)
_HEADLESS_WITH_VALUE_RE = re.compile(
    r"\s+--headless\s+(?:true|false|0|1)(?=\s|$)",
    re.IGNORECASE,
)


def _strip_headless_flag(command: str) -> str:
    """Best-effort removal of `--headless` from a shell-style command string.

    Handles the common variants: bare flag, `--headless=true|false|0|1`, and
    space-separated `--headless true|false|0|1`. Leaves the command unchanged
    when no such flag is present. We deliberately do NOT insert `--headed` —
    Playwright-TS / Cypress / pytest-playwright all default to headed when
    `--headless` is absent, and some frameworks (notably plain pytest with a
    custom conftest hook) reject `--headed` as an unknown option.
    """
    out = _HEADLESS_WITH_VALUE_RE.sub("", command)
    out = _HEADLESS_FLAG_RE.sub("", out)
    return out


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------


def _split_command(command: str) -> list[str]:
    if os.name == "nt":
        # On Windows, shlex.split with posix=False keeps backslashes intact.
        return shlex.split(command, posix=False)
    return shlex.split(command)


def execute_command(
    command: str,
    *,
    cwd: Path,
    timeout_s: int,
    env_extra: dict[str, str] | None = None,
    isolate_venv: bool = False,
) -> tuple[int, str, str, float]:
    env = safe_subprocess_env(isolate_venv=isolate_venv)
    if env_extra:
        env.update(env_extra)

    started = datetime.now(UTC)
    try:
        proc = subprocess.run(
            _split_command(command),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        finished = datetime.now(UTC)
        out_s = _coerce_stream(e.stdout)
        err_s = _coerce_stream(e.stderr) + f"\n[timeout after {timeout_s}s]"
        return 124, out_s, err_s, (finished - started).total_seconds()
    except FileNotFoundError as e:
        finished = datetime.now(UTC)
        return 127, "", f"command not found: {e}", (finished - started).total_seconds()

    finished = datetime.now(UTC)
    duration = (finished - started).total_seconds()
    return proc.returncode, proc.stdout or "", proc.stderr or "", duration


def _coerce_stream(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_junit_xml(xml_path: Path) -> list[TestRunEntry]:
    if not xml_path.exists():
        return []
    try:
        tree = ET.parse(xml_path)  # noqa: S314  reports we generated ourselves
    except ET.ParseError:
        return []
    root = tree.getroot()
    out: list[TestRunEntry] = []
    # Handle both <testsuites> and a single <testsuite>.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        file_attr = suite.attrib.get("file") or suite.attrib.get("name") or ""
        for case in suite.findall("testcase"):
            classname = case.attrib.get("classname", "")
            name = case.attrib.get("name", "test")
            file_rel = (
                case.attrib.get("file")
                or file_attr
                or classname.replace(".", "/")
            )
            duration = _safe_float(case.attrib.get("time"))
            failure_el = case.find("failure")
            error_el = case.find("error")
            problem = failure_el if failure_el is not None else error_el
            skipped = case.find("skipped")
            if problem is not None:
                status = "failed" if problem.tag == "failure" else "error"
                text = (problem.text or "").strip()
                first_line = text.splitlines()[0] if text else None
                message = problem.attrib.get("message") or first_line
                traceback = problem.text or None
            elif skipped is not None:
                status = "skipped"
                message = skipped.attrib.get("message")
                traceback = skipped.text
            else:
                status = "passed"
                message = None
                traceback = None
            tid = _normalize_id(file_rel, name)
            out.append(
                TestRunEntry(
                    id=tid,
                    name=name,
                    file=file_rel,
                    status=status,
                    duration_s=duration,
                    message=message,
                    traceback=traceback,
                )
            )
    return out


def parse_playwright_json(stdout: str) -> list[TestRunEntry]:
    """Parse Playwright `--reporter=json` output (printed to stdout)."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    out: list[TestRunEntry] = []

    def walk(suite: dict, file_rel: str = "") -> None:
        nested_file = suite.get("file") or file_rel
        for spec in suite.get("specs", []) or []:
            spec_file = spec.get("file") or nested_file
            title = spec.get("title", "test")
            for test in spec.get("tests", []) or []:
                results = test.get("results", []) or [{}]
                last = results[-1]
                status_raw = last.get("status", "")
                status = {
                    "passed": "passed",
                    "expected": "passed",
                    "failed": "failed",
                    "unexpected": "failed",
                    "timedOut": "failed",
                    "skipped": "skipped",
                    "interrupted": "error",
                }.get(status_raw, "error")
                duration = (last.get("duration") or 0) / 1000.0
                error = last.get("error") or {}
                message = error.get("message")
                traceback = error.get("stack")
                attachments = [
                    {"path": a.get("path", ""), "type": _attachment_type(a.get("name", ""))}
                    for a in (last.get("attachments") or [])
                    if a.get("path")
                ]
                out.append(
                    TestRunEntry(
                        id=_normalize_id(spec_file, title),
                        name=title,
                        file=spec_file,
                        status=status,
                        duration_s=duration,
                        message=message,
                        traceback=traceback,
                        attachments=attachments,
                    )
                )
        for child in suite.get("suites", []) or []:
            walk(child, nested_file)

    for suite in data.get("suites", []) or []:
        walk(suite)
    return out


def parse_jest_json(json_path: Path) -> list[TestRunEntry]:
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[TestRunEntry] = []
    for tr in data.get("testResults", []) or []:
        file_rel = tr.get("name") or tr.get("testFilePath") or ""
        for ar in tr.get("assertionResults", []) or []:
            name = ar.get("fullName") or ar.get("title", "test")
            status_raw = ar.get("status", "")
            status = {"passed": "passed", "failed": "failed", "pending": "skipped",
                      "skipped": "skipped", "todo": "skipped"}.get(status_raw, "error")
            messages = ar.get("failureMessages") or []
            duration = (ar.get("duration") or 0) / 1000.0
            out.append(
                TestRunEntry(
                    id=_normalize_id(file_rel, name),
                    name=name,
                    file=file_rel,
                    status=status,
                    duration_s=duration,
                    message=messages[0].splitlines()[0] if messages else None,
                    traceback="\n".join(messages) or None,
                )
            )
    return out


def parse_mocha_json(json_path: Path) -> list[TestRunEntry]:
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[TestRunEntry] = []

    def emit(items: list, status: str) -> None:
        for it in items or []:
            file_rel = it.get("file", "")
            name = it.get("fullTitle") or it.get("title", "test")
            duration = (it.get("duration") or 0) / 1000.0
            err = it.get("err") or {}
            out.append(
                TestRunEntry(
                    id=_normalize_id(file_rel, name),
                    name=name,
                    file=file_rel,
                    status=status,
                    duration_s=duration,
                    message=err.get("message"),
                    traceback=err.get("stack"),
                )
            )

    emit(data.get("passes"), "passed")
    emit(data.get("failures"), "failed")
    emit(data.get("pending"), "skipped")
    return out


def parse_robot_xml(xml_path: Path) -> list[TestRunEntry]:
    if not xml_path.exists():
        return []
    try:
        tree = ET.parse(xml_path)  # noqa: S314  reports we generated ourselves
    except ET.ParseError:
        return []
    root = tree.getroot()
    out: list[TestRunEntry] = []
    for test in root.iter("test"):
        name = test.attrib.get("name", "test")
        file_rel = ""
        status_el = test.find("status")
        status_raw = (status_el.attrib.get("status") if status_el is not None else "FAIL")
        status = {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}.get(status_raw, "error")
        message = status_el.text if status_el is not None else None
        out.append(
            TestRunEntry(
                id=_normalize_id(file_rel, name),
                name=name,
                file=file_rel,
                status=status,
                message=message,
            )
        )
    return out


def parse_surefire_dir(reports_dir: Path) -> list[TestRunEntry]:
    out: list[TestRunEntry] = []
    if not reports_dir.exists():
        return out
    for xml in sorted(reports_dir.glob("TEST-*.xml")):
        out.extend(parse_junit_xml(xml))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _attachment_type(name: str) -> str:
    name = name.lower()
    if "screenshot" in name or name.endswith((".png", ".jpg", ".jpeg")):
        return "screenshot"
    if "trace" in name or name.endswith(".zip"):
        return "trace"
    if "video" in name or name.endswith((".mp4", ".webm")):
        return "video"
    if name.endswith(".log") or "log" in name:
        return "log"
    return "other"


def _normalize_id(file_rel: str, name: str) -> str:
    base = slugify(f"{Path(file_rel).stem}-{name}") if file_rel else slugify(name)
    return f"T-{base}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_tests(
    framework: str,
    *,
    cwd: Path,
    detected_command: str | None = None,
    timeout_s: int = 1800,
    env_extra: dict[str, str] | None = None,
    profile: StackProfile | None = None,
    headless: bool = True,
) -> RunResult:
    command, parser = resolve_command(
        framework, detected=detected_command, cwd=cwd, profile=profile,
    )

    # The CLI `--headed` flag is meant to give the user a visible browser
    # while the SUT tests run, so they can watch the test execute live.
    # Two complementary mechanisms cover the bulk of real-world SUTs:
    #   1) Set the HEADLESS env var (1/0). Most polyglot SUTs check an env
    #      var of this name in their conftest / playwright.config / similar
    #      to toggle the browser's head state. The AskBosch SUT for example
    #      lists HEADLESS in its `.env.example`.
    #   2) Strip a literal `--headless` argument from the test command when
    #      we want headed. This covers SUTs that bake `--headless` directly
    #      into their pytest / playwright invocation. We never *add*
    #      `--headed` because not all frameworks accept it (and headed is
    #      the default for most when `--headless` is absent).
    runtime_env = dict(env_extra or {})
    runtime_env["HEADLESS"] = "1" if headless else "0"
    if not headless:
        command = _strip_headless_flag(command)

    # Force a clean SUT-specific venv for Python managers that own a venv
    # (poetry, uv, pdm, pipenv). Without this, a worca-t process that was
    # itself launched from a venv (typical for `uv tool install --editable`)
    # leaks `VIRTUAL_ENV` into the child, and the manager reuses worca-t's
    # venv as the SUT's "active" venv whenever the Python version satisfies
    # the SUT's constraint. The symptom: every SUT install command reports
    # "in sync" but pytest fails on SUT-specific imports (e.g. allure-pytest,
    # pydantic-settings) because they were never installed into the borrowed
    # venv. Node managers (npm/yarn/pnpm) and bare-pip flows don't need this
    # — node install targets `node_modules/` and bare-pip is path-prefixed
    # to the SUT's `.venv/bin/pip` upstream.
    isolate_venv = bool(profile and (profile.package_manager or "").lower() in PYTHON_VENV_MANAGERS)
    started = datetime.now(UTC)
    exit_code, stdout, stderr, duration = execute_command(
        command, cwd=cwd, timeout_s=timeout_s, env_extra=runtime_env,
        isolate_venv=isolate_venv,
    )
    finished = datetime.now(UTC)

    results: list[TestRunEntry] = []
    if parser == "junit":
        results = parse_junit_xml(cwd / "worca-junit.xml")
    elif parser == "playwright-json":
        results = parse_playwright_json(stdout)
    elif parser == "jest-json":
        results = parse_jest_json(cwd / "worca-results.json")
    elif parser == "mocha-json":
        results = parse_mocha_json(cwd / "worca-results.json")
    elif parser == "robot-xml":
        results = parse_robot_xml(cwd / "worca-output.xml")
    elif parser == "surefire":
        results = parse_surefire_dir(cwd / "target" / "surefire-reports")

    if not results and exit_code != 0:
        # Synthesise a single 'error' entry so callers see *something*.
        # When the failure is a missing-module / collection error, attach
        # the classifier output so step 9 can skip the self-heal loop —
        # there is no per-test patch site to fix, and the user just needs
        # to be told which dependency to install.
        runner_failure = classify_runner_failure(
            stderr,
            package_manager=profile.package_manager if profile else None,
        )
        msg = f"command exited with code {exit_code}; no results parsed"
        if runner_failure:
            msg += f" — {runner_failure['summary']}; fix: {runner_failure['hint']}"
        results = [
            TestRunEntry(
                id="T-runner-failure",
                name="<runner-failure>",
                file=str(cwd),
                status="error",
                message=msg,
                stdout=stdout[-4000:],
                stderr=stderr[-4000:],
                runner_failure=runner_failure,
            )
        ]

    return RunResult(
        framework=framework,
        command=command,
        cwd=str(cwd),
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_s=duration,
        exit_code=exit_code,
        results=results,
        stdout=stdout,
        stderr=stderr,
    )


_FAIL_PATTERN_FALLBACK = re.compile(
    r"(?im)^\s*(?:FAIL|FAILED|ERROR)\s+(?P<name>[^\s].+?)\s*$"
)


# Patterns that identify "the test runner couldn't even load the test files"
# class of failure — collection / import errors, missing deps, broken conftest.
# When matched, step 9 skips the self-heal loop (no test to patch) and the
# user gets a one-line actionable fix message instead of 10 timed-out heal
# attempts on a synthetic `T-runner-failure` entry.
#
# Order matters: the more specific `ModuleNotFoundError` runs first so its
# captured module name flows into the hint. The generic "ImportError while
# loading conftest" / "collected 0 items / errors during collection"
# fallback only fires when nothing more specific matched.
_MODULE_NOT_FOUND_RE = re.compile(
    r"(?im)^\s*E?\s*(?:ModuleNotFoundError|ImportError)\s*:\s*"
    r"No module named\s+['\"](?P<module>[^'\"]+)['\"]"
)
_CONFTEST_IMPORT_ERROR_RE = re.compile(
    r"(?im)ImportError while loading conftest"
)
_PYTEST_COLLECTION_ERRORS_RE = re.compile(
    r"(?im)^\s*(?:errors during collection|=+\s*ERRORS\s*=+)"
)


# Package-manager install hint by detected package_manager name. Best-effort
# — when the pm is unknown we fall back to a generic pip command and let the
# user adapt.
_INSTALL_HINT_BY_PM = {
    "poetry": "poetry add --group test {module}",
    "uv":     "uv add --dev {module}",
    "pdm":    "pdm add --dev {module}",
    "hatch":  "hatch run pip install {module}",
    "pip":    "pip install {module}",
    "pipenv": "pipenv install --dev {module}",
    "npm":    "npm install --save-dev {module}",
    "yarn":   "yarn add --dev {module}",
    "pnpm":   "pnpm add --save-dev {module}",
    "maven":  "add {module} to pom.xml <dependencies>",
    "gradle": "add {module} to build.gradle dependencies",
}

# Map a Python module name to the install package when the two differ
# (e.g. `import allure` → `allure-pytest`). The full table would be huge —
# we cover the handful of pytest plugins that come up routinely in the QA
# stacks the pipeline generates against. Unknown imports fall back to the
# module name verbatim and the hint says "install <module> (or its providing
# package)".
_PYTEST_PLUGIN_PROVIDERS = {
    "allure":          "allure-pytest",
    "xdist":           "pytest-xdist",
    "pytest_xdist":    "pytest-xdist",
    "pytest_asyncio":  "pytest-asyncio",
    "pytest_bdd":      "pytest-bdd",
    "pytest_html":     "pytest-html",
    "pytest_mock":     "pytest-mock",
    "pytest_cov":      "pytest-cov",
    "pytest_playwright": "pytest-playwright",
    "playwright":      "playwright",
    "selenium":        "selenium",
    "robot":           "robotframework",
    "Browser":         "robotframework-browser",
}


def _install_hint_for(module: str, package_manager: str | None) -> str:
    """Build a one-line install hint for the user. Always returns *something*."""
    pkg = _PYTEST_PLUGIN_PROVIDERS.get(module, module)
    template = _INSTALL_HINT_BY_PM.get(
        (package_manager or "").lower(), "install {module}",
    )
    return template.format(module=pkg)


# Argv-list templates for package managers we'll execute programmatically on
# behalf of the user (Step 9 missing-dep auto-recovery). Returning a list
# (never a shell string) keeps the runner safe from injection via crafted
# package names. Managers that can't be reduced to a single non-shell argv
# (maven/gradle need pom.xml edits; `hatch run pip install` chains commands)
# are deliberately omitted — they remain prose-hint-only via _install_hint_for.
#
# `pip` uses `{venv_bin}/pip` rather than bare `pip` so the install lands in
# the SUT's own venv. Without the path prefix, bare pip would resolve via
# PATH to either worca-t's own venv (when VIRTUAL_ENV is inherited) or the
# system Python (when it's not) — neither matches the env the test runner
# uses. The caller is responsible for supplying `venv_bin`.
_INSTALL_ARGV_BY_PM: dict[str, list[str]] = {
    "poetry": ["poetry", "add", "--group", "test", "{package}"],
    "uv":     ["uv", "add", "--dev", "{package}"],
    "pdm":    ["pdm", "add", "--dev", "{package}"],
    "pip":    ["{venv_bin}/pip", "install", "{package}"],
    "pipenv": ["pipenv", "install", "--dev", "{package}"],
    "npm":    ["npm", "install", "--save-dev", "{package}"],
    "yarn":   ["yarn", "add", "--dev", "{package}"],
    "pnpm":   ["pnpm", "add", "--save-dev", "{package}"],
}


def install_command_for(
    package_manager: str | None,
    package: str,
    *,
    venv_bin: str | None = None,
) -> list[str] | None:
    """Build an argv list to install *package* via *package_manager*.

    `venv_bin` is the SUT's venv binary directory (e.g. ``.venv/bin`` or
    ``.venv\\Scripts``) — required for ``pip`` so the install can target the
    SUT's own venv. Other managers ignore it.

    Returns ``None`` when we have no safe programmatic install path for that
    manager (maven, gradle, hatch, unknown), or when ``pip`` was requested
    without a ``venv_bin`` — caller should fall back to the prose hint.
    """
    pm = (package_manager or "").lower()
    template = _INSTALL_ARGV_BY_PM.get(pm)
    if template is None:
        return None
    if pm == "pip" and not venv_bin:
        return None
    return [arg.format(package=package, venv_bin=venv_bin or "") for arg in template]


def classify_runner_failure(
    stderr: str, *, package_manager: str | None = None,
) -> dict | None:
    """Inspect stderr for collection-/import-time failures.

    Returns a dict describing the failure when one is detected, else None.
    Shape:
        {
            "kind": "missing_module" | "collection_error",
            "module": str | None,        # name of the missing module if known
            "hint":   str,               # human-readable one-line fix command
            "summary": str,              # short headline for the step error
        }

    The kinds:
      - `missing_module` — `ModuleNotFoundError: No module named 'X'` (or the
        older `ImportError: No module named X`). `module` is filled in and
        `hint` is package-manager-aware. This is the common case (pytest
        plugin not installed, e.g. allure-pytest missing from pyproject).
      - `collection_error` — pytest blew up at collection time (broken
        conftest, syntax error in a test file, fixture-resolution failure)
        with no specific missing-module signal. `module` is None; `hint`
        points the user at the stderr tail.
    """
    if not stderr:
        return None

    m = _MODULE_NOT_FOUND_RE.search(stderr)
    if m:
        module = m.group("module")
        return {
            "kind": "missing_module",
            "module": module,
            "hint": _install_hint_for(module, package_manager),
            "summary": f"missing dependency: {module!r}",
        }

    if _CONFTEST_IMPORT_ERROR_RE.search(stderr) or _PYTEST_COLLECTION_ERRORS_RE.search(stderr):
        return {
            "kind": "collection_error",
            "module": None,
            "hint": (
                "fix the conftest / test-collection error reported in stderr "
                "(check for syntax errors, broken fixtures, or import-time "
                "side effects)"
            ),
            "summary": "test collection failed before any test ran",
        }

    return None


def fallback_status_from_stdout(stdout: str) -> dict[str, str]:
    """Last-resort parser if framework output gave us nothing.

    Returns {test_name: 'failed'|'error'} extracted from common
    `FAIL <name>` style lines.
    """
    out: dict[str, str] = {}
    for m in _FAIL_PATTERN_FALLBACK.finditer(stdout):
        out[m.group("name").strip()] = "failed"
    return out


@dataclass
class PrepareResult:
    """Outcome of `prepare_sut`."""

    ran: bool                # did we actually invoke the install command?
    command: str | None      # the command that was run (None if skipped)
    exit_code: int | None    # subprocess return code (None if skipped)
    duration_s: float | None
    stdout: str = ""
    stderr: str = ""
    skip_reason: str | None = None  # set when ran=False

    def ok(self) -> bool:
        return self.exit_code == 0 if self.ran else True


def _run_subprocess_step(
    cmd: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: int,
) -> tuple[int, str, str, float]:
    """Run a single command as a list-form subprocess (never ``shell=True``).

    Returns ``(exit_code, stdout, stderr, duration_s)``.
    """
    started = datetime.now(UTC)
    try:
        proc = subprocess.run(
            _split_command(cmd), cwd=str(cwd), env=env,
            capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as e:
        dur = (datetime.now(UTC) - started).total_seconds()
        return 124, _coerce_stream(e.stdout), _coerce_stream(e.stderr) + f"\n[timeout after {timeout_s}s]", dur
    except FileNotFoundError as e:
        dur = (datetime.now(UTC) - started).total_seconds()
        return 127, "", f"command not found: {e}", dur
    dur = (datetime.now(UTC) - started).total_seconds()
    return proc.returncode, proc.stdout or "", proc.stderr or "", dur


def prepare_sut(
    profile: StackProfile | None,
    *,
    cwd: Path,
    timeout_s: int = 900,
    env_extra: dict[str, str] | None = None,
) -> PrepareResult:
    """Run ``profile.install_command`` in *cwd*. Idempotent at the manager level.

    When ``profile.pre_install_command`` is set (e.g. venv creation for pip
    projects), it runs first; failure there aborts before the install step.

    Returns a ``PrepareResult``.  Caller decides what to do with a non-zero
    exit; test_runner does NOT raise.  When *profile* is ``None`` or has no
    install command, this is a no-op that returns ``ran=False``.
    """
    if profile is None or not profile.install_command:
        return PrepareResult(
            ran=False, command=None, exit_code=None, duration_s=None,
            skip_reason="no install_command on profile",
        )

    # See `run_tests` for the rationale on stripping VIRTUAL_ENV here.
    isolate_venv = (profile.package_manager or "").lower() in PYTHON_VENV_MANAGERS
    env = safe_subprocess_env(isolate_venv=isolate_venv)
    if env_extra:
        env.update(env_extra)

    all_stdout: list[str] = []
    all_stderr: list[str] = []
    total_duration = 0.0
    commands_label = profile.install_command

    # Phase 1: pre_install_command (e.g. venv creation)
    if profile.pre_install_command:
        pre_cmd = profile.pre_install_command
        commands_label = f"{pre_cmd} && {profile.install_command}"
        log.info("prepare_sut.pre_install", command=pre_cmd, cwd=str(cwd))
        rc, out, err, dur = _run_subprocess_step(pre_cmd, cwd=cwd, env=env, timeout_s=timeout_s)
        all_stdout.append(out)
        all_stderr.append(err)
        total_duration += dur
        if rc != 0:
            return PrepareResult(
                ran=True, command=commands_label, exit_code=rc,
                duration_s=total_duration,
                stdout="\n".join(all_stdout), stderr="\n".join(all_stderr),
            )

    # Phase 2: install_command
    cmd = profile.install_command
    log.info("prepare_sut.start", command=cmd, cwd=str(cwd))

    has_shell_chain = "&&" in cmd or "||" in cmd or " ; " in cmd
    if has_shell_chain:
        log.warning(
            "prepare_sut.shell_chain_detected",
            command=cmd,
            hint="install_command contains shell operators; use pre_install_command instead",
        )

    rc, out, err, dur = _run_subprocess_step(cmd, cwd=cwd, env=env, timeout_s=timeout_s)
    all_stdout.append(out)
    all_stderr.append(err)
    total_duration += dur

    log.info(
        "prepare_sut.end",
        command=cmd,
        exit_code=rc,
        duration_s=round(total_duration, 2),
    )
    return PrepareResult(
        ran=True,
        command=commands_label,
        exit_code=rc,
        duration_s=total_duration,
        stdout="\n".join(all_stdout),
        stderr="\n".join(all_stderr),
    )


# ---------------------------------------------------------------------------
# Missing-dep audit — Step 6 emits warnings; Step 9 pre-installs known-safe
# ones and runtime-recovers the rest. Both steps call this for a shared view.
# ---------------------------------------------------------------------------

# Top-level test imports of these names are never flagged as missing. `src`
# and `tests` are common SUT layout conventions; the SUT's own package name
# is added at call time.
_AUDIT_ALWAYS_OK = frozenset({"src", "tests", "test", "conftest"})
_REQ_HEAD_SPLIT = re.compile(r"[<>=!~;\[\s]")
_PKG_NAME_NORM = re.compile(r"[-_]+")


def _norm_pkg(name: str) -> str:
    """PEP 503 normalize for comparing import names to declared dep names."""
    return _PKG_NAME_NORM.sub("-", name.strip().lower())


def _split_req(spec: str) -> str:
    """`requests>=2.30,<3 ; python_version>='3.9'` -> `requests`."""
    head = _REQ_HEAD_SPLIT.split(spec, maxsplit=1)[0]
    return head.strip()


def _python_test_files(sut_root: Path) -> list[Path]:
    tests_dir = sut_root / "tests"
    if not tests_dir.is_dir():
        return []
    out: list[Path] = []
    for path in tests_dir.rglob("*.py"):
        # Skip dotfile dirs *inside* the tests tree (.venv, .tox, .pytest_cache),
        # NOT dotfile parents on the way to it (e.g. workspaces under ~/.worca-t).
        rel_parts = path.relative_to(tests_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        out.append(path)
    return out


def _top_level_imports(py_file: Path) -> set[str]:
    """Top-level absolute imports only. Relative `from . import x` is skipped."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top:
                    names.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if not node.module:
                continue
            top = node.module.split(".", 1)[0]
            if top:
                names.add(top)
    return names


def _declared_python_deps(sut_root: Path) -> set[str]:
    """Union of all declared deps across pyproject.toml + requirements*.txt.

    Best-effort — names are PEP 503 normalized. Returns an empty set when
    nothing parseable is found (so audit conservatively flags everything,
    relegated to ``confidence == "guessed"``).
    """
    declared: set[str] = set()

    pyproject = sut_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        # PEP 621
        for spec in (data.get("project", {}).get("dependencies") or []):
            declared.add(_norm_pkg(_split_req(spec)))
        opt = data.get("project", {}).get("optional-dependencies") or {}
        for specs in opt.values():
            for spec in specs:
                declared.add(_norm_pkg(_split_req(spec)))
        # Poetry
        poetry = data.get("tool", {}).get("poetry", {})
        for name in (poetry.get("dependencies") or {}):
            if name.lower() == "python":
                continue
            declared.add(_norm_pkg(name))
        for grp in (poetry.get("group") or {}).values():
            for name in (grp.get("dependencies") or {}):
                declared.add(_norm_pkg(name))
        # PDM legacy dev-dependencies table
        for specs in (data.get("tool", {}).get("pdm", {}).get("dev-dependencies") or {}).values():
            for spec in specs:
                declared.add(_norm_pkg(_split_req(spec)))

    for req in sut_root.glob("requirements*.txt"):
        try:
            for line in req.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                declared.add(_norm_pkg(_split_req(line)))
        except OSError:
            continue

    declared.discard("")
    return declared


def _sut_own_package_name(sut_root: Path) -> str | None:
    pyproject = sut_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return (
        data.get("project", {}).get("name")
        or data.get("tool", {}).get("poetry", {}).get("name")
    )


def audit_missing_deps(
    sut_root: Path, *, package_manager: str | None = None
) -> list[dict]:
    """Find top-level test imports that aren't in declared deps.

    Returns one dict per missing import, with:
      - ``module``: the imported top-level name
      - ``suggested_package``: pip-installable name (from _PYTEST_PLUGIN_PROVIDERS
        when mapped; else the module name verbatim)
      - ``source_file``: relative path of the first test file importing it
      - ``confidence``: ``"known"`` when the module is in the curated mapping
        table (high-confidence: Step 9 will auto-install). ``"guessed"`` for
        everything else (Step 9 will HITL-prompt or warn).
      - ``suggested_install``: prose hint from :func:`_install_hint_for`.

    Best-effort and conservative: unparsable files are skipped, dynamic
    imports are missed.
    """
    test_files = _python_test_files(sut_root)
    if not test_files:
        return []

    declared = _declared_python_deps(sut_root)
    own_pkg = _sut_own_package_name(sut_root)
    skip_top = set(_AUDIT_ALWAYS_OK)
    if own_pkg:
        # Imports may use either the kebab project name or its snake_case form.
        skip_top.add(own_pkg)
        skip_top.add(_norm_pkg(own_pkg).replace("-", "_"))
    stdlib = getattr(sys, "stdlib_module_names", frozenset())

    # First-seen wins so we can point the user at the file that needs the dep.
    seen: dict[str, Path] = {}
    for tf in test_files:
        for name in _top_level_imports(tf):
            if name in skip_top or name in stdlib:
                continue
            norm = _norm_pkg(name)
            mapped = _PYTEST_PLUGIN_PROVIDERS.get(name)
            mapped_norm = _norm_pkg(mapped) if mapped else None
            if norm in declared or (mapped_norm and mapped_norm in declared):
                continue
            seen.setdefault(name, tf)

    warnings: list[dict] = []
    for module, source_file in sorted(seen.items()):
        suggested = _PYTEST_PLUGIN_PROVIDERS.get(module, module)
        warnings.append({
            "module": module,
            "suggested_package": suggested,
            "source_file": str(source_file.relative_to(sut_root)).replace("\\", "/"),
            "confidence": "known" if module in _PYTEST_PLUGIN_PROVIDERS else "guessed",
            "suggested_install": _install_hint_for(module, package_manager),
        })
    return warnings
