"""#42 (tech-debt audit BE-15): the Windows Chrome lookup, made executable.

`tiktok_browser._find_system_chrome()` builds two candidate paths inside its
`system == "Windows"` branch. Batch 3 converted the rest of the module to
`pathlib` but deferred these two lines, and the deferral reasoning is the
interesting part: the branch is unreachable on the maintainer's macOS
machine, it lives in a module that drives a real account, and the claim that
`Path(a) / b` equals `os.path.join(a, b)` could be *argued* from this host
but not *demonstrated*. Every other BE-15 sibling finding got behavioural or
AST-level proof; this one would have shipped on assertion alone.

Faking `platform.system()` closes that gap. The equivalence is asserted
against `os.path.join` itself rather than against a hardcoded expectation,
so the test states the actual claim and stays true on a Windows host too.
"""

import os
from pathlib import Path

import pytest

from backend import tiktok_browser

CHROME_TAIL = r"Google\Chrome\Application\chrome.exe"


@pytest.fixture
def windows_host(monkeypatch):
    """Make `_find_system_chrome()` take its Windows branch on any host."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setitem(os.environ, "LOCALAPPDATA", r"C:\Users\rice\AppData\Local")
    monkeypatch.setitem(os.environ, "PROGRAMFILES", r"C:\Program Files")


@pytest.fixture
def everything_exists(monkeypatch):
    """Treat every candidate as present, so the first one is returned."""
    monkeypatch.setattr(Path, "exists", lambda self: True)


@pytest.fixture
def nothing_exists(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)


class TestWindowsCandidatePaths:
    def test_local_appdata_candidate_equals_os_path_join(
        self, windows_host, everything_exists
    ):
        """The exact equivalence claim #42 could not previously execute."""
        found = tiktok_browser._find_system_chrome()
        assert found == os.path.join(os.environ["LOCALAPPDATA"], CHROME_TAIL)

    def test_program_files_candidate_equals_os_path_join(
        self, windows_host, monkeypatch
    ):
        """The second candidate, reached when the first is absent."""
        appdata = os.environ["LOCALAPPDATA"]
        monkeypatch.setattr(
            Path, "exists", lambda self: not str(self).startswith(appdata)
        )
        found = tiktok_browser._find_system_chrome()
        assert found == os.path.join(os.environ["PROGRAMFILES"], CHROME_TAIL)

    def test_windows_semantics_match_ntpath_not_just_posix(self):
        """Cold review, 2026-07-31 — the assertions above are weaker than they look.

        On a POSIX host, both `Path(a) / tail` and `os.path.join(a, tail)`
        reduce to *posixpath* joins, so those tests compare one POSIX
        operation against another. The Windows behaviour #42 actually cared
        about — `WindowsPath` parsing the backslash tail into separate
        components and re-joining them — never executes there.

        `PureWindowsPath` and `ntpath` are pure-algorithm and importable on
        any OS, so the real claim can be checked from a Mac: the two produce
        the same string even though pathlib splits the tail and ntpath does
        not.
        """
        import ntpath
        from pathlib import PureWindowsPath

        for base in (r"C:\Users\rice\AppData\Local", r"C:\Program Files", ""):
            assert str(PureWindowsPath(base) / CHROME_TAIL) == ntpath.join(
                base, CHROME_TAIL
            ), f"pathlib and ntpath disagree for {base!r}"

        # The split really does happen — this is what the source comment
        # describes, and asserting it stops that comment silently going stale.
        assert PureWindowsPath(CHROME_TAIL).parts == (
            "Google", "Chrome", "Application", "chrome.exe"
        )

    def test_the_backslash_tail_is_not_split_into_components(
        self, windows_host, everything_exists
    ):
        """Guards the one way this refactor could silently change behaviour.

        Writing the tail as `Path("Google") / "Chrome" / ...` looks tidier and
        is wrong: on a POSIX host it renders with forward slashes, so the
        produced string stops matching `os.path.join` and this file's other
        assertions would have to be weakened to keep passing. Keeping it a
        single component makes the result byte-identical on both platforms.
        """
        found = tiktok_browser._find_system_chrome()
        assert CHROME_TAIL in found
        assert "Google/Chrome" not in found

    def test_missing_environment_variables_do_not_crash(
        self, monkeypatch, everything_exists
    ):
        """A Windows box without LOCALAPPDATA set still gets an answer.

        `os.path.join("", tail)` returned the bare tail; `Path("") / tail`
        must too, or the lookup would raise where it used to degrade.
        """
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.delitem(os.environ, "LOCALAPPDATA", raising=False)
        monkeypatch.delitem(os.environ, "PROGRAMFILES", raising=False)
        assert tiktok_browser._find_system_chrome() == os.path.join("", CHROME_TAIL)


class TestOtherPlatformsAreUnaffected:
    """#42 touches one branch; the others must be provably untouched."""

    def test_macos_candidates_are_unchanged(self, monkeypatch, everything_exists):
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        assert tiktok_browser._find_system_chrome() == (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

    def test_an_unknown_platform_still_returns_none(
        self, monkeypatch, everything_exists
    ):
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Plan9")
        assert tiktok_browser._find_system_chrome() is None

    def test_nothing_installed_returns_none(self, windows_host, nothing_exists):
        assert tiktok_browser._find_system_chrome() is None
