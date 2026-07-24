"""Pre-run operator context capture — shown between config and launch.

Optional free-text guidance (and optional context images) about the spec that
the operator supplies at run start. Both are inlined as trusted guidance into
Step 2 refinement (see ``PipelineOptions.operator_context`` /
``operator_context_images``). Skipping leaves them empty, in which case pipeline
behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from qtea.context_images import (
    ALLOWED_IMAGE_EXTENSIONS,
    MAX_CONTEXT_IMAGES,
    ContextImageError,
    validate_image_file,
)
from qtea.ui.state import AppState
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE_DIM,
    PRIMARY,
    sz,
)

_ERROR_COLOR = "#FF6B6B"

_PLACEHOLDER = (
    "Optional. Anything not in the ticket that helps sharpen the spec:\n"
    "  • Environment / URLs — e.g. staging is at https://stg.example\n"
    "  • Scope focus — e.g. concentrate on the checkout path\n"
    "  • Known-flaky areas — e.g. the card-number field is slow to load\n"
    "  • Out of scope — e.g. ignore the legacy admin panel\n"
    "  • Domain terms — e.g. what 'SKU' means here"
)


def build_context_capture_view(
    page: ft.Page,
    state: AppState,
    on_skip: Callable[[], None],
    on_continue: Callable[[str], None],
    image_picker: ft.FilePicker | None = None,
) -> ft.Container:
    """Build the pre-run operator-context capture view.

    ``on_skip`` launches the run with no context; ``on_continue`` receives the
    entered text and launches the run with it. ``image_picker`` (a page-service
    FilePicker) drives the optional "from PC" image upload; PNG/JPEG/GIF/WebP
    only, at most ``MAX_CONTEXT_IMAGES`` images of ≤ 5 MB each.
    """

    context_field = ft.TextField(
        label="Context about the requirement (optional)",
        hint_text=_PLACEHOLDER,
        value=state.operator_context,
        multiline=True,
        min_lines=8,
        max_lines=18,
        border_color=DIVIDER,
        focused_border_color=PRIMARY,
        text_size=sz(13),
        expand=True,
        on_change=lambda e: setattr(state, "operator_context", e.data or ""),
    )

    # ── Optional image attachment ────────────────────────────────────────
    selected_list = ft.Column(spacing=4)
    error_text = ft.Text("", color=_ERROR_COLOR, size=sz(12), visible=False)
    count_label = ft.Text("", size=sz(12), color=ON_SURFACE_DIM)

    def _render_selected() -> None:
        imgs = state.operator_context_images
        count_label.value = f"{len(imgs)}/{MAX_CONTEXT_IMAGES} images"
        rows: list[ft.Control] = []
        for path in imgs:
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            rows.append(
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.IMAGE_OUTLINED, color=PRIMARY, size=sz(16)),
                        ft.Text(name, size=sz(12), expand=True, no_wrap=True),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE,
                            icon_size=sz(14),
                            icon_color=ON_SURFACE_DIM,
                            tooltip="Remove",
                            on_click=lambda _e, p=path: _remove(p),
                        ),
                    ],
                    spacing=6,
                    alignment=ft.MainAxisAlignment.START,
                )
            )
        selected_list.controls = rows

    def _remove(path: str) -> None:
        state.operator_context_images = [
            p for p in state.operator_context_images if p != path
        ]
        error_text.visible = False
        _render_selected()
        page.update()

    async def _pick_images(_e) -> None:
        if image_picker is None:
            return
        files = await image_picker.pick_files(
            dialog_title="Select context image(s)",
            allow_multiple=True,
            allowed_extensions=list(ALLOWED_IMAGE_EXTENSIONS),
        )
        if not files:
            return
        current = list(state.operator_context_images)
        errors: list[str] = []
        for f in files:
            path = getattr(f, "path", None)
            if not path or path in current:
                continue
            if len(current) >= MAX_CONTEXT_IMAGES:
                errors.append(
                    f"Limit is {MAX_CONTEXT_IMAGES} images — some were skipped."
                )
                break
            try:
                validate_image_file(path)
            except ContextImageError as exc:
                errors.append(str(exc))
                continue
            current.append(path)
        state.operator_context_images = current
        error_text.value = "  ".join(dict.fromkeys(errors))
        error_text.visible = bool(errors)
        _render_selected()
        page.update()

    add_images_button = ft.OutlinedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.ADD_PHOTO_ALTERNATE_OUTLINED, size=sz(18)),
                ft.Text("Add images (optional)", size=sz(13)),
            ],
            spacing=6,
            tight=True,
        ),
        style=ft.ButtonStyle(color=PRIMARY),
        on_click=_pick_images,
        disabled=image_picker is None,
    )

    _render_selected()

    images_section = ft.Column(
        controls=[
            ft.Row(
                controls=[add_images_button, count_label],
                alignment=ft.MainAxisAlignment.START,
                spacing=12,
            ),
            ft.Text(
                "From your PC — PNG, JPEG, GIF, or WebP · ≤ 5 MB each · "
                f"up to {MAX_CONTEXT_IMAGES} images.",
                size=sz(11),
                color=ON_SURFACE_DIM,
            ),
            error_text,
            selected_list,
        ],
        spacing=6,
    )

    skip_button = ft.TextButton(
        content=ft.Text("Skip", size=sz(15)),
        style=ft.ButtonStyle(
            color=ON_SURFACE_DIM,
            padding=ft.Padding.symmetric(horizontal=24, vertical=16),
        ),
        on_click=lambda _: on_skip(),
    )

    continue_button = ft.ElevatedButton(
        content="Continue",
        icon=ft.Icons.ARROW_FORWARD,
        bgcolor=PRIMARY,
        color="#FFFFFF",
        style=ft.ButtonStyle(
            padding=ft.Padding.symmetric(horizontal=32, vertical=16),
            text_style=ft.TextStyle(size=sz(16), weight=ft.FontWeight.BOLD),
            shape=ft.RoundedRectangleBorder(radius=10),
        ),
        on_click=lambda _: on_continue(context_field.value or ""),
    )

    card = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.LIGHTBULB_OUTLINE, color=PRIMARY, size=sz(28)),
                        ft.Text(
                            "Add context before the run",
                            size=sz(24),
                            weight=ft.FontWeight.BOLD,
                            color=PRIMARY,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=12,
                ),
                ft.Text(
                    "Trusted guidance that sharpens spec refinement. It "
                    "augments the ticket — it never overrides the acceptance "
                    "criteria. Leave blank to skip.",
                    size=sz(13),
                    color=ON_SURFACE_DIM,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Container(height=12),
                context_field,
                ft.Container(height=12),
                images_section,
                ft.Container(height=16),
                ft.Row(
                    controls=[skip_button, continue_button],
                    alignment=ft.MainAxisAlignment.END,
                    spacing=12,
                ),
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        ),
        width=760,
        padding=32,
        bgcolor=CARD_BG,
        border_radius=16,
        border=ft.Border.all(1, DIVIDER),
    )

    return ft.Container(
        content=card,
        expand=True,
        alignment=ft.Alignment.CENTER,
        bgcolor=BACKGROUND,
        padding=ft.Padding.symmetric(vertical=20),
    )
