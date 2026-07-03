"""Attempt-N state persistence + dependency-lockfile fingerprinting.

Step 9 runs at most two attempts (``MAX_ATTEMPTS=2``). After attempt 1 we
persist the list of failing tests plus the install signature so attempt 2
can narrow the runner to those tests and skip an already-satisfied
``pip install`` / ``poetry install`` / ``npm ci``. Files land under the
step's ``out_dir`` (``<workspace>/step-09-execute/``).

Kept free of pipeline imports so it stays cheap to unit-test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from qtea.logging_setup import get_logger

log = get_logger(__name__)


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


__all__ = [
    "_attempt_state_path",
    "_compute_install_sig",
    "_load_attempt_state",
    "_save_attempt_state",
]
