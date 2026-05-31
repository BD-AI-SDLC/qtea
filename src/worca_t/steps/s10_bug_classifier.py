"""Step 10: Bug classification via bug-report-classifier.

Reads Step 9's `run-results.json` + `bug-candidates.json` and (optionally)
`heal-log.jsonl`. When no failures exist, emits an empty, schema-valid
`bug-reports.json` and a trivial markdown summary, skipping the agent.

Otherwise stages the inputs into the agent workdir, invokes the
`bug-report-classifier` agent, validates its `bug-reports.json` against the
canonical schema, and falls back to a deterministic synthesis from
`bug-candidates.json` when the agent output is unusable. The fallback marks
every bug with a `rationale` of "auto-classified (agent output unusable)" so
downstream consumers can distinguish.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worca_t.claude_runner import run_agent
from worca_t.config import package_resource_root, step_timeout
from worca_t.logging_setup import get_logger
from worca_t.md_parser import slugify
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult

log = get_logger(__name__)


_SEVERITY_DEFAULT = "major"
_PRIORITY_DEFAULT = "P2"
_CATEGORY_DEFAULT = "functional"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("step10.load_failed", path=str(path), error=str(e))
        return None


def _categorize_attachments(items: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "screenshots": [],
        "traces": [],
        "videos": [],
        "logs": [],
    }
    for a in items or []:
        t = a.get("type")
        p = a.get("path")
        if not p:
            continue
        if t == "screenshot":
            out["screenshots"].append(p)
        elif t == "trace":
            out["traces"].append(p)
        elif t == "video":
            out["videos"].append(p)
        elif t == "log":
            out["logs"].append(p)
    return out


def _empty_report(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total_failures": 0,
            "by_severity": {"critical": 0, "major": 0, "minor": 0, "cosmetic": 0},
            "by_priority": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
            "by_category": {
                "functional": 0, "ui": 0, "performance": 0, "security": 0,
                "accessibility": 0, "integration": 0, "flaky": 0, "environment": 0,
            },
        },
        "bugs": [],
    }


def _synthesize(
    run_id: str,
    candidates: list[dict],
    heal_log: dict[str, dict],
) -> dict:
    """Deterministic fallback when the agent produces unusable output."""
    report = _empty_report(run_id)
    for idx, c in enumerate(candidates, start=1):
        test_id = c.get("test_id") or "T-unknown"
        title = c.get("title") or test_id
        attachments = _categorize_attachments(c.get("attachments", []))
        heal = heal_log.get(test_id, {})
        attempted = bool(heal.get("attempted"))
        succeeded = bool(heal.get("applied")) and (c.get("status") not in ("failed", "error"))
        if attempted and not succeeded:
            category = "flaky" if heal.get("applied") else _CATEGORY_DEFAULT
        else:
            category = _CATEGORY_DEFAULT
        bug = {
            "id": f"BUG-{slugify(run_id)}-{idx:03d}",
            "test_id": test_id,
            "title": title,
            "severity": _SEVERITY_DEFAULT,
            "priority": _PRIORITY_DEFAULT,
            "category": category,
            "component": "",
            "requirement_id": "",
            "rationale": "auto-classified (agent output unusable)",
            "impact": {
                "reproducibility": "always" if c.get("status") == "failed" else "intermittent",
            },
            "reproduction_steps": [],
            "expected": "test should pass",
            "actual": c.get("message") or c.get("status") or "unknown failure",
            "root_cause_hypothesis": "unknown",
            "attachments": attachments,
            "self_heal": {
                "attempted": attempted,
                "success": succeeded,
                "channel": "playwright" if attempted else "none",
            },
            "related_test_cases": c.get("tc_refs") or [],
            "recommended_action": {
                "immediate": "triage and assign owner",
                "short_term": "fix root cause",
                "long_term": "add regression coverage",
            },
        }
        report["bugs"].append(bug)
        report["summary"]["by_severity"][_SEVERITY_DEFAULT] += 1
        report["summary"]["by_priority"][_PRIORITY_DEFAULT] += 1
        report["summary"]["by_category"][category] += 1
    report["summary"]["total_failures"] = len(report["bugs"])
    return report


def _render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Bug Reports - run {report['run_id']}")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")
    summary = report.get("summary", {})
    lines.append("## Summary")
    lines.append(f"- Total failures: {summary.get('total_failures', 0)}")
    for axis in ("by_severity", "by_priority", "by_category"):
        counts = summary.get(axis, {})
        if not any(counts.values()):
            continue
        joined = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        lines.append(f"- {axis.replace('_', ' ').title()}: {joined}")
    lines.append("")

    bugs = report.get("bugs", [])
    if not bugs:
        lines.append("_No failing tests._")
        return "\n".join(lines) + "\n"

    lines.append("## Bugs")
    for b in bugs:
        lines.append("")
        lines.append(f"### {b.get('id')} - {b.get('title')}")
        lines.append("")
        lines.append(f"- Test: `{b.get('test_id')}`")
        lines.append(f"- Severity / Priority / Category: "
                     f"**{b.get('severity')}** / **{b.get('priority')}** / **{b.get('category')}**")
        if b.get("requirement_id"):
            lines.append(f"- Requirement: {b['requirement_id']}")
        if b.get("rationale"):
            lines.append(f"- Rationale: {b['rationale']}")
        if b.get("expected"):
            lines.append(f"- Expected: {b['expected']}")
        if b.get("actual"):
            lines.append(f"- Actual: {b['actual']}")
        if b.get("root_cause_hypothesis"):
            lines.append(f"- Root cause hypothesis: {b['root_cause_hypothesis']}")
        attachments = b.get("attachments") or {}
        for kind in ("screenshots", "traces", "videos", "logs"):
            paths = attachments.get(kind) or []
            if paths:
                lines.append(f"- {kind.capitalize()}: {', '.join(paths)}")
        sh = b.get("self_heal") or {}
        if sh.get("attempted"):
            lines.append(
                f"- Self-heal: attempted via {sh.get('channel', 'none')}, "
                f"success={bool(sh.get('success'))}"
            )
    lines.append("")
    return "\n".join(lines)


def _load_heal_log(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            test_id = row.get("test_id")
            if test_id:
                out[test_id] = {"attempted": True, "applied": row.get("applied")}
    except OSError:
        return out
    return out


def _agent_report_is_usable(payload: Any, expected_count: int) -> tuple[bool, str | None]:
    if not isinstance(payload, dict):
        return False, "agent payload is not a JSON object"
    ok, err = is_valid(payload, "bug-reports")
    if not ok:
        return False, f"schema invalid: {err}"
    bugs = payload.get("bugs", [])
    if len(bugs) != expected_count:
        return False, f"bugs count {len(bugs)} != expected {expected_count}"
    return True, None


class BugClassifierStep(Step):
    number = 10
    name = "bug-classifier"
    timeout_s = step_timeout(10)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)

        run_results = _load_json(ctx.workspace.step_dir(9) / "run-results.json")
        bug_candidates = _load_json(ctx.workspace.step_dir(9) / "bug-candidates.json") or {}
        candidates = bug_candidates.get("candidates", [])

        json_out = out_dir / "bug-reports.json"
        md_out = out_dir / "bug-reports.md"
        run_id = ctx.workspace.run_id

        # Short-circuit: no failures.
        if not candidates:
            empty = _empty_report(run_id)
            json_out.write_text(
                json.dumps(empty, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            md_out.write_text(_render_markdown(empty), encoding="utf-8")
            return StepResult(
                success=True,
                status="completed",
                outputs=[json_out, md_out],
                notes="no failures; empty report",
            )

        # Stage inputs for the agent.
        if run_results is not None:
            (wd / "run-results.json").write_text(
                json.dumps(run_results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        (wd / "bug-candidates.json").write_text(
            json.dumps(bug_candidates, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        heal_src = ctx.workspace.step_dir(9) / "self-heal" / "heal-log.jsonl"
        heal_map: dict[str, dict] = _load_heal_log(heal_src)
        if heal_src.exists():
            shutil.copy2(heal_src, wd / "heal-log.jsonl")

        strategy_src = ctx.workspace.step_dir(4) / "test-strategy.json"
        if strategy_src.exists():
            shutil.copy2(strategy_src, wd / "test-strategy.json")

        # Stage helpful docs (best-effort).
        docs_root = package_resource_root()
        for rel in (
            "templates/bug-report-template.md",
            "examples/bug-classification-example.md",
            "templates/edge-case-checklist.md",
        ):
            src = docs_root / rel
            if src.exists():
                dst = wd / "docs" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        agent = package_resource_root() / "agents" / "bug-report-classifier.agent.md"
        user_prompt = (
            f"Classify the {len(candidates)} failing test(s) in "
            f"./bug-candidates.json into structured bug reports. Use "
            f"./run-results.json, ./heal-log.jsonl (if present), and "
            f"./test-strategy.json for additional context. Emit "
            f"`./bug-reports.json` AND `./bug-reports.md`. Use run id "
            f"`{run_id}` in the JSON output."
        )

        agent_res = await run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=user_prompt,
            extra_paths=[],
            timeout_s=self.timeout_s,
            step=10,
            max_turns=20,
        )

        produced_json = wd / "bug-reports.json"
        produced_md = wd / "bug-reports.md"
        agent_payload: dict | None = None

        if agent_res.success and produced_json.exists():
            try:
                agent_payload = json.loads(produced_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                log.warning("step10.agent_json_invalid", error=str(e))
                agent_payload = None

        usable, why_not = _agent_report_is_usable(agent_payload, len(candidates))
        used_fallback = not usable
        if used_fallback:
            log.warning(
                "step10.fallback_synthesis",
                reason=why_not,
                agent_success=agent_res.success,
            )
            payload = _synthesize(run_id, candidates, heal_map)
            json_out.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            md_out.write_text(_render_markdown(payload), encoding="utf-8")
        else:
            shutil.copy2(produced_json, json_out)
            if produced_md.exists():
                shutil.copy2(produced_md, md_out)
            else:
                # Agent forgot the markdown; render from its JSON.
                md_out.write_text(_render_markdown(agent_payload), encoding="utf-8")

        notes = (
            f"bugs={len(candidates)} fallback={used_fallback}"
            + (f" reason={why_not}" if why_not else "")
        )
        status = "warned" if used_fallback else "completed"
        return StepResult(
            success=True,
            status=status,
            outputs=[json_out, md_out],
            notes=notes,
        )


__all__ = [
    "BugClassifierStep",
    "_agent_report_is_usable",
    "_categorize_attachments",
    "_empty_report",
    "_load_heal_log",
    "_render_markdown",
    "_synthesize",
]
