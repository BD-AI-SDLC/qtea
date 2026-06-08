"""stderr-capture tests for claude_runner.run_agent.

Background: the Claude Agent SDK's subprocess transport (`claude` CLI) is
always launched with `--verbose`, but its stderr is only piped when the
caller registers an `options.stderr` callback. Before this wiring,
worca-t left the slot empty — discarding the underlying API error text
behind every `api_retry` event and leaving us with `error: "unknown"` in
post-mortems (run 20260603-205851-2d359f exemplified this: 8 retries
emitted, none with a recoverable diagnostic).

The fix in `run_agent`: open `stderr_path` with line-buffering and
register a closure that appends each subprocess stderr line. These tests
verify the callback is wired AND that lines actually land on disk when
the SDK invokes it.
"""

from __future__ import annotations

from pathlib import Path

from worca_t.claude_runner import run_agent


def _fake_agent_file(tmp_path: Path) -> Path:
    p = tmp_path / "fake-agent.agent.md"
    p.write_text("# fake\n", encoding="utf-8")
    return p


async def test_stderr_callback_is_registered(tmp_path: Path, monkeypatch):
    """`ClaudeAgentOptions.stderr` MUST be a callable after run_agent
    sets up its options — otherwise the SDK transport discards stderr
    (see subprocess_cli.py:472: `stderr_dest = PIPE if self._options.stderr
    is not None else None`)."""
    from ._fake_claude import _make_message

    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    captured: dict[str, object] = {}

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        captured["stderr_callback"] = getattr(options, "stderr", None)
        yield _make_message({"type": "result", "result": "ok",
                             "session_id": "sess-stderr"})

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

    assert result.success, result.error
    cb = captured["stderr_callback"]
    assert callable(cb), (
        "ClaudeAgentOptions.stderr must be a callable so the SDK pipes "
        "the subprocess stderr; otherwise debug output is silently dropped."
    )


async def test_stderr_callback_writes_lines_to_disk(tmp_path: Path, monkeypatch):
    """When the SDK transport invokes the registered callback (once per
    stderr line from the `claude` CLI), each line lands in `stderr_path`."""
    from ._fake_claude import _make_message

    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        # Simulate what the real subprocess_cli transport does: feed lines
        # into the callback one at a time. The CLI emits debug lines with
        # AND without trailing newlines — the callback must handle both.
        cb = options.stderr
        cb("[DEBUG] HTTP request: POST /v1/messages\n")
        cb("[ERROR] Connection reset by peer")  # no trailing \n
        cb("[DEBUG] retrying request (attempt 1)\n")
        yield _make_message({"type": "result", "result": "ok",
                             "session_id": "sess-write"})

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

    assert result.success
    contents = result.stderr_path.read_text(encoding="utf-8")
    # All three lines captured.
    assert "POST /v1/messages" in contents
    assert "Connection reset by peer" in contents
    assert "retrying request (attempt 1)" in contents
    # Newline normalisation: the line without trailing `\n` got one added,
    # so we have three distinct lines.
    assert contents.count("\n") == 3


async def test_stderr_callback_swallows_writes_after_file_closed(
    tmp_path: Path, monkeypatch,
):
    """The SDK's stderr-reader task may invoke the callback during async
    cleanup AFTER run_agent returns and the file handle is GC'd. The
    callback must absorb the resulting ValueError rather than crashing
    the stream-reader (which would surface as an opaque SDK error)."""
    from ._fake_claude import _make_message

    monkeypatch.setattr(
        "worca_t.claude_runner.shutil.which",
        lambda *_a, **_kw: "/fake/claude",
    )

    captured_cb: dict[str, object] = {}

    async def _fake_query(*, prompt, options=None, transport=None):  # noqa: ARG001
        captured_cb["cb"] = options.stderr
        yield _make_message({"type": "result", "result": "ok",
                             "session_id": "sess-close"})

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
    assert result.success

    # Force close the underlying file by reading + truncating the handle
    # via its OS-level state. Easier: call the callback after the file
    # handle should have been GC'd. Since closures keep the file alive,
    # we simulate the closed-file race by manually invoking the closure
    # AFTER explicitly closing the file via a backdoor.
    cb = captured_cb["cb"]
    # The captured closure holds a reference to the file. We can reach
    # it through __closure__ — used only here for the race-simulation test.
    fp = cb.__closure__[0].cell_contents  # type: ignore[union-attr]
    fp.close()

    # Now invoke the callback as the SDK would during late cleanup.
    # Must NOT raise.
    cb("late stderr line after cleanup\n")  # ValueError-on-closed
