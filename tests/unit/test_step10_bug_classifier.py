"""Step 10 bug-classifier tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.schemas import is_valid
from worca_t.steps.base import StepContext
from worca_t.steps.s10_bug_classifier import (
    BugClassifierStep,
    _agent_report_is_usable,
    _categorize_attachments,
    _empty_report,
    _load_heal_log,
    _render_markdown,
    _synthesize,
)
from worca_t.workspace import create_workspace

from ._fake_anthropic import disable_vertex_env, install_fake_anthropic

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_categorize_attachments_buckets_by_type():
    items = [
        {"type": "screenshot", "path": "a.png"},
        {"type": "trace", "path": "t.zip"},
        {"type": "video", "path": "v.mp4"},
        {"type": "log", "path": "x.log"},
        {"type": "other", "path": "z"},
        {"type": "screenshot"},  # no path -> dropped
    ]
    out = _categorize_attachments(items)
    assert out["screenshots"] == ["a.png"]
    assert out["traces"] == ["t.zip"]
    assert out["videos"] == ["v.mp4"]
    assert out["logs"] == ["x.log"]


def test_empty_report_validates_against_schema():
    rep = _empty_report("run-x")
    ok, err = is_valid(rep, "bug-reports")
    assert ok, err


def test_synthesize_emits_valid_schema_with_counts():
    candidates = [
        {
            "test_id": "T-a", "title": "logs in", "status": "failed",
            "message": "no element", "attachments": [
                {"type": "screenshot", "path": "s.png"}
            ],
        },
        {"test_id": "T-b", "title": "checkout", "status": "error"},
    ]
    rep = _synthesize("run-1", candidates, {"T-a": {"attempted": True, "applied": False}})
    ok, err = is_valid(rep, "bug-reports")
    assert ok, err
    assert rep["summary"]["total_failures"] == 2
    assert rep["bugs"][0]["self_heal"]["attempted"] is True
    assert rep["bugs"][0]["attachments"]["screenshots"] == ["s.png"]
    ids = [b["id"] for b in rep["bugs"]]
    assert ids == ["BUG-run-1-001", "BUG-run-1-002"]


def test_render_markdown_includes_key_fields():
    rep = _synthesize("r", [{"test_id": "T-x", "title": "wow", "status": "failed"}], {})
    md = _render_markdown(rep)
    assert "T-x" in md
    assert "wow" in md
    assert "Severity" in md or "severity" in md.lower()
    # Empty report uses the no-failures marker.
    md_empty = _render_markdown(_empty_report("r"))
    assert "No failing tests" in md_empty


def test_agent_report_is_usable_checks_count_and_schema():
    candidates = [{"test_id": "T-a", "title": "t", "status": "failed"}]
    good = _synthesize("r", candidates, {})
    ok, err = _agent_report_is_usable(good, expected_count=1)
    assert ok, err
    ok, err = _agent_report_is_usable(good, expected_count=2)
    assert not ok and "count" in err
    ok, err = _agent_report_is_usable({"nope": True}, expected_count=0)
    assert not ok and "schema" in err
    ok, err = _agent_report_is_usable("string", expected_count=0)
    assert not ok and "object" in err


def test_load_heal_log_parses_jsonl(tmp_path: Path):
    p = tmp_path / "heal.jsonl"
    p.write_text(
        json.dumps({"test_id": "T-a", "applied": True}) + "\n"
        + "garbage line\n"
        + json.dumps({"test_id": "T-b", "applied": False}) + "\n",
        encoding="utf-8",
    )
    out = _load_heal_log(p)
    assert out["T-a"] == {"attempted": True, "applied": True}
    assert out["T-b"] == {"attempted": True, "applied": False}


# ---------------------------------------------------------------------------
# Step integration
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _seed_run(ctx: StepContext, *, candidates: list[dict]) -> None:
    s9 = ctx.workspace.step_dir(9)
    s9.mkdir(parents=True, exist_ok=True)
    (s9 / "run-results.json").write_text(
        json.dumps({
            "framework": "pytest",
            "command": "pytest",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "results": [],
        }),
        encoding="utf-8",
    )
    (s9 / "bug-candidates.json").write_text(
        json.dumps({"candidates": candidates}), encoding="utf-8"
    )


async def test_step10_short_circuits_when_no_candidates(tmp_path: Path):
    ctx = _ctx(tmp_path)
    _seed_run(ctx, candidates=[])
    result = await BugClassifierStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    out = ctx.workspace.step_dir(10)
    rep = json.loads((out / "bug-reports.json").read_text(encoding="utf-8"))
    assert rep["summary"]["total_failures"] == 0
    md = (out / "bug-reports.md").read_text(encoding="utf-8")
    assert "No failing tests" in md


async def test_step10_uses_agent_output_when_valid(tmp_path: Path, monkeypatch):
    """Agent returns schema-valid JSON via structured outputs; step uses it."""
    ctx = _ctx(tmp_path)
    candidates = [
        {"test_id": "T-a", "title": "logs in", "status": "failed", "message": "x"},
    ]
    _seed_run(ctx, candidates=candidates)

    # Build a schema-valid agent payload referencing the run id.
    agent_payload = _synthesize(ctx.workspace.run_id, candidates, {})
    agent_payload["bugs"][0]["rationale"] = "agent-classified"
    # Direct SDK returns JSON as the response text (structured outputs).
    install_fake_anthropic(monkeypatch, text=json.dumps(agent_payload))

    result = await BugClassifierStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    out = ctx.workspace.step_dir(10)
    rep = json.loads((out / "bug-reports.json").read_text(encoding="utf-8"))
    assert rep["bugs"][0]["rationale"] == "agent-classified"
    # Markdown is now ALWAYS rendered locally from the JSON for consistency.
    md = (out / "bug-reports.md").read_text(encoding="utf-8")
    assert "T-a" in md
    assert "logs in" in md


async def test_step10_passes_bug_reports_schema_to_reasoning_llm(
    tmp_path: Path, monkeypatch
):
    """On the standard Anthropic API, the step enables structured outputs by
    passing the bug-reports schema via ``output_config.format``. (Vertex
    backends suppress this — see ``test_output_config_skipped_on_vertex``
    in test_reasoning_module.py.)"""
    disable_vertex_env(monkeypatch)
    ctx = _ctx(tmp_path)
    candidates = [{"test_id": "T-a", "title": "t", "status": "failed"}]
    _seed_run(ctx, candidates=candidates)

    agent_payload = _synthesize(ctx.workspace.run_id, candidates, {})
    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(agent_payload), on_call=captured.update
    )

    result = await BugClassifierStep().run(ctx)
    assert result.success

    # The reasoning module sets output_config when output_schema is provided.
    assert "output_config" in captured, (
        "step09 must pass a JSON schema to enable structured outputs"
    )
    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    # Bug-reports schema requires these fields — quick smoke check.
    schema = fmt["schema"]
    assert set(schema.get("required", [])) >= {
        "run_id", "generated_at", "summary", "bugs"
    }


async def test_step10_inlines_inputs_into_user_prompt(
    tmp_path: Path, monkeypatch
):
    """Inputs (bug-candidates.json, run-results.json) are inlined into the
    user message, not staged in the workdir (the latter is the old run_agent
    pattern; direct SDK uses inlining)."""
    ctx = _ctx(tmp_path)
    candidates = [
        {"test_id": "T-zzz-distinctive", "title": "t", "status": "failed"},
    ]
    _seed_run(ctx, candidates=candidates)

    agent_payload = _synthesize(ctx.workspace.run_id, candidates, {})
    captured: dict = {}
    install_fake_anthropic(
        monkeypatch, text=json.dumps(agent_payload), on_call=captured.update
    )

    result = await BugClassifierStep().run(ctx)
    assert result.success

    user_content = captured["messages"][-1]["content"]
    # The distinctive test_id only appears in bug-candidates.json — if
    # it's in the prompt, inlining worked.
    assert "T-zzz-distinctive" in user_content
    assert "bug-candidates.json" in user_content


async def test_step10_falls_back_when_agent_output_invalid(
    tmp_path: Path, monkeypatch
):
    """Agent returns garbage text — fallback path emits deterministic synthesis."""
    ctx = _ctx(tmp_path)
    candidates = [
        {"test_id": "T-a", "title": "logs in", "status": "failed", "message": "x"},
    ]
    _seed_run(ctx, candidates=candidates)

    install_fake_anthropic(monkeypatch, text="not json{")

    result = await BugClassifierStep().run(ctx)
    assert result.success
    assert result.status == "warned"
    rep = json.loads(
        (ctx.workspace.step_dir(10) / "bug-reports.json").read_text(encoding="utf-8")
    )
    ok, err = is_valid(rep, "bug-reports")
    assert ok, err
    assert rep["summary"]["total_failures"] == 1
    assert rep["bugs"][0]["rationale"].startswith("auto-classified")


async def test_step10_falls_back_when_agent_returns_empty(
    tmp_path: Path, monkeypatch
):
    """Agent returns empty text — fallback path kicks in."""
    ctx = _ctx(tmp_path)
    candidates = [{"test_id": "T-a", "title": "t", "status": "failed"}]
    _seed_run(ctx, candidates=candidates)

    install_fake_anthropic(monkeypatch, text="")

    result = await BugClassifierStep().run(ctx)
    assert result.success
    assert result.status == "warned"
    rep = json.loads(
        (ctx.workspace.step_dir(10) / "bug-reports.json").read_text(encoding="utf-8")
    )
    assert rep["bugs"][0]["rationale"].startswith("auto-classified")


async def test_step10_falls_back_when_agent_count_mismatches(
    tmp_path: Path, monkeypatch
):
    """Agent returns valid JSON but wrong bug count — usability check rejects it."""
    ctx = _ctx(tmp_path)
    candidates = [
        {"test_id": "T-a", "title": "t", "status": "failed"},
        {"test_id": "T-b", "title": "u", "status": "failed"},
    ]
    _seed_run(ctx, candidates=candidates)

    # Agent classifies only one of the two candidates — schema-valid but
    # count-wrong. _agent_report_is_usable rejects this.
    short_payload = _synthesize(ctx.workspace.run_id, candidates[:1], {})
    install_fake_anthropic(monkeypatch, text=json.dumps(short_payload))

    result = await BugClassifierStep().run(ctx)
    assert result.success
    assert result.status == "warned"
    rep = json.loads(
        (ctx.workspace.step_dir(10) / "bug-reports.json").read_text(encoding="utf-8")
    )
    # Fallback emits BOTH candidates with auto-classified rationale.
    assert rep["summary"]["total_failures"] == 2
    assert all(b["rationale"].startswith("auto-classified") for b in rep["bugs"])
