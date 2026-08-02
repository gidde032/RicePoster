"""Batch 5 — queue media reconciliation (#4) and queue-row symmetry (#46).

This batch's failure mode is destructive rather than silent: a wrong
classification deletes the only surviving copy of media for a batch that partly
posted, and history records only *that* a slot failed, never the file. So the
tests below are weighted towards proving what reconciliation **keeps**, not
what it removes.

Every test builds its own queue, history, and media directories under
`tmp_path`. Nothing here reads or writes the real `queue_media/`, which holds
forensic evidence from a 2026-07-27 batch that did not fully succeed.

Issue coverage:

- #4 — classification, orphan-only auto-deletion, the explicit cleanup
  endpoints, and snapshot filename disambiguation.
- #46 — unknown top-level queue-row fields are dropped rather than discarding
  the batch; genuinely unparseable rows are still skipped loudly.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import backend.queue as queue_mod
from backend import main
from backend.queue import (
    QueuedBatch, SNAPSHOT_ACTIVE, SNAPSHOT_AMBIGUOUS, SNAPSHOT_FOREIGN,
    SNAPSHOT_ORPHAN, SNAPSHOT_RETAINED, SlotBatch, _snapshot_media,
    classify_snapshots, load_queue, reconcile_queue_media, remove_snapshot,
    save_queue,
)

BATCH_A = "a" * 32
BATCH_B = "b" * 32

# The real retained snapshot's history row, reduced to the fields
# classification reads. Copied from history.jsonl line 315 (batch
# ab9ab58400e44693bd4681303355c5be, 2026-07-27): Instagram posted, TikTok was
# ruled out by the pre-flight check. It reads as a success by ig_post_id alone,
# which is exactly why the rule keys on `errors`.
REAL_PARTIAL_ROW = {
    "ts": "2026-07-28T03:16:46+00:00",
    "slot": "A",
    "file": "A_clip.mp4",
    "ig_post_id": "ig_post_ok_A",
    "tt_post_id": "",
    "errors": ["TT post: skipped (pre-flight ruled the session out)"],
    "scheduled": True,
    "batch_id": "ab9ab58400e44693bd4681303355c5be",
}


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    """Disposable queue file, history file, and queue_media directory."""
    qmedia = tmp_path / "queue_media"
    qmedia.mkdir()
    return {
        "queue_file": tmp_path / "queue.jsonl",
        "history_file": tmp_path / "history.jsonl",
        "queue_media_dir": qmedia,
        "tmp_path": tmp_path,
    }


def _snapshot_dir(env, batch_id, name="A_clip.mp4", age_s=None):
    d = env["queue_media_dir"] / batch_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"video-bytes")
    if age_s is not None:
        old = datetime.now(timezone.utc).timestamp() - age_s
        for p in (f, d):
            import os
            os.utime(p, (old, old))
    return d


def _queue_row(env, batch_id, status="pending"):
    now = datetime.now(timezone.utc)
    batch = QueuedBatch(
        id=batch_id,
        fire_time=now + timedelta(hours=1),
        created_at=now,
        slots=[SlotBatch(slot="A", media_path="A_clip.mp4", caption="c")],
        status=status,
        headless=False,
    )
    existing = load_queue(env["queue_file"])
    save_queue(existing + [batch], env["queue_file"])
    return batch


def _history_rows(env, *rows):
    with open(env["history_file"], "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _classify(env, **kw):
    return {s.batch_id: s for s in classify_snapshots(
        queue_file=env["queue_file"],
        history_file=env["history_file"],
        queue_media_dir=env["queue_media_dir"],
        **kw,
    )}


def _reconcile(env, **kw):
    return reconcile_queue_media(
        queue_file=env["queue_file"],
        history_file=env["history_file"],
        queue_media_dir=env["queue_media_dir"],
        **kw,
    )


# --- classification ---------------------------------------------------------


def test_media_for_a_queued_batch_is_active(env):
    _snapshot_dir(env, BATCH_A)
    _queue_row(env, BATCH_A, status="pending")
    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_ACTIVE
    assert not snap.auto_deletable and not snap.deletable


def test_media_for_an_interrupted_batch_is_active(env):
    """An interrupted batch keeps its queue entry until the maintainer
    dismisses it, so its media is still spoken for."""
    _snapshot_dir(env, BATCH_A)
    _queue_row(env, BATCH_A, status="interrupted")
    assert _classify(env)[BATCH_A].classification == SNAPSHOT_ACTIVE


def test_partial_batch_media_is_retained_not_orphaned(env):
    """The load-bearing case. One slot posted, one did not; the batch is gone
    from the queue because history recorded it. The media must survive."""
    real_id = REAL_PARTIAL_ROW["batch_id"]
    _snapshot_dir(env, real_id)
    _history_rows(env, REAL_PARTIAL_ROW)

    snap = _classify(env)[real_id]
    assert snap.classification == SNAPSHOT_RETAINED
    assert not snap.auto_deletable, "a partial batch's media is never auto-deleted"
    assert snap.deletable, "the maintainer can still remove it explicitly"
    assert "unsuccessful" in snap.reason


def test_failed_batch_media_is_retained(env):
    _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A",
                        "errors": ["IG post: failed"]})
    assert _classify(env)[BATCH_A].classification == SNAPSHOT_RETAINED


def test_history_row_without_an_errors_field_counts_as_unsuccessful(env):
    """Absence of evidence is not evidence of success. A row that does not
    carry a well-formed `errors` list does not prove the slot posted, and only
    proof justifies deleting the media."""
    _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A"})
    assert _classify(env)[BATCH_A].classification == SNAPSHOT_RETAINED

    _snapshot_dir(env, BATCH_B)
    _history_rows(env, {"batch_id": BATCH_B, "slot": "A", "errors": "nope"})
    assert _classify(env)[BATCH_B].classification == SNAPSHOT_RETAINED


def test_fully_successful_batch_media_is_an_orphan(env):
    """execute_batch deletes the snapshot itself on a clean run, so one that
    survives means a crash between the history write and the delete."""
    _snapshot_dir(env, BATCH_A)
    _history_rows(env,
                  {"batch_id": BATCH_A, "slot": "A", "errors": []},
                  {"batch_id": BATCH_A, "slot": "B", "errors": []})
    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_ORPHAN
    assert snap.auto_deletable


def test_one_failed_slot_among_successes_still_retains(env):
    _snapshot_dir(env, BATCH_A)
    _history_rows(env,
                  {"batch_id": BATCH_A, "slot": "A", "errors": []},
                  {"batch_id": BATCH_A, "slot": "B", "errors": ["boom"]})
    assert _classify(env)[BATCH_A].classification == SNAPSHOT_RETAINED


def test_unreferenced_old_snapshot_is_an_orphan(env):
    """BE-18's crash window: cancel_batch saves the queue and then deletes the
    snapshot, so a crash between the two leaves media with nothing to
    reconcile against."""
    _snapshot_dir(env, BATCH_A, age_s=7200)
    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_ORPHAN
    assert snap.auto_deletable


def test_unreferenced_fresh_snapshot_is_ambiguous(env):
    """A snapshot being written right now looks identical to an orphan."""
    _snapshot_dir(env, BATCH_A, age_s=60)
    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_AMBIGUOUS
    assert not snap.auto_deletable


def test_age_is_measured_from_the_directory_not_only_its_files(env):
    """`shutil.copy2` preserves the *source* file's timestamps, so a snapshot
    copied a moment ago from media uploaded last week carries week-old file
    mtimes. Keying the age floor on file mtimes alone would classify a batch
    still being created as a long-dead orphan and delete its media.

    Pins the surviving mutation found in cold review: reducing
    `_dir_size_and_mtime` to a file-mtime-only version left all tests green."""
    import os
    d = env["queue_media_dir"] / BATCH_A
    d.mkdir(parents=True)
    f = d / "A_clip.mp4"
    f.write_bytes(b"video-bytes")
    old = datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600
    os.utime(f, (old, old))          # file looks ancient; directory is fresh

    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_AMBIGUOUS
    assert not snap.auto_deletable


def test_nested_files_are_counted_and_age_the_snapshot(env):
    """Deletion is recursive, so reporting must be too — a subdirectory that
    `rmtree` will remove cannot show as '0 files'. Its mtime counts as well."""
    import os
    d = env["queue_media_dir"] / BATCH_A
    (d / "nested").mkdir(parents=True)
    (d / "nested" / "clip.mp4").write_bytes(b"12345")
    old = datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600
    os.utime(d, (old, old))

    snap = _classify(env)[BATCH_A]
    assert snap.files == ["nested/clip.mp4"]
    assert snap.size_bytes == 5
    assert snap.classification == SNAPSHOT_AMBIGUOUS, (
        "a freshly written nested file must still age the snapshot"
    )


def test_foreign_entries_are_never_touched(env):
    (env["queue_media_dir"] / "not-a-batch-id").mkdir()
    (env["queue_media_dir"] / "stray.mp4").write_bytes(b"x")
    results = _classify(env)
    for name in ("not-a-batch-id", "stray.mp4"):
        assert results[name].classification == SNAPSHOT_FOREIGN
        assert not results[name].auto_deletable
        assert not results[name].deletable


def test_malformed_history_lines_do_not_authorise_deletion(env):
    """An unreadable history line could be the failed slot that should have
    kept this snapshot. Evidence with a hole in it cannot clear anything for
    deletion, so the pass keeps and reports instead."""
    _snapshot_dir(env, BATCH_A, age_s=7200)
    env["history_file"].write_text("{not json\n\n[]\n")
    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_AMBIGUOUS
    assert not snap.auto_deletable
    assert "unreadable history line" in snap.reason


def test_a_malformed_queue_row_does_not_orphan_its_own_media(env):
    """Convergent cold-review finding, 2026-07-30. `load_queue` skips a row it
    cannot parse, so a scheduled — possibly running or interrupted — batch
    disappears from the queue while its media stays on disk. That media then
    looked exactly like an orphan, and the next server start deleted it: the
    corrupt row was reported, and then the evidence needed to repair it was
    destroyed."""
    d = _snapshot_dir(env, BATCH_A, age_s=7200)
    env["queue_file"].write_text(
        '{"id": "' + BATCH_A + '", "fire_time": "2026-08-01T10:00:00+00:00", '
        '"created_at": "2026-07-30T10:00:00+00:00", "slots": [{"slot": "A", '
        '"media_pa\n')

    snap = _classify(env)[BATCH_A]
    assert snap.classification == SNAPSHOT_AMBIGUOUS
    assert not snap.auto_deletable
    assert "unreadable queue line" in snap.reason

    _reconcile(env)
    assert d.exists(), "media must survive a queue file we cannot fully read"


def test_a_damaged_queue_file_suspends_all_auto_deletion(env):
    """Not just the damaged row's own media: one unreadable line means the
    queue no longer accounts for what it claims to, so no directory can be
    proven unreferenced this pass."""
    unrelated = _snapshot_dir(env, BATCH_B, age_s=7200)
    env["queue_file"].write_text("{truncated\n")
    _reconcile(env)
    assert unrelated.exists()


def test_auto_deletion_resumes_once_the_queue_file_is_repaired(env):
    """The suspension is a function of the file's current state, not a latch."""
    d = _snapshot_dir(env, BATCH_B, age_s=7200)
    env["queue_file"].write_text("{truncated\n")
    _reconcile(env)
    assert d.exists()

    env["queue_file"].write_text("")
    _reconcile(env)
    assert not d.exists(), "a repaired queue file restores normal cleanup"


def test_missing_queue_media_dir_classifies_as_empty(tmp_path):
    assert classify_snapshots(queue_file=tmp_path / "q.jsonl",
                              history_file=tmp_path / "h.jsonl",
                              queue_media_dir=tmp_path / "absent") == []


# --- reconciliation (the destructive path) ----------------------------------


def test_reconciliation_deletes_orphans_only(env):
    """Fail-before-fix regression for the whole batch. Five directories, one
    deletable."""
    orphan = _snapshot_dir(env, "0" * 32, age_s=7200)
    fresh = _snapshot_dir(env, "1" * 32, age_s=60)
    active = _snapshot_dir(env, "2" * 32)
    _queue_row(env, "2" * 32)
    partial = _snapshot_dir(env, "3" * 32)
    _history_rows(env, {"batch_id": "3" * 32, "slot": "A", "errors": ["x"]})
    foreign = env["queue_media_dir"] / "maintainer-scratch"
    foreign.mkdir()

    _reconcile(env)

    assert not orphan.exists(), "a provable orphan should be removed"
    for kept in (fresh, active, partial, foreign):
        assert kept.exists(), f"{kept.name} must survive reconciliation"


def test_reconciliation_preserves_media_files_not_just_directories(env):
    """A partial batch's file is the artefact that matters, not its folder."""
    d = _snapshot_dir(env, BATCH_A, name="A_clip.mp4")
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A", "errors": ["x"]})
    _reconcile(env)
    assert (d / "A_clip.mp4").read_bytes() == b"video-bytes"


def test_reconciliation_is_idempotent_across_restarts(env):
    """Classification is recomputed from the queue and history every time, so
    there is no cleanup state to go stale — running it twice changes nothing
    the first run did not already settle."""
    _snapshot_dir(env, BATCH_A, age_s=7200)
    partial = _snapshot_dir(env, BATCH_B)
    _history_rows(env, {"batch_id": BATCH_B, "slot": "A", "errors": ["x"]})

    first = {s.batch_id: s.classification for s in _reconcile(env)}
    second = {s.batch_id: s.classification for s in _reconcile(env)}
    assert first[BATCH_B] == second[BATCH_B] == SNAPSHOT_RETAINED
    assert BATCH_A not in second, "the orphan is gone after the first pass"
    assert partial.exists()


def test_reconciliation_reports_what_it_kept(env, capsys):
    _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A", "errors": ["x"]})
    _reconcile(env)
    out = capsys.readouterr().out
    assert BATCH_A[:8] in out and "keeping snapshot" in out


# --- explicit maintainer cleanup --------------------------------------------


def test_remove_snapshot_deletes_retained_evidence_on_request(env):
    d = _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A", "errors": ["x"]})
    removed = remove_snapshot(BATCH_A, queue_file=env["queue_file"],
                              history_file=env["history_file"],
                              queue_media_dir=env["queue_media_dir"])
    assert removed.classification == SNAPSHOT_RETAINED
    assert not d.exists()


def test_remove_snapshot_refuses_an_active_batch(env):
    d = _snapshot_dir(env, BATCH_A)
    _queue_row(env, BATCH_A)
    with pytest.raises(PermissionError):
        remove_snapshot(BATCH_A, queue_file=env["queue_file"],
                        history_file=env["history_file"],
                        queue_media_dir=env["queue_media_dir"])
    assert d.exists()


def test_remove_snapshot_refuses_a_foreign_directory(env):
    d = env["queue_media_dir"] / "maintainer-scratch"
    d.mkdir()
    with pytest.raises(PermissionError):
        remove_snapshot("maintainer-scratch", queue_file=env["queue_file"],
                        history_file=env["history_file"],
                        queue_media_dir=env["queue_media_dir"])
    assert d.exists()


def test_remove_snapshot_rejects_unknown_and_traversing_ids(env):
    """Deletion targets are matched against directories actually found under
    queue_media/, so a crafted id cannot escape the tree."""
    outside = env["tmp_path"] / "precious"
    outside.mkdir()
    for bad in ("nosuchbatch", "../precious", "../../"):
        with pytest.raises(LookupError):
            remove_snapshot(bad, queue_file=env["queue_file"],
                            history_file=env["history_file"],
                            queue_media_dir=env["queue_media_dir"])
    assert outside.exists()


# --- API --------------------------------------------------------------------


@pytest.fixture
def api(env, monkeypatch):
    monkeypatch.setattr(queue_mod, "QUEUE_FILE", env["queue_file"])
    monkeypatch.setattr(queue_mod, "HISTORY_FILE", env["history_file"])
    monkeypatch.setattr(queue_mod, "QUEUE_MEDIA_DIR", env["queue_media_dir"])
    with TestClient(main.app) as client:
        yield client


def test_get_queue_media_lists_classification_and_reason(env, api):
    _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A", "errors": ["x"]})
    resp = api.get("/api/queue/media")
    assert resp.status_code == 200
    snap = resp.json()["snapshots"][0]
    assert snap["batch_id"] == BATCH_A
    assert snap["classification"] == SNAPSHOT_RETAINED
    assert snap["deletable"] is True
    assert snap["files"] == ["A_clip.mp4"] and snap["size_bytes"] > 0
    assert snap["reason"]


def test_delete_queue_media_removes_retained_snapshot(env, api):
    d = _snapshot_dir(env, BATCH_A)
    _history_rows(env, {"batch_id": BATCH_A, "slot": "A", "errors": ["x"]})
    resp = api.delete(f"/api/queue/media/{BATCH_A}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert not d.exists()


def test_delete_queue_media_conflicts_on_an_active_batch(env, api):
    d = _snapshot_dir(env, BATCH_A)
    _queue_row(env, BATCH_A)
    resp = api.delete(f"/api/queue/media/{BATCH_A}")
    assert resp.status_code == 409
    assert d.exists()


def test_delete_queue_media_404s_for_an_unknown_batch(env, api):
    assert api.delete(f"/api/queue/media/{BATCH_A}").status_code == 404


def test_delete_queue_media_conflicts_on_a_foreign_directory(env, api):
    """SPEC's 409 clause covers an unrecognised directory as well as an active
    batch; only the latter had API-level coverage."""
    d = env["queue_media_dir"] / "maintainer-scratch"
    d.mkdir()
    assert api.delete("/api/queue/media/maintainer-scratch").status_code == 409
    assert d.exists()


def test_queue_media_route_is_not_shadowed_by_the_batch_id_route(env, api):
    """`/api/queue/media` must reach the listing, not be read as a batch id."""
    assert "snapshots" in api.get("/api/queue/media").json()


# --- startup wiring ---------------------------------------------------------


def test_startup_reconciliation_runs_and_cannot_break_boot(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(queue_mod, "reconcile_queue_media",
                        lambda *a, **k: calls.append(1))
    main._reconcile_media_at_startup()
    assert calls == [1]

    def _boom(*a, **k):
        raise OSError("queue_media unreadable")

    monkeypatch.setattr(queue_mod, "reconcile_queue_media", _boom)
    main._reconcile_media_at_startup()   # must not raise
    assert "reconciliation skipped" in capsys.readouterr().out


def test_lifespan_reconciles_before_the_scheduler_starts():
    """Source-level: nothing may be writing a snapshot while the pass runs.
    The queue-media path has no browser to drive, so this is asserted against
    the source rather than exercised end to end."""
    src = (main.__file__).replace(".pyc", ".py")
    body = open(src).read()
    start = body.index("async def lifespan(app)")
    lifespan_src = body[start:body.index("app = FastAPI", start)]
    assert lifespan_src.index("_reconcile_media_at_startup()") < \
        lifespan_src.index("scheduler_loop")


def test_the_suite_can_never_reach_the_real_queue_media(tmp_path):
    """Cold-review finding, 2026-07-30, and the most dangerous defect this
    batch introduced. `_reconcile_media_at_startup` runs from the FastAPI
    lifespan and *deletes directories*, so every test entering
    `TestClient(main.app)` without patching these three names ran a deleting
    pass over the maintainer's real `queue_media/`. It was proven by
    `test_scheduling_slice3.py::TestLifespan` printing a classification of the
    real 2026-07-27 forensic snapshot; nothing was lost only because that
    directory classifies as retained evidence.

    The autouse `tmp_queue_paths` fixture in conftest closes it. This test
    fails if that fixture is removed or renamed."""
    from backend.config import PROJECT_ROOT

    for name in ("QUEUE_FILE", "HISTORY_FILE", "QUEUE_MEDIA_DIR"):
        patched = getattr(queue_mod, name)
        assert not patched.is_relative_to(PROJECT_ROOT), (
            f"queue.{name} still points inside the project root during tests "
            f"({patched}) — the suite can delete real media"
        )


def test_deletion_prompt_distinguishes_ambiguous_from_retained(env):
    """The two deletable classes fail differently: 'retained' means a batch
    already ran and did not fully succeed, 'ambiguous' can mean a batch being
    created right now. One fixed warning describes the first and misleads about
    the second (cold review, 2026-07-30). Source-level, in the same style as
    the other frontend assertions — there is no browser in the suite."""
    from backend.config import FRONTEND_DIR
    body = (FRONTEND_DIR / "index.html").read_text()
    start = body.index("async function deleteQueueMedia(")
    fn = body[start:body.index("async function cancelQueueBatch", start)]
    assert "classification === 'ambiguous'" in fn
    assert "stop that " in fn and "from posting" in fn, (
        "the ambiguous branch must say that deleting could stop a batch that "
        "has not fired yet"
    )
    assert "${reason || classification}" in fn, (
        "the snapshot's own reason must reach the prompt, not just a class name"
    )


# --- snapshot filename disambiguation (#4) ----------------------------------


def test_slots_sharing_a_basename_get_distinct_snapshot_files(tmp_path):
    """Two slots whose media resolve to the same file name used to land on one
    file: the second copy overwrote the first and both queue rows then pointed
    at the survivor, posting one slot's media to two accounts."""
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "clip.mp4").write_bytes(b"slot-A-media")
    (media / "sub" / "clip.mp4").write_bytes(b"slot-B-media")
    qmedia = tmp_path / "queue_media"

    snapped = _snapshot_media(
        BATCH_A,
        [SlotBatch(slot="A", media_path="clip.mp4", caption="a"),
         SlotBatch(slot="B", media_path="sub/clip.mp4", caption="b")],
        media_dir=media, queue_media_dir=qmedia,
    )
    paths = [s.media_path for s in snapped]
    assert len(set(paths)) == 2, "each slot must keep its own snapshot file"
    assert open(paths[0], "rb").read() == b"slot-A-media"
    assert open(paths[1], "rb").read() == b"slot-B-media"


def test_snapshot_names_are_left_alone_when_they_do_not_collide(tmp_path):
    """Disambiguation is collision-only: retained evidence keeps the readable
    filename the maintainer inspects it by."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "A_clip.mp4").write_bytes(b"x")
    snapped = _snapshot_media(
        BATCH_A, [SlotBatch(slot="A", media_path="A_clip.mp4", caption="a")],
        media_dir=media, queue_media_dir=tmp_path / "queue_media")
    assert snapped[0].media_path.endswith("/A_clip.mp4")


# --- #46: queue-row tolerance symmetry --------------------------------------


def _row(**overrides):
    now = datetime.now(timezone.utc)
    row = {
        "id": BATCH_A,
        "fire_time": (now + timedelta(hours=1)).isoformat(),
        "created_at": now.isoformat(),
        "slots": [{"slot": "A", "media_path": "A_clip.mp4", "caption": "c"}],
        "status": "pending",
        "headless": False,
        "results": None,
    }
    row.update(overrides)
    return row


def test_unknown_top_level_field_no_longer_discards_the_batch(tmp_path):
    """#46: `from_dict` ended in `cls(**d)`, so a retired top-level field made
    load_queue report the row as malformed and skip it — silently dropping a
    scheduled batch instead of failing loudly, the same hazard #8 fixed for
    slot fields."""
    qf = tmp_path / "queue.jsonl"
    qf.write_text(json.dumps(_row(retired_flag="x")) + "\n")
    batches = load_queue(qf)
    assert len(batches) == 1
    assert batches[0].id == BATCH_A
    assert not hasattr(batches[0], "retired_flag")


def test_unknown_top_level_field_is_reported_once_per_process(tmp_path, capsys):
    queue_mod._REPORTED_BATCH_KEYS.clear()
    qf = tmp_path / "queue.jsonl"
    qf.write_text(json.dumps(_row(retired_flag="x")) + "\n")
    # Asserting the loads too: the notice is printed *before* the constructor
    # runs, so a version that reports the field and then discards the row would
    # otherwise satisfy the count on its own.
    assert len(load_queue(qf)) == 1
    assert len(load_queue(qf)) == 1
    out = capsys.readouterr().out
    assert out.count("unknown batch field") == 1, (
        "the queue is re-read constantly; one line per read would bury the "
        "malformed-line and history-failure records on the same channel"
    )
    queue_mod._REPORTED_BATCH_KEYS.clear()


@pytest.mark.parametrize("row,why", [
    (_row(status=None) | {"fire_time": "not-a-date"}, "unparseable fire_time"),
    ({k: v for k, v in _row().items() if k != "status"}, "missing required field"),
    (_row(slots="not-a-list"), "slots not a list of dicts"),
])
def test_unparseable_rows_are_still_skipped_loudly(tmp_path, capsys, row, why):
    """The remaining asymmetry is deliberate. Tolerance covers fields we no
    longer need, never fields we never got: inventing a default for a missing
    required field would schedule a post nobody wrote."""
    qf = tmp_path / "queue.jsonl"
    qf.write_text(json.dumps(row) + "\n")
    assert load_queue(qf) == [], why
    assert "malformed line" in capsys.readouterr().out


def test_retired_slot_field_tolerance_is_unchanged(tmp_path):
    """#8's behaviour must survive the #46 change to the same function."""
    qf = tmp_path / "queue.jsonl"
    slots = [{"slot": "A", "media_path": "A_clip.mp4", "caption": "c",
              "style": ""}]
    qf.write_text(json.dumps(_row(slots=slots)) + "\n")
    assert len(load_queue(qf)) == 1
