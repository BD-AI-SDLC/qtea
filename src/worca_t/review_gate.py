"""Post-step-7 human review gate.

Surfaces the test-architect's `code-modification-plan.json` for human review
BEFORE Step 8 (codegen) writes any code. Catches placement mistakes early —
when fixing them costs nothing.

What it renders:
- Per-test-case: test_file_target, function names, fixture decisions
  (reuse vs create), page-object decisions (reuse vs create + count of
  missing methods), locator decisions (reuse vs create_tbd with intent).
- Footer: totals + counts per category (reuse vs create).

Approve / edit / quit flow:
- `a` approve → return True, pipeline proceeds to Step 8.
- `e` edit plan → user types free-text instructions; an LLM applies the
  delta to the plan JSON, re-validates against the schema, and re-renders.
- `q` quit → return False, pipeline aborts with exit code 1.

Auto-approved (no prompt) when stdin is not a TTY or `--no-hitl` is set.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from worca_t.checkpoints import hash_paths
from worca_t.config import package_resource_root
from worca_t.llm.reasoning import call_reasoning_llm
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid, load_schema

if TYPE_CHECKING:
    from worca_t.steps.base import StepContext, StepResult

log = get_logger(__name__)


async def review_step_7_plan(
    ctx: "StepContext",
    result: "StepResult",
    console: Console,
) -> bool:
    """Run the post-step-7 plan review gate. Return True on approve.

    Auto-approves (returns True) when stdin is not a TTY or ``--no-hitl`` is
    set. On the ``edit`` choice the user types free-text instructions and an
    LLM applies the delta; the gate re-validates against the schema and
    re-renders. On schema failure after edit, the user can re-edit or abort.
    """
    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not sys.stdin.isatty():
        log.info("step07.review_gate.skip", reason="non_tty_or_no_hitl")
        return True

    step_dir = ctx.workspace.step_dir(7)
    plan_path = step_dir / "code-modification-plan.json"
    if not plan_path.exists():
        log.warning("step07.review_gate.no_plan", path=str(plan_path))
        return True

    while True:
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            console.print(f"[red]cannot read plan:[/] {e}")
            log.warning("step07.review_gate.plan_unreadable", error=str(e))
            return False

        _render_plan(plan, console)
        choice = Prompt.ask(
            "[bold]Approve and continue to step 8?[/bold] "
            r"[dim](\[a\]pprove / \[e\]dit plan / \[q\]uit)[/]",
            choices=["a", "e", "q"],
            default="a",
            show_choices=False,
        )
        if choice == "a":
            log.info("step07.review_gate.approved")
            return True
        if choice == "q":
            log.info("step07.review_gate.rejected")
            return False

        # --- Free-text edit via LLM ---
        edited_plan = await _apply_nlp_edit(plan, plan_path, ctx, console)
        if edited_plan is None:
            return False
        # Loop back to re-render the updated plan and re-prompt.


async def _apply_nlp_edit(
    plan: dict,
    plan_path: Path,
    ctx: "StepContext",
    console: Console,
) -> dict | None:
    """Prompt for free-text instructions, apply via LLM, persist.

    Returns the updated plan dict on success, or ``None`` if the user quits.
    Writes the updated JSON to *plan_path* and refreshes checkpoint hashes.
    """
    console.print(Panel(
        "Describe the changes you want in plain English.\n"
        "Examples: [dim]\"remove TC-03\"[/], [dim]\"change fixture X to "
        "create instead of reuse\"[/], [dim]\"add a smoke marker to all "
        "test functions\"[/]",
        title="Edit plan",
        border_style="yellow",
    ))
    try:
        instructions = Prompt.ask("[bold]Your instructions[/]")
    except (EOFError, KeyboardInterrupt):
        log.info("step07.review_gate.rejected", reason="interrupt")
        return None

    if not instructions.strip():
        console.print("[dim]empty input — skipping edit[/]")
        return plan

    agent = package_resource_root() / "agents" / "plan-editor.agent.md"
    workdir = ctx.workspace.step_dir(7) / "plan-editor"
    workdir.mkdir(parents=True, exist_ok=True)

    console.print("[dim]applying edits…[/]")
    llm_result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=instructions,
        inputs={"code-modification-plan.json": json.dumps(plan, indent=2)},
        output_schema=load_schema("code-modification-plan"),
        step=7,
    )

    if not llm_result.success or not llm_result.final_text:
        console.print(
            f"[red]LLM edit failed:[/] {llm_result.error or 'no output'}"
        )
        recover = Prompt.ask(
            r"\[r\]etry, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        if recover == "q":
            return None
        return plan  # loop back, plan unchanged

    try:
        new_plan: dict = json.loads(llm_result.final_text)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM returned unparseable JSON:[/] {e}")
        return plan

    ok, err = is_valid(new_plan, "code-modification-plan")
    if not ok:
        console.print(f"[red]edited plan failed schema validation:[/] {err}")
        recover = Prompt.ask(
            r"\[r\]etry, \[a\]pprove anyway (risky), \[q\]uit",
            choices=["r", "a", "q"],
            default="r",
            show_choices=False,
        )
        if recover == "q":
            return None
        if recover == "a":
            _persist_and_refresh_hashes(new_plan, plan_path, ctx)
            return new_plan
        return plan  # loop back with original plan

    _persist_and_refresh_hashes(new_plan, plan_path, ctx)
    return new_plan


def _persist_and_refresh_hashes(
    plan: dict,
    plan_path: Path,
    ctx: "StepContext",
) -> None:
    """Write the updated plan to disk and refresh checkpoint hashes."""
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    record = ctx.state.steps.get(7)
    if record is not None:
        record.output_hashes = hash_paths(
            [p for p in plan_path.parent.iterdir() if p.is_file()]
        )


def _render_plan(plan: dict, console: Console) -> None:
    test_cases = plan.get("test_cases") or []
    active_module = plan.get("active_module") or "?"
    language = plan.get("language") or "?"
    framework = plan.get("framework") or "?"

    table = Table(
        title=(
            f"Step 7 — Code Modification Plan "
            f"({active_module} · {language} · {framework})"
        ),
        show_lines=True,
        expand=True,
    )
    table.add_column("TC", style="bold cyan", no_wrap=True)
    table.add_column("Target", style="dim", overflow="fold")
    table.add_column("Tests", overflow="fold")
    table.add_column("Fixtures", overflow="fold")
    table.add_column("POMs", overflow="fold")
    table.add_column("Locators", overflow="fold")

    totals = {
        "fixtures": {"reuse": 0, "create": 0},
        "page_objects": {"reuse": 0, "create": 0, "missing_methods": 0},
        "locators": {"reuse": 0, "create_tbd": 0},
        "test_functions": 0,
    }
    tbd_intents: list[str] = []

    for tc in test_cases:
        tc_id = tc.get("id") or "<no-id>"
        target = tc.get("test_file_target") or "?"
        fns = tc.get("test_functions") or []
        totals["test_functions"] += len(fns)

        test_cell = "\n".join(
            f"• {fn.get('name')} [{', '.join(fn.get('markers') or []) or 'no-marker'}]"
            for fn in fns
        ) or "—"

        fixtures = tc.get("fixtures") or []
        fix_lines = []
        for f in fixtures:
            src = f.get("source", "?")
            totals["fixtures"][src] = totals["fixtures"].get(src, 0) + 1
            tag = f"[green]reuse[/]" if src == "reuse" else "[yellow]create[/]"
            ref = f.get("from") or f.get("at") or "?"
            fix_lines.append(f"{tag} {f.get('name')} ← {ref}")
        fix_cell = "\n".join(fix_lines) or "—"

        poms = tc.get("page_objects") or []
        pom_lines = []
        for p in poms:
            src = p.get("source", "?")
            totals["page_objects"][src] = totals["page_objects"].get(src, 0) + 1
            mm = p.get("missing_methods") or []
            totals["page_objects"]["missing_methods"] += len(mm)
            tag = f"[green]reuse[/]" if src == "reuse" else "[yellow]create[/]"
            ref = p.get("from") or p.get("at") or "?"
            suffix = f" (+{len(mm)} methods)" if mm else ""
            pom_lines.append(f"{tag} {p.get('name')} ← {ref}{suffix}")
        pom_cell = "\n".join(pom_lines) or "—"

        locators = tc.get("locators") or []
        loc_lines = []
        for loc in locators:
            src = loc.get("source", "?")
            totals["locators"][src] = totals["locators"].get(src, 0) + 1
            if src == "create_tbd":
                intent = loc.get("intent") or "?"
                tbd_intents.append(f"{loc.get('name')}: {intent}")
                loc_lines.append(f"[yellow]TBD[/] {loc.get('name')}: \"{intent}\"")
            else:
                loc_lines.append(
                    f"[green]reuse[/] {loc.get('name')} ← "
                    f"{loc.get('from') or '?'}"
                )
        loc_cell = "\n".join(loc_lines) or "—"

        table.add_row(tc_id, target, test_cell, fix_cell, pom_cell, loc_cell)

    console.print()
    console.print(table)

    footer = [
        f"test cases: [bold]{len(test_cases)}[/]",
        f"test functions: [bold]{totals['test_functions']}[/]",
        f"fixtures: [green]{totals['fixtures']['reuse']} reuse[/] · "
        f"[yellow]{totals['fixtures']['create']} create[/]",
        f"page objects: [green]{totals['page_objects']['reuse']} reuse[/] · "
        f"[yellow]{totals['page_objects']['create']} create[/] · "
        f"+{totals['page_objects']['missing_methods']} missing methods",
        f"locators: [green]{totals['locators']['reuse']} reuse[/] · "
        f"[yellow]{totals['locators']['create_tbd']} TBD[/]",
    ]
    console.print(Panel(
        " · ".join(footer)
        + "\n\n[bold]\\[a\\][/]pprove and continue   "
        "[bold]\\[e\\][/]dit plan   [bold]\\[q\\][/]uit",
        title="Review",
        border_style="cyan",
    ))


# ---------------------------------------------------------------------------
# Step 8 Phase D — TBD intent quality review gate
# ---------------------------------------------------------------------------


async def review_step_8_intents(
    ctx: "StepContext",
    result: "StepResult",
    console: Console,
) -> bool:
    """Surface WARN/FAIL intent entries from Phase D for human review.

    Mirrors `review_step_7_plan` shape. Auto-approves on non-TTY / `--no-hitl`.
    On `[e]dit`, the user types free-text instructions, the `tbd-intent-editor`
    agent rewrites the intent list, and the pipeline updates the SUT sources
    in place via the file:line anchors recorded by `tbd_scanner`.
    """
    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not sys.stdin.isatty():
        log.info("step08.intent_review_gate.skip", reason="non_tty_or_no_hitl")
        return True

    warnings = ctx.extras.get("step8_intent_warnings") or []
    if not warnings:
        return True

    while True:
        _render_intent_warnings(warnings, console)
        choice = Prompt.ask(
            "[bold]Approve and continue to step 9?[/bold] "
            r"[dim](\[a\]pprove / \[e\]dit intents / \[q\]uit)[/]",
            choices=["a", "e", "q"],
            default="a",
            show_choices=False,
        )
        if choice == "a":
            log.info("step08.intent_review_gate.approved",
                     count=len(warnings))
            return True
        if choice == "q":
            log.info("step08.intent_review_gate.rejected")
            return False

        edited = await _apply_intent_edit(warnings, ctx, console)
        if edited is None:
            return False
        warnings = edited
        ctx.extras["step8_intent_warnings"] = warnings


def _render_intent_warnings(
    warnings: list[dict],
    console: Console,
) -> None:
    table = Table(
        title=f"Step 8 — TBD intent quality ({len(warnings)} flagged)",
        show_lines=True,
        expand=True,
    )
    table.add_column("Score", no_wrap=True)
    table.add_column("File:line", style="dim", no_wrap=True)
    table.add_column("Constant", no_wrap=True)
    table.add_column("Intent", overflow="fold")
    table.add_column("Why", overflow="fold")

    for w in warnings:
        score = w.get("score", "?")
        tag = "[red]FAIL[/]" if score == "FAIL" else "[yellow]WARN[/]"
        edited_suffix = (
            f" [dim](was {w['original_score']})[/]"
            if w.get("original_score") and w.get("original_score") != score
            else ""
        )
        table.add_row(
            tag + edited_suffix,
            f"{w.get('file', '?')}:{w.get('line', '?')}",
            w.get("constant_name") or "—",
            w.get("intent", "?"),
            w.get("rationale", "?"),
        )

    console.print()
    console.print(table)
    console.print(Panel(
        "[bold]\\[a\\][/]pprove and continue   "
        "[bold]\\[e\\][/]dit intents (LLM rewrite)   "
        "[bold]\\[q\\][/]uit",
        title="Intent review",
        border_style="cyan",
    ))


async def _apply_intent_edit(
    warnings: list[dict],
    ctx: "StepContext",
    console: Console,
) -> list[dict] | None:
    """Prompt for free-text instructions, rewrite intents via LLM + source patch.

    Returns the updated warnings list on success (with edited intents
    substituted) or ``None`` if the user quits. Rewrites sentinel call-sites
    in the SUT sources in-place using the file:line anchors.
    """
    console.print(Panel(
        "Describe how you want the flagged intents changed in plain English.\n"
        "Examples: [dim]\"make 'submit' specific by referring to the "
        "'Save changes' button in the dialog footer\"[/], "
        "[dim]\"replace any CSS-selector-looking intents with a "
        "role + visible label\"[/], "
        "[dim]\"all the 'OK' buttons are confirmation buttons in their "
        "respective modals — name the modal in the intent\"[/]",
        title="Edit intents",
        border_style="yellow",
    ))
    try:
        instructions = Prompt.ask("[bold]Your instructions[/]")
    except (EOFError, KeyboardInterrupt):
        log.info("step08.intent_review_gate.rejected", reason="interrupt")
        return None

    if not instructions.strip():
        console.print("[dim]empty input — skipping edit[/]")
        return warnings

    agent = package_resource_root() / "agents" / "tbd-intent-editor.agent.md"
    workdir = ctx.workspace.step_dir(8) / "intent-editor"
    workdir.mkdir(parents=True, exist_ok=True)

    console.print("[dim]rewriting intents…[/]")
    llm_result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=instructions,
        inputs={"flagged-intents.json": json.dumps(
            {"intents": warnings}, indent=2, ensure_ascii=False,
        )},
        output_schema=_INTENT_EDITOR_SCHEMA,
        step=8,
    )

    if not llm_result.success or not llm_result.final_text:
        console.print(
            f"[red]intent edit failed:[/] {llm_result.error or 'no output'}"
        )
        recover = Prompt.ask(
            r"\[r\]etry, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        if recover == "q":
            return None
        return warnings

    try:
        edited = json.loads(llm_result.final_text)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM returned unparseable JSON:[/] {e}")
        return warnings

    new_intents = edited.get("intents") or []
    if len(new_intents) != len(warnings):
        console.print(
            f"[red]editor returned {len(new_intents)} entries; expected "
            f"{len(warnings)} — refusing to apply[/]"
        )
        return warnings

    # Rewrite source files in place. We do this best-effort: if a file write
    # fails for one entry, log it and keep going for the rest.
    sut_root = ctx.workspace.sut.resolve()
    rewritten = 0
    failed: list[str] = []
    updated_warnings: list[dict] = []

    for old, new in zip(warnings, new_intents):
        new_intent_str = (new.get("intent") or "").strip()
        old_intent_str = old.get("intent", "")
        # Skip files where the editor returned an unchanged intent — no-op.
        if not new_intent_str or new_intent_str == old_intent_str:
            updated_warnings.append({**old})
            continue
        rel = old.get("file") or ""
        line_no = old.get("line")
        if not rel or not isinstance(line_no, int):
            failed.append(f"{rel}:{line_no} (missing anchor)")
            updated_warnings.append({**old})
            continue
        abs_path = sut_root / rel
        if not abs_path.is_file():
            failed.append(f"{rel} (not found at {abs_path})")
            updated_warnings.append({**old})
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            failed.append(f"{rel} (read: {e})")
            updated_warnings.append({**old})
            continue

        new_text, ok = _replace_intent_at_line(
            text, line_no, old_intent_str, new_intent_str,
        )
        if not ok:
            failed.append(f"{rel}:{line_no} (intent string not found at line)")
            updated_warnings.append({**old})
            continue

        try:
            abs_path.write_text(new_text, encoding="utf-8")
        except OSError as e:
            failed.append(f"{rel} (write: {e})")
            updated_warnings.append({**old})
            continue

        rewritten += 1
        updated_warnings.append({
            **old,
            "intent": new_intent_str,
            "original_score": old.get("score"),
            "score": "EDITED",
            "rationale": old.get("rationale", "") + " · edited via review gate",
        })

    console.print(
        f"[green]rewrote {rewritten} intent(s)[/]"
        + (f"; [red]{len(failed)} failed[/] "
           f"({'; '.join(failed[:3])}{'…' if len(failed) > 3 else ''})"
           if failed else "")
    )
    return updated_warnings


_INTENT_EDITOR_SCHEMA: dict = {
    "type": "object",
    "required": ["intents"],
    "additionalProperties": False,
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["intent"],
                "additionalProperties": True,
                "properties": {
                    "intent": {"type": "string", "maxLength": 120},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "constant_name": {"type": ["string", "null"]},
                    "score": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


def _replace_intent_at_line(
    text: str,
    line_no: int,
    old_intent: str,
    new_intent: str,
) -> tuple[str, bool]:
    """Replace the FIRST occurrence of ``old_intent`` on the 1-based ``line_no``.

    Pure string surgery; preserves the surrounding quote style (single,
    double, or backtick) and the rest of the line verbatim. Returns
    ``(new_text, True)`` on success or ``(text, False)`` when the line is
    out of range or the intent string isn't present on that line.
    """
    lines = text.splitlines(keepends=True)
    idx = line_no - 1
    if idx < 0 or idx >= len(lines):
        return text, False
    line = lines[idx]
    # Try each quote style; whichever wraps the intent on this line wins.
    for q in ("\"", "'", "`"):
        needle = f"{q}{old_intent}{q}"
        if needle in line:
            replacement = f"{q}{new_intent}{q}"
            lines[idx] = line.replace(needle, replacement, 1)
            return "".join(lines), True
    return text, False
