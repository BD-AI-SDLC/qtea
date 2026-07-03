"""Unit tests for the playwright config editor."""

from __future__ import annotations

from pathlib import Path

from qtea.playwright_config_editor import ensure_test_id_attribute, find_config

_CONFIG_WITH_USE = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  use: {
    baseURL: 'https://example.com',
    trace: 'retain-on-failure',
  },
});
"""


_CONFIG_WITH_TESTID_ALREADY = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({
  use: {
    testIdAttribute: 'data-testid',
    baseURL: 'https://example.com',
  },
});
"""


_CONFIG_NO_USE_BLOCK = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 30000,
});
"""


def test_ensure_test_id_attribute_inserts_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "playwright.config.ts"
    p.write_text(_CONFIG_WITH_USE, encoding="utf-8")
    r = ensure_test_id_attribute(tmp_path)
    assert r.changed is True
    assert r.reason == "inserted"
    assert r.path == p
    new = p.read_text(encoding="utf-8")
    assert "testIdAttribute: 'data-test'" in new
    # Inserted at the top of `use: { … }`
    assert new.find("testIdAttribute") < new.find("baseURL")


def test_ensure_test_id_attribute_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "playwright.config.ts"
    p.write_text(_CONFIG_WITH_TESTID_ALREADY, encoding="utf-8")
    r = ensure_test_id_attribute(tmp_path)
    assert r.changed is False
    assert r.reason == "already-present"
    # File unchanged
    assert p.read_text(encoding="utf-8") == _CONFIG_WITH_TESTID_ALREADY


def test_ensure_test_id_attribute_preserves_existing_value(tmp_path: Path) -> None:
    """If SUT already declares testIdAttribute (any value), don't overwrite."""
    p = tmp_path / "playwright.config.ts"
    p.write_text(_CONFIG_WITH_TESTID_ALREADY, encoding="utf-8")  # sets 'data-testid'
    r = ensure_test_id_attribute(tmp_path, attr_name="data-test")
    assert r.changed is False
    assert "testIdAttribute: 'data-testid'" in p.read_text(encoding="utf-8")


def test_ensure_test_id_attribute_no_config(tmp_path: Path) -> None:
    r = ensure_test_id_attribute(tmp_path)
    assert r.changed is False
    assert r.reason == "no-config"
    assert r.path is None


def test_ensure_test_id_attribute_no_use_block(tmp_path: Path) -> None:
    p = tmp_path / "playwright.config.ts"
    p.write_text(_CONFIG_NO_USE_BLOCK, encoding="utf-8")
    r = ensure_test_id_attribute(tmp_path)
    assert r.changed is False
    assert r.reason == "no-use-block"


def test_find_config_prefers_ts_over_js(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.js").write_text("// js", encoding="utf-8")
    (tmp_path / "playwright.config.ts").write_text("// ts", encoding="utf-8")
    p = find_config(tmp_path)
    assert p is not None
    assert p.suffix == ".ts"


def test_ensure_test_id_attribute_indentation_matches_siblings(tmp_path: Path) -> None:
    """Insertion picks up the sibling's indentation prefix (2 or 4 space)."""
    src = (
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({\n"
        "        use: {\n"
        "                baseURL: 'x',\n"
        "        },\n"
        "});\n"
    )
    p = tmp_path / "playwright.config.ts"
    p.write_text(src, encoding="utf-8")
    r = ensure_test_id_attribute(tmp_path)
    assert r.changed
    new = p.read_text(encoding="utf-8")
    # New line should have 16-space indent (matches baseURL)
    assert "                testIdAttribute: 'data-test'," in new
