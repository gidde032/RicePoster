"""Tests for the adjustable-account-count refactor.

Refactor of 2026-07-19 (ROADMAP "Next up"): the hardcoded A/B/C slot list
became config-driven via ACCOUNT_SLOTS in credentials.env (default A,B,C),
and /api/post switched from nine fixed form fields to a JSON body
(PostRequest). Pure refactor — behavior at the default three slots must be
unchanged. This must hold before the scheduling stack is built on top.
"""

import pytest

import backend.main as main
from backend import config
from backend.config import parse_slot_ids
from backend.models import PostResult



# --- ACCOUNT_SLOTS parsing ---------------------------------------------------

def test_default_when_unset_or_blank():
    assert parse_slot_ids("") == ["A", "B", "C"]
    assert parse_slot_ids("  ") == ["A", "B", "C"]
    assert parse_slot_ids(",,") == ["A", "B", "C"]


def test_custom_slot_list():
    assert parse_slot_ids("A,B,C,D") == ["A", "B", "C", "D"]


def test_whitespace_stripped():
    assert parse_slot_ids(" A , B ,C ") == ["A", "B", "C"]


def test_duplicates_removed_order_kept():
    assert parse_slot_ids("A,B,A,C,B") == ["A", "B", "C"]


def test_non_letter_ids_allowed_if_fs_safe():
    assert parse_slot_ids("main,alt-1,alt_2") == ["main", "alt-1", "alt_2"]


def test_invalid_token_fails_loudly():
    """A malformed token must refuse to start, not silently drop an
    account — slot ids become session dir names and filename prefixes."""
    for bad in ["A,B/C", "A,../B", "A,B C", "A,B."]:
        with pytest.raises(ValueError):
            parse_slot_ids(bad)


def test_session_manager_slots_come_from_config():
    from backend import session_manager

    assert session_manager.SLOTS == config.SLOT_IDS


# --- upload validation is dynamic --------------------------------------------

def test_upload_accepts_configured_extra_slot(client, tmp_media, monkeypatch):
    monkeypatch.setattr(main, "SLOT_IDS", ["A", "B", "C", "D"])
    resp = client.post(
        "/api/upload/D", files={"file": ("clip.mp4", b"x", "video/mp4")}
    )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "D_clip.mp4"


def test_upload_rejects_unconfigured_slot(client, tmp_media):
    resp = client.post(
        "/api/upload/Z", files={"file": ("clip.mp4", b"x", "video/mp4")}
    )
    assert resp.status_code == 400
    assert "Configured slots" in resp.json()["detail"]


# --- /api/post JSON body -----------------------------------------------------

def _stub_browser_poster(monkeypatch, captured):
    async def fake_post_all(slots, headless=None, progress_cb=None, notifier=None):
        captured.extend(slots)
        return [PostResult(slot=s["slot"]) for s in slots]

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)


def test_post_accepts_fourth_slot(client, tmp_media, monkeypatch):
    monkeypatch.setattr(main, "SLOT_IDS", ["A", "B", "C", "D"])
    (tmp_media / "D_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_browser_poster(monkeypatch, captured)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "D", "filename": "D_clip.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 200
    assert [s["slot"] for s in captured] == ["D"]


def test_post_rejects_unknown_slot(client, tmp_media, monkeypatch):
    (tmp_media / "Z_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_browser_poster(monkeypatch, captured)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "Z", "filename": "Z_clip.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "Unknown slot" in resp.json()["detail"]
    assert captured == []


def test_post_rejects_duplicate_slot(client, tmp_media, monkeypatch):
    """The old fixed-field API made duplicates impossible; the JSON list
    must not allow double-posting to one real account."""
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    captured = []
    _stub_browser_poster(monkeypatch, captured)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "one", "media_type": "video"},
                {"slot": "A", "filename": "A_clip.mp4", "caption": "two", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "Duplicate slot" in resp.json()["detail"]
    assert captured == []


def test_post_headless_omitted_uses_env_default(client, tmp_media, monkeypatch):
    monkeypatch.setattr(main, "HEADLESS", True)
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    seen = {}

    async def fake_post_all(slots, headless=None, progress_cb=None, notifier=None):
        seen["headless"] = headless
        return []

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)
    client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert seen["headless"] is True


def test_post_media_type_defaults_to_image(client, tmp_media, monkeypatch):
    """media_type is optional in the JSON body, matching the old form
    field's `or "image"` fallback."""
    (tmp_media / "A_pic.jpg").write_bytes(b"x")
    captured = []
    _stub_browser_poster(monkeypatch, captured)

    client.post(
        "/api/post",
        json={"slots": [{"slot": "A", "filename": "A_pic.jpg", "caption": "hi"}]},
    )
    assert captured[0]["media_type"] == "image"


# --- frontend source-level assertions ----------------------------------------
# (Playwright E2E forbidden — established pattern.)

def test_frontend_has_no_hardcoded_slot_state(frontend_src):
    """state.slots must be built from /api/accounts, not a literal
    three-key object."""
    assert "slots: {}," in frontend_src
    assert "function emptySlot()" in frontend_src
    assert "slot_a_filename" not in frontend_src


def test_frontend_posts_json_body(frontend_src):
    assert "'Content-Type': 'application/json'" in frontend_src
    assert "JSON.stringify(payload)" in frontend_src
