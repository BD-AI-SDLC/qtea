"""JIT-resolver bug-candidate emitters (dev-pool drift + unresolvable TBDs).

Two shapes of bug candidate that surface not from a failing pytest row but
from the JIT resolver's own telemetry:

- ``_bug_candidates_for_dev_pool_drift`` reads
  ``<workspace>/locator-cache/dev-pool-quarantine.jsonl`` (written when a
  tier-1b dev-pool selector failed at action time and the runtime fell
  back to LLM under a shadow cache slot). Emits one candidate per unique
  quarantine event pointing the user at the dev-locators entry that has
  drifted.
- ``_bug_candidates_for_unresolvable_tbds`` emits one ``locator-unresolvable``
  candidate per HITL-unanswered TBD, so the operator sees the intent + page
  URL and can either add the entry to dev-locators.json or remove the tbd().

Both are pure functions over their inputs — safe to unit-test in isolation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def _bug_candidates_for_dev_pool_drift(quarantine_log: Path) -> list[dict]:
    """Read ``dev-pool-quarantine.jsonl`` and emit one bug-candidate per
    dev-pool selector that failed at action time.

    The JIT runtime writes one JSONL record per quarantine event. Each
    candidate guides the user to update the dev-locators file OR let
    qtea re-resolve fresh (delete the entry from dev-locators.json).
    """
    if not quarantine_log.exists():
        return []
    try:
        lines = quarantine_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        const = record.get("constant_name") or "unknown"
        matched = record.get("matched_constant")
        # Dedupe within a single run: many tests may hit the same drift.
        key = f"{const}::{matched or ''}::{record.get('intent', '')}"
        if key in seen:
            continue
        seen.add(key)
        intent = record.get("intent") or ""
        stale = record.get("stale_selector")
        out.append({
            "id": f"BC-dev-locator-drifted-{matched or const}",
            "test_id": f"dev-locator-drifted:{matched or const}",
            "title": (
                f"Dev locator {(matched or const)!r} drifted at runtime"
            ),
            "file": record.get("test_file"),
            "status": "error",
            "kind": "dev-locator-drifted",
            "message": (
                f"Tier 1b dev-pool selector for intent {intent!r} "
                f"({stale!r}) failed at action time on "
                f"{record.get('page_url') or '(unknown URL)'}: "
                f"{record.get('exception') or 'TimeoutError'}. "
                f"The JIT runtime fell back to the LLM resolver under a "
                f"shadow cache key; the dev-locators entry was NOT "
                f"overwritten. Update the selector for "
                f"{(matched or const)!r} in dev-locators.json, OR remove "
                f"that entry so qtea resolves fresh on the next run."
            ),
            "traceback": None,
            "tc_refs": [],
            "attachments": [],
            "first_seen": record.get("ts"),
            "constant_name": const,
            "matched_constant": matched,
            "intent": intent,
            "page_url": record.get("page_url"),
            "stale_selector": stale,
            "pool_score": record.get("pool_score"),
        })
    return out


def _bug_candidates_for_unresolvable_tbds(
    remaining: list[dict], dev_locators_path: Path | None = None,
) -> list[dict]:
    """Emit a ``locator-unresolvable`` bug-candidate per HITL-unanswered
    TBD. Step 9's classifier sees these alongside test failures.
    """
    now = datetime.now(UTC).isoformat()
    out: list[dict] = []
    for entry in remaining:
        const = entry.get("constant_name") or "unknown"
        intent = entry.get("intent") or ""
        locators_hint = (
            f"Provide a selector via {str(dev_locators_path)!r}"
            if dev_locators_path
            else "Provide a selector via --dev-locators or QTEA_DEV_LOCATORS"
        )
        out.append({
            "id": f"BC-locator-unresolvable-{const}",
            "test_id": f"locator-unresolvable:{const}",
            "title": f"Locator could not be resolved: {const}",
            "file": entry.get("test_file"),
            "status": "error",
            "kind": "locator-unresolvable",
            "message": (
                f"The JIT runtime could not find any element matching "
                f"intent {intent!r} on {entry.get('page_url') or '(unknown URL)'}. "
                f"{locators_hint} under "
                f"key {const!r}, or update the test to remove the TBD."
            ),
            "traceback": None,
            "tc_refs": [],
            "attachments": [],
            "first_seen": now,
            "constant_name": const,
            "intent": intent,
            "page_url": entry.get("page_url"),
        })
    return out


__all__ = [
    "_bug_candidates_for_dev_pool_drift",
    "_bug_candidates_for_unresolvable_tbds",
]
