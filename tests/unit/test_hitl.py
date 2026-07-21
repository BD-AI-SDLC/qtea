"""Unit tests for the human-in-the-loop module."""

from __future__ import annotations

from pathlib import Path

from qtea.hitl import (
    RESOLUTION_ANSWERED,
    RESOLUTION_HEADED_LOGIN_SKIP,
    RESOLUTION_SCOPE_EXCLUSION,
    RESOLUTION_SKIPPED_DROP,
    RESOLUTION_SKIPPED_LEGACY,
    HitlDecision,
    Question,
    append_ledger,
    distinctive_tokens,
    extract_questions,
    find_prior_decision,
    format_answers_md,
    has_not_ready_verdict,
    ledger_path,
    load_ledger,
    looks_like_negative_drop_intent,
    looks_like_skip_intent,
    prompt_user,
    question_key,
    render_prior_decisions_md,
    resolve_against_ledger,
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
    assert "No items were answered, skipped, or excluded" in md


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


def test_question_key_same_across_kinds():
    """Kind is no longer part of the key — same text means same question."""
    a = Question(id="X", kind="clarification", prompt_text="same text")
    b = Question(id="X", kind="blocker", prompt_text="same text")
    assert question_key(a) == question_key(b)


def test_format_answers_md_renders_skipped_section_with_drop_directive():
    """Skipped items must instruct the agent to DROP the AC/TC and record
    in Coverage Notes. The directive prose may NEGATIVELY reference
    `[ASSUMPTION]` ("Do NOT write..."), but the active instruction must
    be DROP, not the old "make a reasonable assumption" framing."""
    qs = [
        Question(id="CLAR-01", kind="clarification", prompt_text="ans me"),
        Question(id="CLAR-02", kind="clarification", prompt_text="skip me"),
    ]
    answers = {"CLAR-01": "yes"}
    skipped = [qs[1]]
    md = format_answers_md(qs, answers, skipped=skipped)
    lowered = md.lower()
    assert "## Answered" in md
    assert "## Skipped — Drop From Output" in md
    assert "Coverage Notes" in md
    assert "remove" in lowered
    assert "skip me" in md
    # Old instructional phrasing must be gone.
    assert "make a reasonable assumption" not in lowered
    assert "mark it inline with" not in lowered


def test_write_answers_file_passes_skipped_through(tmp_path: Path):
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    path = write_answers_file(tmp_path, qs, {}, skipped=qs)
    content = path.read_text(encoding="utf-8")
    lowered = content.lower()
    assert "Drop From Output" in content
    assert "Coverage Notes" in content
    # Old instructional phrasing must be gone.
    assert "make a reasonable assumption" not in lowered
    assert "mark it inline with" not in lowered


def test_format_answers_md_accepts_legacy_dict_str_str_answers():
    """Legacy callers may still pass `dict[str, str]` (answer-only). The
    function treats those values as RESOLUTION_ANSWERED."""
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    md = format_answers_md(qs, {"CLAR-01": "an answer"})
    assert "## Answered" in md
    assert "an answer" in md


def test_format_answers_md_accepts_tuple_valued_answers():
    """New prompt_user return shape: `dict[str, tuple[resolution, text]]`."""
    qs = [Question(id="CLAR-01", kind="clarification", prompt_text="q?")]
    md = format_answers_md(qs, {"CLAR-01": (RESOLUTION_ANSWERED, "an answer")})
    assert "## Answered" in md
    assert "an answer" in md


def test_format_answers_md_renders_scope_exclusion_section():
    """Scope-exclusion items get their own section with clear "interpret
    as scope-exclusion, NOT as a literal value" framing."""
    qs = [
        Question(
            id="CLAR-01", kind="clarification", prompt_text="aria-label for mobile?"
        ),
    ]
    answers = {"CLAR-01": (RESOLUTION_SCOPE_EXCLUSION, "mobile isn't in scope")}
    md = format_answers_md(qs, answers)
    assert "## Scope Exclusions — Drop Excluded Items" in md
    assert "mobile isn't in scope" in md
    assert "scope-exclusion, NOT a literal value" in md
    assert "Coverage Notes" in md
    # Must not slot the exclusion text into the Answered section.
    assert "## Answered" not in md


def test_question_key_strips_blocker_prefix():
    a = Question(id="CLAR-01", kind="clarification", prompt_text="SSO config unavailable")
    b = Question(
        id="BLOCK-01",
        kind="blocker",
        prompt_text="How should we resolve this blocker: SSO config unavailable",
    )
    assert question_key(a) == question_key(b)


def test_question_key_strips_bold_and_normalizes():
    a = Question(id="X", kind="clarification", prompt_text="exact target URL")
    b = Question(
        id="Y",
        kind="blocker",
        prompt_text="How should we resolve this blocker: **Exact Target URL**",
    )
    assert question_key(a) == question_key(b)


def test_dedup_merges_clarification_referencing_blocker():
    md = """\
# Spec

The target URL is [CLARIFICATION NEEDED: exact target URL — see blocker #1].

## Blockers

| Blocker | Severity |
|---------|----------|
| Exact Gemini Enterprise target URL unknown | high |
"""
    qs = extract_questions(md)
    assert len(qs) == 1
    assert qs[0].kind == "blocker"


def test_dedup_merges_cross_kind_same_normalized_text():
    md = """\
# Spec

Login uses [CLARIFICATION NEEDED: SSO config unavailable].

## Blockers

| Blocker | Severity |
|---------|----------|
| SSO config unavailable | high |
"""
    qs = extract_questions(md)
    assert len(qs) == 1
    assert qs[0].kind == "blocker"


def test_dedup_full_scenario_seven_to_three():
    """Reproduce the exact problem: 7 raw questions should merge to 3."""
    md = """\
# Spec

German. [CLARIFICATION NEEDED: exact German translation string — see blocker #2]
Tooltip [CLARIFICATION NEEDED: exact tooltip string — see blocker #3]
URL [CLARIFICATION NEEDED: exact target URL — see blocker #1]
DE dup [CLARIFICATION NEEDED: exact DE string — see blocker #2]

## Blockers

| # | Blocker | Affects |
|---|---------|---------|
| 1 | **Exact Gemini Enterprise target URL** is not specified. | AC-5 |
| 2 | **German translation string** for the link label is not provided. | AC-8 |
| 3 | **Tooltip text** shown on hover is not specified. | AC-3 |
"""
    qs = extract_questions(md)
    assert len(qs) == 3
    assert all(q.kind == "blocker" for q in qs)


def test_dedup_merges_blocker_and_inline_clarification_by_ac_id():
    """The step-2 real-world case: agent emits both a Blocker (with
    'Affected ACs: AC-5' column) AND leaves an inline `[CLARIFICATION
    NEEDED: exact URL]` on the AC-5 line. The blocker's question and the
    terse inline placeholder describe the same gap — user should be asked
    only once."""
    md = """\
# Spec

## Blockers

| ID | Question | Description | Severity | Affected ACs |
|----|----------|-------------|----------|--------------|
| BLOCK-001 | What is the exact target URL for the Gemini Enterprise link? | AC-5 states "Correct URL is used" but no concrete URL is specified. | high | AC-5 |
| BLOCK-002 | What are the exact EN and DE translation strings? | AC-10/AC-11 reference EN/DE translations but no strings are provided. | high | AC-10, AC-11 |

## Acceptance Criteria

- [ ] **AC-5:** Given the button is visible, When the user clicks it, Then the URL that opens is [CLARIFICATION NEEDED: exact URL] with Bosch domain authentication applied. `[AUTOMATABLE]`
- [ ] **AC-10:** Given English locale, When navigation renders, Then label is [CLARIFICATION NEEDED: exact EN strings]. `[AUTOMATABLE]`
- [ ] **AC-11:** Given German locale, When navigation renders, Then label is [CLARIFICATION NEEDED: exact DE strings]. `[AUTOMATABLE]`
"""
    qs = extract_questions(md)
    # Only the two blockers should survive; all three inline CLARs are
    # subsumed by the AC-ID overlap pass.
    assert len(qs) == 2
    assert all(q.kind == "blocker" for q in qs)
    assert {q.id for q in qs} == {"BLOCK-01", "BLOCK-02"}


def test_dedup_keeps_clarification_when_ac_not_covered_by_any_blocker():
    """Conservative subset-check: a CLAR on an AC that no blocker covers
    must survive. Guards against the AC-ID pass over-firing."""
    md = """\
# Spec

## Blockers

| ID | Question | Description | Severity | Affected ACs |
|----|----------|-------------|----------|--------------|
| BLOCK-001 | What is the exact URL? | URL undefined. | high | AC-5 |

## Acceptance Criteria

- [ ] **AC-5:** URL is [CLARIFICATION NEEDED: exact URL]. `[AUTOMATABLE]`
- [ ] **AC-7:** Aria-label is [CLARIFICATION NEEDED: exact aria-label text]. `[MANUAL ONLY]`
"""
    qs = extract_questions(md)
    # BLOCK-001 + CLAR for AC-7 survive; CLAR for AC-5 is dropped.
    assert len(qs) == 2
    kinds = sorted(q.kind for q in qs)
    assert kinds == ["blocker", "clarification"]
    surviving_clar = next(q for q in qs if q.kind == "clarification")
    assert "aria-label" in surviving_clar.prompt_text


def test_dedup_merges_clarification_referencing_block_id_style():
    """Agents follow the table's `BLOCK-001` ID convention when writing
    inline cross-references — `see BLOCK-006` rather than `see blocker #6`.
    The xref regex must accept both."""
    md = """\
# Spec

## Blockers

| ID | Question | Description | Severity | Affected ACs |
|----|----------|-------------|----------|--------------|
| BLOCK-001 | What is the exact URL? | URL undefined. | high | — |
| BLOCK-002 | Which browsers are supported? | Browser matrix undefined. | medium | — |

## Acceptance Criteria

- [ ] **AC-PERF-1:** Page loads in [CLARIFICATION NEEDED: exact URL — see BLOCK-001]. `[AUTOMATABLE]`
- [ ] **AC-COMPAT-1:** Works on [CLARIFICATION NEEDED: browser matrix — see BLOCK-002]. `[AUTOMATABLE]`
"""
    qs = extract_questions(md)
    # Both inline CLARs reference blockers by `BLOCK-NNN` and must be
    # dropped by Pass 1. Only the two blockers survive.
    assert len(qs) == 2
    assert all(q.kind == "blocker" for q in qs)


def test_dedup_merges_blocker_and_inline_clarification_by_composite_ac_id():
    """Pass 2 must recognise composite AC IDs (e.g. `AC-A11Y-1`, `AC-COMPAT-1`)
    so a blocker covering them subsumes inline CLARs on those bullets."""
    md = """\
# Spec

## Blockers

| ID | Question | Description | Severity | Affected ACs |
|----|----------|-------------|----------|--------------|
| BLOCK-001 | What aria-label should the button expose? | undefined | high | AC-A11Y-1 |
| BLOCK-002 | Which browser matrix? | undefined | medium | AC-COMPAT-1 |

## Acceptance Criteria

- [ ] **AC-A11Y-1:** Screen reader announces [CLARIFICATION NEEDED: exact aria-label]. `[MANUAL ONLY]`
- [ ] **AC-COMPAT-1:** Renders on [CLARIFICATION NEEDED: browser matrix]. `[AUTOMATABLE]`
"""
    qs = extract_questions(md)
    assert len(qs) == 2
    assert all(q.kind == "blocker" for q in qs)


def test_extract_blockers_prefers_description_column():
    """When both 'Blocker' and 'Description' columns exist, use Description."""
    md = """\
# Plan

## Blockers

| Blocker | Description | Affected TCs | Severity |
|---------|-------------|--------------|----------|
| BLOCK-001 | German translation unknown | TC-NAV-009 | medium |
"""
    qs = extract_questions(md)
    blockers = [q for q in qs if q.kind == "blocker"]
    assert len(blockers) == 1
    assert "German translation unknown" in blockers[0].prompt_text
    assert "BLOCK-001" not in blockers[0].prompt_text


def test_dedup_merges_blocker_and_open_question_by_tc_id():
    """Open question whose TC IDs overlap with a blocker is dropped."""
    md = """\
# Plan

## Blockers

| Blocker | Description | Affected TCs | Severity |
|---------|-------------|--------------|----------|
| BLOCK-001 | German string missing | TC-NAV-009, TC-NAV-010 | medium |

## Open PO Questions

- **[Blocks TC-NAV-009, TC-NAV-010]** What is the German translation?
"""
    qs = extract_questions(md)
    assert len(qs) == 1
    assert qs[0].kind == "blocker"


def test_extract_blockers_prefers_question_column_over_description():
    """When a `Question` column exists, use it raw — no boilerplate prefix."""
    md = """\
# Plan

## Blockers

| ID | Question | Description | Affected TCs | Severity |
|----|----------|-------------|--------------|----------|
| BLOCK-001 | Which GA SDK should we intercept — `gtag.js` or custom? | GA integration detail unconfirmed. | TC-GNAV-010 | high |
"""
    qs = extract_questions(md)
    blockers = [q for q in qs if q.kind == "blocker"]
    assert len(blockers) == 1
    prompt = blockers[0].prompt_text
    assert prompt.startswith("Which GA SDK")
    assert "How should we resolve this blocker" not in prompt
    assert "GA integration detail" not in prompt  # description column not used


def test_extract_blockers_no_redundant_prefix_when_desc_is_interrogative():
    """Even without a Question column, drop the prefix when the description
    is already phrased as a question — avoids `'How should we resolve this
    blocker: What is the ...?'` doubling."""
    md = """\
# Plan

## Blockers

| Blocker | Affected TCs | Severity |
|---------|--------------|----------|
| What is the expected GA event payload schema? | TC-GNAV-011 | high |
"""
    qs = extract_questions(md)
    blockers = [q for q in qs if q.kind == "blocker"]
    assert len(blockers) == 1
    assert blockers[0].prompt_text == "What is the expected GA event payload schema?"


def test_extract_blockers_keeps_prefix_for_statement_descriptions():
    """Legacy statement-form descriptions still get the boilerplate prefix
    so the user at least sees them framed as a decision request."""
    md = """\
# Plan

## Blockers

| Blocker | Affected TCs | Severity |
|---------|--------------|----------|
| SSO config unavailable | TC-AUTH-005 | high |
"""
    qs = extract_questions(md)
    blockers = [q for q in qs if q.kind == "blocker"]
    assert len(blockers) == 1
    assert blockers[0].prompt_text.startswith("How should we resolve this blocker:")


def test_dedup_step3_full_scenario_six_to_three():
    """Reproduce the step 3 problem: 6 questions should merge to 3."""
    md = """\
# Test Plan

## Blockers

| Blocker | Description | Affected TCs | Severity |
|---------|-------------|--------------|----------|
| BLOCK-001 | German translation string unknown | TC-GNAV-009, TC-GNAV-010 | medium |
| BLOCK-002 | GA event payload schema unconfirmed | TC-GNAV-011, TC-GNAV-012 | medium |
| BLOCK-003 | Confirmed aria-label text unspecified | TC-GNAV-013, TC-GNAV-014 | medium |

## Open PO Questions

- **[Blocks TC-GNAV-009, TC-GNAV-010]** What is the approved German translation?
- **[Blocks TC-GNAV-011, TC-GNAV-012]** Does the GA event need extra params?
- **[Blocks TC-GNAV-013, TC-GNAV-014]** What is the confirmed aria-label text?
"""
    qs = extract_questions(md)
    assert len(qs) == 3
    assert all(q.kind == "blocker" for q in qs)


# ---------------------------------------------------------------------------
# Skip-intent text detection
# ---------------------------------------------------------------------------


def test_looks_like_skip_intent_matches_common_skip_phrases():
    """The user typed "i'm not sure. skip this" in step 2 and it was treated
    as a real answer — that's the bug. These all need to read as skip."""
    skips = [
        "skip",
        "Skip this",
        "skip me",
        "SKIP THIS",
        "n/a",
        "N/A",
        "na",
        "none",
        "idk",
        "IDK",
        "i don't know",
        "I do not know",
        "dunno",
        "unknown",
        "not sure",
        "i'm not sure",
        "i'm not sure. skip this",
        "I'm not sure, skip this",
        "no idea",
        "pass",
    ]
    for s in skips:
        assert looks_like_skip_intent(s), f"should be skip-intent: {s!r}"


def test_looks_like_skip_intent_rejects_real_answers():
    """Real, substantive answers must NOT be classified as skip-intent —
    silently dropping them would be worse than the original bug."""
    real = [
        "gtag.js",
        "https://example.com/foo",
        "use feature flag X",
        "yes",  # short but a real answer
        "no",
        "Go to Gemini Enterprise",
        "the en label is 'Go to Gemini Enterprise', de is 'Zu Gemini Enterprise wechseln'",
        "skip the second one but use option A for the first",  # context-bearing
    ]
    for s in real:
        assert not looks_like_skip_intent(s), f"should NOT be skip-intent: {s!r}"


# ---------------------------------------------------------------------------
# Negative-drop / scope-exclusion intent detection
# ---------------------------------------------------------------------------


def test_negative_drop_regex_matches_no_aria_label():
    """The original failing example from the user: 'there is no aria-label'
    must be detected so the confirmation prompt fires."""
    matched, phrase = looks_like_negative_drop_intent("there is no aria-label")
    assert matched
    assert phrase == "there is no aria-label"


def test_negative_drop_regex_matches_mobile_out_of_scope():
    """Scope-exclusion phrasing must trigger detection. The user-supplied
    example: 'mobile isn't in scope'."""
    cases = [
        "mobile isn't in scope",
        "mobile is not in scope",
        "out of scope",
        "not in scope",
        "out of scope: mobile",
        "skip mobile",
        "exclude mobile",
        "not testing mobile",
    ]
    for s in cases:
        matched, _ = looks_like_negative_drop_intent(s)
        assert matched, f"should detect negative-drop intent: {s!r}"


def test_negative_drop_regex_matches_doesnt_exist():
    cases = [
        "there are no events",
        "the app doesn't use redux",
        "we don't have an analytics SDK",
        "no such header",
        "the feature doesn't exist",
    ]
    for s in cases:
        matched, _ = looks_like_negative_drop_intent(s)
        assert matched, f"should detect negative-drop intent: {s!r}"


def test_negative_drop_regex_rejects_bare_no_and_none():
    """Bare 'no' / 'none' must NOT trigger drop-intent — those are real
    short answers (and 'none' is already handled by _SKIP_INTENT_RE)."""
    rejects = [
        "no",
        "none",
        "no special characters allowed",  # legitimate negative answer
        "no special characters",
    ]
    for s in rejects:
        matched, _ = looks_like_negative_drop_intent(s)
        assert not matched, f"must NOT detect drop-intent in: {s!r}"


def test_negative_drop_regex_rejects_legitimate_quoted_negatives():
    """Answers that happen to contain 'no' inside quoted technical
    identifiers must not trigger."""
    rejects = [
        "use the 'no-cache' header",
        "the value is 'no-store'",
        "set it to 'no'",
    ]
    for s in rejects:
        matched, _ = looks_like_negative_drop_intent(s)
        assert not matched, f"must NOT detect drop-intent in: {s!r}"


def test_negative_drop_regex_rejects_verbose_phrasing():
    """The regex is anchored end-to-end — trailing prose breaks the match
    so the agent gets the typed text as a literal answer with more
    context to interpret."""
    rejects = [
        "mobile isn't in scope yet but will be next quarter",
        "exclude mobile for now until we have time",
        "there is no aria-label but we should add one",
    ]
    for s in rejects:
        matched, _ = looks_like_negative_drop_intent(s)
        assert not matched, f"verbose phrasing should fall through: {s!r}"


# ---------------------------------------------------------------------------
# Resolution constants & ledger legacy back-compat
# ---------------------------------------------------------------------------


def test_resolution_constants_have_expected_string_values():
    """The ledger JSONL relies on these exact string values for back-compat
    with pre-rework runs. RESOLUTION_SKIPPED_LEGACY must be 'skipped' so
    older ledger entries get classified as legacy on resume."""
    assert RESOLUTION_ANSWERED == "answered"
    assert RESOLUTION_SKIPPED_DROP == "skipped_drop"
    assert RESOLUTION_SCOPE_EXCLUSION == "scope_exclusion"
    assert RESOLUTION_SKIPPED_LEGACY == "skipped"
    assert RESOLUTION_HEADED_LOGIN_SKIP == "headed_login_skip"


# ---------------------------------------------------------------------------
# prompt_user — headed_login metadata branch
# ---------------------------------------------------------------------------


def _headed_login_question() -> Question:
    return Question(
        id="AUTH-HEADED-LOGIN",
        kind="clarification",
        prompt_text="A browser window has opened. Press Enter when done.",
        context="Waiting for you to finish logging in at https://sut.example",
        metadata={"type": "headed_login", "base_url": "https://sut.example"},
    )


def test_prompt_user_headed_login_confirm_on_blank_enter(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("qtea.hitl.Prompt.ask", lambda *a, **kw: "")
    q = _headed_login_question()
    result = prompt_user([q], agent_label="Step 7 Headed Login")
    assert result == {q.id: (RESOLUTION_ANSWERED, "")}


def test_prompt_user_headed_login_skip_on_skip_intent_text(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("qtea.hitl.Prompt.ask", lambda *a, **kw: "skip")
    q = _headed_login_question()
    result = prompt_user([q], agent_label="Step 7 Headed Login")
    assert result == {q.id: (RESOLUTION_HEADED_LOGIN_SKIP, "")}


def test_prompt_user_headed_login_confirm_on_arbitrary_text(monkeypatch):
    """Any non-skip-intent text (not just blank Enter) still confirms — the
    answer content itself is never meaningful for this question."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("qtea.hitl.Prompt.ask", lambda *a, **kw: "done")
    q = _headed_login_question()
    result = prompt_user([q], agent_label="Step 7 Headed Login")
    assert result == {q.id: (RESOLUTION_ANSWERED, "")}


def test_ledger_legacy_skipped_jsonl_maps_to_legacy_resolution(tmp_path: Path):
    """A pre-rework ledger file on disk has entries with
    'resolution': 'skipped'. Load it and confirm those map to
    RESOLUTION_SKIPPED_LEGACY (preserving the old [ASSUMPTION] contract
    the user originally agreed to)."""
    import json
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "step": 2,
            "agent_label": "refine-spec",
            "question_id": "BLOCK-01",
            "question_text": "legacy skipped item?",
            "question_kind": "blocker",
            "resolution": "skipped",
            "answer": "",
            "context": "",
            "tokens": [],
        }) + "\n",
        encoding="utf-8",
    )
    loaded = load_ledger(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].resolution == RESOLUTION_SKIPPED_LEGACY


# ---------------------------------------------------------------------------
# Distinctive-token paraphrase matching
# ---------------------------------------------------------------------------


def test_distinctive_tokens_keeps_tech_identifiers():
    """Tokens with digits and dotted/slashed identifiers must survive — those
    are the highest-signal tokens for matching technical questions."""
    toks = distinctive_tokens(
        "Which GA SDK is used — gtag.js, @google-analytics/ga4, or custom?"
    )
    assert "gtag.js" in toks
    # Slash/dot/dash composites stay glued, which is fine — the same composite
    # appears in the step-3 paraphrase so paraphrase matching still works.
    assert any("google-analytics" in t for t in toks)
    assert "custom" in toks


def test_distinctive_tokens_drops_stopwords_and_short_alpha():
    """Stopwords and short pure-alpha tokens are dropped; substantive words
    (even single ones like ``important``) survive — we want the signal."""
    toks = distinctive_tokens("Which is the most for this?")
    assert toks == frozenset()
    # Short alpha-only ("GA", "is") gone; "GA" is 2 chars + alpha-only.
    toks2 = distinctive_tokens("GA is the SDK")
    assert "ga" not in toks2  # 2 chars, alpha → dropped


def test_find_prior_decision_matches_ga_sdk_paraphrase_from_user_run():
    """The exact scenario from run 20260609-160856-dd086a: step 2 asked one
    GA-SDK question; step 3's planner paraphrased it. Must match."""
    step2_text = (
        "Which Google Analytics SDK/wrapper is in use — `gtag.js`, "
        "`@google-analytics/ga4`, a custom AskBosch wrapper, or something "
        "else — and what are the full event payload fields (event name, "
        "category, label, value)?"
    )
    step2_context = (
        "BLOCK-002 | Which Google Analytics SDK/wrapper is in use — "
        "`gtag.js`, `@google-analytics/ga4`, a custom AskBosch wrapper, "
        "or something else — and what are the full event payload fields "
        "(event name, category, label, value)? | AC-6 mentions GA event "
        "`gemini_enterprise` but does not specify the SDK, the tracking "
        "call signature, or any additional payload fields required."
    )
    step3_question = Question(
        id="BLOCK-01",
        kind="blocker",
        prompt_text=(
            "Which GA SDK is used in this project — `gtag.js`, "
            "`@google-analytics/ga4`, or a custom wrapper?"
        ),
        context=(
            "BLOCK-001 | Which GA SDK is used in this project — `gtag.js`, "
            "`@google-analytics/ga4`, or a custom wrapper? | The correct "
            "stub/spy setup for GA event assertions depends on the SDK."
        ),
    )

    prior = HitlDecision.from_question(
        Question(id="BLOCK-02", kind="blocker", prompt_text=step2_text, context=step2_context),
        step=2,
        agent_label="refine-spec",
        resolution="skipped",
    )
    match = find_prior_decision(step3_question, [prior])
    assert match is not None, "GA paraphrase across steps must match the ledger"
    assert match.question_id == "BLOCK-02"
    assert match.resolution == "skipped"


def test_find_prior_decision_does_not_match_unrelated_question():
    """The fuzzy match must NOT collapse genuinely different questions —
    false positives silently drop real blockers."""
    prior = HitlDecision.from_question(
        Question(
            id="BLOCK-01",
            kind="blocker",
            prompt_text="Which Google Analytics SDK is used — gtag.js or ga4?",
        ),
        step=2,
        agent_label="refine-spec",
        resolution="skipped",
    )
    unrelated = Question(
        id="BLOCK-99",
        kind="blocker",
        prompt_text=(
            "What is the German translation for the 'Go to Gemini "
            "Enterprise' button label?"
        ),
    )
    assert find_prior_decision(unrelated, [prior]) is None


def test_find_prior_decision_returns_none_for_empty_ledger():
    q = Question(id="BLOCK-01", kind="blocker", prompt_text="anything?")
    assert find_prior_decision(q, []) is None


def test_resolve_against_ledger_splits_novel_and_resolved():
    prior = HitlDecision.from_question(
        Question(
            id="BLOCK-OLD",
            kind="blocker",
            prompt_text="Which GA SDK — gtag.js, @google-analytics/ga4, or custom wrapper?",
        ),
        step=2,
        agent_label="refine-spec",
        resolution="answered",
        answer="gtag.js",
    )
    paraphrase = Question(
        id="BLOCK-NEW",
        kind="blocker",
        prompt_text="Which GA SDK is in use in this project — gtag.js, @google-analytics/ga4, or a custom wrapper?",
    )
    novel = Question(
        id="BLOCK-XYZ",
        kind="blocker",
        prompt_text="Does the DSSF SideNavigationButton accept an external-link prop?",
    )
    novel_out, resolved = resolve_against_ledger([paraphrase, novel], [prior])
    assert len(novel_out) == 1
    assert novel_out[0].id == "BLOCK-XYZ"
    assert len(resolved) == 1
    assert resolved[0][0].id == "BLOCK-NEW"
    assert resolved[0][1].answer == "gtag.js"


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------


def test_ledger_roundtrip_through_disk(tmp_path: Path):
    """append_ledger → load_ledger preserves every field, including tokens."""
    decisions = [
        HitlDecision.from_question(
            Question(id="BLOCK-01", kind="blocker", prompt_text="Which GA SDK — gtag.js or ga4?"),
            step=2,
            agent_label="refine-spec",
            resolution="skipped",
        ),
        HitlDecision.from_question(
            Question(id="OPENQ-01", kind="open_question", prompt_text="What is the German label?"),
            step=2,
            agent_label="refine-spec",
            resolution="answered",
            answer="Zu Gemini Enterprise wechseln",
        ),
    ]
    append_ledger(tmp_path, decisions)
    assert ledger_path(tmp_path).exists()

    loaded = load_ledger(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].question_id == "BLOCK-01"
    assert loaded[0].resolution == "skipped"
    assert loaded[1].answer == "Zu Gemini Enterprise wechseln"
    # Tokens must round-trip so paraphrase matching still works after resume.
    assert "gtag.js" in loaded[0].tokens


def test_ledger_append_is_additive(tmp_path: Path):
    """Each append adds lines without truncating prior decisions."""
    d1 = HitlDecision.from_question(
        Question(id="Q1", kind="blocker", prompt_text="first?"),
        step=2, agent_label="a", resolution="skipped",
    )
    d2 = HitlDecision.from_question(
        Question(id="Q2", kind="blocker", prompt_text="second?"),
        step=3, agent_label="b", resolution="answered", answer="yes",
    )
    append_ledger(tmp_path, [d1])
    append_ledger(tmp_path, [d2])
    loaded = load_ledger(tmp_path)
    assert [d.question_id for d in loaded] == ["Q1", "Q2"]


def test_load_ledger_missing_file_returns_empty(tmp_path: Path):
    assert load_ledger(tmp_path) == []


def test_render_prior_decisions_md_contains_answer_and_drop_directives():
    """New semantics: skipped_drop renders a DROP directive (Coverage Notes,
    no instructional [ASSUMPTION] framing)."""
    decisions = [
        HitlDecision.from_question(
            Question(id="BLOCK-01", kind="blocker", prompt_text="Which GA SDK?"),
            step=2, agent_label="refine-spec", resolution=RESOLUTION_ANSWERED,
            answer="gtag.js",
        ),
        HitlDecision.from_question(
            Question(id="BLOCK-02", kind="blocker", prompt_text="Tracking call args?"),
            step=2, agent_label="refine-spec", resolution=RESOLUTION_SKIPPED_DROP,
        ),
    ]
    md = render_prior_decisions_md(decisions)
    lowered = md.lower()
    assert "gtag.js" in md
    assert "User answer" in md
    assert "DROP" in md
    assert "Coverage Notes" in md
    # Old instructional framing must be gone for the new skipped_drop branch.
    # (Legacy entries are tested separately; they ARE allowed to render
    # [ASSUMPTION] framing.)
    assert "reasonable assumption" not in lowered


def test_render_prior_decisions_md_preserves_legacy_assumption_framing():
    """A pre-rework ledger entry with resolution='skipped' (legacy) must
    still render with [ASSUMPTION] framing — preserves user intent at the
    time the decision was made under the old contract."""
    decisions = [
        HitlDecision.from_question(
            Question(id="BLOCK-01", kind="blocker", prompt_text="Legacy skip?"),
            step=2, agent_label="refine-spec",
            resolution=RESOLUTION_SKIPPED_LEGACY,
        ),
    ]
    md = render_prior_decisions_md(decisions)
    assert "ASSUMPTION" in md
    assert "legacy" in md.lower()


def test_render_prior_decisions_md_renders_scope_exclusion():
    decisions = [
        HitlDecision.from_question(
            Question(
                id="BLOCK-01",
                kind="blocker",
                prompt_text="aria-label for mobile and desktop?",
            ),
            step=2, agent_label="refine-spec",
            resolution=RESOLUTION_SCOPE_EXCLUSION,
            answer="mobile isn't in scope",
        ),
    ]
    md = render_prior_decisions_md(decisions)
    assert "scope" in md.lower()
    assert "mobile isn't in scope" in md
    assert "Coverage Notes" in md
    assert "ASSUMPTION" not in md


def test_format_answers_md_renders_ledger_resolved_section():
    prior = HitlDecision.from_question(
        Question(id="BLOCK-OLD", kind="blocker", prompt_text="Which GA SDK?"),
        step=2, agent_label="refine-spec", resolution="answered", answer="gtag.js",
    )
    paraphrase = Question(
        id="BLOCK-NEW", kind="blocker", prompt_text="What's the GA SDK?"
    )
    md = format_answers_md(
        questions=[], answers={}, ledger_resolved=[(paraphrase, prior)]
    )
    assert "Previously Resolved" in md
    assert "gtag.js" in md
    assert "BLOCK-NEW" in md
    assert "BLOCK-OLD" in md
