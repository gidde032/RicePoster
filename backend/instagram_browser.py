"""
Instagram browser automation client.

Uses Playwright to automate posting through the Instagram web interface.
Requires saved browser sessions (login state) per account.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

# _post_id / _resolve_login_outcome are shared with tiktok_browser and must
# behave identically on both platforms (tech-debt audit BE-3, 2026-07-29).
from backend.config import DEBUG_DIR, IG_SESSIONS_DIR
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
from backend.jitter import sleep_jittered, type_with_jitter
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


async def _get_context(playwright, account_key: str, headless: bool = True) -> BrowserContext:
    """Get a persistent browser context for the given account. Preserves login state."""
    session_dir = SESSIONS_DIR / account_key
    session_dir.mkdir(exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=headless,
        channel="chrome",
        # Per-slot device identity (F3): three accounts posting back-to-back
        # from one byte-identical synthetic device is the account-farm pattern
        # Instagram's integrity systems look for. Stable per slot by
        # construction — see backend/device_identity.py.
        viewport=viewport_for_slot(account_key),
        # The viewport alone is not a device. Without these two, window.screen
        # reports the viewport's own size — so screen.height == innerHeight and
        # availHeight == height, neither of which real hardware can produce —
        # and devicePixelRatio stays 1 while the dimensions claim a Retina Mac.
        # Both were measured on 2026-07-27 with tools/probe_fingerprint.py; the
        # same tool verifies the fix without any live traffic.
        screen=screen_for_slot(account_key),
        device_scale_factor=scale_factor_for_slot(account_key),
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


async def _open_create_post(page: Page):
    """Click Create in sidebar, handling both desktop dropdowns and mobile modal layouts."""

    # Step 1: Click the Create button (Your existing working logic)
    clicked_create = await page.evaluate("""() => {
        const svg = document.querySelector('svg[aria-label="New post"]');
        if (!svg) return 'no_svg';
        
        let el = svg;
        while (el && el.tagName !== 'A') {
            el = el.parentElement;
        }
        if (el && el.tagName === 'A') {
            el.click();
            return 'clicked_a';
        }
        
        svg.parentElement.click();
        return 'clicked_parent';
    }""")

    if clicked_create == 'no_svg':
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

    await sleep_jittered(2)

    # === INSERTED FIX: CHECK IF MODAL OPENED DIRECTLY ===
    # If the layout is narrow, clicking 'Create' opens the upload dialog instantly.
    # We check if the 'Select from computer' button is already visible.
    is_modal_open = await page.locator("button:has-text('Select from computer')").is_visible()
    if is_modal_open:
        _log.info("[Instagram] Narrow layout detected: Upload modal opened directly. Skipping Step 2.")
        return 'opened_directly'
    # ===================================================

    # Step 2: Click "Post" from the dropdown (Only runs if desktop dropdown appears)
    clicked = await page.evaluate("""() => {
        const links = document.querySelectorAll('a[href="#"]');
        for (const link of links) {
            const rect = link.getBoundingClientRect();
            const text = link.textContent?.trim() || '';
            if (rect.x < 150 && text.includes('Post') && !text.includes('New') && !text.includes('Create')) {
                link.click();
                return 'found: ' + text;
            }
        }
        const allLinks = [];
        document.querySelectorAll('a[href="#"]').forEach(link => {
            const rect = link.getBoundingClientRect();
            allLinks.push(rect.x + ',' + rect.y + ': ' + link.textContent?.trim()?.substring(0, 30));
        });
        return 'not_found. Links: ' + allLinks.join(' | ');
    }""")

    if clicked.startswith('not_found'):
        raise Exception(f"Could not find Post in dropdown. Debug: {clicked}")

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

        # 3. CRITICAL: force Instagram's state model to acknowledge the text by
        # blasting the element and its parents with native lifecycle events.
        await page.evaluate("""() => {
            const selectors = [
                'div[aria-label="Write a caption..."]',
                'div[aria-label="Write a caption…"]',
                'div[role="textbox"]'
            ];
            let field = null;
            for (const sel of selectors) {
                field = document.querySelector(sel);
                if (field) break;
            }

            if (field) {
                // Fire text input events
                field.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
                field.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));

                // Force React to process text updates by losing focus (blur) then refocusing
                field.dispatchEvent(new Event('blur', { bubbles: true }));
                field.focus();
            }
        }""")

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
