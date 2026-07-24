"""Stage-3 assertion-intent judge (Step 8) — SHADOW mode.

The deterministic gates (Stage 1/2: schema oracle enforcement, the TS/Python
body-verifiers, the zero-assertion + escape-hatch scans) catch STRUCTURAL and
value-binding false-greens. They cannot judge two *semantic* questions the
user posed: (1) do a test's assertions logically verify a derivative of its
TITLE + the methods it calls, pinned to the Step-4 oracle; and (2) does the
test execute every act-phase choreography step the plan prescribed, in order,
before asserting (a test cannot claim to cover a scenario it silently stops
short of)?

This module asks both questions with an INDEPENDENT LLM judge (a different model
+ persona than the code writer), fed the oracle + choreography + Step-4 prose as
ground truth and prompted adversarially ("assume false-green or incomplete;
prove otherwise"). It runs in **shadow mode**: verdicts are written to
``assertion-judge-shadow.json`` and logged, but never block the step. This
gathers real-run agreement/false-positive data before the judge is ever
promoted to blocking (the SDET-agreed rollout).

Enable/disable via ``QTEA_ASSERTION_JUDGE``:
  - ``shadow`` (default) — run, log, never block.
  - ``off``              — do not run.
  - ``block``            — reserved; treated as ``shadow`` for now (a warning is
                            logged) until shadow data justifies promotion.

Boundary note: Python still owns all pass/fail decisions. In shadow mode the
judge changes nothing; even when promoted it would only gate on a
schema-validated boolean — consistent with "Python never reasons".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.schemas import is_valid, load_schema

log = get_logger(__name__)

_PY_EXTS = (".py",)
_JSTS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_MAX_BODY_CHARS = 8000
_MAX_STRATEGY_CHARS = 6000


def _mode() -> str:
    m = (os.environ.get("QTEA_ASSERTION_JUDGE", "shadow") or "shadow").lower()
    return m if m in ("shadow", "off", "block") else "shadow"


def _test_globs(language: str) -> tuple[str, ...]:
    lang = (language or "").lower()
    if lang in ("python", "pytest", "playwright-py", "selenium-py"):
        return ("qtea_*.py",)
    return ("qtea_*.spec.ts", "qtea_*.test.ts", "qtea_*.spec.js",
            "qtea_*.test.js", "Qtea*.java")


def _collect_oracle(plan_data: dict) -> list[dict]:
    """Flatten the plan's assertion oracle into a compact, judge-friendly list.

    Covers both codegen lanes: POM (`page_objects[].missing_methods[]`) and
    exemplar/non-POM (`reusable_units[].missing_behaviors[]`) — the latter
    uses the identical `kind`/`acceptance_criteria` shape (see
    `schemas/code-modification-plan.schema.json`, `$defs.reusable_unit`), so
    omitting it previously left the exemplar lane's oracle silently empty.
    """
    out: list[dict] = []
    for tc in plan_data.get("test_cases") or []:
        if not isinstance(tc, dict):
            continue
        for po in tc.get("page_objects") or []:
            if not isinstance(po, dict):
                continue
            for mm in po.get("missing_methods") or []:
                if not isinstance(mm, dict) or mm.get("kind") != "assertion":
                    continue
                out.append({
                    "tc": tc.get("id"),
                    "pom": po.get("name"),
                    "method": mm.get("name"),
                    "signature": mm.get("signature"),
                    "purpose": mm.get("purpose"),
                    "acceptance_criteria": mm.get("acceptance_criteria") or [],
                })
        for ru in tc.get("reusable_units") or []:
            if not isinstance(ru, dict):
                continue
            for mb in ru.get("missing_behaviors") or []:
                if not isinstance(mb, dict) or mb.get("kind") != "assertion":
                    continue
                out.append({
                    "tc": tc.get("id"),
                    "pom": ru.get("name"),
                    "method": mb.get("name"),
                    "signature": mb.get("signature"),
                    "purpose": mb.get("purpose"),
                    "acceptance_criteria": mb.get("acceptance_criteria") or [],
                })
    return out


def _collect_sequence_oracle(plan_data: dict) -> list[dict]:
    """Flatten each test function's act-phase choreography into a compact,
    judge-friendly ordered sequence — the oracle for ``sequence_complete``.

    Arrange-phase steps are excluded: they are frequently absorbed into
    fixtures, so their absence from a test body is not evidence of a defect.
    `phase` defaults to "act" when omitted (schema default). Test functions
    with no `steps[]` (older-style plans, or genuinely choreography-free
    cases) are omitted — the judge falls back to `sequence_complete: true`.
    """
    out: list[dict] = []
    for tc in plan_data.get("test_cases") or []:
        if not isinstance(tc, dict):
            continue
        for tf in tc.get("test_functions") or []:
            if not isinstance(tf, dict):
                continue
            act_steps = [
                s for s in (tf.get("steps") or [])
                if isinstance(s, dict) and (s.get("phase") or "act") == "act"
            ]
            if not act_steps:
                continue
            act_steps.sort(key=lambda s: s.get("order") or 0)
            out.append({
                "tc": tc.get("id"),
                "test_function": tf.get("name"),
                "steps": [
                    {
                        "order": s.get("order"),
                        "pom": s.get("pom"),
                        "method": s.get("method"),
                    }
                    for s in act_steps
                ],
            })
    return out


async def judge_assertions_shadow(
    *,
    plan_data: dict,
    strategy_text: str,
    sut_root: Path,
    out_dir: Path,
    agents_root: Path,
    workdir: Path,
    language: str,
) -> dict | None:
    """Run the assertion-intent judge over the generated tests (shadow).

    Best-effort and non-raising: any failure logs a warning and returns None.
    Writes ``<out_dir>/assertion-judge-shadow.json`` with the verdicts and a
    summary. NEVER affects the caller's StepResult.
    """
    mode = _mode()
    if mode == "off":
        return None
    if mode == "block":
        log.warning(
            "step08.assertion_judge.block_not_supported",
            hint="QTEA_ASSERTION_JUDGE=block is reserved; running in shadow "
                 "(the judge cannot yet block a step — gather shadow data first)",
        )

    try:
        exts = _test_globs(language)
        test_files: list[Path] = []
        for g in exts:
            test_files.extend(
                p for p in sut_root.rglob(g)
                if p.is_file() and ".git" not in p.parts
            )
        test_files = sorted(set(test_files))
        oracle = _collect_oracle(plan_data)
        sequence_oracle = _collect_sequence_oracle(plan_data)
        if not test_files or not oracle:
            log.info(
                "step08.assertion_judge.skipped",
                reason="no generated tests or no assertion oracle",
                files=len(test_files), oracle=len(oracle),
            )
            return None

        tests_payload: list[dict] = []
        for tf in test_files:
            try:
                body = tf.read_text(encoding="utf-8")
            except OSError:
                continue
            tests_payload.append({
                "file": tf.name,
                "body": body[:_MAX_BODY_CHARS],
            })

        agent_path = agents_root / "assertion-intent-judge.agent.md"
        if not agent_path.is_file():
            log.warning("step08.assertion_judge.agent_missing", path=str(agent_path))
            return None

        result = await call_reasoning_llm(
            agent_path,
            workdir=workdir,
            user_prompt=(
                "Judge whether each generated test's assertions logically verify "
                "a derivative of its title + the methods it calls, pinned to the "
                "oracle, AND whether it executes every act-phase choreography "
                "step in order before asserting. Assume false-green or "
                "incomplete and try to prove otherwise. Return exactly one "
                "verdict per test function found across the files."
            ),
            inputs={
                "generated_tests.json": json.dumps(
                    {"tests": tests_payload}, indent=2,
                ),
                "oracle.json": json.dumps({"oracle": oracle}, indent=2),
                "sequence-oracle.json": json.dumps(
                    {"sequences": sequence_oracle}, indent=2,
                ),
                "strategy.md": (strategy_text or "")[:_MAX_STRATEGY_CHARS],
            },
            output_schema=load_schema("assertion-verdict"),
            timeout_s=180,
            max_tokens=16000,
            step=8,
        )

        if not result.success or not (result.final_text or "").strip():
            log.warning(
                "step08.assertion_judge.failed", error=result.error,
            )
            return None
        try:
            verdicts_doc: dict[str, Any] = json.loads(result.final_text)
        except json.JSONDecodeError as e:
            log.warning("step08.assertion_judge.unparseable", error=str(e))
            return None

        # The Vertex/BMF backend cannot enforce `output_schema` server-side
        # (see llm/reasoning.py), so a malformed verdict (e.g. missing
        # `sequence_complete`) would otherwise fall through to the `.get(...,
        # True)` defaults below and silently read as "no false-green" —
        # defeating the exact check this judge exists to run. Re-validate
        # locally and discard the whole batch rather than trust a partial
        # shape.
        ok, err = is_valid(verdicts_doc, "assertion-verdict")
        if not ok:
            log.warning("step08.assertion_judge.schema_invalid", error=err)
            return None

        verdicts = verdicts_doc.get("verdicts") or []
        flagged = [
            v for v in verdicts
            if isinstance(v, dict) and (
                not v.get("verifies_intent")
                or not v.get("binds_oracle")
                or (v.get("weakness") not in (None, "none"))
                or not v.get("sequence_complete", True)
            )
        ]
        summary = {
            "mode": mode,
            "total": len(verdicts),
            "flagged": len(flagged),
            "ok": len(verdicts) - len(flagged),
        }
        payload = {"summary": summary, "verdicts": verdicts}
        try:
            (out_dir / "assertion-judge-shadow.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("step08.assertion_judge.write_failed", error=str(e))

        log.info(
            "step08.assertion_judge.shadow",
            **summary,
            flagged_tests=[v.get("test") for v in flagged][:10],
        )
        if flagged:
            log.warning(
                "step08.assertion_judge.would_flag",
                count=len(flagged),
                hint="SHADOW: these tests may be false-green (weak/missing "
                     "assertion vs oracle). Not blocking — review "
                     "assertion-judge-shadow.json.",
            )
        return payload
    except Exception as e:  # never let the judge break codegen
        log.warning("step08.assertion_judge.error", error=str(e))
        return None
