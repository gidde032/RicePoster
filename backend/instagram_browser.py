"""
Instagram browser automation client.

Uses Playwright to automate posting through the Instagram web interface.
Requires saved browser sessions (login state) per account.
"""

import asyncio
import random
import re
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

# _post_id / _resolve_login_outcome are shared with tiktok_browser and must
# behave identically on both platforms (tech-debt audit BE-3, 2026-07-29).
from backend.config import (
    DEBUG_DIR,
    FEED_DWELL_MAX_S,
    FEED_DWELL_MIN_S,
    IG_SESSIONS_DIR,
)
from backend.browser_common import (
    EDITOR_MARKER,
    _captions_match,
    _post_id,
    _resolve_login_outcome,
    _selector_chain_error,
    url_matches_login_markers,
)
from backend.device_identity import (
    scale_factor_for_slot,
    screen_for_slot,
    viewport_for_slot,
)
from backend.jitter import jittered_duration, sleep_jittered, type_with_jitter
from backend.logging_setup import get_logger

_log = get_logger("instagram_browser")

# Directory to store persistent browser sessions (cookies/login state).
# Re-exported under the local name conftest's `tmp_sessions` fixture patches —
# see the layout section in config.py before changing this to a call-site
# reference.
SESSIONS_DIR = IG_SESSIONS_DIR
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Failure screenshots go here (gitignored) instead of the project root
DEBUG_DIR.mkdir(exist_ok=True)

# URL fragments that mean "this is Instagram's login wall, not the feed".
# Instagram redirects an expired session to /accounts/login/. The bare /login
# form is kept alongside it because it is what both the posting path and
# session_manager have always matched on, and dropping it would narrow
# expired-session detection. Owned here rather than in session_manager so
# platform knowledge lives with the platform (tech-debt audit BE-23).
LOGIN_REDIRECT_MARKERS = ("/accounts/login", "/login")


def _identity_kwargs(account_key: str, headless: bool) -> dict:
    """Device identity for the browser context, chosen by mode (D2, 2026-09-13).

    Headed: native identity. No viewport, screen, or pixel-ratio override. A
    real window on a real display reports true values by construction, and
    `--start-maximized` makes the window size deterministic per display.
    Three accounts then present as one real device with three logins, which
    Instagram supports, instead of three synthetic devices that differ only
    in screen size.

    Headless: synthetic per-slot identity (F3). No real window exists, so
    without these Playwright's 1280x720 default would be one shared
    fingerprint across every account again. The viewport alone is not a
    device: without `screen` and `device_scale_factor`, screen.height equals
    innerHeight and the pixel ratio stays 1 on a claimed Retina Mac. Measured
    2026-07-27 with tools/probe_fingerprint.py.

    Switching modes changes the device an account presents. Do it once, at a
    login, and never toggle it between runs.
    """
    if not headless:
        return dict(no_viewport=True)
    return dict(
        viewport=viewport_for_slot(account_key),
        screen=screen_for_slot(account_key),
        device_scale_factor=scale_factor_for_slot(account_key),
    )


async def _get_context(playwright, account_key: str, headless: bool = True) -> BrowserContext:
    """Get a persistent browser context for the given account. Preserves login state."""
    session_dir = SESSIONS_DIR / account_key
    session_dir.mkdir(exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=headless,
        channel="chrome",
        **_identity_kwargs(account_key, headless),
        # No user_agent override: we launch real Chrome (channel="chrome"), so
        # its own UA is current and — critically — consistent with the Sec-CH-UA
        # client hints and navigator.userAgentData that Playwright cannot
        # override. A hardcoded string desynchronises those surfaces, which no
        # real browser can do. If an override is ever needed, derive it from the
        # running browser's actual version at runtime; never hardcode one.
        args=[
            # LEGACY, NOT COVERAGE. This flag suppresses navigator.webdriver,
            # which was the frontier of bot detection around 2020. Meta's
            # current integrity systems key on client-hint consistency, device
            # fingerprint linkage across accounts, WebGL/canvas fingerprints
            # and behavioural clustering. Against that stack this does almost
            # nothing. It is kept because it is harmless and marginally useful
            # — do not read its presence as the fingerprint surface being
            # handled. (The redundant navigator.webdriver init scripts that
            # used to sit alongside it were removed: this flag already
            # suppresses the property, and an init script that redefines a
            # property descriptor is itself detectable.)
            "--disable-blink-features=AutomationControlled",
            # NOTE: the ANGLE + SwiftShader GL args were removed here
            # (2026-07-26). They forced *software* WebGL, making
            # WEBGL_debug_renderer_info report SwiftShader — a well-known bot
            # fingerprint. A real Mac reports an Apple GPU via ANGLE Metal.
            # They date to the initial harness commit, were never a response to
            # an observed crash, and their own comments contradicted each other
            # (one claimed "stable hardware graphics layer" next to a flag that
            # forces software). tiktok_browser does the opposite — it forces
            # hardware acceleration — and handles the same video files fine.
            # If a rendering problem reappears, re-solve it without advertising
            # a software renderer.
            "--disable-features=IsolateOrigins,site-per-process",
            # Headed mode only has an effect: the window fills the display, so
            # the native identity above is the same size on every run (D2).
            "--start-maximized",
        ],
    )
    return context


async def login(account_key: str):
    """
    Open a visible browser window for manual login.
    Call this once per account to save the session.
    """
    async with async_playwright() as pw:
        session_dir = SESSIONS_DIR / account_key
        had_profile_before = session_dir.exists() and any(session_dir.iterdir())
        context = await _get_context(pw, account_key, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.instagram.com/accounts/login/")
        print(f"\n[Instagram] Log in to account '{account_key}' in the browser window.")
        print("Once logged in and you see your feed, press Enter here to save the session")
        print("(or type 'abort' + Enter to discard if the login didn't work)...")
        answer = await asyncio.get_event_loop().run_in_executor(None, input)
        await context.close()
        if _resolve_login_outcome(session_dir, answer, had_profile_before):
            print(f"[Instagram] Session saved for '{account_key}'.")
        else:
            print(f"[Instagram] Login aborted — discarded partial session for '{account_key}'.")


async def _dismiss_popups(page: Page):
    """Dismiss notification and cookie popups."""
    for text in ["Not Now", "Not now", "Decline optional cookies", "Allow essential and optional cookies"]:
        try:
            btn = await page.wait_for_selector(f"button:has-text('{text}')", timeout=2000)
            await btn.click()
            await sleep_jittered(0.5)
        except Exception:
            pass


# The sidebar Create control: the anchor that wraps the "New post" icon, or
# the icon's own parent when the layout renders a button instead of a link.
CREATE_BUTTON_SELECTORS = (
    'a:has(svg[aria-label="New post"])',
    'div[role="button"]:has(svg[aria-label="New post"])',
)

# The "Post" item in Create's dropdown. Exact text: the same menu holds "Live
# video", and the sidebar holds "New post" and "Create".
POST_MENU_ITEM_RE = re.compile(r"^\s*Post\s*$")


async def _find_create_button(page: Page):
    """Resolve the Create control, or None when no selector matches."""
    attempts: list[tuple[str, int | None]] = []
    for selector in CREATE_BUTTON_SELECTORS:
        try:
            loc = page.locator(selector)
            count = await loc.count()
            attempts.append((selector, count))
            if count > 0:
                return loc.first
        except Exception:
            attempts.append((selector, None))
    _log.warning(_selector_chain_error("Create button", attempts))
    return None


async def _open_create_post(page: Page):
    """Click Create in the sidebar, then Post in its dropdown.

    Native Playwright actions only (D4, 2026-09-13). The earlier version
    clicked through `page.evaluate` and JavaScript `.click()`, which fires
    events with `isTrusted: false`; a page can log that. Playwright's own
    hover and click go through the browser's input pipeline, the same path a
    mouse takes, and arrive trusted. The narrow-layout branch is unchanged.
    """

    # Step 1: hover, then click the Create control.
    create = await _find_create_button(page)

    if create is None:
        if url_matches_login_markers(page.url, LOGIN_REDIRECT_MARKERS):
            raise Exception(
                "Instagram session expired — page shows login screen. "
                "Re-login: python -m backend.session_manager login instagram <slot>"
            )
        await sleep_jittered(2)
        is_login = await page.evaluate("""() => {
            if (document.querySelector('input[type="password"]'))
                return true;
            if (document.querySelector('input[name="username"]'))
                return true;
            return false;
        }""")
        if is_login or url_matches_login_markers(page.url, LOGIN_REDIRECT_MARKERS):
            raise Exception(
                "Instagram session expired — page shows login screen. "
                "Re-login: python -m backend.session_manager login instagram <slot>"
            )
        raise Exception("Could not find Create button SVG")

    await create.hover()
    await sleep_jittered(0.5)
    await create.click()
    await sleep_jittered(2)

    # === INSERTED FIX: CHECK IF MODAL OPENED DIRECTLY ===
    # If the layout is narrow, clicking 'Create' opens the upload dialog instantly.
    # We check if the 'Select from computer' button is already visible.
    is_modal_open = await page.locator("button:has-text('Select from computer')").is_visible()
    if is_modal_open:
        _log.info("[Instagram] Narrow layout detected: Upload modal opened directly. Skipping Step 2.")
        return 'opened_directly'
    # ===================================================

    # Step 2: hover, then click "Post" in the dropdown. Desktop layout only.
    post_item = page.locator('a[href="#"]', has_text=POST_MENU_ITEM_RE)
    count = await post_item.count()
    if count == 0:
        raise Exception(_selector_chain_error(
            "Post in the Create dropdown", [('a[href="#"] text=Post', count)]
        ))

    await post_item.first.hover()
    await sleep_jittered(0.5)
    await post_item.first.click()
    await sleep_jittered(3)



# ---------------------------------------------------------------------------
# The posting flow, in named steps
#
# These helpers are a decomposition of one 185-line function (tech-debt audit
# BE-4, issue #28) and nothing more. The sequence of Playwright calls, the
# selector chains, the sleep floors and the ordering are unchanged;
# tests/golden/ig_*.trace holds the recorded transcript that proves it.
#
# CLAUDE.md § Intentional design protects the long sleeps, the generous
# timeouts and the fallback selector chains: they are features, not bugs.
# Do not "tidy" them here.
# ---------------------------------------------------------------------------


# --- Feed dwell (D5, 2026-09-13) -------------------------------------------
#
# Scroll geometry and pacing for the read-the-feed span before the composer
# opens. Steps are planned up front as a fixed list, not run against a
# wall clock: the trace harness replaces sleep with a recorder, and a clock
# loop would never end there.

FEED_SCROLL_STEP_PX = (250, 900)       # one wheel notch burst, downward
FEED_SCROLL_BACK_PX = (100, 400)       # an occasional glance back up
FEED_SCROLL_BACK_CHANCE = 0.15
FEED_SCROLL_PAUSE_BASE_S = 1.2         # floor between scrolls
FEED_SCROLL_PAUSE_SPREAD_S = 1.6       # so a pause is 1.2-2.8 s


def feed_dwell_seconds() -> float:
    """Draw the total dwell for one run. 0 when disabled (both bounds 0).
    A min above max is clamped, not raised: a bad env value must not take
    down a posting run."""
    lo = max(0.0, FEED_DWELL_MIN_S)
    hi = max(0.0, FEED_DWELL_MAX_S)
    if hi <= 0:
        return 0.0
    lo = min(lo, hi)
    return jittered_duration(lo, spread=hi - lo)


def feed_dwell_plan(total_s: float) -> list[tuple[int, float]]:
    """Scroll steps as (delta_y, pause_s) whose pauses sum to at least
    `total_s`. Empty when `total_s` is 0 or less."""
    plan: list[tuple[int, float]] = []
    elapsed = 0.0
    while total_s > 0 and elapsed < total_s:
        delta = random.randint(*FEED_SCROLL_STEP_PX)
        if plan and random.random() < FEED_SCROLL_BACK_CHANCE:
            delta = -random.randint(*FEED_SCROLL_BACK_PX)
        pause = jittered_duration(FEED_SCROLL_PAUSE_BASE_S, FEED_SCROLL_PAUSE_SPREAD_S)
        plan.append((delta, pause))
        elapsed += pause
    return plan


async def _browse_feed(page: Page):
    """Read the feed for a while before opening the composer.

    A session that loads, posts, and leaves is a pure-publisher pattern. A
    person scrolls first. Wheel events through Playwright are trusted input.
    No-op when FEED_DWELL_MAX_S is 0.
    """
    total = feed_dwell_seconds()
    if total <= 0:
        return
    plan = feed_dwell_plan(total)
    _log.info(f"[Instagram] Browsing the feed for ~{int(total)}s ({len(plan)} scrolls) before posting...")
    for delta, pause in plan:
        await page.mouse.wheel(0, delta)
        # The pause is already jittered above its floor; spread 0 keeps it.
        await sleep_jittered(pause, 0.0)


async def _open_instagram(page: Page, account_key: str):
    """Load the feed and fail fast if the session is dead.

    Expired sessions redirect to the login page — surface that clearly
    instead of failing later on a missing Create button.
    """
    await page.goto("https://www.instagram.com/", wait_until="networkidle", timeout=60000)
    await sleep_jittered(2)

    if url_matches_login_markers(page.url, LOGIN_REDIRECT_MARKERS):
        raise Exception(
            f"Instagram session expired for {account_key}. Run login again: "
            f"python -m backend.session_manager login instagram {account_key}"
        )


async def _upload_media_file(page: Page, media_path: Path):
    """Inject the file straight into the dialog's file input.

    Bypassing the click coordinates entirely is what allows Instagram and
    TikTok to process video files concurrently.
    """
    _log.info("[Instagram] Resolving file transfer stream nodes...")

    # An attached locator catches the element regardless of slow render loops
    file_input = page.locator("div[role='dialog'] input[type='file']")

    # Wait up to 20 seconds for the upload window DOM fragments to settle
    await file_input.wait_for(state="attached", timeout=20000)

    await file_input.set_input_files(str(media_path))
    _log.info("[Instagram] Video stream successfully transferred to creator panel!")
    await sleep_jittered(2)

    # Upload is complete when the "Next" button appears. The handle is not
    # needed — this call is the wait, and the Next clicks happen later in
    # _advance_past_edit_screens.
    await page.wait_for_selector(
        "div[role='button']:has-text('Next'), button:has-text('Next')",
        timeout=120000,
    )
    await sleep_jittered(1)


async def _dismiss_aspect_ratio_warning(page: Page):
    """Accept Instagram's unsupported-ratio warning when it appears. A no-op
    when it does not, which is the common case."""
    try:
        ok_btn = await page.wait_for_selector("button:has-text('OK')", timeout=2000)
        await ok_btn.click()
        await sleep_jittered(1)
    except Exception:
        pass


async def _select_original_crop(page: Page):
    """Force the 9:16 "Original" crop.

    Instagram only, and that is final: TikTok's web uploader has no crop
    control to drive (CLAUDE.md § Intentional design). Best-effort — a
    failure here proceeds with Instagram's default rather than aborting.
    """
    try:
        _log.info("Adjusting video aspect ratio to 9:16...")
        # 1. Locate the crop/resize toggle button in the bottom-left corner of
        # the preview. Instagram uses an SVG icon inside a role="button" or a
        # native <button>.
        crop_toggle = page.locator("div[role='dialog'] button").filter(
            has=page.locator("svg[aria-label='Select crop']")
        ).first

        if await crop_toggle.count() == 0:
            # Fallback selector targeting the standard layout position
            crop_toggle = page.locator("button:has(svg)").locator("nth=0")

        await crop_toggle.scroll_into_view_if_needed()
        await crop_toggle.click()
        await sleep_jittered(0.5)

        # 2. Select the "Original" aspect ratio option from the popup list.
        # This keeps the video in its native 9:16 format instead of a 1:1 box.
        original_ratio_btn = page.locator("button:has-text('Original')")
        if await original_ratio_btn.count() > 0:
            await original_ratio_btn.click()
            _log.info("Aspect ratio successfully set to Original (9:16).")
        else:
            # Fallback: Instagram sometimes uses SVG checkmarks next to rows,
            # so click by text match instead.
            await page.locator("span:has-text('Original'), div:has-text('Original')").last.click()

        await sleep_jittered(1.0)
    except Exception as e:
        _log.warning(f"Warning: Could not adjust aspect ratio: {e}. Proceeding with default.")


async def _advance_past_edit_screens(page: Page):
    """Click "Next" through the crop and filter screens. Exhausting the
    selector is how the loop detects it has reached the final screen, so the
    break is the success path, not an error path."""
    for _ in range(3):
        try:
            # Instagram uses both <button> and <div role="button"> for Next
            next_btn = await page.wait_for_selector(
                "div[role='button']:has-text('Next'), button:has-text('Next')",
                timeout=5000,
            )
            await next_btn.click()
            await sleep_jittered(2)
        except Exception:
            break


async def _find_caption_field(page: Page):
    """Resolve the caption box, naming the whole chain if nothing matches."""
    caption_attempts: list[tuple[str, int | None]] = []
    for selector in [
        "div[aria-label='Write a caption...']",
        "div[aria-label='Write a caption…']",
        "div[role='textbox']",
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


async def _enter_caption(page: Page, caption_field, caption: str, account_key: str):
    """Type the caption, make Instagram's state model acknowledge it, and
    verify it before the post can be shared.

    Instagram published unverified until Batch 6, unlike TikTok, which has
    read its editor back since the caption-splice fix. That asymmetry was the
    silent-failure hole this closes: Instagram's caption box is a Draft.js
    contenteditable with a '#hashtag'/'@mention' autocomplete dropdown that
    can swallow or rewrite keystrokes, and nothing downstream would notice —
    a mangled caption simply went live.

    Two attempts, then abort. Aborting is the safe outcome: a failed post is
    recoverable, a wrong caption on a live account is not.
    """
    await caption_field.scroll_into_view_if_needed()

    last_seen = ""
    for attempt in range(2):
        # 1. Trigger deep native browser focus
        await caption_field.focus()
        await caption_field.click()
        await sleep_jittered(0.5)

        if attempt:
            # The field holds the failed attempt's text; clear it through the
            # editor model before retyping, or the retry appends to the mess.
            # Only on retry, so the ordinary post is unchanged.
            _log.warning("[Instagram] Caption mismatch — clearing and retyping...")
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Delete")
            await sleep_jittered(0.5)

        # 2. Type sequentially so React captures every keystroke. Cadence is
        # jittered per word-run rather than a fixed 60 ms per key (issue #2);
        # the floor is unchanged, so this is never faster than it used to be.
        await type_with_jitter(caption_field, caption)
        await sleep_jittered(0.5)

        # 3. Commit the editor state the way a person does: leave the field
        # and come back. Both are native focus changes (D4, 2026-09-13). This
        # replaces a page.evaluate that dispatched synthetic input, change,
        # and blur events, all with isTrusted false. Typed keystrokes already
        # reach Draft.js as trusted input events, so the editor model holds
        # the text; the blur/focus pair is what makes React settle it.
        await caption_field.blur()
        await sleep_jittered(0.5)
        await caption_field.focus()

        # Give the framework a moment to process the state changes before Share
        await sleep_jittered(1.5)

        # 4. Read the field back. Whitespace is normalised away, so Draft.js
        # re-rendering block boundaries is not treated as a mismatch.
        last_seen = await caption_field.inner_text()
        if _captions_match(caption, last_seen):
            _log.info("[Instagram] Caption verified in editor.")
            return

    raise Exception(
        f"Instagram caption verification failed for {account_key}: editor text does not "
        f"match the intended caption after 2 attempts — aborting before posting "
        f"garbled text. {EDITOR_MARKER}: {last_seen[:200]!r}"
    )


async def _share_post(page: Page):
    """Click "Share" — specifically the one in the "Create new post" dialog
    header, NOT the share buttons on feed posts behind the dialog."""
    share_btn = page.locator("div[role='dialog']").locator("div[role='button']", has_text="Share")

    if await share_btn.count() == 0:
        share_btn = page.locator("div[role='button']:has-text('Share')").last

    await share_btn.scroll_into_view_if_needed()
    await share_btn.click()


async def _await_post_confirmation(page: Page, account_key: str) -> bool:
    """True only when the success element was actually observed.

    Do NOT report success we didn't observe — the post may or may not be
    live; the caller/UI shows an unconfirmed result differently.
    """
    try:
        await page.wait_for_selector(
            "img[alt='Animated checkmark'], span:has-text('shared'), span:has-text('Post shared'), span:has-text('Reel shared')",
            timeout=180000,  # videos can take a while to process
        )
        return True
    except Exception:
        _log.warning(f"[Instagram] Warning: no post confirmation seen for {account_key} — result unconfirmed.")
        await sleep_jittered(10)
        return False


async def post_media(
    account_key: str,
    media_path: Path,
    caption: str,
    media_type: str,
    headless: bool = True,
) -> str:
    """Post a photo or reel to Instagram via browser automation.

    A thin shell over the named steps above: browser lifecycle, the step
    sequence, failure diagnostics and cleanup. Everything the flow does to
    the page lives in one of the helpers.
    """
    async with async_playwright() as pw:
        context = await _get_context(pw, account_key, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await _open_instagram(page, account_key)
            await _dismiss_popups(page)
            await _browse_feed(page)
            await _open_create_post(page)
            await _upload_media_file(page, media_path)
            await _dismiss_aspect_ratio_warning(page)
            await _select_original_crop(page)
            await _advance_past_edit_screens(page)

            caption_field = await _find_caption_field(page)
            await _enter_caption(page, caption_field, caption, account_key)

            await _share_post(page)
            confirmed = await _await_post_confirmation(page, account_key)

            return _post_id("ig_post", account_key, confirmed)

        except Exception as e:
            # Take a debug screenshot on failure
            try:
                await page.screenshot(path=str(DEBUG_DIR / f"debug_ig_post_{account_key}.png"))
            except Exception:
                pass
            raise Exception(f"Instagram post failed for {account_key}: {e}")
        finally:
            try:
                await context.close()
            except Exception as close_err:
                # A failed close must not mask the real posting error
                _log.warning(f"[Instagram] Warning: browser cleanup failed: {close_err}")
