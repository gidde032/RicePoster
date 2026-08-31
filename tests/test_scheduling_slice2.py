"""Tests for Slice 2 — Failure notifications (FR-F3).

Covers DESIGN-scheduling.md §4: the pluggable Notifier interface, the
NtfyNotifier HTTP surface, the get_notifier() factory, and the wiring of
notifications into poster_browser.post_all. Cross-slice interface contract 2
(get_notifier() -> Notifier, await notifier.send(title, body, priority) -> bool,
never raises) is what Slice 3's scheduler will consume, so its shape is asserted
here.

No test in this file makes a real network request: the conftest tripwire blocks
NtfyNotifier.send, and the send tests that need the real implementation restore
the original (captured at import, below) and mock httpx.
"""

import asyncio

import pytest

import backend.config as config
import backend.notifier as notifier
import backend.poster_browser as poster_browser
from backend.models import PostResult
from backend.notifier import Notifier, NtfyNotifier, get_notifier

_real_post_all = poster_browser.post_all

# Captured at import time, BEFORE the autouse tripwire replaces the class method
# per-test. Tests that exercise the real send restore this and mock httpx.
_REAL_SEND = NtfyNotifier.send


# --- test doubles -----------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Stand-in for httpx.AsyncClient used as an async context manager. Records
    every .post() call so tests can assert URL/headers/body composition."""

    calls = []

    def __init__(self, status_code=200, raise_exc=None):
        self._status_code = status_code
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeClient.calls.append({"url": url, "content": content, "headers": headers})
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResp(self._status_code)


def _use_fake_httpx(monkeypatch, status_code=200, raise_exc=None):
    """Restore the real send (tripwire replaced it) and swap httpx for a fake."""
    _FakeClient.calls = []
    monkeypatch.setattr(NtfyNotifier, "send", _REAL_SEND)
    monkeypatch.setattr(
        notifier.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(status_code=status_code, raise_exc=raise_exc),
    )


class _RecordingNotifier(Notifier):
    """A fake notifier that records every send and never touches the network."""

    def __init__(self, return_value=True):
        self.sent = []
        self._return_value = return_value

    async def send(self, title, body, priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return self._return_value


# --- base Notifier ----------------------------------------------------------

def test_base_notifier_is_noop_returning_false():
    """§4: base Notifier is a no-op — notifications disabled by default."""
    assert asyncio.run(Notifier().send("t", "b")) is False


# --- NtfyNotifier.send ------------------------------------------------------

def test_ntfy_send_success_composes_url_headers_body(monkeypatch):
    """§4: 200 → True; URL is {server}/{topic}, title/priority in headers,
    body is UTF-8 encoded."""
    _use_fake_httpx(monkeypatch, status_code=200)
    n = NtfyNotifier(topic="mytopic", server="https://ntfy.sh")
    assert n.url == "https://ntfy.sh/mytopic"

    ok = asyncio.run(n.send("Title here", "Body text", priority="high"))
    assert ok is True

    call = _FakeClient.calls[-1]
    assert call["url"] == "https://ntfy.sh/mytopic"
    assert call["headers"] == {"Title": "Title here", "Priority": "high"}
    assert call["content"] == b"Body text"


def test_ntfy_send_non_200_returns_false(monkeypatch):
    """§4: any non-200 status → False."""
    _use_fake_httpx(monkeypatch, status_code=429)
    n = NtfyNotifier(topic="t")
    assert asyncio.run(n.send("a", "b")) is False


def test_ntfy_send_exception_returns_false_never_raises(monkeypatch):
    """§4 + contract 2: a network exception collapses to False, never raises."""
    _use_fake_httpx(monkeypatch, raise_exc=RuntimeError("connection refused"))
    n = NtfyNotifier(topic="t")
    # Must not raise:
    assert asyncio.run(n.send("a", "b")) is False


def test_ntfy_server_trailing_slash_not_doubled():
    """A server value with a trailing slash must not yield '//topic'."""
    assert NtfyNotifier(topic="t", server="https://ntfy.sh/").url == "https://ntfy.sh/t"


# --- get_notifier factory ---------------------------------------------------

def test_get_notifier_none_returns_base_notifier(monkeypatch):
    """§4: NOTIFY_SERVICE=none → the no-op base Notifier."""
    monkeypatch.setattr(config, "NOTIFY_SERVICE", "none")
    n = get_notifier()
    assert type(n) is Notifier


def test_get_notifier_ntfy_returns_ntfy_notifier(monkeypatch):
    """§4: NOTIFY_SERVICE=ntfy with a topic → NtfyNotifier at the right URL."""
    from pydantic import SecretStr

    monkeypatch.setattr(config, "NOTIFY_SERVICE", "ntfy")
    monkeypatch.setattr(config, "NTFY_TOPIC", SecretStr("phone-abc"))
    monkeypatch.setattr(config, "NTFY_SERVER", "https://example.test")
    n = get_notifier()
    assert isinstance(n, NtfyNotifier)
    assert n.url == "https://example.test/phone-abc"


def test_get_notifier_ntfy_missing_topic_falls_back_to_noop(monkeypatch):
    """§4 decision: NOTIFY_SERVICE=ntfy but empty NTFY_TOPIC must not crash the
    server — fall back to the no-op base Notifier (a warning is logged)."""
    from pydantic import SecretStr

    monkeypatch.setattr(config, "NOTIFY_SERVICE", "ntfy")
    monkeypatch.setattr(config, "NTFY_TOPIC", SecretStr("   "))  # whitespace only
    n = get_notifier()
    assert type(n) is Notifier


def test_get_notifier_unknown_service_falls_back_to_noop(monkeypatch):
    """An unrecognised NOTIFY_SERVICE disables notifications rather than erroring."""
    monkeypatch.setattr(config, "NOTIFY_SERVICE", "pushover")
    assert type(get_notifier()) is Notifier


# --- post_all integration ---------------------------------------------------

def _run_post_all(monkeypatch, canned, notifier_obj):
    """Drive post_all with post_slot stubbed to return canned PostResults keyed
    by slot, so no session/browser path is touched."""
    async def fake_post_slot(slot, media_path, caption, media_type,
                             headless=True, progress_cb=None,
                             skip_platforms=None):
        return canned[slot]

    monkeypatch.setattr(poster_browser, "post_slot", fake_post_slot)
    monkeypatch.setattr(poster_browser, "post_all", _real_post_all)
    slots = [{"slot": s, "media_path": "x", "caption": "SECRET CAPTION TEXT",
              "media_type": "image"} for s in canned]
    return asyncio.run(poster_browser.post_all(slots, notifier=notifier_obj))


def test_post_all_notifies_failed_slot_and_summary(monkeypatch):
    """§4: a slot that errors gets a per-slot notification; the run summary
    reports the failure count and names the failed slot/platform."""
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_ok_A", tt_post_id="tt_ok_A"),
        "B": PostResult(slot="B", ig_post_id="ig_ok_B",
                        errors=["TT post: session expired"]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    bodies = [m["body"] for m in rec.sent]
    # Per-slot failure notification for B/TikTok:
    assert any("Account 2 TikTok: error" in b and "session expired" in b for b in bodies)
    # No per-slot notification for the fully-successful slot A:
    assert not any("Account 1" in b for b in bodies)
    # Run summary, last, with the failed slot named:
    summary = rec.sent[-1]["body"]
    assert summary.startswith("1/2 posted, 1 failed")
    assert "account 2 TikTok" in summary
    assert "session expired" in summary


def test_post_all_notifies_unconfirmed_slot(monkeypatch):
    """§4: an unconfirmed post (post-id contains 'unconfirmed') triggers a
    per-slot notification even though it isn't a hard error."""
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_unconfirmed_A", tt_post_id="tt_ok_A"),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    bodies = [m["body"] for m in rec.sent]
    assert any("Account 1 Instagram: unconfirmed" in b for b in bodies)


def test_post_all_all_success_summary(monkeypatch):
    """§4: an all-clean run sends only the '3/3 posted successfully' summary."""
    canned = {
        s: PostResult(slot=s, ig_post_id=f"ig_ok_{s}", tt_post_id=f"tt_ok_{s}")
        for s in ("A", "B", "C")
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    assert len(rec.sent) == 1
    assert rec.sent[0]["body"] == "3/3 posted successfully"


def test_notifications_never_contain_caption_text(monkeypatch):
    """§4 (what NOT to include): no caption text may appear in any notification
    title or body — push previews could leak sensitive content."""
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_unconfirmed_A",
                        errors=["TT post: upload failed while captioned"]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    for m in rec.sent:
        assert "SECRET CAPTION TEXT" not in m["title"]
        assert "SECRET CAPTION TEXT" not in m["body"]


def test_notifier_exception_does_not_fail_the_run(monkeypatch):
    """§4: a notifier that raises must not break posting — post_all still
    returns every result."""
    class _BoomNotifier(Notifier):
        async def send(self, title, body, priority="default"):
            raise RuntimeError("notifier is on fire")

    canned = {"A": PostResult(slot="A", errors=["TT post: boom"])}
    results = _run_post_all(monkeypatch, canned, _BoomNotifier())
    assert [r.slot for r in results] == ["A"]


def test_post_all_default_notifier_is_noop_no_network(monkeypatch):
    """post_all(notifier=None) resolves get_notifier(); with NOTIFY_SERVICE=none
    that's the no-op base Notifier, so the run completes without any network."""
    monkeypatch.setattr(config, "NOTIFY_SERVICE", "none")

    async def fake_post_slot(slot, media_path, caption, media_type,
                             headless=True, progress_cb=None,
                             skip_platforms=None):
        return PostResult(slot=slot, ig_post_id="ig_ok", tt_post_id="tt_ok")

    monkeypatch.setattr(poster_browser, "post_slot", fake_post_slot)
    monkeypatch.setattr(poster_browser, "post_all", _real_post_all)
    results = asyncio.run(poster_browser.post_all(
        [{"slot": "A", "media_path": "x", "caption": "c", "media_type": "image"}]
    ))
    assert results[0].slot == "A"


# --- review fixes: caption leak (finding 1, CRITICAL) -----------------------

def test_notifications_redact_tiktok_caption_verification_leak(monkeypatch):
    """Finding 1 (CRITICAL caption leak): the TikTok caption verifier
    (tiktok_browser.py:518-522) echoes live editor text via `{last_seen[:200]!r}`
    and poster_browser stores it as `TT post: {e}`. Built from that REAL f-string
    shape, the sentinel caption must never appear in any per-slot OR summary push.
    """
    sentinel = "SENTINEL_LEAK_private_caption_text"
    last_seen = sentinel + " ...garbled remainder of the editor buffer..."
    tiktok_exc = (
        f"TikTok caption verification failed for A: editor text does not "
        f"match the intended caption after 2 attempts — aborting before posting "
        f"garbled text. Editor contained: {last_seen[:200]!r}"
    )
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_ok_A",
                        errors=[f"TT post: {tiktok_exc}"]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    assert len(rec.sent) >= 2  # per-slot TikTok error push + run summary
    for m in rec.sent:
        assert sentinel not in m["title"]
        assert sentinel not in m["body"]


def test_notification_safe_strips_bare_quoted_repr():
    """Finding 1 (defensive strip): an arbitrary browser exception echoing DOM
    content in a quoted repr >20 chars is redacted even without the
    'Editor contained' marker."""
    leak = "Locator '<div>private caption lives here</div>' not visible"
    out = poster_browser._notification_safe(leak)
    assert "private caption lives here" not in out
    assert "<redacted>" in out


# --- review fixes: no-session skips (finding 2, HIGH) -----------------------

_IG_SKIP = "IG post: skipped (no session — run session_manager login instagram)"


def test_skip_only_slot_produces_no_per_slot_push(monkeypatch):
    """Finding 2: a slot whose platform is only skipped (no session) is neither
    an error nor unconfirmed, so it gets NO per-slot push; the run summary is
    the only notification."""
    canned = {
        "A": PostResult(slot="A", tt_post_id="tt_ok_A", errors=[_IG_SKIP]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    assert len(rec.sent) == 1  # summary only, no per-slot push
    summary = rec.sent[0]["body"]
    assert summary == "1/1 posted, 1 skipped (no session: account 1 IG)"
    assert "failed" not in summary


def test_summary_reports_skips_and_failures_distinctly(monkeypatch):
    """Finding 2: a mixed run (1 ok, 1 failed, 1 skipped) reports the failure
    count separately from the skip count; the skip is excluded from failures."""
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_ok_A", tt_post_id="tt_ok_A"),
        "B": PostResult(slot="B", ig_post_id="ig_ok_B",
                        errors=["TT post: session expired"]),
        "C": PostResult(slot="C", tt_post_id="tt_ok_C", errors=[_IG_SKIP]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    summary = rec.sent[-1]["body"]
    assert summary.startswith("2/3 posted")
    assert "1 failed: [account 2 TikTok: session expired]" in summary
    assert "1 skipped (no session: account 3 IG)" in summary
    # B is the only per-slot push (skip C silent, success A silent):
    per_slot = [m for m in rec.sent[:-1]]
    assert all("Account 2" in m["body"] for m in per_slot)
    assert per_slot  # B did push


def test_notifications_use_order_labels_not_private_account_ids(monkeypatch):
    """Cold-review repair (MEDIUM): local folder IDs never leave through ntfy."""
    private_id = "private-account-handle"
    canned = {
        private_id: PostResult(slot=private_id, errors=["TT post: upload failed"]),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)

    assert rec.sent
    assert all(private_id not in message["title"] for message in rec.sent)
    assert all(private_id not in message["body"] for message in rec.sent)
    assert any("account 1" in message["body"].lower() for message in rec.sent)


# --- review fixes: httpx timeout (finding 3, HIGH) --------------------------

def test_ntfy_send_uses_explicit_bounded_timeout(monkeypatch):
    """Finding 3: NtfyNotifier.send must bound the HTTP call so a hung ntfy
    endpoint cannot stall a posting run — AsyncClient gets timeout=10.0."""
    captured = {}

    def fake_client(*a, **k):
        captured.update(k)
        return _FakeClient(status_code=200)

    _FakeClient.calls = []
    monkeypatch.setattr(NtfyNotifier, "send", _REAL_SEND)
    monkeypatch.setattr(notifier.httpx, "AsyncClient", fake_client)
    asyncio.run(NtfyNotifier(topic="t").send("a", "b"))
    assert captured.get("timeout") == 10.0


# --- review fixes: topic/server validation (finding 4, HIGH) ----------------

def test_get_notifier_rejects_malformed_topic_without_leaking_it(monkeypatch, capsys):
    """Finding 4: a topic with a slash/space/etc. would repoint the request URL.
    Invalid topics → no-op Notifier + a warning that never prints the topic
    value (it is a capability token)."""
    from pydantic import SecretStr

    monkeypatch.setattr(config, "NOTIFY_SERVICE", "ntfy")
    for bad in ("has/slash", "has space", "a" * 65):
        monkeypatch.setattr(config, "NTFY_TOPIC", SecretStr(bad))
        assert type(get_notifier()) is Notifier
        out = capsys.readouterr().out
        assert "invalid NTFY_TOPIC" in out
        assert bad not in out  # the topic value must not leak into logs


def test_get_notifier_rejects_bad_server_scheme(monkeypatch, capsys):
    """Finding 4: NTFY_SERVER without an http(s) scheme → no-op Notifier +
    warning, distinguishing misconfig (warned once at factory) from a network
    failure (False at send time)."""
    from pydantic import SecretStr

    monkeypatch.setattr(config, "NOTIFY_SERVICE", "ntfy")
    monkeypatch.setattr(config, "NTFY_TOPIC", SecretStr("goodtopic"))
    monkeypatch.setattr(config, "NTFY_SERVER", "ftp://evil.test")
    assert type(get_notifier()) is Notifier
    assert "invalid NTFY_SERVER" in capsys.readouterr().out


def test_get_notifier_empty_topic_warns_without_leaking(monkeypatch, capsys):
    """Finding 4: an empty-after-strip topic falls back to no-op and never
    prints a topic value."""
    from pydantic import SecretStr

    monkeypatch.setattr(config, "NOTIFY_SERVICE", "ntfy")
    monkeypatch.setattr(config, "NTFY_TOPIC", SecretStr("   "))
    assert type(get_notifier()) is Notifier
    assert "NTFY_TOPIC is empty" in capsys.readouterr().out


# --- review fixes: empty-run summary (finding 5, MEDIUM) --------------------

def test_post_all_empty_sends_no_summary(monkeypatch):
    """Finding 5: post_all([]) must not push a bogus '0/0 posted' summary."""
    monkeypatch.setattr(poster_browser, "post_all", _real_post_all)
    rec = _RecordingNotifier()
    results = asyncio.run(poster_browser.post_all([], notifier=rec))
    assert results == []
    assert rec.sent == []


# --- review fixes: manual-run wiring (finding 6, MEDIUM) --------------------

def test_manual_post_passes_notifier_to_post_all(client, tmp_media, monkeypatch):
    """Finding 6: main.py must actually wire get_notifier() into
    post_all_browser — deleting that kwarg would otherwise pass the suite. The
    fake captures the notifier kwarg and asserts it's a real Notifier."""
    import backend.main as main

    (tmp_media / "A_clip.mp4").write_bytes(b"x")
    captured = {}

    async def fake_post_all(slots, headless=None, progress_cb=None, notifier=None):
        captured["notifier"] = notifier
        return [PostResult(slot="A", ig_post_id="ig_ok", tt_post_id="tt_ok")]

    monkeypatch.setattr(main, "POST_MODE", "browser")
    monkeypatch.setattr(main, "post_all_browser", fake_post_all)
    resp = client.post("/api/post", json={
        "slots": [
            {"slot": "A", "filename": "A_clip.mp4", "caption": "hi", "media_type": "video"},
        ],
    })
    assert resp.status_code == 200
    assert captured["notifier"] is not None
    assert isinstance(captured["notifier"], Notifier)


# --- review fixes: unconfirmed summary accounting (finding 7, MEDIUM) -------

def test_summary_counts_unconfirmed_slot(monkeypatch):
    """Finding 7: an unconfirmed-but-error-free slot is not a plain success —
    the summary appends an unconfirmed count."""
    canned = {
        "A": PostResult(slot="A", ig_post_id="ig_unconfirmed_A", tt_post_id="tt_ok_A"),
        "B": PostResult(slot="B", ig_post_id="ig_ok_B", tt_post_id="tt_ok_B"),
        "C": PostResult(slot="C", ig_post_id="ig_ok_C", tt_post_id="tt_ok_C"),
    }
    rec = _RecordingNotifier()
    _run_post_all(monkeypatch, canned, rec)
    assert rec.sent[-1]["body"] == "3/3 posted, 1 unconfirmed"


# --- tripwire ---------------------------------------------------------------

def test_tripwire_blocks_unstubbed_ntfy_send():
    """The conftest tripwire must block an un-stubbed NtfyNotifier.send so no
    test can ever reach the real network."""
    n = NtfyNotifier(topic="t")
    with pytest.raises(AssertionError, match="tripwire: NtfyNotifier.send"):
        asyncio.run(n.send("a", "b"))
