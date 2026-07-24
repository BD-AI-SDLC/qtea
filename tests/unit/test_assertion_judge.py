"""Regression tests for `assertion_judge._collect_oracle`.

Mirrors `test_step09_context_loaders.py` — `_collect_oracle` had the same
POM-only blind spot as Step 9's `_load_assertion_oracle`: it only read
`page_objects[].missing_methods[]`, so the shadow judge saw an empty oracle
for every exemplar-lane run.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qtea.assertion_judge import (
    _collect_oracle,
    _collect_sequence_oracle,
    judge_assertions_shadow,
)


def test_collect_oracle_extracts_pom_lane_assertions():
    plan = {
        "test_cases": [{
            "id": "TC-1",
            "page_objects": [{
                "name": "LoginPage",
                "missing_methods": [{
                    "name": "errorBanner", "signature": "()", "kind": "assertion",
                    "purpose": "shows the invalid-credentials error",
                    "acceptance_criteria": [
                        {"check": "exact_text", "expected_literal": "Invalid credentials"},
                    ],
                }],
            }],
        }],
    }
    out = _collect_oracle(plan)
    assert len(out) == 1
    assert out[0]["pom"] == "LoginPage"
    assert out[0]["method"] == "errorBanner"
    assert out[0]["acceptance_criteria"][0]["expected_literal"] == "Invalid credentials"


def test_collect_oracle_extracts_exemplar_lane_assertions():
    plan = {
        "test_cases": [{
            "id": "TC-2",
            "reusable_units": [{
                "name": "CheckDiscountRate", "source": "create", "category": "question",
                "at": "framework/questions/check_discount_rate.py",
                "missing_behaviors": [{
                    "name": "answered_by", "signature": "answered_by(self, actor)",
                    "kind": "assertion",
                    "purpose": "discount rate matches the strategy",
                    "acceptance_criteria": [
                        {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
                    ],
                }],
            }],
        }],
    }
    out = _collect_oracle(plan)
    assert len(out) == 1
    assert out[0]["pom"] == "CheckDiscountRate"
    assert out[0]["method"] == "answered_by"
    assert out[0]["acceptance_criteria"][0]["expected_symbol"] == "EXPECTED_DISCOUNT_RATE"


def test_collect_oracle_ignores_non_assertion_behaviors():
    plan = {
        "test_cases": [{
            "id": "TC-3",
            "reusable_units": [{
                "name": "OpenCatalog", "source": "create", "category": "task",
                "at": "framework/tasks/open_catalog.py",
                "missing_behaviors": [
                    {"name": "perform_as", "signature": "perform_as(self, actor)", "kind": "action"},
                ],
            }],
        }],
    }
    assert _collect_oracle(plan) == []


def test_collect_oracle_combines_both_lanes():
    plan = {
        "test_cases": [{
            "id": "TC-4",
            "page_objects": [{
                "name": "LoginPage",
                "missing_methods": [{
                    "name": "errorBanner", "signature": "()", "kind": "assertion",
                    "purpose": "shows the invalid-credentials error",
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
                    "purpose": "discount rate matches the strategy",
                    "acceptance_criteria": [
                        {"check": "value_equals", "expected_symbol": "EXPECTED_DISCOUNT_RATE"},
                    ],
                }],
            }],
        }],
    }
    out = _collect_oracle(plan)
    assert {o["method"] for o in out} == {"errorBanner", "answered_by"}


def test_collect_sequence_oracle_extracts_act_steps_ordered():
    plan = {
        "test_cases": [{
            "id": "TC-5",
            "test_functions": [{
                "name": "test_complete_checkout",
                "steps": [
                    {"order": 2, "pom": "ProductPage", "method": "addToCart", "phase": "act"},
                    {"order": 1, "pom": "LoginPage", "method": "logIn", "phase": "arrange"},
                    {"order": 3, "pom": "CheckoutPage", "method": "clickPlaceOrder", "phase": "act"},
                ],
            }],
        }],
    }
    out = _collect_sequence_oracle(plan)
    assert len(out) == 1
    assert out[0]["tc"] == "TC-5"
    assert out[0]["test_function"] == "test_complete_checkout"
    # arrange step excluded; act steps sorted by order
    assert out[0]["steps"] == [
        {"order": 2, "pom": "ProductPage", "method": "addToCart"},
        {"order": 3, "pom": "CheckoutPage", "method": "clickPlaceOrder"},
    ]


def test_collect_sequence_oracle_defaults_missing_phase_to_act():
    plan = {
        "test_cases": [{
            "id": "TC-6",
            "test_functions": [{
                "name": "test_no_explicit_phase",
                "steps": [{"order": 1, "pom": "HomePage", "method": "open"}],
            }],
        }],
    }
    out = _collect_sequence_oracle(plan)
    assert out[0]["steps"] == [{"order": 1, "pom": "HomePage", "method": "open"}]


def test_collect_sequence_oracle_skips_test_functions_without_act_steps():
    plan = {
        "test_cases": [{
            "id": "TC-7",
            "test_functions": [
                {"name": "test_no_steps", "steps": []},
                {"name": "test_arrange_only", "steps": [
                    {"order": 1, "pom": "LoginPage", "method": "logIn", "phase": "arrange"},
                ]},
            ],
        }],
    }
    assert _collect_sequence_oracle(plan) == []


# ---------------------------------------------------------------------------
# judge_assertions_shadow — local schema re-validation of the LLM verdict
# ---------------------------------------------------------------------------

_PLAN_WITH_ORACLE = {
    "test_cases": [{
        "id": "TC-1",
        "page_objects": [{
            "name": "LoginPage",
            "missing_methods": [{
                "name": "errorBanner", "signature": "()", "kind": "assertion",
                "purpose": "shows the invalid-credentials error",
                "acceptance_criteria": [
                    {"check": "exact_text", "expected_literal": "Invalid credentials"},
                ],
            }],
        }],
    }],
}


def _judge_shadow_dirs(tmp_path: Path) -> dict[str, Path]:
    sut_root = tmp_path / "sut"
    sut_root.mkdir()
    (sut_root / "qtea_test_login.py").write_text("def test_login(): pass\n", encoding="utf-8")
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    (agents_root / "assertion-intent-judge.agent.md").write_text("persona", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return {"sut_root": sut_root, "agents_root": agents_root, "out_dir": out_dir,
            "workdir": tmp_path / "work"}


async def test_judge_assertions_shadow_discards_verdicts_missing_required_field(
    tmp_path: Path,
):
    """Vertex/BMF can't enforce ``output_schema`` server-side (see
    llm/reasoning.py), so a verdict missing the schema-required
    ``sequence_complete`` field must be rejected locally rather than fall
    through the ``.get(..., True)`` default and read as 'sequence complete'
    (a silent false-green)."""
    dirs = _judge_shadow_dirs(tmp_path)
    malformed = {"verdicts": [{
        "test": "test_login",
        "verifies_intent": True,
        "binds_oracle": True,
        "weakness": "none",
        # sequence_complete intentionally omitted
    }]}
    fake_result = SimpleNamespace(success=True, final_text=json.dumps(malformed), error=None)

    with patch(
        "qtea.assertion_judge.call_reasoning_llm", new=AsyncMock(return_value=fake_result),
    ):
        out = await judge_assertions_shadow(
            plan_data=_PLAN_WITH_ORACLE,
            strategy_text="",
            language="python",
            **dirs,
        )

    assert out is None
    assert not (dirs["out_dir"] / "assertion-judge-shadow.json").exists()


async def test_judge_assertions_shadow_accepts_well_formed_verdict(tmp_path: Path):
    """Control case: a schema-complete verdict still flows through and is
    written to the shadow file (guards against the validation gate being
    overly strict)."""
    dirs = _judge_shadow_dirs(tmp_path)
    good = {"verdicts": [{
        "test": "test_login",
        "verifies_intent": True,
        "binds_oracle": True,
        "weakness": "none",
        "sequence_complete": True,
    }]}
    fake_result = SimpleNamespace(success=True, final_text=json.dumps(good), error=None)

    with patch(
        "qtea.assertion_judge.call_reasoning_llm", new=AsyncMock(return_value=fake_result),
    ):
        out = await judge_assertions_shadow(
            plan_data=_PLAN_WITH_ORACLE,
            strategy_text="",
            language="python",
            **dirs,
        )

    assert out is not None
    assert out["summary"]["total"] == 1
    assert (dirs["out_dir"] / "assertion-judge-shadow.json").exists()
