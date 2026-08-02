"""Tests for Slice 3 — Scheduling / queue (DESIGN-scheduling.md §5).

Stage 1: run_guard extraction + queue data model/persistence/snapshots.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from backend import run_guard
from backend.queue import (
    QueuedBatch, SlotBatch, load_queue, save_queue, add_batch,
    cancel_batch, dismiss_batch, update_fire_time, _snapshot_media,
    _delete_snapshot,
)


# ---------------------------------------------------------------------------
# run_guard — extracted from main.py
# ---------------------------------------------------------------------------

class TestRunGuard:
    """DESIGN-scheduling.md §5d: run_guard.try_acquire/release shared between
    /api/post and the scheduler."""

    def test_acquire_and_release(self):
        run_guard._post_running = False
        assert run_guard.try_acquire() is True
        assert run_guard.is_running() is True
        assert run_guard.try_acquire() is False
        run_guard.release()
        assert run_guard.is_running() is False

    def test_release_idempotent(self):
        run_guard._post_running = False
        run_guard.release()
        assert run_guard.is_running() is False

    def test_existing_409_still_works(self, client, monkeypatch):
        """The 409 guard in /api/post must use run_guard (behavioural proof
        that the extraction preserved the contract)."""
        monkeypatch.setattr(run_guard, "_post_running", True)
        resp = client.post("/api/post", json={"slots": []})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# QueuedBatch serialization round-trip
# ---------------------------------------------------------------------------

class TestQueuedBatchSerialization:
    """DESIGN-scheduling.md §5a/5b: timezone-aware datetimes survive round-trip."""

    def _make_batch(self, **overrides):
        defaults = dict(
            id="abc123",
            fire_time=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            slots=[SlotBatch(slot="A", media_path="snap/a.mp4", caption="hi")],
            status="pending",
            headless=True,
        )
        defaults.update(overrides)
        return QueuedBatch(**defaults)

    def test_round_trip_preserves_datetimes(self):
        batch = self._make_batch()
        d = batch.to_dict()
        restored = QueuedBatch.from_dict(d)
        assert restored.fire_time == batch.fire_time
        assert restored.fire_time.tzinfo is not None
        assert restored.created_at == batch.created_at

    def test_round_trip_preserves_slots(self):
        batch = self._make_batch()
        restored = QueuedBatch.from_dict(batch.to_dict())
        assert len(restored.slots) == 1
        assert restored.slots[0].slot == "A"
        assert restored.slots[0].caption == "hi"

    def test_json_serializable(self):
        batch = self._make_batch()
        line = json.dumps(batch.to_dict())
        restored = QueuedBatch.from_dict(json.loads(line))
        assert restored.id == batch.id


# ---------------------------------------------------------------------------
# Queue file I/O
# ---------------------------------------------------------------------------

class TestQueueFileIO:
    """DESIGN-scheduling.md §5b: JSONL persistence, atomic rewrite, malformed-line handling."""

    def test_load_empty_file(self, tmp_path):
        qf = tmp_path / "queue.jsonl"
        assert load_queue(qf) == []

    def test_load_nonexistent_file(self, tmp_path):
        qf = tmp_path / "nope.jsonl"
        assert load_queue(qf) == []

    def test_save_and_load_round_trip(self, tmp_path):
        qf = tmp_path / "queue.jsonl"
        batch = QueuedBatch(
            id="b1", fire_time=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
            created_at=datetime.now(timezone.utc),
            slots=[SlotBatch(slot="A", media_path="x.mp4", caption="c")],
            status="pending", headless=True,
        )
        save_queue([batch], qf)
        loaded = load_queue(qf)
        assert len(loaded) == 1
        assert loaded[0].id == "b1"

    def test_malformed_line_skipped(self, tmp_path, capsys):
        qf = tmp_path / "queue.jsonl"
        good = QueuedBatch(
            id="good", fire_time=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
            created_at=datetime.now(timezone.utc),
            slots=[SlotBatch(slot="A", media_path="x.mp4", caption="c")],
            status="pending", headless=True,
        )
        qf.write_text(
            json.dumps(good.to_dict()) + "\n"
            + "THIS IS NOT JSON\n"
            + json.dumps(good.to_dict()).replace('"good"', '"good2"').replace('"good"', '"good2"') + "\n"
        )
        loaded = load_queue(qf)
        assert len(loaded) >= 1
        assert "ERROR" in capsys.readouterr().out

    def test_malformed_line_not_rewritten_on_load(self, tmp_path):
        """load_queue must never rewrite the file as a side effect."""
        qf = tmp_path / "queue.jsonl"
        content = '{"bad": true}\n'
        qf.write_text(content)
        load_queue(qf)
        assert qf.read_text() == content

    def test_save_is_atomic(self, tmp_path):
        """If save_queue fails mid-write, the original file is untouched."""
        qf = tmp_path / "queue.jsonl"
        qf.write_text('{"original": true}\n')

        class BadBatch:
            def to_dict(self):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            save_queue([BadBatch()], qf)
        assert qf.read_text() == '{"original": true}\n'
        assert not (tmp_path / "queue.tmp").exists()


# ---------------------------------------------------------------------------
# Media snapshots
# ---------------------------------------------------------------------------

class TestMediaSnapshots:
    """DESIGN-scheduling.md §5b: media snapshot lifecycle."""

    def test_snapshot_copies_media(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / "A_clip.mp4").write_bytes(b"video data")
        qmedia = tmp_path / "queue_media"

        slots = [SlotBatch(slot="A", media_path="A_clip.mp4", caption="c")]
        snapped = _snapshot_media("batch1", slots, media, qmedia)
        assert (qmedia / "batch1" / "A_clip.mp4").exists()
        assert (qmedia / "batch1" / "A_clip.mp4").read_bytes() == b"video data"
        assert snapped[0].media_path == str(qmedia / "batch1" / "A_clip.mp4")

    def test_snapshot_isolation_from_clear(self, tmp_path):
        """Clearing media after scheduling doesn't affect the queued batch."""
        media = tmp_path / "media"
        media.mkdir()
        (media / "A_clip.mp4").write_bytes(b"video")
        qmedia = tmp_path / "queue_media"

        _snapshot_media("batch1", [SlotBatch("A", "A_clip.mp4", "c")], media, qmedia)
        (media / "A_clip.mp4").unlink()
        assert (qmedia / "batch1" / "A_clip.mp4").read_bytes() == b"video"

    def test_delete_snapshot(self, tmp_path):
        qmedia = tmp_path / "queue_media"
        snap = qmedia / "batch1"
        snap.mkdir(parents=True)
        (snap / "file.mp4").write_bytes(b"x")
        _delete_snapshot("batch1", qmedia)
        assert not snap.exists()

    def test_delete_nonexistent_snapshot_is_noop(self, tmp_path):
        _delete_snapshot("nope", tmp_path / "queue_media")


# ---------------------------------------------------------------------------
# add_batch / cancel / dismiss / update_fire_time
# ---------------------------------------------------------------------------

class TestQueueOperations:
    """DESIGN-scheduling.md §5a-5c: queue mutation operations."""

    def _setup(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / "A_clip.mp4").write_bytes(b"video")
        qf = tmp_path / "queue.jsonl"
        qmedia = tmp_path / "queue_media"
        return media, qf, qmedia

    def test_add_batch(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        ft = datetime.now(timezone.utc) + timedelta(hours=1)
        slots = [SlotBatch(slot="A", media_path="A_clip.mp4", caption="hi")]
        batch = add_batch(ft, slots, True, qf, media, qmedia)
        assert batch.status == "pending"
        assert batch.id
        loaded = load_queue(qf)
        assert len(loaded) == 1

    def test_add_batch_rejects_naive_fire_time(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        with pytest.raises(ValueError, match="timezone-aware"):
            add_batch(datetime(2026, 12, 1, 9, 0), [], True, qf, media, qmedia)

    def test_add_batch_rejects_past_fire_time(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="future"):
            add_batch(past, [], True, qf, media, qmedia)

    def test_cancel_pending_batch(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        ft = datetime.now(timezone.utc) + timedelta(hours=1)
        slots = [SlotBatch("A", "A_clip.mp4", "c")]
        batch = add_batch(ft, slots, True, qf, media, qmedia)
        assert cancel_batch(batch.id, qf, qmedia) is True
        assert load_queue(qf) == []
        assert not (qmedia / batch.id).exists()

    def test_cancel_nonpending_returns_false(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        ft = datetime.now(timezone.utc) + timedelta(hours=1)
        batch = add_batch(ft, [SlotBatch("A", "A_clip.mp4", "c")], True, qf, media, qmedia)
        batches = load_queue(qf)
        batches[0].status = "running"
        save_queue(batches, qf)
        assert cancel_batch(batch.id, qf, qmedia) is False

    def test_dismiss_interrupted_batch(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        ft = datetime.now(timezone.utc) + timedelta(hours=1)
        batch = add_batch(ft, [SlotBatch("A", "A_clip.mp4", "c")], True, qf, media, qmedia)
        batches = load_queue(qf)
        batches[0].status = "interrupted"
        save_queue(batches, qf)
        assert dismiss_batch(batch.id, qf, qmedia) is True
        assert load_queue(qf) == []

    def test_dismiss_non_interrupted_returns_false(self, tmp_path):
        _, qf, qmedia = self._setup(tmp_path)
        assert dismiss_batch("nope", qf, qmedia) is False

    def test_update_fire_time(self, tmp_path):
        media, qf, qmedia = self._setup(tmp_path)
        ft = datetime.now(timezone.utc) + timedelta(hours=1)
        batch = add_batch(ft, [SlotBatch("A", "A_clip.mp4", "c")], True, qf, media, qmedia)
        new_ft = datetime.now(timezone.utc) + timedelta(hours=2)
        assert update_fire_time(batch.id, new_ft, qf) is True
        loaded = load_queue(qf)
        assert loaded[0].fire_time == new_ft

    def test_update_fire_time_rejects_naive(self, tmp_path):
        with pytest.raises(ValueError, match="timezone-aware"):
            update_fire_time("any", datetime(2026, 12, 1))

    def test_update_fire_time_rejects_past(self, tmp_path):
        with pytest.raises(ValueError, match="future"):
            update_fire_time("any", datetime(2020, 1, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Stage 2: Queue API endpoints (DESIGN-scheduling.md §5c)
# ---------------------------------------------------------------------------

class TestQueueAPI:
    """TestClient tests for /api/queue endpoints."""

    def _stub_queue_paths(self, monkeypatch, tmp_path):
        """Redirect queue file and media dir to tmp_path."""
        import backend.queue as queue_mod
        qf = tmp_path / "queue.jsonl"
        qmedia = tmp_path / "queue_media"
        monkeypatch.setattr(queue_mod, "QUEUE_FILE", qf)
        monkeypatch.setattr(queue_mod, "QUEUE_MEDIA_DIR", qmedia)
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_path / "media")
        return qf, qmedia

    def _future_iso(self, hours=2):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    def test_post_queue_creates_batch(self, client, tmp_media, monkeypatch, tmp_path):
        qf, qmedia = self._stub_queue_paths(monkeypatch, tmp_path)
        import backend.queue as queue_mod
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")

        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "test", "media_type": "video"}],
            "fire_time": self._future_iso(),
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "fire_time" in data

    def test_post_queue_rejects_naive_fire_time(self, client, tmp_media, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "x.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": "2026-12-01T09:00:00",
        })
        assert resp.status_code == 422

    def test_post_queue_rejects_past_fire_time(self, client, tmp_media, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "x.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": "2020-01-01T00:00:00+00:00",
        })
        assert resp.status_code == 422

    def test_post_queue_rejects_duplicate_slots(self, client, tmp_media, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")
        resp = client.post("/api/queue", json={
            "slots": [
                {"slot": "A", "filename": "A_clip.mp4", "caption": "c1", "media_type": "video"},
                {"slot": "A", "filename": "A_clip.mp4", "caption": "c2", "media_type": "video"},
            ],
            "fire_time": self._future_iso(),
        })
        assert resp.status_code == 400

    def test_post_queue_rejects_missing_media(self, client, tmp_media, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "nope.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": self._future_iso(),
        })
        assert resp.status_code == 400

    def test_post_queue_rejects_unknown_slot(self, client, tmp_media, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "Z", "filename": "x.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": self._future_iso(),
        })
        assert resp.status_code == 400

    def test_get_queue_lists_batches(self, client, tmp_media, monkeypatch, tmp_path):
        qf, qmedia = self._stub_queue_paths(monkeypatch, tmp_path)
        import backend.queue as queue_mod
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")

        client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": self._future_iso(),
        })
        resp = client.get("/api/queue")
        assert resp.status_code == 200
        assert len(resp.json()["batches"]) == 1

    def test_delete_cancels_pending_batch(self, client, tmp_media, monkeypatch, tmp_path):
        qf, qmedia = self._stub_queue_paths(monkeypatch, tmp_path)
        import backend.queue as queue_mod
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")

        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": self._future_iso(),
        })
        batch_id = resp.json()["id"]
        del_resp = client.delete(f"/api/queue/{batch_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "cancelled"
        assert len(client.get("/api/queue").json()["batches"]) == 0

    def test_delete_nonexistent_returns_404(self, client, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        assert client.delete("/api/queue/nope").status_code == 404

    def test_patch_reschedules_pending(self, client, tmp_media, monkeypatch, tmp_path):
        qf, qmedia = self._stub_queue_paths(monkeypatch, tmp_path)
        import backend.queue as queue_mod
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")

        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c", "media_type": "video"}],
            "fire_time": self._future_iso(2),
        })
        batch_id = resp.json()["id"]
        new_ft = self._future_iso(5)
        patch_resp = client.patch(f"/api/queue/{batch_id}", json={"fire_time": new_ft})
        assert patch_resp.status_code == 200

    def test_patch_rejects_naive_time(self, client, monkeypatch, tmp_path):
        self._stub_queue_paths(monkeypatch, tmp_path)
        resp = client.patch("/api/queue/x", json={"fire_time": "2026-12-01T09:00:00"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Stage 3: Scheduler loop + execute_batch (DESIGN-scheduling.md §5d)
# ---------------------------------------------------------------------------

import asyncio
from fastapi.testclient import TestClient
from backend.queue import save_queue
from backend.scheduler import startup_sweep, execute_batch
from backend.notifier import Notifier
from backend.models import PostResult


class _FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


def _make_batch(tmp_path, status="pending", fire_time=None, slot_ids=None):
    """Helper: create a QueuedBatch on disk with media snapshot."""
    from backend.queue import _snapshot_media
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    qf = tmp_path / "queue.jsonl"
    qmedia = tmp_path / "queue_media"

    slots_ids = slot_ids or ["A"]
    raw_slots = []
    for sid in slots_ids:
        fname = f"{sid}_clip.mp4"
        (media / fname).write_bytes(b"video")
        raw_slots.append(SlotBatch(slot=sid, media_path=fname, caption=f"cap_{sid}"))

    snapped = _snapshot_media("batch1", raw_slots, media, qmedia)
    ft = fire_time or datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
    batch = QueuedBatch(
        id="batch1", fire_time=ft, created_at=datetime.now(timezone.utc),
        slots=snapped, status=status, headless=True,
    )
    save_queue([batch], qf)
    return batch, qf, qmedia


class TestStartupSweep:
    """DESIGN §5d: startup sweep marks stale running → interrupted, returns overdue."""

    def test_running_becomes_interrupted(self, tmp_path):
        batch, qf, _ = _make_batch(tmp_path, status="running")
        notifier = _FakeNotifier()
        overdue = asyncio.run(startup_sweep(qf, notifier))
        loaded = load_queue(qf)
        assert loaded[0].status == "interrupted"
        assert len(overdue) == 0

    def test_interrupted_notification_sent(self, tmp_path):
        batch, qf, _ = _make_batch(tmp_path, status="running")
        notifier = _FakeNotifier()
        asyncio.run(startup_sweep(qf, notifier))
        assert any("interrupted" in s["title"] for s in notifier.sent)

    def test_overdue_pending_returned(self, tmp_path):
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        batch, qf, _ = _make_batch(tmp_path, status="pending", fire_time=past)
        notifier = _FakeNotifier()
        overdue = asyncio.run(startup_sweep(qf, notifier))
        assert len(overdue) == 1
        assert overdue[0].id == "batch1"

    def test_future_pending_not_returned(self, tmp_path):
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        batch, qf, _ = _make_batch(tmp_path, status="pending", fire_time=future)
        notifier = _FakeNotifier()
        overdue = asyncio.run(startup_sweep(qf, notifier))
        assert len(overdue) == 0

    def test_interrupted_never_re_executed(self, tmp_path):
        """An interrupted batch must stay interrupted, never appear as overdue."""
        batch, qf, _ = _make_batch(tmp_path, status="interrupted")
        notifier = _FakeNotifier()
        overdue = asyncio.run(startup_sweep(qf, notifier))
        assert len(overdue) == 0
        loaded = load_queue(qf)
        assert loaded[0].status == "interrupted"


class TestExecuteBatch:
    """DESIGN §5d: execute_batch flow."""

    def _setup(self, tmp_path):
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        batch, qf, qmedia = _make_batch(tmp_path, status="pending", fire_time=past)
        hf = tmp_path / "history.jsonl"
        notifier = _FakeNotifier()

        async def fake_check(slot, platform):
            return "live"

        async def fake_post_all(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig_ok", tt_post_id="tt_ok") for s in slots]

        run_guard._post_running = False
        return batch, qf, qmedia, hf, notifier, fake_check, fake_post_all

    def test_successful_batch(self, tmp_path):
        batch, qf, qmedia, hf, notifier, check, post = self._setup(tmp_path)
        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check, post)
        )
        assert load_queue(qf) == []
        assert hf.exists()
        records = [json.loads(l) for l in hf.read_text().strip().splitlines()]
        assert records[0]["scheduled"] is True
        assert records[0]["batch_id"] == "batch1"
        assert not (qmedia / "batch1").exists()

    def test_guard_busy_leaves_pending(self, tmp_path, monkeypatch):
        batch, qf, qmedia, hf, notifier, check, post = self._setup(tmp_path)
        monkeypatch.setattr(run_guard, "_post_running", True)
        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check, post)
        )
        loaded = load_queue(qf)
        assert loaded[0].status == "pending"

    def test_guard_released_on_exception(self, tmp_path):
        batch, qf, qmedia, hf, notifier, check, _ = self._setup(tmp_path)

        async def boom_post(slots, headless=True, notifier=None):
            raise RuntimeError("boom")

        run_guard._post_running = False
        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check, boom_post)
        )
        assert run_guard._post_running is False
        assert load_queue(qf) == [], "exception path must prune — no zombie entry"
        # Snapshot retention was INVERTED by audit fix A2 (finding #1): on a
        # crash the media is the only surviving evidence of what was being
        # posted, so it is deliberately kept. See test_audit_phase1.py
        # ::TestCrashPreservesAuditTrail::test_crash_keeps_the_media_snapshot.
        assert (qmedia / "batch1").exists(), "exception path must keep snapshot"

    def test_single_platform_expired_still_posts(self, tmp_path):
        """Review fix #1: only TT expired → slot still proceeds (IG is live)."""
        batch, qf, qmedia, hf, notifier, _, post = self._setup(tmp_path)

        async def check_one_expired(slot, platform):
            return "expired" if platform == "tiktok" else "live"

        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check_one_expired, post)
        )
        assert any("skipped" in s["title"] for s in notifier.sent)
        assert load_queue(qf) == [], "slot should still post and complete"
        assert hf.exists(), "history should be written for the posted slot"

    def test_both_platforms_expired_creates_skip_result(self, tmp_path):
        """Review fix #1+3: both platforms expired → slot fully skipped with
        synthetic PostResult recording the skip in history."""
        batch, qf, qmedia, hf, notifier, _, post = self._setup(tmp_path)

        async def check_all_expired(slot, platform):
            return "expired"

        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check_all_expired, post)
        )
        assert load_queue(qf) == []
        assert hf.exists()
        records = [json.loads(l) for l in hf.read_text().strip().splitlines()]
        assert any("pre-flight" in (r.get("errors") or [""])[0] for r in records)

    def test_check_error_proceeds(self, tmp_path):
        """check_error means unknown — proceed to post (maintainer decision)."""
        batch, qf, qmedia, hf, notifier, _, post = self._setup(tmp_path)

        async def check_error(slot, platform):
            return "check_error"

        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check_error, post)
        )
        loaded = load_queue(qf)
        assert len(loaded) == 0  # batch completed (pruned)

    def test_snapshot_deleted_on_terminal(self, tmp_path):
        batch, qf, qmedia, hf, notifier, check, post = self._setup(tmp_path)
        assert (qmedia / "batch1").exists()
        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check, post)
        )
        assert not (qmedia / "batch1").exists()

    def test_partial_status_on_mixed_results(self, tmp_path):
        batch, qf, qmedia, hf, notifier, check, _ = self._setup(tmp_path)

        async def mixed_post(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], errors=["fail"] if s["slot"] == "A" else []) for s in slots]

        # Need two slots for partial
        from backend.queue import _snapshot_media
        media = tmp_path / "media"
        (media / "B_clip.mp4").write_bytes(b"v2")
        batch.slots.append(SlotBatch("B", str(qmedia / "batch1" / "B_clip.mp4"), "cap_B"))
        (qmedia / "batch1" / "B_clip.mp4").write_bytes(b"v2")
        save_queue([batch], qf)

        asyncio.run(
            execute_batch(batch, qf, qmedia, hf, notifier, check, mixed_post)
        )
        records = [json.loads(l) for l in hf.read_text().strip().splitlines()]
        assert len(records) == 2


# ---------------------------------------------------------------------------
# Review fix #9: scheduler_loop integration test
# ---------------------------------------------------------------------------

from backend.scheduler import scheduler_loop


class TestSchedulerLoop:
    """Review fix #9: scheduler_loop dispatches overdue batches."""

    def test_fires_overdue_batch_from_startup(self, tmp_path):
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        batch, qf, qmedia = _make_batch(tmp_path, status="pending", fire_time=past)
        hf = tmp_path / "history.jsonl"
        notifier = _FakeNotifier()

        async def fake_check(slot, platform):
            return "live"

        async def fake_post(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt") for s in slots]

        async def run():
            import unittest.mock
            with unittest.mock.patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                try:
                    await scheduler_loop(qf, qmedia, hf, notifier, fake_check, fake_post)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())
        assert load_queue(qf) == [], "overdue batch should be executed and pruned"
        assert hf.exists()

    def test_polls_and_fires_due_batch(self, tmp_path):
        """Due batch found during the poll loop (not startup sweep)."""
        future_then_now = datetime(2099, 1, 1, tzinfo=timezone.utc)
        batch, qf, qmedia = _make_batch(tmp_path, status="pending", fire_time=future_then_now)
        hf = tmp_path / "history.jsonl"
        notifier = _FakeNotifier()

        async def fake_check(slot, platform):
            return "live"

        posted = []

        async def fake_post(slots, headless=True, notifier=None):
            posted.append(True)
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt") for s in slots]

        call_count = 0

        async def patched_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                batches = load_queue(qf)
                for b in batches:
                    b.fire_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
                save_queue(batches, qf)
                return
            raise asyncio.CancelledError()

        async def run():
            import unittest.mock
            with unittest.mock.patch("asyncio.sleep", patched_sleep):
                try:
                    await scheduler_loop(qf, qmedia, hf, notifier, fake_check, fake_post)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())
        assert posted, "batch should have been fired during the poll loop"


# ---------------------------------------------------------------------------
# Review fix #10: lifespan / SCHEDULER_ENABLED integration test
# ---------------------------------------------------------------------------


class TestLifespan:
    """Review fix #10: verify the lifespan context manager actually runs."""

    def test_scheduler_disabled_no_task(self, monkeypatch):
        import backend.main as main
        monkeypatch.setattr(main, "SCHEDULER_ENABLED", False)
        with TestClient(main.app) as c:
            resp = c.get("/api/accounts")
            assert resp.status_code == 200

    def test_scheduler_enabled_starts_loop(self, monkeypatch):
        import backend.main as main
        import backend.scheduler as sched_mod
        started = []

        async def fake_loop(**kwargs):
            started.append(True)
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                pass

        monkeypatch.setattr(main, "SCHEDULER_ENABLED", True)
        monkeypatch.setattr(sched_mod, "scheduler_loop", fake_loop)
        with TestClient(main.app) as c:
            resp = c.get("/")
            assert resp.status_code == 200
        assert started, "scheduler_loop should have been called by lifespan"
