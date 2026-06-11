"""Step 8: TDD codegen via ui-test-automation.

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
     `worca-t/run-<id>` branch (no per-step copy).
  4. Index the SUT, filter to `worca_*`-prefixed files (the agent's
     filename-collision convention), enforce non-negotiable rules.
  5. Commit the step's changes to the worca-t branch.

Outputs (artifacts/step08/):
  - tbd-index.json            (worca-only index + violations)
  - generated-files.json      (SUT-relative paths of files this step wrote)
  - violations.log            (only when violations exist)

The generated test bytes live in `<workspace>/sut/` on the branch —
review via `git diff worca-t/run-<id>` rather than reading a duplicate copy.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from worca_t._sut_git import commit_step
from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.test_indexer import IndexResult, index_tests, resolve_framework, violations_summary

log = get_logger(__name__)


def _vendor_jit_runtime(sut_root: Path) -> Path | None:
    """Copy the worca-t JIT runtime template into `<sut>/tests/worca_t_runtime.py`.

    Returns the destination path on success, or None if the template can't be
    located (best-effort — the absence is logged; Step 8 then falls through
    to the polyglot-test-fixer heal flow for non-Playwright stacks).
    """
    template = (
        package_resource_root() / "_resources" / "runtime" / "worca_t_runtime.py.tpl"
    )
    if not template.is_file():
        # Dev tree may not have the template under _resources (e.g. when
        # WORCA_T_RESOURCE_ROOT points at the repo root). Try src/.
        alt = (
            package_resource_root() / "src" / "worca_t" / "_resources"
            / "runtime" / "worca_t_runtime.py.tpl"
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
    dest = dest_dir / "worca_t_runtime.py"
    try:
        dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("step08.jit_runtime_vendored", path=str(dest))
        return dest
    except OSError as e:
        log.warning("step08.jit_runtime_vendor_failed", error=str(e))
        return None


def _ensure_conftest_registers_runtime(sut_root: Path) -> None:
    """Make sure at least one conftest.py under `<sut>/tests/` has
    `pytest_plugins` referencing `tests.worca_t_runtime`.

    Idempotent — if a conftest already registers it, no change. If no
    conftest exists at the tests/ root, creates a minimal one. If a
    conftest exists but doesn't register the plugin, appends the
    registration line.
    """
    tests_dir = sut_root / "tests"
    if not tests_dir.is_dir():
        return
    conftest = tests_dir / "conftest.py"
    plugin_line = 'pytest_plugins = ["tests.worca_t_runtime"]\n'
    if not conftest.exists():
        conftest.write_text(
            "# worca-t generated: registers the JIT locator runtime plugin\n"
            + plugin_line,
            encoding="utf-8",
        )
        log.info("step08.jit_conftest_created", path=str(conftest))
        return
    try:
        existing = conftest.read_text(encoding="utf-8")
    except OSError:
        return
    if "tests.worca_t_runtime" in existing:
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
                items + ', "tests.worca_t_runtime"' if items
                else '"tests.worca_t_runtime"'
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
        f.write("\n# worca-t: register the JIT locator runtime plugin\n")
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
    src/-prefixed fallback for dev trees where WORCA_T_RESOURCE_ROOT
    points at the repo root rather than the installed wheel."""
    primary = (
        package_resource_root() / "_resources" / "runtime" / filename
    )
    if primary.is_file():
        return primary
    fallback = (
        package_resource_root() / "src" / "worca_t" / "_resources"
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
    ``globalSetup: "./tests/worca-t-runtime"`` manually in that case.
    """
    for ext in ("ts", "mts", "cts", "js", "mjs", "cjs"):
        cfg = sut_root / f"playwright.config.{ext}"
        if cfg.is_file():
            break
    else:
        log.warning(
            "step08.jit_pw_config_missing",
            hint="playwright.config.* not found; runtime vendored but not "
                 "registered. Add `globalSetup: \"./tests/worca-t-runtime\"` "
                 "manually.",
        )
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    if "worca-t-runtime" in text:
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
    if "worca-t-runtime" in text:
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

    1. Copy ``worca-t-runtime.js.tpl`` to ``<sut>/tests/worca-t-runtime.js``.
       Single CommonJS file — works in CJS and ESM-interop modes.
    2. Detect the test runner from ``package.json``.
    3. Register the runtime with the runner's setup hook:
       - Playwright Test → ``globalSetup`` in ``playwright.config.*``
       - Jest / Vitest → log a manual-registration hint (auto-edit of
         their configs is brittle; left for follow-up).

    Returns the list of files created (for commit manifest).
    """
    template = _locate_runtime_template("worca-t-runtime.js.tpl")
    if template is None:
        log.warning(
            "step08.jit_runtime_template_missing",
            framework="typescript-playwright",
            hint="worca-t-runtime.js.tpl not found in _resources/runtime/.",
        )
        return []
    tests_dir = sut_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    dest = tests_dir / "worca-t-runtime.js"
    try:
        dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        log.warning("step08.jit_runtime_vendor_failed", error=str(e))
        return []
    log.info("step08.jit_runtime_vendored", path=str(dest))

    runtime_rel = "tests/worca-t-runtime.js"
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
                 "themselves: `require('./tests/worca-t-runtime');`",
        )
    return [dest]


def _vendor_java_playwright_runtime(sut_root: Path) -> list[Path]:
    """Vendor the Java Playwright JIT runtime (JUnit5 + TestNG).

    Strategy:

    1. Copy ``Tbd.java``, ``WorcaT.java``, ``WorcaTResolver.java`` into
       the SUT's ``src/test/java/com/worca/runtime/`` directory. Standard
       Maven + Gradle source layout applies to both JUnit5 and TestNG.
    2. The agent prompt instructs codegen to import ``com.worca.runtime.Tbd``
       for sentinel constants and call ``WorcaT.wrap(page)`` once at the
       Page acquisition site (typically in ``@BeforeEach`` / ``@BeforeMethod``).
       Java does not permit runtime method replacement the way Python /
       JavaScript do, so explicit wrapping is the cleanest path.

    Returns the list of files created (for commit manifest).
    """
    java_templates = ("Tbd.java.tpl", "WorcaT.java.tpl", "WorcaTResolver.java.tpl")
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

    dest_dir = sut_root / "src" / "test" / "java" / "com" / "worca" / "runtime"
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
        # WorcaT.wrap(page) once at Page acquisition; the agent's TBD-rule §3c
        # documents the pattern with examples.
        log.info(
            "step08.jit_java_runtime_ready",
            count=len(created),
            hint=(
                "Java JIT activated via explicit WorcaT.wrap(page) in tests. "
                "Build tool (Maven/Gradle) picks up src/test/java/com/worca/"
                "runtime/ automatically; no config edit needed."
            ),
        )
    return created


# Framework name → vendor function. Framework strings match the values
# produced by `worca_t.test_indexer.resolve_framework()`.
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
    return vendor_fn(sut_root)


def _read_research(ctx: StepContext) -> dict:
    research_json = ctx.workspace.step_dir(6) / "research.json"
    if not research_json.exists():
        return {}
    try:
        return json.loads(research_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_detected_stack(ctx: StepContext) -> str | None:
    return _read_research(ctx).get("detected_stack")


def _select_skills(detected_stack: str | None) -> list[str]:
    if not detected_stack:
        return ["webapp-testing"]
    if "playwright" in detected_stack:
        return ["playwright-generate-test", "webapp-testing"]
    return ["webapp-testing"]


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


def _is_worca_file(rel: str) -> bool:
    """True for paths whose basename matches the agent's `worca_`/`Worca` convention."""
    name = Path(rel).name.lower()
    return name.startswith("worca")


def _filter_index_to_worca(
    index: IndexResult,
    sut_root: Path,
    *,
    exclude: set[Path] | None = None,
) -> IndexResult:
    """Return a new IndexResult containing only worca-prefixed entries.

    `index_tests` walks the whole SUT and picks up the SUT's own pre-existing
    tests / locators alongside worca-generated ones. The user-facing tbd-index
    must reflect only what worca-t produced, otherwise Step 7's "tests=N" gate
    and Step 8's downstream logic would race against unrelated SUT code.

    ``exclude`` lets the caller drop additional worca-prefixed paths from the
    index — used to skip pre-vendored JIT runtime files (`worca_t_runtime.py`,
    `worca-t-runtime.js`, `WorcaT.java`) so they don't inflate the test/support
    counts. Paths in ``exclude`` must be resolved absolute paths.
    """
    exclude_set: set[Path] = exclude or set()

    def _keep(rel_path: str) -> bool:
        if not _is_worca_file(rel_path):
            return False
        if not exclude_set:
            return True
        try:
            abs_resolved = (sut_root / rel_path).resolve()
        except OSError:
            return True
        return abs_resolved not in exclude_set

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


class CodegenStep(Step):
    number = 8
    name = "codegen"
    timeout_s = step_timeout(8)

    @staticmethod
    def _build_runtime_hint(
        framework: str, jit_files: list[Path], sut_root: Path,
    ) -> str:
        """One-paragraph hint telling the agent EXACTLY where the JIT runtime
        is (or that none was vendored). Eliminates the "find worca_t_runtime"
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
                "Do NOT search for `worca_t_runtime` / `worca-t-runtime` / "
                "`Tbd.java` — they are intentionally absent for this stack."
            )
        paths = "\n".join(f"  - `{p}`" for p in jit_files)
        return (
            "\n\n--- JIT RUNTIME (pre-vendored) ---\n"
            f"Framework `{framework}` runtime is ALREADY written to the SUT "
            f"at:\n{paths}\n"
            "**Do NOT search for it. Do NOT attempt to create it.** Import "
            "it directly per agent.md §3a-c:\n"
            "  - Python+pytest+PW → `from tests.worca_t_runtime import tbd`\n"
            "  - TS/JS+PW → `import { tbd } from \"./worca-t-runtime\"` "
            "(or relative path)\n"
            "  - Java+PW → `import com.worca.runtime.Tbd;`\n"
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

        strategy_md = ctx.workspace.step_dir(4) / "test-strategy.md"
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

        # C2: step 7 (test-architect) must have run. The plan is the
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

        # --- Stage planning artifacts (no SUT bytes) ------------------------
        #
        # Minimum-sufficient input set, ranked by authority:
        #   - test-strategy.md (step 4): the curated, authoritative test
        #     specification. Includes the test cases the agent must implement.
        #   - sut_inventory.json (step 6): the SUT's own layout (existing page
        #     objects, fixtures, locators, helpers). The active module record
        #     lives at modules[active_module] — the agent extracts it on read.
        #
        # Deliberately NOT staged:
        #   - plan.md (step 3) + refined-spec.md (step 2): redundant with
        #     test-strategy.md, which is derived from them.
        #   - research.md (step 6): every datum the agent needed (env var
        #     names via env_hint; frameworks + layout via sut_inventory;
        #     build/test commands consumed by step 8 via research.json) is
        #     already supplied by other inputs. Saves ~25 KB / turn.
        #   - active_module.json: byte-identical duplicate of
        #     sut_inventory.json["modules"][active_module]. Saves ~22 KB / turn.
        inputs = {
            "code-modification-plan.json": plan_json,
            "test-strategy.md": strategy_md,
        }
        if sut_inv_json.exists():
            inputs["sut_inventory.json"] = sut_inv_json

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "ui-test-automation.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in _select_skills(detected_stack):
            p = skills_root / skill
            if p.exists():
                extras.append(p)

        stack_hint = f"Detected stack: `{detected_stack}`. " if detected_stack else ""

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
        # import `tests.worca_t_runtime` / `./worca-t-runtime` / `com.worca.
        # runtime.Tbd`, but that file did not exist yet. Empirical fallout
        # from run 20260610-114657-c9c7c3 step 7 attempt 1: the agent spent
        # 80 turns burning the 1800s timeout with ZERO Writes, including 10
        # Greps for `def tbd|class.*Runtime|__WORCA_T_TBD__`, 18 Bash
        # `find`/`grep` calls, and one spawned subagent literally named
        # "Find worca_t_runtime template". By line 931 it decided IT had
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
                "for or attempt to create a worca-t runtime file."
            )

        # Reuse + folder integration: when an active module is known, tell the
        # agent which language to write in, which directories to land each
        # category of file in (tests vs production code), and which existing
        # page objects/helpers/fixtures it MUST extend rather than re-implement.
        # The agent writes ABSOLUTE paths under `<workspace>/sut/` (granted via
        # `add_dirs=[sut_root]`) — tests + fixtures + data into the SUT's own
        # test directory, page objects + locators into its src tree. The
        # `worca_` filename prefix prevents collisions with the SUT's own files
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
            pages_locators_dir = src_layout.get("pages_locators_dir") or f"{base_dir}/pages/locators"
            helpers_dir = src_layout.get("helpers_dir") or f"{base_dir}/helpers"
            fixtures_dir = f"{base_dir}/fixtures"  # fixtures always under tests/
            data_dir = f"{base_dir}/data"

            # `--isolated-tests` opts into a dedicated `worca-tests/` subdir
            # for the test files (Step 8's runner mirrors this resolution).
            # Page objects + locators + helpers still go under the SUT's src
            # tree in both modes — they have no parallel "isolated" home and
            # the worca_ prefix already prevents file collisions.
            tests_subdir = "worca-tests" if isolated else default_target
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
                f"EXISTING FIXTURES:\n{fixture_lines}\n\n"
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
                f"the deliverable, on a worca-t-owned git branch. Specifically:\n"
                f"   - Test files → `{abs_tests}/worca_test_<feature>.<ext>`\n"
                f"   - Test data → `{abs_data}/worca_<feature>_data.<ext>`\n"
                f"   - Fixtures → `{abs_fixtures}/worca_<feature>_fixture.<ext>`\n"
                f"   - Page objects → `{abs_pages_object}/worca_<feature>_page.<ext>`\n"
                f"   - Locators → `{abs_pages_locators}/worca_<feature>_locators.<ext>`\n"
                f"   - Helpers → `{abs_helpers}/worca_<feature>_helper.<ext>`\n"
                f"   Prefix EVERY generated filename with `worca_` so collisions "
                f"with the SUT's own files stay at zero. Use the Write tool with "
                f"those absolute paths directly — do NOT write into "
                f"`./tests/` or `./src/` relative to your cwd, since your cwd "
                f"is the worca-t step workdir, NOT the SUT.\n"
                f"4. Match the active module's language: `{language}`. Never "
                f"emit Python tests for a TypeScript module or vice versa.\n"
            )

        result = await run_agent(
            agent,
            workdir=wd,
            inputs=inputs,
            user_prompt=(
                f"{stack_hint}**Placement is decided by Step 7 — read the plan first.** "
                f"`./code-modification-plan.json` is the authoritative placement "
                f"contract. For each test case it specifies the test_file_target, "
                f"test_functions (with markers + uses_fixtures), fixtures (reuse "
                f"vs create with `from`/`at` pointers), page_objects (reuse vs "
                f"create with missing_methods + signatures), and locators (reuse "
                f"with `from`, or create_tbd with an `intent` string). Do NOT "
                f"re-derive any of these — your job is to transpile the plan "
                f"into executable code. **For every `reuse` entry: import from "
                f"the `from` reference. For every `create` entry: write at the "
                f"`at` path. For every `missing_methods` entry: add the method "
                f"to the existing POM file with the specified signature. For "
                f"every `create_tbd` locator: emit `tbd(\"<intent>\")` (Python/"
                f"TS/JS) or `Tbd.of(\"<intent>\")` (Java) or `TBD_LOCATOR` + "
                f"`TBD_INTENT:` comment (other stacks) using the plan's intent.**\n\n"
                f"`./sut_inventory.json` is the secondary input — use it only "
                f"for byte-match locator dedup (Rule 7 below) and style mimicry "
                f"(naming conventions, import patterns).\n\n"
                f"**`./test-strategy.md` is AUTHORITATIVE for assertion content.** "
                f"The plan tells you WHERE code goes and which POM methods to "
                f"call; the strategy tells you WHAT each test must assert. For "
                f"every test case in the plan, locate the matching `#### "
                f"TC-<id>:` section in the strategy and lift its `Steps:` and "
                f"`Expected Result:` clauses VERBATIM into your assertions. "
                f"When the strategy says `Assert href equals \"https://...\"`, "
                f"emit `assert actual == \"https://...\"` — NOT `assert actual` "
                f"(truthy), NOT `assert \"http\" in actual` (substring), NOT "
                f"`assert len(actual) > 0` (non-empty). Same for locale "
                f"strings, ARIA labels, counts, attribute values: copy the "
                f"exact literal from the strategy into an equality assertion. "
                f"Weak/loose assertions (truthy, substring, length-only) are a "
                f"defect — they pass when the SUT regresses and defeat the "
                f"purpose of the test. The plan does NOT carry these values; "
                f"the strategy does. Skipping the strategy means skipping the "
                f"assertions.\n\n"
                f"The SUT clone you are testing is at the absolute path "
                f"`{sut_root}` (read it directly via Read/Grep/Glob — no copy "
                f"is staged in your working directory). Generate executable "
                f"test code by writing files at their ABSOLUTE paths under "
                f"`{sut_root}/` (see the per-category placement table below). "
                f"The pipeline does NOT copy your output anywhere — your "
                f"writes ARE the deliverable. Hard rules: locator priority "
                f"`id > data-testid > role > label > text > placeholder > "
                f"scoped css`; NO XPath; NO hard waits (no `time.sleep`, no "
                f"`cy.wait(<number>)`, no `waitForTimeout`); NO "
                f"`page.content()` - use AOM snapshots; no inline credentials. "
                f"Unresolved selectors: follow the four-branch TBD-marker rule "
                f"in your agent.md §3 (Python+PW -> `tbd(...)`, TS/JS+PW -> "
                f"`tbd(...)` from `./worca-t-runtime`, Java+PW -> "
                f"`Tbd.of(...)`, all other stacks -> `TBD_LOCATOR` + "
                f"`TBD_INTENT:` comment). Pick the branch that matches "
                f"`sut_inventory.json[\"modules\"][active_module].language` + "
                f"framework — never mix."
                f"\n\n--- DISCOVERY DISCIPLINE (non-negotiable) ---\n"
                f"1. **Do NOT use Bash for filesystem discovery.** No `find`, "
                f"no `grep -r`, no `ls`. Use `Read` for known paths and "
                f"`Glob`/`Grep` tools for pattern search — they are faster, "
                f"cheaper, and don't pay shell-spawn overhead per call.\n"
                f"2. **Trust `sut_inventory.json`.** Every existing page "
                f"object, fixture, helper, and locator class is listed there "
                f"with its absolute file path. Read those paths directly — "
                f"do NOT Glob/Grep for files the inventory already names.\n"
                f"3. **Discovery budget: ≤5 reads, ≤2 Glob/Grep calls before "
                f"your first `Write`.** Reading `sut_inventory.json` + "
                f"`test-strategy.md` + the 1–3 existing files you'll extend "
                f"is enough context to start writing. If you find yourself "
                f"reading a 4th SUT file before any Write, stop and write.\n"
                f"4. **Batch independent `Write` calls in a single response.** "
                f"When you have content ready for N files, emit N `Write` "
                f"tool calls in the same assistant turn — do not serialize "
                f"them one-per-turn.\n"
                f"5. **Per-file size discipline.** A locator class, page "
                f"object, or test file should be ≤200 lines. If you find "
                f"yourself generating more, split by feature.{env_hint}"
                f"{runtime_hint}{reuse_hint}"
            ),
            extra_paths=extras,
            add_dirs=[sut_root],
            timeout_s=self.timeout_s,
            step=8,
            max_turns=40,
            claude_md=claude_md if claude_md.exists() else None,
        )

        # The agent now writes ABSOLUTE paths under `<workspace>/sut/` via
        # `add_dirs=[sut_root]`. Detect what it produced by walking the SUT
        # for the `worca_` filename convention (enforced in the prompt and
        # by the indexer's worca_ globs in `test_indexer._TEST_FILE_GLOBS`).
        produced_in_sut: list[Path] = sorted(
            p for p in sut_root.rglob("worca_*")
            if p.is_file() and ".git" not in p.parts
        )
        # Capitalised Java pattern (`Worca*Test.java`) — search separately
        # since the lowercase glob above misses it.
        produced_in_sut.extend(sorted(
            p for p in sut_root.rglob("Worca*")
            if p.is_file() and ".git" not in p.parts and p not in produced_in_sut
        ))
        # JIT runtime files were vendored BEFORE the agent ran (see the
        # pre-vendoring block above). They live under worca-prefixed names
        # (`worca_t_runtime.py`, `worca-t-runtime.js`, `WorcaT.java`, ...)
        # and will show up in `produced_in_sut` via the rglob above — but
        # they are NOT agent output, so they don't count toward the
        # "did the agent write anything?" gate.
        jit_resolved = {p.resolve() for p in jit_files_added}
        agent_produced = [p for p in produced_in_sut if p.resolve() not in jit_resolved]
        if not result.success or not agent_produced:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    result.error or
                    f"agent did not produce any worca_*-prefixed files under {sut_root}"
                ),
            )

        # Index the SUT clone, then filter to ONLY worca-prefixed entries so
        # the SUT's own pre-existing tests don't pollute our tbd-index or
        # trigger rule-violation reports for code we didn't write. Also drop
        # pre-vendored JIT runtime files (they are infrastructure, not
        # agent-authored tests/support — and they live under worca-prefixed
        # names so they'd otherwise inflate the count).
        #
        # Resolve framework AFTER the agent ran when `detected_stack` was
        # None: the SUT now has the agent's files, so the extension fallback
        # in `resolve_framework` can pick the right framework (e.g. "pytest"
        # when the agent wrote `worca_test_*.py`).
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
        full_index = index_tests(sut_root, framework=framework)
        index = _filter_index_to_worca(full_index, sut_root, exclude=jit_resolved)
        payload = index.as_dict()

        index_path = out_dir / "tbd-index.json"
        index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Manifest: SUT-relative paths of every file the agent produced. Lets
        # downstream steps and human reviewers see the deliverable without
        # walking the SUT tree.
        generated_manifest = {
            "sut_root": str(sut_root),
            "branch": f"worca-t/run-{ctx.workspace.run_id}",
            "files": [str(p.relative_to(sut_root).as_posix())
                      for p in produced_in_sut],
        }
        manifest_path = out_dir / "generated-files.json"
        manifest_path.write_text(
            json.dumps(generated_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        ok_schema, schema_err = is_valid(payload, "tbd-index")
        if not ok_schema:
            log.warning("step08.schema_invalid", error=schema_err)

        if index.violations:
            summary = violations_summary(index)
            (out_dir / "violations.log").write_text(summary, encoding="utf-8")
            log.error(
                "step08.violations",
                count=len(index.violations),
                framework=framework,
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[index_path, manifest_path, out_dir / "violations.log"],
                error=f"non-negotiable rule violations: {len(index.violations)}",
                notes=summary[:500],
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
                    f"indexer found 0 worca_*-prefixed test functions under "
                    f"{sut_root} (support files: {len(index.support_files)}, "
                    f"total generated: {len(produced_in_sut)}). The agent "
                    f"may have written only locator/page-object scaffolding. "
                    f"Inspect the worca_ files listed in generated-files.json."
                ),
            )

        # JIT runtime files were already vendored before the agent ran
        # (see the pre-vendoring block at the top of `run`). Make sure they
        # are present in `produced_in_sut` so the commit manifest below
        # records them alongside the agent's authored files — the rglob
        # walk above catches `worca_t_runtime.py` and `WorcaT.java` but
        # MAY miss `worca-t-runtime.js` (hyphen vs underscore in the
        # `worca_*` glob), so re-add explicitly to be safe. Set-based
        # dedup via resolve() avoids double-entries on Windows.
        already = {p.resolve() for p in produced_in_sut}
        for p in jit_files_added:
            if p.resolve() not in already:
                produced_in_sut.append(p)

        # Commit the agent's work to the worca-t branch. Per-step commits
        # give the human reviewer a clear `git log` trail of who-wrote-what.
        sha = commit_step(
            sut_root, self.number, self.name,
            message_detail=f"{len(produced_in_sut)} files, {len(index.tests)} tests",
        )

        total_tbd = (
            sum(len(t.tbd_markers) for t in index.tests)
            + sum(len(s.tbd_markers) for s in index.support_files)
        )
        notes = (
            f"framework={framework} files={len(index.files)} "
            f"tests={len(index.tests)} "
            f"support_files={len(index.support_files)} tbd={total_tbd}"
        )
        if sha:
            notes += f" commit={sha}"
        if not ok_schema:
            notes += f"; schema_warning={schema_err}"
        return StepResult(
            success=True,
            status="completed" if ok_schema else "warned",
            outputs=[index_path, manifest_path],
            notes=notes,
        )
