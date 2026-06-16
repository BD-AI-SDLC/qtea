"""Unit tests for Fix 10: Step 4 strategy parser reserved-header filter.

Run 20260611-184450 strategy.json contained a noise `TC-test-cases` entry
because the parser matched the literal `## Test Cases` section header as
a test case. The fix adds a reserved-titles list and a body-marker check.
"""

from __future__ import annotations

from worca_t.md_parser import Section
from worca_t.steps.s04_strategy import _looks_like_test_case, _project_strategy


def _section(title: str, content: str = "", level: int = 2) -> Section:
    s = Section(level=level, title=title)
    s.content = content
    return s


def test_section_header_test_cases_rejected():
    sec = _section("Test Cases", content="Some intro text\n")
    assert _looks_like_test_case(sec) is False


def test_section_header_scope_rejected():
    sec = _section("Scope", content="What is in/out of scope.\n")
    assert _looks_like_test_case(sec) is False


def test_section_header_assumptions_rejected():
    sec = _section("Assumptions", content="System assumptions.\n")
    assert _looks_like_test_case(sec) is False


def test_tc_id_heading_accepted():
    sec = _section(
        "TC-login-001: User signs in", content="**Type:** Smoke\n**Priority:** P0\n",
    )
    assert _looks_like_test_case(sec) is True


def test_tc_id_heading_accepted_even_without_body_markers():
    # When the TC ID is explicit, accept it — the body might be terse.
    sec = _section("TC-edge-001", content="see linked spec\n")
    assert _looks_like_test_case(sec) is True


def test_generic_test_case_heading_requires_body_markers():
    # No TC ID. "Test Case" prefix matches the permissive regex,
    # but without **Type/Priority/Steps/Expected** the parser should reject.
    sec = _section("Test Case overview", content="just an intro\n")
    assert _looks_like_test_case(sec) is False


def test_generic_test_case_heading_with_body_markers_accepted():
    sec = _section(
        "Test Case: login flow",
        content="**Priority:** P1\n**Steps:**\n1. open\n",
    )
    assert _looks_like_test_case(sec) is True


def test_scenario_heading_with_body_markers_accepted():
    sec = _section(
        "Scenario A",
        content="**Type:** integration\n**Expected:** 200 OK\n",
    )
    assert _looks_like_test_case(sec) is True


def test_project_strategy_excludes_test_cases_header():
    """End-to-end: a realistic strategy doc must not emit a TC-test-cases entry."""
    md = """\
# Test Strategy

## Scope

In scope: feature X.

## Test Cases

#### TC-001: User can sign in

- **Type:** Smoke
- **Priority:** P0
- **Steps:**
  1. Open login.
  2. Submit credentials.
- **Expected:** Session token issued.

#### TC-002: Sign-in fails for invalid creds

- **Type:** Negative
- **Priority:** P1
- **Steps:**
  1. Submit bad creds.
- **Expected:** Error banner shown.

## Assumptions

- SSO is up.
"""
    parsed = _project_strategy(md)
    ids = sorted(tc["id"] for tc in parsed["test_cases"])
    assert ids == ["TC-001", "TC-002"]
    assert all(tc["id"].lower() != "tc-test-cases" for tc in parsed["test_cases"])
    assert all(tc["id"].lower() != "tc-scope" for tc in parsed["test_cases"])
