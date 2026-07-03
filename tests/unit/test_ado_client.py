"""Azure DevOps client tests — URL parsing, auth, fetch, HTML→markdown."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest

from qtea.ado_client import (
    AdoFetchError,
    fetch_work_item,
    html_to_markdown,
    normalize_description,
    parse_ado_spec_source,
    slim_ado_payload,
)

# ---------------------------------------------------------------------------
# parse_ado_spec_source — shorthand forms
# ---------------------------------------------------------------------------


def test_parse_ado_prefix_id_only(monkeypatch):
    monkeypatch.setenv("AZDO_ORG", "MyOrg")
    monkeypatch.setenv("AZDO_PROJECT", "MyProject")
    result = parse_ado_spec_source("ado:9370")
    assert result == ("MyOrg", "MyProject", 9370)


def test_parse_ado_prefix_id_only_missing_env(monkeypatch):
    monkeypatch.delenv("AZDO_ORG", raising=False)
    monkeypatch.delenv("AZDO_PROJECT", raising=False)
    assert parse_ado_spec_source("ado:9370") is None


def test_parse_ado_prefix_id_only_missing_project(monkeypatch):
    monkeypatch.setenv("AZDO_ORG", "MyOrg")
    monkeypatch.delenv("AZDO_PROJECT", raising=False)
    assert parse_ado_spec_source("ado:9370") is None


def test_parse_ado_prefix_full():
    result = parse_ado_spec_source("ado:BoschGPT/BoschGPT/9370")
    assert result == ("BoschGPT", "BoschGPT", 9370)


def test_parse_ado_prefix_full_different_org():
    result = parse_ado_spec_source("ado:Contoso/WebApp/42")
    assert result == ("Contoso", "WebApp", 42)


def test_parse_ado_prefix_empty():
    assert parse_ado_spec_source("ado:") is None


def test_parse_ado_prefix_non_numeric():
    assert parse_ado_spec_source("ado:abc") is None


def test_parse_ado_prefix_bad_part_count():
    assert parse_ado_spec_source("ado:a/b") is None
    assert parse_ado_spec_source("ado:a/b/c/d") is None


def test_parse_ado_prefix_non_numeric_full():
    assert parse_ado_spec_source("ado:Org/Proj/notanumber") is None


# ---------------------------------------------------------------------------
# parse_ado_spec_source — full URL forms
# ---------------------------------------------------------------------------


def test_parse_ado_full_url():
    url = "https://dev.azure.com/BoschGPT/BoschGPT/_workitems/edit/9370"
    result = parse_ado_spec_source(url)
    assert result == ("BoschGPT", "BoschGPT", 9370)


def test_parse_ado_full_url_trailing_slash():
    url = "https://dev.azure.com/BoschGPT/BoschGPT/_workitems/edit/9370/"
    result = parse_ado_spec_source(url)
    assert result == ("BoschGPT", "BoschGPT", 9370)


def test_parse_ado_full_url_with_query():
    url = "https://dev.azure.com/Org/Proj/_workitems/edit/123?fullScreen=true"
    result = parse_ado_spec_source(url)
    assert result == ("Org", "Proj", 123)


def test_parse_ado_legacy_url():
    url = "https://myorg.visualstudio.com/MyProject/_workitems/edit/456"
    result = parse_ado_spec_source(url)
    assert result == ("myorg", "MyProject", 456)


def test_parse_non_ado_url_returns_none():
    assert parse_ado_spec_source("https://github.com/org/repo") is None
    assert parse_ado_spec_source("https://example.com/page") is None


def test_parse_jira_url_returns_none():
    assert parse_ado_spec_source("https://bosch-pt.atlassian.net/browse/MEAS-5490") is None


def test_parse_empty_returns_none():
    assert parse_ado_spec_source("") is None


def test_parse_local_file_returns_none():
    assert parse_ado_spec_source("path/to/spec.md") is None


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


def test_auth_headers_basic_pat(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "my-secret-pat")
    from qtea.ado_client import _auth_headers
    headers = _auth_headers()
    expected = base64.b64encode(b":my-secret-pat").decode()
    assert headers["Authorization"] == f"Basic {expected}"


def test_auth_headers_az_cli_fallback(monkeypatch):
    """When AZDO_PAT is unset, fall back to az CLI token."""
    monkeypatch.delenv("AZDO_PAT", raising=False)
    from qtea.ado_client import _auth_headers
    monkeypatch.setattr(
        "qtea.ado_client._az_cli_token",
        lambda: "fake-oauth-token",
    )
    headers = _auth_headers()
    assert headers["Authorization"] == "Bearer fake-oauth-token"


def test_auth_headers_missing_pat_and_no_az(monkeypatch):
    monkeypatch.delenv("AZDO_PAT", raising=False)
    from qtea.ado_client import _auth_headers
    monkeypatch.setattr("qtea.ado_client._az_cli_token", lambda: None)
    with pytest.raises(AdoFetchError, match="az login"):
        _auth_headers()


def test_auth_pat_takes_precedence_over_az(monkeypatch):
    """AZDO_PAT is preferred even when az CLI is available."""
    monkeypatch.setenv("AZDO_PAT", "my-pat")
    from qtea.ado_client import _auth_headers
    monkeypatch.setattr(
        "qtea.ado_client._az_cli_token",
        lambda: "should-not-be-used",
    )
    headers = _auth_headers()
    expected = base64.b64encode(b":my-pat").decode()
    assert headers["Authorization"] == f"Basic {expected}"


# ---------------------------------------------------------------------------
# fetch_work_item
# ---------------------------------------------------------------------------


def _mock_client(status_code: int = 200, body: dict | str | None = None,
                 content_type: str = "application/json") -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.headers = {"content-type": content_type}
    if isinstance(body, dict):
        response.json.return_value = body
        response.text = json.dumps(body)
    elif isinstance(body, str):
        response.json.side_effect = ValueError("not json")
        response.text = body
    else:
        response.json.return_value = body or {}
        response.text = json.dumps(body or {})
    client.get.return_value = response
    return client


def test_fetch_work_item_200(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    payload = {
        "id": 9370,
        "fields": {"System.Title": "Test item"},
    }
    client = _mock_client(200, payload)
    result = fetch_work_item("BoschGPT", "BoschGPT", 9370, client=client)
    assert result["id"] == 9370
    url_called = client.get.call_args[0][0]
    assert "dev.azure.com/BoschGPT/BoschGPT" in url_called
    assert "9370" in url_called
    assert "api-version=7.1" in url_called


def test_fetch_work_item_401(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "bad")
    client = _mock_client(401)
    with pytest.raises(AdoFetchError, match="authentication failed") as exc_info:
        fetch_work_item("Org", "Proj", 1, client=client)
    assert exc_info.value.status_code == 401


def test_fetch_work_item_403(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    client = _mock_client(403)
    with pytest.raises(AdoFetchError, match="authorisation denied") as exc_info:
        fetch_work_item("Org", "Proj", 1, client=client)
    assert exc_info.value.status_code == 403


def test_fetch_work_item_404(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    client = _mock_client(404)
    with pytest.raises(AdoFetchError, match="not found") as exc_info:
        fetch_work_item("Org", "Proj", 999, client=client)
    assert exc_info.value.status_code == 404


def test_fetch_work_item_500(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    client = _mock_client(500, "Internal Server Error")
    with pytest.raises(AdoFetchError) as exc_info:
        fetch_work_item("Org", "Proj", 1, client=client)
    assert exc_info.value.status_code == 500


def test_fetch_work_item_non_json(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    client = _mock_client(200, "<html>login</html>")
    with pytest.raises(AdoFetchError, match="non-JSON"):
        fetch_work_item("Org", "Proj", 1, client=client)


def test_fetch_work_item_network_error(monkeypatch):
    monkeypatch.setenv("AZDO_PAT", "tok")
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = httpx.ConnectError("connection refused")
    with pytest.raises(AdoFetchError, match="network error"):
        fetch_work_item("Org", "Proj", 1, client=client)


# ---------------------------------------------------------------------------
# html_to_markdown
# ---------------------------------------------------------------------------


def test_html_to_markdown_none():
    assert html_to_markdown(None) == ""


def test_html_to_markdown_empty():
    assert html_to_markdown("") == ""


def test_html_to_markdown_plain_text():
    assert html_to_markdown("Hello world") == "Hello world"


def test_html_to_markdown_paragraph():
    assert "Hello" in html_to_markdown("<p>Hello</p>")


def test_html_to_markdown_bold_italic():
    result = html_to_markdown("<strong>bold</strong> and <em>italic</em>")
    assert "**bold**" in result
    assert "*italic*" in result


def test_html_to_markdown_link():
    result = html_to_markdown('<a href="https://example.com">click</a>')
    assert "[click](https://example.com)" in result


def test_html_to_markdown_heading():
    result = html_to_markdown("<h2>Section</h2>")
    assert "## Section" in result


def test_html_to_markdown_unordered_list():
    result = html_to_markdown("<ul><li>one</li><li>two</li></ul>")
    assert "- one" in result
    assert "- two" in result


def test_html_to_markdown_ordered_list():
    result = html_to_markdown("<ol><li>first</li><li>second</li></ol>")
    assert "1. first" in result
    assert "2. second" in result


def test_html_to_markdown_code():
    result = html_to_markdown("use <code>foo()</code> here")
    assert "`foo()`" in result


def test_html_to_markdown_pre():
    result = html_to_markdown("<pre>line1\nline2</pre>")
    assert "```" in result
    assert "line1" in result


def test_html_to_markdown_br():
    result = html_to_markdown("line1<br>line2")
    assert "line1\nline2" in result


def test_html_to_markdown_entities():
    result = html_to_markdown("a &amp; b &lt; c &gt; d")
    assert "a & b < c > d" in result


def test_html_to_markdown_blockquote():
    result = html_to_markdown("<blockquote>quoted text</blockquote>")
    assert "> quoted text" in result


# ---------------------------------------------------------------------------
# normalize_description
# ---------------------------------------------------------------------------


def test_normalize_description_from_system_description():
    payload = {"fields": {"System.Description": "<p>My description</p>"}}
    result = normalize_description(payload)
    assert "My description" in result


def test_normalize_description_fallback_to_repro_steps():
    payload = {"fields": {"Microsoft.VSTS.TCM.ReproSteps": "<p>Step 1</p>"}}
    result = normalize_description(payload)
    assert "Step 1" in result


def test_normalize_description_empty():
    assert normalize_description({"fields": {}}) == ""
    assert normalize_description({}) == ""


# ---------------------------------------------------------------------------
# slim_ado_payload
# ---------------------------------------------------------------------------


def test_slim_ado_payload_keeps_expected_fields():
    payload = {
        "id": 9370,
        "url": "https://dev.azure.com/Org/Proj/_apis/wit/workitems/9370",
        "rev": 5,
        "fields": {
            "System.Title": "Test item",
            "System.State": "Active",
            "System.WorkItemType": "Bug",
            "System.Description": "<p>desc</p>",
            "System.AssignedTo": {"displayName": "John"},
            "System.CreatedBy": {"displayName": "Jane"},
            "System.CreatedDate": "2026-01-01",
            "System.ChangedDate": "2026-01-02",
            "System.Tags": "tag1; tag2",
            "System.AreaPath": "Proj\\Area",
            "System.IterationPath": "Proj\\Sprint1",
            "Microsoft.VSTS.Common.Priority": 2,
            "Microsoft.VSTS.Common.Severity": "2 - High",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>AC</p>",
            "Microsoft.VSTS.TCM.ReproSteps": "<p>Steps</p>",
            "Custom.MyField": "custom value",
            "System.BoardColumn": "Active",  # should be dropped
            "System.Rev": 5,  # should be dropped
        },
        "relations": [{"rel": "System.LinkTypes.Hierarchy-Reverse"}],
        "_links": {"self": {"href": "..."}},  # should be dropped
    }
    slim = slim_ado_payload(payload)
    assert slim["id"] == 9370
    assert slim["url"] == payload["url"]
    assert slim["rev"] == 5
    assert slim["fields"]["System.Title"] == "Test item"
    assert slim["fields"]["System.State"] == "Active"
    assert slim["fields"]["Custom.MyField"] == "custom value"
    assert "System.BoardColumn" not in slim["fields"]
    assert "System.Rev" not in slim["fields"]
    assert slim["relations"] == payload["relations"]
    assert "_links" not in slim


def test_slim_ado_payload_skips_empty_custom():
    payload = {
        "id": 1,
        "fields": {
            "System.Title": "X",
            "Custom.Empty": "",
            "Custom.Null": None,
        },
    }
    slim = slim_ado_payload(payload)
    assert "Custom.Empty" not in slim["fields"]
    assert "Custom.Null" not in slim["fields"]
