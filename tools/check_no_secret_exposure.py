#!/usr/bin/env python3
"""Secret-exposure guard — blocks Bash/PowerShell commands that would print a
known secret env var (or dump a `.env` file / the whole environment) to the
terminal, BEFORE the command runs.

CLAUDE.md's "no secrets in code... masked in logs" rule was previously prose
only: nothing stopped `echo $ANTHROPIC_API_KEY` from executing and printing
the raw value into the transcript. This closes that loop the same way
`check_no_sut_tokens.py` closes the SUT-specific-fix loop: a mechanical,
pre-execution check instead of relying on the model to remember the rule
mid-session.

This is a best-effort denylist, not a full DLP system — it targets the
concrete failure mode (printing a KNOWN secret name via a KNOWN output verb),
not every conceivable exfiltration path.

Modes:
    tools/check_no_secret_exposure.py --check "<command>"   # ad-hoc test
    tools/check_no_secret_exposure.py --hook-mode            # read Claude
                                                              #   Code PreToolUse
                                                              #   JSON on stdin,
                                                              #   exit 2 to block

Exit codes: 0 clean · 1 violation (--check, CLI) · 2 violation (--hook-mode,
causes Claude Code to block the tool call and echo stderr to the model).
"""

from __future__ import annotations

import json
import re
import sys

# Mirrors `src/qtea/config.py:SECRET_ENV_KEYS`. Kept as a separate literal
# (not imported) so this hook has no dependency on the qtea package or its
# third-party imports (yaml, dotenv) — it must run standalone via a bare
# `python` invocation from the Claude Code hook harness. Keep in sync by hand.
_SECRET_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "JIRA_API_TOKEN",
    "JIRA_PAT",
    "DOCUPEDIA_PAT",
    "JIRA_XRAY_CLIENT_SECRET",
    "JIRA_XRAY_API_KEY",
    "JIRA_XRAY_CLIENT_ID",
    "AZDO_PAT",
)

# Commands/cmdlets that write their argument to stdout/stderr.
_OUTPUT_VERBS = re.compile(
    r"\b(echo|printf|print|write-host|write-output|type|cat|more|less|"
    r"get-content|console\.log)\b",
    re.IGNORECASE,
)

# Reading a `.env`-family file to stdout — likely dumps every secret at once,
# regardless of whether a specific var name appears in the command text.
_ENV_FILE_DUMP = re.compile(
    r"\b(cat|type|more|less|get-content)\b[^\n]{0,60}\.env(\.\w+)?\b",
    re.IGNORECASE,
)

# Bulk environment dumps: the *entire* environment printed at once, no
# specific var named. Matched only when used as a standalone statement (not
# `env FOO=bar somecmd`, which sets a var for a subprocess rather than
# dumping the environment).
_BULK_DUMP = re.compile(
    r"(^|[;&|]\s*)"
    r"(env|printenv"
    r"|(?:gci|dir|ls|get-childitem)\s+env:\\?\*?"
    r"|get-item\s+env:\\?\*"
    r"|set)"
    r"\s*($|[;&|])",
    re.IGNORECASE,
)


# Shell control operators that separate independent clauses. The output-verb
# + var-ref combo must land in the SAME clause to count — otherwise a
# presence-only check like `[ -n "$SECRET" ] && echo set` (var referenced in
# one clause, unrelated echo in another) would false-positive.
_CLAUSE_SPLIT = re.compile(r"&&|\|\||;|\n|\||&")


def _name_ref_pattern(name: str) -> str:
    return (
        rf"\$\{{?{name}\}}?\b|%{name}%|\$\{{?env:{name}\}}?\b"
        rf"|os\.environ(?:\.get)?\(\s*[\"']{name}[\"']"
        rf"|os\.getenv\(\s*[\"']{name}[\"']"
    )


def _violations(command: str) -> list[str]:
    if not command:
        return []
    hits: list[str] = []
    for clause in _CLAUSE_SPLIT.split(command):
        if not _OUTPUT_VERBS.search(clause):
            continue
        matched_names = [n for n in _SECRET_NAMES if re.search(_name_ref_pattern(n), clause)]
        if matched_names:
            hits.append(
                "command prints a known secret env var: " + ", ".join(matched_names)
            )
    if _ENV_FILE_DUMP.search(command):
        hits.append("command reads a .env-family file to stdout")
    if _BULK_DUMP.search(command):
        hits.append("command dumps the entire environment (env/printenv/set/Env:)")
    return hits


_BLOCK_MESSAGE = (
    "\nBLOCKED by CLAUDE.md guardrail: this command would print a secret (or "
    "the whole environment / a .env file) to the terminal. Secrets are env "
    "vars only and must stay masked in logs.\n\n"
    "  reason(s):\n{reasons}\n\n"
    "If you need to confirm a var is SET, check truthiness/length only "
    "(e.g. `[ -n \"$VAR\" ] && echo set`), never print the value.\n"
    "Denylist source: tools/check_no_secret_exposure.py "
    "(mirrors src/qtea/config.py:SECRET_ENV_KEYS).\n"
)


def _hook_run() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0

    hits = _violations(command)
    if not hits:
        return 0

    sys.stderr.write(
        _BLOCK_MESSAGE.format(reasons="\n".join(f"    - {h}" for h in hits))
    )
    return 2


def main(argv: list[str]) -> int:
    if "--hook-mode" in argv:
        return _hook_run()
    if "--check" in argv:
        idx = argv.index("--check")
        command = argv[idx + 1] if idx + 1 < len(argv) else ""
        hits = _violations(command)
        if hits:
            print(f"Secret-exposure violations ({len(hits)}):", file=sys.stderr)
            for h in hits:
                print(f"  {h}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
