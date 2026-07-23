---
id: 20260723-canonical-sync-engine
title: Canonical Sync Engine
status: active
created: 2026-07-23
updated: 2026-07-23
branch: codex/canonical-sync-engine
pr:
supersedes: []
superseded_by:
---

# Canonical Sync Engine

## Summary

- Delivery status: `audit_rework_complete_uncommitted`.
- The workstream is consolidating personal sync ownership in `Joey-Tools/codex-personal-sync` and hardening mirror generation, scheduler observability, active-skill auditing, reconciliation, and release retention.
- The bounded canonical audit-rework batch is implemented and validated in the
  local worktree. It remains intentionally uncommitted; no push, consumer
  generation, or PR mutation was performed.

## Scope

- Establish a one-way canonical source lock and explicit `toolbox` / `private` mirror generation boundary.
- Keep release trees immutable after publication, bind `current` and managed-link transitions to durable evidence, and make `removed_links` an exact migration proof rather than a broad deletion authority.
- Run schedulers through the stable installed runner, preserve audited interval configuration, and publish a bounded runtime status contract.
- Audit the active skill discovery root read-only.
- Prune only exact, unreferenced release directory objects through quarantine with durable recovery and clear evidence.
- Document the resulting contracts in [ARCHITECTURE.md](../../../ARCHITECTURE.md).

## Current State

- The rejected pre-audit evidence has been superseded by the current frozen
  source-lock and whole-repository gates below.
- Scheduler installation now binds the exact semantically audited macOS/Linux
  config snapshots through conditional writes and matching-config
  revalidation. Required `current` releases are strict, code-less runtime
  failures are preserved, and `.system` receives cache/backup detection.
- Managed-state transactions bind legacy uid/gid access policy and require the
  effective owner on fixed-mode 0600 after-state files. Uninstalled
  `prune-releases --dry-run` no longer creates lock or personal-sync state.
- Mirror generation now uses filter-free tree/index/worktree parity, bound
  roots and managed ancestors, frozen live Git controls, an fd-addressed fixed
  launcher, durable private snapshot ownership/recovery, bounded operations,
  prior-commit journal recovery, and bidirectional reserved-path rejection.
- Canonical-master toolbox automation, tests, and its least-privilege
  `CODEX_TOOLBOX_SYNC_TOKEN` interface are documented. No credential currently
  exists and none was invented or installed.

## Validation Evidence

- `python3 -B -m unittest discover -s tests -q -b`: 622 tests passed in
  489.089 seconds.
- `python3 -B -m unittest -q -b tests/test_source_lock.py`: 69 tests passed in
  433.216 seconds after the final lock refresh.
- Focused suites passed on the frozen implementation: scheduler/doctor 33,
  reconciliation safety 305, release retention 26, main engine behavior 181,
  and toolbox automation 8.
- `python3 -B scripts/sync_canonical_mirrors.py refresh-lock` refreshed six
  canonical sources; the final `refresh-lock --check` passed.
- `PYTHONPYCACHEPREFIX=<task-scoped-temp> python3 -B -m compileall -q scripts tests`
  passed. Eight temporary `.pyc` files were removed with the temporary
  directory; the repository contains no `__pycache__`, `.pyc`, or `.pyo`.
- Ruff 0.13.2 lint passed for `scripts` and `tests`; the generator and
  source-lock tests also pass Ruff format check. actionlint 1.7.12 passed both
  workflows, and `git diff --check` passed.

## Downstream Dependencies

- `Joey-Tools/codex-toolbox` consumes only files declared by the `toolbox` mirror in `sync-source-lock.json`.
- `Joey-Tools/codex-private-workflows` consumes only files declared by the `private` mirror in `sync-source-lock.json`.
- After canonical bytes and mode are finalized, the lock must be refreshed, both generated mirrors must be updated and checked, and consumer validation must run there. Consumer copies must not become alternate sources.

## Next Steps

- Generate and validate the declared downstream mirrors.
- Create the signed canonical commit and continue the downstream delivery
  workflow only when the parent workstream authorizes it.
- Provision `CODEX_TOOLBOX_SYNC_TOKEN` separately only if the repository owner
  wants the sync-PR workflow to become operational.
