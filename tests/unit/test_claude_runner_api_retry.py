"""api_retry circuit-breaker tests for claude_runner._drive_query.

Background: run 20260603-205851-2d359f (attempt 1) burned ~60 min of
wall-clock because the SDK silently retried failing tool calls 8 times
(~95 s of exponential backoff) before the 1800 s step timeout fired —
no handler for `SystemMessage(subtype="api_retry")` in the runner.

The circuit breaker added in response:

- Counts consecutive `api_retry` SystemMessages with NO intervening
  AssistantMessage / ResultMessage / UserMessage.
- When the counter hits the active threshold (default 8; env-overridable
  via `WORCA_T_API_RETRY_THRESHOLD` in `[1, 10]`), raises `_ApiRetryStorm`
  which `run_agent` translates into a failed AgentResult with a clear,
  actionable error message.
- Each `api_retry` is also logged at WARNING level so an in-progress
  flake is visible in real time (not just in the post-mortem transcript).

The default threshold of 8 was tuned against observed Vertex behavior:
each retried API call can hang for ~3 minutes before the SDK gives up
and bumps the counter (see run 20260611-075728-0aa560 — 5 retries took
~15 min wall-clock even though the SDK's own exp-backoff delays summed
to only ~18 s). Threshold=5 was too tight for that profile — it aborted
on attempts that had already done all the file reads needed. Threshold=8
keeps the worst-case wall-clock under ~25 min (still inside the 1800 s
step timeout) while letting genuine 5-10 min Vertex incident windows
recover. Earlier history: run 20260603 (attempt 1) with threshold=3
aborted at ~4 s after the agent had completed 434 events of real work
— an over-correction we now avoid.
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
    assert _api_retry_storm_threshold() == _API_RETRY_STORM_THRESHOLD_DEFAULT == 8


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
# Default-threshold storm behavior (threshold=8)
# ---------------------------------------------------------------------------


async def test_api_retry_storm_aborts_at_default_threshold(tmp_path: Path, monkeypatch):
    """8 consecutive api_retry events at the default threshold → abort.

    Threshold tuned for observed Vertex per-call hang behavior (~3 min per
    retry); 8 is the first that trips the breaker."""
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
        _retry_event(6, sid),
        _retry_event(7, sid),
        _retry_event(8, sid),
        # Should NEVER be reached — circuit breaker fires on retry #8.
        {"type": "result", "result": "should-not-reach", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):
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
    assert "threshold=8" in err
    # Actionable guidance must be in the message for the operator.
    assert "re-run" in err.lower() or "rerun" in err.lower()
    # Drive aborted before reaching the ResultMessage.
    assert "should-not-reach" not in (result.final_text or "")


async def test_api_retry_below_threshold_does_not_abort(tmp_path: Path, monkeypatch):
    """7 consecutive api_retry events with default threshold=8 must NOT
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
        _retry_event(4, sid),
        _retry_event(5, sid),  # threshold=5 (older default) would have aborted here
        _retry_event(6, sid),
        _retry_event(7, sid),  # threshold=8 (current) still tolerates
        {"type": "result", "result": "recovered after burst", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):
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

    async def _fake_query(*, prompt, options=None, transport=None):
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

    async def _fake_query(*, prompt, options=None, transport=None):
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


# ---------------------------------------------------------------------------
# _ApiFatalError: immediate abort on 4xx / 5xx HTTP status
# ---------------------------------------------------------------------------


def _retry_event_with_status(
    attempt: int, session_id: str, *, error_status: int, error: str = "server_error",
) -> dict:
    return {
        "type": "system",
        "subtype": "api_retry",
        "data": {
            "attempt": attempt,
            "retry_delay_ms": 500,
            "error_status": error_status,
            "error": error,
        },
        "session_id": session_id,
    }


async def test_api_fatal_error_aborts_on_first_4xx(tmp_path: Path, monkeypatch):
    """A single api_retry with HTTP 403 must abort immediately — no storm
    threshold, no retry budget. 4xx errors (auth, quota, permissions) are
    never retryable."""
    from ._fake_claude import _make_message

    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-403"
    messages = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        {"type": "assistant", "content": [], "session_id": sid},
        _retry_event_with_status(1, sid, error_status=403, error="Out of bandwidth quota"),
        {"type": "result", "result": "should-not-reach", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):
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
    assert result.exit_code == -11
    err = result.error or ""
    assert "HTTP 403" in err
    assert "Non-retryable" in err
    assert "should-not-reach" not in (result.final_text or "")


async def test_api_fatal_error_aborts_on_first_5xx(tmp_path: Path, monkeypatch):
    """A single api_retry with HTTP 500 must also abort immediately — sustained
    outages waste the step timeout budget without hope of recovery."""
    from ._fake_claude import _make_message

    monkeypatch.delenv("WORCA_T_API_RETRY_THRESHOLD", raising=False)
    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    sid = "sess-500"
    messages = [
        {"type": "system", "subtype": "init",
         "data": {"session_id": sid, "mcp_servers": []},
         "session_id": sid},
        _retry_event_with_status(1, sid, error_status=500, error="server_error"),
        {"type": "result", "result": "should-not-reach", "session_id": sid},
    ]

    async def _fake_query(*, prompt, options=None, transport=None):
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
    assert result.exit_code == -11
    err = result.error or ""
    assert "HTTP 500" in err
    assert "Non-retryable" in err
