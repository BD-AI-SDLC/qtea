"""JSON Schema loading + validation helpers."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from worca_t.config import package_resource_root


@lru_cache(maxsize=64)
def load_schema(name: str) -> dict[str, Any]:
    """Load a JSON Schema by short name (e.g. 'bug-reports').

    Looks first in package resource `_resources/schemas/`, then in the dev-tree
    `schemas/` directory. Filename pattern: `<name>.schema.json`.
    """
    filename = f"{name}.schema.json"
    try:
        base = resources.files("worca_t").joinpath("_resources").joinpath("schemas")
        ref = base.joinpath(filename)
        if ref.is_file():
            with ref.open("r", encoding="utf-8") as f:
                return json.load(f)
    except (FileNotFoundError, ModuleNotFoundError, AttributeError, NotADirectoryError):
        pass
    path = package_resource_root() / "schemas" / filename
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"Schema not found: {filename}")


def validate(data: Any, schema_name: str) -> None:
    """Validate `data` against the named schema. Raises jsonschema.ValidationError."""
    schema = load_schema(schema_name)
    jsonschema.validate(data, schema)


def list_schemas() -> list[str]:
    """Discover available schemas in dev-tree (best-effort)."""
    root = package_resource_root() / "schemas"
    if not root.exists():
        return []
    return sorted(p.stem.removesuffix(".schema") for p in root.glob("*.schema.json"))


def is_valid(data: Any, schema_name: str) -> tuple[bool, str | None]:
    """Non-raising validator. Returns (ok, error_message)."""
    try:
        validate(data, schema_name)
    except jsonschema.ValidationError as e:
        return False, e.message
    except FileNotFoundError as e:
        return False, str(e)
    return True, None


def write_validated(path: Path, data: Any, schema_name: str) -> None:
    """Validate then write JSON. Raises if invalid; never writes a bad file."""
    validate(data, schema_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
