---
name: analyze-sut-structure
description: 'Procedure for interpreting SUT inventory and making placement decisions'
---

# Analyze SUT Structure

Interpret `sut_inventory.json` to derive POM ownership, fixture matching, and file placement for the code-modification plan. Follow these procedures in order.

## Inventory Structure

```
sut_inventory.json
├── active_module: string          # name of the working module
├── modules[]
│   └── <active module>
│       ├── language: string
│       ├── package_manager: string
│       ├── path: string           # relative path within SUT root
│       ├── test_directory_layout
│       │   ├── base_dir           # e.g. "tests"
│       │   ├── convention         # by_type | by_page | flat
│       │   ├── default_target     # e.g. "tests/smoke"
│       │   └── subdirs[]          # {path, kind}
│       ├── src_directory_layout
│       │   ├── package_root       # e.g. "src/app"
│       │   ├── pages_object_dir   # e.g. "src/app/pages/object"
│       │   ├── pages_locators_dir # e.g. "src/app/pages/locators"
│       │   └── helpers_dir        # e.g. "src/app/helpers"
│       ├── existing_page_objects[]  # {name, file, scope, methods[]}
│       ├── existing_fixtures[]      # {name, file, yields, scope, depends_on[]}
│       ├── existing_helpers[]       # {name, file}
│       ├── existing_locators[]      # {class_name, file, constants[{name, selector}]}
│       └── auth_flow
│           ├── type               # oauth | saml | basic | cookie
│           ├── entry_method       # file:class.method
│           ├── fixture_entry      # file:fixture_name
│           └── credentials_env_vars[]
```

## POM Ownership Decision Tree

For each new UI element the test strategy mentions, determine which POM class owns it:

1. **Identify the screen.** Which URL / route / view does the element render on? A "page" in the POM sense is a screen the user navigates to, not a feature.

2. **Check existing POMs.** Scan `existing_page_objects[].scope` and method names. If an existing POM's methods already operate on the same screen (e.g. `ChatPage` has `click_on_new_chat` and the new element is also in the chat sidebar), that POM owns the new element.

3. **Check locator grouping.** Look at `existing_locators[]`. If a locator class (e.g. `ChatPageLocators`) already groups constants for elements on the same screen, the POM that references that locator class owns the new element. Same testid prefix (e.g. `Layout-*`, `Chat-*`) is a strong signal of co-location.

4. **Check existing method adjacency.** If existing methods in a POM reference elements that are DOM-adjacent to the new element (e.g. `OPEN_CLOSE_SIDE_NAVIGATION` and `NEW_CHAT` are in `ChatPageLocators`, and the new button is also in the side nav), extend that POM.

5. **Create new POM only when** no existing POM models the URL/route. The bar for "new POM" is a new route/page, not a new feature on an existing page. Most UI features extend an existing POM.

## Fixture Matching Heuristic

For each test precondition, find an existing fixture or decide to create:

1. **Exact name match.** Check `existing_fixtures[].name` for the precondition's natural fixture name (e.g. "authenticated user" → `auth_session`, `authenticated_session`, `chat_page`).

2. **Semantic match.** Map common precondition phrases:
   - "user is authenticated" / "logged in" → look for fixtures with `auth`, `login`, `session` in name
   - "application is loaded" / "page is ready" → look for fixtures yielding a page object
   - "test data exists" / "seeded" → look for fixtures with `seed`, `data`, `setup` in name
   - "locale is X" / "language is X" → look for fixtures with `locale`, `lang`, `i18n` in name

3. **Auth flow shortcut.** `auth_flow.fixture_entry` (e.g. `tests/conftest.py:auth_session`) covers authentication preconditions. If the precondition is "user is authenticated", reuse this fixture.

4. **Create only when** checks 1-3 all fail. Emit `source: "create"` with `at` pointing to an inventory-approved directory. Prefer inline fixtures (in the test file) for simple parametrize-feeding fixtures; prefer `conftest.py` for session-scoped or shared fixtures.

## Directory Placement Rules

| File Category | Inventory Field | Example |
|---|---|---|
| Test files | `test_directory_layout.default_target` | `tests/smoke/worca_<feature>_test.py` |
| Page objects | `src_directory_layout.pages_object_dir` | `src/app/pages/object/worca_<feature>_page.py` |
| Locators | `src_directory_layout.pages_locators_dir` | `src/app/pages/locators/worca_<feature>_locators.py` |
| Helpers | `src_directory_layout.helpers_dir` | `src/app/helpers/worca_<feature>_helper.py` |
| Fixtures | `test_directory_layout.base_dir` + `/fixtures/` | `tests/fixtures/worca_<feature>_fixture.py` |
| Test data | `test_directory_layout.base_dir` + `/data/` | `tests/data/worca_<feature>_data.json` |

All generated filenames must start with `worca_` (or `Worca` for Java classes) to prevent collisions with SUT files.

## Reuse-First Checklist

Before emitting `source: "create"` for any fixture, POM, helper, or locator, verify all four checks fail:

1. **Name match.** Is there an existing entry with the same name (or a close variant — `login_page` vs `LoginPage`)?
2. **File match.** Is there an existing entry whose `file` path covers the same screen / concern?
3. **Selector match.** For locators: does any existing constant have a selector that targets the same element (byte-identical or semantically equivalent)?
4. **Auth coverage.** For auth-related preconditions: does `auth_flow.fixture_entry` already cover this?

Only if all four checks fail → propose `source: "create"`. Include a one-line justification (e.g. "no existing fixture covers locale parametrization").
