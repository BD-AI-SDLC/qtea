"""Tests for the dev-supplied locator file discovery + filtering."""

from __future__ import annotations

import json
from pathlib import Path

from qtea.runtime.dev_locators import (
    DevLocator,
    discover_path,
    load_dev_locators,
)


def _write_dev_file(path: Path, locators: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": "1.0", "source": "test", "locators": locators}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Discovery order: CLI > env > convention path
# ---------------------------------------------------------------------------


def test_discover_path_cli_flag_wins(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    cli_file = tmp_path / "from-cli.json"
    convention = sut / ".qtea" / "dev-locators.json"
    _write_dev_file(cli_file, {"X": {"selector": "#cli"}})
    _write_dev_file(convention, {"X": {"selector": "#convention"}})

    found = discover_path(cli_path=cli_file, sut_root=sut)
    assert found == cli_file


def test_discover_path_env_var_used_when_no_cli(tmp_path: Path, monkeypatch):
    sut = tmp_path / "sut"
    env_file = tmp_path / "from-env.json"
    convention = sut / ".qtea" / "dev-locators.json"
    _write_dev_file(env_file, {"X": {"selector": "#env"}})
    _write_dev_file(convention, {"X": {"selector": "#convention"}})
    monkeypatch.setenv("QTEA_DEV_LOCATORS", str(env_file))

    found = discover_path(cli_path=None, sut_root=sut)
    assert found == env_file


def test_discover_path_convention_used_when_no_cli_or_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    convention = sut / ".qtea" / "dev-locators.json"
    _write_dev_file(convention, {"X": {"selector": "#convention"}})

    found = discover_path(cli_path=None, sut_root=sut)
    assert found == convention


def test_discover_path_returns_none_when_no_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    assert discover_path(cli_path=None, sut_root=tmp_path) is None


def test_discover_path_cli_path_not_existing_falls_through_to_env(tmp_path: Path, monkeypatch):
    """A non-existent CLI path doesn't short-circuit; env wins next."""
    env_file = tmp_path / "from-env.json"
    _write_dev_file(env_file, {"X": {"selector": "#env"}})
    monkeypatch.setenv("QTEA_DEV_LOCATORS", str(env_file))

    found = discover_path(cli_path=tmp_path / "does-not-exist.json", sut_root=None)
    assert found == env_file


# ---------------------------------------------------------------------------
# load_dev_locators — happy path
# ---------------------------------------------------------------------------


def test_load_dev_locators_returns_dev_locator_objects(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    _write_dev_file(sut / ".qtea" / "dev-locators.json", {
        "LOGIN_BUTTON": {
            "selector": "[data-testid='login']",
            "strategy": "data-testid",
            "intent": "primary submit button on the login form",
        },
        "PASSWORD_INPUT": {
            "selector": "input[name='password']",
            "strategy": "css",
        },
    })
    locs, path, warnings = load_dev_locators(sut_root=sut)
    assert path is not None
    assert warnings == []
    assert set(locs.keys()) == {"LOGIN_BUTTON", "PASSWORD_INPUT"}
    assert isinstance(locs["LOGIN_BUTTON"], DevLocator)
    assert locs["LOGIN_BUTTON"].selector == "[data-testid='login']"
    assert locs["LOGIN_BUTTON"].intent == "primary submit button on the login form"


# ---------------------------------------------------------------------------
# load_dev_locators — filtering + warnings
# ---------------------------------------------------------------------------


def test_load_dev_locators_filters_xpath(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    _write_dev_file(sut / ".qtea" / "dev-locators.json", {
        "GOOD": {"selector": "#login"},
        "BAD_XPATH": {"selector": "//button[@id='login']"},
        "BAD_XPATH_PREFIX": {"selector": "xpath=//div"},
    })
    locs, _, warnings = load_dev_locators(sut_root=sut)
    assert set(locs.keys()) == {"GOOD"}
    assert len(warnings) == 2
    assert any("BAD_XPATH" in w for w in warnings)


def test_load_dev_locators_skips_entries_without_selector(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    _write_dev_file(sut / ".qtea" / "dev-locators.json", {
        "GOOD": {"selector": "#x"},
        "NO_SELECTOR": {"intent": "foo"},
        "EMPTY_SELECTOR": {"selector": "   "},
        "NOT_A_DICT": "just a string",
    })
    locs, _, warnings = load_dev_locators(sut_root=sut)
    assert set(locs.keys()) == {"GOOD"}
    assert any("NO_SELECTOR" in w for w in warnings)
    assert any("NOT_A_DICT" in w for w in warnings)


def test_load_dev_locators_malformed_json_returns_empty_with_warning(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    bad = sut / ".qtea" / "dev-locators.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not json{", encoding="utf-8")
    locs, path, warnings = load_dev_locators(sut_root=sut)
    assert locs == {}
    assert path == bad
    assert len(warnings) == 1
    assert "unreadable" in warnings[0]


def test_load_dev_locators_no_file_no_warning(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    locs, path, warnings = load_dev_locators(sut_root=tmp_path)
    assert locs == {}
    assert path is None
    assert warnings == []


def test_load_dev_locators_top_level_locators_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QTEA_DEV_LOCATORS", raising=False)
    sut = tmp_path / "sut"
    bad = sut / ".qtea" / "dev-locators.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")  # no `locators` key
    locs, _, warnings = load_dev_locators(sut_root=sut)
    assert locs == {}
    assert any("no top-level" in w for w in warnings)


# ---------------------------------------------------------------------------
# DevLocator dataclass
# ---------------------------------------------------------------------------


def test_dev_locator_as_dict():
    d = DevLocator(
        constant_name="LOGIN", selector="#x", strategy="id",
        intent="login button", page_url="/login", notes="confirmed",
    ).as_dict()
    assert d == {
        "constant_name": "LOGIN",
        "selector": "#x",
        "strategy": "id",
        "intent": "login button",
        "page_url": "/login",
        "notes": "confirmed",
        "payload": None,
    }
