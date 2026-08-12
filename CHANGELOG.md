# Changelog

## [Unreleased]

[#51](https://github.com/gidde032/RicePoster/issues/51) (TikTok fixed sleeps)
is the only remaining v1.0.0 item; it needs an approved live TikTok run.

### Changed

- Over-length captions are now rejected at the API boundary before any browser
  work starts. Previously an Instagram caption past its ~2200-character cap was
  silently truncated by the editor, and the caption read-back check then failed
  to match and abandoned the post *after* the media had already uploaded — so
  the failure moved from "publishes truncated" to "publishes nothing". A single
  `MAX_CAPTION_LENGTH = 2200` limit is enforced on the request model (covering
  both `POST /api/post` and `POST /api/queue`), and the value is served to the
  frontend via `GET /api/accounts` so the live char counter shares the backend's
  single source of truth. Per-platform limits are deferred until a platform cap
  actually differs in practice.
  ([#53](https://github.com/gidde032/RicePoster/issues/53))

- The repository is public. Documentation and caption prompts are local-by-default:
  `.gitignore` allowlists the four public documents and the four generic seed
  caption styles, so a new internal document or a personal caption style is
  ignored the moment it is created rather than needing to be remembered. The
  default caption style is the neutral `generic`. The `main` ruleset is enforced;
  verified by [#72](https://github.com/gidde032/RicePoster/issues/72).
  ([#50](https://github.com/gidde032/RicePoster/issues/50))

- The test suite's duplicated `PROJECT_ROOT` constant is consolidated into one
  `tests/paths.py` definition, and inline `frontend/index.html` reads now share
  the `frontend_src` fixture. Internal test hygiene only — no behaviour or
  assertion changed.
  ([#57](https://github.com/gidde032/RicePoster/issues/57),
  [#68](https://github.com/gidde032/RicePoster/issues/68))

### Fixed

- A slot with media but no caption is now visibly marked as skipped in the UI
  before a run begins, instead of being silently omitted server-side and
  leaving the run looking incomplete or failed. The run still proceeds for the
  remaining slots; there is no block or confirmation.
  ([#24](https://github.com/gidde032/RicePoster/issues/24))

- A malformed `queue.jsonl` line is now reported via a push notification instead
  of only a console print nobody watches. The loss is accepted (a dropped
  scheduled batch is re-creatable in the UI in seconds); the gap was that the
  only record was a print on an unattended machine, and the bad line was
  silently dropped on the next queue rewrite. The notification names only the
  line number and file — never the raw line content, which likely holds caption
  text — and is de-duplicated once per distinct line per process so the constant
  queue re-reads cannot storm the notifier. Unparseable lines still suspend
  automatic media-snapshot deletion (FR-17c).
  ([#35](https://github.com/gidde032/RicePoster/issues/35))

- The frontend's text-escaping helper (`esc`) was also used inside HTML
  attribute contexts, where it did not escape quote characters and so could not
  prevent a value from breaking out of the attribute. A separate `escAttr`
  helper now serves those sites. Not exploitable today (the affected values are
  hex batch IDs and maintainer-authored style names), but the safety lived in
  other files; this closes the latent gap.
  ([#41](https://github.com/gidde032/RicePoster/issues/41))

- The smoke-tier speed gate no longer flakes under machine load. It timed the
  wall-clock of a cold `pytest` subprocess, so roughly 80% of what it measured
  was interpreter startup, plugin loading and collection of the full suite —
  it breached its own 2s budget on an idle machine while the six smoke tests
  executed in 0.27s. It now reads execution time from pytest's JUnit XML
  report against a 1.5s budget, with a deliberately loose 10s wall-clock
  ceiling that only detects hangs.
  ([#13](https://github.com/gidde032/RicePoster/issues/13),
  [#44](https://github.com/gidde032/RicePoster/issues/44))

## [0.4.0] — 2026-07-31

The tech-debt campaign: eight batches partitioned by file surface, each one
branch, one bundled multi-issue pull request, and one independent cold review.
No user-facing features by design — it buys back the maintainability the
unattended-posting stack was built at speed against.

Twenty-three issues were selected; sixteen shipped here. The five read-only
investigations and two follow-ups (#55, #57) were deliberately pushed past the
release rather than held open against it.

### Fixed

- Instagram now verifies the caption before sharing. It previously typed the
  caption and clicked Share without ever reading the field back, so a caption
  mangled by Instagram's hashtag/mention autocomplete would publish silently —
  TikTok has checked this since its own caption-splice fix. The caption is
  retyped once and, if it still does not match, the post is abandoned rather
  than published with wrong text. The error quotes the editor contents for
  diagnosis and is redacted before it can reach a phone notification.

- Caption typing no longer uses a fixed 60 ms per keystroke, which gave long
  captions a machine-perfect rhythm on every run of every account. Captions are
  now typed in short word-runs at a varying speed with brief pauses between
  them. Runs are split only at spaces, so a pause can never land inside a
  hashtag or mention. Typing is never faster than it was before.
  ([#2](https://github.com/gidde032/RicePoster/issues/2))

- A successful reschedule (`PATCH /api/queue/{batch_id}`) returns the persisted
  batch instead of a null body, read back from the queue so the caller sees
  stored state rather than an echo of its own request. Not-found remains 404. A
  missing `fire_time` moved from 400 to 422, superseding the original issue's
  "validation responses unchanged" clause by design.
  ([#10](https://github.com/gidde032/RicePoster/issues/10))

- `reschedule_batch` no longer surfaces a bodiless 500 when the queue layer's
  independent `fire_time` re-validation rejects a value the request model
  accepted. It is guarded to 422, matching `schedule_batch`. Reaching it
  requires roughly a minute to elapse mid-request, so it was not reachable in
  normal single-user operation.

- Caption-generation failures no longer land in the caption box. A failed
  generation previously wrote `[Error generating caption: ...]` into the
  textarea, where an error string was indistinguishable from a real caption and
  could be posted to a live account. Failures now leave the caption empty — so
  Post All stays disabled for that slot — and report themselves on a line
  beside it. Regenerate reports there too, instead of only flashing a button
  label for 2.5 seconds. ([#31](https://github.com/gidde032/RicePoster/issues/31))

- An open queue panel refreshes every 10 seconds, so a batch that fired, was
  cancelled, or came back interrupted no longer stays visibly stale until the
  panel is reopened. Polling runs only while the panel is visible and cannot
  stack timers across open/close cycles.
  ([#7](https://github.com/gidde032/RicePoster/issues/7))

- History and queue load failures are now visible in their panels instead of
  going only to the console, where a stuck panel looked like a slow one.
  ([#31](https://github.com/gidde032/RicePoster/issues/31))

- A scheduled batch whose history row cannot be written now sends a
  high-priority push naming the batch, instead of only printing to the server
  console. Unattended runs have no other audience, and the retained queue entry
  carries a terminal status the queue panel does not surface — so the fact that
  a batch ran could be invisible until someone read the log. The queue record
  and media snapshot are still retained as the surviving evidence, and the push
  is routed through the existing swallow-everything sender, so a broken
  notification service cannot turn a failed history write into a dead
  scheduler. ([#5](https://github.com/gidde032/RicePoster/issues/5))

- A scheduled run that posted successfully can no longer be reported as failed.
  Whatever makes the history write fail — a full or read-only disk — is just as
  likely to make the follow-up status write fail, and that second failure used
  to be handled as a crash: the batch was forced to `failed` and the maintainer
  was told the slots might not have posted. They had. Acting on that message
  means a duplicate live post, so the recorded outcome now survives a failure
  to persist it. ([#5](https://github.com/gidde032/RicePoster/issues/5))

- `POST /api/queue` answers 422 rather than 500 when the queue layer's own
  `fire_time` re-validation rejects a value the request model accepted,
  matching what `PATCH /api/queue/{batch_id}` has always done. Both are
  effectively unreachable in single-user operation; the point is that the two
  endpoints no longer disagree about whose fault the failure is.
  ([#6](https://github.com/gidde032/RicePoster/issues/6))

### Added

- `LOG_LEVEL` controls how much the backend prints. `INFO` (the default) keeps
  the full browser-automation narration — every upload step, caption check and
  confirmation — exactly as before. `WARNING` keeps only degraded and failed
  states, which suits leaving scheduled batches to run unattended. An
  unrecognised value is reported as a startup problem and falls back to `INFO`,
  rather than silently leaving you at a verbosity you did not ask for.

  `session_manager`'s status and health-check reports are deliberately outside
  this control: they are the output of a command you just ran, so no log level
  can suppress them.
  ([#26](https://github.com/gidde032/RicePoster/issues/26))

- Queue media reconciliation. Snapshot directories under `queue_media/` are
  classified at every server start against the queue and the history, and only
  provable orphans are removed: a batch whose every recorded slot posted
  cleanly, or one referenced by neither file and older than an hour. Media for
  a failed, partial, interrupted, still-queued, or ambiguous batch is never
  deleted automatically, and a directory the application did not create is
  never deleted at all — for a partial batch that snapshot is the only copy of
  the media still under RicePoster's control, and history records only *that* a
  slot failed, never the file. Previously a cancelled batch that crashed
  between saving the queue and deleting its snapshot left media behind with
  nothing to reconcile it against, and retained evidence and true orphans were
  indistinguishable on disk. Automatic deletion suspends itself entirely while
  either record contains an unreadable line, since a skipped queue row makes a
  still-scheduled batch's media look exactly like an orphan; repairing the file
  restores normal cleanup.
  ([#4](https://github.com/gidde032/RicePoster/issues/4))

- A retained-media section in the queue panel, with the classification and the
  reason each snapshot was kept, and a confirmation-gated delete backed by
  `GET /api/queue/media` and `DELETE /api/queue/media/{batch_id}`. Retained
  evidence can be removed only this way, never by the automatic pass. The queue
  badge counts retained snapshots too: a partial batch leaves media behind and
  removes its queue entry, so a badge keyed on batches alone would have hidden
  the panel exactly when it had something to review.
  ([#4](https://github.com/gidde032/RicePoster/issues/4))

- Timeouts on frontend network calls: 15 seconds for reads and deletes, 75
  seconds for caption generation. A hung backend previously left the UI loading
  forever with no recovery short of a reload. `POST /api/post` and
  `POST /api/queue` are deliberately exempt — aborting the client does not stop
  a run that is already driving live accounts, so a spurious timeout would
  report failure while posts continued.
  ([#31](https://github.com/gidde032/RicePoster/issues/31))

- Request validation on the two endpoints that bypassed Pydantic.
  `POST /api/generate-caption` moved to a `CaptionRequest` model bound with
  `Annotated[..., Form()]`, so it still accepts the multipart body the UI sends
  while a bad field becomes a 422 that names it; `media_type` is constrained to
  `video`/`image`. `PATCH /api/queue/{batch_id}` moved off a raw dict onto
  `RescheduleRequest`. The `fire_time` rules that the queue POST and PATCH each
  enforced by hand are unified into one shared validator, so the two endpoints
  can no longer drift apart.
  ([#33](https://github.com/gidde032/RicePoster/issues/33))

- A `RequestValidationError` handler that flattens Pydantic's list-of-dicts
  body into a single string `detail`. Without it the UI, which renders
  `detail` directly, showed `[object Object]` for a validation failure.

### Changed

- Every diagnostic and every line of browser-automation progress now goes
  through the `logging` module instead of a bare `print`, which is what makes
  `LOG_LEVEL` possible. The messages themselves are unchanged — the text you
  see during a run at the default level is byte-for-byte what it was before,
  pinned by a golden captured from the previous release.
  ([#26](https://github.com/gidde032/RicePoster/issues/26))

- The Windows Chrome lookup uses `pathlib`, matching the rest of the module.
  Behaviour is unchanged on every platform, and the branch is now covered by
  tests that fake the platform so the paths it builds can be checked from a
  Mac.
  ([#42](https://github.com/gidde032/RicePoster/issues/42))

- A successful TikTok post finishes sooner. The upload flow ran two
  confirmation waits back to back, and the second could only ever repeat what
  the first had already established — so a post that was confirmed immediately
  still waited up to a further 25 seconds before returning. The second wait now
  runs only when the first found nothing. Posts whose success was never
  observed are still reported as unconfirmed exactly as before.
  ([#29](https://github.com/gidde032/RicePoster/issues/29))

- The Instagram and TikTok posting flows are now built from named steps rather
  than two single functions of 185 and 235 lines. No selector, wait, timeout or
  ordering changed; the browser behaviour of both flows is pinned call-by-call
  by recorded transcripts so the restructure could be proven inert.
  ([#28](https://github.com/gidde032/RicePoster/issues/28))

- Each slot's queued media is snapshotted to a distinct file. Snapshots were
  named by base name alone, so two slots whose media resolved to the same file
  name landed on one file — the second copy overwrote the first and both queue
  rows then pointed at the survivor, posting one slot's media to a second
  account. Uploads through the UI are prefixed per slot and could not collide;
  a handcrafted API request could. Names are disambiguated only on collision,
  so retained evidence keeps a readable filename.
  ([#4](https://github.com/gidde032/RicePoster/issues/4))

- Queue rows tolerate unknown fields at the top level, not only inside slots.
  A retired top-level field would have made `load_queue` report the row as
  malformed and skip it, silently discarding a scheduled batch — the hazard
  already fixed for slot fields. Rows that genuinely cannot be parsed are still
  skipped with a log line, since tolerance covers fields no longer needed, not
  fields never supplied. No row currently on disk was affected.
  ([#46](https://github.com/gidde032/RicePoster/issues/46))

- Internal frontend cleanup with no behavior change: the fetch-error
  extraction copy-pasted across six call sites, the slot payload duplicated
  between posting and scheduling, and ~14 unguarded `getElementById` lookups
  now each have a single definition. A missing DOM id reports which id is
  missing rather than failing later as a null dereference.
  ([#30](https://github.com/gidde032/RicePoster/issues/30))

- Internal API cleanup with no behavior change: the posting-progress state
  became a `PostProgress` object whose `start()` and `finish()` own the
  lifecycle rules. The reset list was previously kept in sync with a dict
  literal by hand. Splitting it also makes explicit that `finish()` must retain
  events, because the UI still renders the most recent run's results after a
  run ends. ([#34](https://github.com/gidde032/RicePoster/issues/34))

- Session-manager usage examples and validation messages are generated from the
  configured slot roster instead of hardcoding `A, B, C`. Running
  `ACCOUNT_SLOTS=A,B,C,D` previously produced `Invalid slot 'D'. Use A, B, or
  C.` — the tool refusing a slot it had itself configured. Slot arguments are
  now matched case-insensitively against the configured ids rather than
  uppercased, so a lowercase or mixed-case roster works from the command line.
  ([#9](https://github.com/gidde032/RicePoster/issues/9))

- A slot listed in `ACCOUNT_SLOTS` with a blank token is now reported at
  startup instead of appearing fully configured and failing when that slot's
  post is attempted. Reported, not fatal, and only under `POST_MODE=api`, which
  is the only mode that reads these tokens. The message names the variable and
  never its value. ([#36](https://github.com/gidde032/RicePoster/issues/36))

- The unattended session health check logs the exception message alongside its
  type. A navigation failure, a missing selector and a genuine timeout
  previously printed the identical line, and that console line is the only
  record a scheduled batch leaves.
  ([#36](https://github.com/gidde032/RicePoster/issues/36))

- Internal cleanup with no behavior change: filesystem paths are derived once
  in `config.py` rather than rebuilt in eight modules, with the `sessions/`
  segment previously spelled out independently in four of them. All fourteen
  values are unchanged — a test written before the refactor pins each to its
  prior literal, and two further tests assert the segment is now joined in
  exactly one place. ([#27](https://github.com/gidde032/RicePoster/issues/27))

- Internal cleanup with no behavior change: one shared boolean-environment
  parser, and removal of the unused `generate_all_captions` helper. It had no
  production caller — captions are generated one slot at a time through
  `generate_caption` — so it was deleted rather than given the typed model the
  audit suggested. ([#36](https://github.com/gidde032/RicePoster/issues/36))

- The `fire_time` rule now has a single implementation shared by the API and
  queue layers, parameterised by the required lead time: one minute for a web
  request, none for the queue layer. The two layers still deliberately differ,
  because the queue layer re-checks a moment later and its looser bound is what
  absorbs the elapsed time — making them identical would let a request that
  cleared the API check by a fraction of a second fail the queue check purely
  because the clock moved. What changes is that the rule itself can no longer
  drift between the layers; only the two named lead-time constants express the
  intended difference. No accepted or rejected time changes.
  ([#6](https://github.com/gidde032/RicePoster/issues/6))

- The autouse safety tripwire that stops a test reaching a live posting,
  caption, or notification path is now pinned as a contract rather than
  described by a comment. Its eleven entry points are each called and must
  raise, and the set of targets it patches is checked in both directions, so
  dropping one — or adding one without pinning it — fails the suite. Five of
  the twelve previously had no test that actually called them. This matters
  because the tripwire is the only thing standing between a mistaken test and
  a real Instagram or TikTok account, and narrowing it would not have made any
  quality gate go red.
  ([#37](https://github.com/gidde032/RicePoster/issues/37))

- The Anthropic caption client joined that guard. Only the route alias was
  blocked before, so a test calling `captions.generate_caption` directly
  without installing a fake could have made a real, billed API request. It
  could only happen when an API key was exported in the developer's shell —
  the key is empty in a default test run and in CI, and the caption path
  fails fast without one — but the suite's stated guarantee is that no test
  reaches the caption API, and now the mechanism matches the promise.
  ([#37](https://github.com/gidde032/RicePoster/issues/37))

- The smoke-tier gate counts collected tests instead of pattern-matching
  pytest's summary sentence, so a future pytest release reformatting that line
  can no longer turn a green gate red on its own. Test fixtures that had been
  copied across several files — the frontend source reader, the caption-API
  fake, and a duplicate history-file redirect — are now defined once and
  shared, and three regression-test docstrings name the incident they came
  from instead of relying on a section comment above them.
  ([#37](https://github.com/gidde032/RicePoster/issues/37))

### Removed

- `SlotBatch.style` is gone from the queue schema. Scheduled captions are
  finalised before a batch is queued, so the field was written empty and never
  read back; retaining it implied a caption-regeneration behaviour that does
  not exist. Queue rows written by an earlier version still load: unknown slot
  fields are dropped rather than raised. That path is load-bearing rather than
  defensive — `load_queue` reports a constructor failure as a *malformed line*
  and skips it, so without it a legacy batch would have been silently discarded
  instead of firing. ([#8](https://github.com/gidde032/RicePoster/issues/8))

## [0.3.0] — 2026-07-28

### Added

- MIT License, copyright 2026 Finn Gidden.

- GitHub-native contribution workflow (2026-07-28): structured bug and planned
  work forms, a pull-request template with safety and documentation accounting,
  and read-only GitHub Actions CI that runs the full suite plus the 43% coverage
  floor on pull requests and `main`.

- `tools/probe_fingerprint.py` (2026-07-27): offline fingerprint diagnostic.
  Launches Chrome with the Instagram posting configuration against a local
  `file://` page and prints what it reports about itself — WebGL renderer, user
  agent and client hints, screen geometry, pixel ratio. No platform contact, no
  `sessions/` access, throwaway profile per run; `tests/test_probe_safety.py`
  enforces all three. Settled a detection question that had been parked for
  weeks on the mistaken assumption that it needed live Instagram traffic.

### Changed

- Development now uses short-lived topic branches and pull requests targeting
  `main`. The old `dev` staging branch is retained temporarily for history but
  receives no new work. Repo-local `.skill` bundles were removed in favor of
  the canonical installed `agentic-workflow` plugin.

- Per-slot device identity is now internally consistent (2026-07-27).
  `device_identity.DISPLAYS` became the source of truth (size, pixel ratio,
  laptop/external kind) and `VIEWPORTS` is derived from it, so the two cannot
  drift apart. `screen` and `device_scale_factor` are passed to the Instagram
  launch alongside the viewport.

  Probe-measured before the change: `window.screen` reported the viewport's own
  size, so `screen.height == innerHeight` — a page area exactly filling the
  display with no window furniture, which real hardware cannot produce — while
  `devicePixelRatio` stayed 1 despite the dimensions claiming a MacBook Pro.
  After: screen 1512×982 against viewport 1512×871, ratio 2. No configured slot
  changed device. Known residual: `availHeight` still equals `height`, which
  Playwright exposes no way to set.

  `HEADLESS=false` remains the only way to remove the other two measured tells
  (the `HeadlessChrome` user agent and its client-hint mismatch, and
  `colorDepth` 24 against a real Mac's 30) — Chrome derives both from the mode
  it started in.

### Fixed

- The test suite no longer writes to the maintainer's real `history.jsonl`
  (2026-07-27). `main.HISTORY_FILE` had no test redirect, so every run appended
  7 rows, and 750 of 1054 rows were fixture data by the time it was noticed.
  The write is wrapped in a bare `except`, so the pollution was silent. Now
  redirected by an autouse conftest fixture, matching the existing
  `_IG_SESSIONS_DIR` pattern.

### Added (earlier)

- Headless toggle: the Headless/Visible badge in the header is now
  clickable and overrides the env default for the current session's runs —
  no more editing credentials.env to watch a run.
- Post history log: every run appends per-slot records (time, file,
  caption, per-platform result, mode) to gitignored `history.jsonl`;
  a History panel in the UI shows the latest 25 entries via /api/history.

- Thumbnail-based caption generation (2026-07-19): on upload the frontend
  captures a frame (~1s in for video, the image itself otherwise), downscales
  it to a 512px-long-edge JPEG and shows it as a chip under the drop zone
  ("the frame the caption AI sees"). Sent with both caption paths and attached
  as an Anthropic image block. Backend validates the base64 and caps it at 2MB.
  No thumbnail → the request is identical to pre-feature; capture failure is
  non-fatal and falls back to text-only.

- Adjustable account count (2026-07-19): the hardcoded A/B/C slot list is now
  driven by `ACCOUNT_SLOTS` in credentials.env (default `A,B,C`), validated
  filesystem-safe at startup. `/api/post` moved from nine fixed form fields to
  a JSON body (`PostRequest`, with a duplicate-slot guard); the frontend builds
  its slots from `/api/accounts`.

- Unattended-posting stack (FR-F1–F4, 2026-07-19 → 07-20). Three slices:
  - TikTok cookie write-back after a successful browser post (atomic
    temp+replace, `.bak` of the previous file), plus
    `session_manager check <slot> <platform>` — a lightweight browser session
    health check with full lifecycle cleanup.
  - Failure notifications via ntfy.sh (`backend/notifier.py`): per-slot
    failures, unconfirmed results and a run summary. Caption text never leaves
    the machine — a sanitizer runs at the notification boundary. Notification
    failures never block posting.
  - Scheduling / queue: `backend/queue.py` (JSONL persistence with atomic
    rewrite, per-batch media snapshots), `backend/scheduler.py` (background
    asyncio task on a 30s poll, startup sweep marking stale `running` batches
    `interrupted`, pre-flight session checks), `backend/run_guard.py`
    (shared flag serializing `/api/post` against the scheduler), queue API
    endpoints, and a frontend Schedule button, datetime picker and queue panel.

- Instagram anti-detection fixes F1–F6 (2026-07-26). **First live validation
  passed (2026-07-26):** TikTok and Instagram both posted successfully in
  headless mode, and a scheduled Instagram batch fired unattended through the
  queue, appeared in history and reported progress correctly. One claim
  remained unconfirmed at the time: F2 needed a **visible-mode** run to prove
  video rendering survives the SwiftShader removal.
  - F1: removed a hardcoded `Chrome/126` User-Agent override at 3 sites — the
    installed browser is Chrome 150 and Playwright overrides only the UA
    string, leaving `Sec-CH-UA` and `navigator.userAgentData` contradicting it.
  - F2: removed SwiftShader/ANGLE forced software rendering from Instagram,
    which made WebGL report a software renderer — a well-known bot signature.
  - F3: `backend/device_identity.py` — a distinct, stable per-slot viewport, so
    the accounts stop sharing one identical device fingerprint. Instagram only.
  - F4: `backend/jitter.py` — all 15 Instagram waits now vary above their
    existing floors (never shorter), plus randomised inter-account spacing
    with a UI countdown.
  - F5: session health checks are cached for 6h and now dwell and scroll; the
    old check loaded a page, tested a selector and left, which is a cleaner bot
    signature than actually posting. `PREFLIGHT_CHECK_PLATFORMS` opts out.
  - F6: removed 5 redundant `navigator.webdriver` init scripts (a redefined
    property descriptor is itself detectable); the remaining launch flag is
    labelled legacy, not coverage.

### Fixed

- **Config surface hardening (2026-07-26 review, findings #4/#5/#7/#8).**
  Three silent-failure modes closed, all of which produced no local symptom:
  - Configuring more `ACCOUNT_SLOTS` than `device_identity.VIEWPORTS` has
    entries now refuses to start. The positional index wrapped, so a 6th slot
    silently received slot A's exact viewport — rebuilding the shared device
    fingerprint F3 exists to destroy. **Behaviour change:** a >5-slot config
    that used to start now fails loudly with an explanatory message.
  - `PREFLIGHT_CHECK_PLATFORMS=` (empty) disabled the pre-flight check for
    every platform. Empty now means unset and falls back to the default;
    `none` is the explicit off switch, and unknown platform names are
    rejected rather than silently dropping that platform's check.
    **Behaviour change** for anyone relying on empty-means-off.
  - A typo'd numeric knob crashed at import with a bare `ValueError` naming
    neither the variable nor the file; the message now names all three.
  - All eight previously undocumented env knobs added to `README.md` and
    `credentials.env.example`, pinned by a test that reads `config.py`'s live
    `getenv` list so the next undocumented knob fails the suite.

- `credentials.env.example` is no longer gitignored. It was ignored alongside
  `credentials.env`, so a fresh clone received a README instructing it to copy
  a file that was not there.

- **A scheduled batch now loses its media only when every slot posted
  successfully** (2026-07-27). `partial` and `failed` batches used to delete
  their `queue_media/{id}/` snapshot along with the queue record, destroying
  the media of exactly the slots that had not gone out — history records
  *that* a slot failed, never the file, so retrying meant re-uploading. Both
  now retain the snapshot, matching the crash path. Known cost: retained
  snapshots accumulate with no UI affordance to clear them; delete by hand
  after checking the matching `history.jsonl` rows until the reconciliation
  pass lands.

- The scheduler no longer rewrites the whole queue file when a status update
  finds no matching batch (finding #10).

- The session health check's dwell and scroll now use `sleep_jittered` rather
  than raw `asyncio.sleep` with an inline `random.uniform` (finding #9).
  Timing is unchanged; the waits are now covered by the floor-rule guards that
  previously only inspected `instagram_browser`.

- Scheduled batches now honour cancellation and rescheduling: `execute_batch`
  re-reads `queue.jsonl` and re-validates immediately before posting, so a
  batch cancelled or moved while an earlier run was in progress can no longer
  fire. Aborts send a notification.

- A crashed scheduled batch now leaves a full audit trail: history is written
  before the queue entry is pruned, every slot gets a row marked "may or may
  not have posted; verify manually", and the media snapshot is deliberately
  retained as the only surviving evidence of what was being posted.

- The pre-flight session check's per-platform verdict now reaches the poster.
  The scheduler notified "slot X <platform> skipped" and then handed the slot
  to `post_all` with that decision discarded; `post_slot` re-derived
  availability from `session_exists()`, which cannot distinguish an expired
  profile from a live one, and posted to the platform anyway.

- A failed `history.jsonl` write no longer erases the batch. The queue record
  is only pruned once the run is durably recorded elsewhere; otherwise it is
  retained (as `running` on the crash path, so the startup sweep flags it).

- The coverage gate now actually runs: `--cov=backend --cov-fail-under=43` is
  wired into the pre-push hook, with meta-tests asserting the flags are present
  and the floor matches the docs. The conftest safety tripwire was extended to
  cover `poster.post_all` and the `instagram`/`tiktok` `post_media` leaves,
  which were previously unguarded.

- TikTok failure diagnostics: posting failures now capture a `debug/`
  screenshot (parity with Instagram), and when a one-time TikTok dialog
  (e.g. "Turn on automatic content checks?") is blocking the page, its text
  is embedded in the error with instructions, instead of surfacing as a raw
  30s click timeout. Root cause of the 2/6 failures in the first full
  live run across all accounts.

### Review pass — 2026-07-26 (all 10 findings closed)

*Relocated from handoff.md 2026-07-27. Nothing here is open.*

Three contextless reviewers ran against the 11 unreviewed commits. Findings
#2 and #3 were fixed first; the remaining eight landed in three clusters —
config surface, #6, and test hygiene.

*Commit hashes were dropped here on 2026-08-02: history was squashed to
remove account-identifying blobs (#60), so the original SHAs no longer
resolve. They were the only commit citations in a tracked document, and the
findings they pointed at are closed.*

Two outcomes worth carrying forward:

- **#6 was resolved as already-covered, not fixed.** The finding said
  `partial` batches notify nobody. False: `poster_browser.post_all` owns the
  run summary and already pushes "2/3 posted, 1 failed: [...]" at high
  priority, and `execute_batch` threads its notifier through. Notification is
  deliberately two-layer — the poster owns the run summary, the scheduler owns
  aborts, pre-flight skips, whole-batch failure and crashes. A batch-level
  partial push would double-notify. Three tests now pin the guarantee, which
  nothing did before; validated by mutation, as there was no pre-fix state to
  fail against.
- **Residual gap, closed 2026-07-27.** `partial` deleted its media snapshot
  while the crash path retained it. Scope was widened on maintainer decision
  to cover `failed` too. The rule is now one line: **a batch loses its media
  only when every slot posted successfully.**

Behaviour changes from this pass:

1. A config with more than 5 slots **refuses to start** rather than silently
   cloning a device fingerprint.
2. `PREFLIGHT_CHECK_PLATFORMS=` (empty) means *unset* → both platforms
   checked. It used to disable all pre-flight. `none` is the off switch.
3. A typo'd numeric env knob fails at import with a message naming it.
4. `credentials.env.example` is tracked; it had been gitignored.

### Live validation history

Every release below was validated against live accounts before shipping, and
F2 was settled by a visible-mode run on 2026-07-27 that confirmed video
rendering survives the SwiftShader removal.

The per-run log is deliberately maintainer-local. It records dated posting
activity on identified account slots, which is account-identifying information
rather than release history.

### Withdrawn — TikTok edge-crop investigation (2026-07-27)

A reported defect, "TikTok crops the edges of posted videos", was
investigated during the Phase 1 audit and concluded to be *"introduced by our
automation, not by TikTok"*, with a D1–D4 workplan. **The conclusion was
wrong and the whole line of work is withdrawn.** The maintainer walked
TikTok's web posting flow manually: it has no crop or framing control at all,
unlike Instagram's. Variable edge loss is TikTok's own rendering across
differing phone viewports, and is handled by leaving safe margins when
editing clips — there is nothing for this codebase to fix. The research
document was renamed `RESEARCH-platform-detection.md` and the section
removed; see `pending-lessons.md` for the reasoning-error lesson.

## [0.2.0] — 2026-07-18

*All features below validated by the maintainer with a live posting run.*

### Added

- Per-slot caption regeneration: a Regenerate button under each caption box
  sends the current caption as an anti-repeat reference plus an optional
  feedback tweak ("shorter, punchier") to steer the redo; Undo swaps back
  (and doubles as redo). The global Generate Captions button now only fills
  slots with empty caption boxes.

- UI polish pass ("RicePoster" rename): live per-slot/platform progress while
  a posting run is active (polled from /api/post-progress), upload progress
  bar for large video files, caption character counter with the 2,200 limit,
  session dots on each slot card, auto-growing caption boxes, a New Run reset
  button, and a media-library size display with a Clear control
  (/api/media-info, /api/media/clear — blocked during an active run)

- Rotatable caption styles: prompts moved from a hardcoded constant into
  `prompts/*.json`, selectable per slot from a UI dropdown. Seed styles:
  generic (default), meme-humor, sports, music-fanpage. Styles are hot-reloaded
  per request; unknown styles return a clear 400.

## [0.1.0] — 2026-07-18

### Added

- FastAPI web UI for posting unique media + captions to 3 Instagram and
  3 TikTok accounts (slots A/B/C) in one run
- Playwright browser-mode posting: persistent Chrome profiles for Instagram,
  exported-cookie injection for TikTok, sequential per-slot execution
- AI caption generation via the Anthropic API with a house-style system prompt;
  per-slot topics, concurrent generation, in-place editing
- Instagram 9:16 "Original" aspect-ratio forcing on all posts
- Session management CLI (`login` / `status` / `clear`), with abort-on-failed-login
  cleanup
- `POST_MODE` switch: `mock` / `browser` / `api` (api clients are inactive
  skeletons)
- Test suite: 52 tests (baseline behavior + per-finding regressions), fully
  mocked with a tripwire that prevents any live call

### Fixed (Phase A review, same release)

- Posts that never show a platform confirmation are reported as ⚠ unconfirmed
  instead of falsely successful
- Server binds 127.0.0.1 (was 0.0.0.0 with an unauthenticated posting API)
- `/api/post` validates filenames (path traversal + existence); uploads
  sanitize filenames and validate slots
- `clear` with a mistyped platform no longer deletes the TikTok session
- Debug screenshots moved to gitignored `debug/` and untracked from git
- Concurrent post runs blocked with a clear 409 (Chrome profile-lock protection)
- Instagram detects expired sessions with an actionable message
- Error text is HTML-escaped in the UI; API-mode errors redact tokens;
  tokens stored as `SecretStr`; missing `ANTHROPIC_API_KEY` fails fast
- TikTok caption garbling: TikTok pre-fills the caption with the uploaded
  filename and its editor survives DOM wipes, splicing the real caption into
  filename text. Now uploads a short-named temp copy, clears via keyboard,
  inserts the caption atomically, and verifies the field before posting
  (aborts on mismatch instead of posting garbled text)

### Removed

- Stories (Instagram + TikTok, browser and API paths) — removed entirely and
  permanently by maintainer decision

### Known limitations

- TikTok cookies expire ~30–60 days from export with no auto-refresh
  (write-back is designed and roadmapped)
- `session_manager status` can't detect a stale-but-present session without
  attempting a post
- Sequential posting only; api mode incomplete; single-user, localhost-only
