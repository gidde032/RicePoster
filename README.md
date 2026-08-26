# RicePoster

A local browser-automation tool that posts one media file with an AI-generated
caption to three Instagram accounts and three TikTok accounts (slots A, B, C)
in one click. FastAPI serves a single-page UI; Playwright drives real Chrome
sessions to do the posting; the Anthropic API writes captions in a consistent
house style. Runs entirely on your machine — nothing is deployed.

## Prerequisites

- Python 3.10+ with the dependencies in `requirements.txt`
  (this repo currently runs on the Anaconda `python`)
- Google Chrome installed (Playwright uses the `chrome` channel for video codec
  support)
- An Anthropic API key (for caption generation)

## Installation

```bash
git clone https://github.com/gidde032/RicePoster.git
cd RicePoster
pip install -r requirements.txt
playwright install chrome          # the `chrome` channel, NOT `chromium`
pre-commit install                 # wire up the commit/push quality gates
pre-commit install --hook-type pre-push
cp credentials.env.example credentials.env   # then edit it
```

`playwright install chrome` is deliberate: the posters launch
`channel="chrome"` (real Google Chrome) for video codec support, so
`playwright install chromium` would fetch the wrong browser. If you already
have Google Chrome installed system-wide, you can skip that line.

The two `pre-commit install` lines make the local quality gates real. Until you
run them, local commits and pushes enforce nothing; pull requests still run the
clean-environment GitHub Actions gate described under **Tests**.

## Configuration (`credentials.env`)

See `credentials.env.example` for the full template. The essentials:

| Variable | Meaning |
| --- | --- |
| `ANTHROPIC_API_KEY` | For caption generation |
| `IG_ACCOUNT_{A,B,C}_NAME` | Display names shown in the UI |
| `POST_MODE` | `mock` (fake everything), `browser` (Playwright — the real workflow), `api` (official APIs; incomplete, needs tokens/dev access) |
| `HEADLESS` | `true` = browsers hidden, `false` = visible. **Not only a debugging switch — it is an Instagram detection setting.** Headless Chrome reports `HeadlessChrome/...` in its user agent while the client hints beside it say `Google Chrome`, and a browser contradicting itself about its own identity is a stronger signal than either value alone. It also reports `colorDepth` 24 instead of a real Mac's 30. Neither is fixable in code. Measure with `python tools/probe_fingerprint.py` |
| `ACCOUNT_SLOTS` | Which slots exist, in order (default `A,B,C`). Each slot needs its own login and its own entry in `device_identity.DISPLAYS` |
| `LOG_LEVEL` | Console verbosity (default `INFO`). `INFO` keeps the full browser-automation narration; `WARNING` keeps only degraded and failed states, which suits unattended scheduled batches. An unrecognised value is reported at startup and falls back to `INFO`. Note that `session_manager`'s status and health-check reports are plain CLI output and are never suppressed by this setting |
| `NOTIFY_SERVICE` | `none` (default) or `ntfy` — push notifications for failures and run summaries |
| `NTFY_TOPIC` / `NTFY_SERVER` | ntfy topic (treat it as a secret — it is a bearer token) and server, default `https://ntfy.sh` |
| `SCHEDULER_ENABLED` | `true` (default) runs the scheduler loop at startup so queued batches fire. `false` starts the server without it — queued batches stay queued |
| `SESSION_CHECK_TTL_S` | How long a passing session health check is trusted, in seconds (default `21600` = 6h) |
| `PREFLIGHT_CHECK_PLATFORMS` | Which platforms get a browser pre-flight check before a scheduled batch. Default, and empty/unset, = `instagram,tiktok`; the literal `none` disables both |
| `INTER_SLOT_DELAY_MIN_S` / `INTER_SLOT_DELAY_MAX_S` | Randomised gap between account slots in a run, in seconds (default `60`/`180`). Both `0` disables |
| `HANDOFF_DIR` | Shared folder RiceClipper writes finished clips into and **Pull from Clipper** reads from (default `~/riceclipper-handoff`). Must match RiceClipper's `RICECLIPPER_HANDOFF_DIR` |
| `CLIPPER_INGEST_STYLE` | Caption style applied to clips pulled from RiceClipper (default `benny-blanco`) |

The `INTER_SLOT_DELAY_*`, `SESSION_CHECK_TTL_S` and `PREFLIGHT_CHECK_PLATFORMS` knobs reduce Instagram's automation signal. Instagram flags accounts
that show machine-like patterns, and the three biggest ones this tool can
control are: repeated automated page loads (cut by caching a passing health
check for `SESSION_CHECK_TTL_S`), browser traffic that isn't a post (cut by
narrowing `PREFLIGHT_CHECK_PLATFORMS`), and accounts posting back-to-back on
an identical cadence (cut by the randomised `INTER_SLOT_DELAY_*` gap). Raising
the delays, or dropping `instagram` from the pre-flight list, reduces
automated traffic further.

## Session login (browser mode)

Each account needs a saved login before it can post:

```bash
python -m backend.session_manager login all            # every slot × platform
python -m backend.session_manager login instagram A    # or one at a time (A = an
                                                       # example; slots come from
                                                       # ACCOUNT_SLOTS)
python -m backend.session_manager status               # verify
python -m backend.session_manager clear tiktok A       # remove a bad session
```

A browser window opens; log in manually, then press Enter to save — or type
`abort` + Enter to discard a failed attempt. Instagram sessions are persistent
Chrome profiles under `sessions/instagram/`. TikTok prefers exported cookies:
use the Cookie-Editor extension in a logged-in real browser, export JSON, and
save it as `sessions/tiktok/{SLOT}_cookies.json`. Exported cookies last roughly
30–60 days; when TikTok posts start failing with "session expired," re-export.

## Running

```bash
./run.sh
# → http://127.0.0.1:8000
```

The server binds localhost only, on purpose: the API has no authentication and
can post to real accounts. Don't expose it to the network.

## Posting workflow

1. Open the UI; each account slot shows its session status.
2. Upload a media file per slot (each slot gets unique media).
3. Pick a **caption style** per slot (defaults to Generic / minimal) and optionally
   type a topic/description, then **Generate Captions** — fills every slot
   whose caption box is empty, editable in place.
4. Not happy with one caption? Type an optional tweak ("shorter, punchier")
   and hit that slot's **Regenerate** — it produces a deliberately different
   caption (the old one is sent as an anti-repeat reference). **Undo** swaps
   back if the previous one was better.
5. Review/edit captions (slots without both media and a caption are skipped).
6. **Post All** — slots post sequentially, one browser at a time. The status
   panel updates live with which slot/platform is currently posting.
7. Final per-slot status: ✓ confirmed, ⚠ **unconfirmed** (the success element
   never appeared — check the platform before reposting, the post may have
   gone through), or an error message.

**Pull from Clipper** (steps 2–3 in one click): if you use
[RiceClipper](https://github.com/gidde032/RiceClipper) to render captioned
clips, it writes finished batches into a shared handoff folder (`HANDOFF_DIR`).
**Pull from Clipper** ingests the oldest batch — assigning clips to slots in
order and staging their media — then shows a playable preview per slot and
writes a `CLIPPER_INGEST_STYLE` caption for each, grounded on a frame from the
clip plus its transcript (the same caption path a manual upload uses). You land
at step 5 (review captions → Post All). It never posts or schedules on its own.

Also in the UI: an upload progress bar for large videos, a caption character
counter (2,200 limit), session dots on each slot card, **New Run** to clear
everything for the next batch, **Clear media** to empty the `media/` upload
library (your original files are untouched), a **History** panel showing
recent runs (backed by gitignored `history.jsonl`), and a clickable
**Headless/Visible** badge that toggles browser visibility for this
session's runs without touching credentials.env.

Only one post run can be active at a time; a second attempt gets a clear
"already in progress" message.

**Scheduled batches and retained media.** A scheduled batch copies its media
into `queue_media/{batch_id}/` so an edit or a cleared upload library cannot
change what goes out later. That copy is deleted only when every slot in the
batch posts successfully. A batch that failed, posted partially, or was
interrupted keeps it: history records *that* a slot failed, never the file
itself, so the snapshot is the only copy still under RicePoster's control and
the only way to retry without re-uploading. At startup the server removes
snapshots it can prove belong to no batch and leaves everything else alone.
Whatever it kept is listed in the queue panel with the reason, and can be
deleted from there once you have checked the matching history.

## Caption styles

Caption voices live in `prompts/*.json` — one file per style with a `name`,
`display_name`, `system_prompt`, and `no_topic_fallback` (used when a slot has
no topic text). Seed styles: `generic` (the default), `meme-humor`,
`sports`, `music-fanpage`. To add a style, copy an existing file,
change all four fields, and refresh the UI — files are re-read on every
request, no restart needed. A malformed file is skipped with a console warning.

## Troubleshooting

- **"Session expired"** — run the login command shown in the error message
  (IG), or re-export cookies (TikTok).
- **Post shows ⚠ unconfirmed every time but posts are actually live** — the
  platform changed its success UI; the confirmation selectors in
  `backend/instagram_browser.py` / `backend/tiktok_browser.py` need updating.
- **Failure screenshots** land in `debug/` (gitignored) — the first place to
  look when a post errors.
- **TikTok click timeouts on a fresh account** — TikTok shows one-time
  dialogs (content-check opt-ins, feature promos) that block automation.
  The error message will name the dialog; log into that account in a normal
  browser, dismiss it once, and it won't reappear.
- **Watch it work** — set `HEADLESS=false` to see the browser during posting.
  This also removes two Instagram detection tells; see the `HEADLESS` row in
  the config table.

## Diagnostics

```bash
python tools/probe_fingerprint.py            # both modes, side by side
python tools/probe_fingerprint.py --headless # no window opens
python tools/probe_fingerprint.py --slot B   # a different slot's device
```

Launches Chrome with the Instagram posting configuration, points it at a local
`file://` page, and prints what the browser reports about itself — WebGL
renderer, user agent and client hints, screen geometry, pixel ratio. Use it to
check that a slot's spoofed device is internally consistent after changing
launch args or `backend/device_identity.py`.

It never contacts a platform and never opens `sessions/`; each run gets a
throwaway profile. `tests/test_probe_safety.py` enforces those properties.

## Tests

```bash
python -m pytest tests/ -q                                    # full suite
python -m pytest -m smoke -q                                  # fast tier
python -m pytest tests/ --cov=backend --cov-fail-under=43     # coverage floor
```

The suite is fully mocked — it never launches a browser, posts anywhere, or
calls the Anthropic API, and a conftest tripwire fails any test that tries.

`pytest`, `pytest-cov` and `pre-commit` are in `requirements.txt`. Two hooks
enforce the gates, but **only after you run the two `pre-commit install`
commands** in Installation — the config file alone does nothing:

| Hook | Runs | Enforces |
| --- | --- | --- |
| `pre-commit` | on `git commit` | the 6-test smoke tier |
| `pre-push` | on `git push` | the full suite + the 43% coverage floor |

GitHub Actions runs the full suite and 43% coverage floor on every pull request
to `main` and every push to `main`. The workflow has read-only repository
permission and receives no project secrets. The `main` ruleset names this
workflow's `Python 3.12 tests and coverage` result as its required check. On the
repository's current private GitHub Free plan, GitHub stores but does not
enforce that ruleset; the check still runs and reports its result. Local
`--no-verify` can bypass the hooks, but it cannot bypass the check once ruleset
enforcement is available.

A fresh clone currently reports two intentional skips: it cannot verify that a
local pre-push hook is installed, and it cannot cross-check the gitignored
maintainer `SPEC.md`. CI uses `-ra` so both reasons remain visible.

## Contributing

Open work and roadmap candidates are tracked in
[GitHub Issues](https://github.com/gidde032/RicePoster/issues). Development
uses short-lived branches and pull requests targeting `main`; link the relevant
issue and use `Closes #N` only when the pull request fully resolves it. The
repository templates capture reproduction evidence, live-account risk,
verification, and documentation impact.

Never include credentials, session data, account identifiers, private captions,
or logged-in screenshots in an issue or pull request. Changes that require live
platform traffic need explicit maintainer approval; tests and offline probes
are preferred.

## Internal documentation

Source comments and test docstrings cite design and research notes by
filename — `DESIGN-scheduling.md`, `RESEARCH-platform-detection.md`,
`SPEC.md`, `CLAUDE.md` and others. **Those files are deliberately gitignored
and are not part of this repository.** They are the maintainer's local working
notes. A citation you cannot follow is expected, not a missing file; the code
and this README are meant to stand alone.

## License

RicePoster is available under the [MIT License](LICENSE).
