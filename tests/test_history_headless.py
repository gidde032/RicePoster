"""Tests for the headless toggle + post history log slice (2026-07-19)."""

from pathlib import Path

import backend.main as main
from backend.models import PostResult

from tests.paths import PROJECT_ROOT


def _post_once(client, tmp_media, monkeypatch, captured, extra_form=None, result=None):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")

    async def fake_post_all(slots, headless=None, progress_cb=None, notifier=None):
        captured["headless"] = headless
        return [result or PostResult(slot="A", ig_post_id="ig_post_ok_A", tt_post_id="tt_post_ok_A")]

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)

    payload = {
        "slots": [
            {"slot": "A", "filename": "A_clip.mp4", "caption": "hello world", "media_type": "video"},
        ],
    }
    payload.update(extra_form or {})
    return client.post("/api/post", json=payload)


# --- headless override -------------------------------------------------------

def test_headless_defaults_to_env(client, tmp_media, tmp_history_file, monkeypatch):
    monkeypatch.setattr(main, "HEADLESS", True)
    captured = {}
    _post_once(client, tmp_media, monkeypatch, captured)
    assert captured["headless"] is True


def test_headless_override_false(client, tmp_media, tmp_history_file, monkeypatch):
    monkeypatch.setattr(main, "HEADLESS", True)
    captured = {}
    _post_once(client, tmp_media, monkeypatch, captured, extra_form={"headless": False})
    assert captured["headless"] is False


def test_headless_override_true(client, tmp_media, tmp_history_file, monkeypatch):
    monkeypatch.setattr(main, "HEADLESS", False)
    captured = {}
    _post_once(client, tmp_media, monkeypatch, captured, extra_form={"headless": True})
    assert captured["headless"] is True


# --- history recording -------------------------------------------------------

def test_history_recorded_after_run(client, tmp_media, tmp_history_file, monkeypatch):
    captured = {}
    _post_once(client, tmp_media, monkeypatch, captured)

    entries = client.get("/api/history").json()["entries"]
    assert len(entries) == 1
    en = entries[0]
    assert en["slot"] == "A"
    assert en["file"] == "A_clip.mp4"
    assert en["caption"] == "hello world"
    assert en["ig_post_id"] == "ig_post_ok_A"
    assert en["post_mode"] == "browser"
    assert en["errors"] == []
    assert "ts" in en


def test_history_newest_first_and_limit(client, tmp_media, tmp_history_file, monkeypatch):
    captured = {}
    _post_once(client, tmp_media, monkeypatch, captured)
    _post_once(client, tmp_media, monkeypatch, captured,
               result=PostResult(slot="A", errors=["TT post: boom"]))

    entries = client.get("/api/history").json()["entries"]
    assert len(entries) == 2
    assert entries[0]["errors"] == ["TT post: boom"]  # newest first

    limited = client.get("/api/history?limit=1").json()["entries"]
    assert len(limited) == 1
    assert limited[0]["errors"] == ["TT post: boom"]


def test_history_empty_when_no_file(client, tmp_history_file):
    assert client.get("/api/history").json() == {"entries": []}


def test_history_skips_corrupt_lines(client, tmp_history_file):
    tmp_history_file.write_text('{"slot": "A", "ts": "t"}\nnot json at all\n')
    entries = client.get("/api/history").json()["entries"]
    assert len(entries) == 1


def test_broken_history_write_never_breaks_a_run(client, tmp_media, monkeypatch):
    """History is best-effort: an unwritable file must not fail the post."""
    monkeypatch.setattr(main, "HISTORY_FILE", Path("/nonexistent-dir/history.jsonl"))
    captured = {}
    resp = _post_once(client, tmp_media, monkeypatch, captured)
    assert resp.status_code == 200


def test_history_file_gitignored():
    assert "history.jsonl" in (PROJECT_ROOT / ".gitignore").read_text()


# --- frontend wiring (source-level) ------------------------------------------

def test_frontend_headless_and_history_wiring(frontend_src):
    html = frontend_src
    assert "toggleHeadless" in html
    assert "headlessBadge" in html
    assert "payload.headless = state.headlessOverride" in html
    assert "toggleHistory" in html
    assert "/api/history" in html
    assert "historyPanel" in html
