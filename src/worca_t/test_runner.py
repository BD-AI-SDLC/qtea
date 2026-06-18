"""Framework-agnostic test runner used by Step 8.

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
import platform
import re
import shlex
import shutil
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
    # step 8 to skip the self-heal loop — there's no test to patch — and
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
        """Split real test entries from synthetic infrastructure-failure entries.

        Entries with `runner_failure` set (pytest collection ImportError,
        xdist worker crash, missing module, etc.) are infra, not tests.
        Counting them as `tests` masked the run 20260611-184450 incident
        where `tests=2 errors=2` looked like two tests ran when really
        zero did — both entries were synthetic collection-error artifacts.
        """
        out = {
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "infrastructure_errors": 0,
        }
        for r in self.results:
            if r.runner_failure is not None:
                out["infrastructure_errors"] += 1
                continue
            out["tests"] += 1
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


_PYTEST_FRAMEWORKS = frozenset({"pytest", "playwright-py", "selenium-py"})


_KNOWN_RUNNER_TOKENS = frozenset({
    "pytest", "python", "python3", "py",
    "poetry", "pipenv", "uv", "pdm", "hatch", "rye", "nox", "tox",
    "npx", "npm", "yarn", "pnpm", "bun", "deno", "node",
    "mvn", "mvnw", "gradle", "gradlew",
    "robot", "make", "task", "just",
    "sh", "bash", "cmd", "powershell", "pwsh",
})


def _looks_like_test_command(s: str) -> bool:
    """True if `s` plausibly starts with a runner invocation.

    The researcher agent occasionally writes a test *name* (or a markdown
    snippet) into `research.commands.test` instead of the actual run command.
    Without this guard we feed that garbage straight to a subprocess and
    Windows raises `[WinError 2]` / exit 127 — a confusing failure mode that
    looks like a test failure but is really a code bug.
    """
    if not s or not s.strip():
        return False
    head = s.strip().split(None, 1)[0]
    # Reduce an absolute or relative path to its final component so an
    # interpreter referenced by full path — e.g. `sys.executable`
    # (`C:\...\python.exe`) or POSIX `/usr/bin/python3` — is recognized by
    # its basename rather than rejected as an unknown token. Splitting on
    # both separators also subsumes the old `./` / `.\` prefix stripping.
    head = re.split(r"[\\/]", head)[-1]
    # Strip Windows .exe / .bat suffix and any trailing punctuation.
    head = re.sub(r"\.(exe|bat|cmd|ps1)$", "", head, flags=re.IGNORECASE)
    return head.lower() in _KNOWN_RUNNER_TOKENS


def _inject_pytest_marker(command: str, marker_filter: str | None) -> str:
    """Append `-m "<filter>"` to a pytest-family command.

    Idempotent: if the command already has `-m` it's left alone so an
    explicit selector in `detected` (e.g. from a researcher-extracted
    Makefile target) wins over our default attribution filter.
    """
    if not marker_filter:
        return command
    if re.search(r"(?:^|\s)-m(?:\s|=)", command):
        return command
    return f"{command} -m \"{marker_filter}\""


def _inject_strict_markers(command: str) -> str:
    """Append --strict-markers to a pytest command if not already present.

    Without this, unregistered marker names in -m expressions silently
    evaluate to False (fail-open), causing the wrong test set to be
    collected when the marker registration plugin hasn't loaded.
    """
    if "--strict-markers" in command:
        return command
    return f"{command} --strict-markers"


def _inject_xdist_override(
    command: str,
    parallelism: int = 0,
    cwd: Path | None = None,
) -> str:
    """Control xdist worker count for worca-t test runs.

    xdist worker subprocesses crash under worca-t's subprocess
    environment (ResolverServer socket handle inheritance, captured
    stdin/stdout pipes, env sanitization). ``-n 0`` keeps the xdist
    plugin loaded (so the SUT's ``addopts -n 5`` doesn't become an
    unrecognized flag) but runs tests in-process — no worker
    subprocess, no crash.

    When the caller passes ``parallelism > 0``, xdist is kept active
    with that many workers (``-n <value>``). ``parallelism == 0``
    (default) appends ``-n 0`` to run in-process.
    """
    n_flag = f"-n {parallelism}" if parallelism > 0 else "-n 0"
    return f"{command} {n_flag}"


def resolve_command(
    framework: str,
    *,
    detected: str | None,
    cwd: Path,
    profile: StackProfile | None = None,
    marker_filter: str | None = None,
    parallelism: int = 0,
) -> tuple[str, str]:
    """Pick command + parser id.

    Resolution order (first non-empty wins):
      1. `detected` — the command the researcher agent extracted from
         README / CI / pyproject. Used verbatim *if it passes
         `_looks_like_test_command`*; we only append the junit flag when
         the parser expects it and the command doesn't already have one.
         The researcher is expected to include the package-manager wrapper
         itself (e.g. ``poetry run pytest …``).
      2. Framework default from `_DEFAULT_COMMANDS`, wrapped with the
         package-manager prefix from `profile` (e.g. ``pytest …`` becomes
         ``poetry run pytest …``). This is the load-bearing fallback when
         the researcher fails to extract a runnable command OR when the
         extracted command fails the runner-token sanity check.
      3. Bare ``pytest --junitxml=…`` as the universal fallback.

    `marker_filter` (pytest-family frameworks only): appended as
    ``-m "<filter>"`` so callers can scope a run to a subset of tests
    by marker. Step 8 uses this to select worca-generated tests
    (e.g. ``worca_smoke or worca_regression``) and exclude the SUT's
    native suite. No-op on non-pytest frameworks (Cypress / Jest /
    Playwright-TS) and when the command already carries an explicit
    ``-m`` selector.

    `parallelism`: when > 0, overrides the SUT's xdist ``-n`` with this
    value. When 0 (default), uses ``-n auto`` to let xdist adapt to the
    machine's CPU availability. Only applied to pytest-family frameworks.
    """
    apply_marker = framework in _PYTEST_FRAMEWORKS
    if detected and _looks_like_test_command(detected):
        parser = _DEFAULT_COMMANDS.get(framework, ("", "auto"))[1]
        if parser == "junit" and "--junitxml" not in detected:
            detected = f"{detected} --junitxml={(cwd / 'worca-junit.xml').as_posix()}"
        if apply_marker:
            detected = _inject_pytest_marker(detected, marker_filter)
            detected = _inject_strict_markers(detected)
            detected = _inject_xdist_override(detected, parallelism)
        return detected, parser
    if detected:
        log.warning(
            "test_runner.detected_command_rejected",
            detected=detected[:200],
            reason="does not start with a known runner token "
                   "(pytest / poetry / npx / mvn / ...) — falling back to "
                   "framework default. Likely a researcher hallucination.",
        )
    if framework in _DEFAULT_COMMANDS:
        template, parser = _DEFAULT_COMMANDS[framework]
        bare = _expand_command(template, cwd)
        wrapped = wrap_command(profile, bare)
        if apply_marker:
            wrapped = _inject_pytest_marker(wrapped, marker_filter)
            wrapped = _inject_strict_markers(wrapped)
            wrapped = _inject_xdist_override(wrapped, parallelism)
        return wrapped, parser
    bare = _expand_command("pytest --junitxml={junit}", cwd)
    wrapped = wrap_command(profile, bare)
    if apply_marker:
        wrapped = _inject_pytest_marker(wrapped, marker_filter)
        wrapped = _inject_strict_markers(wrapped)
        wrapped = _inject_xdist_override(wrapped, parallelism)
    return wrapped, "junit"


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


_PYTEST_HEADLESS_OPTION_RE = re.compile(
    r"""addoption\s*\(\s*['"]--headless['"]""",
)


def _sut_registers_headless_option(cwd: Path) -> bool:
    """Detect whether the SUT's conftest registers a ``--headless`` CLI option.

    Many polyglot pytest-playwright SUTs ship a custom ``--headless`` opt-in
    flag (default headed) — the parent worca-t run must inject ``--headless``
    on the pytest command for the SUT browser fixture to launch headless,
    because the ``HEADLESS`` env var alone is not what those conftests read.

    pytest-playwright itself does NOT register ``--headless`` (it defaults
    to headless and exposes ``--headed`` for opt-in). Blindly adding the
    flag for such a SUT causes pytest to abort with "unrecognized arguments",
    so we gate the injection on a literal ``addoption("--headless"...)`` in
    the SUT's conftest at the root or under ``tests/``.
    """
    for candidate in (cwd / "conftest.py", cwd / "tests" / "conftest.py"):
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")
        except (FileNotFoundError, OSError, IsADirectoryError):
            continue
        if _PYTEST_HEADLESS_OPTION_RE.search(content):
            return True
    return False


def _inject_headless_flag(command: str) -> str:
    """Append ``--headless`` to a pytest command if not already present."""
    if re.search(r"(?:^|\s)--headless(?:\s|=|$)", command):
        return command
    return f"{command} --headless"


# ---------------------------------------------------------------------------
# Subprocess wrapper
# ---------------------------------------------------------------------------


def _split_command(command: str) -> list[str]:
    if os.name == "nt":
        # On Windows, posix=False is required to keep backslash paths intact
        # (e.g. ".venv\\Scripts\\pytest"). The trade-off is that posix=False
        # preserves literal double quotes around quoted args — so `-m "a or b"`
        # would arrive at the subprocess as the value `"a or b"` (with quotes),
        # which pytest can't parse as a marker expression and silently treats
        # as "match all tests". Strip surrounding double quotes from each
        # token after splitting to get the actual value.
        tokens = shlex.split(command, posix=False)
        return [
            t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t
            for t in tokens
        ]
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
    marker_filter: str | None = None,
    parallelism: int = 0,
) -> RunResult:
    command, parser = resolve_command(
        framework, detected=detected_command, cwd=cwd, profile=profile,
        marker_filter=marker_filter, parallelism=parallelism,
    )
    log.info(
        "test_runner.resolved_command",
        command=command,
        parser=parser,
        cwd=str(cwd),
        framework=framework,
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
    elif framework in _PYTEST_FRAMEWORKS and _sut_registers_headless_option(cwd):
        # SUT has its own --headless opt-in (default headed). Inject the
        # flag so worca-t's headless default actually reaches the browser
        # fixture — the HEADLESS env var alone isn't read by such SUTs.
        command = _inject_headless_flag(command)

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

    # Exit code 3 = pytest internal error. When partial JUnit results
    # parse but none represent actual test executions (all infrastructure
    # errors like xdist worker crashes or conftest failures), treat the
    # same as "no results" — synthesise T-runner-failure so the
    # pipeline classifies the run as "failed" instead of "warned".
    if results and exit_code == 3:
        real_tests = [
            r for r in results
            if r.status in ("passed", "failed", "skipped")
        ]
        if not real_tests:
            runner_failure = classify_runner_failure(
                stderr,
                package_manager=profile.package_manager if profile else None,
            ) or {
                "kind": "internal_error",
                "module": None,
                "hint": "pytest exited with code 3 (internal error)",
                "summary": "pytest internal error; no tests executed",
            }
            for r in results:
                r.runner_failure = runner_failure
            results.append(
                TestRunEntry(
                    id="T-runner-failure",
                    name="<runner-failure>",
                    file=str(cwd),
                    status="error",
                    message=(
                        f"pytest internal error (exit_code=3); "
                        f"no tests passed/failed"
                    ),
                    stdout=stdout[-4000:],
                    stderr=stderr[-4000:],
                    runner_failure=runner_failure,
                )
            )

    if not results and exit_code != 0:
        # Synthesise a single 'error' entry so callers see *something*.
        # When the failure is a missing-module / collection error, attach
        # the classifier output so step 8 can skip the self-heal loop —
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
# When matched, step 8 skips the self-heal loop (no test to patch) and the
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
# behalf of the user (Step 8 missing-dep auto-recovery). Returning a list
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


def _detect_stale_venv_scripts(venv_dir: Path, env: dict[str, str]) -> bool:
    """Return True when the venv's script wrappers point to a different Python
    than the venv's own python executable.

    On Windows, Scripts/*.exe wrappers have the Python interpreter path baked
    in at venv-creation time. When a .venv directory is committed to git or
    copied from another checkout (a common pattern in monorepos), those
    wrappers still resolve to the original Python while python.exe itself is
    correct. The result: running pytest.exe or pip.exe operates on the old
    environment — the project package is invisible, producing
    ``ModuleNotFoundError`` at collection time even though
    ``python.exe -c "import <pkg>"`` succeeds.

    Detection: compare the sys.prefix that ``python.exe`` reports with the
    site-packages path that ``pip.exe --version`` resolves to. A mismatch
    means the wrappers are stale and the venv needs rebuilding.
    """
    if platform.system() == "Windows":
        python_exe = venv_dir / "Scripts" / "python.exe"
        pip_script = venv_dir / "Scripts" / "pip.exe"
    else:
        python_exe = venv_dir / "bin" / "python"
        pip_script = venv_dir / "bin" / "pip"

    if not python_exe.is_file() or not pip_script.is_file():
        return False

    try:
        res = subprocess.run(
            [str(python_exe), "-c", "import sys; print(sys.prefix)"],
            capture_output=True, text=True, timeout=15, env=env,
        )
        if res.returncode != 0:
            return False
        python_prefix = Path(res.stdout.strip()).resolve()
    except Exception:  # noqa: BLE001
        return False

    try:
        res = subprocess.run(
            [str(pip_script), "--version"],
            capture_output=True, text=True, timeout=15, env=env,
        )
        if res.returncode != 0:
            return False
        # pip --version: "pip 25.x from /path/to/site-packages/pip (python 3.x)"
        m = re.search(r"from (.+?) \(python", res.stdout)
        if not m:
            return False
        pip_prefix = Path(m.group(1)).resolve()
        try:
            pip_prefix.relative_to(python_prefix)
            return False  # pip's site-packages are under the same prefix — healthy
        except ValueError:
            return True  # pip resolves to a different prefix — stale wrappers
    except Exception:  # noqa: BLE001
        return False


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

    # Post-install venv health check. Only for Python venv managers with a
    # known venv_path (poetry, uv, pdm, pipenv). Detects the stale-scripts
    # problem that arises when .venv is committed to git or copied from
    # another checkout: Scripts/*.exe wrappers still point to the original
    # Python, causing ModuleNotFoundError even though python.exe is correct.
    if rc == 0 and profile.venv_path and isolate_venv:
        venv_dir = cwd / profile.venv_path
        if venv_dir.is_dir() and _detect_stale_venv_scripts(venv_dir, env):
            log.warning(
                "prepare_sut.stale_venv_detected",
                venv_dir=str(venv_dir),
                hint="Scripts wrappers resolve to a different Python than venv/python — rebuilding",
            )
            try:
                shutil.rmtree(venv_dir)
            except OSError as e:
                log.warning("prepare_sut.stale_venv_remove_failed", error=str(e))
            else:
                log.info("prepare_sut.stale_venv_removed", venv_dir=str(venv_dir))
                rebuild_label = f"[stale-venv rebuild] {commands_label}"
                rb_stdout: list[str] = []
                rb_stderr: list[str] = []
                rebuild_ok = True
                if profile.pre_install_command:
                    rc2, o2, e2, d2 = _run_subprocess_step(
                        profile.pre_install_command, cwd=cwd, env=env, timeout_s=timeout_s,
                    )
                    rb_stdout.append(o2)
                    rb_stderr.append(e2)
                    total_duration += d2
                    if rc2 != 0:
                        log.warning("prepare_sut.stale_venv_rebuild_pre_failed", exit_code=rc2)
                        rebuild_ok = False
                        rc = rc2
                if rebuild_ok:
                    rc3, o3, e3, d3 = _run_subprocess_step(
                        profile.install_command, cwd=cwd, env=env, timeout_s=timeout_s,
                    )
                    rb_stdout.append(o3)
                    rb_stderr.append(e3)
                    total_duration += d3
                    rc = rc3
                    if rc3 == 0:
                        log.info("prepare_sut.stale_venv_rebuild_ok", venv_dir=str(venv_dir))
                    else:
                        log.warning("prepare_sut.stale_venv_rebuild_failed", exit_code=rc3)
                all_stdout.append(f"# [stale-venv rebuild]\n{chr(10).join(rb_stdout)}")
                all_stderr.append("\n".join(rb_stderr))
                commands_label = rebuild_label

    return PrepareResult(
        ran=True,
        command=commands_label,
        exit_code=rc,
        duration_s=total_duration,
        stdout="\n".join(all_stdout),
        stderr="\n".join(all_stderr),
    )


# ---------------------------------------------------------------------------
# Missing-dep audit — Step 6 emits warnings; Step 8 pre-installs known-safe
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


def _python_prod_files(sut_root: Path) -> list[Path]:
    """The SUT's own production-source `.py` files.

    Resolves the SUT's own package directory via `_sut_own_package_name`,
    preferring the PEP 517 `src/` layout over the flat layout. Returns
    `[]` when the project name is unknown or neither layout directory
    exists. The walk uses the same dotfile-skip discipline as
    `_python_test_files` so transient caches inside the package tree
    (`__pycache__` is already excluded by the `*.py` glob; `.mypy_cache`,
    `.ruff_cache`, etc. inside the package would be skipped here).
    """
    own_pkg = _sut_own_package_name(sut_root)
    if not own_pkg:
        return []
    snake_pkg = _norm_pkg(own_pkg).replace("-", "_")
    for base in (sut_root / "src" / snake_pkg, sut_root / snake_pkg):
        if base.is_dir():
            pkg_dir = base
            break
    else:
        return []
    out: list[Path] = []
    for path in pkg_dir.rglob("*.py"):
        rel_parts = path.relative_to(pkg_dir).parts
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
    """Find top-level imports that aren't in declared deps.

    Scans both the SUT's ``tests/`` tree AND its own production source
    package (resolved via :func:`_sut_own_package_name`, preferring the
    ``src/`` layout). The prod-side walk catches deps reached transitively
    through the SUT's source — pytest collection fails the same way on a
    missing dep whether the test file imports it directly or via a
    ``conftest.py`` → ``src/<pkg>/...`` chain.

    Returns one dict per missing import, with:
      - ``module``: the imported top-level name
      - ``suggested_package``: pip-installable name (from _PYTEST_PLUGIN_PROVIDERS
        when mapped; else the module name verbatim)
      - ``source_file``: relative path of the first file importing it
        (test files are scanned first so they win ties — the operator's
        mental entry point is the test, not the prod module)
      - ``confidence``: ``"known"`` when the module is in the curated mapping
        table (high-confidence: Step 8 will auto-install). ``"guessed"`` for
        everything else (Step 8 will HITL-prompt or warn).
      - ``suggested_install``: prose hint from :func:`_install_hint_for`.

    Best-effort and conservative: unparsable files are skipped, dynamic
    imports are missed. Returns ``[]`` when there is no ``tests/`` dir at
    all — Step 8 has nothing to run in that case.
    """
    test_files = _python_test_files(sut_root)
    if not test_files:
        return []
    prod_files = _python_prod_files(sut_root)

    declared = _declared_python_deps(sut_root)
    own_pkg = _sut_own_package_name(sut_root)
    skip_top = set(_AUDIT_ALWAYS_OK)
    if own_pkg:
        # Imports may use either the kebab project name or its snake_case form.
        skip_top.add(own_pkg)
        skip_top.add(_norm_pkg(own_pkg).replace("-", "_"))
    stdlib = getattr(sys, "stdlib_module_names", frozenset())

    # First-seen wins so we can point the user at the file that needs the dep.
    # Iterate test files first so they win ties (operator's mental entry point).
    seen: dict[str, Path] = {}
    for tf in (*test_files, *prod_files):
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
