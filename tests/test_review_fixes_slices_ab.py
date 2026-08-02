"""Regression tests for the 2026-07-26 reviewer pass on Slices A+B and F1-F6.

Three contextless reviewers (skeptical senior engineer; concurrency /
state-integrity specialist; platform-detection / fingerprint specialist) ran
against the 11 commits `6ae811a`..`184dfa5`, none of which had been reviewed.
Two findings were accepted for fixing; both live here. Findings are numbered as
in that pass's triage table.

- R1 / finding #2 (skeptical senior engineer, CRITICAL): execute_batch ran the
  pre-flight health check per platform and notified "slot X <platform>
  skipped", then handed the slot to post_all with the skip decision thrown
  away. post_slot re-derived platform availability from session_exists(), which
  is a filesystem-existence check and cannot see that a profile is expired — so
  the "skipped" platform was posted to anyway. Two harms: the notification
  lied, and a live account absorbed an automated load that pre-flight had
  already established was pointless. Half-regression of the Slice-3 review fix
  the comment at scheduler.py:111 claims is complete.

- R2 / finding #3 (concurrency / state-integrity specialist, HIGH):
  _append_scheduled_history swallowed every write failure with a print, and
  _prune_batch then ran unconditionally. If the history write failed, the batch
  was erased from queue.jsonl with no history row and no surviving "running"
  marker, so startup_sweep found nothing and notified nobody. That voids
  exactly the audit-trail guarantee Slice A's A2 fix advertises.

These tests live in their own file rather than in test_audit_phase1.py because
they record a *different* review round; a future reader grepping for the
2026-07-26 pass should land here.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend import poster_browser
from backend.models import PostResult
from backend.notifier import Notifier
from backend.queue import QueuedBatch, SlotBatch, _snapshot_media, load_queue, save_queue
from backend.scheduler import execute_batch

PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title="", body="", priority="default"):
        self.sent.append({"title": title, "body": body, "priority": priority})
        return True


def _env(tmp_path, slot_ids=("A",)):
    """A single-batch queue on disk plus the paths execute_batch needs."""
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    qf = tmp_path / "queue.jsonl"
    qmedia = tmp_path / "queue_media"
    hf = tmp_path / "history.jsonl"

    raw = []
    for sid in slot_ids:
        fname = f"{sid}_clip.mp4"
        (media / fname).write_bytes(b"video")
        raw.append(SlotBatch(slot=sid, media_path=fname,
                             caption=f"cap_{sid}"))
    snapped = _snapshot_media("batch1", raw, media, qmedia)
    batch = QueuedBatch(
        id="batch1", fire_time=PAST, created_at=datetime.now(timezone.utc),
        slots=snapped, status="pending", headless=True,
    )
    save_queue([batch], qf)
    return batch, qf, qmedia, hf, _FakeNotifier()


# ---------------------------------------------------------------------------
# R1 / finding #2 — the pre-flight skip must reach the poster
# ---------------------------------------------------------------------------

class TestPreflightSkipReachesPoster:

    def test_expired_platform_is_carried_into_post_fn(self, tmp_path):
        """The CRITICAL: pre-flight said instagram was expired and the
        scheduler notified "skipped", but post_fn was handed a payload with no
        record of it and posted to instagram anyway."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)

        async def check_fn(slot, platform):
            return "expired" if platform == "instagram" else "live"

        received = []

        async def post_fn(slots, headless=True, notifier=None):
            received.extend(slots)
            return [PostResult(slot=s["slot"], tt_post_id="tt") for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        assert len(received) == 1, "the live platform means the slot proceeds"
        assert "instagram" in received[0].get("skip_platforms", set()), (
            "pre-flight found instagram expired and notified the maintainer "
            "that it was skipped; post_fn must be told, or it will post anyway"
        )
        assert "tiktok" not in received[0].get("skip_platforms", set())

    def test_slot_with_no_skips_carries_an_empty_skip_set(self, tmp_path):
        """The all-healthy path must not acquire a spurious skip."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)

        async def check_fn(slot, platform):
            return "live"

        received = []

        async def post_fn(slots, headless=True, notifier=None):
            received.extend(slots)
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                    for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        assert not received[0].get("skip_platforms"), \
            "a fully healthy slot must carry no skips"

    def test_post_slot_honours_skip_platforms_despite_session_on_disk(
            self, tmp_path, monkeypatch):
        """post_slot must not re-derive availability from the filesystem when
        pre-flight already ruled a platform out.

        session_exists() only asks whether the profile directory is present, so
        it returns True for an expired session — that is the documented reason
        the pre-flight check exists at all.
        """
        monkeypatch.setattr(poster_browser, "session_exists",
                            lambda platform, slot: True)

        called = []

        async def fake_ig(**kw):
            called.append("instagram")
            return "ig123"

        async def fake_tt(**kw):
            called.append("tiktok")
            return "tt123"

        monkeypatch.setattr(poster_browser.instagram_browser, "post_media", fake_ig)
        monkeypatch.setattr(poster_browser.tiktok_browser, "post_media", fake_tt)

        media = tmp_path / "clip.mp4"
        media.write_bytes(b"video")

        result = asyncio.run(poster_browser.post_slot(
            slot="A", media_path=media, caption="c", media_type="video",
            skip_platforms={"instagram"},
        ))

        assert called == ["tiktok"], (
            "instagram was ruled out by pre-flight; posting to it anyway "
            "spends a real automated load on an account already known bad"
        )
        assert result.tt_post_id == "tt123"
        assert any("skipped" in e for e in result.errors)

    def test_preflight_skip_is_classified_as_a_skip_not_a_failure(self):
        """_is_skip drives the run summary and suppresses failure pushes. A
        pre-flight skip is already notified by the scheduler, so it must not
        also be counted as a failed platform."""
        assert poster_browser._is_skip(
            "IG post: skipped (pre-flight: expired)")
        assert not poster_browser._is_skip("IG post: something genuinely broke")


# ---------------------------------------------------------------------------
# R2 / finding #3 — a failed history write must not erase the evidence
# ---------------------------------------------------------------------------

class TestHistoryWriteFailureKeepsEvidence:

    @staticmethod
    def _break_history(monkeypatch):
        """Make the history append fail the way a full or read-only disk would."""
        import backend.scheduler as sched
        real_open = open

        def exploding_open(path, mode="r", *a, **kw):
            if "history" in str(path) and "a" in mode:
                raise OSError("No space left on device")
            return real_open(path, mode, *a, **kw)

        monkeypatch.setattr(sched, "open", exploding_open, raising=False)

    def test_crash_path_keeps_the_batch_when_history_cannot_be_written(
            self, tmp_path, monkeypatch):
        """The HIGH: a crash plus a failed history write erased the batch.

        The 'running' record on disk is the only thing startup_sweep can turn
        into an 'interrupted' notification. Pruning it after the history write
        failed leaves a possibly-live post with no trace anywhere.
        """
        batch, qf, qmedia, hf, notifier = _env(tmp_path)
        self._break_history(monkeypatch)

        async def check_fn(slot, platform):
            return "live"

        async def post_fn(slots, headless=True, notifier=None):
            raise RuntimeError("browser died mid-post")

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        remaining = load_queue(qf)
        assert remaining, (
            "history write failed, so the queue record is the only surviving "
            "evidence — it must not be pruned"
        )
        assert (qmedia / "batch1").exists(), \
            "the media snapshot is the other half of the evidence"

    def test_success_path_keeps_the_batch_when_history_cannot_be_written(
            self, tmp_path, monkeypatch):
        """Same guarantee on the clean path: posts happened, and if they were
        not recorded the queue entry is the only proof."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)
        self._break_history(monkeypatch)

        async def check_fn(slot, platform):
            return "live"

        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                    for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        assert load_queue(qf), \
            "a successful post whose history write failed must stay on disk"
        assert (qmedia / "batch1").exists()

    def test_retained_batch_is_never_re_executed(self, tmp_path, monkeypatch):
        """Retaining the record must not create a double-post: only 'pending'
        batches are ever picked up, and a completed one is not pending."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)
        self._break_history(monkeypatch)

        async def check_fn(slot, platform):
            return "live"

        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                    for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        retained = load_queue(qf)[0]
        assert retained.status != "pending", (
            f"retained batch is {retained.status!r}; a 'pending' one would be "
            "re-fired by the next poll and post twice"
        )

    def test_history_write_success_still_prunes(self, tmp_path):
        """The fix must not leak queue entries on the normal path."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)

        async def check_fn(slot, platform):
            return "live"

        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                    for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        assert load_queue(qf) == [], "a recorded batch must still be pruned"
        assert not (qmedia / "batch1").exists(), \
            "a recorded batch's snapshot must still be deleted"


# =========================================================================
# Cluster A — config surface (findings #4, #5, #7, #8)
# =========================================================================

import pathlib
import re
import subprocess
import sys

from backend import config, device_identity
from backend.config import parse_preflight_platforms
from backend.device_identity import VIEWPORTS, _validate_capacity, viewport_for_slot

_ROOT = pathlib.Path(__file__).parent.parent


def _run_import(module: str, env_overrides: dict) -> subprocess.CompletedProcess:
    """Import a backend module in a fresh interpreter under given env vars.

    Import-time guards can only be proven by actually importing. Doing it in
    a subprocess rather than via importlib.reload keeps a deliberately
    broken config from leaking into the rest of the session.
    """
    env = {"PATH": "", "PYTHONHASHSEED": "0", **env_overrides}
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, env=env, cwd=str(_ROOT),
    )


class TestSlotCountCannotExceedFingerprints:
    """R3 / finding #4 (platform-detection specialist, HIGH).

    ACCOUNT_SLOTS is user-configurable; VIEWPORTS has 5 entries; the
    positional index wrapped via `% len(VIEWPORTS)`. So configuring a 6th
    slot silently handed it slot A's exact viewport, rebuilding the shared
    device fingerprint F3 exists to destroy — with no local symptom, only an
    account ban weeks later.

    The pre-existing distinctness test cannot catch this: it iterates only
    *currently configured* slots, so it still passes once the collision
    exists. These tests deliberately test at and beyond capacity instead.
    """

    def test_more_slots_than_viewports_is_rejected(self):
        too_many = [f"S{i}" for i in range(len(VIEWPORTS) + 1)]
        with pytest.raises(ValueError) as exc:
            _validate_capacity(too_many)
        msg = str(exc.value)
        assert str(len(too_many)) in msg and str(len(VIEWPORTS)) in msg, (
            f"The error must name both counts so the maintainer knows how "
            f"many viewports to add. Got: {msg}"
        )
        assert "ACCOUNT_SLOTS" in msg, "The error must name the knob to change"

    def test_exactly_full_capacity_is_allowed(self):
        _validate_capacity([f"S{i}" for i in range(len(VIEWPORTS))])

    def test_viewports_are_distinct_at_full_capacity(self, monkeypatch):
        """The gap the existing test leaves open. Distinctness must hold for
        the largest slot list the guard permits, not just for A/B/C."""
        full = [f"S{i}" for i in range(len(VIEWPORTS))]
        monkeypatch.setattr(device_identity, "SLOT_IDS", full)
        seen = [tuple(sorted(viewport_for_slot(s).items())) for s in full]
        assert len(set(seen)) == len(full), (
            f"At full capacity ({len(full)} slots) the viewports collide: "
            f"{seen}. Accounts sharing a fingerprint is the ban condition."
        )

    def test_guard_runs_at_import_not_just_as_a_helper(self):
        """A validator nobody calls is decoration. Prove the real import
        path refuses a 6-slot config."""
        out = _run_import(
            "backend.device_identity",
            {"ACCOUNT_SLOTS": ",".join(f"S{i}" for i in range(len(VIEWPORTS) + 1))},
        )
        assert out.returncode != 0, (
            "Importing device_identity with more slots than viewports "
            "succeeded — the capacity guard is not wired into import."
        )
        assert "ACCOUNT_SLOTS" in out.stderr

    def test_a_normal_config_still_imports(self):
        out = _run_import("backend.device_identity", {"ACCOUNT_SLOTS": "A,B,C"})
        assert out.returncode == 0, out.stderr


class TestPreflightPlatformParsing:
    """R4 / finding #5 (skeptical senior engineer, MEDIUM).

    `PREFLIGHT_CHECK_PLATFORMS=` parsed to an empty frozenset, so
    check_session returned "check_disabled" for every platform: one blank
    line in credentials.env silently disabled the entire pre-flight check.
    Empty now means unset (matching parse_slot_ids); "none" is the explicit
    off switch.
    """

    def test_empty_falls_back_to_the_default(self):
        assert parse_preflight_platforms("") == frozenset(
            config.PREFLIGHT_PLATFORMS_DEFAULT)

    def test_whitespace_and_stray_commas_fall_back_to_the_default(self):
        for raw in ("   ", ",", " , , "):
            assert parse_preflight_platforms(raw) == frozenset(
                config.PREFLIGHT_PLATFORMS_DEFAULT), f"{raw!r} disabled the check"

    def test_none_is_the_explicit_off_switch(self):
        for raw in ("none", "NONE", "  None  "):
            assert parse_preflight_platforms(raw) == frozenset()

    def test_a_single_platform_is_honoured(self):
        assert parse_preflight_platforms("tiktok") == frozenset({"tiktok"})
        assert parse_preflight_platforms(" TikTok , ") == frozenset({"tiktok"})

    def test_none_mixed_with_platforms_is_rejected(self):
        with pytest.raises(ValueError, match="PREFLIGHT_CHECK_PLATFORMS"):
            parse_preflight_platforms("none,tiktok")

    def test_a_typo_is_rejected_rather_than_silently_disabling(self):
        """'instgram' would otherwise drop Instagram's check silently — the
        same failure class as the empty string this fix addresses."""
        with pytest.raises(ValueError) as exc:
            parse_preflight_platforms("instgram,tiktok")
        assert "instgram" in str(exc.value)

    def test_the_shipped_default_checks_both_platforms(self):
        assert config.PREFLIGHT_CHECK_PLATFORMS, (
            "Pre-flight is disabled in the running config — if that is "
            "deliberate it must be set explicitly, not by an empty value."
        )


class TestNumericEnvKnobsFailLoudly:
    """R5 / finding #8 (skeptical senior engineer, MEDIUM).

    Bare int()/float() casts meant a typo'd value killed the server at
    import with "invalid literal for int() with base 10: '6h'" — naming
    neither the variable nor the file, before any logging existed.
    """

    def test_bad_int_names_the_variable_and_the_value(self, monkeypatch):
        monkeypatch.setenv("SESSION_CHECK_TTL_S", "6h")
        with pytest.raises(ValueError) as exc:
            config._env_int("SESSION_CHECK_TTL_S", 21600)
        msg = str(exc.value)
        assert "SESSION_CHECK_TTL_S" in msg and "6h" in msg
        assert "21600" in msg, "The message should state the default"

    def test_bad_float_names_the_variable_and_the_value(self, monkeypatch):
        monkeypatch.setenv("INTER_SLOT_DELAY_MIN_S", "two minutes")
        with pytest.raises(ValueError) as exc:
            config._env_float("INTER_SLOT_DELAY_MIN_S", 60.0)
        assert "INTER_SLOT_DELAY_MIN_S" in str(exc.value)
        assert "two minutes" in str(exc.value)

    def test_unset_and_empty_use_the_default(self, monkeypatch):
        monkeypatch.delenv("SESSION_CHECK_TTL_S", raising=False)
        assert config._env_int("SESSION_CHECK_TTL_S", 42) == 42
        monkeypatch.setenv("SESSION_CHECK_TTL_S", "   ")
        assert config._env_int("SESSION_CHECK_TTL_S", 42) == 42

    def test_valid_values_still_parse(self, monkeypatch):
        monkeypatch.setenv("SESSION_CHECK_TTL_S", " 900 ")
        assert config._env_int("SESSION_CHECK_TTL_S", 42) == 900
        monkeypatch.setenv("INTER_SLOT_DELAY_MIN_S", "1.5")
        assert config._env_float("INTER_SLOT_DELAY_MIN_S", 0.0) == 1.5

    @pytest.mark.parametrize("var,bad", [
        ("SESSION_CHECK_TTL_S", "6h"),
        ("INTER_SLOT_DELAY_MIN_S", "one"),
        ("INTER_SLOT_DELAY_MAX_S", "3m"),
    ])
    def test_each_knob_is_actually_wired_to_the_validator(self, var, bad):
        """Import-level proof, per knob. A helper the knobs don't use is
        worthless, and this is the failure the finding describes."""
        out = _run_import("backend.config", {var: bad})
        assert out.returncode != 0, f"{var}={bad!r} imported cleanly"
        assert var in out.stderr, (
            f"Import failed but the error never names {var}:\n{out.stderr[-600:]}"
        )


class TestEveryConfigKnobIsDocumented:
    """R6 / finding #7 (user-friction, MEDIUM).

    SESSION_CHECK_TTL_S, PREFLIGHT_CHECK_PLATFORMS and INTER_SLOT_DELAY_*
    were env-only with no UI and appeared in no documentation, so the F5
    opt-out and F4 spacing switch were undiscoverable. Asserted against the
    live getenv list rather than a hardcoded roster, so the next knob added
    without docs fails here (contracts, not conventions).
    """

    ENV_EXAMPLE = _ROOT / "credentials.env.example"
    README = _ROOT / "README.md"

    def _knobs(self):
        src = (_ROOT / "backend" / "config.py").read_text()
        return sorted(set(re.findall(r'os\.getenv\(\s*"([A-Z_]+)"', src)))

    def test_the_template_is_present_and_not_gitignored(self):
        """credentials.env.example was gitignored alongside credentials.env
        until 2026-07-26, so a fresh clone got a README pointing at a file it
        did not have. This test fails if it is ever re-ignored and removed."""
        assert self.ENV_EXAMPLE.exists(), (
            "credentials.env.example is missing. It is a placeholder-only "
            "template that README tells the reader to copy — it must ship."
        )

    def test_every_knob_appears_in_the_env_template(self):
        text = self.ENV_EXAMPLE.read_text()
        missing = [k for k in self._knobs() if k not in text]
        assert not missing, (
            f"config.py reads {missing} but credentials.env.example never "
            f"mentions them, so they are undiscoverable. Add them (commented "
            f"out, with the default) to the template."
        )

    def test_every_knob_appears_in_the_readme(self):
        text = self.README.read_text()
        missing = [k for k in self._knobs() if k not in text]
        assert not missing, (
            f"config.py reads {missing} but README.md's configuration table "
            f"never mentions them. README is the only config doc a fresh "
            f"clone is guaranteed to have."
        )


# =========================================================================
# Cluster B — partial-batch notification (finding #6)
# =========================================================================

class TestPartialBatchStillReachesTheMaintainer:
    """R7 / finding #6 (skeptical senior engineer, MEDIUM) — investigated,
    resolved as *already covered*, then pinned.

    The finding reads: "`partial` batches send no notification — only
    `failed` does (scheduler.py:165)." The scheduler half is accurate, but
    the conclusion ("the maintainer isn't told") is not. Notification is
    deliberately two-layer:

      * `poster_browser.post_all` owns the *run summary* and pushes
        "2/3 posted, 1 failed: [...]" at high priority for exactly the mixed
        outcome that produces `partial`. `execute_batch` passes its notifier
        straight through, so a scheduled run gets it too.
      * `scheduler.execute_batch` owns only what the poster cannot see:
        aborts, pre-flight skips, whole-batch failure and crashes.

    So adding a batch-level `partial` push would double-notify on every
    partial. No code change was made (maintainer decision 2026-07-26). What
    was missing is that *nothing guarded the guarantee* — these two tests do,
    so a future refactor that drops the run summary or stops threading the
    notifier fails here instead of going silent.

    The residual gap this finding surfaced — `partial` deleting its media
    snapshot while the crash path retained it — was fixed separately on
    2026-07-27; see TestOnlyAFullySuccessfulBatchLosesItsMedia below.
    """

    def test_poster_pushes_a_high_priority_summary_on_a_partial_run(self):
        """The mixed outcome that produces final_status == "partial"."""
        notifier = _FakeNotifier()
        results = [
            PostResult(slot="A", ig_post_id="ig_a", tt_post_id="tt_a"),
            PostResult(slot="B", ig_post_id="ig_b", tt_post_id="tt_b"),
            PostResult(slot="C", errors=["TT upload failed: session expired"]),
        ]
        asyncio.run(poster_browser._notify_run_summary(notifier, results))

        assert len(notifier.sent) == 1, (
            f"a partial run must push exactly one summary, got {notifier.sent}"
        )
        push = notifier.sent[0]
        assert push["priority"] == "high", (
            "a run where a slot failed must not arrive at default priority"
        )
        assert "2/3 posted" in push["body"], push["body"]
        assert "C" in push["body"], (
            f"the summary must name the failed slot: {push['body']}"
        )

    def test_scheduler_threads_its_notifier_into_the_poster(self, tmp_path):
        """The wiring the guarantee above rests on. If execute_batch ever
        stops passing `notifier` through, post_all falls back to
        get_notifier() and a scheduled partial goes unreported."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)

        async def check_fn(slot, platform):
            return "live"

        seen = {}

        async def post_fn(slots, headless=True, notifier=None):
            seen["notifier"] = notifier
            return [PostResult(slot=s["slot"], ig_post_id="ig") for s in slots]

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))

        assert seen["notifier"] is notifier, (
            "execute_batch did not hand its notifier to the poster, so the "
            "run summary for a scheduled batch would go to a different sink"
        )

    def test_a_clean_run_stays_at_default_priority(self):
        """Guards against 'fix' by making everything high priority, which
        would train the maintainer to ignore the pushes."""
        notifier = _FakeNotifier()
        results = [PostResult(slot="A", ig_post_id="ig", tt_post_id="tt")]
        asyncio.run(poster_browser._notify_run_summary(notifier, results))
        assert notifier.sent[0]["priority"] == "default"


# =========================================================================
# Cluster C — test hygiene (#10; #1 and #9 live with the code they guard)
# =========================================================================

class TestNoOpStatusUpdateDoesNotRewriteTheQueue:
    """R8 / finding #10 (concurrency / state-integrity specialist, LOW).

    `_update_status` called `save_queue` even when no batch matched. No
    correctness impact — the rewrite is atomic and the contents are
    unchanged — but it is a full-file rewrite triggered by a lookup miss,
    on a path reached exactly when the on-disk state is already confusing.

    Findings #1 and #9 are guarded in the files they belong to
    (test_health_cache.py's AST body extraction, test_jitter.py's
    session_manager sleep guard) rather than here, so a reader looking at
    those modules finds their contracts next to them.
    """

    def test_unknown_batch_id_writes_nothing(self, tmp_path, monkeypatch):
        batch, qf, qmedia, hf, notifier = _env(tmp_path)
        calls = []
        monkeypatch.setattr(
            "backend.scheduler.save_queue",
            lambda batches, path: calls.append(path))

        from backend.scheduler import _update_status
        assert _update_status("no-such-batch", "running", qf) is False
        assert calls == [], (
            "a lookup miss rewrote the whole queue file for no reason"
        )

    def test_a_real_update_still_persists(self, tmp_path):
        """The guard must not be bought by breaking the working path."""
        batch, qf, qmedia, hf, notifier = _env(tmp_path)
        from backend.scheduler import _update_status
        assert _update_status("batch1", "running", qf) is True
        assert load_queue(qf)[0].status == "running"


# =========================================================================
# Snapshot retention on non-done terminal statuses (2026-07-27)
# =========================================================================

class TestOnlyAFullySuccessfulBatchLosesItsMedia:
    """Follow-up to finding #6's residual gap (maintainer decision 2026-07-27).

    `execute_batch` deleted the media snapshot on every terminal status that
    recorded history, so a `partial` or `failed` batch destroyed the media of
    the very slots that had not posted. history.jsonl records *that* a slot
    failed, never the file — so retrying meant re-uploading, and on a partial
    the maintainer might not notice until the snapshot was already gone.

    The rule is now statable in one line: a batch loses its media only when
    every slot posted successfully. That matches the crash path, which has
    always retained its snapshot as forensic evidence.

    Cost, accepted deliberately: retained snapshots accumulate in
    queue_media/ with no UI affordance to clear them (the queue panel renders
    only pending and interrupted batches, and these records are pruned).
    Manual cleanup until the C1 reconciliation pass lands.
    """

    def _run(self, tmp_path, post_fn):
        batch, qf, qmedia, hf, notifier = _env(tmp_path, slot_ids=("A", "B"))
        assert (qmedia / "batch1").exists(), "fixture should start with a snapshot"

        async def check_fn(slot, platform):
            return "live"

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))
        return qmedia, hf

    def test_partial_keeps_the_snapshot(self, tmp_path):
        async def post_fn(slots, headless=True, notifier=None):
            return [
                PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                if s["slot"] == "A" else
                PostResult(slot=s["slot"], errors=["TT upload failed"])
                for s in slots
            ]

        qmedia, hf = self._run(tmp_path, post_fn)
        assert (qmedia / "batch1").exists(), (
            "slot B never posted and its media was deleted anyway — history "
            "records the failure but not the file, so a retry needs a "
            "re-upload"
        )

    def test_failed_keeps_the_snapshot(self, tmp_path):
        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], errors=["boom"]) for s in slots]

        qmedia, hf = self._run(tmp_path, post_fn)
        assert (qmedia / "batch1").exists(), (
            "no slot posted at all and the media was still destroyed"
        )

    def test_done_still_deletes_the_snapshot(self, tmp_path):
        """The retention must not become unconditional — a clean run has no
        forensic value and would otherwise grow queue_media/ without bound."""
        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], ig_post_id="ig", tt_post_id="tt")
                    for s in slots]

        qmedia, hf = self._run(tmp_path, post_fn)
        assert not (qmedia / "batch1").exists(), (
            "a fully successful batch must still clean up after itself"
        )

    def test_retention_does_not_resurrect_the_queue_record(self, tmp_path):
        """Keeping media must not keep the batch schedulable. A retained
        queue record with status 'pending' would re-fire and double-post."""
        async def post_fn(slots, headless=True, notifier=None):
            return [PostResult(slot=s["slot"], errors=["boom"]) for s in slots]

        batch, qf, qmedia, hf, notifier = _env(tmp_path, slot_ids=("A", "B"))

        async def check_fn(slot, platform):
            return "live"

        asyncio.run(execute_batch(batch, qf, qmedia, hf, notifier,
                                  check_fn, post_fn))
        assert load_queue(qf) == [], (
            "the queue record must still be pruned — only the media is kept"
        )
        assert (qmedia / "batch1").exists()
