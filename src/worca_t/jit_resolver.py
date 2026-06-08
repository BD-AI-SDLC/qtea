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

# Locator-priority chain. Mirrors `s08_locator_resolution._PRIORITY` exactly
# so JIT resolutions honour the same gate downstream tooling enforces.
_PRIORITY = ("id", "data-testid", "role", "label", "text", "placeholder", "css")

# Default model. `claude-sonnet-4-6` balances quality and speed for selector
# inference; users who want to trade quality for speed can override via env.
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Maximum retries for transient API failures. Past this the resolver returns
# `unresolvable` and lets the plugin raise `pytest.fail` with a clear reason.
_MAX_API_RETRIES = 2
_API_RETRY_BACKOFF_S = (1.0, 3.0)  # one entry per retry attempt

# XPath detection — mirrors `s08_locator_resolution._is_xpath_replacement`.
_XPATH_PATTERNS = ("xpath=", "By.XPATH", "by_xpath")


@dataclass(frozen=True)
class ResolutionResult:
    """Return shape of :func:`resolve_one`. Serialised to stdout JSON for the
    pytest plugin to consume."""

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


def _build_prompt(intent: str, snapshot_text: str, constant_name: str) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the resolver LLM call.

    Keep the system prompt small and surgical — the model needs to:
      1. Pick a selector that exists in the snapshot (no inventing).
      2. Honour the priority chain.
      3. Refuse XPath.
      4. Express confidence honestly (null when uncertain).
    """
    system = (
        "You resolve a single UI locator from a Playwright accessibility "
        "snapshot. Read the snapshot, find the element matching the locator's "
        "intent, and return ONE JSON object describing the selector. "
        "Rules:\n"
        "  1. Output ONLY a JSON object. No prose, no markdown fences.\n"
        "  2. The selector MUST be findable in the provided snapshot. NEVER "
        "invent attributes or roles not present.\n"
        "  3. Locator priority (use the highest-applicable): id > data-testid "
        "> role > label > text > placeholder > scoped css. NEVER XPath.\n"
        "  4. If no element clearly matches the intent at confidence >= 0.6, "
        "set selector=null and confidence=null with a `reason` explaining "
        "what you looked for and why it isn't there.\n"
        "  5. `strategy` must be one of: id, data-testid, role, label, text, "
        "placeholder, css, or null.\n"
        "Output shape: "
        "{\"selector\": <string|null>, \"strategy\": <string|null>, "
        "\"confidence\": <number 0-1 or null>, \"reason\": <string|null>}"
    )
    user = (
        f"Locator constant: `{constant_name}`\n"
        f"Intent: {intent}\n\n"
        f"AOM snapshot:\n```json\n{snapshot_text}\n```"
    )
    return system, user


def _call_anthropic(system: str, user: str, *, model: str) -> str:
    """Single Anthropic API call with prefilled JSON output. Returns the
    completion text. Raises on transport / SDK errors."""
    # Lazy import so non-resolver callers don't pay the import cost.
    import anthropic  # type: ignore[import-untyped]

    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model=model,
        max_tokens=512,
        temperature=0.0,
        system=system,
        messages=[
            {"role": "user", "content": user},
            {"role": "assistant", "content": "{"},  # prefill JSON open-brace
        ],
    )
    # Anthropic SDK returns a list of content blocks; we take the text of
    # the first one and prepend the prefilled `{`.
    body_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            body_parts.append(block.text)
    body = "".join(body_parts)
    return "{" + body if body else "{}"


def _parse_response(text: str) -> dict[str, Any]:
    """Tolerant JSON parser. The prefill guarantees we open with `{`; the
    model usually closes properly. Strip trailing prose if any."""
    s = text.strip()
    # Trim anything after the last balanced brace.
    depth = 0
    end_idx = -1
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    if end_idx == -1:
        raise ValueError(f"resolver: unbalanced JSON in response: {s[:200]!r}")
    return json.loads(s[:end_idx])


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
        )

    chosen_model = model or os.environ.get("WORCA_T_RESOLVER_MODEL") or _DEFAULT_MODEL
    system, user = _build_prompt(intent, snapshot_text, constant_name)

    last_error: str | None = None
    for attempt in range(_MAX_API_RETRIES + 1):
        try:
            raw = _call_anthropic(system, user, model=chosen_model)
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
                )
            time.sleep(_API_RETRY_BACKOFF_S[min(attempt, len(_API_RETRY_BACKOFF_S) - 1)])
    else:  # pragma: no cover - loop always returns or breaks
        raise RuntimeError("unreachable")

    selector = parsed.get("selector")
    strategy = normalise_strategy(parsed.get("strategy"))
    confidence = parsed.get("confidence")
    reason = parsed.get("reason")

    # Sanitise.
    if isinstance(selector, str) and is_xpath(selector):
        return ResolutionResult(
            selector=None, strategy=None, confidence=None,
            source="unresolvable", intent=intent,
            constant_name=constant_name, page_url=page_url,
            snapshot_hash=snap_hash,
            reason=f"resolver returned XPath ({selector!r}); rejected per locator-priority gate",
            resolved_at=datetime.now(UTC).isoformat(),
        )

    # Confidence-threshold check.
    if not isinstance(selector, str) or not selector.strip():
        result = ResolutionResult(
            selector=None, strategy=None, confidence=None,
            source="unresolvable", intent=intent,
            constant_name=constant_name, page_url=page_url,
            snapshot_hash=snap_hash,
            reason=reason or "resolver did not return a selector",
            resolved_at=datetime.now(UTC).isoformat(),
        )
    else:
        result = ResolutionResult(
            selector=selector.strip(),
            strategy=strategy,
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            source="agent",
            intent=intent,
            constant_name=constant_name,
            page_url=page_url,
            snapshot_hash=snap_hash,
            reason=reason,
            resolved_at=datetime.now(UTC).isoformat(),
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
        cache[key] = entry
        write_cache(cache_path, cache, run_id=run_id or os.environ.get("WORCA_T_RUN_ID"))

    return result
