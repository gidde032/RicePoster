"""Regression coverage for the ratified Slate sibling interface (#78).

The Slate redesign is a **pure visual redesign** of `frontend/index.html`: it
preserves every control ID, handler, endpoint, payload, timeout, and safety
behaviour while re-skinning the UI into the RiceClipper Slate visual language
and reorganising it behind a five-destination sidebar.

`frontend/index.html` is one vanilla HTML/JS file with no build step, and the
project forbids real-browser E2E tests inside the suite, so these are
**source-level assertions** in the established style of
`test_frontend_robustness.py`, `test_ui_polish.py`, and `test_esc_attribute_safety.py`.
They pin the properties the redesign contract depends on — token values, logo
identity, tab structure, control/handler survival, summary and action-bar
ordering, the horizontal account tracker, and the accessibility affordances —
not the rendered pixels (those are captured by the offline `/private/tmp`
fixture and reviewed against the ratified references).

The behavioural preservation is additionally guarded by the unchanged
`test_frontend_robustness.py`, `test_esc_attribute_safety.py`, and
`test_ui_polish.py` suites and the live-call tripwire; this module adds the
Slate-specific contract on top.
"""

import hashlib
import re

import pytest

from tests.paths import PROJECT_ROOT
from tests.test_frontend_robustness import _function_body, _html, _script

INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"
RUNTIME_LOGO = PROJECT_ROOT / "frontend" / "logo-ratified.png"
DESIGN_LOGO = PROJECT_ROOT / "design" / "references" / "logo-ratified.png"

# SHA-256 of the approved symbol-only RicePoster logo. Pinned as a literal so a
# clean CI clone (which does not carry the gitignored design/ authority) still
# verifies the served logo is exactly the ratified bytes, and so any recreation
# in SVG/CSS/glyph or an accidental re-export fails here.
APPROVED_LOGO_SHA256 = (
    "3251da8ee1ab3a4b46ba04d6d6286077478b91fb5080e468f903eea9e0e9800e"
)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- RiceClipper Slate tokens (exact values) --------------------------------

SLATE_TOKENS = {
    "--backdrop": "#04060A",
    "--midground": "#0C1116",
    "--interior": "#14191E",
    "--hairline": "#2B3136",
    "--control-line": "#626A70",
    "--rice-grey": "#AEB3B6",
    "--muted-grey": "#80878C",
    "--charcoal": "#3B4044",
    "--primary-text": "#E5E8EA",
    "--focus": "#D5D9DB",
}


@pytest.mark.parametrize("token,value", SLATE_TOKENS.items())
def test_exact_slate_tokens_are_defined(token, value):
    """Every ratified Slate token must be present with its exact value. Case is
    part of the contract — the spec lists them uppercase."""
    html = _html()
    assert re.search(rf"{re.escape(token)}\s*:\s*{re.escape(value)}\b", html), (
        f"Slate token {token} must be defined as {value}"
    )


def test_inter_typeface_stack():
    html = _html()
    assert (
        'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
        in html
    ), "the ratified Inter font stack must be used"


def test_no_forbidden_chrome_colours():
    """Slate chrome is carbon/grey/rice-grey/charcoal only: no blue, purple,
    cyan, gold, or warm amber. Pin the specific brand hexes the pre-Slate UI
    used so a regression that reintroduces them fails."""
    html = _html().lower()
    for banned in (
        "#2563eb", "#1d4ed8", "#3b82f6",   # blue
        "#7c3aed", "#6d28d9", "#2d1b69", "#a78bfa",  # purple
        "#6dcaca", "#1e2a2a",              # cyan
        "#ffb84d", "#3b2a00", "#caca6d",   # gold / warm amber chrome
    ):
        assert banned not in html, f"forbidden chrome colour {banned} present"


def test_status_hues_are_only_for_genuine_states():
    """Green/amber/red are permitted only for operational status. They are
    declared as dedicated --ok/--warn/--fail tokens so their use is auditable
    and separate from chrome."""
    html = _html()
    for token in ("--ok:", "--warn:", "--fail:"):
        assert token in html, f"status hue token {token} must be declared"
    # Status is never colour-only: the status text tokens travel with words.
    assert "st-text" in html


# --- Logo identity ----------------------------------------------------------

def test_runtime_logo_is_the_approved_bytes():
    """The served logo must be byte-identical to the approved reference, pinned
    against a literal SHA-256.

    The logo is a local-only asset (gitignored, like the design authority and
    internal docs) because the project forbids ANY tracked PNG (#60), so this
    skips on a clean clone where the file is absent. On the maintainer's machine
    it is a hard identity check that catches any recreation or re-export."""
    if not RUNTIME_LOGO.exists():
        pytest.skip("runtime logo is a local-only asset; absent on a clean clone")
    assert _sha256(RUNTIME_LOGO) == APPROVED_LOGO_SHA256, (
        "frontend/logo-ratified.png is not the approved logo bytes"
    )


def test_runtime_logo_matches_design_authority_when_present():
    """When the local design authority is present, the runtime copy must match
    it exactly. Skips on a clean clone where design/ is absent."""
    if not DESIGN_LOGO.exists():
        pytest.skip("design/ authority is local-only; absent on a clean clone")
    assert _sha256(RUNTIME_LOGO) == _sha256(DESIGN_LOGO)


def test_logo_is_served_directly_never_recreated():
    html = _html()
    assert 'src="/static/logo-ratified.png"' in html, (
        "the approved PNG logo must be referenced directly"
    )
    # No SVG/CSS recreation of the logo mark: the only <img> logo is the PNG,
    # and the brand block must not embed an inline <svg> as the logo.
    brand = html.split('class="brand"', 1)[1].split("</div>", 1)[0]
    assert "<svg" not in brand, "the logo must not be recreated as inline SVG"


# --- Global shell: five ratified destinations, Accounts/Settings deferred ----

def test_sidebar_has_exactly_the_five_ratified_destinations():
    html = _html()
    for view in ("localmedia", "review", "queue", "history", "help"):
        assert f'id="nav-{view}"' in html, f"sidebar must have the {view} destination"
        assert f'id="view-{view}"' in html, f"the {view} view section must exist"


def test_accounts_and_settings_are_deferred():
    """Accounts and Settings are explicitly deferred: no nav item, view, or
    handler for them may exist."""
    html = _html()
    assert 'id="nav-accounts"' not in html
    assert 'id="nav-settings"' not in html
    assert 'id="view-accounts"' not in html
    assert 'id="view-settings"' not in html
    # Guard the sibling render's borrowed labels from leaking in as nav.
    assert "navTo('accounts')" not in html
    assert "navTo('settings')" not in html


def test_review_is_the_default_destination():
    html = _html()
    # nav-review carries aria-current; view-review is the only view not hidden.
    review_nav = re.search(r'id="nav-review"[^>]*>', html).group(0)
    assert 'aria-current="page"' in review_nav
    review_view = re.search(r'id="view-review"[^>]*>', html).group(0)
    assert "hidden" not in review_view
    for view in ("localmedia", "queue", "history", "help"):
        section = re.search(rf'id="view-{view}"[^>]*>', html).group(0)
        assert "hidden" in section, f"{view} view must start hidden"


def test_navigation_is_client_side_only():
    """navTo is a view switcher — no new route, no auth, no account state."""
    script = _script()
    assert "function navTo(view)" in script
    body = _function_body("navTo")
    # It only toggles hidden views + aria-current + breadcrumb, and drives the
    # existing queue/history toggles. No fetch of its own.
    assert "fetch(" not in body
    assert "toggleQueuePanel()" in body
    assert "toggleHistory()" in body


# --- Review: condensed summary ----------------------------------------------

def test_condensed_summary_has_exactly_the_five_permitted_fields():
    html = _html()
    summary = html.split('id="runSummary"', 1)[1].split("</div>\n\n", 1)[0]
    for field in ("Run ID", "Status", "Next slot", "Summary"):
        assert field in summary, f"summary must contain {field}"
    assert "View Full History" in summary, "the log control is labelled View Full History"
    # The rejected fields must not return, nor a second summary row.
    assert "Started" not in summary
    assert "Elapsed" not in summary.replace("<!--", "")


def test_condensed_summary_field_order():
    """Run ID -> Status -> Next slot -> Summary -> View Full History, one line."""
    html = _html()
    order = ["rsRunId", "rsStatus", "rsNext", "rsSummary", "rsViewLog"]
    positions = [html.index(f'id="{i}"') for i in order]
    assert positions == sorted(positions), "summary fields are out of order"


def test_view_full_history_navigates_to_history():
    html = _html()
    view_log = re.search(r'id="rsViewLog"[^>]*>', html).group(0)
    assert "navTo('history')" in view_log


def test_run_id_is_a_client_side_run_label():
    """Run ID has no backend source; it is a client-generated UI run label,
    reset to an em-dash on New Run, so it is never fabricated platform data."""
    script = _script()
    assert "state.runId" in script
    post = _function_body("postAll")
    assert "state.runId = 'RUN-'" in post, "postAll must mint the run label"
    reset = _function_body("resetRun")
    assert "state.runId = ''" in reset, "New Run must clear the run label"


def test_summary_derives_from_existing_progress_data_only():
    body = _function_body("renderSummary")
    # It reads the /api/post-progress shape (active/current/waiting/events) and
    # never invents a value beyond the client run label.
    assert "p.active" in body and "p.waiting" in body and "p.events" in body


# --- Review: action bar order ------------------------------------------------

def test_action_bar_order_is_ratified():
    html = _html()
    actions = html.split('<div class="actions">', 1)[1].split("</div>", 1)[0]
    order = ["btnPullClipper", "btnGenerate", "btnPost", "btnSchedule", "New Run"]
    positions = [actions.index(x) for x in order]
    assert positions == sorted(positions), "action bar buttons are out of order"
    # Post All keeps its existing disabled-until-captioned semantics.
    assert 'id="btnPost" disabled' in html


def test_summary_sits_directly_above_the_action_bar():
    html = _html()
    assert html.index('id="runSummary"') < html.index('<div class="actions">')


# --- Review: horizontal account trackers -------------------------------------

def test_slot_header_uses_horizontal_trackers_not_platform_labels():
    script = _script()
    # The trackers are built into the sessDots variable and are horizontal.
    assert "slot-trackers" in script
    assert "IG_ICON" in script and "TT_ICON" in script
    # No platform-name text label in the slot header, no stacking, no 3rd platform.
    head = script.split('class="slot-head"', 1)[1].split("</div>", 1)[0]
    assert ">Instagram<" not in head and ">TikTok<" not in head
    assert "YouTube" not in script and "Shorts" not in script


def test_trackers_keep_truthful_status_meanings():
    """green ready, muted unavailable — status carries text, not colour alone."""
    body = _function_body("renderSlots")
    assert "st-ok" in body and "st-muted" in body
    assert "'Ready'" in body and "'No session'" in body


def test_slot_header_keeps_number_and_menu():
    body = _function_body("renderSlots")
    assert "slot-num" in body
    assert "slot-menu" in body


# --- Preserved control identity ---------------------------------------------

TOP_LEVEL_IDS = [
    "headerBadges", "statusBar", "slotsContainer", "statusPanel",
    "queuePanel", "historyPanel", "scheduleRow", "scheduleTime",
    "btnPullClipper", "btnGenerate", "btnPost", "btnSchedule", "mediaInfo",
]


@pytest.mark.parametrize("elem_id", TOP_LEVEL_IDS)
def test_top_level_control_ids_survive(elem_id):
    assert f'id="{elem_id}"' in _html(), f"control id {elem_id} was lost"


SLOT_SCOPED_IDS = [
    "dropZone", "dropText", "uploadProg", "uploadFill", "thumbRow", "thumbChip",
    "mediaTypeBadge", "topic", "caption", "charCount", "captionError",
    "feedback", "btnRegen", "btnUndo",
]


@pytest.mark.parametrize("root", SLOT_SCOPED_IDS)
def test_slot_scoped_ids_survive(root):
    assert f'id="{root}_${{slot}}"' in _script(), f"slot id {root}_ was lost"


PRESERVED_HANDLERS = [
    "init", "renderSlots", "pullFromClipper", "applyPulledSlot", "handleFile",
    "updateButtons", "generateAll", "regenerateCaption", "undoCaption",
    "resetRun", "clearMedia", "toggleHistory", "startProgressPolling",
    "renderProgress", "postAll", "toggleScheduleRow", "scheduleAll",
    "refreshQueue", "renderQueuePanel", "fetchRetainedMedia", "deleteQueueMedia",
    "cancelQueueBatch", "buildSlotsPayload", "handleFetchError", "fetchWithTimeout",
]


@pytest.mark.parametrize("fn", PRESERVED_HANDLERS)
def test_handlers_survive(fn):
    assert f"function {fn}" in _script(), f"handler {fn} was lost"


ENDPOINTS = [
    "/api/accounts", "/api/upload/", "/api/media-info", "/api/media/clear",
    "/api/generate-caption", "/api/pull-from-clipper", "/api/media/",
    "/api/post-progress", "/api/history", "/api/post", "/api/queue",
    "/api/queue/media", "/api/queue/",
]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_endpoints_are_unchanged(endpoint):
    assert endpoint in _script(), f"endpoint {endpoint} was lost"


# --- Per-slot result footer relocation --------------------------------------

def test_per_slot_result_footer_exists_in_each_card():
    body = _function_body("renderSlots")
    assert 'id="slotResult_${slot}"' in body, "each card needs a result footer"


def test_run_status_rows_render_into_the_card_footers():
    """Per-slot status rows moved from the global panel into each card's footer,
    but keep the status_ id so renderProgress still targets them, and only for
    slots the run touches (unchanged skip semantics)."""
    post = _function_body("postAll")
    assert "slotElOpt('slotResult', slot)" in post
    assert 'id="status_${slot}"' in post
    # renderProgress still keys on the per-slot status_ element.
    assert "slotElOpt('status', slot)" in _function_body("renderProgress")


# --- Accessibility affordances ----------------------------------------------

def test_live_regions_and_labels_present():
    html = _html()
    assert 'aria-live="polite"' in html, "status updates need a live region"
    assert html.count('aria-label=') >= 5, "controls need accessible names"
    # Visible keyboard focus + reduced motion are honoured in CSS.
    assert ":focus-visible" in html
    assert "prefers-reduced-motion" in html


def test_selected_nav_is_not_colour_only():
    """aria-current plus a structural marker (inset box-shadow), not colour alone."""
    html = _html()
    assert '.nav-item[aria-current="page"]' in html
    marker = html.split('.nav-item[aria-current="page"] {', 1)[1].split("}", 1)[0]
    assert "box-shadow" in marker or "border" in marker


def test_narrow_layout_has_responsive_rules():
    html = _html()
    assert "@media (max-width: 860px)" in html
    assert "grid-template-columns: 1fr" in html


# ---------------------------------------------------------------------------
# Accepted review-finding repairs (cold review of feat/slate-ui @ 10af0cc)
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _full_function(name):
    """The complete `function name(...) {...}` declaration (signature + body).

    `_function_body` returns only the brace block, which is not independently
    executable; this returns the whole declaration so it can be run in node.
    """
    script = _script()
    m = _re.search(rf"\nfunction {_re.escape(name)}\s*\(", script)
    assert m, f"{name} not found"
    start = m.start() + 1
    i = script.index("{", start)
    depth = 0
    for j in range(i, len(script)):
        if script[j] == "{":
            depth += 1
        elif script[j] == "}":
            depth -= 1
            if depth == 0:
                return script[start : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


# --- R1-1: a no-session skip must count as skipped, not failed --------------

def test_summary_classifies_a_no_session_skip_as_skipped_not_failed():
    """Reviewer 1 (MEDIUM): the run-completion summary bucketed any slot whose
    `errors` was non-empty as 'error', but the backend appends a
    'skipped (no session ...)' string to `errors` for a platform with no saved
    session while emitting a distinct 'skipped' event. This runs the actual
    extracted classifier in node against that exact shape."""
    node = _shutil.which("node")
    if node is None:
        pytest.skip("node needed to execute the classifier")
    defs = "\n".join(
        _full_function(n)
        for n in ("isUnconfirmed", "isSkipError", "classifyPlatform", "summaryEventsFromResults")
    )
    driver = """
const results = [
  {ig_post_id:'ig_ok_A', tt_post_id:'', errors:['TT post: skipped (no session — run session_manager login tiktok)']},
  {ig_post_id:'ig_ok_B', tt_post_id:'tt_unconfirmed_B', errors:[]},
  {ig_post_id:'', tt_post_id:'', errors:['IG post: upload dialog did not appear']},
];
console.log(JSON.stringify(summaryEventsFromResults(results).map(e => e.status)));
"""
    proc = _subprocess.run(
        [node, "--input-type=module", "--eval", defs + driver],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    import json as _json
    statuses = _json.loads(proc.stdout.strip())
    # IG ok + TT skip / IG ok + TT unconfirmed / IG failed + TT none.
    assert statuses == ["ok", "skipped", "ok", "unconfirmed", "error"], statuses
    # The load-bearing assertion: exactly one genuine failure, and the
    # no-session platform is 'skipped', never 'error'.
    assert statuses.count("error") == 1
    assert "skipped" in statuses


def test_skip_detection_mirrors_the_backend_strings():
    """classifyPlatform's skip rule must match backend _is_skip's markers."""
    body = _function_body("isSkipError")
    assert "skipped (no session" in body and "skipped (pre-flight" in body


# --- R2-1 / R2-5: accessible names ------------------------------------------

@pytest.mark.parametrize("view,label", [
    ("localmedia", "Local Media"), ("review", "Review"), ("queue", "Queue"),
    ("history", "History"), ("help", "Help"),
])
def test_nav_buttons_have_explicit_accessible_names(view, label):
    """Reviewer 2 (CRITICAL): below 860px the nav label is display:none, so an
    icon button with no aria-label had no accessible name. Each nav button now
    carries an explicit aria-label independent of label visibility."""
    html = _html()
    btn = _re.search(rf'id="nav-{view}"[^>]*>', html).group(0)
    assert f'aria-label="{label}"' in btn


def test_topic_input_has_aria_label():
    """Reviewer 2 (MEDIUM): the topic field was the one input in the card
    without an aria-label."""
    assert 'id="topic_${slot}" aria-label=' in _script()


# --- R2-2: the headless toggle is keyboard operable -------------------------

def test_headless_badge_is_keyboard_operable():
    """Reviewer 2 (HIGH): the Headless/Visible toggle (controls live browser
    visibility) was mouse-only. It now mirrors the queue badge's keyboard
    affordance."""
    script = _script()
    hb = script.split('id="headlessBadge"', 1)[1].split("</span>", 1)[0]
    assert 'role="button"' in hb and 'tabindex="0"' in hb
    assert "onkeydown=" in hb and "toggleHeadless()" in hb


# --- R2-3a: the slot menu is decorative, not an inert control ---------------

def test_slot_menu_is_decorative_not_an_inert_control():
    """Reviewer 2 (MEDIUM): the slot menu was a focusable, labelled button with
    no handler. It is now an aria-hidden decorative glyph, not a keyboard/SR
    dead-end."""
    body = _function_body("renderSlots")
    assert '<span class="slot-menu" aria-hidden="true">' in body
    # It is a span, never a <button> (no keyboard/SR dead-end).
    assert 'class="slot-menu"' in body
    assert 'button type="button" class="slot-menu"' not in _script()


# --- R2-4: no tabpanel role without a tablist -------------------------------

def test_views_use_region_not_orphan_tabpanel_role():
    """Reviewer 2 (MEDIUM): role=tabpanel implies a tablist/tab structure that
    does not exist. Views are landmark regions instead."""
    html = _html()
    assert 'role="tabpanel"' not in html
    assert html.count('class="view" role="region"') == 5


# --- R2-6 / R2-7 / R2-8: contrast, targets, dead CSS ------------------------

def test_disabled_post_all_is_legible():
    """Reviewer 2 (LOW): disabled Post All used muted-grey on charcoal (2.88:1).
    It now uses a legible pairing."""
    html = _html()
    rule = html.split(".btn-post:disabled {", 1)[1].split("}", 1)[0]
    assert "var(--muted-grey)" not in rule
    assert "var(--rice-grey)" in rule


def test_small_button_target_size():
    html = _html()
    rule = html.split(".btn-small {", 1)[1].split("}", 1)[0]
    assert "min-height: 40px" in rule


def test_dead_tracker_status_css_removed():
    """Reviewer 2 (LOW): .st-warn/.st-fail were never applied (trackers are
    session-only)."""
    html = _html()
    assert ".status-dot.st-warn" not in html
    assert ".status-dot.st-fail" not in html


# --- R3-1 / R3-2: History and Queue table layouts + coloured status ---------

def test_history_renders_a_table_with_coloured_status():
    """Reviewer 3 (HIGH): History dropped status colour. It now renders the
    ratified column table with coloured status cells."""
    body = _function_body("toggleHistory")
    assert "data-table" in body
    assert "classifyPlatform('IG'" in body and "classifyPlatform('TT'" in body
    assert "statusCell(" in body
    for col in ("Time (local)", "Instagram", "TikTok", "Scheduled", "Caption preview"):
        assert col in body, f"history table missing column {col}"


def test_status_cell_maps_states_to_colour_and_text():
    body = _function_body("statusCell")
    # Colour classes AND words together — never colour alone.
    for token in ("rs-ok", "rs-warn", "rs-fail", "Confirmed", "Unconfirmed", "Failed", "Skipped"):
        assert token in body


def test_per_slot_footer_is_skip_aware_and_consistent_with_summary():
    """Follow-up (maintainer-directed): the per-slot result footer must
    classify outcomes the same way the condensed summary does — a no-session
    'skipped' platform is not a failure, and only real (non-skip) errors surface
    as an error string. Both now route through classifyPlatform/platMark."""
    post = _function_body("postAll")
    assert "classifyPlatform('IG', result.ig_post_id, result.errors)" in post
    assert "classifyPlatform('TT', result.tt_post_id, result.errors)" in post
    assert "platMark(ig)" in post and "platMark(tt)" in post
    # A skip routes to the muted skipped row, not the red error row.
    assert "status-item status-skipped" in post
    assert "ig === 'skipped' || tt === 'skipped'" in post
    # Only non-skip errors are shown as an error string.
    assert "!isSkipError(e)" in post
    # The old blanket "any errors -> error row" is gone.
    assert "result.errors && result.errors.length > 0" not in post


def test_platmark_matches_summary_classification():
    """platMark maps the shared classification to inline marks with words, so
    the footer and summary never disagree; a skip reads 'skipped', not 'failed'."""
    node = _shutil.which("node")
    if node is None:
        pytest.skip("node needed")
    defs = _full_function("platMark")
    driver = ("console.log(JSON.stringify(['ok','unconfirmed','error','skipped','none']"
              ".map(platMark)));")
    proc = _subprocess.run(
        [node, "--input-type=module", "--eval", defs + "\n" + driver],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    import json as _json
    marks = _json.loads(proc.stdout.strip())
    assert marks[0] == "✓"                 # ok
    assert "unconfirmed" in marks[1]
    assert "failed" in marks[2]
    assert "skipped" in marks[3]           # skip is never "failed"
    assert marks[4] == "—"


def test_queue_renders_sectioned_tables():
    """Reviewer 3 (MED-HIGH): Queue was a flat list. It now renders the three
    ratified sections as tables."""
    panel = _function_body("renderQueuePanel")
    for title in ("Scheduled Batches", "Interrupted Batches"):
        assert title in panel
    assert "data-table" in panel
    retained = _function_body("renderRetainedMedia")
    assert "Retained Media" in retained and "data-table" in retained
    # Cancel/Dismiss share the existing handler; Delete media keeps its exact
    # escAttr onclick (also pinned by test_esc_attribute_safety).
    assert "cancelQueueBatch('${escAttr(b.id)}')" in panel
    assert "deleteQueueMedia('${escAttr(s.batch_id)}'" in retained
