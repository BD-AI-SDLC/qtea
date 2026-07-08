"""JIT locator resolver — single-shot LLM call given AOM snapshot + intent.

Invoked as a subprocess by the vendored pytest runtime plugin
(`_resources/runtime/qtea_runtime.py.tpl`). The plugin shells out via
``qtea resolve --intent ... --snapshot ... --constant ... --cache ...``
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

# Selector-safety allowlist gate. Selectors flow from the LLM (or dev-locators
# JSON) into `locator-cache.json`, and from there are substituted into SUT
# source by `s09_execute._promote_resolved_tbds` (which then commits the
# patched file). A poisoned response or hostile dev-locators entry that
# contains a newline, `<script>` tag, or `javascript:` URI is not a selector
# — it's an attempt at code injection at promotion time. Reject at cache-
# write so the unsafe value never lands on disk; the substitution site uses
# `json.dumps` as a second line of defence.
_UNSAFE_SELECTOR_SUBSTRINGS = ("\n", "\r", "\x00", "<script", "javascript:")

# Structured payload kinds. When the resolver returns one of these, the
# runtime calls the corresponding `page.get_by_*` API at action time instead
# of `page.locator(string)`. Promotion writes a `role_locator(...)` / etc.
# call-expression into source, not a raw string. See `validate_selector_payload`.
_PAYLOAD_KINDS = ("role", "text", "label", "placeholder", "test_id", "css")

# Playwright debug-print syntax — `link "Go to Gemini Enterprise"`,
# `button "Sign in"`, etc. — appears in Playwright error messages and AOM
# snapshot tracebacks as a human-readable rendering of a role locator. It is
# NOT a valid CSS selector and NOT a valid Playwright engine selector. The
# LLM has been observed to copy this shape back into the `selector` field;
# `_validate_css_string` catches the regression at cache-write time.
_PLAYWRIGHT_DEBUG_ROLE_PREFIX = re.compile(
    r"^(link|button|textbox|combobox|checkbox|radio|tab|tabpanel|listbox|"
    r"option|menu|menuitem|menubar|heading|cell|row|columnheader|rowheader|"
    r"alert|alertdialog|dialog|status|article|banner|complementary|"
    r"contentinfo|navigation|main|region|search|form|group|list|listitem|"
    r"img|figure|paragraph|generic|separator|switch|slider|spinbutton|"
    r"progressbar|scrollbar|tooltip|treegrid|tree|treeitem|grid|gridcell|"
    r"toolbar)\s+['\"]",
    re.IGNORECASE,
)

# Playwright engine-selector prefixes — `role=button[name="x"]`, `text=Hello`,
# etc. Accepted as valid string-form selectors because Playwright's CSS engine
# understands them natively.
_PLAYWRIGHT_ENGINE_PREFIXES = frozenset({
    "role", "text", "css", "id", "label", "placeholder", "data-testid",
    "alt", "title", "internal:role", "internal:text", "internal:label",
    "internal:has", "internal:has-text", "internal:control", "nth", "xpath",
})


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
    # Structured payload for role/text/label/placeholder/test_id strategies.
    # None when the resolver chose a CSS-string form (back-compat: existing
    # cache entries on disk read back as payload=None and use the legacy
    # `page.locator(selector_string)` path). See `validate_selector_payload`.
    payload: dict[str, Any] | None = None

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
            "payload": self.payload,
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


def is_unsafe_selector(selector: str) -> bool:
    """Reject payloads that break out of string literals or carry executable
    content when substituted into SUT source by `_promote_resolved_tbds`.

    Re-exported so the substitution site can re-validate at promotion time
    (defense in depth — the cache file lives on disk and a long-running
    workspace could be tampered with between resolver write and promoter
    read).
    """
    s = selector or ""
    if not s.strip():
        return True
    lo = s.lower()
    return any(p in lo for p in _UNSAFE_SELECTOR_SUBSTRINGS)


def _validate_css_string(s: str) -> tuple[bool, str | None]:
    """Lightweight string-selector validator.

    Accepts: standard CSS (`#id`, `[attr=val]`, `.cls`, `tag`, scoped CSS,
    pseudo-classes), Playwright engine forms (`role=...`, `text=...`,
    `css=...`), and locator-priority shorthand (`#submit`, `[data-testid=x]`).

    Rejects: Playwright debug-print syntax (`link "..."`), unbalanced brackets,
    and empty strings. Trusts Playwright's CSS engine for the long tail of
    valid grammar — this is a sniff test, not a full parser. The contract is
    "block the specific regressions we've seen", not "validate all of CSS".
    """
    s = (s or "").strip()
    if not s:
        return False, "empty selector"
    # Playwright debug-print syntax — the exact bug that bit run 20260621.
    if _PLAYWRIGHT_DEBUG_ROLE_PREFIX.match(s):
        return False, (
            "selector looks like Playwright AOM debug syntax "
            f"(role + quoted name, no `=`): {s[:80]!r}. "
            "Use structured payload `{kind: 'role', role: ..., name: ...}` "
            "or the engine form `role=...[name=\"...\"]` instead."
        )
    # Playwright engine prefix (`role=`, `text=`, etc.) — accept verbatim.
    head = s.split("=", 1)[0].strip().lower() if "=" in s else ""
    if head in _PLAYWRIGHT_ENGINE_PREFIXES:
        return True, None
    # Bracket-balance is the cheapest "this won't parse" check.
    if s.count("[") != s.count("]"):
        return False, "unbalanced square brackets"
    if s.count("(") != s.count(")"):
        return False, "unbalanced parentheses"
    if s.count('"') % 2 != 0:
        return False, "unbalanced double quotes"
    if s.count("'") % 2 != 0:
        return False, "unbalanced single quotes"
    return True, None


def validate_selector_payload(
    payload: dict[str, Any] | None,
    selector: str | None,
) -> tuple[bool, str | None]:
    """Return ``(is_valid, reason_if_invalid)``.

    Called at three sites: candidate normalisation (resolver response →
    cache), cache write (last line of defence before disk), and TBD-promotion
    (last line of defence before SUT source substitution).

    Two paths:
      - Structured payload (``payload`` non-None): validate ``kind`` enum +
        required fields per kind. ``selector`` is treated as telemetry only.
      - String selector (``payload`` is None): validate via ``is_unsafe_selector``,
        ``is_xpath``, and ``_validate_css_string``.
    """
    # Structured payload path.
    if payload is not None:
        if not isinstance(payload, dict):
            return False, f"payload must be dict, got {type(payload).__name__}"
        kind = payload.get("kind")
        if kind not in _PAYLOAD_KINDS:
            return False, f"payload.kind must be one of {_PAYLOAD_KINDS}, got {kind!r}"
        if kind == "role":
            role = payload.get("role")
            if not isinstance(role, str) or not role.strip():
                return False, "role payload requires non-empty 'role' field"
            name = payload.get("name")
            if name is not None and (not isinstance(name, str) or not name):
                return False, "role payload 'name' must be a non-empty string if present"
        elif kind in ("text", "label", "placeholder"):
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                return False, f"{kind} payload requires non-empty 'text' field"
        elif kind == "test_id":
            value = payload.get("value")
            if not isinstance(value, str) or not value:
                return False, "test_id payload requires non-empty 'value' field"
        elif kind == "css":
            sel = payload.get("selector")
            if not isinstance(sel, str) or not sel.strip():
                return False, "css payload requires non-empty 'selector' field"
            if is_unsafe_selector(sel) or is_xpath(sel):
                return False, "css payload selector contains unsafe/XPath content"
            ok, why = _validate_css_string(sel)
            if not ok:
                return False, f"css payload: {why}"
        return True, None
    # String-only path (back-compat: pre-payload cache entries).
    if not isinstance(selector, str) or not selector.strip():
        return False, "empty or non-string selector"
    if is_unsafe_selector(selector):
        return False, "selector contains injection markers (newline / <script / javascript:)"
    if is_xpath(selector):
        return False, "XPath selectors are forbidden by locator-priority policy"
    return _validate_css_string(selector)


def parse_resolver_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise one LLM candidate dict into the canonical cache shape.

    Returns ``{"selector": str, "strategy": str|None, "payload": dict|None,
    "confidence": float|None, "reason": str|None}``. The LLM may emit either:

      - Structured: ``{"kind": "role", "role": "link", "name": "Sign in",
        "confidence": 0.95}`` → produces a ``payload`` and a derived telemetry
        ``selector`` (``"role=link[name=\"Sign in\"]"``) for back-compat reads.
      - String:     ``{"selector": "#submit", "strategy": "id",
        "confidence": 0.9}`` → ``payload`` is None.

    Raises ``ValueError`` when the entry is structurally unusable (caller
    catches and drops the candidate). Validation of the selector content
    itself happens in ``validate_selector_payload`` — separated so the
    caller can log a different reason for "malformed shape" vs "shape ok
    but content rejected".
    """
    if not isinstance(entry, dict):
        raise ValueError(f"candidate must be dict, got {type(entry).__name__}")

    kind = entry.get("kind")
    confidence = entry.get("confidence")
    confidence_f: float | None = (
        float(confidence) if isinstance(confidence, (int, float)) else None
    )
    reason = entry.get("reason") if isinstance(entry.get("reason"), str) else None

    if isinstance(kind, str) and kind in _PAYLOAD_KINDS:
        payload: dict[str, Any] = {"kind": kind}
        if kind == "role":
            role = entry.get("role")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("role kind requires non-empty 'role'")
            payload["role"] = role.strip()
            name = entry.get("name")
            if isinstance(name, str) and name:
                payload["name"] = name
            if entry.get("exact") is True:
                payload["exact"] = True
            # Telemetry-only string form for back-compat readers (Allure,
            # debug logs). Playwright's `role=` engine accepts this verbatim
            # if someone passes it through `page.locator()`.
            tele_parts = [f"role={payload['role']}"]
            if "name" in payload:
                tele_parts.append(f"[name={json.dumps(payload['name'])}]")
            derived_selector = "".join(tele_parts)
        elif kind in ("text", "label", "placeholder"):
            text = entry.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError(f"{kind} kind requires non-empty 'text'")
            payload["text"] = text
            if entry.get("exact") is True:
                payload["exact"] = True
            derived_selector = f"{kind}={text}"
        elif kind == "test_id":
            value = entry.get("value")
            if not isinstance(value, str) or not value:
                raise ValueError("test_id kind requires non-empty 'value'")
            payload["value"] = value
            derived_selector = f"[data-testid={json.dumps(value)}]"
        elif kind == "css":
            sel = entry.get("selector")
            if not isinstance(sel, str) or not sel.strip():
                raise ValueError("css kind requires non-empty 'selector'")
            payload["selector"] = sel.strip()
            derived_selector = sel.strip()
        else:  # pragma: no cover - guarded by _PAYLOAD_KINDS check
            raise ValueError(f"unhandled kind: {kind}")
        return {
            "selector": derived_selector,
            "strategy": kind if kind != "css" else None,
            "payload": payload,
            "confidence": confidence_f,
            "reason": reason,
        }

    # String-form fallback (legacy LLM shape).
    sel = entry.get("selector")
    if not isinstance(sel, str) or not sel.strip():
        raise ValueError("candidate has neither 'kind' nor non-empty 'selector'")
    return {
        "selector": sel.strip(),
        "strategy": normalise_strategy(entry.get("strategy")),
        "payload": None,
        "confidence": confidence_f,
        "reason": reason,
    }


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


def write_cache(
    cache_path: Path,
    entries: dict[str, dict[str, Any]],
    *,
    run_id: str | None = None,
) -> None:
    """Atomically write the cache (tmp + rename, like checkpoints.py).

    Entries whose ``selector`` + ``payload`` fail :func:`validate_selector_payload`
    are dropped before write (last line of defence — `_normalise_candidates`
    should have already filtered them upstream, but cache writes also happen
    from the runtime prewarm + dev-pool path and we don't want a malformed
    entry to ever land on disk where the TBD promoter could pick it up).
    Unresolvable entries (``selector=None``, ``source="unresolvable"``) and
    quarantine markers are allowed through unchanged.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    safe_entries: list[dict[str, Any]] = []
    for entry in entries.values():
        sel = entry.get("selector")
        pload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        # Allow null-selector entries (unresolvable / quarantine bookkeeping).
        if sel is None and pload is None:
            safe_entries.append(entry)
            continue
        ok, _why = validate_selector_payload(pload, sel)
        if ok:
            safe_entries.append(entry)
        # else: silently dropped — _normalise_candidates already logged.
    payload = {
        "run_id": run_id,
        "produced_at": datetime.now(UTC).isoformat(),
        "entries": safe_entries,
    }
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)


# ---------------------------------------------------------------------------
# Pre-warm: tier-1b dev-pool match at parent-process startup
# ---------------------------------------------------------------------------
#
# Mirrors the tokeniser + scorer in `_resources/runtime/qtea_runtime.py.tpl`
# (functions `_tokenize`, `_stem`, `_token_set_ratio`, `_pool_match`). Kept in
# sync by convention: both are pure Python, deterministic, and tiny — any
# divergence would surface as a behaviour delta between the in-process tier-1b
# match (parent) and the in-test fallback match (vendored runtime), which is
# easy to spot in resolver-spend telemetry. The duplication is the lesser
# evil compared to making the vendored runtime depend on a `qtea` import
# (which is unavailable inside the SUT subprocess).

_INTENT_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "for", "on", "in", "at", "is", "are",
    "and", "or", "with", "by", "that", "this", "it", "be",
})


def _intent_stem(tok: str) -> str:
    if len(tok) <= 3:
        return tok
    for suf in ("ing", "ies", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


_INTENT_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9]+")


def _intent_tokenize(text: str) -> set[str]:
    return {
        _intent_stem(t) for t in _INTENT_TOKEN_SPLIT.split((text or "").lower())
        if len(t) >= 2 and t not in _INTENT_STOPWORDS
    }


def _intent_token_set_ratio(a: str, b: str) -> float:
    sa, sb = _intent_tokenize(a), _intent_tokenize(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return (2.0 * inter) / (len(sa) + len(sb))


def prewarm_dev_pool_cache(
    *,
    tbd_intents: list[dict],
    dev_locators: dict[str, Any],
    cache_path: Path,
    run_id: str | None = None,
) -> int:
    """Pre-populate ``locator-cache.json`` with tier-1b dev-pool matches
    for every TBD intent known at step-8 codegen time.

    Each ``tbd_intents`` entry must carry ``intent`` and ``constant_name``;
    ``test_file`` is optional but recommended (it disambiguates same-named
    constants across test files in the cache key).

    ``dev_locators`` is the dict produced by
    :func:`qtea.runtime.dev_locators.load_dev_locators`.

    Returns the number of entries written. Entries already present in the
    cache are left untouched — this lets the pre-warm run idempotently on
    retry without clobbering tier-4 LLM resolutions from the prior attempt.

    Side benefits over lazy in-test resolution:
      - moves the (cheap) fuzzy match off the test's critical path;
      - lets the human reviewer inspect the prewarm log to see which
        dev-locator entries fired before any tests run, useful when
        tuning dev-locators.json.
    """
    threshold = float(os.environ.get("QTEA_DEV_POOL_THRESHOLD", "0.65"))
    margin = float(os.environ.get("QTEA_DEV_POOL_MARGIN", "0.10"))

    pool_entries = [
        (name, entry) for name, entry in dev_locators.items()
        if getattr(entry, "intent", None)
    ]
    if not pool_entries or not tbd_intents:
        return 0

    existing = read_cache(cache_path)
    added = 0
    for hit in tbd_intents:
        intent = (hit.get("intent") or "").strip()
        constant_name = (hit.get("constant_name") or "").strip()
        if not intent or not constant_name:
            continue
        test_file = hit.get("test_file")
        key = cache_key(test_file, constant_name, intent)
        if key in existing:
            continue
        # Score every dev-pool entry; require the winner to be above the
        # threshold AND beat second-best by the margin (mirrors the
        # runtime tier-1b ambiguity guard).
        scored: list[tuple[str, Any, float]] = [
            (name, entry, _intent_token_set_ratio(intent, entry.intent))
            for name, entry in pool_entries
        ]
        scored.sort(key=lambda x: x[2], reverse=True)
        best_name, best_entry, best_score = scored[0]
        second_score = scored[1][2] if len(scored) > 1 else 0.0
        if best_score < threshold or (best_score - second_score) < margin:
            continue
        pool_payload = getattr(best_entry, "payload", None)
        ok, _why = validate_selector_payload(pool_payload, best_entry.selector)
        if not ok:
            continue
        existing[key] = {
            "key": key,
            "test_file": test_file,
            "constant_name": constant_name,
            "intent": intent,
            "selector": best_entry.selector,
            "strategy": getattr(best_entry, "strategy", None),
            "payload": pool_payload,
            "source": "dev-pool",
            "page_url": getattr(best_entry, "page_url", None),
            "matched_constant": best_name,
            "pool_score": round(best_score, 3),
            "resolved_at": datetime.now(UTC).isoformat(),
            "prewarmed": True,
        }
        added += 1

    if added:
        write_cache(cache_path, existing, run_id=run_id)
    return added


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
    # When the runtime captured boxes (Playwright 1.60+, ``boxes=True``),
    # the snapshot YAML carries ``[box=x,y,w,h]`` annotations on each node.
    # Detected via substring check on the snapshot text — no protocol
    # change needed across the resolver socket boundary.
    boxes_rule = (
        "  8. Snapshot nodes may carry ``[box=x,y,width,height]`` (CSS "
        "pixels, viewport-relative). When two candidates share role+name, "
        "prefer the one whose box position best matches the intent's "
        "spatial hint ('top', 'header', 'footer', 'first', 'last'). NEVER "
        "emit the box itself in the selector.\n"
        if "[box=" in snapshot_text else ""
    )
    system = (
        "You resolve a UI locator from a Playwright accessibility snapshot. "
        "Read the snapshot, find the element matching the locator's intent, "
        "and return a ranked bundle of up to two selector candidates as a "
        "STRUCTURED PAYLOAD so the runtime can call the right Playwright API "
        "(get_by_role / get_by_text / get_by_label / get_by_placeholder / "
        "get_by_test_id / locator) directly. Stringly-typed CSS is only one "
        "kind among several — emitting a role match as a CSS string will be "
        "rejected as it has been observed to corrupt cached entries.\n"
        "Rules:\n"
        "  1. Output ONLY a JSON object. Your first character MUST be `{` "
        "and your last character MUST be `}`. No prose before or after, "
        "no markdown fences, no commentary.\n"
        "  2. Every candidate MUST resolve to EXACTLY ONE element in the "
        "provided snapshot. NEVER invent roles, names, testids, or text not "
        "in the snapshot. If your chosen role+name would match MULTIPLE nodes "
        "(e.g. two buttons both named \"OK\"), it is NOT acceptable — make it "
        "unique by preferring a `test_id` or `id` (`kind:\"css\"` with `#id`), "
        "or a scoped CSS that isolates the one intended node, or use the "
        "spatial hint to disambiguate. If you genuinely cannot produce a "
        "unique selector, return empty `candidates` and say so in `reason` "
        "rather than emitting an ambiguous match.\n"
        "  3. Locator priority (use the highest-applicable for the primary): "
        "id (via `kind: \"css\"` with `#id` selector) > "
        "data-testid (via `kind: \"test_id\"`) > "
        "role (via `kind: \"role\"`) > "
        "label (via `kind: \"label\"`) > "
        "text (via `kind: \"text\"`) > "
        "placeholder (via `kind: \"placeholder\"`) > "
        "scoped css (via `kind: \"css\"`). "
        "NEVER XPath.\n"
        "  4. The fallback (when present) MUST use a different `kind` than "
        "the primary. Omit the fallback when no defensible alternate exists.\n"
        "  5. If no element clearly matches the intent at confidence >= 0.6, "
        "return an empty `candidates` array and set top-level `reason`.\n"
        "  6. Each candidate is a discriminated union by `kind`:\n"
        "       `{\"kind\":\"role\", \"role\":<aom-role>, \"name\":<accessible-name|optional>, \"exact\":<bool|optional>}`\n"
        "       `{\"kind\":\"text\", \"text\":<string>, \"exact\":<bool|optional>}`\n"
        "       `{\"kind\":\"label\", \"text\":<label-string>}`\n"
        "       `{\"kind\":\"placeholder\", \"text\":<placeholder-string>}`\n"
        "       `{\"kind\":\"test_id\", \"value\":<testid>}`\n"
        "       `{\"kind\":\"css\", \"selector\":<css-string>}`\n"
        "     plus optional `confidence` (0-1) and `reason`.\n"
        "  7. NEVER emit Playwright debug-print syntax like "
        "`\"selector\": \"link \\\"Sign in\\\"\"` — that is what a snapshot "
        "renders for a role match, not a valid selector. Use "
        "`{\"kind\":\"role\", \"role\":\"link\", \"name\":\"Sign in\"}` "
        "instead. Likewise never wrap a CSS string in `getByRole(...)` or "
        "any other function-call syntax.\n"
        + pool_rule
        + boxes_rule +
        "Worked examples:\n"
        "  Snapshot has `link \"Go to Gemini Enterprise\" [ref=e24]` →\n"
        "    `{\"kind\":\"role\",\"role\":\"link\",\"name\":\"Go to Gemini Enterprise\",\"confidence\":0.95}`\n"
        "  Snapshot has `button \"Sign in\" [ref=e7] data-testid=\"login-submit\"` →\n"
        "    `{\"kind\":\"test_id\",\"value\":\"login-submit\",\"confidence\":0.98}`\n"
        "  Snapshot has plain `<input id=\"email\">` →\n"
        "    `{\"kind\":\"css\",\"selector\":\"#email\",\"confidence\":0.99}`\n"
        "Output shape: "
        "{\"candidates\": [ <candidate>, ...up to 2 ], "
        "\"reason\": <string|null>}"
    )
    user_parts = [
        f"Locator constant: `{constant_name}`",
        f"Intent: {intent}",
    ]
    if page_url:
        user_parts.append(f"Current page URL: {page_url}")
    if dev_pool:
        lines = [
            "Candidate selectors from the dev team"
            " (prefer when they match the intent"
            " and resolve in the snapshot):"
        ]
        for e in dev_pool:
            sel = e.get("selector", "")
            ent_intent = e.get("intent") or ""
            ent_url = e.get("page_url") or ""
            line = f"- selector={sel!r}  intent={ent_intent!r}"
            if ent_url:
                line += f"  page_url={ent_url!r}"
            lines.append(line)
        user_parts.append("\n".join(lines))
    # The modern ladder (Playwright 1.40+) emits YAML; the legacy
    # ``page.accessibility.snapshot()`` fallback emits a JSON dict. Label
    # the fence truthfully so the model parses correctly.
    fence_lang = "json" if snapshot_text.lstrip().startswith("{") else "yaml"
    user_parts.append(f"AOM snapshot:\n```{fence_lang}\n{snapshot_text}\n```")
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
    from qtea.config import (
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

    Accepts three shapes:
      - Structured bundle: ``{"candidates": [{"kind": "role", ...}, ...]}``
        — the post-payload prompt shape; each candidate carries a discriminated
        union with `kind` (role/text/label/placeholder/test_id/css).
      - String bundle (legacy): ``{"candidates": [{"selector": ..., "strategy": ...}, ...]}``.
      - Flat (legacy): ``{"selector": ..., "strategy": ..., "confidence": ...}``.

    Each candidate is normalised via :func:`parse_resolver_payload`, then
    content-validated via :func:`validate_selector_payload`. Candidates that
    fail either gate are dropped silently (logged via the resolver-spend
    telemetry at the caller); the priority-chain rule on the survivors is
    enforced by the caller.
    """
    raw = parsed.get("candidates")
    if not isinstance(raw, list):
        # Legacy flat shape — wrap into a single-candidate list.
        if parsed.get("selector") or parsed.get("kind"):
            raw = [parsed]
        else:
            return []

    out: list[dict[str, Any]] = []
    for entry in raw:
        try:
            normalised = parse_resolver_payload(entry)
        except ValueError:
            continue
        ok, _why = validate_selector_payload(
            normalised.get("payload"), normalised.get("selector"),
        )
        if not ok:
            continue
        out.append(normalised)
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
        cached_payload = cached.get("payload") if isinstance(cached.get("payload"), dict) else None
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
            payload=cached_payload,
        )

    chosen_model = model or os.environ.get("QTEA_RESOLVER_MODEL") or _DEFAULT_MODEL
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
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= _MAX_API_RETRIES:
                return ResolutionResult(
                    selector=None, strategy=None, confidence=None,
                    source="unresolvable", intent=intent,
                    constant_name=constant_name, page_url=page_url,
                    snapshot_hash=snap_hash,
                    reason=(
                        f"resolver API failed after"
                        f" {_MAX_API_RETRIES + 1} attempts:"
                        f" {last_error}"
                    ),
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
            payload=primary.get("payload"),
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
            "payload": result.payload,
            "confidence": result.confidence,
            "source": result.source,
            "page_url": result.page_url,
            "snapshot_hash": result.snapshot_hash,
            "resolved_at": result.resolved_at,
        }
        if result.candidates:
            entry["candidates"] = list(result.candidates)
        cache[key] = entry
        write_cache(cache_path, cache, run_id=run_id or os.environ.get("QTEA_RUN_ID"))

    return result
