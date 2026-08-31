"""One outcome vocabulary shared by history, Stats, and UI contracts."""

from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timezone


def is_skip_error(error: object) -> bool:
    return isinstance(error, str) and (
        "skipped (no session" in error or "skipped (pre-flight" in error
    )


def classify_platform(prefix: str, post_id: object, errors: object) -> str:
    # An explicitly malformed error collection makes the row's evidence
    # internally inconsistent. Never promote a post ID in that row to a
    # confirmed outcome; retain it in the existing uncertainty bucket.
    if not isinstance(errors, list):
        return "unconfirmed"
    platform = "instagram" if prefix == "IG" else "tiktok"
    matching = [
        e for e in errors if isinstance(e, str) and (
            e.startswith(f"{prefix} post:")
            or e.startswith(f"{prefix} post failed:")
        )
    ]
    # Older scheduled rows recorded the pre-flight verdict without an IG/TT
    # post prefix. They remain local evidence and must retain their meaning.
    legacy_skip = any(
        isinstance(e, str) and e.lower().startswith(f"pre-flight: {platform} ")
        for e in errors
    )
    # A real failure wins over a skip when both were recorded, matching the
    # Review UI and poster event vocabulary.
    if any(not is_skip_error(e) for e in matching):
        return "failed"
    if matching or legacy_skip:
        return "skipped"
    if isinstance(post_id, str) and post_id:
        return "unconfirmed" if "unconfirmed" in post_id.lower() else "confirmed"
    return "not_attempted"


def classify_history_row(row: dict) -> dict[str, str]:
    errors = row.get("errors", [])
    return {
        "instagram": classify_platform("IG", row.get("ig_post_id", ""), errors),
        "tiktok": classify_platform("TT", row.get("tt_post_id", ""), errors),
    }


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def aggregate_stats(history_file: Path, media_dir: Path, queue_media_dir: Path) -> dict:
    rows: list[dict] = []
    if history_file.is_file():
        for line in history_file.read_text().splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue

    counts = {name: 0 for name in ("confirmed", "unconfirmed", "failed", "skipped")}
    accounts: set[str] = set()
    timestamps: list[tuple[datetime, str]] = []
    tracked_media_bytes = 0
    tracked_media_items = 0
    manual_runs: set[str] = set()
    scheduled_runs: set[str] = set()
    content_items = 0
    for index, row in enumerate(rows):
        account_id = row.get("account_id") or row.get("slot")
        if isinstance(account_id, str) and account_id and account_id != "(none)":
            accounts.add(account_id)
            content_items += 1
        if row.get("scheduled"):
            scheduled_runs.add(str(row.get("batch_id") or f"legacy-{index}"))
        else:
            # Pre-tracking manual history wrote one row per account with the
            # same second-resolution timestamp. Group those rows as one run
            # instead of inflating a three-account execution into three runs.
            manual_runs.add(str(
                row.get("run_id")
                or (f"legacy-ts-{row.get('ts')}" if row.get("ts") else f"legacy-{index}")
            ))
        ts = row.get("ts")
        if isinstance(ts, str) and ts:
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                timestamps.append((parsed.astimezone(timezone.utc), ts))
            except ValueError:
                pass
        media_bytes = row.get("media_bytes")
        if isinstance(media_bytes, int) and not isinstance(media_bytes, bool) and media_bytes >= 0:
            tracked_media_bytes += media_bytes
            tracked_media_items += 1
        for outcome in classify_history_row(row).values():
            if outcome in counts:
                counts[outcome] += 1

    return {
        "confirmed_platform_posts": counts["confirmed"],
        "content_items_attempted": content_items,
        "unconfirmed_platform_outcomes": counts["unconfirmed"],
        "failed_platform_outcomes": counts["failed"],
        "skipped_platform_outcomes": counts["skipped"],
        "unique_accounts_used": len(accounts),
        "manual_executions": len(manual_runs),
        "scheduled_executions": len(scheduled_runs),
        "current_uploaded_media_bytes": _tree_size(media_dir),
        "current_retained_queue_media_bytes": _tree_size(queue_media_dir),
        "first_recorded_activity": min(timestamps)[1] if timestamps else None,
        "most_recent_recorded_activity": max(timestamps)[1] if timestamps else None,
        "tracked_media_bytes": tracked_media_bytes,
        "tracked_media_items": tracked_media_items,
        "media_bytes_label": "since tracking began",
    }
