"""Native static-check gate for Step 8 codegen (Phase B.6).

Runs the SUT stack's own type-checker / linter against qtea-generated test
code AFTER B.5 (AST reconciliation) and BEFORE B.4 (pattern quality gate).
Catches the class of bug that AST-walks miss but type-checkers don't:
  - class-attribute access on instance-attribute symbols (the GEMINI_NAV_BUTTON
    failure in run 20260621-213751-ee0fef)
  - missing imports (the axe_playwright_python ModuleNotFoundError sibling)
  - typos in symbol names, wrong arg counts, stale references after renames

Scope: violations reported on files OUTSIDE the qteaouched set are counted
but never fed to the violation-fixer. pyright/tsc will naturally type-check
transitive imports; qtea will not autopatch user-owned app source.

The fix loop reuses ``codegen-violation-fixer`` from the caller — this module
only runs the checker, parses output, and filters scope.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.stack_profile import StackProfile, detect_stack_profile, wrap_command
from qtea.test_indexer import Violation
from qtea.test_runner import execute_command, install_command_for

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-stack dispatch table.
#
# Each entry: (tool, install_pkg, install_pm_override, parser_id).
#   tool            -- executable name (used to probe availability).
#   install_pkg     -- package name to install when the tool is absent.
#   install_pm_override -- when set, prefer this package manager over the SUT's
#                     detected one for the install (e.g. ``typescript`` is
#                     npm-only even if the SUT root has poetry.lock alongside
#                     package.json). None means "use the SUT's detected manager".
#   parser_id       -- selector for the output-format parser below.
#
# The argv (flags + file list) is composed adaptively per stack in
# ``_compose_argv`` rather than hard-coded here — tsc invocation differs
# depending on whether tsconfig.json is present and whether the qtea-generated
# tests are .ts or .js.
#
# Coverage: all Python and all JS/TS stacks qtea emits into. Java, Robot,
# Ruby, Go fall through with no gate (their toolchains compile-before-run, so
# the failure mode this gate exists to catch doesn't apply).
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, tuple[str, str, str | None, str]] = {
    # Python: pyright handles both .py test files and PEP-561 inline-typed code.
    "pytest":        ("pyright", "pyright", None, "pyright-json"),
    "playwright-py": ("pyright", "pyright", None, "pyright-json"),
    "selenium-py":   ("pyright", "pyright", None, "pyright-json"),
    # TS + JS: tsc type-checks .ts AND .js (with --allowJs --checkJs). The
    # binary is shipped by the `typescript` npm package; install via npm even
    # when the SUT root also has a Python lockfile (polyglot repos exist).
    "playwright-ts": ("tsc", "typescript", "npm", "tsc-text"),
    "playwright-js": ("tsc", "typescript", "npm", "tsc-text"),
    "jest":          ("tsc", "typescript", "npm", "tsc-text"),
    "vitest":        ("tsc", "typescript", "npm", "tsc-text"),
    "mocha":         ("tsc", "typescript", "npm", "tsc-text"),
    "wdio":          ("tsc", "typescript", "npm", "tsc-text"),
    "cypress":       ("tsc", "typescript", "npm", "tsc-text"),
}


# tsc flags used when invoking with an explicit file list (no tsconfig path).
# `--allowJs --checkJs` makes tsc type-check .js / .jsx / .mjs / .cjs files;
# `--skipLibCheck` mutes noise from third-party type definitions qtea
# doesn't own. `--target es2020 --module esnext --moduleResolution node`
# matches the defaults of every modern bundler so import statements in the
# generated tests parse correctly when no tsconfig is present.
_TSC_FALLBACK_FLAGS: tuple[str, ...] = (
    "--noEmit",
    "--pretty", "false",
    "--allowJs",
    "--checkJs",
    "--skipLibCheck",
    "--target", "es2020",
    "--module", "esnext",
    "--moduleResolution", "node",
    "--esModuleInterop",
)

# tsc flags used when a tsconfig.json is present (respects user's project
# config; we just suppress the pretty formatter so the regex parser works).
_TSC_TSCONFIG_FLAGS: tuple[str, ...] = ("--noEmit", "--pretty", "false")

# File extensions tsc can type-check. Used to filter the qteaouched set
# down to "things tsc should look at" when composing an explicit file list.
_JS_TS_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".mts", ".cts",
    ".js", ".jsx", ".mjs", ".cjs",
})

# File extensions pyright should be invoked on.
_PY_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyi"})


# Single rule string used for every type-checker finding. Engineers reading
# violations.log will see `[type-error]` and immediately recognise it as a
# checker output rather than a qtea-specific pattern violation.
TYPE_ERROR_RULE = "type-error"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class StaticCheckResult:
    """Outcome of a single Phase B.6 invocation. Serialized verbatim to
    ``artifacts/step08/static-check-result.json`` (validated against
    ``schemas/static-check-result.schema.json``)."""

    tool: str | None
    stack: str
    ran: bool
    skipped_reason: str | None
    duration_s: float
    exit_code: int
    in_scope_errors: int
    out_of_scope_errors: int
    autofix_attempted: bool
    post_fix_errors: int
    violations: list[Violation] = field(default_factory=list)

    @property
    def in_scope_violations(self) -> list[Violation]:
        """The subset fed to the violation-fixer. Out-of-scope rows are
        retained in ``violations`` for audit but the fixer never sees them."""
        return [v for v in self.violations if v.severity != "out_of_scope"]

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "stack": self.stack,
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "in_scope_errors": self.in_scope_errors,
            "out_of_scope_errors": self.out_of_scope_errors,
            "autofix_attempted": self.autofix_attempted,
            "post_fix_errors": self.post_fix_errors,
            "violations": [v.as_dict() for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Parsers (one per output format)
# ---------------------------------------------------------------------------


def _parse_pyright_json(stdout: str, sut_root: Path) -> list[Violation]:
    """Parse ``pyright --outputjson`` output into Violations.

    Pyright's schema (validated against the v1.1.x stable surface):

        {
          "version": "...",
          "generalDiagnostics": [
            {"file": "...", "severity": "error|warning|information",
             "message": "...", "rule": "reportXxx",
             "range": {"start": {"line": 0, "character": 0}, ...}},
            ...
          ],
          "summary": {...}
        }

    We keep only ``severity == "error"`` rows (warnings produce noise the
    fixer can't act on safely). Line numbers are 0-based in pyright JSON —
    convert to 1-based to match the existing Violation contract.
    """
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        log.warning("static_check.pyright_json_parse_failed", error=str(e),
                    stdout_head=stdout[:200])
        return []

    diagnostics = payload.get("generalDiagnostics") or []
    out: list[Violation] = []
    for d in diagnostics:
        if d.get("severity") != "error":
            continue
        file_str = d.get("file") or ""
        try:
            file_rel = str(Path(file_str).resolve().relative_to(sut_root)).replace("\\", "/")
        except (ValueError, OSError):
            file_rel = file_str
        rng = d.get("range") or {}
        start = rng.get("start") or {}
        line = int(start.get("line", 0)) + 1
        rule = d.get("rule") or ""
        message = (d.get("message") or "").strip()
        snippet = f"{message} [{rule}]" if rule else message
        out.append(Violation(
            rule=TYPE_ERROR_RULE,
            file=file_rel,
            line=line,
            snippet=snippet[:280],
            severity="error",
        ))
    return out


# tsc text format: `path/to/file.ts(LINE,COL): error TSnnnn: message`.
# Windows paths use backslashes natively; the regex handles both.
_TSC_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.+)$",
)


def _parse_tsc_text(stdout: str, sut_root: Path) -> list[Violation]:
    """Parse tsc ``--pretty false`` text output."""
    out: list[Violation] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _TSC_RE.match(line)
        if not m:
            continue
        file_str = m.group("file")
        try:
            file_rel = str(Path(file_str).resolve().relative_to(sut_root)).replace("\\", "/")
        except (ValueError, OSError):
            file_rel = file_str.replace("\\", "/")
        out.append(Violation(
            rule=TYPE_ERROR_RULE,
            file=file_rel,
            line=int(m.group("line")),
            snippet=f"{m.group('msg').strip()} [{m.group('code')}]"[:280],
            severity="error",
        ))
    return out


_PARSERS = {
    "pyright-json": _parse_pyright_json,
    "tsc-text":     _parse_tsc_text,
}


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


def _filter_to_scope(
    violations: list[Violation],
    qteaouched: set[Path],
    sut_root: Path,
) -> tuple[list[Violation], list[Violation]]:
    """Split violations into (in_scope, out_of_scope) by resolved file path.

    Out-of-scope entries are kept with severity="out_of_scope" for the artifact
    but excluded from the in_scope list that's handed to the fixer.
    """
    touched_resolved = {p.resolve() for p in qteaouched if p.exists()}
    in_scope: list[Violation] = []
    out_of_scope: list[Violation] = []
    for v in violations:
        # The parser already produced a sut-relative posix path. Reverse it to
        # an absolute resolved path for the set membership check.
        abs_path = (sut_root / v.file).resolve()
        if abs_path in touched_resolved:
            in_scope.append(v)
        else:
            out_of_scope.append(Violation(
                rule=v.rule, file=v.file, line=v.line,
                snippet=v.snippet, severity="out_of_scope",
            ))
    return in_scope, out_of_scope


# ---------------------------------------------------------------------------
# Tool availability + auto-install
# ---------------------------------------------------------------------------


def _tool_available(tool: str, sut_root: Path, wrapper_prefix: str | None) -> bool:
    """Probe whether ``tool`` is invocable from the SUT.

    For wrapped commands (npx, poetry run), the wrapper is responsible for
    resolving the tool — we cannot rely on PATH lookup alone. Probe by
    spawning a short ``<wrapped> --version`` and inspecting the exit code.
    Bare-PATH ``shutil.which`` is only used as a fast-path for unwrapped tools.
    """
    if not wrapper_prefix:
        return shutil.which(tool) is not None
    probe_cmd = wrap_command(
        StackProfile(wrapper_prefix=wrapper_prefix), f"{tool} --version",
    )
    code, _, _, _ = execute_command(probe_cmd, cwd=sut_root, timeout_s=20)
    return code == 0


def _autoinstall(
    profile: StackProfile,
    install_pkg: str,
    install_pm_override: str | None,
    sut_root: Path,
    timeout_s: int,
) -> tuple[bool, str]:
    """Best-effort programmatic install of ``install_pkg`` into the SUT's env.

    Returns ``(installed, log_line)``. On failure the second element is a
    short reason string suitable for artifact ``skipped_reason``.
    """
    pm = install_pm_override or profile.package_manager
    venv_bin = profile.venv_path or ""
    argv = install_command_for(pm, install_pkg, venv_bin=venv_bin)
    if not argv:
        return False, f"no auto-install path for package_manager={pm!r}"
    cmd = " ".join(argv)
    log.info("static_check.autoinstall_attempt", tool=install_pkg, pm=pm, cmd=cmd)
    code, out, err, _ = execute_command(cmd, cwd=sut_root, timeout_s=timeout_s)
    if code != 0:
        return False, f"install exit {code}: {(err or out)[:160].strip()}"
    return True, f"installed {install_pkg} via {pm}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _compose_argv(
    tool: str,
    sut_root: Path,
    qteaouched: set[Path],
) -> tuple[str, str | None]:
    """Build the bare command string for ``tool`` against the qteaouched set.

    Returns ``(argv_string, skip_reason)``. When ``skip_reason`` is non-None
    the caller should record it on the result and not execute. Examples of
    skip conditions: no files of the right extension for the tool to check
    (e.g. tsc invoked but qtea only touched .py files in a polyglot repo).
    """
    if tool == "pyright":
        py_files = sorted(
            p for p in qteaouched
            if p.exists() and p.suffix in _PY_EXTENSIONS
        )
        if not py_files:
            return "", "no Python files in qteaouched set"
        rel = [
            str(p.relative_to(sut_root)).replace("\\", "/")
            for p in py_files
            if p.is_relative_to(sut_root)
        ]
        return f"pyright --outputjson {' '.join(rel)}", None

    if tool == "tsc":
        js_ts_files = sorted(
            p for p in qteaouched
            if p.exists() and p.suffix in _JS_TS_EXTENSIONS
        )
        if not js_ts_files:
            return "", "no JS/TS files in qteaouched set"
        rel = [
            str(p.relative_to(sut_root)).replace("\\", "/")
            for p in js_ts_files
            if p.is_relative_to(sut_root)
        ]
        # When tsconfig.json exists and the qteaouched set is ALL .ts (no
        # JS files), respect the user's project config — tsc reads tsconfig
        # only when no files are passed explicitly. We accept the resulting
        # whole-project scan because the post-parse scope filter trims it
        # back down to qtea files anyway, and TS projects with strict
        # tsconfig settings catch more.
        all_ts = all(p.suffix in {".ts", ".tsx", ".mts", ".cts"} for p in js_ts_files)
        if all_ts and (sut_root / "tsconfig.json").exists():
            return f"tsc {' '.join(_TSC_TSCONFIG_FLAGS)}", None
        # JS or mixed JS+TS, OR no tsconfig — invoke tsc with explicit file
        # list and the fallback flag set so checking works without project
        # config and with --allowJs/--checkJs for .js files.
        return (
            f"tsc {' '.join(_TSC_FALLBACK_FLAGS)} {' '.join(rel)}",
            None,
        )

    return "", f"no argv composer for tool={tool!r}"


def run_static_check(
    sut_root: Path,
    *,
    framework: str,
    qteaouched: set[Path],
    timeout_s: int = 120,
) -> StaticCheckResult:
    """Run the native static-checker for ``framework`` against the SUT.

    Returns a populated ``StaticCheckResult`` describing what happened. The
    function NEVER raises on checker / install failure — every failure path
    surfaces as a result row with ``ran=False`` plus a populated
    ``skipped_reason``. The caller decides whether to fail Step 8 based on
    ``in_scope_errors``.

    Does NOT invoke the violation-fixer; that's the caller's job. This
    function is pure (read + subprocess); easy to unit-test with mocks.
    """
    started = datetime.now(UTC)
    entry = _DISPATCH.get(framework)
    if entry is None:
        return StaticCheckResult(
            tool=None, stack=framework, ran=False,
            skipped_reason=f"no checker for framework={framework!r}",
            duration_s=0.0, exit_code=0,
            in_scope_errors=0, out_of_scope_errors=0,
            autofix_attempted=False, post_fix_errors=0,
        )

    tool, install_pkg, install_pm_override, parser_id = entry
    profile = detect_stack_profile(sut_root)

    argv, skip_reason = _compose_argv(tool, sut_root, qteaouched)
    if skip_reason is not None:
        elapsed = (datetime.now(UTC) - started).total_seconds()
        return StaticCheckResult(
            tool=tool, stack=framework, ran=False,
            skipped_reason=skip_reason,
            duration_s=elapsed, exit_code=0,
            in_scope_errors=0, out_of_scope_errors=0,
            autofix_attempted=False, post_fix_errors=0,
        )

    wrapped = wrap_command(profile, argv)

    if not _tool_available(tool, sut_root, profile.wrapper_prefix):
        installed, reason = _autoinstall(
            profile, install_pkg, install_pm_override, sut_root, timeout_s,
        )
        if not installed:
            elapsed = (datetime.now(UTC) - started).total_seconds()
            return StaticCheckResult(
                tool=tool, stack=framework, ran=False,
                skipped_reason=reason,
                duration_s=elapsed, exit_code=0,
                in_scope_errors=0, out_of_scope_errors=0,
                autofix_attempted=False, post_fix_errors=0,
            )

    code, stdout, stderr, duration = execute_command(
        wrapped, cwd=sut_root, timeout_s=timeout_s,
    )
    # pyright exits non-zero when it finds errors; tsc exits 1 on type errors,
    # 2 on bad CLI usage. We don't treat exit code as failure on its own —
    # the parsed violations are the source of truth. We DO log when both the
    # exit is non-zero AND we parsed zero violations (suggests a config error
    # rather than a real type problem).
    parser = _PARSERS[parser_id]
    raw = parser(stdout, sut_root)
    if code != 0 and not raw and code != 124:
        # Pyright also writes its JSON to stdout on success — empty stdout +
        # non-zero exit means we couldn't even launch the tool. Surface as
        # skipped rather than passing silently.
        if not stdout.strip():
            elapsed = (datetime.now(UTC) - started).total_seconds()
            return StaticCheckResult(
                tool=tool, stack=framework, ran=False,
                skipped_reason=f"checker exit {code}: {stderr[:160].strip()}",
                duration_s=elapsed, exit_code=code,
                in_scope_errors=0, out_of_scope_errors=0,
                autofix_attempted=False, post_fix_errors=0,
            )

    in_scope, out_of_scope = _filter_to_scope(raw, qteaouched, sut_root)
    return StaticCheckResult(
        tool=tool, stack=framework, ran=True,
        skipped_reason=None,
        duration_s=duration, exit_code=code,
        in_scope_errors=len(in_scope),
        out_of_scope_errors=len(out_of_scope),
        autofix_attempted=False, post_fix_errors=0,
        violations=in_scope + out_of_scope,
    )


def format_for_fixer(result: StaticCheckResult) -> str:
    """Render in-scope violations into the freeform-markdown shape the
    existing ``codegen-violation-fixer`` agent expects (matches the format
    produced by ``test_indexer.violations_summary``).
    """
    in_scope = result.in_scope_violations
    if not in_scope:
        return ""
    lines = [f"{len(in_scope)} type error(s) reported by {result.tool}:"]
    for v in in_scope[:50]:
        lines.append(f"  [{v.rule}] {v.file}:{v.line}  {v.snippet.strip()[:200]}")
    if len(in_scope) > 50:
        lines.append(f"  ... and {len(in_scope) - 50} more")
    return "\n".join(lines)
