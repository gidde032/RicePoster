"""The test suite must never write to the maintainer's real history.jsonl.

Found 2026-07-27 while reading history.jsonl as evidence for the Phase 2
HEADLESS/F2 question. `main.HISTORY_FILE` was a module-level constant with no
env override and no conftest redirect, so every `_append_history` call in the
suite appended to the real, gitignored file at the repo root. Measured: a full
`pytest tests/ -q` run grew history.jsonl by 7 rows.

Two costs. The history UI filled with fixture rows (`ig_x`, `ig_post_ok_A`,
`ig_ok` against `A_clip.mp4` — 354 of them by the time this was caught), and
the file stopped being trustworthy as forensic evidence about real runs, which
is exactly what it was being consulted for.

This is the write-direction twin of the existing hermeticity rule that
`test_gates.py` enforces for reads (config.py's UNDER_PYTEST skip, conftest's
`_IG_SESSIONS_DIR` redirect). `scheduler.py` was already clean — it takes an
injectable `history_file` parameter.
"""

import json

from backend import main
from backend.models import PostResult


def _real_history_path():
    """The repo-root history.jsonl, derived independently of main.HISTORY_FILE.

    Deliberately does not read the constant under test — otherwise the
    assertion would follow the redirect and pass vacuously.
    """
    from pathlib import Path

    return Path(main.__file__).parent.parent / "history.jsonl"


def test_history_file_is_redirected_away_from_the_repo_root():
    """The constant itself must not point at the maintainer's file."""
    assert main.HISTORY_FILE != _real_history_path(), (
        "main.HISTORY_FILE still points at the real history.jsonl during tests"
    )


def test_append_history_does_not_touch_the_real_file():
    """Behavioural probe: writing history must leave the real file byte-identical.

    Pre-fix this failed with a 2-row growth — the append landed in the
    maintainer's file.
    """
    real = _real_history_path()
    before = real.read_bytes() if real.exists() else None

    main._append_history(
        [{"slot": "A", "media_path": "A_clip.mp4", "caption": "probe"}],
        [PostResult(slot="A", ig_post_id="ig_probe", tt_post_id="tt_probe")],
        headless_used=True,
    )

    after = real.read_bytes() if real.exists() else None
    assert after == before, (
        "_append_history wrote to the real history.jsonl at the repo root"
    )


def test_append_history_still_writes_to_the_redirected_file():
    """The redirect must not silently disable history — the row still lands."""
    main._append_history(
        [{"slot": "B", "media_path": "B_clip.mp4", "caption": "probe-b"}],
        [PostResult(slot="B", ig_post_id="ig_b")],
        headless_used=False,
    )

    rows = [
        json.loads(line)
        for line in main.HISTORY_FILE.read_text().strip().splitlines()
    ]
    assert rows[-1]["slot"] == "B"
    assert rows[-1]["ig_post_id"] == "ig_b"
    assert rows[-1]["headless"] is False


def test_get_history_reads_the_redirected_file(client):
    """The read path must follow the same redirect as the write path."""
    main._append_history(
        [{"slot": "C", "media_path": "C_clip.mp4", "caption": "probe-c"}],
        [PostResult(slot="C", ig_post_id="ig_c")],
        headless_used=True,
    )

    resp = client.get("/api/history")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert entries, "history endpoint returned nothing after a write"
    assert entries[0]["ig_post_id"] == "ig_c"
