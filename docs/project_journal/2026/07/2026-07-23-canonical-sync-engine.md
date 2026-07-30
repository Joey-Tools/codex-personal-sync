---
id: 20260723-canonical-sync-engine
title: Canonical Sync Engine
status: active
created: 2026-07-23
updated: 2026-07-30
branch: codex/canonical-sync-engine
pr:
supersedes: []
superseded_by:
---

# Canonical Sync Engine

## Summary

- Delivery status: `delivery_gate_blocked`.
- The workstream is consolidating personal sync ownership in `Joey-Tools/codex-personal-sync` and hardening mirror generation, scheduler observability, active-skill auditing, reconciliation, and release retention.
- Signed commit `30ff8cfd36c7aee7b1b5f2e3fa6de14816f7bfaa` is the
  formal-review head and fixed parent of the current follow-up. The code
  candidate is locally gated in an isolated private-control root, while
  production admission remains blocked by the retained host quarantine at its
  exact capacity. No push, consumer generation, PR mutation, or external
  deployment was performed.

## Scope

- Establish a one-way canonical source lock and explicit canonical → toolbox →
  private propagation boundary, with toolbox as the only direct generated
  mirror.
- Keep release trees immutable after publication, bind `current` and managed-link transitions to durable evidence, and make `removed_links` an exact migration proof rather than a broad deletion authority.
- Run schedulers through the stable installed runner, preserve audited interval configuration, and publish a bounded runtime status contract.
- Audit the active skill discovery root read-only.
- Prune only exact, unreferenced release directory objects through quarantine with durable recovery and clear evidence.
- Document the resulting contracts in [ARCHITECTURE.md](../../../ARCHITECTURE.md).

## Current State

- The signed `30ff8cfd36c7aee7b1b5f2e3fa6de14816f7bfaa` candidate has
  been superseded locally by the follow-up candidate containing this journal.
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
- macOS launchd installation now retains the writer-returned plist snapshot,
  parent fd, and plist fd through legacy cleanup and every native activation
  action. Each action is bracketed by exact path/parent/object/content/access
  revalidation; missing, unreadable, replacement, content, and access-policy
  failures remain distinct and suppress the success report, while mtime-only
  churn remains benign.
- Mirror generation now inventories the complete raw stage-0 index and
  recursive `HEAD` tree before managed-ancestor configuration. Exact
  path/mode/object parity, strict UTF-8 raw-path parsing, entry/byte/time caps,
  and a consumer-wide NFC+casefold trie prevent new managed/recovery paths from
  aliasing or enclosing non-managed tracked paths. Exact clean managed targets
  remain valid, and the complete namespace snapshot is revalidated before
  publication boundaries.
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
  generation does not create target transaction or target-sibling quarantine
  churn, and exact-capacity recovery keeps the active journal as the
  authoritative blocker instead of moving it into an already-full recovery
  namespace. Private Git owner cleanup still retains one transient evidence
  record for every operation that reaches Git binding.
- The final scheduler-uninstall follow-up prebinds the current macOS plist and
  every legacy plist before the first current `launchctl` action. Native
  actions and conditional removals revalidate every live binding before and
  after their mutation boundary, so a legacy appearance or replacement is
  retained and reported. A definitively missing macOS/Linux scheduler config
  parent is now a pre-lock idempotent no-op, including `--no-disable`, while
  unreadable, symlink, and non-directory states remain fail-closed.
- The third scheduler follow-up extends that same binding set across macOS
  install: current and every legacy plist are prebound before cleanup and
  retained through current bootout/bootstrap/enable. Linux uninstall now
  brackets post-removal daemon reload with both unit absence bindings.
  Missing-parent classification walks from a bound user-home descriptor with
  no-follow component opens, so intermediate symlinks and uncertain identity
  fail closed while only a confirmed missing component remains an idempotent
  no-op.
- The fourth scheduler follow-up gives uninstall a durable incomplete-state
  marker that survives every unclassified native failure and remains visible
  to status/doctor even after unit removal. Only exact bounded
  already-absent/not-loaded responses are benign; timeout, permission, bus,
  unknown, and reload failures fail closed. Linux status now requires both
  enabled and active timer evidence. Pair recovery holds one unit-parent
  descriptor and treats marker/service/timer as one object group, including
  two complete byte/access/name passes plus final identity and parent
  revalidation before the marker commit.
- The fifth scheduler follow-up moves pair-recovery and uninstall parent fsync
  before their terminal validation boundary. Pair recovery revalidates present
  and absent marker/service/timer members as one group; uninstall routes every
  marker/config canonical-name lookup through one retained config-parent FD.
  Both paths run repeated complete group passes plus a final parent check
  immediately before exact marker unlink, so fsync-time and later-member
  interleavings retain the marker. Linux status treats only exact persistent
  `enabled` plus exact `active` as healthy; `enabled-runtime` is explicit
  runtime-only drift even while active.
- The final workflow-trust follow-up pins both canonical and target checkout
  steps to GitHub-verified `actions/checkout` `v4.4.0` commit
  `11d5960a326750d5838078e36cf38b85af677262`. The token-bearing workflow has no
  other third-party action use. Its security test now parses a strict YAML
  structure, traverses every job-level and step-level `uses`, handles flow
  mappings and quoted-key alternatives, and rejects aliases, merge keys,
  duplicates, or malformed structures that could otherwise hide a movable
  external ref.
- Linux scheduler `ExecStart=` serialization now preserves exact runtime argv
  rather than borrowing shell syntax. Literal `$` and `%` are emitted as
  `$$` and `%%`; spaces, quotes, and backslashes use canonical systemd
  double-quoted escapes. The semantic reader decodes only that representation
  before comparing runner/home/repository arguments, while raw expansion
  syntax, control characters, and non-UTF-8 paths fail closed before an install
  transaction starts.
- Fresh-review follow-up hardening now runs one bounded strict full Git `fsck`
  against the parent-private snapshot before any ordinary object query, makes
  scheduled installs revalidate their attempt CAS under the installation lock
  before mutation, and supervises every GitHub metadata/download child under
  one shared monotonic deadline with bounded streams and verified process-group
  cleanup. The final bytes passed the independent 123-test source-lock suite,
  the complete 780-test native suite, and the complete 780-test Python 3.9.6
  suite. Native and Python 3.9 compileall, Ruff, source-lock verification, and
  `git diff --check` passed; the refreshed source-lock SHA-256 is
  `cb876ccf5a761b09eebaf77559cf0a01c6bcffa8183a81b2b7537bd1bd8f8d6c`.
- The PR #5 Ubuntu source-lock follow-up removes the fixed
  `/usr/bin/python3` launcher assumption and does not re-execute the current
  interpreter. Immediately before each direct `/usr/bin/git` spawn, the
  single-threaded CLI enters the already-bound repository or private-snapshot
  directory through its retained descriptor; the child inherits that exact
  directory object, and the parent restores its prior descriptor-bound
  working directory. The protected launch properties are directory object
  identity and access policy. Replacing the directory pathname at the
  `Popen` boundary cannot redirect the child, while later path revalidation
  still rejects the transaction.
- The combined provider-review follow-up resolves all four reported findings.
  Git is spawned directly from the exact descriptor-bound repository or
  private-snapshot directory without re-executing a Python pathname. Quoted
  launchctl not-loaded output is accepted only when its current/legacy label
  preserves exact case and its `gui/<uid>` target exactly matches the command.
  macOS scheduler tests mock the fixed native-argv resolver before any host
  `/bin/launchctl` inspection, so the same harness remains runnable on Ubuntu.
- Linux activation retains the writer-returned service/timer objects and
  parents, reopens the exact objects read-only, and holds read leases through
  activation. The protected properties are canonical-name object identity,
  exact bytes, access policy, and audited drop-in state. Full
  user-home-to-unit ancestry plus existing empty drop-in directories remain
  descriptor-bound. Their ctime values are command-interval generation
  evidence only: a delta is not classified as mutation, but makes that reload
  interval inconclusive and requires a corrective `daemon-reload`; exact
  missing, mismatch, unreadable, access-policy, or lease-break evidence fails
  closed. Activation proceeds only after one stable interval, with three
  bounded attempts.
- `systemctl enable` no longer reparses a replaceable unit. The installer
  conditionally publishes the one exact `timers.target.wants` symlink, retains
  its identity evidence through `start`, and revalidates it immediately before
  the incomplete-state marker is committed. New-publish and already-matching
  tests deterministically replace a unit, let the first reload consume foreign
  bytes, restore the original, and prove a second stable reload occurs before
  start. Persistent interval instability exhausts the bounded retries and
  retains the marker. The scheduler writer also closes its duplicated stream
  descriptor when `fdopen()` construction fails.
- This Linux interval proof assumes a local ctime-coherent filesystem and
  cooperative same-UID access. It does not claim protection from mount-capable
  actors, remote filesystems with weaker metadata semantics, or a malicious
  same-UID writer that controls the parent namespace outside the observed
  interval. Exact state mismatch remains distinct from an inconclusive
  generation interval.

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
- The final-review follow-up adds 2 scheduler test methods with 30 adversarial
  native-boundary subcases plus an mtime-only success path, and 11 source-lock
  tests for case/NFC/NFD aliases, tracked ancestor/descendant collisions,
  exact managed targets, unmerged stages, full index/HEAD disagreement,
  invalid raw path bytes, capacity exhaustion, non-managed index drift, and
  target `HEAD` drift.
  Focused runs passed all added cases. An initial full source-lock run exposed
  one diagnostic-order regression for an exact tracked symlink ancestor; the
  guard continued to fail closed, but its error no longer matched the existing
  contract. The fix restores the precise symlink-ancestor diagnostic without
  weakening case/NFC portability rejection. The superseding final gate passed
  595 personal-sync, reconciliation, retention, scheduler, and automation tests
  in 390.258 seconds plus 119 source-lock tests in 1427.115 seconds, for 714
  disjoint repository tests. The refreshed source-lock SHA-256 is
  `4dd4ab9e9fa6751f3a83388e791937a10a8478e92a9913086a4ff4c9347463ba`.
- The closing review follow-up adds complete branch-exclusive validation for
  the generated toolbox branch. Every commit-parent edge, including merge and
  octopus side history, is checked without rename collapsing against the
  independently proven managed-path union. The receipt binds topology,
  introduced blobs, budgets, base/head, and the allowlist; transient
  out-of-scope paths and high-confidence secret blobs fail closed. Each run
  then republishes only an exact-base fresh single commit through an exact old
  branch lease. Legacy launchd cleanup now retains and revalidates each legacy
  plist through every native boundary and uses no-replace isolation before
  deleting only the proved original. `status-scheduler` and `doctor` retain the
  audited macOS plist or Linux service/timer pair through the native daemon
  query and preserve distinct config-drift, daemon-unavailable,
  daemon-disabled, and stable-runner diagnostics.
- The aggregate personal-sync, reconciliation, retention, scheduler, and
  automation gate passed 604 tests in 539.402 seconds; the toolbox automation
  module independently passed 22 tests in 360.568 seconds. After a final
  formatting-only blank-line correction and source-lock refresh, the exact
  final source bytes passed all 119 source-lock tests in 1355.707 seconds and
  all 63 scheduler tests in 2.922 seconds. Python compilation, Ruff lint,
  `actionlint`, source-lock verification, and `git diff --check` also passed.
  The refreshed source-lock SHA-256 is
  `a068a413a0f083fe3a3ab572d5b62fb3e05392aa7c87f25bdf6011a3219f3d38`.
- The second fresh-review follow-up binds the installed macOS plist or Linux
  service/timer pair before the first uninstall native action, revalidates
  exact identity, bytes, ownership, mode, and parent access through every
  boundary, and conditionally removes only the proved original. Initially
  absent legacy plists retain parent-and-absence evidence, so later appearance,
  parent replacement, or unreadable evidence fails closed without deleting a
  new object. Daemon queries now return explicit `enabled`, `disabled`, or
  `unavailable` classifications; status and doctor retain daemon evidence
  alongside earlier runtime or config failures. Branch-exclusive secret
  admission now also rejects transient ENCRYPTED, DSA, and PGP private-key
  headers across add/delete, rename/back, and merge-side history.
- The exact final source bytes passed 609 personal-sync, reconciliation,
  retention, scheduler, and automation tests in 587.219 seconds plus all 119
  source-lock tests in 1545.549 seconds, for 728 disjoint repository tests.
  The engine module independently passed 188 tests in 42.448 seconds, the
  scheduler module passed 67 tests in 5.832 seconds, and the nine new
  transient-history subcases passed in 60.471 seconds. The refreshed
  source-lock SHA-256 is
  `10b8caee52a83c71087b6be971812523feab00358688640e6da8334e3c9de15b`.
- The final scheduler-uninstall findings follow-up adds five adversarial test
  methods covering current-action legacy appearance/replacement, removal-time
  reappearance from both initially absent and removed-present states, explicit
  missing-parent no-op for macOS/Linux with and without daemon disable,
  concurrent appearance after the absence observation, and fail-closed
  unreadable/symlink/non-directory parents. The exact final bytes passed 614
  personal-sync, reconciliation, retention, scheduler, and automation tests in
  1196.462 seconds plus all 119 source-lock tests in 2557.828 seconds, for 733
  disjoint repository tests. The scheduler module independently passed 72
  tests in 18.833 seconds; the final source-lock SHA-256 is
  `95e4541724e56b239cea923815ef2c5ccc605ac6f86fb2cf2c0625662fddb505`.
- The third scheduler findings follow-up adds five focused test methods. A
  two-legacy fixture proves every legacy plist is prebound before the first
  install cleanup action; current-action fixtures prove deleted or initially
  absent legacy paths stay bound through current activation. Linux fixtures
  exercise unit reappearance immediately before and during daemon reload.
  Missing-parent fixtures cover confirmed absence, concurrent appearance,
  final and intermediate symlinks (`~/Library` and `~/.config/systemd`),
  unreadable/non-directory components, and opened-component identity
  replacement. The exact final bytes passed 619 personal-sync,
  reconciliation, retention, scheduler, and automation tests in 906.120
  seconds plus all 119 source-lock tests in 1934.155 seconds, for 738 disjoint
  repository tests. The scheduler module independently passed 77 tests in
  8.744 seconds. Compileall, Ruff, actionlint, project-journal validation,
  source-lock verification, and `git diff --check` passed. The final
  source-lock SHA-256 is
  `4a47eb34ea208dc841b19d6ebcb46a490d8c84b0531c982fb87ef8a0603e7688`.
- The fourth scheduler findings follow-up adds eight scheduler test methods
  covering timeout, permission, and unknown native failures; strict
  already-absent parsing; reload failure recovery; per-unit replacement;
  parent rotation/ABA recovery; interleaved earlier-member replacement;
  partial-binding descriptor cleanup; and enabled-but-not-active status.
  Scheduler/doctor passed 85 tests in 6.095 seconds. The exact final bytes then
  passed 627 personal-sync, reconciliation, retention, scheduler, and
  automation tests in 810.036 seconds plus all 119 source-lock tests in
  2067.999 seconds, for 746 disjoint repository tests. The first aggregate run
  exposed one older temporary-home fixture that did not bind `Path.home()` to
  its simulated user home; that test contract was corrected, its focused test
  passed, and the complete 627-test partition passed on rerun. Initial commit
  output then exposed an unintended executable-mode loss from earlier
  formatting recovery; the script was restored from `100644` to canonical
  `100755`, the source lock was refreshed, and the full 119-test source-lock
  suite above passed again on that final mode. Compileall, Ruff, actionlint,
  project-journal validation, source-lock verification, and `git diff
  --check` passed. The final source-lock SHA-256 is
  `a3155369a24a391740f6cbff1e1d1306b6ab3c31239192b426c924a10cd38c59`.
- The fifth scheduler findings follow-up adds post-fsync whole-group
  revalidation for pair recovery and uninstall, including earlier absent-member
  reappearance during later-member checks, plus explicit runtime-only systemd
  enablement drift. The exact final bytes passed 87 scheduler/doctor tests,
  the complete 748-test repository suite, and the independent 119-test
  source-lock suite. Compileall, Ruff, actionlint, project-journal validation,
  source-lock verification, and `git diff --check` passed. The canonical
  engine remains mode `0755`; its SHA-256 is
  `57eb9e9015c460ee073cfb68067ca66f3abeb4c8ef19861770cef8a3fbc431f5`,
  and the refreshed source-lock SHA-256 is
  `e249c583d6f9f43c7447375b550d842615e8ffb42166f5135fa6ce06f54dc578`.
- The final workflow-trust fix passed all 24 toolbox automation tests in
  369.541 seconds, all 87 scheduler/doctor tests in 5.715 seconds, the complete
  749-test repository suite in 1232.265 seconds, and the independent
  119-test source-lock suite in 957.763 seconds. Compileall, Ruff lint and
  changed-range format check, actionlint, project-journal validation,
  source-lock verification, and `git diff --check` passed. Official upstream
  refs `actions/checkout` `v4` and `v4.4.0` both resolved to
  `11d5960a326750d5838078e36cf38b85af677262`, whose GitHub commit verification
  was valid. No declared canonical source changed, so the source-lock SHA-256
  remains
  `e249c583d6f9f43c7447375b550d842615e8ffb42166f5135fa6ce06f54dc578`.

- The final two P2 follow-ups replace the privileged-workflow line regex with a
  strict structural YAML loader and replace Linux scheduler shell parsing with
  canonical systemd argv serialization/decoding. Eight final focused tests
  passed in 0.021 seconds, all 26 toolbox automation tests passed in 365.414
  seconds, and all 92 scheduler/doctor tests passed in 7.189 seconds. The exact
  final bytes passed the complete 756-test repository suite in 1799.892
  seconds and the independent 119-test source-lock suite in 1289.901 seconds.
  Compileall, Ruff lint and changed-range format checks, actionlint,
  source-lock verification, and `git diff --check` passed. The audited
  `actions/checkout` `v4.4.0` pin remains
  `11d5960a326750d5838078e36cf38b85af677262`. The canonical engine remains mode
  `0755` with SHA-256
  `d0b51c4f2ef65876c1c5675f39038a4c016aa48bacd11d72db8e8c1b88df3d30`;
  scheduler tests have SHA-256
  `1b31f5e3da0bf59cec944762e490aed409ed72882044e27fef54f94bd9f56a0b`,
  and the refreshed source-lock SHA-256 is
  `b1f87a037fb38428db572cbbec2fa756c1e8c84cb66f194e0563f5d5305839d6`.
- The next P2 follow-up reserves every home-root release-retention control
  target, its NFC+casefold portable aliases, and all descendants across active,
  removed, and replacement manifest routes. Internal retention transaction
  paths remain covered by the existing reserved `personal-sync/` namespace.
  Legacy macOS scheduler cleanup now accepts a nonzero `bootout` or `disable`
  only when the action-specific parser proves the service is already absent.
  Timeout, permission, and unknown failures abort before conditional removal or
  current-job activation while the current and legacy config bindings remain
  live.
- The final bytes passed the three focused regressions in 0.083 seconds, all
  119 source-lock tests in 832.095 seconds, and the complete 759-test repository
  suite in 1329.938 seconds. Independent module gates passed 189 engine tests
  in 27.121 seconds, 94 scheduler/doctor tests in 4.889 seconds, and 26 toolbox
  automation tests in 249.926 seconds. Compileall, Ruff lint and changed-range
  format checks, actionlint, source-lock verification, and `git diff --check`
  passed. The canonical engine remains mode `0755`; its SHA-256 is
  `c16f3720ad7845971929f54212f47e8b580ee91614f57049e8b2588b536be2b4`.
  Manifest and scheduler tests have SHA-256
  `6485b3c3e320f5a213bb1eb03c45e0a993a9f779f4029c3435624a67ca5d5ace`
  and
  `523ca00bf23cb103dfcbf698837e230c8dd79b7fd06efaa2a05ceac8ca82aa43`;
  the refreshed source-lock SHA-256 is
  `803a51e187204338e055c1f29801aaee5201c1ab6a675619afb714cee6b7e3a1`.
- The provider-review follow-up's exact final bytes passed the complete
  791-test repository gate under native Python in 591.745 seconds and Python
  3.9.6 in 672.796 seconds. The focused personal-sync plus scheduler/doctor
  partition passed 308 tests in 40.617 and 47.817 seconds; the source-lock
  partition passed its exact final formatted bytes with 126 tests in 332.804
  and 366.116 seconds; and toolbox
  workflow automation passed 26 tests in 140.471 and 146.277 seconds. Each
  macOS run skipped only the Linux read-lease integration fixture. A local
  Linux container attempt could not start because Apple Container has no
  default arm64 kernel configured; host kernel configuration was intentionally
  left unchanged. The refreshed six-source lock verifies with SHA-256
  `30bf5743a368ac2d189caf9825edb1d13f69924639dc0713d30f33dc9ffb6d17`.
- The formal fresh-context Codex single review of
  `6c4878f33f5c82714e988b0470ccc5f4f33c0b70..30ff8cfd36c7aee7b1b5f2e3fa6de14816f7bfaa`
  returned one P1: binding the source Git object did not bind the executable
  image selected by `Popen(pathname)` at the final spawn boundary.
  Claude lane temporarily waived by Joey before 2026-08-01 00:00 Asia/Shanghai
  The lane was not run or counted, so this is not a completed named double or
  triple review.
- The follow-up protects the bytes actually executed. The source Git
  descriptor is identity/access-bound and double-read into a stable SHA-256
  digest, then copied with exclusive creation into the parent-private snapshot,
  fsynced, reopened read-only, and rebound by identity, owner/type/mode,
  access policy, and exact content. Every Git child uses that mode-0500 file as
  `Popen(executable=...)`, with pre/post-spawn revalidation and the existing
  descriptor-bound cwd. A replace/consume/restore race against the original
  source pathname therefore still executes the frozen snapshot. Snapshot
  access-policy drift fails before `Popen`, and an in-place source mutation
  during copy invalidates publication.
- macOS does not provide the required Python fd-exec path, and a byte copy of
  the sealed-system `/usr/bin/git` platform shim is killed by AMFI even though
  its embedded signature verifies. The implementation therefore treats fixed
  `/usr/bin/xcrun` as the platform locator trust root, binds the ordinary
  developer-tool Git it selects, and snapshots that executable instead. The
  real copied Mach-O passed both `--version` and repository `rev-parse`; the
  contract does not claim fd-exec or hostile same-UID namespace exclusion.
- Independent follow-up evidence found the local durable private-control
  quarantine at its exact 10,000-entry cap. Both native Python and
  `/usr/bin/python3` production `refresh-lock --check` attempts stopped before
  moving their active owner records and reported the bounded-capacity blocker;
  the retained evidence was not deleted, moved, or rewritten. The new
  `status-scheduler` / `doctor` mirror-quarantine audit is descriptor-bound,
  read-only, and ordered tool-root → quarantine under nonblocking shared
  leases. It reports exact segment path/name, identity/access policy,
  count/cap, and schema-valid stale recovery owner identities; saturation and
  inconclusive audit states fail strict status. Invalid or private-identity
  mismatched owner records are not mislabeled as durable-capacity recovery.
  Safe segment rollover is explicitly deferred because version-2 exchange
  journals store only a basename; a separate high-risk workstream must add
  receipt-bound version-3 segment locators, legacy migration, and global
  entry/logical/allocated-byte ceilings. Production admission therefore
  remains blocked even when isolated-root verification passes.
- The exact follow-up bytes passed all 806 repository tests under native
  Python in 911.205 seconds and `/usr/bin/python3` in 977.546 seconds; each
  run skipped only the Linux read-lease integration fixture. The final
  scheduler/doctor partition passed all 116 tests in 8.479 and 10.032 seconds,
  and the five lock-order/revalidation regressions passed in 0.020 and 0.044
  seconds. Ruff lint and changed-file format checks, dual-Python compileall,
  actionlint, project-journal validation, isolated-root six-source lock
  verification, and `git diff --check` passed. The source-lock, engine,
  scheduler-test, and generator SHA-256 values are respectively
  `b759cbadb48e016d0864b7e38e67a307dc1d41f5d4402999bada14bb1a811405`,
  `a8dd98e16da8dffb6894aa366fcd6d61f79f4f4ec973d59accadf0dd30336dae`,
  `29e9be4cb6e659e24ed97ab63a52aba4299d0df07a4e90500938f4607dee4dd5`,
  and
  `b9830769d53f3e7ea8b6d3069a24437359309021b119cd5158610f0f2efd1aca`.
  A final read-only host check confirmed the retained durable quarantine is
  still device `16777231`, inode `1362674446`, mode `0700`, uid `501`, gid
  `0`, mtime `1785353196`, with exactly 10,000 direct entries. No production
  refresh was retried and no retained host evidence was deleted, moved, or
  rewritten; the isolated gate is green while production admission remains
  blocked.
- An independent read-only terminal verification of signed head `9912396`
  again passed all 806 repository tests: Python 3.13.0 completed in 998.362
  seconds and Xcode Python 3.9.6 completed in 1106.207 seconds, each with only
  the Linux read-lease integration fixture skipped. The exact Git-executable
  boundary selection passed 9 tests in 8.790 and 9.429 seconds, the quarantine
  saturation/lock-order/revalidation selection passed 13 tests in 64.844 and
  68.446 seconds, and scheduler/doctor passed all 116 tests in 11.293 and
  12.637 seconds. Dual-runtime compilation, Ruff lint, the recorded
  changed-file format gate, actionlint, JSON/schema/source-lock validation,
  isolated-root six-source lock verification, project-journal validation, and
  `git diff --check` passed. The verifier did not run production sync or
  scheduler activation and did not read or modify the retained production
  quarantine.
- The terminal verifier also proved that the narrower recorded format gate
  omitted two changed test modules. Ruff mechanically formatted
  `test_personal_sync_reconciliation_safety.py` and
  `test_release_retention.py`; the resulting full-tree format and lint gates
  pass. Their combined 331 tests passed after formatting under Python 3.13.0
  in 115.367 seconds and Xcode Python 3.9.6 in 135.477 seconds. No production
  state or test fixture semantics changed.
- A fresh whole-range named-single review of signed head `7b5017d` found that
  the two mechanically formatted test files no longer matched their canonical
  source-lock entries. Production `refresh-lock` and `refresh-lock --check`
  each performed only the bounded read-only capacity inventory and then stopped
  at the already-proved 10,000-entry durable-quarantine cap; no retained entry
  payload was opened, deleted, moved, or rewritten, and no bypass was added.
  The exact two SHA-256 entries were updated to the independently measured
  current bytes, producing canonical source-lock SHA-256
  `5c0d635b58a8da462a59ee750a432eb4657815a971cb7f4d4b81855ecce77a79`.
  The repository-current and canonical-serialization checks passed in both
  runtimes, followed by all 133 source-lock tests in 625.609 seconds with
  Python 3.13.0 and 656.929 seconds with Xcode Python 3.9.6. Full-tree Ruff
  lint/format, JSON parsing, and `git diff --check` also passed. Production
  refresh remains intentionally blocked until a separate recovery-authorized
  quarantine workstream resolves the retained evidence.
- A final ownership audit found that the repository lock still modeled
  canonical-to-private generation even though the approved release topology is
  canonical → toolbox → private. The real lock now declares only the toolbox
  mirror; private propagation is explicitly receipt-bound to the exact toolbox
  commit and complete immutable public release. The generic multi-consumer
  parser fixture remains covered, so this governance correction does not
  remove engine support for another declared consumer. The exact follow-up
  bytes passed all 133 source-lock tests in 598.528 seconds with Python 3.13.0
  and 636.144 seconds with Xcode Python 3.9.6. The focused repository-lock and
  documented credential-interface selections passed in both runtimes; JSON
  parsing, targeted Ruff lint/format, and `git diff --check` also passed.

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
- `Joey-Tools/codex-private-workflows` consumes the synchronizer only through an exact receipt-bound toolbox commit and its complete immutable public release; no direct canonical-to-private mirror is permitted.
- After canonical bytes and mode are finalized, the lock must be refreshed, the generated toolbox mirror must be updated and checked, and toolbox validation must run there. Private propagation starts only after that exact toolbox release is complete. Consumer copies must not become alternate sources.

## Next Steps

- Generate and validate the declared toolbox mirror, then bridge its exact
  immutable release into the private overlay.
- Push/open the canonical PR and continue downstream mirror/PR delivery only
  when the parent workstream authorizes those remote mutations.
- Provision `CODEX_TOOLBOX_SYNC_TOKEN` separately only if the repository owner
  wants the sync-PR workflow to become operational.
