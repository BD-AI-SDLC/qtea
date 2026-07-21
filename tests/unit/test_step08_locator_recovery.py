"""Step 8 locator-source recovery + contract honesty.

Covers the fix for `step08.undefined_locator_ref_failed`: when Step 6's
inventory misses a SUT's shared locator bag (`existing_locators: []`), Step 8
recovers it by following the POM's own imports, so create_tbd sentinels are
materialised into the real bag instead of the extender emitting dangling
`<BAG>.<KEY>` references. Also covers `_materialized_prewritten_by_page`
(contract honesty) and the tightened `existing_locators` schema item.
"""

from __future__ import annotations

import jsonschema
import pytest

from qtea.schemas import load_schema
from qtea.steps.s08_codegen import (
    _LocatorTask,
    _build_locator_tasks,
    _materialized_prewritten_by_page,
    _resolve_locator_inventory_entry,
    _resolve_locator_source_by_import,
    _write_tbd_locators,
)


# --- fixtures: minimal TS/JS + Python SUTs -------------------------------

def _ts_bag_sut(tmp_path):
    """POM importing a shared `export const BASE_LOCATORS = {…}` bag."""
    pom = tmp_path / "src" / "pages" / "BasePage.ts"
    bag = tmp_path / "src" / "pages" / "locators" / "BasePage.locators.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    bag.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "import { BASE_LOCATORS } from './locators/BasePage.locators';\n"
        "export class BasePage {\n"
        "  constructor(private page: any) {}\n"
        "  async login() {\n"
        "    await this.page.locator(BASE_LOCATORS.btnLogin).click();\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    bag.write_text(
        "export const BASE_LOCATORS = {\n"
        "  btnLogin: '#login',\n"
        "};\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "qtea-runtime.js").write_text(
        "module.exports.tbd = s => s;\n", encoding="utf-8",
    )
    return tmp_path


def _py_module_bag_sut(tmp_path):
    """Python POM importing a module of constants used as `mod.NAME`."""
    pom = tmp_path / "pages" / "base_page.py"
    mod = tmp_path / "pages" / "locators.py"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "from pages import locators\n"
        "class BasePage:\n"
        "    def __init__(self, page):\n"
        "        self.page = page\n"
        "    def login(self):\n"
        "        self.page.locator(locators.BTN_LOGIN).click()\n",
        encoding="utf-8",
    )
    mod.write_text('BTN_LOGIN = "#login"\n', encoding="utf-8")
    return tmp_path


# --- A1: import-following resolver ---------------------------------------

def test_import_follow_resolves_ts_export_const_bag(tmp_path):
    root = _ts_bag_sut(tmp_path)
    entry = _resolve_locator_source_by_import(
        "src/pages/BasePage.ts", root, "typescript",
    )
    assert entry is not None
    assert entry["file"] == "src/pages/locators/BasePage.locators.ts"
    assert entry["class_name"] == "BASE_LOCATORS"
    assert entry["location_pattern"] == "export_const_object"


def test_import_follow_via_inventory_resolver_empty_inventory(tmp_path):
    """The exact failing condition: existing_locators == []."""
    root = _ts_bag_sut(tmp_path)
    entry = _resolve_locator_inventory_entry(
        "BasePage", [],
        pom_file="src/pages/BasePage.ts", sut_root=root, language="typescript",
    )
    assert entry is not None
    assert entry["class_name"] == "BASE_LOCATORS"


def test_import_follow_none_when_no_bag(tmp_path):
    pom = tmp_path / "src" / "pages" / "PlainPage.ts"
    pom.parent.mkdir(parents=True, exist_ok=True)
    pom.write_text(
        "export class PlainPage {\n"
        "  constructor(private page: any) {}\n"
        "  async go() { await this.page.getByRole('button').click(); }\n"
        "}\n",
        encoding="utf-8",
    )
    entry = _resolve_locator_source_by_import(
        "src/pages/PlainPage.ts", tmp_path, "typescript",
    )
    assert entry is None


def test_import_follow_resolves_python_module_const_bag(tmp_path):
    root = _py_module_bag_sut(tmp_path)
    entry = _resolve_locator_source_by_import(
        "pages/base_page.py", root, "python",
    )
    assert entry is not None
    assert entry["file"] == "pages/locators.py"
    assert entry["location_pattern"] == "module_const_bag"


# --- A1: builder wiring --------------------------------------------------

def test_build_locator_tasks_recovers_bag_from_import(tmp_path):
    root = _ts_bag_sut(tmp_path)
    plan = {
        "language": "typescript",
        "test_cases": [{
            "page_objects": [
                {"name": "BasePage", "source": "reuse",
                 "from": "src/pages/BasePage.ts"},
            ],
            "locators": [
                {"name": "btnNotificationBell", "owning_page": "BasePage",
                 "source": "create_tbd", "intent": "notification bell"},
            ],
        }],
    }
    # inventory=None mirrors an empty/missing existing_locators
    tasks = _build_locator_tasks(plan, None, root, "typescript")
    assert len(tasks) == 1
    t = tasks[0]
    assert t.locator_file == "src/pages/locators/BasePage.locators.ts"
    assert t.location_pattern == "export_const_object"
    assert t.container_class_name == "BASE_LOCATORS"


def test_end_to_end_materializes_sentinels_into_recovered_bag(tmp_path):
    root = _ts_bag_sut(tmp_path)
    entry = _resolve_locator_source_by_import(
        "src/pages/BasePage.ts", root, "typescript",
    )
    task = _LocatorTask(
        constant_name="btnNotificationBell",
        intent="notification bell icon",
        owning_page="BasePage",
        locator_file=entry["file"],
        location_pattern="export_const_object",
        container_class_name="BASE_LOCATORS",
    )
    written = _write_tbd_locators([task], root, "typescript")
    assert written == 1
    bag = (root / entry["file"]).read_text(encoding="utf-8")
    assert 'btnNotificationBell: tbd("notification bell icon")' in bag
    assert "qtea-runtime" in bag  # tbd import injected


# --- A2: contract honesty ------------------------------------------------

def test_materialized_prewritten_only_lists_defined_constants(tmp_path):
    bag = tmp_path / "locators.ts"
    bag.write_text(
        "export const BAG = {\n"
        '  present: tbd("x"),\n'
        "};\n",
        encoding="utf-8",
    )
    tasks = [
        _LocatorTask(constant_name="present", intent="x", owning_page="P",
                     locator_file="locators.ts",
                     location_pattern="export_const_object"),
        _LocatorTask(constant_name="missing", intent="y", owning_page="P",
                     locator_file="locators.ts",
                     location_pattern="export_const_object"),
        _LocatorTask(constant_name="nofile", intent="z", owning_page="P",
                     locator_file=None),
    ]
    result = _materialized_prewritten_by_page(tasks, tmp_path)
    assert result == {"P": ["present"]}


# --- B2: schema constraint ----------------------------------------------

def _existing_locators_item_schema():
    schema = load_schema("research")
    return (
        schema["properties"]["sut_inventory"]["properties"]["modules"]
        ["items"]["properties"]["existing_locators"]["items"]
    )


def test_schema_accepts_well_formed_locator_entry():
    item = _existing_locators_item_schema()
    good = {
        "file": "src/pages/locators/BasePage.locators.ts",
        "class_name": "BASE_LOCATORS",
        "owning_pom": "BasePage",
        "location_pattern": "export_const_object",
    }
    jsonschema.validate(good, item)  # must not raise


def test_schema_rejects_entry_without_file():
    item = _existing_locators_item_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"class_name": "BASE_LOCATORS"}, item)


def test_schema_rejects_bad_location_pattern():
    item = _existing_locators_item_schema()
    bad = {"file": "x.ts", "location_pattern": "not_a_real_pattern"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, item)
