"""Batch 6 — poster internals (#28, #2, #29) and the equivalence pin behind them.

The bulk of this file is nine end-to-end transcripts of the two browser
posting flows, captured against the recording fakes in `browser_trace.py`.
See that module's docstring for why transcripts exist and what they prove.

The rule for this file: **a transcript may only change in the commit that
deliberately changes behaviour, and the diff must contain nothing else.**
Refactor commits reproduce the goldens byte for byte.

Regenerating: `RICEPOSTER_REGEN_TRACES=1 python -m pytest
tests/test_poster_internals.py`. That switch exists so an intended change is
easy to land with its diff visible in review. It is not a way to make a
surprising failure go away — a golden diff on a commit that was supposed to
be a pure refactor is the harness working.
"""

import json
import os
from pathlib import Path

import pytest

from backend import instagram_browser, tiktok_browser
from backend.browser_common import url_matches_login_markers
from tests.browser_trace import RAISE, Script, run_traced

GOLDEN_DIR = Path(__file__).parent / "golden"
REGEN = os.environ.get("RICEPOSTER_REGEN_TRACES") == "1"

CAPTION = "sunset over the harbour #latergram #goldenhour @studio"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def media(tmp_path):
    """A real file on disk: TikTok's flow copies the media before uploading."""
    path = tmp_path / "2026-07-30_some_long_descriptive_name.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def tt_cookie_session(tmp_sessions):
    """Give slot A a cookie session so TikTok takes its preferred auth path."""
    cookie_file = tiktok_browser._cookies_file("A")
    cookie_file.write_text(json.dumps([
        {"name": "sessionid", "value": "x" * 32, "domain": ".tiktok.com",
         "path": "/", "expirationDate": 1893456000.0, "secure": True},
    ]))
    return cookie_file


def check_golden(name: str, recorder):
    """Compare a captured transcript against its committed golden."""
    GOLDEN_DIR.mkdir(exist_ok=True)
    path = GOLDEN_DIR / f"{name}.trace"
    actual = recorder.text()
    if REGEN or not path.exists():
        path.write_text(actual)
        if REGEN:
            # Never report green while regenerating. The switch rewrites the
            # only evidence that these flows are unchanged, so a run with it
            # set must not be mistakable for a passing verification — a
            # regression regenerated on a branch would otherwise leave the
            # suite green and the diff looking deliberate. Skipping keeps the
            # regeneration usable while making the run visibly not a check.
            pytest.skip(
                f"RICEPOSTER_REGEN_TRACES=1: {path.name} rewritten, NOT compared. "
                "Review the golden diff before committing it."
            )
        pytest.fail(
            f"golden {path.name} was missing and has been written. Inspect "
            "it, confirm it describes the intended behaviour, and commit it."
        )
    expected = path.read_text()
    assert actual == expected, (
        f"The browser flow transcript for {name!r} changed.\n\n"
        "If this commit was a refactor, it is not behaviour-preserving: "
        "something was reordered, dropped, retimed, or re-selectored.\n"
        "If the change is intended, regenerate with RICEPOSTER_REGEN_TRACES=1 "
        "and put the diff in the pull request.\n\n"
        f"expected:\n{expected}\n\nactual:\n{actual}"
    )


# ---------------------------------------------------------------------------
# Instagram scripts
# ---------------------------------------------------------------------------

def _ig_script(**overrides):
    """The ordinary Instagram run: desktop layout, no popups, no aspect
    warning, crop control present, post confirmed."""
    base = dict(
        url="https://www.instagram.com/",
        wait_for_selector={
            # Only the first notification popup is present; the cookie
            # banners are not shown to a logged-in session.
            "Not now": RAISE,
            "Decline optional cookies": RAISE,
            "Allow essential": RAISE,
            "button:has-text('OK')": RAISE,
        },
        visible={"Select from computer": False},
        evaluate={
            'aria-label="New post"': "clicked_a",
            'input[type="password"]': False,
            'a[href="#"]': "found: Post",
        },
        inner_text=CAPTION,
    )
    base.update(overrides)
    return Script(**base)


def _run_ig(monkeypatch, media, script):
    return run_traced(
        monkeypatch,
        instagram_browser,
        lambda: instagram_browser.post_media("A", media, CAPTION, "reel", headless=True),
        script,
    )


def test_ig_normal_post_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """The whole Instagram happy path, call by call."""
    check_golden("ig_normal_post", _run_ig(monkeypatch, media, _ig_script()))


def test_ig_expired_session_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """A session that redirects to the login wall must abort with an
    actionable error before touching the composer."""
    script = _ig_script(url="https://www.instagram.com/accounts/login/")
    check_golden("ig_expired_session", _run_ig(monkeypatch, media, script))


def test_ig_caption_field_missing_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """Every caption selector exhausted: the error must name the chain it
    tried, and a debug screenshot must be taken."""
    script = _ig_script(counts={
        "Write a caption": 0,
        "role='textbox'": 0,
    })
    check_golden("ig_caption_chain_exhausted", _run_ig(monkeypatch, media, script))


def test_ig_caption_mismatch_aborts_before_sharing(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """Batch 6: Instagram now reads the caption box back before Share.

    The editor never shows the intended caption, so the flow must retry once
    — clearing first, or the retry would append to the mess — and then abort.
    The transcript is the proof that Share is never clicked: a wrong caption
    on a live account is not recoverable, a failed post is.
    """
    script = _ig_script(inner_text="sunset over the harbourrrr #latergra")
    check_golden("ig_caption_mismatch_abort", _run_ig(monkeypatch, media, script))


def test_ig_caption_verification_error_is_redacted_for_push(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """The verification error quotes the editor contents, so it must be
    truncated at the shared marker before it can reach a phone push — the
    same protection TikTok's verifier already had."""
    from backend import poster_browser

    script = _ig_script(inner_text="totally wrong text that is not the caption")
    rec = _run_ig(monkeypatch, media, script)
    raised = rec.lines[-1]

    assert "Instagram caption verification failed" in raised
    safe = poster_browser._notification_safe(raised)
    assert "totally wrong text" not in safe
    assert "editor contents redacted" in safe


def test_ig_unconfirmed_post_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """No success element ever appears. The post may or may not be live, so
    the result id must say `unconfirmed` rather than `ok`."""
    script = _ig_script()
    script.wait_for_selector["Animated checkmark"] = RAISE
    check_golden("ig_unconfirmed_post", _run_ig(monkeypatch, media, script))


# ---------------------------------------------------------------------------
# TikTok scripts
# ---------------------------------------------------------------------------

def _tt_script(**overrides):
    """The ordinary TikTok run: cookie auth, no iframe, no one-time overlay,
    caption verifies on the first attempt, post confirmed."""
    base = dict(
        url="https://www.tiktok.com/upload",
        wait_for_selector={"iframe[src*='upload']": RAISE},
        locator_wait={"Got it": RAISE},
        inner_text={"TUXModal": "", "": CAPTION},
        cookies=[
            {"name": "sessionid", "value": "y" * 32, "domain": ".tiktok.com",
             "path": "/", "expires": 1893456000.0, "secure": True,
             "httpOnly": True, "sameSite": "None"},
            {"name": "_ga", "value": "irrelevant", "domain": ".analytics.test",
             "path": "/", "expires": -1},
        ],
    )
    base.update(overrides)
    return Script(**base)


def _run_tt(monkeypatch, media, script):
    return run_traced(
        monkeypatch,
        tiktok_browser,
        lambda: tiktok_browser.post_media("A", media, CAPTION, "video", headless=True),
        script,
    )


def test_tt_normal_post_transcript(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """The whole TikTok happy path, call by call."""
    check_golden("tt_normal_post", _run_tt(monkeypatch, media, _tt_script()))


def test_tt_iframe_layout_transcript(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """When the upload panel is inside an iframe, the file input and the
    second confirmation wait must be addressed to the frame, not the page."""
    script = _tt_script(wait_for_selector={})
    check_golden("tt_iframe_layout", _run_tt(monkeypatch, media, script))


def test_tt_caption_mismatch_retry_transcript(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """The editor never shows the intended caption. TikTok must retry once
    with sequential typing and then refuse to post garbled text."""
    script = _tt_script(inner_text={"TUXModal": "", "": "tt_upload_A"},
                        counts={"TUXModal": 0})
    check_golden("tt_caption_mismatch_retry", _run_tt(monkeypatch, media, script))


def test_tt_blocked_by_one_time_dialog_transcript(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """A one-time TikTok dialog blocks the editor. The raised error must
    quote the dialog so the manual fix is obvious (incident 2026-07-18)."""
    script = _tt_script(
        counts={
            "contenteditable": 0,
            "data-placeholder": 0,
            "public-DraftEditor": 0,
            "editor-container": 0,
            "TUXModal": 1,
        },
        inner_text={"TUXModal": "Turn on automatic content checks?", "": ""},
    )
    check_golden("tt_blocked_by_dialog", _run_tt(monkeypatch, media, script))


def test_tt_unconfirmed_post_transcript(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """Neither confirmation signal appears and the page is still on /upload.
    The result must be `unconfirmed`, and the cookie write-back must still
    run — the post did not raise."""
    script = _tt_script()
    script.wait_for_selector["Search for post description"] = RAISE
    script.wait_for_selector["div:has-text('uploaded')"] = RAISE
    check_golden("tt_unconfirmed_post", _run_tt(monkeypatch, media, script))


def test_tt_profile_fallback_auth_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """No cookie file, so TikTok falls back to a persistent profile.

    Deliberately uses `tmp_sessions` rather than `tt_cookie_session`: every
    other TikTok scenario takes the cookie path, which left the fallback — a
    different launch call, a different browser handle, and a deliberately
    skipped cookie write-back — with no transcript at all.
    """
    session_dir = tiktok_browser.SESSIONS_DIR / "A"
    session_dir.mkdir(parents=True, exist_ok=True)
    check_golden("tt_profile_fallback_auth", _run_tt(monkeypatch, media, _tt_script()))


def test_ig_narrow_layout_transcript(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """A narrow window opens the upload dialog straight from Create, so the
    dropdown step is skipped. Untraced before: the ordinary scenario forces
    the desktop dropdown path."""
    script = _ig_script(visible={"Select from computer": True})
    check_golden("ig_narrow_layout", _run_ig(monkeypatch, media, script))


def test_ig_session_expiry_detected_by_password_field(monkeypatch, tmp_sessions, media, allow_browser_post_media):
    """The Create button is absent and the URL does not say /login, so the
    flow probes the DOM for a login form instead. This is the second, weaker
    expiry signal and it had no transcript."""
    script = _ig_script()
    script.evaluate['aria-label="New post"'] = "no_svg"
    script.evaluate['input[type="password"]'] = True
    check_golden("ig_expiry_via_password_probe", _run_ig(monkeypatch, media, script))


def test_tt_second_confirmation_is_skipped_once_confirmed(monkeypatch, tt_cookie_session, media, allow_browser_post_media):
    """Issue #29: the second confirmation block is dead cost on a post the
    first block already confirmed — it can only re-set `confirmed` True,
    never clear it — so it must not run.

    Asserted against the transcripts rather than the source: the confirmed
    run must contain exactly one confirmation wait, and the unconfirmed run
    must still contain both plus the 10s still-on-/upload fallback. That
    second half is the part worth guarding; narrowing it would turn "we did
    not see it succeed" into a silently confirmed post.
    """
    confirmed = (GOLDEN_DIR / "tt_normal_post.trace").read_text()
    unconfirmed = (GOLDEN_DIR / "tt_unconfirmed_post.trace").read_text()

    second_wait = "div:has-text('are being uploaded')"
    assert second_wait not in confirmed
    assert second_wait in unconfirmed
    assert "sleep 10.000" not in confirmed
    assert "sleep 10.000" in unconfirmed
    assert "RETURN 'tt_post_ok_A'" in confirmed
    assert "RETURN 'tt_post_unconfirmed_A'" in unconfirmed


# ---------------------------------------------------------------------------
# The tripwire covering these flows
# ---------------------------------------------------------------------------

def test_browser_post_media_is_tripwired_without_the_opt_in(tmp_sessions, media):
    """Batch 6 added the two browser `post_media` functions to the conftest
    tripwire. Assert the guard is genuinely armed rather than trusting that
    the fixture list says so.

    Note this test deliberately does NOT request `allow_browser_post_media` —
    it is the control case for every other test in this file, all of which do.
    Without the opt-in, reaching either function must fail loudly.
    """
    import asyncio

    for module in (instagram_browser, tiktok_browser):
        with pytest.raises(AssertionError, match="live posting/caption path"):
            asyncio.run(module.post_media("A", media, "caption", "video"))


# ---------------------------------------------------------------------------
# BE-23 — platform login knowledge lives in the platform modules
# ---------------------------------------------------------------------------

def test_login_markers_union_matches_the_original_literals():
    """The equivalence pin for the BE-23 move.

    `session_manager._is_login_redirect` used to hardcode
    `"/accounts/login" in u or "/login" in u`. It now matches the union of
    the two platform modules' marker tuples. That is only a safe refactor if
    the union is the *same set of literals*, so assert the set rather than
    trusting the reading — a later edit that drops a marker from one module
    would silently narrow expired-session detection, and a session wrongly
    judged live is a post attempted against a login wall.
    """
    union = set(instagram_browser.LOGIN_REDIRECT_MARKERS) | set(
        tiktok_browser.LOGIN_REDIRECT_MARKERS
    )
    assert union == {"/accounts/login", "/login"}


@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/accounts/login/", True),
    ("https://www.instagram.com/accounts/login/?next=/", True),
    ("https://www.instagram.com/", False),
    ("https://www.instagram.com/p/abc123/", False),
    ("HTTPS://WWW.INSTAGRAM.COM/ACCOUNTS/LOGIN/", True),
    ("", False),
])
def test_instagram_markers_classify_its_own_urls(url, expected):
    """The Instagram posting path aborts on exactly these URLs. Case
    handling is a deliberate widening over the previous literal
    `"/login" in page.url`: matching more login walls can only ever turn a
    doomed post into a clear error, never the reverse."""
    assert url_matches_login_markers(
        url, instagram_browser.LOGIN_REDIRECT_MARKERS
    ) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.tiktok.com/login", True),
    ("https://www.tiktok.com/login?redirect=upload", True),
    ("https://www.tiktok.com/upload", False),
    ("https://www.tiktok.com/foryou", False),
    ("", False),
])
def test_tiktok_markers_classify_its_own_urls(url, expected):
    assert url_matches_login_markers(
        url, tiktok_browser.LOGIN_REDIRECT_MARKERS
    ) is expected


def test_session_manager_no_longer_hardcodes_platform_login_urls():
    """BE-23, source-level: the health check must not re-derive what a login
    URL looks like. If it does, the two copies can drift and the check will
    disagree with the posting path about whether a session is alive."""
    import inspect

    from backend import session_manager

    src = inspect.getsource(session_manager._is_login_redirect)
    assert '"/accounts/login"' not in src
    assert "LOGIN_REDIRECT_MARKERS" in src
    # ...and the TikTok modal selectors are gone from the module entirely.
    module_src = inspect.getsource(session_manager)
    assert "loginContainer" not in module_src
    assert "login-modal" not in module_src
