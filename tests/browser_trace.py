"""A recording fake browser, used to pin the browser posting flows exactly.

WHY THIS EXISTS
---------------
`instagram_browser.post_media` and `tiktok_browser.post_media` drive live
accounts through Playwright. CLAUDE.md forbids end-to-end testing them, so
before Batch 6 they were the two worst-covered active modules (10% and 31%)
and nothing in the suite could tell a behaviour-preserving refactor from a
behaviour-changing one. Their failure mode is silent: a reordered click or a
dropped wait does not raise, it posts wrongly.

Everything those functions do to a browser goes through a small, fixed set of
Playwright method calls. This module supplies stand-ins for the Playwright
object graph that record every call, in order, with its arguments, and return
scripted answers so the flow runs to completion with no browser in existence.
Running the real `post_media` against them yields a **transcript**: a
mechanical record of what the code does, rather than a prose description of
what it is supposed to do.

The transcripts for the scenarios in `test_poster_internals.py` are committed
under `tests/golden/`.

**What the goldens are, precisely.** They were first captured from the
*unmodified* modules at `a5d9b07`, before any Batch 6 edit, so their starting
point was the original behaviour rather than the refactor's opinion of it.
They are not frozen at that content: three later commits changed behaviour
deliberately and regenerated the affected files, each showing its change as a
one- or two-line diff. So a golden's *history* is the equivalence evidence —
`git log -p tests/golden/` is where a refactor is proven inert — while its
current content is the behaviour of head. Read them that way; the file
contents alone do not prove anything about the base.

A refactor that is genuinely behaviour-preserving reproduces them byte for
byte; a commit meant to change behaviour must show that change and nothing
else. Scenarios added after a5d9b07 (the profile-fallback, narrow-layout and
password-probe paths) were captured at head and carry no base comparison —
they guard future changes, not this batch's refactor.

DETERMINISM
-----------
Two sources of randomness are removed so a transcript diff means a real
change and never noise:

* `jitter.jittered_duration` is stubbed to return its `base` unchanged, so a
  recorded `sleep` line is the wait's **floor**. That is exactly the quantity
  CLAUDE.md § Intentional design protects, so the transcripts double as the
  floor-rule guard for the whole flow. Variance itself is proven separately
  in `test_jitter.py`, which does not need a browser.
* `random` is seeded per capture, so any other draw (caption chunk sizes,
  from Batch 6 onward) is reproducible.

`asyncio.sleep` is replaced with a recorder that does not actually wait, so
the thirteen scenarios add well under a second to the suite despite the real
flows sleeping for minutes.

SAFETY
------
No fake here can reach a network, a browser binary, or a session directory.
`fake_playwright()` patches the module-level `async_playwright` symbol in the
module under test, so the real one is never called. `tests/conftest.py`
additionally tripwires both `post_media` functions; the
`allow_browser_post_media` fixture is the single explicit opt-in.
"""

import asyncio
import hashlib
import random
import re
from contextlib import asynccontextmanager

# Sentinel: script entries set to RAISE make that call raise, which is how the
# real flows discover a missing element (every fallback chain is driven by an
# exception or a zero count).
RAISE = object()


def _fmt(value) -> str:
    """Render a call argument stably.

    Absolute paths are reduced to their final component. Session dirs and
    media live under a per-test tmp_path and debug screenshots under the
    checkout, so without this a committed transcript would encode one
    machine's home directory and fail on a clean CI clone. URLs are
    unaffected — they start with a scheme, not a slash.
    """
    if isinstance(value, str):
        if value.startswith("/"):
            value = value.rsplit("/", 1)[-1]
        return repr(value)
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, FakeLocator):
        return value.desc
    return repr(value)


def _kwargs(kw: dict) -> str:
    return ", ".join(f"{k}={_fmt(v)}" for k, v in kw.items() if v is not None)


def _call(name: str, *args, **kw) -> str:
    parts = [_fmt(a) for a in args]
    rendered = _kwargs(kw)
    if rendered:
        parts.append(rendered)
    return f"{name}({', '.join(parts)})"


def _js_fingerprint(js: str) -> str:
    """Identify an inline JS snippet by content, ignoring layout.

    Extracting a block into a helper re-indents its JS. Collapsing whitespace
    before hashing means a pure move reads as unchanged, while any edit to
    what the script actually does changes the hash.
    """
    normalized = re.sub(r"\s+", " ", js).strip()
    digest = hashlib.sha1(normalized.encode()).hexdigest()[:8]
    return f"js#{digest} {normalized[:60]!r}"


class Script:
    """Scripted answers for one scenario.

    Each table maps a *substring* to a return value; the first table entry
    whose substring appears in the call's rendered target wins, otherwise the
    default applies. Substring matching keeps a scenario readable and means a
    selector chain gaining a fallback does not silently change the answers.
    """

    def __init__(
        self,
        *,
        url="https://example.test/",
        counts=None,
        default_count=1,
        visible=None,
        default_visible=True,
        evaluate=None,
        wait_for_selector=None,
        locator_wait=None,
        inner_text="",
        cookies=None,
    ):
        self.url = url
        self.counts = counts or {}
        self.default_count = default_count
        self.visible = visible or {}
        self.default_visible = default_visible
        self.evaluate = evaluate or {}
        self.wait_for_selector = wait_for_selector or {}
        self.locator_wait = locator_wait or {}
        self.inner_text = inner_text
        self.cookies = cookies or []

    @staticmethod
    def _lookup(table, key, default):
        for needle, value in table.items():
            if needle in key:
                return value
        return default

    def count_for(self, desc):
        return self._lookup(self.counts, desc, self.default_count)

    def visible_for(self, desc):
        return self._lookup(self.visible, desc, self.default_visible)

    def evaluate_for(self, js):
        return self._lookup(self.evaluate, re.sub(r"\s+", " ", js), None)

    def wait_for_selector_result(self, selector):
        return self._lookup(self.wait_for_selector, selector, None)

    def locator_wait_result(self, desc):
        return self._lookup(self.locator_wait, desc, None)

    def inner_text_for(self, desc):
        if isinstance(self.inner_text, dict):
            return self._lookup(self.inner_text, desc, "")
        return self.inner_text


class Recorder:
    """Ordered transcript of everything the flow did to the browser."""

    def __init__(self, script: Script):
        self.script = script
        self.lines: list[str] = []

    def add(self, line: str, result=None):
        if result is not None:
            line = f"{line} -> {_fmt(result)}"
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


class FakeElement:
    """What `wait_for_selector` hands back: a resolved element handle."""

    def __init__(self, rec: Recorder, desc: str):
        self.rec = rec
        self.desc = desc

    async def click(self, **kw):
        self.rec.add(_call(f"{self.desc}.click", **kw))

    async def set_input_files(self, files, **kw):
        self.rec.add(_call(f"{self.desc}.set_input_files", files, **kw))

    async def content_frame(self):
        self.rec.add(f"{self.desc}.content_frame()")
        return FakeFrame(self.rec, "frame")

    async def inner_text(self):
        text = self.rec.script.inner_text_for(self.desc)
        self.rec.add(f"{self.desc}.inner_text()", text)
        return text

    async def is_visible(self):
        value = self.rec.script.visible_for(self.desc)
        self.rec.add(f"{self.desc}.is_visible()", value)
        return value


class FakeLocator:
    def __init__(self, rec: Recorder, desc: str):
        self.rec = rec
        self.desc = desc

    # --- chain building: not recorded on its own, it shows up in the target
    # description of whatever action is finally taken on the chain ---------

    def locator(self, selector, **kw):
        return FakeLocator(self.rec, f"{self.desc}.{_call('locator', selector, **kw)}")

    def filter(self, **kw):
        return FakeLocator(self.rec, f"{self.desc}.{_call('filter', **kw)}")

    def aria_role(self, role, **kw):
        return FakeLocator(self.rec, f"{self.desc}.{_call('aria_role', role, **kw)}")

    @property
    def first(self):
        return FakeLocator(self.rec, f"{self.desc}.first")

    @property
    def last(self):
        return FakeLocator(self.rec, f"{self.desc}.last")

    # --- queries: recorded with their answers, because they steer branches -

    async def count(self):
        value = self.rec.script.count_for(self.desc)
        self.rec.add(f"{self.desc}.count()", value)
        return value

    async def is_visible(self):
        value = self.rec.script.visible_for(self.desc)
        self.rec.add(f"{self.desc}.is_visible()", value)
        return value

    async def inner_text(self):
        text = self.rec.script.inner_text_for(self.desc)
        self.rec.add(f"{self.desc}.inner_text()", text)
        return text

    # --- actions ----------------------------------------------------------

    async def click(self, **kw):
        self.rec.add(_call(f"{self.desc}.click", **kw))

    async def focus(self, **kw):
        self.rec.add(_call(f"{self.desc}.focus", **kw))

    async def scroll_into_view_if_needed(self, **kw):
        self.rec.add(_call(f"{self.desc}.scroll_into_view_if_needed", **kw))

    async def press_sequentially(self, text, **kw):
        self.rec.add(_call(f"{self.desc}.press_sequentially", text, **kw))

    async def set_input_files(self, files, **kw):
        self.rec.add(_call(f"{self.desc}.set_input_files", files, **kw))

    async def wait_for(self, **kw):
        self.rec.add(_call(f"{self.desc}.wait_for", **kw))
        if self.rec.script.locator_wait_result(self.desc) is RAISE:
            raise TimeoutError(f"fake: {self.desc} never reached {kw.get('state')}")


class FakeKeyboard:
    def __init__(self, rec: Recorder):
        self.rec = rec

    async def press(self, key, **kw):
        self.rec.add(_call("keyboard.press", key, **kw))

    async def insert_text(self, text):
        self.rec.add(_call("keyboard.insert_text", text))


class FakeFrame:
    """An iframe content frame. Exposes the subset the flows use on `target`."""

    def __init__(self, rec: Recorder, name: str):
        self.rec = rec
        self.name = name

    async def wait_for_selector(self, selector, **kw):
        self.rec.add(_call(f"{self.name}.wait_for_selector", selector, **kw))
        if self.rec.script.wait_for_selector_result(selector) is RAISE:
            raise TimeoutError(f"fake: no element for {selector!r}")
        return FakeElement(self.rec, f"element({selector!r})")

    def locator(self, selector, **kw):
        return FakeLocator(self.rec, _call("locator", selector, **kw))


class FakePage(FakeFrame):
    def __init__(self, rec: Recorder):
        super().__init__(rec, "page")
        self.keyboard = FakeKeyboard(rec)

    @property
    def url(self):
        # Reads of page.url drive the session-expired and still-on-upload
        # branches, so each read is recorded with what it returned.
        value = self.rec.script.url
        self.rec.add("page.url", value)
        return value

    async def goto(self, url, **kw):
        self.rec.add(_call("page.goto", url, **kw))

    async def wait_for_load_state(self, state=None, **kw):
        self.rec.add(_call("page.wait_for_load_state", state, **kw))

    async def evaluate(self, js, *args):
        result = self.rec.script.evaluate_for(js)
        self.rec.add(f"page.evaluate({_js_fingerprint(js)})", result)
        return result

    async def screenshot(self, **kw):
        self.rec.add(_call("page.screenshot", **kw))


class FakeContext:
    def __init__(self, rec: Recorder, label: str):
        self.rec = rec
        self.label = label
        self.pages = []

    async def new_page(self):
        self.rec.add(f"{self.label}.new_page()")
        page = FakePage(self.rec)
        self.pages.append(page)
        return page

    async def add_cookies(self, cookies):
        self.rec.add(f"{self.label}.add_cookies(<{len(cookies)} cookies>)")

    async def cookies(self):
        self.rec.add(f"{self.label}.cookies()")
        return list(self.rec.script.cookies)

    async def close(self):
        self.rec.add(f"{self.label}.close()")


class FakeBrowser:
    def __init__(self, rec: Recorder):
        self.rec = rec

    async def new_context(self, **kw):
        self.rec.add(_call("browser.new_context", **kw))
        return FakeContext(self.rec, "context")

    async def close(self):
        self.rec.add("browser.close()")


class FakeChromium:
    def __init__(self, rec: Recorder):
        self.rec = rec

    async def launch_persistent_context(self, **kw):
        self.rec.add(_call("chromium.launch_persistent_context", **kw))
        return FakeContext(self.rec, "context")

    async def launch(self, **kw):
        self.rec.add(_call("chromium.launch", **kw))
        return FakeBrowser(self.rec)


class FakePlaywright:
    def __init__(self, rec: Recorder):
        self.chromium = FakeChromium(rec)


def fake_playwright(rec: Recorder):
    """Stand-in for `async_playwright()`, which is used as `async with`."""

    @asynccontextmanager
    async def _cm_factory():
        yield FakePlaywright(rec)

    def _async_playwright():
        return _cm_factory()

    return _async_playwright


def run_traced(monkeypatch, module, coro_factory, script: Script, seed: int = 1234) -> Recorder:
    """Run a real posting flow against the fakes and return its transcript.

    `module` is the module whose `async_playwright` symbol is replaced —
    both browser modules import it by name, so patching the module attribute
    is what actually diverts them.
    """
    import backend.jitter as jitter

    rec = Recorder(script)
    random.seed(seed)

    monkeypatch.setattr(module, "async_playwright", fake_playwright(rec))

    # Record the floor of every wait and return immediately. jitter.py's
    # sleep_jittered routes through asyncio.sleep, so one patch covers both
    # the jittered waits and tiktok_browser's remaining fixed ones.
    async def _record_sleep(duration):
        rec.add(f"sleep {duration:.3f}")

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    # Pin each wait to its documented floor so a transcript line is the
    # guarded quantity rather than a sample from a distribution.
    monkeypatch.setattr(jitter, "jittered_duration", lambda base, spread=None: base)

    try:
        result = asyncio.run(coro_factory())
        rec.add(f"RETURN {result!r}")
    except Exception as e:  # noqa: BLE001 - the raised message is the behaviour
        rec.add(f"RAISED {type(e).__name__}: {e}")
    return rec
