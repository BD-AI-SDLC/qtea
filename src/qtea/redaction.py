"""Free-text redaction for artifacts that persist outside a single run's workspace.

Currently used by :mod:`qtea.incident_memory` — the one artifact class that
outlives its run's ``<workspace>/`` and lands in the shared, long-lived
``~/.qtea/incident-memory/`` store, so it needs enforced scrubbing rather than
the log-line masking in :mod:`qtea.logging_setup`.

Broader than ``logging_setup._mask_str`` (known token *shapes* only): also
catches ``Authorization``/``Cookie`` headers, credentialed URLs, and generic
``key=value`` secret assignments that surface in stack traces and CLI output.
Output format ``<redacted:<reason>>`` matches the wording used by CLAUDE.md's
"No PII / runtime secrets in artifacts" Hard Rule, not logging's
``***REDACTED***``.

Limitation (intentional, per scope): this does NOT attempt general PII
detection (names, emails, IP addresses). It targets credentials/secrets that
must never persist. Callers writing genuinely user-facing free text should
still apply their own domain redaction on top.
"""

from __future__ import annotations

import re

# Known secret token shapes — superset of logging_setup._SECRET_VALUE_RE.
_KNOWN_TOKEN_RE = re.compile(
    r"sk-ant-api03-[A-Za-z0-9_-]+"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|glpat-[A-Za-z0-9_-]+"
    r"|xox[bpas]-[A-Za-z0-9_-]+"
    r"|eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]+"
    r"|AKIA[A-Z0-9]{16}"
)
# https://user:pass@host — collapse the credentials, keep the scheme.
_URL_CREDENTIALS_RE = re.compile(r"(https?)://[^\s/:@]+:[^\s/@]+@")
# Authorization: Bearer <tok>  /  Authorization: Basic <tok>
_AUTH_HEADER_RE = re.compile(r"(?im)^(\s*authorization)\s*:\s*(?:bearer|basic)\s+\S+")
# Cookie:/Set-Cookie: <whole line>
_COOKIE_HEADER_RE = re.compile(r"(?im)^(\s*(?:set-)?cookie)\s*:\s*.+$")
# key=value / key: value where key names a secret and value is non-trivial.
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|auth[_-]?token)\b"
    r"(\s*[:=]\s*)"
    r"['\"]?[^\s'\",]{6,}['\"]?"
)


def redact_text(text: str) -> str:
    """Best-effort scrub of credentials/secrets from free text.

    Order matters: header-shaped patterns (which carry a ``key:`` prefix) run
    before the bare-value patterns, so a whole ``Authorization: Bearer xyz``
    line collapses to one placeholder instead of leaving a dangling
    ``Authorization: <redacted:...>`` from a narrower value match firing first.
    """
    if not text:
        return text
    text = _AUTH_HEADER_RE.sub(r"\1: <redacted:auth-header>", text)
    text = _COOKIE_HEADER_RE.sub(r"\1: <redacted:cookie>", text)
    text = _URL_CREDENTIALS_RE.sub(r"\1://<redacted:url-credentials>@", text)
    text = _KV_SECRET_RE.sub(r"\1\2<redacted:secret-value>", text)
    text = _KNOWN_TOKEN_RE.sub("<redacted:token>", text)
    return text
