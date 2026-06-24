"""End-to-end coverage for `_materialize_sut` + the qtea isolation branch.

These tests use REAL git (not mocked) to verify the four materialization
paths and exercise `ensure_git_repo_and_branch` end-to-end:

  - Local-path source that IS a git repo (source's `.git/` stripped by
    `_materialize_sut`; `ensure_git_repo_and_branch` `git init`s a fresh
    repo + commits the baseline + creates the branch).
  - Local-path source that is NOT a git repo (same end-state).
  - Resume: re-materialize the same `run_id` and confirm the branch is
    force-recreated, not duplicated.
  - URL clones are exercised indirectly by `test_step06_research`'s
    monkeypatched test; running a real network clone in unit tests is
    out of scope.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from qtea._sut_git import branch_name, commit_step, current_branch, ensure_git_repo_and_branch
from qtea.steps.s06_research import _materialize_sut


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(),
    reason="git not available — branch tests need real git",
)


def _make_local_source(path: Path, *, with_git: bool) -> None:
    """Create a fake source directory with a couple of files."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# source\n", encoding="utf-8")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "app.py").write_text("def hi(): return 'hi'\n", encoding="utf-8")
    if with_git:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "add", "-A"],
            cwd=path, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "-m", "src baseline"],
            cwd=path, check=True,
        )


def test_materialize_local_non_git_creates_repo_and_branch(tmp_path: Path):
    """Local-path source with no `.git/` → fresh repo + qtea branch."""
    src = tmp_path / "src_sut"
    dst = tmp_path / "ws" / "sut"
    _make_local_source(src, with_git=False)

    _materialize_sut(str(src), dst, run_id="abc123")

    assert (dst / ".git").is_dir(), "git init must run for non-git source"
    assert (dst / "src" / "app.py").exists(), "source files copied"
    branch = current_branch(dst)
    assert branch == branch_name("abc123") == "qtea/run-abc123"


def test_materialize_local_with_git_strips_history_and_branches(tmp_path: Path):
    """Local-path source WITH `.git/` → original history stripped, fresh repo created."""
    src = tmp_path / "src_sut"
    dst = tmp_path / "ws" / "sut"
    _make_local_source(src, with_git=True)

    _materialize_sut(str(src), dst, run_id="def456")

    # Dst has its OWN .git (not a copy of src's — _materialize_sut uses
    # `ignore=ignore_patterns('.git')` to strip it, then ensure_* re-inits).
    assert (dst / ".git").is_dir()
    # The dst's HEAD is the qtea baseline commit, NOT src's "src baseline".
    log = subprocess.run(
        ["git", "-C", str(dst), "log", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ["qtea baseline"]
    assert current_branch(dst) == "qtea/run-def456"


def test_materialize_is_idempotent_on_resume(tmp_path: Path):
    """Re-materialize with the same run_id → branch force-recreated, no error."""
    src = tmp_path / "src_sut"
    dst = tmp_path / "ws" / "sut"
    _make_local_source(src, with_git=False)

    _materialize_sut(str(src), dst, run_id="run-x")
    # Make a qtea step commit on the branch.
    (dst / "step7_artifact.py").write_text("pass\n", encoding="utf-8")
    sha1 = commit_step(dst, 7, "codegen", message_detail="1 file")
    assert sha1 is not None

    # Re-materialize: should wipe + recreate. The previous step commit is
    # lost (expected — _materialize_sut always rmtree's `dst` first).
    _materialize_sut(str(src), dst, run_id="run-x")
    assert current_branch(dst) == "qtea/run-run-x"
    # The artifact from the prior run is gone.
    assert not (dst / "step7_artifact.py").exists()


def test_ensure_git_repo_and_branch_idempotent_when_already_setup(tmp_path: Path):
    """Calling ensure_* twice on the same dir is a no-op (force-checkout)."""
    dst = tmp_path / "sut"
    dst.mkdir()
    (dst / "x.txt").write_text("hi\n", encoding="utf-8")

    ensure_git_repo_and_branch(dst, "id1")
    head1 = subprocess.run(
        ["git", "-C", str(dst), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Second call should not error and should leave us on the same branch.
    ensure_git_repo_and_branch(dst, "id1")
    head2 = subprocess.run(
        ["git", "-C", str(dst), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head1 == head2
    assert current_branch(dst) == "qtea/run-id1"


def test_commit_step_records_sha_and_subject(tmp_path: Path):
    """commit_step stages everything, commits with a structured subject."""
    dst = tmp_path / "sut"
    dst.mkdir()
    (dst / "baseline.txt").write_text("b\n", encoding="utf-8")
    ensure_git_repo_and_branch(dst, "rid")

    # Add a "step output" and commit.
    (dst / "tests" / "qtea_x_test.py").parent.mkdir(parents=True, exist_ok=True)
    (dst / "tests" / "qtea_x_test.py").write_text("def test_x(): pass\n", encoding="utf-8")
    sha = commit_step(dst, 7, "codegen", message_detail="1 file, 1 test")

    assert sha is not None and len(sha) >= 7
    subject = subprocess.run(
        ["git", "-C", str(dst), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject == "qtea/step-07: codegen (1 file, 1 test)"


def test_commit_step_is_noop_on_clean_tree(tmp_path: Path):
    """No staged changes → commit_step returns None and doesn't commit."""
    dst = tmp_path / "sut"
    dst.mkdir()
    (dst / "baseline.txt").write_text("b\n", encoding="utf-8")
    ensure_git_repo_and_branch(dst, "rid")

    # Nothing new since the baseline commit.
    sha = commit_step(dst, 8, "locator-resolution")
    assert sha is None
    # HEAD still on the baseline.
    log = subprocess.run(
        ["git", "-C", str(dst), "log", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert log == ["qtea baseline"]
