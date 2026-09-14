"""Offline fingerprint probe — read what a launched Chrome reports about itself.

Reproduces the Instagram browser's exact launch configuration, opens a local
page, and prints the fingerprint surfaces that platform integrity systems key
on. Run it after any change to launch args, `device_identity.py`, or the
headless default.

    python tools/probe_fingerprint.py              # both modes, side by side
    python tools/probe_fingerprint.py --headless   # headless only, no window
    python tools/probe_fingerprint.py --slot B     # a different slot viewport

Identity follows the mode exactly as `instagram_browser._identity_kwargs`
does (D2, 2026-09-13): headless reproduces the per-slot synthetic device;
visible passes no viewport, screen, or scale, so the values printed are the
real window's. The visible run also clicks a local button two ways and
prints `isTrusted` for each, which is the D4 evidence: a JavaScript
`.click()` reports False, a Playwright click reports True.

SAFETY — this tool does not post and cannot post:
  * It never navigates to instagram.com, tiktok.com, or any network URL. The
    only navigation is a `file://` page written into a temp directory.
  * It never touches `sessions/`. Each launch gets a throwaway profile
    directory, deleted on exit. Real session state is never opened or locked.
  * It imports no poster module — only `device_identity` (pure, no I/O).
  * It writes nothing outside its temp directory except stdout.

`tests/test_probe_safety.py` enforces the first three properties, so this file
cannot quietly grow into a live path.

Why a local file:// page rather than about:blank: `navigator.userAgentData`
and `deviceMemory` are gated on a secure context, and an opaque origin reports
them as absent — which reads as a finding when it is only an artifact.

First run, 2026-07-27 (M2 Mac, Chrome 150), recorded so drift is visible:
  * WebGL renderer identical headless and visible —
    `ANGLE (Apple, ANGLE Metal Renderer: Apple M2)`. F2 holds under headless.
  * Headless UA carries `HeadlessChrome/150.0.0.0`. Visible does not.
  * `devicePixelRatio` 1 on a Retina host; `screen` == viewport exactly, so
    `screen.height == innerHeight` and `availHeight == height` — impossible on
    a real Mac, where browser chrome and the menu bar make both strictly
    smaller.
  * `colorDepth` 24 headless vs 30 visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from backend.device_identity import (  # noqa: E402
    scale_factor_for_slot,
    screen_for_slot,
    viewport_for_slot,
)

# Copied verbatim from `instagram_browser._get_context` (2026-09-13) rather
# than imported, so this tool never pulls in a module that can post. If that
# arg list changes, this copy is stale and the probe is no longer faithful —
# `tests/test_probe_safety.py` fails when the two drift.
IG_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--start-maximized",
]

# One button that records how each click event arrived. `isTrusted` is the
# browser's own verdict on whether a real input device produced the event.
_PROBE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>probe</title>"
    "<button id=b style='width:120px;height:40px'>probe</button>"
    "<script>window.__clicks=[];"
    "document.getElementById('b').addEventListener('click',"
    "e=>window.__clicks.push(e.isTrusted));</script>"
)

_JS_CLICK = "() => document.getElementById('b').click()"
_READ_CLICKS = "() => window.__clicks"

PROBE_JS = """
() => {
  const out = {};

  // --- F2: what renderer do we advertise? ---
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) {
      out.webgl = 'NO WEBGL CONTEXT';
    } else {
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      out.webgl = {
        vendor: gl.getParameter(gl.VENDOR),
        renderer: gl.getParameter(gl.RENDERER),
        unmasked_vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : '(no ext)',
        unmasked_renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '(no ext)',
      };
    }
  } catch (e) { out.webgl = 'ERROR: ' + e.message; }

  // --- automation tells ---
  out.webdriver = navigator.webdriver;
  out.userAgent = navigator.userAgent;
  out.isSecureContext = window.isSecureContext;
  try {
    out.uaData_brands = navigator.userAgentData
      ? navigator.userAgentData.brands.map(b => b.brand + ' ' + b.version).join(', ')
      : '(no userAgentData)';
    out.uaData_platform = navigator.userAgentData
      ? navigator.userAgentData.platform : '(none)';
    out.uaData_mobile = navigator.userAgentData
      ? navigator.userAgentData.mobile : '(none)';
  } catch (e) { out.uaData_brands = 'ERROR: ' + e.message; }

  // --- self-consistency of the per-slot identity ---
  out.devicePixelRatio = window.devicePixelRatio;
  out.innerSize = [window.innerWidth, window.innerHeight];
  out.outerSize = [window.outerWidth, window.outerHeight];
  out.screenSize = [window.screen.width, window.screen.height];
  out.screenAvail = [window.screen.availWidth, window.screen.availHeight];
  out.colorDepth = window.screen.colorDepth;

  // --- linkage vectors shared across every account ---
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory ?? null;
  out.languages = navigator.languages;
  out.platform = navigator.platform;
  out.maxTouchPoints = navigator.maxTouchPoints;
  out.pluginCount = navigator.plugins ? navigator.plugins.length : null;
  try {
    out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (e) { out.timezone = 'ERROR'; }

  return out;
}
"""


def identity_kwargs(headless: bool, slot: str) -> dict:
    """Mirror of `instagram_browser._identity_kwargs`, kept local so the
    probe imports nothing that can post."""
    if not headless:
        return dict(no_viewport=True)
    return dict(
        viewport=viewport_for_slot(slot),
        screen=screen_for_slot(slot),
        device_scale_factor=scale_factor_for_slot(slot),
    )


async def probe(headless: bool, slot: str) -> dict:
    """Launch Chrome once and return its self-reported fingerprint."""
    with tempfile.TemporaryDirectory(prefix="fpprobe-") as tmp:
        page_file = Path(tmp) / "probe.html"
        page_file.write_text(_PROBE_PAGE)
        profile = Path(tmp) / "profile"       # throwaway, never sessions/
        profile.mkdir()

        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                channel="chrome",
                **identity_kwargs(headless, slot),
                args=IG_ARGS,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(page_file.as_uri())   # the only navigation
                data = await page.evaluate(PROBE_JS)
                # D4 evidence: the same button, clicked from script and then
                # through Playwright's input pipeline.
                await page.evaluate(_JS_CLICK)
                button = page.locator("#b")
                await button.hover()
                await button.click()
                clicks = await page.evaluate(_READ_CLICKS)
                data["isTrusted_js_click"] = clicks[0] if len(clicks) > 0 else None
                data["isTrusted_playwright_click"] = clicks[1] if len(clicks) > 1 else None
                return data
            finally:
                await ctx.close()


def show(label: str, data: dict) -> None:
    print(f"\n{'=' * 64}\n  {label}\n{'=' * 64}")
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"  {key}:")
            for sub_key, sub_value in value.items():
                print(f"      {sub_key:<20} {sub_value}")
        else:
            rendered = value if isinstance(value, str) else json.dumps(value)
            print(f"  {key:<22} {rendered}")


def _renderer(data: dict) -> str:
    webgl = data.get("webgl")
    if isinstance(webgl, dict):
        return str(webgl.get("unmasked_renderer", webgl))
    return str(webgl)


def verdict(headless: dict | None, visible: dict | None) -> None:
    print(f"\n{'=' * 64}\n  VERDICT\n{'=' * 64}")

    if headless:
        renderer = _renderer(headless)
        software = any(
            tell in renderer.lower()
            for tell in ("swiftshader", "llvmpipe", "software")
        )
        print(f"  headless renderer            : {renderer}")
        print(f"  headless advertises software : {software}")
        print("      If True, F2 is inert under headless and a clean account")
        print("      record cannot be credited to it.")
        print("  headless UA says HeadlessChrome: "
              f"{'HeadlessChrome' in headless.get('userAgent', '')}")

        # A real browser always has chrome and a menu bar between the viewport
        # and the display, so these are strict inequalities on real hardware.
        inner, screen = headless.get("innerSize"), headless.get("screenSize")
        avail = headless.get("screenAvail")
        if inner and screen:
            print(f"  screen == viewport (impossible): {inner == screen}")
        if screen and avail:
            print(f"  availHeight == height (ditto)  : {screen[1] == avail[1]}")
        print(f"  devicePixelRatio               : "
              f"{headless.get('devicePixelRatio')}  (Retina Macs report 2)")

    for label, data in (("headless", headless), ("visible", visible)):
        if data:
            print(f"  {label} click isTrusted js/playwright: "
                  f"{data.get('isTrusted_js_click')} / "
                  f"{data.get('isTrusted_playwright_click')}")

    if headless and visible:
        print(f"\n  renderer differs between modes : "
              f"{_renderer(headless) != _renderer(visible)}")
        print(f"  colorDepth headless/visible    : "
              f"{headless.get('colorDepth')} / {visible.get('colorDepth')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true",
                      help="headless only — opens no window")
    mode.add_argument("--visible", action="store_true",
                      help="visible only")
    parser.add_argument("--slot", default="A",
                        help="slot whose viewport to reproduce (default: A)")
    args = parser.parse_args()

    run_headless = not args.visible
    run_visible = not args.headless

    print(f"Launching Chrome (local file:// only, throwaway profile, "
          f"slot {args.slot} identity when headless, native when visible).")

    headless_result = visible_result = None
    if run_headless:
        headless_result = await probe(headless=True, slot=args.slot)
        show("HEADLESS=True   (how ~all real traffic runs)", headless_result)
    if run_visible:
        visible_result = await probe(headless=False, slot=args.slot)
        show("HEADLESS=False  (how the F2 claim was validated)",
             visible_result)

    verdict(headless_result, visible_result)


if __name__ == "__main__":
    asyncio.run(main())
