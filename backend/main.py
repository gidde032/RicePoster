import asyncio
import base64
import binascii
import datetime
import json
import os
import shutil
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from backend.config import (
    get_accounts, env_bool, FRONTEND_DIR, HISTORY_FILE, MEDIA_DIR, MOCK_MODE,
    POST_MODE, HEADLESS, SLOT_IDS, check_startup_config,
)
from backend.models import (
    CaptionRequest, PostRequest, PostResult, RescheduleRequest, ScheduleRequest,
    MAX_CAPTION_LENGTH,
)
from backend.captions import generate_caption, load_styles, DEFAULT_STYLE
from backend.poster import post_all as post_all_api
from backend.poster_browser import post_all as post_all_browser
from backend.notifier import get_notifier, send_safe
from backend import handoff_pickup, run_guard
from backend.logging_setup import get_logger

_log = get_logger("main")

SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)


def _scheduler_died(task: asyncio.Task):
    """Backstop for the scheduler task ending for a reason its own loop
    handler did not cover (Phase 2 audit, finding #4).

    Without this the task can end silently: the server stays up, the queue
    panel keeps showing pending batches, and nothing ever fires them. No
    auto-restart — whatever killed it is likely to kill it again, and a batch
    in flight when it died is in an unknown state (maintainer decision
    2026-07-27). Restart the server after reading the cause.
    """
    if task.cancelled():
        return                      # normal shutdown
    exc = task.exception()
    if exc is None:
        return                      # returned cleanly; the loop never should
    _log.error(f"[scheduler] TASK DIED: {type(exc).__name__}: {exc} — no scheduled "
          f"batch will fire until the server is restarted")
    try:
        asyncio.get_running_loop().create_task(send_safe(
            get_notifier(),
            title="RicePoster: scheduler stopped",
            body=f"The scheduler task died ({type(exc).__name__}). No "
                 f"scheduled batch will fire until you restart the server.",
            priority="high",
        ))
    except Exception as e:
        # A raising done-callback is swallowed by asyncio, which would lose
        # the console line above too. The alert path must never hide the death.
        _log.error(f"[scheduler] could not send death notification: {type(e).__name__}")


def _reconcile_media_at_startup():
    """Remove orphaned queue-media snapshots at boot (#4).

    Deliberately runs *before* the scheduler starts, while nothing can be
    writing a new snapshot, and never raises: an unreadable queue_media/ must
    not stop the server from coming up and firing the day's batches. It only
    ever deletes snapshots it can prove belong to no batch — see
    backend/queue.py for the classification rules.

    The startup sweep that reclassifies stale 'running' batches lives in the
    scheduler and runs after this. That ordering is harmless: an interrupted
    batch is still a queue entry either way, so its media classifies as active
    under both statuses.
    """
    try:
        from backend.queue import reconcile_queue_media
        reconcile_queue_media()
    except Exception as e:
        _log.warning(f"[queue] WARNING: media reconciliation skipped "
              f"({type(e).__name__}: {e})")


@asynccontextmanager
async def lifespan(app):
    for problem in check_startup_config():
        _log.warning(f"[config] WARNING: {problem}")
    _reconcile_media_at_startup()
    task = None
    if SCHEDULER_ENABLED:
        from backend.scheduler import scheduler_loop
        task = asyncio.create_task(scheduler_loop())
        task.add_done_callback(_scheduler_died)
    yield
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="RicePoster", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """Flatten Pydantic's list-of-dicts 422 body into a single string `detail`.

    The UI renders `err.detail` straight into an error message, so FastAPI's
    default list body would surface as "[object Object]" once the endpoints
    moved onto request models (#33). Field names are preserved so the message
    still names what was wrong.
    """
    parts = []
    for err in exc.errors():
        # loc[0] is the request part ("body", "query", "path"); drop only that
        # leading element, so a field genuinely named "body" survives.
        location = err.get("loc", ())
        if location and location[0] in ("body", "query", "path"):
            location = location[1:]
        loc = ".".join(str(p) for p in location)
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=422,
        content={"detail": "; ".join(parts) or "Invalid request."},
    )


# Serve frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def serve_ui():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/accounts")
async def list_accounts():
    """Return account slot names for the UI."""
    accounts = get_accounts()
    # Check session status if in browser mode
    session_status = {}
    if POST_MODE == "browser":
        from backend.session_manager import session_exists
        for a in accounts:
            session_status[a.slot] = {
                "instagram": session_exists("instagram", a.slot),
                "tiktok": session_exists("tiktok", a.slot),
            }

    return {
        "post_mode": POST_MODE,
        "mock_mode": MOCK_MODE,
        "headless": HEADLESS,
        "accounts": [
            {"slot": a.slot, "name": a.display_name}
            for a in accounts
        ],
        "sessions": session_status,
        "caption_styles": [
            {"name": s.name, "display_name": s.display_name}
            for s in load_styles().values()
        ],
        "default_caption_style": DEFAULT_STYLE,
        # Served so the frontend's char counter shares the backend's single
        # source of truth rather than carrying a divergent copy (#53).
        "caption_limit": MAX_CAPTION_LENGTH,
    }


@app.post("/api/upload/{slot}")
async def upload_media(slot: str, file: UploadFile = File(...)):
    """Upload a media file for a given account slot. Returns filename and detected type."""
    if slot not in SLOT_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid slot '{slot}'. Configured slots: {', '.join(SLOT_IDS)}.",
        )

    # Save to media dir with slot prefix; basename only so path segments
    # in a client-supplied filename can't escape MEDIA_DIR
    safe_name = Path(file.filename).name
    filename = f"{slot}_{safe_name}"
    dest = MEDIA_DIR / filename

    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Detect media type from extension
    mime = mimetypes.guess_type(file.filename)[0] or ""
    media_type = "video" if mime.startswith("video") else "image"

    return {
        "filename": filename,
        "media_type": media_type,
        "size": dest.stat().st_size,
    }


@app.get("/api/media-info")
async def media_info():
    """Size of the uploaded-media library, for the UI's cleanup control."""
    files = [f for f in MEDIA_DIR.iterdir() if f.is_file() and f.name != ".gitkeep"]
    return {
        "file_count": len(files),
        "total_bytes": sum(f.stat().st_size for f in files),
    }


@app.post("/api/media/clear")
async def clear_media():
    """Delete all uploaded media copies. Originals live elsewhere on disk —
    only the slot-prefixed copies under media/ are removed."""
    if run_guard.is_running():
        raise HTTPException(
            status_code=409,
            detail="A post run is in progress — media can't be cleared right now.",
        )
    removed = 0
    for f in MEDIA_DIR.iterdir():
        if f.is_file() and f.name != ".gitkeep":
            f.unlink()
            removed += 1
    return {"removed": removed}


# A 512px-long-edge JPEG at q0.8 is ~30–80 KB; anything near this cap means
# the client sent something other than the downscaled thumbnail we expect.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024


def clean_thumbnail(thumbnail: str) -> str:
    """Normalize a client-sent thumbnail to raw base64 JPEG data: strip a
    data-URL prefix if present, verify it decodes, enforce the size cap.
    Empty input passes through (caption generation stays text-only)."""
    thumbnail = thumbnail.strip()
    if not thumbnail:
        return ""
    if thumbnail.startswith("data:"):
        if ";base64," not in thumbnail:
            raise ValueError("Thumbnail data-URL is not base64-encoded.")
        thumbnail = thumbnail.split(";base64,", 1)[1]
    try:
        decoded = base64.b64decode(thumbnail, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Thumbnail is not valid base64 data.")
    if len(decoded) > MAX_THUMBNAIL_BYTES:
        raise ValueError(
            f"Thumbnail is too large ({len(decoded)} bytes; max {MAX_THUMBNAIL_BYTES})."
        )
    return thumbnail


@app.post("/api/generate-caption")
async def generate_caption_endpoint(data: Annotated[CaptionRequest, Form()]):
    """Generate a single caption in the given style. For regeneration,
    avoid_caption is the previous caption to differ from and feedback is
    optional user steering ("shorter", "lean into the joke"). `thumbnail`
    is an optional base64 JPEG frame of the media (data-URL prefix ok)."""
    try:
        thumbnail_b64 = clean_thumbnail(data.thumbnail)
        caption = await generate_caption(
            data.media_type,
            data.topic,
            DEFAULT_STYLE if data.style is None else data.style,
            data.avoid_caption,
            data.feedback,
            thumbnail_b64=thumbnail_b64,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"caption": caption}


@app.post("/api/pull-from-clipper")
async def pull_from_clipper():
    """Ingest the oldest RiceClipper handoff batch into a pending run.

    Stages the batch's media and generates a caption per clip for the normal
    review -> Post All flow. This endpoint NEVER posts and NEVER schedules
    (CLAUDE.md safety rule) — it only stages files and generates captions.
    """
    try:
        result = await handoff_pickup.ingest_oldest()
    except handoff_pickup.NoBatchAvailable:
        return {"pulled": False, "reason": "No handoff batches to pull."}
    except handoff_pickup.HandoffPickupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"pulled": True, **result}


class PostProgress:
    """Live progress of the current (or most recent) posting run, polled by the
    UI. Slot×platform granularity only — no callbacks inside Playwright flows.

    A small object rather than a module-level dict so the two lifecycle rules
    are enforced in one place instead of being remembered across a dict literal
    and a field-by-field reset (#34):

    - `start()` clears the previous run's events.
    - `finish()` deliberately KEEPS them, because the UI still renders the most
      recent run's per-slot results after the run ends.

    Single uvicorn worker on a single event loop, so no locking is needed. That
    is a deliberate constraint, not an oversight — see #34.
    """

    def __init__(self):
        self.active: bool = False
        self.current: dict | None = None
        self.events: list[dict] = []
        self.waiting: dict | None = None

    def start(self):
        """Begin a new run, discarding the previous run's events."""
        self.active = True
        self.current = None
        self.events = []
        self.waiting = None

    def finish(self):
        """End the run, retaining events for the UI's most-recent-run view.

        A run that ends mid-gap must not leave a stale countdown on screen, so
        `waiting` is always cleared.
        """
        self.active = False
        self.current = None
        self.waiting = None

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "current": self.current,
            "events": self.events,
            "waiting": self.waiting,
        }


_post_progress = PostProgress()


def _record_progress(slot: str, platform: str, status: str, detail: str = ""):
    # "waiting" is the inter-slot pacing gap (F4), not a post outcome. It is
    # held in its own field and deliberately NOT appended to events: the UI
    # renders events as per-platform results, so a run-level event there
    # would show up as a bogus failure mark against the slot.
    if status == "waiting":
        try:
            seconds = float(detail)
        except (TypeError, ValueError):
            seconds = 0.0
        _post_progress.waiting = {
            "slot": slot, "seconds": seconds, "at": time.time(),
        }
        return

    _post_progress.waiting = None
    if status == "started":
        _post_progress.current = {"slot": slot, "platform": platform}
    else:
        _post_progress.current = None
    _post_progress.events.append(
        {"slot": slot, "platform": platform, "status": status, "detail": detail}
    )


@app.get("/api/post-progress")
async def post_progress():
    """Progress of the active (or most recent) posting run.

    The remaining wait is computed server-side so the countdown cannot drift
    with the browser's clock.
    """
    payload = _post_progress.to_dict()
    w = payload.get("waiting")
    if w:
        remaining = int(round(w["seconds"] - (time.time() - w["at"])))
        payload["waiting"] = {"slot": w["slot"], "remaining": max(0, remaining)}
    return payload


# Append-only record of every posting run (gitignored — contains captions).
# Imported, not derived: conftest's autouse `tmp_history_file` fixture patches
# this module-level name, and the suite silently appended 354 rows to the real
# file before that redirect existed (tests/test_history_isolation.py).


def _append_history(slots: list[dict], results, headless_used: bool):
    """Record one line per slot-result. History must never break a run."""
    try:
        with open(HISTORY_FILE, "a") as f:
            for r in results:
                slot_info = next((s for s in slots if s["slot"] == r.slot), {})
                f.write(json.dumps({
                    # UTC, timezone-aware, to match scheduler.py's rows — the
                    # two writers used different clocks and the UI rendered
                    # both raw (Phase 2 audit, finding #2). The frontend
                    # converts for display; pre-existing naive rows keep
                    # rendering correctly because JS parses an offset-less
                    # timestamp as local time, which is what they were.
                    "ts": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(timespec="seconds"),
                    "slot": r.slot,
                    "file": Path(slot_info["media_path"]).name if slot_info else "",
                    "caption": slot_info.get("caption", ""),
                    "post_mode": POST_MODE,
                    "headless": headless_used,
                    "ig_post_id": r.ig_post_id,
                    "tt_post_id": r.tt_post_id,
                    "errors": r.errors,
                }) + "\n")
    except Exception as e:
        _log.warning(f"[history] Warning: failed to record run history: {e}")


@app.get("/api/history")
async def get_history(limit: int = 50):
    """Most recent post-run records, newest first."""
    if not HISTORY_FILE.exists():
        return {"entries": []}
    entries = []
    for line in HISTORY_FILE.read_text().strip().splitlines()[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    entries.reverse()
    return {"entries": entries}


@app.post("/api/post")
async def post_all_endpoint(request: PostRequest) -> list[PostResult]:
    """Post all requested slots to both platforms. `headless` overrides
    the env default for this run only."""
    if not run_guard.try_acquire():
        raise HTTPException(
            status_code=409,
            detail="A post run is already in progress — wait for it to finish before posting again.",
        )
    effective_headless = HEADLESS if request.headless is None else request.headless
    _post_progress.start()
    try:
        return await _run_post(request, effective_headless)
    finally:
        run_guard.release()
        _post_progress.finish()


async def _run_post(request: PostRequest, effective_headless: bool) -> list[PostResult]:
    # The old fixed-field API made duplicate slots impossible; a JSON list
    # doesn't, and a duplicate would post twice to the same real account.
    requested_ids = [s.slot for s in request.slots]
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(
            status_code=400,
            detail="Duplicate slot in post request — each slot may appear at most once.",
        )
    slots = []
    for req_slot in request.slots:
        if req_slot.slot not in SLOT_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown slot '{req_slot.slot}'. Configured slots: {', '.join(SLOT_IDS)}.",
            )
        if req_slot.filename and req_slot.caption:
            media_path = (MEDIA_DIR / req_slot.filename).resolve()
            if not media_path.is_relative_to(MEDIA_DIR.resolve()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Slot {req_slot.slot}: filename '{req_slot.filename}' resolves outside the media directory.",
                )
            if not media_path.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"Slot {req_slot.slot}: media file '{req_slot.filename}' not found — upload it first.",
                )
            slots.append({
                "slot": req_slot.slot,
                "media_path": media_path,
                "caption": req_slot.caption,
                "media_type": req_slot.media_type or "image",
            })

    if POST_MODE == "browser":
        results = await post_all_browser(
            slots,
            headless=effective_headless,
            progress_cb=_record_progress,
            notifier=get_notifier(),
        )
    else:
        results = await post_all_api(slots)

    _append_history(slots, results, effective_headless)
    return results


# ---------------------------------------------------------------------------
# Scheduling / queue endpoints (DESIGN-scheduling.md §5c)
# ---------------------------------------------------------------------------

from backend.queue import (
    SlotBatch, load_queue, add_batch, cancel_batch, dismiss_batch,
    update_fire_time, classify_snapshots, remove_snapshot,
)


@app.post("/api/queue", status_code=201)
async def schedule_batch(request: ScheduleRequest):
    """Schedule a batch for future posting.

    `fire_time` is validated by ScheduleRequest, which shares one validator with
    the reschedule endpoint (#33).
    """
    requested_ids = [s.slot for s in request.slots]
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(status_code=400, detail="Duplicate slot in schedule request.")

    slot_batches = []
    for req_slot in request.slots:
        if req_slot.slot not in SLOT_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown slot '{req_slot.slot}'. Configured slots: {', '.join(SLOT_IDS)}.",
            )
        if not req_slot.filename or not req_slot.caption:
            raise HTTPException(
                status_code=400,
                detail=f"Slot {req_slot.slot}: both filename and caption are required.",
            )
        media_path = (MEDIA_DIR / req_slot.filename).resolve()
        if not media_path.is_relative_to(MEDIA_DIR.resolve()):
            raise HTTPException(
                status_code=400,
                detail=f"Slot {req_slot.slot}: filename resolves outside the media directory.",
            )
        if not media_path.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"Slot {req_slot.slot}: media file '{req_slot.filename}' not found.",
            )
        slot_batches.append(SlotBatch(
            slot=req_slot.slot,
            media_path=req_slot.filename,
            caption=req_slot.caption,
        ))

    effective_headless = HEADLESS if request.headless is None else request.headless
    try:
        batch = add_batch(request.fire_time, slot_batches, effective_headless)
    except ValueError as e:
        # The queue layer re-validates fire_time independently, with a shorter
        # lead time than ScheduleRequest (backend/models.py). A rejection here
        # is a bad request, not a server fault — reschedule_batch has always
        # answered 422 for the same class of failure, and this path used to
        # answer 500 (#6).
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to schedule batch: {e}")

    return {"id": batch.id, "fire_time": batch.fire_time.isoformat()}


@app.get("/api/queue/media")
async def list_queue_media():
    """List retained media snapshots and why each one was kept (#4).

    Declared before `/api/queue/{batch_id}` routes that could shadow it. There
    is no GET on that path today, but the ordering is the guard.
    """
    snapshots = classify_snapshots()
    return {"snapshots": [s.to_dict() for s in snapshots]}


@app.delete("/api/queue/media/{batch_id}")
async def delete_queue_media(batch_id: str):
    """Delete one media snapshot on explicit maintainer instruction.

    Refuses anything the reconciliation pass would also refuse to delete
    automatically *for structural reasons* — an active batch's media, or a
    directory this code did not create. Retained evidence is deletable here and
    only here: that is the whole point of the endpoint, and the confirmation
    lives in the UI next to the reason the snapshot was kept.
    """
    try:
        removed = remove_snapshot(batch_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No media snapshot for that batch.")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete media snapshot: {e}")
    return {"status": "deleted", "batch_id": batch_id,
            "classification": removed.classification,
            "files": len(removed.files)}


@app.get("/api/queue")
async def list_queue():
    """List pending/running/interrupted batches."""
    batches = load_queue()
    return {"batches": [b.to_dict() for b in batches]}


@app.delete("/api/queue/{batch_id}")
async def cancel_or_dismiss_queue_entry(batch_id: str):
    """Cancel a pending batch or dismiss an interrupted one."""
    if cancel_batch(batch_id):
        return {"status": "cancelled"}
    if dismiss_batch(batch_id):
        return {"status": "dismissed"}
    raise HTTPException(status_code=404, detail="Batch not found or not in a cancellable/dismissable state.")


@app.patch("/api/queue/{batch_id}")
async def reschedule_batch(batch_id: str, request: RescheduleRequest):
    """Update fire_time of a pending batch.

    Intentionally API-only: there is no frontend caller, by design (#10). The
    updated batch is returned so an API caller can confirm the persisted,
    normalized fire time rather than inferring it from a 200 with no body.
    """
    try:
        updated_ok = update_fire_time(batch_id, request.fire_time)
    except ValueError as e:
        # The queue layer re-validates independently (> now, vs the model's
        # > now + 1 minute). Normally unreachable, but an uncaught ValueError
        # here would surface as a bodiless 500; schedule_batch guards add_batch
        # the same way.
        raise HTTPException(status_code=422, detail=str(e))
    if not updated_ok:
        raise HTTPException(status_code=404, detail="Batch not found or not pending.")

    updated = next((b for b in load_queue() if b.id == batch_id), None)
    if updated is None:
        # The batch was persisted a moment ago; a miss here means the queue file
        # changed underneath us, which the caller must not read as success.
        raise HTTPException(
            status_code=500,
            detail="Batch was rescheduled but could not be read back from the queue.",
        )
    return updated.to_dict()
