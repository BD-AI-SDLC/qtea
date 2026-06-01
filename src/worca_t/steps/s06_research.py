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

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.md_parser import extract_bullets, parse_markdown, section_to_dict
from worca_t.proxy import with_proxy_env
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult

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


def _materialize_sut(src: str, dst: Path) -> None:
    if _is_git_url(src):
        if dst.exists():
            _rmtree_safe(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        log.info("sut.clone", url=src, dst=str(dst))
        subprocess.run(
            ["git", "clone", "--depth=1", src, str(dst)],
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
            if "node_modules" in src_file.parts or ".git" in src_file.parts:
                continue
            try:
                text = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in _ENV_REF_PATTERNS:
                for m in pat.finditer(text):
                    keys.add(m.group(m.lastindex))

    return sorted(k for k in keys
                  if not any(k.startswith(p) for p in _INTERNAL_PREFIXES))


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
                _materialize_sut(ctx.sut_source, ctx.workspace.sut)
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

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "polyglot-test-researcher.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in ("stack-catalog",):
            sp = skills_root / skill
            if sp.exists():
                extras.append(sp)
        # Stage the SUT next to the agent so it can grep/read it.
        extras.append(ctx.workspace.sut)

        result = await run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=(
                "Follow the procedure in `./polyglot-test-researcher.prompt.md`. "
                "The repository under test is in `./sut/`. A pre-computed "
                "deterministic scan is at `./scan.txt` — read it first to seed "
                "your discovery. Produce the Discovery Summary at "
                "`./research.md` with explicit Build, Test, and Lint commands "
                "and a clearly labelled detected stack."
            ),
            extra_paths=extras,
            timeout_s=self.timeout_s,
            step=6,
            max_turns=25,
            claude_md=claude_md if claude_md.exists() else None,
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

        sut_env_keys = _discover_sut_env_keys(ctx.workspace.sut)
        if sut_env_keys:
            log.info("step06.sut_env_keys", count=len(sut_env_keys), keys=sut_env_keys)

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
            )
            env_resolution_audit = {
                "resolved": list(resolved.values.keys()),
                "sources": resolved.sources,
                "missing_required": resolved.missing_required,
                "missing_optional": resolved.missing_optional,
            }

        projection = _project_research(
            md_dst.read_text(encoding="utf-8"), scan_text, sut_env_keys=sut_env_keys,
        )
        if env_resolution_audit is not None:
            projection["env_resolution"] = env_resolution_audit
        json_dst = out_dir / "research.json"
        json_dst.write_text(json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8")

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
