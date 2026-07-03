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


__all__ = [
    "_git_revert_path",
    "_git_status_porcelain",
    "_heal_allowlist_dirs",
    "_heal_path_in_scope",
    "_heal_path_is_forbidden",
    "_heal_revert_all_uncommitted",
    "_heal_scope_check_and_revert",
]
