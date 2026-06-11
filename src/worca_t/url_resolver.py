"""SUT base-URL discovery.

The pipeline's downstream steps (8 locator resolution, 9 execute) need
`SUT_BASE_URL` set to the URL of the running QA environment of the SUT.

This module derives that URL from the SUT itself, in a deterministic order
that **always prefers a QA environment over staging/prod**. The QA-first
invariant is non-negotiable: the pipeline must never point the test browser
at a production system by accident.

Detection cascade (first hit wins; each step records into `trail`):

  1. BaseSettings alias scan — find Pydantic settings fields whose alias
     matches an `*_URL` pattern. Rank candidates by environment role:
     qa > test > staging > generic-base > prod (last).
  2. Auth-path AST scan — locate the SUT's sign_in/login modules and grep
     for `settings.<field>` references; use that to disambiguate when
     multiple URL fields exist (the field that the auth code reads is the
     "in-use" URL).
  3. `.env` / `.env.example` default values — if the chosen key has a
     non-empty literal value, surface it as the *suggested* value.
  4. `package.json` `scripts.dev|start` — parse `--port`/`-p`/`PORT=`
     and synthesize `http://localhost:<port>`.
  5. Vite / Next / Nuxt configs — `server.port` / `port` literal.
  6. `docker-compose.yml` — `services.*.ports` host mapping.

If nothing yields a URL value, the resolver still returns the *key* it
believes is canonical (e.g. `"QA_URL"`) so that the existing env-resolver
cascade can prompt the user via HITL for the right key.

The resolver is read-only and side-effect-free; the caller is responsible
for injecting any returned value into `os.environ`.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from worca_t.logging_setup import get_logger

log = get_logger(__name__)


# Environment-role ranking. Lower number = higher preference.
# The user's invariant: **QA wins, production loses**.
_ROLE_RANK = {
    "qa": 0,
    "test": 1,
    "staging": 2,
    "stage": 2,
    "dev": 3,
    "development": 3,
    "base": 4,
    "app": 4,
    "api": 4,
    "url": 5,           # plain "URL"
    "prod": 99,
    "production": 99,
    "live": 99,
}


_URL_KEY_RE = re.compile(r"^([A-Z]+)(?:_BASE)?_URL$|^([A-Z]+)_URL_[A-Z]+$|^URL$|^BASE_URL$|^APP_URL$|^API_URL$")
_PORT_RE = re.compile(r"(?:--port|--port[= ]|-p)\s*[= ]?\s*(\d{2,5})\b")
_ENV_PORT_RE = re.compile(r"\bPORT\s*=\s*(\d{2,5})\b")
_VITE_PORT_RE = re.compile(r"\bport\s*:\s*(\d{2,5})\b")
_HARDCODED_LOCALHOST_RE = re.compile(r"https?://localhost:(\d{2,5})\b", re.I)


@dataclass
class UrlCandidate:
    key: str
    role: str  # qa, prod, staging, dev, base, ...
    rank: int
    source_file: str | None = None  # SUT-relative
    field_name: str | None = None  # python attribute name on the settings class
    suggested_value: str | None = None  # if a literal default / env value was found
    suggested_source: str | None = None
    auth_path_consumes: bool = False  # bumps confidence when sign-in code reads it

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UrlResolution:
    """Outcome of `detect_qa_base_url`."""

    key: str | None = None  # the canonical env-var key (e.g. "QA_URL")
    value: str | None = None  # resolved literal value, if any
    source: str | None = None  # "basesettings_alias" | "env_file" | "config_port" | ...
    confidence: float = 0.0
    candidates: list[UrlCandidate] = field(default_factory=list)
    trail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "candidates": [c.as_dict() for c in self.candidates],
            "trail": self.trail,
        }


# ---------------------------------------------------------------------------
# Role classification + ranking
# ---------------------------------------------------------------------------


def _role_for_key(key: str) -> tuple[str, int]:
    """Map an env-var KEY to a (role, rank) pair.

    Heuristic: split the key on underscores, take the first non-URL/BASE
    token, look it up in `_ROLE_RANK`. Default role is "url" (lowest-confidence
    generic) with the highest available rank.
    """
    upper = key.upper()
    if upper in ("URL", "BASE_URL"):
        return "base", _ROLE_RANK["base"]
    if upper in ("APP_URL", "API_URL"):
        return upper.split("_")[0].lower(), _ROLE_RANK["app"]

    # Strip trailing _URL / _BASE_URL.
    stem = re.sub(r"_(?:BASE_)?URL$", "", upper)
    # Take the leading semantic token.
    leading = stem.split("_")[0].lower() if stem else "url"
    if leading in _ROLE_RANK:
        return leading, _ROLE_RANK[leading]
    return "url", _ROLE_RANK["url"]


# ---------------------------------------------------------------------------
# Pydantic BaseSettings field extraction (richer than s06's name-only walker)
# ---------------------------------------------------------------------------


_BASESETTINGS_HINT = re.compile(rb"\bBaseSettings\b")


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_basesettings_base(base: ast.expr) -> bool:
    if isinstance(base, ast.Name):
        return base.id == "BaseSettings"
    if isinstance(base, ast.Attribute):
        return base.attr == "BaseSettings"
    return False


def _extract_env_prefix(class_body: list[ast.stmt]) -> str:
    for stmt in class_body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for sub in stmt.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and sub.targets[0].id == "env_prefix"
                ):
                    lit = _literal_str(sub.value)
                    if lit is not None:
                        return lit
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "model_config"
            and isinstance(stmt.value, ast.Call)
        ):
            for kw in stmt.value.keywords:
                if kw.arg == "env_prefix":
                    lit = _literal_str(kw.value)
                    if lit is not None:
                        return lit
    return ""


def _parse_field_call(call: ast.Call) -> tuple[str | None, str | None]:
    """Return (alias, default_str) from a `Field(...)` call. None if not set."""
    alias: str | None = None
    default: str | None = None
    if call.args:
        first = call.args[0]
        # Field("https://...") — positional default.
        lit = _literal_str(first)
        if lit is not None:
            default = lit
    for kw in call.keywords:
        if kw.arg == "alias":
            v = _literal_str(kw.value)
            if v is not None:
                alias = v
        elif kw.arg == "default":
            v = _literal_str(kw.value)
            if v is not None:
                default = v
    return alias, default


@dataclass
class _SettingsField:
    """Minimal BaseSettings field record consumed by url_resolver."""

    field_name: str
    env_key: str  # alias or (env_prefix + field).upper()
    class_name: str
    source_file: str  # SUT-relative
    default_value: str | None = None


def _scan_settings_fields(sut_path: Path) -> list[_SettingsField]:
    """Walk SUT Python source for Pydantic BaseSettings fields.

    Returns one record per (class, field). Skips fields that don't pattern-match
    an env-var name. Bounded by file size + standard exclusions to stay fast.
    """
    out: list[_SettingsField] = []
    for src in sut_path.glob("**/*.py"):
        if not src.is_file():
            continue
        if src.stat().st_size > 512_000:
            continue
        if any(part in (".git", "node_modules", ".venv", "venv",
                        "__pycache__", "worca-tests")
               for part in src.parts):
            continue
        try:
            raw = src.read_bytes()
        except OSError:
            continue
        if not _BASESETTINGS_HINT.search(raw):
            continue
        import warnings

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(raw, filename=str(src))
        except SyntaxError:
            continue

        try:
            rel = src.relative_to(sut_path).as_posix()
        except ValueError:
            rel = src.as_posix()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(_is_basesettings_base(b) for b in node.bases):
                continue

            env_prefix = _extract_env_prefix(node.body)

            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                field_name = stmt.target.id
                if field_name in ("model_config", "Config"):
                    continue

                alias: str | None = None
                default: str | None = None

                if isinstance(stmt.value, ast.Call):
                    func = stmt.value.func
                    is_field = (
                        (isinstance(func, ast.Name) and func.id == "Field")
                        or (isinstance(func, ast.Attribute) and func.attr == "Field")
                    )
                    if is_field:
                        alias, default = _parse_field_call(stmt.value)
                elif stmt.value is not None:
                    lit = _literal_str(stmt.value)
                    if lit is not None:
                        default = lit

                env_key = alias if alias else (env_prefix + field_name).upper()
                if not re.match(r"^[A-Z][A-Z0-9_]{1,80}$", env_key):
                    continue

                out.append(_SettingsField(
                    field_name=field_name,
                    env_key=env_key,
                    class_name=node.name,
                    source_file=rel,
                    default_value=default,
                ))
    return out


# ---------------------------------------------------------------------------
# Auth-path scan (which URL field does sign_in / login consume?)
# ---------------------------------------------------------------------------


_AUTH_FILE_HINTS = re.compile(
    r"(sign[_-]?in|sign[_-]?on|sso|login|auth|authn|authenticat)",
    re.IGNORECASE,
)
_SETTINGS_ATTR_HINT = re.compile(r"\bsettings\.([a-z_][a-z0-9_]*)\b", re.I)


def _scan_auth_paths(
    sut_path: Path, fields: list[_SettingsField],
) -> set[str]:
    """Return env-keys whose settings attribute is referenced by auth/sign-in code."""
    field_attr_to_key = {f.field_name: f.env_key for f in fields}
    consumed: set[str] = set()
    for src in sut_path.glob("**/*.py"):
        if not src.is_file() or src.stat().st_size > 512_000:
            continue
        if any(part in (".git", "node_modules", ".venv", "venv",
                        "__pycache__", "worca-tests")
               for part in src.parts):
            continue
        if not _AUTH_FILE_HINTS.search(src.name):
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SETTINGS_ATTR_HINT.finditer(text):
            attr = m.group(1)
            if attr in field_attr_to_key:
                consumed.add(field_attr_to_key[attr])
    return consumed


# ---------------------------------------------------------------------------
# Fallback probes (when no BaseSettings field looks URL-shaped)
# ---------------------------------------------------------------------------


def _probe_package_json_port(sut: Path) -> tuple[int | None, str | None]:
    pj = sut / "package.json"
    if not pj.exists():
        return None, None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    scripts = data.get("scripts", {}) or {}
    for key in ("dev", "start", "serve"):
        cmd = scripts.get(key)
        if not cmd:
            continue
        m = _PORT_RE.search(cmd) or _ENV_PORT_RE.search(cmd)
        if m:
            return int(m.group(1)), f"package.json:scripts.{key}"
    return None, None


def _probe_framework_configs(sut: Path) -> tuple[int | None, str | None]:
    for cfg in ("vite.config.ts", "vite.config.js",
                "next.config.js", "next.config.ts", "next.config.mjs",
                "nuxt.config.ts", "nuxt.config.js"):
        p = sut / cfg
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _VITE_PORT_RE.search(text)
        if m:
            return int(m.group(1)), cfg
    return None, None


def _probe_docker_compose(sut: Path) -> tuple[int | None, str | None]:
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        p = sut / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Crude but effective: find first `- "<host>:<container>"` mapping.
        m = re.search(r'-\s*["\']?(\d{2,5}):\d{2,5}', text)
        if m:
            return int(m.group(1)), name
    return None, None


def _probe_readme(sut: Path) -> tuple[str | None, str | None]:
    for name in ("README.md", "README.rst", "readme.md", "Readme.md"):
        p = sut / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _HARDCODED_LOCALHOST_RE.search(text)
        if m:
            return f"http://localhost:{m.group(1)}", name
    return None, None


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


def detect_qa_base_url(sut_path: Path) -> UrlResolution:
    """Return the canonical QA base-URL key (+ value if directly recoverable).

    Caller is responsible for resolving the *value* through the existing env
    resolver cascade if `value` is None — this function never reads .env
    contents for values (that's `env_resolver.py`'s job) but does inspect
    *literal defaults* in code/manifests and surface them as `suggested_value`.
    """
    res = UrlResolution()
    if not sut_path.exists() or not sut_path.is_dir():
        res.trail.append(f"sut path missing: {sut_path}")
        return res

    # Step 1: scan BaseSettings classes.
    fields = _scan_settings_fields(sut_path)
    res.trail.append(f"basesettings_fields={len(fields)}")

    url_fields = [
        f for f in fields if f.env_key.endswith("_URL") or f.env_key == "URL"
    ]
    res.trail.append(f"url_shaped_fields={len(url_fields)}")

    # Step 2: which fields does the auth code consume?
    auth_consumed = _scan_auth_paths(sut_path, fields) if fields else set()
    res.trail.append(f"auth_consumed_keys={sorted(auth_consumed)}")

    candidates: list[UrlCandidate] = []
    for f in url_fields:
        role, rank = _role_for_key(f.env_key)
        # Auth consumption is a soft tiebreaker — never overrides QA-first.
        # Implementation: subtract a small epsilon from rank if auth uses it,
        # so equal-role candidates favor the auth-consumed one.
        cand = UrlCandidate(
            key=f.env_key,
            role=role,
            rank=rank,
            source_file=f.source_file,
            field_name=f.field_name,
            suggested_value=f.default_value,
            suggested_source=("field_default" if f.default_value else None),
            auth_path_consumes=(f.env_key in auth_consumed),
        )
        candidates.append(cand)

    if candidates:
        # Sort by (role rank ASC, auth_path_consumes DESC for tiebreak).
        candidates.sort(key=lambda c: (c.rank, 0 if c.auth_path_consumes else 1))
        res.candidates = candidates
        chosen = candidates[0]
        res.key = chosen.key
        res.source = "basesettings_alias"
        # Confidence: QA + auth-confirmed → 0.95; QA alone → 0.85;
        # generic base → 0.55; production → 0.10 (we strongly avoid it).
        if chosen.role == "qa":
            res.confidence = 0.95 if chosen.auth_path_consumes else 0.85
        elif chosen.role in ("test", "staging", "stage"):
            res.confidence = 0.65
        elif chosen.role in ("base", "app", "api", "url", "dev", "development"):
            res.confidence = 0.55
        else:  # prod / production / live
            res.confidence = 0.10
            res.trail.append(
                f"WARN: only production-role URL fields found ({chosen.key}); "
                "user should explicitly confirm before proceeding"
            )
        if chosen.suggested_value:
            res.value = chosen.suggested_value
            res.trail.append(
                f"value from {chosen.source_file}:{chosen.field_name} default"
            )
        return res

    # Step 3+: no BaseSettings URL fields. Fall back to manifest probes.
    res.trail.append("no_basesettings_url_fields; falling back to manifest probes")

    port, src = _probe_package_json_port(sut_path)
    if port:
        res.key = "SUT_BASE_URL"
        res.value = f"http://localhost:{port}"
        res.source = "package_json_script"
        res.confidence = 0.7
        res.trail.append(f"port={port} from {src}")
        return res

    port, src = _probe_framework_configs(sut_path)
    if port:
        res.key = "SUT_BASE_URL"
        res.value = f"http://localhost:{port}"
        res.source = "framework_config"
        res.confidence = 0.7
        res.trail.append(f"port={port} from {src}")
        return res

    port, src = _probe_docker_compose(sut_path)
    if port:
        res.key = "SUT_BASE_URL"
        res.value = f"http://localhost:{port}"
        res.source = "docker_compose"
        res.confidence = 0.6
        res.trail.append(f"port={port} from {src}")
        return res

    url, src = _probe_readme(sut_path)
    if url:
        res.key = "SUT_BASE_URL"
        res.value = url
        res.source = "readme"
        res.confidence = 0.4
        res.trail.append(f"url={url} from {src}")
        return res

    res.trail.append("no URL signal anywhere in SUT")
    return res


__all__ = [
    "UrlResolution",
    "UrlCandidate",
    "detect_qa_base_url",
]
