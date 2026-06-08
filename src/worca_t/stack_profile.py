"""Deterministic SUT toolchain detection.

Walks the SUT root looking at manifest files (poetry.lock, package-lock.json,
pom.xml, ...) and returns a `StackProfile` describing the package manager,
its wrapper-prefix invocation (`poetry run`, `npx`, `mvn`, ...), the canonical
install command (`poetry install --no-interaction`, `npm ci`, ...) and, when
available, the project's start command and env-file path.

The detector is pure-Python and intentionally manifest-driven: an LLM agent
that infers these facts is brittle. Lockfiles, in particular, are unambiguous
signals of the canonical package manager — `poetry.lock` plus a `[tool.poetry]`
section pins poetry over any other Python tool that *could* read pyproject.toml.

The `StackProfile.test_command` is left None: it composes with the detected
test framework downstream in `test_runner.resolve_command`, where we already
know which junit/json reporter flag to append.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from worca_t.logging_setup import get_logger

log = get_logger(__name__)


# Python package managers that *own* their virtualenv: invoking them while
# the parent process has VIRTUAL_ENV set causes them to reuse the parent's
# venv as the SUT's venv (if the Python version is compatible). Subprocesses
# targeting these managers must strip VIRTUAL_ENV / POETRY_ACTIVE so a clean
# SUT-specific venv is created. `pip` is intentionally NOT in this set — it
# does NOT own a venv; we handle pip auto-install separately by path-prefixing
# the venv's pip binary directly (see `install_command_for`).
PYTHON_VENV_MANAGERS: frozenset[str] = frozenset({"poetry", "uv", "pdm", "pipenv"})


@dataclass
class StackProfile:
    language: str | None = None
    package_manager: str | None = None
    wrapper_prefix: str | None = None  # e.g. "poetry run", "npx", "mvn", ""
    pre_install_command: str | None = None  # e.g. "python -m venv .venv"
    install_command: str | None = None
    test_command: str | None = None  # filled by researcher / resolve_command
    start_command: str | None = None
    env_file_path: str | None = None  # relative to SUT root
    venv_path: str | None = None  # relative to SUT root
    detection_signal: str | None = None  # manifest file that triggered detection
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("notes") is None:
            d["notes"] = []
        return d


# ---------------------------------------------------------------------------
# Detection table — precedence is load-bearing.
#
# Each entry is (signals, builder) where:
#   signals = tuple of relative paths; ALL must exist for the rule to fire.
#   builder = function(sut_path) -> partial dict of StackProfile fields.
#
# Walked top-to-bottom; first match wins. More-specific lockfiles win over
# generic manifests; e.g. (poetry.lock, pyproject.toml) wins over (pyproject.toml).
# ---------------------------------------------------------------------------


def _venv_bin(sut: Path) -> str:
    """Cross-platform venv binary subdir under .venv/."""
    return ".venv/Scripts" if sys.platform == "win32" else ".venv/bin"


def _find_env_file(sut: Path) -> str | None:
    """Return the first .env-style file we find, relative to the SUT root."""
    for name in (".env", ".env.local", ".env.test", ".env.example",
                 ".env.template", ".env.sample"):
        if (sut / name).exists():
            return name
    return None


def _poetry_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "python",
        "package_manager": "poetry",
        "wrapper_prefix": "poetry run",
        "install_command": "poetry install --no-interaction",
        "venv_path": None,  # poetry-managed, lives outside the project
    }


def _uv_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "python",
        "package_manager": "uv",
        "wrapper_prefix": "uv run",
        "install_command": "uv sync",
        "venv_path": ".venv",
    }


def _pdm_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "python",
        "package_manager": "pdm",
        "wrapper_prefix": "pdm run",
        "install_command": "pdm install",
        "venv_path": ".venv",
    }


def _pipenv_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "python",
        "package_manager": "pipenv",
        "wrapper_prefix": "pipenv run",
        "install_command": "pipenv sync --dev",
        "venv_path": None,
    }


def _pip_build(sut: Path) -> dict[str, Any]:
    req = next(
        (p.name for p in sut.glob("requirements*.txt") if p.is_file()),
        "requirements.txt",
    )
    bin_dir = _venv_bin(sut)
    return {
        "language": "python",
        "package_manager": "pip",
        "wrapper_prefix": bin_dir,
        "pre_install_command": f"{sys.executable} -m venv .venv",
        "install_command": f"{bin_dir}/pip install -r {shlex.quote(req)}",
        "venv_path": ".venv",
        "notes": [
            "pip venv: tests will run with `<.venv-bin>/pytest` (no wrapper command).",
        ],
    }


def _pnpm_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "javascript",
        "package_manager": "pnpm",
        "wrapper_prefix": "pnpm exec",
        "install_command": "pnpm install --frozen-lockfile",
        "venv_path": "node_modules",
    }


def _yarn_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "javascript",
        "package_manager": "yarn",
        "wrapper_prefix": "yarn",
        "install_command": "yarn install --frozen-lockfile",
        "venv_path": "node_modules",
    }


def _npm_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "javascript",
        "package_manager": "npm",
        "wrapper_prefix": "npx",
        "install_command": "npm ci",
        "venv_path": "node_modules",
    }


def _maven_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "java",
        "package_manager": "maven",
        "wrapper_prefix": "mvn",
        "install_command": "mvn -B -DskipTests install",
        "venv_path": None,
    }


def _gradle_build(sut: Path) -> dict[str, Any]:
    # Prefer the wrapper if present; falls back to bare `gradle`.
    if (sut / "gradlew").exists():
        wrap = "./gradlew"
    elif (sut / "gradlew.bat").exists():
        wrap = "gradlew.bat"
    else:
        wrap = "gradle"
    return {
        "language": "java",
        "package_manager": "gradle",
        "wrapper_prefix": wrap,
        "install_command": f"{wrap} assemble",
        "venv_path": None,
    }


def _bundler_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "ruby",
        "package_manager": "bundler",
        "wrapper_prefix": "bundle exec",
        "install_command": "bundle install",
        "venv_path": None,
    }


def _go_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "go",
        "package_manager": "go",
        "wrapper_prefix": "go",
        "install_command": "go mod download",
        "venv_path": None,
    }


def _cargo_build(sut: Path) -> dict[str, Any]:
    return {
        "language": "rust",
        "package_manager": "cargo",
        "wrapper_prefix": "cargo",
        "install_command": "cargo fetch",
        "venv_path": None,
    }


def _has_poetry_section(sut: Path) -> bool:
    pp = sut / "pyproject.toml"
    if not pp.exists():
        return False
    try:
        text = pp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Either classic [tool.poetry] table or PEP 621 build-system = poetry.
    return ("[tool.poetry]" in text) or ("poetry-core" in text) or ("poetry.core" in text)


_RULES: list[tuple[str, list[str], Any]] = [
    # (label, required-files, builder)
    ("poetry-lock", ["poetry.lock", "pyproject.toml"], _poetry_build),
    ("uv-lock", ["uv.lock"], _uv_build),
    ("pdm-lock", ["pdm.lock"], _pdm_build),
    ("pipfile-lock", ["Pipfile.lock"], _pipenv_build),
    ("pipfile", ["Pipfile"], _pipenv_build),
    ("pnpm-lock", ["pnpm-lock.yaml"], _pnpm_build),
    ("yarn-lock", ["yarn.lock"], _yarn_build),
    ("package-lock", ["package-lock.json"], _npm_build),
    ("npm-shrinkwrap", ["npm-shrinkwrap.json"], _npm_build),
    ("pom", ["pom.xml"], _maven_build),
    ("gradle-kts", ["build.gradle.kts"], _gradle_build),
    ("gradle", ["build.gradle"], _gradle_build),
    ("gemfile-lock", ["Gemfile.lock"], _bundler_build),
    ("gemfile", ["Gemfile"], _bundler_build),
    ("go-mod", ["go.mod"], _go_build),
    ("cargo", ["Cargo.toml"], _cargo_build),
]


def _detect_start_command(sut: Path, language: str | None) -> str | None:
    """Best-effort start_command extraction. Returns None if undetectable.

    Stored for downstream consumption — Step 9 does NOT auto-spawn the server
    in v1 (see plan: dev-server lifecycle is out of scope). Recorded only so
    the researcher agent and future versions have a reliable signal.
    """
    pj = sut / "package.json"
    if pj.exists():
        try:
            import json
            data = json.loads(pj.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {}) or {}
            for key in ("dev", "start", "serve"):
                cmd = scripts.get(key)
                if cmd:
                    return f"npm run {key}"
        except (OSError, json.JSONDecodeError):
            pass

    pp = sut / "pyproject.toml"
    if pp.exists() and language == "python":
        try:
            text = pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        # Pattern: poetry scripts table or PEP 621 [project.scripts].
        # We don't parse TOML to avoid an extra dep; the researcher agent will
        # do better when it sees the file. Return None — better than guessing.
        if "uvicorn" in text or "fastapi" in text.lower():
            return "uvicorn <app>:app --reload  # template; replace <app>"
    return None


def detect_stack_profile(sut_path: Path) -> StackProfile:
    """Inspect `sut_path` and return a populated `StackProfile`.

    On no match (no recognized manifest), returns an all-None profile so the
    downstream researcher agent + bare-command fallbacks still work.
    """
    if not sut_path.exists() or not sut_path.is_dir():
        return StackProfile(notes=[f"sut path does not exist: {sut_path}"])

    # pip is a fallback: only fires when Python is implied and no other
    # Python manager won. Requirements files alone aren't a strong enough
    # signal to override poetry/uv/pdm/pipenv.
    for label, files, builder in _RULES:
        if all((sut_path / f).exists() for f in files):
            # Special-case: pyproject.toml without poetry.lock could be uv/pdm/pip.
            # poetry-lock rule above already required poetry.lock; here we just
            # gate the rule on the file existence above.
            if label == "poetry-lock" and not _has_poetry_section(sut_path):
                continue
            fields = builder(sut_path)
            language = fields.get("language")
            profile = StackProfile(
                **{**fields, "detection_signal": ", ".join(files)},
            )
            profile.env_file_path = _find_env_file(sut_path)
            profile.start_command = _detect_start_command(sut_path, language)
            log.info(
                "stack_profile.detected",
                package_manager=profile.package_manager,
                wrapper=profile.wrapper_prefix,
                signal=profile.detection_signal,
            )
            return profile

    # Pip fallback (requirements*.txt with no recognized Python manager).
    if any(sut_path.glob("requirements*.txt")):
        fields = _pip_build(sut_path)
        profile = StackProfile(
            **{**fields, "detection_signal": "requirements*.txt"},
        )
        profile.env_file_path = _find_env_file(sut_path)
        profile.start_command = _detect_start_command(sut_path, "python")
        log.info(
            "stack_profile.detected",
            package_manager=profile.package_manager,
            wrapper=profile.wrapper_prefix,
            signal=profile.detection_signal,
        )
        return profile

    log.info("stack_profile.no_signal", sut=str(sut_path))
    return StackProfile(
        env_file_path=_find_env_file(sut_path),
        notes=["no recognized package manager manifest in SUT root"],
    )


def wrap_command(profile: StackProfile | None, bare_command: str) -> str:
    """Prepend `profile.wrapper_prefix` to a bare command when appropriate.

    Special cases:
      - profile is None or wrapper_prefix is None / empty → return bare.
      - pip's wrapper is a venv bin dir (`.venv/bin`) — prepend as PATH-style:
        the bare command's first token is rewritten as `<bin>/<token>`.
      - npm's wrapper `npx` is correct for invoking tool binaries from
        node_modules; for `npm run <script>` use the script directly.
      - Wrapper already present (caller already wrote `poetry run pytest`) →
        return bare unchanged.
    """
    if not profile or not profile.wrapper_prefix:
        return bare_command

    wrap = profile.wrapper_prefix
    bare = bare_command.strip()

    if bare.startswith(wrap + " "):
        return bare

    # pip / venv: the "wrapper" is a bin directory, not a literal command.
    if profile.package_manager == "pip":
        first, _, rest = bare.partition(" ")
        rewritten = f"{wrap}/{first}"
        return f"{rewritten} {rest}".rstrip()

    return f"{wrap} {bare}"


__all__ = [
    "PYTHON_VENV_MANAGERS",
    "StackProfile",
    "detect_stack_profile",
    "wrap_command",
]
