# RicePoster Roadmap

Updated 2026-09-01. GitHub Issues are the source of truth for open work; this
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

No follow-on implementation is currently authorized. Select any future work
explicitly from the currently open GitHub Issues. Release, deployment,
live-session migration, and live validation remain separate, unauthorized
boundaries.

### Merged via PR #80 — account rosters and lightweight Stats

The account-roster slice extends the ratified Slate interface with
folder-discovered accounts, ordered saved rosters, local per-account caption
defaults, immutable scheduled targets, stable Instagram device assignments,
and database-free Stats. It completed offline verification and merged into
`main` via PR #80 on 2026-08-31; it remains unreleased, undeployed, and not
live-validated.

### Delivered — Slate sibling interface

RicePoster now uses the delivered RiceClipper Slate sibling interface across
its existing workflow, preserving the existing controls, handlers, endpoints,
payloads, and safety behavior. The visual redesign merged via PR #79; it
remains unreleased, undeployed, and not live-validated. Neutral Slate chrome
is shared; green, amber, and red remain reserved for real operational status.

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

## Future work

No post-v1 milestone or candidate is selected or authorized. Current
candidates live in the open GitHub Issues; selection requires Finn's explicit
direction. This roadmap does not duplicate a volatile issue inventory.

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
