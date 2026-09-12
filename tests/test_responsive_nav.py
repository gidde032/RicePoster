"""Source-level tests for the responsive nav collapse at max-width: 860px.

Pins the narrow-viewport layout: logo moves to the topbar, sidebar brand
hides, nav buttons form a horizontal icon-only strip, topbar stays single-line,
and account names truncate instead of wrapping character-by-character.
The wide-viewport layout stays unchanged.
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = PROJECT_ROOT / "frontend" / "index.html"


@pytest.fixture(scope="module")
def html():
    return HTML_PATH.read_text()


@pytest.fixture(scope="module")
def narrow_block(html):
    """Extract the @media (max-width: 860px) block."""
    match = re.search(
        r"@media\s*\(max-width:\s*860px\)\s*\{(.+?)^\s{2}\}",
        html,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "@media (max-width: 860px) block not found"
    return match.group(1)


# --- Narrow: sidebar brand hidden, topbar logo shown -------------------------

def test_narrow_hides_sidebar_brand(narrow_block):
    assert re.search(r"\.brand\s*\{[^}]*display:\s*none", narrow_block), \
        ".brand must be display:none at narrow viewport"


def test_narrow_shows_topbar_logo(narrow_block):
    assert re.search(r"\.topbar-logo\s*\{[^}]*display:\s*block", narrow_block), \
        ".topbar-logo must be display:block at narrow viewport"


# --- Topbar logo element exists in HTML ---------------------------------------

def test_topbar_logo_element_exists(html):
    assert re.search(
        r'<img\s[^>]*class="topbar-logo"[^>]*src="/static/logo-ratified\.png"',
        html,
    ), "topbar must contain an <img> with class topbar-logo and the ratified logo src"


def test_topbar_logo_before_breadcrumb(html):
    logo_pos = html.find('class="topbar-logo"')
    breadcrumb_pos = html.find('class="breadcrumb"')
    assert logo_pos < breadcrumb_pos, \
        "topbar-logo must appear before the breadcrumb in DOM order"


# --- Topbar logo hidden at wide viewport -------------------------------------

def test_topbar_logo_hidden_by_default(html):
    default_match = re.search(
        r"\.topbar-logo\s*\{([^}]+)\}", html,
    )
    assert default_match, ".topbar-logo base rule not found"
    assert re.search(r"display:\s*none", default_match.group(1)), \
        ".topbar-logo must be display:none by default (wide viewport)"


# --- Narrow: nav element is a horizontal flex row -----------------------------

def test_narrow_nav_is_flex_row(narrow_block):
    assert re.search(
        r"\.sidebar\s+nav\s*\{[^}]*display:\s*flex", narrow_block,
    ), "nav inside sidebar must be display:flex at narrow viewport"

    assert re.search(
        r"\.sidebar\s+nav\s*\{[^}]*flex-direction:\s*row", narrow_block,
    ), "nav inside sidebar must be flex-direction:row at narrow viewport"


# --- Narrow: nav labels hidden on all items including current -----------------

def test_narrow_hides_all_nav_labels(narrow_block):
    assert re.search(
        r"\.nav-item\s+\.nav-label\s*\{[^}]*display:\s*none", narrow_block,
    ), "all .nav-item .nav-label must be hidden at narrow viewport"


def test_narrow_hides_current_nav_label(narrow_block):
    assert re.search(
        r'\.nav-item\[aria-current="page"\]\s+\.nav-label\s*\{[^}]*display:\s*none',
        narrow_block,
    ), "active nav item label must also be hidden at narrow viewport"


# --- Narrow: left box-shadow marker removed -----------------------------------

def test_narrow_removes_active_left_marker(narrow_block):
    assert re.search(
        r'\.nav-item\[aria-current="page"\]\s*\{[^}]*box-shadow:\s*none',
        narrow_block,
    ), "active nav item left box-shadow marker must be removed at narrow viewport"


# --- Narrow: sidebar is a centered horizontal strip --------------------------

def test_narrow_sidebar_centered(narrow_block):
    assert re.search(
        r"\.sidebar\s*\{[^}]*justify-content:\s*center", narrow_block,
    ), "sidebar must be justify-content:center at narrow viewport"


def test_narrow_sidebar_horizontal(narrow_block):
    assert re.search(
        r"\.sidebar\s*\{[^}]*flex-direction:\s*row", narrow_block,
    ), "sidebar must be flex-direction:row at narrow viewport"


def test_narrow_sidebar_no_wrap(narrow_block):
    assert re.search(
        r"\.sidebar\s*\{[^}]*flex-wrap:\s*nowrap", narrow_block,
    ), "sidebar must be flex-wrap:nowrap at narrow viewport"


def test_narrow_sidebar_bottom_border(narrow_block):
    assert re.search(
        r"\.sidebar\s*\{[^}]*border-bottom:\s*1px\s+solid", narrow_block,
    ), "sidebar must have a bottom border at narrow viewport"


# --- Narrow: topbar stays single-line ----------------------------------------

def test_narrow_topbar_no_wrap(narrow_block):
    assert re.search(
        r"\.topbar\s*\{[^}]*flex-wrap:\s*nowrap", narrow_block,
    ), "topbar must be flex-wrap:nowrap at narrow viewport"


def test_narrow_breadcrumb_truncates(narrow_block):
    assert re.search(
        r"\.breadcrumb\s*\{[^}]*text-overflow:\s*ellipsis", narrow_block,
    ), "breadcrumb must truncate with ellipsis at narrow viewport"


def test_narrow_breadcrumb_no_line_wrap(narrow_block):
    assert re.search(
        r"\.breadcrumb\s*\{[^}]*white-space:\s*nowrap", narrow_block,
    ), "breadcrumb must not wrap to a new line at narrow viewport"


def test_narrow_topbar_right_no_shrink(narrow_block):
    assert re.search(
        r"\.topbar-right\s*\{[^}]*flex-shrink:\s*0", narrow_block,
    ), "topbar-right must not shrink at narrow viewport"


def test_narrow_topbar_right_no_wrap(narrow_block):
    assert re.search(
        r"\.topbar-right\s*\{[^}]*flex-wrap:\s*nowrap", narrow_block,
    ), "topbar-right must not wrap at narrow viewport"


# --- Account name text: no character-by-character wrapping (all viewports) ----

def test_slot_account_id_never_wraps_mid_character(html):
    """The base rule must prevent character-by-character wrapping."""
    base_match = re.search(r"\.slot-account-id\s*\{([^}]+)\}", html)
    assert base_match, ".slot-account-id base rule not found"
    body = base_match.group(1)
    assert "overflow-wrap" not in body or "anywhere" not in body, \
        "slot-account-id must not use overflow-wrap:anywhere"


def test_slot_account_id_truncates_with_ellipsis(html):
    base_match = re.search(r"\.slot-account-id\s*\{([^}]+)\}", html)
    assert base_match, ".slot-account-id base rule not found"
    body = base_match.group(1)
    assert re.search(r"text-overflow:\s*ellipsis", body), \
        "slot-account-id must use text-overflow:ellipsis"


def test_slot_account_id_hides_overflow(html):
    base_match = re.search(r"\.slot-account-id\s*\{([^}]+)\}", html)
    assert base_match, ".slot-account-id base rule not found"
    body = base_match.group(1)
    assert re.search(r"overflow:\s*hidden", body), \
        "slot-account-id must use overflow:hidden"


def test_slot_account_id_single_line(html):
    base_match = re.search(r"\.slot-account-id\s*\{([^}]+)\}", html)
    assert base_match, ".slot-account-id base rule not found"
    body = base_match.group(1)
    assert re.search(r"white-space:\s*nowrap", body), \
        "slot-account-id must use white-space:nowrap"


def test_slot_account_id_can_flex_shrink(html):
    """min-width:0 lets the flex item shrink below its content size."""
    base_match = re.search(r"\.slot-account-id\s*\{([^}]+)\}", html)
    assert base_match, ".slot-account-id base rule not found"
    body = base_match.group(1)
    assert re.search(r"min-width:\s*0", body), \
        "slot-account-id must have min-width:0 to allow flex shrinking"


# --- Wide viewport: no regression in sidebar/brand/topbar ---------------------

def test_wide_sidebar_has_column_direction(html):
    base_match = re.search(r"\.sidebar\s*\{([^}]+)\}", html)
    assert base_match, ".sidebar base rule not found"
    assert "flex-direction: column" in base_match.group(1), \
        "sidebar base rule must keep flex-direction:column for wide viewport"


def test_wide_brand_visible(html):
    base_match = re.search(r"\.brand\s*\{([^}]+)\}", html)
    assert base_match, ".brand base rule not found"
    assert "display: none" not in base_match.group(1), \
        ".brand must not be hidden in the base (wide viewport) rule"


def test_wide_nav_labels_visible(html):
    base_match = re.search(r"\.nav-item\s*\{([^}]+)\}", html)
    assert base_match, ".nav-item base rule not found"
    assert "display: none" not in base_match.group(1), \
        "nav-item base rule must not hide anything (labels visible at wide)"


def test_wide_topbar_allows_wrap(html):
    """The base topbar rule must not prevent wrapping (only narrow does)."""
    base_match = re.search(r"\.topbar\s*\{([^}]+)\}", html)
    assert base_match, ".topbar base rule not found"
    assert "nowrap" not in base_match.group(1), \
        "topbar base rule must not set flex-wrap:nowrap"
