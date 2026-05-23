"""Tests for tools/check_md_size.py markdown size enforcement."""

from __future__ import annotations

from pathlib import Path

from tools.check_md_size import HARD_LIMIT, SOFT_LIMIT, check, count_lines


def test_no_violations(tmp_path: Path):
    (tmp_path / "ok.md").write_text("# Short\n\nHello.\n", encoding="utf-8")
    assert check(tmp_path) == 0


def test_soft_limit_warns_but_passes(tmp_path: Path):
    content = "\n".join(f"line {i}" for i in range(SOFT_LIMIT + 10))
    (tmp_path / "big.md").write_text(content, encoding="utf-8")
    assert check(tmp_path) == 0


def test_hard_limit_fails(tmp_path: Path):
    content = "\n".join(f"line {i}" for i in range(HARD_LIMIT + 10))
    (tmp_path / "huge.md").write_text(content, encoding="utf-8")
    assert check(tmp_path) == 1


def test_strict_mode_fails_on_soft_limit(tmp_path: Path):
    content = "\n".join(f"line {i}" for i in range(SOFT_LIMIT + 10))
    (tmp_path / "big.md").write_text(content, encoding="utf-8")
    assert check(tmp_path, strict=True) == 1


def test_excluded_dirs_are_skipped(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    content = "\n".join(f"line {i}" for i in range(HARD_LIMIT + 10))
    (agents_dir / "huge.md").write_text(content, encoding="utf-8")
    assert check(tmp_path) == 0


def test_excluded_files_are_skipped(tmp_path: Path):
    content = "\n".join(f"line {i}" for i in range(HARD_LIMIT + 10))
    (tmp_path / "final_plan_implementation.md").write_text(content, encoding="utf-8")
    assert check(tmp_path) == 0


def test_count_lines(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    assert count_lines(f) == 3


def test_empty_dir(tmp_path: Path):
    assert check(tmp_path) == 0
