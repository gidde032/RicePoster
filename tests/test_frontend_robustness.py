"""Batch 2 of the tech-debt campaign: frontend robustness.

Covers Issues #30 (shared fetch/payload/DOM helpers, audit findings FE-2, FE-3,
FE-5, FE-8), #31 (remaining failure states and fetch timeouts, FE-1 residual,
FE-6, FE-7), and #7 (bounded queue polling while the panel is open).

`frontend/index.html` is a single vanilla HTML/JS file driven entirely by
Playwright-free browser behavior, and the project forbids real-browser E2E
tests. These are therefore **source-level assertions** in the established style
of `test_ui_polish.py` and `test_history_headless.py`: they pin the structural
properties the fixes depend on, not the rendered result. The one exception is
`test_frontend_script_parses`, which really does parse the script.

The duplication tests are written as exact occurrence counts rather than
"helper exists" checks on purpose. A helper that exists while the copy-pasted
originals also survive is the failure mode these issues were filed about.
"""

import re
import shutil
import subprocess

import pytest

from tests.paths import PROJECT_ROOT

INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text()


def _script() -> str:
    """The contents of the single <script> block."""
    html = _html()
    assert html.count("<script>") == 1, "index.html is expected to have one script block"
    return html.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def _function_body(name: str) -> str:
    """Extract one function body by brace matching.

    Regex alone cannot do this: several of these functions contain template
    literals with braces, so a non-greedy match to the next `}` truncates the
    body and the assertion silently passes on a partial view.
    """
    script = _script()
    match = re.search(rf"\bfunction {re.escape(name)}\s*\([^)]*\)\s*{{", script)
    assert match, f"{name}() not found in index.html"

    depth = 0
    start = match.end() - 1
    for i in range(start, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start : i + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}()")


# --- sanity: the file still parses ------------------------------------------


def test_frontend_script_parses():
    """The only test here that actually executes a parser rather than grepping.

    Every other assertion in this module is a string check, so a stray brace
    introduced by an edit would leave them all green while the UI is dead on
    load. Skips where node is unavailable — CI installs Python only.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; the script-parse check needs a JS parser")

    proc = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=_script(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"frontend script failed to parse:\n{proc.stderr}"


# --- #30 / FE-2: one fetch-error extraction ---------------------------------


def test_fetch_error_extraction_exists_once():
    """FE-2: the `resp.json().catch(() => null)` + `err?.detail` dance was
    copy-pasted across six call sites. Collapsing it is only a fix if the
    copies are actually gone, so this pins the count at one."""
    script = _script()
    assert "async function handleFetchError(resp)" in script
    assert script.count("resp.json().catch(() => null)") == 1, (
        "the fetch-error extraction should exist only inside handleFetchError"
    )
    assert script.count("err?.detail") == 1


def test_handle_fetch_error_is_adopted_at_every_call_site():
    """Each network call that can fail must route through the helper. Six sites
    had the inline copy and several more had no status check at all."""
    script = _script()
    # Exact, not a floor: a floor lets a call site silently lose its check
    # (review finding, 2026-07-30). The twelve are /api/accounts, both caption
    # paths, media-info, media/clear, history, post-progress, /api/post, the
    # queue POST/GET, the queue DELETE, and the queue-media DELETE (#4).
    assert script.count("await handleFetchError(resp)") == 12, (
        "handleFetchError should be awaited at every fetching call site"
    )
    # One deliberate exemption, pinned so it stays deliberate: fetchRetainedMedia
    # degrades to an empty list instead of throwing, because retained snapshots
    # are a secondary section of the queue panel and a failure to load them must
    # not blank the scheduled-batch list that is its primary content (#4).
    retained = _function_body("fetchRetainedMedia")
    assert "handleFetchError" not in retained
    assert "if (!resp.ok) return [];" in retained
    body = _function_body("handleFetchError")
    assert "if (resp.ok) return;" in body
    assert "HTTP ${resp.status}" in body


# --- #30 / FE-3: one slot-payload builder -----------------------------------


def test_slot_payload_is_built_in_one_place():
    """FE-3: postAll and scheduleAll built a verbatim-identical slots array,
    including the browser-mode headless override that travelled with it."""
    script = _script()
    assert "function buildSlotsPayload()" in script
    assert script.count("media_type: s.mediaType || 'image'") == 1, (
        "slot payload construction should exist only inside buildSlotsPayload"
    )
    assert script.count("payload.headless = state.headlessOverride") == 1

    assert "const payload = buildSlotsPayload();" in _function_body("postAll")
    # scheduleAll adds fire_time on top of the shared base.
    assert "...buildSlotsPayload(), fire_time: fireTime" in _function_body("scheduleAll")


# --- #30 / FE-5 + FE-8: guarded DOM access ----------------------------------


def test_dom_lookups_go_through_guarded_helpers():
    """FE-5/FE-8: ~14 raw getElementById calls meant a renamed id surfaced as
    'Cannot read properties of null' far from the actual mistake."""
    script = _script()
    assert script.count("document.getElementById") == 2, (
        "getElementById should appear only inside the el() and elOpt() helpers"
    )
    body = _function_body("el")
    assert "Missing DOM element" in body, "el() must name the missing id"
    assert "throw new Error" in body

    # The slot-scoped builder is what made the guard tractable (FE-8).
    assert "const slotEl = (name, slot) => el(`${name}_${slot}`);" in script
    assert "const slotElOpt = (name, slot) => elOpt(`${name}_${slot}`);" in script


def test_optional_elements_use_the_non_throwing_getter():
    """Three lookups are legitimately absent at times: the headless badge only
    exists in browser mode, status rows only during a run, and the init()
    fallback renderer runs while the page may be half-built. Routing those
    through the throwing el() would turn a handled case into a crash."""
    assert "elOpt('headlessBadge')" in _function_body("renderHeadlessBadge")
    assert "slotElOpt('status', slot)" in _function_body("renderProgress")
    assert "elOpt('statusPanel')" in _function_body("init")


# --- #31 / FE-6: caption failures never reach the textarea ------------------


def test_caption_failure_is_not_written_into_the_textarea():
    """FE-6, the finding with a real consequence: a failed generation was
    written as text into the caption box, where an error string was
    indistinguishable from a caption and could be posted to a live account."""
    script = _script()
    assert "[Error generating caption" not in script, (
        "caption errors must never be rendered as caption text"
    )

    body = _function_body("generateAll")
    catch = body.split("} catch (e) {", 1)[1]
    assert "s.caption" not in catch, (
        "the generateAll failure path must not touch the caption state"
    )
    assert "ta.value" not in catch, (
        "the generateAll failure path must not touch the textarea"
    )
    assert "setCaptionError(slot," in catch


def test_caption_failure_is_visible_next_to_its_slot():
    """The error has to land somewhere the user actually looks, and an empty
    caption keeps Post All disabled for that slot as a second line of defence."""
    script = _script()
    assert "function setCaptionError(slot, message)" in script
    assert 'id="captionError_${slot}"' in script, "each slot needs its error element"
    assert ".caption-error {" in _html(), "the error element needs styling to be visible"

    # Regenerate previously signalled failure only via a 2.5s button label.
    assert "setCaptionError(slot," in _function_body("regenerateCaption")


def test_replacing_a_slots_file_clears_its_stale_caption_error():
    """Review finding, 2026-07-30: a failed generation left its error line in
    place when the user dropped a *new* file into the same slot, so the message
    appeared to describe media it had never seen. handleFile updates the slot in
    place rather than re-rendering, so the reset has to be explicit."""
    assert "setCaptionError(slot, '');" in _function_body("handleFile")


# --- #31 / FE-7: fetch timeouts, with the live-posting exemptions -----------


def test_fetches_have_a_timeout():
    """FE-7: no fetch had a timeout, so a hung backend left the UI loading
    forever with no recovery short of a reload."""
    script = _script()
    assert "async function fetchWithTimeout(url, opts = {}, ms = FETCH_TIMEOUT_MS)" in script
    body = _function_body("fetchWithTimeout")
    assert "new AbortController()" in body
    assert "ctl.abort()" in body
    assert "clearTimeout(timer)" in body, "the timer must be cleared on the success path too"
    assert "AbortError" in body, "an abort should report as a timeout, not a raw DOM error"


def test_caption_timeout_sits_outside_the_backend_budget():
    """The backend caps caption generation at 60s (CAPTION_API_TIMEOUT_S). A
    client timeout below that would pre-empt the server's own error message."""
    from backend import captions

    script = _script()
    match = re.search(r"const CAPTION_TIMEOUT_MS = (\d+);", script)
    assert match, "CAPTION_TIMEOUT_MS not found"
    assert int(match.group(1)) / 1000 > captions.CAPTION_API_TIMEOUT_S, (
        "the client caption timeout must exceed the backend's own budget"
    )


def test_live_posting_calls_are_exempt_from_client_timeouts():
    """Maintainer decision, 2026-07-30. Aborting the client does not stop a
    browser run that is already driving live accounts, so a spurious timeout
    would show 'failed' in the UI while posts continue — and POST /api/queue is
    what arms an unattended run. Both stay on a bare fetch deliberately.

    This is a safety property, not a style preference: pin it so a later
    'consistency' cleanup has to argue with a failing test."""
    script = _script()
    # Quote-agnostic on purpose. A single-quote-only pattern would still fail if
    # one of these two were converted to fetchWithTimeout, but a *new* untimed
    # call written with double quotes or a template literal would slip past the
    # exemption list entirely — the direction that actually costs safety.
    bare = re.findall(r"""await fetch\(\s*['"`](/api/[^'"`]+)['"`]""", script)
    assert sorted(bare) == ["/api/post", "/api/queue"], (
        f"only the two live-posting writes may bypass fetchWithTimeout; found {bare}"
    )
    assert "fetchWithTimeout('/api/post'" not in script
    assert "fetchWithTimeout('/api/queue'," not in script


def test_the_exemption_regex_would_catch_a_double_quoted_bypass():
    """Guards the guard. The exemption test above is only a safety net if its
    pattern actually matches however a future call happens to be written, so
    this pins the quote-agnosticism rather than trusting it by inspection."""
    pattern = r"""await fetch\(\s*['"`](/api/[^'"`]+)['"`]"""
    for sample in (
        """await fetch('/api/post', {})""",
        '''await fetch("/api/post", {})''',
        """await fetch(`/api/post`, {})""",
    ):
        assert re.findall(pattern, sample) == ["/api/post"], (
            f"the exemption pattern missed a bypass written as: {sample}"
        )


# --- #31 / FE-1 residual: stuck panels report themselves --------------------


def test_history_failure_is_visible_in_its_panel():
    """FE-1 residual: a console-only catch left the panel apparently stuck
    mid-open, with nothing on screen explaining why."""
    catch = _function_body("toggleHistory").split("} catch (e) {", 1)[1]
    assert "panel.innerHTML" in catch
    assert "panel.style.display = ''" in catch, "the panel must be shown to be seen"
    assert "esc(" in catch, "error text is untrusted and must be escaped"


def test_queue_failure_is_visible_when_the_panel_is_open():
    """A silently stale open queue panel is the exact state Issue #7 exists to
    prevent, so a failed refresh must not be console-only either."""
    catch = _function_body("refreshQueue").split("} catch (e) {", 1)[1]
    assert "console.error('Queue refresh failed:', e);" in catch, (
        "the console line stays: #7 requires failures remain observable there"
    )
    assert "panel.innerHTML" in catch
    assert "if (panel && panel.style.display === 'block')" in catch, (
        "report into the panel only when it exists and is actually open — the "
        "null guard is part of the contract, not incidental"
    )
    assert "esc(" in catch


# --- #7: bounded queue polling ----------------------------------------------


def test_queue_polls_while_its_panel_is_open():
    """#7: the panel fetched on open and after a local delete only, so a batch
    that fired, was cancelled, or came back interrupted stayed visibly stale."""
    script = _script()
    assert "const QUEUE_POLL_MS = 10000;" in script
    assert "let queuePollTimer = null;" in script
    assert "setInterval(refreshQueue, QUEUE_POLL_MS)" in _function_body("startQueuePolling")


def test_queue_polling_stops_when_the_panel_closes():
    """Acceptance criterion: polling stops when the panel closes."""
    body = _function_body("toggleQueuePanel")
    assert "startQueuePolling();" in body
    assert "stopQueuePolling();" in body
    open_branch, closed_branch = body.split("} else {", 1)
    assert "startQueuePolling();" in open_branch
    # The eager fetch on open is what makes the panel correct *immediately*;
    # without it the first 10s of every open shows stale state and no other
    # assertion notices (review finding, 2026-07-30).
    assert "refreshQueue();" in open_branch, (
        "opening the panel must fetch at once, not wait a full poll interval"
    )
    assert "stopQueuePolling();" in closed_branch, (
        "closing the panel must stop the timer"
    )

    stop = _function_body("stopQueuePolling")
    assert "clearInterval(queuePollTimer)" in stop
    assert "queuePollTimer = null" in stop, "the handle must be cleared, not just the timer"


def test_repeated_open_close_cycles_cannot_stack_timers():
    """Acceptance criterion: multiple open/close cycles do not create duplicate
    timers. startQueuePolling clears any existing interval before creating one,
    so it is safe even if a close is ever missed."""
    body = _function_body("startQueuePolling")
    stop_at = body.index("stopQueuePolling();")
    start_at = body.index("setInterval(")
    assert stop_at < start_at, (
        "startQueuePolling must clear the previous timer before starting a new one"
    )
