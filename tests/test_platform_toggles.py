"""Per-slot platform toggles: a platform switched off on a slot's tracker is
left out of that slot's posts, recorded as "disabled" (never failed or
skipped), and frozen into scheduled batches at scheduling time."""

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import backend.main as main
import backend.poster_browser as poster_browser
import backend.queue as queue_mod
from backend import scheduler
from backend.account_state import AccountState, AccountStateError, AccountStateStore
from backend.models import PostResult, PostSlot
from backend.notifier import Notifier
from backend.outcomes import classify_history_row, disabled_skip_error
from backend.queue import (
    QueuedBatch, SlotBatch, _history_index, _snapshot_media, load_queue, save_queue,
)

# Captured before the conftest tripwire swaps it out per test.
_real_post_all = poster_browser.post_all

_HTML = (Path(__file__).resolve().parents[1] / "frontend" / "index.html").read_text()


class _FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


# --- request contract --------------------------------------------------------

def test_post_slot_defaults_to_both_platforms_for_legacy_requests():
    slot = PostSlot(slot="A", filename="a.mp4", caption="c")
    assert slot.enabled_platforms == ["instagram", "tiktok"]


@pytest.mark.parametrize("bad", [[], ["instagram", "instagram"], ["youtube"]])
def test_post_slot_rejects_invalid_platform_selection(bad):
    with pytest.raises(ValidationError):
        PostSlot(slot="A", filename="a.mp4", caption="c", enabled_platforms=bad)


def test_post_result_success_ignores_disabled_platform_only():
    assert PostResult(slot="A", ig_post_id="ig",
                      errors=[disabled_skip_error("tiktok")]).success
    assert not PostResult(slot="A", errors=[
        disabled_skip_error("tiktok"),
        "IG post: skipped (no session — run session_manager login instagram)",
    ]).success


# --- browser posting ---------------------------------------------------------

def _stub_platforms(monkeypatch, calls):
    async def ig(**kwargs):
        calls.append("instagram")
        return "ig_ok_A"

    async def tt(**kwargs):
        calls.append("tiktok")
        return "tt_ok_A"

    monkeypatch.setattr(poster_browser, "session_exists", lambda platform, slot: True)
    monkeypatch.setattr(poster_browser.instagram_browser, "post_media", ig)
    monkeypatch.setattr(poster_browser.tiktok_browser, "post_media", tt)


@pytest.mark.parametrize("enabled,posted,disabled", [
    ({"instagram"}, "instagram", "tiktok"),
    ({"tiktok"}, "tiktok", "instagram"),
])
def test_disabled_platform_never_opens_a_browser(monkeypatch, enabled, posted, disabled):
    calls, events = [], []
    _stub_platforms(monkeypatch, calls)
    result = asyncio.run(poster_browser.post_slot(
        "A", Path("a.mp4"), "cap", "video",
        progress_cb=lambda *e: events.append(e),
        enabled_platforms=enabled,
    ))
    assert calls == [posted]
    assert result.errors == [disabled_skip_error(disabled)]
    assert result.success
    assert all(e[1] != disabled for e in events), "no progress event for a disabled platform"
    outcomes = classify_history_row(result.model_dump())
    assert outcomes[disabled] == "disabled"
    assert outcomes[posted] == "confirmed"


def test_disabled_platform_is_silent_in_notifications(monkeypatch):
    calls = []
    _stub_platforms(monkeypatch, calls)
    monkeypatch.setattr(poster_browser, "post_all", _real_post_all)
    notifier = _FakeNotifier()
    asyncio.run(poster_browser.post_all(
        [{"slot": "A", "media_path": Path("a.mp4"), "caption": "c",
          "media_type": "video", "enabled_platforms": {"instagram"}}],
        notifier=notifier,
    ))
    assert [m["title"] for m in notifier.sent] == ["RicePoster: run complete"]
    assert notifier.sent[0]["body"] == "1/1 posted successfully"


# --- manual API --------------------------------------------------------------

def test_api_post_passes_the_selection_through(client, tmp_media, monkeypatch):
    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    captured = []

    async def fake_post_all(slots, **kwargs):
        captured.extend(slots)
        return [PostResult(slot=s["slot"], ig_post_id="ig") for s in slots]

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)
    resp = client.post("/api/post", json={"slots": [{
        "slot": "A", "filename": "A_clip.mp4", "caption": "hi",
        "media_type": "video", "enabled_platforms": ["instagram"],
    }]})
    assert resp.status_code == 200
    assert captured[0]["enabled_platforms"] == {"instagram"}


# --- scheduling --------------------------------------------------------------

def test_schedule_freezes_the_selection_into_the_queue(client, tmp_media, monkeypatch, tmp_path):
    qf = tmp_path / "queue.jsonl"
    monkeypatch.setattr(queue_mod, "QUEUE_FILE", qf)
    monkeypatch.setattr(queue_mod, "QUEUE_MEDIA_DIR", tmp_path / "queue_media")
    monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
    (tmp_media / "A_clip.mp4").write_bytes(b"video")
    resp = client.post("/api/queue", json={
        "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c",
                   "media_type": "video", "enabled_platforms": ["tiktok"]}],
        "fire_time": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    })
    assert resp.status_code == 201
    batches = load_queue(qf)
    assert batches[0].slots[0].enabled_platforms == ["tiktok"]


def test_legacy_queue_rows_default_to_both_platforms(tmp_path):
    row = {"slot": "A", "media_path": "a.mp4", "caption": "c"}
    batch = QueuedBatch.from_dict({
        "id": "b", "fire_time": "2026-10-01T09:00:00+00:00",
        "created_at": "2026-09-01T09:00:00+00:00", "slots": [row],
        "status": "pending", "headless": True,
    })
    assert batch.slots[0].enabled_platforms == ["instagram", "tiktok"]


def test_queue_rejects_garbage_platform_selection():
    with pytest.raises(ValueError):
        QueuedBatch.from_dict({
            "id": "b", "fire_time": "2026-10-01T09:00:00+00:00",
            "created_at": "2026-09-01T09:00:00+00:00",
            "slots": [{"slot": "A", "media_path": "a.mp4", "caption": "c",
                       "enabled_platforms": ["youtube"]}],
            "status": "pending", "headless": True,
        })


def test_snapshot_preserves_the_selection(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"v")
    snapped = _snapshot_media("b1", [SlotBatch("A", "a.mp4", "c", enabled_platforms=["instagram"])],
                              media, tmp_path / "qm")
    assert snapped[0].enabled_platforms == ["instagram"]


def _scheduled_batch(tmp_path, enabled):
    media = tmp_path / "media"
    media.mkdir()
    (media / "A_clip.mp4").write_bytes(b"video")
    qf, qmedia = tmp_path / "queue.jsonl", tmp_path / "queue_media"
    snapped = _snapshot_media("batch1", [SlotBatch(
        slot="A", media_path="A_clip.mp4", caption="c", enabled_platforms=enabled,
    )], media, qmedia)
    batch = QueuedBatch(
        id="batch1", fire_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
        created_at=datetime.now(timezone.utc), slots=snapped,
        status="pending", headless=True,
    )
    save_queue([batch], qf)
    return batch, qf, qmedia


def test_scheduled_run_probes_and_posts_only_enabled_platforms(tmp_path):
    batch, qf, qmedia = _scheduled_batch(tmp_path, ["instagram"])
    hf = tmp_path / "history.jsonl"
    probed, seen = [], {}

    async def check(slot, platform):
        probed.append(platform)
        return "live"

    async def post(slots, headless=True, notifier=None):
        seen["slots"] = slots
        return [PostResult(slot="A", ig_post_id="ig_ok",
                           errors=[disabled_skip_error("tiktok")])]

    notifier = _FakeNotifier()
    asyncio.run(scheduler.execute_batch(batch, qf, qmedia, hf, notifier, check, post))

    assert probed == ["instagram"]
    assert seen["slots"][0]["enabled_platforms"] == {"instagram"}
    # The run finished "done", not "partial": only "done" releases the snapshot.
    assert not (qmedia / "batch1").exists()
    assert not any("failed" in m["title"] or "skipped" in m["title"] for m in notifier.sent)
    index, _ = _history_index(hf)
    assert index["batch1"]["unsuccessful"] == 0


def test_scheduled_preflight_failure_on_only_enabled_platform_skips_slot(tmp_path):
    batch, qf, qmedia = _scheduled_batch(tmp_path, ["tiktok"])
    hf = tmp_path / "history.jsonl"
    posted = []

    async def check(slot, platform):
        return "expired"

    async def post(slots, headless=True, notifier=None):
        posted.append(slots)
        return []

    asyncio.run(scheduler.execute_batch(batch, qf, qmedia, hf, _FakeNotifier(), check, post))
    assert posted == []
    row = json.loads(hf.read_text().splitlines()[0])
    outcomes = classify_history_row(row)
    assert outcomes == {"instagram": "disabled", "tiktok": "skipped"}
    assert (qmedia / "batch1").exists(), "nothing posted, so media is retained"


# --- persistent toggle state -------------------------------------------------

def test_account_state_persists_disabled_platforms(tmp_path):
    store = AccountStateStore(tmp_path / "state.json", ["one", "two"], {"generic"}, 2)
    state = AccountState(active_account_ids=["one"], disabled_platforms={"one": ["tiktok"]})
    store.save(state)
    assert store.load().disabled_platforms == {"one": ["tiktok"]}


def test_account_state_without_toggles_still_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "active_account_ids": ["one"]}))
    store = AccountStateStore(path, ["one"], {"generic"}, 2)
    assert store.load().disabled_platforms == {}


@pytest.mark.parametrize("bad", [{"ghost": ["tiktok"]}, {"one": ["youtube"]},
                                 {"one": ["tiktok", "tiktok"]}])
def test_account_state_rejects_invalid_toggles(tmp_path, bad):
    store = AccountStateStore(tmp_path / "state.json", ["one"], {"generic"}, 2)
    with pytest.raises(AccountStateError):
        store.validate(AccountState(active_account_ids=["one"], disabled_platforms=bad))


# --- frontend ----------------------------------------------------------------

def _function(name):
    start = _HTML.index(f"function {name}(")
    depth, i = 0, _HTML.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(_HTML[i], 0)
        i += 1
        if depth == 0:
            return _HTML[start:i]


def test_trackers_are_accessible_switches():
    render = _function("renderSlots")
    assert 'role="switch"' in render and "aria-checked" in render
    assert "'Session Disabled'" in render
    assert "togglePlatform(" in render
    # A missing session has nothing to switch.
    assert "(missing ? ' disabled'" in render


def test_toggles_persist_through_account_state():
    assert "disabled_platforms: state.accountState.disabled_platforms" in _function("persistAccountState")


def test_payload_and_buttons_follow_the_selection():
    payload = _function("buildSlotsPayload")
    assert "enabled_platforms: enabledPlatforms(slot)" in payload
    assert "enabledPlatforms(slot).length" in payload
    assert "buildSlotsPayload().slots.length" in _function("updateButtons")


def _node(driver, *names):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node needed to execute frontend helpers")
    prelude = ("const state = {accountState: {disabled_platforms: {}}, accounts: [], slots: {},"
               " sessions: {}, postMode: 'browser'};\n"
               "const PLATFORMS = ['instagram', 'tiktok'];\n"
               "const PLATFORM_NAMES = { instagram: 'Instagram', tiktok: 'TikTok' };\n")
    proc = subprocess.run([node, "--input-type=module", "--eval",
                           prelude + "\n".join(_function(n) for n in names) + driver],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_confirmation_names_exact_platforms_and_drops_both_off_slots():
    driver = """
state.accounts = [{slot:'A', name:'Alpha'}, {slot:'B', name:'Beta'}, {slot:'C', name:'Gamma'}];
for (const s of ['A','B','C']) state.slots[s] = {filename:'x.mp4', caption:'c', mediaType:'video'};
state.sessions = {A:{instagram:true,tiktok:true}, B:{instagram:true,tiktok:true}, C:{instagram:true,tiktok:true}};
state.accountState.disabled_platforms = {A: ['tiktok'], C: ['instagram', 'tiktok']};
console.log(JSON.stringify({
  summary: targetSummary('Post'),
  slots: buildSlotsPayload().slots.map(s => [s.slot, s.enabled_platforms]),
}));
"""
    out = _node(driver, "isPlatformEnabled", "enabledPlatforms", "buildSlotsPayload", "targetSummary")
    assert out["slots"] == [["A", ["instagram"]], ["B", ["instagram", "tiktok"]]]
    assert "Alpha [A] (Instagram only)" in out["summary"]
    assert "Beta [B] (Instagram + TikTok)" in out["summary"]
    assert "Gamma" not in out["summary"]


def test_frontend_classifies_disabled_distinctly():
    driver = """
const r = {ig_post_id:'ig_ok_A', tt_post_id:'', errors:['TT post: skipped (disabled for this account)']};
console.log(JSON.stringify({
  tt: classifyPlatform('TT', r.tt_post_id, r.errors),
  events: summaryEventsFromResults([r]).map(e => e.status),
}));
"""
    out = _node(driver, "isUnconfirmed", "isSkipError", "isDisabledSkip",
                "classifyPlatform", "summaryEventsFromResults")
    assert out == {"tt": "disabled", "events": ["ok"]}


def test_disabled_marker_matches_backend():
    assert re.search(r"skipped \(disabled for this account\)", _function("isDisabledSkip"))
    assert disabled_skip_error("tiktok") == "TT post: skipped (disabled for this account)"
