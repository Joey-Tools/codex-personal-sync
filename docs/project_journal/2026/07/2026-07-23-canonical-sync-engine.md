---
id: 20260723-canonical-sync-engine
title: Canonical Sync Engine
status: active
created: 2026-07-23
updated: 2026-08-06
branch: codex/canonical-sync-engine
pr:
supersedes: []
superseded_by:
---

# Canonical Sync Engine

## Summary

- Delivery status: `delivery_gate_in_progress`.
- The workstream is consolidating personal sync ownership in `Joey-Tools/codex-personal-sync` and hardening mirror generation, scheduler observability, active-skill auditing, reconciliation, and release retention.
- PR #5 squash-landed as canonical commit
  `6d078594d547598db037ce358c89c8a8ac58c881`, with tree
  `70dc2c727e91036e9d155ec17dbed643eef26990`.
- The PR #6 scheduler-doctor fixture and source-lock substage is complete for
  the target-branch state. Its latest signed pre-squash implementation evidence
  is head `cc7e932676416aa7f0f29eecafdc5a8469a96252`, tree
  `871eac3a1b891cfa2322891d816c83f318621c31`, with sole parent
  `a02fa93ff87ed27c0b7f573cd900f16db1545b31`. That signed merge checkpoint
  joins feature parent `85f66dbc42550a96fd10d5f2857bc9ae19e7a3de` and canonical
  `master` `867be02c2831b343501eac8c9e6ff325fae68369`. The verified
  BL-generated source-lock SHA-256 is
  `cfc050e042dffd5727d8b9904adfaacbebb6c06455a03394b33f4eda1260d765`.
  These pre-squash identities remain historical evidence only. Downstream
  generation must bind the actual squash-landed canonical `P` identity.
- The retained private-control recovery follow-up is most recently reviewed at
  signed checkpoint `10b0ab3285d898cadb6b533335ad16d632a1dd65`, tree
  `1de01af544dcf867808143d9840543121145e2cf`. Its formal prior-b4ca
  fresh-context named single found that recovery document writes lacked a
  zero-progress/deadline-safe write-all primitive and that recursive directory
  terminal revalidation could leak raw pathname/descriptor `OSError` values.
  The symmetric generator/runtime repair uses bounded `memoryview` writes,
  rejects zero progress, and preserves distinct missing, unreadable,
  descriptor-failure, identity, and access-policy classifications. These
  pre-squash identities remain historical evidence only; downstream handoff
  begins from the actual squash-landed canonical `P`.

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

- The private Git control plane now uses an ordered machine-readable registry:
  allocating `primary-home-v1` under the passwd-derived stable account home,
  then non-allocating `legacy-shared-v0` at the platform shared temporary
  parent. The full account-home ancestry is no-follow descriptor-bound and
  policy-checked. Existing primary home/namespace/tool/quarantine objects are
  retained across the legacy gate and the complete snapshot-owner lifecycle;
  a missing fixed object is published from a random bound directory with a
  no-replace rename and is never adopted after its absence receipt. Foreign
  legacy children receive two metadata-only passes and are never opened.
  Same-UID recovery exact-binds and leases the original tool/quarantine roots;
  an initially absent quarantine plus any tool entry is a conservative
  zero-mutation `legacy-recovery-pending` result. Parent identity alone
  deduplicates a whole root. Every distinct-parent child alias is
  inconclusive. Strict one-way parent/child containment, same-filesystem
  disjoint tool/quarantine roots, and overlap gates against Git plus primary
  control objects complete before any stale recovery mutation. Same-UID legacy
  leases transfer into the primary context and remain held through owner-record
  publication; pre-allocation, post-primary-bind, and publication-terminal
  receipt passes cover every registry root, while every exit releases the
  retained fences exactly once. Scheduler status/doctor consume the same
  registry, perform their own aggregate-terminal receipt pass and shared
  topology checks, and never stop coverage at the first result.
- Private owner-record recovery now uses a descriptor-stable 4096+1-byte hard
  producer ceiling. Both reads consume the shared operation deadline and byte
  budget; malformed or oversized records can no longer amplify the generic
  32-MiB source-reader contract before the 4-KiB schema limit is applied.
- The installed engine remains a standalone one-file runtime and the mirror
  generator remains a separate canonical controller. Their necessary dual
  implementations are guarded as one contract by scenario parity over root
  schema/order, platform paths, ancestor cap, shared-parent policy, reason
  codes, ownership classification, and primary-allocation decisions. One-sided
  private-control changes are explicitly unsupported.
- The final Darwin archive-workspace follow-up recognizes only the platform's
  exact `/tmp -> /private/tmp` alias, pins that symlink object through a retained
  descriptor until canonical-directory binding and revalidation complete, and
  then continues through a no-follow binding of the canonical directory. It
  protects alias and target object identity plus access policy while accepting
  benign directory timestamp and child-entry churn; unlink/recreate cannot be
  hidden by immediate inode reuse, and every other leaf symlink remains
  fail-closed. Mirror-control,
  private-object, cleanup, and tool-root inventories now stop at `limit + 1`
  producer entries, retain and sort no more than the declared limit, close the
  iterator on every path, and reserve all sibling names before recursion so a
  deep first child cannot multiply the aggregate entry budget.
- Archive-workspace failure cleanup now treats the target-directory fd and
  retained `/tmp` alias fd as independent owned resources: every pre-yield
  failure attempts each close exactly once, retains the binding/revalidation
  error as primary, and reports every cleanup failure separately. Canonical
  mirror process launch likewise keeps ownership of a successfully created
  child until the saved parent-directory fd is restored and closed; any
  return-before-handoff cleanup failure terminates and reaps that child before
  reporting the combined cleanup error.
- Native scheduler actions and daemon queries now share a binary-mode bounded
  supervisor. Producer bytes are counted independently for stdout and stderr,
  retained bytes never exceed 64 KiB per stream, and a monotonic 30-second
  action or 10-second query deadline bounds runtime. Overflow or timeout
  performs terminate, bounded drain, kill fallback, child reap, and process-
  group verification; cleanup uncertainty remains distinct from the primary
  output/deadline classification. A focused macOS CI matrix uses Python 3.9
  for the literal `O_SYMLINK` fallback and Python 3.13 for the runtime-provided
  flag, verifies the real `/tmp -> /private/tmp` alias, and runs all alias
  binding/revalidation/close regressions.
- The 2026-08-01 closure makes GitHub repository identities
  ASCII-case-insensitive, rejects portable source-path aliases and source modes
  other than `0644` / `0755`, and keeps transaction-journal inspection
  read-only until target-origin and complete stage-0 revalidation have passed.
  Manifest runtime/schema path rules now agree on backslash rejection and
  nullable base-release fields.
- Release-retention dry-run uses the stable home lock without creating a
  missing `install.lock`. Bare scheduler repair preserves audited mode, repo,
  base repo, owner, and interval while migrating legacy commands to
  `run-scheduled`; status exposes the reconstructable target and
  `migration_needed`. Foreign systemd drop-ins remain outside uninstall
  ownership, and ordinary release/mirror workflows do not mutate scheduler
  configuration.
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

- The post-review archive/bounded-inventory follow-up passed all 839 native
  Python 3.13 tests: 686 non-source-lock tests in 872.714 seconds with the one
  expected Linux-only skip, plus 153 source-lock tests in 1,310.454 seconds.
  Its 18 new focused tests also passed under Python 3.13 in 7.546 seconds and
  Xcode Python 3.9.6 in 4.990 seconds. Both runtimes passed compileall with
  task-private bytecode roots; full-tree Ruff lint/format, JSON parsing,
  actionlint, project-journal validation, and `git diff --check` passed, and no
  bytecode cache remains in the repository.
- Task-private `refresh-lock` and `refresh-lock --check` both verified all six
  canonical sources without accessing or mutating the retained production
  quarantine. The resulting SHA-256 values are
  `5333f82f4487b13bd99d3a97b17cbe50446e3eab11ceee2c8a4a77f1af2b1f62`
  for `sync-source-lock.json`,
  `31ea7eb6a8313a0e3a757dd875ba3e9e22705663b4e53495e5006b3bd2eb8f6d`
  for the engine, and
  `ecb1765b24e850ef9c54379c867ef2f6dca6e59ec54346b7e50fbdd7795db984`
  for the mirror generator.
- The 2026-08-01 final native-Python partition passed all 821 repository tests:
  203 engine tests in 49.518 seconds (one Linux-only skip), 118
  scheduler/doctor tests in 18.019 seconds, 359 reconciliation/retention/
  toolbox-automation tests in 431.586 seconds, and 141 source-lock tests in
  873.307 seconds. The final Python 3.9.6 focused compatibility selection
  passed 19 audit-closure tests in 16.644 seconds.
- Native and Python 3.9.6 compileall, JSON parsing, actionlint, full-tree Ruff
  lint/format, project-journal validation, and `git diff --check` passed. No
  bytecode cache remains in the repository.
- A task-private mode-0700 control root was used for both source-lock refresh
  and verification; all six sources matched. The production quarantine was not
  opened for payload inspection, moved, rewritten, or cleaned. Final SHA-256
  values are `c6df36fc87f4a1f1a618731aef958a3d112b2b334a34d83b293c34ac024daaa6`
  for `sync-source-lock.json`,
  `955ac2caf7e5e0dc0309dc55603c19b3f923efcf5c2096debd5732b9f0fb451b`
  for the engine, and
  `bff5e520f4d2e1ba62ea7b485991456ed41898444f399da26d1a257dab57002c`
  for the mirror generator.
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
- The final pre-landing ordinary ownership merge preserved signed safety head
  `39c7e63358196b055f583f8827ec7db57875bff8` as its first parent and signed
  toolbox-only ownership commit `4136b174a3faba3d98206d1a5192de1fe423bff3`
  as its second parent without rebasing or rewriting either line. That parent
  structure is historical candidate evidence, not a landed provenance gate:
  PR #5 later squash-landed as `6d078594d547598db037ce358c89c8a8ac58c881`,
  whose tree `70dc2c727e91036e9d155ec17dbed643eef26990` equals the reviewed PR head
  `15d3e6bec0d95233665a50678b75cd883c060da4` tree exactly. Downstream
  consumers bind the landed commit and tree-equivalence proof rather than
  requiring the candidate's parent chain. The merged candidate
  lock has exactly one `toolbox` mirror. Two task-private refreshes were
  byte-identical, `refresh-lock --check` verified all six sources, and the
  resulting `sync-source-lock.json` SHA-256 is
  `1789ea2aee5ef0d1325201fba6a7d82f0bfe41c5930695bb6a8a39eccf694d0f`.
  After the merge, all 153 source-lock tests passed under Python 3.13.0 in
  871.566 seconds and Xcode Python 3.9.6 in 921.177 seconds; all 27 toolbox
  automation tests passed under Python 3.13.0 in 399.025 seconds. The merge
  changes only governance/docs, the source lock, and its ownership expectation,
  so the already-recorded 839-test full implementation gate remains applicable
  to the unchanged engine/scheduler/reconciliation bytes. Dual-runtime
  compileall, full-tree Ruff lint/format, JSON parsing, actionlint,
  project-journal validation, source-lock verification, and staged
  `git diff --check` also passed. The retained production quarantine and host
  scheduler state were not mutated.
- Ubuntu Python 3.14 CI run `30713957335` on ownership-merge head
  `dfa65d17c1468fe393f32ad0fd001e975257c2d5` exposed two Darwin-test boundary
  defects: a broad `sys.platform` mock selected the host-incompatible
  `renameatx_np` cleanup primitive, and unlink/recreate could reuse the prior
  alias inode before path-only revalidation. The superseding append-only fix
  gives platform simulation a narrow predicate and retains an exact symlink
  descriptor through target binding and revalidation. The protected
  properties are alias object identity and access policy, link target, resolved
  target, and target-directory object identity/access policy; directory mtime
  and child-entry churn remain deliberately benign. Alias close failures are
  reported without masking an already-active primary failure, and the
  descriptor is closed before the workspace is yielded.
- The exact follow-up bytes passed all 842 disjoint repository tests under
  Python 3.13.12 and Xcode Python 3.9.6, with only the expected Linux read-lease
  integration fixture skipped in each runtime: 212 engine tests in 33.611 and
  40.170 seconds, 450 reconciliation/retention/scheduler tests in 122.729 and
  147.093 seconds, 27 toolbox automation tests in 260.030 and 258.664 seconds,
  and 153 source-lock tests in 790.403 and 836.454 seconds. Seven focused
  macOS-alias regressions, including the real `/tmp -> /private/tmp` path,
  passed in 0.011 and 0.017 seconds. Two task-private source-lock refreshes
  were byte-identical, followed by a successful currentness check; SHA-256 is
  `dbcffb38b173ed3cab845039a5a3cf797f57d3236baf1373667c3a10fe91a513`
  for `sync-source-lock.json`,
  `c29b102b62650ba8def788d115845236224ca5f867a40fdf54af9d46488660d2`
  for the engine, and
  `1c339c39e7e355a0b7f7d950e84c33a14423ffaa08017617e23ab6d6631e9fe6`
  for its tests. Dual-runtime compileall, full-tree Ruff lint/format, JSON
  parsing, actionlint, project-journal validation, and `git diff --check`
  passed; task-private bytecode roots were removed and no repository bytecode
  cache remains. The retained production quarantine and host scheduler state
  were not read for payload inspection, mutated, or cleaned.
- Formal single review of exact range
  `6c4878f33f5c82714e988b0470ccc5f4f33c0b70..a902bcf37d071f91f4e738c24734545316f668d2`
  completed from a fresh trusted-bundle materialization after the initial
  prompt correctly stopped before any Git call because its sanitized Git argv
  prefix was absent. The same reviewer resumed only after receiving the exact
  opaque prefix. Terminal revalidation kept the worktree on `a902bcf`, and the
  trusted playbook/guard/runtime digests matched their parent records. The
  resulting findings were the dual-fd cleanup gap, the post-`Popen` saved-fd
  orphan path, the scheduler's post-hoc/unbounded output capture, and missing
  native Darwin CI coverage.
- The remediation passed the focused engine/scheduler regressions under Python
  3.13 and Xcode Python 3.9.6, including exact-N pass/N+1 fail raw-byte caps for
  both streams, deadline cleanup, payload-suppressed classifications, both
  Darwin symlink flag paths, dual-owned-fd cleanup, and real child reaping after
  a saved-directory close failure. A final independent audit then found two
  additional scheduler boundaries: `allow_fail=True` could suppress an
  inconclusive process cleanup, and selector allocation could fail after
  `Popen` but before cleanup protection. The final implementation always
  propagates `scheduler-cleanup-inconclusive`, sends `SIGTERM` before optional
  selector allocation, and retains monotonic `SIGKILL`, reap, process-group,
  and pipe-close cleanup even when both supervision and cleanup selector
  allocation fail. The independent audit revalidated the fix and returned
  `No findings.`
- The exact final engine suite passed all 213 tests under Python 3.13.0 in
  37.641 seconds and Xcode Python 3.9.6 in 44.898 seconds, with only the
  expected Linux read-lease integration fixture skipped in each runtime. The
  exact scheduler/doctor suite passed all 125 tests in 10.203 and 12.113
  seconds under `ResourceWarning=error`; this includes a real child proving
  reap and pipe closure when runner and cleanup selector allocation both fail,
  plus the regression proving cleanup uncertainty cannot be allow-failed. The
  exact final source-lock suite passed all 154 tests under the same warning
  policy in 662.913 and 702.806 seconds. Earlier reconciliation, retention,
  and toolbox automation evidence remains applicable because those production
  and test bytes did not change after its exact final run.
- Two task-private source-lock refreshes were byte-identical and the currentness
  check verified all six sources. SHA-256 is
  `a9bda725d8031c5ffe103d55edf5bc45015a78ac22703558a38907ab875dc960`
  for `sync-source-lock.json`,
  `418a4aa9e2231ccadafec212a1f99bfb555bf884dddda64f3954258f04bcfcc7`
  for the engine,
  `bd56d9abcf58d745ba3f88df3e29f4c43c62be61c7d5d717dc10c31420402172`
  for its tests,
  `107e097e54f90a78a643679ac7e227894c2ccf0fc9bdfb390df3376381118b95`
  for scheduler/doctor tests, and
  `39e73e72c8ebd017ad467e481a606ae7907b124b6d5b630fcb53205ef723de8f`
  for the generated-mirror controller. Dual-runtime compileall, Ruff
  lint/format, actionlint, JSON parsing, project-journal validation,
  source-lock verification, and `git diff --check` passed. The mode-0700
  task-private control root was used throughout; production quarantine and host
  scheduler state were not read for payload inspection, mutated, or cleaned.
- Signed head `cfc1d1eb7a2e0c19aa2d16977c22820827419239` passed hosted CI
  run `30718963989`, including the main test job and both new macOS Python 3.9
  and 3.13 alias jobs, plus review-gate run `30718963214`. Its exact-secret
  admission was clean with complete temporary cleanup over the exact
  `6c4878f..cfc1d1e` range. A fresh prior-trusted-bundle named-single review
  used a separately materialized and validated private worktree and found one
  blocking macOS locator defect: the closed Git environment omitted `TMPDIR`,
  so sandboxed `xcrun --find git` returned a valid path plus a `confstr()`
  fallback warning that the strict stderr gate rejected during module import.
  The finding matched a direct sandbox reproduction. Post-lane validation
  remained bound to `cfc1d1e`, the trusted control digests were unchanged, and
  the private review workspace was removed completely. All of those head-bound
  gates become stale when the follow-up fix is committed.
- The follow-up binds fixed `/private/tmp` through the existing absolute
  no-follow control-object binder, requires exact root-owned mode-`01777`
  access policy, and revalidates directory object identity and access policy
  immediately before `Popen` and again in the terminal `finally`. Only that
  bound absolute path is added as `TMPDIR` for fixed `/usr/bin/xcrun`; ambient
  `TMPDIR` remains excluded and every stderr byte remains fatal. The protected
  properties are temporary-root object identity and access policy. Child-entry
  churn is deliberately not compared. Adversarial tests cover the sanitized
  import/`--help` path, exact environment injection, nonzero exit, stderr,
  ambiguous/relative/shim stdout, initial symlink or policy mismatch, and
  post-bind replacement or policy drift. The focused locator suite passed all
  6 tests under Python 3.13.0 and Xcode Python 3.9.6 in 1.503 and 1.681
  seconds; an independent read-only audit returned `No findings.`
- The final affected suites passed under `ResourceWarning=error`: all 160
  source-lock tests in 593.845 and 628.869 seconds, and all 27 toolbox
  automation tests in 184.297 and 192.706 seconds, for Python 3.13.0 and Xcode
  Python 3.9.6 respectively. Two sandbox-local task-private source-lock
  refreshes were byte-identical and the currentness check verified all six
  sources without an escape. The unchanged lock SHA-256 is
  `a9bda725d8031c5ffe103d55edf5bc45015a78ac22703558a38907ab875dc960`;
  the final generated-mirror controller, source-lock tests, and CI workflow
  SHA-256 values are
  `40e6068d141b3f5f7b9d0fd02b5bc392c1c62f785c49d0b8322b7df9702defb9`,
  `d374afbd75fdc684de06f3c8488741cee83124e4fccdd8f4a01eb866b845d955`,
  and
  `6c926d12cd5bfe58c5f56e7d6a3a4f6529e7f0d3dd74e476750419ba39374cfa`.
  Dual-runtime compileall, Ruff lint/format, actionlint, JSON parsing,
  project-journal validation, source-lock verification, and `git diff --check`
  passed; no repository bytecode cache remains.
- The fresh named-single review of signed head `bdbd9dc` found two remaining
  process/control-plane gaps. Repository-local `fsck.*` settings could weaken
  or externalize the private snapshot's `git fsck --strict` result, and a Git
  or xcrun leader could exit while a same-session descendant survived the
  stated deadline. Independent review also proved that `url.*.insteadOf` and
  `url.*.pushInsteadOf` made origin/push identity ambiguous, and that the
  token-bearing toolbox prepare step checked out an unvalidated remote branch
  before its scope/history admission. The prior head's hosted CI and review
  gate passed, but all head-bound admission/review evidence became stale when
  this follow-up began.
- The follow-up rejects every case-folded repository-local `fsck.*`,
  `url.*.insteadOf`, and `url.*.pushInsteadOf` key from both private `config`
  and `config.worktree` snapshots before any object query or credential-bearing
  push. Bounded Git/xcrun supervision now treats leader reaping, a bound
  process-group `SIGKILL` fence, and independent closure of both parent pipe
  handles as separate terminal properties. Every numeric PGID operation occurs
  while the observed leader is still unreaped; the final `wait` is followed by
  no PID/PGID signal or probe that could hit a reused identity. Darwin `EPERM`
  is not treated as a general absence proof: it is accepted only after
  waitid/kqueue observed leader exit and only for the closed xcrun/private-Git
  profiles, which bind a fixed executable/argv family, closed environment,
  same real/effective credentials, no credential-transition Popen options,
  and no repository-selected hooks, filters, aliases, or helpers. The owner is
  published before `Popen.__init__`, parent-only deferred handlers keep
  SIGINT/SIGTERM/SIGHUP from interrupting handoff without blocking them in the
  child, and one owner-held absolute deadline covers every cleanup layer.
  Selector allocation/registration and every pre-caller `Popen` failure remain
  inside that cleanup ownership boundary. Signal-handler install/restore paths
  best-effort roll back both exact handlers and the prior thread mask while
  preserving the original failure as the explicit cause.
- Toolbox preparation now validates an existing remote branch by exact commit
  objects while the worktree remains detached at the trusted base. The
  version-2 branch-history receipt separately binds `base_sha`, `head_sha`, and
  `worktree_head_sha`; the credential is unset after the final fetch and before
  target-tree inspection. A real integration fixture proved that an untrusted
  `.gitattributes` plus configured smudge sentinel is rejected as out of scope
  without running the filter or changing HEAD/index, while consecutive
  unmerged rename/removal recovery still succeeds. Fetching the exact object/ref
  remains the necessary pre-admission mutation; no untrusted tree is checked
  out.
- The completed follow-up source-lock suite ran 183 tests under both Python
  3.13.0 and system Python 3.9.6, ending `OK` in 654.150 and 688.654 seconds;
  the Python 3.13 run had the expected no-`waitid` skip. The toolbox automation
  suite ran all 28 tests under both runtimes and ended `OK` in 201.606 and
  201.894 seconds. A final source-lock refresh retained the six-source lock
  byte-for-byte, and both runtimes independently passed `refresh-lock --check`.
  The unchanged lock SHA-256 is
  `a9bda725d8031c5ffe103d55edf5bc45015a78ac22703558a38907ab875dc960`;
  final SHA-256 values are
  `828dcc4e3a73cdb93a58ff227b8c0298106100f4433dc1c55b11fa10884a12c3`
  for the generated-mirror controller,
  `50cc8c4d82326b342b50a5a3971211bb1b6d9355a1186e38a2230044de3381d7`
  for its source-lock tests,
  `e2c4e223de460050e019cbc922a9d41f010b1a54f21f6b810cb600b9f78ccd9f`
  for toolbox automation tests, and
  `2da352cce0245117c9be992486a521c66b91929776d0a606629522c1c33a38bb`
  for the workflow. Dual-runtime compileall, Ruff lint/format, actionlint,
  JSON parsing, project-journal validation, source-lock verification, and
  `git diff --check` passed before this evidence-only journal update.
- Signed head `ee7bab62a47a65532228325decb47bef00cb3dc1` retained the two
  successful macOS alias jobs and the workflow runner success, but its Linux
  Python 3.14.6 main job failed after 881 tests in 913.746 seconds with five
  failures and one error. All five failures used a post-return
  `killpg(pgid, 0)` assertion that cannot distinguish a live member from an
  orphan zombie on Linux; the error read Darwin-only kqueue constants before
  installing the test's mocked Darwin surface. The in-progress fresh
  named-single lane was immediately cancelled as stale/non-counting. Its
  private worktree passed terminal trusted-guard validation, the external
  control digests remained exact, and the owner-private review root was then
  removed completely.
- The portability follow-up leaves production process supervision unchanged:
  the bound process-group `SIGKILL` still occurs while the leader is unreaped,
  the leader still has exactly one final `wait`, and production performs no
  numeric PID/PGID operation afterward. Each affected fixture now gives the
  launched process an inherited liveness-pipe writer, closes the parent copy
  immediately after spawn, and makes a fork leader close its copy so only the
  intended descendant retains that capability. Bounded EOF therefore proves
  that no live fixture descendant retains the writer without depending on
  orphan-zombie or numeric-PGID observation semantics. A negative control
  proves that closing only the parent writer cannot falsely satisfy the EOF
  gate. Fixture cleanup itself signals the group only while the leader remains
  unreaped. The kqueue lifecycle unit now injects its complete synthetic Darwin
  constant/function surface before exercising the mocked observer on
  non-Darwin hosts.
- The settled portability-test bytes passed the 10-test focused matrix under
  Homebrew Python 3.14 and system Python 3.9.6 in 2.784 and 3.014 seconds; the
  Python 3.14 run had the expected no-`waitid` skip. The complete source-lock
  suite then passed all 184 tests under both runtimes in 721.894 and 764.071
  seconds respectively, with the same sole expected Python 3.14 skip. A fresh
  independent read-only audit returned `No findings.` for the writer ownership,
  close order, bounded EOF property, negative control, and cleanup paths. The
  final source-lock-test SHA-256 is
  `b77d80a1507262aeaad09a42bc8d54ad4644b05340c44f69b6af24d586366e1f`.
  Both runtimes passed final compileall and independent six-source
  `refresh-lock --check`; Ruff lint/format, project-journal validation, and
  `git diff --check` also passed on the final test bytes.
  The controller, toolbox-automation test, workflow, and source-lock bytes are
  unchanged from signed head `ee7bab62a47a65532228325decb47bef00cb3dc1`, so
  that head's successful dual-runtime compileall, automation matrix, Ruff,
  actionlint, JSON, journal, source-lock-currentness, macOS alias, and workflow
  runner evidence remains tree-valid for those exact unchanged files. Hosted
  Linux CI, exact-secret admission, and formal named review remain head-bound
  and must be rerun after the portability commit is pushed.
- The fresh named-single review of signed portability head `9befd7e` found that
  GitHub and native-scheduler supervision still reaped the direct child before
  probing or signalling its numeric process group, while cleanup could call
  `poll()` and later reuse that same number. It also found that an exception
  from either main selector's `close()` could replace the primary timeout,
  output-limit, or process-I/O classification. An independent direct-Claude
  lane confirmed the process-group finding. All head-bound review and admission
  evidence became stale when this repair began.
- The repair protects guardian identity, group fencing, protocol integrity,
  bounded output, and error precedence separately. A fresh isolated Python
  guardian is launched as a dedicated live session and process-group leader;
  it launches the requested `gh` or fixed native target in that group, closes
  its own stdout/stderr writers, uniquely waits for the direct target, and
  publishes a fixed-size status record while retaining the sole status writer
  as a liveness capability. Ready and status records bind the exact guardian
  PID and target PID. The target cannot inherit either control writer. The
  parent requires ready receipt completion, both output EOFs, an exact status
  record, and a live-but-quiet status writer before it sends group `SIGKILL`.
  The nonblocking `EAGAIN` proves only that this liveness capability remains
  open at that probe boundary; it does not make the probe and `killpg` atomic
  or prevent an external same-UID process from signalling or escaping the
  group. Safety instead relies on never reaping the guardian before `killpg`,
  so its numeric identity cannot be reused, and on failing closed for every
  signal failure or final result other than exact `-SIGKILL`.
  `ESRCH`, `EPERM`, early guardian exit, extra/truncated protocol bytes, a
  non-`SIGKILL` guardian result, or any cleanup/close uncertainty fails closed.
  The guardian has one final `wait`; there is no numeric PID/PGID operation
  after it. This removes the need for any PATH-executable trust profile or a
  racy Darwin process-table exception.
- Process creation owns all four control-pipe descriptors from `-1`-initialized
  slots and transfers the ready reader before parsing, so second-pipe failure,
  parser failure, and descriptor-number reuse cannot leak or double-close an
  unrelated object. Target-launch failure uses a distinct fixed ready record
  and retains each lane's unavailable taxonomy. Guardian cleanup uncertainty
  is mapped to `gh-cleanup-inconclusive` or
  `scheduler-cleanup-inconclusive`; scheduler `allow_fail` therefore cannot
  ignore it. Selector and stream close failures are collected as secondary
  diagnostics, including non-`OSError` exceptions, without replacing the
  primary cause.
- The first complete affected-file checkpoint passed 349 tests in 32.286
  seconds with one expected platform skip. Focused negative tests additionally
  proved
  `killpg -> wait` ordering, zero post-wait numeric operations, FIFO-liveness
  termination of a target descendant that closed stdio, status-writer EOF
  rejection, target control-FD exclusion, launch/ready protocol cleanup,
  descriptor reuse safety, lane-specific cleanup taxonomy, and primary-error
  preservation across selector-close failure.
- The frozen affected-file suite then passed 356 tests under both runtimes:
  33.229 seconds under the default Python and 41.220 seconds under macOS system
  Python 3.9, with one expected platform skip in each run. The first 3.9 run
  showed that a 50-millisecond operation budget could expire during guardian
  startup and be misclassified as a ready-protocol timeout. The production
  bound remains `min(operation deadline, five-second ready cap)`; only the
  exhausted-operation branch is now mapped back to the existing `gh-timeout`
  or `scheduler-timeout` lane taxonomy. No runtime, byte, cleanup, or
  unavailable bound was relaxed. The GitHub stalled-target test now uses a
  one-second operation budget so it deterministically starts the target; the
  scheduler 50-millisecond test remains.
- `refresh-lock` updated all six source records for the frozen bytes. Both
  supported runtimes verified the lock, whose canonical file SHA-256 is
  `700cab74025b32e31145dedfac8e5861143056c477b5c9c65bb4d53219a1f52b`;
  the canonical synchronizer source SHA-256 is
  `ec92fea5e43b5897aab7d8afbe6b3c54874178537bf0672db8d5b83f8970d361`.
  The complete source-lock suite passed 184 tests in 588.901 seconds under the
  default Python with one expected Darwin-variant skip, and 184 tests in
  636.903 seconds under macOS system Python 3.9 with no skips. Dual-runtime
  compileall, Ruff format/check, and `git diff --check` also passed. Hosted CI,
  exact-head admission, and fresh formal review remain head-bound post-push
  gates rather than evidence for these uncommitted bytes.
- The named-single P2 follow-up removes the predictable shared temporary
  allocation root without abandoning its recovery evidence. New allocations
  use the descriptor-bound passwd account-home namespace; the old shared root
  is metadata-only for foreign owners and original-root-only for same-UID
  recovery. Machine parity binds the generator and standalone engine registry,
  policies, reason codes, ancestor cap, classification, allocation matrix, and
  the owner-record v1/v2 root-scope matrix. New owner records are closed-field
  version 2 with exact `root_id`; version 1 is accepted only in
  `legacy-shared-v0`. Cross-root, missing, extra, and unknown-version scope is
  retained without mutation as `private-owner-root-mismatch` in both generator
  and doctor evidence.
  The automation test runner was upgraded from a one-variable parent patch to
  an isolated account-home plus RootSpec registry so subprocess tests cannot
  fall through to production state.
- The exact five-group remediation audit is clean. It verified close-once
  descriptor ownership and later-root coverage in the generator and installed
  engine, body/unlock/close error aggregation, anchored absence and terminal
  receipt cleanup, and exact legacy owner-root mismatch routing. The final
  mismatch fix preserves reason precedence as `private-owner-root-mismatch`,
  then `legacy-recovery-pending`, then generic inconclusive. Its end-to-end
  test uses a real same-UID legacy wrong-root owner record and proves the
  primary namespace is not allocated while owner/private evidence and the
  legacy tool/quarantine entry sets remain unchanged.
- The final focused remediation/parity matrix passed 25 tests under both the
  default Python 3.13 and macOS system Python 3.9. Full scheduler/doctor tests
  passed 149 tests in 17.943 and 20.917 seconds respectively. Full toolbox-sync
  automation tests passed 28 tests in 270.406 and 289.229 seconds. The first
  230-test source-lock pass under each runtime had exactly the expected stale
  lock-currentness error and no functional failure. After the task-private
  account-home runner refreshed all six sources, both runtimes verified the
  lock and the complete suite passed from scratch: 230 tests in 830.251 seconds
  with one expected platform skip under Python 3.13, and 230 tests in 870.800
  seconds under Python 3.9 with no skips.
- The refreshed `sync-source-lock.json` SHA-256 is
  `e42ed82769bc5f5e5cb998a009c4c4e54b50a82bef82d4d9a4a5f822f50803bc`.
  The canonical engine SHA-256 is
  `ba433d61f4b5aa8c3e4a17f8365488ca5089c58511e091b32e4bc2ae6b872a8a`,
  and the generator SHA-256 is
  `80a7d51804d91b96bae6a2ec3c352fc92fc70cac19640bec992d3b32809f601a`.
  Hosted CI, exact-head admission, and formal PR review remain head-bound
  post-push gates and are not claimed for these uncommitted bytes.
- PR #6 checkpoint `c157628d461df693eb4cbab7a7cb76000b019255`
  updates only `sync-source-lock.json` after the fixture source commit. Its
  official BL-host refresh and `refresh-lock --check` verified all six sources;
  the complete source-lock suite passed 230/230 tests, and the direct
  `TMPDIR=/tmp` scheduler-doctor suite passed 150/150 tests. The resulting lock
  SHA-256 is
  `eaa104c4fdb5ef92cdf0cfd297424cee3d6eb319776cbf48a937d0e2d76c9634`.
  Current-head hosted CI, admission, and formal review remain head-bound gates.
- The formal direct-Claude review of PR #6 head `888ecf2c4e6635864883e009ff7940be037a4f6c`
  found that scheduler-doctor fixtures rooted directly in the real account home
  could leave residue after abnormal termination and couple the suite to host
  home-directory policy. Signed follow-up
  `c04e8f222c312fb0ea542cd8747c5bbd00e5bb59` replaces those roots with one
  repository-local, owner-private suite session namespace, an exclusive
  persistent lease, stale-session cleanup, and per-test `addCleanup`; no test
  root uses the real passwd home. The owner host passed the complete
  scheduler-doctor suite 152/152 under Python 3.13 and macOS system Python 3.9,
  plus Ruff and `git diff --check`. Its one official `refresh-lock` attempt
  correctly stopped at `legacy-recovery-pending` without bypassing or mutating
  the retained legacy evidence. On BL, the stock `refresh-lock` and
  `refresh-lock --check` commands verified all six sources; the complete
  source-lock suite passed 230 tests with one expected platform skip, and the
  direct `TMPDIR=/tmp` scheduler-doctor suite passed 152/152 tests. The
  refreshed `sync-source-lock.json` SHA-256 is
  `5dc260c4be55ccf76fed0819a123c21b22cec392df72d94eb86522dd82882833`.
  Merge, release, and all final current-head admission/review gates remain
  outstanding.
- The final fixture hardening is frozen at signed implementation head
  `d5f7c194db7902a1d41a0aa4cc7b239c45823670`, tree
  `6fe17ecac0a5a8648c1ceb423c86aadf25b3bc5a`, with sole parent
  `e10a275b6956707f7efc2e6f5cd8d6589bf9ad58`. It carries the trusted anchor
  and namespace descriptors through lease probing, allocation, stale-session
  sweep, and cleanup; permits candidate fallback only for exact stable policy
  or permission failures; and fails closed on identity drift, unreadability,
  secondary cleanup failure, or descriptor-close uncertainty. Tracked tests
  cover real Darwin `/tmp` copied checkouts, partial `EROFS` cleanup,
  permission fallback, bounded descriptor cleanup, and exact explicit-anchor
  selection. The owner host passed 160/160 scheduler-doctor tests under uv
  Python 3.13 and macOS system Python 3.9, plus `git diff --check` and an
  independent read-only audit with no findings.
- A fresh full BL custody clone independently matched the exact head, tree,
  sole parent, changed blob, and GitHub provider-valid signature. Its initial
  stock `refresh-lock` invocation failed closed because the owner-private clone
  umask materialized the Git-declared `100755` engine as physical mode `0700`.
  After restoring only Git-declared tracked regular-file modes
  (`100644 -> 0644`, `100755 -> 0755`), the same stock command refreshed all
  six sources and `refresh-lock --check` verified them. The complete
  source-lock suite passed 230/230 tests in 347.957 seconds with one expected
  platform skip; the direct `TMPDIR=/tmp` scheduler-doctor suite passed
  160/160 tests in 3.679 seconds. A separate two-test smoke used a unique
  trusted explicit anchor and a checkout physically nested beneath Darwin
  `/tmp -> /private/tmp`; it passed 2/2 tests, left only the expected
  single-link mode-`0600` `.session.lock`, leaked no copied checkout or session
  directory, and the task anchor was then removed by identity-checked,
  bottom-up cleanup. The refreshed `sync-source-lock.json` SHA-256 is
  `5d248e53641c642a447a399dbb5297a7d3295b3aeb5e37137b9b4241a39e6251`.
  Merge, release, installed-state changes, and final current-head review or
  admission gates are not claimed by this checkpoint.
- The stale-session sweep bound is frozen at signed implementation head
  `0ad62f4aae4594d5367e945e52fb2fa2f287f09b`, tree
  `c774502d7acd2837b9d32bac0a493dade34e1494`, with sole parent
  `46feb5e2b1a68dd19488ebe0a11ec24acc359253`. Its scheduler-doctor test blob is
  `a06ace9f9530957f746a28f4f2f2fc6bedc4a874`, with SHA-256
  `a92f40c644641417e82135407e1d537a4f1f6dea9fa61831b46734d4e72a5925`.
  The fixture uses a context-managed `os.scandir`, reads at most the declared
  1024-entry bound plus one overflow item, raises before append or deletion on
  item 1025, closes the iterator on every path, and sorts only the bounded
  collection. Exact-1024 and 1025-item tests bind both properties.
- A fresh full BL custody clone matched the head, tree, parent, changed blob,
  unique PR merge base, and GitHub provider-valid signature; full strict fsck
  covered all 810 local objects with no missing, promisor, alternate, or bitmap
  dependency. After restoring only Git-declared tracked regular-file modes
  narrowed by the clone's owner-private umask, stock `refresh-lock` refreshed
  all six sources and `refresh-lock --check` verified them. The complete
  source-lock suite passed 230/230 tests in 349.259 seconds with one expected
  platform skip. With `TMPDIR=/tmp` and a unique trusted explicit anchor, the
  scheduler-doctor suite passed 162/162 tests in 3.945 seconds. A separate
  checkout physically nested beneath Darwin `/tmp -> /private/tmp` passed the
  two-test explicit-anchor smoke in 0.315 seconds; the anchor retained only the
  expected single-link mode-`0600` `.session.lock` and no session directory.
  The refreshed `sync-source-lock.json` SHA-256 is
  `3dc052aceb912f0ff2c951c5af81b2a3a620a31edcc4a08a19d425d0ff5731ed`.
  Exact-head macOS Python 3.13 and 3.9 CI jobs passed; Linux stopped at the
  expected stale-lock precondition before downstream tests. Merge, release,
  installed-state changes, and final current-head admission or review gates
  are not claimed by this checkpoint.
- The PR #6 scheduler-doctor fixture hardening substage is complete. The two
  applicable fixture findings were fixed at signed implementation head
  `9fd850cb5d2aec2f463585fd237f0016dd71434e`,
  tree `76d0b026d86d42a4e9a97534432dafd8d8fb130e`, with sole parent
  `b1309d3742155bbd3a40458a3ed463fa63e6f6c6`. Its scheduler-doctor test blob
  is `a3f44375b0f83a37eef6498c3a3bc317e6d50c67`, with SHA-256
  `c302eba7a7da7a14fd5443d8552e60165093398deb40196d5b1ae41b177f6734`.
  A probe that loses the exclusive `.session.lock` create race now performs
  one no-follow reopen, binds fallback name identity to the opened descriptor,
  and fails closed on replacement without deleting the concurrent winner's
  lock. The bounded stale-session sweep now excludes the exact persistent lock
  before applying the 1024-session limit: 1024 sessions plus the lock succeed,
  while the 1025th session stops the scan after 1026 physical entries and
  before any partial deletion. Four focused tests bind those properties. On
  the owner host, both complete scheduler-doctor suites passed 164/164 under
  uv Python 3.13 and macOS system Python 3.9, with Ruff, `py_compile`, and
  `git diff --check` clean. Its one stock lock-refresh attempt changed no
  tracked bytes and correctly stopped at `legacy-shared-v0` state
  `legacy-recovery-pending`; this checkpoint neither repairs nor removes that
  retained host evidence. This exact implementation identity is historical
  review evidence, not the future canonical release identity.
- A fresh full BL custody clone matched the exact implementation head, tree,
  sole parent, changed blob, unique PR merge base, and GitHub provider-valid
  signature; full strict fsck found no missing, promisor, alternate, or bitmap
  dependency. Standard `umask 022` materialized the six locked sources at
  their Git-declared modes, so no mode repair was needed. The unmodified stock
  `refresh-lock` refreshed all six records and stock `refresh-lock --check`
  verified them. The complete source-lock suite passed 230/230 tests in
  333.131 seconds with one expected platform skip through the repository's
  private-TMPDIR wrapper. The four focused fixture tests passed 4/4; complete
  scheduler-doctor suites passed 164/164 under uv Python 3.13.13 and 164/164
  under macOS system Python 3.9.6. A separate checkout physically nested under
  Darwin `/tmp -> /private/tmp` passed the two-test explicit-anchor smoke in
  0.302 seconds; the owner-private namespace retained only the expected
  single-link mode-`0600` `.session.lock`, with no copied checkout or
  `session.*` residue. The refreshed `sync-source-lock.json` SHA-256 is
  `20d1d611ddecfae5397d3b547b85750e6805f446dfb38b18bdef203e43baa322`.
  The fixture fixes, final source lock, and their validation are complete for
  the target-branch state. The wider canonical-to-toolbox-to-private-to-
  scheduler workstream remains active: after P lands, generator provenance
  must bind the actual squash-landed P identity before producing T/B and
  propagating the downstream private and scheduler state.
- The final test-only stabilization is preserved as historical pre-squash
  evidence at signed head `591cec395f7406660e59c82b64c38a16757aead7`,
  tree `646f088834722f003064a9d48472a3181702ae07`, with sole parent
  `7ee54f08de744146d72b56e265265797d26e835f`. Its only changed path is
  `tests/test_scheduler_doctor.py`, blob
  `3defcdb445342f394e69ea65d4eee6a12e0a9a18`, with SHA-256
  `6a0cc99989b77823a7edd764ce693031b60b865f1d1656d591a3544fb7ca7b10`.
  The replacement regression now keeps the concurrently created lock
  descriptor open across unlink and replacement creation, preventing
  immediate inode reuse, and a nested `finally` closes the namespace
  descriptor even if closing the held descriptor fails. Production and
  fixture behavior are otherwise unchanged. On the owner host, both complete
  scheduler-doctor suites passed 164/164 under uv Python 3.13 and macOS system
  Python 3.9, with the exact regression, Ruff E4/E7/E9/F, `py_compile`, and
  `git diff --check` clean.
- A new full BL custody clone matched the exact signed head, tree, sole parent,
  changed blob, unique PR merge base, and GitHub provider-valid signature;
  strict full fsck found no shallow, promisor, alternate, bitmap, or missing
  object dependency. The clone began under owner-private `umask 077`; the
  first source-lock suite correctly rejected Git-tracked mode drift and is
  non-counting. Restoring 16 tracked regular files to their exact Git-declared
  physical modes changed no bytes. Stock `refresh-lock` then refreshed all six
  records, stock `refresh-lock --check` verified them, and the counting source-
  lock suite passed 230/230 in 356.429 seconds with one expected platform skip
  under standard `umask 022`. The exact regression passed 1/1, complete
  scheduler-doctor suites passed 164/164 under uv Python 3.13.13 and 164/164
  under macOS system Python 3.9.6, and the explicit trusted-anchor copied-
  checkout smoke passed 2/2 under Darwin `/tmp -> /private/tmp`. Ruff
  E4/E7/E9/F, both-runtime `py_compile`, and `git diff --check` were clean; the
  owner-private namespace retained only its expected single-link mode-`0600`
  `.session.lock`. The refreshed `sync-source-lock.json` SHA-256 is
  `5ef9f974db8129eb204c16ebd308e92fbc5005b79c77577e29e3d5cd5ccc227c`.
  The PR #6 scheduler-doctor fixture and final source-lock substage are
  complete in target-branch semantics. Head `591cec395f7406660e59c82b64c38a16757aead7`
  remains historical validation evidence only; downstream generation must
  bind the actual squash-landed canonical `P` identity.
- The final PR #6 remediation is preserved as historical pre-squash evidence
  at signed implementation head
  `793a690a2454d0c761e6a08ffdc84999db78dcd6`, tree
  `c5bb627c65b62916e266f1f6c650e90d6b4eeb8e`, with sole parent
  `34ba9b4210adf15a80e838252e528b3211f01d7b` and signing fingerprint
  `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. Its only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `5e87b7f6ef36d84ea7d4a51f133658958c9c9296`, with SHA-256
  `8510855b4a4141ad3cf6a2d24cf3414770f9c8eca5de41229127494300fdd49e`.
  The fixture no longer resolves or writes the real passwd account home;
  module-lease acquisition uses nonblocking `flock` under a monotonic timeout;
  stale-session planning and descriptor-relative deletion share explicit
  entry, depth, and deadline budgets while binding object identity and the
  owner-private access policy; and the journal summary now points at the final
  fixture and lock evidence rather than an intermediate checkpoint. On the
  owner host, the exact tree passed 25/25 focused fixture tests, 174/174 full
  scheduler-doctor tests under uv Python 3.13 and 174/174 under macOS system
  Python 3.9, plus Ruff E4/E7/E9/F, `py_compile`, and `git diff --check`.
  A fresh full BL custody clone independently matched the head, tree, parent,
  branch, unique PR merge base, object closure, and GitHub provider-valid
  signature. The unmodified stock `refresh-lock` refreshed all six sources and
  stock `refresh-lock --check` verified them. Under standard `umask 022`, the
  complete canonical source-lock suite passed 230/230 in 345.369 seconds with
  one expected platform skip, the focused fixture class passed 25/25, and the
  full scheduler-doctor suite passed 174/174 under uv Python 3.13.13. The
  explicit owner-private test anchor retained only the expected regular,
  single-link mode-`0600` `.session.lock` and no `session.*` directory. The
  refreshed `sync-source-lock.json` SHA-256 is
  `73bd88706d65a79569c0b2e05061590aac73345b48faa7aab29a5168a524db66`.
  The fixture fixes, source lock, and their validation are complete for the
  target-branch state. All listed heads remain historical validation evidence;
  after squash landing, toolbox generation must bind the actual canonical `P`
  identity before producing `T`/`B` or propagating private and scheduler state.
- The shared-temporary-checkout remediation is preserved as historical
  pre-squash evidence at signed implementation head
  `c39f0f6e57d4059323fbc0076707f7be53688922`, tree
  `ff179f9484ea0d34576e7d5fa0eea26bad717b5b`, with sole parent
  `aefb480e77d6e445b8e8eb3b7888bb2b3f544525`. Its only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `89cca7bdc776c1ac274a738a07c9774b083eec29`, with SHA-256
  `242440881564df69154433961c05343eff556a481d04b39eb688803496cbaafe`.
  A checkout below shared `/tmp` now allocates a cleanup-owned platform anchor
  without falling back to the account home. Darwin accepts only canonical
  `/private/var/folders/**` user-temp candidates and Linux only
  `/run/user/<euid>/**`; the fixed-path Darwin fallback uses descriptor-relative
  no-follow traversal with one 4,096-entry budget, binds identity and access
  policy at every component, and sorts only the bounded accepted set. A
  scan-to-use re-resolution must remain byte/path equal and inside the fixed
  platform scope before the full ancestry bind and allocation. The existing
  cooperative-same-UID non-guarantee is unchanged.
  A fresh full BL custody clone independently matched the branch, head, tree,
  parent, unique PR merge base, complete object closure, and provider-valid
  signature. Unmodified stock `refresh-lock` refreshed all six sources and
  stock `refresh-lock --check` verified them. Under standard `umask 022`, the
  complete canonical source-lock suite passed 230/230 in 376.149 seconds with
  one expected platform skip. `SchedulerDoctorFixtureTests` passed 34/34 under
  uv Python 3.13.13 in 0.640 seconds and macOS system Python 3.9.6 in 0.442
  seconds; the full scheduler-doctor suite passed 183/183 in 4.269 and 5.360
  seconds respectively. The two real `TMPDIR=/tmp` copied-checkout cases also
  passed 2/2 under each runtime in 0.618 and 0.419 seconds, with no retained
  `scheduler-doctor-checkout.*` directory or cleanup-owned platform anchor.
  The refreshed `sync-source-lock.json` SHA-256 is
  `4695d2c0f3985b4b5014a6e560c03525c8dc927b449b985866964ec379c57641`.
  The fixture behavior, source lock, and validation are complete for the
  target-branch state; downstream generation still binds only the actual
  squash-landed canonical `P` identity before producing `T`/`B` or propagating
  private and scheduler state.
- The stable platform-namespace remediation is preserved as historical
  pre-squash evidence at signed implementation head
  `09c4d648f0c2bd4befad5c3f73cf6e504c3d767d`, tree
  `a31fc531c5f14b9422e2742b02351ea740f57516`, with sole parent
  `2a2ceca80e31e867f29d71679856db6015f75076` and signing fingerprint
  `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. Its implementation changes are
  `.gitignore`, blob `3b7b62fe65629bf3dcdb07e8f9903efe41f0a95f` with SHA-256
  `67efa32b243fbba8989679f2e657c2411e5d66b5437e097171249b5f8dd0789f`,
  and `tests/test_scheduler_doctor.py`, blob
  `889ef6b7c3f22821ee354a603933bed00cdd514b` with SHA-256
  `84a2fb9a483099d6ca445af79b872e6a50348ec1f1cc1fbbcd9e72b1344a49bb`.
  The fixture now selects an explicit anchor, a stable scoped Darwin/Linux
  platform parent, or the repository root; it uses the stable
  `<candidate>/.codex-tmp/scheduler-doctor` namespace, leaves only its safe
  mode-`0600` lease, and independently resets the session, descriptors, and
  module globals. Descriptor-relative stale recovery remains bounded by entry,
  depth, and deadline budgets. Canonical keeps both `/.codex-test-tmp/` for
  legacy residue and `/.codex-tmp/` for the stable namespace. The toolbox
  generated surface remains six source files plus its receipt: `.gitignore` is
  consumer-owned and is not added to the mapping. The mapping digest remains
  `3e26648dd65526e759089c5acf5a9f429f3df0f5adc8dbe94b3856954b801ece`
  and the file-set digest remains
  `c280b934568b6bc8df0c993b91d3e2e051970a8395870bf0419fc475556af7ad`.
  A fresh full BL custody clone independently matched the branch, head, tree,
  parent, unique PR merge base, complete object closure, provider signature,
  and local `GOODSIG`/`VALIDSIG`. Unmodified stock `refresh-lock` refreshed all
  six sources and stock `refresh-lock --check` verified them after restoring
  only the Git-declared physical modes in the owner-private checkout. Under
  standard `umask 022`, the complete canonical source-lock suite passed
  230/230 in 350.163 seconds with one expected platform skip.
  `SchedulerDoctorFixtureTests` passed 40/40 under uv Python 3.13.13 in 0.653
  seconds and macOS system Python 3.9.6 in 0.454 seconds; the full
  scheduler-doctor suite passed 189/189 in 4.182 and 5.184 seconds
  respectively. The two real `TMPDIR=/tmp` copied-checkout cases passed 2/2
  under each runtime in 0.621 and 0.435 seconds, with no retained
  `scheduler-doctor-checkout.*`; the explicit owner-private test namespace
  contained only its regular, single-link mode-`0600` `.session.lock` and no
  `session.*`. Both runtimes passed `py_compile`, and frozen-range plus
  working-tree `git diff --check` passed. The owner-side Ruff E4/E7/E9/F gate
  was clean; BL had no installed Ruff executable and did not install one.
  The refreshed `sync-source-lock.json` SHA-256 is
  `6aa52db04ad49f128e68683728b7b47c022cd292b3860bf6e4147ec027ed6298`.
  The fixture behavior, source lock, and validation are complete for the
  target-branch state. Both the implementation head and its append-only
  validation commit are historical pre-squash evidence only; downstream
  generation must bind the actual squash-landed canonical `P` before producing
  `T`/`B` or propagating private and scheduler state.
- The superseding Linux runtime-candidate and stale-session recovery hardening
  is preserved as historical pre-squash evidence at signed implementation head
  `d35d535ca96ab0d105466a1ce6a8c172c722e0de`, tree
  `cbd65d7c38088b74234d1cb15fd849c68c59686e`, with sole parent
  `ff2ee79228f945ee9ee8507b8c5a518efdbe8771` and signing fingerprint
  `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. Its only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `0b9c9743cff59acfcc6f344cffc7c9ece4189b52`, with SHA-256
  `311609ecf32d9038f8192340529c357628d797a204ac4dabef5646a2b1420d74`.
  The fixture now discovers the fixed Linux `/run/user/<euid>` candidate even
  when `XDG_RUNTIME_DIR` is absent, distinguishes stable initial absence from
  fatal post-binding drift, and carries exact path, object-identity, and
  access-policy receipts across selection and use. Re-resolution, symlink,
  identity, unreadability, or policy drift after binding is fatal without
  fallback. Stale-session cleanup accepts FIFO and Unix-socket leaves only
  after current-UID plus device/inode/type revalidation immediately before
  removal, while device leaves and top-level special roots remain rejected.
  These changes resolve the current applicable findings `3709330172`,
  `3709330174`, and `3709330181`. A fresh full BL custody clone independently
  matched the branch, head, tree, sole parent, unique PR merge base, changed
  blob, and complete object closure. GitHub reported the exact signature as
  provider-valid, matching the owner-side `GOODSIG`/`VALIDSIG` fingerprint;
  the BL keybox did not contain that public key and was not modified.
  Unmodified stock `refresh-lock` refreshed all six sources and stock
  `refresh-lock --check` verified them after restoring only the Git-declared
  physical modes in the owner-private checkout. Under standard `umask 022`,
  the complete canonical source-lock suite passed 230/230 in 328.391 seconds
  with one expected platform skip through the repository's private-`TMPDIR`
  wrapper. `SchedulerDoctorFixtureTests` passed 56/56 under uv Python 3.13.13
  in 0.669 seconds and macOS system Python 3.9.6 in 0.479 seconds; the full
  scheduler-doctor suite passed 205/205 in 4.180 and 5.174 seconds
  respectively. The two real `TMPDIR=/tmp` copied-checkout cases passed 2/2
  under each runtime in 0.625 and 0.423 seconds. The explicit owner-private
  test namespace ended with only its regular, single-link, mode-`0600`
  `.session.lock` and no `session.*`, then passed zero-process/zero-FD checks
  and was removed. Owner-side Ruff E4/E7/E9/F was clean; BL had no installed
  Ruff executable and did not install one. Both runtimes passed `py_compile`,
  frozen-range and working-tree `git diff --check` passed, and project-journal
  validation passed. The refreshed `sync-source-lock.json` SHA-256 is
  `96f7380f96cfb156d92e90bf806645a7aea4fc222c50fe771b34dff8587a45fe`.
  The fixture behavior, source lock, and validation are complete for the
  target-branch state. Both the implementation head and its append-only
  source-lock/journal handoff commit are historical pre-squash evidence only;
  downstream generation must bind the actual squash-landed canonical `P`
  before producing `T`/`B` or propagating private and scheduler state.
- The Linux shared-temporary-checkout fallback is complete for the target-
  branch state. Historical pre-squash implementation evidence is signed head
  `3191ddbefd68ece1cb93ecea91fb2121500ee1eb`, tree
  `bc66257993148b1b01b5ce55414a9d4bfa8c6866`, with sole parent
  `c4a6f0247d0959eaed92c91c31fad451e05f6ebe` and signing fingerprint
  `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. Its only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `cf20cf87cf28dd1f10b53900463a290245e278b9`, with SHA-256
  `1417ceed1d926b604bc6b67b93c971a9d60ef2ac6d38171b7767cf2437552ada`.
  When `XDG_RUNTIME_DIR` is absent or unbindable and fixed
  `/run/user/<euid>` is unavailable, the fixture now selects a cleanup-owned
  current-UID Linux fallback below the exact sticky shared-temp root. Candidate
  selection, allocation, and reuse bind object identity and access policy;
  disappearance, replacement, symlink, owner, or policy drift remains fatal.
  The fixed-anchor lifecycle, bounded namespace cleanup, and cooperative-
  same-UID non-guarantee remain unchanged. This resolves the remaining current
  finding `3709758188` without requiring an explicit checkout override.
  A fresh full BL custody clone independently matched the branch, head, tree,
  parent, unique PR merge base, changed blob, complete object closure, and
  GitHub provider-valid signature. Strict full `fsck` passed with no shallow,
  promisor, alternate, bitmap, or lazy object dependency. The owner-private
  clone's `umask 077` initially exposed Git-mode drift; restoring only the 23
  tracked regular files to their Git-declared `0644` / `0755` modes changed no
  tracked bytes. Unmodified stock `refresh-lock` refreshed all six sources and
  stock `refresh-lock --check` verified them. Under standard `umask 022`, the
  complete canonical source-lock suite passed 230/230 in 330.270 seconds with
  one expected platform skip through the repository's private-`TMPDIR`
  wrapper. The first sandboxed focused run was non-counting because the outer
  Seatbelt denied the Unix-socket fixture; the unchanged direct-local run then
  passed `SchedulerDoctorFixtureTests` 66/66 under uv Python 3.13.13 in 0.993
  seconds and macOS system Python 3.9.6 in 0.695 seconds, each with one expected
  skip. The full scheduler-doctor suite passed 215/215 in 4.542 and 5.475
  seconds respectively, also with one expected skip. The two real
  `TMPDIR=/tmp` explicit/default Darwin copied-checkout cases passed 2/2 under
  both runtimes in 0.635 and 0.428 seconds; the Linux sticky-fallback copied-
  checkout regression passed 1/1 in 0.434 and 0.212 seconds. The explicit
  owner-private test namespace ended with only its regular, single-link,
  mode-`0600` `.session.lock` and no `session.*`. Both runtimes passed
  `py_compile`; BL had no installed Ruff executable and installed no
  substitute, while the owner-side Ruff E4/E7/E9/F gate was clean. The
  refreshed `sync-source-lock.json` SHA-256 is
  `6d6e27bb39f7f43eea24eea92de6f05f73c7ad7c588de602d31ce046eb922bf3`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The receipt-bound platform-parent and fixture-binder remediation is complete
  for the target-branch state. Historical pre-squash implementation evidence
  is signed head `4826113e52c08e2950604beec9e863d466bf6a4f`, tree
  `9400b7bd1fa53d99bfd32ef8878e81b9b5ccbf84`, with sole parent
  `ec90e179ef4dc0510534711fef528428d3ac278e` and expected signer fingerprint
  `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. Its only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `237c1552c35b1362f81289e3828c9c0e1fa53bde`, with SHA-256
  `f9937109f7f5e4224305672a4f56e13efd9474c3c6b59576fea8ba6e6c4fc07d`.
  Darwin ambient, `getconf`, and bounded-scan candidates now carry a bound
  path, object-identity receipt, and access-policy receipt into selection and
  use. A stable initial missing candidate remains eligible for fallback, while
  unreadability, replacement, binding drift, or access-policy drift fails
  closed. The Linux fixture adapter applies only to the exact test root below
  the receipt-bound sticky fallback; every other path still calls the
  production account-home binder. The copied-checkout subprocess also proves
  that its production private-control parent remains descriptor-bindable.
  A fresh full BL custody clone independently matched the PR branch, head,
  tree, sole parent, unique merge base, changed blob, and complete object
  closure. GitHub reported the exact signature provider-valid; the BL keybox
  lacked the signer public key and was not modified. The clone's private
  `umask` initially narrowed 23 tracked regular files to `0600` / `0700`;
  restoring only their Git-declared `0644` / `0755` physical modes changed no
  tracked bytes. Unmodified stock `refresh-lock` refreshed all six sources and
  stock `refresh-lock --check` verified them. Under standard `umask 022`, the
  complete canonical source-lock suite passed 230/230 in 369.092 seconds
  through the repository's private-`TMPDIR` wrapper. The ten exact Darwin,
  Linux, and candidate-order regressions passed 10/10 under uv Python 3.13.13
  and macOS system Python 3.9.6 in 0.013 and 0.014 seconds; the three exact
  Darwin/Linux copied-checkout regressions passed 3/3 in 0.950 and 0.638
  seconds respectively. `SchedulerDoctorFixtureTests` passed 72/72 in 1.080
  and 0.705 seconds, and the full scheduler-doctor suite passed 222/222 in
  4.564 and 5.498 seconds respectively; each fixture/full run had one expected
  platform skip. The platform test namespace ended with only its regular,
  single-link, mode-`0600` `.session.lock` and no `session.*`. Both runtimes
  passed `py_compile` for the generator, engine, source-lock tests, and
  scheduler-doctor tests. BL had no installed Ruff executable and installed no
  substitute. JSON parsing, project-journal validation, source-lock recheck,
  and staged diff checks passed. The refreshed `sync-source-lock.json` SHA-256
  is `a01ef2e2b11c614b85b49c5745c439ee68bf3a728f9b42a6a91ece7afe73f2db`.
  The implementation and append-only source-lock/journal identity remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The mount-bound stale-session cleanup substage is complete for the target-
  branch state. Historical pre-squash implementation evidence is signed head
  `b9ac6c52b1104585b9199711628d00d89d388075`, tree
  `59e4c1d68878d1a82e7b8ad771ae7b8c933c2523`, with sole parent
  `778fbd6c1360b5f02c5fb4a54caf091be9bb0c10`. GitHub reported the exact
  signature provider-valid; the BL keybox lacked the signer public key, so
  local verification remained `NO_PUBKEY` inconclusive and the user keyring
  was not modified. The only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `c62a5323d24007b5813adc4fbdd5b33efc6435fb`, with SHA-256
  `fb72d52f89e466ade6993d2eecac91068f395e7d160d810e43ec63e56a2dbe64`.
  Stale-session recursive deletion is now confined to the namespace's frozen
  mount identity: every entry binds `st_dev`, every opened directory also
  binds the production mount-identity pair, Linux `mnt_id` rejects same-device
  bind mounts, and Darwin binds `st_dev` plus `f_fsid` under the documented
  platform limitation. Planning, whole-tree revalidation, apply, and final
  namespace checks all fail closed and preserve residue when mount identity
  cannot be proved or drifts. Test-side `sys.platform` emulation continues to
  resolve mount identity through the real host kernel interface.
  A fresh full BL custody clone independently matched the PR branch, head,
  tree, sole parent, unique PR merge base, changed blob, and complete object
  closure. The repository was non-shallow and non-promisor with no alternates;
  `git rev-list --objects --missing=print` reported 232 records and zero
  missing objects, strict full `fsck` passed, and the detached worktree was
  clean. Restoring only the 23 tracked regular files to their Git-declared
  `0644` / `0755` physical modes changed no tracked bytes. Unmodified stock
  `refresh-lock` refreshed all six sources and stock `refresh-lock --check`
  verified them. Under standard `umask 022`, the complete canonical source-
  lock suite passed 230/230 in 355.078 seconds with one expected platform skip
  through the repository's private-`TMPDIR` wrapper. The six mount-focused
  regressions passed 6/6 under uv Python 3.13.13 and macOS system Python 3.9.6
  in 0.010 and 0.011 seconds. The three copied-checkout regressions passed 3/3
  in 1.059 and 0.639 seconds. The full scheduler-doctor suite passed 226/226
  in 4.661 and 5.576 seconds respectively, with one expected skip per runtime.
  The explicit owner-private test namespace ended with only its regular,
  single-link, mode-`0600` `.session.lock` and no `session.*`. Both runtimes
  passed `py_compile`; JSON parsing, frozen-range and working-tree diff checks,
  and project-journal validation passed. BL had no installed Ruff executable
  and installed no substitute; the owner-side Ruff E4/E7/E9/F gate was clean.
  The refreshed `sync-source-lock.json` SHA-256 is
  `6723edfba8e2a84caec5ac5431e8b5e0fb1bee326182dbc070e78f683a523cdf`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The identity-bound active-session cleanup substage is complete for the
  target-branch state. Historical pre-squash implementation evidence is signed
  head `e712baec0a9187739aa42990aaf8f596c4706546`, tree
  `87020c1567db20e06a4d7da4c2d46778aba98eb6`, with sole parent
  `2cc3381628d3ec2eb5ee4a0b260089c372d2c46b`. GitHub reported the exact
  signature provider-valid. The only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `bd5034639aa2b383cba3b63d5122bc77139b6240`, with SHA-256
  `7ae358f4e74c5ce15bf670d5267731d34b02aca05381b1300278ef8412473ef4`.
  Active module-session cleanup now retains namespace and session descriptors
  plus exact object-identity and mount-identity receipts through a bounded,
  descriptor-relative delete. Replacement, missing, and unreadable states are
  classified distinctly and preserve residue. Any cleanup failure installs a
  retained failure fence before another fixture session or stale sweep can
  begin, so unproved custody cannot authorize later deletion.
  A fresh full BL custody clone independently matched the PR branch, head,
  tree, sole parent, unique PR merge base, changed blob, and complete object
  closure. The source was non-shallow and non-promisor with no alternates,
  bitmap, filter, replace ref, or missing object; strict full `fsck` passed.
  Restoring only the 23 tracked regular files to their Git-declared `0644` /
  `0755` physical modes changed no tracked bytes. Unmodified stock
  `refresh-lock` refreshed all six sources and stock `refresh-lock --check`
  verified them. Under standard `umask 022`, the complete canonical source-
  lock suite passed 230/230 in 350.199 seconds with one expected platform skip
  through the repository's private-`TMPDIR` wrapper. The three replacement,
  missing, and unreadable active-session regressions passed 3/3 under uv
  Python 3.13.13 and macOS system Python 3.9.6 in 0.007 seconds per runtime.
  `SchedulerDoctorFixtureTests` passed 79/79 in 1.023 and 0.724 seconds, and
  the full scheduler-doctor suite passed 229/229 in 4.539 and 5.424 seconds;
  each fixture/full run had one expected platform skip. The five copied-
  checkout gates passed 5/5 in 0.967 and 0.647 seconds with one expected skip
  per runtime. The explicit owner-private test namespace ended with only its
  regular, single-link, mode-`0600` `.session.lock` and no `session.*`. Both
  runtimes passed `py_compile`. BL had no installed Ruff executable and did
  not install one; the owner-side Ruff E4/E7/E9/F gate was clean. The
  refreshed `sync-source-lock.json` SHA-256 is
  `f6bcc11d3a1d2aeae60f9402f344ae322b7ad299207b9f3be8416b71ddaf792c`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The receipt-bound Linux sticky-fixture adapter substage is complete for the
  target-branch state. Historical pre-squash implementation evidence is signed
  head `ae232b2dbe90f9c56f7b4c9cda7c673703e8ee0c`, tree
  `c1bccc9dbcfaedd18b65df19d43a7a6842c3b5b5`, with sole parent
  `390c956e0ba2631f0c5185e8b6c928be14771af6`. GitHub reports the exact
  signature provider-valid. The only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `1aa7b9df2eca66d6861a3e4e01fe67d238aabce4`, with SHA-256
  `bfc2fe13fb4a85e4b4414e2b9a18cc58c2fb50118ce702b3b27bfe6091089ee0`.
  Fallback-root receipt drift remains fatal, while only a strict receipt-bound
  fixture-root subtree may use the test binder. Siblings, the fallback root
  itself, non-sticky paths, and production paths continue through the
  production binder; synthetic child type or access-policy failures preserve
  their `SyncError` contract. The nested copied-checkout regression covers
  accepted mode `0755`, rejected mode `0770`, symlink rejection, primary
  private-control bindability, and primary-quarantine absence revalidation.
  A fresh full BL custody clone independently matched the PR branch, head,
  tree, sole parent, unique merge base, changed blob, provider signature, and
  complete object closure. The source is non-shallow and non-promisor with no
  alternate, bitmap, filter, or missing object; strict full `fsck` passed.
  Restoring only Git-declared `0644` / `0755` physical modes after the private
  checkout's restrictive umask changed no tracked bytes. Unmodified stock
  `refresh-lock` refreshed all six sources and stock `refresh-lock --check`
  verified them. Under standard `umask 022`, the complete canonical source-
  lock suite passed 230/230 in 335.213 seconds with one expected platform skip
  through the repository's private-`TMPDIR` wrapper. The eight Linux sticky-
  fallback focused tests passed 8/8 under uv Python 3.13.13 and macOS system
  Python 3.9.6, with one expected skip per runtime. The two real `/tmp`
  copied-checkout tests passed 2/2 under each runtime. The full scheduler-
  doctor suite passed 230/230 in 4.581 and 5.549 seconds respectively, with
  one expected skip per runtime. Both runtimes passed `py_compile`; JSON
  parsing and `git diff --check` passed. BL had no installed Ruff executable
  and installed no substitute; the owner-side Ruff E4/E7/E9/F gate was clean.
  The refreshed `sync-source-lock.json` SHA-256 is
  `e36e65e50f7fbc1134f4b2e14a9e9f2579fa05d6fdf4b678ae5984e54dc53bdb`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The sticky copied-checkout close-injection coverage substage is complete for
  the target-branch state. Historical pre-squash implementation evidence is
  signed head `b511af84ed5581fa5d9aa27e3afff22c2465acdb`, tree
  `e5d54a715123f66b5eb98bfc971ed2cfbc2dc813`, with sole parent
  `9be58a83b98f876d69624cbf7277f8c756537e57`. GitHub reports the exact
  signature provider-valid. The only implementation path is
  `tests/test_scheduler_doctor.py`, blob
  `ddb74ec2d9b22cca57158bfefdd6913a86726917`, with SHA-256
  `258dbfd725df7c912f9d2af3bda3032062befbc4b42b698539860bfabcb0b90f`.
  The Linux sticky copied-checkout matrix now runs
  `test_mirror_walkers_transfer_fd_before_effectful_close_error`; that test
  calls the preserved production account-home binder on prevalidated read-only
  `/usr`, so fixture setup cannot consume the injected effectful `os.close`
  failure before the production FD-transfer path under test.
  A fresh full BL custody clone independently matched the PR branch, head,
  tree, sole parent, unique merge base, changed blob, provider signature, and
  complete object closure. The source was non-shallow and non-promisor with no
  alternate, bitmap, filter, lazy-fetch dependency, or missing object; strict
  full `fsck` passed. A second fresh checkout created under standard
  `umask 022` supplied the counting evidence after an initial restrictive-
  umask checkout correctly exposed mode-sensitive non-counting diagnostics.
  Unmodified stock `refresh-lock` refreshed all six sources and stock
  `refresh-lock --check` verified them. Under standard `umask 022`, the
  complete canonical source-lock suite passed 230/230 in 371.423 seconds.
  The final close/sticky copied-checkout regressions passed 2/2 under uv Python
  3.13 and macOS system Python 3.9. The full scheduler-doctor suite passed
  230/230 in 4.560 and 5.521 seconds respectively, with one expected platform
  skip per runtime and real `TMPDIR=/tmp` (`/tmp -> /private/tmp`) coverage.
  No `session.*` residue remained. Both runtimes passed `py_compile`, and
  `git diff --check` passed. BL had no installed Ruff executable and installed
  no substitute; the owner-side Ruff E4/E7/E9/F gate was clean. The refreshed
  `sync-source-lock.json` SHA-256 is
  `c1d9f214f68bdcee8f57443e26b41c761fb905b21c8c312e8c11330a2681e677`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The scheduler-doctor cleanup-quarantine hardening substage is complete for
  the target-branch state. Historical pre-squash implementation evidence is
  signed head `aec928f939b1b99c368edc1796c74feaf814722c`, tree
  `398fca352b8b9e2cf2136add853964e0ae141187`, with sole parent
  `a7a442a8f9406fa623456334cf0d0850b3edf3b3`. GitHub reports the exact
  signature provider-valid. A byte-pinned `JoeyTeng.gpg` public-key snapshot
  with SHA-256
  `f133da0263f60d75b876f0d6f69d997012272250ded580877d75f19677b6f852`
  supplied a task-scoped BL keyring; `GOODSIG` and `VALIDSIG` matched signing
  fingerprint `EFBBC913F49A5F6E0AF0D248F70246143DC28F32`. The only
  implementation path is `tests/test_scheduler_doctor.py`, blob
  `94be428c99cf021f0275ea8d1dd95fa41ab939ad`, with SHA-256
  `7b3bc0c20808dc961b00537c9dfa0f5ef00ee64eba7507b17082434677a12d27`.
  A fresh full HTTPS BL custody clone independently matched PR #6 head, tree,
  sole parent, changed blob, signature, and complete object closure. The source
  was non-shallow and non-promisor with no filter or alternate; lazy fetching
  was disabled and strict full `fsck` passed all 1,040 objects. Under standard
  `umask 022`, the checkout matched its 22 Git-declared `0644` paths and one
  `0755` path. Unmodified stock `refresh-lock` refreshed all six sources and
  stock `refresh-lock --check` verified them. The complete canonical
  source-lock suite passed 230/230 in 366.854 seconds with one expected
  platform skip through the repository's private-`TMPDIR` wrapper. The 14 new
  cleanup-quarantine regressions passed 14/14 under uv Python 3.13.13 and
  macOS system Python 3.9.6 in 1.047 and 0.641 seconds. The full
  scheduler-doctor suite passed 253/253 in 10.738 and 6.201 seconds,
  respectively, with one expected platform skip per runtime. The explicit
  owner-private scheduler-doctor namespace ended with only its regular,
  single-link, mode-`0600` `.session.lock` and no `session.*`. All nine tracked
  Python files passed `py_compile` under both runtimes; JSON parsing and
  `git diff --check` passed. The refreshed `sync-source-lock.json` SHA-256 is
  `343d9fac510b9e010b54fca25dc798acb2e0d468fecb9518b1f0a0cd17f680c8`.
  The implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The superseding PR #6 merged-master source-lock checkpoint is complete for
  the target-branch state. Historical pre-squash evidence is signed head
  `cc7e932676416aa7f0f29eecafdc5a8469a96252`, tree
  `871eac3a1b891cfa2322891d816c83f318621c31`, with sole parent
  `a02fa93ff87ed27c0b7f573cd900f16db1545b31`; that signed merge checkpoint has
  tree `4d59ecbd6d5b77c388d642d41a98c08f1c82bf1c` and parents
  `85f66dbc42550a96fd10d5f2857bc9ae19e7a3de` and canonical `master`
  `867be02c2831b343501eac8c9e6ff325fae68369`. GitHub reported PR #6 open,
  unmerged, and exact-head, and reported the `cc7e932` provider signature
  valid. A fresh full HTTPS BL custody clone matched every identity and the
  changed `tests/test_scheduler_doctor.py` blob
  `67e15a00c520e039d7624e6f4510b8f3a4e3bb8d`, SHA-256
  `07a310e903a0c93645b96c803fde469d1874459a12d6e9bc243a0318664ea0f0`.
  The source was non-shallow and non-promisor with no alternate, bitmap,
  replace ref, or lazy-fetch dependency; strict full `fsck` passed all 1,060
  objects and 106 commit-graph commits. The checkout matched 23 Git-declared
  `0644` files and one `0755` file under standard physical modes. Unmodified
  stock `refresh-lock` refreshed six sources and stock `refresh-lock --check`
  verified them. The canonical source-lock suite passed 230/230 in 334.135
  seconds with one expected platform skip. The descriptor-generation close
  accounting regression passed 100 consecutive repetitions under Python
  3.13.13 and macOS system Python 3.9.6 in 0.378 and 0.526 seconds. The 14
  cleanup-quarantine regressions passed 14/14 in 1.032 and 0.641 seconds; the
  ten merged-master LaunchAgent regressions passed 10/10 in 0.147 and 0.199
  seconds. The full scheduler-doctor suite passed 261/261 in 5.905 and 6.497
  seconds, and the full personal-sync suite passed 228/228 in 11.326 and
  15.078 seconds, with one expected platform skip per full suite and runtime.
  The owner-private scheduler-doctor namespace ended with only its regular,
  single-link, mode-`0600` `.session.lock`. All nine tracked Python files
  passed `py_compile` under both runtimes; JSON parsing, `git diff --check`,
  and project-journal validation passed. The refreshed
  `sync-source-lock.json` SHA-256 is
  `cfc050e042dffd5727d8b9904adfaacbebb6c06455a03394b33f4eda1260d765`.
  These implementation and append-only source-lock/journal identities remain
  historical evidence only. Downstream generation must bind the actual
  squash-landed canonical `P` before producing `T`/`B` or propagating private
  and scheduler state.
- The persisted scheduler argv contract follow-up is complete for the local
  target-branch state. `install` now admits exactly `--repo`/`--home`,
  `install-private` admits exactly `--repo`/`--base-repo`/`--owner`/`--home`,
  and `run-scheduled` retains its public/private mode semantics across the full
  five-flag vocabulary. Every repeated flag and command-invalid flag fails
  closed, and an independent value token beginning with `-` is rejected before
  it can be misclassified as a flag value. Exact legal legacy commands remain
  reconstructable as
  `migration_needed`; legacy `install` or `install-private` commands carrying
  `--mode` report `scheduler-config-invalid`, and strict status exits `1`.
  The parser, loader, strict-status, and repository source-lock focused set
  passed 6/6 under both configured runtimes. The full scheduler-doctor suite
  passed 265/265 under macOS system Python 3.9.6 in 17.424 seconds with one
  expected platform skip. The exact CPython 3.13.0 source-lock suite passed
  230/230 in 784.876 seconds with one expected platform skip, and the final
  exact-byte CPython 3.13.0 repository discovery passed 1088/1088 in 1277.858
  seconds with three expected skips. Both configured runtimes passed
  `py_compile`; Ruff check and `git diff --check` passed. Task-private
  mode-0700 stock `refresh-lock` and `refresh-lock --check` verified all six
  locked sources without accessing retained production evidence. Separate
  production-root stock invocations independently stopped at the existing
  `legacy-recovery-pending` boundary, and no retained evidence was deleted,
  moved, or rewritten. The refreshed `sync-source-lock.json` SHA-256 is
  `129b8feb00f8d1a497eb2b5a0276009bc04350e0665bf627e76fe10ee3eb620c`.
- Signed checkpoint `b341d32d707b6ae22e83ce3c7201dee09456236c`,
  tree `fa696e861130c33ffd87d1abe16e984aa4609923`, completed the fixed-document
  operation-budget accounting repair. Its formal prior-b4ca fresh-context
  named single returned one P2: reuse of an existing small pending publication
  could replace that file with a larger payload without accounting the
  projected aggregate retained bytes, allowing a failed publication to leave
  the pending inventory above its aggregate cap.
- The superseding repair is intentionally symmetric in
  `scripts/sync_canonical_mirrors.py` and `scripts/codex_personal_sync.py`.
  After constructing the reusable payload and enforcing its single-file cap,
  both implementations compute the projected aggregate as the current pending
  logical bytes minus the reusable file's old size plus the new payload size.
  They reject an over-cap projection before the effectful reuse open. An
  inventory within the configured cap therefore cannot cross it; a historical
  pre-cap inventory already above the cap may only remain unchanged or shrink.
  Exact-cap reuse remains admissible, and an injected publication failure
  retains no more than that exact cap.
- The new projected-growth regression passed 1/1 under uv Python 3.13.0 and
  macOS system Python 3.9.6. The three-test pending-publication matrix passed
  3/3 under both runtimes, and complete
  `PrivateControlRetainedRecoveryTests` passed 43/43 under both runtimes. The
  first complete repository run exercised 1,135 tests and stopped only on the
  expected stale source-lock precondition after the engine bytes changed.
  Task-private stock `refresh-lock` and `refresh-lock --check` then refreshed
  and verified all six sources; the exact lock-current regression passed 1/1,
  and the final complete repository suite passed 1,135/1,135 in 860.248
  seconds with three expected skips. The refreshed `sync-source-lock.json`
  SHA-256 is
  `875b37794a2d0233b28d0d8df8d0acd0fa547a19f77b7d254b7a0cf289d94308`.
  The final signed pre-squash identity remains historical evidence only;
  downstream generation and deployment are handed off only after the actual
  squash-landed canonical commit is frozen as `P`.
- Signed checkpoint `e5e3622aeb10cede21c18b328024c1bc0f3fa0e7`, tree
  `3e83c91267d25946348655d42c305542b59c48fc`, carried the projected reusable-
  pending accounting repair. Its formal prior-b4ca fresh-context named single
  returned one P2: dry-run bounded only the pretty plan, while execute embeds
  that plan inside a newly pretty-serialized primary receipt. A valid plan
  near the cap could therefore reach primary-parent creation and pending-file
  allocation before the larger receipt failed its byte cap.
- The superseding repair is symmetric in `scripts/sync_canonical_mirrors.py`
  and `scripts/codex_personal_sync.py`. Both the dry-run writer and external-
  plan validator stream the complete primary-receipt shape through the exact
  pretty JSON encoder before any plan, primary-parent, or pending-inode
  publication. The reservation covers the builder-enforced maximum decimal
  width of the four late-bound `dev`, `ino`, `uid`, and `gid` integers plus the
  terminal newline; receipt type remains a regular file and mode remains
  `0400`. The post-publication exact check remains an independent defense.
  The protected property is full receipt byte capacity before any persistent
  recovery effect, not merely plan-byte capacity.
- The new regression exercises both implementations. It proves rejection one
  byte below the conservative complete-receipt bound before external-plan
  parent binding, acceptance at the exact bound, builder rejection above the
  declared late-bound integer width, and an actual self-consistent plan for
  which `plan_bytes <= cap < primary_receipt_bytes`. Execute rejects that plan
  during read/validation; mocks prove neither primary-parent nor pending-
  receipt publication is reached. The focused regression passed 1/1 and
  complete `PrivateControlRetainedRecoveryTests` passed 44/44 under uv Python
  3.13 and macOS system Python 3.9. The canonical source-lock suite passed
  275/275 in 575.269 seconds with one expected platform skip. Unmodified stock
  `refresh-lock` and `refresh-lock --check` refreshed and verified all six
  sources. The refreshed `sync-source-lock.json` SHA-256 is
  `635d9e7072498ae08eca173c637bcea8f0304a24b6caf53d606075689b27522c`.
  The final complete repository discovery passed 1,136/1,136 tests in 820.547
  seconds with three expected skips. Both configured runtimes passed
  `py_compile`; Ruff 0.16.1 E4/E7/E9/F, JSON parsing, project-journal
  validation, and `git diff --check` passed. Formal current-head review, CI,
  and squash-landed tree binding remain outstanding at this checkpoint.
- Signed checkpoint `cbe52a3f7a1fbdf864c141b2ebbac3513af685de`, tree
  `b256160731ff465e8ab4ae49e545449a54fa3c8f`, carried the complete-receipt
  capacity preflight. Its sole prior-b4ca fresh-context named single returned
  one P2: `_pc_recovery_open_or_create_primary_parent()` closed but did not
  remove its digest-named staging directory when the no-replace publication
  failed before effect. If retained evidence then changed the plan digest, the
  old staging name became unreachable and repeated failures could accumulate
  directories and consume account-home inodes.
- The current repair is symmetric in the canonical generator and standalone
  runtime. On a publication error it keeps the staging descriptor open,
  revalidates the trusted home plus the named and held staging identities and
  exact mode/uid/gid policy, proves the directory empty twice, revalidates
  immediately before a parent-FD-anchored `rmdir`, proves the name absent and
  the held object unchanged, then fsyncs and revalidates the home. The original
  publication error still terminates the attempt. Missing, replacement,
  nonempty, unreadable, removal, or durability uncertainty becomes a secondary
  cleanup failure and preserves the observable state; rename-after-effect
  never authorizes deletion of the published primary namespace.
- The dual-implementation regression proves two successive pre-effect failures
  with different plan digests leave neither staging name, while nonempty and
  replacement states remain intact and a simulated rename-after-effect retains
  the exact published identity. It passed 1/1 under uv Python 3.13 and macOS
  system Python 3.9; complete `PrivateControlRetainedRecoveryTests` passed
  45/45 under both runtimes. The private-`TMPDIR` source-lock suite passed
  276/276 in 563.237 seconds with one expected skip, and full repository
  discovery passed 1,137/1,137 in 830.495 seconds with three expected skips.
  Task-private stock refresh/check verified all six sources and removed its
  control home; the refreshed `sync-source-lock.json` SHA-256 is
  `55c8c3f02cdb196143fd383a2fcd7e6fb0912c88d04a4fbe285488a6f2effabd`.
  Both runtimes passed `py_compile`; Ruff 0.16.1 E4/E7/E9/F and
  `git diff --check` passed. A superseding signed checkpoint and its fresh
  current-head named single remain outstanding.
- Signed checkpoint `10b0ab3285d898cadb6b533335ad16d632a1dd65`, tree
  `1de01af544dcf867808143d9840543121145e2cf`, carried the bound staging-parent
  cleanup repair. Its sole prior-b4ca fresh-context named single returned two
  findings: plan/pending raw-write loops could spin on zero progress or evade
  the recovery deadline through continuous short writes, and recursive
  evidence-directory terminal path/descriptor revalidation could leak raw
  `OSError` instead of the stable recovery domain error contract.
- The target-branch repair is symmetric in the generator and standalone
  runtime. A `memoryview` write-all helper checks the recovery-local deadline
  between syscalls and rejects `written <= 0`; plan and pending callers retain
  their contextual domain errors. Final directory revalidation separately
  classifies pathname missing, other pathname lookup failure, and descriptor
  failure while preserving the existing exact object-identity and access-
  policy mismatch comparison.
- Three new fault-injection tests passed 3/3 and complete
  `PrivateControlRetainedRecoveryTests` passed 48/48 under both uv Python 3.13
  and macOS system Python 3.9. The private-`TMPDIR` source-lock suite passed
  279/279 in 565.529 seconds with one expected skip; full repository discovery
  passed 1,140/1,140 in 813.382 seconds with three expected skips. Unmodified
  stock `refresh-lock` and `refresh-lock --check` each verified all six sources.
  The locked engine SHA-256 is
  `ae87018e9e1f679c1745aaf50a93b899eacd6ce7e782891e4e8adb93178ae835` and the
  refreshed `sync-source-lock.json` SHA-256 is
  `97be8527892c73857cfd3e6c0dcc770805494829925def1c45fde666b1507b06`.
  Both runtimes passed `py_compile`; Ruff 0.16.1 E4/E7/E9/F, JSON parsing,
  project-journal validation, and `git diff --check` passed. An independent
  read-only repair sanity check reported no actionable finding.

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

- Treat the resulting landed canonical commit as `P`. Generate and validate
  toolbox PR #20 from exact `P`, freeze its reviewed head as `T`, then require
  the squash-landed toolbox commit `B` to have the same root tree as `T` and
  publish the immutable public release at exact `B`. The private overlay must
  pin `base_release.sha = B`; it consumes neither pre-landing `P` nor
  pre-landing `T` directly.
- Provision `CODEX_TOOLBOX_SYNC_TOKEN` separately only if the repository owner
  wants the sync-PR workflow to become operational.
