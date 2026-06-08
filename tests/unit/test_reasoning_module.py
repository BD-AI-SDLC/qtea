"""Tests for :mod:`worca_t.llm.reasoning`.

Covers the direct-SDK transport in isolation — no step files involved.
The reasoning module is exercised by the soon-to-be-migrated step
tests (test_step02_refine, test_step10_bug_classifier, etc.) too, but
those tests focus on step orchestration. This file focuses on the
``call_reasoning_llm`` contract: audit-file shape, metrics accumulation,
HITL history, schema passing, model fallback, error propagation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit._fake_anthropic import FakeUsage, install_fake_anthropic
from worca_t.llm.reasoning import call_reasoning_llm
from worca_t.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator


def _write_agent_file(tmp_path: Path, name: str = "test-agent.agent.md") -> Path:
    """Create a minimal ``.agent.md`` fixture file."""
    agent_file = tmp_path / name
    agent_file.write_text("# Test Agent\n\nYou are a test agent.", encoding="utf-8")
    return agent_file


# ---------------------------------------------------------------------------
# Happy-path returns and audit file shape
# ---------------------------------------------------------------------------

async def test_returns_text_on_success(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="hello world")
    agent = _write_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=workdir,
        user_prompt="say hi",
        model="claude-sonnet-4-6",
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.final_text == "hello world"
    assert result.error is None
    assert result.transcript_path.exists()
    assert result.stderr_path.exists()
    assert result.metrics_path.exists()
    assert result.mcp_servers_failed == []
    assert result.session_id is None


async def test_audit_files_use_numbered_naming(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=workdir,
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert result.transcript_path.name == "transcript-00.jsonl"
    assert result.stderr_path.name == "stderr-00.log"
    assert result.metrics_path.name == "metrics-00.json"


async def test_repeat_calls_increment_audit_suffix(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)
    workdir = tmp_path / "wd"

    r1 = await call_reasoning_llm(
        agent_path=agent, workdir=workdir, user_prompt="x", model="claude-sonnet-4-6"
    )
    r2 = await call_reasoning_llm(
        agent_path=agent, workdir=workdir, user_prompt="y", model="claude-sonnet-4-6"
    )

    assert r1.transcript_path.name == "transcript-00.jsonl"
    assert r2.transcript_path.name == "transcript-01.jsonl"


async def test_metrics_json_has_transport_marker(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)
    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    metrics = json.loads(result.metrics_path.read_text())
    assert metrics["transport"] == "direct-sdk-reasoning"
    assert metrics["success"] is True


# ---------------------------------------------------------------------------
# Input handling: inlining, prompt construction
# ---------------------------------------------------------------------------

async def test_inputs_inlined_into_user_prompt(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="analyze this:",
        inputs={"data.json": '{"x": 1}'},
        model="claude-sonnet-4-6",
    )

    user_content = captured["messages"][0]["content"]
    assert "analyze this:" in user_content
    assert "data.json" in user_content
    assert '"x": 1' in user_content
    assert "```json" in user_content


async def test_agent_md_loaded_as_system_prompt(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = tmp_path / "custom.agent.md"
    agent.write_text("SYSTEM-PROMPT-SENTINEL", encoding="utf-8")

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert captured["system"] == "SYSTEM-PROMPT-SENTINEL"


# ---------------------------------------------------------------------------
# Schema (output_config.format) passing
# ---------------------------------------------------------------------------

async def test_output_schema_passed_when_provided(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
        "additionalProperties": False,
    }

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        output_schema=schema,
        model="claude-sonnet-4-6",
    )

    assert "output_config" in captured
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["output_config"]["format"]["schema"] == schema


async def test_no_output_config_when_schema_is_none(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert "output_config" not in captured


# ---------------------------------------------------------------------------
# Metrics accumulator integration
# ---------------------------------------------------------------------------

async def test_metrics_pushed_to_step_accumulator(tmp_path, monkeypatch):
    install_fake_anthropic(
        monkeypatch,
        text="ok",
        usage=FakeUsage(input_tokens=200, output_tokens=80, cache_read_input_tokens=50),
    )
    agent = _write_agent_file(tmp_path)
    acc = StepMetricsAccumulator()
    token = CURRENT_STEP_METRICS.set(acc)
    try:
        await call_reasoning_llm(
            agent_path=agent,
            workdir=tmp_path / "wd",
            user_prompt="x",
            model="claude-sonnet-4-6",
        )
    finally:
        CURRENT_STEP_METRICS.reset(token)

    assert acc.agent_calls == 1
    assert acc.totals.input_tokens == 200
    assert acc.totals.output_tokens == 80
    assert acc.totals.cache_read_input_tokens == 50


async def test_works_without_step_accumulator(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)
    # No accumulator set; CURRENT_STEP_METRICS.get() returns None.
    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )
    assert result.success is True
    assert result.metrics.input_tokens == 100  # default FakeUsage


# ---------------------------------------------------------------------------
# Error propagation + audit-file persistence on failure
# ---------------------------------------------------------------------------

async def test_error_propagates_with_audit_files(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, raises=RuntimeError("api broke"))
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert result.success is False
    assert result.exit_code == -1
    assert "api broke" in result.error
    assert result.transcript_path.exists()
    assert result.stderr_path.exists()
    assert result.metrics_path.exists()
    # stderr file captures the error text for forensic review
    assert "api broke" in result.stderr_path.read_text()


# ---------------------------------------------------------------------------
# HITL history support (multi-turn conversation)
# ---------------------------------------------------------------------------

async def test_hitl_history_prepended_to_messages(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)

    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer [CLARIFICATION NEEDED: foo]"},
    ]
    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="answer to foo: bar",
        hitl_history=history,
        model="claude-sonnet-4-6",
    )

    msgs = captured["messages"]
    assert len(msgs) == 3
    assert msgs[0]["content"] == "first question"
    assert msgs[1]["role"] == "assistant"
    assert msgs[2]["content"].startswith("answer to foo: bar")


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

async def test_missing_agent_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await call_reasoning_llm(
            agent_path=tmp_path / "nope.agent.md",
            workdir=tmp_path / "wd",
            user_prompt="x",
            model="claude-sonnet-4-6",
        )


async def test_no_model_resolved_raises(tmp_path, monkeypatch):
    # Force model_for_agent() to return None and don't pass model=
    monkeypatch.setattr("worca_t.llm.reasoning.model_for_agent", lambda _: None)
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    with pytest.raises(ValueError, match="No model resolved"):
        await call_reasoning_llm(
            agent_path=agent,
            workdir=tmp_path / "wd",
            user_prompt="x",
        )


# ---------------------------------------------------------------------------
# Model fallback chain
# ---------------------------------------------------------------------------

async def test_model_fallback_on_unavailable_error(tmp_path, monkeypatch):
    """On 'overloaded' / model-unavailable errors, the chain advances."""
    call_log: list[str] = []

    class FailFirstThenSucceed:
        def __init__(self):
            self.call_n = 0

        async def __call__(self, **kwargs):
            self.call_n += 1
            call_log.append(kwargs.get("model", "?"))
            if self.call_n == 1:
                raise RuntimeError("model overloaded — please retry")
            from tests.unit._fake_anthropic import (
                FakeResponse, FakeTextBlock, FakeUsage,
            )
            return FakeResponse(content=[FakeTextBlock(text="ok")], usage=FakeUsage())

    handler = FailFirstThenSucceed()

    class FakeMessages:
        def __init__(self):
            self.create = handler

    class FakeClient:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert result.success is True
    assert handler.call_n >= 2  # first call failed, second succeeded
    # First attempted model was the requested one, second was the fallback
    assert call_log[0] == "claude-sonnet-4-6"
    assert call_log[1] != call_log[0]


async def test_non_unavailable_error_does_not_trigger_fallback(tmp_path, monkeypatch):
    """A generic non-unavailability error stops at the first attempt."""
    install_fake_anthropic(monkeypatch, raises=RuntimeError("bad request"))
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    assert result.success is False
    metrics = json.loads(result.metrics_path.read_text())
    assert len(metrics["models_attempted"]) == 1


# ---------------------------------------------------------------------------
# Model ID normalisation (@<date> → -<date>)
# ---------------------------------------------------------------------------

async def test_at_date_suffix_normalized_to_dash(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-haiku-4-5@20251001",
    )

    assert captured["model"] == "claude-haiku-4-5-20251001"
