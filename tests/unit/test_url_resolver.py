"""Unit tests for the SUT base-URL resolver.

The QA-first invariant is the non-negotiable behavior under test: when
multiple URL fields exist, the resolver MUST pick the QA one over staging /
prod / generic, with auth-path consumption as a tiebreaker within rank.
"""

from __future__ import annotations

from pathlib import Path

from worca_t.url_resolver import (
    UrlResolution,
    _role_for_key,
    detect_qa_base_url,
)


def _touch(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _role_for_key — env-key → role rank
# ---------------------------------------------------------------------------


def test_role_qa_url():
    assert _role_for_key("QA_URL")[0] == "qa"


def test_role_qa_base_url():
    assert _role_for_key("QA_BASE_URL")[0] == "qa"


def test_role_staging_url():
    assert _role_for_key("STAGING_URL")[0] == "staging"


def test_role_production_url():
    assert _role_for_key("PRODUCTION_URL")[0] == "production"


def test_role_prod_url():
    assert _role_for_key("PROD_URL")[0] == "prod"


def test_role_generic_base_url():
    assert _role_for_key("BASE_URL")[0] == "base"


def test_role_app_url():
    assert _role_for_key("APP_URL")[0] == "app"


def test_qa_outranks_production():
    qa = _role_for_key("QA_URL")[1]
    prod = _role_for_key("PRODUCTION_URL")[1]
    assert qa < prod, "QA must rank higher (lower number) than production"


def test_qa_outranks_staging():
    qa = _role_for_key("QA_URL")[1]
    stg = _role_for_key("STAGING_URL")[1]
    assert qa < stg


def test_staging_outranks_production():
    stg = _role_for_key("STAGING_URL")[1]
    prod = _role_for_key("PRODUCTION_URL")[1]
    assert stg < prod


# ---------------------------------------------------------------------------
# BaseSettings field discovery: QA-first invariant
# ---------------------------------------------------------------------------


_SETTINGS_QA_AND_PROD = """\
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    qa_url: str = Field(..., alias="QA_URL")
    production_url: str = Field(..., alias="PRODUCTION_URL")
"""


def test_qa_wins_over_production_in_basesettings(tmp_path: Path):
    _touch(tmp_path / "config" / "settings.py", _SETTINGS_QA_AND_PROD)
    res = detect_qa_base_url(tmp_path)
    assert res.key == "QA_URL"
    assert res.source == "basesettings_alias"
    assert res.confidence >= 0.85
    keys = [c.key for c in res.candidates]
    assert keys == ["QA_URL", "PRODUCTION_URL"]


_SETTINGS_THREE_TIERS = """\
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    prod: str = Field(..., alias="PRODUCTION_URL")
    qa: str = Field(..., alias="QA_URL")
    staging: str = Field(..., alias="STAGING_URL")
"""


def test_qa_wins_when_all_three_tiers_present(tmp_path: Path):
    _touch(tmp_path / "settings.py", _SETTINGS_THREE_TIERS)
    res = detect_qa_base_url(tmp_path)
    assert res.key == "QA_URL"


# ---------------------------------------------------------------------------
# Auth-path AST scan: tiebreaker within same role
# ---------------------------------------------------------------------------


_SETTINGS_TWO_QA = """\
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    qa_url_a: str = Field(..., alias="QA_URL")
    qa_url_b: str = Field(..., alias="QA_BACKUP_URL")
"""

_AUTH_USES_QA_A = """\
from settings import settings

def sign_in():
    url = settings.qa_url_a
    return url
"""


def test_auth_consumption_breaks_tie_within_qa(tmp_path: Path):
    _touch(tmp_path / "settings.py", _SETTINGS_TWO_QA)
    _touch(tmp_path / "auth" / "sign_in.py", _AUTH_USES_QA_A)
    res = detect_qa_base_url(tmp_path)
    # Both rank as "qa"; auth-consumed wins.
    # Note: QA_URL and QA_BACKUP_URL both classify as "qa" role under our heuristic.
    assert res.key in ("QA_URL", "QA_BACKUP_URL")
    chosen = next(c for c in res.candidates if c.key == res.key)
    assert chosen.auth_path_consumes is True


# ---------------------------------------------------------------------------
# Literal default extraction
# ---------------------------------------------------------------------------


_SETTINGS_WITH_DEFAULT = """\
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    qa_url: str = Field("https://qa.example.com", alias="QA_URL")
"""


def test_literal_default_surfaces_as_value(tmp_path: Path):
    _touch(tmp_path / "settings.py", _SETTINGS_WITH_DEFAULT)
    res = detect_qa_base_url(tmp_path)
    assert res.key == "QA_URL"
    assert res.value == "https://qa.example.com"


# ---------------------------------------------------------------------------
# Fallback probes (no BaseSettings URL fields present)
# ---------------------------------------------------------------------------


def test_fallback_to_package_json_dev_script(tmp_path: Path):
    _touch(
        tmp_path / "package.json",
        '{"scripts": {"dev": "next dev --port 4000"}}',
    )
    res = detect_qa_base_url(tmp_path)
    assert res.value == "http://localhost:4000"
    assert res.source == "package_json_script"


def test_fallback_to_next_config(tmp_path: Path):
    _touch(
        tmp_path / "next.config.js",
        "module.exports = { server: { port: 5000 } }",
    )
    res = detect_qa_base_url(tmp_path)
    assert res.value == "http://localhost:5000"
    assert res.source == "framework_config"


def test_fallback_to_docker_compose(tmp_path: Path):
    _touch(
        tmp_path / "docker-compose.yml",
        'services:\n  web:\n    ports:\n      - "8088:80"\n',
    )
    res = detect_qa_base_url(tmp_path)
    assert res.value == "http://localhost:8088"
    assert res.source == "docker_compose"


def test_fallback_to_readme_localhost_url(tmp_path: Path):
    _touch(
        tmp_path / "README.md",
        "Start the dev server. It listens on http://localhost:9000.",
    )
    res = detect_qa_base_url(tmp_path)
    assert res.value == "http://localhost:9000"
    assert res.source == "readme"


def test_no_signals_yields_empty_resolution(tmp_path: Path):
    res = detect_qa_base_url(tmp_path)
    assert res.key is None
    assert res.value is None
    assert "no URL signal anywhere in SUT" in res.trail[-1]


def test_missing_path_returns_empty(tmp_path: Path):
    res = detect_qa_base_url(tmp_path / "does-not-exist")
    assert res.key is None
    assert isinstance(res, UrlResolution)


# ---------------------------------------------------------------------------
# Production-only safety net
# ---------------------------------------------------------------------------


_SETTINGS_ONLY_PROD = """\
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    production_url: str = Field(..., alias="PRODUCTION_URL")
"""


def test_only_production_url_has_low_confidence_and_warns(tmp_path: Path):
    _touch(tmp_path / "settings.py", _SETTINGS_ONLY_PROD)
    res = detect_qa_base_url(tmp_path)
    assert res.key == "PRODUCTION_URL"
    assert res.confidence <= 0.2  # very low, user must confirm
    assert any("production-role" in t for t in res.trail)
