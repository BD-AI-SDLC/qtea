"""Tests for `<sut>/.env` preservation across `_materialize_sut` re-runs.

On `qtea run --from-step 6+`, the SUT clone is wiped and re-materialized.
`_materialize_sut` stashes the existing `<sut>/.env` in memory and restores it
afterward so HITL-provided env values survive across the wipe. Two gaps are
covered here:

  * When the freshly materialized source ships its OWN `.env` (local-dir SUTs
    copied via copytree), the stashed prior-run values must be merged ON TOP so
    HITL endpoints/identity/credentials survive; source-only keys are preserved.
  * `.env` must be gitignored BEFORE the baseline commit so a source-shipped
    `.env` is never tracked — otherwise the cleanup `git reset --hard`
    (`_rollback_sut_to_before_step`) reverts it to the placeholder before the
    stash can read the HITL values.

These exercise the real git plumbing, so they require `git` on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

from qtea.env_resolver import merge_dotenv_file
from qtea.steps.s06_research import _materialize_sut

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _make_source(tmp_path: Path, with_env: str | None = None) -> Path:
    """A minimal local-dir SUT source; optionally shipping its own `.env`."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")
    if with_env is not None:
        (src / ".env").write_text(with_env, encoding="utf-8")
    return src


def test_materialize_merges_hitl_over_source_env(tmp_path: Path):
    """Source ships a placeholder `.env`; re-materialize must keep the HITL
    value merged over it and preserve source-only keys."""
    src = _make_source(
        tmp_path, with_env="SUT_BASE_URL=http://placeholder\nSOURCE_ONLY=keep\n"
    )
    dst = tmp_path / "sut"

    # Run 1: fresh materialize (nothing to stash yet).
    _materialize_sut(str(src), dst, run_id="r1")
    # Simulate Step 6 HITL persisting a real endpoint.
    merge_dotenv_file(dst / ".env", {"SUT_BASE_URL": "https://real.example.com"})

    # Run 2: re-materialize (the --from-step wipe). Source still ships placeholder.
    _materialize_sut(str(src), dst, run_id="r1")

    vals = dotenv_values(dst / ".env")
    assert vals["SUT_BASE_URL"] == "https://real.example.com"  # HITL preserved
    assert vals["SOURCE_ONLY"] == "keep"  # source-only key preserved


def test_materialize_restores_env_verbatim_when_source_has_none(tmp_path: Path):
    """Source has no `.env` (git-URL-style); the stashed file is restored
    verbatim, preserving comments/formatting."""
    src = _make_source(tmp_path, with_env=None)
    dst = tmp_path / "sut"

    _materialize_sut(str(src), dst, run_id="r1")
    # Simulate Step 6 creating a fresh .env with a comment.
    (dst / ".env").write_text(
        "# my creds\nPASSWORD_APP=secret123\n", encoding="utf-8"
    )

    _materialize_sut(str(src), dst, run_id="r1")

    text = (dst / ".env").read_text(encoding="utf-8")
    assert "PASSWORD_APP=secret123" in text
    assert "# my creds" in text  # verbatim restore keeps comments


def test_materialize_empty_stashed_value_does_not_clobber(tmp_path: Path):
    """An empty stashed value must not overwrite a real source value."""
    src = _make_source(tmp_path, with_env="SUT_BASE_URL=http://placeholder\n")
    dst = tmp_path / "sut"

    _materialize_sut(str(src), dst, run_id="r1")
    # Prior-run .env has the key present but blank.
    (dst / ".env").write_text("SUT_BASE_URL=\n", encoding="utf-8")

    _materialize_sut(str(src), dst, run_id="r1")

    vals = dotenv_values(dst / ".env")
    assert vals["SUT_BASE_URL"] == "http://placeholder"  # source value kept


def test_materialize_gitignores_source_env(tmp_path: Path):
    """A source-shipped `.env` must be gitignored (untracked) after materialize,
    yet remain on disk so a human can run the generated tests locally."""
    src = _make_source(tmp_path, with_env="SECRET=abc\n")
    dst = tmp_path / "sut"

    _materialize_sut(str(src), dst, run_id="r1")

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(dst),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert ".env" not in tracked  # never committed into the reviewable branch
    assert (dst / ".env").is_file()  # still on disk for local runs
    assert ".env" in (dst / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_env_survives_git_reset_hard_rollback(tmp_path: Path):
    """End-to-end (A): even after a step commits and the cleanup `git reset
    --hard` rolls the branch back, the untracked `.env` (with HITL values)
    survives on disk."""
    src = _make_source(tmp_path, with_env="SUT_BASE_URL=http://placeholder\n")
    dst = tmp_path / "sut"

    _materialize_sut(str(src), dst, run_id="r1")
    merge_dotenv_file(dst / ".env", {"SUT_BASE_URL": "https://real.example.com"})

    # Simulate a Step 7 commit (add -A skips the gitignored .env) + a later
    # cleanup rollback to before it.
    git_id = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *git_id, "add", "-A"], cwd=str(dst), check=True)
    subprocess.run(
        ["git", *git_id, "commit", "-q", "--allow-empty", "-m", "qtea/step-07: x"],
        cwd=str(dst),
        check=True,
    )
    subprocess.run(
        ["git", "reset", "--hard", "HEAD~1"],
        cwd=str(dst),
        check=True,
        capture_output=True,
    )

    assert (
        dotenv_values(dst / ".env")["SUT_BASE_URL"] == "https://real.example.com"
    )
