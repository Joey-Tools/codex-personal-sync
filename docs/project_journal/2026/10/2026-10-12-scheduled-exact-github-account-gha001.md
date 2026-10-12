---
id: 20261012-scheduled-exact-github-account-gha001
title: Scheduled Exact GitHub Account
status: completed
created: 2026-10-12
updated: 2026-10-12
branch: codex/scheduled-exact-github-account-20261012
pr: https://github.com/Joey-Tools/codex-personal-sync/pull/37
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
- Inherited auth, host, repository, `GH_CONFIG_DIR`, debug (including legacy
  `DEBUG`), forced-terminal, pager and formatting overrides are cleared.
  Scheduled children disable telemetry logging and update notices.
  Empty/failed token lookup, malformed token, unverifiable identity,
  and identity mismatch stop the attempt without active-account fallback.
- No token is written into scheduler configuration, runtime receipts, command
  arguments, or the parent environment. Token diagnostics are redacted and
  the in-memory binding is reset on success and failure. No auth switch,
  credential synchronization, or new filesystem safety bypass is introduced.
- Existing `run-scheduled` argv remains valid after normal release
  propagation. Legacy jobs still require the existing audited migration.
  Manual `install`/`install-private` authentication remains unchanged.
- The global pool follows gh's standard configuration lookup. Legitimate
  `HOME` and `XDG_CONFIG_HOME` roots remain intact, as explicitly selected by
  the user. Exact GitHub host/login/numeric ID is verified; credential-store
  pathname attestation is not part of this binding. The scheduler never creates
  an isolated `GH_CONFIG_DIR` or silently rewrites the account-home root.

## Validation

- Focused account binding tests cover exact host/login selection, numeric
  identity, inherited overrides, missing and invalid tokens, failure
  classification, credential redaction, binding cleanup, prohibited host or
  alias overrides, and actual native child environment injection for both
  metadata and streamed asset transport.
- Scheduler regression records account admission failure before invoking the
  installer. Existing offline scheduling fixtures isolate credentials from
  scheduling, attempt ownership, and completion-state tests.
- Initial candidate `6858387` completed all three affected modules: 886 tests in
  730.765 seconds, 3 skips, exit 0. The bounded supervisor also exited 0 with
  no incomplete result; the raw log SHA-256 is
  `1303988565368647753575a3af37cb41e67d321f4ede45f074da33102b596225`.
- The four selected source/test/lock inputs were revalidated unchanged.
  Ruff, source-only syntax checks, the 11-source lock check, whitespace, and
  journal validation passed. This is not a full-repository or remote-CI pass.
- [Curated validation receipt](../../../evidence/20261012-scheduled-github-account.json)
  preserves the exact command, coverage, input hashes, and terminal without
  raw home data or credential values.
- Applicable review finding4248581910 identified omitted supported output and
  debug overrides. The repaired scope adds an independent literal control-list
  regression across token lookup, identity and release API children, plus
  actual metadata/asset child checks. Native CPython 3.14.2 and declared
  CPython 3.9.6 each completed all 12 focused account tests.
- The repaired scope completed all three affected modules again: 887 tests in
  747.471 seconds, 3 skips, exit 0; supervisor 0 with incomplete null and log
  SHA-256 `0988b1a62df8f68be8d5ea392801431726a4ffe1283bc7c6b4e01c2d9eb459f0`.
  All five frozen source/test/lock contents were independently revalidated.
  [Repair validation receipt](../../../evidence/20261012-scheduled-github-output-controls.json)
  preserves this successor's actual coverage without replacing initial evidence.
- Finding4249168489 correctly describes gh's global-root precedence, but its
  suggested forced OS-account HOME is outside the selected design. The README
  now names the narrower `GH_CONFIG_DIR` sanitization boundary. A mocked-child
  regression preserves legitimate global roots across token lookup, identity
  verification and release transport while retaining exact account selection
  and an unchanged parent environment. No alternate real account pool is used.
- Both native CPython 3.14.2 and declared CPython 3.9.6 completed all 13
  focused account tests. The complete source-lock module passed 282 tests in
  534.473 seconds, one skip, exit 0. Its supervisor exited 0 with incomplete
  null; all five selected input contents were revalidated unchanged.
  [Global-pool boundary validation](../../../evidence/20261012-scheduled-standard-global-pool.json)
  records this documentation/test-only scope. The earlier 887-test pass remains
  historical; no new 888-test affected-suite or hosted-CI pass is claimed.

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
