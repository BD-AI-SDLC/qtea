"""Step 1: Requirement intake.

Sources:
  - ``jira:KEY-123``                              → fetch via direct Jira REST,
    render deterministically as spec.md
  - ``https://*.atlassian.net/browse/KEY-123``    → same, base URL inferred
    from the URL host (overrides JIRA_BASE_URL)
  - ``https://rb-tracker.bosch.com/tracker01/browse/KEY-123`` → same, DC path
  - ``http(s)://...`` (other)                     → download markdown
  - file path / relative path                     → copy locally

Outputs (in ``artifacts/step01/``):
  - ``spec.md``       — normalized spec, downstream input
  - ``jira-spec.md``  — provenance stub (no downstream consumer, audit only)

Step 1 is now pure code (no LLM call). The JIRA path fetches via
:func:`worca_t.jira_client.fetch_issue` and formats via
:func:`worca_t.jira_client.format_payload_as_spec_md` — a deterministic
JSON-to-markdown renderer. All semantic structuring (requirement
extraction, AC derivation, edge-case identification) is handled
uniformly across source types by Step 2's ``refine-spec`` agent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from worca_t.config import step_timeout
from worca_t.jira_client import (
    JiraFetchError,
    fetch_issue,
    format_payload_as_spec_md,
    normalize_description,
    parse_jira_spec_source,
)
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


async def _jira_via_rest(
    ctx: StepContext,
    base_url: str,
    ticket_id: str,
    out_dir: Path,
    workdir: Path,
    *,
    source_url: str | None = None,
) -> Path:
    """Fetch a Jira ticket via direct REST + render deterministically as spec.md.

    No LLM call. The deterministic renderer in
    :func:`worca_t.jira_client.format_payload_as_spec_md` produces clean
    markdown that Step 2's ``refine-spec`` agent then refines uniformly
    alongside local-file and generic-URL specs.

    Parameters
    ----------
    ctx, base_url, ticket_id, out_dir, workdir:
        Standard intake arguments.
    source_url:
        Optional ``--spec``-form URL to record in the spec's provenance
        block. Pass for the URL-form path; ``None`` for the
        ``jira:KEY`` shorthand.
    """
    # workdir is accepted for parity with other intake helpers, but the
    # deterministic renderer doesn't need a scratchpad.
    del ctx, workdir

    try:
        payload = fetch_issue(base_url, ticket_id)
    except JiraFetchError as e:
        raise RuntimeError(f"jira fetch failed: {e}") from e

    # Normalize the description (Cloud ADF → markdown; DC wiki passthrough)
    # so the formatter sees a single text shape regardless of backend.
    if "fields" in payload:
        payload["fields"]["description"] = normalize_description(payload)

    spec_md = format_payload_as_spec_md(payload, source_url=source_url)
    dst = out_dir / "spec.md"
    dst.write_text(spec_md, encoding="utf-8")
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
            # Try parsing as a Jira reference first — handles both
            # ``jira:KEY`` and full ``https://.../browse/KEY`` URLs.
            jira_parsed = parse_jira_spec_source(src)

            if _is_jira(src):
                # ``jira:KEY`` shorthand: parse_jira_spec_source returns
                # None when JIRA_BASE_URL is unset, so re-check explicitly
                # for a clearer error.
                ticket = src.split(":", 1)[1].strip().upper()
                if not ticket:
                    raise ValueError("jira: source missing ticket id")
                if jira_parsed is None:
                    raise ValueError(
                        "jira:KEY shorthand requires JIRA_BASE_URL env var "
                        "(or pass the full URL form: "
                        "https://<tenant>.atlassian.net/browse/<KEY>)"
                    )
                base_url, _ = jira_parsed
                source_url = f"{base_url}/browse/{ticket}"
                await _jira_via_rest(
                    ctx, base_url, ticket, out_dir, wd, source_url=source_url
                )
                jira_dst.write_text(
                    f"# {ticket}\n\n"
                    f"Source: {source_url}\n\n"
                    f"Raw Jira capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif jira_parsed is not None:
                # Full URL form pointing at /browse/KEY — Cloud or DC.
                base_url, ticket = jira_parsed
                await _jira_via_rest(
                    ctx, base_url, ticket, out_dir, wd, source_url=src
                )
                jira_dst.write_text(
                    f"# {ticket}\n\n"
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
