"""Unit tests for the human-in-the-loop module."""

from __future__ import annotations

from pathlib import Path

from worca_t.hitl import (
    Question,
    extract_questions,
    format_answers_md,
    has_not_ready_verdict,
    question_key,
    write_answers_file,
)


def test_extract_clarifications_finds_tagged_lines():
    md = """\
# Spec

The login button color is [CLARIFICATION NEEDED: should this be blue or green?].
Another line.
The retry policy is [CLARIFICATION NEEDED: exponential or linear?].
"""
    qs = extract_questions(md)
    assert len(qs) == 2
    assert all(q.kind == "clarification" for q in qs)
    assert "blue or green" in qs[0].prompt_text
    assert "exponential or linear" in qs[1].prompt_text


def test_extract_blockers_from_table():
    md = """\
# Plan

## Blockers

| Blocker | Affected TCs | Severity |
|---------|--------------|----------|
| SSO config unavailable | TC-AUTH-005 | high |
| Test data missing | TC-PAY-002 | critical |
"""
    qs = extract_questions(md)
    blockers = [q for q in qs if q.kind == "blocker"]
    assert len(blockers) == 2
    assert "SSO config unavailable" in blockers[0].prompt_text
    assert "Test data missing" in blockers[1].prompt_text


def test_extract_blockers_skips_no_blockers_marker():
    md = """\
# Plan

## Blockers

No blockers identified.
"""
    qs = extract_questions(md)
    assert [q for q in qs if q.kind == "blocker"] == []


def test_extract_open_questions_from_section():
    md = """\
# Plan

## Open Questions

- What is the expected timeout for the API call?
- Should we support partial refunds?
"""
    qs = extract_questions(md)
    opens = [q for q in qs if q.kind == "open_question"]
    assert len(opens) == 2
    assert "timeout" in opens[0].prompt_text
    assert "partial refunds" in opens[1].prompt_text


def test_extract_questions_deduplicates_identical_prompts():
    md = """\
# Spec

[CLARIFICATION NEEDED: same question]
Other line.
[CLARIFICATION NEEDED: same question]
"""
    qs = extract_questions(md)
    assert len(qs) == 1


def test_extract_questions_returns_empty_for_clean_doc():
    md = """\
# Spec

This is clean.

## Acceptance Criteria

- AC-1: behaves as expected
"""
    assert extract_questions(md) == []


def test_has_not_ready_verdict_detects_marker():
    assert has_not_ready_verdict("**Readiness:** NOT READY (2 blockers)")
    assert not has_not_ready_verdict("**Readiness:** READY")


def test_format_answers_md_includes_each_qa_pair():
    qs = [
        Question(id="CLAR-01", kind="clarification", prompt_text="blue or green?"),
        Question(id="BLOCK-01", kind="blocker", prompt_text="resolve SSO?", context="row context"),
    ]
    answers = {"CLAR-01": "blue", "BLOCK-01": "use mock IdP"}
    md = format_answers_md(qs, answers)
    assert "## CLAR-01" in md
    assert "## BLOCK-01" in md
    assert "blue" in md
    assert "use mock IdP" in md
    assert "row context" in md


def test_format_answers_md_handles_skipped_answers():
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    md = format_answers_md(qs, {})
    assert "No items were answered or skipped" in md


def test_write_answers_file_writes_to_workdir(tmp_path: Path):
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    path = write_answers_file(tmp_path, qs, {"CLAR-01": "answer"})
    assert path.exists()
    assert path.name == "user-answers.md"
    assert "answer" in path.read_text(encoding="utf-8")


def test_question_key_normalizes_whitespace_and_case():
    a = Question(id="CLAR-01", kind="clarification", prompt_text="  Which IdP?  ")
    b = Question(id="CLAR-99", kind="clarification", prompt_text="which idp?")
    assert question_key(a) == question_key(b)


def test_question_key_differs_by_kind():
    a = Question(id="X", kind="clarification", prompt_text="same text")
    b = Question(id="X", kind="blocker", prompt_text="same text")
    assert question_key(a) != question_key(b)


def test_format_answers_md_renders_skipped_section_with_assumption_directive():
    qs = [
        Question(id="CLAR-01", kind="clarification", prompt_text="ans me"),
        Question(id="CLAR-02", kind="clarification", prompt_text="skip me"),
    ]
    answers = {"CLAR-01": "yes"}
    skipped = [qs[1]]
    md = format_answers_md(qs, answers, skipped=skipped)
    assert "## Answered" in md
    assert "## Skipped" in md
    assert "ASSUMPTION" in md
    assert "Do NOT re-emit" in md
    assert "skip me" in md


def test_write_answers_file_passes_skipped_through(tmp_path: Path):
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    path = write_answers_file(tmp_path, qs, {}, skipped=qs)
    content = path.read_text(encoding="utf-8")
    assert "Skipped" in content
    assert "ASSUMPTION" in content
