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
- Every acquired ACL and qualifier object is offered to `acl_free`. Cleanup failure never masks an already-active admission or qualifier-decoding error, while a cleanup-only failure remains a fail-closed unverifiable admission.
- The protected property is access-policy safety, not raw ACL serialization. Raw ACL text/order, `ctime`, and unrelated extended-attribute changes do not independently prove an unsafe policy; the semantic gate is rerun at each admission boundary. A `ctime` change is always a revalidation trigger and is never ignored as content proof.
- A regular file with `ctime`-only drift receives at most one bounded same-bound-FD rehash against its retained `size || SHA-256`, followed by terminal ACL, metadata, settled-`ctime`, and canonical-name binding. The allowance is per retained-FD stage: capture `file_fd` and later reopened verification `entry_fd` are independent, while a second drift within one stage fails before another content read. A directory with `ctime`-only drift is not byte-hashed: its retained parent FD binds exact member names plus each immediate child's exact device, inode, type, mode, size, `mtime`, and `ctime`, recursively at every directory level.
- Initial snapshot construction performs terminal and stable retained-FD member rescans, including the immediate-child identity check, after visiting each directory's children. Later verification adds a deepest-first postorder directory admission pass so parents are finalized after children. Per-directory scan count is constant and member-count bounded under the existing tree caps; the closure neither retains unbounded FDs nor rehashes files solely for directory `ctime` drift. Existing independent directory signals, including `mtime`, remain enforced, so create-and-remove child churn can still reject revalidation.
- Every successful Darwin install retains and finally revalidates the full synchronization-home-to-release ancestor descriptor chain for every next-current release, including exact no-op and managed-link-only paths. An empty `current` action set does not bypass admission.
- The install directory-chain container owns only the deduplicated unique strict-ancestor descriptors across owners. A release binding's `releases_fd` and `release_fd` are borrowed, remain outside that ownership set, and are revalidated separately.
- After initial chain validation, every non-no-op Darwin install, including a managed-link-only update, completes a point-in-time 80-FD `dup` headroom probe before pending staging or publication, `current`, managed-link, or state changes. All probe FDs close on success or failure. Exact no-op validates its chains but skips the mutation-only probe; the probe does not claim protection from concurrent same-process FD churn.
- Managed-state prepublication ACL drift preserves the stable `current-release-unverifiable` error code.
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
- Eighteen focused FD-ownership, headroom, error-code, directory-closure, and `ctime` revalidation tests passed 18/18 under both the current Python and macOS system Python 3.9.
- `tests.test_codex_personal_sync` exited zero under both runtimes with 285 tests run and one skipped per run. `tests.test_personal_sync_reconciliation_safety` exited zero under both runtimes with 305 tests per runtime.
- Source-lock currentness and serialization selectors passed 2/2 under both runtimes. Ruff, built-in compilation under both Python versions, `git diff --check`, and `refresh-lock --check` also passed.
- Superseded decision: `docs/project_journal/2026/08/2026-08-04-macos-background-launchagent.md`.
