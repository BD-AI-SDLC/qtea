"""Unit tests for tools/check_no_secret_exposure.py — the PreToolUse hook
that blocks Bash/PowerShell commands from printing known secret env vars.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_no_secret_exposure.py"
_spec = importlib.util.spec_from_file_location("check_no_secret_exposure", _MODULE_PATH)
check_no_secret_exposure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_no_secret_exposure)


@pytest.mark.parametrize(
    "command",
    [
        'echo $ANTHROPIC_API_KEY',
        'echo "$ANTHROPIC_API_KEY"',
        "printf '%s' \"$JIRA_API_TOKEN\"",
        "Write-Host $env:JIRA_XRAY_CLIENT_SECRET",
        "python -c \"print(os.environ.get('ANTHROPIC_API_KEY'))\"",
        "python -c \"print(os.getenv('AZDO_PAT'))\"",
        "cat .env",
        "type .env.local",
        "Get-Content .env",
        "env",
        "printenv",
        "env | grep ANTHROPIC",
    ],
)
def test_blocks_secret_exposure(command: str) -> None:
    assert check_no_secret_exposure._violations(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo hello world",
        'curl -H "x-api-key: $ANTHROPIC_API_KEY" https://api.anthropic.com/v1/messages',
        '[ -n "$ANTHROPIC_API_KEY" ] && echo set',
        "env NODE_ENV=test npm run build",
        "export ANTHROPIC_API_KEY=$NEW_VALUE",
        "git status",
        "cat README.md",
    ],
)
def test_allows_legitimate_commands(command: str) -> None:
    assert not check_no_secret_exposure._violations(command)


def test_hook_mode_blocks_and_exits_2(monkeypatch, capsys) -> None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "echo $ANTHROPIC_API_KEY"}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert check_no_secret_exposure._hook_run() == 2
    assert "BLOCKED" in capsys.readouterr().err


def test_hook_mode_allows_clean_command(monkeypatch) -> None:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert check_no_secret_exposure._hook_run() == 0


def test_hook_mode_ignores_non_shell_tools(monkeypatch) -> None:
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "foo.py"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert check_no_secret_exposure._hook_run() == 0


def test_hook_mode_handles_malformed_json(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert check_no_secret_exposure._hook_run() == 0
