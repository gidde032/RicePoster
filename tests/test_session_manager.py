"""Unit tests for backend/session_manager.py against temp session dirs."""

import json

from backend import session_manager, tiktok_browser


def test_instagram_session_missing(tmp_sessions):
    assert session_manager.session_exists("instagram", "A") is False


def test_instagram_session_empty_dir_is_not_logged_in(tmp_sessions):
    (tmp_sessions["instagram"] / "A").mkdir()
    assert session_manager.session_exists("instagram", "A") is False


def test_instagram_session_populated_dir(tmp_sessions):
    d = tmp_sessions["instagram"] / "A"
    d.mkdir()
    (d / "Default").mkdir()
    assert session_manager.session_exists("instagram", "A") is True


def test_tiktok_cookie_session_detected(tmp_sessions):
    cookies = tmp_sessions["tiktok"] / "A_cookies.json"
    cookies.write_text(json.dumps([{"name": "sessionid", "value": "x" * 20}]))
    assert tiktok_browser.has_cookie_session("A") is True
    assert session_manager.session_exists("tiktok", "A") is True


def test_tiktok_tiny_cookie_file_ignored(tmp_sessions):
    (tmp_sessions["tiktok"] / "A_cookies.json").write_text("[]")
    assert tiktok_browser.has_cookie_session("A") is False
    assert session_manager.session_exists("tiktok", "A") is False


def test_tiktok_profile_dir_counts_as_session(tmp_sessions):
    d = tmp_sessions["tiktok"] / "B"
    d.mkdir()
    (d / "Default").mkdir()
    assert session_manager.session_exists("tiktok", "B") is True


def test_clear_session_instagram_removes_only_instagram(tmp_sessions):
    ig = tmp_sessions["instagram"] / "A"
    tt = tmp_sessions["tiktok"] / "A"
    for d in (ig, tt):
        d.mkdir()
        (d / "marker").write_text("x")

    session_manager.clear_session("instagram", "A")
    assert not ig.exists()
    assert tt.exists()


def test_clear_session_tiktok(tmp_sessions):
    tt = tmp_sessions["tiktok"] / "A"
    tt.mkdir()
    (tt / "marker").write_text("x")
    session_manager.clear_session("tiktok", "A")
    assert not tt.exists()


# --- #9: slot guidance generated from the configured roster ------------------
#
# Several errors and usage hints hardcoded "A, B, C" while the roster has been
# configurable since ACCOUNT_SLOTS landed, so a maintainer running
# ACCOUNT_SLOTS=A,B,C,D was told by the tool that D was invalid.

import sys

import pytest


@pytest.fixture
def custom_slots(monkeypatch):
    """Redirect the roster. Guidance is generated at call time, so patching the
    module attribute is enough."""

    def _set(slots):
        monkeypatch.setattr(session_manager, "SLOTS", slots)

    return _set


def _run_cli(monkeypatch, argv):
    """Invoke main() with argv, returning its SystemExit code (None if it fell
    through without exiting)."""
    monkeypatch.setattr(sys, "argv", ["session_manager", *argv])
    try:
        session_manager.main()
    except SystemExit as e:
        return e.code
    return None


def test_slot_choices_reads_the_configured_roster(custom_slots):
    custom_slots(["A", "B", "C"])
    assert session_manager._slot_choices() == "A, B, or C"

    custom_slots(["A", "B", "C", "D"])
    assert session_manager._slot_choices() == "A, B, C, or D"

    custom_slots(["only"])
    assert session_manager._slot_choices() == "only"


def test_no_message_claims_abc_is_the_fixed_roster(custom_slots, monkeypatch, capsys):
    """Acceptance criterion 1. With a four-slot roster, D must be offered."""
    custom_slots(["A", "B", "C", "D"])

    assert _run_cli(monkeypatch, ["login", "instagram", "Z"]) == 1

    out = capsys.readouterr().out
    assert "A, B, C, or D" in out
    assert "Use A, B, or C." not in out


def test_a_configured_slot_is_never_rejected(custom_slots, monkeypatch, capsys):
    """The bug the hardcoding caused: slot D is configured, so it must be
    accepted rather than reported invalid."""
    custom_slots(["A", "B", "C", "D"])
    called = []

    # A real coroutine, so `asyncio.run` runs normally. An earlier version
    # patched `session_manager.asyncio.run` instead — but `asyncio` there is
    # the global module object, not a module-local alias, so that swapped
    # `asyncio.run` process-wide for the duration of the test.
    async def _record(platform, slot):
        called.append((platform, slot))

    monkeypatch.setattr(session_manager, "login_account", _record)

    _run_cli(monkeypatch, ["login", "instagram", "D"])

    assert called == [("instagram", "D")]
    assert "Invalid slot" not in capsys.readouterr().out


def test_lowercase_slot_ids_are_resolved_not_uppercased(custom_slots):
    """ACCOUNT_SLOTS accepts letters, digits, '_' and '-' in any case, so the
    previous bare `.upper()` turned `--slot a1` into "A1" and then rejected it
    as invalid — the tool refusing a slot it had itself configured."""
    custom_slots(["a1", "b2"])

    assert session_manager._resolve_slot("a1") == "a1"
    assert session_manager._resolve_slot("A1") == "a1"
    assert session_manager._resolve_slot("  B2 ") == "b2"
    assert session_manager._resolve_slot("c3") is None


def test_slot_input_stays_case_insensitive_for_the_default_roster(custom_slots):
    """The convenience the `.upper()` provided must not be lost."""
    custom_slots(["A", "B", "C"])

    assert session_manager._resolve_slot("a") == "A"
    assert session_manager._resolve_slot("A") == "A"


def test_clear_all_still_works_and_is_case_insensitive(
    custom_slots, monkeypatch, capsys
):
    """`all` is not a slot id, so slot resolution must not swallow it."""
    custom_slots(["A", "B"])
    cleared = []
    monkeypatch.setattr(
        session_manager, "clear_session", lambda p, s: cleared.append((p, s))
    )

    _run_cli(monkeypatch, ["clear", "tiktok", "ALL"])

    assert cleared == [("tiktok", "A"), ("tiktok", "B")]
    assert "Invalid slot" not in capsys.readouterr().out


def test_usage_block_names_the_configured_slots(custom_slots, monkeypatch, capsys):
    """Acceptance criteria 2 and 3: concise by default, accurate when custom."""
    custom_slots(["A", "B", "C"])
    assert _run_cli(monkeypatch, []) == 1
    out = capsys.readouterr().out
    assert "login instagram A" in out
    assert "configured slots: A, B, C" in out

    custom_slots(["prod", "burner"])
    assert _run_cli(monkeypatch, []) == 1
    out = capsys.readouterr().out
    assert "login instagram prod" in out
    assert "login tiktok burner" in out
    assert "configured slots: prod, burner" in out


def test_ambiguous_case_colliding_roster_resolves_to_nothing(custom_slots):
    """`parse_slot_ids` dedupes case-sensitively, so ACCOUNT_SLOTS=a,A yields
    two distinct slots. Device identity is assigned by slot *index*, so
    silently picking the first would hand `--slot A` a different viewport and
    a different session directory than it had before. Exact match wins; a
    genuinely ambiguous input resolves to nothing and is reported invalid."""
    custom_slots(["a", "A"])

    assert session_manager._resolve_slot("A") == "A"
    assert session_manager._resolve_slot("a") == "a"

    custom_slots(["aB", "Ab"])
    assert session_manager._resolve_slot("ab") is None


def test_no_hardcoded_roster_text_remains_in_the_source():
    """Source-level guard so the literals cannot creep back in.

    Scans every string literal in the module rather than three exact phrases
    inside `main`. Cold review pointed out that the guidance now lives in
    `_slot_choices`/`_example_slot`, which the old `getsource(main)` scan could
    not see at all, and that `"Use A, B, C."` — one character off the banned
    list — sailed straight through.

    Docstrings are excluded: this module's own docstring says "A is an example,
    not the fixed roster", and a guard that forbids explaining itself is one
    the next person deletes.
    """
    import ast
    import inspect
    import re

    tree = ast.parse(inspect.getsource(session_manager))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    # Two or more single-character ids in a comma list: "A, B", "A, B, C",
    # "A, B, or C", "A, B, C, or all".
    roster = re.compile(r"\b[A-Za-z0-9]\s*,\s*[A-Za-z0-9]\b")

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
        and roster.search(node.value)
    ]
    assert offenders == [], f"hardcoded roster text returned: {offenders!r}"
