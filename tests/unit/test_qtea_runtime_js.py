"""Tests for the vendored JavaScript runtime (``qtea-runtime.js.tpl``).

The template ships as a ``.js.tpl`` file that gets copied verbatim into
Playwright TS/JS SUTs at Step 8 codegen time. Since it's a Node module,
we exercise it via a ``node -e '<script>'`` subprocess harness — the
template is copied into a tmp directory, then a small inline script
requires it and prints the tested API's return value as JSON.

Coverage focuses on the Thread 2 additions:
  - AOM capability ladder + iframe enumeration (``snapshotPage``)
  - ``parseAriaSnapshotYaml`` shape parity with the Python parser
  - On-failure AOM capture (``captureAomOnFailure`` writes to disk)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_TPL = (
    Path(__file__).resolve().parents[2]
    / "src" / "qtea" / "_resources" / "runtime" / "qtea-runtime.js.tpl"
)


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    """Copy the ``.tpl`` into ``tmp_path/qtea-runtime.js`` so node can
    require it. The template is fully self-contained (no build step)."""
    dest = tmp_path / "qtea-runtime.js"
    dest.write_text(_TPL.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _run_node(runtime_dir: Path, script: str, env: dict | None = None) -> dict:
    """Run ``script`` under node with the runtime pre-required as ``rt``.

    The script MUST end with ``console.log(JSON.stringify(<value>))`` —
    that final line is parsed as JSON and returned. Any extra stderr
    output is included in a raised AssertionError on parse failure.

    ``env`` is MERGED into the current process environment (not
    replacement), so node retains SystemRoot / PATH / other essentials
    needed to initialize its crypto subsystem on Windows.
    """
    import os
    node = shutil.which("node") or "node"
    prelude = (
        "const rt = require('./qtea-runtime.js');\n"
        # Silence the runtime's structured log to stderr so JSON on stdout
        # is unambiguous.
        "const _origWrite = process.stderr.write.bind(process.stderr);\n"
        "process.stderr.write = () => true;\n"
        "(async () => {\n"
    )
    postlude = "\n})();\n"
    full = prelude + script + postlude
    merged_env = {**os.environ}
    # Explicit unset: if caller passes env=None but wants a var STRIPPED
    # they can't via this helper — that's OK, they can override to "".
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [node, "-e", full],
        cwd=str(runtime_dir),
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
        )
    stdout = proc.stdout.strip()
    if not stdout:
        raise AssertionError(f"node produced no stdout\nSTDERR:\n{proc.stderr}")
    # Last non-empty line is the JSON payload.
    last = stdout.splitlines()[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"could not parse JSON from node stdout: {e}\nSTDOUT:\n{stdout}\nSTDERR:\n{proc.stderr}"
        ) from e


# ---------------------------------------------------------------------------
# snapshotPage capability ladder
# ---------------------------------------------------------------------------


def test_js_snapshot_prefers_mode_ai_boxes(runtime_dir: Path):
    """Rung A wins first when the fake body accepts both mode and boxes.
    The legacy accessibility path must NOT be invoked."""
    script = """
    const calls = [];
    const page = {
      locator: (sel) => ({
        ariaSnapshot: async (opts) => { calls.push(opts || {}); return '- button "Save"'; },
      }),
      accessibility: { snapshot: async () => { throw new Error("must not be called"); } },
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({ text: result.text, dict: result.dict, calls }));
    """
    out = _run_node(runtime_dir, script)
    assert out["text"] == '- button "Save"'
    assert out["calls"] == [{"mode": "ai", "boxes": True}]
    # Dict shape: {role:"document", name:"", children:[{role:"button",...}]}
    assert out["dict"]["role"] == "document"
    assert out["dict"]["children"][0]["role"] == "button"
    assert out["dict"]["children"][0]["name"] == "Save"


def test_js_snapshot_falls_back_when_boxes_unsupported(runtime_dir: Path):
    """Rung A raises TypeError on `boxes` → ladder descends to Rung B
    (mode-only). Capability cache marks boxes as False."""
    script = """
    const calls = [];
    const page = {
      locator: () => ({
        ariaSnapshot: async (opts) => {
          calls.push(opts || {});
          if (opts && opts.boxes === true) throw new TypeError("unknown option boxes");
          return '- link "Docs"';
        },
      }),
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({
      text: result.text, calls,
      caps: rt.__internal.AOM_CAPS,
    }));
    """
    out = _run_node(runtime_dir, script)
    assert out["text"] == '- link "Docs"'
    assert out["calls"] == [{"mode": "ai", "boxes": True}, {"mode": "ai"}]
    assert out["caps"]["boxes"] is False
    assert out["caps"]["modeAi"] is True


def test_js_snapshot_falls_back_when_mode_unsupported(runtime_dir: Path):
    """Rung A + Rung B raise on `mode` → descends to Rung C (no opts).
    Layer-2 uses this shape when mode='ai' isn't available."""
    script = """
    const calls = [];
    const page = {
      locator: () => ({
        ariaSnapshot: async (opts) => {
          calls.push(opts || {});
          if (opts && opts.mode === "ai") throw new TypeError("unknown option mode");
          return '- main\\n  - button "Home"';
        },
      }),
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({
      text: result.text, calls,
      caps: rt.__internal.AOM_CAPS,
    }));
    """
    out = _run_node(runtime_dir, script)
    assert 'button "Home"' in out["text"]
    # All three rungs attempted on first call.
    assert out["calls"] == [
        {"mode": "ai", "boxes": True},
        {"mode": "ai"},
        {},
    ]
    assert out["caps"]["modeAi"] is False


def test_js_snapshot_falls_back_to_legacy_when_ariaSnapshot_missing(runtime_dir: Path):
    """When the body locator lacks ariaSnapshot (pre-Playwright-1.49),
    fall through to `page.accessibility.snapshot()`."""
    script = """
    const page = {
      locator: () => ({}),  // No ariaSnapshot method.
      accessibility: {
        snapshot: async () => ({ role: "document", name: "legacy", children: [] }),
      },
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({ text: result.text, dict: result.dict }));
    """
    out = _run_node(runtime_dir, script)
    assert out["dict"] == {"role": "document", "name": "legacy", "children": []}
    assert "legacy" in out["text"]


def test_js_snapshot_returns_empty_on_total_failure(runtime_dir: Path):
    """Both APIs unusable → return `{text: "", dict: {}}` so the caller
    still gets a well-formed value (LLM tier says 'no candidates' cleanly)."""
    script = """
    const page = {
      locator: () => { throw new Error("page closed"); },
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({ text: result.text, dict: result.dict }));
    """
    out = _run_node(runtime_dir, script)
    assert out["text"] == ""
    assert out["dict"] == {}


def test_js_snapshot_enumerates_iframes_on_no_kwargs_rung(runtime_dir: Path):
    """Rung C succeeds → iframe subtrees appended manually with markers.
    Non-main frames are included; main_frame is skipped."""
    script = """
    const mainBody = {
      ariaSnapshot: async (opts) => {
        if (opts && (opts.mode || opts.boxes)) throw new TypeError("older PW");
        return '- main\\n  - button "Home"';
      },
    };
    const mainFrame = { __marker: "main" };
    const frameA = {
      url: () => "https://payments.example.com/checkout",
      locator: () => ({ ariaSnapshot: async () => '- form\\n  - textbox "Card"' }),
    };
    const frameB = {
      url: () => "https://help.example.com/chat",
      locator: () => ({ ariaSnapshot: async () => '- region\\n  - button "Send"' }),
    };
    const page = {
      locator: (sel) => mainBody,
      frames: () => [mainFrame, frameA, frameB],
      mainFrame: () => mainFrame,
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({ text: result.text, dict: result.dict }));
    """
    out = _run_node(runtime_dir, script)
    assert "- main" in out["text"]
    assert 'button "Home"' in out["text"]
    assert "# iframe: https://payments.example.com/checkout" in out["text"]
    assert 'textbox "Card"' in out["text"]
    assert "# iframe: https://help.example.com/chat" in out["text"]
    assert 'button "Send"' in out["text"]
    # Main-frame-only dict — heuristic scope guard.
    child_names = [c.get("name") for c in out["dict"]["children"][0]["children"]]
    assert child_names == ["Home"]


def test_js_snapshot_iframe_marker_format(runtime_dir: Path):
    """The label prefers frame.url() over frame.name() when both are
    present (mirrors Python behaviour)."""
    script = """
    const mainBody = {
      ariaSnapshot: async (opts) => {
        if (opts && (opts.mode || opts.boxes)) throw new TypeError("older PW");
        return '- main';
      },
    };
    const mainFrame = { __marker: "main" };
    const bothFrame = {
      url: () => "https://iframe.example.com/embed",
      name: () => "embedded-widget",
      locator: () => ({ ariaSnapshot: async () => '- form' }),
    };
    const page = {
      locator: () => mainBody,
      frames: () => [mainFrame, bothFrame],
      mainFrame: () => mainFrame,
    };
    const result = await rt.__internal.snapshotPage(page);
    console.log(JSON.stringify({ text: result.text }));
    """
    out = _run_node(runtime_dir, script)
    assert "# iframe: https://iframe.example.com/embed" in out["text"]
    assert "# iframe: embedded-widget" not in out["text"]


def test_js_parseAriaSnapshotYaml_produces_main_frame_only_tree(runtime_dir: Path):
    """The parser must skip lines starting with `#` (iframe markers) and
    produce a tree with the same shape as the Python parser — so the JS
    heuristic tier keeps working under modern Playwright."""
    script = """
    const yaml = [
      '- main',
      '  - button "Save"',
      '# iframe: https://sub.example.com',
      '- region',
      '  - textbox "Query"',
    ].join('\\n');
    const tree = rt.__internal.parseAriaSnapshotYaml(yaml);
    console.log(JSON.stringify(tree));
    """
    out = _run_node(runtime_dir, script)
    assert out["role"] == "document"
    # Marker is skipped; both top-level real nodes are captured.
    roles = [c["role"] for c in out["children"]]
    assert roles == ["main", "region"]
    # Nested structure preserved.
    assert out["children"][0]["children"][0]["role"] == "button"
    assert out["children"][0]["children"][0]["name"] == "Save"


# ---------------------------------------------------------------------------
# On-failure AOM capture
# ---------------------------------------------------------------------------


def test_js_capture_writes_file_on_failed_status(runtime_dir: Path, tmp_path: Path):
    """captureAomOnFailure(page, testInfo) writes an AOM snapshot to
    ``<QTEA_WORKSPACE_DIR>/aom-at-failure/<entry_id>.txt`` using the
    Python-compatible ``T-<slug>`` naming convention."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = """
    const page = {
      locator: () => ({ ariaSnapshot: async (opts) => {
        if (opts && (opts.mode || opts.boxes)) throw new TypeError("older PW");
        return '- main\\n  - button "Save"';
      } }),
      frames: () => [],
      mainFrame: () => null,
    };
    const testInfo = {
      file: process.cwd() + "/tests/checkout.spec.ts",
      title: "user can pay",
      status: "failed",
    };
    await rt.__internal.captureAomOnFailure(page, testInfo);
    console.log(JSON.stringify({ done: true }));
    """
    out = _run_node(runtime_dir, script, env={"QTEA_WORKSPACE_DIR": str(workspace)})
    assert out["done"] is True
    aom_dir = workspace / "aom-at-failure"
    assert aom_dir.exists()
    files = list(aom_dir.iterdir())
    assert len(files) == 1
    # Slug: "checkout.spec" stem → "checkout-spec"; combined with title → "checkout-spec-user-can-pay"
    assert files[0].name == "T-checkout-spec-user-can-pay.txt"
    content = files[0].read_text(encoding="utf-8")
    assert 'button "Save"' in content


def test_js_capture_no_op_when_workspace_env_unset(runtime_dir: Path):
    """Without QTEA_WORKSPACE_DIR set, capture returns silently. This is
    the normal condition when tests run outside qtea's harness — never
    breaks the test just because we're not being orchestrated."""
    script = """
    const page = {
      locator: () => ({ ariaSnapshot: async () => '- main' }),
      frames: () => [],
    };
    const testInfo = { file: "x.spec.ts", title: "t", status: "failed" };
    await rt.__internal.captureAomOnFailure(page, testInfo);
    // Verify no file was written into cwd's aom-at-failure.
    const fs = require('fs');
    const exists = fs.existsSync(process.cwd() + '/aom-at-failure');
    console.log(JSON.stringify({ dirCreated: exists }));
    """
    # Explicit empty string — JS `!process.env.QTEA_WORKSPACE_DIR` treats
    # this as "unset". Passing None on Windows leaves the parent-inherited
    # value in place, which we do NOT want for this test.
    out = _run_node(runtime_dir, script, env={"QTEA_WORKSPACE_DIR": ""})
    assert out["dirCreated"] is False
