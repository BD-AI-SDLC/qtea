"""Configuration form — the start screen for launching a pipeline run."""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from qtea.ui.state import AppState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PRIMARY,
    SECONDARY,
)


def build_config_view(
    page: ft.Page,
    state: AppState,
    on_start: Callable[[], None],
    spec_picker: ft.FilePicker | None = None,
    sut_picker: ft.FilePicker | None = None,
) -> ft.Container:
    """Build the configuration form view."""

    # ── Spec source ──────────────────────────────────────────────────────
    spec_field = ft.TextField(
        label="Spec Source",
        hint_text="jira:KEY-123  |  https://...  |  /path/to/spec.md",
        value=state.spec,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=13,
        expand=True,
        on_change=lambda e: setattr(state, "spec", e.data or ""),
    )

    async def _pick_spec(e):
        if spec_picker is None:
            return
        files = await spec_picker.pick_files(
            dialog_title="Select spec file",
            allowed_extensions=["md", "json", "txt", "yaml", "yml"],
        )
        if files:
            state.spec = files[0].path
            spec_field.value = state.spec
            page.update()

    spec_row = ft.Row(
        controls=[
            spec_field,
            ft.IconButton(
                icon=ft.Icons.FOLDER_OPEN,
                tooltip="Browse for spec file",
                icon_color=PRIMARY,
                on_click=_pick_spec,
            ),
        ],
        spacing=4,
    )

    # ── SUT path ─────────────────────────────────────────────────────────
    sut_field = ft.TextField(
        label="System Under Test (SUT)",
        hint_text="/path/to/project  |  https://github.com/org/repo",
        value=state.sut,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=13,
        expand=True,
        on_change=lambda e: setattr(state, "sut", e.data or ""),
    )

    async def _pick_sut(e):
        if sut_picker is None:
            return
        path = await sut_picker.get_directory_path(
            dialog_title="Select SUT directory"
        )
        if path:
            state.sut = path
            sut_field.value = state.sut
            page.update()

    sut_row = ft.Row(
        controls=[
            sut_field,
            ft.IconButton(
                icon=ft.Icons.FOLDER_OPEN,
                tooltip="Browse for SUT directory",
                icon_color=PRIMARY,
                on_click=_pick_sut,
            ),
        ],
        spacing=4,
    )

    # ── Toggle switches ──────────────────────────────────────────────────
    # Flet sends Switch/Checkbox state changes as native Python bools in
    # ``e.data`` (and on ``e.control.value``). The old ``e.data == "true"``
    # string compare always returned False, so any toggle set the underlying
    # state to False — turning the Headless switch ON silently produced
    # headed test runs in Step 9. Read ``e.control.value`` instead: it is
    # the authoritative bool the user just landed on.
    headless_switch = ft.Switch(
        label="Headless",
        value=state.headless,
        active_color=SECONDARY,
        on_change=lambda e: setattr(state, "headless", bool(e.control.value)),
    )

    # The debug agent runs automatically on final-failure and the fix-proposal
    # chain (critical-thinking → principal-software-engineer) auto-fires on
    # top of it. Both used to be UI toggles; opinionated defaults now, with
    # `--no-fix` / `--debug` on the CLI for power users.
    switches_row = ft.Row(
        controls=[headless_switch],
        spacing=24,
        wrap=True,
    )

    # ── Dropdowns ────────────────────────────────────────────────────────
    report_dropdown = ft.Dropdown(
        label="Report",
        value=state.report,
        options=[
            ft.dropdown.Option("auto"),
            ft.dropdown.Option("allure"),
            ft.dropdown.Option("builtin"),
            ft.dropdown.Option("both"),
        ],
        width=160,
        text_size=13,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        on_select=lambda e: setattr(state, "report", e.data),
    )

    cache_dropdown = ft.Dropdown(
        label="Cache",
        value=state.cache,
        options=[
            ft.dropdown.Option("auto"),
            ft.dropdown.Option("on"),
            ft.dropdown.Option("off"),
        ],
        width=130,
        text_size=13,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        on_select=lambda e: setattr(state, "cache", e.data),
    )

    log_level_dropdown = ft.Dropdown(
        label="Log Level",
        value=state.log_level,
        options=[
            ft.dropdown.Option("info"),
            ft.dropdown.Option("debug"),
            ft.dropdown.Option("trace"),
        ],
        width=130,
        text_size=13,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        on_select=lambda e: setattr(state, "log_level", e.data),
    )

    dropdowns_row = ft.Row(
        controls=[report_dropdown, cache_dropdown, log_level_dropdown],
        spacing=16,
    )

    # ── Parallel workers slider ──────────────────────────────────────────
    workers_label = ft.Text(
        f"Parallel Workers: {state.parallel_run}",
        size=13,
        color=ON_SURFACE,
    )

    def on_workers_change(e: ft.ControlEvent) -> None:
        state.parallel_run = int(float(e.data))
        workers_label.value = f"Parallel Workers: {state.parallel_run}"
        page.update()

    workers_slider = ft.Slider(
        min=0,
        max=16,
        divisions=16,
        value=state.parallel_run,
        active_color=SECONDARY,
        on_change=on_workers_change,
        expand=True,
    )

    workers_row = ft.Row(
        controls=[workers_label, workers_slider],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    # ── Skip steps ───────────────────────────────────────────────────────
    from qtea.ui.state import STEP_DEFINITIONS

    skip_checks: list[ft.Control] = []
    for num, name, _ in STEP_DEFINITIONS:

        def make_handler(n: int) -> Callable:
            def handler(e: ft.ControlEvent) -> None:
                # Same fix as the Switch handlers above: Flet sends a real
                # bool on ``e.control.value``; comparing ``e.data == "true"``
                # always missed, so toggling a skip-step checkbox never
                # actually added the step to the skip set.
                if bool(e.control.value):
                    state.skip_steps.add(n)
                else:
                    state.skip_steps.discard(n)

            return handler

        skip_checks.append(
            ft.Checkbox(
                label=str(num),
                value=num in state.skip_steps,
                active_color=SECONDARY,
                on_change=make_handler(num),
            )
        )

    skip_row = ft.Column(
        controls=[
            ft.Text("Skip Steps", size=13, color=ON_SURFACE_DIM),
            ft.Row(controls=skip_checks, spacing=4, wrap=True),
        ],
        spacing=4,
    )

    # ── Optional fields ──────────────────────────────────────────────────
    storage_field = ft.TextField(
        label="Storage State (optional)",
        hint_text="Path to storageState.json",
        value=state.storage_state,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=13,
        on_change=lambda e: setattr(state, "storage_state", e.data or ""),
    )

    dev_locators_field = ft.TextField(
        label="Dev Locators (optional)",
        hint_text="Path to dev-locators.json",
        value=state.dev_locators,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=13,
        on_change=lambda e: setattr(state, "dev_locators", e.data or ""),
    )

    optional_row = ft.Row(
        controls=[
            ft.Container(content=storage_field, expand=True),
            ft.Container(content=dev_locators_field, expand=True),
        ],
        spacing=16,
    )

    # ── Resume (--run-id + --from-step) ──────────────────────────────────
    from qtea.ui.state import STEP_DEFINITIONS as _STEPS
    from qtea.ui.workspaces import get_workspace, list_workspaces

    def _label_for(entry) -> str:
        # e.g. "20260624-122905-ab12  ·  failed · last=4 · reqi.md"
        spec = entry.spec_source or "-"
        leaf = spec.replace("\\", "/").rsplit("/", 1)[-1] or spec
        last = "-" if entry.last_step is None else str(entry.last_step)
        return f"{entry.run_id}  ·  {entry.status} · last={last} · {leaf}"

    _workspace_entries = list_workspaces()
    _ws_options: list[ft.dropdown.Option] = [
        ft.dropdown.Option("", "(fresh run)"),
    ] + [
        ft.dropdown.Option(e.run_id, _label_for(e))
        for e in _workspace_entries
    ]

    resume_dropdown = ft.Dropdown(
        label="Resume from run",
        value=state.resume_run_id or "",
        options=_ws_options,
        text_size=12,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        expand=True,
    )

    from_step_dropdown = ft.Dropdown(
        label="From step",
        value=str(state.from_step) if state.from_step else "",
        options=[ft.dropdown.Option("", "—")] + [
            ft.dropdown.Option(str(n), f"{n:02d}. {name}")
            for n, name, _ in _STEPS
        ],
        text_size=12,
        width=200,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        disabled=not state.resume_run_id,
    )

    resume_hint = ft.Text(
        "",
        size=11,
        color=ON_SURFACE_DIM,
    )

    def _on_resume_select(e: ft.ControlEvent) -> None:
        run_id = e.data or ""
        state.resume_run_id = run_id
        if not run_id:
            # Fresh run — clear from_step, re-enable spec/sut for free entry.
            state.from_step = None
            from_step_dropdown.value = ""
            from_step_dropdown.disabled = True
            resume_hint.value = ""
            page.update()
            return

        entry = get_workspace(run_id)
        from_step_dropdown.disabled = False
        if entry is None:
            resume_hint.value = "Could not load state.json for this workspace."
            page.update()
            return

        # Auto-fill spec/sut from the saved state (CLI does the same on resume).
        if entry.spec_source and not state.spec:
            state.spec = entry.spec_source
            spec_field.value = entry.spec_source
        if entry.sut_source and not state.sut:
            state.sut = entry.sut_source
            sut_field.value = entry.sut_source

        # Default to the step AFTER the last completed one.
        default_step = (entry.last_step or 0) + 1
        if 1 <= default_step <= len(_STEPS):
            state.from_step = default_step
            from_step_dropdown.value = str(default_step)
        resume_hint.value = (
            f"Last completed: step {entry.last_step or '-'} · "
            f"defaulting to step {default_step}. "
            "Spec/SUT auto-filled if previously empty."
        )
        page.update()

    def _on_from_step_select(e: ft.ControlEvent) -> None:
        raw = e.data or ""
        state.from_step = int(raw) if raw.isdigit() else None

    resume_dropdown.on_select = _on_resume_select
    from_step_dropdown.on_select = _on_from_step_select

    resume_row = ft.Column(
        controls=[
            ft.Row(
                controls=[resume_dropdown, from_step_dropdown],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            resume_hint,
        ],
        spacing=4,
    )

    # ── Validation + start ───────────────────────────────────────────────
    error_text = ft.Text("", size=12, color="#FF5252", visible=False)

    def on_start_click(e: ft.ControlEvent) -> None:
        # Sync field values
        state.spec = spec_field.value or ""
        state.sut = sut_field.value or ""

        if not state.spec.strip():
            error_text.value = "Spec source is required."
            error_text.visible = True
            page.update()
            return
        if not state.sut.strip():
            error_text.value = "SUT path is required."
            error_text.visible = True
            page.update()
            return

        # Resume validation: --from-step requires --run-id (mirrors the
        # pipeline-side check in pipeline.py:_select_workspace).
        if state.from_step is not None and not state.resume_run_id:
            error_text.value = (
                "From-step is set but no Resume-run is selected — pick a "
                "run-id to resume into, or clear From-step for a fresh run."
            )
            error_text.visible = True
            page.update()
            return
        if state.resume_run_id and state.from_step is None:
            error_text.value = (
                "Resume-run is selected but From-step is empty — pick the "
                "step to re-enter."
            )
            error_text.visible = True
            page.update()
            return

        error_text.visible = False
        on_start()

    start_button = ft.ElevatedButton(content="Start Pipeline",
        icon=ft.Icons.ROCKET_LAUNCH,
        bgcolor=PRIMARY,
        color="#FFFFFF",
        style=ft.ButtonStyle(
            padding=ft.Padding.symmetric(horizontal=32, vertical=16),
            text_style=ft.TextStyle(size=16, weight=ft.FontWeight.BOLD),
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
        on_click=on_start_click,
    )

    # ── Assemble form ────────────────────────────────────────────────────
    form = ft.Container(
        content=ft.Column(
            controls=[
                # Header
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Icon(ft.Icons.ROCKET_LAUNCH, color=PRIMARY, size=32),
                                    ft.Text(
                                        "QTea",
                                        size=28,
                                        weight=ft.FontWeight.BOLD,
                                        color=PRIMARY,
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.CENTER,
                                spacing=12,
                            ),
                            ft.Text(
                                "Autonomous QA SDLC Pipeline",
                                size=14,
                                color=ON_SURFACE_DIM,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    ),
                    padding=ft.Padding.only(bottom=20),
                ),
                # Required fields
                ft.Text(
                    "REQUIRED",
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                spec_row,
                sut_row,
                ft.Container(height=8),
                # Options
                ft.Text(
                    "OPTIONS",
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                switches_row,
                dropdowns_row,
                workers_row,
                skip_row,
                ft.Container(height=4),
                # Optional
                ft.Text(
                    "ADVANCED",
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                optional_row,
                ft.Container(height=4),
                ft.Text(
                    "RESUME",
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                resume_row,
                ft.Container(height=12),
                # Start
                error_text,
                ft.Row(
                    controls=[start_button],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            ],
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
        ),
        width=720,
        padding=32,
        bgcolor=CARD_BG,
        border_radius=16,
        border=ft.Border.all(1, DIVIDER),
    )

    # Center the form on the page
    return ft.Container(
        content=form,
        expand=True,
        alignment=ft.Alignment.CENTER,
        bgcolor=BACKGROUND,
        padding=ft.Padding.symmetric(vertical=20),
    )
