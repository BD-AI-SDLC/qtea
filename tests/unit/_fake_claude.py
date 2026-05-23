"""Shared fake-claude shim factory for step integration tests."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from textwrap import dedent


def write_fake_claude(
    bin_dir: Path,
    *,
    events: list[dict] | None = None,
    files: dict[str, str] | None = None,
    exit_code: int = 0,
    sleep_s: float = 0.0,
) -> Path:
    """Install a fake `claude` CLI in `bin_dir`.

    The fake claude script:
      - emits `events` to stdout (NDJSON, one per line)
      - writes each `files[relpath] = content` into its current working dir,
        which matches the agent workdir because run_agent sets cwd=workdir.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload_path = bin_dir / "fake_payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "events": events or [{"type": "result", "result": "ok"}],
                "files": files or {},
                "sleep_s": sleep_s,
                "exit_code": exit_code,
            }
        ),
        encoding="utf-8",
    )

    impl = bin_dir / "fake_claude_impl.py"
    impl.write_text(
        dedent(
            f"""
            import json, os, sys, time
            payload = json.loads(open(r"{payload_path}", "r", encoding="utf-8").read())
            for evt in payload["events"]:
                sys.stdout.write(json.dumps(evt) + "\\n")
                sys.stdout.flush()
            for rel, content in payload.get("files", {{}}).items():
                p = os.path.join(os.getcwd(), rel)
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
            time.sleep(payload.get("sleep_s", 0))
            sys.exit(payload.get("exit_code", 0))
            """
        ),
        encoding="utf-8",
    )

    if os.name == "nt":
        bin_path = bin_dir / "claude.cmd"
        bin_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8"
        )
    else:
        bin_path = bin_dir / "claude"
        bin_path.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "{impl}" "$@"\n',
            encoding="utf-8",
        )
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


def install_on_path(monkeypatch, bin_path: Path) -> None:
    bin_dir = bin_path.parent
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("WORCA_T_CLAUDE_BIN", bin_path.name)
