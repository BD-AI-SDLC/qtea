"""Flet application bootstrap: page setup, routing, and pipeline lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import flet as ft

from qtea.ui import theme
from qtea.ui.state import AppState
from qtea.ui.theme import BACKGROUND, build_dark_theme, sz

_state = AppState()

# Diagnostic for the crash -> SESSION_CRASHED -> client-reconnect loop: Flet
# calls main() again for every reconnect, so a burst of calls in a short
# window IS the failure signature. Module-level (survives across sessions,
# same process) so the burst is visible even though each main() call gets a
# fresh local scope. Previously this could only be confirmed after the fact
# via a manual `grep -c "App session started"` on the run log.
_session_starts: list[float] = []
_SESSION_STORM_WINDOW_S = 6.0
_SESSION_STORM_THRESHOLD = 3


def main(page: ft.Page):
    now = time.monotonic()
    _session_starts.append(now)
    del _session_starts[:-50]  # bounded history; only recent bursts matter
    recent = [t for t in _session_starts if now - t <= _SESSION_STORM_WINDOW_S]
    crash_looping = len(recent) >= _SESSION_STORM_THRESHOLD
    if crash_looping:
        with contextlib.suppress(Exception):
            from qtea.logging_setup import get_logger as _get_logger

            _get_logger(__name__).error(
                "ui.session_crash_loop_detected",
                sessions_in_window=len(recent),
                window_s=_SESSION_STORM_WINDOW_S,
                detail="main() re-entered this many times in this window: "
                "the client is crash-looping (an exception escaped an event "
                "handler, Flet sent SESSION_CRASHED, the client reconnected, "
                "and main() ran again). Search this log around this "
                "timestamp for 'ui.handler_exception' or "
                "'Unhandled error in main() handler' to find the cause. "
                "Rendering the minimal crash-safe view to break the loop.",
            )

    if _state.run_status == "running":
        initial_route = "/run"
    elif _state.exit_code is not None:
        initial_route = "/results"
    else:
        initial_route = "/"

    # Crash-loop breaker: bail out HERE, before touching page.title, window
    # geometry, theme, or registering services/route handlers. A prior
    # version of this guard rendered the minimal view only after that setup
    # had already run unconditionally on every main() call (reconnect or
    # not) — but a real crash-storm run log showed zero Python-side
    # exceptions anywhere (no ui.handler_exception, no ui.view_build_failed,
    # no "Unhandled error in main() handler" from Flet's own app.py). Every
    # path in Flet that sends SESSION_CRASHED is preceded by one of those
    # log calls, except a hooks/effects path this app doesn't use — so a
    # storm with none of them logged means the fault is a Dart/Flutter
    # render crash triggered by something applied unconditionally before the
    # view is even built (most likely the native window resize from
    # `page.window.width/height`), not by anything in the view tree. This
    # branch touches nothing but page.views + page.update() to eliminate
    # every one of those suspects on the recovery path.
    if crash_looping:
        with contextlib.suppress(Exception):
            page.views.clear()
            _rid = getattr(_state, "run_id", None) or "N/A"
            _ws = getattr(_state, "workspace_path", None)

            def _minimal_new_run(_: ft.ControlEvent) -> None:
                _state.reset_run()
                with contextlib.suppress(Exception):
                    page.views.clear()
                    page.update()

            page.views.append(
                ft.View(
                    route=initial_route,
                    controls=[
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    ft.Container(height=24),
                                    ft.Text(
                                        "Display error — simplified view",
                                        size=sz(22),
                                        weight=ft.FontWeight.BOLD,
                                        color="#FFB74D",
                                    ),
                                    ft.Text(
                                        "The full screen kept crashing the "
                                        "renderer, so it was replaced with "
                                        "this safe view to stop a reconnect "
                                        "loop. Your run's artifacts are intact "
                                        "on disk.",
                                        size=sz(13),
                                        color="#E0E0E0",
                                    ),
                                    ft.Container(height=8),
                                    ft.Text(f"Run: {_rid}", size=sz(12)),
                                    ft.Text(
                                        f"Workspace: {_ws}" if _ws else "",
                                        size=sz(12),
                                    ),
                                    ft.Container(height=16),
                                    ft.ElevatedButton(
                                        "Start New Run",
                                        icon=ft.Icons.REPLAY,
                                        on_click=_minimal_new_run,
                                    ),
                                ],
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=4,
                            ),
                            expand=True,
                            alignment=ft.Alignment.center,
                            bgcolor=BACKGROUND,
                            padding=40,
                        )
                    ],
                    padding=0,
                    bgcolor=BACKGROUND,
                )
            )
            page.update()
        return

    page.title = "QTea"
    page.window.width = 1480
    page.window.height = 920
    page.window.min_width = 1100
    page.window.min_height = 700
    page.theme_mode = ft.ThemeMode.DARK
    page.dark_theme = build_dark_theme()
    page.bgcolor = BACKGROUND
    page.padding = 0

    # `_state` is a module-level singleton that survives every reconnect
    # (see the SESSION_CRASHED note above) — `main()` re-runs on each one,
    # so an unconditional init_steps() here would wipe the live/finished
    # run's per-step statuses (status/duration/tokens/cost) back to
    # fresh "pending" on any mid-run or post-run reconnect, while leaving
    # the aggregate total_cost/elapsed_s/etc. untouched (only reset_run()
    # zeroes those) — producing a results screen with correct totals but
    # every step row showing "pending"/0. Only initialize on a genuinely
    # cold `_state` (never-run-yet); an actual new run goes through
    # on_start_pipeline()'s `_state.reset_run()` instead.
    if not _state.steps:
        _state.init_steps()
    _state.load_prefs()
    theme.set_scale(_state.text_scale)

    # Lazy imports to avoid circular refs
    from qtea.ui.views.config_view import build_config_view
    from qtea.ui.views.context_capture_view import build_context_capture_view
    from qtea.ui.views.pipeline_view import build_pipeline_view
    from qtea.ui.views.results_view import build_results_view

    # ── File pickers (registered ONCE as page services) ──────────────────
    spec_picker = ft.FilePicker()
    sut_picker = ft.FilePicker()
    context_image_picker = ft.FilePicker()
    page.services.append(spec_picker)
    page.services.append(sut_picker)
    page.services.append(context_image_picker)

    # ── Timer task for live elapsed updates ──────────────────────────────

    async def _tick_elapsed() -> None:
        """Advance the elapsed-time clock once per second.

        We update only the live elapsed widgets (the header's persistent
        widget, plus a fresh tree-walk for the in-progress step's widget)
        rather than calling ``state.notify()``, which would rebuild every
        step card, the metrics panel, and the entire log list once per
        second. The clock is pause-aware via ``state.update_elapsed()`` —
        HITL waits don't accumulate.

        The step widget is re-located every tick (not cached) because a
        step that makes one long blocking call (e.g. a single LLM
        reasoning turn) emits no log lines between ``step.start`` and
        ``step.end`` — nothing would trigger a rebuild to refresh a cached
        reference, leaving the step's clock frozen for its entire duration.
        The tree-walk itself is cheap: it searches already-built controls,
        it doesn't rebuild them.
        """
        from qtea.ui.components.progress_header import fmt_elapsed
        from qtea.ui.views.pipeline_view import find_live_step_widget

        while _state.run_status == "running":
            _state.update_elapsed()
            widget = None
            step_widget = None
            if isinstance(page.data, dict):
                widget = page.data.get("live_elapsed")
                phase_groups = page.data.get("phase_groups_ref")
                if phase_groups is not None:
                    step_widget = find_live_step_widget(phase_groups.controls)
            if widget is not None:
                widget.value = fmt_elapsed(_state.elapsed_s)
                try:
                    widget.update()
                except Exception:
                    with contextlib.suppress(Exception):
                        page.update()
            if step_widget is not None and _state.current_step:
                s = _state.steps.get(_state.current_step)
                if s:
                    step_widget.value = fmt_elapsed(s.elapsed_s)
                    with contextlib.suppress(Exception):
                        step_widget.update()
            await asyncio.sleep(1)

    # ── Pipeline runner ──────────────────────────────────────────────────

    async def _run_pipeline() -> None:
        """Run the pipeline in a worker thread so blocking HITL calls don't
        freeze the Flet event loop."""
        import os

        from qtea.config import get_settings
        from qtea.pipeline import PipelineOptions, run_pipeline
        from qtea.ui.event_bridge import UIEventBridge
        from qtea.ui.hitl_bridge import HitlBridge
        from qtea.ui.review_gate_bridge import ReviewGateBridge

        os.environ["QTEA_UI_MODE"] = "1"
        settings = get_settings()
        cache_val: bool | None = None
        if _state.cache == "on":
            cache_val = True
        elif _state.cache == "off":
            cache_val = False

        opts = PipelineOptions(
            workspace_base=settings.default_workspace,
            spec=_state.spec or None,
            sut=_state.sut or None,
            headless=_state.headless,
            parallelism=_state.parallel_run,
            report=_state.report,
            log_level=_state.log_level,
            skip_steps=set(_state.skip_steps),
            cache=cache_val,
            storage_state=Path(_state.storage_state) if _state.storage_state else None,
            dev_locators=Path(_state.dev_locators) if _state.dev_locators else None,
            # Resume support — empty/None = fresh run, otherwise mirror
            # the CLI's --run-id + --from-step contract.
            run_id=_state.resume_run_id or None,
            from_step=_state.from_step,
            ui_mode=True,
            operator_context=_state.operator_context or None,
            operator_context_images=list(_state.operator_context_images) or None,
        )

        # Capture the Flet event loop so bridges running work on the worker
        # thread can schedule UI updates back onto it instead of touching
        # Flet controls directly from a non-UI thread.
        ui_loop = asyncio.get_running_loop()

        event_bridge = UIEventBridge(_state, page, ui_loop)
        event_bridge.install()

        hitl_bridge = HitlBridge(_state, page)
        hitl_bridge.install(ui_loop)

        # Same loop is shared with the review-gate bridge — it routes the
        # Step 4/7/8 "Approve and continue?" prompts to the UI dialog
        # instead of stdout/stdin, which would otherwise hang the run.
        review_gate_bridge = ReviewGateBridge(_state, page)
        review_gate_bridge.install(ui_loop)

        def _worker() -> int:
            # Each worker thread needs its own asyncio loop.
            # Pass a silent Rich Console so direct console.print() calls in
            # the pipeline don't bleed onto the terminal (the UI log panel
            # captures everything via structlog events).
            import io

            from rich.console import Console
            silent_console = Console(file=io.StringIO(), force_terminal=False)

            # Create the loop manually (instead of asyncio.run) so we can
            # publish the loop + task to AppState. The Stop button uses
            # those handles to cancel the pipeline from the UI thread.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(run_pipeline(opts, console=silent_console))
            _state.pipeline_loop = loop
            _state.pipeline_task = task
            # Stop may have fired before this worker thread published the
            # loop/task — the Stop handler's `if loop and task` guard would
            # then no-op, leaving the pipeline running (and billing) after
            # the UI already moved to /results. Honor a Stop that raced ahead.
            if _state.cancel_requested:
                task.cancel()
            try:
                return loop.run_until_complete(task)
            except asyncio.CancelledError:
                return 130
            finally:
                with contextlib.suppress(Exception):
                    loop.close()
                _state.pipeline_loop = None
                _state.pipeline_task = None

        try:
            # asyncio.to_thread runs _worker on the default executor so the
            # Flet event loop stays free to render and process clicks.
            rc = await asyncio.to_thread(_worker)
            _state.exit_code = rc
            _state.run_status = "completed" if rc == 0 else "failed"
        except Exception as exc:
            _state.exit_code = 2
            _state.run_status = "failed"
            from qtea.ui.state import LogLine

            _state.log_lines.append(
                LogLine(
                    timestamp="",
                    level="error",
                    event="pipeline.exception",
                    message=str(exc),
                )
            )
        finally:
            # Each uninstall() is independent — one bridge failing to clean
            # up must never prevent the other two, or the results view
            # rebuild below, from running.
            with contextlib.suppress(Exception):
                review_gate_bridge.uninstall()
            with contextlib.suppress(Exception):
                hitl_bridge.uninstall()
            with contextlib.suppress(Exception):
                event_bridge.uninstall()
            # A review-gate / HITL dialog may still be closing on the client
            # when the run ends: the dialog's Approve handler fires
            # completion_event.set() (which unblocks the pipeline worker) a
            # moment BEFORE page.pop_dialog() has finished removing the dialog
            # on the Flutter side, and the unblocked worker races straight
            # here (~100 ms). Clearing + rebuilding the whole view stack for
            # /results while that dialog removal is in flight orphans an
            # ancestor on the client -> Dart "Null check operator used on a
            # null value" -> SESSION_CRASHED -> reconnect loop (the exact
            # hours-long flicker seen after approving Step 4's gate). Defuse
            # it: drop any pending dialog + its state, then yield once so the
            # client applies that removal before we tear the views down.
            with contextlib.suppress(Exception):
                _state.pending_review_gate = None
                _state.pending_hitl = None
                page.pop_dialog()
            with contextlib.suppress(Exception):
                await asyncio.sleep(0.05)
            # The page/session may already be gone by the time we get here —
            # e.g. the window was closed while the pipeline was still running,
            # or the client briefly disconnected. Touching `page` in that case
            # raises RuntimeError("An attempt to fetch destroyed session.")
            # from deep inside Flet. Left unguarded, that exception escapes
            # this task, Flet reports SESSION_CRASHED to the client, the
            # client immediately reconnects, `main()` runs again for the new
            # session, and — since nothing here has changed — the same crash
            # repeats. Observed in the wild as the results screen "blinking"
            # in an infinite reconnect loop instead of showing the summary.
            try:
                # Cut the old /run view's on_state_change subscription before
                # we navigate. The fix-proposal chain (debug -> critical-thinking
                # -> PSE) emits extra `aux_agent.recorded` notify()s between
                # step.end and here; any trailing notify — including the
                # explicit one below — would otherwise drive the stale
                # pipeline_view listener to mutate now-detached controls, whose
                # reconcile blanks the whole page (the flicker bug in a new
                # form). The Stop path already clears listeners for the same
                # reason (progress_header.py); the normal finish path must too.
                _state._listeners.clear()
                # Skip the rebuild if the Stop button already snap-navigated
                # to /results. Two full page.views.clear()+rebuild sequences
                # in rapid succession orphan the Flet client's widget-id
                # map, leaving the summary rendered but every button
                # dead-on-click (verified in the wild: the flet log shows
                # 10s `Column(NNN).scroll_to` timeouts and none of the
                # results-view on_click handlers fire). The snap-navigate
                # already captured post-cancellation state; a stale line or
                # two in the log tail is a much smaller regression than a
                # frozen summary screen.
                if _state.results_navigated:
                    with contextlib.suppress(Exception):
                        _state.notify()
                else:
                    page.route = "/results"
                    _build_views_for_route("/results")
                    page.update()
            except Exception:
                with contextlib.suppress(Exception):
                    from qtea.logging_setup import get_logger as _get_logger
                    _get_logger(__name__).warning(
                        "ui.results_navigation_failed",
                        detail="page/session no longer available when the "
                        "run finished; summary not shown for this session",
                    )

    # ── Structural crash guard ─────────────────────────────────────────────
    #
    # Root cause behind every prior "summary screen didn't appear" fix: Flet
    # itself has exactly two places that catch an exception raised inside
    # user code — session.dispatch_event() for event handlers (button
    # clicks, on_route_change, on_view_pop, on_keyboard_event, ...) and
    # app.py's on_session_created() for main() itself — and BOTH react to
    # any uncaught exception the same way: log it, then call
    # session.error(), which sends SESSION_CRASHED to the client. The client
    # immediately reconnects, Flet invokes main() again for the new session,
    # and unless the *specific* exception that fired has already been
    # patched, the same thing happens again — a "blinking" reconnect loop
    # instead of the summary. Every prior fix (results-view None-safety,
    # workspace-listing guard, the two fixes earlier in this session) closed
    # off one specific exception source. That approach cannot be made
    # exhaustive by construction — there is always another possible
    # exception source.
    #
    # The fix that IS exhaustive: wrap every callable Flet invokes directly
    # as an event handler (on_click targets, on_route_change, on_view_pop,
    # on_keyboard_event) with `_safe_handler`, so control never reaches
    # Flet's own exception handling at all. Whatever went wrong is logged
    # here, and the UI is put back into a state that reflects `_state`
    # (running / finished / idle) instead of crashing. This covers any
    # exception, from any cause, present or future — not just the ones
    # already diagnosed.
    def _recover_view() -> None:
        """Best-effort resync of the visible view to `_state`, without ever
        raising itself. Called after a handler exception has been swallowed
        so the user is never left on a frozen or half-updated screen."""
        if _state.run_status == "running":
            route = "/run"
        elif _state.exit_code is not None:
            route = "/results"
        else:
            route = "/"
        with contextlib.suppress(Exception):
            page.route = route
            _build_views_for_route(route)
        with contextlib.suppress(Exception):
            page.update()

    def _safe_handler(func):
        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                with contextlib.suppress(Exception):
                    import traceback as _tb

                    from qtea.logging_setup import get_logger as _get_logger

                    _get_logger(__name__).error(
                        "ui.handler_exception",
                        handler=getattr(func, "__name__", str(func)),
                        error=str(exc),
                        traceback=_tb.format_exc()[:2000],
                    )
                _recover_view()

        return wrapper

    # ── Actions ──────────────────────────────────────────────────────────

    @_safe_handler
    def on_start_pipeline() -> None:
        _state.save_prefs()
        _state.reset_run()
        _state.run_status = "running"
        # Snapshot children that exist BEFORE the pipeline starts — notably the
        # flet-desktop Flutter GUI process. Stop kills only children spawned
        # AFTER this (the pipeline's subprocesses), so the window survives.
        with contextlib.suppress(Exception):
            import psutil

            _state.baseline_child_pids = {
                c.pid for c in psutil.Process().children(recursive=True)
            }
        page.route = "/run"
        _build_views_for_route("/run")
        page.update()
        page.run_task(_run_pipeline)
        page.run_task(_tick_elapsed)

    @_safe_handler
    def on_new_run() -> None:
        _state.reset_run()
        _state.operator_context = ""
        page.route = "/"
        _build_views_for_route("/")
        page.update()

    @_safe_handler
    def on_config_continue() -> None:
        """Config 'Start' handler (config already validated).

        Operator context is only consumed by Step 1 (ticket enrich) and Step 2
        (refine). Show the pre-run context screen only when it can still take
        effect — a fresh run, or a resume that re-enters at Step 1 or 2. A
        resume at Step 3+ skips those steps, so the screen would be a no-op:
        launch straight through (the prior run's stored context still carries
        forward via pipeline's resume fallback)."""
        _state.save_prefs()
        is_resume = bool(_state.resume_run_id)
        reenters_early = _state.from_step is not None and _state.from_step <= 2
        if is_resume and not reenters_early:
            on_start_pipeline()
            return
        if is_resume and reenters_early:
            # Pre-populate the box with the prior run's stored context so the
            # operator sees/edits what was used before. Best-effort: a missing
            # or unreadable state file just leaves the box empty.
            with contextlib.suppress(Exception):
                from qtea.checkpoints import load_state
                from qtea.config import get_settings

                base = get_settings().default_workspace
                prior = load_state(base / _state.resume_run_id / "state.json")
                if prior is not None and prior.operator_context:
                    _state.operator_context = prior.operator_context
        page.route = "/context"
        _build_views_for_route("/context")
        page.update()

    @_safe_handler
    def on_context_skip() -> None:
        _state.operator_context = ""
        _state.operator_context_images = []
        on_start_pipeline()

    @_safe_handler
    def on_context_continue(text: str) -> None:
        _state.operator_context = (text or "").strip()
        on_start_pipeline()

    # ── Routing ──────────────────────────────────────────────────────────

    def _fallback_view(route_label: str, exc: Exception) -> ft.Control:
        """Minimal, dependency-free view shown when ANY of the three real
        view builders (config / pipeline / results) fails to build.
        Intentionally uses only basic controls so it can never itself throw
        — the point is to never strand the user on a blank or stale screen,
        and to make the failure visible + recoverable regardless of which
        route it happened on."""
        run_id = getattr(_state, "run_id", None) or "N/A"
        ws = getattr(_state, "workspace_path", None)
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(height=24),
                    ft.Text(
                        f"{route_label} could not be displayed",
                        size=sz(22),
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Text(
                        f"Run: {run_id}"
                        + (f"  ·  Workspace: {ws}" if ws else ""),
                        size=sz(12),
                    ),
                    ft.Container(height=8),
                    ft.Text(
                        f"({type(exc).__name__}: {exc}). If a run is in "
                        "progress or finished, its artifacts are on disk "
                        "regardless of this screen.",
                        size=sz(13),
                        color="#FFB74D",
                    ),
                    ft.Container(height=16),
                    ft.ElevatedButton(
                        "New Run",
                        icon=ft.Icons.REPLAY,
                        on_click=lambda _: on_new_run(),
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
            ),
            expand=True,
            alignment=ft.Alignment.center,
            bgcolor=BACKGROUND,
            padding=40,
        )

    def _safe_build(builder, route_label: str) -> ft.Control:
        """Run a view builder, converting ANY exception it raises into the
        generic fallback view instead of letting it propagate. Applied to
        all three routes uniformly — config, pipeline, and results are all
        one bad state away from throwing, and there's no reason results
        should be the only one covered."""
        try:
            return builder()
        except Exception as exc:  # noqa: BLE001 — must never fall through
            import traceback as _tb

            from qtea.logging_setup import get_logger as _get_logger

            _get_logger(__name__).error(
                "ui.view_build_failed",
                route=route_label,
                error=str(exc),
                traceback=_tb.format_exc()[:2000],
            )
            with contextlib.suppress(Exception):
                from qtea.ui.state import LogLine as _LogLine

                _state.log_lines.append(
                    _LogLine(
                        timestamp="",
                        level="error",
                        event="ui.view_build_failed",
                        message=f"{route_label}: {exc}",
                    )
                )
            return _fallback_view(route_label, exc)

    def _build_views_for_route(route: str) -> None:
        try:
            page.views.clear()
            page.views.append(
                ft.View(
                    route="/",
                    controls=[
                        _safe_build(
                            lambda: build_config_view(
                                page,
                                _state,
                                on_config_continue,
                                spec_picker=spec_picker,
                                sut_picker=sut_picker,
                            ),
                            "/",
                        )
                    ],
                    padding=0,
                    bgcolor=BACKGROUND,
                )
            )
            if route == "/context":
                page.views.append(
                    ft.View(
                        route="/context",
                        controls=[
                            _safe_build(
                                lambda: build_context_capture_view(
                                    page,
                                    _state,
                                    on_context_skip,
                                    on_context_continue,
                                    context_image_picker,
                                ),
                                "/context",
                            )
                        ],
                        padding=0,
                        bgcolor=BACKGROUND,
                    )
                )
            elif route == "/run":
                page.views.append(
                    ft.View(
                        route="/run",
                        controls=[
                            _safe_build(
                                lambda: build_pipeline_view(page, _state), "/run"
                            )
                        ],
                        padding=0,
                        bgcolor=BACKGROUND,
                    )
                )
            elif route == "/results":
                page.views.append(
                    ft.View(
                        route="/results",
                        controls=[
                            _safe_build(
                                lambda: build_results_view(page, _state, on_new_run),
                                "/results",
                            )
                        ],
                        padding=0,
                        bgcolor=BACKGROUND,
                    )
                )
        except Exception as exc:
            # Backstop of last resort: something outside the builders
            # themselves failed (e.g. ft.View construction, page.views
            # mutation on a half-torn-down session). _safe_build already
            # covers builder exceptions, so reaching here means the failure
            # is in Flet/session plumbing this code doesn't control — still
            # never let it escape to Flet's own crash handling.
            with contextlib.suppress(Exception):
                import traceback as _tb

                from qtea.logging_setup import get_logger as _get_logger

                _get_logger(__name__).error(
                    "ui.build_views_failed",
                    route=route,
                    error=str(exc),
                    traceback=_tb.format_exc()[:2000],
                )
            with contextlib.suppress(Exception):
                page.views.clear()
                page.views.append(
                    ft.View(
                        route=route,
                        controls=[ft.Text(f"UI error: {exc}")],
                        padding=20,
                        bgcolor=BACKGROUND,
                    )
                )

    @_safe_handler
    def route_change(e):
        _build_views_for_route(page.route)
        page.update()

    @_safe_handler
    def view_pop(e):
        if len(page.views) <= 1:
            # Nothing to pop back to — stay on current view.
            return
        page.views.pop()
        top = page.views[-1]
        page.route = top.route
        page.update()

    page.on_route_change = route_change
    page.on_view_pop = view_pop

    # Expose the route-builder so components outside main()'s closure (the
    # Stop button in progress_header, for example) can trigger navigation
    # directly without waiting for a route_change round-trip. Callers pass
    # a route string, e.g. `page.data["navigate_to"]("/results")`.
    if page.data is None:
        page.data = {}
    if isinstance(page.data, dict):
        page.data["navigate_to"] = _build_views_for_route

    # ── Ctrl+/Ctrl- zoom (scales text + icon sizes app-wide; Ctrl+0 resets) ──
    @_safe_handler
    def _on_keyboard(e: ft.KeyboardEvent) -> None:
        if not e.ctrl:
            return
        if e.key in ("=", "+", "Numpad Add"):
            new_scale = theme.set_scale(theme.get_scale() + theme.SCALE_STEP)
        elif e.key in ("-", "Numpad Subtract"):
            new_scale = theme.set_scale(theme.get_scale() - theme.SCALE_STEP)
        elif e.key == "0":
            new_scale = theme.set_scale(1.0)
        else:
            return

        _state.text_scale = new_scale
        _state.save_prefs()

        if page.route == "/run" and _state.run_status == "running":
            # A full page.views.clear() + rebuild here would race with the
            # pipeline worker thread, which calls page.update() directly
            # (un-scheduled — see event_bridge.py's _UILogHandler) on every
            # log line. Tearing down the view tree from the UI thread while
            # that's happening corrupts Flet's rendered state (step cards
            # were observed reverting to "pending" mid-run). Instead, reuse
            # the same in-place refresh path the worker thread already
            # drives continuously via state.notify() — pipeline_view.py's
            # on_state_change rebuilds phase_groups/metrics/header/log
            # content (picking up the new sz() scale) without touching
            # page.views.
            _state.notify()
        else:
            # Idle (/) or finished (/results) route — no concurrent worker
            # thread, so a full rebuild is safe. Clear stale subscribers
            # first — pipeline_view.py re-subscribes to state on every
            # build, and _build_views_for_route reconstructs the whole view
            # stack, so without this, repeated zoom presses during a run
            # would leak duplicate on_state_change listeners.
            _state._listeners.clear()
            _build_views_for_route(page.route)

        # Live-resize whichever popup is currently open (rebuild in place).
        # This discards any not-yet-submitted text in that popup's fields.
        #
        # `show_hitl_dialog`/`show_review_gate_dialog` are idempotency-
        # guarded on `_dialog_open` (see their docstrings — prevents two
        # stacked AlertDialogs from a duplicate `state.notify()`). That
        # guard is exactly what silently swallowed the reopen call here:
        # `pop_dialog()` only tears down the widget, it doesn't clear the
        # flag (only `_close_dialog()` on submit/cancel does), so the
        # immediately-following `show_*_dialog()` saw `_dialog_open` still
        # True and no-opped — popping the old dialog with nothing to
        # replace it. Clear the flag first so the rebuild isn't blocked by
        # its own leftover state.
        if (
            _state.pending_hitl is not None
            and getattr(_state.pending_hitl, "_dialog_open", False)
        ):
            from qtea.ui.components.hitl_dialog import show_hitl_dialog

            _state.pending_hitl._dialog_open = False  # type: ignore[attr-defined]
            page.pop_dialog()
            show_hitl_dialog(page, _state)
        elif (
            _state.pending_review_gate is not None
            and getattr(_state.pending_review_gate, "_dialog_open", False)
        ):
            from qtea.ui.components.hitl_dialog import show_review_gate_dialog

            _state.pending_review_gate._dialog_open = False  # type: ignore[attr-defined]
            page.pop_dialog()
            show_review_gate_dialog(page, _state)
        elif _state.active_step_dialog_num is not None:
            step = _state.steps.get(_state.active_step_dialog_num)
            if step is not None:
                from qtea.ui.components.step_details_dialog import (
                    show_step_details_dialog,
                )

                page.pop_dialog()
                show_step_details_dialog(page, _state, step)

        page.update()

    page.on_keyboard_event = _on_keyboard

    # Populate initial view directly — page.go(page.route) is a no-op when
    # route hasn't changed, so route_change wouldn't fire.
    #
    # `_state` is a module-level singleton shared by every session (this is a
    # single-user desktop app), so a *new* session — from a reconnect after a
    # brief disconnect, or a crash-recovery reconnect — must reflect whatever
    # run is already in flight or already finished, not always reset to the
    # empty config screen. Landing back on "/" after a run has already
    # completed/failed is exactly the "session ended but the main screen
    # shows instead of the summary" bug. `initial_route` was already computed
    # at the top of this function (the crash-loop bailout up there needs it
    # too, before any of this setup runs).
    #
    # _build_views_for_route is internally exception-proof (see _safe_build /
    # the backstop try/except above), but page.route/page.update() themselves
    # touch the session and this runs outside any _safe_handler wrapper —
    # guard it directly so a startup hiccup can't trigger even one
    # crash/reconnect cycle.
    with contextlib.suppress(Exception):
        page.route = initial_route
        _build_views_for_route(initial_route)
        page.update()
