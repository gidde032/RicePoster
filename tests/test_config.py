"""Unit tests for backend/config.py env loading and account config.

These read the real credentials.env via the already-imported module, but
assert only structure — never secret values.
"""

import pytest

from backend import config


@pytest.mark.smoke
def test_get_accounts_matches_configured_slots():
    """One account per configured slot id, in order. With ACCOUNT_SLOTS
    unset this is the historical A/B/C default."""
    accounts = config.get_accounts()
    assert [a.slot for a in accounts] == config.SLOT_IDS
    assert len(accounts) >= 1


def test_account_slot_fields_typed():
    from pydantic import SecretStr

    for a in config.get_accounts():
        assert isinstance(a.display_name, str) and a.display_name
        assert isinstance(a.ig_user_id, str)
        assert isinstance(a.ig_token, SecretStr)
        assert isinstance(a.tt_token, SecretStr)


def test_post_mode_is_a_known_mode():
    assert config.POST_MODE in ("mock", "browser", "api")


def test_mock_mode_matches_post_mode():
    assert config.MOCK_MODE == (config.POST_MODE == "mock")


def test_media_dir_exists():
    assert config.MEDIA_DIR.is_dir()


class TestSlotDisplayName:
    """_slot_display_name: LABEL file → env var → slot letter fallback chain."""

    def test_label_file_takes_priority(self, tmp_path, monkeypatch):
        label_dir = tmp_path / "A"
        label_dir.mkdir()
        (label_dir / "LABEL").write_text("@burner_account\n")
        monkeypatch.setattr(config, "IG_SESSIONS_DIR", tmp_path)
        monkeypatch.setenv("IG_ACCOUNT_A_NAME", "Studio Page A")
        assert config._slot_display_name("A") == "@burner_account"

    def test_falls_back_to_env_when_no_label_file(self, tmp_path, monkeypatch):
        (tmp_path / "A").mkdir()
        monkeypatch.setattr(config, "IG_SESSIONS_DIR", tmp_path)
        monkeypatch.setenv("IG_ACCOUNT_A_NAME", "Studio Page A")
        assert config._slot_display_name("A") == "Studio Page A"

    def test_falls_back_to_slot_letter_when_no_env(self, tmp_path, monkeypatch):
        (tmp_path / "A").mkdir()
        monkeypatch.setattr(config, "IG_SESSIONS_DIR", tmp_path)
        monkeypatch.delenv("IG_ACCOUNT_A_NAME", raising=False)
        assert config._slot_display_name("A") == "Account A"

    def test_empty_label_file_skipped(self, tmp_path, monkeypatch):
        label_dir = tmp_path / "A"
        label_dir.mkdir()
        (label_dir / "LABEL").write_text("   \n")
        monkeypatch.setattr(config, "IG_SESSIONS_DIR", tmp_path)
        monkeypatch.setenv("IG_ACCOUNT_A_NAME", "Studio Page A")
        assert config._slot_display_name("A") == "Studio Page A"
