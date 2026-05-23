#!/usr/bin/env python3
"""Check markdown files against size limits.

Usage:
    python tools/check_md_size.py [--strict] [ROOT_DIR]

Scans *.md files for line-count violations:
  - WARN  when a file exceeds the soft limit (200 lines)
  - FAIL  when a file exceeds the hard limit (500 lines)

Exit code 0 when no hard-limit violations exist (or --strict: no violations at all).
"""

from __future__ import annotations

import sys
from pathlib import Path

SOFT_LIMIT = 200
HARD_LIMIT = 500

EXCLUDED_DIRS = frozenset({
    "agents", "skills", "candidate_agents",
    ".venv", ".worca-t", "node_modules", "__pycache__",
    ".git", ".github",
})

EXCLUDED_FILES = frozenset({
    "final_plan_implementation.md",
})


def _should_skip(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    return any(p in EXCLUDED_DIRS for p in parts) or rel.name in EXCLUDED_FILES


def count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def check(root: Path, *, strict: bool = False) -> int:
    warnings: list[tuple[str, int]] = []
    failures: list[tuple[str, int]] = []

    for md in sorted(root.rglob("*.md")):
        if _should_skip(md, root):
            continue
        lines = count_lines(md)
        rel = md.relative_to(root)
        if lines > HARD_LIMIT:
            failures.append((str(rel), lines))
        elif lines > SOFT_LIMIT:
            warnings.append((str(rel), lines))

    for name, lines in warnings:
        print(f"WARN  {name}: {lines} lines (soft limit {SOFT_LIMIT})")
    for name, lines in failures:
        print(f"FAIL  {name}: {lines} lines (hard limit {HARD_LIMIT})")

    if not warnings and not failures:
        print("All markdown files within limits.")

    if failures:
        return 1
    if strict and warnings:
        return 1
    return 0


def main() -> None:
    strict = "--strict" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    root = Path(args[0]) if args else Path.cwd()
    sys.exit(check(root, strict=strict))


if __name__ == "__main__":
    main()
