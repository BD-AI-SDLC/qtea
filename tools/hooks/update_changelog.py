#!/usr/bin/env python3
"""Pre-commit hook: validate CHANGELOG.md format.

Checks:
  - An `## [Unreleased]` section exists.
  - Each `### Added (Mx ...)` entry has at least one bullet point.

Exit code 0 = valid, 1 = issues found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def validate(changelog: Path) -> list[str]:
    if not changelog.exists():
        return [f"{changelog} not found"]

    text = changelog.read_text(encoding="utf-8")
    lines = text.splitlines()
    issues: list[str] = []

    if not any(line.strip() == "## [Unreleased]" for line in lines):
        issues.append("Missing `## [Unreleased]` section")

    in_section = False
    section_name = ""
    has_bullet = False

    for line in lines:
        if re.match(r"^### Added \(M", line):
            if in_section and not has_bullet:
                issues.append(f"Section '{section_name}' has no bullet points")
            in_section = True
            section_name = line.strip()
            has_bullet = False
        elif in_section and line.strip().startswith("- "):
            has_bullet = True
        elif in_section and re.match(r"^#{1,3} ", line):
            if not has_bullet:
                issues.append(f"Section '{section_name}' has no bullet points")
            in_section = False

    if in_section and not has_bullet:
        issues.append(f"Section '{section_name}' has no bullet points")

    return issues


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    changelog = Path(args[0]) if args else Path.cwd() / "CHANGELOG.md"
    issues = validate(changelog)
    for issue in issues:
        print(f"FAIL  {issue}")
    if not issues:
        print("CHANGELOG.md format OK.")
    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
