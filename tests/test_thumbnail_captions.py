"""Tests for thumbnail-based caption generation.

Feature added 2026-07-19 (ROADMAP "Next up"): the frontend captures a frame
~1s into the media, shows it as a chip, and sends a downscaled base64 JPEG
with caption requests; captions.py attaches it as an Anthropic image block
so the model captions what the clip actually shows. No thumbnail → the
request stays text-only, exactly as before the feature.
"""

import asyncio
import base64

import pytest

import backend.main as main
from backend import captions
from backend.main import clean_thumbnail, MAX_THUMBNAIL_BYTES

FAKE_JPEG_B64 = base64.b64encode(b"\xff\xd8\xff fake jpeg bytes").decode()


# --- captions._build_content -------------------------------------------------

def test_no_thumbnail_keeps_plain_text_content():
    """Without a thumbnail the message content must stay a plain string —
    the pre-feature request shape, byte for byte."""
    assert captions._build_content("write a caption") == "write a caption"


def test_thumbnail_builds_image_then_text_blocks():
    content = captions._build_content("write a caption", FAKE_JPEG_B64)
    assert isinstance(content, list) and len(content) == 2
    image, text = content
    assert image["type"] == "image"
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": FAKE_JPEG_B64,
    }
    assert text["type"] == "text"
    assert "write a caption" in text["text"]
    assert "frame from the media" in text["text"]


# --- generate_caption passes the thumbnail to the API ------------------------

def test_generate_caption_sends_image_block(capturing_anthropic):
    asyncio.run(captions.generate_caption("video", "a topic", thumbnail_b64=FAKE_JPEG_B64))
    (call,) = capturing_anthropic.calls
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == FAKE_JPEG_B64


def test_generate_caption_without_thumbnail_stays_text_only(capturing_anthropic):
    asyncio.run(captions.generate_caption("video", "a topic"))
    (call,) = capturing_anthropic.calls
    assert isinstance(call["messages"][0]["content"], str)


# --- main.clean_thumbnail ----------------------------------------------------

def test_clean_thumbnail_empty_passes_through():
    assert clean_thumbnail("") == ""
    assert clean_thumbnail("   ") == ""


def test_clean_thumbnail_accepts_raw_base64():
    assert clean_thumbnail(FAKE_JPEG_B64) == FAKE_JPEG_B64


def test_clean_thumbnail_strips_data_url_prefix():
    data_url = f"data:image/jpeg;base64,{FAKE_JPEG_B64}"
    assert clean_thumbnail(data_url) == FAKE_JPEG_B64


def test_clean_thumbnail_rejects_invalid_base64():
    with pytest.raises(ValueError):
        clean_thumbnail("not!!!valid###base64")


def test_clean_thumbnail_rejects_non_base64_data_url():
    with pytest.raises(ValueError):
        clean_thumbnail("data:image/jpeg,rawbytes")


def test_clean_thumbnail_rejects_oversized_payload():
    huge = base64.b64encode(b"x" * (MAX_THUMBNAIL_BYTES + 1)).decode()
    with pytest.raises(ValueError):
        clean_thumbnail(huge)


# --- /api/generate-caption endpoint ------------------------------------------

def test_endpoint_passes_thumbnail_through(client, monkeypatch):
    seen = {}

    async def _fake(media_type, topic, style, avoid_caption, feedback, thumbnail_b64=""):
        seen["thumbnail_b64"] = thumbnail_b64
        return "a caption"

    monkeypatch.setattr(main, "generate_caption", _fake)
    resp = client.post(
        "/api/generate-caption",
        data={
            "media_type": "video",
            "thumbnail": f"data:image/jpeg;base64,{FAKE_JPEG_B64}",
        },
    )
    assert resp.status_code == 200
    assert seen["thumbnail_b64"] == FAKE_JPEG_B64


def test_endpoint_defaults_to_no_thumbnail(client, monkeypatch):
    seen = {}

    async def _fake(media_type, topic, style, avoid_caption, feedback, thumbnail_b64=""):
        seen["thumbnail_b64"] = thumbnail_b64
        return "a caption"

    monkeypatch.setattr(main, "generate_caption", _fake)
    resp = client.post("/api/generate-caption", data={"media_type": "video"})
    assert resp.status_code == 200
    assert seen["thumbnail_b64"] == ""


def test_endpoint_rejects_bad_thumbnail_with_400(client):
    # No stub needed: validation must fail before any caption path is reached
    # (the conftest tripwire enforces exactly that).
    resp = client.post(
        "/api/generate-caption",
        data={"media_type": "video", "thumbnail": "not!!!valid###base64"},
    )
    assert resp.status_code == 400
    assert "base64" in resp.json()["detail"]


# --- frontend source-level assertions ----------------------------------------
# Playwright/browser E2E is forbidden in this suite, so the capture flow is
# asserted at source level (established pattern from test_ui_polish.py).

def test_frontend_has_capture_function(frontend_src):
    assert "function captureThumbnail" in frontend_src
    assert "toDataURL('image/jpeg', 0.8)" in frontend_src
    assert "THUMB_MAX_EDGE = 512" in frontend_src


def test_frontend_sends_thumbnail_on_generate_and_regenerate(frontend_src):
    # Both caption request paths (global generate + per-slot regenerate)
    # must attach the thumbnail when one was captured.
    assert frontend_src.count("formData.append('thumbnail', s.thumb)") == 2


def test_frontend_shows_thumb_chip(frontend_src):
    assert "thumbChip_${slot}" in frontend_src
    assert "frame the caption AI sees" in frontend_src


def test_frontend_clears_thumb_on_new_run(frontend_src):
    assert "s.thumb = ''" in frontend_src
