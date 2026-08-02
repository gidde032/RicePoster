"""Regression tests for the accepted Phase A review findings (Groups A + B).

One test (or test group) per finding. Docstrings name the reviewer(s) and
severity. Findings that live deep inside Playwright flows cannot be executed
without launching a real browser (forbidden — this tool posts to live
accounts), so those are covered at the closest testable seam: extracted
helpers where possible, source-level assertions where not. Source-level
tests are explicitly labeled as such.
"""

import inspect
import subprocess
from pathlib import Path

from pydantic import SecretStr

import backend.main as main
from tests.source_probe import real_source
from backend import captions, instagram_browser, jitter, session_manager, tiktok_browser

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Finding #1 — fake success on missing post confirmation
# ---------------------------------------------------------------------------

def test_post_id_helper_distinguishes_unconfirmed():
    """Finding #1 (Reviewers 1+2+3, CRITICAL): posters returned a hardcoded
    ok ID even when no confirmation was observed. The extracted _post_id
    helper now encodes confirmation state into the returned ID."""
    for mod, prefix in ((instagram_browser, "ig_post"), (tiktok_browser, "tt_post")):
        ok = mod._post_id(prefix, "A", confirmed=True)
        unconfirmed = mod._post_id(prefix, "A", confirmed=False)
        assert ok == f"{prefix}_ok_A"
        assert "unconfirmed" in unconfirmed
        assert ok != unconfirmed


def test_posters_no_longer_return_unconditional_ok():
    """Finding #1 (Reviewers 1+2+3, CRITICAL) — source-level: the
    unconditional `return f\"..._ok_{account_key}\"` is gone from both
    post_media flows; both route through _post_id with a confirmed flag."""
    for mod in (instagram_browser, tiktok_browser):
        src = real_source(mod, "post_media")
        assert "_ok_{account_key}" not in src
        assert "_post_id(" in src
        assert "confirmed" in src


def test_frontend_renders_unconfirmed_state():
    """Finding #1 display layer (Reviewer 1, MEDIUM): the UI no longer
    hardcodes 'IG ✓ TT ✓'; it derives per-platform status from the IDs."""
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text()
    assert "IG ✓ TT ✓" not in html
    assert "platStatus" in html
    assert "unconfirmed" in html


# ---------------------------------------------------------------------------
# Finding #3 — /api/post accepts raw client filenames
# ---------------------------------------------------------------------------

def test_post_rejects_path_traversal_filename(client, tmp_media, monkeypatch):
    """Finding #3 (Reviewers 1+3, HIGH): a filename escaping media/ must be
    rejected at the API boundary, never reaching the poster."""
    reached = []

    async def fail_if_reached(slots, **kwargs):
        reached.append(slots)
        return []

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fail_if_reached)

    outside = tmp_media.parent / "outside.mp4"
    outside.write_bytes(b"x")
    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "../outside.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "outside the media directory" in resp.json()["detail"]
    assert reached == []


def test_post_rejects_missing_file(client, tmp_media, monkeypatch):
    """Finding #3 companion (Reviewer 3, MEDIUM): a stale/mistyped filename
    fails with a clear 400 instead of deep inside Playwright."""
    async def fail_if_reached(slots, **kwargs):
        raise AssertionError("poster reached with missing file")

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fail_if_reached)

    resp = client.post(
        "/api/post",
        json={
            "slots": [
                {"slot": "A", "filename": "A_never_uploaded.mp4", "caption": "hi", "media_type": "video"},
            ],
        },
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_upload_sanitizes_filename_and_validates_slot(client, tmp_media):
    """Finding #3 residue (all reviewers flagged the upload variant; largely
    defused by the slot prefix, but now hardened): path segments in the
    client filename are stripped, and unknown slots are rejected."""
    data = client.post(
        "/api/upload/A",
        files={"file": ("../../evil.mp4", b"x", "video/mp4")},
    ).json()
    assert data["filename"] == "A_evil.mp4"
    assert (tmp_media / "A_evil.mp4").exists()

    resp = client.post(
        "/api/upload/Z",
        files={"file": ("clip.mp4", b"x", "video/mp4")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Finding #4 — debug screenshots in project root, tracked by git
# ---------------------------------------------------------------------------

def test_no_screenshots_tracked_and_gitignore_covers_debug():
    """Finding #4 (Reviewers 1+2+3, HIGH): the five debug PNGs were tracked
    in git. They are now untracked and future ones are gitignored."""
    tracked = subprocess.run(
        ["git", "ls-files", "*.png"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    ).stdout.strip()
    assert tracked == ""
    gitignore = (PROJECT_ROOT / ".gitignore").read_text()
    assert "debug/" in gitignore


def test_screenshots_written_to_debug_dir():
    """Finding #4 (source-level): failure screenshots go to DEBUG_DIR, not
    the project root / CWD."""
    src = inspect.getsource(instagram_browser)
    assert "DEBUG_DIR" in src
    assert 'path=f"debug_' not in src  # old CWD-relative form is gone


# ---------------------------------------------------------------------------
# Finding #6 — clear_session platform typo deletes TikTok session
# ---------------------------------------------------------------------------

def test_clear_session_rejects_unknown_platform(tmp_sessions):
    """Finding #6 (Reviewer 1, CRITICAL): `clear isntagram A` used to fall
    into the else branch and delete the TikTok session dir. Unknown
    platforms must now delete nothing."""
    ig = tmp_sessions["instagram"] / "A"
    tt = tmp_sessions["tiktok"] / "A"
    for d in (ig, tt):
        d.mkdir()
        (d / "marker").write_text("x")

    session_manager.clear_session("isntagram", "A")

    assert ig.exists()
    assert tt.exists()


# ---------------------------------------------------------------------------
# Finding #7 — server bound to 0.0.0.0 with no auth
# ---------------------------------------------------------------------------

def test_run_sh_binds_localhost_only():
    """Finding #7 (Reviewer 1, CRITICAL): run.sh exposed the unauthenticated
    posting API to the whole network. It must bind loopback only."""
    run_sh = (PROJECT_ROOT / "run.sh").read_text()
    assert "127.0.0.1" in run_sh
    assert "0.0.0.0" not in run_sh


# ---------------------------------------------------------------------------
# Finding #5 — missing ANTHROPIC_API_KEY surfaces as raw SDK error
# ---------------------------------------------------------------------------

def test_missing_anthropic_key_raises_clear_error(monkeypatch):
    """Finding #5 (Reviewers 1+3, LOW): an unset key now fails fast with an
    actionable message instead of a generic SDK auth error."""
    import asyncio

    import pytest

    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr(""))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(captions.generate_caption("video", "topic"))


# ---------------------------------------------------------------------------
# Finding #8 — aborted login leaves a fake "logged in" session
# ---------------------------------------------------------------------------

def test_aborted_login_discards_fresh_profile(tmp_path):
    """Finding #8 (Reviewer 2, MEDIUM): an aborted manual login used to
    leave a populated profile dir that session_exists() reported as logged
    in forever. Typing 'abort' now discards a profile this attempt created."""
    for mod in (instagram_browser, tiktok_browser):
        d = tmp_path / f"{mod.__name__}_fresh"
        d.mkdir()
        (d / "junk").write_text("x")
        kept = mod._resolve_login_outcome(d, "abort", had_profile_before=False)
        assert kept is False
        assert not d.exists()


def test_aborted_login_preserves_preexisting_profile(tmp_path):
    """Finding #8 companion: 'abort' must never delete a session that
    existed before the login attempt."""
    d = tmp_path / "existing"
    d.mkdir()
    (d / "real_session").write_text("x")
    kept = tiktok_browser._resolve_login_outcome(d, "abort", had_profile_before=True)
    assert kept is True
    assert d.exists()


def test_plain_enter_still_saves_session(tmp_path):
    """Finding #8 guard: the maintainer's existing workflow (press Enter to
    save) is unchanged."""
    d = tmp_path / "fresh"
    d.mkdir()
    kept = tiktok_browser._resolve_login_outcome(d, "", had_profile_before=False)
    assert kept is True
    assert d.exists()


# ---------------------------------------------------------------------------
# Finding #9 — IG post path never detects an expired session
# ---------------------------------------------------------------------------

def test_ig_post_media_checks_login_redirect():
    """Finding #9 (Reviewer 2, MEDIUM) — source-level: the IG post path
    mirrors TikTok's login-redirect check and raises an actionable
    'session expired' error instead of an opaque selector failure.

    Batch 6 (#28) moved the check out of `post_media` into `_open_instagram`,
    which `post_media` calls first. Retargeted at the helper rather than
    widened to the whole module: this must keep asserting that the check runs
    on the posting path, not merely that the strings exist somewhere in the
    file. `tests/golden/ig_expired_session.trace` pins the resulting flow.
    """
    src = inspect.getsource(instagram_browser._open_instagram)
    # BE-23 replaced the inline literal with the module's marker tuple; the
    # markers themselves are pinned in test_poster_internals.py.
    assert "url_matches_login_markers(page.url, LOGIN_REDIRECT_MARKERS)" in src
    assert "/login" in "".join(instagram_browser.LOGIN_REDIRECT_MARKERS)
    assert "session expired" in src.lower()
    # ...and that post_media still reaches it, first, before the composer.
    flow = real_source(instagram_browser, "post_media")
    assert "_open_instagram(page, account_key)" in flow
    assert flow.index("_open_instagram") < flow.index("_upload_media_file")


# ---------------------------------------------------------------------------
# Finding #10 — unguarded context.close() in finally masks real errors
# ---------------------------------------------------------------------------

def test_finally_close_is_guarded():
    """Finding #10 (Reviewer 2, MEDIUM) — source-level: cleanup failures in
    the post_media finally blocks are caught so they can't replace the
    original posting exception."""
    for mod in (instagram_browser, tiktok_browser):
        src = real_source(mod, "post_media")
        assert "cleanup failed" in src


# ---------------------------------------------------------------------------
# Finding #11 — error text rendered via innerHTML unescaped
# ---------------------------------------------------------------------------

def test_frontend_escapes_untrusted_text():
    """Finding #11 (Reviewer 3, MEDIUM): error strings and account names in
    the status panel are HTML-escaped before insertion."""
    html = (PROJECT_ROOT / "frontend" / "index.html").read_text()
    assert "function esc(" in html
    assert "${esc(result.errors.join('; '))}" in html
    assert "${result.errors.join('; ')}" not in html
    assert "Posting failed: ${esc(e.message)}" in html
    # Note: the ${e.message} at the caption-generation catch goes into a
    # textarea .value (plain text, not innerHTML) and is intentionally
    # left unescaped.


# ---------------------------------------------------------------------------
# Finding #2 — overlapping post runs collide on Chrome's profile lock
# ---------------------------------------------------------------------------

def test_post_returns_409_while_run_in_progress(client, monkeypatch):
    """Finding #2 (Reviewers 1+2, HIGH): a second /api/post during an active
    run now gets a clear 409 instead of racing the persistent profile."""
    from backend import run_guard
    monkeypatch.setattr(run_guard, "_post_running", True)
    resp = client.post("/api/post", json={"slots": []})
    assert resp.status_code == 409
    assert "already in progress" in resp.json()["detail"]


def test_post_guard_resets_after_run(client, tmp_media, monkeypatch):
    """Finding #2 companion: the guard releases even when the run errors,
    so one failure can't wedge all future posting."""
    async def boom(slots, **kwargs):
        raise RuntimeError("poster exploded")

    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", boom)

    import pytest

    with pytest.raises(RuntimeError):
        client.post(
            "/api/post",
            json={
                "slots": [
                    {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
                ],
            },
        )
    from backend import run_guard
    assert run_guard._post_running is False


# ---------------------------------------------------------------------------
# Finding #14 / maintainer decision — stories removed permanently
# ---------------------------------------------------------------------------

def test_story_code_fully_removed():
    """Maintainer decision (2026-07-18): stories are removed entirely and
    permanently. No module exposes share_to_story and PostResult carries no
    story fields."""
    from backend import instagram, tiktok
    from backend.models import PostResult

    for mod in (instagram, tiktok, instagram_browser, tiktok_browser):
        assert not hasattr(mod, "share_to_story")
    assert "ig_story_id" not in PostResult.model_fields
    assert "tt_story_id" not in PostResult.model_fields


# ---------------------------------------------------------------------------
# Finding #12 — access token leaks into API-mode error text
# ---------------------------------------------------------------------------

def test_api_mode_errors_redact_tokens(monkeypatch):
    """Finding #12 (Reviewer 3, MEDIUM — api mode only): httpx errors embed
    the request URL including access_token; poster now redacts token values
    before they reach PostResult.errors / the UI."""
    import asyncio

    from pydantic import SecretStr

    from backend import poster
    from backend.config import AccountSlot

    fake = AccountSlot(
        slot="A", display_name="Test", ig_user_id="123",
        ig_token=SecretStr("IGSECRET_abc"), tt_token=SecretStr("TTSECRET_xyz"),
    )
    monkeypatch.setattr(poster, "get_accounts", lambda: [fake])

    async def leaky_ig(**kwargs):
        raise Exception("400 for url https://graph.facebook.com/x?access_token=IGSECRET_abc")

    async def leaky_tt(**kwargs):
        raise Exception("401 Authorization: Bearer TTSECRET_xyz")

    monkeypatch.setattr(poster.instagram, "post_media", leaky_ig)
    monkeypatch.setattr(poster.tiktok, "post_media", leaky_tt)

    result = asyncio.run(
        poster.post_slot("A", Path("/tmp/x.mp4"), "cap", "video")
    )
    joined = "; ".join(result.errors)
    assert "IGSECRET_abc" not in joined
    assert "TTSECRET_xyz" not in joined
    assert "***REDACTED***" in joined


# ---------------------------------------------------------------------------
# Finding #16 — tokens stored as plain str
# ---------------------------------------------------------------------------

def test_tokens_masked_in_repr_and_dump():
    """Finding #16 (Reviewer 3, LOW): tokens are SecretStr, so accidental
    logging or model_dump can't print them in cleartext."""
    from pydantic import SecretStr

    from backend.config import AccountSlot

    slot = AccountSlot(
        slot="A", display_name="Test", ig_user_id="123",
        ig_token=SecretStr("rawsecret1"), tt_token=SecretStr("rawsecret2"),
    )
    assert "rawsecret1" not in repr(slot)
    assert "rawsecret1" not in str(slot.model_dump())
    assert slot.ig_token.get_secret_value() == "rawsecret1"


# ---------------------------------------------------------------------------
# Maintainer-reported bug (2026-07-18): TikTok caption garbled by filename
# pre-fill. Root cause: TikTok pre-fills the caption field with the uploaded
# file's name; the editor's internal state survives DOM wipes, so the real
# caption got spliced into the middle of the resurrected filename text.
# Present in the original prototype; fixed on dev.
# ---------------------------------------------------------------------------

def test_captions_match_ignores_whitespace_rendering():
    """Maintainer-reported bug (2026-07-18), TikTok filename-splice garbling:
    the verify step must tolerate the editor re-rendering newlines/spacing,
    while still catching real corruption."""
    # Shape matters here, not wording: emoji, a blank-line-separated mention
    # block, and a trailing hashtag block are what the editor re-renders.
    # Sanitized from a real posted caption (#50 Phase B).
    caption = (
        "This one goes harder than it has any right to. 😂 Genuinely one of "
        "the best clips of the whole run.\n\n@studio @thestudio\n\n"
        "#clips #highlights #editing #latenight #goodstuff"
    )
    rerendered = caption.replace("\n\n", "\n")
    assert tiktok_browser._captions_match(caption, rerendered) is True
    assert tiktok_browser._captions_match(caption, caption) is True


def test_captions_match_catches_filename_splice():
    """Maintainer-reported bug (2026-07-18), TikTok filename-splice garbling —
    the exact failure mode observed on a live account: the caption spliced into
    the middle of the pre-filled filename text must fail verification."""
    # Sanitized from the real posted caption (#50 Phase B); the splice shape
    # is what matters — the caption embedded inside the filename text.
    caption = "This one goes harder than it has any right to. 😂"
    garbled = (
        "A_clip_export_final_v2 late night studio session "
        "-ud83d-udc80 " + caption + " and the rest of the filename"
    )
    assert tiktok_browser._captions_match(caption, garbled) is False
    assert tiktok_browser._captions_match(caption, "") is False


def test_captions_match_tolerates_editor_unicode_rewriting():
    """Batch 6 cold review (browser-automation reviewer, HIGH).

    Instagram now *abandons* a post whose caption does not read back, so a
    false mismatch is no longer harmless — it throws away a run whose media
    has already uploaded. Chromium contenteditable normalises inserted text
    to NFC, and Draft.js-style editors pad content with zero-width
    characters, so a byte comparison would reject captions that are in fact
    perfectly correct.

    Only invisible differences are tolerated. Anything a human could see is
    still a mismatch — that is what the check is for.
    """
    match = tiktok_browser._captions_match

    # Decomposed vs composed accents: identical to a reader, different bytes.
    assert match("café sunset", "café sunset") is True
    assert match("ṩ", "ṩ") is True

    # Zero-width padding the editor inserted on its own.
    assert match("golden hour", "golden​ hour") is True
    assert match("#tag", "﻿#tag") is True

    # Still catches visible corruption.
    assert match("golden hour", "golden hourr") is False
    assert match("#goldenhour", "#goldenhou") is False


def test_tiktok_failure_diagnostics():
    """Reviewer 2 LOW (IG/TT screenshot inconsistency) + live incident
    2026-07-18: two accounts failed with an opaque click timeout caused by
    TikTok's one-time 'Turn on automatic content checks?' TUXModal dialog.
    TikTok failures now capture a debug/ screenshot like IG does, and a
    blocking TUXModal's text is embedded in the raised error."""
    src = real_source(tiktok_browser, "post_media")
    assert "debug_tt_post_" in src
    assert "blocked by a TikTok dialog" in src
    assert "DEBUG_DIR" in inspect.getsource(tiktok_browser)
    # Batch 6 (#28) moved the overlay probe into _describe_blocking_modal.
    # Assert both the probe and that the failure path still calls it, so the
    # diagnostic cannot be orphaned by a later edit.
    assert "TUXModal" in inspect.getsource(tiktok_browser._describe_blocking_modal)
    assert "_describe_blocking_modal(page)" in src


def test_tiktok_caption_flow_hardened():
    """Maintainer-reported bug (2026-07-18), TikTok filename-splice garbling —
    source-level: the caption path uploads a short-named temp copy (so the
    filename can't pre-fill junk), clears via keyboard (not a DOM wipe the
    editor ignores), inserts atomically with a typing fallback, verifies via
    _captions_match before Post, and cleans up the temp copy.

    Batch 6 (#28) split this across named helpers. Each assertion is aimed at
    the helper that now owns that step rather than at a concatenation of the
    module, so it still proves the step happens in the posting path — and
    `tests/golden/tt_normal_post.trace` pins the resulting order.
    """
    shell = real_source(tiktok_browser, "post_media")
    upload = inspect.getsource(tiktok_browser._upload_media_file)
    entry = inspect.getsource(tiktok_browser._enter_and_verify_caption)

    assert "tt_upload_" in shell                    # temp-copy upload
    assert "upload_path.unlink" in shell            # temp cleanup in finally
    assert "set_input_files(str(upload_path))" in upload
    assert "ControlOrMeta+A" in entry               # editor-level clear
    assert "insert_text(caption)" in entry          # atomic insertion
    # Retry fallback retained. Batch 6 (#2) routed it through the shared
    # jittered-typing driver, which still types via press_sequentially — the
    # cadence changed, the fallback did not.
    assert "type_with_jitter(caption_field, caption)" in entry
    assert "press_sequentially" in inspect.getsource(jitter.type_with_jitter)
    assert "_captions_match(caption" in entry       # read-back verification

    # The ineffective DOM wipe must stay gone from the whole module.
    assert "textContent = ''" not in inspect.getsource(tiktok_browser)

    # ...and the shell must still run those steps, in order.
    assert shell.index("_upload_media_file") < shell.index("_enter_and_verify_caption")
    assert shell.index("_enter_and_verify_caption") < shell.index("_click_post_button")
