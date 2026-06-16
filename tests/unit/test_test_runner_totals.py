"""Unit tests for Fix 7: tests vs infrastructure_errors split in RunResult.totals.

Run 20260611-184450 reported `tests=2 errors=2` when zero real tests had
actually run — both entries were synthetic T-runner-failure artifacts from
a pytest collection ImportError. The totals split makes the masked-by-infra
case impossible to misread.
"""

from __future__ import annotations

from worca_t.test_runner import RunResult, TestRunEntry


def _entry(
    *, status: str = "passed", runner_failure: dict | None = None,
    id_: str = "T-1",
) -> TestRunEntry:
    return TestRunEntry(
        id=id_, name=id_, file="x.py", status=status,
        runner_failure=runner_failure,
    )


def _run(results: list[TestRunEntry]) -> RunResult:
    return RunResult(
        framework="pytest", command="pytest", cwd=".",
        started_at="t0", finished_at="t1", duration_s=1.0,
        exit_code=0, results=results,
    )


def test_totals_count_real_tests_only():
    r = _run([
        _entry(status="passed", id_="T-1"),
        _entry(status="failed", id_="T-2"),
        _entry(status="skipped", id_="T-3"),
        _entry(status="error", id_="T-4"),
    ])
    t = r.totals
    assert t["tests"] == 4
    assert t["passed"] == 1
    assert t["failed"] == 1
    assert t["skipped"] == 1
    assert t["errors"] == 1
    assert t["infrastructure_errors"] == 0


def test_totals_exclude_synthetic_runner_failure():
    """The bug we're fixing: synthetic T-runner-failure must NOT count as a test."""
    r = _run([
        _entry(
            status="error", id_="T-runner-failure",
            runner_failure={"kind": "missing_module", "module": "playwright"},
        ),
        _entry(
            status="error", id_="T-pytest-internal",
            runner_failure={"kind": "collection_error"},
        ),
    ])
    t = r.totals
    assert t["tests"] == 0
    assert t["infrastructure_errors"] == 2
    assert t["passed"] == 0
    assert t["failed"] == 0
    assert t["errors"] == 0


def test_totals_mixed_real_and_infra():
    r = _run([
        _entry(status="passed", id_="T-1"),
        _entry(status="failed", id_="T-2"),
        _entry(
            status="error", id_="T-runner-failure",
            runner_failure={"kind": "collection_error"},
        ),
    ])
    t = r.totals
    assert t["tests"] == 2
    assert t["passed"] == 1
    assert t["failed"] == 1
    assert t["infrastructure_errors"] == 1
    assert t["errors"] == 0


def test_totals_empty_results():
    r = _run([])
    t = r.totals
    assert t == {
        "tests": 0, "passed": 0, "failed": 0, "skipped": 0,
        "errors": 0, "infrastructure_errors": 0,
    }


# --- exit code 3 internal error tests --------------------------------------

def test_exit_code_3_entries_tagged_with_runner_failure_are_infrastructure():
    """Entries from exit code 3 tagged with runner_failure must count as
    infrastructure_errors, not as regular test errors."""
    rf = {"kind": "internal_error", "module": None,
          "hint": "pytest internal error", "summary": "internal error"}
    r = _run([
        _entry(status="error", id_="T-pytest-internal", runner_failure=rf),
        _entry(status="error", id_="T-runner-failure", runner_failure=rf),
    ])
    t = r.totals
    assert t["infrastructure_errors"] == 2
    assert t["tests"] == 0
    assert t["errors"] == 0


def test_exit_code_3_with_real_tests_preserves_results():
    """When exit code 3 has some real passed/failed tests alongside
    errors, the real tests should still count normally."""
    r = _run([
        _entry(status="passed", id_="T-1"),
        _entry(status="error", id_="T-pytest-internal"),
    ])
    t = r.totals
    assert t["passed"] == 1
    assert t["errors"] == 1
    assert t["tests"] == 2
