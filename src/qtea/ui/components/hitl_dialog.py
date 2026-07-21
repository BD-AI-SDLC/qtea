"""Modal dialogs for HITL questions and review gates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import flet as ft

from qtea.hitl import (
    RESOLUTION_ANSWERED,
    RESOLUTION_ANSWERED_SENSITIVE,
    RESOLUTION_HEADED_LOGIN_SKIP,
    RESOLUTION_OVERLAY_BUG,
    RESOLUTION_OVERLAY_ONCE,
    RESOLUTION_OVERLAY_PERSIST,
)
from qtea.ui.state import AppState, ReviewGateRequest
from qtea.ui.theme import (
    BACKGROUND,
    CARD_BG,
    DIVIDER,
    ON_SURFACE,
    ON_SURFACE_DIM,
    PRIMARY,
    SECONDARY,
    sz,
)

# Callback signature for per-row Edit submits. The dialog passes a closure
# that scopes ``instructions`` to the named row id and closes the modal —
# the existing edit_instructions / decision="edit" plumbing carries the
# scoped text through to the matching ``*-editor`` agent.
PerRowEditCallback = Callable[[str, str, str], None]
# ^ args: (row_id, row_title_or_label, raw_instructions)


def _build_overlay_dismiss_widget(
    q_id: str, metadata: dict,
) -> tuple[ft.Control, Callable[[], tuple[str, str] | None]]:
    """Build the overlay-dismiss widget group and a collector closure.

    The widget shows: (1) header with test id + overlay role/name, (2) the
    cropped screenshot inline via ``ft.Image``, (3) a radio group of
    candidate dismiss actions (AOM-extracted buttons + press-Escape + custom
    + "real bug"), and (4) a persist checkbox.

    Returns ``(widget, collector)``. ``collector()`` returns the
    ``(resolution, json_answer)`` tuple the parent expects, or ``None`` if
    the user hasn't picked anything (no-op on submit).
    """
    test_id = metadata.get("test_id") or "(unknown test)"
    overlay_role = metadata.get("overlay_role") or "?"
    overlay_name = metadata.get("overlay_name") or "?"
    page_url = metadata.get("page_url") or ""
    target_intent = metadata.get("target_intent") or "(unknown target)"
    screenshot_path = metadata.get("screenshot_path") or ""
    candidates = list(metadata.get("candidates") or [])

    # Image widget — file:// scheme so Flet reads from local disk. Missing
    # file falls back to a placeholder text so the dialog still renders.
    image_widget: ft.Control
    if screenshot_path and Path(screenshot_path).exists():
        image_widget = ft.Container(
            content=ft.Image(
                src=screenshot_path,
                fit=ft.ImageFit.CONTAIN,
                width=680,
                height=280,
                error_content=ft.Text(
                    f"(screenshot failed to load: {screenshot_path})",
                    size=sz(11), color=ON_SURFACE_DIM, italic=True,
                ),
            ),
            padding=6,
            border=ft.Border.all(1, DIVIDER),
            border_radius=6,
            bgcolor=BACKGROUND,
        )
    else:
        image_widget = ft.Container(
            content=ft.Text(
                "(no screenshot captured)" if not screenshot_path
                else f"(screenshot missing on disk: {screenshot_path})",
                size=sz(12), color=ON_SURFACE_DIM, italic=True,
            ),
            padding=12,
            border=ft.Border.all(1, DIVIDER),
            border_radius=6,
            bgcolor=BACKGROUND,
        )

    # Radio values are namespaced so we can decode intent + index on submit.
    # "cand:<idx>" for AOM candidates, "esc" for Escape, "custom" for custom,
    # "bug" for real-bug. Empty selection → collector returns None.
    radios: list[ft.Control] = []
    for idx, c in enumerate(candidates):
        role = c.get("role") or "?"
        name = c.get("name") or "?"
        safe = bool(c.get("safe"))
        safe_tag = "" if safe else "  [risky — verify]"
        color = ON_SURFACE if safe else "#FFB74D"
        radios.append(
            ft.Radio(
                value=f"cand:{idx}",
                label=f"Click {role} '{name}'{safe_tag}",
                fill_color=color,
                label_style=ft.TextStyle(color=color, size=sz(13)),
            )
        )
    radios.extend([
        ft.Radio(
            value="esc",
            label="Press Escape key",
            label_style=ft.TextStyle(color=ON_SURFACE, size=sz(13)),
        ),
        ft.Radio(
            value="custom",
            label="Custom locator (role + accessible name below)",
            label_style=ft.TextStyle(color=ON_SURFACE, size=sz(13)),
        ),
        ft.Radio(
            value="bug",
            label="This is a real bug — fail the test",
            label_style=ft.TextStyle(color="#FF5252", size=sz(13)),
        ),
    ])
    radio_group = ft.RadioGroup(
        value="",
        content=ft.Column(controls=radios, spacing=4, tight=True),
    )

    # Custom locator inputs (visible only when the "custom" radio is picked
    # — we don't wire the reveal here to keep the widget stateless; the
    # collector treats empty custom fields as an invalid submission).
    custom_role_dropdown = ft.Dropdown(
        value="button",
        options=[
            ft.dropdown.Option("button"),
            ft.dropdown.Option("link"),
            ft.dropdown.Option("menuitem"),
        ],
        width=160,
        text_size=sz(13),
        border_color=DIVIDER,
    )
    custom_name_field = ft.TextField(
        hint_text="Accessible name of dismiss control (leave blank if not using 'custom')",
        text_size=sz(13),
        border_color=DIVIDER,
        expand=True,
    )
    custom_row = ft.Row(
        controls=[
            ft.Text("Custom:", size=sz(12), color=ON_SURFACE_DIM, width=64),
            custom_role_dropdown,
            custom_name_field,
        ],
        spacing=8,
    )

    persist_checkbox = ft.Checkbox(
        value=True,
        label="Persist to interceptors.json so future runs are clean",
        fill_color=SECONDARY,
        label_style=ft.TextStyle(color=ON_SURFACE, size=sz(12)),
    )

    header = ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Text(
                            "OVERLAY", size=sz(10), weight=ft.FontWeight.BOLD,
                            color="#FFFFFF",
                        ),
                        bgcolor="#FFB74D",
                        border_radius=4,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    ),
                    ft.Text(q_id, size=sz(11), color=ON_SURFACE_DIM),
                ],
                spacing=8,
            ),
            ft.Text(
                f"Overlay blocked action: {target_intent}",
                size=sz(14), weight=ft.FontWeight.W_500, color=ON_SURFACE,
            ),
            ft.Text(
                f"role={overlay_role!r}  name={overlay_name!r}",
                size=sz(11), color=ON_SURFACE_DIM,
                selectable=True, font_family="Courier New",
            ),
            ft.Text(
                f"test:  {test_id}",
                size=sz(11), color=ON_SURFACE_DIM,
                selectable=True, font_family="Courier New",
            ),
            ft.Text(
                f"page:  {page_url}",
                size=sz(11), color=ON_SURFACE_DIM,
                selectable=True, font_family="Courier New",
                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ],
        spacing=3,
        tight=True,
    )

    widget = ft.Container(
        content=ft.Column(
            controls=[
                header,
                ft.Container(height=6),
                image_widget,
                ft.Container(height=6),
                ft.Text(
                    "Dismiss action:", size=sz(12), weight=ft.FontWeight.W_500,
                    color=ON_SURFACE,
                ),
                radio_group,
                custom_row,
                ft.Container(height=6),
                persist_checkbox,
            ],
            spacing=6,
            tight=True,
        ),
        padding=12,
        border=ft.Border.all(1, DIVIDER),
        border_radius=8,
    )

    def collector() -> tuple[str, str] | None:
        choice = radio_group.value or ""
        if not choice:
            return None
        if choice == "bug":
            return RESOLUTION_OVERLAY_BUG, json.dumps({"kind": "bug"})
        persist = bool(persist_checkbox.value)
        resolution = (
            RESOLUTION_OVERLAY_PERSIST if persist else RESOLUTION_OVERLAY_ONCE
        )
        if choice == "esc":
            return resolution, json.dumps({"kind": "press_escape"})
        if choice == "custom":
            name = (custom_name_field.value or "").strip()
            if not name:
                # Custom picked but no name → treat as no-op skip.
                return None
            role = (custom_role_dropdown.value or "button").strip()
            return resolution, json.dumps({"kind": "custom", "role": role, "name": name})
        if choice.startswith("cand:"):
            try:
                idx = int(choice.split(":", 1)[1])
            except (IndexError, ValueError):
                return None
            return resolution, json.dumps({
                "kind": "click_candidate", "candidate_index": idx,
            })
        return None

    return widget, collector


def _build_headed_login_content(
    q_id: str, q_text: str, metadata: dict,
) -> tuple[ft.Control, ft.Text]:
    """Bespoke content for the Step 7 headed-login confirm-or-skip question.

    Unlike the generic clarification card, there's nothing to type — the
    browser window itself is where the work happens. A neutral "LOGIN" chip
    (not the orange "CLARIFICATION"/"BLOCKER" ones) signals "an external
    action is in progress" rather than "the agent needs your knowledge".

    Returns ``(content, elapsed_text)`` — the caller owns starting/stopping
    the live ticker that updates ``elapsed_text.value``.
    """
    base_url = metadata.get("base_url") or ""
    elapsed_text = ft.Text("Waiting… 00:00", size=sz(13), color=ON_SURFACE_DIM)
    chip = ft.Container(
        content=ft.Text(
            "LOGIN", size=sz(10), weight=ft.FontWeight.BOLD, color="#FFFFFF",
        ),
        bgcolor="#2E7D9A",
        border_radius=4,
        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
    )
    return ft.Column(
        controls=[
            ft.Row(controls=[chip, ft.Text(q_id, size=sz(11), color=ON_SURFACE_DIM)], spacing=8),
            ft.Text(q_text, size=sz(13), color=ON_SURFACE, weight=ft.FontWeight.W_500),
            ft.Container(
                content=ft.Text(base_url, size=sz(12), color=ON_SURFACE_DIM, selectable=True),
                bgcolor=BACKGROUND,
                border_radius=4,
                border=ft.Border.all(1, DIVIDER),
                padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            ),
            elapsed_text,
            ft.Text(
                "Skipping does NOT capture your session — Step 7 proceeds "
                "unauthenticated.",
                size=sz(11), color=ON_SURFACE_DIM, italic=True,
            ),
        ],
        spacing=10,
        tight=True,
    ), elapsed_text


def _show_headed_login_dialog(page: ft.Page, req) -> None:
    """Bespoke 3-button dialog for the headed-login confirm/skip question.

    Bypasses the generic Submit/Skip-All bar entirely (that bar resolves
    ALL questions in the request at once and always shows a free-text
    field — wrong model for a pure "click when done" gate). "Reopen
    browser window" is a direct in-process call into
    :mod:`qtea.headed_auth_capture` and deliberately does NOT resolve or
    close the dialog.
    """
    from qtea import headed_auth_capture
    from qtea.ui.components.progress_header import fmt_elapsed

    q = req.questions[0]
    q_id = q.get("id", "")
    q_text = q.get("text", q.get("question", ""))
    metadata = q.get("metadata") or {}
    content, elapsed_text = _build_headed_login_content(q_id, q_text, metadata)

    started_at = metadata.get("started_at")
    t0 = started_at if isinstance(started_at, (int, float)) else time.monotonic()
    ticking = True

    async def _tick() -> None:
        while ticking:
            elapsed_text.value = f"Waiting… {fmt_elapsed(time.monotonic() - t0)}"
            try:
                elapsed_text.update()
            except Exception:
                with contextlib.suppress(Exception):
                    page.update()
            await asyncio.sleep(1)

    def _resolve(resolution: str) -> None:
        nonlocal ticking
        ticking = False
        req.answers[q_id] = (resolution, "")
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        page.pop_dialog()
        if req.completion_event:
            req.completion_event.set()

    def _on_confirm(e: ft.ControlEvent) -> None:
        _resolve(RESOLUTION_ANSWERED)

    def _on_skip(e: ft.ControlEvent) -> None:
        _resolve(RESOLUTION_HEADED_LOGIN_SKIP)

    def _on_reopen(e: ft.ControlEvent) -> None:
        # In-process call — headed_auth_capture owns the thread-safe hop
        # onto the pipeline's own event loop. Does not close the dialog.
        headed_auth_capture.request_browser_reopen(q_id)

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Step {req.step} — Waiting for you to log in",
            size=sz(16),
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Container(content=content, width=520),
        actions=[
            ft.TextButton(
                "Skip authentication — continue unauthenticated",
                style=ft.ButtonStyle(color="#FF5252"),
                on_click=_on_skip,
            ),
            ft.OutlinedButton("Reopen browser window", on_click=_on_reopen),
            ft.ElevatedButton(
                "I've Logged In — Continue",
                icon=ft.Icons.CHECK,
                bgcolor=SECONDARY,
                color="#FFFFFF",
                on_click=_on_confirm,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )

    # Mark BEFORE show_dialog so any synchronous re-entry from inside
    # page.show_dialog's update cycle hits the guard above (see show_hitl_dialog).
    try:
        req._dialog_open = True  # type: ignore[attr-defined]
    except Exception:
        pass
    page.show_dialog(dlg)
    page.run_task(_tick)


def show_hitl_dialog(page: ft.Page, state: AppState) -> None:
    """Show a modal dialog for pending HITL questions.

    Idempotent per request: ``HitlBridge._show_in_ui`` posts the request and
    fires ``state.notify()`` (which triggers ``on_state_change`` ->
    ``show_hitl_dialog``); but other code paths may also try to display the
    dialog. Without this guard, two ``AlertDialog`` instances get stacked
    onto ``page._dialogs.controls``. The user types in the topmost one,
    clicks Submit, and ``page.pop_dialog()`` closes only the top — revealing
    the still-empty bottom one. End-user symptom: "answers got wiped and
    submit didn't work."
    """
    req = state.pending_hitl
    if not req:
        return
    if getattr(req, "_dialog_open", False):
        return

    if (
        len(req.questions) == 1
        and (req.questions[0].get("metadata") or {}).get("type") == "headed_login"
    ):
        _show_headed_login_dialog(page, req)
        return

    answer_fields: dict[str, ft.TextField] = {}
    sensitive_checkboxes: dict[str, ft.Checkbox] = {}
    # Per-question collectors that override the default text-field read.
    # An overlay_dismiss question registers a closure here that maps the
    # user's radio selection (+ optional custom-name text + persist checkbox)
    # into the (resolution, json_answer) tuple the parent handler expects.
    answer_collectors: dict[str, Callable[[], tuple[str, str] | None]] = {}
    has_overlay_question = False

    # Build question widgets
    question_controls: list[ft.Control] = []
    for q in req.questions:
        q_id = q.get("id", "")
        q_text = q.get("text", q.get("question", ""))
        q_context = q.get("context", "")
        q_type = q.get("type", "blocker")
        q_metadata = q.get("metadata") or {}

        # Overlay-dismiss questions get a bespoke widget: screenshot on
        # top + radio candidates + persist checkbox. Bypasses the default
        # TextField loop entirely. First image widget in the qtea UI.
        if q_metadata.get("type") == "overlay_dismiss":
            has_overlay_question = True
            overlay_widget, collector = _build_overlay_dismiss_widget(
                q_id, q_metadata,
            )
            answer_collectors[q_id] = collector
            question_controls.append(overlay_widget)
            continue

        if q_type == "env":
            field = ft.TextField(
                password=True,
                can_reveal_password=True,
                border_color=DIVIDER,
                text_size=sz(13),
                hint_text="Type value — click eye icon to reveal",
                expand=True,
            )
            answer_fields[q_id] = field
            env_context_children: list[ft.Control] = []
            if q_context and q_context != q_text:
                env_context_children.append(
                    ft.Container(
                        content=ft.Text(
                            q_context,
                            size=sz(11),
                            color=ON_SURFACE_DIM,
                            italic=True,
                        ),
                        bgcolor=BACKGROUND,
                        border_radius=4,
                        border=ft.Border(left=ft.BorderSide(2, DIVIDER)),
                        padding=ft.Padding.only(left=8, top=4, bottom=4, right=4),
                    )
                )
            question_controls.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Container(
                                        content=ft.Text(
                                            "ENV",
                                            size=sz(10),
                                            weight=ft.FontWeight.BOLD,
                                            color="#FFFFFF",
                                        ),
                                        bgcolor="#7E57C2",
                                        border_radius=4,
                                        padding=ft.Padding.symmetric(
                                            horizontal=6, vertical=2
                                        ),
                                    ),
                                    ft.Text(q_id, size=sz(11), color=ON_SURFACE_DIM),
                                ],
                                spacing=8,
                            ),
                            ft.Text(
                                q_text,
                                size=sz(13),
                                color=ON_SURFACE,
                                weight=ft.FontWeight.W_500,
                            ),
                            *env_context_children,
                            field,
                        ],
                        spacing=6,
                    ),
                    padding=12,
                    border=ft.Border.all(1, DIVIDER),
                    border_radius=8,
                )
            )
            continue

        type_color = "#FF5252" if q_type == "blocker" else "#FFB74D"
        type_label = q_type.upper()

        field = ft.TextField(
            multiline=True,
            min_lines=4,
            max_lines=20,
            border_color=DIVIDER,
            text_size=sz(13),
            hint_text="Type your answer...",
            expand=True,
        )
        answer_fields[q_id] = field

        sensitive_cb = ft.Checkbox(
            value=False,
            label="Sensitive — store locally, don't send to LLM",
            fill_color=SECONDARY,
            label_style=ft.TextStyle(color=ON_SURFACE_DIM, size=sz(11)),
        )
        sensitive_checkboxes[q_id] = sensitive_cb

        question_controls.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Container(
                                    content=ft.Text(
                                        type_label,
                                        size=sz(10),
                                        weight=ft.FontWeight.BOLD,
                                        color="#FFFFFF",
                                    ),
                                    bgcolor=type_color,
                                    border_radius=4,
                                    padding=ft.Padding.symmetric(
                                        horizontal=6, vertical=2
                                    ),
                                ),
                                ft.Text(q_id, size=sz(11), color=ON_SURFACE_DIM),
                            ],
                            spacing=8,
                        ),
                        ft.Text(
                            q_text,
                            size=sz(13),
                            color=ON_SURFACE,
                            weight=ft.FontWeight.W_500,
                        ),
                        *(
                            [
                                ft.Container(
                                    content=ft.Text(
                                        q_context,
                                        size=sz(11),
                                        color=ON_SURFACE_DIM,
                                        italic=True,
                                        selectable=True,
                                    ),
                                    bgcolor=BACKGROUND,
                                    border_radius=4,
                                    border=ft.Border(
                                        left=ft.BorderSide(2, DIVIDER),
                                    ),
                                    padding=ft.Padding.only(
                                        left=8, top=4, bottom=4, right=4,
                                    ),
                                )
                            ]
                            if q_context and q_context != q_text
                            else []
                        ),
                        field,
                        sensitive_cb,
                    ],
                    spacing=6,
                ),
                padding=12,
                border=ft.Border.all(1, DIVIDER),
                border_radius=8,
            )
        )

    def on_submit(e: ft.ControlEvent) -> None:
        # Per-question collectors first — overlay dialogs return the full
        # (resolution, json_answer) tuple directly. Fall back to text
        # fields for the default clarification / blocker case.
        for q_id, collect in answer_collectors.items():
            result = collect()
            if result is not None:
                req.answers[q_id] = result
        for q_id, field in answer_fields.items():
            if q_id in answer_collectors:
                continue
            if field.value:
                cb = sensitive_checkboxes.get(q_id)
                resolution = (
                    RESOLUTION_ANSWERED_SENSITIVE
                    if cb and cb.value
                    else RESOLUTION_ANSWERED
                )
                req.answers[q_id] = (resolution, field.value)
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        # Queue the dialog removal before unblocking the waiter (see the
        # review-gate _close_dialog note): prevents a view rebuild from
        # racing an in-flight dialog close and crashing the Flutter client.
        page.pop_dialog()
        if req.completion_event:
            req.completion_event.set()

    def on_skip(e: ft.ControlEvent) -> None:
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        # Queue the dialog removal before unblocking the waiter (see the
        # review-gate _close_dialog note): prevents a view rebuild from
        # racing an in-flight dialog close and crashing the Flutter client.
        page.pop_dialog()
        if req.completion_event:
            req.completion_event.set()

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Step {req.step} — Input Required",
            size=sz(16),
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        f"Agent '{req.agent_label}' needs your input on "
                        f"{len(req.questions)} item(s).",
                        size=sz(13),
                        color=ON_SURFACE_DIM,
                    ),
                    ft.Container(height=8),
                    *question_controls,
                ],
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            width=760 if has_overlay_question else 600,
            height=(
                720 if has_overlay_question
                else min(500, 120 + len(req.questions) * 160)
            ),
        ),
        actions=[
            ft.TextButton("Skip All", on_click=on_skip),
            ft.ElevatedButton(
                "Submit",
                icon=ft.Icons.SEND,
                bgcolor=SECONDARY,
                color="#FFFFFF",
                on_click=on_submit,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    # Mark BEFORE show_dialog so any synchronous re-entry from inside
    # page.show_dialog's update cycle hits the guard above.
    try:
        req._dialog_open = True  # type: ignore[attr-defined]
    except Exception:
        pass
    page.show_dialog(dlg)


_PRI_BADGE_COLORS: dict[str, str] = {
    "P0": "#FF5252",
    "P1": "#FFB74D",
    "P2": "#66BB6A",
    "P3": "#7E57C2",
}

# Step 7 plan source tags. Green = reusing existing SUT code (safe, no
# new surface). Yellow = creating new code or a TBD intent (review-worthy).
_SOURCE_BADGE_COLORS: dict[str, str] = {
    "reuse": "#66BB6A",
    "create": "#FFB74D",
    "create_tbd": "#FFB74D",
    "tbd": "#FFB74D",
}


def _badge(text: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=sz(10), weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        bgcolor=color,
        border_radius=4,
        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
    )


def _parse_raw_tc_markdown(raw: str) -> dict[str, list[str] | str]:
    """Pull Preconditions / Steps / Expected out of the planner's raw markdown.

    The planner emits each TC as markdown like::

        - **Preconditions:**
          - Authenticated user
        - **Steps:**
          1. Open the app
          2. Click the button
        - **Expected Result:**
          - The button is visible

    The structured ``steps`` / ``preconditions`` fields on the JSON test
    case are often empty (the markdown was parsed only loosely). Falling
    back to ``raw`` lets the reviewer see the actual content.
    """
    out: dict[str, list[str] | str] = {
        "preconditions": [],
        "steps": [],
        "expected": "",
    }
    if not raw:
        return out

    # Patterns match both `**Steps:**` (colon inside) and `**Steps**:` (colon
    # outside) — agents emit either form depending on model/prompt variation.
    _SEC_PRE = re.compile(r"\*\*preconditions?\*\*:?|preconditions?:", re.I)
    _SEC_STE = re.compile(r"\*\*steps?\*\*:?|steps?:", re.I)
    _SEC_EXP = re.compile(r"\*\*expected\s+results?\*\*:?|\*\*expected\*\*:?", re.I)
    # Generic `- **Foo:**` / `- **Foo**:` header that is NOT one of the known
    # sections above — resets the current section so its content is skipped.
    _SEC_OTHER = re.compile(r"^-?\s*\*\*[^*]+\*\*:?$")

    section: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        # Known section headers — checked before the generic "other header"
        # pattern so `**Steps**:` is recognised before being swallowed.
        if _SEC_PRE.search(low):
            section = "preconditions"
            continue
        if _SEC_STE.search(low):
            section = "steps"
            continue
        if _SEC_EXP.search(low):
            section = "expected"
            continue
        if _SEC_OTHER.match(stripped):
            # Some other header (Type, Priority, etc.) — leave the section.
            section = None
            continue
        if stripped.startswith("---"):
            section = None
            continue

        if section is None or not stripped:
            continue

        # Strip bullet/number prefixes: "- foo", "1. foo", "  - foo"
        item = stripped
        for prefix in ("- ", "* "):
            if item.startswith(prefix):
                item = item[len(prefix):].strip()
                break
        else:
            # Numbered list: "1. foo"
            head, sep, tail = item.partition(". ")
            if sep and head.isdigit():
                item = tail.strip()

        if not item:
            continue

        if section == "expected":
            cur = out.get("expected") or ""
            out["expected"] = (cur + " " + item).strip() if cur else item
        else:
            out[section].append(item)  # type: ignore[union-attr]

    return out


def _render_tc_section(
    title: str, items: list[str], numbered: bool = False,
) -> ft.Control:
    """One Preconditions/Steps/Expected sub-section.

    Module-level so the strategy accordion and any future renderer can share it.
    """
    if not items:
        return ft.Container(
            content=ft.Text(
                f"{title}: —",
                size=sz(11),
                color=ON_SURFACE_DIM,
                italic=True,
            ),
            padding=ft.Padding.symmetric(vertical=2),
        )
    bullets: list[ft.Control] = []
    for i, it in enumerate(items, start=1):
        marker = f"{i}." if numbered else "•"
        bullets.append(
            ft.Row(
                controls=[
                    ft.Text(
                        marker,
                        size=sz(12),
                        color=ON_SURFACE_DIM,
                        width=22,
                    ),
                    ft.Text(
                        it,
                        size=sz(12),
                        color=ON_SURFACE,
                        selectable=True,
                        expand=True,
                    ),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
        )
    return ft.Column(
        controls=[
            ft.Text(
                title.upper(),
                size=sz(10),
                weight=ft.FontWeight.BOLD,
                color=ON_SURFACE_DIM,
            ),
            *bullets,
        ],
        spacing=3,
        tight=True,
    )


def _tc_details_body(tc: dict) -> ft.Control:
    """Body shown when an accordion TC row is expanded.

    Header (id, priority, title) lives on the row itself — only the
    Preconditions / Steps / Expected Result sections go here.
    """
    parsed_raw = _parse_raw_tc_markdown(tc.get("raw") or "")
    preconditions = list(tc.get("preconditions") or []) or list(
        parsed_raw.get("preconditions") or []  # type: ignore[arg-type]
    )
    steps = list(tc.get("steps") or []) or list(
        parsed_raw.get("steps") or []  # type: ignore[arg-type]
    )
    expected = tc.get("expected") or parsed_raw.get("expected") or ""

    return ft.Column(
        controls=[
            _render_tc_section("Preconditions", preconditions),
            ft.Container(height=6),
            _render_tc_section("Steps", steps, numbered=True),
            ft.Container(height=6),
            _render_tc_section(
                "Expected Result",
                [expected] if expected else [],
            ),
        ],
        spacing=4,
        tight=True,
    )


def _make_per_row_edit_panel(
    row_id: str,
    row_label: str,
    on_submit: PerRowEditCallback,
    *,
    hint: str | None = None,
) -> tuple[ft.Container, Callable[[], None]]:
    """Build an inline edit panel scoped to one accordion row / plan card.

    Returns ``(panel, toggle)`` — ``panel`` is the visible=False container
    holding caption + text field + Cancel/Submit buttons; ``toggle`` flips
    its visibility. ``on_submit`` receives ``(row_id, row_label, text)``
    when the user clicks Submit with non-empty input.
    """
    caption_default = (
        f"Edit instructions for {row_id} — queued when you click "
        "“Queue edit”, applied together when you Approve below"
    )
    edit_field = ft.TextField(
        multiline=True,
        min_lines=2,
        max_lines=4,
        border_color=DIVIDER,
        text_size=sz(12),
        hint_text=hint or f"What should change about {row_id}?",
    )
    edit_caption = ft.Text(
        caption_default,
        size=sz(11),
        weight=ft.FontWeight.W_500,
        color=ON_SURFACE_DIM,
    )

    open_state = {"v": False}

    def _do_submit(_e: ft.ControlEvent) -> None:
        text = (edit_field.value or "").strip()
        if not text:
            edit_field.border_color = "#FFB74D"
            edit_caption.value = (
                f"Type your edit instructions for {row_id}, "
                "then click Queue edit."
            )
            edit_caption.color = "#FFB74D"
            try:
                edit_field.update()
                edit_caption.update()
            except Exception:
                pass
            return
        on_submit(row_id, row_label, text)
        # Queue-and-collapse: the edit is BATCHED, not sent yet. Collapse the
        # panel and confirm so the user can queue edits on other rows before
        # applying them all at once via the dialog's Approve/Edit button. The
        # field value is preserved so reopening this row shows what's queued.
        open_state["v"] = False
        panel.visible = False
        edit_field.border_color = DIVIDER
        edit_caption.value = (
            f"✓ Edit queued for {row_id}. Reopen to change it; "
            "click Approve below to apply all queued edits."
        )
        edit_caption.color = SECONDARY
        with contextlib.suppress(Exception):
            panel.update()

    def _do_cancel(_e: ft.ControlEvent) -> None:
        open_state["v"] = False
        panel.visible = False
        edit_field.value = ""
        edit_field.border_color = DIVIDER
        edit_caption.value = caption_default
        edit_caption.color = ON_SURFACE_DIM
        with contextlib.suppress(Exception):
            panel.update()

    panel = ft.Container(
        content=ft.Column(
            controls=[
                edit_caption,
                edit_field,
                ft.Row(
                    controls=[
                        ft.TextButton("Cancel", on_click=_do_cancel),
                        ft.ElevatedButton(
                            "Queue edit",
                            icon=ft.Icons.ADD,
                            bgcolor=SECONDARY,
                            color="#FFFFFF",
                            on_click=_do_submit,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.END,
                    spacing=8,
                ),
            ],
            spacing=6,
            tight=True,
        ),
        visible=False,
        padding=ft.Padding.only(left=46, right=14, top=8, bottom=10),
        bgcolor=BACKGROUND,
    )

    def toggle() -> None:
        open_state["v"] = not open_state["v"]
        panel.visible = open_state["v"]
        with contextlib.suppress(Exception):
            panel.update()

    return panel, toggle


def _make_tc_accordion_row(
    tc: dict,
    on_edit_submit: PerRowEditCallback | None = None,
) -> ft.Control:
    """One test case row: a clickable header bar that expands inline to
    reveal preconditions / steps / expected result.

    Single-column layout — no master/detail split, no horizontal scroll,
    no width-clipping of the title. The body is built lazily on first
    expand so opening the dialog with N test cases stays cheap.

    When *on_edit_submit* is provided, an edit icon is added to the header
    bar; clicking it expands an inline panel where the user can type
    instructions scoped to this TC.
    """
    tc_id = tc.get("id") or "?"
    title = tc.get("title") or ""
    pri = tc.get("priority") or "?"
    atype = tc.get("automation_type") or tc.get("type") or "?"
    acs = ", ".join(tc.get("ac_ids") or []) or "—"

    expanded = {"v": False}

    body_container = ft.Container(
        content=None,
        visible=False,
        padding=ft.Padding.only(left=46, right=14, top=2, bottom=12),
        bgcolor=BACKGROUND,
    )
    chevron = ft.Icon(
        ft.Icons.CHEVRON_RIGHT, color=ON_SURFACE_DIM, size=sz(20),
    )

    def _toggle(_e: ft.ControlEvent) -> None:
        expanded["v"] = not expanded["v"]
        if expanded["v"] and body_container.content is None:
            body_container.content = _tc_details_body(tc)
        body_container.visible = expanded["v"]
        chevron.name = (
            ft.Icons.KEYBOARD_ARROW_DOWN if expanded["v"]
            else ft.Icons.CHEVRON_RIGHT
        )
        try:
            body_container.update()
            chevron.update()
        except Exception:
            pass

    # Optional per-row edit panel + icon button. The IconButton stops click
    # propagation on its own (Flet wraps the inkwell), so the header_bar's
    # ``on_click=_toggle`` body-expand handler doesn't fire when the icon
    # is tapped — chevron and edit icon act independently.
    edit_panel: ft.Container | None = None
    edit_icon: ft.Control = ft.Container(width=0)  # placeholder, no width
    if on_edit_submit is not None:
        edit_panel, _toggle_edit_panel = _make_per_row_edit_panel(
            tc_id, title, on_edit_submit,
        )
        edit_icon = ft.IconButton(
            icon=ft.Icons.EDIT,
            icon_size=sz(14),
            tooltip=f"Edit {tc_id}",
            on_click=lambda _e: _toggle_edit_panel(),
        )

    header_bar = ft.Container(
        content=ft.Row(
            controls=[
                chevron,
                edit_icon,
                _badge(pri, _PRI_BADGE_COLORS.get(pri, ON_SURFACE_DIM)),
                ft.Container(
                    content=ft.Text(
                        tc_id,
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=PRIMARY,
                        font_family="Courier New",
                        no_wrap=True,
                    ),
                    width=140,
                ),
                ft.Container(
                    content=ft.Text(
                        title,
                        size=sz(13),
                        color=ON_SURFACE,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Text(
                        atype,
                        size=sz(10),
                        color=ON_SURFACE_DIM,
                        text_align=ft.TextAlign.RIGHT,
                        no_wrap=True,
                    ),
                    width=90,
                ),
                ft.Container(
                    content=ft.Text(
                        acs,
                        size=sz(10),
                        color=ON_SURFACE_DIM,
                        text_align=ft.TextAlign.RIGHT,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    width=110,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        on_click=_toggle,
        ink=True,
        bgcolor=CARD_BG,
    )

    children: list[ft.Control] = [header_bar]
    if edit_panel is not None:
        children.append(edit_panel)
    children.extend([
        body_container,
        ft.Divider(height=1, color=DIVIDER, thickness=0.5),
    ])

    return ft.Column(
        controls=children,
        spacing=0,
        tight=True,
    )


def _render_strategy_table(
    strategy: dict,
    on_edit_submit: PerRowEditCallback | None = None,
) -> ft.Control:
    """Render a Step 4 test design as an inline-expandable accordion.

    One row per test case showing ID + priority + title + type + ACs. Click
    a row to reveal its preconditions, steps, and expected result inline.
    Single-column — fixes the master/detail width-collapse glitch where
    the right-hand panel was getting pushed off-screen and only TC ID /
    Title were visible.

    When *on_edit_submit* is provided, each TC row carries an edit icon
    that opens an inline panel for instructions scoped to that TC.
    """
    test_cases = list(strategy.get("test_cases") or [])
    strat_title = strategy.get("title") or "Test Design"

    pri_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for tc in test_cases:
        p = tc.get("priority") or "?"
        t = tc.get("automation_type") or tc.get("type") or "?"
        pri_counts[p] = pri_counts.get(p, 0) + 1
        type_counts[t] = type_counts.get(t, 0) + 1

    summary_chips: list[ft.Control] = [
        ft.Text(
            f"{len(test_cases)} test cases",
            size=sz(12),
            weight=ft.FontWeight.W_500,
            color=ON_SURFACE,
        ),
    ]
    for pri in sorted(pri_counts):
        summary_chips.append(
            _badge(
                f"{pri} · {pri_counts[pri]}",
                _PRI_BADGE_COLORS.get(pri, ON_SURFACE_DIM),
            )
        )
    for t in sorted(type_counts):
        summary_chips.append(
            ft.Container(
                content=ft.Text(
                    f"{t} · {type_counts[t]}",
                    size=sz(10),
                    color=ON_SURFACE,
                ),
                bgcolor=CARD_BG,
                border=ft.Border.all(1, DIVIDER),
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            )
        )

    # Column-header strip (mirrors the accordion row layout for alignment).
    col_header = ft.Container(
        content=ft.Row(
            controls=[
                ft.Container(width=20),  # chevron spacer
                ft.Container(width=28),  # priority badge spacer
                ft.Container(
                    content=ft.Text(
                        "TC ID",
                        size=sz(10),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE_DIM,
                    ),
                    width=140,
                ),
                ft.Container(
                    content=ft.Text(
                        "TITLE",
                        size=sz(10),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE_DIM,
                    ),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Text(
                        "TYPE",
                        size=sz(10),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE_DIM,
                        text_align=ft.TextAlign.RIGHT,
                    ),
                    width=90,
                ),
                ft.Container(
                    content=ft.Text(
                        "ACs",
                        size=sz(10),
                        weight=ft.FontWeight.BOLD,
                        color=ON_SURFACE_DIM,
                        text_align=ft.TextAlign.RIGHT,
                    ),
                    width=110,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        bgcolor=BACKGROUND,
    )

    accordion_rows: list[ft.Control] = [
        _make_tc_accordion_row(tc, on_edit_submit=on_edit_submit)
        for tc in test_cases
    ]

    accordion = ft.Container(
        content=ft.Column(
            controls=[col_header, *accordion_rows],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            tight=True,
        ),
        border=ft.Border.all(1, DIVIDER),
        border_radius=6,
        expand=True,
    )

    header = ft.Column(
        controls=[
            ft.Text(
                strat_title,
                size=sz(14),
                weight=ft.FontWeight.W_600,
                color=ON_SURFACE,
            ),
            ft.Row(controls=summary_chips, spacing=6, wrap=True),
            ft.Text(
                "Click any test case to expand its preconditions, steps, "
                "and expected result.",
                size=sz(11),
                color=ON_SURFACE_DIM,
                italic=True,
            ),
        ],
        spacing=4,
        tight=True,
    )

    return ft.Column(
        controls=[header, ft.Container(height=6), accordion],
        spacing=4,
        tight=False,
        expand=True,
    )


def _plan_section_header(title: str, count: int | None = None) -> ft.Control:
    label = title.upper() if count is None else f"{title.upper()} ({count})"
    return ft.Text(
        label, size=sz(10), weight=ft.FontWeight.BOLD, color=ON_SURFACE_DIM,
    )


def _plan_inline_chip(text: str) -> ft.Container:
    """Small outlined chip — used for markers like @qtea_smoke and key/value
    metadata (scope, yields) under a fixture / function bullet."""
    return ft.Container(
        content=ft.Text(text, size=sz(10), color=ON_SURFACE),
        bgcolor=CARD_BG,
        border=ft.Border.all(1, DIVIDER),
        border_radius=4,
        padding=ft.Padding.symmetric(horizontal=5, vertical=1),
    )


def _plan_meta_line(text: str) -> ft.Text:
    return ft.Text(
        text, size=sz(11), color=ON_SURFACE_DIM,
        selectable=True, font_family="Courier New",
    )


_TRIVIAL_RETURN_RE = re.compile(
    r"\s*(?::\s*Promise\s*<\s*void\s*>|->\s*(?:None|void))\s*$",
    re.IGNORECASE,
)


def _strip_trivial_return_type(sig: str) -> str:
    """Drop `: Promise<void>` / `-> None` / `-> void` tails from a method
    signature — they're noise in the plan view. Meaningful return types
    (e.g. `: Locator`, `-> ElementHandle`) are preserved."""
    return _TRIVIAL_RETURN_RE.sub("", sig)


def _render_plan_test_functions(fns: list[dict]) -> ft.Control:
    if not fns:
        return ft.Text(
            "test functions: —", size=sz(11), color=ON_SURFACE_DIM, italic=True,
        )
    rows: list[ft.Control] = [_plan_section_header("Test functions", len(fns))]
    for fn in fns:
        name = fn.get("name") or "?"
        markers = list(fn.get("markers") or [])
        uses = list(fn.get("uses_fixtures") or [])
        head_children: list[ft.Control] = [
            ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
            ft.Text(
                name, size=sz(12), color=ON_SURFACE, selectable=True,
                font_family="Courier New",
            ),
        ]
        for m in markers:
            head_children.append(_plan_inline_chip(m))
        rows.append(ft.Row(
            controls=head_children, spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ))
        if uses:
            rows.append(ft.Container(
                content=_plan_meta_line(f"fixtures: {', '.join(uses)}"),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _source_badge(src: str) -> ft.Container:
    label = "TBD" if src in ("create_tbd", "tbd") else src
    color = _SOURCE_BADGE_COLORS.get(src, ON_SURFACE_DIM)
    return _badge(label, color)


def _render_plan_fixtures(fixtures: list[dict]) -> ft.Control:
    if not fixtures:
        return ft.Text(
            "fixtures: —", size=sz(11), color=ON_SURFACE_DIM, italic=True,
        )
    rows: list[ft.Control] = [
        _plan_section_header("Fixtures", len(fixtures)),
        ft.Text(
            "Playwright fixtures — thin DI wrappers that instantiate & inject "
            "the Page Objects below into each test.",
            size=sz(10), color=ON_SURFACE_DIM, italic=True, selectable=True,
        ),
    ]
    for f in fixtures:
        src = f.get("source", "?")
        name = f.get("name") or "?"
        ref = f.get("from") or f.get("at") or "?"
        arrow = "←" if src == "reuse" else "→"
        head = ft.Row(
            controls=[
                ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
                _source_badge(src),
                ft.Text(
                    name, size=sz(12), color=ON_SURFACE, selectable=True,
                    font_family="Courier New",
                ),
                ft.Text(f"{arrow} {ref}", size=sz(11), color=ON_SURFACE_DIM,
                        selectable=True),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        )
        rows.append(head)
        meta_parts: list[str] = []
        if f.get("yields"):
            meta_parts.append(f"yields: {f['yields']}")
        if f.get("scope"):
            meta_parts.append(f"scope: {f['scope']}")
        if f.get("depends_on"):
            meta_parts.append(f"depends_on: {', '.join(f['depends_on'])}")
        if meta_parts:
            rows.append(ft.Container(
                content=_plan_meta_line(" · ".join(meta_parts)),
                padding=ft.Padding.only(left=20),
            ))
        if f.get("reuse_justification"):
            rows.append(ft.Container(
                content=ft.Text(
                    f"why reuse: {f['reuse_justification']}",
                    size=sz(11), color=ON_SURFACE_DIM, italic=True,
                    selectable=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _render_plan_page_objects(poms: list[dict]) -> ft.Control:
    if not poms:
        return ft.Text(
            "page objects: —", size=sz(11), color=ON_SURFACE_DIM, italic=True,
        )
    rows: list[ft.Control] = [_plan_section_header("Page objects", len(poms))]
    for p in poms:
        src = p.get("source", "?")
        name = p.get("name") or "?"
        ref = p.get("from") or p.get("at") or "?"
        arrow = "←" if src == "reuse" else "→"
        mm = list(p.get("missing_methods") or [])
        head_children: list[ft.Control] = [
            ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
            _source_badge(src),
            ft.Text(
                name, size=sz(12), color=ON_SURFACE, selectable=True,
                font_family="Courier New",
            ),
            ft.Text(f"{arrow} {ref}", size=sz(11), color=ON_SURFACE_DIM,
                    selectable=True),
        ]
        if mm:
            head_children.append(_plan_inline_chip(f"+{len(mm)} methods"))
        rows.append(ft.Row(
            controls=head_children, spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ))
        for m in mm:
            sig = _strip_trivial_return_type(
                m.get("signature") or m.get("name") or "?",
            )
            rows.append(ft.Container(
                content=ft.Row(
                    controls=[
                        _badge("ADD", _SOURCE_BADGE_COLORS["create"]),
                        _plan_meta_line(sig),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
            if m.get("purpose"):
                rows.append(ft.Container(
                    content=ft.Text(
                        f"  purpose: {m['purpose']}",
                        size=sz(11), color=ON_SURFACE_DIM, italic=True,
                        selectable=True,
                    ),
                    padding=ft.Padding.only(left=20),
                ))
        if p.get("reuse_justification"):
            rows.append(ft.Container(
                content=ft.Text(
                    f"why reuse: {p['reuse_justification']}",
                    size=sz(11), color=ON_SURFACE_DIM, italic=True,
                    selectable=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _deferred_locators_from_units(units: list[dict]) -> list[dict]:
    """Exemplar (non-POM) lane: the TBD locators the SUT needs live inside each
    reusable unit's ``deferred_targets[]`` (name + intent), not in the TC-level
    ``locators[]``. Flatten them into ``locator_entry``-shaped dicts so the
    shared ``_render_plan_locators`` renderer can surface them as create_tbd
    locators (with the owning unit as ``owning_page``)."""
    out: list[dict] = []
    for u in units:
        owner = u.get("name") or "?"
        for dt in u.get("deferred_targets") or []:
            out.append({
                "name": dt.get("name") or "?",
                "owning_page": dt.get("owning_unit") or owner,
                "source": "create_tbd",
                "intent": dt.get("intent") or "?",
            })
    return out


def _render_plan_reusable_units(units: list[dict]) -> ft.Control:
    """Exemplar-lane counterpart to ``_render_plan_page_objects``. Renders the
    SUT's own reusable units (Screenplay Task/Question/Interaction/…) with their
    category, reuse/create placement, and ``missing_behaviors``. Deferred
    locators are surfaced in the shared Locators section, not here."""
    if not units:
        return ft.Text(
            "reusable units: —", size=sz(11), color=ON_SURFACE_DIM, italic=True,
        )
    rows: list[ft.Control] = [_plan_section_header("Reusable units", len(units))]
    for u in units:
        src = u.get("source", "?")
        name = u.get("name") or "?"
        ref = u.get("from") or u.get("at") or "?"
        arrow = "←" if src == "reuse" else "→"
        cat = u.get("category")
        mb = list(u.get("missing_behaviors") or [])
        head_children: list[ft.Control] = [
            ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
            _source_badge(src),
            ft.Text(
                name, size=sz(12), color=ON_SURFACE, selectable=True,
                font_family="Courier New",
            ),
            ft.Text(f"{arrow} {ref}", size=sz(11), color=ON_SURFACE_DIM,
                    selectable=True),
        ]
        if cat:
            head_children.append(_plan_inline_chip(str(cat)))
        if mb:
            head_children.append(_plan_inline_chip(f"+{len(mb)} behaviors"))
        rows.append(ft.Row(
            controls=head_children, spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ))
        for m in mb:
            sig = _strip_trivial_return_type(
                m.get("signature") or m.get("name") or "?",
            )
            rows.append(ft.Container(
                content=ft.Row(
                    controls=[
                        _badge("ADD", _SOURCE_BADGE_COLORS["create"]),
                        _plan_meta_line(sig),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
            if m.get("purpose"):
                rows.append(ft.Container(
                    content=ft.Text(
                        f"  purpose: {m['purpose']}",
                        size=sz(11), color=ON_SURFACE_DIM, italic=True,
                        selectable=True,
                    ),
                    padding=ft.Padding.only(left=20),
                ))
        if u.get("reuse_justification"):
            rows.append(ft.Container(
                content=ft.Text(
                    f"why reuse: {u['reuse_justification']}",
                    size=sz(11), color=ON_SURFACE_DIM, italic=True,
                    selectable=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _render_plan_helpers(helpers: list[dict]) -> ft.Control:
    if not helpers:
        # Helpers are optional in the plan schema; collapse silently.
        return ft.Container(visible=False)
    rows: list[ft.Control] = [_plan_section_header("Helpers", len(helpers))]
    for h in helpers:
        src = h.get("source", "?")
        name = h.get("name") or "?"
        ref = h.get("from") or h.get("at") or "?"
        arrow = "←" if src == "reuse" else "→"
        rows.append(ft.Row(
            controls=[
                ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
                _source_badge(src),
                ft.Text(
                    name, size=sz(12), color=ON_SURFACE, selectable=True,
                    font_family="Courier New",
                ),
                ft.Text(f"{arrow} {ref}", size=sz(11), color=ON_SURFACE_DIM,
                        selectable=True),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ))
        if h.get("signature"):
            rows.append(ft.Container(
                content=_plan_meta_line(h["signature"]),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _render_plan_locators(locators: list[dict]) -> ft.Control:
    if not locators:
        return ft.Text(
            "locators: —", size=sz(11), color=ON_SURFACE_DIM, italic=True,
        )
    rows: list[ft.Control] = [_plan_section_header("Locators", len(locators))]
    for loc in locators:
        src = loc.get("source", "?")
        name = loc.get("name") or "?"
        owner = loc.get("owning_page") or "?"
        head_children: list[ft.Control] = [
            ft.Text("•", size=sz(12), color=ON_SURFACE_DIM, width=14),
            _source_badge(src),
            ft.Text(
                name, size=sz(12), color=ON_SURFACE, selectable=True,
                font_family="Courier New",
            ),
            ft.Text(
                f"(owning: {owner})", size=sz(11), color=ON_SURFACE_DIM,
            ),
        ]
        if src == "create_tbd":
            intent = loc.get("intent") or "?"
            head_children.append(
                ft.Text(
                    f'intent: "{intent}"', size=sz(11), color="#FFB74D",
                    selectable=True, italic=True,
                ),
            )
        else:
            ref = loc.get("from") or "?"
            head_children.append(
                ft.Text(
                    f"← {ref}", size=sz(11), color=ON_SURFACE_DIM,
                    selectable=True,
                ),
            )
        rows.append(ft.Row(
            controls=head_children, spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        ))
        if loc.get("reuse_justification"):
            rows.append(ft.Container(
                content=ft.Text(
                    f"why reuse: {loc['reuse_justification']}",
                    size=sz(11), color=ON_SURFACE_DIM, italic=True,
                    selectable=True,
                ),
                padding=ft.Padding.only(left=20),
            ))
    return ft.Column(controls=rows, spacing=3, tight=True)


def _plan_tc_details_body(tc: dict) -> ft.Control:
    """All five plan sections for one TC. Built lazily on first expand."""
    fns = list(tc.get("test_functions") or tc.get("tests") or [])
    fixtures = list(tc.get("fixtures") or [])
    poms = list(tc.get("page_objects") or [])
    units = list(tc.get("reusable_units") or [])
    helpers = list(tc.get("helpers") or [])
    locators = list(tc.get("locators") or [])
    # Exemplar (non-POM) lane: render reusable_units in place of page_objects and
    # source the Locators section from each unit's deferred_targets[].
    if units and not locators:
        locators = _deferred_locators_from_units(units)
    children: list[ft.Control] = [
        _render_plan_test_functions(fns),
        ft.Container(height=6),
        _render_plan_fixtures(fixtures),
        ft.Container(height=6),
        _render_plan_reusable_units(units) if units
        else _render_plan_page_objects(poms),
    ]
    if helpers:
        children.extend([
            ft.Container(height=6),
            _render_plan_helpers(helpers),
        ])
    children.extend([
        ft.Container(height=6),
        _render_plan_locators(locators),
    ])
    return ft.Column(controls=children, spacing=4, tight=True)


def _summarise_plan_tc(tc: dict) -> str:
    """Compact one-line counts string for the collapsed accordion header."""
    fns = list(tc.get("test_functions") or tc.get("tests") or [])
    fixtures = list(tc.get("fixtures") or [])
    poms = list(tc.get("page_objects") or [])
    units = list(tc.get("reusable_units") or [])
    helpers = list(tc.get("helpers") or [])
    locators = list(tc.get("locators") or [])
    fix_r = sum(1 for f in fixtures if f.get("source") == "reuse")
    fix_c = sum(1 for f in fixtures if f.get("source") == "create")
    parts = [
        f"fns: {len(fns)}",
        f"fix: {fix_r}r/{fix_c}c",
    ]
    if units and not poms:
        # Exemplar (non-POM) lane: units + deferred TBD locators.
        u_r = sum(1 for u in units if u.get("source") == "reuse")
        u_c = sum(1 for u in units if u.get("source") == "create")
        u_b = sum(len(u.get("missing_behaviors") or []) for u in units)
        tbd = sum(len(u.get("deferred_targets") or []) for u in units)
        parts.append(
            f"units: {u_r}r+{u_b}b" if u_c == 0
            else f"units: {u_r}r/{u_c}c+{u_b}b"
        )
        parts.append(f"loc: {tbd}t")
    else:
        pom_r = sum(1 for p in poms if p.get("source") == "reuse")
        pom_c = sum(1 for p in poms if p.get("source") == "create")
        pom_m = sum(len(p.get("missing_methods") or []) for p in poms)
        loc_r = sum(1 for x in locators if x.get("source") == "reuse")
        loc_t = sum(1 for x in locators if x.get("source") == "create_tbd")
        parts.append(
            f"pom: {pom_r}r+{pom_m}m" if pom_c == 0
            else f"pom: {pom_r}r/{pom_c}c+{pom_m}m"
        )
        parts.append(f"loc: {loc_r}r/{loc_t}t")
    if helpers:
        parts.append(f"helpers: {len(helpers)}")
    return " · ".join(parts)


def _make_plan_accordion_row(
    tc: dict,
    on_edit_submit: PerRowEditCallback | None = None,
) -> ft.Control:
    """One plan TC row: clickable header that expands inline to reveal all
    test_functions / fixtures / page_objects / helpers / locators.

    Mirrors `_make_tc_accordion_row` (Step 4) so the two review dialogs
    share visual language. Per-row Edit icon scopes instructions to this TC.
    """
    tc_id = tc.get("id") or tc.get("tc_id") or "?"
    fn_names = [
        (fn.get("name") or "").strip()
        for fn in (tc.get("test_functions") or [])
        if isinstance(fn, dict) and (fn.get("name") or "").strip()
    ]
    target = (
        ", ".join(fn_names)
        or tc.get("test_file_target")
        or tc.get("target")
        or tc.get("file")
        or "?"
    )

    expanded = {"v": False}

    body_container = ft.Container(
        content=None,
        visible=False,
        padding=ft.Padding.only(left=46, right=14, top=2, bottom=12),
        bgcolor=BACKGROUND,
    )
    chevron = ft.Icon(
        ft.Icons.CHEVRON_RIGHT, color=ON_SURFACE_DIM, size=sz(20),
    )

    def _toggle(_e: ft.ControlEvent) -> None:
        expanded["v"] = not expanded["v"]
        if expanded["v"] and body_container.content is None:
            body_container.content = _plan_tc_details_body(tc)
        body_container.visible = expanded["v"]
        chevron.name = (
            ft.Icons.KEYBOARD_ARROW_DOWN if expanded["v"]
            else ft.Icons.CHEVRON_RIGHT
        )
        try:
            body_container.update()
            chevron.update()
        except Exception:
            pass

    edit_panel: ft.Container | None = None
    edit_icon: ft.Control = ft.Container(width=0)
    if on_edit_submit is not None:
        edit_panel, _toggle_edit_panel = _make_per_row_edit_panel(
            tc_id, str(target) or tc_id, on_edit_submit,
            hint=(
                f"What should change for {tc_id}? "
                "(e.g. 'create fixture X instead of reuse')"
            ),
        )
        edit_icon = ft.IconButton(
            icon=ft.Icons.EDIT,
            icon_size=sz(14),
            tooltip=f"Edit {tc_id}",
            on_click=lambda _e: _toggle_edit_panel(),
        )

    header_bar = ft.Container(
        content=ft.Row(
            controls=[
                chevron,
                edit_icon,
                ft.Container(
                    content=ft.Text(
                        tc_id,
                        size=sz(12),
                        weight=ft.FontWeight.BOLD,
                        color=PRIMARY,
                        font_family="Courier New",
                        no_wrap=True,
                    ),
                    width=140,
                ),
                ft.Container(
                    content=ft.Text(
                        str(target),
                        size=sz(11),
                        color=ON_SURFACE_DIM,
                        selectable=True,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    expand=True,
                ),
                ft.Container(
                    content=ft.Text(
                        _summarise_plan_tc(tc),
                        size=sz(10),
                        color=ON_SURFACE,
                        text_align=ft.TextAlign.RIGHT,
                        no_wrap=True,
                        font_family="Courier New",
                    ),
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        on_click=_toggle,
        ink=True,
        bgcolor=CARD_BG,
    )

    children: list[ft.Control] = [header_bar]
    if edit_panel is not None:
        children.append(edit_panel)
    children.extend([
        body_container,
        ft.Divider(height=1, color=DIVIDER, thickness=0.5),
    ])

    return ft.Column(controls=children, spacing=0, tight=True)


def _render_plan_summary(
    plan: dict,
    on_edit_submit: PerRowEditCallback | None = None,
) -> ft.Control:
    """Render a Step 7 code-modification plan as an inline-expandable
    accordion. Each row expands to show all test functions, fixtures,
    POMs, helpers, and locators belonging to that TC — matching what the
    CLI gate's Rich table shows.

    When *on_edit_submit* is provided, each row carries an edit icon that
    opens an inline panel for instructions scoped to that TC.
    """
    test_cases = list(plan.get("test_cases") or [])

    # Top-level chips: module / language / framework + TC count.
    chips: list[ft.Control] = [
        ft.Text(
            f"{plan.get('active_module', '?')} · "
            f"{plan.get('language') or '?'} · "
            f"{plan.get('framework') or '?'}",
            size=sz(12),
            color=ON_SURFACE_DIM,
        ),
        ft.Text(
            f"{len(test_cases)} test cases",
            size=sz(12),
            weight=ft.FontWeight.W_500,
            color=ON_SURFACE,
        ),
    ]

    # Totals across all TCs — mirrors the CLI `_render_plan` footer.
    totals = {
        "fns": 0,
        "fix_r": 0, "fix_c": 0,
        "pom_r": 0, "pom_c": 0, "pom_m": 0,
        "unit_r": 0, "unit_c": 0, "unit_b": 0,
        "loc_r": 0, "loc_t": 0,
        "helpers": 0,
    }
    for tc in test_cases:
        totals["fns"] += len(tc.get("test_functions") or tc.get("tests") or [])
        for f in tc.get("fixtures") or []:
            if f.get("source") == "reuse":
                totals["fix_r"] += 1
            elif f.get("source") == "create":
                totals["fix_c"] += 1
        for p in tc.get("page_objects") or []:
            if p.get("source") == "reuse":
                totals["pom_r"] += 1
            elif p.get("source") == "create":
                totals["pom_c"] += 1
            totals["pom_m"] += len(p.get("missing_methods") or [])
        # Exemplar (non-POM) lane: reusable units + their deferred TBD locators.
        for u in tc.get("reusable_units") or []:
            if u.get("source") == "reuse":
                totals["unit_r"] += 1
            elif u.get("source") == "create":
                totals["unit_c"] += 1
            totals["unit_b"] += len(u.get("missing_behaviors") or [])
            totals["loc_t"] += len(u.get("deferred_targets") or [])
        for x in tc.get("locators") or []:
            if x.get("source") == "reuse":
                totals["loc_r"] += 1
            elif x.get("source") == "create_tbd":
                totals["loc_t"] += 1
        totals["helpers"] += len(tc.get("helpers") or [])

    has_units = bool(totals["unit_r"] or totals["unit_c"])
    totals_chips: list[ft.Control] = [
        _plan_inline_chip(f"test functions · {totals['fns']}"),
        _plan_inline_chip(
            f"fixtures · {totals['fix_r']} reuse / {totals['fix_c']} create"
        ),
    ]
    if has_units:
        totals_chips.append(_plan_inline_chip(
            f"reusable units · {totals['unit_r']} reuse / "
            f"{totals['unit_c']} create · +{totals['unit_b']} behaviors"
        ))
    else:
        totals_chips.append(_plan_inline_chip(
            f"POMs · {totals['pom_r']} reuse / {totals['pom_c']} create "
            f"· +{totals['pom_m']} methods"
        ))
    totals_chips.append(_plan_inline_chip(
        f"locators · {totals['loc_r']} reuse / {totals['loc_t']} TBD"
    ))
    if totals["helpers"]:
        totals_chips.append(_plan_inline_chip(f"helpers · {totals['helpers']}"))

    accordion_rows: list[ft.Control] = [
        _make_plan_accordion_row(tc, on_edit_submit=on_edit_submit)
        for tc in test_cases
    ]
    accordion = ft.Container(
        content=ft.Column(
            controls=accordion_rows,
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            tight=True,
        ),
        border=ft.Border.all(1, DIVIDER),
        border_radius=6,
        expand=True,
    )

    header = ft.Column(
        controls=[
            ft.Row(controls=chips, spacing=12, wrap=True),
            ft.Row(controls=totals_chips, spacing=6, wrap=True),
            ft.Text(
                "Click any test case to expand its test functions, "
                "fixtures, page objects / reusable units, and locators.",
                size=sz(11),
                color=ON_SURFACE_DIM,
                italic=True,
            ),
        ],
        spacing=4,
        tight=True,
    )

    return ft.Column(
        controls=[header, ft.Container(height=6), accordion],
        spacing=4,
        tight=False,
        expand=True,
    )


_INTENT_SCORE_COLORS: dict[str, str] = {
    "FAIL": "#FF5252",
    "WARN": "#FFB74D",
    "PASS": "#66BB6A",
    "EDITED": "#42A5F5",
}


def _render_intents_list(warnings: list) -> ft.Control:
    """Render Step 8 TBD-intent warnings as a list of score-coded cards.

    Mirrors the CLI Rich table (`review_gate.py:_render_intent_warnings`)
    column-for-column: Score / File:line / Constant / Intent / Why, plus
    the ``(was <X>)`` edited-status suffix when the user has rewritten the
    intent through the gate.
    """
    items: list[ft.Control] = []
    for w in warnings or []:
        # Schema field is ``score`` (PASS / WARN / FAIL / EDITED). The old
        # ``severity`` / ``level`` lookups were stale leftovers that meant
        # every card rendered yellow regardless of FAIL status.
        score = (w.get("score") or "WARN").upper()
        score_color = _INTENT_SCORE_COLORS.get(score, "#FFB74D")
        original_score = w.get("original_score")
        file_ = w.get("file") or ""
        line = w.get("line")
        loc_str = f"{file_}:{line}" if line else file_
        constant = w.get("constant_name") or ""
        intent = w.get("intent") or w.get("message") or ""
        rationale = w.get("rationale") or ""

        header_children: list[ft.Control] = [_badge(score, score_color)]
        if original_score and original_score != score:
            header_children.append(
                ft.Text(
                    f"(was {original_score})",
                    size=sz(10),
                    color=ON_SURFACE_DIM,
                    italic=True,
                )
            )
        if loc_str:
            header_children.append(
                ft.Text(
                    loc_str,
                    size=sz(11),
                    color=ON_SURFACE_DIM,
                    selectable=True,
                    font_family="Courier New",
                )
            )
        if constant:
            header_children.append(
                ft.Text(
                    constant,
                    size=sz(11),
                    weight=ft.FontWeight.BOLD,
                    color=PRIMARY,
                    selectable=True,
                    font_family="Courier New",
                )
            )

        body_children: list[ft.Control] = [
            ft.Text(
                intent or "(no intent)",
                size=sz(12),
                color=ON_SURFACE,
                selectable=True,
            ),
        ]
        if rationale:
            body_children.append(
                ft.Text(
                    f"why: {rationale}",
                    size=sz(11),
                    color=ON_SURFACE_DIM,
                    selectable=True,
                    italic=True,
                )
            )
        code_ctx = w.get("code_context") or ""
        if code_ctx:
            body_children.append(
                ft.Container(
                    content=ft.Text(
                        code_ctx,
                        size=sz(11),
                        font_family="Courier New",
                        color="#D4D4D4",
                        selectable=True,
                    ),
                    bgcolor="#1E1E1E",
                    border_radius=4,
                    padding=8,
                    margin=ft.Margin(top=4, bottom=0, left=0, right=0),
                )
            )

        items.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=header_children,
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            wrap=True,
                        ),
                        *body_children,
                    ],
                    spacing=4,
                ),
                padding=8,
                border=ft.Border.all(1, DIVIDER),
                border_radius=6,
            )
        )

    # Header summary chips: total + per-score counts (so the user can see
    # at a glance how many FAIL vs WARN entries are in the list).
    score_counts: dict[str, int] = {}
    for w in warnings or []:
        s = (w.get("score") or "WARN").upper()
        score_counts[s] = score_counts.get(s, 0) + 1
    chip_row: list[ft.Control] = [
        ft.Text(
            f"{len(warnings or [])} flagged intent(s)",
            size=sz(12),
            weight=ft.FontWeight.W_500,
            color=ON_SURFACE,
        ),
    ]
    for score in ("FAIL", "WARN", "EDITED", "PASS"):
        n = score_counts.get(score, 0)
        if n:
            chip_row.append(_badge(f"{score} · {n}",
                                   _INTENT_SCORE_COLORS.get(score, "#FFB74D")))

    return ft.Column(
        controls=[
            ft.Row(controls=chip_row, spacing=8, wrap=True),
            ft.Container(height=6),
            *items,
        ],
        spacing=6,
        tight=True,
    )


def _build_review_body(
    req: ReviewGateRequest,
    on_edit_submit: PerRowEditCallback | None = None,
) -> ft.Control:
    """Dispatch on req.kind; fall back to monospace text for unknown kinds.

    When *on_edit_submit* is provided, the strategy (Step 4) and plan
    (Step 7) renderers add per-row Edit affordances. Intent reviews
    (Step 8) don't currently support per-row edit — global Edit only.
    """
    kind = (req.kind or "").lower()
    try:
        if kind == "strategy" and isinstance(req.data, dict):
            return _render_strategy_table(req.data, on_edit_submit=on_edit_submit)
        if kind == "plan" and isinstance(req.data, dict):
            return _render_plan_summary(req.data, on_edit_submit=on_edit_submit)
        if kind == "intents" and isinstance(req.data, list):
            return _render_intents_list(req.data)
    except Exception as e:
        return ft.Text(
            f"[render error: {e}]\n\n{req.summary}",
            size=sz(12),
            color=ON_SURFACE_DIM,
            font_family="Courier New",
        )
    return ft.Text(
        req.summary,
        size=sz(12),
        color=ON_SURFACE,
        selectable=True,
        font_family="Courier New",
    )


def show_review_gate_dialog(page: ft.Page, state: AppState) -> None:
    """Show a modal dialog for review gate approval. Idempotent per request
    (see ``show_hitl_dialog`` for why)."""
    req = state.pending_review_gate
    if not req:
        return
    if getattr(req, "_dialog_open", False):
        return

    # Always-visible instruction field. The previous two-click flow
    # (first Edit → make field visible; second Edit → submit) was broken
    # because the ``visible=False → True`` toggle didn't propagate through
    # the modal dialog's render path, leaving the field stuck-hidden so
    # users couldn't actually type. Showing it up front also doubles as a
    # discovery affordance: the user immediately sees that Edit takes
    # natural-language instructions.
    #
    # We deliberately do NOT use the Flet ``label`` property here — the
    # floating Material label was z-ordering on top of the strategy
    # title in the review body, producing a glitchy text-overlap. The
    # field's purpose is signalled by the explicit caption above it
    # instead.
    edit_field = ft.TextField(
        multiline=True,
        min_lines=2,
        max_lines=4,
        border_color=DIVIDER,
        text_size=sz(12),
        hint_text=(
            "Describe what to change in plain English — "
            "leave blank to just Approve."
        ),
    )
    edit_caption = ft.Text(
        "Edit instructions (optional)",
        size=sz(11),
        weight=ft.FontWeight.W_500,
        color=ON_SURFACE_DIM,
    )

    # Per-row edits are BATCHED, not applied one at a time. Each row's
    # "Queue edit" stashes a scoped instruction here (keyed by row id so
    # re-editing a row overwrites its prior entry); they're all combined
    # with the global field into edit_instructions when the user Approves.
    queued_edits: dict[str, str] = {}

    def _refresh_queue_caption() -> None:
        if queued_edits:
            ids = ", ".join(queued_edits)
            edit_caption.value = (
                f"Edit instructions — {len(queued_edits)} queued per-row "
                f"edit(s): {ids}. Add more below, then click Approve to apply."
            )
            edit_caption.color = SECONDARY
        else:
            edit_caption.value = "Edit instructions (optional)"
            edit_caption.color = ON_SURFACE_DIM
        with contextlib.suppress(Exception):
            edit_caption.update()

    def _combined_instructions() -> str:
        # Number each edit and separate with a blank line + rule so a
        # multiline per-row instruction can't blur into the next entry —
        # the agent sees discrete, individually-scoped edits.
        parts = list(queued_edits.values())
        extra = (edit_field.value or "").strip()
        if extra:
            parts.append(extra)
        if len(parts) <= 1:
            return parts[0] if parts else ""
        return "\n\n".join(
            f"{i}. {p}" for i, p in enumerate(parts, start=1)
        )

    def _close_dialog() -> None:
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        # Enqueue the dialog removal BEFORE unblocking the waiter: setting
        # completion_event wakes the pipeline worker, which races ahead to
        # tear down + rebuild the view stack for /results. If that happens
        # before pop_dialog() is dispatched, the Flutter client is left
        # holding a dialog whose ancestor view was cleared -> Dart null-check
        # crash -> reconnect loop. Queue the pop first so it reaches the
        # client ahead of the rebuild.
        page.pop_dialog()
        if req.completion_event:
            req.completion_event.set()

    def on_approve(e: ft.ControlEvent) -> None:
        combined = _combined_instructions()
        if combined:
            req.decision = "edit"
            req.edit_instructions = combined
        else:
            req.decision = "approve"
        _close_dialog()

    def on_per_row_edit_submit(
        row_id: str, row_label: str, instructions: str,
    ) -> None:
        """Queue a per-row edit — does NOT close the dialog.

        The edit is scoped to one TC / plan entry and stashed in
        ``queued_edits``; the user can queue more rows, then apply them all
        at once via Approve/Edit. Combining works because the ``*-editor``
        agents accept multiple scoped instructions in a single prompt — the
        scoped prefix ("Modify only <id>") keeps each edit targeted.
        """
        scoped = (
            f"Modify only {row_id}"
            + (f" ({row_label})" if row_label and row_label != row_id else "")
            + f": {instructions}"
        )
        queued_edits[row_id] = scoped
        _refresh_queue_caption()

    def on_edit(e: ft.ControlEvent) -> None:
        combined = _combined_instructions()
        if not combined:
            # Nothing typed and nothing queued — make the field visibly
            # demand input so the user sees what Edit actually wants. The
            # caption above the field doubles as the prompt; tint it +
            # the border orange.
            edit_field.border_color = "#FFB74D"
            edit_caption.value = (
                "Type edit instructions below (or Queue a per-row edit), "
                "then click Edit again."
            )
            edit_caption.color = "#FFB74D"
            try:
                edit_field.update()
                edit_caption.update()
            except Exception:
                pass
            return
        req.decision = "edit"
        req.edit_instructions = combined
        _close_dialog()

    def on_reject(e: ft.ControlEvent) -> None:
        req.decision = "reject"
        _close_dialog()

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Step {req.step} — {req.title}",
            size=sz(16),
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Container(
            content=ft.Column(
                controls=[
                    # Review body fills remaining vertical space.
                    ft.Container(
                        content=_build_review_body(
                            req, on_edit_submit=on_per_row_edit_submit,
                        ),
                        expand=True,
                    ),
                    # Clear divider between body and edit zone — prevents
                    # the previous title/label overlap glitch.
                    ft.Divider(height=1, color=DIVIDER, thickness=0.5),
                    # Dedicated edit zone, visually distinct from the body.
                    ft.Container(
                        content=ft.Column(
                            controls=[edit_caption, edit_field],
                            spacing=4,
                            tight=True,
                        ),
                        padding=ft.Padding.symmetric(horizontal=4, vertical=4),
                    ),
                ],
                spacing=8,
                tight=False,
            ),
            width=980,
            height=620,
        ),
        actions=[
            ft.TextButton(
                "Reject",
                style=ft.ButtonStyle(color="#FF5252"),
                on_click=on_reject,
            ),
            ft.OutlinedButton("Edit", icon=ft.Icons.EDIT, on_click=on_edit),
            ft.ElevatedButton(
                "Approve",
                icon=ft.Icons.CHECK,
                bgcolor=SECONDARY,
                color="#FFFFFF",
                on_click=on_approve,
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    try:
        req._dialog_open = True  # type: ignore[attr-defined]
    except Exception:
        pass
    page.show_dialog(dlg)
