"""Tests for :mod:`worca_t.jira_client`.

Covers:
  * URL parsing (jira: shorthand, Cloud URL, DC URL with context path)
  * _profile (auto-detection + env override)
  * Auth header construction (Cloud Basic, DC Bearer, missing creds)
  * fetch_issue with a mocked httpx.Client (success + error paths)
  * adf_to_markdown (paragraphs, headings, lists, marks, fallthrough)
  * normalize_description (ADF dict + DC wiki string + empty)
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest

from worca_t.jira_client import (
    JiraFetchError,
    _auth_headers,
    _profile,
    adf_to_markdown,
    fetch_issue,
    format_payload_as_spec_md,
    normalize_description,
    parse_jira_spec_source,
)


# ---------------------------------------------------------------------------
# parse_jira_spec_source
# ---------------------------------------------------------------------------


def test_parse_jira_prefix_with_base_url_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://bosch-pt.atlassian.net")
    assert parse_jira_spec_source("jira:MEAS-5490") == (
        "https://bosch-pt.atlassian.net",
        "MEAS-5490",
    )


def test_parse_jira_prefix_normalises_to_upper(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://bosch-pt.atlassian.net")
    assert parse_jira_spec_source("jira:meas-5490") == (
        "https://bosch-pt.atlassian.net",
        "MEAS-5490",
    )


def test_parse_jira_prefix_strips_trailing_slash_from_base(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://bosch-pt.atlassian.net/")
    assert parse_jira_spec_source("jira:MEAS-5490") == (
        "https://bosch-pt.atlassian.net",
        "MEAS-5490",
    )


def test_parse_jira_prefix_without_base_url_returns_none(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    assert parse_jira_spec_source("jira:MEAS-5490") is None


def test_parse_jira_prefix_empty_ticket_returns_none(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    assert parse_jira_spec_source("jira:") is None


def test_parse_cloud_browse_url():
    src = "https://bosch-pt.atlassian.net/browse/MEAS-5490"
    assert parse_jira_spec_source(src) == (
        "https://bosch-pt.atlassian.net",
        "MEAS-5490",
    )


def test_parse_cloud_browse_url_with_query_and_fragment():
    src = "https://acme.atlassian.net/browse/PROJ-7?foo=bar#comment"
    assert parse_jira_spec_source(src) == (
        "https://acme.atlassian.net",
        "PROJ-7",
    )


def test_parse_dc_browse_url_preserves_context_path():
    src = "https://rb-tracker.bosch.com/tracker01/browse/DXFAA-14642"
    assert parse_jira_spec_source(src) == (
        "https://rb-tracker.bosch.com/tracker01",
        "DXFAA-14642",
    )


def test_parse_non_jira_url_returns_none():
    assert parse_jira_spec_source("https://example.com/foo") is None


def test_parse_garbage_returns_none():
    assert parse_jira_spec_source("not-a-url-or-jira-ref") is None
    assert parse_jira_spec_source("") is None


# ---------------------------------------------------------------------------
# _profile
# ---------------------------------------------------------------------------


def test_profile_cloud_returns_v3_basic():
    assert _profile("https://bosch-pt.atlassian.net") == ("3", "basic")


def test_profile_dc_returns_v2_bearer():
    assert _profile("https://rb-tracker.bosch.com/tracker01") == ("2", "bearer")


def test_profile_env_override_basic(monkeypatch):
    monkeypatch.setenv("JIRA_AUTH_TYPE", "basic")
    # DC URL would normally be bearer; override forces basic.
    assert _profile("https://rb-tracker.bosch.com/tracker01") == ("2", "basic")


def test_profile_env_override_bearer(monkeypatch):
    monkeypatch.setenv("JIRA_AUTH_TYPE", "bearer")
    assert _profile("https://acme.atlassian.net") == ("3", "bearer")


def test_profile_invalid_override_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("JIRA_AUTH_TYPE", "nonsense")
    assert _profile("https://acme.atlassian.net") == ("3", "basic")


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


def test_auth_headers_basic_builds_authorization(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "user@bosch.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok123")
    h = _auth_headers("basic")
    expected = "Basic " + base64.b64encode(b"user@bosch.com:tok123").decode()
    assert h == {"Authorization": expected}


def test_auth_headers_basic_missing_email_raises(monkeypatch):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    with pytest.raises(JiraFetchError, match="JIRA_EMAIL"):
        _auth_headers("basic")


def test_auth_headers_basic_missing_token_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(JiraFetchError, match="JIRA_API_TOKEN"):
        _auth_headers("basic")


def test_auth_headers_bearer_builds_authorization(monkeypatch):
    monkeypatch.setenv("JIRA_PAT", "pat-xyz")
    assert _auth_headers("bearer") == {"Authorization": "Bearer pat-xyz"}


def test_auth_headers_bearer_missing_pat_raises(monkeypatch):
    monkeypatch.delenv("JIRA_PAT", raising=False)
    with pytest.raises(JiraFetchError, match="JIRA_PAT"):
        _auth_headers("bearer")


def test_auth_headers_unknown_kind_raises():
    with pytest.raises(JiraFetchError, match="unsupported"):
        _auth_headers("something-else")


# ---------------------------------------------------------------------------
# fetch_issue (with mocked httpx client)
# ---------------------------------------------------------------------------


def _mock_client(*, status_code: int, json_data=None, text: str = "", content_type: str = "application/json"):
    """Build a MagicMock that mimics httpx.Client's .get() return."""
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


def test_fetch_issue_cloud_uses_v3_basic(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    payload = {"key": "MEAS-5490", "fields": {"summary": "ok"}}
    client = _mock_client(status_code=200, json_data=payload)

    out = fetch_issue("https://bosch-pt.atlassian.net", "MEAS-5490", client=client)

    assert out == payload
    # URL uses /rest/api/3 and includes the expand param.
    call_url = client.get.call_args[0][0]
    assert "/rest/api/3/issue/MEAS-5490" in call_url
    assert "expand=renderedFields" in call_url
    # Authorization is Basic
    headers = client.get.call_args[1]["headers"]
    assert headers["Authorization"].startswith("Basic ")


def test_fetch_issue_dc_uses_v2_bearer(monkeypatch):
    monkeypatch.setenv("JIRA_PAT", "pat")
    payload = {"key": "DXFAA-14642", "fields": {}}
    client = _mock_client(status_code=200, json_data=payload)

    fetch_issue(
        "https://rb-tracker.bosch.com/tracker01", "DXFAA-14642", client=client
    )

    call_url = client.get.call_args[0][0]
    # DC URL preserves the /tracker01 context path AND uses /rest/api/2.
    assert call_url.startswith("https://rb-tracker.bosch.com/tracker01/rest/api/2/issue/DXFAA-14642")
    headers = client.get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer pat"


def test_fetch_issue_normalises_key_to_uppercase(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    client = _mock_client(status_code=200, json_data={"key": "MEAS-5490"})
    fetch_issue("https://x.atlassian.net", "meas-5490", client=client)
    assert "/issue/MEAS-5490" in client.get.call_args[0][0]


def test_fetch_issue_401_raises_with_helpful_message(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    client = _mock_client(status_code=401, text="unauth")
    with pytest.raises(JiraFetchError) as exc_info:
        fetch_issue("https://x.atlassian.net", "MEAS-1", client=client)
    assert exc_info.value.status_code == 401
    assert "expired" in str(exc_info.value).lower() or "401" in str(exc_info.value)


def test_fetch_issue_404_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    client = _mock_client(status_code=404, text="not found")
    with pytest.raises(JiraFetchError) as exc_info:
        fetch_issue("https://x.atlassian.net", "MEAS-NOPE", client=client)
    assert exc_info.value.status_code == 404


def test_fetch_issue_403_raises(monkeypatch):
    monkeypatch.setenv("JIRA_PAT", "pat")
    client = _mock_client(status_code=403)
    with pytest.raises(JiraFetchError) as exc_info:
        fetch_issue("https://rb-tracker.bosch.com/tracker01", "X-1", client=client)
    assert exc_info.value.status_code == 403


def test_fetch_issue_500_raises_with_body_snippet(monkeypatch):
    monkeypatch.setenv("JIRA_PAT", "pat")
    client = _mock_client(status_code=500, text="boom")
    with pytest.raises(JiraFetchError, match="500"):
        fetch_issue("https://rb-tracker.bosch.com/tracker01", "X-1", client=client)


def test_fetch_issue_non_json_raises_sso_hint(monkeypatch):
    monkeypatch.setenv("JIRA_PAT", "pat")
    # 200 but the body isn't valid JSON — likely an SSO login page.
    client = _mock_client(
        status_code=200, text="<html>...login...</html>",
        content_type="text/html",
    )
    with pytest.raises(JiraFetchError, match="non-JSON"):
        fetch_issue("https://rb-tracker.bosch.com/tracker01", "X-1", client=client)


def test_fetch_issue_network_error_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(JiraFetchError, match="network error"):
        fetch_issue("https://x.atlassian.net", "MEAS-1", client=client)


def test_fetch_issue_base_url_trailing_slash_tolerated(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "u@b.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    client = _mock_client(status_code=200, json_data={"key": "X-1"})
    fetch_issue("https://x.atlassian.net/", "X-1", client=client)
    url = client.get.call_args[0][0]
    # No double-slash before /rest
    assert "//rest" not in url
    assert "/rest/api/3/issue/X-1" in url


# ---------------------------------------------------------------------------
# adf_to_markdown
# ---------------------------------------------------------------------------


def test_adf_passthrough_for_string():
    """DC wiki markup is already a string — pass through unchanged."""
    assert adf_to_markdown("plain wiki markup") == "plain wiki markup"


def test_adf_none_returns_empty():
    assert adf_to_markdown(None) == ""


def test_adf_paragraph_with_text():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]},
    ]}
    assert adf_to_markdown(adf).strip() == "hello world"


def test_adf_heading_levels():
    adf = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Title"}]},
        {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Subtitle"}]},
    ]}
    out = adf_to_markdown(adf)
    assert "# Title" in out
    assert "### Subtitle" in out


def test_adf_text_marks():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
            {"type": "text", "text": " "},
            {"type": "text", "text": "italic", "marks": [{"type": "em"}]},
            {"type": "text", "text": " "},
            {"type": "text", "text": "code", "marks": [{"type": "code"}]},
        ]},
    ]}
    out = adf_to_markdown(adf)
    assert "**bold**" in out
    assert "*italic*" in out
    assert "`code`" in out


def test_adf_link_mark():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "site", "marks": [
                {"type": "link", "attrs": {"href": "https://example.com"}}
            ]},
        ]},
    ]}
    assert "[site](https://example.com)" in adf_to_markdown(adf)


def test_adf_bullet_list():
    adf = {"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
        ]},
    ]}
    out = adf_to_markdown(adf)
    assert "- one" in out
    assert "- two" in out


def test_adf_code_block_with_language():
    adf = {"type": "doc", "content": [
        {"type": "codeBlock", "attrs": {"language": "python"},
         "content": [{"type": "text", "text": "print('x')"}]},
    ]}
    out = adf_to_markdown(adf)
    assert "```python" in out
    assert "print('x')" in out


def test_adf_unknown_type_flattens_content():
    """Unknown wrappers should still surface inner text."""
    adf = {"type": "weirdCustomNode", "content": [
        {"type": "text", "text": "preserved"},
    ]}
    assert "preserved" in adf_to_markdown(adf)


# ---------------------------------------------------------------------------
# normalize_description
# ---------------------------------------------------------------------------


def test_normalize_description_adf():
    payload = {"fields": {"description": {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "from adf"}]},
    ]}}}
    assert normalize_description(payload) == "from adf"


def test_normalize_description_wiki_string():
    payload = {"fields": {"description": "h1. wiki heading\n\nbody"}}
    assert "wiki heading" in normalize_description(payload)


def test_normalize_description_empty_returns_empty():
    assert normalize_description({"fields": {"description": None}}) == ""
    assert normalize_description({"fields": {}}) == ""
    assert normalize_description({}) == ""


def test_normalize_description_falls_back_to_rendered_html():
    """When ADF is empty but renderedFields has content, strip HTML and use it."""
    payload = {
        "fields": {"description": {"type": "doc", "content": []}},
        "renderedFields": {"description": "<p>Rendered <b>text</b></p>"},
    }
    out = normalize_description(payload)
    assert "Rendered" in out
    assert "<p>" not in out


# ---------------------------------------------------------------------------
# format_payload_as_spec_md — deterministic JIRA → spec.md renderer
# ---------------------------------------------------------------------------


def _full_payload() -> dict:
    """A representative Cloud REST v3 payload with most fields populated."""
    return {
        "key": "MEAS-5490",
        "fields": {
            "summary": "Move function — UI Screens and 3 dots menu",
            "description": "PRO users can move workspaces between projects.",
            "status": {"name": "Done"},
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Story"},
            "reporter": {"displayName": "Sagui Ettinger"},
            "assignee": None,
            "created": "2024-05-13T08:30:00.000+0200",
            "updated": "2025-04-01T14:00:00.000+0200",
            "labels": ["frontend", "move-feature"],
            "components": [{"name": "UI"}, {"name": "Workspaces"}],
            "fixVersions": [{"name": "v2025.04"}],
            "issuelinks": [
                {
                    "type": {"name": "Blocks"},
                    "outwardIssue": {
                        "key": "MEAS-5489",
                        "fields": {"summary": "Backend API for move"},
                    },
                },
                {
                    "type": {"name": "Relates"},
                    "inwardIssue": {
                        "key": "MEAS-5250",
                        "fields": {"summary": "Move & Copy Workspace epic"},
                    },
                },
            ],
        },
    }


def test_format_renders_title_and_metadata():
    md = format_payload_as_spec_md(_full_payload())
    assert "# Move function — UI Screens and 3 dots menu (MEAS-5490)" in md
    assert "**Status:** Done" in md
    assert "**Priority:** Medium" in md
    assert "**Type:** Story" in md
    assert "**Reporter:** Sagui Ettinger" in md
    assert "**Assignee:** Unassigned" in md  # null assignee
    assert "2024-05-13" in md  # created
    assert "2025-04-01" in md  # updated


def test_format_includes_source_url_when_provided():
    md = format_payload_as_spec_md(
        _full_payload(), source_url="https://bosch-pt.atlassian.net/browse/MEAS-5490"
    )
    assert "**Source:** https://bosch-pt.atlassian.net/browse/MEAS-5490" in md


def test_format_omits_source_when_not_provided():
    md = format_payload_as_spec_md(_full_payload())
    assert "**Source:**" not in md


def test_format_description_block():
    md = format_payload_as_spec_md(_full_payload())
    assert "## Description" in md
    assert "PRO users can move workspaces between projects." in md


def test_format_description_placeholder_when_empty():
    payload = _full_payload()
    payload["fields"]["description"] = ""
    md = format_payload_as_spec_md(payload)
    assert "_No description provided._" in md


def test_format_labels_components_fixversions():
    md = format_payload_as_spec_md(_full_payload())
    assert "## Labels & Components" in md
    assert "**Labels:** frontend, move-feature" in md
    assert "**Components:** UI, Workspaces" in md
    assert "**Fix Versions:** v2025.04" in md


def test_format_omits_labels_section_when_all_empty():
    """No labels + no components + no fix versions → omit the whole section."""
    payload = _full_payload()
    payload["fields"]["labels"] = []
    payload["fields"]["components"] = []
    payload["fields"]["fixVersions"] = []
    md = format_payload_as_spec_md(payload)
    assert "## Labels & Components" not in md


def test_format_linked_issues_include_summary_when_available():
    md = format_payload_as_spec_md(_full_payload())
    assert "## Linked Issues" in md
    assert "_References only — not fetched._" in md
    assert "MEAS-5489" in md
    assert "Backend API for move" in md
    assert "MEAS-5250" in md
    assert "Move & Copy Workspace epic" in md


def test_format_omits_linked_issues_when_none():
    payload = _full_payload()
    payload["fields"]["issuelinks"] = []
    md = format_payload_as_spec_md(payload)
    assert "## Linked Issues" not in md


def test_format_falls_back_to_key_when_summary_missing():
    payload = {"key": "X-1", "fields": {}}
    md = format_payload_as_spec_md(payload)
    assert "# X-1 (X-1)" in md or "# X-1" in md
    # Description placeholder still emitted.
    assert "_No description provided._" in md


def test_format_handles_dc_wiki_description_string():
    """DC returns description as a string (wiki markup); pass through as-is."""
    payload = _full_payload()
    payload["fields"]["description"] = "h1. DC wiki heading\n\nbody text"
    md = format_payload_as_spec_md(payload)
    assert "h1. DC wiki heading" in md
    assert "body text" in md
