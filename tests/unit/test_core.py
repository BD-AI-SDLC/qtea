"""Sanity tests for config + workspace + proxy + checkpoints."""

from __future__ import annotations

from pathlib import Path

from worca_t.checkpoints import RunState, StepRecord, hash_paths, load_state, save_state
from worca_t.config import (
    DEFAULT_STEP_TIMEOUTS,
    MAX_STEP_TIMEOUT_S,
    agent_model_map,
    model_for_agent,
    step_timeout,
)
from worca_t.proxy import mask_secrets, with_proxy_env
from worca_t.workspace import create_workspace, generate_run_id


def test_run_id_format():
    rid = generate_run_id()
    assert len(rid) >= 22
    assert rid[8] == "-" and rid[15] == "-"


def test_workspace_layout(tmp_path: Path):
    ws = create_workspace(tmp_path)
    assert ws.root.exists()
    for i in range(1, 12):
        assert ws.step_dir(i).exists()
    assert ws.artifacts.exists()
    assert ws.debug.exists()
    assert ws.sut.exists()


def test_agent_model_map_loaded():
    m = agent_model_map()
    assert m["ui-test-automation"] == "claude-opus-4-6"
    assert model_for_agent("polyglot-test-fixer") == "claude-opus-4-6"
    assert model_for_agent("does-not-exist") is None


def test_step_timeout_caps():
    for s, t in DEFAULT_STEP_TIMEOUTS.items():
        assert step_timeout(s) == t
        assert step_timeout(s) <= MAX_STEP_TIMEOUT_S
    assert step_timeout(99, override=99999) == MAX_STEP_TIMEOUT_S


def test_proxy_mirroring(monkeypatch):
    # On Windows os.environ is case-insensitive at the OS layer, so setting
    # HTTPS_PROXY automatically makes https_proxy visible. On POSIX our
    # explicit mirroring in with_proxy_env() does the same. Either way the
    # returned env must expose both spellings.
    # Clear any pre-existing lower-case value (corp registry may set it) so
    # we're testing the mirroring path, not env-merge precedence.
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = with_proxy_env()
    assert env.get("HTTPS_PROXY") == "http://proxy:3128"
    assert env.get("https_proxy") == "http://proxy:3128"


def test_mask_secrets():
    masked = mask_secrets({"ANTHROPIC_API_KEY": "supersecret", "OTHER": "ok"})
    assert masked["ANTHROPIC_API_KEY"] == "***REDACTED***"
    assert masked["OTHER"] == "ok"


def test_checkpoint_roundtrip(tmp_path: Path):
    rs = RunState(run_id="r1", workspace=str(tmp_path), spec_source="x", sut_source="y")
    rs.steps[1] = StepRecord(step=1, name="intake", status="completed", attempts=1)
    out = tmp_path / "state.json"
    save_state(rs, out)
    loaded = load_state(out)
    assert loaded is not None
    assert loaded.steps[1].status == "completed"
    assert loaded.steps[1].name == "intake"


def test_hash_paths(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    h = hash_paths([f, tmp_path / "missing"])
    assert "a.txt" in h
    assert len(h["a.txt"]) == 64
