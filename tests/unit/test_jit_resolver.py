"""Tests for the JIT resolver module.

The Anthropic SDK call is mocked at module level — these tests cover the
prompt construction, JSON parsing, XPath rejection, cache hit/miss path,
and bounded-retry behaviour without making real API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from worca_t.jit_resolver import (
    ResolutionResult,
    cache_key,
    is_xpath,
    normalise_strategy,
    read_cache,
    resolve_one,
    snapshot_hash,
    write_cache,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_cache_key_is_stable_across_whitespace_in_intent():
    """Whitespace differences in intent must not produce different cache keys —
    the codegen agent may emit slightly different whitespace from one run to
    the next; cache hits should still happen."""
    k1 = cache_key("tests/t.py", "LOGIN_BUTTON", "primary submit button")
    k2 = cache_key("tests/t.py", "LOGIN_BUTTON", "  primary   submit   button  ")
    k3 = cache_key("tests/t.py", "LOGIN_BUTTON", "Primary Submit Button")  # case
    assert k1 == k2 == k3


def test_cache_key_differs_across_test_files():
    k1 = cache_key("tests/a.py", "X", "intent")
    k2 = cache_key("tests/b.py", "X", "intent")
    assert k1 != k2


def test_is_xpath_detects_all_flavours():
    assert is_xpath("//div[@id='x']")
    assert is_xpath("xpath=//button")
    assert is_xpath("By.XPATH, '//x'")
    assert not is_xpath("#submit")
    assert not is_xpath("[data-testid='login']")
    assert not is_xpath("")


def test_normalise_strategy_accepts_known_values():
    assert normalise_strategy("data-testid") == "data-testid"
    assert normalise_strategy("ID") == "id"
    assert normalise_strategy("  ROLE  ") == "role"


def test_normalise_strategy_rejects_unknown():
    assert normalise_strategy("xpath") is None
    assert normalise_strategy("magic") is None
    assert normalise_strategy(None) is None
    assert normalise_strategy("") is None


def test_snapshot_hash_is_deterministic():
    a = snapshot_hash('{"role":"button"}')
    b = snapshot_hash('{"role":"button"}')
    c = snapshot_hash('{"role":"link"}')
    assert a == b
    assert a != c
    assert len(a) == 16  # 16-hex-char prefix


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def test_read_cache_missing_file_returns_empty(tmp_path: Path):
    assert read_cache(tmp_path / "nope.json") == {}


def test_read_cache_invalid_json_returns_empty(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text("not json{", encoding="utf-8")
    assert read_cache(p) == {}


def test_read_cache_missing_entries_field_returns_empty(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    assert read_cache(p) == {}


def test_write_then_read_cache_roundtrip(tmp_path: Path):
    p = tmp_path / "cache.json"
    entries = {
        "abc123": {"key": "abc123", "selector": "#x", "intent": "x"},
        "def456": {"key": "def456", "selector": "[data-testid=y]", "intent": "y"},
    }
    write_cache(p, entries, run_id="20260607-test")
    out = read_cache(p)
    assert set(out.keys()) == {"abc123", "def456"}
    assert out["abc123"]["selector"] == "#x"


# ---------------------------------------------------------------------------
# resolve_one — cache-hit path (no LLM call)
# ---------------------------------------------------------------------------


def test_resolve_one_cache_hit_skips_llm(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    # Pre-populate cache.
    key = cache_key("tests/t.py", "LOGIN", "submit button")
    write_cache(
        cache / "locator-cache.json",
        {
            key: {
                "key": key,
                "test_file": "tests/t.py",
                "constant_name": "LOGIN",
                "intent": "submit button",
                "selector": "#cached-login",
                "strategy": "id",
                "confidence": 0.95,
                "source": "agent",
            },
        },
    )
    # Mock _call_anthropic so test fails if it's invoked.
    with patch("worca_t.jit_resolver._call_anthropic", side_effect=AssertionError("LLM called on cache hit")):
        result = resolve_one(
            intent="submit button",
            snapshot_text='{"role":"button"}',
            constant_name="LOGIN",
            test_file="tests/t.py",
            cache_dir=cache,
        )
    assert result.source == "cached"
    assert result.selector == "#cached-login"
    assert result.strategy == "id"


# ---------------------------------------------------------------------------
# resolve_one — LLM path (mocked)
# ---------------------------------------------------------------------------


def _fake_anthropic_response(payload: dict) -> tuple[str, dict[str, int | None]]:
    """Return what _call_anthropic returns: ``(json_string, usage_dict)``.

    The JSON string starts with `{`; the usage dict carries token telemetry
    (``input_tokens`` / ``output_tokens``) consumed by resolve_one (Phase 6).
    """
    return json.dumps(payload), {"input_tokens": 42, "output_tokens": 17}


def test_resolve_one_llm_success_caches_and_returns_agent_source(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "selector": "[data-testid='login-submit']",
            "strategy": "data-testid",
            "confidence": 0.92,
            "reason": None,
        }),
    ):
        result = resolve_one(
            intent="primary submit button on the login form",
            snapshot_text='{"role":"button","name":"Sign in"}',
            constant_name="LOGIN_BUTTON",
            test_file="tests/test_login.py",
            cache_dir=cache,
        )
    assert result.source == "agent"
    assert result.selector == "[data-testid='login-submit']"
    assert result.strategy == "data-testid"
    assert result.confidence == pytest.approx(0.92)

    # Cache populated with the resolution.
    cached = read_cache(cache / "locator-cache.json")
    assert len(cached) == 1
    entry = next(iter(cached.values()))
    assert entry["selector"] == "[data-testid='login-submit']"
    assert entry["source"] == "agent"


def test_resolve_one_llm_returns_xpath_treated_as_unresolvable(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "selector": "//button[@id='login']",
            "strategy": "xpath",
            "confidence": 0.9,
        }),
    ):
        result = resolve_one(
            intent="submit button",
            snapshot_text="{}",
            constant_name="LOGIN",
            cache_dir=cache,
        )
    assert result.source == "unresolvable"
    assert result.selector is None
    assert "XPath" in (result.reason or "")


def test_resolve_one_llm_returns_null_selector_unresolvable(tmp_path: Path):
    """When the LLM honestly says it can't find the element, the result is
    unresolvable (not cached as a fake success)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "selector": None,
            "strategy": None,
            "confidence": None,
            "reason": "no button with name 'Sign in' in snapshot",
        }),
    ):
        result = resolve_one(
            intent="submit button",
            snapshot_text='{"role":"button","name":"Cancel"}',
            constant_name="LOGIN",
            cache_dir=cache,
        )
    assert result.source == "unresolvable"
    assert result.selector is None
    assert "no button" in (result.reason or "")


def test_resolve_one_bounded_retry_on_api_failure(tmp_path: Path):
    """After _MAX_API_RETRIES+1 transport failures, result is unresolvable
    (not raised). The plugin uses `source` to decide pytest.fail behavior."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        side_effect=RuntimeError("connection reset"),
    ), patch("worca_t.jit_resolver.time.sleep") as sleep_mock:
        result = resolve_one(
            intent="submit button",
            snapshot_text="{}",
            constant_name="LOGIN",
            cache_dir=cache,
        )
    assert result.source == "unresolvable"
    assert "connection reset" in (result.reason or "")
    # Sleep called between attempts (max retries == 2 means 2 sleeps).
    assert sleep_mock.call_count == 2


def test_resolve_one_no_cache_dir_still_works(tmp_path: Path):
    """When no cache_dir is supplied (e.g. test or one-off call), resolution
    still happens but the result isn't persisted."""
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "selector": "#x", "strategy": "id", "confidence": 0.9,
        }),
    ):
        result = resolve_one(
            intent="x", snapshot_text="{}", constant_name="X",
            cache_dir=None,
        )
    assert result.source == "agent"
    assert result.selector == "#x"


# ---------------------------------------------------------------------------
# ResolutionResult.as_dict — serialisation contract
# ---------------------------------------------------------------------------


def test_resolution_result_as_dict_round_trip():
    r = ResolutionResult(
        selector="#x", strategy="id", confidence=0.9, source="agent",
        intent="submit button", constant_name="LOGIN",
        page_url="https://example.test/login",
        snapshot_hash="abc123",
        resolved_at="2026-06-07T12:00:00Z",
    )
    d = r.as_dict()
    assert d["selector"] == "#x"
    assert d["source"] == "agent"
    assert d["constant_name"] == "LOGIN"
    assert d["candidates"] is None  # no bundle when not supplied
    # JSON-serializable
    json.dumps(d)


# ---------------------------------------------------------------------------
# Multi-candidate bundle parsing + cache round-trip
# ---------------------------------------------------------------------------


def test_resolve_one_parses_two_candidate_bundle(tmp_path: Path):
    """LLM returns a {candidates: [primary, fallback]} bundle; resolve_one
    surfaces it on the result and writes it to the cache so the runtime can
    use the fallback on TimeoutError without re-calling the LLM."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "candidates": [
                {"selector": "[data-testid='login-submit']", "strategy": "data-testid", "confidence": 0.92, "reason": None},
                {"selector": "role=button[name=\"Sign in\"]", "strategy": "role", "confidence": 0.78, "reason": "fallback if data-testid drops"},
            ],
            "reason": None,
        }),
    ):
        result = resolve_one(
            intent="primary submit button on the login form",
            snapshot_text='{"role":"button","name":"Sign in"}',
            constant_name="LOGIN_BUTTON",
            test_file="tests/test_login.py",
            cache_dir=cache,
        )
    assert result.source == "agent"
    assert result.selector == "[data-testid='login-submit']"
    assert result.strategy == "data-testid"
    assert result.candidates is not None
    assert len(result.candidates) == 2
    assert result.candidates[1]["selector"] == "role=button[name=\"Sign in\"]"
    assert result.candidates[1]["strategy"] == "role"

    # Cache carries the bundle for runtime reuse.
    cached = read_cache(cache / "locator-cache.json")
    entry = next(iter(cached.values()))
    assert entry["selector"] == "[data-testid='login-submit']"
    assert isinstance(entry.get("candidates"), list)
    assert len(entry["candidates"]) == 2


def test_resolve_one_accepts_single_candidate_bundle(tmp_path: Path):
    """The LLM is allowed to return just one candidate when no defensible
    alternate exists — result is still source=agent with len(candidates)==1."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "candidates": [
                {"selector": "#unique-id", "strategy": "id", "confidence": 0.95, "reason": None},
            ],
        }),
    ):
        result = resolve_one(
            intent="x", snapshot_text="{}", constant_name="X",
            cache_dir=cache,
        )
    assert result.source == "agent"
    assert result.candidates is not None
    assert len(result.candidates) == 1


def test_resolve_one_drops_xpath_candidates_from_bundle(tmp_path: Path):
    """Bundle entries that violate the priority chain (XPath) are dropped
    silently; the remaining valid candidates survive."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "candidates": [
                {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
                {"selector": "//button[@id='go']", "strategy": "xpath", "confidence": 0.5},
            ],
        }),
    ):
        result = resolve_one(
            intent="go", snapshot_text="{}", constant_name="GO",
            cache_dir=cache,
        )
    assert result.source == "agent"
    assert result.selector == "[data-testid='go']"
    assert result.candidates is not None
    assert len(result.candidates) == 1  # XPath entry dropped


def test_resolve_one_falls_back_to_flat_shape_for_legacy_response(tmp_path: Path):
    """If the model regresses to the older single-selector output shape,
    the parser wraps it into a single-entry bundle (backward-compat)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "selector": "#legacy", "strategy": "id", "confidence": 0.8, "reason": None,
        }),
    ):
        result = resolve_one(
            intent="legacy", snapshot_text="{}", constant_name="L",
            cache_dir=cache,
        )
    assert result.source == "agent"
    assert result.selector == "#legacy"
    assert result.candidates is not None
    assert len(result.candidates) == 1
    assert result.candidates[0]["selector"] == "#legacy"


def test_resolve_one_empty_candidates_array_is_unresolvable(tmp_path: Path):
    """The new shape's null case: empty candidates array → unresolvable,
    with the top-level reason surfaced on the result."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with patch(
        "worca_t.jit_resolver._call_anthropic",
        return_value=_fake_anthropic_response({
            "candidates": [],
            "reason": "no button with name 'Sign in' present",
        }),
    ):
        result = resolve_one(
            intent="x", snapshot_text="{}", constant_name="X",
            cache_dir=cache,
        )
    assert result.source == "unresolvable"
    assert result.selector is None
    assert "no button" in (result.reason or "")


def test_cache_round_trip_preserves_candidates(tmp_path: Path):
    """Bundle survives write+read so a cache hit on the next call
    surfaces the same fallback alternates to the runtime."""
    p = tmp_path / "cache.json"
    entries = {
        "abc123": {
            "key": "abc123",
            "selector": "[data-testid='go']",
            "strategy": "data-testid",
            "confidence": 0.9,
            "candidates": [
                {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
                {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
            ],
            "source": "agent",
            "intent": "go",
        },
    }
    write_cache(p, entries, run_id="20260615-test")
    out = read_cache(p)
    assert len(out["abc123"]["candidates"]) == 2
    assert out["abc123"]["candidates"][1]["selector"] == "text=Go"


def test_resolve_one_cache_hit_surfaces_bundle(tmp_path: Path):
    """When the cache already has a bundle, resolve_one returns it on the
    result so the runtime's _RetryingLocator can walk the fallback."""
    cache = tmp_path / "cache"
    cache.mkdir()
    key = cache_key("tests/t.py", "GO", "go button")
    write_cache(
        cache / "locator-cache.json",
        {
            key: {
                "key": key,
                "test_file": "tests/t.py",
                "constant_name": "GO",
                "intent": "go button",
                "selector": "[data-testid='go']",
                "strategy": "data-testid",
                "confidence": 0.9,
                "candidates": [
                    {"selector": "[data-testid='go']", "strategy": "data-testid", "confidence": 0.9},
                    {"selector": "text=Go", "strategy": "text", "confidence": 0.7},
                ],
                "source": "agent",
            },
        },
    )
    with patch("worca_t.jit_resolver._call_anthropic", side_effect=AssertionError("LLM called on cache hit")):
        result = resolve_one(
            intent="go button",
            snapshot_text="{}",
            constant_name="GO",
            test_file="tests/t.py",
            cache_dir=cache,
        )
    assert result.source == "cached"
    assert result.candidates is not None
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# Vertex-AI compatibility: no assistant-message prefill (rejected by Vertex
# and the Bosch BMF Vertex relay with "This model does not support
# assistant message prefill. The conversation must end with a user
# message.")
# ---------------------------------------------------------------------------


def test_parse_response_tolerates_leading_prose():
    """`_parse_response` must find the first balanced JSON object even when
    the model emits a leading newline, whitespace, or a stray token before
    the opening brace. Without the assistant-prefill nudge this is the
    safety net against rare format slips."""
    from worca_t.jit_resolver import _parse_response

    payload = '{"candidates": [{"selector": "#x", "strategy": "id", "confidence": 0.9}]}'
    for prefix in ("", "\n", "  ", "```json\n", "Here is the JSON:\n"):
        parsed = _parse_response(prefix + payload)
        assert parsed["candidates"][0]["selector"] == "#x"


def test_call_anthropic_messages_end_with_user_role(tmp_path: Path):
    """Regression test for the Vertex AI 400 'assistant prefill' error.
    The `messages` list sent to the Anthropic SDK must end with a user-role
    message — never assistant — so the same call works on both native
    Anthropic and Vertex backends."""
    from worca_t.jit_resolver import _call_anthropic

    captured: dict = {}

    class _FakeUsage:
        input_tokens = 10
        output_tokens = 5

    class _FakeBlock:
        type = "text"
        text = '{"candidates": []}'

    class _FakeResponse:
        content: ClassVar[list] = [_FakeBlock()]
        usage = _FakeUsage()

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    with patch("worca_t.config.use_vertex_backend", return_value=False), \
         patch("anthropic.Anthropic", return_value=_FakeClient()):
        body, _ = _call_anthropic("sys", "user", model="claude-sonnet-4-6")

    msgs = captured["messages"]
    assert msgs[-1]["role"] == "user", (
        f"Vertex AI rejects assistant-prefill; final message must be user. "
        f"Got: {[m['role'] for m in msgs]}"
    )
    assert all(m["role"] == "user" for m in msgs), (
        f"Only user-role messages expected in single-turn resolver call; "
        f"got: {[m['role'] for m in msgs]}"
    )
    assert body.startswith("{"), f"body should start with JSON open brace, got: {body!r}"
