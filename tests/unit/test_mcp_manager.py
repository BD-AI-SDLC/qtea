"""Tests for MCP config loader + env substitution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qtea.mcp_manager import (
    _substitute_env,
    load_mcp_config,
    stage_empty_mcp_config,
    stage_mcp_config,
)


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv("EMPTY", "")
    out = _substitute_env({"a": "${FOO}", "b": ["x", "${MISSING}"], "c": {"d": "p${FOO}q"}})
    assert out == {"a": "bar", "b": ["x", ""], "c": {"d": "pbarq"}}


def test_load_mcp_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ATLASSIAN_URL", "https://example.atlassian.net")
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "atlassian": {
                "command": "npx",
                "args": ["-y", "atlassian-mcp"],
                "env": {"ATLASSIAN_URL": "${ATLASSIAN_URL}"},
            }
        }
    }), encoding="utf-8")
    servers = load_mcp_config(cfg)
    assert servers["atlassian"].env["ATLASSIAN_URL"] == "https://example.atlassian.net"
    assert servers["atlassian"].command == "npx"


def test_stage_mcp_config(tmp_path: Path):
    src = tmp_path / ".mcp.json"
    src.write_text(json.dumps({"mcpServers": {"x": {"command": "echo"}}}), encoding="utf-8")
    target = tmp_path / "wd"
    out = stage_mcp_config(target, source=src)
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "x" in data["mcpServers"]


def test_stage_empty_mcp_config_writes_no_servers(tmp_path: Path):
    """stage_empty_mcp_config writes an explicitly empty `mcpServers` so
    the SDK reads zero servers, instead of relying on file absence (which
    is ambiguous when setting_sources=['project']).

    The empty config is the default `_stage_resources` writes when
    `run_agent(enable_mcp=False)` — verifying the bytes here so a refactor
    of either side doesn't silently re-enable MCP spawning everywhere.
    """
    target = tmp_path / "wd"
    out = stage_empty_mcp_config(target)
    assert out == target / ".mcp.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"mcpServers": {}}


def test_probe_server_strips_secrets(monkeypatch):
    """SECRET_ENV_KEYS must not appear in the env passed to MCP server probes."""
    import subprocess

    from qtea.config import SECRET_ENV_KEYS
    from qtea.mcp_manager import McpServer, probe_server

    for key in SECRET_ENV_KEYS:
        monkeypatch.setenv(key, f"FAKE_{key}")

    captured_env: dict[str, str] | None = None

    class FakeProc:
        returncode = 0
        pid = 99999
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)
        def kill(self):
            pass
        def terminate(self):
            pass
        def poll(self):
            return 0

    def fake_popen(cmd, *, stdin, stdout, stderr, env, **kw):
        nonlocal captured_env
        captured_env = dict(env)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    server = McpServer(name="test", command="echo", args=[], env={})
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/echo")
    probe_server(server)
    assert captured_env is not None
    for key in SECRET_ENV_KEYS:
        assert key not in captured_env, f"{key} leaked to MCP server probe"


def test_probe_server_passes_resolved_path_to_popen(monkeypatch):
    """Regression: on Windows `npx` resolves to `npx.CMD`; passing the bare
    name to subprocess.Popen fails with WinError 2 because CreateProcess
    doesn't resolve .cmd / .bat wrappers. probe_server must hand Popen the
    full path returned by shutil.which.
    """
    import subprocess

    from qtea.mcp_manager import McpServer, probe_server

    captured_argv: list[str] | None = None

    class FakeProc:
        returncode = 0
        pid = 99998
        stderr = None
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)
        def kill(self): pass
        def terminate(self): pass
        def poll(self): return 0

    def fake_popen(cmd, *, stdin, stdout, stderr, env, **kw):
        nonlocal captured_argv
        captured_argv = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("shutil.which", lambda _: "C:\\nvm4w\\nodejs\\npx.CMD")
    ok, _msg = probe_server(McpServer(name="t", command="npx", args=["-y", "x"], env={}))
    assert ok
    assert captured_argv == ["C:\\nvm4w\\nodejs\\npx.CMD", "-y", "x"]


def test_stage_mcp_config_isolates_per_workdir(tmp_path: Path):
    """Per-call MCP isolation regression: two consecutive stage_mcp_config
    calls into distinct workdirs MUST produce distinct staged config files
    with no shared mutable state. This is what guarantees Step 8a's
    Playwright browser doesn't leak into Step 8b or Step 8 (see
    `mcp_manager` module docstring 'Per-call MCP isolation guarantee')."""
    src = tmp_path / ".mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"playwright": {
            "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"],
        }}}),
        encoding="utf-8",
    )
    wd_a = tmp_path / "step07-workdir"
    wd_b = tmp_path / "step08-workdir"

    staged_a = stage_mcp_config(wd_a, source=src)
    staged_b = stage_mcp_config(wd_b, source=src)

    assert staged_a != staged_b
    assert staged_a.parent == wd_a
    assert staged_b.parent == wd_b
    # Mutating one staged file must not affect the other (proves they're
    # independent files on disk, not a shared symlink or cached object).
    staged_a.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert json.loads(staged_b.read_text(encoding="utf-8"))["mcpServers"]["playwright"]["command"] == "npx"


def test_stage_mcp_config_does_not_rewrite_playwright_args(tmp_path: Path):
    """Regression: qtea must NOT touch the Playwright MCP's `--headless`
    flag based on the CLI `--headed` option. The MCP is a background tool;
    its head state is controlled solely by `.mcp.json`. The CLI flag instead
    controls Step 8's SUT test execution (see `test_runner._strip_headless_flag`).

    Earlier versions appended `--headed` to the MCP args when the CLI flag
    was set, which made `@playwright/mcp` exit with
    `error: unknown option '--headed'` and Step 8 produced empty results.
    """
    src = tmp_path / ".mcp.json"
    base_args = ["-y", "@playwright/mcp@latest", "--headless"]
    src.write_text(
        json.dumps({"mcpServers": {"playwright": {
            "command": "npx", "args": base_args,
        }}}),
        encoding="utf-8",
    )
    out = stage_mcp_config(tmp_path / "wd", source=src)
    args = json.loads(out.read_text(encoding="utf-8"))["mcpServers"]["playwright"]["args"]
    assert args == base_args
    assert "--headed" not in args


# ---------------------------------------------------------------------------
# Per-call env overlay (Step 9 storage-state injection)
# ---------------------------------------------------------------------------


def test_substitute_env_uses_explicit_env_dict_first(monkeypatch):
    """When ``env`` is provided, its values win over ``os.environ`` —
    lets Step 9 inject ``QTEA_STORAGE_STATE_ARG`` per-run without
    mutating process env (which would leak into Step 10+)."""
    monkeypatch.setenv("FOO", "from-os")
    out = _substitute_env({"a": "${FOO}"}, env={"FOO": "from-env-dict"})
    assert out == {"a": "from-env-dict"}


def test_substitute_env_falls_back_to_os_environ_when_token_missing_from_env(monkeypatch):
    """Tokens not in ``env`` still resolve via ``os.environ`` — so the
    overlay is additive, not replacing."""
    monkeypatch.setenv("FROM_OS", "os-value")
    out = _substitute_env(
        {"a": "${FROM_OS}", "b": "${FROM_OVERLAY}"},
        env={"FROM_OVERLAY": "overlay-value"},
    )
    assert out == {"a": "os-value", "b": "overlay-value"}


def test_load_mcp_config_filters_empty_args_after_substitution(tmp_path: Path, monkeypatch):
    """Optional ``${OPTIONAL}`` tokens collapse to empty strings when
    unset; those empty entries must be filtered before reaching the MCP
    subprocess (which would otherwise treat ``""`` as a positional arg
    and error out)."""
    monkeypatch.delenv("OPTIONAL_FLAG", raising=False)
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "x": {
                "command": "echo",
                "args": ["--headless", "${OPTIONAL_FLAG}", "--strict"],
            }
        }
    }), encoding="utf-8")
    servers = load_mcp_config(cfg)
    assert servers["x"].args == ["--headless", "--strict"]


def test_load_mcp_config_threads_env_overlay(tmp_path: Path):
    """Verify the full ``load_mcp_config(env=...)`` round-trip — token
    in args resolves from the overlay, and the resolved arg is preserved
    (not filtered, since it's non-empty)."""
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["-y", "@playwright/mcp", "--headless",
                         "${QTEA_STORAGE_STATE_ARG}"],
            }
        }
    }), encoding="utf-8")
    servers = load_mcp_config(
        cfg,
        env={"QTEA_STORAGE_STATE_ARG": "--storage-state=/abs/path/s.json"},
    )
    assert servers["playwright"].args == [
        "-y", "@playwright/mcp", "--headless",
        "--storage-state=/abs/path/s.json",
    ]


def test_load_mcp_config_http_type(tmp_path: Path):
    """HTTP-type MCP servers must have their type and url fields populated."""
    from qtea.mcp_manager import load_mcp_config
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "context7": {
                "type": "http",
                "url": "https://mcp.context7.com/mcp",
            }
        }
    }), encoding="utf-8")
    servers = load_mcp_config(cfg)
    assert servers["context7"].type == "http"
    assert servers["context7"].url == "https://mcp.context7.com/mcp"
    assert servers["context7"].command == ""


def test_probe_server_http_type_reachable(monkeypatch):
    """probe_server routes http-type servers through _probe_http, not subprocess spawn."""
    import urllib.request
    from qtea.mcp_manager import McpServer, probe_server

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    # _probe_http uses a proxy-aware opener (build_opener().open()), NOT the
    # module-level urlopen — patch the OpenerDirector method the code actually
    # calls, or the probe silently hits the real network.
    monkeypatch.setattr(
        urllib.request.OpenerDirector, "open", lambda self, *a, **kw: FakeResp()
    )
    server = McpServer(name="context7", command="", args=[], env={}, type="http", url="https://mcp.context7.com/mcp")
    ok, detail = probe_server(server)
    assert ok
    assert "http endpoint reachable" in detail


def test_probe_server_http_type_unreachable(monkeypatch):
    """probe_server reports FAIL for unreachable http endpoints."""
    import urllib.request
    from qtea.mcp_manager import McpServer, probe_server

    def fail_open(self, *a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fail_open)
    server = McpServer(name="context7", command="", args=[], env={}, type="http", url="https://mcp.context7.com/mcp")
    ok, detail = probe_server(server)
    assert not ok
    assert "unreachable" in detail


def test_probe_server_http_4xx_counts_as_reachable(monkeypatch):
    """A 4xx response means the server answered — it's reachable even without auth."""
    import urllib.error
    import urllib.request
    from qtea.mcp_manager import McpServer, probe_server

    def raise_404(self, *a, **kw):
        raise urllib.error.HTTPError(url="u", code=404, msg="Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", raise_404)
    server = McpServer(name="context7", command="", args=[], env={}, type="http", url="https://mcp.context7.com/mcp")
    ok, detail = probe_server(server)
    assert ok
    assert "HTTP 404" in detail


def test_stage_mcp_config_threads_env_overlay_and_filters_empty(
    tmp_path: Path, monkeypatch,
):
    """The staged file (what the spawned ``claude`` CLI reads) must match
    ``load_mcp_config`` output: tokens substituted from the overlay AND
    empty args filtered. Otherwise the staged config diverges from what
    the parent process expected."""
    monkeypatch.delenv("ABSENT", raising=False)
    src = tmp_path / ".mcp.json"
    src.write_text(json.dumps({
        "mcpServers": {
            "x": {
                "command": "echo",
                "args": ["--keep", "${ABSENT}", "${SET}"],
            }
        }
    }), encoding="utf-8")
    out = stage_mcp_config(tmp_path / "wd", source=src, env={"SET": "--present"})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mcpServers"]["x"]["args"] == ["--keep", "--present"]


# ---------------------------------------------------------------------------
# npx → direct-node Playwright launch (bypass npx's per-spawn overhead)
# ---------------------------------------------------------------------------


@pytest.mark.use_ambient_playwright_mcp
def test_playwright_npx_rewritten_to_node_when_pinned_install_present(
    tmp_path: Path, monkeypatch,
):
    """With a pinned cli.js resolvable, the npx spec is rewritten to a direct
    ``node cli.js`` launch — dropping ``-y`` + the package spec and keeping the
    tail (``--headless`` + any resolved storage-state arg)."""
    fake_cli = tmp_path / "cli.js"
    fake_cli.write_text("// fake", encoding="utf-8")
    monkeypatch.setenv("QTEA_PLAYWRIGHT_MCP_CLI", str(fake_cli))
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.78", "--headless",
                 "${QTEA_STORAGE_STATE_ARG}"],
    }}}), encoding="utf-8")
    servers = load_mcp_config(
        cfg, env={"QTEA_STORAGE_STATE_ARG": "--storage-state=/abs/s.json"},
    )
    pw = servers["playwright"]
    assert pw.command == "node"
    assert pw.args == [str(fake_cli), "--headless", "--storage-state=/abs/s.json"]


def test_playwright_stays_npx_when_no_pinned_install(tmp_path: Path):
    """Default (no pinned install, via the isolation fixture): the committed npx
    form is preserved — the direct-node rewrite is opt-in on the install."""
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.78", "--headless"],
    }}}), encoding="utf-8")
    pw = load_mcp_config(cfg)["playwright"]
    assert pw.command == "npx"
    assert pw.args == ["-y", "@playwright/mcp@0.0.78", "--headless"]


@pytest.mark.use_ambient_playwright_mcp
def test_stage_mcp_config_rewrites_playwright_to_node(tmp_path: Path, monkeypatch):
    """The staged file (what the spawned claude CLI reads) is the node form,
    with empty optional tokens filtered out of the surviving tail."""
    fake_cli = tmp_path / "cli.js"
    fake_cli.write_text("//", encoding="utf-8")
    monkeypatch.setenv("QTEA_PLAYWRIGHT_MCP_CLI", str(fake_cli))
    monkeypatch.setenv("QTEA_MCP_INSTALL_DIR", str(tmp_path / "empty"))
    src = tmp_path / ".mcp.json"
    src.write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@0.0.78", "--headless", "${MISSING}"],
    }}}), encoding="utf-8")
    out = stage_mcp_config(tmp_path / "wd", source=src)
    data = json.loads(out.read_text(encoding="utf-8"))["mcpServers"]["playwright"]
    assert data["command"] == "node"
    assert data["args"] == [str(fake_cli), "--headless"]


def test_non_playwright_npx_server_never_rewritten(tmp_path: Path, monkeypatch):
    """The rewrite is Playwright-specific — an unrelated npx MCP server is
    left untouched even when a pinned Playwright cli.js exists."""
    fake_cli = tmp_path / "cli.js"
    fake_cli.write_text("//", encoding="utf-8")
    monkeypatch.setenv("QTEA_PLAYWRIGHT_MCP_CLI", str(fake_cli))
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {
        "command": "npx", "args": ["-y", "some-other-mcp", "--flag"],
    }}}), encoding="utf-8")
    other = load_mcp_config(cfg)["other"]
    assert other.command == "npx"
    assert other.args == ["-y", "some-other-mcp", "--flag"]


def test_pinned_playwright_version_parses_config(tmp_path: Path):
    from qtea.mcp_manager import pinned_playwright_version
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx", "args": ["-y", "@playwright/mcp@1.2.3", "--headless"],
    }}}), encoding="utf-8")
    assert pinned_playwright_version(cfg) == "1.2.3"


def test_pinned_playwright_version_fallback_when_absent(tmp_path: Path):
    from qtea.mcp_manager import (
        _DEFAULT_PLAYWRIGHT_MCP_VERSION,
        pinned_playwright_version,
    )
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert pinned_playwright_version(cfg) == _DEFAULT_PLAYWRIGHT_MCP_VERSION


@pytest.mark.use_ambient_playwright_mcp
def test_resolve_playwright_cli_env_override_then_none(tmp_path: Path, monkeypatch):
    from qtea.mcp_manager import resolve_playwright_cli
    real = tmp_path / "cli.js"
    real.write_text("//", encoding="utf-8")
    monkeypatch.setenv("QTEA_PLAYWRIGHT_MCP_CLI", str(real))
    assert resolve_playwright_cli() == real
    # Override points at a missing file → fall through to the (empty) managed dir.
    monkeypatch.setenv("QTEA_PLAYWRIGHT_MCP_CLI", str(tmp_path / "missing.js"))
    monkeypatch.setenv("QTEA_MCP_INSTALL_DIR", str(tmp_path / "empty"))
    assert resolve_playwright_cli() is None


def test_ensure_playwright_mcp_installed_respects_no_auto_install():
    """With auto-install disabled (set by the isolation fixture) and no pinned
    install present, ensure returns False WITHOUT spawning npm."""
    from qtea.mcp_manager import ensure_playwright_mcp_installed
    ok, detail = ensure_playwright_mcp_installed()
    assert ok is False
    assert "auto-install disabled" in detail


# ---------------------------------------------------------------------------
# MCP initialize handshake readiness probe
# ---------------------------------------------------------------------------


def _fake_proc(stdout_lines, stderr_lines=()):
    class _Stream:
        def __init__(self, lines):
            self._lines = list(lines)

        def __iter__(self):
            return iter(self._lines)

    class _Stdin:
        def __init__(self):
            self.written: list[str] = []

        def write(self, s):
            self.written.append(s)

        def flush(self):
            pass

    class _Proc:
        def __init__(self):
            self.stdin = _Stdin()
            self.stdout = _Stream(stdout_lines)
            self.stderr = _Stream(stderr_lines)

        def poll(self):
            return None

    return _Proc()


def test_mcp_handshake_success():
    from qtea.mcp_manager import _mcp_initialize_handshake
    resp = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    ) + "\n"
    ok, detail = _mcp_initialize_handshake(_fake_proc([resp]), timeout_s=5)
    assert ok is True
    assert detail == "mcp initialize ok"


def test_mcp_handshake_skips_non_json_banner():
    from qtea.mcp_manager import _mcp_initialize_handshake
    resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
    ok, _ = _mcp_initialize_handshake(
        _fake_proc(["Listening on stdio\n", resp]), timeout_s=5,
    )
    assert ok is True


def test_mcp_handshake_error_response():
    from qtea.mcp_manager import _mcp_initialize_handshake
    resp = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}}
    ) + "\n"
    ok, detail = _mcp_initialize_handshake(_fake_proc([resp]), timeout_s=5)
    assert ok is False
    assert "initialize error" in detail


def test_mcp_handshake_eof_before_response():
    from qtea.mcp_manager import _mcp_initialize_handshake
    ok, detail = _mcp_initialize_handshake(
        _fake_proc([], stderr_lines=["boom\n"]), timeout_s=5,
    )
    assert ok is False
    assert "closed before responding" in detail
