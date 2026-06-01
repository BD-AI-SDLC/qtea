"""Multi-strategy SUT environment variable resolution.

After Step 6 discovers which env var *keys* the SUT needs, this module
resolves their *values* via a cascade of strategies:

  1. ProcessEnvStrategy     — already in os.environ
  2. DotenvFileStrategy     — parse a .env / .env.example file
  3. AzureDevOpsStrategy    — REST API: Variable Group
  4. InteractivePromptStrategy — Rich terminal prompt (skipped in CI)

Resolved values are injected into ``os.environ`` so downstream steps
(8, 9) pick them up transparently.  Values are never logged or persisted
to disk.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from worca_t.logging_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Key classification
# ---------------------------------------------------------------------------

_CRITICAL_PATTERNS = (
    "BASE_URL", "SUT_", "APP_URL", "API_URL", "DATABASE_URL", "QA_URL",
)

_SECRET_NAME_RE = re.compile(
    r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIALS|AUTH", re.IGNORECASE
)


def classify_env_keys(
    keys: list[str],
    sut_path: Path,
    extra_required: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split *keys* into (required, optional).

    A key is required when it appears in ``.env.example`` (the canonical
    "you must set these" file), matches a critical naming pattern, or is
    present in ``extra_required`` (e.g. Pydantic ``BaseSettings`` fields
    declared without a default).
    """
    required_from_example: set[str] = set()
    env_example = sut_path / ".env.example"
    if env_example.exists():
        try:
            text = env_example.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                eq = line.find("=")
                if eq > 0:
                    required_from_example.add(line[:eq].strip())
        except OSError:
            pass

    extra = extra_required or set()
    required: list[str] = []
    optional: list[str] = []
    for k in keys:
        if (
            k in required_from_example
            or k in extra
            or any(p in k for p in _CRITICAL_PATTERNS)
        ):
            required.append(k)
        else:
            optional.append(k)
    return required, optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvResolverConfig:
    """Derived from CLI flags + env vars."""

    env_file: Path | None = None
    sut_path: Path | None = None
    no_hitl: bool = False
    azdo_org: str | None = None
    azdo_project: str | None = None
    azdo_variable_group: str | None = None
    azdo_pat: str | None = None  # never logged


@dataclass
class ResolvedEnv:
    """Result of the resolution cascade."""

    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Strategy base + implementations
# ---------------------------------------------------------------------------

class EnvStrategy(ABC):
    name: str = ""

    @abstractmethod
    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        """Return ``{key: value}`` for every *key* this strategy can fulfil."""
        ...


class ProcessEnvStrategy(EnvStrategy):
    name = "process_env"

    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for k in keys:
            if k in already_resolved:
                continue
            v = os.environ.get(k)
            if v is not None:
                out[k] = v
        return out


class DotenvFileStrategy(EnvStrategy):
    name = "dotenv"

    def __init__(self, env_file: Path | None, sut_path: Path | None) -> None:
        self._paths: list[Path] = []
        if env_file and env_file.exists():
            self._paths.append(env_file)
        if sut_path:
            for name in (".env.example", ".env.template", ".env.sample"):
                p = sut_path / name
                if p.exists():
                    self._paths.append(p)

    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        if not self._paths:
            return {}

        try:
            from dotenv import dotenv_values
        except ImportError:
            return {}

        combined: dict[str, str | None] = {}
        for p in self._paths:
            try:
                combined.update(dotenv_values(p))
            except OSError:
                pass

        out: dict[str, str] = {}
        for k in keys:
            if k in already_resolved:
                continue
            v = combined.get(k)
            if v is not None and v != "":
                out[k] = v
        return out

    @property
    def source_label(self) -> str:
        if self._paths:
            return f"dotenv:{self._paths[0].name}"
        return "dotenv"


class AzureDevOpsStrategy(EnvStrategy):
    name = "azdo_variable_group"

    def __init__(
        self,
        org: str,
        project: str,
        variable_group: str,
        pat: str,
    ) -> None:
        self._org = org
        self._project = project
        self._group = variable_group
        self._pat = pat

    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        unresolved = [k for k in keys if k not in already_resolved]
        if not unresolved:
            return {}

        variables = self._fetch_variables()
        if variables is None:
            return {}

        out: dict[str, str] = {}
        secret_keys: list[str] = []
        for k in unresolved:
            entry = variables.get(k)
            if entry is None:
                continue
            if entry.get("isSecret"):
                secret_keys.append(k)
                continue
            val = entry.get("value")
            if val is not None:
                out[k] = str(val)

        if secret_keys:
            log.warning(
                "env_resolver.azdo_secret_vars",
                keys=secret_keys,
                hint="Secret variables cannot be retrieved via API; "
                     "set them in your CI pipeline or use --env-file.",
            )
        return out

    def _fetch_variables(self) -> dict | None:
        url = (
            f"https://dev.azure.com/{self._org}/{self._project}"
            f"/_apis/distributedtask/variablegroups"
            f"?groupName={self._group}&api-version=7.1"
        )
        creds = base64.b64encode(f":{self._pat}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            log.warning("env_resolver.azdo_fetch_failed", error=str(exc))
            return None

        groups = data.get("value", [])
        if not groups:
            log.warning(
                "env_resolver.azdo_group_not_found",
                group=self._group,
            )
            return None

        return groups[0].get("variables", {})


class InteractivePromptStrategy(EnvStrategy):
    name = "interactive"

    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        unresolved = [k for k in keys if k not in already_resolved]
        if not unresolved or not sys.stdin.isatty():
            return {}

        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt

        console = Console()
        console.print()
        console.print(
            Panel(
                f"[bold yellow]Step 6[/] discovered [bold]{len(unresolved)}[/] "
                f"required SUT environment variable(s) that are not yet set.\n"
                f"Enter a value for each, or press [bold]Enter[/] to skip.",
                title="SUT environment input required",
                border_style="yellow",
            )
        )

        out: dict[str, str] = {}
        for k in unresolved:
            is_secret = bool(_SECRET_NAME_RE.search(k))
            console.print()
            val = Prompt.ask(
                f"  [green]{k}[/green]",
                default="",
                password=is_secret,
            )
            val = val.strip()
            if val:
                out[k] = val
        return out


# ---------------------------------------------------------------------------
# Top-level resolver
# ---------------------------------------------------------------------------

def resolve_sut_env(
    config: EnvResolverConfig,
    discovered_keys: list[str],
    sut_path: Path,
    extra_required: set[str] | None = None,
) -> ResolvedEnv:
    """Run the full resolution cascade and inject results into ``os.environ``.

    ``extra_required`` flags keys as required beyond the ``.env.example`` and
    critical-pattern heuristics — typically Pydantic ``BaseSettings`` fields
    declared without a default.
    """
    required, optional = classify_env_keys(
        discovered_keys, sut_path, extra_required=extra_required,
    )
    all_keys = required + optional

    strategies: list[EnvStrategy] = [ProcessEnvStrategy()]

    if config.env_file or config.sut_path:
        strategies.append(DotenvFileStrategy(config.env_file, config.sut_path))

    if config.azdo_org and config.azdo_project and config.azdo_variable_group:
        if config.azdo_pat:
            strategies.append(
                AzureDevOpsStrategy(
                    config.azdo_org,
                    config.azdo_project,
                    config.azdo_variable_group,
                    config.azdo_pat,
                )
            )
        else:
            log.warning(
                "env_resolver.azdo_no_pat",
                hint="AZDO_ORG/PROJECT/VARIABLE_GROUP set but AZDO_PAT missing; "
                     "skipping Azure DevOps variable resolution.",
            )

    if not config.no_hitl:
        strategies.append(InteractivePromptStrategy())

    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}

    for strategy in strategies:
        # Interactive prompts only fire for *required* keys. Optional keys
        # (e.g. Pydantic BaseSettings fields with literal defaults like
        # timeout=30, headless=True) must never block the user — if a
        # value is not found in env/.env/AzDO, the SUT's own default
        # wins at runtime. Silent strategies still scope over all keys.
        scope = required if isinstance(strategy, InteractivePromptStrategy) else all_keys
        remaining = [k for k in scope if k not in resolved]
        if not remaining:
            continue
        found = strategy.resolve(remaining, resolved)
        for k, v in found.items():
            resolved[k] = v
            label = strategy.name
            if isinstance(strategy, DotenvFileStrategy):
                label = strategy.source_label
            sources[k] = label

    for k, v in resolved.items():
        os.environ[k] = v

    log.info(
        "env_resolver.resolved",
        count=len(resolved),
        keys=list(resolved.keys()),
        sources=sources,
    )

    missing_req = [k for k in required if k not in resolved]
    missing_opt = [k for k in optional if k not in resolved]

    if missing_req:
        log.warning("env_resolver.missing_required", keys=missing_req)
    if missing_opt:
        log.info("env_resolver.missing_optional", keys=missing_opt)

    return ResolvedEnv(
        values=resolved,
        sources=sources,
        missing_required=missing_req,
        missing_optional=missing_opt,
    )
