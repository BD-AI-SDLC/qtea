"""Unit tests for Step 9's Playwright trace URL extractor."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from qtea.steps.s09.trace_parser import extract_failure_url, find_trace_path
from qtea.test_runner import TestRunEntry


def _write_trace_zip(dest: Path, events: list[dict]) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(
            "trace.trace",
            "\n".join(json.dumps(e) for e in events),
        )
    return dest


# ---------------------------------------------------------------------------
# extract_failure_url
# ---------------------------------------------------------------------------


def test_extract_returns_last_frame_snapshot_url(tmp_path: Path) -> None:
    trace = _write_trace_zip(
        tmp_path / "trace.zip",
        [
            {"type": "context-options"},
            {"type": "frame-snapshot", "snapshot": {"frameUrl": "https://app.test/login"}},
            {"type": "before", "apiName": "page.goto", "params": {"url": "https://app.test/home"}},
            {"type": "frame-snapshot", "snapshot": {"frameUrl": "https://app.test/home"}},
            {"type": "frame-snapshot", "snapshot": {"frameUrl": "https://app.test/settings"}},
            {"type": "after", "error": {"message": "locator timeout"}},
        ],
    )
    assert extract_failure_url(trace) == "https://app.test/settings"


def test_extract_falls_back_to_navigation_when_no_snapshot(tmp_path: Path) -> None:
    trace = _write_trace_zip(
        tmp_path / "trace.zip",
        [
            {"type": "context-options"},
            {"type": "before", "apiName": "page.goto", "params": {"url": "https://app.test/x"}},
            {"type": "before", "apiName": "page.goto", "params": {"url": "https://app.test/y"}},
            {"type": "after", "error": {"message": "crashed early"}},
        ],
    )
    assert extract_failure_url(trace) == "https://app.test/y"


def test_extract_ignores_about_blank_snapshots(tmp_path: Path) -> None:
    trace = _write_trace_zip(
        tmp_path / "trace.zip",
        [
            {"type": "frame-snapshot", "snapshot": {"frameUrl": "https://app.test/real"}},
            {"type": "frame-snapshot", "snapshot": {"frameUrl": "about:blank"}},
        ],
    )
    assert extract_failure_url(trace) == "https://app.test/real"


def test_extract_missing_file_returns_none(tmp_path: Path) -> None:
    assert extract_failure_url(tmp_path / "does-not-exist.zip") is None


def test_extract_corrupt_zip_returns_none(tmp_path: Path) -> None:
    corrupt = tmp_path / "trace.zip"
    corrupt.write_bytes(b"not a zip file")
    assert extract_failure_url(corrupt) is None


def test_extract_no_trace_entry_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "trace.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "no trace here")
    assert extract_failure_url(empty) is None


def test_extract_no_urls_at_all_returns_none(tmp_path: Path) -> None:
    trace = _write_trace_zip(
        tmp_path / "trace.zip",
        [{"type": "context-options"}, {"type": "console", "text": "hi"}],
    )
    assert extract_failure_url(trace) is None


def test_extract_tolerates_malformed_jsonl_lines(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace_path, "w") as zf:
        zf.writestr(
            "trace.trace",
            "not-json-line\n"
            + json.dumps({"type": "frame-snapshot", "snapshot": {"frameUrl": "https://app.test/ok"}})
            + "\n"
            + "{corrupt",
        )
    assert extract_failure_url(trace_path) == "https://app.test/ok"


# ---------------------------------------------------------------------------
# find_trace_path
# ---------------------------------------------------------------------------


def _entry(**kw) -> TestRunEntry:
    defaults = {
        "id": "T-x", "name": "test_x", "file": "tests/test_x.py", "status": "failed",
    }
    defaults.update(kw)
    return TestRunEntry(**defaults)


def test_find_prefers_attachment_path(tmp_path: Path) -> None:
    trace = tmp_path / "attached-trace.zip"
    trace.write_bytes(b"placeholder")
    entry = _entry(attachments=[{"type": "trace", "path": str(trace)}])
    assert find_trace_path(entry, tmp_path) == trace


def test_find_skips_missing_attachment(tmp_path: Path) -> None:
    entry = _entry(attachments=[{"type": "trace", "path": str(tmp_path / "gone.zip")}])
    assert find_trace_path(entry, tmp_path) is None


def test_find_globs_pytest_playwright_convention(tmp_path: Path) -> None:
    # pytest-playwright puts traces at test-results/<slug>/trace.zip
    trace_dir = tmp_path / "test-results" / "test_login_py_test_bad_password_chromium"
    trace_dir.mkdir(parents=True)
    trace = trace_dir / "trace.zip"
    trace.write_bytes(b"placeholder")
    entry = _entry(id="T-t", name="test_bad_password", file="tests/test_login.py")
    assert find_trace_path(entry, tmp_path) == trace


def test_find_returns_none_on_ambiguous_glob(tmp_path: Path) -> None:
    a = tmp_path / "test-results" / "test_login_test_x_chromium" / "trace.zip"
    b = tmp_path / "test-results" / "test_login_test_x_firefox" / "trace.zip"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"placeholder")
    entry = _entry(id="T-t", name="test_x", file="tests/test_login.py")
    assert find_trace_path(entry, tmp_path) is None


def test_find_returns_none_when_no_results_dir(tmp_path: Path) -> None:
    entry = _entry()
    assert find_trace_path(entry, tmp_path) is None
