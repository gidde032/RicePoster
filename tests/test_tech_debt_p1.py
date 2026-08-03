"""Regression tests for the P1 tech-debt batch (audit 2026-07-29).

Each test names the audit finding it locks in. The audit was produced by a
multi-agent read-only pass (glm-4.7 finders, orchestrator-reviewed) and then
re-verified against the source before any fix landed; several of its findings
were rejected on that second pass and are deliberately NOT tested here.

Findings covered: BE-3 (duplicated browser helpers), BE-2 (selector-chain
diagnostics), BE-12 (fail fast on a missing API key), BE-10 (explicit caption
API timeout), TS-2 (loud pre-push-hook absence). FE-1a is asserted at
source level in this file because the frontend has no test harness.
"""

import re
import warnings

import pytest

from backend import browser_common, instagram_browser, tiktok_browser

from tests.paths import PROJECT_ROOT

BACKEND = PROJECT_ROOT / "backend"


# --- BE-3: shared browser helpers -------------------------------------------

def test_shared_browser_helpers_are_one_implementation():
    """BE-3: `_post_id` and `_resolve_login_outcome` were byte-identical copies
    in instagram_browser and tiktok_browser. Two copies of a decision about
    how results are labelled is a drift hazard: a fix to one platform silently
    leaves the other wrong. They must now be the *same object*, not merely
    equal source."""
    assert instagram_browser._post_id is browser_common._post_id
    assert tiktok_browser._post_id is browser_common._post_id
    assert instagram_browser._resolve_login_outcome is browser_common._resolve_login_outcome
    assert tiktok_browser._resolve_login_outcome is browser_common._resolve_login_outcome


def test_caption_verification_is_now_shared():
    """This test used to be the BE-3 scope guard asserting the opposite:
    `_captions_match` was TikTok-only, and hoisting it into the shared module
    "would imply Instagram needs the same read-back verification, which it
    does not".

    That premise was wrong and was reversed deliberately in Batch 6
    (maintainer decision 2026-07-30). Instagram's caption box is also a
    contenteditable, with the same '#hashtag' autocomplete hazard, and it was
    clicking Share without ever reading the field back — so a mangled caption
    published silently. Both platforms now verify, so the helper and the
    EDITOR_MARKER that pairs with it are genuinely shared.

    Rewritten in the commit that made the change rather than deleted, so the
    reversal stays visible to a later reader.
    """
    assert hasattr(browser_common, "_captions_match")
    assert instagram_browser._captions_match is browser_common._captions_match
    assert tiktok_browser._captions_match is browser_common._captions_match
    assert instagram_browser.EDITOR_MARKER is browser_common.EDITOR_MARKER
    assert tiktok_browser.EDITOR_MARKER is browser_common.EDITOR_MARKER


def test_post_id_behavior_unchanged_after_extraction():
    """BE-3: the extraction must not alter the ID format the UI and history
    parse. `poster_browser` decides ok-vs-unconfirmed by substring, so the
    'unconfirmed' token is load-bearing."""
    assert browser_common._post_id("ig_post", "A", confirmed=True) == "ig_post_ok_A"
    assert browser_common._post_id("tt_post", "B", confirmed=False) == "tt_post_unconfirmed_B"


def test_resolve_login_outcome_behavior_unchanged_after_extraction(tmp_path):
    """BE-3: an aborted login must not leave a profile dir that later looks
    like a saved session — but must not delete a dir that existed before."""
    created = tmp_path / "created"
    created.mkdir()
    assert browser_common._resolve_login_outcome(created, "abort", had_profile_before=False) is False
    assert not created.exists()

    preexisting = tmp_path / "preexisting"
    preexisting.mkdir()
    assert browser_common._resolve_login_outcome(preexisting, "abort", had_profile_before=True) is True
    assert preexisting.exists()

    kept = tmp_path / "kept"
    kept.mkdir()
    assert browser_common._resolve_login_outcome(kept, "", had_profile_before=False) is True
    assert kept.exists()


# --- BE-2: selector-chain diagnostics ---------------------------------------

def test_selector_chain_error_names_every_selector_and_count():
    """BE-2: an exhausted fallback chain used to raise a bare "Could not find
    caption field", discarding the element counts it had just measured. Since
    posting runs unattended on live accounts, a selector break — the most
    common failure mode, because the platforms reskin without notice — left no
    starting point for diagnosis."""
    msg = browser_common._selector_chain_error(
        "caption field",
        [("div[aria-label='Write a caption...']", 0), ("div[role='textbox']", 0)],
    )
    assert "caption field" in msg
    assert "div[aria-label='Write a caption...']" in msg
    assert "div[role='textbox']" in msg
    assert "→0" in msg
    assert "all 2 fallback selectors failed" in msg


def test_selector_chain_error_distinguishes_a_raising_locator_from_zero_matches():
    """BE-2: a selector that matched nothing and a selector whose locator call
    blew up are different diagnoses — one means the DOM changed, the other
    means the selector itself is now invalid. `None` count encodes the latter."""
    msg = browser_common._selector_chain_error("caption field", [("div.a", 0), ("div.b", None)])
    assert "'div.a'→0" in msg
    assert "'div.b'→raised" in msg


def test_selector_chain_error_handles_an_empty_chain():
    """BE-2 edge case: never raise a confusing "all 0 selectors failed"."""
    assert "no selectors were tried" in browser_common._selector_chain_error("x", [])


def test_both_caption_chains_raise_with_diagnostics():
    """BE-2: source-level assertion — Playwright flows are not E2E-tested
    (would require a real browser, which is forbidden), so this pins that
    neither platform still raises the bare message, and that both feed the
    accumulated attempts into the shared formatter."""
    for name in ("instagram_browser.py", "tiktok_browser.py"):
        src = (BACKEND / name).read_text()
        assert 'raise Exception("Could not find caption field")' not in src, name
        assert '_selector_chain_error("caption field", caption_attempts)' in src, name
        # the accumulator must record both the match count and the raise case
        assert "caption_attempts.append((selector, count))" in src, name
        assert "caption_attempts.append((selector, None))" in src, name


def test_tiktok_post_button_chain_is_out_of_scope():
    """BE-2 scope guard. The audit also cited tiktok_browser's Post-button
    chain, but that block never raises on exhaustion — it clicks whatever it
    finds, which is the positional heuristic tracked in issue #22, not a
    diagnostics gap. This pins the distinction so a later reader doesn't
    "finish" BE-2 by bolting diagnostics onto a chain that has no failure
    branch, and so the #22 fix is recognised as the real repair."""
    src = (BACKEND / "tiktok_browser.py").read_text()
    assert "button:has-text('Post')" in src
    assert 'raise Exception(_selector_chain_error("Post button"' not in src


# --- FE-1a: init() failure is visible ---------------------------------------

def test_init_failure_is_surfaced_in_the_ui(frontend_src):
    """FE-1: `init()` builds the entire UI, so a failed /api/accounts left a
    blank page and a console line nobody was watching — indistinguishable from
    "still loading". Source-level assertion: the frontend has no JS test
    harness, per the project's Playwright/E2E constraint.

    Note the audit called this `loadAccounts`; the function is `init()` (the
    name comes from its error string)."""
    src = frontend_src
    init_body = src.split("async function init()", 1)[1].split("\nasync function", 1)[0]
    assert "Failed to load accounts:" in init_body
    # The catch must write something into the DOM, not only the console. Batch 2
    # (Issue #30) routed every lookup through the guarded el()/elOpt() helpers,
    # so this asserts the panel is reached rather than naming one mechanism —
    # the contract is "the failure becomes visible", not "getElementById".
    assert "elOpt('statusPanel')" in init_body
    assert "Could not load accounts" in init_body
    # And a non-2xx must be treated as a failure rather than parsed as JSON.
    # Batch 2 (Issue #30, FE-2) replaced the inline `if (!resp.ok)` check here
    # with the shared helper, which throws on exactly the same condition.
    assert "await handleFetchError(resp)" in init_body
    # untrusted text goes through the existing escaper
    assert "esc(e.message" in init_body


# --- BE-12: fail loudly on a missing API key --------------------------------

def test_startup_config_flags_missing_api_key_outside_mock_mode(monkeypatch):
    """BE-12: an empty ANTHROPIC_API_KEY used to surface only when a caption was
    requested, as an Anthropic auth error mid-run — which reads like an API
    outage rather than a setup mistake."""
    from pydantic import SecretStr

    from backend import config

    monkeypatch.setattr(config, "POST_MODE", "browser")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr(""))
    problems = config.check_startup_config()
    assert any("ANTHROPIC_API_KEY" in p for p in problems)
    assert any("POST_MODE" in p for p in problems)


def test_startup_config_ignores_whitespace_only_api_key(monkeypatch):
    """BE-12: a key of spaces is not a key."""
    from pydantic import SecretStr

    from backend import config

    monkeypatch.setattr(config, "POST_MODE", "browser")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("   "))
    assert config.check_startup_config()


def test_startup_config_is_quiet_in_mock_mode_and_when_configured(monkeypatch):
    """BE-12: mock mode never calls the API, so a missing key is not a problem
    there — warning anyway would train the maintainer to ignore the check."""
    from pydantic import SecretStr

    from backend import config

    monkeypatch.setattr(config, "POST_MODE", "mock")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr(""))
    assert config.check_startup_config() == []

    monkeypatch.setattr(config, "POST_MODE", "browser")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("sk-ant-fake"))
    assert config.check_startup_config() == []


def test_startup_config_never_leaks_the_key(monkeypatch):
    """BE-12: the warning goes to stdout, so it must name the variable without
    echoing its value."""
    from pydantic import SecretStr

    from backend import config

    monkeypatch.setattr(config, "POST_MODE", "browser")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("sk-ant-SECRETVALUE"))
    # configured, so no problems at all — but assert the guard directly too
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("  "))
    assert "SECRETVALUE" not in " ".join(config.check_startup_config())


def test_startup_check_runs_at_server_startup():
    """BE-12: a validator nothing calls is not a gate."""
    src = (BACKEND / "main.py").read_text()
    lifespan = src.split("async def lifespan(app)", 1)[1].split("\napp = ", 1)[0]
    assert "check_startup_config()" in lifespan


# --- BE-10: bounded caption API timeout -------------------------------------

def test_caption_client_has_an_explicit_bounded_timeout():
    """BE-10, corrected on verification: the audit reported "no retry/timeout",
    but the installed SDK already retries twice with backoff — retry was never
    the gap. The real problem is the SDK's 600s default read timeout on a call
    that sits on the posting critical path, where a ten-minute stall is
    indistinguishable from a hang."""
    from backend import captions

    assert 0 < captions.CAPTION_API_TIMEOUT_S <= 120, (
        "timeout should be bounded well under the SDK's 600s default but long "
        "enough for a vision request"
    )
    src = (BACKEND / "captions.py").read_text()
    assert "timeout=CAPTION_API_TIMEOUT_S" in src


def test_sdk_still_provides_the_retries_we_rely_on():
    """BE-10: we deliberately did NOT add a retry wrapper because the SDK has
    one. That makes the SDK default part of our contract — if a future upgrade
    sets it to 0, this fix silently loses half its value."""
    from anthropic._constants import DEFAULT_MAX_RETRIES

    assert DEFAULT_MAX_RETRIES >= 1


# --- TS-2: a missing pre-push hook is not silent ----------------------------

def test_partial_hook_install_fails_loudly_rather_than_skipping():
    """TS-2: the old test skipped whenever the pre-push hook was absent, so it
    passed most confidently exactly when the gate was missing. A fresh clone
    should still skip, but a *partial* install — commit hook present, pre-push
    absent — is a real broken gate and must fail.

    This matters more than the audit credited: per CLAUDE.md the main ruleset
    requiring the Actions check is stored but not enforced on the current
    private Free repo, so the local hook is the only live enforcement."""
    src = (PROJECT_ROOT / "tests" / "test_audit_phase2.py").read_text()
    body = src.split("def test_pre_push_hook_if_installed_delegates_to_pre_commit", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert "pytest.fail(" in body, "partial install must fail, not skip"
    assert "PARTIAL HOOK INSTALL" in body
    assert "warnings.warn(" in body, "the fresh-clone case must still be visible"
    # the fresh-clone escape hatch must survive
    assert "pytest.skip(" in body


def test_caption_client_actually_receives_the_timeout(monkeypatch):
    """BE-10: the source-level assertion proves the kwarg is written, not that it
    reaches the client. This exercises the real construction path.

    The local stub is deliberate but not ideal — it is the fourth near-identical
    AsyncAnthropic fake in this suite (test_captions, test_caption_styles,
    test_thumbnail_captions, here). That duplication is issue #37 (TS-5); this
    test is concrete evidence for it rather than a reason to pre-empt it."""
    import asyncio
    from types import SimpleNamespace

    from pydantic import SecretStr

    from backend import captions

    seen = {}

    class _Stub:
        def __init__(self, api_key=None, **kwargs):
            seen.update(kwargs)
            self.messages = self

        async def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text="c")])

    monkeypatch.setattr(captions.anthropic, "AsyncAnthropic", _Stub)
    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("test-key"))
    asyncio.run(captions.generate_caption("video", "a topic"))

    assert seen.get("timeout") == captions.CAPTION_API_TIMEOUT_S


# --- Doc batch: pin the claims that drifted ---------------------------------

def test_documented_long_file_sizes_are_current():
    """DOC-2: CLAUDE.md's token-economics list is the map agents use to decide
    whether to full-read a file, so a stale entry costs tokens on every session.
    It had drifted twice. This makes the next drift a test failure instead of a
    re-measure someone has to remember.

    CLAUDE.md is gitignored and local-only, so a clean CI clone does not have
    it. Warns rather than skipping silently — the same trap TS-2 was about."""
    claude_path = PROJECT_ROOT / "CLAUDE.md"
    if not claude_path.exists():
        warnings.warn(
            "CLAUDE.md absent (clean clone/CI) — the token-economics line-count "
            "check only runs where the local agent docs exist.",
            UserWarning,
            stacklevel=2,
        )
        pytest.skip("CLAUDE.md is a local-only doc; see warning above")

    section = claude_path.read_text().split("- Long files:", 1)[1].split("\n- ", 1)[0]
    for rel in [
        "frontend/index.html",
        "backend/tiktok_browser.py",
        "backend/main.py",
        "backend/session_manager.py",
        "backend/scheduler.py",
        "backend/instagram_browser.py",
    ]:
        name = rel.split("/")[-1]
        # The claimed figure is read out of CLAUDE.md rather than duplicated
        # here. Hardcoding it meant a re-measure had to be applied in two
        # places, and forgetting the second made this test fail for the wrong
        # reason — the same double-bookkeeping the check exists to catch.
        # The list writes some entries with a path prefix (`frontend/index.html`)
        # and others bare (`main.py`), so allow either inside the backticks.
        match = re.search(rf"`[^`]*{re.escape(name)}` \(~(\d+)\)", section)
        assert match, f"{name} has no `~N` line-count claim in the long-file list"
        claimed = int(match.group(1))
        actual = len((PROJECT_ROOT / rel).read_text().splitlines())
        # 10% tolerance: the doc says "~", and pinning exactly would fail on
        # every one-line change. Wider than 10% is the drift worth catching.
        assert abs(actual - claimed) <= claimed * 0.10, (
            f"{rel} is {actual} lines but CLAUDE.md claims ~{claimed} — "
            f"re-measure the token-economics list"
        )


def test_scheduler_enabled_is_documented_where_users_look():
    """DOC-1: SCHEDULER_ENABLED gates whether queued batches fire at all, which
    is a safety-relevant switch, yet it appeared in no user-facing doc. The
    audit caught the README; it was also missing from the file users actually
    copy."""
    assert "SCHEDULER_ENABLED" in (PROJECT_ROOT / "README.md").read_text()
    assert "SCHEDULER_ENABLED" in (PROJECT_ROOT / "credentials.env.example").read_text()
    assert "SCHEDULER_ENABLED" in (BACKEND / "main.py").read_text()


def test_run_script_advertises_the_address_it_binds():
    """DOC-7: run.sh printed localhost while uvicorn binds 127.0.0.1. Not purely
    cosmetic — localhost can resolve to ::1, where nothing is listening."""
    run_sh = (PROJECT_ROOT / "run.sh").read_text()
    assert "--host 127.0.0.1" in run_sh
    assert "http://127.0.0.1:8000" in run_sh
    assert "http://localhost:8000" not in run_sh
