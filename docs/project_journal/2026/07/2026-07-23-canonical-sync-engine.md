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

- Delivery status: `canonical_mirror_pr_terminal_evidence_gate_complete`.
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
- Fresh-review Git control-plane findings are closed locally. After binding the
  fixed executable and source marker/admin/common/object directories, mirror
  generation requires one bounded Git 2.45+ `--no-lazy-fetch` capability
  probe. Before any repository/object Git command, the parent-private snapshot
  rejects `.promisor`/alternate markers and parses only its own config files
  with `--file --no-includes`, rejecting include, partial-clone, and promisor
  keys by presence. Every later Git argv carries `--no-lazy-fetch`.
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
- Toolbox automation now binds the exact prepared remote branch record and
  fetched tracking SHA, revalidates base/branch state immediately before an
  ordinary push, and treats an exact already-published desired SHA as an
  idempotent no-op. `pr_has_changes` is independent from
  `branch_needs_update`, so stale-to-clean reconciliation still advances the
  branch; an exact owned open PR is then revalidated against its lifecycle,
  base/head, head OID, owner, repository shape, and marker before it is closed
  without deleting the branch.
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

- A final orchestration audit found that target-base OID revalidation covered
  push but not the no-push pull-request create/edit/close paths. The workflow
  now binds `baseRefOid` in owned-PR evidence, queries the exact live target
  ref immediately before each PR mutation, and revalidates the base plus owned
  PR after create/edit. Focused toolbox workflow tests add PR-base and live-ref
  drift cases; `python3 -m unittest tests.test_sync_toolbox_automation` passed
  14 tests, and actionlint plus Ruff passed for the follow-up.
- Fresh named-single review then found that the no-existing-PR path could
  create from a sync branch moved after the push step and only reject it after
  the wrong PR already existed. Publication now revalidates the exact live
  sync-branch OID immediately before every PR mutation and after create/edit,
  and binds a newly created PR to the exact number returned by `gh pr create`.
  A failed post-create condition closes and verifies only that exact,
  identity-revalidated automation PR; ambiguous cleanup retains its exact
  locator. The execution-level matrix covers pre-create and post-create head
  drift, successful compensating closure, and a mismatched returned PR number;
  the focused suite now passes 15 tests.
- The next fresh named-single pass found no implementation defect and requested
  stronger executable coverage. The harness now covers existing-PR edit
  success plus ownership/head/base drift before and after edit; create base
  drift before and after publication; and compensating-close refusal for
  identity drift, close failure, an unclosed result, and post-close identity
  drift. The focused workflow suite now passes 16 tests, with actionlint and
  Ruff still clean.
- A subsequent fresh named-single pass found that ordinary clean-PR closure
  trusted the `gh pr close` return code without proving the terminal state and
  that its executable matrix did not independently protect every automation
  identity predicate. Ordinary closure now re-reads the exact PR number and
  requires the expected closed lifecycle, base/head names and OIDs, same-repo
  shape, owner, and marker; failed commands, unreadable evidence, reopen/no-op,
  or identity drift retain the exact PR locator for manual recovery. Dynamic
  before/after matrices now exercise marker, lifecycle, base/head names,
  same-repository shape, and owner for create recovery, edit, and close. The
  focused suite remains 16 tests and passes with actionlint, Ruff lint/format,
  and `git diff --check`.
- The next fresh named-single pass found that a server-side PR creation could
  outlive a failing or unrecognized `gh pr create` response and that number
  plus endpoint-OID predicates were not each exercised independently. Create
  now captures command status and output without `set -e` escape, queries the
  bounded exact base/head scope after every abnormal response, records only
  fully identity-matching candidate locators, and never closes a candidate
  whose request lineage was not proved. Compensating closure now binds
  base/head OIDs before and after close. Dynamic create/edit/close matrices
  independently mutate number, marker, lifecycle, base/head names and OIDs,
  same-repository shape, and owner; response-failure tests prove the exact
  candidate is reported but left unchanged. The focused 16-test suite,
  actionlint, Ruff lint/format, and `git diff --check` pass.
- The following fresh named-single pass found that nonzero close responses
  skipped terminal inspection, compensating-close tests covered most
  predicates only before closure, and recovery-candidate parsing accepted
  fractional JSON numbers. Both ordinary and compensating closure now capture
  the CLI status but always query and validate exact terminal evidence; a
  nonzero response can succeed only when the fully bound closed state is
  proved. The compensating matrix mutates every identity predicate separately
  before and after close, and candidate numbers must be canonical positive
  integers. Fractional, string, null, zero, and negative candidate numbers
  retain the broad base/head recovery scope instead of producing a false PR
  locator. The focused 16-test suite, actionlint, Ruff lint/format, and
  `git diff --check` pass.
- `python3 -B -m unittest tests.test_sync_toolbox_automation
  tests.test_source_lock`: 119 tests passed in 956.398 seconds after the
  fresh-review fixes. The added matrix covers unsupported/old/malformed Git
  capability output, bounded probe failures, all partial/promisor config
  values by key presence, case-insensitive promisor and alternate markers,
  helper non-execution, failed-snapshot cleanup/retry, strict remote-ref
  parsing, fetch mismatch, push drift/idempotence/creation, stale-to-clean
  branch advancement, and exact clean-PR closure.
- `python3 scripts/sync_canonical_mirrors.py refresh-lock` and
  `refresh-lock --check` both completed for all six locked sources. The
  generator/workflow follow-up does not change a declared consumer source, so
  `sync-source-lock.json` remains
  `678b5eaeef2a12280c062f657dda9d23b04bcc77bc11608b5a8edacea96ab131`.
- `actionlint .github/workflows/sync-toolbox.yml`, Ruff lint and format check
  for the three changed Python files, and `git diff --check` all passed after
  the final edits.
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
  This follow-up changes the generator, toolbox workflow, their focused tests,
  and documentation, so no locked canonical source digest changed.
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
- A later named-single pass found that toolbox automation derived its complete
  managed-path allowlist only from `master`, so a second unmerged canonical
  rename/removal could reject the prior generated branch path. Preparation now
  validates `managed-paths` independently on the exact target base and exact
  fetched branch receipt, then admits only their sorted union. A real
  bare-remote fixture exercises `one -> two -> three -> removed` without
  merging `master` and still rejects an unrelated committed branch path.
- The same follow-up replaces the final unguarded sync-branch push with an
  exact ref lease. Existing branches require proof that the desired commit
  descends from the prepared SHA and use
  `--force-with-lease=<ref>:<prepared-sha>`; first publication uses the exact
  absent-ref lease, and the refspec binds the exact desired OID rather than
  moving `HEAD`. Focused race fixtures cover deletion, rollback,
  appearance, movement to the desired SHA, malformed duplicate ref evidence,
  and non-descendant heads.
- The final source-lock suite passed 105 tests in 966.119 seconds. The complete
  workflow suite passed 17 tests in 235.479 seconds before the final
  exact-desired-OID refspec tightening; after that tightening, the static and
  lease-race tests passed 2 tests in 5.164 seconds, while the unchanged
  rename/removal fixture had already passed after formatting in 210.925
  seconds. One additional full-suite run under concurrent source-lock load hit
  only the fixture's former 30-second subprocess timeout, so the fixture now
  retains explicit 90-second generator and 120-second prepare ceilings.
- A subsequent fresh single review found three remaining publication-boundary
  gaps. Linked-worktree setup now rejects any root-level `commondir`
  case/canonical collision in the shared common directory before copying,
  binds the same absence in the private Git snapshot before its first Git
  process, and revalidates both descriptors through completion. Real linked
  worktree fixtures cover exact and case-aliased escape markers plus later
  common/private marker appearance.
- Linux scheduler audits now bind service/timer drop-in absence or an exact
  empty-directory identity before the no-unit early return, through pair
  publication, daemon reload, separate enable, and start phases. Foreign
  drop-in residue is never inferred to be tool-owned or deleted without an
  ownership receipt; uninstall preserves and reports it, status/doctor reports
  it even when both unit files are absent, and reinstall remains blocked until
  the user-owned residue is resolved.
- Scheduler runtime state now carries the complete
  `ManagedStateFileSnapshot` from read through conditional publication.
  Existing-file CAS includes parent-directory identity while ignoring
  timestamp-only churn. Publication never exchanges an unproved displaced
  temporary object back into the live status path; it leaves trusted staged
  bytes live and retains descriptor-bound original recovery evidence plus the
  untrusted displaced locator. Tests cover identity replacement, same-inode
  content/access drift, whole-parent rotation with the same hard-linked file,
  absent-to-appeared races, late exchange replacement/swap, and benign
  mtime-only transitions while retaining monotonic attempt behavior.
- The next fresh named-single pass found four remaining binding gaps. Linux
  activation now binds the published service/timer objects, bytes, access
  policy, and parent identity, then revalidates the pair after daemon reload,
  before enable, before start, and after start; injected main-unit drift stops
  before the next native action. Linked-worktree `commondir` binding now
  retains the entire NFC+casefold collision set from initial bind through
  private materialization, every Git boundary, and terminal validation.
  Case-sensitive alias/add-and-rename fixtures plus simulated
  case-insensitive/NFC inventories exercise the retained set.
- A later fresh named-single pass found that runtime publication could expose
  staged success after an ambiguous CAS and that systemd pair validation did
  not retain both unit bindings across an interleaved read. Runtime writes now
  create a durable fixed-name blocking marker before any live mutation; exact
  marker unlink is the final commit linearization point after all live,
  preimage-cleanup, and parent validation. Readers check the marker plus
  bounded status-transaction residue before and after their status snapshot,
  so even consecutive marker-rebuild failures remain fail closed without a
  second exchange. Recovery binding now covers name, descriptor, parent,
  identity, bytes, and access policy; link preservation begins immediately
  after `link(2)`, and cleanup removes `.original` while the displaced
  preimage remains independently durable. Linux activation carries the exact
  writer-returned service/timer snapshots and revalidates both descriptors,
  both canonical names, and their shared parent after both unit reads.
- The closing fresh single review found one portable-name gap in the runtime
  reader. It now directly stats the fixed publication marker through the bound
  parent descriptor and compares scanned marker/transaction/retained evidence
  with NFC+casefold keys. Case-only and NFC/NFD aliases block reads, while
  multiple equivalent spellings in the protected namespace are classified as
  ambiguous and fail closed. This remains a cooperative same-UID design:
  exact descriptor/path revalidation detects observed replacements, but
  portable `stat` followed by `unlink` is not an inode-conditional deletion
  primitive and does not claim isolation from a malicious same-UID writer with
  parent-directory access.
- The superseding final validation passed 108 source-lock tests in 871.124
  seconds and 576 complete
  personal-sync/scheduler/reconciliation/retention tests in 130.934 seconds,
  for 684 disjoint repository tests. The scheduler module independently passed
  57 tests in 1.293 seconds and the engine module passed 188 tests in 28.348
  seconds; the four new portable-name fixtures also passed in 0.039 seconds
  before the full partitions. Ruff lint and changed-range formatting, isolated
  compileall, project-journal validation, source-lock check, and
  `git diff --check` passed; no bytecode, formatter backup, or reject artifact
  remains. The refreshed source-lock SHA-256 is
  `f6037083cc2b20df71a754103f8603a9a85ce162248111ef471e40f2cd5974a7`.

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
