"""Unit tests for the Step 7 cross-run live-map cache."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from qtea.steps.s07 import live_map_cache
from qtea.steps.s07.live_map_cache import (
    PROBE_VERSION,
    CacheKey,
    _sha256_text,
    cache_enabled,
    cache_root,
    compute_key,
    load,
    save,
)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a scratch dir and ensure it's enabled."""
    monkeypatch.setenv("QTEA_LIVE_MAP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("QTEA_LIVE_MAP_CACHE", raising=False)
    return tmp_path / "cache"


def test_cache_enabled_off_when_env_set(monkeypatch):
    monkeypatch.setenv("QTEA_LIVE_MAP_CACHE", "off")
    assert cache_enabled() is False


def test_cache_enabled_on_by_default(monkeypatch):
    monkeypatch.delenv("QTEA_LIVE_MAP_CACHE", raising=False)
    assert cache_enabled() is True


def test_cache_root_respects_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("QTEA_LIVE_MAP_CACHE_DIR", str(tmp_path / "x"))
    assert cache_root() == tmp_path / "x"


def test_cache_key_fingerprint_is_stable_for_same_inputs(tmp_path):
    k1 = CacheKey(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    k2 = CacheKey(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    assert k1.fingerprint == k2.fingerprint


def test_cache_key_fingerprint_changes_when_any_component_changes():
    base = dict(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    baseline = CacheKey(**base).fingerprint
    for field, mutated in [
        ("sut_hash", "xyz"), ("design_hash", "different"),
        ("base_url", "https://y"), ("auth_mode", "mcp"),
        ("probe_version", PROBE_VERSION + 1),
    ]:
        variant = dict(base)
        variant[field] = mutated
        assert CacheKey(**variant).fingerprint != baseline, (
            f"fingerprint failed to change when {field} did"
        )


def test_compute_key_uses_design_hash_of_text(tmp_path):
    (tmp_path / ".git").mkdir()  # will fail git rev-parse but that's the point
    k = compute_key(
        sut_root=tmp_path,
        test_design_text="hello world",
        base_url="https://x/",
        auth_mode="Headed",
    )
    assert k.design_hash == _sha256_text("hello world")
    # auth_mode lower-cased.
    assert k.auth_mode == "headed"
    # base_url stripped of trailing slash.
    assert k.base_url == "https://x"


def test_save_then_load_roundtrips(cache_dir):
    key = CacheKey(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    live_map = {"base_url": "https://x", "routes": [{"path": "/", "exists": True,
                                                     "auth_required": False}]}
    # Bypass liveness probe for the save fingerprint too.
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP"):
        path = save(key, live_map)
    assert path is not None
    assert path.is_file()
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP"):
        out = load(key)
    assert out == live_map


def test_load_returns_none_on_liveness_mismatch(cache_dir):
    key = CacheKey(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    live_map = {"routes": []}
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP1"):
        save(key, live_map)
    # Fingerprint changed between save and load → cache miss.
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP2"):
        assert load(key) is None


def test_load_skips_liveness_when_disabled(cache_dir):
    key = CacheKey(
        sut_hash="abc", design_hash="def", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    live_map = {"routes": []}
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP1"):
        save(key, live_map)
    # verify_liveness=False bypasses the fingerprint check entirely.
    assert load(key, verify_liveness=False) == live_map


def test_load_returns_none_when_cache_disabled(cache_dir, monkeypatch):
    key = CacheKey(
        sut_hash="a", design_hash="b", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP"):
        save(key, {"routes": []})
    monkeypatch.setenv("QTEA_LIVE_MAP_CACHE", "off")
    assert load(key) is None


def test_save_noop_when_cache_disabled(monkeypatch, cache_dir):
    monkeypatch.setenv("QTEA_LIVE_MAP_CACHE", "off")
    key = CacheKey(
        sut_hash="a", design_hash="b", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    with patch.object(live_map_cache, "_liveness_fingerprint", return_value="FP"):
        assert save(key, {"routes": []}) is None


def test_load_returns_none_on_missing_file(cache_dir):
    key = CacheKey(
        sut_hash="does-not-exist", design_hash="x", base_url="https://x",
        auth_mode="headed", probe_version=PROBE_VERSION,
    )
    assert load(key, verify_liveness=False) is None
