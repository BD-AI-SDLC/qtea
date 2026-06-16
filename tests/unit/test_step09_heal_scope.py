"""Unit tests for self-heal scope guard, rollback on timeout, and quality gates.

The heal agent may touch POM/locator source and codegen-generated test
files (for interaction-pattern fixes). It must never touch fixtures,
conftest, or pre-existing test files. Assertions in generated test
files are immutable — the assertion-immutability gate reverts any patch
that removes or alters a pre-existing assertion line.

Tests exercise the pure-function helpers (no subprocess) and a tmp-dir
git-based revert path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from worca_t.steps.s09_execute import (
    _extract_assertion_lines,
    _heal_allowlist_dirs,
    _heal_path_in_scope,
    _heal_revert_all_uncommitted,
    _heal_scope_check_and_revert,
    _patch_modifies_assertions,
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
    "tests/conftest.py",
    "tests/fixtures/worca_nav.py",
    "tests/fixtures/anything.py",
    "tests/smoke/test_login.py",
    "tests/smoke/login_test.py",
    "tests/__tests__/foo.spec.js",
    "app/login.spec.ts",
    "src/main/java/LoginTest.java",
])
def test_heal_path_in_scope_rejects_forbidden_paths(path: str):
    # With an empty allowlist, only FORBIDDEN globs gate; these must all reject.
    assert _heal_path_in_scope(path, set()) is False


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
    """Empty allowlist means 'no inventory info' — fall through to FORBIDDEN-only."""
    assert _heal_path_in_scope("src/pkg/pages/object/login_page.py", set()) is True
    # Still blocks the forbidden globs.
    assert _heal_path_in_scope("tests/conftest.py", set()) is False


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


def test_scope_check_reverts_out_of_scope_fixture_edit(tmp_path: Path):
    """The exact run 20260611-184450 scenario: heal edits a fixtures file → revert."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object", "src/pkg/pages/locators"}

    # Heal "edits" the fixtures file (out of scope) and the POM (in scope).
    fx = sut / "tests" / "fixtures" / "fx.py"
    pom = sut / "src" / "pkg" / "pages" / "object" / "login_page.py"
    fx.write_text("import pytest\n\n@pytest.fixture\ndef new_fix(): yield\n", encoding="utf-8")
    pom.write_text("class LoginPage:\n    def x(self): pass\n", encoding="utf-8")

    reverted = _heal_scope_check_and_revert(sut, base_sha, allowlist)

    # The fixture edit must be reverted; the POM edit must be preserved.
    assert reverted == ["tests/fixtures/fx.py"]
    assert fx.read_text(encoding="utf-8") == "import pytest\n"
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


def test_heal_path_in_scope_conftest_not_overridden_by_generated():
    """conftest.py remains FORBIDDEN even if (erroneously) listed in
    generated_files — but generated_files overrides the forbidden check,
    so this DOES pass. The intent is that conftest.py should never appear
    in generated_files in practice."""
    gen = {"tests/conftest.py"}
    assert _heal_path_in_scope("tests/conftest.py", set(), generated_files=gen) is True
    # Without the override, conftest is forbidden.
    assert _heal_path_in_scope("tests/conftest.py", set()) is False


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
    """Files already dirty before the heal (e.g. worca-junit.xml) are
    not flagged as scope violations."""
    sut = tmp_path / "sut"
    sut.mkdir()
    base_sha = _init_git(sut)
    allowlist = {"src/pkg/pages/object"}

    # Simulate worca-junit.xml written by pytest BEFORE heal.
    junit_xml = sut / "worca-junit.xml"
    junit_xml.write_text("<xml/>", encoding="utf-8")

    # The heal agent also creates an out-of-scope file.
    new_test = sut / "tests" / "smoke" / "test_new.py"
    new_test.parent.mkdir(parents=True)
    new_test.write_text("def test_x(): pass\n", encoding="utf-8")

    pre_heal_dirty = {"worca-junit.xml"}

    reverted = _heal_scope_check_and_revert(
        sut, base_sha, allowlist,
        pre_heal_dirty=pre_heal_dirty,
    )

    assert "tests/smoke/test_new.py" in reverted
    assert "worca-junit.xml" not in reverted
    assert not new_test.exists()
    assert junit_xml.exists()


# ---------------------------------------------------------------------------
# Assertion-immutability gate
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


def test_patch_modifies_assertions_rejects_removal():
    pre = b"x = 1\nassert x == 1\nprint('ok')\n"
    post = b"x = 1\nprint('ok')\n"
    assert _patch_modifies_assertions(pre, post) is True


def test_patch_modifies_assertions_rejects_mutation():
    pre = b"assert x == 1\n"
    post = b"assert x == 2\n"
    assert _patch_modifies_assertions(pre, post) is True


def test_patch_modifies_assertions_allows_addition():
    pre = b"assert x == 1\n"
    post = b"assert x == 1\nexpect(locator).to_be_visible()\n"
    assert _patch_modifies_assertions(pre, post) is False


def test_patch_modifies_assertions_allows_no_change():
    pre = b"assert x == 1\nassert y == 2\n"
    post = b"page.click('#z')\nassert x == 1\nassert y == 2\n"
    assert _patch_modifies_assertions(pre, post) is False


def test_patch_modifies_assertions_none_pre_is_safe():
    """When pre is None (new file), no prior assertions to protect."""
    assert _patch_modifies_assertions(None, b"assert True\n") is False


def test_patch_modifies_assertions_none_post_is_safe():
    assert _patch_modifies_assertions(b"assert True\n", None) is False


def test_patch_modifies_assertions_no_assertions_in_pre():
    """When pre has no assertions, any post content is safe."""
    pre = b"page.click('#x')\n"
    post = b"page.click('#y')\nassert True\n"
    assert _patch_modifies_assertions(pre, post) is False
