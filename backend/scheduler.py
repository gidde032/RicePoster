"""Scheduler loop: fires queued batches at their fire_time.

DESIGN-scheduling.md §5d. Runs as a background asyncio.Task in the
FastAPI lifespan. Polls every 30s — a batch scheduled for 9:00:00 may
fire at 9:00:28; that's fine.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from backend import run_guard
from backend.config import HISTORY_FILE
from backend.queue import (
    QueuedBatch, load_queue, save_queue, _delete_snapshot,
    drain_malformed_reports, QUEUE_FILE, QUEUE_MEDIA_DIR,
)
from backend.notifier import get_notifier, send_safe
from backend.models import PostResult
from backend.logging_setup import get_logger

_log = get_logger("scheduler")

POLL_INTERVAL_S = 30


async def _drain_and_notify_malformed(notifier):
    """Push one aggregated notification for any queue lines that could not be
    parsed since the last drain (#35).

    A malformed line is silently dropped from disk on the next save_queue
    rewrite — a lost scheduled batch. The loss is accepted (it is re-creatable
    in the UI in seconds); the gap was that nobody was told. This closes it.

    De-dup is at the source in queue.py (once per distinct line per process),
    so a single bad line never storms the notifier no matter how often the
    queue is re-read. All bad lines drained in one cycle are aggregated into a
    single push so a batch of N corrupt lines is one notification, not N.

    The body names only line number(s) and file. It MUST NOT include the raw
    line content: a malformed queue line very likely holds caption text, and
    the whole notification boundary is redaction-clean (FR-6, #35 caption-leak
    trap). A batch id is not recoverable for a line that failed to parse.
    """
    reports = drain_malformed_reports()
    if not reports:
        return
    line_nos = ", ".join(str(r.line_no) for r in reports)
    await send_safe(
        notifier,
        title="RicePoster: malformed queue line(s) skipped",
        body=(f"{len(reports)} queue line(s) could not be parsed and were skipped "
              f"(line number(s): {line_nos} in queue.jsonl). They will be lost "
              f"when the queue is next rewritten — re-create the affected "
              f"batch(es) in the UI."),
        priority="high",
    )

# Consecutive failed poll cycles before the loop escalates to a push. At the
# 30s poll interval this is a three-minute grace period: long enough that a
# momentary unreadable queue file heals itself in silence, short enough that a
# genuinely broken queue reaches the maintainer while the day's batches are
# still pending (Phase 2 audit, finding #4; interval set by the maintainer).
POLL_FAILURES_BEFORE_ALERT = 6


async def startup_sweep(queue_file: Path | None = None,
                        notifier=None):
    """Run once at server boot before the first poll.

    1. Stale 'running' → 'interrupted' + notification (never re-executed).
    2. Returns overdue pending batches for catch-up (caller executes them).
    """
    qf = queue_file or QUEUE_FILE
    if notifier is None:
        notifier = get_notifier()
    batches = load_queue(qf)
    # Report any malformed lines found at boot before the first poll fires
    # (#35).
    await _drain_and_notify_malformed(notifier)
    changed = False
    overdue = []
    interrupted = []

    for b in batches:
        if b.status == "running":
            b.status = "interrupted"
            changed = True
            interrupted.append(b)
        elif b.status == "pending" and b.fire_time <= datetime.now(timezone.utc):
            overdue.append(b)

    if changed:
        save_queue(batches, qf)

    for b in interrupted:
        slots_desc = ", ".join(s.slot for s in b.slots)
        await send_safe(
            notifier,
            title="RicePoster: batch interrupted",
            body=f"Batch {b.id[:8]} (slots: {slots_desc}) was interrupted by a "
                 f"server shutdown. Check accounts for partial posts, then dismiss.",
            priority="high",
        )

    overdue.sort(key=lambda b: b.fire_time)
    return overdue


async def execute_batch(batch: QueuedBatch,
                        queue_file: Path | None = None,
                        queue_media_dir: Path | None = None,
                        history_file: Path | None = None,
                        notifier=None,
                        check_session_fn=None,
                        post_all_fn=None):
    """Execute a single queued batch per DESIGN §5d flow."""
    from backend.session_manager import check_session as _check_session
    from backend.poster_browser import post_all as _post_all

    qf = queue_file or QUEUE_FILE
    qmd = queue_media_dir or QUEUE_MEDIA_DIR
    hf = history_file or HISTORY_FILE
    if notifier is None:
        notifier = get_notifier()
    check_fn = check_session_fn or _check_session
    post_fn = post_all_fn or _post_all

    if not run_guard.try_acquire():
        return

    # Accumulator lives outside the try so the exception path can record
    # whatever landed before the crash (fix: audit A2 / finding #1).
    all_results: list[PostResult] = []

    try:
        # Re-validate against disk before posting (fix: audit A1 / finding #2).
        # The in-memory batch came from a due list that may be minutes stale —
        # a posting run takes minutes and the queue is mutable throughout.
        # Without this, a cancelled batch still posts and a rescheduled one
        # fires at its old time.
        abort_reason = _abort_reason(batch.id, qf)
        if abort_reason is None and not _update_status(batch.id, "running", qf):
            # Cannot happen after a passing re-validation; kept as a guard so a
            # vanished record can never fall through to post_fn again.
            abort_reason = "disappeared from the queue between check and update"
        if abort_reason is not None:
            _log.warning(f"[scheduler] aborting batch {batch.id[:8]}: {abort_reason}")
            await send_safe(
                notifier,
                title="RicePoster: scheduled batch aborted",
                body=f"Batch {batch.id[:8]} was not posted — {abort_reason}.",
            )
            return

        batch = _reload_batch(batch.id, qf) or batch

        # Pre-flight health check — per-platform, not per-slot (fix: review #1).
        # Only skip the entire slot when ALL platforms are expired/no_session.
        # A single live platform is worth attempting.
        proceeding_slots = []
        for s in batch.slots:
            platform_skips: dict[str, str] = {}
            for platform in ["instagram", "tiktok"]:
                status = await check_fn(s.slot, platform)
                if status in ("expired", "no_session"):
                    platform_skips[platform] = status
                    await send_safe(
                        notifier,
                        title=f"RicePoster: slot {s.slot} {platform} skipped",
                        body=f"Pre-flight check: {status}",
                        priority="high",
                    )
            if len(platform_skips) == 2:
                all_results.append(PostResult(
                    slot=s.slot,
                    errors=[f"pre-flight: {p} {st}"
                            for p, st in platform_skips.items()],
                ))
            else:
                proceeding_slots.append({
                    "slot": s.slot,
                    "media_path": Path(s.media_path),
                    "caption": s.caption,
                    "media_type": "video",
                    # Carry the per-platform verdict through to post_slot
                    # (review 2026-07-26, finding #2). Without it post_slot
                    # falls back to session_exists(), which cannot tell an
                    # expired profile from a live one, and posts to a platform
                    # the maintainer was just told had been skipped.
                    "skip_platforms": set(platform_skips),
                })

        if proceeding_slots:
            all_results.extend(await post_fn(
                proceeding_slots,
                headless=batch.headless,
                notifier=notifier,
            ))

        if not all_results or all(not r.success for r in all_results):
            final_status = "failed"
        elif all(r.success for r in all_results):
            final_status = "done"
        else:
            final_status = "partial"

        # A batch must never complete without leaving a history row, even when
        # it had nothing to execute (fix: audit A3 / finding #7).
        if not all_results:
            all_results = [PostResult(
                slot="(none)",
                errors=["batch executed with no slots — nothing to post"],
            )]

        batch.status = final_status
        batch.results = [r.model_dump() for r in all_results]

        # Only discard the queue record once the run is durably recorded
        # elsewhere (review 2026-07-26, finding #3). Retaining it is safe:
        # startup_sweep and due_batches only ever pick up "pending" batches,
        # and this one now carries a terminal status.
        if _append_scheduled_history(hf, batch, all_results):
            _prune_batch(batch.id, qf)
            # A batch only loses its media once every slot posted successfully
            # (maintainer decision 2026-07-27). "partial" and "failed" mean at
            # least one slot did not go out, and its media was the only copy
            # under our control — history records *that* it failed, never the
            # file itself. Deleting it forced a re-upload to retry, and on a
            # partial the maintainer might not even notice until the snapshot
            # was already gone. Same forensic rationale as the crash path
            # below, which has always retained its snapshot.
            if final_status == "done":
                _delete_snapshot(batch.id, qmd)
            else:
                _log.info(f"[scheduler] batch {batch.id[:8]} finished "
                      f"'{final_status}': media snapshot retained at "
                      f"{qmd / batch.id} for retry/inspection")
        else:
            _log.warning(f"[scheduler] batch {batch.id[:8]} kept on disk: history "
                  f"write failed, queue record is the only evidence")
            # Whatever made the history write fail — a full or read-only disk —
            # is just as likely to make this status write fail. Letting it
            # escape would drop a *successful* run into the crash handler
            # below, which forces status "failed" and tells the maintainer the
            # slots may not have posted. They did. Acting on that could mean a
            # duplicate live post, so the failure is contained here and the run
            # keeps its true outcome (review 2026-07-30).
            try:
                _update_status(batch.id, final_status, qf)
            except Exception as status_err:
                _log.error(f"[scheduler] batch {batch.id[:8]}: could not record "
                      f"status '{final_status}' either "
                      f"({type(status_err).__name__}); the queue entry stays "
                      f"'running' and the next startup sweep will flag it")
            await _notify_history_write_failed(
                notifier, batch, f"finished '{final_status}'")

        if final_status == "failed":
            await send_safe(
                notifier,
                title="RicePoster: scheduled batch failed",
                body=f"Batch {batch.id[:8]} ({_slots_desc(batch)}) completed "
                     f"with no successful posts.",
                priority="high",
            )

    except Exception as e:
        _log.error(f"[scheduler] execute_batch failed: {e}")
        # Write the audit trail BEFORE pruning (fix: audit A2 / finding #1).
        # A crash after slot A posted used to leave a live post with zero
        # record it happened. Slots with no result may or may not have posted.
        recorded = {r.slot for r in all_results}
        for s in batch.slots:
            if s.slot not in recorded:
                all_results.append(PostResult(
                    slot=s.slot,
                    errors=[f"scheduler crash: {type(e).__name__}: {e} — slot "
                            f"may or may not have posted; verify manually"],
                ))
        batch.status = "failed"
        batch.results = [r.model_dump() for r in all_results]
        # A failed history write on the crash path is the worst case in the
        # system: the slots may have posted, and pruning would erase the last
        # trace (review 2026-07-26, finding #3). Keep the record as "running"
        # so startup_sweep converts it to "interrupted" and notifies.
        if _append_scheduled_history(hf, batch, all_results):
            _prune_batch(batch.id, qf)
        else:
            _log.error(f"[scheduler] batch {batch.id[:8]} kept on disk: crashed AND "
                  f"history write failed — startup_sweep will flag it")
            await _notify_history_write_failed(
                notifier, batch, "crashed mid-run")
        # The media snapshot is deliberately NOT deleted here: on a crash it is
        # the only surviving evidence of what was being posted. Orphan dirs are
        # cleaned up by the reconciliation pass (audit C1).
        await send_safe(
            notifier,
            title="RicePoster: scheduled batch failed",
            body=f"Batch {batch.id[:8]} ({_slots_desc(batch)}) crashed: "
                 f"{type(e).__name__}. Slots may have posted — verify accounts.",
            priority="high",
        )
    finally:
        run_guard.release()


async def _notify_history_write_failed(notifier, batch: QueuedBatch,
                                       outcome: str):
    """Tell the maintainer that a batch left no history row (#5).

    Unattended runs have no other audience. The console line next to each call
    site is the only existing record, and the retained queue entry carries a
    status the queue panel does not surface — so without this push the fact
    that a batch ran can be invisible until someone reads the server log.

    Deliberately a *separate* push from the crash notification on that path:
    "the batch crashed, verify the accounts" and "the run was not recorded, the
    queue file is the only evidence" are different facts and prompt different
    actions. Routed through send_safe, so a broken notifier cannot escalate a
    failed history write into a dead scheduler.

    `outcome` is a phrase, not a status value. The two call sites genuinely
    know different things: the terminal path knows the final status, while the
    crash path deliberately leaves the record `running` for the next startup
    sweep to reclassify. Naming a terminal status there would assert a state
    the system does not hold, and dismissal is not even available until that
    sweep runs (review 2026-07-30).
    """
    await send_safe(
        notifier,
        title="RicePoster: scheduled run not recorded",
        body=f"Batch {batch.id[:8]} ({_slots_desc(batch)}) {outcome} but its "
             f"history could not be written. The queue entry and media "
             f"snapshot are the only evidence — check the accounts and the "
             f"server log.",
        priority="high",
    )


def _slots_desc(batch: QueuedBatch) -> str:
    return "slots: " + (", ".join(s.slot for s in batch.slots) or "none")


def _reload_batch(batch_id: str, queue_file: Path | None = None) -> QueuedBatch | None:
    """Read the current on-disk record for a batch, or None if it is gone."""
    for b in load_queue(queue_file or QUEUE_FILE):
        if b.id == batch_id:
            return b
    return None


def _abort_reason(batch_id: str, queue_file: Path | None = None) -> str | None:
    """None if the batch is still safe to fire, else a human-readable reason.

    Guards the three reachable ways an in-memory batch goes stale: cancelled
    (gone from disk), already running/interrupted, or rescheduled later.
    """
    fresh = _reload_batch(batch_id, queue_file)
    if fresh is None:
        return "it is no longer in the queue (cancelled)"
    if fresh.status != "pending":
        return f"its status on disk is '{fresh.status}', not 'pending'"
    if fresh.fire_time > datetime.now(timezone.utc):
        return (f"it was rescheduled to {fresh.fire_time.isoformat()} "
                f"and is not due yet")
    return None


def _update_status(batch_id: str, status: str,
                   queue_file: Path | None = None) -> bool:
    """Set a batch's status on disk. Returns False if no such batch exists —
    callers must not treat a no-op write as success (fix: audit A1)."""
    qf = queue_file or QUEUE_FILE
    batches = load_queue(qf)
    for b in batches:
        if b.id == batch_id:
            b.status = status
            save_queue(batches, qf)
            return True
    # No match means nothing changed, so don't rewrite (review 2026-07-26,
    # finding #10). save_queue is an atomic full-file rewrite; doing one for
    # a no-op put every other queue entry through a needless write on a path
    # reached precisely when the on-disk state is already confusing.
    return False


def _prune_batch(batch_id: str, queue_file: Path | None = None):
    qf = queue_file or QUEUE_FILE
    batches = load_queue(qf)
    batches = [b for b in batches if b.id != batch_id]
    save_queue(batches, qf)


def _append_scheduled_history(history_file: Path, batch: QueuedBatch,
                              results: list[PostResult]) -> bool:
    """Append one history row per result. Returns False if the write failed.

    Callers must not prune the queue entry on a False return (review
    2026-07-26, finding #3): if the history write failed, the on-disk queue
    record is the only surviving evidence that the batch ran at all.
    """
    try:
        with open(history_file, "a") as f:
            for r in results:
                slot_info = next((s for s in batch.slots if s.slot == r.slot), None)
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "slot": r.slot,
                    "file": Path(slot_info.media_path).name if slot_info else "",
                    "caption": slot_info.caption if slot_info else "",
                    "post_mode": "browser",
                    "headless": batch.headless,
                    "ig_post_id": r.ig_post_id,
                    "tt_post_id": r.tt_post_id,
                    "errors": r.errors,
                    "scheduled": True,
                    "batch_id": batch.id,
                }) + "\n")
        return True
    except Exception as e:
        _log.warning(f"[scheduler] Warning: failed to record scheduled history: {e}")
        return False


async def scheduler_loop(queue_file: Path | None = None,
                         queue_media_dir: Path | None = None,
                         history_file: Path | None = None,
                         notifier=None,
                         check_session_fn=None,
                         post_all_fn=None):
    """Main scheduler loop — started as an asyncio.Task in the FastAPI lifespan.

    Every cycle is wrapped: an exception escaping the body would end the
    `while True` and permanently stop the loop, leaving the server up, the
    queue panel still showing pending batches, and nothing to ever fire them
    (Phase 2 audit, finding #4). Reading `queue.jsonl` is the realistic
    failure — a read racing an atomic rewrite, a disk or permissions hiccup —
    and it is almost always momentary, so the right response is to log it and
    take the next cycle rather than to die.

    Retrying cannot double-post. `execute_batch` re-reads the batch from disk
    and refuses to proceed unless it is still `pending`, flipping it to
    `running` before it posts anything; a batch that already fired is no
    longer pending and a later cycle walks past it.

    Silence would hide a genuinely broken queue, so consecutive failures
    escalate once at POLL_FAILURES_BEFORE_ALERT and then stay quiet until the
    loop recovers — one alert, not one every poll.
    """
    qf = queue_file or QUEUE_FILE
    n = notifier or get_notifier()

    overdue = await startup_sweep(qf, n)
    for batch in overdue:
        await execute_batch(batch, qf, queue_media_dir, history_file, n,
                            check_session_fn, post_all_fn)

    consecutive_failures = 0
    alerted = False

    while True:
        try:
            now = datetime.now(timezone.utc)
            batches = load_queue(qf)
            # Drain any malformed-line reports recorded by the read above (and
            # by incidental reads inside execute_batch on the previous cycle).
            await _drain_and_notify_malformed(n)
            due = sorted(
                [b for b in batches if b.status == "pending" and b.fire_time <= now],
                key=lambda b: b.fire_time,
            )
            for batch in due:
                await execute_batch(batch, qf, queue_media_dir, history_file, n,
                                    check_session_fn, post_all_fn)
        except asyncio.CancelledError:
            # Shutdown, not a fault. Must propagate or the lifespan's
            # task.cancel() would hang waiting for a loop that swallowed it.
            raise
        except Exception as e:
            consecutive_failures += 1
            _log.error(f"[scheduler] poll cycle failed ({consecutive_failures} in a "
                  f"row): {type(e).__name__}: {e}")
            if consecutive_failures >= POLL_FAILURES_BEFORE_ALERT and not alerted:
                alerted = True
                await send_safe(
                    n,
                    title="RicePoster: scheduler is failing",
                    body=f"The scheduler has failed {consecutive_failures} poll "
                         f"cycles in a row ({type(e).__name__}). Scheduled "
                         f"batches are not firing — check the server log.",
                    priority="high",
                )
        else:
            if consecutive_failures:
                _log.info(f"[scheduler] recovered after {consecutive_failures} "
                      f"failed cycle(s)")
            consecutive_failures = 0
            alerted = False
        await asyncio.sleep(POLL_INTERVAL_S)
