"""Unit tests for the deterministic SUT inventory detector.

Covers the three-tier detection (Python AST + TS regex + LLM-YAML merge),
monorepo enumeration across the 9 supported workspace types, test-directory
layout classification, and active-module resolution. The fixtures here use
tmp_path to keep tests hermetic — no network, no external clones.
"""

from __future__ import annotations

from pathlib import Path

from qtea.sut_inventory import (
    Fixture,
    LocatorClass,
    LocatorConstant,
    ModuleInventory,
    NavigationPrecondition,
    PageObject,
    SutInventory,
    capture_pattern_exemplars,
    detect_architecture_pattern,
    detect_module_inventory,
    detect_monorepo,
    detect_src_directory_layout,
    detect_sut_inventory,
    detect_test_directory_layout,
    merge_llm_inventory,
    parse_llm_inventory_yaml,
    resolve_active_module,
    scan_python_auth_flow,
    scan_python_fixtures,
    scan_python_lifecycle_hooks,
    scan_python_locators,
    scan_python_page_objects,
    scan_ts_auth_flow,
    scan_ts_lifecycle_hooks,
    scan_ts_locators,
    scan_ts_page_objects,
)


def _seed_screenplay(root: Path) -> None:
    """Custom Screenplay SUT: framework/ package, tasks/questions dirs, no src/."""
    _touch(root / "framework" / "actor.py",
           "class Actor:\n    def attempts_to(self, *tasks):\n        pass\n")
    _touch(root / "framework" / "tasks" / "login.py",
           "from framework.actor import Actor\n\n\n"
           "class Login(Task):\n"
           "    def perform_as(self, actor):\n"
           "        return actor\n")
    _touch(root / "framework" / "questions" / "title.py",
           "class PageTitle(Question):\n"
           "    def answered_by(self, actor):\n"
           "        return 'x'\n")


def _seed_pom(root: Path) -> None:
    _touch(root / "src" / "app" / "pages" / "login_page.py",
           "class LoginPage:\n    def click_submit(self):\n        pass\n")


def test_detect_architecture_pattern_screenplay(tmp_path: Path) -> None:
    _seed_screenplay(tmp_path)
    assert detect_architecture_pattern(
        tmp_path, page_objects=[], locators=[], language="python",
    ) == "screenplay"


def test_detect_architecture_pattern_pom(tmp_path: Path) -> None:
    _seed_pom(tmp_path)
    pages = scan_python_page_objects(tmp_path)
    assert detect_architecture_pattern(
        tmp_path, page_objects=pages, locators=[], language="python",
    ) == "pom"


def test_detect_architecture_pattern_none(tmp_path: Path) -> None:
    _touch(tmp_path / "README.md", "hi")
    assert detect_architecture_pattern(
        tmp_path, page_objects=[], locators=[], language="python",
    ) == "none"


def test_capture_pattern_exemplars_screenplay(tmp_path: Path) -> None:
    _seed_screenplay(tmp_path)
    exemplars = capture_pattern_exemplars(
        tmp_path, pattern="screenplay", language="python",
    )
    cats = {e.category for e in exemplars}
    assert "task" in cats and "question" in cats
    login = next(e for e in exemplars if e.category == "task")
    assert login.class_name == "Login"
    assert login.dir == "framework/tasks"
    assert "def perform_as" in login.excerpt


def _seed_bespoke_screenplay(root: Path) -> None:
    """Bespoke Screenplay SUT: base classes are named `BaseTask`/`BaseQuestion`
    (not the upstream `screenpy` `Task`/`Question`), and a single file holds
    several units — the real-world shape that captured zero exemplars before."""
    _touch(root / "framework" / "tasks" / "base_task.py",
           "from abc import ABC\n\n\nclass BaseTask(ABC):\n    pass\n")
    _touch(root / "framework" / "questions" / "base_question.py",
           "from abc import ABC\n\n\nclass BaseQuestion(ABC):\n    pass\n")
    _touch(root / "framework" / "tasks" / "forms.py",
           "from framework.tasks.base_task import BaseTask\n\n\n"
           "class FillBudget(BaseTask):\n"
           "    def perform_as(self, actor):\n        return actor\n\n\n"
           "class SavePlanPosition(BaseTask):\n"
           "    def perform_as(self, actor):\n        return actor\n")
    _touch(root / "framework" / "questions" / "state.py",
           "from framework.questions.base_question import BaseQuestion\n\n\n"
           "class PlanItemCreated(BaseQuestion):\n"
           "    def answered_by(self, actor):\n        return True\n")


def test_capture_pattern_exemplars_bespoke_base_classes(tmp_path: Path) -> None:
    # Regression: a SUT whose base classes are `BaseTask`/`BaseQuestion` must
    # still yield exemplars. Empty exemplars left the writer with nothing to
    # imitate, so it invented upstream `screenpy` imports.
    _seed_bespoke_screenplay(tmp_path)
    exemplars = capture_pattern_exemplars(
        tmp_path, pattern="screenplay", language="python",
    )
    cats = {e.category for e in exemplars}
    assert "task" in cats and "question" in cats
    task = next(e for e in exemplars if e.category == "task")
    assert task.class_name in ("FillBudget", "SavePlanPosition")
    assert "BaseTask" in task.excerpt


def test_capture_pattern_exemplars_generic_base_question(tmp_path: Path) -> None:
    # Regression: Python questions typed as `BaseQuestion[str]` are subscripted
    # generics. `_base_names` used to handle only ast.Name / ast.Attribute, so
    # every generic-based unit was dropped — losing the entire `question`
    # category (observed on the valuemation SUT, which false-greened Step 8).
    _touch(tmp_path / "framework" / "questions" / "base_question.py",
           "from abc import ABC, abstractmethod\n"
           "from typing import Generic, TypeVar\n\n"
           "T = TypeVar('T')\n\n\n"
           "class BaseQuestion(ABC, Generic[T]):\n"
           "    @abstractmethod\n"
           "    def answered_by(self, actor): ...\n")
    _touch(tmp_path / "framework" / "questions" / "cost_center.py",
           "from framework.questions.base_question import BaseQuestion\n\n\n"
           "class SelectedFieldValue(BaseQuestion[str]):\n"
           "    def answered_by(self, actor):\n        return 'x'\n")
    _touch(tmp_path / "framework" / "tasks" / "base_task.py",
           "from abc import ABC\n\n\nclass BaseTask(ABC):\n    pass\n")
    _touch(tmp_path / "framework" / "tasks" / "login.py",
           "from framework.tasks.base_task import BaseTask\n\n\n"
           "class Login(BaseTask):\n"
           "    def perform_as(self, actor):\n        return actor\n")
    exemplars = capture_pattern_exemplars(
        tmp_path, pattern="screenplay", language="python",
    )
    cats = {e.category for e in exemplars}
    assert "question" in cats, "generic-based question was dropped"
    question = next(e for e in exemplars if e.category == "question")
    assert question.class_name == "SelectedFieldValue"
    assert "BaseQuestion[str]" in question.excerpt


def _seed_bespoke_ts_screenplay(root: Path) -> None:
    """Bespoke Playwright+TS Screenplay SUT: base classes named `BaseTask`/
    `BaseQuestion` and inherited via `extends` (not `implements Task`)."""
    _touch(root / "framework" / "tasks" / "fill-budget.ts",
           "import { BaseTask } from '../base/base-task';\n\n"
           "export class FillBudget extends BaseTask {\n"
           "  async performAs(actor: Actor): Promise<void> {}\n"
           "}\n")
    _touch(root / "framework" / "questions" / "plan-item.ts",
           "import { BaseQuestion } from '../base/base-question';\n\n"
           "export class PlanItemCreated extends BaseQuestion<boolean> {\n"
           "  async answeredBy(actor: Actor): Promise<boolean> { return true; }\n"
           "}\n")


def test_capture_pattern_exemplars_bespoke_ts(tmp_path: Path) -> None:
    # Regression: a Playwright+TS/JS Screenplay SUT whose units use bespoke
    # base classes via `extends BaseTask` must still yield exemplars — the old
    # `implements \bTask\b` regex missed them, leaving the writer ungrounded.
    _seed_bespoke_ts_screenplay(tmp_path)
    exemplars = capture_pattern_exemplars(
        tmp_path, pattern="screenplay", language="typescript",
    )
    cats = {e.category for e in exemplars}
    assert "task" in cats and "question" in cats
    task = next(e for e in exemplars if e.category == "task")
    assert task.class_name == "FillBudget"
    assert "extends BaseTask" in task.excerpt


def test_capture_pattern_exemplars_empty_for_pom(tmp_path: Path) -> None:
    _seed_pom(tmp_path)
    assert capture_pattern_exemplars(
        tmp_path, pattern="pom", language="python",
    ) == []


def test_src_layout_no_fabricated_pom_path_for_screenplay(tmp_path: Path) -> None:
    """Root-cause regression: Screenplay SUTs must NOT get a synthetic
    `src/.../pages/object` fallback (broke run 20260715-075512-f2dbad)."""
    _seed_screenplay(tmp_path)
    layout = detect_src_directory_layout(
        tmp_path, page_objects=[], helpers=[], language="python",
        architecture_pattern="screenplay",
    )
    assert layout.pages_object_dir is None
    assert layout.pages_locators_dir is None


def test_src_layout_fabricates_pom_path_for_pom_greenfield(tmp_path: Path) -> None:
    """POM/unknown SUTs keep the greenfield fallback (unchanged behaviour)."""
    layout = detect_src_directory_layout(
        tmp_path, page_objects=[], helpers=[], language="python",
        architecture_pattern="pom",
    )
    assert layout.pages_object_dir is not None
    assert layout.pages_object_dir.endswith("pages/object")


def test_pattern_fields_survive_serialize_and_merge(tmp_path: Path) -> None:
    _seed_screenplay(tmp_path)
    inv = detect_module_inventory(tmp_path, ".")
    assert inv.architecture_pattern == "screenplay"
    assert inv.pattern_exemplars
    d = inv.as_dict()
    assert d["architecture_pattern"] == "screenplay"
    assert len(d["pattern_exemplars"]) >= 1
    # merge (the clone-through path) must NOT drop the new fields
    merged = merge_llm_inventory(inv, {"language": "python"})
    assert merged.architecture_pattern == "screenplay"
    assert len(merged.pattern_exemplars) == len(inv.pattern_exemplars)


def _touch(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Lifecycle-hook + open-method discovery
# ---------------------------------------------------------------------------


def test_ts_lifecycle_hooks_and_open_method(tmp_path: Path) -> None:
    """TS: beforeEach/afterEach hooks capture ordered body calls; the open
    method (openBaseURL → page.goto) is detected on the BasePage POM."""
    _touch(
        tmp_path / "src" / "pages" / "BasePage.ts",
        "export class BasePage {\n"
        "  async openBaseURL() { await this.page.goto('/'); }\n"
        "  async logIn(u: string, p: string) { /* ... */ }\n"
        "  async logout() { /* ... */ }\n"
        "}\n",
    )
    _touch(
        tmp_path / "tests" / "smoke.spec.ts",
        "import { test } from './fixtures';\n"
        "test.beforeEach(async ({ basePage }) => {\n"
        "  await basePage.openBaseURL();\n"
        "  await basePage.logIn('u', 'p');\n"
        "});\n"
        "test.afterEach(async ({ basePage }) => {\n"
        "  await basePage.logout();\n"
        "});\n",
    )
    pages = scan_ts_page_objects(tmp_path)
    auth = scan_ts_auth_flow(tmp_path, pages)
    assert auth.open_method == "src/pages/BasePage.ts:BasePage.openBaseURL"

    hooks = scan_ts_lifecycle_hooks(tmp_path)
    by_event = {h.event: h for h in hooks}
    assert by_event["before_each"].calls == ["basePage.openBaseURL", "basePage.logIn"]
    assert by_event["after_each"].calls == ["basePage.logout"]


def test_python_lifecycle_hooks_unittest_and_autouse(tmp_path: Path) -> None:
    _touch(
        tmp_path / "tests" / "test_smoke.py",
        "import pytest\n"
        "class TestThing:\n"
        "    def setUp(self):\n"
        "        self.base.open_base_url()\n"
        "        self.base.log_in('u', 'p')\n"
        "    def tearDown(self):\n"
        "        self.base.logout()\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def around(page):\n"
        "    page.goto('/')\n"
        "    yield\n"
        "    page.close()\n",
    )
    hooks = scan_python_lifecycle_hooks(tmp_path)
    events = {h.event for h in hooks}
    assert {"before_each", "after_each"} <= events
    setup = next(h for h in hooks if h.framework_construct == "setUp")
    assert setup.event == "before_each"
    assert setup.calls == ["self.base.open_base_url", "self.base.log_in"]
    # autouse function fixture with yield → both a before_each and after_each
    autouse_hooks = [h for h in hooks if "autouse" in h.framework_construct]
    assert {h.event for h in autouse_hooks} == {"before_each", "after_each"}


def test_python_open_method_detected_from_name(tmp_path: Path) -> None:
    _touch(
        tmp_path / "src" / "pages" / "base_page.py",
        "class BasePage:\n"
        "    def open_base_url(self):\n"
        "        self.page.goto('/')\n"
        "    def login(self, u, p):\n"
        "        pass\n",
    )
    pages = scan_python_page_objects(tmp_path)
    auth = scan_python_auth_flow(tmp_path, pages, [])
    assert auth.open_method and auth.open_method.endswith("BasePage.open_base_url")


# ---------------------------------------------------------------------------
# Monorepo detection — one test per workspace signal
# ---------------------------------------------------------------------------


def test_no_monorepo_signal_returns_single_module(tmp_path: Path):
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is False
    assert signal is None
    assert modules == ["."]


def test_pnpm_workspace(tmp_path: Path):
    _touch(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _touch(tmp_path / "packages" / "web" / "package.json", "{}")
    _touch(tmp_path / "packages" / "api" / "package.json", "{}")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "pnpm-workspace.yaml"
    assert sorted(modules) == ["packages/api", "packages/web"]


def test_npm_yarn_workspaces_list(tmp_path: Path):
    _touch(
        tmp_path / "package.json",
        '{"name": "root", "workspaces": ["packages/*"]}',
    )
    _touch(tmp_path / "packages" / "core" / "package.json", "{}")
    _touch(tmp_path / "packages" / "ui" / "package.json", "{}")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "package.json:workspaces"
    assert sorted(modules) == ["packages/core", "packages/ui"]


def test_npm_workspaces_object_form(tmp_path: Path):
    _touch(
        tmp_path / "package.json",
        '{"workspaces": {"packages": ["apps/*"]}}',
    )
    _touch(tmp_path / "apps" / "web" / "package.json", "{}")
    is_mono, _, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert modules == ["apps/web"]


def test_lerna_packages(tmp_path: Path):
    _touch(tmp_path / "lerna.json", '{"packages": ["modules/*"]}')
    _touch(tmp_path / "modules" / "a" / "package.json", "{}")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "lerna.json"
    assert modules == ["modules/a"]


def test_nx_enumerates_project_json(tmp_path: Path):
    _touch(tmp_path / "nx.json", "{}")
    _touch(tmp_path / "apps" / "web" / "project.json", "{}")
    _touch(tmp_path / "libs" / "shared" / "project.json", "{}")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "nx.json"
    assert sorted(modules) == ["apps/web", "libs/shared"]


def test_pyproject_uv_workspace(tmp_path: Path):
    _touch(
        tmp_path / "pyproject.toml",
        "[tool.uv.workspace]\nmembers = ['packages/api', 'packages/web']\n",
    )
    _touch(tmp_path / "packages" / "api" / "pyproject.toml", "")
    _touch(tmp_path / "packages" / "web" / "pyproject.toml", "")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert "uv" in signal
    assert sorted(modules) == ["packages/api", "packages/web"]


def test_maven_modules(tmp_path: Path):
    _touch(
        tmp_path / "pom.xml",
        """<?xml version="1.0"?>
<project>
  <modules>
    <module>web</module>
    <module>api</module>
  </modules>
</project>""",
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "api").mkdir()
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "pom.xml:modules"
    assert sorted(modules) == ["api", "web"]


def test_gradle_settings_include(tmp_path: Path):
    _touch(
        tmp_path / "settings.gradle.kts",
        'include(":app")\ninclude(":lib:core")\n',
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "core").mkdir(parents=True)
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "settings.gradle.kts"
    assert sorted(modules) == ["app", "lib/core"]


def test_cargo_workspace(tmp_path: Path):
    _touch(
        tmp_path / "Cargo.toml",
        "[workspace]\nmembers = ['crates/a', 'crates/b']\n",
    )
    _touch(tmp_path / "crates" / "a" / "Cargo.toml", "")
    _touch(tmp_path / "crates" / "b" / "Cargo.toml", "")
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert "Cargo" in signal
    assert sorted(modules) == ["crates/a", "crates/b"]


def test_go_work_use_directives(tmp_path: Path):
    _touch(
        tmp_path / "go.work",
        "go 1.21\n\nuse (\n  ./svc1\n  ./svc2\n)\n",
    )
    (tmp_path / "svc1").mkdir()
    (tmp_path / "svc2").mkdir()
    is_mono, signal, modules = detect_monorepo(tmp_path)
    assert is_mono is True
    assert signal == "go.work"
    assert sorted(modules) == ["svc1", "svc2"]


# ---------------------------------------------------------------------------
# Test directory layout classification
# ---------------------------------------------------------------------------


def test_layout_by_type_with_regression_preferred(tmp_path: Path):
    (tmp_path / "tests" / "smoke").mkdir(parents=True)
    (tmp_path / "tests" / "regression").mkdir()
    (tmp_path / "tests" / "fixtures").mkdir()
    layout = detect_test_directory_layout(tmp_path)
    assert layout.base_dir == "tests"
    assert layout.convention == "by_type"
    assert layout.default_target == "tests/regression"
    kinds = {s.name: s.kind for s in layout.subdirs}
    assert kinds["smoke"] == "type"
    assert kinds["fixtures"] == "support"


def test_layout_by_page(tmp_path: Path):
    (tmp_path / "tests" / "home_page").mkdir(parents=True)
    (tmp_path / "tests" / "login_page").mkdir()
    layout = detect_test_directory_layout(tmp_path)
    assert layout.convention == "by_page"
    assert layout.default_target.startswith("tests/")


def test_layout_flat(tmp_path: Path):
    _touch(tmp_path / "tests" / "test_x.py", "def test_x():\n    pass\n")
    layout = detect_test_directory_layout(tmp_path)
    assert layout.convention == "flat"
    assert layout.default_target == "tests"


def test_layout_missing_returns_empty(tmp_path: Path):
    layout = detect_test_directory_layout(tmp_path)
    assert layout.base_dir is None
    assert layout.convention == "unknown"


def test_layout_finds_e2e_root(tmp_path: Path):
    (tmp_path / "e2e" / "smoke").mkdir(parents=True)
    layout = detect_test_directory_layout(tmp_path)
    assert layout.base_dir == "e2e"


# ---------------------------------------------------------------------------
# Tier 1: Python AST detection of page objects + helpers + fixtures + auth
# ---------------------------------------------------------------------------


_SIGN_IN_PAGE = """\
from base_page import BasePage


class SignInPage(BasePage):
    def __init__(self, page):
        self.page = page

    def sign_in(self, user: str, password: str) -> None:
        self.page.fill('#user', user)
        self.page.fill('#password', password)
        self.page.click('button[type=submit]')

    def _internal_helper(self):
        pass
"""

_NAV_PAGE = """\
class NavPage:
    def open_home(self): pass
    def click_settings(self): pass
"""


def test_scan_python_page_objects_finds_nested_pages(tmp_path: Path):
    _touch(tmp_path / "src" / "app" / "pages" / "object" / "sign_in_page.py", _SIGN_IN_PAGE)
    _touch(tmp_path / "src" / "app" / "pages" / "object" / "nav_page.py", _NAV_PAGE)
    pages = scan_python_page_objects(tmp_path)
    by_name = {p.name: p for p in pages}
    assert "SignInPage" in by_name
    assert "NavPage" in by_name
    assert by_name["SignInPage"].scope == "auth"
    assert by_name["NavPage"].scope == "navigation"
    assert "sign_in" in by_name["SignInPage"].methods
    assert "_internal_helper" not in by_name["SignInPage"].methods


def test_scan_python_page_objects_skips_classes_without_methods(tmp_path: Path):
    _touch(tmp_path / "pages" / "empty.py", "class EmptyPage:\n    pass\n")
    pages = scan_python_page_objects(tmp_path)
    assert all(p.name != "EmptyPage" for p in pages)


_FIXTURE_PY = """\
import pytest


@pytest.fixture(scope="session")
def base_url() -> str:
    return "http://qa"


@pytest.fixture
def chat_setup(base_url, page):
    return page
"""


def test_scan_python_fixtures(tmp_path: Path):
    _touch(tmp_path / "tests" / "conftest.py", _FIXTURE_PY)
    fixtures = scan_python_fixtures(tmp_path)
    by_name = {f.name: f for f in fixtures}
    assert by_name["base_url"].scope == "session"
    assert by_name["chat_setup"].scope == "function"
    assert "base_url" in by_name["chat_setup"].depends_on


def test_scan_python_auth_flow_prefers_class_method(tmp_path: Path):
    _touch(tmp_path / "pages" / "sign_in_page.py", _SIGN_IN_PAGE)
    pages = scan_python_page_objects(tmp_path)
    fixtures: list[Fixture] = []
    auth = scan_python_auth_flow(tmp_path, pages, fixtures)
    assert auth.type in ("sso", "oauth")
    assert auth.entry_method.endswith(":SignInPage.sign_in")


def test_scan_python_auth_flow_falls_back_to_grep(tmp_path: Path):
    # No page-object hits; should still find sign_in() via file-grep fallback.
    _touch(
        tmp_path / "src" / "auth" / "sign_in.py",
        "import os\n\nSSO_USER = os.environ.get('SSO_USER')\n\ndef sign_in(user, pw): pass\n",
    )
    auth = scan_python_auth_flow(tmp_path, [], [])
    assert auth.entry_method is not None
    assert "sign_in" in auth.entry_method
    assert "SSO_USER" in auth.credentials_env_vars


# ---------------------------------------------------------------------------
# Tier 2: TS regex page-object detection
# ---------------------------------------------------------------------------


_LOGIN_PAGE_TS = """\
import { Page } from '@playwright/test';

export class LoginPage {
  constructor(private page: Page) {}
  async login(email: string, password: string): Promise<void> {
    await this.page.fill('[name=email]', email);
  }
  async logout() { /* ... */ }
}
"""


def test_scan_ts_page_objects(tmp_path: Path):
    _touch(tmp_path / "tests" / "pages" / "LoginPage.ts", _LOGIN_PAGE_TS)
    pages = scan_ts_page_objects(tmp_path)
    assert len(pages) == 1
    assert pages[0].name == "LoginPage"
    assert pages[0].scope == "auth"
    assert "login" in pages[0].methods


# ---------------------------------------------------------------------------
# Tier 1: Python locator-class detection (fix A)
# ---------------------------------------------------------------------------


_CLASS_LEVEL_LOCATORS_PY = """\
from typing import ClassVar


class ChatPageLocators:
    DEFAULT_PROMPT: ClassVar[str] = "[data-testid='PromptInput-Input']"
    SEND_BUTTON = "[data-testid='PromptInput-Submit']"
    LOGIN_URL = "https://example.com/login"
    EMPTY = ""
    NOTES = "some freeform text that is not a selector"
"""

_SELF_ASSIGN_LOCATORS_PY = """\
class ChatPageLocators:
    \"\"\"AskBosch pattern: constants inside __init__ so reset() can re-seed.\"\"\"
    def __init__(self):
        self.LANGUAGE_DROP_DOWN = "[data-testid='LanguageSelect-Select']"
        self.SELECT_EN = "[data-testid='LanguageSelect-Item'][value='en']"
        self.SELECT_DE = "[data-testid='LanguageSelect-Item'][value='de']"
        self.OPEN_CLOSE_SIDE_NAVIGATION = "[data-testid='dssf-button-close-side-navigation']"
        self.WIDGET_NAME = "OK-MIXED"  # short token that still looks like a selector
        # blank/empty values commonly seen — the scanner must skip them
        self.MISSING_LINK = ""
"""

_LOCATORS_NO_LOCATOR_CLASS_PY = """\
class HelperUtility:
    PROMPT = "[data-testid='Prompt']"  # not a Locator class, must be skipped
"""


def test_scan_python_locators_finds_class_constants(tmp_path: Path):
    _touch(
        tmp_path / "src" / "app" / "pages" / "locators" / "chat_page_locators.py",
        _CLASS_LEVEL_LOCATORS_PY,
    )
    classes = scan_python_locators(tmp_path)
    assert len(classes) == 1
    lc = classes[0]
    assert lc.class_name == "ChatPageLocators"
    by_name = {c.name: c for c in lc.constants}
    assert "DEFAULT_PROMPT" in by_name
    assert by_name["DEFAULT_PROMPT"].selector == "[data-testid='PromptInput-Input']"
    assert "SEND_BUTTON" in by_name
    # URLs / empty / freeform-text constants must be filtered out
    assert "LOGIN_URL" not in by_name
    assert "EMPTY" not in by_name
    assert "NOTES" not in by_name


def test_scan_python_locators_finds_self_assignments(tmp_path: Path):
    _touch(
        tmp_path
        / "src" / "app" / "pages" / "locators" / "chat_page_locators.py",
        _SELF_ASSIGN_LOCATORS_PY,
    )
    classes = scan_python_locators(tmp_path)
    assert len(classes) == 1
    by_name = {c.name: c for c in classes[0].constants}
    # The four real selectors must surface
    assert "LANGUAGE_DROP_DOWN" in by_name
    assert by_name["LANGUAGE_DROP_DOWN"].selector == "[data-testid='LanguageSelect-Select']"
    assert "SELECT_EN" in by_name
    assert "SELECT_DE" in by_name
    assert "OPEN_CLOSE_SIDE_NAVIGATION" in by_name
    # Short identifier-shaped value is allowed (matches `[A-Za-z][\w\-]*`)
    assert "WIDGET_NAME" in by_name
    # Empty string must be skipped
    assert "MISSING_LINK" not in by_name


def test_scan_python_locators_skips_non_locator_classes(tmp_path: Path):
    # A non-Locator class in a candidate file must not surface even though
    # it has a selector-looking constant inside.
    _touch(
        tmp_path / "src" / "app" / "pages" / "locators" / "helper_locators.py",
        _LOCATORS_NO_LOCATOR_CLASS_PY,
    )
    classes = scan_python_locators(tmp_path)
    assert classes == []


def test_scan_python_locators_caps_constants_per_class(tmp_path: Path):
    # 100 constants > cap (80). Scanner must keep 80 (sorted alphabetically)
    # and record truncated_count=20.
    body_lines = ["class HugeLocators:"]
    for i in range(100):
        body_lines.append(f"    LOC_{i:03d} = '[data-testid=\"item-{i:03d}\"]'")
    _touch(
        tmp_path / "src" / "pages" / "locators" / "huge_locators.py",
        "\n".join(body_lines) + "\n",
    )
    classes = scan_python_locators(tmp_path)
    assert len(classes) == 1
    lc = classes[0]
    assert len(lc.constants) == 80
    assert lc.truncated_count == 20
    # Alphabetical truncation keeps the first 80 by name.
    kept_names = [c.name for c in lc.constants]
    assert kept_names[0] == "LOC_000"
    assert kept_names[-1] == "LOC_079"


def test_scan_python_locators_picks_up_files_outside_locators_dir(tmp_path: Path):
    # Naming-based discovery: any *locator*.py file is a candidate even
    # without the conventional `pages/locators/` directory.
    _touch(
        tmp_path / "src" / "myMod" / "myMod_locators.py",
        "class MyModLocators:\n    BUTTON = '#submit'\n",
    )
    classes = scan_python_locators(tmp_path)
    assert any(c.class_name == "MyModLocators" for c in classes)


# ---------------------------------------------------------------------------
# Tier 2: TS locator-class detection (fix A)
# ---------------------------------------------------------------------------


_TS_STATIC_LOCATORS = """\
export class ChatLocators {
  static readonly PROMPT_INPUT = "[data-testid='PromptInput-Input']";
  public static readonly SEND_BUTTON = '[data-testid="PromptInput-Submit"]';
  static readonly LOGIN_URL = "https://example.com";
  static readonly EMPTY = "";
}
"""

_TS_CONST_OBJECT_LOCATORS = """\
export const ChatLocators = {
  PROMPT_INPUT: "[data-testid='PromptInput-Input']",
  SEND_BUTTON: "[data-testid='PromptInput-Submit']",
};
"""


def test_scan_ts_locators_static_readonly(tmp_path: Path):
    _touch(tmp_path / "src" / "pages" / "locators" / "chat.locators.ts", _TS_STATIC_LOCATORS)
    classes = scan_ts_locators(tmp_path)
    assert len(classes) == 1
    by_name = {c.name: c for c in classes[0].constants}
    assert "PROMPT_INPUT" in by_name
    assert "SEND_BUTTON" in by_name
    assert "LOGIN_URL" not in by_name
    assert "EMPTY" not in by_name


def test_scan_ts_locators_const_object_pattern(tmp_path: Path):
    _touch(tmp_path / "tests" / "locators" / "chat.locators.ts", _TS_CONST_OBJECT_LOCATORS)
    classes = scan_ts_locators(tmp_path)
    assert len(classes) == 1
    names = {c.name for c in classes[0].constants}
    assert names == {"PROMPT_INPUT", "SEND_BUTTON"}


# ---------------------------------------------------------------------------
# detect_module_inventory wiring (fix A)
# ---------------------------------------------------------------------------


def test_detect_module_inventory_python_poetry(tmp_path: Path):
    _touch(tmp_path / "pyproject.toml", "[tool.poetry]\nname='x'\n")
    _touch(tmp_path / "poetry.lock", "")
    _touch(tmp_path / "tests" / "regression" / "test_x.py", "def test_x(): pass\n")
    _touch(tmp_path / "pages" / "sign_in_page.py", _SIGN_IN_PAGE)
    inv = detect_module_inventory(tmp_path, ".")
    assert inv.name == "sut"
    assert inv.path == "."
    assert inv.language == "python"
    assert inv.package_manager == "poetry"
    assert inv.test_directory_layout.base_dir == "tests"
    assert inv.test_directory_layout.default_target == "tests/regression"
    names = [p.name for p in inv.existing_page_objects]
    assert "SignInPage" in names
    assert inv.source == "deterministic"


def test_detect_module_inventory_includes_existing_locators(tmp_path: Path):
    # End-to-end: locator class on disk → surfaces under existing_locators
    # on the assembled ModuleInventory.
    _touch(tmp_path / "pyproject.toml", "[tool.poetry]\nname='x'\n")
    _touch(tmp_path / "poetry.lock", "")
    _touch(tmp_path / "tests" / "smoke" / "test_x.py", "def test_x(): pass\n")
    _touch(
        tmp_path / "src" / "app" / "pages" / "locators" / "chat_page_locators.py",
        _SELF_ASSIGN_LOCATORS_PY,
    )
    inv = detect_module_inventory(tmp_path, ".")
    assert len(inv.existing_locators) == 1
    lc = inv.existing_locators[0]
    assert lc.class_name == "ChatPageLocators"
    names = {c.name for c in lc.constants}
    assert {"LANGUAGE_DROP_DOWN", "SELECT_EN", "SELECT_DE", "OPEN_CLOSE_SIDE_NAVIGATION"} <= names
    assert inv.source == "deterministic"  # locators alone justify deterministic


def test_detect_sut_inventory_single_module(tmp_path: Path):
    _touch(tmp_path / "package-lock.json", "{}")
    _touch(tmp_path / "package.json", "{}")
    _touch(tmp_path / "tests" / "Login.spec.ts", "test('login', () => {})")
    inv = detect_sut_inventory(tmp_path)
    assert inv.is_monorepo is False
    assert len(inv.modules) == 1
    assert inv.active_module == "sut"
    assert inv.modules[0].language == "typescript"


def test_detect_sut_inventory_missing_path_returns_empty(tmp_path: Path):
    inv = detect_sut_inventory(tmp_path / "doesnotexist")
    assert isinstance(inv, SutInventory)
    assert inv.modules == []


def test_detect_sut_inventory_monorepo_with_module_hint(tmp_path: Path):
    _touch(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _touch(tmp_path / "packages" / "web" / "package.json", "{}")
    _touch(tmp_path / "packages" / "api" / "pyproject.toml", "[tool.poetry]\nname='api'\n")
    _touch(tmp_path / "packages" / "api" / "poetry.lock", "")
    inv = detect_sut_inventory(tmp_path, module_hint="api")
    assert inv.is_monorepo is True
    assert sorted(m.name for m in inv.modules) == ["api", "web"]
    assert inv.active_module == "api"


def test_detect_sut_inventory_monorepo_missing_hint_fails_resolve(tmp_path: Path):
    _touch(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _touch(tmp_path / "packages" / "a" / "package.json", "{}")
    _touch(tmp_path / "packages" / "b" / "package.json", "{}")
    inv = detect_sut_inventory(tmp_path)  # no hint, no spec_text
    # active_module is None; notes carry the resolution-failure message.
    assert inv.active_module is None
    assert inv.notes
    assert any("multiple modules" in n or "--module" in n for n in inv.notes)


def test_detect_sut_inventory_monorepo_invalid_hint(tmp_path: Path):
    _touch(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _touch(tmp_path / "packages" / "x" / "package.json", "{}")
    inv = detect_sut_inventory(tmp_path, module_hint="nope")
    assert inv.active_module is None
    assert any("not found" in n for n in inv.notes)


def test_detect_sut_inventory_spec_text_auto_detects(tmp_path: Path):
    _touch(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    _touch(tmp_path / "packages" / "web-app" / "package.json", "{}")
    _touch(tmp_path / "packages" / "api" / "package.json", "{}")
    spec = "Refactor the web-app navigation to add a new menu item."
    inv = detect_sut_inventory(tmp_path, spec_text=spec)
    assert inv.active_module == "web-app"


# ---------------------------------------------------------------------------
# Active module resolution edge cases
# ---------------------------------------------------------------------------


def test_resolve_active_module_no_modules(tmp_path: Path):
    inv = SutInventory()
    name, err = resolve_active_module(inv, explicit=None)
    assert name is None
    assert "no modules" in err


def test_resolve_active_module_single_auto_selected():
    inv = SutInventory(modules=[ModuleInventory(name="sut", path=".")])
    name, err = resolve_active_module(inv, explicit=None)
    assert name == "sut"
    assert err is None


def test_resolve_active_module_explicit_wins():
    inv = SutInventory(modules=[
        ModuleInventory(name="a", path="a"),
        ModuleInventory(name="b", path="b"),
    ])
    name, _err = resolve_active_module(inv, explicit="b")
    assert name == "b"


# ---------------------------------------------------------------------------
# Tier 3: LLM YAML block parser
# ---------------------------------------------------------------------------


def test_parse_llm_inventory_yaml_simple_block():
    md = """## SUT Inventory

```yaml
sut_inventory_module:
  name: api
  path: packages/api
  language: java
  package_manager: maven
  existing_page_objects:
    - { name: LoginPage, file: src/test/java/LoginPage.java, methods: [login], scope: auth }
  auth_flow:
    type: basic
    entry_method: src/test/java/LoginPage.java:LoginPage.login
    credentials_env_vars: [API_USER, API_PASSWORD]
```
"""
    blocks = parse_llm_inventory_yaml(md)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["name"] == "api"
    assert b["language"] == "java"
    assert b["package_manager"] == "maven"
    assert b["existing_page_objects"][0]["name"] == "LoginPage"
    assert b["auth_flow"]["credentials_env_vars"] == ["API_USER", "API_PASSWORD"]


def test_parse_llm_inventory_yaml_multiple_blocks():
    md = """
```yaml
sut_inventory_module:
  name: web
  path: packages/web
```

```yaml
sut_inventory_module:
  name: api
  path: packages/api
```
"""
    blocks = parse_llm_inventory_yaml(md)
    assert len(blocks) == 2
    assert {b["name"] for b in blocks} == {"web", "api"}


def test_parse_llm_inventory_yaml_no_blocks():
    blocks = parse_llm_inventory_yaml("Just some prose, no inventory.")
    assert blocks == []


# ---------------------------------------------------------------------------
# Tier 3: merge logic — deterministic wins where both present
# ---------------------------------------------------------------------------


def test_merge_llm_inventory_fills_only_gaps():
    det = ModuleInventory(
        name="sut", path=".", language="python", package_manager="poetry",
        existing_page_objects=[PageObject(
            name="Page1", file="pages/p1.py", class_name="Page1",
            methods=["m1"], scope="navigation",
        )],
        source="deterministic",
    )
    llm = {
        "name": "sut",
        "language": "javascript",  # should LOSE to deterministic
        "package_manager": "npm",   # should LOSE
        "existing_page_objects": [
            {"name": "Page1", "file": "pages/p1.py", "methods": ["new_m"], "scope": "navigation"},
            {"name": "Page2", "file": "pages/p2.py", "methods": ["m2"], "scope": "form"},
        ],
        "auth_flow": {"type": "sso", "entry_method": "x:y.z"},
    }
    merged = merge_llm_inventory(det, llm)
    assert merged.language == "python"          # deterministic preserved
    assert merged.package_manager == "poetry"   # deterministic preserved
    page_names = [p.name for p in merged.existing_page_objects]
    assert "Page1" in page_names
    assert "Page2" in page_names                # LLM-only POs are appended
    assert merged.auth_flow.type == "sso"       # filled from LLM (det was unknown)
    assert merged.source == "llm_augmented"


def test_merge_llm_inventory_promotes_llm_only_source():
    # Deterministic returned empty (e.g. Java SUT); LLM fills everything.
    det = ModuleInventory(name="api", path="packages/api", source="llm_only")
    llm = {
        "name": "api",
        "language": "java",
        "package_manager": "maven",
        "existing_page_objects": [
            {"name": "LoginPage", "file": "x.java", "methods": ["login"], "scope": "auth"},
        ],
    }
    merged = merge_llm_inventory(det, llm)
    assert merged.language == "java"
    assert merged.package_manager == "maven"
    assert merged.source == "llm_only"
    assert merged.existing_page_objects[0].name == "LoginPage"


def test_merge_llm_inventory_empty_llm_returns_deterministic():
    det = ModuleInventory(name="sut", path=".", language="python")
    merged = merge_llm_inventory(det, {})
    assert merged.language == "python"


def test_merge_llm_inventory_merges_existing_locators():
    # Deterministic surfaces ChatPageLocators with 1 constant; LLM augments
    # with a class the AST scan didn't catch (e.g. a Java-side locators
    # file) AND adds a new constant to the existing class. Both must merge.
    det = ModuleInventory(
        name="sut", path=".", language="python", package_manager="poetry",
        existing_locators=[LocatorClass(
            name="ChatPageLocators",
            file="pages/locators/chat_page_locators.py",
            class_name="ChatPageLocators",
            constants=[LocatorConstant(name="EXISTING", selector="#x", line=10)],
        )],
        source="deterministic",
    )
    llm = {
        "name": "sut",
        "existing_locators": [
            # Same class — should graft NEW_FROM_LLM onto the existing one
            {
                "name": "ChatPageLocators",
                "file": "pages/locators/chat_page_locators.py",
                "class_name": "ChatPageLocators",
                "constants": [
                    # Dup of EXISTING — must NOT be added twice
                    {"name": "EXISTING", "selector": "#x", "line": 10},
                    {"name": "NEW_FROM_LLM", "selector": "#dynamic", "line": 99},
                ],
            },
            # Net-new class — must be appended
            {
                "name": "ExtraLocators",
                "file": "pages/locators/extra_locators.py",
                "class_name": "ExtraLocators",
                "constants": [{"name": "FOO", "selector": "#foo", "line": 1}],
            },
        ],
    }
    merged = merge_llm_inventory(det, llm)
    by_class = {lc.class_name: lc for lc in merged.existing_locators}
    assert "ChatPageLocators" in by_class
    assert "ExtraLocators" in by_class
    chat_consts = {c.name for c in by_class["ChatPageLocators"].constants}
    assert chat_consts == {"EXISTING", "NEW_FROM_LLM"}  # de-duped + grafted


# ---------------------------------------------------------------------------
# Serialization: LocatorConstant.line is stripped from JSON output
# ---------------------------------------------------------------------------


def test_asdict_strips_locator_constant_line_from_module():
    """ModuleInventory.as_dict must NOT include `line` on locator constants.

    The field stays on the in-memory dataclass for any future debugging
    consumer, but the JSON written to disk and forwarded to the codegen
    agent must omit it: the codegen step uses constants only for
    byte-match dedup (name + selector), `line` is never consulted
    programmatically, and dropping it trims ~10% off the inventory wire
    payload — a meaningful win on the Bosch relay where each codegen
    turn re-sends the full prefix uncached.
    """
    mod = ModuleInventory(
        name="m", path=".", language="python", package_manager="poetry",
        existing_locators=[LocatorClass(
            name="ChatPageLocators",
            file="x.py",
            class_name="ChatPageLocators",
            constants=[
                LocatorConstant(name="A", selector="#a", line=10),
                LocatorConstant(name="B", selector="#b", line=42),
            ],
        )],
        source="deterministic",
    )
    # In-memory dataclass still carries `line` (preserve traceability):
    assert mod.existing_locators[0].constants[0].line == 10

    # Serialized dict must NOT carry `line`:
    d = mod.as_dict()
    consts = d["existing_locators"][0]["constants"]
    assert {c["name"] for c in consts} == {"A", "B"}
    for c in consts:
        assert "line" not in c, f"`line` leaked into serialized constant: {c}"
        # Functional fields must still be present.
        assert "name" in c and "selector" in c


def test_asdict_strips_locator_constant_line_through_full_inventory():
    """The strip also walks SutInventory.modules[].existing_locators[]
    so multi-module SUTs get the same treatment as single-module ones.
    """
    inv = SutInventory(
        is_monorepo=True,
        modules=[
            ModuleInventory(
                name="frontend", path="apps/web", language="typescript",
                existing_locators=[LocatorClass(
                    name="LoginLocators", file="login.ts",
                    class_name="LoginLocators",
                    constants=[LocatorConstant(name="EMAIL", selector="#e", line=7)],
                )],
            ),
            ModuleInventory(
                name="backend", path="apps/api", language="python",
                existing_locators=[LocatorClass(
                    name="ApiLocators", file="api.py",
                    class_name="ApiLocators",
                    constants=[LocatorConstant(name="HEALTH", selector="#h", line=22)],
                )],
            ),
        ],
        active_module="frontend",
    )
    d = inv.as_dict()
    for mod_d in d["modules"]:
        for lc in mod_d["existing_locators"]:
            for c in lc["constants"]:
                assert "line" not in c, (
                    f"`line` leaked into {mod_d['name']}.{lc['class_name']}.{c['name']}"
                )


# ---------------------------------------------------------------------------
# scan_ts_fixtures — Playwright test.extend blocks
# ---------------------------------------------------------------------------


def test_scan_ts_fixtures_finds_async_and_sync(tmp_path: Path):
    """Both async and sync arrow-function fixtures are found.

    Regression: the old regex required `async` and silently dropped
    sync fixtures like ``{syncFix: ({page}, use) => {...}}``. Also
    covers the ``baseTest.extend<T>({...})`` receiver-name variant.
    """
    from qtea.sut_inventory import scan_ts_fixtures

    fixture_file = tmp_path / "tests" / "fixtures" / "pageFixtures.ts"
    fixture_file.parent.mkdir(parents=True, exist_ok=True)
    fixture_file.write_text(
        "import { test as baseTest } from '@playwright/test';\n"
        "\n"
        "export const test = baseTest.extend<{"
        "loginPage: LoginPage; basePage: BasePage;"
        "}>({\n"
        "  loginPage: async ({ page }, use) => {\n"
        "    await use(new LoginPage(page));\n"
        "  },\n"
        "  basePage: ({ page }, use) => {\n"
        "    use(new BasePage(page));\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )
    fixtures = scan_ts_fixtures(tmp_path)
    names = sorted(f.name for f in fixtures)
    assert names == ["basePage", "loginPage"], (
        f"expected both async loginPage and sync basePage, got {names!r}"
    )


def test_scan_ts_fixtures_accepts_no_generic_param(tmp_path: Path):
    """`.extend({...})` without a generic type param still finds fixtures."""
    from qtea.sut_inventory import scan_ts_fixtures

    fixture_file = tmp_path / "tests" / "fixtures" / "simple.ts"
    fixture_file.parent.mkdir(parents=True, exist_ok=True)
    fixture_file.write_text(
        "import { test } from '@playwright/test';\n"
        "\n"
        "export const customTest = test.extend({\n"
        "  myFixture: async ({ page }, use) => { await use(page); },\n"
        "});\n",
        encoding="utf-8",
    )
    fixtures = scan_ts_fixtures(tmp_path)
    assert [f.name for f in fixtures] == ["myFixture"]


# ---------------------------------------------------------------------------
# Pattern-agnostic TS locator scanners — multi-convention recognition
# ---------------------------------------------------------------------------
#
# The scanner must recognise any reasonable Playwright-TS convention:
#   - separate `class *Locators*` (historical Python-Selenium style)
#   - `export const *Locators/*Selectors/*Elements = {...}` object literal
#   - inline `elements = {btnX: '...'}` property on a POM class
#   - `readonly submitBtn = this.page.getByRole(...)` Locator properties


def test_scan_ts_locators_inline_object_property(tmp_path: Path):
    """The user's SUT convention: `class Foo { elements = {btnX: '...'} }`.

    Regression: the old scanner required a separate `*Locators*` class and
    UPPERCASE constants, silently returning zero entries for this common
    Playwright-TS pattern.
    """
    from qtea.sut_inventory import scan_ts_locators

    page_file = tmp_path / "src" / "pages" / "EntityFormPage.ts"
    page_file.parent.mkdir(parents=True, exist_ok=True)
    page_file.write_text(
        "import { Page } from '@playwright/test';\n"
        "\n"
        "export class EntityFormPage {\n"
        "    constructor(private page: Page) {}\n"
        "\n"
        "    elements: Record<string, string> = {\n"
        "        btnCreateNewRopa: '//button[@data-test=\"create\"]',\n"
        "        btnSubmit: '[data-testid=\"submit-btn\"]',\n"
        "        inpName: '#name-input',\n"
        "    };\n"
        "}\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    assert len(results) == 1
    lc = results[0]
    assert lc.location_pattern == "inline_object_property"
    assert lc.owning_pom == "EntityFormPage"
    assert lc.container_name == "elements"
    assert lc.class_name == "EntityFormPage"
    names = sorted(c.name for c in lc.constants)
    assert names == ["btnCreateNewRopa", "btnSubmit", "inpName"], (
        f"camelCase properties must be found; got {names!r}"
    )


def test_scan_ts_locators_export_const_object(tmp_path: Path):
    """`export const FooLocators = {EMAIL: '...'}` object-literal convention."""
    from qtea.sut_inventory import scan_ts_locators

    src = tmp_path / "src" / "pages" / "LoginPage.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "export const LoginLocators = {\n"
        "    EMAIL_INPUT: '[data-testid=\"email\"]',\n"
        "    PASSWORD_INPUT: '[data-testid=\"password\"]',\n"
        "    SUBMIT_BTN: '#submit',\n"
        "};\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    assert len(results) == 1
    lc = results[0]
    assert lc.location_pattern == "export_const_object"
    assert lc.class_name == "LoginLocators"
    names = sorted(c.name for c in lc.constants)
    assert names == ["EMAIL_INPUT", "PASSWORD_INPUT", "SUBMIT_BTN"]


def test_scan_ts_locators_uppercase_snake_bag_with_owning_pom(tmp_path: Path):
    """Regression: an UPPERCASE_SNAKE bag
    (`export const BASE_LOCATORS = {...}`) with camelCase string keys, in a
    `<Pom>.locators.ts` file, must be catalogued (the old PascalCase-only,
    case-sensitive regex missed it) AND carry owning_pom inferred from the
    filename so Step 8's locator resolver can map create_tbd(owning_page) to
    it."""
    from qtea.sut_inventory import scan_ts_locators

    src = tmp_path / "src" / "pages" / "locators" / "BasePage.locators.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "export const BASE_LOCATORS = {\n"
        "  inpUsername: '//input[@data-test=\"username-input\"]',\n"
        "  btnLogin: '//input[@data-test=\"submit-button\"]',\n"
        "};\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    assert len(results) == 1
    lc = results[0]
    assert lc.location_pattern == "export_const_object"
    assert lc.class_name == "BASE_LOCATORS"
    assert lc.owning_pom == "BasePage"
    assert sorted(c.name for c in lc.constants) == ["btnLogin", "inpUsername"]


def test_scan_ts_locators_rejects_incidental_name_matches(tmp_path: Path):
    """Names that CONTAIN 'locator'/'selector'/'element' but don't END with
    the bag-suffix must NOT be picked up as locator bags. Guards against the
    over-broad `re.I` regex that would flag `defaultLocatorStrategy` (a config
    variable) and similar as locator sources.

    Positive counter-example: a properly-named `PageSelectors` const in the
    same file IS picked up, so the negatives above are being rejected on
    naming shape, not because the scanner is broken."""
    page_file = tmp_path / "src" / "pages" / "MixedNaming.ts"
    page_file.parent.mkdir(parents=True, exist_ok=True)
    page_file.write_text(
        # NEGATIVES: contain the keyword but are not locator bags.
        'export const defaultLocatorStrategy = "css";\n'
        'export const elementCounter = 0;\n'
        'export const selectorTests = [];\n'
        'export const someLocatorFactory = () => ({ x: 1 });\n'
        # POSITIVE: real bag, name ends with the suffix.
        "export const PageSelectors = {\n"
        '  btnGo: "#go",\n'
        "};\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    # Only the well-named bag should be picked up.
    names = [lc.class_name for lc in results]
    assert names == ["PageSelectors"], (
        f"regex over-flagged non-locator names: {names}"
    )


def test_scan_ts_locators_readonly_locator_props(tmp_path: Path):
    """`readonly submitBtn = this.page.getByRole(...)` Locator properties.

    Playwright-idiomatic pattern where locators ARE Locator objects, not
    selector strings. The scanner records their existence for dedup even
    though `selector` is empty.
    """
    from qtea.sut_inventory import scan_ts_locators

    src = tmp_path / "src" / "pages" / "DashboardPage.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "import { Page, Locator } from '@playwright/test';\n"
        "\n"
        "export class DashboardPage {\n"
        "    readonly submitBtn: Locator;\n"
        "    readonly navMenu: Locator;\n"
        "\n"
        "    constructor(page: Page) {\n"
        "        this.submitBtn = page.getByRole('button', { name: 'Submit' });\n"
        "        this.navMenu = page.getByTestId('nav-menu');\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    # The assignment-style syntax above is inside constructor, not the
    # class body — a more accurate test file uses inline `readonly X =`
    # property definitions:
    src.write_text(
        "import { Page } from '@playwright/test';\n"
        "\n"
        "export class DashboardPage {\n"
        "    constructor(private page: Page) {}\n"
        "\n"
        "    readonly submitBtn = this.page.getByRole('button', { name: 'Submit' });\n"
        "    readonly navMenu = this.page.getByTestId('nav-menu');\n"
        "}\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    assert len(results) == 1
    lc = results[0]
    assert lc.location_pattern == "readonly_locator_props"
    assert lc.owning_pom == "DashboardPage"
    names = sorted(c.name for c in lc.constants)
    assert names == ["navMenu", "submitBtn"]


def test_scan_ts_locators_separate_class_still_works(tmp_path: Path):
    """The historical `class FooLocators { EMAIL = "..." }` still works —
    the multi-pattern rewrite must not regress existing SUTs."""
    from qtea.sut_inventory import scan_ts_locators

    src = tmp_path / "src" / "locators" / "loginLocators.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "export class LoginPageLocators {\n"
        "    static readonly EMAIL = '[data-testid=\"email\"]';\n"
        "    static readonly PASSWORD = '[data-testid=\"password\"]';\n"
        "}\n",
        encoding="utf-8",
    )
    results = scan_ts_locators(tmp_path)
    assert len(results) >= 1
    hit = next((lc for lc in results if lc.class_name == "LoginPageLocators"), None)
    assert hit is not None
    assert hit.location_pattern == "separate_class"
    names = sorted(c.name for c in hit.constants)
    assert names == ["EMAIL", "PASSWORD"]


# ---------------------------------------------------------------------------
# Pattern-agnostic TS POM detection — structural intent + naming
# ---------------------------------------------------------------------------


def test_scan_ts_page_objects_finds_extends_class(tmp_path: Path):
    """POM class name followed by `extends` (not `{` or `<`) must be found.

    Regression: the old `_TS_CLASS_RE` required `[{<]` immediately after
    the class name, missing every subclass declaration in the wild.
    """
    from qtea.sut_inventory import scan_ts_page_objects

    src = tmp_path / "src" / "pages" / "EntityFormPage.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "import { BasePage } from './BasePage';\n"
        "\n"
        "export class EntityFormPage extends BasePage {\n"
        "    constructor(page) { super(page); }\n"
        "\n"
        "    elements = { btnX: '//button' };\n"
        "\n"
        "    async click() { await this.page.locator('#x').click(); }\n"
        "}\n",
        encoding="utf-8",
    )
    poms = scan_ts_page_objects(tmp_path)
    names = sorted(p.name for p in poms)
    assert "EntityFormPage" in names
    hit = next(p for p in poms if p.name == "EntityFormPage")
    assert hit.has_inline_locators is True


def test_scan_ts_page_objects_structural_detection(tmp_path: Path):
    """Class NOT named `*Page*` is still classified as POM when its body
    contains Playwright/Selenium/Cypress API calls."""
    from qtea.sut_inventory import scan_ts_page_objects

    src = tmp_path / "src" / "screens" / "Dashboard.ts"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "import { Page } from '@playwright/test';\n"
        "\n"
        "export class Dashboard {\n"
        "    constructor(private page: Page) {}\n"
        "\n"
        "    async open() {\n"
        "        await this.page.goto('/dashboard');\n"
        "        await this.page.locator('#main').waitFor();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    poms = scan_ts_page_objects(tmp_path)
    names = sorted(p.name for p in poms)
    assert "Dashboard" in names, (
        f"Structural POM detection should find `Dashboard` "
        f"despite non-`*Page` name; got {names!r}"
    )


# ---------------------------------------------------------------------------
# navigation_preconditions — LLM-only field, no deterministic producer
# ---------------------------------------------------------------------------


def test_merge_llm_inventory_appends_navigation_preconditions():
    det = ModuleInventory(name="sut", path=".", language="typescript", source="deterministic")
    llm = {
        "name": "sut",
        "navigation_preconditions": [
            {
                "method": "DirectoryPage.selectFilteredEntity",
                "requires_call": "BasePage.selectLoginOptionByText",
                "requires_args_hint": "NAV_OPTIONS.DIRECTORY",
                "evidence": "tests/EntityFormSmoke.spec.ts:104",
            },
        ],
    }
    merged = merge_llm_inventory(det, llm)
    assert len(merged.navigation_preconditions) == 1
    entry = merged.navigation_preconditions[0]
    assert entry.method == "DirectoryPage.selectFilteredEntity"
    assert entry.requires_call == "BasePage.selectLoginOptionByText"
    assert entry.requires_args_hint == "NAV_OPTIONS.DIRECTORY"
    assert entry.evidence == "tests/EntityFormSmoke.spec.ts:104"


def test_merge_llm_inventory_dedupes_navigation_preconditions():
    det = ModuleInventory(
        name="sut", path=".", language="typescript",
        navigation_preconditions=[
            NavigationPrecondition(
                method="DirectoryPage.selectFilteredEntity",
                requires_call="BasePage.selectLoginOptionByText",
                requires_args_hint="NAV_OPTIONS.DIRECTORY",
                evidence="tests/EntityFormSmoke.spec.ts:104",
            ),
        ],
        source="llm_augmented",
    )
    llm = {
        "name": "sut",
        "navigation_preconditions": [
            # Exact duplicate on (method, requires_call) — must not be added twice
            {
                "method": "DirectoryPage.selectFilteredEntity",
                "requires_call": "BasePage.selectLoginOptionByText",
                "requires_args_hint": "NAV_OPTIONS.DIRECTORY",
                "evidence": "tests/EntityFormSmoke.spec.ts:104",
            },
            # Net-new pair — must be appended
            {
                "method": "OtherGrid.selectRow",
                "requires_call": "BasePage.selectLoginOptionByText",
                "requires_args_hint": "NAV_OPTIONS.OTHER",
                "evidence": "tests/Other.spec.ts:10",
            },
            # Malformed — missing requires_call — must be skipped
            {"method": "Broken.method"},
        ],
    }
    merged = merge_llm_inventory(det, llm)
    keys = [(np.method, np.requires_call) for np in merged.navigation_preconditions]
    assert keys.count(("DirectoryPage.selectFilteredEntity", "BasePage.selectLoginOptionByText")) == 1
    assert ("OtherGrid.selectRow", "BasePage.selectLoginOptionByText") in keys
    assert len(merged.navigation_preconditions) == 2
