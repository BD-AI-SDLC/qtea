"""Centralized configuration: env loading, proxy, paths, model map, defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Default per-step timeouts (seconds). All <= MAX_STEP_TIMEOUT_S.
MAX_STEP_TIMEOUT_S = 1800

DEFAULT_STEP_TIMEOUTS: dict[int, int] = {
    1: 600,   # intake
    2: 600,   # refine
    3: 900,   # plan
    4: 1500,  # strategy
    5: 500,   # xray-upload
    6: 900,   # research
    7: 1800,  # test-architect
    8: 1800,  # codegen (multi-phase, multiple LLM calls)
    9: 1500,  # execute + self-heal (heal alone gets HEAL_AGENT_TIMEOUT_S)
    10: 600,  # bug-classifier
    11: 600,  # report
}

# Per-heal-attempt timeout used by Step 9's polyglot-test-fixer invocation.
# A single heal pass typically does: read test source → read POM source →
# read snapshot (MCP or live page) → diagnose → write patch. That's 3–6 min
# of model + tool time on a non-trivial failure. The previous derivation
# (`step_timeout(9) // 4`) yielded 150 s which truncated every heal in
# run 20260611-184450-1fbf3d. Decoupled here so a Step 9 budget bump and a
# heal budget bump are independent knobs.
HEAL_AGENT_TIMEOUT_S = 600

# Markdown size enforcement.
MD_SOFT_LIMIT_LINES = 200
MD_HARD_LIMIT_LINES = 500

# Env var names treated as secrets (masked in logs).
SECRET_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "JIRA_API_TOKEN",
        "JIRA_PAT",
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


def _parse_custom_headers() -> dict[str, str]:
    """Parse ``ANTHROPIC_CUSTOM_HEADERS`` into a header dict.

    Format: ``"key1: value1, key2: value2"`` (comma-separated).
    Returns an empty dict when the env var is unset or empty.
    """
    raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").strip()
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        headers[k.strip()] = v.strip()
    return headers


def anthropic_auth_kwargs() -> dict[str, str]:
    """Return the auth + header kwargs for ``anthropic.AsyncAnthropic`` / ``anthropic.Anthropic``.

    The Anthropic Python SDK has TWO mutually exclusive auth parameters and
    they produce different HTTP headers:

    * ``api_key=<k>``    → ``x-api-key: <k>`` header (raw Anthropic API)
    * ``auth_token=<t>`` → ``Authorization: Bearer <t>`` header (OAuth / model farm proxies)

    The ``claude`` CLI dispatches based on which env var is set:
    ``ANTHROPIC_AUTH_TOKEN`` → Bearer, ``ANTHROPIC_API_KEY`` → x-api-key.
    This helper replicates that logic so direct-SDK callers
    (``llm/reasoning.py``, ``jit_resolver.py``) authenticate correctly
    against either the raw Anthropic API OR a model-farm proxy that
    expects Bearer auth.

    When ``ANTHROPIC_CUSTOM_HEADERS`` is set (e.g. BMF sticky-session
    routing), those headers are injected via ``default_headers`` so
    direct-SDK callers benefit from the same routing as the Claude Code
    CLI subprocess.

    Returns an empty dict when neither env var is set — let the SDK raise
    its standard "no API key" error rather than masking it here.

    For Vertex-routed setups (Google Cloud Vertex AI, or proxies that
    mimic the Vertex API like Bosch's model farm), use
    :func:`use_vertex_backend` + :func:`anthropic_vertex_kwargs` instead.
    """
    kwargs: dict[str, Any] = {}
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        kwargs["auth_token"] = auth_token
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            kwargs["api_key"] = api_key
    custom = _parse_custom_headers()
    if custom:
        kwargs["default_headers"] = custom
    return kwargs


def use_vertex_backend() -> bool:
    """Return True when worca-t should route LLM calls through Vertex AI.

    Detection mirrors the ``claude`` CLI:
      * ``CLAUDE_CODE_USE_VERTEX=1`` (explicit opt-in), OR
      * ``ANTHROPIC_VERTEX_BASE_URL`` set (proxies like Bosch's
        ``aoai-farm.bosch-temp.com/api/google/v1`` that mimic the Vertex API)

    Either signal flips the LLM transport from
    :class:`anthropic.AsyncAnthropic` to :class:`anthropic.AsyncAnthropicVertex`.
    The two clients construct URLs differently (Vertex includes
    region + project in the path) and accept different auth (Vertex uses
    ``access_token``; standard uses ``api_key`` / ``auth_token``).
    """
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
        return True
    if os.environ.get("ANTHROPIC_VERTEX_BASE_URL"):
        return True
    return False


def anthropic_vertex_kwargs() -> dict[str, Any]:
    """Return constructor kwargs for ``anthropic.AsyncAnthropicVertex``.

    Reads the standard Vertex env vars (``ANTHROPIC_VERTEX_BASE_URL``,
    ``ANTHROPIC_VERTEX_PROJECT_ID``, ``CLOUD_ML_REGION``) plus the
    ``access_token`` from ``ANTHROPIC_AUTH_TOKEN``. For proxy setups
    (Bosch model farm) ``project_id`` is often a placeholder like ``"_"``
    and ``region`` is unused because the proxy ignores it.

    When ``ANTHROPIC_CUSTOM_HEADERS`` is set (e.g. BMF sticky-session
    routing), those headers are injected via ``default_headers``.

    Returns only the kwargs that have values in env — the SDK fills in
    sensible defaults for omitted ones (and raises a clear error if a
    truly required one is missing, like project_id when ``CLOUD_ML_REGION``
    isn't set).
    """
    kwargs: dict[str, Any] = {}
    base_url = os.environ.get("ANTHROPIC_VERTEX_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    access_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if access_token:
        kwargs["access_token"] = access_token
    project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    if project_id:
        kwargs["project_id"] = project_id
    region = os.environ.get("CLOUD_ML_REGION")
    if region:
        kwargs["region"] = region
    elif base_url:
        # Bosch-style proxy with custom base_url ignores region but the
        # SDK requires a non-empty value to construct the (then-discarded)
        # URL template. Provide a safe placeholder.
        kwargs["region"] = "us-east5"
    custom = _parse_custom_headers()
    if custom:
        kwargs["default_headers"] = custom
    return kwargs


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
