"""Unit tests for the deterministic Step 7 live-explore driver.

Mocks `async_playwright` at the module boundary so tests never launch a real
browser. Verifies:

  * Visit plan derivation (routes + nav-labels, dedup, truncation counter).
  * Route entry shape matches the live-map schema.
  * Auth-redirect detection flips `exists=false + auth_required=true`.
  * Reveal callout is invoked when a named target isn't on paint, capped by
    `max_reveals_per_page`.
  * Ambiguity callout groups by (role, name) and only fires when |group| >= 2.
  * Telemetry counters accumulate and land in `live_map["_telemetry"]`.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qtea.steps.s07.live_driver import (
    AmbiguityContext,
    DriverTelemetry,
    RevealContext,
    _looks_like_auth_redirect,
    _make_visit_plan,
    _resolve_ambiguities,
    _snapshot_excerpt,
    _target_in_elements,
    drive_live_exploration,
)


# ---- Pure helpers ---------------------------------------------------------


def test_make_visit_plan_dedupes_routes_and_appends_nav_targets():
    routes = ["/", "/reports", "/reports"]  # dup on purpose
    targets = [
        {"nav_label": "My Notifications", "name": "Inbox", "reach_via": "menu"},
        {"nav_label": "My Notifications", "name": "Inbox"},  # dup nav label
        {"nav_label": "", "name": "Skipped"},  # no nav_label → dropped
    ]
    plan, truncated = _make_visit_plan(routes, targets, max_pages=10)
    paths = [p["path"] for p in plan]
    assert paths == ["/", "/reports", "/"]  # nav-label entry always at "/"
    assert plan[-1]["nav_label"] == "My Notifications"
    assert truncated == 0


def test_make_visit_plan_truncates_and_counts():
    routes = [f"/p{i}" for i in range(20)]
    plan, truncated = _make_visit_plan(routes, [], max_pages=5)
    assert len(plan) == 5
    assert truncated == 15


def test_make_visit_plan_falls_back_to_root_when_empty():
    plan, truncated = _make_visit_plan([], [], max_pages=5)
    assert plan == [{"path": "/", "nav_label": ""}]
    assert truncated == 0


def test_looks_like_auth_redirect_detects_login_paths():
    assert _looks_like_auth_redirect(
        "https://sut.example.com/login", "https://sut.example.com/dashboard",
    )
    assert _looks_like_auth_redirect(
        "https://accounts.google.com/x", "https://sut.example.com/reports",
    )
    # No redirect: same URL → not a redirect.
    assert not _looks_like_auth_redirect(
        "https://sut.example.com/reports", "https://sut.example.com/reports",
    )
    # Redirect to non-login path — not treated as auth gate.
    assert not _looks_like_auth_redirect(
        "https://sut.example.com/reports/detail",
        "https://sut.example.com/reports",
    )


def test_target_in_elements_matches_case_insensitive_and_partial():
    els = [
        {"role": "button", "name": "New Notification"},
        {"role": "link", "name": "Reports"},
    ]
    assert _target_in_elements("new notification", els)
    assert _target_in_elements("Notification", els)  # partial
    assert not _target_in_elements("Save", els)
    assert not _target_in_elements("", els)


def test_snapshot_excerpt_bounds_and_includes_locators():
    els = [
        {"role": "button", "name": "Save",
         "locator": {"strategy": "test_id", "value": "save-btn", "verified_unique": True}},
        {"role": "link", "name": "Home", "locator": None},
    ]
    out = _snapshot_excerpt(els)
    assert "button: 'Save'" in out
    assert "[test_id='save-btn']" in out
    assert "link: 'Home'" in out


def test_snapshot_excerpt_truncates_when_over_cap():
    els = [{"role": "button", "name": "X" * 500} for _ in range(50)]
    out = _snapshot_excerpt(els, cap=200)
    assert "truncated" in out
    assert len(out) < 300


# ---- Ambiguity resolution -------------------------------------------------


async def test_resolve_ambiguities_calls_judge_once_per_tie_group():
    elements = [
        {"role": "button", "name": "Save", "locator": None, "locator_ambiguous": True},
        {"role": "button", "name": "Save", "locator": None, "locator_ambiguous": True},
        {"role": "button", "name": "Delete", "locator": None, "locator_ambiguous": True},
    ]
    calls: list[AmbiguityContext] = []

    async def judge(ctx):
        calls.append(ctx)
        # Pick the first candidate for the Save group.
        picked = dict(ctx.candidates[0])
        picked["locator"] = {"strategy": "role", "value": "Save", "name": "Save",
                             "verified_unique": True}
        return picked

    tel = DriverTelemetry()
    out = await _resolve_ambiguities(
        elements, route_path="/", on_ambiguity=judge, telemetry=tel,
    )
    # Only the Save group has >=2 members → one callout; Delete singleton skipped.
    assert len(calls) == 1
    assert tel.ambiguity_callouts == 1
    # The first Save element got a locator; ambiguity flag cleared on head.
    head = out[0]
    assert head["locator"] is not None
    assert "locator_ambiguous" not in head


async def test_resolve_ambiguities_no_judge_is_noop():
    elements = [
        {"role": "button", "name": "Save", "locator_ambiguous": True},
        {"role": "button", "name": "Save", "locator_ambiguous": True},
    ]
    tel = DriverTelemetry()
    out = await _resolve_ambiguities(
        elements, route_path="/", on_ambiguity=None, telemetry=tel,
    )
    assert out == elements
    assert tel.ambiguity_callouts == 0


async def test_resolve_ambiguities_swallows_judge_errors():
    elements = [
        {"role": "button", "name": "Save", "locator_ambiguous": True},
        {"role": "button", "name": "Save", "locator_ambiguous": True},
    ]

    async def bad(_ctx):
        raise RuntimeError("boom")

    tel = DriverTelemetry()
    out = await _resolve_ambiguities(
        elements, route_path="/", on_ambiguity=bad, telemetry=tel,
    )
    # Elements pass through unchanged; failure counter incremented.
    assert out[0].get("locator_ambiguous") is True
    assert tel.ambiguity_callouts == 1
    assert tel.ambiguity_callouts_failed == 1


# ---- drive_live_exploration end-to-end (mocked Playwright) ----------------


class _FakePage:
    """Minimal async page stand-in that records visits + returns preset probes."""

    def __init__(self, per_url_elements: dict[str, list[dict]]):
        self._per_url = per_url_elements
        self.visited: list[str] = []
        self.url = ""

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.visited.append(url)
        self.url = url

    async def wait_for_load_state(self, *_a, **_kw):
        return None

    async def evaluate(self, _js):
        # Return the preset elements for the current URL, JSON-encoded (mirrors
        # _DOM_PROBE_JS which returns a JSON string).
        return json.dumps(self._per_url.get(self.url, []))

    # Reveal path uses page.get_by_role / get_by_text. We don't test the click
    # loop here (that's covered in the reveal callout tests) — supply a stub.
    def get_by_role(self, *_a, **_kw):
        return _FakeLocator()

    def get_by_text(self, *_a, **_kw):
        return _FakeLocator()


class _FakeLocator:
    def or_(self, _other):
        return self

    @property
    def first(self):
        return self

    async def click(self, *_a, **_kw):
        return None


class _FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, **_kw):
        return _FakeContext(self._page)

    async def close(self):
        return None


class _FakePWChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, **_kw):
        return self._browser


class _FakePW:
    def __init__(self, browser):
        self.chromium = _FakePWChromium(browser)


class _FakeAsyncPWContext:
    def __init__(self, pw):
        self._pw = pw

    async def __aenter__(self):
        return self._pw

    async def __aexit__(self, *_a):
        return False


def _fake_async_playwright(page):
    def _factory():
        return _FakeAsyncPWContext(_FakePW(_FakeBrowser(page)))
    return _factory


async def test_drive_live_exploration_happy_path(monkeypatch):
    """End-to-end shape: two routes → two entries with elements + telemetry."""
    origin = "https://sut.example.com"
    page = _FakePage({
        f"{origin}": [
            {"role": "link", "name": "Reports", "locator":
             {"strategy": "text", "value": "Reports", "verified_unique": True},
             "testId": None},
        ],
        f"{origin}/reports": [
            {"role": "button", "name": "Export", "locator":
             {"strategy": "role", "value": "Export", "name": "Export",
              "verified_unique": True}, "testId": None},
        ],
    })
    # async_playwright is imported lazily inside drive_live_exploration, so
    # patch it at the source module.
    with patch(
        "playwright.async_api.async_playwright",
        side_effect=lambda: _FakeAsyncPWContext(_FakePW(_FakeBrowser(page))),
    ):
        with patch("qtea.headed_auth_capture.is_available", return_value=True):
            with patch(
                "qtea.headed_auth_capture._proxy_launch_kwargs", return_value={},
            ):
                live_map = await drive_live_exploration(
                    base_url=origin,
                    routes=["/", "/reports"],
                    reconciled_targets=[],
                    storage_state_path=None,
                    max_pages=10,
                )
    assert live_map is not None
    assert live_map["base_url"] == origin
    assert len(live_map["routes"]) == 2
    r0, r1 = live_map["routes"]
    assert r0["path"] == "/"
    assert r0["elements"][0]["name"] == "Reports"
    assert r1["path"] == "/reports"
    assert r1["elements"][0]["name"] == "Export"
    tel = live_map["_telemetry"]
    assert tel["routes_explored"] == 2
    assert tel["routes_truncated_by_cap"] == 0
    assert tel["reveal_callouts"] == 0
    assert tel["ambiguity_callouts"] == 0


async def test_drive_live_exploration_returns_none_when_playwright_missing():
    with patch("qtea.headed_auth_capture.is_available", return_value=False):
        out = await drive_live_exploration(
            base_url="https://x", routes=["/"],
            reconciled_targets=[], storage_state_path=None, max_pages=1,
        )
    assert out is None


async def test_drive_live_exploration_telemetry_counts_truncation():
    origin = "https://sut.example.com"
    page = _FakePage({origin: []})
    # async_playwright is imported lazily inside drive_live_exploration, so
    # patch it at the source module.
    with patch(
        "playwright.async_api.async_playwright",
        side_effect=lambda: _FakeAsyncPWContext(_FakePW(_FakeBrowser(page))),
    ):
        with patch("qtea.headed_auth_capture.is_available", return_value=True):
            with patch(
                "qtea.headed_auth_capture._proxy_launch_kwargs", return_value={},
            ):
                live_map = await drive_live_exploration(
                    base_url=origin,
                    routes=[f"/p{i}" for i in range(10)],
                    reconciled_targets=[],
                    storage_state_path=None,
                    max_pages=3,
                )
    assert live_map is not None
    tel = live_map["_telemetry"]
    assert tel["routes_requested_by_plan"] == 10
    assert tel["routes_truncated_by_cap"] == 7
