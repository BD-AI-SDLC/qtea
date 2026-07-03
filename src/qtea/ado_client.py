"""Direct REST client for Azure DevOps work items.

Supports fetching work items from Azure DevOps Services
(``https://dev.azure.com/{org}/{project}``) and legacy Visual Studio Team
Services (``https://{org}.visualstudio.com/{project}``).

Auth resolution order:
  1. ``AZDO_PAT`` env var → HTTP Basic auth (PAT)
  2. ``az account get-access-token`` → Bearer auth (Azure CLI OAuth)

The Azure CLI fallback means users who are already logged in via ``az login``
(common on Bosch machines where git also uses Azure AD) need no extra
configuration.

Env vars consumed (see :mod:`qtea.config` ``SECRET_ENV_KEYS``):
  * ``AZDO_PAT``       — Personal Access Token (optional if ``az`` CLI is logged in)
  * ``AZDO_ORG``       — default organization for ``ado:ID`` shorthand
  * ``AZDO_PROJECT``   — default project for ``ado:ID`` shorthand
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from qtea.logging_setup import get_logger
from qtea.proxy import BoschProxyTransport, with_proxy_env

log = get_logger(__name__)

_ADO_WORKITEM_RE = re.compile(
    r"/_workitems/edit/(\d+)", re.IGNORECASE,
)

_LEGACY_HOST_RE = re.compile(
    r"^([^.]+)\.visualstudio\.com$", re.IGNORECASE,
)


class AdoFetchError(RuntimeError):
    """Raised on auth / connectivity / HTTP errors during ``fetch_work_item``."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def parse_ado_spec_source(src: str) -> tuple[str, str, int] | None:
    """Extract ``(org, project, work_item_id)`` from a qtea ``--spec`` argument.

    Accepts:

    * ``ado:9370`` shorthand → ``(AZDO_ORG env, AZDO_PROJECT env, 9370)``.
      Returns ``None`` when env vars are unset.

    * ``ado:BoschGPT/BoschGPT/9370`` → ``("BoschGPT", "BoschGPT", 9370)``.

    * ``https://dev.azure.com/{org}/{project}/_workitems/edit/{id}``

    * ``https://{org}.visualstudio.com/{project}/_workitems/edit/{id}``
      (legacy VSTS URLs)

    Returns ``None`` for inputs that match neither form.
    """
    if not src:
        return None

    if src.lower().startswith("ado:"):
        rest = src.split(":", 1)[1].strip()
        if not rest:
            return None
        parts = rest.split("/")
        if len(parts) == 1:
            try:
                item_id = int(parts[0])
            except ValueError:
                return None
            org = os.environ.get("AZDO_ORG", "").strip()
            project = os.environ.get("AZDO_PROJECT", "").strip()
            if not org or not project:
                return None
            return (org, project, item_id)
        if len(parts) == 3:
            try:
                item_id = int(parts[2])
            except ValueError:
                return None
            return (parts[0], parts[1], item_id)
        return None

    try:
        parsed = urlparse(src)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    m = _ADO_WORKITEM_RE.search(parsed.path)
    if not m:
        return None
    item_id = int(m.group(1))

    host = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.split("/") if p and not p.startswith("_")]

    if host == "dev.azure.com":
        if len(path_parts) < 2:
            return None
        return (path_parts[0], path_parts[1], item_id)

    legacy = _LEGACY_HOST_RE.match(parsed.netloc)
    if legacy:
        org = legacy.group(1)
        if not path_parts:
            return None
        return (org, path_parts[0], item_id)

    return None


def _resolve_token() -> tuple[str, str]:
    """Resolve an Azure DevOps auth token.

    Resolution order:
      1. ``AZDO_PAT`` env var → Basic auth (PAT)
      2. ``az account get-access-token`` → Bearer auth (Azure CLI OAuth)

    Returns ``(scheme, token)`` where scheme is ``"Basic"`` or ``"Bearer"``.
    """
    pat = os.environ.get("AZDO_PAT", "").strip()
    if pat:
        return ("Basic", pat)

    token = _az_cli_token()
    if token:
        return ("Bearer", token)

    raise AdoFetchError(
        "Azure DevOps auth: set AZDO_PAT env var or log in via "
        "'az login' (Azure CLI). Neither was found."
    )


def _az_cli_token() -> str | None:
    """Try to get an Azure DevOps access token from the Azure CLI.

    Calls ``az account get-access-token --resource 499b84ac-...`` which
    returns an OAuth token scoped to Azure DevOps. Returns ``None`` if
    ``az`` is not installed or not logged in.
    """
    import json
    import shutil
    import subprocess

    az = shutil.which("az")
    if not az:
        return None

    try:
        result = subprocess.run(
            [az, "account", "get-access-token",
             "--resource", "499b84ac-1321-427f-aa17-267ca6975798"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.debug("ado.az_cli_failed", stderr=result.stderr[:200])
            return None
        data = json.loads(result.stdout)
        token = data.get("accessToken", "").strip()
        return token or None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        log.debug("ado.az_cli_error", error=str(e))
        return None


def _auth_headers() -> dict[str, str]:
    """Build the ``Authorization`` header for Azure DevOps REST calls.

    Uses ``AZDO_PAT`` (Basic auth) if set, otherwise falls back to
    Azure CLI OAuth token (Bearer auth).
    """
    scheme, token = _resolve_token()
    if scheme == "Basic":
        creds = base64.b64encode(f":{token}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {"Authorization": f"Bearer {token}"}


def _http_client(*, timeout_s: float = 30.0) -> httpx.Client:
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


def fetch_work_item(
    org: str,
    project: str,
    item_id: int,
    *,
    timeout_s: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch a single Azure DevOps work item via REST API v7.1.

    Returns the parsed JSON response as a dict.
    Raises :class:`AdoFetchError` on auth failure, 404, network error,
    or non-JSON response.
    """
    headers = {"Accept": "application/json", **_auth_headers()}
    url = (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/wit/workitems/{item_id}"
        f"?$expand=all&api-version=7.1"
    )

    owns_client = client is None
    if owns_client:
        client = _http_client(timeout_s=timeout_s)

    log.info("ado.fetch_start", url=url, org=org, project=project, item_id=item_id)

    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise AdoFetchError(f"network error fetching work item {item_id}: {e}") from e
    finally:
        if owns_client:
            client.close()

    if response.status_code == 401:
        raise AdoFetchError(
            f"Azure DevOps authentication failed (401) for work item {item_id}. "
            f"Check that AZDO_PAT is valid and not expired.",
            status_code=401,
        )
    if response.status_code == 403:
        raise AdoFetchError(
            f"Azure DevOps authorisation denied (403) for work item {item_id}. "
            f"The PAT may lack the 'Work Items (Read)' scope.",
            status_code=403,
        )
    if response.status_code == 404:
        raise AdoFetchError(
            f"Azure DevOps work item {item_id} not found (404). "
            f"Check the ID and that AZDO_ORG/AZDO_PROJECT are correct.",
            status_code=404,
        )
    if response.status_code >= 400:
        raise AdoFetchError(
            f"Azure DevOps returned {response.status_code} for work item "
            f"{item_id}: {response.text[:200]}",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise AdoFetchError(
            f"Azure DevOps returned non-JSON for work item {item_id} "
            f"(content-type={response.headers.get('content-type', '?')}). "
            f"This may indicate an SSO redirect or auth issue.",
        ) from e

    title = (payload.get("fields") or {}).get("System.Title", "")
    log.info("ado.fetch_ok", item_id=item_id, title=title)
    return payload


# ---------------------------------------------------------------------------
# HTML → markdown (Azure DevOps uses HTML for rich text, not ADF)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)>", re.DOTALL)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_WHITESPACE_COLLAPSE_RE = re.compile(r"\n{3,}")


def html_to_markdown(html: str | None) -> str:
    """Best-effort conversion of Azure DevOps HTML to markdown.

    Handles the common elements ADO emits: ``<p>``, ``<br>``, ``<strong>``,
    ``<b>``, ``<em>``, ``<i>``, ``<a>``, ``<ul>/<ol>/<li>``, ``<h1-6>``,
    ``<code>``, ``<pre>``, ``<blockquote>``. Unknown tags are stripped.
    """
    if not html:
        return ""

    result: list[str] = []
    pos = 0
    list_stack: list[str] = []
    ol_counters: list[int] = []
    link_href: str | None = None

    for m in _TAG_RE.finditer(html):
        start, end = m.span()
        if start > pos:
            text = html[pos:start]
            text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ").replace("&quot;", '"')
            result.append(text)
        pos = end

        closing = m.group(1) == "/"
        tag = m.group(2).lower()
        attrs = m.group(3)

        if tag in ("p", "div"):
            if closing:
                result.append("\n\n")
        elif tag == "br":
            result.append("\n")
        elif tag in ("strong", "b"):
            result.append("**")
        elif tag in ("em", "i"):
            result.append("*")
        elif (tag == "code" and not closing) or (tag == "code" and closing):
            result.append("`")
        elif tag == "pre":
            if not closing:
                result.append("\n```\n")
            else:
                result.append("\n```\n\n")
        elif tag == "blockquote":
            if not closing:
                result.append("\n> ")
            else:
                result.append("\n\n")
        elif tag == "a":
            if not closing:
                href_m = _HREF_RE.search(attrs)
                link_href = href_m.group(1) if href_m else None
                result.append("[")
            else:
                if link_href:
                    result.append(f"]({link_href})")
                else:
                    result.append("]")
                link_href = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            if not closing:
                result.append(f"\n{'#' * level} ")
            else:
                result.append("\n\n")
        elif tag == "ul":
            if not closing:
                list_stack.append("ul")
            else:
                if list_stack:
                    list_stack.pop()
                result.append("\n")
        elif tag == "ol":
            if not closing:
                list_stack.append("ol")
                ol_counters.append(0)
            else:
                if list_stack:
                    list_stack.pop()
                if ol_counters:
                    ol_counters.pop()
                result.append("\n")
        elif tag == "li":
            if not closing:
                if list_stack and list_stack[-1] == "ol" and ol_counters:
                    ol_counters[-1] += 1
                    result.append(f"{ol_counters[-1]}. ")
                else:
                    result.append("- ")
            else:
                result.append("\n")
        elif tag == "hr":
            result.append("\n---\n\n")

    if pos < len(html):
        tail = html[pos:]
        tail = tail.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ").replace("&quot;", '"')
        result.append(tail)

    text = "".join(result).strip()
    text = _WHITESPACE_COLLAPSE_RE.sub("\n\n", text)
    return text


def normalize_description(payload: dict[str, Any]) -> str:
    """Extract work item description as markdown.

    Tries ``System.Description`` first; falls back to
    ``Microsoft.VSTS.TCM.ReproSteps`` (common on Bug work item types).
    """
    fields = payload.get("fields") or {}
    desc = fields.get("System.Description")
    md = html_to_markdown(desc).strip()
    if md:
        return md

    repro = fields.get("Microsoft.VSTS.TCM.ReproSteps")
    md = html_to_markdown(repro).strip()
    if md:
        return md

    return ""


def slim_ado_payload(payload: dict) -> dict:
    """Trim a raw Azure DevOps work item payload to spec-relevant fields.

    Keeps:
      * top-level: id, url, rev
      * fields: System.*, Microsoft.VSTS.Common.*, AcceptanceCriteria,
        ReproSteps, any Custom.* with non-empty values
      * relations (linked items)
    """
    slim: dict = {k: payload.get(k) for k in ("id", "url", "rev") if k in payload}
    fields = payload.get("fields") or {}
    keep_prefixes = (
        "System.Title",
        "System.Description",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.CreatedBy",
        "System.CreatedDate",
        "System.ChangedDate",
        "System.Tags",
        "System.AreaPath",
        "System.IterationPath",
        "Microsoft.VSTS.Common.Priority",
        "Microsoft.VSTS.Common.Severity",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
        "Microsoft.VSTS.TCM.ReproSteps",
    )
    slim_fields = {k: fields[k] for k in keep_prefixes if k in fields}
    for k, v in fields.items():
        if k.startswith("Custom.") and v not in (None, "", [], {}):
            slim_fields[k] = v
    slim["fields"] = slim_fields

    if "relations" in payload:
        slim["relations"] = payload["relations"]

    return slim


__all__ = [
    "AdoFetchError",
    "fetch_work_item",
    "html_to_markdown",
    "normalize_description",
    "parse_ado_spec_source",
    "slim_ado_payload",
]
