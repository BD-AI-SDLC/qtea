"""worca-t desktop UI (optional ``[ui]`` extra)."""

from __future__ import annotations


def launch() -> None:
    """Entry point for ``worca-t ui`` and the ``worca-t-ui`` console script."""
    import os

    # Set BEFORE anything else imports logging — silences the terminal
    # console handler so the UI owns the screen.
    os.environ["WORCA_T_UI_MODE"] = "1"

    import flet as ft

    from worca_t.ui.app import main

    ft.run(main)
