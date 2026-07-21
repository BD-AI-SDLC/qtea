"""Unit tests for the Step 7 reveal judge (progressive-disclosure callout)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from qtea.steps.s07 import reveal_judge
from qtea.steps.s07.live_driver import RevealContext
from qtea.steps.s07.reveal_judge import _parse_response, judge_reveal


# ---- Response parsing ----------------------------------------------------


def test_parse_response_extracts_click_string():
    assert _parse_response('{"click": "New Notification"}') == "New Notification"


def test_parse_response_returns_none_for_sentinel():
    assert _parse_response('{"click": "__none__"}') is None


def test_parse_response_returns_none_for_empty_click():
    assert _parse_response('{"click": ""}') is None


def test_parse_response_tolerates_markdown_fences():
    assert _parse_response(
        "```json\n{\"click\": \"Save\"}\n```"
    ) == "Save"


def test_parse_response_tolerates_prose_wrapping():
    assert _parse_response(
        "Here you go: {\"click\": \"Menu\"} — hope that helps."
    ) == "Menu"


def test_parse_response_returns_none_for_garbage():
    assert _parse_response("not JSON at all") is None
    assert _parse_response("") is None


def test_parse_response_returns_none_when_click_not_string():
    assert _parse_response('{"click": 42}') is None


# ---- End-to-end judge_reveal (call_reasoning_llm mocked) ------------------


async def test_judge_reveal_returns_none_when_agent_file_missing(tmp_path, monkeypatch):
    # Point package_resource_root at an empty dir so the agent file is missing.
    with patch.object(reveal_judge, "package_resource_root", return_value=tmp_path):
        out = await judge_reveal(
            RevealContext(
                target_name="X", target_reach_via="",
                route_path="/", route_url="https://x",
                snapshot_excerpt="", candidates=[],
            ),
            workdir=tmp_path / "wd",
        )
    assert out is None


async def test_judge_reveal_returns_click_string_on_success(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-reveal-judge.agent.md").write_text("dummy")
    fake_result = SimpleNamespace(
        success=True, final_text='{"click": "New"}', error=None,
    )
    with patch.object(reveal_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            reveal_judge, "call_reasoning_llm",
            new=AsyncMock(return_value=fake_result),
        ):
            out = await judge_reveal(
                RevealContext(
                    target_name="target", target_reach_via="hint",
                    route_path="/", route_url="https://x",
                    snapshot_excerpt="- button: 'New'", candidates=[
                        {"role": "button", "name": "New",
                         "locator": {"strategy": "role", "value": "New",
                                     "verified_unique": True}},
                    ],
                ),
                workdir=tmp_path / "wd",
            )
    assert out == "New"


async def test_judge_reveal_returns_none_when_reasoning_call_raises(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-reveal-judge.agent.md").write_text("dummy")
    with patch.object(reveal_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            reveal_judge, "call_reasoning_llm",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            out = await judge_reveal(
                RevealContext(
                    target_name="X", target_reach_via="",
                    route_path="/", route_url="https://x",
                    snapshot_excerpt="", candidates=[],
                ),
                workdir=tmp_path / "wd",
            )
    assert out is None


async def test_judge_reveal_returns_none_when_agent_output_empty(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-reveal-judge.agent.md").write_text("dummy")
    fake_result = SimpleNamespace(success=True, final_text="", error=None)
    with patch.object(reveal_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            reveal_judge, "call_reasoning_llm",
            new=AsyncMock(return_value=fake_result),
        ):
            out = await judge_reveal(
                RevealContext(
                    target_name="X", target_reach_via="",
                    route_path="/", route_url="https://x",
                    snapshot_excerpt="", candidates=[],
                ),
                workdir=tmp_path / "wd",
            )
    assert out is None
