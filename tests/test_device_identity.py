"""
Tests for per-slot device identity (F3).

Source: RESEARCH-platform-detection.md Finding 3 (2026-07-25) —
every Instagram slot shared one byte-identical device fingerprint, which is
the best explanation for accounts on such a setup being flagged together
rather than one at a time.

Unlike the other detection fixes, this module is pure logic, so these are
real behavioural tests rather than source-level assertions.
"""

import inspect as _inspect
import pathlib
import subprocess
import sys

from backend import instagram_browser, tiktok_browser
from backend.config import SLOT_IDS
from backend.device_identity import VIEWPORTS, viewport_for_slot


def test_configured_slots_get_distinct_viewports():
    """The core requirement. A hash-based mapping was tried first and
    collided on the default A/B/C config (B and C landed on the same
    viewport), which silently defeats the whole fix — hence positional
    assignment. This test is what caught that."""
    viewports = [tuple(sorted(viewport_for_slot(s).items())) for s in SLOT_IDS]
    assert len(set(viewports)) == len(SLOT_IDS), (
        f"Configured slots {SLOT_IDS} do not all get distinct viewports: "
        f"{viewports}. Accounts sharing a device fingerprint is the exact "
        "condition this module exists to prevent."
    )


def test_viewport_is_stable_across_calls():
    """A fingerprint that changes run-to-run is worse than one that is
    merely shared."""
    for slot in SLOT_IDS:
        first = viewport_for_slot(slot)
        assert all(viewport_for_slot(slot) == first for _ in range(5))


def test_viewport_is_stable_across_processes():
    """Regression guard for the builtin-hash trap: hash() on a str is salted
    by PYTHONHASHSEED, so a hash()-based mapping would silently give each
    slot a different device on every server restart. Runs a fresh
    interpreter with an explicit non-default seed to prove stability."""
    code = (
        "from backend.device_identity import viewport_for_slot;"
        "print(viewport_for_slot('A'))"
    )
    runs = []
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env={"PYTHONHASHSEED": seed, "PATH": ""},
            cwd=str(pathlib.Path(__file__).parent.parent),
        )
        assert out.returncode == 0, out.stderr
        runs.append(out.stdout.strip())
    assert len(set(runs)) == 1, (
        f"Viewport for slot A varies across interpreter runs: {runs}"
    )


def test_all_viewports_come_from_the_table():
    for slot in list(SLOT_IDS) + ["ZZ", "unknown-slot"]:
        assert viewport_for_slot(slot) in [dict(v) for v in VIEWPORTS]


def test_unconfigured_slot_still_resolves():
    """session_manager can be invoked with an ad-hoc slot id; it must not
    raise, even though such a slot has no position in SLOT_IDS."""
    vp = viewport_for_slot("burner")
    assert set(vp) == {"width", "height"}


def test_returned_viewport_is_a_copy():
    """Callers hand this dict to Playwright; a mutation must not corrupt the
    shared table for every later slot."""
    vp = viewport_for_slot(SLOT_IDS[0])
    vp["width"] = 1
    assert viewport_for_slot(SLOT_IDS[0])["width"] != 1


def test_viewports_are_plausible_desktop_sizes():
    """A synthetic-looking viewport is itself a fingerprint. Guard against a
    future edit adding a mobile or absurd size to the table."""
    for vp in VIEWPORTS:
        assert 1200 <= vp["width"] <= 2560, vp
        assert 700 <= vp["height"] <= 1600, vp
        assert vp["width"] > vp["height"], f"{vp} is portrait — not a desktop"


# --- Wiring: Instagram only, deliberately -------------------------------


def test_instagram_uses_per_slot_viewport():
    """Source-level: the IG context must derive its viewport per slot rather
    than pinning the old shared 1280x900."""
    src = _inspect.getsource(instagram_browser)
    assert "viewport_for_slot(account_key)" in src
    assert '"width": 1280, "height": 900' not in src, (
        "instagram_browser still pins the old shared viewport."
    )


def test_tiktok_viewport_left_alone_by_design():
    """F3 is scoped to Instagram on purpose. TikTok has shown no detection
    problems, so its forced 1280x900 viewport is left as-is — F3 answers an
    Instagram concern rather than being a uniformity goal.

    This test previously justified the scoping by the TikTok edge-crop
    investigation ("changing the viewport would confound the diagnosis").
    That investigation was withdrawn on 2026-07-27; the scoping decision
    stands on its own. If a future slice intentionally widens F3 to TikTok,
    delete this test in that commit rather than quietly editing around it."""
    src = _inspect.getsource(tiktok_browser)
    assert '"width": 1280, "height": 900' in src
    assert "viewport_for_slot" not in src
