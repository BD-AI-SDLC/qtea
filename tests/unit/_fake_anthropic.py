"""Mocking infrastructure for direct-SDK reasoning calls.

Parallel to :mod:`tests.unit._fake_claude` which mocks the Agent SDK
subprocess. This one mocks ``anthropic.AsyncAnthropic`` so tests of
:func:`worca_t.llm.reasoning.call_reasoning_llm` (and the step files
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

from dataclasses import dataclass, field
from typing import Any, Callable
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
        text block — sufficient for all reasoning-step tests.
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
    fake_response = FakeResponse(
        content=[FakeTextBlock(text=text)],
        usage=usage or FakeUsage(),
    )

    create_mock = AsyncMock()
    if raises is not None:
        create_mock.side_effect = raises
    else:
        async def _capture_and_return(**kwargs):
            if on_call is not None:
                on_call(kwargs)
            return fake_response
        create_mock.side_effect = _capture_and_return

    class FakeMessages:
        def __init__(self):
            self.create = create_mock

    class FakeClient:
        """Stand-in for ``anthropic.AsyncAnthropic`` supporting ``async with``."""
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("anthropic.AsyncAnthropic", FakeClient)
    return create_mock
