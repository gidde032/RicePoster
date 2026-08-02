"""Unit tests for backend/models.py Pydantic models."""

from backend.models import CaptionRequest, PostResult


def test_post_result_defaults():
    r = PostResult(slot="A")
    assert r.ig_post_id == ""
    assert r.tt_post_id == ""
    assert r.errors == []


def test_post_result_success_property():
    assert PostResult(slot="A").success is True
    assert PostResult(slot="A", errors=["IG post: boom"]).success is False


def test_post_result_errors_not_shared_between_instances():
    a = PostResult(slot="A")
    a.errors.append("oops")
    b = PostResult(slot="B")
    assert b.errors == []


def test_caption_request_topic_optional():
    req = CaptionRequest(media_type="video")
    assert req.topic == ""
