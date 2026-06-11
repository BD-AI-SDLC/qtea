"""Step 6: Repository discovery via polyglot-test-researcher.

- Materializes the SUT into workspace/sut/ (clone if remote, copy if local).
- Optionally pre-runs the bundled deterministic scan.py.
- Invokes the researcher agent against the SUT.
- Parses its Discovery Summary into research.json (best-effort projection).

Outputs (artifacts/step06/):
  - research.md     (full agent narrative)
  - research.json   (structured projection - guaranteed keys: detected_stack, commands)
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from worca_t._sut_git import ensure_git_repo_and_branch
from worca_t.claude_runner import run_agent
from worca_t.config import SECRET_ENV_KEYS, package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.md_parser import extract_bullets, parse_markdown, section_to_dict
from worca_t.proxy import with_proxy_env
from worca_t.schemas import is_valid
from worca_t.stack_profile import detect_stack_profile
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.sut_inventory import (
    detect_sut_inventory,
    merge_llm_inventory,
    parse_llm_inventory_yaml,
    resolve_active_module,
)
from worca_t.url_resolver import detect_qa_base_url

log = get_logger(__name__)


_GIT_HOSTS = (
    "github.com", "gitlab", "bitbucket.org",
    "dev.azure.com", "ssh.dev.azure.com", "visualstudio.com",
    "codeberg.org", "gitea.", "sr.ht",
)


def _is_git_url(s: str) -> bool:
    if not s.startswith(("git@", "ssh://", "http://", "https://")):
        return False
    if s.endswith(".git"):
        return True
    return any(host in s for host in _GIT_HOSTS)


def _rmtree_safe(path: Path) -> None:
    """shutil.rmtree with a Windows readonly/lock error handler."""

    def _on_error(_func, _path, exc_info):  # noqa: ANN001
        import stat

        try:
            os.chmod(_path, stat.S_IWRITE)
            os.unlink(_path)
        except Exception:
            pass

    shutil.rmtree(path, onerror=_on_error)


def _materialize_sut(src: str, dst: Path, *, run_id: str) -> None:
    """Bring the SUT onto disk at ``dst`` and put it on the worca-t branch.

    Three sources:
      - Git URL → ``git clone --depth=1``. Already a git repo afterwards.
      - Local directory → ``shutil.copytree(..., ignore=patterns('.git'))``
        which strips the source's ``.git/`` (so worca-t never writes back
        into the user's actual repo) and produces a fresh non-git tree.
      - Local file → ``shutil.copy2`` (rare; degenerate single-file SUT).

    After materialization, ``ensure_git_repo_and_branch`` runs to:
      - ``git init`` + baseline commit on non-git copies, and
      - force-create ``worca-t/run-<run_id>`` so every downstream step has
        a writable, isolated branch to commit into.

    The branch is the deliverable: a human reviews it via ``git diff`` or
    a PR; nothing worca-t writes ever touches the upstream's ``main``.
    """
    if _is_git_url(src):
        if dst.exists():
            _rmtree_safe(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        log.info("sut.clone", url=src, dst=str(dst))
        subprocess.run(
            ["git", "clone", "--depth=1", "--", src, str(dst)],
            check=True,
            capture_output=True,
            env=with_proxy_env(),
            timeout=300,
        )
    else:
        p = Path(src).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"sut path not found: {p}")
        if dst.exists():
            _rmtree_safe(dst)
        if p.is_dir():
            shutil.copytree(p, dst, ignore=shutil.ignore_patterns(".git"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

    # Single-file SUT: nothing meaningful to branch from. Skip git setup —
    # downstream steps would fail anyway and the preflight in pipeline.py
    # surfaces the message clearly.
    if dst.is_file():
        return
    ensure_git_repo_and_branch(dst, run_id)


def _run_scan_skill(sut: Path, out_path: Path) -> bool:
    """Run skills/acquire-codebase-knowledge/scripts/scan.py against the SUT.

    The script is invoked with ``cwd=sut`` because it scans ``Path.cwd()`` and
    accepts only ``--output`` (no positional target directory). Stdout of the
    script is informational only — the real payload is written to ``out_path``
    by the script itself.
    """
    skill_root = package_resource_root() / "skills" / "acquire-codebase-knowledge"
    script = skill_root / "scripts" / "scan.py"
    if not script.exists():
        log.warning("scan.skill_missing", path=str(script))
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--output", str(out_path)],
            cwd=str(sut),
            capture_output=True,
            text=True,
            timeout=300,
            env=with_proxy_env(),
            check=False,
        )
        if result.returncode != 0:
            (out_path.parent / "scan.stderr.log").write_text(
                result.stderr or "", encoding="utf-8"
            )
            stderr_preview = (result.stderr or "").strip().splitlines()[:3]
            log.warning(
                "scan.skill_failed",
                returncode=result.returncode,
                stderr_preview=stderr_preview,
            )
            return False
        return True
    except Exception as e:
        log.warning("scan.failed", error=str(e))
        (out_path.parent / "scan.stderr.log").write_text(str(e), encoding="utf-8")
        return False


_ENV_TEMPLATE_FILES = (
    ".env.example", ".env.template", ".env.sample",
    ".env.local", ".env.test", ".env.development", ".env.production",
)

_CYPRESS_ENV_FILES = ("cypress.env.json",)

_JAVA_PROPS_FILES = (
    "application.properties", "application-test.properties",
    "src/main/resources/application.properties",
    "src/test/resources/application-test.properties",
)

_JAVA_PROP_KEY = re.compile(r"^([A-Z][A-Z0-9_.]{1,80})\s*=", re.MULTILINE)

_ENV_KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]{1,80})=", re.MULTILINE)

_ENV_REF_PATTERNS = [
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]{1,80})"),
    re.compile(r"process\.env\[(['\"])([A-Z][A-Z0-9_]{1,80})\1\]"),
    re.compile(r"os\.environ(?:\.get)?\(\s*['\"]([A-Z][A-Z0-9_]{1,80})['\"]"),
    re.compile(r"os\.environ\[['\"]([A-Z][A-Z0-9_]{1,80})['\"]\]"),
    re.compile(r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]{1,80})['\"]"),
    re.compile(r"System\.getenv\(\s*['\"]([A-Z][A-Z0-9_]{1,80})['\"]"),
    re.compile(r"ENV\[(['\"])([A-Z][A-Z0-9_]{1,80})\1\]"),
    re.compile(r"ENV\.fetch\(\s*['\"]([A-Z][A-Z0-9_]{1,80})['\"]"),
]

_SUT_SOURCE_GLOBS = ("**/*.ts", "**/*.js", "**/*.tsx", "**/*.jsx",
                     "**/*.py", "**/*.java", "**/*.rb", "**/*.cs")

_INTERNAL_PREFIXES = ("WORCA_T_", "ANTHROPIC_", "CLAUDE", "NODE_", "npm_",
                      "PATH", "HOME", "USER", "SHELL", "TERM", "LANG",
                      "HOSTNAME", "PWD", "OLDPWD", "SHLVL", "TMPDIR")

# Pydantic BaseSettings discovery — keep AST work to files that mention it.
_BASESETTINGS_HINT = re.compile(rb"\bBaseSettings\b")
_ENV_KEY_FINAL = re.compile(r"^[A-Z][A-Z0-9_]{1,80}$")


def _literal_str(node: ast.AST | None) -> str | None:
    """Return the str value if node is a Constant str literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_basesettings_base(base: ast.expr) -> bool:
    """True when a class base resolves to a name ending in 'BaseSettings'."""
    if isinstance(base, ast.Name):
        return base.id == "BaseSettings"
    if isinstance(base, ast.Attribute):
        return base.attr == "BaseSettings"
    return False


def _extract_env_prefix(class_body: list[ast.stmt]) -> str:
    """Pull env_prefix from a nested `class Config:` or `model_config = SettingsConfigDict(...)`.

    Non-literal values fall back to an empty prefix. Only string literals supported.
    """
    for stmt in class_body:
        # Nested `class Config: env_prefix = "..."`
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for sub in stmt.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and sub.targets[0].id == "env_prefix"
                ):
                    lit = _literal_str(sub.value)
                    if lit is not None:
                        return lit
        # `model_config = SettingsConfigDict(env_prefix="APP_")`
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "model_config"
            and isinstance(stmt.value, ast.Call)
        ):
            for kw in stmt.value.keywords:
                if kw.arg == "env_prefix":
                    lit = _literal_str(kw.value)
                    if lit is not None:
                        return lit
    return ""


def _parse_field_call(call: ast.Call) -> tuple[str | None, bool]:
    """Inspect a `Field(...)` call. Returns (alias_or_None, is_required).

    Required = first positional arg is `...` (Ellipsis) AND no default kw.
    Otherwise optional. Alias is taken from `alias=` keyword if literal string.
    Non-literal alias (e.g. variable reference) returns None so the caller can
    fall back to the field-name-uppercased convention.
    """
    alias: str | None = None
    has_default = False
    first_positional_is_ellipsis = False

    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and first.value is Ellipsis:
            first_positional_is_ellipsis = True
        else:
            # Positional default — `Field("foo")` means default="foo".
            has_default = True

    for kw in call.keywords:
        if kw.arg in ("default", "default_factory"):
            # Treat `default=...` (literal Ellipsis) as still required.
            if kw.arg == "default" and isinstance(kw.value, ast.Constant) and kw.value.value is Ellipsis:
                continue
            has_default = True
        elif kw.arg == "alias":
            lit = _literal_str(kw.value)
            if lit is not None:
                alias = lit

    is_required = first_positional_is_ellipsis and not has_default
    return alias, is_required


def _annotation_is_optional(ann: ast.AST | None) -> bool:
    """True for `Optional[X]`, `X | None`, or `None | X` annotations."""
    if ann is None:
        return False
    # `Optional[X]`
    if isinstance(ann, ast.Subscript):
        value = ann.value
        if isinstance(value, ast.Name) and value.id == "Optional":
            return True
        if isinstance(value, ast.Attribute) and value.attr == "Optional":
            return True
    # `X | None` / `None | X`
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            if isinstance(side, ast.Constant) and side.value is None:
                return True
            if isinstance(side, ast.Name) and side.id == "None":
                return True
    return False


def _discover_pydantic_env_keys(sut_path: Path) -> tuple[set[str], set[str]]:
    """Scan SUT Python source for Pydantic BaseSettings field declarations.

    Returns ``(required_keys, optional_keys)`` — env var names that the SUT
    reads implicitly through ``BaseSettings`` field bindings rather than
    explicit ``os.environ.get`` calls.

    Implementation notes:
      - Prefilter on raw bytes (cheap) so we only ``ast.parse`` files that
        actually mention ``BaseSettings``.
      - For each class inheriting BaseSettings, extract ``env_prefix`` from
        ``class Config`` or ``model_config = SettingsConfigDict(...)``.
      - For each annotated assignment, derive the env var name from
        ``Field(alias="X")`` if present, otherwise ``(env_prefix + field_name).upper()``.
      - Required iff ``Field(...)`` with bare ellipsis and no default AND
        annotation is not ``Optional`` AND no value is set on the AnnAssign.
    """
    required: set[str] = set()
    optional: set[str] = set()

    for src in sut_path.glob("**/*.py"):
        if not src.is_file():
            continue
        if src.stat().st_size > 512_000:
            continue
        if any(part in (".git", "node_modules", ".venv", "venv", "__pycache__")
               for part in src.parts):
            continue
        try:
            raw = src.read_bytes()
        except OSError:
            continue
        if not _BASESETTINGS_HINT.search(raw):
            continue
        try:
            tree = ast.parse(raw)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(_is_basesettings_base(b) for b in node.bases):
                continue

            env_prefix = _extract_env_prefix(node.body)

            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                field_name = stmt.target.id
                # Skip pydantic internals.
                if field_name in ("model_config", "Config"):
                    continue

                alias: str | None = None
                is_required_field = False
                rhs = stmt.value

                if isinstance(rhs, ast.Call):
                    func = rhs.func
                    is_field_call = (
                        (isinstance(func, ast.Name) and func.id == "Field")
                        or (isinstance(func, ast.Attribute) and func.attr == "Field")
                    )
                    if is_field_call:
                        alias, is_required_field = _parse_field_call(rhs)
                    else:
                        # Non-Field call value — treat as having a default.
                        is_required_field = False
                else:
                    # Bare annotation `qa_url: str` (no value) → required if
                    # not Optional. `qa_url: str = "x"` → optional (has value).
                    if rhs is None:
                        is_required_field = not _annotation_is_optional(stmt.annotation)

                # Optional annotation always overrides required classification.
                if _annotation_is_optional(stmt.annotation):
                    is_required_field = False

                env_key = alias if alias else (env_prefix + field_name).upper()
                if not _ENV_KEY_FINAL.match(env_key):
                    continue

                (required if is_required_field else optional).add(env_key)

    # Required wins if a key appears in both via different declarations.
    optional -= required
    return required, optional


def _discover_sut_env_keys(sut_path: Path) -> list[str]:
    """Scan the SUT for env var key names. Returns names only, never values."""
    keys: set[str] = set()

    # dotenv-style files (.env.example, .env.local, .env.test, etc.)
    for name in _ENV_TEMPLATE_FILES:
        candidate = sut_path / name
        if candidate.exists():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
                for m in _ENV_KEY_LINE.finditer(text):
                    keys.add(m.group(1))
            except OSError:
                pass

    # Cypress: cypress.env.json (top-level JSON keys)
    for name in _CYPRESS_ENV_FILES:
        candidate = sut_path / name
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    keys.update(k for k in data if isinstance(k, str) and k == k.upper())
            except (OSError, json.JSONDecodeError):
                pass

    # Java/Spring: application.properties / application-test.properties
    for name in _JAVA_PROPS_FILES:
        candidate = sut_path / name
        if candidate.exists():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
                for m in _JAVA_PROP_KEY.finditer(text):
                    key = m.group(1).replace(".", "_").upper()
                    keys.add(key)
            except OSError:
                pass

    # Source code scan for env var references
    for glob_pat in _SUT_SOURCE_GLOBS:
        for src_file in sut_path.glob(glob_pat):
            if not src_file.is_file() or src_file.stat().st_size > 512_000:
                continue
            # Skip vendored / installed third-party code. Without this we
            # descend into .venv/site-packages and harvest every os.getenv
            # call in pytest/playwright/allure/etc. plugin code — turning a
            # 10-key SUT into a 150+-key prompt. Matches the exclusion set
            # in _discover_pydantic_env_keys (above) so both scanners agree
            # on what counts as "the SUT".
            if any(part in (".git", "node_modules", ".venv", "venv",
                            "__pycache__", "site-packages", "dist-packages",
                            "vendor", "target", "build", "dist")
                   for part in src_file.parts):
                continue
            try:
                text = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in _ENV_REF_PATTERNS:
                for m in pat.finditer(text):
                    keys.add(m.group(m.lastindex))

    # Pydantic BaseSettings field declarations — implicit env-var bindings
    # that the regex source-scan above cannot detect.
    pyd_required, pyd_optional = _discover_pydantic_env_keys(sut_path)
    keys.update(pyd_required)
    keys.update(pyd_optional)

    return sorted(k for k in keys
                  if k not in SECRET_ENV_KEYS
                  and not any(k.startswith(p) for p in _INTERNAL_PREFIXES))


_FRAMEWORK_HINTS = (
    ("playwright-ts", re.compile(r"@playwright/test", re.I)),
    ("playwright-py", re.compile(r"\bplaywright\b", re.I)),
    ("pytest", re.compile(r"\bpytest\b", re.I)),
    ("jest", re.compile(r"\bjest\b", re.I)),
    ("cypress", re.compile(r"\bcypress\b", re.I)),
    ("selenium-java", re.compile(r"selenium.*java|java.*selenium", re.I)),
    ("robot", re.compile(r"\brobot framework\b|robotframework", re.I)),
    ("vitest", re.compile(r"\bvitest\b", re.I)),
    ("mocha", re.compile(r"\bmocha\b", re.I)),
)

_COMMAND_HINTS = {
    "test": re.compile(r"(?:Test|run tests?)\s*[:=]\s*`?([^`\n]+)`?", re.I),
    "build": re.compile(r"Build\s*[:=]\s*`?([^`\n]+)`?", re.I),
    "lint": re.compile(r"Lint\s*[:=]\s*`?([^`\n]+)`?", re.I),
}


def _detect_stack(md_text: str) -> str | None:
    for label, pat in _FRAMEWORK_HINTS:
        if pat.search(md_text):
            return label
    return None


def _extract_commands(md_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, pat in _COMMAND_HINTS.items():
        m = pat.search(md_text)
        if m:
            out[key] = m.group(1).strip().strip("`")
    return out


def _project_research(
    md_text: str, scan_text: str | None, *, sut_env_keys: list[str] | None = None,
) -> dict:
    root = parse_markdown(md_text)
    title = root.children[0].title if root.children else "research"
    commands = _extract_commands(md_text)
    projection: dict = {
        "title": title,
        "detected_stack": _detect_stack(md_text + ("\n" + (scan_text or ""))),
        "commands": commands,
        "summary_bullets": extract_bullets(root.content),
        "sections": [section_to_dict(c) for c in root.children],
    }
    if sut_env_keys:
        projection["sut_env_keys"] = sut_env_keys
    return projection


class ResearchStep(Step):
    number = 6
    name = "research"
    timeout_s = step_timeout(6)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        # Materialize SUT (skip if already done by pipeline early-clone).
        sut_ready = ctx.workspace.sut.exists() and any(ctx.workspace.sut.iterdir())
        if not sut_ready:
            try:
                _materialize_sut(
                    ctx.sut_source,
                    ctx.workspace.sut,
                    run_id=ctx.workspace.run_id,
                )
            except Exception as e:
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[],
                    error=f"sut materialize: {e}",
                )

        # Pre-run scan skill. Deterministic Python — if it fails it's a bug,
        # not a flake. Hard-fail the step before burning an LLM call on an
        # agent that would be working without its primary discovery seed.
        scan_out = wd / "scan.txt"
        if not _run_scan_skill(ctx.workspace.sut, scan_out):
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="scan skill failed; see step-06/scan.stderr.log",
            )
        scan_text = scan_out.read_text(encoding="utf-8") if scan_out.exists() else None

        # Deterministic toolchain + URL discovery. Run before the agent so its
        # prompt can reference the pre-computed artifacts; the agent's role
        # narrows from "infer the package manager" to "refine if the lockfile
        # signal is misleading."
        stack_profile = detect_stack_profile(ctx.workspace.sut)
        url_resolution = detect_qa_base_url(ctx.workspace.sut)

        # Best-effort: read the refined spec from Step 2's output to seed the
        # active-module auto-detect heuristic. None on first-time monorepo
        # runs without --module — resolve_active_module will surface a clear
        # error and Step 6 will fail with that message.
        refined_spec_text: str | None = None
        refined_spec_path = ctx.workspace.step_dir(2) / "refined-spec.md"
        if refined_spec_path.exists():
            try:
                refined_spec_text = refined_spec_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                refined_spec_text = None

        sut_inventory = detect_sut_inventory(
            ctx.workspace.sut,
            module_hint=getattr(ctx.options, "module", None),
            spec_text=refined_spec_text,
        )

        stack_profile_path = wd / "stack_profile.json"
        url_resolution_path = wd / "url_resolution.json"
        sut_inventory_path = wd / "sut_inventory.json"
        stack_profile_path.write_text(
            json.dumps(stack_profile.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        url_resolution_path.write_text(
            json.dumps(url_resolution.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        sut_inventory_path.write_text(
            json.dumps(sut_inventory.as_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Also stage an `active_module.json` for the codegen / locator-resolution
        # agents that consume the inventory but want a single-fact pointer.
        active = sut_inventory.active()
        if active is not None:
            (wd / "active_module.json").write_text(
                json.dumps({"name": active.name, "path": active.path,
                            "language": active.language,
                            "package_manager": active.package_manager}, indent=2),
                encoding="utf-8",
            )

        log.info(
            "step06.stack_profile",
            package_manager=stack_profile.package_manager,
            wrapper=stack_profile.wrapper_prefix,
            language=stack_profile.language,
        )
        log.info(
            "step06.url_resolution",
            key=url_resolution.key,
            source=url_resolution.source,
            confidence=url_resolution.confidence,
        )
        log.info(
            "step06.sut_inventory",
            is_monorepo=sut_inventory.is_monorepo,
            modules=[m.name for m in sut_inventory.modules],
            active_module=sut_inventory.active_module,
            page_object_count=sum(len(m.existing_page_objects) for m in sut_inventory.modules),
        )

        # Fail-fast when no active module could be resolved (monorepo + no
        # --module + no clear auto-detect winner). The notes carry the error
        # message produced by `resolve_active_module`.
        if sut_inventory.modules and sut_inventory.active_module is None:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "active module unresolved; "
                    + (sut_inventory.notes[-1] if sut_inventory.notes else "pass --module <name>")
                ),
            )

        # Default model is Haiku (agent_models.yaml). For languages NOT covered
        # by the deterministic tiers (Python AST / TS-JS regex), the researcher
        # must generate the full SUT Inventory YAML block from scratch — upgrade
        # to Sonnet for that heavier task.
        _HAIKU_SUFFICIENT_LANGUAGES = {"python", "typescript", "javascript"}
        researcher_model: str | None = (
            None  # Haiku from agent_models.yaml — deterministic tiers handle inventory
            if (stack_profile.language or "") in _HAIKU_SUFFICIENT_LANGUAGES
            else "claude-sonnet-4-6"  # override UP for languages without deterministic coverage
        )

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "polyglot-test-researcher.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in ("stack-catalog",):
            sp = skills_root / skill
            if sp.exists():
                extras.append(sp)

        # Read-only access to the canonical SUT clone — no copy. Before this,
        # `extras.append(ctx.workspace.sut)` triggered a full `shutil.copytree`
        # inside `_stage_resources`, producing a redundant `<workspace>/step-06/sut/`
        # duplicate on every run.
        sut_abs = ctx.workspace.sut.resolve()

        result = await run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=(
                f"Follow the procedure in `./polyglot-test-researcher.prompt.md`. "
                f"The repository under test is at the absolute path "
                f"`{sut_abs}` — read files there directly (no copy is staged "
                f"under the working directory). A pre-computed deterministic "
                f"scan is at `./scan.txt` — read it first to seed your "
                f"discovery. Three more pre-computed artifacts are also "
                f"authoritative: `./stack_profile.json` (package manager, "
                f"wrapper prefix, install command), `./url_resolution.json` "
                f"(canonical QA URL key + value), and `./sut_inventory.json` "
                f"(per-module test directory layout, existing page objects, "
                f"helpers, fixtures, auth flow). Echo their values in the "
                f"Discovery Summary — only override a field when you have "
                f"concrete evidence (README/CI text) that contradicts the "
                f"deterministic detection. For any field in `sut_inventory.json` "
                f"that is empty for a non-Python / non-TypeScript module, emit "
                f"a fenced ```yaml block whose top-level key is "
                f"`sut_inventory_module:` and whose body matches the template "
                f"in `./polyglot-test-researcher.prompt.md`. Produce the "
                f"Discovery Summary at `./research.md` with explicit Build, "
                f"Test, and Lint commands and a clearly labelled detected stack."
            ),
            extra_paths=extras,
            add_dirs=[sut_abs],
            timeout_s=self.timeout_s,
            step=6,
            max_turns=25,
            model=researcher_model,
            claude_md=claude_md if claude_md.exists() else None,
        )
        log.info(
            "step06.researcher_model",
            language=stack_profile.language,
            model=researcher_model or "agent_models.yaml default",
        )

        produced = wd / "research.md"
        if not result.success or not produced.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "research.md not produced",
            )

        md_dst = out_dir / "research.md"
        shutil.copy2(produced, md_dst)

        # Tier 3 merge: parse `## SUT Inventory` YAML blocks from research.md
        # and per-field-merge into the deterministic inventory (existing values
        # win; LLM fills gaps). After merge, re-resolve the active module in
        # case the LLM revealed modules we couldn't enumerate deterministically.
        llm_blocks = parse_llm_inventory_yaml(md_dst.read_text(encoding="utf-8"))
        if llm_blocks:
            for block in llm_blocks:
                module_name = str(block.get("name", "")).strip()
                if not module_name:
                    continue
                existing = sut_inventory.module_by_name(module_name)
                if existing is None:
                    # New module the deterministic tier didn't see (e.g. Java
                    # module in a Java+Python mono). Synthesize a stub.
                    from worca_t.sut_inventory import ModuleInventory  # local import to avoid cycle
                    stub = ModuleInventory(
                        name=module_name,
                        path=str(block.get("path", ".")),
                        source="llm_only",
                    )
                    merged = merge_llm_inventory(stub, block)
                    sut_inventory.modules.append(merged)
                else:
                    idx = sut_inventory.modules.index(existing)
                    sut_inventory.modules[idx] = merge_llm_inventory(existing, block)
            # Re-resolve active module after merge.
            active_name, _err = resolve_active_module(
                sut_inventory,
                explicit=getattr(ctx.options, "module", None),
                spec_text=refined_spec_text,
            )
            if active_name:
                sut_inventory.active_module = active_name
            # Re-persist sut_inventory.json with merged content.
            sut_inventory_path.write_text(
                json.dumps(sut_inventory.as_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info(
                "step06.sut_inventory_merged",
                blocks=len(llm_blocks),
                modules=[m.name for m in sut_inventory.modules],
            )

        sut_env_keys = _discover_sut_env_keys(ctx.workspace.sut)
        if sut_env_keys:
            log.info("step06.sut_env_keys", count=len(sut_env_keys), keys=sut_env_keys)

        # Pydantic BaseSettings required fields are as authoritative as
        # `.env.example` membership for classifying a key as required. Pass
        # the required set as `extra_required` so HITL prompts for them.
        pyd_required, _pyd_optional = _discover_pydantic_env_keys(ctx.workspace.sut)

        # Make sure the canonical URL key surfaced by `url_resolver` is in
        # the discovered set so the resolver cascade attempts to resolve it.
        if url_resolution.key and url_resolution.key not in sut_env_keys:
            sut_env_keys = sorted(set(sut_env_keys) | {url_resolution.key})

        # If url_resolver recovered a literal default value, seed os.environ
        # so ProcessEnvStrategy picks it up first (lowest-confidence values
        # still get overridden by an explicit .env file via DotenvFileStrategy).
        if url_resolution.key and url_resolution.value and not os.environ.get(url_resolution.key):
            os.environ[url_resolution.key] = url_resolution.value
            log.info(
                "step06.seed_url_default",
                key=url_resolution.key,
                source=url_resolution.source,
            )

        # Resolve SUT env vars via multi-strategy cascade.
        env_resolution_audit: dict | None = None
        if sut_env_keys:
            from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

            resolver_config = EnvResolverConfig(
                env_file=getattr(ctx.options, "env_file", None),
                sut_path=ctx.workspace.sut,
                no_hitl=getattr(ctx.options, "no_hitl", False),
                azdo_org=os.environ.get("AZDO_ORG"),
                azdo_project=os.environ.get("AZDO_PROJECT"),
                azdo_variable_group=os.environ.get("AZDO_VARIABLE_GROUP"),
                azdo_pat=os.environ.get("AZDO_PAT"),
            )
            resolved = resolve_sut_env(
                resolver_config, sut_env_keys, ctx.workspace.sut,
                extra_required=pyd_required,
            )
            env_resolution_audit = {
                "resolved": list(resolved.values.keys()),
                "sources": resolved.sources,
                "missing_required": resolved.missing_required,
                "missing_optional": resolved.missing_optional,
            }

            # Mirror the resolved canonical URL into SUT_BASE_URL so Step 8's
            # `os.environ.get("SUT_BASE_URL")` picks it up without the user
            # having to set SUT_BASE_URL directly. The QA-first invariant is
            # already encoded in `url_resolver.detect_qa_base_url`.
            chosen_key = url_resolution.key
            if (
                chosen_key
                and chosen_key in resolved.values
                and not os.environ.get("SUT_BASE_URL")
            ):
                os.environ["SUT_BASE_URL"] = resolved.values[chosen_key]
                log.info(
                    "step06.sut_base_url_mirrored",
                    from_key=chosen_key,
                    source=resolved.sources.get(chosen_key, "?"),
                )

        projection = _project_research(
            md_dst.read_text(encoding="utf-8"), scan_text, sut_env_keys=sut_env_keys,
        )
        projection["stack_profile"] = stack_profile.as_dict()
        projection["url_resolution"] = url_resolution.as_dict()
        projection["sut_inventory"] = sut_inventory.as_dict()
        if env_resolution_audit is not None:
            projection["env_resolution"] = env_resolution_audit

        # Cross-check test-folder imports vs declared deps. Surfaces gaps
        # (e.g. `import allure` with no `allure-pytest` in pyproject) so Step 8
        # can pre-install the known-safe ones before the first pytest run
        # instead of bailing at collection time.
        from worca_t.test_runner import audit_missing_deps
        dep_warnings = audit_missing_deps(
            ctx.workspace.sut, package_manager=stack_profile.package_manager,
        )
        if dep_warnings:
            projection["dependency_warnings"] = dep_warnings
            log.info(
                "step06.dependency_warnings",
                count=len(dep_warnings),
                known=sum(1 for w in dep_warnings if w["confidence"] == "known"),
                guessed=sum(1 for w in dep_warnings if w["confidence"] == "guessed"),
                modules=[w["module"] for w in dep_warnings],
            )
        json_dst = out_dir / "research.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

        # Persist the pre-computed artifacts next to research.json for
        # downstream consumers that load them independently.
        shutil.copy2(stack_profile_path, out_dir / "stack_profile.json")
        shutil.copy2(url_resolution_path, out_dir / "url_resolution.json")
        shutil.copy2(sut_inventory_path, out_dir / "sut_inventory.json")

        ok, err = is_valid(projection, "research")
        status = "completed" if ok else "warned"
        notes = f"detected_stack={projection['detected_stack']}"
        if not ok:
            notes += f"; schema_warning={err}"
            log.warning("step06.schema_invalid", error=err)

        return StepResult(
            success=True,
            status=status,
            outputs=[md_dst, json_dst],
            notes=notes,
        )


def replay_env_from_artifacts(workspace: Any, options: Any) -> bool:
    """Re-populate `os.environ` from existing Step 6 artifacts.

    Step 6's `resolve_sut_env()` call loads the SUT's `.env` file into
    `os.environ` and mirrors the canonical URL key (e.g. `QA_URL`) into
    `SUT_BASE_URL`. Those injections are **in-process only** — they vanish
    on process restart. When the user re-runs `worca-t run --from-step 7+`,
    Step 6 doesn't fire, so downstream steps that depend on `SUT_BASE_URL`
    (8, 9) see it as unset and abort or warn.

    This helper replays only the env-resolution slice of Step 6 (no LLM, no
    new discovery) by reading the persisted `research.json` and
    `url_resolution.json` and rerunning the same env resolver cascade.

    Returns True when at least one env var was re-injected, False otherwise
    (no artifacts on disk or nothing to resolve). Never raises; logs errors
    and continues.
    """
    # Use the read-only path helper here — replay runs at pipeline preflight
    # BEFORE Step 6 has had a chance to execute. Using `step_dir(6)` would
    # mkdir an empty `artifacts/step06/` folder on every fresh run even when
    # no prior research artifacts exist.
    research_json = workspace.step_dir_path(6) / "research.json"
    if not research_json.exists():
        return False

    try:
        research = json.loads(research_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("env_replay.research_unreadable", error=str(e))
        return False

    sut_env_keys = research.get("sut_env_keys") or []
    url_resolution = research.get("url_resolution") or {}
    url_key = url_resolution.get("key")
    if url_key and url_key not in sut_env_keys:
        sut_env_keys = list(sut_env_keys) + [url_key]
    if not sut_env_keys:
        return False

    # Skip keys already in process env to avoid clobbering values set by
    # the user's shell or by --env-file (which pipeline.py loaded already).
    needed = [k for k in sut_env_keys if not os.environ.get(k)]
    resolved_count = 0
    resolved_keys: list[str] = []
    resolved_sources: dict[str, str] = {}

    if needed:
        from worca_t.env_resolver import EnvResolverConfig, resolve_sut_env

        resolver_config = EnvResolverConfig(
            env_file=getattr(options, "env_file", None),
            sut_path=workspace.sut,
            no_hitl=True,  # never prompt during replay; Step 6 already did that
            azdo_org=os.environ.get("AZDO_ORG"),
            azdo_project=os.environ.get("AZDO_PROJECT"),
            azdo_variable_group=os.environ.get("AZDO_VARIABLE_GROUP"),
            azdo_pat=os.environ.get("AZDO_PAT"),
        )
        resolved = resolve_sut_env(resolver_config, needed, workspace.sut)
        if resolved.values:
            resolved_count = len(resolved.values)
            resolved_keys = list(resolved.values.keys())
            resolved_sources = dict(resolved.sources)
        else:
            log.info("env_replay.no_values_found", requested=needed)

    # Mirror the canonical URL key to SUT_BASE_URL whenever it ends up
    # available — whether already in process env (user's shell / --env-file
    # / pipeline.load_env) or newly resolved by the cascade above. This
    # MUST run as a final step regardless of `needed`/`resolved.values`,
    # because the common "URL already in env, optional keys absent" case
    # leaves `resolved.values` empty but SUT_BASE_URL still needs setting.
    mirrored = False
    if url_key and not os.environ.get("SUT_BASE_URL"):
        url_value = os.environ.get(url_key)
        source = "process_env"
        if not url_value:
            # Fallback: just-resolved value (should already be in os.environ
            # via env_resolver, but check explicitly for clarity).
            url_value = resolved_sources.get(url_key)
            source = resolved_sources.get(url_key, "?")
        if url_value:
            os.environ["SUT_BASE_URL"] = url_value
            mirrored = True
            log.info(
                "env_replay.sut_base_url_mirrored",
                from_key=url_key,
                source=source,
            )

    if resolved_count > 0 or mirrored:
        log.info(
            "env_replay.complete",
            replayed=resolved_keys,
            sources=resolved_sources,
            sut_base_url_mirrored=mirrored,
        )
        return True
    return False
