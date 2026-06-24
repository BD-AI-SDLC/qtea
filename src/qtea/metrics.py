"""Per-step token / cost accounting.

Every agent call funnels through ``claude_runner.run_agent``. When a step is
executing, ``Step._attempt`` opens a fresh ``StepMetricsAccumulator`` and sets
``CURRENT_STEP_METRICS`` to point at it. The runner extracts ``usage`` and
``total_cost_usd`` from each SDK ``ResultMessage`` and pushes them into the
active accumulator. The step then copies the totals onto its ``StepRecord``.

This means we never have to thread metrics through step return types or touch
the 11 step implementations -- the accumulator is invisible to them.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class AgentMetrics:
    """Aggregated token + cost figures for a single agent invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0


@dataclass
class StepMetricsAccumulator:
    """Sums ``AgentMetrics`` across every agent call made during a step."""

    agent_calls: int = 0
    totals: AgentMetrics = field(default_factory=AgentMetrics)

    def record(self, m: AgentMetrics) -> None:
        self.agent_calls += 1
        self.totals.input_tokens += m.input_tokens
        self.totals.output_tokens += m.output_tokens
        self.totals.cache_creation_input_tokens += m.cache_creation_input_tokens
        self.totals.cache_read_input_tokens += m.cache_read_input_tokens
        self.totals.cost_usd += m.cost_usd
        self.totals.num_turns += m.num_turns


# Active accumulator for the running step, or None when no step owns the
# current context. asyncio.Task copies the Context at task-creation time, so
# subtasks spawned by a step still see the same accumulator object and their
# mutations propagate back -- mutable shared state is the point here.
CURRENT_STEP_METRICS: ContextVar[StepMetricsAccumulator | None] = ContextVar(
    "qtea_step_metrics", default=None
)


def extract_agent_metrics(
    usage: object | None, total_cost_usd: float | None
) -> AgentMetrics:
    """Pull token/cost figures out of an SDK ``ResultMessage``.

    ``usage`` may be a dict (already serialized) or an SDK dataclass-like
    object. ``total_cost_usd`` may be ``None`` for cache-only responses.
    """
    out = AgentMetrics()
    out.cost_usd = float(total_cost_usd) if total_cost_usd is not None else 0.0

    if usage is None:
        return out

    def _get(name: str) -> int:
        v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    out.input_tokens = _get("input_tokens")
    out.output_tokens = _get("output_tokens")
    out.cache_creation_input_tokens = _get("cache_creation_input_tokens")
    out.cache_read_input_tokens = _get("cache_read_input_tokens")
    return out


def format_tokens(n: int) -> str:
    """Render a token count compactly: 1234 -> '1.2k', 1_500_000 -> '1.5M'."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def format_cost(usd: float) -> str:
    """Render USD to 2 decimal places; 4 for sub-cent amounts to avoid $0.00."""
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
