"""qtea desktop UI (optional ``[ui]`` extra)."""

from __future__ import annotations


def launch() -> None:
    """Entry point for ``qtea ui`` and the ``qtea-ui`` console script."""
    import os

    # Set BEFORE anything else imports logging — silences the terminal
    # console handler so the UI owns the screen.
    os.environ["QTEA_UI_MODE"] = "1"

    import flet as ft

    from qtea.ui.app import main

    ft.run(main)
