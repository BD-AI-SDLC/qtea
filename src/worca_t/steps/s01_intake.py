"""Step 1: Requirement intake.

Sources:
  - `jira:KEY-123`            -> invoke jira-to-ai-spec agent (Atlassian MCP)
  - http(s)://...              -> download markdown
  - file path / relative path  -> copy locally

Outputs (in artifacts/step01/):
  - spec.md       (normalized spec, downstream input)
  - jira-spec.md  (raw Jira capture, stub when not from Jira)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.proxy import with_proxy_env
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)


def _is_jira(src: str) -> bool:
    return src.lower().startswith("jira:")


def _is_url(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def _download(url: str, dst: Path) -> None:
    proxies_env = with_proxy_env()
    proxy = proxies_env.get("HTTPS_PROXY") or proxies_env.get("HTTP_PROXY")
    with httpx.Client(timeout=30.0, proxy=proxy, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        dst.write_text(r.text, encoding="utf-8")


def _copy_local(src: str, dst: Path) -> None:
    p = Path(src).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"spec file not found: {p}")
    shutil.copy2(p, dst)


async def _jira_via_agent(ctx: StepContext, ticket_id: str, out_dir: Path, workdir: Path) -> Path:
    agents_root = package_resource_root() / "agents"
    agent = agents_root / "jira-to-ai-spec.agent.md"
    claude_md = package_resource_root() / "CLAUDE.md"

    result = await run_agent(
        agent,
        workdir=workdir,
        inputs={},
        user_prompt=(
            f"Fetch Jira ticket `{ticket_id}` and produce a normalized markdown "
            f"spec following your 10-section structure. Write the spec to "
            f"`./spec.md` in this directory. Also write the raw Jira content to "
            f"`./jira-spec.md`."
        ),
        timeout_s=step_timeout(1),
        step=1,
        max_turns=10,
        claude_md=claude_md if claude_md.exists() else None,
    )
    if not result.success:
        raise RuntimeError(f"jira-to-ai-spec failed: {result.error or result.exit_code}")
    produced = workdir / "spec.md"
    if not produced.exists():
        raise FileNotFoundError(f"jira-to-ai-spec did not produce {produced}")
    dst = out_dir / "spec.md"
    shutil.copy2(produced, dst)
    if (workdir / "jira-spec.md").exists():
        shutil.copy2(workdir / "jira-spec.md", out_dir / "jira-spec.md")
    return dst


class IntakeStep(Step):
    number = 1
    name = "intake"
    timeout_s = step_timeout(1)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        spec_dst = out_dir / "spec.md"
        jira_dst = out_dir / "jira-spec.md"

        src = ctx.spec_source
        try:
            if _is_jira(src):
                ticket = src.split(":", 1)[1].strip()
                if not ticket:
                    raise ValueError("jira: source missing ticket id")
                await _jira_via_agent(ctx, ticket, out_dir, wd)
                if not jira_dst.exists():
                    jira_dst.write_text(
                        f"# {ticket}\n\n(raw Jira capture written by agent)\n",
                        encoding="utf-8",
                    )
            elif _is_url(src):
                _download(src, spec_dst)
                jira_dst.write_text(
                    f"# External source\n\nDownloaded from: {src}\n",
                    encoding="utf-8",
                )
            else:
                _copy_local(src, spec_dst)
                jira_dst.write_text(
                    f"# Local spec\n\nCopied from: {Path(src).resolve()}\n",
                    encoding="utf-8",
                )
        except Exception as e:
            log.error("step01.failed", error=str(e), source=src)
            return StepResult(success=False, status="failed", outputs=[], error=str(e))

        if not spec_dst.exists() or spec_dst.stat().st_size == 0:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="spec.md missing or empty after intake",
            )

        return StepResult(
            success=True,
            status="completed",
            outputs=[spec_dst, jira_dst],
            notes=f"intake source: {src}",
        )
