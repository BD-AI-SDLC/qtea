"""Step 1: Requirement intake.

Sources:
  - `jira:KEY-123`            -> invoke jira-to-ai-spec agent (Atlassian MCP)
  - https://*.atlassian.net/browse/KEY-123  (or any URL whose path matches
    `/browse/<KEY-NNN>`)      -> extract ticket key, route through Jira agent
  - http(s)://... (other)      -> download markdown
  - file path / relative path  -> copy locally

Outputs (in artifacts/step01/):
  - spec.md       (normalized spec, downstream input)
  - jira-spec.md  (provenance stub — no downstream consumer, kept for auditing)
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

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


_JIRA_BROWSE_RE = re.compile(r"/browse/([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


def _jira_ticket_from_url(src: str) -> str | None:
    """Return the ticket key if `src` is a Jira browse URL, else None.

    Matches the canonical permalink shape used by Atlassian Cloud and
    self-hosted Jira: a path ending in `/browse/<PROJECT>-<NUMBER>`.
    Query strings and fragments are ignored.
    """
    try:
        parsed = urlparse(src)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    m = _JIRA_BROWSE_RE.search(parsed.path)
    return m.group(1).upper() if m else None


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
            f"`./spec.md` in this directory. This is the ONLY file you need to "
            f"produce — do not write `jira-spec.md` or any other artifact.\n\n"
            f"SCOPE — strict: call `mcp__atlassian__jira_get_issue` for "
            f"`{ticket_id}` exactly once. Do NOT run `jira_search`, do NOT "
            f"fetch linked/sub-task/parent tickets, do NOT chase referenced "
            f"keys mentioned in the description or comments. Linked issues "
            f"belong in Section 5.1 as plain references, not as fetched data. "
            f"As soon as the main ticket payload is in hand, write `spec.md` "
            f"and finish."
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
            ticket_from_url = _jira_ticket_from_url(src) if _is_url(src) else None
            if _is_jira(src):
                ticket = src.split(":", 1)[1].strip()
                if not ticket:
                    raise ValueError("jira: source missing ticket id")
                await _jira_via_agent(ctx, ticket, out_dir, wd)
                jira_dst.write_text(
                    f"# {ticket}\n\n"
                    f"Raw Jira capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif ticket_from_url:
                await _jira_via_agent(ctx, ticket_from_url, out_dir, wd)
                jira_dst.write_text(
                    f"# {ticket_from_url}\n\n"
                    f"Source URL: {src}\n\n"
                    f"Raw Jira capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
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
