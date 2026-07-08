"""Step 7 pre-codegen live-exploration tests (live-map rendering)."""

from __future__ import annotations

from qtea.steps.s07_live_explore import (
    _extract_routes,
    _parse_live_map,
    render_live_map_for_prompt,
)


def test_render_distinguishes_auth_required_from_missing():
    """A login-gated route (auth_required) must be rendered as EXISTING and
    NOT as missing, so the architect plans it normally; only a genuine 404
    route is flagged as absent."""
    live_map = {
        "base_url": "https://qa.example.com",
        "routes": [
            {"path": "/", "exists": True, "auth_required": False,
             "notable_roles": ["button: Sign in"]},
            {"path": "/dashboard", "exists": False, "auth_required": True,
             "redirected_to": "https://qa.example.com/login"},
            {"path": "/nope", "exists": False, "auth_required": False},
        ],
    }
    out = render_live_map_for_prompt(live_map)

    # Auth-gated route: present, explicitly "not missing".
    assert "/dashboard" in out
    assert "behind login/SSO" in out
    assert "do NOT treat as missing" in out
    # Only the genuine 404 is flagged absent.
    assert "`/nope` — DOES NOT EXIST" in out
    assert "`/dashboard` — DOES NOT EXIST" not in out
    # Rendered app page keeps its observed roles.
    assert "button: Sign in" in out


def test_render_empty_when_no_routes():
    assert render_live_map_for_prompt(None) == ""
    assert render_live_map_for_prompt({"routes": []}) == ""
    assert render_live_map_for_prompt({"nope": 1}) == ""


def test_parse_live_map_accepts_auth_required_field():
    raw = (
        '{"base_url": "https://qa.example.com", "routes": ['
        '{"path": "/x", "exists": false, "auth_required": true, '
        '"redirected_to": "https://qa.example.com/login", "notable_roles": []}]}'
    )
    parsed = _parse_live_map(raw)
    assert parsed is not None
    assert parsed["routes"][0]["auth_required"] is True


def test_parse_live_map_rejects_non_route_shapes():
    assert _parse_live_map("not json") is None
    assert _parse_live_map('{"routes": "x"}') is None


def test_extract_routes_stays_on_origin_and_includes_root():
    strategy = (
        "Navigate to https://qa.example.com/ropa/create and then to "
        "https://evil.example.net/steal — also /directory/list is referenced."
    )
    routes = _extract_routes(strategy, "https://qa.example.com", max_routes=12)
    assert "/" in routes
    assert "/ropa/create" in routes
    assert "/directory/list" in routes
    # Off-origin URL is dropped (never navigate off-origin while authenticated).
    assert not any("evil" in r for r in routes)
