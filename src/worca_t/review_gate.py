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
- `e` edit files → user edits `code-modification-plan.json` directly; on
  Enter the gate re-validates against the schema and re-renders.
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
from worca_t.logging_setup import get_logger
from worca_t.schemas import is_valid

if TYPE_CHECKING:
    from worca_t.steps.base import StepContext, StepResult

log = get_logger(__name__)


def review_step_7_plan(
    ctx: "StepContext",
    result: "StepResult",
    console: Console,
) -> bool:
    """Run the post-step-7 plan review gate. Return True on approve.

    Auto-approves (returns True) when stdin is not a TTY or ``--no-hitl`` is
    set. On the ``edit`` choice the user edits the JSON file directly; the
    gate re-validates against the schema and re-renders. On schema failure
    after edit, the user can either re-edit or abort.
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
        console.print(Panel(
            f"Edit the plan JSON at:\n  [cyan]{plan_path}[/]\n\n"
            "When you're done, press [bold]Enter[/] to re-validate "
            "and re-render.",
            title="Manual edit",
            border_style="yellow",
        ))
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            log.info("step07.review_gate.rejected", reason="interrupt")
            return False

        # Re-validate after edit. If invalid, let the user re-edit or abort.
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            console.print(f"[red]plan JSON unparseable after edit:[/] {e}")
            recover = Prompt.ask(
                r"\[r\]e-edit, \[q\]uit",
                choices=["r", "q"],
                default="r",
                show_choices=False,
            )
            if recover == "q":
                return False
            continue

        ok, err = is_valid(plan, "code-modification-plan")
        if not ok:
            console.print(f"[red]plan failed schema validation:[/] {err}")
            recover = Prompt.ask(
                r"\[r\]e-edit, \[a\]pprove anyway (risky), \[q\]uit",
                choices=["r", "a", "q"],
                default="r",
                show_choices=False,
            )
            if recover == "a":
                # Refresh checkpoint hashes so a later --resume doesn't treat
                # the human edits as drift.
                record = ctx.state.steps.get(7)
                if record is not None:
                    record.output_hashes = hash_paths(result.outputs)
                return True
            if recover == "q":
                return False
            continue

        # Schema-valid edit. Refresh hashes so resume sees the new bytes.
        record = ctx.state.steps.get(7)
        if record is not None:
            record.output_hashes = hash_paths(result.outputs)
        # Loop back to re-render the updated plan and re-prompt.


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
