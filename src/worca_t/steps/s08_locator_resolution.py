"""Step 8: Locator resolution via playwright-tester.

Reads tests-with-tbd.json from Step 7 and the generated tests under
artifacts/step07/tests/. For tests containing `TBD_LOCATOR` markers,
invokes the playwright-tester agent (which has access to the Playwright
MCP and can browse the live SUT at $SUT_BASE_URL) to discover real
locators. The agent writes `./locator-resolution.json` following the
schema. We then deterministically patch the test files in-place, refuse
any XPath replacement, and re-index to confirm zero TBD markers remain.

Outputs (artifacts/step08/):
  - locator-resolution.json   (agent output + applied/skipped per item)
  - tests/                    (mutated copy of step07 tests, patched)
  - tests-with-tbd.json       (re-indexed; expected `tbd_locators == 0`)
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.test_indexer import index_tests, resolve_framework

log = get_logger(__name__)

# Locator strategies, ranked highest-priority first.
_PRIORITY = ("id", "data-testid", "role", "label", "text", "placeholder", "css")


def _is_xpath_replacement(replacement: str) -> bool:
    """Reject XPath replacements regardless of agent claim."""
    s = replacement.strip()
    if s.startswith("xpath="):
        return True
    if s.startswith("//"):
        return True
    return "By.XPATH" in s


def _load_index(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tests_with_tbd(index: dict) -> list[dict]:
    return [t for t in index.get("tests", []) if t.get("tbd_markers")]


def _build_user_prompt(index: dict, sut_base_url: str | None) -> str:
    items: list[str] = []
    for t in _tests_with_tbd(index):
        markers = "; ".join(
            f"line {m['line']}: {m['raw'][:80]}"
            for m in t["tbd_markers"]
        )
        items.append(f"- {t['id']}  ({t['file']}):  {markers}")
    listing = "\n".join(items) or "(no TBD markers detected)"
    base = sut_base_url or "(unset)"
    return (
        f"You are resolving TBD_LOCATOR placeholders left by an upstream "
        f"codegen step. Base URL of the SUT: `{base}`. Use the Playwright "
        f"MCP to navigate and take AOM snapshots; do not generate code, only "
        f"discover the correct locators. Honor the project's locator "
        f"priority: id > data-testid > role > label > text > placeholder > "
        f"scoped css. Never propose XPath. Tests requiring resolution:\n\n"
        f"{listing}\n\nWrite the result to `./locator-resolution.json` "
        f"following this shape: "
        f'{{"base_url": "...", "resolutions": [{{"test_id": "<id>", "file": "<rel>", '
        f'"items": [{{"tbd": "<exact TBD token>", "replacement": "<locator>", '
        f'"strategy": "<one of: id|data-testid|role|label|text|placeholder|css>", '
        f'"line": <int>, "confidence": <0..1>}}]}}]}}'
    )


def _rank_strategy(strategy: str) -> int:
    return _PRIORITY.index(strategy) if strategy in _PRIORITY else len(_PRIORITY)


def _apply_patches(
    tests_dir: Path,
    resolutions: list[dict],
) -> list[dict]:
    """Mutate tests_dir files in place. Returns a copy of `resolutions` with
    each item annotated with `applied` and (when skipped) `skip_reason`."""
    out: list[dict] = []
    # Group by file to load once.
    by_file: dict[str, list[tuple[dict, dict]]] = {}
    for r in resolutions:
        file_rel = r.get("file")
        if not file_rel:
            continue
        for item in r.get("items", []):
            by_file.setdefault(file_rel, []).append((r, item))

    for file_rel, entries in by_file.items():
        path = tests_dir / file_rel
        if not path.exists():
            for r, item in entries:
                annotated = dict(item, applied=False, skip_reason=f"file not found: {file_rel}")
                out.append(annotated)
                _record(r, annotated)
            continue

        text = path.read_text(encoding="utf-8")

        # Sort items by priority strategy first so that higher-priority
        # replacements are attempted first when multiple entries target the
        # same TBD token (defence in depth - the agent should not duplicate).
        entries.sort(key=lambda e: _rank_strategy(e[1].get("strategy", "css")))

        for r, item in entries:
            replacement = item.get("replacement", "")
            strategy = item.get("strategy", "")
            tbd_token = item.get("tbd", "TBD_LOCATOR")
            if strategy not in _PRIORITY:
                annotated = dict(item, applied=False, skip_reason=f"unknown strategy: {strategy}")
            elif _is_xpath_replacement(replacement):
                annotated = dict(item, applied=False, skip_reason="xpath replacement rejected")
            elif tbd_token not in text:
                annotated = dict(item, applied=False, skip_reason=f"token not found: {tbd_token!r}")
            else:
                text = text.replace(tbd_token, replacement, 1)
                annotated = dict(item, applied=True, skip_reason=None)
            out.append(annotated)
            _record(r, annotated)

        path.write_text(text, encoding="utf-8")
    return out


def _record(resolution: dict, annotated_item: dict) -> None:
    """Replace the matching item in the resolution dict with the annotated one."""
    items: list[dict] = resolution.get("items", [])
    for i, it in enumerate(items):
        if (
            it.get("tbd") == annotated_item.get("tbd")
            and it.get("line") == annotated_item.get("line")
            and it.get("strategy") == annotated_item.get("strategy")
        ):
            items[i] = annotated_item
            return
    items.append(annotated_item)


def _ensure_files_for(resolutions: list[dict], index: dict) -> None:
    """If an agent omitted `file` on a resolution, look it up by test_id."""
    by_id = {t["id"]: t.get("file") for t in index.get("tests", [])}
    for r in resolutions:
        if not r.get("file"):
            r["file"] = by_id.get(r.get("test_id"))


def _empty_resolution(index: dict, sut_base_url: str | None) -> dict[str, Any]:
    return {
        "base_url": sut_base_url,
        "resolutions": [],
        "totals": {
            "tests_with_tbd": len(_tests_with_tbd(index)),
            "items": 0,
            "applied": 0,
            "skipped": 0,
        },
    }


class LocatorResolutionStep(Step):
    number = 8
    name = "locator-resolution"
    timeout_s = step_timeout(8)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        src_index = ctx.workspace.step_dir(7) / "tests-with-tbd.json"
        src_tests = ctx.workspace.step_dir(7) / "tests"
        if not src_index.exists() or not src_tests.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="step 8 requires step 7 outputs (tests-with-tbd.json + tests/)",
            )

        index = _load_index(src_index)
        sut_base_url = os.environ.get("SUT_BASE_URL")

        if not sut_base_url and _tests_with_tbd(index):
            log.warning(
                "step08.sut_base_url_missing",
                hint="SUT_BASE_URL is not set; locator resolution via Playwright "
                     "may fail. Set it in your environment, via --env-file, or "
                     "respond to the Step 6 interactive prompt.",
            )

        # Copy tests into step08 artifact dir; we mutate the copy.
        dst_tests = out_dir / "tests"
        if dst_tests.exists():
            shutil.rmtree(dst_tests)
        shutil.copytree(src_tests, dst_tests)

        tests_needing = _tests_with_tbd(index)
        resolution_path = out_dir / "locator-resolution.json"

        # If nothing to do, write a trivial resolution file and short-circuit.
        if not tests_needing:
            payload = _empty_resolution(index, sut_base_url)
            resolution_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            framework = index.get("framework", resolve_framework(None, dst_tests))
            re_idx = index_tests(dst_tests, framework=framework).as_dict()
            (out_dir / "tests-with-tbd.json").write_text(
                json.dumps(re_idx, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return StepResult(
                success=True,
                status="completed",
                outputs=[resolution_path, dst_tests],
                notes="no TBD markers; nothing to resolve",
            )

        # Stage inputs for the agent.
        staged_tests = wd / "tests"
        if staged_tests.exists():
            shutil.rmtree(staged_tests)
        shutil.copytree(dst_tests, staged_tests)
        staged_index = wd / "tests-with-tbd.json"
        staged_index.write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        agents_root = package_resource_root() / "agents"
        skills_root = package_resource_root() / "skills"
        agent = agents_root / "playwright-tester.agent.md"
        claude_md = package_resource_root() / "CLAUDE.md"

        extras: list[Path] = []
        for skill in ("playwright-explore-website",):
            p = skills_root / skill
            if p.exists():
                extras.append(p)

        result = await run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=_build_user_prompt(index, sut_base_url),
            extra_paths=extras,
            timeout_s=self.timeout_s,
            step=8,
            max_turns=60,
            claude_md=claude_md if claude_md.exists() else None,
        )

        produced = wd / "locator-resolution.json"
        if not result.success or not produced.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=result.error or "locator-resolution.json not produced",
            )

        try:
            payload = json.loads(produced.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=f"locator-resolution.json is not valid JSON: {e}",
            )

        resolutions = payload.get("resolutions") or []
        _ensure_files_for(resolutions, index)
        applied_items = _apply_patches(dst_tests, resolutions)
        applied_count = sum(1 for it in applied_items if it.get("applied"))
        skipped_count = sum(1 for it in applied_items if not it.get("applied"))

        payload.setdefault("base_url", sut_base_url)
        payload["totals"] = {
            "tests_with_tbd": len(tests_needing),
            "items": len(applied_items),
            "applied": applied_count,
            "skipped": skipped_count,
        }
        resolution_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok_schema, schema_err = is_valid(payload, "locator-resolution")
        if not ok_schema:
            log.warning("step08.schema_invalid", error=schema_err)

        # Re-index the patched tests; surface any *new* violations and the
        # remaining TBD count.
        framework = index.get("framework", resolve_framework(None, dst_tests))
        reindex = index_tests(dst_tests, framework=framework)
        (out_dir / "tests-with-tbd.json").write_text(
            json.dumps(reindex.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

        remaining_tbd = sum(len(t.tbd_markers) for t in reindex.tests)
        if reindex.violations:
            return StepResult(
                success=False,
                status="failed",
                outputs=[resolution_path],
                error=(
                    f"patched tests introduced violations: "
                    f"{[v.rule for v in reindex.violations]}"
                ),
            )

        notes = (
            f"applied={applied_count} skipped={skipped_count} "
            f"remaining_tbd={remaining_tbd}"
        )
        if remaining_tbd > 0:
            notes += "; some markers unresolved"

        # Treat unresolved markers as a warning, not a hard failure: the
        # downstream tester (step 9) is responsible for surfacing test failures.
        status = "warned" if remaining_tbd > 0 or not ok_schema else "completed"
        if not ok_schema:
            notes += f"; schema_warning={schema_err}"
        return StepResult(
            success=True,
            status=status,
            outputs=[resolution_path, out_dir / "tests-with-tbd.json", dst_tests],
            notes=notes,
        )


# Internal helpers exposed for unit testing.
__all__ = [
    "LocatorResolutionStep",
    "_apply_patches",
    "_build_user_prompt",
    "_ensure_files_for",
    "_is_xpath_replacement",
    "_rank_strategy",
    "_tests_with_tbd",
]


_PRIORITY_RE = re.compile("|".join(_PRIORITY))  # kept for future helpers
