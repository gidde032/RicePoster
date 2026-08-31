"""Batch 4 — queue and scheduler durability (#8, #5, #6).

These two modules are the *unattended* posting path: a durability defect here
fires against live accounts with nobody watching, or silently fails to fire at
all. Every test below is fully mocked; none reaches a poster or a network.

Issue coverage:

- #8 — `SlotBatch.style` removed, legacy rows carrying it still load.
- #5 — a failed scheduled-history write reaches the maintainer as a push.
- #6 — the named scheduler/queue contract branches, the queue-modify-op group
  from audit finding TS-1, and the shared fire-time validator.
"""

import asyncio
import json
import unittest.mock
from datetime import datetime, timedelta, timezone

import pytest

import backend.queue as queue_mod
from backend import run_guard, scheduler
from backend.models import (
    API_MIN_LEAD, QUEUE_MIN_LEAD, PostResult, ScheduleRequest,
    validate_future_fire_time,
)
from backend.notifier import Notifier
from backend.queue import (
    QueuedBatch, SlotBatch, _delete_snapshot, _snapshot_media, add_batch,
    cancel_batch, dismiss_batch, load_queue, save_queue, update_fire_time,
)


class _FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True

    def titles(self):
        return [m["title"] for m in self.sent]


def _make_batch(tmp_path, status="pending", fire_time=None, slot_ids=("A",),
                batch_id="batch1"):
    """A QueuedBatch on disk with a real media snapshot, as the scheduler
    would find it."""
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    qf = tmp_path / "queue.jsonl"
    qmedia = tmp_path / "queue_media"

    raw = []
    for sid in slot_ids:
        fname = f"{sid}_clip.mp4"
        (media / fname).write_bytes(b"video")
        raw.append(SlotBatch(slot=sid, media_path=fname, caption=f"cap_{sid}"))

    snapped = _snapshot_media(batch_id, raw, media, qmedia)
    batch = QueuedBatch(
        id=batch_id,
        fire_time=fire_time or datetime(2020, 1, 1, tzinfo=timezone.utc),
        created_at=datetime.now(timezone.utc),
        slots=snapped, status=status, headless=True,
    )
    save_queue([batch], qf)
    return batch, qf, qmedia


def _tmp_queue_paths(tmp_path):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    return media, tmp_path / "queue.jsonl", tmp_path / "queue_media"


# ---------------------------------------------------------------------------
# #8 — SlotBatch.style is gone, and legacy rows still load
# ---------------------------------------------------------------------------

_LEGACY_ROW = {
    "id": "legacy1",
    "fire_time": "2099-01-01T09:00:00+00:00",
    "created_at": "2026-07-20T09:00:00+00:00",
    "slots": [{"slot": "A", "media_path": "/tmp/a.mp4", "caption": "cap",
               "style": "generic"}],
    "status": "pending",
    "headless": True,
    "results": None,
}


class TestRetiredStyleField:
    """#8: the field was always written empty and never read back."""

    def test_slotbatch_no_longer_has_a_style_field(self):
        """Pins the removal so a future reader does not restore it as an
        oversight (same rationale as `generate_all_captions`, Batch 3)."""
        assert not hasattr(SlotBatch("A", "a.mp4", "cap"), "style")

    def test_legacy_row_would_break_a_naive_constructor(self):
        """Proves the tolerant read path is load-bearing rather than defensive
        decoration.

        This is the failure the compatibility path exists to prevent. Note the
        exception type: `load_queue` catches everything and reports it as a
        *malformed line*, so without the tolerant read a legacy batch would be
        silently skipped — never fired, never surfaced — rather than raising.
        """
        with pytest.raises(TypeError):
            SlotBatch(**_LEGACY_ROW["slots"][0])

    def test_legacy_row_loads_and_is_not_treated_as_malformed(self, tmp_path,
                                                              capsys):
        qf = tmp_path / "queue.jsonl"
        qf.write_text(json.dumps(_LEGACY_ROW) + "\n")

        loaded = load_queue(qf)

        assert len(loaded) == 1, "a legacy row must survive the schema change"
        assert loaded[0].slots[0].caption == "cap"
        assert loaded[0].slots[0].media_path == "/tmp/a.mp4"
        assert "malformed" not in capsys.readouterr().out

    def test_retired_style_key_is_dropped_silently(self, tmp_path, capsys):
        """The scheduler re-reads the queue every 30s. A known-retired key must
        not produce a log line on every poll."""
        qf = tmp_path / "queue.jsonl"
        qf.write_text(json.dumps(_LEGACY_ROW) + "\n")

        load_queue(qf)

        assert capsys.readouterr().out == ""

    def test_unknown_slot_key_is_dropped_but_reported(self, tmp_path, capsys,
                                                      monkeypatch):
        """An unrecognised key is not `style` — it means the row was written by
        something this code does not understand, which is worth saying once."""
        monkeypatch.setattr(queue_mod, "_REPORTED_SLOT_KEYS", set())
        row = json.loads(json.dumps(_LEGACY_ROW))
        row["slots"][0]["sharpness"] = 3

        qf = tmp_path / "queue.jsonl"
        qf.write_text(json.dumps(row) + "\n")
        loaded = load_queue(qf)

        out = capsys.readouterr().out
        assert len(loaded) == 1
        assert "sharpness" in out
        assert "style" not in out, "the retired key must stay silent"

    def test_unknown_key_is_reported_once_per_process_not_per_read(
            self, tmp_path, capsys, monkeypatch):
        """Review 2026-07-30. The queue is re-read roughly four times per batch
        execution plus every 30-second poll. A per-read notice would put
        thousands of lines a day onto the same console channel that carries the
        malformed-line and history-failure records that actually matter.
        """
        monkeypatch.setattr(queue_mod, "_REPORTED_SLOT_KEYS", set())
        row = json.loads(json.dumps(_LEGACY_ROW))
        row["slots"][0]["sharpness"] = 3
        qf = tmp_path / "queue.jsonl"
        qf.write_text(json.dumps(row) + "\n")

        load_queue(qf)
        capsys.readouterr()          # discard the first, expected notice

        for _ in range(5):
            load_queue(qf)

        assert capsys.readouterr().out == "", (
            "the unknown-field notice repeated on a subsequent queue read"
        )

    def test_non_dict_slot_is_malformed_without_a_bogus_notice(self, tmp_path,
                                                              capsys,
                                                              monkeypatch):
        """Review 2026-07-30: `set("A")` is a set of characters, so a slot
        entry that is a string used to emit a per-character 'unknown field'
        notice on its way to failing. The diagnostic pointed at the wrong
        problem entirely."""
        monkeypatch.setattr(queue_mod, "_REPORTED_SLOT_KEYS", set())
        row = json.loads(json.dumps(_LEGACY_ROW))
        row["slots"] = ["A"]
        qf = tmp_path / "queue.jsonl"
        qf.write_text(json.dumps(row) + "\n")

        assert load_queue(qf) == []
        out = capsys.readouterr().out
        assert "malformed" in out, "a row we cannot parse must be reported"
        assert "unknown slot field" not in out

    def test_new_rows_do_not_persist_style(self, tmp_path):
        media, qf, qmedia = _tmp_queue_paths(tmp_path)
        (media / "A_clip.mp4").write_bytes(b"video")
        add_batch(datetime.now(timezone.utc) + timedelta(hours=1),
                  [SlotBatch("A", "A_clip.mp4", "cap")], True, qf, media, qmedia)

        assert "style" not in qf.read_text()

    def test_snapshot_preserves_slot_fields(self, tmp_path):
        """`_snapshot_media` rebuilds each SlotBatch by hand; dropping a field
        from the dataclass must not quietly drop a surviving one."""
        media, _, qmedia = _tmp_queue_paths(tmp_path)
        (media / "A_clip.mp4").write_bytes(b"video")

        snapped = _snapshot_media(
            "b1", [SlotBatch("A", "A_clip.mp4", "cap")], media, qmedia)

        assert snapped[0].slot == "A"
        assert snapped[0].caption == "cap"
        assert snapped[0].media_path.endswith("A_clip.mp4")


# ---------------------------------------------------------------------------
# #5 — a failed scheduled-history write must reach the maintainer
# ---------------------------------------------------------------------------

_HISTORY_FAIL_TITLE = "RicePoster: scheduled run not recorded"


def _run_batch(batch, qf, qmedia, hf, notifier, check="live", post=None):
    async def fake_check(slot, platform):
        return check

    async def default_post(slots, headless=True, notifier=None):
        return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                for s in slots]

    asyncio.run(scheduler.execute_batch(
        batch, qf, qmedia, hf, notifier, fake_check, post or default_post))


class TestHistoryWriteFailureIsSurfaced:
    """#5: in unattended operation the console line may be the only record."""

    def test_terminal_batch_notifies_when_history_cannot_be_written(
            self, tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path)
        batch.slots[0].slot = "private-handle"
        batch.slots[0].account_id = "private-handle"
        save_queue([batch], qf)
        notifier = _FakeNotifier()
        # A directory where the history file should be: open(..., "a") raises.
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        _run_batch(batch, qf, qmedia, hf, notifier)

        assert _HISTORY_FAIL_TITLE in notifier.titles()
        msg = next(m for m in notifier.sent
                   if m["title"] == _HISTORY_FAIL_TITLE)
        assert msg["priority"] == "high"
        assert batch.id[:8] in msg["body"]
        assert "account 1" in msg["body"]
        assert "private-handle" not in msg["body"]

    def test_evidence_survives_a_failed_history_write(self, tmp_path):
        """The queue row and the media snapshot are the only remaining record,
        so neither may be pruned on this path."""
        batch, qf, qmedia = _make_batch(tmp_path)
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        _run_batch(batch, qf, qmedia, hf, _FakeNotifier())

        remaining = load_queue(qf)
        assert len(remaining) == 1, "queue record must be retained"
        assert remaining[0].status == "done"
        assert (qmedia / batch.id).exists(), "media snapshot must be retained"

    def test_crash_path_also_notifies_and_keeps_the_record_running(self,
                                                                  tmp_path):
        """The worst case in the system: the slots may have posted, history
        failed, and pruning would erase the last trace."""
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        async def exploding_post(slots, headless=True, notifier=None):
            raise RuntimeError("browser died mid-run")

        _run_batch(batch, qf, qmedia, hf, notifier, post=exploding_post)

        titles = notifier.titles()
        assert _HISTORY_FAIL_TITLE in titles
        assert "RicePoster: scheduled batch failed" in titles, (
            "the crash push and the not-recorded push are different facts and "
            "must both be sent"
        )
        remaining = load_queue(qf)
        assert len(remaining) == 1
        assert remaining[0].status == "running", (
            "left as 'running' so startup_sweep converts it to 'interrupted'"
        )

    def test_crash_notification_does_not_claim_a_terminal_status(self,
                                                                 tmp_path):
        """Review 2026-07-30. The crash branch deliberately leaves the queue
        record `running` for the next startup sweep to reclassify, and
        `dismiss_batch` only accepts `interrupted`. Naming a terminal status
        here asserted a state the system does not hold, and telling the
        maintainer to dismiss the entry described an action unavailable until
        the server restarts.
        """
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        async def exploding_post(slots, headless=True, notifier=None):
            raise RuntimeError("browser died mid-run")

        _run_batch(batch, qf, qmedia, hf, notifier, post=exploding_post)

        body = next(m["body"] for m in notifier.sent
                    if m["title"] == _HISTORY_FAIL_TITLE)
        assert "crashed mid-run" in body
        assert "finished" not in body, (
            "the crash path holds no final status to report"
        )
        assert "dismiss" not in body.lower(), (
            "the entry cannot be dismissed until the startup sweep runs"
        )

    def test_successful_run_is_never_reported_as_failed(self, tmp_path,
                                                        monkeypatch):
        """Review 2026-07-30, reproduced with a disposable probe.

        Whatever makes the history write fail — a full or read-only disk — is
        just as likely to make the follow-up status write fail. That second
        failure used to escape into the crash handler, which forces the status
        to 'failed' and tells the maintainer the slots may not have posted.

        They *had* posted. Acting on that message means a duplicate live post,
        which is the worst outcome this system can produce, so the true outcome
        must survive a failure to record it.
        """
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        real_update = scheduler._update_status
        calls = {"n": 0}

        def flaky_update(batch_id, status, queue_file=None):
            calls["n"] += 1
            if calls["n"] >= 2:      # the "running" write lands; the final one does not
                raise OSError("No space left on device")
            return real_update(batch_id, status, queue_file)

        monkeypatch.setattr(scheduler, "_update_status", flaky_update)

        _run_batch(batch, qf, qmedia, hf, notifier)   # every slot succeeds

        body = next(m["body"] for m in notifier.sent
                    if m["title"] == _HISTORY_FAIL_TITLE)
        assert "finished 'done'" in body, (
            f"a fully successful run was reported as something else: {body}"
        )
        assert "RicePoster: scheduled batch failed" not in notifier.titles(), (
            "a successful run must not also raise the crash alarm"
        )
        assert calls["n"] >= 2, "the failing status write was never reached"

    def test_no_notification_when_history_is_written(self, tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()

        _run_batch(batch, qf, qmedia, tmp_path / "history.jsonl", notifier)

        assert _HISTORY_FAIL_TITLE not in notifier.titles()
        assert load_queue(qf) == [], "a recorded batch is pruned as before"

    def test_prune_failure_does_not_duplicate_history_or_leave_batch_runnable(
            self, tmp_path, monkeypatch):
        """A cleanup error happens after durable history and must not enter the
        crash recorder, which would append the same execution a second time."""
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()
        history = tmp_path / "history.jsonl"
        monkeypatch.setattr(
            scheduler,
            "_prune_batch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),
        )

        _run_batch(batch, qf, qmedia, history, notifier)

        assert len(history.read_text().splitlines()) == len(batch.slots)
        remaining = load_queue(qf)
        assert len(remaining) == 1
        assert remaining[0].status == "done"
        assert (qmedia / batch.id).is_dir()
        assert "RicePoster: scheduled batch cleanup incomplete" in notifier.titles()

    def test_notifier_raising_on_that_push_cannot_kill_the_scheduler(
            self, tmp_path):
        """A broken notifier must not escalate a failed history write into a
        dead scheduler loop (Phase 2 audit, finding #3 — same shape)."""
        batch, qf, qmedia = _make_batch(tmp_path)
        hf = tmp_path / "history_is_a_dir"
        hf.mkdir()

        class _Boom(Notifier):
            async def send(self, title="", body="", priority="default"):
                raise RuntimeError("ntfy exploded")

        _run_batch(batch, qf, qmedia, hf, _Boom())  # must not raise

        assert run_guard._post_running is False, "the run guard must be released"


# ---------------------------------------------------------------------------
# #6 — named contract branches
# ---------------------------------------------------------------------------

class TestFireTimeUpdateMisses:
    """#6: the not-found / not-pending false returns."""

    def test_update_fire_time_unknown_batch_returns_false(self, tmp_path):
        _, qf, _ = _tmp_queue_paths(tmp_path)
        qf.write_text("")
        assert update_fire_time(
            "nope", datetime.now(timezone.utc) + timedelta(hours=1), qf) is False

    def test_update_fire_time_ignores_a_running_batch(self, tmp_path):
        """Rescheduling a batch that is already posting must not move its fire
        time underneath the run."""
        batch, qf, _ = _make_batch(tmp_path, status="running")
        original = load_queue(qf)[0].fire_time

        result = update_fire_time(
            batch.id, datetime.now(timezone.utc) + timedelta(hours=5), qf)

        assert result is False
        assert load_queue(qf)[0].fire_time == original

    def test_update_status_miss_returns_false_without_rewriting(self, tmp_path):
        """A no-op write on this path would put every other queue entry through
        an atomic full-file rewrite exactly when on-disk state is already
        confusing (review 2026-07-26, finding #10)."""
        _, qf, _ = _make_batch(tmp_path)
        before = qf.read_text()

        assert scheduler._update_status("no-such-batch", "running", qf) is False
        assert qf.read_text() == before


class TestQueueModifyOps:
    """Audit finding TS-1: the status filter is what stops a running batch
    being cancelled mid-post, and it deserves a direct test."""

    def test_cancel_refuses_a_running_batch_and_keeps_its_media(self, tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path, status="running")

        assert cancel_batch(batch.id, qf, qmedia) is False
        assert len(load_queue(qf)) == 1
        assert (qmedia / batch.id).exists(), (
            "a refused cancel must not delete the media of a live run"
        )

    def test_cancel_unknown_batch_is_a_false_not_an_error(self, tmp_path):
        _, qf, qmedia = _tmp_queue_paths(tmp_path)
        qf.write_text("")
        assert cancel_batch("nope", qf, qmedia) is False

    def test_dismiss_refuses_a_pending_batch_and_keeps_its_media(self, tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path, status="pending")

        assert dismiss_batch(batch.id, qf, qmedia) is False
        assert len(load_queue(qf)) == 1
        assert (qmedia / batch.id).exists()

    def test_delete_snapshot_touches_only_the_named_batch(self, tmp_path):
        media, _, qmedia = _tmp_queue_paths(tmp_path)
        (media / "A_clip.mp4").write_bytes(b"video")
        _snapshot_media("keep", [SlotBatch("A", "A_clip.mp4", "c")], media, qmedia)
        _snapshot_media("drop", [SlotBatch("A", "A_clip.mp4", "c")], media, qmedia)

        _delete_snapshot("drop", qmedia)

        assert not (qmedia / "drop").exists()
        assert (qmedia / "keep").exists()


class TestPreflightNoSession:
    """#6: the no_session branch, distinct from expired."""

    def test_slot_with_no_session_on_both_platforms_is_not_posted(self,
                                                                 tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path)
        notifier = _FakeNotifier()
        posted = []

        async def never_posts(slots, headless=True, notifier=None):
            posted.append(slots)
            return []

        _run_batch(batch, qf, qmedia, tmp_path / "h.jsonl", notifier,
                   check="no_session", post=never_posts)

        assert posted == [], "post_all must not be called for a skipped slot"
        rows = [json.loads(line)
                for line in (tmp_path / "h.jsonl").read_text().splitlines()]
        assert any("no_session" in e for r in rows for e in r["errors"])
        assert any("no_session" in m["body"] for m in notifier.sent)

    def test_no_session_on_one_platform_still_attempts_the_other(self,
                                                                tmp_path):
        """A single live platform is worth attempting; the verdict rides
        through to post_slot so it cannot re-derive a wrong answer."""
        batch, qf, qmedia = _make_batch(tmp_path)
        seen = {}

        async def check(slot, platform):
            return "no_session" if platform == "tiktok" else "live"

        async def capture(slots, headless=True, notifier=None):
            seen["slots"] = slots
            return [PostResult(slot=s["slot"], ig_post_id="ig") for s in slots]

        asyncio.run(scheduler.execute_batch(
            batch, qf, qmedia, tmp_path / "h.jsonl", _FakeNotifier(),
            check, capture))

        assert seen["slots"][0]["skip_platforms"] == {"tiktok"}


class TestGuardBusyIsRetried:
    """#6: a batch that could not run because a manual post held the guard must
    be picked up by a later poll, not dropped."""

    def test_batch_blocked_by_the_run_guard_fires_on_a_later_poll(self,
                                                                  tmp_path):
        batch, qf, qmedia = _make_batch(tmp_path)
        hf = tmp_path / "history.jsonl"
        posted = []

        async def check(slot, platform):
            return "live"

        async def post(slots, headless=True, notifier=None):
            posted.append(len(posted) + 1)
            return [PostResult(slot=s["slot"], ig_post_id="ig") for s in slots]

        polls = {"n": 0}

        async def patched_sleep(seconds):
            polls["n"] += 1
            if polls["n"] == 1:
                # A manual post finished; the guard is now free.
                assert not posted, (
                    "the batch must not have fired while the guard was held"
                )
                run_guard.release()
                return
            raise asyncio.CancelledError()

        # A manual run holds the guard across the startup sweep and first poll.
        assert run_guard.try_acquire() is True

        async def run():
            with unittest.mock.patch("asyncio.sleep", patched_sleep):
                try:
                    await scheduler.scheduler_loop(
                        qf, qmedia, hf, _FakeNotifier(), check, post)
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        assert polls["n"] >= 2, "the test must drive more than one poll"
        assert posted == [1], "the batch should fire exactly once, on a later poll"
        assert load_queue(qf) == [], "and then be pruned normally"


# ---------------------------------------------------------------------------
# #6 — the API and library fire-time contracts share one implementation
# ---------------------------------------------------------------------------

class TestFireTimeContract:
    """The two layers deliberately require different lead times. What must not
    drift is the *rule* — so both call one validator, and the difference is a
    pair of named constants (maintainer decision 2026-07-30).
    """

    def test_lead_times_are_the_only_intended_difference(self):
        assert API_MIN_LEAD == timedelta(minutes=1)
        assert QUEUE_MIN_LEAD == timedelta(0)

    def test_api_layer_requires_a_one_minute_cushion(self):
        soon = datetime.now(timezone.utc) + timedelta(seconds=30)
        with pytest.raises(ValueError, match="at least 1 minute"):
            validate_future_fire_time(soon)

    def test_queue_layer_accepts_the_same_time(self, tmp_path):
        """The looser bound is the tolerance band absorbing the elapsed time
        between the API check and this one. Tightening it would make a request
        that cleared the API by a fraction of a second fail here purely because
        the clock moved.
        """
        media, qf, qmedia = _tmp_queue_paths(tmp_path)
        (media / "A_clip.mp4").write_bytes(b"video")
        soon = datetime.now(timezone.utc) + timedelta(seconds=30)

        batch = add_batch(soon, [SlotBatch("A", "A_clip.mp4", "cap")], True,
                          qf, media, qmedia)

        assert batch.fire_time == soon

    def test_both_layers_reject_a_naive_datetime_with_one_message(self):
        naive = datetime(2099, 1, 1)
        with pytest.raises(ValueError) as api_err:
            validate_future_fire_time(naive, API_MIN_LEAD)
        with pytest.raises(ValueError) as queue_err:
            validate_future_fire_time(naive, QUEUE_MIN_LEAD)
        assert str(api_err.value) == str(queue_err.value)

    def test_queue_layer_uses_the_shared_validator(self):
        """Source-level: the point of the change is that there is no second
        hand-written copy of the rule to drift."""
        import inspect
        import backend.queue as queue_mod

        src = inspect.getsource(queue_mod)
        assert "validate_future_fire_time" in src
        for hand_rolled in ("tzinfo is None", "fire_time must be"):
            assert hand_rolled not in src, (
                f"backend/queue.py re-derives the fire_time rule ({hand_rolled}) "
                f"instead of calling the shared validator"
            )

    def test_schedule_request_still_rejects_a_past_time(self):
        with pytest.raises(ValueError):
            ScheduleRequest(slots=[], fire_time=datetime(2020, 1, 1,
                                                         tzinfo=timezone.utc))

    def test_library_rejection_is_a_422_not_a_500(self, client, monkeypatch,
                                                  tmp_media, tmp_path):
        """Rescheduling has always answered 422 for this class of failure;
        scheduling answered 500. Unreachable in practice — the model check
        fires first — but the two endpoints should not disagree about whose
        fault it is (#6)."""
        import backend.main as main_mod

        def rejecting_add_batch(*a, **kw):
            raise ValueError("fire_time must be in the future.")

        monkeypatch.setattr(main_mod, "add_batch", rejecting_add_batch)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")

        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c"}],
            "fire_time": (datetime.now(timezone.utc)
                          + timedelta(hours=1)).isoformat(),
        })

        assert resp.status_code == 422
        assert "future" in resp.json()["detail"]
