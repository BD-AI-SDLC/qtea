"""Regression tests for `replay_env_from_artifacts`.

Step 6's `resolve_sut_env()` writes into `os.environ` in-process only. When
the user re-runs `qtea run --from-step 7+` in a new process, those writes
are gone — `SUT_BASE_URL` ends up unset and Step 8 aborts with
`BASE_URL_UNRESOLVED`. `replay_env_from_artifacts` re-populates os.environ
from the persisted Step 6 artifacts so re-runs work as expected.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from qtea.steps.s06_research import replay_env_from_artifacts
from qtea.workspace import Workspace


@pytest.fixture
def clean_env(monkeypatch):
    """Each test starts with the URL-related env vars unset."""
    for k in ("SUT_BASE_URL", "QA_URL", "STAGING_URL", "PRODUCTION_URL"):
        monkeypatch.delenv(k, raising=False)


def _make_workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(root=tmp_path, run_id="test-run-id")
    ws.ensure_layout()
    return ws


@dataclass
class _Opts:
    env_file: Path | None = None
    no_hitl: bool = True


def test_replay_returns_false_when_no_research_artifact(tmp_path: Path, clean_env):
    ws = _make_workspace(tmp_path)
    assert replay_env_from_artifacts(ws, _Opts()) is False


def test_replay_returns_false_when_no_env_keys(tmp_path: Path, clean_env):
    ws = _make_workspace(tmp_path)
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({"title": "x", "sections": [], "sut_env_keys": []}),
        encoding="utf-8",
    )
    assert replay_env_from_artifacts(ws, _Opts()) is False


def test_replay_loads_qa_url_from_sut_dotenv_and_mirrors(tmp_path: Path, clean_env, monkeypatch):
    """The happy path: research.json + url_resolution.json + SUT .env file
    in place → QA_URL is loaded from .env into os.environ and mirrored to
    SUT_BASE_URL."""
    ws = _make_workspace(tmp_path)
    # Write a SUT .env (Step 6 reads this via DotenvFileStrategy).
    (ws.sut / ".env").write_text("QA_URL=https://qa.example.com\n", encoding="utf-8")
    # Persist Step 6 artifacts.
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "x", "sections": [],
            "sut_env_keys": ["QA_URL"],
            "url_resolution": {"key": "QA_URL", "source": "basesettings_alias"},
        }),
        encoding="utf-8",
    )
    assert replay_env_from_artifacts(ws, _Opts()) is True
    assert os.environ["QA_URL"] == "https://qa.example.com"
    assert os.environ["SUT_BASE_URL"] == "https://qa.example.com"


def test_replay_mirrors_when_qa_url_already_in_process_env(tmp_path: Path, clean_env, monkeypatch):
    """When QA_URL is already in os.environ (from --env-file at pipeline start),
    the replay still mirrors it to SUT_BASE_URL without touching QA_URL."""
    ws = _make_workspace(tmp_path)
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "x", "sections": [],
            "sut_env_keys": ["QA_URL"],
            "url_resolution": {"key": "QA_URL"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("QA_URL", "https://from-env-file.example.com")
    assert replay_env_from_artifacts(ws, _Opts()) is True
    assert os.environ["SUT_BASE_URL"] == "https://from-env-file.example.com"


def test_replay_does_not_overwrite_existing_sut_base_url(tmp_path: Path, clean_env, monkeypatch):
    """If the user explicitly set SUT_BASE_URL (e.g. via --env-file or shell),
    the replay must not overwrite it."""
    ws = _make_workspace(tmp_path)
    (ws.sut / ".env").write_text("QA_URL=https://qa.example.com\n", encoding="utf-8")
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "x", "sections": [],
            "sut_env_keys": ["QA_URL"],
            "url_resolution": {"key": "QA_URL"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUT_BASE_URL", "https://explicit-override.example.com")
    replay_env_from_artifacts(ws, _Opts())
    # SUT_BASE_URL is preserved; QA_URL still gets resolved from .env.
    assert os.environ["SUT_BASE_URL"] == "https://explicit-override.example.com"
    assert os.environ.get("QA_URL") == "https://qa.example.com"


def test_replay_handles_corrupt_research_json(tmp_path: Path, clean_env):
    ws = _make_workspace(tmp_path)
    (ws.step_dir(6) / "research.json").write_text("{ not valid json", encoding="utf-8")
    # Should not raise; returns False.
    assert replay_env_from_artifacts(ws, _Opts()) is False


def test_replay_mirrors_when_url_key_in_env_but_optional_keys_missing(
    tmp_path: Path, clean_env, monkeypatch,
):
    """Regression: the common case is QA_URL in user's shell env + 20 other
    optional keys (HEADLESS, SCREEN_WIDTH, etc.) NOT in env. The resolver
    would find nothing for the optional keys and the mirror would never fire
    — leaving SUT_BASE_URL unset. The fix: mirror must run as a final step
    regardless of what the resolver found.
    """
    ws = _make_workspace(tmp_path)
    # 25 optional keys not present anywhere + 1 URL key already in env.
    optional_keys = [f"OPTIONAL_KEY_{i}" for i in range(25)]
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "x", "sections": [],
            "sut_env_keys": ["QA_URL", *optional_keys],
            "url_resolution": {"key": "QA_URL", "source": "basesettings_alias"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("QA_URL", "https://qa.bosch.com/")
    # SUT_BASE_URL deliberately not set.
    assert replay_env_from_artifacts(ws, _Opts()) is True
    assert os.environ["SUT_BASE_URL"] == "https://qa.bosch.com/"


def test_replay_returns_true_when_only_mirror_fired(tmp_path: Path, clean_env, monkeypatch):
    """When the resolver finds nothing but the URL is already in env, the
    function must still report success (True) because it DID set SUT_BASE_URL.
    """
    ws = _make_workspace(tmp_path)
    (ws.step_dir(6) / "research.json").write_text(
        json.dumps({
            "title": "x", "sections": [],
            "sut_env_keys": ["QA_URL", "OPTIONAL_NOT_SET"],
            "url_resolution": {"key": "QA_URL"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("QA_URL", "https://qa.example.com")
    assert replay_env_from_artifacts(ws, _Opts()) is True
    assert os.environ["SUT_BASE_URL"] == "https://qa.example.com"
