# RicePoster Roadmap

Updated 2026-08-12. GitHub Issues are the source of truth for open work; this
document records direction, priority horizons, and boundaries without
duplicating issue bodies.

## Product destination

RicePoster supports a **five-minute daily workflow**: prepare unique media and
captions for each account, schedule the batch, and spend daily effort on
comment replies. The core unattended-posting stack is complete.

The product remains a local, single-maintainer tool. It favors reliable
sequential posting, reviewable captions, and honest results over throughput or
hands-off growth.

## Current milestone

### v1.0.0 — Public release

The repository is public. The security and public-readiness audit
([#50](https://github.com/gidde032/RicePoster/issues/50)) is complete, the
screenshot purge ([#60](https://github.com/gidde032/RicePoster/issues/60)) is
verified, and the `main` ruleset is enforced
([#72](https://github.com/gidde032/RicePoster/issues/72)).

[#51](https://github.com/gidde032/RicePoster/issues/51) (TikTok fixed sleeps)
remains and needs an approved live TikTok run before its PR leaves draft.

## Shipped milestones

Listed newest first. No Git tag or GitHub Release has been published for any
of these; the entries in `CHANGELOG.md` are the release record.

### v0.4.0 — Tech-debt campaign (released 2026-07-31)

Twenty-three issues drawn from the 2026-07-29 audit and the pre-existing
backlog. The milestone is **pass-shaped rather than phase-shaped**: audit,
rank, select a slice, deliver it, repeat. It adds no user-facing features by
design — it buys back the maintainability the unattended-posting stack was
built at speed against.

Work is partitioned into eight batches **by file surface**, because the backlog
collides on `main.py`, `frontend/index.html`, the two browser modules, and
`conftest.py`. Each batch is one branch, one bundled multi-issue pull request,
and one independent review cycle.

| Batch | Surface | State |
| --- | --- | --- |
| API contract and validation | `main.py`, `models.py` | Merged, PR #39 |
| Frontend robustness | `frontend/index.html` | Merged, PR #40 |
| Paths and conventions | `config.py`, browser modules | Merged, PR #43 |
| Queue and scheduler durability | `queue.py`, `scheduler.py` | Merged, PR #45 |
| Queue media reconciliation | `queue.py`, `frontend/index.html` | Merged, PR #47 |
| Poster internals | browser modules | Merged, PR #52 |
| Logging adoption | all of `backend/` | Merged, PR #54. Last code batch |
| Test infrastructure | `conftest.py` | Merged, PR #58. Last batch |

Three orderings are load-bearing. Logging adoption rewrites output lines in
nearly every backend file, so it goes last to avoid conflicting with the
earlier batches. The `conftest.py` work goes last because it is a shared
registry every earlier batch writes tests into. Path centralization precedes
the poster restructuring, because both edit the same browser modules.

Investigations that touch only `tools/` and are read-only run in parallel with
the code batches.

### v0.3.0 — Documentation and GitHub refresh (released 2026-07-28)

GitHub Issues and pull requests became the durable planning and delivery
record, current state was separated from shipped history, and clean-clone CI
was put in place. Completed by
[issue #1](https://github.com/gidde032/RicePoster/issues/1) and merged through
[pull request #25](https://github.com/gidde032/RicePoster/pull/25).

## Next candidates

No milestone beyond v1.0.0 is committed. Product candidates
[#18](https://github.com/gidde032/RicePoster/issues/18)–[#21](https://github.com/gidde032/RicePoster/issues/21)
are parked pending discovery; they were deliberately excluded from the
tech-debt campaign, whose scope is debt and investigations only.

## Later opportunities

Durable but unselected ideas remain unmilestoned. This now includes the five
read-only investigations (#3, #13, #15, #16, #17) and two follow-ups (#55,
#57), which were pushed past the v0.4.0 release rather than held against it.

- Scheduling and account behavior:
  [#12](https://github.com/gidde032/RicePoster/issues/12),
  [#16](https://github.com/gidde032/RicePoster/issues/16), and
  [#21](https://github.com/gidde032/RicePoster/issues/21).
- Offline diagnostics:
  [#14](https://github.com/gidde032/RicePoster/issues/14),
  [#15](https://github.com/gidde032/RicePoster/issues/15), and
  [#17](https://github.com/gidde032/RicePoster/issues/17).
- Caption and media workflow:
  [#18](https://github.com/gidde032/RicePoster/issues/18),
  [#19](https://github.com/gidde032/RicePoster/issues/19), and
  [#20](https://github.com/gidde032/RicePoster/issues/20).
- Trigger-based maintenance:
  [#11](https://github.com/gidde032/RicePoster/issues/11),
  [#13](https://github.com/gidde032/RicePoster/issues/13),
  [#22](https://github.com/gidde032/RicePoster/issues/22),
  [#23](https://github.com/gidde032/RicePoster/issues/23), and
  [#24](https://github.com/gidde032/RicePoster/issues/24).

## Deliberate non-goals

- Stories.
- Concurrent slot posting; sequential posting is a reliability decision.
- Automatic dismissal of TikTok's one-time account dialogs.
- Completing the dormant official API clients.
- LAN or hosted deployment; the unauthenticated service stays localhost-only.
- Performance optimization without a demonstrated workflow problem.
- Visible synthetic mouse or hover behavior, unless the existing F1–F6
  mitigations prove insufficient in practice.
- A frontend framework, or splitting `frontend/index.html` for its own sake.
  The single inline-CSS/JS file is an accepted simplicity trade-off; it is the
  ceiling on navigability, and that is priced in. Recorded here because audits
  keep re-raising the file's size as debt. If it is ever split, peel CSS and JS
  into separate files — do not introduce a framework.

Completed feature history belongs in `CHANGELOG.md`. Detailed scope,
acceptance criteria, evidence, and status belong in the linked Issues.
