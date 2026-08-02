"""The live-call tripwire's coverage list, pinned as a contract.

Batch 8 (#37) moves fixtures into `tests/conftest.py`. That file also holds the
autouse `_no_live_calls` tripwire — the suite's only guard against a test
reaching a live posting path. A "centralizing" refactor that dropped a target
would leave every quality gate green while removing the guard, which is the
same silent-failure shape as Batches 3, 6 and 7. So the coverage list is pinned
here *before* the file is touched.

Two independent locks, because either alone is defeatable:

1. **Behavioural** — each guarded entry point is called inside a test and must
   raise the tripwire's own error. This catches a target that is still named in
   conftest but no longer actually patched.
2. **Structural** — the set of `monkeypatch.setattr` targets *inside the
   `_no_live_calls` function body* must equal the pinned table exactly. This
   catches a target removed outright, and a target added without being pinned.
   `test_audit_phase2.py::test_spec_documents_every_tripwire_target` only
   asserts a substring appears somewhere in the file, which a refactor that
   moved the patch calls into a never-invoked helper would still satisfy.

The two locks are not redundant, and neither is sufficient alone. Cold review
on 2026-07-31 defeated the structural lock — see
`test_slice_stops_at_a_relocated_helper` — and only the behavioural lock
noticed. Treat the structural one as a cheap early warning rather than a
guarantee.

Five of the twelve entry points had no behavioural coverage anywhere in the
suite before this file existed: the three `backend.main` aliases,
`NtfyNotifier.send`, and the Anthropic client. The other seven were covered,
but scattered across `test_audit_phase1.py`, `test_poster_internals.py` and
`test_scheduling_slice1.py` with no single place stating the whole contract.

Do not weaken this file to make a refactor pass. If a target legitimately
leaves the tripwire, that is a maintainer decision about a safety gate.
"""

import ast
import asyncio
import importlib
import re
from pathlib import Path

import pytest

from tests.source_probe import real_source

CONFTEST = Path(__file__).parent / "conftest.py"

# (import path, attribute, kind, expected exception, message fragment)
#
# `kind` is "coroutine" for the async entry points and "class" for the caption
# client, which is guarded by replacing the class itself. The distinction is
# load-bearing in `test_guarded_entry_point_raises_when_reached` below.
#
# The exception types differ by design: the posting paths share one
# AssertionError, while `_run_session_check` raises RuntimeError and the
# notifier and caption client carry their own text. Pinning the actual type and
# message means a stub replaced by a silently-passing no-op fails here rather
# than letting the test that relies on it pass for the wrong reason.
GUARDED = [
    ("backend.main", "post_all_browser", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.main", "post_all_api", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.main", "generate_caption", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.poster_browser", "post_all", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.poster", "post_all", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.instagram", "post_media", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.tiktok", "post_media", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.instagram_browser", "post_media", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.tiktok_browser", "post_media", "coroutine", AssertionError, "live posting/caption path"),
    ("backend.session_manager", "_run_session_check", "coroutine", RuntimeError, "tripwire"),
    ("backend.notifier", "NtfyNotifier.send", "coroutine", AssertionError, "tripwire: NtfyNotifier.send"),
    ("backend.captions", "anthropic.AsyncAnthropic", "class", AssertionError, "tripwire: a test constructed"),
]

# conftest imports some modules under private aliases so it can capture the
# real functions before the guard replaces them, and patches two attributes on
# something other than a module. Map each source spelling to the module it
# refers to plus the prefix the pinned attribute carries.
_SOURCE_ALIASES = {
    "main": ("backend.main", ""),
    "poster_browser": ("backend.poster_browser", ""),
    "poster": ("backend.poster", ""),
    "instagram": ("backend.instagram", ""),
    "tiktok": ("backend.tiktok", ""),
    "_instagram_browser": ("backend.instagram_browser", ""),
    "_tiktok_browser": ("backend.tiktok_browser", ""),
    "session_manager": ("backend.session_manager", ""),
    "notifier.NtfyNotifier": ("backend.notifier", "NtfyNotifier."),
    "captions.anthropic": ("backend.captions", "anthropic."),
}


def _resolve(import_path: str, attribute: str):
    """Import `import_path` and walk `attribute`, which may be dotted."""
    obj = importlib.import_module(import_path)
    for part in attribute.split("."):
        obj = getattr(obj, part)
    return obj


# The three `backend.main` entries are re-exported aliases (main.py:24-26), not
# functions defined in main.py, so the signature must be read from the module
# that actually defines each one.
_ALIAS_DEFINITIONS = {
    ("backend.main", "post_all_browser"): ("backend.poster_browser", "post_all"),
    ("backend.main", "post_all_api"): ("backend.poster", "post_all"),
    ("backend.main", "generate_caption"): ("backend.captions", "generate_caption"),
}


def _real_required_positional(import_path: str, attribute: str) -> list[str]:
    """Required positional parameters of the entry point *as written in its file*.

    Read from the module source rather than from the live attribute, because
    during a test the live attribute IS the tripwire stub, whose
    `(*args, **kwargs)` signature has no required parameters at all. Inspecting
    it would make the caller's assertion vacuous in exactly the direction that
    matters — it would report "no required parameter" for every entry point and
    fire constantly, or, if inverted, never fire. This is the same trap
    `tests/source_probe.py` exists to avoid, so it reuses that helper.
    """
    import_path, attribute = _ALIAS_DEFINITIONS.get(
        (import_path, attribute), (import_path, attribute)
    )
    module = importlib.import_module(import_path)
    leaf = attribute.split(".")[-1]
    function = ast.parse(real_source(module, leaf)).body[0]

    positional = function.args.posonlyargs + function.args.args
    optional = len(function.args.defaults)
    required = positional[: len(positional) - optional] if optional else positional
    return [arg.arg for arg in required]


@pytest.mark.parametrize(
    "import_path,attribute,kind,exc_type,fragment",
    GUARDED,
    ids=[f"{m.split('.')[-1]}.{a}" for m, a, _, _, _ in GUARDED],
)
def test_guarded_entry_point_raises_when_reached(
    import_path, attribute, kind, exc_type, fragment
):
    """Reaching a guarded entry point un-stubbed must fail loudly.

    Called with no arguments: every stub takes `(*args, **kwargs)`, so a
    zero-arg call reaches the guard rather than a signature error.

    That zero-arg call is only safe because of an invariant, so the invariant
    is asserted rather than assumed (cold review, 2026-07-31). Every guarded
    *coroutine* has at least one required positional parameter, so if the guard
    were ever absent the real function would raise TypeError before running any
    of its body — it could not reach a platform. If a future refactor gave one
    of them all-default parameters, this test would start invoking the real
    posting path in exactly the scenario the file exists to detect. The
    assertion below fails first instead.

    The caption client is exempt from that invariant because it is a class, and
    `anthropic.AsyncAnthropic()` is constructible with no arguments. Building a
    client object opens no connection, so an absent guard here costs nothing at
    construction time; the guard exists to stop the *request* that
    `generate_caption` would make next.
    """
    obj = _resolve(import_path, attribute)

    if kind == "coroutine":
        required = _real_required_positional(import_path, attribute)
        assert required, (
            f"{import_path}.{attribute} no longer has a required positional "
            "parameter. The zero-arg call below is only safe while it does — "
            "without it, a disarmed guard would let this test invoke the real "
            "function. Give this entry a dedicated call instead of relaxing "
            "this assertion."
        )

    with pytest.raises(exc_type, match=re.escape(fragment)):
        asyncio.run(obj())


def test_caption_path_is_guarded_when_a_key_is_present(monkeypatch):
    """The caption-client guard must fire in the only scenario that reaches it.

    `captions.generate_caption` raises on an empty key *before* it builds a
    client (`captions.py`, the `ANTHROPIC_API_KEY` check), and the key is empty
    in a default test run and in CI because `config.py` skips `load_dotenv`
    under pytest. So the parametrized construction check above never exercises
    the realistic path.

    This supplies a key — as a developer with `ANTHROPIC_API_KEY` exported in
    their shell effectively does, since `os.getenv` picks that up regardless —
    and then calls the real `generate_caption` without installing a fake. It
    must trip the guard rather than reach the API.

    Cold review, 2026-07-31: before the guard existed this call constructed a
    real `anthropic.AsyncAnthropic` and issued a billed request. No test did
    that, because all eleven direct callers install a fake, but nothing
    stopped one from being written.

    Makes no network call: the guard raises inside `__init__`.
    """
    from pydantic import SecretStr

    from backend import captions

    monkeypatch.setattr(captions, "ANTHROPIC_API_KEY", SecretStr("sk-not-a-real-key"))

    with pytest.raises(AssertionError, match="tripwire: a test constructed"):
        asyncio.run(captions.generate_caption("video", "a topic"))


def _slice_fixture_body(source: str) -> str:
    """The source of `_no_live_calls` alone, not the whole conftest file.

    Sliced textually rather than via `inspect.getsource` so that a patch call
    relocated *out* of the fixture — into a helper the fixture never calls, or
    into module scope where it never runs — drops out of this slice and fails
    the structural check below. Reading the whole file would not notice.

    The slice ends at the next **top-level construct**: a decorator, `def`, or
    `class` at column 0. It used to end at the next `"@pytest.fixture"`, which
    was not enough — a plain `def` helper inserted between this fixture and the
    next decorated one landed *inside* the slice, so patches relocated into a
    never-invoked helper were still counted as though the fixture ran them.
    Found and reproduced by cold review, 2026-07-31; the file previously
    claimed the opposite. `test_slice_stops_at_a_relocated_helper` pins it.
    """
    start = source.index("def _no_live_calls(")
    body_start = source.index("\n", start) + 1
    match = re.search(r"^(?:@|def |class )", source[body_start:], re.M)
    end = body_start + match.start() if match else len(source)
    return source[start:end]


def _tripwire_body() -> str:
    return _slice_fixture_body(CONFTEST.read_text())


def test_slice_stops_at_a_relocated_helper():
    """The structural lock must not be defeatable by an intervening helper.

    Cold review, 2026-07-31, reproduced against the real `conftest.py`: moving
    the two browser-leaf patches into a never-invoked module-level `def` placed
    before the next `@pytest.fixture` left the structural check **passing**
    while the guard was genuinely disarmed. Only the behavioural lock caught
    it, and this file's docstring claimed the structural one would.

    Asserted against a synthetic source rather than by mutating `conftest.py`,
    so the regression is pinned without a test that edits the safety file.
    """
    source = (
        "def _no_live_calls(monkeypatch):\n"
        '    monkeypatch.setattr(main, "post_all_browser", _blocked)\n'
        "\n\n"
        "def _never_called(monkeypatch):\n"
        '    monkeypatch.setattr(main, "generate_caption", _blocked)\n'
        "\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def reset_run_guard():\n"
        "    pass\n"
    )

    body = _slice_fixture_body(source)

    assert "post_all_browser" in body, "the fixture's own patch fell out of the slice"
    assert "generate_caption" not in body, (
        "a patch relocated into a never-invoked helper leaked into the slice, "
        "so the structural lock would count it as though the fixture ran it"
    )


def test_tripwire_patches_exactly_the_pinned_targets():
    """The fixture's setattr targets must equal GUARDED, in both directions.

    A refactor that drops a target fails here; so does one that adds a target
    without pinning it. The second direction matters as much as the first —
    an unpinned guard is one nobody will notice disappearing next time.
    """
    body = _tripwire_body()

    found = set()
    for target, attribute in re.findall(
        r"monkeypatch\.setattr\(\s*([\w.]+)\s*,\s*[\"'](\w+)[\"']", body
    ):
        assert target in _SOURCE_ALIASES, (
            f"conftest patches an unrecognised target {target!r}. If a new live "
            f"entry point joined the tripwire, add it to GUARDED and to "
            f"_SOURCE_ALIASES here — an unpinned guard is not a guard."
        )
        module, prefix = _SOURCE_ALIASES[target]
        found.add((module, f"{prefix}{attribute}"))

    pinned = {(module, attribute) for module, attribute, _, _, _ in GUARDED}

    assert found == pinned, (
        "The conftest tripwire's coverage changed.\n"
        f"  Dropped (pinned but no longer patched): {sorted(pinned - found)}\n"
        f"  Added (patched but not pinned):         {sorted(found - pinned)}\n"
        "Do not edit GUARDED to make this pass unless a maintainer decided to "
        "change the safety gate itself."
    )


def test_tripwire_is_autouse():
    """The guard must apply without being requested.

    Kept for its failure message, not for unique detection: demoting
    `_no_live_calls` to an opt-in fixture already turns all twelve parametrized
    cases above red. This one says *why* in a single line, instead of leaving a
    reader to infer a fixture-registration change from twelve TypeErrors.

    An earlier version of this docstring claimed the behavioural tests would
    still pass under demotion "via the autouse chain". There is no such chain —
    no other autouse fixture requests `_no_live_calls` — and the claim was
    wrong. Corrected after cold review, 2026-07-31.
    """
    body = CONFTEST.read_text()
    decorator = body[: body.index("def _no_live_calls(")].rsplit("@pytest.fixture", 1)[-1]
    assert "autouse=True" in decorator, (
        "_no_live_calls is no longer autouse. The tripwire only protects tests "
        "that opt in, which is every test in the suite except the ones that "
        "forget — exactly the population it exists to catch."
    )
