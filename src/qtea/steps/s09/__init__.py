"""Step 9 orchestration helpers, split from ``s09_execute.py`` by concern.

Each submodule owns a distinct slice of Step 9's post-run pipeline (patch
validation, heal scope, TBD promotion, overlay sweep, etc.). The parent
``s09_execute`` module re-exports every public/private symbol these modules
own so external callers — including the pytest test suite that pins
``qtea.steps.s09_execute._foo`` monkeypatch paths — continue to work
unchanged.
"""
