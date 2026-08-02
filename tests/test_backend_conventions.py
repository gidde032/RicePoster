"""Batch 3 of the tech-debt campaign: #36's convention cleanups.

Source: tech-debt audit 2026-07-29, findings BE-14, BE-22, BE-24, BE-25.
One section per finding; BE-15 was deferred rather than shipped (see the PR),
and BE-25 resolved as a deletion rather than a typed model (see its section).
"""

import ast
import asyncio
import inspect
import os

import pytest
from pydantic import SecretStr

from backend import captions, config, main, models, session_manager
from tests.test_paths import _backend_modules


# --- BE-14: one boolean-env parser -------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        # Whitespace does NOT get trimmed. This row read `True` in the first
        # version of this file, which blessed a real behaviour change: the
        # helper had a `.strip()` the expression it replaced did not, so
        # `HEADLESS=" true "` flipped from visible to headless. The table was
        # written from what the code did rather than from what it replaced.
        ("  true  ", False),
        ("true ", False),
        (" true", False),
        ("false", False),
        ("False", False),
        ("", False),
        ("   ", False),
        ("1", False),
        ("yes", False),
        ("on", False),
        ("0", False),
        ("nonsense", False),
    ],
)
def test_env_bool_is_true_only_for_the_literal_true(monkeypatch, raw, expected):
    """`env_bool` must reproduce `os.getenv(n, d).lower() == "true"` exactly.

    The widening cases ("1", "yes", "on") are the ones that matter: a helper
    that generously accepted them would flip `HEADLESS=1` from visible to
    headless, changing the browser identity Instagram sees.
    """
    monkeypatch.setenv("SOME_KNOB", raw)
    assert config.env_bool("SOME_KNOB", True) is expected
    assert config.env_bool("SOME_KNOB", False) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "true", "True", "TRUE", "  true  ", "true ", " true", "\ttrue",
        "false", "", "   ", "1", "0", "yes", "no", "on", "off", "nonsense",
    ],
)
@pytest.mark.parametrize("default", [True, False])
def test_env_bool_matches_the_expression_it_replaced(monkeypatch, raw, default):
    """Differential pin against the original expression, not a hand table.

    The hand-written table above got this wrong once: it was populated from
    what the new helper did rather than from what the old expression did, so
    it asserted `("  true  ", True)` while its own docstring claimed exact
    equivalence — the test blessed the divergence it existed to prevent.
    Comparing against the literal replaced expression cannot make that
    mistake, because there is no second source of truth to drift from.
    """
    monkeypatch.setenv("SOME_KNOB", raw)

    original = os.getenv("SOME_KNOB", "true" if default else "false").lower() == "true"

    assert config.env_bool("SOME_KNOB", default) is original


@pytest.mark.parametrize("default", [True, False])
def test_env_bool_uses_the_default_only_when_the_variable_is_absent(
    monkeypatch, default
):
    """Unset falls back to the default; set-but-blank does not.

    This is the deliberate asymmetry with `_env_int`/`_env_float`, where empty
    *does* mean default. `HEADLESS=` (a blank line in credentials.env) means a
    visible browser today and must keep meaning that — the maintainer runs
    live Instagram posts with HEADLESS=false precisely because headless leaks
    a mismatched user agent and colour depth.
    """
    monkeypatch.delenv("SOME_KNOB", raising=False)
    assert config.env_bool("SOME_KNOB", default) is default

    monkeypatch.setenv("SOME_KNOB", "")
    assert config.env_bool("SOME_KNOB", default) is False


def test_boolean_knobs_are_parsed_through_the_shared_helper():
    """Source-level: no module re-implements the `.lower() == "true"` pattern.

    Labelled as source-level per the project's testing rules — the constants
    are evaluated at import, so a behavioural assertion would need a subprocess
    per case.

    Matched over the AST rather than the raw text. A line-based scan flagged
    `env_bool`'s own docstring, which quotes the pattern it replaced; a test
    that forbids describing the thing it forbids is one someone eventually
    deletes.

    Widened after cold review, which showed the first version matched only
    `getenv` against a bare `"true"` constant — so `os.environ.get("X", "")
    .lower() == "true"` and `... .lower() in ("true", "1")` both slipped past,
    and only `config` and `main` were scanned at all.
    """

    def _reads_env(node):
        return any(
            (isinstance(sub, ast.Name) and sub.id in {"getenv", "environ"})
            or (isinstance(sub, ast.Attribute) and sub.attr in {"getenv", "environ"})
            for sub in ast.walk(node)
        )

    def _mentions_bool_literal(node):
        return any(
            isinstance(sub, ast.Constant) and sub.value in {"true", "false"}
            for sub in ast.walk(node)
        )

    offenders = {}
    for module in _backend_modules():
        if module.name == "config.py":
            continue  # env_bool itself lives here
        hits = [
            ast.unparse(node)
            for node in ast.walk(ast.parse(module.read_text()))
            if isinstance(node, ast.Compare)
            and _reads_env(node.left)
            and any(_mentions_bool_literal(c) for c in node.comparators)
        ]
        if hits:
            offenders[module.name] = hits
    assert offenders == {}, f"boolean env parsed outside env_bool: {offenders!r}"


def test_headless_and_scheduler_enabled_are_wired_to_env_bool(monkeypatch):
    """The two knobs BE-14 converted, re-derived rather than read ambiently.

    The first version asserted `os.environ.get("HEADLESS") is None`, which made
    the test a function of the developer's shell: the maintainer debugs live
    Instagram runs with `HEADLESS=false` exported, and
    `HEADLESS=false python -m pytest` turned it red while CI stayed green.
    Controlling the variable instead of asserting about it is the fix — the
    same hermeticity rule `config.UNDER_PYTEST` exists for.

    `SCHEDULER_ENABLED` is forced to "false" by `conftest.py` so the scheduler
    never starts during tests, which makes it the better evidence of the two:
    it proves `env_bool` reads the environment rather than always returning
    its default.
    """
    monkeypatch.delenv("HEADLESS", raising=False)
    assert config.env_bool("HEADLESS", True) is True

    monkeypatch.setenv("HEADLESS", "false")
    assert config.env_bool("HEADLESS", True) is False

    assert os.environ["SCHEDULER_ENABLED"] == "false"
    assert main.SCHEDULER_ENABLED is False


# --- BE-22: a blank per-slot token must not look configured ------------------


@pytest.fixture
def api_mode(monkeypatch):
    """Put config in the only mode that reads per-slot tokens."""
    monkeypatch.setattr(config, "POST_MODE", "api")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("sk-present"))
    monkeypatch.setattr(config, "SLOT_IDS", ["A", "B"])


def _set_slot(monkeypatch, slot, ig_id="id", ig_token="igt", tt_token="ttt"):
    monkeypatch.setenv(f"IG_ACCOUNT_{slot}_ID", ig_id)
    monkeypatch.setenv(f"IG_ACCOUNT_{slot}_TOKEN", ig_token)
    monkeypatch.setenv(f"TT_ACCOUNT_{slot}_TOKEN", tt_token)


def test_blank_slot_token_is_reported_at_startup(api_mode, monkeypatch):
    """The finding itself: slot B looks configured and fails at post time."""
    _set_slot(monkeypatch, "A")
    _set_slot(monkeypatch, "B", ig_token="   ")

    problems = config.check_startup_config()

    assert len(problems) == 1
    assert "Slot B" in problems[0]
    assert "IG_ACCOUNT_B_TOKEN" in problems[0]
    assert "Slot A" not in problems[0]


def test_a_fully_configured_api_setup_reports_nothing(api_mode, monkeypatch):
    _set_slot(monkeypatch, "A")
    _set_slot(monkeypatch, "B")

    assert config.check_startup_config() == []


def test_all_missing_values_for_one_slot_are_named_in_one_problem(
    api_mode, monkeypatch
):
    """One line per slot, listing every empty field — not three lines for the
    same slot, which is how a startup block becomes unreadable."""
    _set_slot(monkeypatch, "A")
    _set_slot(monkeypatch, "B", ig_id="", ig_token="", tt_token="")

    problems = config.check_startup_config()

    assert len(problems) == 1
    for expected in ("IG_ACCOUNT_B_ID", "IG_ACCOUNT_B_TOKEN", "TT_ACCOUNT_B_TOKEN"):
        assert expected in problems[0]
    assert "are empty" in problems[0]


@pytest.mark.parametrize("mode", ["browser", "mock"])
def test_blank_tokens_are_silent_outside_api_mode(monkeypatch, mode):
    """The gate that keeps this check from becoming noise.

    Tokens are read only by `poster.py`, the official-API path. Browser mode
    authenticates from the persistent profiles under `sessions/`, so warning
    there would print on every startup of the mode actually in use — and a
    startup block that is always noisy buries the ANTHROPIC_API_KEY line it
    shares.
    """
    monkeypatch.setattr(config, "POST_MODE", mode)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", SecretStr("sk-present"))
    monkeypatch.setattr(config, "SLOT_IDS", ["A"])
    _set_slot(monkeypatch, "A", ig_id="", ig_token="", tt_token="")

    assert config.check_startup_config() == []


def test_startup_check_never_raises_on_a_blank_token(api_mode, monkeypatch):
    """Reports rather than raises. Refusing to start over an unused slot's
    blank token would be a worse failure than the one being fixed."""
    _set_slot(monkeypatch, "A", ig_id="", ig_token="", tt_token="")
    _set_slot(monkeypatch, "B", ig_id="", ig_token="", tt_token="")

    problems = config.check_startup_config()

    assert isinstance(problems, list) and len(problems) == 2


def test_slot_problem_never_echoes_the_token_value(api_mode, monkeypatch):
    """The message names the *variable*, never its content. A partially typed
    token is still a credential."""
    _set_slot(monkeypatch, "A")
    _set_slot(monkeypatch, "B", ig_token="   ", tt_token="tt-secret-value")

    joined = " ".join(config.check_startup_config())

    assert "tt-secret-value" not in joined
    assert "igt" not in joined


# --- BE-25: generate_all_captions was dead and is gone -----------------------


def test_generate_all_captions_stays_removed():
    """BE-25 asked for a typed model on `generate_all_captions`. The cold
    review then established the helper had no production caller at all — only
    its own test — so the maintainer chose deletion over typing it
    (2026-07-30). `CaptionJob` existed solely to feed it and went with it.

    The per-slot path is `generate_caption`, which the frontend drives one
    request at a time; a future batch caller would be three lines of
    `asyncio.gather`. If this test fails because the helper came back, that is
    fine — re-add it with a production caller, and delete this test.
    """
    assert not hasattr(captions, "generate_all_captions")
    assert not hasattr(models, "CaptionJob")


# --- BE-24: error paths must keep the exception message ----------------------


@pytest.fixture
def failing_check(monkeypatch, tmp_path):
    """Drive `check_session` down its except branch without a browser.

    Overrides the conftest tripwire's `_run_session_check` stub, which is the
    documented way to exercise this path. `session_exists` is forced True so
    the no_session short-circuit does not fire first.
    """

    def _arrange(exc):
        async def _raise(*a, **kw):
            raise exc

        monkeypatch.setattr(session_manager, "_run_session_check", _raise)
        monkeypatch.setattr(session_manager, "session_exists", lambda *a, **kw: True)

    return _arrange


def test_check_session_error_log_names_the_cause(failing_check, capsys):
    """Behavioural: the printed line must carry the message, not just the type.

    `check error (Error)` was the same line for a navigation failure, a missing
    selector and a genuine timeout. This runs unattended before a scheduled
    batch, so that console line is the entire record of what went wrong.
    """
    failing_check(RuntimeError("Timeout 30000ms exceeded waiting for selector"))

    result = asyncio.run(session_manager.check_session("A", "instagram"))
    assert result == "check_error"

    line = capsys.readouterr().out
    assert "RuntimeError" in line, "the class name is still worth printing"
    assert "Timeout 30000ms exceeded waiting for selector" in line


def test_check_session_error_log_has_no_dangling_separator(
    failing_check, capsys
):
    """`asyncio.wait_for` raises TimeoutError with an empty `str()`, and it is
    the single most likely failure on this path. The message is appended only
    when non-empty, so the line must not end in a bare ": "."""
    failing_check(TimeoutError())

    result = asyncio.run(session_manager.check_session("A", "instagram"))
    assert result == "check_error"

    line = capsys.readouterr().out.strip()
    assert "check error (TimeoutError)" in line
    assert ": )" not in line


def test_check_session_still_never_raises(failing_check):
    """The contract BE-24 must not disturb: any exception collapses to
    "check_error" so a failed check never takes down a scheduled batch."""
    failing_check(ValueError("anything at all"))

    assert asyncio.run(session_manager.check_session("A", "tiktok")) == "check_error"


def test_notifier_still_withholds_its_exception_message():
    """BE-24's deliberate exception, pinned so a future consistency pass does
    not 'fix' it into matching the change above.

    The ntfy topic string is a bearer secret and forms part of `self.url`, so
    an httpx exception — which embeds the request URL — would leak send and
    subscribe access to the maintainer's phone into a log file. Source-level
    because the assertion is about what is *absent* from the log line.

    Reads the method out of the module's file source via AST rather than
    `inspect.getsource(NtfyNotifier.send)`: conftest's tripwire replaces that
    attribute with `_blocked_send`, so the direct call inspects the stub and
    the assertion passes or fails for reasons that have nothing to do with
    `notifier.py`.
    """
    from backend import notifier

    tree = ast.parse(inspect.getsource(notifier))
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "NtfyNotifier"
    )
    # Scoped to the class: the abstract `Notifier.send` base is defined first
    # in the module and would otherwise be what a bare walk finds.
    send = next(
        node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send"
    )
    src = ast.unparse(send)

    assert "type(e).__name__" in src
    assert "{e}" not in src, "the ntfy exception message must stay out of logs"
    assert "self.url" not in src.split("except Exception")[1]
