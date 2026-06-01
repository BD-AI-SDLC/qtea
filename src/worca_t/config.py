"""Centralized configuration: env loading, proxy, paths, model map, defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Default per-step timeouts (seconds). All <= MAX_STEP_TIMEOUT_S.
MAX_STEP_TIMEOUT_S = 1800

DEFAULT_STEP_TIMEOUTS: dict[int, int] = {
    1: 600,
    2: 300,
    3: 900,
    4: 1500,
    5: 300,
    6: 900,
    7: 1800,
    8: 1500,
    9: 1800,
    10: 600,
    11: 600,
}

# Markdown size enforcement.
MD_SOFT_LIMIT_LINES = 200
MD_HARD_LIMIT_LINES = 500

# Env var names treated as secrets (masked in logs).
SECRET_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "JIRA_API_TOKEN",
        "JIRA_XRAY_CLIENT_SECRET",
        "JIRA_XRAY_API_KEY",
        "JIRA_XRAY_CLIENT_ID",
        "AZDO_PAT",
    }
)

# Session vars stripped from claude subprocess env to prevent nesting detection.
CLAUDE_SESSION_KEYS = frozenset(
    {
        "CLAUDECODE",
        "AI_AGENT",
        "CLAUDE_CODE_ENTRYPOINT",
    }
)

# Proxy-related env keys propagated to all subprocesses.
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@dataclass(frozen=True)
class Settings:
    """Process-wide settings derived from env + defaults."""

    claude_bin: str = "claude"
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    sut_base_url: str | None = None
    default_workspace: Path = field(default_factory=lambda: Path.home() / ".worca-t")
    max_step_timeout_s: int = MAX_STEP_TIMEOUT_S


def load_env(dotenv_path: Path | None = None) -> None:
    """Load .env if present (idempotent). Does not override existing env vars."""
    if dotenv_path and dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
        return
    candidates = [Path.cwd() / ".env", Path.cwd() / ".env.local"]
    for c in candidates:
        if c.exists():
            load_dotenv(c, override=False)


def get_settings() -> Settings:
    """Construct Settings from current env (call after load_env)."""
    ws_str = os.environ.get("WORCA_T_DEFAULT_WORKSPACE", str(Path.home() / ".worca-t"))
    try:
        timeout = int(
            os.environ.get("WORCA_T_MAX_STEP_TIMEOUT_S", str(MAX_STEP_TIMEOUT_S))
        )
    except ValueError:
        timeout = MAX_STEP_TIMEOUT_S
    timeout = min(timeout, MAX_STEP_TIMEOUT_S)
    return Settings(
        claude_bin=os.environ.get("WORCA_T_CLAUDE_BIN", "claude"),
        anthropic_api_key=os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY"),
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        sut_base_url=os.environ.get("SUT_BASE_URL"),
        default_workspace=Path(ws_str),
        max_step_timeout_s=timeout,
    )


@lru_cache(maxsize=1)
def agent_model_map() -> dict[str, str]:
    """Return the agent->model mapping. Loaded once."""
    # Try installed-package resource first, then dev-tree file.
    try:
        ref = resources.files("worca_t").joinpath("agent_models.yaml")
        with ref.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        pass
    fallback = Path(__file__).parent / "agent_models.yaml"
    if fallback.exists():
        data = yaml.safe_load(fallback.read_text(encoding="utf-8")) or {}
        return {str(k): str(v) for k, v in data.items()}
    return {}


MODEL_FALLBACK_CHAIN: dict[str, list[str]] = {
    "claude-haiku-4-5@20251001": ["claude-sonnet-4-6", "claude-opus-4-6"],
    "claude-sonnet-4-6": ["claude-opus-4-6", "claude-haiku-4-5@20251001"],
    "claude-opus-4-6": ["claude-sonnet-4-6", "claude-haiku-4-5@20251001"],
}


def model_for_agent(agent_key: str) -> str | None:
    """Lookup model id for a given agent key (e.g. 'refine-spec')."""
    return agent_model_map().get(agent_key)


def get_model_chain(primary: str) -> list[str]:
    """Return ``[primary, fallback1, fallback2]`` for resilient model selection."""
    return [primary] + MODEL_FALLBACK_CHAIN.get(primary, [])


def step_timeout(step: int, override: int | None = None) -> int:
    """Resolve per-step timeout with cap."""
    base = override if override is not None else DEFAULT_STEP_TIMEOUTS.get(step, 600)
    return min(base, MAX_STEP_TIMEOUT_S)


def package_resource_root() -> Path:
    """Best-effort path to the package resource root (for agents/, skills/, etc.).

    Resolution order:
      1. ``WORCA_T_RESOURCE_ROOT`` env var (developer override — point at the
         working tree to pick up live edits without reinstalling).
      2. ``_resources`` baked into the installed wheel.
      3. Dev tree (two parents up from this file).
    """
    override = os.environ.get("WORCA_T_RESOURCE_ROOT")
    if override:
        p = Path(override)
        if p.exists():
            return p
    pkg_resources = Path(__file__).parent / "_resources"
    if pkg_resources.exists():
        return pkg_resources
    return Path(__file__).parent.parent.parent
