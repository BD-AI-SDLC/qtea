"""Step 1: Requirement intake.

Behavior is **source-asymmetric**: only JIRA / Azure DevOps paths invoke the
LLM enrichment agent. Local files and generic URLs pass through as literal
copies / literal downloads. Step 2 (`refine-spec`) handles structural
refinement uniformly.

Sources:
  - ``jira:KEY-123``                              → fetch via direct Jira REST,
    inline the slim JSON payload, enrich via the ``ticket-to-ai-spec`` agent
  - ``https://*.atlassian.net/browse/KEY-123``    → same; base URL inferred
    from the URL host (overrides ``JIRA_BASE_URL``)
  - ``https://rb-tracker.bosch.com/tracker01/browse/KEY-123`` → same, DC path
  - ``ado:9370`` / ``ado:ORG/PROJECT/9370``       → fetch via Azure DevOps
    REST, enrich via the same ``ticket-to-ai-spec`` agent
  - ``https://dev.azure.com/{org}/{proj}/_workitems/edit/{id}`` → same
  - ``http(s)://...`` (other)                     → download the body and write
    it verbatim to ``spec.md`` (no agent call)
  - file path / relative path                     → read the file and write
    it verbatim to ``spec.md`` (no agent call)

Outputs (in ``artifacts/step01/``):
  - ``spec.md``       — JIRA/ADO: normalized 10-section spec (agent output).
                         Non-ticket: literal source content (passthrough).
  - ``jira-spec.md``  — provenance stub (no downstream consumer, audit only)

Transport: ``qtea.jira_client.fetch_issue`` / ``qtea.ado_client.fetch_work_item``
for the REST fetch and ``qtea.llm.reasoning.call_reasoning_llm`` (with the
``ticket-to-ai-spec`` agent) for ticket enrichment.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from qtea.ado_client import (
    AdoFetchError,
    fetch_work_item,
    normalize_description as ado_normalize_description,
    parse_ado_spec_source,
    slim_ado_payload,
)
from qtea.config import package_resource_root, step_timeout
from qtea.jira_client import (
    JiraFetchError,
    fetch_issue,
    normalize_description,
    parse_jira_spec_source,
)
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.proxy import with_proxy_env
from qtea.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)


def _is_jira(src: str) -> bool:
    return src.lower().startswith("jira:")


def _is_ado(src: str) -> bool:
    return src.lower().startswith("ado:")


def _is_url(src: str) -> bool:
    return src.lower().startswith(("http://", "https://"))


def _download_text(url: str) -> str:
    """Fetch a URL and return its body as text (uses corporate proxy env)."""
    proxies_env = with_proxy_env()
    proxy = proxies_env.get("HTTPS_PROXY") or proxies_env.get("HTTP_PROXY")
    with httpx.Client(timeout=30.0, proxy=proxy, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _read_local_text(src: str) -> str:
    """Read a local file and return its content as text."""
    p = Path(src).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"spec file not found: {p}")
    return p.read_text(encoding="utf-8")


def _slim_jira_payload(payload: dict) -> dict:
    """Trim a raw Jira payload to the fields the ticket-to-ai-spec agent needs.

    Raw Jira issue payloads carry dozens of fields the spec template doesn't
    use (avatar URLs, schema metadata, etc.) — these are pure context-window
    noise. We keep:
      * top-level: key, self, id
      * fields: summary, status, priority, issuetype, assignee, reporter,
        created, updated, labels, components, fixVersions, issuelinks,
        description (already normalized to markdown upstream)
      * any field whose key starts with ``customfield_`` AND has a
        non-empty value (those often carry meaningful project-specific data)
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


async def _enrich_jira_via_agent(
    *,
    workdir: Path,
    out_dir: Path,
    source_label: str,
    payload_json: str,
) -> Path:
    """Invoke the ``ticket-to-ai-spec`` agent on an inlined JIRA payload.

    Used ONLY for JIRA sources. Local files and generic URLs bypass this
    helper and write their content verbatim to ``spec.md``.

    Parameters
    ----------
    workdir, out_dir:
        Standard step paths.
    source_label:
        Human-readable provenance string the agent embeds in the spec's
        ``> Source:`` header line. Examples: ``"jira:MEAS-5490"`` or
        ``"https://bosch-pt.atlassian.net/browse/MEAS-5490"``.
    payload_json:
        JSON string of the slimmed JIRA issue payload.
    """
    agents_root = package_resource_root() / "agents"
    agent = agents_root / "ticket-to-ai-spec.agent.md"

    user_prompt = (
        f"Enrich the JIRA payload below into the spec.md markdown per "
        f"your agent template. Use `{source_label}` as the value of the "
        f"`Source:` provenance line in the header. Don't add code fences "
        f"around the spec body, no preamble. Return only the spec.md "
        f"markdown content."
    )

    result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=user_prompt,
        inputs={"jira-issue.json": payload_json},
        output_schema=None,
        timeout_s=step_timeout(1),
        step=1,
    )

    if not result.success or not result.final_text:
        raise RuntimeError(
            f"ticket-to-ai-spec failed: {result.error or 'no output'}"
        )

    dst = out_dir / "spec.md"
    dst.write_text(result.final_text, encoding="utf-8")
    return dst


async def _jira_via_rest(
    ctx: StepContext,
    base_url: str,
    ticket_id: str,
    out_dir: Path,
    workdir: Path,
) -> Path:
    """Fetch a Jira ticket via direct REST + enrich via the ticket-to-ai-spec agent."""
    del ctx  # accepted for signature parity with other intake helpers
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
    return await _enrich_jira_via_agent(
        workdir=workdir,
        out_dir=out_dir,
        source_label=f"{base_url}/browse/{ticket_id}",
        payload_json=json.dumps(slim, indent=2, ensure_ascii=False),
    )


async def _enrich_ado_via_agent(
    *,
    workdir: Path,
    out_dir: Path,
    source_label: str,
    payload_json: str,
) -> Path:
    """Invoke the ``ticket-to-ai-spec`` agent on an inlined Azure DevOps payload.

    Reuses the same agent as JIRA — the 10-section output template is
    source-agnostic. The input header ``ado-workitem.json`` tells the agent
    which field namespace to expect.
    """
    agents_root = package_resource_root() / "agents"
    agent = agents_root / "ticket-to-ai-spec.agent.md"

    user_prompt = (
        f"Enrich the Azure DevOps work item payload below into the spec.md "
        f"markdown per your agent template. Use `{source_label}` as the value "
        f"of the `Source:` provenance line in the header. Don't add code fences "
        f"around the spec body, no preamble. Return only the spec.md "
        f"markdown content."
    )

    result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=user_prompt,
        inputs={"ado-workitem.json": payload_json},
        output_schema=None,
        timeout_s=step_timeout(1),
        step=1,
    )

    if not result.success or not result.final_text:
        raise RuntimeError(
            f"ticket-to-ai-spec failed (ADO): {result.error or 'no output'}"
        )

    dst = out_dir / "spec.md"
    dst.write_text(result.final_text, encoding="utf-8")
    return dst


async def _ado_via_rest(
    ctx: StepContext,
    org: str,
    project: str,
    item_id: int,
    out_dir: Path,
    workdir: Path,
) -> Path:
    """Fetch an Azure DevOps work item via REST + enrich via the agent."""
    del ctx
    try:
        payload = fetch_work_item(org, project, item_id)
    except AdoFetchError as e:
        raise RuntimeError(f"ado fetch failed: {e}") from e

    if "fields" in payload:
        payload["fields"]["System.Description"] = ado_normalize_description(payload)

    slim = slim_ado_payload(payload)
    source_label = f"https://dev.azure.com/{org}/{project}/_workitems/edit/{item_id}"
    return await _enrich_ado_via_agent(
        workdir=workdir,
        out_dir=out_dir,
        source_label=source_label,
        payload_json=json.dumps(slim, indent=2, ensure_ascii=False),
    )


def _url_passthrough(url: str, out_dir: Path) -> Path:
    """Download a generic URL and write the body verbatim to spec.md."""
    try:
        raw_md = _download_text(url)
    except httpx.HTTPError as e:
        raise RuntimeError(f"url download failed: {e}") from e

    dst = out_dir / "spec.md"
    dst.write_text(raw_md, encoding="utf-8")
    return dst


def _file_passthrough(src: str, out_dir: Path) -> Path:
    """Copy a local file's content verbatim to spec.md."""
    raw_md = _read_local_text(src)  # raises FileNotFoundError if missing
    dst = out_dir / "spec.md"
    dst.write_text(raw_md, encoding="utf-8")
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
            ado_parsed = parse_ado_spec_source(src)

            if _is_jira(src):
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
                base_url, ticket = jira_parsed
                await _jira_via_rest(ctx, base_url, ticket, out_dir, wd)
                jira_dst.write_text(
                    f"# {ticket}\n\n"
                    f"Source URL: {src}\n\n"
                    f"Raw Jira capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif _is_ado(src):
                if ado_parsed is None:
                    raise ValueError(
                        "ado:ID shorthand requires AZDO_ORG + AZDO_PROJECT "
                        "env vars (or use ado:ORG/PROJECT/ID form, or pass "
                        "the full URL: "
                        "https://dev.azure.com/{org}/{project}/_workitems/edit/{id})"
                    )
                org, project, item_id = ado_parsed
                await _ado_via_rest(ctx, org, project, item_id, out_dir, wd)
                ado_url = f"https://dev.azure.com/{org}/{project}/_workitems/edit/{item_id}"
                jira_dst.write_text(
                    f"# ADO #{item_id}\n\n"
                    f"Source: {ado_url}\n\n"
                    f"Raw ADO capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif ado_parsed is not None:
                org, project, item_id = ado_parsed
                await _ado_via_rest(ctx, org, project, item_id, out_dir, wd)
                jira_dst.write_text(
                    f"# ADO #{item_id}\n\n"
                    f"Source URL: {src}\n\n"
                    f"Raw ADO capture not retained — see `spec.md` for the "
                    f"normalized content.\n",
                    encoding="utf-8",
                )
            elif _is_url(src):
                _url_passthrough(src, out_dir)
                jira_dst.write_text(
                    f"# External source\n\nDownloaded from: {src}\n\n"
                    f"Raw download written verbatim to `spec.md` — no agent "
                    f"enrichment was performed at step 1.\n",
                    encoding="utf-8",
                )
            else:
                _file_passthrough(src, out_dir)
                jira_dst.write_text(
                    f"# Local spec\n\nCopied from: {Path(src).expanduser().resolve()}\n\n"
                    f"Raw file content written verbatim to `spec.md` — no "
                    f"agent enrichment was performed at step 1.\n",
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


# Re-export legacy helper names for tests/other modules that import them.
__all__ = [
    "IntakeStep",
    "_ado_via_rest",
    "_download_text",
    "_enrich_ado_via_agent",
    "_enrich_jira_via_agent",
    "_file_passthrough",
    "_is_ado",
    "_is_jira",
    "_is_url",
    "_jira_via_rest",
    "_read_local_text",
    "_slim_jira_payload",
    "_url_passthrough",
]
