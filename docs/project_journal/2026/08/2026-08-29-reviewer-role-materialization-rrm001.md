---
id: 20260829-rrm001
title: Reviewer Role Regular-File Materialization
status: active
created: 2026-08-29
updated: 2026-09-01
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

## Next Steps

- Form the signed landing commit and run the frozen whole-range fresh-context
  local review.
- Open and merge the canonical pull request after CI and current-head GitHub
  Codex evidence pass.
- Propagate the accepted canonical source through toolbox and the private
  overlay, then smoke-test the installed reviewer role without a path override
  on every target host.

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
