"""
Session manager — log into Instagram and TikTok accounts and save browser sessions.

Usage (A is an example, not the fixed roster — slots come from ACCOUNT_SLOTS;
run the module with no arguments to print the configured list):
    python -m backend.session_manager login instagram A
    python -m backend.session_manager login tiktok A
    python -m backend.session_manager login all
    python -m backend.session_manager status
    python -m backend.session_manager check
    python -m backend.session_manager check --slot A
"""

import asyncio
import json
import random
import sys
import time
from playwright.async_api import async_playwright
from backend import instagram_browser, tiktok_browser
from backend.browser_common import url_matches_login_markers
from backend.config import (
    HEALTH_CACHE_FILE,
    SLOT_IDS,
    SESSION_CHECK_TTL_S,
    PREFLIGHT_CHECK_PLATFORMS,
)
from backend.jitter import sleep_jittered
from backend.logging_setup import get_logger

_log = get_logger("session_manager")

# The slot list comes from ACCOUNT_SLOTS in credentials.env (default A,B,C)
SLOTS = SLOT_IDS


# --- Slot guidance (#9) ------------------------------------------------------
#
# Every example and validation message below is generated from SLOTS. They used
# to hardcode "A, B, C" while the roster has been configurable since
# ACCOUNT_SLOTS landed, so a maintainer running ACCOUNT_SLOTS=A,B,C,D was told
# by the tool itself that D was invalid.
#
# Read SLOTS at call time rather than building the strings at import, so tests
# (and a future reload) see a redirected roster.


def _example_slot() -> str:
    """A concrete slot id to show in a usage line."""
    return SLOTS[0] if SLOTS else "A"


def _slot_choices() -> str:
    """The roster as prose: "A, B, or C". Used in validation messages."""
    if not SLOTS:
        return "a configured slot"
    if len(SLOTS) == 1:
        return SLOTS[0]
    return f"{', '.join(SLOTS[:-1])}, or {SLOTS[-1]}"


def _resolve_slot(raw: str) -> str | None:
    """Map user input to a configured slot id, or None if it matches none.

    Case-insensitive, returning the *configured* spelling. Replaces a bare
    `.upper()`, which assumed the roster was uppercase: ACCOUNT_SLOTS accepts
    letters, digits, '_' and '-' in any case (config._SLOT_ID_RE), so
    `--slot a1` against ACCOUNT_SLOTS=a1,b2 was uppercased to "A1" and then
    rejected as invalid — the tool refusing a slot it had itself configured.

    An exact match wins before the case-insensitive sweep, and an ambiguous
    sweep resolves to nothing rather than to whichever slot came first. Both
    matter because `parse_slot_ids` dedupes case-*sensitively*, so
    ACCOUNT_SLOTS=a,A yields two distinct slots: first-match-wins would have
    sent `--slot A` to slot "a", and device identity is assigned by slot
    *index* (device_identity.py), so that is a different viewport and a
    different session directory on a case-sensitive volume. Silently picking
    one of two live accounts is not an acceptable way to resolve ambiguity.
    """
    needle = raw.strip()
    if needle in SLOTS:
        return needle
    matches = [slot for slot in SLOTS if slot.lower() == needle.lower()]
    return matches[0] if len(matches) == 1 else None

# Generous per-slot timeout for the health check — the maintainer's machine is
# under load and cold Playwright startups are slow (DESIGN-scheduling.md §3b).
#
# Raised 30 -> 50 on 2026-07-26. The F5 dwell/scroll added up to 4.5s inside
# this budget and silently cut the launch headroom from 7s to 2.5s, which is
# not enough for a cold Chrome start on a loaded machine. See
# _inner_worst_case_s() below: the margin is now an asserted invariant, not a
# number someone has to remember to re-derive.
SESSION_CHECK_TIMEOUT_S = 50

# Inner navigation timeout, kept below the outer per-slot wait_for above so a
# slow page load surfaces as a graceful goto timeout inside _run_session_check
# — reaching the finally that closes the browser — rather than the outer
# wait_for cancelling mid-launch and stranding the Chromium process.
SESSION_CHECK_GOTO_TIMEOUT_S = 20

# Dwell and scroll applied inside the check so it does not read as an
# instantaneous load-and-leave (F5). Named rather than inlined so the timeout
# budget below can be computed from them instead of hardcoded.
SESSION_CHECK_DWELL_BASE_S = 3.0
SESSION_CHECK_DWELL_SPREAD_S = 2.5
SESSION_CHECK_SCROLL_PAUSE_MIN_S = 0.8
SESSION_CHECK_SCROLL_PAUSE_MAX_S = 2.0

# Headroom the outer timeout must leave for everything that is NOT the
# navigation and dwell: cold Playwright startup, Chrome launch, context and
# page creation, selector reads, and the close in the finally. Cold starts on
# a loaded machine are the reason this is large.
SESSION_CHECK_LAUNCH_MARGIN_S = 7.0


def _inner_worst_case_s() -> float:
    """Longest _run_session_check can take before the outer wait_for fires,
    excluding browser launch/teardown.

    Exists so the budget invariant is machine-checkable. Anyone adding
    another wait inside the check must add it here too, and
    test_session_check_budget_leaves_launch_headroom will fail until the
    outer timeout is raised to match — which is exactly the failure that was
    missed when the dwell was first added.
    """
    return (
        SESSION_CHECK_GOTO_TIMEOUT_S
        + SESSION_CHECK_DWELL_BASE_S
        + SESSION_CHECK_DWELL_SPREAD_S
        + SESSION_CHECK_SCROLL_PAUSE_MAX_S
    )

# Cache of successful health checks (F5). Lives under the already-gitignored
# sessions/ tree because it is session state. Module-level so tests can
# redirect it away from the real file.


def _load_health_cache() -> dict:
    """Read the cache. Any problem — missing file, truncated JSON, wrong
    shape — degrades to an empty cache, which just means a real check runs.
    A corrupt cache must never be able to block a posting run."""
    try:
        with open(HEALTH_CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cache_key(slot: str, platform: str) -> str:
    return f"{slot}|{platform}"


def get_cached_check(slot: str, platform: str, now: float | None = None) -> str | None:
    """Return a cached "live" status if one was recorded within the TTL,
    else None. A TTL of 0 disables the cache entirely."""
    if SESSION_CHECK_TTL_S <= 0:
        return None
    entry = _load_health_cache().get(_cache_key(slot, platform))
    if not isinstance(entry, dict):
        return None
    ts = entry.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    now = time.time() if now is None else now
    # A timestamp in the future means a clock change or a hand-edited file;
    # treat it as unusable rather than trusting it indefinitely.
    age = now - ts
    if age < 0 or age > SESSION_CHECK_TTL_S:
        return None
    return entry.get("status") if entry.get("status") == "live" else None


def record_check_result(slot: str, platform: str, status: str) -> None:
    """Persist a successful check. Only "live" is cached — a re-login must be
    picked up on the very next check, so expired/no_session/check_error are
    never cached and additionally evict any stale entry."""
    cache = _load_health_cache()
    key = _cache_key(slot, platform)
    if status == "live":
        cache[key] = {"status": "live", "ts": time.time()}
    else:
        cache.pop(key, None)
    try:
        HEALTH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(cache, f)
        tmp.replace(HEALTH_CACHE_FILE)
    except Exception:
        # Cache writes are best-effort; failing to record must never fail a
        # check or a posting run.
        pass


def session_exists(platform: str, slot: str) -> bool:
    """Check if a session directory has data or exported cookies exist."""
    if platform == "instagram":
        session_dir = instagram_browser.SESSIONS_DIR / slot
        return session_dir.exists() and any(session_dir.iterdir())
    else:
        # TikTok: check for cookie JSON first, then profile directory
        if tiktok_browser.has_cookie_session(slot):
            return True
        return tiktok_browser.has_profile_session(slot)


async def login_account(platform: str, slot: str):
    """Open a browser for manual login to one account."""
    if platform == "instagram":
        await instagram_browser.login(slot)
    elif platform == "tiktok":
        await tiktok_browser.login(slot)
    else:
        print(f"Unknown platform: {platform}")


async def login_all():
    """Walk through login for every slot × platform, with option to skip each."""
    for slot in SLOTS:
        # Instagram
        if session_exists("instagram", slot):
            print(f"Instagram {slot}: session already exists (skip)")
        else:
            answer = input(f"\nInstagram Account {slot} — login now? [Y/n/q] ").strip().lower()
            if answer == "q":
                print("Stopped.")
                return
            if answer != "n":
                await login_account("instagram", slot)

        # TikTok
        if session_exists("tiktok", slot):
            print(f"TikTok {slot}: session already exists (skip)")
        else:
            answer = input(f"\nTikTok Account {slot} — login now? [Y/n/q] ").strip().lower()
            if answer == "q":
                print("Stopped.")
                return
            if answer != "n":
                await login_account("tiktok", slot)

    print("\nDone. Run 'status' to verify:")
    show_status()


def clear_session(platform: str, slot: str):
    """Remove a saved session."""
    import shutil
    if platform == "instagram":
        session_dir = instagram_browser.SESSIONS_DIR / slot
    elif platform == "tiktok":
        session_dir = tiktok_browser.SESSIONS_DIR / slot
    else:
        print(f"Unknown platform '{platform}'. Use 'instagram' or 'tiktok'.")
        return
    if session_dir.exists():
        shutil.rmtree(session_dir)
        print(f"Cleared {platform} {slot} session.")
    else:
        print(f"No session to clear for {platform} {slot}.")


def show_status():
    """Print which accounts have saved sessions."""
    print("\nSession Status:")
    print("-" * 40)
    for slot in SLOTS:
        ig = "✓ logged in" if session_exists("instagram", slot) else "✗ not logged in"
        tt = "✓ logged in" if session_exists("tiktok", slot) else "✗ not logged in"
        print(f"  Account {slot}:")
        print(f"    Instagram: {ig}")
        print(f"    TikTok:    {tt}")
    print()


def _is_login_redirect(url: str) -> bool:
    """True when a post-navigation URL indicates a login wall / expired
    session (DESIGN-scheduling.md §3b). Pure and case-insensitive.

    The marker sets moved into the platform modules (tech-debt audit BE-23,
    issue #28) — session_manager should not be the place that knows what
    Instagram's or TikTok's login URLs look like. This is deliberately their
    *union* rather than a per-platform lookup: the health check applies one
    rule to both platforms, and the union of the two marker tuples is exactly
    the pair of literals this function used to hardcode, so the behaviour is
    unchanged rather than approximately unchanged.
    `test_login_markers_union_matches_the_original_literals` pins that.
    """
    markers = (
        instagram_browser.LOGIN_REDIRECT_MARKERS
        + tiktok_browser.LOGIN_REDIRECT_MARKERS
    )
    return url_matches_login_markers(url, markers)


def _tiktok_session_status(url: str, login_modal_present: bool) -> str:
    """Classify a TikTok health-check result from the post-navigation URL and
    whether a login modal is present in the DOM (DESIGN-scheduling.md §3b
    step 3). Either signal means the session is expired. Pure/testable."""
    if _is_login_redirect(url) or login_modal_present:
        return "expired"
    return "live"


# The probe itself is TikTok's business and lives in tiktok_browser (BE-23).
# Kept under the local name because the health check reads better with it and
# because `_run_session_check` names it at the call site.
_tiktok_login_modal_present = tiktok_browser.login_modal_present


async def _run_session_check(slot: str, platform: str) -> str:
    """Open a headless context using the same session-loading path as posting,
    navigate to the platform home, and report "live" or "expired". Never posts,
    logs out, or rewrites session state. Closes the context and browser before
    returning. May raise (timeout, navigation error) — check_session collapses
    that to "check_error"."""
    async with async_playwright() as pw:
        # Acquire the handles INSIDE the try so that if the outer wait_for
        # cancels during a slow launch, the finally still closes whatever was
        # opened — otherwise a cancelled IG launch strands a persistent context
        # holding the profile lock, and a cancelled TikTok launch leaks Chrome.
        context = None
        browser = None
        try:
            if platform == "instagram":
                context = await instagram_browser._get_context(pw, slot, headless=True)
                home_url = "https://www.instagram.com/"
            elif platform == "tiktok":
                context, browser = await tiktok_browser._get_context(pw, slot, headless=True)
                home_url = "https://www.tiktok.com/"
            else:
                raise ValueError(f"Unknown platform: {platform}")

            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(home_url, wait_until="domcontentloaded",
                            timeout=SESSION_CHECK_GOTO_TIMEOUT_S * 1000)
            # Give client-side redirects a moment to settle before reading URL.
            # The floor stays at the original 3s and only gains variance above
            # it (F4 rule: never shorten a wait). The check used to load the
            # home feed and vanish at a machine-exact interval with zero
            # interaction, which reads as a cleaner bot signature than actually
            # posting (F5).
            # Routed through sleep_jittered rather than an inline
            # random.uniform (review 2026-07-26, finding #9). Identical
            # timing — sleep_jittered(base, spread) is base + uniform(0,
            # spread) by construction — but the floor rule is now enforced by
            # jitter.py and guarded by a shared test rather than re-derived
            # here, where test_instagram_has_no_fixed_sleeps_left never
            # looked.
            await sleep_jittered(
                SESSION_CHECK_DWELL_BASE_S, SESSION_CHECK_DWELL_SPREAD_S
            )
            try:
                # A real session scrolls. Cheap, and it makes the visit look
                # like a visit. Never allowed to fail the check.
                await page.mouse.wheel(0, random.randint(300, 900))
                await sleep_jittered(
                    SESSION_CHECK_SCROLL_PAUSE_MIN_S,
                    SESSION_CHECK_SCROLL_PAUSE_MAX_S
                    - SESSION_CHECK_SCROLL_PAUSE_MIN_S,
                )
            except Exception:
                pass
            if platform == "tiktok":
                modal_present = await _tiktok_login_modal_present(page)
                return _tiktok_session_status(page.url, modal_present)
            return "expired" if _is_login_redirect(page.url) else "live"
        finally:
            # Close both handles; never let a failed close mask the check
            # result. Each close is independent (a persistent context has no
            # separate browser, so browser stays None there).
            for handle in (context, browser):
                if handle is not None:
                    try:
                        await handle.close()
                    except Exception:
                        pass


async def check_session(slot: str, platform: str) -> str:
    """Return the session health for one account without posting: "live",
    "expired", "no_session", or "check_error" (DESIGN-scheduling.md §3b).

    Never raises — a timeout, navigation error, or any unexpected exception
    collapses to "check_error", meaning "state unknown, attempt anyway": only
    a clean "expired"/"no_session" should skip a slot. Opens the same session
    state as posting, so it must never run concurrently with a posting run."""
    if not session_exists(platform, slot):
        record_check_result(slot, platform, "no_session")
        return "no_session"

    # Browser pre-flight can be turned off per platform (F5). Deliberately
    # placed AFTER the session_exists test, so disabling the check keeps the
    # free filesystem-level no_session filtering and gives up only the
    # browser load. "check_disabled" is not in the scheduler's skip set, so
    # the slot is attempted — the same "unknown, attempt anyway" semantics as
    # check_error, which is already how a failed check behaves.
    if platform not in PREFLIGHT_CHECK_PLATFORMS:
        return "check_disabled"

    # A session verified this morning does not need re-verifying before every
    # batch (F5). This is the change that removes most of the automated
    # Instagram traffic from this machine.
    cached = get_cached_check(slot, platform)
    if cached is not None:
        return cached

    try:
        status = await asyncio.wait_for(
            _run_session_check(slot, platform), timeout=SESSION_CHECK_TIMEOUT_S
        )
    except Exception as e:
        # check_error = unknown, not dead.
        #
        # Type *and* message (tech-debt audit BE-24). The type alone made a
        # navigation failure, a missing selector and a genuine timeout all
        # print the identical line, so "check error (Error)" told the
        # maintainer only that something went wrong — and this runs
        # unattended before a scheduled batch, so the console line is the
        # whole record.
        #
        # Still no cookie or credential values: the exception is a Playwright
        # or asyncio error carrying selectors, timeouts and platform URLs.
        # Session state lives in the persistent profile directory and never
        # reaches an exception string. This is not `notifier.py`, where the
        # URL itself is the secret.
        #
        # The message is appended only when non-empty — `asyncio.wait_for`
        # raises TimeoutError with an empty str(), which is the single most
        # likely failure here and would otherwise print a trailing ": ".
        detail = str(e).strip()
        suffix = f": {detail}" if detail else ""
        _log.warning(f"[check] {platform} {slot}: check error ({type(e).__name__}{suffix})")
        record_check_result(slot, platform, "check_error")
        return "check_error"
    record_check_result(slot, platform, status)
    return status


async def run_check(slot_filter: str | None = None):
    """Run the health check for each slot × platform and print per-slot status."""
    print("\n⚠️  Do NOT run this while a post is in progress — it opens the same")
    print("    sessions as posting and must never overlap a posting run.\n")
    print("Session Health Check:")
    print("-" * 40)
    slots = [slot_filter] if slot_filter else SLOTS
    for slot in slots:
        for platform in ("instagram", "tiktok"):
            status = await check_session(slot, platform)
            print(f"  Account {slot} {platform}: {status}")
    print()


def main():
    if len(sys.argv) < 2:
        eg = _example_slot()
        # Second example uses a different slot where one exists, so the usage
        # block shows the argument varying rather than looking like a literal.
        eg2 = SLOTS[1] if len(SLOTS) > 1 else eg
        print("Usage:")
        print(f"  python -m backend.session_manager login instagram {eg}")
        print(f"  python -m backend.session_manager login tiktok {eg2}")
        print("  python -m backend.session_manager login all")
        print(f"  python -m backend.session_manager clear tiktok {eg}    # remove a bad session")
        print("  python -m backend.session_manager clear tiktok all  # remove all TikTok sessions")
        print("  python -m backend.session_manager status")
        print("  python -m backend.session_manager check             # health-check all sessions")
        print(f"  python -m backend.session_manager check --slot {eg}    # health-check one slot")
        print(f"  (configured slots: {', '.join(SLOTS)})")
        print("  (⚠️  never run 'check' while a post is in progress)")
        sys.exit(1)

    command = sys.argv[1]

    if command == "status":
        show_status()

    elif command == "check":
        slot_filter = None
        if "--slot" in sys.argv:
            i = sys.argv.index("--slot")
            if i + 1 >= len(sys.argv):
                print(
                    "Specify a slot: python -m backend.session_manager check "
                    f"--slot {_example_slot()}"
                )
                sys.exit(1)
            raw = sys.argv[i + 1]
            slot_filter = _resolve_slot(raw)
            if slot_filter is None:
                print(f"Invalid slot '{raw}'. Use {_slot_choices()}.")
                sys.exit(1)
        asyncio.run(run_check(slot_filter))

    elif command == "clear":
        if len(sys.argv) < 4:
            print("Specify: python -m backend.session_manager clear <platform> <slot|all>")
            sys.exit(1)
        platform = sys.argv[2]
        if platform not in ("instagram", "tiktok"):
            print(f"Unknown platform '{platform}'. Use 'instagram' or 'tiktok'.")
            sys.exit(1)
        target = sys.argv[3]
        resolved = _resolve_slot(target)
        if target.strip().lower() == "all":
            for s in SLOTS:
                clear_session(platform, s)
        elif resolved is not None:
            clear_session(platform, resolved)
        else:
            print(f"Invalid slot '{target}'. Use {_slot_choices()}, or all.")

    elif command == "login":
        if len(sys.argv) < 3:
            print("Specify: 'all', or '<platform> <slot>'")
            sys.exit(1)

        target = sys.argv[2]

        if target == "all":
            asyncio.run(login_all())
        else:
            if len(sys.argv) < 4:
                print(
                    "Specify slot: python -m backend.session_manager login "
                    f"{target} {_example_slot()}"
                )
                sys.exit(1)
            platform = target
            raw_slot = sys.argv[3]
            slot = _resolve_slot(raw_slot)
            if slot is None:
                print(f"Invalid slot '{raw_slot}'. Use {_slot_choices()}.")
                sys.exit(1)
            asyncio.run(login_account(platform, slot))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
