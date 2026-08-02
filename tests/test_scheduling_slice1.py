"""Slice 1 — cookie write-back + session health check.

Covers the pure helpers and non-browser logic specified in
DESIGN-scheduling.md §3 (cookie format normalization, TikTok cookie
write-back, login-redirect detection, check_session state machine, and the
`session_manager check` CLI dispatch). The browser-opening paths cannot be
E2E tested (would require a real browser — forbidden), so those are covered
by source-level assertions, explicitly labeled below.

All tests use tmp_path / the tmp_sessions fixture and never touch real
sessions, real cookies, or real browsers.
"""

import asyncio
import inspect
import json
import sys

import pytest

from tests.source_probe import real_source
from backend import session_manager, tiktok_browser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeContext:
    """Stand-in for a Playwright BrowserContext exposing only .cookies()."""

    def __init__(self, cookies, raise_on_cookies=False):
        self._cookies = cookies
        self._raise = raise_on_cookies

    async def cookies(self):
        if self._raise:
            raise RuntimeError("boom reading cookies")
        return list(self._cookies)


# ---------------------------------------------------------------------------
# 3a — cookie format round-trip (_playwright_to_cookie_editor / _normalize)
# ---------------------------------------------------------------------------

def test_roundtrip_normal_cookie_preserves_effective_fields():
    """DESIGN §3a: Playwright → Cookie-Editor → _normalize_cookies keeps the
    effective cookie identical for a persistent, secure, Lax cookie."""
    pw_cookie = {
        "name": "sessionid", "value": "abc123", "domain": ".tiktok.com",
        "path": "/", "expires": 1893456000.0, "httpOnly": True,
        "secure": True, "sameSite": "Lax",
    }
    editor = tiktok_browser._playwright_to_cookie_editor(pw_cookie)
    assert editor["expirationDate"] == 1893456000.0
    assert editor["session"] is False
    assert editor["sameSite"] == "lax"
    assert editor["hostOnly"] is False  # domain starts with "."
    assert editor["storeId"] == "0"

    back = tiktok_browser._normalize_cookies([editor])[0]
    assert back["name"] == "sessionid"
    assert back["value"] == "abc123"
    assert back["domain"] == ".tiktok.com"
    assert back["path"] == "/"
    assert back["expires"] == 1893456000.0
    assert back["sameSite"] == "Lax"
    assert back["secure"] is True
    assert back["httpOnly"] is True


def test_roundtrip_samesite_none_cookie():
    """DESIGN §3a: sameSite=None is a lossy corner — it must survive the
    Playwright('None') → Cookie-Editor('no_restriction') → Playwright('None')
    round-trip."""
    pw_cookie = {
        "name": "tt_csrf", "value": "z", "domain": ".tiktok.com", "path": "/",
        "expires": 1893456000.0, "httpOnly": False, "secure": True,
        "sameSite": "None",
    }
    editor = tiktok_browser._playwright_to_cookie_editor(pw_cookie)
    assert editor["sameSite"] == "no_restriction"

    back = tiktok_browser._normalize_cookies([editor])[0]
    assert back["sameSite"] == "None"


def test_roundtrip_session_cookie_expires_minus_one():
    """DESIGN §3a: expires == -1 is a session cookie — expirationDate is
    omitted, session=True, and no 'expires' key comes back."""
    pw_cookie = {
        "name": "s", "value": "v", "domain": "www.tiktok.com", "path": "/",
        "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Strict",
    }
    editor = tiktok_browser._playwright_to_cookie_editor(pw_cookie)
    assert editor["session"] is True
    assert "expirationDate" not in editor
    assert editor["hostOnly"] is True  # domain does not start with "."

    back = tiktok_browser._normalize_cookies([editor])[0]
    assert "expires" not in back
    assert back["sameSite"] == "Strict"


def test_playwright_to_cookie_editor_unknown_samesite_is_unspecified():
    """DESIGN §3a: missing/unknown sameSite maps to 'unspecified'."""
    assert tiktok_browser._playwright_to_cookie_editor({"name": "n", "value": "v",
        "domain": ".tiktok.com"})["sameSite"] == "unspecified"
    assert tiktok_browser._playwright_to_cookie_editor({"name": "n", "value": "v",
        "domain": ".tiktok.com", "sameSite": "weird"})["sameSite"] == "unspecified"


# ---------------------------------------------------------------------------
# 3a — write-back logic
# ---------------------------------------------------------------------------

def test_write_back_writes_tiktok_cookies_on_success(tmp_sessions):
    """DESIGN §3a: a successful post writes the fresh tiktok.com cookies to
    the session file in Cookie-Editor format."""
    ctx = _FakeContext([
        {"name": "sessionid", "value": "live", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0, "secure": True,
         "httpOnly": True, "sameSite": "Lax"},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))

    cookie_file = tiktok_browser._cookies_file("A")
    assert cookie_file.exists()
    data = json.loads(cookie_file.read_text())
    assert data[0]["name"] == "sessionid"
    assert data[0]["expirationDate"] == 1893456000.0
    assert data[0]["storeId"] == "0"


def test_write_back_creates_bak_before_writing(tmp_sessions):
    """DESIGN §3a: the current cookie file is backed up to .bak before the
    fresh cookies overwrite it."""
    cookie_file = tiktok_browser._cookies_file("A")
    cookie_file.write_text(json.dumps([{"name": "sessionid", "value": "OLD"}]))

    ctx = _FakeContext([
        {"name": "sessionid", "value": "NEW", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))

    bak = cookie_file.with_name(cookie_file.name + ".bak")
    assert bak.exists()
    assert json.loads(bak.read_text())[0]["value"] == "OLD"
    assert json.loads(cookie_file.read_text())[0]["value"] == "NEW"


def test_write_back_filters_non_tiktok_domains(tmp_sessions):
    """DESIGN §3a: Playwright returns cookies for every domain the context
    touched — only tiktok.com cookies may enter the session file."""
    ctx = _FakeContext([
        {"name": "sessionid", "value": "v", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
        {"name": "_ga", "value": "analytics", "domain": ".google-analytics.com",
         "path": "/", "expires": 1893456000.0},
        {"name": "cdn", "value": "x", "domain": ".tiktokcdn.example",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))

    data = json.loads(tiktok_browser._cookies_file("A").read_text())
    domains = {c["domain"] for c in data}
    assert domains == {".tiktok.com"}


def test_write_back_skips_when_no_sessionid(tmp_sessions):
    """DESIGN §3a: with no tiktok.com sessionid cookie, skip the write-back
    rather than overwrite a working session file."""
    cookie_file = tiktok_browser._cookies_file("A")
    cookie_file.write_text(json.dumps([{"name": "sessionid", "value": "KEEP"}]))

    ctx = _FakeContext([
        {"name": "tt_csrf", "value": "v", "domain": ".tiktok.com", "path": "/",
         "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))

    # Untouched: original preserved, no .bak created.
    assert json.loads(cookie_file.read_text())[0]["value"] == "KEEP"
    assert not cookie_file.with_name(cookie_file.name + ".bak").exists()


def test_write_back_failure_does_not_raise(tmp_sessions):
    """DESIGN §3a: a write-back failure is logged but never raises — it must
    never fail the post. Here reading cookies raises."""
    ctx = _FakeContext([], raise_on_cookies=True)
    # Must not raise.
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))
    assert not tiktok_browser._cookies_file("A").exists()


def test_write_back_write_error_does_not_raise(tmp_sessions):
    """DESIGN §3a: a failure early in the write is swallowed. Making the
    target path a directory makes the .bak `shutil.copyfile` step raise
    IsADirectoryError before any temp write — the outer except swallows it."""
    cookie_file = tiktok_browser._cookies_file("A")
    cookie_file.mkdir()  # copyfile(cookie_file, ...bak) raises IsADirectoryError

    ctx = _FakeContext([
        {"name": "sessionid", "value": "v", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))  # must not raise


def test_write_back_only_on_success_not_exception_source_level():
    """DESIGN §3a (source-level assertion — browser path can't be E2E tested):
    post_media invokes _write_back_cookies exactly once, in the success path
    before the return, and never inside the exception handler."""
    src = _real_source(tiktok_browser, "post_media")
    assert src.count("_write_back_cookies(") == 1
    call_pos = src.index("_write_back_cookies(")
    confirmed_pos = src.index("if not confirmed:")
    return_pos = src.index('return _post_id("tt_post"')
    # The single call sits in the success path: after the confirmation check
    # and immediately before the success return, not in any except handler.
    assert confirmed_pos < call_pos < return_pos


# ---------------------------------------------------------------------------
# 3b — _is_login_redirect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/accounts/login/", True),
    ("https://www.instagram.com/accounts/login/?next=/", True),
    ("https://www.tiktok.com/login", True),
    ("https://www.tiktok.com/login?redirect=upload", True),
    ("https://www.instagram.com/", False),
    ("https://www.tiktok.com/foryou", False),
    ("https://www.tiktok.com/@someuser", False),
    ("", False),
    ("HTTPS://WWW.TIKTOK.COM/LOGIN", True),  # case-insensitive
])
def test_is_login_redirect(url, expected):
    """DESIGN §3b: login-wall detection for IG /accounts/login/ and TT /login."""
    assert session_manager._is_login_redirect(url) is expected


# ---------------------------------------------------------------------------
# 3b — check_session state machine
# ---------------------------------------------------------------------------

def test_check_session_no_session_when_missing(tmp_sessions):
    """DESIGN §3b: a missing session returns 'no_session', not an error, and
    never opens a browser."""
    assert asyncio.run(session_manager.check_session("A", "instagram")) == "no_session"
    assert asyncio.run(session_manager.check_session("A", "tiktok")) == "no_session"


def test_check_session_check_error_on_exception(tmp_sessions, monkeypatch):
    """DESIGN §3b: any exception during the check collapses to 'check_error'
    (unknown, not dead). Session exists so we get past the no_session guard."""
    d = tmp_sessions["instagram"] / "A"
    d.mkdir()
    (d / "Default").mkdir()

    async def _boom(slot, platform):
        raise RuntimeError("navigation blew up")

    monkeypatch.setattr(session_manager, "_run_session_check", _boom)
    assert asyncio.run(session_manager.check_session("A", "instagram")) == "check_error"


def test_check_session_check_error_on_timeout(tmp_sessions, monkeypatch):
    """DESIGN §3b: a timeout collapses to 'check_error'. Shrink the per-slot
    timeout and make the check overrun it."""
    d = tmp_sessions["instagram"] / "A"
    d.mkdir()
    (d / "Default").mkdir()

    async def _slow(slot, platform):
        await asyncio.sleep(1)
        return "live"

    monkeypatch.setattr(session_manager, "_run_session_check", _slow)
    monkeypatch.setattr(session_manager, "SESSION_CHECK_TIMEOUT_S", 0.01)
    assert asyncio.run(session_manager.check_session("A", "instagram")) == "check_error"


def test_check_session_passes_through_live_and_expired(tmp_sessions, monkeypatch):
    """DESIGN §3b: a clean check result flows straight through check_session.

    Each leg uses a *different slot*. Originally both ran against slot A, but
    the F5 health cache (RESEARCH-platform-detection.md) now serves
    the second call from the first call's cached "live", so a same-slot
    sequence no longer exercises pass-through at all. The pass-through
    contract this test protects is unchanged — only the setup needed to
    observe it is. The caching behaviour itself is covered in
    test_health_cache.py.
    """
    for slot in ("A", "B"):
        cookies = tmp_sessions["tiktok"] / f"{slot}_cookies.json"
        cookies.write_text(json.dumps([{"name": "sessionid", "value": "x" * 20}]))

    async def _live(slot, platform):
        return "live"

    monkeypatch.setattr(session_manager, "_run_session_check", _live)
    assert asyncio.run(session_manager.check_session("A", "tiktok")) == "live"

    async def _expired(slot, platform):
        return "expired"

    monkeypatch.setattr(session_manager, "_run_session_check", _expired)
    assert asyncio.run(session_manager.check_session("B", "tiktok")) == "expired"


# ---------------------------------------------------------------------------
# 3b — `check` CLI subcommand dispatch
# ---------------------------------------------------------------------------

def test_cli_check_dispatches_all_slots(monkeypatch):
    """DESIGN §3b: `check` with no --slot runs the health check for every slot
    (slot_filter=None)."""
    calls = []

    async def _fake_run_check(slot_filter=None):
        calls.append(slot_filter)

    monkeypatch.setattr(session_manager, "run_check", _fake_run_check)
    monkeypatch.setattr(sys, "argv", ["session_manager", "check"])
    session_manager.main()
    assert calls == [None]


def test_cli_check_dispatches_single_slot(monkeypatch):
    """DESIGN §3b: `check --slot A` passes the upper-cased slot through."""
    calls = []

    async def _fake_run_check(slot_filter=None):
        calls.append(slot_filter)

    monkeypatch.setattr(session_manager, "run_check", _fake_run_check)
    monkeypatch.setattr(sys, "argv", ["session_manager", "check", "--slot", "a"])
    session_manager.main()
    assert calls == ["A"]


def test_cli_check_invalid_slot_exits(monkeypatch):
    """DESIGN §3b: an unknown slot is rejected before any check runs."""
    monkeypatch.setattr(sys, "argv", ["session_manager", "check", "--slot", "Z"])
    with pytest.raises(SystemExit):
        session_manager.main()


def test_cli_check_missing_slot_value_exits(monkeypatch):
    """DESIGN §3b: `check --slot` with no value is rejected."""
    monkeypatch.setattr(sys, "argv", ["session_manager", "check", "--slot"])
    with pytest.raises(SystemExit):
        session_manager.main()


# ===========================================================================
# Slice 1 review fixes
# ===========================================================================

# Moved to tests/source_probe.py in Batch 6, when the browser post_media
# functions joined the tripwire and every source-level pin needed it. Kept
# under the local name so the call sites below are unchanged.
_real_source = real_source


# --- Finding 1: atomic write ------------------------------------------------

def test_write_back_uses_atomic_replace_source_level():
    """Review fix: browser-reliability finding 1 — atomic write. The live
    cookie file is written via a temp file + os.replace, never truncated in
    place with open(cookie_file, "w")."""
    src = inspect.getsource(tiktok_browser._write_back_cookies)
    assert "os.replace(" in src
    assert 'open(cookie_file, "w")' not in src


def test_write_back_atomic_leaves_original_intact_on_write_failure(tmp_sessions, monkeypatch):
    """Review fix: browser-reliability finding 1 (atomic write) + test-hygiene
    7a — when the backup succeeds but the write step fails (os.replace raises),
    the write-back must not raise and the live file stays byte-identical."""
    cookie_file = tiktok_browser._cookies_file("A")
    original = json.dumps([{"name": "sessionid", "value": "ORIGINAL"}])
    cookie_file.write_text(original)
    original_bytes = cookie_file.read_bytes()

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(tiktok_browser.os, "replace", _boom)

    ctx = _FakeContext([
        {"name": "sessionid", "value": "NEW", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))  # must not raise

    assert cookie_file.read_bytes() == original_bytes
    # The partial temp file is cleaned up, never left behind.
    assert not cookie_file.with_name(cookie_file.name + ".tmp").exists()


# --- Finding 2: health-check browser lifecycle ------------------------------

def test_tiktok_get_context_returns_browser_handle_source_level():
    """Review fix: lifecycle finding 2a — _get_context exposes the browser
    handle so cookie-path health checks can close the Chromium process instead
    of leaking a Chrome per check."""
    src = inspect.getsource(tiktok_browser._get_context)
    assert "context, browser = await _get_context_from_cookies" in src
    assert "return context, browser" in src
    assert "return context, None" in src  # persistent path returns a handle too


def test_run_session_check_lifecycle_source_level():
    """Review fix: lifecycle finding 2b/2c — handles are acquired inside the
    try (so an outer wait_for cancel still reaches the finally), both context
    and browser are closed in the finally, and the inner goto timeout is
    shorter than the outer per-slot timeout so the graceful path is reachable."""
    src = _real_source(session_manager, "_run_session_check")
    try_pos = src.index("try:")
    finally_pos = src.index("finally:")
    # Acquisition (the _get_context calls) sits after `try:`, before `finally:`.
    assert try_pos < src.index("_get_context") < finally_pos
    # Both handles are closed in the finally block.
    assert "for handle in (context, browser)" in src[finally_pos:]
    assert ".close()" in src[finally_pos:]
    # Inner goto timeout strictly below the outer per-slot wait_for.
    assert session_manager.SESSION_CHECK_GOTO_TIMEOUT_S < session_manager.SESSION_CHECK_TIMEOUT_S


# --- Finding 3: TikTok login-modal detection --------------------------------

@pytest.mark.parametrize("url,modal,expected", [
    ("https://www.tiktok.com/foryou", False, "live"),
    ("https://www.tiktok.com/foryou", True, "expired"),   # modal despite good URL
    ("https://www.tiktok.com/login", False, "expired"),   # URL redirect only
    ("https://www.tiktok.com/login", True, "expired"),
])
def test_tiktok_session_status(url, modal, expected):
    """Review fix: modal-detection finding 3 — a login modal OR a /login
    redirect marks the TikTok session expired (DESIGN §3b step 3)."""
    assert session_manager._tiktok_session_status(url, modal) == expected


def test_run_session_check_probes_login_modal_source_level():
    """Review fix: modal-detection finding 3 (source-level — DOM query can't be
    E2E tested) — the TikTok branch probes the DOM for a login modal with a
    fallback selector chain, not just the URL."""
    src = _real_source(session_manager, "_run_session_check")
    assert "_tiktok_login_modal_present" in src
    # BE-23 (issue #28) moved the probe into tiktok_browser and its selectors
    # into a module constant; session_manager's name is now an alias. Assert
    # the alias and the chain, so the coverage this test protects survived the
    # move rather than being quietly dropped with it.
    assert session_manager._tiktok_login_modal_present is tiktok_browser.login_modal_present
    selectors = "".join(tiktok_browser.LOGIN_MODAL_SELECTORS)
    assert "login-modal" in selectors
    assert "loginContainer" in selectors
    assert 'id*="login"' in selectors
    assert "LOGIN_MODAL_SELECTORS" in inspect.getsource(tiktok_browser.login_modal_present)


# --- Finding 4: domain filter -----------------------------------------------

@pytest.mark.parametrize("domain,ok", [
    ("tiktok.com", True),
    (".tiktok.com", True),
    ("www.tiktok.com", True),
    ("faketiktok.com", False),
    ("x.tiktok.com.evil.example", False),
    ("tiktok.com.evil.example", False),
    ("", False),
])
def test_is_tiktok_domain(domain, ok):
    """Review fix: domain-filter finding 4 — anchored suffix match rejects
    lookalike domains that a bare substring test would accept."""
    assert tiktok_browser._is_tiktok_domain(domain) is ok


# --- Finding 5: tripwire extension ------------------------------------------

def test_tripwire_blocks_unstubbed_run_session_check(tmp_sessions):
    """Review fix: tripwire finding 5 — conftest stubs _run_session_check so a
    test with an existing session that forgets to monkeypatch it hits the
    tripwire rather than launching real Chromium."""
    with pytest.raises(RuntimeError, match="tripwire"):
        asyncio.run(session_manager._run_session_check("A", "tiktok"))


def test_check_session_existing_session_collapses_tripwire_to_check_error(tmp_sessions):
    """Review fix: tripwire finding 5 — with an existing session and no explicit
    stub, check_session reaches the tripwire and collapses it to check_error
    (never a real browser launch)."""
    cookies = tmp_sessions["tiktok"] / "A_cookies.json"
    cookies.write_text(json.dumps([{"name": "sessionid", "value": "x" * 20}]))
    assert asyncio.run(session_manager.check_session("A", "tiktok")) == "check_error"


# --- Finding 6: write-back auth-mode switch ---------------------------------

def test_write_back_skipped_when_profile_based(tmp_sessions):
    """Review fix: auth-mode-switch finding 6 — a profile-fallback session also
    carries a sessionid cookie; writing it out would create a cookie file and
    flip the slot to cookie-preferred auth. Skip when the context wasn't
    cookie-based."""
    ctx = _FakeContext([
        {"name": "sessionid", "value": "v", "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A", from_cookie_session=False))
    assert not tiktok_browser._cookies_file("A").exists()


def test_post_media_threads_cookie_origin_to_write_back_source_level():
    """Review fix: auth-mode-switch finding 6 (source-level) — post_media
    passes its cookie-session origin through to the write-back call."""
    src = _real_source(tiktok_browser, "post_media")
    assert "from_cookie_session=used_cookie_session" in src


# --- Finding 7: test hygiene ------------------------------------------------

def test_run_session_check_never_posts_or_writes_source_level():
    """Review fix: test-hygiene 7b — _run_session_check's body contains no call
    to post_media and no cookie-file writes; it only reads session state."""
    src = _real_source(session_manager, "_run_session_check")
    assert "post_media" not in src
    assert "_write_back_cookies" not in src
    assert "_cookies_file" not in src
    assert "open(" not in src


def test_write_back_never_logs_cookie_values(tmp_sessions, capsys):
    """Review fix: test-hygiene 7c — write-back log output never contains a
    cookie value from the context."""
    secret = "SUPERSECRETSESSIONID_zzz999"
    ctx = _FakeContext([
        {"name": "sessionid", "value": secret, "domain": ".tiktok.com",
         "path": "/", "expires": 1893456000.0},
    ])
    asyncio.run(tiktok_browser._write_back_cookies(ctx, "A"))
    out = capsys.readouterr().out
    assert secret not in out
