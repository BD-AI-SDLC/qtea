"""Git operations on the materialized SUT.

The pipeline materializes a single SUT clone at ``<workspace>/sut/`` and
hands it off to every downstream step. To keep that clone safe to mutate,
qtea creates a per-run isolation branch (``qtea/run-<run_id>``) and
asks each code-writing step (7, 8, 9) to commit its work there. A human
then reviews the branch via ``git diff`` or opens a PR against the SUT's
upstream — nothing qtea writes ever lands on ``main`` directly.

This module is the single home for the git plumbing both ends rely on:

  - :func:`ensure_git_repo_and_branch` runs once, immediately after
    materialize. Initialises a repo if the source wasn't one
    (``_materialize_sut`` strips ``.git/`` from local-path copies), then
    force-creates the qtea branch. Idempotent — safe on resume.

  - :func:`commit_step` is called at the end of every step that writes
    files into the SUT. Stages everything and commits with a structured
    message; no-op when the working tree is clean.

All git invocations use inline ``-c user.name`` / ``-c user.email`` config
so the user's global git identity is never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse

from qtea.logging_setup import get_logger

log = get_logger(__name__)


_AUTHOR_NAME = "qtea"
_AUTHOR_EMAIL = "qtea@local"

_GIT_HOSTS = (
    "github.com", "gitlab", "bitbucket.org",
    "dev.azure.com", "ssh.dev.azure.com", "visualstudio.com",
    "codeberg.org", "gitea.", "sr.ht",
)


def is_git_url(s: str) -> bool:
    """True when ``s`` looks like a git remote URL rather than a local path.

    Shared by ``steps.s06_research`` (SUT materialization) and
    ``incident_memory`` (per-SUT fingerprint derivation) so both agree on
    what counts as "the same SUT source".
    """
    if not s.startswith(("git@", "ssh://", "http://", "https://")):
        return False
    if s.endswith(".git"):
        return True
    try:
        # .hostname strips userinfo (e.g. "user@host" → "host") and lowercases
        hostname = urlparse(s).hostname or ""
    except Exception:
        return False
    return any(hostname == host or hostname.endswith("." + host) for host in _GIT_HOSTS)


def branch_name(run_id: str) -> str:
    """Canonical qtea branch name for a run."""
    return f"qtea/run-{run_id}"


def _git(sut_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git -c user.name=qtea -c user.email=qtea@local <args>`` in the SUT."""
    return subprocess.run(
        [
            "git",
            "-c", f"user.name={_AUTHOR_NAME}",
            "-c", f"user.email={_AUTHOR_EMAIL}",
            *args,
        ],
        cwd=str(sut_root),
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_git_repo_and_branch(sut_root: Path, run_id: str) -> str:
    """Guarantee that ``sut_root`` is a git repo on branch ``qtea/run-<run_id>``.

    - When ``sut_root/.git/`` already exists (URL clone), no init runs.
    - When it doesn't (local-path copy via ``shutil.copytree(...,
      ignore_patterns('.git'))``), inits a fresh repo and commits a baseline
      so the qtea branch has a parent commit to branch from.
    - In both cases, force-creates the branch via ``git checkout -B`` so
      resume re-runs are idempotent. (``_materialize_sut`` wipes ``sut_root``
      on every pipeline invocation, so previous-run branches do not survive
      anyway.)

    Returns the branch name actually checked out.
    """
    branch = branch_name(run_id)
    git_dir = sut_root / ".git"
    if not git_dir.exists():
        log.info("sut.git_init", dst=str(sut_root))
        _git(sut_root, "init", "-q")
        _git(sut_root, "add", "-A")
        # `--allow-empty` covers the rare empty-folder source case so the
        # subsequent `checkout -B` always has a parent commit to branch from.
        _git(sut_root, "commit", "--allow-empty", "-q", "-m", "qtea baseline")
    _git(sut_root, "checkout", "-B", branch, "-q")
    log.info("sut.branch_ready", branch=branch, dst=str(sut_root))
    return branch


def commit_step(
    sut_root: Path,
    step_num: int,
    step_name: str,
    message_detail: str = "",
) -> str | None:
    """Stage and commit any changes the step made to the SUT.

    Returns the new commit's short sha, or ``None`` when the working tree is
    clean (nothing to commit — common on read-only steps, or when a step
    re-runs and produces byte-identical output).

    Never raises — git failures are logged at warning level and treated as
    a no-op, since a missed commit must not abort a successful step. Callers
    can pass ``message_detail`` (e.g. ``"3 files, 12 tests"``) to enrich the
    commit subject.
    """
    if not (sut_root / ".git").exists():
        log.warning("sut.commit_skip", reason="not a git repo", sut=str(sut_root))
        return None

    try:
        _git(sut_root, "add", "-A")
        status = _git(sut_root, "status", "--porcelain")
        if not status.stdout.strip():
            log.info("sut.commit_noop", step=step_num, name=step_name)
            return None
        subject = f"qtea/step-{step_num:02d}: {step_name}"
        if message_detail:
            subject = f"{subject} ({message_detail})"
        _git(sut_root, "commit", "-q", "-m", subject)
        sha = _git(sut_root, "rev-parse", "--short", "HEAD").stdout.strip()
        log.info("sut.commit_ok", step=step_num, name=step_name, sha=sha)
        return sha or None
    except subprocess.CalledProcessError as e:
        log.warning(
            "sut.commit_failed",
            step=step_num,
            name=step_name,
            stderr=(e.stderr or "").strip()[:500],
        )
        return None


def files_in_commit(sut_root: Path, sha: str) -> list[str]:
    """Return SUT-relative paths of every file changed in `sha`.

    Used by Step 8 to build `generated-files.json` from ground truth
    (the actual commit) rather than a `qtea_*` glob — the glob missed
    in-place modifications to existing files (POM extensions, locator
    appends, conftest.py edits), causing the manifest to under-report.

    Empty list on git error / empty sha.
    """
    if not sha or not (sut_root / ".git").exists():
        return []
    try:
        result = _git(
            sut_root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha,
        )
    except subprocess.CalledProcessError as e:
        log.warning(
            "sut.diff_tree_failed",
            sha=sha,
            stderr=(e.stderr or "").strip()[:500],
        )
        return []
    files = [
        line.strip().replace("\\", "/")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]
    return files


def current_branch(sut_root: Path) -> str | None:
    """Return the currently-checked-out branch name, or None on detached HEAD / error."""
    try:
        result = _git(sut_root, "branch", "--show-current", check=False)
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name or None
    except subprocess.CalledProcessError:
        return None


__all__ = [
    "branch_name",
    "commit_step",
    "current_branch",
    "ensure_git_repo_and_branch",
    "is_git_url",
]
