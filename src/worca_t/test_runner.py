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
so the run-results.json stays a faithful join with tests-with-tbd.json.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from worca_t.logging_setup import get_logger
from worca_t.md_parser import slugify
from worca_t.proxy import with_proxy_env

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
) -> tuple[str, str]:
    """Pick command + parser id. Prefer detected command from Step 6 when given."""
    if detected:
        parser = _DEFAULT_COMMANDS.get(framework, ("", "auto"))[1]
        if parser == "junit" and "--junitxml" not in detected:
            detected = f"{detected} --junitxml={(cwd / 'worca-junit.xml').as_posix()}"
        return detected, parser
    if framework in _DEFAULT_COMMANDS:
        template, parser = _DEFAULT_COMMANDS[framework]
        return _expand_command(template, cwd), parser
    return _expand_command("pytest --junitxml={junit}", cwd), "junit"


def _expand_command(template: str, cwd: Path) -> str:
    return template.format(
        junit=str((cwd / "worca-junit.xml").as_posix()),
        json_out=str((cwd / "worca-results.json").as_posix()),
        robot_xml=str((cwd / "worca-output.xml").as_posix()),
    )


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
) -> tuple[int, str, str, float]:
    env = dict(os.environ)
    env.update(with_proxy_env())
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
) -> RunResult:
    command, parser = resolve_command(framework, detected=detected_command, cwd=cwd)
    started = datetime.now(UTC)
    exit_code, stdout, stderr, duration = execute_command(
        command, cwd=cwd, timeout_s=timeout_s, env_extra=env_extra
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
        results = [
            TestRunEntry(
                id="T-runner-failure",
                name="<runner-failure>",
                file=str(cwd),
                status="error",
                message=f"command exited with code {exit_code}; no results parsed",
                stdout=stdout[-4000:],
                stderr=stderr[-4000:],
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


def fallback_status_from_stdout(stdout: str) -> dict[str, str]:
    """Last-resort parser if framework output gave us nothing.

    Returns {test_name: 'failed'|'error'} extracted from common
    `FAIL <name>` style lines.
    """
    out: dict[str, str] = {}
    for m in _FAIL_PATTERN_FALLBACK.finditer(stdout):
        out[m.group("name").strip()] = "failed"
    return out
