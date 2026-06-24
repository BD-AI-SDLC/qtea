"""One-shot rename script: worca-t -> qtea across all text files."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Files/dirs to skip
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist", "build"}
SKIP_FILES = {"CHANGELOG.md", "rename_to_qtea.py"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".whl", ".egg"}

# Ordered replacements — longer/more-specific first to avoid partial matches
REPLACEMENTS = [
    # Env var prefix (MUST be first)
    ("WORCA_T_", "QTEA_"),
    # Sentinel marker
    ("__WORCA_T_TBD__", "__QTEA_TBD__"),
    # Runtime module names (before generic worca_t / worca-t replacement)
    ("worca_t_runtime", "qtea_runtime"),
    ("worca-t-runtime", "qtea-runtime"),
    # Specific internal identifiers
    ("worca_t_step_metrics", "qtea_step_metrics"),
    ("_worca_t_keep", "_qtea_keep"),
    # Python module path (underscore form)
    ("worca_t", "qtea"),
    # Display title forms (before hyphen form to avoid partial match issues)
    ("Worca-T", "QTea"),
    ("Worca T", "QTea"),
    # Hyphenated CLI/package name
    ("worca-t", "qtea"),
    # Workspace directory (.worca-t already covered by worca-t above since . is literal)
    (".worca-t", ".qtea"),
    # Generated class prefix (capitalized)
    ("WorcaT", "QteaT"),   # WorcaT.java variants (after specific file renames)
    ("Worca", "Qtea"),     # remaining Worca class prefixes
    # Schema URIs
    ("https://worca-t.dev/schemas/", "https://qtea.dev/schemas/"),
    ("https://worca-t/schemas/", "https://qtea/schemas/"),
    # Catch-all for remaining standalone "worca" (comments, Java packages, adjectives)
    # e.g. "worca-generated", "worca-junit.xml", "com.worca.runtime", "worca.tbd"
    ("worca", "qtea"),
]


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.name in SKIP_FILES:
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    return False


def process_file(path: Path) -> bool:
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return False

    content = original
    for old, new in REPLACEMENTS:
        content = content.replace(old, new)

    if content != original:
        path.write_text(content, encoding="utf-8")
        return True
    return False


def main():
    changed = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if should_skip(path):
            continue
        if process_file(path):
            changed.append(path.relative_to(ROOT))

    print(f"Updated {len(changed)} files:")
    for p in sorted(changed):
        print(f"  {p}")


if __name__ == "__main__":
    main()
