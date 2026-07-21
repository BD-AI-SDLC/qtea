"""Tests for Step 8's observed-element dev-pool seed (_seed_observed_dev_pool).

Verifies the JIT tier-1b intent pool is written from Step-7 observed elements,
respects an operator-supplied dev-locators source, and merges without clobber.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from qtea.steps.s08_codegen import _seed_observed_dev_pool


def _ctx(tmp_path, dev_locators=None):
    return SimpleNamespace(
        workspace=SimpleNamespace(root=tmp_path),
        options=SimpleNamespace(dev_locators=dev_locators),
    )


def _pool(**locators):
    return {"locators": locators}


def test_seed_writes_default_dev_locators(monkeypatch, tmp_path):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    _seed_observed_dev_pool(
        _ctx(tmp_path),
        _pool(observed_0={"selector": '[data-testid="x"]', "intent": "X btn"}),
    )
    dst = tmp_path / "locator-cache" / "dev-locators.json"
    assert dst.is_file()
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert "observed_0" in data["locators"]


def test_seed_skips_when_dev_locators_flag_supplied(monkeypatch, tmp_path):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    _seed_observed_dev_pool(
        _ctx(tmp_path, dev_locators=tmp_path / "mine.json"),
        _pool(observed_0={"selector": "#x"}),
    )
    assert not (tmp_path / "locator-cache" / "dev-locators.json").exists()


def test_seed_skips_when_env_var_supplied(monkeypatch, tmp_path):
    monkeypatch.setenv("QTEA_DEV_LOCATORS", str(tmp_path / "env.json"))
    _seed_observed_dev_pool(_ctx(tmp_path), _pool(observed_0={"selector": "#x"}))
    assert not (tmp_path / "locator-cache" / "dev-locators.json").exists()


def test_seed_merges_without_overwriting(monkeypatch, tmp_path):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    cache = tmp_path / "locator-cache"
    cache.mkdir()
    (cache / "dev-locators.json").write_text(
        json.dumps({"locators": {
            "hitl_1": {"selector": "#a"},
            "observed_0": {"selector": "#OLD"},
        }}),
        encoding="utf-8",
    )
    _seed_observed_dev_pool(
        _ctx(tmp_path),
        _pool(observed_0={"selector": "#NEW"}, observed_1={"selector": "#b"}),
    )
    data = json.loads((cache / "dev-locators.json").read_text(encoding="utf-8"))
    assert data["locators"]["hitl_1"]["selector"] == "#a"       # preserved
    assert data["locators"]["observed_0"]["selector"] == "#OLD"  # not clobbered
    assert data["locators"]["observed_1"]["selector"] == "#b"    # added


def test_seed_empty_pool_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    _seed_observed_dev_pool(_ctx(tmp_path), {"locators": {}})
    assert not (tmp_path / "locator-cache" / "dev-locators.json").exists()
