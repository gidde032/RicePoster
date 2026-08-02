"""
Regression tests for the Instagram platform-detection fixes (F1-F6).

Source: RESEARCH-platform-detection.md (2026-07-25), written after
accounts were observed being flagged for suspicious activity within 2-3
days of being connected to automated posting.

These are **source-level assertions**, per CLAUDE.md § Testing rules:
Playwright flows cannot be E2E-tested (that would require a real browser and
real platform traffic, which is forbidden). What we can assert is that the
specific fingerprint-bearing constructs identified in the research doc are
absent from — or present in — the source of the browser modules.

The point of these tests is to stop a future edit from silently
reintroducing a fingerprint tell that took a live account-flagging incident
to find.
"""

import inspect

from backend import instagram_browser, tiktok_browser


def _module_source(module) -> str:
    return inspect.getsource(module)


# --- F1: no hardcoded User-Agent override ---------------------------------
#
# The installed browser was Chrome 150 while the code hardcoded a Chrome/126
# UA string. Playwright overrides the UA *string* only; real Chrome still
# reports its true version via Sec-CH-UA and navigator.userAgentData. That
# self-inconsistency is not producible by any real browser.


def test_no_hardcoded_user_agent_in_instagram():
    """F1: instagram_browser must not pin a User-Agent string."""
    src = _module_source(instagram_browser)
    assert "user_agent=" not in src, (
        "instagram_browser sets a user_agent override. Real Chrome's own UA is "
        "consistent with its client hints; a pinned string is not."
    )


def test_no_hardcoded_user_agent_in_tiktok():
    """F1: tiktok_browser had *two* UA sites — the cookie context and the
    persistent-profile fallback. The research doc only cited the first."""
    src = _module_source(tiktok_browser)
    assert "user_agent=" not in src, (
        "tiktok_browser sets a user_agent override on at least one of its two "
        "context-creation paths."
    )


def test_no_stale_chrome_version_string_anywhere():
    """F1: the specific stale version must not survive in either module, in a
    comment, a docstring, or a resurrected override."""
    for module in (instagram_browser, tiktok_browser):
        src = _module_source(module)
        assert "Chrome/126" not in src, (
            f"{module.__name__} still contains the stale Chrome/126 UA string."
        )


# --- F2: no forced software rendering -------------------------------------
#
# instagram_browser forced software WebGL via ANGLE/SwiftShader, so
# WEBGL_debug_renderer_info reported "SwiftShader" — a classic headless/bot
# fingerprint. Note the asymmetry that made this damning: tiktok_browser
# deliberately forces *hardware* acceleration for the same video files, and
# Instagram is the platform that flagged.


def test_no_swiftshader_software_rendering_in_instagram():
    """F2: the SwiftShader/ANGLE args must not come back.

    Provenance checked before removal (the research doc asked for this): the
    flags trace to the initial harness commit 5e79f98, not to any later fix
    for an observed crash.
    """
    src = _module_source(instagram_browser)
    assert '"--use-angle=swiftshader"' not in src, (
        "instagram_browser reintroduced forced software rendering. A "
        "SwiftShader WebGL renderer string is a well-known bot fingerprint."
    )
    assert '"--use-gl=angle"' not in src, (
        "instagram_browser reintroduced the ANGLE GL override that paired with "
        "the SwiftShader flag."
    )


def test_neither_module_forces_software_rendering():
    """F2: guard the whole surface, not just the one line that was wrong.

    Matches the quoted argument literal rather than the bare word, because
    instagram_browser deliberately retains an explanatory comment naming
    SwiftShader so the flag is not reintroduced by a future reader.
    """
    for module in (instagram_browser, tiktok_browser):
        src = _module_source(module)
        assert '"--use-angle=swiftshader"' not in src, (
            f"{module.__name__} passes a SwiftShader launch argument."
        )


# --- F6: legacy anti-detection measures, decided deliberately -------------
#
# Five navigator.webdriver init scripts existed across the two modules. They
# were redundant with --disable-blink-features=AutomationControlled, which
# already suppresses the property, and an init script that redefines a
# property descriptor is itself detectable. The real cost was false
# confidence: the area *looked* handled, which is plausibly why the stale UA
# (F1) and SwiftShader (F2) went unexamined for so long.


def test_redundant_webdriver_init_scripts_removed():
    """F6: the five init-script overrides are gone from both modules."""
    for module in (instagram_browser, tiktok_browser):
        src = _module_source(module)
        assert "navigator, 'webdriver'" not in src, (
            f"{module.__name__} reintroduced a navigator.webdriver init script. "
            "The launch flag already suppresses the property; the init script "
            "adds a detectable property descriptor and no protection."
        )


def test_automation_controlled_flag_retained():
    """F6: the launch flag is deliberately *kept* — this fix was a decision,
    not a blanket deletion. Guards against over-correcting into removing it."""
    for module in (instagram_browser, tiktok_browser):
        src = _module_source(module)
        assert "--disable-blink-features=AutomationControlled" in src, (
            f"{module.__name__} dropped the AutomationControlled flag. F6 kept "
            "it deliberately: harmless and marginally useful."
        )


def test_legacy_measures_are_labelled_as_non_coverage():
    """F6: the whole point of the fix is that the next reader must not mistake
    these measures for real coverage. If the comment goes, the fix is undone."""
    for module in (instagram_browser, tiktok_browser):
        src = _module_source(module)
        assert "LEGACY, NOT COVERAGE" in src, (
            f"{module.__name__} lost the note recording that the remaining "
            "anti-detection measure is legacy and does not address current "
            "platform detection."
        )
