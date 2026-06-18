"""Dev-supplied locator file: discovery, validation, lookup.

This module is the canonical implementation used by the worca-t pipeline.
A reduced copy of the same logic is vendored into the SUT alongside the
pytest plugin so the runtime can consult dev-locators even when the
``worca_t`` package isn't importable from the SUT's interpreter.

Discovery order (first match that resolves to a readable, schema-valid
file wins):

  1. ``cli_path`` argument                (parent-worca → CLI flag)
  2. ``$WORCA_T_DEV_LOCATORS``            (parent-worca → env var)
  3. ``<sut_root>/.worca-t/dev-locators.json``  (parent-worca → convention path)

When no file resolves, :func:`load_dev_locators` returns an empty mapping
and the runtime falls through to the LLM resolver exactly as today.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Conventional location relative to the SUT root.
CONVENTION_REL_PATH = ".worca-t/dev-locators.json"
ENV_VAR = "WORCA_T_DEV_LOCATORS"


@dataclass(frozen=True)
class DevLocator:
    """One dev-supplied entry. Mirrors the schema's locators[].* shape."""

    constant_name: str
    selector: str
    strategy: str | None = None
    intent: str | None = None
    page_url: str | None = None
    notes: str | None = None

    def as_dict(self) -> dict:
        return {
            "constant_name": self.constant_name,
            "selector": self.selector,
            "strategy": self.strategy,
            "intent": self.intent,
            "page_url": self.page_url,
            "notes": self.notes,
        }


# Selectors starting with these tokens are XPath — rejected at load time.
_XPATH_TOKENS = ("//", "xpath=")


def _is_xpath(selector: str) -> bool:
    s = (selector or "").strip()
    return any(s.startswith(tok) for tok in _XPATH_TOKENS) or "By.XPATH" in s


def discover_path(
    *,
    cli_path: str | os.PathLike | None = None,
    sut_root: Path | None = None,
) -> Path | None:
    """Resolve the dev-locators file path using the three-tier discovery.

    Returns ``None`` when no candidate exists on disk. Does NOT load or
    validate — call :func:`load_dev_locators` for the full pipeline.
    """
    if cli_path:
        p = Path(cli_path)
        if p.is_file():
            return p
    env_val = os.environ.get(ENV_VAR)
    if env_val:
        p = Path(env_val)
        if p.is_file():
            return p
    if sut_root:
        p = Path(sut_root) / CONVENTION_REL_PATH
        if p.is_file():
            return p
    return None


def load_dev_locators(
    *,
    cli_path: str | os.PathLike | None = None,
    sut_root: Path | None = None,
) -> tuple[dict[str, DevLocator], Path | None, list[str]]:
    """Load dev-supplied locators using the discovery chain.

    Returns ``(locators, source_path, warnings)``:
      - ``locators`` — mapping ``{constant_name: DevLocator}``. Empty when
        no file is found, the file is unreadable, or the schema-level
        validation removes every entry.
      - ``source_path`` — the path that won discovery, or ``None``.
      - ``warnings`` — human-readable strings about entries that were
        dropped (XPath selectors, malformed entries). Caller logs them.

    XPath entries are filtered out at load time — the dev file cannot
    smuggle XPath past the locator-priority gate.
    """
    warnings: list[str] = []
    path = discover_path(cli_path=cli_path, sut_root=sut_root)
    if path is None:
        return {}, None, warnings

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        warnings.append(f"dev-locators file at {path} is unreadable/invalid JSON: {e}")
        return {}, path, warnings

    locators_block = raw.get("locators") if isinstance(raw, dict) else None
    if not isinstance(locators_block, dict):
        warnings.append(f"dev-locators file at {path} has no top-level `locators` object")
        return {}, path, warnings

    out: dict[str, DevLocator] = {}
    for name, entry in locators_block.items():
        if not isinstance(entry, dict):
            warnings.append(f"dev-locators[{name}] is not an object; skipping")
            continue
        selector = entry.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            warnings.append(f"dev-locators[{name}] missing selector; skipping")
            continue
        if _is_xpath(selector):
            warnings.append(
                f"dev-locators[{name}] selector is XPath"
                f" ({selector!r}); rejected per locator-priority gate"
            )
            continue
        out[name] = DevLocator(
            constant_name=name,
            selector=selector.strip(),
            strategy=entry.get("strategy") if isinstance(entry.get("strategy"), str) else None,
            intent=entry.get("intent") if isinstance(entry.get("intent"), str) else None,
            page_url=entry.get("page_url") if isinstance(entry.get("page_url"), str) else None,
            notes=entry.get("notes") if isinstance(entry.get("notes"), str) else None,
        )
    return out, path, warnings
