"""Shared pytest configuration for the qtea test suite.

Auto-tags the integration-style test modules — those whose tests spawn real
``git`` / subprocesses or do heavy filesystem work — with the ``integration``
marker. This keeps the marker assignment in one visible place instead of
scattering ``pytestmark`` lines across modules, and lets the fast inner loop
skip them::

    pytest -m "not integration"     # fast feedback loop
    pytest                          # full suite (CI default)
"""

from __future__ import annotations

import pytest

# Modules whose tests exercise real `git` (via `commit_step` /
# `ensure_git_repo_and_branch`) or spawn real subprocesses. Everything else is
# a fast, pure-logic unit test.
_INTEGRATION_MODULES = {
    "test_step07_test_architect.py",
    "test_step08_codegen.py",
    "test_step09_execute.py",
    "test_sut_branch.py",
    "test_pipeline.py",
    # Runs real environment / MCP probes (a single check spawns subprocesses
    # and can take ~20s); firmly integration, not a unit test.
    "test_doctor.py",
}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if item.path.name in _INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)
