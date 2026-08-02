import asyncio
from pathlib import Path
from backend.config import get_accounts
from backend.models import PostResult
from backend import instagram, tiktok


def _redact(text: str, *secrets: str) -> str:
    """Strip token values from error text — httpx errors embed the full
    request URL, which carries access_token as a query param."""
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


async def post_slot(
    slot: str,
    media_path: Path,
    caption: str,
    media_type: str,
) -> PostResult:
    """Post one media+caption to both IG and TikTok for a given account slot."""
    accounts = {a.slot: a for a in get_accounts()}
    account = accounts[slot]
    result = PostResult(slot=slot)
    ig_token = account.ig_token.get_secret_value()
    tt_token = account.tt_token.get_secret_value()

    # Instagram post
    try:
        result.ig_post_id = await instagram.post_media(
            ig_user_id=account.ig_user_id,
            token=ig_token,
            media_path=media_path,
            caption=caption,
            media_type=media_type,
        )
    except Exception as e:
        result.errors.append(_redact(f"IG post failed: {e}", ig_token, tt_token))

    # TikTok post
    try:
        result.tt_post_id = await tiktok.post_media(
            token=tt_token,
            media_path=media_path,
            caption=caption,
            media_type=media_type,
        )
    except Exception as e:
        result.errors.append(_redact(f"TT post failed: {e}", ig_token, tt_token))

    return result


async def post_all(
    slots: list[dict],  # [{"slot": "A", "media_path": Path, "caption": str, "media_type": str}]
) -> list[PostResult]:
    """Post to all account slots concurrently."""
    tasks = [
        post_slot(
            slot=s["slot"],
            media_path=s["media_path"],
            caption=s["caption"],
            media_type=s["media_type"],
        )
        for s in slots
    ]
    return await asyncio.gather(*tasks)
