"""Rule-based failure classification for the step retry layer.

When a `Step` fails (`StepResult.success == False`), the retry loop in
`steps/base.py` historically did a BLIND retry: same agent, same inputs,
same params → same output. Two real incidents drove this module:

- Run `20260614-190647-ab7dac` Step 7: same `notes`-as-string schema
  violation on attempt 1 AND attempt 2 because nothing in the prompt
  changed between attempts.
- Same run, Step 8: POM extender truncated at `max_tokens=8000` on
  attempt 1, Phase B.5 auto-patch hit the same cap on attempt 2.

A classifier doesn't *fix* failures — it routes them. For deterministic
categories with a known recovery recipe (truncation → bump max_tokens,
schema-type-mismatch → prepend a clarification to the prompt), it
populates a `fix_hint` dict that the step's `run()` consumes on the
NEXT attempt via `ctx.extras`. For non-recoverable categories (genuine
agent confusion, infrastructure failures, API outages) it surfaces a
human-readable category label and a `safe_to_auto_retry=False` flag so
the auto-firing fix-proposal chain still fires.

This is pure-code rule-based. NO LLM call, NO new agent. Per
`CLAUDE.md` boundary: "Python never reasons. Agents never checkpoint."
String matching is not reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qtea.steps.base import StepContext, StepResult


# --- Existing API-layer sentinels (preserved verbatim) ---------------------
#
# These two strings are the prefixes `claude_runner._ApiRetryStorm` and the
# fatal-HTTP-error path use. The pre-existing `_is_api_*` helpers in
# steps/base.py match on these. We re-declare them here so this module is
# the single source of truth; base.py's helpers will become thin wrappers
# delegating here (preserving backward-compat with their existing call-sites).

_API_RETRY_STORM_PREFIX = "SDK api_retry storm"
_API_FATAL_ERROR_PREFIX = "API fatal error: HTTP"


# --- Hard caps for fix-hint values -----------------------------------------

_DEFAULT_MAX_TOKENS_CAP = 32000


# --- Category enum ---------------------------------------------------------


class FailureCategory(Enum):
    """Coarse categorisation of a step failure.

    The categories are deliberately small in number — we want each one
    to have either a clear deterministic recovery OR a clear "needs
    human" verdict. Avoid expanding beyond ~12 entries.
    """

    # Recoverable categories — have a fix_hint, safe_to_auto_retry=True
    TRUNCATION_RECOVERABLE = "truncation_recoverable"
    SCHEMA_TYPE_MISMATCH = "schema_type_mismatch"
    SCHEMA_MISSING_REQUIRED_FIELD = "schema_missing_required_field"
    JSON_UNPARSEABLE = "json_unparseable"
    PLAN_GATE_VIOLATION = "plan_gate_violation"

    # Non-recoverable — surface for human, auto-firing fix-proposal still fires
    API_FATAL = "api_fatal"
    API_RETRY_STORM = "api_retry_storm"
    AGENT_NO_OUTPUT = "agent_no_output"
    LOCATOR_RESOLUTION_TIMEOUT = "locator_resolution_timeout"
    SUT_GIT_FAILURE = "sut_git_failure"
    TEST_RUNNER_ERROR = "test_runner_error"

    # Catch-all
    UNKNOWN = "unknown"


_RECOVERABLE_CATEGORIES: frozenset[FailureCategory] = frozenset({
    FailureCategory.TRUNCATION_RECOVERABLE,
    FailureCategory.SCHEMA_TYPE_MISMATCH,
    FailureCategory.SCHEMA_MISSING_REQUIRED_FIELD,
    FailureCategory.JSON_UNPARSEABLE,
    FailureCategory.PLAN_GATE_VIOLATION,
})


# --- Result dataclass ------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of `classify_failure`.

    - ``category``: the bucketed category.
    - ``fix_hint``: when non-None, a dict to merge into ``ctx.extras`` so
      the step's next attempt can adjust its behavior. Convention: keys
      are namespaced by the consuming call-site (e.g.
      ``"s08_pom_extender_max_tokens_override"``) so multiple hints
      coexist without collision.
    - ``explanation``: one-line human-readable summary used in logs.
    - ``safe_to_auto_retry``: True iff the standard MAX_ATTEMPTS=2 retry
      is expected to succeed with the fix_hint applied. False → the
      auto-firing fix-proposal chain (debug RCA → critical-thinking →
      principal-software-engineer) fires on retry exhaustion as usual.
    """

    category: FailureCategory
    explanation: str
    safe_to_auto_retry: bool
    fix_hint: dict[str, Any] | None = None


# --- Detection helpers (each is a pure predicate over the error string) ----


def _is_api_retry_storm(error: str | None) -> bool:
    """Preserved sentinel-prefix match. Thin wrapper used by base.py too."""
    return bool(error) and error.lstrip().startswith(_API_RETRY_STORM_PREFIX)


def _is_api_fatal_error(error: str | None) -> bool:
    """Preserved sentinel-prefix match. Thin wrapper used by base.py too."""
    return bool(error) and error.lstrip().startswith(_API_FATAL_ERROR_PREFIX)


# Truncation: matched on s08's emitted log/error patterns from Phase 1+2.
# We don't try to parse the full error here — the truncation hint key is
# already armed in `ctx.extras` by `_extend_one` itself (see the syntax
# rollback block). The classifier merely RECOGNIZES the category for
# logging/auditing purposes and re-affirms the override if armed.
_TRUNCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pom_syntax_invalid", re.I),
    re.compile(r"max_tokens.*truncat", re.I),
    re.compile(r"truncat.*max_tokens", re.I),
    re.compile(r"response.*cut off", re.I),
)


def _looks_like_truncation(error: str | None) -> bool:
    if not error:
        return False
    return any(p.search(error) for p in _TRUNCATION_PATTERNS)


# Schema validation — the jsonschema library produces error strings like
# "'foo' is not of type 'array'" and "'bar' is a required property".
_SCHEMA_TYPE_MISMATCH_RE = re.compile(
    r"'([^']+)'\s+is\s+not\s+of\s+type\s+'([^']+)'", re.I,
)
_SCHEMA_REQUIRED_FIELD_RE = re.compile(
    r"'([^']+)'\s+is\s+a\s+required\s+property", re.I,
)


def _detect_schema_type_mismatch(error: str | None) -> tuple[str, str] | None:
    """Return (received_value, expected_type) when error matches the pattern."""
    if not error:
        return None
    # The error may contain JSON validator output AFTER a qtea prefix
    # (e.g. "plan failed schema validation: '...' is not of type 'array'").
    m = _SCHEMA_TYPE_MISMATCH_RE.search(error)
    if not m:
        return None
    return m.group(1), m.group(2)


def _detect_schema_missing_field(error: str | None) -> str | None:
    if not error:
        return None
    m = _SCHEMA_REQUIRED_FIELD_RE.search(error)
    return m.group(1) if m else None


# JSON unparseable — covers our own error strings + python's stdlib parser.
_JSON_UNPARSEABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"json\s+unparseable", re.I),
    re.compile(r"JSONDecodeError", re.I),
    re.compile(r"expecting\s+value", re.I),
)


def _looks_like_json_unparseable(error: str | None) -> bool:
    if not error:
        return False
    return any(p.search(error) for p in _JSON_UNPARSEABLE_PATTERNS)


# Step 7's business-rule phase gate (`_validate_plan_against_inventory` in
# s07_test_architect.py) — schema-valid plan that still violates a project
# rule (e.g. a `kind: "assertion"` missing_method with a void-shaped
# signature). The step arms the FULL violation list into
# `ctx.extras["prompt_clarification"]` itself before returning the failed
# `StepResult` (same pattern as TRUNCATION_RECOVERABLE below) because
# `result.notes` is truncated to 5 entries / 500 chars for human display —
# too lossy to hand back to the model as a fix instruction.
def _looks_like_plan_gate_violation(error: str | None) -> bool:
    if not error:
        return False
    return "phase-gate failed" in error.lower()


# Agent produced nothing usable.
def _looks_like_agent_no_output(error: str | None) -> bool:
    if not error:
        return False
    return "agent produced no output" in error.lower()


# Locator resolution — covered by JIT/heal flows already, not auto-retry safe.
def _looks_like_locator_unresolved(error: str | None) -> bool:
    if not error:
        return False
    return "tbd locator" in error.lower() or "locator unresolved" in error.lower()


# Git operation against the SUT failed (commit, checkout, merge, push).
_GIT_FAILURE_RE = re.compile(
    r"\bgit\b.*\b(failed|error|conflict|denied)\b", re.I,
)


def _looks_like_sut_git_failure(error: str | None) -> bool:
    if not error:
        return False
    return bool(_GIT_FAILURE_RE.search(error))


# Pytest collection / import errors — not fixable without editing SUT source.
_TEST_RUNNER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"pytest.*(collection|ImportError|ModuleNotFoundError)", re.I),
    re.compile(r"E\s+ImportError:", re.I),
    re.compile(r"E\s+ModuleNotFoundError:", re.I),
    re.compile(r"INTERNALERROR>", re.I),
)


def _looks_like_test_runner_error(error: str | None) -> bool:
    if not error:
        return False
    return any(p.search(error) for p in _TEST_RUNNER_PATTERNS)


# --- Public entrypoint -----------------------------------------------------


def classify_failure(
    result: StepResult,
    ctx: StepContext | None = None,
) -> ClassificationResult:
    """Classify a failed step result into a `FailureCategory`.

    Strict ordering of checks — earlier rules win on overlap. The order
    follows specificity (most concrete signal first) and recovery value
    (categories with cheap deterministic recoveries first, so we don't
    accidentally classify them as the catch-all):

      1. API-layer sentinels (preserved from existing base.py logic)
      2. Truncation (deterministic recovery: bump max_tokens)
      3. Schema type mismatch (recoverable: prompt clarification)
      4. Schema missing field (recoverable: prompt clarification)
      5. JSON unparseable (recoverable: prompt clarification)
      6. Plan phase-gate violation (recoverable: prompt clarification,
         armed by the step itself)
      7. Agent produced no output (non-recoverable)
      8. Locator unresolved (non-recoverable; defer to JIT/heal)
      9. SUT git failure (non-recoverable)
     10. Test runner error (non-recoverable)
     11. UNKNOWN (catch-all)
    """
    err = result.error

    # 1. API sentinels (preserved verbatim from base.py)
    if _is_api_fatal_error(err):
        return ClassificationResult(
            category=FailureCategory.API_FATAL,
            explanation="upstream API returned a fatal HTTP error",
            safe_to_auto_retry=False,
        )
    if _is_api_retry_storm(err):
        return ClassificationResult(
            category=FailureCategory.API_RETRY_STORM,
            explanation="upstream API entered a retry storm",
            safe_to_auto_retry=False,
        )

    # 2. Truncation
    if _looks_like_truncation(err):
        # The step itself owns the override key (call-site-scoped). The
        # classifier doesn't try to invent a budget — it surfaces the
        # category for audit logs. The recovery happens because the step
        # already armed `ctx.extras` before returning the failure.
        return ClassificationResult(
            category=FailureCategory.TRUNCATION_RECOVERABLE,
            explanation=(
                "agent response truncated by max_tokens;"
                " smart-retry should re-attempt with higher budget"
            ),
            safe_to_auto_retry=True,
            fix_hint=None,  # step already armed its own override
        )

    # 3. Schema type mismatch — extract field + expected type for the hint
    mismatch = _detect_schema_type_mismatch(err)
    if mismatch is not None:
        received, expected = mismatch
        # Trim the received value preview (often a long string body).
        received_preview = received if len(received) <= 80 else received[:77] + "..."
        clarification = (
            f"On the previous attempt, the field with value '{received_preview}' "
            f"was emitted but the schema requires type '{expected}'. "
            f"Re-emit with the correct type."
        )
        return ClassificationResult(
            category=FailureCategory.SCHEMA_TYPE_MISMATCH,
            explanation=(
                f"schema type mismatch — got value of unexpected type, "
                f"expected '{expected}'"
            ),
            safe_to_auto_retry=True,
            fix_hint={"prompt_clarification": clarification},
        )

    # 4. Schema missing required field
    missing = _detect_schema_missing_field(err)
    if missing is not None:
        clarification = (
            f"On the previous attempt, the required field '{missing}' was "
            f"missing from the output. Include '{missing}' in your response."
        )
        return ClassificationResult(
            category=FailureCategory.SCHEMA_MISSING_REQUIRED_FIELD,
            explanation=f"schema missing required field '{missing}'",
            safe_to_auto_retry=True,
            fix_hint={"prompt_clarification": clarification},
        )

    # 5. JSON unparseable
    if _looks_like_json_unparseable(err):
        clarification = (
            "On the previous attempt, the response was not valid JSON. "
            "Respond with a JSON object only — no prose, no markdown fences."
        )
        return ClassificationResult(
            category=FailureCategory.JSON_UNPARSEABLE,
            explanation="agent response was not parseable JSON",
            safe_to_auto_retry=True,
            fix_hint={"prompt_clarification": clarification},
        )

    # 6. Plan phase-gate violation — the step already armed the full
    # violation list into ctx.extras["prompt_clarification"] before
    # returning, so there's nothing to extract here (mirrors
    # TRUNCATION_RECOVERABLE's fix_hint=None convention).
    if _looks_like_plan_gate_violation(err):
        return ClassificationResult(
            category=FailureCategory.PLAN_GATE_VIOLATION,
            explanation=(
                "plan passed schema validation but violated a business "
                "rule (phase gate); next attempt retries with the "
                "specific violations as a prompt clarification"
            ),
            safe_to_auto_retry=True,
            fix_hint=None,
        )

    # 7. Agent no output
    if _looks_like_agent_no_output(err):
        return ClassificationResult(
            category=FailureCategory.AGENT_NO_OUTPUT,
            explanation="agent returned empty / no final_text",
            safe_to_auto_retry=False,
        )

    # 8. Locator unresolved
    if _looks_like_locator_unresolved(err):
        return ClassificationResult(
            category=FailureCategory.LOCATOR_RESOLUTION_TIMEOUT,
            explanation="TBD locator could not be resolved at runtime",
            safe_to_auto_retry=False,
        )

    # 9. SUT git failure
    if _looks_like_sut_git_failure(err):
        return ClassificationResult(
            category=FailureCategory.SUT_GIT_FAILURE,
            explanation="git operation against the SUT clone failed",
            safe_to_auto_retry=False,
        )

    # 10. Test runner error
    if _looks_like_test_runner_error(err):
        return ClassificationResult(
            category=FailureCategory.TEST_RUNNER_ERROR,
            explanation="pytest collection / import failed",
            safe_to_auto_retry=False,
        )

    # 11. Catch-all
    return ClassificationResult(
        category=FailureCategory.UNKNOWN,
        explanation="failure did not match any known category",
        safe_to_auto_retry=False,
    )


def is_recoverable_category(category: FailureCategory) -> bool:
    """True iff the category has a deterministic recovery recipe.

    Convenience used by `pipeline.py` to gate the expensive
    auto-firing fix-proposal chain (debug RCA → critical-thinking →
    principal-software-engineer) — we skip it for categories the classifier
    is already handling deterministically.
    """
    return category in _RECOVERABLE_CATEGORIES


__all__ = [
    "ClassificationResult",
    "FailureCategory",
    # Re-exported sentinels for back-compat with base.py wrappers
    "_is_api_fatal_error",
    "_is_api_retry_storm",
    "classify_failure",
    "is_recoverable_category",
]
