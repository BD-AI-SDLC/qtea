#!/usr/bin/env python3
"""SUT-token denylist scanner — guards CLAUDE.md's "generic, not SUT-specific"
rule against silent recurrence.

Modes:
    tools/check_no_sut_tokens.py                       # scan default roots (CI)
    tools/check_no_sut_tokens.py PATH [PATH ...]       # scan specific paths
    tools/check_no_sut_tokens.py --diff-only [PATH...] # scan only lines
                                                       #   added vs git HEAD
    tools/check_no_sut_tokens.py --hook-mode           # read Claude Code
                                                       #   tool JSON on stdin,
                                                       #   scan diff-only, exit 2
                                                       #   on any hit so the
                                                       #   hook harness surfaces
                                                       #   the block back into
                                                       #   the model's turn

Exit codes: 0 clean · 1 violation (CLI) · 2 violation (--hook-mode, causes
Claude Code to block the tool call and echo stderr to the model).

Rationale: prose guardrails in CLAUDE.md were violated within the very session
they were reaffirmed. A regex denylist run automatically per Edit/Write closes
the loop mechanically — no model discipline required.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Add new SUT tokens here. Case-sensitivity / word-boundaries are tuned to avoid
# false positives (`\bRoPA\b` won't match `propagate`; `\bRopa[A-Z]` matches
# `RopaEntry` / `RopaPage` / `RopaLocators` but not lowercase `ropa` in prose).
_DENYLIST: tuple[tuple[str, str], ...] = (
    (r"\bRoPA\b", "RoPA"),
    (r"\bROPA\b", "ROPA"),
    (r"\bRopa[A-Z][A-Za-z]*", "Ropa* (RopaEntry / RopaPage / RopaLocators / etc.)"),
    (r"\bDirectoryRopa\w*", "DirectoryRopa*"),
    (r"qtea_ropa\w*", "qtea_ropa*"),
    (r"\bgoToRopaModule\b", "goToRopaModule"),
    (r"\bcreateBasicRopaEntry\b", "createBasicRopaEntry"),
    (r"(?i)MANAGEMENT SYSTEMS", "MANAGEMENT SYSTEMS"),
    (r"Records of Processing Activit(?:ies|y)", "Records of Processing Activities"),
    (r"GRC[- _]?HUB", "GRC-HUB"),
    (r"GRC HOME", "GRC HOME"),
    (r"grchub", "grchub"),
    (r"corporatercloud", "corporatercloud"),
    (r"CorpoWebserver", "CorpoWebserver"),
    (r"Internal Control System", "Internal Control System (launcher tile)"),
    (r"Compliance Management System", "Compliance Management System (launcher tile)"),
    (r"run 20\d{6}[- ]?[A-Za-z0-9]*\s+ROPA", "run YYYYMMDD ROPA (incident trail)"),
    (r"\bHCDI\b", "HCDI"),
    (r"testuser9\d", "testuser9N"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat), label) for pat, label in _DENYLIST
)

# Scanner scope. Prompts/instructions/schemas that agents actually consume.
# `tests/` and `docs/` are LOW-severity per audit and out of scope for the
# per-edit hook (they don't shape agent behavior at runtime).
_DEFAULT_ROOTS: tuple[str, ...] = (
    "src", "agents", "schemas", "skills", "templates",
)

# Paths that legitimately reference the Bosch OPERATING environment (proxy,
# on-prem Jira / Confluence / Azure DevOps hosts, model farm). Bosch is the
# environment qtea runs *inside*, distinct from any SUT under test. These paths
# are exempted from denylist matches because such references belong there — and
# `Bosch` isn't in the denylist anyway. Keep this list minimal.
_ENV_ALLOWLIST_SUBPATHS: tuple[str, ...] = (
    "src/qtea/proxy.py",
    "src/qtea/ado_client.py",
    "src/qtea/jira_client.py",
    "src/qtea/confluence_client.py",
    "src/qtea/config.py",
)

_SKIP_DIRS = frozenset({
    ".git", ".venv", "__pycache__", "node_modules", ".qtea", ".claude",
})


def _repo_root() -> Path:
    """Anchor to the git root so relative paths resolve regardless of CWD."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def _in_scope(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts or parts[0] not in _DEFAULT_ROOTS:
        return False
    if any(p in _SKIP_DIRS for p in parts):
        return False
    if rel.as_posix() in _ENV_ALLOWLIST_SUBPATHS:
        return False
    # Skip binaries and other clearly-non-text.
    if path.suffix in {".pyc", ".png", ".jpg", ".gif", ".ico", ".zip", ".gz"}:
        return False
    return True


def _iter_default_targets(root: Path):
    for r in _DEFAULT_ROOTS:
        base = root / r
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and _in_scope(p, root):
                yield p


def _added_lines(path: Path, root: Path) -> list[tuple[int, str]] | None:
    """(new-file line number, content) for every line added in this working
    tree vs git HEAD. None when git can't diff (not a repo, git missing) — the
    caller then falls back to a whole-file scan.

    For an untracked/new file, `git diff --no-index /dev/null <file>` treats
    every line as added.
    """
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)
    try:
        # Tracked-file path: diff against HEAD.
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-color",
             "--unified=0", "HEAD", "--", rel],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        if diff.returncode == 0 and diff.stdout:
            return _parse_added_lines(diff.stdout)
        # Untracked/new-file path: force a diff against /dev/null (works on
        # Windows too — git normalizes the sentinel).
        untracked = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-color", "--unified=0",
             "--no-index", "/dev/null", rel],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        # `git diff --no-index` exits 1 when files differ (which is our normal
        # signal), so treat both 0 and 1 as parseable output.
        if untracked.returncode in (0, 1) and untracked.stdout:
            return _parse_added_lines(untracked.stdout)
        return []
    except FileNotFoundError:  # git not on PATH
        return None


def _parse_added_lines(diff_text: str) -> list[tuple[int, str]]:
    """Walk a unified-diff hunk header (`@@ -a,b +c,d @@`), tracking the
    running new-file line number so each `+` body line is attributed to a real
    line in the working tree. `+++` file-header lines are ignored."""
    out: list[tuple[int, str]] = []
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    new_ln = 0
    for line in diff_text.splitlines():
        m = hunk_re.match(line)
        if m:
            new_ln = int(m.group(1))
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append((new_ln, line[1:]))
            new_ln += 1
        elif not line.startswith("-"):
            new_ln += 1
    return out


def _scan_lines(path: Path, lines: list[tuple[int, str]]) -> list[str]:
    hits: list[str] = []
    for lineno, text in lines:
        for pat, label in _COMPILED:
            m = pat.search(text)
            if m:
                snippet = text.strip()[:120]
                hits.append(f"{path}:{lineno}: {label} — {snippet!r}")
    return hits


def _whole_file_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(enumerate(text.splitlines(), start=1))


def scan(paths: list[Path], *, root: Path, diff_only: bool) -> list[str]:
    all_hits: list[str] = []
    for p in paths:
        if not p.is_file() or not _in_scope(p, root):
            continue
        lines: list[tuple[int, str]] | None
        if diff_only:
            lines = _added_lines(p, root)
            if lines is None:
                lines = _whole_file_lines(p)
        else:
            lines = _whole_file_lines(p)
        all_hits.extend(_scan_lines(p, lines))
    return all_hits


def _scan_string(text: str) -> list[tuple[str, str]]:
    """Return (label, snippet) for every denylist match in ``text``. Used by
    hook mode to scan the edit DELTA (not the whole file) — precise per-edit
    accounting with no git dependency."""
    hits: list[tuple[str, str]] = []
    if not text:
        return hits
    for pat, label in _COMPILED:
        for m in pat.finditer(text):
            # Snippet: the line containing the match, trimmed. Locates the
            # match in context without dumping surrounding source.
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            if end == -1:
                end = len(text)
            hits.append((label, text[start:end].strip()[:120]))
    return hits


def _hook_run(root: Path) -> int:
    """Read Claude Code's PostToolUse JSON on stdin, scan the edit DELTA, and
    return 2 on a new SUT-token violation (blocks the tool call, stderr echoed
    to the model).

    Delta semantics:
      * Edit: scan ``new_string``; suppress hits already present in
        ``old_string`` (the edit didn't INTRODUCE the token — it was there).
      * Write: scan ``content`` (whole file). Writes are new files or full
        rewrites; any SUT token in a Write is deliberate.

    Files outside the scanner's scope (see ``_in_scope``) short-circuit to 0."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name")
    if tool not in ("Edit", "Write"):
        return 0
    tool_input = payload.get("tool_input") or {}
    fp = tool_input.get("file_path")
    if not isinstance(fp, str) or not fp:
        return 0
    target = Path(fp)
    if not target.is_absolute():
        target = (root / target).resolve()
    if not _in_scope(target, root):
        return 0

    if tool == "Edit":
        new_text = tool_input.get("new_string") or ""
        old_text = tool_input.get("old_string") or ""
        new_hits = _scan_string(new_text)
        old_labels = {label for label, _ in _scan_string(old_text)}
        # An Edit "introduces" a token only when the NEW string contains a
        # denylist label the OLD string didn't. Otherwise the token was there
        # already and the edit is just reflowing surrounding code.
        introduced = [(lbl, snip) for lbl, snip in new_hits if lbl not in old_labels]
    else:  # Write
        content = tool_input.get("content") or ""
        introduced = _scan_string(content)

    if not introduced:
        return 0

    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        rel = str(target)
    sys.stderr.write(
        "\nBLOCKED by CLAUDE.md guardrail: this edit introduces SUT-specific "
        "token(s). Fixes to qtea must be generic -- framework-general at most, "
        "never tied to a specific SUT. Replace with a neutral illustration.\n\n"
    )
    for label, snippet in introduced:
        sys.stderr.write(f"  {rel}: {label} - {snippet!r}\n")
    sys.stderr.write(
        "\nDenylist source: tools/check_no_sut_tokens.py "
        "(edit `_DENYLIST` there if a match is a legitimate environment "
        "reference, not SUT-specific).\n"
    )
    return 2


def main(argv: list[str]) -> int:
    root = _repo_root()
    hook_mode = "--hook-mode" in argv
    diff_only = "--diff-only" in argv
    positional = [
        a for a in argv[1:]
        if a not in ("--hook-mode", "--diff-only")
    ]

    if hook_mode:
        return _hook_run(root)

    targets = (
        [Path(a).resolve() for a in positional]
        if positional else list(_iter_default_targets(root))
    )
    hits = scan(targets, root=root, diff_only=diff_only)
    if hits:
        mode = "added-lines" if diff_only else "full-file"
        print(f"SUT-token denylist violations ({mode} scan, {len(hits)} hit(s)):",
              file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
