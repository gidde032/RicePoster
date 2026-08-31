from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

# Accepted media kinds for caption generation. Previously unvalidated: an
# unknown value reached generate_caption and shaped a prompt from nonsense
# instead of failing with a 422 that names the field (#33).
MEDIA_TYPES = ("video", "image")

# Caption length cap, validated at the API boundary so an over-length caption
# is rejected (422) before any browser work starts (#53, maintainer decision
# 2026-08-03, option 1). Previously Instagram read back truncated text and
# abandoned the post after the media had already uploaded.
#
# A SINGLE limit is used even though Instagram's real cap is ~2200 and
# TikTok's differs, because in practice captions are AI-generated and short
# and no platform cap has actually been hit on TikTok. Per-platform constants
# are DEFERRED until a platform cap actually differs in practice — this is a
# decision, not an oversight. When that day comes, split this into a
# per-platform map and validate against the slot's target platforms.
#
# This is the single source of truth: it is served to the frontend via
# GET /api/accounts (`caption_limit`) so the UI's live char counter cannot
# drift from the value the backend enforces (#53, the duplicated-constant
# trap pending-lessons.md records from the smoke-budget test).
MAX_CAPTION_LENGTH = 2200


# Two layers check fire_time, and they deliberately require different lead
# times (#6, maintainer decision 2026-07-30).
#
# The API layer demands a one-minute cushion so a batch cannot be scheduled to
# fire essentially instantly. The queue layer re-checks a moment later, after
# the endpoint has validated each slot's media file, and demands only "in the
# future" — that looser bound is the tolerance band absorbing the time elapsed
# between the two checks. Tightening the queue layer to match would let a
# request that cleared the API check by a fraction of a second fail the queue
# check purely because the clock moved, surfacing as an intermittent 422 on a
# request that succeeds on retry.
#
# So: one implementation, two lead times. The *rule* cannot drift between the
# layers; only this pair of constants expresses the intended difference.
API_MIN_LEAD = timedelta(minutes=1)
QUEUE_MIN_LEAD = timedelta(0)


def validate_future_fire_time(value: datetime,
                              min_lead: timedelta = API_MIN_LEAD) -> datetime:
    """Shared fire_time contract for scheduling and rescheduling.

    POST /api/queue and PATCH /api/queue/{batch_id} each enforced this by hand,
    in duplicate. One validator means the two endpoints cannot drift apart
    (#33, unification approved 2026-07-29). `backend.queue` calls the same
    validator with QUEUE_MIN_LEAD so the API and library layers share one
    implementation of the rule (#6).
    """
    if value.tzinfo is None:
        # Wording carries both layers' historical phrasings: this message is
        # returned to an API caller and raised at the queue layer.
        raise ValueError(
            "fire_time must be timezone-aware — include a UTC timezone offset."
        )
    if value <= datetime.now(timezone.utc) + min_lead:
        if min_lead <= timedelta(0):
            raise ValueError("fire_time must be in the future.")
        # Only the two constants above are used, and only API_MIN_LEAD reaches
        # here, so the phrasing is stated rather than derived from min_lead.
        raise ValueError("fire_time must be at least 1 minute in the future.")
    return value


class CaptionRequest(BaseModel):
    """Form payload for POST /api/generate-caption.

    Bound with Annotated[..., Form()] so the endpoint keeps accepting the
    multipart body the UI already sends (needs FastAPI >= 0.113). Only the
    failure mode changes: a bad field is a 422 naming it rather than a
    hand-coded 400 or an uncaught 500.
    """

    media_type: str
    topic: str = ""
    # None means "not supplied" and resolves to DEFAULT_STYLE at the endpoint,
    # matching the previous Form(DEFAULT_STYLE) default. An explicitly empty
    # style stays empty and is still rejected downstream by generate_caption,
    # which is the pre-existing behaviour and not this issue's to change.
    style: str | None = None
    avoid_caption: str = ""
    feedback: str = ""
    thumbnail: str = ""

    @field_validator("media_type")
    @classmethod
    def _known_media_type(cls, v: str) -> str:
        if v not in MEDIA_TYPES:
            raise ValueError(f"media_type must be one of: {', '.join(MEDIA_TYPES)}")
        return v


class CaptionResponse(BaseModel):
    slot: str
    caption: str


class PostSlot(BaseModel):
    slot: str  # slot id from ACCOUNT_SLOTS, e.g. "A"
    filename: str
    caption: str
    media_type: str = "image"

    @model_validator(mode="after")
    def _caption_within_limit(self):
        """Reject an over-length caption before any browser work starts (#53).

        A model-level validator (not a field validator) so the message can name
        the slot as well as the limit. Applies to both POST /api/post and
        POST /api/queue, which both build PostSlots — so a too-long caption is
        a 422 at the request boundary and never reaches a posting run.
        """
        if len(self.caption) > MAX_CAPTION_LENGTH:
            raise ValueError(
                f"Slot {self.slot}: caption is {len(self.caption)} characters, "
                f"exceeds the {MAX_CAPTION_LENGTH}-character limit."
            )
        return self


class PostRequest(BaseModel):
    slots: list[PostSlot]
    # None → use the env default; the UI sends an explicit override in
    # browser mode (mirrors the old "headless" form field)
    headless: bool | None = None


class PostResult(BaseModel):
    slot: str
    ig_post_id: str = ""
    tt_post_id: str = ""
    errors: list[str] = []

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class ScheduleRequest(PostRequest):
    fire_time: datetime

    @field_validator("fire_time")
    @classmethod
    def _fire_time_is_future(cls, v: datetime) -> datetime:
        return validate_future_fire_time(v)


class RescheduleRequest(BaseModel):
    """Body of PATCH /api/queue/{batch_id}.

    Intentionally API-only — there is no frontend caller (#10).
    """

    fire_time: datetime

    @field_validator("fire_time")
    @classmethod
    def _fire_time_is_future(cls, v: datetime) -> datetime:
        return validate_future_fire_time(v)


class AccountStateRequest(BaseModel):
    """Complete local account-state update from the Accounts workspace."""

    active_account_ids: list[str]
    rosters: dict[str, list[str]] = Field(default_factory=dict)
    caption_defaults: dict[str, str] = Field(default_factory=dict)
