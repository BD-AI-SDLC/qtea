#!/usr/bin/env python3
"""Detect test framework from manifest files, config files, and file patterns.

Standalone script — no worca_t imports. Outputs JSON to stdout (or to a file
via --output). Follows the same conventions as
skills/acquire-codebase-knowledge/scripts/scan.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

# ── Manifest → framework mappings ──────────────────────────────────────────

_PKG_JSON_DEPS: list[tuple[str, str]] = [
    ("@playwright/test", "playwright-ts"),
    ("cypress", "cypress"),
    ("vitest", "vitest"),
    ("jest", "jest"),
    ("mocha", "mocha"),
    ("@cucumber/cucumber", "cucumber-js"),
    ("webdriverio", "wdio"),
]

_PYPROJECT_DEPS: list[tuple[str, str]] = [
    ("pytest-playwright", "playwright-py"),
    ("playwright", "playwright-py"),
    ("robotframework", "robot"),
    ("selenium", "selenium-py"),
    ("behave", "behave"),
    ("pytest", "pytest"),
]

_POM_ARTIFACTS: list[tuple[str, str]] = [
    ("com.microsoft.playwright", "playwright-java"),
    ("io.cucumber", "cucumber-jvm"),
    ("org.seleniumhq.selenium", "selenium-java"),
    ("io.appium", "appium"),
    ("org.junit.jupiter", "junit5"),
    ("org.testng", "testng"),
]

_GRADLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"com\.microsoft\.playwright", re.I), "playwright-java"),
    (re.compile(r"io\.cucumber", re.I), "cucumber-jvm"),
    (re.compile(r"org\.seleniumhq\.selenium", re.I), "selenium-java"),
    (re.compile(r"org\.junit\.jupiter", re.I), "junit5"),
    (re.compile(r"org\.testng", re.I), "testng"),
]

# ── Config file → framework mappings ──────────────────────────────────────

_CONFIG_FILES: list[tuple[str, str]] = [
    ("playwright.config.ts", "playwright-ts"),
    ("playwright.config.js", "playwright-ts"),
    ("playwright.config.mts", "playwright-ts"),
    ("playwright.config.mjs", "playwright-ts"),
    ("pytest.ini", "pytest"),
    ("jest.config.js", "jest"),
    ("jest.config.ts", "jest"),
    ("jest.config.mjs", "jest"),
    ("jest.config.cjs", "jest"),
    ("cypress.config.ts", "cypress"),
    ("cypress.config.js", "cypress"),
    ("cypress.config.mjs", "cypress"),
    ("cypress.config.cjs", "cypress"),
    ("vitest.config.ts", "vitest"),
    ("vitest.config.js", "vitest"),
    ("vitest.config.mts", "vitest"),
    (".mocharc.yml", "mocha"),
    (".mocharc.yaml", "mocha"),
    (".mocharc.js", "mocha"),
    (".mocharc.cjs", "mocha"),
    ("robot.yaml", "robot"),
    ("robot.toml", "robot"),
    ("testng.xml", "testng"),
    ("wdio.conf.js", "wdio"),
    ("wdio.conf.ts", "wdio"),
]

# ── File glob → framework mappings ────────────────────────────────────────

_FILE_GLOBS: list[tuple[str, list[str]]] = [
    ("playwright-ts", ["**/*.spec.ts", "**/*.spec.js"]),
    ("playwright-py", ["**/test_*.py", "**/*_test.py"]),
    ("pytest", ["**/test_*.py", "**/*_test.py"]),
    ("jest", ["**/*.test.ts", "**/*.test.js", "**/*.test.tsx", "**/*.test.jsx"]),
    ("cypress", ["**/*.cy.ts", "**/*.cy.js"]),
    ("vitest", ["**/*.test.ts", "**/*.test.js"]),
    ("robot", ["**/*.robot"]),
    ("selenium-java", ["**/*Test.java", "**/*Tests.java"]),
    ("junit5", ["**/*Test.java"]),
]


# ── Detection functions ───────────────────────────────────────────────────

def _hit(framework: str, confidence: str, evidence: str) -> dict:
    return {"framework": framework, "confidence": confidence, "evidence": evidence}


def detect_from_manifests(root: Path) -> list[dict]:
    hits: list[dict] = []

    # package.json
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            all_deps: dict[str, str] = {
                **(data.get("dependencies") or {}),
                **(data.get("devDependencies") or {}),
            }
            for dep, fw in _PKG_JSON_DEPS:
                if dep in all_deps:
                    hits.append(_hit(fw, "high", f"package.json: {dep} in dependencies"))
        except (OSError, json.JSONDecodeError):
            pass

    # pyproject.toml
    pyproj = root / "pyproject.toml"
    if pyproj.is_file() and tomllib is not None:
        try:
            data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
            dep_names: set[str] = set()
            # PEP 621
            for d in data.get("project", {}).get("dependencies", []):
                dep_names.add(re.split(r"[<>=!;\[]", d)[0].strip().lower())
            for group in (data.get("project", {}).get("optional-dependencies") or {}).values():
                for d in group:
                    dep_names.add(re.split(r"[<>=!;\[]", d)[0].strip().lower())
            # Poetry
            for section in ("dependencies", "dev-dependencies"):
                for name in (data.get("tool", {}).get("poetry", {}).get(section) or {}):
                    dep_names.add(name.lower())
            for group in (data.get("tool", {}).get("poetry", {}).get("group") or {}).values():
                for name in (group.get("dependencies") or {}):
                    dep_names.add(name.lower())
            for dep, fw in _PYPROJECT_DEPS:
                if dep.lower() in dep_names:
                    hits.append(_hit(fw, "high", f"pyproject.toml: {dep} in dependencies"))
        except (OSError, ValueError, KeyError):
            pass

    # pyproject.toml [tool.pytest] section (config, not dep)
    if pyproj.is_file() and tomllib is not None:
        try:
            data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
            if data.get("tool", {}).get("pytest"):
                if not any(h["framework"] == "pytest" for h in hits):
                    hits.append(_hit("pytest", "medium", "pyproject.toml: [tool.pytest] section"))
        except (OSError, ValueError):
            pass

    # pom.xml
    pom = root / "pom.xml"
    if pom.is_file():
        try:
            tree = ET.parse(pom)  # noqa: S314 — trusted local file
            root_el = tree.getroot()
            ns = ""
            m = re.match(r"\{(.+)\}", root_el.tag)
            if m:
                ns = m.group(1)
            nsmap = {"m": ns} if ns else {}
            dep_els = (
                root_el.findall(".//m:dependency", nsmap) if ns
                else root_el.findall(".//dependency")
            )
            for dep_el in dep_els:
                group = dep_el.findtext(f"{{{ns}}}groupId" if ns else "groupId") or ""
                for artifact_prefix, fw in _POM_ARTIFACTS:
                    if group.startswith(artifact_prefix):
                        hits.append(_hit(fw, "high", f"pom.xml: {group}"))
        except (OSError, ET.ParseError):
            pass

    # build.gradle / build.gradle.kts
    for gradle_name in ("build.gradle", "build.gradle.kts"):
        gradle = root / gradle_name
        if gradle.is_file():
            try:
                text = gradle.read_text(encoding="utf-8")
                for pat, fw in _GRADLE_PATTERNS:
                    if pat.search(text):
                        hits.append(_hit(fw, "high", f"{gradle_name}: {fw} dependency"))
            except OSError:
                pass

    return hits


def detect_from_configs(root: Path) -> list[dict]:
    hits: list[dict] = []
    seen: set[str] = set()
    for filename, fw in _CONFIG_FILES:
        if fw in seen:
            continue
        if (root / filename).is_file():
            hits.append(_hit(fw, "medium", f"config file: {filename}"))
            seen.add(fw)

    # conftest.py → pytest (if not already detected)
    if "pytest" not in seen and (root / "conftest.py").is_file():
        hits.append(_hit("pytest", "medium", "config file: conftest.py"))

    return hits


def detect_from_globs(root: Path) -> list[dict]:
    hits: list[dict] = []
    for fw, patterns in _FILE_GLOBS:
        count = 0
        for pat in patterns:
            count += len(list(root.glob(pat)))
            if count >= 3:
                break
        if count >= 3:
            hits.append(_hit(fw, "low", f"file patterns: {count}+ test files"))
    return hits


# ── Confidence ranking ────────────────────────────────────────────────────

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

_FRAMEWORK_PRIORITY = [
    "playwright-py", "playwright-ts", "playwright-java",
    "cypress", "pytest", "jest", "vitest",
    "selenium-java", "selenium-py", "robot",
    "junit5", "testng", "mocha", "wdio",
    "cucumber-jvm", "cucumber-js", "behave", "appium",
]


def detect_framework(root: Path) -> dict:
    all_hits: list[dict] = []
    all_hits.extend(detect_from_manifests(root))
    all_hits.extend(detect_from_configs(root))
    all_hits.extend(detect_from_globs(root))

    if not all_hits:
        return {
            "framework": None,
            "confidence": None,
            "evidence": [],
            "all_detected": [],
        }

    # Deduplicate by framework (keep highest confidence)
    best: dict[str, dict] = {}
    for hit in all_hits:
        fw = hit["framework"]
        if fw not in best or _CONFIDENCE_RANK.get(hit["confidence"], 0) > _CONFIDENCE_RANK.get(best[fw]["confidence"], 0):
            best[fw] = hit

    deduped = sorted(
        best.values(),
        key=lambda h: (
            -_CONFIDENCE_RANK.get(h["confidence"], 0),
            _FRAMEWORK_PRIORITY.index(h["framework"]) if h["framework"] in _FRAMEWORK_PRIORITY else 999,
        ),
    )

    winner = deduped[0]
    return {
        "framework": winner["framework"],
        "confidence": winner["confidence"],
        "evidence": [winner["evidence"]],
        "all_detected": deduped,
    }


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect test framework from project manifests, configs, and file patterns.",
    )
    parser.add_argument(
        "--root", default=".", type=Path,
        help="SUT root directory (default: current directory)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write JSON to file instead of stdout",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    result = detect_framework(root)
    text = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
