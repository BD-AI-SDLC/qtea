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
from urllib.parse import urlparse

from qtea.logging_setup import get_logger

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


_URL_KEY_RE = re.compile(
    r"^([A-Z]+)(?:_BASE)?_URL$|^([A-Z]+)_URL_[A-Z]+$|^URL$|^BASE_URL$|^APP_URL$|^API_URL$"
)
_PORT_RE = re.compile(r"(?:--port|--port[= ]|-p)\s*[= ]?\s*(\d{2,5})\b")
_ENV_PORT_RE = re.compile(r"\bPORT\s*=\s*(\d{2,5})\b")
_VITE_PORT_RE = re.compile(r"\bport\s*:\s*(\d{2,5})\b")
_HARDCODED_LOCALHOST_RE = re.compile(r"https?://localhost:(\d{2,5})\b", re.I)
# Any quoted/unquoted absolute http(s) URL literal in a JS/TS config expression.
# Hostnames never contain whitespace, quotes, backticks, commas or closing parens,
# so this cleanly captures each literal branch of a `baseURL:` ternary.
_URL_LITERAL_RE = re.compile(r"https?://[^\s'\"`,)}\]]+")
# `baseURL:` (Playwright) / `baseUrl:` (Cypress) property key. Case-insensitive
# so both spellings (and `baseurl`) match; `\b` + trailing `:` avoid matching
# substrings of unrelated identifiers.
_BASEURL_KEY_RE = re.compile(r"\bbaseurl\s*:", re.IGNORECASE)


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
                        "__pycache__", "qteaests")
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
                        "__pycache__", "qteaests")
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


def _url_role_rank(url: str) -> int:
    """Rank a URL by its host's environment role. QA wins, prod/live lose.

    Mirrors ``_role_for_key`` but classifies the hostname (config baseURLs
    carry the role in the host, e.g. ``grchub-qa.example.com``), not a key.

    Non-production roles require a WHOLE-TOKEN match (host split on ``. - _``)
    so ``latest``/``product``/``backstage`` are not mis-read as test/prod/stage.
    Production-family tokens use a substring match as a deliberate SAFETY net:
    over-flagging a benign host as prod only lowers confidence and warns
    (harmless under the QA-first invariant), whereas UNDER-flagging a real prod
    host (e.g. ``latest-prod`` seen as ``test``) would silently point tests at
    production.
    """
    host = (urlparse(url).hostname or url).lower()
    tokens = set(re.split(r"[.\-_]", host))
    # Check QA/test/staging/dev first, on token boundaries, so a host carrying
    # both (e.g. 'preprod-qa') resolves to the safe QA role.
    for token in ("qa", "test", "staging", "stage", "development", "dev"):
        if token in tokens:
            return _ROLE_RANK[token]
    for token in ("production", "prod", "live"):
        if token in host:
            return _ROLE_RANK[token]
    return _ROLE_RANK["base"]


def _strip_js_comments(text: str) -> str:
    """Remove JS/TS ``//`` line and ``/* */`` block comments, string-aware.

    Preserves comment-like sequences INSIDE string literals (e.g. the ``//`` in
    ``'https://qa.example.com'``) so a commented-out or example ``baseURL:``
    can't be mistaken for the real declaration.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # keep escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _capture_rhs(text: str, start: int) -> str:
    """Capture a property's RHS expression from ``start`` (just after its `:`).

    Scans tracking string state and bracket depth so a multi-line ternary is
    captured whole, stopping at the first top-level comma (property separator)
    or the enclosing object's closing brace.
    """
    i, n, depth = start, len(text), 0
    quote: str | None = None
    chars: list[str] = []
    while i < n:
        ch = text[i]
        if quote is not None:
            chars.append(ch)
            if ch == quote and text[i - 1] != "\\":
                quote = None
        elif ch in "'\"`":
            quote = ch
            chars.append(ch)
        elif ch in "([{":
            depth += 1
            chars.append(ch)
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
            chars.append(ch)
        elif ch == "," and depth == 0:
            break
        else:
            chars.append(ch)
        i += 1
    return "".join(chars)


def _select_base_url(text: str) -> str | None:
    """Pick the QA-preferred absolute URL from a JS/TS config's ``baseURL`` RHS.

    Config files hardcode the base URL as a plain literal or a
    ``process.env.X ? 'urlA' : 'urlB'`` ternary. We strip comments, then across
    EVERY ``baseURL:`` / ``baseUrl:`` declaration (a later ``projects[]``
    override may hold the real value) capture each right-hand-side expression,
    extract every absolute URL literal, drop empties (the ``PROD2 ? '' : ...``
    branch) and interpolated literals (``…${PORT}…`` — not statically
    resolvable), and choose by the QA-first role ranking — tie-breaking to the
    LAST literal, which is the trailing ``else`` (default) branch of a ternary.

    Returns ``None`` when no static absolute URL literal is present (e.g. a
    computed or imported base URL), so the caller's cascade/HITL can resolve it.
    """
    text = _strip_js_comments(text)
    urls: list[str] = []
    for m in _BASEURL_KEY_RE.finditer(text):
        rhs = _capture_rhs(text, m.end())
        for u in _URL_LITERAL_RE.findall(rhs):
            # Drop empties and template-literal-interpolated URLs (the `}` is
            # already excluded by the regex, so an interpolated literal arrives
            # here truncated and still carrying the `${` marker).
            if u.strip() and "${" not in u:
                urls.append(u)
    if not urls:
        return None
    # min role rank wins; among equal ranks prefer the last (else/default) branch.
    best_idx = 0
    best_key = (_url_role_rank(urls[0]), 0)
    for idx, u in enumerate(urls):
        key = (_url_role_rank(u), -idx)
        if key < best_key:
            best_key = key
            best_idx = idx
    return urls[best_idx]


def _probe_playwright_config(sut: Path) -> tuple[str | None, str | None]:
    """Extract ``use.baseURL`` from a Playwright config. Returns (url, file)."""
    for name in ("playwright.config.ts", "playwright.config.js",
                 "playwright.config.mjs", "playwright.config.cjs",
                 "playwright.config.mts", "playwright.config.cts"):
        p = sut / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        url = _select_base_url(text)
        if url:
            return url, name
    return None, None


def _probe_cypress_config(sut: Path) -> tuple[str | None, str | None]:
    """Extract ``baseUrl`` from a Cypress config (modern or legacy JSON)."""
    for name in ("cypress.config.ts", "cypress.config.js",
                 "cypress.config.mjs", "cypress.config.cjs"):
        p = sut / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        url = _select_base_url(text)
        if url:
            return url, name
    # Legacy cypress.json — baseUrl is a JSON string value.
    legacy = sut / "cypress.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            val = (data.get("baseUrl") or "").strip()
            if val.startswith(("http://", "https://")):
                return val, "cypress.json"
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return None, None


# `base_url` / `baseUrl` / `SUT_BASE_URL` etc — a base-url-shaped identifier.
# The `(?:^|_)` boundary excludes `database_url` (which ends in "base_url").
_PY_BASEURL_NAME_RE = re.compile(r"(?:^|_)base_?url$", re.IGNORECASE)
_PY_BASEURL_HINT_RE = re.compile(r"base_?url", re.IGNORECASE)


def _is_http_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _is_base_url_name(name: str | None) -> bool:
    return bool(name) and _PY_BASEURL_NAME_RE.search(name) is not None


def _py_assign_target_name(target: ast.expr) -> str | None:
    """The identifier of a simple assignment target (``x`` or ``self.x``)."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _probe_python_base_url(sut: Path) -> tuple[str | None, str | None]:
    """Scan Python source for a hardcoded base URL the BaseSettings scan misses.

    Covers the common pytest-playwright / conftest patterns that live outside
    a Pydantic settings class:
      - assignment:  ``base_url = "https://qa…"`` / ``self.base_url = "…"``
      - kwarg:       ``browser.new_context(base_url="https://qa…")``
      - fixture:     ``def base_url(): return "https://qa…"``
    Only http(s) string literals to a base-url-shaped name count. Among
    candidates, QA-first ranking wins (tie-break: source path for determinism).
    Returns ``(url, "<rel>:<where>")`` or ``(None, None)``.
    """
    candidates: list[tuple[str, str]] = []
    for src in sut.glob("**/*.py"):
        if not src.is_file() or src.stat().st_size > 512_000:
            continue
        if any(part in (".git", "node_modules", ".venv", "venv",
                        "__pycache__")
               for part in src.parts):
            continue
        try:
            raw = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _PY_BASEURL_HINT_RE.search(raw):  # cheap prefilter
            continue
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(raw, filename=str(src))
        except SyntaxError:
            continue
        try:
            rel = src.relative_to(sut).as_posix()
        except ValueError:
            rel = src.as_posix()

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                val = _literal_str(node.value)
                if val and _is_http_url(val):
                    for t in node.targets:
                        nm = _py_assign_target_name(t)
                        if _is_base_url_name(nm):
                            candidates.append((val, f"{rel}:{nm}"))
            elif isinstance(node, ast.AnnAssign):
                val = _literal_str(node.value)
                if val and _is_http_url(val):
                    nm = _py_assign_target_name(node.target)
                    if _is_base_url_name(nm):
                        candidates.append((val, f"{rel}:{nm}"))
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if _is_base_url_name(kw.arg):
                        val = _literal_str(kw.value)
                        if val and _is_http_url(val):
                            candidates.append((val, f"{rel}:{kw.arg}="))
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if _is_base_url_name(node.name):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Return):
                            val = _literal_str(sub.value)
                            if val and _is_http_url(val):
                                candidates.append((val, f"{rel}:{node.name}()"))

    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (_url_role_rank(c[0]), c[1]))
    return candidates[0]


def _config_url_confidence(url: str) -> float:
    """Confidence for a URL parsed from a Playwright/Cypress config.

    A hardcoded remote QA URL is a strong, deliberate signal (stronger than a
    synthesized ``http://localhost:<port>``). Production/live-only configs get
    low confidence so the QA-first invariant surfaces them for confirmation.
    """
    rank = _url_role_rank(url)
    if rank == _ROLE_RANK["qa"]:
        return 0.85
    if rank >= _ROLE_RANK["prod"]:
        return 0.15
    if rank in (_ROLE_RANK["test"], _ROLE_RANK["staging"]):
        return 0.7
    return 0.75  # base / dev — a real remote URL, still better than a port guess


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

    # A hardcoded remote base URL in a Playwright/Cypress config is a stronger,
    # more deliberate signal than a synthesized http://localhost:<port>, so
    # probe these BEFORE the port probes. Common for browser-test SUTs that
    # point at a shared QA environment (no local dev server).
    for probe, source in (
        (_probe_playwright_config, "playwright_config"),
        (_probe_cypress_config, "cypress_config"),
        (_probe_python_base_url, "python_source"),
    ):
        cfg_url, cfg_file = probe(sut_path)
        if cfg_url:
            res.key = "SUT_BASE_URL"
            res.value = cfg_url
            res.source = source
            res.confidence = _config_url_confidence(cfg_url)
            res.trail.append(f"base_url={cfg_url} from {cfg_file}")
            if _url_role_rank(cfg_url) >= _ROLE_RANK["prod"]:
                res.trail.append(
                    f"WARN: {source} baseURL resolves to a production/live host "
                    f"({cfg_url}); confirm before pointing tests at it"
                )
            return res

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
    "UrlCandidate",
    "UrlResolution",
    "detect_qa_base_url",
]
