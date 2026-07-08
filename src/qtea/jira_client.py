"""Direct REST client for Jira (Atlassian Cloud + Server/Data Center).

Replaces the ``atlassian-jira-mcp`` MCP server in qtea's Step 1 (intake).
Supports both Atlassian Cloud (``https://<tenant>.atlassian.net``) and
on-prem Jira Server/Data Center (e.g. Bosch's ``rb-tracker.bosch.com/tracker01``)
behind one ``fetch_issue()`` function.

The host is auto-detected from the base URL:
  * ``*.atlassian.net``                → REST v3, HTTP Basic auth (email + token)
  * any other host (Server/Data Center) → REST v2, Bearer auth (PAT)

Override the auth scheme via the ``JIRA_AUTH_TYPE`` env var (``basic`` |
``bearer``) when the heuristic guesses wrong.

Env vars consumed (see :mod:`qtea.config` ``SECRET_ENV_KEYS``):
  * ``JIRA_BASE_URL``     — used by :func:`parse_jira_spec_source` for the
    ``jira:KEY`` shorthand. Not needed when the spec is a full URL.
  * ``JIRA_EMAIL``        — Cloud Basic-auth username
  * ``JIRA_API_TOKEN``    — Cloud Basic-auth secret
  * ``JIRA_PAT``          — DC Bearer-auth token
  * ``JIRA_AUTH_TYPE``    — optional override (``basic``/``bearer``)

Requests are made through :class:`qtea.proxy.BoschProxyTransport`, which
falls back to PowerShell ``Invoke-WebRequest -ProxyUseDefaultCredentials``
on Windows when the corporate proxy demands NTLM auth.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from qtea.logging_setup import get_logger
from qtea.proxy import BoschProxyTransport, with_proxy_env

log = get_logger(__name__)


# Matches Atlassian's canonical permalink path: /browse/<PROJECT>-<NUMBER>.
# Used by both URL detection (parse_jira_spec_source) and to lift the key
# out of the path while keeping any preceding context path (e.g. /tracker01).
_JIRA_BROWSE_RE = re.compile(r"/browse/([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


class JiraFetchError(RuntimeError):
    """Raised on auth / connectivity / HTTP errors during ``fetch_issue``.

    Carries the HTTP status code (if any) so callers can distinguish 401
    (token expired) from 404 (wrong key) from 5xx (server problem).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def parse_jira_spec_source(src: str) -> tuple[str, str] | None:
    """Extract ``(base_url, ticket_key)`` from a qtea ``--spec`` argument.

    Accepts two forms:

    * ``jira:KEY-123`` shorthand → ``(JIRA_BASE_URL env, "KEY-123")``.
      Returns ``None`` when ``JIRA_BASE_URL`` is unset.

    * Full URL ``https://<host>/[<context>/]browse/KEY-123`` →
      ``("https://<host>[/<context>]", "KEY-123")``. Self-describing — the
      base URL is taken from the URL itself, NOT from env. This fixes the
      latent bug in the old MCP-era code where the URL host was extracted
      but then thrown away (the MCP always used JIRA_BASE_URL).

    Returns ``None`` for inputs that match neither form.
    """
    if not src:
        return None

    # jira:KEY shorthand
    if src.lower().startswith("jira:"):
        ticket = src.split(":", 1)[1].strip().upper()
        if not ticket:
            return None
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        if not base:
            return None
        return (base, ticket)

    # Full URL form
    try:
        parsed = urlparse(src)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    m = _JIRA_BROWSE_RE.search(parsed.path)
    if not m:
        return None
    ticket_key = m.group(1).upper()

    # Cloud: base is just scheme://host. DC: preserve context path (e.g.
    # /tracker01) by taking everything up to (but excluding) /browse.
    if parsed.netloc.endswith(".atlassian.net"):
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        browse_idx = parsed.path.lower().find("/browse")
        ctx = parsed.path[:browse_idx] if browse_idx > 0 else ""
        base_url = f"{parsed.scheme}://{parsed.netloc}{ctx}"
    return (base_url, ticket_key)


def _profile(base_url: str) -> tuple[str, str]:
    """Return ``(api_version, auth_kind)`` for a Jira base URL.

    Auto-detection:
      * ``*.atlassian.net`` → ``("3", "basic")``   — Cloud, REST v3
      * everything else     → ``("2", "bearer")``  — DC/Server, REST v2

    Override via ``JIRA_AUTH_TYPE=basic|bearer`` env when the heuristic
    is wrong (e.g. a self-hosted Cloud mirror or an SSO-fronted DC).
    """
    host = urlparse(base_url).hostname or ""
    is_cloud = host.endswith(".atlassian.net")
    api_version = "3" if is_cloud else "2"
    auth_kind = "basic" if is_cloud else "bearer"

    override = os.environ.get("JIRA_AUTH_TYPE", "").strip().lower()
    if override in ("basic", "bearer"):
        auth_kind = override
    return (api_version, auth_kind)


def _auth_headers(auth_kind: str) -> dict[str, str]:
    """Build the ``Authorization`` header for the chosen auth scheme.

    Reads ``JIRA_EMAIL`` + ``JIRA_API_TOKEN`` (Cloud) or ``JIRA_PAT`` (DC)
    from env. Raises :class:`JiraFetchError` when required env vars are
    missing — fail-fast at preflight, not after the HTTP call.
    """
    if auth_kind == "basic":
        email = os.environ.get("JIRA_EMAIL", "").strip()
        token = os.environ.get("JIRA_API_TOKEN", "").strip()
        if not email or not token:
            raise JiraFetchError(
                "Cloud Jira requires JIRA_EMAIL + JIRA_API_TOKEN env vars "
                "(get a token at https://id.atlassian.com/manage-profile/security/api-tokens)"
            )
        # httpx will set the Basic Authorization header from the auth arg,
        # but we build it manually here so the BoschProxyTransport PowerShell
        # fallback sees the same header it would have sent natively.
        import base64
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}

    if auth_kind == "bearer":
        pat = os.environ.get("JIRA_PAT", "").strip()
        if not pat:
            raise JiraFetchError(
                "DC Jira requires a JIRA_PAT env var "
                "(create one at <jira-base-url>/secure/ViewProfile.jspa → "
                "Personal Access Tokens)"
            )
        return {"Authorization": f"Bearer {pat}"}

    raise JiraFetchError(f"unsupported auth kind: {auth_kind!r}")


def _http_client(base_url: str, *, timeout_s: float = 30.0) -> httpx.Client:
    """Construct the httpx Client used by :func:`fetch_issue`.

    Wires up :class:`BoschProxyTransport` so 407s on Windows fall back to
    PowerShell with ``-ProxyUseDefaultCredentials``. Proxy URL is taken
    from ``HTTPS_PROXY`` / ``HTTP_PROXY`` (matches every other outbound
    HTTP path in qtea).
    """
    proxies_env = with_proxy_env()
    proxy = (
        proxies_env.get("HTTPS_PROXY")
        or proxies_env.get("HTTP_PROXY")
        or proxies_env.get("https_proxy")
        or proxies_env.get("http_proxy")
    )
    transport = BoschProxyTransport()
    return httpx.Client(
        transport=transport,
        proxy=proxy,
        timeout=timeout_s,
        follow_redirects=True,
    )


def fetch_issue(
    base_url: str,
    ticket_key: str,
    *,
    timeout_s: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch a single Jira issue via REST.

    Picks REST v3 + Basic auth for Cloud, REST v2 + Bearer for DC. Uses
    ``?expand=renderedFields`` so the description comes back as rendered
    HTML alongside the raw ADF/wiki source — downstream consumers can
    use whichever shape is easier to reformat.

    Returns the parsed JSON response as a dict (the issue payload).
    Raises :class:`JiraFetchError` on auth failure, 404, network error,
    or non-JSON response.

    Parameters
    ----------
    base_url:
        Full base URL **including any context path** for DC instances
        (e.g. ``https://rb-tracker.bosch.com/tracker01``). Trailing slash
        is tolerated.
    ticket_key:
        Issue key like ``MEAS-5490``. Normalised to uppercase.
    timeout_s:
        Per-request timeout. Default 30s.
    client:
        Optional pre-built ``httpx.Client`` — tests inject a mock; production
        callers should leave this ``None`` and let :func:`_http_client`
        build one with :class:`BoschProxyTransport` wired up.
    """
    base = base_url.rstrip("/")
    key = ticket_key.upper()
    api_version, auth_kind = _profile(base)
    headers = {"Accept": "application/json", **_auth_headers(auth_kind)}

    # CRITICAL: do NOT use urljoin here. DC base URLs carry a context path
    # (e.g. "/tracker01") that urljoin would discard when resolving against
    # an absolute path. Plain string concat preserves it.
    url = f"{base}/rest/api/{api_version}/issue/{key}?expand=renderedFields"

    owns_client = client is None
    if owns_client:
        client = _http_client(base, timeout_s=timeout_s)

    log.info(
        "jira.fetch_start",
        url=url,
        auth_kind=auth_kind,
        api_version=api_version,
    )

    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise JiraFetchError(f"network error fetching {key}: {e}") from e
    finally:
        if owns_client:
            client.close()

    if response.status_code == 401:
        raise JiraFetchError(
            f"Jira authentication failed (401) for {key}. "
            f"Token may be expired (Cloud tokens issued after 2024-12-15 "
            f"expire) or scope-restricted. "
            f"Refresh at https://id.atlassian.com/manage-profile/security/api-tokens",
            status_code=401,
        )
    if response.status_code == 403:
        raise JiraFetchError(
            f"Jira authorisation denied (403) for {key}. "
            f"The authenticated user lacks 'Browse Projects' permission "
            f"on this issue.",
            status_code=403,
        )
    if response.status_code == 404:
        raise JiraFetchError(
            f"Jira issue {key} not found (404). Check the key and that "
            f"JIRA_BASE_URL points at the correct tenant/instance.",
            status_code=404,
        )
    if response.status_code >= 400:
        raise JiraFetchError(
            f"Jira returned {response.status_code} for {key}: "
            f"{response.text[:200]}",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise JiraFetchError(
            f"Jira returned non-JSON for {key} "
            f"(content-type={response.headers.get('content-type', '?')}). "
            f"This often indicates an SSO redirect — check that your "
            f"token-based auth bypasses SSO, not your browser session.",
        ) from e

    log.info(
        "jira.fetch_ok",
        key=key,
        summary=(payload.get("fields") or {}).get("summary", ""),
    )
    return payload


# ---------------------------------------------------------------------------
# Atlassian Document Format (ADF) → markdown
# ---------------------------------------------------------------------------

def adf_to_markdown(adf: Any) -> str:
    """Best-effort conversion of an Atlassian Document Format dict to markdown.

    Cloud REST v3 returns rich-text fields (most importantly ``description``
    and comments) as ADF — a nested JSON tree of typed nodes. This walker
    flattens the common node types (``paragraph``, ``heading``,
    ``bulletList``, ``orderedList``, ``listItem``, ``codeBlock``,
    ``blockquote``, ``hardBreak``, ``text`` with marks) into markdown.

    Unknown node types are flattened recursively without their wrapper —
    the goal is "extract all text content reasonably formatted" rather
    than "perfect ADF parser". Worth keeping ``payload['renderedFields']``
    in mind as a fallback for fields where the ADF walker drops detail.

    DC/Server uses wiki markup (a string) instead of ADF — pass that
    through untouched (it's plain text with light markup that the
    reasoning agent can reformat).
    """
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf  # Server/DC wiki markup — already a string
    if not isinstance(adf, dict):
        return str(adf)

    return _adf_node(adf).rstrip()


def _adf_node(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    if node_type == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []) or []:
            mt = mark.get("type")
            if mt == "strong":
                text = f"**{text}**"
            elif mt == "em":
                text = f"*{text}*"
            elif mt == "code":
                text = f"`{text}`"
            elif mt == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                if href:
                    text = f"[{text}]({href})"
        return text

    content = node.get("content", []) or []
    inner = "".join(_adf_node(c) for c in content)

    if node_type == "doc":
        return inner
    if node_type == "paragraph":
        return inner + "\n\n"
    if node_type == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        return f"{'#' * level} {inner}\n\n"
    if node_type == "bulletList":
        return inner
    if node_type == "orderedList":
        # Mark items so the listItem handler can number them.
        items = [_adf_node(c) for c in content]
        return "".join(
            f"{i + 1}. {item.lstrip('- ').rstrip()}\n"
            for i, item in enumerate(items)
        ) + "\n"
    if node_type == "listItem":
        # Strip trailing blank lines from inner paragraph rendering.
        body = inner.rstrip()
        return f"- {body}\n"
    if node_type == "codeBlock":
        lang = (node.get("attrs") or {}).get("language", "")
        return f"```{lang}\n{inner.rstrip()}\n```\n\n"
    if node_type == "blockquote":
        lines = inner.rstrip().splitlines()
        return "\n".join(f"> {ln}" for ln in lines) + "\n\n"
    if node_type == "hardBreak":
        return "\n"
    if node_type == "rule":
        return "---\n\n"

    # Unknown node type — flatten its content without a wrapper.
    return inner


def normalize_description(payload: dict[str, Any]) -> str:
    """Pull the description out of a Jira issue payload as markdown.

    Cloud returns ADF (dict); DC returns wiki markup (string). Both are
    fed through :func:`adf_to_markdown`, which short-circuits on strings
    and walks ADF nodes. ``renderedFields.description`` is preferred when
    present (Atlassian already rendered to HTML; we strip tags) only as
    a fallback when the raw description converts to empty.
    """
    fields = payload.get("fields") or {}
    desc = fields.get("description")
    md = adf_to_markdown(desc).strip()
    if md:
        return md

    rendered = (payload.get("renderedFields") or {}).get("description")
    if isinstance(rendered, str) and rendered.strip():
        return _strip_html(rendered).strip()
    return ""


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    """Last-resort fallback: strip HTML tags to plain text.

    Only used when both the ADF and the rendered HTML are present but ADF
    extraction yields nothing. Loses formatting but preserves text.
    """
    return _HTML_TAG_RE.sub("", html).strip()


# ---------------------------------------------------------------------------
# Linked issues: fetch summary + description for related tickets
# ---------------------------------------------------------------------------

# Link type name fragments (lowercased) that disqualify a link from fetching.
_JIRA_SKIP_LINK_FRAGMENTS = frozenset({"clone", "duplicat", "test"})


def fetch_linked_issues(
    base_url: str,
    primary_payload: dict[str, Any],
    *,
    max_linked: int = 5,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    """Fetch text content for linked issues referenced in *primary_payload*.

    Only Jira text fields are fetched (summary, description, status,
    acceptance criteria). External URLs embedded in those fields (Figma,
    Confluence, etc.) are NOT followed.

    Returns a list of dicts with keys:
      ``key``, ``summary``, ``status``, ``relationship``, ``description``,
      ``acceptance_criteria``.

    Errors on individual linked-issue fetches are silently logged so a
    broken or inaccessible link never aborts Step 1.

    Priority order: parent/child links first, then blocks/depends, then
    general relates-to.
    """
    links = (primary_payload.get("fields") or {}).get("issuelinks") or []
    if not links:
        return []

    seen_keys: set[str] = set()
    candidates: list[tuple[str, str, int]] = []  # (key, rel_text, priority)

    for link in links:
        link_type = link.get("type") or {}
        type_name = (link_type.get("name") or "").lower()
        if any(frag in type_name for frag in _JIRA_SKIP_LINK_FRAGMENTS):
            continue

        for direction, side_key in (
            ("outward", "outwardIssue"),
            ("inward", "inwardIssue"),
        ):
            linked_issue = link.get(side_key)
            if not linked_issue:
                continue
            key = (linked_issue.get("key") or "").upper()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            rel_text = link_type.get(direction) or direction
            rel_lower = rel_text.lower()
            if any(f in rel_lower for f in ("parent", "child")):
                prio = 0
            elif any(f in rel_lower for f in ("block", "depend")):
                prio = 1
            else:
                prio = 2
            candidates.append((key, rel_text, prio))

    candidates.sort(key=lambda t: t[2])
    candidates = candidates[:max_linked]
    if not candidates:
        return []

    results: list[dict[str, str]] = []
    owns_client = client is None
    if owns_client:
        client = _http_client(base_url)
    try:
        for key, relationship, _ in candidates:
            try:
                payload = fetch_issue(base_url, key, client=client)
                fields = payload.get("fields") or {}

                desc = adf_to_markdown(fields.get("description")).strip()
                if not desc:
                    rendered = (payload.get("renderedFields") or {}).get("description")
                    if isinstance(rendered, str):
                        desc = _strip_html(rendered).strip()

                # AC lives in different custom fields across Jira instances.
                ac = ""
                for cf in (
                    "customfield_10014",
                    "customfield_10016",
                    "customfield_10018",
                    "customfield_10100",
                ):
                    raw = fields.get(cf)
                    if raw:
                        ac_text = (
                            adf_to_markdown(raw).strip()
                            if isinstance(raw, dict)
                            else str(raw).strip()
                        )
                        if ac_text:
                            ac = ac_text
                            break

                results.append({
                    "key": key,
                    "summary": fields.get("summary") or "",
                    "status": (fields.get("status") or {}).get("name") or "",
                    "relationship": relationship,
                    "description": desc,
                    "acceptance_criteria": ac,
                })
                log.info("jira.linked_fetch_ok", key=key, relationship=relationship)
            except JiraFetchError as e:
                log.warning("jira.linked_fetch_failed", key=key, error=str(e))
    finally:
        if owns_client:
            client.close()

    return results


__all__ = [
    "JiraFetchError",
    "adf_to_markdown",
    "fetch_issue",
    "fetch_linked_issues",
    "normalize_description",
    "parse_jira_spec_source",
]
