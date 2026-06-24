"""Shared SUT-seed helper for step tests.

Steps 7, 8, and 9 now require `<workspace>/sut/` to be a git repo on the
`qtea/run-<id>` branch — pipeline.py materializes that in production
via `_materialize_sut`. Tests don't go through the pipeline, so this
helper mimics the same end state: a populated `ws.sut/` with `.git/`
initialised and the qtea branch checked out.

Optional `inventory` arg writes a `sut_inventory.json` into
`ws.step_dir(6)` so Step 7's pre-flight (which requires that file) passes.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

from qtea._sut_git import ensure_git_repo_and_branch

# --- Session-scoped git template -------------------------------------------
#
# Building a git repo costs four `git` subprocess spawns (init / add / commit
# / checkout). On scanned corporate Windows boxes each spawn can take seconds
# (AV intercepts every git.exe launch), which made every step test pay a
# multi-second SUT-seed tax. Copying a prebuilt `.git` is ~100x cheaper
# (measured: ~80ms vs seconds), so we pay the real git cost ONCE per test run
# and filesystem-copy the result per test.
#
# Under `pytest-xdist`, every worker is a separate process, so a naive
# module-level cache would rebuild the template N times (once per worker). We
# instead build into a run-scoped *shared* directory guarded by a file lock:
# the first worker to grab the lock builds it, every other worker reuses it.
#
# The template lives on branch `qtea/run-template`; step tests assert on
# run-results / disk state, never on the exact branch name (the git module's
# own behaviour is covered separately by test_sut_branch.py, which keeps
# exercising the real `ensure_git_repo_and_branch`).
_TEMPLATE_DIR: Path | None = None


def _git_template() -> Path:
    """Return a run-scoped directory containing a baseline git repo.

    Built once across the whole test run (shared across xdist workers via a
    file lock) and cleaned up at process exit.
    """
    global _TEMPLATE_DIR
    if _TEMPLATE_DIR is not None and (_TEMPLATE_DIR / ".git").exists():
        return _TEMPLATE_DIR

    # Run-scoped key: shared across xdist workers of the SAME run, distinct
    # across separate invocations. Falls back to the pid for non-xdist runs.
    run_uid = os.environ.get("PYTEST_XDIST_TESTRUNUID") or f"pid{os.getpid()}"
    shared = Path(tempfile.gettempdir()) / f"qtea-sut-template-{run_uid}"
    # Marker lives OUTSIDE the template dir so it isn't copied into each SUT.
    ready = Path(str(shared) + ".ready")

    with FileLock(str(shared) + ".lock"):
        if not ready.exists():
            shutil.rmtree(shared, ignore_errors=True)
            shared.mkdir(parents=True, exist_ok=True)
            (shared / "README.md").write_text("# fake SUT for tests\n", encoding="utf-8")
            ensure_git_repo_and_branch(shared, "template")
            ready.write_text("ok", encoding="utf-8")
            # Only the builder registers cleanup; ignore_errors covers the
            # case where a sibling worker already removed it.
            atexit.register(lambda: shutil.rmtree(shared, ignore_errors=True))
            atexit.register(lambda: ready.unlink(missing_ok=True))

    _TEMPLATE_DIR = shared
    return shared


def seed_sut(
    workspace,
    *,
    seed_files: dict[str, str] | None = None,
    inventory: dict[str, Any] | None = None,
    include_default_inventory: bool = True,
) -> None:
    """Make `workspace.sut/` a git repo (copied from a session template).

    Mirrors the production end-state (`<workspace>/sut/` is a git repo with a
    baseline commit and a qtea branch checked out) by filesystem-copying a
    prebuilt template instead of re-running `git` per test.

    Parameters
    ----------
    seed_files:
        Optional `{rel_path: content}` map of files to place into the SUT
        working tree (overlaid on top of the template's `README.md`). The
        files are left uncommitted on disk — the step under test stages and
        commits them via `commit_step`, exactly as in production.
    inventory:
        Optional dict to write as `ws.step_dir(6)/sut_inventory.json`. When
        omitted and `include_default_inventory=True`, a minimal stub with
        no active module is written so Step 7's "sut_inventory.json must
        exist" pre-flight passes while skipping the per-file pre-flight.
    include_default_inventory:
        Default True. Set False to leave `sut_inventory.json` absent
        (used by tests that exercise the missing-inventory failure path).
    """
    ws_sut = workspace.sut
    template = _git_template()
    if ws_sut.exists():
        shutil.rmtree(ws_sut)
    ws_sut.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, ws_sut)

    # Overlay per-test seed files on the working tree (uncommitted; the step
    # under test commits them). README.md is already present from the template.
    for rel, content in (seed_files or {}).items():
        target = ws_sut / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if inventory is not None:
        _write_inventory(workspace, inventory)
    elif include_default_inventory:
        _write_inventory(workspace, _MINIMAL_INVENTORY)


_MINIMAL_INVENTORY: dict[str, Any] = {
    "modules": [],
    "active_module": None,
}


def _write_inventory(workspace, inventory: dict[str, Any]) -> None:
    step6 = workspace.step_dir(6)
    step6.mkdir(parents=True, exist_ok=True)
    (step6 / "sut_inventory.json").write_text(
        json.dumps(inventory, indent=2),
        encoding="utf-8",
    )


__all__ = ["seed_sut"]
