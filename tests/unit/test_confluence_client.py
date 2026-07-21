"""Tests for :mod:`qtea.confluence_client`.

Covers:
  * is_docupedia_url (host match, scheme, non-Docupedia)
  * parse_confluence_source (/pages/<id>/, ?pageId=, /display/SPACE/Title, None)
  * find_docupedia_urls (bare, markdown link, dedupe, cap, non-matches)
  * _auth_headers (Bearer from env, missing → raises)
  * fetch_page with a mocked httpx.Client (200 + 401/403/404 + non-JSON)
  * storage_to_markdown (headings/list/link + macro strip + fallback)
  * fetch_page_markdown (id path + title path via mocked client)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from qtea.confluence_client import (
    ConfluenceFetchError,
    _auth_headers,
    fetch_page,
    fetch_page_markdown,
    find_docupedia_urls,
    is_docupedia_url,
    parse_confluence_source,
    storage_to_markdown,
)

_BASE = "https://inside-docupedia.bosch.com/confluence"
_PAGE_URL = f"{_BASE}/spaces/AIENG/pages/4486682440/Architecture+-+Test+bed"


def _mock_client(*, status_code=200, json_data=None, text="", content_type="application/json"):
    client = MagicMock(spec=httpx.Client)
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.text = text or (json.dumps(json_data) if json_data is not None else "")
    response.headers = {"content-type": content_type}
    if json_data is not None:
        response.json.return_value = json_data
    else:
        response.json.side_effect = ValueError("no json")
    client.get.return_value = response
    return client


# ---------------------------------------------------------------------------
# is_docupedia_url
# ---------------------------------------------------------------------------


def test_is_docupedia_url_true():
    assert is_docupedia_url(_PAGE_URL) is True


def test_is_docupedia_url_false():
    assert is_docupedia_url("https://example.com/x") is False
    assert is_docupedia_url("jira:PROJ-1") is False
    assert is_docupedia_url("") is False
    # Host must match exactly (no suffix trickery).
    assert is_docupedia_url("https://inside-docupedia.bosch.com.evil.com/x") is False


# ---------------------------------------------------------------------------
# parse_confluence_source
# ---------------------------------------------------------------------------


def test_parse_pages_id_form():
    out = parse_confluence_source(_PAGE_URL)
    assert out == (_BASE, "id", "4486682440")


def test_parse_pageid_query_form():
    url = f"{_BASE}/pages/viewpage.action?pageId=12345"
    out = parse_confluence_source(url)
    assert out == (_BASE, "id", "12345")


def test_parse_display_title_form():
    url = f"{_BASE}/display/AIENG/Architecture+-+Test+bed"
    out = parse_confluence_source(url)
    assert out is not None
    base, kind, ref = out
    assert base == _BASE
    assert kind == "title"
    assert ref == "AIENG\tArchitecture - Test bed"


def test_parse_non_docupedia_returns_none():
    assert parse_confluence_source("https://example.com/pages/1/x") is None


def test_parse_docupedia_without_page_ref_returns_none():
    assert parse_confluence_source(f"{_BASE}/dashboard.action") is None


def test_parse_preserves_confluence_context_path():
    # base_url must keep /confluence so the REST path resolves.
    out = parse_confluence_source(_PAGE_URL)
    assert out[0].endswith("/confluence")


# ---------------------------------------------------------------------------
# find_docupedia_urls
# ---------------------------------------------------------------------------


def test_find_bare_and_markdown_links():
    text = (
        f"See {_PAGE_URL} for details.\n"
        f"Also [the design]({_BASE}/pages/999/Design).\n"
    )
    urls = find_docupedia_urls(text)
    assert _PAGE_URL in urls
    assert f"{_BASE}/pages/999/Design" in urls


def test_find_dedupes_and_caps():
    urls_in = "\n".join(f"{_BASE}/pages/{i}/P" for i in range(10))
    urls_in += f"\n{_BASE}/pages/0/P"  # duplicate of the first
    out = find_docupedia_urls(urls_in, max_urls=5)
    assert len(out) == 5
    assert len(set(out)) == 5


def test_find_ignores_non_docupedia():
    assert find_docupedia_urls("https://example.com/x and no links") == []


def test_find_strips_trailing_paren():
    text = f"(see {_PAGE_URL})"
    out = find_docupedia_urls(text)
    assert out == [_PAGE_URL]


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


def test_auth_headers_bearer(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok123")
    assert _auth_headers() == {"Authorization": "Bearer tok123"}


def test_auth_headers_missing_raises(monkeypatch):
    monkeypatch.delenv("DOCUPEDIA_PAT", raising=False)
    with pytest.raises(ConfluenceFetchError):
        _auth_headers()


# ---------------------------------------------------------------------------
# fetch_page
# ---------------------------------------------------------------------------


def test_fetch_page_ok(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    payload = {"title": "T", "body": {"storage": {"value": "<p>hi</p>"}}}
    client = _mock_client(json_data=payload)

    out = fetch_page(_BASE, "4486682440", client=client)

    assert out == payload
    call_url = client.get.call_args[0][0]
    assert "/rest/api/content/4486682440" in call_url
    assert "expand=body.storage" in call_url
    # context path preserved
    assert call_url.startswith(_BASE)
    headers = client.get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer tok"


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_fetch_page_http_errors(monkeypatch, code):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    client = _mock_client(status_code=code, text="err")
    with pytest.raises(ConfluenceFetchError) as ei:
        fetch_page(_BASE, "1", client=client)
    assert ei.value.status_code == code


def test_fetch_page_non_json_sso(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    client = _mock_client(status_code=200, text="<html>login</html>", content_type="text/html")
    with pytest.raises(ConfluenceFetchError) as ei:
        fetch_page(_BASE, "1", client=client)
    assert "non-JSON" in str(ei.value)


# ---------------------------------------------------------------------------
# storage_to_markdown
# ---------------------------------------------------------------------------


def test_storage_to_markdown_structure():
    html = (
        "<h1>Title</h1><p>Intro para.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        '<p>A <a href="https://x.test">link</a> here.</p>'
    )
    md = storage_to_markdown(html)
    assert "# Title" in md
    assert "Intro para." in md
    assert "- one" in md
    assert "- two" in md
    assert "[link](https://x.test)" in md


def test_storage_to_markdown_strips_macros():
    html = '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>note</p></ac:rich-text-body></ac:structured-macro>'
    md = storage_to_markdown(html)
    assert "note" in md
    assert "ac:structured-macro" not in md


def test_storage_to_markdown_drops_macro_parameters():
    # Drawing/draw.io macros carry config in <ac:parameter> — their values
    # (true/auto/ids) must NOT leak into the markdown as concatenated text.
    html = (
        "<h1>Enterprise Architecture drawing</h1>"
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="border">true</ac:parameter>'
        '<ac:parameter ac:name="diagramName">test_bed_architecture</ac:parameter>'
        '<ac:parameter ac:name="revision">237122</ac:parameter>'
        "</ac:structured-macro>"
    )
    md = storage_to_markdown(html)
    assert "# Enterprise Architecture drawing" in md
    assert "test_bed_architecture" not in md
    assert "237122" not in md


def test_storage_to_markdown_keeps_macro_body():
    # Info/note panels keep their content in <ac:rich-text-body>.
    html = (
        '<ac:structured-macro ac:name="info">'
        '<ac:parameter ac:name="title">Heads up</ac:parameter>'
        "<ac:rich-text-body><p>Important body text.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    md = storage_to_markdown(html)
    assert "Important body text." in md
    assert "Heads up" not in md  # parameter suppressed


def test_storage_to_markdown_empty():
    assert storage_to_markdown("") == ""


# ---------------------------------------------------------------------------
# fetch_page_markdown
# ---------------------------------------------------------------------------


def test_fetch_page_markdown_by_id(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    payload = {"title": "Arch", "body": {"storage": {"value": "<p>body text</p>"}}}
    client = _mock_client(json_data=payload)

    title, md = fetch_page_markdown(_PAGE_URL, client=client)

    assert title == "Arch"
    assert "body text" in md


def test_fetch_page_markdown_by_title(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    payload = {"results": [{"title": "Arch", "body": {"storage": {"value": "<p>x</p>"}}}]}
    client = _mock_client(json_data=payload)

    url = f"{_BASE}/display/AIENG/Some+Page"
    title, md = fetch_page_markdown(url, client=client)

    assert title == "Arch"
    assert "x" in md
    call_url = client.get.call_args[0][0]
    assert "spaceKey=AIENG" in call_url


def test_fetch_page_markdown_unrecognized_url_raises(monkeypatch):
    monkeypatch.setenv("DOCUPEDIA_PAT", "tok")
    with pytest.raises(ConfluenceFetchError):
        fetch_page_markdown(f"{_BASE}/dashboard.action")
