"""Step 9 runner-failure diagnostics — stderr AND stdout must both surface.

Regression guard for a class of failure where Playwright's real
SyntaxError lived in the JSON reporter's stdout but the failure-context
handed to the debug agent only carried a 27-byte stderr snippet (the
benign `qtea {"event":"installed"}` marker). The prior `elif` collapsed
stdout as soon as stderr was non-empty.

Also asserts HEAD slices are included, not just TAIL — parse errors and
module-not-found errors appear at the head of the output, and a tail-only
snippet drops exactly the line the debug agent needs.
"""

from __future__ import annotations

from qtea.steps.s09_execute import _compose_runner_stream_diagnostics


def test_both_streams_appear_when_both_non_empty():
    stderr = "qtea {\"event\":\"installed\"}"
    stdout = (
        "SyntaxError: qtea_entity_approval_test.spec.ts: Unexpected token (1:0)\n"
        "> 1 | # Stack: typescript+playwright\n"
    )
    out = _compose_runner_stream_diagnostics(stderr, stdout)
    assert "stderr" in out and "installed" in out
    assert "stdout" in out and "SyntaxError" in out
    assert "# Stack: typescript+playwright" in out


def test_short_streams_shown_once_not_head_tail_bracketed():
    """Streams under the 3000-char cutoff are surfaced whole — no need to
    split into HEAD/TAIL when the whole thing already fits."""
    out = _compose_runner_stream_diagnostics("short err", "short out")
    assert "--- stderr ---" in out
    assert "--- stdout ---" in out
    assert "HEAD" not in out and "TAIL" not in out


def test_long_streams_get_head_and_tail_slices():
    """A > 3000 char stream is split into HEAD (first 1500) + TAIL (last 1500)
    so a head-of-output message (parse error, missing module) survives."""
    long_stdout = "HEAD-MARKER\n" + ("filler\n" * 800) + "TAIL-MARKER"
    assert len(long_stdout) > 3000
    out = _compose_runner_stream_diagnostics("", long_stdout)
    assert "HEAD (first 1500)" in out
    assert "TAIL (last 1500)" in out
    assert "HEAD-MARKER" in out
    assert "TAIL-MARKER" in out


def test_empty_streams_produce_empty_appendix():
    assert _compose_runner_stream_diagnostics(None, None) == ""
    assert _compose_runner_stream_diagnostics("", "") == ""
    assert _compose_runner_stream_diagnostics("   \n  ", "") == ""


def test_stderr_only_still_surfaces_stderr():
    """When stdout is empty, stderr still gets surfaced (regression guard
    against a naive rewrite that only shows stdout)."""
    out = _compose_runner_stream_diagnostics("pytest: error: unknown arg", "")
    assert "stderr" in out
    assert "unknown arg" in out
    assert "stdout" not in out


def test_stdout_only_still_surfaces_stdout():
    """The failure that broke run 20260701-114656-9394eb: stdout carried the
    real error, stderr was empty. This must surface stdout."""
    out = _compose_runner_stream_diagnostics(
        "",
        "SyntaxError: Unexpected token (1:0)",
    )
    assert "stdout" in out
    assert "SyntaxError" in out


def test_token_shaped_secrets_are_masked_in_both_streams():
    """result.error (built from this appendix) flows into run-results.json,
    the HTML report, and the bug classifier — a token the SUT printed on
    failure must not ride along verbatim."""
    leaked_token = "ghp_" + ("b" * 36)
    out = _compose_runner_stream_diagnostics(
        f"stderr had a cached token {leaked_token}",
        f"stdout dumped header Authorization: token {leaked_token}",
    )
    assert leaked_token not in out
    assert out.count("***REDACTED***") == 2
