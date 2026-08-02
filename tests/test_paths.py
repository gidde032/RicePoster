"""Every filesystem path the backend derives must keep its exact current value.

Written 2026-07-30 *before* the #27 refactor that centralises path construction
in `config.py`, so it proves equivalence rather than describing whatever the
new code happens to produce. It passed unchanged on `fd37a8b` (Batch 2's squash
merge, the commit this branch is based on) before a single path line moved.

The reason this test exists at all, rather than trusting review: a wrong
`sessions/` derivation does not raise. `launch_persistent_context` treats a
non-existent `user_data_dir` as "make a fresh profile", so a typo'd path opens
an unauthenticated Chrome against a live account — one of the strongest
automation signals there is — and the run looks completely normal from the
console. That is worse than a crash. The three literals under `sessions/` (rows 2, 4, 6, 8 below) are
the load-bearing ones; the rest are pinned because there is no reason not to.

Deliberately does not read the constants through the module attribute at *call*
time. `tests/conftest.py` redirects several of these away from the real files
with autouse fixtures (`IG_SESSIONS_DIR`, `HEALTH_CACHE_FILE`,
`HISTORY_FILE`), so an assertion made inside a test function would follow the
redirect and pass vacuously — the same trap `test_history_isolation.py`
documents. The values are snapshotted at module import, which pytest performs
at collection, before any fixture runs. `_snapshot_predates_the_conftest_redirects`
below proves that ordering still holds rather than assuming it.
"""

import ast
from pathlib import Path

from backend import captions, config, instagram_browser, main, scheduler
from backend import queue as queue_mod
from backend import session_manager, tiktok_browser

# Captured at collection time: the real values, before conftest's autouse
# redirects take effect.
_SNAPSHOT = {
    "config.ENV_PATH": config.ENV_PATH,
    "config.IG_SESSIONS_DIR": config.IG_SESSIONS_DIR,
    "config.MEDIA_DIR": config.MEDIA_DIR,
    "instagram_browser.SESSIONS_DIR": instagram_browser.SESSIONS_DIR,
    "instagram_browser.DEBUG_DIR": instagram_browser.DEBUG_DIR,
    "tiktok_browser.SESSIONS_DIR": tiktok_browser.SESSIONS_DIR,
    "tiktok_browser.DEBUG_DIR": tiktok_browser.DEBUG_DIR,
    "session_manager.HEALTH_CACHE_FILE": session_manager.HEALTH_CACHE_FILE,
    "queue.QUEUE_FILE": queue_mod.QUEUE_FILE,
    "queue.QUEUE_MEDIA_DIR": queue_mod.QUEUE_MEDIA_DIR,
    "captions.PROMPTS_DIR": captions.PROMPTS_DIR,
    "scheduler.HISTORY_FILE": scheduler.HISTORY_FILE,
    "main.HISTORY_FILE": main.HISTORY_FILE,
    "main.FRONTEND_DIR": main.FRONTEND_DIR,
}

# The literal each name resolved to on fd37a8b, relative to the project root.
# Written out segment by segment so a table row is readable as a path.
_EXPECTED = {
    "config.ENV_PATH": ("credentials.env",),
    "config.IG_SESSIONS_DIR": ("sessions", "instagram"),
    "config.MEDIA_DIR": ("media",),
    "instagram_browser.SESSIONS_DIR": ("sessions", "instagram"),
    "instagram_browser.DEBUG_DIR": ("debug",),
    "tiktok_browser.SESSIONS_DIR": ("sessions", "tiktok"),
    "tiktok_browser.DEBUG_DIR": ("debug",),
    "session_manager.HEALTH_CACHE_FILE": ("sessions", ".health_cache.json"),
    "queue.QUEUE_FILE": ("queue.jsonl",),
    "queue.QUEUE_MEDIA_DIR": ("queue_media",),
    "captions.PROMPTS_DIR": ("prompts",),
    "scheduler.HISTORY_FILE": ("history.jsonl",),
    "main.HISTORY_FILE": ("history.jsonl",),
    "main.FRONTEND_DIR": ("frontend",),
}


def _project_root() -> Path:
    """The repo root, derived from this test file rather than from any backend
    module — so the assertion cannot agree with a refactor that redefines the
    root incorrectly in `config.py`."""
    return Path(__file__).parent.parent


def _expected(name: str) -> Path:
    root = _project_root()
    for segment in _EXPECTED[name]:
        root = root / segment
    return root


def _backend_modules() -> list[Path]:
    """Every module under `backend/`, recursively.

    `rglob`, not `glob`: the first version scanned only the top level, so a
    future `backend/browsers/` subpackage would have been unguarded.
    """
    import backend

    return sorted(
        p
        for p in Path(backend.__file__).parent.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _string_literals(node: ast.AST) -> list[str]:
    """Every string literal in a subtree, including f-string fragments."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _is_path_call(node: ast.AST) -> bool:
    """`Path(...)`, `x.joinpath(...)`, or `os.path.join(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "Path"
    if isinstance(func, ast.Attribute):
        return func.attr in {"Path", "joinpath", "join"}
    return False


def _path_literals_mentioning(module: Path, needle: str) -> list[str]:
    """String literals inside path-construction expressions that mention
    `needle`. Deduped and sorted so the failure message is stable."""
    tree = ast.parse(module.read_text())
    found = set()
    for node in ast.walk(tree):
        is_join = isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        if not (is_join or _is_path_call(node)):
            continue
        for literal in _string_literals(node):
            if needle in literal:
                found.add(literal)
    return sorted(found)


def _rebuilds_root(module: Path) -> bool:
    """True if the module walks `.parent.parent` off an expression mentioning
    `__file__` — however many calls sit in between (`.resolve()`, `.absolute()`).
    """
    tree = ast.parse(module.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "parent"):
            continue
        inner = node.value
        if not (isinstance(inner, ast.Attribute) and inner.attr == "parent"):
            continue
        if any(
            isinstance(sub, ast.Name) and sub.id == "__file__"
            for sub in ast.walk(inner)
        ):
            return True
    return False


def test_every_pinned_path_is_covered():
    """Snapshot and expectation tables must not drift apart.

    A path silently dropped from one table would otherwise stop being pinned
    without any test going red.
    """
    assert set(_SNAPSHOT) == set(_EXPECTED)
    assert len(_SNAPSHOT) == 14


def test_snapshot_predates_the_conftest_redirects():
    """Guard the collection-time assumption this whole module rests on.

    If pytest ever applied autouse fixtures before importing test modules, the
    snapshot would hold temp paths and every assertion below would compare a
    redirect against itself. These three names are redirected by autouse
    fixtures, so at call time they must differ from the snapshot — that
    difference is the proof the snapshot was taken first.
    """
    assert config.IG_SESSIONS_DIR != _SNAPSHOT["config.IG_SESSIONS_DIR"]
    assert main.HISTORY_FILE != _SNAPSHOT["main.HISTORY_FILE"]
    assert (
        session_manager.HEALTH_CACHE_FILE
        != _SNAPSHOT["session_manager.HEALTH_CACHE_FILE"]
    )


def test_pinned_paths_equal_their_pre_refactor_literals():
    """The table itself. Reported all at once: seeing one row move tells you
    less than seeing which rows moved together."""
    moved = {
        name: (str(actual), str(_expected(name)))
        for name, actual in _SNAPSHOT.items()
        if actual != _expected(name)
    }
    assert not moved, "centralised paths no longer match their literals: " + repr(moved)


def test_session_paths_are_string_identical_not_merely_equivalent():
    """`user_data_dir=` takes `str(path)`, so string form is what reaches
    Chrome. A `.resolve()` slipped into a centralised root would still compare
    equal under `Path.__eq__` on a case-insensitive filesystem while producing
    a different string — and a different string is a different profile.
    """
    root = str(_project_root())
    assert str(_SNAPSHOT["instagram_browser.SESSIONS_DIR"]) == (
        root + "/sessions/instagram"
    )
    assert str(_SNAPSHOT["tiktok_browser.SESSIONS_DIR"]) == root + "/sessions/tiktok"
    assert str(_SNAPSHOT["config.IG_SESSIONS_DIR"]) == root + "/sessions/instagram"
    assert str(_SNAPSHOT["session_manager.HEALTH_CACHE_FILE"]) == (
        root + "/sessions/.health_cache.json"
    )


def test_sessions_is_derived_in_exactly_one_place():
    """The invariant #27 buys, and the one worth defending hardest.

    Source-level, because the values above would still agree if a future change
    re-introduced a second independent derivation that happened to be correct
    today. The point is that there is nothing to keep *in sync*.

    Matched semantically, over the AST, not by text. The first version of this
    test used the regex `/\\s*["']sessions["']`, and cold review demonstrated by
    execution that four plausible spellings walked straight past it:

        PROJECT_ROOT / "sessions/instagram"      # segment not its own literal
        PROJECT_ROOT.joinpath("sessions", ...)   # no `/` operator
        os.path.join(str(PROJECT_ROOT), "sessions", ...)
        Path(f"{PROJECT_ROOT}/sessions")         # f-string, not a Constant

    Each would have re-introduced exactly the divergent `user_data_dir` this
    guard exists to prevent, with the suite green. It now collects every string
    literal that appears anywhere inside a path-construction expression —
    `a / b`, `Path(...)`, `.joinpath(...)`, `os.path.join(...)` — including
    f-string fragments, and asks whether any of them mentions "sessions".

    `main.py`'s `"sessions": session_status` key in the `/api/accounts` payload
    is not in a path context and is correctly ignored; a guard that flagged it
    would be noise the next person silences rather than a guard.
    """
    offenders = {
        module.name: hits
        for module in _backend_modules()
        if (hits := _path_literals_mentioning(module, "sessions"))
    }

    assert offenders == {"config.py": ["sessions"]}, (
        "the sessions/ path segment is built outside config.py's layout "
        f"section: {offenders!r}"
    )


def test_no_module_rebuilds_the_project_root_for_itself():
    """A `.parent.parent` walk off `__file__` under `backend/` means a path
    that escaped the layout section. config.py owns the one occurrence.

    Also AST-matched: the previous substring check for the exact text
    `Path(__file__).parent.parent` missed `Path(__file__).resolve().parent
    .parent`, which produces a *different string* for the same directory and
    so a different Chrome profile.
    """
    offenders = {
        module.name for module in _backend_modules() if _rebuilds_root(module)
    }
    assert offenders == {"config.py"}, (
        f"modules deriving the project root independently of config: {offenders!r}"
    )


def test_the_two_duplicate_derivations_agree():
    """#27 exists because these pairs are derived independently today. Pin the
    agreement now, so centralising them cannot be what breaks it."""
    assert (
        _SNAPSHOT["instagram_browser.SESSIONS_DIR"]
        == _SNAPSHOT["config.IG_SESSIONS_DIR"]
    )
    assert _SNAPSHOT["main.HISTORY_FILE"] == _SNAPSHOT["scheduler.HISTORY_FILE"]
    assert (
        _SNAPSHOT["instagram_browser.DEBUG_DIR"] == _SNAPSHOT["tiktok_browser.DEBUG_DIR"]
    )
