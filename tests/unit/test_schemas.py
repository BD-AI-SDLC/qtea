"""Tests for schema validation helpers."""

import json

import pytest

from worca_t import schemas


def test_load_schema_returns_dict():
    s = schemas.load_schema("refined-spec")
    assert s["title"] == "RefinedSpec"
    assert "requirement_id" in s["required"]


def test_load_schema_missing_raises():
    with pytest.raises(FileNotFoundError):
        schemas.load_schema("does-not-exist-xyz")


def test_validate_accepts_minimal_valid_refined_spec():
    data = {
        "requirement_id": "REQ-login",
        "title": "Login",
        "sections": [],
        "acceptance_criteria": ["can sign in"],
    }
    schemas.validate(data, "refined-spec")
    ok, err = schemas.is_valid(data, "refined-spec")
    assert ok and err is None


def test_validate_rejects_bad_requirement_id_pattern():
    data = {
        "requirement_id": "not-prefixed",
        "title": "x",
        "sections": [],
        "acceptance_criteria": [],
    }
    ok, err = schemas.is_valid(data, "refined-spec")
    assert not ok and err is not None


def test_research_schema_accepts_minimal():
    data = {"title": "research", "sections": []}
    ok, err = schemas.is_valid(data, "research")
    assert ok, err


def test_write_validated_writes_or_raises(tmp_path):
    good = {
        "requirement_id": "REQ-x",
        "title": "x",
        "sections": [],
        "acceptance_criteria": [],
    }
    out = tmp_path / "spec.json"
    schemas.write_validated(out, good, "refined-spec")
    assert json.loads(out.read_text(encoding="utf-8"))["requirement_id"] == "REQ-x"

    bad = {"title": "x"}
    bad_out = tmp_path / "bad.json"
    with pytest.raises(Exception):
        schemas.write_validated(bad_out, bad, "refined-spec")
    assert not bad_out.exists()


def test_list_schemas_includes_known():
    names = schemas.list_schemas()
    # In dev tree, at minimum these should be present:
    assert "refined-spec" in names
    assert "research" in names
    assert "bug-reports" in names
