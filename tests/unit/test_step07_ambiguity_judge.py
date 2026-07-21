"""Unit tests for the Step 7 ambiguity judge (locator disambiguation callout)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from qtea.steps.s07 import ambiguity_judge
from qtea.steps.s07.ambiguity_judge import _parse_response, judge_ambiguity
from qtea.steps.s07.live_driver import AmbiguityContext


def test_parse_response_extracts_positive_index():
    assert _parse_response('{"pick_index": 2}') == 2


def test_parse_response_returns_none_for_negative_index():
    assert _parse_response('{"pick_index": -1}') is None


def test_parse_response_returns_none_when_index_not_int():
    assert _parse_response('{"pick_index": "one"}') is None


def test_parse_response_tolerates_fences():
    assert _parse_response('```json\n{"pick_index": 0}\n```') == 0


def test_parse_response_returns_none_for_garbage():
    assert _parse_response("nope") is None
    assert _parse_response("{}") is None


async def test_judge_ambiguity_returns_candidate_at_picked_index(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-ambiguity-judge.agent.md").write_text("dummy")
    candidates = [
        {"role": "button", "name": "Save", "locator": None},
        {"role": "button", "name": "Save",
         "locator": {"strategy": "role", "value": "Save", "verified_unique": True}},
    ]
    fake_result = SimpleNamespace(
        success=True, final_text='{"pick_index": 1}', error=None,
    )
    with patch.object(ambiguity_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            ambiguity_judge, "call_reasoning_llm",
            new=AsyncMock(return_value=fake_result),
        ):
            out = await judge_ambiguity(
                AmbiguityContext(intent="button: Save", route_path="/",
                                 candidates=candidates),
                workdir=tmp_path / "wd",
            )
    assert out is candidates[1]


async def test_judge_ambiguity_returns_none_for_unresolvable(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-ambiguity-judge.agent.md").write_text("dummy")
    fake_result = SimpleNamespace(
        success=True, final_text='{"pick_index": -1}', error=None,
    )
    with patch.object(ambiguity_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            ambiguity_judge, "call_reasoning_llm",
            new=AsyncMock(return_value=fake_result),
        ):
            out = await judge_ambiguity(
                AmbiguityContext(intent="X", route_path="/", candidates=[{}, {}]),
                workdir=tmp_path / "wd",
            )
    assert out is None


async def test_judge_ambiguity_returns_none_for_out_of_range_index(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-ambiguity-judge.agent.md").write_text("dummy")
    fake_result = SimpleNamespace(
        success=True, final_text='{"pick_index": 99}', error=None,
    )
    with patch.object(ambiguity_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            ambiguity_judge, "call_reasoning_llm",
            new=AsyncMock(return_value=fake_result),
        ):
            out = await judge_ambiguity(
                AmbiguityContext(intent="X", route_path="/", candidates=[{}]),
                workdir=tmp_path / "wd",
            )
    assert out is None


async def test_judge_ambiguity_returns_none_when_agent_missing(tmp_path):
    with patch.object(ambiguity_judge, "package_resource_root", return_value=tmp_path):
        out = await judge_ambiguity(
            AmbiguityContext(intent="X", route_path="/", candidates=[{}]),
            workdir=tmp_path / "wd",
        )
    assert out is None


async def test_judge_ambiguity_swallows_errors(tmp_path):
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    (agent_dir / "live-explore-ambiguity-judge.agent.md").write_text("dummy")
    with patch.object(ambiguity_judge, "package_resource_root", return_value=tmp_path):
        with patch.object(
            ambiguity_judge, "call_reasoning_llm",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            out = await judge_ambiguity(
                AmbiguityContext(intent="X", route_path="/", candidates=[{}]),
                workdir=tmp_path / "wd",
            )
    assert out is None
