"""Dep-recovery HITL confirmation + package-manager install shim for Step 9.

Two helpers, both invoked from ``ExecuteStep.run()`` when a first-attempt
test failure indicates a missing test dependency (``ModuleNotFoundError``
raised inside the SUT venv). ``_hitl_confirm_dep_install`` asks the
operator before touching the SUT (CLI + UI HITL parity via
``hitl.prompt_user``); ``_run_dep_install`` shells out to pip / poetry /
uv / pdm / pipenv with the SUT venv isolated so we never install into
qtea's parent venv.

The Poetry-noop guard exists because ``poetry add`` returns exit 0 when a
package is already declared in ``pyproject.toml`` even if it's not
installed in the resolved venv — the two ``_POETRY_NOOP_MARKERS`` strings
catch that stdout and turn it into a structured failure so the caller
doesn't claim victory and re-run the same broken tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.proxy import safe_subprocess_env
from qtea.stack_profile import PYTHON_VENV_MANAGERS, StackProfile
from qtea.test_runner import install_command_for

log = get_logger(__name__)


# Poetry stdout phrases that indicate `poetry add` was a no-op because the
# package is already declared in pyproject.toml. Exit code is 0 in that case
# even though NOTHING was installed — treat as failure so the caller doesn't
# claim victory and re-run the same broken tests. See the run forensics in
# the bug that introduced this check: the package was declared but never
# installed in the resolved venv (because qtea's parent venv was being
# reused — see proxy.safe_subprocess_env's isolate_venv flag).
_POETRY_NOOP_MARKERS: tuple[str, ...] = (
    "already present in the pyproject.toml",
    "Nothing to add",
)


def _run_dep_install(
    package_manager: str | None,
    package: str,
    sut_root: Path,
    install_log_path: Path,
    *,
    timeout_s: int = 600,
    profile: StackProfile | None = None,
) -> tuple[bool, str]:
    """Install a single missing test dependency. Append outcome to install.log.

    `profile` is consulted for two reasons: (1) pip needs the SUT's venv-bin
    directory to build a path-prefixed install argv, and (2) Python managers
    that own a venv (poetry, uv, pdm, pipenv) need `VIRTUAL_ENV` stripped
    from the subprocess env so they don't reuse qtea's parent venv as
    the SUT's venv. Falls back to bare argv when `profile` is None — same
    behavior as pre-fix runs for callers that don't have a profile handy.

    Returns ``(success, summary)`` where ``summary`` is the command line on
    success or a one-line error on failure. Never raises — package-manager
    timeouts / OS errors become structured failures.
    """
    pm = (package_manager or "").lower()
    # pip needs the SUT's venv bin dir to install into the venv the tests
    # actually use; for poetry/uv/pdm/pipenv venv_bin is unused.
    venv_bin = None
    if pm == "pip" and profile and profile.wrapper_prefix:
        venv_bin = profile.wrapper_prefix
    argv = install_command_for(package_manager, package, venv_bin=venv_bin)
    if argv is None:
        if pm == "pip":
            return False, (
                f"pip auto-install requires the SUT to have a .venv "
                f"(profile.wrapper_prefix). None detected for "
                f"package_manager={package_manager!r}."
            )
        return False, f"no install argv for package_manager={package_manager!r}"

    cmd_line = " ".join(argv)
    try:
        proc = subprocess.run(
            argv,
            cwd=sut_root,
            env=safe_subprocess_env(isolate_venv=pm in PYTHON_VENV_MANAGERS),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        with install_log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n$ {cmd_line}  # auto-install\n# error: {e}\n")
        return False, f"{cmd_line}: {e}"

    with install_log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n\n$ {cmd_line}  # auto-install missing dep\n"
            f"# exit_code: {proc.returncode}\n"
            f"# STDOUT\n{proc.stdout}\n"
            f"# STDERR\n{proc.stderr}\n"
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        snippet = tail[-1] if tail else ""
        return False, f"`{cmd_line}` exited {proc.returncode}: {snippet[:200]}"

    if pm == "poetry" and any(m in (proc.stdout or "") for m in _POETRY_NOOP_MARKERS):
        return False, (
            f"`{cmd_line}` was a no-op: package already declared in pyproject.toml "
            f"but not installed in the resolved venv. Run `poetry install` against "
            f"a clean SUT-specific venv, or delete the active venv and re-run."
        )
    return True, cmd_line


def _hitl_confirm_dep_install(
    *, module: str, package: str, hint: str, default: bool = True,
) -> bool:
    """Route the dep-recovery install confirmation through the shared HITL
    channel so both CLI (Rich terminal) and UI (Flet dialog) modes render
    the prompt correctly.

    Previously this call site used ``rich.prompt.Confirm.ask`` directly,
    which is a no-op in UI mode (``sys.stdin`` is not a TTY and the Rich
    prompt bypasses the HITL bridge). ``hitl.prompt_user`` handles the
    branching via its bridge monkey-patch, so calling it here surfaces the
    prompt in whichever mode is active.

    Answer parsing: ``y`` / ``yes`` → True, ``n`` / ``no`` → False. Any
    other text (or an empty answer, timeout, or non-TTY-non-UI environment
    where ``prompt_user`` returns ``{}``) falls back to ``default`` with a
    log entry so the decision is traceable in ``run.log.jsonl``.
    """
    from qtea.hitl import Question, prompt_user

    q = Question(
        id="DEPINSTALL-01",
        kind="clarification",
        prompt_text=(
            f"Missing test dependency `{module}` detected. Install "
            f"`{package}` ({hint or 'no install hint recorded'}) and retry? "
            f"[y/n, default={'y' if default else 'n'}]"
        ),
        context=f"module={module} package={package}",
    )
    try:
        answers = prompt_user([q], agent_label="dep-recovery")
    except Exception as e:
        log.warning("step09.dep_recover_hitl_failed", error=str(e))
        return default

    raw = answers.get(q.id)
    if raw is None:
        # Empty prompt, skip, or non-interactive with no bridge — fall back
        # to the default so the enclosing flow behaves exactly as it did
        # when the CLI Confirm.ask defaulted-True on Enter.
        log.info(
            "step09.dep_recover_hitl_defaulted",
            module=module, package=package, default=default,
        )
        return default
    _resolution, text = raw
    normalized = text.strip().lower()
    if normalized in ("y", "yes"):
        return True
    if normalized in ("n", "no"):
        return False
    log.warning(
        "step09.dep_recover_answer_unclear",
        module=module, package=package, answer=text, default=default,
    )
    return default


__all__ = [
    "_POETRY_NOOP_MARKERS",
    "_hitl_confirm_dep_install",
    "_run_dep_install",
]
