"""One stdout console handler for `backend/`, installed exactly once.

Issue #26 asked for `logging` adoption but named the constraint that makes
it a decision: browser-automation progress must stay visible to the CLI user
during an interactive run. Everything here exists to keep that true.

Three properties are load-bearing:

**Bare `%(message)s` format.** The 67 migrated call sites already carry a
hand-rolled `[scheduler]` / `[TikTok]` prefix in the message text. Keeping
the formatter bare makes the migration byte-identical on stdout, which is
what `tests/golden/output_catalogue.txt` pins. Logger names mirror those
prefixes, so a later change can move the prefix into the format string as a
deliberate, single-commit output change.

**stdout, resolved at emit time.** `logging.StreamHandler` defaults to
stderr; this project's output has always gone to stdout and ~20 tests assert
on `capsys.readouterr().out`, one of them asserting it is exactly empty.
Binding `sys.stdout` at construction time is not enough either — pytest
replaces `sys.stdout` after import, so a handler holding the original object
emits to the real terminal and the assertions silently see nothing.
Measured on 2026-07-30: an eagerly-bound handler fails those tests while a
lazily-resolved one passes.

**No propagation.** uvicorn installs its own root handlers, so propagating
would print every automation line twice under `./run.sh`.
"""

import logging
import sys

#: Parent logger for the whole backend. Module loggers hang off this, so one
#: handler and one level control the entire application.
ROOT_NAME = "riceposter"

DEFAULT_LEVEL = "INFO"


class _StdoutHandler(logging.StreamHandler):
    """A `StreamHandler` that resolves `sys.stdout` on every emit.

    Late binding is the entire point: pytest's capture fixtures swap
    `sys.stdout` after this module is imported, and a handler that captured
    the original stream would write past them.
    """

    def __init__(self) -> None:
        super().__init__(stream=sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        """Ignore the base class's assignment; the property is the source."""


def resolve_level(name: str) -> int | None:
    """Return the numeric level for `name`, or None if it is not usable.

    Returning None rather than falling back silently lets `config.py` report
    a bad `LOG_LEVEL` through the existing startup-problem channel instead of
    quietly running at a level the maintainer did not ask for.

    `NOTSET` is rejected even though the standard library maps it, because it
    is not a threshold: it means "defer to the parent logger", and with
    propagation off it would leave the console at the root logger's default
    rather than at anything the maintainer chose (cold review, 2026-07-31).
    """
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    if not isinstance(level, int) or level == logging.NOTSET:
        return None
    return level


def configure_logging(level_name: str = DEFAULT_LEVEL) -> logging.Logger:
    """Install the console handler on the `riceposter` logger, once.

    Safe to call repeatedly: re-calling updates the level but never stacks a
    second handler, which would double every line.
    """
    logger = logging.getLogger(ROOT_NAME)
    # Explicit None check rather than `or logging.INFO`: a falsy-but-valid
    # level would slip through the truthiness test and be silently replaced.
    level = resolve_level(level_name)
    logger.setLevel(logging.INFO if level is None else level)
    logger.propagate = False

    if not any(isinstance(h, _StdoutHandler) for h in logger.handlers):
        handler = _StdoutHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return the module logger for `name`, e.g. `get_logger("scheduler")`."""
    return logging.getLogger(f"{ROOT_NAME}.{name}")
