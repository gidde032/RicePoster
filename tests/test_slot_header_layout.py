"""Rendered-layout guard for the slot header (per-slot platform toggles).

"Disabled" is longer than "Ready", and with two of them plus the
media badge the header used to overflow its card at medium widths, pushing the
labels and the ⋯ menu into the neighbouring slot. This renders the real page
in Chrome with every /api call faked (nothing touches a server, queue, or
account state) and measures the result at several viewport widths.

Skipped where Playwright or a local Chrome is unavailable (e.g. CI).
"""

import json
import re
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

HTML_PATH = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
HTML = HTML_PATH.read_text()

ACCOUNTS = [{"slot": s, "account_id": s, "name": f"Account {s}"} for s in "ABC"]
STATE = {
    "schema_version": 1, "active_account_ids": list("ABC"), "rosters": {},
    "caption_defaults": {}, "device_profiles": {},
    # A: both off (the worst case), B: TikTok off, C: both on.
    "disabled_platforms": {"A": ["instagram", "tiktok"], "B": ["tiktok"]},
}
SESSIONS = {s: {"instagram": True, "tiktok": True} for s in "ABC"}

# Wide three-column (cards stop growing at ~488px from here, and even the
# worst-case header fits whole), medium and tight three-column (just above the
# 1080px single-column breakpoint), single column, and phone.
WIDE, TIGHT = 2000, 1100
WIDTHS = [WIDE, 1700, 1400, 1250, TIGHT, 1000, 700, 390]


def _fake_api(route):
    url = route.request.url
    if "/api/accounts/state" in url:
        return route.fulfill(json={"status": "saved", "account_state": STATE})
    if "/api/accounts" in url:
        return route.fulfill(json={
            "post_mode": "browser", "headless": True, "accounts": ACCOUNTS,
            "available_accounts": ACCOUNTS, "account_state": STATE,
            "sessions": SESSIONS,
            "caption_styles": [{"name": "generic", "display_name": "Generic"}],
            "default_caption_style": "generic", "caption_limit": 2200,
        })
    if "/api/" in url:
        return route.fulfill(json={"entries": [], "batches": [], "snapshots": []})
    return route.fulfill(body=HTML, content_type="text/html")


# Every header child's box, its card's box, and each tracker label's
# truncation state, per card.
_MEASURE = """() => [...document.querySelectorAll('.slot-card')].map(card => {
  const box = r => { const b = r.getBoundingClientRect(); return [b.left, b.right]; };
  const head = card.querySelector('.slot-head');
  return {
    id: card.dataset.accountId,
    card: box(card),
    parts: [...head.querySelectorAll('*')]
      .filter(n => n.getClientRects().length)
      .map(n => ({cls: n.getAttribute('class') || n.tagName, box: box(n)})),
    labels: [...head.querySelectorAll('.st-text')].map(n => ({
      text: n.textContent, truncated: n.scrollWidth > n.clientWidth + 0.5,
    })),
  };
})"""


@pytest.fixture(scope="module")
def layouts():
    """{width: (measurements, page scrollWidth)} for every width in WIDTHS."""
    try:
        with playwright_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:  # no local Chrome
                pytest.skip(f"Chrome unavailable for layout test: {exc}")
            page = browser.new_page()
            page.route("**/*", _fake_api)
            page.goto("http://ricepost.test/")
            page.wait_for_selector(".slot-card button.tracker")
            # Show the media badge too: it shares the header row.
            page.evaluate("""() => document.querySelectorAll('.media-type-badge')
              .forEach(b => { b.style.display = ''; b.textContent = 'Video'; })""")
            out = {}
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(50)
                out[width] = (page.evaluate(_MEASURE),
                              page.evaluate("document.documentElement.scrollWidth"))
            browser.close()
            return out
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"Playwright could not run: {exc}")


@pytest.mark.parametrize("width", WIDTHS)
def test_header_stays_inside_its_own_card(layouts, width):
    cards, _ = layouts[width]
    assert len(cards) == 3
    for card in cards:
        left, right = card["card"]
        escaped = [
            p["cls"] for p in card["parts"]
            if p["box"][0] < left - 0.5 or p["box"][1] > right + 0.5
        ]
        assert not escaped, (
            f"{width}px, slot {card['id']}: {escaped} overflow the card "
            f"({json.dumps(card['parts'])})"
        )


@pytest.mark.parametrize("width", WIDTHS)
def test_no_horizontal_page_scroll(layouts, width):
    _, scroll_width = layouts[width]
    assert scroll_width <= width


def test_menu_stays_at_the_cards_right_edge(layouts):
    for width in WIDTHS:
        for card in layouts[width][0]:
            menu = next(p for p in card["parts"] if p["cls"] == "slot-menu")
            assert card["card"][1] - menu["box"][1] < 24, (width, card["id"])


def test_both_labels_truncate_together_when_space_runs_out(layouts):
    """At the tightest three-column width both labels of a both-disabled slot
    are truncated, not just the right-hand TikTok one."""
    cards, _ = layouts[TIGHT]
    both_off = next(c for c in cards if c["id"] == "A")
    assert [l["text"] for l in both_off["labels"]] == ["Disabled"] * 2
    assert all(l["truncated"] for l in both_off["labels"]), both_off["labels"]
    # One label truncating never leaves the other untouched while it too is
    # wider than it can show.
    for card in cards:
        states = {l["truncated"] for l in card["labels"]}
        texts = {l["text"] for l in card["labels"]}
        if len(texts) == 1:
            assert len(states) == 1, (card["id"], card["labels"])


def test_labels_are_whole_when_there_is_room(layouts):
    cards, _ = layouts[WIDE]
    for card in cards:
        assert not any(l["truncated"] for l in card["labels"]), (card["id"], card["labels"])


# --- source-level pins (run everywhere, including CI) -----------------------

def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", HTML)
    assert match, f"{selector} rule not found"
    return match.group(1)


def test_tracker_labels_truncate_with_ellipsis():
    rule = _rule(".tracker .st-text")
    for decl in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis",
                 "white-space: nowrap"):
        assert decl in rule


def test_trackers_can_shrink():
    assert "flex: 0 1 auto; min-width: 0" in _rule(".slot-trackers")
    assert "flex: 0 1 auto; min-width: 0" in _rule(".tracker")


@pytest.mark.parametrize("selector", [
    ".slot-num", ".slot-menu", ".media-type-badge", ".tracker .status-dot",
])
def test_fixed_header_parts_never_shrink(selector):
    assert "flex: none" in _rule(selector)


def test_icon_never_shrinks():
    assert "flex: 0 0 auto" in _rule(".tracker .plat-ico")
