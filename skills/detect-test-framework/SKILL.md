---
name: detect-test-framework
description: 'Detect the test framework from manifest files, config files, and file patterns'
---

# Detect Test Framework

Deterministic test framework detection from project manifests, config files, and file patterns. Run the bundled script against the SUT root to get a structured JSON result.

## Detection Tiers

Results are ranked by confidence. The script returns the highest-confidence match.

### Tier 1: Manifest Dependencies (confidence: high)

Parse dependency declarations in manifest files for known test framework packages.

| Manifest | Frameworks Detected |
|---|---|
| `package.json` (dependencies + devDependencies) | `@playwright/test` → playwright-ts, `jest` → jest, `cypress` → cypress, `vitest` → vitest, `mocha` → mocha, `@cucumber/cucumber` → cucumber-js |
| `pyproject.toml` (all dependency groups) | `pytest-playwright` → playwright-py, `pytest` → pytest, `selenium` → selenium-py, `robotframework` → robot, `behave` → behave |
| `pom.xml` (dependencies) | `selenium-java` → selenium-java, `junit-jupiter` → junit5, `cucumber-java` → cucumber-jvm |
| `build.gradle` / `build.gradle.kts` | `testImplementation` lines with framework names |

### Tier 2: Config Files (confidence: medium)

Check for framework-specific configuration files.

| Config File | Framework |
|---|---|
| `playwright.config.ts` / `.js` / `.mts` / `.mjs` | playwright-ts |
| `pytest.ini`, `pyproject.toml [tool.pytest]`, `conftest.py` | pytest |
| `jest.config.*` | jest |
| `cypress.config.*` | cypress |
| `vitest.config.*` | vitest |
| `.mocharc.*` | mocha |
| `robot.yaml` / `robot.toml` | robot |
| `testng.xml` | testng |

### Tier 3: File Patterns (confidence: low)

Count test files matching per-framework glob patterns. Least reliable — used only when Tiers 1-2 produce no result.

## Script Usage

```bash
python skills/detect-test-framework/scripts/detect_framework.py --root /path/to/sut
```

Output (JSON to stdout):
```json
{
  "framework": "playwright-py",
  "confidence": "high",
  "evidence": ["pyproject.toml: pytest-playwright in [tool.poetry.group.dev.dependencies]"],
  "all_detected": [
    {"framework": "playwright-py", "confidence": "high", "evidence": "..."},
    {"framework": "pytest", "confidence": "high", "evidence": "..."}
  ]
}
```

## Monorepo Note

In monorepos, run detection per-module from the module root, not the repo root. Different modules may use different frameworks.
