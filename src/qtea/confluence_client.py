"""Direct REST client for Bosch Docupedia (Confluence Data Center).

Lets Step 1 (intake) fetch Docupedia page content with PAT Bearer auth, in two
situations:

  * ``--spec`` **is** a Docupedia URL — the page becomes the primary spec.
  * a produced ``spec.md`` **contains** Docupedia URLs — each is fetched and
    appended as context, mirroring how linked Jira issues are pulled in.

Docupedia is Bosch's on-prem Confluence DC. Its REST API accepts a Personal
Access Token via ``Authorization: Bearer <token>`` — the same auth model qtea
already uses for on-prem Jira (``JIRA_PAT``). Confluence Cloud
(``*.atlassian.net/wiki``) is out of scope.

Env vars consumed (see :mod:`qtea.config` ``SECRET_ENV_KEYS``):
  * ``DOCUPEDIA_PAT`` — Personal Access Token (Bearer auth). Masked in logs.

The base URL is self-describing: it is derived from the page URL itself
(``scheme://host`` + the ``/confluence`` context path), so no base-URL env var
is required.

Requests are made through :class:`qtea.proxy.BoschProxyTransport`, identical to
:mod:`qtea.jira_client`, so the corporate-proxy / NTLM fallback behaves the same.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from qtea.logging_setup import get_logger
from qtea.proxy import BoschProxyTransport, with_proxy_env

log = get_logger(__name__)

_DOCUPEDIA_HOST = "inside-docupedia.bosch.com"

# Page-id forms: /pages/<id>/... (modern) and ...?pageId=<id> (viewpage.action).
_PAGES_ID_RE = re.compile(r"/pages/(\d+)")
# /display/<SPACE>/<Title> (legacy, no id — resolved via title lookup).
_DISPLAY_RE = re.compile(r"/display/([^/]+)/([^/?#]+)")

# Docupedia URLs embedded in free text: bare and markdown-link forms. Hostnames
# never contain whitespace, quotes, backticks, commas, or closing brackets.
_DOCUPEDIA_URL_RE = re.compile(
    r"https?://inside-docupedia\.bosch\.com/[^\s'\"`,)}\]]+",
    re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class ConfluenceFetchError(RuntimeError):
    """Raised on auth / connectivity / HTTP errors during a Docupedia fetch.

    Carries the HTTP status code (if any) so callers can distinguish 401
    (token expired) from 404 (wrong page) from 5xx (server problem).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_docupedia_url(src: str) -> bool:
    """True when *src* is an HTTP(S) URL on the Docupedia host."""
    if not src:
        return False
    try:
        parsed = urlparse(src)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() == _DOCUPEDIA_HOST


def _base_url(parsed: Any) -> str:
    """Derive ``scheme://host[/confluence]`` from a parsed Docupedia URL.

    Docupedia serves the wiki under a ``/confluence`` context path. Preserve it
    (like the Jira DC context-path handling) so the REST path resolves.
    """
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or ""
    if path.lower().startswith("/confluence"):
        return f"{base}/confluence"
    return base


def parse_confluence_source(src: str) -> tuple[str, str, str | None] | None:
    """Extract ``(base_url, kind, ref)`` from a Docupedia URL.

    ``kind`` is either ``"id"`` (``ref`` is the numeric page id) or ``"title"``
    (``ref`` is ``"<SPACE>\\t<Title>"`` for a legacy ``/display/`` URL).

    Returns ``None`` for non-Docupedia inputs or URLs with no recognizable
    page reference.

    Recognized forms::

        .../pages/<id>/Title           → ("id",    "<id>")
        .../pages/viewpage.action?pageId=<id>
                                       → ("id",    "<id>")
        .../display/<SPACE>/<Title>    → ("title", "<SPACE>\\t<Title>")
    """
    if not is_docupedia_url(src):
        return None
    try:
        parsed = urlparse(src)
    except ValueError:
        return None

    base = _base_url(parsed)

    m = _PAGES_ID_RE.search(parsed.path)
    if m:
        return (base, "id", m.group(1))

    qs = parse_qs(parsed.query or "")
    page_ids = qs.get("pageId") or qs.get("pageid")
    if page_ids and page_ids[0].isdigit():
        return (base, "id", page_ids[0])

    m = _DISPLAY_RE.search(parsed.path)
    if m:
        space = unquote(m.group(1))
        title = unquote(m.group(2)).replace("+", " ")
        return (base, "title", f"{space}\t{title}")

    return None


def find_docupedia_urls(text: str, *, max_urls: int = 5) -> list[str]:
    """Scan free text / markdown for Docupedia URLs (deduped, order-preserving).

    Matches both bare URLs and the URL inside a ``[label](url)`` markdown link
    (the regex simply finds the ``https://inside-docupedia.bosch.com/...`` run
    wherever it appears). A trailing ``)`` is stripped so markdown-link URLs are
    not captured with the closing paren.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _DOCUPEDIA_URL_RE.finditer(text):
        url = m.group(0).rstrip(").,;")
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_urls:
            break
    return out


def _auth_headers() -> dict[str, str]:
    """Build the Bearer ``Authorization`` header from ``DOCUPEDIA_PAT`` env.

    Raises :class:`ConfluenceFetchError` when the token is missing — fail-fast
    at preflight, not after the HTTP call.
    """
    pat = os.environ.get("DOCUPEDIA_PAT", "").strip()
    if not pat:
        raise ConfluenceFetchError(
            "Docupedia requires a DOCUPEDIA_PAT env var (Bearer auth). "
            "Create a Personal Access Token at "
            "<docupedia-base>/plugins/personalaccesstokens/usertokens.action"
        )
    return {"Authorization": f"Bearer {pat}"}


def _http_client(*, timeout_s: float = 30.0) -> httpx.Client:
    """Construct the httpx Client used for Docupedia fetches.

    Identical wiring to :func:`qtea.jira_client._http_client`
    (:class:`BoschProxyTransport` + ``with_proxy_env`` proxy resolution) so the
    407 → PowerShell ``-ProxyUseDefaultCredentials`` fallback behaves the same.
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


def _raise_for_status(response: httpx.Response, ref: str) -> None:
    """Translate non-2xx / non-JSON Docupedia responses into ConfluenceFetchError."""
    sc = response.status_code
    if sc == 401:
        raise ConfluenceFetchError(
            f"Docupedia authentication failed (401) for {ref}. "
            f"DOCUPEDIA_PAT may be expired or invalid.",
            status_code=401,
        )
    if sc == 403:
        raise ConfluenceFetchError(
            f"Docupedia authorisation denied (403) for {ref}. "
            f"The token's user lacks read permission on this page/space.",
            status_code=403,
        )
    if sc == 404:
        raise ConfluenceFetchError(
            f"Docupedia page {ref} not found (404). Check the URL / page id.",
            status_code=404,
        )
    if sc >= 400:
        raise ConfluenceFetchError(
            f"Docupedia returned {sc} for {ref}: {response.text[:200]}",
            status_code=sc,
        )
    ctype = response.headers.get("content-type", "")
    if "json" not in ctype.lower():
        raise ConfluenceFetchError(
            f"Docupedia returned non-JSON for {ref} (content-type={ctype or '?'}). "
            f"This usually means an SSO redirect — confirm DOCUPEDIA_PAT is set "
            f"and PAT auth is enabled on this instance."
        )


def fetch_page(
    base_url: str,
    page_id: str,
    *,
    timeout_s: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch a single Confluence page by id via REST.

    Returns the parsed JSON content payload (``body.storage`` + metadata).
    Raises :class:`ConfluenceFetchError` on auth/HTTP/non-JSON failure.
    """
    base = base_url.rstrip("/")
    headers = {"Accept": "application/json", **_auth_headers()}
    # CRITICAL: string-concat, not urljoin — the /confluence context path in
    # base must survive (same rule as the Jira DC client).
    url = f"{base}/rest/api/content/{page_id}?expand=body.storage,version,space"

    owns = client is None
    if owns:
        client = _http_client(timeout_s=timeout_s)
    log.info("docupedia.fetch_start", url=url, page_id=page_id)
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise ConfluenceFetchError(
            f"network error fetching page {page_id}: {e}"
        ) from e
    finally:
        if owns:
            client.close()

    _raise_for_status(response, page_id)
    try:
        payload = response.json()
    except ValueError as e:
        raise ConfluenceFetchError(
            f"Docupedia returned unparseable JSON for page {page_id}"
        ) from e
    log.info("docupedia.fetch_ok", page_id=page_id, title=payload.get("title", ""))
    return payload


def fetch_page_by_title(
    base_url: str,
    space: str,
    title: str,
    *,
    timeout_s: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Best-effort fetch of a page by ``spaceKey`` + ``title`` (legacy URLs).

    Returns the first matching content payload, or ``None`` when no page
    matches. Raises :class:`ConfluenceFetchError` on auth/HTTP failure.
    """
    base = base_url.rstrip("/")
    headers = {"Accept": "application/json", **_auth_headers()}
    from urllib.parse import quote

    url = (
        f"{base}/rest/api/content"
        f"?spaceKey={quote(space)}&title={quote(title)}"
        f"&expand=body.storage,version,space"
    )

    owns = client is None
    if owns:
        client = _http_client(timeout_s=timeout_s)
    ref = f"{space}/{title}"
    log.info("docupedia.fetch_by_title_start", url=url, ref=ref)
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise ConfluenceFetchError(f"network error fetching {ref}: {e}") from e
    finally:
        if owns:
            client.close()

    _raise_for_status(response, ref)
    try:
        payload = response.json()
    except ValueError as e:
        raise ConfluenceFetchError(
            f"Docupedia returned unparseable JSON for {ref}"
        ) from e
    results = payload.get("results") or []
    if not results:
        log.warning("docupedia.fetch_by_title_empty", ref=ref)
        return None
    return results[0]


class _StorageMarkdownParser(HTMLParser):
    """Best-effort Confluence storage-format (XHTML) → markdown walker.

    The Confluence analogue of :func:`qtea.jira_client.adf_to_markdown`: flatten
    common block/inline elements into markdown, drop Confluence macro wrappers
    (``ac:*`` / ``ri:*``) while keeping their text. Not a perfect parser — the
    goal is "extract all text content reasonably formatted".
    """

    _BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._list_stack: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_code = False
        # Depth inside macro-config leaf elements whose text is not content
        # (e.g. <ac:parameter>true</ac:parameter> in a drawing macro). The
        # macro container itself stays transparent so its rich-text-body
        # (info/note panels, etc.) still renders.
        self._suppress_depth = 0

    @staticmethod
    def _is_suppressed(t: str) -> bool:
        return t == "ac:parameter" or t.startswith("ri:")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if self._is_suppressed(t):
            self._suppress_depth += 1
            return
        if self._suppress_depth:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n\n" + "#" * int(t[1]) + " ")
        elif t == "p" or t == "div":
            self._parts.append("\n\n")
        elif t == "br":
            self._parts.append("\n")
        elif t in ("ul", "ol"):
            self._list_stack.append(t)
        elif t == "li":
            depth = max(0, len(self._list_stack) - 1)
            marker = "- " if (self._list_stack[-1:] or ["ul"])[0] == "ul" else "1. "
            self._parts.append("\n" + "  " * depth + marker)
        elif t == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        elif t in ("code", "pre"):
            self._in_code = True
            self._parts.append("`")
        elif t in ("strong", "b"):
            self._parts.append("**")
        elif t in ("em", "i"):
            self._parts.append("*")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._is_suppressed(t):
            if self._suppress_depth:
                self._suppress_depth -= 1
            return
        if self._suppress_depth:
            return
        if t in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
        elif t == "a":
            text = "".join(self._link_text).strip()
            if self._href and text:
                self._parts.append(f"[{text}]({self._href})")
            elif text:
                self._parts.append(text)
            self._href = None
            self._link_text = []
        elif t in ("code", "pre"):
            self._in_code = False
            self._parts.append("`")
        elif t in ("strong", "b"):
            self._parts.append("**")
        elif t in ("em", "i"):
            self._parts.append("*")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        if self._href is not None:
            self._link_text.append(data)
        else:
            self._parts.append(data)

    def result(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def storage_to_markdown(html: str) -> str:
    """Convert Confluence storage-format XHTML to markdown (best-effort).

    Falls back to plain tag-stripping if the structured walk yields nothing.
    """
    if not html:
        return ""
    try:
        parser = _StorageMarkdownParser()
        parser.feed(html)
        md = parser.result()
    except Exception as e:  # pragma: no cover - defensive
        log.warning("docupedia.storage_parse_error", error=str(e))
        md = ""
    if md:
        return md
    return _HTML_TAG_RE.sub("", html).strip()


def _payload_to_markdown(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract ``(title, markdown_body)`` from a content payload."""
    title = payload.get("title") or ""
    storage = (
        ((payload.get("body") or {}).get("storage") or {}).get("value") or ""
    )
    body_md = storage_to_markdown(storage)
    return title, body_md


def fetch_page_markdown(
    url: str,
    *,
    timeout_s: float = 30.0,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """Fetch a Docupedia URL and return ``(title, markdown_body)``.

    Parses the URL, fetches by id or (for legacy ``/display/`` URLs) by title,
    and converts the storage body to markdown. Raises
    :class:`ConfluenceFetchError` on any failure (missing PAT, auth, HTTP,
    unrecognized URL, or page-not-found for title lookup).
    """
    parsed = parse_confluence_source(url)
    if parsed is None:
        raise ConfluenceFetchError(f"unrecognized Docupedia URL: {url}")
    base, kind, ref = parsed

    if kind == "id":
        payload = fetch_page(base, ref or "", timeout_s=timeout_s, client=client)
    else:
        space, _, title = (ref or "").partition("\t")
        payload = fetch_page_by_title(
            base, space, title, timeout_s=timeout_s, client=client
        )
        if payload is None:
            raise ConfluenceFetchError(
                f"Docupedia page not found by title: {space}/{title}"
            )
    return _payload_to_markdown(payload)


__all__ = [
    "ConfluenceFetchError",
    "fetch_page",
    "fetch_page_by_title",
    "fetch_page_markdown",
    "find_docupedia_urls",
    "is_docupedia_url",
    "parse_confluence_source",
    "storage_to_markdown",
]
