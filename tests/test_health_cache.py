"""
Tests for the session health-check TTL cache (F5).

Source: RESEARCH-platform-detection.md Finding 5 (2026-07-25) —
the health check navigates to the platform home, reads a selector and
closes, with no interaction. The scheduler ran it per slot per platform
before every batch, so with 3 slots that was 3 extra Instagram home-page
loads per batch. A repeated load-and-leave is a cleaner bot signature than
actually posting.

The cache is the substantive part of the fix: it removes most of that
traffic. The dwell/scroll is covered by source-level assertion only, since
exercising it needs a real browser.
"""

import ast
import asyncio
import inspect
import time

import pytest

from backend import session_manager
from backend.session_manager import (
    get_cached_check,
    record_check_result,
)


@pytest.fixture(autouse=True)
def _ttl(monkeypatch):
    """Pin a known TTL; the module reads it at call time."""
    monkeypatch.setattr(session_manager, "SESSION_CHECK_TTL_S", 3600)


def test_live_result_is_cached_and_returned():
    record_check_result("A", "instagram", "live")
    assert get_cached_check("A", "instagram") == "live"


def test_cache_is_scoped_per_slot_and_platform():
    """A live Instagram session for slot A must not satisfy a check for
    slot B, or for A's TikTok."""
    record_check_result("A", "instagram", "live")
    assert get_cached_check("B", "instagram") is None
    assert get_cached_check("A", "tiktok") is None


def test_entry_expires_after_ttl():
    record_check_result("A", "instagram", "live")
    assert get_cached_check("A", "instagram", now=time.time() + 3599) == "live"
    assert get_cached_check("A", "instagram", now=time.time() + 3601) is None


@pytest.mark.parametrize("status", ["expired", "no_session", "check_error"])
def test_unsuccessful_results_are_never_cached(status):
    """The critical correctness property. If "expired" were cached, a slot
    would keep being skipped for hours after the maintainer re-logged in."""
    record_check_result("A", "instagram", status)
    assert get_cached_check("A", "instagram") is None


@pytest.mark.parametrize("status", ["expired", "no_session", "check_error"])
def test_unsuccessful_result_evicts_an_earlier_live_entry(status):
    """A session that has since died must not be shielded by this morning's
    cached "live"."""
    record_check_result("A", "instagram", "live")
    record_check_result("A", "instagram", status)
    assert get_cached_check("A", "instagram") is None


def test_ttl_of_zero_disables_the_cache(monkeypatch):
    """The off switch — needed to force real checks when debugging."""
    record_check_result("A", "instagram", "live")
    monkeypatch.setattr(session_manager, "SESSION_CHECK_TTL_S", 0)
    assert get_cached_check("A", "instagram") is None


def test_future_timestamp_is_not_trusted():
    """A clock change or hand-edited file must not produce an entry that
    never expires."""
    record_check_result("A", "instagram", "live")
    assert get_cached_check("A", "instagram", now=time.time() - 99999) is None


def test_corrupt_cache_file_degrades_to_a_real_check():
    """A corrupt cache must never be able to block a posting run."""
    session_manager.HEALTH_CACHE_FILE.write_text("{not json at all")
    assert get_cached_check("A", "instagram") is None
    # and must still be writable afterwards
    record_check_result("A", "instagram", "live")
    assert get_cached_check("A", "instagram") == "live"


def test_missing_cache_file_is_not_an_error():
    assert get_cached_check("nonexistent", "instagram") is None


def test_unwritable_cache_never_raises(monkeypatch, tmp_path):
    """Cache writes are best-effort — a write failure must not fail a check
    or a posting run. Made unwritable by putting the cache under a path whose
    parent is a regular file, so mkdir raises."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(
        session_manager, "HEALTH_CACHE_FILE", blocker / "nested" / "cache.json"
    )
    record_check_result("A", "instagram", "live")  # must not raise
    assert get_cached_check("A", "instagram") is None


# --- check_session integration -------------------------------------------


def test_check_session_skips_the_browser_on_a_cache_hit(monkeypatch):
    """The whole point: a cached "live" must not launch a browser. If this
    regresses, F5's traffic reduction silently disappears."""
    monkeypatch.setattr(session_manager, "session_exists", lambda p, s: True)
    calls = []

    async def _counted(slot, platform):
        calls.append((slot, platform))
        return "live"

    monkeypatch.setattr(session_manager, "_run_session_check", _counted)

    assert asyncio.run(session_manager.check_session("A", "instagram")) == "live"
    assert len(calls) == 1, "first call should perform a real check"

    assert asyncio.run(session_manager.check_session("A", "instagram")) == "live"
    assert len(calls) == 1, (
        "second call ran a real check instead of using the cache — F5's "
        "traffic reduction is not working"
    )


def test_no_session_short_circuits_without_caching_live(monkeypatch):
    monkeypatch.setattr(session_manager, "session_exists", lambda p, s: False)
    assert asyncio.run(
        session_manager.check_session("A", "instagram")) == "no_session"
    assert get_cached_check("A", "instagram") is None


# --- dwell / scroll: source-level only (needs a real browser) -------------


def _session_manager_source() -> str:
    """Read the *module* source, not _run_session_check's.

    The conftest tripwire monkeypatches _run_session_check with a stub, so
    inspect.getsource on the attribute returns the stub's body and these
    assertions would vacuously pass.
    """
    return inspect.getsource(session_manager)


def _check_node() -> ast.AsyncFunctionDef:
    """The AST node for _run_session_check, located structurally.

    Replaces a `src.split("async def _run_session_check")[1].split("async
    def")[0]` body extraction (review 2026-07-26, finding #1 — raised
    independently by all three reviewers). That slice silently returned the
    wrong region as soon as a nested `async def` appeared inside the check or
    the surrounding functions were reordered, and the count assertion below
    would then pass against text that is not the function's body.

    Parses the *module* source for the same reason _session_manager_source
    reads the module: the conftest tripwire monkeypatches
    _run_session_check, so inspecting the attribute yields the stub.
    """
    tree = ast.parse(_session_manager_source())
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_run_session_check"):
            return node
    raise AssertionError("_run_session_check not found in session_manager")


def _inner_wait_count(node: ast.AsyncFunctionDef) -> int:
    """Count awaited waits in the check, excluding any nested function.

    Counts both `asyncio.sleep` and `sleep_jittered`: finding #9 converted
    these waits to sleep_jittered, and a counter that knew only one spelling
    would silently report 0 and pass.
    """
    nested = {n for f in ast.walk(node)
              if f is not node and isinstance(
                  f, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda))
              for n in ast.walk(f)}
    count = 0
    for inner in ast.walk(node):
        if inner in nested or not isinstance(inner, ast.Call):
            continue
        fn = inner.func
        name = (fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name) else "")
        if name in ("sleep", "sleep_jittered"):
            count += 1
    return count


def test_check_performs_a_dwell_and_scroll():
    """F5: the check must not read as an instantaneous load-and-leave."""
    src = _session_manager_source()
    assert "mouse.wheel" in src, "health check no longer scrolls"
    assert "sleep_jittered(" in src, "health check dwell is no longer varied"


def test_ast_body_extraction_survives_a_nested_async_def():
    """The regression #1 names, demonstrated rather than asserted.

    The old string-split logic is reproduced here against a synthetic module
    containing a nested `async def`. It under-counts; the AST walk does not.
    Kept as a test so the brittle idiom cannot quietly return.
    """
    synthetic = (
        "import asyncio\n"
        "async def _run_session_check(slot, platform):\n"
        "    await asyncio.sleep(1)\n"
        "    async def _retry():\n"
        "        await asyncio.sleep(2)\n"
        "    await _retry()\n"
        "    await asyncio.sleep(3)\n"
        "async def check_session(slot, platform):\n"
        "    await asyncio.sleep(99)\n"
    )
    old_body = synthetic.split("async def _run_session_check")[1].split("async def")[0]
    assert old_body.count("asyncio.sleep(") == 1, (
        "the old split stopped at the nested 'async def' and saw 1 of the "
        "check's 2 own sleeps — that is the silent miscount finding #1 names"
    )

    tree = ast.parse(synthetic)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_run_session_check")
    assert _inner_wait_count(node) == 2, (
        "the AST walk must see both of the check's own waits and neither the "
        "nested helper's nor the following function's"
    )


def test_dwell_floor_is_preserved():
    """CLAUDE.md § Intentional design protects existing waits: variance is
    added *above* the original 3s settle, never in place of it.

    Asserts the constant rather than a source literal — the dwell was moved
    into named constants so the timeout budget could be computed from it."""
    assert session_manager.SESSION_CHECK_DWELL_BASE_S >= 3.0, (
        "the 3s redirect-settle floor was shortened; variance must be added "
        "above it, never in place of it"
    )
    assert session_manager.SESSION_CHECK_DWELL_SPREAD_S > 0


# --- timeout budget invariant --------------------------------------------
#
# The dwell above was added inside a fixed outer wait_for, silently cutting
# launch headroom from 7s to 2.5s. session_manager's own comment warns that
# the outer wait_for cancelling mid-launch strands a Chromium process or
# leaves a persistent context holding the profile lock. These tests make the
# margin an asserted invariant instead of a number to remember.


def test_session_check_budget_leaves_launch_headroom():
    """The regression this whole section exists for. Adding another wait
    inside the check must fail here until the outer timeout is raised."""
    worst = session_manager._inner_worst_case_s()
    margin = session_manager.SESSION_CHECK_TIMEOUT_S - worst
    assert margin >= session_manager.SESSION_CHECK_LAUNCH_MARGIN_S, (
        f"Inner worst case is {worst}s against a "
        f"{session_manager.SESSION_CHECK_TIMEOUT_S}s outer timeout, leaving "
        f"{margin}s for cold browser launch and teardown — below the "
        f"{session_manager.SESSION_CHECK_LAUNCH_MARGIN_S}s required. A "
        "cancelled launch strands Chrome or holds the profile lock."
    )


def test_goto_timeout_stays_below_the_outer_timeout():
    """The original invariant, previously only documented in a comment: a
    slow page load must surface as a graceful goto timeout inside the check,
    reaching the finally that closes the browser."""
    assert (
        session_manager.SESSION_CHECK_GOTO_TIMEOUT_S
        < session_manager.SESSION_CHECK_TIMEOUT_S
    )


def test_inner_worst_case_accounts_for_every_inner_wait():
    """Guards the budget helper itself: if a wait is added to the check but
    not to _inner_worst_case_s, the invariant above becomes a lie."""
    worst = session_manager._inner_worst_case_s()
    expected = (
        session_manager.SESSION_CHECK_GOTO_TIMEOUT_S
        + session_manager.SESSION_CHECK_DWELL_BASE_S
        + session_manager.SESSION_CHECK_DWELL_SPREAD_S
        + session_manager.SESSION_CHECK_SCROLL_PAUSE_MAX_S
    )
    assert worst == expected
    sleep_count = _inner_wait_count(_check_node())
    assert sleep_count == 2, (
        f"_run_session_check now has {sleep_count} waits but the budget "
        "helper models 2 (dwell + scroll pause). Update _inner_worst_case_s "
        "and re-check the outer timeout."
    )


# --- per-platform pre-flight opt-out (F5, third bullet) -------------------


def test_preflight_disabled_platform_skips_the_browser(monkeypatch):
    """Dropping a platform from PREFLIGHT_CHECK_PLATFORMS must eliminate the
    browser load entirely — that is the whole point of the switch."""
    monkeypatch.setattr(session_manager, "session_exists", lambda p, s: True)
    monkeypatch.setattr(
        session_manager, "PREFLIGHT_CHECK_PLATFORMS", frozenset({"tiktok"}))

    async def _must_not_run(slot, platform):
        raise AssertionError("browser check ran for a disabled platform")

    monkeypatch.setattr(session_manager, "_run_session_check", _must_not_run)
    assert asyncio.run(
        session_manager.check_session("A", "instagram")) == "check_disabled"


def test_preflight_disabled_still_filters_no_session(monkeypatch):
    """The free filesystem-level filtering must survive: disabling the check
    gives up the browser load, not the no_session case."""
    monkeypatch.setattr(session_manager, "session_exists", lambda p, s: False)
    monkeypatch.setattr(
        session_manager, "PREFLIGHT_CHECK_PLATFORMS", frozenset({"tiktok"}))
    assert asyncio.run(
        session_manager.check_session("A", "instagram")) == "no_session"


def test_check_disabled_does_not_skip_the_slot():
    """check_disabled must mean 'unknown, attempt anyway'. If it ever landed
    in the scheduler's skip set, disabling the check would silently stop all
    posting for that platform — the worst possible failure for this switch."""
    from backend import scheduler

    src = inspect.getsource(scheduler)
    skip_set = src.split('if status in (')[1].split(')')[0]
    assert "check_disabled" not in skip_set
    assert "expired" in skip_set and "no_session" in skip_set


def test_other_platform_still_checked_when_one_is_disabled(monkeypatch):
    """Disabling Instagram must not disable TikTok."""
    monkeypatch.setattr(session_manager, "session_exists", lambda p, s: True)
    monkeypatch.setattr(
        session_manager, "PREFLIGHT_CHECK_PLATFORMS", frozenset({"tiktok"}))

    async def _live(slot, platform):
        return "live"

    monkeypatch.setattr(session_manager, "_run_session_check", _live)
    assert asyncio.run(session_manager.check_session("A", "tiktok")) == "live"


def test_default_config_checks_both_platforms():
    """Default must preserve existing behaviour — this is opt-out, not a
    silent change to how scheduled batches behave."""
    from backend.config import PREFLIGHT_CHECK_PLATFORMS

    assert PREFLIGHT_CHECK_PLATFORMS == frozenset({"instagram", "tiktok"})
