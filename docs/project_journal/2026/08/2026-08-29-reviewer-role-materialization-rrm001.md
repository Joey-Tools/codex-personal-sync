---
id: 20260829-rrm001
title: Reviewer Role Regular-File Materialization
status: active
created: 2026-08-29
updated: 2026-08-29
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
