"""Tests for the runtime secret-value masking registry (logging_setup).

Used to keep MCP-login credentials out of on-disk agent prompt files,
transcripts, and structured logs.
"""

from __future__ import annotations

import qtea.logging_setup as ls


def test_register_and_mask_value():
    ls.register_secret_values(["hunter2-password"])
    out = ls.mask_secret_values("logging in with hunter2-password now")
    assert "hunter2-password" not in out
    assert "***REDACTED***" in out


def test_short_values_are_ignored():
    ls.register_secret_values(["ab"])  # below _MIN_SECRET_LEN
    out = ls.mask_secret_values("value ab stays visible")
    assert "ab stays visible" in out


def test_token_shapes_still_masked_without_registration():
    out = ls.mask_secret_values("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert "sk-ant-api03" not in out
    assert "***REDACTED***" in out


def test_longest_first_masks_overlapping_secrets():
    ls.register_secret_values(["pass", "passphrase-longer"])
    out = ls.mask_secret_values("the passphrase-longer value")
    # The longer secret is fully masked (not left as '***REDACTED***phrase-longer').
    assert "phrase-longer" not in out
    assert "***REDACTED***" in out


def test_masking_applied_to_structured_log_processor():
    ls.register_secret_values(["topsecretvalue"])
    event = {"event": "login", "detail": "used topsecretvalue here"}
    masked = ls._mask_secrets_processor(None, "info", event)
    assert "topsecretvalue" not in masked["detail"]
