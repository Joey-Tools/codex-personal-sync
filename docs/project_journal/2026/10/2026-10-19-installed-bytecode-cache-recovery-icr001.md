---
id: 20261019-installed-bytecode-cache-recovery-icr001
title: Installed Bytecode Cache Recovery
status: completed
created: 2026-10-19
updated: 2026-10-19
branch: wip/installer-cache-repair
pr:
supersedes: []
superseded_by:
---

# Installed Bytecode Cache Recovery

## Scope

- Recover local runtime bytecode caches during installation without accepting
  changes to the published release's source content or access policy.
- Preserve strict validation for incoming packages and observational commands.
- Bind repair to a verified same-SHA incoming source or a matching successful
  scheduler tree baseline; preserve unproven installed state.
- Keep dry-run observational and retain failed-repair evidence outside the
  installed release.

## Delivery

- Canonical ownership remains in `codex-personal-sync`.
- Promotion follows the receipt-bound toolbox mirror, immutable public
  release, and private overlay's exact public-release pin.
- Healthy schedulers activate the replacement through normal installation.
  Existing cache pollution can stop an older engine before it activates the
  replacement, so those hosts may require a verified-engine bootstrap.

## Validation

- Python 3.13.0 main installer module: 348 tests, 195.822 seconds, one skip,
  exit 0. The initial fixture-only failure placed case homes outside the mocked
  user home; the corrected fixture and entire main module were rerun.
- Source-lock module: 282 tests, 642.705 seconds, one skip, exit 0.
- Native Python 3.9.6 cache regressions: 17 tests, 23.898 seconds, exit 0.
- Scheduler module: 276 tests, 19.455 seconds, one skip, exit 0.
- The 11-source lock check, Python 3.9 syntax and in-memory compilation,
  undefined-name checks, whitespace checks, and project-journal validation
  passed. No full local repository-suite pass is claimed here.
- Additional regression results and exact-head hosted CI/review evidence belong
  to the delivery PR. A 1,297-test aggregate run exceeded its 1,800-second
  deadline and is incomplete; subsequent verification uses independent bounded
  groups with per-test diagnostics.

## Delivery Boundary

- This entry records the completed canonical implementation. Promotion uses
  the receipt-bound toolbox mirror and immutable public release, followed by
  a private bootstrap pin change that includes the corresponding generated
  receipt and managed files. Only then can forced private synchronization
  consume the new public base.
- Native scheduler activation and host recovery are separate operational
  changes. A healthy stable runner follows the coupled release installation;
  an already-blocked older engine may require the verified-engine bootstrap.
