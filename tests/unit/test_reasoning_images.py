"""Tests for image (multimodal) content blocks in the direct-SDK transport."""

from __future__ import annotations

import json
from pathlib import Path

from qtea.llm.reasoning import _redact_images_in_messages, call_reasoning_llm
from tests.unit._fake_anthropic import install_fake_anthropic

_IMG_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
}


def _agent(tmp_path: Path) -> Path:
    p = tmp_path / "a.agent.md"
    p.write_text("# agent", encoding="utf-8")
    return p


async def test_no_images_sends_bare_string(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)

    await call_reasoning_llm(
        agent_path=_agent(tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="hi",
        model="claude-sonnet-4-6",
    )

    content = captured["messages"][-1]["content"]
    assert isinstance(content, str)
    assert content == "hi"


async def test_images_send_content_block_list(tmp_path, monkeypatch):
    captured: dict = {}
    install_fake_anthropic(monkeypatch, text="ok", on_call=captured.update)

    await call_reasoning_llm(
        agent_path=_agent(tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="describe",
        model="claude-sonnet-4-6",
        images=[_IMG_BLOCK],
    )

    content = captured["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == _IMG_BLOCK
    # The real request keeps the base64 data intact.
    assert content[1]["source"]["data"] == "QUJD"


async def test_transcript_redacts_image_data(tmp_path, monkeypatch):
    install_fake_anthropic(monkeypatch, text="ok")

    result = await call_reasoning_llm(
        agent_path=_agent(tmp_path),
        workdir=tmp_path / "wd",
        user_prompt="describe",
        model="claude-sonnet-4-6",
        images=[_IMG_BLOCK],
    )

    raw = result.transcript_path.read_text(encoding="utf-8")
    assert "QUJD" not in raw  # base64 payload never hits disk
    assert "redacted:image" in raw


def test_redact_helper_blanks_data_preserves_shape():
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "t"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "SECRET"}},
        ]},
        {"role": "assistant", "content": "reply"},
    ]
    out = _redact_images_in_messages(messages)

    # Original untouched.
    assert messages[0]["content"][1]["source"]["data"] == "SECRET"
    # Redacted copy blanks the data but keeps media_type + type.
    img = out[0]["content"][1]
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"].startswith("<redacted:image bytes=")
    assert out[0]["content"][0] == {"type": "text", "text": "t"}
    # String-content messages pass through unchanged.
    assert out[1] == {"role": "assistant", "content": "reply"}
    # Serializable + small.
    assert "SECRET" not in json.dumps(out)
