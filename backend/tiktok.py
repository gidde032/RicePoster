import asyncio
import httpx
import uuid
from pathlib import Path
from backend.config import MOCK_MODE

TIKTOK_API_BASE = "https://open.tiktokapis.com"


async def post_media(
    token: str,
    media_path: Path,
    caption: str,
    media_type: str,  # "video" or "image"
) -> str:
    """Post video or photo to TikTok. Returns publish_id."""
    if MOCK_MODE:
        await asyncio.sleep(1)
        return f"mock_tt_post_{uuid.uuid4().hex[:8]}"

    async with httpx.AsyncClient(timeout=120) as client:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        if media_type == "video":
            # Direct post for video
            file_size = media_path.stat().st_size

            # Step 1: Initialize upload
            init_resp = await client.post(
                f"{TIKTOK_API_BASE}/v2/post/publish/inbox/video/init/",
                headers=headers,
                json={
                    "post_info": {
                        "title": caption[:150],  # TikTok title limit
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                        "disable_duet": False,
                        "disable_comment": False,
                        "disable_stitch": False,
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": file_size,
                        "chunk_size": file_size,
                        "total_chunk_count": 1,
                    },
                },
            )
            init_resp.raise_for_status()
            data = init_resp.json()["data"]
            upload_url = data["upload_url"]
            publish_id = data["publish_id"]

            # Step 2: Upload video bytes
            with open(media_path, "rb") as f:
                video_bytes = f.read()

            upload_resp = await client.put(
                upload_url,
                headers={
                    "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                    "Content-Type": "video/mp4",
                },
                content=video_bytes,
            )
            upload_resp.raise_for_status()

        else:
            # Photo post
            # Step 1: Initialize photo post
            init_resp = await client.post(
                f"{TIKTOK_API_BASE}/v2/post/publish/content/init/",
                headers=headers,
                json={
                    "post_info": {
                        "title": caption[:150],
                        "post_mode": "DIRECT_POST",
                        "media_type": "PHOTO",
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "photo_images": [str(media_path)],
                    },
                },
            )
            init_resp.raise_for_status()
            publish_id = init_resp.json()["data"]["publish_id"]

        # Step 3: Poll for completion
        for _ in range(30):
            status_resp = await client.post(
                f"{TIKTOK_API_BASE}/v2/post/publish/status/fetch/",
                headers=headers,
                json={"publish_id": publish_id},
            )
            status = status_resp.json().get("data", {}).get("status")
            if status == "PUBLISH_COMPLETE":
                return publish_id
            elif status in ("FAILED", "PUBLISH_FAILED"):
                raise Exception(f"TikTok publish failed for {publish_id}")
            await asyncio.sleep(5)

        raise Exception(f"TikTok publish timed out for {publish_id}")
