"""Step 8: Locator resolution via playwright-tester.

Reads tbd-index.json from Step 7 (which itself indexes the worca-generated
files in the SUT clone). For tests containing `TBD_LOCATOR` markers,
invokes the playwright-tester agent (which has access to the Playwright
MCP and can browse the live SUT at $SUT_BASE_URL) to discover real
locators. The agent writes `./locator-resolution.json` following the
schema. We then deterministically patch the test files IN PLACE inside
`<workspace>/sut/` on the worca-t branch, refuse any XPath replacement,
and re-index to confirm zero TBD markers remain.

Outputs (artifacts/step08/):
  - locator-resolution.json   (agent output + applied/skipped per item)
  - tbd-index.json            (re-indexed worca-only; expected `tbd_locators == 0`)

Patched test bytes live in `<workspace>/sut/` on the branch — review via
`git diff worca-t/run-<id>~1..worca-t/run-<id>` rather than reading a copy.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any

from worca_t._sut_git import _git, commit_step
from worca_t.claude_runner import run_agent
from worca_t.config import model_for_agent, package_resource_root, step_timeout
from worca_t.hitl import extract_questions, prompt_user
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid
from worca_t.steps.base import Step, StepContext, StepResult
from worca_t.test_indexer import IndexResult, index_tests, resolve_framework

log = get_logger(__name__)

# Locator strategies, ranked highest-priority first.
_PRIORITY = ("id", "data-testid", "role", "label", "text", "placeholder", "css")

# Items the resolver applied with confidence below this threshold AND whose
# selector is reused for another item in the same file are flagged as
# `low_confidence_masks` — the signature of a silently masked spec gap
# (e.g. "no real tooltip in DOM; returning the parent button" from the
# 20260601-212148 run). 0.6 leaves headroom above the 0.45 we saw in the
# wild while still catching anything clearly uncertain.
_LOW_CONFIDENCE_THRESHOLD = 0.6

# Matches a Python `NAME = "..."` / `self.NAME = "..."` assignment so we
# can recover the constant name surrounding a duplicated selector for the
# duplicates report. Best-effort; if no match, falls back to the agent's
# `tbd` field.
_CONST_NAME_RE = re.compile(r"(?:self\.)?([A-Z][A-Z0-9_]*)\s*=")

# Step 8 snapshot policy — AOM-first, raw-DOM only as a scoped fallback.
# The playwright-tester (8a) captures every distinct URL with `browser_snapshot`
# and persists the AOM tree to `page-snapshot-NN.json`. Raw-DOM fallbacks
# (`browser_evaluate(() => document.documentElement.outerHTML)`) land at
# `page-snapshot-NN-raw.html` and MUST be accompanied by a `fallback_reason`
# on the resolution entries that cited them. All snapshots persist in the
# step-08 workdir so the fixer-audit agent (8b) can read them. Names match
# the convention in agents/playwright-tester.agent.md.
_SNAPSHOT_GLOB = "page-snapshot-*"
_AOM_SNAPSHOT_RE = re.compile(r"^page-snapshot-\d+\.json$")
_RAW_SNAPSHOT_RE = re.compile(r"^page-snapshot-\d+-raw\.html$")
_DOM_COMPARISON_FILE = "dom-comparison.json"
# Sentinel token the 8b prompt opens with — keyed off in
# polyglot-test-fixer.agent.md to switch the agent into audit-only mode.
_AUDIT_MODE_TOKEN = "MODE: DOM-COMPARISON-AUDIT"
# Optional HITL artefact — the playwright-tester writes this when it sets
# `applied: false` for any TBD whose element could not be located. Absent
# in the happy path; presence triggers the orchestrator-side HITL splicer.
_CLARIFICATIONS_FILE = "clarifications.md"


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


def _load_url_audit(ctx: StepContext) -> dict:
    """Best-effort load of Step 6's url_resolution.json for the error trail."""
    p = ctx.workspace.step_dir(6) / "url_resolution.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tests_with_tbd(index: dict) -> list[dict]:
    """Return every TBD-bearing entry from `tests[]` AND `support_files[]`.

    The indexer separates test functions from support files (page objects,
    locators, helpers) so consumers can distinguish them. For locator
    resolution, both kinds carry TBD markers worth patching — flatten them
    into a single list with a synthetic `id` so downstream code (prompt
    builder, patcher) doesn't need to know about the split. Support entries
    don't have `id`; we synthesise one from the file's stem so the agent
    can reference it.
    """
    out: list[dict] = []
    for t in index.get("tests") or []:
        if t.get("tbd_markers"):
            out.append(t)
    for s in index.get("support_files") or []:
        if not s.get("tbd_markers"):
            continue
        # Synthesise a TestEntry-compatible shape for the agent prompt and
        # the patcher; `id` is derived from the file stem.
        out.append({
            "id": f"S-{s.get('name', 'support')}",
            "name": s.get("name", ""),
            "file": s.get("file", ""),
            "line": 1,
            "status": "pending",
            "tbd_markers": s.get("tbd_markers") or [],
            "_kind": s.get("kind", "other"),  # informational; ignored by patcher
        })
    return out


def _auth_summary_for_prompt(active_module: dict | None) -> str:
    """Compact auth-flow summary for inclusion in an agent prompt.

    Inlined from the (now-deleted) `_sut_staging` helper. Step 8 and Step 9
    both call this — keep it local to step 8 (the canonical user) and have
    step 9 import from here, rather than reviving a tiny shared module.
    """
    if not active_module:
        return ""
    auth = active_module.get("auth_flow") or {}
    lines = [
        f"Active module: `{active_module.get('name')}` "
        f"(path: `{active_module.get('path')}`, language: "
        f"`{active_module.get('language') or 'unknown'}`)",
        f"Auth type: `{auth.get('type', 'unknown')}`",
    ]
    if auth.get("entry_method"):
        lines.append(f"Auth entry method: `{auth['entry_method']}`")
    if auth.get("fixture_entry"):
        lines.append(f"Auth fixture: `{auth['fixture_entry']}`")
    creds = auth.get("credentials_env_vars") or []
    if creds:
        lines.append(f"Credentials env vars: {', '.join(creds)}")
    return "\n".join(lines)


def _auth_relevant_sut_files(active_module: dict | None) -> list[str]:
    """Return SUT-relative paths of auth/page-object/helper/fixture/locator
    files for the active module. Replaces the old `collect_sut_files` from
    `_sut_staging.py`; used to populate the prompt's "files you can call"
    list (no staging anymore — the agent reads them via `add_dirs=[sut]`).
    """
    if not active_module:
        return []
    files: list[str] = []
    auth = active_module.get("auth_flow") or {}
    for key in ("entry_method", "fixture_entry"):
        v = auth.get(key)
        if isinstance(v, str) and v:
            files.append(v.split(":", 1)[0])
    for bucket in ("existing_page_objects", "existing_fixtures",
                   "existing_helpers", "existing_locators"):
        for entry in active_module.get(bucket) or []:
            p = entry.get("file") if isinstance(entry, dict) else None
            if p:
                files.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for p in files:
        if p and p not in seen and Path(p).name != "__init__.py":
            seen.add(p)
            out.append(p)
    return out


def _build_user_prompt(
    index: dict,
    sut_base_url: str | None,
    *,
    active_module: dict | None = None,
    sut_root: Path | None = None,
    staged_files: list[str] | None = None,
) -> str:
    items: list[str] = []
    for t in _tests_with_tbd(index):
        marker_parts: list[str] = []
        for m in t["tbd_markers"]:
            chunk = f"line {m['line']}: {m['raw'][:80]}"
            desc = m.get("description")
            if desc:
                chunk += f"  — intent: {desc.strip()[:140]}"
            fn = m.get("test_function")
            if fn:
                chunk += f"  (in `{fn}`)"
            marker_parts.append(chunk)
        markers = "; ".join(marker_parts)
        items.append(f"- {t['id']}  ({t['file']}):  {markers}")
    listing = "\n".join(items) or "(no TBD markers detected)"
    base = sut_base_url or "(unset)"

    # Authentication context block. Only emitted when an active module exists.
    # `staged_files` (when provided) lists SUT-relative paths the agent should
    # read directly from `sut_root` (granted via `add_dirs=[sut_root]`). The
    # previous `./_sut/<rel>` staging-dir convention is gone — no SUT copy
    # exists under the step workdir anymore.
    auth_block = ""
    if active_module:
        summary = _auth_summary_for_prompt(active_module)
        if sut_root is None:
            # Tests that exercise prompt construction without a workspace
            # pass `sut_root=None`; render relative paths so the string is
            # still inspectable.
            files_str = "\n".join(f"  - `{p}`" for p in (staged_files or [])) \
                or "  (none discovered)"
        else:
            files_str = "\n".join(f"  - `{sut_root / p}`" for p in (staged_files or [])) \
                or "  (none discovered)"
        auth_block = (
            f"\n\n--- SUT NAVIGATION CONTEXT ---\n{summary}\n"
            + (f"\nSUT clone root (read these files directly — you have "
               f"add_dirs access): `{sut_root}`\n"
               if sut_root is not None else "\n")
            + (f"\nThe following SUT files are most relevant for sign-in / "
               f"navigation (do not reinvent auth from the DOM — call these "
               f"instead):\n{files_str}\n\n"
               f"Workflow: (1) read the auth entry method + relevant page "
               f"objects above. (2) Use the Playwright MCP to navigate via the "
               f"SUT's existing sign-in flow before snapshotting — do NOT issue "
               f"a bare `browser_navigate` to a deep-link if the page is behind "
               f"auth. (3) Once authenticated, navigate to the page under test "
               f"and take an AOM snapshot. (4) Match selectors back to the TBD "
               f"markers below.\n")
        )

    return (
        f"You are resolving TBD_LOCATOR placeholders left by an upstream "
        f"codegen step. Base URL of the SUT: `{base}`. Use the Playwright "
        f"MCP to navigate; do not generate code, only discover the correct "
        f"locators. Honor the project's locator priority: id > data-testid > "
        f"role > label > text > placeholder > scoped css. Never propose XPath."
        f"\n\nSNAPSHOT POLICY — AOM-first (non-negotiable — see your agent "
        f"definition for the full rules):\n"
        f"  - For EVERY distinct URL opened this session: capture the AOM via "
        f"`browser_snapshot` and persist to `./page-snapshot-NN.json` "
        f"(NN zero-padded, starting at 01) BEFORE doing further work.\n"
        f"  - Raw-DOM is a FALLBACK only — permitted when the target element "
        f"is missing from the AOM, non-semantic (div/span without ARIA), or "
        f"hidden from screen readers. When you fall back, capture via "
        f"`browser_evaluate(() => document.documentElement.outerHTML)` and "
        f"persist to `./page-snapshot-NN-raw.html`, AND set "
        f"`snapshot_source: 'raw_dom_fallback'` plus a `fallback_reason` "
        f"(`not_in_aom` | `non_semantic` | `aria_hidden` | <free text>) "
        f"on every resolution item that cited that snapshot.\n"
        f"  - Re-visiting an already-captured URL does NOT re-capture; re-use "
        f"the persisted file. Within a captured page, use element-scoped "
        f"queries only (never re-capture the whole page).\n"
        f"  A sibling audit agent in Step 8b reads these files to verify your "
        f"resolutions against DOM truth — persisting them is mandatory."
        f"\n\nHITL ESCAPE — for TBDs you genuinely cannot resolve, set "
        f"`applied: false` with a clear `skip_reason` AND append a "
        f"`[CLARIFICATION NEEDED: <CONST> @ <file>:<line>]` block to "
        f"`./{_CLARIFICATIONS_FILE}` (do NOT create the file if every TBD "
        f"resolved cleanly). Include the locator's intent, the selectors you "
        f"tried, and the snapshot evidence. The orchestrator prompts the "
        f"user with each block and splices their selector / spec-gap choice "
        f"directly into `./locator-resolution.json` — you do not need to "
        f"re-invoke or re-edit the JSON yourself."
        f"{auth_block}"
        f"\n\nTests requiring resolution (paths are relative to the SUT root "
        f"`{sut_root}` — read them at that absolute location). Each TBD's "
        f"`intent:` comes from the codegen agent's `TBD_INTENT` comment "
        f"(may be absent on legacy runs — infer intent from the constant "
        f"name and surrounding test code in that case):\n\n"
        f"{listing}\n\nWrite the result to `./locator-resolution.json` "
        f"following this shape: "
        f'{{"base_url": "...", "resolutions": [{{"test_id": "<id>", "file": "<rel>", '
        f'"items": [{{"tbd": "<exact TBD token>", "replacement": "<locator>", '
        f'"strategy": "<one of: id|data-testid|role|label|text|placeholder|css>", '
        f'"line": <int>, "confidence": <0..1>, '
        f'"snapshot_source": "aom"|"raw_dom_fallback", "fallback_reason": <str|null>}}]}}]}}'
    )


def _filter_index_to_worca(index: IndexResult, sut_root: Path) -> IndexResult:
    """Same worca-only filter step 7 uses. Re-implemented here rather than
    imported so step 8 stays independent of step 7's module layout."""
    def is_worca(rel: str) -> bool:
        return Path(rel).name.lower().startswith("worca")
    return _dc_replace(
        index,
        test_root=str(sut_root),
        files=[f for f in index.files if is_worca(f)],
        tests=[t for t in index.tests if is_worca(t.file)],
        support_files=[s for s in index.support_files if is_worca(s.file)],
        violations=[v for v in index.violations if is_worca(v.file)],
    )


def _rank_strategy(strategy: str) -> int:
    return _PRIORITY.index(strategy) if strategy in _PRIORITY else len(_PRIORITY)


def _resolve_patch_target(tests_dir: Path, file_rel: str) -> Path | None:
    """Best-effort lookup of the actual on-disk path for a resolution's `file`.

    The agent's `file` field SHOULD be relative to `tests_dir` (no leading
    `tests/`). In practice the agent sometimes prefixes `tests/` (because the
    SUT's test directory layout is `tests/...`), or even uses absolute paths.
    Without a fallback, all such resolutions get `applied=false` and the user
    sees `applied=0 skipped=N` despite the agent producing correct locators.

    Resolution order:
      1. `tests_dir / file_rel` — the expected case.
      2. Strip leading path components (`tests/`, `tests/pages/`, ...) one
         at a time until a match is found.
      3. Match by basename anywhere under `tests_dir` — single hit wins,
         ambiguous matches return None so caller skips with a clear reason.
    Returns None when no on-disk target exists.
    """
    direct = tests_dir / file_rel
    if direct.exists():
        return direct

    parts = Path(file_rel).parts
    # Try progressively-shorter suffix paths (strip leading components).
    for i in range(1, len(parts)):
        candidate = tests_dir.joinpath(*parts[i:])
        if candidate.exists():
            return candidate

    # Last-resort: unique basename match anywhere under tests_dir.
    basename = Path(file_rel).name
    matches = list(tests_dir.rglob(basename))
    if len(matches) == 1 and matches[0].is_file():
        return matches[0]
    return None


# How many lines on either side of the agent's reported `line` we'll scan
# for the TBD token when the exact line doesn't contain it. Wider than the
# pre-anchor 3-line tolerance because the new anchor check (`_is_assignment_line`)
# rejects comment lines and decorative mentions — the only way a wrong line
# matches inside the window is if there are multiple legitimate assignments
# to the same TBD token in the file, which the codegen agent does not do.
_LINE_DRIFT_TOLERANCE = 10


def _is_assignment_line(line: str, tbd_token: str) -> bool:
    """True when `line` is a patchable host for `tbd_token` — i.e. the token
    is present AND its position on the line is not inside a single-line
    comment.

    Accepts (every realistic codegen shape):
        ``LOGIN_BUTTON = TBD_LOCATOR``                    (assignment)
        ``self.LOGIN_BUTTON = "TBD_LOCATOR"``             (Python attr)
        ``const x = "TBD_LOCATOR";``                      (JS/TS)
        ``await page.locator('TBD_LOCATOR').click();``    (inline call arg)
        ``By.cssSelector("TBD_LOCATOR")``                 (Java argument)

    Rejects:
        ``# fallback to TBD_LOCATOR (TODO)``                — Python comment
        ``// TBD_INTENT: TBD_LOCATOR is the login button`` — JS/TS comment
        ``  *  TBD_LOCATOR``                                — JSDoc continuation

    The rule: the substring on `line` before the token does not start with a
    single-line comment marker (`#`, `//`, `*`, `/*`). False positives are
    bounded — comment lines that mention the literal token (e.g. the
    `TBD_INTENT:` description comments the codegen agent attaches) are
    rejected outright, which is what protects us from scrambled patches.
    """
    if tbd_token not in line:
        return False
    idx = line.find(tbd_token)
    prefix = line[:idx]
    stripped = prefix.lstrip()
    return not stripped.startswith(("#", "//", "*", "/*"))


def _coerce_line(raw: object) -> int | None:
    """Coerce an agent-supplied `line` value to a positive 1-based int.

    The schema declares `line: int|null` but agents have been observed
    emitting `"7"` (string) and `7.0` (float). Returns None for any value
    that can't be safely interpreted as a positive integer.
    """
    if raw is None:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _classify_item(item: dict, file_text: str) -> tuple[bool, dict | None]:
    """Run strategy/xpath/no-op checks that don't depend on line numbers.

    Returns `(can_proceed, skip_annotation_or_None)`. When `can_proceed`
    is False, the caller MUST use the returned skip annotation and not
    attempt replacement. When True, the caller proceeds to line-targeted
    or global replacement.

    Order matters: xpath rejection runs BEFORE any line-validity check so
    a malicious/wrong agent response that includes both `line: 999` AND
    `replacement: 'xpath=//x'` skips with the security-critical reason
    (`xpath`), not the milder `line out of bounds`.
    """
    replacement = item.get("replacement", "")
    strategy = item.get("strategy", "")
    tbd_token = item.get("tbd", "TBD_LOCATOR")

    if strategy not in _PRIORITY:
        # When the agent honestly skipped — strategy=null AND it supplied a
        # diagnostic skip_reason — preserve the agent's reason instead of
        # clobbering it with the generic "unknown strategy: None". Otherwise
        # we destroy valuable context like
        # "no DOM element matched: <description>" that downstream
        # comparison and reporting need.
        existing_reason = item.get("skip_reason")
        if strategy is None and existing_reason:
            return False, dict(item, applied=False, skip_reason=existing_reason)
        return False, dict(item, applied=False,
                           skip_reason=f"unknown strategy: {strategy}")
    if _is_xpath_replacement(replacement):
        return False, dict(item, applied=False,
                           skip_reason="xpath replacement rejected")
    if replacement == tbd_token:
        # Agent echoed the TBD token back (confidence 0.0, unresolved).
        # Applying this would consume an occurrence without changing
        # anything, shifting any later replacements by one slot.
        return False, dict(item, applied=False,
                           skip_reason="replacement is the TBD token itself (no-op)")
    if tbd_token not in file_text:
        # The token has been fully consumed by earlier patches, OR the file
        # doesn't actually contain it (agent reported a phantom).
        return False, dict(item, applied=False,
                           skip_reason=f"token not found: {tbd_token!r}")
    return True, None


def _apply_line_targeted(
    lines: list[str], item: dict, line_no: int,
) -> tuple[list[str], dict, str | None]:
    """Anchor on `CONST = TBD_LOCATOR`-style assignment lines within the drift window.

    Returns `(new_lines, annotated_item, applied_via)` where `applied_via`
    is `"line:N"` on a direct hit, `"line:N±M (anchor)"` when drift-search
    succeeded against an assignment line, or `None` when no assignment-line
    candidate exists within tolerance. **The caller MUST treat `None` as a
    skip** (mark `applied: false`) — no global text-replacement fallback,
    because that path was the source of mis-patches when the TBD token
    appeared in multiple places (comments + assignments) in the same file.

    Replaces exactly ONE token per call — preserves the one-item-one-
    occurrence contract that lets the agent emit two items for two TBDs
    on the same line and have both applied independently.

    Search order within the drift window:
      1. The exact agent-reported line, if it is an assignment line.
      2. Lines progressively further from the agent-reported line
         (±1, ±2, ..., ±_LINE_DRIFT_TOLERANCE), preferring the closest
         assignment-line match.
    """
    tbd_token = item.get("tbd", "TBD_LOCATOR")
    replacement = item.get("replacement", "")
    n = len(lines)

    if line_no < 1 or line_no > n:
        return lines, dict(item, applied=False,
                           skip_reason=f"line {line_no} out of bounds (file has {n} lines)"), None

    target_idx = line_no - 1

    # Direct hit on the agent-reported line — requires assignment-line shape.
    if _is_assignment_line(lines[target_idx], tbd_token):
        lines[target_idx] = lines[target_idx].replace(tbd_token, replacement, 1)
        return lines, dict(item, applied=True, skip_reason=None), f"line:{line_no}"

    # Drift tolerance: scan nearby lines, preferring closest assignment line.
    for offset in range(1, _LINE_DRIFT_TOLERANCE + 1):
        for candidate in (target_idx - offset, target_idx + offset):
            if 0 <= candidate < n and _is_assignment_line(lines[candidate], tbd_token):
                lines[candidate] = lines[candidate].replace(tbd_token, replacement, 1)
                actual = candidate + 1
                return lines, dict(item, applied=True, skip_reason=None), f"line:{actual} (anchor drift from {line_no})"

    # No patchable line match within the window. We deliberately do NOT
    # fall through to a global text-replacement: that path was responsible
    # for scrambled patches when the TBD token appeared in comments or
    # multiple non-adjacent places in the same file. Mark the item skipped
    # and let HITL / 8b audit surface it.
    return lines, dict(
        item, applied=False,
        skip_reason=(
            f"no patchable `{tbd_token}` occurrence on line {line_no} or within "
            f"±{_LINE_DRIFT_TOLERANCE} lines (comment lines are excluded); "
            "possible source drift"
        ),
    ), None


def _split_with_endings(text: str) -> tuple[list[str], str]:
    """Split `text` into lines while preserving each line's original line
    ending. Returns `(lines, bom)` where `bom` is the leading UTF-8 BOM
    if present, '' otherwise. Reassemble via `bom + ''.join(lines)`.
    """
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom):]
    return body.splitlines(keepends=True), bom


def _apply_patches(
    tests_dir: Path,
    resolutions: list[dict],
) -> list[dict]:
    """Mutate tests_dir files in place using line-targeted replacement.

    Algorithm:
      1. Resolve each resolution's `file` to an on-disk path via the
         3-tier fallback in `_resolve_patch_target`.
      2. Group items by resolved file path.
      3. For each file:
         a. Read text with `utf-8-sig` (strips BOM if any; re-prepended
            on write).
         b. Split into lines preserving original line endings (CRLF/LF
            both survive round-trip).
         c. Sort items by (line ascending, strategy priority ascending).
            Items without a valid `line` field sort to the FRONT (legacy
            global-replacement path) so they consume untouched text
            before line-targeted edits change positions.
         d. For each item:
            - Run `_classify_item` (strategy/xpath/no-op/token-presence)
              before any line indexing.
            - If `line` is valid: try `_apply_line_targeted`. On miss
              (token not within ±N lines), fall back to global
              first-occurrence replacement and log the fallback.
            - If no `line`: use global first-occurrence (legacy path).
         e. Write back with preserved line endings + BOM.

    Returns annotated items (one per input item) with `applied` and
    `skip_reason`. Items receive an additional `applied_via` field
    indicating which path resolved them (audit aid).
    """
    out: list[dict] = []

    # Resolve target paths up front; route unresolvable file refs to skip.
    by_file: dict[Path, list[tuple[dict, dict]]] = {}
    for r in resolutions:
        file_rel = r.get("file")
        if not file_rel:
            continue
        target = _resolve_patch_target(tests_dir, file_rel)
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

        # Read as bytes to preserve CRLF / LF / BOM byte-for-byte. The default
        # `read_text` would silently normalise line endings via Python's
        # universal-newlines mode, producing a giant whole-file diff on
        # CRLF-checked-out Windows SUTs after the first patch.
        raw_bytes = path.read_bytes()
        had_bom = raw_bytes[:3] == b"\xef\xbb\xbf"
        body_bytes = raw_bytes[3:] if had_bom else raw_bytes
        raw_text = body_bytes.decode("utf-8")
        lines, _ = _split_with_endings(raw_text)

        # Sort order:
        #   - Items without a valid `line` go FIRST (legacy global path)
        #     so they see un-patched text before line-targeted edits.
        #   - Then items with `line`, ascending by line number.
        #   - Ties: strategy priority (id > data-testid > role > ...).
        # `_coerce_line` returns None for invalid; key-sort puts None
        # first via a sentinel.
        def _sort_key(e: tuple[dict, dict]) -> tuple[int, int, int]:
            ln = _coerce_line(e[1].get("line"))
            line_key = ln if ln is not None else -1  # legacy items first
            strat_key = _rank_strategy(e[1].get("strategy", "css"))
            return (line_key, strat_key, id(e[1]))

        entries.sort(key=_sort_key)

        for r, item in entries:
            # Recompute text from current lines for classification checks
            # (a previous replacement may have consumed the only occurrence).
            current_text = "".join(lines)
            can_proceed, skip_anno = _classify_item(item, current_text)
            if not can_proceed:
                out.append(skip_anno)
                _record(r, skip_anno)
                continue

            line_no = _coerce_line(item.get("line"))

            if line_no is None:
                # Legacy / un-anchored item: global first-occurrence. Kept
                # for genuinely line-less entries (rare; older agents that
                # didn't emit `line`). The drift-induced global fallback —
                # the one that used to mispatch when items DID have a line
                # but missed by more than tolerance — is gone (see below).
                replacement = item.get("replacement", "")
                tbd_token = item.get("tbd", "TBD_LOCATOR")
                new_text = current_text.replace(tbd_token, replacement, 1)
                lines, _ = _split_with_endings(new_text)
                annotated = dict(item, applied=True, skip_reason=None,
                                 applied_via="global (no line)")
                out.append(annotated)
                _record(r, annotated)
                continue

            new_lines, annotated, applied_via = _apply_line_targeted(
                lines, item, line_no,
            )
            if applied_via is None:
                # Line-targeted anchor miss → leave the file untouched and
                # let HITL / 8b audit surface the unresolvable TBD. No
                # global-replacement fallback (that was the source of
                # scrambled assignments).
                annotated["applied_via"] = "none"
            else:
                lines = new_lines
                annotated["applied_via"] = applied_via

            out.append(annotated)
            _record(r, annotated)

        # Write back as bytes so universal-newlines mode can't normalise the
        # line endings we just preserved via splitlines(keepends=True).
        body_out = "".join(lines).encode("utf-8")
        prefix = b"\xef\xbb\xbf" if had_bom else b""
        path.write_bytes(prefix + body_out)

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


def _constant_name_for(item: dict, tests_dir: Path | None) -> str:
    """Best-effort recovery of the constant name for an applied item.

    The agent emits the TBD token + line in `locator-resolution.json` but
    not the surrounding constant name (`LOCALE_SWITCHER`). For the duplicates
    report it's much more useful to say "LOCALE_SWITCHER and LANGUAGE_DROP_DOWN
    share the same selector" than to repeat the TBD token twice. We pull the
    name off the patched line; if anything looks wrong we fall back to the
    `tbd` field so the report is still informative.
    """
    file_rel = item.get("file")
    line_no = item.get("line")
    if not file_rel or not isinstance(line_no, int) or tests_dir is None:
        return str(item.get("tbd") or "")
    target = _resolve_patch_target(tests_dir, file_rel)
    if target is None or not target.exists():
        return str(item.get("tbd") or "")
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return str(item.get("tbd") or "")
    idx = line_no - 1
    if idx < 0 or idx >= len(lines):
        return str(item.get("tbd") or "")
    m = _CONST_NAME_RE.search(lines[idx])
    return m.group(1) if m else str(item.get("tbd") or "")


def _iter_applied_items(payload: dict) -> Iterator[tuple[dict, dict]]:
    """Yield `(resolution, item)` pairs for every applied item across all
    resolutions in `payload`. Filters out skipped / non-applied items so
    duplicate / mask detection only sees what actually landed in the
    generated code."""
    for r in payload.get("resolutions") or []:
        file_rel = r.get("file")
        if not file_rel:
            continue
        for item in r.get("items") or []:
            if not item.get("applied"):
                continue
            # Synthesise a `file` field on the item for downstream helpers
            # that don't have the surrounding `resolution` dict in scope.
            yield r, dict(item, file=file_rel)


def _detect_duplicate_replacements(
    payload: dict, *, tests_dir: Path | None = None,
) -> list[dict]:
    """Group applied items by `(file, replacement)`; report groups > 1.

    Surfaces both flavours of duplication seen in the wild:
      - Codegen-invented overlap (`GEMINI_NAV_BUTTON` and `GEMINI_NAV_LINK`
        both → `[data-testid="Layout-GeminiEnterprise"]`).
      - Resolver collapse (`TOOLTIP` masked onto the same button selector
        because no real tooltip element exists).

    Returns one entry per duplicate group:
      `{file, replacement, members: [{name, tbd, line, strategy, confidence}], reason}`

    Doesn't fail the step — purely advisory for the report.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for _r, item in _iter_applied_items(payload):
        key = (item["file"], item.get("replacement", ""))
        groups.setdefault(key, []).append(item)

    out: list[dict] = []
    for (file_rel, replacement), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        members = [
            {
                "name": _constant_name_for(it, tests_dir),
                "tbd": it.get("tbd"),
                "line": it.get("line"),
                "strategy": it.get("strategy"),
                "confidence": it.get("confidence"),
            }
            for it in items
        ]
        out.append({
            "file": file_rel,
            "replacement": replacement,
            "members": members,
            "reason": (
                f"{len(items)} applied items resolved to the same selector — "
                "either codegen invented overlapping constants OR the resolver "
                "collapsed multiple TBDs onto one element. Review the named "
                "constants and either merge them into one or correct the "
                "selectors."
            ),
        })
    return out


def _detect_low_confidence_masks(
    payload: dict,
    *,
    threshold: float = _LOW_CONFIDENCE_THRESHOLD,
    tests_dir: Path | None = None,
) -> list[dict]:
    """Items with `applied=True AND confidence<threshold AND replacement is
    shared with another item in the same file` — the silent-mask signature.

    The bare "low confidence" alone isn't actionable (the resolver legitimately
    sometimes finds a single-best match it's not 100% sure about). What
    matters is the combination: low confidence PLUS the same selector being
    handed back for another TBD. That pattern means the agent fell back to
    a parent/sibling rather than admitting the element doesn't exist —
    which is exactly the spec/implementation gap we want to surface.
    """
    # First, build the per-file selector reuse map (regardless of confidence).
    reuse: dict[tuple[str, str], int] = {}
    for _r, item in _iter_applied_items(payload):
        key = (item["file"], item.get("replacement", ""))
        reuse[key] = reuse.get(key, 0) + 1

    out: list[dict] = []
    for _r, item in _iter_applied_items(payload):
        conf = item.get("confidence")
        if not isinstance(conf, (int, float)):
            continue
        if conf >= threshold:
            continue
        key = (item["file"], item.get("replacement", ""))
        if reuse.get(key, 0) < 2:
            continue  # low conf but unique selector — not a silent mask
        out.append({
            "name": _constant_name_for(item, tests_dir),
            "file": item["file"],
            "tbd": item.get("tbd"),
            "line": item.get("line"),
            "strategy": item.get("strategy"),
            "confidence": conf,
            "replacement": item.get("replacement"),
            "reason": (
                f"applied at confidence {conf:.2f} (< {threshold:.2f}) AND "
                f"selector is reused for another TBD in {item['file']!s} — "
                "likely a suspected spec/implementation gap that the "
                "resolver masked with a parent/duplicate selector instead "
                "of flagging via skip_reason."
            ),
        })
    return out


def _build_comparison_prompt(
    index: dict,
    sut_root: Path,
    *,
    snapshot_filenames: list[str],
) -> str:
    """Prompt for Step 8b — polyglot-test-fixer in DOM-COMPARISON-AUDIT mode.

    The fixer reads the locator-resolution + snapshots persisted by 8a and
    emits dom-comparison.json with one verdict per TBD constant. Opens with
    the literal token the agent's audit-mode section keys off.
    """
    constants: list[str] = []
    for entry in _tests_with_tbd(index):
        markers = "; ".join(
            f"line {m['line']}: {m['raw'][:80]}"
            for m in entry.get("tbd_markers") or []
        )
        constants.append(f"- {entry['id']}  ({entry.get('file', '?')}):  {markers}")
    listing = "\n".join(constants) or "(no TBD markers in index)"

    if snapshot_filenames:
        snap_list = "\n".join(f"  - `./{name}`" for name in snapshot_filenames)
    else:
        snap_list = "  (none — playwright-tester did not persist any snapshots; emit `unevaluated` verdicts)"

    return (
        f"{_AUDIT_MODE_TOKEN}\n\n"
        f"You are in audit-only mode. Do NOT call any Playwright MCP tool. "
        f"Do NOT edit any file inside the SUT. Read the inputs listed below "
        f"and write a single output file at `./{_DOM_COMPARISON_FILE}` "
        f"conforming to `dom-comparison.schema.json`.\n\n"
        f"INPUTS — files already present in this working directory:\n"
        f"  - `./tbd-index.json` (every TBD constant in the codegen output)\n"
        f"  - `./locator-resolution.json` (playwright-tester's resolution attempt — you AUDIT it, you don't rewrite it)\n"
        f"  - Page snapshots (HTML for distinct URLs 1 & 2 of the session, AOM JSON for #3+):\n"
        f"{snap_list}\n\n"
        f"INPUTS — files inside the SUT (read-only via `add_dirs`). SUT root: `{sut_root}`. "
        f"Read each codegen-produced test/locator file to infer the semantic intent "
        f"of every TBD constant from function names and assertion bodies:\n\n"
        f"{listing}\n\n"
        f"VERDICTS to emit per TBD constant (see schema for the full list):\n"
        f"  - `matched`     — a real DOM element exists for this constant's intent (fill matched_selector / confidence)\n"
        f"  - `ghost`       — no element exists in any snapshot for this constant's intent (fill explanation)\n"
        f"  - `duplicate`   — same DOM element as another constant (fill duplicate_of + explanation)\n"
        f"  - `low_confidence` — element exists that *might* be the intended one, but uncertain\n"
        f"  - `unevaluated` — snapshot coverage insufficient to decide\n\n"
        f"The `summary.should_exist_total` MUST equal matched + low_confidence + unevaluated "
        f"(it EXCLUDES ghost and duplicate). The pipeline divides into this number when "
        f"computing the apply-rate gate — honest ghost/duplicate verdicts no longer "
        f"penalise the run.\n\n"
        f"Honest verdicts beat hopeful ones. A `ghost` for a non-existent element is "
        f"the correct outcome — it surfaces a spec/codegen gap rather than masking it."
    )


def _apply_comparison_verdict(payload: dict, comparison: dict) -> dict:
    """Stamp Step 8b's audit verdicts into the Step 8a locator-resolution payload.

    For each entry in `comparison.expected_elements`:
      - Locate the matching item in `payload.resolutions[*].items` by (file, line)
        and stamp `comparison_verdict` on it.
      - When the verdict is `ghost` or `duplicate`, additionally force
        `applied=false`, `strategy=None`, `replacement=None`, and replace
        `skip_reason` with the auditor's `explanation` (or a synthesised one).

    Mutates and returns `payload` for chaining. Items the auditor did not
    evaluate keep their original state; their `comparison_verdict` is left
    as `None` so the apply-rate gate can fall back to the pre-audit logic
    for them.
    """
    by_file_line: dict[tuple[str | None, int | None], dict] = {}
    for resolution in payload.get("resolutions") or []:
        file_rel = resolution.get("file")
        for item in resolution.get("items") or []:
            key = (file_rel, item.get("line"))
            by_file_line[key] = item

    for expected in comparison.get("expected_elements") or []:
        verdict = expected.get("verdict")
        if not verdict:
            continue
        key = (expected.get("file"), expected.get("line"))
        item = by_file_line.get(key)
        if item is None:
            # Auditor mentioned a constant we don't have a resolution item for.
            # Could happen if Step 7's index drifted vs the agent's expected list.
            # Skip silently — the comparison report still records the verdict.
            continue
        item["comparison_verdict"] = verdict
        if verdict in ("ghost", "duplicate"):
            explanation = expected.get("explanation") or (
                f"duplicate of {expected.get('duplicate_of') or '?'}"
                if verdict == "duplicate" else "no DOM element matches this constant's intent"
            )
            item["applied"] = False
            item["strategy"] = None
            item["replacement"] = None
            item["skip_reason"] = f"dom-comparison: {explanation}"
            # Drop applied_via if we previously thought we applied this.
            item.pop("applied_via", None)
    return payload


def _audit_snapshot_policy(
    wd: Path, payload: dict | None = None,
) -> list[dict]:
    """Verify the AOM-first snapshot policy by inspecting the persisted files
    plus the resolution payload.

    Returns a list of violation dicts; empty list = compliant. Each violation:
      {kind: "raw_without_fallback_reason" | "no_aom_captures",
       file: <str|None>, hint: <str>}

    The rule is now AOM-first: every distinct URL the agent captured should
    have produced a `page-snapshot-NN.json` (AOM). Raw-DOM captures
    (`page-snapshot-NN-raw.html`) are permitted only when at least one
    resolution item carries `snapshot_source: "raw_dom_fallback"` with a
    `fallback_reason` populated. A `*-raw.html` with no payload-side
    justification is a policy violation.

    Best-effort: returns ``[]`` when the workdir is missing or the payload
    is malformed — the resolver still produced data, so an unparseable
    audit shouldn't fail the step.
    """
    if not wd.exists() or not wd.is_dir():
        return []

    try:
        snapshot_files = sorted(p.name for p in wd.glob(_SNAPSHOT_GLOB) if p.is_file())
    except OSError:
        return []

    aom_files = [n for n in snapshot_files if _AOM_SNAPSHOT_RE.match(n)]
    raw_files = [n for n in snapshot_files if _RAW_SNAPSHOT_RE.match(n)]

    violations: list[dict] = []

    # Tally how many resolutions explicitly invoked the raw-DOM fallback,
    # which is what authorises the presence of `*-raw.html` files.
    fallback_count = 0
    if isinstance(payload, dict):
        for resolution in payload.get("resolutions") or []:
            for item in resolution.get("items") or []:
                if item.get("snapshot_source") == "raw_dom_fallback":
                    fallback_count += 1

    if raw_files and fallback_count == 0:
        violations.append({
            "kind": "raw_without_fallback_reason",
            "file": raw_files[0] if len(raw_files) == 1 else None,
            "hint": (
                f"agent persisted {len(raw_files)} raw-DOM snapshot(s) "
                f"({', '.join(raw_files[:3])}{'…' if len(raw_files) > 3 else ''}) "
                "but no resolution carries `snapshot_source=raw_dom_fallback`. "
                "AOM-first policy: raw-DOM captures must be justified per-item "
                "with a `fallback_reason`. Treating as advisory; agent prompt "
                "may need sharpening."
            ),
        })

    if not aom_files and snapshot_files:
        # The agent captured something but no AOM at all — this is the
        # signature of the old HTML-first policy still being followed.
        violations.append({
            "kind": "no_aom_captures",
            "file": None,
            "hint": (
                f"agent persisted snapshots ({len(snapshot_files)} file(s)) but "
                "none of them are AOM (`page-snapshot-NN.json`). The current "
                "policy is AOM-first; raw-DOM is fallback-only."
            ),
        })

    return violations


def _count_playwright_mcp_calls(transcript_path: Path) -> int:
    """Count tool uses of any ``mcp__playwright__*`` tool in the agent transcript.

    The MCP-usage gate in :class:`LocatorResolutionStep` uses this: the
    playwright-tester can return ``success=True`` without ever invoking
    Playwright MCP (e.g. by delegating to a Task sub-agent that doesn't
    inherit MCP, then accepting its "not connected" report). Zero calls +
    non-empty TBD set means no live DOM evidence — fail attempt 1 so the
    debug agent co-runs on retry.

    Best-effort: returns 0 when the transcript is missing or unreadable.
    """
    if not transcript_path.exists():
        return 0
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return len(re.findall(r'"name"\s*:\s*"mcp__playwright__', text))


def _agent_sut_writes_outside_scope(
    sut_root: Path,
    allowed_files: set[str],
) -> tuple[list[str], list[str]]:
    """Inspect the SUT git tree for files the agent touched outside its scope.

    Returns ``(out_of_scope_modified, out_of_scope_untracked)`` — both lists
    hold SUT-relative POSIX paths. Step 8 starts on a clean tree (step 7
    just committed), so any porcelain output is the agent's doing. Paths
    listed in ``allowed_files`` (the TBD-bearing files from tbd-index.json)
    are filtered out.

    Best-effort: returns ``([], [])`` when git is unavailable or status
    fails — never blocks the step on infrastructure issues.
    """
    try:
        result = _git(
            sut_root, "status", "--porcelain", "--untracked-files=all",
            check=False,
        )
    except OSError:
        return [], []
    if result.returncode != 0:
        return [], []

    out_of_scope: list[str] = []
    untracked: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        flags = line[:2]
        # Porcelain v1: "XY path" or "XY old -> new" for renames. Paths with
        # spaces / special chars get wrapped in double quotes by git.
        path_str = line[3:]
        if path_str.startswith('"') and path_str.endswith('"'):
            path_str = path_str[1:-1]
        if " -> " in path_str:
            path_str = path_str.split(" -> ", 1)[1]
        norm = Path(path_str).as_posix()
        if norm in allowed_files:
            continue
        if flags.startswith("??"):
            untracked.append(norm)
        else:
            out_of_scope.append(norm)
    return out_of_scope, untracked


def _revert_agent_writes(
    sut_root: Path,
    modified_paths: list[str],
    untracked_paths: list[str],
) -> None:
    """Best-effort revert of out-of-scope agent writes to the SUT.

    Tracked-but-modified files restore to HEAD via ``git checkout HEAD --``;
    untracked files (and the directories git introduced for them) clean via
    ``git clean -fd``. Used after a scope violation so the SUT is clean for
    the retry / next step. Failures are swallowed — partial revert beats
    aborting the cleanup, and the operator sees the original error.
    """
    if modified_paths:
        try:
            _git(sut_root, "checkout", "HEAD", "--", *modified_paths, check=False)
        except OSError:
            pass
    if untracked_paths:
        try:
            _git(sut_root, "clean", "-fd", "--", *untracked_paths, check=False)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HITL splicer for unresolvable TBDs
# ---------------------------------------------------------------------------

# Header inside a `[CLARIFICATION NEEDED: ...]` block. The playwright-tester
# is instructed to format the header as `<CONST_NAME> @ <file>:<line>`; this
# regex parses that back out so the splicer can find the matching item in
# `locator-resolution.json`.
_CLARIFICATION_HEADER_RE = re.compile(
    r"^\s*(?P<const>[A-Za-z_][A-Za-z0-9_]*)\s*@\s*(?P<file>.+):(?P<line>\d+)\s*$"
)

# Phrases the user can type to confirm a "spec gap" (element doesn't exist)
# instead of pasting a selector. Comparison is case-insensitive after strip.
_SPEC_GAP_ANSWERS = {
    "ghost", "spec-gap", "spec gap", "gap", "skip", "(b)", "b", "no element",
    "doesn't exist", "does not exist",
}


def _parse_clarification_header(question_text: str) -> tuple[str, str, int] | None:
    """Pull (constant_name, file, line) out of a clarification question's
    text. Returns None when the question doesn't match the agent's expected
    header format (a sign the agent emitted free-form text instead)."""
    m = _CLARIFICATION_HEADER_RE.match(question_text.strip())
    if not m:
        return None
    try:
        return m.group("const"), m.group("file").strip(), int(m.group("line"))
    except (ValueError, AttributeError):
        return None


def _infer_strategy(selector: str) -> str:
    """Best-effort: infer the locator strategy from a user-pasted selector.

    Returns one of the strategies in `_PRIORITY`; defaults to `"css"` for
    anything else (the broadest category in the priority chain). Never
    returns `xpath` — the caller is expected to have rejected XPath inputs
    via :func:`_is_xpath_replacement` already.
    """
    s = selector.strip()
    low = s.lower()
    if s.startswith("#") and " " not in s:
        return "id"
    if "data-testid" in low or "data-test-id" in low or "data-cy" in low:
        return "data-testid"
    if "getbytestid" in low.replace(" ", ""):
        return "data-testid"
    if low.startswith("role=") or "getbyrole" in low.replace(" ", ""):
        return "role"
    if "aria-label" in low or "getbylabel" in low.replace(" ", ""):
        return "label"
    if "getbyplaceholder" in low.replace(" ", "") or "placeholder=" in low:
        return "placeholder"
    if "getbytext" in low.replace(" ", "") or low.startswith("text="):
        return "text"
    return "css"


def _is_spec_gap_answer(answer: str) -> bool:
    """True when the user typed one of the standard 'spec gap' confirmations."""
    return answer.strip().lower() in _SPEC_GAP_ANSWERS


def _find_item_for_clarification(
    payload: dict, file_rel: str, line_no: int,
) -> dict | None:
    """Locate the resolution item that matches (file, line) so we can splice
    a user-supplied selector into it. Tries exact file match first; falls
    back to a basename-only match because the agent's clarification header
    sometimes uses a slightly different path style than the payload."""
    candidates: list[dict] = []
    for resolution in payload.get("resolutions") or []:
        res_file = resolution.get("file") or ""
        items = resolution.get("items") or []
        for item in items:
            if item.get("line") != line_no:
                continue
            if res_file == file_rel:
                return item  # exact match wins immediately
            # Compare by basename in case of leading-path drift.
            if Path(res_file).name == Path(file_rel).name:
                candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def _hitl_resolve_unresolvable(
    *, wd: Path, payload: dict, agent_label: str = "playwright-tester",
) -> int:
    """Read `./clarifications.md` and prompt the user for each unresolved TBD.

    For every answered question:
      - If the user typed a 'spec gap' phrase, mark the matching item
        `applied=false, comparison_verdict="ghost", source="hitl"`.
      - If the user pasted an XPath selector, reject it and mark the item
        skipped with a clear `skip_reason` (no patching).
      - Otherwise, infer the strategy from the selector's shape and splice
        `applied=true, replacement=<selector>, strategy=<inferred>,
        source="hitl"` into the matching item.

    Returns the number of items spliced. 0 in non-TTY / --no-hitl runs
    (because :func:`worca_t.hitl.prompt_user` returns ``{}``), in which
    case items stay as the agent left them and fall through to the
    apply-rate gate exactly as today.
    """
    clar = wd / _CLARIFICATIONS_FILE
    if not clar.exists():
        return 0
    try:
        md = clar.read_text(encoding="utf-8")
    except OSError:
        return 0
    questions = extract_questions(md)
    if not questions:
        return 0
    log.info(
        "hitl.locator_unresolvable",
        agent=agent_label,
        count=len(questions),
    )
    answers = prompt_user(questions, agent_label=agent_label)
    if not answers:
        return 0

    spliced = 0
    for q in questions:
        ans = answers.get(q.id)
        if not ans:
            continue
        parsed = _parse_clarification_header(q.prompt_text)
        if parsed is None:
            log.warning(
                "hitl.locator_unresolvable_unparseable_header",
                id=q.id,
                text=q.prompt_text[:200],
            )
            continue
        const_name, file_rel, line_no = parsed
        item = _find_item_for_clarification(payload, file_rel, line_no)
        if item is None:
            log.warning(
                "hitl.locator_unresolvable_no_match",
                file=file_rel,
                line=line_no,
                const=const_name,
            )
            continue
        if _is_spec_gap_answer(ans):
            item["applied"] = False
            item["strategy"] = None
            item["replacement"] = None
            item["comparison_verdict"] = "ghost"
            item["skip_reason"] = "user-confirmed spec gap (HITL)"
            item["source"] = "hitl"
            item.pop("applied_via", None)
            spliced += 1
            continue
        if _is_xpath_replacement(ans):
            log.warning(
                "hitl.locator_unresolvable_xpath_rejected",
                id=q.id,
                selector=ans[:120],
            )
            item["applied"] = False
            item["strategy"] = None
            item["replacement"] = None
            item["skip_reason"] = "user provided XPath; rejected"
            item["source"] = "hitl"
            item.pop("applied_via", None)
            spliced += 1
            continue
        item["applied"] = True
        item["strategy"] = _infer_strategy(ans)
        item["replacement"] = ans.strip()
        item["skip_reason"] = None
        item["source"] = "hitl"
        spliced += 1
    return spliced


class LocatorResolutionStep(Step):
    number = 8
    name = "locator-resolution"
    timeout_s = step_timeout(8)

    async def run(self, ctx: StepContext) -> StepResult:
        out_dir = self.out_dir(ctx.workspace)
        wd = self.workdir(ctx.workspace)
        wd.mkdir(parents=True, exist_ok=True)
        sut_root = ctx.workspace.sut.resolve()

        # Pre-flight: SUT must be present. Pipeline already verified the
        # branch state — re-check `.git/` here for the `--from-step 8` path.
        if not sut_root.exists() or not (sut_root / ".git").exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"SUT clone not found at {sut_root} (or missing .git). "
                    f"Step 8 patches files in place on the worca-t branch; "
                    f"re-run from step 1 to re-materialize."
                ),
            )

        # Step 7 wrote `tbd-index.json` into its artifact dir; that index
        # references files at SUT-relative paths (the agent now writes
        # directly into the SUT on the worca-t branch). The artifact dir
        # holds only metadata — no test bytes are duplicated there.
        src_codegen_root = ctx.workspace.step_dir(7)
        src_index = src_codegen_root / "tbd-index.json"
        if not src_index.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "step 8 requires step 7's tbd-index.json. Run step 7 "
                    "first (drop --only-step 8, or use --from-step 7)."
                ),
            )

        index = _load_index(src_index)
        sut_base_url = os.environ.get("SUT_BASE_URL")

        # JIT short-circuit. When the SUT is Python+pytest+Playwright AND the
        # codegen step vendored the runtime plugin (`tests/worca_t_runtime.py`),
        # locator resolution happens at Step 9 runtime — the plugin intercepts
        # `tbd("...")` sentinels against the live page. Step 8 writes a stub
        # artifact and returns "skipped" so the gate, the agent, the snapshot
        # audit, and 8b are all bypassed for this stack.
        jit_framework = index.get("framework") in {"pytest", "playwright-py"}
        jit_runtime_present = (sut_root / "tests" / "worca_t_runtime.py").is_file()
        if jit_framework and jit_runtime_present:
            tests_needing = _tests_with_tbd(index)
            stub_payload = {
                "base_url": sut_base_url,
                "mode": "jit",
                "resolutions": [],
                "totals": {
                    "tests_with_tbd": len(tests_needing),
                    "items": 0,
                    "applied": 0,
                    "skipped": 0,
                },
            }
            resolution_path = out_dir / "locator-resolution.json"
            resolution_path.write_text(
                json.dumps(stub_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info(
                "step08.jit_mode_skip",
                framework=index.get("framework"),
                tests_with_tbd=len(tests_needing),
                hint=(
                    "Python+pytest+Playwright stack with worca-t runtime "
                    "vendored — locator resolution deferred to Step 9 JIT "
                    "plugin against the live page."
                ),
            )
            return StepResult(
                success=True,
                status="skipped",
                outputs=[resolution_path],
                notes=(
                    f"JIT mode: {len(tests_needing)} TBD-bearing files; "
                    f"resolution deferred to Step 9 runtime plugin"
                ),
            )

        # Fail-fast under --no-hitl when no URL is available: the playwright-tester
        # agent has no way to discover the SUT and would otherwise burn its turn
        # budget probing localhost ports that aren't there. The url_resolution
        # audit (from Step 6) tells the user where we already looked.
        if not sut_base_url and _tests_with_tbd(index):
            no_hitl = getattr(ctx.options, "no_hitl", False)
            url_audit = _load_url_audit(ctx)
            if no_hitl:
                trail = ", ".join(url_audit.get("trail", []) or []) or "<no trail recorded>"
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[],
                    error=(
                        "BASE_URL_UNRESOLVED: SUT_BASE_URL is unset and --no-hitl "
                        "prevents interactive prompt. Step 6 url_resolution trail: "
                        f"{trail}. Provide a value via --env-file, host env, or "
                        "Azure DevOps Variable Groups."
                    ),
                )
            log.warning(
                "step08.sut_base_url_missing",
                hint="SUT_BASE_URL is not set; locator resolution via Playwright "
                     "may fail. Set it in your environment, via --env-file, or "
                     "respond to the Step 6 interactive prompt.",
                url_audit=url_audit,
            )

        # Patch target is the SUT clone itself. No mirroring — step 7 already
        # wrote files in place under `<workspace>/sut/` on the worca-t branch,
        # and step 8 mutates them line-anchored. `dst_tests` retains the local
        # name for compatibility with `_apply_patches(tests_dir, ...)`.
        dst_root = sut_root
        dst_tests = sut_root

        tests_needing = _tests_with_tbd(index)

        # Defensive fallback: if the index reports zero TBDs but worca-generated
        # files in the SUT still contain raw TBD_LOCATOR strings, the codegen
        # produced a layout the indexer didn't cover. Re-index the SUT
        # (filtered to worca files) so the agent receives the full picture.
        if not tests_needing:
            worca_files = [
                p for p in dst_root.rglob("*")
                if p.is_file()
                and Path(p.name).name.lower().startswith("worca")
                and p.suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".robot")
                and ".git" not in p.parts
            ]
            any_tbd = any(
                "TBD_LOCATOR" in p.read_text(encoding="utf-8", errors="ignore")
                for p in worca_files
            )
            if any_tbd:
                log.warning(
                    "step08.tbd_grep_fallback",
                    hint="index claimed 0 TBDs but raw TBD_LOCATOR found; re-indexing",
                )
                framework = index.get("framework", resolve_framework(None, dst_root))
                full_idx = index_tests(dst_root, framework=framework)
                index = _filter_index_to_worca(full_idx, sut_root).as_dict()
                tests_needing = _tests_with_tbd(index)

        resolution_path = out_dir / "locator-resolution.json"

        # If nothing to do, write a trivial resolution file and short-circuit.
        if not tests_needing:
            payload = _empty_resolution(index, sut_base_url)
            resolution_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            framework = index.get("framework", resolve_framework(None, dst_root))
            full_idx = index_tests(dst_root, framework=framework)
            re_idx = _filter_index_to_worca(full_idx, sut_root).as_dict()
            (out_dir / "tbd-index.json").write_text(
                json.dumps(re_idx, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return StepResult(
                success=True,
                status="completed",
                outputs=[resolution_path, out_dir / "tbd-index.json"],
                notes="no TBD markers; nothing to resolve",
            )

        # Stage tbd-index.json into the workdir so the agent can read it at
        # `./tbd-index.json` without needing add_dirs to the artifact dir.
        staged_index = wd / "tbd-index.json"
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

        # Surface auth + page-object files to the agent by listing their
        # absolute paths under the SUT — no copy-staging. The agent has
        # `add_dirs=[sut_root]` and reads each file directly.
        sut_inventory_path = ctx.workspace.step_dir(6) / "sut_inventory.json"
        active_module: dict | None = None
        if sut_inventory_path.exists():
            try:
                inv_dict = json.loads(sut_inventory_path.read_text(encoding="utf-8"))
                active = inv_dict.get("active_module")
                if active:
                    for mod in inv_dict.get("modules") or []:
                        if isinstance(mod, dict) and mod.get("name") == active:
                            active_module = mod
                            break
            except (OSError, json.JSONDecodeError):
                active_module = None
        relevant_sut_files = _auth_relevant_sut_files(active_module)

        # Remove stale Playwright MCP snapshots from prior runs so the
        # agent doesn't waste turns reading day-old AOM data. Also purge
        # stale page-snapshot-* files (HTML and AOM) from a previous
        # attempt — the graduated-snapshot policy requires fresh captures,
        # and the audit agent in 8b must not consume leftovers.
        import shutil as _shutil
        stale_mcp = wd / ".playwright-mcp"
        if stale_mcp.exists():
            _shutil.rmtree(stale_mcp, ignore_errors=True)
        for stale in wd.glob(_SNAPSHOT_GLOB):
            try:
                stale.unlink()
            except OSError:
                pass
        stale_comparison = wd / _DOM_COMPARISON_FILE
        if stale_comparison.exists():
            try:
                stale_comparison.unlink()
            except OSError:
                pass
        stale_clarifications = wd / _CLARIFICATIONS_FILE
        if stale_clarifications.exists():
            try:
                stale_clarifications.unlink()
            except OSError:
                pass

        result = await run_agent(
            agent,
            workdir=wd,
            inputs={},
            user_prompt=_build_user_prompt(
                index, sut_base_url,
                active_module=active_module,
                sut_root=sut_root,
                staged_files=relevant_sut_files,
            ),
            extra_paths=extras,
            add_dirs=[sut_root],
            timeout_s=self.timeout_s,
            step=8,
            max_turns=60,
            claude_md=claude_md if claude_md.exists() else None,
        )

        # Hard-fail when Playwright MCP didn't start. Without it, the agent
        # has no way to navigate the SUT or snapshot the DOM — its
        # `locator-resolution.json` (if it even writes one) will be empty,
        # and the warned-with-no-resolutions outcome confuses the user with
        # a meaningless "0 applied, 13 remaining" notes line. A clear error
        # tells the operator exactly what to retry.
        if "playwright" in result.mcp_servers_failed:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "Playwright MCP failed to start (server status=failed in "
                    "agent init). Step 8 cannot discover locators without it. "
                    "Common causes: stale npx cache (try `npx clear-npx-cache`), "
                    "corporate proxy blocking npm registry, or "
                    "`@playwright/mcp` not yet installed on this host. "
                    "Re-run Step 8 once `npx -y @playwright/mcp@latest --version` "
                    "succeeds from a shell."
                ),
            )

        transcript_path = wd / "transcript-00.jsonl"

        # MCP-usage gate. The agent can return success=True without ever
        # invoking Playwright MCP — e.g. by delegating to a Task/Agent
        # sub-agent (which runs in an isolated session and does NOT inherit
        # MCP servers, so it reports "Playwright MCP not connected" no
        # matter what), or by fabricating selectors from app-pattern guesses.
        # Either way, zero MCP calls means zero live DOM evidence. Fail
        # attempt 1 here so the debug agent co-runs on retry; a clear error
        # tells the operator what went wrong if retries are exhausted.
        mcp_calls = _count_playwright_mcp_calls(transcript_path)
        if mcp_calls == 0:
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"playwright-tester returned success but never invoked "
                    f"Playwright MCP — 0 `mcp__playwright__*` tool calls in "
                    f"transcript despite {len(tests_needing)} TBD locator(s) "
                    "awaiting resolution. Any 'applied:true' items in "
                    "locator-resolution.json are necessarily speculative "
                    "(no live DOM evidence). Common cause: the agent "
                    "delegated to a Task sub-agent (sub-agents do not inherit "
                    "MCP). See agents/playwright-tester.agent.md rule #7. If "
                    "this repeats, consider raising the playwright-tester "
                    "model tier in src/worca_t/agent_models.yaml."
                ),
            )

        # Scope guard. The agent must only modify files listed in
        # tbd-index.json (those carry the TBD_LOCATOR markers). Any other
        # SUT edits — conftest.py, fixtures, auth pages, .env, CI YAML, new
        # files — are out of scope and get reverted before failing. Past
        # failure mode: a Sonnet-4.6 playwright-tester run wrote new SUT
        # helpers, edited sign_in_page.py + conftest + .env, and corrupted
        # the SUT while still claiming MCP was unavailable. Cleaning up
        # those writes here keeps the SUT viable for the retry / step 9.
        allowed_files = {Path(t["file"]).as_posix() for t in tests_needing}
        out_of_scope, untracked = _agent_sut_writes_outside_scope(
            sut_root, allowed_files,
        )
        if out_of_scope or untracked:
            _revert_agent_writes(sut_root, out_of_scope, untracked)
            sample_mod = sorted(out_of_scope)[:5]
            sample_new = sorted(untracked)[:5]
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"playwright-tester mutated {len(out_of_scope)} tracked + "
                    f"{len(untracked)} new SUT file(s) outside its allowed "
                    f"scope. Allowed: only the {len(allowed_files)} "
                    f"TBD-bearing file(s) in tbd-index.json. "
                    f"Out-of-scope modified: {sample_mod}"
                    f"{'…' if len(out_of_scope) > 5 else ''}. "
                    f"Untracked: {sample_new}"
                    f"{'…' if len(untracked) > 5 else ''}. "
                    "All bad writes reverted. See "
                    "agents/playwright-tester.agent.md rule #8."
                ),
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

        # HITL splice — if the agent wrote ./clarifications.md for TBDs it
        # could not locate, prompt the user (interactive TTY only) and patch
        # their selectors / spec-gap confirmations into the payload BEFORE
        # applying file patches. Non-TTY / --no-hitl returns 0 spliced and
        # the items stay `applied: false`, falling through to the apply-rate
        # gate as today.
        hitl_spliced = _hitl_resolve_unresolvable(wd=wd, payload=payload)
        if hitl_spliced:
            # Persist the spliced payload so audit + downstream see it.
            produced.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        # Snapshot-policy audit (warning-only). Inspects persisted snapshot
        # files in the workdir against the payload's `snapshot_source` /
        # `fallback_reason` annotations. AOM-first: raw-DOM captures must
        # be justified per-item; un-justified raw captures and zero-AOM
        # captures are advisory violations.
        try:
            snapshot_violations = _audit_snapshot_policy(wd, payload)
        except Exception:  # noqa: BLE001 - audit is best-effort
            snapshot_violations = []
        if snapshot_violations:
            log.warning(
                "step08.snapshot_policy_violation",
                count=len(snapshot_violations),
                violations=snapshot_violations,
                hint="playwright-tester departed from the AOM-first snapshot "
                     "policy (AOM via browser_snapshot as the default; raw-DOM "
                     "only as a justified per-item fallback). The agent prompt "
                     "may need sharpening; data was still produced so the step "
                     "is not failed on this signal.",
            )

        applied_items = _apply_patches(dst_tests, resolutions)

        # Persist the post-patch payload back into the workdir so the audit
        # agent in 8b reads the same item state the pipeline will gate on.
        payload.setdefault("base_url", sut_base_url)
        produced.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # ---- Step 8b: polyglot-test-fixer in audit-only mode ----
        # Reads the persisted snapshots + locator-resolution + tbd-index
        # (already in wd) plus the codegen test files (via add_dirs=[sut_root])
        # and emits dom-comparison.json. Honest ghost/duplicate verdicts get
        # stamped into payload items and excused from the apply-rate gate.
        snapshot_files = sorted(
            p.name for p in wd.glob(_SNAPSHOT_GLOB) if p.is_file()
        )
        fixer_agent = package_resource_root() / "agents" / "polyglot-test-fixer.agent.md"
        comparison_path = wd / _DOM_COMPARISON_FILE
        comparison: dict | None = None
        if fixer_agent.exists():
            audit_model = model_for_agent("polyglot-test-fixer-audit")
            audit_result = await run_agent(
                fixer_agent,
                workdir=wd,
                inputs={},
                user_prompt=_build_comparison_prompt(
                    index, sut_root, snapshot_filenames=snapshot_files,
                ),
                extra_paths=[],
                add_dirs=[sut_root],
                timeout_s=min((self.timeout_s or 1800) // 3, 600),
                step=8,
                max_turns=30,
                model=audit_model,
                claude_md=claude_md if claude_md.exists() else None,
            )
            if audit_result.success and comparison_path.exists():
                try:
                    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    log.warning(
                        "step08.comparison_invalid_json",
                        error=str(e),
                        hint="dom-comparison.json from fixer-audit is not valid "
                             "JSON; ghost/duplicate excusal will be skipped.",
                    )
                else:
                    ok_cmp_schema, cmp_err = is_valid(comparison, "dom-comparison")
                    if not ok_cmp_schema:
                        log.warning("step08.comparison_schema_invalid", error=cmp_err)
                    # All-unevaluated guard. If the auditor returned
                    # `unevaluated` for every expected constant, it had no
                    # usable snapshots to compare against — which only happens
                    # when the playwright-tester didn't persist any (or
                    # persisted only empty/unreadable ones). Without DOM
                    # evidence, the apply-rate gate downstream could
                    # rubber-stamp a fabricated all-applied=true resolution
                    # set; fail here instead so the operator sees the real
                    # cause. Note: ghost+duplicate are already excluded from
                    # should_exist_total per the schema, so reaching this
                    # branch with should_exist > 0 means the agent claimed
                    # constants exist but provided nothing to verify them.
                    cmp_summary = comparison.get("summary") or {}
                    unevaluated = cmp_summary.get("unevaluated") or 0
                    should_exist = cmp_summary.get("should_exist_total") or 0
                    if should_exist > 0 and unevaluated >= should_exist:
                        return StepResult(
                            success=False,
                            status="failed",
                            outputs=[produced],
                            error=(
                                f"DOM-truth audit (Step 8b) returned "
                                f"'unevaluated' for all {should_exist} expected "
                                "constants — no usable page snapshots existed "
                                "to verify the playwright-tester's "
                                "resolutions. Any 'applied:true' items are "
                                "unverifiable and likely speculative. Check "
                                f"{wd}/page-snapshot-*.{{html,json}} (probably "
                                "absent) and the playwright-tester transcript "
                                "for missing browser_evaluate / "
                                "browser_snapshot calls."
                            ),
                        )
                    payload = _apply_comparison_verdict(payload, comparison)
                    # _apply_comparison_verdict may have flipped items from
                    # applied=True to applied=False (ghost/duplicate); refresh
                    # the in-memory applied_items list off the mutated payload
                    # so totals downstream reflect the verdict.
                    applied_items = [
                        item
                        for resolution in payload.get("resolutions") or []
                        for item in resolution.get("items") or []
                    ]
            else:
                log.warning(
                    "step08.comparison_agent_failed",
                    success=audit_result.success,
                    has_output=comparison_path.exists(),
                    error=audit_result.error,
                    hint="proceeding without comparison verdicts; ghost/duplicate "
                         "items will count against the apply-rate gate as before.",
                )
        else:
            log.warning(
                "step08.fixer_agent_missing",
                path=str(fixer_agent),
                hint="polyglot-test-fixer.agent.md not found; skipping 8b audit.",
            )

        applied_count = sum(1 for it in applied_items if it.get("applied"))
        skipped_count = sum(1 for it in applied_items if not it.get("applied"))
        excused_count = sum(
            1
            for it in applied_items
            if it.get("comparison_verdict") in ("ghost", "duplicate")
        )

        payload["totals"] = {
            "tests_with_tbd": len(tests_needing),
            "items": len(applied_items),
            "applied": applied_count,
            "skipped": skipped_count,
            "excused": excused_count,
        }

        # Quality signals (advisory — never fail the step on these):
        #   - duplicates: applied items that share a selector with another
        #     in the same file. Surfaces codegen over-abstraction and
        #     resolver collapse alike.
        #   - low_confidence_masks: low-confidence applied items whose
        #     selector is also reused — the silent spec-gap mask signature.
        duplicates = _detect_duplicate_replacements(payload, tests_dir=dst_tests)
        masks = _detect_low_confidence_masks(payload, tests_dir=dst_tests)
        payload["duplicates"] = duplicates
        payload["low_confidence_masks"] = masks
        if duplicates:
            log.warning(
                "step08.duplicate_replacements",
                count=len(duplicates),
                files=sorted({d["file"] for d in duplicates}),
                hint="multiple TBDs resolved to identical selectors; see "
                     "locator-resolution.json#/duplicates",
            )
        if masks:
            log.warning(
                "step08.low_confidence_masks",
                count=len(masks),
                files=sorted({m["file"] for m in masks}),
                hint="low-confidence applied items reuse selectors with "
                     "other TBDs — suspected silently-masked spec gaps. "
                     "See locator-resolution.json#/low_confidence_masks",
            )

        resolution_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok_schema, schema_err = is_valid(payload, "locator-resolution")
        if not ok_schema:
            log.warning("step08.schema_invalid", error=schema_err)

        # Re-index the patched files in the SUT; filter to worca-only so
        # SUT-native tests don't contaminate the violation report.
        framework = index.get("framework", resolve_framework(None, dst_tests))
        full_reindex = index_tests(dst_tests, framework=framework)
        reindex = _filter_index_to_worca(full_reindex, sut_root)
        (out_dir / "tbd-index.json").write_text(
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
        if excused_count:
            ghost_count = sum(
                1 for it in applied_items
                if it.get("comparison_verdict") == "ghost"
            )
            duplicate_verdict_count = sum(
                1 for it in applied_items
                if it.get("comparison_verdict") == "duplicate"
            )
            notes += f" ghosts={ghost_count} duplicates={duplicate_verdict_count}"
        elif duplicates:
            # Fall back to the heuristic duplicate detector when the comparison
            # agent didn't run (e.g. fixer-audit failed).
            notes += f" duplicates={len(duplicates)}"
        if masks:
            notes += f" suspected_spec_gaps={len(masks)}"
        if snapshot_violations:
            notes += f" snapshot_policy_violations={len(snapshot_violations)}"
        if remaining_tbd > 0:
            notes += "; some markers unresolved"
        if not ok_schema:
            notes += f"; schema_warning={schema_err}"

        # Status semantics — partial success requires ≥90% of TBD items
        # resolved. Below that threshold, too many tests will run against
        # unpatched locators and produce noise; halt and let the operator
        # investigate. The 90% threshold was chosen by the user to distinguish
        # "a couple of edge-case locators missed" from "the agent couldn't
        # navigate the page at all."
        #
        # As of Step 8b (dom-comparison audit), items the auditor marked as
        # `ghost` (element doesn't exist) or `duplicate` (same element as
        # another TBD) are EXCUSED from the denominator — those aren't
        # resolver failures, they're upstream codegen over-abstractions and
        # spec/implementation gaps. The numerator stays `applied`; the
        # denominator becomes "items the auditor thinks should resolve."
        total_items = applied_count + skipped_count
        gate_denominator = total_items - excused_count
        apply_rate = (
            applied_count / gate_denominator if gate_denominator > 0 else 1.0
        )
        _MIN_APPLY_RATE = 0.9

        if gate_denominator > 0 and apply_rate < _MIN_APPLY_RATE:
            return StepResult(
                success=False,
                status="failed",
                outputs=[resolution_path, out_dir / "tbd-index.json"],
                error=(
                    f"locator-resolution applied {applied_count}/{gate_denominator} "
                    f"non-excused patches ({apply_rate:.0%}), below the "
                    f"{_MIN_APPLY_RATE:.0%} threshold "
                    f"(excused {excused_count} ghost/duplicate items per "
                    f"dom-comparison.json). Downstream steps would run against "
                    f"unpatched tests and produce noise. Inspect locator-resolution.json "
                    f"`skip_reason` and `comparison_verdict` fields to diagnose "
                    f"(common causes: agent emitted wrong file path, rejected "
                    f"xpath replacement, or token mismatch)."
                ),
                notes=notes,
            )

        # Commit the patched files to the worca-t branch. No-op when no
        # files actually changed (rare; usually means every patch missed).
        commit_msg = (
            f"{applied_count}/{gate_denominator} TBDs resolved"
            if gate_denominator else "no TBDs"
        )
        if excused_count:
            commit_msg += f" ({excused_count} excused: ghost/duplicate)"
        sha = commit_step(
            sut_root, self.number, self.name,
            message_detail=commit_msg,
        )
        if sha:
            notes += f" commit={sha}"

        # Publish dom-comparison.json to the artifact dir so the HTML report
        # and downstream steps can read the auditor's verdicts.
        published_comparison: Path | None = None
        if comparison is not None:
            published_comparison = out_dir / _DOM_COMPARISON_FILE
            published_comparison.write_text(
                json.dumps(comparison, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        status = "warned" if remaining_tbd > 0 or not ok_schema else "completed"
        outputs_list: list[Path] = [resolution_path, out_dir / "tbd-index.json"]
        if published_comparison is not None:
            outputs_list.append(published_comparison)
        return StepResult(
            success=True,
            status=status,
            outputs=outputs_list,
            notes=notes,
        )


# Internal helpers exposed for unit testing.
__all__ = [
    "LocatorResolutionStep",
    "_agent_sut_writes_outside_scope",
    "_apply_comparison_verdict",
    "_apply_patches",
    "_audit_snapshot_policy",
    "_build_comparison_prompt",
    "_build_user_prompt",
    "_classify_item",
    "_count_playwright_mcp_calls",
    "_detect_duplicate_replacements",
    "_detect_low_confidence_masks",
    "_ensure_files_for",
    "_find_item_for_clarification",
    "_hitl_resolve_unresolvable",
    "_infer_strategy",
    "_is_assignment_line",
    "_is_spec_gap_answer",
    "_is_xpath_replacement",
    "_parse_clarification_header",
    "_rank_strategy",
    "_revert_agent_writes",
    "_tests_with_tbd",
]


_PRIORITY_RE = re.compile("|".join(_PRIORITY))  # kept for future helpers
