"""The per-slot device identity must describe a machine that could exist.

Phase 2 slice 3, 2026-07-27. `tools/probe_fingerprint.py` measured what a
launched Chrome actually reports and found the identity self-contradictory in
two ways, both invisible from source alone:

  * `window.screen` reported the viewport's own size, so
    `screen.height == innerHeight`. Real hardware always has window furniture
    between the page area and the display — `device_identity` even subtracts
    111px for it, but the display was reported as the already-subtracted
    number, cancelling the effort out.
  * `devicePixelRatio` was 1 while the dimensions claimed a MacBook Pro. Every
    Apple laptop panel is Retina and reports 2.

That is the self-contradictory-spoof failure mode, inside the one mechanism
built to stop the accounts linking to each other — worse than being honestly
automated, per `practices/detection-avoidance.md`.

Re-run `python tools/probe_fingerprint.py` after touching any of this; the
probe verifies the real browser offline, with no live traffic.
"""

import inspect

import pytest

from backend import instagram_browser
from backend.config import SLOT_IDS
from backend.device_identity import (
    CHROME_HEIGHT_PX,
    DISPLAYS,
    VIEWPORTS,
    scale_factor_for_slot,
    screen_for_slot,
    viewport_for_slot,
)

# The viewports slots A/B/C had before the display table was introduced.
# Pinned because `device_identity`'s own docstring is emphatic that a
# fingerprint changing between runs is worse than one that is merely shared:
# changing these silently gives live accounts new hardware.
HISTORICAL_VIEWPORTS = {
    0: {"width": 1512, "height": 871},
    1: {"width": 1440, "height": 789},
    2: {"width": 1728, "height": 1006},
    3: {"width": 1680, "height": 939},
}


@pytest.mark.parametrize("index", sorted(HISTORICAL_VIEWPORTS))
def test_existing_slots_did_not_get_new_hardware(index):
    """Adding the display table must not have moved any configured slot."""
    assert VIEWPORTS[index] == HISTORICAL_VIEWPORTS[index]


@pytest.mark.parametrize("index", range(len(DISPLAYS)))
def test_page_area_fits_inside_its_display(index):
    """screen.height > innerHeight, always — the pre-fix defect."""
    display, viewport = DISPLAYS[index], VIEWPORTS[index]
    assert viewport["height"] < display["height"], (
        f"display {index} reports a page area {viewport['height']}px tall "
        f"inside a {display['height']}px display — no window furniture, which "
        f"real hardware cannot produce"
    )
    assert viewport["width"] == display["width"], (
        "a maximised window spans the full display width"
    )


@pytest.mark.parametrize("index", range(len(DISPLAYS)))
def test_viewport_is_derived_from_its_display(index):
    """The two tables cannot drift: one is computed from the other."""
    assert (
        VIEWPORTS[index]["height"]
        == DISPLAYS[index]["height"] - CHROME_HEIGHT_PX
    )


@pytest.mark.parametrize("index", range(len(DISPLAYS)))
def test_laptop_panels_claim_retina(index):
    """An Apple laptop panel at ratio 1 describes a machine that doesn't exist.

    Keyed on the explicit `kind`, not on size: a 1920-wide external monitor is
    wider than every MacBook in the table, so size cannot tell them apart.
    """
    display = DISPLAYS[index]
    assert display["kind"] in ("laptop", "external")
    if display["kind"] == "laptop":
        assert display["scale"] == 2, (
            f"display {index} ({display['width']}x{display['height']}) is an "
            f"Apple laptop panel but reports devicePixelRatio "
            f"{display['scale']}"
        )
    assert display["scale"] >= 1


def test_configured_slots_get_distinct_screens():
    """Per-slot identity must differ on the display too, not just the viewport."""
    screens = [tuple(sorted(screen_for_slot(s).items())) for s in SLOT_IDS]
    assert len(set(screens)) == len(screens), f"screen collision: {screens}"


@pytest.mark.parametrize("slot", SLOT_IDS)
def test_screen_and_scale_are_stable_per_slot(slot):
    """A device that changes between runs is the failure F3 exists to avoid."""
    first_screen = screen_for_slot(slot)
    first_scale = scale_factor_for_slot(slot)
    for _ in range(5):
        assert screen_for_slot(slot) == first_screen
        assert scale_factor_for_slot(slot) == first_scale


@pytest.mark.parametrize("slot", SLOT_IDS)
def test_each_slot_is_internally_consistent(slot):
    """The three values a slot reports must agree with each other."""
    viewport, screen = viewport_for_slot(slot), screen_for_slot(slot)
    assert screen["height"] > viewport["height"]
    assert screen["width"] == viewport["width"]
    assert scale_factor_for_slot(slot) in (1, 2)


def test_returned_dicts_are_defensive_copies():
    """A caller mutating a result must not corrupt the shared table."""
    screen = screen_for_slot(SLOT_IDS[0])
    screen["width"] = 1
    assert screen_for_slot(SLOT_IDS[0])["width"] != 1


def test_instagram_browser_passes_screen_and_scale():
    """Source-level: the values must reach the launch call.

    Playwright flows are not E2E-tested here, so this asserts the wiring and
    `tools/probe_fingerprint.py` verifies the runtime effect.
    """
    src = inspect.getsource(instagram_browser)
    assert "screen=screen_for_slot(account_key)" in src
    assert "device_scale_factor=scale_factor_for_slot(account_key)" in src
