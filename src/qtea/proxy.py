"""Proxy detection + propagation to subprocesses.

On Windows, also merges user-level registry environment variables so that
credentials stored by ``claude login`` are available to child processes even
when the parent shell was started before login.

For outbound HTTPS that traverses an NTLM-authenticated corporate proxy
(e.g. Bosch's internal proxy), :class:`BoschProxyTransport` provides a
PowerShell-based fallback that uses Windows session credentials transparently
when the standard ``httpx`` request returns ``407 Proxy Authentication
Required``. This is the direct-SDK replacement for the workaround Bosch's
custom MCP servers used.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping

import httpx

from qtea.config import PROXY_ENV_KEYS, SECRET_ENV_KEYS
from qtea.logging_setup import get_logger

log = get_logger(__name__)


def _windows_user_env() -> dict[str, str]:
    """Read user-level env vars from HKEY_CURRENT_USER\\Environment.

    Returns an empty dict on non-Windows or if the registry is unreadable.
    """
    if sys.platform != "win32":
        return {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            env: dict[str, str] = {}
            idx = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, idx)
                    env[name] = value
                    idx += 1
                except OSError:
                    break
            return env
    except Exception:
        return {}


def detected_proxies() -> dict[str, str]:
    """Return proxy-related env vars currently set in the process."""
    return {k: os.environ[k] for k in PROXY_ENV_KEYS if k in os.environ}


def with_proxy_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build an env dict containing the current process env + proxy vars + extras.

    Use this when constructing subprocesses to guarantee proxy + credential
    propagation under corporate networks.

    On Windows, user-level registry variables are merged first so that
    credentials stored by ``claude login`` are picked up even when the current
    process inherited an older environment.  Process-level vars take precedence.
    """
    # Start with registry vars (Windows only) so process env can override.
    env = _windows_user_env()
    env.update(os.environ)

    # Ensure both upper- and lower-case proxy keys agree. When process env sets
    # one spelling, it's authoritative for both (overrides the Windows registry
    # fallback). Falls back to copy-the-only-present-spelling when neither is in
    # process env.
    proxy_pairs = (
        ("HTTP_PROXY", "http_proxy"),
        ("HTTPS_PROXY", "https_proxy"),
        ("NO_PROXY", "no_proxy"),
    )
    for upper, lower in proxy_pairs:
        upper_proc = os.environ.get(upper)
        lower_proc = os.environ.get(lower)
        if upper_proc is not None:
            env[upper] = upper_proc
            env[lower] = upper_proc
        elif lower_proc is not None:
            env[upper] = lower_proc
            env[lower] = lower_proc
        elif upper in env and lower not in env:
            env[lower] = env[upper]
        elif lower in env and upper not in env:
            env[upper] = env[lower]
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def safe_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    isolate_venv: bool = False,
) -> dict[str, str]:
    """Like ``with_proxy_env``, but strips inherited ``SECRET_ENV_KEYS``.

    Use for any subprocess that runs SUT-provided code (test commands, install
    commands, MCP server probes).  Trusted tooling (Claude CLI, git, allure)
    should continue using ``with_proxy_env`` directly.

    Keys present in ``extra`` are honored even if they appear in
    ``SECRET_ENV_KEYS``: ``extra`` is the caller's explicit declaration of
    what the child needs (e.g. an MCP server's ``env`` block in ``.mcp.json``).
    Only secrets *inherited* from the parent process are scrubbed.

    When ``isolate_venv=True``, ``VIRTUAL_ENV`` and ``POETRY_ACTIVE`` are also
    stripped (unless overridden via ``extra``). Use this for subprocesses
    targeting a Python SUT managed by poetry / uv: if qtea itself runs from
    a venv (e.g. ``uv tool install --editable``), that venv leaks into the
    child via ``VIRTUAL_ENV`` and poetry happily reuses it as the SUT's venv
    when the Python version satisfies the SUT's constraint. The result is a
    contaminated environment where ``poetry install`` reports "in sync" yet
    pytest fails on missing SUT-specific deps. Stripping forces poetry to
    create / use a clean SUT-specific venv.
    """
    explicit = {str(k) for k in (extra or {})}
    env = with_proxy_env(extra)
    for key in SECRET_ENV_KEYS:
        if key in explicit:
            continue
        env.pop(key, None)
    if isolate_venv:
        for key in ("VIRTUAL_ENV", "POETRY_ACTIVE"):
            if key in explicit:
                continue
            env.pop(key, None)
    return env


def mask_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of env with secret values redacted (for logging)."""
    return {k: ("***REDACTED***" if k in SECRET_ENV_KEYS else v) for k, v in env.items()}


def set_owner_only_perms(path: Path) -> None:
    """Best-effort owner-only read/write on both Unix and Windows."""
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return
    try:
        import subprocess as _sp
        username = os.environ.get("USERNAME") or os.environ.get("USER", "")
        if not username:
            return
        _sp.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{username}:(R,W)", "/remove", "Everyone"],
            capture_output=True, timeout=10, check=False,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BoschProxyTransport — Windows NTLM proxy fallback
# ---------------------------------------------------------------------------

# Headers that ``Invoke-WebRequest`` rejects or that conflict with its
# automatic handling. Strip them from the request before passing to PS.
_PS_STRIP_HEADERS = frozenset({"host", "content-length", "connection"})

# PowerShell sub-process timeout (seconds). The cold-start cost is
# ~300-800ms on first invocation; subsequent calls inside the same Python
# process aren't reused (each call spawns a fresh PS).
_PS_TIMEOUT_S = 60

_PS_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$headersJson = @'
__HEADERS_JSON__
'@
$headers = @{}
foreach ($prop in (ConvertFrom-Json -InputObject $headersJson).PSObject.Properties) {
    $headers[$prop.Name] = $prop.Value
}
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri __URL__ -Method __METHOD__ `
        -Headers $headers -ProxyUseDefaultCredentials
    Write-Output ("STATUS=" + $r.StatusCode)
    Write-Output '---BODY---'
    Write-Output $r.Content
} catch {
    if ($_.Exception.Response) {
        Write-Output ("STATUS=" + [int]$_.Exception.Response.StatusCode.value__)
    } else {
        Write-Output 'STATUS=502'
    }
    Write-Output '---BODY---'
    Write-Output $_.Exception.Message
}
""".strip()

_STATUS_RE = re.compile(r"^STATUS=(\d+)", re.MULTILINE)


def _build_ps_command(request: httpx.Request) -> list[str]:
    """Build the ``powershell.exe -EncodedCommand`` argv for a fallback request.

    Uses ``-EncodedCommand`` (base64 UTF-16-LE) so headers / URLs containing
    quotes, dollars, semicolons, or other shell metacharacters survive the
    Python → PS boundary unscathed.

    Headers are passed as JSON inside the script body and parsed via
    ``ConvertFrom-Json`` on the PS side — eliminates the per-header
    escaping headache.
    """
    # Strip headers that Invoke-WebRequest manages itself.
    headers_dict = {}
    for raw_name, raw_value in request.headers.raw:
        name = raw_name.decode("ascii", errors="replace")
        value = raw_value.decode("latin-1", errors="replace")
        if name.lower() not in _PS_STRIP_HEADERS:
            headers_dict[name] = value

    # Embed headers as JSON inside a here-string. JSON's own escaping
    # handles quotes / backslashes; the PS here-string boundary is the
    # only thing we need to avoid colliding with — '@ on a new line.
    # JSON output never produces that sequence.
    headers_json = json.dumps(headers_dict, ensure_ascii=False)

    # URL + method as single-quoted PS literals — safe because URL/method
    # don't contain single quotes in valid HTTP.
    url_literal = "'" + str(request.url).replace("'", "''") + "'"
    method_literal = "'" + request.method.upper() + "'"

    script = (
        _PS_SCRIPT_TEMPLATE
        .replace("__HEADERS_JSON__", headers_json)
        .replace("__URL__", url_literal)
        .replace("__METHOD__", method_literal)
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _parse_ps_response(stdout: str) -> tuple[int, bytes]:
    """Split PS stdout into ``(status_code, body_bytes)``.

    PS script writes ``STATUS=<code>`` then ``---BODY---`` then the body.
    A malformed response (no STATUS line) is treated as 502.
    """
    status_match = _STATUS_RE.search(stdout)
    status_code = int(status_match.group(1)) if status_match else 502
    _, _, body = stdout.partition("---BODY---")
    return status_code, body.lstrip("\r\n").encode("utf-8")


class BoschProxyTransport(httpx.HTTPTransport):
    """httpx transport with Windows NTLM-proxy fallback via PowerShell.

    The standard ``httpx`` request is attempted first. If it returns
    ``407 Proxy Authentication Required`` AND we're running on Windows,
    the request is re-issued via ``powershell.exe Invoke-WebRequest
    -ProxyUseDefaultCredentials``, which uses the current Windows session's
    credentials to authenticate against the corporate proxy. This is the
    same technique Bosch's custom MCP servers use internally.

    Behaviour on non-Windows platforms is a transparent passthrough — the
    407 response propagates unchanged. Bosch primary users are on Windows;
    Linux / macOS users either bypass the corporate proxy entirely or run
    a local NTLM-proxy sidecar (e.g. ``cntlm``, ``px``).

    Limitations:
      * Cold-start latency ~300-800ms per fallback (PowerShell startup).
      * GET requests only have been exercised in production. POST/PUT
        should work since the script propagates ``-Method``, but bodies
        are not currently forwarded — keep this transport for read-only
        external calls (e.g. JIRA REST fetches).
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = super().handle_request(request)
        if response.status_code != 407 or sys.platform != "win32":
            return response

        log.info(
            "bosch_proxy.fallback",
            url=str(request.url),
            reason="407 Proxy Authentication Required",
        )
        # Drain the 407 body before reissuing so the connection is freed.
        try:
            response.read()
            response.close()
        except Exception:
            pass

        return self._powershell_fallback(request)

    def _powershell_fallback(self, request: httpx.Request) -> httpx.Response:
        argv = _build_ps_command(request)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_PS_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            log.error("bosch_proxy.powershell_missing")
            return httpx.Response(
                status_code=502,
                content=b"BoschProxyTransport: powershell.exe not on PATH",
                request=request,
            )
        except subprocess.TimeoutExpired:
            log.error(
                "bosch_proxy.powershell_timeout",
                url=str(request.url),
                timeout_s=_PS_TIMEOUT_S,
            )
            return httpx.Response(
                status_code=504,
                content=b"BoschProxyTransport: PowerShell request timed out",
                request=request,
            )

        status_code, body = _parse_ps_response(proc.stdout or "")
        if status_code >= 400:
            log.warning(
                "bosch_proxy.fallback_non_2xx",
                url=str(request.url),
                status=status_code,
                stderr_tail=(proc.stderr or "")[-300:] if proc.stderr else "",
            )
        return httpx.Response(
            status_code=status_code,
            content=body,
            request=request,
        )


__all__ = [
    "BoschProxyTransport",
    "detected_proxies",
    "mask_secrets",
    "safe_subprocess_env",
    "with_proxy_env",
]
