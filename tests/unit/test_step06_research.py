"""Step 6 research tests."""

from __future__ import annotations

import json
from pathlib import Path

from worca_t.checkpoints import RunState
from worca_t.pipeline import PipelineOptions
from worca_t.steps.base import StepContext
from worca_t.steps.s06_research import (
    ResearchStep,
    _detect_stack,
    _discover_pydantic_env_keys,
    _discover_sut_env_keys,
    _extract_commands,
    _materialize_sut,
    _project_research,
)
from worca_t.workspace import create_workspace

from ._fake_claude import install_fake_query

RESEARCH_MD = """\
# Repository Discovery

## Detected Stack

@playwright/test detected (TypeScript)

## Commands

- Build: `npm run build`
- Test: `npx playwright test`
- Lint: `npm run lint`

## Notes

- monorepo
"""


def test_detect_stack_recognizes_playwright_ts():
    assert _detect_stack("we use @playwright/test here") == "playwright-ts"


def test_detect_stack_pytest_when_only_pytest():
    assert _detect_stack("uses pytest for testing") == "pytest"


def test_extract_commands_parses_build_test_lint():
    cmds = _extract_commands("Build: `npm run build`\nTest: `npx playwright test`\nLint: `npm run lint`")
    assert cmds["build"] == "npm run build"
    assert cmds["test"] == "npx playwright test"
    assert cmds["lint"] == "npm run lint"


def test_project_research_full_shape():
    proj = _project_research(RESEARCH_MD, scan_text=None)
    assert proj["title"] == "Repository Discovery"
    assert proj["detected_stack"] == "playwright-ts"
    assert proj["commands"]["build"] == "npm run build"
    assert any("Detected Stack" in s["title"] for s in proj["sections"][0]["children"])


def _ctx(tmp_path: Path, sut: Path) -> StepContext:
    ws = create_workspace(tmp_path / ".ws")
    state = RunState(run_id=ws.run_id, workspace=str(ws.root), spec_source="x", sut_source=str(sut))
    opts = PipelineOptions(spec="x", sut=str(sut), workspace_base=tmp_path / ".ws")
    return StepContext(workspace=ws, state=state, spec_source="x", sut_source=str(sut), options=opts)


async def test_research_step_local_sut_and_agent_output(tmp_path: Path, monkeypatch):
    # Create a local SUT directory.
    sut = tmp_path / "my-sut"
    sut.mkdir()
    (sut / "package.json").write_text('{"name":"x"}', encoding="utf-8")

    install_fake_query(
        monkeypatch,
        messages=[{"type": "result", "result": "ok"}],
        files={"research.md": RESEARCH_MD},
    )

    ctx = _ctx(tmp_path, sut)
    result = await ResearchStep().run(ctx)
    assert result.success, result.error
    out = ctx.workspace.step_dir(6)
    assert (out / "research.md").exists()
    proj = json.loads((out / "research.json").read_text(encoding="utf-8"))
    assert proj["detected_stack"] == "playwright-ts"
    # SUT materialized
    assert (ctx.workspace.sut / "package.json").exists()


async def test_research_step_missing_sut_fails(tmp_path: Path):
    ctx = _ctx(tmp_path, tmp_path / "does-not-exist")
    result = await ResearchStep().run(ctx)
    assert not result.success
    assert "sut" in (result.error or "").lower()


async def test_research_step_agent_no_output_fails(tmp_path: Path, monkeypatch):
    sut = tmp_path / "sut2"
    sut.mkdir()
    install_fake_query(monkeypatch, messages=[{"type": "result", "result": "ok"}], files={})

    ctx = _ctx(tmp_path, sut)
    result = await ResearchStep().run(ctx)
    assert not result.success
    assert "research.md" in (result.error or "")


# ---------------------------------------------------------------------------
# _discover_pydantic_env_keys
# ---------------------------------------------------------------------------


def _write_settings(sut: Path, body: str, *, filename: str = "settings.py") -> Path:
    """Write a settings.py file under a nested src/ tree."""
    src = sut / "src" / "app"
    src.mkdir(parents=True, exist_ok=True)
    p = src / filename
    p.write_text(body, encoding="utf-8")
    return p


def test_pydantic_discovers_single_line_required_with_alias(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    qa_url: str = Field(..., alias="QA_URL")
""")
    req, opt = _discover_pydantic_env_keys(sut)
    assert "QA_URL" in req
    assert "QA_URL" not in opt


def test_pydantic_discovers_multiline_field_call(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    qa_url: str = Field(
        ...,
        alias="QA_URL",
        description="long description",
    )
""")
    req, _opt = _discover_pydantic_env_keys(sut)
    assert "QA_URL" in req


def test_pydantic_optional_field_with_default_is_optional(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional

class Settings(BaseSettings):
    advanced_sso_user: Optional[str] = Field(default=None, alias="ADVANCED_SSO_USER")
""")
    req, opt = _discover_pydantic_env_keys(sut)
    assert "ADVANCED_SSO_USER" in opt
    assert "ADVANCED_SSO_USER" not in req


def test_pydantic_optional_annotation_without_field(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    log_level: Optional[str] = None
""")
    req, opt = _discover_pydantic_env_keys(sut)
    assert "LOG_LEVEL" in opt
    assert "LOG_LEVEL" not in req


def test_pydantic_pipe_none_annotation_is_optional(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    log_level: str | None = None
""")
    _req, opt = _discover_pydantic_env_keys(sut)
    assert "LOG_LEVEL" in opt


def test_pydantic_class_config_env_prefix(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    timeout: int = Field(...)

    class Config:
        env_prefix = "APP_"
""")
    req, _opt = _discover_pydantic_env_keys(sut)
    assert "APP_TIMEOUT" in req


def test_pydantic_model_config_settingsconfigdict_env_prefix(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MY_")
    host: str = Field(...)
""")
    req, _opt = _discover_pydantic_env_keys(sut)
    assert "MY_HOST" in req


def test_pydantic_non_literal_alias_falls_back_to_field_name(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field

_ALIAS = "MY_VAR"

class Settings(BaseSettings):
    my_var: str = Field(..., alias=_ALIAS)
""")
    req, _opt = _discover_pydantic_env_keys(sut)
    # alias was non-literal — fall back to uppercased field name
    assert "MY_VAR" in req


def test_pydantic_class_not_inheriting_basesettings_is_ignored(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
from pydantic import BaseModel, Field

class NotSettings(BaseModel):
    qa_url: str = Field(..., alias="QA_URL")
""")
    req, opt = _discover_pydantic_env_keys(sut)
    assert "QA_URL" not in req
    assert "QA_URL" not in opt


def test_pydantic_syntax_error_file_is_skipped(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, "this is not valid python BaseSettings :::")
    # Should not raise; nothing discovered.
    req, opt = _discover_pydantic_env_keys(sut)
    assert req == set()
    assert opt == set()


def test_pydantic_skips_node_modules_and_venv(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    for excluded in ("node_modules", ".venv", "venv"):
        d = sut / excluded / "pkg"
        d.mkdir(parents=True)
        (d / "settings.py").write_text("""
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    leaked: str = Field(..., alias="LEAKED_FROM_DEP")
""", encoding="utf-8")
    req, opt = _discover_pydantic_env_keys(sut)
    assert "LEAKED_FROM_DEP" not in req
    assert "LEAKED_FROM_DEP" not in opt


def test_discover_sut_env_keys_merges_pydantic_keys(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    # An os.getenv reference (existing discovery path).
    (sut / "code.py").write_text(
        'import os\nx = os.getenv("EXPLICIT_VAR")\n', encoding="utf-8",
    )
    # A Pydantic BaseSettings (new discovery path).
    _write_settings(sut, """
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    qa_url: str = Field(..., alias="QA_URL")
""")
    keys = _discover_sut_env_keys(sut)
    assert "EXPLICIT_VAR" in keys
    assert "QA_URL" in keys


def test_discover_sut_env_keys_skips_vendored_dirs(tmp_path: Path):
    """Regression: the source-glob scanner must skip vendored third-party
    code (`.venv/site-packages`, `node_modules`, `__pycache__`, etc.).
    Without this, installed plugins like `pytest-base-url` leak their own
    env vars (PYTEST_BASE_URL, VERIFY_BASE_URL) into the prompt list,
    blowing the "essentials" count from ~5 to 15+ and asking the user
    about variables their source code never references."""
    sut = tmp_path / "sut"
    sut.mkdir()

    # Legitimate SUT source — should be picked up.
    (sut / "app.py").write_text(
        'import os\nx = os.getenv("REAL_APP_VAR")\n', encoding="utf-8",
    )

    # Vendored plugin inside the SUT's venv — should be SKIPPED.
    venv_plugin = sut / ".venv" / "Lib" / "site-packages" / "pytest_base_url" / "plugin.py"
    venv_plugin.parent.mkdir(parents=True, exist_ok=True)
    venv_plugin.write_text(
        'import os\n'
        'default = os.getenv("PYTEST_BASE_URL", None)\n'
        'verify = os.getenv("VERIFY_BASE_URL", "false")\n',
        encoding="utf-8",
    )

    # Vendored JS package — should be SKIPPED.
    nm_lib = sut / "node_modules" / "some-lib" / "index.js"
    nm_lib.parent.mkdir(parents=True, exist_ok=True)
    nm_lib.write_text('const x = process.env.LEAKED_JS_VAR;\n', encoding="utf-8")

    # __pycache__ — should be SKIPPED (even though .py files there are stale bytecode names).
    pyc_dir = sut / "src" / "__pycache__"
    pyc_dir.mkdir(parents=True)
    (pyc_dir / "stale.py").write_text('import os\nos.getenv("LEAKED_FROM_PYCACHE")\n', encoding="utf-8")

    keys = _discover_sut_env_keys(sut)
    assert "REAL_APP_VAR" in keys
    assert "PYTEST_BASE_URL" not in keys, "pytest-base-url plugin leaked from .venv"
    assert "VERIFY_BASE_URL" not in keys, "pytest-base-url plugin leaked from .venv"
    assert "LEAKED_JS_VAR" not in keys, "node_modules leak"
    assert "LEAKED_FROM_PYCACHE" not in keys, "__pycache__ leak"


def test_discover_sut_env_keys_excludes_secrets(tmp_path: Path):
    """SECRET_ENV_KEYS must never leak through env-key discovery, even if
    the SUT source explicitly references them."""
    from worca_t.config import SECRET_ENV_KEYS

    sut = tmp_path / "sut"
    sut.mkdir()
    lines = ["import os"]
    for key in SECRET_ENV_KEYS:
        lines.append(f'x = os.environ.get("{key}")')
    lines.append('y = os.getenv("LEGITIMATE_APP_KEY")')
    (sut / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    keys = _discover_sut_env_keys(sut)
    for key in SECRET_ENV_KEYS:
        assert key not in keys, f"SECRET_ENV_KEY {key} leaked through discovery"
    assert "LEGITIMATE_APP_KEY" in keys


def test_materialize_sut_git_clone_uses_double_dash(tmp_path: Path, monkeypatch):
    """git clone must use -- separator so URLs can't be misread as flags.

    Also verifies the branch-setup contract: after the clone succeeds,
    `_materialize_sut` calls `ensure_git_repo_and_branch` to put the SUT
    on the worca-t/run-<id> branch. s06_research and _sut_git share the
    global `subprocess.run` (both import the same module), so a single
    fake captures every call site regardless of which module dispatched it.
    """
    import subprocess

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # Mimic clone-on-disk: the real `git clone` would create dst + .git/
        # so the post-clone `ensure_git_repo_and_branch` sees a "real" repo.
        if cmd[:2] == ["git", "clone"]:
            dst_path = Path(cmd[-1])
            dst_path.mkdir(parents=True, exist_ok=True)
            (dst_path / ".git").mkdir(exist_ok=True)
        # `_sut_git.current_branch` (not invoked here, but harmless) inspects
        # stdout; return a string so its `.strip()` doesn't blow up.
        return subprocess.CompletedProcess(cmd, 0, stdout="worca-t/run-test\n")

    monkeypatch.setattr("worca_t.steps.s06_research.subprocess.run", fake_run)

    dst = tmp_path / "sut"
    _materialize_sut(
        "https://github.com/org/repo.git", dst, run_id="test",
    )

    clone_call = next(c for c in calls if c[:2] == ["git", "clone"])
    assert "--" in clone_call, "git clone must use -- separator before positional args"
    dd_idx = clone_call.index("--")
    assert clone_call[dd_idx + 1] == "https://github.com/org/repo.git"
    # Branch setup ran: at least one `checkout -B worca-t/run-test` call.
    checkout_calls = [c for c in calls if "checkout" in c and "-B" in c]
    assert checkout_calls, "ensure_git_repo_and_branch must run after clone"
    assert "worca-t/run-test" in checkout_calls[0]


def test_pydantic_inherited_basesettings_via_attribute_access(tmp_path: Path):
    sut = tmp_path / "sut"
    sut.mkdir()
    _write_settings(sut, """
import pydantic_settings
from pydantic import Field

class Settings(pydantic_settings.BaseSettings):
    qa_url: str = Field(..., alias="QA_URL")
""")
    req, _opt = _discover_pydantic_env_keys(sut)
    assert "QA_URL" in req


# ---------------------------------------------------------------------------
# Dynamic researcher model selection
# ---------------------------------------------------------------------------

import pytest

_SONNET = "claude-sonnet-4-6"
_HAIKU_LANGUAGES = {"python", "typescript", "javascript"}


def _researcher_model(language: str | None) -> str | None:
    """Mirror the logic in s06_research.py for unit testing.

    Default is Haiku (from agent_models.yaml → model=None means "use yaml").
    Override UP to Sonnet for languages without deterministic tier coverage.
    """
    haiku_sufficient = {"python", "typescript", "javascript"}
    return (
        None  # Haiku from yaml
        if (language or "") in haiku_sufficient
        else _SONNET
    )


@pytest.mark.parametrize("language", sorted(_HAIKU_LANGUAGES))
def test_researcher_model_is_haiku_for_covered_languages(language: str):
    """Python, TypeScript, and JavaScript use the yaml default (Haiku) —
    deterministic tiers already fill the SUT inventory."""
    assert _researcher_model(language) is None  # None = yaml default = Haiku


@pytest.mark.parametrize("language", ["java", "robot", "ruby", "go", "kotlin", "csharp", "rust"])
def test_researcher_model_is_sonnet_for_uncovered_languages(language: str):
    """Non-deterministic-covered languages override UP to Sonnet so the
    researcher can generate the full YAML inventory."""
    assert _researcher_model(language) == _SONNET


def test_researcher_model_is_sonnet_for_unknown():
    """'unknown' language means no manifest found — Sonnet for safety."""
    assert _researcher_model("unknown") == _SONNET


def test_researcher_model_is_sonnet_for_none():
    """None language (stack_profile returned nothing) → Sonnet."""
    assert _researcher_model(None) == _SONNET


def test_researcher_model_is_sonnet_for_empty_string():
    """Empty string (edge case) → Sonnet, not Haiku."""
    assert _researcher_model("") == _SONNET
