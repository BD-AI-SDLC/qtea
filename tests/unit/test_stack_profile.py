"""Unit tests for the deterministic stack-profile detector."""

from __future__ import annotations

from pathlib import Path

from qtea.stack_profile import (
    StackProfile,
    detect_stack_profile,
    wrap_command,
)


def _touch(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-manifest detection (each row of the plan's precedence table)
# ---------------------------------------------------------------------------


def test_detect_poetry(tmp_path: Path):
    _touch(tmp_path / "pyproject.toml", "[tool.poetry]\nname = 'x'\n")
    _touch(tmp_path / "poetry.lock", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "poetry"
    assert p.wrapper_prefix == "poetry run"
    assert p.install_command == "poetry install --no-interaction"
    assert p.language == "python"


def test_detect_poetry_requires_poetry_section(tmp_path: Path):
    # pyproject.toml without a poetry section + poetry.lock should NOT match
    # (some projects keep a stale lock file from older setups). Fall through
    # to other rules — here, nothing else matches, so we get no-signal.
    _touch(tmp_path / "pyproject.toml", "[build-system]\nrequires=['setuptools']\n")
    _touch(tmp_path / "poetry.lock", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager is None


def test_detect_uv(tmp_path: Path):
    _touch(tmp_path / "uv.lock", "")
    _touch(tmp_path / "pyproject.toml", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "uv"
    assert p.wrapper_prefix == "uv run"
    assert p.install_command == "uv sync"


def test_detect_pdm(tmp_path: Path):
    _touch(tmp_path / "pdm.lock", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "pdm"
    assert p.wrapper_prefix == "pdm run"


def test_detect_pipenv(tmp_path: Path):
    _touch(tmp_path / "Pipfile.lock", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "pipenv"
    assert p.wrapper_prefix == "pipenv run"


def test_detect_pip_fallback(tmp_path: Path):
    _touch(tmp_path / "requirements.txt", "pytest\n")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "pip"
    assert ".venv" in p.install_command
    assert "requirements.txt" in p.install_command
    assert "pytest" not in p.install_command  # we install the file, not name pytest


def test_detect_pnpm(tmp_path: Path):
    _touch(tmp_path / "pnpm-lock.yaml", "")
    _touch(tmp_path / "package.json", "{}")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "pnpm"
    assert p.wrapper_prefix == "pnpm exec"


def test_detect_yarn(tmp_path: Path):
    _touch(tmp_path / "yarn.lock", "")
    _touch(tmp_path / "package.json", "{}")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "yarn"
    assert p.wrapper_prefix == "yarn"


def test_detect_npm(tmp_path: Path):
    _touch(tmp_path / "package-lock.json", "{}")
    _touch(tmp_path / "package.json", "{}")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "npm"
    assert p.wrapper_prefix == "npx"
    assert p.install_command == "npm ci"


def test_detect_maven(tmp_path: Path):
    _touch(tmp_path / "pom.xml", "<project/>")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "maven"
    assert p.wrapper_prefix == "mvn"


def test_detect_gradle_kts_with_wrapper(tmp_path: Path):
    _touch(tmp_path / "build.gradle.kts", "")
    _touch(tmp_path / "gradlew", "#!/bin/sh\n")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "gradle"
    assert p.wrapper_prefix == "./gradlew"
    assert "assemble" in p.install_command


def test_detect_gradle_groovy_without_wrapper(tmp_path: Path):
    _touch(tmp_path / "build.gradle", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "gradle"
    assert p.wrapper_prefix == "gradle"


def test_detect_bundler(tmp_path: Path):
    _touch(tmp_path / "Gemfile.lock", "")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "bundler"
    assert p.wrapper_prefix == "bundle exec"


def test_detect_go(tmp_path: Path):
    _touch(tmp_path / "go.mod", "module x\n")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "go"


def test_detect_cargo(tmp_path: Path):
    _touch(tmp_path / "Cargo.toml", "[package]\nname='x'\n")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "cargo"


def test_detect_no_signal(tmp_path: Path):
    p = detect_stack_profile(tmp_path)
    assert p.package_manager is None
    assert p.wrapper_prefix is None
    assert p.notes and "no recognized package manager" in p.notes[0]


def test_detect_missing_path(tmp_path: Path):
    p = detect_stack_profile(tmp_path / "does-not-exist")
    assert p.package_manager is None


# ---------------------------------------------------------------------------
# Precedence: more-specific lockfiles win over generic manifests
# ---------------------------------------------------------------------------


def test_poetry_wins_over_pip(tmp_path: Path):
    # Both poetry.lock + requirements.txt present. Poetry wins.
    _touch(tmp_path / "pyproject.toml", "[tool.poetry]\nname='x'\n")
    _touch(tmp_path / "poetry.lock", "")
    _touch(tmp_path / "requirements.txt", "pytest\n")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "poetry"


def test_pnpm_wins_over_npm(tmp_path: Path):
    _touch(tmp_path / "pnpm-lock.yaml", "")
    _touch(tmp_path / "package-lock.json", "{}")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "pnpm"


def test_yarn_wins_over_npm(tmp_path: Path):
    _touch(tmp_path / "yarn.lock", "")
    _touch(tmp_path / "package-lock.json", "{}")
    p = detect_stack_profile(tmp_path)
    assert p.package_manager == "yarn"


# ---------------------------------------------------------------------------
# env_file_path + start_command discovery
# ---------------------------------------------------------------------------


def test_env_file_path_detected(tmp_path: Path):
    _touch(tmp_path / "package-lock.json", "{}")
    _touch(tmp_path / ".env.example", "QA_URL=http://qa.example.com\n")
    p = detect_stack_profile(tmp_path)
    assert p.env_file_path == ".env.example"


def test_env_file_dotenv_preferred_over_example(tmp_path: Path):
    _touch(tmp_path / "package-lock.json", "{}")
    _touch(tmp_path / ".env", "X=1\n")
    _touch(tmp_path / ".env.example", "X=\n")
    p = detect_stack_profile(tmp_path)
    assert p.env_file_path == ".env"


def test_start_command_from_package_json_dev(tmp_path: Path):
    _touch(tmp_path / "package-lock.json", "{}")
    _touch(
        tmp_path / "package.json",
        '{"scripts": {"dev": "next dev --port 3000"}}',
    )
    p = detect_stack_profile(tmp_path)
    assert p.start_command == "npm run dev"


# ---------------------------------------------------------------------------
# wrap_command: per-manager wrapping rules
# ---------------------------------------------------------------------------


def test_wrap_command_poetry():
    profile = StackProfile(package_manager="poetry", wrapper_prefix="poetry run")
    assert wrap_command(profile, "pytest -m smoke") == "poetry run pytest -m smoke"


def test_wrap_command_idempotent():
    """Calling wrap twice does not double-prefix."""
    profile = StackProfile(package_manager="poetry", wrapper_prefix="poetry run")
    once = wrap_command(profile, "pytest")
    twice = wrap_command(profile, once)
    assert once == twice == "poetry run pytest"


def test_wrap_command_npm():
    profile = StackProfile(package_manager="npm", wrapper_prefix="npx")
    assert wrap_command(profile, "playwright test").startswith("npx playwright test")


def test_wrap_command_maven_no_double_wrap():
    profile = StackProfile(package_manager="maven", wrapper_prefix="mvn")
    # mvn-style commands often arrive already prefixed.
    assert wrap_command(profile, "mvn -B test") == "mvn -B test"


def test_wrap_command_pip_rewrites_first_token():
    """For pip's venv, the wrapper is a bin dir, not a literal command word."""
    profile = StackProfile(package_manager="pip", wrapper_prefix=".venv/bin")
    out = wrap_command(profile, "pytest --junitxml=x.xml")
    assert out == ".venv/bin/pytest --junitxml=x.xml"


def test_wrap_command_none_profile_returns_bare():
    assert wrap_command(None, "pytest") == "pytest"


def test_wrap_command_no_wrapper_prefix_returns_bare():
    profile = StackProfile(package_manager="custom", wrapper_prefix=None)
    assert wrap_command(profile, "pytest") == "pytest"


def test_wrap_command_empty_wrapper_returns_bare():
    profile = StackProfile(package_manager="custom", wrapper_prefix="")
    assert wrap_command(profile, "pytest") == "pytest"
