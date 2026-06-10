"""Human-in-the-loop: detect agent clarification requests and prompt the user.

After steps 2 (refine) and 3 (plan), agents may emit unresolved
``[CLARIFICATION NEEDED: ...]`` tags or list blockers/open questions. This
module surfaces those to the user via the CLI, collects answers, and packages
them into a markdown file the agent can read on the next invocation.

It also carries a **cross-step HITL ledger** through the run so a question
the user already addressed in an earlier step is never re-asked when a
later agent paraphrases it. The ledger lives on ``ctx.extras["hitl_ledger"]``
in memory and is mirrored to ``<workspace>/.hitl-ledger.jsonl`` for resume.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
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
_AC_ID_RE = re.compile(r"\bAC-\d+\b", re.IGNORECASE)

# Text the user can type to mean "skip this — make an assumption". Without
# this, "i'm not sure" / "skip" / "n/a" are treated as literal answers and
# the agent dutifully threads them into the spec, which then causes the next
# step's agent to re-raise the same gap as a fresh blocker.
_SKIP_INTENT_RE = re.compile(
    r"""^(?:
        skip(?:\s+(?:this|it|that|me|please))?
        | n\s*/\s*a
        | na
        | none
        | nope
        | idk
        | i\s*don[''']?t\s*know
        | i\s*do\s*not\s*know
        | dunno
        | unknown
        | unsure
        | not\s+sure
        | i[''']?m\s+not\s+sure(?:[.,\s]+skip(?:\s+this)?)?
        | no\s+idea
        | pass
    )[.!\s]*$""",
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_skip_intent(text: str) -> bool:
    """True when the user's typed text is a polite stand-in for skip-empty-Enter.

    Why: users naturally type ``skip`` / ``n/a`` / ``i'm not sure`` instead
    of hitting Enter with no content. Treating that literally pollutes the
    refined spec with "the user answered: i'm not sure", which downstream
    agents then re-raise as fresh blockers.
    """
    return bool(_SKIP_INTENT_RE.match(text.strip()))


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


# ---------------------------------------------------------------------------
# Cross-step HITL ledger: paraphrase-aware "already asked" memory
# ---------------------------------------------------------------------------

# Tokens too generic to be useful for paraphrase detection. Keep the list
# short — every entry is a paraphrase we'll fail to detect when it's the
# only thing two questions share. Only words that are truly meaning-free in
# this context belong here.
_STOPWORDS: frozenset[str] = frozenset({
    "about", "above", "across", "after", "against", "along", "also", "among",
    "another", "around", "because", "been", "before", "being", "below",
    "between", "both", "could", "does", "doing", "during", "each", "either",
    "every", "from", "have", "having", "into", "just", "less", "like",
    "more", "most", "much", "must", "neither", "only", "other", "over",
    "should", "since", "some", "such", "than", "that", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "towards",
    "under", "until", "upon", "very", "what", "when", "where", "which",
    "while", "whose", "with", "within", "without", "would", "your",
    # HITL-domain noise — every other prompt contains these:
    "blocker", "blockers", "clarification", "needed", "question", "questions",
    "answer", "answers", "value", "values", "field", "fields", "thing",
    "things", "item", "items", "exact", "specific", "given", "when", "then",
    "able", "available", "possible", "needed", "required", "anything",
    "something", "still", "without", "unconfirmed",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-./@]*")


def distinctive_tokens(text: str) -> frozenset[str]:
    """Meaning-bearing tokens from ``text`` for paraphrase matching.

    Rules: lower-cased; length ≥ 4 OR contains a digit (tech identifiers
    like ``ga4`` / ``oauth2`` / ``s3``); not a stopword. Keeps tokens with
    interior punctuation (``gtag.js``, ``@google-analytics/ga4``) intact
    so the most distinctive parts of a technical question survive.
    """
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        t = m.group(0).lower()
        if t in _STOPWORDS:
            continue
        if len(t) >= 4 or any(c.isdigit() for c in t):
            out.add(t)
    return frozenset(out)


def _is_tech_identifier(tok: str) -> bool:
    """A token that's almost certainly NOT a coincidence between two unrelated
    questions: digits or interior punctuation (``gtag.js``, ``ga4``,
    ``google-analytics/ga4``, ``oauth2``). When two questions share two or
    more of these, they're talking about the same technical thing."""
    return any(c.isdigit() or c in "./-@_" for c in tok)


def _looks_like_paraphrase(
    a_tokens: frozenset[str], b_tokens: frozenset[str]
) -> bool:
    """True when two distinctive-token sets share enough specific terms to
    be considered paraphrases of the same question.

    Two independent signals — either triggers a match:

    1. **Tech-identifier co-occurrence.** ≥ 2 shared tokens that contain
       digits or interior punctuation (``gtag.js``, ``google-analytics/ga4``,
       ``ga4``, ``oauth2``). These don't collide by chance between unrelated
       questions, so two in common is essentially proof.

    2. **Overlap coefficient.** ``|A∩B| / min(|A|,|B|) ≥ 0.5`` with ≥ 3
       shared tokens, using the smaller side as denominator because a
       verbose question (with context column attached) and its terse
       paraphrase differ a lot in size — Jaccard punishes that even when
       every meaning-bearing term in the shorter side appears in the
       longer one.

    Conservative on purpose: false positives silently drop a real question;
    false negatives just re-prompt the user once. The latter is recoverable.
    """
    if len(a_tokens) < 3 or len(b_tokens) < 3:
        return False
    inter = a_tokens & b_tokens
    if len(inter) < 3:
        return False
    if sum(1 for t in inter if _is_tech_identifier(t)) >= 2:
        return True
    overlap = len(inter) / min(len(a_tokens), len(b_tokens))
    return overlap >= 0.5


@dataclass
class HitlDecision:
    """One question the user already addressed earlier in this run.

    Stored on ``ctx.extras["hitl_ledger"]`` (in-memory list) AND appended
    to ``<workspace>/.hitl-ledger.jsonl`` so resumed runs (``--from-step``)
    pick up the same ledger and don't re-prompt the user.
    """

    step: int
    agent_label: str
    question_id: str
    question_text: str
    question_kind: str
    resolution: str  # "answered" | "skipped"
    answer: str = ""
    context: str = ""
    tokens: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_question(
        cls,
        q: Question,
        *,
        step: int,
        agent_label: str,
        resolution: str,
        answer: str = "",
    ) -> HitlDecision:
        return cls(
            step=step,
            agent_label=agent_label,
            question_id=q.id,
            question_text=q.prompt_text,
            question_kind=q.kind,
            resolution=resolution,
            answer=answer,
            context=q.context,
            tokens=distinctive_tokens(f"{q.prompt_text} {q.context or ''}"),
        )

    def to_jsonable(self) -> dict:
        d = asdict(self)
        d["tokens"] = sorted(self.tokens)
        return d

    @classmethod
    def from_jsonable(cls, d: dict) -> HitlDecision:
        d = dict(d)
        d["tokens"] = frozenset(d.get("tokens") or [])
        return cls(**d)


def find_prior_decision(
    q: Question, ledger: list[HitlDecision]
) -> HitlDecision | None:
    """Match ``q`` against earlier decisions in this run.

    Returns the matching entry (oldest match wins) or ``None``. Match if
    EITHER the normalized text key is equal OR the distinctive-token sets
    look like paraphrases per :func:`_looks_like_paraphrase`.
    """
    if not ledger:
        return None
    q_key = question_key(q)
    q_tokens = distinctive_tokens(f"{q.prompt_text} {q.context or ''}")
    for entry in ledger:
        if _normalize_question_text(entry.question_text) == q_key:
            return entry
        if _looks_like_paraphrase(q_tokens, entry.tokens):
            return entry
    return None


def ledger_path(workspace_root: Path) -> Path:
    """Per-run ledger file location."""
    return workspace_root / ".hitl-ledger.jsonl"


def load_ledger(workspace_root: Path) -> list[HitlDecision]:
    """Read the on-disk ledger so resumed runs (``--from-step``) don't lose
    cross-step memory."""
    path = ledger_path(workspace_root)
    if not path.exists():
        return []
    out: list[HitlDecision] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(HitlDecision.from_jsonable(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                log.warning("hitl.ledger_line_corrupt", error=str(e))
    except OSError as e:
        log.warning("hitl.ledger_read_failed", path=str(path), error=str(e))
    return out


def append_ledger(workspace_root: Path, decisions: list[HitlDecision]) -> None:
    """Append ``decisions`` to the on-disk ledger (one JSON object per line)."""
    if not decisions:
        return
    path = ledger_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            for d in decisions:
                fp.write(json.dumps(d.to_jsonable(), ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("hitl.ledger_write_failed", path=str(path), error=str(e))


def render_prior_decisions_md(ledger: list[HitlDecision]) -> str:
    """Render the ledger as a markdown block the next agent reads as context.

    The agent is instructed to treat each entry as final — do not re-raise.
    For skipped items, the agent should propagate the same ``[ASSUMPTION]``
    framing the prior agent used.
    """
    if not ledger:
        return ""
    lines = [
        "# Prior Decisions (do NOT re-raise)",
        "",
        "The user has already addressed the following items earlier in this",
        "run. Treat each one as final. **Do NOT re-emit any of these as new",
        "blockers, clarifications, or open questions** — even when the test",
        "case you are designing would benefit from more detail. For items",
        "marked _Skipped_, apply the same conservative assumption the prior",
        "agent used and proceed.",
        "",
    ]
    for entry in ledger:
        lines.append(f"## {entry.question_id} (step {entry.step}, {entry.resolution})")
        lines.append("")
        lines.append(f"**Question:** {entry.question_text}")
        if entry.context and entry.context != entry.question_text:
            lines.append("")
            lines.append(f"**Original context:** {entry.context}")
        lines.append("")
        if entry.resolution == "answered":
            lines.append(f"**User answer:** {entry.answer}")
        else:
            lines.append(
                "**User chose to skip.** Apply a reasonable default and "
                "document it inline with `[ASSUMPTION: ...]`. Do not re-ask."
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def resolve_against_ledger(
    questions: list[Question], ledger: list[HitlDecision]
) -> tuple[list[Question], list[tuple[Question, HitlDecision]]]:
    """Split ``questions`` into (novel, ledger-resolved) pairs.

    ``novel`` should be put to the user; ``ledger-resolved`` already have an
    answer or skip in the ledger and the agent gets told to apply it.
    """
    novel: list[Question] = []
    resolved: list[tuple[Question, HitlDecision]] = []
    for q in questions:
        match = find_prior_decision(q, ledger)
        if match is None:
            novel.append(q)
        else:
            resolved.append((q, match))
    return novel, resolved


_KIND_PRIORITY = {"blocker": 0, "open_question": 1, "clarification": 2}


def _extract_tc_ids(q: Question) -> set[str]:
    """Extract test-case IDs (e.g. TC-GNAV-009) from a question's text and context."""
    ids: set[str] = set()
    for text in (q.prompt_text, q.context):
        ids.update(m.upper() for m in _TC_ID_RE.findall(text))
    return ids


def _extract_ac_ids(q: Question) -> set[str]:
    """Extract acceptance-criterion IDs (e.g. AC-5) from a question's text and context."""
    ids: set[str] = set()
    for text in (q.prompt_text, q.context):
        ids.update(m.upper() for m in _AC_ID_RE.findall(text))
    return ids


def _dedup(qs: list[Question]) -> list[Question]:
    """Deduplicate questions across kinds, resolving cross-references.

    Three dedup passes run in order:
    1. "see blocker #N" — clarifications/open questions referencing a blocker
       by number are dropped.
    2. TC-ID / AC-ID overlap — non-blocker questions whose test-case or
       acceptance-criterion IDs are a subset of a blocker's affected IDs are
       dropped. Catches the step-2 case where the agent emits a blocker with
       "Affected ACs: AC-5" AND leaves `[CLARIFICATION NEEDED: ...]` inline
       on the AC-5 line — both describe the same gap; the blocker wins.
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

    # Pass 2: TC-ID / AC-ID overlap — drop non-blockers whose TCs or ACs are
    # a subset of a blocker's affected IDs. The subset check keeps it
    # conservative: a CLAR that mentions only AC-5 vs a blocker that covers
    # AC-5 → dropped; a CLAR that mentions AC-5 + AC-7 with no blocker
    # covering both → kept.
    blocker_tcs: set[str] = set()
    blocker_acs: set[str] = set()
    for q in qs:
        if q.kind == "blocker":
            blocker_tcs.update(_extract_tc_ids(q))
            blocker_acs.update(_extract_ac_ids(q))
    if blocker_tcs or blocker_acs:
        for q in qs:
            if q.kind != "blocker" and q.id not in xref_ids:
                q_tcs = _extract_tc_ids(q)
                if q_tcs and q_tcs <= blocker_tcs:
                    xref_ids.add(q.id)
                    continue
                q_acs = _extract_ac_ids(q)
                if q_acs and q_acs <= blocker_acs:
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


def _looks_like_question(text: str) -> bool:
    """A blocker entry is already an actionable question if it ends in `?`.

    Cheap heuristic — agents that follow the question-form rule write a
    direct interrogative; raw descriptions don't end in `?`.
    """
    stripped = re.sub(r"\s+", " ", text).strip().rstrip("*").rstrip()
    return stripped.endswith("?")


def _blocker_prompt(text: str) -> str:
    """Build the user-facing prompt for a blocker row.

    When the agent supplied an actionable question (ends in `?`), pass it
    through verbatim — the `"How should we resolve this blocker: "` boilerplate
    would only dilute it. Otherwise (legacy / non-conforming agent output),
    prepend the boilerplate so the user at least sees the gap framed as a
    decision request.
    """
    text = text.strip()
    if _looks_like_question(text):
        return text
    return f"{_BLOCKER_PREFIX}{text}"


def _extract_blockers(md_text: str) -> list[Question]:
    """Pick blockers out of a `## Blockers` table or bullet list.

    Column preference: ``question`` > ``description`` > ``blocker`` > first
    column. The ``question`` column is the agent's actionable interrogative
    (per the question-form rule); description is the supporting statement.
    """
    root = parse_markdown(md_text)
    sec = root.find("blocker")
    if sec is None:
        return []
    out: list[Question] = []
    for table in extract_tables(sec.content):
        if not table or len(table) < 2:
            continue
        header = [h.lower() for h in table[0]]
        prompt_idx = next(
            (i for i, h in enumerate(header) if "question" in h),
            next(
                (i for i, h in enumerate(header) if "description" in h),
                next(
                    (i for i, h in enumerate(header) if "blocker" in h),
                    0,
                ),
            ),
        )
        for row_idx, row in enumerate(table[1:], start=1):
            if row_idx > len(table) - 1:
                break
            if not row or all(not c.strip() for c in row):
                continue
            text = row[prompt_idx] if prompt_idx < len(row) else row[0]
            text = text.strip()
            if not text or text.lower().startswith("no blockers") or text == "—":
                continue
            out.append(
                Question(
                    id=f"BLOCK-{row_idx:02d}",
                    kind="blocker",
                    prompt_text=_blocker_prompt(text),
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
                    prompt_text=_blocker_prompt(text),
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
        if not ans:
            continue
        if looks_like_skip_intent(ans):
            # Treat as a skip — same effect as pressing Enter — and tell the
            # user so they know the typed phrase did NOT enter the spec.
            console.print(
                f"[dim]→ recognized as skip intent ({ans!r}); "
                f"deferring like an empty answer.[/dim]"
            )
            log.info("hitl.skip_intent_text", q_id=q.id, raw=ans)
            continue
        answers[q.id] = ans
    return answers


def format_answers_md(
    questions: list[Question],
    answers: dict[str, str],
    *,
    skipped: list[Question] | None = None,
    ledger_resolved: list[tuple[Question, HitlDecision]] | None = None,
) -> str:
    """Render the user's answers (and skips) as a markdown file the agent reads on rerun.

    Answered questions get an explicit answer to incorporate. Skipped questions
    instruct the agent to make a reasonable assumption, mark it with
    ``[ASSUMPTION: ...]``, and remove the original `[CLARIFICATION NEEDED]`
    tag / blocker row / open-question entry — DO NOT re-emit clarification
    requests for the same items.

    ``ledger_resolved`` carries questions the agent emitted in this round that
    matched a prior-step decision in the run's HITL ledger. They get rendered
    with the prior answer / skip directive so the agent silently applies the
    same resolution and drops the duplicate item.
    """
    skipped = skipped or []
    ledger_resolved = ledger_resolved or []
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
        "- **Previously resolved** items: the user already addressed the same",
        "  question in an earlier step. Apply that prior answer / assumption",
        "  verbatim — do NOT re-raise the question to the user.",
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

    if ledger_resolved:
        lines.append("## Previously Resolved — Apply Prior Decision")
        lines.append("")
        for q, prior in ledger_resolved:
            lines.append(f"### {q.id} ({q.kind}) — matches {prior.question_id} from step {prior.step}")
            lines.append("")
            lines.append(f"**This-round question:** {q.prompt_text}")
            lines.append("")
            lines.append(f"**Prior question:** {prior.question_text}")
            lines.append("")
            if prior.resolution == "answered":
                lines.append(f"**Prior answer (apply verbatim):** {prior.answer}")
            else:
                lines.append(
                    "**Prior resolution:** user skipped — apply the same "
                    "`[ASSUMPTION: ...]` framing the earlier agent used; do NOT "
                    "re-raise this item as a blocker / clarification / open question."
                )
            lines.append("")

    if not answered and not skipped and not ledger_resolved:
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
    ledger_resolved: list[tuple[Question, HitlDecision]] | None = None,
    filename: str = "user-answers.md",
) -> Path:
    """Write the answers markdown into ``workdir`` and return the path."""
    path = workdir / filename
    path.write_text(
        format_answers_md(
            questions, answers, skipped=skipped, ledger_resolved=ledger_resolved
        ),
        encoding="utf-8",
    )
    return path
