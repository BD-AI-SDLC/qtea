"""Tests for the TBD promotion gate in `s09_execute._promote_resolved_tbds`.

After run-20260621 the promoter must reject any cache entry that:
  - has no `passing_witnesses` (test that used it must have passed)
  - fails `validate_selector_payload` (e.g. Playwright debug-print syntax)
  - has an unrepresentable payload (shouldn't happen — defence in depth)

When it accepts, structured payloads emit `role_locator(...)` / `text_locator(...)`
calls, not raw strings. The legacy CSS-string path still works.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qtea.steps.s09_execute import (
    _ensure_runtime_imports,
    _format_promoted_substitution,
    _promote_resolved_tbds,
)


# ---------------------------------------------------------------------------
# _format_promoted_substitution: payload → Python expression
# ---------------------------------------------------------------------------


def test_format_css_payload_emits_json_string():
    out = _format_promoted_substitution(
        {"kind": "css", "selector": "[data-testid='login']"}, None,
    )
    assert out == '"[data-testid=\'login\']"'


def test_format_no_payload_emits_json_string():
    out = _format_promoted_substitution(None, "#submit")
    assert out == '"#submit"'


def test_format_role_payload_with_name():
    out = _format_promoted_substitution(
        {"kind": "role", "role": "link", "name": "Go to Gemini Enterprise"}, None,
    )
    assert out == 'role_locator("link", name="Go to Gemini Enterprise")'


def test_format_role_payload_with_exact():
    out = _format_promoted_substitution(
        {"kind": "role", "role": "tab", "name": "Settings", "exact": True}, None,
    )
    assert out == 'role_locator("tab", name="Settings", exact=True)'


def test_format_role_payload_without_name():
    out = _format_promoted_substitution({"kind": "role", "role": "button"}, None)
    assert out == 'role_locator("button")'


def test_format_text_label_placeholder():
    assert _format_promoted_substitution({"kind": "text", "text": "Submit"}, None) \
        == 'text_locator("Submit")'
    assert _format_promoted_substitution({"kind": "label", "text": "Email"}, None) \
        == 'label_locator("Email")'
    assert _format_promoted_substitution(
        {"kind": "placeholder", "text": "Search...", "exact": True}, None,
    ) == 'placeholder_locator("Search...", exact=True)'


def test_format_test_id():
    out = _format_promoted_substitution({"kind": "test_id", "value": "submit-btn"}, None)
    assert out == 'test_id_locator("submit-btn")'


def test_format_role_payload_missing_role_returns_none():
    assert _format_promoted_substitution({"kind": "role"}, None) is None


def test_format_unknown_kind_returns_none():
    assert _format_promoted_substitution({"kind": "xpath", "value": "//div"}, None) is None


# ---------------------------------------------------------------------------
# _ensure_runtime_imports: extend the `from tests.qtea_runtime import …` line
# ---------------------------------------------------------------------------


def test_ensure_runtime_imports_adds_new_helpers():
    text = (
        "from tests.qtea_runtime import tbd\n"
        "from typing import ClassVar\n"
    )
    out = _ensure_runtime_imports(text, {"role_locator", "text_locator"})
    assert "from tests.qtea_runtime import role_locator, tbd, text_locator" in out


def test_ensure_runtime_imports_is_noop_when_already_present():
    text = "from tests.qtea_runtime import tbd, role_locator\n"
    out = _ensure_runtime_imports(text, {"role_locator"})
    assert out == text


def test_ensure_runtime_imports_is_noop_when_no_import_line():
    text = "from somewhere.else import foo\n"
    out = _ensure_runtime_imports(text, {"role_locator"})
    assert out == text


# ---------------------------------------------------------------------------
# _promote_resolved_tbds: end-to-end gating
# ---------------------------------------------------------------------------


def _make_sut(tmp_path: Path, pom_text: str) -> Path:
    """Create a minimal SUT tree with one locators file."""
    sut = tmp_path / "sut"
    (sut / "pages" / "locators").mkdir(parents=True, exist_ok=True)
    (sut / "pages" / "locators" / "chat_page_locators.py").write_text(
        pom_text, encoding="utf-8",
    )
    return sut


def _write_cache(tmp_path: Path, entries: list[dict]) -> Path:
    cache_path = tmp_path / "locator-cache.json"
    cache_path.write_text(
        json.dumps({"run_id": "test", "entries": entries}, indent=2),
        encoding="utf-8",
    )
    return cache_path


def test_promote_witnessed_css_substitutes_string(tmp_path):
    pom = (
        "from tests.qtea_runtime import tbd\n"
        "class L:\n"
        "    def __init__(self):\n"
        "        self.X = tbd('login button')\n"
    )
    sut = _make_sut(tmp_path, pom)
    cache = _write_cache(tmp_path, [{
        "key": "k1",
        "intent": "login button",
        "constant_name": "X",
        "selector": "[data-testid='login']",
        "payload": None,
        "source": "agent",
        "passing_witnesses": ["tests/x.py::test_y"],
    }])
    modified, blocked = _promote_resolved_tbds(sut, cache)
    assert blocked == []
    assert modified
    text = (sut / "pages" / "locators" / "chat_page_locators.py").read_text(encoding="utf-8")
    assert "tbd('login button')" not in text
    assert '"[data-testid=\'login\']"' in text


def test_promote_witnessed_role_emits_role_locator_call(tmp_path):
    pom = (
        "from tests.qtea_runtime import tbd\n"
        "class L:\n"
        "    def __init__(self):\n"
        "        self.GEMINI = tbd('Go to Gemini Enterprise side nav button')\n"
    )
    sut = _make_sut(tmp_path, pom)
    cache = _write_cache(tmp_path, [{
        "key": "k1",
        "intent": "Go to Gemini Enterprise side nav button",
        "constant_name": "GEMINI",
        "selector": 'role=link[name="Go to Gemini Enterprise"]',
        "payload": {
            "kind": "role", "role": "link", "name": "Go to Gemini Enterprise",
        },
        "source": "agent",
        "passing_witnesses": ["tests/x.py::test_gemini"],
    }])
    modified, blocked = _promote_resolved_tbds(sut, cache)
    assert blocked == []
    assert modified
    text = (sut / "pages" / "locators" / "chat_page_locators.py").read_text(encoding="utf-8")
    assert 'role_locator("link", name="Go to Gemini Enterprise")' in text
    # The runtime import line gained `role_locator`.
    assert "from tests.qtea_runtime import role_locator, tbd" in text


def test_unwitnessed_entry_is_blocked(tmp_path):
    pom = (
        "from tests.qtea_runtime import tbd\n"
        "class L:\n"
        "    def __init__(self):\n"
        "        self.X = tbd('foo')\n"
    )
    sut = _make_sut(tmp_path, pom)
    cache = _write_cache(tmp_path, [{
        "key": "k1",
        "intent": "foo",
        "constant_name": "X",
        "selector": "#x",
        "payload": None,
        "source": "agent",
        # No passing_witnesses.
    }])
    modified, blocked = _promote_resolved_tbds(sut, cache)
    assert modified == []
    assert len(blocked) == 1
    assert blocked[0]["kind"] == "promotion-blocked"
    assert blocked[0]["reason"] == "no_passing_witness"
    # The POM stays unchanged.
    text = (sut / "pages" / "locators" / "chat_page_locators.py").read_text(encoding="utf-8")
    assert "tbd('foo')" in text


def test_malformed_selector_is_blocked(tmp_path):
    """The exact regression from run-20260621: cached selector is Playwright
    debug-print syntax, not valid CSS. The validator catches it; the POM is
    left alone; a bug-candidate is emitted."""
    pom = (
        "from tests.qtea_runtime import tbd\n"
        "class L:\n"
        "    def __init__(self):\n"
        "        self.X = tbd('Gemini button')\n"
    )
    sut = _make_sut(tmp_path, pom)
    cache = _write_cache(tmp_path, [{
        "key": "k1",
        "intent": "Gemini button",
        "constant_name": "X",
        "selector": 'link "Go to Gemini Enterprise"',
        "payload": None,
        "source": "agent",
        "passing_witnesses": ["tests/x.py::test_y"],  # witnessed!
    }])
    modified, blocked = _promote_resolved_tbds(sut, cache)
    assert modified == []
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "invalid_selector_form"
    assert "debug syntax" in blocked[0]["validation_reason"].lower()
    text = (sut / "pages" / "locators" / "chat_page_locators.py").read_text(encoding="utf-8")
    assert "tbd('Gemini button')" in text


def test_mixed_witnessed_and_unwitnessed(tmp_path):
    """One entry promotes, the other is blocked — both reported correctly."""
    pom = (
        "from tests.qtea_runtime import tbd\n"
        "class L:\n"
        "    def __init__(self):\n"
        "        self.A = tbd('alpha')\n"
        "        self.B = tbd('beta')\n"
    )
    sut = _make_sut(tmp_path, pom)
    cache = _write_cache(tmp_path, [
        {"key": "k1", "intent": "alpha", "constant_name": "A",
         "selector": "#a", "payload": None, "source": "agent",
         "passing_witnesses": ["t::p"]},
        {"key": "k2", "intent": "beta", "constant_name": "B",
         "selector": "#b", "payload": None, "source": "agent"},
    ])
    modified, blocked = _promote_resolved_tbds(sut, cache)
    assert modified  # alpha was rewritten
    assert len(blocked) == 1
    assert blocked[0]["intent"] == "beta"
    text = (sut / "pages" / "locators" / "chat_page_locators.py").read_text(encoding="utf-8")
    assert 'self.A = "#a"' in text
    assert "tbd('beta')" in text


def test_promote_handles_missing_cache(tmp_path):
    sut = _make_sut(tmp_path, "x = 1\n")
    modified, blocked = _promote_resolved_tbds(sut, tmp_path / "missing.json")
    assert modified == []
    assert blocked == []
