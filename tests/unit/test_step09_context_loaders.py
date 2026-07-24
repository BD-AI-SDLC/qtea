"""Regression tests for Step 9's `_load_assertion_oracle`.

Prior to this fix, the oracle extractor only read the POM lane's
``page_objects[].missing_methods[]`` shape. The exemplar (non-POM) lane
emits ``reusable_units[].missing_behaviors[]`` instead — identical
``kind``/``acceptance_criteria`` field shape, per
``schemas/code-modification-plan.schema.json`` — so every exemplar-lane run
silently got an empty oracle (`expected_values == set()`), which fails OPEN
the "assertion corrected, never weakened" heal guarantee for the entire
lane.
"""

from __future__ import annotations

import json
from pathlib import Path

from qtea.checkpoints import RunState
from qtea.pipeline import PipelineOptions
from qtea.steps.base import StepContext
from qtea.steps.s09.context_loaders import _load_assertion_oracle
from qtea.workspace import Workspace


def _ctx(tmp_path: Path) -> StepContext:
    ws = Workspace(root=tmp_path / ".ws", run_id="test-run-id")
    ws.ensure_layout()
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def _write_plan(ctx: StepContext, plan: dict) -> None:
    p = ctx.workspace.step_dir(7) / "code-modification-plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")


def test_oracle_empty_when_no_plan(tmp_path: Path):
    ctx = _ctx(tmp_path)
    oracle = _load_assertion_oracle(ctx)
    assert oracle == {"expected_values": set(), "by_method": {}}


def test_oracle_extracts_pom_lane_assertions(tmp_path: Path):
    """Baseline — the pre-existing POM-lane behavior must be unchanged."""
    ctx = _ctx(tmp_path)
    _write_plan(ctx, {
        "test_cases": [{
            "id": "TC-1",
            "page_objects": [{
                "name": "LoginPage",
                "missing_methods": [{
                    "name": "errorBanner", "kind": "assertion",
                    "acceptance_criteria": [
                        {"check": "exact_text", "expected_literal": "Invalid credentials"},
                    ],
                }],
            }],
        }],
    })
    oracle = _load_assertion_oracle(ctx)
    assert "Invalid credentials" in oracle["expected_values"]
    assert "errorBanner" in oracle["by_method"]


def test_oracle_extracts_exemplar_lane_assertions(tmp_path: Path):
    """The fix under test: exemplar-lane `reusable_units[].missing_behaviors[]`
    must populate the oracle exactly like the POM lane does."""
    ctx = _ctx(tmp_path)
    _write_plan(ctx, {
        "test_cases": [{
            "id": "TC-2",
            "reusable_units": [{
                "name": "CheckDiscountRate", "source": "create", "category": "question",
                "at": "framework/questions/check_discount_rate.py",
                "missing_behaviors": [{
                    "name": "answered_by", "signature": "answered_by(self, actor)",
                    "kind": "assertion",
                    "acceptance_criteria": [
                        {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
                    ],
                }],
            }],
        }],
    })
    oracle = _load_assertion_oracle(ctx)
    assert "EXPECTED_DISCOUNT_RATE" in oracle["expected_values"]
    assert "answered_by" in oracle["by_method"]


def test_oracle_combines_both_lanes_in_same_test_case(tmp_path: Path):
    """A test case may (in principle) reference both a pre-existing POM
    probe and a new exemplar-lane unit — both must contribute to the
    oracle."""
    ctx = _ctx(tmp_path)
    _write_plan(ctx, {
        "test_cases": [{
            "id": "TC-3",
            "page_objects": [{
                "name": "LoginPage",
                "missing_methods": [{
                    "name": "errorBanner", "kind": "assertion",
                    "acceptance_criteria": [
                        {"check": "exact_text", "expected_literal": "Invalid credentials"},
                    ],
                }],
            }],
            "reusable_units": [{
                "name": "CheckDiscountRate", "source": "create", "category": "question",
                "at": "framework/questions/check_discount_rate.py",
                "missing_behaviors": [{
                    "name": "answered_by", "signature": "answered_by(self, actor)",
                    "kind": "assertion",
                    "acceptance_criteria": [
                        {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
                    ],
                }],
            }],
        }],
    })
    oracle = _load_assertion_oracle(ctx)
    assert "Invalid credentials" in oracle["expected_values"]
    assert "EXPECTED_DISCOUNT_RATE" in oracle["expected_values"]


def test_oracle_ignores_non_assertion_behaviors(tmp_path: Path):
    """`kind: action|query` reusable-unit behaviors are not part of the
    assertion oracle — mirrors the POM lane's `kind` filter."""
    ctx = _ctx(tmp_path)
    _write_plan(ctx, {
        "test_cases": [{
            "id": "TC-4",
            "reusable_units": [{
                "name": "OpenCatalog", "source": "create", "category": "task",
                "at": "framework/tasks/open_catalog.py",
                "missing_behaviors": [
                    {"name": "perform_as", "signature": "perform_as(self, actor)", "kind": "action"},
                ],
            }],
        }],
    })
    oracle = _load_assertion_oracle(ctx)
    assert oracle == {"expected_values": set(), "by_method": {}}


def test_oracle_fails_open_when_acceptance_criteria_absent(tmp_path: Path):
    """The exemplar lane's schema has no `if kind==assertion then require
    acceptance_criteria` constraint (unlike the POM lane) — an assertion
    behavior with no criteria must not raise, just contribute nothing."""
    ctx = _ctx(tmp_path)
    _write_plan(ctx, {
        "test_cases": [{
            "id": "TC-5",
            "reusable_units": [{
                "name": "CheckBanner", "source": "create", "category": "question",
                "at": "framework/questions/check_banner.py",
                "missing_behaviors": [
                    {"name": "answered_by", "signature": "answered_by(self, actor)", "kind": "assertion"},
                ],
            }],
        }],
    })
    oracle = _load_assertion_oracle(ctx)
    assert oracle["expected_values"] == set()
    assert oracle["by_method"]["answered_by"] == []
