"""Step 9: Execute tests + self-heal.

Workflow:
  1. Load Step 6 research and resolve the SUT tests directory (no mirror —
     Step 9 already wrote files in `<workspace>/sut/` on the qtea branch
     based on the Step 8 plan).
  2. Resolve test-run command (research.commands.test or per-framework default).
  3. Execute via `test_runner.run_tests` with `cwd=<workspace>/sut/`. Capture
     per-test status + attachments.
  4. For each failing test: invoke `polyglot-test-fixer` once with the failing
     test source + traceback. The fixer agent has `add_dirs=[<workspace>/sut/]`
     so it can read SUT helpers / page objects directly and edit the failing
     test file in place. On a successful patch, commit it to the qtea
     branch with one commit per healed test.
  5. Re-run only the failing tests once. Record self-heal outcome per test.
  6. Emit run-results.json + bug-candidates.json + heal-log.jsonl.

Self-heal budget is capped (default 5 tests) to bound runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from qtea._sut_git import commit_step
from qtea.auth_helpers import (
    auth_relevant_sut_files as _auth_relevant_sut_files,
)
from qtea.auth_helpers import (
    auth_summary_for_prompt as _auth_summary_for_prompt,
)
from qtea.claude_runner import run_agent
from qtea.config import (
    HEAL_AGENT_MAX_TURNS,
    HEAL_AGENT_TIMEOUT_S,
    package_resource_root,
    step_timeout,
)
from qtea.hitl import (
    RESOLUTION_OVERLAY_BUG,
    RESOLUTION_OVERLAY_ONCE,
    RESOLUTION_OVERLAY_PERSIST,
    Question,
    prompt_user,
)
from qtea.logging_setup import get_logger
from qtea.overlay_handling import (
    OverlayEvent,
    append_interceptor,
    build_overlay_question_metadata,
    dedup_overlay_events,
    delete_screenshot,
    filter_already_registered,
    load_interceptors,
    load_overlay_events,
    parse_overlay_answer,
    reclassify_bug_candidates,
)
from qtea.proxy import safe_subprocess_env
from qtea.resolver_server import ResolverServer
from qtea.schemas import is_valid
from qtea.stack_profile import PYTHON_VENV_MANAGERS, StackProfile, wrap_command
from qtea.steps.base import Step, StepContext, StepResult
from qtea.test_runner import (
    _PYTEST_PLUGIN_PROVIDERS,
    _PW_TEST_FRAMEWORKS,
    RunResult,
    TestRunEntry,
    execute_command,
    install_command_for,
    prepare_sut,
    resolve_command,
    run_tests,
)

log = get_logger(__name__)

# Cap on number of HEALABLE failing tests we'll attempt to self-heal in a
# single step run. The cap exists as runaway-cost protection: each heal
# invocation spawns a Playwright MCP browser and burns LLM tokens
# (~$0.10-0.50 per heal). The default was 5 historically; bumped to 15 so
# typical small suites (10-15 tests) where the LLM resolver hit a hard UI
# (DSSF-style synthetic CSS classes, hover-only elements) fit inside the
# cap. Tests are pre-filtered by ``_partition_failures`` so this counts
# only locator/timeout class failures; WCAG / TTI / fixture-missing rows
# are excluded from heal entirely and do NOT count against the cap.
_MAX_HEAL_TESTS = int(os.environ.get("QTEA_MAX_HEAL", "15"))

# Pytest -m selector that scopes Step 9 to ONLY the qtea generated tests.
# The codegen agent (`codegen-violation-fixer.agent.md` rule 8) applies one of these
# markers to every generated test based on the planning phase. The vendored
# `tests/qtea_runtime.py` plugin registers them via `pytest_configure` so
# strict-markers runs don't fail. Keep this list in sync with the agent prompt
# and the runtime template's `_QTEA_PHASE_MARKERS`. Operator escape: set
# `QTEA_PYTEST_MARKER` to override (e.g. `""` to disable marker scoping
# and run the SUT's full native suite alongside qtea-generated tests).
_QTEA_PYTEST_MARKER_FILTER = os.environ.get(
    "QTEA_PYTEST_MARKER",
    "qtea_smoke or qtea_regression or qtea_e2e or qtea_exploratory",
)

# Patch-content quality gates (XPath / assertion-immutability / anti-patterns)
# live in a dedicated submodule. Re-exported here so external callers and the
# test suite keep resolving `qtea.steps.s09_execute._foo` at the historical
# dotted path (test files pin these names via `from qtea.steps.s09_execute
# import _foo` and monkeypatch targets like `qtea.steps.s09_execute._foo`).
from qtea.steps.s09.patch_gates import (  # noqa: E402
    _ASSERTION_LINE_PATTERNS,
    _EMPTY_HANDLER_PATTERNS,
    _XPATH_PATTERNS,
    _count_empty_handlers,
    _count_xpath_markers,
    _extract_assertion_lines,
    _patch_has_anti_patterns,
    _patch_introduces_xpath,
    _patch_modifies_assertions,
)


# Heal-scope predicates + git revert helpers live in a dedicated submodule.
# Re-exported here so tests using `monkeypatch.setattr("qtea.steps.s09_execute._foo", ...)`
# and callers using `from qtea.steps.s09_execute import _foo` keep working.
from qtea.steps.s09.heal_scope import (  # noqa: E402
    _git_revert_path,
    _git_status_porcelain,
    _heal_allowlist_dirs,
    _heal_path_in_scope,
    _heal_path_is_forbidden,
    _heal_revert_all_uncommitted,
    _heal_scope_check_and_revert,
)
# Dep-recovery HITL + package-manager install shim live in a dedicated
# submodule. Re-exported here to preserve the historical dotted path.
from qtea.steps.s09.dep_install import (  # noqa: E402
    _POETRY_NOOP_MARKERS,
    _hitl_confirm_dep_install,
    _run_dep_install,
)


# Read-side helpers that load prior-step artifacts + resolve SUT tests dir
# live in a dedicated submodule. Re-exported here to preserve dotted paths.
from qtea.steps.s09.context_loaders import (  # noqa: E402
    _ISOLATED_TESTS_DIR_NAME,
    _active_module,
    _attachment_glob,
    _clean_sut_artifacts,
    _detected_command,
    _framework,
    _load_generated_files,
    _load_index,
    _load_stack_profile,
    _research_payload,
    _sut_tests_dir,
)


# Fixer-agent prompt builder, patch application, and runner-narrowing helpers
# live in a dedicated submodule. Re-exported here to preserve the historical
# dotted path. `_NO_PATCH_SUMMARY` (the summary_text sentinel that gates the
# real-bug classification on the ExecuteStep side) also lives there.
from qtea.steps.s09.fixer_prompt import (  # noqa: E402
    _NO_PATCH_SUMMARY,
    _apply_fixer_outputs,
    _build_fixer_prompt,
    _filter_command_for_tests,
    _narrow_command_to_ids,
)


# Attempt-N state persistence + install-signature fingerprinting live in a
# dedicated submodule. Re-exported here to preserve the historical dotted path.
from qtea.steps.s09.attempt_state import (  # noqa: E402
    _attempt_state_path,
    _compute_install_sig,
    _load_attempt_state,
    _save_attempt_state,
)


# Failure classification + _failing_tests / _build_bug_candidates live in a
# dedicated submodule (failure_class heuristics, real-bug bucketing).
# Re-exported here to preserve the historical dotted path used by tests
# (`from qtea.steps.s09_execute import _failing_tests`, etc.).
from qtea.steps.s09.failure_class import (  # noqa: E402
    _CLASSIFY_PATTERNS,
    _FAILURE_CLASS_HEALABLE,
    _build_bug_candidates,
    _classify_failure,
    _failing_tests,
    _partition_failures,
)

# JIT locator-cache dev-pool prewarm + resolver-spend telemetry summarizer
# live in a dedicated submodule. Re-exported here to preserve dotted paths.
from qtea.steps.s09.jit_prewarm import (  # noqa: E402
    _prewarm_jit_cache_dev_pool,
    _summarize_resolver_spend,
)


# JIT resolver HITL escalation for unresolvable TBD sentinels lives in a
# dedicated submodule. Re-exported here to preserve the historical dotted path.
from qtea.steps.s09.jit_hitl import (  # noqa: E402
    _append_resolved_to_dev_locators,
    _collect_hitl_pending,
    _hitl_resolve_unresolvable,
)


# End-of-attempt overlay-event sweep + HITL persistence lives in a dedicated
# submodule. Re-exported here to preserve the historical dotted path.
from qtea.steps.s09.overlay_sweep import (  # noqa: E402
    _hitl_overlay_sweep,
    _interceptors_path,
    _overlay_events_path,
)


# TBD-sentinel promotion (rewrites tbd("intent") calls end-of-attempt) lives
# in a dedicated submodule. Re-exported here to preserve the historical
# dotted path used by tests (test_promote_tbd_gating imports these via
# `from qtea.steps.s09_execute import _promote_resolved_tbds`).
from qtea.steps.s09.tbd_promotion import (  # noqa: E402
    _ensure_runtime_imports,
    _format_promoted_substitution,
    _promote_resolved_tbds,
)


# JIT-resolver bug-candidate emitters (dev-pool drift + unresolvable TBDs)
# live in a dedicated submodule. Re-exported here to preserve the historical
# dotted path used by tests (test_dev_pool_quarantine imports these via
# `from qtea.steps.s09_execute import _bug_candidates_for_dev_pool_drift`).
from qtea.steps.s09.bug_candidates_ext import (  # noqa: E402
    _bug_candidates_for_dev_pool_drift,
    _bug_candidates_for_unresolvable_tbds,
)


# Playwright MCP server lazy-probe (warms npx cache before first heal)
# lives in a dedicated submodule. Re-exported here to preserve the
# monkey-patch path used by tests/unit/test_mcp_preflight_lazy.py, which
# patches `qtea.steps.s09_execute._lazy_probe_heal_mcp` directly.
from qtea.steps.s09.mcp_probe import _lazy_probe_heal_mcp  # noqa: E402


def _compose_runner_stream_diagnostics(
    stderr: str | None, stdout: str | None,
) -> str:
    """Compose the stderr/stdout appendix for `result.error` on a
    runner-only failure.

    Both streams are surfaced (not `if stderr elif stdout` — Playwright's
    JSON reporter writes diagnostics to stdout, and the runtime's benign
    `qtea {"event":"installed"}` marker can fill stderr and mask stdout).
    Long streams get HEAD + TAIL slices because parse errors
    (`Unexpected token (1:0)`) and module-not-found errors surface at the
    head, not the tail — the prior tail-only truncation dropped exactly
    the line the debug agent needed.

    See run 20260701-114656-9394eb for the incident.
    """
    parts: list[str] = []
    stderr_s = (stderr or "").strip()
    stdout_s = (stdout or "").strip()
    if stderr_s:
        if len(stderr_s) <= 3000:
            parts.append(f"\n\n--- stderr ---\n{stderr_s}")
        else:
            parts.append(f"\n\n--- stderr HEAD (first 1500) ---\n{stderr_s[:1500]}")
            parts.append(f"\n\n--- stderr TAIL (last 1500) ---\n{stderr_s[-1500:]}")
    if stdout_s:
        if len(stdout_s) <= 3000:
            parts.append(f"\n\n--- stdout ---\n{stdout_s}")
        else:
            parts.append(f"\n\n--- stdout HEAD (first 1500) ---\n{stdout_s[:1500]}")
            parts.append(f"\n\n--- stdout TAIL (last 1500) ---\n{stdout_s[-1500:]}")
    return "".join(parts)


@dataclasses.dataclass
class _PostRunState:
    """State handed from Step 9's heal-loop phase to ``_finalize_and_report``.

    Groups the ~11 locals the finalization phase reads so the call site is a
    single argument. All fields are read-only from ``_finalize_and_report``'s
    perspective except ``first.results``, which the attachment-glob step
    mutates in-place before the payload snapshot.
    """
    first: RunResult
    self_heal_meta: dict[str, dict]
    test_runner_invocations: int
    patches_applied: int
    patches_rejected: int
    framework: str
    jit_cache_dir: Path
    install_sig: str | None
    prior_state: dict | None
    current_attempt: int
    runtime_env: dict


@dataclasses.dataclass
class _BootstrapResult:
    """State produced by :meth:`ExecuteStep._bootstrap` and consumed by the
    rest of ``run()``.

    Most fields are read-only from the caller's perspective, but a few
    (``stack_profile``, ``install_sig``, ``runtime_env``) get updated later
    by dep-recovery and the post-run storage-state re-resolve, so the
    caller reassigns via ``boot.<field> = ...`` at those spots.
    """
    out_dir: Path
    heal_log_path: Path
    current_attempt: int
    prior_state: dict | None
    research: dict
    framework: str
    detected_cmd: str | None
    sut_tests: Path
    active_module: dict | None
    runtime_env: dict
    jit_cache_dir: Path
    jit_runtime_vendored: bool
    resolver_server: object | None  # ResolverServer | None
    heal_mcp_env: dict
    storage_state_mod: object  # the qtea.storage_state module
    storage_state_path: Path | None
    install_log_path: Path
    stack_profile: StackProfile | None
    install_sig: str | None
    skip_install: bool
    no_auto_deps: bool


class ExecuteStep(Step):
    number = 9
    name = "execute"
    timeout_s = step_timeout(9)
    # Playwright MCP is only consumed by the `polyglot-test-fixer` heal
    # agent (`enable_mcp=True` call site below). Heal only runs when the
    # first test pass produces failing tests AND those failures aren't
    # synthetic runner-failure entries. On green runs (all tests pass,
    # or runner_only_failure short-circuits) the agent is never spawned,
    # so we'd be paying the 5-15s MCP probe + npx-cache warmup for no
    # benefit. Probing lazily inside :meth:`run` (just before the first
    # heal call) keeps the warmup contiguous with the SDK spawn (the
    # `pending` race the eager probe was meant to avoid) without taxing
    # green runs.
    mcp_servers_required: frozenset[str] = frozenset()
    # Server name probed lazily. Kept as a class constant so tests can
    # override without monkey-patching string literals.
    _LAZY_MCP_SERVER: str = "playwright"

    def pre_attempt_cleanup(self, ctx: StepContext, attempt: int) -> None:
        """Rotate ``heal-log.jsonl`` so attempt 2 doesn't append on top of
        attempt 1's entries. Step 9 reads only the current heal-log; prior
        attempts are archived to ``heal-log.attempt-N.jsonl`` so the forensic
        trail survives without polluting the classifier's input."""
        out_dir = self.out_dir(ctx.workspace)
        heal_log = out_dir / "self-heal" / "heal-log.jsonl"
        if heal_log.exists() and heal_log.stat().st_size > 0:
            prior = attempt - 1
            archive = out_dir / "self-heal" / f"heal-log.attempt-{prior}.jsonl"
            try:
                heal_log.rename(archive)
                log.info(
                    "step09.heal_log_rotated",
                    archived_attempt=prior,
                    archive_path=str(archive),
                )
            except OSError as e:
                log.warning("step09.heal_log_rotate_failed", error=str(e))

    def _finalize_and_report(
        self, ctx: StepContext, out_dir: Path, state: _PostRunState,
    ) -> StepResult:
        """Post-run pipeline: attach late artifacts, assemble payload, write
        artifacts, run TBD promotion + HITL sweeps + bug-candidate build,
        then determine status.

        Extracted from :meth:`run` (Tier 2) so the finalization phase is
        legible as a single call site instead of ~320 inline lines. Every
        branch preserves the original behaviour byte-for-byte.
        """
        # Attach SUT-side artifacts discovered post-run to entries without any.
        # Keep only the newest file per artifact type so each failed test
        # gets at most one screenshot, one trace, one video.
        extra_attachments = _attachment_glob(ctx.workspace.sut)
        if extra_attachments:
            newest_by_type: dict[str, dict] = {}
            for a in extra_attachments:
                kind = a.get("type", "other")
                prev = newest_by_type.get(kind)
                if prev is None:
                    newest_by_type[kind] = a
                    continue
                try:
                    if Path(a["path"]).stat().st_mtime > Path(prev["path"]).stat().st_mtime:
                        newest_by_type[kind] = a
                except OSError:
                    pass
            deduped = list(newest_by_type.values())
            for r in state.first.results:
                if r.status in ("failed", "error") and not r.attachments:
                    r.attachments = deduped

        # Annotate self-heal metadata into per-entry dicts via the serializer.
        payload = state.first.as_dict()
        for entry_dict in payload["results"]:
            meta = state.self_heal_meta.get(entry_dict["id"])
            if meta:
                entry_dict["self_heal"] = meta
        payload["self_heal"] = {
            # Historical key name — external consumers (Step 10, Step
            # 11, report renderer) depend on this. Populated from the
            # renamed local `test_runner_invocations` for clarity in
            # this file.
            "attempts": state.test_runner_invocations,
            "patches_applied": state.patches_applied,
            "patches_rejected": state.patches_rejected,
        }

        # Resolver telemetry (Phase 6). Per-run only — no global
        # aggregator. Absent for non-JIT stacks (no spend file).
        resolver_spend = _summarize_resolver_spend(state.jit_cache_dir)
        if resolver_spend is not None:
            payload["resolver_spend"] = resolver_spend
            log.info(
                "step09.resolver_spend_summarised",
                total_resolutions=resolver_spend["total_resolutions"],
                total_input_tokens=resolver_spend["total_input_tokens"],
                total_output_tokens=resolver_spend["total_output_tokens"],
            )

        run_results_path = out_dir / "run-results.json"
        run_results_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok_schema, schema_err = is_valid(payload, "run-results")
        if not ok_schema:
            log.warning("step09.schema_invalid", error=schema_err)

        # Tasks 2 + 4 + 5: persist this attempt's outcomes for any
        # subsequent retry. ``no_patch_ids`` is the running union with
        # the prior attempt's so multi-attempt convergence works: once
        # a test is classified as a real bug it stays skipped on every
        # later attempt without needing each attempt to rediscover it.
        _now_failing = [
            (r.id, r.name) for r in state.first.results
            if r.status in ("failed", "error")
        ]
        _now_no_patch = {
            tid for tid, meta in state.self_heal_meta.items()
            if not meta.get("applied")
            and meta.get("summary") == _NO_PATCH_SUMMARY
        }
        if state.prior_state:
            _now_no_patch |= set(state.prior_state.get("no_patch_ids", []) or [])
        _save_attempt_state(
            out_dir, state.current_attempt,
            failing=_now_failing,
            no_patch_ids=sorted(_now_no_patch),
            install_sig=state.install_sig,
        )

        # TBD promotion: any tbd("intent") sentinels whose intent now has a
        # cached selector get replaced in-place in the SUT source files and
        # committed, so the code is self-sufficient without the JIT plugin.
        _promoted, _promotion_blocked = _promote_resolved_tbds(
            ctx.workspace.sut,
            state.jit_cache_dir / "locator-cache.json",
        )
        if _promoted:
            log.info("step09.tbd_promoted", count=len(_promoted), files=_promoted)
            commit_step(ctx.workspace.sut, 9, self.name, "tbd-promotion")
        if _promotion_blocked:
            log.info(
                "step09.tbd_promotion_blocked",
                count=len(_promotion_blocked),
                reasons=sorted({b["reason"] for b in _promotion_blocked}),
            )

        # HITL escalation pass. The JIT runtime drops `hitl-pending-*.json`
        # files in the cache dir whenever it could not resolve a TBD.
        # On a TTY (and unless --no-hitl) we prompt for a selector and
        # write it to dev-locators.json so the next run skips Tier 4 for
        # that key; otherwise the unresolved TBDs flow into the bug
        # candidates as `locator-unresolvable` entries for Step 9.
        hitl_pendings = _collect_hitl_pending(state.jit_cache_dir)
        hitl_dev_locators_path = Path(state.runtime_env["QTEA_DEV_LOCATORS"])
        _, hitl_remaining = _hitl_resolve_unresolvable(
            hitl_pendings,
            dev_locators_path=hitl_dev_locators_path,
            no_hitl=bool(getattr(ctx.options, "no_hitl", False)),
        )

        # Overlay auto-dismiss sweep (Layer 3). Runtime dropped events
        # to <workspace>/overlay-events.jsonl for popups it couldn't
        # safely auto-dismiss. Prompt the operator; persist chosen
        # dismiss actions to <sut>/.qtea/interceptors.json so future
        # runs are clean. Best-effort — never blocks Step 9.
        _overlay_events, _overlay_persisted = _hitl_overlay_sweep(
            ctx.workspace.root,
            ctx.workspace.sut,
            no_hitl=bool(getattr(ctx.options, "no_hitl", False)),
        )

        # bug-candidates.json: emitted regardless (empty list when no failures).
        final_failing = _failing_tests(state.first)
        bug_payload = _build_bug_candidates(final_failing)
        # Layer 4 — reclassify overlay-caused failures so Step 10's
        # bug-classifier doesn't file them as defects. Entries with
        # a persisted interceptor are marked overlay_handled_next_run
        # (next run is clean); the rest are overlay_pending_hitl.
        if _overlay_events:
            # First pass: mark PERSISTED as handled_next_run.
            if _overlay_persisted:
                persisted_events = [
                    ev for ev in _overlay_events
                    if (ev.overlay_role, ev.overlay_name) in _overlay_persisted
                ]
                bug_payload["candidates"] = reclassify_bug_candidates(
                    bug_payload["candidates"],
                    persisted_events,
                    persisted_after_hitl=True,
                )
            # Second pass: mark UNPERSISTED as pending_hitl.
            unpersisted_events = [
                ev for ev in _overlay_events
                if (ev.overlay_role, ev.overlay_name) not in _overlay_persisted
            ]
            if unpersisted_events:
                bug_payload["candidates"] = reclassify_bug_candidates(
                    bug_payload["candidates"],
                    unpersisted_events,
                    persisted_after_hitl=False,
                )
        bug_payload["candidates"].extend(
            _bug_candidates_for_unresolvable_tbds(
                hitl_remaining, dev_locators_path=hitl_dev_locators_path,
            )
        )
        # Promotion gate emits structured candidates for entries that
        # had a cached resolution but couldn't be safely frozen into
        # source (no passing-test witness, malformed selector, or
        # unrepresentable payload). Surface them so reviewers see what
        # the JIT runtime is still chewing on between runs.
        if _promotion_blocked:
            bug_payload["candidates"].extend(_promotion_blocked)
        # Dev-pool drift candidates: one per dev-locators entry whose
        # selector failed at action time this run. The JIT runtime
        # quarantined them and stored an LLM fallback under a shadow
        # cache key; the user owns updating the dev file.
        _drift_candidates = _bug_candidates_for_dev_pool_drift(
            state.jit_cache_dir / "dev-pool-quarantine.jsonl",
        )
        if _drift_candidates:
            bug_payload["candidates"].extend(_drift_candidates)
        bug_path = out_dir / "bug-candidates.json"
        bug_path.write_text(
            json.dumps(bug_payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # JIT cache publish — if the runtime plugin populated locator-cache.json
        # during the test run, copy it into artifacts/step09 so step 11 can
        # surface per-TBD resolution sources in the report. Best-effort; absence
        # is normal for non-JIT stacks.
        jit_cache_src = ctx.workspace.root / "locator-cache" / "locator-cache.json"
        if jit_cache_src.exists():
            try:
                jit_cache_dst = out_dir / "locator-cache.json"
                jit_cache_dst.write_text(
                    jit_cache_src.read_text(encoding="utf-8"), encoding="utf-8"
                )
                log.info("step09.jit_cache_published", entries_path=str(jit_cache_dst))
            except OSError as e:
                log.warning("step09.jit_cache_publish_failed", error=str(e))

        # Counts come from `totals` (Fix 7): `tests` excludes synthetic
        # T-runner-failure entries; `infrastructure_errors` is reported
        # separately so a green-looking `tests=N` cannot conceal a run
        # that never executed a single real test.
        totals = payload["totals"]
        notes_parts = [
            f"framework={state.framework}",
            f"tests={totals['tests']}",
            f"failed={totals['failed']}",
            f"errors={totals['errors']}",
            f"infra_errors={totals.get('infrastructure_errors', 0)}",
            f"attempts={state.test_runner_invocations}",
            f"healed={state.patches_applied}",
        ]
        notes = " ".join(notes_parts)

        # Status semantics:
        #   - `completed` when nothing failed.
        #   - `failed` when EVERY result is a synthesised `T-runner-failure`
        #     (the test runner didn't even produce parseable output — typically
        #     a conftest import error, missing dep, exit code 4, etc.). The
        #     prior "warned" status hid this and Step 9/11 ran on garbage;
        #     Step 10 then crashed rendering an environment-bug card.
        #     This is an environment failure, not a real test failure.
        #   - `failed` when ALL tests errored/failed and NONE passed — no
        #     assertion was ever evaluated, so there is nothing to classify.
        #   - `warned` when some tests passed and some failed/errored —
        #     Step 10 will classify the failures as bug candidates.
        runner_only_failure = (
            len(state.first.results) > 0
            and all(r.id == "T-runner-failure" for r in state.first.results)
        )
        # Defensive: exit code 3 (pytest internal error) with no
        # passed/failed tests is also an environment failure, even
        # if the JUnit entries don't all carry T-runner-failure IDs.
        if not runner_only_failure and state.first.exit_code == 3:
            real_passed_or_failed = any(
                r.status in ("passed", "failed")
                and r.id != "T-runner-failure"
                for r in state.first.results
            )
            if not real_passed_or_failed:
                runner_only_failure = True
        if runner_only_failure:
            first_entry = state.first.results[0]
            first_msg = (first_entry.message or "").strip() or "test runner failed"
            rf = first_entry.runner_failure or {}
            # When the classifier identified a specific failure mode
            # (missing module, collection error), lead the error with the
            # actionable fix command. Otherwise fall through to the raw
            # message tail — at least the operator gets the stderr summary.
            if rf:
                error = (
                    f"test runner failed before any test could run: "
                    f"{rf.get('summary', 'collection / import error')}. "
                    f"To fix: {rf.get('hint', 'see stderr in run-results.json')}. "
                    f"(exit_code={state.first.exit_code})"
                )
            else:
                error = (
                    f"test runner produced no parseable test results "
                    f"(exit_code={state.first.exit_code}). This is an environment "
                    f"failure, not a real test failure. {first_msg[:300]}"
                )
            error += _compose_runner_stream_diagnostics(
                first_entry.stderr, first_entry.stdout,
            )
            return StepResult(
                success=False,
                status="failed",
                outputs=[run_results_path, bug_path],
                error=error,
                notes=notes,
            )

        # All-tests-errored gate: when every test errored/failed and
        # none passed, no assertion was evaluated. This is functionally
        # equivalent to a runner failure (e.g. DNS unreachable, auth
        # fixture crash, SUT down) and should not be masked as "warned".
        any_passed = any(
            r.status == "passed" for r in state.first.results
        )
        if final_failing and not any_passed:
            first_entry = final_failing[0]
            msg_snippet = (first_entry.message or "").strip()[:300]
            error_counts: dict[str, int] = {}
            for r in state.first.results:
                error_counts[r.status] = error_counts.get(r.status, 0) + 1
            status_breakdown = ", ".join(
                f"{v} {k}" for k, v in sorted(error_counts.items())
            )
            setup_failures = [
                r for r in state.first.results
                if r.message and "failed on setup" in r.message
            ]
            hint = ""
            if setup_failures:
                hint = (
                    " Likely cause: shared fixture/setup failure blocking "
                    "all tests — check conftest.py and fixture code."
                )
            return StepResult(
                success=False,
                status="failed",
                outputs=[run_results_path, bug_path],
                error=(
                    f"all {len(state.first.results)} test(s) errored with zero "
                    f"passing ({status_breakdown}) — no assertion was "
                    f"evaluated.{hint} First error: {msg_snippet}"
                ),
                notes=notes,
            )

        sub_status = "all_passed" if not final_failing else "bugs_found"
        return StepResult(
            success=True,
            status="completed",
            sub_status=sub_status,
            outputs=[run_results_path, bug_path],
            notes=notes,
        )

    def _bootstrap(self, ctx: StepContext) -> _BootstrapResult | StepResult:
        """Preflight guards, workspace/artifact setup, prior-attempt load,
        research load, runtime-env assembly, storage-state resolution,
        install (``prepare_sut``), venv detection, Playwright browser
        install, pre-install of known-safe missing deps, resolver-server
        startup, and JIT cache dev-pool prewarm.

        Returns a :class:`_BootstrapResult` on success, or a :class:`StepResult`
        when the run must abort early: SUT missing, .git missing, step-8
        manifest missing, or the SUT install command fails.

        Extracted from :meth:`run` (Tier 2) so the phased contract between
        bootstrap → heal-loop → finalize is legible. Every branch preserves
        the original behaviour byte-for-byte.
        """
        out_dir = self.out_dir(ctx.workspace)
        out_dir.mkdir(parents=True, exist_ok=True)
        heal_log_path = out_dir / "self-heal" / "heal-log.jsonl"
        heal_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Cross-attempt narrowing (Tasks 2 + 4 + 5). When this is a retry,
        # we can:
        #   - skip the SUT install (Task 4 — lockfile hasn't changed; heal
        #     commits don't touch lockfiles);
        #   - narrow the initial test run to only the previously-failing
        #     tests minus the ones the prior heal flagged as real bugs
        #     (Tasks 2 + 5);
        #   - short-circuit the heal call for those real-bug tests so the
        #     LLM doesn't burn cost re-discovering "nothing to fix".
        #
        # ``record.attempts`` is incremented in ``base.py:_attempt`` BEFORE
        # ``run()`` is called, so a value > 1 is a true retry.
        _record = ctx.state.steps.get(self.number)
        _current_attempt = _record.attempts if _record else 1
        _prior_state: dict | None = None
        if _current_attempt > 1:
            _prior_state = _load_attempt_state(out_dir, _current_attempt - 1)
            if _prior_state:
                log.info(
                    "step09.prior_attempt_state_loaded",
                    attempt=_current_attempt,
                    prior_failing=len(_prior_state.get("failing_ids", [])),
                    prior_no_patch=len(_prior_state.get("no_patch_ids", [])),
                )

        # Pre-flight: SUT must be present + on the qtea branch. Step 8
        # already wrote into it; step 9 runs tests + heals against it.
        if not ctx.workspace.sut.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error="SUT not materialized (run from step 1 to re-clone)",
            )
        if not (ctx.workspace.sut / ".git").exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    f"SUT at {ctx.workspace.sut} is not a git repo — the "
                    f"qtea branch is missing. Re-run from step 1."
                ),
            )

        # Step 8 committed qtea_*-prefixed files into the SUT on the
        # qtea branch. We don't need a separate codegen_root anymore;
        # the SUT itself is the source of truth.
        # Sanity-check that step 8's manifest exists so we fail fast when
        # someone runs --only-step 9 on a fresh workspace.
        step8_manifest = ctx.workspace.step_dir(8) / "generated-files.json"
        if not step8_manifest.exists():
            return StepResult(
                success=False,
                status="failed",
                outputs=[],
                error=(
                    "step 9 requires step 8's generated-files.json manifest. "
                    "Run step 8 first (drop --only-step 9, or use "
                    "--from-step 8)."
                ),
            )

        research = _research_payload(ctx)
        index = _load_index(ctx)
        framework = _framework(research, index)
        detected_cmd = _detected_command(research)

        sut_env_keys = research.get("sut_env_keys") or []
        if sut_env_keys:
            missing = [k for k in sut_env_keys if k not in os.environ]
            if missing:
                env_res = research.get("env_resolution")
                if env_res:
                    log.warning(
                        "step09.env_missing",
                        keys=missing,
                        strategies_tried=list(env_res.get("sources", {}).values()),
                        hint="These keys were not resolved by any strategy in Step 6. "
                             "Provide them via --env-file, host environment, or "
                             "Azure DevOps Variable Groups.",
                    )
                else:
                    log.warning("step09.env_missing", keys=missing)

        # Resolve where qtea-generated tests live inside the SUT (steps 7+8
        # already wrote them there). No copy — just compute the path.
        sut_inventory = research.get("sut_inventory") or {}
        active_module = _active_module(sut_inventory)
        isolated = bool(getattr(ctx.options, "isolated_tests", False))
        sut_tests = _sut_tests_dir(
            ctx.workspace.sut,
            active_module=active_module,
            isolated=isolated,
        )
        log.info(
            "step09.tests_resolved",
            destination=str(sut_tests),
            active_module=(active_module or {}).get("name"),
            isolated=isolated,
        )
        runtime_env = {"QTEA_TESTS_DIR": str(sut_tests)}

        # JIT runtime plugin env wiring. The vendored `tests/qtea_runtime.py`
        # (when present) reads these vars to discover the cache, optional
        # dev-supplied locator file, resolver port/token, and timeout defaults.
        # SECURITY: ANTHROPIC_API_KEY is deliberately NOT re-exported here —
        # safe_subprocess_env() strips it because the pytest subprocess executes
        # untrusted SUT test code that could exfiltrate the key via os.environ.
        # The LLM resolver path works WITHOUT the key in the SUT env because
        # the parent process (here in step 9) starts a ResolverServer on a
        # local loopback port; the pytest plugin reaches it via the short-lived
        # per-run QTEA_RESOLVER_TOKEN (set further down, once the server
        # binds a port). Tier order at runtime: dev-locators → cache →
        # in-process heuristic → ResolverServer (LLM) → HITL/fail-fast.
        jit_cache_dir = ctx.workspace.root / "locator-cache"
        # Dir creation deferred to the `if jit_runtime_vendored:` branch below —
        # non-Playwright stacks never load the vendored runtime, so they don't
        # need the dir and shouldn't pollute the workspace with an empty one.
        # All write sites (runtime template's _write_cache / _append_spend_line /
        # _write_hitl_pending and jit_resolver.write_cache) mkdir on demand.
        runtime_env["QTEA_CACHE_DIR"] = str(jit_cache_dir)
        runtime_env["QTEA_RUN_ID"] = ctx.workspace.run_id
        resolver_model = os.environ.get("QTEA_RESOLVER_MODEL")
        if resolver_model:
            runtime_env["QTEA_RESOLVER_MODEL"] = resolver_model
        timeout_ms = os.environ.get("QTEA_DEFAULT_TIMEOUT_MS")
        if timeout_ms:
            runtime_env["QTEA_DEFAULT_TIMEOUT_MS"] = timeout_ms
        # Dev-locators file: --dev-locators CLI flag wins; env var next; default
        # to workspace/locator-cache/dev-locators.json so HITL answers are never
        # written into the SUT (they're run-workspace artifacts, not SUT source).
        dev_locators_opt = getattr(ctx.options, "dev_locators", None)
        if dev_locators_opt:
            runtime_env["QTEA_DEV_LOCATORS"] = str(dev_locators_opt)
        elif os.environ.get("QTEA_DEV_LOCATORS"):
            runtime_env["QTEA_DEV_LOCATORS"] = os.environ["QTEA_DEV_LOCATORS"]
        else:
            runtime_env["QTEA_DEV_LOCATORS"] = str(jit_cache_dir / "dev-locators.json")

        # Workspace dir for the runtime plugin's same-run storage-state auto-
        # capture (Use case B in storage_state.py). The plugin reads this on
        # first passing test to know where to write storage-state.json.
        runtime_env["QTEA_WORKSPACE_DIR"] = str(ctx.workspace.root)

        # Overlay auto-dismiss registry. Runtime consults this file at
        # BrowserContext creation and registers `page.add_locator_handler()`
        # for every entry — known popups become invisible on every run
        # without HITL after first encounter. Location is per-SUT so the
        # dismissal patterns are shared across the team. See
        # docs/qa-orchestrator.instructions.md and
        # `<sut>/.qtea/interceptors.json` schema.
        runtime_env["QTEA_INTERCEPTORS"] = str(_interceptors_path(ctx.workspace.sut))
        # Feature-flag pass-through — QTEA_OVERLAY_HANDLING=0 disables all
        # overlay code paths (detection, heuristic, JSONL writes,
        # add_locator_handler registration, sweep).
        _overlay_flag = os.environ.get("QTEA_OVERLAY_HANDLING")
        if _overlay_flag is not None:
            runtime_env["QTEA_OVERLAY_HANDLING"] = _overlay_flag

        # Storage state for Playwright MCP injection. Resolved against the
        # 4-tier precedence (CLI flag > env > SUT convention path > workspace
        # auto-capture). When set, _heal_mcp_env carries the
        # ``--storage-state=<path>`` flag for `.mcp.json` token substitution;
        # when unset, the empty arg is filtered by mcp_manager.
        from qtea import storage_state as _storage_state_mod
        _storage_state_path = _storage_state_mod.resolve(
            sut_root=ctx.workspace.sut,
            workspace_root=ctx.workspace.root,
            cli_opt=getattr(ctx.options, "storage_state", None),
        )
        _heal_mcp_env = {
            "QTEA_STORAGE_STATE_ARG": _storage_state_mod.to_mcp_arg(_storage_state_path),
            "QTEA_MCP_USER_DATA_DIR_ARG": (
                f"--user-data-dir={ctx.workspace.root / 'playwright-mcp'}"
            ),
        }
        if _storage_state_path is not None:
            runtime_env["QTEA_STORAGE_STATE"] = str(_storage_state_path)
            log.info(
                "step09.storage_state_resolved",
                path=_storage_state_mod.mask_path(_storage_state_path),
            )

        # Prepare SUT: run the deterministically-detected install command
        # (poetry install / npm ci / mvn install / ...). Idempotent at the
        # package-manager level; the cost on warm runs is a few seconds and
        # buys us "tests work on a fresh clone." Failure here aborts before
        # we burn the test budget — broken deps invariably manifest as
        # opaque test errors that the self-heal loop cannot recover from.
        # install_log_path is bound unconditionally so the auto-install
        # paths below can append to it regardless of whether prepare_sut ran.
        install_log_path = out_dir / "install.log"
        stack_profile = _load_stack_profile(ctx)
        log.info(
            "step09.stack_profile",
            package_manager=stack_profile.package_manager if stack_profile else None,
            pre_install=stack_profile.pre_install_command if stack_profile else None,
            install=stack_profile.install_command if stack_profile else None,
            wrapper=stack_profile.wrapper_prefix if stack_profile else None,
            venv_path=stack_profile.venv_path if stack_profile else None,
            detection_signal=stack_profile.detection_signal if stack_profile else None,
        )
        # Task 4: skip install on retry when the dependency state is byte-
        # identical to the attempt that ran the install. Lockfile mtime +
        # size + package_manager give us a cheap-but-sufficient signature.
        install_sig = _compute_install_sig(ctx.workspace.sut, stack_profile)
        _skip_install = (
            _prior_state is not None
            and install_sig is not None
            and _prior_state.get("install_sig") == install_sig
        )
        if stack_profile and stack_profile.install_command and not _skip_install:
            prep = prepare_sut(
                stack_profile,
                cwd=ctx.workspace.sut,
                timeout_s=900,
            )
            install_log_path.write_text(
                f"$ {prep.command}\n\n# STDOUT\n{prep.stdout}\n\n# STDERR\n{prep.stderr}\n",
                encoding="utf-8",
            )
            if not prep.ok():
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[install_log_path],
                    error=(
                        f"SUT install failed: `{prep.command}` exited with "
                        f"{prep.exit_code}. See install.log."
                    ),
                )
            log.info(
                "step09.install_done",
                command=prep.command,
                duration_s=prep.duration_s,
            )
        elif _skip_install:
            log.info(
                "step09.install_skipped_retry",
                attempt=_current_attempt,
                install_sig=install_sig,
                reason="dependency signature matches prior attempt",
            )

        # Venv detection / wrapper_prefix swap must STILL fire even when
        # install was skipped — pytest + playwright invocations downstream
        # rely on ``stack_profile.wrapper_prefix`` pointing at the venv
        # bin dir. Without this branch, attempt 2 would fall back to
        # ``poetry run pytest`` (slow path) instead of ``.venv/bin/pytest``.
        if stack_profile and stack_profile.venv_path:
            venv_abs = ctx.workspace.sut / stack_profile.venv_path
            log.info(
                "step09.venv_check",
                venv_path=str(venv_abs),
                exists=venv_abs.exists(),
                is_dir=venv_abs.is_dir() if venv_abs.exists() else False,
            )
            if venv_abs.exists():
                # Bypass poetry's venv resolution for all subsequent
                # commands (playwright install, pytest). After
                # prepare_sut created .venv, invoke directly via its
                # bin dir — equivalent to activating the venv.
                bin_dir = str(
                    venv_abs / ("Scripts" if os.name == "nt" else "bin")
                )
                stack_profile = dataclasses.replace(
                    stack_profile,
                    wrapper_prefix=bin_dir,
                    package_manager="pip",
                )
                log.info("step09.venv_activated", bin_dir=bin_dir)

        # Playwright stacks need browser binaries installed after the
        # package install. Idempotent — skips if already present.
        _PW_FRAMEWORKS = {"playwright-py", "playwright-ts", "playwright-js", "playwright-java"}
        if stack_profile and framework in _PW_FRAMEWORKS:
            pw_cmd = wrap_command(stack_profile, "playwright install chromium")
            log.info("step09.playwright_install", command=pw_cmd)
            rc, out, err, _dur = execute_command(
                pw_cmd, cwd=ctx.workspace.sut, timeout_s=400,
                isolate_venv=bool(
                    (stack_profile.package_manager or "").lower()
                    in PYTHON_VENV_MANAGERS
                ),
            )
            with install_log_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n$ {pw_cmd}\n# exit_code: {rc}\n"
                    f"# STDOUT\n{out}\n\n# STDERR\n{err}\n"
                )
            if rc != 0:
                log.warning(
                    "step09.playwright_install_failed",
                    exit_code=rc, stderr=err[:300],
                )

        if stack_profile is None:
            log.warning(
                "step09.no_stack_profile",
                hint="step06 stack_profile.json missing; running tests with "
                     "bare framework command (no package-manager wrapper).",
            )

        # Pre-install known-safe missing deps surfaced by Step 6's audit
        # (research.dependency_warnings). High-confidence entries (`known`
        # confidence — module is in the curated _PYTEST_PLUGIN_PROVIDERS
        # table) get installed and committed up-front so the first pytest
        # run doesn't blow up at collection. `guessed` entries are logged
        # only — the runtime recovery path handles them with a HITL prompt
        # if/when pytest actually fails on them.
        no_auto_deps = bool(getattr(ctx.options, "no_auto_deps", False))
        dep_warnings = research.get("dependency_warnings") or []
        for w in (w for w in dep_warnings if w.get("confidence") == "guessed"):
            log.warning(
                "step09.dep_warning_unverified",
                module=w.get("module"),
                suggested_package=w.get("suggested_package"),
                source_file=w.get("source_file"),
                hint=w.get("suggested_install"),
            )
        known_warnings = [
            w for w in dep_warnings
            if w.get("confidence") == "known" and w.get("suggested_package")
        ]
        if known_warnings and no_auto_deps:
            log.warning(
                "step09.auto_install_skipped",
                reason="--no-auto-deps",
                packages=[w["suggested_package"] for w in known_warnings],
            )
        elif known_warnings:
            pkg_mgr = stack_profile.package_manager if stack_profile else None
            installed_pre: list[str] = []
            for w in known_warnings:
                ok, summary = _run_dep_install(
                    pkg_mgr, w["suggested_package"],
                    ctx.workspace.sut, install_log_path,
                    profile=stack_profile,
                )
                log.info(
                    "step09.auto_install_dep",
                    phase="pre",
                    module=w["module"],
                    package=w["suggested_package"],
                    confidence="known",
                    success=ok,
                    summary=summary,
                )
                if ok:
                    installed_pre.append(w["suggested_package"])
            if installed_pre:
                sha = commit_step(
                    ctx.workspace.sut, 9, "execute",
                    message_detail=(
                        f"pre-install missing test deps: "
                        f"{', '.join(installed_pre)}"
                    ),
                )
                log.info(
                    "step09.auto_install_commit",
                    phase="pre", packages=installed_pre, sha=sha,
                )

        # Resolver bridge: when the JIT runtime is vendored into the SUT,
        # start a parent-side TCP server that the pytest plugin can call to
        # resolve TBDs. This is the security-correct path for the LLM tier:
        # the Anthropic API key stays in the parent process and is never
        # exported into the SUT subprocess (where safe_subprocess_env strips
        # it anyway). The pytest plugin's _call_resolver dispatcher picks
        # the socket path automatically when QTEA_RESOLVER_PORT is set.
        jit_runtime_vendored = (
            ctx.workspace.sut / "tests" / "qtea_runtime.py"
        ).is_file()
        _resolver_server = None
        if jit_runtime_vendored:
            jit_cache_dir.mkdir(parents=True, exist_ok=True)
            _resolver_server = ResolverServer(
                cache_dir=jit_cache_dir,
                run_id=ctx.workspace.run_id,
                model=runtime_env.get("QTEA_RESOLVER_MODEL"),
                dev_locators_path=Path(runtime_env["QTEA_DEV_LOCATORS"]),
            )
            _resolver_server.start()
            runtime_env["QTEA_RESOLVER_PORT"] = str(_resolver_server.port)
            runtime_env["QTEA_RESOLVER_TOKEN"] = _resolver_server.token
            log.info(
                "step09.resolver_server_started",
                port=_resolver_server.port,
            )

            # Task 3: pre-warm the dev-pool tier of the JIT cache. We
            # already know every TBD intent codegen emitted (it's in
            # ``artifacts/step08/tbd-index.json``) and we have the dev-
            # locator pool loaded. Running the fuzzy match now (in the
            # parent, off the test's critical path) means pytest hits a
            # populated cache for every tier-1b-resolvable intent
            # instead of paying a snapshot+score round-trip per test.
            # Idempotent on retry — entries already present in the
            # cache are not overwritten.
            try:
                _prewarm_count = _prewarm_jit_cache_dev_pool(
                    ctx=ctx,
                    jit_cache_dir=jit_cache_dir,
                    dev_locators_path=Path(runtime_env["QTEA_DEV_LOCATORS"]),
                )
                if _prewarm_count > 0:
                    log.info(
                        "step09.jit_cache_prewarmed",
                        count=_prewarm_count,
                        tier="dev-pool",
                    )
            except Exception as _e:  # noqa: BLE001 — best-effort, never poison run
                log.warning("step09.jit_cache_prewarm_failed", error=str(_e))

        return _BootstrapResult(
            out_dir=out_dir,
            heal_log_path=heal_log_path,
            current_attempt=_current_attempt,
            prior_state=_prior_state,
            research=research,
            framework=framework,
            detected_cmd=detected_cmd,
            sut_tests=sut_tests,
            active_module=active_module,
            runtime_env=runtime_env,
            jit_cache_dir=jit_cache_dir,
            jit_runtime_vendored=jit_runtime_vendored,
            resolver_server=_resolver_server,
            heal_mcp_env=_heal_mcp_env,
            storage_state_mod=_storage_state_mod,
            storage_state_path=_storage_state_path,
            install_log_path=install_log_path,
            stack_profile=stack_profile,
            install_sig=install_sig,
            skip_install=_skip_install,
            no_auto_deps=no_auto_deps,
        )

    async def run(self, ctx: StepContext) -> StepResult:
        # Bootstrap phase (Tier 2): preflight guards + workspace/artifact
        # setup + install + resolver-server startup. Returns _BootstrapResult
        # on success, or a StepResult when the run must abort early.
        boot = self._bootstrap(ctx)
        if isinstance(boot, StepResult):
            return boot

        # Unpack the fields the middle phase reads directly. Fields that
        # get MUTATED downstream (stack_profile via venv swap already done
        # in bootstrap; install_sig refreshed by dep-recovery; runtime_env
        # mutated by post-run storage-state re-resolve) stay accessed via
        # ``boot.<field>`` so the mutations land back on the shared record.
        out_dir = boot.out_dir
        heal_log_path = boot.heal_log_path
        _current_attempt = boot.current_attempt
        _prior_state = boot.prior_state
        research = boot.research
        framework = boot.framework
        detected_cmd = boot.detected_cmd
        sut_tests = boot.sut_tests
        active_module = boot.active_module
        runtime_env = boot.runtime_env
        jit_cache_dir = boot.jit_cache_dir
        _heal_mcp_env = boot.heal_mcp_env
        _storage_state_mod = boot.storage_state_mod
        _storage_state_path = boot.storage_state_path
        install_log_path = boot.install_log_path
        stack_profile = boot.stack_profile
        install_sig = boot.install_sig
        no_auto_deps = boot.no_auto_deps
        _resolver_server = boot.resolver_server

        # Preserve the pre-narrowing command so dep-recovery (which fires on
        # a missing_module runner failure) re-runs the FULL test suite, not
        # the retry-narrowed subset. Without this, `_save_attempt_state`
        # would persist a `failing_ids` computed only over the narrowed
        # subset and forget previously-passing tests across attempts.
        _orig_cmd = detected_cmd
        # Task 5: narrow attempt 2's initial test run to the subset that
        # failed in attempt 1 minus tests flagged as real bugs (Task 2).
        # Saves the bulk of the previously-passing tests' wall time —
        # those passed once on identical code, no need to re-prove it.
        _retry_subset_count: int | None = None
        if _prior_state:
            _prior_failing = _prior_state.get("failing", []) or []
            _prior_no_patch = set(_prior_state.get("no_patch_ids", []) or [])
            _rerun_pairs = [
                (e["id"], e["name"]) for e in _prior_failing
                if isinstance(e, dict) and e.get("id") and e.get("name")
                and e["id"] not in _prior_no_patch
                and e["id"] != "T-runner-failure"
            ]
            if _rerun_pairs:
                from types import SimpleNamespace
                _rerun_entries = [
                    SimpleNamespace(id=tid, name=tname)
                    for tid, tname in _rerun_pairs
                ]
                # Resolve the framework default when codegen didn't pin one,
                # so the narrowing can append a -k / --grep to a real cmd.
                _base_cmd, _ = resolve_command(
                    framework,
                    detected=detected_cmd,
                    cwd=ctx.workspace.sut,
                    profile=stack_profile,
                    marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                )
                detected_cmd = _filter_command_for_tests(_base_cmd, _rerun_entries)
                _retry_subset_count = len(_rerun_pairs)
                log.info(
                    "step09.retry_narrowed_to_subset",
                    attempt=_current_attempt,
                    rerun_count=len(_rerun_pairs),
                    skipped_real_bugs=len(_prior_no_patch),
                    original_cmd=_orig_cmd or "(framework default)",
                    narrowed_cmd=detected_cmd,
                )

        try:
            log.info(
                "step09.test_run_start",
                framework=framework,
                cwd=str(ctx.workspace.sut),
                detected_cmd=detected_cmd or "(none — will use framework default)",
                marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                parallelism=getattr(ctx.options, "parallelism", 0),
                headless=getattr(ctx.options, "headless", True),
                retry_subset_count=_retry_subset_count,
            )
            _applied_marker_filter = _QTEA_PYTEST_MARKER_FILTER
            first = run_tests(
                framework,
                cwd=ctx.workspace.sut,
                detected_command=detected_cmd,
                timeout_s=min(self.timeout_s or 1800, 1800),
                env_extra=runtime_env,
                profile=stack_profile,
                headless=getattr(ctx.options, "headless", True),
                marker_filter=_applied_marker_filter,
                parallelism=getattr(ctx.options, "parallelism", 0),
            )

            # Detect "no tests collected" — a codegen quality failure where
            # Step 8's test scoping filter matched nothing.
            # pytest: exit code 5 = no tests collected.
            # Playwright Test: exit code 1 + total==0 = no matching files.
            _is_empty_collection = (
                first.exit_code == 5
                or (
                    framework in _PW_TEST_FRAMEWORKS
                    and first.exit_code == 1
                    and first.totals.get("tests", 0) == 0
                )
            )
            if _is_empty_collection and _applied_marker_filter:
                return StepResult(
                    success=False,
                    status="failed",
                    outputs=[],
                    error=(
                        f"{framework} collected 0 tests matching the qtea "
                        f"test filter. This is a codegen defect: Step 8 must "
                        f"generate test files with the 'qtea_' prefix "
                        f"(Playwright Test) or add @pytest.mark.qtea_<phase> "
                        f"to every test function (pytest). Check the test files "
                        f"in the SUT and ensure they follow the naming convention. "
                        f"Override with QTEA_PYTEST_MARKER='' to run without "
                        f"scoping (runs the full SUT suite, not recommended)."
                    ),
                )

            log.info(
                "step09.test_run_done",
                command=first.command,
                cwd=first.cwd,
                exit_code=first.exit_code,
                duration_s=round(first.duration_s, 1),
                totals=first.totals,
            )

            # Persist raw test-runner stdout/stderr as a standalone artifact
            # so humans can diagnose without parsing run-results.json.
            test_output_path = out_dir / "test-output.log"
            with contextlib.suppress(OSError):
                test_output_path.write_text(
                    f"# framework: {framework}\n"
                    f"$ {first.command}\n"
                    f"# exit_code: {first.exit_code}\n"
                    f"# duration: {first.duration_s:.1f}s\n\n"
                    f"--- STDOUT ---\n{first.stdout or '(empty)'}\n\n"
                    f"--- STDERR ---\n{first.stderr or '(empty)'}\n",
                    encoding="utf-8",
                )

            # `test_runner_invocations` counts calls to run_tests within a
            # single Step 9 attempt (initial → optional dep-recovery →
            # optional post-heal re-run). Distinct from `_current_attempt`
            # (the pipeline-level retry counter owned by base.py). Surfaced
            # to consumers under the historical key "attempts" for artifact
            # schema stability.
            test_runner_invocations = 1
            patches_applied = 0
            patches_rejected = 0

            failing = _failing_tests(first)
            self_heal_meta: dict[str, dict] = {}

            # Task 2: skip the heal call for tests the prior attempt's
            # heal agent classified as real product bugs (summary ==
            # "no usable patch produced"). Heal would only spend more
            # LLM cycles arriving at the same conclusion. The
            # classification is conservative: only the exact "no usable
            # patch" summary qualifies — agent timeouts, scope violations,
            # and XPath rejections all stay eligible because they indicate
            # the heal could still succeed under different conditions.
            if _prior_state:
                _prior_no_patch = set(_prior_state.get("no_patch_ids", []) or [])
                if _prior_no_patch:
                    _skipped_heals = [e for e in failing if e.id in _prior_no_patch]
                    failing = [e for e in failing if e.id not in _prior_no_patch]
                    for _entry in _skipped_heals:
                        _summary = (
                            "skipped: prior attempt's heal produced no patch "
                            "(classified as real product bug)"
                        )
                        self_heal_meta[_entry.id] = {
                            "attempted": False,
                            "applied": False,
                            "summary": _summary,
                        }
                        try:
                            with heal_log_path.open("a", encoding="utf-8") as _fh:
                                _fh.write(json.dumps({
                                    "test_id": _entry.id,
                                    "file": _entry.file,
                                    "applied": False,
                                    "agent_success": False,
                                    "agent_error": _summary,
                                    "ts": datetime.now(UTC).isoformat(),
                                }, ensure_ascii=False) + "\n")
                        except OSError:
                            pass
                        log.info(
                            "step09.heal_skipped_real_bug",
                            test_id=_entry.id,
                            attempt=_current_attempt,
                        )

            # Skip self-heal when the failure is the synthetic `T-runner-failure`
            # entry — the runner blew up at collection / import time and there is
            # no per-test traceback, no patch site, nothing the fixer agent can
            # produce a diff against. Past behaviour burned ~10 timed-out heal
            # attempts (≈75 min wall-clock + tokens) on exactly these cases
            # before falling through to the runner_only_failure error path below.
            # The classifier output (when present on the entry) flows into that
            # error path so the user sees the missing dep name and install hint.
            runner_only = (
                len(failing) > 0
                and all(r.id == "T-runner-failure" for r in failing)
            )
            # Also skip heal on exit code 3 (pytest internal error) when all
            # failing entries are infrastructure errors — no real test ran, so
            # there is no POM/locator to patch.
            if not runner_only and first.exit_code == 3:
                all_infra = (
                    len(failing) > 0
                    and all(r.runner_failure is not None for r in failing)
                )
                if all_infra:
                    runner_only = True

            # Runtime dep-recovery: on a missing_module runner failure, attempt
            # one install + re-run before declaring defeat. This catches gaps
            # Step 6's static audit missed (dynamic imports, conftest-only deps
            # the SUT layout heuristic skipped). Bounded to ONE retry per Step 9
            # invocation — if the re-run still produces runner_only, fall through
            # to the existing heal_skip behavior.
            if runner_only and not no_auto_deps:
                rf = (failing[0].runner_failure or {})
                module = rf.get("module") if rf.get("kind") == "missing_module" else None
                if module:
                    confidence = "known" if module in _PYTEST_PLUGIN_PROVIDERS else "guessed"
                    package = _PYTEST_PLUGIN_PROVIDERS.get(module, module)
                    pkg_mgr = stack_profile.package_manager if stack_profile else None
                    proceed = confidence == "known"
                    if confidence == "guessed":
                        # HITL is available when we can reach a real operator —
                        # a TTY (CLI mode) or the Flet UI. --no-hitl and --yes
                        # both bypass HITL; --yes historically defaults to
                        # "skip install" here (matches prior semantics), so
                        # both flags leave proceed=False.
                        hitl_available = (
                            (sys.stdin.isatty() or getattr(ctx.options, "ui_mode", False))
                            and not getattr(ctx.options, "no_hitl", False)
                            and not getattr(ctx.options, "yes", False)
                        )
                        if hitl_available:
                            proceed = _hitl_confirm_dep_install(
                                module=module,
                                package=package,
                                hint=rf.get("hint") or "",
                                default=True,
                            )
                        else:
                            log.warning(
                                "step09.dep_recover_skipped",
                                reason="non-interactive and confidence=guessed",
                                module=module, package=package,
                            )
                    # Mirror the venv_bin lookup _run_dep_install does internally so
                    # the can-we-install? gate doesn't false-reject pip when the
                    # SUT has a .venv (or false-accept when it doesn't).
                    _venv_bin = (
                        stack_profile.wrapper_prefix
                        if stack_profile and (pkg_mgr or "").lower() == "pip"
                        else None
                    )
                    if (
                        proceed
                        and install_command_for(pkg_mgr, package, venv_bin=_venv_bin) is not None
                    ):
                        ok, summary = _run_dep_install(
                            pkg_mgr, package, ctx.workspace.sut, install_log_path,
                            profile=stack_profile,
                        )
                        log.info(
                            "step09.auto_install_dep",
                            phase="runtime",
                            module=module, package=package,
                            confidence=confidence,
                            success=ok, summary=summary,
                        )
                        if ok:
                            sha = commit_step(
                                ctx.workspace.sut, 9, "execute",
                                message_detail=f"install missing test dep {package}",
                            )
                            log.info(
                                "step09.auto_install_commit",
                                phase="runtime", package=package, sha=sha,
                            )
                            # Use _orig_cmd (unnarrowed): the missing dep is
                            # an import-time failure — subsetting by test id
                            # is meaningless and would drop previously-passing
                            # tests from the persisted attempt state.
                            first = run_tests(
                                framework,
                                cwd=ctx.workspace.sut,
                                detected_command=_orig_cmd,
                                timeout_s=min(self.timeout_s or 1800, 1800),
                                env_extra=runtime_env,
                                profile=stack_profile,
                                headless=getattr(ctx.options, "headless", True),
                                marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                                parallelism=getattr(ctx.options, "parallelism", 0),
                            )
                            test_runner_invocations = 2
                            # Refresh install_sig: the auto-install just wrote
                            # to the lockfile, so the pre-recovery signature
                            # captured at step start is stale. Without this,
                            # the value persisted to attempt state (below)
                            # would misrepresent the post-recovery dep state
                            # and a subsequent attempt could false-skip
                            # `prepare_sut`.
                            install_sig = _compute_install_sig(
                                ctx.workspace.sut, stack_profile,
                            )
                            failing = _failing_tests(first)
                            runner_only = (
                                len(failing) > 0
                                and all(r.id == "T-runner-failure" for r in failing)
                            )
                            if (
                                not runner_only
                                and first.exit_code == 3
                                and all(r.runner_failure is not None for r in failing)
                            ):
                                runner_only = True
                            log.info(
                                "step09.dep_recover_retry",
                                runner_only_after=runner_only,
                                failing_after=len(failing),
                            )

            if runner_only:
                rf = (failing[0].runner_failure or {})
                log.warning(
                    "step09.heal_skip",
                    reason="runner failure — no per-test data to patch",
                    kind=rf.get("kind"),
                    module=rf.get("module"),
                    hint=rf.get("hint"),
                )
                # Record a heal-log line per skipped runner failure so the audit
                # trail explains the empty heal block instead of going silent.
                for entry in failing:
                    rf_entry = entry.runner_failure or {}
                    with heal_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "test_id": entry.id,
                            "file": entry.file,
                            "applied": False,
                            "agent_success": False,
                            "agent_error": (
                                f"skipped: {rf_entry.get('summary', 'runner failure')} "
                                f"— no test to patch"
                            ),
                            "ts": datetime.now(UTC).isoformat(),
                        }, ensure_ascii=False) + "\n")
                # Short-circuit the regular heal loop without changing the rest
                # of the flow (run-results.json / bug-candidates.json still get
                # written, status still falls through to the runner_only_failure
                # branch below).
                failing = []

            # QTEA_NO_LLM_RESOLVE=1 disables the JIT runtime LLM tier (in the
            # pytest subprocess) AND the on-failure self-heal agent here. The
            # flag is the single dial for "no LLM spend in this test region";
            # CI runs that need cost determinism set it once and get symmetric
            # behaviour across runtime resolution and post-failure heal. Tier 5
            # (HITL/fail-fast with locator-unresolvable bug candidate) still
            # applies — unresolved TBDs surface in run-results.json for Step 9.
            no_llm_resolve = os.environ.get("QTEA_NO_LLM_RESOLVE") == "1"
            if failing and no_llm_resolve:
                log.info(
                    "step09.heal_skipped",
                    reason="QTEA_NO_LLM_RESOLVE=1",
                    failing_count=len(failing),
                )
                failing = []

            # Re-resolve storage state AFTER run_tests() so Use case B (the
            # runtime plugin's same-run auto-capture) is visible to the heal
            # loop on the FIRST run, not only on `--from-step 9` resumes.
            # The early resolve at step start happens BEFORE tests execute,
            # so the workspace file does not exist yet on a cold run. Re-
            # resolving here, after tests have finished writing it, closes
            # that gap. _heal_mcp_env is mutated in place so the lazy MCP
            # probe + every run_agent call below pick up the fresh path
            # without further plumbing.
            _storage_state_path = _storage_state_mod.resolve(
                sut_root=ctx.workspace.sut,
                workspace_root=ctx.workspace.root,
                cli_opt=getattr(ctx.options, "storage_state", None),
            )
            _heal_mcp_env["QTEA_STORAGE_STATE_ARG"] = (
                _storage_state_mod.to_mcp_arg(_storage_state_path)
            )
            if _storage_state_path is not None:
                runtime_env["QTEA_STORAGE_STATE"] = str(_storage_state_path)
                log.info(
                    "step09.storage_state_resolved_post_run",
                    path=_storage_state_mod.mask_path(_storage_state_path),
                )

            # Partition failures by class. Only "healable" rows (locator
            # timeouts, TBD unresolvable, assertion-on-locator-mediated
            # values, unknown) enter the heal queue. Real bugs (WCAG, TTI,
            # fixture-missing, import-error, dom-order) skip heal — they
            # cannot be fixed by selector or interaction-pattern tweaks
            # and would just burn LLM + MCP budget. They still surface to
            # Step 10 via _build_bug_candidates (which derives from
            # final_failing post-heal, so this partition does not hide
            # them from the bug-candidates emission).
            #
            # Without this partition, run 20260621-213751-ee0fef hit the
            # canonical recurring failure: 11/13 tests failed → heal-skip
            # cap blocked the entire heal flow → no recovery → user saw
            # 11 mixed failures with the cache holding wrong selectors.
            if failing:
                healable, real_bugs = _partition_failures(failing)
                if real_bugs:
                    _ts = datetime.now(UTC).isoformat()
                    for entry, cls in real_bugs:
                        with heal_log_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({
                                "test_id": entry.id,
                                "file": entry.file,
                                "applied": False,
                                "agent_success": False,
                                "agent_error": (
                                    f"skipped: classified as {cls!r} "
                                    f"(real bug — not a heal target)"
                                ),
                                "ts": _ts,
                            }, ensure_ascii=False) + "\n")
                    log.info(
                        "step09.heal_skip_real_bugs",
                        count=len(real_bugs),
                        classes=sorted({c for _, c in real_bugs}),
                        healable_remaining=len(healable),
                    )
                failing = healable

            if failing and len(failing) <= _MAX_HEAL_TESTS:
                # Lazy Playwright MCP probe — replaces the eager preflight
                # so green runs skip the 5-15s warmup. We probe ONCE before
                # the first heal-agent invocation; the warmup is contiguous
                # with the SDK spawn (same cache-warm semantics the eager
                # preflight provided, just deferred until actually needed).
                # On probe failure: log + skip the heal loop entirely. Heal
                # is best-effort — the failing tests still flow to Step 10
                # as bug candidates without an MCP-driven patch.
                mcp_ok, mcp_detail, mcp_warmup_s = _lazy_probe_heal_mcp(
                    self._LAZY_MCP_SERVER,
                    env=_heal_mcp_env,
                )
                if not mcp_ok:
                    log.warning(
                        "step09.heal_mcp_probe_failed",
                        server=self._LAZY_MCP_SERVER,
                        detail=mcp_detail,
                        warmup_s=mcp_warmup_s,
                        failing_count=len(failing),
                    )
                    # Record per-test skip in heal-log so the audit trail
                    # explains the absent heal without going silent.
                    for entry in failing:
                        with heal_log_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({
                                "test_id": entry.id,
                                "file": entry.file,
                                "applied": False,
                                "agent_success": False,
                                "agent_error": (
                                    f"skipped: Playwright MCP probe failed "
                                    f"({mcp_detail or 'unknown'})"
                                ),
                                "ts": datetime.now(UTC).isoformat(),
                            }, ensure_ascii=False) + "\n")
                    # Short-circuit the heal loop; downstream emission of
                    # run-results.json / bug-candidates.json continues so
                    # Step 10 still sees the failures.
                    failing = []
                else:
                    log.info(
                        "step09.heal_mcp_probe_ok",
                        server=self._LAZY_MCP_SERVER,
                        warmup_s=mcp_warmup_s,
                    )
            if failing and len(failing) <= _MAX_HEAL_TESTS:
                fixer_agent = package_resource_root() / "agents" / "polyglot-test-fixer.agent.md"
                sut_base_url = os.environ.get("SUT_BASE_URL")
                heal_relevant_sut_files = _auth_relevant_sut_files(active_module)
                heal_allowlist = _heal_allowlist_dirs(active_module)
                generated_files = _load_generated_files(ctx)

                # Task 1: parallelize heal agents via asyncio.gather. The
                # LLM call (run_agent) dominates each heal's wall time
                # (~1–10 min on Opus); running them concurrently nearly
                # halves the total when 2+ tests fail. Bounded by
                # ``QTEA_HEAL_CONCURRENCY`` (default 3) to cap memory
                # — each agent spawns a Playwright MCP browser process.
                #
                # Concurrent SUT edits: agents may write to the same POM
                # file in ``acceptEdits`` mode; the agent whose write
                # completes second wins. We accept this race for v1; the
                # post-heal verify re-run catches incorrect patches and
                # the existing scope guards / quality gates protect
                # against most catastrophic outcomes. The git commit
                # itself is sync (blocks the event loop briefly) and so
                # is naturally serialized — concurrent commits can't
                # race because Python won't preempt mid-subprocess.
                #
                # ``patches_applied`` / ``patches_rejected`` increments
                # and ``self_heal_meta`` writes are asyncio-safe: they
                # happen between awaits within each coroutine, and
                # asyncio guarantees no preemption inside an await-free
                # span. Same for the per-line heal-log appends.
                _heal_concurrency = max(
                    1, int(os.environ.get("QTEA_HEAL_CONCURRENCY", "3")),
                )
                _heal_sem = asyncio.Semaphore(_heal_concurrency)
                log.info(
                    "step09.heal_parallel_start",
                    failing_count=len(failing),
                    concurrency=_heal_concurrency,
                    tests=[e.name for e in failing],
                )

                async def _do_one_heal(entry):
                    nonlocal patches_applied, patches_rejected
                    heal_wd = ctx.workspace.step_workdir(9) / f"heal-{entry.id}"
                    heal_wd.mkdir(parents=True, exist_ok=True)
                    # Snapshot the failing test's current bytes BEFORE the fixer
                    # runs so we can detect a real change after it returns. The
                    # snapshot lives under heal_wd (NOT in the SUT) so it never
                    # ends up in a qtea commit.
                    target_in_sut = sut_tests / Path(entry.file).name
                    pre_bytes: bytes | None = None
                    if target_in_sut.exists():
                        try:
                            pre_bytes = target_in_sut.read_bytes()
                        except OSError:
                            pre_bytes = None

                    # Capture HEAD before the heal so the scope guard below
                    # can revert any out-of-scope edits the agent made (or
                    # left dangling on timeout) back to the Step 8 commit
                    # state. Best-effort — git not available means we skip
                    # the scope check rather than failing the heal.
                    import subprocess as _sp
                    base_sha: str | None = None
                    try:
                        _res = _sp.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=ctx.workspace.sut,
                            capture_output=True, text=True, check=False,
                            timeout=5,
                        )
                        if _res.returncode == 0:
                            base_sha = (_res.stdout or "").strip() or None
                    except (OSError, _sp.TimeoutExpired):
                        base_sha = None

                    # Snapshot git-dirty state BEFORE the heal agent runs.
                    # Files already dirty at this point (e.g. qtea-junit.xml
                    # from pytest) are excluded from the scope check to avoid
                    # false scope violations.
                    pre_heal_dirty: set[str] = {
                        p for _, p in _git_status_porcelain(ctx.workspace.sut)
                    }

                    # Step 9's heal flow is the only `run_agent` call site in
                    # the pipeline that actually uses Playwright MCP tools
                    # (`browser_navigate`, `browser_snapshot`). After the
                    # audit that flipped `run_agent`'s `enable_mcp` default
                    # to False, this call must opt back in.
                    #
                    # ``_heal_sem`` bounds the in-flight LLM calls so the
                    # parallel orchestration doesn't spin up more browser
                    # processes than the host can handle.

                    # Per-heal MCP env: each concurrent agent gets its own
                    # Chromium user-data-dir so profile locks don't cause
                    # "Browser is already in use" contention.
                    _per_heal_mcp_env = {
                        **_heal_mcp_env,
                        "QTEA_MCP_USER_DATA_DIR_ARG": (
                            f"--user-data-dir={heal_wd / 'playwright-mcp'}"
                        ),
                    }

                    _entry_class = _classify_failure(entry)

                    log.info(
                        "step09.heal_start",
                        test_id=entry.id,
                        test_name=entry.name,
                        test_file=entry.file,
                        failure_class=_entry_class,
                    )
                    async with _heal_sem:
                        agent_res = await run_agent(
                            fixer_agent,
                            workdir=heal_wd,
                            inputs={},
                            user_prompt=_build_fixer_prompt(
                                entry, sut_tests,
                                sut_root=ctx.workspace.sut,
                                sut_base_url=sut_base_url,
                                active_module=active_module,
                                staged_files=heal_relevant_sut_files,
                                storage_state_path=_storage_state_path,
                                generated_files=generated_files,
                                failure_class=_entry_class,
                            ),
                            extra_paths=[
                                package_resource_root() / "skills" / "diagnose-test-failure",
                                package_resource_root() / "skills" / "playwright-explore-website",
                                package_resource_root() / "skills" / "webapp-testing",
                            ],
                            add_dirs=[ctx.workspace.sut],
                            timeout_s=HEAL_AGENT_TIMEOUT_S,
                            step=9,
                            max_turns=HEAL_AGENT_MAX_TURNS,
                            enable_mcp=True,
                            mcp_env=_per_heal_mcp_env,
                        )

                    # Scope guard: revert any heal edits to files outside the
                    # POM/locator allowlist (or matching the FORBIDDEN globs).
                    # Runs unconditionally — even on agent_res.success=False
                    # (timeout, error) — because the agent may have written
                    # files to disk before the timeout fired and left them
                    # uncommitted on the qtea branch. The run 20260611
                    # incident left 5 in-flight fixture edits on disk after
                    # the 150s timeout — this revert prevents that recurrence.
                    scope_reverted = _heal_scope_check_and_revert(
                        ctx.workspace.sut, base_sha, heal_allowlist,
                        generated_files=generated_files,
                        pre_heal_dirty=pre_heal_dirty,
                    )
                    scope_violation = bool(scope_reverted)
                    if scope_violation:
                        log.warning(
                            "step09.heal_scope_violation",
                            test_id=entry.id,
                            reverted=scope_reverted,
                            allowlist=sorted(heal_allowlist),
                        )

                    # Additional cleanup: when the heal agent FAILED outright
                    # (timeout, transport error) any in-scope edits it left
                    # uncommitted on disk must also be reverted. An in-flight
                    # patch that the agent never finished reviewing is no
                    # safer than an out-of-scope one — keeping it commits the
                    # orchestrator to half-thought-through code and confuses
                    # the next heal attempt.
                    failed_partial_revert: list[str] = []
                    if not agent_res.success:
                        failed_partial_revert = _heal_revert_all_uncommitted(
                            ctx.workspace.sut, base_sha,
                        )
                        if failed_partial_revert:
                            log.warning(
                                "step09.heal_failed_partial_edits_reverted",
                                test_id=entry.id,
                                reverted=failed_partial_revert,
                                agent_error=agent_res.error,
                            )

                    # Detect whether the heal agent actually changed something.
                    # Three detection tiers:
                    #   1. The failing TEST file's bytes changed (inline edit).
                    #   2. ANY file in the SUT working tree changed (the agent
                    #      edited a POM/locator file instead of the test file —
                    #      this is the most common heal pattern).
                    #   3. Legacy fallback: the agent dropped a candidate file
                    #      in its workdir (pre-add_dirs path).
                    applied = False
                    if agent_res.success and not scope_violation:
                        post_bytes: bytes | None = None
                        if target_in_sut.exists():
                            try:
                                post_bytes = target_in_sut.read_bytes()
                            except OSError:
                                post_bytes = None
                        applied = post_bytes is not None and post_bytes != pre_bytes
                        if not applied:
                            post_heal_dirty = {
                                p for _, p in _git_status_porcelain(ctx.workspace.sut)
                            }
                            heal_changed = post_heal_dirty - pre_heal_dirty
                            if heal_changed:
                                applied = True
                                log.info(
                                    "step09.heal_detected_via_git",
                                    test_id=entry.id,
                                    changed_files=sorted(heal_changed),
                                )
                        if not applied:
                            applied = _apply_fixer_outputs(
                                heal_wd,
                                sut_tests,
                                entry.file,
                            )

                    xpath_rejected = False
                    if applied:
                        # Step 9 quality gate: a heal that introduces an XPath
                        # selector is rejected. Revert the SUT file to its
                        # pre-heal state (restore bytes if it existed, delete
                        # if the heal created it from scratch) and mark the
                        # patch unapplied. See `docs/qa-orchestrator.instructions.md`
                        # §6 "No XPath (self-heal)".
                        post_bytes_check = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        if _patch_introduces_xpath(pre_bytes, post_bytes_check):
                            xpath_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.xpath_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_xpath",
                                test_id=entry.id,
                                file=entry.file,
                            )
                            applied = False

                    assertion_rejected = False
                    if applied and generated_files:
                        post_bytes_assert = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        if _patch_modifies_assertions(pre_bytes, post_bytes_assert):
                            assertion_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.assertion_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_assertion_modified",
                                test_id=entry.id,
                                file=entry.file,
                            )
                            applied = False

                    anti_pattern_rejected = False
                    anti_pattern_violations: list[str] = []
                    if applied:
                        post_bytes_ap = (
                            target_in_sut.read_bytes() if target_in_sut.exists() else None
                        )
                        anti_pattern_violations = _patch_has_anti_patterns(
                            pre_bytes, post_bytes_ap,
                        )
                        if anti_pattern_violations:
                            anti_pattern_rejected = True
                            try:
                                if pre_bytes is not None:
                                    target_in_sut.write_bytes(pre_bytes)
                                elif target_in_sut.exists():
                                    target_in_sut.unlink()
                            except OSError as e:
                                log.warning(
                                    "step09.anti_pattern_revert_failed",
                                    test_id=entry.id,
                                    file=entry.file,
                                    error=str(e),
                                )
                            log.warning(
                                "step09.heal_rejected_anti_pattern",
                                test_id=entry.id,
                                file=entry.file,
                                violations=anti_pattern_violations,
                            )
                            applied = False

                    if applied:
                        patches_applied += 1
                        # Per-test commit so the human reviewer sees exactly which
                        # heal landed which patch. No-op when the bytes equal the
                        # branch tip (e.g. agent reverted its own edit).
                        commit_step(
                            ctx.workspace.sut,
                            self.number,
                            f"{self.name}-heal-{entry.id}",
                            message_detail=f"healed {Path(entry.file).name}",
                        )
                    else:
                        patches_rejected += 1

                    if scope_violation:
                        summary_text = (
                            f"rejected: heal touched out-of-scope file(s) "
                            f"{','.join(scope_reverted[:3])}; "
                            f"reverted to pre-heal state. Heal scope is "
                            f"POM/locator only — see "
                            f"agents/polyglot-test-fixer.agent.md FORBIDDEN."
                        )
                    elif not agent_res.success:
                        summary_text = agent_res.error
                    elif applied:
                        summary_text = "patch applied"
                    elif xpath_rejected:
                        summary_text = (
                            "rejected: heal introduced XPath selector "
                            "(Step 9 quality gate)"
                        )
                    elif assertion_rejected:
                        summary_text = (
                            "rejected: heal modified assertions in generated "
                            "test (Step 9 assertion-immutability gate)"
                        )
                    elif anti_pattern_rejected:
                        summary_text = (
                            f"rejected: heal introduced anti-pattern — "
                            f"{'; '.join(anti_pattern_violations)}"
                        )
                    else:
                        summary_text = "no usable patch produced"
                    self_heal_meta[entry.id] = {
                        "attempted": True,
                        "applied": applied,
                        "summary": summary_text,
                    }

                    # Append to heal-log.jsonl
                    heal_entry = {
                        "test_id": entry.id,
                        "file": entry.file,
                        "failure_class": _entry_class,
                        "applied": applied,
                        "agent_success": agent_res.success,
                        "agent_error": agent_res.error,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                    if xpath_rejected:
                        heal_entry["rejected"] = "xpath"
                    if assertion_rejected:
                        heal_entry["rejected"] = "assertion_modified"
                    if anti_pattern_rejected:
                        heal_entry["rejected"] = "anti_pattern"
                    if scope_violation:
                        heal_entry["rejected"] = "scope_violation"
                        heal_entry["reverted_files"] = scope_reverted
                    with heal_log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(heal_entry, ensure_ascii=False) + "\n")

                # Launch every heal concurrently. ``return_exceptions=False``
                # propagates the first failure so we don't silently mask
                # bugs in the heal harness itself — individual agent errors
                # are already captured as ``agent_res.success=False`` and
                # do not raise; anything that DOES raise here is a true
                # orchestrator bug worth surfacing.
                _heal_t0 = time.monotonic()
                await asyncio.gather(*[_do_one_heal(e) for e in failing])
                log.info(
                    "step09.heal_parallel_done",
                    failing_count=len(failing),
                    duration_s=round(time.monotonic() - _heal_t0, 1),
                    patches_applied=patches_applied,
                    patches_rejected=patches_rejected,
                )

                if patches_applied > 0:
                    # Re-run ONLY the healed tests via `-k`, not the whole
                    # suite. We resolve the command the same way `run_tests`
                    # would, narrow it to the tests we just patched, then feed
                    # it back as `detected_command`. Non-pytest stacks fall
                    # back to the full command (see `_filter_command_for_tests`).
                    base_cmd, _parser = resolve_command(
                        framework,
                        detected=detected_cmd,
                        cwd=ctx.workspace.sut,
                        profile=stack_profile,
                        marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                    )
                    narrowed_cmd = _filter_command_for_tests(base_cmd, failing)
                    _clean_sut_artifacts(ctx.workspace.sut)
                    second = run_tests(
                        framework,
                        cwd=ctx.workspace.sut,
                        detected_command=narrowed_cmd,
                        timeout_s=min((self.timeout_s or 1800) // 2, 900),
                        env_extra=runtime_env,
                        profile=stack_profile,
                        headless=getattr(ctx.options, "headless", True),
                        marker_filter=_QTEA_PYTEST_MARKER_FILTER,
                        parallelism=getattr(ctx.options, "parallelism", 0),
                    )
                    test_runner_invocations = 2
                    # The narrowed re-run only reports the healed subset, so
                    # MERGE its outcomes into the full first-run result set
                    # (override the healed entries by id, keep everyone else).
                    # Replacing wholesale would drop every non-healed test from
                    # totals / bug-candidates / the runner-failure check.
                    if second.results:
                        by_id = {r.id: r for r in second.results}
                        first.results = [
                            by_id.get(r.id, r) for r in first.results
                        ]
                        first.exit_code = second.exit_code
            elif failing:
                log.warning(
                    "step09.heal_skip",
                    reason="too many failing tests",
                    count=len(failing),
                    cap=_MAX_HEAL_TESTS,
                )

            # Post-run pipeline (payload assembly + artifacts + TBD promotion
            # + HITL sweeps + bug candidates + status determination) lives in
            # ``_finalize_and_report`` for legibility. Pure code motion —
            # zero behaviour change.
            return self._finalize_and_report(
                ctx,
                out_dir,
                _PostRunState(
                    first=first,
                    self_heal_meta=self_heal_meta,
                    test_runner_invocations=test_runner_invocations,
                    patches_applied=patches_applied,
                    patches_rejected=patches_rejected,
                    framework=framework,
                    jit_cache_dir=jit_cache_dir,
                    install_sig=install_sig,
                    prior_state=_prior_state,
                    current_attempt=_current_attempt,
                    runtime_env=runtime_env,
                ),
            )

        finally:
            if _resolver_server is not None:
                _resolver_server.stop()


__all__ = [
    "ExecuteStep",
    "_apply_fixer_outputs",
    "_build_bug_candidates",
    "_build_fixer_prompt",
    "_filter_command_for_tests",
]
