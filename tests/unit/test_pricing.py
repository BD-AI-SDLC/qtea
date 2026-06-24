"""Tests for :mod:`qtea.pricing` — cost estimator + family fallback."""

from __future__ import annotations

import pytest

from qtea.metrics import AgentMetrics
from qtea.pricing import (
    PRICING_BASIS,
    estimate_cost,
    estimate_cost_from_metrics,
)

# ---------------------------------------------------------------------------
# estimate_cost — basic math
# ---------------------------------------------------------------------------


def test_estimate_cost_sonnet_input_only():
    """1M input tokens on Sonnet = $3.00."""
    assert estimate_cost("claude-sonnet-4-6", input_tokens=1_000_000) == 3.0


def test_estimate_cost_sonnet_output_only():
    """1M output tokens on Sonnet = $15.00."""
    assert estimate_cost("claude-sonnet-4-6", output_tokens=1_000_000) == 15.0


def test_estimate_cost_sonnet_realistic_call():
    """4815 input + 4439 output on Sonnet ≈ $0.081 (from the user's actual run)."""
    cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=4815,
        output_tokens=4439,
    )
    # 4815 * 3 / 1M + 4439 * 15 / 1M = 0.014445 + 0.066585 = 0.08103
    assert cost == pytest.approx(0.081030, abs=1e-6)


def test_estimate_cost_haiku_realistic_call():
    """1000 input + 500 output on Haiku = (1k×1 + 500×5) / 1M = $0.0035."""
    cost = estimate_cost(
        "claude-haiku-4-5",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost == pytest.approx(0.0035, abs=1e-6)


def test_estimate_cost_includes_cache_tokens():
    """Cache creation = 1.25× base input rate, cache read = 0.10×."""
    cost = estimate_cost(
        "claude-sonnet-4-6",
        cache_creation_input_tokens=1_000_000,  # = $3.75
        cache_read_input_tokens=1_000_000,      # = $0.30
    )
    assert cost == pytest.approx(4.05, abs=1e-6)


def test_estimate_cost_combines_all_token_types():
    """Sum across input + output + cache_create + cache_read."""
    cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,                 # = $3
        output_tokens=100_000,                  # = $1.50
        cache_creation_input_tokens=100_000,    # = $0.375
        cache_read_input_tokens=500_000,        # = $0.15
    )
    assert cost == pytest.approx(5.025, abs=1e-6)


# ---------------------------------------------------------------------------
# Model identification — exact + family fallback
# ---------------------------------------------------------------------------


def test_estimate_cost_exact_at_form_matches_dash_form():
    """`claude-haiku-4-5@20251001` and `-20251001` should produce identical cost."""
    at_form_cost = estimate_cost(
        "claude-haiku-4-5@20251001", input_tokens=100_000
    )
    dash_form_cost = estimate_cost(
        "claude-haiku-4-5-20251001", input_tokens=100_000
    )
    assert at_form_cost == dash_form_cost
    assert at_form_cost == pytest.approx(0.10, abs=1e-6)


def test_estimate_cost_opus_pricing():
    """Opus: 1M input = $15, 1M output = $75 (5× Sonnet)."""
    assert estimate_cost("claude-opus-4-8", input_tokens=1_000_000) == 15.0
    assert estimate_cost("claude-opus-4-8", output_tokens=1_000_000) == 75.0


def test_estimate_cost_unknown_model_falls_back_to_family():
    """A brand-new model id like 'claude-sonnet-4-7-foo' falls back to Sonnet rates."""
    cost = estimate_cost(
        "claude-sonnet-4-7-experimental", input_tokens=1_000_000
    )
    assert cost == 3.0  # Sonnet family


def test_estimate_cost_unknown_opus_variant_falls_back_to_opus_family():
    cost = estimate_cost(
        "claude-opus-9000-future", input_tokens=1_000_000
    )
    assert cost == 15.0  # Opus family


def test_estimate_cost_completely_unknown_returns_zero():
    """Truly unknown model (no family match) → 0.0 instead of guessing."""
    assert estimate_cost("gpt-5-turbo", input_tokens=1_000_000) == 0.0
    assert estimate_cost("gemini-3-pro", input_tokens=1_000_000) == 0.0


def test_estimate_cost_none_model_returns_zero():
    assert estimate_cost(None, input_tokens=1_000_000) == 0.0


def test_estimate_cost_empty_string_model_returns_zero():
    assert estimate_cost("", input_tokens=1_000_000) == 0.0


def test_estimate_cost_zero_tokens_returns_zero():
    assert estimate_cost("claude-sonnet-4-6") == 0.0


# ---------------------------------------------------------------------------
# estimate_cost_from_metrics — convenience wrapper
# ---------------------------------------------------------------------------


def test_estimate_cost_from_metrics_uses_all_fields():
    m = AgentMetrics(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=300,
    )
    cost = estimate_cost_from_metrics("claude-sonnet-4-6", m)
    # (1000*3 + 500*15 + 200*3.75 + 300*0.30) / 1M = 0.003 + 0.0075 + 0.00075 + 0.00009
    expected = (1000 * 3 + 500 * 15 + 200 * 3.75 + 300 * 0.30) / 1_000_000
    assert cost == pytest.approx(expected, abs=1e-6)


def test_estimate_cost_from_metrics_empty_metrics():
    cost = estimate_cost_from_metrics("claude-sonnet-4-6", AgentMetrics())
    assert cost == 0.0


# ---------------------------------------------------------------------------
# PRICING_BASIS sanity
# ---------------------------------------------------------------------------


def test_pricing_basis_is_versioned():
    """The basis string must be informative — used in audit JSON for traceability."""
    assert "anthropic" in PRICING_BASIS.lower()
    assert any(c.isdigit() for c in PRICING_BASIS)  # has a year/version marker
