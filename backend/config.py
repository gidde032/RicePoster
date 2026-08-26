import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

from backend.logging_setup import DEFAULT_LEVEL, configure_logging, resolve_level

# --- Filesystem layout -------------------------------------------------------
#
# Every path the backend derives lives here (tech-debt audit BE-13, #27).
# Previously `Path(__file__).parent.parent / "<dir>"` was repeated across eight
# modules and the literal "sessions" appeared in four of them independently, so
# a layout rename meant finding each one by hand.
#
# `.parent.parent` is purely lexical and every consumer lives in `backend/`, so
# this is byte-identical to what each module computed for itself — deliberately
# *not* `.resolve()`d. `user_data_dir=` receives `str(path)`, and a different
# string is a different Chrome profile.
#
# Consumers re-export these under their existing module-level names rather than
# referencing `config.X` at the call site. That is not stylistic: conftest
# redirects several of them away from the maintainer's real files with autouse
# fixtures (`monkeypatch.setattr(instagram_browser, "SESSIONS_DIR", ...)`), and
# a call-site reference would step around the redirect and let the suite run
# against live session data. `tests/test_paths.py` pins all fourteen values.
PROJECT_ROOT = Path(__file__).parent.parent

ENV_PATH = PROJECT_ROOT / "credentials.env"

# Named SESSIONS_ROOT, not SESSIONS_DIR: both browser modules already export a
# `SESSIONS_DIR` meaning their own platform subdirectory, and a future
# `from backend.config import SESSIONS_DIR` in one of them would silently point
# a persistent profile at the parent directory.
SESSIONS_ROOT = PROJECT_ROOT / "sessions"
IG_SESSIONS_DIR = SESSIONS_ROOT / "instagram"
TT_SESSIONS_DIR = SESSIONS_ROOT / "tiktok"
HEALTH_CACHE_FILE = SESSIONS_ROOT / ".health_cache.json"

DEBUG_DIR = PROJECT_ROOT / "debug"
MEDIA_DIR = PROJECT_ROOT / "media"
QUEUE_FILE = PROJECT_ROOT / "queue.jsonl"
QUEUE_MEDIA_DIR = PROJECT_ROOT / "queue_media"
HISTORY_FILE = PROJECT_ROOT / "history.jsonl"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# mkdir calls stay with the module that needs the directory, so importing
# `config` does not start creating `sessions/instagram` and `sessions/tiktok`
# as a side effect. This one is the exception: it has always lived in config.
MEDIA_DIR.mkdir(exist_ok=True)

# --- Environment -------------------------------------------------------------
#
# Load env from project root.
#
# Never under pytest. Every knob below is a module-level constant evaluated at
# import, so loading the maintainer's real credentials.env made the *test
# suite* a function of an untracked local file: setting
# PREFLIGHT_CHECK_PLATFORMS=none turned seven tests red without a line of code
# changing, and a fresh clone (no credentials.env at all) exercised a third
# set of values again. A gate whose result depends on a gitignored file is not
# a gate. Tests get the shipped defaults; a test that wants another value
# monkeypatches the constant in the module that consumes it.
UNDER_PYTEST = "pytest" in sys.modules
if not UNDER_PYTEST:
    load_dotenv(ENV_PATH)


class AccountSlot(BaseModel):
    slot: str  # slot id from ACCOUNT_SLOTS, e.g. "A"
    display_name: str
    ig_user_id: str
    # SecretStr so accidental logging/model_dump prints '**********',
    # never the raw token
    ig_token: SecretStr
    tt_token: SecretStr


# Slot ids become session directory names and media filename prefixes, so
# they must be filesystem-safe. Fail loudly at import: a malformed token
# silently dropping an account is worse than refusing to start.
_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_slot_ids(raw: str) -> list[str]:
    """Parse an ACCOUNT_SLOTS value ("A,B,C,D") into a validated, deduped
    slot-id list. Empty/whitespace input yields the historical default."""
    ids = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not _SLOT_ID_RE.match(token):
            raise ValueError(
                f"ACCOUNT_SLOTS contains invalid slot id '{token}' — "
                "use letters, digits, '_' or '-' only."
            )
        if token not in ids:
            ids.append(token)
    return ids or ["A", "B", "C"]


SLOT_IDS = parse_slot_ids(os.getenv("ACCOUNT_SLOTS", ""))


# Numeric knobs are parsed through these rather than a bare int()/float()
# (review 2026-07-26, finding #8). A bare cast on a typo'd value raises a
# ValueError naming neither the variable nor the file, at import time, before
# any logging exists — so the server dies with "invalid literal for int() with
# base 10: '6h'" and the maintainer has to guess which knob it came from.
# Same fail-loudly rationale as parse_slot_ids above: refusing to start beats
# silently running with a default the maintainer did not choose.
def _env_number(name: str, default, cast, kind: str):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name} in credentials.env must be {kind}, got {raw!r}. "
            f"Fix the value or delete the line to use the default ({default})."
        ) from None


def _env_int(name: str, default: int) -> int:
    return _env_number(name, default, int, "a whole number")


def _env_float(name: str, default: float) -> float:
    return _env_number(name, default, float, "a number")


def env_bool(name: str, default: bool) -> bool:
    """Parse a boolean knob. True only for the literal "true", any casing.

    Deliberately does *not* follow `_env_number`'s empty-means-default rule,
    and that asymmetry is the whole point (tech-debt audit BE-14, #36).
    `HEADLESS=` with nothing after it parses to False today — a visible
    browser. Under empty-means-default it would flip to True, and headless
    Chrome advertises a mismatched user agent, client hints and colour depth
    to Instagram. Turning a blank line into a different browser identity is a
    live-identity change, not a style cleanup, so this helper reproduces the
    existing `os.getenv(name, "<default>").lower() == "true"` expression
    exactly rather than improving on it.

    Public, unlike `_env_int`/`_env_float`, because `main.py` parses
    SCHEDULER_ENABLED with it — importing a `_`-prefixed name across modules
    would be worse than the duplication this removes.

    No `.strip()`, deliberately. The first version of this helper added one,
    which looked like an obvious courtesy and was in fact the exact live
    identity change the paragraph above refuses: `HEADLESS=" true "` parsed
    False (visible) before and True (headless) after. Three independent
    reviewers caught it. "Reproduces the old expression exactly" has to mean
    exactly, including the parts that look like bugs —
    `test_env_bool_matches_the_expression_it_replaced` pins it differentially
    against the original expression rather than against a hand-written table.
    """
    raw = os.getenv(name, "true" if default else "false")
    return raw.lower() == "true"


def _slot_display_name(slot_id: str) -> str:
    label_file = IG_SESSIONS_DIR / slot_id / "LABEL"
    if label_file.is_file():
        label = label_file.read_text().strip()
        if label:
            return label
    return os.getenv(f"IG_ACCOUNT_{slot_id}_NAME", f"Account {slot_id}")


def get_accounts() -> list[AccountSlot]:
    slots = []
    for slot_id in SLOT_IDS:
        slots.append(AccountSlot(
            slot=slot_id,
            display_name=_slot_display_name(slot_id),
            ig_user_id=os.getenv(f"IG_ACCOUNT_{slot_id}_ID", ""),
            ig_token=os.getenv(f"IG_ACCOUNT_{slot_id}_TOKEN", ""),
            tt_token=os.getenv(f"TT_ACCOUNT_{slot_id}_TOKEN", ""),
        ))
    return slots


ANTHROPIC_API_KEY = SecretStr(os.getenv("ANTHROPIC_API_KEY", ""))


def check_startup_config() -> list[str]:
    """Return human-readable problems with the current configuration.

    The empty-string default above is load-bearing: it keeps imports and the
    test suite working without a credentials.env. But it also meant a missing
    key surfaced only when a caption was actually requested — as an Anthropic
    auth error mid-run, which reads like an outage rather than a setup mistake
    (tech-debt audit BE-12). Checking at startup names the real cause while
    the maintainer is still watching the console.

    Returns problems rather than raising so the caller decides how loud to be,
    and so this is testable without manipulating process state.
    """
    problems = []

    # A typo'd LOG_LEVEL falls back to INFO rather than raising, so without
    # this the maintainer would set LOG_LEVEL=WARN, see the full INFO
    # narration anyway, and have no way to tell the setting was rejected.
    # Reported through this channel specifically so it needs no print of its
    # own — see tests/golden/output_catalogue.txt.
    if resolve_level(LOG_LEVEL) is None:
        problems.append(
            f"LOG_LEVEL={LOG_LEVEL!r} is not a level name — using "
            f"{DEFAULT_LEVEL}. Use DEBUG, INFO, WARNING, ERROR or CRITICAL."
        )

    if POST_MODE != "mock" and not ANTHROPIC_API_KEY.get_secret_value().strip():
        problems.append(
            f"ANTHROPIC_API_KEY is empty but POST_MODE={POST_MODE!r} — caption "
            f"generation will fail at request time. Set it in credentials.env, "
            f"or use POST_MODE=mock."
        )

    # A slot with a blank token looks configured — it appears in the account
    # list, in the UI, and in a scheduled batch — and fails only when that
    # slot's post is attempted (tech-debt audit BE-22).
    #
    # Gated on POST_MODE == "api" specifically, not on `!= "mock"` like the
    # check above. These tokens are consumed solely by poster.py, the official
    # Graph/TikTok API path; browser mode authenticates from the persistent
    # profiles under sessions/ and never reads them. Warning in browser mode —
    # the mode actually in use — would print on every single startup, and a
    # startup block that is noise is one the maintainer stops reading, which
    # would cost more than it buys by burying the ANTHROPIC_API_KEY line above.
    #
    # Reports rather than raises, matching the rest of this function: refusing
    # to start over an unused slot's blank token would be a worse failure than
    # the one being fixed.
    #
    # A cold reviewer raised that this block therefore cannot fire for the
    # current maintainer, who runs browser mode. Kept deliberately (maintainer,
    # 2026-07-30): POST_MODE='api' is a real supported mode and the check is
    # what a non-maintainer user would hit first. Do not re-raise as dead code.
    if POST_MODE == "api":
        for account in get_accounts():
            missing = [
                label
                for label, value in (
                    (f"IG_ACCOUNT_{account.slot}_ID", account.ig_user_id),
                    (f"IG_ACCOUNT_{account.slot}_TOKEN",
                     account.ig_token.get_secret_value()),
                    (f"TT_ACCOUNT_{account.slot}_TOKEN",
                     account.tt_token.get_secret_value()),
                )
                if not value.strip()
            ]
            if missing:
                problems.append(
                    f"Slot {account.slot} is listed in ACCOUNT_SLOTS but "
                    f"{', '.join(missing)} "
                    f"{'is' if len(missing) == 1 else 'are'} empty — that slot "
                    f"will fail at post time under POST_MODE='api'. Fill the "
                    f"value(s) in credentials.env, or drop {account.slot} from "
                    f"ACCOUNT_SLOTS."
                )
    return problems

# Failure notifications (FR-F3 / DESIGN-scheduling.md §4). NOTIFY_SERVICE is
# the off switch: "none" (default) disables notifications entirely, "ntfy"
# pushes via ntfy.sh or a self-hosted instance.
NOTIFY_SERVICE = os.getenv("NOTIFY_SERVICE", "none").lower()
# The ntfy topic string is a bearer secret: whoever knows it can send to (and
# subscribe to) the maintainer's phone on public ntfy.sh. SecretStr so it
# prints '**********' on any accidental log/model_dump — read the raw value
# only where the request is actually built (get_notifier).
NTFY_TOPIC = SecretStr(os.getenv("NTFY_TOPIC", ""))
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# POST_MODE: "mock" (fake everything), "browser" (Playwright automation), "api" (official APIs)
POST_MODE = os.getenv("POST_MODE", "mock").lower()
MOCK_MODE = POST_MODE == "mock"  # backward compat

# Whether to run browsers visibly (useful for debugging automation)
HEADLESS = env_bool("HEADLESS", True)

# RiceClipper handoff pickup ("Pull from Clipper"). HANDOFF_DIR is the shared
# directory RiceClipper writes finished batches into and this app reads from;
# it must match RiceClipper's RICECLIPPER_HANDOFF_DIR. CLIPPER_INGEST_STYLE is
# the caption style applied to pulled clips (the maintainer's daily style).
HANDOFF_DIR = Path(os.getenv("HANDOFF_DIR", "~/riceclipper-handoff")).expanduser()
CLIPPER_INGEST_STYLE = os.getenv("CLIPPER_INGEST_STYLE", "benny-blanco")

# How long a successful session health check stays valid, in seconds
# (RESEARCH-platform-detection.md F5). The scheduler runs a
# pre-flight check per slot per platform before every batch, so with 3 slots
# that was 3 extra Instagram home-page loads per batch — a load-and-leave
# pattern that is a cleaner bot signature than actually posting. Caching the
# "live" answer removes most of that traffic. Only successful checks are
# cached; expired/no_session/check_error are always re-checked so a re-login
# is picked up immediately.
SESSION_CHECK_TTL_S = _env_int("SESSION_CHECK_TTL_S", 6 * 60 * 60)

# Which platforms get a *browser* pre-flight health check before a scheduled
# batch (RESEARCH-platform-detection.md F5, third bullet).
#
# The check's value is narrow: check_error proceeds to post anyway, and
# no_session is a pure filesystem test needing no browser. So the browser
# load exists solely to catch the "expired" case — and on Instagram that
# load is itself a bot signature. Dropping "instagram" from this list keeps
# the free no_session filtering while eliminating the browser traffic.
#
# Default is both platforms: current behaviour, unchanged.
#
# Empty means *unset*, not "disable everything" (review 2026-07-26, finding
# #5). Previously `PREFLIGHT_CHECK_PLATFORMS=` parsed to an empty frozenset,
# which made check_session return "check_disabled" for every platform — a
# blank line in credentials.env silently switched off the whole pre-flight
# check with no signal anywhere. Empty now falls back to the default, matching
# parse_slot_ids above. To genuinely disable both, say so explicitly:
# `PREFLIGHT_CHECK_PLATFORMS=none`.
PREFLIGHT_PLATFORMS_DEFAULT = ("instagram", "tiktok")
_KNOWN_PLATFORMS = frozenset(PREFLIGHT_PLATFORMS_DEFAULT)


def parse_preflight_platforms(raw: str) -> frozenset[str]:
    """Parse a PREFLIGHT_CHECK_PLATFORMS value into a platform set.

    Empty/whitespace yields the default (both platforms). The literal "none"
    disables the browser pre-flight entirely. Unknown names are rejected
    rather than ignored: a typo'd "instgram" would otherwise disable
    Instagram's check silently, which is the same failure mode as the empty
    string this function exists to fix.
    """
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if not tokens:
        return frozenset(PREFLIGHT_PLATFORMS_DEFAULT)
    if "none" in tokens:
        if len(tokens) > 1:
            raise ValueError(
                f"PREFLIGHT_CHECK_PLATFORMS mixes 'none' with platform names "
                f"({raw!r}) — that is contradictory. Use 'none' alone to "
                f"disable the browser pre-flight, or list only the platforms "
                f"that should keep it."
            )
        return frozenset()
    unknown = sorted(set(tokens) - _KNOWN_PLATFORMS)
    if unknown:
        raise ValueError(
            f"PREFLIGHT_CHECK_PLATFORMS contains unknown platform(s) "
            f"{', '.join(repr(u) for u in unknown)}. Valid values are "
            f"{', '.join(sorted(_KNOWN_PLATFORMS))}, or 'none' to disable."
        )
    return frozenset(tokens)


PREFLIGHT_CHECK_PLATFORMS = parse_preflight_platforms(
    os.getenv("PREFLIGHT_CHECK_PLATFORMS", "")
)

# Randomised delay between account slots in a posting run, in seconds
# (RESEARCH-platform-detection.md F4). Slots posted back-to-back
# with no gap, which is part of the "one device, many accounts, immediate
# succession" pattern that links accounts.
#
# Enabled by default at 1-3 minutes (maintainer decision 2026-07-26). It
# originally defaulted to 0/off, but an off-by-default pacing control is a
# dead switch: nothing would ever have used it. Set both to 0 to disable.
#
# A run with 3 slots now takes 2-6 minutes longer. The UI reports the gap as
# a countdown (progress status "waiting"), so a paused run cannot be
# mistaken for a hung one.
INTER_SLOT_DELAY_MIN_S = _env_float("INTER_SLOT_DELAY_MIN_S", 60.0)
INTER_SLOT_DELAY_MAX_S = _env_float("INTER_SLOT_DELAY_MAX_S", 180.0)

# Console verbosity for the `riceposter` logger (#26). Left at INFO, browser
# automation narrates every step exactly as it always has; raised to WARNING,
# only degraded and failed states are printed.
#
# Configuring logging *here* rather than in backend/__init__.py is deliberate
# and order-dependent: load_dotenv() has already run by this point, and every
# backend module that logs imports this one, so the handler is guaranteed to
# exist before the first record without importing config from logging_setup
# (which would be a cycle). logging_setup stays pure mechanism and this file
# keeps ownership of reading the environment, as it does for every other
# setting above.
LOG_LEVEL = os.getenv("LOG_LEVEL", DEFAULT_LEVEL)
configure_logging(LOG_LEVEL)
