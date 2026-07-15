"""Unit tests for qtea.failure_classifiers.

One test per category. Builds a synthetic `StepResult` with the matching
error string, asserts the classification + any fix_hint produced.
"""

from __future__ import annotations

from qtea.failure_classifiers import (
    ClassificationResult,
    FailureCategory,
    classify_failure,
    is_recoverable_category,
)
from qtea.steps.base import StepResult


def _result(error: str | None) -> StepResult:
    """Synthetic failed StepResult for classifier tests."""
    return StepResult(
        success=False, status="failed", outputs=[], error=error,
    )


# --- API-layer categories --------------------------------------------------


def test_classifies_api_fatal_error():
    r = _result("API fatal error: HTTP 401 Unauthorized")
    c = classify_failure(r)
    assert c.category == FailureCategory.API_FATAL
    assert c.safe_to_auto_retry is False
    assert c.fix_hint is None


def test_classifies_api_retry_storm():
    r = _result("SDK api_retry storm: 5 consecutive 502s on Vertex partner endpoint")
    c = classify_failure(r)
    assert c.category == FailureCategory.API_RETRY_STORM
    assert c.safe_to_auto_retry is False


# --- Truncation (recoverable; step owns the budget hint) -------------------


def test_classifies_truncation_via_pom_syntax_invalid_log():
    r = _result("pom_syntax_invalid: line 585 mid-def click_and_wait")
    c = classify_failure(r)
    assert c.category == FailureCategory.TRUNCATION_RECOVERABLE
    assert c.safe_to_auto_retry is True
    # No fix_hint from the classifier — _extend_one already armed its own
    # call-site-scoped override key. The classifier just labels it.
    assert c.fix_hint is None


def test_classifies_truncation_via_max_tokens_phrase():
    r = _result("agent response cut off — likely max_tokens truncation")
    c = classify_failure(r)
    assert c.category == FailureCategory.TRUNCATION_RECOVERABLE


# --- Schema type mismatch --------------------------------------------------


def test_classifies_schema_type_mismatch_and_produces_clarification():
    r = _result(
        "plan failed schema validation: 'Skipped non-automatable TCs: TC-X' "
        "is not of type 'array'"
    )
    c = classify_failure(r)
    assert c.category == FailureCategory.SCHEMA_TYPE_MISMATCH
    assert c.safe_to_auto_retry is True
    assert c.fix_hint is not None
    hint = c.fix_hint["prompt_clarification"]
    assert "type 'array'" in hint
    # The received value should appear (possibly truncated to 80 chars).
    assert "Skipped non-automatable" in hint


def test_schema_type_mismatch_truncates_long_value_in_clarification():
    long_value = "x" * 500
    r = _result(f"... '{long_value}' is not of type 'array'")
    c = classify_failure(r)
    assert c.category == FailureCategory.SCHEMA_TYPE_MISMATCH
    assert c.fix_hint is not None
    # Should not embed the full 500-char value verbatim.
    assert "x" * 500 not in c.fix_hint["prompt_clarification"]
    assert "..." in c.fix_hint["prompt_clarification"]


# --- Schema missing required field ----------------------------------------


def test_classifies_schema_missing_required_field():
    r = _result(
        "plan failed schema validation: 'test_file_target' is a required property"
    )
    c = classify_failure(r)
    assert c.category == FailureCategory.SCHEMA_MISSING_REQUIRED_FIELD
    assert c.safe_to_auto_retry is True
    assert c.fix_hint is not None
    assert "test_file_target" in c.fix_hint["prompt_clarification"]


# --- JSON unparseable ------------------------------------------------------


def test_classifies_json_unparseable_via_qtea_prefix():
    r = _result("plan JSON unparseable: Expecting value: line 1 column 1")
    c = classify_failure(r)
    assert c.category == FailureCategory.JSON_UNPARSEABLE
    assert c.safe_to_auto_retry is True
    assert c.fix_hint is not None
    assert "JSON" in c.fix_hint["prompt_clarification"]


def test_classifies_json_unparseable_via_decoder_error():
    r = _result("json.decoder.JSONDecodeError: Expecting value at line 3")
    c = classify_failure(r)
    assert c.category == FailureCategory.JSON_UNPARSEABLE


# --- Non-recoverable categories -------------------------------------------


def test_classifies_agent_no_output():
    r = _result("agent produced no output")
    c = classify_failure(r)
    assert c.category == FailureCategory.AGENT_NO_OUTPUT
    assert c.safe_to_auto_retry is False


def test_classifies_locator_resolution_timeout():
    r = _result("TBD locator unresolved after 30s timeout: 'sign in button'")
    c = classify_failure(r)
    assert c.category == FailureCategory.LOCATOR_RESOLUTION_TIMEOUT
    assert c.safe_to_auto_retry is False


def test_classifies_sut_git_failure():
    r = _result("git commit failed: nothing to commit, working tree clean")
    c = classify_failure(r)
    assert c.category == FailureCategory.SUT_GIT_FAILURE
    assert c.safe_to_auto_retry is False


def test_classifies_test_runner_import_error():
    r = _result(
        "pytest collection failed: E ImportError: cannot import name 'X' from 'tests.fixtures'"
    )
    c = classify_failure(r)
    assert c.category == FailureCategory.TEST_RUNNER_ERROR
    assert c.safe_to_auto_retry is False


# --- Catch-all -------------------------------------------------------------


def test_unknown_for_empty_error():
    r = _result(None)
    c = classify_failure(r)
    assert c.category == FailureCategory.UNKNOWN
    assert c.safe_to_auto_retry is False


def test_unknown_for_unmatched_error_text():
    r = _result("the gerbil escaped the wheel")
    c = classify_failure(r)
    assert c.category == FailureCategory.UNKNOWN


# --- Ordering: more specific categories beat the catch-all ----------------


def test_truncation_beats_unknown_on_combined_error():
    """An error mentioning both truncation AND something else still matches
    truncation because it's checked before UNKNOWN."""
    r = _result("step08.pom_syntax_invalid triggered; misc context follows")
    c = classify_failure(r)
    assert c.category == FailureCategory.TRUNCATION_RECOVERABLE


def test_schema_mismatch_beats_test_runner_when_both_match():
    """An error string that contains schema-validator text AND test-runner
    text classifies as schema-mismatch (checked earlier)."""
    r = _result(
        "'foo' is not of type 'array' (pytest collection failed downstream)"
    )
    c = classify_failure(r)
    assert c.category == FailureCategory.SCHEMA_TYPE_MISMATCH


# --- is_recoverable_category helper ---------------------------------------


def test_is_recoverable_category_lists():
    recoverable = {
        FailureCategory.TRUNCATION_RECOVERABLE,
        FailureCategory.SCHEMA_TYPE_MISMATCH,
        FailureCategory.SCHEMA_MISSING_REQUIRED_FIELD,
        FailureCategory.JSON_UNPARSEABLE,
        FailureCategory.PLAN_GATE_VIOLATION,
    }
    for cat in FailureCategory:
        assert is_recoverable_category(cat) == (cat in recoverable)


# --- ClassificationResult dataclass is frozen -----------------------------


def test_classification_result_is_frozen():
    c = ClassificationResult(
        category=FailureCategory.UNKNOWN,
        explanation="x",
        safe_to_auto_retry=False,
    )
    import pytest
    with pytest.raises((AttributeError, Exception)):
        c.category = FailureCategory.API_FATAL  # type: ignore[misc]


# --- Edge cases: empty / whitespace / None error --------------------------


def test_empty_string_error_classifies_as_unknown():
    """An empty string is falsy in every predicate, so it falls through to
    the catch-all UNKNOWN — same as None."""
    c = classify_failure(_result(""))
    assert c.category == FailureCategory.UNKNOWN
    assert c.fix_hint is None


def test_whitespace_only_error_classifies_as_unknown():
    """A whitespace-only string matches none of the patterns (lstrip
    leaves it empty for the sentinel checks; regex finds no match)."""
    c = classify_failure(_result("   \n\t  "))
    assert c.category == FailureCategory.UNKNOWN


def test_classifier_does_not_raise_on_none_error():
    """Defensive: classify_failure must never raise, even on the weirdest
    inputs. Pipeline.py wraps this in critical-path logging — an exception
    here would mask the real failure."""
    # No assertion needed beyond the call not raising.
    classify_failure(_result(None))


# --- API sentinels: leading whitespace tolerance --------------------------


def test_api_fatal_tolerates_leading_whitespace():
    """``_is_api_fatal_error`` lstrips before prefix-checking — error
    strings emitted with a leading newline (common when logged) must
    still classify correctly."""
    c = classify_failure(_result("\n  API fatal error: HTTP 500 Server Error"))
    assert c.category == FailureCategory.API_FATAL


def test_api_retry_storm_tolerates_leading_whitespace():
    c = classify_failure(_result("   SDK api_retry storm: 3 failures"))
    assert c.category == FailureCategory.API_RETRY_STORM


# --- Case sensitivity ------------------------------------------------------


def test_truncation_pattern_is_case_insensitive():
    """The truncation regex uses re.I — variations in case must still match."""
    c = classify_failure(_result("POM_SYNTAX_INVALID at line 800"))
    assert c.category == FailureCategory.TRUNCATION_RECOVERABLE


def test_json_unparseable_is_case_insensitive():
    c = classify_failure(_result("Plan JSON Unparseable: bad token"))
    assert c.category == FailureCategory.JSON_UNPARSEABLE


def test_schema_type_mismatch_is_case_insensitive():
    """The 'is not of type' regex uses re.I — match must hold regardless
    of case in the validator preamble."""
    c = classify_failure(
        _result("'val' Is Not Of Type 'array'")
    )
    assert c.category == FailureCategory.SCHEMA_TYPE_MISMATCH


# --- Multi-line error strings ---------------------------------------------


def test_classifies_correctly_when_error_spans_multiple_lines():
    """Realistic stack-trace-style error: validator output appears on a
    line below qtea's prefix. re.search (not re.match) must still find it."""
    err = (
        "plan failed schema validation:\n"
        "    Detail: 'short_string' is not of type 'array'\n"
        "    Path: plan.notes\n"
    )
    c = classify_failure(_result(err))
    assert c.category == FailureCategory.SCHEMA_TYPE_MISMATCH
    assert c.fix_hint is not None
    assert "short_string" in c.fix_hint["prompt_clarification"]


def test_schema_missing_field_multiline_error():
    err = (
        "plan failed schema validation:\n"
        "  'test_file_target' is a required property\n"
        "  on instance plan.test_cases[0]\n"
    )
    c = classify_failure(_result(err))
    assert c.category == FailureCategory.SCHEMA_MISSING_REQUIRED_FIELD
    assert c.fix_hint is not None
    assert "test_file_target" in c.fix_hint["prompt_clarification"]


# --- fix_hint dict shape contract -----------------------------------------


def test_recoverable_categories_use_prompt_clarification_key():
    """Every recoverable category that emits a fix_hint must use the
    ``prompt_clarification`` key. The smart-retry consumer (base.py +
    steps/s08_codegen.py) reads exactly that key — diverging keys would
    silently drop the hint between attempts."""
    fixtures = [
        "'x' is not of type 'array'",                # SCHEMA_TYPE_MISMATCH
        "'field_y' is a required property",          # SCHEMA_MISSING_REQUIRED_FIELD
        "json.decoder.JSONDecodeError at line 2",    # JSON_UNPARSEABLE
    ]
    for err in fixtures:
        c = classify_failure(_result(err))
        assert c.fix_hint is not None, f"no fix_hint for {err!r}"
        assert list(c.fix_hint.keys()) == ["prompt_clarification"], (
            f"unexpected fix_hint shape for {err!r}: {c.fix_hint!r}"
        )
        assert isinstance(c.fix_hint["prompt_clarification"], str)
        assert c.fix_hint["prompt_clarification"]  # non-empty


def test_non_recoverable_categories_have_no_fix_hint():
    """Symmetric contract: non-recoverable categories never set fix_hint
    (otherwise the smart-retry path would consume garbage on attempt 2)."""
    fixtures = [
        ("API fatal error: HTTP 401",                       FailureCategory.API_FATAL),
        ("SDK api_retry storm: 5 failures",                 FailureCategory.API_RETRY_STORM),
        ("agent produced no output",                        FailureCategory.AGENT_NO_OUTPUT),
        ("TBD locator unresolved after 30s",                FailureCategory.LOCATOR_RESOLUTION_TIMEOUT),
        ("git push failed: connection denied",              FailureCategory.SUT_GIT_FAILURE),
        ("E ImportError: cannot import name 'X'",           FailureCategory.TEST_RUNNER_ERROR),
        ("the gerbil escaped the wheel",                    FailureCategory.UNKNOWN),
    ]
    for err, expected_cat in fixtures:
        c = classify_failure(_result(err))
        assert c.category == expected_cat, f"wrong category for {err!r}"
        assert c.fix_hint is None, f"unexpected fix_hint for {err!r}: {c.fix_hint!r}"


# --- Truncation: special — recoverable but emits no fix_hint --------------


def test_truncation_is_recoverable_but_emits_no_fix_hint():
    """TRUNCATION_RECOVERABLE is the exception to the
    recoverable→fix_hint correspondence: the step (s08_codegen._extend_one)
    arms its own call-site-scoped override key BEFORE returning the
    failure. The classifier only labels."""
    c = classify_failure(_result("pom_syntax_invalid at line 800 of 800"))
    assert c.category == FailureCategory.TRUNCATION_RECOVERABLE
    assert c.safe_to_auto_retry is True
    assert c.fix_hint is None  # the step armed its own override


# --- Detection rule variants ----------------------------------------------


def test_git_failure_matches_various_verbs():
    """The git-failure regex matches `git ... (failed|error|conflict|denied)`
    — verify each verb classifies correctly."""
    for err in (
        "git push failed: remote rejected",
        "git merge error: index lock present",
        "git rebase conflict in 3 files",
        "git fetch denied: not authorized",
    ):
        c = classify_failure(_result(err))
        assert c.category == FailureCategory.SUT_GIT_FAILURE, (
            f"not classified as git failure: {err!r}"
        )


def test_test_runner_matches_internalerror():
    """INTERNALERROR> is pytest's catastrophic-failure marker — the
    classifier picks it up as a runner failure."""
    c = classify_failure(_result("INTERNALERROR> KeyError in conftest.py"))
    assert c.category == FailureCategory.TEST_RUNNER_ERROR


def test_locator_unresolved_alternative_phrasing():
    """The classifier matches BOTH 'TBD locator' and 'locator unresolved'
    so the rule isn't brittle against minor heal-agent wording shifts."""
    c = classify_failure(_result("locator unresolved: 'submit-button'"))
    assert c.category == FailureCategory.LOCATOR_RESOLUTION_TIMEOUT


# --- Long-value truncation in clarification --------------------------------


def test_schema_type_mismatch_clarification_includes_ellipsis_marker():
    """When the received value is truncated, the clarification ends with
    `...` so the agent (and any human reading the prompt) can tell the
    preview is incomplete."""
    long_value = "ab" * 100   # 200 chars
    c = classify_failure(_result(f"'{long_value}' is not of type 'array'"))
    assert c.fix_hint is not None
    clarif = c.fix_hint["prompt_clarification"]
    assert "..." in clarif
    # The expected type label still appears in the clarification.
    assert "type 'array'" in clarif


# --- Ordering: API_FATAL beats every other category ----------------------


def test_api_fatal_beats_other_signals_in_same_string():
    """API_FATAL is rule #1 and wins even when the error string also
    contains schema-violator text — important for upstream-blip failures
    that happen mid-validation."""
    err = (
        "API fatal error: HTTP 503 Service Unavailable; "
        "previously: 'x' is not of type 'array'"
    )
    c = classify_failure(_result(err))
    assert c.category == FailureCategory.API_FATAL
    # Non-recoverable wins → no fix_hint even though the substring looks
    # like a recoverable schema violation.
    assert c.fix_hint is None
