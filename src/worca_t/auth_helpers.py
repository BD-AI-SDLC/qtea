"""Auth-context helpers shared by Step 8 (now a stub) and Step 9 (execute).

Extracted from the previous ``steps/s08_locator_resolution.py`` location
so Step 9 no longer imports from the soft-deleted Step 8 module. Pure
helpers — no side effects, no I/O. Operate on the `active_module` dict
that Step 6 (research) emits and downstream steps consume.
"""

from __future__ import annotations

from pathlib import Path


def auth_summary_for_prompt(active_module: dict | None) -> str:
    """Compact auth-flow summary for inclusion in an agent prompt."""
    if not active_module:
        return ""
    auth = active_module.get("auth_flow") or {}
    lines = [
        f"Active module: `{active_module.get('name')}` "
        f"(path: `{active_module.get('path')}`, language: "
        f"`{active_module.get('language') or 'unknown'}`)",
        f"Auth type: `{auth.get('type', 'unknown')}`",
    ]
    if auth.get("entry_method"):
        lines.append(f"Auth entry method: `{auth['entry_method']}`")
    if auth.get("fixture_entry"):
        lines.append(f"Auth fixture: `{auth['fixture_entry']}`")
    creds = auth.get("credentials_env_vars") or []
    if creds:
        lines.append(f"Credentials env vars: {', '.join(creds)}")
    return "\n".join(lines)


def auth_relevant_sut_files(active_module: dict | None) -> list[str]:
    """Return SUT-relative paths of auth/page-object/helper/fixture/locator
    files for the active module. Used to populate the prompt's "files you
    can call" list when the agent has ``add_dirs=[sut]``.
    """
    if not active_module:
        return []
    files: list[str] = []
    auth = active_module.get("auth_flow") or {}
    for key in ("entry_method", "fixture_entry"):
        v = auth.get(key)
        if isinstance(v, str) and v:
            files.append(v.split(":", 1)[0])
    for bucket in ("existing_page_objects", "existing_fixtures",
                   "existing_helpers", "existing_locators"):
        for entry in active_module.get(bucket) or []:
            p = entry.get("file") if isinstance(entry, dict) else None
            if p:
                files.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for p in files:
        if p and p not in seen and Path(p).name != "__init__.py":
            seen.add(p)
            out.append(p)
    return out
