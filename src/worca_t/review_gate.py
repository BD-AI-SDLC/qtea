"""Post-step-7 human review gate.

Lightweight TDD review: surface generated test names + descriptions + counts so
the reviewer can approve the codegen output or pause to manually edit the
generated test files. On manual edit the gate re-indexes the SUT so step 8
sees fresh line numbers and a refreshed ``tbd-index.json``. No agent
re-invocation here — manual edits flow through to step 8 because step 8 reads
test bytes from the SUT disk (see ``s08_locator_resolution.py``).

Auto-skipped when stdin is not a TTY or when ``--no-hitl`` is set.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from worca_t.checkpoints import hash_paths
from worca_t.logging_setup import get_logger

if TYPE_CHECKING:
    from worca_t.steps.base import StepContext, StepResult

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-test description extraction (framework-aware, best-effort)
# ---------------------------------------------------------------------------

_PY_DEF_RE = re.compile(r"^\s*(async\s+)?def\s+\w+")
_PY_TRIPLE_RE = re.compile(r'^\s*(?P<q>["\']{3})')
_JS_LINE_COMMENT_RE = re.compile(r"^\s*//\s?(.*)$")
_JS_BLOCK_END_RE = re.compile(r"\*/\s*$")
_JS_BLOCK_START_RE = re.compile(r"^\s*/\*\*?")
_ROBOT_DOC_RE = re.compile(r"^\s*\[Documentation\]\s+(.*)$", re.IGNORECASE)


def _shorten(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _py_docstring(lines: list[str], idx: int) -> str | None:
    start = None
    for cand in range(max(0, idx - 2), min(len(lines), idx + 3)):
        if _PY_DEF_RE.match(lines[cand]):
            start = cand
            break
    if start is None:
        return None
    cur = start
    while cur < len(lines) and not lines[cur].rstrip().endswith(":"):
        cur += 1
    cur += 1
    while cur < len(lines) and not lines[cur].strip():
        cur += 1
    if cur >= len(lines):
        return None
    m = _PY_TRIPLE_RE.match(lines[cur])
    if not m:
        return None
    q = m.group("q")
    body = lines[cur][m.end():]
    if q in body:
        return _shorten(body.split(q, 1)[0].strip())
    pieces = [body]
    cur += 1
    while cur < len(lines):
        if q in lines[cur]:
            pieces.append(lines[cur].split(q, 1)[0])
            break
        pieces.append(lines[cur])
        cur += 1
    return _shorten(" ".join(p.strip() for p in pieces if p.strip()))


def _strip_block_comment(block_lines: list[str]) -> str | None:
    parts: list[str] = []
    for bl in block_lines:
        bl = bl.strip()
        bl = re.sub(r"^/\*\*?", "", bl)
        bl = re.sub(r"\*/$", "", bl)
        bl = re.sub(r"^\*\s?", "", bl)
        bl = bl.strip()
        if bl and not bl.startswith("@"):
            parts.append(bl)
    return _shorten(" ".join(parts)) if parts else None


def _js_comment_above(lines: list[str], idx: int) -> str | None:
    cur = idx - 1
    while cur >= 0 and not lines[cur].strip():
        cur -= 1
    if cur < 0:
        return None
    if _JS_BLOCK_END_RE.search(lines[cur]):
        end = cur
        while cur >= 0 and not _JS_BLOCK_START_RE.match(lines[cur]):
            cur -= 1
        if cur < 0:
            return None
        return _strip_block_comment(lines[cur:end + 1])
    parts: list[str] = []
    while cur >= 0:
        m = _JS_LINE_COMMENT_RE.match(lines[cur])
        if not m:
            break
        parts.append(m.group(1).strip())
        cur -= 1
    if not parts:
        return None
    parts.reverse()
    return _shorten(" ".join(p for p in parts if p))


def _java_javadoc_above(lines: list[str], idx: int) -> str | None:
    cur = idx - 1
    while cur >= 0 and (lines[cur].strip().startswith("@") or not lines[cur].strip()):
        cur -= 1
    if cur < 0:
        return None
    if not _JS_BLOCK_END_RE.search(lines[cur]):
        return None
    end = cur
    while cur >= 0 and not _JS_BLOCK_START_RE.match(lines[cur]):
        cur -= 1
    if cur < 0:
        return None
    return _strip_block_comment(lines[cur:end + 1])


def _robot_documentation(lines: list[str], idx: int) -> str | None:
    for cur in range(idx, min(len(lines), idx + 10)):
        m = _ROBOT_DOC_RE.match(lines[cur])
        if m:
            return _shorten(m.group(1).strip())
    return None


def _extract_description(file_path: Path, line: int) -> str | None:
    """One-line description of the test at ``line`` in ``file_path``.

    Heuristics by extension. Best-effort — returns None on any miss so the
    renderer falls back to ``(no description)`` instead of failing the gate.
    """
    if not file_path.exists() or line < 1:
        return None
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    idx = min(line, len(lines)) - 1
    ext = file_path.suffix.lower()
    if ext == ".py":
        return _py_docstring(lines, idx)
    if ext in {".ts", ".tsx", ".js", ".jsx"}:
        return _js_comment_above(lines, idx)
    if ext == ".java":
        return _java_javadoc_above(lines, idx)
    if ext == ".robot":
        return _robot_documentation(lines, idx)
    return None


# ---------------------------------------------------------------------------
# Gate entry point + rendering
# ---------------------------------------------------------------------------


def review_step_7_tests(
    ctx: "StepContext",
    result: "StepResult",
    console: Console,
) -> bool:
    """Run the post-step-7 human review gate. Return True on approve.

    Auto-approves (returns True) when stdin is not a TTY or ``--no-hitl`` is
    set. On the ``edit`` choice the SUT is re-indexed in-place so step 8 sees
    fresh line numbers; ``record.output_hashes`` is refreshed so a later
    ``--resume`` doesn't treat the human edits as drift.
    """
    opts = ctx.options
    if getattr(opts, "no_hitl", False) or not sys.stdin.isatty():
        log.info("step07.review_gate.skip", reason="non_tty_or_no_hitl")
        return True

    step_dir = ctx.workspace.step_dir(7)
    index_path = step_dir / "tbd-index.json"
    if not index_path.exists():
        log.warning("step07.review_gate.no_index", path=str(index_path))
        return True

    sut_root = ctx.workspace.sut.resolve()

    while True:
        _render_review(index_path, sut_root, console)
        choice = Prompt.ask(
            "[bold]Approve and continue to step 8?[/bold] "
            "[dim]([a]pprove / [e]dit files / [q]uit)[/]",
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
            f"Edit any worca_*-prefixed files under:\n  [cyan]{sut_root}[/]\n\n"
            "When you're done, press [bold]Enter[/] to re-index and re-render.",
            title="Manual edit",
            border_style="yellow",
        ))
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            log.info("step07.review_gate.rejected", reason="interrupt")
            return False
        if not _reindex(ctx, result, index_path, sut_root, console):
            recover = Prompt.ask(
                "[red]Re-index failed.[/red] [a]pprove anyway, [r]e-edit, [q]uit",
                choices=["a", "r", "q"],
                default="r",
                show_choices=False,
            )
            if recover == "a":
                return True
            if recover == "q":
                return False
            continue


def _render_review(index_path: Path, sut_root: Path, console: Console) -> None:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    framework = payload.get("framework", "unknown")
    tests = payload.get("tests", [])
    support_files = payload.get("support_files", [])
    violations = payload.get("violations", [])
    totals = payload.get("totals", {})

    table = Table(
        title=f"Step 7 — Generated tests ({framework})",
        show_lines=False,
        expand=True,
    )
    table.add_column("Test", style="bold cyan", no_wrap=False)
    table.add_column("TC refs", style="magenta", no_wrap=True)
    table.add_column("File:Line", style="dim", no_wrap=True)
    table.add_column("Description", overflow="fold")

    for t in tests:
        name = t.get("name") or t.get("id") or "<unnamed>"
        tc_refs = ", ".join(t.get("tc_refs") or []) or "—"
        rel_file = t.get("file") or ""
        line = t.get("line")
        file_line = f"{rel_file}:{line}" if line else rel_file
        abs_path = (sut_root / rel_file) if rel_file and not Path(rel_file).is_absolute() else Path(rel_file)
        desc = _extract_description(abs_path, int(line)) if (abs_path.exists() and line) else None
        table.add_row(name, tc_refs, file_line, desc or "[dim](no description)[/]")

    console.print()
    console.print(table)

    footer = [
        f"tests: [bold]{len(tests)}[/]",
        f"support files: [bold]{len(support_files)}[/]",
        f"tbd locators: [bold]{totals.get('tbd_locators', 0)}[/]",
    ]
    if violations:
        footer.append(f"[red]violations: {len(violations)}[/]")
    console.print(Panel(
        " · ".join(footer)
        + "\n\n[bold][a][/]pprove and continue   [bold][e][/]dit files   [bold][q][/]uit",
        title="Review",
        border_style="cyan",
    ))


def _reindex(
    ctx: "StepContext",
    result: "StepResult",
    index_path: Path,
    sut_root: Path,
    console: Console,
) -> bool:
    """Re-walk the SUT, rewrite tbd-index.json, refresh checkpoint hashes.

    Returns True on success. On failure the caller decides whether to retry,
    approve anyway, or abort — see ``review_step_7_tests``.
    """
    from worca_t.steps.s07_codegen import _filter_index_to_worca
    from worca_t.test_indexer import index_tests, resolve_framework

    try:
        payload_old = json.loads(index_path.read_text(encoding="utf-8"))
        framework = resolve_framework(payload_old.get("framework"), sut_root)
        full = index_tests(sut_root, framework=framework)
        new_payload = _filter_index_to_worca(full, sut_root).as_dict()
        index_path.write_text(
            json.dumps(new_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("step07.review_gate.reindex_failed", error=str(e))
        console.print(f"[red]Re-index error:[/] {e}")
        return False

    record = ctx.state.steps.get(7)
    if record is not None:
        record.output_hashes = hash_paths(result.outputs)

    violations = new_payload.get("violations") or []
    if violations:
        console.print(
            f"[yellow]⚠ Re-index found {len(violations)} rule violation(s) "
            f"in the edited files. Step 8 will reject these — fix before "
            f"approving.[/]"
        )
    return True
