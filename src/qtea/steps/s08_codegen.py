"""Step 8: TDD codegen via codegen-violation-fixer.

Inputs: code-modification-plan.json (step 7) + sut_inventory.json (step 6).
The plan is authoritative for placement decisions (which fixtures to reuse,
where new POM methods land, what TBD intents to emit). The inventory is
secondary, used for byte-match locator dedup and style mimicry.

Behavior:
  1. Pre-flight: SUT exists, sut_inventory.json present, code-modification-plan.json
     present, inventory's referenced files actually reachable under
     `<workspace>/sut/`. Any miss → fail in <1s instead of waiting on a 1800s
     agent timeout.
  2. Stage planning artifacts into the step workdir (the agent reads
     them via cwd-relative paths).
  3. Run the agent with `add_dirs=[<workspace>/sut/]` so it can write
     generated tests + page objects DIRECTLY into the SUT clone on the
     `qtea/run-<id>` branch (no per-step copy).
  4. Index the SUT, filter to `qtea_*`-prefixed files (the agent's
     filename-collision convention), enforce non-negotiable rules.
  5. Commit the step's changes to the qtea branch.

Outputs (artifacts/step08/):
  - tbd-index.json            (qtea-only index + violations)
  - generated-files.json      (SUT-relative paths of files this step wrote)
  - violations.log            (only when violations exist)

The generated test bytes live in `<workspace>/sut/` on the branch —
review via `git diff qtea/run-<id>` rather than reading a duplicate copy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re as _re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from qtea._sut_git import commit_step, files_in_commit
from qtea.claude_runner import run_agent
from qtea.codegen_reconcile import (
    fixture_mismatches_to_fixture_tasks,
    mismatches_to_pom_tasks,
    pom_method_signatures,
    reconcile_codegen,
    reconcile_fixtures,
)
from qtea.config import AUTOFIX_MAX_TURNS, package_resource_root, step_timeout
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.parse_check import (
    ParseCheckResult,
    has_degraded_violations,
    run_parse_check,
)
from qtea.parse_check import (
    format_for_fixer as parse_check_format_for_fixer,
)
from qtea.playwright_config_editor import ensure_test_id_attribute
from qtea.preflight import run_preflight
from qtea.runtime.dev_locators import DevLocator, load_dev_locators
from qtea.schemas import is_valid
from qtea.static_check import (
    StaticCheckResult,
    format_for_fixer,
    run_static_check,
)
from qtea.steps.base import Step, StepContext, StepResult
from qtea.test_indexer import (
    IndexResult,
    blocking_violations,
    index_tests,
    resolve_framework,
    violations_summary,
)
from qtea.xpath_rewriter import RewriteReport, XpathSite, rewrite_file

log = get_logger(__name__)

# Phase B.5 auto-patch is intentionally a SINGLE retry. Do NOT convert the
# if-block at the call site into a `while recon.mismatches` loop — a second
# failure must hard-fail to Step 9 so a human sees the real bug instead of
# the orchestrator silently re-extending forever. See plan Phase B.5.
B5_MAX_AUTOPATCH_RETRIES = 1

# Phase B.6 (native static-check gate) mirrors B.5's single-retry philosophy.
# If pyright/tsc still reports in-scope type errors after one violation-fixer
# pass, fail the step — the global MAX_ATTEMPTS=2 will re-run Step 8 from
# the top, which often resolves the issue (the type error usually reflects
# upstream POM/locator drift). Persisting errors after that escalate to
# fix-proposal / human review.
B6_MAX_AUTOPATCH_RETRIES = 1

# Languages B.5 currently understands. Other languages skip reconciliation
# entirely; the StepResult records `b5_skipped=<lang>` so a green B.5 line
# cannot be misread as "that language was covered." The skip is logged at
# WARNING level so it surfaces at the default log level.
_B5_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({
    "python", "typescript", "javascript", "java",
})


def _vendor_jit_runtime(sut_root: Path) -> Path | None:
    """Copy the qtea JIT runtime template into `<sut>/tests/qtea_runtime.py`.

    Returns the destination path on success, or None if the template can't be
    located (best-effort — the absence is logged; Step 8 then falls through
    to the polyglot-test-fixer heal flow for non-Playwright stacks).
    """
    template = (
        package_resource_root() / "_resources" / "runtime" / "qtea_runtime.py.tpl"
    )
    if not template.is_file():
        # Dev tree may not have the template under _resources (e.g. when
        # QTEA_RESOURCE_ROOT points at the repo root). Try src/.
        alt = (
            package_resource_root() / "src" / "qtea" / "_resources"
            / "runtime" / "qtea_runtime.py.tpl"
        )
        if alt.is_file():
            template = alt
        else:
            log.warning(
                "step08.jit_runtime_template_missing",
                tried=[str(template), str(alt)],
                hint="Step 8 will fall through to the heal-flow path.",
            )
            return None
    dest_dir = sut_root / "tests"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "qtea_runtime.py"
    try:
        dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("step08.jit_runtime_vendored", path=str(dest))
        return dest
    except OSError as e:
        log.warning("step08.jit_runtime_vendor_failed", error=str(e))
        return None


def _ensure_conftest_registers_runtime(sut_root: Path) -> None:
    """Make sure at least one conftest.py under `<sut>/tests/` has
    `pytest_plugins` referencing `tests.qtea_runtime`.

    Idempotent — if a conftest already registers it, no change. If no
    conftest exists at the tests/ root, creates a minimal one. If a
    conftest exists but doesn't register the plugin, appends the
    registration line.
    """
    tests_dir = sut_root / "tests"
    if not tests_dir.is_dir():
        return
    conftest = tests_dir / "conftest.py"
    plugin_line = 'pytest_plugins = ["tests.qtea_runtime"]\n'
    if not conftest.exists():
        conftest.write_text(
            "# qtea generated: registers the JIT locator runtime plugin\n"
            + plugin_line,
            encoding="utf-8",
        )
        log.info("step08.jit_conftest_created", path=str(conftest))
        return
    try:
        existing = conftest.read_text(encoding="utf-8")
    except OSError:
        return
    if "tests.qtea_runtime" in existing:
        return
    if "pytest_plugins" in existing:
        # A pytest_plugins list already exists; append the runtime entry
        # by replacing the first list-bracket close, best-effort. If the
        # format is too exotic, fall through to an extra assignment line
        # (pytest tolerates multiple assignments — last one wins, so put
        # ours last with the merged content if we can detect a simple list).
        import re as _re
        m = _re.search(
            r"pytest_plugins\s*=\s*\[(?P<items>[^\]]*)\]",
            existing,
        )
        if m:
            items = m.group("items").strip()
            new_items = (
                items + ', "tests.qtea_runtime"' if items
                else '"tests.qtea_runtime"'
            )
            replaced = (
                existing[:m.start()] + f'pytest_plugins = [{new_items}]'
                + existing[m.end():]
            )
            conftest.write_text(replaced, encoding="utf-8")
            log.info("step08.jit_conftest_extended", path=str(conftest))
            return
    # No pytest_plugins detected, or detection didn't match — append a line.
    with conftest.open("a", encoding="utf-8") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# qtea: register the JIT locator runtime plugin\n")
        f.write(plugin_line)
    log.info("step08.jit_conftest_appended", path=str(conftest))


# ---------------------------------------------------------------------------
# Framework-aware JIT runtime vendoring dispatch
# ---------------------------------------------------------------------------
#
# Frameworks that have a vendorable runtime plugin map to a function that
# (a) copies template files into the SUT, (b) registers the plugin with the
# framework's setup hook (conftest / playwright.config / setupFiles /
# @Listeners), and (c) returns the list of files it created so they can be
# added to the commit manifest.
#
# Frameworks NOT in the dispatch fall through to Step 8's on-failure heal
# flow for non-JIT stacks (Selenium / Cypress / Robot / etc.).


def _vendor_python_pytest_runtime(sut_root: Path) -> list[Path]:
    """Vendor the Python pytest+Playwright JIT runtime."""
    added = _vendor_jit_runtime(sut_root)
    if added is None:
        return []
    _ensure_conftest_registers_runtime(sut_root)
    return [added]


def _locate_runtime_template(filename: str) -> Path | None:
    """Find a runtime template file under _resources/runtime/, with a
    src/-prefixed fallback for dev trees where QTEA_RESOURCE_ROOT
    points at the repo root rather than the installed wheel."""
    primary = (
        package_resource_root() / "_resources" / "runtime" / filename
    )
    if primary.is_file():
        return primary
    fallback = (
        package_resource_root() / "src" / "qtea" / "_resources"
        / "runtime" / filename
    )
    return fallback if fallback.is_file() else None


def _detect_js_test_runner(sut_root: Path) -> str | None:
    """Inspect package.json to detect the SUT's test runner.

    Returns one of ``"playwright-test"``, ``"jest"``, ``"vitest"``, or
    ``None`` when nothing matches. When multiple are present, prefer
    Playwright Test (it's the most likely setup for a Playwright JIT path).
    """
    pkg = sut_root / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    deps = {
        **(data.get("dependencies") or {}),
        **(data.get("devDependencies") or {}),
    }
    if "@playwright/test" in deps:
        return "playwright-test"
    if "jest" in deps:
        return "jest"
    if "vitest" in deps:
        return "vitest"
    return None


def _register_playwright_test_global_setup(sut_root: Path, runtime_rel: str) -> Path | None:
    """Set ``globalSetup`` in playwright.config.{ts,js} to the vendored runtime.

    Best-effort string surgery — if the config file is too exotic to parse,
    logs the unfinished registration and returns None. The user can add
    ``globalSetup: "./tests/qtea-runtime"`` manually in that case.
    """
    for ext in ("ts", "mts", "cts", "js", "mjs", "cjs"):
        cfg = sut_root / f"playwright.config.{ext}"
        if cfg.is_file():
            break
    else:
        log.warning(
            "step08.jit_pw_config_missing",
            hint="playwright.config.* not found; runtime vendored but not "
                 "registered. Add `globalSetup: \"./tests/qtea-runtime\"` "
                 "manually.",
        )
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    if "qtea-runtime" in text:
        return cfg  # idempotent — already registered
    # Find the defineConfig({ ... }) opening brace and inject the key after it.
    import re as _re
    m = _re.search(r"defineConfig\s*\(\s*\{", text)
    if m is None:
        log.warning(
            "step08.jit_pw_config_unparseable",
            path=str(cfg),
            hint="couldn't find `defineConfig({` shape; add globalSetup manually.",
        )
        return None
    insertion = f'\n  globalSetup: "./{runtime_rel.replace(".js", "")}",'
    new_text = text[:m.end()] + insertion + text[m.end():]
    cfg.write_text(new_text, encoding="utf-8")
    log.info("step08.jit_pw_globalsetup_added", path=str(cfg))
    return cfg


def _register_setup_files(
    sut_root: Path, runner: str, runtime_rel: str,
) -> Path | None:
    """Register the runtime in Jest / Vitest config via the appropriate
    ``setupFiles`` key. Same best-effort approach as the Playwright-Test
    registration above.
    """
    if runner == "jest":
        candidates = ["jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs"]
        config_key = "setupFiles"
    elif runner == "vitest":
        candidates = ["vitest.config.ts", "vitest.config.js", "vitest.config.mts"]
        config_key = "test.setupFiles"
    else:
        return None

    for name in candidates:
        cfg = sut_root / name
        if cfg.is_file():
            break
    else:
        log.warning(
            "step08.jit_setup_config_missing",
            runner=runner,
            hint=f"{runner} config not found; runtime vendored but not registered.",
        )
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    if "qtea-runtime" in text:
        return cfg
    log.warning(
        "step08.jit_setup_files_manual",
        runner=runner, path=str(cfg),
        hint=(
            f"Auto-edit of {runner} config not supported in v1 — "
            f"add `{config_key}: ['<rootDir>/{runtime_rel}']` to {cfg.name} "
            f"manually. The vendored file at {runtime_rel} works once loaded."
        ),
    )
    return cfg


def _vendor_typescript_playwright_runtime(sut_root: Path) -> list[Path]:
    """Vendor the TS/JS Playwright JIT runtime (Playwright Test / Jest / Vitest).

    Strategy:

    1. Copy ``qtea-runtime.js.tpl`` to ``<sut>/tests/qtea-runtime.js``.
       Single CommonJS file — works in CJS and ESM-interop modes.
    2. Detect the test runner from ``package.json``.
    3. Register the runtime with the runner's setup hook:
       - Playwright Test → ``globalSetup`` in ``playwright.config.*``
       - Jest / Vitest → log a manual-registration hint (auto-edit of
         their configs is brittle; left for follow-up).

    Returns the list of files created (for commit manifest).
    """
    template = _locate_runtime_template("qtea-runtime.js.tpl")
    if template is None:
        log.warning(
            "step08.jit_runtime_template_missing",
            framework="typescript-playwright",
            hint="qtea-runtime.js.tpl not found in _resources/runtime/.",
        )
        return []
    tests_dir = sut_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    dest = tests_dir / "qtea-runtime.js"
    try:
        dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        log.warning("step08.jit_runtime_vendor_failed", error=str(e))
        return []
    log.info("step08.jit_runtime_vendored", path=str(dest))

    runtime_rel = "tests/qtea-runtime.js"
    runner = _detect_js_test_runner(sut_root)
    if runner == "playwright-test":
        _register_playwright_test_global_setup(sut_root, runtime_rel)
    elif runner in ("jest", "vitest"):
        _register_setup_files(sut_root, runner, runtime_rel)
    else:
        log.warning(
            "step08.jit_no_runner_detected",
            hint="package.json missing or no recognised runner; runtime "
                 "vendored but not auto-registered. Tests must require it "
                 "themselves: `require('./tests/qtea-runtime');`",
        )
    return [dest]


def _vendor_java_playwright_runtime(sut_root: Path) -> list[Path]:
    """Vendor the Java Playwright JIT runtime (JUnit5 + TestNG).

    Strategy:

    1. Copy ``Tbd.java``, ``QteaT.java``, ``QteaTResolver.java`` into
       the SUT's ``src/test/java/com/qtea/runtime/`` directory. Standard
       Maven + Gradle source layout applies to both JUnit5 and TestNG.
    2. The agent prompt instructs codegen to import ``com.qtea.runtime.Tbd``
       for sentinel constants and call ``QteaT.wrap(page)`` once at the
       Page acquisition site (typically in ``@BeforeEach`` / ``@BeforeMethod``).
       Java does not permit runtime method replacement the way Python /
       JavaScript do, so explicit wrapping is the cleanest path.

    Returns the list of files created (for commit manifest).
    """
    java_templates = ("Tbd.java.tpl", "QteaT.java.tpl", "QteaTResolver.java.tpl")
    located: list[tuple[Path, str]] = []
    for name in java_templates:
        path = _locate_runtime_template(name)
        if path is None:
            log.warning(
                "step08.jit_runtime_template_missing",
                framework="java-playwright",
                template=name,
            )
            return []
        located.append((path, name.removesuffix(".tpl")))

    dest_dir = sut_root / "src" / "test" / "java" / "com" / "qtea" / "runtime"
    dest_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for src, target_name in located:
        dest = dest_dir / target_name
        try:
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(dest)
            log.info("step08.jit_runtime_vendored", path=str(dest))
        except OSError as e:
            log.warning(
                "step08.jit_runtime_vendor_failed",
                path=str(dest), error=str(e),
            )
    if created:
        # JUnit5 + TestNG both pick these up automatically once they're on
        # the test classpath — no @Listeners / META-INF/services entries
        # required for the explicit-wrap pattern. Tests must call
        # QteaT.wrap(page) once at Page acquisition; the agent's TBD-rule §3c
        # documents the pattern with examples.
        log.info(
            "step08.jit_java_runtime_ready",
            count=len(created),
            hint=(
                "Java JIT activated via explicit QteaT.wrap(page) in tests. "
                "Build tool (Maven/Gradle) picks up src/test/java/com/qtea/"
                "runtime/ automatically; no config edit needed."
            ),
        )
    return created


# Framework name → vendor function. Framework strings match the values
# produced by `qtea.test_indexer.resolve_framework()`.
_RUNTIME_VENDORS = {
    "pytest": _vendor_python_pytest_runtime,
    "playwright-py": _vendor_python_pytest_runtime,
    "playwright-ts": _vendor_typescript_playwright_runtime,
    "playwright-js": _vendor_typescript_playwright_runtime,
    "jest": _vendor_typescript_playwright_runtime,
    "vitest": _vendor_typescript_playwright_runtime,
    "junit5-playwright": _vendor_java_playwright_runtime,
    "testng-playwright": _vendor_java_playwright_runtime,
    "playwright-java": _vendor_java_playwright_runtime,
}


def _ensure_gitignore_entry(directory: Path, entry: str) -> None:
    """Append *entry* to ``<directory>/.gitignore`` if not already listed."""
    gitignore = directory / ".gitignore"
    try:
        if gitignore.exists():
            text = gitignore.read_text(encoding="utf-8", errors="replace")
            if any(line.strip() == entry for line in text.splitlines()):
                return
            if text and not text.endswith("\n"):
                text += "\n"
        else:
            text = ""
        gitignore.write_text(text + entry + "\n", encoding="utf-8")
    except OSError:
        pass


# SUT-relative paths of vendored runtime files that must be gitignored.
# Checked after each vendor call; only entries whose target exists on
# disk are added (so a Python-only run won't gitignore the JS path).
_RUNTIME_GITIGNORE_ENTRIES: tuple[str, ...] = (
    "tests/qtea_runtime.py",
    "tests/qtea-runtime.js",
    "src/test/java/com/qtea/runtime/",
)


def _vendor_runtime_for_framework(framework: str | None, sut_root: Path) -> list[Path]:
    """Dispatch JIT runtime vendoring by framework. Returns the list of
    files written into the SUT (for inclusion in the commit manifest).
    Frameworks without a dispatch entry return ``[]`` — Step 8 picks them
    up via the on-failure heal path."""
    vendor_fn = _RUNTIME_VENDORS.get(framework or "")
    if vendor_fn is None:
        log.info(
            "step08.jit_runtime_not_vendored",
            framework=framework,
            hint="Step 8 will use the on-failure heal flow for this stack.",
        )
        return []
    created = vendor_fn(sut_root)
    if created:
        for entry in _RUNTIME_GITIGNORE_ENTRIES:
            target = sut_root / entry.rstrip("/")
            if target.exists():
                _ensure_gitignore_entry(sut_root, entry)
    return created


def _read_research(ctx: StepContext) -> dict:
    research_json = ctx.workspace.step_dir(6) / "research.json"
    if not research_json.exists():
        return {}
    try:
        return json.loads(research_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Framework ↔ test-command consistency check (pre-flight, runs before vendor)
# ---------------------------------------------------------------------------
#
# `_RUNTIME_VENDORS` dispatches off `research.json.detected_stack`. When
# Step 6 misdetects the framework (e.g. labels a Robot+SeleniumLibrary repo
# as `pytest` because of glob accidents), Step 8 vendors the wrong runtime
# template, conftest registers a plugin that no collector consumes, and
# Step 9 fails at collection with an opaque trace. Cross-check the detected
# framework against `commands.test`'s argv head and fail fast on mismatch.
#
# The check is intentionally permissive: when either side is unknown / the
# command is provided as a single freeform string we cannot tokenise, we
# silently skip rather than risk false-positive blocking. The goal is to
# catch obvious misclassifications, not to police every command shape.

# Map argv-head tokens (lower-cased, stripped of shell prefixes like `npx`,
# `uv run`, `poetry run`) to the set of `detected_stack` values that argv
# is consistent with. Keys are exact match against the first non-prefix
# token; for multi-word commands like `playwright test`, the first two
# tokens are joined with a space.
_TEST_COMMAND_TO_STACKS: dict[str, frozenset[str]] = {
    "pytest": frozenset({"pytest", "playwright-py", "selenium-py"}),
    "py.test": frozenset({"pytest", "playwright-py", "selenium-py"}),
    "playwright test": frozenset({"playwright-ts", "playwright-js"}),
    "cypress run": frozenset({"cypress"}),
    "cypress open": frozenset({"cypress"}),
    "robot": frozenset({"robot"}),
    "jest": frozenset({"jest"}),
    "vitest": frozenset({"vitest"}),
    "mocha": frozenset({"mocha"}),
    "wdio": frozenset({"wdio"}),
    "mvn": frozenset({"selenium-java", "junit5-playwright", "testng-playwright"}),
    "mvnw": frozenset({"selenium-java", "junit5-playwright", "testng-playwright"}),
    "gradle": frozenset({"selenium-java", "junit5-playwright", "testng-playwright"}),
    "gradlew": frozenset({"selenium-java", "junit5-playwright", "testng-playwright"}),
}

# Shell wrapper prefixes that don't carry framework signal — strip and read
# the next token. `uv run`, `poetry run`, `pdm run`, `pipenv run`, `npx`,
# `npm run` / `pnpm run` / `yarn run`, `./mvnw`, `./gradlew`.
_TEST_COMMAND_WRAPPER_PREFIXES: tuple[str, ...] = (
    "uv", "poetry", "pdm", "pipenv", "npx", "npm", "pnpm", "yarn",
)


def _parse_test_command_head(command: str | None) -> str | None:
    """Extract the canonical framework token from a test-run command string.

    Returns one of the keys in :data:`_TEST_COMMAND_TO_STACKS`, or ``None``
    when the command is missing / too freeform to classify safely.
    """
    if not command or not isinstance(command, str):
        return None
    # Tokenise on whitespace; strip shell wrapper prefixes and their subcommands.
    tokens = command.strip().split()
    while tokens and tokens[0].lower() in _TEST_COMMAND_WRAPPER_PREFIXES:
        # Drop the wrapper plus its sub-token (e.g. `run`, `exec`) when
        # present.
        tokens = tokens[1:]
        if tokens and tokens[0].lower() in ("run", "exec", "x"):
            tokens = tokens[1:]
    if not tokens:
        return None
    # Normalise `./gradlew` / `.\\mvnw.cmd` / absolute paths to basename.
    head = tokens[0].lower().lstrip("./\\").replace(".cmd", "").replace(".bat", "")
    head = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Multi-word match (e.g. "playwright test", "cypress run") — try the
    # two-word join first; fall back to single word.
    if len(tokens) >= 2:
        two = f"{head} {tokens[1].lower()}"
        if two in _TEST_COMMAND_TO_STACKS:
            return two
    return head if head in _TEST_COMMAND_TO_STACKS else None


def _framework_mismatch_message(
    detected_stack: str | None, command_head: str | None,
) -> str | None:
    """Return a one-line mismatch description, or None when consistent /
    unverifiable."""
    if not detected_stack or not command_head:
        return None
    allowed = _TEST_COMMAND_TO_STACKS.get(command_head)
    if not allowed:
        return None
    if detected_stack in allowed:
        return None
    return (
        f"research.json.detected_stack={detected_stack!r} is inconsistent "
        f"with research.json.commands.test (argv head: {command_head!r}, "
        f"expected stack in {sorted(allowed)}). Step 6 likely misdetected "
        f"the framework — re-run Step 6 with the correct hint, or fix "
        f"research.json by hand and retry from Step 8."
    )


def _active_module_dict(sut_inventory_dict: dict) -> dict | None:
    """Pull the active module entry out of a raw `sut_inventory` dict.

    Returns None when the inventory has no `active_module` set or the name
    doesn't match any entry. Tolerant of missing keys so an older
    research.json (no `sut_inventory` block) won't crash the step.
    """
    active = sut_inventory_dict.get("active_module")
    if not active:
        return None
    for mod in sut_inventory_dict.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == active:
            return mod
    return None


def _inventory_files(active_module: dict | None) -> list[str]:
    """Return SUT-relative paths the active module says exist.

    Pulled from `auth_flow`, `existing_page_objects`, `existing_fixtures`,
    `existing_helpers`, `existing_locators` — the same set the previous
    `_sut_staging.collect_sut_files` helper built. Used by the pre-flight
    to verify the clone actually contains what step 6 said it would.
    """
    if not active_module:
        return []
    paths: list[str] = []

    auth = active_module.get("auth_flow") or {}
    for key in ("entry_method", "fixture_entry"):
        v = auth.get(key)
        if isinstance(v, str) and v:
            # `<file>:<class>.<method>` or `<file>:<func>` → `<file>`
            paths.append(v.split(":", 1)[0])

    for bucket in ("existing_page_objects", "existing_fixtures",
                   "existing_helpers", "existing_locators"):
        for entry in active_module.get(bucket) or []:
            p = entry.get("file") if isinstance(entry, dict) else None
            if p:
                paths.append(p)

    # Dedup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p and p not in seen and Path(p).name != "__init__.py":
            seen.add(p)
            out.append(p)
    return out


_FENCE_OPEN_RE = _re.compile(r"^```[A-Za-z0-9_+\-]*\s*$", _re.MULTILINE)
_FENCE_CLOSE_RE = _re.compile(r"^```\s*$", _re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Extract source code from an LLM response that may wrap it in fences.

    ``call_reasoning_llm`` without ``output_schema`` returns freeform text.
    Models frequently wrap code output in ```python / ```py / ``` fences,
    AND sometimes write a prose preamble before the fence ("Looking at
    the plan, I need to…") and/or postamble after it ("Hope that helps!").
    Writing any of that into a .py / .ts / .java file causes SyntaxError
    at import time — observed twice:

    1. Run 20260611-075728 step 8 — two test files began with the LLM's
       reasoning paragraph; Phase B.5 reported `parse_error` at line 1.
    2. Earlier: chat_page.py written starting with literal ``\\`\\`\\`py``.

    Algorithm:
      - If the text contains a `\\`\\`\\`<lang>` opening fence on its own
        line, return the contents up to the matching closing `\\`\\`\\``
        (or end of text if the closing fence is missing).
      - Otherwise return the stripped text unchanged — models that obey
        the "code only, no fences" instruction send raw code.
    """
    s = text.strip()
    if not s:
        return s
    open_m = _FENCE_OPEN_RE.search(s)
    if open_m is None:
        return s
    body_start = open_m.end()
    # Skip the newline immediately after the opening fence line.
    if body_start < len(s) and s[body_start] == "\n":
        body_start += 1
    rest = s[body_start:]
    close_m = _FENCE_CLOSE_RE.search(rest)
    if close_m is None:
        # Unclosed fence — everything after the opener is the body.
        return rest.rstrip()
    return rest[: close_m.start()].rstrip()


def _is_qtea_file(rel: str) -> bool:
    """True for paths whose basename matches the agent's `qtea_`/`Qtea` convention."""
    name = Path(rel).name.lower()
    return name.startswith("qtea")


_B5_NON_TEST_SUFFIXES: tuple[str, ...] = (
    "_page", "_locators", "_fixture", "_data", "_helper", "_runtime",
)


def _b5_filter_test_files(produced: list[Path], language: str) -> list[Path]:
    """Filter agent_produced down to test files only.

    POMs, fixtures, locators, helpers, data, and runtime files are excluded
    — B.5 only verifies tests' calls against POMs. Conventions:

    * Python / TS / JS: `qtea_<feature>_test.<ext>` — snake_case, ends in
      ``_test``. Files named ``qtea_<feature>_<role>.<ext>`` (role ∈ the
      non-test suffix table above) are explicitly skipped.
    * TS / JS also: Playwright's ``.spec.ts`` / ``.spec.js`` convention
      (``qtea_<feature>_test.spec.ts`` or ``qtea_<feature>.spec.ts``).
      The compound extension leaves the stem ending in ``.spec``, so this
      branch adds an explicit check. Missing this check was why
      run 20260701-114656-9394eb's ``qtea_ropa_approval_test.spec.ts``
      was silently skipped by B.5 (0 files scanned).
    * Java: ``Qtea<Feature>Test.java`` — CamelCase. Lowercased, the stem
      ends in ``test`` with no underscore separator. Only ``.java`` files
      get this looser match; without the extension gate, a Python POM
      named ``qtea_dashboardtest`` (unusual but legal) would false-match.
    """
    out: list[Path] = []
    _JS_TS_EXTS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
    for p in produced:
        stem = p.stem.lower()
        name = p.name.lower()
        ext = p.suffix.lower()
        if any(stem.endswith(suf) for suf in _B5_NON_TEST_SUFFIXES):
            continue
        is_test = (
            stem.endswith("_test")
            or stem.startswith("test_")
            or "_test_" in stem
            or (ext == ".java" and stem.endswith("test"))
            or (ext in _JS_TS_EXTS and (stem.endswith(".spec") or ".spec." in name))
        )
        if is_test:
            out.append(p)
    return out


def _filter_index_to_qtea(
    index: IndexResult,
    sut_root: Path,
    *,
    exclude: set[Path] | None = None,
    include: set[Path] | None = None,
) -> IndexResult:
    """Return a new IndexResult containing only qtea-relevant entries.

    `index_tests` walks the whole SUT and picks up the SUT's own pre-existing
    tests / locators alongside qtea-generated ones. The user-facing tbd-index
    must reflect only what qtea produced, otherwise Step 7's "tests=N" gate
    and Step 8's downstream logic would race against unrelated SUT code.

    ``exclude`` lets the caller drop additional qtea-prefixed paths from the
    index — used to skip pre-vendored JIT runtime files (`qtea_runtime.py`,
    `qtea-runtime.js`, `QteaT.java`) so they don't inflate the test/support
    counts. Paths in ``exclude`` must be resolved absolute paths.

    ``include`` keeps non-qtea files that codegen modified (e.g. existing POM
    locator files extended with new TBD constants in Phase A2). Without this,
    TBD markers in modified existing files are invisible to the quality gate.
    Paths in ``include`` must be resolved absolute paths.
    """
    exclude_set: set[Path] = exclude or set()
    include_set: set[Path] = include or set()

    def _keep(rel_path: str) -> bool:
        try:
            abs_resolved = (sut_root / rel_path).resolve()
        except OSError:
            abs_resolved = None
        if abs_resolved and abs_resolved in exclude_set:
            return False
        if _is_qtea_file(rel_path):
            return True
        return bool(abs_resolved and abs_resolved in include_set)

    files = [f for f in index.files if _keep(f)]
    tests = [t for t in index.tests if _keep(t.file)]
    support_files = [s for s in index.support_files if _keep(s.file)]
    violations = [v for v in index.violations if _keep(v.file)]
    return replace(
        index,
        test_root=str(sut_root),
        files=files,
        tests=tests,
        support_files=support_files,
        violations=violations,
    )


# ---------------------------------------------------------------------------
# Phase A/B: plan decomposition + reasoning-call orchestration
# ---------------------------------------------------------------------------

_MAX_CONCURRENT_LLM_CALLS = int(os.environ.get("QTEA_CODEGEN_CONCURRENCY", "3"))


@dataclass
class _PomTask:
    pom_name: str
    pom_file: str  # SUT-relative path
    source: str  # "reuse" or "create"
    from_path: str | None = None
    at_path: str | None = None
    missing_methods: list[dict[str, Any]] = field(default_factory=list)
    locator_file: str | None = None
    locator_class: str | None = None


@dataclass
class _LocatorTask:
    constant_name: str
    intent: str
    owning_page: str
    locator_file: str | None = None
    # Where in the SUT to place the new TBD constant. Set from the
    # matching ``LocatorClass.location_pattern`` when a matching entry is
    # found in the inventory. None means "no locator source found for
    # this POM" — the caller (``_write_tbd_locators``) falls back to
    # emitting inline ``tbd("intent")`` in the POM method body via the
    # POM extender agent.
    location_pattern: str | None = None
    container_name: str | None = None  # e.g. "elements" for inline_object_property
    # Outer identifier the mechanical writer searches for when inserting
    # into an object literal. For ``export_const_object`` this is the
    # const's name (e.g. ``TrialPageSelectors``). For
    # ``inline_object_property`` this is the owning POM class name
    # (e.g. ``RopaEntryPage``) — the writer then descends into the
    # ``container_name`` property of that class body. None for linear-
    # append patterns (``separate_class`` / ``module_const_bag``).
    container_class_name: str | None = None


@dataclass
class _FixtureTask:
    name: str
    at: str
    yields: str | None = None
    scope: str = "function"
    depends_on: list[str] = field(default_factory=list)


@dataclass
class _HelperTask:
    name: str
    at: str
    signature: str | None = None


def _build_pom_tasks(
    plan: dict[str, Any],
    sut_root: Path,
    inventory: dict[str, Any] | None,
) -> dict[str, _PomTask]:
    """Group and deduplicate page_objects with missing_methods across all TCs."""
    tasks: dict[str, _PomTask] = {}  # keyed by POM file path
    inv_entries: list[dict[str, Any]] = []
    if inventory:
        am = _active_module_dict(inventory) or {}
        for lc in am.get("existing_locators") or []:
            if isinstance(lc, dict):
                inv_entries.append(lc)

    for tc in plan.get("test_cases") or []:
        for po in tc.get("page_objects") or []:
            src = po.get("source", "reuse")
            file_path = po.get("from") or po.get("at") or ""
            if not file_path:
                continue
            pom_name = po.get("name", "")

            if file_path not in tasks:
                loc_info = _resolve_locator_inventory_entry(
                    pom_name, inv_entries,
                ) or {}
                tasks[file_path] = _PomTask(
                    pom_name=pom_name,
                    pom_file=file_path,
                    source=src,
                    from_path=po.get("from"),
                    at_path=po.get("at"),
                    locator_file=loc_info.get("file"),
                    locator_class=loc_info.get("class_name"),
                )

            task = tasks[file_path]
            existing_names = {m["name"] for m in task.missing_methods}
            for method in po.get("missing_methods") or []:
                name = method.get("name", "")
                if name and name not in existing_names:
                    task.missing_methods.append(method)
                    existing_names.add(name)
                elif name in existing_names:
                    log.debug(
                        "step08.pom_method_dedup",
                        method=name, pom=file_path,
                    )
    return tasks


def _resolve_locator_inventory_entry(
    owning: str, inv_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Look up the locator-source inventory entry for an owning POM.

    Priority: an ``existing_locators[]`` entry whose ``owning_pom``
    matches (inline/readonly-property conventions), else the first
    naming-convention fallback hit. Historical bug (fixed here for
    good — run 20260708-121117-99f5ed): this resolution used to be
    duplicated with only `_build_locator_tasks` carrying the
    `{Owning}Locators`/`{Owning}Selectors`/... suffix fallback list;
    `_build_pom_tasks` had its own copy that only tried
    `{Owning}Locators`, so a POM named `TrialPage` with a companion
    const `TrialPageSelectors` resolved locators for the sentinel
    writer but NOT for the pom-extender's prompt — the extender never
    saw `locators.py` as a separate input and the LOCATOR CONTRACT text
    named a generic "the locator source" instead of the real file.
    Both call sites now share this single resolver.
    """
    if not owning:
        return None
    inline_hit = next(
        (lc for lc in inv_entries if lc.get("owning_pom") == owning),
        None,
    )
    if inline_hit:
        return inline_hit
    for suffix in ("Locators", "Selectors", "Elements",
                   "Locator", "Selector", "Element"):
        name = f"{owning}{suffix}"
        hit = next(
            (lc for lc in inv_entries if lc.get("class_name") == name),
            None,
        )
        if hit:
            return hit
    return None


def _build_locator_tasks(
    plan: dict[str, Any],
    inventory: dict[str, Any] | None,
) -> list[_LocatorTask]:
    """Collect create_tbd locators across all TCs.

    Locator-source resolution respects whatever convention the SUT
    already uses (recorded on each ``existing_locators[]`` entry as
    ``location_pattern``). Priority order per owning page:

    1. ``owning_pom == owning_page`` — inline patterns
       (``inline_object_property`` / ``readonly_locator_props``) win
       when the POM itself owns the locators.
    2. ``class_name == f"{owning_page}Locators"`` — separate-class
       fallback for the historical Python-Selenium convention.
    3. Nothing — task gets ``locator_file=None`` and ``_write_tbd_locators``
       hands it to the POM extender agent for inline ``tbd()`` placement
       (never silently dropped).
    """
    inv_entries: list[dict[str, Any]] = []
    if inventory:
        am = _active_module_dict(inventory) or {}
        for lc in am.get("existing_locators") or []:
            if isinstance(lc, dict):
                inv_entries.append(lc)

    tasks: list[_LocatorTask] = []
    seen: set[str] = set()
    for tc in plan.get("test_cases") or []:
        for loc in tc.get("locators") or []:
            if loc.get("source") != "create_tbd":
                continue
            name = loc.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            owning = loc.get("owning_page", "")
            entry = _resolve_locator_inventory_entry(owning, inv_entries)
            tasks.append(_LocatorTask(
                constant_name=name,
                intent=loc.get("intent", ""),
                owning_page=owning,
                locator_file=entry.get("file") if entry else None,
                location_pattern=(
                    entry.get("location_pattern") if entry else None
                ),
                container_name=(
                    entry.get("container_name") if entry else None
                ),
                container_class_name=(
                    entry.get("class_name") if entry else None
                ),
            ))
    return tasks


def _build_fixture_tasks(plan: dict[str, Any]) -> list[_FixtureTask]:
    """Collect source=create fixtures across all TCs."""
    tasks: list[_FixtureTask] = []
    seen: set[str] = set()
    for tc in plan.get("test_cases") or []:
        for fix in tc.get("fixtures") or []:
            if fix.get("source") != "create":
                continue
            name = fix.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            tasks.append(_FixtureTask(
                name=name,
                at=fix.get("at", ""),
                yields=fix.get("yields"),
                scope=fix.get("scope", "function"),
                depends_on=fix.get("depends_on") or [],
            ))
    return tasks


def _build_helper_tasks(plan: dict[str, Any]) -> list[_HelperTask]:
    """Collect source=create helpers across all TCs."""
    tasks: list[_HelperTask] = []
    seen: set[str] = set()
    for tc in plan.get("test_cases") or []:
        for h in tc.get("helpers") or []:
            if h.get("source") != "create":
                continue
            name = h.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            tasks.append(_HelperTask(
                name=name,
                at=h.get("at", ""),
                signature=h.get("signature"),
            ))
    return tasks


def _group_helper_tasks_by_file(
    helper_tasks: list[_HelperTask],
) -> dict[str, list[_HelperTask]]:
    by_file: dict[str, list[_HelperTask]] = {}
    for task in helper_tasks:
        if not task.at:
            continue
        by_file.setdefault(task.at, []).append(task)
    return by_file


async def _create_helpers(
    helper_tasks: list[_HelperTask],
    sut_root: Path,
    workdir: Path,
    agents_root: Path,
    active_module: dict[str, Any] | None,
    step: int,
    rules_content: str = "",
) -> list[tuple[str, bool]]:
    """Phase A5: create new helper functions via call_reasoning_llm.

    One LLM call per target file (all helpers sharing a target file
    are created in a single pass).
    """
    if not helper_tasks:
        return []

    agent_path = agents_root / "codegen-pom-extender.agent.md"
    sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)

    existing_helpers = (active_module or {}).get("existing_helpers") or []
    style_ref = ""
    if existing_helpers and existing_helpers[0].get("file"):
        ref_path = sut_root / existing_helpers[0]["file"]
        if ref_path.is_file():
            try:
                raw_ref = ref_path.read_text(encoding="utf-8")
                head = raw_ref[:3000]
                last_nl = head.rfind("\n")
                style_ref = head[:last_nl] if last_nl > 0 else head
            except OSError:
                pass

    by_file = _group_helper_tasks_by_file(helper_tasks)

    async def _create_file(
        file_path: str, tasks: list[_HelperTask],
    ) -> tuple[str, bool]:
        specs = [
            {"name": t.name, "signature": t.signature}
            for t in tasks
        ]
        existing = ""
        target = sut_root / file_path
        if target.is_file():
            with contextlib.suppress(OSError):
                existing = target.read_text(encoding="utf-8")

        inputs: dict[str, str] = {
            "helper_specs.json": json.dumps(specs, indent=2),
        }
        if existing:
            inputs["existing_file.py"] = existing
        if style_ref:
            inputs["style_reference.py"] = style_ref
        if rules_content:
            inputs["codegen-rules.md"] = rules_content

        names = ", ".join(t.name for t in tasks)
        async with sem:
            log.info(
                "step08.helper_create.start",
                file=file_path,
                helpers=len(tasks),
                names=names,
            )
            result = await call_reasoning_llm(
                agent_path,
                workdir=workdir,
                user_prompt=(
                    f"Create {len(tasks)} helper function(s) — {names} — "
                    f"matching the specs in `helper_specs.json`. "
                    f"If `existing_file.py` is provided, append the new "
                    f"helpers to it and return the complete updated file. "
                    f"Otherwise return a complete new file. "
                    f"`style_reference.py` shows coding conventions only. "
                    f"The output must be syntactically valid Python."
                ),
                inputs=inputs,
                step=step,
                timeout_s=120,
                max_tokens=4000 + 800 * len(tasks),
            )

        if result.success and result.final_text.strip():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                clean = _strip_code_fences(result.final_text)
                target.write_text(clean, encoding="utf-8")
                # Language-agnostic truncation gate (see _extend_one). A
                # max_tokens stop means the helper file was cut off mid-output
                # — roll back to the original (or remove a brand-new file) so
                # the truncated version is never accepted. Protects non-Python
                # stacks that the ast.parse check below cannot cover.
                if getattr(result, "stop_reason", None) == "max_tokens":
                    if existing:
                        with contextlib.suppress(OSError):
                            target.write_text(existing, encoding="utf-8")
                    else:
                        target.unlink(missing_ok=True)
                    log.error(
                        "step08.helper_truncated",
                        file=file_path,
                        chars_written=len(clean),
                        hint="output truncated by max_tokens",
                    )
                    return file_path, False
                if target.suffix == ".py":
                    import ast as _ast
                    import warnings as _warnings
                    try:
                        with _warnings.catch_warnings():
                            _warnings.simplefilter("ignore", SyntaxWarning)
                            _ast.parse(clean)
                    except SyntaxError as e:
                        try:
                            if existing:
                                target.write_text(existing, encoding="utf-8")
                            else:
                                target.unlink(missing_ok=True)
                        except OSError:
                            pass
                        log.error(
                            "step08.helper_syntax_invalid",
                            file=file_path, error=str(e),
                        )
                        return file_path, False
                missing = [
                    t.name for t in tasks
                    if _re.search(
                        rf"^\s*def\s+{_re.escape(t.name)}\s*\(",
                        clean, _re.M,
                    ) is None
                ]
                if missing:
                    log.error(
                        "step08.helper_create.symbols_missing",
                        file=file_path, missing=missing,
                    )
                    return file_path, False
                log.info(
                    "step08.helper_create.done",
                    file=file_path, helpers=len(tasks),
                )
                return file_path, True
            except OSError as e:
                log.error(
                    "step08.helper_write_failed",
                    file=file_path, error=str(e),
                )
                return file_path, False
        else:
            log.warning(
                "step08.helper_create.failed",
                file=file_path, error=result.error,
            )
            return file_path, False

    results = list(await asyncio.gather(
        *[_create_file(fp, tasks) for fp, tasks in by_file.items()]
    ))
    return results


_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY = "s08_pom_extender_max_tokens_override"
_POM_EXTENDER_MAX_TOKENS_HARD_CAP = 32000

# Timeout scaling for the POM extender (fix-proposal Action 3 / TD-3). The
# smart-retry doubles max_tokens; a proportionally larger generation needs
# proportionally more wall-clock, or the retry is structurally doomed to time
# out (run 20260708-121117-99f5ed attempt 2: 21338 tokens vs a fixed 120s ->
# APITimeoutError at 367s). Scale the client timeout with the token budget,
# clamped to a ceiling that leaves margin under MAX_STEP_TIMEOUT_S (1800s).
_POM_EXTENDER_TIMEOUT_BASE_S = 120
_POM_EXTENDER_TIMEOUT_CEILING_S = 600


def _pom_extender_timeout_s(max_tokens: int) -> int:
    """Client timeout scaled with the token budget, floored/capped."""
    return max(
        _POM_EXTENDER_TIMEOUT_BASE_S,
        min(max_tokens // 25, _POM_EXTENDER_TIMEOUT_CEILING_S),
    )


async def _extend_poms(
    pom_tasks: dict[str, _PomTask],
    sut_root: Path,
    workdir: Path,
    agents_root: Path,
    step: int,
    rules_content: str = "",
    ctx: StepContext | None = None,
    locator_tasks: list[_LocatorTask] | None = None,
) -> list[tuple[str, bool]]:
    """Phase A2: extend each POM with missing_methods via call_reasoning_llm.

    When ``ctx`` is provided, the per-call max_tokens budget can be overridden
    by setting ``ctx.extras[_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY]`` to an int
    BEFORE this call. The override is consumed (popped) so a successful
    attempt 2 doesn't leak the override into unrelated POMs or subsequent
    Step 8 phases. The override is armed by ``_extend_one`` itself on
    syntax-validation failure (truncation signal) — see the rollback block.

    ``locator_tasks`` (RCA-B) — the list of ``create_tbd`` locators the
    pipeline pre-wrote (Phase A2) into the SUT's locator sources. The
    extender's per-POM prompt then lists the pre-written constant names
    explicitly, instructing the agent to reference them by name and
    fail loud with ``[CLARIFICATION NEEDED]`` if a referenced constant
    is missing — closing the coherence trap that let the extender
    invent raw XPath selectors on run 20260708-121117-99f5ed.
    """
    agent_path = agents_root / "codegen-pom-extender.agent.md"
    sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)
    results: list[tuple[str, bool]] = []

    # Group pre-written locator constants by owning_page so we can tell
    # the extender exactly which constants exist for its POM.
    prewritten_by_page: dict[str, list[str]] = {}
    for lt in locator_tasks or []:
        prewritten_by_page.setdefault(lt.owning_page, []).append(
            lt.constant_name,
        )

    # Smart-retry override is per-_extend_poms-call. Consume once at the top:
    # all POMs in this call share the same budget multiplier (when armed),
    # and we don't want to re-apply it across nested calls.
    # TODO: migrate to classifier-driven override key in Phase 3.
    budget_override: int | None = None
    if ctx is not None:
        raw = ctx.extras.pop(_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY, None)
        if isinstance(raw, int) and raw > 0:
            budget_override = min(raw, _POM_EXTENDER_MAX_TOKENS_HARD_CAP)

    async def _extend_one(file_path: str, task: _PomTask) -> tuple[str, bool]:
        if not task.missing_methods:
            return file_path, True

        abs_path = sut_root / file_path
        if not abs_path.is_file():
            log.warning("step08.pom_not_found", path=file_path)
            return file_path, False

        existing_source = abs_path.read_text(encoding="utf-8")
        locator_source = ""
        if task.locator_file:
            loc_path = sut_root / task.locator_file
            if loc_path.is_file():
                locator_source = loc_path.read_text(encoding="utf-8")

        methods_json = json.dumps(task.missing_methods, indent=2)

        inputs = {"existing_pom.py": existing_source}
        if locator_source:
            inputs["locators.py"] = locator_source
        inputs["missing_methods.json"] = methods_json
        if rules_content:
            inputs["codegen-rules.md"] = rules_content

        # Scale max_tokens with the workload: the agent must return the FULL
        # updated file (existing source + new method bodies). Hard-coding 8000
        # truncated ChatPage on run 20260614-190647-ab7dac (22K-char file + 19
        # methods → response cut mid-`def`, file became unparseable, Phase B.5
        # reconciler then reported every test method as `method_not_found`
        # with `existing_methods=[]` because ast.parse choked on the broken
        # file). Heuristic: existing-source tokens (~chars/3) + ~600 tokens
        # per new method body + 1000 buffer. Floor 8000, cap 32000 to stay
        # within model output limits and avoid runaway.
        method_count = len(task.missing_methods)
        estimated = (len(existing_source) // 3) + method_count * 600 + 1000
        dynamic_max_tokens = max(8000, min(estimated, _POM_EXTENDER_MAX_TOKENS_HARD_CAP))
        # Smart-retry override (consumed at _extend_poms entry above) wins
        # over the heuristic — it carries the previous attempt's budget × 2.
        if budget_override is not None:
            dynamic_max_tokens = budget_override

        async with sem:
            log.info(
                "step08.pom_extend.start",
                pom=task.pom_name,
                methods=method_count,
                existing_chars=len(existing_source),
                max_tokens=dynamic_max_tokens,
            )
            # Build the state-aware LOCATOR CONTRACT for this POM (RCA-B).
            # The prompt lists every constant the pipeline pre-wrote for
            # THIS POM's locator source so the extender knows which
            # sentinel identifiers are guaranteed to exist — and can be
            # instructed to fail loud on anything else.
            prewritten = prewritten_by_page.get(task.pom_name, [])
            if prewritten:
                prewritten_lines = "\n".join(
                    f"    - {c}" for c in prewritten
                )
                locator_contract = (
                    f"LOCATOR CONTRACT — this is the ONLY correct behavior:\n"
                    f"The pipeline has PRE-WRITTEN the following TBD sentinel "
                    f"constants into `{task.locator_file or 'the locator source'}`. "
                    f"They are guaranteed to exist. Reference each one by name — "
                    f"via `self.locators.<CONSTANT>` (Python), "
                    f"`<ContainerName>.<CONSTANT>` (TS), or the equivalent for "
                    f"your stack. Do NOT redefine, reassign, or duplicate them:\n"
                    f"{prewritten_lines}\n\n"
                    f"If a method specification references a locator constant "
                    f"NOT in the pre-written list above AND NOT already present "
                    f"in the file, DO NOT INVENT a selector — hardcoding is a "
                    f"contract violation that Phase A3.5 will hard-fail. "
                    f"Instead emit `throw new Error(\"[CLARIFICATION NEEDED]: "
                    f"locator <NAME> was not pre-written\")` (TS) or "
                    f"`raise RuntimeError(\"[CLARIFICATION NEEDED]: locator "
                    f"<NAME> was not pre-written\")` (Python) and move on. "
                    f"The pipeline will surface the gap via HITL."
                )
            else:
                # No create_tbd locators for this POM — either everything
                # was reused (fine) or the plan expects the extender to
                # place tbd() inline in method bodies. Keep the historical
                # instruction wording.
                locator_contract = (
                    f"LOCATOR RULE: All locator constants referenced by these "
                    f"methods either already exist in `{task.locator_file or 'the POM file'}` "
                    f"or must be added inline as `tbd(\"intent\")` sentinels — "
                    f"NEVER as hardcoded selector strings (see §3 of "
                    f"`codegen-rules.md`). Reference existing constants via "
                    f"`self.locators.<CONSTANT>` (Python) or the equivalent for "
                    f"your stack. If a required constant is not defined anywhere, "
                    f"DO NOT INVENT one — emit `[CLARIFICATION NEEDED]` and stop."
                )

            result = await call_reasoning_llm(
                agent_path,
                workdir=workdir,
                user_prompt=(
                    f"Add {len(task.missing_methods)} missing method(s) to the "
                    f"`{task.pom_name}` class in `existing_pom.py`. The companion "
                    f"locator class is in `locators.py` (if provided). The method "
                    f"specifications are in `missing_methods.json` — each has `name`, "
                    f"`signature`, and optionally `purpose`. Return the complete "
                    f"updated file content.\n\n"
                    f"{locator_contract}"
                ),
                inputs=inputs,
                step=step,
                timeout_s=_pom_extender_timeout_s(dynamic_max_tokens),
                max_tokens=dynamic_max_tokens,
            )

        if not (result.success and result.final_text.strip()):
            log.warning(
                "step08.pom_extend.failed",
                pom=task.pom_name,
                error=result.error,
            )
            return file_path, False

        new_content = _strip_code_fences(result.final_text)
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            log.error("step08.pom_write_failed", pom=task.pom_name, error=str(e))
            return file_path, False

        def _rollback() -> None:
            # Restore the untouched original so Phase B.5 sees a parseable
            # file with a meaningful mismatch list instead of truncated
            # garbage (which would AST-parse to zero methods and produce
            # 30+ misleading "method_not_found" mismatches).
            with contextlib.suppress(OSError):
                abs_path.write_text(existing_source, encoding="utf-8")

        def _arm_smart_retry(signal: str) -> None:
            # Stash a 2× budget on ctx.extras so the step's retry
            # (MAX_ATTEMPTS=2 in base.py) picks it up at the top of the next
            # _extend_poms call. Capped at the hard limit; only armed when
            # ctx is available.
            if ctx is None:
                return
            new_budget = min(
                dynamic_max_tokens * 2, _POM_EXTENDER_MAX_TOKENS_HARD_CAP,
            )
            if new_budget > dynamic_max_tokens:
                ctx.extras[_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY] = new_budget
                log.info(
                    "step08.pom_extender.smart_retry_armed",
                    pom=task.pom_name,
                    prev_max_tokens=dynamic_max_tokens,
                    next_max_tokens=new_budget,
                    signal=signal,
                )

        # Language-agnostic truncation gate. `stop_reason == "max_tokens"` is
        # a definitive "the model was cut off mid-output" signal from the API
        # — it needs no language-specific parser, so it protects EVERY SUT
        # stack (TS/JS, Java, Robot, Python, ...). Without this, a truncated
        # non-Python POM (e.g. TrialPage.ts on run 20260708-121117-99f5ed) is
        # written to disk and accepted as success, because call_reasoning_llm
        # sets success=True for any non-empty response regardless of
        # stop_reason. The Python-only ast.parse below never runs for .ts, so
        # this gate is the only truncation defense those stacks have.
        if getattr(result, "stop_reason", None) == "max_tokens":
            _rollback()
            _arm_smart_retry("stop_reason=max_tokens")
            log.error(
                "step08.pom_truncated",
                pom=task.pom_name,
                file=file_path,
                chars_written=len(new_content),
                max_tokens=dynamic_max_tokens,
                hint=(
                    "output truncated by max_tokens; "
                    "smart-retry armed with doubled budget"
                ),
            )
            return file_path, False

        # Python-only syntax validation (secondary signal). Catches
        # truncation the stop_reason gate missed (older SDKs may not surface
        # stop_reason) and genuine mid-file logic bugs, BEFORE Phase B.5
        # reconciliation chokes on the broken file. No free stdlib parser
        # exists for TS/Java/Robot — those rely on the stop_reason gate above.
        if abs_path.suffix == ".py":
            import ast as _ast
            import warnings as _warnings
            try:
                with _warnings.catch_warnings():
                    _warnings.simplefilter("ignore", SyntaxWarning)
                    _ast.parse(new_content)
            except SyntaxError as e:
                _rollback()
                # stop_reason was NOT "max_tokens" here (handled above), so
                # use the position heuristic: a syntax error in the back
                # third of the file looks like truncation (bump budget);
                # elsewhere it's likely a real logic bug (bumping won't help).
                line_no = getattr(e, "lineno", 0) or 0
                written_lines = max(new_content.count("\n"), 1)
                truncation_likely = line_no >= int(written_lines * 0.66)
                if truncation_likely:
                    _arm_smart_retry("syntax_error_at_eof")
                log.error(
                    "step08.pom_syntax_invalid",
                    pom=task.pom_name,
                    file=file_path,
                    line=getattr(e, "lineno", None),
                    error=str(e),
                    chars_written=len(new_content),
                    max_tokens=dynamic_max_tokens,
                    truncation_likely=truncation_likely,
                    hint=(
                        "smart-retry armed: next attempt will use doubled max_tokens"
                        if truncation_likely else
                        "syntax error not at file end — likely a real logic bug, not truncation"
                    ),
                )
                return file_path, False

        log.info("step08.pom_extend.done", pom=task.pom_name)
        return file_path, True

    tasks_to_run = [
        _extend_one(fp, task)
        for fp, task in pom_tasks.items()
        if task.missing_methods
    ]
    if tasks_to_run:
        results = list(await asyncio.gather(*tasks_to_run))
    return results


def _detect_const_indent(lines: list[str], is_java: bool) -> str:
    """Best-effort detection of the indentation new TBD constants should use.

    Scans for an existing constant declaration and reuses its leading
    whitespace. This adapts to both module-level locator files (column 0)
    and class-body page objects (indented) instead of assuming a fixed
    4-space class-body placement.
    """
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if is_java:
            if "static final" in s and "=" in s:
                return ln[: len(ln) - len(ln.lstrip())]
        elif "=" in s:
            head = s.split("=", 1)[0].strip()
            if head and head.replace("_", "").isalnum() and head.isupper():
                return ln[: len(ln) - len(ln.lstrip())]
    # No existing constant to mirror. Java constants always live in a class
    # body; Python constants live at class-body indent only when a class wraps
    # the file, otherwise at module level.
    if is_java:
        return "    "
    for ln in lines:
        st = ln.lstrip()
        if st.startswith("class ") and st.rstrip().endswith(":"):
            return "    "
    return ""


def _match_dev_locator(
    task: _LocatorTask,
    dev_locators: dict[str, DevLocator],
) -> DevLocator | None:
    """Check if a locator task matches a dev-locator entry.

    Tier 1a: exact constant-name key match.
    Tier 1b: intent match (case-insensitive).
    """
    if not dev_locators:
        return None
    hit = dev_locators.get(task.constant_name)
    if hit:
        return hit
    intent_lower = (task.intent or "").strip().lower()
    if not intent_lower:
        return None
    for entry in dev_locators.values():
        if (entry.intent or "").strip().lower() == intent_lower:
            return entry
    return None


_SELF_ATTR_RE = _re.compile(r"^\s+self\.([A-Z][A-Z_0-9]*)\s*=\s*")


def _detect_init_placement(lines: list[str]) -> tuple[bool, str, int]:
    """Detect if locator class uses ``self.X = ...`` inside ``__init__``.

    Returns ``(use_self, indent, insert_line_idx)`` where:
      - ``use_self`` — True when new constants should be ``self.X = ...``
      - ``indent`` — the whitespace prefix to use
      - ``insert_line_idx`` — line index to insert new constants at
        (end of ``__init__`` body, before the next ``def`` or dedent)
    """
    in_init = False
    init_indent = ""
    body_indent = ""
    last_self_line = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("def __init__"):
            in_init = True
            init_indent = line[: len(line) - len(line.lstrip())]
            continue
        if in_init and stripped and not stripped.startswith("#"):
            cur_indent = line[: len(line) - len(line.lstrip())]
            if len(cur_indent) <= len(init_indent) and stripped.startswith(("def ", "class ", "@")):
                break
            if _SELF_ATTR_RE.match(line):
                body_indent = cur_indent
                last_self_line = i

    if last_self_line < 0:
        return False, "", -1

    insert_at = last_self_line + 1
    for i in range(last_self_line + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            insert_at = i + 1
            continue
        cur_indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        if len(cur_indent) <= len(init_indent) or stripped.startswith(("def ", "class ", "@")):
            break
        if _SELF_ATTR_RE.match(lines[i]):
            insert_at = i + 1
        else:
            break

    return True, body_indent, insert_at


def _find_matching_close_brace(text: str, open_idx: int) -> int:
    """Return the index of the ``}`` matching the ``{`` at ``open_idx``,
    or -1 if unbalanced. String-aware so ``{`` inside a string literal
    doesn't count toward depth.

    Mirror of ``qtea.sut_inventory._find_matching_brace`` — duplicated
    locally to avoid an import cycle (sut_inventory imports codegen bits
    in some codepaths).
    """
    if open_idx >= len(text) or text[open_idx] != "{":
        return -1
    depth = 0
    quote: str | None = None
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _locate_export_const_object_body(
    text: str, const_name: str,
) -> tuple[int, int] | None:
    """Locate the ``{...}`` body of ``export const <const_name> = {...}``.

    Returns ``(open_brace_idx, close_brace_idx)`` or ``None`` if not
    found / unbalanced. The type-annotation form
    ``export const Foo: SomeType = {...}`` is accepted.
    """
    pat = _re.compile(
        rf"export\s+const\s+{_re.escape(const_name)}\b"
        r"(?:\s*:\s*[\w<>\[\],\s.]+?)?\s*=\s*\{",
    )
    m = pat.search(text)
    if not m:
        return None
    open_brace = m.end() - 1  # position of the opening `{`
    close_brace = _find_matching_close_brace(text, open_brace)
    if close_brace == -1:
        return None
    return open_brace, close_brace


def _locate_inline_object_prop_body(
    text: str, class_name: str, prop_name: str,
) -> tuple[int, int] | None:
    """Locate the ``{...}`` body of a class property object literal:

    ::

        class <class_name> { ... <prop_name>[: <type>] = { ... } ... }

    Returns ``(open_brace_idx, close_brace_idx)`` of the inner object
    or ``None``.
    """
    class_pat = _re.compile(
        rf"(?:export\s+)?class\s+{_re.escape(class_name)}\b[^{{]*\{{",
    )
    cm = class_pat.search(text)
    if not cm:
        return None
    class_open = cm.end() - 1
    class_close = _find_matching_close_brace(text, class_open)
    if class_close == -1:
        return None
    class_body_start = class_open + 1
    prop_pat = _re.compile(
        rf"(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+)*"
        rf"{_re.escape(prop_name)}\s*"
        rf"(?::\s*(?:Record<[^>]*>|\{{[^}}]*\}}|[\w<>\[\],\s.]+?))?"
        rf"\s*=\s*\{{",
    )
    pm = prop_pat.search(text, class_body_start, class_close)
    if not pm:
        return None
    prop_open = pm.end() - 1
    prop_close = _find_matching_close_brace(text, prop_open)
    if prop_close == -1 or prop_close > class_close:
        return None
    return prop_open, prop_close


_OBJECT_LITERAL_KEY_RE = _re.compile(
    r"^(?P<indent>[ \t]*)[\w$]+\s*:",
    _re.MULTILINE,
)


def _detect_object_literal_indent(body_text: str, fallback: str = "  ") -> str:
    """Sniff the indentation of existing keys inside an object literal.

    Returns the fallback (two spaces) when the object is empty or
    contains no key-like lines.
    """
    for m in _OBJECT_LITERAL_KEY_RE.finditer(body_text):
        indent = m.group("indent")
        if indent:
            return indent
    return fallback


def _ts_runtime_import_specifier(abs_path: Path, sut_root: Path) -> str:
    """Module specifier for the vendored TS/JS runtime, relative to the
    importing file.

    The runtime is vendored at ``<sut>/tests/qtea-runtime.js`` (single
    fixed location — see ``_vendor_typescript_playwright_runtime``), so a
    POM at ``src/pages/X.ts`` must import it as ``../../tests/qtea-runtime``.
    A hardcoded ``./qtea-runtime`` is compile-fatal and invisible to both
    the compliance and body-verify gates (only ``tsc --noEmit`` catches it)
    — the H2 defect on run 20260708-121117-99f5ed.
    """
    runtime = (sut_root / "tests" / "qtea-runtime").resolve()
    rel = os.path.relpath(runtime, abs_path.parent.resolve())
    spec = rel.replace(os.sep, "/")
    if not spec.startswith("."):
        spec = "./" + spec
    return spec


def _inject_tbd_import_ts(content: str, abs_path: Path, sut_root: Path) -> str:
    """Insert ``import { tbd } from "<relpath>";`` after the last existing
    import line, or at file top if no imports exist, where ``<relpath>`` is
    computed relative to ``abs_path`` by ``_ts_runtime_import_specifier``.
    Returns the modified content.
    """
    stmt = f'import {{ tbd }} from "{_ts_runtime_import_specifier(abs_path, sut_root)}";'
    import_re = _re.compile(r"^(?:import\s.+?;|import\s+[^;]+?;)\s*$", _re.MULTILINE)
    last: _re.Match[str] | None = None
    for m in import_re.finditer(content):
        last = m
    if last is None:
        return stmt + "\n" + content
    insert_at = last.end()
    return content[:insert_at] + "\n" + stmt + content[insert_at:]


def _locator_constant_defined(content: str, constant_name: str) -> bool:
    """True if ``constant_name`` has an actual definition in ``content``.

    Deliberately stricter than a substring check. A dangling *reference*
    to a constant (e.g. ``TrialPageSelectors.CHECKBOX_LEGAL_PROTECTION``
    inside a method body, with no ``TrialPageSelectors`` entry defining
    it) must NOT count as "already present" — that exact confusion
    caused a `tbd_locators_written` 3-to-2 drift across retries on run
    20260708-121117-99f5ed: attempt 1's write-back left the constant
    name as a bare reference, and attempt 2's plain `name in content`
    check treated that reference as a valid definition and skipped
    re-inserting the sentinel. Matches the same shape the compliance
    gate (`_verify_tbd_compliance`) already accepts as a definition:
    ``NAME:``/``NAME =`` (object-literal or assignment form).
    """
    name_esc = _re.escape(constant_name)
    return bool(_re.search(rf"\b{name_esc}\b\s*[:=]", content))


def _write_object_literal_tbd_locators(
    abs_path: Path,
    tasks: list[_LocatorTask],
    dev_locators: dict[str, DevLocator],
    sut_root: Path,
) -> int:
    """Insert ``KEY: tbd("intent")`` (or dev-locator selector) entries
    into a TS/JS object literal container.

    Returns the number of NEW entries written. Zero if every task's
    constant is already present in the file. Safe to call more than
    once against the same file (idempotent, structural detection) —
    used both as the pre-agent mechanical write and as a post-agent
    re-assert (see ``_extend_poms`` caller).
    """
    if not abs_path.is_file():
        log.warning("step08.tbd_locator_file_missing", path=str(abs_path))
        return 0

    content = abs_path.read_text(encoding="utf-8")
    written = 0
    any_needs_tbd = False

    for task in tasks:
        if _locator_constant_defined(content, task.constant_name):
            continue  # already present — respect prior state
        target = _resolve_object_literal_body(content, task)
        if target is None:
            log.warning(
                "step08.object_literal_container_not_found",
                constant=task.constant_name,
                container_class=task.container_class_name,
                container_property=task.container_name,
                pattern=task.location_pattern,
                file=str(abs_path),
            )
            continue

        open_brace, close_brace = target
        body_text = content[open_brace + 1: close_brace]
        indent = _detect_object_literal_indent(body_text)
        dev_match = _match_dev_locator(task, dev_locators)
        if dev_match:
            value_expr = f'"{dev_match.selector}"'
            log.info(
                "step08.tbd_locator_dev_match",
                constant=task.constant_name,
                selector=dev_match.selector[:80],
                source=dev_match.constant_name,
            )
        else:
            value_expr = f'tbd("{task.intent}")'
            any_needs_tbd = True

        new_entry = f"{indent}{task.constant_name}: {value_expr},"

        # Ensure the previous non-whitespace char is a comma / open-brace /
        # semicolon so the inserted entry lands in valid position. If
        # missing, inject a comma at that spot.
        j = close_brace - 1
        while j >= 0 and content[j] in " \t\r\n":
            j -= 1
        needs_prior_comma = j >= 0 and content[j] not in ",{;"
        insert_text = f"\n{new_entry}"
        if needs_prior_comma:
            content = (
                content[: j + 1]
                + ","
                + content[j + 1: close_brace]
                + insert_text
                + content[close_brace:]
            )
        else:
            content = content[:close_brace] + insert_text + content[close_brace:]
        written += 1

    if any_needs_tbd and not _re.search(
        r"""from\s+['"][^'"]*qtea-runtime['"]""", content
    ):
        content = _inject_tbd_import_ts(content, abs_path, sut_root)

    if written or any_needs_tbd:
        abs_path.write_text(content, encoding="utf-8")
        log.info(
            "step08.tbd_locators_written_object_literal",
            file=str(abs_path),
            count=written,
        )
    return written


def _resolve_object_literal_body(
    content: str, task: _LocatorTask,
) -> tuple[int, int] | None:
    """Locate the object-literal ``{...}`` body this task's sentinel belongs
    in. Pattern-directed first; then a content-driven fallback.

    The fallback makes the writer robust to a Step-6 inventory *mislabel*:
    on run 20260708-121117-99f5ed a TS ``export const TrialPageSelectors =
    {…}`` object was classified ``separate_class`` (a Python-Selenium idiom),
    which routed it to the LLM-defer path with no mechanical writer. The task
    still carries ``container_class_name``, so if the file actually contains a
    matching ``export const <name> = {…}`` (or class-field object literal), we
    detect and write into it deterministically regardless of the label.
    """
    if task.location_pattern == "export_const_object":
        container = task.container_class_name or ""
        if not container:
            return None
        return _locate_export_const_object_body(content, container)
    if task.location_pattern == "inline_object_property":
        class_name = task.container_class_name or task.owning_page
        prop = task.container_name or ""
        if not class_name or not prop:
            return None
        return _locate_inline_object_prop_body(content, class_name, prop)

    # Content-driven fallback for any other (or mislabeled) pattern.
    if task.container_class_name:
        body = _locate_export_const_object_body(content, task.container_class_name)
        if body is not None:
            return body
    class_name = task.container_class_name or task.owning_page
    if class_name and task.container_name:
        body = _locate_inline_object_prop_body(
            content, class_name, task.container_name,
        )
        if body is not None:
            return body
    return None


def _verify_tbd_compliance(
    locator_tasks: list[_LocatorTask],
    sut_root: Path,
    *,
    dev_locators: dict[str, DevLocator] | None = None,
) -> list[str]:
    """Phase A3.5 — verify pom-extender obeyed the TBD contract.

    For every ``create_tbd`` locator the plan sent to the extender, the
    resulting file must contain either:

      1. A sentinel: ``<CONST> = tbd("intent")`` or the object-literal
         form ``<CONST>: tbd("intent")`` (Python / TS) OR
         ``<CONST> = Tbd.of("intent")`` (Java), OR
      2. A raw string whose value EXACTLY matches a dev-locator selector
         (dev-supplied override — legitimate).

    Any other value is an INVENTED selector — the extender ignored
    §3 of ``codegen-rules.md`` and hardcoded a locator. This is the
    exact failure mode that produced the marketing-consent XPath
    selectors on run 20260708-121117-99f5ed. Returns a list of
    violation messages; empty list means compliant.

    Contract violations are unrecoverable by retry (identical inputs
    produce identical outputs), so the caller should hard-fail Step 8
    rather than loop the extender.
    """
    dev_selectors = {d.selector for d in (dev_locators or {}).values()}
    violations: list[str] = []
    for task in locator_tasks:
        if not task.locator_file:
            # No locator source in inventory — extender placed the value
            # inline in a method body. That path has its own scrutiny
            # (Phase C xpath / hard-wait gates + the pom-assertion rule).
            continue
        abs_path = sut_root / task.locator_file
        if not abs_path.is_file():
            continue
        content = abs_path.read_text(encoding="utf-8")
        name_esc = _re.escape(task.constant_name)
        # Branch 1 — sentinel form: `CONSTANT: tbd("intent")` (object),
        # `CONSTANT = tbd("intent")` (module/class attr), or
        # `CONSTANT = Tbd.of("intent")` (Java).
        sentinel_pat = _re.compile(
            rf"\b{name_esc}\b\s*[:=]\s*(?:tbd\s*\(|Tbd\.of\s*\()",
        )
        if sentinel_pat.search(content):
            continue

        # Branch 2 — raw string literal. Non-greedy value match with
        # backref-terminated close quote handles selectors like
        # `"[data-testid='foo']"` (apostrophes inside a double-quoted
        # string) that a naive negated-class would trip on.
        string_pat = _re.compile(
            rf"""\b{name_esc}\b\s*[:=]\s*(?P<q>['"`])(?P<val>[^\n]*?)(?P=q)""",
        )
        m = string_pat.search(content)
        if not m:
            violations.append(
                f"{task.constant_name} not found in {task.locator_file} "
                f"— pom-extender failed to add the create_tbd locator"
            )
            continue
        raw_val = m.group("val") or ""
        if raw_val in dev_selectors:
            continue  # dev-locator selector match — compliant
        violations.append(
            f"{task.constant_name} in {task.locator_file} contains raw "
            f"selector {raw_val[:80]!r} — must be tbd() sentinel or a "
            f"dev-locator match (RCA-B: pom-extender invented a selector)"
        )
    return violations


def _write_tbd_locators(
    locator_tasks: list[_LocatorTask],
    sut_root: Path,
    language: str | None,
    *,
    dev_locators: dict[str, DevLocator] | None = None,
    deferral_seen: set[tuple[str, str]] | None = None,
) -> int:
    """Phase A2: mechanical append of TBD locator constants (pure Python).

    When ``dev_locators`` is provided, each task is checked against the
    dev-locator pool before emitting ``tbd("intent")``.  A match writes
    the dev-supplied selector directly; a miss writes the usual sentinel.

    The function also detects whether the target locator class uses instance
    attributes (``self.X = ...`` inside ``__init__``) and places new
    constants accordingly.

    **Convention dispatch.** Mechanical writing covers:
      - ``separate_class`` / ``module_const_bag`` — historical
        Python-Selenium convention (line-appended constants).
      - ``export_const_object`` / ``inline_object_property`` — TS/JS
        object-literal conventions (inserted before the object's
        closing ``}``). These are the shapes the pom-extender used to
        mis-handle: without pre-writing, the extender was told
        "sentinels are pre-declared" while looking at a file with none,
        and invented raw selectors to satisfy the prompt. Pre-writing
        makes that prompt invariant true.

    ``readonly_locator_props`` remains DEFERRED to the POM extender —
    each entry is a call expression (``readonly submitBtn = () =>
    this.page.getByRole(...)``), not a key-value pair, so a mechanical
    ``tbd()`` sentinel would need extra structural work (the extender
    handles this today with its live view of the class body).
    """
    if not locator_tasks:
        return 0

    # Run-scoped dedup: this function runs up to 4× per run (Phase A2
    # pre-write + Phase A3.25 re-assert, × MAX_ATTEMPTS), each pass logging
    # the same deferral for every non-mechanical constant. Suppress repeats
    # so a given (constant, file) deferral is logged once per run. When the
    # caller passes no set (e.g. unit tests) dedup is disabled.
    def _first_deferral(constant: str, file_key: str) -> bool:
        if deferral_seen is None:
            return True
        key = (constant, file_key)
        if key in deferral_seen:
            return False
        deferral_seen.add(key)
        return True

    by_file: dict[str, list[_LocatorTask]] = {}
    _MECHANICAL_PATTERNS: frozenset[str | None] = frozenset({
        None, "separate_class", "module_const_bag",
        "export_const_object", "inline_object_property",
    })
    for task in locator_tasks:
        mechanical = task.location_pattern in _MECHANICAL_PATTERNS
        if task.locator_file and mechanical:
            by_file.setdefault(task.locator_file, []).append(task)
        elif task.locator_file and not mechanical:
            # Convention detected but this function's mechanical writer
            # doesn't yet know how to append to it — POM extender handles.
            if _first_deferral(task.constant_name, task.locator_file):
                log.info(
                    "step08.tbd_locator_deferred_to_extender",
                    constant=task.constant_name,
                    owning_page=task.owning_page,
                    pattern=task.location_pattern,
                    container=task.container_name,
                    file=task.locator_file,
                    reason=(
                        f"SUT uses {task.location_pattern!r} convention; "
                        f"POM extender will add the constant in the same style"
                    ),
                )
        else:
            # No matching locator source in inventory. The POM extender
            # will emit the locator inline in the method body (Playwright
            # `getBy*` / Selenium `driver.findElement`) — respects any
            # SUT that doesn't keep locators in a separate structure.
            log.info(
                "step08.tbd_locator_no_source_defer",
                constant=task.constant_name,
                owning_page=task.owning_page,
                reason=(
                    f"No existing locator source found for POM "
                    f"{task.owning_page!r}; POM extender will emit "
                    f"the locator inline in the method body"
                ),
            )

    written = 0
    is_java = (language or "").lower() == "java"
    is_ts_like = (language or "").lower() in {"typescript", "javascript"}
    dev_locs = dev_locators or {}

    _OBJECT_LITERAL_PATTERNS = frozenset({
        "export_const_object", "inline_object_property",
    })

    for file_path, tasks in by_file.items():
        abs_path = sut_root / file_path
        if not abs_path.is_file():
            log.warning("step08.tbd_locator_file_missing", path=file_path)
            continue

        # Partition tasks by dispatch mode: object-literal insertion (TS
        # object shapes) vs linear append (Python / Java historical
        # patterns). Object-literal writing MUTATES THE FILE first so
        # that any subsequent linear-append pass sees the updated
        # content and doesn't misdetect placement.
        object_tasks = [
            t for t in tasks if t.location_pattern in _OBJECT_LITERAL_PATTERNS
        ]
        linear_tasks = [
            t for t in tasks if t.location_pattern not in _OBJECT_LITERAL_PATTERNS
        ]

        # The linear/mechanical writer below only knows two placement
        # idioms: Java (`public static final String X = ...`) and Python
        # (`self.X = ...` inside `__init__`, via `_detect_init_placement`,
        # which only recognises `def __init__`/`self.`). A TS/JS locator
        # task that isn't object-literal-shaped has no linear idiom this
        # writer understands — defer to the POM extender (which has a
        # live view of the class body) instead of guessing Python and
        # emitting a bare module-scope `CONST = tbd(...)` plus a Python
        # `from tests.qtea_runtime import tbd` import into a `.ts` file
        # (the exact H2 defect from run 20260708-121117-99f5ed).
        if linear_tasks and is_ts_like:
            # A TS/JS task labeled with a non-object-literal pattern (e.g. a
            # Step-6 `separate_class` mislabel on an `export const <X> = {…}`
            # object) has no linear idiom this writer understands. Before
            # deferring to the LLM extender, probe the file: if it actually
            # contains a resolvable object-literal container, PROMOTE the task
            # to the deterministic object-literal writer — this is what closes
            # the coherence trap (the extender then sees the sentinel already
            # present and can't invent a selector). Only genuinely
            # unresolvable shapes stay deferred.
            probe = abs_path.read_text(encoding="utf-8")
            for t in linear_tasks:
                if _resolve_object_literal_body(probe, t) is not None:
                    object_tasks.append(t)
                    continue
                if not _first_deferral(t.constant_name, file_path):
                    continue
                log.info(
                    "step08.tbd_locator_deferred_to_extender",
                    constant=t.constant_name,
                    owning_page=t.owning_page,
                    pattern=t.location_pattern,
                    file=file_path,
                    reason=(
                        f"{language} locator file uses a non-object-literal "
                        f"convention {t.location_pattern!r} and no object-"
                        f"literal container was found in the file; the linear "
                        f"mechanical writer only supports Python (self.X) / "
                        f"Java (public static final) placement idioms — "
                        f"POM extender will add the constant in the file's "
                        f"own style"
                    ),
                )
            linear_tasks = []

        if object_tasks:
            written += _write_object_literal_tbd_locators(
                abs_path, object_tasks, dev_locs, sut_root,
            )

        if not linear_tasks:
            continue

        content = abs_path.read_text(encoding="utf-8")
        lines = content.rstrip().split("\n")

        # Detect instance-attribute placement before potentially adding imports.
        use_self, self_indent, init_insert_idx = _detect_init_placement(lines)

        # Determine if ANY task will need the tbd import (i.e. has no
        # dev-locator match). Skip the import when every task is satisfied
        # by dev-locators — no tbd() calls will be emitted.
        any_needs_tbd = any(
            _match_dev_locator(t, dev_locs) is None
            for t in linear_tasks
            if not _locator_constant_defined(content, t.constant_name)
        )

        tbd_import = "from tests.qtea_runtime import tbd"
        if is_java:
            tbd_import = "import com.qtea.runtime.Tbd;"
        needs_import = (
            any_needs_tbd
            and tbd_import not in content
            and "import tbd" not in content.lower()
        )

        if needs_import:
            if is_java:
                for i, line in enumerate(lines):
                    if line.strip().startswith("package ") and line.rstrip().endswith(";"):
                        lines.insert(i + 1, "")
                        lines.insert(i + 2, tbd_import)
                        if use_self and init_insert_idx > i:
                            init_insert_idx += 2
                        break
                else:
                    lines.insert(0, tbd_import)
                    if use_self:
                        init_insert_idx += 1
            else:
                for i, line in enumerate(lines):
                    if line.startswith("import ") or line.startswith("from "):
                        lines.insert(i, tbd_import)
                        if use_self and init_insert_idx > i:
                            init_insert_idx += 1
                        break
                else:
                    lines.insert(0, tbd_import)
                    if use_self:
                        init_insert_idx += 1

        # Determine the indentation new constants should carry.
        const_indent = self_indent if use_self else _detect_const_indent(lines, is_java)

        new_lines: list[str] = []
        for task in linear_tasks:
            if _locator_constant_defined(content, task.constant_name):
                log.debug(
                    "step08.tbd_locator_exists",
                    constant=task.constant_name,
                )
                continue

            dev_match = _match_dev_locator(task, dev_locs)
            if dev_match:
                selector = dev_match.selector
                if is_java:
                    new_lines.append(
                        f'{const_indent}public static final String '
                        f'{task.constant_name} = "{selector}";'
                    )
                elif use_self:
                    new_lines.append(
                        f'{const_indent}self.{task.constant_name} = '
                        f'"{selector}"'
                    )
                else:
                    new_lines.append(
                        f'{const_indent}{task.constant_name} = "{selector}"'
                    )
                log.info(
                    "step08.tbd_locator_dev_match",
                    constant=task.constant_name,
                    selector=selector[:80],
                    source=dev_match.constant_name,
                )
            elif is_java:
                new_lines.append(
                    f'{const_indent}public static final String '
                    f'{task.constant_name} = Tbd.of("{task.intent}");'
                )
            elif use_self:
                new_lines.append(
                    f'{const_indent}self.{task.constant_name} = '
                    f'tbd("{task.intent}")'
                )
            else:
                new_lines.append(
                    f'{const_indent}{task.constant_name} = '
                    f'tbd("{task.intent}")'
                )
            written += 1

        if new_lines:
            if use_self and init_insert_idx >= 0:
                insert_at = init_insert_idx
            else:
                insert_at = len(lines)
                for i in range(len(lines) - 1, -1, -1):
                    stripped = lines[i].strip()
                    if stripped and not stripped.startswith("#") and stripped != "":
                        insert_at = i + 1
                        break
            for nl in new_lines:
                lines.insert(insert_at, nl)
                insert_at += 1

            abs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info(
                "step08.tbd_locators_written",
                file=file_path,
                count=len(new_lines),
            )

    return written


_HARDCODED_LOCATOR_RE = _re.compile(
    r"^(\s*(?:self\.|this\.)?)"   # optional indent + optional self./this.
    r"([A-Z][A-Z_0-9]*)"         # UPPERCASE constant name
    r"\s*=\s*"                    # assignment
    r"""(["'])(.+?)\3"""          # quoted string value
    r";?\s*$",                    # optional trailing `;` (TS/JS), end of line
)


def _run_phase_b55_xpath_normalisation(
    sut_root: Path,
    candidates: set[Path],
) -> tuple[list[RewriteReport], list[XpathSite]]:
    """Phase B.5.5 — deterministic XPath → Playwright locator rewrite.

    Walks every ``.ts`` / ``.js`` / ``.mts`` / ``.mjs`` file in *candidates*
    (the codegen-modified set), invokes ``qtea.xpath_rewriter.rewrite_file``
    on each, and — if any rewrite emitted a ``getByTestId(...)`` call —
    idempotently adds ``testIdAttribute: 'data-test'`` to the SUT's
    playwright config.

    Returns ``(reports, stragglers)`` where ``reports`` covers every file
    the rewriter touched and ``stragglers`` aggregates the xpath sites the
    deterministic layer refused to translate. The caller feeds stragglers
    into the LLM violation-fixer via the existing gate path — the exempt
    marker the rewriter stamps keeps the quality gate from failing on
    them regardless of whether the LLM succeeds.
    """
    # Unconditional entry log so operators can confirm this phase fired
    # even if every downstream step below is a no-op. Debugging aid: if
    # `step08.b55.started` is missing from `run.log.jsonl`, Phase B.5.5
    # was NOT invoked — check the call site in `CodegenStep.run` and any
    # early-return that might have skipped it.
    log.info(
        "step08.b55.started",
        candidates=len(candidates),
        sut_root=str(sut_root),
    )

    reports: list[RewriteReport] = []
    stragglers: list[XpathSite] = []
    testid_needed = False
    changed_files: list[Path] = []

    ts_suffixes = {".ts", ".js", ".mts", ".mjs", ".cts", ".cjs"}
    for p in sorted(candidates):
        if not p.is_file() or p.suffix.lower() not in ts_suffixes:
            continue
        try:
            report = rewrite_file(p)
        except Exception as e:
            log.warning(
                "step08.b55.rewrite_failed",
                path=str(p.relative_to(sut_root))
                if p.is_relative_to(sut_root) else str(p),
                error=str(e),
            )
            continue
        if report.rewritten or report.stragglers or report.container_migrated:
            reports.append(report)
            stragglers.extend(report.stragglers)
            if report.testid_attr_needed:
                testid_needed = True
            if report.changed:
                changed_files.append(p)

    if testid_needed:
        cfg_edit = ensure_test_id_attribute(sut_root, attr_name="data-test")
        log.info(
            "step08.b55.playwright_config",
            reason=cfg_edit.reason,
            changed=cfg_edit.changed,
            path=str(cfg_edit.path.relative_to(sut_root))
            if cfg_edit.path and cfg_edit.path.is_relative_to(sut_root)
            else None,
        )

    log.info(
        "step08.b55.xpath_normalised",
        files_touched=len(changed_files),
        rewritten=sum(len(r.rewritten) for r in reports),
        stragglers=len(stragglers),
        call_sites_migrated=sum(r.call_sites_migrated for r in reports),
        containers_migrated=sum(1 for r in reports if r.container_migrated),
    )
    return reports, stragglers


def _scan_and_convert_hardcoded_locators(
    sut_root: Path,
    codegen_modified: set[Path],
    dev_locators: dict[str, DevLocator] | None,
    language: str | None = None,
) -> int:
    """Safety net: find hardcoded selector assignments in codegen-modified
    locator/POM files and convert them to ``tbd()`` sentinels.

    Only processes lines added by codegen (new in the git diff).  Returns
    the number of constants converted.
    """
    import subprocess as _sp

    dev_locs = dev_locators or {}
    dev_selectors = {e.selector for e in dev_locs.values()}
    converted = 0
    is_java = (language or "").lower() == "java"
    is_ts_like = (language or "").lower() in {"typescript", "javascript"}
    _pages_suffixes = (".py", ".ts", ".tsx", ".js", ".jsx")

    for abs_path in sorted(codegen_modified):
        if not abs_path.is_file():
            continue
        try:
            rel = abs_path.relative_to(sut_root)
        except ValueError:
            continue
        name_low = rel.name.lower()
        parts_low = [p.lower() for p in rel.parts]
        is_locator_file = (
            "locator" in name_low
            or "locators" in parts_low
            or ("pages" in parts_low and name_low.endswith(_pages_suffixes))
        )
        if not is_locator_file:
            continue

        diff_result = _sp.run(
            ["git", "diff", "HEAD", "--", str(rel.as_posix())],
            cwd=str(sut_root), capture_output=True, text=True,
            timeout=15, check=False,
        )
        if diff_result.returncode != 0:
            continue
        added_lines: set[str] = set()
        for line in diff_result.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.add(line[1:])

        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = content.split("\n")
        file_converted = 0
        for i, line in enumerate(lines):
            if line not in added_lines:
                continue
            if "tbd(" in line or "Tbd.of(" in line or "TBD_LOCATOR" in line:
                continue
            m = _HARDCODED_LOCATOR_RE.match(line)
            if not m:
                continue
            prefix, const_name, _q, selector = m.groups()
            if selector in dev_selectors:
                continue
            has_self = "self." in prefix
            has_this = "this." in prefix
            indent = prefix.replace("self.", "").replace("this.", "")
            trailing_semi = ";" if is_ts_like and line.rstrip().endswith(";") else ""
            if has_self:
                lines[i] = f'{indent}self.{const_name} = tbd("{const_name}"){trailing_semi}'
            elif has_this:
                lines[i] = f'{indent}this.{const_name} = tbd("{const_name}"){trailing_semi}'
            else:
                lines[i] = f'{indent}{const_name} = tbd("{const_name}"){trailing_semi}'
            file_converted += 1
            log.warning(
                "step08.hardcoded_locator_converted",
                file=str(rel),
                constant=const_name,
                old_selector=selector[:80],
            )

        converted += file_converted
        if file_converted:
            if is_java:
                tbd_import = "import com.qtea.runtime.Tbd;"
            elif is_ts_like:
                tbd_import = (
                    f'import {{ tbd }} from '
                    f'"{_ts_runtime_import_specifier(abs_path, sut_root)}";'
                )
            else:
                tbd_import = "from tests.qtea_runtime import tbd"
            joined = "\n".join(lines)
            already_imported = (
                _re.search(r"""from\s+['"][^'"]*qtea-runtime['"]""", joined)
                if is_ts_like
                else (tbd_import in joined or "import tbd" in joined.lower())
            )
            if not already_imported:
                if is_ts_like:
                    joined = _inject_tbd_import_ts(joined, abs_path, sut_root)
                    lines = joined.split("\n")
                else:
                    for j, ln in enumerate(lines):
                        if ln.startswith("import ") or ln.startswith("from "):
                            lines.insert(j, tbd_import)
                            break
                    else:
                        lines.insert(0, tbd_import)
            abs_path.write_text("\n".join(lines), encoding="utf-8")

    if converted:
        log.info("step08.hardcoded_locator_scan", converted=converted)
    return converted


def _group_fixture_tasks_by_file(
    fixture_tasks: list[_FixtureTask],
) -> dict[str, list[_FixtureTask]]:
    """Collate fixtures by target file so each file gets one LLM call.

    Without this, parallel `asyncio.gather` calls all read the same starting
    `existing` content and overwrite each other (last writer wins) — silently
    dropping every fixture except one. See run 20260611-184450-1fbf3d for the
    incident where 5 of 6 fixtures vanished.
    """
    by_file: dict[str, list[_FixtureTask]] = {}
    for task in fixture_tasks:
        if not task.at:
            continue
        by_file.setdefault(task.at, []).append(task)
    return by_file


async def _create_fixtures(
    fixture_tasks: list[_FixtureTask],
    sut_root: Path,
    workdir: Path,
    agents_root: Path,
    active_module: dict[str, Any] | None,
    step: int,
    rules_content: str = "",
) -> list[tuple[str, bool]]:
    """Phase A4: create new fixtures via call_reasoning_llm.

    One LLM call per target file (not per fixture) — all fixtures destined
    for the same file are created in a single pass to avoid the read/write
    race that drops co-located fixtures.
    """
    if not fixture_tasks:
        return []

    agent_path = agents_root / "codegen-pom-extender.agent.md"
    sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)

    existing_fixtures = (active_module or {}).get("existing_fixtures") or []
    style_ref = ""
    if existing_fixtures and existing_fixtures[0].get("file"):
        ref_path = sut_root / existing_fixtures[0]["file"]
        if ref_path.is_file():
            try:
                raw_ref = ref_path.read_text(encoding="utf-8")
                # Truncate at a clean line boundary so the LLM never sees a
                # mid-statement cut (e.g. `parser.addoption(` with no closer).
                # A truncated style reference once caused the LLM to copy the
                # broken fragment verbatim into the generated fixture file,
                # producing unparseable Python that the reconciler reported
                # as `fixture_file_missing` for every declared fixture.
                head = raw_ref[:3000]
                last_nl = head.rfind("\n")
                style_ref = head[:last_nl] if last_nl > 0 else head
            except OSError:
                pass

    by_file = _group_fixture_tasks_by_file(fixture_tasks)

    async def _create_file(file_path: str, tasks: list[_FixtureTask]) -> tuple[str, bool]:
        specs = [
            {
                "name": t.name,
                "yields": t.yields,
                "scope": t.scope,
                "depends_on": t.depends_on,
            }
            for t in tasks
        ]
        existing = ""
        target = sut_root / file_path
        if target.is_file():
            with contextlib.suppress(OSError):
                existing = target.read_text(encoding="utf-8")

        inputs: dict[str, str] = {
            "fixture_specs.json": json.dumps(specs, indent=2),
        }
        if existing:
            inputs["existing_file.py"] = existing
        if style_ref:
            inputs["style_reference.py"] = style_ref
        if rules_content:
            inputs["codegen-rules.md"] = rules_content

        # Inject auth/dependency context when fixtures declare depends_on
        dep_fixture_names: set[str] = set()
        for t in tasks:
            dep_fixture_names.update(t.depends_on)

        dep_clause = ""
        if dep_fixture_names and active_module:
            for inv_fix in (active_module.get("existing_fixtures") or []):
                if inv_fix.get("name") in dep_fixture_names:
                    dep_file = inv_fix.get("file")
                    if dep_file:
                        dep_path = sut_root / dep_file
                        if dep_path.is_file():
                            try:
                                dep_source = dep_path.read_text(
                                    encoding="utf-8",
                                )
                                inputs[
                                    f"dep_fixture_{inv_fix['name']}.py"
                                ] = dep_source
                            except OSError:
                                pass
            auth_flow = active_module.get("auth_flow")
            if auth_flow:
                inputs["auth_flow.json"] = json.dumps(auth_flow, indent=2)
            dep_names = ", ".join(sorted(dep_fixture_names))
            dep_clause = (
                f" The new fixture(s) depend on existing fixture(s): "
                f"{dep_names}. The source of each depended-on fixture is "
                f"provided as `dep_fixture_<name>.py`. The new fixture(s) "
                f"MUST request the depended-on fixture as a pytest "
                f"parameter and build on top of its yielded object — do "
                f"NOT re-implement authentication or session setup. If "
                f"`auth_flow.json` is provided, it describes the SUT's "
                f"authentication mechanism."
            )

        names = ", ".join(t.name for t in tasks)
        async with sem:
            log.info(
                "step08.fixture_create.start",
                file=file_path,
                fixtures=len(tasks),
                names=names,
            )
            result = await call_reasoning_llm(
                agent_path,
                workdir=workdir,
                user_prompt=(
                    f"Create {len(tasks)} pytest fixture(s) — {names} — "
                    f"matching the specs in `fixture_specs.json`. ALL "
                    f"specified fixtures must appear in the output. "
                    f"If `existing_file.py` is provided, append the new "
                    f"fixtures to it and return the complete updated file "
                    f"(existing content + new fixtures). Otherwise return "
                    f"a complete new file containing ONLY the requested "
                    f"fixtures plus the imports they need. "
                    f"`style_reference.py` shows coding conventions only "
                    f"(import grouping, fixture scope, naming) — do NOT "
                    f"copy its content into your output. The output must "
                    f"be syntactically valid Python.{dep_clause}"
                ),
                inputs=inputs,
                step=step,
                timeout_s=120,
                max_tokens=4000 + 1000 * len(tasks),
            )

        if result.success and result.final_text.strip():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                clean = _strip_code_fences(result.final_text)
                # When `existing` was supplied, the agent is instructed to
                # return the COMPLETE file. Log a noticeable shrink so an
                # agent that wrongly returns only the new fixtures
                # (clobbering the existing file) is diagnosable from the
                # run log rather than failing silently.
                if existing and len(clean) < len(existing) // 2:
                    log.warning(
                        "step08.fixture_overwrite_shrink",
                        file=file_path,
                        prev_bytes=len(existing),
                        new_bytes=len(clean),
                        hint="agent may have dropped existing content",
                    )
                target.write_text(clean, encoding="utf-8")
                # Validate Python syntax post-write. Mirrors the POM
                # extender's gate (see `_extend_one`): catches truncated /
                # malformed output BEFORE the reconciler chokes on it and
                # reports misleading `fixture_file_missing` for every
                # declared fixture. Roll back so the next attempt starts
                # from the prior file state (or no file, if newly created).
                if target.suffix == ".py":
                    import ast as _ast
                    import warnings as _warnings
                    try:
                        with _warnings.catch_warnings():
                            _warnings.simplefilter("ignore", SyntaxWarning)
                            _ast.parse(clean)
                    except SyntaxError as e:
                        try:
                            if existing:
                                target.write_text(existing, encoding="utf-8")
                            else:
                                target.unlink(missing_ok=True)
                        except OSError:
                            pass
                        log.error(
                            "step08.fixture_syntax_invalid",
                            file=file_path,
                            line=getattr(e, "lineno", None),
                            error=str(e),
                            chars_written=len(clean),
                            hint="rolled back; next attempt will regenerate",
                        )
                        return file_path, False
                # Verify each requested fixture name actually appears as a
                # `def <name>` in the written file. A missing name surfaces
                # immediately in the log AND fails the file so reconcile
                # (Fix 2) catches it.
                missing = [
                    t.name for t in tasks
                    if _re.search(rf"^\s*def\s+{_re.escape(t.name)}\s*\(", clean, _re.M) is None
                ]
                if missing:
                    log.error(
                        "step08.fixture_create.symbols_missing",
                        file=file_path,
                        missing=missing,
                    )
                    return file_path, False
                log.info(
                    "step08.fixture_create.done",
                    file=file_path,
                    fixtures=len(tasks),
                )
                return file_path, True
            except OSError as e:
                log.error(
                    "step08.fixture_write_failed",
                    file=file_path,
                    error=str(e),
                )
                return file_path, False
        else:
            log.warning(
                "step08.fixture_create.failed",
                file=file_path,
                error=result.error,
            )
            return file_path, False

    results = list(await asyncio.gather(
        *[_create_file(fp, tasks) for fp, tasks in by_file.items()]
    ))
    return results


def _collect_referenced_methods(plan: dict[str, Any]) -> dict[str, set[str]]:
    """Map each POM class name → set of method names its choreography calls.

    Sourced from every ``test_functions[].steps[]`` entry (``pom`` + ``method``)
    across all test cases. Used to decide which PRE-EXISTING POM methods the
    writer needs real signatures for.
    """
    refs: dict[str, set[str]] = {}
    for tc in plan.get("test_cases") or []:
        for fn in tc.get("test_functions") or []:
            for st in fn.get("steps") or []:
                if not isinstance(st, dict):
                    continue
                pom = st.get("pom")
                method = st.get("method")
                if pom and method:
                    refs.setdefault(pom, set()).add(method)
    return refs


def _build_imports_manifest(
    plan: dict[str, Any],
    pom_tasks: dict[str, _PomTask],
    locator_tasks: list[_LocatorTask],
    fixture_tasks: list[_FixtureTask],
    helper_tasks: list[_HelperTask],
    sut_root: Path,
    active_module: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase B1: build the imports manifest for the test writer."""
    # Resolve the POM-parser language the same way the B.5 arity gate does —
    # active-module first (authoritative), plan.language second (often null),
    # python last — so the manifest's signature extractor and the reconciler
    # never parse the SUT with different language assumptions.
    language = (
        (active_module or {}).get("language")
        or plan.get("language")
        or "python"
    )
    referenced = _collect_referenced_methods(plan)
    pom_files = []
    for fp, task in pom_tasks.items():
        added_names = {m["name"] for m in task.missing_methods}
        # Give the writer real signatures for PRE-EXISTING POM methods the
        # choreography references. Without this the writer only sees signatures
        # for NEWLY-CREATED methods (methods_added_detail) and defaults to
        # zero-arg stub calls for reused methods — the exact defect that made
        # switchUser()/approveReview()/assertRopaStatus() fail reconciliation.
        existing_methods_detail: list[dict[str, str]] = []
        wanted = referenced.get(task.pom_name, set()) - added_names
        if wanted:
            pom_abs = sut_root / fp
            try:
                if pom_abs.is_file() and pom_abs.stat().st_size <= 2_000_000:
                    text = pom_abs.read_text(encoding="utf-8", errors="replace")
                    sigs = pom_method_signatures(text, task.pom_name, language)
                    existing_methods_detail = [
                        {"name": n, "signature": sigs[n]}
                        for n in sorted(wanted)
                        if n in sigs
                    ]
            except OSError as e:
                log.warning(
                    "step08.manifest_pom_read_failed", file=fp, error=str(e),
                )
        pom_files.append({
            "class_name": task.pom_name,
            "file": fp,
            "import_path": fp.replace("/", ".").replace("\\", ".").removesuffix(".py"),
            "methods_added": [m["name"] for m in task.missing_methods],
            # Full signatures so the writer knows arity/params when
            # transpiling the choreography (steps[]) into POM calls — the
            # bare names above are insufficient to emit a correct call site.
            "methods_added_detail": [
                {"name": m["name"], "signature": m.get("signature")}
                for m in task.missing_methods
            ],
            # Signatures for PRE-EXISTING referenced methods, read from disk.
            "existing_methods_detail": existing_methods_detail,
            "locator_class": task.locator_class,
            "locator_file": task.locator_file,
        })

    tbd_locators = [
        {
            "constant_name": t.constant_name,
            "file": t.locator_file or "",
            "intent": t.intent,
            "owning_page": t.owning_page,
        }
        for t in locator_tasks
    ]

    fixtures_created = [
        {"name": t.name, "file": t.at, "yields": t.yields, "scope": t.scope}
        for t in fixture_tasks
    ]

    helpers_created = [
        {"name": t.name, "file": t.at, "signature": t.signature}
        for t in helper_tasks
    ]

    existing_fixtures: dict[str, str] = {}
    for tc in plan.get("test_cases") or []:
        for fix in tc.get("fixtures") or []:
            if fix.get("source") == "reuse" and fix.get("from"):
                existing_fixtures[fix["name"]] = fix["from"]

    return {
        "language": plan.get("language"),
        "framework": plan.get("framework"),
        "sut_root": str(sut_root),
        "pom_files": pom_files,
        "tbd_locators_added": tbd_locators,
        "fixtures_created": fixtures_created,
        "helpers_created": helpers_created,
        "existing_fixtures": existing_fixtures,
    }


def _filter_strategy_for_tcs(strategy_text: str, tc_ids: list[str]) -> str:
    """Extract only the relevant #### TC-<id>: sections from the strategy."""
    if not tc_ids:
        return strategy_text

    sections: list[str] = []
    current: list[str] = []
    current_id: str | None = None
    tc_set = set(tc_ids)

    for line in strategy_text.split("\n"):
        m = _re.match(r"^####\s+(TC-[^:\s]+)", line)
        if m:
            if current_id and current_id in tc_set:
                sections.append("\n".join(current))
            current = [line]
            current_id = m.group(1)
        elif current_id is not None:
            current.append(line)

    if current_id and current_id in tc_set:
        sections.append("\n".join(current))

    return "\n\n".join(sections) if sections else strategy_text


async def _generate_test_files(
    plan: dict[str, Any],
    strategy_text: str,
    manifest: dict[str, Any],
    sut_root: Path,
    workdir: Path,
    agents_root: Path,
    reuse_hint: str,
    runtime_hint: str,
    env_hint: str,
    step: int,
    rules_content: str = "",
) -> list[tuple[str, bool]]:
    """Phase B2: generate test files via call_reasoning_llm (one per target)."""
    agent_path = agents_root / "codegen-test-writer.agent.md"
    sem = asyncio.Semaphore(_MAX_CONCURRENT_LLM_CALLS)

    by_target: dict[str, list[dict[str, Any]]] = {}
    for tc in plan.get("test_cases") or []:
        target = tc.get("test_file_target", "tests/qteaest.py")
        by_target.setdefault(target, []).append(tc)

    if not by_target:
        return []

    async def _generate_one(
        target: str, tcs: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        tc_ids = [tc.get("id", "") for tc in tcs]
        filtered_strategy = _filter_strategy_for_tcs(strategy_text, tc_ids)

        sub_plan = {
            "plan_version": plan.get("plan_version"),
            "active_module": plan.get("active_module"),
            "language": plan.get("language"),
            "framework": plan.get("framework"),
            "test_cases": tcs,
        }

        abs_target = sut_root / target
        inputs = {
            "plan.json": json.dumps(sub_plan, indent=2),
            "strategy.md": filtered_strategy,
            "imports.json": json.dumps(manifest, indent=2),
        }
        if rules_content:
            inputs["codegen-rules.md"] = rules_content

        prompt = (
            f"Generate a complete test file to be written at "
            f"`{abs_target}`. The plan contains {len(tcs)} test case(s): "
            f"{', '.join(tc_ids)}. "
            f"Use `plan.json` for structure (test functions, fixtures, markers) "
            f"and `strategy.md` only to source the exact VALUES (expected strings, "
            f"URLs, counts) named in the plan's `kind: \"assertion\"` "
            f"`acceptance_criteria` — do not add an assertion for an Expected-Result "
            f"bullet that has no corresponding `kind: \"assertion\"` method in "
            f"`plan.json`. "
            f"Use `imports.json` to know what POM classes, locators, and "
            f"fixtures are available to import (see `pom_files[].methods_added_detail` "
            f"for method signatures). "
            f"When a test_function in `plan.json` carries a `steps[]` array, "
            f"transpile those entries IN ASCENDING `order` into the test body — "
            f"one POM method call per entry (`<pom>.<method>(...)`), sourcing "
            f"exact argument values from `strategy.md`. Do NOT re-derive the "
            f"action sequence from prose when `steps[]` is present. Only fall "
            f"back to inferring the sequence from `strategy.md` when a "
            f"test_function has no `steps[]`. Emit exactly one assertion call per "
            f"plan-classified `kind: \"assertion\"` method, appended after the "
            f"choreographed actions — never one per Expected-Result bullet."
            f"{env_hint}{runtime_hint}{reuse_hint}"
        )

        async with sem:
            log.info(
                "step08.test_gen.start",
                target=target,
                test_cases=len(tcs),
            )
            result = await call_reasoning_llm(
                agent_path,
                workdir=workdir,
                user_prompt=prompt,
                inputs=inputs,
                step=step,
                timeout_s=180,
                max_tokens=32000,
            )

        if result.success and result.final_text.strip():
            try:
                abs_target.parent.mkdir(parents=True, exist_ok=True)
                abs_target.write_text(_strip_code_fences(result.final_text), encoding="utf-8")
                log.info("step08.test_gen.done", target=target)
                return target, True
            except OSError as e:
                log.error(
                    "step08.test_gen.write_failed",
                    target=target, error=str(e),
                )
                return target, False
        else:
            log.warning(
                "step08.test_gen.failed",
                target=target, error=result.error,
            )
            return target, False

    results = list(await asyncio.gather(
        *[_generate_one(t, tcs) for t, tcs in by_target.items()]
    ))
    return results


# ---------------------------------------------------------------------------
# Phase D: TBD intent quality gate
# ---------------------------------------------------------------------------
#
# After Phases A-C have written code and the indexer's quality gate has
# passed, score every `tbd("intent")` / `Tbd.of("intent")` call-site for
# resolver-quality. Low-quality intents (vague, literal CSS, empty) waste
# runtime tokens and cause unrecoverable resolution failures — better to
# block here than at Step 9.
#
# - FAIL → step fails (overridable via QTEA_INTENT_FAIL_AS_WARN=1).
# - WARN → step succeeds but warnings are stashed on `ctx.extras` for the
#   post-Step-8 review gate to surface on TTY.
# - QTEA_SKIP_INTENT_SCORE=1 skips Phase D entirely (no scoring, no gate).


_INTENT_QUALITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results"],
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["intent", "score", "rationale"],
                "additionalProperties": False,
                "properties": {
                    "intent": {"type": "string"},
                    "score": {"enum": ["PASS", "WARN", "FAIL"]},
                    "rationale": {"type": "string", "maxLength": 200},
                },
            },
        },
    },
}


def _extract_code_context(
    sut_root: Path, rel_path: Path, line: int, radius: int = 3,
) -> str:
    """Return a few source lines around *line* with line numbers.

    Best-effort: returns ``""`` on any I/O error so callers never fail.
    """
    try:
        abs_path = sut_root / rel_path
        all_lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - 1 - radius)
        end = min(len(all_lines), line + radius)
        parts: list[str] = []
        for i in range(start, end):
            marker = ">" if i == line - 1 else " "
            parts.append(f"{marker} {i + 1:4d} | {all_lines[i]}")
        return "\n".join(parts)
    except Exception:
        return ""


async def _phase_d_score_intents(
    produced_in_sut: list[Path],
    jit_files_added: list[Path],
    sut_root: Path,
    out_dir: Path,
    workdir: Path,
    agents_root: Path,
) -> tuple[bool, dict[str, Any], list[dict[str, Any]], str | None]:
    """Score every TBD sentinel in produced sources. Returns
    ``(success, summary_dict, warnings_list, error_message)``.

    - ``success`` is False only when at least one FAIL surfaces AND
      ``QTEA_INTENT_FAIL_AS_WARN`` is not set to 1.
    - ``summary_dict`` is the persisted artifact payload.
    - ``warnings_list`` carries every WARN entry (and every FAIL when
      FAIL_AS_WARN is in effect) for the post-step review gate.
    - ``error_message`` is a short user-facing string on failure.
    """
    # Local import keeps the qtea boot path light when callers don't
    # exercise codegen (tests, CLI subcommands).
    from qtea.tbd_scanner import scan_tbd_intents

    if os.environ.get("QTEA_SKIP_INTENT_SCORE") == "1":
        log.info("step08.phase_d.skipped", reason="QTEA_SKIP_INTENT_SCORE=1")
        return True, {"skipped": True, "reason": "env_skip"}, [], None

    jit_resolved = {p.resolve() for p in jit_files_added if p.exists()}
    scan_paths: list[Path] = []
    seen: set[Path] = set()
    for p in produced_in_sut:
        resolved = p.resolve() if p.exists() else p
        if resolved in seen or resolved in jit_resolved:
            continue
        seen.add(resolved)
        scan_paths.append(p)

    intents = scan_tbd_intents(scan_paths, sut_root)
    if not intents:
        log.info("step08.phase_d.no_intents")
        empty = {
            "results": [],
            "summary": {"pass": 0, "warn": 0, "fail": 0, "total": 0},
        }
        return True, empty, [], None

    # Build a deterministic input payload — anchors needed by the post-step
    # editor live alongside the intent string so the model has the file:line
    # context without having to invent it.
    payload_intents = [
        {
            "intent": t.intent,
            "context": f"{str(t.file).replace(chr(92), '/')}:{t.line}",
        }
        for t in intents
    ]

    agent_path = agents_root / "tbd-intent-scorer.agent.md"
    result = await call_reasoning_llm(
        agent_path,
        workdir=workdir,
        user_prompt=(
            f"Score {len(payload_intents)} TBD locator intent(s) emitted by "
            f"Step 8 codegen. The intents will be passed to the Step 9 JIT "
            f"resolver against a live page's AOM. Be conservative on FAIL — "
            f"WARN is the right call when in doubt. Return exactly one entry "
            f"per input intent, in the same order."
        ),
        inputs={"intents.json": json.dumps({"intents": payload_intents}, indent=2)},
        output_schema=_INTENT_QUALITY_SCHEMA,
        timeout_s=120,
        max_tokens=4000,
        step=8,
    )

    if not result.success or not result.final_text.strip():
        log.warning(
            "step08.phase_d.scorer_failed",
            error=result.error,
            count=len(payload_intents),
        )
        # Scorer failure is a Phase-D infrastructure problem, not an intent
        # quality problem. Surface as a warning, don't block the step.
        partial = {
            "scorer_error": result.error or "no output",
            "results": [],
            "summary": {"pass": 0, "warn": 0, "fail": 0,
                        "total": len(payload_intents)},
        }
        return True, partial, [], None

    try:
        scored: dict[str, Any] = json.loads(result.final_text)
    except json.JSONDecodeError as e:
        log.warning("step08.phase_d.unparseable", error=str(e))
        return True, {"scorer_error": f"unparseable JSON: {e}",
                      "results": []}, [], None

    raw_results = scored.get("results") or []
    if len(raw_results) != len(intents):
        log.warning(
            "step08.phase_d.result_count_mismatch",
            expected=len(intents),
            got=len(raw_results),
        )

    # Splice scanner anchors into the scorer output so downstream consumers
    # (review gate, editor agent) can find each call-site without re-scanning.
    enriched: list[dict[str, Any]] = []
    for idx, intent_obj in enumerate(intents):
        scored_entry = raw_results[idx] if idx < len(raw_results) else {
            "intent": intent_obj.intent, "score": "WARN",
            "rationale": "scorer omitted this intent — defaulted to WARN",
        }
        enriched.append({
            "file": str(intent_obj.file).replace("\\", "/"),
            "line": intent_obj.line,
            "constant_name": intent_obj.constant_name,
            "intent": intent_obj.intent,
            "language": intent_obj.language,
            "score": scored_entry.get("score", "WARN"),
            "rationale": scored_entry.get("rationale", ""),
            "code_context": _extract_code_context(
                sut_root, intent_obj.file, intent_obj.line,
            ),
        })

    pass_n = sum(1 for e in enriched if e["score"] == "PASS")
    warn_n = sum(1 for e in enriched if e["score"] == "WARN")
    fail_n = sum(1 for e in enriched if e["score"] == "FAIL")
    summary = {
        "results": enriched,
        "summary": {"pass": pass_n, "warn": warn_n, "fail": fail_n,
                    "total": len(enriched)},
    }

    log.info(
        "step08.phase_d.scored",
        pass_n=pass_n, warn_n=warn_n, fail_n=fail_n, total=len(enriched),
    )

    fail_as_warn = os.environ.get("QTEA_INTENT_FAIL_AS_WARN") == "1"
    if fail_n > 0 and not fail_as_warn:
        # Step fails. Surface WARN+FAIL entries so a manual --from-step 8
        # restart with the env var set can still show them in the review gate.
        warnings_list = [e for e in enriched if e["score"] in ("WARN", "FAIL")]
        return (
            False, summary, warnings_list,
            f"intent quality gate: {fail_n} FAIL intent(s)",
        )

    if fail_as_warn and fail_n > 0:
        log.warning(
            "step08.phase_d.fail_downgraded",
            fail_n=fail_n,
            reason="QTEA_INTENT_FAIL_AS_WARN=1",
        )

    warnings_list = [e for e in enriched if e["score"] in ("WARN", "FAIL")]
    return True, summary, warnings_list, None


async def _auto_fix_intents(
    flagged: list[dict],
    sut_root: Path,
    workdir: Path,
    agents_root: Path,
) -> tuple[int, list[str]]:
    """Attempt to rewrite WARN/FAIL intents via the tbd-intent-editor agent.

    Returns ``(rewritten_count, errors)``.  Single attempt — no retry loop.
    """
    from qtea.review_gate import _replace_intent_at_line

    agent_path = agents_root / "tbd-intent-editor.agent.md"
    result = await call_reasoning_llm(
        agent_path,
        workdir=workdir,
        user_prompt=(
            "Improve each flagged intent based on its rationale. For FAIL "
            "intents: replace literal selectors (CSS, XPath, IDs) with "
            "role + visible label descriptions. For WARN intents: add "
            "specificity — include the UI region or disambiguating context "
            "(e.g. 'submit' → 'submit order button in checkout form'). Use "
            "the constant_name as a hint for the element's purpose when the "
            "intent is too vague."
        ),
        inputs={
            "flagged-intents.json": json.dumps(
                {"intents": flagged}, indent=2, ensure_ascii=False,
            ),
        },
        output_schema={
            "type": "object",
            "required": ["intents"],
            "additionalProperties": False,
            "properties": {
                "intents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["intent"],
                        "additionalProperties": True,
                        "properties": {
                            "intent": {"type": "string", "maxLength": 120},
                        },
                    },
                },
            },
        },
        step=8,
        timeout_s=60,
        max_tokens=2000 + 200 * len(flagged),
    )

    if not result.success or not result.final_text.strip():
        log.warning(
            "step08.phase_d.autofix_agent_failed", error=result.error,
        )
        return 0, [result.error or "agent produced no output"]

    try:
        updated = json.loads(result.final_text)
    except json.JSONDecodeError as e:
        log.warning("step08.phase_d.autofix_unparseable", error=str(e))
        return 0, [f"unparseable response: {e}"]

    new_intents = updated.get("intents") or []
    if len(new_intents) != len(flagged):
        log.warning(
            "step08.phase_d.autofix_count_mismatch",
            expected=len(flagged), got=len(new_intents),
        )
        return 0, [f"count mismatch: expected {len(flagged)}, got {len(new_intents)}"]

    rewritten = 0
    errors: list[str] = []
    for old, new in zip(flagged, new_intents, strict=False):
        old_intent = old.get("intent", "")
        new_intent = (new.get("intent") or "").strip()
        if not new_intent or new_intent == old_intent:
            continue
        rel = old.get("file", "")
        line_no = old.get("line", 0)
        abs_path = sut_root / rel
        if not abs_path.is_file():
            errors.append(f"{rel} (not found)")
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"{rel} (read: {e})")
            continue
        new_text, ok = _replace_intent_at_line(
            text, line_no, old_intent, new_intent,
        )
        if not ok:
            errors.append(f"{rel}:{line_no} (intent not found at line)")
            continue
        try:
            abs_path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            errors.append(f"{rel} (write: {e})")
            continue
        rewritten += 1
        log.info(
            "step08.phase_d.autofix_rewritten",
            file=rel, line=line_no,
            old=old_intent[:60], new=new_intent[:60],
        )

    return rewritten, errors


async def _run_phase_b65_parse_check(
    *,
    sut_root: Path,
    qtea_files: set[Path],
    agents_root: Path,
    workdir: Path,
    timeout_s: int | None,
) -> ParseCheckResult:
    """Phase B.6.5 — language-native parse gate.

    Runs BEFORE Phase B.6 (type-check) because a file that doesn't tokenise
    cannot be type-checked. Uses ``ast.parse`` for Python (always available)
    and shells to ``tsc`` / ``node --check`` / ``javac`` for other languages,
    with a regex smoke fallback when no native tool is on PATH.

    On parse errors, invokes ``codegen-violation-fixer`` ONCE with
    ``rule=parse-error`` and re-runs the check. Returns a
    ``ParseCheckResult`` with ``autofix_attempted`` / ``post_fix_errors`` set
    so the caller can decide whether to fail the step.

    Honors opt-outs ``QTEA_SKIP_PARSE_CHECK=1`` and ``QTEA_NO_PARSE_CHECK=1``.

    Motivating incident: run 20260701-114656-9394eb (`# Stack: typescript+playwright`
    header emitted into a `.spec.ts` file — Playwright's TS parser refused it,
    zero tests ran, both retry attempts hit the same broken file).
    """
    if os.environ.get("QTEA_SKIP_PARSE_CHECK") == "1":
        log.info("step08.phase_b65.skipped", reason="QTEA_SKIP_PARSE_CHECK=1")
        return ParseCheckResult(
            ran=False, skipped_reason="env_skip", duration_s=0.0,
            files_checked=0, in_scope_errors=0,
        )
    if os.environ.get("QTEA_NO_PARSE_CHECK") == "1":
        log.info("step08.phase_b65.skipped", reason="QTEA_NO_PARSE_CHECK=1")
        return ParseCheckResult(
            ran=False, skipped_reason="flag_skip", duration_s=0.0,
            files_checked=0, in_scope_errors=0,
        )

    result = await asyncio.to_thread(
        run_parse_check, sut_root, qtea_files=qtea_files,
    )

    if result.in_scope_errors == 0:
        log.info(
            "step08.phase_b65.clean",
            files_checked=result.files_checked,
            degraded_languages=result.degraded_languages,
            duration_s=round(result.duration_s, 2),
        )
        return result

    # Parse errors present — one autofix attempt via the shared
    # codegen-violation-fixer agent. The `parse-error` rule was added
    # to `codegen-violation-fixer.agent.md` §"Violation Fix Workflow";
    # the agent knows to rewrite `# Stack:` → `// Stack:` for TS/JS/Java
    # and to fix leaked-fence / prose-preamble artefacts.
    log.info(
        "step08.phase_b65.autofix",
        in_scope=result.in_scope_errors,
        degraded_languages=result.degraded_languages,
    )
    fix_agent = agents_root / "codegen-violation-fixer.agent.md"
    summary = parse_check_format_for_fixer(result)
    await run_agent(
        fix_agent,
        workdir=workdir,
        inputs={},
        user_prompt=(
            f"The language-native parse gate found "
            f"{result.in_scope_errors} parse error(s) in your generated "
            f"test code:\n\n```\n{summary}\n```\n\n"
            f"Each row is rule `parse-error`. Read the file, identify the "
            f"token the parser refused, and rewrite ONLY the offending "
            f"tokens (not the whole file). Most common cause: a Python-style "
            f"`# Stack:` comment on line 1 of a `.ts` / `.js` / `.java` "
            f"file — rewrite to `// Stack:`. See "
            f"`codegen-violation-fixer.agent.md` §\"Violation Fix Workflow\" "
            f"row `parse-error` for the workflow and the prohibition on "
            f"`@ts-nocheck` / equivalent escape hatches."
        ),
        extra_paths=[package_resource_root() / "skills" / "webapp-testing"],
        add_dirs=[sut_root],
        timeout_s=min(timeout_s or 1800, 300),
        step=8,
        max_turns=AUTOFIX_MAX_TURNS,
    )

    # Re-run the check. Single autofix pass — persisting errors escalate to
    # the caller's fail-step branch, mirroring Phase B.6's philosophy.
    post = await asyncio.to_thread(
        run_parse_check, sut_root, qtea_files=qtea_files,
    )
    result.autofix_attempted = True
    result.post_fix_errors = post.in_scope_errors
    result.violations = post.violations
    result.file_results = post.file_results
    result.degraded_languages = post.degraded_languages
    result.missing_tools = post.missing_tools
    result.duration_s = result.duration_s + post.duration_s

    log.info(
        "step08.phase_b65.postfix",
        in_scope=result.post_fix_errors,
        degraded_languages=result.degraded_languages,
    )
    return result


async def _run_phase_b6(
    *,
    sut_root: Path,
    framework: str,
    qteaouched: set[Path],
    agents_root: Path,
    workdir: Path,
    timeout_s: int | None,
) -> StaticCheckResult:
    """Phase B.6 — native static-check gate.

    Runs the SUT stack's native type-checker on the qteaouched files. On
    in-scope errors, invokes ``codegen-violation-fixer`` ONCE and re-runs the
    checker. Returns the final ``StaticCheckResult`` with ``autofix_attempted``
    and ``post_fix_errors`` set so the caller can decide whether to fail
    Step 8.

    Honors two opt-outs (matching the QTEA_SKIP_INTENT_SCORE precedent at
    Phase D): QTEA_SKIP_STATIC_CHECK=1 and QTEA_NO_STATIC_CHECK=1
    (latter set by the --no-static-check CLI flag in cli.py). When skipped,
    returns a result row with ran=False so the artifact still records WHY
    the gate didn't run.
    """
    if os.environ.get("QTEA_SKIP_STATIC_CHECK") == "1":
        log.info("step08.phase_b6.skipped", reason="QTEA_SKIP_STATIC_CHECK=1")
        return StaticCheckResult(
            tool=None, stack=framework, ran=False,
            skipped_reason="env_skip",
            duration_s=0.0, exit_code=0,
            in_scope_errors=0, out_of_scope_errors=0,
            autofix_attempted=False, post_fix_errors=0,
        )
    if os.environ.get("QTEA_NO_STATIC_CHECK") == "1":
        log.info("step08.phase_b6.skipped", reason="--no-static-check")
        return StaticCheckResult(
            tool=None, stack=framework, ran=False,
            skipped_reason="flag_skip",
            duration_s=0.0, exit_code=0,
            in_scope_errors=0, out_of_scope_errors=0,
            autofix_attempted=False, post_fix_errors=0,
        )

    check_timeout = int(os.environ.get("QTEA_STATIC_CHECK_TIMEOUT_S", "120"))
    result = await asyncio.to_thread(
        run_static_check,
        sut_root,
        framework=framework,
        qteaouched=qteaouched,
        timeout_s=check_timeout,
    )

    if not result.ran:
        log.info(
            "step08.phase_b6.no_run",
            reason=result.skipped_reason, framework=framework,
        )
        return result

    if result.in_scope_errors == 0:
        log.info(
            "step08.phase_b6.clean",
            tool=result.tool, framework=framework,
            duration_s=round(result.duration_s, 2),
            out_of_scope=result.out_of_scope_errors,
        )
        return result

    # In-scope errors present — one autofix attempt via the existing
    # codegen-violation-fixer agent (same agent that handles xpath/hard-wait
    # etc.; we just hand it a different violation summary).
    log.info(
        "step08.phase_b6.autofix",
        tool=result.tool, framework=framework,
        in_scope=result.in_scope_errors,
        out_of_scope=result.out_of_scope_errors,
    )
    fix_agent = agents_root / "codegen-violation-fixer.agent.md"
    summary = format_for_fixer(result)
    await run_agent(
        fix_agent,
        workdir=workdir,
        inputs={},
        user_prompt=(
            f"The native static-checker ({result.tool}) found "
            f"{result.in_scope_errors} type error(s) in your generated "
            f"test code:\n\n```\n{summary}\n```\n\n"
            f"Each row is rule `type-error`. Read the file, follow the "
            f"import to find the symbol's REAL definition, and rewrite "
            f"the call site to match. See `codegen-violation-fixer.agent.md`"
            f" §3 row `type-error` for the workflow and the prohibitions "
            f"on `# type: ignore` / `@ts-ignore` / `pytest.skip` (silencing "
            f"the checker is forbidden — the fix must be a real correction)."
        ),
        extra_paths=[package_resource_root() / "skills" / "webapp-testing"],
        add_dirs=[sut_root],
        timeout_s=min(timeout_s or 1800, 300),
        step=8,
        max_turns=AUTOFIX_MAX_TURNS,
    )

    # Re-run the checker. B6_MAX_AUTOPATCH_RETRIES = 1 — no further attempts.
    post = await asyncio.to_thread(
        run_static_check,
        sut_root,
        framework=framework,
        qteaouched=qteaouched,
        timeout_s=check_timeout,
    )
    result.autofix_attempted = True
    result.post_fix_errors = post.in_scope_errors
    # Replace violations with the post-fix set so the persisted artifact
    # reflects what remains, not what was originally found.
    result.violations = post.violations
    result.out_of_scope_errors = post.out_of_scope_errors
    result.exit_code = post.exit_code
    result.duration_s = result.duration_s + post.duration_s

    log.info(
        "step08.phase_b6.postfix",
        in_scope=result.post_fix_errors,
        out_of_scope=result.out_of_scope_errors,
    )
    return result


class CodegenStep(Step):
    number = 8
    name = "codegen"
    timeout_s = step_timeout(8)

    @staticmethod
    def _build_runtime_hint(
        framework: str, jit_files: list[Path], sut_root: Path,
    ) -> str:
        """One-paragraph hint telling the agent EXACTLY where the JIT runtime
        is (or that none was vendored). Eliminates the "find qtea_runtime"
        detour observed in run 20260610-114657-c9c7c3 step 7 attempt 1.

        Empty hint when the framework has no vendor entry — for those stacks
        the agent already knows from agent.md §3d to emit `TBD_LOCATOR`
        placeholders and there's nothing useful to say beyond that.
        """
        if not jit_files:
            if framework in _RUNTIME_VENDORS:
                return (
                    "\n\n--- JIT RUNTIME ---\nThe runtime vendor step ran but "
                    f"produced no files for framework `{framework}` (likely a "
                    "missing template). Emit `TBD_LOCATOR` placeholders with "
                    "`TBD_INTENT:` comments per agent.md §3d as a fallback. "
                    "Do NOT search for or attempt to create the runtime file "
                    "yourself."
                )
            return (
                "\n\n--- JIT RUNTIME ---\nStack `"
                f"{framework}` has no JIT runtime; emit `TBD_LOCATOR` "
                "placeholders with `TBD_INTENT:` comments per agent.md §3d. "
                "Do NOT search for `qtea_runtime` / `qtea-runtime` / "
                "`Tbd.java` — they are intentionally absent for this stack."
            )
        paths = "\n".join(f"  - `{p}`" for p in jit_files)
        return (
            "\n\n--- JIT RUNTIME (pre-vendored) ---\n"
            f"Framework `{framework}` runtime is ALREADY written to the SUT "
            f"at:\n{paths}\n"
            "**Do NOT search for it. Do NOT attempt to create it.** Import "
            "it directly per agent.md §3a-c:\n"
            "  - Python+pytest+PW → `from tests.qtea_runtime import tbd`\n"
            "  - TS/JS+PW → `import { tbd } from \"./qtea-runtime\"` "
            "(or relative path)\n"
            "  - Java+PW → `import com.qtea.runtime.Tbd;`\n"
            "The framework's setup hook (conftest.py / playwright.config / "
            "setupFiles / @Listeners) was also updated automatically; you "
            "do NOT need to register the runtime."
        )

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)
        sut_root = ctx.workspace.sut.resolve()

        # --- Pre-flight (fail in <1s) ---------------------------------------

        # A: SUT must be materialized. Pipeline materializes eagerly, but a
        # rogue `--from-step 7` on a workspace whose `<workspace>/sut/` was
        # manually deleted would otherwise burn the full step timeout.
        if not sut_root.exists() or not any(sut_root.iterdir()):
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"SUT not found at {sut_root}. Re-run the pipeline from "
                    f"step 1 (drop --from-step) to re-materialize the clone."
                ),
            )

        strategy_md = ctx.workspace.step_dir(4) / "test-design.md"
        sut_inv_json = ctx.workspace.step_dir(6) / "sut_inventory.json"
        plan_json = ctx.workspace.step_dir(7) / "code-modification-plan.json"

        # B: planning artifacts required by the agent.
        if not strategy_md.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"missing {strategy_md}; run step 4 first",
            )

        # C: step 6 must have run. Without sut_inventory.json the codegen
        # prompt has no active-module context for fallback byte-match
        # locator dedup. The plan is authoritative for placement, but the
        # inventory is still useful as a style-mimicry reference.
        if not sut_inv_json.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "step 8 requires sut_inventory.json from step 6. Run "
                    "step 6 first (e.g. drop --only-step 8, or use "
                    "--from-step 6)."
                ),
            )

        # C2: step 7 (test-automation-architect) must have run. The plan is the
        # authoritative placement contract — without it, this step has no
        # mapping from test cases to file paths / fixture decisions / TBD
        # intents and would have to re-derive everything from inventory +
        # strategy. That's exactly what Step 7 was inserted to eliminate.
        if not plan_json.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "step 8 requires code-modification-plan.json from step 7. "
                    "Run step 7 first (drop --only-step 8, or use "
                    "--from-step 7)."
                ),
            )

        research = _read_research(ctx)
        detected_stack = research.get("detected_stack")
        sut_env_keys = research.get("sut_env_keys") or []
        sut_inventory_dict = research.get("sut_inventory") or {}
        active_module_dict = _active_module_dict(sut_inventory_dict)

        # E: framework ↔ test-command consistency. Catches Step 6 misdetection
        # before we vendor a runtime template that won't load. Silent skip
        # when either side is unverifiable.
        test_command = (research.get("commands") or {}).get("test")
        command_head = _parse_test_command_head(test_command)
        mismatch_msg = _framework_mismatch_message(detected_stack, command_head)
        if mismatch_msg:
            log.error(
                "step08.framework_mismatch",
                detected_stack=detected_stack,
                command_head=command_head,
                test_command=test_command,
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=mismatch_msg,
            )

        # Load dev-locators (--dev-locators CLI flag / env / convention).
        # Used in Phase A2 to substitute matched intents with dev-supplied
        # selectors instead of emitting tbd() sentinels.
        dev_locators_opt = getattr(ctx.options, "dev_locators", None)
        dev_locators_map, dev_loc_path, dev_loc_warnings = load_dev_locators(
            cli_path=dev_locators_opt, sut_root=sut_root,
        )
        for w in dev_loc_warnings:
            log.warning("step08.dev_locators.warning", msg=w)
        if dev_locators_map:
            log.info(
                "step08.dev_locators.loaded",
                count=len(dev_locators_map),
                path=str(dev_loc_path),
            )

        # D: when an active module exists, its referenced page-objects /
        # locators / helpers / auth-flow files must actually live under
        # `<workspace>/sut/`. A non-empty inventory + zero reachable files
        # means the clone is incomplete (e.g. shallow-clone dropped a
        # submodule) — abort cleanly with the count + first-missing path.
        expected_inventory_files = _inventory_files(active_module_dict)
        if expected_inventory_files:
            missing = [p for p in expected_inventory_files
                       if not (sut_root / p).is_file()]
            if len(missing) == len(expected_inventory_files):
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[],
                    error=(
                        f"SUT inventory references {len(expected_inventory_files)} "
                        f"files; 0 of them found under {sut_root} (first "
                        f"missing: {missing[0]}). The clone may be incomplete "
                        f"or step 6 ran against a different SUT — re-run "
                        f"step 6."
                    ),
                )

        # --- Parse plan + load inputs for phased codegen --------------------
        try:
            plan_data = json.loads(plan_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return StepResult(
                success=False, status="failed", outputs=[],
                error=f"failed to parse code-modification-plan.json: {e}",
            )
        strategy_text = strategy_md.read_text(encoding="utf-8")

        agents_root = package_resource_root() / "agents"

        rules_path = agents_root / "codegen-rules.md"
        rules_content = ""
        if rules_path.is_file():
            rules_content = rules_path.read_text(encoding="utf-8")
            log.info("step08.codegen_rules_loaded", path=str(rules_path))


        env_hint = ""
        if sut_env_keys:
            joined = ", ".join(sut_env_keys)
            env_hint = (
                f" The SUT uses these environment variables: {joined}. "
                f"Reference them in generated tests via process.env.<NAME> "
                f"(or the framework equivalent such as os.environ). "
                f"Never hardcode their values."
            )

        # --- Pre-vendor the JIT runtime BEFORE the agent runs -------------
        #
        # Vendoring used to run AFTER the agent succeeded, which forced the
        # agent into a chicken-and-egg search loop: the prompt told it to
        # import `tests.qtea_runtime` / `./qtea-runtime` / `com.qtea.
        # runtime.Tbd`, but that file did not exist yet. Empirical fallout
        # from run 20260610-114657-c9c7c3 step 7 attempt 1: the agent spent
        # 80 turns burning the 1800s timeout with ZERO Writes, including 10
        # Greps for `def tbd|class.*Runtime|__QTEA_TBD__`, 18 Bash
        # `find`/`grep` calls, and one spawned subagent literally named
        # "Find qtea_runtime template". By line 931 it decided IT had
        # to create the runtime — work the pipeline would have overwritten
        # with the template anyway.
        #
        # Vendoring first means the agent reads the runtime once (Read by
        # absolute path) and gets to writing tests.
        #
        # `detected_stack` is None only when Step 6 hard-failed and the
        # operator forced through. In that case the SUT may have no test
        # files yet, so `resolve_framework` would scan an empty dir and
        # return "unknown" — we can't pick a vendor entry. Defer vendoring
        # to AFTER the agent runs (original behavior) in that edge case;
        # the agent will see no runtime and use the TBD_LOCATOR fallback
        # for this turn, which Step 8's heal flow can pick up.
        if detected_stack:
            pre_framework = resolve_framework(detected_stack, sut_root)
            jit_files_added: list[Path] = _vendor_runtime_for_framework(
                pre_framework, sut_root,
            )
            runtime_hint = self._build_runtime_hint(
                pre_framework, jit_files_added, sut_root,
            )
        else:
            jit_files_added = []
            runtime_hint = (
                "\n\n--- JIT RUNTIME ---\nNo stack was detected by Step 6, so "
                "no runtime is pre-vendored. Emit `TBD_LOCATOR` placeholders "
                "with `TBD_INTENT:` comments per agent.md §3d. Do NOT search "
                "for or attempt to create a qtea runtime file."
            )

        def _fixture_import_guidance(lang: str, fix_list: list) -> str:
            """Framework-specific fixture import instructions.

            pytest auto-discovers fixtures via conftest; Playwright Test (TS/JS)
            and Java require explicit imports from the file that defines them.
            """
            if lang in ("python",):
                return (
                    "FIXTURE IMPORT RULE: pytest auto-discovers fixtures via "
                    "conftest.py — reference them by name in test function "
                    "signatures. No explicit import needed.\n\n"
                )
            fixture_files = sorted(
                {f.get("file") for f in fix_list if f.get("file")},
            )
            if not fixture_files:
                return ""
            file_list = ", ".join(f"`{fp}`" for fp in fixture_files)
            return (
                f"FIXTURE IMPORT RULE: This SUT uses custom test fixtures "
                f"defined in {file_list}. You MUST import `test` (and "
                f"`expect` if needed) from that fixture file — NOT from "
                f"`@playwright/test`. The fixture file extends Playwright's "
                f"`test` object with custom fixtures via `test.extend`. "
                f"Importing from `@playwright/test` directly will cause "
                f"all custom fixtures to be `undefined` at runtime.\n"
                f"Example: `import {{ test, expect }} from "
                f"'<relative-path-to-fixture-file>';`\n\n"
            )

        # Reuse + folder integration: when an active module is known, tell the
        # agent which language to write in, which directories to land each
        # category of file in (tests vs production code), and which existing
        # page objects/helpers/fixtures it MUST extend rather than re-implement.
        # The agent writes ABSOLUTE paths under `<workspace>/sut/` (granted via
        # `add_dirs=[sut_root]`) — tests + fixtures + data into the SUT's own
        # test directory, page objects + locators into its src tree. The
        # `qtea_` filename prefix prevents collisions with the SUT's own files
        # and lets Step 8's runner + indexer pick up only our generated tests.
        isolated = bool(getattr(ctx.options, "isolated_tests", False))
        reuse_hint = ""
        if active_module_dict:
            am = active_module_dict
            layout = am.get("test_directory_layout") or {}
            src_layout = am.get("src_directory_layout") or {}
            base_dir = layout.get("base_dir") or "tests"
            default_target = layout.get("default_target") or base_dir
            language = am.get("language") or "unknown"
            page_objects = am.get("existing_page_objects") or []
            helpers = am.get("existing_helpers") or []
            fixtures = am.get("existing_fixtures") or []
            locator_classes = am.get("existing_locators") or []

            # The agent writes via absolute paths under the canonical SUT
            # clone (granted via `add_dirs=[sut_root]`). Build the module-rooted
            # absolute path for each per-category target so the prompt can
            # spell out exactly where each kind of file goes — no relative
            # `./tests/`-style guessing that depends on cwd.
            module_path = am.get("path") or "."
            module_root = sut_root if module_path == "." else (sut_root / module_path)

            po_lines = "\n".join(
                f"  - `{p.get('name')}` ({p.get('scope', 'generic')}) at "
                f"`{p.get('file')}` — methods: {', '.join((p.get('methods') or [])[:8])}"
                for p in page_objects[:20]
            ) or "  (none discovered)"
            fixture_lines = "\n".join(
                f"  - `{f.get('name')}` at `{f.get('file')}` "
                f"(scope={f.get('scope', 'function')})"
                for f in fixtures[:15]
            ) or "  (none discovered)"
            helper_lines = "\n".join(
                f"  - `{h.get('name')}` at `{h.get('file')}`"
                for h in helpers[:15]
            ) or "  (none discovered)"
            # Existing locator classes — the per-class constant list is what
            # prevents the agent from inventing byte-identical duplicates
            # (the LOCALE_SWITCHER / LANGUAGE_DROP_DOWN issue from the
            # 20260601-212148 run). Cap visible constants per class at 25
            # to keep the prompt bounded; the absolute path to the full file
            # lets the agent grep for the rest when it needs them.
            _MAX_VISIBLE_CONSTS_PER_CLASS = 25
            if locator_classes:
                lc_blocks: list[str] = []
                for lc in locator_classes[:10]:
                    consts = lc.get("constants") or []
                    visible = consts[:_MAX_VISIBLE_CONSTS_PER_CLASS]
                    const_lines = "\n".join(
                        f"      - `{c.get('name')} = {c.get('selector')!r}`"
                        for c in visible
                    )
                    hidden_remainder = max(
                        0,
                        len(consts) - len(visible)
                        + int(lc.get("truncated_count") or 0),
                    )
                    full_file_path = sut_root / (lc.get("file") or "")
                    tail = (
                        f"\n      - … {hidden_remainder} more (read "
                        f"`{full_file_path}` for the full list)"
                        if hidden_remainder > 0 else ""
                    )
                    lc_blocks.append(
                        f"  - **`{lc.get('class_name')}`** "
                        f"@ `{lc.get('file')}`:\n{const_lines}{tail}"
                    )
                locator_lines = "\n".join(lc_blocks)
            else:
                locator_lines = "  (none discovered)"
            subdir_lines = "\n".join(
                f"  - `{s.get('path')}` (kind={s.get('kind')})"
                for s in (layout.get("subdirs") or [])
            ) or "  (none)"

            # Per-category placement table — absolute paths anchored at the
            # SUT root. Falls back to test-folder if the src layout wasn't
            # detected (greenfield TS/JS, or unknown lang).
            pages_object_dir = src_layout.get("pages_object_dir") or f"{base_dir}/pages/object"
            pages_locators_dir = (
                src_layout.get("pages_locators_dir")
                or f"{base_dir}/pages/locators"
            )
            raw_helpers_dir = src_layout.get("helpers_dir")
            pkg_root = src_layout.get("package_root")
            if raw_helpers_dir and pkg_root and raw_helpers_dir.startswith(base_dir):
                helpers_dir = f"{pkg_root}/utils"
            elif raw_helpers_dir:
                helpers_dir = raw_helpers_dir
            else:
                helpers_dir = f"{base_dir}/helpers"
            fixtures_dir = f"{base_dir}/fixtures"  # fixtures always under tests/
            data_dir = f"{base_dir}/data"

            # `--isolated-tests` opts into a dedicated `qteaests/` subdir
            # for the test files (Step 8's runner mirrors this resolution).
            # Page objects + locators + helpers still go under the SUT's src
            # tree in both modes — they have no parallel "isolated" home and
            # the qtea_ prefix already prevents file collisions.
            tests_subdir = "qteaests" if isolated else default_target
            abs_tests = module_root / tests_subdir
            abs_data = module_root / (
                f"{tests_subdir}/data" if isolated else data_dir
            )
            abs_fixtures = module_root / (
                f"{tests_subdir}/fixtures" if isolated else fixtures_dir
            )
            abs_pages_object = module_root / pages_object_dir
            abs_pages_locators = module_root / pages_locators_dir
            abs_helpers = module_root / helpers_dir

            reuse_hint = (
                f"\n\n--- ACTIVE MODULE (from "
                f"`./sut_inventory.json[\"modules\"][active_module]`) ---\n"
                f"Name: `{am.get('name')}`  Path: `{am.get('path')}`  "
                f"Language: `{language}`  Package manager: "
                f"`{am.get('package_manager') or 'unknown'}`\n\n"
                f"SUT clone (read + write directly here — you have `add_dirs` "
                f"access; all file paths in the lists below are relative to "
                f"this root): `{sut_root}`\n\n"
                f"Test directory layout: `{base_dir}` "
                f"(convention: {layout.get('convention', 'unknown')}, "
                f"default target: `{default_target}`)\n"
                f"Subdirs:\n{subdir_lines}\n\n"
                f"Src directory layout "
                f"(source: {src_layout.get('convention_source', 'unknown')}):\n"
                f"  - package_root: `{src_layout.get('package_root') or '(none)'}`\n"
                f"  - pages_object_dir: `{pages_object_dir}`\n"
                f"  - pages_locators_dir: `{pages_locators_dir}`\n"
                f"  - helpers_dir: `{helpers_dir}`\n\n"
                f"EXISTING PAGE OBJECTS (reuse these — do NOT redefine):\n"
                f"{po_lines}\n\n"
                f"{_fixture_import_guidance(language, fixtures)}"
                f"EXISTING FIXTURES (do NOT redefine these in your test "
                f"file — use them directly):\n{fixture_lines}\n\n"
                f"EXISTING HELPERS:\n{helper_lines}\n\n"
                f"EXISTING LOCATORS (reuse these constants — do NOT redefine "
                f"byte-identical selectors in a new locator class):\n"
                f"{locator_lines}\n\n"
                f"--- REUSE RULES (non-negotiable) ---\n"
                f"1. Before writing any page-object class, helper, fixture, or "
                f"**locator constant**, check the lists above. If an existing "
                f"class/method/constant covers the behavior you need, **import "
                f"and extend it** — do not redefine. A locator constant whose "
                f"selector string matches an existing one byte-for-byte is "
                f"ALWAYS a reuse violation: import the existing constant "
                f"(e.g. `from <pkg>.pages.locators.chat_page_locators import "
                f"ChatPageLocators`) instead of redeclaring it.\n"
                f"2. If you must write new code, add a one-line docstring "
                f"justification (e.g. `\"\"\"New: SUT has no fixture for locale "
                f"switching.\"\"\"`).\n"
                f"3. **File placement is per-category** — write each kind of "
                f"file at its ABSOLUTE path under the SUT clone. The pipeline "
                f"does NOT copy your output anywhere afterwards — the SUT IS "
                f"the deliverable, on a qtea-owned git branch. Specifically:\n"
                f"   - Test files → `{abs_tests}/qtea_<feature>_test.<ext>`\n"
                f"   - Test data → `{abs_data}/qtea_<feature>_data.<ext>`\n"
                f"   - Fixtures → `{abs_fixtures}/qtea_<feature>_fixture.<ext>`\n"
                f"   - Page objects → `{abs_pages_object}/qtea_<feature>_page.<ext>`\n"
                f"   - Locators → `{abs_pages_locators}/qtea_<feature>_locators.<ext>`\n"
                f"   - Helpers → `{abs_helpers}/qtea_<feature>_helper.<ext>`\n"
                f"   Prefix EVERY generated filename with `qtea_` so collisions "
                f"with the SUT's own files stay at zero. Use the Write tool with "
                f"those absolute paths directly — do NOT write into "
                f"`./tests/` or `./src/` relative to your cwd, since your cwd "
                f"is the qtea step workdir, NOT the SUT.\n"
                f"4. Match the active module's language: `{language}`. Never "
                f"emit Python tests for a TypeScript module or vice versa.\n"
                f"5. **Fixture reuse discipline:**\n"
                f"   - `source: reuse` in the code-modification plan means the "
                f"fixture already exists and is auto-discovered by the "
                f"framework. Use it by name — do NOT redefine, wrap, copy, "
                f"or shadow it in the test file.\n"
                f"   - `source: create` means no suitable fixture exists. "
                f"Create it in the designated fixtures directory following "
                f"the SUT's conventions.\n"
                f"   - This applies to all frameworks with fixture/setup "
                f"injection (pytest fixtures, JUnit @Before/@BeforeEach, "
                f"TestNG @BeforeMethod, Mocha before/beforeEach, Jest "
                f"beforeAll/beforeEach, etc.).\n"
            )

        # --- Phased codegen orchestration -----------------------------------
        #
        # Instead of one monolithic run_agent call (which grew context to
        # 60-80K+ tokens across 9+ turns), split into focused phases:
        #   A: Infrastructure scaffold (POM extension, TBD locators, fixtures)
        #   B: Test file generation (one reasoning call per test_file_target)
        #   C: Quality gate (unchanged — indexer + violation fix)
        #
        # Each call_reasoning_llm call is a single API round-trip with ~5-10K
        # tokens of bounded context. No multi-turn growth.
        language = (active_module_dict or {}).get("language")

        # Phase A1: deduplicate infrastructure tasks across TCs
        pom_tasks = _build_pom_tasks(plan_data, sut_root, sut_inventory_dict)
        locator_tasks = _build_locator_tasks(plan_data, sut_inventory_dict)
        fixture_tasks = _build_fixture_tasks(plan_data)
        helper_tasks = _build_helper_tasks(plan_data)

        total_methods = sum(len(t.missing_methods) for t in pom_tasks.values())
        log.info(
            "step08.phased.plan_parsed",
            pom_count=len(pom_tasks),
            missing_methods=total_methods,
            tbd_locators=len(locator_tasks),
            fixture_creates=len(fixture_tasks),
            helper_creates=len(helper_tasks),
            test_cases=len(plan_data.get("test_cases") or []),
        )

        # Phase A2: TBD locators (pure Python — no LLM call).
        # Runs BEFORE POM extension so the locator file already contains
        # TBD constants when the POM extender reads it — methods then
        # reference self.locators.<CONSTANT> instead of inline tbd() calls.
        # Run-scoped across attempts: ctx.extras is the same object for every
        # attempt of this step, so a deferral logged in attempt 1 stays
        # suppressed in attempt 2's re-run of this same phase sequence.
        deferral_seen = ctx.extras.setdefault("s08_tbd_deferral_logged", set())
        tbd_written = _write_tbd_locators(
            locator_tasks, sut_root, language,
            dev_locators=dev_locators_map,
            deferral_seen=deferral_seen,
        )
        if tbd_written:
            log.info("step08.tbd_locators.total", count=tbd_written)

        # Phase A3: extend POMs with missing methods
        if total_methods > 0:
            pom_results = await _extend_poms(
                pom_tasks, sut_root, wd, agents_root, step=8,
                rules_content=rules_content,
                ctx=ctx,
                locator_tasks=locator_tasks,
            )
            pom_failures = [fp for fp, ok in pom_results if not ok]
            if pom_failures:
                log.warning(
                    "step08.pom_extend.partial_failure",
                    failed=pom_failures,
                )

            # Phase A3.25: structural TBD re-assert.
            #
            # The pom-extender contract has it return the COMPLETE updated
            # file (full replace, no merge — see `_extend_one`'s
            # `abs_path.write_text`). Its persona frames "the POM" and its
            # "companion locator class" as separate deliverables, with no
            # instruction to preserve non-method top-level declarations —
            # so when a POM and its locator object share one physical file
            # (e.g. `TrialPage.ts` containing both `class TrialPage` and
            # `export const TrialPageSelectors`), the agent can reproduce
            # the class and silently drop the locator object, clobbering
            # the sentinels this same writer inserted in Phase A2. That is
            # exactly what happened on run 20260708-121117-99f5ed. Re-run
            # the identical structural writer here: it is idempotent
            # (`_locator_constant_defined` is a real definition check, not
            # a substring match) so it only restores what the agent
            # dropped and never duplicates what's already valid.
            tbd_reasserted = _write_tbd_locators(
                locator_tasks, sut_root, language,
                dev_locators=dev_locators_map,
                deferral_seen=deferral_seen,
            )
            if tbd_reasserted:
                log.warning(
                    "step08.tbd_locators_reasserted",
                    count=tbd_reasserted,
                    hint=(
                        "pom-extender's write-back dropped pre-written "
                        "tbd() sentinel(s); restored structurally"
                    ),
                )

        # Phase A3.5: TBD-compliance gate.
        #
        # Verify the pom-extender emitted tbd() sentinels (or dev-locator
        # matches) for every create_tbd locator the plan asked for.
        # Hard-fails when the extender invented raw selector strings —
        # the specific failure mode from run 20260708-121117-99f5ed where
        # the coherence trap (mechanical pre-write gap + unconditional
        # "sentinels exist" prompt) forced the LLM to hardcode XPath. No
        # retry: identical inputs would produce the same output.
        tbd_violations = _verify_tbd_compliance(
            locator_tasks, sut_root, dev_locators=dev_locators_map,
        )
        if tbd_violations:
            log.error(
                "step08.tbd_compliance_failed",
                count=len(tbd_violations),
            )
            (out_dir / "tbd-compliance-violations.log").write_text(
                "\n".join(tbd_violations), encoding="utf-8",
            )
            # The two `_verify_tbd_compliance` violation shapes are
            # opposite failure modes ("not found" = a sentinel was never
            # (re-)defined; "contains raw selector" = a real value was
            # hardcoded in its place) — collapsing both into "invented"
            # misdirected the run-20260708-121117-99f5ed RCA/fix chain,
            # whose actual failure was 3/3 "not found". Report each
            # bucket by its own name.
            missing = [v for v in tbd_violations if " not found in " in v]
            invented = [v for v in tbd_violations if v not in missing]
            if invented and missing:
                headline = (
                    f"pom-extender left {len(missing)} tbd() sentinel(s) "
                    f"undefined and invented {len(invented)} raw "
                    f"selector(s) instead"
                )
            elif invented:
                headline = (
                    f"pom-extender invented {len(invented)} "
                    f"selector(s) instead of using tbd() sentinels"
                )
            else:
                headline = (
                    f"pom-extender left {len(missing)} pre-written "
                    f"tbd() sentinel(s) undefined"
                )
            return StepResult(
                success=False, status="failed",
                outputs=[out_dir / "tbd-compliance-violations.log"],
                error=headline,
                notes="\n".join(tbd_violations[:5])[:500],
            )

        # Phase A3.5 (body-verifier half) — RCA-C — used to run HERE for
        # TS/JS, but Phase B2 (test-file generation) hasn't executed yet at
        # this point, so the companion test file the criterion is allowed
        # to be satisfied from (Fix 5: assertion may live in the POM OR the
        # test) never exists at check time. That made any TS/JS
        # `kind=assertion` criterion requiring exact_text/count/attribute/
        # value_equals structurally unsatisfiable — the only way to satisfy
        # it was an assertion inside the POM, which the very next gate
        # (pom-assertion, below) then hard-fails. Moved to run once EVERY
        # stack's test files exist — see the unified body-verify gate after
        # Phase B2 (Phase B2.5) further down, which now covers
        # python/typescript/javascript/java identically.

        # Phase A3.5 (pom-assertion structural gate) — run the same regex
        # battery as `test_indexer._scan_pom_assertions` but at the earliest
        # possible point, so a Phase B.5 abort can't leave broken POMs on
        # disk unchecked (as happened on run 20260708-121117-99f5ed where
        # `expect(marketingCheckbox).toBeAttached(...)` shipped inside
        # `verifyMarketingConsentPositionAndLabel`). Not autopatchable —
        # identical inputs would produce identical output; the fix is
        # prompt/persona review. Covers Java too (Playwright-Java
        # `assertThat(...)` / JUnit `Assertions.assertEquals(...)` inside a
        # POM method is the same anti-pattern). Python isn't wired into
        # this EARLY half yet — see `find_pom_assertion_violations`'s
        # docstring.
        if (language or "").lower() in {"typescript", "javascript", "java"}:
            from qtea.codegen_pom_hygiene import find_pom_assertion_violations
            agent_authored_by_pom: dict[str, set[str]] = {}
            for _pt in pom_tasks.values():
                names = {
                    _mm.get("name") for _mm in (_pt.missing_methods or [])
                    if isinstance(_mm, dict) and _mm.get("name")
                }
                if names:
                    agent_authored_by_pom[_pt.pom_file] = names
            assert_violations: list[str] = []
            for pom_task in pom_tasks.values():
                names = agent_authored_by_pom.get(pom_task.pom_file, set())
                if not names:
                    continue
                pom_abs = sut_root / pom_task.pom_file
                if not pom_abs.is_file():
                    continue
                for v in find_pom_assertion_violations(
                    pom_abs, pom_task.pom_name, names, language=language,
                ):
                    assert_violations.append(v.format())
            if assert_violations:
                log.error(
                    "step08.pom_assertion_gate_failed",
                    count=len(assert_violations),
                )
                (out_dir / "pom-assertion-violations.log").write_text(
                    "\n".join(assert_violations), encoding="utf-8",
                )
                return StepResult(
                    success=False, status="failed",
                    outputs=[out_dir / "pom-assertion-violations.log"],
                    error=(
                        f"pom-extender wrote assertions inside "
                        f"{len(assert_violations)} POM method(s); "
                        f"assertions belong in tests only"
                    ),
                    notes="\n".join(assert_violations[:5])[:500],
                )

        # Phase A3.6: purpose-fidelity judge — shadow by default
        # (QTEA_PURPOSE_JUDGE=shadow), logs verdicts on whether each
        # generated POM method's body actually implements its own
        # `purpose` for the blind spot the deterministic body-verifier
        # (Phase B2.5, below) cannot cover: kind=action/query methods
        # (no acceptance_criteria) and kind=assertion methods with a
        # check="custom" criterion. Runs before Phase A4/A5/B2 spend
        # further LLM calls building on top of a possibly-broken POM.
        # QTEA_PURPOSE_JUDGE=block additionally enforces one auto-repair
        # retry then a hard-fail; QTEA_PURPOSE_JUDGE=off skips entirely.
        try:
            from qtea.purpose_judge import (
                _mode as _pf_mode_fn,
                judge_and_repair_blocking,
                judge_purpose_fidelity,
            )
            pf_mode = _pf_mode_fn()
            if pf_mode != "off":
                pf_result = await judge_purpose_fidelity(
                    pom_tasks=pom_tasks, sut_root=sut_root, out_dir=out_dir,
                    agents_root=agents_root, workdir=wd, language=language,
                    locator_tasks=locator_tasks,
                )
                if pf_mode == "block" and pf_result and pf_result["summary"]["flagged"]:
                    still_flagged = await judge_and_repair_blocking(
                        pf_result, pom_tasks=pom_tasks, sut_root=sut_root,
                        out_dir=out_dir, wd=wd, agents_root=agents_root,
                        step=8, rules_content=rules_content, ctx=ctx,
                        language=language, locator_tasks=locator_tasks,
                    )
                    if still_flagged:
                        (out_dir / "purpose-fidelity-violations.log").write_text(
                            "\n".join(still_flagged), encoding="utf-8",
                        )
                        return StepResult(
                            success=False, status="failed",
                            outputs=[out_dir / "purpose-fidelity-violations.log"],
                            error=(
                                f"{len(still_flagged)} method(s) failed "
                                f"purpose-fidelity review after auto-repair"
                            ),
                            notes="\n".join(still_flagged[:5])[:500],
                        )
        except Exception as _pf_exc:
            log.warning("step08.purpose_judge.wiring_error", error=str(_pf_exc))

        # Phase A4: create fixtures
        if fixture_tasks:
            await _create_fixtures(
                fixture_tasks, sut_root, wd, agents_root,
                active_module=active_module_dict, step=8,
                rules_content=rules_content,
            )

        # Phase A5: create helpers
        if helper_tasks:
            await _create_helpers(
                helper_tasks, sut_root, wd, agents_root,
                active_module=active_module_dict, step=8,
                rules_content=rules_content,
            )

        # Phase B1: build imports manifest
        manifest = _build_imports_manifest(
            plan_data, pom_tasks, locator_tasks, fixture_tasks,
            helper_tasks, sut_root, active_module=active_module_dict,
        )

        # Step 9->8 back-edge (Gap C): when Step 9 rejected the previous
        # codegen output as a structural defect (e.g. zero tests collected —
        # missing qtea markers / filename prefix, or an unresolved import to a
        # module codegen should have emitted), it re-queues Step 8 with a
        # reason on ctx.extras. Prepend that reason so this regeneration fixes
        # the specific gap rather than blindly reproducing it.
        _defect_feedback = ctx.extras.pop("step8_defect_feedback", None)
        if _defect_feedback:
            reuse_hint = (
                f"\n\n--- REGENERATION FEEDBACK (Step 9 rejected the previous "
                f"codegen output — fix THIS specifically) ---\n"
                f"{_defect_feedback}\n"
                f"Every generated test function MUST carry a "
                f"`@pytest.mark.qtea_<phase>` marker (pytest) or its file MUST "
                f"use the `qtea_` filename prefix (Playwright Test), or Step 9's "
                f"marker filter collects zero tests. Emit every import target "
                f"the tests reference.\n"
            ) + reuse_hint
            log.info(
                "step08.regen_with_defect_feedback",
                feedback=str(_defect_feedback)[:200],
            )

        # Phase B2: generate test files
        test_results = await _generate_test_files(
            plan_data, strategy_text, manifest, sut_root, wd, agents_root,
            reuse_hint=reuse_hint,
            runtime_hint=runtime_hint,
            env_hint=env_hint,
            step=8,
            rules_content=rules_content,
        )

        if not test_results or not any(ok for _, ok in test_results):
            failed_targets = [t for t, ok in test_results if not ok] if test_results else []
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"all test file generation calls failed "
                    f"(targets: {failed_targets or 'none'})"
                ),
            )

        # The agent now writes ABSOLUTE paths under `<workspace>/sut/` via
        # `add_dirs=[sut_root]`. Detect what it produced by walking the SUT
        # for the `qtea_` filename convention (enforced in the prompt and
        # by the indexer's qtea_ globs in `test_indexer._TEST_FILE_GLOBS`).
        produced_in_sut: list[Path] = sorted(
            p for p in sut_root.rglob("qtea_*")
            if p.is_file() and ".git" not in p.parts
        )
        # Capitalised Java pattern (`Qtea*Test.java`) — search separately
        # since the lowercase glob above misses it.
        produced_in_sut.extend(sorted(
            p for p in sut_root.rglob("Qtea*")
            if p.is_file() and ".git" not in p.parts and p not in produced_in_sut
        ))
        # JIT runtime files were vendored BEFORE the agent ran (see the
        # pre-vendoring block above). They live under qtea-prefixed names
        # (`qtea_runtime.py`, `qtea-runtime.js`, `QteaT.java`, ...)
        # and will show up in `produced_in_sut` via the rglob above — but
        # they are NOT agent output, so they don't count toward the
        # "did the agent write anything?" gate.
        jit_resolved = {p.resolve() for p in jit_files_added}
        agent_produced = [p for p in produced_in_sut if p.resolve() not in jit_resolved]
        if not agent_produced:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"codegen did not produce any qtea_*-prefixed files under {sut_root}",
            )

        # --- Phase B.5: static reconciliation + auto-patch ---------------
        #
        # Walk every generated test file, extract POM method call sites,
        # verify the called methods exist on the post-extension POMs with
        # compatible arity. On mismatch, auto-patch by re-invoking the POM
        # extender once (B5_MAX_AUTOPATCH_RETRIES); if mismatches persist
        # after the retry, hard-fail before Step 9 burns time on
        # AttributeErrors. See plan: Phase B.5.
        language = (
            (active_module_dict or {}).get("language")
            or plan_data.get("language")
            or "python"
        ).lower()

        # Phase B2.5 (body-verifier) — RCA-C, run HERE (post-generation) for
        # EVERY stack because assertions may live in the generated TEST file
        # (Fix 5), which does not exist yet at Phase A3.5. For every
        # kind=assertion POM method, confirm the POM getter + its generated
        # test encode the acceptance-criteria oracle (exact text/count/attr/
        # value, visible, bounding-box) and reject the count-drift /
        # tautology anti-patterns from run 20260708-121117-99f5ed. Python was
        # the first stack wired here (findings 4 & 5); TS/JS previously ran
        # this same check too early (Phase A3.5, see the removed block
        # above) and Java had no semantic assertion gate at all — both are
        # now wired into this single post-B2 call so all four supported
        # languages get an identical contract.
        _BODY_VERIFY_TEST_GLOBS: dict[str, tuple[str, ...]] = {
            "python": ("qtea_*.py",),
            "pytest": ("qtea_*.py",),
            "playwright-py": ("qtea_*.py",),
            "selenium-py": ("qtea_*.py",),
            "typescript": ("qtea_*.spec.ts", "qtea_*.test.ts", "qtea_*.spec.js", "qtea_*.test.js"),
            "javascript": ("qtea_*.spec.js", "qtea_*.test.js", "qtea_*.spec.ts", "qtea_*.test.ts"),
            "java": ("Qtea*Test.java", "Qtea*Tests.java"),
        }
        _body_verify_globs = _BODY_VERIFY_TEST_GLOBS.get(language)
        if _body_verify_globs:
            from qtea.codegen_body_verify import verify_method_bodies

            bv_test_files: list[Path] = sorted({
                p
                for pattern in _body_verify_globs
                for p in sut_root.rglob(pattern)
                if p.is_file() and ".git" not in p.parts
            })

            # Pre-verify parse-check — body-verify below does an AST/regex
            # scan of method *bodies*; that scan is unreliable (and its
            # violations are noise) if the file doesn't even parse/compile.
            # Phase B.6.5 already exists (below, ~line 4853) but runs AFTER
            # this block, which returns early on body_violations — so a
            # file with a compile-fatal defect (e.g. a Python-idiom leak
            # into a `.ts` POM) never reached it and body-verify's
            # assertion-coverage failure became the only, misleading,
            # signal. Run the same gate here first, scoped to just the
            # files body-verify is about to inspect. Motivating incident:
            # run 20260708-121117-99f5ed (`this.locators` referenced on a
            # class with no such field, `from tests.qtea_runtime import
            # tbd` in a `.ts` file — both invisible to body-verify).
            pre_verify_files = {
                sut_root / pom_task.pom_file for pom_task in pom_tasks.values()
                if (sut_root / pom_task.pom_file).is_file()
            } | set(bv_test_files)
            if pre_verify_files:
                pre_parse_result = await _run_phase_b65_parse_check(
                    sut_root=sut_root,
                    qtea_files=pre_verify_files,
                    agents_root=agents_root,
                    workdir=wd,
                    timeout_s=self.timeout_s,
                )
                ppc_path = out_dir / "parse-check-pre-verify-result.json"
                ppc_path.write_text(
                    json.dumps(pre_parse_result.as_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                ok_ppc, ppc_err = is_valid(pre_parse_result.as_dict(), "parse-check-result")
                if not ok_ppc:
                    log.warning("step08.pre_verify_parse_schema_invalid", error=ppc_err)
                if (
                    pre_parse_result.ran
                    and pre_parse_result.autofix_attempted
                    and pre_parse_result.post_fix_errors > 0
                ):
                    return StepResult(
                        success=False, status="failed",
                        outputs=[ppc_path],
                        error=(
                            f"parse-check (pre-body-verify): "
                            f"{pre_parse_result.post_fix_errors} parse error(s) "
                            f"remain after one autofix pass — file doesn't "
                            f"compile, so body-verify's method-body scan would "
                            f"be unreliable"
                        ),
                        notes=parse_check_format_for_fixer(pre_parse_result)[:500],
                    )
                if (
                    pre_parse_result.ran
                    and has_degraded_violations(pre_parse_result)
                ):
                    missing = ", ".join(pre_parse_result.missing_tools) or "unknown"
                    return StepResult(
                        success=False, status="failed",
                        outputs=[ppc_path],
                        error=(
                            f"parse-check (pre-body-verify): running in "
                            f"degraded (regex-smoke) mode for language(s) "
                            f"{', '.join(pre_parse_result.degraded_languages)} "
                            f"and found parse violation(s) that can't be "
                            f"verified without a real parser. Install: "
                            f"{missing} — then re-run Step 8."
                        ),
                        notes=parse_check_format_for_fixer(pre_parse_result)[:500],
                    )

            body_violations: list[str] = []
            for pom_task in pom_tasks.values():
                pom_abs = sut_root / pom_task.pom_file
                if not pom_abs.is_file():
                    continue
                for bv in verify_method_bodies(
                    pom_abs, pom_task.pom_name, pom_task.missing_methods,
                    test_files=bv_test_files, language=language,
                ):
                    body_violations.append(bv.format(pom_file=pom_task.pom_file))
            if body_violations:
                log.error(
                    "step08.body_verify_failed",
                    count=len(body_violations),
                    language=language,
                )
                (out_dir / "body-verify-violations.log").write_text(
                    "\n".join(body_violations), encoding="utf-8",
                )
                return StepResult(
                    success=False, status="failed",
                    outputs=[out_dir / "body-verify-violations.log"],
                    error=(
                        f"generated assertions fail {len(body_violations)} "
                        f"acceptance-criteria check(s) — a passing test would not "
                        f"actually verify the Step-4 expected value (false-green)"
                    ),
                    notes="\n".join(body_violations[:5])[:500],
                )

        # Stage-3 assertion-intent judge (SHADOW) — the semantic backstop the
        # deterministic gates can't be: does each generated test's assertions
        # verify a derivative of its title + the methods it calls, pinned to the
        # oracle? Runs an independent LLM (different model/persona than the
        # writer), logs verdicts to assertion-judge-shadow.json, and NEVER
        # blocks the step (QTEA_ASSERTION_JUDGE=off to disable). Promoted to
        # blocking only once shadow data supports it (SDET-agreed rollout).
        try:
            from qtea.assertion_judge import judge_assertions_shadow
            await judge_assertions_shadow(
                plan_data=plan_data,
                strategy_text=strategy_text,
                sut_root=sut_root,
                out_dir=out_dir,
                agents_root=agents_root,
                workdir=wd,
                language=language,
            )
        except Exception as _judge_exc:  # never let the shadow judge break codegen
            log.warning("step08.assertion_judge.wiring_error", error=str(_judge_exc))

        b5_skipped_reason: str | None = None
        if language not in _B5_SUPPORTED_LANGUAGES:
            b5_skipped_reason = language
            log.warning(
                "step08.b5.skipped",
                language=language,
                hint=(
                    f"B.5 reconciliation supports "
                    f"{sorted(_B5_SUPPORTED_LANGUAGES)}; language={language!r} "
                    f"was skipped — generated tests were NOT statically "
                    f"validated against POM/fixture signatures."
                ),
            )
        b5_test_files = (
            _b5_filter_test_files(agent_produced, language)
            if b5_skipped_reason is None else []
        )
        recon = reconcile_codegen(
            test_files=b5_test_files,
            pom_files=manifest["pom_files"],
            sut_root=sut_root,
            language=language,
        )
        # Fixture reconciliation runs alongside POM reconciliation: it walks
        # the plan and asserts every `source==create` fixture exists on disk
        # as a `@pytest.fixture`-decorated function. Catches the Phase A4
        # race that silently dropped 5 of 6 fixtures in run 20260611-184450.
        fx_files_scanned, fx_mismatches = reconcile_fixtures(
            plan_data, sut_root,
        )
        recon.fixture_files_scanned = fx_files_scanned
        recon.fixture_mismatches = fx_mismatches
        b5_autopatched = False
        b5_autopatch_error: str | None = None
        if recon.mismatches and b5_skipped_reason is None:
            log.info(
                "step08.b5.mismatches_found",
                count=len(recon.mismatches),
                kinds=sorted({m.kind for m in recon.mismatches}),
            )
            patch_tasks = mismatches_to_pom_tasks(
                recon.mismatches, pom_tasks,
                manifest_pom_files=manifest.get("pom_files"),
            )
            if patch_tasks:
                b5_autopatched = True
                # Robustness: _extend_poms makes LLM calls + disk reads;
                # transport / API / OSError must not crash the step. On
                # exception we hard-fail with the original mismatches in
                # the audit artifact.
                try:
                    await _extend_poms(
                        patch_tasks, sut_root, wd, agents_root, step=8,
                        rules_content=rules_content,
                        ctx=ctx,
                    )
                except Exception as e:
                    b5_autopatch_error = f"{type(e).__name__}: {e}"
                    log.error(
                        "step08.b5.autopatch_crashed",
                        error=b5_autopatch_error,
                    )
                else:
                    recon = reconcile_codegen(
                        test_files=b5_test_files,
                        pom_files=manifest["pom_files"],
                        sut_root=sut_root,
                        language=language,
                    )
                    recon.fixture_files_scanned = fx_files_scanned
                    recon.fixture_mismatches = fx_mismatches

        # Phase B.5 fixture auto-patch: re-run _create_fixtures for any
        # `source==create` fixture the plan declared but reconciliation
        # didn't find. Single retry (same MAX_AUTOPATCH semantics as POM
        # repair). `source==reuse` misses are NOT auto-patched.
        if recon.fixture_mismatches and b5_skipped_reason is None:
            fx_patch = fixture_mismatches_to_fixture_tasks(
                recon.fixture_mismatches, plan_data,
            )
            if fx_patch:
                log.info(
                    "step08.b5.fixture_autopatch.start",
                    count=len(fx_patch),
                    names=[t.name for t in fx_patch],
                )
                try:
                    await _create_fixtures(
                        fx_patch, sut_root, wd, agents_root,
                        active_module=active_module_dict, step=8,
                        rules_content=rules_content,
                    )
                except Exception as e:
                    log.error(
                        "step08.b5.fixture_autopatch_crashed",
                        error=f"{type(e).__name__}: {e}",
                    )
                else:
                    fx_files_scanned, fx_mismatches = reconcile_fixtures(
                        plan_data, sut_root,
                    )
                    recon.fixture_files_scanned = fx_files_scanned
                    recon.fixture_mismatches = fx_mismatches
                    b5_autopatched = True

        reconcile_path = out_dir / "reconcile-result.json"
        reconcile_path.write_text(
            json.dumps(recon.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ok_recon_schema, recon_schema_err = is_valid(
            recon.as_dict(), "reconcile-result",
        )
        if not ok_recon_schema:
            log.warning("step08.b5.schema_invalid", error=recon_schema_err)

        if b5_autopatch_error is not None:
            return StepResult(
                success=False,
                status="failed",
                outputs=[reconcile_path],
                error=(
                    f"Phase B.5 auto-patch crashed before re-verify: "
                    f"{b5_autopatch_error}"
                ),
                notes=(
                    f"{len(recon.mismatches)} mismatch(es) before crash; "
                    f"b5_autopatched=True"
                ),
            )

        if recon.mismatches:
            def _anchor(m: Any) -> str:
                base = (
                    f"{m.call_site.test_file}:{m.call_site.line} "
                    f"calls {m.resolved_pom}.{m.call_site.method_name}() "
                    f"({m.kind}"
                )
                if m.suggested_method:
                    base += f" — did you mean `{m.suggested_method}`?"
                return base + ")"

            anchors = "; ".join(_anchor(m) for m in recon.mismatches[:5])
            log.error(
                "step08.b5.reconciliation_failed",
                unresolved=len(recon.mismatches),
                autopatched=b5_autopatched,
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[reconcile_path],
                error=(
                    f"Phase B.5 reconciliation failed "
                    f"({'after auto-patch' if b5_autopatched else 'no autopatch tried'}): "
                    f"{anchors}"
                ),
                notes=f"{len(recon.mismatches)} unresolved mismatch(es)",
            )

        if recon.fixture_mismatches:
            def _fx_anchor(fm: Any) -> str:
                refs = (
                    f" used by {','.join(fm.referenced_by[:3])}"
                    if fm.referenced_by else ""
                )
                return (
                    f"{fm.expected_file} missing `{fm.name}` "
                    f"({fm.kind}, source={fm.source}{refs})"
                )

            fx_anchors = "; ".join(
                _fx_anchor(fm) for fm in recon.fixture_mismatches[:5]
            )
            log.error(
                "step08.b5.fixture_reconciliation_failed",
                unresolved=len(recon.fixture_mismatches),
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[reconcile_path],
                error=(
                    f"Phase B.5 fixture reconciliation failed: {fx_anchors}"
                ),
                notes=(
                    f"{len(recon.fixture_mismatches)} unresolved fixture "
                    f"mismatch(es)"
                ),
            )
        log.info(
            "step08.b5.reconciled",
            test_files=recon.test_files_scanned,
            call_sites=recon.call_sites_checked,
            autopatched=b5_autopatched,
            skipped=b5_skipped_reason,
        )

        # Phase B.5.5 (return-consumption gate) — the pom-extender
        # promoted a `Promise<void>` signature to `Promise<{...}>` on
        # run 20260708-121117-99f5ed and the test-writer emitted
        # `await pom.foo();` (discarded return), so the German-label
        # assertion the plan required was never executed. This gate
        # catches that shape by requiring every non-void POM method
        # call in the generated tests to consume its return value
        # (assign, destructure, wrap in expect(), return, throw, or
        # use as a sub-expression). Not autopatchable — the fix is a
        # semantic choice between "add expect(...)" vs "revert POM
        # signature to void".
        if (
            (language or "").lower() in {"typescript", "javascript"}
            and b5_skipped_reason is None
        ):
            from qtea.codegen_pom_hygiene import find_return_consumption_violations
            rc_violations: list[str] = []
            for pom_task in pom_tasks.values():
                names = {
                    _mm.get("name") for _mm in (pom_task.missing_methods or [])
                    if isinstance(_mm, dict) and _mm.get("name")
                }
                if not names:
                    continue
                pom_abs = sut_root / pom_task.pom_file
                if not pom_abs.is_file():
                    continue
                for v in find_return_consumption_violations(
                    pom_abs, pom_task.pom_name, names, b5_test_files,
                    language=language,
                ):
                    rc_violations.append(v.format())
            if rc_violations:
                log.error(
                    "step08.return_consumption_gate_failed",
                    count=len(rc_violations),
                )
                (out_dir / "return-consumption-violations.log").write_text(
                    "\n".join(rc_violations), encoding="utf-8",
                )
                return StepResult(
                    success=False, status="failed",
                    outputs=[out_dir / "return-consumption-violations.log"],
                    error=(
                        f"{len(rc_violations)} test call site(s) discard "
                        f"the return value of a non-void POM method — "
                        f"assertion is missing"
                    ),
                    notes="\n".join(rc_violations[:5])[:500],
                )

        # Safety-net scan: catch any hardcoded selector strings the agent
        # wrote into locator/POM files despite the tbd()-only instruction.
        # Runs BEFORE indexing so the converted tbd() sentinels appear in
        # the tbd-index and flow through Phase D intent quality scoring.
        # We compute `codegen_modified` early so the scanner can diff
        # against HEAD for new-line detection.
        import subprocess as _sp
        _diff_result = _sp.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(sut_root), capture_output=True, text=True,
            timeout=30, check=False,
        )
        codegen_modified: set[Path] = set()
        if _diff_result.returncode == 0 and _diff_result.stdout.strip():
            for _rel in _diff_result.stdout.strip().splitlines():
                _rel = _rel.strip()
                if _rel:
                    codegen_modified.add((sut_root / _rel).resolve())
        if codegen_modified:
            log.info(
                "step08.codegen_modified_files",
                count=len(codegen_modified),
            )

        _scan_and_convert_hardcoded_locators(
            sut_root, codegen_modified, dev_locators_map, language=language,
        )

        # -------------------------------------------------------------------
        # Phase B.5.5 — legacy XPath normalisation.
        # -------------------------------------------------------------------
        # `codegen_modified` covers files the agent WROTE this run AND files
        # it extended (e.g. a pre-existing POM class that got a new method).
        # When those pre-existing POMs ship xpath locator strings, they hit
        # the `[xpath]` gate — killing Step 8 for legacy code the agent
        # didn't author. Phase B.5.5 rewrites those xpath sites to
        # Playwright-idiomatic locators BEFORE the gate sees them.
        #
        # Handles ~90% of common patterns deterministically. Anything the
        # rewriter can't safely translate is kept in-place with a
        # `// qtea-xpath-exempt:` marker (test_indexer honours the marker)
        # AND collected as a straggler bundle for the LLM violation-fixer.
        _xpath_reports, _xpath_stragglers = _run_phase_b55_xpath_normalisation(
            sut_root=sut_root,
            candidates=codegen_modified,
        )

        # Index the SUT clone, then filter to ONLY qtea-prefixed entries so
        # the SUT's own pre-existing tests don't pollute our tbd-index or
        # trigger rule-violation reports for code we didn't write. Also drop
        # pre-vendored JIT runtime files (they are infrastructure, not
        # agent-authored tests/support — and they live under qtea-prefixed
        # names so they'd otherwise inflate the count).
        #
        # Resolve framework AFTER the agent ran when `detected_stack` was
        # None: the SUT now has the agent's files, so the extension fallback
        # in `resolve_framework` can pick the right framework (e.g. "pytest"
        # when the agent wrote `qtea_*_test.py`).
        framework = resolve_framework(detected_stack, sut_root)
        # Lazy-vendor for the no-detected-stack edge case (matches original
        # post-agent behavior). When `detected_stack` was set, vendoring
        # already happened pre-agent and this call short-circuits via the
        # `_RUNTIME_VENDORS` dispatch with idempotent file writes.
        if not detected_stack:
            late_added = _vendor_runtime_for_framework(framework, sut_root)
            for p in late_added:
                if p not in jit_files_added:
                    jit_files_added.append(p)
            jit_resolved.update(p.resolve() for p in late_added)

        # Build the union of agent-authored method names across all POM
        # tasks so `pom-assertion` (test_indexer RCA-D) can distinguish
        # "qtea just wrote this" (error) from pre-existing SUT code
        # (warning).
        agent_authored_methods: set[str] = set()
        for _pt in pom_tasks.values():
            for _mm in _pt.missing_methods or []:
                name = _mm.get("name") if isinstance(_mm, dict) else None
                if name:
                    agent_authored_methods.add(name)

        full_index = index_tests(
            sut_root, framework=framework,
            agent_authored_methods=agent_authored_methods,
        )
        index = _filter_index_to_qtea(
            full_index, sut_root,
            exclude=jit_resolved, include=codegen_modified,
        )
        payload = index.as_dict()

        index_path = out_dir / "tbd-index.json"
        index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Manifest: SUT-relative paths of every file the agent produced. Lets
        # downstream steps and human reviewers see the deliverable without
        # walking the SUT tree.
        # The vendored JIT runtime (`tests/qtea_runtime.py`,
        # `qtea-runtime.js`, etc.) is captured by the `qtea_*` rglob in
        # `produced_in_sut` but is qtea's template, not codegen output —
        # excluding it here keeps Phase B.6 from feeding template errors to
        # the violation-fixer, which is forbidden from touching it
        # (`agents/codegen-violation-fixer.agent.md` §"What NOT to Do").
        all_codegen_files = {
            p for p in produced_in_sut if p.resolve() not in jit_resolved
        }
        all_codegen_files.update(
            p for p in codegen_modified
            if p.is_file() and p not in jit_resolved
        )
        generated_manifest = {
            "sut_root": str(sut_root),
            "branch": f"qtea/run-{ctx.workspace.run_id}",
            "files": sorted(
                str(p.relative_to(sut_root).as_posix())
                for p in all_codegen_files
                if p.is_relative_to(sut_root)
            ),
        }
        manifest_path = out_dir / "generated-files.json"
        manifest_path.write_text(
            json.dumps(generated_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        ok_schema, schema_err = is_valid(payload, "tbd-index")
        if not ok_schema:
            log.warning("step08.schema_invalid", error=schema_err)

        # -------------------------------------------------------------------
        # Phase B.6.5 — language-native parse gate (runs BEFORE B.6).
        #
        # A file that doesn't tokenise cannot be type-checked. This gate
        # uses `ast.parse` for Python (stdlib, always available) and shells
        # to `tsc --noEmit --isolatedModules` / `node --check` / `javac`
        # for the other languages, with a regex smoke fallback when no
        # native tool is on PATH. On parse errors it invokes
        # `codegen-violation-fixer` once with rule=parse-error, re-runs
        # the check, and hard-fails the step if errors remain (mirrors
        # B.6's single-autofix philosophy).
        #
        # Added after run 20260701-114656-9394eb where the codegen agent
        # emitted `# Stack: typescript+playwright` (Python-style comment)
        # on line 1 of a `.spec.ts` file and B.6 was silently skipped
        # because tsc wasn't on PATH — the invalid file reached Step 9
        # unchallenged. This gate's loud-fail-on-degraded semantics close
        # that hole.
        # -------------------------------------------------------------------
        parse_check_result = await _run_phase_b65_parse_check(
            sut_root=sut_root,
            qtea_files=all_codegen_files,
            agents_root=agents_root,
            workdir=wd,
            timeout_s=self.timeout_s,
        )
        pc_path = out_dir / "parse-check-result.json"
        pc_path.write_text(
            json.dumps(parse_check_result.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ok_pc, pc_err = is_valid(parse_check_result.as_dict(), "parse-check-result")
        if not ok_pc:
            log.warning("step08.b65_schema_invalid", error=pc_err)

        # Fail-step gate for B.6.5.
        #
        # Case 1 — autofix ran and errors persisted: canonical hard fail
        # (mirrors B.6). The violation-fixer had its single retry and did
        # not resolve the parse error; escalate to the step retry.
        #
        # Case 2 — degraded mode fired a violation: even though the fixer
        # may not have run yet (or ran and cleared other files), a
        # regex-smoke violation on a language where no real parser was
        # available cannot be trusted as "actually broken" without
        # verification. Refuse to proceed so the operator installs the
        # missing tool (surfaced in `missing_tools`).
        if (
            parse_check_result.ran
            and parse_check_result.autofix_attempted
            and parse_check_result.post_fix_errors > 0
        ):
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, pc_path],
                error=(
                    f"parse-check (Phase B.6.5): "
                    f"{parse_check_result.post_fix_errors} "
                    f"parse error(s) remain after one autofix pass"
                ),
                notes=parse_check_format_for_fixer(parse_check_result)[:500],
            )
        if (
            parse_check_result.ran
            and has_degraded_violations(parse_check_result)
        ):
            missing = ", ".join(parse_check_result.missing_tools) or "unknown"
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, pc_path],
                error=(
                    f"parse-check (Phase B.6.5): running in degraded "
                    f"(regex-smoke) mode for language(s) "
                    f"{', '.join(parse_check_result.degraded_languages)} "
                    f"and found parse violation(s) that can't be verified "
                    f"without a real parser. Install: {missing} — then "
                    f"re-run Step 8. Set QTEA_NO_PARSE_CHECK=1 to bypass "
                    f"(NOT RECOMMENDED)."
                ),
                notes=parse_check_format_for_fixer(parse_check_result)[:500],
            )

        # -------------------------------------------------------------------
        # Phase B.6 — native static-check gate.
        # Runs the SUT stack's own type-checker (pyright for Python; tsc with
        # --allowJs --checkJs for JS/TS) against the qteaouched files.
        # Catches the bug class AST-reconciliation (B.5) cannot see:
        # class-vs-instance attribute access, missing imports, wrong arg
        # counts, stale rename references. Single autofix attempt mirroring
        # B.5's philosophy; persisting errors escalate to step-level retry.
        # -------------------------------------------------------------------
        static_check_result = await _run_phase_b6(
            sut_root=sut_root,
            framework=framework,
            qteaouched=all_codegen_files,
            agents_root=agents_root,
            workdir=wd,
            timeout_s=self.timeout_s,
        )
        sc_path = out_dir / "static-check-result.json"
        sc_path.write_text(
            json.dumps(static_check_result.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ok_sc, sc_err = is_valid(static_check_result.as_dict(), "static-check-result")
        if not ok_sc:
            log.warning("step08.b6_schema_invalid", error=sc_err)
        if (
            static_check_result.ran
            and static_check_result.autofix_attempted
            and static_check_result.post_fix_errors > 0
        ):
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, sc_path],
                error=(
                    f"static-check (Phase B.6): "
                    f"{static_check_result.post_fix_errors} "
                    f"type error(s) remain after one autofix pass"
                ),
                notes=format_for_fixer(static_check_result)[:500],
            )

        blocking = blocking_violations(index)
        if blocking:
            summary = violations_summary(index)
            log.info(
                "step08.violation_self_fix",
                count=len(blocking),
                advisory=len(index.violations) - len(blocking),
                framework=framework,
            )
            fix_agent = agents_root / "codegen-violation-fixer.agent.md"
            await run_agent(
                fix_agent,
                workdir=wd,
                inputs={},
                user_prompt=(
                    f"Your generated code has {len(blocking)} "
                    f"non-negotiable rule violation(s):\n\n"
                    f"```\n{summary}\n```\n\n"
                    f"Fix each violation IN-PLACE by rewriting the "
                    f"offending file(s). Hard waits (`wait_for_timeout`, "
                    f"`time.sleep`, `cy.wait(<ms>)`) must be replaced "
                    f"with Playwright's built-in auto-waiting (e.g. "
                    f"`expect(locator).to_be_visible()`, "
                    f"`locator.click()` which auto-waits, "
                    f"`page.wait_for_selector()`). XPath selectors must "
                    f"be replaced with CSS / data-testid / role "
                    f"selectors. Write the corrected files using the "
                    f"same absolute paths.\n\n"
                    f"The full codegen rules are in "
                    f"`{agents_root / 'codegen-rules.md'}` — read it "
                    f"if you need to understand why a rule exists or "
                    f"what the correct replacement pattern is."
                ),
                extra_paths=[package_resource_root() / "skills" / "webapp-testing"],
                add_dirs=[sut_root],
                timeout_s=min(self.timeout_s or 1800, 300),
                step=8,
                max_turns=AUTOFIX_MAX_TURNS,
            )

            full_index = index_tests(
                sut_root, framework=framework,
                agent_authored_methods=agent_authored_methods,
            )
            index = _filter_index_to_qtea(
                full_index, sut_root,
                exclude=jit_resolved, include=codegen_modified,
            )
            payload = index.as_dict()
            index_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # Step 8.5 semantic preflight — static-import / fixture-graph /
        # sentinel-constant checks. Runs AFTER the violation-fix loop because
        # the fix-agent isn't trained to repair these defect classes; surfacing
        # them now is cheaper than letting Step 9 collection-time discover them.
        try:
            plan_for_preflight = json.loads(plan_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            plan_for_preflight = {}
        try:
            inventory_for_preflight = json.loads(
                sut_inv_json.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            inventory_for_preflight = None
        preflight_files = {
            str(p.relative_to(sut_root).as_posix())
            for p in all_codegen_files
            if p.is_relative_to(sut_root)
        }
        try:
            strategy_md_text = strategy_md.read_text(encoding="utf-8")
        except OSError:
            strategy_md_text = ""
        preflight_violations = run_preflight(
            sut_root,
            framework=framework,
            generated_files=preflight_files,
            plan=plan_for_preflight,
            inventory=inventory_for_preflight,
            strategy_md=strategy_md_text,
        )
        if preflight_violations:
            log.warning(
                "step08.preflight_failed",
                count=len(preflight_violations),
            )
            index.violations.extend(preflight_violations)
            # Re-serialise the index so consumers see preflight rows too.
            payload = index.as_dict()
            index_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # Persist the violations.log when ANY violations exist (errors OR
        # warnings) so advisory-mode rules are auditable; only hard-fail when
        # at least one ERROR-severity violation remains.
        post_fix_blocking = blocking_violations(index)
        if index.violations:
            summary = violations_summary(index)
            (out_dir / "violations.log").write_text(summary, encoding="utf-8")
            log.warning(
                "step08.violations",
                errors=len(post_fix_blocking),
                warnings=len(index.violations) - len(post_fix_blocking),
                framework=framework,
            )
        if post_fix_blocking:
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, out_dir / "violations.log"],
                error=f"non-negotiable rule violations: {len(post_fix_blocking)}",
                notes=violations_summary(index)[:500],
            )

        # Phase gate: indexer must find at least one real test function.
        # Support files (page objects / locators with TBDs) alone don't
        # count — the user wants actual test coverage, not just scaffolding.
        if not index.tests:
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path],
                error=(
                    f"indexer found 0 qtea_*-prefixed test functions under "
                    f"{sut_root} (support files: {len(index.support_files)}, "
                    f"total generated: {len(produced_in_sut)}). The agent "
                    f"may have written only locator/page-object scaffolding. "
                    f"Inspect the qtea_ files listed in generated-files.json."
                ),
            )

        # --- Phase D: TBD intent quality gate -------------------------------
        # Score every `tbd("intent")` / `Tbd.of("intent")` sentinel before
        # the JIT resolver consumes them at runtime. Cheap one-shot Haiku
        # call; results persist alongside the other Step 8 artifacts.
        (
            phase_d_ok, phase_d_summary, phase_d_warnings, phase_d_error,
        ) = await _phase_d_score_intents(
            produced_in_sut=produced_in_sut,
            jit_files_added=jit_files_added,
            sut_root=sut_root,
            out_dir=out_dir,
            workdir=wd,
            agents_root=agents_root,
        )
        intent_quality_path = out_dir / "tbd-intent-quality.json"
        intent_quality_path.write_text(
            json.dumps(phase_d_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Phase D auto-fix: when WARN/FAIL intents exist, attempt one
        # automated rewrite via the tbd-intent-editor agent, then re-score.
        # Single attempt — mirrors B5_MAX_AUTOPATCH_RETRIES philosophy.
        fixable = [
            e for e in (phase_d_warnings or [])
            if e.get("score") in ("WARN", "FAIL")
        ]
        if fixable:
            rewritten, fix_errors = await _auto_fix_intents(
                fixable, sut_root, wd, agents_root,
            )
            if fix_errors:
                log.warning(
                    "step08.phase_d.autofix_errors",
                    errors=fix_errors[:5],
                )
            if rewritten > 0:
                log.info(
                    "step08.phase_d.autofix_rescoring",
                    rewritten=rewritten,
                )
                (
                    phase_d_ok, phase_d_summary,
                    phase_d_warnings, phase_d_error,
                ) = await _phase_d_score_intents(
                    produced_in_sut=produced_in_sut,
                    jit_files_added=jit_files_added,
                    sut_root=sut_root,
                    out_dir=out_dir,
                    workdir=wd,
                    agents_root=agents_root,
                )
                intent_quality_path.write_text(
                    json.dumps(phase_d_summary, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        if not phase_d_ok:
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, intent_quality_path],
                error=phase_d_error or "intent quality gate failed",
                notes=f"fail={phase_d_summary.get('summary', {}).get('fail', 0)}",
            )
        # Stash WARN entries for the post-step review gate. Use ctx.extras
        # (already used by Step 9 etc.) — the gate is only rendered on TTY.
        if phase_d_warnings:
            ctx.extras["step8_intent_warnings"] = phase_d_warnings
            ctx.extras["step8_intent_quality_path"] = str(intent_quality_path)

        # JIT runtime files were already vendored before the agent ran
        # (see the pre-vendoring block at the top of `run`). Make sure they
        # are present in `produced_in_sut` so the commit manifest below
        # records them alongside the agent's authored files — the rglob
        # walk above catches `qtea_runtime.py` and `QteaT.java` but
        # MAY miss `qtea-runtime.js` (hyphen vs underscore in the
        # `qtea_*` glob), so re-add explicitly to be safe. Set-based
        # dedup via resolve() avoids double-entries on Windows.
        already = {p.resolve() for p in produced_in_sut}
        for p in jit_files_added:
            if p.resolve() not in already:
                produced_in_sut.append(p)

        # Commit the agent's work to the qtea branch. Per-step commits
        # give the human reviewer a clear `git log` trail of who-wrote-what.
        sha = commit_step(
            sut_root, self.number, self.name,
            message_detail=f"{len(produced_in_sut)} files, {len(index.tests)} tests",
        )

        # Rewrite generated-files.json with the ACTUAL commit changeset.
        # The pre-commit write above (line ~2091) was glob-based and misses
        # in-place modifications to existing files (POM extensions, locator
        # appends, conftest patches). Run 20260611-184450 surfaced this:
        # the manifest listed 3 files while the commit modified 6. Falls
        # back to the glob result when the diff query fails or returns empty.
        if sha:
            committed_files = files_in_commit(sut_root, sha)
            if committed_files:
                glob_paths = {
                    str(p.relative_to(sut_root).as_posix())
                    for p in produced_in_sut
                }
                merged = sorted(set(committed_files) | glob_paths)
                manifest_path.write_text(
                    json.dumps(
                        {
                            "sut_root": str(sut_root),
                            "branch": f"qtea/run-{ctx.workspace.run_id}",
                            "commit": sha,
                            "files": merged,
                        },
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                log.info(
                    "step08.generated_manifest.rewritten",
                    commit=sha,
                    file_count=len(merged),
                )

        total_tbd = (
            sum(len(t.tbd_markers) for t in index.tests)
            + sum(len(s.tbd_markers) for s in index.support_files)
        )
        notes = (
            f"framework={framework} files={len(index.files)} "
            f"tests={len(index.tests)} "
            f"support_files={len(index.support_files)} tbd={total_tbd} "
            f"b5_autopatched={b5_autopatched}"
        )
        if b5_skipped_reason is not None:
            notes += f" b5_skipped={b5_skipped_reason}"
        if sha:
            notes += f" commit={sha}"
        if not ok_schema:
            notes += f"; schema_warning={schema_err}"
        if not ok_recon_schema:
            notes += f"; b5_schema_warning={recon_schema_err}"
        return StepResult(
            success=True,
            status="completed" if ok_schema else "warned",
            outputs=[index_path, manifest_path, reconcile_path],
            notes=notes,
        )
