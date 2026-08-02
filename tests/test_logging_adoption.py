"""Batch 7 (#26): adopting `logging` must not change what the user sees.

Issue #26 flags this as a decision rather than a mechanical change, because
browser-automation progress has to stay visible on the CLI during an
interactive run. A naive migration hides it behind a handler configuration
and nothing fails — the post still happens, the maintainer just stops being
told what it is doing.

The pin follows Batch 6's golden-transcript structure: the output catalogue
in `tests/golden/output_catalogue.txt` was generated from the code as it
stood at `498619d`, *before* any call site was touched, so it proves
equivalence against the original behaviour rather than describing whatever
the new code happens to produce.
"""

import logging
import pathlib
import sys

import pytest

from backend import config
from backend.logging_setup import (
    ROOT_NAME,
    configure_logging,
    get_logger,
    resolve_level,
)
from tests.output_catalogue import catalogue_text

GOLDEN = (
    pathlib.Path(__file__).resolve().parent / "golden" / "output_catalogue.txt"
)


def test_output_catalogue_matches_the_pre_migration_golden():
    """Every message in `backend/` is byte-identical to its pre-#26 form.

    Regenerate with `python tests/output_catalogue.py > <golden>` only when
    a message is *intended* to change, and say so in the commit. A silent
    regeneration turns this pin back into a description.
    """
    current = catalogue_text()
    expected = GOLDEN.read_text()
    if current == expected:
        return

    current_lines = current.splitlines()
    expected_lines = expected.splitlines()
    added = [ln for ln in current_lines if ln not in expected_lines]
    removed = [ln for ln in expected_lines if ln not in current_lines]
    pytest.fail(
        "backend/ output changed against the pre-#26 golden.\n"
        f"  {len(expected_lines)} sites expected, {len(current_lines)} found\n"
        + "".join(f"  + {ln}\n" for ln in added[:10])
        + "".join(f"  - {ln}\n" for ln in removed[:10])
    )


@pytest.fixture
def restore_logger():
    """Undo level/handler changes so one test cannot silence another."""
    logger = logging.getLogger(ROOT_NAME)
    before_level = logger.level
    before_handlers = list(logger.handlers)
    # propagate is restored too: test_records_do_not_propagate_to_root asserts
    # it without this fixture, so a future test that flipped it would leak
    # past that assertion invisibly (cold review, 2026-07-31).
    before_propagate = logger.propagate
    yield logger
    logger.setLevel(before_level)
    logger.handlers[:] = before_handlers
    logger.propagate = before_propagate


class TestConsoleHandlerStaysVisible:
    """#26's named constraint: CLI progress must survive the migration."""

    def test_log_records_reach_captured_stdout(self, capsys):
        """The load-bearing property, and the one that fails silently.

        `logging.StreamHandler` defaults to stderr, and a handler that binds
        `sys.stdout` at construction time keeps the pre-pytest stream. Either
        mistake leaves every `capsys.readouterr().out` assertion in the suite
        passing against an empty string instead of the message it names.
        Measured on 2026-07-30: an eagerly-bound handler fails this test with
        the text visible only under pytest's "Captured stdout" section.
        """
        get_logger("probe").info("PROGRESS-LINE")
        captured = capsys.readouterr()
        assert "PROGRESS-LINE" in captured.out
        assert captured.err == "", "output belongs on stdout, as it always has"

    def test_format_is_bare_so_migrated_messages_are_byte_identical(self, capsys):
        """No level, timestamp or logger name may be prepended.

        The 67 migrated sites carry their own `[scheduler]`-style prefix, so
        any formatter decoration would double it and break the golden.
        """
        get_logger("probe").warning("[queue] note: something")
        assert capsys.readouterr().out == "[queue] note: something\n"

    def test_module_loggers_hang_off_one_configurable_parent(self):
        """The point of #26: one level controls the whole backend."""
        assert get_logger("scheduler").name == f"{ROOT_NAME}.scheduler"
        assert get_logger("scheduler").parent is logging.getLogger(ROOT_NAME)

    def test_level_filters_below_the_threshold(self, capsys, restore_logger):
        restore_logger.setLevel(logging.WARNING)
        get_logger("probe").info("SHOULD-NOT-APPEAR")
        get_logger("probe").warning("SHOULD-APPEAR")
        out = capsys.readouterr().out
        assert "SHOULD-NOT-APPEAR" not in out
        assert "SHOULD-APPEAR" in out


class TestHandlerInstallation:
    def test_reconfiguring_does_not_stack_handlers(self, restore_logger):
        """A second handler would print every automation line twice."""
        configure_logging("INFO")
        configure_logging("DEBUG")
        assert len(restore_logger.handlers) == 1

    def test_records_do_not_propagate_to_root(self):
        """uvicorn installs root handlers; propagating would double output."""
        assert logging.getLogger(ROOT_NAME).propagate is False

    def test_handler_follows_a_replaced_stdout(self, restore_logger, capsys):
        """Late binding, asserted directly rather than through capsys."""
        handler = restore_logger.handlers[0]
        original = sys.stdout
        try:
            sys.stdout = object()
            assert handler.stream is not original
        finally:
            sys.stdout = original
        capsys.readouterr()


class TestLevelConfiguration:
    @pytest.mark.parametrize(
        "name,expected",
        [("INFO", logging.INFO), ("warning", logging.WARNING),
         (" debug ", logging.DEBUG),
         # `WARN` and `FATAL` are stdlib aliases, so accepting them is the
         # standard library's behaviour rather than a courtesy of ours.
         ("WARN", logging.WARNING), ("FATAL", logging.CRITICAL)],
    )
    def test_level_names_are_accepted_case_and_space_insensitively(
        self, name, expected
    ):
        assert resolve_level(name) == expected

    @pytest.mark.parametrize("name", ["verbose", "", "20x", "TRACE", "NOTSET"])
    def test_non_level_names_are_rejected_rather_than_guessed(self, name):
        assert resolve_level(name) is None

    def test_notset_is_rejected_rather_than_silently_becoming_info(
        self, monkeypatch, restore_logger
    ):
        """Cold review, 2026-07-31.

        `NOTSET` maps to 0 in the standard library, which is falsy — so the
        original `resolve_level(name) or logging.INFO` replaced it silently
        *and* the startup check saw a valid level and reported nothing. That
        is exactly the "running at a level you did not ask for" outcome the
        None sentinel exists to prevent. It is also not a threshold: it means
        "defer to the parent", which with propagation off is meaningless here.
        """
        configure_logging("NOTSET")
        assert restore_logger.level == logging.INFO

        monkeypatch.setattr(config, "LOG_LEVEL", "NOTSET")
        assert any("LOG_LEVEL" in p for p in config.check_startup_config())

    def test_unusable_log_level_is_reported_at_startup(self, monkeypatch):
        """Otherwise a typo'd LOG_LEVEL is indistinguishable from INFO."""
        monkeypatch.setattr(config, "LOG_LEVEL", "verbose")
        problems = config.check_startup_config()
        assert any("LOG_LEVEL" in p and "verbose" in p for p in problems)

    def test_a_valid_log_level_reports_no_problem(self, monkeypatch):
        monkeypatch.setattr(config, "LOG_LEVEL", "WARNING")
        assert not any("LOG_LEVEL" in p for p in config.check_startup_config())


class TestTheTwoPopulationsStaySeparate:
    """Batch 7 converted 67 of 118 output sites, not all of them.

    The 51 that stayed are CLI presentation: `session_manager`'s status,
    health-check and usage reports — `print("-" * 40)`, blank spacer lines,
    aligned rows — and the interactive login blocks in both browser modules,
    which sit directly above an `input()` prompt. Those are a command-line
    program rendering a report to a person who is standing there, not an
    application emitting log records, and routing them through `logging`
    would let `LOG_LEVEL=WARNING` blank out the output of
    `python -m backend.session_manager status`.

    Nothing in the code marks which population a site belongs to, so the
    boundary is pinned here (maintainer decision, 2026-07-30).
    """

    EXPECTED_PRINT_SCOPES = {
        # A CLI report, end to end. Its one diagnostic — the `[check]` error
        # line in `check_session` — is a log record.
        ("session_manager.py", "show_status"),
        ("session_manager.py", "run_check"),
        ("session_manager.py", "login_account"),
        ("session_manager.py", "login_all"),
        ("session_manager.py", "clear_session"),
        ("session_manager.py", "main"),
        # "press Enter here to save the session" — prompts for a blocking
        # input() call, useless if a log level can suppress them.
        ("instagram_browser.py", "login"),
        ("tiktok_browser.py", "login"),
    }

    def test_print_survives_only_where_it_is_the_user_interface(self):
        from tests.output_catalogue import print_scopes

        actual = print_scopes()
        strays = actual - self.EXPECTED_PRINT_SCOPES
        assert not strays, (
            f"{sorted(strays)} still call print(). Diagnostic and automation "
            f"progress output must go through _log so LOG_LEVEL controls it "
            f"(#26). If this is genuinely CLI presentation, add it above with "
            f"a reason."
        )
        gone = self.EXPECTED_PRINT_SCOPES - actual
        assert not gone, (
            f"{sorted(gone)} no longer print. If they were migrated to "
            f"logging on purpose, LOG_LEVEL can now blank out a CLI report — "
            f"confirm that is intended and update this set."
        )

    def test_the_automation_modules_narrate_through_logging(self):
        """The property #26 names: progress must be level-controlled."""
        from tests.output_catalogue import callees

        for module in ("scheduler.py", "queue.py", "notifier.py", "main.py",
                       "captions.py", "instagram_browser.py",
                       "tiktok_browser.py"):
            levels = {c for m, _, c in callees() if m == module and c != "print"}
            assert levels, f"{module} emits nothing through _log"


class TestExpectedMissesAreNotWarnings:
    """Cold review, 2026-07-31 — convergent finding from two reviewers.

    `_dismiss_one_time_overlay` waits for TikTok's one-time "Got it" promo.
    That dialog is absent on virtually every post, so the wait times out and
    the `except` branch is the *ordinary* path. It was assigned WARNING,
    which put a Playwright timeout dump on every successful TikTok post at
    `LOG_LEVEL=WARNING` — precisely the setting meant for unattended batches,
    where a stream of routine warnings hides the real one.

    The general rule this pins: an `except` branch that the happy path runs
    through is not a degraded state. Levels are assigned per rule, not per
    site.
    """

    def _level_of(self, module, function, needle):
        import ast
        import pathlib

        from tests.output_catalogue import BACKEND, _enclosing_scopes

        src = (pathlib.Path(BACKEND) / module).read_text()
        tree = ast.parse(src)
        scopes = _enclosing_scopes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "_log"):
                continue
            segment = ast.get_source_segment(src, node) or ""
            if needle in segment and scopes.get(id(node)) == function:
                return func.attr
        pytest.fail(f"no _log call matching {needle!r} in {module}:{function}")

    def test_the_absent_overlay_is_not_reported_as_degraded(self):
        assert self._level_of(
            "tiktok_browser.py",
            "_dismiss_one_time_overlay",
            "Overlay clearance check complete",
        ) == "info"

    def test_the_absent_iframe_stays_the_reference_case(self):
        """The site whose correct level made the one above look wrong."""
        assert self._level_of(
            "tiktok_browser.py", "_resolve_upload_target", "No iframe detected"
        ) == "info"

    def test_a_genuine_playwright_error_still_warns(self):
        """Not every `except` is routine, and this one is not.

        `_confirm_post_modal` handles the modal being absent with
        `count() == 0`, so its `except` fires only on a real Playwright
        failure. A reviewer proposed downgrading it by analogy with the
        overlay; rejected with this evidence.
        """
        assert self._level_of(
            "tiktok_browser.py", "_confirm_post_modal", "bypass step notice"
        ) == "warning"
