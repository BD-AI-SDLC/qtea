"""Reusable helpers for line-anchored locator patching.

Extracted from the now-deprecated `steps/s08_locator_resolution.py` so
Step 9's on-failure heal flow (for non-JIT stacks: Selenium / Cypress /
Robot) can use the same line-anchored patcher, XPath guard, and
apply-rate gate without dragging in Step 8's full orchestration layer.

Pure functions — no I/O beyond the file mutations explicitly requested
via ``apply_patches``. No reference to ``StepContext`` or any step-level
state. Safe to import from any step.

Algorithm summary (apply_patches):

  1. Resolve each resolution's ``file`` to an on-disk path via the
     3-tier fallback in :func:`resolve_patch_target`.
  2. Group items by resolved file path.
  3. For each file:
       a. Read bytes (preserves CRLF/LF/BOM exactly).
       b. Split into lines keeping original line endings.
       c. Sort items: ``line``-less items first (legacy global path),
          then by ascending line number, ties broken by strategy
          priority (id > data-testid > role > label > text > placeholder > css).
       d. For each item: classify (XPath/no-op/token-presence), then
          line-anchored replace via :func:`apply_line_targeted` within
          ±``LINE_DRIFT_TOLERANCE`` lines. No global fallback.
       e. Write back bytes-as-bytes — universal-newlines mode never
          touches the line endings we preserved.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

# Locator strategies, ranked highest-priority first. Matches the
# discipline rule in CLAUDE.md / qa-orchestrator instructions.
PRIORITY: tuple[str, ...] = (
    "id", "data-testid", "role", "label", "text", "placeholder", "css",
)

# How many lines on either side of the agent's reported `line` we'll scan
# for the TBD token when the exact line doesn't contain it. Wider than the
# pre-anchor 3-line tolerance because the new anchor check
# (:func:`is_assignment_line`) rejects comment lines and decorative mentions
# — the only way a wrong line matches inside the window is if there are
# multiple legitimate assignments to the same TBD token in the file, which
# the codegen agent does not do.
LINE_DRIFT_TOLERANCE: int = 10

# Apply-rate gate threshold. Below this fraction of non-excused items
# successfully patched, callers should treat the run as failed (downstream
# tests would run against unpatched locators and produce noise).
MIN_APPLY_RATE: float = 0.9

# Matches a Python ``NAME = "..."`` / ``self.NAME = "..."`` assignment so we
# can recover the constant name surrounding a duplicated selector for
# diagnostics. Best-effort; if no match, callers should fall back to the
# agent's ``tbd`` field.
CONST_NAME_RE = re.compile(r"(?:self\.)?([A-Z][A-Z0-9_]*)\s*=")


def is_xpath_replacement(replacement: str) -> bool:
    """Reject XPath replacements regardless of agent claim.

    XPath bypasses Playwright's locator priority chain and is brittle
    against DOM restructure. Surfaces three common XPath shapes:

    - Playwright's ``xpath=...`` prefix
    - The raw XPath-axis shorthand ``//foo``
    - Selenium's ``By.XPATH`` enum reference
    """
    s = replacement.strip()
    if s.startswith("xpath="):
        return True
    if s.startswith("//"):
        return True
    return "By.XPATH" in s


def rank_strategy(strategy: str) -> int:
    """Return the priority index of ``strategy`` (lower = higher priority).

    Unknown strategies sort to the end (len(PRIORITY)), so a ``css``
    selector always wins over a strategy the resolver didn't recognise.
    """
    return PRIORITY.index(strategy) if strategy in PRIORITY else len(PRIORITY)


def resolve_patch_target(tests_dir: Path, file_rel: str) -> Path | None:
    """Best-effort lookup of the actual on-disk path for a resolution's ``file``.

    The agent's ``file`` field SHOULD be relative to ``tests_dir`` (no
    leading ``tests/``). In practice the agent sometimes prefixes
    ``tests/`` (because the SUT's test directory layout is ``tests/...``),
    or even uses absolute paths. Without a fallback, all such resolutions
    get ``applied=false`` and the user sees ``applied=0 skipped=N``
    despite the agent producing correct locators.

    Resolution order:

    1. ``tests_dir / file_rel`` — the expected case.
    2. Strip leading path components (``tests/``, ``tests/pages/``, ...)
       one at a time until a match is found.
    3. Match by basename anywhere under ``tests_dir`` — single hit wins,
       ambiguous matches return ``None`` so caller skips with a clear reason.

    Returns ``None`` when no on-disk target exists.
    """
    direct = tests_dir / file_rel
    if direct.exists():
        return direct

    parts = Path(file_rel).parts
    for i in range(1, len(parts)):
        candidate = tests_dir.joinpath(*parts[i:])
        if candidate.exists():
            return candidate

    basename = Path(file_rel).name
    matches = list(tests_dir.rglob(basename))
    if len(matches) == 1 and matches[0].is_file():
        return matches[0]
    return None


def is_assignment_line(line: str, tbd_token: str) -> bool:
    """True when ``line`` is a patchable host for ``tbd_token``.

    Patchable means the token is present AND its position on the line is
    not inside a single-line comment. The rule: the substring on ``line``
    before the token does not start with a single-line comment marker
    (``#``, ``//``, ``*``, ``/*``). False positives are bounded — comment
    lines that mention the literal token (e.g. the ``TBD_INTENT:``
    description comments the codegen agent attaches) are rejected
    outright, which is what protects against scrambled patches.

    Accepts (every realistic codegen shape):

    - ``LOGIN_BUTTON = TBD_LOCATOR``                  (assignment)
    - ``self.LOGIN_BUTTON = "TBD_LOCATOR"``           (Python attr)
    - ``const x = "TBD_LOCATOR";``                    (JS/TS)
    - ``await page.locator('TBD_LOCATOR').click();``  (inline call arg)
    - ``By.cssSelector("TBD_LOCATOR")``               (Java argument)

    Rejects:

    - ``# fallback to TBD_LOCATOR (TODO)``                — Python comment
    - ``// TBD_INTENT: TBD_LOCATOR is the login button`` — JS/TS comment
    - ``  *  TBD_LOCATOR``                                — JSDoc continuation
    """
    if tbd_token not in line:
        return False
    idx = line.find(tbd_token)
    prefix = line[:idx]
    stripped = prefix.lstrip()
    return not stripped.startswith(("#", "//", "*", "/*"))


def coerce_line(raw: object) -> int | None:
    """Coerce an agent-supplied ``line`` value to a positive 1-based int.

    The schema declares ``line: int|null`` but agents have been observed
    emitting ``"7"`` (string) and ``7.0`` (float). Returns ``None`` for
    any value that can't be safely interpreted as a positive integer.
    """
    if raw is None:
        return None
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def classify_item(item: dict, file_text: str) -> tuple[bool, dict | None]:
    """Run strategy/xpath/no-op checks that don't depend on line numbers.

    Returns ``(can_proceed, skip_annotation_or_None)``. When
    ``can_proceed`` is False, the caller MUST use the returned skip
    annotation and not attempt replacement. When True, the caller
    proceeds to line-targeted replacement.

    Order matters: XPath rejection runs BEFORE any line-validity check so
    a malicious/wrong agent response that includes both ``line: 999`` AND
    ``replacement: 'xpath=//x'`` skips with the security-critical reason
    (``xpath``), not the milder ``line out of bounds``.
    """
    replacement = item.get("replacement", "")
    strategy = item.get("strategy", "")
    tbd_token = item.get("tbd", "TBD_LOCATOR")

    if strategy not in PRIORITY:
        existing_reason = item.get("skip_reason")
        if strategy is None and existing_reason:
            return False, dict(item, applied=False, skip_reason=existing_reason)
        return False, dict(
            item, applied=False,
            skip_reason=f"unknown strategy: {strategy}",
        )
    if is_xpath_replacement(replacement):
        return False, dict(
            item, applied=False,
            skip_reason="xpath replacement rejected",
        )
    if replacement == tbd_token:
        return False, dict(
            item, applied=False,
            skip_reason="replacement is the TBD token itself (no-op)",
        )
    if tbd_token not in file_text:
        return False, dict(
            item, applied=False,
            skip_reason=f"token not found: {tbd_token!r}",
        )
    return True, None


def apply_line_targeted(
    lines: list[str], item: dict, line_no: int,
) -> tuple[list[str], dict, str | None]:
    """Anchor on ``CONST = TBD_LOCATOR``-style lines within the drift window.

    Returns ``(new_lines, annotated_item, applied_via)`` where
    ``applied_via`` is ``"line:N"`` on a direct hit, ``"line:N (anchor
    drift from M)"`` when drift-search succeeded against an assignment
    line, or ``None`` when no assignment-line candidate exists within
    tolerance.

    **The caller MUST treat ``None`` as a skip** (mark ``applied: false``)
    — no global text-replacement fallback, because that path was the
    source of mis-patches when the TBD token appeared in multiple places
    (comments + assignments) in the same file.

    Replaces exactly ONE token per call — preserves the
    one-item-one-occurrence contract that lets the agent emit two items
    for two TBDs on the same line and have both applied independently.

    Search order within the drift window:

    1. The exact agent-reported line, if it is an assignment line.
    2. Lines progressively further from the agent-reported line
       (±1, ±2, ..., ±:data:`LINE_DRIFT_TOLERANCE`), preferring the
       closest assignment-line match.
    """
    tbd_token = item.get("tbd", "TBD_LOCATOR")
    replacement = item.get("replacement", "")
    n = len(lines)

    if line_no < 1 or line_no > n:
        return lines, dict(
            item, applied=False,
            skip_reason=(
                f"line {line_no} out of bounds (file has {n} lines)"
            ),
        ), None

    target_idx = line_no - 1

    if is_assignment_line(lines[target_idx], tbd_token):
        lines[target_idx] = lines[target_idx].replace(tbd_token, replacement, 1)
        return lines, dict(item, applied=True, skip_reason=None), f"line:{line_no}"

    for offset in range(1, LINE_DRIFT_TOLERANCE + 1):
        for candidate in (target_idx - offset, target_idx + offset):
            if 0 <= candidate < n and is_assignment_line(lines[candidate], tbd_token):
                lines[candidate] = lines[candidate].replace(tbd_token, replacement, 1)
                actual = candidate + 1
                return lines, dict(item, applied=True, skip_reason=None), (
                    f"line:{actual} (anchor drift from {line_no})"
                )

    return lines, dict(
        item, applied=False,
        skip_reason=(
            f"no patchable `{tbd_token}` occurrence on line {line_no} or within "
            f"±{LINE_DRIFT_TOLERANCE} lines (comment lines are excluded); "
            "possible source drift"
        ),
    ), None


def split_with_endings(text: str) -> tuple[list[str], str]:
    """Split ``text`` preserving each line's original line ending.

    Returns ``(lines, bom)`` where ``bom`` is the leading UTF-8 BOM if
    present, ``''`` otherwise. Reassemble via ``bom + ''.join(lines)``.
    """
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom):]
    return body.splitlines(keepends=True), bom


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


def apply_patches(
    tests_dir: Path,
    resolutions: list[dict],
) -> list[dict]:
    """Mutate tests_dir files in place using line-targeted replacement.

    See module docstring for the full algorithm. Returns annotated items
    (one per input item) with ``applied`` and ``skip_reason``. Items
    receive an additional ``applied_via`` field indicating which path
    resolved them (audit aid).
    """
    out: list[dict] = []
    by_file: dict[Path, list[tuple[dict, dict]]] = {}
    for r in resolutions:
        file_rel = r.get("file")
        if not file_rel:
            continue
        target = resolve_patch_target(tests_dir, file_rel)
        for item in r.get("items", []):
            if target is None:
                annotated = dict(item, applied=False,
                                 skip_reason=f"file not found: {file_rel}")
                out.append(annotated)
                _record(r, annotated)
            else:
                by_file.setdefault(target, []).append((r, item))

    for path, entries in by_file.items():
        if not path.exists():
            for r, item in entries:
                annotated = dict(item, applied=False,
                                 skip_reason=f"file not found: {path}")
                out.append(annotated)
                _record(r, annotated)
            continue

        raw_bytes = path.read_bytes()
        had_bom = raw_bytes[:3] == b"\xef\xbb\xbf"
        body_bytes = raw_bytes[3:] if had_bom else raw_bytes
        raw_text = body_bytes.decode("utf-8")
        lines, _ = split_with_endings(raw_text)

        def _sort_key(e: tuple[dict, dict]) -> tuple[int, int, int]:
            ln = coerce_line(e[1].get("line"))
            line_key = ln if ln is not None else -1
            strat_key = rank_strategy(e[1].get("strategy", "css"))
            return (line_key, strat_key, id(e[1]))

        entries.sort(key=_sort_key)

        for r, item in entries:
            current_text = "".join(lines)
            can_proceed, skip_anno = classify_item(item, current_text)
            if not can_proceed:
                out.append(skip_anno)
                _record(r, skip_anno)
                continue

            line_no = coerce_line(item.get("line"))

            if line_no is None:
                replacement = item.get("replacement", "")
                tbd_token = item.get("tbd", "TBD_LOCATOR")
                new_text = current_text.replace(tbd_token, replacement, 1)
                lines, _ = split_with_endings(new_text)
                annotated = dict(item, applied=True, skip_reason=None,
                                 applied_via="global (no line)")
                out.append(annotated)
                _record(r, annotated)
                continue

            new_lines, annotated, applied_via = apply_line_targeted(
                lines, item, line_no,
            )
            if applied_via is None:
                annotated["applied_via"] = "none"
            else:
                lines = new_lines
                annotated["applied_via"] = applied_via

            out.append(annotated)
            _record(r, annotated)

        body_out = "".join(lines).encode("utf-8")
        prefix = b"\xef\xbb\xbf" if had_bom else b""
        path.write_bytes(prefix + body_out)

    return out


def constant_name_for(item: dict, tests_dir: Path | None) -> str:
    """Best-effort recovery of the constant name for an applied item.

    The agent emits the TBD token + line in resolution payloads but not
    the surrounding constant name (``LOCALE_SWITCHER``). For diagnostics
    it's much more useful to say "LOCALE_SWITCHER and LANGUAGE_DROP_DOWN
    share the same selector" than to repeat the TBD token twice. We pull
    the name off the patched line; if anything looks wrong we fall back
    to the ``tbd`` field so the report is still informative.
    """
    file_rel = item.get("file")
    line_no = item.get("line")
    if not file_rel or not isinstance(line_no, int) or tests_dir is None:
        return str(item.get("tbd") or "")
    target = resolve_patch_target(tests_dir, file_rel)
    if target is None or not target.exists():
        return str(item.get("tbd") or "")
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return str(item.get("tbd") or "")
    idx = line_no - 1
    if idx < 0 or idx >= len(lines):
        return str(item.get("tbd") or "")
    m = CONST_NAME_RE.search(lines[idx])
    return m.group(1) if m else str(item.get("tbd") or "")


def iter_applied_items(payload: dict) -> Iterator[tuple[dict, dict]]:
    """Yield ``(resolution, item)`` pairs for every applied item.

    Filters out skipped / non-applied items so consumers (duplicate
    detection, masking, telemetry) only see what actually landed in the
    generated code. Synthesises a ``file`` field on the item from the
    enclosing resolution so downstream helpers that don't have the
    surrounding ``resolution`` dict in scope can still locate the file.
    """
    for r in payload.get("resolutions") or []:
        file_rel = r.get("file")
        if not file_rel:
            continue
        for item in r.get("items") or []:
            if not item.get("applied"):
                continue
            yield r, dict(item, file=file_rel)


def compute_apply_rate(
    applied_items: list[dict],
    *,
    min_apply_rate: float = MIN_APPLY_RATE,
) -> dict:
    """Compute per-batch apply-rate stats from annotated items.

    Returns::

        {
            "applied": int,        # items with applied=True
            "skipped": int,        # items with applied=False
            "excused": int,        # items the auditor flagged ghost/duplicate
            "total": int,
            "denominator": int,    # total - excused
            "apply_rate": float,   # applied / denominator (1.0 if denom == 0)
            "passes_gate": bool,   # apply_rate >= min_apply_rate
            "min_apply_rate": float,
        }

    The 90% threshold was chosen to distinguish "a couple of edge-case
    locators missed" from "the agent couldn't navigate the page at all."
    Items the DOM-comparison auditor marked as ``ghost`` (element doesn't
    exist) or ``duplicate`` (same element as another TBD) are EXCUSED
    from the denominator — those aren't resolver failures, they're
    upstream codegen over-abstractions and spec/implementation gaps.
    """
    applied = sum(1 for it in applied_items if it.get("applied"))
    skipped = sum(1 for it in applied_items if not it.get("applied"))
    excused = sum(
        1 for it in applied_items
        if it.get("comparison_verdict") in ("ghost", "duplicate")
    )
    total = applied + skipped
    denominator = total - excused
    apply_rate = applied / denominator if denominator > 0 else 1.0
    passes_gate = denominator == 0 or apply_rate >= min_apply_rate
    return {
        "applied": applied,
        "skipped": skipped,
        "excused": excused,
        "total": total,
        "denominator": denominator,
        "apply_rate": apply_rate,
        "passes_gate": passes_gate,
        "min_apply_rate": min_apply_rate,
    }
