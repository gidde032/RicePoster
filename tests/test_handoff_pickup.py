"""Tests for the RiceClipper handoff pickup (Pull from Clipper).

Ingest only *stages* media now — captioning happens in the browser afterward,
through the existing `/api/generate-caption` path (grounded on a frame captured
from the staged clip), so there is no server-side caption call here. The autouse
`tmp_handoff_paths` fixture redirects the handoff root and media dir to temp
paths so ingest never scans/moves the maintainer's real handoff or writes into
the real `media/`.
"""

import json
from types import SimpleNamespace

import pytest

from backend import handoff_pickup, main
from tests.paths import PROJECT_ROOT


def _write_batch(root, batch_id, clips, *, schema_version=1, write_files=None):
    """Create a batch dir. `clips` = list of (position, filename, transcript).

    `write_files` limits which filenames actually get written (to simulate a
    missing clip); default writes them all. `manifest.json` is written last.
    """
    if write_files is None:
        write_files = {name for _, name, _ in clips}
    d = root / batch_id
    d.mkdir()
    manifest_clips = []
    for position, filename, transcript in clips:
        if filename in write_files:
            (d / filename).write_bytes(f"{filename} bytes".encode())
        manifest_clips.append(
            {
                "file": filename,
                "position": position,
                "transcript": transcript,
                "header": "",
                "presets": {"caption_style": "classic", "header_style": "plain"},
            }
        )
    manifest = {
        "schema_version": schema_version,
        "batch_id": batch_id,
        "created_at": "2026-08-26T00:00:00Z",
        "producer": "riceclipper",
        "clips": manifest_clips,
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


# --- ingest_oldest ----------------------------------------------------------


def test_ingest_targets_active_accounts_and_archives_source_with_receipt(tmp_handoff_paths):
    """Incident repair (CRITICAL): staging must retain recoverable source evidence.

    Fix: freeze explicit account IDs, atomically archive the producer batch, and
    preserve its manifest plus staged-file hashes in a durable receipt.
    """
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "first transcript"), (2, "clip_2.mp4", "second transcript")],
    )

    result = handoff_pickup.ingest_oldest(["creator-one", "creator-two"])

    assert result["batch_id"] == "batch_20260826_120000_aaaa"
    slots = result["slots"]
    assert [s["slot"] for s in slots] == ["creator-one", "creator-two"]
    assert slots[0]["filename"] == "creator-one_batch_20260826_120000_aaaa_clip_1.mp4"
    assert slots[0]["topic"] == "first transcript"  # transcript -> caption topic
    assert slots[0]["style"] == "generic"
    assert slots[0]["media_type"] == "video"
    assert "caption" not in slots[0]  # captioning happens in the browser now

    assert (media / "creator-one_batch_20260826_120000_aaaa_clip_1.mp4").is_file()
    assert (media / "creator-two_batch_20260826_120000_aaaa_clip_2.mp4").is_file()
    assert not batch.exists()  # removed from the producer-ready queue
    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / batch.name
    assert (archive / "clip_1.mp4").is_file()
    assert (archive / "manifest.json").is_file()
    receipt = json.loads((archive / handoff_pickup.RECEIPT_FILENAME).read_text())
    assert receipt["status"] == "staged"
    assert receipt["target_account_ids"] == ["creator-one", "creator-two"]
    assert receipt["manifest"]["batch_id"] == batch.name
    assert len(receipt["slots"][0]["sha256"]) == 64
    assert result["source_archived"] is True and result["replayed"] is False


def test_ingest_selects_oldest_and_leaves_newer(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    old = _write_batch(handoff, "batch_20260101_000000_aaaa", [(1, "clip_1.mp4", "old")])
    new = _write_batch(handoff, "batch_20260102_000000_bbbb", [(1, "clip_1.mp4", "new")])

    result = handoff_pickup.ingest_oldest(["A"])

    assert result["batch_id"] == "batch_20260101_000000_aaaa"
    assert not old.exists()  # oldest moved out of the ready queue
    assert (handoff / handoff_pickup.ARCHIVE_DIRNAME / old.name).is_dir()
    assert new.exists()  # newer retained for the next pull


def test_ingest_rejects_more_clips_than_slots(tmp_handoff_paths):
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    # The explicit active roster has three accounts; four clips must be rejected.
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(i, f"clip_{i}.mp4", f"t{i}") for i in range(1, 5)],
    )

    with pytest.raises(handoff_pickup.HandoffPickupError, match="only 3 account"):
        handoff_pickup.ingest_oldest(["A", "B", "C"])

    assert batch.exists()  # nothing consumed
    assert list(media.iterdir()) == []  # nothing staged


def test_ingest_rolls_back_and_retains_on_missing_clip(tmp_handoff_paths):
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    # Second clip is listed in the manifest but its file is absent.
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "one"), (2, "clip_2.mp4", "two")],
        write_files={"clip_1.mp4"},
    )

    with pytest.raises(handoff_pickup.HandoffPickupError, match="missing"):
        handoff_pickup.ingest_oldest(["A", "B", "C"])

    assert batch.exists()  # whole batch retained for re-pull
    assert list(media.iterdir()) == []  # first clip's staged media rolled back


def test_ingest_rejects_unsupported_schema(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(handoff, "batch_x_aaaa", [(1, "clip_1.mp4", "t")], schema_version=999)
    with pytest.raises(handoff_pickup.HandoffPickupError, match="schema_version"):
        handoff_pickup.ingest_oldest(["A", "B", "C"])


def test_ingest_raises_when_no_batches(tmp_handoff_paths):
    with pytest.raises(handoff_pickup.NoBatchAvailable):
        handoff_pickup.ingest_oldest(["A", "B", "C"])


def test_half_written_batch_without_manifest_is_ignored(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    # A dir with clips but no manifest.json is mid-write; it must be skipped.
    partial = handoff / "batch_20260826_120000_aaaa"
    partial.mkdir()
    (partial / "clip_1.mp4").write_bytes(b"x")
    with pytest.raises(handoff_pickup.NoBatchAvailable):
        handoff_pickup.ingest_oldest(["A", "B", "C"])


def test_unacknowledged_pull_replays_frozen_targets_and_restores_media(tmp_handoff_paths):
    """Incident repair (CRITICAL): response/DOM failure must be safely retryable."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "hello"), (2, "clip_2.mp4", "world")],
    )
    first = handoff_pickup.ingest_oldest(["creator-one", "creator-two"])
    missing = media / first["slots"][0]["filename"]
    missing.unlink()

    replay = handoff_pickup.ingest_oldest(["creator-one", "creator-two"])

    assert replay["batch_id"] == first["batch_id"]
    assert replay["slots"] == first["slots"]
    assert replay["replayed"] is True
    assert missing.read_bytes() == b"clip_1.mp4 bytes"


def test_unacknowledged_pull_fails_closed_when_roster_would_retarget(tmp_handoff_paths):
    """Incident repair (CRITICAL): a roster change must never retarget staged work."""
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(handoff, "batch_20260826_120000_aaaa", [(1, "clip_1.mp4", "hello")])
    handoff_pickup.ingest_oldest(["creator-one"])

    with pytest.raises(handoff_pickup.HandoffPickupError, match="creator-one"):
        handoff_pickup.ingest_oldest(["secondary-one"])

    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / "batch_20260826_120000_aaaa"
    assert archive.is_dir()
    assert json.loads((archive / handoff_pickup.RECEIPT_FILENAME).read_text())["status"] == "staged"


def test_acknowledgement_is_idempotent_and_never_deletes_archive(tmp_handoff_paths):
    """Incident repair (HIGH): ACK advances state but preserves recovery evidence."""
    handoff = tmp_handoff_paths["handoff"]
    batch_id = "batch_20260826_120000_aaaa"
    _write_batch(handoff, batch_id, [(1, "clip_1.mp4", "hello")])
    handoff_pickup.ingest_oldest(["creator-one"])

    assert handoff_pickup.acknowledge(batch_id, ["creator-one"])["status"] == "applied"
    assert handoff_pickup.acknowledge(batch_id, ["creator-one"])["status"] == "applied"

    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / batch_id
    receipt = json.loads((archive / handoff_pickup.RECEIPT_FILENAME).read_text())
    assert archive.is_dir()
    assert (archive / "clip_1.mp4").is_file()
    assert receipt["status"] == "applied"
    assert receipt["applied_at"]
    with pytest.raises(handoff_pickup.NoBatchAvailable):
        handoff_pickup.ingest_oldest(["creator-one"])


def test_receipt_written_before_archive_move_is_completed_on_retry(tmp_handoff_paths):
    """Incident repair (HIGH): a crash at the move boundary must remain recoverable."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch_id = "batch_20260826_120000_aaaa"
    _write_batch(handoff, batch_id, [(1, "clip_1.mp4", "hello")])
    first = handoff_pickup.ingest_oldest(["creator-one"])
    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / batch_id
    source_again = handoff / batch_id
    archive.rename(source_again)
    staged = media / first["slots"][0]["filename"]
    staged.unlink()

    replay = handoff_pickup.ingest_oldest(["creator-one"])

    assert replay["replayed"] is True
    assert archive.is_dir()
    assert not source_again.exists()
    assert staged.is_file()


def test_manifest_batch_identity_cannot_change_paths(tmp_handoff_paths):
    """Security regression (HIGH): manifest identity is bound to its directory."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(handoff, "batch_20260826_120000_aaaa", [(1, "clip.mp4", "x")])
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["batch_id"] = "batch_different"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(handoff_pickup.HandoffPickupError, match="exactly match"):
        handoff_pickup.ingest_oldest(["creator-one"])

    assert batch.is_dir()
    assert list(media.iterdir()) == []


def test_manifest_positions_must_be_unique_and_contiguous(tmp_handoff_paths):
    """Cold-review repair (HIGH): ambiguous positions never choose account order."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "one"), (2, "clip_2.mp4", "two")],
    )
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["clips"][1]["position"] = 1
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(handoff_pickup.HandoffPickupError, match="duplicate clip position"):
        handoff_pickup.ingest_oldest(["creator-one", "creator-two"])

    assert batch.is_dir()
    assert list(media.iterdir()) == []


def test_clip_symlink_is_rejected_without_leaving_ready_queue(tmp_handoff_paths, tmp_path):
    """Cold-review repair (HIGH): handoff clips cannot escape through symlinks."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(handoff, "batch_20260826_120000_aaaa", [(1, "clip.mp4", "x")])
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    (batch / "clip.mp4").unlink()
    (batch / "clip.mp4").symlink_to(outside)

    with pytest.raises(handoff_pickup.HandoffPickupError, match="unsupported"):
        handoff_pickup.ingest_oldest(["creator-one"])

    assert batch.is_dir()
    assert list(media.iterdir()) == []


def test_archive_root_symlink_is_rejected_before_staging(tmp_handoff_paths, tmp_path):
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    _write_batch(handoff, "batch_20260826_120000_aaaa", [(1, "clip.mp4", "x")])
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    (handoff / handoff_pickup.ARCHIVE_DIRNAME).symlink_to(outside, target_is_directory=True)

    with pytest.raises(handoff_pickup.HandoffPickupError, match="real directory"):
        handoff_pickup.ingest_oldest(["creator-one"])

    assert list(media.iterdir()) == []
    assert list(outside.iterdir()) == []


def test_archived_batch_symlink_is_never_followed(tmp_handoff_paths, tmp_path):
    handoff = tmp_handoff_paths["handoff"]
    archive_root = handoff / handoff_pickup.ARCHIVE_DIRNAME
    archive_root.mkdir()
    outside = tmp_path / "outside-batch"
    outside.mkdir()
    batch_id = "batch_20260826_120000_aaaa"
    (archive_root / batch_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(handoff_pickup.HandoffPickupError, match="no safe archived"):
        handoff_pickup.acknowledge(batch_id, ["creator-one"])

    assert list(outside.iterdir()) == []


def test_malformed_recovery_receipt_is_rejected_before_archive_move(tmp_handoff_paths):
    """Cold-review repair (HIGH): malformed receipts remain in the ready queue."""
    handoff = tmp_handoff_paths["handoff"]
    batch_id = "batch_20260826_120000_aaaa"
    _write_batch(handoff, batch_id, [(1, "clip.mp4", "x")])
    result = handoff_pickup.ingest_oldest(["creator-one"])
    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / batch_id
    source = handoff / batch_id
    archive.rename(source)
    receipt_path = source / handoff_pickup.RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text())
    del receipt["slots"][0]["sha256"]
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(handoff_pickup.HandoffPickupError, match="unsafe file metadata"):
        handoff_pickup.ingest_oldest(["creator-one"])

    assert source.is_dir()
    assert not archive.exists()
    assert result["batch_id"] == batch_id


def test_receipt_write_failure_rolls_back_staged_media(tmp_handoff_paths, monkeypatch):
    """Cold-review repair (MEDIUM): receipt failure preserves all-or-nothing staging."""
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(handoff, "batch_20260826_120000_aaaa", [(1, "clip.mp4", "x")])
    monkeypatch.setattr(
        handoff_pickup,
        "_atomic_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        handoff_pickup.ingest_oldest(["creator-one"])

    assert batch.is_dir()
    assert list(media.iterdir()) == []


def test_ack_rejects_roster_change_and_keeps_receipt_staged(tmp_handoff_paths):
    """Cold-review repair (HIGH): asynchronous roster changes cannot ACK old targets."""
    handoff = tmp_handoff_paths["handoff"]
    batch_id = "batch_20260826_120000_aaaa"
    _write_batch(handoff, batch_id, [(1, "clip.mp4", "x")])
    handoff_pickup.ingest_oldest(["creator-one"])

    with pytest.raises(handoff_pickup.HandoffPickupError, match="creator-one"):
        handoff_pickup.acknowledge(batch_id, ["secondary-one"])

    receipt_path = (
        handoff / handoff_pickup.ARCHIVE_DIRNAME / batch_id / handoff_pickup.RECEIPT_FILENAME
    )
    assert json.loads(receipt_path.read_text())["status"] == "staged"


def test_handoff_consumer_contains_no_recursive_delete():
    """Incident repair (CRITICAL): transition batches are never recursively purged."""
    source = (PROJECT_ROOT / "backend" / "handoff_pickup.py").read_text()
    assert "shutil.rmtree" not in source


# --- pull endpoint ----------------------------------------------------------


def test_pull_endpoint_returns_staged_slots(client, tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "hello"), (2, "clip_2.mp4", "world")],
    )

    resp = client.post("/api/pull-from-clipper")

    assert resp.status_code == 200
    body = resp.json()
    assert body["pulled"] is True
    assert body["batch_id"] == "batch_20260826_120000_aaaa"
    assert [s["slot"] for s in body["slots"]] == ["A", "B"]
    assert body["slots"][0]["topic"] == "hello"
    assert body["slots"][0]["style"] == "generic"
    assert "caption" not in body["slots"][0]
    assert body["source_archived"] is True
    assert body["replayed"] is False


def test_pull_endpoint_uses_current_account_ids_not_legacy_slots(
    client, tmp_handoff_paths, monkeypatch
):
    """Incident repair (CRITICAL): handoff positions map to active account identity."""
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "hello"), (2, "clip_2.mp4", "world")],
    )
    state = SimpleNamespace(active_account_ids=["creator-one", "secondary-one"])
    monkeypatch.setattr(main, "_account_context", lambda: ([], state, None, None))

    response = client.post("/api/pull-from-clipper")

    assert response.status_code == 200
    assert [slot["slot"] for slot in response.json()["slots"]] == [
        "creator-one",
        "secondary-one",
    ]


def test_pull_endpoint_reports_nothing_to_pull(client, tmp_handoff_paths):
    resp = client.post("/api/pull-from-clipper")
    assert resp.status_code == 200
    assert resp.json() == {"pulled": False, "reason": "No handoff batches to pull."}


def test_pull_endpoint_rejects_overflow(client, tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(i, f"clip_{i}.mp4", f"t{i}") for i in range(1, 5)],
    )
    resp = client.post("/api/pull-from-clipper")
    assert resp.status_code == 400
    assert "account" in resp.json()["detail"]


def test_pull_endpoint_is_registered():
    assert "/api/pull-from-clipper" in {route.path for route in main.app.routes}


def test_pull_ack_endpoint_marks_receipt_but_retains_archive(client, tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    batch_id = "batch_20260826_120000_aaaa"
    _write_batch(handoff, batch_id, [(1, "clip_1.mp4", "hello")])
    assert client.post("/api/pull-from-clipper").status_code == 200

    response = client.post(f"/api/pull-from-clipper/{batch_id}/ack")

    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    archive = handoff / handoff_pickup.ARCHIVE_DIRNAME / batch_id
    assert (archive / "clip_1.mp4").is_file()
    assert json.loads((archive / handoff_pickup.RECEIPT_FILENAME).read_text())["status"] == "applied"


# --- media route (preview) --------------------------------------------------


def test_media_route_serves_staged_file(client, tmp_media):
    (tmp_media / "A_batch_x_clip_1.mp4").write_bytes(b"video-bytes")
    resp = client.get("/api/media/A_batch_x_clip_1.mp4")
    assert resp.status_code == 200
    assert resp.content == b"video-bytes"


def test_media_route_404_for_missing(client, tmp_media):
    assert client.get("/api/media/missing.mp4").status_code == 404


def test_media_route_rejects_path_segments(client, tmp_media):
    # A name carrying a path segment can never escape MEDIA_DIR: it is rejected
    # (400) or simply does not match the single-segment route (404).
    resp = client.get("/api/media/sub%2Ffile.mp4")
    assert resp.status_code in (400, 404)


# --- frontend wiring --------------------------------------------------------


def test_pull_frontend_captures_frame_and_uses_media_route():
    """Source-level: the pull flow shows a server preview, captures the frame the
    caption AI sees, and routes captions through the existing generateAll path."""
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text()
    assert "captureThumbnailFromUrl" in html
    assert "/api/media/" in html
    assert "await generateAll();" in html


def test_pull_frontend_preflights_before_mutation_and_acknowledges_after_apply():
    """Incident repair (CRITICAL): missing DOM cannot strand a consumed batch."""
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text()
    pull = html[html.index("async function pullFromClipper()") : html.index("function assertPulledTargets")]
    assert pull.index("assertPulledTargets(data);") < pull.index("applyPulledSlot(entry)")
    assert pull.index("await generateAll();") < pull.index("/ack`")
    assert "Source remains archived" in pull
    assert "unacknowledged batches are archived and replayed" in pull
    preflight = html[html.index("function assertPulledTargets") : html.index("function applyPulledSlot")]
    assert "slotElOpt('thumbRow', entry.slot)" in preflight
    assert "slotElOpt('thumbChip', entry.slot)" in preflight


def test_account_switch_removes_every_stale_slot_not_only_drafts():
    """Incident repair (HIGH): removed accounts cannot leave ghost DOM targets."""
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text()
    assert "for (const id of removed) {" in html
    assert "delete state.slots[id];" in html
    assert "for (const id of drafts) delete state.slots[id];" not in html
