"""Tests for the JIT resolver module.

The Anthropic SDK call is mocked at module level — these tests cover the
prompt construction, JSON parsing, XPath rejection, cache hit/miss path,
and bounded-retry behaviour without making real API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
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


def _fake_anthropic_response(payload: dict) -> str:
    """Return what _call_anthropic would return: a JSON string starting with `{`."""
    return json.dumps(payload)


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
    # JSON-serializable
    json.dumps(d)
