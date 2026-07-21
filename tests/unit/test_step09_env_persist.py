"""Regression tests for Step 9's `_persist_env_vars`.

HITL-recovered env vars (from a `missing_env` runner failure) must survive on
disk even when `<sut>/.env` didn't exist before the recovery fired —
otherwise the values that fixed the Step 9 retry are lost once the workspace
is cleaned up, and a test engineer running the SUT standalone later hits the
same missing-env failure.
"""

from __future__ import annotations

from pathlib import Path

from qtea.steps.s09_execute import _persist_env_vars
from qtea.workspace import Workspace


def _make_workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(root=tmp_path, run_id="test-run-id")
    ws.ensure_layout()
    return ws


def test_creates_sut_env_when_missing(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    assert not (ws.sut / ".env").exists()

    _persist_env_vars(ws, {"USERNAME_APP": "tester", "PASSWORD_APP": "secret123"})

    sut_env = (ws.sut / ".env").read_text(encoding="utf-8")
    assert "USERNAME_APP=tester" in sut_env
    assert "PASSWORD_APP=secret123" in sut_env


def test_adds_gitignore_entry_when_missing(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    assert not (ws.sut / ".gitignore").exists()

    _persist_env_vars(ws, {"QA_URL": "https://qa.example.com"})

    gitignore = (ws.sut / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.splitlines()


def test_adds_gitignore_entry_to_existing_file_without_duplicating(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    (ws.sut / ".gitignore").write_text("node_modules/\n.env\n", encoding="utf-8")

    _persist_env_vars(ws, {"QA_URL": "https://qa.example.com"})

    lines = (ws.sut / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".env") == 1
    assert "node_modules/" in lines


def test_merges_into_existing_sut_env_without_clobbering(tmp_path: Path):
    ws = _make_workspace(tmp_path)
    (ws.sut / ".env").write_text(
        "# existing comment\nEXISTING_KEY=keep_me\nQA_URL=old_value\n",
        encoding="utf-8",
    )

    _persist_env_vars(ws, {"QA_URL": "new_value", "NEW_KEY": "new_val"})

    text = (ws.sut / ".env").read_text(encoding="utf-8")
    assert "# existing comment" in text
    assert "EXISTING_KEY=keep_me" in text
    assert "QA_URL=new_value" in text
    assert "QA_URL=old_value" not in text
    assert "NEW_KEY=new_val" in text


def test_no_op_when_env_vars_empty(tmp_path: Path):
    ws = _make_workspace(tmp_path)

    _persist_env_vars(ws, {})

    assert not (ws.sut / ".env").exists()
