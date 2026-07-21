"""Mocking infrastructure for direct-SDK reasoning calls.

Parallel to :mod:`tests.unit._fake_claude` which mocks the Agent SDK
subprocess. This one mocks ``anthropic.AsyncAnthropic`` so tests of
:func:`qtea.llm.reasoning.call_reasoning_llm` (and the step files
that call it) can supply canned responses without hitting a real API.

Typical use::

    install_fake_anthropic(monkeypatch, text='{"x": 1}')
    result = await call_reasoning_llm(...)
    assert result.final_text == '{"x": 1}'

For schema-validation assertions, pair with ``on_call=`` to capture the
kwargs the reasoning module passed to ``messages.create()``::

    captured = {}
    install_fake_anthropic(monkeypatch, text='ok', on_call=captured.update)
    ...
    assert captured["output_config"]["format"]["type"] == "json_schema"
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock


@dataclass
class FakeTextBlock:
    """Mimics ``anthropic.types.TextBlock``."""
    text: str
    type: str = "text"


@dataclass
class FakeUsage:
    """Mimics ``anthropic.types.Usage``."""
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    """Mimics ``anthropic.types.Message`` (the return of ``messages.create``)."""
    content: list[Any]
    stop_reason: str = "end_turn"
    id: str = "msg_fake_001"
    model: str = "claude-fake"
    usage: FakeUsage = field(default_factory=FakeUsage)


def install_fake_anthropic(
    monkeypatch,
    *,
    text: str = "ok",
    texts: list[str] | None = None,
    usage: FakeUsage | None = None,
    raises: Exception | None = None,
    on_call: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncMock:
    """Replace ``anthropic.AsyncAnthropic`` with a fake that returns canned data.

    Parameters
    ----------
    monkeypatch:
        pytest's ``monkeypatch`` fixture.
    text:
        Text to return as the assistant's response content. Single
        text block — sufficient for all reasoning-step tests. Ignored
        when ``texts`` is provided.
    texts:
        Sequence of texts to cycle through on successive ``messages.create``
        calls. Once exhausted, the LAST text is reused for any further
        calls (so over-calling doesn't crash; tests can assert exact
        call counts via ``create_mock.call_count``). Use for retry-loop
        tests that need attempt 1 to fail and attempt 2 to succeed.
    usage:
        Token usage to attach to the response. Defaults to
        :class:`FakeUsage` (100 input, 50 output).
    raises:
        If set, ``messages.create()`` raises this exception instead of
        returning a response. Useful for error-path tests + model
        fallback tests.
    on_call:
        Optional callback invoked with the ``messages.create()`` kwargs
        dict on every call. Use to assert on the request shape (model
        chosen, schema passed, etc.).

    Returns
    -------
    AsyncMock
        The mock standing in for ``messages.create``. Use
        ``.call_count`` / ``.call_args_list`` for assertions on call
        count or call ordering.
    """
    text_sequence = list(texts) if texts is not None else [text]
    if not text_sequence:
        raise ValueError("install_fake_anthropic: `texts` must be non-empty")
    responses = [
        FakeResponse(content=[FakeTextBlock(text=t)], usage=usage or FakeUsage())
        for t in text_sequence
    ]

    create_mock = AsyncMock()
    if raises is not None:
        create_mock.side_effect = raises
    else:
        call_index = {"i": 0}

        async def _capture_and_return(**kwargs):
            if on_call is not None:
                on_call(kwargs)
            i = call_index["i"]
            # Clamp to last response so over-calls don't IndexError.
            response = responses[min(i, len(responses) - 1)]
            call_index["i"] = i + 1
            return response
        create_mock.side_effect = _capture_and_return

    class _FakeStream:
        """Async-context-manager stand-in for ``client.messages.stream(...)``.

        Routes through the same ``create_mock`` so call-count / ``on_call`` /
        ``raises`` / sequence assertions behave identically whether the code
        under test uses ``create()`` or ``stream().get_final_message()``.
        """

        def __init__(self, kwargs: dict[str, Any]):
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def get_final_message(self):
            return await create_mock(**self._kwargs)

    class FakeMessages:
        def __init__(self):
            self.create = create_mock

        def stream(self, **kwargs):
            # Sync method returning an async CM, mirroring the real SDK.
            return _FakeStream(kwargs)

    class FakeClient:
        """Stand-in for ``anthropic.AsyncAnthropic`` / ``AsyncAnthropicVertex``.

        Records the constructor kwargs and which class name was used so tests
        can assert which auth mode + backend was selected without intercepting
        at the SDK boundary.
        """

        last_init_kwargs: dict | None = None
        last_init_class: str | None = None

        def __init__(self, **kwargs):
            FakeClient.last_init_kwargs = kwargs
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    class FakeStandardClient(FakeClient):
        """Mock for ``anthropic.AsyncAnthropic`` (standard API path)."""
        def __init__(self, **kwargs):
            FakeClient.last_init_class = "AsyncAnthropic"
            super().__init__(**kwargs)

    class FakeVertexClient(FakeClient):
        """Mock for ``anthropic.AsyncAnthropicVertex`` (Vertex / model-farm path)."""
        def __init__(self, **kwargs):
            FakeClient.last_init_class = "AsyncAnthropicVertex"
            super().__init__(**kwargs)

    # Reset class-level state between tests so a leftover value from the
    # prior test can't spuriously pass an assertion.
    FakeClient.last_init_kwargs = None
    FakeClient.last_init_class = None
    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeStandardClient)
    monkeypatch.setattr("anthropic.AsyncAnthropicVertex", FakeVertexClient)
    # Also expose the FakeClient class on the anthropic module so tests
    # can introspect last_init_kwargs / last_init_class regardless of
    # which path was taken.
    monkeypatch.setattr("anthropic._fake_init_record", FakeClient, raising=False)
    return create_mock


def disable_vertex_env(monkeypatch) -> None:
    """Strip Vertex-signal env vars so the code under test takes the standard
    ``anthropic.AsyncAnthropic`` path.

    Required for tests that verify the standard-SDK auth dispatch
    (``auth_token`` vs ``api_key``), since the developer's machine may have
    ``CLAUDE_CODE_USE_VERTEX=1`` and ``ANTHROPIC_VERTEX_BASE_URL`` set
    globally (Bosch model-farm setup), which would otherwise route to the
    Vertex branch and skip the assertions.
    """
    for var in (
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


def enable_vertex_env(monkeypatch, *, base_url: str = "https://farm.example/api/google/v1") -> None:
    """Set the Vertex-signal env vars so the code under test takes the
    ``anthropic.AsyncAnthropicVertex`` path.

    Use for tests that exercise the Vertex code path explicitly. Sets a
    placeholder ``base_url`` (overridable) and dummy project_id/region.
    """
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", base_url)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "_")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
