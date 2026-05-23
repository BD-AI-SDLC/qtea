"""Step 8 locator-resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s08_locator_resolution import (
    LocatorResolutionStep,
    _apply_patches,
    _build_user_prompt,
    _ensure_files_for,
    _is_xpath_replacement,
    _rank_strategy,
    _tests_with_tbd,
)
from worca_t.workspace import create_workspace

from ._fake_claude import install_on_path, write_fake_claude

# ---------------------------------------------------------------------------
# Unit tests (pure functions)
# ---------------------------------------------------------------------------


def test_is_xpath_replacement_detects_all_flavours():
    assert _is_xpath_replacement("xpath=//button")
    assert _is_xpath_replacement("//div[@id='a']")
    assert _is_xpath_replacement("By.XPATH, '//x'")
    assert not _is_xpath_replacement("#username")
    assert not _is_xpath_replacement("[data-testid=username]")


def test_rank_strategy_orders_correctly():
    assert _rank_strategy("id") < _rank_strategy("data-testid") < _rank_strategy("css")
    assert _rank_strategy("nope") >= _rank_strategy("css")


def test_tests_with_tbd_filters():
    index = {
        "tests": [
            {"id": "T-a", "tbd_markers": [{"line": 1, "raw": "TBD"}]},
            {"id": "T-b", "tbd_markers": []},
        ]
    }
    out = _tests_with_tbd(index)
    assert [t["id"] for t in out] == ["T-a"]


def test_build_user_prompt_lists_tests_and_url():
    index = {
        "tests": [
            {
                "id": "T-login-1",
                "file": "tests/login.spec.ts",
                "tbd_markers": [{"line": 7, "raw": "TBD_LOCATOR"}],
            }
        ]
    }
    prompt = _build_user_prompt(index, "https://example.test")
    assert "T-login-1" in prompt
    assert "tests/login.spec.ts" in prompt
    assert "https://example.test" in prompt
    assert "Never propose XPath" in prompt


def test_ensure_files_for_uses_test_id_lookup():
    index = {"tests": [{"id": "T-1", "file": "tests/a.spec.ts"}]}
    resolutions = [{"test_id": "T-1", "items": []}]
    _ensure_files_for(resolutions, index)
    assert resolutions[0]["file"] == "tests/a.spec.ts"


# ---------------------------------------------------------------------------
# Patcher tests
# ---------------------------------------------------------------------------


def test_apply_patches_replaces_tbd_token(tmp_path: Path):
    f = tmp_path / "login.spec.ts"
    f.write_text(
        """test('x', async ({ page }) => {\n  await page.locator('TBD_LOCATOR').click();\n});\n""",
        encoding="utf-8",
    )
    resolutions = [
        {
            "test_id": "T-x",
            "file": "login.spec.ts",
            "items": [
                {
                    "tbd": "TBD_LOCATOR",
                    "replacement": "#submit",
                    "strategy": "id",
                    "line": 2,
                    "confidence": 0.9,
                }
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is True
    assert "#submit" in f.read_text(encoding="utf-8")
    assert "TBD_LOCATOR" not in f.read_text(encoding="utf-8")


def test_apply_patches_rejects_xpath(tmp_path: Path):
    f = tmp_path / "x.spec.ts"
    f.write_text("await page.locator('TBD_LOCATOR').click();\n", encoding="utf-8")
    resolutions = [
        {
            "test_id": "T-x",
            "file": "x.spec.ts",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "xpath=//x", "strategy": "css"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "xpath" in applied[0]["skip_reason"]
    assert "TBD_LOCATOR" in f.read_text(encoding="utf-8")


def test_apply_patches_rejects_unknown_strategy(tmp_path: Path):
    f = tmp_path / "x.spec.ts"
    f.write_text("TBD_LOCATOR\n", encoding="utf-8")
    resolutions = [
        {
            "test_id": "T-x",
            "file": "x.spec.ts",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "magic"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "unknown strategy" in applied[0]["skip_reason"]
    assert "TBD_LOCATOR" in f.read_text(encoding="utf-8")


def test_apply_patches_missing_token_recorded(tmp_path: Path):
    f = tmp_path / "x.spec.ts"
    f.write_text("nothing here\n", encoding="utf-8")
    resolutions = [
        {
            "test_id": "T-x",
            "file": "x.spec.ts",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#submit", "strategy": "id"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "token not found" in applied[0]["skip_reason"]


def test_apply_patches_missing_file_recorded(tmp_path: Path):
    resolutions = [
        {
            "test_id": "T-x",
            "file": "nope.spec.ts",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "file not found" in applied[0]["skip_reason"]


# ---------------------------------------------------------------------------
# Step integration tests
# ---------------------------------------------------------------------------


GOOD_TS_WITH_TBD = """\
test('should login', async ({ page }) => {
  await page.locator('TBD_LOCATOR').click();
});
"""


def _seed_step7(ws_path: Path, *, tbd_file: str = "login.spec.ts", content: str = GOOD_TS_WITH_TBD):
    step7 = ws_path / "artifacts" / "step07"
    (step7 / "tests").mkdir(parents=True, exist_ok=True)
    (step7 / "tests" / tbd_file).write_text(content, encoding="utf-8")
    index = {
        "framework": "playwright-ts",
        "test_root": str(step7 / "tests"),
        "totals": {"files": 1, "tests": 1, "tbd_locators": 1},
        "files": [tbd_file],
        "tests": [
            {
                "id": "T-login",
                "name": "should login",
                "file": tbd_file,
                "line": 1,
                "status": "pending",
                "tags": [],
                "tc_refs": [],
                "locator_candidates": [],
                "tbd_markers": [{"line": 2, "raw": "TBD_LOCATOR", "context": ""}],
            }
        ],
        "violations": [],
    }
    (step7 / "tests-with-tbd.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


def test_step08_requires_step07_outputs(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "step 8 requires" in (result.error or "")


def test_step08_short_circuits_when_no_tbd(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    step7 = ctx.workspace.root / "artifacts" / "step07"
    (step7 / "tests").mkdir(parents=True, exist_ok=True)
    (step7 / "tests" / "ok.spec.ts").write_text(
        "test('a', async ({ page }) => { await page.getByRole('button').click(); });\n",
        encoding="utf-8",
    )
    (step7 / "tests-with-tbd.json").write_text(
        json.dumps(
            {
                "framework": "playwright-ts",
                "test_root": str(step7 / "tests"),
                "totals": {"files": 1, "tests": 1, "tbd_locators": 0},
                "files": ["ok.spec.ts"],
                "tests": [
                    {
                        "id": "T-a",
                        "name": "a",
                        "file": "ok.spec.ts",
                        "line": 1,
                        "status": "pending",
                        "tags": [],
                        "tc_refs": [],
                        "locator_candidates": [],
                        "tbd_markers": [],
                    }
                ],
                "violations": [],
            }
        ),
        encoding="utf-8",
    )

    # Even though we install a fake claude, it must NOT be invoked.
    install_on_path(
        monkeypatch,
        write_fake_claude(tmp_path / "bin", events=[{"type": "result", "result": "ok"}], exit_code=99),
    )

    result = LocatorResolutionStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    res = json.loads((ctx.workspace.step_dir(8) / "locator-resolution.json").read_text(encoding="utf-8"))
    assert res["resolutions"] == []
    assert res["totals"]["tests_with_tbd"] == 0


def test_step08_happy_path_patches_files(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace.root)
    monkeypatch.setenv("SUT_BASE_URL", "https://example.test")

    resolution = {
        "base_url": "https://example.test",
        "resolutions": [
            {
                "test_id": "T-login",
                "file": "login.spec.ts",
                "items": [
                    {
                        "tbd": "TBD_LOCATOR",
                        "replacement": "#submit",
                        "strategy": "id",
                        "line": 2,
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }
    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )
    install_on_path(monkeypatch, bin_path)

    result = LocatorResolutionStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    out = ctx.workspace.step_dir(8)
    patched = (out / "tests" / "login.spec.ts").read_text(encoding="utf-8")
    assert "TBD_LOCATOR" not in patched
    assert "#submit" in patched
    payload = json.loads((out / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 1
    assert payload["totals"]["skipped"] == 0
    reidx = json.loads((out / "tests-with-tbd.json").read_text(encoding="utf-8"))
    assert reidx["totals"]["tbd_locators"] == 0
    assert not reidx["violations"]


def test_step08_rejects_xpath_replacement_via_patcher(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace.root)

    resolution = {
        "resolutions": [
            {
                "test_id": "T-login",
                "file": "login.spec.ts",
                "items": [
                    {"tbd": "TBD_LOCATOR", "replacement": "//button", "strategy": "css"}
                ],
            }
        ]
    }
    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )
    install_on_path(monkeypatch, bin_path)

    result = LocatorResolutionStep().run(ctx)
    # Step succeeds but warns: token stays, applied=0, skipped=1.
    assert result.success
    assert result.status == "warned"
    out = ctx.workspace.step_dir(8)
    payload = json.loads((out / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 0
    assert payload["totals"]["skipped"] == 1
    # File unchanged: TBD still present.
    assert "TBD_LOCATOR" in (out / "tests" / "login.spec.ts").read_text(encoding="utf-8")


def test_step08_agent_invalid_json_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace.root)

    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "ok"}],
        files={"locator-resolution.json": "not json{"},
    )
    install_on_path(monkeypatch, bin_path)

    result = LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "not valid JSON" in (result.error or "")


def test_step08_agent_no_output_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace.root)

    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "ok"}],
        files={},
    )
    install_on_path(monkeypatch, bin_path)

    result = LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "locator-resolution.json" in (result.error or "")


def test_step08_resolves_file_from_test_id_when_omitted(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace.root)

    # File field deliberately omitted; must be filled in by _ensure_files_for.
    resolution = {
        "resolutions": [
            {
                "test_id": "T-login",
                "items": [
                    {"tbd": "TBD_LOCATOR", "replacement": "#submit", "strategy": "id"}
                ],
            }
        ]
    }
    bin_path = write_fake_claude(
        tmp_path / "bin",
        events=[{"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )
    install_on_path(monkeypatch, bin_path)

    result = LocatorResolutionStep().run(ctx)
    assert result.success
    payload = json.loads((ctx.workspace.step_dir(8) / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 1
