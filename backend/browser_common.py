"""Helpers shared by both platform browser clients.

`instagram_browser` and `tiktok_browser` are deliberately separate modules —
the platforms' web UIs have nothing in common and are expected to drift apart.
But a few helpers are *not* platform-specific: they encode decisions about how
a result is labelled and how an aborted login is cleaned up, and those
decisions must stay identical across platforms or the UI and the session store
start disagreeing with themselves.

These lived as byte-identical copies in both modules until 2026-07-29
(tech-debt audit, BE-3). They keep their underscore names because both modules
re-export them by import, and existing tests reach them as module attributes
on `instagram_browser` / `tiktok_browser`.

Platform-specific helpers do NOT belong here. `_captions_match` was the
standing example of that rule until Batch 6: it lived in `tiktok_browser`
because only TikTok verified its caption. Instagram now does too (maintainer
decision 2026-07-30 — its caption box is a Draft.js contenteditable with the
same autocomplete hazards, and it was publishing unverified), so the helper
and the redaction marker that pairs with it are genuinely shared and have
moved here. See `test_caption_verification_is_now_shared`.
"""

import shutil
import unicodedata
from pathlib import Path

# Marker that separates a caption-verification error's explanation from the
# editor text it quotes. `poster_browser._notification_safe` truncates from
# here so page/caption content can never reach a phone push. Both platforms
# raise it, so both are covered by that redaction.
EDITOR_MARKER = "Editor contained"


# Zero-width characters a contenteditable can inject on its own. Draft.js and
# similar editors use them as block/entity padding, so they appear in a
# read-back that is otherwise a faithful copy of what was typed.
_INVISIBLE_CHARS = str.maketrans("", "", "​‌‍﻿")


def _captions_match(expected: str, actual: str) -> bool:
    """True when the editor text matches the intended caption.

    Three classes of difference are ignored, because all three are the editor
    re-rendering rather than the caption being wrong:

    * **Whitespace.** Contenteditable editors re-render newlines and spacing
      unpredictably. Note this is whitespace-*insensitive*, not
      whitespace-normalising: "hello world" and "helloworld" compare equal.
      That is deliberate and long-standing — the check exists to catch a
      spliced filename or a swallowed hashtag, not to police spacing.
    * **Unicode composition.** Chromium normalises inserted text to NFC, so a
      caption containing decomposed accents (NFD) reads back composed and a
      byte comparison would call a perfectly good caption a mismatch.
    * **Zero-width characters** the editor inserts as its own padding.

    The consequence of getting this wrong is asymmetric, which is why it is
    generous: a false mismatch abandons a post whose media has already
    uploaded, while a false match only lets through a caption differing by
    invisible characters.
    """
    def norm(s: str) -> str:
        return unicodedata.normalize("NFC", "".join(s.split()).translate(_INVISIBLE_CHARS))
    return norm(expected) == norm(actual)


def _post_id(prefix: str, account_key: str, confirmed: bool) -> str:
    """Result IDs distinguish a confirmed post from one where the success
    element never appeared — the post may or may not be live, so the UI
    must not show a plain checkmark for it."""
    status = "ok" if confirmed else "unconfirmed"
    return f"{prefix}_{status}_{account_key}"


def _selector_chain_error(what: str, attempts: list[tuple[str, int | None]]) -> str:
    """Build the message for an exhausted fallback selector chain.

    A chain that gives up used to raise a bare "Could not find X", discarding
    the element counts it had just measured. Posting runs unattended on live
    accounts, so that turned every selector break — the most common failure
    mode, since the platforms reskin without notice — into a manual forensics
    session with no starting point.

    `attempts` is (selector, elements_found), where None means the locator call
    itself raised rather than returning a count. Ordered as tried, so the last
    entry is the last fallback.
    """
    if not attempts:
        return f"Could not find {what}: no selectors were tried."
    detail = ", ".join(
        f"{selector!r}→{'raised' if count is None else count}"
        for selector, count in attempts
    )
    return (
        f"Could not find {what}: all {len(attempts)} fallback selectors failed. "
        f"Tried (selector→elements found): {detail}"
    )


def url_matches_login_markers(url: str, markers: tuple[str, ...]) -> bool:
    """True when a post-navigation URL sits behind a platform's login wall.

    The marker sets themselves are platform knowledge and live in
    `instagram_browser` / `tiktok_browser` (tech-debt audit BE-23, issue #28);
    only the matching rule is shared, because "expired" must mean the same
    thing to the health check and to the posting path.

    Case-insensitive, and an empty URL is never a match — a navigation that
    produced no URL is "unknown", not "expired", and the callers treat those
    very differently.
    """
    if not url:
        return False
    u = url.lower()
    return any(marker in u for marker in markers)


def _resolve_login_outcome(session_dir: Path, answer: str, had_profile_before: bool) -> bool:
    """Decide whether to keep the profile dir after a manual login attempt.
    Typing 'abort' discards a profile dir this attempt created, so an
    aborted login can't masquerade as a saved session. Returns True if kept."""
    if answer.strip().lower() == "abort" and not had_profile_before:
        shutil.rmtree(session_dir, ignore_errors=True)
        return False
    return True
