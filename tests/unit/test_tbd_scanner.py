"""Tests for qtea.tbd_scanner."""

from __future__ import annotations

from pathlib import Path

from qtea.tbd_scanner import (
    detect_language,
    scan_file,
    scan_tbd_intents,
)

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_detect_language_covers_supported_extensions():
    assert detect_language(Path("x.py")) == "python"
    assert detect_language(Path("x.ts")) == "typescript"
    assert detect_language(Path("x.tsx")) == "typescript"
    assert detect_language(Path("x.js")) == "javascript"
    assert detect_language(Path("x.jsx")) == "javascript"
    assert detect_language(Path("x.mjs")) == "javascript"
    assert detect_language(Path("x.java")) == "java"
    assert detect_language(Path("x.txt")) is None
    assert detect_language(Path("README.md")) is None


# ---------------------------------------------------------------------------
# Python — call-site detection
# ---------------------------------------------------------------------------


def test_python_assignment_form_captures_constant(tmp_path: Path):
    src = tmp_path / "locators.py"
    src.write_text(
        "from tests.qtea_runtime import tbd\n"
        "\n"
        "class LoginLocators:\n"
        "    LOGIN_BUTTON = tbd(\"sign in button\")\n"
        "    EMAIL_INPUT = tbd('username input')\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 2
    by_name = {r.constant_name: r for r in results}
    assert by_name["LOGIN_BUTTON"].intent == "sign in button"
    assert by_name["LOGIN_BUTTON"].language == "python"
    assert by_name["EMAIL_INPUT"].intent == "username input"
    assert by_name["LOGIN_BUTTON"].line == 4
    assert by_name["EMAIL_INPUT"].line == 5


def test_python_inline_form_no_constant_name(tmp_path: Path):
    src = tmp_path / "test_x.py"
    src.write_text(
        "def test_x(page):\n"
        "    page.locator(tbd(\"submit\")).click()\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].constant_name is None
    assert results[0].intent == "submit"
    assert results[0].line == 2


def test_python_skips_tbd_in_line_comment(tmp_path: Path):
    src = tmp_path / "x.py"
    src.write_text(
        "# example: X = tbd(\"in comment\")\n"
        "Y = tbd(\"real\")\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "real"
    assert results[0].line == 2


def test_python_quoted_hash_in_intent_does_not_truncate(tmp_path: Path):
    src = tmp_path / "x.py"
    src.write_text(
        'NAME = tbd("the #header anchor")\n',
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "the #header anchor"


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------


def test_typescript_const_assignment(tmp_path: Path):
    src = tmp_path / "locators.ts"
    src.write_text(
        "import { tbd } from \"./qtea-runtime\";\n"
        "export const LOGIN_BUTTON = tbd(\"sign in button\");\n"
        "export const EMAIL = tbd(`username input`);\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 2
    intents = {r.intent for r in results}
    assert intents == {"sign in button", "username input"}


def test_typescript_qtea_prefixed_call(tmp_path: Path):
    src = tmp_path / "page.ts"
    src.write_text(
        "page.locator(qtea.tbd(\"close button\")).click();\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "close button"


def test_javascript_skips_line_comment(tmp_path: Path):
    src = tmp_path / "x.js"
    src.write_text(
        "// const FOO = tbd(\"commented\");\n"
        "const BAR = tbd(\"real\");\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "real"
    assert results[0].constant_name == "BAR"


def test_typescript_block_comment_stripped(tmp_path: Path):
    src = tmp_path / "x.ts"
    src.write_text(
        "/* example:\n"
        "   const X = tbd(\"in block\");\n"
        "*/\n"
        "const Y = tbd(\"real\");\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "real"
    # Line number preserved through block-comment padding.
    assert results[0].line == 4


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def test_java_static_final_captures_constant(tmp_path: Path):
    src = tmp_path / "LoginLocators.java"
    src.write_text(
        "package com.example;\n"
        "import com.qtea.runtime.Tbd;\n"
        "import org.openqa.selenium.By;\n"
        "\n"
        "public class LoginLocators {\n"
        "    public static final By LOGIN_BUTTON = Tbd.of(\"sign in button\");\n"
        "}\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "sign in button"
    assert results[0].constant_name == "LOGIN_BUTTON"
    assert results[0].language == "java"


def test_java_inline_use_no_constant_name(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text(
        "driver.findElement(Tbd.of(\"submit\")).click();\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].constant_name is None
    assert results[0].intent == "submit"


def test_java_block_comment_stripped(tmp_path: Path):
    src = tmp_path / "X.java"
    src.write_text(
        "/**\n"
        " * Example: By X = Tbd.of(\"in javadoc\");\n"
        " */\n"
        "public class X {\n"
        "    public static final By Y = Tbd.of(\"real\");\n"
        "}\n",
        encoding="utf-8",
    )
    results = scan_file(src)
    assert len(results) == 1
    assert results[0].intent == "real"


# ---------------------------------------------------------------------------
# scan_tbd_intents — directory walking + ordering
# ---------------------------------------------------------------------------


def test_scan_directory_recursive(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "first.py").write_text(
        'X = tbd("alpha")\n', encoding="utf-8",
    )
    (tmp_path / "a" / "b" / "second.py").write_text(
        'Y = tbd("beta")\n', encoding="utf-8",
    )
    results = scan_tbd_intents([tmp_path])
    intents = sorted(r.intent for r in results)
    assert intents == ["alpha", "beta"]


def test_scan_skips_vendor_dirs(tmp_path: Path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules" / "x" / "evil.py").write_text(
        'X = tbd("vendor leak")\n', encoding="utf-8",
    )
    (tmp_path / ".venv" / "lib" / "pkg.py").write_text(
        'Y = tbd("venv leak")\n', encoding="utf-8",
    )
    (tmp_path / "src" / "real.py").write_text(
        'Z = tbd("legitimate")\n', encoding="utf-8",
    )
    results = scan_tbd_intents([tmp_path])
    assert {r.intent for r in results} == {"legitimate"}


def test_scan_results_are_sorted_for_determinism(tmp_path: Path):
    (tmp_path / "b.py").write_text(
        'Y = tbd("bee")\n', encoding="utf-8",
    )
    (tmp_path / "a.py").write_text(
        'A = tbd("ay")\n', encoding="utf-8",
    )
    results = scan_tbd_intents([tmp_path])
    # Sorted by (file, line) — file paths alphabetical.
    assert [r.intent for r in results] == ["ay", "bee"]


def test_scan_returns_sut_relative_paths_when_sut_root_given(tmp_path: Path):
    sut = tmp_path / "sut"
    (sut / "tests").mkdir(parents=True)
    src = sut / "tests" / "x.py"
    src.write_text('X = tbd("a")\n', encoding="utf-8")
    results = scan_tbd_intents([sut], sut_root=sut)
    assert len(results) == 1
    # Path is relative to sut_root.
    assert str(results[0].file).replace("\\", "/") == "tests/x.py"


def test_scan_file_returns_empty_for_unsupported_extension(tmp_path: Path):
    src = tmp_path / "x.txt"
    src.write_text("tbd(\"not code\")\n", encoding="utf-8")
    assert scan_file(src) == []


def test_scan_handles_unreadable_file_gracefully(tmp_path: Path):
    # Pass a nonexistent file — scan_file should return [] rather than raise.
    assert scan_file(tmp_path / "ghost.py") == []


def test_scan_finds_files_under_hidden_prefixed_ancestor(tmp_path: Path):
    """Regression: when the scan root lives under a hidden-prefixed
    parent (e.g. qtea workspaces at ``~/.qtea/<run>/sut``), the
    hidden-dir filter MUST NOT match the parent and skip every file.
    Previously this silently disabled TBD promotion across every run.
    """
    # Build  ``<tmp>/.qtea/run-abc/sut/src/locators.py`` —
    # the path contains a ``.qtea`` ancestor that the filter would
    # historically reject.
    hidden_root = tmp_path / ".qtea" / "run-abc" / "sut"
    (hidden_root / "src").mkdir(parents=True)
    (hidden_root / "src" / "locators.py").write_text(
        'BUTTON = tbd("primary submit button")\n', encoding="utf-8",
    )
    # Scan the SUT root that itself sits beneath ``.qtea/``.
    results = scan_tbd_intents([hidden_root], sut_root=hidden_root)
    assert len(results) == 1
    assert results[0].intent == "primary submit button"
    # And the in-tree ``.venv`` / ``__pycache__`` / dot-dirs are STILL
    # excluded relative to the scan root.
    (hidden_root / ".venv").mkdir()
    (hidden_root / ".venv" / "leak.py").write_text(
        'X = tbd("venv leak")\n', encoding="utf-8",
    )
    (hidden_root / "__pycache__").mkdir()
    (hidden_root / "__pycache__" / "cache_leak.py").write_text(
        'Y = tbd("cache leak")\n', encoding="utf-8",
    )
    results2 = scan_tbd_intents([hidden_root], sut_root=hidden_root)
    intents = {r.intent for r in results2}
    assert intents == {"primary submit button"}  # leaks correctly excluded
