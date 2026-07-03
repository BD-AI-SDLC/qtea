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
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from qtea.logging_setup import get_logger
from qtea.md_parser import extract_bullets, extract_tables, parse_markdown

log = get_logger(__name__)

_CLARIFICATION_RE = re.compile(r"\[CLARIFICATION\s+NEEDED:\s*([^\]]+)\]", re.IGNORECASE)
_NOT_READY_RE = re.compile(r"\bNOT\s+READY\b", re.IGNORECASE)
_XREF_BLOCKER_RE = re.compile(
    r"\s*(?:—|--|–|-)\s*see\s+(?:blocker\s+#?|block-)(\d+)", re.IGNORECASE
)
_BLOCKER_XREF_ONLY_RE = re.compile(
    r"^(?:exact\s+copy\s+)?(?:per|see|ref|from|same\s+as)\s+BLOCK-(\d+)\.?$",
    re.IGNORECASE,
)
_BLOCKER_PREFIX = "How should we resolve this blocker: "
_TC_ID_RE = re.compile(r"TC-[A-Z]+-\d+", re.IGNORECASE)
_AC_ID_RE = re.compile(r"\bAC(?:-[A-Z0-9]+)+\b", re.IGNORECASE)

# Text the user can type to mean "skip this — drop from output". Without
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


# Negative / scope-exclusion phrases that LOOK like answers but mean "don't
# test this aspect". The user types "there is no aria-label" or "mobile
# isn't in scope" expecting the agent to drop the corresponding coverage,
# but without this detection the agent receives the raw text as a literal
# answer and threads it into the plan. Conservative on purpose: requires a
# noun token after "no" / "don't have" so bare "no" / "none" / "no special
# characters allowed" don't trigger. Matches always go through a
# confirmation prompt in `prompt_user` — never auto-applied.
# The trailing ``(?:\s+\S+){0,3}`` after each noun anchor allows multi-word
# topic phrases ("analytics SDK", "aria-label text") without losing the
# end-anchor guard — the input must still wrap up cleanly after at most a
# handful of additional tokens, so verbose prose ("mobile isn't in scope
# yet but will be next quarter") still falls through.
_NEGATIVE_DROP_RE = re.compile(
    r"""^(?:
        there\s+is\s+no\s+\S+(?:\s+\S+){0,3}
      | there\s+are\s+no\s+\S+(?:\s+\S+){0,3}
      | (?:we|i)\s+(?:don[''']?t|do\s+not)\s+have\s+
          (?:an?\s+|any\s+)?\S+(?:\s+\S+){0,3}
      | no\s+such\s+\S+(?:\s+\S+){0,3}
      | (?:\S+\s+){1,3}(?:does\s+not|doesn[''']?t)\s+exist
      | (?:we|the\s+app|the\s+page|the\s+product)\s+
          (?:do(?:n[''']?t|esn[''']?t)|do\s+not)\s+
          (?:use|have|support)\s+\S+(?:\s+\S+){0,3}
      | (?:\S+\s+){1,3}(?:isn[''']?t|is\s+not|aren[''']?t|are\s+not)\s+
          (?:in\s+scope|applicable|relevant|tested|supported)
      | (?:not|out\s+of)\s+(?:in\s+)?scope(?:\s*[:\-,]\s*\S+(?:\s+\S+){0,3})?
      | (?:skip(?:ping)?|drop|exclude|omit)\s+\S+(?:\s+\S+){0,3}
      | not\s+testing\s+\S+(?:\s+\S+){0,3}
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


def looks_like_negative_drop_intent(text: str) -> tuple[bool, str | None]:
    """Detect negative / scope-exclusion phrases like "there is no aria-label"
    or "mobile isn't in scope".

    Returns ``(matched, phrase)`` — *phrase* is the trimmed input on a match,
    used to populate the confirmation prompt in ``prompt_user`` so the user
    sees exactly what we interpreted as an exclusion. ``(False, None)`` on
    no match. Bare ``"no"`` / ``"none"`` / ``"no special characters allowed"``
    do NOT match — the pattern requires a noun token after the negation.
    """
    stripped = text.strip()
    if _NEGATIVE_DROP_RE.match(stripped):
        return True, stripped
    return False, None


# Resolution values stored on ``HitlDecision.resolution`` and persisted to
# the on-disk ledger. Strings (not Enum) so the JSONL format stays flat and
# resumable across versions.
#
# - ``answered``        — user typed a literal answer to incorporate.
# - ``skipped_drop``    — user pressed Enter / typed "skip" — agent drops
#                         the corresponding AC/TC and records it in
#                         ``## Coverage Notes``.
# - ``scope_exclusion`` — user's answer was a scope-exclusion ("mobile
#                         isn't in scope"); agent removes the named scope
#                         from coverage and keeps the rest.
# - ``skipped``         — LEGACY pre-rework value. Ledger entries written
#                         before the skip-as-drop change carried this; we
#                         continue to render them with the old
#                         ``[ASSUMPTION]`` framing so a resumed run honors
#                         the contract the user answered under.
RESOLUTION_ANSWERED = "answered"
RESOLUTION_SKIPPED_DROP = "skipped_drop"
RESOLUTION_SCOPE_EXCLUSION = "scope_exclusion"
RESOLUTION_SKIPPED_LEGACY = "skipped"
# Overlay-dismiss HITL resolutions — carried alongside the standard answer
# tuple so the parent-side handler (s09_execute._hitl_overlay_sweep) can
# route accepts / one-shots / bug flags without duplicating the answer
# schema. See :mod:`qtea.overlay_handling` for wire format.
RESOLUTION_OVERLAY_PERSIST = "overlay_persist"
RESOLUTION_OVERLAY_ONCE = "overlay_once"
RESOLUTION_OVERLAY_BUG = "overlay_bug"


@dataclass
class Question:
    """A single unresolved question surfaced to the user."""

    id: str
    kind: str  # "clarification" | "blocker" | "open_question" | "overlay_dismiss"
    prompt_text: str
    context: str = ""
    # Optional structured payload for kinds that need more than text (e.g.
    # ``overlay_dismiss`` carries screenshot_path + candidate list). Flows
    # untouched through the HITL bridge into the UI dialog; CLI renderer
    # branches on ``metadata["type"]`` to choose its rendering.
    metadata: dict = field(default_factory=dict)


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
    "things", "item", "items", "exact", "specific", "given", "able",
    "available", "possible", "required", "anything",
    "something", "still", "unconfirmed",
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
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(HitlDecision.from_jsonable(json.loads(stripped)))
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
    Per-entry directive depends on the resolution: answered items apply
    verbatim; ``skipped_drop`` and ``scope_exclusion`` propagate the
    drop / exclusion (no ``[ASSUMPTION]``); legacy ``skipped`` entries
    (pre-rework runs) preserve the old assumption framing the user
    originally agreed to.
    """
    if not ledger:
        return ""
    lines = [
        "# Prior Decisions (do NOT re-raise)",
        "",
        "The user has already addressed the following items earlier in this",
        "run. Treat each one as final. **Do NOT re-emit any of these as new",
        "blockers, clarifications, or open questions** — even when the test",
        "case you are designing would benefit from more detail. Each entry",
        "below carries a per-item directive on how to honor the prior",
        "decision (incorporate, drop, exclude scope, or apply legacy",
        "assumption).",
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
        if entry.resolution == RESOLUTION_ANSWERED:
            lines.append(f"**User answer:** {entry.answer}")
        elif entry.resolution == RESOLUTION_SKIPPED_DROP:
            lines.append(
                "**User chose to skip.** DROP the corresponding AC / TC "
                "from your output and record the drop in a "
                "`## Coverage Notes` section. Do NOT write `[ASSUMPTION]`. "
                "Do not re-ask."
            )
        elif entry.resolution == RESOLUTION_SCOPE_EXCLUSION:
            lines.append(
                f"**User excluded a scope.** Their answer: `{entry.answer}`. "
                f"Remove ACs / TCs / sub-bullets that depend on the "
                f"excluded scope. Record the exclusion in "
                f"`## Coverage Notes`. Do not re-ask."
            )
        elif entry.resolution == RESOLUTION_SKIPPED_LEGACY:
            lines.append(
                "**User skipped (legacy pre-rework contract).** Apply the "
                "same conservative assumption framing (`[ASSUMPTION: ...]`) "
                "the earlier agent used. Do not re-ask."
            )
        else:
            lines.append(
                "**Unknown resolution.** Do not re-ask this item."
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

    # Pass 1: cross-references to existing blockers ("see blocker #N",
    # "per BLOCK-N", "exact copy per BLOCK-N", etc.)
    for q in qs:
        if q.kind != "blocker":
            m = _XREF_BLOCKER_RE.search(q.prompt_text)
            if m:
                ref_num = int(m.group(1))
                if ref_num in blocker_by_num:
                    xref_ids.add(q.id)
                    continue
            m2 = _BLOCKER_XREF_ONLY_RE.match(q.prompt_text.strip())
            if m2:
                ref_num2 = int(m2.group(1))
                if ref_num2 in blocker_by_num:
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


def _resolve_blocker_xref(
    text: str, blockers: list[Question],
) -> str | None:
    """If *text* is a cross-reference like ``per BLOCK-003``, return the
    referenced blocker's ``prompt_text`` so the clarification carries the real
    question.  Returns ``None`` when *text* is not a cross-reference.
    """
    m = _BLOCKER_XREF_ONLY_RE.match(text)
    if not m:
        return None
    ref_num = int(m.group(1))
    for b in blockers:
        if b.kind == "blocker" and b.id == f"BLOCK-{ref_num:02d}":
            return b.prompt_text
    # Referenced blocker not found — keep original text so the user at least
    # sees *something* rather than a silent drop.
    return None


def _extract_clarifications(
    md_text: str, blockers: list[Question] | None = None,
) -> list[Question]:
    out: list[Question] = []
    for idx, m in enumerate(_CLARIFICATION_RE.finditer(md_text), start=1):
        text = m.group(1).strip()
        if blockers:
            resolved = _resolve_blocker_xref(text, blockers)
            if resolved is not None:
                text = resolved
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
    blockers = _extract_blockers(md_text)
    qs: list[Question] = []
    qs.extend(_extract_clarifications(md_text, blockers=blockers))
    qs.extend(blockers)
    qs.extend(_extract_open_questions(md_text))
    return _dedup(qs)


def has_not_ready_verdict(md_text: str) -> bool:
    return bool(_NOT_READY_RE.search(md_text))


def _prompt_overlay_dismiss(
    console: Console, q: Question,
) -> tuple[str | None, str]:
    """CLI-side rendering for an overlay-dismiss HITL question.

    Returns ``(resolution, json_answer)`` where ``resolution`` is one of
    :data:`RESOLUTION_OVERLAY_PERSIST` / :data:`RESOLUTION_OVERLAY_ONCE` /
    :data:`RESOLUTION_OVERLAY_BUG`, or ``None`` when the user skips (empty
    input / abort). ``json_answer`` is the wire format described in
    :func:`qtea.overlay_handling.parse_overlay_answer`.

    The dialog surfaces the cropped overlay screenshot (path only in CLI —
    terminals can't render images reliably; the UI dialog does the inline
    ``ft.Image`` render) plus:
      - AOM-extracted candidates (safe class listed first, risky class
        marked ``[risky — verify]``)
      - Press-Escape option
      - Custom locator (``role=button, name=<text>``)
      - "This is a real bug" — fail the test class
    """
    meta = q.metadata or {}
    test_id = meta.get("test_id") or "(unknown test)"
    overlay_role = meta.get("overlay_role") or "?"
    overlay_name = meta.get("overlay_name") or "?"
    page_url = meta.get("page_url") or ""
    screenshot = meta.get("screenshot_path") or ""
    candidates = list(meta.get("candidates") or [])

    console.print()
    console.print(
        Panel(
            f"[bold]Test:[/bold] {test_id}\n"
            f"[bold]Overlay:[/bold] role={overlay_role!r}  "
            f"name={overlay_name!r}\n"
            f"[bold]Page:[/bold] {page_url}\n"
            f"[bold]Screenshot:[/bold] {screenshot or '(none)'}",
            title=f"[bold yellow]Overlay blocked action:[/bold yellow] "
                  f"{meta.get('target_intent') or '(unknown target)'}",
            border_style="yellow",
        )
    )

    console.print()
    if candidates:
        console.print("[bold]Dismiss candidates (from overlay AOM):[/bold]")
        # Safe candidates first — matches the dialog's persist-friendly
        # ordering. Numbering is 1-based for humans.
        ordered: list[tuple[int, dict]] = []
        for i, c in enumerate(candidates):
            ordered.append((i, c))
        ordered.sort(key=lambda p: (not p[1].get("safe"), -int(p[1].get("score") or 0)))
        for display_num, (orig_idx, c) in enumerate(ordered, start=1):
            role = c.get("role") or "?"
            name = c.get("name") or "?"
            safe_tag = "" if c.get("safe") else "  [yellow][risky — verify][/yellow]"
            console.print(
                f"  [cyan]{display_num}[/cyan]. Click {role} [green]{name!r}[/green]{safe_tag}"
            )
            # Remember the original index for the wire format.
            c["_display_num"] = display_num
            c["_orig_idx"] = orig_idx
        next_num = len(ordered) + 1
    else:
        console.print("[dim](no button candidates extracted from AOM)[/dim]")
        ordered = []
        next_num = 1

    esc_num = next_num
    custom_num = esc_num + 1
    bug_num = custom_num + 1
    skip_num = bug_num + 1
    console.print(f"  [cyan]{esc_num}[/cyan]. Press Escape key")
    console.print(f"  [cyan]{custom_num}[/cyan]. Custom locator (provide role + name)")
    console.print(f"  [cyan]{bug_num}[/cyan]. This is a real bug — fail the test")
    console.print(f"  [cyan]{skip_num}[/cyan]. Skip (do nothing this run)")
    console.print()

    valid_nums = [str(i) for i in range(1, skip_num + 1)]
    choice_str = Prompt.ask(
        "[green]Pick a number[/green]",
        choices=valid_nums,
        default=str(skip_num),
    ).strip()
    try:
        choice = int(choice_str)
    except ValueError:
        return None, ""

    if choice == skip_num:
        return None, ""

    if choice == bug_num:
        return RESOLUTION_OVERLAY_BUG, json.dumps({"kind": "bug"})

    if choice == esc_num:
        answer = json.dumps({"kind": "press_escape"})
        return _confirm_persist(console, answer)

    if choice == custom_num:
        role = Prompt.ask(
            "[green]Custom locator role[/green]",
            choices=["button", "link", "menuitem"],
            default="button",
        ).strip()
        name = Prompt.ask("[green]Custom locator accessible name[/green]", default="").strip()
        if not name:
            console.print("[dim](no name supplied — skipping)[/dim]")
            return None, ""
        answer = json.dumps({"kind": "custom", "role": role, "name": name})
        return _confirm_persist(console, answer)

    # Candidate click
    for _display_num, (_orig_idx, c) in enumerate(ordered, start=1):
        if c.get("_display_num") == choice:
            answer = json.dumps({
                "kind": "click_candidate",
                "candidate_index": c["_orig_idx"],
            })
            return _confirm_persist(console, answer)
    return None, ""


def _confirm_persist(console: Console, answer: str) -> tuple[str, str]:
    """Ask whether to persist this overlay entry to interceptors.json."""
    persist = Prompt.ask(
        "[bold]Persist to interceptors.json so future runs are clean?[/bold] "
        "[dim](y = yes, n = one-shot only)[/dim]",
        choices=["y", "n"],
        default="y",
    ).strip()
    resolution = (
        RESOLUTION_OVERLAY_PERSIST if persist == "y" else RESOLUTION_OVERLAY_ONCE
    )
    return resolution, answer


def prompt_user(
    questions: list[Question], *, agent_label: str
) -> dict[str, tuple[str, str]]:
    """Ask each question on stdin and return ``{question_id: (resolution, answer)}``.

    *resolution* is one of :data:`RESOLUTION_ANSWERED` /
    :data:`RESOLUTION_SCOPE_EXCLUSION`. ``RESOLUTION_SKIPPED_DROP`` items are
    NOT included in the return dict — callers detect them by their absence
    from the answer set (matching the historical ``id not in answers``
    pattern). *answer* is the user's typed text (for ANSWERED items) or the
    typed text we interpreted as an exclusion (for SCOPE_EXCLUSION items).

    Two-step UX per question:

    1. Ask the question. Empty input / :func:`looks_like_skip_intent` →
       skipped (omitted from return dict).
    2. If :func:`looks_like_negative_drop_intent` flags the typed answer
       (e.g. "there is no aria-label", "mobile isn't in scope"), show a
       one-line confirmation — default Y interprets as a scope-exclusion;
       N keeps the typed text as a literal answer; E re-prompts so the user
       can retype.

    Returns an empty dict when stdin is not a TTY (CI) so callers can skip
    the re-invocation loop cleanly.
    """
    if not sys.stdin.isatty() and not os.environ.get("QTEA_UI_MODE"):
        log.info("hitl.skip_non_tty", agent=agent_label, count=len(questions))
        return {}

    console = Console()
    console.print()
    console.print(
        Panel(
            f"[bold yellow]{agent_label}[/bold yellow] needs input on "
            f"[bold]{len(questions)}[/bold] open item(s).\n"
            f"Type your answer, or press [bold]Enter[/bold] to skip — "
            f"skipped items are [bold]dropped from the output[/bold] "
            f"(no assumption made).",
            title="Human input required",
            border_style="yellow",
        )
    )

    answers: dict[str, tuple[str, str]] = {}
    for q in questions:
        console.print()
        console.rule(f"[bold]{q.id}[/bold] · {q.kind}", style="cyan")
        if q.context and q.context != q.prompt_text:
            console.print(f"[dim]context:[/dim] {q.context}")

        # Overlay-dismiss branch — bypass the free-text loop. Shows the
        # cropped screenshot path, lists AOM-extracted candidates + Escape
        # + custom + bug options, and returns a JSON-encoded answer plus a
        # per-choice resolution the parent-side handler routes on.
        if q.metadata and q.metadata.get("type") == "overlay_dismiss":
            resolution, ans = _prompt_overlay_dismiss(console, q)
            if resolution is not None:
                answers[q.id] = (resolution, ans)
            continue

        while True:
            ans = Prompt.ask(f"[green]{q.prompt_text}[/green]", default="").strip()
            if not ans:
                break
            if looks_like_skip_intent(ans):
                console.print(
                    f"[dim]→ recognized as skip intent ({ans!r}); "
                    f"dropping like an empty answer.[/dim]"
                )
                log.info("hitl.skip_intent_text", q_id=q.id, raw=ans)
                break

            matched, phrase = looks_like_negative_drop_intent(ans)
            if matched:
                console.print(
                    f"[yellow]Looks like you want to exclude "
                    f"'{phrase}' from coverage.[/]"
                )
                choice = Prompt.ask(
                    r"[bold]Proceed?[/] [dim](\[y\]es-drop / "
                    r"\[n\]o-keep-as-answer / \[e\]dit-retype)[/]",
                    choices=["y", "n", "e"],
                    default="y",
                    show_choices=False,
                )
                if choice == "y":
                    answers[q.id] = (RESOLUTION_SCOPE_EXCLUSION, ans)
                    log.info(
                        "hitl.scope_exclusion_confirmed",
                        q_id=q.id,
                        phrase=phrase,
                    )
                    break
                if choice == "n":
                    answers[q.id] = (RESOLUTION_ANSWERED, ans)
                    break
                # 'e' falls through to re-prompt
                console.print("[dim]→ retype your answer[/]")
                continue

            answers[q.id] = (RESOLUTION_ANSWERED, ans)
            break
    return answers


def format_answers_md(
    questions: list[Question],
    answers: dict[str, tuple[str, str]] | dict[str, str],
    *,
    skipped: list[Question] | None = None,
    ledger_resolved: list[tuple[Question, HitlDecision]] | None = None,
) -> str:
    """Render the user's answers (and skips) as a markdown file the agent reads on rerun.

    The ``answers`` dict maps ``question_id`` to either ``(resolution, answer)``
    (the new :func:`prompt_user` shape) or a bare ``answer`` string (legacy
    callers — treated as ``RESOLUTION_ANSWERED``). Skipped items are passed
    through the ``skipped`` list and rendered as drop directives (no
    ``[ASSUMPTION]``). Scope-exclusion items live in the ``answers`` dict
    with resolution ``scope_exclusion`` and get their own section.

    ``ledger_resolved`` carries questions the agent emitted in this round that
    matched a prior-step decision in the run's HITL ledger. Each entry's
    directive depends on the prior decision's resolution — answered items
    apply verbatim; ``skipped_drop`` and ``scope_exclusion`` propagate the
    drop / exclusion; legacy ``skipped`` entries (pre-rework runs) preserve
    the old ``[ASSUMPTION]`` framing the user originally agreed to.
    """
    skipped = skipped or []
    ledger_resolved = ledger_resolved or []
    answered_items: list[tuple[Question, str]] = []
    scope_excluded_items: list[tuple[Question, str]] = []
    for q in questions:
        raw = answers.get(q.id)
        if raw is None:
            continue
        if isinstance(raw, tuple):
            resolution, text = raw
        else:
            resolution, text = RESOLUTION_ANSWERED, raw
        if resolution == RESOLUTION_SCOPE_EXCLUSION:
            scope_excluded_items.append((q, text))
        else:
            answered_items.append((q, text))

    lines = [
        "# User Answers",
        "",
        "The user has reviewed your clarification questions / blockers.",
        "",
        "- **Answered** items: incorporate the answer and remove the corresponding",
        "  `[CLARIFICATION NEEDED]` tag, blocker row, or open-question entry.",
        "- **Skipped** items: the user has chosen NOT to specify this. "
        "**Drop** the",
        "  corresponding AC / TC / sub-item entirely from the output and record",
        "  the drop in a `## Coverage Notes` section at the end of the document.",
        "  **Do NOT** write `[ASSUMPTION: ...]` — the user's intent is to remove",
        "  this coverage, not to test it under an invented value.",
        "- **Scope-excluded** items: the user's answer names a scope to exclude",
        "  (e.g. \"mobile isn't in scope\"). Remove ACs / TCs / sub-bullets that",
        "  depend on the excluded scope; keep the in-scope portions. Record the",
        "  exclusion in `## Coverage Notes`.",
        "- **Previously resolved** items: the user already addressed the same",
        "  question in an earlier step. Apply that prior decision verbatim —",
        "  do NOT re-raise the question to the user.",
        "",
        "Preserve the `## Coverage Notes` section verbatim across iterations —",
        "only append new entries; never delete existing ones.",
        "",
    ]

    if answered_items:
        lines.append("## Answered")
        lines.append("")
        for q, text in answered_items:
            lines.append(f"### {q.id} ({q.kind})")
            lines.append("")
            lines.append(f"**Question:** {q.prompt_text}")
            if q.context and q.context != q.prompt_text:
                lines.append("")
                lines.append(f"**Original context:** {q.context}")
            lines.append("")
            lines.append(f"**Answer:** {text}")
            lines.append("")

    if skipped:
        lines.append("## Skipped — Drop From Output")
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
                "**Action:** Remove the `[CLARIFICATION NEEDED]` tag AND the "
                "entire AC / TC / sub-item the question was attached to from "
                "the output. Append an entry to `## Coverage Notes` at the end "
                "of the document recording the dropped ID and the reason "
                "(e.g. `AC-7: dropped — user skipped clarification on the "
                "expected aria-label text`). **Do NOT** write "
                "`[ASSUMPTION: ...]`."
            )
            lines.append("")

    if scope_excluded_items:
        lines.append("## Scope Exclusions — Drop Excluded Items")
        lines.append("")
        for q, text in scope_excluded_items:
            lines.append(f"### {q.id} ({q.kind})")
            lines.append("")
            lines.append(f"**Question:** {q.prompt_text}")
            if q.context and q.context != q.prompt_text:
                lines.append("")
                lines.append(f"**Original context:** {q.context}")
            lines.append("")
            lines.append(
                f"**User's answer (interpret as scope-exclusion, NOT a "
                f"literal value):** {text}"
            )
            lines.append("")
            lines.append(
                "**Action:** Identify the scope the user is excluding (the "
                "named element / platform / locale / feature) and REMOVE any "
                "ACs / TCs / sub-bullets that depend solely on the excluded "
                "scope. Keep the in-scope portions intact. Append an entry to "
                "`## Coverage Notes` recording the exclusion and the user's "
                "exact answer."
            )
            lines.append("")

    if ledger_resolved:
        lines.append("## Previously Resolved — Apply Prior Decision")
        lines.append("")
        for q, prior in ledger_resolved:
            lines.append(
                f"### {q.id} ({q.kind}) — matches {prior.question_id} "
                f"from step {prior.step}"
            )
            lines.append("")
            lines.append(f"**This-round question:** {q.prompt_text}")
            lines.append("")
            lines.append(f"**Prior question:** {prior.question_text}")
            lines.append("")
            if prior.resolution == RESOLUTION_ANSWERED:
                lines.append(f"**Prior answer (apply verbatim):** {prior.answer}")
            elif prior.resolution == RESOLUTION_SKIPPED_DROP:
                lines.append(
                    "**Prior resolution:** user skipped — DROP the "
                    "corresponding AC / TC from the output and add a "
                    "`## Coverage Notes` entry. Do NOT re-raise; do NOT "
                    "write `[ASSUMPTION]`."
                )
            elif prior.resolution == RESOLUTION_SCOPE_EXCLUSION:
                lines.append(
                    f"**Prior resolution:** scope-exclusion — user answered "
                    f"`{prior.answer}`. Remove the excluded scope from "
                    f"coverage and add a `## Coverage Notes` entry. Do NOT "
                    f"re-raise."
                )
            elif prior.resolution == RESOLUTION_SKIPPED_LEGACY:
                # Pre-rework ledger entry — preserve old assumption framing.
                lines.append(
                    "**Prior resolution:** user skipped under the legacy "
                    "(pre-rework) contract — apply the same "
                    "`[ASSUMPTION: ...]` framing the earlier agent used. "
                    "Do NOT re-raise."
                )
            else:
                lines.append(
                    "**Prior resolution:** unknown — do NOT re-raise this "
                    "item to the user."
                )
            lines.append("")

    if (
        not answered_items
        and not skipped
        and not scope_excluded_items
        and not ledger_resolved
    ):
        lines.append(
            "_No items were answered, skipped, or excluded — return the "
            "document unchanged._"
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
