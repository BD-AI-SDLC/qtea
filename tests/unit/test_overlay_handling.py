"""Tests for the overlay auto-dismiss parent-side foundation.

Cover heuristic scoring, schema validation, storage-state filter, bug-
candidate reclassifier, dedup, and HITL question construction. Do NOT
touch the runtime template directly — those helpers are tested indirectly
via the shared field-name contract (both sides read/write the same JSONL
shape).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qtea.overlay_handling import (
    CONSENT_COOKIE_PATTERNS,
    DISMISS_RISKY_TOKENS,
    DISMISS_SAFE_TOKENS,
    INTERCEPTORS_SCHEMA_VERSION,
    DismissCandidate,
    Interceptor,
    OverlayEvent,
    RESOLUTION_OVERLAY_BUG,
    RESOLUTION_OVERLAY_ONCE,
    RESOLUTION_OVERLAY_PERSIST,
    append_interceptor,
    build_overlay_question_metadata,
    classify_dismiss_name,
    dedup_overlay_events,
    delete_screenshot,
    filter_already_registered,
    filter_consent_cookies,
    load_interceptors,
    load_overlay_events,
    parse_overlay_answer,
    pick_safe_candidate,
    reclassify_bug_candidates,
    score_candidates,
    write_interceptors,
)


# ---------------------------------------------------------------------------
# Heuristic scoring — the safety-critical piece.
# ---------------------------------------------------------------------------


def test_classify_dismiss_name_safe():
    for name in ("Close", "Dismiss", "×", "Skip", "Not now", "Maybe later"):
        assert classify_dismiss_name(name) == "safe", name


def test_classify_dismiss_name_risky():
    for name in ("Accept all cookies", "Continue to checkout", "OK",
                 "Got it", "I agree", "Proceed"):
        assert classify_dismiss_name(name) == "risky", name


def test_classify_dismiss_name_unknown():
    for name in ("Delete account", "Submit payment", "Login", "Search"):
        assert classify_dismiss_name(name) == "unknown", name


def test_classify_dismiss_name_multi_word_safe():
    # "not now" is a multi-word token; the classifier substrings the
    # normalized name so a full "Not now, thanks" still matches.
    assert classify_dismiss_name("Not now, thanks") == "safe"
    assert classify_dismiss_name("Remind me later") == "safe"


def test_classify_dismiss_name_edge_cases():
    assert classify_dismiss_name("") == "unknown"
    assert classify_dismiss_name("   ") == "unknown"
    # "closest" should NOT match "close" (token boundary, not substring)
    assert classify_dismiss_name("closest button") == "unknown"


def test_score_candidates_prefers_safe_over_risky():
    aom = [
        {"role": "button", "name": "Accept all", "bbox": [0, 0, 100, 30]},
        {"role": "button", "name": "Close", "bbox": [200, 0, 30, 30]},
    ]
    scored = score_candidates(aom, overlay_bbox=(0, 0, 300, 200))
    assert scored, "expected at least one scored candidate"
    # Safe candidate should surface first in tie-breaking.
    top = scored[0]
    assert top.name == "Close"
    assert top.safe is True


def test_score_candidates_top_right_bonus():
    # Button in top-right corner of overlay gets +2 vs same-role in bottom-left.
    aom = [
        {"role": "button", "name": "Close", "bbox": [10, 180, 30, 15]},  # bottom-left
        {"role": "button", "name": "Close", "bbox": [270, 5, 25, 15]},   # top-right
    ]
    scored = score_candidates(aom, overlay_bbox=(0, 0, 300, 200))
    assert scored, "expected candidates"
    # Same name and role — position tiebreak means top-right ranks higher.
    top_scores = sorted(scored, key=lambda c: -c.score)
    assert top_scores[0].bbox is not None
    assert top_scores[0].bbox[0] > 200  # top-right x


def test_pick_safe_candidate_refuses_risky_only():
    # If the ONLY candidates are risky, the heuristic must return None so
    # the parent-side HITL takes over. This is the key safety guardrail.
    aom = [
        {"role": "button", "name": "Accept all", "bbox": [0, 0, 100, 30]},
        {"role": "button", "name": "I agree", "bbox": [110, 0, 100, 30]},
    ]
    scored = score_candidates(aom, overlay_bbox=(0, 0, 300, 200))
    assert scored, "risky candidates should still be scored (for HITL display)"
    assert all(not c.safe for c in scored)
    assert pick_safe_candidate(scored) is None


def test_pick_safe_candidate_picks_safe_when_available():
    aom = [
        {"role": "button", "name": "Accept", "bbox": [0, 0, 100, 30]},
        {"role": "button", "name": "Close", "bbox": [200, 0, 30, 30]},
    ]
    scored = score_candidates(aom, overlay_bbox=(0, 0, 300, 200))
    picked = pick_safe_candidate(scored)
    assert picked is not None
    assert picked.name == "Close"


def test_score_candidates_ignores_non_dismiss_text_nodes():
    # Headings, spans, and other decorative elements should NOT appear as
    # candidates (they inflate the HITL list uselessly).
    aom = [
        {"role": "heading", "name": "Welcome!", "bbox": [10, 10, 200, 30]},
        {"role": "text", "name": "Some paragraph text here", "bbox": [10, 50, 200, 60]},
        {"role": "button", "name": "Close", "bbox": [200, 0, 30, 30]},
    ]
    scored = score_candidates(aom, overlay_bbox=(0, 0, 300, 200))
    names = {c.name for c in scored}
    assert names == {"Close"}


# ---------------------------------------------------------------------------
# Schema validation — supply-chain guardrail.
# ---------------------------------------------------------------------------


def test_load_interceptors_rejects_evaluate_dismiss(tmp_path):
    # This is THE supply-chain vector: a malicious PR tries to smuggle
    # arbitrary JavaScript via {"dismiss": {"kind": "evaluate"}}. Loader
    # must reject it (skip that entry) rather than pass it through.
    path = tmp_path / "interceptors.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "entries": [
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "Attack"},
                "dismiss": {"kind": "evaluate", "script": "fetch('//evil')"},
            },
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "Cookie consent"},
                "dismiss": {
                    "kind": "click",
                    "target": {"kind": "role", "role": "button", "name": "Reject all"},
                },
            },
        ],
    }))
    loaded = load_interceptors(path)
    assert len(loaded) == 1
    assert loaded[0].overlay_name == "Cookie consent"


def test_load_interceptors_rejects_fill_and_goto(tmp_path):
    path = tmp_path / "interceptors.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "entries": [
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "A"},
                "dismiss": {"kind": "fill", "target": {"kind": "role", "role": "input", "name": "x"}},
            },
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "B"},
                "dismiss": {"kind": "goto", "url": "http://evil"},
            },
        ],
    }))
    assert load_interceptors(path) == []


def test_load_interceptors_wrong_schema_version_returns_empty(tmp_path):
    path = tmp_path / "interceptors.json"
    path.write_text(json.dumps({
        "schema_version": 99,
        "entries": [
            {"overlay": {"kind": "role", "role": "dialog", "name": "X"},
             "dismiss": {"kind": "press_escape"}},
        ],
    }))
    assert load_interceptors(path) == []


def test_load_interceptors_missing_file(tmp_path):
    assert load_interceptors(tmp_path / "does-not-exist.json") == []


def test_load_interceptors_malformed_json(tmp_path):
    path = tmp_path / "interceptors.json"
    path.write_text("not json at all {")
    assert load_interceptors(path) == []


def test_load_interceptors_click_missing_target_dropped(tmp_path):
    path = tmp_path / "interceptors.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "entries": [
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "X"},
                "dismiss": {"kind": "click"},  # target missing
            },
        ],
    }))
    assert load_interceptors(path) == []


def test_load_interceptors_press_escape_no_target(tmp_path):
    # press_escape doesn't need a target — this is a valid entry.
    path = tmp_path / "interceptors.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "entries": [
            {
                "overlay": {"kind": "role", "role": "dialog", "name": "Modal"},
                "dismiss": {"kind": "press_escape"},
            },
        ],
    }))
    loaded = load_interceptors(path)
    assert len(loaded) == 1
    assert loaded[0].dismiss_kind == "press_escape"
    assert loaded[0].dismiss_target is None


def test_write_interceptors_round_trip(tmp_path):
    path = tmp_path / "interceptors.json"
    entries = [
        Interceptor(
            overlay_role="dialog",
            overlay_name="Cookie consent",
            dismiss_kind="click",
            dismiss_target={
                "kind": "role", "role": "button",
                "name": "Reject all", "name_op": "equals",
            },
            metadata={"created_by": "hitl"},
        ),
    ]
    write_interceptors(path, entries)
    assert path.exists()
    reloaded = load_interceptors(path)
    assert len(reloaded) == 1
    assert reloaded[0].overlay_name == "Cookie consent"
    assert reloaded[0].dismiss_kind == "click"
    assert reloaded[0].dismiss_target["name"] == "Reject all"


def test_append_interceptor_replaces_existing_by_key(tmp_path):
    path = tmp_path / "interceptors.json"
    a = Interceptor(
        overlay_role="dialog", overlay_name="Cookie consent",
        dismiss_kind="click",
        dismiss_target={"kind": "role", "role": "button", "name": "Accept all"},
    )
    b = Interceptor(  # same overlay, different dismiss (operator changed mind)
        overlay_role="dialog", overlay_name="Cookie consent",
        dismiss_kind="click",
        dismiss_target={"kind": "role", "role": "button", "name": "Reject all"},
    )
    append_interceptor(path, a)
    append_interceptor(path, b)
    loaded = load_interceptors(path)
    assert len(loaded) == 1
    assert loaded[0].dismiss_target["name"] == "Reject all"


def test_append_interceptor_appends_new_key(tmp_path):
    path = tmp_path / "interceptors.json"
    a = Interceptor(
        overlay_role="dialog", overlay_name="Cookie consent",
        dismiss_kind="click",
        dismiss_target={"kind": "role", "role": "button", "name": "Accept"},
    )
    b = Interceptor(  # different overlay
        overlay_role="dialog", overlay_name="What's new",
        dismiss_kind="press_escape",
    )
    append_interceptor(path, a)
    append_interceptor(path, b)
    loaded = load_interceptors(path)
    assert len(loaded) == 2
    names = {e.overlay_name for e in loaded}
    assert names == {"Cookie consent", "What's new"}


# ---------------------------------------------------------------------------
# Storage-state cookie filter (Layer 6).
# ---------------------------------------------------------------------------


def test_filter_consent_cookies_strips_gdpr():
    state = {
        "cookies": [
            {"name": "_gdpr_consent", "domain": ".example.com", "value": "true"},
            {"name": "sessionid", "domain": ".example.com", "value": "abc123"},
            {"name": "OptanonConsent", "domain": ".example.com", "value": "yes"},
        ],
        "origins": [],
    }
    filtered, removed = filter_consent_cookies(state)
    assert removed == 2  # _gdpr_consent (name match), OptanonConsent (via domain? no, name has "consent")
    assert len(filtered["cookies"]) == 1
    assert filtered["cookies"][0]["name"] == "sessionid"


def test_filter_consent_cookies_by_domain():
    # OneTrust widget on its own subdomain — cookie name is generic but
    # domain matches "onetrust", so it's filtered. Cookielaw.org (OneTrust's
    # CDN) also filters because its domain literally contains "cookie".
    state = {
        "cookies": [
            {"name": "visitor_id", "domain": ".cdn.cookielaw.org", "value": "abc"},
            {"name": "session_id", "domain": ".onetrust.com", "value": "xyz"},
            {"name": "app_session", "domain": ".example.com", "value": "keep-me"},
        ],
    }
    filtered, removed = filter_consent_cookies(state)
    # Both consent-CDN cookies stripped; only the app session survives.
    assert removed == 2
    kept_names = {c["name"] for c in filtered["cookies"]}
    assert kept_names == {"app_session"}


def test_filter_consent_cookies_empty_cookies():
    state = {"cookies": []}
    filtered, removed = filter_consent_cookies(state)
    assert removed == 0
    assert filtered["cookies"] == []


def test_filter_consent_cookies_no_cookies_key():
    state = {"origins": []}
    filtered, removed = filter_consent_cookies(state)
    assert removed == 0
    assert filtered.get("cookies") == [] or filtered.get("cookies") is None


def test_filter_consent_cookies_all_patterns_covered():
    # Sanity: each pattern in CONSENT_COOKIE_PATTERNS actually triggers a filter.
    for pat in CONSENT_COOKIE_PATTERNS:
        cookie_name = f"my_{pat}_cookie"
        state = {"cookies": [{"name": cookie_name, "domain": ".x.com", "value": "1"}]}
        _, removed = filter_consent_cookies(state)
        assert removed == 1, f"pattern {pat!r} did not trigger filter"


# ---------------------------------------------------------------------------
# Dedup + already-registered filter.
# ---------------------------------------------------------------------------


def _mk_event(role="dialog", name="Cookie consent", url="https://x.com",
              test_id="tests/test_a.py::test_login", ts="2026-07-01T00:00:00+00:00"):
    return OverlayEvent(
        ts=ts, test_id=test_id, target_intent="LOGIN_BUTTON",
        overlay_role=role, overlay_name=name, page_url=url,
    )


def test_dedup_by_role_name_url_collapses_many_to_one():
    # A cookie banner that intercepts 12 tests in one attempt produces 12
    # events but should surface as ONE HITL prompt.
    events = [_mk_event(test_id=f"t{i}") for i in range(12)]
    deduped = dedup_overlay_events(events)
    assert len(deduped) == 1


def test_dedup_keeps_distinct_role_name_url():
    events = [
        _mk_event(name="Cookie consent"),
        _mk_event(name="What's new"),
        _mk_event(name="Cookie consent", url="https://y.com"),  # different URL
    ]
    deduped = dedup_overlay_events(events)
    assert len(deduped) == 3


def test_dedup_prefers_newest_ts_on_tie():
    events = [
        _mk_event(ts="2026-07-01T00:00:00+00:00", test_id="t_old"),
        _mk_event(ts="2026-07-01T01:00:00+00:00", test_id="t_new"),
    ]
    deduped = dedup_overlay_events(events)
    assert len(deduped) == 1
    assert deduped[0].test_id == "t_new"


def test_filter_already_registered_drops_matches():
    events = [_mk_event(name="Cookie consent"), _mk_event(name="What's new")]
    registered = [
        Interceptor(
            overlay_role="dialog", overlay_name="Cookie consent",
            dismiss_kind="press_escape",
        ),
    ]
    unhandled = filter_already_registered(events, registered)
    assert len(unhandled) == 1
    assert unhandled[0].overlay_name == "What's new"


def test_filter_already_registered_empty_registry():
    events = [_mk_event()]
    unhandled = filter_already_registered(events, [])
    assert len(unhandled) == 1


# ---------------------------------------------------------------------------
# Bug-candidate reclassifier (Layer 4).
# ---------------------------------------------------------------------------


def test_reclassify_bug_candidates_pending_when_not_persisted():
    events = [_mk_event(test_id="tests/test_a.py::test_login",
                        name="Cookie consent")]
    bug_candidates = [
        {"id": "BC-1", "test_id": "tests/test_a.py::test_login",
         "message": "click failed"},
    ]
    out = reclassify_bug_candidates(bug_candidates, events, persisted_after_hitl=False)
    assert out[0]["_type"] == "overlay_pending_hitl"
    assert out[0]["overlay_role"] == "dialog"
    assert out[0]["overlay_name"] == "Cookie consent"


def test_reclassify_bug_candidates_handled_when_persisted():
    events = [_mk_event(test_id="tests/test_a.py::test_login")]
    bug_candidates = [
        {"id": "BC-1", "test_id": "tests/test_a.py::test_login"},
    ]
    out = reclassify_bug_candidates(bug_candidates, events, persisted_after_hitl=True)
    assert out[0]["_type"] == "overlay_handled_next_run"


def test_reclassify_bug_candidates_leaves_unrelated_alone():
    events = [_mk_event(test_id="tests/test_a.py::test_login")]
    bug_candidates = [
        {"id": "BC-1", "test_id": "tests/test_a.py::test_login"},  # matches
        {"id": "BC-2", "test_id": "tests/test_b.py::test_search"}, # unrelated
    ]
    out = reclassify_bug_candidates(bug_candidates, events, persisted_after_hitl=False)
    assert out[0]["_type"] == "overlay_pending_hitl"
    assert "_type" not in out[1]  # untouched


def test_reclassify_bug_candidates_no_events_passthrough():
    bug_candidates = [{"id": "BC-1", "test_id": "tests/test_a.py::test_login"}]
    out = reclassify_bug_candidates(bug_candidates, [])
    assert out == bug_candidates


# ---------------------------------------------------------------------------
# HITL question construction + answer parsing.
# ---------------------------------------------------------------------------


def test_build_overlay_question_metadata_includes_extras():
    ev = _mk_event()
    ev.candidates = [
        {"role": "button", "name": "Close", "bbox": [200, 0, 30, 30]},
        {"role": "button", "name": "Accept all", "bbox": [0, 0, 100, 30]},
    ]
    meta = build_overlay_question_metadata(ev)
    assert meta["type"] == "overlay_dismiss"
    assert meta["overlay_role"] == "dialog"
    assert meta["overlay_name"] == "Cookie consent"
    assert len(meta["candidates"]) == 2
    # Extra options should always be present so the UI can render them.
    extra_kinds = {opt["kind"] for opt in meta["extra_options"]}
    assert extra_kinds == {"press_escape", "custom", "bug"}


def test_parse_overlay_answer_bug_returns_none():
    meta = build_overlay_question_metadata(_mk_event())
    result = parse_overlay_answer(meta, RESOLUTION_OVERLAY_BUG, json.dumps({"kind": "bug"}))
    assert result is None


def test_parse_overlay_answer_press_escape():
    meta = build_overlay_question_metadata(_mk_event())
    result = parse_overlay_answer(
        meta, RESOLUTION_OVERLAY_PERSIST, json.dumps({"kind": "press_escape"}),
    )
    assert result is not None
    assert result.dismiss_kind == "press_escape"
    assert result.dismiss_target is None


def test_parse_overlay_answer_click_candidate():
    ev = _mk_event()
    ev.candidates = [
        {"role": "button", "name": "Close", "bbox": [200, 0, 30, 30]},
    ]
    meta = build_overlay_question_metadata(ev)
    answer = json.dumps({"kind": "click_candidate", "candidate_index": 0})
    result = parse_overlay_answer(meta, RESOLUTION_OVERLAY_PERSIST, answer)
    assert result is not None
    assert result.dismiss_kind == "click"
    assert result.dismiss_target["role"] == "button"
    assert result.dismiss_target["name"] == "Close"


def test_parse_overlay_answer_custom():
    meta = build_overlay_question_metadata(_mk_event())
    answer = json.dumps({"kind": "custom", "role": "link", "name": "Not now"})
    result = parse_overlay_answer(meta, RESOLUTION_OVERLAY_ONCE, answer)
    assert result is not None
    assert result.dismiss_target["role"] == "link"
    assert result.dismiss_target["name"] == "Not now"


def test_parse_overlay_answer_custom_missing_name_returns_none():
    meta = build_overlay_question_metadata(_mk_event())
    answer = json.dumps({"kind": "custom", "role": "button", "name": ""})
    assert parse_overlay_answer(meta, RESOLUTION_OVERLAY_PERSIST, answer) is None


def test_parse_overlay_answer_bad_candidate_index_returns_none():
    ev = _mk_event()
    ev.candidates = [{"role": "button", "name": "Close"}]
    meta = build_overlay_question_metadata(ev)
    answer = json.dumps({"kind": "click_candidate", "candidate_index": 99})
    assert parse_overlay_answer(meta, RESOLUTION_OVERLAY_PERSIST, answer) is None


def test_parse_overlay_answer_malformed_json_returns_none():
    meta = build_overlay_question_metadata(_mk_event())
    assert parse_overlay_answer(meta, RESOLUTION_OVERLAY_PERSIST, "not json") is None


# ---------------------------------------------------------------------------
# JSONL round-trip (runtime writes → parent reads).
# ---------------------------------------------------------------------------


def test_load_overlay_events_reads_runtime_written_format(tmp_path):
    path = tmp_path / "overlay-events.jsonl"
    # Two events + one blank line + one malformed line — parent should
    # skip the corrupt line, keep the two valid entries.
    lines = [
        json.dumps({
            "ts": "2026-07-01T00:00:00+00:00",
            "test_id": "tests/test_a.py::test_login",
            "target_intent": "LOGIN_BUTTON",
            "overlay_role": "dialog",
            "overlay_name": "Cookie consent",
            "page_url": "https://x.com",
            "screenshot_path": str(tmp_path / "ss.png"),
            "overlay_frame": "top",
            "overlay_bbox": [0, 0, 300, 200],
            "heuristic_attempted": True,
            "heuristic_succeeded": False,
            "candidates": [
                {"role": "button", "name": "Close", "safe": True, "score": 6},
            ],
        }),
        "",  # blank line, ignored
        "{not valid json",  # corrupt, skipped with warning
        json.dumps({
            "ts": "2026-07-01T00:01:00+00:00",
            "test_id": "tests/test_b.py::test_x",
            "target_intent": "SEARCH_INPUT",
            "overlay_role": "banner",
            "overlay_name": "What's new",
            "page_url": "https://x.com/y",
        }),
    ]
    path.write_text("\n".join(lines))
    events = load_overlay_events(path)
    assert len(events) == 2
    assert events[0].overlay_role == "dialog"
    assert events[0].overlay_bbox == (0, 0, 300, 200)
    assert events[1].overlay_role == "banner"


def test_load_overlay_events_missing_file():
    assert load_overlay_events(Path("/nonexistent/xxx.jsonl")) == []


# ---------------------------------------------------------------------------
# Screenshot cleanup.
# ---------------------------------------------------------------------------


def test_delete_screenshot_removes_file(tmp_path):
    p = tmp_path / "ss.png"
    p.write_bytes(b"fake png data")
    assert p.exists()
    assert delete_screenshot(str(p)) is True
    assert not p.exists()


def test_delete_screenshot_missing_file_returns_false(tmp_path):
    assert delete_screenshot(str(tmp_path / "does-not-exist.png")) is False


def test_delete_screenshot_empty_path_returns_false():
    assert delete_screenshot("") is False


# ---------------------------------------------------------------------------
# Constants agreement — both sides must import the same tokens.
# ---------------------------------------------------------------------------


def test_dismiss_token_classes_are_disjoint():
    # A token classified as both safe AND risky is a bug — the safety
    # decision would be ambiguous.
    assert not (DISMISS_SAFE_TOKENS & DISMISS_RISKY_TOKENS)


def test_schema_version_constant():
    # If we ever bump the schema version, we need to update the loader
    # in both parent and runtime. This test is a checklist reminder.
    assert INTERCEPTORS_SCHEMA_VERSION == 1
