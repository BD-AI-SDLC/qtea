"""Step 8 purpose-fidelity judge — POM method body vs its own `purpose`.

``codegen_body_verify.verify_method_bodies`` already regex-verifies
``kind: "assertion"`` methods whose ``acceptance_criteria`` use a
*structured* check (``exact_text``, ``exact_count``, ``boundingbox_below``,
etc.). It structurally CANNOT verify two shapes of missing method:

- ``kind: "action"`` / ``kind: "query"`` — no ``acceptance_criteria`` at all,
  only a ``purpose`` string.
- ``kind: "assertion"`` with a ``check: "custom"`` criterion — an
  unenumerated expected value (e.g. an exact copy string not yet pinned).

This module asks an INDEPENDENT LLM judge (different persona than the
pom-extender that wrote the code) whether each such method's generated BODY
actually implements what its own ``purpose`` declares, fed the actual
generated code as ground truth and prompted adversarially ("assume it's a
stub or wrong; prove otherwise").

Enable/disable via ``QTEA_PURPOSE_JUDGE`` (mirrors ``QTEA_ASSERTION_JUDGE``):
  - ``shadow`` (default) — run, log to ``purpose-fidelity-shadow.json``,
    never block.
  - ``off``              — do not run.
  - ``block``            — flagged methods trigger one targeted auto-repair
                            retry (re-invoke the pom-extender on just that
                            method with the verdict's reasoning appended to
                            its purpose); still-failing methods after the
                            retry hard-fail Step 8.

Boundary note: Python still owns all pass/fail decisions. In shadow mode the
judge changes nothing; in block mode it only gates on a schema-validated
boolean — consistent with "Python never reasons".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from qtea.codegen_body_verify import (
    _java_method_bodies,
    _js_method_bodies,
    _py_method_bodies,
)
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.schemas import load_schema

log = get_logger(__name__)

_MAX_BODY_CHARS = 4000
_SHADOW_FILENAME = "purpose-fidelity-shadow.json"


def _mode() -> str:
    m = (os.environ.get("QTEA_PURPOSE_JUDGE", "shadow") or "shadow").lower()
    return m if m in ("shadow", "off", "block") else "shadow"


def _is_judgeable(m: dict) -> bool:
    """kind action/query (no acceptance_criteria), or assertion+check=custom."""
    if not isinstance(m, dict):
        return False
    kind = m.get("kind")
    if kind in ("action", "query"):
        return True
    if kind == "assertion":
        criteria = m.get("acceptance_criteria") or []
        return any(
            isinstance(c, dict) and c.get("check") == "custom" for c in criteria
        )
    return False


def collect_judgeable_methods(pom_tasks: dict[str, Any]) -> dict[str, list[dict]]:
    """Return ``{pom_file: [missing_method dict, ...]}`` filtered to the blind spot.

    Only methods ``codegen_body_verify.verify_method_bodies`` structurally
    cannot check — see module docstring. Most POMs will contribute nothing
    (empty dict = fast no-op for the caller).
    """
    out: dict[str, list[dict]] = {}
    for pom_file, task in pom_tasks.items():
        methods = [m for m in (task.missing_methods or []) if _is_judgeable(m)]
        if methods:
            out[pom_file] = methods
    return out


def _method_bodies_for(pom_abs: Path, class_name: str, language: str) -> dict[str, str]:
    lang = (language or "").lower()
    try:
        src = pom_abs.read_text(encoding="utf-8")
    except OSError:
        return {}
    if lang in ("python", "pytest", "playwright-py", "selenium-py"):
        return _py_method_bodies(src, class_name)
    if lang in ("java", "selenium-java", "playwright-java"):
        return _java_method_bodies(src, class_name)
    return _js_method_bodies(src, class_name)


def _allowed_locators_for(pom_name: str, locator_tasks: list[Any] | None) -> list[str]:
    return sorted({
        lt.constant_name for lt in (locator_tasks or [])
        if getattr(lt, "owning_page", None) == pom_name
    })


def _flagged(verdicts: list[dict]) -> list[dict]:
    return [
        v for v in verdicts
        if isinstance(v, dict) and (
            not v.get("fulfills_purpose")
            or v.get("weakness") not in (None, "none")
        )
    ]


async def judge_purpose_fidelity(
    *,
    pom_tasks: dict[str, Any],
    sut_root: Path,
    out_dir: Path,
    agents_root: Path,
    workdir: Path,
    language: str,
    locator_tasks: list[Any] | None = None,
    shadow_filename: str = _SHADOW_FILENAME,
) -> dict | None:
    """Judge blind-spot POM methods' bodies against their own ``purpose``.

    Best-effort and non-raising: any failure logs a warning and returns
    ``None``. Writes ``<out_dir>/<shadow_filename>``. In ``shadow`` mode
    (default) this NEVER affects the caller's ``StepResult`` — the caller
    only acts on the returned summary when ``QTEA_PURPOSE_JUDGE=block``.
    """
    mode = _mode()
    if mode == "off":
        return None
    try:
        judgeable = collect_judgeable_methods(pom_tasks)
        if not judgeable:
            log.info("step08.purpose_judge.skipped", reason="no blind-spot methods")
            return None

        agent_path = agents_root / "purpose-fidelity-judge.agent.md"
        if not agent_path.is_file():
            log.warning("step08.purpose_judge.agent_missing", path=str(agent_path))
            return None

        all_verdicts: list[dict] = []
        for pom_file, methods in judgeable.items():
            task = pom_tasks[pom_file]
            pom_abs = sut_root / pom_file
            if not pom_abs.is_file():
                continue
            bodies = _method_bodies_for(pom_abs, task.pom_name, language)
            methods_payload: list[dict] = []
            bodies_payload: dict[str, str] = {}
            for m in methods:
                name = m.get("name", "")
                body = bodies.get(name, "")
                if not body:
                    all_verdicts.append({
                        "method": name, "pom": task.pom_name,
                        "fulfills_purpose": False, "weakness": "stub_or_noop",
                        "reasoning": f"method {name!r} not found in generated {pom_file}",
                    })
                    continue
                methods_payload.append({
                    "name": name,
                    "pom": task.pom_name,
                    "signature": m.get("signature"),
                    "kind": m.get("kind"),
                    "purpose": m.get("purpose"),
                    "acceptance_criteria": m.get("acceptance_criteria") or [],
                })
                bodies_payload[name] = body[:_MAX_BODY_CHARS]
            if not methods_payload:
                continue

            result = await call_reasoning_llm(
                agent_path,
                workdir=workdir,
                user_prompt=(
                    "Judge whether each POM method's generated body actually "
                    "fulfills its own `purpose`. Assume each is a stub or wrong "
                    "and try to prove otherwise. Return exactly one verdict per "
                    "method in `methods.json`, in the same order."
                ),
                inputs={
                    "methods.json": json.dumps({"methods": methods_payload}, indent=2),
                    "method_bodies.json": json.dumps(bodies_payload, indent=2),
                    "allowed_locators.json": json.dumps(
                        {"allowed": _allowed_locators_for(task.pom_name, locator_tasks)},
                        indent=2,
                    ),
                },
                output_schema=load_schema("purpose-fidelity-verdict"),
                timeout_s=180,
                max_tokens=16000,
                step=8,
            )
            if not result.success or not (result.final_text or "").strip():
                log.warning(
                    "step08.purpose_judge.call_failed",
                    pom=task.pom_name, error=result.error,
                )
                continue
            try:
                verdicts_doc: dict[str, Any] = json.loads(result.final_text)
            except json.JSONDecodeError as e:
                log.warning(
                    "step08.purpose_judge.unparseable",
                    pom=task.pom_name, error=str(e),
                )
                continue
            for v in verdicts_doc.get("verdicts") or []:
                if isinstance(v, dict):
                    v.setdefault("pom", task.pom_name)
                    all_verdicts.append(v)

        if not all_verdicts:
            return None

        flagged = _flagged(all_verdicts)
        summary = {
            "mode": mode,
            "total": len(all_verdicts),
            "flagged": len(flagged),
            "ok": len(all_verdicts) - len(flagged),
        }
        payload = {"summary": summary, "verdicts": all_verdicts}
        try:
            (out_dir / shadow_filename).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("step08.purpose_judge.write_failed", error=str(e))

        log.info(
            "step08.purpose_judge.result",
            **summary,
            flagged_methods=[v.get("method") for v in flagged][:10],
        )
        if flagged and mode == "shadow":
            log.warning(
                "step08.purpose_judge.would_flag",
                count=len(flagged),
                hint="SHADOW: these methods may not fulfill their purpose. "
                     "Not blocking — review purpose-fidelity-shadow.json.",
            )
        return payload
    except Exception as e:  # never let the judge break codegen
        log.warning("step08.purpose_judge.error", error=str(e))
        return None


async def judge_and_repair_blocking(
    pf_result: dict,
    *,
    pom_tasks: dict[str, Any],
    sut_root: Path,
    out_dir: Path,
    wd: Path,
    agents_root: Path,
    step: int,
    rules_content: str = "",
    ctx: Any = None,
    language: str = "typescript",
    locator_tasks: list[Any] | None = None,
) -> list[str]:
    """Auto-repair loop for ``QTEA_PURPOSE_JUDGE=block``.

    For each flagged verdict, synthesize a single-method patch task (original
    ``name``/``signature``/``kind``/``acceptance_criteria``, ``purpose``
    amended with the judge's reasoning), re-invoke the pom-extender ONCE
    (mirrors the existing Phase B.5 auto-patch-once pattern in
    ``s08_codegen.py``), then re-judge only the retried methods.

    Returns a list of formatted violation strings still flagged after the
    retry — empty means every flagged method was successfully repaired.
    """
    # Deferred import — dodges the s08_codegen -> purpose_judge -> s08_codegen
    # cycle at module load (same reason codegen_reconcile.mismatches_to_pom_tasks
    # defers this identical import).
    from qtea.steps.s08_codegen import _PomTask, _extend_poms

    verdicts = pf_result.get("verdicts") or []
    flagged = _flagged(verdicts)
    if not flagged:
        return []

    flagged_by_pom_name: dict[str, list[dict]] = {}
    for v in flagged:
        flagged_by_pom_name.setdefault(v.get("pom", ""), []).append(v)

    by_pom_name: dict[str, tuple[str, Any]] = {
        task.pom_name: (pom_file, task) for pom_file, task in pom_tasks.items()
    }

    patch_tasks: dict[str, Any] = {}
    for pom_name, pom_verdicts in flagged_by_pom_name.items():
        entry = by_pom_name.get(pom_name)
        if entry is None:
            continue
        pom_file, base_task = entry
        by_method_name = {
            m.get("name"): m for m in (base_task.missing_methods or [])
            if isinstance(m, dict)
        }
        patched_methods: list[dict] = []
        for v in pom_verdicts:
            orig = by_method_name.get(v.get("method"))
            if orig is None:
                continue
            amended = dict(orig)
            reasoning = v.get("reasoning") or "unspecified"
            amended["purpose"] = (
                f"{orig.get('purpose', '')}\n\n[PURPOSE-FIDELITY RETRY] Previous "
                f"implementation did not fulfill purpose: {reasoning} Fix the "
                f"logic accordingly."
            )
            patched_methods.append(amended)
        if patched_methods:
            patch_tasks[pom_file] = _PomTask(
                pom_name=base_task.pom_name, pom_file=base_task.pom_file,
                source=base_task.source, from_path=base_task.from_path,
                at_path=base_task.at_path, missing_methods=patched_methods,
                locator_file=base_task.locator_file,
                locator_class=base_task.locator_class,
            )

    def _format_still_failing(vs: list[dict], *, retried: bool) -> list[str]:
        prefix = "still failing after repair" if retried else "repair not attempted"
        return [
            f"{v.get('pom')}::{v.get('method')}: {prefix} — {v.get('reasoning', '')}"
            for v in vs
        ]

    if not patch_tasks:
        return _format_still_failing(flagged, retried=False)

    try:
        await _extend_poms(
            patch_tasks, sut_root, wd, agents_root, step,
            rules_content=rules_content, ctx=ctx,
        )
    except Exception as e:
        log.error("step08.purpose_judge.repair_crashed", error=str(e))
        return _format_still_failing(flagged, retried=False)

    recheck = await judge_purpose_fidelity(
        pom_tasks=patch_tasks, sut_root=sut_root, out_dir=out_dir,
        agents_root=agents_root, workdir=wd, language=language,
        locator_tasks=locator_tasks,
        shadow_filename="purpose-fidelity-recheck.json",
    )
    if recheck is None:
        # No parseable re-judgment came back — conservative: treat as unresolved.
        return _format_still_failing(flagged, retried=True)

    return _format_still_failing(_flagged(recheck.get("verdicts") or []), retried=True)
