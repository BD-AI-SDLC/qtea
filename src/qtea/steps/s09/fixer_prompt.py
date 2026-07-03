"""Heal-agent prompt builder + patch-application + test-runner narrowing.

Owns the following slice of Step 9's per-failure loop:

- ``_build_fixer_prompt`` — assembles the multi-section prompt the
  ``polyglot-test-fixer`` agent receives (traceback snippet, generated-file
  editability block, storage-state directive, failure-class strategy hint,
  workflow steps referencing MCP tool names).
- ``_apply_fixer_outputs`` — copies the agent's edited test file from the
  agent workdir back into the SUT tests dir; skips no-op writes.
- ``_filter_command_for_tests`` + ``_narrow_command_to_ids`` — pytest ``-k`` /
  Playwright ``--grep`` narrowing so the re-run only exercises the tests the
  heal actually touched.

The three patch-content quality gates (XPath / assertion / anti-patterns) and
the heal-scope revert live in ``patch_gates`` and ``heal_scope`` respectively;
they are called around ``_apply_fixer_outputs`` from ``ExecuteStep.run()``,
not from inside this module.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from qtea.auth_helpers import (
    auth_summary_for_prompt as _auth_summary_for_prompt,
)
from qtea.test_runner import TestRunEntry


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
        if getattr(e, "id", None) == "T-runner-failure":
            continue
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


__all__ = [
    "_NO_PATCH_SUMMARY",
    "_apply_fixer_outputs",
    "_build_fixer_prompt",
    "_filter_command_for_tests",
    "_narrow_command_to_ids",
]
