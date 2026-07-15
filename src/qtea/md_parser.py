"""Markdown -> structured-dict parser used by step post-processors.

This parser is intentionally lightweight: it walks a markdown document and
builds a tree of headings -> content. Steps that consume LLM output (`spec.md`,
`refined-spec.md`, `plan.md`, `test-design.md`, `research.md`) all share
heading-based structure, so this single parser handles their JSON projections.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from qtea.logging_setup import get_logger

log = get_logger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CODE_FENCE_RE = re.compile(r"^```")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


@dataclass
class Section:
    """A markdown section: a heading + its content + child subsections."""

    level: int
    title: str
    content: str = ""
    children: list[Section] = field(default_factory=list)

    def find(self, title_substring: str, *, case_insensitive: bool = True) -> Section | None:
        """Depth-first search for a section whose title contains the substring."""
        needle = title_substring.lower() if case_insensitive else title_substring
        stack: list[Section] = [self]
        while stack:
            s = stack.pop()
            hay = s.title.lower() if case_insensitive else s.title
            if needle in hay:
                return s
            stack.extend(reversed(s.children))
        return None

    def walk(self) -> Iterable[Section]:
        yield self
        for c in self.children:
            yield from c.walk()


def parse_markdown(text: str) -> Section:
    """Parse markdown into a Section tree rooted at a synthetic level-0 node."""
    root = Section(level=0, title="<root>")
    stack: list[Section] = [root]
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if buf:
            # Strip leading/trailing blank lines but preserve internal formatting.
            text_block = "\n".join(buf).strip("\n")
            existing = stack[-1].content
            if existing:
                stack[-1].content = (existing + "\n" + text_block).strip("\n")
            else:
                stack[-1].content = text_block
            buf.clear()

    for line in text.splitlines():
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                flush()
                level = len(m.group(1))
                title = m.group(2).strip()
                node = Section(level=level, title=title)
                # Pop until parent.level < this level.
                while stack and stack[-1].level >= level:
                    stack.pop()
                if not stack:
                    stack.append(root)
                stack[-1].children.append(node)
                stack.append(node)
                continue
        buf.append(line)

    flush()
    return root


def extract_bullets(content: str) -> list[str]:
    """Extract `- ` / `* ` / `1. ` bullet items, one per line, trimmed."""
    out: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            out.append(stripped[2:].strip())
        elif re.match(r"^\d+\.\s+", stripped):
            out.append(re.sub(r"^\d+\.\s+", "", stripped).strip())
    return out


def extract_bullet_blocks(content: str) -> list[str]:
    """Like `extract_bullets`, but folds a bullet's indented continuation lines
    into the same string with a single space. A wrapped `[requires TC: AC-N]`
    on the next physical line becomes part of the bullet, so downstream
    parsers see the full marker instead of just the first physical line."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in content.splitlines():
        if not line.strip():
            if current is not None:
                blocks.append(" ".join(current))
                current = None
            continue
        starts_bullet = not line[:1].isspace() and (
            line.lstrip().startswith(("- ", "* ", "+ "))
            or re.match(r"^\d+\.\s+", line.lstrip()) is not None
        )
        if starts_bullet:
            if current is not None:
                blocks.append(" ".join(current))
            stripped = line.lstrip()
            if stripped.startswith(("- ", "* ", "+ ")):
                current = [stripped[2:].strip()]
            else:
                current = [re.sub(r"^\d+\.\s+", "", stripped).strip()]
        elif current is not None:
            # Continuation line: either indented text OR a nested sub-bullet.
            # In both cases, fold into the current top-level bullet.
            current.append(line.strip())
    if current is not None:
        blocks.append(" ".join(current))
    return blocks


def extract_tables(content: str) -> list[list[list[str]]]:
    """Extract markdown tables as lists-of-rows-of-cells. Header row included."""
    tables: list[list[list[str]]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _TABLE_DELIM_RE.match(lines[i + 1]):
            # Collect contiguous table rows.
            rows: list[list[str]] = []
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(header)
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                rows.append(cells)
                j += 1
            tables.append(rows)
            i = j
            continue
        i += 1
    return tables


def slugify(s: str, *, prefix: str = "") -> str:
    """Conservative slug: ascii-alnum + hyphens, lowercased."""
    base = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "untitled"
    return f"{prefix}{base}" if prefix else base


# Keyword span allows `_`, `(`, `)` and spaces so qualified keywords like
# `accepted_risk` and `Dropped (accepted risk)` are captured rather than
# breaking the match (the parenthetical sits between the keyword and the
# em-dash delimiter; forbidding `(` here made the whole bullet fail to parse
# and be silently discarded).
_COVERAGE_NOTE_RE = re.compile(
    r"\*\*([^*]+?):\*\*\s*([A-Za-z][A-Za-z_()\- ]*?)\s*[—–\-]+\s*(.+)",
    re.I,
)
# Closed synonym map. Note `dropped` (a plain, unaccepted orphan drop) is
# deliberately distinct from `dropped (accepted risk)` — the parenthetical is
# the discriminator. Matching only the normalized keyword span (not free text)
# prevents a genuine coverage gap in the reason from masquerading as accepted
# risk.
_COVERAGE_RESOLUTION_MAP = {
    "dropped": "dropped",
    "drop": "dropped",
    "excluded": "scope_excluded",
    "exclude": "scope_excluded",
    "scope-excluded": "scope_excluded",
    "scope excluded": "scope_excluded",
    "accepted risk": "accepted_risk",
    "accepted-risk": "accepted_risk",
    "accepted_risk": "accepted_risk",
    "accept risk": "accepted_risk",
    "risk accepted": "accepted_risk",
    "dropped (accepted risk)": "accepted_risk",
    "drop (accepted risk)": "accepted_risk",
    "assumption": "legacy_assumption",
    "legacy assumption": "legacy_assumption",
}
# Unrecognized-but-structural keyword resolves to `dropped` (an orphan the
# downstream audit flags loudly) rather than `accepted_risk`, so parser drift
# surfaces as a visible audit failure instead of silently exempting a TC.
_COVERAGE_RESOLUTION_DEFAULT = "dropped"


def _normalize_resolution_keyword(word: str) -> str:
    """Lowercase, trim, and collapse internal whitespace for map lookup."""
    return re.sub(r"\s+", " ", word.strip().lower())


def extract_coverage_notes(root: Section) -> list[dict]:
    """Parse a top-level `## Coverage Notes` section into structured entries.

    Format expected:
      ## Coverage Notes
      - **AC-7:** Dropped — user skipped clarification on aria-label text.
      - **<Topic>:** Excluded — user said "<exact answer>"
      - **TC-14:** Dropped (accepted risk) — no automatable oracle.
      - **TC-15:** accepted_risk — deferred to manual verification.

    Recognized resolution keywords normalize to
    {dropped, scope_excluded, accepted_risk, legacy_assumption}; a plain
    `Dropped` is an unaccepted orphan, while `Dropped (accepted risk)` /
    `accepted_risk` record a deliberate accepted-risk drop. Bullets without the
    `**<ID>:** <keyword> — <reason>` shape are skipped; a structural bullet
    whose keyword is unrecognized is kept as `dropped` and logged.
    """
    section = root.find("coverage notes")
    if section is None:
        return []
    notes: list[dict] = []
    raw_lines = (section.content or "").splitlines()
    for child in section.children:
        raw_lines.extend((child.content or "").splitlines())
    for line in raw_lines:
        text = line.strip()
        if text.startswith(("- ", "* ", "+ ")):
            text = text[2:].strip()
        m = _COVERAGE_NOTE_RE.match(text)
        if not m:
            continue
        item_id = m.group(1).strip()
        word = _normalize_resolution_keyword(m.group(2))
        reason = m.group(3).strip()
        resolution = _COVERAGE_RESOLUTION_MAP.get(word)
        if resolution is None:
            resolution = _COVERAGE_RESOLUTION_DEFAULT
            log.warning(
                "coverage_note.keyword_unrecognized",
                item_id=item_id,
                keyword=word,
                resolution=resolution,
            )
        notes.append({"item_id": item_id, "reason": reason, "resolution": resolution})
    return notes


def section_to_dict(s: Section) -> dict:
    """Recursive dict projection useful for *-spec.json / plan.json / etc."""
    return {
        "title": s.title,
        "level": s.level,
        "content": s.content,
        "bullets": extract_bullet_blocks(s.content),
        "tables": extract_tables(s.content),
        "children": [section_to_dict(c) for c in s.children],
    }


def parse_file(path: Path) -> Section:
    return parse_markdown(path.read_text(encoding="utf-8"))
