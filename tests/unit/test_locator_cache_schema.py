"""Regression tests for `locator-cache.schema.json` wiring.

`locator-cache.json` had a schema with zero validation call sites, and the
schema's own `source` enum (`dev_verified`/`dev_unverified`/`cached`/`agent`/
`hitl`/`unresolvable`) never matched the values actually written by either
producer:

- `qtea.jit_resolver.write_cache()` (the `qtea resolve` CLI subprocess path)
  writes `source` in {"cached", "agent", "unresolvable"}.
- The vendored runtime template (`_resources/runtime/qtea_runtime.py.tpl`,
  injected into the SUT subprocess — cannot import `qtea.schemas`) writes
  `source` in {"dev", "dev-pool", "heuristic", "hitl", "none"} directly, and
  those entries only re-enter qtea's own process when Step 9 copies
  `locator-cache.json` into `artifacts/step09/` (`s09_execute.py`).

The schema's enum has been corrected to the real value set. Validation is
wired at both write points: `jit_resolver.write_cache()` (non-blocking log
warning) and the Step 9 artifact-publish copy in `s09_execute.py` (same).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from qtea.jit_resolver import write_cache
from qtea.schemas import is_valid
from qtea.steps.s09_execute import _validate_published_locator_cache


def _entry(**overrides) -> dict:
    base = {
        "key": "abc123",
        "test_file": "tests/test_login.py",
        "constant_name": "LOGIN_BUTTON",
        "intent": "log in button",
        "selector": "#login",
        "source": "agent",
    }
    base.update(overrides)
    return base


def test_write_cache_output_matches_schema_for_every_real_source_value():
    """Every source value actually emitted by either producer must validate."""
    for source in ("dev", "dev-pool", "cached", "heuristic", "agent", "hitl", "unresolvable", "none"):
        payload = {
            "run_id": "20260724-test",
            "produced_at": "2026-07-24T00:00:00+00:00",
            "entries": [_entry(key=source, source=source, selector=None if source in ("unresolvable", "none") else "#x")],
        }
        ok, err = is_valid(payload, "locator-cache")
        assert ok, f"source={source!r} failed: {err}"


def test_write_cache_logs_warning_on_schema_invalid_entry(tmp_path: Path):
    """`write_cache` must call `is_valid()` and log (not raise) on mismatch —
    a bad cache entry must not crash the resolver."""
    p = tmp_path / "locator-cache.json"
    malformed = {"bad-key": {"key": "bad-key", "selector": "#x"}}
    # constant_name/intent/source intentionally omitted (schema-required)
    with patch("qtea.jit_resolver.log") as mock_log:
        write_cache(p, malformed)
        assert mock_log.warning.called
        args = mock_log.warning.call_args
        assert args[0][0] == "jit_resolver.locator_cache_schema_invalid"
    # File is still written — validation is observability, not a gate.
    assert p.exists()
    written = json.loads(p.read_text(encoding="utf-8"))
    assert written["entries"]


def test_write_cache_no_warning_on_schema_valid_entries(tmp_path: Path):
    p = tmp_path / "locator-cache.json"
    entries = {"abc123": _entry()}
    with patch("qtea.jit_resolver.log") as mock_log:
        write_cache(p, entries)
        mock_log.warning.assert_not_called()


def test_dev_pool_promoted_entry_shape_matches_schema():
    """Mirrors the tier-1b promotion dict literal built inline in the
    vendored runtime template (`_resources/runtime/qtea_runtime.py.tpl`,
    around `_resolve_tiers_1_2`) — this file can't import `qtea` at all, so
    the shape is asserted here as a cross-file contract instead."""
    payload = {"entries": [{
        "key": "def456",
        "test_file": "tests/test_checkout.py",
        "constant_name": "SUBMIT_BTN",
        "intent": "submit order button",
        "selector": "#submit",
        "strategy": "id",
        "payload": None,
        "source": "dev-pool",
        "page_url": "https://x/checkout",
        "matched_constant": "SUBMIT_BUTTON",
        "pool_score": 0.92,
    }]}
    ok, err = is_valid(payload, "locator-cache")
    assert ok, err


def test_is_valid_rejects_payload_missing_required_entry_field():
    malformed = {"entries": [{
        "key": "abc123", "selector": "#x",
        # constant_name/intent/source intentionally omitted (schema-required)
    }]}
    ok, err = is_valid(malformed, "locator-cache")
    assert not ok
    assert err


# ---------------------------------------------------------------------------
# _validate_published_locator_cache — Step 9's publish-side validation of
# the vendored runtime template's own cache writes (which can't import
# qtea.schemas at all, since they run inside the SUT subprocess).
# ---------------------------------------------------------------------------


def test_validate_published_locator_cache_warns_on_schema_invalid_text():
    malformed_text = json.dumps({"entries": [{"key": "abc123", "selector": "#x"}]})
    with patch("qtea.steps.s09_execute.log") as mock_log:
        _validate_published_locator_cache(malformed_text)
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "step09.locator_cache_schema_invalid"


def test_validate_published_locator_cache_silent_on_valid_text():
    valid_text = json.dumps({"entries": [_entry()]})
    with patch("qtea.steps.s09_execute.log") as mock_log:
        _validate_published_locator_cache(valid_text)
        mock_log.warning.assert_not_called()


def test_validate_published_locator_cache_warns_on_unparseable_text():
    with patch("qtea.steps.s09_execute.log") as mock_log:
        _validate_published_locator_cache("{not json")
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args[0][0] == "step09.locator_cache_unparseable"
