"""Read-side helpers that load Step 9's inputs from prior steps' artifacts.

Reads Step 6 research (``research.json``, ``stack_profile.json``,
``sut_inventory``) and Step 8 codegen output (``tbd-index.json``,
``generated-files.json``); resolves the SUT tests directory and the
attachment glob for allure/screenshot collection. All returns are
defensive — missing / corrupt JSON degrades to empty dict, None, or
empty set rather than raising, so the parent step can log-and-continue.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from qtea.stack_profile import StackProfile
from qtea.steps.base import StepContext


# Fallback tests subdirectory used when:
#   - --isolated-tests is set (explicit user opt-in to today's behavior), OR
#   - sut_inventory has no test_directory_layout for the active module.
# When isolated, the dir lives under the active module's path so monorepos
# don't clobber sibling modules.
_ISOLATED_TESTS_DIR_NAME = "qteaests"


def _research_payload(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(6) / "research.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_stack_profile(ctx: StepContext) -> StackProfile | None:
    """Load Step 6's stack_profile.json into a `StackProfile` dataclass.

    Returns None when the artifact is missing (older workspaces re-run from
    Step 8+) or when the JSON is unparseable. Step 9 falls back to bare
    framework commands in that case.
    """
    p = ctx.workspace.step_dir(6) / "stack_profile.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # Tolerate extra keys; only consume known dataclass fields.
    allowed = {
        "language", "package_manager", "wrapper_prefix",
        "pre_install_command", "install_command",
        "test_command", "start_command", "env_file_path", "venv_path",
        "detection_signal", "notes",
    }
    return StackProfile(**{k: data.get(k) for k in allowed})


def _detected_command(research: dict) -> str | None:
    cmds = research.get("commands") or {}
    return cmds.get("test")


def _active_module(sut_inventory: dict) -> dict | None:
    """Pull the active module entry out of a raw `sut_inventory` dict.

    Returns None when no `active_module` is set or the name doesn't match.
    Older runs (no `sut_inventory` field) get None and fall through to the
    isolated-tests fallback.
    """
    active = sut_inventory.get("active_module")
    if not active:
        return None
    for mod in sut_inventory.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == active:
            return mod
    return None


def _framework(research: dict, index: dict) -> str:
    return research.get("detected_stack") or index.get("framework") or "unknown"


def _load_index(ctx: StepContext) -> dict:
    p = ctx.workspace.step_dir(8) / "tbd-index.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_generated_files(ctx: StepContext) -> set[str]:
    """Load Step 8's ``generated-files.json`` and return SUT-relative posix paths.

    These are the files codegen produced this run.  The heal agent is allowed
    to edit them even when they match a test-file FORBIDDEN pattern, because
    the heal agent is fixing codegen's own mistakes (interaction patterns,
    locator usage) — NOT pre-existing SUT test code.
    """
    p = ctx.workspace.step_dir(8) / "generated-files.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    files = data.get("files") or []
    return {f.replace("\\", "/") for f in files if isinstance(f, str)}


def _sut_tests_dir(
    sut_root: Path,
    *,
    active_module: dict | None,
    isolated: bool,
) -> Path:
    """Resolve the directory inside the SUT where qtea-generated tests live.

    Steps 7 & 8 write tests there directly (on the qtea branch); this
    function only computes the path — nothing is copied or wiped.

    Resolution mirrors the active module's layout:
      - `--isolated-tests` → `<sut>/<module.path>/qteaests/`
      - Active module's `test_directory_layout.base_dir` (the SUT's own
        convention, e.g. `tests/` or `e2e/`) → `<sut>/<module.path>/<base_dir>/`
      - Fallback when nothing is known: `<sut>/<module.path>/qteaests/`
    """
    module_path = "."
    base_dir = _ISOLATED_TESTS_DIR_NAME
    if active_module:
        module_path = str(active_module.get("path") or ".")
        if not isolated:
            layout = active_module.get("test_directory_layout") or {}
            candidate = layout.get("base_dir")
            if candidate:
                base_dir = str(candidate)
    module_root = sut_root if module_path == "." else (sut_root / module_path)
    return module_root / base_dir


def _attachment_glob(sut_root: Path) -> list[dict]:
    """Best-effort discovery of common test-result artifacts in the SUT root."""
    out: list[dict] = []
    patterns: list[tuple[str, str]] = [
        ("test-results/**/trace.zip", "trace"),
        ("test-results/**/*.png", "screenshot"),
        ("test-results/**/*.webm", "video"),
        ("screenshots/**/*.png", "screenshot"),
        ("screenshots/**/*.jpg", "screenshot"),
        ("reports/screenshots/**/*.png", "screenshot"),
        ("playwright-report/**/*", "other"),
        ("allure-results/**/*.png", "screenshot"),
        ("allure-results/**/*.json", "other"),
    ]
    for pattern, kind in patterns:
        for p in sut_root.glob(pattern):
            if p.is_file():
                out.append({"path": str(p), "type": kind})
    return out


def _clean_sut_artifacts(sut_root: Path) -> None:
    """Remove prior-attempt screenshots/traces so only the last run's artifacts survive."""
    import contextlib

    patterns = [
        "test-results/**/*.png",
        "test-results/**/*.webm",
        "screenshots/**/*.png",
        "screenshots/**/*.jpg",
        "reports/screenshots/**/*.png",
    ]
    for pattern in patterns:
        for p in sut_root.glob(pattern):
            with contextlib.suppress(OSError):
                p.unlink()


__all__ = [
    "_ISOLATED_TESTS_DIR_NAME",
    "_active_module",
    "_attachment_glob",
    "_clean_sut_artifacts",
    "_detected_command",
    "_framework",
    "_load_generated_files",
    "_load_index",
    "_load_stack_profile",
    "_research_payload",
    "_sut_tests_dir",
]
