"""Step 9: Execute tests + self-heal.

Workflow:
  1. Load Step 6 research and resolve the SUT tests directory (no mirror —
     Step 9 already wrote files in `<workspace>/sut/` on the qtea branch
     based on the Step 8 plan).
  2. Resolve test-run command (research.commands.test or per-framework default).
  3. Execute via `test_runner.run_tests` with `cwd=<workspace>/sut/`. Capture
     per-test status + attachments.
  4. For each failing test: invoke `polyglot-test-fixer` once with the failing
     test source + traceback. The fixer agent has `add_dirs=[<workspace>/sut/]`
     so it can read SUT helpers / page objects directly and edit the failing
     test file in place. On a successful patch, commit it to the qtea
     branch with one commit per healed test.
  5. Re-run only the failing tests once. Record self-heal outcome per test.
  6. Emit run-results.json + bug-candidates.json + heal-log.jsonl.

Self-heal budget is capped (default 5 tests) to bound runtime.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from qtea._sut_git import commit_step
from qtea.auth_helpers import (
    auth_relevant_sut_files as _auth_relevant_sut_files,
)
from qtea.auth_helpers import (
    auth_summary_for_prompt as _auth_summary_for_prompt,
)
from qtea.claude_runner import run_agent
from qtea.config import (
    HEAL_AGENT_MAX_TURNS,
    HEAL_AGENT_TIMEOUT_S,
    package_resource_root,
    step_timeout,
)
from qtea.logging_setup import get_logger
from qtea.proxy import safe_subprocess_env
from qtea.resolver_server import ResolverServer
from qtea.schemas import is_valid
from qtea.stack_profile import PYTHON_VENV_MANAGERS, StackProfile, wrap_command
from qtea.steps.base import Step, StepContext, StepResult
from qtea.test_runner import (
    _PYTEST_PLUGIN_PROVIDERS,
    _PW_TEST_FRAMEWORKS,
    RunResult,
    TestRunEntry,
    execute_command,
    install_command_for,
    prepare_sut,
    resolve_command,
    run_tests,
)

log = get_logger(__name__)

# Cap on number of HEALABLE failing tests we'll attempt to self-heal in a
# single step run. The cap exists as runaway-cost protection: each heal
# invocation spawns a Playwright MCP browser and burns LLM tokens
# (~$0.10-0.50 per heal). The default was 5 historically; bumped to 15 so
# typical small suites (10-15 tests) where the LLM resolver hit a hard UI
# (DSSF-style synthetic CSS classes, hover-only elements) fit inside the
# cap. Tests are pre-filtered by ``_partition_failures`` so this counts
# only locator/timeout class failures; WCAG / TTI / fixture-missing rows
# are excluded from heal entirely and do NOT count against the cap.
_MAX_HEAL_TESTS = int(os.environ.get("QTEA_MAX_HEAL", "15"))

# Pytest -m selector that scopes Step 9 to ONLY the qtea generated tests.
# The codegen agent (`codegen-violation-fixer.agent.md` rule 8) applies one of these
# markers to every generated test based on the planning phase. The vendored
# `tests/qtea_runtime.py` plugin registers them via `pytest_configure` so
# strict-markers runs don't fail. Keep this list in sync with the agent prompt
# and the runtime template's `_QTEA_PHASE_MARKERS`. Operator escape: set
# `QTEA_PYTEST_MARKER` to override (e.g. `""` to disable marker scoping
# and run the SUT's full native suite alongside qtea-generated tests).
_QTEA_PYTEST_MARKER_FILTER = os.environ.get(
    "QTEA_PYTEST_MARKER",
    "qtea_smoke or qtea_regression or qtea_e2e or qtea_exploratory",
)

# Patterns that mark an XPath selector. Used by `_patch_introduces_xpath` to
# reject heal patches that quietly downgrade to XPath in violation of the
# Step 9 quality gate (see docs/qa-orchestrator.instructions.md §6).
_XPATH_PATTERNS: tuple[str, ...] = (
    "By.XPATH",
    "xpath=",
    "getByXPath(",
    ".xpath(",
    "By.xpath(",
    "XPATH:",
)


def _count_xpath_markers(source: str) -> int:
    """Count XPath-marker occurrences in a source blob. Combines literal-pattern
    matches with a regex that catches string literals beginning with `//` (the
    raw XPath shorthand)."""
    import re as _re

    count = sum(source.count(p) for p in _XPATH_PATTERNS)
    # String literals starting with // — Selenium's `By.XPATH, '//x'` and the
    # bare `'//div'` form. Counts both single- and double-quoted variants.
    count += len(_re.findall(r"""['"]//[^'"\n]+['"]""", source))
    return count


def _patch_introduces_xpath(pre: bytes | None, post: bytes | None) -> bool:
    """True iff the post-heal source contains MORE XPath markers than the
    pre-heal source. We count rather than detect-any so an existing XPath in
    the SUT (legitimate or grandfathered) doesn't false-trigger the gate; only
    a NEW introduction is rejected.

    When ``pre is None`` the heal CREATED a new file — any XPath in the new
    file is by definition introduced."""
    if post is None:
        return False
    try:
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return False
    post_count = _count_xpath_markers(post_src)
    if pre is None:
        return post_count > 0
    try:
        pre_src = pre.decode("utf-8", errors="replace")
    except Exception:
        return False
    return post_count > _count_xpath_markers(pre_src)


# ---------------------------------------------------------------------------
# Assertion-immutability gate (mirrors XPath gate above)
# ---------------------------------------------------------------------------

import contextlib
import re as _re_module

_ASSERTION_LINE_PATTERNS: tuple[_re_module.Pattern, ...] = (
    _re_module.compile(r"^\s*assert\b"),
    _re_module.compile(r"^\s*expect\s*\("),
    _re_module.compile(r"^\s*with\s+pytest\.raises\b"),
    _re_module.compile(r"\.should\s*\("),
    _re_module.compile(
        r"^\s*assert(?:Equals|True|False|Null|NotNull|That|Same|Throws)\s*\(",
        _re_module.IGNORECASE,
    ),
    _re_module.compile(r"^\s*Should\s+(?:Be|Contain|Match|Not)", _re_module.IGNORECASE),
)


def _extract_assertion_lines(source: str) -> list[str]:
    """Extract normalised assertion lines from source (stripped + lowered)."""
    out: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if any(p.search(stripped) for p in _ASSERTION_LINE_PATTERNS):
            out.append(stripped.lower())
    return out


def _patch_modifies_assertions(pre: bytes | None, post: bytes | None) -> bool:
    """True iff the post-heal source REMOVED or ALTERED any assertion line
    that existed in the pre-heal source.

    Adding new assertions is allowed. When *pre* is ``None`` the file was
    created by the heal, so there were no prior assertions to protect."""
    if pre is None or post is None:
        return False
    try:
        pre_src = pre.decode("utf-8", errors="replace")
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return False
    pre_assertions = _extract_assertion_lines(pre_src)
    if not pre_assertions:
        return False
    post_assertions = _extract_assertion_lines(post_src)
    from collections import Counter

    pre_counts = Counter(pre_assertions)
    post_counts = Counter(post_assertions)
    return any(post_counts.get(assertion, 0) < count for assertion, count in pre_counts.items())


# ---------------------------------------------------------------------------
# Anti-pattern gate — rejects heals that introduce exception-swallowing
# ---------------------------------------------------------------------------

_EMPTY_HANDLER_PATTERNS: tuple[_re_module.Pattern, ...] = (
    # Python: except ...: pass / except: pass (single or multi-line)
    _re_module.compile(
        r"except\b[^:]*:\s*(?:#[^\n]*)?\n\s*pass\b",
        _re_module.MULTILINE,
    ),
    # JS/TS: catch (...) { } or catch { } with empty/whitespace-only body
    _re_module.compile(
        r"catch\s*(?:\([^)]*\))?\s*\{\s*\}",
    ),
    # Java/C#: catch (...) { } with empty/whitespace-only body
    _re_module.compile(
        r"catch\s*\([^)]+\)\s*\{\s*\}",
    ),
)


def _count_empty_handlers(source: str) -> int:
    """Count exception handlers with empty/no-op bodies across stacks."""
    return sum(len(p.findall(source)) for p in _EMPTY_HANDLER_PATTERNS)


def _patch_has_anti_patterns(pre: bytes | None, post: bytes | None) -> list[str]:
    """Return a list of anti-pattern violations INTRODUCED by the heal.

    Only flags patterns that are NEW (post count > pre count) so
    pre-existing SUT code doesn't trigger false positives.
    Returns an empty list when clean."""
    if post is None:
        return []
    try:
        post_src = post.decode("utf-8", errors="replace")
    except Exception:
        return []
    post_count = _count_empty_handlers(post_src)
    if post_count == 0:
        return []
    pre_count = 0
    if pre is not None:
        try:
            pre_src = pre.decode("utf-8", errors="replace")
            pre_count = _count_empty_handlers(pre_src)
        except Exception:
            pass
    if post_count > pre_count:
        return [
            f"exception swallowing: {post_count - pre_count} new empty "
            f"exception handler(s) (except/catch with no-op body)"
        ]
    return []


# File-shape predicates that heal is forbidden to touch. Mirrors the
# FORBIDDEN block in `agents/polyglot-test-fixer.agent.md`. A heal that
# modifies any file matching one of these (and not also matching the
# POM allowlist) is reverted and reported as scope_violation. Catches
# the run 20260611-184450 incident where the heal agent edited
# `tests/fixtures/qtea_gemini_nav_*` instead of staying inside POM/
# locator source. Implemented as predicates rather than glob patterns
# because `fnmatch` does not handle `**`-recursive semantics portably.


def _heal_path_is_forbidden(rel_posix: str) -> bool:
    """True iff the path matches a FORBIDDEN file shape (basename + segments)."""
    p = rel_posix
    basename = p.rsplit("/", 1)[-1] if "/" in p else p
    segments = p.split("/")
    if basename == "conftest.py":
        return True
    if "__tests__" in segments:
        return True
    if "tests" in segments and "fixtures" in segments:
        # Forbidden when 'fixtures' sits directly under any 'tests/' segment.
        for i, seg in enumerate(segments[:-1]):
            if seg == "tests" and i + 1 < len(segments) and segments[i + 1] == "fixtures":
                return True
    if "tests" in segments:
        if basename.startswith("test_") and basename.endswith(".py"):
            return True
        if basename.endswith("_test.py"):
            return True
    if basename.endswith((".spec.ts", ".spec.js", ".test.ts", ".test.js")):
        return True
    return bool(basename.endswith("Test.java"))


def _heal_allowlist_dirs(active_module: dict | None) -> set[str]:
    """POM/locator directories (SUT-relative, posix-style) heal may touch.

    Derived from `sut_inventory.json` → `modules[active].existing_page_objects`
    + `existing_locators`. Empty set means "no allowlist information" — in
    that case we fall back to permissive behaviour (only the FORBIDDEN globs
    are enforced).
    """
    if not isinstance(active_module, dict):
        return set()
    dirs: set[str] = set()
    for key in ("existing_page_objects", "existing_locators"):
        for entry in active_module.get(key) or []:
            file_rel = (entry.get("file") if isinstance(entry, dict) else "") or ""
            file_rel = file_rel.replace("\\", "/")
            if not file_rel:
                continue
            parent = file_rel.rsplit("/", 1)[0] if "/" in file_rel else ""
            if parent:
                dirs.add(parent)
    return dirs


def _heal_path_in_scope(
    rel_path: str,
    allowlist_dirs: set[str],
    generated_files: set[str] | None = None,
) -> bool:
    """True iff a heal-modified path is in-scope.

    Logic:
      0. If the path is a codegen-generated file → always in-scope
         (the heal agent is fixing codegen's own mistakes).
      1. If the path is FORBIDDEN (fixture / test / conftest shape) → out.
      2. If ``allowlist_dirs`` is non-empty, the path's parent must START WITH
         one of those dirs. Empty allowlist → only rule (1) applies.
    """
    p = rel_path.replace("\\", "/")
    if generated_files and p in generated_files:
        return True
    if _heal_path_is_forbidden(p):
        return False
    if not allowlist_dirs:
        return True
    return any(p == d or p.startswith(d + "/") for d in allowlist_dirs)


def _git_revert_path(sut_root: Path, rel_path: str, status_code: str) -> bool:
    """Revert a single uncommitted change. Returns True on success."""
    import subprocess
    try:
        if status_code.strip() == "??":
            (sut_root / rel_path).unlink(missing_ok=True)
        else:
            subprocess.run(
                ["git", "checkout", "HEAD", "--", rel_path],
                cwd=sut_root, capture_output=True, text=True,
                check=False, timeout=10,
            )
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("step09.git_revert_failed", path=rel_path, error=str(e))
        return False


def _git_status_porcelain(sut_root: Path) -> list[tuple[str, str]]:
    """Return [(status_code, path), …] from `git status --porcelain`.

    Uses `--untracked-files=all` so new files inside a previously-untracked
    directory are listed individually (default porcelain collapses them to
    the directory path, which breaks per-file revert). Empty list on
    git-missing / error. Handles rename entries by taking the destination
    path.
    """
    import subprocess
    if not (sut_root / ".git").exists():
        return []
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=sut_root, capture_output=True, text=True, check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("step09.git_status_failed", error=str(e))
        return []
    out: list[tuple[str, str]] = []
    for line in (res.stdout or "").splitlines():
        if len(line) < 4:
            continue
        status_code = line[:2]
        path_part = line[3:].strip().strip('"')
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip().strip('"')
        if path_part:
            out.append((status_code, path_part))
    return out


def _heal_scope_check_and_revert(
    sut_root: Path,
    base_sha: str | None,
    allowlist_dirs: set[str],
    generated_files: set[str] | None = None,
    pre_heal_dirty: set[str] | None = None,
) -> list[str]:
    """Inspect ``git status --porcelain`` for files the heal touched.

    Reverts any out-of-scope modifications (``git checkout HEAD -- <file>``
    for modified/deleted, ``rm`` for newly added). Returns the list of paths
    that were reverted — empty when every touched file was in-scope.
    Caller maps a non-empty return to ``applied=false, reason=scope_violation``.

    *pre_heal_dirty*: files already dirty before the heal agent ran (e.g.
    ``qtea-junit.xml`` from pytest). These are skipped — the heal agent
    did not create them.
    """
    reverted: list[str] = []
    for status_code, path_part in _git_status_porcelain(sut_root):
        if pre_heal_dirty and path_part in pre_heal_dirty:
            continue
        if _heal_path_in_scope(path_part, allowlist_dirs, generated_files=generated_files):
            continue
        if _git_revert_path(sut_root, path_part, status_code):
            reverted.append(path_part)
            log.warning(
                "step09.heal_out_of_scope_reverted",
                path=path_part,
                base_sha=base_sha,
            )
    return reverted


def _heal_revert_all_uncommitted(
    sut_root: Path,
    base_sha: str | None,
) -> list[str]:
    """Revert EVERY uncommitted change in the SUT working tree.

    Called when the heal agent failed outright (timeout, transport error)
    to ensure no in-flight edits — even ones inside the POM allowlist —
    survive on disk. Without this, run 20260611-184450 left 5 in-progress
    fixture edits on the qtea branch after the 150s timeout, and the
    `applied=false` log conflicted with the on-disk reality.
    """
    reverted: list[str] = []
    for status_code, path_part in _git_status_porcelain(sut_root):
        if _git_revert_path(sut_root, path_part, status_code):
            reverted.append(path_part)
    if reverted:
        log.warning(
            "step09.heal_full_revert",
            base_sha=base_sha,
            paths=reverted,
        )
    return reverted
# Fallback tests subdirectory used when:
#   - --isolated-tests is set (explicit user opt-in to today's behavior), OR
#   - sut_inventory has no test_directory_layout for the active module.
# When isolated, the dir lives under the active module's path so monorepos
# don't clobber sibling modules.
_ISOLATED_TESTS_DIR_NAME = "qteaests"


# Poetry stdout phrases that indicate `poetry add` was a no-op because the
# package is already declared in pyproject.toml. Exit code is 0 in that case
# even though NOTHING was installed — treat as failure so the caller doesn't
# claim victory and re-run the same broken tests. See the run forensics in
# the bug that introduced this check: the package was declared but never
# installed in the resolved venv (because qtea's parent venv was being
# reused — see proxy.safe_subprocess_env's isolate_venv flag).
_POETRY_NOOP_MARKERS: tuple[str, ...] = (
    "already present in the pyproject.toml",
    "Nothing to add",
)


def _run_dep_install(
    package_manager: str | None,
    package: str,
    sut_root: Path,
    install_log_path: Path,
    *,
    timeout_s: int = 600,
    profile: StackProfile | None = None,
) -> tuple[bool, str]:
    """Install a single missing test dependency. Append outcome to install.log.

    `profile` is consulted for two reasons: (1) pip needs the SUT's venv-bin
    directory to build a path-prefixed install argv, and (2) Python managers
    that own a venv (poetry, uv, pdm, pipenv) need `VIRTUAL_ENV` stripped
    from the subprocess env so they don't reuse qtea's parent venv as
    the SUT's venv. Falls back to bare argv when `profile` is None — same
    behavior as pre-fix runs for callers that don't have a profile handy.

    Returns ``(success, summary)`` where ``summary`` is the command line on
    success or a one-line error on failure. Never raises — package-manager
    timeouts / OS errors become structured failures.
    """
    pm = (package_manager or "").lower()
    # pip needs the SUT's venv bin dir to install into the venv the tests
    # actually use; for poetry/uv/pdm/pipenv venv_bin is unused.
    venv_bin = None
    if pm == "pip" and profile and profile.wrapper_prefix:
        venv_bin = profile.wrapper_prefix
    argv = install_command_for(package_manager, package, venv_bin=venv_bin)
    if argv is None:
        if pm == "pip":
            return False, (
                f"pip auto-install requires the SUT to have a .venv "
                f"(profile.wrapper_prefix). None detected for "
                f"package_manager={package_manager!r}."
            )
        return False, f"no install argv for package_manager={package_manager!r}"

    cmd_line = " ".join(argv)
    try:
        proc = subprocess.run(
            argv,
            cwd=sut_root,
            env=safe_subprocess_env(isolate_venv=pm in PYTHON_VENV_MANAGERS),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        with install_log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n$ {cmd_line}  # auto-install\n# error: {e}\n")
        return False, f"{cmd_line}: {e}"

    with install_log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n\n$ {cmd_line}  # auto-install missing dep\n"
            f"# exit_code: {proc.returncode}\n"
            f"# STDOUT\n{proc.stdout}\n"
            f"# STDERR\n{proc.stderr}\n"
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        snippet = tail[-1] if tail else ""
        return False, f"`{cmd_line}` exited {proc.returncode}: {snippet[:200]}"

    if pm == "poetry" and any(m in (proc.stdout or "") for m in _POETRY_NOOP_MARKERS):
        return False, (
            f"`{cmd_line}` was a no-op: package already declared in pyproject.toml "
            f"but not installed in the resolved venv. Run `poetry install` against "
            f"a clean SUT-specific venv, or delete the active venv and re-run."
        )
    return True, cmd_line


def _research_payload(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(6) / "research.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_stack_profile(ctx: StepContext) -> StackProfile | None:
    """Load Step 6's stack_profile.json into a `StackProfile` dataclass.

    Returns None when the artifact is missing (older workspaces re-run from
    Step 8+) or when the JSON is unparseable. Step 9 falls back to bare
    framework commands in that case.
    """
    p = ctx.workspace.step_dir(6) / "stack_profile.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # Tolerate extra keys; only consume known dataclass fields.
    allowed = {
        "language", "package_manager", "wrapper_prefix",
        "pre_install_command", "install_command",
        "test_command", "start_command", "env_file_path", "venv_path",
        "detection_signal", "notes",
    }
    return StackProfile(**{k: data.get(k) for k in allowed})


def _detected_command(research: dict) -> str | None:
    cmds = research.get("commands") or {}
    return cmds.get("test")


def _active_module(sut_inventory: dict) -> dict | None:
    """Pull the active module entry out of a raw `sut_inventory` dict.

    Returns None when no `active_module` is set or the name doesn't match.
    Older runs (no `sut_inventory` field) get None and fall through to the
    isolated-tests fallback.
    """
    active = sut_inventory.get("active_module")
    if not active:
        return None
    for mod in sut_inventory.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == active:
            return mod
    return None


def _framework(research: dict, index: dict) -> str:
    return research.get("detected_stack") or index.get("framework") or "unknown"


def _load_index(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(8) / "tbd-index.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_generated_files(ctx: StepContext) -> set[str]:
    """Load Step 8's ``generated-files.json`` and return SUT-relative posix paths.

    These are the files codegen produced this run.  The heal agent is allowed
    to edit them even when they match a test-file FORBIDDEN pattern, because
    the heal agent is fixing codegen's own mistakes (interaction patterns,
    locator usage) — NOT pre-existing SUT test code.
    """
    p = ctx.workspace.step_dir(8) / "generated-files.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    files = data.get("files") or []
    return {f.replace("\\", "/") for f in files if isinstance(f, str)}


def _sut_tests_dir(
    sut_root: Path,
    *,
    active_module: dict | None,
    isolated: bool,
) -> Path:
    """Resolve the directory inside the SUT where qtea-generated tests live.

    Steps 7 & 8 write tests there directly (on the qtea branch); this
    function only computes the path — nothing is copied or wiped.

    Resolution mirrors the active module's layout:
      - `--isolated-tests` → `<sut>/<module.path>/qteaests/`
      - Active module's `test_directory_layout.base_dir` (the SUT's own
        convention, e.g. `tests/` or `e2e/`) → `<sut>/<module.path>/<base_dir>/`
      - Fallback when nothing is known: `<sut>/<module.path>/qteaests/`
    """
    module_path = "."
    base_dir = _ISOLATED_TESTS_DIR_NAME
    if active_module:
        module_path = str(active_module.get("path") or ".")
        if not isolated:
            layout = active_module.get("test_directory_layout") or {}
            candidate = layout.get("base_dir")
            if candidate:
                base_dir = str(candidate)
    module_root = sut_root if module_path == "." else (sut_root / module_path)
    return module_root / base_dir


def _attachment_glob(sut_root: Path) -> list[dict]:
    """Best-effort discovery of common test-result artifacts in the SUT root."""
    out: list[dict] = []
    patterns: list[tuple[str, str]] = [
        ("test-results/**/trace.zip", "trace"),
        ("test-results/**/*.png", "screenshot"),
        ("test-results/**/*.webm", "video"),
        ("screenshots/**/*.png", "screenshot"),
        ("screenshots/**/*.jpg", "screenshot"),
        ("reports/screenshots/**/*.png", "screenshot"),
        ("playwright-report/**/*", "other"),
        ("allure-results/**/*.png", "screenshot"),
        ("allure-results/**/*.json", "other"),
    ]
    for pattern, kind in patterns:
        for p in sut_root.glob(pattern):
            if p.is_file():
                out.append({"path": str(p), "type": kind})
    return out


def _clean_sut_artifacts(sut_root: Path) -> None:
    """Remove prior-attempt screenshots/traces so only the last run's artifacts survive."""
    import contextlib

    patterns = [
        "test-results/**/*.png",
        "test-results/**/*.webm",
        "screenshots/**/*.png",
        "screenshots/**/*.jpg",
        "reports/screenshots/**/*.png",
    ]
    for pattern in patterns:
        for p in sut_root.glob(pattern):
            with contextlib.suppress(OSError):
                p.unlink()


def _failing_tests(run: RunResult) -> list[TestRunEntry]:
    return [r for r in run.results if r.status in ("failed", "error")]


def _build_fixer_prompt(
    entry: TestRunEntry,
    tests_root_in_sut: Path,
    *,
    sut_root: Path | None = None,
    sut_base_url: str | None = None,
    active_module: dict | None = None,
    staged_files: list[str] | None = None,
    storage_state_path: Path | None = None,
    generated_files: set[str] | None = None,
    failure_class: str | None = None,
) -> str:
    snippet = (entry.traceback or entry.message or "(no traceback)")[-3000:]

    live_block = ""
    if sut_base_url:
        auth_summary = _auth_summary_for_prompt(active_module) if active_module else ""
        if sut_root is None:
            files_str = "\n".join(f"  - `{p}`" for p in (staged_files or [])) \
                or "  (none discovered)"
        else:
            files_str = "\n".join(f"  - `{sut_root / p}`" for p in (staged_files or [])) \
                or "  (none discovered)"
        language = (active_module or {}).get("language") or "unknown"
        # Storage-state pre-load directive (empty string when no state
        # was resolved by Step 9). When set, instructs the agent to skip
        # the auth-replay step entirely; when unset, the auth-replay
        # workflow remains.
        from qtea import storage_state as _storage_state_mod
        storage_state_block = _storage_state_mod.summary_for_prompt(storage_state_path)
        # Step (1) of the workflow varies based on storage-state availability.
        # When pre-loaded, the agent skips the sign-in helper outright.
        # When absent, the agent follows the SUT's auth flow as before.
        if storage_state_path is not None:
            step_one = (
                "(1) The browser is already authenticated via the pre-loaded "
                "storage state described above. Call `mcp__playwright__browser_navigate` "
                "to go directly to the failing page URL — do NOT call the SUT's "
                "sign-in helper. (If the page redirects to login, the state "
                "is stale: log a note and fall back to the auth-replay path "
                "via the helpers below.) "
            )
        else:
            step_one = (
                f"(1) Call `mcp__playwright__browser_navigate` to open "
                f"`{sut_base_url}` and follow the SUT's auth flow via the "
                f"existing sign-in helper above. "
            )
        live_block = (
            f"\n--- LIVE DIAGNOSIS ---\n"
            f"SUT base URL: `{sut_base_url}`. Active module language: `{language}`.\n"
            f"{auth_summary}\n"
            f"{storage_state_block}\n"
            + (f"\nSUT clone root (you have add_dirs access — read + edit "
               f"these files directly): `{sut_root}`\n" if sut_root else "\n")
            + (f"\nKey SUT files for this active module — call these instead "
               f"of reimplementing auth or navigation:\n{files_str}\n\n"
               f"Playwright MCP is pre-warmed and ready — its tools are exposed "
               f"as `mcp__playwright__<name>` (e.g. `mcp__playwright__browser_navigate`, "
               f"`mcp__playwright__browser_snapshot`). Use the exact prefixed name; "
               f"bare `browser_navigate` will not resolve.\n\n"
               f"Workflow: {step_one}"
               f"(2) Take a `mcp__playwright__browser_snapshot` of the page "
               f"the failing test targets and compare it to what the traceback "
               f"says the test expected. (3) Patch the test based on what you "
               f"observe live, NOT just from the traceback text. Match the "
               f"active module's language: `{language}`. Never rewrite a Python "
               f"test in TypeScript or vice versa.\n")
        )

    # Absolute path of the failing test inside the SUT. The fixer must edit
    # THIS exact file (on the qtea branch) — no per-step workdir copy.
    failing_test_abs = (
        (tests_root_in_sut / Path(entry.file).name).as_posix()
        if entry.file else "(unknown — see `entry.file`)"
    )

    gen_block = ""
    if generated_files:
        _test_suffixes = (
            "_test.py", ".spec.ts", ".spec.js", ".test.ts", ".test.js",
            "Test.java",
        )
        gen_test_files = sorted(
            f for f in generated_files
            if f.endswith(_test_suffixes)
            or (f.split("/")[-1].startswith("test_") and f.endswith(".py"))
        )
        if gen_test_files:
            files_list = "\n".join(f"  - `{f}`" for f in gen_test_files)
            gen_block = (
                f"\n--- GENERATED TEST FILES (EDITABLE) ---\n"
                f"The following test files were generated by codegen (Step 8) "
                f"this run. You MAY edit interaction patterns in these files "
                f"(method calls, locator usage, navigation sequences, API "
                f"usage like switching from `.click()` to `page.select_option()`). "
                f"You MUST NOT modify assertions (`assert`, `expect`, `.should()`). "
                f"Pre-existing test files NOT listed here remain FORBIDDEN.\n"
                f"{files_list}\n"
            )

    class_block = ""
    if failure_class:
        class_block = f"\nFailure class: `{failure_class}`\n"
        if failure_class == "assertion_value":
            class_block += (
                "Strategy hint: this failure was classified as `assertion_value` — "
                "the traceback shows an assertion mismatch (e.g. `assert None == "
                "'true'`). This is SOMETIMES downstream of locator drift (wrong "
                "element found -> wrong attribute value) and SOMETIMES a genuine "
                "app defect that heal cannot fix.\n\n"
                "**Diagnose from the traceback first.** Read the assertion line, "
                "the expected vs actual values, and the locator used. If the "
                "mismatch clearly indicates a wrong-element problem (e.g. "
                "`get_attribute` returned `None` because the locator resolved to "
                "a different element), proceed with live browser navigation to "
                "find the correct locator.\n\n"
                "If the mismatch indicates a genuine attribute/content defect in "
                "the app (e.g. the correct element was found but it genuinely "
                "lacks the expected attribute, or returns a different value), "
                "this is an app bug — not locator drift. Emit "
                "`OUT_OF_SCOPE: assertion-attribute-defect` and stop. Do NOT "
                "spend turns navigating the browser to confirm what the "
                "traceback already tells you.\n"
            )

    return (
        "A single test failed. Apply the smallest possible patch to make it "
        "pass without modifying assertions, business logic, or test_ids, and "
        "without adding hard waits. Locator priority: id > data-testid > role > "
        "label > text > placeholder > scoped css. NEVER XPath.\n\n"
        f"Test id: {entry.id}\n"
        f"Test file (relative to repo root): {entry.file}\n"
        f"Tests directory in SUT: {tests_root_in_sut.as_posix()}\n"
        f"Failing test absolute path (edit this file in place): {failing_test_abs}\n"
        f"Status: {entry.status}\n"
        f"Message: {entry.message or '(none)'}\n\n"
        f"Traceback:\n{snippet}\n"
        f"{class_block}"
        f"{gen_block}"
        f"{live_block}\n"
        f"Edit the failing test file at its absolute path above using the "
        f"Edit tool. The pipeline does NOT copy your changes anywhere — the "
        f"SUT clone IS the deliverable, on a qtea-owned git branch. Only "
        f"edit files that already exist under the tests directory."
    )


def _apply_fixer_outputs(
    workdir: Path,
    sut_tests_dir: Path,
    target_file_rel: str,
    *,
    ignore_paths: set[Path] | None = None,
) -> bool:
    """Copy any test-file edits produced by the fixer back into sut_tests_dir.

    Only files matching the target test's relative path (or any sibling under
    the same tests dir layout) are accepted; everything else is ignored.
    `ignore_paths` should contain absolute paths the caller staged BEFORE the
    agent ran (e.g. the original test file copy); those are filtered out so
    they are not mis-detected as a "patch".

    Returns True if anything was applied.
    """
    if not target_file_rel:
        return False
    ignore = {p.resolve() for p in (ignore_paths or set())}
    basename = Path(target_file_rel).name
    target_abs = sut_tests_dir / basename
    # Find every matching file under workdir; exclude ignored stage copies.
    candidates = [p for p in workdir.rglob(basename) if p.is_file() and p.resolve() not in ignore]
    if not candidates:
        return False
    # Pick latest-modified candidate.
    chosen = max(candidates, key=lambda p: p.stat().st_mtime)
    new_text = chosen.read_text(encoding="utf-8", errors="replace")
    if target_abs.exists():
        old_text = target_abs.read_text(encoding="utf-8", errors="replace")
        if old_text == new_text:
            return False
    target_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(chosen, target_abs)
    return True


def _filter_command_for_tests(command: str, failing: list[TestRunEntry]) -> str:
    """Narrow a re-run command to only the healed tests.

    pytest: ``-k "name1 or name2"``
    Playwright Test: ``--grep "name1|name2"``

    Parametrization suffixes (``test_x[case]``) are stripped to the base
    name so every parametrization of a healed test is re-run.

    No-op when there are no failing tests, no usable names could be
    extracted, or the command already carries an explicit filter.
    """
    if not failing:
        return command
    names: list[str] = []
    for e in failing:
        name = (e.name or "").split("[", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return command

    tokens = command.lower().split()

    # Playwright Test: --grep "name1|name2"
    if "playwright" in tokens and "test" in tokens:
        if re.search(r"(?:^|\s)--grep(?:\s|=)", command):
            return command
        escaped = [re.escape(n) for n in names]
        return f'{command} --grep "{"|".join(escaped)}"'

    # pytest: -k "name1 or name2"
    if "pytest" not in tokens:
        return command
    if re.search(r"(?:^|\s)-k(?:\s|=)", command):
        return command
    expr = " or ".join(names)
    return f'{command} -k "{expr}"'


def _narrow_command_to_ids(
    command: str, all_results: list, allow_ids: set[str],
) -> str:
    """Restrict ``command`` to tests whose id is in ``allow_ids``. Wraps
    :func:`_filter_command_for_tests` so the same -k / --grep narrowing
    logic applies to attempt-2's initial test run, not just the post-heal
    re-run."""
    keep = [e for e in all_results if e.id in allow_ids]
    return _filter_command_for_tests(command, keep)


# ----------------------------------------------------------------------------
# Cross-attempt state (Tasks 2 + 4 + 5)
#
# Persisted per attempt under ``artifacts/step09/attempt-N-state.json`` so the
# retry path can:
#   - skip the SUT install when nothing the package manager cares about
#     changed (Task 4 — saves ~10–30 s);
#   - narrow attempt 2's initial test run to the subset that failed in
#     attempt 1, MINUS tests the heal agent classified as real bugs
#     (Tasks 2 + 5 — saves ~150–300 s plus the LLM cost of re-healing
#     tests we already know we can't fix);
#   - skip the heal call for those same real-bug tests on attempt 2.
# ----------------------------------------------------------------------------

# Heal `summary_text` value that signals "agent ran, found no fixable
# defect" — i.e. very likely a real product bug, not a flaky selector.
# Other summary strings ("rejected: heal introduced XPath", scope violation,
# agent error) are NOT classified as real bug — they mean the heal needs
# another try with different prompt / scope, not that there's nothing to fix.
_NO_PATCH_SUMMARY = "no usable patch produced"


def _attempt_state_path(out_dir: Path, attempt: int) -> Path:
    return out_dir / f"attempt-{attempt}-state.json"


def _save_attempt_state(
    out_dir: Path,
    attempt: int,
    *,
    failing: list[tuple[str, str]],
    no_patch_ids: list[str],
    install_sig: str | None,
) -> None:
    """Persist attempt N outcomes for attempt N+1's pre-run narrowing.

    ``failing`` is a list of ``(id, name)`` tuples — ``id`` for set
    membership in the heal-skip filter; ``name`` is what
    :func:`_filter_command_for_tests` needs to build the ``-k`` /
    ``--grep`` expression for the narrowed test run.

    Best-effort: IO errors log and swallow so an artifact-write failure
    cannot poison the retry path itself.
    """
    path = _attempt_state_path(out_dir, attempt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "attempt": attempt,
                "failing": [{"id": i, "name": n} for i, n in failing],
                "no_patch_ids": list(no_patch_ids),
                "install_sig": install_sig,
                "saved_at": datetime.now(UTC).isoformat(),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning(
            "step09.attempt_state_save_failed", attempt=attempt, error=str(e),
        )


def _load_attempt_state(out_dir: Path, attempt: int) -> dict | None:
    """Read attempt-N state. None when missing or corrupt — callers MUST
    handle None by treating the attempt as cold (no narrowing, no skips)."""
    path = _attempt_state_path(out_dir, attempt)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning(
            "step09.attempt_state_load_failed", attempt=attempt, error=str(e),
        )
        return None


def _compute_install_sig(sut_root: Path, stack_profile) -> str | None:
    """Stable signature of the SUT's dependency state. Two attempts of the
    same step in the same workspace will see identical sig (heal commits
    touch SUT source but NOT lockfiles) → install skip is safe.

    Returns None when no lockfile is found — caller MUST treat as
    "don't skip install" (better to re-install than risk a stale env)."""
    if stack_profile is None:
        return None
    import hashlib

    lock_candidates = (
        "poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "uv.lock", "Pipfile.lock", "Gemfile.lock", "go.sum", "Cargo.lock",
    )
    parts: list[str] = [stack_profile.package_manager or ""]
    found_any = False
    for name in lock_candidates:
        p = sut_root / name
        if p.is_file():
            try:
                st = p.stat()
                parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
                found_any = True
            except OSError:
                continue
    if not found_any:
        return None
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _prewarm_jit_cache_dev_pool(
    *,
    ctx: StepContext,
    jit_cache_dir: Path,
    dev_locators_path: Path,
) -> int:
    """Pre-populate the locator cache with tier-1b dev-pool matches for
    every TBD sentinel currently in the SUT source tree. Returns count.

    Source of truth: the live SUT — scanned via
    :func:`qtea.tbd_scanner.scan_tbd_intents`. That guarantees we
    prewarm the intents the next test run will actually request, even
    if step 8's archived ``tbd-index.json`` has gone stale (heal
    agent rewrote a constant, manual edit between steps, etc.).
    No-op when no TBDs are present or no dev-locator pool is supplied.
    """
    from qtea import jit_resolver
    from qtea.runtime.dev_locators import load_dev_locators
    from qtea.tbd_scanner import scan_tbd_intents

    sut_root = ctx.workspace.sut
    scan_roots = [p for p in (sut_root / d for d in ("src", "tests", "pages")) if p.is_dir()]
    if not scan_roots:
        scan_roots = [sut_root]
    hits = scan_tbd_intents(scan_roots, sut_root=sut_root)
    if not hits:
        return 0

    # Dedupe by (intent, constant_name) — same intent referenced by
    # multiple constants or files still needs one prewarm per cache key,
    # which the resolver will dedupe internally via cache_key().
    intents_payload: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for h in hits:
        intent = (h.intent or "").strip()
        const = (h.constant_name or "").strip() or intent  # bare tbd() → intent as const
        dedupe_key = (intent, h.constant_name)
        if not intent or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        intents_payload.append({
            "intent": intent,
            "constant_name": const,
            "test_file": str(h.file).replace("\\", "/"),
        })

    if not intents_payload:
        return 0

    locators, _src, _warnings = load_dev_locators(cli_path=dev_locators_path)
    if not locators:
        return 0
    return jit_resolver.prewarm_dev_pool_cache(
        tbd_intents=intents_payload,
        dev_locators=locators,
        cache_path=jit_cache_dir / "locator-cache.json",
        run_id=ctx.workspace.run_id,
    )


# ---------------------------------------------------------------------------
# Failure classification (used by the heal-gate to skip un-healable rows)
# ---------------------------------------------------------------------------
#
# Run 20260621-213751-ee0fef hit the canonical recurring failure: 11/13 tests
# failed, the heal-skip cap (`len(failing) > _MAX_HEAL_TESTS`) blocked the
# entire heal flow, and TBD-promotion stayed blocked on `no_passing_witness`
# — so the user saw 11 mixed failures with no recovery path. Decomposition
# of the 11:
#   - 7 locator/timeout issues (Playwright TimeoutError, action-mediated
#     assertion-on-None) — heal can fix these via live MCP browser inspection
#   - 3 real bugs (WCAG violations, TTI budget, DOM-order assertion) — heal
#     cannot fix these; they are app-behaviour defects
#   - 1 codegen bug (`fixture 'snapshot' not found`) — needs Step 8 retry,
#     not heal
#
# The classifier below splits a `TestRunEntry` into one of:
#   - locator_timeout    — Playwright TimeoutError on locator action
#   - tbd_unresolvable   — JIT runtime exhausted bundle + LLM and gave up
#   - assertion_value    — bare assertion mismatch (e.g. `assert None == 'x'`,
#                          typically downstream of a locator finding the wrong
#                          element); treated as healable because the cause is
#                          usually upstream locator drift
#   - wcag_violation     — axe-core / WCAG audit reported issues
#   - tti_budget         — performance budget assertion
#   - fixture_missing    — pytest fixture lookup failure (codegen drift)
#   - import_error       — ModuleNotFoundError / ImportError at collection
#   - dom_order          — order-sensitive DOM assertion (e.g. `is_above is True`)
#   - unknown            — defaults to healable so we never lose a fix
#                          opportunity to a classifier gap
#
# The classifier is a PURE FUNCTION over `entry.message` + `entry.traceback`
# strings. No side effects, easy to unit-test. Anything classified as
# locator_timeout / tbd_unresolvable / assertion_value / unknown counts
# toward the heal queue; everything else flows directly to bug-candidates
# as a "real bug" without consuming heal budget.
#
# Operator escape: set `QTEA_HEAL_ALL=1` to bypass the classifier and
# heal every failure (useful for debugging the classifier itself).

_FAILURE_CLASS_HEALABLE = frozenset({
    "locator_timeout", "tbd_unresolvable", "assertion_value", "unknown",
})

_CLASSIFY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # JIT runtime fail-fast — the bundle was exhausted, LLM re-resolve gave
    # up. Heal with MCP can interact (hover/click) then snapshot to find the
    # right selector for elements not visible in initial AOM.
    ("tbd_unresolvable", re.compile(
        r"qtea JIT runtime: could not resolve locator", re.I,
    )),
    # Playwright TimeoutError on any Locator action (get_attribute, click,
    # select_option, etc.). The runtime template's bundle-fallback already
    # tried alternatives + re-resolved; reaching this stage means we need
    # MCP-driven live inspection.
    ("locator_timeout", re.compile(
        r"playwright[\._]+_impl[\._]+_errors\.TimeoutError"
        r"|TimeoutError:\s*Locator\.",
        re.I,
    )),
    ("locator_timeout", re.compile(
        r"Timeout\s+\d+ms\s+exceeded.*while\s+waiting", re.I,
    )),
    # Pytest fixture lookup failure — codegen referenced a fixture that
    # isn't available (e.g. pytest-snapshot not installed). Heal cannot
    # fix this; needs a Step 8 codegen retry with a corrected test.
    ("fixture_missing", re.compile(
        r"fixture '[^']+' not found|fixture \".+?\" not found", re.I,
    )),
    # Import errors at collection time. Heal scope forbids touching
    # imports / fixtures / conftest. Word boundaries guard against false
    # positives in AOM snapshots that might quote module names verbatim.
    ("import_error", re.compile(
        r"\bModuleNotFoundError\b|\bImportError\b|\bNo module named\b", re.I,
    )),
    # WCAG / accessibility audit. axe-core results are app behaviour;
    # rewriting the test won't change the violation count.
    ("wcag_violation", re.compile(
        r"WCAG\s*[\d\.]+|wcag\d|axe-core|accessibility violation",
        re.I,
    )),
    # Performance budget. A heal pass can't make the SUT faster.
    # `\bTTI\b` requires word boundaries — bare `TTI` matched inside
    # words like "settings" (seTTIngs) and false-flagged any test whose
    # AOM dump contained UI text with that substring.
    ("tti_budget", re.compile(
        r"\bTTI\b|exceeds budget of \d+ms|p9[05] (?:latency|tti|response)",
        re.I,
    )),
    # Order-sensitive DOM assertion (typically `is_above`, `is_before`).
    # These are app-behaviour assertions — heal cannot reorder the DOM.
    ("dom_order", re.compile(
        r"(?:appear\s+(?:before|above)|DOM\s+order|is_above|is_before)",
        re.I,
    )),
    # Bare assertion mismatch — usually a downstream symptom of locator
    # drift (wrong element found → wrong value). Treat as healable: if
    # heal can re-target the locator, the assertion will pass.
    ("assertion_value", re.compile(
        r"^\s*AssertionError|assert\s+\S+\s*(?:==|is|!=)",
        re.I | re.MULTILINE,
    )),
)


def _classify_failure(entry: TestRunEntry) -> str:
    """Return one of the classes above based on entry.message + entry.traceback.

    First matching pattern wins. Order matters — more-specific patterns
    (e.g. `qtea JIT runtime`) come before more-general ones (e.g. bare
    AssertionError). Returns ``"unknown"`` when nothing matches; the heal
    gate treats unknown as healable so a classifier gap never blocks a
    fix opportunity.
    """
    haystack = "\n".join(filter(None, (entry.message, entry.traceback)))
    if not haystack:
        return "unknown"
    for label, pat in _CLASSIFY_PATTERNS:
        if pat.search(haystack):
            return label
    return "unknown"


def _partition_failures(
    failing: list[TestRunEntry],
) -> tuple[list[TestRunEntry], list[tuple[TestRunEntry, str]]]:
    """Split ``failing`` into (healable, real_bugs).

    ``real_bugs`` carries (entry, class_label) so the caller can record
    the rationale in heal-log.jsonl without re-classifying.

    Operator escape: ``QTEA_HEAL_ALL=1`` returns ``(failing, [])`` —
    skips classification and heals everything. Use when the classifier
    itself is suspected of false-positively excluding a real heal target.
    """
    if os.environ.get("QTEA_HEAL_ALL") == "1":
        return list(failing), []
    healable: list[TestRunEntry] = []
    real_bugs: list[tuple[TestRunEntry, str]] = []
    for entry in failing:
        cls = _classify_failure(entry)
        if cls in _FAILURE_CLASS_HEALABLE:
            healable.append(entry)
        else:
            real_bugs.append((entry, cls))
    return healable, real_bugs


def _build_bug_candidates(failing: list[TestRunEntry]) -> dict:
    now = datetime.now(UTC).isoformat()
    out = {"candidates": []}
    for f in failing:
        out["candidates"].append({
            "id": f"BC-{f.id}",
            "test_id": f.id,
            "title": f.name,
            "file": f.file,
            "status": f.status,
            "message": f.message,
            "traceback": f.traceback,
            "tc_refs": [],
            "attachments": f.attachments,
            "first_seen": now,
        })
    return out


def _summarize_resolver_spend(jit_cache_dir: Path) -> dict | None:
    """Read ``<jit_cache_dir>/resolver-spend.jsonl`` and build a summary
    block for ``run-results.json``. Returns None when no spend file was
    produced (no JIT runtime ran, or no resolution events fired).

    Telemetry shape kept narrow on purpose — counts, totals, and hits per
    tier. No selectors, page URLs, or snapshot bodies (privacy + size).
    """
    p = jit_cache_dir / "resolver-spend.jsonl"
    if not p.is_file():
        return None
    tier_hits = {1: 0, 2: 0, 3: 0, 4: 0}
    total_input = 0
    total_output = 0
    unresolvable = 0
    fallback_promoted_count = 0
    durations_ms: list[int] = []
    models: set[str] = set()
    count = 0
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                count += 1
                tier = entry.get("tier")
                if tier in tier_hits:
                    tier_hits[tier] += 1
                total_input += int(entry.get("input_tokens") or 0)
                total_output += int(entry.get("output_tokens") or 0)
                if entry.get("model"):
                    models.add(entry["model"])
                if entry.get("duration_ms") is not None:
                    durations_ms.append(int(entry["duration_ms"]))
                if entry.get("success") is False:
                    unresolvable += 1
                if entry.get("fallback_promoted"):
                    fallback_promoted_count += 1
    except OSError:
        return None
    if count == 0:
        return None
    # Cost estimation reuses the existing pricing table if available;
    # otherwise the consumer can compute it from input/output tokens.
    est_cost_usd: float | None = None
    try:
        from qtea.llm.cost import estimate_cost  # type: ignore[import-not-found]
        for m in (models or {""}):
            est_cost_usd = (est_cost_usd or 0.0) + estimate_cost(
                m, total_input, total_output,
            )
    except Exception:
        est_cost_usd = None
    return {
        "total_resolutions": count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "tier_1_hits": tier_hits[1],
        "tier_2_hits": tier_hits[2],
        "tier_3_hits": tier_hits[3],
        "tier_4_hits": tier_hits[4],
        "unresolvable_count": unresolvable,
        "fallback_promoted_count": fallback_promoted_count,
        "models": sorted(models) or None,
        "median_duration_ms": (
            sorted(durations_ms)[len(durations_ms) // 2] if durations_ms else None
        ),
        "est_cost_usd": est_cost_usd,
    }


def _collect_hitl_pending(jit_cache_dir: Path) -> list[dict]:
    """Read every ``hitl-pending-*.json`` file the JIT runtime dropped during
    test execution. Each file represents an unresolvable TBD that needs a
    human-in-the-loop selector OR a structured bug candidate.

    Returns the parsed dicts (one per TBD); files that fail to parse are
    skipped with a warning. The files are NOT deleted here — Phase 4's
    HITL prompt deletes the ones that get answered; the rest persist for
    the bug-candidates emission downstream.
    """
    if not jit_cache_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(jit_cache_dir.glob("hitl-pending-*.json")):
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("step09.hitl_pending_unreadable", path=str(p), error=str(e))
            continue
        entry["_pending_path"] = str(p)
        out.append(entry)
    return out


def _hitl_resolve_unresolvable(
    pendings: list[dict], *,
    dev_locators_path: Path | None,
    no_hitl: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Surface each unresolved TBD to the user on a TTY, write their
    answer to ``dev-locators.json`` (next run uses it as Tier 1), and
    delete the pending file. Non-TTY / ``no_hitl=True`` runs leave every
    pending in place — caller emits them as structured bug candidates.

    Returns ``(resolved, remaining)`` — ``resolved`` were answered by the
    user, ``remaining`` are still unresolved and flow into bug-candidates.
    """
    import sys

    import os
    is_tty = (sys.stdin is not None and sys.stdin.isatty()) or os.environ.get("QTEA_UI_MODE")
    if not is_tty or no_hitl or not pendings:
        return [], pendings

    resolved: list[dict] = []
    remaining: list[dict] = []
    log.info("step09.hitl_pending_count", count=len(pendings))
    print(
        f"\n[qtea] {len(pendings)} locator(s) the JIT runtime could not "
        f"resolve. You can supply a selector for each, or press ENTER to skip "
        f"(skipped TBDs become bug-candidate entries for Step 9).\n",
        flush=True,
    )
    for entry in pendings:
        intent = entry.get("intent") or "(no intent)"
        constant = entry.get("constant_name") or "(unknown)"
        page_url = entry.get("page_url") or "(unknown)"
        print(f"  TBD: {constant} — {intent}")
        print(f"       page: {page_url}")
        try:
            answer = input("       selector (or ENTER to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer:
            remaining.append(entry)
            continue
        if answer.startswith("//") or answer.startswith("xpath=") or "By.XPATH" in answer:
            print("       [rejected] XPath selectors are forbidden by qtea.")
            remaining.append(entry)
            continue
        entry["_user_selector"] = answer
        resolved.append(entry)
        # Best-effort: also remove the pending file so next runs don't re-prompt.
        with contextlib.suppress(OSError):
            Path(entry.get("_pending_path", "")).unlink(missing_ok=True)

    if resolved and dev_locators_path is not None:
        _append_resolved_to_dev_locators(resolved, dev_locators_path)
    return resolved, remaining


def _append_resolved_to_dev_locators(
    resolved: list[dict], dev_locators_path: Path,
) -> None:
    """Merge HITL answers into dev-locators.json so the next run's
    Tier 1 picks them up without re-prompting. File schema:
    ``{"locators": {"CONST_NAME": {"selector": "...", "source": "hitl"}}}``."""
    try:
        if dev_locators_path.exists():
            raw = json.loads(dev_locators_path.read_text(encoding="utf-8"))
        else:
            raw = {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    locators = raw.get("locators")
    if not isinstance(locators, dict):
        locators = {}
        raw["locators"] = locators
    for entry in resolved:
        const = entry.get("constant_name")
        sel = entry.get("_user_selector")
        if not const or not sel:
            continue
        locators[const] = {
            "selector": sel,
            "source": "hitl",
            "intent": entry.get("intent"),
            "page_url": entry.get("page_url"),
        }
    try:
        dev_locators_path.parent.mkdir(parents=True, exist_ok=True)
        dev_locators_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        log.info(
            "step09.hitl_dev_locators_updated",
            path=str(dev_locators_path), added=len(resolved),
        )
    except OSError as e:
        log.warning("step09.hitl_dev_locators_write_failed", error=str(e))


def _format_promoted_substitution(payload: dict | None, selector: str | None) -> str | None:
    """Render a cache entry as the Python expression to substitute for `tbd(...)`.

    Returns the substitution string (a `json.dumps`'d CSS string OR a call
    like `role_locator("link", name="...")`), or None when the entry has no
    representable form. None means "leave the tbd() in place" — the caller
    emits a promotion-blocked bug-candidate.

    For structured payloads, we emit calls to the runtime helpers
    (`role_locator`, `text_locator`, …) defined in
    ``src/qtea/_resources/runtime/qtea_runtime.py.tpl``. The
    codegen-pom-extender ensures the runtime import is already present in
    the POM file (`from tests.qtea_runtime import tbd`); the new
    helpers live in the same module, so we may need to extend that import.
    """
    if isinstance(payload, dict):
        kind = payload.get("kind")
        if kind == "css":
            sel = payload.get("selector") or selector
            if not sel:
                return None
            return json.dumps(sel)
        if kind == "role":
            role = payload.get("role")
            if not role:
                return None
            parts = [f"role_locator({json.dumps(role)}"]
            if payload.get("name"):
                parts.append(f", name={json.dumps(payload['name'])}")
            if payload.get("exact") is True:
                parts.append(", exact=True")
            parts.append(")")
            return "".join(parts)
        if kind in ("text", "label", "placeholder"):
            text = payload.get("text")
            if not text:
                return None
            fn = f"{kind}_locator"
            parts = [f"{fn}({json.dumps(text)}"]
            if payload.get("exact") is True:
                parts.append(", exact=True")
            parts.append(")")
            return "".join(parts)
        if kind == "test_id":
            value = payload.get("value")
            if not value:
                return None
            return f"test_id_locator({json.dumps(value)})"
        return None
    # No payload — fall back to the legacy CSS-string path.
    if not selector:
        return None
    return json.dumps(selector)


def _ensure_runtime_imports(text: str, needed_names: set[str]) -> str:
    """Extend `from tests.qtea_runtime import …` to include `needed_names`.

    No-op when no such import line exists (caller's POM doesn't follow the
    convention; promotion still works for CSS-string substitutions because
    those don't need extra symbols).
    """
    import re as _re

    if not needed_names:
        return text
    pat = _re.compile(
        r"^(from\s+tests\.qtea_runtime\s+import\s+)([^\n]+)$",
        _re.MULTILINE,
    )
    m = pat.search(text)
    if not m:
        return text
    existing = {n.strip() for n in m.group(2).split(",") if n.strip()}
    missing = needed_names - existing
    if not missing:
        return text
    new_imports = ", ".join(sorted(existing | needed_names))
    return text[:m.start()] + m.group(1) + new_imports + text[m.end():]


def _promote_resolved_tbds(
    sut_root: Path, cache_path: Path,
) -> tuple[list[str], list[dict]]:
    """Replace tbd("intent") sentinels with their resolved selectors in-place.

    Returns ``(modified_files, blocked_candidates)``:
      - ``modified_files`` — SUT-relative paths of files actually rewritten.
      - ``blocked_candidates`` — bug-candidate dicts for entries the promoter
        REFUSED to substitute (no passing witness OR fails validation OR
        unrepresentable payload). The caller appends these to bug-candidates.json.

    Gating (the safety net added after the run-20260621 regression):
      1. ``passing_witnesses`` must be non-empty — the selector has been used
         by at least one test that PASSED in this attempt. Selectors that
         only failing tests touched never reach SUT source.
      2. ``validate_selector_payload(payload, selector)`` must return ok —
         catches Playwright debug-print syntax (`link "..."`), unbalanced
         brackets, injection markers, and structurally-malformed payloads.
      3. ``_format_promoted_substitution`` must yield a valid Python
         expression for the substitution. Structured payloads emit
         `role_locator(...)` / `text_locator(...)` / etc.; the runtime
         import line is extended to include the needed helpers.
    """
    import re as _re

    from qtea.jit_resolver import validate_selector_payload
    from qtea.tbd_scanner import scan_tbd_intents

    blocked: list[dict] = []
    if not cache_path.exists():
        return [], blocked
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], blocked

    intent_to_entry: dict[str, dict] = {}
    for e in (data.get("entries") or []):
        intent = e.get("intent")
        if not intent:
            continue
        if e.get("source", "none") == "none":
            continue
        # Gate 1: passing-test witness required. The cache file may carry
        # entries with no witnesses yet (resolved + used in a failing test);
        # those stay as tbd() this round but may earn witnesses on a later run.
        witnesses = e.get("passing_witnesses")
        if not isinstance(witnesses, list) or not witnesses:
            blocked.append({
                "id": f"BC-promotion-blocked-{e.get('constant_name', 'unknown')}",
                "kind": "promotion-blocked",
                "reason": "no_passing_witness",
                "intent": intent,
                "constant_name": e.get("constant_name"),
                "cached_selector": e.get("selector"),
                "cached_payload": e.get("payload"),
                "remediation": (
                    "No passing test has used this resolution yet. The "
                    "selector stays as tbd() so the JIT runtime keeps "
                    "resolving it. Add a test that exercises it OR fix the "
                    "test that triggered the resolution."
                ),
            })
            continue
        # Gate 2: structural validation.
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else None
        ok, why = validate_selector_payload(payload, e.get("selector"))
        if not ok:
            blocked.append({
                "id": f"BC-promotion-blocked-{e.get('constant_name', 'unknown')}",
                "kind": "promotion-blocked",
                "reason": "invalid_selector_form",
                "intent": intent,
                "constant_name": e.get("constant_name"),
                "cached_selector": e.get("selector"),
                "cached_payload": payload,
                "validation_reason": why,
                "remediation": (
                    "The cached selector failed validate_selector_payload. "
                    "Drop the bad cache entry (rm locator-cache.json) and "
                    "re-run; the resolver will try again with the updated "
                    "prompt that demands structured payloads."
                ),
            })
            continue
        intent_to_entry[intent] = e

    if not intent_to_entry:
        return [], blocked

    scan_roots = [p for p in (sut_root / d for d in ("src", "tests", "pages")) if p.is_dir()]
    if not scan_roots:
        scan_roots = [sut_root]
    hits = scan_tbd_intents(scan_roots, sut_root=sut_root)

    by_file: dict[Path, list] = {}
    for hit in hits:
        if hit.intent in intent_to_entry:
            by_file.setdefault(hit.file, []).append(hit)

    # Map runtime helper kinds to import names — added to the POM's
    # `from tests.qtea_runtime import ...` line when the substitution
    # uses them. CSS / no-payload substitutions don't need any extra imports.
    _KIND_TO_HELPER = {
        "role": "role_locator",
        "text": "text_locator",
        "label": "label_locator",
        "placeholder": "placeholder_locator",
        "test_id": "test_id_locator",
    }

    modified: list[str] = []
    for file_path, file_hits in by_file.items():
        abs_path = (sut_root / file_path) if not file_path.is_absolute() else file_path
        rel_str = str(file_path) if not file_path.is_absolute() else str(file_path.relative_to(sut_root))
        text = abs_path.read_text(encoding="utf-8")
        new_text = text
        helper_imports: set[str] = set()
        for hit in file_hits:
            entry = intent_to_entry[hit.intent]
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
            substitution = _format_promoted_substitution(payload, entry.get("selector"))
            if substitution is None:
                blocked.append({
                    "id": f"BC-promotion-blocked-{entry.get('constant_name', 'unknown')}",
                    "kind": "promotion-blocked",
                    "reason": "unrepresentable_payload",
                    "intent": hit.intent,
                    "constant_name": entry.get("constant_name"),
                    "cached_selector": entry.get("selector"),
                    "cached_payload": payload,
                    "remediation": "Payload could not be rendered; investigate jit_resolver / cache state.",
                })
                continue
            if isinstance(payload, dict):
                helper = _KIND_TO_HELPER.get(payload.get("kind"))
                if helper:
                    helper_imports.add(helper)
            escaped = _re.escape(hit.intent)
            new_text = _re.sub(
                rf'tbd\((?P<q>["\']){escaped}(?P=q)\)',
                lambda _m, _r=substitution: _r,
                new_text,
            )
        # Add any new helper imports BEFORE writing back.
        if helper_imports:
            new_text = _ensure_runtime_imports(new_text, helper_imports)
        if new_text != text:
            abs_path.write_text(new_text, encoding="utf-8")
            modified.append(rel_str)
    return modified, blocked


def _bug_candidates_for_dev_pool_drift(quarantine_log: Path) -> list[dict]:
    """Read ``dev-pool-quarantine.jsonl`` and emit one bug-candidate per
    dev-pool selector that failed at action time.

    The JIT runtime writes one JSONL record per quarantine event. Each
    candidate guides the user to update the dev-locators file OR let
    qtea re-resolve fresh (delete the entry from dev-locators.json).
    """
    if not quarantine_log.exists():
        return []
    try:
        lines = quarantine_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        const = record.get("constant_name") or "unknown"
        matched = record.get("matched_constant")
        # Dedupe within a single run: many tests may hit the same drift.
        key = f"{const}::{matched or ''}::{record.get('intent', '')}"
        if key in seen:
            continue
        seen.add(key)
        intent = record.get("intent") or ""
        stale = record.get("stale_selector")
        out.append({
            "id": f"BC-dev-locator-drifted-{matched or const}",
            "test_id": f"dev-locator-drifted:{matched or const}",
            "title": (
                f"Dev locator {(matched or const)!r} drifted at runtime"
            ),
            "file": record.get("test_file"),
            "status": "error",
            "kind": "dev-locator-drifted",
            "message": (
                f"Tier 1b dev-pool selector for intent {intent!r} "
                f"({stale!r}) failed at action time on "
                f"{record.get('page_url') or '(unknown URL)'}: "
                f"{record.get('exception') or 'TimeoutError'}. "
                f"The JIT runtime fell back to the LLM resolver under a "
                f"shadow cache key; the dev-locators entry was NOT "
                f"overwritten. Update the selector for "
                f"{(matched or const)!r} in dev-locators.json, OR remove "
                f"that entry so qtea resolves fresh on the next run."
            ),
            "traceback": None,
            "tc_refs": [],
            "attachments": [],
            "first_seen": record.get("ts"),
            "constant_name": const,
            "matched_constant": matched,
            "intent": intent,
            "page_url": record.get("page_url"),
            "stale_selector": stale,
            "pool_score": record.get("pool_score"),
        })
    return out


def _bug_candidates_for_unresolvable_tbds(
    remaining: list[dict], dev_locators_path: Path | None = None,
) -> list[dict]:
    """Emit a ``locator-unresolvable`` bug-candidate per HITL-unanswered
    TBD. Step 9's classifier sees these alongside test failures.
    """
    now = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for entry in remaining:
        const = entry.get("constant_name") or "unknown"
        intent = entry.get("intent") or ""
        locators_hint = (
            f"Provide a selector via {str(dev_locators_path)!r}"
            if dev_locators_path
            else "Provide a selector via --dev-locators or QTEA_DEV_LOCATORS"
        )
        out.append({
            "id": f"BC-locator-unresolvable-{const}",
            "test_id": f"locator-unresolvable:{const}",
            "title": f"Locator could not be resolved: {const}",
            "file": entry.get("test_file"),
            "status": "error",
            "kind": "locator-unresolvable",
            "message": (
                f"The JIT runtime could not find any element matching "
                f"intent {intent!r} on {entry.get('page_url') or '(unknown URL)'}. "
                f"{locators_hint} under "
                f"key {const!r}, or update the test to remove the TBD."
            ),
            "traceback": None,
            "tc_refs": [],
            "attachments": [],
            "first_seen": now,
            "constant_name": const,
            "intent": intent,
            "page_url": entry.get("page_url"),
        })
    return out


def _lazy_probe_heal_mcp(
    server_name: str,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, float]:
    """Warm + probe one MCP server just before the first heal-agent invocation.

    ``probe_server`` spawns the server and lets it run for ~30 s, then kills
    it. The side effect is a warm npx cache and a completed Playwright
    binary check, so when the Agent SDK later spawns its own copy of the
    server it reaches `connected` faster — eliminating the race where the
    heal agent burns turns calling ``WaitForMcpServers`` before MCP is up.

    ``env`` is an optional per-call MCP env overlay (e.g.
    ``{"QTEA_STORAGE_STATE_ARG": "--storage-state=/abs/path"}``).
    Threaded through to ``load_mcp_config`` so the rendered MCP server
    args reflect per-run substitutions (e.g. the storage-state file path
    Step 9 just resolved) without mutating ``os.environ``.

    Returns ``(ok, detail, elapsed_s)``. On failure the caller logs + skips
    the heal loop (heal is best-effort — a missing Playwright MCP shouldn't
    fail the whole Step 9 run; the failing tests still flow to Step 10 as
    bug candidates).

    Centralised in a module-level helper so unit tests can monkey-patch
    ``s09_execute._lazy_probe_heal_mcp`` without touching the MCP plumbing.
    """
    import time as _time

    from qtea.mcp_manager import load_mcp_config, probe_server

    started = _time.monotonic()
    try:
        all_servers = load_mcp_config(env=env)
    except (FileNotFoundError, OSError, ValueError) as e:
        return False, f"could not load .mcp.json: {e}", 0.0

    server = all_servers.get(server_name)
    if server is None:
        return False, f"{server_name!r} not declared in .mcp.json", 0.0

    ok, detail = probe_server(server)
    elapsed = round(_time.monotonic() - started, 2)
    return ok, detail or "", elapsed


class ExecuteStep(Step):
    number = 9
    name = "execute"
    timeout_s = step_timeout(9)
    # Playwright MCP is only consumed by the `polyglot-test-fixer` heal
    # agent (`enable_mcp=True` call site below). Heal only runs when the
    # first test pass produces failing tests AND those failures aren't
    # synthetic runner-failure entries. On green runs (all tests pass,
    # or runner_only_failure short-circuits) the agent is never spawned,
    # so we'd be paying the 5-15s MCP probe + npx-cache warmup for no
    # benefit. Probing lazily inside :meth:`run` (just before the first
    # heal call) keeps the warmup contiguous with the SDK spawn (the
    # `pending` race the eager probe was meant to avoid) without taxing
    # green runs.
    mcp_servers_required: frozenset[str] = frozenset()
    # Server name probed lazily. Kept as a class constant so tests can
    # override without monkey-patching string literals.
    _LAZY_MCP_SERVER: str = "playwright"

    def pre_attempt_cleanup(self, ctx: StepContext, attempt: int) -> None:
        """Rotate ``heal-log.jsonl`` so attempt 2 doesn't append on top of
        attempt 1's entries. Step 9 reads only the current heal-log; prior
        attempts are archived to ``heal-log.attempt-N.jsonl`` so the forensic
        trail survives without polluting the classifier's input."""
        out_dir = self.out_dir(ctx.workspace)
        heal_log = out_dir / "self-heal" / "heal-log.jsonl"
        if heal_log.exists() and heal_log.stat().st_size > 0:
            prior = attempt - 1
            archive = out_dir / "self-heal" / f"heal-log.attempt-{prior}.jsonl"
            try:
                heal_log.rename(archive)
                log.info(
                    "step09.heal_log_rotated",
                    archived_attempt=prior,
                    archive_path=str(archive),
                )
            except OSError as e:
                log.warning("step09.heal_log_rotate_failed", error=str(e))

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        heal_log_path = out_dir / "self-heal" / "heal-log.jsonl"
        heal_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Cross-attempt narrowing (Tasks 2 + 4 + 5). When this is a retry,
        # we can:
        #   - skip the SUT install (Task 4 — lockfile hasn't changed; heal
        #     commits don't touch lockfiles);
        #   - narrow the initial test run to only the previously-failing
        #     tests minus the ones the prior heal flagged as real bugs
        #     (Tasks 2 + 5);
        #   - short-circuit the heal call for those real-bug tests so the
        #     LLM doesn't burn cost re-discovering "nothing to fix".
        #
        # ``record.attempts`` is incremented in ``base.py:_attempt`` BEFORE
        # ``run()`` is called, so a value > 1 is a true retry.
        _record = ctx.state.steps.get(self.number)
        _current_attempt = _record.attempts if _record else 1
        _prior_state: dict | None = None
        if _current_attempt > 1:
            _prior_state = _load_attempt_state(out_dir, _current_attempt - 1)
            if _prior_state:
                log.info(
                    "step09.prior_attempt_state_loaded",
                    attempt=_current_attempt,
                    prior_failing=len(_prior_state.get("failing_ids", [])),
                    prior_no_patch=len(_prior_state.get("no_patch_ids", [])),
                )

        # Pre-flight: SUT must be present + on the qtea branch. Step 8
        # already wrote into it; step 9 runs tests + heals against it.
        if not ctx.workspace.sut.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="SUT not materialized (run from step 1 to re-clone)",
            )
        if not (ctx.workspace.sut / ".git").exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"SUT at {ctx.workspace.sut} is not a git repo — the "
                    f"qtea branch is missing. Re-run from step 1."
                ),
            )

        # Step 8 committed qtea_*-prefixed files into the SUT on the
        # qtea branch. We don't need a separate codegen_root anymore;
        # the SUT itself is the source of truth.
        # Sanity-check that step 8's manifest exists so we fail fast when
        # someone runs --only-step 9 on a fresh workspace.
        step8_manifest = ctx.workspace.step_dir(8) / "generated-files.json"
        if not step8_manifest.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "step 9 requires step 8's generated-files.json manifest. "
                    "Run step 8 first (drop --only-step 9, or use "
                    "--from-step 8)."
                ),
            )

        research = _research_payload(ctx)
        index = _load_index(ctx)
        framework = _framework(research, index)
        detected_cmd = _detected_command(research)

        sut_env_keys = research.get("sut_env_keys") or []
        if sut_env_keys:
            missing = [k for k in sut_env_keys if k not in os.environ]
            if missing:
                env_res = research.get("env_resolution")
                if env_res:
                    log.warning(
                        "step09.env_missing",
                        keys=missing,
                        strategies_tried=list(env_res.get("sources", {}).values()),
                        hint="These keys were not resolved by any strategy in Step 6. "
                             "Provide them via --env-file, host environment, or "
                             "Azure DevOps Variable Groups.",
                    )
                else:
                    log.warning("step09.env_missing", keys=missing)

        # Resolve where qtea-generated tests live inside the SUT (steps 7+8
        # already wrote them there). No copy — just compute the path.
        sut_inventory = research.get("sut_inventory") or {}
        active_module = _active_module(sut_inventory)
        isolated = bool(getattr(ctx.options, "isolated_tests", False))
        sut_tests = _sut_tests_dir(
            ctx.workspace.sut,
            active_module=active_module,
            isolated=isolated,
        )
        log.info(
            "step09.tests_resolved",
            destination=str(sut_tests),
            active_module=(active_module or {}).get("name"),
            isolated=isolated,
        )
        runtime_env = {"QTEA_TESTS_DIR": str(sut_tests)}

        # JIT runtime plugin env wiring. The vendored `tests/qtea_runtime.py`
        # (when present) reads these vars to discover the cache, optional
        # dev-supplied locator file, resolver port/token, and timeout defaults.
        # SECURITY: ANTHROPIC_API_KEY is deliberately NOT re-exported here —
        # safe_subprocess_env() strips it because the pytest subprocess executes
        # untrusted SUT test code that could exfiltrate the key via os.environ.
        # The LLM resolver path works WITHOUT the key in the SUT env because
        # the parent process (here in step 9) starts a ResolverServer on a
        # local loopback port; the pytest plugin reaches it via the short-lived
        # per-run QTEA_RESOLVER_TOKEN (set further down, once the server
        # binds a port). Tier order at runtime: dev-locators → cache →
        # in-process heuristic → ResolverServer (LLM) → HITL/fail-fast.
        jit_cache_dir = ctx.workspace.root / "locator-cache"
        # Dir creation deferred to the `if jit_runtime_vendored:` branch below —
        # non-Playwright stacks never load the vendored runtime, so they don't
        # need the dir and shouldn't pollute the workspace with an empty one.
        # All write sites (runtime template's _write_cache / _append_spend_line /
        # _write_hitl_pending and jit_resolver.write_cache) mkdir on demand.
        runtime_env["QTEA_CACHE_DIR"] = str(jit_cache_dir)
        runtime_env["QTEA_RUN_ID"] = ctx.workspace.run_id
        resolver_model = os.environ.get("QTEA_RESOLVER_MODEL")
        if resolver_model:
            runtime_env["QTEA_RESOLVER_MODEL"] = resolver_model
        timeout_ms = os.environ.get("QTEA_DEFAULT_TIMEOUT_MS")
        if timeout_ms:
            runtime_env["QTEA_DEFAULT_TIMEOUT_MS"] = timeout_ms
        # Dev-locators file: --dev-locators CLI flag wins; env var next; default
        # to workspace/locator-cache/dev-locators.json so HITL answers are never
        # written into the SUT (they're run-workspace artifacts, not SUT source).
        dev_locators_opt = getattr(ctx.options, "dev_locators", None)
        if dev_locators_opt:
            runtime_env["QTEA_DEV_LOCATORS"] = str(dev_locators_opt)
        elif os.environ.get("QTEA_DEV_LOCATORS"):
            runtime_env["QTEA_DEV_LOCATORS"] = os.environ["QTEA_DEV_LOCATORS"]
        else:
            runtime_env["QTEA_DEV_LOCATORS"] = str(jit_cache_dir / "dev-locators.json")

        # Workspace dir for the runtime plugin's same-run storage-state auto-
        # capture (Use case B in storage_state.py). The plugin reads this on
        # first passing test to know where to write storage-state.json.
        runtime_env["QTEA_WORKSPACE_DIR"] = str(ctx.workspace.root)

        # Storage state for Playwright MCP injection. Resolved against the
        # 4-tier precedence (CLI flag > env > SUT convention path > workspace
        # auto-capture). When set, _heal_mcp_env carries the
        # ``--storage-state=<path>`` flag for `.mcp.json` token substitution;
        # when unset, the empty arg is filtered by mcp_manager.
        from qtea import storage_state as _storage_state_mod
        _storage_state_path = _storage_state_mod.resolve(
            sut_root=ctx.workspace.sut,
            workspace_root=ctx.workspace.root,
            cli_opt=getattr(ctx.options, "storage_state", None),
        )
        _heal_mcp_env = {
            "QTEA_STORAGE_STATE_ARG": _storage_state_mod.to_mcp_arg(_storage_state_path),
            "QTEA_MCP_USER_DATA_DIR_ARG": (
                f"--user-data-dir={ctx.workspace.root / 'playwright-mcp'}"
            ),
        }
        if _storage_state_path is not None:
            runtime_env["QTEA_STORAGE_STATE"] = str(_storage_state_path)
            log.info(
                "step09.storage_state_resolved",
                path=_storage_state_mod.mask_path(_storage_state_path),
            )

        # Prepare SUT: run the deterministically-detected install command
        # (poetry install / npm ci / mvn install / ...). Idempotent at the
        # package-manager level; the cost on warm runs is a few seconds and
        # buys us "tests work on a fresh clone." Failure here aborts before
        # we burn the test budget — broken deps invariably manifest as
        # opaque test errors that the self-heal loop cannot recover from.
        # install_log_path is bound unconditionally so the auto-install
        # paths below can append to it regardless of whether prepare_sut ran.
        install_log_path = out_dir / "install.log"
        stack_profile = _load_stack_profile(ctx)
        log.info(
            "step09.stack_profile",
            package_manager=stack_profile.package_manager if stack_profile else None,
            pre_install=stack_profile.pre_install_command if stack_profile else None,
            install=stack_profile.install_command if stack_profile else None,
            wrapper=stack_profile.wrapper_prefix if stack_profile else None,
            venv_path=stack_profile.venv_path if stack_profile else None,
            detection_signal=stack_profile.detection_signal if stack_profile else None,
        )
        # Task 4: skip install on retry when the dependency state is byte-
        # identical to the attempt that ran the install. Lockfile mtime +
        # size + package_manager give us a cheap-but-sufficient signature.
        install_sig = _compute_install_sig(ctx.workspace.sut, stack_profile)
        _skip_install = (
            _prior_state is not None
            and install_sig is not None
            and _prior_state.get("install_sig") == install_sig
        )
        if stack_profile and stack_profile.install_command and not _skip_install:
            prep = prepare_sut(
                stack_profile,
                cwd=ctx.workspace.sut,
                timeout_s=900,
            )
            install_log_path.write_text(
                f"$ {prep.command}\n\n# STDOUT\n{prep.stdout}\n\n# STDERR\n{prep.stderr}\n",
                encoding="utf-8",
            )
            if not prep.ok():
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[install_log_path],
                    error=(
                        f"SUT install failed: `{prep.command}` exited with "
                        f"{prep.exit_code}. See install.log."
                    ),
                )
            log.info(
                "step09.install_done",
                command=prep.command,
                duration_s=prep.duration_s,
            )
        elif _skip_install:
            log.info(
                "step09.install_skipped_retry",
                attempt=_current_attempt,
                install_sig=install_sig,
                reason="dependency signature matches prior attempt",
            )

        # Venv detection / wrapper_prefix swap must STILL fire even when
        # install was skipped — pytest + playwright invocations downstream
        # rely on ``stack_profile.wrapper_prefix`` pointing at the venv
        # bin dir. Without this branch, attempt 2 would fall back to
        # ``poetry run pytest`` (slow path) instead of ``.venv/bin/pytest``.
        if stack_profile and stack_profile.venv_path:
            venv_abs = ctx.workspace.sut / stack_profile.venv_path
            log.info(
                "step09.venv_check",
                venv_path=str(venv_abs),
                exists=venv_abs.exists(),
                is_dir=venv_abs.is_dir() if venv_abs.exists() else False,
            )
            if venv_abs.exists():
                # Bypass poetry's venv resolution for all subsequent
                # commands (playwright install, pytest). After
                # prepare_sut created .venv, invoke directly via its
                # bin dir — equivalent to activating the venv.
                bin_dir = str(
                    venv_abs / ("Scripts" if os.name == "nt" else "bin")
                )
                stack_profile = dataclasses.replace(
                    stack_profile,
                    wrapper_prefix=bin_dir,
                    package_manager="pip",
                )
                log.info("step09.venv_activated", bin_dir=bin_dir)

        # Playwright stacks need browser binaries installed after the
        # package install. Idempotent — skips if already present.
        _PW_FRAMEWORKS = {"playwright-py", "playwright-ts", "playwright-js", "playwright-java"}
        if stack_profile and framework in _PW_FRAMEWORKS:
            pw_cmd = wrap_command(stack_profile, "playwright install chromium")
            log.info("step09.playwright_install", command=pw_cmd)
            rc, out, err, _dur = execute_command(
                pw_cmd, cwd=ctx.workspace.sut, timeout_s=400,
                isolate_venv=bool(
                    (stack_profile.package_manager or "").lower()
                    in PYTHON_VENV_MANAGERS
                ),
            )
            with install_log_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n$ {pw_cmd}\n# exit_code: {rc}\n"
                    f"# STDOUT\n{out}\n\n# STDERR\n{err}\n"
                )
            if rc != 0:
                log.warning(
                    "step09.playwright_install_failed",
                    exit_code=rc, stderr=err[:300],
                )

        if stack_profile is None:
            log.warning(
                "step09.no_stack_profile",
                hint="step06 stack_profile.json missing; running tests with "
                     "bare framework command (no package-manager wrapper).",
            )

        # Pre-install known-safe missing deps surfaced by Step 6's audit
        # (research.dependency_warnings). High-confidence entries (`known`
        # confidence — module is in the curated _PYTEST_PLUGIN_PROVIDERS
        # table) get installed and committed up-front so the first pytest
        # run doesn't blow up at collection. `guessed` entries are logged
        # only — the runtime recovery path handles them with a HITL prompt
        # if/when pytest actually fails on them.
        no_auto_deps = bool(getattr(ctx.options, "no_auto_deps", False))
        dep_warnings = research.get("dependency_warnings") or []
        for w in (w for w in dep_warnings if w.get("confidence") == "guessed"):
            log.warning(
                "step09.dep_warning_unverified",
                module=w.get("module"),
                suggested_package=w.get("suggested_package"),
                source_file=w.get("source_file"),
                hint=w.get("suggested_install"),
            )
        known_warnings = [
            w for w in dep_warnings
            if w.get("confidence") == "known" and w.get("suggested_package")
        ]
        if known_warnings and no_auto_deps:
            log.warning(
                "step09.auto_install_skipped",
                reason="--no-auto-deps",
                packages=[w["suggested_package"] for w in known_warnings],
            )
        elif known_warnings:
            pkg_mgr = stack_profile.package_manager if stack_profile else None
            installed_pre: list[str] = []
            for w in known_warnings:
                ok, summary = _run_dep_install(
                    pkg_mgr, w["suggested_package"],
                    ctx.workspace.sut, install_log_path,
                    profile=stack_profile,
                )
                log.info(
                    "step09.auto_install_dep",
                    phase="pre",
                    module=w["module"],
                    package=w["suggested_package"],
                    confidence="known",
                    success=ok,
                    summary=summary,
                )
                if ok:
                    installed_pre.append(w["suggested_package"])
            if installed_pre:
                sha = commit_step(
                    ctx.workspace.sut, 9, "execute",
                    message_detail=(
                        f"pre-install missing test deps: "
                        f"{', '.join(installed_pre)}"
                    ),
                )
                log.info(
                    "step09.auto_install_commit",
                    phase="pre", packages=installed_pre, sha=sha,
                )

        # Resolver bridge: when the JIT runtime is vendored into the SUT,
        # start a parent-side TCP server that the pytest plugin can call to
        # resolve TBDs. This is the security-correct path for the LLM tier:
        # the Anthropic API key stays in the parent process and is never
        # exported into the SUT subprocess (where safe_subprocess_env strips
        # it anyway). The pytest plugin's _call_resolver dispatcher picks
        # the socket path automatically when QTEA_RESOLVER_PORT is set.
        jit_runtime_vendored = (
            ctx.workspace.sut / "tests" / "qtea_runtime.py"
        ).is_file()
        _resolver_server = None
        if jit_runtime_vendored:
            jit_cache_dir.mkdir(parents=True, exist_ok=True)
            _resolver_server = ResolverServer(
                cache_dir=jit_cache_dir,
                run_id=ctx.workspace.run_id,
                model=runtime_env.get("QTEA_RESOLVER_MODEL"),
                dev_locators_path=Path(runtime_env["QTEA_DEV_LOCATORS"]),
            )
            _resolver_server.start()
            runtime_env["QTEA_RESOLVER_PORT"] = str(_resolver_server.port)
            runtime_env["QTEA_RESOLVER_TOKEN"] = _resolver_server.token
            log.info(
                "step09.resolver_server_started",
                port=_resolver_server.port,
            )

            # Task 3: pre-warm the dev-pool tier of the JIT cache. We
            # already know every TBD intent codegen emitted (it's in
            # ``artifacts/step08/tbd-index.json``) and we have the dev-
            # locator pool loaded. Running the fuzzy match now (in the
            # parent, off the test's critical path) means pytest hits a
            # populated cache for every tier-1b-resolvable intent
            # instead of paying a snapshot+score round-trip per test.
            # Idempotent on retry — entries already present in the
            # cache are not overwritten.
            try:
                _prewarm_count = _prewarm_jit_cache_dev_pool(
                    ctx=ctx,
                    jit_cache_dir=jit_cache_dir,
                    dev_locators_path=Path(runtime_env["QTEA_DEV_LOCATORS"]),
                )
                if _prewarm_count > 0:
                    log.info(
                        "step09.jit_cache_prewarmed",
                        count=_prewarm_count,
                        tier="dev-pool",
                    )
            except Exception as _e:  # noqa: BLE001 — best-effort, never poison run
                log.warning("step09.jit_cache_prewarm_failed", error=str(_e))

        # Task 5: narrow attempt 2's initial test run to the subset that
        # failed in attempt 1 minus tests flagged as real bugs (Task 2).
        # Saves the bulk of the previously-passing tests' wall time —
        # those passed once on identical code, no need to re-prove it.
        _retry_subset_count: int | None = None
        if _prior_state:
            _prior_failing = _prior_state.get("failing", []) or []
            _prior_no_patch = set(_prior_state.get("no_patch_ids", []) or [])
            _rerun_pairs = [
                (e["id"], e["name"]) for e in _prior_failing
                if isinstance(e, dict) and e.get("id") and e.get("name")
                and e["id"] not in _prior_no_patch
            ]
            if _rerun_pairs:
                from types import SimpleNamespace
                _rerun_entries = [
                    SimpleNamespace(id=tid, name=tname)
                    for tid, tname in _rerun_pairs
                ]
                _orig_cmd = detected_cmd
                # Resolve the framework default when codegen didn't pin one,
                # so the narrowing can append a -k / --grep to a real cmd.
                _base_cmd, _ = resolve_command(
                    framework,
                    detected=detected_cmd,
                    cwd=ctx.workspace.sut,
                    profile=stack_profile,
                    marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                )
                detected_cmd = _filter_command_for_tests(_base_cmd, _rerun_entries)
                _retry_subset_count = len(_rerun_pairs)
                log.info(
                    "step09.retry_narrowed_to_subset",
                    attempt=_current_attempt,
                    rerun_count=len(_rerun_pairs),
                    skipped_real_bugs=len(_prior_no_patch),
                    original_cmd=_orig_cmd or "(framework default)",
                    narrowed_cmd=detected_cmd,
                )

        try:
            log.info(
                "step09.test_run_start",
                framework=framework,
                cwd=str(ctx.workspace.sut),
                detected_cmd=detected_cmd or "(none — will use framework default)",
                marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                parallelism=getattr(ctx.options, "parallelism", 0),
                headless=getattr(ctx.options, "headless", True),
                retry_subset_count=_retry_subset_count,
            )
            _applied_marker_filter = _QTEA_PYTEST_MARKER_FILTER
            first = run_tests(
                framework,
                cwd=ctx.workspace.sut,
                detected_command=detected_cmd,
                timeout_s=min(self.timeout_s or 1800, 1800),
                env_extra=runtime_env,
                profile=stack_profile,
                headless=getattr(ctx.options, "headless", True),
                marker_filter=_applied_marker_filter,
                parallelism=getattr(ctx.options, "parallelism", 0),
            )

            # Detect "no tests collected" — a codegen quality failure where
            # Step 8's test scoping filter matched nothing.
            # pytest: exit code 5 = no tests collected.
            # Playwright Test: exit code 1 + total==0 = no matching files.
            _is_empty_collection = (
                first.exit_code == 5
                or (
                    framework in _PW_TEST_FRAMEWORKS
                    and first.exit_code == 1
                    and first.total == 0
                )
            )
            if _is_empty_collection and _applied_marker_filter:
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[],
                    error=(
                        f"{framework} collected 0 tests matching the qtea "
                        f"test filter. This is a codegen defect: Step 8 must "
                        f"generate test files with the 'qtea_' prefix "
                        f"(Playwright Test) or add @pytest.mark.qtea_<phase> "
                        f"to every test function (pytest). Check the test files "
                        f"in the SUT and ensure they follow the naming convention. "
                        f"Override with QTEA_PYTEST_MARKER='' to run without "
                        f"scoping (runs the full SUT suite, not recommended)."
                    ),
                )

            log.info(
                "step09.test_run_done",
                command=first.command,
                cwd=first.cwd,
                exit_code=first.exit_code,
                duration_s=round(first.duration_s, 1),
                totals=first.totals,
            )

            # Persist raw test-runner stdout/stderr as a standalone artifact
            # so humans can diagnose without parsing run-results.json.
            test_output_path = out_dir / "test-output.log"
            with contextlib.suppress(OSError):
                test_output_path.write_text(
                    f"# framework: {framework}\n"
                    f"$ {first.command}\n"
                    f"# exit_code: {first.exit_code}\n"
                    f"# duration: {first.duration_s:.1f}s\n\n"
                    f"--- STDOUT ---\n{first.stdout or '(empty)'}\n\n"
                    f"--- STDERR ---\n{first.stderr or '(empty)'}\n",
                    encoding="utf-8",
                )

            attempts = 1
            patches_applied = 0
            patches_rejected = 0

            failing = _failing_tests(first)
            self_heal_meta: dict[str, dict] = {}

            # Task 2: skip the heal call for tests the prior attempt's
            # heal agent classified as real product bugs (summary ==
            # "no usable patch produced"). Heal would only spend more
            # LLM cycles arriving at the same conclusion. The
            # classification is conservative: only the exact "no usable
            # patch" summary qualifies — agent timeouts, scope violations,
            # and XPath rejections all stay eligible because they indicate
            # the heal could still succeed under different conditions.
            if _prior_state:
                _prior_no_patch = set(_prior_state.get("no_patch_ids", []) or [])
                if _prior_no_patch:
                    _skipped_heals = [e for e in failing if e.id in _prior_no_patch]
                    failing = [e for e in failing if e.id not in _prior_no_patch]
                    for _entry in _skipped_heals:
                        _summary = (
                            "skipped: prior attempt's heal produced no patch "
                            "(classified as real product bug)"
                        )
                        self_heal_meta[_entry.id] = {
                            "attempted": False,
                            "applied": False,
                            "summary": _summary,
                        }
                        try:
                            with heal_log_path.open("a", encoding="utf-8") as _fh:
                                _fh.write(json.dumps({
                                    "test_id": _entry.id,
                                    "file": _entry.file,
                                    "applied": False,
                                    "agent_success": False,
                                    "agent_error": _summary,
                                    "ts": datetime.now(UTC).isoformat(),
                                }, ensure_ascii=False) + "\n")
                        except OSError:
                            pass
                        log.info(
                            "step09.heal_skipped_real_bug",
                            test_id=_entry.id,
                            attempt=_current_attempt,
                        )

            # Skip self-heal when the failure is the synthetic `T-runner-failure`
            # entry — the runner blew up at collection / import time and there is
            # no per-test traceback, no patch site, nothing the fixer agent can
            # produce a diff against. Past behaviour burned ~10 timed-out heal
            # attempts (≈75 min wall-clock + tokens) on exactly these cases
            # before falling through to the runner_only_failure error path below.
            # The classifier output (when present on the entry) flows into that
            # error path so the user sees the missing dep name and install hint.
            runner_only = (
                len(failing) > 0
                and all(r.id == "T-runner-failure" for r in failing)
            )
            # Also skip heal on exit code 3 (pytest internal error) when all
            # failing entries are infrastructure errors — no real test ran, so
            # there is no POM/locator to patch.
            if not runner_only and first.exit_code == 3:
                all_infra = (
                    len(failing) > 0
                    and all(r.runner_failure is not None for r in failing)
                )
                if all_infra:
                    runner_only = True

            # Runtime dep-recovery: on a missing_module runner failure, attempt
            # one install + re-run before declaring defeat. This catches gaps
            # Step 6's static audit missed (dynamic imports, conftest-only deps
            # the SUT layout heuristic skipped). Bounded to ONE retry per Step 9
            # invocation — if the re-run still produces runner_only, fall through
            # to the existing heal_skip behavior.
            if runner_only and not no_auto_deps:
                rf = (failing[0].runner_failure or {})
                module = rf.get("module") if rf.get("kind") == "missing_module" else None
                if module:
                    confidence = "known" if module in _PYTEST_PLUGIN_PROVIDERS else "guessed"
                    package = _PYTEST_PLUGIN_PROVIDERS.get(module, module)
                    pkg_mgr = stack_profile.package_manager if stack_profile else None
                    proceed = confidence == "known"
                    if confidence == "guessed":
                        interactive = (
                            (sys.stdin.isatty() or getattr(ctx.options, "ui_mode", False))
                            and not getattr(ctx.options, "no_hitl", False)
                            and not getattr(ctx.options, "yes", False)
                        )
                        if interactive:
                            from rich.console import Console
                            from rich.prompt import Confirm
                            proceed = Confirm.ask(
                                f"Missing test dependency `{module}` detected. "
                                f"Install `{package}` (`{rf.get('hint') or ''}`) "
                                f"and retry?",
                                default=True,
                                console=Console(),
                            )
                        else:
                            log.warning(
                                "step09.dep_recover_skipped",
                                reason="non-interactive and confidence=guessed",
                                module=module, package=package,
                            )
                    # Mirror the venv_bin lookup _run_dep_install does internally so
                    # the can-we-install? gate doesn't false-reject pip when the
                    # SUT has a .venv (or false-accept when it doesn't).
                    _venv_bin = (
                        stack_profile.wrapper_prefix
                        if stack_profile and (pkg_mgr or "").lower() == "pip"
                        else None
                    )
                    if (
                        proceed
                        and install_command_for(pkg_mgr, package, venv_bin=_venv_bin) is not None
                    ):
                        ok, summary = _run_dep_install(
                            pkg_mgr, package, ctx.workspace.sut, install_log_path,
                            profile=stack_profile,
                        )
                        log.info(
                            "step09.auto_install_dep",
                            phase="runtime",
                            module=module, package=package,
                            confidence=confidence,
                            success=ok, summary=summary,
                        )
                        if ok:
                            sha = commit_step(
                                ctx.workspace.sut, 9, "execute",
                                message_detail=f"install missing test dep {package}",
                            )
                            log.info(
                                "step09.auto_install_commit",
                                phase="runtime", package=package, sha=sha,
                            )
                            first = run_tests(
                                framework,
                                cwd=ctx.workspace.sut,
                                detected_command=detected_cmd,
                                timeout_s=min(self.timeout_s or 1800, 1800),
                                env_extra=runtime_env,
                                profile=stack_profile,
                                headless=getattr(ctx.options, "headless", True),
                                marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                                parallelism=getattr(ctx.options, "parallelism", 0),
                            )
                            attempts = 2
                            failing = _failing_tests(first)
                            runner_only = (
                                len(failing) > 0
                                and all(r.id == "T-runner-failure" for r in failing)
                            )
                            if (
                                not runner_only
                                and first.exit_code == 3
                                and all(r.runner_failure is not None for r in failing)
                            ):
                                runner_only = True
                            log.info(
                                "step09.dep_recover_retry",
                                runner_only_after=runner_only,
                                failing_after=len(failing),
                            )

            if runner_only:
                rf = (failing[0].runner_failure or {})
                log.warning(
                    "step09.heal_skip",
                    reason="runner failure — no per-test data to patch",
                    kind=rf.get("kind"),
                    module=rf.get("module"),
                    hint=rf.get("hint"),
                )
                # Record a heal-log line per skipped runner failure so the audit
                # trail explains the empty heal block instead of going silent.
                for entry in failing:
                    rf_entry = entry.runner_failure or {}
                    with heal_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "test_id": entry.id,
                            "file": entry.file,
                            "applied": False,
                            "agent_success": False,
                            "agent_error": (
                                f"skipped: {rf_entry.get('summary', 'runner failure')} "
                                f"— no test to patch"
                            ),
                            "ts": datetime.now(UTC).isoformat(),
                        }, ensure_ascii=False) + "\n")
                # Short-circuit the regular heal loop without changing the rest
                # of the flow (run-results.json / bug-candidates.json still get
                # written, status still falls through to the runner_only_failure
                # branch below).
                failing = []

            # QTEA_NO_LLM_RESOLVE=1 disables the JIT runtime LLM tier (in the
            # pytest subprocess) AND the on-failure self-heal agent here. The
            # flag is the single dial for "no LLM spend in this test region";
            # CI runs that need cost determinism set it once and get symmetric
            # behaviour across runtime resolution and post-failure heal. Tier 5
            # (HITL/fail-fast with locator-unresolvable bug candidate) still
            # applies — unresolved TBDs surface in run-results.json for Step 9.
            no_llm_resolve = os.environ.get("QTEA_NO_LLM_RESOLVE") == "1"
            if failing and no_llm_resolve:
                log.info(
                    "step09.heal_skipped",
                    reason="QTEA_NO_LLM_RESOLVE=1",
                    failing_count=len(failing),
                )
                failing = []

            # Re-resolve storage state AFTER run_tests() so Use case B (the
            # runtime plugin's same-run auto-capture) is visible to the heal
            # loop on the FIRST run, not only on `--from-step 9` resumes.
            # The early resolve at step start happens BEFORE tests execute,
            # so the workspace file does not exist yet on a cold run. Re-
            # resolving here, after tests have finished writing it, closes
            # that gap. _heal_mcp_env is mutated in place so the lazy MCP
            # probe + every run_agent call below pick up the fresh path
            # without further plumbing.
            _storage_state_path = _storage_state_mod.resolve(
                sut_root=ctx.workspace.sut,
                workspace_root=ctx.workspace.root,
                cli_opt=getattr(ctx.options, "storage_state", None),
            )
            _heal_mcp_env["QTEA_STORAGE_STATE_ARG"] = (
                _storage_state_mod.to_mcp_arg(_storage_state_path)
            )
            if _storage_state_path is not None:
                runtime_env["QTEA_STORAGE_STATE"] = str(_storage_state_path)
                log.info(
                    "step09.storage_state_resolved_post_run",
                    path=_storage_state_mod.mask_path(_storage_state_path),
                )

            # Partition failures by class. Only "healable" rows (locator
            # timeouts, TBD unresolvable, assertion-on-locator-mediated
            # values, unknown) enter the heal queue. Real bugs (WCAG, TTI,
            # fixture-missing, import-error, dom-order) skip heal — they
            # cannot be fixed by selector or interaction-pattern tweaks
            # and would just burn LLM + MCP budget. They still surface to
            # Step 10 via _build_bug_candidates (which derives from
            # final_failing post-heal, so this partition does not hide
            # them from the bug-candidates emission).
            #
            # Without this partition, run 20260621-213751-ee0fef hit the
            # canonical recurring failure: 11/13 tests failed → heal-skip
            # cap blocked the entire heal flow → no recovery → user saw
            # 11 mixed failures with the cache holding wrong selectors.
            if failing:
                healable, real_bugs = _partition_failures(failing)
                if real_bugs:
                    _ts = datetime.now(UTC).isoformat()
                    for entry, cls in real_bugs:
                        with heal_log_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({
                                "test_id": entry.id,
                                "file": entry.file,
                                "applied": False,
                                "agent_success": False,
                                "agent_error": (
                                    f"skipped: classified as {cls!r} "
                                    f"(real bug — not a heal target)"
                                ),
                                "ts": _ts,
                            }, ensure_ascii=False) + "\n")
                    log.info(
                        "step09.heal_skip_real_bugs",
                        count=len(real_bugs),
                        classes=sorted({c for _, c in real_bugs}),
                        healable_remaining=len(healable),
                    )
                failing = healable

            if failing and len(failing) <= _MAX_HEAL_TESTS:
                # Lazy Playwright MCP probe — replaces the eager preflight
                # so green runs skip the 5-15s warmup. We probe ONCE before
                # the first heal-agent invocation; the warmup is contiguous
                # with the SDK spawn (same cache-warm semantics the eager
                # preflight provided, just deferred until actually needed).
                # On probe failure: log + skip the heal loop entirely. Heal
                # is best-effort — the failing tests still flow to Step 10
                # as bug candidates without an MCP-driven patch.
                mcp_ok, mcp_detail, mcp_warmup_s = _lazy_probe_heal_mcp(
                    self._LAZY_MCP_SERVER,
                    env=_heal_mcp_env,
                )
                if not mcp_ok:
                    log.warning(
                        "step09.heal_mcp_probe_failed",
                        server=self._LAZY_MCP_SERVER,
                        detail=mcp_detail,
                        warmup_s=mcp_warmup_s,
                        failing_count=len(failing),
                    )
                    # Record per-test skip in heal-log so the audit trail
                    # explains the absent heal without going silent.
                    for entry in failing:
                        with heal_log_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({
                                "test_id": entry.id,
                                "file": entry.file,
                                "applied": False,
                                "agent_success": False,
                                "agent_error": (
                                    f"skipped: Playwright MCP probe failed "
                                    f"({mcp_detail or 'unknown'})"
                                ),
                                "ts": datetime.now(UTC).isoformat(),
                            }, ensure_ascii=False) + "\n")
                    # Short-circuit the heal loop; downstream emission of
                    # run-results.json / bug-candidates.json continues so
                    # Step 10 still sees the failures.
                    failing = []
                else:
                    log.info(
                        "step09.heal_mcp_probe_ok",
                        server=self._LAZY_MCP_SERVER,
                        warmup_s=mcp_warmup_s,
                    )
            if failing and len(failing) <= _MAX_HEAL_TESTS:
                fixer_agent = package_resource_root() / "agents" / "polyglot-test-fixer.agent.md"
                sut_base_url = os.environ.get("SUT_BASE_URL")
                heal_relevant_sut_files = _auth_relevant_sut_files(active_module)
                heal_allowlist = _heal_allowlist_dirs(active_module)
                generated_files = _load_generated_files(ctx)

                # Task 1: parallelize heal agents via asyncio.gather. The
                # LLM call (run_agent) dominates each heal's wall time
                # (~1–10 min on Opus); running them concurrently nearly
                # halves the total when 2+ tests fail. Bounded by
                # ``QTEA_HEAL_CONCURRENCY`` (default 3) to cap memory
                # — each agent spawns a Playwright MCP browser process.
                #
                # Concurrent SUT edits: agents may write to the same POM
                # file in ``acceptEdits`` mode; the agent whose write
                # completes second wins. We accept this race for v1; the
                # post-heal verify re-run catches incorrect patches and
                # the existing scope guards / quality gates protect
                # against most catastrophic outcomes. The git commit
                # itself is sync (blocks the event loop briefly) and so
                # is naturally serialized — concurrent commits can't
                # race because Python won't preempt mid-subprocess.
                #
                # ``patches_applied`` / ``patches_rejected`` increments
                # and ``self_heal_meta`` writes are asyncio-safe: they
                # happen between awaits within each coroutine, and
                # asyncio guarantees no preemption inside an await-free
                # span. Same for the per-line heal-log appends.
                _heal_concurrency = max(
                    1, int(os.environ.get("QTEA_HEAL_CONCURRENCY", "3")),
                )
                _heal_sem = asyncio.Semaphore(_heal_concurrency)
                log.info(
                    "step09.heal_parallel_start",
                    failing_count=len(failing),
                    concurrency=_heal_concurrency,
                    tests=[e.name for e in failing],
                )

                async def _do_one_heal(entry):
                    nonlocal patches_applied, patches_rejected
                    heal_wd = ctx.workspace.step_workdir(9) / f"heal-{entry.id}"
                    heal_wd.mkdir(parents=True, exist_ok=True)
                    # Snapshot the failing test's current bytes BEFORE the fixer
                    # runs so we can detect a real change after it returns. The
                    # snapshot lives under heal_wd (NOT in the SUT) so it never
                    # ends up in a qtea commit.
                    target_in_sut = sut_tests / Path(entry.file).name
                    pre_bytes: bytes | None = None
                    if target_in_sut.exists():
                        try:
                            pre_bytes = target_in_sut.read_bytes()
                        except OSError:
                            pre_bytes = None

                    # Capture HEAD before the heal so the scope guard below
                    # can revert any out-of-scope edits the agent made (or
                    # left dangling on timeout) back to the Step 8 commit
                    # state. Best-effort — git not available means we skip
                    # the scope check rather than failing the heal.
                    import subprocess as _sp
                    base_sha: str | None = None
                    try:
                        _res = _sp.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=ctx.workspace.sut,
                            capture_output=True, text=True, check=False,
                            timeout=5,
                        )
                        if _res.returncode == 0:
                            base_sha = (_res.stdout or "").strip() or None
                    except (OSError, _sp.TimeoutExpired):
                        base_sha = None

                    # Snapshot git-dirty state BEFORE the heal agent runs.
                    # Files already dirty at this point (e.g. qtea-junit.xml
                    # from pytest) are excluded from the scope check to avoid
                    # false scope violations.
                    pre_heal_dirty: set[str] = {
                        p for _, p in _git_status_porcelain(ctx.workspace.sut)
                    }

                    # Step 9's heal flow is the only `run_agent` call site in
                    # the pipeline that actually uses Playwright MCP tools
                    # (`browser_navigate`, `browser_snapshot`). After the
                    # audit that flipped `run_agent`'s `enable_mcp` default
                    # to False, this call must opt back in.
                    #
                    # ``_heal_sem`` bounds the in-flight LLM calls so the
                    # parallel orchestration doesn't spin up more browser
                    # processes than the host can handle.

                    # Per-heal MCP env: each concurrent agent gets its own
                    # Chromium user-data-dir so profile locks don't cause
                    # "Browser is already in use" contention.
                    _per_heal_mcp_env = {
                        **_heal_mcp_env,
                        "QTEA_MCP_USER_DATA_DIR_ARG": (
                            f"--user-data-dir={heal_wd / 'playwright-mcp'}"
                        ),
                    }

                    _entry_class = _classify_failure(entry)

                    log.info(
                        "step09.heal_start",
                        test_id=entry.id,
                        test_name=entry.name,
                        test_file=entry.file,
                        failure_class=_entry_class,
                    )
                    async with _heal_sem:
                        agent_res = await run_agent(
                            fixer_agent,
                            workdir=heal_wd,
                            inputs={},
                            user_prompt=_build_fixer_prompt(
                                entry, sut_tests,
                                sut_root=ctx.workspace.sut,
                                sut_base_url=sut_base_url,
                                active_module=active_module,
                                staged_files=heal_relevant_sut_files,
                                storage_state_path=_storage_state_path,
                                generated_files=generated_files,
                                failure_class=_entry_class,
                            ),
                            extra_paths=[
                                package_resource_root() / "skills" / "diagnose-test-failure",
                                package_resource_root() / "skills" / "playwright-explore-website",
                                package_resource_root() / "skills" / "webapp-testing",
                            ],
                            add_dirs=[ctx.workspace.sut],
                            timeout_s=HEAL_AGENT_TIMEOUT_S,
                            step=9,
                            max_turns=HEAL_AGENT_MAX_TURNS,
                            enable_mcp=True,
                            mcp_env=_per_heal_mcp_env,
                        )

                    # Scope guard: revert any heal edits to files outside the
                    # POM/locator allowlist (or matching the FORBIDDEN globs).
                    # Runs unconditionally — even on agent_res.success=False
                    # (timeout, error) — because the agent may have written
                    # files to disk before the timeout fired and left them
                    # uncommitted on the qtea branch. The run 20260611
                    # incident left 5 in-flight fixture edits on disk after
                    # the 150s timeout — this revert prevents that recurrence.
                    scope_reverted = _heal_scope_check_and_revert(
                        ctx.workspace.sut, base_sha, heal_allowlist,
                        generated_files=generated_files,
                        pre_heal_dirty=pre_heal_dirty,
                    )
                    scope_violation = bool(scope_reverted)
                    if scope_violation:
                        log.warning(
                            "step09.heal_scope_violation",
                            test_id=entry.id,
                            reverted=scope_reverted,
                            allowlist=sorted(heal_allowlist),
                        )

                    # Additional cleanup: when the heal agent FAILED outright
                    # (timeout, transport error) any in-scope edits it left
                    # uncommitted on disk must also be reverted. An in-flight
                    # patch that the agent never finished reviewing is no
                    # safer than an out-of-scope one — keeping it commits the
                    # orchestrator to half-thought-through code and confuses
                    # the next heal attempt.
                    failed_partial_revert: list[str] = []
                    if not agent_res.success:
                        failed_partial_revert = _heal_revert_all_uncommitted(
                            ctx.workspace.sut, base_sha,
                        )
                        if failed_partial_revert:
                            log.warning(
                                "step09.heal_failed_partial_edits_reverted",
                                test_id=entry.id,
                                reverted=failed_partial_revert,
                                agent_error=agent_res.error,
                            )

                    # Detect whether the heal agent actually changed something.
                    # Three detection tiers:
                    #   1. The failing TEST file's bytes changed (inline edit).
                    #   2. ANY file in the SUT working tree changed (the agent
                    #      edited a POM/locator file instead of the test file —
                    #      this is the most common heal pattern).
                    #   3. Legacy fallback: the agent dropped a candidate file
                    #      in its workdir (pre-add_dirs path).
                    applied = False
                    if agent_res.success and not scope_violation:
                        post_bytes: bytes | None = None
                        if target_in_sut.exists():
                            try:
                                post_bytes = target_in_sut.read_bytes()
                            except OSError:
                                post_bytes = None
                        applied = post_bytes is not None and post_bytes != pre_bytes
                        if not applied:
                            post_heal_dirty = {
                                p for _, p in _git_status_porcelain(ctx.workspace.sut)
                            }
                            heal_changed = post_heal_dirty - pre_heal_dirty
                            if heal_changed:
                                applied = True
                                log.info(
                                    "step09.heal_detected_via_git",
                                    test_id=entry.id,
                                    changed_files=sorted(heal_changed),
                                )
                        if not applied:
                            applied = _apply_fixer_outputs(
                                heal_wd,
                                sut_tests,
                                entry.file,
                            )

                    xpath_rejected = False
                    if applied:
                        # Step 9 quality gate: a heal that introduces an XPath
                        # selector is rejected. Revert the SUT file to its
                        # pre-heal state (restore bytes if it existed, delete
                        # if the heal created it from scratch) and mark the
                        # patch unapplied. See `docs/qa-orchestrator.instructions.md`
                        # §6 "No XPath (self-heal)".
                        post_bytes_check = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        if _patch_introduces_xpath(pre_bytes, post_bytes_check):
                            xpath_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.xpath_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_xpath",
                                test_id=entry.id,
                                file=entry.file,
                            )
                            applied = False

                    assertion_rejected = False
                    if applied and generated_files:
                        post_bytes_assert = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        if _patch_modifies_assertions(pre_bytes, post_bytes_assert):
                            assertion_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.assertion_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_assertion_modified",
                                test_id=entry.id,
                                file=entry.file,
                            )
                            applied = False

                    anti_pattern_rejected = False
                    anti_pattern_violations: list[str] = []
                    if applied:
                        post_bytes_ap = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        anti_pattern_violations = _patch_has_anti_patterns(
                            pre_bytes, post_bytes_ap,
                        )
                        if anti_pattern_violations:
                            anti_pattern_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.anti_pattern_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_anti_pattern",
                                test_id=entry.id,
                                file=entry.file,
                                violations=anti_pattern_violations,
                            )
                            applied = False

                    if applied:
                        patches_applied += 1
                        # Per-test commit so the human reviewer sees exactly which
                        # heal landed which patch. No-op when the bytes equal the
                        # branch tip (e.g. agent reverted its own edit).
                        commit_step(
                            ctx.workspace.sut,
                            self.number,
                            f"{self.name}-heal-{entry.id}",
                            message_detail=f"healed {Path(entry.file).name}",
                        )
                    else:
                        patches_rejected += 1

                    if scope_violation:
                        summary_text = (
                            f"rejected: heal touched out-of-scope file(s) "
                            f"{','.join(scope_reverted[:3])}; "
                            f"reverted to pre-heal state. Heal scope is "
                            f"POM/locator only — see "
                            f"agents/polyglot-test-fixer.agent.md FORBIDDEN."
                        )
                    elif not agent_res.success:
                        summary_text = agent_res.error
                    elif applied:
                        summary_text = "patch applied"
                    elif xpath_rejected:
                        summary_text = (
                            "rejected: heal introduced XPath selector "
                            "(Step 9 quality gate)"
                        )
                    elif assertion_rejected:
                        summary_text = (
                            "rejected: heal modified assertions in generated "
                            "test (Step 9 assertion-immutability gate)"
                        )
                    elif anti_pattern_rejected:
                        summary_text = (
                            f"rejected: heal introduced anti-pattern — "
                            f"{'; '.join(anti_pattern_violations)}"
                        )
                    else:
                        summary_text = "no usable patch produced"
                    self_heal_meta[entry.id] = {
                        "attempted": True,
                        "applied": applied,
                        "summary": summary_text,
                    }

                    # Append to heal-log.jsonl
                    heal_entry = {
                        "test_id": entry.id,
                        "file": entry.file,
                        "failure_class": _entry_class,
                        "applied": applied,
                        "agent_success": agent_res.success,
                        "agent_error": agent_res.error,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                    if xpath_rejected:
                        heal_entry["rejected"] = "xpath"
                    if assertion_rejected:
                        heal_entry["rejected"] = "assertion_modified"
                    if anti_pattern_rejected:
                        heal_entry["rejected"] = "anti_pattern"
                    if scope_violation:
                        heal_entry["rejected"] = "scope_violation"
                        heal_entry["reverted_files"] = scope_reverted
                    with heal_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(heal_entry, ensure_ascii=False) + "\n")

                # Launch every heal concurrently. ``return_exceptions=False``
                # propagates the first failure so we don't silently mask
                # bugs in the heal harness itself — individual agent errors
                # are already captured as ``agent_res.success=False`` and
                # do not raise; anything that DOES raise here is a true
                # orchestrator bug worth surfacing.
                _heal_t0 = time.monotonic()
                await asyncio.gather(*[_do_one_heal(e) for e in failing])
                log.info(
                    "step09.heal_parallel_done",
                    failing_count=len(failing),
                    duration_s=round(time.monotonic() - _heal_t0, 1),
                    patches_applied=patches_applied,
                    patches_rejected=patches_rejected,
                )

                if patches_applied > 0:
                    # Re-run ONLY the healed tests via `-k`, not the whole
                    # suite. We resolve the command the same way `run_tests`
                    # would, narrow it to the tests we just patched, then feed
                    # it back as `detected_command`. Non-pytest stacks fall
                    # back to the full command (see `_filter_command_for_tests`).
                    base_cmd, _parser = resolve_command(
                        framework,
                        detected=detected_cmd,
                        cwd=ctx.workspace.sut,
                        profile=stack_profile,
                        marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                    )
                    narrowed_cmd = _filter_command_for_tests(base_cmd, failing)
                    _clean_sut_artifacts(ctx.workspace.sut)
                    second = run_tests(
                        framework,
                        cwd=ctx.workspace.sut,
                        detected_command=narrowed_cmd,
                        timeout_s=min((self.timeout_s or 1800) // 2, 900),
                        env_extra=runtime_env,
                        profile=stack_profile,
                        headless=getattr(ctx.options, "headless", True),
                        marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                        parallelism=getattr(ctx.options, "parallelism", 0),
                    )
                    attempts = 2
                    # The narrowed re-run only reports the healed subset, so
                    # MERGE its outcomes into the full first-run result set
                    # (override the healed entries by id, keep everyone else).
                    # Replacing wholesale would drop every non-healed test from
                    # totals / bug-candidates / the runner-failure check.
                    if second.results:
                        by_id = {r.id: r for r in second.results}
                        first.results = [
                            by_id.get(r.id, r) for r in first.results
                        ]
                        first.exit_code = second.exit_code
            elif failing:
                log.warning(
                    "step09.heal_skip",
                    reason="too many failing tests",
                    count=len(failing),
                    cap=_MAX_HEAL_TESTS,
                )

            # Attach SUT-side artifacts discovered post-run to entries without any.
            # Keep only the newest file per artifact type so each failed test
            # gets at most one screenshot, one trace, one video.
            extra_attachments = _attachment_glob(ctx.workspace.sut)
            if extra_attachments:
                newest_by_type: dict[str, dict] = {}
                for a in extra_attachments:
                    kind = a.get("type", "other")
                    prev = newest_by_type.get(kind)
                    if prev is None:
                        newest_by_type[kind] = a
                        continue
                    try:
                        if Path(a["path"]).stat().st_mtime > Path(prev["path"]).stat().st_mtime:
                            newest_by_type[kind] = a
                    except OSError:
                        pass
                deduped = list(newest_by_type.values())
                for r in first.results:
                    if r.status in ("failed", "error") and not r.attachments:
                        r.attachments = deduped

            # Annotate self-heal metadata into per-entry dicts via the serializer.
            payload = first.as_dict()
            for entry_dict in payload["results"]:
                meta = self_heal_meta.get(entry_dict["id"])
                if meta:
                    entry_dict["self_heal"] = meta
            payload["self_heal"] = {
                "attempts": attempts,
                "patches_applied": patches_applied,
                "patches_rejected": patches_rejected,
            }

            # Resolver telemetry (Phase 6). Per-run only — no global
            # aggregator. Absent for non-JIT stacks (no spend file).
            resolver_spend = _summarize_resolver_spend(jit_cache_dir)
            if resolver_spend is not None:
                payload["resolver_spend"] = resolver_spend
                log.info(
                    "step09.resolver_spend_summarised",
                    total_resolutions=resolver_spend["total_resolutions"],
                    total_input_tokens=resolver_spend["total_input_tokens"],
                    total_output_tokens=resolver_spend["total_output_tokens"],
                )

            run_results_path = out_dir / "run-results.json"
            run_results_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            ok_schema, schema_err = is_valid(payload, "run-results")
            if not ok_schema:
                log.warning("step09.schema_invalid", error=schema_err)

            # Tasks 2 + 4 + 5: persist this attempt's outcomes for any
            # subsequent retry. ``no_patch_ids`` is the running union with
            # the prior attempt's so multi-attempt convergence works: once
            # a test is classified as a real bug it stays skipped on every
            # later attempt without needing each attempt to rediscover it.
            _now_failing = [
                (r.id, r.name) for r in first.results
                if r.status in ("failed", "error")
            ]
            _now_no_patch = {
                tid for tid, meta in self_heal_meta.items()
                if not meta.get("applied")
                and meta.get("summary") == _NO_PATCH_SUMMARY
            }
            if _prior_state:
                _now_no_patch |= set(_prior_state.get("no_patch_ids", []) or [])
            _save_attempt_state(
                out_dir, _current_attempt,
                failing=_now_failing,
                no_patch_ids=sorted(_now_no_patch),
                install_sig=install_sig,
            )

            # TBD promotion: any tbd("intent") sentinels whose intent now has a
            # cached selector get replaced in-place in the SUT source files and
            # committed, so the code is self-sufficient without the JIT plugin.
            _promoted, _promotion_blocked = _promote_resolved_tbds(
                ctx.workspace.sut,
                jit_cache_dir / "locator-cache.json",
            )
            if _promoted:
                log.info("step09.tbd_promoted", count=len(_promoted), files=_promoted)
                commit_step(ctx.workspace.sut, 9, self.name, "tbd-promotion")
            if _promotion_blocked:
                log.info(
                    "step09.tbd_promotion_blocked",
                    count=len(_promotion_blocked),
                    reasons=sorted({b["reason"] for b in _promotion_blocked}),
                )

            # HITL escalation pass. The JIT runtime drops `hitl-pending-*.json`
            # files in the cache dir whenever it could not resolve a TBD.
            # On a TTY (and unless --no-hitl) we prompt for a selector and
            # write it to dev-locators.json so the next run skips Tier 4 for
            # that key; otherwise the unresolved TBDs flow into the bug
            # candidates as `locator-unresolvable` entries for Step 9.
            hitl_pendings = _collect_hitl_pending(jit_cache_dir)
            hitl_dev_locators_path = Path(runtime_env["QTEA_DEV_LOCATORS"])
            _, hitl_remaining = _hitl_resolve_unresolvable(
                hitl_pendings,
                dev_locators_path=hitl_dev_locators_path,
                no_hitl=bool(getattr(ctx.options, "no_hitl", False)),
            )

            # bug-candidates.json: emitted regardless (empty list when no failures).
            final_failing = _failing_tests(first)
            bug_payload = _build_bug_candidates(final_failing)
            bug_payload["candidates"].extend(
                _bug_candidates_for_unresolvable_tbds(
                    hitl_remaining, dev_locators_path=hitl_dev_locators_path,
                )
            )
            # Promotion gate emits structured candidates for entries that
            # had a cached resolution but couldn't be safely frozen into
            # source (no passing-test witness, malformed selector, or
            # unrepresentable payload). Surface them so reviewers see what
            # the JIT runtime is still chewing on between runs.
            if _promotion_blocked:
                bug_payload["candidates"].extend(_promotion_blocked)
            # Dev-pool drift candidates: one per dev-locators entry whose
            # selector failed at action time this run. The JIT runtime
            # quarantined them and stored an LLM fallback under a shadow
            # cache key; the user owns updating the dev file.
            _drift_candidates = _bug_candidates_for_dev_pool_drift(
                jit_cache_dir / "dev-pool-quarantine.jsonl",
            )
            if _drift_candidates:
                bug_payload["candidates"].extend(_drift_candidates)
            bug_path = out_dir / "bug-candidates.json"
            bug_path.write_text(
                json.dumps(bug_payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            # JIT cache publish — if the runtime plugin populated locator-cache.json
            # during the test run, copy it into artifacts/step09 so step 11 can
            # surface per-TBD resolution sources in the report. Best-effort; absence
            # is normal for non-JIT stacks.
            jit_cache_src = ctx.workspace.root / "locator-cache" / "locator-cache.json"
            if jit_cache_src.exists():
                try:
                    jit_cache_dst = out_dir / "locator-cache.json"
                    jit_cache_dst.write_text(
                        jit_cache_src.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    log.info("step09.jit_cache_published", entries_path=str(jit_cache_dst))
                except OSError as e:
                    log.warning("step09.jit_cache_publish_failed", error=str(e))

            # Counts come from `totals` (Fix 7): `tests` excludes synthetic
            # T-runner-failure entries; `infrastructure_errors` is reported
            # separately so a green-looking `tests=N` cannot conceal a run
            # that never executed a single real test.
            totals = payload["totals"]
            notes_parts = [
                f"framework={framework}",
                f"tests={totals['tests']}",
                f"failed={totals['failed']}",
                f"errors={totals['errors']}",
                f"infra_errors={totals.get('infrastructure_errors', 0)}",
                f"attempts={attempts}",
                f"healed={patches_applied}",
            ]
            notes = " ".join(notes_parts)

            # Status semantics:
            #   - `completed` when nothing failed.
            #   - `failed` when EVERY result is a synthesised `T-runner-failure`
            #     (the test runner didn't even produce parseable output — typically
            #     a conftest import error, missing dep, exit code 4, etc.). The
            #     prior "warned" status hid this and Step 9/11 ran on garbage;
            #     Step 10 then crashed rendering an environment-bug card.
            #     This is an environment failure, not a real test failure.
            #   - `failed` when ALL tests errored/failed and NONE passed — no
            #     assertion was ever evaluated, so there is nothing to classify.
            #   - `warned` when some tests passed and some failed/errored —
            #     Step 10 will classify the failures as bug candidates.
            runner_only_failure = (
                len(first.results) > 0
                and all(r.id == "T-runner-failure" for r in first.results)
            )
            # Defensive: exit code 3 (pytest internal error) with no
            # passed/failed tests is also an environment failure, even
            # if the JUnit entries don't all carry T-runner-failure IDs.
            if not runner_only_failure and first.exit_code == 3:
                real_passed_or_failed = any(
                    r.status in ("passed", "failed")
                    and r.id != "T-runner-failure"
                    for r in first.results
                )
                if not real_passed_or_failed:
                    runner_only_failure = True
            if runner_only_failure:
                first_entry = first.results[0]
                first_msg = (first_entry.message or "").strip() or "test runner failed"
                rf = first_entry.runner_failure or {}
                # When the classifier identified a specific failure mode
                # (missing module, collection error), lead the error with the
                # actionable fix command. Otherwise fall through to the raw
                # message tail — at least the operator gets the stderr summary.
                if rf:
                    error = (
                        f"test runner failed before any test could run: "
                        f"{rf.get('summary', 'collection / import error')}. "
                        f"To fix: {rf.get('hint', 'see stderr in run-results.json')}. "
                        f"(exit_code={first.exit_code})"
                    )
                else:
                    error = (
                        f"test runner produced no parseable test results "
                        f"(exit_code={first.exit_code}). This is an environment "
                        f"failure, not a real test failure. {first_msg[:300]}"
                    )
                # Surface stderr/stdout so the user can diagnose without
                # opening run-results.json.
                runner_stderr = (first_entry.stderr or "").strip()
                runner_stdout = (first_entry.stdout or "").strip()
                if runner_stderr:
                    error += f"\n\n--- stderr (last 1500 chars) ---\n{runner_stderr[-1500:]}"
                elif runner_stdout:
                    error += f"\n\n--- stdout (last 1500 chars) ---\n{runner_stdout[-1500:]}"
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[run_results_path, bug_path],
                    error=error,
                    notes=notes,
                )

            # All-tests-errored gate: when every test errored/failed and
            # none passed, no assertion was evaluated. This is functionally
            # equivalent to a runner failure (e.g. DNS unreachable, auth
            # fixture crash, SUT down) and should not be masked as "warned".
            any_passed = any(
                r.status == "passed" for r in first.results
            )
            if final_failing and not any_passed:
                first_entry = final_failing[0]
                msg_snippet = (first_entry.message or "").strip()[:300]
                error_counts: dict[str, int] = {}
                for r in first.results:
                    error_counts[r.status] = error_counts.get(r.status, 0) + 1
                status_breakdown = ", ".join(
                    f"{v} {k}" for k, v in sorted(error_counts.items())
                )
                setup_failures = [
                    r for r in first.results
                    if r.message and "failed on setup" in r.message
                ]
                hint = ""
                if setup_failures:
                    hint = (
                        " Likely cause: shared fixture/setup failure blocking "
                        "all tests — check conftest.py and fixture code."
                    )
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[run_results_path, bug_path],
                    error=(
                        f"all {len(first.results)} test(s) errored with zero "
                        f"passing ({status_breakdown}) — no assertion was "
                        f"evaluated.{hint} First error: {msg_snippet}"
                    ),
                    notes=notes,
                )

            sub_status = "all_passed" if not final_failing else "bugs_found"
            return StepResult(
                success=True,
                status="completed",
                sub_status=sub_status,
                outputs=[run_results_path, bug_path],
                notes=notes,
            )

        finally:
            if _resolver_server is not None:
                _resolver_server.stop()


__all__ = [
    "ExecuteStep",
    "_apply_fixer_outputs",
    "_build_bug_candidates",
    "_build_fixer_prompt",
    "_filter_command_for_tests",
]
