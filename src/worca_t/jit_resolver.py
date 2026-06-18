"""JIT locator resolver — single-shot LLM call given AOM snapshot + intent.

Invoked as a subprocess by the vendored pytest runtime plugin
(`_resources/runtime/worca_t_runtime.py.tpl`). The plugin shells out via
``worca-t resolve --intent ... --snapshot ... --constant ... --cache ...``
on every cache miss; this module is the implementation behind that
subcommand.

Why direct Anthropic SDK instead of ``claude_runner.run_agent``:
this is a one-turn Q&A — no tool use, no MCP, no multi-step reasoning.
The agent-runner overhead (subprocess CLI, transcript persistence,
agent.md loading) costs 3-5x more per call. A direct SDK call with
temperature=0 + prefilled JSON output is both faster and more
deterministic for the same input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Locator-priority chain. Canonical authority for the JIT path; downstream
# tooling (Step 7 codegen rules, Step 8 self-heal scope) honours the same
# order. Keep in sync with CLAUDE.md § Locator priority.
_PRIORITY = ("id", "data-testid", "role", "label", "text", "placeholder", "css")

# Default model. `claude-sonnet-4-6` balances quality and speed for selector
# inference; users who want to trade quality for speed can override via env.
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Maximum retries for transient API failures. Past this the resolver returns
# `unresolvable` and lets the plugin raise `pytest.fail` with a clear reason.
_MAX_API_RETRIES = 2
_API_RETRY_BACKOFF_S = (1.0, 3.0)  # one entry per retry attempt

# XPath detection — reject any LLM-proposed selector that downgrades to
# XPath. Mirrors the Step 9 self-heal quality gate in `s09_execute.py`.
_XPATH_PATTERNS = ("xpath=", "By.XPATH", "by_xpath")


@dataclass(frozen=True)
class ResolutionResult:
    """Return shape of :func:`resolve_one`. Serialised to stdout JSON for the
    pytest plugin to consume.

    ``candidates`` carries the LLM's ranked bundle (primary + optional
    fallback). The top-level ``selector``/``strategy``/``confidence`` mirror
    ``candidates[0]`` so legacy consumers that don't know about the bundle
    keep working. For non-agent sources (cached / unresolvable) ``candidates``
    is ``None``.

    Cost-tracking fields (``input_tokens`` / ``output_tokens`` / ``model`` /
    ``duration_ms``) are populated for tier 4 (LLM) results; for ``cached``
    they're zero / null. The runtime plugin reads these and appends one
    line per resolution to ``<cache_dir>/resolver-spend.jsonl`` so Step 8
    can aggregate them into a ``resolver_spend`` summary on
    ``run-results.json``.
    """

    selector: str | None
    strategy: str | None
    confidence: float | None
    source: str  # "cached" | "agent" | "unresolvable"
    intent: str
    constant_name: str
    page_url: str | None = None
    snapshot_hash: str | None = None
    reason: str | None = None
    resolved_at: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    duration_ms: int | None = None
    candidates: tuple[dict[str, Any], ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "source": self.source,
            "intent": self.intent,
            "constant_name": self.constant_name,
            "page_url": self.page_url,
            "snapshot_hash": self.snapshot_hash,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "candidates": list(self.candidates) if self.candidates else None,
        }


def cache_key(test_file: str | None, constant_name: str, intent: str) -> str:
    """Stable cache key. Survives whitespace changes in intent (normalised)."""
    norm_intent = re.sub(r"\s+", " ", intent or "").strip().lower()
    payload = f"{test_file or ''}::{constant_name}::{norm_intent}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_xpath(selector: str) -> bool:
    """Reject XPath replacements — same rule the step-8 patcher enforces."""
    s = (selector or "").strip()
    if s.startswith("//"):
        return True
    return any(p in s for p in _XPATH_PATTERNS)


def normalise_strategy(value: str | None) -> str | None:
    """Coerce strategy string to one of the allowed enum values."""
    if not value:
        return None
    lo = value.strip().lower()
    if lo in _PRIORITY:
        return lo
    return None


def snapshot_hash(snapshot_text: str) -> str:
    """sha256 prefix for cache-entry provenance."""
    return hashlib.sha256(snapshot_text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def read_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    """Return cache as {key: entry_dict}. Missing/invalid file → empty dict."""
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for e in entries:
        if isinstance(e, dict) and e.get("key"):
            out[e["key"]] = e
    return out


def write_cache(cache_path: Path, entries: dict[str, dict[str, Any]], *, run_id: str | None = None) -> None:
    """Atomically write the cache (tmp + rename, like checkpoints.py)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "produced_at": datetime.now(UTC).isoformat(),
        "entries": list(entries.values()),
    }
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)


def _build_prompt(
    intent: str, snapshot_text: str, constant_name: str,
    *, dev_pool: list[dict] | None = None, page_url: str | None = None,
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the resolver LLM call.

    Keep the system prompt small and surgical — the model needs to:
      1. Pick a selector that exists in the snapshot (no inventing).
      2. Honour the priority chain.
      3. Refuse XPath.
      4. Express confidence honestly (null when uncertain).

    When ``dev_pool`` is supplied (frontend-dev-curated selector pool that
    failed Tier 1b fuzzy match), it's appended to the user prompt as a
    high-priority prior — the model should prefer a pool entry whose
    intent matches and whose selector resolves in the snapshot, before
    generating a fresh selector.
    """
    pool_rule = (
        "  7. If a CANDIDATE selector from the dev team is present and "
        "matches the intent AND resolves to an element visible in the AOM "
        "snapshot, you MUST prefer it as the primary. Set `confidence` "
        "accordingly and put the matched selector verbatim in `selector`. "
        "Fall back to AOM-derived selectors only when no candidate matches.\n"
        if dev_pool else ""
    )
    system = (
        "You resolve a UI locator from a Playwright accessibility snapshot. "
        "Read the snapshot, find the element matching the locator's intent, "
        "and return a ranked bundle of up to two selector candidates so the "
        "runtime can fall back transparently if the primary mutates.\n"
        "Rules:\n"
        "  1. Output ONLY a JSON object. Your first character MUST be `{` "
        "and your last character MUST be `}`. No prose before or after, "
        "no markdown fences, no commentary.\n"
        "  2. Every selector MUST be findable in the provided snapshot. NEVER "
        "invent attributes or roles not present.\n"
        "  3. Locator priority (use the highest-applicable for the primary): "
        "id > data-testid > role > label > text > placeholder > scoped css. "
        "NEVER XPath.\n"
        "  4. The fallback (if present) MUST use a different `strategy` family "
        "than the primary (e.g. if primary strategy is `role`, fallback should "
        "prefer `text`, `label`, or `data-testid`). Omit the fallback when no "
        "defensible alternate exists in the snapshot.\n"
        "  5. If no element clearly matches the intent at confidence >= 0.6, "
        "return an empty `candidates` array and set the top-level `reason` "
        "explaining what you looked for and why it isn't there.\n"
        "  6. `strategy` must be one of: id, data-testid, role, label, text, "
        "placeholder, css, or null.\n"
        + pool_rule +
        "Output shape: "
        "{\"candidates\": ["
        "{\"selector\": <string>, \"strategy\": <string|null>, "
        "\"confidence\": <number 0-1 or null>, \"reason\": <string|null>}, "
        "...up to 2 entries], "
        "\"reason\": <string|null>}"
    )
    user_parts = [
        f"Locator constant: `{constant_name}`",
        f"Intent: {intent}",
    ]
    if page_url:
        user_parts.append(f"Current page URL: {page_url}")
    if dev_pool:
        lines = ["Candidate selectors from the dev team (prefer when they match the intent and resolve in the snapshot):"]
        for e in dev_pool:
            sel = e.get("selector", "")
            ent_intent = e.get("intent") or ""
            ent_url = e.get("page_url") or ""
            line = f"- selector={sel!r}  intent={ent_intent!r}"
            if ent_url:
                line += f"  page_url={ent_url!r}"
            lines.append(line)
        user_parts.append("\n".join(lines))
    user_parts.append(f"AOM snapshot:\n```json\n{snapshot_text}\n```")
    user = "\n\n".join(user_parts)
    return system, user


def _call_anthropic(system: str, user: str, *, model: str) -> tuple[str, dict[str, int | None]]:
    """Single Anthropic API call with prefilled JSON output.

    Returns ``(completion_text, usage_dict)`` where ``usage_dict`` carries
    ``{"input_tokens", "output_tokens"}`` for telemetry (Phase 6). Raises
    on transport / SDK errors.
    """
    # Lazy import so non-resolver callers don't pay the import cost.
    import anthropic  # type: ignore[import-untyped]

    # Backend selection: Vertex (or Vertex-mimicking proxy like Bosch's
    # model farm) when CLAUDE_CODE_USE_VERTEX=1 or ANTHROPIC_VERTEX_BASE_URL
    # is set; standard Anthropic otherwise. Mirrors llm/reasoning.py.
    from worca_t.config import (
        anthropic_auth_kwargs,
        anthropic_vertex_kwargs,
        use_vertex_backend,
    )

    if use_vertex_backend():
        client = anthropic.AnthropicVertex(**anthropic_vertex_kwargs())
        # Vertex expects the @-form (e.g. claude-haiku-4-5@20251001).
        send_model = model
    else:
        client = anthropic.Anthropic(**anthropic_auth_kwargs())
        # Standard SDK expects the dash-form; convert @ to -.
        send_model = model.replace("@", "-") if "@" in model else model
    # No assistant-message prefill: Vertex AI (and the Bosch BMF Vertex
    # relay) rejects requests whose final message is from `assistant` with
    # `'This model does not support assistant message prefill. The
    # conversation must end with a user message.'`. The system prompt's
    # "Output ONLY a JSON object" rule plus temperature=0 produces a
    # leading `{` reliably; `_parse_response` is tolerant of any leading
    # whitespace or stray prose by scanning to the first balanced object.
    response = client.messages.create(
        model=send_model,
        max_tokens=512,
        temperature=0.0,
        system=system,
        messages=[
            {"role": "user", "content": user},
        ],
    )
    body_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            body_parts.append(block.text)
    body = "".join(body_parts)
    usage_obj = getattr(response, "usage", None)
    usage: dict[str, int | None] = {
        "input_tokens": getattr(usage_obj, "input_tokens", None),
        "output_tokens": getattr(usage_obj, "output_tokens", None),
    }
    return (body if body else "{}"), usage


def _parse_response(text: str) -> dict[str, Any]:
    """Tolerant JSON parser. Locates the first balanced `{...}` object and
    parses just that. Tolerates leading whitespace, markdown fences, or a
    stray prose token preceding the JSON — important now that we no longer
    use assistant-message prefill (Vertex AI rejects that pattern, see
    `_call_anthropic`)."""
    s = text.strip()
    start_idx = s.find("{")
    if start_idx == -1:
        raise ValueError(f"resolver: no JSON object found in response: {s[:200]!r}")
    depth = 0
    end_idx = -1
    for i in range(start_idx, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    if end_idx == -1:
        raise ValueError(f"resolver: unbalanced JSON in response: {s[:200]!r}")
    return json.loads(s[start_idx:end_idx])


_MAX_CANDIDATES: int = 2


def _normalise_candidates(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a ranked candidate list from the LLM response, dropping
    malformed entries rather than failing the whole call.

    Accepts both the new shape (``{"candidates": [...]}``) and the legacy
    flat shape (``{"selector": ..., "strategy": ..., "confidence": ...}``)
    so a model that regresses to the older instruction format doesn't
    break the runtime. XPath candidates are dropped; the priority-chain
    rule is enforced per-entry by :func:`is_xpath` further down.
    """
    raw = parsed.get("candidates")
    if not isinstance(raw, list):
        # Legacy flat shape — wrap into a single-candidate bundle if it
        # carries a usable, non-XPath selector. XPath selectors fall through
        # to the empty-list path so resolve_one can surface the priority-gate
        # rejection reason explicitly.
        flat_sel = parsed.get("selector")
        if isinstance(flat_sel, str) and flat_sel.strip() and not is_xpath(flat_sel):
            return [{
                "selector": flat_sel.strip(),
                "strategy": normalise_strategy(parsed.get("strategy")),
                "confidence": (
                    float(parsed["confidence"])
                    if isinstance(parsed.get("confidence"), (int, float))
                    else None
                ),
                "reason": parsed.get("reason") if isinstance(parsed.get("reason"), str) else None,
            }]
        return []

    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sel = entry.get("selector")
        if not isinstance(sel, str) or not sel.strip():
            continue
        sel = sel.strip()
        if is_xpath(sel):
            continue
        conf = entry.get("confidence")
        out.append({
            "selector": sel,
            "strategy": normalise_strategy(entry.get("strategy")),
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "reason": entry.get("reason") if isinstance(entry.get("reason"), str) else None,
        })
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def resolve_one(
    *,
    intent: str,
    snapshot_text: str,
    constant_name: str,
    test_file: str | None = None,
    page_url: str | None = None,
    cache_dir: Path | None = None,
    model: str | None = None,
    run_id: str | None = None,
    dev_pool: list[dict] | None = None,
) -> ResolutionResult:
    """Resolve one TBD locator. Checks cache, then LLM, with bounded retry.

    Returns a :class:`ResolutionResult`. Cache hits skip the LLM call entirely.
    On API failure after ``_MAX_API_RETRIES`` attempts, returns a result with
    ``source="unresolvable"`` instead of raising — the pytest plugin uses the
    ``source`` field to decide whether to call ``pytest.fail``.
    """
    key = cache_key(test_file, constant_name, intent)
    snap_hash = snapshot_hash(snapshot_text)
    cache_path = (cache_dir / "locator-cache.json") if cache_dir else None

    # Cache lookup.
    cache: dict[str, dict[str, Any]] = read_cache(cache_path) if cache_path else {}
    cached = cache.get(key)
    if cached:
        cached_candidates = cached.get("candidates")
        return ResolutionResult(
            selector=cached.get("selector"),
            strategy=cached.get("strategy"),
            confidence=cached.get("confidence"),
            source="cached",
            intent=intent,
            constant_name=constant_name,
            page_url=cached.get("page_url"),
            snapshot_hash=cached.get("snapshot_hash"),
            reason=cached.get("reason"),
            resolved_at=cached.get("resolved_at"),
            candidates=(
                tuple(cached_candidates)
                if isinstance(cached_candidates, list) and cached_candidates
                else None
            ),
        )

    chosen_model = model or os.environ.get("WORCA_T_RESOLVER_MODEL") or _DEFAULT_MODEL
    system, user = _build_prompt(
        intent, snapshot_text, constant_name,
        dev_pool=dev_pool, page_url=page_url,
    )

    last_error: str | None = None
    usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None}
    t_call_start = time.monotonic()
    for attempt in range(_MAX_API_RETRIES + 1):
        try:
            raw, usage = _call_anthropic(system, user, model=chosen_model)
            parsed = _parse_response(raw)
            break
        except Exception as e:  # noqa: BLE001 - SDK exceptions are not pinned to one type
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= _MAX_API_RETRIES:
                return ResolutionResult(
                    selector=None, strategy=None, confidence=None,
                    source="unresolvable", intent=intent,
                    constant_name=constant_name, page_url=page_url,
                    snapshot_hash=snap_hash,
                    reason=f"resolver API failed after {_MAX_API_RETRIES + 1} attempts: {last_error}",
                    resolved_at=datetime.now(UTC).isoformat(),
                    model=chosen_model,
                    duration_ms=int((time.monotonic() - t_call_start) * 1000),
                )
            time.sleep(_API_RETRY_BACKOFF_S[min(attempt, len(_API_RETRY_BACKOFF_S) - 1)])
    else:  # pragma: no cover - loop always returns or breaks
        raise RuntimeError("unreachable")
    duration_ms = int((time.monotonic() - t_call_start) * 1000)

    candidates = _normalise_candidates(parsed)
    top_level_reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else None

    # Surface the XPath-rejection reason explicitly when the model regressed
    # to flat shape with an XPath selector (the candidates list will be empty
    # because _normalise_candidates drops XPath entries).
    flat_sel = parsed.get("selector")
    if not candidates and isinstance(flat_sel, str) and is_xpath(flat_sel):
        return ResolutionResult(
            selector=None, strategy=None, confidence=None,
            source="unresolvable", intent=intent,
            constant_name=constant_name, page_url=page_url,
            snapshot_hash=snap_hash,
            reason=f"resolver returned XPath ({flat_sel!r}); rejected per locator-priority gate",
            resolved_at=datetime.now(UTC).isoformat(),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model=chosen_model,
            duration_ms=duration_ms,
        )

    if not candidates:
        result = ResolutionResult(
            selector=None, strategy=None, confidence=None,
            source="unresolvable", intent=intent,
            constant_name=constant_name, page_url=page_url,
            snapshot_hash=snap_hash,
            reason=top_level_reason or "resolver did not return a selector",
            resolved_at=datetime.now(UTC).isoformat(),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model=chosen_model,
            duration_ms=duration_ms,
        )
    else:
        primary = candidates[0]
        result = ResolutionResult(
            selector=primary["selector"],
            strategy=primary.get("strategy"),
            confidence=primary.get("confidence"),
            source="agent",
            intent=intent,
            constant_name=constant_name,
            page_url=page_url,
            snapshot_hash=snap_hash,
            reason=primary.get("reason") or top_level_reason,
            resolved_at=datetime.now(UTC).isoformat(),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model=chosen_model,
            duration_ms=duration_ms,
            candidates=tuple(candidates),
        )

    # Persist.
    if cache_path is not None:
        entry = {
            "key": key,
            "test_file": test_file,
            "constant_name": constant_name,
            "intent": intent,
            "selector": result.selector,
            "strategy": result.strategy,
            "confidence": result.confidence,
            "source": result.source,
            "page_url": result.page_url,
            "snapshot_hash": result.snapshot_hash,
            "resolved_at": result.resolved_at,
        }
        if result.candidates:
            entry["candidates"] = list(result.candidates)
        cache[key] = entry
        write_cache(cache_path, cache, run_id=run_id or os.environ.get("WORCA_T_RUN_ID"))

    return result
