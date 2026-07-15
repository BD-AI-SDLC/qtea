"""Multi-strategy SUT environment variable resolution.

After Step 6 discovers which env var *keys* the SUT needs, this module
resolves their *values* via a cascade of strategies:

  1. ProcessEnvStrategy     — already in os.environ
  2. DotenvFileStrategy     — parse a .env / .env.example file
  3. AzureDevOpsStrategy    — REST API: Variable Group
  4. InteractivePromptStrategy — Rich terminal prompt (skipped in CI)

Resolved values are injected into ``os.environ`` so downstream steps
(8, 9) pick them up transparently.  Values are never logged but ARE
persisted to ``<workspace>/.env.qtea`` (mode 600) so that
``--from-step`` restarts can recover HITL-provided values without
re-prompting.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from qtea.logging_setup import get_logger

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

# Runtime-essential keys: things a real run cannot proceed without and that
# the user MUST be given the chance to confirm or override interactively —
# endpoints (BASE_URL / API_URL / QA_URL / DATABASE_URL), identity
# (USER / USERNAME / EMAIL / LOGIN / SSO_*), and credentials (PASSWORD).
# Excluded by design: infrastructure / tuning keys (TIMEOUT, BROWSER,
# HEADLESS, WORKERS, RETRIES, LOG_LEVEL, ...). Those usually have defaults
# in code; asking the user about them is noise.
#
# Note: every substring is matched case-insensitively against the key,
# so "QA_URL" matches both "QA_URL" and "PROD_QA_URL_BASE".
_ESSENTIAL_PATTERNS = (
    # endpoints
    "URL", "ENDPOINT", "HOST", "BASE_URL", "API_URL", "QA_URL", "DATABASE_URL",
    # identity / accounts
    "USER", "USERNAME", "EMAIL", "LOGIN", "ACCOUNT", "SSO",
    # credentials
    "PASSWORD", "PASS", "PASSWD", "PWD",
)


def _is_essential_key(key: str) -> bool:
    """Runtime-essential: the user must confirm endpoints/identity/credentials.

    Matched case-insensitively as a substring; explicitly excludes the
    internal/infra keys already filtered upstream (`SECRET_ENV_KEYS`,
    `_INTERNAL_PREFIXES` in `s06_research.py`). False for tuning knobs
    like TIMEOUT / BROWSER / HEADLESS / WORKERS / RETRIES.
    """
    k = key.upper()
    return any(p in k for p in _ESSENTIAL_PATTERNS)


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
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                eq = stripped.find("=")
                if eq > 0:
                    required_from_example.add(stripped[:eq].strip())
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
            # Order matters: dotenv_values entries later in the list override
            # earlier ones. Templates / examples (placeholders) go first;
            # real `.env` / `.env.local` files (actual values) go last so they
            # win. Reading `.env` is what lets `qtea run --from-step 7+`
            # find QA_URL across process restarts — without it, the in-process
            # `os.environ` write from a prior Step 6 run is gone and Step 8
            # aborts with BASE_URL_UNRESOLVED.
            for name in (".env.example", ".env.template", ".env.sample",
                         ".env", ".env.local"):
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
            with contextlib.suppress(OSError):
                combined.update(dotenv_values(p))

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
        org = urllib.parse.quote(self._org, safe="")
        project = urllib.parse.quote(self._project, safe="")
        query = urllib.parse.urlencode(
            {"groupName": self._group, "api-version": "7.1"},
        )
        url = (
            f"https://dev.azure.com/{org}/{project}"
            f"/_apis/distributedtask/variablegroups?{query}"
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
    """HITL confirmation/override for runtime-essential SUT env vars.

    Unlike the silent strategies, this one is invoked specifically for
    *essential* keys regardless of whether they're already resolved —
    the user must always get the chance to confirm or override endpoints,
    identity, and credentials before any test code runs against them.

    Behaviour per key:
      * non-secret with a discovered value → prompt shows the value as
        the default; pressing Enter accepts it, typing overrides it
      * non-secret with no value           → prompt with empty default;
        Enter skips, typing supplies it
      * secret (PASSWORD/TOKEN/AUTH/...)   → prompt is password-masked;
        any discovered default is preserved silently (never echoed)
    """

    name = "interactive"

    def __init__(self, defaults: dict[str, str] | None = None) -> None:
        self._defaults = defaults or {}

    def resolve(
        self,
        keys: list[str],
        already_resolved: dict[str, str],
    ) -> dict[str, str]:
        if not keys:
            return {}

        ui_mode = bool(os.environ.get("QTEA_UI_MODE"))
        if not ui_mode and not sys.stdin.isatty():
            return {}

        # UI mode: route through the shared HITL bridge so the desktop
        # dialog collects the values instead of stdout/stdin (which would
        # otherwise hang the run with an unanswerable terminal prompt).
        # ``hitl.prompt_user`` is already monkey-patched by HitlBridge in
        # UI mode — we just need to package the env keys as Question
        # objects and translate the responses back into env values.
        if ui_mode:
            from qtea.hitl import (
                RESOLUTION_ANSWERED,
                Question,
                prompt_user,
            )

            questions: list[Question] = []
            for k in keys:
                current = self._defaults.get(k, "")
                context = (
                    "value found — press Enter to keep, or type new"
                    if current
                    else "no value found — type to supply"
                )
                questions.append(
                    Question(
                        id=k,
                        kind="env",
                        prompt_text=k,
                        context=context,
                    )
                )

            answers = prompt_user(questions, agent_label="env-resolver")
            out: dict[str, str] = {}
            for k in keys:
                ans = answers.get(k)
                if ans is None:
                    continue
                resolution, val = ans
                if resolution != RESOLUTION_ANSWERED:
                    continue
                val = (val or "").strip()
                if val:
                    out[k] = val
            return out

        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt

        console = Console()
        console.print()
        console.print(
            Panel(
                f"[bold yellow]Step 6[/] needs you to confirm "
                f"[bold]{len(keys)}[/] runtime-essential SUT variable(s) "
                f"(endpoints / identity / credentials).\n"
                f"Press [bold]Enter[/] to accept the discovered default, "
                f"or type a new value to override.",
                title="SUT environment input required",
                border_style="yellow",
            )
        )

        out = {}
        for k in keys:
            current = self._defaults.get(k, "")
            suffix = (
                "[dim](found — Enter to keep, or type new)[/]"
                if current
                else "[dim](not found — type to supply)[/]"
            )
            console.print()
            val = Prompt.ask(
                f"  [green]{k}[/green] {suffix}",
                default=current,
                password=True,
                show_default=False,
            )
            val = (val or "").strip()
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

    Two-phase cascade:
      1. Silent strategies (process env → .env file → AzDO) try every
         discovered key. They write to *resolved* and never block.
      2. Interactive prompt fires **only for runtime-essential keys**
         (endpoints / identity / credentials — see ``_is_essential_key``).
         For each essential, the user sees the discovered value as the
         prompt default and may confirm (Enter) or override (type new).
         Infrastructure keys (TIMEOUT, BROWSER, HEADLESS, WORKERS, ...)
         are NEVER prompted for; if they have no value here, the SUT's
         in-code default wins at runtime.

    ``extra_required`` flags keys as required beyond the ``.env.example``
    and critical-pattern heuristics — typically Pydantic ``BaseSettings``
    fields declared without a default. It does NOT influence which keys
    are prompted for; that's purely the essential-pattern test.
    """
    required, optional = classify_env_keys(
        discovered_keys, sut_path, extra_required=extra_required,
    )
    all_keys = required + optional
    essentials = list(dict.fromkeys(k for k in all_keys if _is_essential_key(k)))

    silent: list[EnvStrategy] = [ProcessEnvStrategy()]

    if config.env_file or config.sut_path:
        silent.append(DotenvFileStrategy(config.env_file, config.sut_path))

    if config.azdo_org and config.azdo_project and config.azdo_variable_group:
        if config.azdo_pat:
            silent.append(
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

    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}

    # Phase 1 — silent strategies over ALL keys.
    for strategy in silent:
        remaining = [k for k in all_keys if k not in resolved]
        if not remaining:
            break
        found = strategy.resolve(remaining, resolved)
        for k, v in found.items():
            resolved[k] = v
            label = strategy.name
            if isinstance(strategy, DotenvFileStrategy):
                label = strategy.source_label
            sources[k] = label

    # Phase 2 — interactive confirmation for essentials NOT yet resolved.
    unconfirmed = [k for k in essentials if k not in resolved]
    if not config.no_hitl and unconfirmed:
        interactive = InteractivePromptStrategy(
            defaults={k: resolved.get(k, "") for k in unconfirmed},
        )
        confirmed = interactive.resolve(unconfirmed, resolved)
        for k, v in confirmed.items():
            # An override is anything the user supplied that differs from
            # what the silent cascade had (or for a previously-missing key,
            # anything they supplied at all).
            if resolved.get(k) != v:
                sources[k] = "interactive"
            resolved[k] = v

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
