"""Human-in-the-loop: detect agent clarification requests and prompt the user.

After steps 2 (refine) and 3 (plan), agents may emit unresolved
``[CLARIFICATION NEEDED: ...]`` tags or list blockers/open questions. This
module surfaces those to the user via the CLI, collects answers, and packages
them into a markdown file the agent can read on the next invocation.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from worca_t.logging_setup import get_logger
from worca_t.md_parser import extract_bullets, extract_tables, parse_markdown

log = get_logger(__name__)

_CLARIFICATION_RE = re.compile(r"\[CLARIFICATION\s+NEEDED:\s*([^\]]+)\]", re.IGNORECASE)
_NOT_READY_RE = re.compile(r"\bNOT\s+READY\b", re.IGNORECASE)
_XREF_BLOCKER_RE = re.compile(
    r"\s*(?:—|--|–|-)\s*see\s+blocker\s+#?(\d+)", re.IGNORECASE
)
_BLOCKER_PREFIX = "How should we resolve this blocker: "
_TC_ID_RE = re.compile(r"TC-[A-Z]+-\d+", re.IGNORECASE)


@dataclass
class Question:
    """A single unresolved question surfaced to the user."""

    id: str
    kind: str  # "clarification" | "blocker" | "open_question"
    prompt_text: str
    context: str = ""


def _normalize_question_text(text: str) -> str:
    """Normalize question text for dedup: strip boilerplate, kind-agnostic."""
    t = text.strip().lower()
    prefix = _BLOCKER_PREFIX.lower()
    if t.startswith(prefix):
        t = t[len(prefix) :]
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)
    t = _XREF_BLOCKER_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def question_key(q: Question) -> str:
    """Stable identity for a question across iterations (kind-agnostic, normalized)."""
    return _normalize_question_text(q.prompt_text)


_KIND_PRIORITY = {"blocker": 0, "open_question": 1, "clarification": 2}


def _extract_tc_ids(q: Question) -> set[str]:
    """Extract test-case IDs (e.g. TC-GNAV-009) from a question's text and context."""
    ids: set[str] = set()
    for text in (q.prompt_text, q.context):
        ids.update(m.upper() for m in _TC_ID_RE.findall(text))
    return ids


def _dedup(qs: list[Question]) -> list[Question]:
    """Deduplicate questions across kinds, resolving cross-references.

    Three dedup passes run in order:
    1. "see blocker #N" — clarifications/open questions referencing a blocker
       by number are dropped.
    2. TC-ID overlap — non-blocker questions whose test-case IDs overlap with
       a blocker's affected TCs are dropped.
    3. Normalised-text — remaining duplicates with the same normalised text
       are collapsed (blocker wins via sort priority).
    """
    blocker_by_num: dict[int, Question] = {}
    for q in qs:
        if q.kind == "blocker" and q.id.startswith("BLOCK-"):
            try:
                num = int(q.id.split("-")[1])
                blocker_by_num[num] = q
            except (IndexError, ValueError):
                pass

    xref_ids: set[str] = set()

    # Pass 1: "see blocker #N" cross-references
    for q in qs:
        if q.kind != "blocker":
            m = _XREF_BLOCKER_RE.search(q.prompt_text)
            if m:
                ref_num = int(m.group(1))
                if ref_num in blocker_by_num:
                    xref_ids.add(q.id)

    # Pass 2: TC-ID overlap — drop non-blockers whose TCs are covered by a blocker
    blocker_tcs: set[str] = set()
    for q in qs:
        if q.kind == "blocker":
            blocker_tcs.update(_extract_tc_ids(q))
    if blocker_tcs:
        for q in qs:
            if q.kind != "blocker" and q.id not in xref_ids:
                q_tcs = _extract_tc_ids(q)
                if q_tcs and q_tcs <= blocker_tcs:
                    xref_ids.add(q.id)

    # Pass 3: normalised-text dedup (blockers sorted first so they win ties)
    sorted_qs = sorted(qs, key=lambda q: _KIND_PRIORITY.get(q.kind, 9))

    seen: set[str] = set()
    out: list[Question] = []
    for q in sorted_qs:
        if q.id in xref_ids:
            continue
        key = question_key(q)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _extract_clarifications(md_text: str) -> list[Question]:
    out: list[Question] = []
    for idx, m in enumerate(_CLARIFICATION_RE.finditer(md_text), start=1):
        text = m.group(1).strip()
        line_start = md_text.rfind("\n", 0, m.start()) + 1
        line_end = md_text.find("\n", m.end())
        if line_end == -1:
            line_end = len(md_text)
        context = md_text[line_start:line_end].strip()
        out.append(
            Question(
                id=f"CLAR-{idx:02d}",
                kind="clarification",
                prompt_text=text,
                context=context,
            )
        )
    return out


def _extract_blockers(md_text: str) -> list[Question]:
    """Pick blockers out of a `## Blockers` table or bullet list."""
    root = parse_markdown(md_text)
    sec = root.find("blocker")
    if sec is None:
        return []
    out: list[Question] = []
    for table in extract_tables(sec.content):
        if not table or len(table) < 2:
            continue
        header = [h.lower() for h in table[0]]
        # Prefer "description" column; fall back to "blocker" column.
        desc_idx = next(
            (i for i, h in enumerate(header) if "description" in h),
            next(
                (i for i, h in enumerate(header) if "blocker" in h),
                0,
            ),
        )
        for row_idx, row in enumerate(table[1:], start=1):
            if row_idx > len(table) - 1:
                break
            if not row or all(not c.strip() for c in row):
                continue
            desc = row[desc_idx] if desc_idx < len(row) else row[0]
            desc = desc.strip()
            if not desc or desc.lower().startswith("no blockers"):
                continue
            out.append(
                Question(
                    id=f"BLOCK-{row_idx:02d}",
                    kind="blocker",
                    prompt_text=f"{_BLOCKER_PREFIX}{desc}",
                    context=" | ".join(row),
                )
            )
    if not out:
        for idx, bullet in enumerate(extract_bullets(sec.content), start=1):
            text = bullet.strip()
            if not text or text.lower().startswith("no blockers"):
                continue
            out.append(
                Question(
                    id=f"BLOCK-{idx:02d}",
                    kind="blocker",
                    prompt_text=f"{_BLOCKER_PREFIX}{text}",
                    context=text,
                )
            )
    return out


def _extract_open_questions(md_text: str) -> list[Question]:
    root = parse_markdown(md_text)
    sec = root.find("open question") or root.find("open po question")
    if sec is None:
        return []
    out: list[Question] = []
    bullets = extract_bullets(sec.content)
    for child in sec.children:
        bullets.extend(extract_bullets(child.content))
    for idx, b in enumerate(bullets, start=1):
        text = b.strip()
        if not text:
            continue
        out.append(
            Question(
                id=f"OPENQ-{idx:02d}",
                kind="open_question",
                prompt_text=text,
                context=text,
            )
        )
    return out


def extract_questions(md_text: str) -> list[Question]:
    """Return every unresolved question / blocker / clarification in the markdown."""
    qs: list[Question] = []
    qs.extend(_extract_clarifications(md_text))
    qs.extend(_extract_blockers(md_text))
    qs.extend(_extract_open_questions(md_text))
    return _dedup(qs)


def has_not_ready_verdict(md_text: str) -> bool:
    return bool(_NOT_READY_RE.search(md_text))


def prompt_user(questions: list[Question], *, agent_label: str) -> dict[str, str]:
    """Ask each question on stdin and return ``{question_id: answer}``.

    Uses ``rich`` for formatting. If stdin is not a TTY (CI), returns an empty
    dict so callers can skip the re-invocation loop cleanly.
    """
    if not sys.stdin.isatty():
        log.info("hitl.skip_non_tty", agent=agent_label, count=len(questions))
        return {}

    console = Console()
    console.print()
    console.print(
        Panel(
            f"[bold yellow]{agent_label}[/bold yellow] needs input on "
            f"[bold]{len(questions)}[/bold] open item(s).\n"
            f"Press [bold]Enter[/bold] with no text to skip an item — the agent "
            f"will document it as a `[ASSUMPTION]` and not ask again.",
            title="Human input required",
            border_style="yellow",
        )
    )

    answers: dict[str, str] = {}
    for q in questions:
        console.print()
        console.rule(f"[bold]{q.id}[/bold] · {q.kind}", style="cyan")
        if q.context and q.context != q.prompt_text:
            console.print(f"[dim]context:[/dim] {q.context}")
        ans = Prompt.ask(f"[green]{q.prompt_text}[/green]", default="").strip()
        if ans:
            answers[q.id] = ans
    return answers


def format_answers_md(
    questions: list[Question],
    answers: dict[str, str],
    *,
    skipped: list[Question] | None = None,
) -> str:
    """Render the user's answers (and skips) as a markdown file the agent reads on rerun.

    Answered questions get an explicit answer to incorporate. Skipped questions
    instruct the agent to make a reasonable assumption, mark it with
    ``[ASSUMPTION: ...]``, and remove the original `[CLARIFICATION NEEDED]`
    tag / blocker row / open-question entry — DO NOT re-emit clarification
    requests for the same items.
    """
    skipped = skipped or []
    answered = [q for q in questions if answers.get(q.id)]

    lines = [
        "# User Answers",
        "",
        "The user has reviewed your clarification questions / blockers.",
        "",
        "- **Answered** items: incorporate the answer and remove the corresponding",
        "  `[CLARIFICATION NEEDED]` tag, blocker row, or open-question entry.",
        "- **Skipped** items: the user is OK proceeding without a definitive answer.",
        "  Make a reasonable assumption, mark it inline with `[ASSUMPTION: ...]`,",
        "  and remove the original `[CLARIFICATION NEEDED]` tag / blocker row /",
        "  open-question entry. **Do NOT re-emit `[CLARIFICATION NEEDED]` for",
        "  skipped items.**",
        "",
    ]

    if answered:
        lines.append("## Answered")
        lines.append("")
        for q in answered:
            lines.append(f"### {q.id} ({q.kind})")
            lines.append("")
            lines.append(f"**Question:** {q.prompt_text}")
            if q.context and q.context != q.prompt_text:
                lines.append("")
                lines.append(f"**Original context:** {q.context}")
            lines.append("")
            lines.append(f"**Answer:** {answers[q.id]}")
            lines.append("")

    if skipped:
        lines.append("## Skipped — Document As Assumptions")
        lines.append("")
        for q in skipped:
            lines.append(f"### {q.id} ({q.kind})")
            lines.append("")
            lines.append(f"**Question:** {q.prompt_text}")
            if q.context and q.context != q.prompt_text:
                lines.append("")
                lines.append(f"**Original context:** {q.context}")
            lines.append("")
            lines.append(
                "**Action:** Pick a reasonable default, document it inline with "
                "`[ASSUMPTION: ...]`, and remove the `[CLARIFICATION NEEDED]` "
                "tag / blocker row / open-question entry."
            )
            lines.append("")

    if not answered and not skipped:
        lines.append(
            "_No items were answered or skipped — proceed with reasonable "
            "assumptions and document them._"
        )

    return "\n".join(lines) + "\n"


def write_answers_file(
    workdir: Path,
    questions: list[Question],
    answers: dict[str, str],
    *,
    skipped: list[Question] | None = None,
    filename: str = "user-answers.md",
) -> Path:
    """Write the answers markdown into ``workdir`` and return the path."""
    path = workdir / filename
    path.write_text(
        format_answers_md(questions, answers, skipped=skipped),
        encoding="utf-8",
    )
    return path
