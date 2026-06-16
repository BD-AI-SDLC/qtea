"""Unit tests for the Phase A4 fixture collation fix (s08_codegen.py).

Run 20260611-184450-1fbf3d lost 5 of 6 fixtures because `_create_fixtures`
launched one `asyncio.gather` task per fixture and the parallel reads all
saw the same starting `existing` content, with last-writer-wins overwriting
everyone else.

The fix collates fixtures by target file so one LLM call covers all
co-located fixtures. These tests pin that behaviour by mocking
`call_reasoning_llm` and asserting (a) one call per file, (b) the prompt
carries every fixture for that file, and (c) the resulting file contains
every requested `def <name>`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from worca_t.steps.s08_codegen import (
    _FixtureTask,
    _create_fixtures,
    _group_fixture_tasks_by_file,
)


def _make_tasks() -> list[_FixtureTask]:
    return [
        _FixtureTask(name="gemini_nav_locale_en", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="gemini_nav_locale_de", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="gtag_spy", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="gtag_removed", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="unauthenticated_context", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="mobile_viewport", at="tests/fixtures/worca_nav.py"),
        _FixtureTask(name="shared_browser", at="tests/fixtures/worca_shared.py"),
    ]


def test_group_fixture_tasks_by_file_collates_correctly():
    tasks = _make_tasks()
    by_file = _group_fixture_tasks_by_file(tasks)
    assert set(by_file.keys()) == {
        "tests/fixtures/worca_nav.py",
        "tests/fixtures/worca_shared.py",
    }
    assert len(by_file["tests/fixtures/worca_nav.py"]) == 6
    assert len(by_file["tests/fixtures/worca_shared.py"]) == 1
    nav_names = [t.name for t in by_file["tests/fixtures/worca_nav.py"]]
    assert "gemini_nav_locale_en" in nav_names
    assert "mobile_viewport" in nav_names


def test_group_fixture_tasks_by_file_drops_empty_at():
    tasks = [
        _FixtureTask(name="bad", at=""),
        _FixtureTask(name="good", at="tests/fixtures/a.py"),
    ]
    by_file = _group_fixture_tasks_by_file(tasks)
    assert list(by_file.keys()) == ["tests/fixtures/a.py"]


@dataclass
class _StubResult:
    success: bool = True
    final_text: str = ""
    error: str | None = None


def _all_fixtures_body(names: list[str]) -> str:
    """A minimal valid file body that defines every fixture in `names`."""
    body = "import pytest\n\n"
    for n in names:
        body += f"\n@pytest.fixture(scope='function')\ndef {n}():\n    yield\n"
    return body


def test_create_fixtures_one_llm_call_per_file(tmp_path: Path, monkeypatch):
    """Phase A4 must invoke the LLM once per target file, not once per fixture."""
    tasks = _make_tasks()
    sut_root = tmp_path / "sut"
    workdir = tmp_path / "wd"
    agents_root = tmp_path / "agents"
    for p in (sut_root, workdir, agents_root):
        p.mkdir()
    (agents_root / "codegen-pom-extender.agent.md").write_text("agent", encoding="utf-8")

    calls_per_file: dict[str, list[dict]] = {}

    async def _stub(agent_path, *, workdir, user_prompt, inputs, step, timeout_s, max_tokens):
        specs = json.loads(inputs["fixture_specs.json"])
        names = [s["name"] for s in specs]
        # The prompt is the only signal that says which file the agent
        # is targeting (the agent uses `existing_file.py` if present).
        # Identify the call by the fixture name list.
        # We recover the file path by checking which of our planned
        # files matches this name list.
        file_match = None
        for fp, fts in _group_fixture_tasks_by_file(tasks).items():
            if sorted(t.name for t in fts) == sorted(names):
                file_match = fp
                break
        assert file_match is not None, (
            f"Unexpected fixture-name set in LLM call: {names}"
        )
        calls_per_file.setdefault(file_match, []).append({
            "names": names, "prompt": user_prompt,
        })
        return _StubResult(success=True, final_text=_all_fixtures_body(names))

    monkeypatch.setattr(
        "worca_t.steps.s08_codegen.call_reasoning_llm", _stub,
    )

    results = asyncio.run(_create_fixtures(
        tasks, sut_root, workdir, agents_root,
        active_module=None, step=8, rules_content="",
    ))

    # Exactly one call per file
    assert len(calls_per_file) == 2
    for fp, calls in calls_per_file.items():
        assert len(calls) == 1, (
            f"{fp} got {len(calls)} calls — Phase A4 race regression"
        )

    # Both target files written to disk
    nav_path = sut_root / "tests" / "fixtures" / "worca_nav.py"
    shared_path = sut_root / "tests" / "fixtures" / "worca_shared.py"
    assert nav_path.is_file()
    assert shared_path.is_file()

    # All 6 nav fixtures present in the written file
    nav_body = nav_path.read_text(encoding="utf-8")
    for name in [
        "gemini_nav_locale_en", "gemini_nav_locale_de", "gtag_spy",
        "gtag_removed", "unauthenticated_context", "mobile_viewport",
    ]:
        assert f"def {name}" in nav_body, (
            f"missing `def {name}` in nav fixtures file — "
            f"per-file collation broken"
        )

    # All file results report success
    assert all(ok for _, ok in results)


def test_create_fixtures_reports_failure_when_agent_drops_a_name(
    tmp_path: Path, monkeypatch,
):
    """If the LLM returns a file that's missing one of the requested fixtures,
    `_create_fixtures` must return ok=False so reconcile (Fix 2) catches it."""
    tasks = [
        _FixtureTask(name="alpha", at="tests/fixtures/x.py"),
        _FixtureTask(name="beta", at="tests/fixtures/x.py"),
    ]
    sut_root = tmp_path / "sut"
    workdir = tmp_path / "wd"
    agents_root = tmp_path / "agents"
    for p in (sut_root, workdir, agents_root):
        p.mkdir()
    (agents_root / "codegen-pom-extender.agent.md").write_text("agent", encoding="utf-8")

    async def _stub(agent_path, *, workdir, user_prompt, inputs, step, timeout_s, max_tokens):
        # Only return `alpha` — drop `beta` on purpose.
        return _StubResult(success=True, final_text=_all_fixtures_body(["alpha"]))

    monkeypatch.setattr(
        "worca_t.steps.s08_codegen.call_reasoning_llm", _stub,
    )

    results = asyncio.run(_create_fixtures(
        tasks, sut_root, workdir, agents_root,
        active_module=None, step=8, rules_content="",
    ))
    assert results == [("tests/fixtures/x.py", False)]


def test_create_fixtures_rolls_back_on_syntax_error(
    tmp_path: Path, monkeypatch,
):
    """Run 20260614-190647 wrote a file with an unclosed `parser.addoption(`
    (the LLM copied a truncated `style_reference.py` verbatim). The regex
    fixture-name check passed, but the reconciler's `ast.parse` failed and
    every declared fixture surfaced as `fixture_file_missing`. The
    `ast.parse` rollback gate must catch broken output before it slips
    through, and a newly-created file must not survive on disk."""
    tasks = [
        _FixtureTask(name="alpha", at="tests/fixtures/y.py"),
        _FixtureTask(name="beta", at="tests/fixtures/y.py"),
    ]
    sut_root = tmp_path / "sut"
    workdir = tmp_path / "wd"
    agents_root = tmp_path / "agents"
    for p in (sut_root, workdir, agents_root):
        p.mkdir()
    (agents_root / "codegen-pom-extender.agent.md").write_text("agent", encoding="utf-8")

    broken_body = (
        "import pytest\n"
        "\n"
        "parser.addoption(\n"  # unclosed call → SyntaxError, matches the real incident
        "\n"
        "@pytest.fixture(scope='function')\n"
        "def alpha():\n    yield\n"
        "\n"
        "@pytest.fixture(scope='function')\n"
        "def beta():\n    yield\n"
    )

    async def _stub(agent_path, *, workdir, user_prompt, inputs, step, timeout_s, max_tokens):
        return _StubResult(success=True, final_text=broken_body)

    monkeypatch.setattr(
        "worca_t.steps.s08_codegen.call_reasoning_llm", _stub,
    )

    results = asyncio.run(_create_fixtures(
        tasks, sut_root, workdir, agents_root,
        active_module=None, step=8, rules_content="",
    ))
    assert results == [("tests/fixtures/y.py", False)]
    # File did not exist before; rollback must remove it so the next
    # attempt starts clean rather than re-reading the broken bytes.
    assert not (sut_root / "tests" / "fixtures" / "y.py").exists()


def test_create_fixtures_restores_prior_content_on_syntax_error(
    tmp_path: Path, monkeypatch,
):
    """When the target already existed, rollback must restore the prior
    bytes so the next retry sees a parseable file, not the broken write."""
    tasks = [_FixtureTask(name="alpha", at="tests/fixtures/z.py")]
    sut_root = tmp_path / "sut"
    workdir = tmp_path / "wd"
    agents_root = tmp_path / "agents"
    for p in (sut_root, workdir, agents_root):
        p.mkdir()
    (agents_root / "codegen-pom-extender.agent.md").write_text("agent", encoding="utf-8")

    target = sut_root / "tests" / "fixtures" / "z.py"
    target.parent.mkdir(parents=True)
    prior = "import pytest\n\n@pytest.fixture\ndef preexisting():\n    yield\n"
    target.write_text(prior, encoding="utf-8")

    async def _stub(agent_path, *, workdir, user_prompt, inputs, step, timeout_s, max_tokens):
        return _StubResult(
            success=True,
            final_text="import pytest\n\nparser.addoption(\n\n@pytest.fixture\ndef alpha():\n    yield\n",
        )

    monkeypatch.setattr(
        "worca_t.steps.s08_codegen.call_reasoning_llm", _stub,
    )

    results = asyncio.run(_create_fixtures(
        tasks, sut_root, workdir, agents_root,
        active_module=None, step=8, rules_content="",
    ))
    assert results == [("tests/fixtures/z.py", False)]
    assert target.read_text(encoding="utf-8") == prior
