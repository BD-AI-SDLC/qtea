"""QTea: Fully autonomous QA SDLC orchestrator."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _read_version() -> str:
    # Editable / dev: read directly from pyproject.toml (always current).
    pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    if pyproject.exists():
        with pyproject.open("rb") as f:
            return tomllib.load(f)["project"]["version"]
    # Wheel install: read from installed package metadata.
    try:
        return version("qtea")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _read_version()
__all__ = ["__version__"]
