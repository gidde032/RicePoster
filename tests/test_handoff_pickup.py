"""Tests for the RiceClipper handoff pickup (Pull from Clipper).

Ingest only *stages* media now — captioning happens in the browser afterward,
through the existing `/api/generate-caption` path (grounded on a frame captured
from the staged clip), so there is no server-side caption call here. The autouse
`tmp_handoff_paths` fixture redirects the handoff root and media dir to temp
paths so ingest never scans/deletes the maintainer's real handoff or writes into
the real `media/`.
"""

import json

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


def test_ingest_stages_media_then_purges(tmp_handoff_paths):
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(1, "clip_1.mp4", "first transcript"), (2, "clip_2.mp4", "second transcript")],
    )

    result = handoff_pickup.ingest_oldest()

    assert result["batch_id"] == "batch_20260826_120000_aaaa"
    slots = result["slots"]
    assert [s["slot"] for s in slots] == ["A", "B"]  # positional assignment
    assert slots[0]["filename"] == "A_batch_20260826_120000_aaaa_clip_1.mp4"
    assert slots[0]["topic"] == "first transcript"  # transcript -> caption topic
    assert slots[0]["style"] == "generic"
    assert slots[0]["media_type"] == "video"
    assert "caption" not in slots[0]  # captioning happens in the browser now

    assert (media / "A_batch_20260826_120000_aaaa_clip_1.mp4").is_file()
    assert (media / "B_batch_20260826_120000_aaaa_clip_2.mp4").is_file()
    assert not batch.exists()  # purged after full success


def test_ingest_selects_oldest_and_leaves_newer(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    old = _write_batch(handoff, "batch_20260101_000000_aaaa", [(1, "clip_1.mp4", "old")])
    new = _write_batch(handoff, "batch_20260102_000000_bbbb", [(1, "clip_1.mp4", "new")])

    result = handoff_pickup.ingest_oldest()

    assert result["batch_id"] == "batch_20260101_000000_aaaa"
    assert not old.exists()  # oldest consumed
    assert new.exists()  # newer retained for the next pull


def test_ingest_rejects_more_clips_than_slots(tmp_handoff_paths):
    handoff, media = tmp_handoff_paths["handoff"], tmp_handoff_paths["media"]
    # Default SLOT_IDS is A,B,C (3); four clips must be rejected.
    batch = _write_batch(
        handoff,
        "batch_20260826_120000_aaaa",
        [(i, f"clip_{i}.mp4", f"t{i}") for i in range(1, 5)],
    )

    with pytest.raises(handoff_pickup.HandoffPickupError, match="only 3 slot"):
        handoff_pickup.ingest_oldest()

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
        handoff_pickup.ingest_oldest()

    assert batch.exists()  # whole batch retained for re-pull
    assert list(media.iterdir()) == []  # first clip's staged media rolled back


def test_ingest_rejects_unsupported_schema(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    _write_batch(handoff, "batch_x_aaaa", [(1, "clip_1.mp4", "t")], schema_version=999)
    with pytest.raises(handoff_pickup.HandoffPickupError, match="schema_version"):
        handoff_pickup.ingest_oldest()


def test_ingest_raises_when_no_batches(tmp_handoff_paths):
    with pytest.raises(handoff_pickup.NoBatchAvailable):
        handoff_pickup.ingest_oldest()


def test_half_written_batch_without_manifest_is_ignored(tmp_handoff_paths):
    handoff = tmp_handoff_paths["handoff"]
    # A dir with clips but no manifest.json is mid-write; it must be skipped.
    partial = handoff / "batch_20260826_120000_aaaa"
    partial.mkdir()
    (partial / "clip_1.mp4").write_bytes(b"x")
    with pytest.raises(handoff_pickup.NoBatchAvailable):
        handoff_pickup.ingest_oldest()


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
    assert "slot" in resp.json()["detail"]


def test_pull_endpoint_is_registered():
    assert "/api/pull-from-clipper" in {route.path for route in main.app.routes}


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
