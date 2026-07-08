"""Overlay/popup auto-dismiss — parent-side foundation.

Complements the runtime-side detection + heuristic dismiss (inline in
``src/qtea/_resources/runtime/qtea_runtime.py.tpl``) with:

- Dataclasses that mirror the JSONL event schema the runtime writes and the
  ``interceptors.json`` schema shared across both sides.
- Loaders + writers for ``<workspace>/overlay-events.jsonl`` and
  ``<sut>/.qtea/interceptors.json``.
- The heuristic scorer's parent-side twin (used to extract HITL candidate
  buttons from an AOM subtree captured by the runtime).
- ``storage_state`` cookie filter (Layer 6) so consent cookies dismissed by
  our locator handlers don't leak into the persisted state and mask future
  regressions of the consent flow.
- Dedup + bug-candidate reclassification helpers used by Step 9's
  end-of-attempt HITL sweep.

Import-safe from any code running in the parent qtea process. NOT imported by
the runtime template — the runtime is vendored into the SUT subprocess and
must remain self-contained (no qtea imports). To stay in sync, the runtime
mirrors the FIELD NAMES defined here.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — mirrored in the runtime template's inline heuristic.
# ---------------------------------------------------------------------------

# Dismiss-SAFE tokens: the heuristic MAY auto-fire on candidates whose
# accessible name matches any of these. Chosen so that clicking would
# essentially never mask a real bug — pure dismissal semantics, not consent
# acceptance or state mutation.
DISMISS_SAFE_TOKENS: frozenset[str] = frozenset({
    "close", "dismiss", "not now", "skip", "later", "maybe later",
    "remind me later", "×", "✕", "x",
})

# Dismiss-RISKY tokens: the heuristic MUST NOT auto-fire on these. They
# reach HITL as candidates the operator can pick, but never zero-touch.
# Rationale: "accept" could be "accept terms" (consent, ok) OR "accept
# payment" (destructive). "Continue" could be "continue to checkout"
# (destructive). Two-token-class separation is the primary safety guardrail.
DISMISS_RISKY_TOKENS: frozenset[str] = frozenset({
    "accept", "agree", "continue", "ok", "got it", "understand",
    "confirm", "proceed",
})

# Cookie name / domain fragments that indicate consent/GDPR/tracking state.
# Layer 6 storage-state filter strips these before persistence so the
# consent flow's own regression tests still see the fresh overlay next run.
CONSENT_COOKIE_PATTERNS: tuple[str, ...] = (
    "consent", "cookie", "gdpr", "banner",
    "onetrust", "trustarc", "cookiebot", "osano",
)

# Interceptors.json envelope version we write today. Loaders that see a
# different version log a warning and skip the file rather than misreading.
INTERCEPTORS_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Dataclasses — mirror runtime-side JSONL writes and schema entries.
# ---------------------------------------------------------------------------


@dataclass
class OverlayEvent:
    """One overlay-intercept detection recorded by the runtime.

    Matches the JSONL line format the runtime writes to
    ``<workspace>/overlay-events.jsonl``. Field names are the wire format;
    parent-side reads MUST use the same names.
    """
    ts: str
    test_id: str
    target_intent: str
    overlay_role: str
    overlay_name: str
    page_url: str
    screenshot_path: str = ""
    overlay_frame: str = "top"
    overlay_bbox: tuple[float, float, float, float] | None = None
    heuristic_attempted: bool = False
    heuristic_succeeded: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # ``candidates`` = AOM-extracted button dicts the runtime snapshotted at
    # detection time. Each: {role, name, safe, score, bbox}. Parent uses
    # them to build HITL candidate list without needing to re-open the browser.

    def dedup_key(self) -> tuple[str, str, str]:
        return (self.overlay_role, self.overlay_name, self.page_url)


@dataclass
class DismissCandidate:
    """A button the heuristic identified inside an overlay's AOM subtree."""
    role: str
    name: str
    safe: bool  # True → dismiss-safe class; False → dismiss-risky
    score: int
    bbox: tuple[float, float, float, float] | None = None

    def to_target_dict(self) -> dict[str, str]:
        """Shape suitable for interceptors.json ``dismiss.target``."""
        return {"kind": "role", "role": self.role, "name": self.name, "name_op": "equals"}


@dataclass
class Interceptor:
    """One entry in ``interceptors.json``. Parallels the JSON schema."""
    overlay_role: str
    overlay_name: str
    dismiss_kind: str  # "click" | "press_escape"
    dismiss_target: dict[str, str] | None = None  # required when dismiss_kind == "click"
    overlay_frame: str = "top"
    overlay_name_op: str = "equals"
    handler_times: int = 100
    handler_no_wait_after: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        overlay = {
            "kind": "role",
            "role": self.overlay_role,
            "name": self.overlay_name,
            "name_op": self.overlay_name_op,
        }
        if self.overlay_frame and self.overlay_frame != "top":
            overlay["frame"] = self.overlay_frame
        dismiss: dict[str, Any] = {"kind": self.dismiss_kind}
        if self.dismiss_kind == "click":
            dismiss["target"] = self.dismiss_target or {}
        entry: dict[str, Any] = {
            "overlay": overlay,
            "dismiss": dismiss,
            "handler_config": {
                "times": self.handler_times,
                "no_wait_after": self.handler_no_wait_after,
            },
        }
        if self.metadata:
            entry["metadata"] = dict(self.metadata)
        return entry

    @classmethod
    def from_json(cls, entry: dict[str, Any]) -> Interceptor:
        overlay = entry.get("overlay") or {}
        dismiss = entry.get("dismiss") or {}
        hc = entry.get("handler_config") or {}
        return cls(
            overlay_role=str(overlay.get("role") or ""),
            overlay_name=str(overlay.get("name") or ""),
            overlay_name_op=str(overlay.get("name_op") or "equals"),
            overlay_frame=str(overlay.get("frame") or "top"),
            dismiss_kind=str(dismiss.get("kind") or "click"),
            dismiss_target=dict(dismiss.get("target") or {}) or None,
            handler_times=int(hc.get("times") or 100),
            handler_no_wait_after=bool(hc.get("no_wait_after", True)),
            metadata=dict(entry.get("metadata") or {}),
        )

    def dedup_key(self) -> tuple[str, str]:
        return (self.overlay_role, self.overlay_name)


# ---------------------------------------------------------------------------
# File I/O — overlay-events.jsonl (runtime → parent) and interceptors.json
# (parent → sut, consumed by both sides).
# ---------------------------------------------------------------------------

# Whitelist enforced by the loader. Kept narrow on purpose — see schema
# doc for the supply-chain rationale.
_ALLOWED_DISMISS_KINDS: frozenset[str] = frozenset({"click", "press_escape"})


def load_overlay_events(path: Path) -> list[OverlayEvent]:
    """Read overlay events written by the runtime. Missing/empty file → []."""
    if not path.exists():
        return []
    out: list[OverlayEvent] = []
    try:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                log.warning("overlay.event_line_corrupt line=%d error=%s", lineno, e)
                continue
            try:
                bbox_raw = obj.get("overlay_bbox")
                bbox = tuple(bbox_raw) if bbox_raw and len(bbox_raw) == 4 else None
                out.append(
                    OverlayEvent(
                        ts=str(obj.get("ts") or ""),
                        test_id=str(obj.get("test_id") or ""),
                        target_intent=str(obj.get("target_intent") or ""),
                        overlay_role=str(obj.get("overlay_role") or ""),
                        overlay_name=str(obj.get("overlay_name") or ""),
                        page_url=str(obj.get("page_url") or ""),
                        screenshot_path=str(obj.get("screenshot_path") or ""),
                        overlay_frame=str(obj.get("overlay_frame") or "top"),
                        overlay_bbox=bbox,  # type: ignore[arg-type]
                        heuristic_attempted=bool(obj.get("heuristic_attempted", False)),
                        heuristic_succeeded=bool(obj.get("heuristic_succeeded", False)),
                        candidates=list(obj.get("candidates") or []),
                    )
                )
            except (TypeError, ValueError) as e:
                log.warning("overlay.event_shape_invalid line=%d error=%s", lineno, e)
    except OSError as e:
        log.warning("overlay.events_read_failed path=%s error=%s", path, e)
    return out


def load_interceptors(path: Path) -> list[Interceptor]:
    """Read interceptors.json. Missing / wrong-version / malformed → [] with warnings.

    Each entry is validated against the whitelist (`dismiss.kind` in
    {click, press_escape}). Entries that fail validation are logged and
    dropped; the rest of the file survives — one bad entry doesn't disable
    the whole registry.
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("overlay.interceptors_read_failed path=%s error=%s", path, e)
        return []

    version = raw.get("schema_version")
    if version != INTERCEPTORS_SCHEMA_VERSION:
        log.warning(
            "overlay.interceptors_wrong_version path=%s got=%r want=%d — skipping file",
            path, version, INTERCEPTORS_SCHEMA_VERSION,
        )
        return []

    entries_raw = raw.get("entries") or []
    if not isinstance(entries_raw, list):
        log.warning("overlay.interceptors_entries_not_list path=%s", path)
        return []

    out: list[Interceptor] = []
    for idx, entry in enumerate(entries_raw):
        if not isinstance(entry, dict):
            log.warning("overlay.interceptor_entry_not_dict path=%s idx=%d", path, idx)
            continue
        dismiss = entry.get("dismiss") or {}
        dismiss_kind = dismiss.get("kind")
        if dismiss_kind not in _ALLOWED_DISMISS_KINDS:
            # Supply-chain guardrail — a PR trying to smuggle an
            # `evaluate`/`fill`/`goto` dismiss action gets rejected here.
            log.warning(
                "overlay.interceptor_dismiss_kind_forbidden path=%s idx=%d kind=%r",
                path, idx, dismiss_kind,
            )
            continue
        if dismiss_kind == "click":
            target = dismiss.get("target") or {}
            if not isinstance(target, dict) or not target.get("name") or not target.get("role"):
                log.warning(
                    "overlay.interceptor_click_missing_target path=%s idx=%d",
                    path, idx,
                )
                continue
        overlay = entry.get("overlay") or {}
        if not overlay.get("role") or not overlay.get("name"):
            log.warning(
                "overlay.interceptor_overlay_missing_role_or_name path=%s idx=%d",
                path, idx,
            )
            continue
        try:
            out.append(Interceptor.from_json(entry))
        except (TypeError, ValueError) as e:
            log.warning("overlay.interceptor_parse_failed path=%s idx=%d error=%s",
                        path, idx, e)
    return out


def write_interceptors(path: Path, entries: Iterable[Interceptor]) -> None:
    """Serialize + validate + write. Never writes an invalid file.

    Uses the schema validator when qtea is on the path; falls back to the
    local whitelist check only. Atomic-write to avoid a torn file when a
    concurrent runtime is reading.
    """
    entries_list = list(entries)
    envelope = {
        "schema_version": INTERCEPTORS_SCHEMA_VERSION,
        "entries": [e.to_json() for e in entries_list],
    }
    # Optional schema-level validation.
    try:
        from qtea.schemas import validate as _validate
        _validate(envelope, "interceptors")
    except (ImportError, FileNotFoundError):
        pass
    except Exception as e:
        raise ValueError(f"interceptors.json failed schema validation: {e}") from e

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def append_interceptor(path: Path, entry: Interceptor) -> None:
    """Merge one new entry into interceptors.json (dedup by role+name).

    Existing entry with the same key is replaced (last-writer-wins) — the
    operator's newer HITL decision is authoritative.
    """
    existing = load_interceptors(path)
    key = entry.dedup_key()
    merged = [e for e in existing if e.dedup_key() != key]
    merged.append(entry)
    write_interceptors(path, merged)


# ---------------------------------------------------------------------------
# Heuristic scoring — parent-side twin of the runtime's inline logic.
# ---------------------------------------------------------------------------

_NAME_TOKEN_SPLIT = re.compile(r"[\s\-_/,.:;·|()\[\]{}]+")


def _tokenize_name(name: str) -> list[str]:
    """Split accessible name into lowercase tokens for token-class matching."""
    if not name:
        return []
    return [t for t in _NAME_TOKEN_SPLIT.split(name.strip().lower()) if t]


def _matches_token_set(name: str, token_set: frozenset[str]) -> bool:
    """True when the accessible name contains any token from the set.

    Handles multi-word tokens (e.g. "not now") by substring-checking the
    full name after normalization. Single-word tokens use token-set
    membership so "close" matches "Close ×" without matching "closest".
    """
    if not name:
        return False
    norm = name.strip().lower()
    tokens = set(_tokenize_name(name))
    for phrase in token_set:
        if " " in phrase:
            if phrase in norm:
                return True
        elif phrase in tokens:
            return True
    return False


def classify_dismiss_name(name: str) -> str:
    """Return "safe" / "risky" / "unknown" for a candidate button's name.

    "safe" = auto-fire allowed. "risky" = HITL-only. "unknown" = neither
    class matched; still eligible for HITL as a low-priority candidate.
    """
    if _matches_token_set(name, DISMISS_SAFE_TOKENS):
        return "safe"
    if _matches_token_set(name, DISMISS_RISKY_TOKENS):
        return "risky"
    return "unknown"


def _score_candidate(
    role: str, name: str, bbox: tuple[float, float, float, float] | None,
    overlay_bbox: tuple[float, float, float, float] | None,
) -> tuple[int, bool]:
    """Score one candidate. Returns (score, is_safe).

    Scoring (mirrored in the runtime):
      +3 : name in safe or risky token class (either matters)
      +1 : role is button
      +2 : bbox lies in the top-right corner of the overlay's bbox
    Missing bbox loses only the +2 (candidate still ranked, just lower).
    """
    score = 0
    classification = classify_dismiss_name(name)
    if classification in ("safe", "risky"):
        score += 3
    if role.lower() == "button":
        score += 1
    if bbox is not None and overlay_bbox is not None:
        # Top-right quadrant of overlay.
        cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        ov_right = overlay_bbox[0] + overlay_bbox[2]
        ov_top_third = overlay_bbox[1] + overlay_bbox[3] / 3
        if cx > overlay_bbox[0] + overlay_bbox[2] / 2 and cy < ov_top_third \
                and cx <= ov_right:
            score += 2
    return score, classification == "safe"


def score_candidates(
    aom_subtree: list[dict[str, Any]],
    overlay_bbox: tuple[float, float, float, float] | None = None,
) -> list[DismissCandidate]:
    """Walk an AOM subtree, return scored dismiss candidates sorted best-first.

    ``aom_subtree`` is a flat list of node dicts each containing at least
    ``role``, ``name``, and optionally ``bbox`` (as [x, y, w, h]). Non-button
    nodes with no dismiss-classified name are omitted.

    The runtime and parent both use this shape so a candidate list captured
    by the runtime (into ``OverlayEvent.candidates``) can be re-ranked
    parent-side without needing to re-open the browser.
    """
    out: list[DismissCandidate] = []
    for node in aom_subtree or []:
        role = str(node.get("role") or "").strip()
        name = str(node.get("name") or "").strip()
        bbox_raw = node.get("bbox")
        bbox: tuple[float, float, float, float] | None = (
            tuple(bbox_raw) if bbox_raw and len(bbox_raw) == 4 else None  # type: ignore[assignment]
        )
        classification = classify_dismiss_name(name)
        # Keep only nodes that have SOME dismiss signal — either the role is
        # button/link/menuitem OR the name matches a dismiss token. Drops
        # random text nodes and headings that inflate the candidate list.
        role_ok = role.lower() in ("button", "link", "menuitem")
        if not role_ok and classification == "unknown":
            continue
        score, is_safe = _score_candidate(role, name, bbox, overlay_bbox)
        if score == 0:
            continue
        out.append(DismissCandidate(role=role, name=name, safe=is_safe, score=score, bbox=bbox))
    out.sort(key=lambda c: (-c.score, not c.safe, c.name.lower()))
    return out


def pick_safe_candidate(candidates: list[DismissCandidate]) -> DismissCandidate | None:
    """Return the highest-scoring SAFE candidate, or None if only risky/unknown.

    This is the "auto-fire allowed" decision — a None return means the
    heuristic bows out and the failure propagates for HITL to handle.
    """
    for c in candidates:
        if c.safe:
            return c
    return None


# ---------------------------------------------------------------------------
# Dedup — collapse repeat overlay encounters into one HITL prompt.
# ---------------------------------------------------------------------------


def dedup_overlay_events(events: Iterable[OverlayEvent]) -> list[OverlayEvent]:
    """Collapse events by (role, name, page_url). Newest wins on ties.

    Rationale: a cookie banner that intercepts 12 tests in one Step 9
    attempt should produce ONE HITL prompt, not 12. The other 11 test
    failures will resolve once the interceptor is registered.
    """
    latest: dict[tuple[str, str, str], OverlayEvent] = {}
    for ev in events:
        key = ev.dedup_key()
        prior = latest.get(key)
        if prior is None or (ev.ts and prior.ts and ev.ts > prior.ts):
            latest[key] = ev
    return list(latest.values())


def filter_already_registered(
    events: Iterable[OverlayEvent], registered: Iterable[Interceptor],
) -> list[OverlayEvent]:
    """Drop events whose (role, name) already have a persisted interceptor.

    Prevents re-prompting the operator when the runtime's handler was too
    slow (or the overlay appeared before the handler was registered on a
    freshly-opened page).
    """
    seen_keys = {i.dedup_key() for i in registered}
    return [ev for ev in events if (ev.overlay_role, ev.overlay_name) not in seen_keys]


# ---------------------------------------------------------------------------
# Storage-state cookie filter (Layer 6).
# ---------------------------------------------------------------------------


def filter_consent_cookies(state: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Strip consent/GDPR cookies from a Playwright ``storageState`` dict.

    Returns ``(filtered_state, removed_count)``. Non-destructive — mutates
    a shallow copy of the input so callers can compare before/after.

    Match logic: a cookie is stripped if any :data:`CONSENT_COOKIE_PATTERNS`
    fragment appears (case-insensitive) in EITHER its ``name`` OR its
    ``domain``. The domain check catches OneTrust/TrustArc widgets that
    set generically-named cookies (``consent`` → ``visitor_id``) on their
    own subdomain.
    """
    filtered = dict(state)
    cookies = list(filtered.get("cookies") or [])
    if not cookies:
        return filtered, 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for c in cookies:
        name = str(c.get("name") or "").lower()
        domain = str(c.get("domain") or "").lower()
        combined = f"{name} {domain}"
        if any(pat in combined for pat in CONSENT_COOKIE_PATTERNS):
            removed += 1
            continue
        kept.append(c)
    filtered["cookies"] = kept
    return filtered, removed


# ---------------------------------------------------------------------------
# Bug-candidate reclassification (Layer 4).
# ---------------------------------------------------------------------------


def _event_matches_bug_candidate(ev: OverlayEvent, bc: dict[str, Any]) -> bool:
    """Heuristic: same test id AND either same target intent or similar failure.

    Conservative — a bug candidate with a different test id is NEVER
    reclassified, even if the overlay could theoretically have caused it.
    """
    bc_test = str(bc.get("test_id") or bc.get("test") or "")
    return not (not bc_test or bc_test != ev.test_id)


def reclassify_bug_candidates(
    bug_candidates: list[dict[str, Any]],
    events: Iterable[OverlayEvent],
    *,
    persisted_after_hitl: bool = False,
) -> list[dict[str, Any]]:
    """Rewrite `_type` on bug candidates matching an overlay event.

    Without this, every first-encounter popup becomes a false Step 10 bug.
    A matched candidate is reclassified to:
      - ``overlay_handled_next_run`` if HITL resolved and persisted an
        interceptor for this class (next run will be clean)
      - ``overlay_pending_hitl`` otherwise (the overlay was seen but no
        interceptor exists yet — reviewer should run the HITL sweep)

    Non-matching bug candidates pass through unchanged.
    """
    events_list = list(events)
    if not events_list:
        return bug_candidates
    new_type = "overlay_handled_next_run" if persisted_after_hitl else "overlay_pending_hitl"
    out: list[dict[str, Any]] = []
    for bc in bug_candidates:
        matched: OverlayEvent | None = None
        for ev in events_list:
            if _event_matches_bug_candidate(ev, bc):
                matched = ev
                break
        if matched is None:
            out.append(bc)
            continue
        reclassified = dict(bc)
        reclassified["_type"] = new_type
        reclassified["overlay_role"] = matched.overlay_role
        reclassified["overlay_name"] = matched.overlay_name
        reclassified["overlay_page_url"] = matched.page_url
        out.append(reclassified)
    return out


# ---------------------------------------------------------------------------
# HITL question construction — package OverlayEvent + candidates as metadata
# on a hitl.Question so the CLI + UI dialogs can render appropriately.
# ---------------------------------------------------------------------------

# Resolution constants distinct from hitl.RESOLUTION_ANSWERED. The parent
# handler in s09_execute.py branches on these to decide whether to persist,
# apply once, or record a suppression.
RESOLUTION_OVERLAY_PERSIST = "overlay_persist"     # apply + write to interceptors.json
RESOLUTION_OVERLAY_ONCE = "overlay_once"           # apply for this session only
RESOLUTION_OVERLAY_BUG = "overlay_bug"             # this is a real bug — fail the test


def build_overlay_question_metadata(ev: OverlayEvent) -> dict[str, Any]:
    """Package an OverlayEvent as ``Question.metadata`` for HITL rendering.

    The metadata dict travels through ``prompt_user`` untouched — CLI and
    UI dialogs read the same keys. Kept as plain JSON-serializable dicts
    so it can also serialize into the ledger.
    """
    candidates = score_candidates(ev.candidates, ev.overlay_bbox)
    return {
        "type": "overlay_dismiss",
        "test_id": ev.test_id,
        "target_intent": ev.target_intent,
        "overlay_role": ev.overlay_role,
        "overlay_name": ev.overlay_name,
        "overlay_frame": ev.overlay_frame,
        "page_url": ev.page_url,
        "screenshot_path": ev.screenshot_path,
        "candidates": [
            {
                "role": c.role,
                "name": c.name,
                "safe": c.safe,
                "score": c.score,
            }
            for c in candidates
        ],
        # Include "press_escape" and "custom" and "bug" as pseudo-candidates
        # the dialog will render alongside AOM-extracted buttons.
        "extra_options": [
            {"kind": "press_escape", "label": "Press Escape key"},
            {"kind": "custom", "label": "Custom locator (advanced)"},
            {"kind": "bug", "label": "This is a real bug — fail the test"},
        ],
    }


def parse_overlay_answer(
    metadata: dict[str, Any], resolution: str, answer: str,
) -> Interceptor | None:
    """Turn a HITL response into an Interceptor entry (or None if not persistable).

    ``resolution`` is one of ``RESOLUTION_OVERLAY_PERSIST`` /
    ``RESOLUTION_OVERLAY_ONCE`` / ``RESOLUTION_OVERLAY_BUG``.

    ``answer`` encodes the operator's choice. Wire format is a JSON object:
    ``{"kind": "click_candidate", "candidate_index": 0}`` OR
    ``{"kind": "press_escape"}`` OR
    ``{"kind": "custom", "role": "button", "name": "..."}`` OR
    ``{"kind": "bug"}``.

    Returns an ``Interceptor`` when the response translates to a
    registerable pattern (kind in {click_candidate, press_escape, custom}).
    Returns ``None`` for ``kind == "bug"`` or unrecognized shapes.
    """
    if resolution == RESOLUTION_OVERLAY_BUG:
        return None
    try:
        parsed = json.loads(answer) if answer else {}
    except json.JSONDecodeError:
        log.warning("overlay.answer_not_json raw=%r", answer)
        return None
    if not isinstance(parsed, dict):
        return None
    kind = parsed.get("kind")
    if kind == "bug":
        return None

    interceptor_kwargs: dict[str, Any] = {
        "overlay_role": str(metadata.get("overlay_role") or ""),
        "overlay_name": str(metadata.get("overlay_name") or ""),
        "overlay_frame": str(metadata.get("overlay_frame") or "top"),
        "metadata": {
            "created_by": "hitl",
            "first_seen_test": str(metadata.get("test_id") or ""),
            "first_seen_url": str(metadata.get("page_url") or ""),
        },
    }

    if kind == "press_escape":
        return Interceptor(dismiss_kind="press_escape", **interceptor_kwargs)

    if kind == "click_candidate":
        idx = parsed.get("candidate_index")
        candidates = metadata.get("candidates") or []
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            log.warning("overlay.answer_bad_candidate_index idx=%r", idx)
            return None
        c = candidates[idx]
        return Interceptor(
            dismiss_kind="click",
            dismiss_target={
                "kind": "role",
                "role": str(c.get("role") or "button"),
                "name": str(c.get("name") or ""),
                "name_op": "equals",
            },
            **interceptor_kwargs,
        )

    if kind == "custom":
        role = str(parsed.get("role") or "").strip() or "button"
        name = str(parsed.get("name") or "").strip()
        if not name:
            log.warning("overlay.answer_custom_missing_name")
            return None
        return Interceptor(
            dismiss_kind="click",
            dismiss_target={"kind": "role", "role": role, "name": name, "name_op": "equals"},
            **interceptor_kwargs,
        )

    log.warning("overlay.answer_unknown_kind kind=%r", kind)
    return None


# ---------------------------------------------------------------------------
# Screenshot cleanup — delete PII-bearing images after HITL resolves.
# ---------------------------------------------------------------------------


def delete_screenshot(path_str: str) -> bool:
    """Remove a screenshot from disk. Missing file → no-op, returns False."""
    if not path_str:
        return False
    p = Path(path_str)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError as e:
        log.warning("overlay.screenshot_delete_failed path=%s error=%s", p, e)
        return False


__all__ = [
    "CONSENT_COOKIE_PATTERNS",
    "DISMISS_RISKY_TOKENS",
    "DISMISS_SAFE_TOKENS",
    "INTERCEPTORS_SCHEMA_VERSION",
    "RESOLUTION_OVERLAY_BUG",
    "RESOLUTION_OVERLAY_ONCE",
    "RESOLUTION_OVERLAY_PERSIST",
    "DismissCandidate",
    "Interceptor",
    "OverlayEvent",
    "append_interceptor",
    "build_overlay_question_metadata",
    "classify_dismiss_name",
    "dedup_overlay_events",
    "delete_screenshot",
    "filter_already_registered",
    "filter_consent_cookies",
    "load_interceptors",
    "load_overlay_events",
    "parse_overlay_answer",
    "pick_safe_candidate",
    "score_candidates",
    "write_interceptors",
]
