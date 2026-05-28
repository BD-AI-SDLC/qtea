"""Step 9: Execute tests + self-heal.

Workflow:
  1. Load Step 8 patched tests (artifacts/step08/tests/) and Step 6 research.
  2. Mirror patched tests into the SUT working dir (ctx.workspace.sut / <tests_dir>).
  3. Resolve test-run command (research.commands.test or per-framework default).
  4. Execute via `test_runner.run_tests`. Capture per-test status + attachments.
  5. For each failing test: invoke `polyglot-test-fixer` once with the failing
     test source + traceback. If the agent writes patched test files, apply the
     diff (verbatim file overwrite limited to files already under tests dir).
     Re-run only the failing tests once. Record self-heal outcome per test.
  6. Emit run-results.json + bug-candidates.json + heal-log.jsonl.

Self-heal budget is capped (default 5 tests) to bound runtime.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.test_runner import RunResult, TestRunEntry, run_tests

log = get_logger(__name__)

# Cap on number of failing tests we'll attempt to self-heal in a single step run.
_MAX_HEAL_TESTS = int(os.environ.get("WORCA_T_MAX_HEAL", "5"))
# Tests subdirectory inside the SUT we'll mirror our patched tests into.
# We deliberately keep this isolated to avoid clobbering existing SUT tests.
_TESTS_DIR_NAME = "worca-tests"


def _research_payload(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(6) / "research.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _detected_command(research: dict) -> str | None:
    cmds = research.get("commands") or {}
    return cmds.get("test")


def _framework(research: dict, index: dict) -> str:
    return research.get("detected_stack") or index.get("framework") or "unknown"


def _load_index(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(8) / "tests-with-tbd.json"
    if not p.exists():
        # fallback to step 7 if step 8 was skipped (no TBD case)
        p = ctx.workspace.step_dir(7) / "tests-with-tbd.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tests_src(ctx: StepContext) -> Path | None:
    candidates = [
        ctx.workspace.step_dir(8) / "tests",
        ctx.workspace.step_dir(7) / "tests",
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return None


def _mirror_tests_into_sut(src_tests: Path, sut_root: Path) -> Path:
    """Copy tests into <sut>/worca-tests/. Returns the destination path."""
    dest = sut_root / _TESTS_DIR_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src_tests, dest)
    return dest


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


def _build_fixer_prompt(entry: TestRunEntry, tests_root_in_sut: Path) -> str:
    snippet = (entry.traceback or entry.message or "(no traceback)")[-3000:]
    return (
        "A single test failed. Apply the smallest possible patch to make it "
        "pass without modifying assertions, business logic, or test_ids, and "
        "without adding hard waits. Locator priority: id > data-testid > role > "
        "label > text > placeholder > scoped css. NEVER XPath.\n\n"
        f"Test id: {entry.id}\n"
        f"Test file (relative to repo root): {entry.file}\n"
        f"Tests directory in SUT: {tests_root_in_sut.as_posix()}\n"
        f"Status: {entry.status}\n"
        f"Message: {entry.message or '(none)'}\n\n"
        f"Traceback:\n{snippet}\n\n"
        "Write your replacement file(s) directly under the same relative path "
        "in your working directory (e.g. `./<file>`). Only edit files that "
        "already exist under the tests directory."
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


class ExecuteStep(Step):
    number = 9
    name = "execute"
    timeout_s = step_timeout(9)

    def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        heal_log_path = out_dir / "self-heal" / "heal-log.jsonl"
        heal_log_path.parent.mkdir(parents=True, exist_ok=True)

        src_tests = _tests_src(ctx)
        if not src_tests:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="step 9 requires patched tests from step 7 or step 8",
            )

        if not ctx.workspace.sut.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="SUT not materialized (step 6 must run first)",
            )

        research = _research_payload(ctx)
        index = _load_index(ctx)
        framework = _framework(research, index)
        detected_cmd = _detected_command(research)

        sut_env_keys = research.get("sut_env_keys") or []
        if sut_env_keys:
            missing = [k for k in sut_env_keys if k not in os.environ]
            if missing:
                log.warning("step09.env_missing", keys=missing)

        # Mirror tests into a dedicated subdir under the SUT and aim the runner
        # at that directory by default (when no detected command is provided).
        sut_tests = _mirror_tests_into_sut(src_tests, ctx.workspace.sut)
        runtime_env = {"WORCA_T_TESTS_DIR": str(sut_tests)}

        first = run_tests(
            framework,
            cwd=ctx.workspace.sut,
            detected_command=detected_cmd,
            timeout_s=min(self.timeout_s or 1800, 1800),
            env_extra=runtime_env,
        )

        attempts = 1
        patches_applied = 0
        patches_rejected = 0

        failing = _failing_tests(first)
        self_heal_meta: dict[str, dict] = {}

        if failing and len(failing) <= _MAX_HEAL_TESTS:
            fixer_agent = package_resource_root() / "agents" / "polyglot-test-fixer.agent.md"
            for entry in failing:
                heal_wd = ctx.workspace.step_workdir(9) / f"heal-{entry.id}"
                heal_wd.mkdir(parents=True, exist_ok=True)
                # Stage the target test file alongside the prompt so the agent
                # can read it without MCP access. Use an `_orig/` subdir so we
                # can ignore the unchanged stage copy while still detecting any
                # edits the agent writes at its CWD or under its workdir.
                target_in_sut = sut_tests / Path(entry.file).name
                staged_copy: Path | None = None
                if target_in_sut.exists():
                    orig_dir = heal_wd / "_orig"
                    orig_dir.mkdir(exist_ok=True)
                    staged_copy = orig_dir / Path(entry.file).name
                    shutil.copy2(target_in_sut, staged_copy)

                agent_res = run_agent(
                    fixer_agent,
                    workdir=heal_wd,
                    inputs={},
                    user_prompt=_build_fixer_prompt(entry, sut_tests),
                    extra_paths=[],
                    timeout_s=min((self.timeout_s or 1800) // 4, 600),
                    step=9,
                    max_turns=30,
                )

                applied = False
                if agent_res.success:
                    applied = _apply_fixer_outputs(
                        heal_wd,
                        sut_tests,
                        entry.file,
                        ignore_paths={staged_copy} if staged_copy else set(),
                    )

                if applied:
                    patches_applied += 1
                else:
                    patches_rejected += 1

                self_heal_meta[entry.id] = {
                    "attempted": True,
                    "applied": applied,
                    "summary": (
                        agent_res.error
                        if not agent_res.success
                        else ("patch applied" if applied else "no usable patch produced")
                    ),
                }

                # Append to heal-log.jsonl
                with heal_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "test_id": entry.id,
                        "file": entry.file,
                        "applied": applied,
                        "agent_success": agent_res.success,
                        "agent_error": agent_res.error,
                        "ts": datetime.now(UTC).isoformat(),
                    }, ensure_ascii=False) + "\n")

            if patches_applied > 0:
                # Re-run the full suite once. Cheaper than per-test filtering
                # for our typical small surface, and avoids brittle CLI assumptions.
                second = run_tests(
                    framework,
                    cwd=ctx.workspace.sut,
                    detected_command=detected_cmd,
                    timeout_s=min((self.timeout_s or 1800) // 2, 900),
                    env_extra=runtime_env,
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

        run_results_path = out_dir / "run-results.json"
        run_results_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok_schema, schema_err = is_valid(payload, "run-results")
        if not ok_schema:
            log.warning("step09.schema_invalid", error=schema_err)

        # bug-candidates.json: emitted regardless (empty list when no failures).
        final_failing = _failing_tests(first)
        bug_payload = _build_bug_candidates(final_failing)
        bug_path = out_dir / "bug-candidates.json"
        bug_path.write_text(
            json.dumps(bug_payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        notes_parts = [
            f"framework={framework}",
            f"tests={len(first.results)}",
            f"failed={payload['totals']['failed']}",
            f"errors={payload['totals']['errors']}",
            f"attempts={attempts}",
            f"healed={patches_applied}",
        ]
        notes = " ".join(notes_parts)

        # Step is "completed" when nothing failed; "warned" when failures
        # remain. We intentionally do NOT mark the step as failed on test
        # failures - those are surfaced as bug candidates in step 10.
        status = "completed" if not final_failing else "warned"
        return StepResult(
            success=True,
            status=status,
            outputs=[run_results_path, bug_path],
            notes=notes,
        )


__all__ = [
    "ExecuteStep",
    "_apply_fixer_outputs",
    "_build_bug_candidates",
    "_build_fixer_prompt",
    "_filter_command_for_tests",
]
