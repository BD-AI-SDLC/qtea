"""Tests for :class:`worca_t.proxy.BoschProxyTransport`.

The PowerShell fallback path is Windows-only and cannot be exercised
end-to-end in a portable test suite (Linux/macOS CI runs don't have
``powershell.exe``). Tests cover:

  * Non-407 responses pass through unchanged on any platform.
  * Non-Windows + 407 returns the 407 as-is (no fallback attempted).
  * Windows + 407 invokes ``subprocess.run(["powershell", ...])`` —
    fully mocked, never spawns a real PS.
  * PowerShell argv construction (URL, method, headers) is correct.
  * PowerShell stdout parsing extracts status + body.
  * Missing powershell.exe / timeout → graceful 502 / 504 responses.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import httpx

from worca_t.proxy import (
    BoschProxyTransport,
    _build_ps_command,
    _parse_ps_response,
)

# ---------------------------------------------------------------------------
# _parse_ps_response — PowerShell stdout parser
# ---------------------------------------------------------------------------


def test_parse_ps_response_extracts_status_and_body():
    stdout = "STATUS=200\r\n---BODY---\r\n{\"key\":\"X-1\"}\r\n"
    status, body = _parse_ps_response(stdout)
    assert status == 200
    assert body == b'{"key":"X-1"}\r\n'


def test_parse_ps_response_missing_status_returns_502():
    stdout = "garbage with no STATUS line\n"
    status, _body = _parse_ps_response(stdout)
    assert status == 502


def test_parse_ps_response_handles_status_only_no_body():
    stdout = "STATUS=204\n---BODY---\n"
    status, body = _parse_ps_response(stdout)
    assert status == 204
    assert body == b""


# ---------------------------------------------------------------------------
# _build_ps_command — PowerShell argv construction
# ---------------------------------------------------------------------------


def _decode_ps_script(argv: list[str]) -> str:
    """Reverse the base64 UTF-16-LE encoding of -EncodedCommand."""
    assert argv[0] == "powershell"
    assert "-EncodedCommand" in argv
    idx = argv.index("-EncodedCommand")
    encoded = argv[idx + 1]
    return base64.b64decode(encoded).decode("utf-16-le")


def test_build_ps_command_uses_encoded_command():
    req = httpx.Request("GET", "https://x.atlassian.net/rest/api/3/issue/X-1")
    argv = _build_ps_command(req)
    assert argv[0] == "powershell"
    assert "-NoProfile" in argv
    assert "-NonInteractive" in argv
    assert "-EncodedCommand" in argv


def test_build_ps_command_embeds_url():
    url = "https://rb-tracker.bosch.com/tracker01/rest/api/2/issue/DXFAA-14642?expand=renderedFields"
    req = httpx.Request("GET", url)
    script = _decode_ps_script(_build_ps_command(req))
    assert url in script


def test_build_ps_command_embeds_method():
    req = httpx.Request("POST", "https://x.atlassian.net/foo")
    script = _decode_ps_script(_build_ps_command(req))
    assert "'POST'" in script


def test_build_ps_command_embeds_headers_as_json():
    headers = {"Authorization": "Bearer abc==", "Accept": "application/json"}
    req = httpx.Request("GET", "https://x.atlassian.net/foo", headers=headers)
    script = _decode_ps_script(_build_ps_command(req))
    # Header values must appear in the embedded JSON.
    assert "Bearer abc==" in script
    assert "application/json" in script


def test_build_ps_command_strips_host_and_content_length():
    req = httpx.Request(
        "GET", "https://x.atlassian.net/foo",
        headers={"Host": "x.atlassian.net", "Content-Length": "0", "X-Keep": "yes"},
    )
    script = _decode_ps_script(_build_ps_command(req))
    # The stripped headers shouldn't appear in the JSON payload section
    # (httpx will have added a Host automatically — the test verifies our
    # filter at minimum removes Content-Length and keeps X-Keep).
    assert "Content-Length" not in script
    assert "X-Keep" in script


def test_build_ps_command_uses_proxy_use_default_credentials():
    req = httpx.Request("GET", "https://x.atlassian.net/foo")
    script = _decode_ps_script(_build_ps_command(req))
    assert "-ProxyUseDefaultCredentials" in script


# ---------------------------------------------------------------------------
# handle_request — full transport behaviour
# ---------------------------------------------------------------------------


def _fake_super_response(status_code: int, body: bytes = b"") -> httpx.Response:
    """Build a Response that mimics what httpx.HTTPTransport would return."""
    return httpx.Response(
        status_code=status_code,
        content=body,
        request=httpx.Request("GET", "https://x.atlassian.net/foo"),
    )


def test_non_407_passes_through_on_any_platform(monkeypatch):
    """200 OK from upstream is returned unchanged regardless of platform."""
    upstream = _fake_super_response(200, b'{"ok":1}')

    # Pretend we're on Windows so the platform check doesn't matter here.
    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    transport = BoschProxyTransport()
    with patch.object(
        BoschProxyTransport, "_powershell_fallback"
    ) as mock_fallback, patch(
        "httpx.HTTPTransport.handle_request", return_value=upstream
    ):
        response = transport.handle_request(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert response.status_code == 200
    mock_fallback.assert_not_called()


def test_407_on_non_windows_passes_through(monkeypatch):
    """Non-Windows + 407 returns the 407 — no PowerShell available."""
    upstream = _fake_super_response(407, b"Proxy Auth Required")
    monkeypatch.setattr("worca_t.proxy.sys.platform", "linux")

    transport = BoschProxyTransport()
    with patch.object(
        BoschProxyTransport, "_powershell_fallback"
    ) as mock_fallback, patch(
        "httpx.HTTPTransport.handle_request", return_value=upstream
    ):
        response = transport.handle_request(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert response.status_code == 407
    mock_fallback.assert_not_called()


def test_407_on_windows_invokes_powershell_fallback(monkeypatch):
    """Windows + 407 triggers the PowerShell fallback and returns its response."""
    upstream = _fake_super_response(407, b"need auth")
    fallback_response = _fake_super_response(200, b'{"key":"X-1"}')

    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    transport = BoschProxyTransport()
    with patch.object(
        BoschProxyTransport,
        "_powershell_fallback",
        return_value=fallback_response,
    ) as mock_fallback, patch(
        "httpx.HTTPTransport.handle_request", return_value=upstream
    ):
        response = transport.handle_request(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert response.status_code == 200
    mock_fallback.assert_called_once()


def test_powershell_fallback_parses_subprocess_output(monkeypatch):
    """End-to-end of _powershell_fallback with mocked subprocess.run."""
    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    mock_proc = MagicMock()
    mock_proc.stdout = "STATUS=200\n---BODY---\n{\"ok\":true}\n"
    mock_proc.stderr = ""

    with patch("worca_t.proxy.subprocess.run", return_value=mock_proc):
        transport = BoschProxyTransport()
        result = transport._powershell_fallback(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert result.status_code == 200
    assert b'"ok":true' in result.content


def test_powershell_fallback_missing_binary_returns_502(monkeypatch):
    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    with patch(
        "worca_t.proxy.subprocess.run",
        side_effect=FileNotFoundError("powershell.exe not found"),
    ):
        transport = BoschProxyTransport()
        result = transport._powershell_fallback(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert result.status_code == 502
    assert b"powershell.exe" in result.content


def test_powershell_fallback_timeout_returns_504(monkeypatch):
    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    import subprocess as _sp
    with patch(
        "worca_t.proxy.subprocess.run",
        side_effect=_sp.TimeoutExpired(cmd="powershell", timeout=60),
    ):
        transport = BoschProxyTransport()
        result = transport._powershell_fallback(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert result.status_code == 504
    assert b"timed out" in result.content


def test_powershell_fallback_propagates_non_2xx_status(monkeypatch):
    """Auth still failing via PS (e.g. user has no NTLM creds) → propagate the status."""
    monkeypatch.setattr("worca_t.proxy.sys.platform", "win32")

    mock_proc = MagicMock()
    mock_proc.stdout = "STATUS=401\n---BODY---\nstill unauthorized\n"
    mock_proc.stderr = ""

    with patch("worca_t.proxy.subprocess.run", return_value=mock_proc):
        transport = BoschProxyTransport()
        result = transport._powershell_fallback(
            httpx.Request("GET", "https://x.atlassian.net/foo")
        )

    assert result.status_code == 401
