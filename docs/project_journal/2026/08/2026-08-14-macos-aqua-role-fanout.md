---
id: 20260814-macos-aqua-release-identity
title: macOS Aqua Scheduler and Release Identity
status: completed
created: 2026-08-14
updated: 2026-08-14
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

## Current State

- Aqua installation preserves the scheduler's existing `HOME`, `WorkingDirectory`, `Umask`, throttle, low-priority IO, stable-runner, and transaction-hardening contracts while moving the canonical launchd identity to `gui/$UID`.
- Bare repair preserves audited target settings and migrates only exact historical profiles. Activation removes the precise Background identity before enabling and bootstrapping the canonical Aqua identity.
- `release-identities` holds the installation lock, validates each installed manifest and full tree, and rejects any current-pointer change across the validation boundary.
- The canonical repository does not infer host roles or implement SSH fanout. A headless Mac must not install this Aqua scheduler.

## Boundary

- Ordinary release installation does not mutate scheduler state; scheduler installation and migration remain explicit host operations.
- Aqua scheduling requires a live GUI login. Host inventory, role activation, controller/target relationships, SSH retry, pending state, and notification policy are downstream private-overlay concerns.
- The identity query is read-only evidence. It does not perform synchronization, determine whether a host is GUI-capable, or attest remote convergence.

## Downstream Handoff

- Refresh the generated `codex-toolbox` consumer from this canonical source and publish its immutable public release.
- Let `codex-private-workflows` consume that exact release before implementing and activating role-aware controller fanout.
- No private fanout implementation is claimed by this completed canonical slice.

## Evidence

- Implementation: `scripts/codex_personal_sync.py`.
- Operator contract: `README.md` and `docs/ARCHITECTURE.md`.
- Superseded decision: `docs/project_journal/2026/08/2026-08-04-macos-background-launchagent.md`.
