"""Regression tests for AUDIT-phase1-scheduling.md § Ranked fix plan, Slice A.

Every test here fails against the pre-fix scheduler and passes after. Findings
are numbered as in the audit's triage table; fixes as A1-A5.

- A1 / finding #2 (R2, CRITICAL): execute_batch never re-validated its
  in-memory batch against queue.jsonl, so a cancelled batch still posted and a
  rescheduled one fired at its old time.
- A2 / finding #1 (R1 + R2 convergent, CRITICAL): the exception path pruned the
  batch and its media snapshot without ever writing history — a crash after a
  slot posted left a live post with zero record of it.
- A3 / finding #7 (R1, MEDIUM): a clean "failed" terminal state wrote zero
  history rows and sent no notification.
- A4 / finding #6 (R3, HIGH): _update_status("running") was unasserted and
  therefore deletable without failing a test.
- A5 / finding #9 (R1, MEDIUM): no test covered a crash partway through a
  multi-slot post.

Slice B (gate integrity):
- B2 / finding #4 (R3, HIGH): the conftest tripwire claimed to block "every
  live-side entry point" but did not cover `poster.post_all` or its two
  API leaves.
- B3 / finding #5 (R3, HIGH): `run_guard._post_running` was never reset
  between tests. B1's tests live in test_gates.py, next to the gate they fix.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend import run_guard
from backend.models import PostResult
from backend.notifier import Notifier
from backend.queue import QueuedBatch, SlotBatch, _snapshot_media, load_queue, save_queue
from backend.scheduler import execute_batch

PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


class _Env:
    """A batch on disk plus the paths execute_batch needs."""

    def __init__(self, tmp_path, slot_ids=("A",), status="pending", fire_time=PAST):
        media = tmp_path / "media"
        media.mkdir(exist_ok=True)
        self.qf = tmp_path / "queue.jsonl"
        self.qmedia = tmp_path / "queue_media"
        self.hf = tmp_path / "history.jsonl"
        self.notifier = _FakeNotifier()

        raw = []
        for sid in slot_ids:
            fname = f"{sid}_clip.mp4"
            (media / fname).write_bytes(b"video")
            raw.append(SlotBatch(slot=sid, media_path=fname,
                                 caption=f"cap_{sid}"))
        snapped = _snapshot_media("batch1", raw, media, self.qmedia)
        self.batch = QueuedBatch(
            id="batch1", fire_time=fire_time, created_at=datetime.now(timezone.utc),
            slots=snapped, status=status, headless=True,
        )
        save_queue([self.batch], self.qf)
        run_guard._post_running = False

    def run(self, post_fn, check_fn=None):
        async def live(slot, platform):
            return "live"

        asyncio.run(execute_batch(self.batch, self.qf, self.qmedia, self.hf,
                                  self.notifier, check_fn or live, post_fn))

    @property
    def history(self):
        if not self.hf.exists():
            return []
        return [json.loads(l) for l in self.hf.read_text().strip().splitlines()]

    def titles(self):
        return " | ".join(s["title"] for s in self.notifier.sent)


def _recording_post_fn(calls):
    async def post(slots, headless=True, notifier=None):
        calls.append([s["slot"] for s in slots])
        return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                for s in slots]
    return post


# run_guard is reset around every test by conftest's autouse `reset_run_guard`
# fixture (audit B3 / finding #5); no local reset needed here.


# ---------------------------------------------------------------------------
# A1 — re-validate the batch against disk before posting (finding #2)
# ---------------------------------------------------------------------------

class TestRevalidateAgainstDisk:

    def test_cancelled_batch_does_not_post(self, tmp_path):
        """The CRITICAL: cancel while another batch posts → it posted anyway.

        cancel_batch removes the record from disk, but scheduler_loop's due
        list still held the object and execute_batch never looked again.
        """
        env = _Env(tmp_path)
        save_queue([], env.qf)  # what cancel_batch does

        calls = []
        env.run(_recording_post_fn(calls))

        assert calls == [], "a cancelled batch must never reach post_fn"
        assert env.history == [], "nothing posted → nothing to record"
        assert "aborted" in env.titles()

    def test_rescheduled_batch_does_not_fire_early(self, tmp_path):
        """Reschedule to tomorrow → the stale in-memory fire_time fired now."""
        env = _Env(tmp_path)
        on_disk = load_queue(env.qf)
        on_disk[0].fire_time = datetime.now(timezone.utc) + timedelta(days=1)
        save_queue(on_disk, env.qf)

        calls = []
        env.run(_recording_post_fn(calls))

        assert calls == []
        assert load_queue(env.qf)[0].status == "pending", "must stay queued"

    def test_non_pending_on_disk_does_not_post(self, tmp_path):
        """Guards reboot double-fire: a batch already running/interrupted on
        disk must not be executed from a stale due list."""
        env = _Env(tmp_path)
        on_disk = load_queue(env.qf)
        on_disk[0].status = "interrupted"
        save_queue(on_disk, env.qf)

        calls = []
        env.run(_recording_post_fn(calls))

        assert calls == []
        assert load_queue(env.qf)[0].status == "interrupted", "left untouched"

    def test_abort_releases_the_run_guard(self, tmp_path):
        """An abort must not wedge the guard and block every later batch."""
        env = _Env(tmp_path)
        save_queue([], env.qf)
        env.run(_recording_post_fn([]))
        assert run_guard._post_running is False

    def test_still_due_batch_fires(self, tmp_path):
        """Control: re-validation must not break the happy path."""
        env = _Env(tmp_path)
        calls = []
        env.run(_recording_post_fn(calls))

        assert calls == [["A"]]
        assert load_queue(env.qf) == [], "completed batch is pruned"
        assert len(env.history) == 1


# ---------------------------------------------------------------------------
# A4 — the "running" status write is the entire basis of crash detection
# ---------------------------------------------------------------------------

class TestRunningStatusIsWritten:

    def test_status_is_running_during_the_post(self, tmp_path):
        """Deleting _update_status("running") must fail a test. It is what
        startup_sweep keys on to convert a crashed batch to 'interrupted'."""
        env = _Env(tmp_path)
        observed = {}

        async def post(slots, headless=True, notifier=None):
            observed["status"] = load_queue(env.qf)[0].status
            return [PostResult(slot=s["slot"], ig_post_id="ig") for s in slots]

        env.run(post)
        assert observed["status"] == "running"


# ---------------------------------------------------------------------------
# A2 / A5 — the exception path must preserve the audit trail (finding #1, #9)
# ---------------------------------------------------------------------------

class TestCrashPreservesAuditTrail:

    @staticmethod
    async def _boom(slots, headless=True, notifier=None):
        raise RuntimeError("browser died")

    def test_crash_writes_history(self, tmp_path):
        """The CRITICAL: a crash used to prune the batch with no history at
        all, so a slot that had already posted left no record."""
        env = _Env(tmp_path)
        env.run(self._boom)

        assert len(env.history) == 1
        row = env.history[0]
        assert row["slot"] == "A"
        assert row["batch_id"] == "batch1"
        assert row["scheduled"] is True
        assert "scheduler crash" in row["errors"][0]
        assert "verify manually" in row["errors"][0]

    def test_partial_post_crash_records_every_slot(self, tmp_path):
        """A5: post_fn succeeds on A, then raises on B. Both slots must appear
        in history — A because it may be live, B because it may be half-done.
        post_all is all-or-nothing on raise, so neither result is returned and
        both are recorded as unverified."""
        env = _Env(tmp_path, slot_ids=("A", "B"))
        posted = []

        async def post_a_then_raise(slots, headless=True, notifier=None):
            for s in slots:
                if s["slot"] == "B":
                    raise RuntimeError("timeout on slot B")
                posted.append(s["slot"])
            return []

        env.run(post_a_then_raise)

        assert posted == ["A"], "slot A did post — that is the point"
        assert {r["slot"] for r in env.history} == {"A", "B"}
        assert all("scheduler crash" in r["errors"][0] for r in env.history)

    def test_crash_keeps_the_media_snapshot(self, tmp_path):
        """A2: the snapshot is the only surviving evidence of what was posted,
        so the exception path no longer deletes it."""
        env = _Env(tmp_path)
        env.run(self._boom)
        assert (env.qmedia / "batch1").exists()

    def test_crash_notification_names_the_slots(self, tmp_path):
        """The old body was just type(e).__name__ — unactionable at 6am."""
        env = _Env(tmp_path, slot_ids=("A", "B"))
        env.run(self._boom)

        bodies = " | ".join(s["body"] for s in env.notifier.sent)
        assert "slots: A, B" in bodies
        assert "RuntimeError" in bodies
        assert any(s["priority"] == "high" for s in env.notifier.sent)

    def test_crash_still_prunes_the_queue_entry(self, tmp_path):
        """Unchanged from the pre-fix behaviour: no zombie entry that would be
        re-fired on the next poll."""
        env = _Env(tmp_path)
        env.run(self._boom)
        assert load_queue(env.qf) == []


# ---------------------------------------------------------------------------
# A3 — clean "failed" terminal state must notify and record (finding #7)
# ---------------------------------------------------------------------------

class TestCleanFailureIsReported:

    def test_all_slots_failed_sends_notification(self, tmp_path):
        env = _Env(tmp_path)

        async def all_fail(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], errors=["login wall"])
                    for s in slots]

        env.run(all_fail)

        assert any("failed" in s["title"] for s in env.notifier.sent)
        assert "slots: A" in " | ".join(s["body"] for s in env.notifier.sent)
        assert len(env.history) == 1, "failed slots still get a history row"

    def test_success_sends_no_failure_notification(self, tmp_path):
        """Control: the happy path must stay quiet (poster_browser already
        sends its own run summary)."""
        env = _Env(tmp_path)
        env.run(_recording_post_fn([]))
        assert not any("failed" in s["title"] for s in env.notifier.sent)

    def test_empty_batch_still_leaves_a_history_row(self, tmp_path):
        """A batch with no slots used to write zero rows and send nothing —
        indistinguishable from the scheduler never having run."""
        env = _Env(tmp_path, slot_ids=())
        env.run(_recording_post_fn([]))

        assert len(env.history) == 1
        assert env.history[0]["slot"] == "(none)"
        assert any("failed" in s["title"] for s in env.notifier.sent)


# ---------------------------------------------------------------------------
# B2 — the tripwire must cover the API posting path too (finding #4)
# ---------------------------------------------------------------------------

class TestTripwireCoversApiPath:
    """The tripwire's docstring claimed "every live-side entry point"; the
    API path (backend/poster.py) was never stubbed. A test importing it would
    have made real Graph API / TikTok API calls with the maintainer's tokens.
    """

    def test_poster_post_all_is_blocked(self):
        from backend import poster

        with pytest.raises(AssertionError, match="live posting/caption path"):
            asyncio.run(poster.post_all([]))

    def test_instagram_post_media_is_blocked(self):
        """poster.post_slot reaches the leaves without going through
        post_all, so blocking post_all alone is not enough."""
        from backend import instagram

        with pytest.raises(AssertionError, match="live posting/caption path"):
            asyncio.run(instagram.post_media())

    def test_tiktok_post_media_is_blocked(self):
        from backend import tiktok

        with pytest.raises(AssertionError, match="live posting/caption path"):
            asyncio.run(tiktok.post_media())

    def test_tests_can_still_override_the_api_stubs(self, monkeypatch):
        """The tripwire must not break tests that deliberately observe these
        calls — e.g. test_review_fixes_mvp's token-redaction test, which
        monkeypatches poster.instagram.post_media after the autouse fixture."""
        from backend import poster

        async def fake(**kwargs):
            return "observed"

        monkeypatch.setattr(poster.instagram, "post_media", fake)
        assert asyncio.run(poster.instagram.post_media()) == "observed"


# ---------------------------------------------------------------------------
# B3 — run_guard must not leak between tests (finding #5)
# ---------------------------------------------------------------------------

class TestRunGuardIsolation:
    """Two tests reset _post_running by hand; anything that forgot left the
    flag True, and every later scheduler test silently took the "already
    running" early return — passing without executing anything.

    These two run in file order: the first leaks, the second proves the
    autouse fixture cleaned up after it.
    """

    def test_a_leaks_the_guard(self):
        run_guard._post_running = True
        assert run_guard._post_running is True

    def test_b_sees_a_clean_guard(self, tmp_path):
        assert run_guard._post_running is False, (
            "conftest's autouse reset_run_guard fixture did not reset the flag"
        )
        # And prove the consequence: a batch actually executes.
        env = _Env(tmp_path)
        calls = []
        env.run(_recording_post_fn(calls))
        assert calls == [["A"]], "leaked guard would make this a silent no-op"
