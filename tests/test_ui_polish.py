"""Tests for the RicePoster UI polish pass (2026-07-18): rename, live run
progress, media library management, and frontend wiring."""

import asyncio
from pathlib import Path

import backend.main as main
from backend import poster_browser
from backend.models import PostResult


# --- rename ------------------------------------------------------------------

def test_renamed_to_riceposter(frontend_src):
    assert main.app.title == "RicePoster"
    html = frontend_src
    assert "<title>RicePoster</title>" in html
    assert "RICEPOSTER" in html
    # The frontend used to carry the seed account's persona in its copy and
    # its default-style fallback. Both are gone (#50 Phase B); the fallback is
    # now the neutral default, and prompts/ is guarded by
    # test_tracked_prompts_are_exactly_the_seed_set.
    assert "'generic'" in html, "default-style fallback must be the neutral style"


# --- poster progress events --------------------------------------------------

def _run_post_slot(monkeypatch, ig_ok=True, tt_ok=True, has_sessions=True):
    """Drive post_slot with stubbed browser modules and record progress events."""
    events = []

    async def fake_ig(**kwargs):
        if not ig_ok:
            raise RuntimeError("ig boom")
        return "ig_post_ok_A"

    async def fake_tt(**kwargs):
        if not tt_ok:
            raise RuntimeError("tt boom")
        return "tt_post_unconfirmed_A"

    monkeypatch.setattr(poster_browser.instagram_browser, "post_media", fake_ig)
    monkeypatch.setattr(poster_browser.tiktok_browser, "post_media", fake_tt)
    monkeypatch.setattr(poster_browser, "session_exists", lambda p, s: has_sessions)

    result = asyncio.run(
        poster_browser.post_slot(
            "A", Path("/tmp/x.mp4"), "cap", "video",
            progress_cb=lambda *a: events.append(a),
        )
    )
    return result, events


def test_progress_events_success_and_unconfirmed(monkeypatch):
    result, events = _run_post_slot(monkeypatch)
    assert [(e[1], e[2]) for e in events] == [
        ("instagram", "started"),
        ("instagram", "ok"),
        ("tiktok", "started"),
        ("tiktok", "unconfirmed"),
    ]
    assert result.ig_post_id == "ig_post_ok_A"


def test_progress_events_error(monkeypatch):
    result, events = _run_post_slot(monkeypatch, ig_ok=False)
    assert ("instagram", "error") in [(e[1], e[2]) for e in events]
    assert result.errors


def test_progress_events_skipped(monkeypatch):
    _, events = _run_post_slot(monkeypatch, has_sessions=False)
    assert [(e[1], e[2]) for e in events] == [
        ("instagram", "skipped"),
        ("tiktok", "skipped"),
    ]


def test_broken_progress_callback_never_breaks_a_run(monkeypatch):
    """A progress bug must never take down a posting run."""
    async def fake_post(**kwargs):
        return "ig_post_ok_A"

    monkeypatch.setattr(poster_browser.instagram_browser, "post_media", fake_post)
    monkeypatch.setattr(poster_browser.tiktok_browser, "post_media", fake_post)
    monkeypatch.setattr(poster_browser, "session_exists", lambda p, s: True)

    def exploding_cb(*a):
        raise RuntimeError("cb boom")

    result = asyncio.run(
        poster_browser.post_slot("A", Path("/tmp/x.mp4"), "c", "video", progress_cb=exploding_cb)
    )
    assert result.errors == []


# --- progress endpoint lifecycle ---------------------------------------------

def test_post_progress_endpoint_lifecycle(client, tmp_media, monkeypatch):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")

    async def fake_post_all(slots, headless=None, progress_cb=None, notifier=None):
        progress_cb("A", "instagram", "started")
        progress_cb("A", "instagram", "ok", "ig_post_ok_A")
        # mid-run, the endpoint must report active
        assert main._post_progress.active is True
        return [PostResult(slot="A", ig_post_id="ig_post_ok_A", tt_post_id="tt_post_ok_A")]

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)

    client.post("/api/post", json={
        "slots": [
            {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
        ],
    })

    p = client.get("/api/post-progress").json()
    assert p["active"] is False           # run finished
    assert p["current"] is None
    assert {(e["platform"], e["status"]) for e in p["events"]} == {
        ("instagram", "started"), ("instagram", "ok"),
    }


def test_post_progress_resets_between_runs(client, tmp_media, monkeypatch):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    main._post_progress.events = [{"slot": "Z", "platform": "x", "status": "stale", "detail": ""}]

    async def fake_post_all(slots, **kwargs):
        return []

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)
    client.post("/api/post", json={
        "slots": [
            {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
        ],
    })
    assert client.get("/api/post-progress").json()["events"] == []


# --- media library endpoints -------------------------------------------------

def test_media_info(client, tmp_media):
    (tmp_media / "A_a.mp4").write_bytes(b"12345")
    (tmp_media / "B_b.jpg").write_bytes(b"123")
    (tmp_media / ".gitkeep").write_bytes(b"")

    d = client.get("/api/media-info").json()
    assert d["file_count"] == 2
    assert d["total_bytes"] == 8


def test_media_clear_removes_files_keeps_gitkeep(client, tmp_media):
    (tmp_media / "A_a.mp4").write_bytes(b"x")
    (tmp_media / ".gitkeep").write_bytes(b"")

    resp = client.post("/api/media/clear")
    assert resp.json() == {"removed": 1}
    assert not (tmp_media / "A_a.mp4").exists()
    assert (tmp_media / ".gitkeep").exists()


def test_media_clear_blocked_during_post_run(client, tmp_media, monkeypatch):
    from backend import run_guard
    monkeypatch.setattr(run_guard, "_post_running", True)
    assert client.post("/api/media/clear").status_code == 409


# --- frontend wiring (source-level) ------------------------------------------

def test_frontend_polish_wiring(frontend_src):
    html = frontend_src
    # live progress polling
    assert "/api/post-progress" in html
    assert "startProgressPolling" in html and "stopProgressPolling" in html
    # upload progress via XHR
    assert "XMLHttpRequest" in html
    assert "upload.onprogress" in html
    # caption counter + auto-grow
    assert "CAPTION_LIMIT = 2200" in html
    assert "updateCharCount" in html and "autoGrow" in html
    # per-slot session dots + reset + media controls
    assert "sessDots" in html
    assert "resetRun" in html
    assert "/api/media/clear" in html and "/api/media-info" in html
