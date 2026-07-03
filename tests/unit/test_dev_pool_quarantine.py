"""Tests for the dev-pool quarantine path.

When a tier-1b dev-pool selector fails at action time:
  1. The runtime calls `_quarantine_dev_pool_entry` → cache entry gets
     `quarantined: True`, JSONL record appended.
  2. Re-resolve runs with `skip_pool=True` so tier-1b doesn't bounce back
     to the same dev-pool answer.
  3. `_shadow_dev_pool_fallback` moves the LLM result to `_shadow:<key>`
     and restores the dev-pool entry (quarantined) at the standard key.
  4. Tier-2 reads now prefer the shadow entry.
  5. Step 9 reads the JSONL at end-of-run and emits a `dev-locator-drifted`
     bug-candidate per record.

These tests exercise the runtime helpers in isolation (via the template
loader from `test_runtime_plugin.py`) plus Step 9's candidate emitter.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from qtea.steps.s09_execute import _bug_candidates_for_dev_pool_drift

# ---------------------------------------------------------------------------
# Load runtime template (mirrors `test_runtime_plugin._load_runtime`)
# ---------------------------------------------------------------------------


def _load_runtime():
    import sys
    tpl = (
        Path(__file__).resolve().parents[2]
        / "src" / "qtea" / "_resources" / "runtime" / "qtea_runtime.py.tpl"
    )
    loader = SourceFileLoader("qtea_runtime_quar_under_test", str(tpl))
    spec = importlib.util.spec_from_loader("qtea_runtime_quar_under_test", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qtea_runtime_quar_under_test"] = mod
    try:
        loader.exec_module(mod)
    except Exception:
        sys.modules.pop("qtea_runtime_quar_under_test", None)
        raise
    return mod


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("QTEA_CACHE_DIR", str(tmp_path / "locator-cache"))
    return _load_runtime()


# ---------------------------------------------------------------------------
# _quarantine_dev_pool_entry: marks cache + writes JSONL
# ---------------------------------------------------------------------------


def test_quarantine_marks_entry_and_writes_jsonl(runtime, tmp_path):
    # Seed a dev-pool cache entry.
    key = runtime._cache_key("tests/x.py", "GEMINI", "Go to Gemini Enterprise")
    cache = {
        key: {
            "key": key,
            "intent": "Go to Gemini Enterprise",
            "constant_name": "GEMINI",
            "test_file": "tests/x.py",
            "selector": "[data-testid='stale-gemini']",
            "source": "dev-pool",
            "matched_constant": "goToGeminiBtn",
            "pool_score": 0.85,
        }
    }
    runtime._write_cache(cache)

    stale = runtime._Resolution(
        selector="[data-testid='stale-gemini']",
        source="dev-pool",
        constant_name="GEMINI",
        intent="Go to Gemini Enterprise",
        test_file="tests/x.py",
    )
    runtime._quarantine_dev_pool_entry(
        stale, page_url="https://example.com",
        exception=TimeoutError("Timeout 30000ms exceeded"),
    )

    # Cache entry now carries `quarantined: True`, selector unchanged.
    after = runtime._read_cache()
    assert after[key]["quarantined"] is True
    assert after[key]["selector"] == "[data-testid='stale-gemini']"

    # JSONL record written next to the cache dir.
    cache_dir = runtime._cache_path()
    log_path = cache_dir.parent / "dev-pool-quarantine.jsonl"
    assert log_path.exists()
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["constant_name"] == "GEMINI"
    assert record["matched_constant"] == "goToGeminiBtn"
    assert record["page_url"] == "https://example.com"
    assert "Timeout" in record["exception"]


# ---------------------------------------------------------------------------
# _shadow_dev_pool_fallback: restores dev-pool key, moves LLM to shadow
# ---------------------------------------------------------------------------


def test_shadow_fallback_preserves_dev_pool_and_relocates_llm(runtime):
    key = runtime._cache_key("tests/x.py", "X", "intent")
    # State after the resolver wrote its fresh LLM entry under the
    # standard key (overwriting the quarantined dev-pool record).
    cache = {
        key: {
            "key": key, "intent": "intent", "constant_name": "X",
            "test_file": "tests/x.py",
            "selector": "[data-testid='llm-answer']",
            "source": "agent",
        },
    }
    runtime._write_cache(cache)

    stale = runtime._Resolution(
        selector="[data-testid='stale']", source="dev-pool",
        constant_name="X", intent="intent", test_file="tests/x.py",
    )
    fresh = runtime._Resolution(
        selector="[data-testid='llm-answer']", source="agent",
        constant_name="X", intent="intent", test_file="tests/x.py",
    )
    runtime._shadow_dev_pool_fallback(stale, fresh)

    after = runtime._read_cache()
    # Standard key now carries the quarantined dev-pool entry again.
    assert after[key]["source"] == "dev-pool"
    assert after[key]["quarantined"] is True
    assert after[key]["selector"] == "[data-testid='stale']"
    # Shadow key carries the LLM answer.
    shadow_key = f"_shadow:{key}"
    assert shadow_key in after
    assert after[shadow_key]["source"] == "agent"
    assert after[shadow_key]["selector"] == "[data-testid='llm-answer']"


# ---------------------------------------------------------------------------
# Tier-2 read skips quarantined entries, uses shadow
# ---------------------------------------------------------------------------


def test_tier2_read_skips_quarantined_uses_shadow(runtime):
    key = runtime._cache_key(None, "X", "intent")
    cache = {
        key: {
            "key": key, "intent": "intent", "constant_name": "X",
            "selector": "[data-testid='stale']",
            "source": "dev-pool",
            "quarantined": True,
        },
        f"_shadow:{key}": {
            "key": f"_shadow:{key}", "intent": "intent", "constant_name": "X",
            "selector": "[data-testid='llm-answer']",
            "source": "agent",
        },
    }
    runtime._write_cache(cache)
    res = runtime._resolve_tiers_1_2(
        "intent", "X", None, None,
        skip_dev=True, skip_cache=False, skip_pool=True,
    )
    assert res is not None
    assert res.selector == "[data-testid='llm-answer']"


def test_tier2_returns_none_when_quarantined_and_no_shadow(runtime):
    key = runtime._cache_key(None, "X", "intent")
    cache = {
        key: {
            "key": key, "intent": "intent", "constant_name": "X",
            "selector": "[data-testid='stale']",
            "source": "dev-pool",
            "quarantined": True,
        },
    }
    runtime._write_cache(cache)
    res = runtime._resolve_tiers_1_2(
        "intent", "X", None, None,
        skip_dev=True, skip_cache=False, skip_pool=True,
    )
    # No shadow → tier 2 misses; caller proceeds to snapshot + LLM.
    assert res is None


# ---------------------------------------------------------------------------
# Step 9: dev-pool-quarantine.jsonl → bug-candidates
# ---------------------------------------------------------------------------


def test_bug_candidates_from_quarantine_log_emits_one_per_drift(tmp_path):
    log_path = tmp_path / "dev-pool-quarantine.jsonl"
    log_path.write_text("\n".join([
        json.dumps({
            "ts": "2026-06-22T08:00:00Z",
            "intent": "Go to Gemini",
            "constant_name": "GEMINI",
            "matched_constant": "goToGeminiBtn",
            "test_file": "tests/smoke/gemini_test.py",
            "stale_selector": "[data-testid='stale']",
            "page_url": "https://x.com",
            "exception": "TimeoutError: Timeout 30000ms",
            "pool_score": 0.85,
        }),
        json.dumps({
            "ts": "2026-06-22T08:00:01Z",
            "intent": "Submit",
            "constant_name": "SUBMIT",
            "matched_constant": "submitBtn",
            "page_url": "https://x.com",
            "exception": "TimeoutError",
        }),
    ]) + "\n", encoding="utf-8")
    candidates = _bug_candidates_for_dev_pool_drift(log_path)
    assert len(candidates) == 2
    assert candidates[0]["kind"] == "dev-locator-drifted"
    assert candidates[0]["matched_constant"] == "goToGeminiBtn"
    assert "stale" in candidates[0]["message"]
    assert candidates[0]["stale_selector"] == "[data-testid='stale']"


def test_bug_candidates_from_quarantine_log_dedupes_same_intent(tmp_path):
    log_path = tmp_path / "dev-pool-quarantine.jsonl"
    record = {
        "ts": "2026-06-22T08:00:00Z",
        "intent": "Go to Gemini",
        "constant_name": "GEMINI",
        "matched_constant": "goToGeminiBtn",
        "page_url": "https://x.com",
        "exception": "TimeoutError",
    }
    log_path.write_text("\n".join([json.dumps(record)] * 5) + "\n", encoding="utf-8")
    candidates = _bug_candidates_for_dev_pool_drift(log_path)
    assert len(candidates) == 1


def test_bug_candidates_from_missing_log_returns_empty(tmp_path):
    assert _bug_candidates_for_dev_pool_drift(tmp_path / "missing.jsonl") == []
