"""Shared fixtures and safety guards for the ClippingHarness test suite.

SAFETY: this suite must never launch a browser, post to a platform, or call
the Anthropic API. The autouse guard below replaces live-side entry points
with stubs that fail the test loudly. Tests that want to observe those calls
override the stub with monkeypatch.

The list below is explicit, not exhaustive-by-construction: it names each
guarded entry point, and a new one added to the codebase is NOT covered until
it is added here. The docstring used to claim it blocked "every live-side
entry point"; the Phase 1 audit (finding #4) found `poster.post_all` reachable
and unguarded, which is exactly the failure that overconfident wording hides.
Currently guarded:

  - main.post_all_browser / post_all_api / generate_caption  (app aliases)
  - poster_browser.post_all                                  (scheduler path)
  - poster.post_all                                          (API path)
  - instagram.post_media / tiktok.post_media                 (API leaves)
  - instagram_browser.post_media / tiktok_browser.post_media (browser leaves)
  - session_manager._run_session_check                       (health check)
  - NtfyNotifier.send                                        (network)
  - captions.anthropic.AsyncAnthropic                        (caption client)

The browser leaves were added in Batch 6. They are the code that actually
posts, and they had been unguarded while their inactive official-API
counterparts were guarded — safe only because a real Chrome is absent under
test. `allow_browser_post_media` is the single, explicit opt-in.
"""

import os
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).parent.parent

# Captured at import, before any test can run, so the opt-in fixture below can
# hand back the genuine coroutine functions after the autouse guard has
# replaced the module attributes.
import backend.instagram_browser as _instagram_browser
import backend.tiktok_browser as _tiktok_browser

_REAL_IG_POST_MEDIA = _instagram_browser.post_media
_REAL_TT_POST_MEDIA = _tiktok_browser.post_media


@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch):
    """Tripwire: any un-stubbed path to posting or caption generation fails."""
    import backend.main as main

    async def _blocked(*args, **kwargs):
        raise AssertionError(
            "Test reached a live posting/caption path without stubbing it. "
            "This must never happen — patch backend.main.post_all_browser / "
            "post_all_api / generate_caption in the test."
        )

    monkeypatch.setattr(main, "post_all_browser", _blocked)
    monkeypatch.setattr(main, "post_all_api", _blocked)
    monkeypatch.setattr(main, "generate_caption", _blocked)

    # The scheduler imports post_all directly from poster_browser, bypassing
    # the main module aliases above. Block it at the source so a test that
    # forgets to pass an explicit post_all_fn stub still trips the wire.
    import backend.poster_browser as poster_browser
    monkeypatch.setattr(poster_browser, "post_all", _blocked)

    # Same hole on the API side (fix: audit B2 / finding #4). poster.post_all
    # is the inactive-but-live official-API path; nothing stubbed it, so a test
    # importing it directly would have made real Graph/TikTok API calls. Its
    # two leaves are blocked as well, since poster.post_slot reaches them
    # without going through post_all.
    import backend.poster as poster
    import backend.instagram as instagram
    import backend.tiktok as tiktok

    monkeypatch.setattr(poster, "post_all", _blocked)
    monkeypatch.setattr(instagram, "post_media", _blocked)
    monkeypatch.setattr(tiktok, "post_media", _blocked)

    # The browser posting leaves. These are the functions that actually drive
    # live Instagram and TikTok, and until Batch 6 they were the only live
    # entry points not named here — the inactive official-API path was
    # guarded while the active one was not. Nothing had reached them, but
    # that was luck plus the absence of a browser binary, not a guard.
    monkeypatch.setattr(_instagram_browser, "post_media", _blocked)
    monkeypatch.setattr(_tiktok_browser, "post_media", _blocked)

    # Health-check path launches real Chromium when a session exists. A test
    # that hits check_session without stubbing _run_session_check would open a
    # browser; block that too. Tests that exercise the check monkeypatch over
    # this, same pattern as the stubs above.
    import backend.session_manager as session_manager

    async def _blocked_check(*args, **kwargs):
        raise RuntimeError("tripwire: _run_session_check reached un-stubbed")

    monkeypatch.setattr(session_manager, "_run_session_check", _blocked_check)

    # NtfyNotifier.send makes a real HTTP POST to ntfy.sh. No test may ever hit
    # the network. Block the method loudly; tests that assert real send
    # behaviour restore it (capturing the original at import) and mock httpx.
    import backend.notifier as notifier

    async def _blocked_send(*args, **kwargs):
        raise AssertionError(
            "tripwire: NtfyNotifier.send reached un-stubbed — no test may make a "
            "real ntfy request. Restore the captured original and mock httpx, or "
            "use a fake Notifier."
        )

    monkeypatch.setattr(notifier.NtfyNotifier, "send", _blocked_send)

    # The caption client itself. `main.generate_caption` is aliased and blocked
    # above, but eleven tests import `captions.generate_caption` directly, and
    # that function constructs its own AsyncAnthropic. Blocking the client
    # class means a test that forgets to install a fake fails loudly instead of
    # making a real, billed Anthropic request.
    #
    # This was a *conditional* hole rather than a live defect: config.py skips
    # load_dotenv under pytest, so ANTHROPIC_API_KEY is empty in a default run
    # and on CI, and captions.generate_caption raises before it builds a
    # client. It becomes reachable only when the key is exported in the
    # developer's shell, which os.getenv picks up regardless. conftest's own
    # docstring and README both promise this is closed, so it now is.
    # Found by cold review, 2026-07-31.
    import backend.captions as captions

    class _BlockedAnthropic:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "tripwire: a test constructed a real Anthropic client. Install "
                "a fake — see the `capturing_anthropic` fixture — rather than "
                "letting captions.generate_caption reach the API."
            )

    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic", _BlockedAnthropic)


@pytest.fixture(autouse=True)
def reset_run_guard():
    """Reset the process-global posting flag around every test.

    Fix: audit B3 / finding #5. `run_guard._post_running` is module state
    shared by /api/post and the scheduler. A test that set it True and did not
    restore it made every later test silently take the "already running" early
    return — passing for the wrong reason. Several tests reset it by hand;
    this makes that unnecessary and closes the ordering trap.
    """
    from backend import run_guard

    run_guard._post_running = False
    yield
    run_guard._post_running = False


@pytest.fixture
def allow_browser_post_media(monkeypatch):
    """Explicit opt-in to calling the real browser `post_media` functions.

    The only legitimate user is `tests/test_poster_internals.py`, which runs
    them against the recording fakes in `tests/browser_trace.py`. Those
    replace `async_playwright` in the module under test, so no browser
    binary, network call or session directory is reachable — the flow is
    driven entirely by scripted answers.

    Requested by name, never autouse: a test that wants the real function has
    to say so in its signature, which is where a reviewer will see it.

    This must run *after* the autouse `_no_live_calls` guard, which it does
    today because pytest instantiates autouse fixtures before explicitly
    requested ones at the same scope. That ordering is emergent behaviour
    rather than a documented stability guarantee, so it is asserted rather
    than assumed: if a future pytest reverses it, this fails loudly here
    instead of silently leaving the tripwire un-restored — or worse, leaving
    the guard disarmed for the rest of the test.
    """
    for module in (_instagram_browser, _tiktok_browser):
        assert module.post_media.__name__ == "_blocked", (
            f"{module.__name__}.post_media is not the tripwire stub at opt-in "
            "time — fixture ordering changed, and the guard may be disarmed. "
            "Do not weaken this assertion; fix the ordering."
        )
    monkeypatch.setattr(_instagram_browser, "post_media", _REAL_IG_POST_MEDIA)
    monkeypatch.setattr(_tiktok_browser, "post_media", _REAL_TT_POST_MEDIA)


@pytest.fixture
def client():
    from backend.main import app

    return TestClient(app)


@pytest.fixture
def tmp_media(monkeypatch, tmp_path):
    """Redirect MEDIA_DIR to a temp dir so tests never touch media/."""
    import backend.main as main

    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(main, "MEDIA_DIR", media)
    return media


@pytest.fixture
def tmp_sessions(monkeypatch, tmp_path):
    """Redirect both platforms' SESSIONS_DIR to temp dirs so tests never
    touch sessions/. session_manager reads these via module attributes at
    call time, so patching the browser modules is sufficient."""
    from backend import instagram_browser, tiktok_browser

    ig = tmp_path / "sessions" / "instagram"
    tt = tmp_path / "sessions" / "tiktok"
    ig.mkdir(parents=True)
    tt.mkdir(parents=True)
    monkeypatch.setattr(instagram_browser, "SESSIONS_DIR", ig)
    monkeypatch.setattr(tiktok_browser, "SESSIONS_DIR", tt)
    return {"instagram": ig, "tiktok": tt}


@pytest.fixture(autouse=True)
def no_inter_slot_delay(monkeypatch):
    """Disable the F4 account-spacing gap for the whole suite.

    It defaults to 60-180s, and tests that drive the real post_all with
    several slots (test_scheduling_slice2) would otherwise sleep for minutes
    per gap — the suite went from 3s to hanging when the default was turned
    on. Tests that exercise the delay itself set their own bounds.
    """
    from backend import poster_browser

    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MIN_S", 0.0)
    monkeypatch.setattr(poster_browser, "INTER_SLOT_DELAY_MAX_S", 0.0)


@pytest.fixture(autouse=True)
def tmp_health_cache(monkeypatch, tmp_path):
    """Redirect the F5 session-health cache to a temp file.

    Autouse and unconditional: check_session now writes a cache entry on
    every call, so without this a test run would both pollute the real
    sessions/.health_cache.json and let one test's cached "live" leak into
    another test's check."""
    from backend import session_manager

    monkeypatch.setattr(
        session_manager, "HEALTH_CACHE_FILE", tmp_path / ".health_cache.json"
    )


@pytest.fixture(autouse=True)
def tmp_history_file(monkeypatch, tmp_path):
    """Redirect the run-history file to a temp path.

    `main.HISTORY_FILE` was a module-level constant pointing at the repo
    root, so every test that reached `_append_history` appended to the
    maintainer's real, gitignored history.jsonl — a full suite run grew it by
    7 rows, and 354 fixture rows had accumulated before this was caught
    (2026-07-27). See `tests/test_history_isolation.py` for the full finding.

    Autouse and unconditional, matching `tmp_ig_sessions_dir`: the write is
    wrapped in a bare `except` that swallows failures, so an unpatched test
    pollutes silently rather than erroring. `scheduler.py` needs no equivalent
    — it already takes an injectable `history_file` parameter.

    Returns the redirected path. Autouse fixtures may also be requested by
    name, and #37 folded test_history_headless.py's `tmp_history` — a second
    fixture patching this same attribute to this same path, differing only in
    returning it — into this one.
    """
    from backend import main

    history = tmp_path / "history.jsonl"
    monkeypatch.setattr(main, "HISTORY_FILE", history)
    return history


@pytest.fixture(autouse=True)
def tmp_queue_paths(monkeypatch, tmp_path):
    """Redirect the queue file, its history, and the media snapshot tree.

    Autouse and unconditional, for the same reason as `tmp_history_file` but
    with a worse failure mode: `queue.reconcile_queue_media` **deletes
    directories**, and it now runs from the FastAPI lifespan. Any test that
    enters `TestClient(main.app)` without patching these three names therefore
    ran a deleting pass over the maintainer's real `queue_media/` — proven by
    `tests/test_scheduling_slice3.py::TestLifespan`, which printed a
    classification of the real 2026-07-27 forensic snapshot (cold review,
    2026-07-30). Nothing was lost, because that directory classifies as
    retained evidence and is never auto-deleted; a snapshot that classified as
    an orphan would have been removed by running the test suite.

    Patched on `backend.queue` rather than `backend.config` because every
    function in that module resolves its default from the module-level name at
    call time, which is also the pattern the per-test patches already use.
    """
    from backend import queue as queue_mod

    monkeypatch.setattr(queue_mod, "QUEUE_FILE", tmp_path / "queue.jsonl")
    monkeypatch.setattr(queue_mod, "HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr(queue_mod, "QUEUE_MEDIA_DIR", tmp_path / "queue_media")


@pytest.fixture(autouse=True)
def tmp_ig_sessions_dir(monkeypatch, tmp_path):
    """Point the account-label lookup at an empty temp dir.

    Renamed from `_IG_SESSIONS_DIR` to `IG_SESSIONS_DIR` by #27, which moved
    the constant into config.py's shared layout section. If a rename like that
    ever misses this line the fixture becomes a silent no-op and the suite
    reads live session data again, so
    `test_paths.py::test_snapshot_predates_the_conftest_redirects` asserts this
    redirect is actually in effect.

    `_slot_display_name` reads `sessions/instagram/{slot}/LABEL` to name an
    account, so an unpatched `get_accounts()` otherwise reads the
    maintainer's real, gitignored session directory — the same
    non-hermeticity that made the suite depend on `credentials.env` (see
    `backend/config.py` UNDER_PYTEST and
    `test_gates.py::test_suite_does_not_read_the_maintainers_credentials_env`).
    Two tests in the smoke tier call `get_accounts()` unpatched, so this is
    autouse and unconditional. Tests exercising the label chain set their own
    directory, which wins over this fixture.
    """
    from backend import config

    labels = tmp_path / "ig_sessions"
    labels.mkdir()
    monkeypatch.setattr(config, "IG_SESSIONS_DIR", labels)


# ---------------------------------------------------------------------------
# Fixtures centralized by #37 (TS-5)
#
# Each one below had two or more near-identical copies in individual test
# files. Fixtures with a single consumer deliberately stayed where they are —
# #37 asks for the *reusable* ones, and moving a single-use fixture only puts
# distance between it and the test that explains it. `restore_logger` in
# test_logging_adoption.py is the notable case left in place on that reasoning
# (maintainer decision, 2026-07-31).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frontend_src():
    """The whole of `frontend/index.html` as text.

    The frontend is one vanilla HTML/JS file with no build step and no E2E
    coverage, so a large family of tests asserts against its source directly.
    Module-scoped because it is a ~1500-line read and nothing mutates it.
    """
    return (PROJECT_ROOT / "frontend" / "index.html").read_text()


class CapturingAnthropic:
    """Fake `AsyncAnthropic` that records the kwargs it was called with.

    `calls` is class-level state, reset by the `capturing_anthropic` fixture
    rather than by `__init__`, because `captions.generate_caption` constructs
    its own client and the test only gets to see the class.
    """

    calls = []

    def __init__(self, api_key=None, **kwargs):
        # **kwargs mirrors the real client (timeout/max_retries) — see BE-10.
        self.messages = self

    async def create(self, **kwargs):
        CapturingAnthropic.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text="a caption")])


@pytest.fixture
def capturing_anthropic(monkeypatch):
    """Swap in `CapturingAnthropic` and supply a key, so no real call is made.

    Merged from copies in test_caption_styles.py and test_thumbnail_captions.py
    that differed only in the caption text the fake returned ("styled caption"
    vs "a caption"). No consumer of either asserted on that text — all eight
    assert on `.calls` — so the copies were behaviourally identical and the
    surviving text is arbitrary.

    Patching `ANTHROPIC_API_KEY` is not decoration: `generate_caption` raises a
    clear error on an unset key before it ever reaches the client, so without
    it the fake is never constructed.
    """
    # Imported in-body, matching every other fixture here: conftest must not
    # pull backend modules in at collection time, before SCHEDULER_ENABLED is
    # set above.
    from backend import captions

    CapturingAnthropic.calls = []
    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic", CapturingAnthropic)
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("test-key"))
    return CapturingAnthropic
