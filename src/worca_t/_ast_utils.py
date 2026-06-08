"""Shared Python-AST primitives used by the SUT introspection modules.

`url_resolver.py` and `sut_inventory.py` both walk SUT source via `ast.parse`
to find Pydantic `BaseSettings` field aliases, page-object class definitions,
fixture decorators, and similar structural cues. The helpers in this module
encapsulate the patterns that would otherwise be duplicated.

This file intentionally has zero non-stdlib imports: the SUT-introspection
hot path runs hundreds of times per `worca-t run` and any extra dependency
hurts cold-start latency.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

# Walks are bounded by these limits to keep latency predictable on large repos.
MAX_FILE_BYTES = 512_000
SKIP_DIR_NAMES = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".tox", "worca-tests", "dist", "build",
    "target", "out", "coverage", "htmlcov", ".idea", ".vscode",
    ".next", ".nuxt", ".cache", ".turbo",
})


def literal_str(node: ast.AST | None) -> str | None:
    """Return the str value if `node` is a `Constant` str literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def is_basesettings_base(base: ast.expr) -> bool:
    """True when a class base resolves to a name ending in `BaseSettings`."""
    if isinstance(base, ast.Name):
        return base.id == "BaseSettings"
    if isinstance(base, ast.Attribute):
        return base.attr == "BaseSettings"
    return False


def annotation_is_optional(ann: ast.AST | None) -> bool:
    """True for `Optional[X]`, `X | None`, or `None | X` annotations."""
    if ann is None:
        return False
    if isinstance(ann, ast.Subscript):
        value = ann.value
        if isinstance(value, ast.Name) and value.id == "Optional":
            return True
        if isinstance(value, ast.Attribute) and value.attr == "Optional":
            return True
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            if isinstance(side, ast.Constant) and side.value is None:
                return True
            if isinstance(side, ast.Name) and side.id == "None":
                return True
    return False


def extract_env_prefix(class_body: list[ast.stmt]) -> str:
    """Pull `env_prefix` from a nested `class Config:` or `model_config = ...`.

    Non-literal values fall back to an empty prefix; only string literals supported.
    """
    for stmt in class_body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for sub in stmt.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and sub.targets[0].id == "env_prefix"
                ):
                    lit = literal_str(sub.value)
                    if lit is not None:
                        return lit
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "model_config"
            and isinstance(stmt.value, ast.Call)
        ):
            for kw in stmt.value.keywords:
                if kw.arg == "env_prefix":
                    lit = literal_str(kw.value)
                    if lit is not None:
                        return lit
    return ""


def iter_python_files(root: Path, *, contains_hint: bytes | None = None) -> Iterator[Path]:
    """Yield `.py` files under `root`, skipping noise dirs + oversize files.

    If `contains_hint` is provided, files whose raw bytes don't contain that
    substring are skipped (cheap prefilter before invoking the AST parser).
    """
    if not root.exists() or not root.is_dir():
        return
    for src in root.glob("**/*.py"):
        if not src.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in src.parts):
            continue
        try:
            if src.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        if contains_hint is not None:
            try:
                if contains_hint not in src.read_bytes():
                    continue
            except OSError:
                continue
        yield src


def parse_file(src: Path) -> ast.AST | None:
    """Parse a Python source file; return None on SyntaxError / IO error."""
    try:
        raw = src.read_bytes()
    except OSError:
        return None
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(raw, filename=str(src))
    except SyntaxError:
        return None


def relative_posix(src: Path, root: Path) -> str:
    """Return `src` relative to `root` as a POSIX-style path string."""
    try:
        return src.relative_to(root).as_posix()
    except ValueError:
        return src.as_posix()


__all__ = [
    "MAX_FILE_BYTES",
    "SKIP_DIR_NAMES",
    "annotation_is_optional",
    "extract_env_prefix",
    "is_basesettings_base",
    "iter_python_files",
    "literal_str",
    "parse_file",
    "relative_posix",
]
