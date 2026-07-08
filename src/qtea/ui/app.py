"""Flet application bootstrap: page setup, routing, and pipeline lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import flet as ft

from qtea.ui.state import AppState
from qtea.ui.theme import BACKGROUND, build_dark_theme

_state = AppState()


def main(page: ft.Page):
    page.title = "QTea"
    page.window.width = 1480
    page.window.height = 920
    page.window.min_width = 1100
    page.window.min_height = 700
    page.theme_mode = ft.ThemeMode.DARK
    page.dark_theme = build_dark_theme()
    page.bgcolor = BACKGROUND
    page.padding = 0

    _state.init_steps()
    _state.load_prefs()

    # Lazy imports to avoid circular refs
    from qtea.ui.views.config_view import build_config_view
    from qtea.ui.views.pipeline_view import build_pipeline_view
    from qtea.ui.views.results_view import build_results_view

    # ── File pickers (registered ONCE as page services) ──────────────────
    spec_picker = ft.FilePicker()
    sut_picker = ft.FilePicker()
    page.services.append(spec_picker)
    page.services.append(sut_picker)

    # ── Timer task for live elapsed updates ──────────────────────────────

    async def _tick_elapsed() -> None:
        """Advance the elapsed-time clock once per second.

        We update only the live elapsed widget (published on ``page.data``)
        rather than calling ``state.notify()``, which would rebuild every
        step card, the metrics panel, and the entire log list once per
        second. The clock is pause-aware via ``state.update_elapsed()`` —
        HITL waits don't accumulate.
        """
        from qtea.ui.components.progress_header import fmt_elapsed

        while _state.run_status == "running":
            _state.update_elapsed()
            widget = None
            step_widget = None
            if isinstance(page.data, dict):
                widget = page.data.get("live_elapsed")
                step_widget = page.data.get("live_step_elapsed")
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
        )

        event_bridge = UIEventBridge(_state, page)
        event_bridge.install()

        # Capture the Flet event loop so the HITL bridge (running in the
        # worker thread) can schedule UI work back onto it.
        ui_loop = asyncio.get_running_loop()
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
            review_gate_bridge.uninstall()
            hitl_bridge.uninstall()
            event_bridge.uninstall()
            page.route = "/results"
            _build_views_for_route("/results")
            page.update()

    # ── Actions ──────────────────────────────────────────────────────────

    def on_start_pipeline() -> None:
        _state.save_prefs()
        _state.reset_run()
        _state.run_status = "running"
        page.route = "/run"
        _build_views_for_route("/run")
        page.update()
        page.run_task(_run_pipeline)
        page.run_task(_tick_elapsed)

    def on_new_run() -> None:
        _state.reset_run()
        page.route = "/"
        _build_views_for_route("/")
        page.update()

    # ── Routing ──────────────────────────────────────────────────────────

    def _build_views_for_route(route: str) -> None:
        page.views.clear()
        page.views.append(
            ft.View(
                route="/",
                controls=[
                    build_config_view(
                        page,
                        _state,
                        on_start_pipeline,
                        spec_picker=spec_picker,
                        sut_picker=sut_picker,
                    )
                ],
                padding=0,
                bgcolor=BACKGROUND,
            )
        )
        if route == "/run":
            page.views.append(
                ft.View(
                    route="/run",
                    controls=[build_pipeline_view(page, _state)],
                    padding=0,
                    bgcolor=BACKGROUND,
                )
            )
        elif route == "/results":
            page.views.append(
                ft.View(
                    route="/results",
                    controls=[build_results_view(page, _state, on_new_run)],
                    padding=0,
                    bgcolor=BACKGROUND,
                )
            )

    def route_change(e):
        _build_views_for_route(page.route)
        page.update()

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

    # ── Ctrl+/Ctrl- zoom (resize window ±10%; Ctrl+0 resets) ────────────
    _DEFAULT_W = int(page.window.width or 1480)
    _DEFAULT_H = int(page.window.height or 920)
    _STEP = 0.10  # 10% per keypress

    def _on_keyboard(e: ft.KeyboardEvent) -> None:
        if not e.ctrl:
            return
        w = int(page.window.width or _DEFAULT_W)
        h = int(page.window.height or _DEFAULT_H)
        if e.key in ("=", "+", "Numpad Add"):
            page.window.width = max(800, round(w * (1 + _STEP)))
            page.window.height = max(600, round(h * (1 + _STEP)))
        elif e.key in ("-", "Numpad Subtract"):
            page.window.width = max(800, round(w * (1 - _STEP)))
            page.window.height = max(600, round(h * (1 - _STEP)))
        elif e.key == "0":
            page.window.width = _DEFAULT_W
            page.window.height = _DEFAULT_H
        else:
            return
        page.update()

    page.on_keyboard_event = _on_keyboard

    # Populate initial view directly — page.go(page.route) is a no-op when
    # route hasn't changed, so route_change wouldn't fire.
    _build_views_for_route("/")
    page.update()
