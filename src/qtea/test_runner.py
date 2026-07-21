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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.md_parser import slugify
from qtea.proxy import safe_subprocess_env
from qtea.stack_profile import PYTHON_VENV_MANAGERS, StackProfile, wrap_command

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
        # Cypress uses Mocha's `json` reporter, which writes to STDOUT unless
        # `--reporter-options output=<path>` is given. The `mocha-json` parser
        # reads `qtea-results.json` from disk, so without the output option a
        # green suite yields an unread stdout blob -> zero parsed results ->
        # false-green. Wire the reporter to the file the parser reads.
        "npx cypress run --reporter json --reporter-options output={json_out}",
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

_PW_TEST_FRAMEWORKS = frozenset({"playwright-ts", "playwright-js"})


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
    parallelism: int = 2,
    cwd: Path | None = None,
) -> str:
    """Control xdist worker count for qtea test runs.

    ``parallelism > 0`` pins to that many workers (``-n <value>``).
    ``parallelism == 0`` appends ``-n 0`` to run in-process (no worker
    subprocess).
    ``parallelism == -1`` uses ``-n auto`` (one worker per logical CPU core).
    """
    if re.search(r"(?:^|\s)-n(?:\s|=)", command):
        return command
    if parallelism == -1:
        n_flag = "-n auto"
    elif parallelism > 0:
        n_flag = f"-n {parallelism}"
    else:
        n_flag = "-n 0"
    return f"{command} {n_flag}"


def _inject_playwright_file_filter(command: str) -> str:
    """Scope a Playwright Test command to qtea-generated test files only.

    Playwright Test treats positional args as regex patterns matched against
    file paths. ``"qtea_"`` selects all files whose path contains the
    qtea-generated prefix, mirroring pytest's ``-m "qtea_*"`` scoping.
    """
    if "qtea_" in command:
        return command
    return f'{command} "qtea_"'


def _inject_playwright_workers(command: str, parallelism: int) -> str:
    """Set Playwright Test ``--workers`` count."""
    import os as _os

    if "--workers" in command:
        return command
    if parallelism == -1:
        workers = _os.cpu_count() or 4
        return f"{command} --workers {workers}"
    if parallelism > 0:
        return f"{command} --workers {parallelism}"
    return command


def _inject_playwright_project(command: str, project: str | None) -> str:
    """Pin a Playwright Test command to a single ``--project``.

    qtea Step 9 must run the SUT's tests on exactly one browser (chromium
    first, firefox second) — never both. A SUT ``playwright.config.ts`` that
    defines multiple projects (e.g. ``chromium`` + ``firefox``) otherwise runs
    every project, doubling the run and opening two browser windows.

    Idempotent: if the command already carries an explicit ``--project`` (e.g.
    a researcher-detected command that already selects one), it is left alone.
    No-op when ``project`` is falsy (no project could be resolved).
    """
    if not project:
        return command
    if re.search(r"(?:^|\s)--project(?:\s|=)", command):
        return command
    return f"{command} --project={project}"


def _inject_playwright_reporter_json(command: str) -> str:
    """Ensure a Playwright Test command emits the JSON reporter to stdout.

    ``parse_playwright_json`` reads ``--reporter=json`` output from stdout. A
    researcher-detected command (e.g. a bare ``npx playwright test`` lifted
    from a README) otherwise runs with Playwright's default human-readable
    ``list`` reporter, which the JSON parser cannot decode -> zero results ->
    a passing suite is silently misreported as ``all_passed`` (false-green).
    Injecting ``--reporter=json`` (only when no explicit ``--reporter`` is
    present) makes the detected path match the framework default. A CLI
    ``--reporter`` overrides any reporter set in playwright.config, so this is
    safe. Pairs with the universal zero-parsed-results guard in ``run_tests``.
    """
    if re.search(r"(?:^|\s)--reporter(?:=|\s)", command):
        return command
    return f"{command} --reporter=json"


def _inject_mocha_json_output(command: str, cwd: Path) -> str:
    """Ensure a Mocha/Cypress ``json``-reporter command writes to the file the
    ``mocha-json`` parser reads (``qtea-results.json``).

    Mocha's ``json`` reporter writes to STDOUT unless
    ``--reporter-options output=<path>`` is given, and ``parse_mocha_json``
    reads a file. Without this a detected green suite yields an unread stdout
    blob -> zero results -> false-green. Injects the ``json`` reporter when no
    reporter is set and the output path when absent.
    """
    if not re.search(r"(?:^|\s)--reporter(?:=|\s)", command):
        command = f"{command} --reporter json"
    if "output=" not in command:
        out = (cwd / "qtea-results.json").as_posix()
        command = f"{command} --reporter-options output={out}"
    return command


def _inject_headed_flag(command: str) -> str:
    """Append ``--headed`` to a Playwright Test command.

    Playwright Test defaults to headless; ``--headed`` overrides it to show
    the browser window (e.g. when the user passes ``qtea run --headed``).
    """
    if "--headed" in command:
        return command
    return f"{command} --headed"


def resolve_command(
    framework: str,
    *,
    detected: str | None,
    cwd: Path,
    profile: StackProfile | None = None,
    marker_filter: str | None = None,
    parallelism: int = 2,
    playwright_project: str | None = None,
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
    by marker. Step 8 uses this to select qtea-generated tests
    (e.g. ``qtea_smoke or qtea_regression``) and exclude the SUT's
    native suite. No-op when the command already carries an explicit
    ``-m`` selector.

    For Playwright Test frameworks (``playwright-ts``, ``playwright-js``),
    scoping is file-based: a ``"qtea_"`` positional arg is appended so
    only files with the qtea prefix are matched.

    `parallelism`: number of parallel workers. Applies uniformly:
    pytest gets ``-n <value>`` (xdist), Playwright Test gets
    ``--workers <value>``. ``0`` runs pytest in-process (``-n 0``)
    and is a no-op for Playwright Test.

    `playwright_project` (Playwright Test frameworks only): pins the run to a
    single ``--project=<name>`` so Step 9 runs on exactly one browser
    (chromium first, firefox second) instead of every project the SUT config
    defines. No-op when ``None`` or when the command already selects a project.
    """
    apply_marker = framework in _PYTEST_FRAMEWORKS
    apply_pw_filter = framework in _PW_TEST_FRAMEWORKS
    if detected and _looks_like_test_command(detected):
        parser = _DEFAULT_COMMANDS.get(framework, ("", "unsupported"))[1]
        if parser == "junit" and "--junitxml" not in detected:
            detected = f"{detected} --junitxml={(cwd / 'qtea-junit.xml').as_posix()}"
        if apply_marker:
            detected = _inject_pytest_marker(detected, marker_filter)
            detected = _inject_strict_markers(detected)
            detected = _inject_xdist_override(detected, parallelism)
        elif apply_pw_filter:
            # Force the JSON reporter BEFORE the file filter/workers so a
            # detected `npx playwright test` can't run with the default human
            # reporter (unparseable stdout -> false-green). See finding 12.
            detected = _inject_playwright_reporter_json(detected)
            detected = _inject_playwright_file_filter(detected)
            detected = _inject_playwright_workers(detected, parallelism)
            detected = _inject_playwright_project(detected, playwright_project)
        if parser == "mocha-json":
            # Cypress/Mocha detected commands need the json reporter wired to
            # the file the parser reads, or a green suite reports zero results.
            detected = _inject_mocha_json_output(detected, cwd)
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
        elif apply_pw_filter:
            wrapped = _inject_playwright_file_filter(wrapped)
            wrapped = _inject_playwright_workers(wrapped, parallelism)
            wrapped = _inject_playwright_project(wrapped, playwright_project)
        return wrapped, parser
    # No default known for this framework and no usable detected command.
    # Fail loud: silently defaulting to `pytest` for e.g. a Java or Ruby
    # stack would run the wrong runner and report "0 tests, all passed" —
    # the exact class of stealth failure this branch exists to prevent.
    # `run_tests` short-circuits on an empty command and synthesises a
    # `T-runner-failure` entry so the operator sees the failure surface.
    log.error(
        "test_runner.unsupported_framework",
        framework=framework,
        supported=sorted(_DEFAULT_COMMANDS.keys()),
        hint=(
            f"No default test command registered for framework={framework!r}. "
            f"Either add an entry to _DEFAULT_COMMANDS or have the researcher "
            f"agent supply a detected_command that starts with a known runner."
        ),
    )
    return "", "unsupported"


def _expand_command(template: str, cwd: Path) -> str:
    return template.format(
        junit=str((cwd / "qtea-junit.xml").as_posix()),
        json_out=str((cwd / "qtea-results.json").as_posix()),
        robot_xml=str((cwd / "qtea-output.xml").as_posix()),
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
    flag (default headed) — the parent qtea run must inject ``--headless``
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
        tokens = [
            t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t
            for t in tokens
        ]
        # Resolve the executable via shutil.which() so .cmd/.bat wrappers
        # (npx.cmd, yarn.cmd, etc.) are found. Without this, subprocess.run
        # with list-form args on Windows cannot locate .cmd files — only
        # .exe/.com are searched by CreateProcessW.
        if tokens:
            resolved = shutil.which(tokens[0])
            if resolved:
                tokens[0] = resolved
        return tokens
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
    playwright_project: str | None = None,
) -> RunResult:
    command, parser = resolve_command(
        framework, detected=detected_command, cwd=cwd, profile=profile,
        marker_filter=marker_filter, parallelism=parallelism,
        playwright_project=playwright_project,
    )
    log.info(
        "test_runner.resolved_command",
        command=command,
        parser=parser,
        cwd=str(cwd),
        framework=framework,
    )
    # Short-circuit when resolve_command signalled unsupported framework.
    # Skips execute_command (which would blow up on an empty command with a
    # cryptic OSError) and surfaces a clean, actionable T-runner-failure
    # entry the operator can act on.
    if not command or parser == "unsupported":
        now = datetime.now(UTC).isoformat()
        return RunResult(
            framework=framework,
            command=command,
            cwd=str(cwd),
            started_at=now,
            finished_at=now,
            duration_s=0.0,
            exit_code=127,
            results=[TestRunEntry(
                id="T-runner-failure",
                name="<unsupported-framework>",
                file=str(cwd),
                status="error",
                message=(
                    f"No test runner registered for framework={framework!r}. "
                    f"Supported: {sorted(_DEFAULT_COMMANDS.keys())}. "
                    f"Provide a detected_command or add an entry to "
                    f"_DEFAULT_COMMANDS."
                ),
                runner_failure={
                    "kind": "unsupported_framework",
                    "module": None,
                    "hint": (
                        f"Add {framework!r} to test_runner._DEFAULT_COMMANDS "
                        f"or supply a runnable command via detected_command."
                    ),
                    "summary": f"unsupported framework: {framework!r}",
                },
            )],
            stdout="",
            stderr="",
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
    #      into their pytest / playwright invocation.
    #   3) For Playwright Test (playwright-ts/js), inject `--headed` when
    #      the user wants a visible browser — PW Test defaults to headless,
    #      so removing `--headless` alone isn't sufficient.
    runtime_env = dict(env_extra or {})
    runtime_env["HEADLESS"] = "1" if headless else "0"
    if not headless:
        command = _strip_headless_flag(command)
        if framework in _PW_TEST_FRAMEWORKS:
            command = _inject_headed_flag(command)
    elif framework in _PYTEST_FRAMEWORKS and _sut_registers_headless_option(cwd):
        # SUT has its own --headless opt-in (default headed). Inject the
        # flag so qtea's headless default actually reaches the browser
        # fixture — the HEADLESS env var alone isn't read by such SUTs.
        command = _inject_headless_flag(command)

    # Force a clean SUT-specific venv for Python managers that own a venv
    # (poetry, uv, pdm, pipenv). Without this, a qtea process that was
    # itself launched from a venv (typical for `uv tool install --editable`)
    # leaks `VIRTUAL_ENV` into the child, and the manager reuses qtea's
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
    _KNOWN_PARSERS = {
        "junit", "playwright-json", "jest-json",
        "mocha-json", "robot-xml", "surefire",
    }
    if parser == "junit":
        results = parse_junit_xml(cwd / "qtea-junit.xml")
    elif parser == "playwright-json":
        results = parse_playwright_json(stdout)
    elif parser == "jest-json":
        results = parse_jest_json(cwd / "qtea-results.json")
    elif parser == "mocha-json":
        results = parse_mocha_json(cwd / "qtea-results.json")
    elif parser == "robot-xml":
        results = parse_robot_xml(cwd / "qtea-output.xml")
    elif parser == "surefire":
        results = parse_surefire_dir(cwd / "target" / "surefire-reports")
    elif parser not in _KNOWN_PARSERS:
        # Fail loud: silent empty results would look like "0 tests, all
        # passed" to downstream. Emit a visible synthetic failure entry
        # so the operator sees the actual problem.
        log.error(
            "test_runner.unknown_parser",
            parser=parser,
            framework=framework,
            known=sorted(_KNOWN_PARSERS),
        )
        results = [TestRunEntry(
            id="T-runner-failure",
            name="<unknown-parser>",
            file=str(cwd),
            status="error",
            message=(
                f"No parser registered for parser={parser!r}. "
                f"Supported: {sorted(_KNOWN_PARSERS)}. "
                f"The test command ran (exit_code={exit_code}) but its "
                f"output was NOT parsed — treat this as a failed run."
            ),
            stdout=stdout[-4000:],
            stderr=stderr[-4000:],
            runner_failure={
                "kind": "unknown_parser",
                "module": None,
                "hint": (
                    f"Register a parser for {parser!r} in run_tests() "
                    f"or map framework={framework!r} to a supported parser "
                    f"in test_runner._DEFAULT_COMMANDS."
                ),
                "summary": f"unknown parser: {parser!r}",
            },
        )]

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
                stdout=stdout,
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
                        "pytest internal error (exit_code=3); "
                        "no tests passed/failed"
                    ),
                    stdout=stdout[-4000:],
                    stderr=stderr[-4000:],
                    runner_failure=runner_failure,
                )
            )

    if not results:
        # Zero parsed test results is ALWAYS a failure for qtea. We generated
        # the tests, so a run that yields nothing parseable either never
        # executed them or emitted output the parser could not read. This guard
        # fires on exit_code == 0 too — the critical false-green class: a
        # "passing" suite whose results were unparseable (Playwright JSON
        # polluted by stdout, a Cypress/Mocha reporter writing to the wrong
        # sink, a detected command with the wrong reporter) must NOT be
        # reported as all_passed. Synthesise a single visible 'error' entry so
        # downstream classifies the run as failed / infrastructure_error rather
        # than a silent green. When the failure is a missing-module /
        # collection error, attach the classifier output so step 8 can skip the
        # self-heal loop — there is no per-test patch site to fix.
        runner_failure = classify_runner_failure(
            stderr,
            package_manager=profile.package_manager if profile else None,
            stdout=stdout,
        )
        if exit_code == 0:
            msg = (
                "command exited 0 but produced ZERO parseable test results — "
                "treating as a FAILED run (a passing suite with no readable "
                "results is a false-green: likely a reporter/output "
                "misconfiguration or a suite that executed nothing)"
            )
            if runner_failure is None:
                runner_failure = {
                    "kind": "no_results_exit_zero",
                    "module": None,
                    "hint": (
                        f"framework={framework!r} parser={parser!r} exited 0 but "
                        "wrote no parseable results; verify the test command's "
                        "reporter writes JSON/JUnit to the path qtea reads "
                        "(--reporter=json to stdout for Playwright; "
                        "--outputFile / --reporter-options output= for the "
                        "file-based parsers)"
                    ),
                    "summary": "zero parseable results despite exit 0",
                }
        else:
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

# Node/TS equivalents of the Python module-not-found signal above. Node's
# runtime resolver (`Cannot find module 'X'`) and tsc's compile-time
# diagnostic (`error TS2307: Cannot find module 'X'`) both name the module —
# a relative specifier (`./foo`, `../foo`) means a broken *local* import
# (e.g. a bad runtime-import path, our H1/H2 incident class) which is a
# `collection_error`, not a missing dependency; a bare package name (`foo`,
# `@scope/foo`) means an uninstalled npm package, which is `missing_module`.
_JS_MODULE_NOT_FOUND_RE = re.compile(
    r"(?im)Cannot find module\s+['\"](?P<module>[^'\"]+)['\"]"
)
_TS_MODULE_NOT_FOUND_RE = re.compile(
    r"(?im)error\s+TS2307\s*:\s*Cannot find module\s+['\"](?P<module>[^'\"]+)['\"]"
)
# Generic tsc/Jest/Vitest/Cypress compile-or-collection failure with no
# specific missing-module signal (syntax error, type error, broken fixture
# import, etc.) — the JS/TS analogue of `_CONFTEST_IMPORT_ERROR_RE` /
# `_PYTEST_COLLECTION_ERRORS_RE` above.
_TS_JS_COLLECTION_ERROR_RE = re.compile(
    r"(?im)(?:error\s+TS\d+\s*:"
    r"|^\s*SyntaxError\s*:"
    r"|Test suite failed to run"
    r"|Transform failed with)"
)

_MISSING_ENV_RE = re.compile(
    r"Missing required environment variables?\s*:?\s*"
    r"(?P<body>(?:(?:\s|\\n)*-?\s*[A-Z][A-Z0-9_]+(?:\s|\\n)*)+)",
    re.I,
)
_ENVVAR_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,80}")


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
# PATH to either qtea's own venv (when VIRTUAL_ENV is inherited) or the
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
    stderr: str,
    *,
    package_manager: str | None = None,
    stdout: str = "",
) -> dict | None:
    """Inspect stderr (and stdout) for collection-/import-time failures.

    Returns a dict describing the failure when one is detected, else None.
    Shape:
        {
            "kind": "missing_module" | "missing_env" | "collection_error",
            "module": str | None,        # name of the missing module if known
            "hint":   str,               # human-readable one-line fix command
            "summary": str,              # short headline for the step error
            "vars":   list[str] | None,  # missing env var names (missing_env only)
        }

    The kinds:
      - `missing_module` — `ModuleNotFoundError: No module named 'X'` (or the
        older `ImportError: No module named X`). `module` is filled in and
        `hint` is package-manager-aware. This is the common case (pytest
        plugin not installed, e.g. allure-pytest missing from pyproject).
      - `missing_env` — the test runner (typically a globalSetup guard)
        reported missing environment variables. `vars` lists the names.
      - `collection_error` — pytest blew up at collection time (broken
        conftest, syntax error in a test file, fixture-resolution failure)
        with no specific missing-module signal. `module` is None; `hint`
        points the user at the stderr tail.
    """
    combined = (stderr or "") + "\n" + (stdout or "")
    if not combined.strip():
        return None

    m = _MODULE_NOT_FOUND_RE.search(combined)
    if m:
        module = m.group("module")
        return {
            "kind": "missing_module",
            "module": module,
            "hint": _install_hint_for(module, package_manager),
            "summary": f"missing dependency: {module!r}",
        }

    m = _TS_MODULE_NOT_FOUND_RE.search(combined) or _JS_MODULE_NOT_FOUND_RE.search(combined)
    if m:
        module = m.group("module")
        if module.startswith("."):
            return {
                "kind": "collection_error",
                "module": module,
                "hint": (
                    f"fix the broken local import path {module!r} — the "
                    "referenced file does not exist relative to the "
                    "importing file (check for a nested-directory path bug)"
                ),
                "summary": f"broken local import: {module!r} could not be resolved",
            }
        return {
            "kind": "missing_module",
            "module": module,
            "hint": _install_hint_for(module, package_manager),
            "summary": f"missing dependency: {module!r}",
        }

    m = _MISSING_ENV_RE.search(combined)
    if m:
        body = m.group("body")
        var_names = _ENVVAR_NAME_RE.findall(body)
        return {
            "kind": "missing_env",
            "module": None,
            "vars": var_names,
            "hint": (
                f"provide {', '.join(var_names)} via .env, host environment, "
                f"or Azure DevOps Variable Groups"
                if var_names
                else "provide the missing environment variables"
            ),
            "summary": f"missing environment variables: {', '.join(var_names)}",
        }

    if (
        _CONFTEST_IMPORT_ERROR_RE.search(combined)
        or _PYTEST_COLLECTION_ERRORS_RE.search(combined)
        or _TS_JS_COLLECTION_ERROR_RE.search(combined)
    ):
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
        return (
            124, _coerce_stream(e.stdout),
            _coerce_stream(e.stderr) + f"\n[timeout after {timeout_s}s]", dur,
        )
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
            check=False,
        )
        if res.returncode != 0:
            return False
        python_prefix = Path(res.stdout.strip()).resolve()
    except Exception:
        return False

    try:
        res = subprocess.run(
            [str(pip_script), "--version"],
            capture_output=True, text=True, timeout=15, env=env,
            check=False,
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
    except Exception:
        return False


def _rmtree_robust(path: Path) -> None:
    """``shutil.rmtree`` with Windows read-only + transient-lock handling.

    Two failure modes are common when wiping a Python venv on Windows:

      1. Read-only files (typical for pip-installed dist-info / RECORD
         entries): handled inline via ``chmod(S_IWRITE) + retry`` in the
         ``onerror`` callback, mirroring ``s06_research._rmtree_safe``.

      2. ``.pyd`` extension modules still loaded by a subprocess that has
         only just exited: Windows briefly keeps the DLL handle alive
         after the process is gone (``[WinError 5] Access is denied``).
         A short backoff-and-retry resolves it without escalating to the
         caller. Three attempts at 0.3s / 0.8s / 1.6s cover the common
         case (pytest worker shutting down). A persistent lock (IDE, AV
         scanner) will still fail — the caller decides how to surface that.

    Raises ``OSError`` on terminal failure so the caller can log + skip
    the venv rebuild rather than continue with a half-deleted directory.
    """
    import stat
    import time

    def _on_error(func, target, _exc_info):
        try:
            Path(target).chmod(stat.S_IWRITE)
            func(target)
        except Exception:
            # Re-raise so the outer retry loop catches transient locks.
            raise

    last_err: OSError | None = None
    for attempt, delay in enumerate((0.3, 0.8, 1.6)):
        try:
            shutil.rmtree(path, onerror=_on_error)
            return
        except OSError as e:
            last_err = e
            if attempt < 2:
                time.sleep(delay)
    if last_err is not None:
        raise last_err


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
                _rmtree_robust(venv_dir)
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


# Playwright frameworks that need browser binaries installed after the package
# install. Kept in sync with the set Step 9 bootstrap keys off.
_PW_FRAMEWORKS = frozenset(
    {"playwright-py", "playwright-ts", "playwright-js", "playwright-java"}
)

# Marker file (workspace-root relative) recording the install signature of a
# successful pre-Step-7 env prewarm. Step 9's bootstrap reads it to skip a
# redundant re-install of an already-prepared environment on its first attempt.
_ENV_PREP_MARKER_NAME = ".qtea-env-prep-sig"


@dataclass
class SutEnvResult:
    """Outcome of :func:`prepare_sut_env` — full SUT test-env preparation."""

    ok: bool
    ran_install: bool
    stack_profile: StackProfile | None
    error: str | None = None


def _sut_env_present(cwd: Path, framework: str | None) -> bool:
    """Whether the SUT already has a usable test env despite a failed install.

    Node: ``node_modules/playwright`` or ``@playwright/test`` present. Python:
    a ``.venv`` directory present. Used to let the best-effort env prewarm
    proceed on a committed / prior-run environment when a strict ``npm ci``
    fails on a drifted lockfile.
    """
    if (framework or "") in {"playwright-ts", "playwright-js"}:
        return (
            (cwd / "node_modules" / "playwright").is_dir()
            or (cwd / "node_modules" / "@playwright" / "test").is_dir()
        )
    # Python (playwright-py) and unknown frameworks: a venv is the usable signal.
    return (cwd / ".venv").is_dir()


def prepare_sut_env(
    profile: StackProfile | None,
    *,
    cwd: Path,
    framework: str | None,
    install_log_path: Path | None = None,
    timeout_s: int = 900,
) -> SutEnvResult:
    """Install SUT deps, activate the venv, and install Playwright browsers.

    Single source of truth for "make the SUT test environment usable". Shared
    by the pipeline's pre-Step-7 prewarm (so ``qtea auth-capture`` can drive
    the SUT's own sign-in helper at Step 7) and Step 9's bootstrap. Every phase
    is idempotent at the tool level (``poetry install`` / ``npm ci`` /
    ``playwright install`` all no-op or restore-from-cache when satisfied), so
    a warm second call in the same run is cheap. Never raises.

    Returns a :class:`SutEnvResult` carrying the (possibly venv-swapped) profile
    so callers that go on to run tests use the venv bin directory directly
    instead of the slower package-manager wrapper.
    """
    # Phase 1: package install (poetry install / npm ci / ...).
    prep = prepare_sut(profile, cwd=cwd, timeout_s=timeout_s)
    if install_log_path is not None and prep.ran:
        try:
            install_log_path.write_text(
                f"$ {prep.command}\n\n# STDOUT\n{prep.stdout}\n\n"
                f"# STDERR\n{prep.stderr}\n",
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("prepare_sut_env.log_write_failed", error=str(e))
    if not prep.ok():
        # A strict install command (`npm ci`) fails on a drifted lockfile even
        # when a usable environment is already present (committed / prior run).
        # For the best-effort auth-prewarm caller, proceed on the existing env
        # rather than blocking — the install failure is non-fatal if we can
        # still drive the SUT's own code. Otherwise report failure.
        if _sut_env_present(cwd, framework):
            log.warning(
                "prepare_sut_env.install_failed_but_env_present",
                command=prep.command, exit_code=prep.exit_code,
                hint="using the existing SUT env; sync the lockfile to silence this",
            )
        else:
            return SutEnvResult(
                ok=False, ran_install=prep.ran, stack_profile=profile,
                error=f"install failed: `{prep.command}` exited {prep.exit_code}",
            )

    # Phase 2: venv detection + wrapper swap. After prepare_sut created .venv,
    # invoke subsequent commands via its bin dir directly (equivalent to
    # activating the venv) — bypasses poetry's slower venv resolution.
    swapped = profile
    if profile and profile.venv_path:
        venv_abs = cwd / profile.venv_path
        if venv_abs.exists():
            bin_dir = str(venv_abs / ("Scripts" if os.name == "nt" else "bin"))
            swapped = replace(
                profile, wrapper_prefix=bin_dir, package_manager="pip",
            )

    # Phase 3: Playwright browser binaries. Idempotent — skips if present.
    if swapped and (framework or "") in _PW_FRAMEWORKS:
        pw_cmd = wrap_command(swapped, "playwright install chromium")
        rc, out, err, _dur = execute_command(
            pw_cmd, cwd=cwd, timeout_s=400,
            isolate_venv=(swapped.package_manager or "").lower()
            in PYTHON_VENV_MANAGERS,
        )
        if install_log_path is not None:
            try:
                with install_log_path.open("a", encoding="utf-8") as f:
                    f.write(
                        f"\n$ {pw_cmd}\n# exit_code: {rc}\n"
                        f"# STDOUT\n{out}\n\n# STDERR\n{err}\n"
                    )
            except OSError as e:
                log.warning("prepare_sut_env.pw_log_write_failed", error=str(e))
        if rc != 0:
            # Non-fatal: browsers may already be present, or a later step can
            # still function. Mirror Step 9's warn-and-continue.
            log.warning(
                "prepare_sut_env.playwright_install_failed",
                exit_code=rc, stderr=err[:300],
            )

    return SutEnvResult(ok=True, ran_install=prep.ran, stack_profile=swapped)


def write_env_prep_marker(workspace_root: Path, install_sig: str | None) -> None:
    """Record a successful pre-Step-7 env prewarm's install signature.

    Best-effort — a write failure just means Step 9 re-runs the (idempotent)
    install instead of fast-pathing. Never raises.
    """
    if not install_sig:
        return
    try:
        (Path(workspace_root) / _ENV_PREP_MARKER_NAME).write_text(
            install_sig, encoding="utf-8",
        )
    except OSError as e:
        log.warning("prepare_sut_env.marker_write_failed", error=str(e))


def read_env_prep_marker(workspace_root: Path) -> str | None:
    """Read the install signature written by a prior :func:`prepare_sut_env`
    prewarm, or ``None`` when absent/unreadable."""
    try:
        p = Path(workspace_root) / _ENV_PREP_MARKER_NAME
        return p.read_text(encoding="utf-8").strip() if p.is_file() else None
    except OSError:
        return None


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
        # NOT dotfile parents on the way to it (e.g. workspaces under ~/.qtea).
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
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "-")):
                    continue
                declared.add(_norm_pkg(_split_req(stripped)))
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
