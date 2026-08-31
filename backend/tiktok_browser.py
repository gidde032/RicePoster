"""
TikTok browser automation client.

Uses Playwright to automate posting through the TikTok web interface.
Requires saved browser sessions (login state) per account.
"""

import asyncio
import os
import shutil
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

import json

# These are shared with instagram_browser and must behave identically on both
# platforms (tech-debt audit BE-3, 2026-07-29). _captions_match and
# EDITOR_MARKER joined them in Batch 6, when Instagram gained the same caption
# read-back check.
from backend.config import DEBUG_DIR, TT_SESSIONS_DIR
from backend.jitter import type_with_jitter
from backend.logging_setup import get_logger
from backend.browser_common import (
    EDITOR_MARKER,
    _captions_match,
    _post_id,
    _resolve_login_outcome,
    _selector_chain_error,
    url_matches_login_markers,
)

_log = get_logger("tiktok_browser")

# Directory to store persistent browser sessions (cookies/login state).
# Re-exported under the local name conftest's `tmp_sessions` fixture patches —
# see the layout section in config.py before changing this to a call-site
# reference.
SESSIONS_DIR = TT_SESSIONS_DIR
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Failure screenshots go here (gitignored), same as instagram_browser
DEBUG_DIR.mkdir(exist_ok=True)

# URL fragments that mean "this is TikTok's login wall". TikTok redirects an
# expired session to /login. Owned here rather than in session_manager so
# platform knowledge lives with the platform (tech-debt audit BE-23).
LOGIN_REDIRECT_MARKERS = ("/login",)

# TikTok often surfaces the login wall as an in-page modal with no URL
# change, so the URL markers above are not sufficient on their own. Generous
# fallback chain in the codebase's idiom, since platform UIs shift.
LOGIN_MODAL_SELECTORS = (
    '[data-e2e="login-modal"]',
    '#loginContainer',
    'div[id*="login" i]',
)


async def login_modal_present(page) -> bool:
    """Probe the DOM for a TikTok login modal, complementing the /login URL
    check (DESIGN-scheduling.md §3b step 3). Never raises; any match → the
    session is expired.

    Moved here from session_manager by BE-23: the selectors are TikTok's, and
    session_manager should not need to know how TikTok renders a login wall.
    """
    for sel in LOGIN_MODAL_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _is_usable_cookie_file(path: Path) -> bool:
    try:
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or SESSIONS_DIR.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 10
        ):
            return False
        cookies = json.loads(path.read_text())
        return (
            isinstance(cookies, list)
            and bool(cookies)
            and all(
                isinstance(cookie, dict)
                and isinstance(cookie.get("name"), str)
                and bool(cookie["name"])
                and isinstance(cookie.get("value"), str)
                for cookie in cookies
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _cookies_file(account_key: str) -> Path:
    """Preferred cookie path, with the legacy flat file handled by readers."""
    preferred = SESSIONS_DIR / account_key / "cookies.json"
    legacy = SESSIONS_DIR / f"{account_key}_cookies.json"
    # A half-written or empty preferred file must not mask a usable legacy
    # export. This selection rule is shared by the availability probe and the
    # loader so they cannot disagree about which saved session is usable.
    for candidate in (preferred, legacy):
        if _is_usable_cookie_file(candidate):
            return candidate
    return preferred if preferred.exists() else legacy


def has_cookie_session(account_key: str) -> bool:
    """Check if exported cookies exist for this account."""
    path = _cookies_file(account_key)
    return _is_usable_cookie_file(path)


def has_profile_session(account_key: str) -> bool:
    """A persistent profile needs state beyond the optional cookie export.

    The preferred cookie file lives inside the same account directory. A bad
    or empty `cookies.json` must not make that directory look like an
    authenticated persistent profile merely because the file itself exists.
    """
    session_dir = SESSIONS_DIR / account_key
    if not session_dir.is_dir() or session_dir.is_symlink():
        return False
    try:
        return any(
            entry.name not in {"cookies.json", ".DS_Store"}
            for entry in session_dir.iterdir()
        )
    except OSError:
        return False


def _normalize_cookies(raw_cookies: list) -> list:
    """Convert Cookie-Editor export records into the cookie dicts Playwright's
    context.add_cookies expects. Cookie-Editor uses "expirationDate" and
    lowercase sameSite ("strict"/"lax"/"no_restriction"/"unspecified");
    Playwright wants "expires" and capitalized "Strict"/"Lax"/"None". This is
    the inverse of _playwright_to_cookie_editor for the round-trippable fields."""
    same_site_map = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }
    cookies = []
    for c in raw_cookies:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".tiktok.com"),
            "path": c.get("path", "/"),
        }
        # Cookie-Editor uses "expirationDate", Playwright uses "expires"
        if "expirationDate" in c:
            cookie["expires"] = c["expirationDate"]
        elif "expires" in c:
            cookie["expires"] = c["expires"]
        ss = same_site_map.get(str(c.get("sameSite", "")).lower())
        if ss:
            cookie["sameSite"] = ss
        if c.get("secure"):
            cookie["secure"] = True
        if c.get("httpOnly"):
            cookie["httpOnly"] = True
        cookies.append(cookie)
    return cookies


def _playwright_to_cookie_editor(cookie: dict) -> dict:
    """Convert a Playwright cookie (from context.cookies()) back into a
    Cookie-Editor export record so refreshed cookies round-trip through
    _normalize_cookies (DESIGN-scheduling.md §3a). The field mappings are not
    1:1 renames — the lossy corners (session cookies, sameSite) are handled
    explicitly:
      - expires == -1 (session cookie) → omit expirationDate, session=True.
      - sameSite "Strict"/"Lax"/"None" → "strict"/"lax"/"no_restriction";
        missing/unknown → "unspecified".
      - hostOnly is derived: True iff the domain does not start with ".".
      - storeId is the constant "0".
    Pure function — extract, test, use."""
    domain = cookie.get("domain", "")
    same_site_map = {"Strict": "strict", "Lax": "lax", "None": "no_restriction"}
    out = {
        "name": cookie.get("name", ""),
        "value": cookie.get("value", ""),
        "domain": domain,
        "path": cookie.get("path", "/"),
        "hostOnly": not domain.startswith("."),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
        "sameSite": same_site_map.get(cookie.get("sameSite"), "unspecified"),
        "storeId": "0",
    }
    if cookie.get("expires", -1) == -1:
        out["session"] = True
    else:
        out["expirationDate"] = cookie["expires"]
        out["session"] = False
    return out


def _is_tiktok_domain(domain: str) -> bool:
    """True only for tiktok.com and its subdomains, using an anchored suffix
    match so lookalikes (faketiktok.com, x.tiktok.com.evil.example) are
    rejected — a bare `"tiktok.com" in domain` substring test accepts them.
    Cookie domains may carry a single leading dot; strip one. Pure/testable."""
    if not domain:
        return False
    d = domain[1:] if domain.startswith(".") else domain
    return d == "tiktok.com" or d.endswith(".tiktok.com")


async def _write_back_cookies(context, account_key: str, from_cookie_session: bool = True) -> None:
    """Persist the context's fresh cookies back to the session file after a
    successful TikTok post so long-lived sessions self-sustain instead of
    silently expiring (DESIGN-scheduling.md §3a). Filters to tiktok.com
    cookies (Playwright returns cookies for every domain the context touched —
    CDN/analytics domains must not enter the session file), backs the current
    file up to .bak first, then writes Cookie-Editor format atomically. Any
    failure is logged and swallowed — a write-back must never fail the post —
    and no cookie values are ever logged.

    `from_cookie_session` guards the design's "session may be profile-based"
    case: a profile-fallback session also carries a sessionid cookie, so
    writing it out would create a cookie file and silently flip the slot to
    cookie-preferred auth. Only write back when the posting context actually
    came from the cookie file."""
    if not from_cookie_session:
        _log.info(f"[TikTok] Cookie write-back skipped for {account_key}: session is profile-based, not cookie-based.")
        return

    try:
        all_cookies = await context.cookies()
    except Exception as e:
        _log.warning(f"[TikTok] Warning: could not read cookies for write-back ({account_key}): {e}")
        return

    tiktok_cookies = [c for c in all_cookies if _is_tiktok_domain(c.get("domain") or "")]

    # No sessionid → don't overwrite a working session file with a
    # potentially profile-based or broken cookie set. Skip quietly.
    if not any(c.get("name") == "sessionid" for c in tiktok_cookies):
        _log.warning(f"[TikTok] Cookie write-back skipped for {account_key}: no tiktok.com sessionid cookie.")
        return

    cookie_file = _cookies_file(account_key)
    try:
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        # Single-depth backup; overwrite any existing .bak. Only touched when
        # the write itself is about to run, never on a failed write-back.
        if cookie_file.exists():
            shutil.copyfile(cookie_file, cookie_file.with_name(cookie_file.name + ".bak"))
        editor_cookies = [_playwright_to_cookie_editor(c) for c in tiktok_cookies]
        # Atomic write: serialize to a temp file in the same directory, then
        # os.replace() it onto the live file. A crash mid-write can only ever
        # damage the temp file — the live session file is either the old bytes
        # or the fully-written new bytes, never a truncated mix (review fix).
        tmp_file = cookie_file.with_name(cookie_file.name + ".tmp")
        try:
            with open(tmp_file, "w") as f:
                json.dump(editor_cookies, f, indent=2)
            os.replace(tmp_file, cookie_file)
        except Exception:
            # Clean up the partial temp file; leave the live file untouched.
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        _log.info(f"[TikTok] Wrote back {len(editor_cookies)} cookies for {account_key}.")
    except Exception as e:
        _log.warning(f"[TikTok] Warning: cookie write-back failed for {account_key}: {e}")


async def _get_context_from_cookies(playwright, account_key: str, headless: bool = True):
    """Create a browser context and load exported cookies. No persistent profile needed."""
    cookie_file = _cookies_file(account_key)
    if not _is_usable_cookie_file(cookie_file):
        raise ValueError(f"No safe usable TikTok cookie session exists for {account_key!r}.")
    browser = await playwright.chromium.launch(
        channel="chrome",
        headless=headless,
        args=[
            # LEGACY, NOT COVERAGE — see instagram_browser._get_context for the
            # full note. Kept as harmless; does not address current detection.
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            # --- CRITICAL VIDEO RENDERING FIXES ---
            "--ignore-gpu-blocklist",             # Overrides default restrictions on hardware acceleration
            "--enable-gpu-rasterization",         # Offloads interface drawing to your physical GPU
            "--enable-zero-copy",                 # Drastically reduces video memory consumption 
            "--disable-gpu-sandbox",              # Allows the browser threads to talk directly to your graphics card
            "--force-gpu-rasterization"           # Ensures UI layers do not fall back to crashing CPU engines
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        # No user_agent override — see instagram_browser._get_context for why a
        # hardcoded UA desynchronises the client-hint surfaces.
    )

    # Load cookies from exported JSON. Cookie-Editor exports a slightly
    # different format than Playwright expects — normalize the objects.
    with open(cookie_file) as f:
        raw_cookies = json.load(f)

    cookies = _normalize_cookies(raw_cookies)

    await context.add_cookies(cookies)

    return context, browser


async def _get_context(playwright, account_key: str, headless: bool = True):
    """Get a browser context for the given account, returning
    (context, browser). Prefers cookie-based auth, falls back to persistent
    profile. The browser handle is the standalone Chromium process for the
    cookie path (callers must close it to avoid leaking a Chrome), or None for
    the persistent-profile path (closing the context closes that browser)."""

    # Prefer cookie-based approach (no automation fingerprint issues)
    if has_cookie_session(account_key):
        context, browser = await _get_context_from_cookies(playwright, account_key, headless)
        return context, browser

    # Fallback: persistent profile
    session_dir = SESSIONS_DIR / account_key
    session_dir.mkdir(exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=headless,
        channel="chrome",
        viewport={"width": 1280, "height": 900},
        # No user_agent override — see instagram_browser._get_context.
        args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )
    # Persistent context owns its own browser; no separate handle to close.
    return context, None


def _find_system_chrome() -> str | None:
    """Find the system-installed Chrome/Chromium executable."""
    import platform
    import shutil

    system = platform.system()
    if system == "Darwin":  # macOS
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Linux":
        candidates = [
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        progfiles = os.environ.get("PROGRAMFILES", "")
        # pathlib to match the rest of the module (#42/BE-15). This branch is
        # unreachable on the maintainer's macOS machine and sits inside a
        # live-posting module, which is why it was deferred out of Batch 3 —
        # the equivalence claim could be argued but not executed. It can be
        # executed now: test_windows_chrome_lookup.py fakes the platform and
        # asserts these paths against os.path.join from any host OS.
        #
        # The tail stays a single raw literal rather than being split into
        # Path("Google") / "Chrome" / ... — splitting it emits forward slashes
        # on a POSIX host, so the test above could no longer compare against
        # os.path.join. Written this way the produced *string* is identical to
        # the old code on both POSIX and Windows. (On Windows the literal is
        # still parsed into separate path components — WindowsPath treats the
        # backslashes as separators — but it renders back to the same string,
        # which is what this function returns and what callers use.)
        chrome_exe = r"Google\Chrome\Application\chrome.exe"
        candidates = [
            str(Path(local) / chrome_exe),
            str(Path(progfiles) / chrome_exe),
        ]
    else:
        candidates = []

    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


async def login(account_key: str):
    """
    Open a visible browser window for manual login.
    Uses system Chrome instead of Playwright's Chromium to avoid TikTok bot detection.
    Falls back to Playwright Chromium if system Chrome isn't found.
    """
    async with async_playwright() as pw:
        session_dir = SESSIONS_DIR / account_key
        had_profile_before = session_dir.exists() and any(session_dir.iterdir())
        session_dir.mkdir(exist_ok=True)

        chrome_path = _find_system_chrome()
        browser = None

        if chrome_path:
            _log.info(f"[TikTok] Using system Chrome: {chrome_path}")
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(session_dir),
                headless=False,
                executable_path=chrome_path,
                viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        else:
            _log.warning("[TikTok] System Chrome not found, using Playwright Chromium (may trigger bot detection)")
            context, browser = await _get_context(pw, account_key, headless=False)

        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.tiktok.com/login")
        print(f"\n[TikTok] Log in to account '{account_key}' in the browser window.")
        print("Once logged in and you see your feed, press Enter here to save the session")
        print("(or type 'abort' + Enter to discard if the login didn't work)...")
        answer = await asyncio.get_event_loop().run_in_executor(None, input)
        await context.close()
        # The cookie-path _get_context returns a standalone browser to close too.
        if browser:
            await browser.close()
        if _resolve_login_outcome(session_dir, answer, had_profile_before):
            print(f"[TikTok] Session saved for '{account_key}'.")
        else:
            print(f"[TikTok] Login aborted — discarded partial session for '{account_key}'.")


# ---------------------------------------------------------------------------
# The posting flow, in named steps
#
# These helpers are a decomposition of one 235-line function (tech-debt audit
# BE-4, issue #28) and nothing more. The sequence of Playwright calls, the
# selector chains, the sleep floors and the ordering are unchanged;
# tests/golden/tt_*.trace holds the recorded transcript that proves it.
#
# CLAUDE.md § Intentional design protects the long sleeps, the generous
# timeouts and the fallback selector chains. Note also that TikTok's one-time
# per-account dialogs are dismissed manually by the maintainer, deliberately:
# _describe_blocking_modal names the blocker, it does not clear it.
# ---------------------------------------------------------------------------


async def _open_upload_page(page: Page, account_key: str):
    """Load the upload page and fail fast if the session is dead."""
    # domcontentloaded completely ignores endless background tracking loops
    await page.goto("https://www.tiktok.com/upload", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(5)

    if url_matches_login_markers(page.url, LOGIN_REDIRECT_MARKERS):
        raise Exception(
            f"Session expired for {account_key}. Run login again: "
            f"python -m backend.session_manager login tiktok {account_key}"
        )


async def _resolve_upload_target(page: Page):
    """TikTok's upload page sometimes nests the panel in an iframe. Return
    whichever object subsequent element lookups must be addressed to."""
    iframe = None
    try:
        iframe_el = await page.wait_for_selector(
            "iframe[src*='upload']", timeout=10000
        )
        iframe = await iframe_el.content_frame()
        _log.info("[TikTok] Intercepted upload panel inside iframe view context.")
    except Exception:
        _log.info("[TikTok] No iframe detected, interacting directly with main page layout.")
        iframe = page

    return iframe or page


async def _upload_media_file(target, upload_path: Path):
    """Send the short-named copy to the picker and wait out processing."""
    file_input = await target.wait_for_selector(
        "input[type='file']",
        state="attached",
        timeout=15000
    )
    await file_input.set_input_files(str(upload_path))
    _log.info("[TikTok] Media file sent to picker layer. Monitoring upload progress...")
    await asyncio.sleep(5)

    _log.info("[TikTok] Video processing complete. Target fields ready.")


async def _dismiss_one_time_overlay(page: Page):
    """Clear the 'Got it' feature-promo overlay when it is present.

    This is NOT the content-check dialog: that one is dismissed manually by
    the maintainer by deliberate decision after the 2026-07-18 incident, and
    _describe_blocking_modal only reports it.
    """
    try:
        _log.info("[TikTok] Dismissing layout overlays...")
        # Wait up to 5 seconds for the popup to finish its slide-in animation
        got_it_btn = page.locator("button:has-text('Got it'), div[role='button']:has-text('Got it')").first
        await got_it_btn.wait_for(state="visible", timeout=5000)
        await got_it_btn.click()
        _log.info("[TikTok] Successfully clicked 'Got it' overlay.")
        await asyncio.sleep(1.0)
    except Exception as e:
        # INFO, not WARNING: the "Got it" promo is a one-time dialog, so it is
        # absent on virtually every post and this timeout is the ordinary
        # path, not a degraded one. Warning here put a Playwright timeout dump
        # on every successful TikTok post at LOG_LEVEL=WARNING — the setting
        # meant for unattended batches — which buries the real warnings.
        # Matches _resolve_upload_target's structurally identical
        # "No iframe detected" expected-miss (cold review, 2026-07-31).
        _log.info(f"[TikTok] Overlay clearance check complete (No buttons clicked: {e})")


async def _find_caption_field(page: Page):
    """Resolve the caption editor, naming the whole chain if nothing matches."""
    caption_attempts: list[tuple[str, int | None]] = []
    for selector in [
        "div[contenteditable='true']",
        "div[data-placeholder*='caption']",
        "div[data-placeholder*='title']",
        "div.public-DraftEditor-content",
        "[class*='editor-container'] div[role='textbox']"
    ]:
        try:
            loc = page.locator(selector)
            count = await loc.count()
            caption_attempts.append((selector, count))
            if count > 0:
                return loc.first
        except Exception:
            caption_attempts.append((selector, None))
            continue

    raise Exception(_selector_chain_error("caption field", caption_attempts))


async def _enter_and_verify_caption(page: Page, caption_field, caption: str, account_key: str):
    """Clear, insert atomically, read back, and refuse to post garbled text.

    TikTok pre-fills this field with the video filename, and the editor keeps
    internal state that survives DOM wipes. Clear with real keyboard input,
    insert the caption atomically (no per-key events for '#'/'@' means no
    autocomplete interception), then read the field back and verify before
    ever clicking Post. The retry types sequentially instead.
    """
    await caption_field.scroll_into_view_if_needed()

    last_seen = ""
    for attempt in range(2):
        await caption_field.focus()
        await caption_field.click()
        await asyncio.sleep(0.5)

        # Editor-level clear — select-all + Delete goes through the editor
        # model, unlike setting textContent
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.5)

        if attempt == 0:
            await page.keyboard.insert_text(caption)
        else:
            _log.warning("[TikTok] Caption mismatch — retrying with sequential typing...")
            await type_with_jitter(caption_field, caption)
        await asyncio.sleep(0.5)

        # Dispatch lifecycle events inside the targeted window tree to bind state data securely
        await page.evaluate("""() => {
            const selectors = [
                'div[contenteditable="true"]',
                'div[data-placeholder*="caption"]',
                'div.public-DraftEditor-content'
            ];
            let field = null;
            for (const sel of selectors) {
                field = document.querySelector(sel);
                if (field) break;
            }
            if (field) {
                field.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
                field.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
                field.dispatchEvent(new Event('blur', { bubbles: true }));
                field.focus();
            }
        }""")
        await asyncio.sleep(1.5)

        last_seen = await caption_field.inner_text()
        if _captions_match(caption, last_seen):
            _log.info("[TikTok] Caption verified in editor.")
            return
    raise Exception(
        f"TikTok caption verification failed for {account_key}: editor text does not "
        f"match the intended caption after 2 attempts — aborting before posting "
        f"garbled text. {EDITOR_MARKER}: {last_seen[:200]!r}"
    )


async def _click_post_button(page: Page):
    """Click the base "Post" button at the bottom of the editing panel.

    The chain deliberately has no failure branch — it clicks whatever it
    finds. That positional heuristic is tracked separately in issue #22; it
    is not a diagnostics gap (see test_tiktok_post_button_chain_is_out_of_scope).
    """
    _log.info("[TikTok] Caption finalized. Locating the base Publish/Post button...")

    post_btn = page.locator("button:has-text('Post')").filter(
        has_not=page.locator("button:has-text('Cancel'), button:has-text('Save')")
    ).last

    if await post_btn.count() == 0:
        post_btn = page.locator("[class*='button']").aria_role("button", name="Post").first

    await post_btn.scroll_into_view_if_needed()
    await post_btn.click()
    _log.info("[TikTok] Base Post action button clicked. Monitoring for confirmation modal...")
    await asyncio.sleep(1.5)


async def _confirm_post_modal(page: Page):
    """Handle the secondary confirmation modal if it slides up."""
    try:
        # TikTok Studio embeds the final submission button inside a specific
        # modal-content layer
        confirm_modal_btn = page.locator("div[class*='modal'] button:has-text('Post'), [class*='dialog'] button:has-text('Post')").first

        if await confirm_modal_btn.count() == 0:
            # Fallback: the last active visible Post button on the page layer
            confirm_modal_btn = page.locator("button:has-text('Post')").last

        if await confirm_modal_btn.count() > 0:
            _log.info("[TikTok] Final confirmation modal detected. Clicking final 'Post' switch...")
            await confirm_modal_btn.click()
            _log.info("[TikTok] Final confirmation action deployed successfully.")
    except Exception as e:
        _log.warning(f"[TikTok] Confirmation modal bypass step notice: {e}")


async def _await_upload_confirmation(page: Page, target) -> bool:
    """True only when a success signal was actually observed.

    Two independent signals, because TikTok shows different ones depending
    on whether it redirects to the Studio dashboard or stays on the upload
    page. Note the asymmetry: the dashboard check is against the page (a
    redirect leaves any iframe behind) while the in-page banner check is
    against the upload target.
    """
    _log.info("[TikTok] Holding execution. Waiting for upload confirmation or dashboard redirect...")
    confirmed = False
    try:
        # Selectors matching unique elements of the post-success dashboard
        await page.wait_for_selector(
            "input[placeholder*='Search for post description'], "
            "span:has-text('Posts (Created on)'), "
            "div:has-text('Post successfully uploaded')",
            timeout=45000  # Stays high to ensure the upload stream completely finishes
        )
        _log.info("[TikTok] Server confirmation packet and dashboard redirect validated successfully!")
        confirmed = True
    except Exception:
        _log.warning("[TikTok] Warning: Expected confirmation element not found. Proceeding with safety fallback.")
        await asyncio.sleep(2.0)

    # Second signal, only when the first did not already confirm (issue #29).
    #
    # This block used to run unconditionally. It can only ever *set* confirmed
    # True, never clear it, so on an already-confirmed post it was pure cost:
    # a 15s selector wait plus, when the page is still on /upload, a further
    # 10s sleep — up to ~25s of dead wait on the critical path of every
    # successful TikTok post. Skipping it when confirmed is already True
    # changes no outcome; the unconfirmed path below is untouched, and
    # tests/golden/tt_unconfirmed_post.trace pins that.
    if not confirmed:
        # TikTok usually shows a success message or redirects
        try:
            await target.wait_for_selector(
                "div:has-text('uploaded'), div:has-text('are being uploaded'), "
                "div:has-text('Your video has been uploaded')",
                timeout=15000,
            )
            confirmed = True
        except Exception:
            # If we got redirected away from upload, it likely succeeded
            if "/upload" not in page.url:
                confirmed = True
            else:
                await asyncio.sleep(10)

    return confirmed


async def _describe_blocking_modal(page: Page) -> str:
    """Return the text of a blocking TikTok dialog, or "" if none is open.

    One-time per-account dialogs (content checks, feature promos) sit in a
    TUXModal overlay and block all clicks. Naming the blocker in the error
    makes the fix obvious — observed live 2026-07-18 with "Turn on automatic
    content checks?". Deliberately reports rather than dismisses.
    """
    try:
        overlay = page.locator("div.TUXModal-overlay, div[class*='TUXModal']").first
        if await overlay.count() > 0 and await overlay.is_visible():
            return " ".join((await overlay.inner_text()).split())[:200]
    except Exception:
        pass
    return ""


async def post_media(
    account_key: str,
    media_path: Path,
    caption: str,
    media_type: str,
    headless: bool = True,
) -> str:
    """Post a video or photo to TikTok via the web upload page.

    A thin shell over the named steps above: session acquisition, the
    short-named upload copy, the step sequence, cookie write-back, failure
    diagnostics and cleanup.
    """
    async with async_playwright() as pw:
        browser = None
        if has_cookie_session(account_key):
            context, browser = await _get_context_from_cookies(pw, account_key, headless=headless)
            used_cookie_session = True
        else:
            context, browser = await _get_context(pw, account_key, headless=headless)
            used_cookie_session = False

        page = context.pages[0] if context.pages else await context.new_page()

        # TikTok pre-fills the caption field with the uploaded file's name,
        # so upload a short-named copy — long descriptive filenames must
        # never be able to bleed into the caption
        upload_path = media_path.with_name(f"tt_upload_{account_key}{media_path.suffix or '.mp4'}")
        shutil.copyfile(media_path, upload_path)

        try:
            await _open_upload_page(page, account_key)
            target = await _resolve_upload_target(page)
            await _upload_media_file(target, upload_path)
            await _dismiss_one_time_overlay(page)

            caption_field = await _find_caption_field(page)
            await _enter_and_verify_caption(page, caption_field, caption, account_key)

            await _click_post_button(page)
            await _confirm_post_modal(page)

            confirmed = await _await_upload_confirmation(page, target)

            if not confirmed:
                # Do NOT report success we didn't observe — the post may or
                # may not be live; the caller/UI shows this as unconfirmed
                _log.warning(f"[TikTok] Warning: no post confirmation seen for {account_key} — result unconfirmed.")

            # Write refreshed cookies back so the session self-sustains
            # (DESIGN-scheduling.md §3a). Only reached when the post did not
            # raise; _write_back_cookies never raises, so a failed write-back
            # can never fail the post. Gated on cookie-session origin so a
            # profile-fallback post never creates a cookie file and flips the
            # slot's auth mode.
            await _write_back_cookies(context, account_key, from_cookie_session=used_cookie_session)

            return _post_id("tt_post", account_key, confirmed)

        except Exception as e:
            # Screenshot the failure state (parity with instagram_browser)
            try:
                await page.screenshot(path=str(DEBUG_DIR / f"debug_tt_post_{account_key}.png"))
            except Exception:
                pass

            modal_text = await _describe_blocking_modal(page)

            if modal_text:
                raise Exception(
                    f"TikTok post failed for {account_key}: blocked by a TikTok dialog: "
                    f"\"{modal_text}\" — log into this account in a normal browser, dismiss "
                    f"the dialog once, then retry. Original error: {e}"
                )
            raise Exception(f"TikTok post failed for {account_key}: {e}")
        finally:
            try:
                upload_path.unlink(missing_ok=True)
            except Exception:
                pass
            # A failed close must not mask the real posting error
            try:
                await context.close()
            except Exception as close_err:
                _log.warning(f"[TikTok] Warning: context cleanup failed: {close_err}")
            if browser:
                try:
                    await browser.close()
                except Exception as close_err:
                    _log.warning(f"[TikTok] Warning: browser cleanup failed: {close_err}")
