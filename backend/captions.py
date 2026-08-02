import json

import anthropic
from pydantic import BaseModel

from backend.config import ANTHROPIC_API_KEY, PROMPTS_DIR
from backend.logging_setup import get_logger

_log = get_logger("captions")

# Caption styles live in prompts/*.json — one file per style, loaded fresh on
# every request so style edits apply without a server restart.
# The default is deliberately the neutral style: a shipped default naming real
# people would tie this repo to the accounts it posts to. Maintainer-specific
# styles are kept as local, gitignored prompt files.
DEFAULT_STYLE = "generic"

# Bounded timeout for the caption API call (tech-debt audit BE-10). The SDK
# already retries twice with backoff, so retry was not the gap — but its default
# read timeout is 600s, and this call sits on the posting critical path, where a
# ten-minute stall is indistinguishable from a hang. Bounded for the same reason
# notifier.send is.
CAPTION_API_TIMEOUT_S = 60.0


class CaptionStyle(BaseModel):
    name: str
    display_name: str
    system_prompt: str
    no_topic_fallback: str


def load_styles() -> dict[str, CaptionStyle]:
    """Load all caption styles from PROMPTS_DIR. A malformed file is skipped
    with a warning so one bad edit can't take down caption generation for
    every style."""
    styles: dict[str, CaptionStyle] = {}
    for path in sorted(PROMPTS_DIR.glob("*.json")):
        try:
            style = CaptionStyle(**json.loads(path.read_text()))
            styles[style.name] = style
        except Exception as e:
            _log.warning(f"[captions] Skipping invalid style file {path.name}: {e}")
    return styles


def _build_user_prompt(
    media_type: str,
    topic: str,
    no_topic_fallback: str,
    avoid_caption: str = "",
    feedback: str = "",
) -> str:
    parts = [f"Write a caption for a {media_type} post."]
    if topic.strip():
        parts.append(f"Content description: {topic}")
    else:
        parts.append(no_topic_fallback)
    if avoid_caption.strip():
        parts.append(
            "A previous caption for this exact post was: "
            f'"{avoid_caption.strip()}" '
            "Write a meaningfully different caption: different opening, "
            "different angle, and do not reuse its phrasing."
        )
    if feedback.strip():
        parts.append(f"Revision guidance from the user: {feedback.strip()}")
    return " ".join(parts)


def _build_content(user_prompt: str, thumbnail_b64: str = ""):
    """Message content for the caption request. With a thumbnail (base64
    JPEG, no data-URL prefix) the frame goes first so the model captions
    what the media actually shows; without one, plain text as before."""
    if not thumbnail_b64:
        return user_prompt
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": thumbnail_b64,
            },
        },
        {
            "type": "text",
            "text": (
                f"{user_prompt} The attached image is a frame from the media "
                "being posted — ground the caption in what it actually shows."
            ),
        },
    ]


async def generate_caption(
    media_type: str,
    topic: str,
    style: str = DEFAULT_STYLE,
    avoid_caption: str = "",
    feedback: str = "",
    thumbnail_b64: str = "",
) -> str:
    """Generate a single caption for one media file using the given style."""
    if not ANTHROPIC_API_KEY.get_secret_value():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in credentials.env — captions cannot be generated."
        )

    styles = load_styles()
    selected = styles.get(style)
    if selected is None:
        raise ValueError(
            f"Unknown caption style '{style}'. Available: {', '.join(sorted(styles)) or 'none'}"
        )

    client = anthropic.AsyncAnthropic(
        api_key=ANTHROPIC_API_KEY.get_secret_value(),
        timeout=CAPTION_API_TIMEOUT_S,
    )
    user_prompt = _build_user_prompt(
        media_type, topic, selected.no_topic_fallback, avoid_caption, feedback
    )

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=selected.system_prompt,
        messages=[{"role": "user", "content": _build_content(user_prompt, thumbnail_b64)}],
    )
    return response.content[0].text.strip()
