"""Regression tests for the three bundled needs-decision issues.

  #24 — empty-caption slots are visibly marked skipped in the UI before a run.
  #35 — malformed queue JSONL lines are reported via notification, not a print.
  #53 — over-length captions are rejected at the API boundary before posting.

Frontend behaviour is covered by source-level assertions: Playwright flows are
not E2E-tested (would require a real browser — forbidden), so the
`frontend_src` fixture and `tests/paths.py` stand in, matching the project's
established convention.
"""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.models import MAX_CAPTION_LENGTH, PostSlot
from backend.queue import drain_malformed_reports, load_queue
from tests.paths import PROJECT_ROOT


# ---------------------------------------------------------------------------
# #53 — caption length validated at the API boundary
# ---------------------------------------------------------------------------


class TestCaptionLimit:
    """#53: one limit (MAX_CAPTION_LENGTH), enforced on PostSlot so both
    POST /api/post and POST /api/queue reject before any browser work starts."""

    def test_constant_is_2200(self):
        """Pin the value the maintainer decided on; the per-platform split is
        deferred (see the comment in models.py), so a single number is the
        contract."""
        assert MAX_CAPTION_LENGTH == 2200

    def test_post_slot_rejects_overlength_caption(self):
        """A caption one char over the limit is rejected, naming the slot and
        the limit — the message the API caller sees."""
        too_long = "x" * (MAX_CAPTION_LENGTH + 1)
        with pytest.raises(ValidationError) as exc:
            PostSlot(slot="A", filename="A_f.mp4", caption=too_long)
        msg = str(exc.value)
        assert "Slot A" in msg
        assert str(MAX_CAPTION_LENGTH) in msg

    def test_post_slot_accepts_exactly_at_limit(self):
        """The boundary is inclusive: a caption of exactly the limit passes."""
        at_limit = "x" * MAX_CAPTION_LENGTH
        slot = PostSlot(slot="A", filename="A_f.mp4", caption=at_limit)
        assert len(slot.caption) == MAX_CAPTION_LENGTH

    def test_post_api_rejects_overlength_before_posting(self, client, tmp_media):
        """POST /api/post returns 422 for an over-length caption, and the
        posting path is never reached (validation fires at model parse time,
        before _run_post)."""
        media = tmp_media / "A_f.mp4"
        media.write_bytes(b"video")
        too_long = "x" * (MAX_CAPTION_LENGTH + 1)
        resp = client.post("/api/post", json={
            "slots": [{"slot": "A", "filename": "A_f.mp4", "caption": too_long}],
        })
        assert resp.status_code == 422
        body = resp.text
        assert "Slot A" in body
        assert str(MAX_CAPTION_LENGTH) in body

    def test_queue_api_rejects_overlength_caption(self, client, tmp_media):
        """POST /api/queue shares PostSlot, so the scheduled path is covered
        too (#53 requires both endpoints)."""
        media = tmp_media / "A_f.mp4"
        media.write_bytes(b"video")
        too_long = "x" * (MAX_CAPTION_LENGTH + 1)
        fire_time = (datetime.now(timezone.utc).isoformat())
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_f.mp4", "caption": too_long}],
            "fire_time": fire_time,
        })
        assert resp.status_code == 422

    def test_accounts_serves_caption_limit(self, client):
        """The backend is the single source of truth and serves the value to
        the frontend via GET /api/accounts (#53). This is the agreement that
        removes the frontend's divergent copy."""
        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        assert resp.json()["caption_limit"] == MAX_CAPTION_LENGTH


# ---------------------------------------------------------------------------
# #24 — empty-caption slots visibly marked skipped (frontend source-level)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frontend_src():
    return (PROJECT_ROOT / "frontend" / "index.html").read_text()


class TestEmptyCaptionSkippedUI:
    """#24: a slot with media but no caption is skipped server-side (FR-3); the
    UI must name it as skipped BEFORE posting begins, and the run proceeds."""

    def test_postall_renders_skipped_row_for_media_without_caption(self,
                                                                   frontend_src):
        """postAll() renders a status-skipped row naming the account for any
        slot that has a filename but a blank caption, before the pending rows."""
        # The skip branch keyed on "has media, no caption".
        assert "status-skipped" in frontend_src
        assert "skipped: no caption" in frontend_src
        # The skip branch is inside the slot loop in postAll, ahead of the
        # `!s.filename || !s.caption.trim()` early-continue for empty slots.
        self._assert_skip_branch_precedes_empty_continue(frontend_src)

    def test_css_has_status_skipped_style(self, frontend_src):
        """The skipped row needs a visible style distinct from pending/error."""
        assert ".status-skipped .status-icon" in frontend_src

    def test_no_confirmation_dialog_added(self, frontend_src):
        """The decision was explicit: mark visibly, do NOT block or confirm.
        A confirm() on the post path would contradict that."""
        assert "confirm(" not in frontend_src.split("function postAll")[1].split(
            "function scheduleAll")[0]

    @staticmethod
    def _assert_skip_branch_precedes_empty_continue(src: str):
        postall = src.split("function postAll")[1].split("function scheduleAll")[0]
        skip_idx = postall.find("skipped: no caption")
        # The empty-slot early-continue guard, matched literally so it is not
        # confused with the same `!s.caption.trim()` token inside the skip
        # branch's own condition.
        continue_guard = "if (!s.filename || !s.caption.trim()) continue;"
        continue_idx = postall.find(continue_guard)
        assert skip_idx != -1, "skip branch missing in postAll"
        assert continue_idx != -1, "empty-slot continue guard missing"
        assert skip_idx < continue_idx, (
            "the skipped-row branch must render before the empty-slot "
            "continue so media-without-caption slots are named, not silently "
            "dropped (#24)"
        )


# ---------------------------------------------------------------------------
# #35 — malformed queue line notification
# ---------------------------------------------------------------------------


class _FakeNotifier:
    """Records sends; never reaches the network (the tripwire blocks the real
    NtfyNotifier.send, and this does not subclass it)."""

    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


_CAPTION_SECRET = "this-is-definitely-caption-text-never-leak-me"


def _write_malformed_queue(qf):
    """One valid line followed by one malformed line carrying caption text."""
    valid = {
        "id": "valid1",
        "fire_time": "2099-01-01T09:00:00+00:00",
        "created_at": "2026-08-03T09:00:00+00:00",
        "slots": [{"slot": "A", "media_path": "A_f.mp4", "caption": "ok"}],
        "status": "pending",
        "headless": True,
        "results": None,
    }
    qf.write_text(json.dumps(valid) + "\n" + "{not valid json " + _CAPTION_SECRET + "\n")
    return 2  # the malformed line is line 2


class TestMalformedQueueNotification:
    """#35 option 2: accept the data loss, make it loud via notification."""

    def test_malformed_line_reported_once_without_content(self, tmp_path):
        """load_queue records each distinct malformed line once (dedup across
        the constant re-reads), and the report carries no raw content — a
        malformed line likely holds caption text (caption-leak trap)."""
        qf = tmp_path / "queue.jsonl"
        line_no = _write_malformed_queue(qf)

        assert len(load_queue(qf)) == 1  # only the valid batch loads
        reports = drain_malformed_reports()
        assert len(reports) == 1
        report = reports[0]
        assert report.line_no == line_no
        assert _CAPTION_SECRET not in report.error
        assert _CAPTION_SECRET not in report.fingerprint
        # fingerprint is sha1[:16] of the stripped line — held only for dedup.
        assert report.fingerprint == hashlib.sha1(
            ("{not valid json " + _CAPTION_SECRET).encode()).hexdigest()[:16]

        # Re-reading the same bad line does NOT produce a second report
        # (notification-storm trap: the queue is re-read ~4x/batch + every poll).
        load_queue(qf)
        assert drain_malformed_reports() == []

    def test_scheduler_pushes_notification_without_caption_content(self,
                                                                   tmp_path):
        """startup_sweep drains the malformed channel and sends exactly one
        push naming the line number but NOT the caption text."""
        from backend import scheduler

        qf = tmp_path / "queue.jsonl"
        line_no = _write_malformed_queue(qf)
        notifier = _FakeNotifier()

        import asyncio
        asyncio.run(scheduler.startup_sweep(qf, notifier))

        titles = [m["title"] for m in notifier.sent]
        assert any("malformed queue line" in t for t in titles), (
            "a malformed line must produce a notification (#35)")
        body = next(m["body"] for m in notifier.sent
                    if "malformed queue line" in m["title"])
        assert str(line_no) in body
        assert _CAPTION_SECRET not in body, (
            "the push must not include raw line content — caption-leak trap (#35)")
        # Exactly one push for one bad line (no storm).
        malformed_pushes = [m for m in notifier.sent
                            if "malformed queue line" in m["title"]]
        assert len(malformed_pushes) == 1

    def test_malformed_line_still_suspends_snapshot_deletion(self, tmp_path):
        """FR-17c must survive #35: an unparseable queue line already suspends
        automatic media-snapshot deletion. The notification change is purely
        additive and must not weaken that."""
        from backend.queue import (classify_snapshots, reconcile_queue_media,
                                   SNAPSHOT_AMBIGUOUS, SNAPSHOT_ORPHAN)

        qf = tmp_path / "queue.jsonl"
        _write_malformed_queue(qf)
        qmedia = tmp_path / "queue_media"
        # An orphan snapshot dir that, with a clean queue, would be
        # auto-deleted. Name matches the batch-id shape the reconciler expects.
        snap = qmedia / "deadbeefdeadbeefdeadbeefdeadbeef"
        snap.mkdir(parents=True)
        (snap / "A_f.mp4").write_bytes(b"video")

        classification = classify_snapshots(qf, None, qmedia)
        # A damaged queue means evidence is incomplete, so the orphan is
        # reclassified as ambiguous and NOT auto-deletable.
        assert any(s.classification == SNAPSHOT_AMBIGUOUS and not s.auto_deletable
                   for s in classification), (
            "a malformed queue line must keep orphan deletion suspended (FR-17c)")
        # And reconcile leaves it on disk.
        reconcile_queue_media(qf, None, qmedia)
        assert snap.exists(), (
            "the orphan snapshot must survive reconciliation while the queue "
            "has an unreadable line (FR-17c)")
