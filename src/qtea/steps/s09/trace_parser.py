"""Playwright trace URL extraction for the heal loop.

When a test fails in Step 9's attempt 1, we know the traceback but not the
page URL at the moment of failure. The heal agent, having Playwright MCP
pre-warmed, can jump straight to the failing page if we tell it where —
otherwise it wastes turns navigating from the base URL or reconstructing
the location from the stack.

Playwright records a `trace.zip` for every failing test when tracing is
enabled (via `--tracing=retain-on-failure` for Python pytest-playwright or
`trace: 'retain-on-failure'` in the TS config). The archive contains a
`trace.trace` JSONL where each `frame-snapshot` event carries the frame's
URL — the last snapshot in the trace is the state at the point of failure.

This module is best-effort: any parse failure returns None and the heal
loop proceeds with its current behavior. Nothing here raises.

Language-agnostic: the trace format is identical across every Playwright
binding (Python, TS, Java, .NET, etc.).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.test_runner import TestRunEntry

log = get_logger(__name__)


_TRACE_ENTRY_NAMES = ("trace.trace", "test.trace")


def extract_failure_url(trace_path: Path) -> str | None:
    """Return the URL at the point of failure, or None if not derivable.

    Reads `trace.trace` JSONL from the Playwright trace archive and returns
    the frame URL from the last `frame-snapshot` event. If no snapshot
    events are present, falls back to the last successful navigation
    (`before` event with `apiName == "page.goto"`).

    Never raises — corrupt archives, missing files, and unrecognized
    schema shapes all return None with a debug log.
    """
    try:
        if not trace_path.exists():
            log.debug("step09.trace_parser.missing", path=str(trace_path))
            return None
        with zipfile.ZipFile(trace_path) as zf:
            trace_name = next(
                (n for n in _TRACE_ENTRY_NAMES if n in zf.namelist()),
                None,
            )
            if trace_name is None:
                log.debug(
                    "step09.trace_parser.no_trace_entry",
                    path=str(trace_path),
                    contents=zf.namelist()[:10],
                )
                return None
            raw = zf.read(trace_name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError) as exc:
        log.debug(
            "step09.trace_parser.read_failed",
            path=str(trace_path),
            error=repr(exc),
        )
        return None

    last_snapshot_url: str | None = None
    last_navigation_url: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype in ("frame-snapshot", "snapshot"):
            snap = event.get("snapshot") or {}
            url = snap.get("frameUrl") or snap.get("url")
            if isinstance(url, str) and url and not url.startswith("about:"):
                last_snapshot_url = url
        elif etype == "before":
            api = event.get("apiName") or event.get("method") or ""
            if api in ("page.goto", "Page.goto", "goto"):
                params = event.get("params") or {}
                url = params.get("url")
                if isinstance(url, str) and url:
                    last_navigation_url = url

    return last_snapshot_url or last_navigation_url


def find_trace_path(entry: TestRunEntry, sut_root: Path) -> Path | None:
    """Locate the trace.zip for a failing test.

    Prefers the runner's own attachment metadata (`entry.attachments`),
    which the @playwright/test JSON reporter populates directly. Falls
    back to a bounded search under `<sut>/test-results/` for Python
    pytest-playwright, which emits traces to that convention path but
    does not surface them via JUnit XML.

    Returns None on ambiguity (multiple candidate traces match) rather
    than guessing.
    """
    for att in entry.attachments or []:
        if att.get("type") != "trace":
            continue
        path_str = att.get("path")
        if not path_str:
            continue
        candidate = Path(path_str)
        if candidate.exists():
            return candidate

    results_dir = sut_root / "test-results"
    if not results_dir.exists():
        return None

    slug_tokens = _slug_tokens(entry.file, entry.name)
    if not slug_tokens:
        return None

    matches: list[Path] = []
    try:
        for trace in results_dir.rglob("trace.zip"):
            parent = trace.parent.name.lower()
            if all(tok in parent for tok in slug_tokens):
                matches.append(trace)
    except OSError as exc:
        log.debug(
            "step09.trace_parser.glob_failed",
            root=str(results_dir),
            error=repr(exc),
        )
        return None

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.debug(
            "step09.trace_parser.ambiguous",
            candidates=[str(m) for m in matches],
            test_id=entry.id,
        )
    return None


def _slug_tokens(file_rel: str, name: str) -> list[str]:
    """Split a test's file basename + name into lowercase tokens.

    Used to match a failing test against the parent directory name of a
    trace file — pytest-playwright names those dirs like
    `test_login_py_test_bad_password_chromium/`, so token-inclusion is a
    more forgiving match than an exact slug.
    """
    tokens: list[str] = []
    if file_rel:
        stem = Path(file_rel).stem.lower()
        tokens.extend(t for t in _tokenize(stem) if t)
    if name:
        tokens.extend(t for t in _tokenize(name.lower()) if t)
    return [t for t in tokens if len(t) >= 3]


def _tokenize(s: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ch in s:
        if ch.isalnum():
            buf.append(ch)
        elif buf:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


__all__ = [
    "extract_failure_url",
    "find_trace_path",
]
