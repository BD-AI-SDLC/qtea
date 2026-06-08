"""api_retry circuit-breaker tests for claude_runner._drive_query.

Background: run 20260603-205851-2d359f (attempt 1) burned ~60 min of
wall-clock because the SDK silently retried failing tool calls 8 times
(~95 s of exponential backoff) before the 1800 s step timeout fired —
no handler for `SystemMessage(subtype="api_retry")` in the runner.

The circuit breaker added in response:

- Counts consecutive `api_retry` SystemMessages with NO intervening
  AssistantMessage / ResultMessage / UserMessage.
- When the counter hits the active threshold (default 5; env-overridable
  via `WORCA_T_API_RETRY_THRESHOLD` in `[1, 10]`), raises `_ApiRetryStorm`
  which `run_agent` translates into a failed AgentResult with a clear,
  actionable error message.
- Each `api_retry` is also logged at WARNING level so an in-progress
  flake is visible in real time (not just in the post-mortem transcript).

The default threshold of 5 was chosen against the SDK's exp-backoff
schedule (`retry_delay_ms`: 0.5s, 1s, 2s, 4s, 9s, ...). At 5 retries,
the cumulative wait is ~18 s — short enough to bound waste from a stuck
agent loop, long enough to ride out the typical 2-4 retry Anthropic /
Vertex transient bursts that DO recover. Run 20260603 (attempt 1) with
threshold=3 aborted at ~4 s on a healthy-but-flaky API window after the
agent had completed 434 events of real work — an over-correction we
now avoid.
"""

from __future__ import annotations

from pathlib import Path

from worca_t.claude_runner import (
    _API_RETRY_STORM_THRESHOLD_DEFAULT,
    _api_retry_storm_threshold,
    run_agent,
)


def _fake_agent_file(tmp_path: Path) -> Path:
    """Minimal agent .md so run_agent's staging passes."""
    p = tmp_path / "fake-agent.agent.md"
    p.write_text("# fake agent\n", encoding="utf-8")
    return p


def _retry_event(attempt: int, session_id: str) -> dict:
    """Shape-accurate `api_retry` SystemMessage spec for the fake SDK."""
    return {
        "type": "system",
        "subtype": "api_retry",
        "data": {
            "attempt": attempt,
            "retry_delay_ms": 500 * (2 ** (attempt - 1)),
            "error_status": None,
            "error": "unknown",
        },
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Threshold-resolution helper
# ---------------------------------------------------------------------------


def test_api_retry_threshold_default(monkeypatch):
    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT == 5


def test_api_retry_threshold_env_override(monkeypatch):
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "7")
    assert _api_retry_storm_threshold() == 7


def test_api_retry_threshold_env_invalid_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "not-a-number")
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT


def test_api_retry_threshold_env_out_of_range_falls_back(monkeypatch):
    # SDK gives up at 10 by default; anything above is unreachable.
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "99")
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT
    # Zero / negative is a misconfiguration (effectively "no SDK retries
    # tolerated" — the SDK always retries once before the first event).
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "0")
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "-1")
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT


def test_api_retry_threshold_env_empty_falls_back(monkeypatch):
    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "")
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT


# ---------------------------------------------------------------------------
# Default-threshold storm behavior (threshold=5)
# ---------------------------------------------------------------------------


async def test_api_retry_storm_aborts_at_default_threshold(tmp_path: Path, monkeypatch):
    """5 consecutive api_retry events at the default threshold → abort.

    Threshold tuned to absorb typical 2-4 retry transient Anthropic / Vertex
    bursts; 5 is the first that trips the breaker."""
    from ._fake_claude import _make_message

    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-default"
    storm = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        {"type": "assistant", "content": [], "session_id": sid},
        _retry_event(1, sid),
        _retry_event(2, sid),
        _retry_event(3, sid),
        _retry_event(4, sid),
        _retry_event(5, sid),
        # Should NEVER be reached — circuit breaker fires on retry #5.
        {"type": "result", "result": "should-not-reach", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        for spec in storm:
            yield _make_message(spec)

    monkeypatch.setattr("worca_t.claude_runner.query", _fake_query)

    agent_path = _fake_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await run_agent(
        agent_path,
        workdir=workdir,
        inputs={},
        user_prompt="anything",
        timeout_s=60,
    )

    assert result.success is False
    assert result.exit_code == -10
    err = result.error or ""
    assert "api_retry storm" in err
    assert "threshold=5" in err
    # Actionable guidance must be in the message for the operator.
    assert "re-run" in err.lower() or "rerun" in err.lower()
    # Drive aborted before reaching the ResultMessage.
    assert "should-not-reach" not in (result.final_text or "")


async def test_api_retry_below_threshold_does_not_abort(tmp_path: Path, monkeypatch):
    """4 consecutive api_retry events with default threshold=5 must NOT
    abort — this is exactly the regression we're guarding against (the
    over-aggressive threshold=3 from before would have aborted at retry 3,
    sacrificing real progress to a transient flake)."""
    from ._fake_claude import _make_message

    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-burst"
    messages = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        {"type": "assistant", "content": [], "session_id": sid},
        _retry_event(1, sid),
        _retry_event(2, sid),
        _retry_event(3, sid),  # threshold=3 (old) would have aborted here
        _retry_event(4, sid),  # threshold=5 (new) still tolerates
        {"type": "result", "result": "recovered after burst", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        for spec in messages:
            yield _make_message(spec)

    monkeypatch.setattr("worca_t.claude_runner.query", _fake_query)

    agent_path = _fake_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await run_agent(
        agent_path,
        workdir=workdir,
        inputs={},
        user_prompt="anything",
        timeout_s=60,
    )

    assert result.success is True, result.error
    assert result.final_text == "recovered after burst"


# ---------------------------------------------------------------------------
# Progress-reset behavior
# ---------------------------------------------------------------------------


async def test_api_retry_counter_resets_on_progress(tmp_path: Path, monkeypatch):
    """4 retries + AssistantMessage + 4 more retries → does NOT trip
    (intervening progress reset the counter; second burst stays below
    the new threshold)."""
    from ._fake_claude import _make_message

    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-reset"
    messages = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        _retry_event(1, sid),
        _retry_event(2, sid),
        _retry_event(3, sid),
        _retry_event(4, sid),
        # Progress → counter resets.
        {"type": "assistant", "content": [], "session_id": sid},
        _retry_event(1, sid),
        _retry_event(2, sid),
        _retry_event(3, sid),
        _retry_event(4, sid),
        {"type": "result", "result": "recovered twice", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        for spec in messages:
            yield _make_message(spec)

    monkeypatch.setattr("worca_t.claude_runner.query", _fake_query)

    agent_path = _fake_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await run_agent(
        agent_path,
        workdir=workdir,
        inputs={},
        user_prompt="anything",
        timeout_s=60,
    )

    assert result.success is True, result.error
    assert result.final_text == "recovered twice"


# ---------------------------------------------------------------------------
# Env-overridden threshold actually shifts the abort point
# ---------------------------------------------------------------------------


async def test_api_retry_env_override_lowers_threshold(tmp_path: Path, monkeypatch):
    """`WORCA_T_API_RETRY_THRESHOLD=2` → abort on the 2nd consecutive retry
    (useful for debugging agent-loop bugs, where any retry is suspicious)."""
    from ._fake_claude import _make_message

    monkeypatch.setenv("WORCA_T_API_RETRY_THRESHOLD", "2")
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-low"
    messages = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        _retry_event(1, sid),
        _retry_event(2, sid),
        {"type": "result", "result": "should-not-reach", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        for spec in messages:
            yield _make_message(spec)

    monkeypatch.setattr("worca_t.claude_runner.query", _fake_query)

    agent_path = _fake_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await run_agent(
        agent_path,
        workdir=workdir,
        inputs={},
        user_prompt="anything",
        timeout_s=60,
    )

    assert result.success is False
    assert result.exit_code == -10
    assert "threshold=2" in (result.error or "")
