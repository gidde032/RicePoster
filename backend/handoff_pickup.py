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

Config (`HANDOFF_DIR`, `MEDIA_DIR`, `CLIPPER_INGEST_STYLE`) is imported as
module-level names and read at call time, so tests redirect them by patching
this module's attributes (see conftest `tmp_handoff_paths`). Account targets
are supplied explicitly from the current saved roster and frozen in a durable
receipt; legacy A/B/C slot constants never choose a handoff destination.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from backend.config import CLIPPER_INGEST_STYLE, HANDOFF_DIR, MEDIA_DIR
from backend.logging_setup import get_logger

_log = get_logger("handoff")

SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
ARCHIVE_DIRNAME = ".riceposter-consumed"
RECEIPT_FILENAME = "riceposter-receipt.json"
_BATCH_ID_RE = re.compile(r"^batch_[A-Za-z0-9_-]+$")


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
    batches = []
    for path in root.iterdir():
        manifest = path / "manifest.json"
        if (
            path.is_dir()
            and not path.is_symlink()
            and manifest.is_file()
            and not manifest.is_symlink()
        ):
            batches.append(path)
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
    batch_id = manifest.get("batch_id")
    if (
        not isinstance(batch_id, str)
        or not _BATCH_ID_RE.fullmatch(batch_id)
        or batch_id != batch_dir.name
    ):
        raise HandoffPickupError(
            f"manifest batch_id must exactly match directory {batch_dir.name!r}"
        )
    clips = manifest.get("clips")
    if not isinstance(clips, list) or not clips:
        raise HandoffPickupError(f"manifest in {batch_dir.name} lists no clips")
    positions: set[int] = set()
    files: set[str] = set()
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            raise HandoffPickupError(
                f"manifest clip {index} in {batch_dir.name} must be an object"
            )
        position = clip.get("position")
        filename = clip.get("file")
        if not isinstance(position, int) or isinstance(position, bool) or position < 1:
            raise HandoffPickupError(
                f"manifest clip {index} in {batch_dir.name} has an invalid position"
            )
        if position in positions:
            raise HandoffPickupError(
                f"manifest in {batch_dir.name} has duplicate clip position {position}"
            )
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or filename in files
        ):
            raise HandoffPickupError(
                f"manifest clip {index} in {batch_dir.name} has an unsafe or duplicate file"
            )
        if not isinstance(clip.get("transcript", ""), str) or not isinstance(
            clip.get("header", ""), str
        ):
            raise HandoffPickupError(
                f"manifest clip {index} in {batch_dir.name} has invalid text metadata"
            )
        positions.add(position)
        files.add(filename)
    if positions != set(range(1, len(clips) + 1)):
        raise HandoffPickupError(
            f"manifest in {batch_dir.name} must use contiguous positions 1..{len(clips)}"
        )
    return manifest


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomic(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f".{destination.name}.handoff.tmp")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, destination)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _validate_targets(target_ids: list[str]) -> list[str]:
    if not isinstance(target_ids, list) or not target_ids:
        raise HandoffPickupError("no active account targets are available for this batch")
    if len(target_ids) != len(set(target_ids)):
        raise HandoffPickupError("active account targets contain a duplicate id")
    for account_id in target_ids:
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise HandoffPickupError(f"unsafe active account target: {account_id!r}")
    return target_ids


def _archive_root() -> Path:
    return HANDOFF_DIR / ARCHIVE_DIRNAME


def _checked_archive_root(*, create: bool = False) -> Path:
    root = _archive_root()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise HandoffPickupError("handoff archive root must be a real directory")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _load_receipt(path: Path) -> dict:
    if path.is_symlink() or path.parent.is_symlink():
        raise HandoffPickupError(f"RicePoster receipt path is a symlink: {path}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffPickupError(f"unreadable RicePoster receipt at {path}: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise HandoffPickupError(f"unsupported RicePoster receipt at {path}")
    if not isinstance(receipt.get("slots"), list) or not receipt["slots"]:
        raise HandoffPickupError(f"RicePoster receipt at {path} has no staged slots")
    return receipt


def _validate_receipt(receipt: dict, batch_dir: Path) -> None:
    if batch_dir.is_symlink() or not batch_dir.is_dir():
        raise HandoffPickupError("handoff receipt batch must be a real directory")
    if receipt.get("batch_id") != batch_dir.name:
        raise HandoffPickupError(
            f"receipt batch identity does not match {batch_dir.name}"
        )
    frozen = receipt.get("target_account_ids")
    _validate_targets(frozen)
    slots = receipt["slots"]
    if len(slots) != len(frozen):
        raise HandoffPickupError(
            f"receipt for {batch_dir.name} does not match its frozen target count"
        )
    for index, (slot, account_id) in enumerate(zip(slots, frozen), 1):
        if not isinstance(slot, dict) or slot.get("slot") != account_id:
            raise HandoffPickupError(
                f"receipt slot {index} for {batch_dir.name} does not match its frozen target"
            )
        source_name = slot.get("source_file")
        staged_name = slot.get("filename")
        digest = slot.get("sha256")
        if (
            not isinstance(source_name, str)
            or not source_name
            or Path(source_name).name != source_name
            or not isinstance(staged_name, str)
            or not staged_name
            or Path(staged_name).name != staged_name
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise HandoffPickupError(
                f"receipt slot {index} for {batch_dir.name} has unsafe file metadata"
            )
        source = batch_dir / source_name
        if not source.is_file() or source.is_symlink():
            raise HandoffPickupError(
                f"receipt source missing or unsupported for {batch_dir.name}: {source_name}"
            )
        if _sha256(source) != digest:
            raise HandoffPickupError(
                f"receipt source hash mismatch for {batch_dir.name}: {source_name}"
            )


def _receipt_result(receipt: dict, *, replayed: bool) -> dict:
    return {
        "batch_id": receipt["batch_id"],
        "slots": [
            {key: value for key, value in slot.items() if key != "source_file" and key != "sha256"}
            for slot in receipt["slots"]
        ],
        "receipt_status": receipt.get("status", "staged"),
        "replayed": replayed,
        "source_archived": True,
    }


def _restore_staged_media(archive_dir: Path, receipt: dict) -> None:
    _validate_receipt(receipt, archive_dir)
    for slot in receipt["slots"]:
        source = archive_dir / Path(slot["source_file"]).name
        destination = MEDIA_DIR / Path(slot["filename"]).name
        if not source.is_file():
            raise HandoffPickupError(
                f"archived clip missing for batch {receipt['batch_id']}: {source.name}"
            )
        if destination.is_file():
            if _sha256(destination) != slot["sha256"]:
                raise HandoffPickupError(
                    f"staged media changed for batch {receipt['batch_id']}: {destination.name}"
                )
            continue
        _copy_atomic(source, destination)
        if _sha256(destination) != slot["sha256"]:
            destination.unlink(missing_ok=True)
            raise HandoffPickupError(
                f"restored media failed verification for batch {receipt['batch_id']}"
            )


def _oldest_unacknowledged() -> tuple[Path, dict] | None:
    root = _checked_archive_root()
    if not root.is_dir():
        return None
    for archive_dir in sorted(
        path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    ):
        receipt_path = archive_dir / RECEIPT_FILENAME
        if not receipt_path.is_file():
            continue
        receipt = _load_receipt(receipt_path)
        if receipt.get("status") != "applied":
            _validate_receipt(receipt, archive_dir)
            return archive_dir, receipt
    return None


def _require_same_targets(receipt: dict, target_ids: list[str]) -> None:
    frozen = receipt.get("target_account_ids")
    if not isinstance(frozen, list) or frozen != target_ids[: len(frozen)]:
        raise HandoffPickupError(
            f"batch {receipt.get('batch_id', '(unknown)')} is staged for "
            f"{', '.join(frozen or [])}; activate that ordered roster prefix to retry"
        )


def ingest_oldest(target_ids: list[str]) -> dict:
    """Stage the oldest ready batch and return its slot assignments.

    Copies each clip into `MEDIA_DIR` as `{account_id}_{batch}_{file}` and,
    only after every clip and the receipt are durable, atomically moves the
    source batch under `.riceposter-consumed/`. Until the browser acknowledges
    application, the same frozen assignments are replayed and missing staged
    files are restored from that archive. The archive is retained after ACK.
    Captioning is left to the browser step.

    Raises ``NoBatchAvailable`` when nothing is ready and ``HandoffPickupError``
    for a malformed batch or a batch with more clips than active accounts.
    """
    target_ids = _validate_targets(target_ids)

    pending = _oldest_unacknowledged()
    if pending is not None:
        archive_dir, receipt = pending
        _require_same_targets(receipt, target_ids)
        _restore_staged_media(archive_dir, receipt)
        return _receipt_result(receipt, replayed=True)

    batches = _ready_batches(HANDOFF_DIR)
    if not batches:
        raise NoBatchAvailable("no handoff batches to pull")

    batch_dir = batches[0]
    manifest = _read_manifest(batch_dir)
    batch_id = batch_dir.name
    clips = sorted(manifest["clips"], key=lambda c: c.get("position", 0))

    if len(clips) > len(target_ids):
        raise HandoffPickupError(
            f"batch {batch_id} has {len(clips)} clips but the active roster has only "
            f"{len(target_ids)} account(s) ({', '.join(target_ids)})"
        )

    receipt_path = batch_dir / RECEIPT_FILENAME
    if receipt_path.is_file():
        receipt = _load_receipt(receipt_path)
        _validate_receipt(receipt, batch_dir)
        _require_same_targets(receipt, target_ids)
        archive_root = _checked_archive_root(create=True)
        archive_dir = archive_root / batch_dir.name
        if archive_dir.exists() or archive_dir.is_symlink():
            raise HandoffPickupError(f"archive already exists for batch {batch_id}")
        os.replace(batch_dir, archive_dir)
        _restore_staged_media(archive_dir, receipt)
        return _receipt_result(receipt, replayed=True)

    staged: list[Path] = []
    slots: list[dict] = []
    try:
        for clip, slot in zip(clips, target_ids):
            src = batch_dir / clip["file"]
            if not src.is_file() or src.is_symlink():
                raise HandoffPickupError(
                    f"clip file missing or unsupported in {batch_id}: {clip.get('file')!r}"
                )
            filename = f"{slot}_{batch_id}_{src.name}"
            dest = MEDIA_DIR / filename
            _copy_atomic(src, dest)
            staged.append(dest)
            slots.append(
                {
                    "slot": slot,
                    "filename": filename,
                    "media_type": "video",
                    "topic": clip.get("transcript", ""),
                    "header": clip.get("header", ""),
                    "style": CLIPPER_INGEST_STYLE,
                    "source_file": src.name,
                    "sha256": _sha256(dest),
                }
            )
    except Exception:
        # Staging is all-or-nothing: roll back staged media and keep the batch.
        for dest in staged:
            dest.unlink(missing_ok=True)
        raise

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "status": "staged",
        "staged_at": _now(),
        "target_account_ids": target_ids[: len(clips)],
        "manifest": manifest,
        "slots": slots,
    }
    try:
        _atomic_json(receipt_path, receipt)
    except Exception:
        for dest in staged:
            dest.unlink(missing_ok=True)
        raise
    archive_root = _checked_archive_root(create=True)
    archive_dir = archive_root / batch_dir.name
    if archive_dir.exists() or archive_dir.is_symlink():
        raise HandoffPickupError(f"archive already exists for batch {batch_id}")
    os.replace(batch_dir, archive_dir)
    _log.info("[handoff] staged and archived batch %s into %d account(s)", batch_id, len(slots))
    return _receipt_result(receipt, replayed=False)


def acknowledge(batch_id: str, target_ids: list[str]) -> dict:
    """Mark a staged receipt applied; archived source media remains recoverable."""
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise HandoffPickupError("invalid handoff batch id")
    target_ids = _validate_targets(target_ids)
    archive_dir = _checked_archive_root() / batch_id
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise HandoffPickupError(f"no safe archived RicePoster receipt for batch {batch_id}")
    receipt_path = archive_dir / RECEIPT_FILENAME
    if not receipt_path.is_file():
        raise HandoffPickupError(f"no archived RicePoster receipt for batch {batch_id}")
    receipt = _load_receipt(receipt_path)
    _validate_receipt(receipt, archive_dir)
    _require_same_targets(receipt, target_ids)
    if receipt.get("status") != "applied":
        receipt["status"] = "applied"
        receipt["applied_at"] = _now()
        _atomic_json(receipt_path, receipt)
    return {"batch_id": batch_id, "status": "applied", "source_archived": True}
