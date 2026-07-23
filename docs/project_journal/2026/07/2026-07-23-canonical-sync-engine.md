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

- Delivery status: `canonical_mirror_recovery_gate_complete`.
- The workstream is consolidating personal sync ownership in `Joey-Tools/codex-personal-sync` and hardening mirror generation, scheduler observability, active-skill auditing, reconciliation, and release retention.
- Signed commit `39050ba7629ae0ee896f1df5e9e9c9dd75421825` is the fixed
  parent of the current recovery-hardening candidate. The candidate is fully
  locally gated; no push, consumer generation, PR mutation, or external
  deployment was performed.

## Scope

- Establish a one-way canonical source lock and explicit `toolbox` / `private` mirror generation boundary.
- Keep release trees immutable after publication, bind `current` and managed-link transitions to durable evidence, and make `removed_links` an exact migration proof rather than a broad deletion authority.
- Run schedulers through the stable installed runner, preserve audited interval configuration, and publish a bounded runtime status contract.
- Audit the active skill discovery root read-only.
- Prune only exact, unreferenced release directory objects through quarantine with durable recovery and clear evidence.
- Document the resulting contracts in [ARCHITECTURE.md](../../../ARCHITECTURE.md).

## Current State

- The signed `39050ba7629ae0ee896f1df5e9e9c9dd75421825` candidate has
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
- Private Git object-store bytes are copied and fully hashed during
  materialization. Per-command revalidation now binds the complete
  pack/idx/loose manifest and rejects any invalidated content-stability signal
  before or after Git without repeatedly rereading stable pack bytes.
- A prior generated receipt can authorize only exact, clean retired paths:
  receipt digests plus target `HEAD`, stage-0 index, worktree bytes/mode, and
  recovery state must agree. Rename/removal uses desired-absent group
  transactions and recoverable per-target removal journals. Toolbox automation
  consumes the read-only `managed-paths` result so those exact deletions can be
  staged without widening the PR scope.
- Scheduler config exchange preserves an identity-bound hard link to the exact
  original before publication. A replaced displaced pathname is never
  exchanged into the live plist/unit; trusted staged bytes remain live and the
  original recovery object is retained.
- Canonical-master toolbox automation, tests, and its least-privilege
  `CODEX_TOOLBOX_SYNC_TOKEN` interface are documented. No credential currently
  exists and none was invented or installed. Generated files are staged with
  filter-free Git plumbing, deletion is explicit, and mirror parity is checked
  again after the generated commit.
- Scheduler runtime timestamps use a bounded canonical UTC representation,
  reject impossible or far-future values, and recover only timestamp
  corruption under the state lock without accepting unsafe structure or mode.
- Historical receipts now derive retirement authority only from a locally
  available canonical ancestor whose source lock, committed source bytes, and
  mirror mapping reconstruct the receipt byte-for-byte. Recovery journals
  cannot expand the exact current-mapping plus trusted-prior-receipt path set.
- File retirement and internal owner-record cleanup no longer finish with a
  compare-then-pathname unlink. They move the exact isolated object into a
  pre-bound durable mode-0700 quarantine, revalidate identity, content, and
  access policy, and fail before isolation on a cross-filesystem destination.
  Private Git owner records use a quarantine beside the private-control parent
  so repositories on separate mounts remain supported and the active tool root
  stays bounded.
- Mirror retirement now rejects duplicate JSON object keys recursively across
  source locks, receipts, journals, and recovery records. Version-2 removal
  recovery holds the exact quarantine lock while it revalidates evidence
  immediately before and after the active-journal move, so concurrent evidence
  replacement cannot be misclassified as a completed cleanup. A no-op
  generation does not create quarantine churn, and exact-capacity recovery
  keeps the active journal as the authoritative blocker instead of moving it
  into an already-full recovery namespace.

## Validation Evidence

- `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -q -b
  tests/test_source_lock.py`: 99 tests passed in 1057.621 seconds after the
  final format-only test update.
- The disjoint remaining repository suites
  (`test_codex_personal_sync`, reconciliation safety, release retention,
  scheduler/doctor, and toolbox automation) passed 561 tests in 148.644
  seconds. Together with the source-lock suite, the repository gate covers 660
  tests.
- The source-lock suite includes a real pack larger than 20 MiB; generate and
  check both pass with a 384 MiB operation cap and consume less than 256 MiB.
- Scheduler/doctor, toolbox automation, reconciliation safety, release
  retention, and main engine behavior are covered by the 561-test partition.
- `python3 scripts/sync_canonical_mirrors.py refresh-lock` refreshed six
  canonical sources after the final source changes. The final
  `sync-source-lock.json` SHA-256 is
  `678b5eaeef2a12280c062f657dda9d23b04bcc77bc11608b5a8edacea96ab131`.
  This follow-up changes only the generator, its focused tests, and
  documentation, so no locked canonical source digest changed.
- `PYTHONPYCACHEPREFIX=<task-scoped-temp> python3 -B -m compileall -q scripts tests`
  passed. The temporary compile root was removed; the repository contains no
  `__pycache__`, `.pyc`, or `.pyo`.
- Ruff lint passed for `scripts` and `tests`; the generator,
  source-lock tests, and toolbox automation tests also pass Ruff format check.
  actionlint passed both workflows, and `git diff --check` passed.
- Earlier read-only static audits found the separate-mount owner-record issue
  and subsequent recovery-evidence races; the current candidate closes those
  findings. This is pre-commit design evidence, not the required post-commit
  named review.

## Installed Host Baseline

The following evidence was collected read-only before any scheduler update or
reinstallation:

- Local macOS has an hourly LaunchAgent using the stable installed runner and
  the expected public/private repositories. Its current public/private pointers
  are `ed048355...` / `9257aca1...`, but its latest exit is `1` because
  CPython 3.13/3.14 bytecode caches were written into the current private
  review-runtime release. The legacy runtime correctly preserves that
  mismatched immutable tree instead of overwriting it.
- `BL-mac-mini-m4-hoteng` has the same pointers and runtime digest, but no
  personal-sync LaunchAgent is installed.
- `miku-bot-dev` has an enabled hourly user timer, the same pointers, and a
  successful run at `2026-07-23T22:31:19Z`.
- `hoteng-srv-01` and `codex-hoteng-srv-01` each retain an enabled hourly user
  timer and the same public runtime digest, but their timer managers report no
  next monotonic trigger and their latest observed runs are from 2026-06-05 and
  2026-06-19. Their current pointers are `ed048355...` / `23575934...`.
- All five public installed runners have SHA-256
  `09ae830e4391092bccf251de2535dd07247fe2fc7329f902e230d7e2162bad85`.
  Current private overlay releases do not contain a second synchronizer copy,
  so public/private runtime parity remains a downstream generated-mirror and
  release acceptance requirement.

Do not repair these baselines by deleting cache files, changing timer
intervals, or reinstalling the legacy runtime. After the canonical, toolbox,
and private releases are trusted, use the new `status-scheduler` and `doctor`
contracts to repair only the proved gaps while preserving each existing hourly
configuration.

## Downstream Dependencies

- `Joey-Tools/codex-toolbox` consumes only files declared by the `toolbox` mirror in `sync-source-lock.json`.
- `Joey-Tools/codex-private-workflows` consumes only files declared by the `private` mirror in `sync-source-lock.json`.
- After canonical bytes and mode are finalized, the lock must be refreshed, both generated mirrors must be updated and checked, and consumer validation must run there. Consumer copies must not become alternate sources.

## Next Steps

- Generate and validate the declared downstream mirrors.
- Create a signed checkpoint, then review the exact candidate range against
  parent `39050ba7629ae0ee896f1df5e9e9c9dd75421825`.
- Push/open the canonical PR and continue downstream mirror/PR delivery only
  when the parent workstream authorizes those remote mutations.
- Provision `CODEX_TOOLBOX_SYNC_TOKEN` separately only if the repository owner
  wants the sync-PR workflow to become operational.
