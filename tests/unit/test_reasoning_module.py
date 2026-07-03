"""Tests for :mod:`qtea.llm.reasoning`.

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

from qtea.llm.reasoning import (
    call_reasoning_llm,
    reset_vertex_structured_outputs_warning_latch,
)
from qtea.metrics import CURRENT_STEP_METRICS, StepMetricsAccumulator
from tests.unit._fake_anthropic import (
    FakeUsage,
    disable_vertex_env,
    enable_vertex_env,
    install_fake_anthropic,
)


@pytest.fixture(autouse=True)
def _reset_vertex_warning_latch():
    """Reset the once-per-run latch so each test sees a clean slate."""
    reset_vertex_structured_outputs_warning_latch()
    yield
    reset_vertex_structured_outputs_warning_latch()


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
    disable_vertex_env(monkeypatch)
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
    disable_vertex_env(monkeypatch)
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


async def test_output_config_skipped_on_vertex(tmp_path, monkeypatch):
    """Vertex backends enforce
    ``constraints/vertexai.allowedPartnerModelFeatures`` which usually does
    not include ``structured_outputs`` for partner Anthropic models. Sending
    ``output_config`` triggers a 400 FAILED_PRECONDITION, so
    ``call_reasoning_llm`` must omit it on the Vertex path and rely on the
    caller's local ``is_valid()`` re-check for schema enforcement."""
    enable_vertex_env(monkeypatch)
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text='{"x": "y"}', on_call=captured.update)
    agent = _write_agent_file(tmp_path)
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        output_schema=schema,
        model="claude-sonnet-4-6",
    )

    assert "output_config" not in captured, (
        "Vertex backend disallows structured outputs; output_config "
        "must not be sent"
    )


async def test_vertex_structured_outputs_warning_fires_once_per_run(
    tmp_path, monkeypatch, caplog
):
    """The "structured outputs skipped on Vertex" banner is a one-time
    notice per process. Subsequent calls demote to debug-level so the same
    warning doesn't repeat for every reasoning step in a pipeline."""
    import logging

    enable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text='{"x": "y"}')
    agent = _write_agent_file(tmp_path)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    with caplog.at_level(logging.WARNING, logger="qtea.llm.reasoning"):
        for _ in range(3):
            await call_reasoning_llm(
                agent_path=agent,
                workdir=tmp_path / "wd",
                user_prompt="x",
                output_schema=schema,
                model="claude-sonnet-4-6",
            )

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "structured_outputs_skipped_vertex" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one warning across 3 calls; got {len(warnings)}"
    )


async def test_vertex_structured_outputs_warning_resets_between_runs(
    tmp_path, monkeypatch, caplog
):
    """After the explicit reset (simulating a fresh process / `qtea run`),
    the banner fires again on the next call."""
    import logging

    enable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text='{"x": "y"}')
    agent = _write_agent_file(tmp_path)
    schema = {"type": "object"}

    with caplog.at_level(logging.WARNING, logger="qtea.llm.reasoning"):
        await call_reasoning_llm(
            agent_path=agent, workdir=tmp_path / "wd",
            user_prompt="x", output_schema=schema, model="claude-sonnet-4-6",
        )
        reset_vertex_structured_outputs_warning_latch()
        await call_reasoning_llm(
            agent_path=agent, workdir=tmp_path / "wd",
            user_prompt="x", output_schema=schema, model="claude-sonnet-4-6",
        )

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "structured_outputs_skipped_vertex" in r.getMessage()
    ]
    assert len(warnings) == 2


async def test_vertex_fallback_strips_json_fences(tmp_path, monkeypatch):
    """When structured outputs is unavailable (Vertex), models sometimes
    wrap JSON in ```json ... ``` fences despite prompt instructions. The
    reasoning module strips them so downstream ``json.loads`` works."""
    enable_vertex_env(monkeypatch)
    payload = {"x": "y"}
    fenced = f"```json\n{json.dumps(payload)}\n```"
    install_fake_anthropic(monkeypatch, text=fenced)
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        model="claude-sonnet-4-6",
    )

    assert result.success
    # final_text is the stripped JSON, ready for json.loads.
    assert json.loads(result.final_text) == payload


async def test_vertex_fallback_leaves_unfenced_json_intact(tmp_path, monkeypatch):
    """If the model correctly returns bare JSON (no fences), the stripper
    is a no-op."""
    enable_vertex_env(monkeypatch)
    payload = {"x": "y"}
    install_fake_anthropic(monkeypatch, text=json.dumps(payload))
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        output_schema={"type": "object"},
        model="claude-sonnet-4-6",
    )

    assert result.success
    assert json.loads(result.final_text) == payload


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
    monkeypatch.setattr("qtea.llm.reasoning.model_for_agent", lambda _: None)
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
    disable_vertex_env(monkeypatch)
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
                FakeResponse,
                FakeTextBlock,
                FakeUsage,
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

    # Patch BOTH client classes so the test works regardless of which
    # branch the backend selector picks (disable_vertex_env above pins
    # the standard branch, but defense-in-depth).
    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
    monkeypatch.setattr("anthropic.AsyncAnthropicVertex", FakeClient)
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

async def test_cost_populated_via_pricing_table(tmp_path, monkeypatch):
    """The reasoning module fills `cost_usd` from the pricing table.

    Regression guard for the cost-zero bug: before the pricing module was
    wired in, every direct-SDK call produced `cost_usd: 0.0` regardless of
    tokens because the SDK doesn't return a cost field. The pricing
    module restores parity with the Agent SDK's `total_cost_usd`.
    """
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(
        monkeypatch,
        text="ok",
        usage=FakeUsage(input_tokens=10_000, output_tokens=5_000),
    )
    agent = _write_agent_file(tmp_path)

    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    # 10k input × $3/MTok + 5k output × $15/MTok = $0.03 + $0.075 = $0.105
    assert result.metrics.cost_usd == pytest.approx(0.105, abs=1e-6)

    # Audit JSON includes the basis label so consumers know the source.
    import json as _json
    metrics = _json.loads(result.metrics_path.read_text())
    assert metrics["cost_usd"] == pytest.approx(0.105, abs=1e-6)
    assert "cost_estimation_basis" in metrics
    assert "bosch" in metrics["cost_estimation_basis"].lower()


async def test_cost_zero_for_unknown_model(tmp_path, monkeypatch):
    """Unknown model id (no family match) → cost_usd stays 0.0, not crash."""
    disable_vertex_env(monkeypatch)
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    # Force model_for_agent + override model to a non-Anthropic-family id.
    monkeypatch.setattr("qtea.llm.reasoning.model_for_agent", lambda _: "gpt-9-future")
    result = await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
    )

    assert result.success
    assert result.metrics.cost_usd == 0.0


async def test_at_date_suffix_normalized_to_dash(tmp_path, monkeypatch):
    """Standard SDK path: @-form model IDs get converted to dash-form."""
    disable_vertex_env(monkeypatch)
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


# ---------------------------------------------------------------------------
# Anthropic SDK auth dispatch — ANTHROPIC_AUTH_TOKEN vs ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


async def test_auth_token_env_sent_as_bearer(tmp_path, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN must reach the SDK as auth_token= (Bearer header).

    Regression guard for the M1 bug where a model-farm bearer token set via
    ANTHROPIC_AUTH_TOKEN was passed to anthropic.AsyncAnthropic as api_key=,
    causing the SDK to send x-api-key and the model farm to reject with
    "invalid x-api-key" (401).
    """
    disable_vertex_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "farm-bearer-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    # Inspect the captured constructor kwargs from the FakeClient class.
    import anthropic
    fc = anthropic.AsyncAnthropic
    init_kwargs = getattr(fc, "last_init_kwargs", None)
    assert init_kwargs is not None
    assert init_kwargs.get("auth_token") == "farm-bearer-xyz"
    assert "api_key" not in init_kwargs
    assert getattr(fc, "last_init_class", None) == "AsyncAnthropic"


async def test_api_key_env_sent_as_x_api_key(tmp_path, monkeypatch):
    """ANTHROPIC_API_KEY (no AUTH_TOKEN) must reach the SDK as api_key=."""
    disable_vertex_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    import anthropic
    init_kwargs = getattr(anthropic.AsyncAnthropic, "last_init_kwargs", None)
    assert init_kwargs is not None
    assert init_kwargs.get("api_key") == "sk-ant-abc"
    assert "auth_token" not in init_kwargs


# ---------------------------------------------------------------------------
# Vertex backend selection (Bosch model farm / Google Cloud Vertex AI)
# ---------------------------------------------------------------------------


async def test_vertex_env_routes_to_async_anthropic_vertex(tmp_path, monkeypatch):
    """CLAUDE_CODE_USE_VERTEX=1 + ANTHROPIC_VERTEX_BASE_URL → AsyncAnthropicVertex.

    Regression guard for the M1 bug where Vertex-routed setups (like Bosch's
    model farm proxy) silently used anthropic.AsyncAnthropic and got 401
    "Invalid bearer token" because the auth model doesn't match.
    """
    enable_vertex_env(
        monkeypatch,
        base_url="https://aoai-farm.bosch-temp.com/api/google/v1",
    )
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "farm-token-abc")
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-haiku-4-5@20251001",
    )

    import anthropic
    init_class = getattr(anthropic.AsyncAnthropicVertex, "last_init_class", None)
    init_kwargs = getattr(anthropic.AsyncAnthropicVertex, "last_init_kwargs", None)
    assert init_class == "AsyncAnthropicVertex"
    assert init_kwargs is not None
    # access_token is the Vertex-equivalent of auth_token + reads from
    # ANTHROPIC_AUTH_TOKEN (same env var the Bosch CLI uses).
    assert init_kwargs.get("access_token") == "farm-token-abc"
    assert init_kwargs.get("base_url") == "https://aoai-farm.bosch-temp.com/api/google/v1"
    assert init_kwargs.get("project_id") == "_"


async def test_vertex_keeps_at_form_model_id(tmp_path, monkeypatch):
    """Vertex backend expects ``claude-haiku-4-5@20251001`` (@-form), unchanged."""
    enable_vertex_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-haiku-4-5@20251001",
    )

    # Vertex path must NOT convert @ to - (the standard SDK does, but Vertex
    # rejects the dash-form).
    assert captured["model"] == "claude-haiku-4-5@20251001"


async def test_anthropic_vertex_base_url_alone_triggers_vertex(tmp_path, monkeypatch):
    """ANTHROPIC_VERTEX_BASE_URL alone (no CLAUDE_CODE_USE_VERTEX) triggers Vertex.

    Either signal independently is sufficient to flip the backend.
    """
    disable_vertex_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://farm.example/api/google/v1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    install_fake_anthropic(monkeypatch, text="ok")
    agent = _write_agent_file(tmp_path)

    await call_reasoning_llm(
        agent_path=agent,
        workdir=tmp_path / "wd",
        user_prompt="x",
        model="claude-sonnet-4-6",
    )

    import anthropic
    assert getattr(anthropic.AsyncAnthropicVertex, "last_init_class", None) == "AsyncAnthropicVertex"
