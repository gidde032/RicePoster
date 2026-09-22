"""First-run setup friction found by a prospective-user walkthrough of the
README (2026-09-22): caption-key errors must be readable, and a
credentials.env that follows the template must not turn the gates red."""

import asyncio

import anthropic
import httpx
import pytest
from pydantic import SecretStr

import backend.main as main
from backend import captions
from tests.conftest import HERMETIC_ENV


def _use_real_caption_path(monkeypatch):
    """Undo the conftest tripwire on main.generate_caption. Safe: the
    Anthropic client itself stays faked (or is never reached), so no request
    leaves the machine."""
    monkeypatch.setattr(main, "generate_caption", captions.generate_caption)


def _rejecting_client(error_cls, status):
    class _Rejecting:
        def __init__(self, api_key=None, **kwargs):
            self.messages = self

        async def create(self, **kwargs):
            request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
            raise error_cls(
                "invalid x-api-key",
                response=httpx.Response(status, request=request),
                body=None,
            )

    return _Rejecting


@pytest.mark.parametrize("error_cls,status", [
    (anthropic.AuthenticationError, 401),
    (anthropic.PermissionDeniedError, 403),
])
def test_rejected_api_key_is_a_readable_400(client, monkeypatch, error_cls, status):
    """A placeholder key used to surface as a bare 'Internal Server Error'."""
    _use_real_caption_path(monkeypatch)
    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic", _rejecting_client(error_cls, status))
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("sk-ant-your-key-here"))
    resp = client.post("/api/generate-caption", data={"media_type": "video", "topic": "t"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "ANTHROPIC_API_KEY" in detail and "credentials.env" in detail
    assert "sk-ant-your-key-here" not in detail


def test_missing_api_key_is_a_readable_400(client, monkeypatch):
    _use_real_caption_path(monkeypatch)
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr(""))
    resp = client.post("/api/generate-caption", data={"media_type": "video", "topic": "t"})
    assert resp.status_code == 400
    assert "ANTHROPIC_API_KEY is not set" in resp.json()["detail"]


def test_other_api_failures_are_not_reported_as_key_problems(monkeypatch):
    """Only a rejected key is a setup error; anything else keeps propagating."""
    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic",
                        _rejecting_client(anthropic.InternalServerError, 500))
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("k"))
    with pytest.raises(anthropic.InternalServerError):
        asyncio.run(captions.generate_caption("video", "t"))


def test_credentials_matching_the_suites_own_env_are_not_a_leak(tmp_path, monkeypatch):
    """Following the template's advice (SCHEDULER_ENABLED=false) used to fail
    the credentials-leak gate, because conftest sets that same value itself.
    Keys the suite sets are excluded; any other matching key still counts."""
    from tests.test_gates import leaked_credentials_keys

    env = tmp_path / "credentials.env"
    env.write_text("SCHEDULER_ENABLED=false\nLOG_LEVEL=WARNING\n")
    for key, value in HERMETIC_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert leaked_credentials_keys(env) == []

    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert leaked_credentials_keys(env) == ["LOG_LEVEL"]
