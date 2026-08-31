"""Offline contracts for account rosters, immutable targets, and Stats."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.account_state import (
    AccountState, AccountStateError, AccountStateStore, discover_accounts,
)
from backend.device_identity import VIEWPORTS, viewport_for_slot
from backend.outcomes import aggregate_stats, classify_history_row
from backend.queue import QueuedBatch, SlotBatch
from backend.models import PostResult


def test_discovers_union_partial_platforms_and_legacy_tiktok(tmp_path):
    ig = tmp_path / "instagram"
    tt = tmp_path / "tiktok"
    (ig / "account-one").mkdir(parents=True)
    (ig / "account-one" / "profile").write_text("x")
    (tt / "account-two").mkdir(parents=True)
    (tt / "account-two" / "cookies.json").write_text("[]" * 6)
    (tt / "legacy_cookies.json").write_text("[]" * 6)

    accounts = discover_accounts(ig, tt, ["configured"], {"configured": "Configured"})
    by_id = {account.account_id: account for account in accounts}
    assert set(by_id) == {"configured", "account-one", "account-two", "legacy"}
    assert by_id["account-one"].instagram and not by_id["account-one"].tiktok
    assert by_id["account-two"].tiktok and not by_id["account-two"].instagram
    assert by_id["legacy"].tiktok
    assert by_id["account-one"].display_name == "account-one"


def test_state_defaults_validation_corruption_and_atomic_write(tmp_path):
    path = tmp_path / "sessions" / ".account-state.json"
    store = AccountStateStore(path, ["one", "two"], {"generic", "style-one"}, 2)
    default = store.load()
    assert default.active_account_ids == ["one", "two"]

    state = AccountState(
        active_account_ids=["two", "one"],
        rosters={"daily two": ["two", "one"]},
        caption_defaults={"two": "style-one"},
    )
    store.save(state)
    assert store.load().active_account_ids == ["two", "one"]
    assert not path.with_name(".account-state.json.tmp").exists()

    path.write_text("{broken")
    with pytest.raises(AccountStateError, match="corrupt"):
        store.load()


def test_missing_state_activates_only_compatibility_ids_at_legacy_device_indices(tmp_path):
    """Cold-review repair (CRITICAL/HIGH): discovery never changes default targets/devices."""
    store = AccountStateStore(
        tmp_path / "state.json",
        ["A", "B", "C", "folder-account"],
        {"generic"},
        3,
        instagram_ids={"B", "C", "folder-account"},
        compatibility_ids=["A", "B", "C"],
    )

    state = store.load()

    assert state.active_account_ids == ["A", "B", "C"]
    assert state.device_profiles == {"B": 1, "C": 2}
    assert "folder-account" not in state.active_account_ids


@pytest.mark.parametrize(
    "state, message",
    [
        (AccountState(active_account_ids=["one", "one"]), "duplicate"),
        (AccountState(active_account_ids=["one", "ONE"]), "case-colliding"),
        (AccountState(active_account_ids=["stale"]), "unknown"),
        (AccountState(active_account_ids=["one", "two", "three"]), "unknown"),
        (AccountState(active_account_ids=["one"], rosters={"bad/name": ["one"]}), "roster name"),
        (AccountState(active_account_ids=["one"], caption_defaults={"one": "missing"}), "style"),
    ],
)
def test_state_rejects_unknown_duplicate_stale_and_bad_values(tmp_path, state, message):
    store = AccountStateStore(tmp_path / "state.json", ["one", "two"], {"generic"}, 2)
    with pytest.raises(AccountStateError, match=message):
        store.save(state)


def test_device_assignment_survives_reorder_restart_and_roster_switch(tmp_path, monkeypatch):
    from backend import config

    path = tmp_path / ".account-state.json"
    monkeypatch.setattr(config, "ACCOUNT_STATE_FILE", path)
    store = AccountStateStore(path, ["one", "two", "three"], {"generic"}, 3)
    first = AccountState(active_account_ids=["one", "two"])
    store.save(first)
    one_viewport = viewport_for_slot("one")

    switched = store.load()
    switched.active_account_ids = ["three", "one"]
    store.save(switched)
    assert viewport_for_slot("one") == one_viewport
    assert len({tuple(viewport_for_slot(x).items()) for x in ("one", "two", "three")}) == 3
    assert viewport_for_slot("one") in VIEWPORTS


def test_device_capacity_refuses_activation(tmp_path):
    store = AccountStateStore(tmp_path / "state.json", ["one", "two"], {"generic"}, 1)
    with pytest.raises(AccountStateError, match="only 1"):
        store.save(AccountState(active_account_ids=["one", "two"]))


def test_tiktok_only_active_account_does_not_consume_instagram_device_profile(tmp_path):
    instagram_ids = {"creator-one", "creator-two", "creator-three"}
    store = AccountStateStore(
        tmp_path / "state.json",
        [*sorted(instagram_ids), "video-only"],
        {"generic"},
        3,
        instagram_ids=instagram_ids,
    )
    state = AccountState(
        active_account_ids=["creator-one", "creator-two", "creator-three", "video-only"],
        rosters={
            "creator three": ["creator-one", "creator-two", "creator-three"],
            "with tiktok": ["creator-one", "creator-two", "creator-three", "video-only"],
        },
    )

    store.save(state)

    assert set(state.device_profiles) == instagram_ids
    assert "video-only" not in state.device_profiles


def test_old_tiktok_only_assignment_is_reclaimed_for_instagram_account(tmp_path):
    store = AccountStateStore(
        tmp_path / "state.json",
        ["creator-one", "creator-two", "creator-three", "video-only"],
        {"generic"},
        3,
        instagram_ids={"creator-one", "creator-two", "creator-three"},
    )
    state = AccountState(
        active_account_ids=["creator-one", "creator-two", "creator-three", "video-only"],
        device_profiles={"creator-one": 0, "creator-two": 1, "video-only": 2},
    )

    store.save(state)

    assert state.device_profiles == {
        "creator-one": 0,
        "creator-two": 1,
        "creator-three": 2,
    }


def test_accounts_api_allows_tiktok_only_account_when_all_instagram_profiles_reserved(
    client,
):
    from backend import config, instagram_browser, tiktok_browser

    instagram_ids = ["A", "B", "C", "creator-four", "creator-five"]
    for account_id in instagram_ids:
        profile = instagram_browser.SESSIONS_DIR / account_id
        profile.mkdir()
        (profile / "Default").mkdir()
    tiktok_browser.SESSIONS_DIR.joinpath("video-only_cookies.json").write_text(
        json.dumps([{"name": "sessionid", "value": "x" * 20}])
    )
    config.ACCOUNT_STATE_FILE.write_text(json.dumps({
        "schema_version": 1,
        "active_account_ids": ["A", "B", "C"],
        "rosters": {"creator three": ["A", "B", "C"]},
        "caption_defaults": {},
        "device_profiles": {
            account_id: profile for profile, account_id in enumerate(instagram_ids)
        },
    }))

    discovered = client.get("/api/accounts").json()
    tiktok_account = next(
        account for account in discovered["available_accounts"]
        if account["account_id"] == "video-only"
    )
    assert tiktok_account["sessions"] == {"instagram": False, "tiktok": True}
    assert tiktok_account["instagram_device_required"] is False

    response = client.put("/api/accounts/state", json={
        "active_account_ids": ["A", "B", "C", "video-only"],
        "rosters": {
            "creator three": ["A", "B", "C"],
            "with tiktok": ["A", "B", "C", "video-only"],
        },
        "caption_defaults": {},
    })
    assert response.status_code == 200, response.text
    assert "video-only" not in response.json()["account_state"]["device_profiles"]


def test_device_lookup_fails_closed_on_corrupt_or_colliding_state(tmp_path, monkeypatch):
    from backend import config

    path = tmp_path / ".account-state.json"
    monkeypatch.setattr(config, "ACCOUNT_STATE_FILE", path)
    path.write_text("not-json")
    with pytest.raises(ValueError, match="safe device assignment"):
        viewport_for_slot("A")
    path.write_text(json.dumps({"device_profiles": {"A": 0, "B": 0}}))
    with pytest.raises(ValueError, match="collide"):
        viewport_for_slot("A")
    path.write_text(json.dumps({"device_profiles": {}}))
    with pytest.raises(ValueError, match="schema"):
        viewport_for_slot("A")


def test_mock_poster_accepts_folder_discovered_id_but_api_mode_rejects_it_clearly(
    tmp_path, monkeypatch
):
    """Cold-review repair (HIGH): folder IDs never become an uncaught API KeyError."""
    from backend import main, poster

    async def fake_ig(**_kwargs):
        return "mock-ig"

    async def fake_tt(**_kwargs):
        return "mock-tt"

    monkeypatch.setattr(poster, "MOCK_MODE", True)
    monkeypatch.setattr(poster.instagram, "post_media", fake_ig)
    monkeypatch.setattr(poster.tiktok, "post_media", fake_tt)
    result = asyncio.run(
        poster.post_slot("folder-account", tmp_path / "clip.mp4", "caption", "video")
    )
    assert result.ig_post_id == "mock-ig" and result.tt_post_id == "mock-tt"

    discovered = [SimpleNamespace(account_id="folder-account")]
    state = AccountState(active_account_ids=["folder-account"])
    store = SimpleNamespace(instagram_ids=set())
    monkeypatch.setattr(main, "_account_context", lambda: (discovered, state, None, store))
    monkeypatch.setattr(main, "POST_MODE", "api")
    monkeypatch.setattr(main, "SLOT_IDS", ["A"])
    with pytest.raises(HTTPException, match="credentials-backed ACCOUNT_SLOTS"):
        main._validate_active_targets(["folder-account"])


def test_legacy_queue_target_is_literal_not_roster_position():
    raw = {
        "id": "batch",
        "fire_time": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "slots": [{"slot": "legacy-account", "media_path": "x", "caption": "c"}],
        "status": "pending",
        "headless": True,
    }
    batch = QueuedBatch.from_dict(raw)
    assert batch.slots[0].slot == "legacy-account"
    assert batch.slots[0].account_id == "legacy-account"


@pytest.mark.parametrize(
    "slot",
    [
        {"slot": "A", "account_id": "B", "media_path": "x", "caption": "c"},
        {"slot": "../A", "media_path": "x", "caption": "c"},
    ],
)
def test_ambiguous_or_unsafe_legacy_queue_target_fails_closed(slot):
    raw = {
        "id": "batch",
        "fire_time": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "slots": [slot], "status": "pending", "headless": True,
    }
    with pytest.raises(ValueError):
        QueuedBatch.from_dict(raw)


def test_queue_api_persists_immutable_account_id(client, tmp_media, monkeypatch):
    import backend.main as main
    from backend import queue

    (tmp_media / "A_clip.mp4").write_bytes(b"123")
    queue_file = tmp_media.parent / "queue.jsonl"
    queue_media = tmp_media.parent / "queue_media"
    monkeypatch.setattr(queue, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(queue, "QUEUE_MEDIA_DIR", queue_media)
    monkeypatch.setattr(queue, "MEDIA_DIR", tmp_media)
    response = client.post("/api/queue", json={
        "fire_time": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "hello"}],
    })
    assert response.status_code == 201, response.text
    row = json.loads(queue_file.read_text())
    assert row["slots"][0]["slot"] == "A"
    assert row["slots"][0]["account_id"] == "A"

    # Changing the current roster cannot mutate the already-persisted target.
    changed = client.put("/api/accounts/state", json={
        "active_account_ids": ["B"], "rosters": {}, "caption_defaults": {},
    })
    assert changed.status_code == 200
    queued = client.get("/api/queue").json()["batches"][0]
    assert queued["slots"][0]["account_id"] == "A"
    assert queued["slots"][0]["slot"] == "A"


def test_account_state_api_crud_and_generic_fallback(client):
    initial = client.get("/api/accounts").json()
    assert initial["account_state_error"] is None
    assert all(a["caption_default"] == "generic" for a in initial["available_accounts"])
    response = client.put("/api/accounts/state", json={
        "active_account_ids": ["B", "A"],
        "rosters": {"daily two": ["B", "A"], "single account": ["A"]},
        "caption_defaults": {"B": "sports"},
    })
    assert response.status_code == 200
    updated = client.get("/api/accounts").json()
    assert [a["slot"] for a in updated["accounts"]] == ["B", "A"]
    assert updated["account_state"]["rosters"]["single account"] == ["A"]
    assert next(a for a in updated["available_accounts"] if a["slot"] == "B")["caption_default"] == "sports"


def test_corrupt_account_state_returns_no_guessed_active_targets(client):
    from backend import config

    config.ACCOUNT_STATE_FILE.write_text("not-json")
    data = client.get("/api/accounts").json()
    assert data["accounts"] == []
    assert data["account_state"]["active_account_ids"] == []
    assert "corrupt" in data["account_state_error"].lower()


def test_shared_outcome_classification_and_old_history_compatibility():
    old = {
        "slot": "A", "ig_post_id": "ig_1", "tt_post_id": "",
        "errors": ["TT post: skipped (no session — login)"],
    }
    assert classify_history_row(old) == {"instagram": "confirmed", "tiktok": "skipped"}
    preflight = {
        "ig_post_id": "", "tt_post_id": "", "errors": [
            "IG post: skipped (pre-flight ruled the session out: expired)",
            "TT post: skipped (pre-flight ruled the session out: no_session)",
        ],
    }
    assert classify_history_row(preflight) == {"instagram": "skipped", "tiktok": "skipped"}

    legacy_preflight = {
        "errors": ["pre-flight: instagram no_session", "pre-flight: tiktok expired"],
    }
    assert classify_history_row(legacy_preflight) == {
        "instagram": "skipped", "tiktok": "skipped",
    }


def test_outcome_classifier_recognizes_api_failures_and_failure_wins_over_skip():
    api_failure = {"errors": ["IG post failed: auth", "TT post failed: timeout"]}
    assert classify_history_row(api_failure) == {
        "instagram": "failed", "tiktok": "failed",
    }
    mixed = {"errors": [
        "IG post: skipped (no session)",
        "IG post: upload failed",
    ]}
    assert classify_history_row(mixed)["instagram"] == "failed"


def test_accounts_report_saved_sessions_even_in_mock_mode(client, tmp_path, monkeypatch):
    from backend import instagram_browser
    import backend.main as main

    account_dir = instagram_browser.SESSIONS_DIR / "A"
    account_dir.mkdir()
    (account_dir / "Default").mkdir()
    monkeypatch.setattr(main, "POST_MODE", "mock")

    data = client.get("/api/accounts").json()
    account = next(row for row in data["available_accounts"] if row["account_id"] == "A")
    assert account["sessions"]["instagram"] is True
    assert data["sessions"] == {}


def test_accounts_report_retained_queue_media_target(client, tmp_history_file, tmp_path):
    batch_id = "a" * 32
    snapshot = tmp_path / "queue_media" / batch_id
    snapshot.mkdir(parents=True)
    (snapshot / "A_clip.mp4").write_bytes(b"retry evidence")
    tmp_history_file.write_text(json.dumps({
        "batch_id": batch_id,
        "slot": "A",
        "account_id": "A",
        "errors": ["IG post: upload failed"],
    }) + "\n")

    data = client.get("/api/accounts").json()
    account = next(row for row in data["available_accounts"] if row["account_id"] == "A")
    assert account["queued_target"] is True


def test_future_manual_history_rows_track_account_and_media_bytes(
    tmp_history_file, tmp_path, monkeypatch
):
    import backend.main as main

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"123456")
    monkeypatch.setattr(main, "POST_MODE", "browser")
    main._append_history(
        [{"slot": "account-one", "media_path": media, "caption": "generic caption"}],
        [PostResult(slot="account-one", ig_post_id="ig_1")],
        True,
    )
    row = json.loads(tmp_history_file.read_text())
    assert row["slot"] == "account-one"
    assert row["account_id"] == "account-one"
    assert len(row["run_id"]) == 32
    assert row["media_bytes"] == 6
    assert "style" not in row


def test_stats_counts_outcomes_tracking_limits_and_storage(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join([
        json.dumps({
            "ts": "2026-01-01T00:00:00+00:00", "slot": "legacy",
            "ig_post_id": "ig_1", "tt_post_id": "tt_unconfirmed",
            "errors": [],
        }),
        json.dumps({
            "ts": "2026-02-01T00:00:00+00:00", "slot": "A", "account_id": "A",
            "ig_post_id": "", "tt_post_id": "", "media_bytes": 12,
            "errors": ["IG post: failed", "TT post: skipped (no session)"],
            "scheduled": True,
        }),
        "malformed",
    ]))
    media = tmp_path / "media"
    retained = tmp_path / "queue_media" / "batch"
    media.mkdir()
    retained.mkdir(parents=True)
    (media / "a").write_bytes(b"123")
    (retained / "b").write_bytes(b"12345")

    stats = aggregate_stats(history, media, retained.parent)
    assert stats["confirmed_platform_posts"] == 1
    assert stats["unconfirmed_platform_outcomes"] == 1
    assert stats["failed_platform_outcomes"] == 1
    assert stats["skipped_platform_outcomes"] == 1
    assert stats["content_items_attempted"] == 2
    assert stats["unique_accounts_used"] == 2
    assert stats["manual_executions"] == 1 and stats["scheduled_executions"] == 1
    assert stats["tracked_media_bytes"] == 12
    assert stats["tracked_media_items"] == 1
    assert stats["media_bytes_label"] == "since tracking began"
    assert stats["current_uploaded_media_bytes"] == 3
    assert stats["current_retained_queue_media_bytes"] == 5


def test_malformed_error_evidence_never_confirms_a_post_id():
    outcomes = classify_history_row({
        "ig_post_id": "concrete-id",
        "tt_post_id": "another-id",
        "errors": {"unexpected": "shape"},
    })

    assert outcomes == {"instagram": "unconfirmed", "tiktok": "unconfirmed"}


def test_legacy_manual_rows_with_same_timestamp_count_as_one_execution(tmp_path):
    """Cold-review repair (MEDIUM): legacy per-account rows do not inflate run totals."""
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps({
        "ts": "2026-01-01T00:00:00+00:00",
        "slot": account_id,
        "ig_post_id": "confirmed",
        "errors": [],
    }) for account_id in ("A", "B", "C")))

    stats = aggregate_stats(history, tmp_path / "media", tmp_path / "queue-media")

    assert stats["manual_executions"] == 1
    assert stats["content_items_attempted"] == 3


def test_stats_endpoint_uses_redirected_local_paths(client, tmp_history_file, tmp_path, monkeypatch):
    import backend.main as main

    media = tmp_path / "media-now"
    retained = tmp_path / "retained-now"
    media.mkdir()
    retained.mkdir()
    (media / "clip").write_bytes(b"1234")
    (retained / "queued").write_bytes(b"12")
    tmp_history_file.write_text(json.dumps({
        "ts": "2026-03-01T00:00:00+00:00", "slot": "A", "account_id": "A",
        "ig_post_id": "ig_1", "tt_post_id": "", "errors": [], "media_bytes": 4,
    }) + "\n")
    monkeypatch.setattr(main, "MEDIA_DIR", media)
    monkeypatch.setattr(main, "QUEUE_MEDIA_DIR", retained)

    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["confirmed_platform_posts"] == 1
    assert data["current_uploaded_media_bytes"] == 4
    assert data["current_retained_queue_media_bytes"] == 2
