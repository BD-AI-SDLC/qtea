"""JIT resolver HITL escalation for unresolvable TBD sentinels.

When the JIT resolver ladder (dev pool → cache → AOM heuristic → LLM)
gives up on a TBD, the runtime drops a ``hitl-pending-*.json`` file into
the JIT cache dir. This module reads those files, surfaces each one to
the operator on a TTY (skipped on non-TTY / UI / CI), rewrites their
answer into ``dev-locators.json`` so the next run's Tier 1 picks it up
without prompting again, and returns the residual list for the caller to
emit as ``locator-unresolvable`` bug-candidates.

UI-mode is treated as non-interactive here — the worker thread's stdin
isn't reachable from the Flet window. Routing this through the UI dialog
would require new HITL wiring; today the pending entries flow to bug
candidates so they stay visible in the final report.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from qtea.logging_setup import get_logger

log = get_logger(__name__)


def _collect_hitl_pending(jit_cache_dir: Path) -> list[dict]:
    """Read every ``hitl-pending-*.json`` file the JIT runtime dropped during
    test execution. Each file represents an unresolvable TBD that needs a
    human-in-the-loop selector OR a structured bug candidate.

    Returns the parsed dicts (one per TBD); files that fail to parse are
    skipped with a warning. The files are NOT deleted here — Phase 4's
    HITL prompt deletes the ones that get answered; the rest persist for
    the bug-candidates emission downstream.
    """
    if not jit_cache_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(jit_cache_dir.glob("hitl-pending-*.json")):
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("step09.hitl_pending_unreadable", path=str(p), error=str(e))
            continue
        entry["_pending_path"] = str(p)
        out.append(entry)
    return out


def _hitl_resolve_unresolvable(
    pendings: list[dict], *,
    dev_locators_path: Path | None,
    no_hitl: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Surface each unresolved TBD to the user on a TTY, write their
    answer to ``dev-locators.json`` (next run uses it as Tier 1), and
    delete the pending file. Non-TTY / ``no_hitl=True`` / UI runs leave
    every pending in place — caller emits them as structured bug candidates.

    UI mode is treated as non-interactive here because the worker thread's
    stdin is unreachable from the Flet window. Routing this prompt through
    the UI dialog would require new HITL wiring; for now we skip and log so
    the bug-candidate path picks up the unresolved entries (visible in the
    final report) instead of the worker hanging silently on ``input()``.

    Returns ``(resolved, remaining)`` — ``resolved`` were answered by the
    user, ``remaining`` are still unresolved and flow into bug-candidates.
    """
    import os
    import sys
    is_tty = sys.stdin is not None and sys.stdin.isatty()
    is_ui = bool(os.environ.get("QTEA_UI_MODE"))
    if is_ui or not is_tty or no_hitl or not pendings:
        if pendings and (is_ui or no_hitl):
            log.info(
                "step09.hitl_pending_skipped",
                reason="ui_mode" if is_ui else "no_hitl",
                count=len(pendings),
                constants=[p.get("constant_name") for p in pendings],
            )
        return [], pendings

    resolved: list[dict] = []
    remaining: list[dict] = []
    log.info("step09.hitl_pending_count", count=len(pendings))
    print(
        f"\n[qtea] {len(pendings)} locator(s) the JIT runtime could not "
        f"resolve. You can supply a selector for each, or press ENTER to skip "
        f"(skipped TBDs become bug-candidate entries for Step 9).\n",
        flush=True,
    )
    for entry in pendings:
        intent = entry.get("intent") or "(no intent)"
        constant = entry.get("constant_name") or "(unknown)"
        page_url = entry.get("page_url") or "(unknown)"
        print(f"  TBD: {constant} — {intent}")
        print(f"       page: {page_url}")
        try:
            answer = input("       selector (or ENTER to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer:
            remaining.append(entry)
            continue
        if answer.startswith("//") or answer.startswith("xpath=") or "By.XPATH" in answer:
            print("       [rejected] XPath selectors are forbidden by qtea.")
            remaining.append(entry)
            continue
        entry["_user_selector"] = answer
        resolved.append(entry)
        # Best-effort: also remove the pending file so next runs don't re-prompt.
        with contextlib.suppress(OSError):
            Path(entry.get("_pending_path", "")).unlink(missing_ok=True)

    if resolved and dev_locators_path is not None:
        _append_resolved_to_dev_locators(resolved, dev_locators_path)
    return resolved, remaining


def _append_resolved_to_dev_locators(
    resolved: list[dict], dev_locators_path: Path,
) -> None:
    """Merge HITL answers into dev-locators.json so the next run's
    Tier 1 picks them up without re-prompting. File schema:
    ``{"locators": {"CONST_NAME": {"selector": "...", "source": "hitl"}}}``."""
    try:
        if dev_locators_path.exists():
            raw = json.loads(dev_locators_path.read_text(encoding="utf-8"))
        else:
            raw = {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    locators = raw.get("locators")
    if not isinstance(locators, dict):
        locators = {}
        raw["locators"] = locators
    for entry in resolved:
        const = entry.get("constant_name")
        sel = entry.get("_user_selector")
        if not const or not sel:
            continue
        locators[const] = {
            "selector": sel,
            "source": "hitl",
            "intent": entry.get("intent"),
            "page_url": entry.get("page_url"),
        }
    try:
        dev_locators_path.parent.mkdir(parents=True, exist_ok=True)
        dev_locators_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        log.info(
            "step09.hitl_dev_locators_updated",
            path=str(dev_locators_path), added=len(resolved),
        )
    except OSError as e:
        log.warning("step09.hitl_dev_locators_write_failed", error=str(e))


__all__ = [
    "_append_resolved_to_dev_locators",
    "_collect_hitl_pending",
    "_hitl_resolve_unresolvable",
]
