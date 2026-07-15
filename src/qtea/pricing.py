"""Model pricing table + cost estimator for direct-SDK calls.

Restores the ``cost_usd`` field that the Agent SDK used to populate in
``ResultMessage.total_cost_usd`` — the direct Anthropic SDK doesn't ship
a pricing table, so we maintain one here and compute cost in
:func:`qtea.llm.reasoning.call_reasoning_llm` after each response.

**Accuracy caveat.** The rates below are the **published Claude list
prices** for the model family qtea runs (see :data:`PRICING_BASIS`).
Both input/output and cache (write + read) tokens are priced. Actual
billing may differ by transport (proxy markups,
committed-use / volume discounts on Vertex, batch discounts) and by the
>200K-input-token tier, which this flat table does not model — treat the
figure as an informational estimate, not authoritative billing.

Regardless of transport, the audit JSON labels the value
``cost_usd_estimated`` (not ``cost_usd``) and stamps the
``cost_estimation_basis`` field with :data:`PRICING_BASIS` so downstream
readers can't mistake the estimate for actual billing.
"""

from __future__ import annotations

from qtea.metrics import AgentMetrics

# Pricing basis (published Claude list prices, USD per 1M tokens).
# Update when new prices are published; keep the version marker in the
# string so audit JSON consumers can detect drift.
#
# Sources:
#   * Published Claude 4.x / Claude 5 model pricing sheets (accessed 2026-07-14)
#
# Cache pricing: the Anthropic API does not distinguish between 5-minute and
# 1-hour TTL cache writes in its usage response — both surface as
# cache_creation_input_tokens. The 5-minute write rate is used here so cost
# estimates lean conservative (1-hour writes are ~1.6× more expensive but
# amortise over more reads).
PRICING_BASIS = "claude-list-price-2026-07"


# Per-model rates: (input_per_MTok, output_per_MTok, cache_create_5m_per_MTok, cache_read_per_MTok)
#
# qtea's agent_models.yaml pins the pipeline to three models — Opus 4.8,
# Sonnet 5, and Haiku 4.5 (dated variant). Family aliases and adjacent
# versions (Opus 4.7/4.6, Sonnet 4.6) are included so the pricing survives a
# model bump or an SDK response header that echoes a slightly different ID.
#
# Adding a new model? Also update :func:`_model_family_fallback` if the new
# family isn't already covered (currently opus / sonnet / haiku), and the
# version guard if the new family bumps the version digit.
_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Sonnet 5 — standard rates (input / output / 5m-cache-write / cache-hit).
    "claude-sonnet-5":              ( 3.00, 15.00,  3.75, 0.30),
    "claude-opus-4-8":              ( 5.00, 25.00,  6.25, 0.50),
    "claude-opus-4-7":              ( 5.00, 25.00,  6.25, 0.50),
    "claude-opus-4-6":              ( 5.00, 25.00,  6.25, 0.50),
    "claude-sonnet-4-6":            ( 3.00, 15.00,  3.75, 0.30),
    "claude-haiku-4-5":             ( 1.00,  5.00,  1.25, 0.10),
    # Date-pinned Haiku (both @ and dash separators — agent_models.yaml uses
    # @-form; some SDK response paths normalise it to dash-form).
    "claude-haiku-4-5-20251001":    ( 1.00,  5.00,  1.25, 0.10),
    "claude-haiku-4-5@20251001":    ( 1.00,  5.00,  1.25, 0.10),
}


def _model_family_fallback(model: str) -> tuple[float, float, float, float] | None:
    """Best-effort family lookup for Claude 4.x / 5 IDs not in the explicit table.

    **Scoped to Claude 4.x and 5 on purpose.** The model ID must contain a
    ``-4-`` or ``-5`` segment (matching every ID in :data:`_MODEL_PRICING`)
    OR the fallback returns None → 0.0 estimated cost. This is defensive: a
    Claude 3 (or older) model that slipped through agent_models.yaml would be
    *silently mispriced* at current rates otherwise, and a zero-cost audit
    entry is much easier to notice + fix than a subtly-wrong number.

    Family fallbacks point at the newest priced ID per family (Opus 4.8,
    Sonnet 5, Haiku 4.5). When qtea adopts a new Claude family, extend the
    version guard below (e.g. add ``"-6"``) and add the family's rates to
    :data:`_MODEL_PRICING`.
    """
    lower = model.lower()
    if "-4-" not in lower and "-5" not in lower:
        return None
    if "opus" in lower:
        return _MODEL_PRICING["claude-opus-4-8"]
    if "sonnet" in lower:
        return _MODEL_PRICING["claude-sonnet-5"]
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
    """Estimate the USD cost of a Claude model call from token counts.

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
