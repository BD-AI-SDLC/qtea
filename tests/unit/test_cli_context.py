"""CLI --context / --context-file resolution tests.

Exercises how `qtea run` turns the two context flags into
`PipelineOptions.operator_context`, without actually running the pipeline
(run_pipeline is stubbed to capture the constructed options).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from qtea.cli import MAX_OPERATOR_CONTEXT_CHARS, app

runner = CliRunner()


@pytest.fixture
def captured_opts(monkeypatch):
    """Stub run_pipeline + ensure_node; capture the PipelineOptions built by run()."""
    captured: dict = {}

    async def _fake_run_pipeline(opts, console=None):
        captured["opts"] = opts
        return 0

    monkeypatch.setattr("qtea.pipeline.run_pipeline", _fake_run_pipeline)
    monkeypatch.setattr("qtea.node_env.ensure_node", lambda **_kw: None)
    return captured


def test_context_flag_sets_operator_context(captured_opts):
    result = runner.invoke(
        app, ["run", "--spec", "x", "--sut", ".", "--context", "focus checkout"]
    )
    assert result.exit_code == 0, result.output
    assert captured_opts["opts"].operator_context == "focus checkout"


def test_context_file_read_into_operator_context(tmp_path: Path, captured_opts):
    ctx_file = tmp_path / "ctx.md"
    ctx_file.write_text("staging at https://stg.example\n", encoding="utf-8")
    result = runner.invoke(
        app, ["run", "--spec", "x", "--sut", ".", "--context-file", str(ctx_file)]
    )
    assert result.exit_code == 0, result.output
    assert captured_opts["opts"].operator_context == "staging at https://stg.example"


def test_context_and_context_file_are_mutually_exclusive(tmp_path: Path, captured_opts):
    ctx_file = tmp_path / "ctx.md"
    ctx_file.write_text("hello", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run", "--spec", "x", "--sut", ".",
            "--context", "hello", "--context-file", str(ctx_file),
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert "opts" not in captured_opts  # never reached run_pipeline


def test_no_context_leaves_operator_context_none(captured_opts):
    result = runner.invoke(app, ["run", "--spec", "x", "--sut", "."])
    assert result.exit_code == 0, result.output
    assert captured_opts["opts"].operator_context is None


def test_blank_context_normalizes_to_none(captured_opts):
    result = runner.invoke(
        app, ["run", "--spec", "x", "--sut", ".", "--context", "   "]
    )
    assert result.exit_code == 0, result.output
    assert captured_opts["opts"].operator_context is None


def test_oversized_context_is_truncated(captured_opts):
    big = "x" * (MAX_OPERATOR_CONTEXT_CHARS + 500)
    result = runner.invoke(
        app, ["run", "--spec", "x", "--sut", ".", "--context", big]
    )
    assert result.exit_code == 0, result.output
    assert len(captured_opts["opts"].operator_context) == MAX_OPERATOR_CONTEXT_CHARS
