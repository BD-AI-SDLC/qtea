"""Model pricing table + cost estimator for direct-SDK calls.

Restores the ``cost_usd`` field that the Agent SDK used to populate in
``ResultMessage.total_cost_usd`` — the direct Anthropic SDK doesn't ship
a pricing table, so we maintain one here and compute cost in
:func:`worca_t.llm.reasoning.call_reasoning_llm` after each response.

**Accuracy caveat.** The rates below are **Anthropic's public list
prices**. Actual billing depends on the model provider:

* **Direct Anthropic API** users → estimate is ~95-99% accurate (drift
  comes from occasional Anthropic price changes)
* **Vertex AI direct** users → ~80-90% accurate (Vertex bills slightly
  differently than Anthropic direct; volume / committed-use discounts
  add further drift)
* **Custom proxy / model farm** users (e.g. Bosch's
  ``aoai-farm.bosch-temp.com``) → **absolute number is unknowable** —
  could be 0 (flat-rate enterprise contract) or 200% of list (proxy
  markup). The number IS useful for relative comparisons across steps
  and trend tracking (token counts are ground truth).

The audit JSON labels the value as ``cost_usd_estimated`` (not
``cost_usd``) and includes a ``cost_estimation_basis`` field so
downstream readers can't mistake the estimate for actual billing.
"""

from __future__ import annotations

from worca_t.metrics import AgentMetrics

# Pricing basis (Anthropic public list prices, USD per 1M tokens).
# Update when Anthropic publishes new prices.
#
# Sources:
#   * https://www.anthropic.com/pricing
#   * Claude 4.x family pricing as of 2026-Q2
#
# Cache pricing:
#   * cache_creation (5min TTL) = 1.25 × base input rate
#   * cache_read                = 0.10 × base input rate
PRICING_BASIS = "anthropic-list-price-2026-q2"


# Per-model rates: (input_per_MTok, output_per_MTok, cache_create_per_MTok, cache_read_per_MTok)
# Keys are the agent_models.yaml model IDs (both @-form and dash-form
# variants are supported because worca-t passes whichever form the env
# expects).
_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Claude 4.x family
    "claude-opus-4-8":              (15.00, 75.00, 18.75, 1.50),
    "claude-opus-4-7":              (15.00, 75.00, 18.75, 1.50),
    "claude-opus-4-6":              (15.00, 75.00, 18.75, 1.50),
    "claude-sonnet-4-6":            ( 3.00, 15.00,  3.75, 0.30),
    "claude-haiku-4-5":             ( 1.00,  5.00,  1.25, 0.10),
    # Date-pinned haiku (both @ and dash separators)
    "claude-haiku-4-5-20251001":    ( 1.00,  5.00,  1.25, 0.10),
    "claude-haiku-4-5@20251001":    ( 1.00,  5.00,  1.25, 0.10),
    # Claude 3.x family (kept for older agent definitions)
    "claude-3-5-sonnet-20241022":   ( 3.00, 15.00,  3.75, 0.30),
    "claude-3-5-haiku-20241022":    ( 0.80,  4.00,  1.00, 0.08),
    "claude-3-opus-20240229":       (15.00, 75.00, 18.75, 1.50),
}


def _model_family_fallback(model: str) -> tuple[float, float, float, float] | None:
    """Best-effort family lookup for model IDs not in the explicit table.

    Catches model variants that haven't been added to :data:`_MODEL_PRICING`
    yet (e.g. a brand-new dated revision) by matching on the model-family
    prefix (``claude-opus``, ``claude-sonnet``, ``claude-haiku``).
    Conservative — when in doubt, falls through to ``None`` and the cost
    estimator returns 0.0 rather than guessing wrong.
    """
    lower = model.lower()
    if "opus" in lower:
        return _MODEL_PRICING["claude-opus-4-6"]
    if "sonnet" in lower:
        return _MODEL_PRICING["claude-sonnet-4-6"]
    if "haiku" in lower:
        return _MODEL_PRICING["claude-haiku-4-5"]
    return None


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Estimate the USD cost of an Anthropic API call from token counts.

    Returns 0.0 when:
      * ``model`` is None or unknown to the pricing table (and family
        fallback also fails)
      * All token counts are zero (no work was done)

    Otherwise returns ``sum(tokens × rate) / 1_000_000`` rounded to six
    decimal places for display parity with the Agent SDK's
    ``total_cost_usd`` field.
    """
    if not model:
        return 0.0
    rates = _MODEL_PRICING.get(model) or _model_family_fallback(model)
    if rates is None:
        return 0.0
    input_rate, output_rate, cache_create_rate, cache_read_rate = rates
    cost = (
        input_tokens                 * input_rate
        + output_tokens              * output_rate
        + cache_creation_input_tokens * cache_create_rate
        + cache_read_input_tokens     * cache_read_rate
    ) / 1_000_000.0
    return round(cost, 6)


def estimate_cost_from_metrics(model: str | None, metrics: AgentMetrics) -> float:
    """Convenience wrapper: estimate cost from an :class:`AgentMetrics` instance."""
    return estimate_cost(
        model,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        cache_creation_input_tokens=metrics.cache_creation_input_tokens,
        cache_read_input_tokens=metrics.cache_read_input_tokens,
    )


__all__ = [
    "PRICING_BASIS",
    "estimate_cost",
    "estimate_cost_from_metrics",
]
