"""Regression tests for `purpose_judge.judge_purpose_fidelity`'s local
schema re-validation of the LLM verdict.

Vertex/BMF can't enforce `output_schema` server-side (see llm/reasoning.py),
so a malformed verdict batch must be discarded rather than silently
extended into `all_verdicts` — see the matching gap fixed in
`assertion_judge.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qtea.purpose_judge import judge_purpose_fidelity

_POM_SOURCE = (
    "class LoginPage:\n"
    "    def errorBanner(self):\n"
    "        return self.page.locator('#error')\n"
)


def _judge_dirs(tmp_path: Path) -> dict:
    sut_root = tmp_path / "sut"
    sut_root.mkdir()
    pom_rel = "pages/login_page.py"
    pom_path = sut_root / pom_rel
    pom_path.parent.mkdir(parents=True)
    pom_path.write_text(_POM_SOURCE, encoding="utf-8")

    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    (agents_root / "purpose-fidelity-judge.agent.md").write_text("persona", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    pom_tasks = {
        pom_rel: SimpleNamespace(
            pom_name="LoginPage",
            missing_methods=[{
                "name": "errorBanner",
                "kind": "action",
                "purpose": "reveals the invalid-credentials error banner",
            }],
        ),
    }
    return {
        "pom_tasks": pom_tasks,
        "sut_root": sut_root,
        "agents_root": agents_root,
        "out_dir": out_dir,
        "workdir": tmp_path / "work",
        "language": "python",
    }


async def test_judge_purpose_fidelity_discards_verdicts_missing_required_field(
    tmp_path: Path,
):
    dirs = _judge_dirs(tmp_path)
    malformed = {"verdicts": [{
        "method": "errorBanner",
        "pom": "LoginPage",
        # fulfills_purpose intentionally omitted (schema-required)
        "weakness": "none",
    }]}
    fake_result = SimpleNamespace(success=True, final_text=json.dumps(malformed), error=None)

    with patch(
        "qtea.purpose_judge.call_reasoning_llm", new=AsyncMock(return_value=fake_result),
    ):
        out = await judge_purpose_fidelity(**dirs)

    # No verdict survives validation, so no shadow output is produced.
    assert out is None
    assert not (dirs["out_dir"] / "purpose-fidelity-shadow.json").exists()


async def test_judge_purpose_fidelity_accepts_well_formed_verdict(tmp_path: Path):
    dirs = _judge_dirs(tmp_path)
    good = {"verdicts": [{
        "method": "errorBanner",
        "pom": "LoginPage",
        "fulfills_purpose": True,
        "weakness": "none",
    }]}
    fake_result = SimpleNamespace(success=True, final_text=json.dumps(good), error=None)

    with patch(
        "qtea.purpose_judge.call_reasoning_llm", new=AsyncMock(return_value=fake_result),
    ):
        out = await judge_purpose_fidelity(**dirs)

    assert out is not None
    assert out["summary"]["total"] == 1
    assert (dirs["out_dir"] / "purpose-fidelity-shadow.json").exists()
