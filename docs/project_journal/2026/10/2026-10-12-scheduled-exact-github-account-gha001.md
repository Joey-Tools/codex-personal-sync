---
id: 20261012-scheduled-exact-github-account-gha001
title: Scheduled Exact GitHub Account
status: completed
created: 2026-10-12
updated: 2026-10-12
branch: codex/scheduled-exact-github-account-20261012
pr:
supersedes: []
superseded_by:
---

# Scheduled Exact GitHub Account

## Summary

- `run-scheduled` binds `github.com/JoeyTeng-Codex` and numeric user ID
  `263204028`, rather than relying on the global active GitHub account.
- Each attempt resolves the exact native global-pool token, verifies the
  account before installation, and injects the credential only into bounded
  core GitHub API subprocess environments. Metadata and streamed release
  asset requests share the same attempt-scoped binding.
- Inherited auth, host, repository, config-path, and debug overrides are
  cleared. Empty/failed token lookup, malformed token, unverifiable identity,
  and identity mismatch stop the attempt without active-account fallback.
- No token is written into scheduler configuration, runtime receipts, command
  arguments, or the parent environment. Token diagnostics are redacted and
  the in-memory binding is reset on success and failure. No auth switch,
  credential synchronization, or new filesystem safety bypass is introduced.
- Existing `run-scheduled` argv remains valid after normal release
  propagation. Legacy jobs still require the existing audited migration.
  Manual `install`/`install-private` authentication remains unchanged.

## Validation

- Focused account binding tests cover exact host/login selection, numeric
  identity, inherited overrides, missing and invalid tokens, failure
  classification, credential redaction, binding cleanup, prohibited host or
  alias overrides, and actual native child environment injection for both
  metadata and streamed asset transport.
- Scheduler regression records account admission failure before invoking the
  installer. Existing offline scheduling fixtures isolate credentials from
  scheduling, attempt ownership, and completion-state tests.
- Native CPython 3.14.2 completed all three affected modules: 886 tests in
  730.765 seconds, 3 skips, exit 0. The bounded supervisor also exited 0 with
  no incomplete result; the raw log SHA-256 is
  `1303988565368647753575a3af37cb41e67d321f4ede45f074da33102b596225`.
- The four selected source/test/lock inputs were revalidated unchanged.
  Ruff, source-only syntax checks, the 11-source lock check, whitespace, and
  journal validation passed. This is not a full-repository or remote-CI pass.
- [Curated validation receipt](../../../evidence/20261012-scheduled-github-account.json)
  preserves the exact command, coverage, input hashes, and terminal without
  raw home data or credential values.

## Delivery Boundary

- This workstream implements and validates the canonical source only.
  No native installation, active-account switch,
  remote review, CI, push, or PR publication is claimed by local validation.
- Propagation must follow the accepted canonical-to-toolbox generator and
  receipt, immutable public release, then coupled private overlay flow.
  Installed copies and generated consumer files are not hand-edited.

## Next Steps

- Continue source promotion and native acceptance in the delivery workstream
  through the normal reviewed canonical/public/private release path.
