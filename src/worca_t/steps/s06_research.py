"""Step 6: Repository discovery via polyglot-test-researcher.

- Materializes the SUT into workspace/sut/ (clone if remote, copy if local).
- Optionally pre-runs the skill's deterministic scan.py.
- Invokes the researcher agent against the SUT.
- Parses its Discovery Summary into research.json (best-effort projection).

Outputs (artifacts/step06/):
  - research.md     (full agent narrative)
  - research.json   (structured projection - guaranteed keys: detected_stack, commands)
"""

from __future__ import annotations

import json
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


def _is_git_url(s: str) -> bool:
    if not s.startswith(("git@", "http://", "https://")):
        return False
    return s.endswith(".git") or "github.com" in s or "gitlab" in s


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
    """Run skills/acquire-codebase-knowledge/scripts/scan.py against the SUT."""
    skill_root = package_resource_root() / "skills" / "acquire-codebase-knowledge"
    script = skill_root / "scripts" / "scan.py"
    if not script.exists():
        log.warning("scan.skill_missing", path=str(script))
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(script), str(sut)],
            capture_output=True,
            text=True,
            timeout=300,
            env=with_proxy_env(),
            check=False,
        )
        out_path.write_text(result.stdout, encoding="utf-8")
        if result.returncode != 0:
            (out_path.parent / "scan.stderr.log").write_text(result.stderr or "", encoding="utf-8")
        return result.returncode == 0
    except Exception as e:
        log.warning("scan.failed", error=str(e))
        return False


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


def _project_research(md_text: str, scan_text: str | None) -> dict:
    root = parse_markdown(md_text)
    title = root.children[0].title if root.children else "research"
    commands = _extract_commands(md_text)
    return {
        "title": title,
        "detected_stack": _detect_stack(md_text + ("\n" + (scan_text or ""))),
        "commands": commands,
        "summary_bullets": extract_bullets(root.content),
        "sections": [section_to_dict(c) for c in root.children],
    }


class ResearchStep(Step):
    number = 6
    name = "research"
    timeout_s = step_timeout(6)

    def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        # Materialize SUT.
        try:
            _materialize_sut(ctx.sut_source, ctx.workspace.sut)
        except Exception as e:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"sut materialize: {e}",
            )

        # Pre-run scan skill (best-effort).
        scan_out = wd / "scan.txt"
        _run_scan_skill(ctx.workspace.sut, scan_out)
        scan_text = scan_out.read_text(encoding="utf-8") if scan_out.exists() else None

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "polyglot-test-researcher.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in ("acquire-codebase-knowledge", "context-map"):
            sp = skills_root / skill
            if sp.exists():
                extras.append(sp)
        # Stage the SUT next to the agent so it can grep/read it.
        extras.append(ctx.workspace.sut)

        result = run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=(
                "Discover the repository under `./sut/`. Produce a research "
                "document at `./research.md` following your prompt structure. "
                "Include explicit Build / Test / Lint commands and a clearly "
                "labelled detected stack."
            ),
            extra_paths=extras,
            timeout_s=self.timeout_s,
            step=6,
            max_turns=40,
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

        projection = _project_research(md_dst.read_text(encoding="utf-8"), scan_text)
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
