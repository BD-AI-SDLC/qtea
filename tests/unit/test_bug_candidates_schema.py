"""Regression tests for `bug-candidates.schema.json` wiring.

`bug-candidates.json` had a schema (`schemas/bug-candidates.schema.json`)
with zero validation call sites — a schema/code drift could go undetected
indefinitely. This wires a non-blocking `is_valid()` check at the actual
write site (`s09_execute.py`, right after `bug_path.write_text(...)`),
mirroring the existing `run-results` pattern in the same function.

Validation is non-blocking (log a warning, still write the file) because
bug-candidates.json carries real user-facing bug reports — unlike a shadow
judge verdict, dropping it on a schema mismatch would silently hide a real
test failure from Step 10, which is worse than surfacing a stale schema.
"""

from __future__ import annotations

from qtea.schemas import is_valid
from qtea.steps.s09.bug_candidates_ext import (
    _bug_candidates_for_dev_pool_drift,
    _bug_candidates_for_unresolvable_tbds,
)
from qtea.steps.s09.failure_class import _build_bug_candidates
from qtea.test_runner import TestRunEntry


def test_build_bug_candidates_output_matches_schema():
    failing = [
        TestRunEntry(
            id="t1", name="test_login", file="tests/test_login.py",
            status="failed", message="AssertionError: boom", traceback="Traceback...",
            attachments=[{"path": "shot.png", "type": "screenshot"}],
        ),
        TestRunEntry(
            id="t2", name="test_logout", file="tests/test_logout.py",
            status="error", message=None, traceback=None,
        ),
    ]
    payload = _build_bug_candidates(failing)
    ok, err = is_valid(payload, "bug-candidates")
    assert ok, err


def test_build_bug_candidates_empty_list_matches_schema():
    payload = _build_bug_candidates([])
    ok, err = is_valid(payload, "bug-candidates")
    assert ok, err


def test_dev_pool_drift_candidate_matches_schema(tmp_path):
    """`test_file` can legitimately be None (fallback from
    `PYTEST_CURRENT_TEST` parsing in the runtime template) — the schema
    must accept a null `file`, not just a missing one."""
    log_path = tmp_path / "dev-pool-quarantine.jsonl"
    log_path.write_text(
        '{"constant_name": "LOGIN_BTN", "intent": "log in button", '
        '"stale_selector": "#old", "exception": "TimeoutError", "ts": null, '
        '"test_file": null, "page_url": "https://x/login"}\n',
        encoding="utf-8",
    )
    candidates = _bug_candidates_for_dev_pool_drift(log_path)
    assert len(candidates) == 1
    payload = {"candidates": candidates}
    ok, err = is_valid(payload, "bug-candidates")
    assert ok, err


def test_unresolvable_tbd_candidate_matches_schema():
    remaining = [{
        "constant_name": "SUBMIT_BTN", "intent": "submit button",
        "test_file": None, "page_url": "https://x/checkout",
    }]
    candidates = _bug_candidates_for_unresolvable_tbds(remaining)
    payload = {"candidates": candidates}
    ok, err = is_valid(payload, "bug-candidates")
    assert ok, err


def test_is_valid_rejects_payload_missing_required_candidate_field():
    malformed = {"candidates": [{
        "id": "BC-1", "test_id": "t1",
        # "title" and "file" intentionally omitted (schema-required)
    }]}
    ok, err = is_valid(malformed, "bug-candidates")
    assert not ok
    assert err
