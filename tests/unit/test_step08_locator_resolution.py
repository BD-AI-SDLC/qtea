"""Step 8 locator-resolution tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s08_locator_resolution import (
    LocatorResolutionStep,
    _apply_comparison_verdict,
    _apply_patches,
    _audit_snapshot_policy,
    _build_comparison_prompt,
    _build_user_prompt,
    _classify_item,
    _detect_duplicate_replacements,
    _detect_low_confidence_masks,
    _ensure_files_for,
    _find_item_for_clarification,
    _hitl_resolve_unresolvable,
    _infer_strategy,
    _is_assignment_line,
    _is_spec_gap_answer,
    _is_xpath_replacement,
    _parse_clarification_header,
    _rank_strategy,
    _tests_with_tbd,
)
from worca_t.workspace import create_workspace

from ._fake_claude import fake_playwright_mcp_call, install_fake_query
from ._sut_setup import seed_sut

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
# Duplicate-replacement detection (fix B)
# ---------------------------------------------------------------------------


def _payload_with_items(items: list[dict], file_rel: str = "src/loc.py") -> dict:
    """Helper: wrap a flat item list in the payload shape `_detect_*` expects."""
    return {
        "resolutions": [
            {"test_id": "S-loc", "file": file_rel, "items": items},
        ],
    }


def test_detect_duplicate_replacements_groups_same_selector(tmp_path: Path):
    # Two applied items, same file, identical replacement → one duplicate
    # entry with both members. The locator file on disk lets the helper
    # recover the constant names from the lines (GEMINI_NAV_BUTTON, etc.).
    loc_file = tmp_path / "src" / "loc.py"
    loc_file.parent.mkdir(parents=True, exist_ok=True)
    loc_file.write_text(
        "class GeminiNavLocators:\n"
        "    GEMINI_NAV_BUTTON = \"[data-testid='Layout-GeminiEnterprise']\"\n"
        "    GEMINI_NAV_LINK = \"[data-testid='Layout-GeminiEnterprise']\"\n",
        encoding="utf-8",
    )
    payload = _payload_with_items(
        [
            {
                "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Layout-GeminiEnterprise']",
                "strategy": "data-testid", "line": 2, "confidence": 0.95, "applied": True,
            },
            {
                "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Layout-GeminiEnterprise']",
                "strategy": "data-testid", "line": 3, "confidence": 0.95, "applied": True,
            },
        ],
        file_rel="src/loc.py",
    )
    dups = _detect_duplicate_replacements(payload, tests_dir=tmp_path)
    assert len(dups) == 1
    d = dups[0]
    assert d["file"] == "src/loc.py"
    assert d["replacement"] == "[data-testid='Layout-GeminiEnterprise']"
    names = sorted(m["name"] for m in d["members"])
    assert names == ["GEMINI_NAV_BUTTON", "GEMINI_NAV_LINK"]


def test_detect_duplicate_replacements_ignores_distinct_selectors(tmp_path: Path):
    payload = _payload_with_items([
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='button-a']",
            "strategy": "data-testid", "line": 2, "confidence": 0.9, "applied": True,
        },
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='button-b']",
            "strategy": "data-testid", "line": 3, "confidence": 0.9, "applied": True,
        },
    ])
    assert _detect_duplicate_replacements(payload, tests_dir=tmp_path) == []


def test_detect_duplicate_replacements_skips_not_applied_items(tmp_path: Path):
    # An item that the resolver returned but the patcher skipped (applied=False)
    # must NOT count toward duplication — only what actually landed in code
    # matters for this report.
    payload = _payload_with_items([
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='shared']",
            "strategy": "data-testid", "line": 2, "confidence": 0.9, "applied": True,
        },
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='shared']",
            "strategy": "data-testid", "line": 3, "confidence": 0.9, "applied": False,
            "skip_reason": "token not found",
        },
    ])
    assert _detect_duplicate_replacements(payload, tests_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Low-confidence mask detection (fix C)
# ---------------------------------------------------------------------------


def test_detect_low_confidence_masks_flags_dup_and_low_conf(tmp_path: Path):
    # The TOOLTIP signature: low confidence (0.45) + selector reused for
    # another item → flagged as a suspected silent mask.
    payload = _payload_with_items([
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Layout-Gemini']",
            "strategy": "data-testid", "line": 2, "confidence": 0.95, "applied": True,
        },
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Layout-Gemini']",
            "strategy": "data-testid", "line": 8, "confidence": 0.45, "applied": True,
        },
    ])
    masks = _detect_low_confidence_masks(payload, tests_dir=tmp_path)
    assert len(masks) == 1
    assert masks[0]["confidence"] == 0.45
    assert masks[0]["line"] == 8


def test_detect_low_confidence_masks_ignores_high_conf_duplicates(tmp_path: Path):
    # Two high-confidence items with the same selector → duplicates list
    # picks it up, but low_confidence_masks must NOT flag (no mask
    # signature, just plain duplication).
    payload = _payload_with_items([
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Shared']",
            "strategy": "data-testid", "line": 2, "confidence": 0.9, "applied": True,
        },
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Shared']",
            "strategy": "data-testid", "line": 3, "confidence": 0.9, "applied": True,
        },
    ])
    assert _detect_low_confidence_masks(payload, tests_dir=tmp_path) == []


def test_detect_low_confidence_masks_ignores_unique_low_conf(tmp_path: Path):
    # Low confidence but unique selector — the resolver might legitimately
    # be ~0.5 confident about a one-off, that's not a mask.
    payload = _payload_with_items([
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Lonely']",
            "strategy": "data-testid", "line": 2, "confidence": 0.4, "applied": True,
        },
        {
            "tbd": "TBD_LOCATOR", "replacement": "[data-testid='Other']",
            "strategy": "data-testid", "line": 5, "confidence": 0.9, "applied": True,
        },
    ])
    assert _detect_low_confidence_masks(payload, tests_dir=tmp_path) == []


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


def test_apply_patches_strips_wrong_tests_prefix(tmp_path: Path):
    """Regression: agent sometimes prefixes `tests/` to file paths because
    the SUT layout is `tests/...`. Without the path fallback, every patch
    skips with 'file not found' and the step warns with applied=0.
    """
    real_file = tmp_path / "pages" / "locators" / "worca_x_locators.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text(
        "class L:\n    A = \"TBD_LOCATOR\"\n", encoding="utf-8",
    )
    resolutions = [
        {
            "test_id": "T-x",
            # NOTE the wrong `tests/` prefix the agent emits:
            "file": "tests/pages/locators/worca_x_locators.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is True, applied
    assert "#x" in real_file.read_text(encoding="utf-8")


def test_apply_patches_falls_back_to_basename_match(tmp_path: Path):
    """When the agent's path doesn't match any prefix, a unique-basename
    match anywhere under tests_dir is the last-resort fallback."""
    real_file = tmp_path / "a" / "b" / "c" / "worca_unique_name.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("X = \"TBD_LOCATOR\"\n", encoding="utf-8")
    resolutions = [
        {
            "test_id": "T-x",
            # Wildly wrong directory path; basename is unique under tests_dir.
            "file": "completely/wrong/path/worca_unique_name.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id"}
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is True


def test_apply_patches_ambiguous_basename_skips(tmp_path: Path):
    """Two files share the same basename → ambiguous → skip rather than
    pick the wrong one."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "shared.py").write_text("A=\"TBD_LOCATOR\"\n", encoding="utf-8")
    (tmp_path / "b" / "shared.py").write_text("B=\"TBD_LOCATOR\"\n", encoding="utf-8")
    resolutions = [
        {
            "test_id": "T-x", "file": "no/match/shared.py",
            "items": [{"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id"}],
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


def _seed_step7(
    ws,  # Workspace instance (used for ws.sut)
    *,
    tbd_file: str = "worca_test_login.spec.ts",
    content: str = GOOD_TS_WITH_TBD,
):
    """Seed step-7 metadata + place the test file in the SUT.

    Under the new design, step 7 commits worca-prefixed files into the
    SUT clone on the worca-t branch, and `artifacts/step07/` holds only
    tbd-index.json + generated-files.json (no test bytes). Step 8 reads
    the index, locates the file inside `<workspace>/sut/`, and patches it
    in place.
    """
    # Place the test file inside the SUT under tests/ (where step 7 would).
    sut_tests = ws.sut / "tests"
    sut_tests.mkdir(parents=True, exist_ok=True)
    (sut_tests / tbd_file).write_text(content, encoding="utf-8")

    # tbd-index.json with SUT-relative paths (`tests/<file>`) — the indexer
    # output step 7 produces.
    rel_path = f"tests/{tbd_file}"
    step7 = ws.root / "artifacts" / "step07"
    step7.mkdir(parents=True, exist_ok=True)
    index = {
        "framework": "playwright-ts",
        "test_root": str(ws.sut),
        "totals": {"files": 1, "tests": 1, "tbd_locators": 1},
        "files": [rel_path],
        "tests": [
            {
                "id": "T-login",
                "name": "should login",
                "file": rel_path,
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
    (step7 / "tbd-index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return rel_path


def _ctx(tmp_path: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    # Step 8 requires the SUT to be a git repo on the worca-t branch.
    seed_sut(ws)
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=".")
    opts = PipelineOptions(spec="x", sut=".", workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=".", options=opts)


async def test_step08_requires_step07_outputs(tmp_path: Path):
    ctx = _ctx(tmp_path)
    result = await LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "step 8 requires" in (result.error or "")


async def test_step08_short_circuits_when_no_tbd(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    # Seed a SUT test file with no TBDs + a matching tbd-index.json with
    # tbd_locators=0.
    sut_tests = ctx.workspace.sut / "tests"
    sut_tests.mkdir(parents=True, exist_ok=True)
    (sut_tests / "worca_test_ok.spec.ts").write_text(
        "test('a', async ({ page }) => { await page.getByRole('button').click(); });\n",
        encoding="utf-8",
    )
    step7 = ctx.workspace.root / "artifacts" / "step07"
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "tbd-index.json").write_text(
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

    # Even though we install a fake query, it must NOT be invoked.
    install_fake_query(monkeypatch, raises=RuntimeError("simulated exit 99"))

    result = await LocatorResolutionStep().run(ctx)
    assert result.success
    assert result.status == "completed"
    res = json.loads((ctx.workspace.step_dir(8) / "locator-resolution.json").read_text(encoding="utf-8"))
    assert res["resolutions"] == []
    assert res["totals"]["tests_with_tbd"] == 0


async def test_step08_happy_path_patches_files(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    rel = _seed_step7(ctx.workspace)
    monkeypatch.setenv("SUT_BASE_URL", "https://example.test")

    resolution = {
        "base_url": "https://example.test",
        "resolutions": [
            {
                "test_id": "T-login",
                "file": rel,
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
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )

    result = await LocatorResolutionStep().run(ctx)
    assert result.success, result.error
    assert result.status == "completed"
    out = ctx.workspace.step_dir(8)
    # Patched file lives in the SUT now (NOT in artifacts/step08/).
    patched = (ctx.workspace.sut / rel).read_text(encoding="utf-8")
    assert "TBD_LOCATOR" not in patched
    assert "#submit" in patched
    payload = json.loads((out / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 1
    assert payload["totals"]["skipped"] == 0
    reidx = json.loads((out / "tbd-index.json").read_text(encoding="utf-8"))
    assert reidx["totals"]["tbd_locators"] == 0
    assert not reidx["violations"]


async def test_step08_rejects_xpath_replacement_via_patcher(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    rel = _seed_step7(ctx.workspace)

    resolution = {
        "resolutions": [
            {
                "test_id": "T-login",
                "file": rel,
                "items": [
                    {"tbd": "TBD_LOCATOR", "replacement": "//button", "strategy": "css"}
                ],
            }
        ]
    }
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )

    result = await LocatorResolutionStep().run(ctx)
    # applied=0 out of 1 item → 0% apply rate, well below the 90% threshold.
    # Step must fail so the pipeline halts before wasting compute downstream.
    assert result.success is False
    assert result.status == "failed"
    assert "below the 90%" in (result.error or "") or "0/1" in (result.error or "")
    out = ctx.workspace.step_dir(8)
    payload = json.loads((out / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 0
    assert payload["totals"]["skipped"] == 1
    # File unchanged in the SUT: TBD still present (xpath was rejected).
    assert "TBD_LOCATOR" in (ctx.workspace.sut / rel).read_text(encoding="utf-8")


async def test_step08_agent_invalid_json_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace)

    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": "not json{"},
    )

    result = await LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "not valid JSON" in (result.error or "")


async def test_step08_agent_no_output_fails(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace)

    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={},
    )

    result = await LocatorResolutionStep().run(ctx)
    assert not result.success
    assert "locator-resolution.json" in (result.error or "")


async def test_step08_resolves_file_from_test_id_when_omitted(tmp_path: Path, monkeypatch):
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace)

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
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )

    result = await LocatorResolutionStep().run(ctx)
    assert result.success
    payload = json.loads((ctx.workspace.step_dir(8) / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 1


async def test_step08_fails_fast_when_all_patches_skipped(tmp_path: Path, monkeypatch):
    """Regression: applied=0 + tests-needing-resolution>0 must surface as
    `failed`, not `warned`. Previously this silent failure let Steps 9-11 run
    against unpatched tests and waste compute. The new contract: Step 8 fails
    immediately so the operator sees the actual locator-resolution problem.
    """
    ctx = _ctx(tmp_path)
    _seed_step7(ctx.workspace)

    # Agent emits a resolution but the file path is wrong AND not
    # recoverable by the prefix/basename fallback.
    resolution = {
        "resolutions": [
            {
                "test_id": "T-login",
                "file": "completely/wrong/path/nonexistent.spec.ts",
                "items": [
                    {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id"}
                ],
            }
        ]
    }
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
    )

    result = await LocatorResolutionStep().run(ctx)
    assert result.success is False
    assert result.status == "failed"
    assert "below the 90%" in (result.error or "") or "0/" in (result.error or "")
    # Artifacts are still attached so the operator can inspect them.
    assert any("locator-resolution.json" in str(p) for p in result.outputs)


# ---------------------------------------------------------------------------
# Line-targeted patching regression tests (adversarial review concerns).
# These pin the fix for the multi-bug "scrambled locators" failure mode
# discovered when the AskBosch SUT run produced a file where:
#   - The first comment line containing the literal string `TBD_LOCATOR`
#     consumed the first replacement.
#   - Strategy-priority sort scrambled the agent's intended line-by-line
#     order so values landed on the wrong keys.
#   - One TBD remained unpatched at the bottom.
# ---------------------------------------------------------------------------


def _write_locator_file_with_comment(tmp_path: Path) -> Path:
    """Realistic Step-7-emitted locator file with a `TBD_LOCATOR` literal in
    the docstring comment AND three real TBDs in class assignments. This is
    the exact shape that triggered the original scrambling bug."""
    p = tmp_path / "locators.py"
    p.write_text(
        # Line 1: regular comment
        "# Stack: playwright-py | Priority chain\n"
        # Line 2: comment containing the literal TBD_LOCATOR token
        "# Selectors marked TBD_LOCATOR require resolution.\n"
        "\n"
        "\n"
        "class L:\n"
        "    BTN_A = \"TBD_LOCATOR\"\n"  # line 6
        "    BTN_B = \"TBD_LOCATOR\"\n"  # line 7
        "    BTN_C = \"TBD_LOCATOR\"\n",  # line 8
        encoding="utf-8",
    )
    return p


def test_apply_patches_line_targeted_does_not_consume_comment_tbd(tmp_path: Path):
    """The comment on line 2 contains the literal `TBD_LOCATOR` for
    documentation purposes. Line-targeted replacement must leave it alone
    even when the agent's 3 items refer to lines 6/7/8 only."""
    _write_locator_file_with_comment(tmp_path)
    resolutions = [
        {
            "test_id": "T-x", "file": "locators.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "replacement": "#btn-a",
                 "strategy": "id", "line": 6},
                {"tbd": "TBD_LOCATOR", "replacement": "#btn-b",
                 "strategy": "id", "line": 7},
                {"tbd": "TBD_LOCATOR", "replacement": "#btn-c",
                 "strategy": "id", "line": 8},
            ],
        }
    ]
    applied = _apply_patches(tmp_path, resolutions)
    assert all(a["applied"] for a in applied), applied
    final = (tmp_path / "locators.py").read_text(encoding="utf-8")
    # Comment is untouched — TBD_LOCATOR survives in the documentation string.
    assert "Selectors marked TBD_LOCATOR" in final
    # Each key got its INTENDED value (not shuffled).
    assert 'BTN_A = "#btn-a"' in final
    assert 'BTN_B = "#btn-b"' in final
    assert 'BTN_C = "#btn-c"' in final


def test_apply_patches_line_drift_within_tolerance_falls_back_to_nearby(tmp_path: Path):
    """Formatter may insert/remove a blank line between Step 7's indexer
    snapshot and Step 8's patch. Agent reports line=6 but the token is on
    line 7 (one line drifted). Must still apply, not skip."""
    p = tmp_path / "locators.py"
    p.write_text(
        "class L:\n"
        "\n"  # blank line added by formatter after indexer snapshot
        "\n"
        "\n"
        "\n"
        "\n"  # line 6 (where agent THOUGHT the TBD was)
        "    BTN = \"TBD_LOCATOR\"\n",  # line 7 (where the TBD actually is)
        encoding="utf-8",
    )
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [
            {"tbd": "TBD_LOCATOR", "replacement": "#btn",
             "strategy": "id", "line": 6},  # off by one
        ],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"], applied
    assert "drift" in applied[0].get("applied_via", "")
    assert "#btn" in p.read_text(encoding="utf-8")


def test_apply_patches_line_drift_beyond_tolerance_skips(tmp_path: Path):
    """Agent reports line=2 but the TBD is on line 30 — far outside the ±10
    drift tolerance. The new contract: do NOT fall back to global
    first-occurrence replacement (that path scrambled assignments when
    the token appeared in multiple non-adjacent places). Instead, mark
    the item `applied: false` with a drift `skip_reason` and let HITL /
    8b surface it."""
    p = tmp_path / "locators.py"
    lines = ["# pad\n"] * 29 + ["BTN = \"TBD_LOCATOR\"\n"]
    p.write_text("".join(lines), encoding="utf-8")
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [
            {"tbd": "TBD_LOCATOR", "replacement": "#btn",
             "strategy": "id", "line": 2},
        ],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "source drift" in (applied[0].get("skip_reason") or "")
    # File untouched — token still present (no scrambled patch).
    assert "TBD_LOCATOR" in p.read_text(encoding="utf-8")
    assert "#btn" not in p.read_text(encoding="utf-8")


def test_apply_patches_preserves_crlf_line_endings(tmp_path: Path):
    """Windows-checked-out SUTs use CRLF. The patcher must NOT silently
    normalise to LF — that would produce a giant whole-file diff on the
    next git operation."""
    p = tmp_path / "locators.py"
    crlf_text = "class L:\r\n    BTN = \"TBD_LOCATOR\"\r\n"
    p.write_bytes(crlf_text.encode("utf-8"))
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": 2}],
    }]
    _apply_patches(tmp_path, resolutions)
    # Read raw bytes — CRLF must survive round-trip.
    raw = p.read_bytes()
    assert b"\r\n" in raw, f"expected CRLF preserved, got: {raw!r}"
    assert b"#btn" in raw


def test_apply_patches_preserves_utf8_bom(tmp_path: Path):
    """A leading UTF-8 BOM must round-trip through the patcher."""
    p = tmp_path / "locators.py"
    bom_text = "﻿class L:\n    BTN = \"TBD_LOCATOR\"\n"
    p.write_bytes(bom_text.encode("utf-8"))
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": 2}],
    }]
    _apply_patches(tmp_path, resolutions)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), f"BOM dropped: {raw[:5]!r}"


def test_apply_patches_line_field_string_coerces_to_int(tmp_path: Path):
    """Agent may emit `\"line\": \"6\"` (string) due to loose JSON
    serialisation. Coercion must not crash and the patch must still apply."""
    p = tmp_path / "locators.py"
    p.write_text("\n\n\n\n\n    BTN = \"TBD_LOCATOR\"\n", encoding="utf-8")  # line 6
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": "6"}],  # string, not int
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"]


def test_apply_patches_line_field_float_coerces(tmp_path: Path):
    p = tmp_path / "locators.py"
    p.write_text("\n\n\n\n\n    BTN = \"TBD_LOCATOR\"\n", encoding="utf-8")  # line 6
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": 6.0}],  # float
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"]


def test_apply_patches_line_field_invalid_falls_back_to_global(tmp_path: Path):
    """`line: "abc"` or negative line must NOT crash. Treated as missing
    `line`, falls through the legacy global-replacement path."""
    p = tmp_path / "locators.py"
    p.write_text("BTN = \"TBD_LOCATOR\"\n", encoding="utf-8")
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": "not-a-number"}],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"]
    assert "global" in applied[0].get("applied_via", "")


def test_apply_patches_two_items_same_line_distinct_occurrences(tmp_path: Path):
    """Two TBDs on the same line, agent emits two items both `line: 1`.
    Both must apply (one-item-one-occurrence contract)."""
    p = tmp_path / "locators.py"
    p.write_text(
        'BTNS = ("TBD_LOCATOR", "TBD_LOCATOR")\n',
        encoding="utf-8",
    )
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [
            {"tbd": "TBD_LOCATOR", "replacement": "#a",
             "strategy": "id", "line": 1},
            {"tbd": "TBD_LOCATOR", "replacement": "#b",
             "strategy": "id", "line": 1},
        ],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert all(a["applied"] for a in applied), applied
    text = p.read_text(encoding="utf-8")
    assert '"#a"' in text
    assert '"#b"' in text
    assert "TBD_LOCATOR" not in text


def test_apply_patches_xpath_rejected_even_with_invalid_line(tmp_path: Path):
    """XPath rejection MUST run before line validation — security-critical.
    Agent supplying `line: 999, replacement: 'xpath=//x'` skips with
    `xpath replacement rejected`, not `line out of bounds`."""
    p = tmp_path / "locators.py"
    p.write_text("BTN = \"TBD_LOCATOR\"\n", encoding="utf-8")
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "xpath=//button",
                   "strategy": "css", "line": 999}],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "xpath" in applied[0]["skip_reason"]


def test_apply_patches_no_op_replacement_still_caught_with_line(tmp_path: Path):
    """`replacement == tbd_token` no-op skip must fire on the line-targeted
    path too, not just the legacy global path."""
    p = tmp_path / "locators.py"
    p.write_text("BTN = \"TBD_LOCATOR\"\n", encoding="utf-8")
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "TBD_LOCATOR",
                   "strategy": "css", "line": 1}],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied"] is False
    assert "no-op" in applied[0]["skip_reason"]
    # File unchanged.
    assert p.read_text(encoding="utf-8") == "BTN = \"TBD_LOCATOR\"\n"


def test_apply_patches_audit_records_applied_via(tmp_path: Path):
    """Every applied item gets an `applied_via` field for the audit trail
    (line:N, line:N±M, global fallback, or global no-line)."""
    p = tmp_path / "locators.py"
    p.write_text("\nBTN = \"TBD_LOCATOR\"\n", encoding="utf-8")  # line 2
    resolutions = [{
        "test_id": "T-x", "file": "locators.py",
        "items": [{"tbd": "TBD_LOCATOR", "replacement": "#btn",
                   "strategy": "id", "line": 2}],
    }]
    applied = _apply_patches(tmp_path, resolutions)
    assert applied[0]["applied_via"] == "line:2"


# ---------------------------------------------------------------------------
# _classify_item — agent-honest-skip preservation (new in Step 8b design)
# ---------------------------------------------------------------------------


def test_classify_item_preserves_agent_skip_reason_when_strategy_null():
    """When the agent returns strategy=None AND supplies its own skip_reason
    (the 'honest skip' pattern), _classify_item must preserve that reason
    instead of clobbering it with the generic 'unknown strategy: None'.

    This is the bug that made today's askbosch run unrecoverable: the
    diagnostic 'no DOM element matched: <description>' was being silently
    rewritten, hiding the real cause and counting against the apply-rate
    gate.
    """
    item = {
        "tbd": "TBD_LOCATOR",
        "line": 10,
        "strategy": None,
        "replacement": None,
        "skip_reason": "no DOM element matched: no tooltip in either snapshot",
    }
    can_proceed, annotated = _classify_item(item, "TBD_LOCATOR\n")
    assert can_proceed is False
    assert annotated["applied"] is False
    assert annotated["skip_reason"] == (
        "no DOM element matched: no tooltip in either snapshot"
    )


def test_classify_item_falls_back_to_unknown_strategy_for_garbage():
    """If the agent emits a real-but-unrecognised strategy value (no
    diagnostic skip_reason), the generic message stays."""
    item = {
        "tbd": "TBD_LOCATOR",
        "line": 1,
        "strategy": "telepathy",
        "replacement": "#x",
    }
    can_proceed, annotated = _classify_item(item, "TBD_LOCATOR\n")
    assert can_proceed is False
    assert "unknown strategy: telepathy" in annotated["skip_reason"]


def test_classify_item_strategy_null_no_reason_still_gets_generic():
    """Honest-skip preservation only kicks in when the agent supplies a
    diagnostic reason. strategy=None + no skip_reason → fall back to the
    generic message so we don't silently drop the item."""
    item = {
        "tbd": "TBD_LOCATOR",
        "line": 1,
        "strategy": None,
        "replacement": None,
    }
    can_proceed, annotated = _classify_item(item, "TBD_LOCATOR\n")
    assert can_proceed is False
    assert "unknown strategy: None" in annotated["skip_reason"]


# ---------------------------------------------------------------------------
# _audit_snapshot_policy — AOM-first file+payload auditor
# ---------------------------------------------------------------------------


def test_audit_snapshot_policy_aom_only_run_yields_no_violations(tmp_path: Path):
    """Reference happy path: every distinct URL captured as AOM
    (`page-snapshot-NN.json`), no raw-DOM fallbacks. No violations."""
    (tmp_path / "page-snapshot-01.json").write_text("{}", encoding="utf-8")
    (tmp_path / "page-snapshot-02.json").write_text("{}", encoding="utf-8")
    payload = {"resolutions": [
        {"test_id": "T-x", "file": "a.py", "items": [
            {"tbd": "TBD_LOCATOR", "replacement": "#x", "strategy": "id",
             "line": 1, "applied": True, "snapshot_source": "aom"},
        ]},
    ]}
    assert _audit_snapshot_policy(tmp_path, payload) == []


def test_audit_snapshot_policy_justified_raw_fallback_passes(tmp_path: Path):
    """A `*-raw.html` capture is OK when at least one resolution declares
    `snapshot_source: raw_dom_fallback` with a `fallback_reason`."""
    (tmp_path / "page-snapshot-01.json").write_text("{}", encoding="utf-8")
    (tmp_path / "page-snapshot-02-raw.html").write_text("<html/>", encoding="utf-8")
    payload = {"resolutions": [
        {"test_id": "T-x", "file": "a.py", "items": [
            {"tbd": "TBD_X", "replacement": ".x", "strategy": "css",
             "line": 1, "applied": True,
             "snapshot_source": "raw_dom_fallback",
             "fallback_reason": "non_semantic"},
        ]},
    ]}
    assert _audit_snapshot_policy(tmp_path, payload) == []


def test_audit_snapshot_policy_flags_raw_without_fallback_reason(tmp_path: Path):
    """A raw-DOM capture without any item declaring `raw_dom_fallback` is
    a policy violation — the agent fell back without justifying it."""
    (tmp_path / "page-snapshot-01.json").write_text("{}", encoding="utf-8")
    (tmp_path / "page-snapshot-02-raw.html").write_text("<html/>", encoding="utf-8")
    payload = {"resolutions": [
        {"test_id": "T-x", "file": "a.py", "items": [
            {"tbd": "TBD_X", "replacement": "#x", "strategy": "id",
             "line": 1, "applied": True, "snapshot_source": "aom"},
        ]},
    ]}
    violations = _audit_snapshot_policy(tmp_path, payload)
    assert len(violations) == 1
    assert violations[0]["kind"] == "raw_without_fallback_reason"


def test_audit_snapshot_policy_flags_zero_aom_captures(tmp_path: Path):
    """If snapshots exist but none are AOM, the agent is still using the
    old HTML-first policy — flag as a violation so the operator can fix
    the prompt."""
    (tmp_path / "page-snapshot-01-raw.html").write_text("<html/>", encoding="utf-8")
    payload = {"resolutions": [
        {"test_id": "T-x", "file": "a.py", "items": [
            {"tbd": "TBD_X", "replacement": "#x", "strategy": "id",
             "line": 1, "applied": True,
             "snapshot_source": "raw_dom_fallback",
             "fallback_reason": "non_semantic"},
        ]},
    ]}
    violations = _audit_snapshot_policy(tmp_path, payload)
    # 1 violation for "no AOM" (the raw is justified, so no
    # raw_without_fallback_reason violation).
    kinds = sorted(v["kind"] for v in violations)
    assert kinds == ["no_aom_captures"]


def test_audit_snapshot_policy_missing_workdir_returns_empty(tmp_path: Path):
    """Missing workdir = no violations (best-effort, never blocks)."""
    assert _audit_snapshot_policy(tmp_path / "nonexistent", {}) == []


def test_audit_snapshot_policy_empty_workdir_returns_empty(tmp_path: Path):
    """Empty workdir (no snapshots at all) → no violations from this auditor.
    The MCP-usage gate elsewhere catches 'no DOM evidence' separately."""
    assert _audit_snapshot_policy(tmp_path, {"resolutions": []}) == []


# ---------------------------------------------------------------------------
# _apply_comparison_verdict — stamps verdicts and excuses ghost/duplicate
# ---------------------------------------------------------------------------


def test_apply_comparison_verdict_excuses_ghost():
    """Ghost verdict forces applied=False and rewrites skip_reason."""
    payload = {
        "resolutions": [{
            "test_id": "S-loc",
            "file": "src/loc.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 8, "applied": True,
                 "strategy": "data-testid", "replacement": "[data-testid='x']",
                 "applied_via": "line:8"},
            ],
        }],
    }
    comparison = {
        "expected_elements": [
            {"tbd_constant": "TOOLTIP", "file": "src/loc.py", "line": 8,
             "verdict": "ghost", "explanation": "no tooltip element in any snapshot"},
        ],
        "summary": {"matched": 0, "ghost": 1, "duplicate": 0,
                    "low_confidence": 0, "should_exist_total": 0},
    }
    out = _apply_comparison_verdict(payload, comparison)
    item = out["resolutions"][0]["items"][0]
    assert item["comparison_verdict"] == "ghost"
    assert item["applied"] is False
    assert item["strategy"] is None
    assert item["replacement"] is None
    assert "no tooltip element" in item["skip_reason"]
    assert "applied_via" not in item


def test_apply_comparison_verdict_excuses_duplicate():
    """Duplicate verdict forces applied=False, names the duplicate target."""
    payload = {
        "resolutions": [{
            "test_id": "S-loc", "file": "src/loc.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 9, "applied": True,
                 "strategy": "data-testid", "replacement": "[data-testid='x']"},
            ],
        }],
    }
    comparison = {
        "expected_elements": [
            {"tbd_constant": "GEMINI_LINK", "file": "src/loc.py", "line": 9,
             "verdict": "duplicate", "duplicate_of": "GEMINI_BUTTON",
             "explanation": "same DOM element as GEMINI_BUTTON"},
        ],
        "summary": {"matched": 0, "ghost": 0, "duplicate": 1,
                    "low_confidence": 0, "should_exist_total": 0},
    }
    out = _apply_comparison_verdict(payload, comparison)
    item = out["resolutions"][0]["items"][0]
    assert item["comparison_verdict"] == "duplicate"
    assert item["applied"] is False


def test_apply_comparison_verdict_matched_leaves_item_alone():
    """Matched verdict stamps comparison_verdict but does not flip applied/strategy."""
    payload = {
        "resolutions": [{
            "test_id": "S-loc", "file": "src/loc.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 8, "applied": True,
                 "strategy": "data-testid",
                 "replacement": "[data-testid='Layout-Gemini']",
                 "applied_via": "line:8"},
            ],
        }],
    }
    comparison = {
        "expected_elements": [
            {"tbd_constant": "GEMINI_BUTTON", "file": "src/loc.py", "line": 8,
             "verdict": "matched",
             "matched_selector": "[data-testid='Layout-Gemini']",
             "confidence": 0.95},
        ],
        "summary": {"matched": 1, "ghost": 0, "duplicate": 0,
                    "low_confidence": 0, "should_exist_total": 1},
    }
    out = _apply_comparison_verdict(payload, comparison)
    item = out["resolutions"][0]["items"][0]
    assert item["comparison_verdict"] == "matched"
    assert item["applied"] is True
    assert item["strategy"] == "data-testid"
    assert item["replacement"] == "[data-testid='Layout-Gemini']"
    assert item["applied_via"] == "line:8"


def test_apply_comparison_verdict_unknown_constant_silently_ignored():
    """If the auditor mentions a constant that isn't in the resolution items
    (e.g. tbd-index drift), we silently skip — the comparison report still
    records it but the resolution payload stays unchanged."""
    payload = {
        "resolutions": [{
            "test_id": "S-loc", "file": "src/loc.py",
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 8, "applied": True,
                 "strategy": "id", "replacement": "#a"},
            ],
        }],
    }
    comparison = {
        "expected_elements": [
            {"tbd_constant": "PHANTOM", "file": "src/loc.py", "line": 999,
             "verdict": "ghost", "explanation": "not in resolution"},
        ],
        "summary": {"matched": 0, "ghost": 1, "duplicate": 0,
                    "low_confidence": 0, "should_exist_total": 0},
    }
    out = _apply_comparison_verdict(payload, comparison)
    # Original item untouched (no comparison_verdict, still applied).
    item = out["resolutions"][0]["items"][0]
    assert item["applied"] is True
    assert "comparison_verdict" not in item


# ---------------------------------------------------------------------------
# _build_comparison_prompt — the 8b prompt builder
# ---------------------------------------------------------------------------


def test_build_comparison_prompt_opens_with_audit_mode_token():
    """The prompt must open with `MODE: DOM-COMPARISON-AUDIT` so the fixer
    agent switches into its audit-only behaviour."""
    index = {"tests": [], "support_files": []}
    prompt = _build_comparison_prompt(index, Path("/sut"), snapshot_filenames=[])
    assert prompt.startswith("MODE: DOM-COMPARISON-AUDIT")
    assert "dom-comparison.json" in prompt
    assert "no Playwright MCP" in prompt.lower() or "do not call any playwright mcp tool" in prompt.lower()


def test_build_comparison_prompt_lists_snapshots(tmp_path: Path):
    index = {"tests": [], "support_files": []}
    prompt = _build_comparison_prompt(
        index, tmp_path, snapshot_filenames=["page-snapshot-01.html", "page-snapshot-02.html"],
    )
    assert "page-snapshot-01.html" in prompt
    assert "page-snapshot-02.html" in prompt


def test_build_comparison_prompt_handles_zero_snapshots(tmp_path: Path):
    """When the playwright-tester wrote no snapshots, the prompt tells the
    auditor to emit `unevaluated` rather than fabricating verdicts."""
    index = {"tests": [], "support_files": []}
    prompt = _build_comparison_prompt(index, tmp_path, snapshot_filenames=[])
    assert "unevaluated" in prompt


# ---------------------------------------------------------------------------
# Integration: end-to-end Step 8 with the 8b audit agent succeeding
# ---------------------------------------------------------------------------


async def test_step08_excuses_ghost_verdicts_from_apply_rate_gate(tmp_path: Path, monkeypatch):
    """Reproduces today's askbosch failure mode under the new design:
    3 TBDs in one locator file, 1 resolvable + 2 honest skips. The 8b
    auditor marks the 2 skips as ghost/duplicate; the apply-rate gate
    excuses them and the step completes (was: failed under 90% gate).
    """
    ctx = _ctx(tmp_path)
    # Seed a Python locator file with 3 TBDs (lines 6, 7, 8).
    sut_src = ctx.workspace.sut / "src" / "pages"
    sut_src.mkdir(parents=True, exist_ok=True)
    loc_file = sut_src / "worca_gemini_nav_locators.py"
    loc_file.write_text(
        "# stack: playwright-py\n"
        "class GeminiNavLocators:\n"
        "\n"
        "\n"
        "\n"
        "    GEMINI_BUTTON = \"TBD_LOCATOR\"\n"   # line 6
        "    GEMINI_LINK = \"TBD_LOCATOR\"\n"     # line 7
        "    GEMINI_TOOLTIP = \"TBD_LOCATOR\"\n", # line 8
        encoding="utf-8",
    )

    rel_path = "src/pages/worca_gemini_nav_locators.py"
    step7 = ctx.workspace.root / "artifacts" / "step07"
    step7.mkdir(parents=True, exist_ok=True)
    index = {
        "framework": "playwright-py",
        "test_root": str(ctx.workspace.sut),
        "totals": {"files": 1, "tests": 0, "tbd_locators": 3,
                   "total_support_files": 1},
        "files": [rel_path],
        "tests": [],
        "support_files": [{
            "name": "worca_gemini_nav_locators",
            "file": rel_path,
            "kind": "locators",
            "tbd_markers": [
                {"line": 6, "raw": "TBD_LOCATOR", "context": ""},
                {"line": 7, "raw": "TBD_LOCATOR", "context": ""},
                {"line": 8, "raw": "TBD_LOCATOR", "context": ""},
            ],
        }],
        "violations": [],
    }
    (step7 / "tbd-index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8",
    )
    monkeypatch.setenv("SUT_BASE_URL", "https://askbosch.test")

    # 8a's expected output: 1 applied + 2 honest skips (strategy=null,
    # skip_reason set). 8b's expected output: confirms 1 matched, 1
    # ghost, 1 duplicate.
    resolution = {
        "base_url": "https://askbosch.test",
        "resolutions": [{
            "test_id": "S-worca_gemini_nav_locators",
            "file": rel_path,
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 6, "applied": True,
                 "strategy": "data-testid", "confidence": 0.95,
                 "replacement": "[data-testid='Layout-GeminiEnterprise']"},
                {"tbd": "TBD_LOCATOR", "line": 7, "applied": False,
                 "strategy": None, "replacement": None, "confidence": 0.0,
                 "skip_reason": "same element as GEMINI_BUTTON"},
                {"tbd": "TBD_LOCATOR", "line": 8, "applied": False,
                 "strategy": None, "replacement": None, "confidence": 0.0,
                 "skip_reason": "no DOM element matched: no tooltip exists"},
            ],
        }],
    }
    comparison = {
        "snapshots_consumed": [
            {"file": "page-snapshot-01.html", "kind": "html",
             "url": "https://askbosch.test/login"},
            {"file": "page-snapshot-02.html", "kind": "html",
             "url": "https://askbosch.test/"},
        ],
        "expected_elements": [
            {"tbd_constant": "GEMINI_BUTTON", "file": rel_path, "line": 6,
             "verdict": "matched",
             "matched_selector": "[data-testid='Layout-GeminiEnterprise']",
             "snapshot": "page-snapshot-02.html", "confidence": 0.95},
            {"tbd_constant": "GEMINI_LINK", "file": rel_path, "line": 7,
             "verdict": "duplicate", "duplicate_of": "GEMINI_BUTTON",
             "explanation": "same element as GEMINI_BUTTON"},
            {"tbd_constant": "GEMINI_TOOLTIP", "file": rel_path, "line": 8,
             "verdict": "ghost",
             "explanation": "no tooltip element exists in any snapshot"},
        ],
        "summary": {"matched": 1, "ghost": 1, "duplicate": 1,
                    "low_confidence": 0, "should_exist_total": 1},
    }
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={
            "locator-resolution.json": json.dumps(resolution),
            "dom-comparison.json": json.dumps(comparison),
        },
    )

    result = await LocatorResolutionStep().run(ctx)
    assert result.success, result.error
    # status is "warned" because remaining_tbd > 0 (the 2 skipped TBDs
    # still sit in the file as TBD_LOCATOR after step 8). That's the
    # correct outcome — we surfaced the gap rather than masking it.
    assert result.status in ("warned", "completed")
    out = ctx.workspace.step_dir(8)
    payload = json.loads((out / "locator-resolution.json").read_text(encoding="utf-8"))
    assert payload["totals"]["applied"] == 1
    assert payload["totals"]["skipped"] == 2
    assert payload["totals"].get("excused", 0) == 2
    # ghost/duplicate verdicts stamped on the items.
    items = payload["resolutions"][0]["items"]
    by_line = {it["line"]: it for it in items}
    assert by_line[6]["comparison_verdict"] == "matched"
    assert by_line[7]["comparison_verdict"] == "duplicate"
    assert by_line[8]["comparison_verdict"] == "ghost"
    # dom-comparison.json published to the artifact dir as well.
    assert (out / "dom-comparison.json").exists()


async def test_step08_proceeds_without_verdict_when_8b_produces_nothing(tmp_path: Path, monkeypatch):
    """If the audit agent runs but doesn't write dom-comparison.json, the
    step falls back to the pre-audit gate calculation (no excusal). Today's
    failure mode (0 excused, low apply rate) still fails as before — we
    haven't regressed when the auditor itself fails."""
    ctx = _ctx(tmp_path)
    rel = _seed_step7(ctx.workspace)
    monkeypatch.setenv("SUT_BASE_URL", "https://example.test")

    # 8a returns 0 applied / 1 skipped; 8b returns no dom-comparison.json.
    resolution = {
        "resolutions": [{
            "test_id": "T-login", "file": rel,
            "items": [
                {"tbd": "TBD_LOCATOR", "line": 2, "applied": False,
                 "strategy": None, "replacement": None,
                 "skip_reason": "couldn't find element"},
            ],
        }],
    }
    install_fake_query(
        monkeypatch,
        messages=[fake_playwright_mcp_call(), {"type": "result", "result": "ok"}],
        files={"locator-resolution.json": json.dumps(resolution)},
        # dom-comparison.json deliberately omitted.
    )

    result = await LocatorResolutionStep().run(ctx)
    # No excusal → 0/1 = 0% < 90% → failed.
    assert result.success is False
    assert "below the 90%" in (result.error or "")


def test_apply_patches_realworld_askbosch_shape(tmp_path: Path):
    """Reproduces the EXACT shape of the AskBosch failure: 13 items, mixed
    strategies, comment containing TBD_LOCATOR, line numbers ascending.
    Asserts that every key gets its agent-intended value (no shuffling)."""
    p = tmp_path / "worca_gemini_nav_locators.py"
    p.write_text(
        "# Stack: playwright-py | Locator priority\n"
        "# Selectors marked TBD_LOCATOR require resolution.\n"
        "\n"
        "\n"
        "class GeminiNavLocators:\n"
        "    GEMINI_NAV_BUTTON = \"TBD_LOCATOR\"\n"        # line 6
        "    GEMINI_NAV_LABEL = \"TBD_LOCATOR\"\n"          # line 7
        "    GEMINI_NAV_ICON = \"TBD_LOCATOR\"\n"           # line 8
        "    GEMINI_NAV_LINK = \"TBD_LOCATOR\"\n"           # line 9
        "    NEW_CHAT_BUTTON = \"TBD_LOCATOR\"\n"           # line 10
        "    SIDE_NAV_CONTAINER = \"TBD_LOCATOR\"\n"        # line 11
        "    SIDE_NAV_TOGGLE = \"TBD_LOCATOR\"\n"           # line 12
        "    TOOLTIP = \"TBD_LOCATOR\"\n"                   # line 13
        "    LOCALE_SWITCHER = \"TBD_LOCATOR\"\n"           # line 14
        "    LOCALE_OPTION_EN = \"TBD_LOCATOR\"\n"          # line 15
        "    LOCALE_OPTION_DE = \"TBD_LOCATOR\"\n"          # line 16
        "    NAV_ITEMS_CONTAINER = \"TBD_LOCATOR\"\n",      # line 17
        encoding="utf-8",
    )
    items = [
        # Mixed strategies + lines in NON-ascending order to verify sort.
        {"line": 6,  "strategy": "data-testid", "replacement": "[data-testid=\"gemini-button\"]"},
        {"line": 7,  "strategy": "css",         "replacement": "[data-testid=\"gemini-button\"] p"},
        {"line": 8,  "strategy": "css",         "replacement": "[data-testid=\"gemini-button\"] svg"},
        {"line": 9,  "strategy": "data-testid", "replacement": "[data-testid=\"gemini-link\"]"},
        {"line": 10, "strategy": "data-testid", "replacement": "[data-testid=\"new-chat\"]"},
        {"line": 11, "strategy": "data-testid", "replacement": "[data-testid=\"side-nav\"]"},
        {"line": 12, "strategy": "data-testid", "replacement": "[data-testid=\"side-toggle\"]"},
        {"line": 13, "strategy": "role",        "replacement": "[role=\"tooltip\"]"},
        {"line": 14, "strategy": "data-testid", "replacement": "[data-testid=\"locale\"]"},
        {"line": 15, "strategy": "data-testid", "replacement": "[data-testid=\"locale-en\"]"},
        {"line": 16, "strategy": "data-testid", "replacement": "[data-testid=\"locale-de\"]"},
        {"line": 17, "strategy": "data-testid", "replacement": "[data-testid=\"nav-container\"]"},
    ]
    for it in items:
        it["tbd"] = "TBD_LOCATOR"
    resolutions = [{"test_id": "T-x", "file": "worca_gemini_nav_locators.py", "items": items}]
    applied = _apply_patches(tmp_path, resolutions)
    assert all(a["applied"] for a in applied), [a for a in applied if not a["applied"]]
    final = p.read_text(encoding="utf-8")

    # Comment intact.
    assert "Selectors marked TBD_LOCATOR require resolution." in final
    # No remaining TBDs in code.
    code_lines = [l for l in final.splitlines() if l.strip().startswith(("GEMINI", "NEW_", "SIDE_", "TOOLTIP", "LOCALE", "NAV_"))]
    assert all("TBD_LOCATOR" not in l for l in code_lines), code_lines
    # Each key got its INTENDED value — no shuffling.
    assert 'GEMINI_NAV_BUTTON = "[data-testid="gemini-button"]"' in final
    assert 'GEMINI_NAV_LABEL = "[data-testid="gemini-button"] p"' in final
    assert 'NEW_CHAT_BUTTON = "[data-testid="new-chat"]"' in final
    assert 'NAV_ITEMS_CONTAINER = "[data-testid="nav-container"]"' in final
    assert 'LOCALE_SWITCHER = "[data-testid="locale"]"' in final


# ---------------------------------------------------------------------------
# _is_assignment_line — patchable-line anchor (replaces global text fallback)
# ---------------------------------------------------------------------------


def test_is_assignment_line_accepts_python_assignment():
    assert _is_assignment_line('    LOGIN_BUTTON = "TBD_LOCATOR"\n', "TBD_LOCATOR")


def test_is_assignment_line_accepts_inline_function_call():
    # `page.locator('TBD_LOCATOR')` style — token inside a call argument
    # (no `=` required). Realistic codegen shape in test files.
    assert _is_assignment_line(
        "  await page.locator('TBD_LOCATOR').click();\n", "TBD_LOCATOR"
    )


def test_is_assignment_line_accepts_ts_const():
    assert _is_assignment_line('const x = "TBD_LOCATOR";\n', "TBD_LOCATOR")


def test_is_assignment_line_rejects_python_comment():
    assert not _is_assignment_line(
        "    # fallback to TBD_LOCATOR (TODO)\n", "TBD_LOCATOR"
    )


def test_is_assignment_line_rejects_js_comment():
    assert not _is_assignment_line(
        "  // TBD_INTENT: TBD_LOCATOR is the login button\n", "TBD_LOCATOR"
    )


def test_is_assignment_line_rejects_jsdoc_continuation():
    assert not _is_assignment_line(" *  TBD_LOCATOR (still unresolved)\n", "TBD_LOCATOR")


def test_is_assignment_line_rejects_no_token():
    assert not _is_assignment_line("X = 1\n", "TBD_LOCATOR")


# ---------------------------------------------------------------------------
# _infer_strategy — pasted-selector shape detection
# ---------------------------------------------------------------------------


def test_infer_strategy_id():
    assert _infer_strategy("#submit") == "id"


def test_infer_strategy_data_testid():
    assert _infer_strategy('[data-testid="login-btn"]') == "data-testid"
    assert _infer_strategy("[data-cy=submit]") == "data-testid"


def test_infer_strategy_role():
    assert _infer_strategy("role=button[name=Login]") == "role"
    assert _infer_strategy("getByRole('button')") == "role"


def test_infer_strategy_label():
    assert _infer_strategy("getByLabel('Email')") == "label"
    assert _infer_strategy('[aria-label="Email Address"]') == "label"


def test_infer_strategy_placeholder():
    assert _infer_strategy('[placeholder="email@example.com"]') == "placeholder"


def test_infer_strategy_text():
    assert _infer_strategy("text=Submit") == "text"


def test_infer_strategy_defaults_to_css():
    # Class selector, descendant combinator — generic CSS.
    assert _infer_strategy(".card > .submit-button") == "css"


# ---------------------------------------------------------------------------
# _is_spec_gap_answer — phrase recognition
# ---------------------------------------------------------------------------


def test_is_spec_gap_answer_canonical_phrases():
    for phrase in ("ghost", "Spec-Gap", "spec gap", "GAP", "skip", "(b)", "B"):
        assert _is_spec_gap_answer(phrase), phrase


def test_is_spec_gap_answer_rejects_selector():
    assert not _is_spec_gap_answer("#submit")
    assert not _is_spec_gap_answer('[data-testid="x"]')


# ---------------------------------------------------------------------------
# _parse_clarification_header — extract (const, file, line) from agent block
# ---------------------------------------------------------------------------


def test_parse_clarification_header_happy_path():
    parsed = _parse_clarification_header(
        "TBD_LOGIN_BUTTON @ tests/test_login.py:42"
    )
    assert parsed == ("TBD_LOGIN_BUTTON", "tests/test_login.py", 42)


def test_parse_clarification_header_unrecognized_returns_none():
    assert _parse_clarification_header("freeform agent rambling") is None
    assert _parse_clarification_header("no constant @ ") is None


# ---------------------------------------------------------------------------
# _hitl_resolve_unresolvable — splices user-supplied selectors / spec gaps
# ---------------------------------------------------------------------------


_HITL_CLAR_TEMPLATE = (
    "[CLARIFICATION NEEDED: {const} @ {file}:{line}]\n"
    "Intent: {intent}\n"
    "Tried: data-testid=\"x\", role=button\n"
    "Snapshot evidence: page-snapshot-01.json (not found)\n"
    "Resolution options:\n"
    "  (a) Provide a selector to patch in\n"
    "  (b) Confirm spec gap\n"
)


def _hitl_payload(const: str, file_rel: str, line: int) -> dict:
    """Build a minimal locator-resolution payload with one unresolved item."""
    return {
        "resolutions": [{
            "test_id": "T-login",
            "file": file_rel,
            "items": [{
                "tbd": "TBD_LOCATOR",
                "replacement": None,
                "strategy": None,
                "line": line,
                "applied": False,
                "skip_reason": "no DOM element matched",
            }],
        }],
    }


def test_hitl_no_clarifications_file_returns_zero(tmp_path: Path):
    payload = _hitl_payload("X", "a.py", 1)
    assert _hitl_resolve_unresolvable(wd=tmp_path, payload=payload) == 0


def test_hitl_non_tty_returns_zero_without_mutating(tmp_path: Path, monkeypatch):
    """Non-TTY environment (CI) → prompt_user returns {} → no splice."""
    (tmp_path / "clarifications.md").write_text(
        _HITL_CLAR_TEMPLATE.format(
            const="TBD_LOGIN_BUTTON", file="a.py", line=42, intent="submit button"
        ),
        encoding="utf-8",
    )
    payload = _hitl_payload("TBD_LOGIN_BUTTON", "a.py", 42)
    # Force non-TTY by monkeypatching sys.stdin.isatty.
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    spliced = _hitl_resolve_unresolvable(wd=tmp_path, payload=payload)
    assert spliced == 0
    # Item left as agent set it.
    item = payload["resolutions"][0]["items"][0]
    assert item["applied"] is False
    assert item.get("source") != "hitl"


def test_hitl_splices_user_selector_with_inferred_strategy(tmp_path: Path, monkeypatch):
    (tmp_path / "clarifications.md").write_text(
        _HITL_CLAR_TEMPLATE.format(
            const="TBD_LOGIN_BUTTON", file="a.py", line=42, intent="submit button"
        ),
        encoding="utf-8",
    )
    payload = _hitl_payload("TBD_LOGIN_BUTTON", "a.py", 42)

    # Simulate user pasting a data-testid selector.
    monkeypatch.setattr(
        "worca_t.steps.s08_locator_resolution.prompt_user",
        lambda questions, *, agent_label: {questions[0].id: '[data-testid="login-btn"]'},
    )

    spliced = _hitl_resolve_unresolvable(wd=tmp_path, payload=payload)
    assert spliced == 1
    item = payload["resolutions"][0]["items"][0]
    assert item["applied"] is True
    assert item["replacement"] == '[data-testid="login-btn"]'
    assert item["strategy"] == "data-testid"
    assert item["source"] == "hitl"
    assert item["skip_reason"] is None


def test_hitl_splices_spec_gap_as_ghost(tmp_path: Path, monkeypatch):
    (tmp_path / "clarifications.md").write_text(
        _HITL_CLAR_TEMPLATE.format(
            const="TBD_TOOLTIP", file="a.py", line=7, intent="tooltip"
        ),
        encoding="utf-8",
    )
    payload = _hitl_payload("TBD_TOOLTIP", "a.py", 7)

    monkeypatch.setattr(
        "worca_t.steps.s08_locator_resolution.prompt_user",
        lambda questions, *, agent_label: {questions[0].id: "ghost"},
    )

    spliced = _hitl_resolve_unresolvable(wd=tmp_path, payload=payload)
    assert spliced == 1
    item = payload["resolutions"][0]["items"][0]
    assert item["applied"] is False
    assert item["comparison_verdict"] == "ghost"
    assert item["source"] == "hitl"
    assert "spec gap" in (item.get("skip_reason") or "")


def test_hitl_rejects_xpath_user_input(tmp_path: Path, monkeypatch):
    """User pasted an XPath selector → reject, mark item as skipped."""
    (tmp_path / "clarifications.md").write_text(
        _HITL_CLAR_TEMPLATE.format(
            const="TBD_X", file="a.py", line=5, intent="something"
        ),
        encoding="utf-8",
    )
    payload = _hitl_payload("TBD_X", "a.py", 5)

    monkeypatch.setattr(
        "worca_t.steps.s08_locator_resolution.prompt_user",
        lambda questions, *, agent_label: {questions[0].id: "//div[@id='x']"},
    )

    spliced = _hitl_resolve_unresolvable(wd=tmp_path, payload=payload)
    assert spliced == 1
    item = payload["resolutions"][0]["items"][0]
    assert item["applied"] is False
    assert item["strategy"] is None
    assert "XPath" in (item.get("skip_reason") or "")
    assert item["source"] == "hitl"


def test_hitl_user_skipped_answer_leaves_item_alone(tmp_path: Path, monkeypatch):
    """User pressed Enter without typing → answer absent in dict → no splice."""
    (tmp_path / "clarifications.md").write_text(
        _HITL_CLAR_TEMPLATE.format(
            const="TBD_X", file="a.py", line=5, intent="something"
        ),
        encoding="utf-8",
    )
    payload = _hitl_payload("TBD_X", "a.py", 5)

    monkeypatch.setattr(
        "worca_t.steps.s08_locator_resolution.prompt_user",
        lambda questions, *, agent_label: {},  # user skipped everything
    )

    assert _hitl_resolve_unresolvable(wd=tmp_path, payload=payload) == 0


def test_find_item_for_clarification_exact_match(tmp_path: Path):
    payload = _hitl_payload("X", "tests/foo.py", 10)
    item = _find_item_for_clarification(payload, "tests/foo.py", 10)
    assert item is not None
    assert item["line"] == 10


def test_find_item_for_clarification_basename_fallback(tmp_path: Path):
    """File field uses a slightly different leading-path style — basename
    match is the fallback (matches one of the orchestrator's path-resolution
    heuristics elsewhere in the module)."""
    payload = _hitl_payload("X", "src/pages/locators/foo.py", 10)
    item = _find_item_for_clarification(payload, "foo.py", 10)
    assert item is not None


def test_find_item_for_clarification_no_match(tmp_path: Path):
    payload = _hitl_payload("X", "tests/foo.py", 10)
    assert _find_item_for_clarification(payload, "other.py", 10) is None
    assert _find_item_for_clarification(payload, "tests/foo.py", 999) is None


# ---------------------------------------------------------------------------
# _build_user_prompt — TBD descriptions are surfaced when present
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_tbd_description():
    index = {
        "tests": [
            {
                "id": "T-login-1",
                "file": "tests/login.spec.ts",
                "tbd_markers": [
                    {
                        "line": 7,
                        "raw": "TBD_LOCATOR",
                        "description": "primary submit button on the login form",
                        "test_function": "test_login_valid",
                    }
                ],
            }
        ]
    }
    prompt = _build_user_prompt(index, "https://example.test")
    assert "intent: primary submit button on the login form" in prompt
    assert "test_login_valid" in prompt
    assert "AOM-first" in prompt
    assert "clarifications.md" in prompt


def test_build_user_prompt_handles_legacy_markers_without_description():
    """Markers without `description` (legacy / older runs) still render
    cleanly — the agent is told to infer intent from constant name in that
    case."""
    index = {
        "tests": [
            {
                "id": "T-x",
                "file": "tests/x.spec.ts",
                "tbd_markers": [{"line": 2, "raw": "TBD_LOCATOR"}],
            }
        ]
    }
    prompt = _build_user_prompt(index, "https://example.test")
    assert "T-x" in prompt
    # No description → no per-item `— intent:` segment (the em-dash form is
    # what `_build_user_prompt` emits when description is present). The bare
    # word "intent:" appears in the prompt's explanatory header text and is
    # not what we're checking here.
    assert "— intent:" not in prompt
    assert "AOM-first" in prompt


# ---------------------------------------------------------------------------
# JIT framework gate — Step 8 short-circuits for Python+pytest+Playwright
# ---------------------------------------------------------------------------


async def test_step08_jit_short_circuit_when_runtime_vendored(tmp_path: Path):
    """When framework is pytest/playwright-py AND `tests/worca_t_runtime.py`
    exists in the SUT, Step 8 returns `status: skipped` with `mode: jit` in
    the artifact — bypassing the agent invocation, the patch step, and the
    8b audit. Step 9 picks up the resolution via the JIT plugin."""
    ctx = _ctx(tmp_path)
    # Vendor the runtime plugin into the SUT.
    sut_tests = ctx.workspace.sut / "tests"
    sut_tests.mkdir(parents=True, exist_ok=True)
    (sut_tests / "worca_t_runtime.py").write_text(
        "def tbd(intent): return f'__WORCA_T_TBD__::{intent}'\n",
        encoding="utf-8",
    )
    # Seed a step-7 index claiming the pytest framework.
    step7 = ctx.workspace.root / "artifacts" / "step07"
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "tbd-index.json").write_text(
        json.dumps({
            "framework": "pytest",
            "test_root": str(ctx.workspace.sut),
            "totals": {"files": 1, "tests": 1, "tbd_locators": 2},
            "files": ["tests/worca_test_login.py"],
            "tests": [{
                "id": "T-login",
                "name": "test_login",
                "file": "tests/worca_test_login.py",
                "status": "pending",
                "tbd_markers": [
                    {"line": 5, "raw": 'tbd("submit")', "description": "submit"},
                    {"line": 6, "raw": 'tbd("password")', "description": "password"},
                ],
            }],
            "violations": [],
        }),
        encoding="utf-8",
    )
    result = await LocatorResolutionStep().run(ctx)
    assert result.success is True
    assert result.status == "skipped"
    assert "JIT mode" in (result.notes or "")
    # Stub artifact written with mode=jit and empty resolutions.
    out = ctx.workspace.step_dir(8) / "locator-resolution.json"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "jit"
    assert payload["resolutions"] == []
    assert payload["totals"]["tests_with_tbd"] == 1


async def test_step08_jit_gate_no_runtime_runs_legacy_flow(tmp_path: Path, monkeypatch):
    """If framework is pytest/playwright-py BUT the runtime plugin file isn't
    in the SUT (codegen didn't vendor it, or an older run), the JIT gate
    doesn't fire — Step 8 runs its normal flow (which here fails on missing
    Playwright MCP / SUT_BASE_URL, but that's expected for a unit test
    fixture without env)."""
    ctx = _ctx(tmp_path)
    # NO worca_t_runtime.py vendored.
    step7 = ctx.workspace.root / "artifacts" / "step07"
    step7.mkdir(parents=True, exist_ok=True)
    (step7 / "tbd-index.json").write_text(
        json.dumps({
            "framework": "pytest",
            "tests": [{
                "id": "T-x", "name": "t", "file": "tests/worca_test.py",
                "status": "pending",
                "tbd_markers": [{"line": 1, "raw": "TBD_LOCATOR"}],
            }],
        }),
        encoding="utf-8",
    )
    # Disable HITL so the no-SUT_BASE_URL path fails fast (not relevant to
    # the gate test itself, just to keep the unit test bounded).
    ctx.options.no_hitl = True
    monkeypatch.delenv("SUT_BASE_URL", raising=False)

    result = await LocatorResolutionStep().run(ctx)
    # We DON'T short-circuit: status is failed (legacy flow's BASE_URL guard)
    # rather than skipped (the JIT gate).
    assert result.status != "skipped"
