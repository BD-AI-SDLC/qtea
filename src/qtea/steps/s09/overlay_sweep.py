"""End-of-attempt overlay-event sweep + HITL persistence.

Reads ``<workspace>/overlay-events.jsonl`` (written by the runtime when an
overlay intercepted a click and the safe-class heuristic couldn't handle
it), dedups by (role, name, page_url), filters out entries already present
in ``<sut>/.qtea/interceptors.json``, prompts the operator via the shared
HITL channel, and persists PERSIST-tier answers back to
``interceptors.json`` so the next run's runtime picks them up via
``page.add_locator_handler`` and the overlays become invisible.

Best-effort throughout — any exception in the sweep logs and returns
empties. Overlay handling is a quality improvement, not a correctness
gate; a broken sweep must not fail Step 9.
"""

from __future__ import annotations

from pathlib import Path

from qtea.hitl import (
    RESOLUTION_OVERLAY_BUG,
    RESOLUTION_OVERLAY_PERSIST,
    Question,
    prompt_user,
)
from qtea.logging_setup import get_logger
from qtea.overlay_handling import (
    OverlayEvent,
    append_interceptor,
    build_overlay_question_metadata,
    dedup_overlay_events,
    delete_screenshot,
    filter_already_registered,
    load_interceptors,
    load_overlay_events,
    parse_overlay_answer,
)

log = get_logger(__name__)


def _overlay_events_path(workspace_root: Path) -> Path:
    return workspace_root / "overlay-events.jsonl"


def _interceptors_path(sut_root: Path) -> Path:
    """Where <sut>/.qtea/interceptors.json lives.

    Per-SUT, checked into the user's repo so team members share the
    dismissal registry. Runtime reads via QTEA_INTERCEPTORS env var
    (Step 9 sets it) with this path as the convention default.
    """
    return sut_root / ".qtea" / "interceptors.json"


def _hitl_overlay_sweep(
    workspace_root: Path,
    sut_root: Path,
    *,
    no_hitl: bool,
) -> tuple[list[OverlayEvent], set[tuple[str, str]]]:
    """End-of-attempt sweep: surface unhandled overlays to the operator.

    Reads ``<workspace>/overlay-events.jsonl`` (written by the runtime
    when an overlay intercepted a click and the safe-class heuristic
    couldn't handle it), dedups by (role, name, page_url), filters out
    entries already present in ``interceptors.json``, prompts the user via
    the shared HITL channel, and persists PERSIST-tier answers back to
    ``interceptors.json`` so subsequent runs are clean.

    Returns ``(all_deduped_events, persisted_keys)``. The caller uses the
    events list + persisted-keys set to reclassify Step 9 bug candidates
    (see Layer 4 in the design). ``persisted_keys`` uses
    ``(overlay_role, overlay_name)`` — matching :class:`Interceptor.dedup_key`.

    Non-blocking on failures: any exception in the sweep logs and returns
    empties. Overlay handling is a best-effort quality improvement, not a
    correctness gate.
    """
    events_path = _overlay_events_path(workspace_root)
    events = load_overlay_events(events_path)
    if not events:
        return [], set()
    interceptors_path = _interceptors_path(sut_root)
    existing = load_interceptors(interceptors_path)
    deduped = dedup_overlay_events(events)
    unhandled = filter_already_registered(deduped, existing)
    if not unhandled:
        log.info(
            "step09.overlay_sweep_all_registered",
            total_events=len(events), unique=len(deduped),
        )
        return deduped, set()

    log.info(
        "step09.overlay_sweep_pending",
        total_events=len(events), unique=len(deduped),
        unhandled=len(unhandled),
    )

    if no_hitl:
        # CI / --no-hitl: don't prompt, don't persist. The reclassifier will
        # still mark matching bug candidates as overlay_pending_hitl so
        # Step 10 doesn't misfile them as bugs.
        log.info("step09.overlay_sweep_no_hitl_skipped", count=len(unhandled))
        return deduped, set()

    questions: list[Question] = []
    ev_by_qid: dict[str, OverlayEvent] = {}
    for idx, ev in enumerate(unhandled, start=1):
        qid = f"OVR-{idx:02d}"
        meta = build_overlay_question_metadata(ev)
        questions.append(
            Question(
                id=qid,
                kind="overlay_dismiss",
                prompt_text=(
                    f"Overlay {ev.overlay_role!r} '{ev.overlay_name}' "
                    f"blocked action on {ev.test_id}"
                ),
                context=ev.page_url,
                metadata=meta,
            )
        )
        ev_by_qid[qid] = ev

    try:
        answers = prompt_user(questions, agent_label="overlay-dismiss")
    except Exception as e:
        log.warning("step09.overlay_sweep_hitl_failed", error=str(e))
        return deduped, set()

    persisted: set[tuple[str, str]] = set()
    for qid, ev in ev_by_qid.items():
        raw = answers.get(qid)
        if raw is None:
            # Skipped — leave events intact; reclassifier marks pending.
            continue
        resolution, answer_json = raw
        if resolution == RESOLUTION_OVERLAY_BUG:
            log.info(
                "step09.overlay_marked_as_bug",
                overlay_role=ev.overlay_role,
                overlay_name=ev.overlay_name,
            )
            # Don't persist — the reclassifier keeps it as overlay_pending
            # (not overlay_handled_next_run) since no interceptor exists.
            delete_screenshot(ev.screenshot_path)
            continue
        # Both PERSIST and ONCE build the same Interceptor object; only
        # PERSIST writes it to disk.
        entry = parse_overlay_answer(
            build_overlay_question_metadata(ev),
            resolution,
            answer_json,
        )
        if entry is None:
            log.warning(
                "step09.overlay_answer_unparseable",
                qid=qid, resolution=resolution,
            )
            continue
        if resolution == RESOLUTION_OVERLAY_PERSIST:
            try:
                append_interceptor(interceptors_path, entry)
                persisted.add(entry.dedup_key())
                log.info(
                    "step09.overlay_persisted",
                    overlay_role=entry.overlay_role,
                    overlay_name=entry.overlay_name,
                    dismiss_kind=entry.dismiss_kind,
                    path=str(interceptors_path),
                )
            except (OSError, ValueError) as e:
                log.warning(
                    "step09.overlay_persist_failed",
                    qid=qid, error=str(e),
                )
        else:  # RESOLUTION_OVERLAY_ONCE
            log.info(
                "step09.overlay_one_shot",
                overlay_role=entry.overlay_role,
                overlay_name=entry.overlay_name,
            )
        # Screenshot has served its purpose — delete to bound PII exposure
        # (CLAUDE.md rule: no PII in workspace artifacts beyond the run).
        delete_screenshot(ev.screenshot_path)

    return deduped, persisted


__all__ = [
    "_hitl_overlay_sweep",
    "_interceptors_path",
    "_overlay_events_path",
]
