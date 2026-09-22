"""Slot A posting-path hardening (2026-09-13): D2, D4, D5.

Behavioral tests against the recording fakes. They assert what the flow does
to the browser, not what its source text looks like, so a later refactor is
free to move code as long as the browser sees the same actions.

* D2 — device identity follows the mode: native when headed, synthetic per
  slot when headless.
* D4 — the Create click, the Post menu click, and the caption commit are
  native Playwright actions. No `page.evaluate` runs on the happy path.
* D5 — the flow scrolls the feed for a bounded, configurable span before the
  composer opens, and not at all when disabled.
"""

import random

import pytest

from backend import config, instagram_browser
from backend.device_identity import (
    scale_factor_for_slot,
    screen_for_slot,
    viewport_for_slot,
)
from tests.browser_trace import run_traced
from tests.test_poster_internals import CAPTION, _ig_script, media  # noqa: F401


def _normal_run(monkeypatch, media):
    return run_traced(
        monkeypatch,
        instagram_browser,
        lambda: instagram_browser.post_media("A", media, CAPTION, "reel", headless=True),
        _ig_script(),
    )


# --- D2: identity by mode ---------------------------------------------------

def test_headed_context_uses_native_identity():
    assert instagram_browser._identity_kwargs("A", headless=False) == {"no_viewport": True}


def test_headless_context_keeps_synthetic_per_slot_identity():
    kwargs = instagram_browser._identity_kwargs("A", headless=True)
    assert kwargs == {
        "viewport": viewport_for_slot("A"),
        "screen": screen_for_slot("A"),
        "device_scale_factor": scale_factor_for_slot("A"),
    }
    assert "no_viewport" not in kwargs


def test_launch_args_request_a_maximised_window(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    rec = _normal_run(monkeypatch, media)
    launch = next(line for line in rec.lines if line.startswith("chromium.launch_persistent_context("))
    assert "'--start-maximized'" in launch


# --- D4: trusted input on the happy path -------------------------------------

def test_happy_path_runs_no_page_evaluate(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """JavaScript-dispatched clicks and synthetic events fire with isTrusted
    false. The happy path must not evaluate any script at all."""
    rec = _normal_run(monkeypatch, media)
    assert rec.lines[-1] == "RETURN 'ig_post_ok_A'"
    assert not [line for line in rec.lines if line.startswith("page.evaluate(")]


def test_create_and_post_clicks_are_native_and_hovered(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    rec = _normal_run(monkeypatch, media)
    create = [line for line in rec.lines if 'svg[aria-label="New post"]' in line]
    assert any(line.endswith(".hover()") for line in create)
    assert any(line.endswith(".click()") for line in create)
    assert rec.lines.index(next(l for l in create if l.endswith(".hover()"))) < \
        rec.lines.index(next(l for l in create if l.endswith(".click()")))
    post = [line for line in rec.lines if "a[href=\"#\"]" in line and "Post" in line]
    assert any(line.endswith(".hover()") for line in post)
    assert any(line.endswith(".click()") for line in post)


def test_non_anchor_post_menu_item_falls_back_to_exact_text(
    monkeypatch, tmp_sessions, media, allow_browser_post_media,
):
    """Instagram can render the visible Post row as nested spans and divs,
    with no anchor or href anywhere in the menu-item chain."""
    script = _ig_script(counts={
        'a[href="#"]': 0,
        "get_by_text('Post', exact=True)": 2,
    })
    rec = run_traced(
        monkeypatch,
        instagram_browser,
        lambda: instagram_browser.post_media("A", media, CAPTION, "reel", headless=True),
        script,
    )

    assert rec.lines[-1] == "RETURN 'ig_post_ok_A'"
    fallback = [line for line in rec.lines if "get_by_text('Post', exact=True)" in line]
    assert any("wait_for(state='visible', timeout=10000)" in line for line in fallback)
    assert any(line.endswith(".hover()") for line in fallback)
    assert any(line.endswith(".click()") for line in fallback)


def test_caption_commit_is_native_blur_then_focus(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    rec = _normal_run(monkeypatch, media)
    typed = max(i for i, line in enumerate(rec.lines) if ".press_sequentially(" in line)
    shared = next(i for i, line in enumerate(rec.lines) if "has_text='Share'" in line)
    between = rec.lines[typed:shared]
    blur = next(i for i, line in enumerate(between) if line.endswith(".blur()"))
    focus = next(i for i, line in enumerate(between) if line.endswith(".focus()"))
    assert blur < focus
    assert not [line for line in between if line.startswith("page.evaluate(")]


def test_missing_create_button_still_reports_expired_session(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """The password probe is the one evaluate that remains, and only on the
    not-found path. It must still turn a login form into the expired error."""
    script = _ig_script()
    script.counts["New post"] = 0
    script.evaluate['input[type="password"]'] = True
    rec = run_traced(
        monkeypatch,
        instagram_browser,
        lambda: instagram_browser.post_media("A", media, CAPTION, "reel", headless=True),
        script,
    )
    assert "session expired" in rec.lines[-1]


# --- D5: feed dwell -----------------------------------------------------------

def test_feed_dwell_defaults():
    assert config.FEED_DWELL_MIN_S == 30.0
    assert config.FEED_DWELL_MAX_S == 60.0


def test_feed_dwell_disabled_when_both_bounds_are_zero(monkeypatch):
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MIN_S", 0.0)
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MAX_S", 0.0)
    assert instagram_browser.feed_dwell_seconds() == 0.0
    assert instagram_browser.feed_dwell_plan(0.0) == []


def test_feed_dwell_draws_inside_the_configured_range(monkeypatch):
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MIN_S", 30.0)
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MAX_S", 60.0)
    random.seed(7)
    draws = [instagram_browser.feed_dwell_seconds() for _ in range(300)]
    assert min(draws) >= 30.0
    assert max(draws) <= 60.0
    assert max(draws) - min(draws) > 10.0


def test_feed_dwell_clamps_a_min_above_max(monkeypatch):
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MIN_S", 90.0)
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MAX_S", 40.0)
    for _ in range(50):
        assert instagram_browser.feed_dwell_seconds() == pytest.approx(40.0)


def test_feed_dwell_plan_covers_the_total_with_floored_pauses():
    random.seed(11)
    plan = instagram_browser.feed_dwell_plan(45.0)
    assert plan
    pauses = [pause for _, pause in plan]
    assert sum(pauses) >= 45.0
    assert sum(pauses[:-1]) < 45.0
    assert all(pause >= instagram_browser.FEED_SCROLL_PAUSE_BASE_S for pause in pauses)
    lo, hi = instagram_browser.FEED_SCROLL_STEP_PX
    back_lo, back_hi = instagram_browser.FEED_SCROLL_BACK_PX
    for delta, _ in plan:
        assert (lo <= delta <= hi) or (-back_hi <= delta <= -back_lo)
    assert plan[0][0] > 0


def test_feed_dwell_runs_between_popups_and_create(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MIN_S", 5.0)
    monkeypatch.setattr(instagram_browser, "FEED_DWELL_MAX_S", 5.0)
    rec = _normal_run(monkeypatch, media)
    wheels = [i for i, line in enumerate(rec.lines) if line.startswith("mouse.wheel(")]
    assert wheels
    last_popup = max(i for i, line in enumerate(rec.lines) if "optional cookies" in line)
    create_hover = next(i for i, line in enumerate(rec.lines)
                        if 'svg[aria-label="New post"]' in line and line.endswith(".hover()"))
    assert last_popup < wheels[0]
    assert wheels[-1] < create_hover
    assert rec.lines[-1] == "RETURN 'ig_post_ok_A'"


def test_feed_dwell_skipped_when_disabled(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    rec = _normal_run(monkeypatch, media)
    assert not [line for line in rec.lines if line.startswith("mouse.wheel(")]
