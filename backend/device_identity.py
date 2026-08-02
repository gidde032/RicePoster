"""
Per-slot device identity (RESEARCH-platform-detection.md, F3).

Every Instagram slot used to launch with a byte-identical device
fingerprint: same viewport, same launch args, same UA, same IP, posting
back-to-back within one run. Instagram links accounts by device
fingerprint, so several accounts operating from one identical synthetic
device in immediate succession is the account-farm pattern its integrity
systems exist to catch — and it is a pattern that gets accounts actioned
together rather than one at a time.

This module gives each slot a distinct, plausible desktop viewport.

**Stability is the whole point.** A fingerprint that changes run-to-run is
worse than one that is merely shared — it looks like a device that
physically morphs between sessions. So the viewport is derived
deterministically from the slot id via a stable hash. That is stronger than
persisting a random choice to disk: it survives the session directory being
cleared or the profile being rebuilt, and there is no second source of
truth to fall out of sync.

Assignment is **positional** — a slot's index in the configured slot list
selects its viewport. The obvious alternative, hashing the slot id, was
tried first and rejected: with a 5-entry table the default A/B/C config
collided, putting B and C on an identical viewport. That silently defeats
the entire purpose of the fix, since the whole point is that the accounts
must not look like one device. Positional assignment is collision-free by
construction for any slot count up to `len(VIEWPORTS)`.

The cost of positional assignment is that reordering `ACCOUNT_SLOTS` in
credentials.env reassigns devices. That is a deliberate, rare config action
rather than something that happens on every restart, so it is the better
trade — but it is a real edge, and renaming or reordering slots should be
treated as giving those accounts new hardware.

Scope: Instagram only. TikTok's forced 1280x900 viewport is deliberately
left alone — TikTok has shown no detection problems, so there is nothing
here for per-slot identity to solve. F3 answers a specific Instagram
concern; it is not a uniformity goal to be applied for its own sake.
"""

import hashlib

from backend.config import SLOT_IDS

# Height of the macOS Chrome window furniture (tab strip + toolbar) that sits
# between the display and the page area. The viewport is the display height
# minus this, which is what a real maximised window produces.
CHROME_HEIGHT_PX = 111

# Real macOS display sizes in logical points, with the pixel ratio each one
# actually reports. The *display* is the source of truth and the viewport is
# derived, because the two must agree: reporting a page area exactly equal to
# the whole display is geometrically impossible on real hardware (there is
# always window furniture in between) and was a live tell until 2026-07-27 —
# see the probe evidence in AUDIT-phase2.md § Slice 3.
#
# `scale` is `devicePixelRatio`. Every Apple laptop display is Retina and
# reports 2; reporting 1 while claiming MacBook Pro dimensions is the
# self-contradictory-spoof failure mode. The 1080p entry is an external
# monitor, where 1 is correct and adds realistic variety.
#
# Do not reorder or remove entries casually: the index is derived from the
# slot id, so changing this list reassigns slots to different devices, which
# is the run-to-run instability described above. Appending is safe for
# existing slots only if the length changes — see _index_for_slot.
# `kind` is not decoration: it is what makes the Retina rule checkable. A
# laptop panel at ratio 1 is a contradiction, an external panel at ratio 1 is
# ordinary, and the two are not distinguishable by size alone — a 1920-wide
# external monitor is wider than every MacBook in this table.
DISPLAYS: tuple[dict[str, object], ...] = (
    {"width": 1512, "height": 982,  "scale": 2, "kind": "laptop"},   # MBP 14"
    {"width": 1440, "height": 900,  "scale": 2, "kind": "laptop"},   # MBA 13"
    {"width": 1728, "height": 1117, "scale": 2, "kind": "laptop"},   # MBP 16"
    {"width": 1680, "height": 1050, "scale": 2, "kind": "laptop"},   # MBP 15"
    # Was 1280x800, whose derived viewport (689px) fell below the plausible-
    # desktop floor in test_device_identity. 1080p is the commonest external
    # panel and genuinely non-Retina, so it fits the table better anyway.
    # Index 4 is unused by any configured slot, so nothing changed device.
    {"width": 1920, "height": 1080, "scale": 1, "kind": "external"},
)

# Derived, not hand-maintained: a second hand-written table would let the
# viewport and its display drift back out of agreement, which is the exact
# defect this structure exists to make unrepresentable. Entries 0-3 keep the
# viewport values they had before the display table was introduced, so no
# configured slot changed device.
VIEWPORTS: tuple[dict[str, int], ...] = tuple(
    {"width": int(d["width"]), "height": int(d["height"]) - CHROME_HEIGHT_PX}
    for d in DISPLAYS
)


def _validate_capacity(slot_ids) -> None:
    """Refuse to run if there are more slots than distinct fingerprints.

    `ACCOUNT_SLOTS` is user-configurable, so a 6th slot was reachable by
    config alone. The positional index used to wrap via `% len(VIEWPORTS)`,
    which handed slot 6 slot A's exact viewport — reconstructing the shared
    device fingerprint this whole module exists to destroy, silently, at the
    moment the maintainer scaled up (review 2026-07-26, finding #4).

    Note the trap this guard closes: `test_configured_slots_get_distinct_
    viewports` only iterates *currently configured* slots, so it keeps
    passing after the collision is introduced. A silent wrap is strictly
    worse than a refusal to start, because the failure it causes is an
    account ban weeks later with no local symptom.
    """
    if len(slot_ids) > len(VIEWPORTS):
        raise ValueError(
            f"ACCOUNT_SLOTS configures {len(slot_ids)} slots "
            f"({', '.join(slot_ids)}) but device_identity.VIEWPORTS defines "
            f"only {len(VIEWPORTS)} distinct device fingerprints. Slots past "
            f"the {len(VIEWPORTS)}th would share an earlier slot's "
            f"fingerprint, which is the account-linkage pattern F3 exists to "
            f"prevent. Add more viewports to backend/device_identity.py, or "
            f"reduce ACCOUNT_SLOTS."
        )


_validate_capacity(SLOT_IDS)


def _index_for_slot(slot: str) -> int:
    """Map a slot id to a stable index into VIEWPORTS.

    Positional where possible: a configured slot's position in SLOT_IDS is
    its index, which guarantees distinct viewports for every configured slot
    up to len(VIEWPORTS).

    A slot not present in SLOT_IDS (an ad-hoc session_manager invocation, a
    test fixture) falls back to a hash of the id. That fallback can collide,
    but it only affects slots outside the configured set, where cross-account
    linkage is not the concern. hashlib rather than the builtin hash():
    hash() on a str is salted by PYTHONHASHSEED and differs between
    interpreter runs, which would make the device change on every restart —
    exactly the failure this module exists to avoid.
    """
    if slot in SLOT_IDS:
        # No modulo here on purpose: _validate_capacity has already
        # guaranteed the index is in range, and a wrap would be a silent
        # fingerprint collision rather than a bounds fix.
        return SLOT_IDS.index(slot)
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()
    return int(digest, 16) % len(VIEWPORTS)


def viewport_for_slot(slot: str) -> dict[str, int]:
    """Return the stable viewport for one account slot.

    Returns a fresh dict each call so a caller mutating the result cannot
    corrupt the shared table.
    """
    return dict(VIEWPORTS[_index_for_slot(slot)])


def screen_for_slot(slot: str) -> dict[str, int]:
    """Return the display size that the slot's viewport sits inside.

    Must be passed to Playwright alongside the viewport. Without it,
    `window.screen` reports the viewport's own dimensions, so
    `screen.height == innerHeight` — a page area exactly filling the display,
    with no room for the window furniture the viewport height already
    subtracts. Real hardware cannot produce that.
    """
    display = DISPLAYS[_index_for_slot(slot)]
    return {"width": int(display["width"]), "height": int(display["height"])}


def scale_factor_for_slot(slot: str) -> int:
    """Return `devicePixelRatio` for the slot's display.

    Playwright defaults to 1. Every Apple laptop panel is Retina and reports
    2, so a context claiming MacBook Pro dimensions at ratio 1 describes a
    machine that does not exist.
    """
    return int(DISPLAYS[_index_for_slot(slot)]["scale"])
