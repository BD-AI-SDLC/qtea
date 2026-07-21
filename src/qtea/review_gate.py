"""Human review gates for pipeline steps.

Post-step review gates that surface artifacts for human review BEFORE
downstream steps consume them. Each gate renders a Rich table summary
and prompts the user with:

- ``a`` approve → return True, pipeline continues.
- ``e`` edit (LLM) → free-text instructions applied by an LLM agent.
- ``f`` file edit → open the artifact ``.md`` in ``$EDITOR``.
- ``q`` quit → return False, pipeline aborts with exit code 1.

Auto-approved (no prompt) when stdin is not a TTY or ``--no-hitl`` is set.

Currently implemented gates:
- **Step 4** — test design (``test-design.md`` / ``.json``)
- **Step 7** — code-modification plan (``code-modification-plan.json`` / ``.md``)
- **Step 8** — TBD intent quality (WARN/FAIL entries)
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from qtea.checkpoints import hash_paths
from qtea.config import package_resource_root
from qtea.llm.reasoning import call_reasoning_llm
from qtea.logging_setup import get_logger
from qtea.schemas import is_valid, load_schema

if TYPE_CHECKING:
    from qtea.steps.base import StepContext, StepResult

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# UI bridge hook
# ---------------------------------------------------------------------------
#
# When the desktop UI is active, the bridge installs a hook here that
# replaces the interactive ``Prompt.ask`` flow with a UI dialog. The hook
# returns ``"approve"`` or ``"reject"`` (Edit-by-text is not yet wired
# through the bridge — treat as approve). The signature is intentionally
# minimal so review_gate.py stays decoupled from Flet / AppState.

_UI_PROMPT_HOOK: Callable[..., tuple[str, str]] | None = None


def set_ui_prompt_hook(hook: Callable[..., tuple[str, str]] | None) -> None:
    """Install / clear the UI prompt hook.

    Hook signature:
    ``hook(step, title, summary_text, *, kind="", data=None) -> tuple[str, str]``
    where the first element is the decision (``"approve"`` / ``"reject"`` /
    ``"edit"``) and the second is the user-typed edit instructions
    (``""`` for non-edit decisions).

    ``kind`` + ``data`` carry the structured payload (strategy / plan /
    intents dict). The UI renders a real table from ``data`` and uses
    ``summary_text`` only as a fallback for unknown kinds.
    """
    global _UI_PROMPT_HOOK
    _UI_PROMPT_HOOK = hook


def _capture_render(render_fn, *args) -> str:
    """Render a Rich table/panel into plain text without touching the terminal.

    Used in UI mode so the gate's table summary can be surfaced inside the
    dialog instead of being printed to stderr/stdout.
    """
    buf = io.StringIO()
    cap = Console(
        file=buf,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    render_fn(*args, cap)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _open_in_editor(file_path: Path, console: Console) -> bool:
    """Open *file_path* in the user's ``$EDITOR`` and return whether it changed."""
    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("notepad" if sys.platform == "win32" else "vi")
    )
    before = hashlib.sha256(file_path.read_bytes()).digest()
    try:
        subprocess.call([editor, str(file_path)])
    except OSError as e:
        console.print(f"[red]could not launch editor ({editor}):[/] {e}")
        return False
    after = hashlib.sha256(file_path.read_bytes()).digest()
    return before != after


# ---------------------------------------------------------------------------
# Step 4 — Test-Strategy review gate
# ---------------------------------------------------------------------------


async def review_step_4_strategy(
    ctx: StepContext,
    result: StepResult,
    console: Console,
) -> bool:
    """Post-step-4 review gate for the test design.

    Auto-approves when stdin is not a TTY or ``--no-hitl`` is set.
    """
    from qtea.steps.s04_strategy import _project_strategy

    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not (sys.stdin.isatty() or getattr(opts, "ui_mode", False)):
        step_dir = ctx.workspace.step_dir(4)
        json_path = step_dir / "test-design.json"
        if json_path.exists():
            from qtea.schemas import is_valid
            ok, err = is_valid(json.loads(json_path.read_text(encoding="utf-8")), "test-design")
            if not ok:
                log.error("step04.review_gate.schema_fail_no_hitl", error=err)
                return False
        log.warning("step04.review_gate.auto_approved", reason="no_hitl")
        return True

    step_dir = ctx.workspace.step_dir(4)
    md_path = step_dir / "test-design.md"
    json_path = step_dir / "test-design.json"

    if not md_path.exists():
        log.warning("step04.review_gate.no_md", path=str(md_path))
        return True

    while True:
        try:
            strategy = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            console.print(f"[red]cannot read strategy JSON:[/] {e}")
            return False

        # UI mode: pass the structured strategy through so the dialog
        # can render a proper Flet table; the captured monospace text is
        # only a fallback if the dialog doesn't recognise the kind.
        if _UI_PROMPT_HOOK is not None:
            summary_text = _capture_render(_render_strategy, strategy)
            decision, edit_instructions = _UI_PROMPT_HOOK(
                step=4,
                title="Test Design Review",
                summary_text=summary_text,
                kind="strategy",
                data=strategy,
            )
            if decision == "reject":
                log.info("step04.review_gate.rejected", source="ui")
                return False
            if decision == "edit" and edit_instructions.strip():
                log.info(
                    "step04.review_gate.ui_edit",
                    instructions_preview=edit_instructions[:80],
                )
                await _apply_strategy_nlp_edit(
                    md_path, json_path, ctx, console,
                    instructions=edit_instructions,
                )
                # Loop back to re-show the (possibly updated) artifact;
                # the top of the while-loop reloads JSON from disk.
                continue
            log.info("step04.review_gate.approved", source="ui")
            return True

        _render_strategy(strategy, console)
        choice = Prompt.ask(
            "[bold]Approve and continue to step 5?[/bold] "
            r"[dim](\[a\]pprove / \[e\]dit LLM / \[f\]ile edit / \[q\]uit)[/]",
            choices=["a", "e", "f", "q"],
            default="a",
            show_choices=False,
        )
        if choice == "a":
            log.info("step04.review_gate.approved")
            return True
        if choice == "q":
            log.info("step04.review_gate.rejected")
            return False

        if choice == "e":
            updated = await _apply_strategy_nlp_edit(
                md_path, json_path, ctx, console,
            )
        else:
            updated = await _apply_strategy_file_edit(
                md_path, json_path, _project_strategy, ctx, console,
            )
        if updated is None:
            return False


def _render_strategy(strategy: dict, console: Console) -> None:
    """Render test-design summary as a Rich table."""
    test_cases = strategy.get("test_cases") or []
    title_text = strategy.get("title") or "Test Design"

    table = Table(
        title=f"Step 4 — {title_text} ({len(test_cases)} test cases)",
        show_lines=True,
        expand=True,
    )
    table.add_column("TC ID", style="bold cyan", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Pri", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Steps", no_wrap=True, justify="right")
    table.add_column("ACs", overflow="fold")

    pri_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}

    _PRI_STYLE = {"P0": "bold red", "P1": "yellow", "P2": "green", "P3": "dim"}

    for tc in test_cases:
        tc_id = tc.get("id") or "<no-id>"
        title = tc.get("title") or ""
        pri = tc.get("priority") or "UNKNOWN"
        atype = tc.get("automation_type") or tc.get("type") or "?"
        steps = tc.get("steps") or []
        acs = ", ".join(tc.get("ac_ids") or []) or "—"

        pri_style = _PRI_STYLE.get(pri, "")
        pri_cell = f"[{pri_style}]{pri}[/{pri_style}]" if pri_style else pri

        pri_counts[pri] = pri_counts.get(pri, 0) + 1
        type_counts[atype] = type_counts.get(atype, 0) + 1

        table.add_row(tc_id, title, pri_cell, atype, str(len(steps)), acs)

    console.print()
    console.print(table)

    pri_summary = " · ".join(
        f"{k}={v}" for k, v in sorted(pri_counts.items())
    )
    type_summary = " · ".join(
        f"{k}={v}" for k, v in sorted(type_counts.items())
    )
    console.print(Panel(
        f"test cases: [bold]{len(test_cases)}[/] · "
        f"by priority: {pri_summary} · by type: {type_summary}"
        "\n\n[bold]\\[a\\][/]pprove and continue   "
        "[bold]\\[e\\][/]dit (LLM)   "
        "[bold]\\[f\\][/]ile edit   "
        "[bold]\\[q\\][/]uit",
        title="Strategy review",
        border_style="cyan",
    ))


async def _apply_strategy_file_edit(
    md_path: Path,
    json_path: Path,
    project_strategy_fn,
    ctx: StepContext,
    console: Console,
) -> dict | None:
    """Open test-design.md in $EDITOR, re-project JSON on save."""
    if not _open_in_editor(md_path, console):
        console.print("[dim]file unchanged or editor failed — skipping[/]")
        return {}  # non-None signals "stay in loop"

    return _reproject_strategy(md_path, json_path, project_strategy_fn, ctx, console)


async def _apply_strategy_nlp_edit(
    md_path: Path,
    json_path: Path,
    ctx: StepContext,
    console: Console,
    instructions: str | None = None,
) -> dict | None:
    """Apply free-text instructions to test-design.md via LLM.

    *instructions* is optional: ``None`` triggers the CLI ``Prompt.ask`` flow
    (used by the terminal review gate). UI callers pass the user-typed text
    directly so the ``Prompt.ask`` is skipped — the LLM-failure recover
    prompt is also skipped on the UI path so the caller can loop back via
    the dialog instead of hanging on stdin.
    """
    from qtea.steps.s04_strategy import _project_strategy

    ui_mode = instructions is not None
    if not ui_mode:
        console.print(Panel(
            "Describe the changes you want in plain English.\n"
            "Examples: [dim]\"remove TC-03\"[/], [dim]\"change TC-login "
            "priority to P0\"[/], [dim]\"add a smoke test for the search "
            "feature\"[/]",
            title="Edit strategy",
            border_style="yellow",
        ))
        try:
            instructions = Prompt.ask("[bold]Your instructions[/]")
        except (EOFError, KeyboardInterrupt):
            log.info("step04.review_gate.rejected", reason="interrupt")
            return None

    assert instructions is not None
    if not instructions.strip():
        console.print("[dim]empty input — skipping edit[/]")
        return {}

    agent = package_resource_root() / "agents" / "design-editor.agent.md"
    workdir = ctx.workspace.step_dir(4) / "design-editor"
    workdir.mkdir(parents=True, exist_ok=True)

    current_md = md_path.read_text(encoding="utf-8")

    console.print("[dim]applying edits…[/]")
    llm_result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=instructions,
        inputs={"test-design.md": current_md},
        output_schema=None,
        step=4,
    )

    if not llm_result.success or not llm_result.final_text:
        console.print(
            f"[red]LLM edit failed:[/] {llm_result.error or 'no output'}"
        )
        if ui_mode:
            return {}  # loop back; the dialog re-renders the unchanged artifact
        recover = Prompt.ask(
            r"\[r\]etry, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        return None if recover == "q" else {}

    md_path.write_text(llm_result.final_text, encoding="utf-8")
    return _reproject_strategy(
        md_path, json_path, _project_strategy, ctx, console, ui_mode=ui_mode,
    )


def _reproject_strategy(
    md_path: Path,
    json_path: Path,
    project_strategy_fn,
    ctx: StepContext,
    console: Console,
    ui_mode: bool = False,
) -> dict | None:
    """Re-project test-design.md → .json, validate, persist.

    *ui_mode* suppresses the interactive ``Prompt.ask`` recover prompts so
    the worker thread doesn't hang on stdin the UI user can't reach.
    """
    new_md = md_path.read_text(encoding="utf-8")
    projection = project_strategy_fn(new_md)

    dup_ids = projection.pop("_duplicate_tc_ids", [])
    if dup_ids:
        console.print(
            f"[red]duplicate TC IDs:[/] {', '.join(dup_ids)}. "
            f"Fix them in the markdown and retry."
        )
        if ui_mode:
            return {}  # loop back; dialog re-renders the prior JSON
        recover = Prompt.ask(
            r"\[r\]etry file edit, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        return None if recover == "q" else {}

    ok, err = is_valid(projection, "test-design")
    if not ok:
        console.print(f"[red]edited strategy failed schema validation:[/] {err}")
        if ui_mode:
            return {}
        recover = Prompt.ask(
            r"\[r\]etry, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        return None if recover == "q" else {}

    json_path.write_text(
        json.dumps(projection, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    record = ctx.state.steps.get(4)
    if record is not None:
        record.output_hashes = hash_paths(
            [p for p in json_path.parent.iterdir() if p.is_file()]
        )
    return projection


# ---------------------------------------------------------------------------
# Step 7 — Code-modification plan review gate
# ---------------------------------------------------------------------------


async def review_step_7_plan(
    ctx: StepContext,
    result: StepResult,
    console: Console,
) -> bool:
    """Run the post-step-7 plan review gate. Return True on approve.

    Auto-approves (returns True) when stdin is not a TTY or ``--no-hitl`` is
    set. On the ``edit`` choice the user types free-text instructions and an
    LLM applies the delta; the gate re-validates against the schema and
    re-renders. On schema failure after edit, the user can re-edit or abort.
    """
    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not (sys.stdin.isatty() or getattr(opts, "ui_mode", False)):
        step_dir = ctx.workspace.step_dir(7)
        plan_path = step_dir / "code-modification-plan.json"
        if plan_path.exists():
            from qtea.schemas import is_valid
            ok, err = is_valid(json.loads(plan_path.read_text(encoding="utf-8")), "code-modification-plan")
            if not ok:
                log.error("step07.review_gate.schema_fail_no_hitl", error=err)
                return False
        log.warning("step07.review_gate.auto_approved", reason="no_hitl")
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

        if _UI_PROMPT_HOOK is not None:
            summary_text = _capture_render(_render_plan, plan)
            decision, edit_instructions = _UI_PROMPT_HOOK(
                step=7,
                title="Code Modification Plan Review",
                summary_text=summary_text,
                kind="plan",
                data=plan,
            )
            if decision == "reject":
                log.info("step07.review_gate.rejected", source="ui")
                return False
            if decision == "edit" and edit_instructions.strip():
                log.info(
                    "step07.review_gate.ui_edit",
                    instructions_preview=edit_instructions[:80],
                )
                await _apply_nlp_edit(
                    plan, plan_path, ctx, console,
                    instructions=edit_instructions,
                )
                # Loop back; the top of the while-loop reloads the plan
                # JSON from disk so we re-render the LLM-edited version.
                continue
            log.info("step07.review_gate.approved", source="ui")
            return True

        _render_plan(plan, console)
        choice = Prompt.ask(
            "[bold]Approve and continue to step 8?[/bold] "
            r"[dim](\[a\]pprove / \[e\]dit LLM / \[f\]ile edit / \[q\]uit)[/]",
            choices=["a", "e", "f", "q"],
            default="a",
            show_choices=False,
        )
        if choice == "a":
            log.info("step07.review_gate.approved")
            return True
        if choice == "q":
            log.info("step07.review_gate.rejected")
            return False

        if choice == "e":
            edited_plan = await _apply_nlp_edit(plan, plan_path, ctx, console)
        else:
            edited_plan = await _apply_plan_file_edit(
                plan, plan_path, step_dir, ctx, console,
            )
        if edited_plan is None:
            return False
        # Loop back to re-render the updated plan and re-prompt.


async def _apply_nlp_edit(
    plan: dict,
    plan_path: Path,
    ctx: StepContext,
    console: Console,
    instructions: str | None = None,
) -> dict | None:
    """Apply free-text instructions to the code-modification plan via LLM.

    *instructions* is optional: ``None`` triggers the CLI ``Prompt.ask`` flow
    (terminal review gate). UI callers pass the user-typed text directly;
    on the UI path the LLM-failure / schema-failure recover prompts are
    skipped (the dialog loops back so the user can retry or reject).

    Returns the updated plan dict on success, ``plan`` (unchanged) for the
    "loop back" outcomes, or ``None`` only when the CLI user explicitly quits.
    Writes the updated JSON to *plan_path* and refreshes checkpoint hashes.
    """
    ui_mode = instructions is not None
    if not ui_mode:
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

    assert instructions is not None
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
        if ui_mode:
            return plan
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
        if ui_mode:
            return plan
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
    ctx: StepContext,
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


async def _apply_plan_file_edit(
    plan: dict,
    plan_path: Path,
    step_dir: Path,
    ctx: StepContext,
    console: Console,
) -> dict | None:
    """Open code-modification-plan.md in $EDITOR, sync changes back to JSON."""
    from qtea.steps.s07_test_architect import _render_plan_markdown

    md_path = step_dir / "code-modification-plan.md"
    # Re-render .md from current JSON so the file reflects any prior edits.
    old_md = _render_plan_markdown(plan)
    md_path.write_text(old_md, encoding="utf-8")

    if not _open_in_editor(md_path, console):
        console.print("[dim]file unchanged or editor failed — skipping[/]")
        return plan

    new_md = md_path.read_text(encoding="utf-8")
    if new_md == old_md:
        console.print("[dim]no changes detected — skipping[/]")
        return plan

    return await _sync_md_to_json(old_md, new_md, plan, plan_path, ctx, console)


async def _sync_md_to_json(
    old_md: str,
    new_md: str,
    current_plan: dict,
    plan_path: Path,
    ctx: StepContext,
    console: Console,
) -> dict | None:
    """Diff old/new .md and have the plan-editor LLM apply changes to JSON."""
    from qtea.steps.s07_test_architect import _render_plan_markdown

    diff_lines = list(difflib.unified_diff(
        old_md.splitlines(), new_md.splitlines(),
        fromfile="before", tofile="after", lineterm="",
    ))
    if not diff_lines:
        console.print("[dim]no diff detected — skipping[/]")
        return current_plan

    diff_text = "\n".join(diff_lines)

    agent = package_resource_root() / "agents" / "plan-editor.agent.md"
    workdir = plan_path.parent / "plan-editor"
    workdir.mkdir(parents=True, exist_ok=True)

    console.print("[dim]syncing markdown edits to plan JSON…[/]")
    llm_result = await call_reasoning_llm(
        agent,
        workdir=workdir,
        user_prompt=(
            "The user directly edited the markdown view of this plan. "
            "The diff of their changes is below. Apply the equivalent "
            "changes to the JSON plan. Preserve all fields the diff "
            "doesn't touch.\n\n```diff\n" + diff_text + "\n```"
        ),
        inputs={
            "code-modification-plan.json": json.dumps(
                current_plan, indent=2,
            ),
        },
        output_schema=load_schema("code-modification-plan"),
        step=7,
    )

    if not llm_result.success or not llm_result.final_text:
        console.print(
            f"[red]LLM sync failed:[/] {llm_result.error or 'no output'}"
        )
        recover = Prompt.ask(
            r"\[r\]etry, \[q\]uit",
            choices=["r", "q"],
            default="r",
            show_choices=False,
        )
        return None if recover == "q" else current_plan

    try:
        new_plan: dict = json.loads(llm_result.final_text)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM returned unparseable JSON:[/] {e}")
        return current_plan

    ok, err = is_valid(new_plan, "code-modification-plan")
    if not ok:
        console.print(f"[red]synced plan failed schema validation:[/] {err}")
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
        return current_plan

    _persist_and_refresh_hashes(new_plan, plan_path, ctx)
    # Re-render .md from the updated JSON for consistency.
    md_path = plan_path.parent / "code-modification-plan.md"
    md_path.write_text(_render_plan_markdown(new_plan), encoding="utf-8")
    return new_plan


def _deferred_locators_from_units(units: list[dict]) -> list[dict]:
    """Exemplar (non-POM) lane: TBD locators live inside each reusable unit's
    ``deferred_targets[]`` (name + intent), not in the TC-level ``locators[]``.
    Flatten them into locator-shaped dicts (source=create_tbd) so the plan
    renderers can surface them with their owning unit."""
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
    table.add_column("POMs / Units", overflow="fold")
    table.add_column("Locators", overflow="fold")

    totals = {
        "fixtures": {"reuse": 0, "create": 0},
        "page_objects": {"reuse": 0, "create": 0, "missing_methods": 0},
        "reusable_units": {"reuse": 0, "create": 0, "missing_behaviors": 0},
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
            tag = "[green]reuse[/]" if src == "reuse" else "[yellow]create[/]"
            ref = f.get("from") or f.get("at") or "?"
            fix_lines.append(f"{tag} {f.get('name')} ← {ref}")
        fix_cell = "\n".join(fix_lines) or "—"

        poms = tc.get("page_objects") or []
        units = tc.get("reusable_units") or []
        # Exemplar (non-POM) lane: the fifth column shows reusable units and the
        # Locators column is sourced from each unit's deferred_targets[].
        if units and not poms:
            unit_lines = []
            for u in units:
                src = u.get("source", "?")
                totals["reusable_units"][src] = (
                    totals["reusable_units"].get(src, 0) + 1
                )
                mb = u.get("missing_behaviors") or []
                totals["reusable_units"]["missing_behaviors"] += len(mb)
                tag = "[green]reuse[/]" if src == "reuse" else "[yellow]create[/]"
                ref = u.get("from") or u.get("at") or "?"
                cat = u.get("category")
                cat_s = f" [dim]({cat})[/]" if cat else ""
                suffix = f" (+{len(mb)} behaviors)" if mb else ""
                unit_lines.append(f"{tag} {u.get('name')}{cat_s} ← {ref}{suffix}")
            pom_cell = "\n".join(unit_lines) or "—"

            loc_lines = []
            for loc in _deferred_locators_from_units(units):
                totals["locators"]["create_tbd"] += 1
                intent = loc.get("intent") or "?"
                owner = loc.get("owning_page") or "?"
                tbd_intents.append(f"{loc.get('name')}: {intent}")
                loc_lines.append(
                    f"[yellow]TBD[/] {loc.get('name')} [dim]({owner})[/]: "
                    f"\"{intent}\""
                )
            loc_cell = "\n".join(loc_lines) or "—"
        else:
            pom_lines = []
            for p in poms:
                src = p.get("source", "?")
                totals["page_objects"][src] = (
                    totals["page_objects"].get(src, 0) + 1
                )
                mm = p.get("missing_methods") or []
                totals["page_objects"]["missing_methods"] += len(mm)
                tag = "[green]reuse[/]" if src == "reuse" else "[yellow]create[/]"
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
                    loc_lines.append(
                        f"[yellow]TBD[/] {loc.get('name')}: \"{intent}\""
                    )
                else:
                    loc_lines.append(
                        f"[green]reuse[/] {loc.get('name')} ← "
                        f"{loc.get('from') or '?'}"
                    )
            loc_cell = "\n".join(loc_lines) or "—"

        table.add_row(tc_id, target, test_cell, fix_cell, pom_cell, loc_cell)

    console.print()
    console.print(table)

    ru = totals["reusable_units"]
    has_units = bool(ru["reuse"] or ru["create"])
    footer = [
        f"test cases: [bold]{len(test_cases)}[/]",
        f"test functions: [bold]{totals['test_functions']}[/]",
        f"fixtures: [green]{totals['fixtures']['reuse']} reuse[/] · "
        f"[yellow]{totals['fixtures']['create']} create[/]",
    ]
    if has_units:
        footer.append(
            f"reusable units: [green]{ru['reuse']} reuse[/] · "
            f"[yellow]{ru['create']} create[/] · "
            f"+{ru['missing_behaviors']} missing behaviors"
        )
    else:
        footer.append(
            f"page objects: [green]{totals['page_objects']['reuse']} reuse[/] · "
            f"[yellow]{totals['page_objects']['create']} create[/] · "
            f"+{totals['page_objects']['missing_methods']} missing methods"
        )
    footer.append(
        f"locators: [green]{totals['locators']['reuse']} reuse[/] · "
        f"[yellow]{totals['locators']['create_tbd']} TBD[/]"
    )
    console.print(Panel(
        " · ".join(footer)
        + "\n\n[bold]\\[a\\][/]pprove and continue   "
        "[bold]\\[e\\][/]dit (LLM)   "
        "[bold]\\[f\\][/]ile edit   "
        "[bold]\\[q\\][/]uit",
        title="Review",
        border_style="cyan",
    ))


# ---------------------------------------------------------------------------
# Step 8 Phase D — TBD intent quality review gate
# ---------------------------------------------------------------------------


async def review_step_8_intents(
    ctx: StepContext,
    result: StepResult,
    console: Console,
) -> bool:
    """Surface WARN/FAIL intent entries from Phase D for human review.

    Mirrors `review_step_7_plan` shape. Auto-approves on non-TTY / `--no-hitl`.
    On `[e]dit`, the user types free-text instructions, the `tbd-intent-editor`
    agent rewrites the intent list, and the pipeline updates the SUT sources
    in place via the file:line anchors recorded by `tbd_scanner`.
    """
    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not (sys.stdin.isatty() or getattr(opts, "ui_mode", False)):
        log.warning("step08.intent_review_gate.auto_approved", reason="no_hitl")
        return True

    warnings = ctx.extras.get("step8_intent_warnings") or []
    if not warnings:
        return True

    while True:
        if _UI_PROMPT_HOOK is not None:
            summary_text = _capture_render(_render_intent_warnings, warnings)
            decision, edit_instructions = _UI_PROMPT_HOOK(
                step=8,
                title="TBD Intent Quality Review",
                summary_text=summary_text,
                kind="intents",
                data=warnings,
            )
            if decision == "reject":
                log.info("step08.intent_review_gate.rejected", source="ui")
                return False
            if decision == "edit" and edit_instructions.strip():
                log.info(
                    "step08.intent_review_gate.ui_edit",
                    instructions_preview=edit_instructions[:80],
                )
                edited = await _apply_intent_edit(
                    warnings, ctx, console,
                    instructions=edit_instructions,
                )
                if edited is not None:
                    warnings = edited
                    ctx.extras["step8_intent_warnings"] = warnings
                continue
            log.info(
                "step08.intent_review_gate.approved",
                source="ui",
                count=len(warnings),
            )
            return True

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
    table.add_column("Context", overflow="fold", style="dim")

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
            w.get("code_context") or "",
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
    ctx: StepContext,
    console: Console,
    instructions: str | None = None,
) -> list[dict] | None:
    """Rewrite flagged TBD intents via LLM and patch the SUT source files.

    *instructions* is optional: ``None`` triggers the CLI ``Prompt.ask`` flow
    (terminal review gate). UI callers pass the user-typed text directly;
    on the UI path the LLM-failure recover prompt is skipped (the dialog
    loops back so the user can retry or reject).

    Returns the updated warnings list on success (with edited intents
    substituted), ``warnings`` (unchanged) for "loop back" outcomes, or
    ``None`` only when the CLI user explicitly quits. Rewrites sentinel
    call-sites in the SUT sources in-place using the file:line anchors.
    """
    ui_mode = instructions is not None
    if not ui_mode:
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

    assert instructions is not None
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
        if ui_mode:
            return warnings
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

    for old, new in zip(warnings, new_intents, strict=False):
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
