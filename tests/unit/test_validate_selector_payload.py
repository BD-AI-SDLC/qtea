"""Tests for `validate_selector_payload` and `parse_resolver_payload`.

These guard the cache-write and TBD-promotion paths in `jit_resolver.py`.
The specific regression: in run 20260621-213751-ee0fef the LLM returned
`{"strategy": "role", "selector": "link \"Go to Gemini Enterprise\""}` —
Playwright AOM debug-print syntax that is neither valid CSS nor a valid
Playwright engine selector. The string flowed through `is_unsafe_selector`
(which only blocks injection markers), got cached, then was promoted into
SUT source where Playwright's CSS parser blew up. The validator below is
the structural fix.
"""

from __future__ import annotations

import pytest

from qtea.jit_resolver import (
    _PAYLOAD_KINDS,
    parse_resolver_payload,
    validate_selector_payload,
)


# ---------------------------------------------------------------------------
# validate_selector_payload: string-only path (back-compat, no payload)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sel", [
    "#login-button",
    "[data-testid='submit']",
    '[data-testid="submit"]',
    ".css-1abc2de",
    "div.card > button.primary",
    "role=button[name=\"Sign in\"]",
    "text=Hello",
    "css=#submit",
    "label=Email",
    "[role='dialog'] button",
    "ul > li:nth-child(2)",
])
def test_validate_accepts_valid_string_selectors(sel):
    ok, why = validate_selector_payload(None, sel)
    assert ok, f"expected accept, got reject ({why}) for {sel!r}"


@pytest.mark.parametrize("sel,reason_match", [
    # The exact regression from run 20260621.
    ('link "Go to Gemini Enterprise"', "debug syntax"),
    ('button "Sign in"', "debug syntax"),
    ('textbox "Email"', "debug syntax"),
    ('LINK "Foo"', "debug syntax"),  # case-insensitive
    # Injection markers.
    ("<script>alert(1)</script>", "injection"),
    ("javascript:void(0)", "injection"),
    ("line1\nline2", "injection"),
    # XPath rejection.
    ("//div[@id='x']", "XPath"),
    ("xpath=//button", "XPath"),
    # Structural rejections.
    ("", "empty"),
    ("   ", "empty"),
    ("[unbalanced", "square brackets"),
    ("a[role='b'", "square brackets"),
    ("input(", "parentheses"),
    # Brackets are checked before quotes; this hits the bracket gate first.
    ('div[name="unclosed', "square brackets"),
    # Pure-quote imbalance (no bracket / paren issue).
    ('a[name="unbalanced]', "double quotes"),
    ("a[name='unbalanced]", "single quotes"),
])
def test_validate_rejects_bad_string_selectors(sel, reason_match):
    ok, why = validate_selector_payload(None, sel)
    assert not ok, f"expected reject for {sel!r}, got accept"
    assert reason_match.lower() in (why or "").lower(), (
        f"reason {why!r} should mention {reason_match!r}"
    )


# ---------------------------------------------------------------------------
# validate_selector_payload: structured payload path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "role", "role": "link"},
    {"kind": "role", "role": "button", "name": "Sign in"},
    {"kind": "role", "role": "textbox", "name": "Email", "exact": True},
    {"kind": "text", "text": "Submit"},
    {"kind": "text", "text": "Hello world", "exact": False},
    {"kind": "label", "text": "Email"},
    {"kind": "placeholder", "text": "Enter your name"},
    {"kind": "test_id", "value": "submit-button"},
    {"kind": "css", "selector": "#submit"},
    {"kind": "css", "selector": "[data-testid='login']"},
])
def test_validate_accepts_valid_structured_payloads(payload):
    ok, why = validate_selector_payload(payload, None)
    assert ok, f"expected accept for {payload!r}, got reject ({why})"


@pytest.mark.parametrize("payload,reason_match", [
    # Unknown kind.
    ({"kind": "xpath", "selector": "//div"}, "kind"),
    ({"kind": "selector", "value": "x"}, "kind"),
    ({"kind": None}, "kind"),
    # Wrong type.
    ("not a dict", "must be dict"),
    # Role missing required field.
    ({"kind": "role"}, "requires non-empty 'role'"),
    ({"kind": "role", "role": ""}, "requires non-empty 'role'"),
    ({"kind": "role", "role": "   "}, "requires non-empty 'role'"),
    # Role with empty name (when present, must be non-empty).
    ({"kind": "role", "role": "link", "name": ""}, "non-empty string"),
    # Text/label/placeholder missing text.
    ({"kind": "text"}, "requires non-empty 'text'"),
    ({"kind": "label", "text": ""}, "requires non-empty 'text'"),
    ({"kind": "placeholder", "value": "x"}, "requires non-empty 'text'"),
    # test_id missing value.
    ({"kind": "test_id"}, "requires non-empty 'value'"),
    # css payload with bad selector content.
    ({"kind": "css", "selector": 'link "x"'}, "debug syntax"),
    ({"kind": "css", "selector": ""}, "non-empty"),
    ({"kind": "css", "selector": "javascript:void(0)"}, "unsafe"),
])
def test_validate_rejects_bad_structured_payloads(payload, reason_match):
    ok, why = validate_selector_payload(payload, None)
    assert not ok, f"expected reject for {payload!r}, got accept"
    assert reason_match.lower() in (why or "").lower(), (
        f"reason {why!r} should mention {reason_match!r}"
    )


# ---------------------------------------------------------------------------
# parse_resolver_payload: LLM response → canonical cache shape
# ---------------------------------------------------------------------------


def test_parse_structured_role_with_name():
    out = parse_resolver_payload({
        "kind": "role",
        "role": "link",
        "name": "Go to Gemini Enterprise",
        "confidence": 0.95,
        "reason": "matches link in nav",
    })
    assert out["payload"] == {
        "kind": "role",
        "role": "link",
        "name": "Go to Gemini Enterprise",
    }
    assert out["strategy"] == "role"
    assert out["confidence"] == 0.95
    assert out["reason"] == "matches link in nav"
    # Derived telemetry selector is Playwright engine form (safe via locator()).
    assert out["selector"] == 'role=link[name="Go to Gemini Enterprise"]'


def test_parse_structured_role_without_name():
    out = parse_resolver_payload({"kind": "role", "role": "button"})
    assert out["payload"] == {"kind": "role", "role": "button"}
    assert out["selector"] == "role=button"


def test_parse_structured_role_with_exact():
    out = parse_resolver_payload({
        "kind": "role", "role": "tab", "name": "Settings", "exact": True,
    })
    assert out["payload"]["exact"] is True


def test_parse_structured_text():
    out = parse_resolver_payload({"kind": "text", "text": "Submit", "confidence": 0.8})
    assert out["payload"] == {"kind": "text", "text": "Submit"}
    assert out["selector"] == "text=Submit"
    assert out["strategy"] == "text"


def test_parse_structured_test_id():
    out = parse_resolver_payload({"kind": "test_id", "value": "submit-btn"})
    assert out["payload"] == {"kind": "test_id", "value": "submit-btn"}
    assert out["selector"] == '[data-testid="submit-btn"]'


def test_parse_structured_css_normalises_whitespace():
    out = parse_resolver_payload({"kind": "css", "selector": "  #submit  "})
    assert out["payload"] == {"kind": "css", "selector": "#submit"}
    assert out["selector"] == "#submit"
    assert out["strategy"] is None  # css uses string path


def test_parse_legacy_string_form():
    """A model that regresses to the pre-structured prompt shape still works."""
    out = parse_resolver_payload({
        "selector": "[data-testid='login']",
        "strategy": "data-testid",
        "confidence": 0.9,
    })
    assert out["payload"] is None
    assert out["selector"] == "[data-testid='login']"
    assert out["strategy"] == "data-testid"


def test_parse_rejects_missing_both_kind_and_selector():
    with pytest.raises(ValueError, match="neither 'kind' nor"):
        parse_resolver_payload({"strategy": "role"})


def test_parse_rejects_non_dict():
    with pytest.raises(ValueError, match="must be dict"):
        parse_resolver_payload("not a dict")  # type: ignore[arg-type]


def test_parse_rejects_role_kind_missing_role_field():
    with pytest.raises(ValueError, match="requires non-empty 'role'"):
        parse_resolver_payload({"kind": "role"})


def test_parse_rejects_text_kind_missing_text_field():
    with pytest.raises(ValueError, match="requires non-empty 'text'"):
        parse_resolver_payload({"kind": "label"})


# ---------------------------------------------------------------------------
# Integration: parse → validate round-trip
# ---------------------------------------------------------------------------


def test_parsed_structured_payloads_pass_validation():
    """Anything `parse_resolver_payload` produces must pass `validate_selector_payload`."""
    raw_entries = [
        {"kind": "role", "role": "link", "name": "x"},
        {"kind": "text", "text": "Hello"},
        {"kind": "test_id", "value": "submit"},
        {"kind": "css", "selector": "#go"},
        {"selector": "#submit", "strategy": "id"},
    ]
    for raw in raw_entries:
        out = parse_resolver_payload(raw)
        ok, why = validate_selector_payload(out["payload"], out["selector"])
        assert ok, f"parse→validate round-trip failed for {raw!r}: {why}"


def test_payload_kinds_constant_matches_validator():
    """`_PAYLOAD_KINDS` is the single source of truth — validator must accept all."""
    samples = {
        "role": {"kind": "role", "role": "link"},
        "text": {"kind": "text", "text": "x"},
        "label": {"kind": "label", "text": "x"},
        "placeholder": {"kind": "placeholder", "text": "x"},
        "test_id": {"kind": "test_id", "value": "x"},
        "css": {"kind": "css", "selector": "#x"},
    }
    for kind in _PAYLOAD_KINDS:
        assert kind in samples, f"_PAYLOAD_KINDS contains {kind!r} but test has no sample"
        ok, why = validate_selector_payload(samples[kind], None)
        assert ok, f"validator rejected kind={kind!r}: {why}"
