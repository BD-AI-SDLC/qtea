"""``qtea auth-capture`` — one-shot Playwright storageState producer.

Spawns the SUT's own interpreter with a generated wrapper script that:

1. Opens a HEADED Chromium via Playwright (so the user can interactively
   complete MFA / SSO / captcha).
2. Calls the SUT's sign-in helper (resolved from ``sut_inventory.json`` →
   ``auth_flow.entry_method``).
3. Saves the resulting context's ``storage_state(path=<output>)``.

The output file becomes the highest-priority cross-run source consulted
by Step 9's storage-state resolver (``qtea.storage_state.resolve``).
Subsequent ``qtea run`` invocations skip the heal-agent's auth-replay
step entirely because Playwright MCP boots already authenticated.

Supported stacks: Python + Playwright, Node.js (JavaScript / TypeScript)
+ Playwright. Non-Playwright stacks (Selenium, Cypress, Robot) raise
``NotImplementedError`` — the storage-state format is Playwright-specific
(Selenium ``driver.get_cookies()`` would need manual conversion and would
not capture localStorage).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qtea.logging_setup import get_logger
from qtea.proxy import safe_subprocess_env
from qtea.storage_state import mask_path

log = get_logger(__name__)

DEFAULT_OUTPUT_REL = Path(".qtea") / "storage-state.json"

_PLAYWRIGHT_LANGUAGES = frozenset({"python", "javascript", "typescript"})


@dataclass(frozen=True)
class AuthFlowSpec:
    """The bits of ``sut_inventory.json`` ``auth_flow`` the capture needs.

    Pulled into a named dataclass so the wrapper-script generator and the
    error messages don't pass around half-typed dicts.
    """

    entry_method: str   # "tests/fixtures/auth.py:sign_in" — file:symbol
    fixture_entry: str | None  # optional fixture wrapping the helper
    credentials_env_vars: tuple[str, ...]
    language: str  # active module language — "python", "javascript", or "typescript"


def _find_sut_inventory(sut_root: Path) -> dict | None:
    """Locate ``sut_inventory.json`` under the SUT root.

    Modern runs put it under ``<sut>/.qtea/sut_inventory.json`` (the
    convention dir). Older / standalone runs may have it elsewhere; we
    glob shallowly under ``.qtea/`` and ``research/`` if the canonical
    path is missing. Returns the parsed dict on success, ``None`` if not
    found (caller errors out).
    """
    canonical = sut_root / ".qtea" / "sut_inventory.json"
    if canonical.is_file():
        try:
            return json.loads(canonical.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("auth_capture.sut_inventory_unreadable %s", e)
            return None
    for candidate in sut_root.glob(".qtea/*sut_inventory*.json"):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _active_module(inventory: dict) -> dict | None:
    """Resolve the active module entry from ``sut_inventory.json``."""
    name = inventory.get("active_module")
    if not name:
        return None
    for mod in inventory.get("modules") or []:
        if isinstance(mod, dict) and mod.get("name") == name:
            return mod
    return None


def _build_auth_flow_spec(active_module: dict) -> AuthFlowSpec:
    """Extract the auth_flow bits into our typed spec, validating required
    fields. Raises ``ValueError`` with a precise message on each missing
    field so the operator sees exactly what to add to the SUT inventory."""
    auth = active_module.get("auth_flow") or {}
    entry = auth.get("entry_method") or ""
    if not entry or ":" not in entry:
        raise ValueError(
            "active_module.auth_flow.entry_method must be set to "
            "'<file>:<symbol>' (e.g. 'tests/fixtures/auth.py:sign_in'). "
            f"Got: {entry!r}."
        )
    return AuthFlowSpec(
        entry_method=entry,
        fixture_entry=auth.get("fixture_entry"),
        credentials_env_vars=tuple(auth.get("credentials_env_vars") or []),
        language=(active_module.get("language") or "").lower(),
    )


def _resolve_sut_python(sut_root: Path) -> Path:
    """Locate the SUT's venv Python interpreter.

    Tries the standard ``.venv/Scripts/python.exe`` (Windows) /
    ``.venv/bin/python`` (POSIX) layout. Raises ``FileNotFoundError``
    with a clear hint when the venv is absent — the user needs to run
    ``poetry install`` (or equivalent) in the SUT first.
    """
    candidates = [
        sut_root / ".venv" / "Scripts" / "python.exe",
        sut_root / ".venv" / "bin" / "python",
        sut_root / ".venv" / "bin" / "python3",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"No venv Python found under {sut_root}/.venv. "
        f"Run `poetry install` (or `pip install -e .`) in the SUT first."
    )


def _resolve_sut_node(sut_root: Path, language: str) -> list[str]:
    """Locate a Node.js runner capable of executing the auth-capture wrapper.

    Returns the command prefix (e.g. ``["node"]`` or ``["npx", "tsx"]``).
    Raises ``FileNotFoundError`` when Playwright is absent from the SUT's
    ``node_modules`` or Node.js cannot be bootstrapped.
    """
    has_pw = (
        (sut_root / "node_modules" / "playwright").is_dir()
        or (sut_root / "node_modules" / "@playwright" / "test").is_dir()
    )
    if not has_pw:
        raise FileNotFoundError(
            f"No playwright package in {sut_root}/node_modules/. "
            f"Run `npm install playwright` or `npm install @playwright/test` "
            f"in the SUT first."
        )

    from qtea.node_env import ensure_node

    if not ensure_node():
        raise FileNotFoundError(
            "Node.js is not available and auto-bootstrap failed. "
            "Install Node.js manually or check the error above."
        )

    if language == "typescript":
        tsx_name = "tsx.cmd" if os.name == "nt" else "tsx"
        tsx_local = sut_root / "node_modules" / ".bin" / tsx_name
        if tsx_local.is_file():
            return [str(tsx_local)]
        return ["npx", "tsx"]

    node_path = shutil.which("node")
    if not node_path:
        raise FileNotFoundError(
            "node not found on PATH after ensure_node(). "
            "Install Node.js manually."
        )
    return [node_path]


def _wrapper_script(
    spec: AuthFlowSpec, output: Path, sut_root: Path, headed: bool,
) -> str:
    """Generate the Python script the SUT venv will execute.

    The script:
      - Opens ``sync_playwright()`` with chromium (matches Playwright MCP
        default — storage state is per-browser-engine).
      - Imports the sign-in helper from ``entry_method`` and calls it
        with a fresh ``BrowserContext`` (or the page derived from it).
      - Persists ``context.storage_state(path=<output>)``.

    The helper signature is inferred at runtime: a single-arg callable
    is invoked with the context; a no-arg callable is wrapped (the caller
    is expected to create its own context via env / fixtures, in which
    case this capture path doesn't apply — V1 docs the limitation).
    """
    file_part, _, symbol = spec.entry_method.partition(":")
    helper_path = (sut_root / file_part).resolve()
    headless_str = "False" if headed else "True"
    return (
        "import sys, json, inspect\n"
        f"sys.path.insert(0, {str(sut_root)!r})\n"
        "from playwright.sync_api import sync_playwright\n"
        "import importlib.util\n"
        f"_spec = importlib.util.spec_from_file_location('_sut_signin', {str(helper_path)!r})\n"
        "_mod = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_mod)\n"
        f"_fn = getattr(_mod, {symbol!r})\n"
        "with sync_playwright() as _p:\n"
        f"    _browser = _p.chromium.launch(headless={headless_str})\n"
        "    _context = _browser.new_context()\n"
        "    _sig = inspect.signature(_fn)\n"
        "    _params = [p for p in _sig.parameters.values() "
        "if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]\n"
        "    # Heuristic: pass context if helper accepts >=1 positional;\n"
        "    # else assume it builds its own context (capture path won't work).\n"
        "    if _params:\n"
        "        _first = _params[0]\n"
        "        _name = _first.name.lower()\n"
        "        # Pass a Page if the helper expects 'page', else BrowserContext.\n"
        "        if 'page' in _name:\n"
        "            _fn(_context.new_page())\n"
        "        else:\n"
        "            _fn(_context)\n"
        "    else:\n"
        "        raise SystemExit('sign-in helper takes no args — cannot inject "
        "context for capture. Refactor the helper to accept a "
        "BrowserContext or Page argument.')\n"
        f"    _context.storage_state(path={str(output)!r})\n"
        "    _browser.close()\n"
        "print('[auth-capture] saved', "
        f"{str(output)!r})\n"
    )


def _wrapper_script_node(
    spec: AuthFlowSpec, output: Path, sut_root: Path, headed: bool,
) -> str:
    """Generate the Node.js (.mjs) wrapper for auth capture.

    Uses ``createRequire`` anchored at ``sut_root`` so the temp file can
    live in the OS temp dir while still resolving ``playwright`` from the
    SUT's ``node_modules``. The SUT's sign-in helper is loaded via dynamic
    ``import()`` to support both ESM and CJS exports.
    """
    file_part, _, symbol = spec.entry_method.partition(":")
    helper_path = (sut_root / file_part).resolve()
    headless_str = "false" if headed else "true"
    sut_root_json = json.dumps(str(sut_root).replace("\\", "/"))
    helper_json = json.dumps(str(helper_path))
    symbol_json = json.dumps(symbol)
    output_json = json.dumps(str(output))

    return (
        "import { createRequire } from 'module';\n"
        "import { pathToFileURL } from 'url';\n"
        "\n"
        f"const _require = createRequire({sut_root_json} + '/package.json');\n"
        "let chromium;\n"
        "try {\n"
        "  chromium = _require('playwright').chromium;\n"
        "} catch {\n"
        "  chromium = _require('@playwright/test').chromium;\n"
        "}\n"
        "\n"
        f"const helperPath = {helper_json};\n"
        f"const symbol = {symbol_json};\n"
        f"const outputPath = {output_json};\n"
        "\n"
        "const mod = await import(pathToFileURL(helperPath).href);\n"
        "let fn = mod[symbol];\n"
        "if (!fn && mod.default) {\n"
        "  fn = typeof mod.default === 'function' && symbol === 'default'\n"
        "    ? mod.default\n"
        "    : mod.default[symbol];\n"
        "}\n"
        "if (typeof fn !== 'function') {\n"
        "  const names = Object.keys(mod).join(', ') || '(none)';\n"
        "  console.error("
        "`Could not find export '${symbol}' in ${helperPath}. "
        "Available: ${names}`);\n"
        "  process.exit(1);\n"
        "}\n"
        "\n"
        f"const browser = await chromium.launch({{ headless: {headless_str} }});\n"
        "const context = await browser.newContext();\n"
        "\n"
        "const fnStr = fn.toString();\n"
        "const paramMatch = fnStr.match("
        "/^\\s*(?:async\\s+)?(?:function\\s*\\w*\\s*)?\\(?\\s*(\\w+)/);\n"
        "const firstParam = (paramMatch?.[1] || '').toLowerCase();\n"
        "\n"
        "if (fn.length > 0) {\n"
        "  if (firstParam.includes('page')) {\n"
        "    const page = await context.newPage();\n"
        "    await fn(page);\n"
        "  } else {\n"
        "    await fn(context);\n"
        "  }\n"
        "} else {\n"
        "  console.error("
        "'sign-in helper takes no args — cannot inject context for capture.');\n"
        "  process.exit(1);\n"
        "}\n"
        "\n"
        "await context.storageState({ path: outputPath });\n"
        "await browser.close();\n"
        f"console.log('[auth-capture] saved', {output_json});\n"
    )


def _set_owner_only_perms(path: Path) -> None:
    """Set file mode 0o600 on POSIX; on Windows, log a note (no reliable
    cross-version chmod equivalent — owner-only is the default on a
    personal Windows account, but we don't enforce it)."""
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError as e:
            log.warning("auth_capture.chmod_failed %s", e)
    else:
        log.info(
            "auth_capture.windows_perms_note "
            "path=%s note='file permissions follow Windows ACL defaults; "
            "verify owner-only access if storing tokens for shared accounts'",
            path,
        )


def cmd_auth_capture(
    sut: Path,
    output: Path | None = None,
    headed: bool = True,
    timeout_s: int = 600,
) -> Path:
    """Drive the SUT's sign-in helper to produce a storageState.json.

    Returns the absolute path of the written file. Raises on any
    unrecoverable error (missing inventory, missing auth_flow, missing
    venv, non-Playwright SUT, subprocess failure) with an actionable
    message.
    """
    sut_root = Path(sut).resolve()
    if not sut_root.is_dir():
        raise FileNotFoundError(f"SUT path not found or not a directory: {sut_root}")

    inventory = _find_sut_inventory(sut_root)
    if inventory is None:
        raise FileNotFoundError(
            f"No sut_inventory.json found under {sut_root}/.qtea/. "
            f"Run a normal `qtea run` first so Step 6 can produce one."
        )
    module = _active_module(inventory)
    if module is None:
        raise ValueError(
            "sut_inventory.json has no active_module set (or it points at "
            "a name not in modules[]). Fix the inventory and retry."
        )
    spec = _build_auth_flow_spec(module)
    if spec.language not in _PLAYWRIGHT_LANGUAGES:
        raise NotImplementedError(
            f"auth-capture supports Python and Node.js (JS/TS) Playwright "
            f"SUTs (active_module language is {spec.language!r}). For other "
            f"stacks, produce a Playwright-format storageState.json manually "
            f"and pass it via --storage-state."
        )

    out_path = (output or (sut_root / DEFAULT_OUTPUT_REL)).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if spec.language == "python":
        cmd_prefix = [str(_resolve_sut_python(sut_root))]
        script_src = _wrapper_script(spec, out_path, sut_root, headed=headed)
        suffix = "_qtea_auth_capture.py"
    else:
        cmd_prefix = _resolve_sut_node(sut_root, spec.language)
        script_src = _wrapper_script_node(spec, out_path, sut_root, headed=headed)
        suffix = "_qtea_auth_capture.mjs"

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=suffix, delete=False,
    ) as fh:
        fh.write(script_src)
        wrapper_path = Path(fh.name)

    try:
        log.info(
            "auth_capture.spawn cmd=%s wrapper=%s output=%s headed=%s",
            cmd_prefix, wrapper_path, mask_path(out_path), headed,
        )
        env = safe_subprocess_env(isolate_venv=True)
        proc = subprocess.run(
            cmd_prefix + [str(wrapper_path)],
            cwd=str(sut_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"auth-capture timed out after {timeout_s}s — increase "
            f"--timeout if MFA / SSO needs more interactive time."
        ) from e
    finally:
        with contextlib.suppress(OSError):
            wrapper_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        # Surface stderr tail so the operator sees the actual error.
        stderr_tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise RuntimeError(
            f"auth-capture failed with exit code {proc.returncode}. "
            f"stderr tail:\n" + "\n".join(stderr_tail)
        )

    if not out_path.is_file():
        raise RuntimeError(
            f"auth-capture ran successfully but no file was written at "
            f"{out_path}. Inspect the SUT's sign-in helper — it must call "
            f"context.storage_state(path=...) or the wrapper must succeed."
        )

    _set_owner_only_perms(out_path)
    log.info(
        "auth_capture.success path=%s creds_env=%s hint='subsequent "
        "qtea run / Step 9 heal will reuse this storage state via "
        "the SUT convention path resolution'",
        mask_path(out_path), list(spec.credentials_env_vars),
    )
    return out_path


__all__ = [
    "DEFAULT_OUTPUT_REL",
    "AuthFlowSpec",
    "cmd_auth_capture",
]
