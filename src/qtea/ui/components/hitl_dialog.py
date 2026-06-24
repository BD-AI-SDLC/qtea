"""Modal dialogs for HITL questions and review gates."""

from __future__ import annotations

import flet as ft

from qtea.ui.state import AppState, HitlRequest, ReviewGateRequest
from qtea.ui.theme import CARD_BG, DIVIDER, ON_SURFACE, ON_SURFACE_DIM, PRIMARY, SECONDARY


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

    answer_fields: dict[str, ft.TextField] = {}

    # Build question widgets
    question_controls: list[ft.Control] = []
    for q in req.questions:
        q_id = q.get("id", "")
        q_text = q.get("text", q.get("question", ""))
        # Rationale / severity / AC references are intentionally NOT shown
        # under the question — the block id is already in the red chip and
        # that's enough to correlate. The full row stays in the
        # ``user-answers.md`` ledger if anyone needs to trace it later.
        q_context = ""
        q_type = q.get("type", "blocker")

        type_color = "#FF5252" if q_type == "blocker" else "#FFB74D"
        type_label = q_type.upper()

        field = ft.TextField(
            multiline=True,
            min_lines=4,
            max_lines=20,
            border_color=DIVIDER,
            text_size=13,
            hint_text="Type your answer...",
            expand=True,
        )
        answer_fields[q_id] = field

        question_controls.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Container(
                                    content=ft.Text(
                                        type_label,
                                        size=10,
                                        weight=ft.FontWeight.BOLD,
                                        color="#FFFFFF",
                                    ),
                                    bgcolor=type_color,
                                    border_radius=4,
                                    padding=ft.Padding.symmetric(
                                        horizontal=6, vertical=2
                                    ),
                                ),
                                ft.Text(q_id, size=11, color=ON_SURFACE_DIM),
                            ],
                            spacing=8,
                        ),
                        ft.Text(
                            q_text,
                            size=13,
                            color=ON_SURFACE,
                            weight=ft.FontWeight.W_500,
                        ),
                        *(
                            [
                                ft.Text(
                                    q_context,
                                    size=11,
                                    color=ON_SURFACE_DIM,
                                    italic=True,
                                )
                            ]
                            if q_context
                            else []
                        ),
                        field,
                    ],
                    spacing=6,
                ),
                padding=12,
                border=ft.Border.all(1, DIVIDER),
                border_radius=8,
            )
        )

    def on_submit(e: ft.ControlEvent) -> None:
        for q_id, field in answer_fields.items():
            if field.value:
                req.answers[q_id] = ("user", field.value)
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        if req.completion_event:
            req.completion_event.set()
        page.pop_dialog()

    def on_skip(e: ft.ControlEvent) -> None:
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        if req.completion_event:
            req.completion_event.set()
        page.pop_dialog()

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Step {req.step} — Input Required",
            size=16,
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        f"Agent '{req.agent_label}' needs your input on "
                        f"{len(req.questions)} item(s).",
                        size=13,
                        color=ON_SURFACE_DIM,
                    ),
                    ft.Container(height=8),
                    *question_controls,
                ],
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            width=600,
            height=min(500, 120 + len(req.questions) * 160),
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


def _badge(text: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=10, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
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

    section: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        # Section headers
        if "**preconditions:**" in low:
            section = "preconditions"
            continue
        if "**steps:**" in low:
            section = "steps"
            continue
        if "**expected result:**" in low or "**expected:**" in low:
            section = "expected"
            continue
        if stripped.startswith("- **") and stripped.endswith(":**"):
            # Some other header (Type, Priority, etc.) — leave the section
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


def _tc_details_panel(tc: dict) -> ft.Control:
    """Build the right-hand details panel for one test case."""
    if not tc:
        return ft.Container(
            content=ft.Text(
                "Select a test case from the table to see its steps,\n"
                "preconditions, and expected result here.",
                size=12,
                color=ON_SURFACE_DIM,
                text_align=ft.TextAlign.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

    parsed_raw = _parse_raw_tc_markdown(tc.get("raw") or "")
    preconditions = list(tc.get("preconditions") or []) or list(
        parsed_raw.get("preconditions") or []  # type: ignore[arg-type]
    )
    steps = list(tc.get("steps") or []) or list(
        parsed_raw.get("steps") or []  # type: ignore[arg-type]
    )
    expected = tc.get("expected") or parsed_raw.get("expected") or ""

    pri = tc.get("priority") or "?"
    atype = tc.get("automation_type") or tc.get("type") or "?"
    acs = ", ".join(tc.get("ac_ids") or []) or "—"

    parts: list[ft.Control] = [
        ft.Row(
            controls=[
                _badge(pri, _PRI_BADGE_COLORS.get(pri, ON_SURFACE_DIM)),
                ft.Text(
                    tc.get("id") or "?",
                    size=13,
                    weight=ft.FontWeight.BOLD,
                    color=PRIMARY,
                    font_family="Courier New",
                    selectable=True,
                ),
            ],
            spacing=8,
        ),
        ft.Text(
            tc.get("title") or "",
            size=13,
            color=ON_SURFACE,
            weight=ft.FontWeight.W_500,
            selectable=True,
        ),
        ft.Row(
            controls=[
                ft.Text(f"type: {atype}", size=11, color=ON_SURFACE_DIM),
                ft.Text(f"acs: {acs}", size=11, color=ON_SURFACE_DIM),
            ],
            spacing=12,
            wrap=True,
        ),
        ft.Divider(height=12, color=DIVIDER),
    ]

    def _section(title: str, items: list[str], numbered: bool = False) -> ft.Control:
        if not items:
            return ft.Container(
                content=ft.Text(
                    f"{title}: —",
                    size=11,
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
                            size=12,
                            color=ON_SURFACE_DIM,
                            width=22,
                        ),
                        ft.Text(
                            it,
                            size=12,
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
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ON_SURFACE_DIM,
                ),
                *bullets,
            ],
            spacing=3,
            tight=True,
        )

    parts.append(_section("Preconditions", preconditions))
    parts.append(ft.Container(height=8))
    parts.append(_section("Steps", steps, numbered=True))
    parts.append(ft.Container(height=8))
    parts.append(
        _section(
            "Expected Result",
            [expected] if expected else [],
        )
    )

    return ft.Column(
        controls=parts,
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        tight=True,
        expand=True,
    )


def _render_strategy_table(strategy: dict) -> ft.Control:
    """Render a Step 4 test-strategy with master/detail.

    Left: condensed DataTable of all test cases (TC ID, Title, Pri, Type, ACs).
    Right: a details panel that shows the SELECTED TC's preconditions, steps,
    and expected result. Each row is click-selectable.

    The "Steps" count column is dropped — the inline number was always 0
    when the planner emits markdown-only content, and the steps live in
    the right-hand panel now anyway.
    """
    test_cases = list(strategy.get("test_cases") or [])
    strat_title = strategy.get("title") or "Test Strategy"

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
            size=12,
            color=ON_SURFACE_DIM,
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
                    size=10,
                    color=ON_SURFACE,
                ),
                bgcolor=CARD_BG,
                border=ft.Border.all(1, DIVIDER),
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
            )
        )

    # Details panel: mutable Container whose .content is swapped on row click.
    details_box = ft.Container(
        content=_tc_details_panel(test_cases[0] if test_cases else {}),
        padding=12,
        bgcolor=CARD_BG,
        border_radius=8,
        border=ft.Border.all(1, DIVIDER),
        width=360,
        expand=True,
    )

    selected_id: dict[str, str | None] = {
        "id": (test_cases[0].get("id") if test_cases else None),
    }

    def _on_row_select(tc_id: str) -> None:
        tc = next(
            (t for t in test_cases if t.get("id") == tc_id),
            None,
        )
        if tc is None:
            return
        selected_id["id"] = tc_id
        details_box.content = _tc_details_panel(tc)
        try:
            details_box.update()
        except Exception:
            pass

    rows: list[ft.DataRow] = []
    for tc in test_cases:
        tc_id = tc.get("id") or "?"
        title = tc.get("title") or ""
        pri = tc.get("priority") or "?"
        atype = tc.get("automation_type") or tc.get("type") or "?"
        acs = ", ".join(tc.get("ac_ids") or []) or "—"
        rows.append(
            ft.DataRow(
                # Flet 0.85 spells it `on_select_change` (singular).
                on_select_change=(
                    lambda e, _id=tc_id: _on_row_select(_id)
                ),
                cells=[
                    ft.DataCell(
                        ft.Text(
                            tc_id,
                            size=12,
                            weight=ft.FontWeight.BOLD,
                            color=PRIMARY,
                            font_family="Courier New",
                            selectable=True,
                        ),
                        on_tap=lambda e, _id=tc_id: _on_row_select(_id),
                    ),
                    ft.DataCell(
                        ft.Text(
                            title,
                            size=12,
                            color=ON_SURFACE,
                            selectable=True,
                        ),
                        on_tap=lambda e, _id=tc_id: _on_row_select(_id),
                    ),
                    ft.DataCell(
                        _badge(
                            pri,
                            _PRI_BADGE_COLORS.get(pri, ON_SURFACE_DIM),
                        ),
                        on_tap=lambda e, _id=tc_id: _on_row_select(_id),
                    ),
                    ft.DataCell(
                        ft.Text(atype, size=12, color=ON_SURFACE),
                        on_tap=lambda e, _id=tc_id: _on_row_select(_id),
                    ),
                    ft.DataCell(
                        ft.Text(
                            acs,
                            size=11,
                            color=ON_SURFACE_DIM,
                            selectable=True,
                        ),
                        on_tap=lambda e, _id=tc_id: _on_row_select(_id),
                    ),
                ],
            )
        )

    table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("TC ID", weight=ft.FontWeight.BOLD, size=11)),
            ft.DataColumn(ft.Text("Title", weight=ft.FontWeight.BOLD, size=11)),
            ft.DataColumn(ft.Text("Pri", weight=ft.FontWeight.BOLD, size=11)),
            ft.DataColumn(ft.Text("Type", weight=ft.FontWeight.BOLD, size=11)),
            ft.DataColumn(ft.Text("ACs", weight=ft.FontWeight.BOLD, size=11)),
        ],
        rows=rows,
        column_spacing=18,
        heading_row_color=CARD_BG,
        divider_thickness=0.5,
        horizontal_lines=ft.BorderSide(0.5, DIVIDER),
        show_bottom_border=True,
    )

    # Header
    header = ft.Column(
        controls=[
            ft.Text(
                strat_title,
                size=14,
                weight=ft.FontWeight.W_600,
                color=ON_SURFACE,
            ),
            ft.Row(controls=summary_chips, spacing=6, wrap=True),
            ft.Container(height=4),
            ft.Text(
                "Click a row to see preconditions, steps, "
                "and expected result.",
                size=11,
                color=ON_SURFACE_DIM,
                italic=True,
            ),
        ],
        spacing=4,
        tight=True,
    )

    # Master/detail split row
    body_row = ft.Row(
        controls=[
            # Table scrolls vertically inside a sized Container so the
            # right-hand details panel stays put as the user scrolls.
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[table],
                            scroll=ft.ScrollMode.AUTO,
                            tight=True,
                        ),
                    ],
                    scroll=ft.ScrollMode.AUTO,
                    tight=True,
                ),
                expand=True,
            ),
            details_box,
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        expand=True,
    )

    return ft.Column(
        controls=[header, ft.Container(height=6), body_row],
        spacing=4,
        tight=True,
        expand=True,
    )


def _render_plan_summary(plan: dict) -> ft.Control:
    """Render a Step 7 code-modification plan as a list of TC cards."""
    test_cases = list(plan.get("test_cases") or [])
    chips: list[ft.Control] = [
        ft.Text(
            f"{plan.get('active_module', '?')} · "
            f"{plan.get('language', '?')} · "
            f"{plan.get('framework', '?')}",
            size=12,
            color=ON_SURFACE_DIM,
        ),
        ft.Text(
            f"{len(test_cases)} test cases",
            size=12,
            color=ON_SURFACE_DIM,
        ),
    ]

    cards: list[ft.Control] = []
    for tc in test_cases:
        tc_id = tc.get("id") or tc.get("tc_id") or "?"
        target = tc.get("target") or tc.get("file") or ""
        tests = tc.get("tests") or tc.get("test_functions") or []
        loc_count = len(tc.get("locators") or [])
        pom_count = len(tc.get("page_objects") or [])
        cards.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Text(
                                    tc_id,
                                    size=12,
                                    weight=ft.FontWeight.BOLD,
                                    color=PRIMARY,
                                    font_family="Courier New",
                                ),
                                ft.Text(
                                    str(target),
                                    size=11,
                                    color=ON_SURFACE_DIM,
                                    selectable=True,
                                ),
                            ],
                            spacing=10,
                        ),
                        ft.Text(
                            f"tests: {len(tests)} · locators: {loc_count} · poms: {pom_count}",
                            size=11,
                            color=ON_SURFACE_DIM,
                        ),
                    ],
                    spacing=2,
                ),
                padding=8,
                border=ft.Border.all(1, DIVIDER),
                border_radius=6,
            )
        )

    return ft.Column(
        controls=[
            ft.Row(controls=chips, spacing=12, wrap=True),
            ft.Container(height=6),
            *cards,
        ],
        spacing=6,
        tight=True,
    )


def _render_intents_list(warnings: list) -> ft.Control:
    """Render Step 8 TBD-intent warnings as a list of severity-coded cards."""
    items: list[ft.Control] = []
    for w in warnings or []:
        sev = (w.get("severity") or w.get("level") or "warn").upper()
        sev_color = "#FF5252" if sev == "FAIL" else "#FFB74D"
        file = w.get("file") or ""
        line = w.get("line")
        loc_str = f"{file}:{line}" if line else file
        intent = w.get("intent") or w.get("message") or ""
        items.append(
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                _badge(sev, sev_color),
                                ft.Text(
                                    loc_str,
                                    size=11,
                                    color=ON_SURFACE_DIM,
                                    selectable=True,
                                    font_family="Courier New",
                                ),
                            ],
                            spacing=8,
                        ),
                        ft.Text(
                            intent,
                            size=12,
                            color=ON_SURFACE,
                            selectable=True,
                        ),
                    ],
                    spacing=4,
                ),
                padding=8,
                border=ft.Border.all(1, DIVIDER),
                border_radius=6,
            )
        )
    return ft.Column(
        controls=[
            ft.Text(
                f"{len(warnings or [])} flagged intent(s)",
                size=12,
                color=ON_SURFACE_DIM,
            ),
            ft.Container(height=6),
            *items,
        ],
        spacing=6,
        tight=True,
    )


def _build_review_body(req: ReviewGateRequest) -> ft.Control:
    """Dispatch on req.kind; fall back to monospace text for unknown kinds."""
    kind = (req.kind or "").lower()
    try:
        if kind == "strategy" and isinstance(req.data, dict):
            return _render_strategy_table(req.data)
        if kind == "plan" and isinstance(req.data, dict):
            return _render_plan_summary(req.data)
        if kind == "intents" and isinstance(req.data, list):
            return _render_intents_list(req.data)
    except Exception as e:  # noqa: BLE001
        return ft.Text(
            f"[render error: {e}]\n\n{req.summary}",
            size=12,
            color=ON_SURFACE_DIM,
            font_family="Courier New",
        )
    return ft.Text(
        req.summary,
        size=12,
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
    edit_field = ft.TextField(
        multiline=True,
        min_lines=2,
        max_lines=6,
        border_color=DIVIDER,
        text_size=12,
        label="Optional edit instructions",
        hint_text=(
            "Describe what to change in plain English — "
            "leave blank to just Approve."
        ),
    )

    def _close_dialog() -> None:
        try:
            req._dialog_open = False  # type: ignore[attr-defined]
        except Exception:
            pass
        if req.completion_event:
            req.completion_event.set()
        page.pop_dialog()

    def on_approve(e: ft.ControlEvent) -> None:
        req.decision = "approve"
        _close_dialog()

    def on_edit(e: ft.ControlEvent) -> None:
        text = (edit_field.value or "").strip()
        if not text:
            # No instructions provided — surface a tooltip-like hint via
            # the label so the user knows what to do, then bail.
            edit_field.label = "Type your edit instructions first"
            edit_field.border_color = "#FFB74D"
            try:
                edit_field.update()
            except Exception:
                pass
            return
        req.decision = "edit"
        req.edit_instructions = text
        _close_dialog()

    def on_reject(e: ft.ControlEvent) -> None:
        req.decision = "reject"
        _close_dialog()

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(
            f"Step {req.step} — {req.title}",
            size=16,
            weight=ft.FontWeight.BOLD,
        ),
        content=ft.Container(
            content=ft.Column(
                controls=[
                    _build_review_body(req),
                    edit_field,
                ],
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            width=900,
            height=540,
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
