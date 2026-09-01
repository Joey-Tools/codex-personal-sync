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
  claim semantics. Only version 6 applies the path-derived regular-file claim
  omission, so an upgrade cannot strand an older pending transaction.
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
