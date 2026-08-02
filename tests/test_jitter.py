"""
Tests for timing jitter and inter-slot delay (F4).

Source: RESEARCH-platform-detection.md Finding 4 (2026-07-25) —
every wait was a fixed integer constant, so inter-action timing was
identical on every run of every account, and slots posted back-to-back with
no gap.

The dominant risk in this fix is not that jitter fails to happen; it is that
jitter quietly *shortens* a wait. CLAUDE.md § Intentional design protects
long sleeps and generous timeouts as deliberate — platform UIs are unstable
and the machine is under load. Most of the tests below exist to defend that
floor.
"""

import asyncio
import inspect
import pathlib
import time

import pytest

from backend import instagram_browser, poster_browser, session_manager, tiktok_browser
from backend.jitter import (
    TYPING_CHUNK_MAX_WORDS,
    TYPING_DELAY_FLOOR_MS,
    TYPING_DELAY_SPREAD_MS,
    caption_chunks,
    jittered_duration,
    sleep_jittered,
    type_with_jitter,
    typing_delay_ms,
)


# --- the floor rule -------------------------------------------------------


@pytest.mark.parametrize("base", [0, 0.5, 1, 1.5, 2, 3, 10])
def test_jitter_never_returns_less_than_the_base_wait(base):
    """The single most important property in this module. If this fails, F4
    has silently shortened the timeouts that CLAUDE.md protects."""
    for _ in range(2000):
        assert jittered_duration(base) >= base


def test_jitter_actually_varies():
    """The flip side: a 'jitter' that always returns the same value would
    pass the floor test while doing nothing."""
    values = {jittered_duration(2) for _ in range(200)}
    assert len(values) > 100, "jittered_duration is not producing variance"


def test_jitter_is_bounded_by_the_spread():
    """Variance must stay proportional — a 0.5s pause must not occasionally
    become a 30s one and stall a run."""
    for _ in range(2000):
        assert 2 <= jittered_duration(2, spread=1.0) <= 3


def test_default_spread_is_proportional_to_the_base():
    """A 10s backoff should gain a meaningful spread; a 0.5s pause should
    not grow into several seconds."""
    short = [jittered_duration(0.5) for _ in range(500)]
    long_ = [jittered_duration(10) for _ in range(500)]
    assert max(short) <= 1.0
    assert max(long_) <= 14.0
    assert max(long_) > max(short)


def test_negative_inputs_are_rejected():
    """A negative base or spread could produce a wait shorter than intended,
    so fail loudly rather than silently."""
    with pytest.raises(ValueError):
        jittered_duration(-1)
    with pytest.raises(ValueError):
        jittered_duration(1, spread=-1)


def test_sleep_jittered_sleeps_for_the_duration_it_reports(monkeypatch):
    """Guards the seam the other tests rely on: the returned value must be
    the value actually passed to asyncio.sleep."""
    slept = []

    async def _fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    returned = asyncio.run(sleep_jittered(2))
    assert slept == [returned]
    assert returned >= 2


# --- Instagram flow uses it ----------------------------------------------


def test_instagram_has_no_fixed_sleeps_left():
    """Source-level: a single surviving asyncio.sleep(N) re-creates the
    machine-exact rhythm for that step."""
    src = inspect.getsource(instagram_browser)
    assert "asyncio.sleep(" not in src, (
        "instagram_browser still contains a fixed asyncio.sleep — every wait "
        "in the posting flow should go through sleep_jittered."
    )
    assert "sleep_jittered(" in src


def test_session_manager_has_no_fixed_sleeps_left():
    """Review 2026-07-26, finding #9.

    The health check's dwell and scroll pause used raw asyncio.sleep with an
    inline random.uniform. Variance was present, so this was consistency
    rather than a live bug — but the guard above only ever looked at
    instagram_browser, so the check's waits sat outside every floor-rule test
    in the suite. They now go through sleep_jittered like the posting flow,
    and this test is what keeps them there.
    """
    src = inspect.getsource(session_manager)
    assert "asyncio.sleep(" not in src, (
        "session_manager still contains a fixed asyncio.sleep — the health "
        "check's waits should go through sleep_jittered like the posting flow."
    )
    assert "sleep_jittered(" in src


def test_session_check_wait_floors_are_unchanged():
    """The conversion must not shorten either wait (CLAUDE.md § Intentional
    design: jitter is added *above* a floor, never in place of it).

    sleep_jittered(base, spread) sleeps base + uniform(0, spread), so the
    scroll pause only keeps its original 0.8-2.0s range if the spread is
    passed as MAX - MIN. Asserts the arithmetic rather than trusting it.
    """
    assert session_manager.SESSION_CHECK_DWELL_BASE_S >= 3.0
    lo = session_manager.SESSION_CHECK_SCROLL_PAUSE_MIN_S
    hi = session_manager.SESSION_CHECK_SCROLL_PAUSE_MAX_S
    base = session_manager.SESSION_CHECK_DWELL_BASE_S
    spread = session_manager.SESSION_CHECK_DWELL_SPREAD_S
    for _ in range(200):
        dwell = jittered_duration(base, spread)
        assert base <= dwell <= base + spread, (
            f"dwell {dwell} escaped its original {base}-{base + spread}s range"
        )
        pause = jittered_duration(lo, hi - lo)
        assert lo <= pause <= hi, (
            f"scroll pause {pause} escaped the original {lo}-{hi}s range — "
            "the spread argument is probably being passed as the max rather "
            "than as max minus min"
        )


def test_instagram_sleep_floors_are_unchanged():
    """The jitter conversion must not have retuned any wait downward. These
    are the exact bases present before F4."""
    src = inspect.getsource(instagram_browser)
    for base in ("0.5", "1", "1.0", "1.5", "2", "3", "10"):
        assert f"sleep_jittered({base})" in src, (
            f"the {base}s wait disappeared or was retuned during the F4 "
            "conversion; floors must be preserved exactly"
        )


# --- caption typing cadence (issue #2) ------------------------------------
#
# The dominant risk here is the same as everywhere else in this file — that
# the change quietly makes typing *faster* than the 60 ms baseline — plus one
# risk unique to typing: that chunking corrupts the caption, either by losing
# characters or by pausing inside a '#hashtag' where the platform's
# autocomplete dropdown can intercept the following keystrokes.


CAPTION_CORPUS = [
    "sunset over the harbour #latergram #goldenhour @studio",
    "one",
    "",
    "   ",
    "trailing space ",
    " leading space",
    "line one\nline two\n\nline four",
    "emoji 🌅 caption with 👍 marks #vibe",
    "日本語のキャプション テスト #タグ",
    "  awkward   spacing\tand\ttabs  ",
    "#tag1 #tag2 #tag3 #tag4 #tag5 #tag6 #tag7 #tag8",
    "a" * 300,
]


@pytest.mark.parametrize("text", CAPTION_CORPUS)
def test_chunks_always_rejoin_exactly(text):
    """Chunking must be invisible to the posted caption. If this can fail,
    the feature can silently publish a mangled caption — and Instagram had no
    read-back check when this landed."""
    for _ in range(50):  # chunk sizes are random; one pass proves nothing
        assert "".join(caption_chunks(text)) == text


@pytest.mark.parametrize("text", CAPTION_CORPUS)
def test_pauses_never_land_on_an_unterminated_token(text):
    """The safety property, asserted as timing rather than as string layout.

    A pause is only safe if, at the moment it happens, the last character
    already typed terminated its token — otherwise the caret is sitting
    inside an open '#hashtag' or '@mention' with the platform's autocomplete
    dropdown rendered and able to intercept what comes next.

    This replaces `test_pauses_never_land_inside_a_word`, which asserted
    `before[-1:].isspace() or after[:1].isspace()`. Under the original
    leading-whitespace chunking the right-hand disjunct was *always* true, so
    that assertion could not fail for any input or any tokenisation — it
    passed while every single pause landed on an open token. Reconstruct the
    typing timeline instead; that is the thing the browser actually sees.
    """
    for _ in range(50):
        chunks = caption_chunks(text)
        typed = ""
        for index, chunk in enumerate(chunks):
            if index and typed and not typed[-1].isspace():
                pytest.fail(
                    f"pause lands mid-token: caret sits after "
                    f"{typed.split()[-1]!r} with the token still open"
                )
            typed += chunk


def test_pause_after_a_hashtag_waits_for_its_closing_space():
    """Concrete form of the property above, on the case that motivated it:
    the dropdown for '#goldenhour' must already have been dismissed by its
    terminating space before any pause occurs."""
    text = "beach day #goldenhour #sunsetlover @thestudio more words here"
    for _ in range(100):
        chunks = caption_chunks(text)
        typed = ""
        for index, chunk in enumerate(chunks):
            if index:
                assert typed.endswith((" ", "\n", "\t")), (
                    f"pause with an unterminated token: ...{typed[-20:]!r}"
                )
            typed += chunk
        # ...and no tag was split across a boundary either.
        for tag in ["#goldenhour", "#sunsetlover", "@thestudio"]:
            assert any(tag in chunk for chunk in chunks), (
                f"{tag} was split across chunks: {chunks}"
            )


def test_chunks_are_bounded_in_size():
    """Chunks stay short enough that the cadence actually varies within one
    caption — a single chunk would be the old machine-perfect rhythm."""
    text = " ".join(f"word{i}" for i in range(60))
    seen_counts = set()
    for _ in range(50):
        chunks = caption_chunks(text)
        for chunk in chunks:
            assert len(chunk.split()) <= TYPING_CHUNK_MAX_WORDS
        seen_counts.add(len(chunks))
    assert len(chunks) > 1
    assert len(seen_counts) > 1, "chunk layout is deterministic, so is the cadence"


def test_typing_delay_never_drops_below_the_original_floor():
    """The fixed cadence was 60 ms per key. Bounded variance must be added
    *above* it — a faster delay is a bug, not a tuning decision."""
    for _ in range(500):
        delay = typing_delay_ms()
        assert delay >= TYPING_DELAY_FLOOR_MS
        assert delay <= TYPING_DELAY_FLOOR_MS + TYPING_DELAY_SPREAD_MS


def test_typing_delay_actually_varies():
    assert len({typing_delay_ms() for _ in range(50)}) > 1


def test_typing_floor_still_matches_the_cadence_it_replaced():
    """Pins the constant itself. The pre-#2 flows passed `delay=60`; if this
    is ever lowered it must be a deliberate, signed-off retune, not drift."""
    assert TYPING_DELAY_FLOOR_MS == 60


class _RecordingField:
    """Stands in for a Playwright locator; records what it was asked to type."""

    def __init__(self):
        self.typed = []

    async def press_sequentially(self, text, delay=None):
        self.typed.append((text, delay))


def test_type_with_jitter_types_the_whole_caption_and_pauses_between_chunks(monkeypatch):
    """The driver must reproduce the caption exactly, delay every chunk at or
    above the floor, and add one pause between chunks — never before the
    first keystroke or after the last."""
    slept = []

    async def _fake_sleep(duration):
        slept.append(duration)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    text = "sunset over the harbour #latergram #goldenhour @studio"
    field = _RecordingField()
    asyncio.run(type_with_jitter(field, text))

    assert "".join(chunk for chunk, _ in field.typed) == text
    assert all(delay >= TYPING_DELAY_FLOOR_MS for _, delay in field.typed)
    assert len(slept) == len(field.typed) - 1
    assert all(pause > 0 for pause in slept)


def test_type_with_jitter_makes_a_single_call_for_an_empty_caption(monkeypatch):
    """Degenerate input must not change the number of browser calls."""
    monkeypatch.setattr(asyncio, "sleep", lambda d: asyncio.sleep(0))
    field = _RecordingField()
    asyncio.run(type_with_jitter(field, ""))
    assert [text for text, _ in field.typed] == [""]


def test_both_browser_modules_use_the_shared_typing_contract():
    """Issue #2 acceptance: neither module may keep a hardcoded cadence.

    TikTok only types on its verification retry — its normal path inserts the
    caption atomically — so this asserts the retry path, not a claim that
    TikTok types every post.
    """
    for mod in (instagram_browser, tiktok_browser):
        src = inspect.getsource(mod)
        assert "delay=60" not in src, (
            f"{mod.__name__} still hardcodes the fixed 60 ms typing cadence"
        )
        assert "type_with_jitter(" in src


# --- inter-slot delay -----------------------------------------------------


def test_inter_slot_delay_is_on_by_default():
    """Enabled at 1-3 min (maintainer decision 2026-07-26). It previously
    defaulted to 0, which made it a dead switch nothing would ever use."""
    from backend import config

    assert config.INTER_SLOT_DELAY_MIN_S == 60.0
    assert config.INTER_SLOT_DELAY_MAX_S == 180.0


def test_inter_slot_delay_can_still_be_disabled(monkeypatch):
    """Setting both bounds to 0 must fully disable the gap — the escape
    hatch if pacing ever gets in the way of a debugging run."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 0.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 0.0)
    assert asyncio.run(poster_browser._inter_slot_delay()) == 0.0


# --- waiting progress event ----------------------------------------------


def test_delay_emits_a_waiting_event_before_sleeping(monkeypatch):
    """The event must be emitted BEFORE the sleep, or the UI only learns
    about the gap once it is already over."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 60.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 180.0)
    order = []
    events = []

    async def _tracking_sleep(d):
        order.append("slept")

    monkeypatch.setattr(asyncio, "sleep", _tracking_sleep)

    def _cb(slot, platform, status, detail):
        order.append("notified")
        events.append((slot, platform, status, detail))

    asyncio.run(poster_browser._inter_slot_delay(_cb, "B"))
    assert order == ["notified", "slept"], (
        "the waiting event must precede the sleep"
    )
    slot, platform, status, detail = events[0]
    assert (slot, platform, status) == ("B", "run", "waiting")
    assert 60 <= int(detail) <= 180, "event must carry the gap length"


def test_waiting_event_names_the_upcoming_slot(monkeypatch):
    """The countdown belongs to the account about to post, not the one that
    just finished."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 1.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 2.0)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    seen = []
    asyncio.run(poster_browser._inter_slot_delay(
        lambda s, p, st, d: seen.append(s), "C"))
    assert seen == ["C"]


def test_disabled_delay_emits_no_waiting_event(monkeypatch):
    """No gap, no countdown — a stale 0s countdown would be noise."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 0.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 0.0)
    seen = []
    asyncio.run(poster_browser._inter_slot_delay(
        lambda s, p, st, d: seen.append(s), "B"))
    assert seen == []


def test_broken_progress_callback_cannot_break_the_gap(monkeypatch):
    """Progress reporting must never take down a posting run (_notify
    contract)."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 1.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 2.0)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    def _explode(*a):
        raise RuntimeError("UI is gone")

    assert asyncio.run(poster_browser._inter_slot_delay(_explode, "B")) >= 1.0


def test_inter_slot_delay_waits_within_the_configured_window(monkeypatch):
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 60.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 180.0)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    for _ in range(200):
        assert 60.0 <= asyncio.run(poster_browser._inter_slot_delay()) <= 180.0


def test_inverted_delay_bounds_are_clamped_not_raised(monkeypatch):
    """A bad env value must never take down a posting run."""
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 300.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 60.0)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    assert asyncio.run(poster_browser._inter_slot_delay()) == 60.0


def test_negative_delay_config_is_treated_as_off(monkeypatch):
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", -5.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", -1.0)
    assert asyncio.run(poster_browser._inter_slot_delay()) == 0.0


async def _noop_sleep(d):
    return None


def test_waiting_is_not_recorded_as_a_post_outcome():
    """The UI renders `events` as per-platform results, so a run-level
    waiting event there would show as a bogus failure mark against the slot.
    It belongs in its own field."""
    from backend import main

    main._post_progress.events = []
    main._post_progress.current = None
    main._record_progress("B", "run", "waiting", "90")
    assert main._post_progress.events == [], (
        "waiting leaked into the events list and will render as a result mark"
    )
    assert main._post_progress.waiting["slot"] == "B"


def test_waiting_clears_when_the_next_slot_starts():
    """A stale countdown must not linger once posting resumes."""
    from backend import main

    main._post_progress.events = []
    main._record_progress("B", "run", "waiting", "90")
    main._record_progress("B", "instagram", "started")
    assert main._post_progress.waiting is None


def test_progress_endpoint_reports_remaining_seconds(monkeypatch):
    """Remaining time is computed server-side so the countdown cannot drift
    with the browser clock."""
    from backend import main

    main._post_progress.events = []
    main._record_progress("B", "run", "waiting", "90")
    main._post_progress.waiting["at"] = time.time() - 30
    payload = asyncio.run(main.post_progress())
    assert 59 <= payload["waiting"]["remaining"] <= 61
    assert payload["waiting"]["slot"] == "B"


def test_remaining_never_goes_negative():
    """An overrun gap must show 0s, not a negative countdown."""
    from backend import main

    main._post_progress.events = []
    main._record_progress("B", "run", "waiting", "10")
    main._post_progress.waiting["at"] = time.time() - 600
    assert asyncio.run(main.post_progress())["waiting"]["remaining"] == 0


def test_malformed_waiting_detail_does_not_raise():
    """detail is a string off the wire; a bad value must not break progress
    reporting mid-run."""
    from backend import main

    main._post_progress.events = []
    main._record_progress("B", "run", "waiting", "not-a-number")
    assert main._post_progress.waiting["seconds"] == 0.0


def test_frontend_renders_the_countdown():
    """Source-level (browser JS is not E2E-testable here): the UI must read
    the waiting field and show a countdown rather than 'waiting…'."""
    html = (pathlib.Path(__file__).parent.parent
            / "frontend" / "index.html").read_text()
    assert "p.waiting && p.waiting.slot === slot" in html
    assert "p.waiting.remaining" in html


def test_delay_runs_between_slots_but_not_before_the_first():
    """A run must not open with a dead wait, and must not end with one
    either — the gap belongs between accounts."""
    # Module source, not post_all's: the conftest tripwire monkeypatches
    # post_all with a stub, so getsource on the attribute returns the stub.
    src = inspect.getsource(poster_browser)
    assert "if i > 0:" in src
    assert "await _inter_slot_delay(progress_cb, s[\"slot\"])" in src
