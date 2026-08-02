import asyncio
import httpx
import uuid
from pathlib import Path
from backend.config import MOCK_MODE

GRAPH_API_BASE = "https://graph.facebook.com/v20.0"


async def post_media(
    ig_user_id: str,
    token: str,
    media_path: Path,
    caption: str,
    media_type: str,  # "video" or "image"
) -> str:
    """Post a reel or image to Instagram. Returns the published media ID."""
    if MOCK_MODE:
        await asyncio.sleep(1)  # simulate network delay
        return f"mock_ig_post_{uuid.uuid4().hex[:8]}"

    async with httpx.AsyncClient(timeout=120) as client:
        if media_type == "video":
            # Step 1: Create reel container
            resp = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media",
                params={
                    "access_token": token,
                    "media_type": "REELS",
                    "video_url": str(media_path),  # must be a public URL
                    "caption": caption,
                },
            )
            resp.raise_for_status()
            container_id = resp.json()["id"]

            # Step 2: Poll until video is processed
            for _ in range(30):
                status_resp = await client.get(
                    f"{GRAPH_API_BASE}/{container_id}",
                    params={
                        "access_token": token,
                        "fields": "status_code",
                    },
                )
                status = status_resp.json().get("status_code")
                if status == "FINISHED":
                    break
                elif status == "ERROR":
                    raise Exception(f"IG video processing failed for container {container_id}")
                await asyncio.sleep(10)
            else:
                raise Exception(f"IG video processing timed out for container {container_id}")

        else:
            # Image post
            resp = await client.post(
                f"{GRAPH_API_BASE}/{ig_user_id}/media",
                params={
                    "access_token": token,
                    "image_url": str(media_path),  # must be a public URL
                    "caption": caption,
                },
            )
            resp.raise_for_status()
            container_id = resp.json()["id"]

        # Step 3: Publish
        pub_resp = await client.post(
            f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
            params={
                "access_token": token,
                "creation_id": container_id,
            },
        )
        pub_resp.raise_for_status()
        return pub_resp.json()["id"]
