"""
Orchestration using browser automation instead of APIs.
Drop-in replacement for poster.py — same interface, different backend.
"""

import asyncio
import re
from pathlib import Path
from backend.models import PostResult
from backend.session_manager import session_exists
from backend import instagram_browser, tiktok_browser
from backend.config import INTER_SLOT_DELAY_MIN_S, INTER_SLOT_DELAY_MAX_S
from backend.jitter import jittered_duration
from backend.notifier import get_notifier, send_safe


def _notify(progress_cb, slot: str, platform: str, status: str, detail: str = ""):
    """Report a progress event to the UI; reporting must never break a run."""
    if progress_cb is None:
        return
    try:
        progress_cb(slot, platform, status, detail)
    except Exception:
        pass


# --- Failure notifications (FR-F3 / DESIGN-scheduling.md §4) -----------------
# The progress_callback above serves the live UI; the notifier below serves the
# absent maintainer's phone. They are deliberately separate audiences — do not
# merge them. Everything here is careful to include slot + platform + status +
# error summary ONLY: never caption text, credentials, cookie values, or ids
# beyond the honest post-id string.

# A quoted-repr run of >20 chars in an error string almost always echoes DOM /
# caption content (e.g. TikTok's verifier does `{last_seen[:200]!r}`). Browser
# exceptions can echo arbitrary page text the same way, so redact defensively.
_REPR_SEGMENT_RE = re.compile(r"""(['"]).{8,}?\1""", re.DOTALL)

# Both platforms raise this marker since Batch 6, so the truncation below
# redacts Instagram's caption-verification errors as well as TikTok's.
from backend.browser_common import EDITOR_MARKER as _EDITOR_MARKER


def _notification_safe(error: str) -> str:
    """Scrub an error string so no caption/DOM text can reach a phone push.

    Fixes the CRITICAL caption-leak finding. The full diagnostic still lives in
    result.errors / the UI / history.jsonl unchanged — this only guards the
    notifier boundary: (a) truncate from the 'Editor contained' marker onward;
    (b) strip any quoted-repr segment >20 chars (defensive against arbitrary
    browser exceptions echoing page content)."""
    idx = error.find(_EDITOR_MARKER)
    if idx != -1:
        error = error[:idx] + "editor contents redacted"
    return _REPR_SEGMENT_RE.sub("'<redacted>'", error)


def _is_skip(error: str) -> bool:
    """True for the 'skipped (...)' strings post_slot appends when a platform
    has no saved session, or when the scheduler's pre-flight check already
    ruled it out. A skip is neither an error nor an
    unconfirmed post, so it must not trigger a per-slot push."""
    return "skipped (no session" in error or "skipped (pre-flight" in error


def _shorten(text: str, limit: int = 120) -> str:
    """Collapse whitespace, strip the redundant "IG post:"/"TT post:" prefix,
    and cap length so a long exception message doesn't bloat a push preview."""
    for prefix in ("IG post: ", "TT post: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _platform_events(result: PostResult) -> list[tuple[str, str, str]]:
    """Per-platform (label, status, detail) for platforms that need noting:
    "error" (a real failure), "unconfirmed" (posted but not verified), or
    "skipped" (no saved session — not a failure). A clean success yields
    nothing. Every detail string is run through `_notification_safe`, so no
    caption/DOM text can ever reach a push."""
    events: list[tuple[str, str, str]] = []
    for label, prefix, post_id in (
        ("Instagram", "IG", result.ig_post_id),
        ("TikTok", "TT", result.tt_post_id),
    ):
        errors = [e for e in result.errors if e.startswith(prefix)]
        real_errors = [e for e in errors if not _is_skip(e)]
        skips = [e for e in errors if _is_skip(e)]
        if real_errors:
            events.append(
                (label, "error", _notification_safe("; ".join(real_errors)))
            )
        elif skips:
            events.append((label, "skipped", _notification_safe("; ".join(skips))))
        elif post_id and "unconfirmed" in post_id:
            events.append((label, "unconfirmed", _notification_safe(post_id)))
    return events


async def _push(notifier, title: str, body: str, priority: str = "default"):
    """Send one notification, guarded against a raising notifier.

    Thin alias kept for the existing call sites and their test doubles; the
    implementation moved to `notifier.send_safe` so the scheduler shares it
    rather than awaiting `notifier.send` bare (Phase 2 audit, finding #3)."""
    return await send_safe(notifier, title, body, priority)


async def _notify_slot_result(
    notifier, result: PostResult, notification_label: str | None = None
):
    """Push one notification per platform that errored or came back unconfirmed.
    Skips ("no session") are deliberately silent — the spec notifies on
    error/unconfirmed only; a skip is neither."""
    label = notification_label or f"slot {result.slot}"
    for platform, status, detail in _platform_events(result):
        if status == "skipped":
            continue
        priority = "high" if status == "error" else "default"
        await _push(
            notifier,
            title=f"RicePoster: {label} {platform} {status}",
            body=f"{label.capitalize()} {platform}: {status} — {_shorten(detail)}",
            priority=priority,
        )


async def _notify_run_summary(
    notifier, results: list[PostResult], notification_labels: dict[str, str] | None = None
):
    """Push the end-of-run summary. A skipped platform is not a failure, and an
    unconfirmed slot is not a plain success — both are surfaced distinctly:
      "3/3 posted successfully"
      "2/3 posted, 1 failed: [slot B TikTok: session expired]"
      "1/1 posted, 1 skipped (no session: A IG)"   (the other platform posted)
      "0/3 posted, 6 skipped (no session: A IG, A TT, ...)"
      "3/3 posted, 1 unconfirmed"
    Nothing is pushed for an empty run (post_all([]))."""
    if not results:
        return
    total = len(results)
    failed = []           # slots with at least one real (non-skip) error
    skipped_only = []     # slots where nothing was posted at all
    fail_parts = []       # "slot B TikTok: <detail>"
    skip_notes = []       # "A IG"
    unconfirmed = 0
    notification_labels = notification_labels or {}
    for r in results:
        label = notification_labels.get(r.slot, f"slot {r.slot}")
        events = _platform_events(r)
        if any(status == "error" for _, status, _ in events):
            failed.append(r)
        elif not (r.ig_post_id or r.tt_post_id):
            # Every platform on this slot was skipped: post_media was never
            # called, so neither id was ever assigned. Such a slot used to
            # land in the numerator, because `failed` only counts real errors
            # and a skip is not one — so a run where every session was missing
            # pushed "3/3 posted, 6 skipped", claiming success and total
            # failure in one message (Phase 2 audit, finding #1). The existing
            # tests only ever skipped one platform of a slot that posted on
            # the other, which is why the arithmetic looked correct.
            skipped_only.append(r)
        for platform, status, detail in events:
            if status == "error":
                fail_parts.append(f"{label} {platform}: {_shorten(detail, 60)}")
            elif status == "skipped":
                skip_notes.append(f"{label} {'IG' if platform == 'Instagram' else 'TT'}")
            elif status == "unconfirmed":
                unconfirmed += 1
    posted = total - len(failed) - len(skipped_only)

    segments = []
    if failed:
        detail_str = "; ".join(fail_parts) if fail_parts else ", ".join(
            notification_labels.get(r.slot, f"slot {r.slot}") for r in failed
        )
        segments.append(f"{len(failed)} failed: [{detail_str}]")
    if skip_notes:
        segments.append(f"{len(skip_notes)} skipped (no session: {', '.join(skip_notes)})")
    if unconfirmed:
        segments.append(f"{unconfirmed} unconfirmed")

    if not segments:
        await _push(
            notifier,
            title="RicePoster: run complete",
            body=f"{posted}/{total} posted successfully",
        )
        return
    # A run that posted nothing is worth waking the maintainer for even when
    # nothing technically errored — every session missing is the shape this
    # takes in practice, and it used to arrive as a normal-priority success
    # (maintainer decision 2026-07-27).
    nothing_posted = posted == 0
    if failed:
        title = "RicePoster: run had failures"
    elif nothing_posted:
        title = "RicePoster: nothing posted"
    else:
        title = "RicePoster: run complete"
    await _push(
        notifier,
        title=title,
        body=f"{posted}/{total} posted, " + ", ".join(segments),
        priority="high" if (failed or nothing_posted) else "default",
    )


async def post_slot(
    slot: str,
    media_path: Path,
    caption: str,
    media_type: str,
    headless: bool = True,
    progress_cb=None,
    skip_platforms: set[str] | None = None,
) -> PostResult:
    """Post one media+caption to both IG and TikTok for a given account slot.

    Skips any platform that doesn't have a saved session, and any platform
    named in `skip_platforms`.

    `skip_platforms` carries the scheduler's pre-flight verdict (review
    2026-07-26, finding #2). session_exists() below is a filesystem-existence
    check: it returns True for a profile directory whose session has expired,
    which is precisely why the pre-flight check exists. Without this parameter
    the pre-flight result was discarded here, so a platform the maintainer had
    just been pushed a "skipped" notification for was posted to anyway.
    """
    result = PostResult(slot=slot)
    skip_platforms = skip_platforms or set()

    has_ig = session_exists("instagram", slot) and "instagram" not in skip_platforms
    has_tt = session_exists("tiktok", slot) and "tiktok" not in skip_platforms

    # Instagram post
    if has_ig:
        _notify(progress_cb, slot, "instagram", "started")
        try:
            result.ig_post_id = await instagram_browser.post_media(
                account_key=slot,
                media_path=media_path,
                caption=caption,
                media_type=media_type,
                headless=headless,
            )
            _notify(
                progress_cb, slot, "instagram",
                "unconfirmed" if "unconfirmed" in result.ig_post_id else "ok",
                result.ig_post_id,
            )
        except Exception as e:
            result.errors.append(f"IG post: {e}")
            _notify(progress_cb, slot, "instagram", "error", str(e))
    elif "instagram" in skip_platforms:
        result.errors.append("IG post: skipped (pre-flight ruled the session out)")
        _notify(progress_cb, slot, "instagram", "skipped", "pre-flight")
    else:
        result.errors.append("IG post: skipped (no session — run session_manager login instagram)")
        _notify(progress_cb, slot, "instagram", "skipped", "no session")

    # TikTok post
    if has_tt:
        _notify(progress_cb, slot, "tiktok", "started")
        try:
            result.tt_post_id = await tiktok_browser.post_media(
                account_key=slot,
                media_path=media_path,
                caption=caption,
                media_type=media_type,
                headless=headless,
            )
            _notify(
                progress_cb, slot, "tiktok",
                "unconfirmed" if "unconfirmed" in result.tt_post_id else "ok",
                result.tt_post_id,
            )
        except Exception as e:
            result.errors.append(f"TT post: {e}")
            _notify(progress_cb, slot, "tiktok", "error", str(e))
    elif "tiktok" in skip_platforms:
        result.errors.append("TT post: skipped (pre-flight ruled the session out)")
        _notify(progress_cb, slot, "tiktok", "skipped", "pre-flight")
    else:
        result.errors.append("TT post: skipped (no session — run session_manager login tiktok)")
        _notify(progress_cb, slot, "tiktok", "skipped", "no session")

    return result


async def _inter_slot_delay(progress_cb=None, next_slot: str = "") -> float:
    """Wait a randomised gap between accounts. Returns the seconds waited.

    Emits a "waiting" progress event carrying the gap length *before*
    sleeping, so the UI can count down. Without it a multi-minute pause looks
    exactly like a hung run.

    Returns 0 immediately when disabled (both bounds 0). A min above max is
    treated as a misconfiguration and clamped rather than raising — a bad env
    value must not take down a posting run.
    """
    lo = max(0.0, INTER_SLOT_DELAY_MIN_S)
    hi = max(0.0, INTER_SLOT_DELAY_MAX_S)
    if hi <= 0:
        return 0.0
    lo = min(lo, hi)
    duration = jittered_duration(lo, spread=hi - lo)
    # platform "run" marks this as a run-level event, not a per-platform
    # result — the UI must not render it as a post outcome.
    _notify(progress_cb, next_slot, "run", "waiting", str(int(duration)))
    await asyncio.sleep(duration)
    return duration


async def post_all(
    slots: list[dict],
    headless: bool = True,
    progress_cb=None,
    notifier=None,
) -> list[PostResult]:
    """Post to all account slots sequentially.
    Running one browser at a time is more reliable than 3 concurrent instances.

    `notifier` handles phone pushes for failures/unconfirmed slots and the
    end-of-run summary (all runs, manual and scheduled — maintainer decision).
    Defaults to get_notifier(); NOTIFY_SERVICE=none makes it a no-op. Kept
    entirely separate from progress_cb (live UI, different audience)."""
    if notifier is None:
        notifier = get_notifier()
    results = []
    notification_labels = {
        s["slot"]: s.get("notification_label", f"account {index + 1}")
        for index, s in enumerate(slots)
    }
    for i, s in enumerate(slots):
        # Randomised gap between accounts (F4). Off by default; see
        # INTER_SLOT_DELAY_MIN_S/MAX_S in config.py. Deliberately *before*
        # each slot after the first, so a run never ends on a dead wait.
        if i > 0:
            await _inter_slot_delay(progress_cb, s["slot"])
        result = await post_slot(
            slot=s["slot"],
            media_path=s["media_path"],
            caption=s["caption"],
            media_type=s["media_type"],
            headless=headless,
            progress_cb=progress_cb,
            # Absent on the manual /api/post path, which runs no pre-flight.
            skip_platforms=s.get("skip_platforms"),
        )
        results.append(result)
        # Notify per-slot as soon as it lands, so a phone alert isn't held
        # hostage by the next slot's slow browser run.
        await _notify_slot_result(notifier, result, notification_labels[result.slot])
    await _notify_run_summary(notifier, results, notification_labels)
    return results
