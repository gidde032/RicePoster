"""Integration smoke tests for the FastAPI routes.

All posting/caption paths are stubbed (see conftest tripwire) — these tests
exercise route wiring, slot assembly, and upload handling only.
"""

import pytest

import backend.main as main
from backend.models import PostResult


@pytest.mark.smoke
def test_serve_ui(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<script" in resp.text


@pytest.mark.smoke
def test_accounts_mock_mode(client, monkeypatch):
    monkeypatch.setattr(main, "POST_MODE", "mock")
    data = client.get("/api/accounts").json()
    assert data["post_mode"] == "mock"
    assert [a["slot"] for a in data["accounts"]] == ["A", "B", "C"]
    assert data["sessions"] == {}


def test_accounts_browser_mode_reports_sessions(client, monkeypatch):
    import backend.session_manager as sm

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(sm, "session_exists", lambda platform, slot: platform == "instagram")
    data = client.get("/api/accounts").json()
    assert data["sessions"]["A"] == {"instagram": True, "tiktok": False}
    assert set(data["sessions"]) == {"A", "B", "C"}


@pytest.mark.smoke
def test_upload_video(client, tmp_media):
    resp = client.post(
        "/api/upload/A",
        files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "A_clip.mp4"
    assert data["media_type"] == "video"
    assert (tmp_media / "A_clip.mp4").read_bytes() == b"fake video bytes"


def test_upload_image(client, tmp_media):
    data = client.post(
        "/api/upload/B",
        files={"file": ("pic.jpg", b"fake jpeg", "image/jpeg")},
    ).json()
    assert data["filename"] == "B_pic.jpg"
    assert data["media_type"] == "image"


@pytest.mark.smoke
def test_generate_caption_endpoint(client, monkeypatch):
    async def fake_generate(media_type, topic, style="generic", *args, **kwargs):
        return f"caption for {media_type}/{topic}"

    monkeypatch.setattr(main, "generate_caption", fake_generate)
    resp = client.post(
        "/api/generate-caption",
        data={"media_type": "video", "topic": "rice"},
    )
    assert resp.json() == {"caption": "caption for video/rice"}


def _stub_poster(monkeypatch, mode, captured):
    """Install a capturing stub on whichever orchestrator `mode` routes to."""

    async def fake_post_all(slots, **kwargs):
        captured.extend(slots)
        return [PostResult(slot=s["slot"], ig_post_id="ig_x", tt_post_id="tt_x") for s in slots]

    monkeypatch.setattr(main, "POST_MODE", mode)
    if mode == "browser":
        monkeypatch.setattr(main, "post_all_browser", fake_post_all)
    else:
        monkeypatch.setattr(main, "post_all_api", fake_post_all)


def test_post_assembles_slots_browser_mode(client, tmp_media, monkeypatch):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_poster(monkeypatch, "browser", captured)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "hello world", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 200
    assert len(captured) == 1
    assert captured[0]["slot"] == "A"
    assert captured[0]["caption"] == "hello world"
    assert captured[0]["media_path"] == tmp_media / "A_clip.mp4"
    results = resp.json()
    assert results[0]["slot"] == "A"
    assert results[0]["errors"] == []


def test_post_routes_to_api_mode_when_not_browser(client, tmp_media, monkeypatch):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_poster(monkeypatch, "mock", captured)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 200
    assert len(captured) == 1


def test_post_drops_slot_with_missing_caption(client, tmp_media, monkeypatch):
    """Pins current (deferred-LOW) behavior: a slot with media but no caption
    is silently skipped rather than rejected."""
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    (tmp_media / "B_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_poster(monkeypatch, "browser", captured)

    client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "present", "media_type": "video"},
                {"slot": "B", "filename": "B_clip.mp4", "caption": "", "media_type": "video"},
            ],
        },
    )
    assert [s["slot"] for s in captured] == ["A"]
