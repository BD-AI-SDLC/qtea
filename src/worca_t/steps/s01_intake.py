"""Step 1: Requirement intake.

Sources:
  - ``jira:KEY-123``                              → fetch via direct Jira REST,
    reformat as spec via the reasoning agent
  - ``https://*.atlassian.net/browse/KEY-123``    → same, base URL inferred
    from the URL host (overrides JIRA_BASE_URL)
  - ``https://rb-tracker.bosch.com/tracker01/browse/KEY-123`` → same, DC path
  - ``http(s)://...`` (other)                     → download markdown
  - file path / relative path                     → copy locally

Outputs (in ``artifacts/step01/``):
  - ``spec.md``       — normalized spec, downstream input
  - ``jira-spec.md``  — provenance stub (no downstream consumer, audit only)

Transport for the JIRA path: ``worca_t.jira_client.fetch_issue`` for the REST
fetch (replaces the Atlassian MCP) and
``worca_t.llm.reasoning.call_reasoning_llm`` for the reformat (replaces the
Agent SDK subprocess). Local-file and generic-URL paths stay pure-code.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx

from worca_t.config import package_resource_root, step_timeout
from worca_t.jira_client import (
    JiraFetchError,
    fetch_issue,
    normalize_description,
    parse_jira_spec_source,
)
from worca_t.llm.reasoning import call_reasoning_llm
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


def _slim_jira_payload(payload: dict) -> dict:
    """Return a payload trimmed to the fields the agent actually reads.

    Raw Jira issue payloads carry dozens of fields the spec template doesn't
    use (avatar URLs, customfield_* for arbitrary plugin state, schema
    metadata, etc.) — these are pure context-window noise. We keep:
      * top-level: key, self, id
      * fields: summary, status, priority, issuetype, assignee, reporter,
        created, updated, labels, components, fixVersions, issuelinks,
        description (already normalized to markdown by the orchestrator)
      * any field whose key starts with ``customfield_`` AND has a
        non-empty value (those often carry meaningful project-specific data)

    Rendered field bodies (``renderedFields``) are dropped — the
    description has already been normalized via ``normalize_description``.
    """
    slim: dict = {k: payload.get(k) for k in ("key", "self", "id") if k in payload}
    fields = payload.get("fields") or {}
    keep_fields = (
        "summary", "status", "priority", "issuetype", "assignee", "reporter",
        "created", "updated", "labels", "components", "fixVersions",
        "issuelinks",
    )
    slim_fields = {k: fields.get(k) for k in keep_fields if k in fields}
    for k, v in fields.items():
        if k.startswith("customfield_") and v not in (None, "", [], {}):
            slim_fields[k] = v
    slim["fields"] = slim_fields
    return slim


async def _jira_via_rest(
    ctx: StepContext,
    base_url: str,
    ticket_id: str,
    out_dir: Path,
    workdir: Path,
) -> Path:
    """Fetch a Jira ticket via direct REST + reformat via the reasoning agent."""
    try:
        payload = fetch_issue(base_url, ticket_id)
    except JiraFetchError as e:
        raise RuntimeError(f"jira fetch failed: {e}") from e

    # Normalize the description in-place: Cloud ADF → markdown, DC wiki
    # passthrough. The agent template treats description as already-markdown
    # to keep its instructions transport-agnostic.
    if "fields" in payload:
        payload["fields"]["description"] = normalize_description(payload)

    slim = _slim_jira_payload(payload)
    payload_json = json.dumps(slim, indent=2, ensure_ascii=False)

    agents_root = package_resource_root() / "agents"
    agent = agents_root / "jira-to-ai-spec.agent.md"

    user_prompt = (
        f"Reformat the Jira ticket payload below into the spec.md markdown "
        f"per your agent template. Use ticket key `{ticket_id}` as the "
        f"section-1 anchor. The description field has already been "
        f"normalized to markdown — preserve formatting, don't add code "
        f"fences around the spec body, no preamble. Return only the "
        f"spec.md markdown content."
    )

    result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=user_prompt,
        inputs={"jira-issue.json": payload_json},
        output_schema=None,  # markdown output, no schema enforcement
        timeout_s=step_timeout(1),
        step=1,
    )

    if not result.success or not result.final_text:
        raise RuntimeError(f"jira-to-ai-spec failed: {result.error or 'no output'}")

    dst = out_dir / "spec.md"
    dst.write_text(result.final_text, encoding="utf-8")
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
                await _jira_via_rest(ctx, base_url, ticket, out_dir, wd)
                jira_dst.write_text(
                    f"# {ticket}\n\n"
                    f"Source: {base_url}/browse/{ticket}\n\n"
                    f"Raw Jira capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif jira_parsed is not None:
                # Full URL form pointing at /browse/KEY — Cloud or DC.
                base_url, ticket = jira_parsed
                await _jira_via_rest(ctx, base_url, ticket, out_dir, wd)
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
