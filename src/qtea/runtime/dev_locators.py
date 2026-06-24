"""Dev-supplied locator file: discovery, validation, lookup.

This module is the canonical implementation used by the qtea pipeline.
A reduced copy of the same logic is vendored into the SUT alongside the
pytest plugin so the runtime can consult dev-locators even when the
``qtea`` package isn't importable from the SUT's interpreter.

Discovery order (first match that resolves to a readable, schema-valid
file wins):

  1. ``cli_path`` argument                (parent-qtea → CLI flag)
  2. ``$QTEA_DEV_LOCATORS``            (parent-qtea → env var)
  3. ``<sut_root>/.qtea/dev-locators.json``  (parent-qtea → convention path)

When no file resolves, :func:`load_dev_locators` returns an empty mapping
and the runtime falls through to the LLM resolver exactly as today.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Conventional location relative to the SUT root.
CONVENTION_REL_PATH = ".qtea/dev-locators.json"
ENV_VAR = "QTEA_DEV_LOCATORS"


@dataclass(frozen=True)
class DevLocator:
    """One dev-supplied entry. Mirrors the schema's locators[].* shape.

    ``payload`` is the structured form for role/text/label/placeholder/test_id
    locators (introduced after the run-20260621 regression where the LLM
    cached `link "Go to Gemini Enterprise"` as a CSS string). When ``payload``
    is set, the runtime calls ``page.get_by_role(...)`` etc. at action time
    instead of ``page.locator(selector)``. ``selector`` is still required
    (carried for telemetry / back-compat readers) but is not used at action
    time when ``payload`` is present.
    """

    constant_name: str
    selector: str
    strategy: str | None = None
    intent: str | None = None
    page_url: str | None = None
    notes: str | None = None
    payload: dict | None = None

    def as_dict(self) -> dict:
        return {
            "constant_name": self.constant_name,
            "selector": self.selector,
            "strategy": self.strategy,
            "intent": self.intent,
            "page_url": self.page_url,
            "notes": self.notes,
            "payload": self.payload,
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

    # Lazy import — keeps this module importable inside the vendored runtime
    # where `qtea` isn't on sys.path. Validation falls back to the local
    # `_is_xpath` + presence checks when the import fails.
    try:
        from qtea.jit_resolver import validate_selector_payload as _validate
    except Exception:
        _validate = None  # type: ignore[assignment]

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
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
        # Validate up-front so a malformed dev-locators file fails at run-start,
        # not silently at test time when a fuzzy-match accidentally lands on
        # the bad entry. The string-form path catches Playwright debug syntax
        # (`link "..."`), unbalanced brackets, and the same injection markers
        # that `is_unsafe_selector` blocks.
        if _validate is not None:
            ok, why = _validate(payload, selector)
            if not ok:
                warnings.append(
                    f"dev-locators[{name}] rejected by validate_selector_payload"
                    f" ({why}); skipping"
                )
                continue
        out[name] = DevLocator(
            constant_name=name,
            selector=selector.strip(),
            strategy=entry.get("strategy") if isinstance(entry.get("strategy"), str) else None,
            intent=entry.get("intent") if isinstance(entry.get("intent"), str) else None,
            page_url=entry.get("page_url") if isinstance(entry.get("page_url"), str) else None,
            notes=entry.get("notes") if isinstance(entry.get("notes"), str) else None,
            payload=payload,
        )
    return out, path, warnings
