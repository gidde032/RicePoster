"""
Timing jitter for browser automation (RESEARCH-platform-detection.md, F4).

Every wait in the posting flow was a fixed integer constant, so inter-action
timing was identical on every run of every account — a stable,
machine-precise rhythm that no human produces.

**The floor rule.** CLAUDE.md § Intentional design protects long sleeps and
generous timeouts as deliberate: platform UIs are unstable and the
maintainer's machine is under load. So jitter is only ever added *above* an
existing wait, never in place of it. `sleep_jittered(2)` waits at least 2
seconds, exactly as `asyncio.sleep(2)` did. A change here that could return
less than `base` is a bug, not a tuning decision — see
tests/test_jitter.py.
"""

import asyncio
import random
import re


def jittered_duration(base: float, spread: float | None = None) -> float:
    """Return `base` plus a random margin. Never less than `base`.

    `spread` defaults to 40% of the base wait (with a 0.5s minimum for very
    short waits), which keeps the added latency proportional: a 0.5s pause
    does not grow into a 3s one, and a 10s backoff gains a meaningful spread
    rather than a negligible one.
    """
    if base < 0:
        raise ValueError(f"base wait must be non-negative, got {base}")
    if spread is None:
        spread = max(0.5, base * 0.4)
    if spread < 0:
        raise ValueError(f"spread must be non-negative, got {spread}")
    return base + random.uniform(0, spread)


async def sleep_jittered(base: float, spread: float | None = None) -> float:
    """Drop-in replacement for `asyncio.sleep(base)` that waits a
    human-irregular amount of time *at least* as long. Returns the duration
    actually slept, which is what makes this testable without real waiting.
    """
    duration = jittered_duration(base, spread)
    await asyncio.sleep(duration)
    return duration


# --- Caption typing cadence (issue #2) --------------------------------------
#
# Both posting flows typed captions with a fixed 60 ms inter-key delay, so a
# long caption produced a machine-perfect rhythm no human makes — the same
# artefact F4 removed from every other wait in the flow, left behind on the
# one action that generates the most events.
#
# Scope note: this is effectively an Instagram change. TikTok's normal path
# inserts the caption atomically via keyboard.insert_text (deliberately — no
# per-key events for '#'/'@' means no autocomplete interception), and only
# reaches sequential typing on its verification retry.
#
# The floor rule applies here exactly as it does to sleeps: the per-key delay
# is never below the 60 ms the flows have always used, and the pauses between
# chunks only ever add time. Typing must not get faster.

TYPING_DELAY_FLOOR_MS = 60
TYPING_DELAY_SPREAD_MS = 40

# Between-chunk pauses. Floor is deliberately small: the pause is a hesitation
# between words, not a wait for the page.
TYPING_PAUSE_BASE_S = 0.05
TYPING_PAUSE_SPREAD_S = 0.65

# Words per uninterrupted run. Short enough that the cadence varies several
# times within a normal caption, long enough not to turn a caption into
# hundreds of separate Playwright calls.
TYPING_CHUNK_MIN_WORDS = 1
TYPING_CHUNK_MAX_WORDS = 3


def caption_chunks(text: str) -> list[str]:
    """Split a caption into runs to be typed without pausing.

    **Every chunk carries its own trailing whitespace, so a pause always
    happens with the caret sitting after a space.** That is a safety
    requirement, not a stylistic one, and the direction matters:

    Instagram's caption box pops an autocomplete dropdown as soon as a
    '#hashtag' or '@mention' starts, and keeps it open until the token is
    terminated. The dangerous moment to stop typing is therefore *any* point
    where the last character typed is still part of an open token — including
    the instant just after the final letter of `#latergram`, before its
    closing space. Pausing there is precisely when the dropdown is fully
    rendered and able to swallow or rewrite the keystrokes that follow.

    Attaching whitespace to the *front* of a chunk looks equivalent and is
    not: the pause fires before the chunk, so the space that would close the
    previous token has not been typed yet and every pause lands on an open
    token. That was the original implementation and the bug this shape fixes;
    `test_pauses_never_land_on_an_unterminated_token` asserts the timing
    directly rather than the string layout, because the layout test that
    replaced it could not fail.

    Concatenating the result reproduces `text` byte for byte — including
    leading and trailing whitespace and any newlines — so chunking can never
    alter the caption that gets posted. `test_chunks_always_rejoin_exactly`
    pins that over a corpus including emoji, newlines and CJK text.
    """
    # Each token is one word plus the whitespace that FOLLOWS it, so a chunk
    # boundary always sits after a terminator.
    leading = re.match(r"\s*", text).group()
    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        # All-whitespace or empty: one chunk, typed as-is. Preserves the
        # single press_sequentially call the flows made before.
        return [text]

    chunks = []
    i = 0
    while i < len(tokens):
        size = random.randint(TYPING_CHUNK_MIN_WORDS, TYPING_CHUNK_MAX_WORDS)
        chunks.append("".join(tokens[i:i + size]))
        i += size
    if leading:
        chunks[0] = leading + chunks[0]
    return chunks


def typing_delay_ms() -> float:
    """Per-keystroke delay for one chunk, in milliseconds.

    Never below TYPING_DELAY_FLOOR_MS — a value under it would make typing
    faster than the current safety baseline, which is a bug rather than a
    tuning decision (issue #2, and the same floor rule as `sleep_jittered`).
    """
    return jittered_duration(TYPING_DELAY_FLOOR_MS, TYPING_DELAY_SPREAD_MS)


async def type_with_jitter(field, text: str) -> None:
    """Type `text` into a Playwright locator with a human-irregular cadence.

    Each chunk is typed by the same `press_sequentially` call the flows used
    before, so unicode, emoji and newlines behave exactly as they did; only
    the delay varies and short pauses appear at word boundaries. The pause
    comes *before* each chunk after the first, so nothing is added before the
    first keystroke or after the last.
    """
    for index, chunk in enumerate(caption_chunks(text)):
        if index:
            await sleep_jittered(TYPING_PAUSE_BASE_S, TYPING_PAUSE_SPREAD_S)
        await field.press_sequentially(chunk, delay=typing_delay_ms())
