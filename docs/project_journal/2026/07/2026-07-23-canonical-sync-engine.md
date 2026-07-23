---
id: 20260723-canonical-sync-engine
title: Canonical Sync Engine
status: active
created: 2026-07-23
updated: 2026-07-24
branch: codex/canonical-sync-engine
pr:
supersedes: []
superseded_by:
---

# Canonical Sync Engine

## Summary

- Delivery status: `signed_candidate_local_gate_complete`.
- The workstream is consolidating personal sync ownership in `Joey-Tools/codex-personal-sync` and hardening mirror generation, scheduler observability, active-skill auditing, reconciliation, and release retention.
- Signed commit `e2459178d4d85509dbf3b20fecf2e727e8eb02cb` is the fixed
  parent of the follow-up candidate containing this journal. The candidate is
  fully locally gated; no push, consumer generation, PR mutation, or external
  deployment was performed.

## Scope

- Establish a one-way canonical source lock and explicit `toolbox` / `private` mirror generation boundary.
- Keep release trees immutable after publication, bind `current` and managed-link transitions to durable evidence, and make `removed_links` an exact migration proof rather than a broad deletion authority.
- Run schedulers through the stable installed runner, preserve audited interval configuration, and publish a bounded runtime status contract.
- Audit the active skill discovery root read-only.
- Prune only exact, unreferenced release directory objects through quarantine with durable recovery and clear evidence.
- Document the resulting contracts in [ARCHITECTURE.md](../../../ARCHITECTURE.md).

## Current State

- The signed `e2459178d4d85509dbf3b20fecf2e727e8eb02cb` candidate has
  been superseded by the follow-up candidate containing this journal.
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
- Private Git object-store bytes are copied and fully verified once during
  materialization. Per-command revalidation retains config/ref/index/control
  identity checks and no-fetch gates without rescanning pack contents.
- Scheduler config exchange preserves an identity-bound hard link to the exact
  original before publication. A replaced displaced pathname is never
  exchanged into the live plist/unit; trusted staged bytes remain live and the
  original recovery object is retained.
- Canonical-master toolbox automation, tests, and its least-privilege
  `CODEX_TOOLBOX_SYNC_TOKEN` interface are documented. No credential currently
  exists and none was invented or installed. Generated files are staged with
  filter-free Git plumbing, deletion is explicit, and mirror parity is checked
  again after the generated commit.

## Validation Evidence

- `python3 -B -m unittest discover -s tests -q -b`: 625 tests passed in
  544.789 seconds.
- `python3 -B -m unittest -q -b tests/test_source_lock.py`: 70 tests passed in
  383.091 seconds after the final lock refresh.
- The source-lock suite includes a real pack larger than 20 MiB; generate and
  check both pass with a 384 MiB operation cap and consume less than 256 MiB.
- Focused suites passed on the frozen implementation: scheduler/doctor 34 and
  toolbox automation 9. The prior candidate's reconciliation safety 305,
  release retention 26, and main engine behavior 181 tests are covered again
  by the 625-test whole-repository run.
- `python3 -B scripts/sync_canonical_mirrors.py refresh-lock` refreshed six
  canonical sources; the final `refresh-lock --check` passed. The final
  `sync-source-lock.json` SHA-256 is
  `7f30db0897aecb4877089c78a975327d52c0c1136bf960d00b9b6e8c4fe5b460`.
- `PYTHONPYCACHEPREFIX=<task-scoped-temp> python3 -B -m compileall -q scripts tests`
  passed. Eight temporary `.pyc` files were removed with the temporary
  directory; the repository contains no `__pycache__`, `.pyc`, or `.pyo`.
- Ruff 0.13.2 lint passed for `scripts` and `tests`; the generator,
  source-lock tests, and toolbox automation tests also pass Ruff format check.
  actionlint 1.7.12 passed both workflows, and `git diff --check` passed.

## Downstream Dependencies

- `Joey-Tools/codex-toolbox` consumes only files declared by the `toolbox` mirror in `sync-source-lock.json`.
- `Joey-Tools/codex-private-workflows` consumes only files declared by the `private` mirror in `sync-source-lock.json`.
- After canonical bytes and mode are finalized, the lock must be refreshed, both generated mirrors must be updated and checked, and consumer validation must run there. Consumer copies must not become alternate sources.

## Next Steps

- Generate and validate the declared downstream mirrors.
- Review the signed follow-up candidate containing this journal against parent
  `e2459178d4d85509dbf3b20fecf2e727e8eb02cb`.
- Push/open the canonical PR and continue downstream mirror/PR delivery only
  when the parent workstream authorizes those remote mutations.
- Provision `CODEX_TOOLBOX_SYNC_TOKEN` separately only if the repository owner
  wants the sync-PR workflow to become operational.
