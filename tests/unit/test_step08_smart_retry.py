"""Phase 1: Step 8 POM-extender smart-retry on truncation.

When the POM extender writes a syntactically-invalid file (the symptom of
max_tokens truncation), the orchestrator arms a 2× max_tokens budget on
``ctx.extras`` for the next attempt. Verified at the granularity of
``_extend_poms`` (the function `Step.execute` retries via MAX_ATTEMPTS=2):
each test exercises ONE pass through the function and asserts the override
arming/consumption behavior.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from qtea.steps.s08_codegen import (
    _POM_EXTENDER_MAX_TOKENS_HARD_CAP,
    _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY,
    _extend_poms,
    _PomTask,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_pom(sut_root: Path, rel: str = "src/pages/login.py") -> Path:
    """Write a minimal-but-real Python POM that an extender would target."""
    abs_path = sut_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(
        "class LoginPage:\n"
        "    def __init__(self, page):\n"
        "        self.page = page\n"
        "\n"
        "    def navigate(self):\n"
        "        self.page.goto('/login')\n",
        encoding="utf-8",
    )
    return abs_path


def _make_ctx(extras: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(extras=dict(extras or {}))


def _task(file_path: str, *, method_count: int = 3) -> _PomTask:
    return _PomTask(
        pom_name="LoginPage",
        pom_file=file_path,
        source="reuse",
        from_path=file_path,
        missing_methods=[
            {"name": f"method_{i}", "signature": f"method_{i}(self) -> None"}
            for i in range(method_count)
        ],
    )


def _make_async_result(text: str, stop_reason: str | None = None):
    """Return an object shaped like AgentResult that _extend_one expects."""
    return SimpleNamespace(
        success=True, final_text=text, error=None, stop_reason=stop_reason,
    )


def _patch_llm(
    monkeypatch, returns_text: str, stop_reason: str | None = None,
):
    """Replace s08's call_reasoning_llm with a stub returning the given text.

    Returns the AsyncMock so callers can inspect call_args_list / call_count.
    """
    mock = AsyncMock(
        return_value=_make_async_result(returns_text, stop_reason=stop_reason),
    )
    from qtea.steps import s08_codegen
    monkeypatch.setattr(s08_codegen, "call_reasoning_llm", mock)
    return mock


# Source content that fails ast.parse near EOF — simulates max_tokens
# truncation cutting an agent response mid-`def`. Line 8 (the bad line)
# is the LAST line of an 8-line file, so 8 >= 8*0.66 → truncation_likely.
# Mid-`def` form: `def name` with no opening paren is a SyntaxError.
_TRUNCATED_POM = (
    "class LoginPage:\n"                  # line 1
    "    def __init__(self, page):\n"     # line 2
    "        self.page = page\n"          # line 3
    "    def navigate(self):\n"           # line 4
    "        self.page.goto('/')\n"       # line 5
    "    def method_0(self):\n"           # line 6
    "        return None\n"               # line 7
    "    def click_and_wait_for_popup"    # line 8 — no parens, EOF
)

# Valid source that ast.parse accepts — happy path.
_VALID_POM = (
    "class LoginPage:\n"
    "    def __init__(self, page):\n"
    "        self.page = page\n"
    "    def navigate(self):\n"
    "        self.page.goto('/')\n"
    "    def method_0(self):\n"
    "        return None\n"
)

# Source with a syntax error EARLY in the file — simulates a real logic
# bug rather than truncation. Line 2 of an 8-line file: 2 < 8*0.66.
_EARLY_SYNTAX_ERROR_POM = (
    "class LoginPage:\n"
    "    def __init__(self, page) -> NONESENSE@@:\n"  # line 2 — bad syntax
    "        self.page = page\n"
    "\n"
    "    def navigate(self):\n"
    "        pass\n"
    "    def method_0(self):\n"
    "        return None\n"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_extend_one_arms_smart_retry_on_truncation(
    tmp_path: Path, monkeypatch,
):
    """When the agent returns truncated source (syntax error near EOF), the
    override key is set on ctx.extras at 2× the previous budget."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx()

    _patch_llm(monkeypatch, _TRUNCATED_POM)

    results = await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    assert results == [(rel, False)]
    override = ctx.extras.get(_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY)
    assert isinstance(override, int)
    # Default budget for a small POM is the floor 8000; doubled to 16000.
    assert override == 16000


async def test_extend_one_consumes_override_on_call(
    tmp_path: Path, monkeypatch,
):
    """When the override key is pre-set on ctx.extras, the LLM call receives
    that budget (not the heuristic), AND the key is popped on entry so a
    second invocation in the same step doesn't double-apply it."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx(extras={
        _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY: 24000,
    })

    mock = _patch_llm(monkeypatch, _VALID_POM)

    await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    # Key was popped at the top of _extend_poms.
    assert _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY not in ctx.extras
    # The LLM call used the override budget.
    assert mock.call_count == 1
    assert mock.call_args.kwargs["max_tokens"] == 24000


async def test_override_capped_at_hard_limit(
    tmp_path: Path, monkeypatch,
):
    """A huge override is capped at _POM_EXTENDER_MAX_TOKENS_HARD_CAP (32000)
    so a runaway smart-retry can't blow through the model's output limit."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx(extras={
        _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY: 999_999,
    })

    mock = _patch_llm(monkeypatch, _VALID_POM)

    await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    assert mock.call_args.kwargs["max_tokens"] == _POM_EXTENDER_MAX_TOKENS_HARD_CAP


async def test_smart_retry_not_armed_on_early_syntax_error(
    tmp_path: Path, monkeypatch,
):
    """A syntax error EARLY in the file (line < 66% of total) is treated as
    a real logic bug rather than truncation — override is NOT armed because
    bumping max_tokens won't help fix the bug. The step still fails, but
    the retry will use the same budget."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx()

    _patch_llm(monkeypatch, _EARLY_SYNTAX_ERROR_POM)

    results = await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    assert results == [(rel, False)]
    assert _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY not in ctx.extras


async def test_smart_retry_not_armed_when_ctx_missing(
    tmp_path: Path, monkeypatch,
):
    """When ctx is None (e.g. older callers, direct test invocations), the
    smart-retry path is silently skipped — no AttributeError, no override."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)

    _patch_llm(monkeypatch, _TRUNCATED_POM)

    results = await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=None,  # explicit
    )
    assert results == [(rel, False)]


async def test_smart_retry_armed_via_stop_reason_even_for_early_syntax_error(
    tmp_path: Path, monkeypatch,
):
    """When the LLM signals `stop_reason == 'max_tokens'`, smart-retry is
    armed unconditionally — the heuristic line-position check is bypassed
    because the SDK signal is authoritative. Use the early-syntax-error
    fixture (which the heuristic alone would skip) and assert the override
    still lands because stop_reason takes precedence."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx()

    _patch_llm(monkeypatch, _EARLY_SYNTAX_ERROR_POM, stop_reason="max_tokens")

    results = await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    assert results == [(rel, False)]
    assert ctx.extras.get(_POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY) == 16000


async def test_smart_retry_not_armed_when_stop_reason_is_end_turn(
    tmp_path: Path, monkeypatch,
):
    """`stop_reason == 'end_turn'` means the model finished naturally —
    a syntax error in this case is a real logic bug, not truncation.
    The heuristic falls through to its line-position check; if the bad
    line is mid-file, no override is armed."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    _seed_pom(sut_root, rel)
    ctx = _make_ctx()

    _patch_llm(monkeypatch, _EARLY_SYNTAX_ERROR_POM, stop_reason="end_turn")

    results = await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    assert results == [(rel, False)]
    assert _POM_EXTENDER_MAX_TOKENS_OVERRIDE_KEY not in ctx.extras


async def test_extender_rolls_back_corrupted_file_on_syntax_error(
    tmp_path: Path, monkeypatch,
):
    """The rollback-to-original behavior is preserved alongside smart-retry:
    after a truncated write, the original POM is restored so Phase B.5
    reconciler sees a parseable file (the same fix shipped this morning)."""
    sut_root = tmp_path
    rel = "src/pages/login.py"
    pom_path = _seed_pom(sut_root, rel)
    original = pom_path.read_text(encoding="utf-8")
    ctx = _make_ctx()

    _patch_llm(monkeypatch, _TRUNCATED_POM)

    await _extend_poms(
        pom_tasks={rel: _task(rel)},
        sut_root=sut_root,
        workdir=tmp_path / "wd",
        agents_root=tmp_path / "agents",
        step=8,
        ctx=ctx,
    )
    # File contents are EXACTLY the original — no leftover truncated content.
    assert pom_path.read_text(encoding="utf-8") == original
