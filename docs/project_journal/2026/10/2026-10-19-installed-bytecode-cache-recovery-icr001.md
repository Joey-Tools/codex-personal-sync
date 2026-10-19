---
id: 20261019-installed-bytecode-cache-recovery-icr001
title: Installed Bytecode Cache Recovery
status: completed
created: 2026-10-19
updated: 2026-10-19
branch: wip/installer-cache-repair
pr: https://github.com/Joey-Tools/codex-personal-sync/pull/38
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
- Persist cache identity, content, and access evidence before isolation so an
  interrupted operation can resume under the installation lock. Preserve and
  report unknown or invalid recovery state.
- Require a strictly cache-free installed tree after isolation; a later cache
  writer cannot reuse the observational preflight exception.
- Bind active recovery batches to their original payload and receipt objects;
  replaced or missing evidence cannot fall through into restart recovery.
- Reject invalid release input before recovery, and require real installation
  locks in mutating internal-helper callers.

## Delivery

- Canonical ownership remains in `codex-personal-sync`.
- Promotion follows the receipt-bound toolbox mirror, immutable public
  release, and private overlay's exact public-release pin.
- Healthy schedulers activate the replacement through normal installation.
  Existing cache pollution can stop an older engine before it activates the
  replacement, so those hosts may require a verified-engine bootstrap.

## Validation

- Python 3.13.0 main installer and locked preflight recovery regressions:
  371 tests, 224.340 seconds, one skip, exit 0.
- Native Python 3.9.6 cache regressions: 30 tests, 60.833 seconds, exit 0.
- Input-order and locked-helper regressions: five test methods passed on
  Python 3.13.0 and 3.9.6, including all eight previously failing subcases.
- Canonical source-lock verification passed for all eleven managed sources.
- Regression coverage includes interrupted isolation and cleanup, invalid
  recovery controls, lock ownership, newly written caches after isolation,
  and replacement of an active receipt with an identical-content new inode.
- Python 3.9 syntax and in-memory compilation, undefined-name checks, and
  whitespace checks passed. Complete exact-head CI and review evidence is
  recorded in the delivery PR; a full local repository-suite pass is not claimed.

## Delivery Boundary

- This entry records the completed canonical implementation. Promotion uses
  the receipt-bound toolbox mirror and immutable public release, followed by
  a private bootstrap pin change that includes the corresponding generated
  receipt and managed files. Only then can forced private synchronization
  consume the new public base.
- Native scheduler activation and host recovery are separate operational
  changes. A healthy stable runner follows the coupled release installation;
  an already-blocked older engine may require the verified-engine bootstrap.
