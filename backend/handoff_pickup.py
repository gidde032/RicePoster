"""Ingest RiceClipper handoff batches into a pending RicePoster run.

Consumer side of RiceClipper's handoff contract. RiceClipper writes finished
clips into a shared handoff directory as `batch_<ts>/` folders, each containing
`clip_<position>.mp4` files plus a `manifest.json` written **last** (the
"batch complete" signal). This module turns the **oldest** ready batch into
staged media for the existing review -> Post All flow.

SAFETY: this module NEVER posts and NEVER schedules (CLAUDE.md rule #1). It only
copies media into `MEDIA_DIR`. It does **not** generate captions — captioning
happens in the browser afterward, through the same `/api/generate-caption` path
manual uploads use, so a pulled clip's caption is grounded on a real frame
(captured client-side from the staged video) exactly like manual, rather than on
the transcript alone. Each slot carries `topic` (the transcript) and a default
`style` for that step.

Config (`HANDOFF_DIR`, `MEDIA_DIR`, `SLOT_IDS`, `CLIPPER_INGEST_STYLE`) is
imported as module-level names and read at call time, so tests redirect them by
patching this module's attributes (see conftest `tmp_handoff_paths`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from backend.config import CLIPPER_INGEST_STYLE, HANDOFF_DIR, MEDIA_DIR, SLOT_IDS
from backend.logging_setup import get_logger

_log = get_logger("handoff")

SCHEMA_VERSION = 1


class HandoffPickupError(RuntimeError):
    """A handoff batch could not be ingested."""


class NoBatchAvailable(HandoffPickupError):
    """No ready batch was found in the handoff directory."""


def _ready_batches(root: Path) -> list[Path]:
    """Return batch dirs that carry a manifest, oldest first.

    A directory without `manifest.json` is a half-written (or already-consumed)
    batch and is skipped. `batch_<YYYYmmdd_HHMMSS>_<rand>` sorts lexically in
    timestamp order, so the name sort is oldest-first.
    """
    if not root.is_dir():
        return []
    batches = [
        p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()
    ]
    return sorted(batches, key=lambda p: p.name)


def _read_manifest(batch_dir: Path) -> dict:
    try:
        manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffPickupError(f"unreadable manifest in {batch_dir.name}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise HandoffPickupError(
            f"unsupported manifest schema_version in {batch_dir.name}: "
            f"{manifest.get('schema_version')!r} (expected {SCHEMA_VERSION})"
        )
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise HandoffPickupError(f"manifest in {batch_dir.name} lists no clips")
    return manifest


def ingest_oldest() -> dict:
    """Stage the oldest ready batch and return its slot assignments.

    Copies each clip into `MEDIA_DIR` as `{slot}_{batch}_{file}` and — only once
    every clip has staged successfully — deletes the batch directory. On any
    failure the whole batch is retained (staged media is rolled back) so it can
    be re-pulled without re-rendering. Captioning is left to the browser step.

    Raises ``NoBatchAvailable`` when nothing is ready and ``HandoffPickupError``
    for a malformed batch or a batch with more clips than configured slots.
    """
    batches = _ready_batches(HANDOFF_DIR)
    if not batches:
        raise NoBatchAvailable("no handoff batches to pull")

    batch_dir = batches[0]
    manifest = _read_manifest(batch_dir)
    batch_id = manifest.get("batch_id", batch_dir.name)
    clips = sorted(manifest["clips"], key=lambda c: c.get("position", 0))

    if len(clips) > len(SLOT_IDS):
        raise HandoffPickupError(
            f"batch {batch_id} has {len(clips)} clips but only {len(SLOT_IDS)} "
            f"slot(s) ({', '.join(SLOT_IDS)}) are configured — reduce the batch "
            f"or add slots."
        )

    staged: list[Path] = []
    slots: list[dict] = []
    try:
        for clip, slot in zip(clips, SLOT_IDS):
            src = batch_dir / Path(clip["file"]).name
            if not src.is_file():
                raise HandoffPickupError(
                    f"clip file missing in {batch_id}: {clip.get('file')!r}"
                )
            filename = f"{slot}_{batch_id}_{src.name}"
            dest = MEDIA_DIR / filename
            shutil.copyfile(src, dest)
            staged.append(dest)
            slots.append(
                {
                    "slot": slot,
                    "filename": filename,
                    "media_type": "video",
                    "topic": clip.get("transcript", ""),
                    "header": clip.get("header", ""),
                    "style": CLIPPER_INGEST_STYLE,
                }
            )
    except Exception:
        # Staging is all-or-nothing: roll back staged media and keep the batch.
        for dest in staged:
            dest.unlink(missing_ok=True)
        raise

    # Every clip staged — the batch dir is now redundant (RicePoster holds its
    # own media copy), so purge it per the contract's purge policy.
    shutil.rmtree(batch_dir, ignore_errors=True)
    _log.info("[handoff] pulled batch %s into %d slot(s)", batch_id, len(slots))
    return {"batch_id": batch_id, "slots": slots}
