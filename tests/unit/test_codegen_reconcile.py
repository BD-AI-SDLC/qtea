"""Unit tests for Phase B.5 static reconciliation (`codegen_reconcile`).

Exercises the AST-based Python extractor and the regex-based TS/JS extractor
against tiny in-test fixture strings written to `tmp_path` — no mocks of the
parsers themselves, so the real call-site discovery + import resolution +
method-signature comparison paths are covered end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qtea.codegen_reconcile import (
    FixtureMismatch,
    Mismatch,
    fixture_mismatches_to_fixture_tasks,
    mismatches_to_pom_tasks,
    reconcile_codegen,
    reconcile_fixtures,
)


def _touch(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture POM / test sources
# ---------------------------------------------------------------------------


_PY_POM_THREE_METHODS = """\
class LoginPage:
    def __init__(self, page):
        self.page = page

    def fill_username(self, user):
        self.page.fill('#user', user)

    def fill_password(self, pw):
        self.page.fill('#pw', pw)

    def submit(self):
        self.page.click('#submit')
"""

_PY_TEST_CALLS_MISSING_METHOD = """\
from pages.login_page import LoginPage


def test_login(page):
    login_page = LoginPage(page)
    login_page.fill_username('alice')
    login_page.fill_password('secret')
    login_page.click_remember_me()
    login_page.submit()
"""


_PY_POM_TWO_ARG_METHOD = """\
class LoginPage:
    def __init__(self, page):
        self.page = page

    def foo(self, a, b):
        self.page.fill('#x', a)
"""

_PY_TEST_BAD_ARITY = """\
from pages.login_page import LoginPage


def test_arity(page):
    login_page = LoginPage(page)
    login_page.foo(1)
"""


_PY_POM_LOGIN_FOR_HAPPY = """\
class LoginPage:
    def __init__(self, page):
        self.page = page

    def login(self, user, password):
        self.page.fill('#user', user)
        self.page.fill('#pw', password)
"""

_PY_TEST_HAPPY = """\
from pages.login_page import LoginPage


def test_login_happy(page):
    login_page = LoginPage(page)
    login_page.login('alice', 'secret')
"""


_PY_TEST_UNRELATED_HELPER = """\
def test_unrelated(unrelated_helper):
    unrelated_helper.foo()
"""


_PY_TEST_MALFORMED = """\
def test_broken(page):
    if True
        page.click()
"""


_TS_POM_LOGIN_TWO_ARGS = """\
import { Page } from '@playwright/test';

export class LoginPage {
  constructor(private page: Page) {}
  async login(user: string, pass: string): Promise<void> {
    await this.page.fill('#u', user);
  }
}
"""

_TS_TEST_BAD_ARITY = """\
import { LoginPage } from './LoginPage';
import { test } from '@playwright/test';

test('login arity', async ({ page }) => {
  const loginPage = new LoginPage(page);
  await loginPage.login("u");
});
"""


_TS_POM_LOGIN_ONLY = """\
export class LoginPage {
  async login(): Promise<void> {}
}
"""

_TS_TEST_MISSING_METHOD = """\
import { LoginPage } from './LoginPage';
import { test } from '@playwright/test';

test('signup missing', async ({ page }) => {
  const loginPage = new LoginPage(page);
  await loginPage.signUp("u", "p");
});
"""


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------


def test_python_method_not_found_on_three_method_pom(tmp_path: Path):
    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_login.py"
    _touch(tmp_path / pom_rel, _PY_POM_THREE_METHODS)
    _touch(tmp_path / test_rel, _PY_TEST_CALLS_MISSING_METHOD)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, "python",
    )

    not_found = [m for m in result.mismatches if m.kind == "method_not_found"]
    assert len(not_found) == 1
    miss = not_found[0]
    assert miss.call_site.method_name == "click_remember_me"
    assert miss.resolved_pom == "LoginPage"
    assert miss.pom_file == pom_rel
    # The other three calls (fill_username, fill_password, submit) must NOT
    # surface as mismatches — they all exist on the POM with matching arity.
    assert all(m.kind != "arity_mismatch" for m in result.mismatches)
    assert result.test_files_scanned == 1
    assert result.pom_files_scanned == 1


def test_python_arity_mismatch(tmp_path: Path):
    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_arity.py"
    _touch(tmp_path / pom_rel, _PY_POM_TWO_ARG_METHOD)
    _touch(tmp_path / test_rel, _PY_TEST_BAD_ARITY)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, "python",
    )

    arity = [m for m in result.mismatches if m.kind == "arity_mismatch"]
    assert len(arity) == 1
    miss = arity[0]
    assert miss.call_site.method_name == "foo"
    assert miss.call_site.arity == 1
    assert miss.resolved_pom == "LoginPage"


def test_python_happy_path_no_mismatches(tmp_path: Path):
    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_happy.py"
    _touch(tmp_path / pom_rel, _PY_POM_LOGIN_FOR_HAPPY)
    _touch(tmp_path / test_rel, _PY_TEST_HAPPY)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, "python",
    )

    assert result.mismatches == []
    assert result.call_sites_checked >= 1


def test_python_unrelated_receiver_ignored(tmp_path: Path):
    # The test calls `unrelated_helper.foo()` but the POM manifest only knows
    # about `LoginPage` — the receiver must NOT resolve, so no mismatch.
    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_unrelated.py"
    _touch(tmp_path / pom_rel, _PY_POM_LOGIN_FOR_HAPPY)
    _touch(tmp_path / test_rel, _PY_TEST_UNRELATED_HELPER)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, "python",
    )

    assert result.mismatches == []
    # `unrelated_helper` does not resolve to any known POM, so it was not
    # counted against `call_sites_checked`.
    assert result.call_sites_checked == 0


def test_python_parse_error_emits_parse_error_mismatch(tmp_path: Path):
    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_broken.py"
    _touch(tmp_path / pom_rel, _PY_POM_LOGIN_FOR_HAPPY)
    _touch(tmp_path / test_rel, _PY_TEST_MALFORMED)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, "python",
    )

    parse_errors = [m for m in result.mismatches if m.kind == "parse_error"]
    assert len(parse_errors) == 1
    assert parse_errors[0].pom_file == test_rel


# ---------------------------------------------------------------------------
# TS/JS extractor — same arity scenario across both languages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "pom_ext", "test_ext"),
    [
        ("typescript", "ts", "spec.ts"),
        ("javascript", "js", "spec.js"),
    ],
)
def test_js_ts_arity_mismatch(
    tmp_path: Path, language: str, pom_ext: str, test_ext: str,
):
    pom_rel = f"pages/LoginPage.{pom_ext}"
    test_rel = f"tests/login.{test_ext}"
    _touch(tmp_path / pom_rel, _TS_POM_LOGIN_TWO_ARGS)
    _touch(tmp_path / test_rel, _TS_TEST_BAD_ARITY)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, language,
    )

    arity = [m for m in result.mismatches if m.kind == "arity_mismatch"]
    assert len(arity) == 1
    miss = arity[0]
    assert miss.call_site.method_name == "login"
    assert miss.call_site.arity == 1
    assert miss.resolved_pom == "LoginPage"
    assert miss.pom_file == pom_rel


@pytest.mark.parametrize(
    ("language", "pom_ext", "test_ext"),
    [
        ("typescript", "ts", "spec.ts"),
        ("javascript", "js", "spec.js"),
    ],
)
def test_js_ts_method_not_found(
    tmp_path: Path, language: str, pom_ext: str, test_ext: str,
):
    pom_rel = f"pages/LoginPage.{pom_ext}"
    test_rel = f"tests/signup.{test_ext}"
    _touch(tmp_path / pom_rel, _TS_POM_LOGIN_ONLY)
    _touch(tmp_path / test_rel, _TS_TEST_MISSING_METHOD)

    pom_files = [{"file": pom_rel, "class_name": "LoginPage"}]
    result = reconcile_codegen(
        [tmp_path / test_rel], pom_files, tmp_path, language,
    )

    not_found = [m for m in result.mismatches if m.kind == "method_not_found"]
    assert len(not_found) == 1
    miss = not_found[0]
    assert miss.call_site.method_name == "signUp"
    assert miss.resolved_pom == "LoginPage"


# ---------------------------------------------------------------------------
# `mismatches_to_pom_tasks`: grouping + dedup across POMs
# ---------------------------------------------------------------------------


def test_mismatches_to_pom_tasks_groups_by_pom_and_dedups(tmp_path: Path):
    # Build a small real reconciliation so the Mismatch instances are produced
    # by the same code path that the helper consumes downstream.
    pom_a_rel = "pages/login_page.py"
    pom_b_rel = "pages/dashboard_page.py"
    _touch(
        tmp_path / pom_a_rel,
        "class LoginPage:\n"
        "    def existing(self):\n"
        "        pass\n",
    )
    _touch(
        tmp_path / pom_b_rel,
        "class DashboardPage:\n"
        "    def existing(self):\n"
        "        pass\n",
    )

    # Two test files — both call a missing method on LoginPage with the SAME
    # name (must dedup), and one calls a missing method on DashboardPage.
    test_a = "tests/test_a.py"
    test_b = "tests/test_b.py"
    _touch(
        tmp_path / test_a,
        "from pages.login_page import LoginPage\n"
        "from pages.dashboard_page import DashboardPage\n"
        "\n"
        "def test_a(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.missing_one()\n"
        "    dashboard_page = DashboardPage(page)\n"
        "    dashboard_page.missing_two()\n",
    )
    _touch(
        tmp_path / test_b,
        "from pages.login_page import LoginPage\n"
        "\n"
        "def test_b(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.missing_one()\n",
    )

    pom_files = [
        {"file": pom_a_rel, "class_name": "LoginPage"},
        {"file": pom_b_rel, "class_name": "DashboardPage"},
    ]
    recon = reconcile_codegen(
        [tmp_path / test_a, tmp_path / test_b],
        pom_files, tmp_path, "python",
    )

    # Sanity: we have 3 raw mismatches (2 dup `missing_one` on LoginPage +
    # 1 `missing_two` on DashboardPage).
    not_found = [m for m in recon.mismatches if m.kind == "method_not_found"]
    assert len(not_found) == 3

    tasks = mismatches_to_pom_tasks(recon.mismatches, original_pom_tasks={})

    # Two POM tasks — one per POM file — and the LoginPage one has a single
    # entry (dedup squashed the duplicate `missing_one`).
    assert set(tasks.keys()) == {pom_a_rel, pom_b_rel}
    login_task = tasks[pom_a_rel]
    dash_task = tasks[pom_b_rel]
    login_missing = [mm["name"] for mm in login_task.missing_methods]
    dash_missing = [mm["name"] for mm in dash_task.missing_methods]
    assert login_missing == ["missing_one"]
    assert dash_missing == ["missing_two"]
    # Synthesized tasks (no `original_pom_tasks` entry) record the resolved
    # POM class name as `pom_name`.
    assert login_task.pom_name == "LoginPage"
    assert dash_task.pom_name == "DashboardPage"


def test_mismatches_to_pom_tasks_reuses_existing_pom_task_metadata(tmp_path: Path):
    # When the original `_PomTask` is present, the synthesized task must
    # preserve its `source` / `from_path` / `at_path` / locator wiring so
    # `_extend_poms` re-runs against the same physical POM file with the
    # same locator imports.
    from qtea.steps.s08_codegen import _PomTask

    pom_rel = "pages/login_page.py"
    test_rel = "tests/test_x.py"
    _touch(tmp_path / pom_rel, "class LoginPage:\n    def existing(self):\n        pass\n")
    _touch(
        tmp_path / test_rel,
        "from pages.login_page import LoginPage\n"
        "\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.missing()\n",
    )

    original = {
        pom_rel: _PomTask(
            pom_name="LoginPage",
            pom_file=pom_rel,
            source="reuse",
            from_path=pom_rel,
            at_path=pom_rel,
            locator_file="pages/locators/login_page_locators.py",
            locator_class="LoginPageLocators",
        ),
    }
    recon = reconcile_codegen(
        [tmp_path / test_rel],
        [{"file": pom_rel, "class_name": "LoginPage"}],
        tmp_path,
        "python",
    )
    tasks = mismatches_to_pom_tasks(recon.mismatches, original_pom_tasks=original)

    assert set(tasks.keys()) == {pom_rel}
    t = tasks[pom_rel]
    assert t.locator_file == "pages/locators/login_page_locators.py"
    assert t.locator_class == "LoginPageLocators"
    assert t.source == "reuse"
    assert [mm["name"] for mm in t.missing_methods] == ["missing"]


# ---------------------------------------------------------------------------
# Mismatch dataclass smoke test — the as_dict shape is what reconcile-result
# .json downstream depends on; guard it against drift.
# ---------------------------------------------------------------------------


def test_mismatch_as_dict_carries_required_fields():
    from qtea.codegen_reconcile import CallSite

    cs = CallSite(
        test_file="tests/t.py", line=42, obj_name="login_page",
        method_name="missing", arity=2, kw_names=["timeout"],
        snippet="login_page.missing(1, timeout=5)",
    )
    m = Mismatch(
        kind="method_not_found", call_site=cs,
        resolved_pom="LoginPage", pom_file="pages/login_page.py",
        existing_methods=["other"],
    )
    d = m.as_dict()
    assert d["kind"] == "method_not_found"
    assert d["resolved_pom"] == "LoginPage"
    assert d["pom_file"] == "pages/login_page.py"
    assert d["existing_methods"] == ["other"]
    assert d["call_site"]["method_name"] == "missing"
    assert d["call_site"]["arity"] == 2


# ---------------------------------------------------------------------------
# Regression tests for the v1 bug-fix pass.
# Each test pins a specific failure mode the adversarial review caught.
# ---------------------------------------------------------------------------


def _reconcile_python_pair(tmp_path: Path, pom_src: str, test_src: str):
    pom_rel = "pages/login_page.py"
    test_rel = "tests/qtea_x_test.py"
    _touch(tmp_path / pom_rel, pom_src)
    _touch(tmp_path / test_rel, test_src)
    return reconcile_codegen(
        [tmp_path / test_rel],
        [{"file": pom_rel, "class_name": "LoginPage"}],
        tmp_path,
        "python",
    )


def _reconcile_js_pair(
    tmp_path: Path, pom_src: str, test_src: str, *,
    language: str = "typescript", ext: str = "ts",
):
    pom_rel = f"pages/login_page.{ext}"
    test_rel = f"tests/qtea_x_test.{ext}"
    _touch(tmp_path / pom_rel, pom_src)
    _touch(tmp_path / test_rel, test_src)
    return reconcile_codegen(
        [tmp_path / test_rel],
        [{"file": pom_rel, "class_name": "LoginPage"}],
        tmp_path,
        language,
    )


def test_python_pom_with_default_does_not_false_match_arity(tmp_path: Path):
    """`def fill(self, name, email=None)` accepts `pom.fill('a')` — no arity_mismatch."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n    def fill(self, name, email=None):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.fill('a')\n",
    )
    assert recon.mismatches == [], (
        f"Default values must make the POM def flexible; got {recon.mismatches!r}"
    )


def test_python_pom_with_var_kw_args_does_not_false_match(tmp_path: Path):
    """`def foo(self, *args, **kwargs)` accepts any call shape."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n    def foo(self, *args, **kwargs):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.foo(1, 2, 3, key='v')\n",
    )
    assert recon.mismatches == []


def test_python_call_with_spread_skips_arity_check(tmp_path: Path):
    """Caller `pom.foo(*xs)` — runtime arity is unknown; never flag arity_mismatch."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n    def foo(self, a, b):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    args = (1, 2)\n"
        "    login_page.foo(*args)\n",
    )
    assert recon.mismatches == []


def test_js_string_with_double_slash_does_not_corrupt_following_calls(tmp_path: Path):
    """A URL like `'http://x'` must not eat the rest of the file via comment-strip."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n  click() {}\n}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'const url = "http://example.com";\n'
        'test("x", async () => { await loginPage.click(); });\n',
    )
    # The `click` call after the URL must be discovered AND match the POM.
    assert recon.call_sites_checked >= 1, (
        f"URL with `//` corrupted scan; calls_checked={recon.call_sites_checked}"
    )
    assert recon.mismatches == []


def test_js_multiline_method_chain_matched(tmp_path: Path):
    """Playwright fluent style `page\\n  .foo()` must be discovered."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n  submit() {}\n}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'test("x", async () => {\n'
        '  await loginPage\n'
        '    .submit();\n'
        '});\n',
    )
    assert recon.call_sites_checked == 1
    assert recon.mismatches == []


def test_js_nested_parens_correct_arity(tmp_path: Path):
    """`page.foo(bar(), 'x')` is arity 2, not 1."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n  foo(a) {}\n}\n",  # arity 1 def
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'function bar() { return 1; }\n'
        'test("x", async () => { await loginPage.foo(bar(), "x"); });\n',
    )
    # foo(a) has arity 1, caller has arity 2 → mismatch
    assert len(recon.mismatches) == 1
    assert recon.mismatches[0].kind == "arity_mismatch"
    assert recon.mismatches[0].call_site.arity == 2


def test_js_optional_chaining_matched(tmp_path: Path):
    """`page?.foo()` must resolve like `page.foo()`."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n  foo() {}\n}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'test("x", async () => { await loginPage?.foo(); });\n',
    )
    assert recon.call_sites_checked == 1
    assert recon.mismatches == []


def test_ts_generic_method_def_matched(tmp_path: Path):
    """`getValue<T>(): T` must register as a POM method (no spurious not-found)."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n"
        "  getValue<T>(): T { return null as T; }\n"
        "}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'test("x", async () => { await loginPage.getValue(); });\n',
    )
    assert recon.mismatches == [], (
        f"Generic method def missed; mismatches={recon.mismatches!r}"
    )


def test_js_pom_method_with_function_type_param_arity_correct(tmp_path: Path):
    """`foo(fn: () => void)` is arity 1; nested `()` in the type must not break parsing."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n"
        "  foo(fn: () => void): void { fn(); }\n"
        "}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage = new LoginPage();\n'
        'test("x", async () => { await loginPage.foo(() => {}); });\n',
    )
    # foo arity is 1, caller arity is 1 → no mismatch
    assert recon.mismatches == [], (
        f"Function-type param broke arity parse; mismatches={recon.mismatches!r}"
    )


def test_ts_typed_const_declaration_resolves_alias(tmp_path: Path):
    """`const x: T = new X()` must register the alias just like `const x = new X()`."""
    recon = _reconcile_js_pair(
        tmp_path,
        "export class LoginPage {\n  submit() {}\n}\n",
        'import { LoginPage } from "./pages/login_page";\n'
        'const loginPage: LoginPage = new LoginPage();\n'
        'test("x", async () => { await loginPage.submit(); });\n',
    )
    assert recon.call_sites_checked == 1
    assert recon.mismatches == []


def test_playwright_page_fixture_not_resolved_to_pom_named_page(tmp_path: Path):
    """A POM literally named `Page` must not capture every `page.click()` in tests."""
    # POM happens to be named Page (uncommon but legal — e.g. a base class).
    pom_rel = "pages/base.py"
    test_rel = "tests/qtea_x_test.py"
    _touch(
        tmp_path / pom_rel,
        "class Page:\n    def open(self):\n        pass\n",
    )
    _touch(
        tmp_path / test_rel,
        "from pages.base import Page\n\n"
        "def test_x(page):\n"
        "    page.click('#x')\n"
        "    page.goto('/')\n",
    )
    recon = reconcile_codegen(
        [tmp_path / test_rel],
        [{"file": pom_rel, "class_name": "Page"}],
        tmp_path,
        "python",
    )
    # The bare `page` fixture is reserved — no call site should resolve to the
    # Page POM, otherwise `page.click` / `page.goto` would flag as not_found.
    assert recon.call_sites_checked == 0
    assert recon.mismatches == []


def test_mismatches_to_pom_tasks_uses_manifest_when_no_original_task(tmp_path: Path):
    """When the test calls a POM not in the original plan, the synthesised
    task must still pick up locator_file / locator_class from the manifest."""
    from qtea.codegen_reconcile import CallSite

    cs = CallSite(
        test_file="tests/qtea_x_test.py", line=10, obj_name="dashboard_page",
        method_name="open", arity=0, kw_names=[],
        snippet="dashboard_page.open()",
    )
    mm = Mismatch(
        kind="method_not_found", call_site=cs,
        resolved_pom="DashboardPage", pom_file="pages/dashboard_page.py",
        existing_methods=[],
    )
    tasks = mismatches_to_pom_tasks(
        [mm],
        original_pom_tasks={},  # POM wasn't in the plan
        manifest_pom_files=[{
            "file": "pages/dashboard_page.py",
            "class_name": "DashboardPage",
            "locator_file": "pages/locators/dashboard_page_locators.py",
            "locator_class": "DashboardPageLocators",
        }],
    )
    assert set(tasks.keys()) == {"pages/dashboard_page.py"}
    t = tasks["pages/dashboard_page.py"]
    assert t.locator_file == "pages/locators/dashboard_page_locators.py"
    assert t.locator_class == "DashboardPageLocators"
    assert [mm["name"] for mm in t.missing_methods] == ["open"]


def test_typo_detected_emits_likely_typo_with_suggestion(tmp_path: Path):
    """Single-char typo on a long method name → `likely_typo` with suggestion."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n"
        "    def submit_form(self):\n        pass\n"
        "    def cancel_form(self):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.sumbit_form()\n",  # typo of submit_form (distance 1)
    )
    typos = [m for m in recon.mismatches if m.kind == "likely_typo"]
    assert len(typos) == 1, (
        f"Expected one likely_typo; got mismatches={recon.mismatches!r}"
    )
    assert typos[0].suggested_method == "submit_form"
    assert typos[0].call_site.method_name == "sumbit_form"
    # No method_not_found should also be emitted for this call site.
    assert not [m for m in recon.mismatches if m.kind == "method_not_found"]


def test_typo_on_short_method_name_falls_through_to_method_not_found(tmp_path: Path):
    """`go` is too short for a typo claim (length < 5) → plain method_not_found."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n    def do(self):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.go()\n",  # length 2, distance 1 from "do" — but too short
    )
    assert len(recon.mismatches) == 1
    assert recon.mismatches[0].kind == "method_not_found", (
        f"Short names must not flag as typos; got {recon.mismatches!r}"
    )
    assert recon.mismatches[0].suggested_method is None


def test_typo_too_far_emits_method_not_found(tmp_path: Path):
    """`download_csv` vs `submit_form` is distance > 2 → method_not_found."""
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n    def submit_form(self):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.download_csv()\n",
    )
    assert len(recon.mismatches) == 1
    assert recon.mismatches[0].kind == "method_not_found"
    assert recon.mismatches[0].suggested_method is None


def test_typo_ambiguous_tie_falls_through_to_method_not_found(tmp_path: Path):
    """Two existing methods equidistant from the called name → no suggestion,
    fall through to plain method_not_found (ambiguous tie)."""
    # `submit_form` is distance 1 from BOTH `submit_forn` and `subnit_form`.
    recon = _reconcile_python_pair(
        tmp_path,
        "class LoginPage:\n"
        "    def submit_forn(self):\n        pass\n"
        "    def subnit_form(self):\n        pass\n",
        "from pages.login_page import LoginPage\n\n"
        "def test_x(page):\n"
        "    login_page = LoginPage(page)\n"
        "    login_page.submit_form()\n",
    )
    assert len(recon.mismatches) == 1
    assert recon.mismatches[0].kind == "method_not_found", (
        f"Ambiguous tie must not commit to a suggestion; got {recon.mismatches!r}"
    )
    assert recon.mismatches[0].suggested_method is None


def test_likely_typo_excluded_from_autopatch_tasks(tmp_path: Path):
    """`mismatches_to_pom_tasks` must NEVER synthesise a patch for a typo —
    otherwise a stub for `sumbit_form` would be added, masking the test bug."""
    from qtea.codegen_reconcile import CallSite

    cs = CallSite(
        test_file="tests/qtea_x_test.py", line=10,
        obj_name="login_page", method_name="sumbit_form",
        arity=0, kw_names=[], snippet="login_page.sumbit_form()",
    )
    typo_mismatch = Mismatch(
        kind="likely_typo", call_site=cs,
        resolved_pom="LoginPage", pom_file="pages/login_page.py",
        existing_methods=["submit_form"],
        suggested_method="submit_form",
    )
    tasks = mismatches_to_pom_tasks(
        [typo_mismatch],
        original_pom_tasks={"pages/login_page.py": _make_orig_task()},
    )
    assert tasks == {}, (
        f"likely_typo must NEVER produce a patch task; got {tasks!r}"
    )


def _make_orig_task():
    from qtea.steps.s08_codegen import _PomTask
    return _PomTask(
        pom_name="LoginPage", pom_file="pages/login_page.py",
        source="reuse", from_path="pages/login_page.py",
        at_path="pages/login_page.py",
        locator_file=None, locator_class=None,
    )


def test_parse_error_call_site_line_is_at_least_one(tmp_path: Path):
    """parse_error mismatches must satisfy schema's `line >= 1` constraint."""
    pom_rel = "pages/login_page.py"
    test_rel = "tests/qtea_broken_test.py"
    _touch(tmp_path / pom_rel, "class LoginPage:\n    def x(self):\n        pass\n")
    _touch(tmp_path / test_rel, "def test_x(:\n    invalid syntax\n")  # SyntaxError
    recon = reconcile_codegen(
        [tmp_path / test_rel],
        [{"file": pom_rel, "class_name": "LoginPage"}],
        tmp_path,
        "python",
    )
    parse_errors = [m for m in recon.mismatches if m.kind == "parse_error"]
    assert len(parse_errors) == 1
    assert parse_errors[0].call_site.line >= 1, (
        f"line={parse_errors[0].call_site.line} violates schema minimum=1"
    )


# ---------------------------------------------------------------------------
# Fix 2: fixture reconciliation
# ---------------------------------------------------------------------------


_FIX_FILE_TWO_DEFINED = """\
import pytest


@pytest.fixture(scope="function")
def gemini_nav_locale_en():
    yield


@pytest.fixture(scope="function")
def mobile_viewport(page):
    page.set_viewport_size({"width": 390, "height": 844})
    yield
"""


_FIX_FILE_ONLY_MOBILE = """\
import pytest


@pytest.fixture(scope="function")
def mobile_viewport(page):
    page.set_viewport_size({"width": 390, "height": 844})
    yield
"""


def _plan_with_create_fixtures(file_rel: str, names: list[str]) -> dict:
    """Build a minimal plan that declares `names` as create-fixtures at file_rel."""
    return {
        "test_cases": [
            {
                "id": f"TC-{i}",
                "fixtures": [
                    {"name": name, "source": "create", "at": file_rel}
                ],
            }
            for i, name in enumerate(names)
        ]
    }


def test_reconcile_fixtures_no_mismatches_when_all_present(tmp_path: Path):
    file_rel = "tests/fixtures/qtea_nav.py"
    _touch(tmp_path / file_rel, _FIX_FILE_TWO_DEFINED)
    plan = _plan_with_create_fixtures(
        file_rel, ["gemini_nav_locale_en", "mobile_viewport"],
    )
    scanned, mismatches = reconcile_fixtures(plan, tmp_path)
    assert scanned == 1
    assert mismatches == []


def test_reconcile_fixtures_symbol_missing_when_file_lacks_def(tmp_path: Path):
    """The Phase A4 race condition reproducer: 1 fixture survived, 5 declared."""
    file_rel = "tests/fixtures/qtea_nav.py"
    _touch(tmp_path / file_rel, _FIX_FILE_ONLY_MOBILE)
    plan = _plan_with_create_fixtures(
        file_rel,
        [
            "gemini_nav_locale_en",
            "gemini_nav_locale_de",
            "gtag_spy",
            "gtag_removed",
            "unauthenticated_context",
            "mobile_viewport",
        ],
    )
    scanned, mismatches = reconcile_fixtures(plan, tmp_path)
    assert scanned == 1
    missing_names = sorted(m.name for m in mismatches)
    assert missing_names == [
        "gemini_nav_locale_de",
        "gemini_nav_locale_en",
        "gtag_removed",
        "gtag_spy",
        "unauthenticated_context",
    ]
    for m in mismatches:
        assert m.kind == "fixture_symbol_missing"
        assert m.expected_file == file_rel
        assert "mobile_viewport" in m.existing_symbols


def test_reconcile_fixtures_file_missing(tmp_path: Path):
    file_rel = "tests/fixtures/does_not_exist.py"
    plan = _plan_with_create_fixtures(file_rel, ["foo", "bar"])
    scanned, mismatches = reconcile_fixtures(plan, tmp_path)
    assert scanned == 1
    assert len(mismatches) == 2
    assert all(m.kind == "fixture_file_missing" for m in mismatches)


def test_reconcile_fixtures_reuse_missing_symbol(tmp_path: Path):
    file_rel = "tests/fixtures/chat_setup.py"
    _touch(tmp_path / file_rel, _FIX_FILE_ONLY_MOBILE)
    plan = {
        "test_cases": [{
            "id": "TC-1",
            "fixtures": [{
                "name": "chat_page",
                "source": "reuse",
                "from": f"{file_rel}:chat_page",
            }],
        }],
    }
    _, mismatches = reconcile_fixtures(plan, tmp_path)
    assert len(mismatches) == 1
    m = mismatches[0]
    assert m.kind == "fixture_symbol_missing"
    assert m.source == "reuse"
    assert m.name == "chat_page"


def test_reconcile_fixtures_referenced_by_carries_tc_ids(tmp_path: Path):
    file_rel = "tests/fixtures/nav.py"
    _touch(tmp_path / file_rel, _FIX_FILE_ONLY_MOBILE)
    plan = {
        "test_cases": [
            {"id": "TC-A", "fixtures": [
                {"name": "gtag_spy", "source": "create", "at": file_rel},
            ]},
            {"id": "TC-B", "fixtures": [
                {"name": "gtag_spy", "source": "create", "at": file_rel},
            ]},
        ]
    }
    _, mismatches = reconcile_fixtures(plan, tmp_path)
    assert len(mismatches) == 1
    assert sorted(mismatches[0].referenced_by) == ["TC-A", "TC-B"]


def test_fixture_mismatches_to_fixture_tasks_only_synthesises_create(tmp_path: Path):
    """`reuse` mismatches are NOT auto-patched — they need plan/inventory work."""
    file_rel = "tests/fixtures/nav.py"
    plan = {
        "test_cases": [
            {
                "id": "TC-A",
                "fixtures": [
                    {
                        "name": "gtag_spy", "source": "create", "at": file_rel,
                        "yields": "dict", "scope": "function",
                    },
                ],
            },
            {
                "id": "TC-B",
                "fixtures": [
                    {
                        "name": "chat_page", "source": "reuse",
                        "from": "tests/fixtures/chat_setup.py:chat_page",
                    },
                ],
            },
        ]
    }
    fms = [
        FixtureMismatch(
            kind="fixture_symbol_missing", name="gtag_spy",
            expected_file=file_rel, source="create",
            referenced_by=["TC-A"], existing_symbols=[],
        ),
        FixtureMismatch(
            kind="fixture_symbol_missing", name="chat_page",
            expected_file="tests/fixtures/chat_setup.py", source="reuse",
            referenced_by=["TC-B"], existing_symbols=[],
        ),
    ]
    tasks = fixture_mismatches_to_fixture_tasks(fms, plan)
    assert len(tasks) == 1
    assert tasks[0].name == "gtag_spy"
    assert tasks[0].at == file_rel
    assert tasks[0].yields == "dict"


def test_reconcile_fixtures_handles_async_fixture(tmp_path: Path):
    file_rel = "tests/fixtures/async_nav.py"
    _touch(tmp_path / file_rel, (
        "import pytest\n\n"
        "@pytest.fixture\nasync def async_fix():\n    yield\n"
    ))
    plan = _plan_with_create_fixtures(file_rel, ["async_fix"])
    _, mismatches = reconcile_fixtures(plan, tmp_path)
    assert mismatches == []
