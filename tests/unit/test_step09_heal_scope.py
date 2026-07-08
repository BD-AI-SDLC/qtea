"""Unit tests for self-heal scope guard, rollback on timeout, and quality gates.

Scope model (see `agents/polyglot-test-fixer.agent.md`): the heal agent may
edit any TEST-SIDE code — POMs, locators, helpers, fixtures, `conftest.py`,
and codegen-generated test files. It must NOT edit application/production
source (which would mask DEV bugs) nor pre-existing SUT-authored test files.
Assertions may be corrected to match the Step-4 expected value but never
weakened or removed.

Tests exercise the pure-function helpers (no subprocess) and a tmp-dir
git-based revert path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qtea.steps.s09.heal_scope import (
    _heal_allowlist_dirs,
    _heal_path_in_scope,
    _heal_revert_all_uncommitted,
    _heal_scope_check_and_revert,
)
from qtea.steps.s09.patch_gates import (
    _extract_assertion_lines,
    _patch_weakens_assertions,
)

# ---------------------------------------------------------------------------
# Allowlist construction
# ---------------------------------------------------------------------------


def test_heal_allowlist_dirs_extracts_pom_and_locator_dirs():
    active_module = {
        "existing_page_objects": [
            {"file": "src/pkg/pages/object/login_page.py"},
            {"file": "src/pkg/pages/object/chat_page.py"},
        ],
        "existing_locators": [
            {"file": "src/pkg/pages/locators/login_locators.py"},
        ],
    }
    dirs = _heal_allowlist_dirs(active_module)
    assert dirs == {
        "src/pkg/pages/object",
        "src/pkg/pages/locators",
    }


def test_heal_allowlist_dirs_empty_when_no_active_module():
    assert _heal_allowlist_dirs(None) == set()
    assert _heal_allowlist_dirs({}) == set()


# ---------------------------------------------------------------------------
# Scope predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "tests/smoke/test_login.py",
    "tests/smoke/login_test.py",
    "tests/__tests__/foo.spec.js",
    "app/login.spec.ts",
    "src/main/java/LoginTest.java",
])
def test_heal_path_in_scope_rejects_pre_existing_test_files(path: str):
    # Pre-existing SUT-authored test files stay off-limits even with an empty
    # allowlist (qtea's own tests are passed via generated_files instead).
    assert _heal_path_in_scope(path, set()) is False


@pytest.mark.parametrize("path", [
    "tests/conftest.py",
    "tests/fixtures/qtea_nav.py",
    "tests/fixtures/anything.py",
])
def test_heal_path_in_scope_allows_test_infra(path: str):
    # Post-relaxation: conftest.py and fixture files are editable test
    # infrastructure, allowed even with no allowlist information.
    assert _heal_path_in_scope(path, set()) is True


def test_heal_path_in_scope_allows_pom_inside_allowlist():
    allowlist = {"src/pkg/pages/object", "src/pkg/pages/locators"}
    assert _heal_path_in_scope(
        "src/pkg/pages/object/login_page.py", allowlist,
    ) is True
    assert _heal_path_in_scope(
        "src/pkg/pages/locators/login_locators.py", allowlist,
    ) is True


def test_heal_path_in_scope_rejects_pom_outside_allowlist():
    allowlist = {"src/pkg/pages/object"}
    # A page object that exists in a different directory must be rejected.
    assert _heal_path_in_scope(
        "src/pkg/other/random_page.py", allowlist,
    ) is False


def test_heal_path_in_scope_permissive_when_allowlist_empty():
    """Empty allowlist means 'no inventory info' — permissive for non-test
    paths; conftest is now editable test infrastructure."""
    assert _heal_path_in_scope("src/pkg/pages/object/login_page.py", set()) is True
    assert _heal_path_in_scope("tests/conftest.py", set()) is True
    # Pre-existing SUT test files still blocked.
    assert _heal_path_in_scope("tests/smoke/test_login.py", set()) is False


def test_heal_path_in_scope_rejects_app_source_outside_allowlist():
    """With a known allowlist, application/production source outside it is
    out-of-scope so a heal cannot mask a DEV bug by editing the code under test."""
    allowlist = {"src/pkg/pages/object", "src/pkg/pages/locators"}
    assert _heal_path_in_scope("src/pkg/services/auth_service.py", allowlist) is False
    assert _heal_path_in_scope("src/pkg/app/main.py", allowlist) is False


# ---------------------------------------------------------------------------
# Revert helpers (tmp-dir git)
# ---------------------------------------------------------------------------


def _init_git(sut: Path) -> str:
    """Init a tmp git repo and commit a baseline. Returns HEAD sha."""
    subprocess.run(
        ["git", "init", "-q"], cwd=sut, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=sut, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=sut, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=sut, check=True,
    )
    (sut / "src" / "pkg" / "pages" / "object").mkdir(parents=True)
    (sut / "src" / "pkg" / "pages" / "locators").mkdir(parents=True)
    (sut / "tests" / "fixtures").mkdir(parents=True)
    (sut / "src" / "pkg" / "pages" / "object" / "login_page.py").write_text(
        "class LoginPage: pass\n", encoding="utf-8",
    )
    (sut / "tests" / "fixtures" / "fx.py").write_text(
        "import pytest\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=sut, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=sut, check=True,
    )
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sut, check=True,
        capture_output=True, text=True,
    )
    return res.stdout.strip()


def test_scope_check_reverts_app_source_edit_preserves_fixture_and_pom(tmp_path: Path):
    """Post-relaxation: a fixture edit is IN scope (preserved); an
    application/production-source edit outside the allowlist is reverted so a
    heal cannot mask a DEV bug by editing the code under test."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object", "src/pkg/pages/locators"}

    # Heal edits: the fixture (in scope), the POM (in scope), and a new
    # application-source file (out of scope → must be reverted).
    fx = sut / "tests" / "fixtures" / "fx.py"
    pom = sut / "src" / "pkg" / "pages" / "object" / "login_page.py"
    app = sut / "src" / "pkg" / "services" / "auth_service.py"
    app.parent.mkdir(parents=True)
    fx.write_text("import pytest\n\n@pytest.fixture\ndef new_fix(): yield\n", encoding="utf-8")
    pom.write_text("class LoginPage:\n    def x(self): pass\n", encoding="utf-8")
    app.write_text("def login(): return True  # masked DEV bug\n", encoding="utf-8")

    reverted = _heal_scope_check_and_revert(sut, base_sha, allowlist)

    # Only the app-source edit is reverted; fixture + POM edits survive.
    assert reverted == ["src/pkg/services/auth_service.py"]
    assert not app.exists()
    assert "new_fix" in fx.read_text(encoding="utf-8")
    assert "def x" in pom.read_text(encoding="utf-8")


def test_scope_check_removes_new_untracked_out_of_scope_file(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object"}

    # Heal creates a new test file (forbidden glob).
    new_test = sut / "tests" / "smoke" / "test_new.py"
    new_test.parent.mkdir(parents=True)
    new_test.write_text("def test_x(): pass\n", encoding="utf-8")

    reverted = _heal_scope_check_and_revert(sut, base_sha, allowlist)
    assert reverted == ["tests/smoke/test_new.py"]
    assert not new_test.exists()


def test_revert_all_uncommitted_clears_in_scope_edits_too(tmp_path: Path):
    """Used on agent-failure (timeout). Even in-scope POM edits must be reverted
    because a partially-completed heal is no safer than an out-of-scope one."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)

    pom = sut / "src" / "pkg" / "pages" / "object" / "login_page.py"
    pom.write_text("class LoginPage:\n    BROKEN_INFLIGHT_EDIT\n", encoding="utf-8")

    reverted = _heal_revert_all_uncommitted(sut, base_sha)
    assert "src/pkg/pages/object/login_page.py" in reverted
    assert "BROKEN_INFLIGHT_EDIT" not in pom.read_text(encoding="utf-8")


def test_scope_check_handles_missing_git_gracefully(tmp_path: Path):
    """A non-git directory must not crash the scope check."""
    sut = tmp_path / "sut"
    sut.mkdir()
    # No git init.
    reverted = _heal_scope_check_and_revert(sut, None, set())
    assert reverted == []


# ---------------------------------------------------------------------------
# Generated-file override
# ---------------------------------------------------------------------------


def test_heal_path_in_scope_allows_generated_test_file():
    """Generated test files override both FORBIDDEN and allowlist checks."""
    gen = {"tests/smoke/test_login.py"}
    assert _heal_path_in_scope("tests/smoke/test_login.py", set(), generated_files=gen) is True


def test_heal_path_in_scope_rejects_non_generated_test_file():
    """Test files NOT in generated_files remain forbidden."""
    gen = {"tests/smoke/test_other.py"}
    assert _heal_path_in_scope("tests/smoke/test_login.py", set(), generated_files=gen) is False


def test_heal_path_in_scope_generated_overrides_allowlist():
    """Generated test file is accepted even when allowlist is non-empty
    and does not contain the test's directory."""
    gen = {"tests/smoke/test_login.py"}
    allowlist = {"src/pkg/pages/object"}
    assert _heal_path_in_scope(
        "tests/smoke/test_login.py", allowlist, generated_files=gen,
    ) is True


def test_heal_path_in_scope_conftest_is_editable_test_infra():
    """conftest.py is editable test infrastructure — in scope with or without
    being listed in generated_files."""
    gen = {"tests/conftest.py"}
    assert _heal_path_in_scope("tests/conftest.py", set(), generated_files=gen) is True
    assert _heal_path_in_scope("tests/conftest.py", set()) is True


def test_scope_check_preserves_generated_test_file_edits(tmp_path: Path):
    """In-scope edits to generated test files survive the scope check."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object"}
    gen = {"tests/smoke/test_login.py"}

    test_dir = sut / "tests" / "smoke"
    test_dir.mkdir(parents=True)
    test_file = test_dir / "test_login.py"
    test_file.write_text("def test_x(): pass\n", encoding="utf-8")

    reverted = _heal_scope_check_and_revert(
        sut, base_sha, allowlist, generated_files=gen,
    )
    assert reverted == []
    assert test_file.exists()


# ---------------------------------------------------------------------------
# Pre-heal dirty exemption
# ---------------------------------------------------------------------------


def test_scope_check_skips_pre_heal_dirty_files(tmp_path: Path):
    """Files already dirty before the heal (e.g. qtea-junit.xml) are
    not flagged as scope violations."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object"}

    # Simulate qtea-junit.xml written by pytest BEFORE heal.
    junit_xml = sut / "qtea-junit.xml"
    junit_xml.write_text("<xml/>", encoding="utf-8")

    # The heal agent also creates an out-of-scope file.
    new_test = sut / "tests" / "smoke" / "test_new.py"
    new_test.parent.mkdir(parents=True)
    new_test.write_text("def test_x(): pass\n", encoding="utf-8")

    pre_heal_dirty = {"qtea-junit.xml"}

    reverted = _heal_scope_check_and_revert(
        sut, base_sha, allowlist,
        pre_heal_dirty=pre_heal_dirty,
    )

    assert "tests/smoke/test_new.py" in reverted
    assert "qtea-junit.xml" not in reverted
    assert not new_test.exists()
    assert junit_xml.exists()


# ---------------------------------------------------------------------------
# Assertion-faithfulness gate (Gap F): corrections allowed, weakening rejected
# ---------------------------------------------------------------------------


def test_extract_assertion_lines_finds_python_asserts():
    src = "x = 1\nassert x == 1\nprint('done')\n"
    lines = _extract_assertion_lines(src)
    assert len(lines) == 1
    assert "assert x == 1" in lines[0]


def test_extract_assertion_lines_finds_expect_calls():
    src = "expect(page.locator('#x')).to_be_visible()\npage.click('#y')\n"
    lines = _extract_assertion_lines(src)
    assert len(lines) == 1


def test_extract_assertion_lines_finds_pytest_raises():
    src = "with pytest.raises(ValueError):\n    func()\n"
    lines = _extract_assertion_lines(src)
    assert len(lines) == 1


def test_extract_assertion_lines_finds_should():
    src = "cy.get('#btn').should('be.visible')\n"
    lines = _extract_assertion_lines(src)
    assert len(lines) == 1


def test_patch_weakens_assertions_rejects_removal():
    pre = b"x = 1\nassert x == 1\nprint('ok')\n"
    post = b"x = 1\nprint('ok')\n"
    assert _patch_weakens_assertions(pre, post) is True


def test_patch_weakens_assertions_allows_value_correction():
    """Correcting an expected value (strong -> strong, same count) is a
    legitimate codegen-transcription fix — NOT a weakening."""
    pre = b"assert x == 1\n"
    post = b"assert x == 2\n"
    assert _patch_weakens_assertions(pre, post) is False


def test_patch_weakens_assertions_rejects_downgrade_to_truthy():
    """Downgrading a concrete comparison to a bare-truthy assert to force a
    green is a weakening."""
    pre = b"assert value == 'noopener noreferrer'\n"
    post = b"assert value\n"
    assert _patch_weakens_assertions(pre, post) is True


def test_patch_weakens_assertions_allows_addition():
    pre = b"assert x == 1\n"
    post = b"assert x == 1\nexpect(locator).to_be_visible()\n"
    assert _patch_weakens_assertions(pre, post) is False


def test_patch_weakens_assertions_allows_no_change():
    pre = b"assert x == 1\nassert y == 2\n"
    post = b"page.click('#z')\nassert x == 1\nassert y == 2\n"
    assert _patch_weakens_assertions(pre, post) is False


def test_patch_weakens_assertions_none_pre_is_safe():
    """When pre is None (new file), no prior assertions to protect."""
    assert _patch_weakens_assertions(None, b"assert True\n") is False


def test_patch_weakens_assertions_none_post_is_safe():
    assert _patch_weakens_assertions(b"assert True\n", None) is False


def test_patch_weakens_assertions_no_assertions_in_pre():
    """When pre has no assertions, any post content is safe."""
    pre = b"page.click('#x')\n"
    post = b"page.click('#y')\nassert True\n"
    assert _patch_weakens_assertions(pre, post) is False
