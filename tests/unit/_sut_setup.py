"""Shared SUT-seed helper for step tests.

Steps 7, 8, and 9 now require `<workspace>/sut/` to be a git repo on the
`worca-t/run-<id>` branch — pipeline.py materializes that in production
via `_materialize_sut`. Tests don't go through the pipeline, so this
helper mimics the same end state: a populated `ws.sut/` with `.git/`
initialised and the worca-t branch checked out.

Optional `inventory` arg writes a `sut_inventory.json` into
`ws.step_dir(6)` so Step 7's pre-flight (which requires that file) passes.
"""

from __future__ import annotations

import json
from typing import Any

from worca_t._sut_git import ensure_git_repo_and_branch


def seed_sut(
    workspace,
    *,
    seed_files: dict[str, str] | None = None,
    inventory: dict[str, Any] | None = None,
    include_default_inventory: bool = True,
) -> None:
    """Make `workspace.sut/` a git repo on the worca-t branch.

    Parameters
    ----------
    seed_files:
        Optional `{rel_path: content}` map of files to place into the SUT
        before the baseline commit. A `README.md` is always added so the
        baseline commit is non-empty (even on Windows where empty
        directories can confuse `git add -A`).
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
    ws_sut.mkdir(parents=True, exist_ok=True)
    files = dict(seed_files or {})
    files.setdefault("README.md", "# fake SUT for tests\n")
    for rel, content in files.items():
        target = ws_sut / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    ensure_git_repo_and_branch(ws_sut, workspace.run_id)

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
