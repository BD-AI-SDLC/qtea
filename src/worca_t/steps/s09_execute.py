"""Step 9: Execute tests + self-heal.

Workflow:
  1. Load Step 6 research and resolve the SUT tests directory (no mirror —
     Step 9 already wrote files in `<workspace>/sut/` on the worca-t branch
     based on the Step 8 plan).
  2. Resolve test-run command (research.commands.test or per-framework default).
  3. Execute via `test_runner.run_tests` with `cwd=<workspace>/sut/`. Capture
     per-test status + attachments.
  4. For each failing test: invoke `polyglot-test-fixer` once with the failing
     test source + traceback. The fixer agent has `add_dirs=[<workspace>/sut/]`
     so it can read SUT helpers / page objects directly and edit the failing
     test file in place. On a successful patch, commit it to the worca-t
     branch with one commit per healed test.
  5. Re-run only the failing tests once. Record self-heal outcome per test.
  6. Emit run-results.json + bug-candidates.json + heal-log.jsonl.

Self-heal budget is capped (default 5 tests) to bound runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from worca_t._sut_git import commit_step
from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.proxy import safe_subprocess_env
from worca_t.resolver_server import ResolverServer
from worca_t.schemas import is_valid
from worca_t.stack_profile import PYTHON_VENV_MANAGERS, StackProfile
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.auth_helpers import (
    auth_relevant_sut_files as _auth_relevant_sut_files,
    auth_summary_for_prompt as _auth_summary_for_prompt,
)
from worca_t.test_runner import (
    _PYTEST_PLUGIN_PROVIDERS,
    RunResult,
    TestRunEntry,
    install_command_for,
    prepare_sut,
    run_tests,
)

log = get_logger(__name__)

# Cap on number of failing tests we'll attempt to self-heal in a single step run.
_MAX_HEAL_TESTS = int(os.environ.get("WORCA_T_MAX_HEAL", "5"))

# Pytest -m selector that scopes Step 9 to ONLY the worca-t generated tests.
# The codegen agent (`ui-test-automation.agent.md` rule 8) applies one of these
# markers to every generated test based on the planning phase. The vendored
# `tests/worca_t_runtime.py` plugin registers them via `pytest_configure` so
# strict-markers runs don't fail. Keep this list in sync with the agent prompt
# and the runtime template's `_WORCA_PHASE_MARKERS`. Operator escape: set
# `WORCA_T_PYTEST_MARKER` to override (e.g. `""` to disable marker scoping
# and run the SUT's full native suite alongside worca-generated tests).
_WORCA_PYTEST_MARKER_FILTER = os.environ.get(
    "WORCA_T_PYTEST_MARKER",
    "worca_smoke or worca_regression or worca_e2e or worca_exploratory",
)

# Patterns that mark an XPath selector. Used by `_patch_introduces_xpath` to
# reject heal patches that quietly downgrade to XPath in violation of the
# Step 9 quality gate (see qa-orchestrator.instructions.md §6).
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
# Fallback tests subdirectory used when:
#   - --isolated-tests is set (explicit user opt-in to today's behavior), OR
#   - sut_inventory has no test_directory_layout for the active module.
# When isolated, the dir lives under the active module's path so monorepos
# don't clobber sibling modules.
_ISOLATED_TESTS_DIR_NAME = "worca-tests"


# Poetry stdout phrases that indicate `poetry add` was a no-op because the
# package is already declared in pyproject.toml. Exit code is 0 in that case
# even though NOTHING was installed — treat as failure so the caller doesn't
# claim victory and re-run the same broken tests. See the run forensics in
# the bug that introduced this check: the package was declared but never
# installed in the resolved venv (because worca-t's parent venv was being
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
    from the subprocess env so they don't reuse worca-t's parent venv as
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


def _sut_tests_dir(
    sut_root: Path,
    *,
    active_module: dict | None,
    isolated: bool,
) -> Path:
    """Resolve the directory inside the SUT where worca-generated tests live.

    Steps 7 & 8 write tests there directly (on the worca-t branch); this
    function only computes the path — nothing is copied or wiped.

    Resolution mirrors the active module's layout:
      - `--isolated-tests` → `<sut>/<module.path>/worca-tests/`
      - Active module's `test_directory_layout.base_dir` (the SUT's own
        convention, e.g. `tests/` or `e2e/`) → `<sut>/<module.path>/<base_dir>/`
      - Fallback when nothing is known: `<sut>/<module.path>/worca-tests/`
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
        ("playwright-report/**/*", "other"),
        ("allure-results/**/*.json", "other"),
    ]
    for pattern, kind in patterns:
        for p in sut_root.glob(pattern):
            if p.is_file():
                out.append({"path": str(p), "type": kind})
    return out


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
        live_block = (
            f"\n--- LIVE DIAGNOSIS ---\n"
            f"SUT base URL: `{sut_base_url}`. Active module language: `{language}`.\n"
            f"{auth_summary}\n"
            + (f"\nSUT clone root (you have add_dirs access — read + edit "
               f"these files directly): `{sut_root}`\n" if sut_root else "\n")
            + (f"\nKey SUT files for this active module — call these instead "
               f"of reimplementing auth or navigation:\n{files_str}\n\n"
               f"Workflow: (1) use the Playwright MCP `browser_navigate` to open "
               f"`{sut_base_url}` and follow the SUT's auth flow via the existing "
               f"sign-in helper above. (2) Take a `browser_snapshot` of the page "
               f"the failing test targets and compare it to what the traceback "
               f"says the test expected. (3) Patch the test based on what you "
               f"observe live, NOT just from the traceback text. Match the "
               f"active module's language: `{language}`. Never rewrite a Python "
               f"test in TypeScript or vice versa.\n")
        )

    # Absolute path of the failing test inside the SUT. The fixer must edit
    # THIS exact file (on the worca-t branch) — no per-step workdir copy.
    failing_test_abs = (
        (tests_root_in_sut / Path(entry.file).name).as_posix()
        if entry.file else "(unknown — see `entry.file`)"
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
        f"{live_block}\n"
        f"Edit the failing test file at its absolute path above using the "
        f"Edit tool. The pipeline does NOT copy your changes anywhere — the "
        f"SUT clone IS the deliverable, on a worca-t-owned git branch. Only "
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


def _filter_command_for_tests(command: str, test_ids: list[str]) -> str:
    """Best-effort narrowing of the run command to a subset of failing tests.

    For pytest we append `-k <expr>` constructed from the test ids' tail.
    For other frameworks we just return the command unchanged - re-running
    everything is safe (idempotent) and avoids brittle CLI assumptions.
    """
    if not test_ids:
        return command
    tokens = command.lower().split()
    if "pytest" in tokens:
        names = [tid.split("-", 1)[-1].replace("_", " ") for tid in test_ids]
        # quote whole thing
        expr = " or ".join(t.split(" ")[-1] for t in names if t)
        if expr:
            return f"{command} -k \"{expr}\""
    return command


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
    durations_ms: list[int] = []
    models: set[str] = set()
    count = 0
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
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
    except OSError:
        return None
    if count == 0:
        return None
    # Cost estimation reuses the existing pricing table if available;
    # otherwise the consumer can compute it from input/output tokens.
    est_cost_usd: float | None = None
    try:
        from worca_t.llm.cost import estimate_cost  # type: ignore[import-not-found]
        for m in (models or {""}):
            est_cost_usd = (est_cost_usd or 0.0) + estimate_cost(
                m, total_input, total_output,
            )
    except Exception:  # noqa: BLE001 - pricing table is optional
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

    is_tty = sys.stdin is not None and sys.stdin.isatty()
    if not is_tty or no_hitl or not pendings:
        return [], pendings

    resolved: list[dict] = []
    remaining: list[dict] = []
    log.info("step09.hitl_pending_count", count=len(pendings))
    print(
        f"\n[worca-t] {len(pendings)} locator(s) the JIT runtime could not "
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
            print(f"       [rejected] XPath selectors are forbidden by worca-t.")
            remaining.append(entry)
            continue
        entry["_user_selector"] = answer
        resolved.append(entry)
        # Best-effort: also remove the pending file so next runs don't re-prompt.
        try:
            Path(entry.get("_pending_path", "")).unlink(missing_ok=True)
        except OSError:
            pass

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


def _bug_candidates_for_unresolvable_tbds(remaining: list[dict]) -> list[dict]:
    """Emit a ``locator-unresolvable`` bug-candidate per HITL-unanswered
    TBD. Step 9's classifier sees these alongside test failures.
    """
    now = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for entry in remaining:
        const = entry.get("constant_name") or "unknown"
        intent = entry.get("intent") or ""
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
                f"Provide a selector via .worca-t/dev-locators.json under "
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


class ExecuteStep(Step):
    number = 9
    name = "execute"
    timeout_s = step_timeout(9)

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

        # Pre-flight: SUT must be present + on the worca-t branch. Step 8
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
                    f"worca-t branch is missing. Re-run from step 1."
                ),
            )

        # Step 8 committed worca_*-prefixed files into the SUT on the
        # worca-t branch. We don't need a separate codegen_root anymore;
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

        # Resolve where worca-generated tests live inside the SUT (steps 7+8
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
        runtime_env = {"WORCA_T_TESTS_DIR": str(sut_tests)}

        # JIT runtime plugin env wiring. The vendored `tests/worca_t_runtime.py`
        # (when present) reads these vars to discover the cache, optional
        # dev-supplied locator file, resolver port/token, and timeout defaults.
        # SECURITY: ANTHROPIC_API_KEY is deliberately NOT re-exported here —
        # safe_subprocess_env() strips it because the pytest subprocess executes
        # untrusted SUT test code that could exfiltrate the key via os.environ.
        # The LLM resolver path works WITHOUT the key in the SUT env because
        # the parent process (here in step 9) starts a ResolverServer on a
        # local loopback port; the pytest plugin reaches it via the short-lived
        # per-run WORCA_T_RESOLVER_TOKEN (set further down, once the server
        # binds a port). Tier order at runtime: dev-locators → cache →
        # in-process heuristic → ResolverServer (LLM) → HITL/fail-fast.
        jit_cache_dir = ctx.workspace.root / "locator-cache"
        jit_cache_dir.mkdir(parents=True, exist_ok=True)
        runtime_env["WORCA_T_CACHE_DIR"] = str(jit_cache_dir)
        runtime_env["WORCA_T_RUN_ID"] = ctx.workspace.run_id
        resolver_model = os.environ.get("WORCA_T_RESOLVER_MODEL")
        if resolver_model:
            runtime_env["WORCA_T_RESOLVER_MODEL"] = resolver_model
        timeout_ms = os.environ.get("WORCA_T_DEFAULT_TIMEOUT_MS")
        if timeout_ms:
            runtime_env["WORCA_T_DEFAULT_TIMEOUT_MS"] = timeout_ms
        # Dev-locators file: --dev-locators CLI flag wins; env var as fallback.
        dev_locators_opt = getattr(ctx.options, "dev_locators", None)
        if dev_locators_opt:
            runtime_env["WORCA_T_DEV_LOCATORS"] = str(dev_locators_opt)
        elif os.environ.get("WORCA_T_DEV_LOCATORS"):
            runtime_env["WORCA_T_DEV_LOCATORS"] = os.environ["WORCA_T_DEV_LOCATORS"]

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
        if stack_profile and stack_profile.install_command:
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
        elif stack_profile is None:
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
        # the socket path automatically when WORCA_T_RESOLVER_PORT is set.
        jit_runtime_vendored = (
            ctx.workspace.sut / "tests" / "worca_t_runtime.py"
        ).is_file()
        _resolver_server = None
        if jit_runtime_vendored:
            _resolver_server = ResolverServer(
                cache_dir=jit_cache_dir,
                run_id=ctx.workspace.run_id,
                model=runtime_env.get("WORCA_T_RESOLVER_MODEL"),
            )
            _resolver_server.start()
            runtime_env["WORCA_T_RESOLVER_PORT"] = str(_resolver_server.port)
            runtime_env["WORCA_T_RESOLVER_TOKEN"] = _resolver_server.token
            log.info(
                "step09.resolver_server_started",
                port=_resolver_server.port,
            )

        try:
            first = run_tests(
                framework,
                cwd=ctx.workspace.sut,
                detected_command=detected_cmd,
                timeout_s=min(self.timeout_s or 1800, 1800),
                env_extra=runtime_env,
                profile=stack_profile,
                headless=getattr(ctx.options, "headless", True),
                marker_filter=_WORCA_PYTEST_MARKER_FILTER,
            )

            attempts = 1
            patches_applied = 0
            patches_rejected = 0

            failing = _failing_tests(first)
            self_heal_meta: dict[str, dict] = {}

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
                            sys.stdin.isatty()
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
                    if proceed and install_command_for(pkg_mgr, package, venv_bin=_venv_bin) is not None:
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
                                marker_filter=_WORCA_PYTEST_MARKER_FILTER,
                            )
                            attempts = 2
                            failing = _failing_tests(first)
                            runner_only = (
                                len(failing) > 0
                                and all(r.id == "T-runner-failure" for r in failing)
                            )
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

            # WORCA_T_NO_LLM_RESOLVE=1 disables the JIT runtime LLM tier (in the
            # pytest subprocess) AND the on-failure self-heal agent here. The
            # flag is the single dial for "no LLM spend in this test region";
            # CI runs that need cost determinism set it once and get symmetric
            # behaviour across runtime resolution and post-failure heal. Tier 5
            # (HITL/fail-fast with locator-unresolvable bug candidate) still
            # applies — unresolved TBDs surface in run-results.json for Step 9.
            no_llm_resolve = os.environ.get("WORCA_T_NO_LLM_RESOLVE") == "1"
            if failing and no_llm_resolve:
                log.info(
                    "step09.heal_skipped",
                    reason="WORCA_T_NO_LLM_RESOLVE=1",
                    failing_count=len(failing),
                )
                failing = []
            if failing and len(failing) <= _MAX_HEAL_TESTS:
                fixer_agent = package_resource_root() / "agents" / "polyglot-test-fixer.agent.md"
                sut_base_url = os.environ.get("SUT_BASE_URL")
                heal_relevant_sut_files = _auth_relevant_sut_files(active_module)
                for entry in failing:
                    heal_wd = ctx.workspace.step_workdir(9) / f"heal-{entry.id}"
                    heal_wd.mkdir(parents=True, exist_ok=True)
                    # Snapshot the failing test's current bytes BEFORE the fixer
                    # runs so we can detect a real change after it returns. The
                    # snapshot lives under heal_wd (NOT in the SUT) so it never
                    # ends up in a worca-t commit.
                    target_in_sut = sut_tests / Path(entry.file).name
                    pre_bytes: bytes | None = None
                    if target_in_sut.exists():
                        try:
                            pre_bytes = target_in_sut.read_bytes()
                        except OSError:
                            pre_bytes = None

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
                        ),
                        extra_paths=[],
                        add_dirs=[ctx.workspace.sut],
                        timeout_s=min((self.timeout_s or 1800) // 4, 600),
                        step=9,
                        max_turns=30,
                    )

                    # The fixer now edits the failing test file IN PLACE inside
                    # the SUT (via add_dirs). Detect a real change by comparing
                    # the post-run bytes against the snapshot. Fall back to the
                    # legacy "did the agent drop a candidate file in heal_wd?"
                    # path for the rare case where the agent wrote to its cwd
                    # instead of editing in place — _apply_fixer_outputs copies
                    # that file into the SUT.
                    applied = False
                    if agent_res.success:
                        post_bytes: bytes | None = None
                        if target_in_sut.exists():
                            try:
                                post_bytes = target_in_sut.read_bytes()
                            except OSError:
                                post_bytes = None
                        applied = post_bytes is not None and post_bytes != pre_bytes
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
                        # patch unapplied. See `qa-orchestrator.instructions.md`
                        # §6 "No XPath (self-heal)".
                        post_bytes_check = target_in_sut.read_bytes() if target_in_sut.exists() else None
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

                    summary_text = (
                        agent_res.error
                        if not agent_res.success
                        else (
                            "patch applied"
                            if applied
                            else (
                                "rejected: heal introduced XPath selector (Step 9 quality gate)"
                                if xpath_rejected
                                else "no usable patch produced"
                            )
                        )
                    )
                    self_heal_meta[entry.id] = {
                        "attempted": True,
                        "applied": applied,
                        "summary": summary_text,
                    }

                    # Append to heal-log.jsonl
                    heal_entry = {
                        "test_id": entry.id,
                        "file": entry.file,
                        "applied": applied,
                        "agent_success": agent_res.success,
                        "agent_error": agent_res.error,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                    if xpath_rejected:
                        heal_entry["rejected"] = "xpath"
                    with heal_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(heal_entry, ensure_ascii=False) + "\n")

                if patches_applied > 0:
                    # Re-run the full suite once. Cheaper than per-test filtering
                    # for our typical small surface, and avoids brittle CLI assumptions.
                    second = run_tests(
                        framework,
                        cwd=ctx.workspace.sut,
                        detected_command=detected_cmd,
                        timeout_s=min((self.timeout_s or 1800) // 2, 900),
                        env_extra=runtime_env,
                        profile=stack_profile,
                        headless=getattr(ctx.options, "headless", True),
                        marker_filter=_WORCA_PYTEST_MARKER_FILTER,
                    )
                    attempts = 2
                    # Second run is authoritative when it produced parseable results.
                    if second.results:
                        first.results = second.results
                        first.exit_code = second.exit_code
            elif failing:
                log.warning(
                    "step09.heal_skip",
                    reason="too many failing tests",
                    count=len(failing),
                    cap=_MAX_HEAL_TESTS,
                )

            # Attach SUT-side artifacts discovered post-run to entries without any.
            extra_attachments = _attachment_glob(ctx.workspace.sut)
            if extra_attachments:
                for r in first.results:
                    if r.status in ("failed", "error") and not r.attachments:
                        # naive: attach everything (renderer can filter); cheaper
                        # than walking the per-test trace tree.
                        r.attachments = extra_attachments

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

            # HITL escalation pass. The JIT runtime drops `hitl-pending-*.json`
            # files in the cache dir whenever it could not resolve a TBD.
            # On a TTY (and unless --no-hitl) we prompt for a selector and
            # write it to dev-locators.json so the next run skips Tier 4 for
            # that key; otherwise the unresolved TBDs flow into the bug
            # candidates as `locator-unresolvable` entries for Step 9.
            hitl_pendings = _collect_hitl_pending(jit_cache_dir)
            hitl_dev_locators_path = (
                ctx.workspace.sut / ".worca-t" / "dev-locators.json"
            )
            _, hitl_remaining = _hitl_resolve_unresolvable(
                hitl_pendings,
                dev_locators_path=hitl_dev_locators_path,
                no_hitl=bool(getattr(ctx.options, "no_hitl", False)),
            )

            # bug-candidates.json: emitted regardless (empty list when no failures).
            final_failing = _failing_tests(first)
            bug_payload = _build_bug_candidates(final_failing)
            bug_payload["candidates"].extend(
                _bug_candidates_for_unresolvable_tbds(hitl_remaining)
            )
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

            notes_parts = [
                f"framework={framework}",
                f"tests={len(first.results)}",
                f"failed={payload['totals']['failed']}",
                f"errors={payload['totals']['errors']}",
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
            #   - `warned` when real failures or errors remain alongside passing
            #     tests — Step 9 will classify them as bug candidates.
            runner_only_failure = (
                len(first.results) > 0
                and all(r.id == "T-runner-failure" for r in first.results)
            )
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
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[run_results_path, bug_path],
                    error=error,
                    notes=notes,
                )

            status = "completed" if not final_failing else "warned"
            return StepResult(
                success=True,
                status=status,
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
