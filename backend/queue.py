"""Scheduling queue: data model + JSONL persistence + media snapshots.

DESIGN-scheduling.md §5a–5b. queue.jsonl holds only pending/running/interrupted
batches; terminal states are pruned after results land in history.jsonl.
"""

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, fields, asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from backend.config import HISTORY_FILE, MEDIA_DIR, QUEUE_FILE, QUEUE_MEDIA_DIR
from backend.models import QUEUE_MIN_LEAD, validate_future_fire_time
from backend.logging_setup import get_logger

_log = get_logger("queue")


@dataclass
class SlotBatch:
    slot: str
    media_path: str
    caption: str


# Keys that older queue rows carry and current code no longer models. `style`
# was always written empty and never read back (#8); dropping the field would
# otherwise make every legacy row raise TypeError in from_dict, which
# load_queue catches and reports as a *malformed line* — silently discarding a
# scheduled batch rather than failing loudly.
_RETIRED_SLOT_KEYS = frozenset({"style"})

# Field names already reported by _slot_from_dict this process. Deliberately
# module-level and never cleared: the point is one line per unknown field for
# the life of the server, not one per queue read.
_REPORTED_SLOT_KEYS: set[str] = set()

# Same once-per-process rule for top-level batch fields (#46).
_REPORTED_BATCH_KEYS: set[str] = set()


def _slot_from_dict(s: dict) -> SlotBatch:
    """Build a SlotBatch from a persisted row, tolerating retired fields.

    A queue row written by an older version carries keys this dataclass no
    longer has. Those rows are already on disk and still have to fire, so an
    unknown key is dropped rather than raised. A retired key is expected and
    silent; anything else is reported, because it means the row was written by
    something this code does not understand.

    That report is emitted **once per process per field name**. The queue is
    re-read constantly — roughly four times per batch execution plus every 30s
    poll — so a per-read message would put thousands of lines a day onto the
    same console channel carrying the malformed-line and history-failure
    records that actually need to be seen (review 2026-07-30).
    """
    if not isinstance(s, dict):
        # Not a row we can reason about. Fall through to the constructor so
        # load_queue reports it as the malformed line it is, rather than
        # printing a per-character "unknown field" notice on the way past.
        return SlotBatch(**s)
    known = {f.name for f in fields(SlotBatch)}
    unexpected = set(s) - known - _RETIRED_SLOT_KEYS - _REPORTED_SLOT_KEYS
    if unexpected:
        _REPORTED_SLOT_KEYS.update(unexpected)
        _log.warning(f"[queue] note: ignoring unknown slot field(s) "
              f"{', '.join(sorted(unexpected))} in a queued batch")
    return SlotBatch(**{k: v for k, v in s.items() if k in known})


@dataclass
class QueuedBatch:
    id: str
    fire_time: datetime
    created_at: datetime
    slots: list[SlotBatch]
    status: str
    headless: bool
    results: list | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fire_time"] = self.fire_time.isoformat()
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "QueuedBatch":
        """Build a QueuedBatch from a persisted row.

        Unknown **top-level** keys are dropped for the same reason unknown slot
        keys are (#8): load_queue reports a constructor failure as a malformed
        line and skips the row, so an untolerated retired field would silently
        discard a scheduled batch instead of failing loudly. The tolerance was
        applied only to slot rows in #8, leaving the two levels asymmetric
        (#46); this closes that.

        The asymmetry that *remains* is deliberate. A row missing a required
        field, or whose `slots` is not a list of dicts, still raises and is
        skipped with a log line: that row cannot be turned into a batch we
        could fire, and inventing defaults for it would schedule a post nobody
        wrote. Tolerance covers fields we no longer need, never fields we
        never got.
        """
        d = dict(d)
        d["fire_time"] = datetime.fromisoformat(d["fire_time"])
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        d["slots"] = [_slot_from_dict(s) for s in d["slots"]]
        known = {f.name for f in fields(cls)}
        unexpected = set(d) - known - _REPORTED_BATCH_KEYS
        if unexpected:
            _REPORTED_BATCH_KEYS.update(unexpected)
            _log.warning(f"[queue] note: ignoring unknown batch field(s) "
                  f"{', '.join(sorted(unexpected))} in a queued batch")
        return cls(**{k: v for k, v in d.items() if k in known})


def _load_queue_detailed(queue_file: Path | None = None
                         ) -> tuple[list[QueuedBatch], int]:
    """Load batches, and report how many lines could not be parsed.

    The count exists for reconciliation, which must not treat "absent from the
    queue" as "belongs to no batch" when part of the queue was unreadable
    (review 2026-07-30).
    """
    qf = queue_file or QUEUE_FILE
    if not qf.exists():
        return [], 0
    batches = []
    unparseable = 0
    for i, line in enumerate(qf.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            batches.append(QueuedBatch.from_dict(json.loads(line)))
        except Exception as e:
            unparseable += 1
            _log.error(f"[queue] ERROR: malformed line {i} in {qf.name}, skipping: {e}")
    return batches, unparseable


def load_queue(queue_file: Path | None = None) -> list[QueuedBatch]:
    """Load all batches from the queue file. Malformed lines are logged and
    skipped — never rewritten as a side effect of loading."""
    return _load_queue_detailed(queue_file)[0]


def save_queue(batches: list[QueuedBatch], queue_file: Path | None = None):
    """Atomic rewrite: temp file + os.replace."""
    qf = queue_file or QUEUE_FILE
    tmp = qf.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            for b in batches:
                f.write(json.dumps(b.to_dict()) + "\n")
        os.replace(tmp, qf)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _unique_snapshot_path(snap_dir: Path, name: str) -> Path:
    """Return a path inside snap_dir that does not overwrite an earlier slot.

    Snapshots were named by basename alone, so two slots whose media resolve to
    the same file name landed on one file: the second copy overwrote the first
    and *both* queue rows then pointed at the survivor, silently posting one
    slot's media to two accounts. Uploads through the UI are prefixed per slot
    and cannot collide, but a handcrafted API request can name any file under
    the media directory (#4).

    Disambiguation is applied only on collision, so ordinary snapshots keep the
    readable filename the maintainer needs when inspecting retained evidence.
    """
    dst = snap_dir / name
    if not dst.exists():
        return dst
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(2, 1000):
        candidate = snap_dir / f"{stem}__{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free snapshot name for '{name}'")


def _snapshot_media(batch_id: str, slots: list[SlotBatch],
                    media_dir: Path | None = None,
                    queue_media_dir: Path | None = None) -> list[SlotBatch]:
    """Copy each slot's media into queue_media/{batch_id}/ and return new
    SlotBatch list with paths pointing to the snapshot."""
    md = media_dir or MEDIA_DIR
    qmd = queue_media_dir or QUEUE_MEDIA_DIR
    snap_dir = qmd / batch_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    updated = []
    for s in slots:
        src = md / s.media_path
        dst = _unique_snapshot_path(snap_dir, Path(s.media_path).name)
        shutil.copy2(src, dst)
        updated.append(SlotBatch(
            slot=s.slot,
            media_path=str(dst),
            caption=s.caption,
        ))
    return updated


def _delete_snapshot(batch_id: str, queue_media_dir: Path | None = None):
    """Delete a batch's media snapshot directory."""
    qmd = queue_media_dir or QUEUE_MEDIA_DIR
    snap_dir = qmd / batch_id
    if snap_dir.exists():
        shutil.rmtree(snap_dir, ignore_errors=True)


def add_batch(fire_time: datetime, slots: list[SlotBatch], headless: bool,
              queue_file: Path | None = None, media_dir: Path | None = None,
              queue_media_dir: Path | None = None) -> QueuedBatch:
    """Validate, snapshot media, persist a new pending batch. Returns the batch."""
    validate_future_fire_time(fire_time, QUEUE_MIN_LEAD)
    now = datetime.now(timezone.utc)

    batch_id = uuid.uuid4().hex
    snapped = _snapshot_media(batch_id, slots, media_dir, queue_media_dir)
    batch = QueuedBatch(
        id=batch_id,
        fire_time=fire_time,
        created_at=now,
        slots=snapped,
        status="pending",
        headless=headless,
    )
    batches = load_queue(queue_file)
    batches.append(batch)
    save_queue(batches, queue_file)
    return batch


def cancel_batch(batch_id: str, queue_file: Path | None = None,
                 queue_media_dir: Path | None = None) -> bool:
    """Cancel a pending batch. Returns True if found and cancelled."""
    batches = load_queue(queue_file)
    found = None
    for b in batches:
        if b.id == batch_id and b.status == "pending":
            found = b
            break
    if not found:
        return False
    batches.remove(found)
    save_queue(batches, queue_file)
    _delete_snapshot(batch_id, queue_media_dir)
    return True


def dismiss_batch(batch_id: str, queue_file: Path | None = None,
                  queue_media_dir: Path | None = None) -> bool:
    """Dismiss an interrupted batch. Returns True if found and dismissed."""
    batches = load_queue(queue_file)
    found = None
    for b in batches:
        if b.id == batch_id and b.status == "interrupted":
            found = b
            break
    if not found:
        return False
    batches.remove(found)
    save_queue(batches, queue_file)
    _delete_snapshot(batch_id, queue_media_dir)
    return True


# ---------------------------------------------------------------------------
# Media snapshot reconciliation (#4)
# ---------------------------------------------------------------------------
#
# Every classification below answers one question: is this directory the only
# surviving copy of media for a batch that may have posted? Whenever the answer
# is "maybe", the directory is kept. Deleting a retained snapshot destroys the
# evidence a partial batch is reconstructed from and forces a re-upload to
# retry; keeping an orphan costs disk space. The rules are asymmetric on
# purpose.
#
# Nothing here holds state of its own. Classification is recomputed from
# queue.jsonl and history.jsonl on every call, so it cannot go stale across a
# restart and there is no cleanup ledger to corrupt.

SNAPSHOT_ACTIVE = "active"
SNAPSHOT_RETAINED = "retained"
SNAPSHOT_ORPHAN = "orphan"
SNAPSHOT_AMBIGUOUS = "ambiguous"
SNAPSHOT_FOREIGN = "foreign"

# A directory with no queue entry and no history row is normally the crash
# window in cancel_batch/dismiss_batch, which save the queue and *then* delete
# the snapshot (BE-18) — or an add_batch that died between snapshotting and
# persisting. It is also, briefly, what a snapshot being written right now
# looks like. Lifespan startup completes before requests are served, so
# reconciliation cannot race add_batch today; this age floor keeps that from
# being load-bearing, because if the ordering ever changes the failure is
# deleting media for a batch that is about to fire.
ORPHAN_MIN_AGE_S = 3600

_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class SnapshotInfo:
    """One directory under queue_media/, and what may be done with it."""
    batch_id: str
    #: Absolute filesystem path. The frontend does not use it; it is here for
    #: the maintainer reading `GET /api/queue/media` directly, who needs to go
    #: and look at the files before deciding to delete them. Kept deliberately
    #: after two reviewers noted it was unused by the UI (2026-07-30) — this is
    #: a localhost-only tool, so the path is not sensitive.
    path: str
    classification: str
    reason: str
    files: list[str]
    size_bytes: int
    modified: str
    #: Safe for startup reconciliation to remove with no maintainer involved.
    auto_deletable: bool
    #: Safe for the maintainer to remove explicitly, having seen the reason.
    deletable: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _history_index(history_file: Path | None = None
                   ) -> tuple[dict[str, dict], int]:
    """Summarise history rows per batch_id, with a count of unreadable lines.

    A row counts as unsuccessful unless it carries an `errors` list that is
    empty — matching PostResult.success. A row whose `errors` key is missing or
    malformed counts as unsuccessful too: it does not *prove* the slot posted
    cleanly, and only proof justifies deleting media. History rows before
    2026-07-27 are known to be unreliable, which is the same argument.
    """
    hf = history_file or HISTORY_FILE
    index: dict[str, dict] = {}
    unparseable = 0
    if not hf.exists():
        return index, 0
    for line in hf.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            # An unreadable row proves nothing either way. It is counted, not
            # ignored: the slot result it was going to describe might be the
            # failure that should have kept a snapshot alive.
            unparseable += 1
            continue
        if not isinstance(row, dict):
            unparseable += 1
            continue
        batch_id = row.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            # A row with no batch id belongs to a manual run, which never has a
            # snapshot. Not evidence of anything, and not a defect.
            continue
        entry = index.setdefault(batch_id, {"rows": 0, "unsuccessful": 0})
        entry["rows"] += 1
        errors = row.get("errors")
        if not isinstance(errors, list) or errors:
            entry["unsuccessful"] += 1
    return index, unparseable


def _dir_size_and_mtime(path: Path) -> tuple[list[str], int, float]:
    """Return (file names, total bytes, newest mtime) for a snapshot dir.

    Walks the whole tree, because deletion does: a nested directory would
    otherwise be reported as "0 files, 0 KB" in the panel and then removed
    anyway by `rmtree`. Its mtime counts too — the age floor must see the
    newest thing in the tree, not the newest thing at the top of it.

    The directory's own mtime is the baseline for the same reason: `copy2`
    preserves the *source* file's timestamps, so a snapshot copied a moment ago
    from media uploaded last week has week-old file mtimes and would otherwise
    read as long-dead.
    """
    names, total = [], 0
    newest = path.stat().st_mtime
    for f in sorted(path.rglob("*")):
        st = f.stat()
        newest = max(newest, st.st_mtime)
        if not f.is_file():
            continue
        names.append(str(f.relative_to(path)))
        total += st.st_size
    return names, total, newest


def classify_snapshots(queue_file: Path | None = None,
                       history_file: Path | None = None,
                       queue_media_dir: Path | None = None,
                       now: datetime | None = None,
                       min_orphan_age_s: int = ORPHAN_MIN_AGE_S,
                       ) -> list[SnapshotInfo]:
    """Classify every entry under queue_media/ against the queue and history.

    Pure apart from reading those three locations — it deletes nothing.
    """
    qmd = queue_media_dir or QUEUE_MEDIA_DIR
    if not qmd.exists():
        return []
    batches, bad_queue_lines = _load_queue_detailed(queue_file)
    queued = {b.id: b for b in batches}
    history, bad_history_lines = _history_index(history_file)
    now_ts = (now or datetime.now(timezone.utc)).timestamp()

    # Both records are read as *evidence*, and evidence with a hole in it
    # cannot clear a snapshot for deletion (review 2026-07-30). A truncated
    # queue row is skipped by the loader, so its batch simply disappears —
    # leaving a scheduled, running, or interrupted batch's media looking
    # exactly like an orphan, and one boot away from being deleted. A
    # truncated history row can likewise hide the one failed slot that would
    # have kept a snapshot alive. While either file has an unreadable line,
    # nothing is deleted automatically; the snapshots are reported instead, and
    # cleanup resumes once the file is repaired.
    evidence_complete = not (bad_queue_lines or bad_history_lines)

    out: list[SnapshotInfo] = []
    for entry in sorted(qmd.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or not _BATCH_ID_RE.match(entry.name):
            # Not something this code created. Never touched — it may be the
            # maintainer's own working copy of media parked here.
            out.append(SnapshotInfo(
                batch_id=entry.name, path=str(entry),
                classification=SNAPSHOT_FOREIGN,
                reason="not a batch snapshot directory; left untouched",
                files=[], size_bytes=0,
                modified=_iso(entry.stat().st_mtime),
                auto_deletable=False, deletable=False,
            ))
            continue

        files, size, mtime = _dir_size_and_mtime(entry)
        common = dict(batch_id=entry.name, path=str(entry), files=files,
                      size_bytes=size, modified=_iso(mtime))

        batch = queued.get(entry.name)
        if batch is not None:
            out.append(SnapshotInfo(
                **common, classification=SNAPSHOT_ACTIVE,
                reason=f"still in the queue with status '{batch.status}'",
                auto_deletable=False, deletable=False,
            ))
            continue

        hist = history.get(entry.name)
        if hist is not None:
            if hist["unsuccessful"]:
                out.append(SnapshotInfo(
                    **common, classification=SNAPSHOT_RETAINED,
                    reason=(f"history records {hist['unsuccessful']} of "
                            f"{hist['rows']} slot result(s) as unsuccessful — "
                            f"kept as retry and forensic evidence"),
                    auto_deletable=False, deletable=True,
                ))
            else:
                out.append(SnapshotInfo(
                    **common, classification=SNAPSHOT_ORPHAN,
                    reason=(f"all {hist['rows']} recorded slot result(s) "
                            f"posted successfully; the snapshot outlived its "
                            f"batch"),
                    auto_deletable=True, deletable=True,
                ))
            continue

        age_s = now_ts - mtime
        if age_s < min_orphan_age_s:
            out.append(SnapshotInfo(
                **common, classification=SNAPSHOT_AMBIGUOUS,
                reason=(f"no queue entry and no history row, but written "
                        f"{int(max(age_s, 0) // 60)} minute(s) ago — may be a "
                        f"batch still being created"),
                auto_deletable=False, deletable=True,
            ))
        else:
            out.append(SnapshotInfo(
                **common, classification=SNAPSHOT_ORPHAN,
                reason=("no queue entry and no history row — a cancelled or "
                        "dismissed batch, or a batch whose creation was "
                        "interrupted"),
                auto_deletable=True, deletable=True,
            ))

    if not evidence_complete:
        damaged = " and ".join(
            part for part in (
                f"{bad_queue_lines} unreadable queue line(s)" if bad_queue_lines else "",
                f"{bad_history_lines} unreadable history line(s)" if bad_history_lines else "",
            ) if part
        )
        out = [s if s.classification != SNAPSHOT_ORPHAN else replace(
            s,
            classification=SNAPSHOT_AMBIGUOUS,
            reason=(f"{s.reason} — but there are {damaged}, so this cannot be "
                    f"confirmed; kept until the file is repaired"),
            auto_deletable=False,
        ) for s in out]
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def reconcile_queue_media(queue_file: Path | None = None,
                          history_file: Path | None = None,
                          queue_media_dir: Path | None = None,
                          now: datetime | None = None,
                          min_orphan_age_s: int = ORPHAN_MIN_AGE_S,
                          ) -> list[SnapshotInfo]:
    """Delete true orphans under queue_media/. Returns the full classification.

    Only SNAPSHOT_ORPHAN is removed. Anything failed, partial, interrupted,
    still queued, ambiguous, or unrecognised survives and is reported, so the
    maintainer can act on it through the explicit cleanup path instead.
    """
    snapshots = classify_snapshots(queue_file, history_file, queue_media_dir,
                                   now, min_orphan_age_s)
    kept: dict[str, int] = {}
    for snap in snapshots:
        if snap.auto_deletable:
            _delete_snapshot(snap.batch_id, queue_media_dir)
            _log.info(f"[queue] reconciliation: removed orphaned media snapshot "
                  f"{snap.batch_id[:8]} ({snap.reason})")
        else:
            kept[snap.classification] = kept.get(snap.classification, 0) + 1
    for snap in snapshots:
        if snap.classification == SNAPSHOT_RETAINED:
            _log.warning(f"[queue] reconciliation: keeping snapshot "
                  f"{snap.batch_id[:8]} — {snap.reason}")
    if kept:
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kept.items()))
        _log.warning(f"[queue] reconciliation: {summary} snapshot(s) kept; review "
              f"them in the queue panel")
    return snapshots


def remove_snapshot(batch_id: str, queue_file: Path | None = None,
                    history_file: Path | None = None,
                    queue_media_dir: Path | None = None,
                    ) -> SnapshotInfo:
    """Delete one snapshot on explicit maintainer instruction.

    Raises LookupError if there is no such snapshot and PermissionError if it
    is one this code refuses to delete — an active batch's media, or a
    directory it did not create.
    """
    snapshots = classify_snapshots(queue_file, history_file, queue_media_dir)
    match = next((s for s in snapshots if s.batch_id == batch_id), None)
    if match is None:
        raise LookupError(f"no media snapshot for batch {batch_id}")
    if not match.deletable:
        raise PermissionError(
            f"snapshot {batch_id} is {match.classification}: {match.reason}")
    _delete_snapshot(batch_id, queue_media_dir)
    _log.info(f"[queue] removed {match.classification} media snapshot "
          f"{batch_id[:8]} on maintainer request")
    return match


def update_fire_time(batch_id: str, new_fire_time: datetime,
                     queue_file: Path | None = None) -> bool:
    """Reschedule a pending batch. Returns True if found and updated."""
    validate_future_fire_time(new_fire_time, QUEUE_MIN_LEAD)

    batches = load_queue(queue_file)
    for b in batches:
        if b.id == batch_id and b.status == "pending":
            b.fire_time = new_fire_time
            save_queue(batches, queue_file)
            return True
    return False
