"""Heal-scope predicates and git-based revert helpers for Step 9 self-heal.

Enforces the allowed/forbidden path matrix from
``agents/polyglot-test-fixer.agent.md``: the heal agent may only touch POM /
locator sources listed in ``sut_inventory.json`` (plus codegen-generated
files it is fixing). Out-of-scope edits are reverted via
``git checkout HEAD -- <file>`` (or ``rm`` for newly-added files) before the
caller records ``applied=false, reason=scope_violation``.

Kept free of pipeline imports so it stays cheap to unit-test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from qtea.logging_setup import get_logger

log = get_logger(__name__)


# Scope model (see `agents/polyglot-test-fixer.agent.md` "Strict Scope").
#
# The heal agent may edit any TEST-SIDE code needed to make a qtea test pass
# correctly per the Step-4 cases: page objects, locators, helpers, fixtures,
# `conftest.py`, test configuration, and qtea-GENERATED test files. Two
# categories remain out of scope and are reverted:
#
#   1. Application / production source — editing the code under test would
#      MASK genuine DEV bugs, which defeats the pipeline. Detected as any
#      path that is neither a recognised test-infra shape nor under an
#      inventory-derived allowlist directory (when an allowlist is known).
#   2. Pre-existing, SUT-authored test files — those belong to the SUT team
#      and are not qtea's deliverable (codegen never writes into them either).
#      qtea's own generated tests are passed in via ``generated_files`` and
#      ARE editable.
#
# Implemented as predicates rather than glob patterns because `fnmatch` does
# not handle `**`-recursive semantics portably.


def _heal_path_is_pre_existing_test(rel_posix: str) -> bool:
    """True iff the path is a SUT-authored test-file shape.

    These stay off-limits to heal (qtea never edits the SUT team's own
    tests). qtea-GENERATED test files match the same shapes but are allowed
    because they are listed in ``generated_files`` and short-circuit the
    scope check before this predicate runs. NOTE: ``conftest.py`` and
    fixture files are deliberately NOT included here — under the current
    scope model they are editable test infrastructure.
    """
    p = rel_posix
    basename = p.rsplit("/", 1)[-1] if "/" in p else p
    segments = p.split("/")
    if "__tests__" in segments:
        return True
    if "tests" in segments:
        if basename.startswith("test_") and basename.endswith(".py"):
            return True
        if basename.endswith("_test.py"):
            return True
    if basename.endswith((".spec.ts", ".spec.js", ".test.ts", ".test.js")):
        return True
    return bool(basename.endswith("Test.java"))


def _heal_path_is_test_infra(rel_posix: str) -> bool:
    """True iff the path is editable test infrastructure regardless of the
    allowlist: ``conftest.py`` or any file under a ``fixtures`` directory.

    These are the categories the scope relaxation opened up — the heal agent
    may create/repair fixtures and conftest entries so a qtea test's
    preconditions are satisfied."""
    p = rel_posix
    basename = p.rsplit("/", 1)[-1] if "/" in p else p
    segments = p.split("/")
    if basename == "conftest.py":
        return True
    return "fixtures" in segments


def _heal_allowlist_dirs(active_module: dict | None) -> set[str]:
    """Test-side directories (SUT-relative, posix-style) heal may touch.

    Derived from `sut_inventory.json` → the active module's existing POM /
    locator / helper / fixture file locations, plus the test-directory and
    src (pages/locators/helpers) layout roots. Empty set means "no
    inventory information" — in that case we fall back to permissive
    behaviour (only the pre-existing-test and test-infra predicates apply).

    Application/production source lives OUTSIDE these dirs, so a non-empty
    allowlist is what lets the scope check reject edits to the code under
    test (which would mask DEV bugs).
    """
    if not isinstance(active_module, dict):
        return set()
    dirs: set[str] = set()
    for key in (
        "existing_page_objects", "existing_locators",
        "existing_helpers", "existing_fixtures",
    ):
        for entry in active_module.get(key) or []:
            file_rel = (entry.get("file") if isinstance(entry, dict) else "") or ""
            file_rel = file_rel.replace("\\", "/")
            if not file_rel:
                continue
            parent = file_rel.rsplit("/", 1)[0] if "/" in file_rel else ""
            if parent:
                dirs.add(parent)
    # Test-directory + src layout roots so newly-created files under them
    # (e.g. a new fixture module, a new POM) are in-scope.
    test_layout = active_module.get("test_directory_layout") or {}
    for k in ("base_dir", "default_target"):
        v = test_layout.get(k)
        if isinstance(v, str) and v:
            dirs.add(v.replace("\\", "/").rstrip("/"))
    for sub in test_layout.get("subdirs") or []:
        if isinstance(sub, dict) and sub.get("path"):
            dirs.add(str(sub["path"]).replace("\\", "/").rstrip("/"))
    src_layout = active_module.get("src_directory_layout") or {}
    for k in ("pages_object_dir", "pages_locators_dir", "helpers_dir"):
        v = src_layout.get(k)
        if isinstance(v, str) and v:
            dirs.add(v.replace("\\", "/").rstrip("/"))
    return dirs


def _heal_path_in_scope(
    rel_path: str,
    allowlist_dirs: set[str],
    generated_files: set[str] | None = None,
) -> bool:
    """True iff a heal-modified path is in-scope.

    Logic (in order):
      0. qtea-generated file → always in-scope (heal fixes codegen's output).
      1. Pre-existing SUT-authored test file → out-of-scope (never edit the
         SUT team's tests; qtea's own tests are covered by rule 0).
      2. Test infrastructure (conftest.py / fixtures/**) → in-scope.
      3. Under an inventory allowlist dir → in-scope.
      4. Empty allowlist (no inventory) → permissive in-scope.
      5. Otherwise (application/production source when an allowlist IS known)
         → out-of-scope, so a heal cannot mask a DEV bug by editing the code
         under test.
    """
    p = rel_path.replace("\\", "/")
    if generated_files and p in generated_files:
        return True
    if _heal_path_is_pre_existing_test(p):
        return False
    if _heal_path_is_test_infra(p):
        return True
    if not allowlist_dirs:
        return True
    return any(p == d or p.startswith(d + "/") for d in allowlist_dirs)


def _git_revert_path(sut_root: Path, rel_path: str, status_code: str) -> bool:
    """Revert a single uncommitted change. Returns True on success."""
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


__all__ = [
    "_git_revert_path",
    "_git_status_porcelain",
    "_heal_allowlist_dirs",
    "_heal_path_in_scope",
    "_heal_path_is_pre_existing_test",
    "_heal_path_is_test_infra",
    "_heal_revert_all_uncommitted",
    "_heal_scope_check_and_revert",
]
