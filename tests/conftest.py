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

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _stub_aux_agents(request, monkeypatch):
    """Neutralise the failure-path aux agents (debug-RCA + fix-proposal).

    On any ``success=False`` StepResult, ``Step.execute`` fires the debug-RCA
    and fix-proposal chain, which call ``qtea.steps.base.run_agent``. That name
    is bound ONLY in ``base`` and is used ONLY by those aux agents — every real
    step imports ``run_agent`` into its own module namespace, and
    ``run_agent_with_hitl`` has no live callers. So stubbing it here cannot
    touch a main-step agent call.

    Without this, failure-path tests that don't mock the transport make real
    network calls that fail slowly via SDK exponential backoff (~90-227s each,
    dominating the whole suite). The stub returns the same ``success=False``
    outcome those calls already produce, but instantly. Tests that assert on
    aux-agent output patch ``qtea.steps.base.run_agent`` themselves inside the
    test body, which overrides this default and restores it on exit.
    """
    if "no_stub_aux_agents" in request.keywords:
        return
    result = type("_StubAgentResult", (), {
        "success": False,
        "final_text": "",
        "error": "[aux agent stubbed in unit tests]",
        "transcript_path": None,
        # Turn-cap salvage path in _run_debug_rca / _run_fix_proposal reads
        # this attribute; keep it False so the stub mimics a
        # non-turn-cap failure (avoids kicking off the finalizer
        # sub-invocation, which would need its own stubbing).
        "hit_max_turns": False,
    })()
    monkeypatch.setattr(
        "qtea.steps.base.run_agent", AsyncMock(return_value=result),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _isolate_playwright_mcp_install(request, monkeypatch, tmp_path):
    """Isolate tests from any ambient pinned ``@playwright/mcp`` install.

    ``mcp_manager`` rewrites the Playwright server's ``npx`` spec to a direct
    ``node cli.js`` launch whenever a pinned install is resolvable (``~/.qtea/mcp``
    or ``QTEA_PLAYWRIGHT_MCP_CLI``). A dev/CI box that has run ``qtea doctor`` (or
    Step 7/9) WILL have that install, which would silently flip the staged config
    from the npx form to the node form and break tests asserting the npx spec —
    plus trigger real ``npm install`` / browser downloads. Point the resolver at
    an empty temp dir and disable auto-install so, by default, tests see the
    committed npx form and never hit the network. Tests exercising the rewrite
    opt in with ``@pytest.mark.use_ambient_playwright_mcp`` + their own
    ``QTEA_PLAYWRIGHT_MCP_CLI``.
    """
    if "use_ambient_playwright_mcp" in request.keywords:
        return
    monkeypatch.setenv("QTEA_MCP_INSTALL_DIR", str(tmp_path / "no-mcp-install"))
    monkeypatch.setenv("QTEA_MCP_NO_AUTO_INSTALL", "1")
    monkeypatch.delenv("QTEA_PLAYWRIGHT_MCP_CLI", raising=False)


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
