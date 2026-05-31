"""Tests for the per-step token + cost accumulator."""

from __future__ import annotations

import pytest

from worca_t.metrics import (
    CURRENT_STEP_METRICS,
    AgentMetrics,
    StepMetricsAccumulator,
    extract_agent_metrics,
    format_cost,
    format_tokens,
)


def test_accumulator_sums_multiple_records():
    acc = StepMetricsAccumulator()
    acc.record(AgentMetrics(input_tokens=100, output_tokens=50, cost_usd=0.01, num_turns=2))
    acc.record(AgentMetrics(input_tokens=200, output_tokens=80, cost_usd=0.03, num_turns=3))
    acc.record(AgentMetrics(cache_creation_input_tokens=500, cache_read_input_tokens=1200))

    assert acc.agent_calls == 3
    assert acc.totals.input_tokens == 300
    assert acc.totals.output_tokens == 130
    assert acc.totals.cache_creation_input_tokens == 500
    assert acc.totals.cache_read_input_tokens == 1200
    assert acc.totals.cost_usd == pytest.approx(0.04)
    assert acc.totals.num_turns == 5


def test_accumulator_starts_empty():
    acc = StepMetricsAccumulator()
    assert acc.agent_calls == 0
    assert acc.totals.input_tokens == 0
    assert acc.totals.cost_usd == 0.0


def test_extract_agent_metrics_from_dict_usage():
    m = extract_agent_metrics(
        {
            "input_tokens": 1000,
            "output_tokens": 250,
            "cache_creation_input_tokens": 5000,
            "cache_read_input_tokens": 12000,
        },
        total_cost_usd=0.0421,
    )
    assert m.input_tokens == 1000
    assert m.output_tokens == 250
    assert m.cache_creation_input_tokens == 5000
    assert m.cache_read_input_tokens == 12000
    assert m.cost_usd == pytest.approx(0.0421)


def test_extract_agent_metrics_tolerates_none():
    m = extract_agent_metrics(None, total_cost_usd=None)
    assert m.input_tokens == 0
    assert m.output_tokens == 0
    assert m.cost_usd == 0.0


def test_extract_agent_metrics_tolerates_missing_keys():
    m = extract_agent_metrics({"input_tokens": 42}, total_cost_usd=0.001)
    assert m.input_tokens == 42
    assert m.output_tokens == 0
    assert m.cost_usd == pytest.approx(0.001)


def test_extract_agent_metrics_from_object_usage():
    class FakeUsage:
        input_tokens = 7
        output_tokens = 11
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    m = extract_agent_metrics(FakeUsage(), total_cost_usd=0.5)
    assert m.input_tokens == 7
    assert m.output_tokens == 11
    assert m.cost_usd == 0.5


def test_extract_agent_metrics_coerces_garbage_values():
    m = extract_agent_metrics({"input_tokens": "not-an-int"}, total_cost_usd=None)
    assert m.input_tokens == 0


def test_contextvar_default_is_none():
    # Outside a step, no accumulator is active.
    assert CURRENT_STEP_METRICS.get() is None


def test_contextvar_set_and_reset():
    acc = StepMetricsAccumulator()
    token = CURRENT_STEP_METRICS.set(acc)
    try:
        assert CURRENT_STEP_METRICS.get() is acc
    finally:
        CURRENT_STEP_METRICS.reset(token)
    assert CURRENT_STEP_METRICS.get() is None


def test_format_tokens_boundaries():
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"
    assert format_tokens(1_000) == "1.0k"
    assert format_tokens(12_345) == "12.3k"
    assert format_tokens(999_999) == "1000.0k"
    assert format_tokens(1_500_000) == "1.50M"


def test_format_cost_precision():
    assert format_cost(0.0) == "$0.0000"
    assert format_cost(0.0042) == "$0.0042"
    assert format_cost(0.123) == "$0.123"
    assert format_cost(4.21) == "$4.21"
    # Sub-cent values keep four decimals so an agent call doesn't show as $0.00.
    assert format_cost(0.0001) == "$0.0001"
