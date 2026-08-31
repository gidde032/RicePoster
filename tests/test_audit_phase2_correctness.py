"""Regression tests for the Phase 2 multi-lens audit (2026-07-27), Slice 1.

Slice 1 is the correctness slice — the findings that make the tool lie to the
maintainer or silently stop working, all of them on paths the 400-test suite
was fully green over. Reviewer: skeptical senior engineer.

Each test below names the finding it closes. All were demonstrated failing
against the pre-fix tree before the fix landed; the ones that could not fail
that way (the run.sh and frontend source assertions) are labelled as
source-level checks, per CLAUDE.md § Testing rules.
"""

import asyncio
import datetime
import inspect
import json
import unittest.mock

import pytest

from backend import main, poster_browser, scheduler
from backend.models import PostResult
from backend.notifier import Notifier, send_safe

from tests.paths import PROJECT_ROOT


class _RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title, body, priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


def _skipped_slot(slot: str) -> PostResult:
    """A slot where both platforms were skipped for a missing session.

    Mirrors what post_slot actually produces: the skip strings land in
    errors and neither post id is ever assigned, because post_media was
    never called.
    """
    return PostResult(
        slot=slot,
        errors=[
            "IG post: skipped (no session — run session_manager login instagram)",
            "TT post: skipped (no session — run session_manager login tiktok)",
        ],
    )


# --- Finding #1: the summary counted a fully-skipped slot as posted --------

def test_all_slots_skipped_reports_zero_posted():
    """The message that reaches the maintainer's phone.

    When every slot is skipped — expired sessions, or a pre-flight check that
    declines to run — an all-skipped run is the realistic shape. It used to
    push "3/3 posted, 6 skipped", claiming complete success and complete
    failure in the same sentence, with the success half first.
    """
    rec = _RecordingNotifier()
    results = [_skipped_slot(s) for s in ("A", "B", "C")]

    asyncio.run(poster_browser._notify_run_summary(rec, results))

    assert len(rec.sent) == 1
    body = rec.sent[0]["body"]
    assert body.startswith("0/3 posted"), (
        f"a run where nothing posted must not claim posts: {body!r}"
    )
    assert "6 skipped" in body


def test_a_run_that_posted_nothing_is_high_priority():
    """Nothing errored, so the old code sent this at normal priority.

    A missing session is not a failure, which is why priority was tied to
    errors only — but a run where nothing posted is worth waking the
    maintainer for regardless (decision 2026-07-27).
    """
    rec = _RecordingNotifier()

    asyncio.run(poster_browser._notify_run_summary(rec, [_skipped_slot("A")]))

    assert rec.sent[0]["priority"] == "high"
    assert rec.sent[0]["title"] == "RicePoster: nothing posted", (
        "a zero-post run titled 'run complete' contradicts its own priority"
    )


def test_a_slot_that_posted_on_one_platform_still_counts_as_posted():
    """The mixed case must not regress into over-correction.

    A slot whose Instagram session is missing but whose TikTok post landed
    genuinely did post. Excluding it would swap one wrong number for another.
    """
    rec = _RecordingNotifier()
    result = PostResult(
        slot="A",
        tt_post_id="tt_123",
        errors=["IG post: skipped (no session — run session_manager login instagram)"],
    )

    asyncio.run(poster_browser._notify_run_summary(rec, [result]))

    assert rec.sent[0]["body"] == "1/1 posted, 1 skipped (no session: slot A IG)"
    assert rec.sent[0]["priority"] == "default"


def test_a_clean_run_is_unchanged():
    """Guard against the fix leaking into the happy path."""
    rec = _RecordingNotifier()
    results = [
        PostResult(slot=s, ig_post_id="ig", tt_post_id="tt") for s in ("A", "B", "C")
    ]

    asyncio.run(poster_browser._notify_run_summary(rec, results))

    assert rec.sent[0]["body"] == "3/3 posted successfully"
    assert rec.sent[0]["priority"] == "default"


# --- Finding #2: two writers, two different clocks -------------------------

def test_manual_history_rows_are_timezone_aware_utc(tmp_path, monkeypatch):
    """main.py wrote naive local time; scheduler.py wrote UTC.

    The UI rendered both raw, so two rows in the same list sat on different
    clocks with only a trailing offset to tell them apart.
    """
    monkeypatch.setattr(main, "HISTORY_FILE", tmp_path / "history.jsonl")
    slots = [{"slot": "A", "media_path": "/tmp/a.mp4", "caption": "hi"}]
    results = [PostResult(slot="A", ig_post_id="ig", tt_post_id="tt")]

    main._append_history(slots, results, headless_used=True)

    row = json.loads((tmp_path / "history.jsonl").read_text().strip())
    parsed = datetime.datetime.fromisoformat(row["ts"])
    assert parsed.tzinfo is not None, (
        f"manual history row is timezone-naive: {row['ts']!r}"
    )
    assert parsed.utcoffset() == datetime.timedelta(0), (
        f"manual history row is not UTC: {row['ts']!r}"
    )


def test_both_history_writers_agree_on_the_clock(tmp_path, monkeypatch):
    """The two writers must produce interchangeable timestamps."""
    monkeypatch.setattr(main, "HISTORY_FILE", tmp_path / "manual.jsonl")
    main._append_history(
        [{"slot": "A", "media_path": "/tmp/a.mp4", "caption": ""}],
        [PostResult(slot="A", ig_post_id="ig")],
        headless_used=True,
    )
    manual = json.loads((tmp_path / "manual.jsonl").read_text().strip())["ts"]

    scheduled_src = inspect.getsource(scheduler._append_scheduled_history)
    assert "timezone.utc" in scheduled_src, (
        "the scheduled writer no longer stamps UTC; the two writers have "
        "diverged again"
    )
    assert datetime.datetime.fromisoformat(manual).tzinfo is not None


def test_frontend_converts_history_timestamps_for_display(frontend_src):
    """Source-level assertion — the frontend has no test harness.

    Rendering `ts` raw is what made the split clocks visible to the user. The
    helper must also tolerate the pre-fix naive rows already in the
    maintainer's history.jsonl rather than showing "Invalid Date".
    """
    html = frontend_src
    assert "function fmtHistoryTime" in html
    assert "esc(fmtHistoryTime(en.ts))" in html, (
        "the history row no longer renders through the converter"
    )
    assert "isNaN(d.getTime())" in html, (
        "fmtHistoryTime must fall back for unparseable timestamps instead of "
        "rendering 'Invalid Date' over the maintainer's existing rows"
    )


# --- Finding #3: a raising notifier could kill the scheduler ---------------

def test_send_safe_swallows_a_raising_notifier():
    class _Boom(Notifier):
        async def send(self, title, body, priority="default"):
            raise RuntimeError("notifier exploded")

    # Must not raise.
    assert asyncio.run(send_safe(_Boom(), "t", "b")) is False


def test_scheduler_never_awaits_notifier_send_bare():
    """Reads the module, not a function the conftest tripwire may patch.

    A source-level assertion against a patched attribute reads the stub and
    passes vacuously — that trap has bitten this project twice, so this
    reads `inspect.getsource(scheduler)`.
    """
    src = inspect.getsource(scheduler)
    assert "notifier.send(" not in src, (
        "scheduler.py awaits notifier.send directly again. One of those "
        "sites is inside the crash handler, where a raise escapes "
        "execute_batch and kills scheduler_loop."
    )
    assert "send_safe(" in src


def test_a_raising_notifier_does_not_escape_execute_batch(tmp_path):
    """The crash path is where this mattered most.

    scheduler.py:242 sits inside `except Exception`. A notifier raising there
    escaped execute_batch entirely, taking the scheduler loop with it while
    reporting a crash.
    """
    from backend.queue import QueuedBatch, SlotBatch, save_queue

    class _Boom(Notifier):
        async def send(self, title, body, priority="default"):
            raise RuntimeError("notifier exploded")

    qf = tmp_path / "queue.jsonl"
    batch = QueuedBatch(
        id="b1",
        fire_time=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        slots=[SlotBatch(slot="A", media_path=str(tmp_path / "a.mp4"),
                         caption="c")],
        status="pending", headless=True,
    )
    save_queue([batch], qf)

    async def exploding_post(slots, headless=True, notifier=None):
        raise RuntimeError("posting blew up")

    async def fake_check(slot, platform):
        return "live"

    # Must complete rather than propagate either exception.
    asyncio.run(scheduler.execute_batch(
        batch, qf, tmp_path / "media", tmp_path / "history.jsonl",
        _Boom(), fake_check, exploding_post,
    ))


# --- Finding #4: the loop died on a bad cycle, silently --------------------

def _run_loop_with(monkeypatch, load_side_effects, cycles, notifier):
    """Drive scheduler_loop for `cycles` polls, then cancel it.

    load_side_effects[i] is called for the i-th load_queue; index 0 is
    startup_sweep's read, which must succeed for the loop to be reached.
    """
    calls = {"load": 0, "sleep": 0}

    def fake_load(path=None):
        i = calls["load"]
        calls["load"] += 1
        effect = load_side_effects[min(i, len(load_side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    async def fake_sleep(seconds):
        calls["sleep"] += 1
        if calls["sleep"] >= cycles:
            raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler, "load_queue", fake_load)

    async def run():
        with unittest.mock.patch("asyncio.sleep", fake_sleep):
            try:
                await scheduler.scheduler_loop(notifier=notifier)
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    return calls


def test_loop_survives_a_failing_queue_read(monkeypatch):
    """A single unreadable queue used to end the loop permanently.

    The server stayed up, the queue panel kept showing pending batches, and
    nothing would ever fire them again.
    """
    rec = _RecordingNotifier()
    effects = [[], OSError("disk hiccup"), OSError("disk hiccup"), []]

    calls = _run_loop_with(monkeypatch, effects, cycles=5, notifier=rec)

    assert calls["sleep"] >= 5, (
        f"loop stopped after {calls['sleep']} cycles — a failing read must "
        f"not end it"
    )
    assert calls["load"] > 3, "loop stopped reading the queue after a failure"


def test_persistent_failure_alerts_once_after_six_cycles(monkeypatch):
    """Silence would hide a genuinely broken queue; spam would be worse.

    Six cycles at the 30s poll is a three-minute grace period (maintainer
    decision 2026-07-27), then exactly one push until the loop recovers.
    """
    rec = _RecordingNotifier()
    effects = [[], OSError("queue is corrupt")]

    _run_loop_with(monkeypatch, effects, cycles=12, notifier=rec)

    alerts = [s for s in rec.sent if "scheduler is failing" in s["title"]]
    assert len(alerts) == 1, (
        f"expected exactly one escalation, got {len(alerts)}: "
        f"{[a['title'] for a in alerts]}"
    )
    assert alerts[0]["priority"] == "high"
    assert scheduler.POLL_FAILURES_BEFORE_ALERT == 6


def test_a_recovered_cycle_resets_the_failure_count(monkeypatch):
    """Intermittent blips must not accumulate into a false alarm.

    Five failures, a success, five more failures: never six in a row, so
    nothing should reach the maintainer.
    """
    rec = _RecordingNotifier()
    effects = (
        [[]]
        + [OSError("blip")] * 5
        + [[]]
        + [OSError("blip")] * 5
        + [[]]
    )

    _run_loop_with(monkeypatch, effects, cycles=12, notifier=rec)

    alerts = [s for s in rec.sent if "scheduler is failing" in s["title"]]
    assert not alerts, (
        "an intermittent failure that keeps recovering escalated anyway"
    )


def test_cancellation_still_propagates(monkeypatch):
    """The catch-all must not swallow shutdown.

    lifespan calls task.cancel() and awaits it; a loop that caught
    CancelledError as an ordinary error would hang the server's shutdown.
    """
    src = inspect.getsource(scheduler.scheduler_loop)
    assert "except asyncio.CancelledError:" in src and "raise" in src, (
        "scheduler_loop must re-raise CancelledError so lifespan shutdown "
        "completes"
    )


def test_scheduler_task_death_is_reported(monkeypatch):
    """The backstop for a death the loop's own handler did not cover."""
    rec = _RecordingNotifier()
    monkeypatch.setattr(main, "get_notifier", lambda: rec)

    async def scenario():
        async def boom():
            raise RuntimeError("something unforeseen")

        task = asyncio.create_task(boom())
        task.add_done_callback(main._scheduler_died)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert any("scheduler stopped" in s["title"] for s in rec.sent), (
        f"a dead scheduler task notified nothing: {rec.sent}"
    )


def test_lifespan_attaches_the_death_callback():
    """Source-level: the callback is useless if it is never wired up."""
    src = inspect.getsource(main)
    assert "add_done_callback(_scheduler_died)" in src, (
        "the scheduler task no longer has a death handler attached"
    )


# --- Finding #13: --reload would restart mid-post --------------------------

def test_run_sh_does_not_enable_auto_reload():
    """Source-level assertion — run.sh is never executed by the suite.

    uvicorn --reload restarts the server on any file change, which would kill
    an in-flight scheduled post. The startup sweep catches the wreckage and
    marks the batch interrupted, but the post itself is already lost.
    """
    # Check the uvicorn invocation, not the whole file: the comment above it
    # names --reload in order to explain why it is absent, and a whole-file
    # search reports that explanation as the defect.
    run_sh = (PROJECT_ROOT / "run.sh").read_text()
    uvicorn_lines = [
        line for line in run_sh.splitlines()
        if "uvicorn" in line and not line.lstrip().startswith("#")
    ]
    assert uvicorn_lines, "run.sh no longer starts uvicorn."
    for line in uvicorn_lines:
        assert "--reload" not in line, (
            f"run.sh starts uvicorn with --reload; a file change during an "
            f"unattended scheduled post would restart the server mid-run: "
            f"{line.strip()!r}"
        )
