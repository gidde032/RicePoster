"""Regressions for the Batch 1 API-contract issues.

- #33 — the two endpoints that bypassed Pydantic (`POST /api/generate-caption`,
  `PATCH /api/queue/{batch_id}`) plus the duplicated fire_time validation on
  `POST /api/queue`, unified into one shared validator.
- #10 — a successful reschedule returns the persisted batch instead of null.
- #34 — `_post_progress` lifecycle invariants are enforced by the object rather
  than remembered across a dict literal and a field-by-field reset.

Source: tech-debt audit 2026-07-29, findings BE-7 and BE-9.
"""

from datetime import datetime, timedelta, timezone

import pytest

import backend.main as main
from backend.main import PostProgress
from backend.models import CaptionRequest, RescheduleRequest


def _future_iso(hours=2):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _stub_queue_paths(monkeypatch, tmp_path):
    """Redirect the queue file and its media dir away from the live queue."""
    import backend.queue as queue_mod

    qf = tmp_path / "queue.jsonl"
    monkeypatch.setattr(queue_mod, "QUEUE_FILE", qf)
    monkeypatch.setattr(queue_mod, "QUEUE_MEDIA_DIR", tmp_path / "queue_media")
    monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_path / "media")
    return qf


# ---------------------------------------------------------------------------
# #33 — validation errors are 422s with a caller-readable detail
# ---------------------------------------------------------------------------

class TestValidationErrorShape:
    """The UI renders `err.detail` directly into an error message, so a
    list-of-dicts body would surface as "[object Object]". The flattening
    handler keeps `detail` a string on every endpoint."""

    def test_detail_is_a_string_not_a_list(self, client):
        resp = client.post("/api/generate-caption", data={"media_type": "hologram"})
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str), (
            "Pydantic's default list detail would render as [object Object] in the UI"
        )

    def test_detail_names_the_offending_field(self, client):
        resp = client.post("/api/generate-caption", data={"media_type": "hologram"})
        assert "media_type" in resp.json()["detail"]

    def test_missing_required_field_is_422_and_names_it(self, client):
        resp = client.post("/api/generate-caption", data={"topic": "no media type"})
        assert resp.status_code == 422
        assert "media_type" in resp.json()["detail"]


class TestGenerateCaptionForm:
    """The endpoint moved onto a request model but must keep accepting the
    multipart form the UI already sends — no frontend change was in scope."""

    def test_accepts_the_multipart_form_the_ui_sends(self, client, monkeypatch):
        captured = {}

        async def fake_generate(media_type, topic, style, avoid, feedback,
                                thumbnail_b64=""):
            captured.update(
                media_type=media_type, topic=topic, style=style,
                avoid=avoid, feedback=feedback,
            )
            return "a caption"

        monkeypatch.setattr(main, "generate_caption", fake_generate)
        resp = client.post("/api/generate-caption", data={
            "media_type": "video",
            "topic": "rice",
            "style": "generic",
            "avoid_caption": "old one",
            "feedback": "shorter",
        })
        assert resp.status_code == 200
        assert resp.json() == {"caption": "a caption"}
        assert captured == {
            "media_type": "video", "topic": "rice", "style": "generic",
            "avoid": "old one", "feedback": "shorter",
        }

    def test_omitted_style_resolves_to_the_default(self, client, monkeypatch):
        """Previously Form(DEFAULT_STYLE) supplied this default; the model uses
        None as "not supplied" so the endpoint must restore it."""
        captured = {}

        async def fake_generate(media_type, topic, style, *a, **kw):
            captured["style"] = style
            return "c"

        monkeypatch.setattr(main, "generate_caption", fake_generate)
        resp = client.post("/api/generate-caption", data={"media_type": "image"})
        assert resp.status_code == 200, resp.json()
        assert captured["style"] == main.DEFAULT_STYLE

    def test_explicitly_empty_style_is_still_passed_through(self, client, monkeypatch):
        """Pre-existing behaviour: an empty style is rejected downstream by
        generate_caption as an unknown style, not silently defaulted."""
        captured = {}

        async def fake_generate(media_type, topic, style, *a, **kw):
            captured["style"] = style
            return "c"

        monkeypatch.setattr(main, "generate_caption", fake_generate)
        resp = client.post(
            "/api/generate-caption", data={"media_type": "image", "style": ""}
        )
        assert resp.status_code == 200, resp.json()
        assert captured["style"] == ""

    @pytest.mark.parametrize("media_type", ["video", "image"])
    def test_known_media_types_validate(self, media_type):
        assert CaptionRequest(media_type=media_type).media_type == media_type

    def test_unknown_media_type_is_rejected_by_the_model(self):
        with pytest.raises(ValueError, match="media_type must be one of"):
            CaptionRequest(media_type="hologram")


class TestSharedFireTimeValidator:
    """Both queue endpoints validated fire_time by hand, in duplicate. One
    shared validator means they cannot drift apart."""

    def test_reschedule_rejects_naive_fire_time(self):
        with pytest.raises(ValueError, match="timezone offset"):
            RescheduleRequest(fire_time="2099-12-01T09:00:00")

    def test_reschedule_rejects_a_past_fire_time(self):
        with pytest.raises(ValueError, match="at least 1 minute"):
            RescheduleRequest(fire_time=_future_iso(hours=-2))

    def test_both_endpoints_reject_naive_with_the_same_message(
        self, client, tmp_media, monkeypatch, tmp_path
    ):
        _stub_queue_paths(monkeypatch, tmp_path)
        naive = "2099-12-01T09:00:00"

        post = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "x.mp4", "caption": "c",
                       "media_type": "video"}],
            "fire_time": naive,
        })
        patch = client.patch("/api/queue/whatever", json={"fire_time": naive})

        assert post.status_code == patch.status_code == 422
        assert "timezone offset" in post.json()["detail"]
        assert "timezone offset" in patch.json()["detail"]

    def test_schedule_endpoint_detail_is_also_a_string(
        self, client, tmp_media, monkeypatch, tmp_path
    ):
        """The flattening handler is app-wide, so endpoints beyond the two named
        in #33 inherit the string-detail contract. Asserted here because the
        handler's scope was wider than the issue's."""
        _stub_queue_paths(monkeypatch, tmp_path)
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "x.mp4", "caption": "c",
                       "media_type": "video"}],
            "fire_time": "not-a-datetime",
        })
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)
        assert "fire_time" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# #10 — a successful reschedule returns the persisted batch
# ---------------------------------------------------------------------------

class TestRescheduleReturnsBatch:

    def _schedule(self, client, tmp_media, monkeypatch, tmp_path):
        import backend.queue as queue_mod

        _stub_queue_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(queue_mod, "MEDIA_DIR", tmp_media)
        (tmp_media / "A_clip.mp4").write_bytes(b"video")
        resp = client.post("/api/queue", json={
            "slots": [{"slot": "A", "filename": "A_clip.mp4", "caption": "c",
                       "media_type": "video"}],
            "fire_time": _future_iso(2),
        })
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_returns_the_updated_batch_and_normalized_fire_time(
        self, client, tmp_media, monkeypatch, tmp_path
    ):
        batch_id = self._schedule(client, tmp_media, monkeypatch, tmp_path)
        new_ft = _future_iso(5)

        resp = client.patch(f"/api/queue/{batch_id}", json={"fire_time": new_ft})

        assert resp.status_code == 200
        body = resp.json()
        assert body is not None, "a successful reschedule returned a null body"
        assert body["id"] == batch_id
        assert body["status"] == "pending"
        # Normalized: parsed and re-serialized by the queue layer, so it is
        # comparable as an instant rather than byte-identical to the request.
        assert datetime.fromisoformat(body["fire_time"]) == datetime.fromisoformat(new_ft)
        assert [s["slot"] for s in body["slots"]] == ["A"]

    def test_persisted_state_matches_the_returned_body(
        self, client, tmp_media, monkeypatch, tmp_path
    ):
        from backend.queue import load_queue

        batch_id = self._schedule(client, tmp_media, monkeypatch, tmp_path)
        new_ft = _future_iso(6)
        body = client.patch(
            f"/api/queue/{batch_id}", json={"fire_time": new_ft}
        ).json()

        stored = next(b for b in load_queue() if b.id == batch_id)
        assert stored.to_dict() == body, (
            "the response must report what was persisted, not the request echo"
        )

    def test_unknown_batch_is_still_404(self, client, monkeypatch, tmp_path):
        _stub_queue_paths(monkeypatch, tmp_path)
        resp = client.patch("/api/queue/nope", json={"fire_time": _future_iso(3)})
        assert resp.status_code == 404

    def test_queue_layer_value_error_is_422_not_500(
        self, client, monkeypatch, tmp_path
    ):
        """The queue layer re-validates fire_time independently of the model
        (> now, vs the model's > now + 1 minute). An uncaught ValueError there
        surfaced as a bodiless 500; schedule_batch already guarded its
        equivalent call, so the two endpoints now agree.

        Reported by two independent reviewers on PR #39.
        """
        _stub_queue_paths(monkeypatch, tmp_path)

        def _raise(*args, **kwargs):
            raise ValueError("fire_time must be in the future")

        monkeypatch.setattr(main, "update_fire_time", _raise)
        resp = client.patch("/api/queue/any", json={"fire_time": _future_iso(3)})
        assert resp.status_code == 422
        assert "future" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# #34 — the progress object owns its lifecycle rules
# ---------------------------------------------------------------------------

class TestPostProgressLifecycle:
    """The reset list used to be kept in sync with a dict literal by hand.
    These pin the two rules that duplication hid."""

    def test_start_clears_the_previous_runs_events(self):
        p = PostProgress()
        p.events.append({"slot": "Z", "platform": "x", "status": "stale", "detail": ""})
        p.start()
        assert p.active is True
        assert p.events == []
        assert p.current is None
        assert p.waiting is None

    def test_finish_keeps_events_for_the_most_recent_run_view(self):
        """The UI renders the last run's per-slot results after it ends, so
        finish() must not clear events — only start() may."""
        p = PostProgress()
        p.start()
        p.events.append({"slot": "A", "platform": "instagram", "status": "ok",
                         "detail": ""})
        p.finish()
        assert p.active is False
        assert p.events == [{"slot": "A", "platform": "instagram", "status": "ok",
                             "detail": ""}]

    def test_finish_clears_a_countdown_so_it_cannot_go_stale(self):
        p = PostProgress()
        p.start()
        p.waiting = {"slot": "B", "seconds": 90, "at": 0}
        p.finish()
        assert p.waiting is None

    def test_to_dict_exposes_exactly_the_polled_fields(self):
        """The /api/post-progress payload is built from this, so a new field
        has to be added here deliberately rather than leaking."""
        assert set(PostProgress().to_dict()) == {
            "active", "current", "events", "waiting",
        }

    def test_a_fresh_object_is_idle(self):
        p = PostProgress()
        assert (p.active, p.current, p.events, p.waiting) == (False, None, [], None)

    def test_events_are_not_shared_between_instances(self):
        a = PostProgress()
        a.events.append({"slot": "A"})
        assert PostProgress().events == []
