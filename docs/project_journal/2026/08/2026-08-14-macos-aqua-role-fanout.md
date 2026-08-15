---
id: 20260814-macos-aqua-release-identity
title: macOS Aqua Scheduler and Release Identity
status: completed
created: 2026-08-14
updated: 2026-08-15
branch: wip/macos-role-aware-sync
pr:
supersedes: [20260804-macos-background-launchagent]
superseded_by:
---

# macOS Aqua Scheduler and Release Identity

## Summary

- The canonical macOS scheduler is a hardened per-user Aqua LaunchAgent in `gui/$UID`; Linux user-systemd behavior is unchanged.
- Exact historical Background `user/$UID` and loose GUI `gui/$UID` profiles remain auditable migration inputs, but neither is the canonical installed profile.
- A versioned, read-only `release-identities` command validates and reports the active public/private `sha` and `tree_sha256` values for downstream identity handoff.
- Darwin active-release and release-inventory admission revalidates the expected UID and semantic extended-ACL safety on every bound directory descriptor; non-Darwin behavior is unchanged.

## Current State

- Aqua installation preserves the scheduler's existing `HOME`, `WorkingDirectory`, `Umask`, throttle, low-priority IO, stable-runner, and transaction-hardening contracts while moving the canonical launchd identity to `gui/$UID`.
- Bare repair preserves audited target settings and migrates only exact historical profiles. Activation removes the precise Background identity before enabling and bootstrapping the canonical Aqua identity.
- `release-identities` holds the installation lock, validates each installed manifest and full tree, and rejects any current-pointer change across the validation boundary.
- The Darwin access-policy gate accepts no extended ACL, deny-only ACLs, and owner-only `ALLOW` entries. Any non-owner `ALLOW`, unknown ACL structure, failed query, or unreadable state fails closed at install or final admission.
- A retrieved Darwin ACL must pass `acl_valid` before entry enumeration. Invalid-argument is normal exhaustion only for a next-entry request; the same result on the first entry fails closed.
- The protected property is access-policy safety, not raw ACL serialization. Raw ACL text/order, `ctime`, and unrelated extended-attribute changes do not independently prove an unsafe policy; the semantic gate is rerun at each admission boundary. Existing independent directory signals, including `mtime`, remain enforced, so create-and-remove child churn can still reject revalidation.
- Every successful Darwin install retains and finally revalidates the full synchronization-home-to-release ancestor descriptor chain for every next-current release, including exact no-op and managed-link-only paths. An empty `current` action set does not bypass admission.
- The canonical repository does not infer host roles or implement SSH fanout. A headless Mac must not install this Aqua scheduler.

## Boundary

- Ordinary release installation does not mutate scheduler state; scheduler installation and migration remain explicit host operations.
- Aqua scheduling requires a live GUI login. Host inventory, role activation, controller/target relationships, SSH retry, pending state, and notification policy are downstream private-overlay concerns.
- The identity query is read-only evidence. It does not perform synchronization, determine whether a host is GUI-capable, or attest remote convergence.
- A parent-inherited unsafe ACL can block install or final active-inventory admission even when mode bits appear restrictive. The recovery action is administrator removal of the unsafe inherited ACL followed by retry, not bypassing the admission gate.

## Downstream Handoff

- Refresh the generated `codex-toolbox` consumer from this canonical source and publish its immutable public release.
- Let `codex-private-workflows` consume that exact release before implementing and activating role-aware controller fanout.
- No private fanout implementation is claimed by this completed canonical slice.

## Evidence

- Implementation: `scripts/codex_personal_sync.py`.
- Operator contract: `README.md` and `docs/ARCHITECTURE.md`.
- Seven new focused ACL and directory-chain tests passed 7/7 under both the current Python and macOS system Python 3.9; five error-priority compatibility selectors passed 5/5 under both runtimes.
- `tests.test_codex_personal_sync` exited zero under both runtimes with 263 tests run and one skipped per run. `tests.test_personal_sync_reconciliation_safety` exited zero under both runtimes with 305 tests run and no skips per run.
- Source-lock currentness and serialization selectors passed 2/2 under both runtimes. Ruff, built-in compilation under both Python versions, `git diff --check`, and `refresh-lock --check` also passed.
- Superseded decision: `docs/project_journal/2026/08/2026-08-04-macos-background-launchagent.md`.
