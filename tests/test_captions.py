"""Unit tests for backend/captions.py with the Anthropic client mocked."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from backend import captions


class _FakeAsyncAnthropic:
    """Stands in for anthropic.AsyncAnthropic; returns a canned caption."""

    created_with = []
    init_kwargs = []

    def __init__(self, api_key=None, **kwargs):
        # **kwargs mirrors the real client, which accepts timeout/max_retries
        # alongside api_key. Recorded rather than swallowed so the bounded
        # timeout (tech-debt audit BE-10) is assertable.
        _FakeAsyncAnthropic.created_with.append(api_key)
        _FakeAsyncAnthropic.init_kwargs.append(kwargs)
        self.messages = self

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(text="  a fine caption  ")])


@pytest.fixture
def fake_anthropic(monkeypatch):
    _FakeAsyncAnthropic.created_with = []
    _FakeAsyncAnthropic.init_kwargs = []
    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    return _FakeAsyncAnthropic


def test_build_user_prompt_with_topic():
    p = captions._build_user_prompt("video", "rice cooking", "fallback text")
    assert "video" in p
    assert "rice cooking" in p
    assert "fallback text" not in p


def test_build_user_prompt_without_topic_falls_back():
    p = captions._build_user_prompt("image", "   ", "No specific topic provided. Generic.")
    assert "No specific topic provided" in p


def test_generate_caption_returns_stripped_text(fake_anthropic, monkeypatch):
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("test-key"))
    result = asyncio.run(captions.generate_caption("video", "topic"))
    assert result == "a fine caption"
