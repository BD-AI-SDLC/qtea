"""Bootstrap a local Node.js environment when npx is not on the system PATH.

On first use, `ensure_node()` downloads and installs a self-contained Node.js
environment under ~/.worca-t/.nodeenv/ via the `nodeenv` PyPI package, then
patches os.environ so every subsequent shutil.which("npx") and subprocess call
(including the `claude` CLI spawning MCP servers) finds the bootstrapped npx.

Subsequent calls are instant — the environment is reused once it exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console


def _nodeenv_root() -> Path:
    return Path.home() / ".worca-t" / ".nodeenv"


def _bin_dir() -> Path:
    return _nodeenv_root() / ("Scripts" if sys.platform == "win32" else "bin")


def _npx_name() -> str:
    return "npx.cmd" if sys.platform == "win32" else "npx"


def _nodeenv_ready() -> bool:
    return (_bin_dir() / _npx_name()).exists()


def _activate() -> None:
    """Prepend the nodeenv bin dir to os.environ so shutil.which finds it."""
    bin_dir = str(_bin_dir())
    current = os.environ.get("PATH", "")
    if bin_dir not in current.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + current


def ensure_node(console: Console | None = None) -> bool:
    """Ensure npx is available; bootstrap via nodeenv if not.

    Returns True if npx is available after this call, False if bootstrap failed.
    Patches os.environ["PATH"] in-process so all downstream code finds npx
    without any further configuration.
    """
    if shutil.which("npx"):
        return True

    if _nodeenv_ready():
        _activate()
        return True

    _con = console or Console(stderr=True)
    _con.print(
        "[dim]Node.js not found — bootstrapping local environment "
        f"({_nodeenv_root()}) this may take a minute...[/]"
    )

    root = _nodeenv_root()
    root.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "nodeenv", "--quiet", str(root)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _con.print("[yellow]warn:[/] Node.js bootstrap timed out after 5 minutes.")
        return False
    except Exception as e:
        _con.print(f"[yellow]warn:[/] Node.js bootstrap error: {e}")
        return False

    if result.returncode != 0:
        _con.print(
            f"[yellow]warn:[/] Node.js bootstrap failed:\n{result.stderr.strip()}"
        )
        return False

    _activate()
    _con.print("[dim]Node.js environment ready.[/]")
    return True
