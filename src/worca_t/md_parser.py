"""Markdown -> structured-dict parser used by step post-processors.

This parser is intentionally lightweight: it walks a markdown document and
builds a tree of headings -> content. Steps that consume LLM output (`spec.md`,
`refined-spec.md`, `plan.md`, `test-strategy.md`, `research.md`) all share
heading-based structure, so this single parser handles their JSON projections.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

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


def section_to_dict(s: Section) -> dict:
    """Recursive dict projection useful for *-spec.json / plan.json / etc."""
    return {
        "title": s.title,
        "level": s.level,
        "content": s.content,
        "bullets": extract_bullets(s.content),
        "tables": extract_tables(s.content),
        "children": [section_to_dict(c) for c in s.children],
    }


def parse_file(path: Path) -> Section:
    return parse_markdown(path.read_text(encoding="utf-8"))
