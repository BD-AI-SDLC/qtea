"""Idempotent editor for a SUT's `playwright.config.ts|js|mjs`.

The xpath rewriter (Phase B.6) emits `getByTestId('X')` calls when it sees
`[@data-test="X"]` predicates. Playwright's `getByTestId()` defaults to the
`data-testid` attribute; to make it target `data-test` instead, the SUT's
config must declare ``testIdAttribute: 'data-test'`` in the top-level
``use: { … }`` block.

This module's single public function ``ensure_test_id_attribute`` inserts
that line if — and only if — it's missing. Idempotent, safe to call on
every run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CONFIG_FILENAMES = (
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
)


@dataclass
class ConfigEditResult:
    path: Path | None
    changed: bool
    reason: str  # 'inserted' | 'already-present' | 'no-config' | 'no-use-block' | 'unparseable'


def find_config(sut_root: Path) -> Path | None:
    """Return the first playwright config file found at the SUT root."""
    for name in _CONFIG_FILENAMES:
        p = sut_root / name
        if p.is_file():
            return p
    return None


# `testIdAttribute` appears somewhere in the file — whether inside `use`
# or a project override is fine; both paths make `getByTestId` see
# `data-test`. Case-sensitive by design.
_TESTID_ATTR_ANY_RE = re.compile(
    r"""testIdAttribute\s*:\s*['"](?P<val>[^'"]+)['"]""",
)


# The top-level `use: { … }` block inside `defineConfig({ … })`. Matches
# up through the closing `}` of `use`. Uses a manual-depth walk after the
# initial `use: {` anchor to survive nested braces.
_USE_ANCHOR_RE = re.compile(r"\buse\s*:\s*\{")


def ensure_test_id_attribute(
    sut_root: Path,
    attr_name: str = "data-test",
) -> ConfigEditResult:
    """Add ``testIdAttribute: '<attr_name>'`` to the SUT's playwright config.

    Behaviour:
    - No config file → returns ``ConfigEditResult(reason='no-config')``.
    - Any ``testIdAttribute: '…'`` already present → ``'already-present'``
      (does NOT overwrite even if the value differs — the SUT owner's
      choice wins).
    - Otherwise, inserts ``testIdAttribute: '<attr_name>',`` as the first
      line inside the top-level ``use: { … }`` block and writes the file.
    - Missing ``use: { … }`` block → ``'no-use-block'`` (unusual; caller
      should surface this to HITL).
    """
    config = find_config(sut_root)
    if config is None:
        return ConfigEditResult(path=None, changed=False, reason="no-config")

    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return ConfigEditResult(path=config, changed=False, reason="unparseable")

    if _TESTID_ATTR_ANY_RE.search(text):
        return ConfigEditResult(path=config, changed=False, reason="already-present")

    anchor = _USE_ANCHOR_RE.search(text)
    if not anchor:
        return ConfigEditResult(path=config, changed=False, reason="no-use-block")

    # Insert the new key right after the opening brace of `use: {`.
    insert_at = anchor.end()
    # Preserve the indentation of the FIRST non-empty child line of `use`
    # so the new line lines up with siblings.
    child_indent = _detect_child_indent(text, insert_at)
    new_line = f"\n{child_indent}testIdAttribute: '{attr_name}',"
    new_text = text[:insert_at] + new_line + text[insert_at:]

    config.write_text(new_text, encoding="utf-8")
    return ConfigEditResult(path=config, changed=True, reason="inserted")


def _detect_child_indent(text: str, start: int) -> str:
    """Return the indent (whitespace prefix) of the first non-empty child
    line of a block whose opening `{` is at ``start - 1``.
    """
    # Skip whitespace/newlines
    i = start
    while i < len(text) and text[i] in " \t\n":
        if text[i] == "\n":
            # Grab the indent of the following line
            j = i + 1
            k = j
            while k < len(text) and text[k] in " \t":
                k += 1
            if k < len(text) and text[k] not in "\n":
                return text[j:k]
        i += 1
    return "    "  # sensible default


__all__ = ["ConfigEditResult", "ensure_test_id_attribute", "find_config"]
