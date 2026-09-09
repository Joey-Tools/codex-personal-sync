---
id: 20260829-rrm001
title: Reviewer Role Regular-File Materialization
status: active
created: 2026-08-29
updated: 2026-09-09
branch: codex/daily-skill-friction-20260829-codex-personal-sync-reviewer-role-regular-install
pr:
supersedes: []
superseded_by:
---

# Reviewer Role Regular-File Materialization

## Summary

- Install Codex custom-agent TOML targets as managed regular files instead of
  symbolic links so a fresh Codex process can load the role configuration.
- Preserve the existing immutable release tree and fail-closed reconciliation
  model while allowing an already-managed symlink to migrate safely.

## Current State

- `codex-cli 0.149.1` discovers the installed `reviewer` role but rejects its
  symlinked configuration with `Too many levels of symbolic links (os error
  62)`, then reports `agent type is currently not available`.
- The current manifest and managed-state contract publish every active target
  as a symlink. The repair must protect the installed target's regular-file
  object identity, exact release-source content, and access policy across
  publication and verification.
- The first release carrying the new runtime may still be installed by the
  previous runtime. The migration therefore must remain backward-compatible
  at the manifest boundary and converge when the newly active installer runs.
- The regular-file publication must participate in the durable pending
  transaction. A post-commit copy is insufficient because a crash can otherwise
  strand a truncated or missing final target after the transaction pointer has
  already been cleared.
- Pre-commit rollback keeps all regular-file stage and evidence inodes until a
  durable rollback marker and version-2 cleanup ticket prove the exact restored
  state. The active pending pointer is cleared only after that proof is durable;
  cleanup is retryable after a crash. Existing version-1 committed-cleanup
  tickets and pending transaction versions 4 and 5 remain readable.
- Status, overlay verification, manifest removal, and overlay uninstall must
  interpret the target materialization derived from the release manifest. A
  regular target is healthy only when its content and access policy match the
  recorded release; modified or unproved files remain foreign and fail closed.
- The managed regular-file property is exact content plus access policy: mode
  `0600`, current effective UID, and one final hard link. A temporary link count
  above one is accepted only inside the bound transaction while its evidence
  inode exists; terminal validation requires one link.
- Staging now publishes a version-3 cleanup ticket and a batch-bound marker
  before creating the first hard link to a live regular target. A durable
  pending pointer supersedes that staging authority; without a pointer, the
  next lock holder removes the exact bound batch before validating final link
  counts.
- Cleanup does not treat a missing batch name as proof that cleanup completed.
  It publishes a separate durable empty-batch proof only after recursively
  clearing and identity-verifying the isolated batch root. A renamed or
  replaced batch without that proof retains its ticket and fails closed.
- Pending metadata versions 4 and 5 keep their historical agent-TOML symlink
  claim semantics. Version 6 applies the path-derived regular-file claim
  omission, so an upgrade cannot strand an older pending transaction. Version
  7 additionally persists the complete regular-target state before and after
  the transaction; uncovered version-6 terminal state fails closed instead of
  guessing that omitted records prove a healthy final file.
- Version 8 journals each exact live-leaf cleanup before the leaf is renamed
  away. The immutable receipt binds the transaction record, cleanup phase,
  target and parent identity, file identity, digest, size, mode, UID, exact link
  count, and active cleanup name. Recovery consumes both produced-file and
  restored-preimage receipts before validating the terminal target, so a crash
  after the durable rename cannot leave an unrecorded third hard link.
- Version-6 and version-7 legacy active-alias recovery precomputes the immutable
  batch alias authority and scans each distinct target parent once under a
  shared bounded budget. The cache only narrows candidate discovery: before an
  unlink, recovery still rebinds the parent and revalidates the live object,
  content, access policy, alias set, and link count; duplicate or foreign
  candidates remain fail closed.
- Terminal regular-file cleanup now uses cleanup-ticket version 4 as the last
  durable authority. It binds the complete affected target group to each
  parent and file identity, exact digest and size, mode `0600`, and effective
  UID; cleanup retires that authority only after whole-group validation. GID is
  protected only when the recorded mode grants group access, so harmless group
  churn on owner-only files cannot masquerade as an access-policy change.
- Recovery treats marker-only staging batches, retained ticket or empty-proof
  tombstones, and incomplete ticket or scan-cursor temporaries as observable
  control state rather than absence-as-success. Malformed or ambiguous
  authority fails closed. All reconciliation classes share a rotating budget
  of eight logical batches or standalone cursor-control actions per installer
  run; cursor residue consumes that same budget before ticket selection so a
  persistent deferred prefix cannot starve later terminal validation.
- Version-8 allocation is a reservation fence, not authority to adopt a later
  batch inode. A present batch therefore remains fail closed unless an exact
  version-5/version-7 cleanup authority joins it. Private use publishes a
  separate immutable version-1 `private-use-retirement` receipt before either
  joined control is retired. It binds the caller-captured moved file snapshot,
  all control-file evidence, and the complete private root/batch/leaf/metadata
  namespace; it authorizes only retirement of v5, v8, and itself.
- Recovery accepts only strict receipt temporary or retained forms, resumes
  retirement in v5 → v8 → receipt order, and revalidates the entire private
  namespace after every control-index scan. The pre-identity construction
  window deliberately remains fail closed: without a durable object identity,
  recovery never guesses that a later inode belongs to this operation.

## Next Steps

- Create a signed frozen canonical head after the current portability repair,
  then repeat the full-range fresh-context GPT-5.6 Sol Ultra review.
- Propagate the actual squash-landed canonical commit through toolbox and the
  private overlay using the receipt-bound release chain.
- Smoke-test the installed reviewer role without a path override on every
  target host.

## Evidence

- OpenAI Codex issue `openai/codex#15345` documents the same custom-agent TOML
  symlink failure and byte-for-byte regular-file workaround.
- Local CLI smoke test: `spawn_accepted=false`, exact runtime error `agent type
  is currently not available`, preceded by the role-application `ELOOP` warning.
- A fresh reviewer-role launch reproduced `agent type is currently not
  available`; a zero-inherited-context GPT-5.6 Sol Ultra fallback then found
  four blocking gaps in the first implementation: non-durable post-commit
  materialization, retained files on removal, false overlay drift, and false
  status drift.
- The final reconciliation safety file ran 312 tests in 117.392 seconds with
  no failures. The focused regular-agent suite ran 13 tests, including rollback
  pointer retention, cleanup-ticket recovery, regular update rollback, removal,
  overlay uninstall, status drift, and pending-v5/v6 compatibility.
- The first frozen whole-range review of commit `cd9ef61d` found five valid
  blockers: a pre-pointer regular-preimage hard-link crash window, incorrect
  v4/v5 agent claim projection, incomplete overlay-uninstall rollback and
  cleanup finalization, and a missing mandatory-regular ledger check in
  `status`. All five now have dedicated regression coverage.
- A focused fresh-context GPT-5.6 Sol Ultra audit of the staging repair found a
  further cleanup-authority bug: renaming a bound batch to a third name could
  make the old cleanup path delete its only ticket. The durable empty-proof
  protocol fixes that ambiguity. The same audit exposed a hard-link race after
  preimage publication; staging now requires the live link count to increase
  by exactly one instead of adopting a concurrent third link.
- The post-fix focused suites ran 25 tests covering regular materialization,
  v4/v5 claim compatibility, uninstall/status finalization, six staging crash
  and tamper windows, and exact final link counts. The reconciliation safety
  suite then passed 312/312 tests in 84.316 seconds.
- The post-fix complete discovery run exercised 1,255 tests in 974.693 seconds.
  Its only repository-local failure was an assertion that still expected the
  superseded `pending pointer` rollback label; the corrected exact test passed.
  Source-lock drift was refreshed and both the repository source-lock tests and
  `refresh-lock --check` passed. The four outer-sandbox private-home setup
  errors passed 4/4 outside the sandbox, and the core synchronizer file passed
  303 tests with one platform-condition skip.
- The synchronizer test file ran 303 tests with one platform-condition skip.
  Three compatibility regressions showed that a foreign regular `AGENTS.md`
  must still use the established optional claim-relinquishment path; the final
  guard preserves that behavior without bypassing managed regular-file digest
  and access-policy checks.
- The complete discovery run exercised 1,243 tests in 1,081.187 seconds. All
  1,239 tests permitted by the workspace sandbox passed and three platform
  cases skipped; the four tests whose setup intentionally allocates a private
  directory under the account home were blocked by the outer sandbox and then
  passed 4/4 in 0.701 seconds outside it.
- `compileall`, `py_compile`, `git diff --check`, source-lock refresh/check,
  strict status, and private overlay verification passed.
- After the first frozen whole-range review, eight additional regular-file and
  recovery findings were fixed: terminal authority lifetime, parent/file
  identity binding, non-access-bearing GID churn, marker-only staging recovery,
  materialization-aware replacement checks, public status composition,
  complete version-6 projection, and ticket/proof tombstone reconciliation.
  Follow-up audits then closed active-pointer v3 tombstones, malformed v4
  classification, bounded control reconciliation, retained temp cleanup, and
  scan-cursor fairness. The final narrow independent audit reported no
  remaining blocker.
- Final post-fix focused coverage passed 46/46 tests in 22.011 seconds. The
  reconciliation safety suite passed 312/312 tests in 87.791 seconds, and the
  core synchronizer suite passed 303 tests in 47.677 seconds with one expected
  platform-condition skip.
- Final complete discovery exercised 1,278 tests in 966.031 seconds. The only
  four errors were setup denials when the outer workspace sandbox prevented
  `test_ci_private_tmp` from allocating its intentional private directory
  under the account home; those exact four tests then passed 4/4 in 0.446
  seconds with narrow home-directory permission. Three platform cases skipped.
  Ruff `E4/E7/E9/F`, `compileall`, `git diff --check`, and the refreshed
  six-source lock and `refresh-lock --check` also passed on this final source.
- A task-local private install from the current released toolbox and private
  overlay created `agents/reviewer.toml` as a byte-identical regular file with
  mode `0600`, effective UID ownership, and link count one. An authenticated
  `codex exec` parent then loaded that regular file through a one-shot
  `agents.reviewer.config_file` override; the `reviewer` spawn was accepted and
  returned without `ELOOP`, role-application, or unavailable-role errors.
- Read-only host preflight succeeded locally. `BL-mac-mini-m4-hoteng`,
  `miku-bot-dev`, `hoteng-srv-01`, and `codex-hoteng-srv-01` timed out before
  SSH authentication; deployment will retry after the release rather than
  weakening the all-host verification requirement.
- A later frozen whole-range GPT-5.6 Sol Ultra review found fifteen actionable
  gaps across atomic regular-leaf removal, cumulative materialization and
  cleanup budgets, v4-v6 recovery compatibility, exclusive control-file
  publication, mandatory ledger coverage, complete version-7 terminal state,
  optional foreign-file relinquishment, multi-pass final verification,
  portable target aliases, semantic GID handling, status winner selection, and
  overlay verification. Each finding was repaired with focused regression
  coverage; no legacy helper entrypoint was added or reintroduced.
- Optional foreign regular `AGENTS.md` relinquishment now binds only parent and
  object identity without reading foreign contents. Verification reproduces
  that identity-only projection, while retained version-6 transactions that
  already contain full regular evidence continue to revalidate the full
  snapshot before recovery.
- The final pending-staging suite passed 26/26, reconciliation safety passed
  314/314, and the core synchronizer passed 303/303 with one expected platform
  skip. After correcting v4-v6 downgrade fixtures and refreshing the six-source
  lock, the repository-private full discovery passed 1,301/1,301 in 1,241.556
  seconds with three expected platform skips. Ruff `E4/E7/E9/F`, repository-wide
  `compileall`, `git diff --check`, source-lock canonical serialization, and
  `refresh-lock --check` also passed.
- The final pre-landing repair pass closed aggregate planning, evidence-read,
  materialization, terminal-state, and cleanup budgets; exact pending-alias
  authorization; frozen batch-root and cleanup-entry identity custody;
  publication and rollback replacement races; version-4 through version-7
  recovery compatibility; regular-file exact-noop, status, and uninstall
  authority; restrictive-umask private-file creation; and tri-state backup
  evidence. The source lock now covers all ten affected production and test
  sources.
- The first 1,342-test post-repair discovery exposed a test-process file
  descriptor leak rather than a product-state failure: an existing quarantine
  path was opened twice and the first descriptor was overwritten. The focused
  regression closes both the normal and fsync-error descriptors. Repeated core
  synchronizer measurements fell from about 745 accumulated descriptors per
  round to one near-baseline descriptor per round, and both exact scheduler
  guardian tests passed.
- The final repository-private discovery passed 1,344/1,344 tests in 954.113
  seconds with three expected platform skips. The core and reconciliation
  safety suites separately passed 627/627, pending-agent compatibility passed
  15/15, pending-staging cleanup passed 35/35, cleanup safety passed 83/83, and
  reconciliation ordering passed 22/22. Ruff `E4/E7/E9/F`, repository-wide
  `compileall`, `git diff --check`, source-lock canonical serialization, and the
  refreshed ten-source `refresh-lock --check` all passed on the same source.
- A subsequent frozen whole-range GPT-5.6 Sol Ultra review identified one
  remaining crash window: exact regular-leaf cleanup durably renamed the target
  to an active cleanup name before unlinking it, but did not persist that third
  alias. Version-8 per-record receipts now make both produced-file deletion and
  preimage-restoration cleanup recoverable. The five previously failing
  compatibility and rollback cases passed 5/5, the focused receipt recovery
  tests passed, and reconciliation safety passed 314/314 in 85.566 seconds.
- The exact post-repair repository discovery passed 1,349/1,349 tests in
  987.701 seconds with three expected platform skips. The final focused pending
  compatibility and staging-cleanup run passed 48/48; core synchronization
  passed 313/313 with one expected skip; regular materialization and overlay
  regression coverage passed 59/59. Ruff, repository-wide `compileall`, project
  journal validation, `git diff --check`, and the refreshed ten-source
  `refresh-lock --check` all passed on the same source bytes.
- The next frozen whole-range GPT-5.6 Sol Ultra review found two remaining
  recovery gaps. Retained metadata versions 6 and 7 could parse regular-file
  records but recovery routed their uncommitted produced target through the
  version-8-only receipt API. Separately, the version-8 cleanup receipt used an
  exclusive write directly at its final pathname, so a hard crash could leave
  an empty or truncated final receipt that correctly failed closed but could
  never recover automatically.
- Version-6 and version-7 deletion now retain their closed legacy exact-delete
  semantics. Recovery also recognizes a legacy active cleanup alias left by a
  second crash in either the produced-file or restored-preimage phase, but only
  after a bounded parent scan and exact parent, object, content, access-policy,
  alias-set, and link-count validation. This compatibility authority applies to
  both versions because the upgraded recovery code itself can expose either
  retained version to the legacy rename-before-unlink window; foreign or
  duplicate candidates remain fail closed.
- Version-8 receipts are now written and synchronized at a temporary pathname
  and published with a no-replace atomic rename. The batch cleanup allowlist
  accepts only the canonical receipt temporary names and their exact retained
  forms, allowing truncated temporary residue to be discarded and retried
  without treating a partial final receipt as valid authority.
- New regressions cover version 6 and 7 across uncommitted create and replace,
  produced active-alias recovery, restored-preimage active-alias recovery with
  a different produced target still present, foreign replacement, truncated
  receipt temporary recovery, and the atomic-rename boundary. The complete
  regular-materialization module passed 45/45 in 22.145 seconds and pending
  compatibility passed 15/15 in 6.329 seconds. Ruff, focused `compileall`, and
  `git diff --check` passed, and a fresh-context GPT-5.6 Sol Ultra narrow review
  of the two-file repair reported no findings.
- The exact post-repair full repository discovery passed 1,355/1,355 tests in
  1,151.232 seconds with three expected platform skips. It ran once with the
  narrow account-private temporary-directory permission required by four
  isolation tests; no sandbox-only failed precursor was counted as product
  evidence.
- A fresh-context Codex GPT-5.6 Sol xhigh narrow review of the final legacy
  recovery boundedness repair reported no findings. The current main process
  then reran regular materialization 48/48 in 20.155 seconds and pending-agent
  compatibility 15/15 in 4.622 seconds; Ruff `E4/E7/E9/F`, repository-wide
  `compileall`, `git diff --check`, and the refreshed ten-source lock check
  passed. The final repository-wide discovery remains required for this new
  frozen head.
- The first post-checkpoint discovery ran 1,358 tests in 1,094.464 seconds and
  exposed one test-fixture omission: a `Mock` pending batch did not initialize
  the new in-memory alias-index field, so attribute auto-creation masqueraded
  as an invalid cache. The fixture now explicitly supplies `None`.
- The corrective full discovery passed 1,358/1,358 tests in 1,031.112 seconds
  with three expected platform skips. Its exact hard-link rejection regression
  passed, and no test error remained on the resulting code head.
- A subsequent frozen GPT-5.6 Sol Ultra whole-range review found that three
  destructive regular-file revalidation sites still compared every snapshot
  field, including GID for owner-only mode `0600`. That made a harmless GID
  change abort a planned replace, removal, or rollback even though it did not
  alter the protected object identity, content, or access policy. Those sites
  now use the existing semantic snapshot predicates: GID remains mandatory
  when any group permission bit is present, while owner-only files tolerate
  only that non-access-policy drift. New replace, removal, rollback, and
  group-access rejection regressions cover the distinction. The focused
  regular-materialization module passed 53/53 in 23.052 seconds; `py_compile`,
  Ruff, `git diff --check`, and the refreshed ten-source lock check passed.
  Because production and test bytes changed after the prior discovery, another
  full discovery and a new fresh whole-range review remain required before PR
  creation.
- Signed checkpoint `70f859070e5b56d908946790da70aa6d349d5361` carries that
  repair, the refreshed lock, and this journal. Its GPG signature verifies as
  Joey Teng's configured EdDSA key. The repository-private full CI discovery
  then passed 1,363/1,363 in 956.189 seconds with three expected skips. The
  same exact code passed repository-wide `compileall`, Ruff,
  `refresh-lock --check`, `git diff --check`, and project-journal validation.
  The remaining gate is a new independent clean-clone whole-range review; this
  journal-only checkpoint does not expand the implementation test surface.
- The next independent GPT-5.6 Sol Ultra whole-range review found a genuine
  version-6 recovery compatibility defect and several places where owner-only
  (`0600`) regular-file GID churn was incorrectly treated as a protected
  mutation. The repair deliberately uses a semantic comparison: object
  identity, content, mode, UID, size, link count, and parent binding remain
  strict; GID is exact only when group permission bits make it an access-policy
  signal. Historical version-6 producing regular records must carry their
  observed integer GID, while version-7/8 records must carry `null`; dedicated
  committed and uncommitted create, replace, and removal recovery fixtures
  cover those boundaries. The architecture contract now documents the narrow
  `agents/*.toml` regular-file exception and this GID rule. The final local
  full discovery passed 1,374/1,374 tests in 936.453 seconds with three
  expected skips; repository-wide `compileall`, Ruff, `git diff --check`, and
  the refreshed ten-source lock check passed on the same source. A new signed
  frozen head and clean-clone whole-range review remain required before PR
  creation.
- The following independent GPT-5.6 Sol Ultra whole-range review found three
  additional recovery defects before any remote publication: version-6 foreign
  regular relinquishment still used dataclass equality for its complete
  `planned_before` snapshot; several `0600` control authorities and cleanup
  tickets did the same; and a failed ordinary-file move could restore a
  same-inode leaf after its content, mode, UID, or link count changed. The
  repairs apply the same access-policy semantic predicate consistently to
  snapshots and tickets, retain strict identity/content/mode/UID/size/link
  count/parent checks, and restore a failed moved leaf only after a full
  regular-file revalidation. A changed or unreadable leaf is retained as
  destination evidence instead. New regressions reproduce an actual v6
  foreign-regular metadata shape, ticket/cursor GID-only churn, and same-inode
  content/mode/link-count restore-window races. Focused recovery, staging, and
  atomic-move suites passed; a fresh full discovery, source-lock refresh, signed
  checkpoint, and new clean-clone whole-range review remain mandatory.
- A first full discovery after that repair exposed one incomplete unit-test
  fixture: the terminal-budget test constructed a ticket snapshot without the
  complete file evidence that production parsing always supplies. The fixture
  now represents a real `0600` ticket snapshot. Its targeted compatibility
  class passed, then the final repository-private discovery passed
  1,381/1,381 tests in 997.782 seconds with three expected platform skips.
  The source lock, static checks, signed checkpoint, and a new clean-clone
  whole-range review remain the required next gates.
- The next independent GPT-5.6 Sol Ultra whole-range review found six real
  blockers before remote publication. Historical version-6 writers had emitted
  both integer and `null` `regular_gid` values; version 9 now persists
  managed-state UID/GID evidence, accepts only the real closed legacy shapes,
  and binds UID always while binding GID only when group permission bits give
  it access-policy meaning. Legacy state evidence without a group-bearing GID
  fails closed rather than being silently upgraded.
- The same audit tightened destructive recovery. Failed hard-link publication
  retains the destination when no exact source alias can be re-proven; changed
  regular evidence is retained outside the active agent path instead of being
  renamed back into it; failed moves rebind the canonical parent and never move
  a newly inserted racer; and all cleanup ticket, staging-marker, and
  empty-proof adoption paths require the effective UID. New race and
  compatibility coverage passed 465/465 focused tests in 132.856 seconds.
  A first 1,393-test discovery then had no product regression: four
  account-home temporary-directory tests were denied by the outer sandbox and
  the source-lock test correctly observed pending source changes. Refresh the
  lock, rerun that suite with its narrowly required private-home permission,
  then create a new signed frozen head and a clean-clone whole-range review.
- The next fresh GPT-5.6 Sol Ultra whole-range review found eight concrete
  remaining recovery/authority defects, all repaired before remote publication.
  Failed-move rollback now never mutates through a detached destination parent
  FD and first isolates a destination leaf before any restore; a replacement
  racer therefore cannot enter an active `agents/*.toml` path. Once a hard
  link is published, every later failure retains its destination evidence, and
  canonical TOML cleanup isolates before snapshot or journal decisions so an
  unreadable or mismatched leaf is never left loadable. State-before recovery
  binds UID and semantic GID before and after linking; commit/rollback markers,
  orphan proofs, staging skeletons, and staging-marker temporaries require the
  effective UID and their expected access policy before they can become cleanup
  authority. The implementation treats identity, content stability, and access
  policy as distinct protected properties: group identity is compared only
  when group permission bits make it meaningful.
- The eight repairs have explicit interleaving regressions for detached parents,
  destination racers, post-link last-alias loss, snapshot failure, pending
  cleanup mismatch, UID/GID drift, wrong-owner markers/proofs, and foreign
  staging skeletons. The merged changed-module run passed 770 tests with one
  expected skip. The first 1,405-test discovery ran all implementation tests
  successfully and reported only the expected stale source-lock hash; after
  refreshing the ten-source lock, `refresh-lock --check` passed and the
  source-lock module passed 280/280 with one expected skip in 514.753 seconds.
  A new signed frozen head and another clean-clone GPT-5.6 Sol Ultra whole-range
  review remain required before PR creation.
- A new frozen-head review then exposed one P1 and eight P2 recovery and
  authority gaps. The follow-up preserves a recovery alias through v4 terminal
  validation and records that validation durably before deletion; binds GID when
  either group/other permissions or setgid make it an access-policy signal;
  validates the complete regular target-parent chain and leaf ACL both before
  and after reads; and makes pending cleanup control objects Darwin-ACL-aware.
  It also converts post-link retained staging into durable manual retention,
  writes prepared v8/v9 cleanup receipts before moving canonical names, records
  failed-move isolation in a fixed durable control namespace, and restores
  regular-remove replacement dependencies. These changes protect object
  identity, content stability, and access policy separately while allowing
  safe directory link-count/read-execute churn.
- During full validation, two compatibility regressions were repaired without
  weakening the security contract: a failed `fchmod` now triggers exact-object
  cleanup once `O_EXCL` has created the file, and release-retention cleanup
  distinguishes ordinary release-tree files from `0600` pending authorities.
  The latter still requires exact-FD identity, current UID, no group/world
  write, and Darwin ACL admission; it merely accepts safe ordinary file modes
  such as `0644`. Static checks passed, the expanded focused group passed
  839 tests with one expected skip, full discovery passed all implementation
  tests (1,427 with three expected skips) except the expected stale source lock,
  then the refreshed ten-source lock verified and its dedicated suite passed
  280/280 with one expected skip in 500.510 seconds. A signed checkpoint and
  another clean-clone GPT-5.6 Sol Ultra whole-range review remain required
  before PR creation.
- The final local repair round tightened three boundaries identified during
  independent review. Failed-move recovery now rebinds both restore parents
  immediately before its rename and rechecks the complete source/destination/
  isolation alias group before retiring the durable receipt. A destructive
  regular-file move revalidates the complete managed parent access policy at
  its final mutation boundary, and overlay verification applies the same
  parent-chain admission to regular targets.
- Pending cleanup separates authority from ordinary content. Control
  directories and files remain exact current-user `0700`/`0600` objects;
  historical `links/**` evidence and release content may use safe normal modes
  such as `0755` or `0644`, but still require exact-FD identity, current UID,
  no group/world write, and Darwin ACL admission. A typed active cleanup token
  records only a top-level `links` isolation, so a crash after that rename can
  restore the content policy on restart without inferring it from a generic
  internal name. Old untyped active tokens remain fail closed rather than being
  guessed to be `links` content.
- The corresponding race, recovery, and fixture coverage passed in the merged
  focused group (514 tests in 186.667 seconds). After refreshing the ten-source
  lock, the final repository discovery passed 1,440/1,440 tests in 987.912
  seconds with three expected platform skips. It ran with the narrow
  account-private temporary-directory permission needed by the four CI private
  temp tests; their earlier outer-sandbox setup denials were verified 4/4 as
  environment-only. `refresh-lock --check`, Ruff lint, repository-wide
  `compileall`, and `git diff --check` passed on the same source bytes.
- The downstream consumer's fresh review found one compatibility regression
  before it could publish the generated mirror: `zip(..., strict=True)` in the
  pending regular-target validation path requires Python 3.10, while this
  runtime supports Python 3.9. The preceding exact target-tuple comparison
  already proves equal length and order, so the canonical repair uses ordinary
  `zip` without weakening that authority check. The macOS Python 3.9 CI matrix
  now directly executes the affected validation test. Locally, the targeted
  compatibility test passed, the CI-workflow tests passed with their narrow
  private-home permission, and `actionlint`, Ruff, repository-wide
  `compileall`, and `git diff --check` passed. The same affected test also
  passed under the local Xcode Python 3.9.6 interpreter; the new GitHub matrix
  lane keeps that runtime coverage durable.
- Pending metadata version 10 now records both the prior materialization and
  the exact removed-link key for a same-target regular-to-symlink migration.
  Recovery derives removal authority only from a receipt-bound release tree:
  an owner that remains in the after state uses only its after-state release,
  while a departed owner uses only its before-state release. Versions 4 through
  9 retain their closed legacy schemas; receiptless active-alias recovery is
  limited to versions 6 through 9 and revalidates the complete managed parent
  chain and exact leaf immediately before deletion.
- The pre-PR gate on 2026-09-05 passed 1,453 tests in 973.647 seconds with
  three expected skips. The same source passed source-lock refresh/check, Ruff
  lint and formatting, Python 3.9 `compileall`, `actionlint`, and
  `git diff --check`; the two macOS Python 3.9 CI-targeted pending-regular
  compatibility tests passed locally. The new cleanup path preserves a failed
  exclusive-file creation error where `add_note` exists and uses a chained,
  fail-closed `SyncError` on Python 3.9 when final-name cleanup fails.
- The first fresh frozen-range GPT-5.6 Sol Ultra review of the reconstructed
  signed candidate found two final-boundary cleanup races: a public parent
  access-policy check was incomplete immediately before a legacy cleanup
  mutation, and a final public-path unlink could delete a name replacement.
  The repair distinguishes protected leaf identity, content stability, and
  access policy from harmless child-entry churn. It revalidates the complete
  managed parent chain before a public-to-private rename, validates the exact
  leaf inside a mode-`0700` private cleanup namespace, and only then deletes
  the private alias. A replacement is first atomically removed from the
  Codex-loadable canonical name; it is then retained as private quarantine
  evidence whenever it cannot re-prove the authorized publication identity,
  content, and access policy.
- Receiptless compatibility cleanup first atomically moves the observed
  canonical leaf to a generated non-TOML active alias. A regular leaf is then
  moved into private quarantine for exact validation; a non-regular or
  unreadable replacement remains fail-closed under the non-loadable retained
  alias. New interleavings cover an already-replaced canonical leaf, a private
  authority retained when the public target reappears, and a replacement that
  appears while the private alias is being deleted. None of those paths unlinks
  through the public target name.
- The final affected six-module suite passed 848 tests with one expected skip
  in 211.260 seconds. Final repository-private discovery passed 1,459 tests
  with three expected skips in 939.908 seconds, and the actual local Python
  3.9.6 regular-materialization suite passed 104/104 in 51.347 seconds. Ruff
  `E4/E7/E9/F`, Ruff formatting, `actionlint`, `git diff --check`, and a
  Python 3.9 compile check passed; the ten-source lock was refreshed after the
  final source and test changes.
- A subsequent whole-range fresh reviewer exceeded its fixed 30-minute bound
  without a final result, so the range was not treated as clean. Its bounded
  trace led to two independently reproduced repairs: receiptless successful
  publication cleanup had retained an otherwise empty private quarantine batch
  that could consume the bounded batch capacity, and durable receipt cleanup
  lacked a canonical-name recheck immediately around private-alias deletion.
  The new fallback-only reclamation keeps exact batch, leaf, metadata identity,
  content, and access-policy bindings; it removes only a fully empty private
  scaffold and leaves every unexpected child or replacement intact. Durable
  cleanup now checks the relevant public aliases both before and after private
  deletion, while legacy active-alias recovery keeps its valid restored
  preimage behavior. Focused validation passed 108 regular-materialization
  tests and 324 reconciliation-safety tests. A new final full suite and a
  short, fresh final reviewer still remain required before PR creation.
- A follow-up independent audit found three further recovery-boundary issues
  before the final gate. The repair keeps the v6/v7 before-phase exception
  narrow: an exact canonical preimage restored after the legacy private alias
  has passed its initial absence check is not treated as a new canonical race,
  while current durable receipt paths still recheck both canonical and active
  names. A private alias that reappears after unlink is now re-snapshotted for
  object identity, content, access policy, link count, and internal-name plan
  before any attempted public restoration; a mismatch remains private retained
  evidence. Finally, an in-process fallback batch is rebound as soon as its
  metadata is proven; a later empty leaf-setup failure closes the captured
  descriptor and reclaims only the exact metadata-only scaffold. An unreadable
  or changed metadata file remains fail-closed evidence rather than being
  guessed safe to remove. Post-repair validation passed 111 regular-
  materialization tests in 47.618 seconds and 324 reconciliation-safety tests
  in 84.892 seconds. The final repository-private discovery then passed 1,466
  tests with three expected skips in 945.508 seconds. A fresh exact-head review
  remains pending.
- A new frozen-range fresh review found and the follow-up repair closed the
  remaining range-introduced boundaries. Creation cleanup now evacuates a
  newly created TOML through a non-loadable name even when the final parent
  access-policy check fails, so it cannot remain active in a newly unsafe
  parent. Produced publication recovery accepts a public canonical name only
  when it is the exact receipt-bound before preimage; every create, before,
  foreign, or post-boundary replacement remains fail-closed. Receiptless
  cleanup now publishes a version-5 `ephemeral-quarantine` durable ticket
  before it unlinks private payload, and its recovery consumes only an exact
  empty scaffold through the existing ticket, tombstone, and empty-proof
  protocol. It never treats a nonempty leaf, foreign sibling, replacement, or
  dual batch root as deletion authority. The parser also caches and budgets
  v10 regular-to-symlink source evidence by its release-bound identity, so
  repeated targets cannot amplify locked recovery reads. macOS Python 3.9 CI
  now runs the exact v10 migration and recovery paths. The integrated affected
  suites passed 522 tests in 160.205 seconds; the Xcode Python 3.9 pair passed
  in 2.435 seconds and its compile check passed. The final repository-private
  discovery passed 1,480 tests with three expected skips in 955.481 seconds.
  Ruff, formatting, `actionlint`, and `git diff --check` passed, and the
  ten-source lock was refreshed. A new final exact-head review remains pending.
- The preceding version-5 scaffold description was superseded during the final
  repair by a version-6 receiptless leaf ticket. It publishes a durable ticket
  before any public or private leaf mutation, then publishes an immutable
  `ephemeral-private-isolated` phase receipt after the exact authority inode
  reaches private evidence and before that inode can be unlinked. The receipt
  binds the ticket identity and digest, payload identity, public parent, target,
  and quarantine root. This distinguishes an initial foreign object, which must
  be isolated from the loadable TOML path and retained with its identity, from a
  later canonical or alias reappearance, which is outside cleanup authority and
  is retained in place. The receipt remains durable across private unlink and
  ticket deletion; orphan recovery removes it only after revalidating bound
  parents and proving every ticket-derived public and private name absent.
  Thus object identity, content stability, link count, and access policy are
  protected without treating benign directory child-entry churn as mutation.
  The final affected five-module regression suite passed 536 tests in 184.901
  seconds, after the phase repair; source lock refresh succeeded for all ten
  sources. Full discovery and fresh exact-head review remain pending.
- A final repository-private discovery subsequently passed 1,494 tests with
  three expected skips in 948.300 seconds. It ran under a 30-minute
  process-group deadline with a 128 MiB retained-log ceiling; the resulting
  35 KiB log recorded no timeout or output-budget event. The prior unbounded
  run's four CI-private-temp errors were sandbox write denials for deliberately
  private home-directory fixtures, not product failures; the bounded local
  run permitted those fixtures and passed. A stale v6 evidence-layout assertion
  in the broader legacy suite was also aligned with the direct private evidence
  path, while the publication error now retains both the `private isolation`
  and retained-evidence diagnostic context. Ruff `E4/E7/E9/F`, Ruff format,
  `actionlint`, Python compilation, and `git diff --check` passed after the
  final source-lock refresh. A fresh exact-head review remains pending.
- A subsequent fresh frozen-range review of `c87d6c4..04044ec` found two
  additional pre-PR issues and they were repaired before opening a PR. First,
  modern pending-publication cleanup now advances from a public-authorized
  journal state to private authority through an independent exclusive,
  identity-bound anchor before private unlink. The anchor makes that lifecycle
  monotonic across crashes: a later public canonical or active-alias
  reappearance is retained in place, a failed private unlink remains private
  for retry, and replaying an earlier valid v2 journal cannot recreate public
  deletion authority. Old v1 journals that cannot prove their lifecycle remain
  fail-closed. Second, v10 pending parsing now caches immutable release
  receipts by owner, release SHA, directory identity, and tree digest. It
  captures and verifies a release once, revalidates every cached release before
  returning cleanup authority, then seals the budget; cache hits still verify
  source and parent-chain evidence but do not repeat a full release-tree walk.
  This removes the proven repeated-tree recovery amplification without using a
  mutable manifest as authority. A follow-up integration review caught and
  closed the private-unlink retry, v2 journal replay, and malformed-version
  boundaries. Its second pass returned clean. The affected cross-module suite
  passed 864 tests with one expected skip in 229.273 seconds; Ruff lint and
  formatting, `actionlint`, Python compilation, `git diff --check`, and
  source-lock regeneration passed. A new full discovery and fresh exact-head
  review remain required before PR creation.
- The required post-repair full repository-private discovery completed under
  the same 30-minute process-group deadline and 128 MiB log ceiling: 1,505
  tests passed with three expected skips in 967.447 seconds. The retained log
  reported no deadline or output-budget event. This test result covers the
  monotonic private-authority anchor, v2-replay rejection, malformed journal
  versions, release-receipt cache finalization, and the revised cache-hit
  compatibility contract. The next gate is a fresh exact-head whole-range
  review after a signed frozen commit; no PR has been created yet.
- A final allocation/reclaim repair introduces a durable v8 allocation fence
  before any quarantine batch, metadata, or leaf mutation. The fence reserves
  capacity and blocks unsafe recovery, but is never deletion authority: only a
  separately exact v5/v7 cleanup ticket may remove a bound entity. Legacy v5
  ticket bytes retain their historical schema without `metadata.size`; complete
  v5 temporary tickets are promoted only through full canonical validation.
  Classification, promotion, duplicate-canonical handling, isolation, and
  unlink now carry exact snapshots for object identity, content stability, and
  access policy. A malformed captured object can be retired only when that
  same object remains bound at the destructive edge; any replacement, vanished
  name, I/O failure, or policy uncertainty preserves durable evidence and
  blocks further mutation.
- Three independent fresh GPT-5.6 Sol Ultra boundary reviews drove and then
  rechecked this repair. They covered legacy v5 compatibility, temporary-ticket
  promotion, same-inode callback rewrites, scanner classification-to-discard
  replacement, and duplicate-canonical revalidation. The final focused review
  returned PASS. The post-repair cross-module suite passed 921 tests with one
  expected skip in 229.300 seconds; complete discovery passed 1,562 tests with
  three expected skips in 960.578 seconds under the 30-minute process-group
  deadline and 128 MiB log ceiling. Ruff `E4/E7/E9/F`, Ruff formatting,
  `actionlint`, Python compilation, `git diff --check`, and the refreshed
  ten-source lock check passed. A signed frozen exact-head whole-range review
  remains required before PR creation.
- The ensuing frozen whole-range review identified two final lifecycle TOCTOU
  boundaries. A legacy v2 journal is no longer overwritten in place after its
  validation: the already-exclusive v3 private-authority anchor is the
  monotonic authority transition, while the legacy journal keeps its exact
  inode, content, and policy. Before private publication cleanup can unlink an
  alias, it now moves the exact record-bound object with no-replace semantics
  to a random private tombstone, fsyncs, and revalidates object identity,
  content stability, access policy, link count, bound parents, journal, anchor,
  and public authority. Recovery accepts exactly one canonical alias or one
  valid tombstone; replacement, absence, or ambiguous tombstones remain
  preserved fail-closed evidence.
- A new fresh GPT-5.6 Sol Ultra review of the anchor/tombstone repair returned
  PASS. The associated cross-module suite passed 924 tests with one expected
  skip in 241.092 seconds. Complete discovery passed 1,565 tests with three
  expected skips in 969.527 seconds under the same 30-minute process-group
  deadline and 128 MiB log ceiling. The ten-source lock was refreshed before
  that run. A new signed exact-head whole-range review remains required before
  PR creation.
- The PR #18 provider follow-up keeps allocation-owned root, batch, leaf, and
  metadata descriptors live through empty-batch cleanup, so reopening names
  cannot recreate deletion authority through inode reuse. It also makes
  replacement and fault-injection tests portable without assuming inode
  monotonicity, and locks `test_quarantine_empty_batch_reclaim.py` into both
  sides of the eleven-source toolbox mirror. The focused reclaim module passed
  51 tests; source-lock tests passed 2 tests; `refresh-lock --check` verified
  all 11 sources. Complete discovery passed 1,567 tests with three expected
  skips in 980.313 seconds, and the affected cross-module suite passed 926
  tests with one expected skip in 253.504 seconds. Both runs used bounded
  process-group deadlines and retained logs. A signed frozen exact-head
  whole-range review remains required before the provider findings are
  resolved.
- A later frozen review found that cleanup-race assertions only modeled the
  Python 3.11 `BaseException.add_note` path. The tests now separately verify
  the Python 3.9 combined-error fallback and the CI 3.9 lane runs the complete
  quarantine reclaim module plus the two affected pending-publication races.
  The same review proposed automatically completing an old v1 public cleanup
  journal after a pre-unlink crash. An independent GPT-5.6 Sol Ultra
  adjudication rejected that proposal: a v1 journal retained through batch
  finalization cannot distinguish the genuine crash from a later exact
  hard-link replay from retained evidence. Public v1 candidates therefore
  remain fail closed; only an already-bound private alias may advance through
  the existing private-authority anchor.
- The next frozen audit closed four final mutation-boundary findings without
  weakening that fail-closed contract. Allocation close now detaches all four
  owned descriptors before the first close syscall and closes the saved values
  best-effort before rethrowing the first `BaseException`; a reused descriptor
  can therefore never be closed by a retry or finalizer. v5/v7 canonical batch
  isolation rechecks the bound quarantine root access policy immediately before
  its rename. v6 private payload cleanup moves the exact object to a random
  final-private tombstone, binds an open descriptor through the unlink, and
  rechecks identity, content, access policy, link count, parent bindings,
  ticket, and phase receipt. Terminal receipt deletion in both the main and
  orphan paths now revalidates the public/private namespace and every
  recoverable ticket representation at its mutation boundary.
- The associated regressions cover descriptor-number reuse after a post-close
  `BaseException`, v5/v7 mode and Darwin ACL drift, final-private replacement
  and restart recovery, phase-receipt replacement, public replay after ticket
  deletion, retained ticket forms, and orphan receipt deletion. Local
  `/usr/bin/python3` 3.9.6 passed the exact CI selections (56 tests), the
  regular-to-symlink compatibility selections (2 tests), and the complete
  pending-staging module (81 tests). The two cleanup modules passed 133 tests
  on the default interpreter; source-lock tests, lock regeneration/check,
  Ruff lint/format, `actionlint`, Python compilation, and `git diff --check`
  also passed. A newly frozen signed exact-head whole-range review and full
  discovery remain required before pushing the repaired PR head.
- That discovery subsequently exposed one receiptless-cleanup regression:
  the new exact final-private tombstone changed a retained sibling from
  `<evidence>-retained-*` to `<evidence>.delete-<token>-retained-*`, which the
  tombstone parser mistakenly treated as unrelated child churn. It could then
  retire the ticket and terminal receipt despite preserved residue. The shared
  private inventory now classifies only an exact 32-character tombstone as a
  candidate deletion object; related malformed or retained derivatives are
  retained evidence, including when they are hard links to the expected inode.
  This leaves unrelated quarantine-root churn unaffected while preserving the
  exact-name authority boundary through normal cleanup, restart recovery, and
  terminal receipt revalidation. The original sibling race, a same-inode
  derivative, exact tombstone recovery, and the complete 133-test regular
  materialization module passed locally. A new full discovery and frozen review
  are required for the amended head.
- The amended signed head passed the complete repository-private discovery:
  1,577 tests passed with three expected skips in 975.408 seconds, under the
  retained 30-minute process-group deadline and 128 MiB log ceiling. The
  affected cross-module suite then passed 936 tests with one expected skip in
  243.090 seconds. The remaining pre-push gate is one fresh isolated
  whole-range review of this exact final head; the journal-only evidence update
  does not require another complete discovery run.
- A subsequent frozen review found one final-private deletion TOCTOU: the
  regular-publication path released its leaf descriptor after the last snapshot,
  then performed broad parent-policy checks and unlinked the tombstone by name.
  A replacement in that interval could therefore be deleted without exact
  authority. The repair keeps an `O_NOFOLLOW` descriptor for the exact
  tombstone open through journal, public-name, and parent-policy revalidation,
  then reproves descriptor identity, content, access policy, link count, name
  binding, and parent binding immediately before unlink. Regular replacement,
  symlink replacement, same-inode content drift, and same-inode mode drift all
  remain as fail-closed evidence. The complete regular-materialization module
  passed 134 tests, the eleven-source lock was refreshed and checked, and the
  complete repository-private discovery passed 1,578 tests with two expected
  skips in 1,072.844 seconds. Ruff lint/format, Python compilation, and
  `git diff --check` passed. The attempted preceding CLI review was interrupted
  by provider capacity and is not counted as a pass; a new fresh exact-head
  whole-range review is required before remote publication.
- The next fresh review found a separate v6 terminal-control gap: strict
  retained-ticket parsing ignored a suffix-added tombstone derivative, so a
  same-inode `<ticket-tombstone>.extra` could retain valid ticket bytes while
  terminal receipt deletion and a later mutation gate treated the ticket as
  absent. A new blocking-only classifier recognizes a valid batch's ticket and
  ticket-temp derivatives as retained evidence but cannot recover, delete, or
  otherwise grant authority to them; exact canonical and strict retained forms
  keep their existing recovery paths. The regression creates a same-inode
  suffixed tombstone after ticket deletion, proves that the terminal receipt
  remains, then simulates its unsafe retirement and proves the independent
  mutation gate still blocks. Adjacent terminal/orphan regressions passed, the
  eleven-source lock was refreshed and checked, Ruff and Python compilation
  passed, and complete repository-private discovery passed 1,579 tests with
  two expected skips in 1,076.901 seconds. A new frozen exact-head whole-range
  review remains required before remote publication.
- The following fresh frozen review found two related P2 boundaries in that
  blocking-only classifier. First, v5/v7 cleanup could delete its canonical
  ticket and then retire the exact `.empty-proof` while a retained or
  suffix-added ticket representation remained. The proof now revalidates the
  representation absence on the same bound index descriptor before its
  canonical-to-retained rename and again before its retained unlink; malformed
  names remain blocking evidence only and never become restoration or deletion
  authority. Second, a malformed representation could be invisible to status
  and dry-run planning while the real mutation gate rejected it. A shared
  unresolved-representation classifier now rejects every related noncanonical
  form before planning, reports it as managed-state drift in ordinary status,
  and prevents install and overlay-uninstall dry runs from claiming that it can
  be auto-cleaned. The ready-state observer scans the entire control namespace
  and raises the same error if residue appears after an earlier preflight scan,
  so an ordinary ready marker cannot downgrade a late ambiguity to a
  `would clean` plan.
- New regressions cover v5 retained and v7 suffixed evidence after canonical
  ticket deletion, the proof's final-unlink race, malformed direct and
  retained forms in status/install/uninstall dry-run, and a late-residue race
  coexisting with an ordinary ready marker. The complete discovery initially
  exposed three test-harness compatibility errors from the new preflight and
  callback: a cursor-FD fault injection now explicitly isolates the new gate,
  and an existing empty-proof mock accepts and forwards the new revalidator.
  The final focused selection passed 9 tests in 6.210 seconds. The 11-source
  lock was refreshed and checked; Ruff lint, Python compilation, and
  `git diff --check` passed. The final repository-private discovery passed
  1,583 tests with two expected skips in 1,125.110 seconds. A new signed
  frozen exact-head whole-range review remains required before remote
  publication.
- That review found two final repairable boundaries. Orphan empty-proof cleanup
  now uses the same bound index descriptor to reject every recovered ticket
  representation before proof isolation and again at each generic rename and
  final-unlink boundary; canonical, strict retained, temp, and suffix-added
  ticket forms therefore preserve the proof as blocking evidence rather than
  losing the only durable empty-batch record. The v6 receiptless leaf-cleanup
  path now owns both directory descriptors inside one `try`/`finally`, so a
  `BaseException` while opening the quarantine root closes the already-open
  public-parent descriptor without performing a mutation. New regressions
  replay the exact ticket inode during the orphan proof's final unlink for both
  canonical and retained representations, and inject a second-open
  `SystemExit` while asserting descriptor release. The seven affected tests
  passed in 4.325 seconds; source lock refresh/check verified 11 sources,
  Ruff and syntax validation passed, and the repository-private complete
  discovery passed 1,585 tests with two expected skips in 1,091.404 seconds.
  A signed amended head and a wholly new frozen exact-head review are required
  before remote publication.
- The next frozen review identified three related fail-closed gaps. A v8
  allocation fence could bind planned full metadata while a direct partial
  write remained after a hard crash; metadata is now staged and fsynced under
  the private index before the fence and batch enter the recoverable namespace.
  The v8 dry-run classifier now distinguishes an exact v5/v7 joined scaffold
  from an unowned or conflicting entity, so it cannot promise cleanup that the
  mutating path would reject. A malformed v3 regular-publication `phase` is
  rejected as managed-state drift before path-derived alias construction.
- Recovery permits only an empty batch or a canonical metadata scaffold whose
  bytes, identity, mode, ownership, and digest exactly match the v8 plan;
  legacy partial, replacement, or extra-member states remain retained. The
  subsequent boundary audit also found that v8-only retirement ignored sibling
  durable cleanup controls. Its bounded, index-FD-bound scan now allows only
  the exact current allocation at each mutation boundary and blocks same-batch
  ticket, proof, terminal receipt, temporary, retained, metadata-stage, and
  suffix residue. Normal v5/v7 joined cleanup still deletes its exact proof
  before retiring its matching v8 fence; the orphan-proof regression fixture
  explicitly holds that final retirement to model the crash window.
- The affected reclaim, pending-staging, and regular-materialization modules
  passed 291 tests in 129.047 seconds. The refreshed eleven-source lock check
  passed, followed by 280 source-lock regressions in 580.265 seconds. Ruff
  lint/format, in-memory syntax compilation, and `git diff --check` passed.
  Complete repository-private discovery then passed 1,602 tests with two
  expected skips in 1,092.322 seconds. A new frozen exact-head review remains
  required before remote publication.
- The next frozen review found three v8 control-boundary gaps: a v5→v8 crash
  could strand a present private batch, legal v5/v7 temporary authority could
  be blocked by the v8 classifier, and a late malformed v8 descendant could
  escape a joined mutation boundary. The repair adds the private-use-retirement
  receipt described above, strict temporary promotion before v8 classification,
  and same-index-FD broad-representation checks at every joined boundary.
  A second independent audit then found two TOCTOU details: private evidence
  must be fully resampled after its control scan, and the joined v8 parser must
  capture and parse on the same index FD as that scan. Both are now covered by
  direct regressions.
- The current affected suite passed 316 tests in 133.432 seconds. Source lock
  was refreshed and checked for all eleven sources; its 280-test regression
  suite passed in 568.903 seconds. Ruff lint/format, Python syntax compilation,
  and `git diff --check` passed on the same implementation. Complete discovery
  and a fresh frozen whole-range review remain required before remote
  publication.
- Complete repository-private discovery subsequently passed 1,627 tests with
  two expected skips in 1,101.205 seconds. The journal-only evidence update
  does not alter the reviewed production or test source range; it needs only
  lightweight documentation and lock checks before the signed frozen commit.
- A fresh whole-range review then found that the v5/v7 joined-allocation
  classifier was called after either cleanup entry point had opened its index
  directory descriptor but before that descriptor entered the existing
  `try`/`finally`. A malformed, replaced, or mismatched v8 control could
  therefore raise during the join and leak the descriptor. Both entry points
  now perform the joined read inside their existing `try`/`finally`, preserving
  the same fail-closed ordering while guaranteeing descriptor release on that
  exception path. A direct v7/v8 metadata-size-mismatch regression exercises
  ticket deletion and empty-proof deletion separately and verifies every
  tracked index descriptor is closed (`EBADF`) after the expected `SyncError`.
- The new regression passed alone, and the affected reclaim, pending-staging,
  and regular-materialization suite passed 317 tests in 148.393 seconds. The
  eleven-source lock was refreshed and checked; Ruff lint/format, Python
  syntax compilation, and `git diff --check` passed. Complete
  repository-private discovery then passed 1,628 tests with two expected
  skips in 1,308.322 seconds. Because this repair changes the reviewed source
  and tests, it still requires a new signed frozen head and fresh exact-head
  whole-range review before remote publication.
- A second fresh whole-range review found a final pathname rebinding window in
  v5/v7 leaf and batch directory cleanup: the joined allocation scan could
  complete after the final identity check but before direct `rmdir`, allowing a
  competing empty replacement to be deleted. Portable Unix has no
  inode-conditional `rmdir`, so cleanup now atomically moves only the
  identity-encoded, exact directory entry into the existing private active
  tombstone namespace before reopening and jointly revalidating its held FD,
  pathname identity, emptiness, ownership, and mode. A replacement that wins
  the race is moved to retained evidence and is never deleted.
- Exact active directory tombstones now have v5/v7 crash recovery. Ambiguous,
  malformed, replaced, policy-incompatible, or non-exact representations stay
  fail closed. The implementation deliberately does not classify benign
  metadata churn as mutation: cleanup requires an actual identity, content, or
  access-policy mismatch before it withholds authority.
- New regressions cover v5 and v7 competing replacement retention and exact
  private leaf/batch tombstone recovery. The focused six-regression set passed
  in 0.551 seconds; the affected reclaim, pending-staging, and
  regular-materialization suite passed 321 tests in 139.904 seconds. Source
  lock refresh/check verified all eleven sources; Ruff lint/format, Python
  syntax compilation, and `git diff --check` passed. Complete
  repository-private discovery subsequently passed 1,632 tests with two
  expected skips in 1,148.870 seconds. This changed source/test range still
  requires a new signed frozen head and a fresh exact-head whole-range review
  before remote publication.
- That fresh review found one remaining P1 at the private directory removal
  boundary: parent identity alone was rechecked after the authority callback,
  but its owner-only `0700`/ACL access policy was not. A same-identity parent
  whose policy became writable could invalidate the private-namespace premise
  before final pathname deletion. The shared helper now reproves the parent
  identity, pathname binding, owner, mode, and ACL policy after the callback,
  after private-member reopening, and immediately before the final
  policy-before-binding deletion boundary. It keeps object identity, empty
  contents, and access policy as the protected properties; benign ctime or
  link-count churn remains non-authoritative.
- Deterministic v5 leaf, v5 batch, and v7 batch regressions relax the parent
  to `0770`, replace the private member, and prove the replacement survives
  while cleanup/allocation authority remains retained. Ruff lint/format,
  Python syntax compilation, `git diff --check`, and the refreshed eleven
  source lock check passed. The affected reclaim, pending-staging, and
  regular-materialization suite passed 323 tests in 244.553 seconds. Complete
  repository-private discovery then passed 1,634 tests with two expected
  skips in 1,922.213 seconds. This amended source/test range requires a new
  signed frozen head and fresh exact-head whole-range review before remote
  publication.
- A subsequent fresh readonly review found a v6 failure-publication recovery
  ordering gap. If an exact ticket-bound canonical role and a same-UID derived
  alias existed before the first scan, retaining the alias first could make
  private evidence nonempty and fail closed while the failed canonical role
  remained loadable. Recovery now evacuates only the exact canonical inode
  first, then selects foreign aliases by identity rather than alias-slot
  order. Foreign evidence remains retained; the exact inode stays in a
  non-loadable alias when retained evidence prevents deletion. A foreign
  canonical plus an exact alias continues through the established
  private-phase receipt path, preserving the existing reappearance boundary.
- Deterministic regressions cover a competing alias in both the primary and a
  later alias slot, asserting that the canonical path is absent, the exact
  inode is non-loadable, and the foreign inode remains private evidence across
  repeated recovery. The affected pending-staging, reclaim, and regular
  materialization suite passed 324 tests in 177.769 seconds. Ruff lint/format,
  Python syntax compilation, `git diff --check`, and refreshed eleven-source
  lock validation passed. Complete repository-private discovery then passed
  1,635 tests with two expected skips in 1,406.074 seconds. This changed
  source/test range still requires a new signed frozen head and fresh
  exact-head whole-range review before remote publication.
- A current-head GitHub Codex review then identified a test portability issue:
  several quarantine regression cases inferred replacement from a changed
  `(st_dev, st_ino)` tuple, which is not valid on filesystems that immediately
  reuse inode tuples. The repair keeps the original FD as evidence where
  needed, asserts the original payload versus the replacement's foreign
  marker, and uses narrowly scoped deterministic binding-failure injection for
  the replacement branch. This preserves the tested properties—replacement,
  retained foreign evidence, and fail-closed cleanup—without treating inode
  allocation as identity proof. The already-fixed descriptor-lifetime and
  source-lock findings were independently rechecked; the eleven-source lock
  includes the quarantine regression module in both canonical sources and the
  toolbox mirror.
- The full quarantine reclaim module passed 97 tests in 203.572 seconds.
  Ruff lint/format, Python syntax compilation, `git diff --check`, and source
  lock refresh/check passed. Complete repository-private discovery then passed
  1,635 tests with two expected skips in 2,185.987 seconds. This changed test
  and lock range requires a new signed frozen head and fresh exact-head
  whole-range review before remote publication.
