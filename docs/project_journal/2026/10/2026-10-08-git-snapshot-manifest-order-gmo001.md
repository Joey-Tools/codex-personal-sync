---
id: 20261008-gmo001
title: Canonical Git Snapshot Manifest Ordering
status: completed
created: 2026-10-08
updated: 2026-10-08
branch: codex/git-snapshot-manifest-order-20261008
pr: https://github.com/Joey-Tools/codex-personal-sync/pull/34
supersedes: []
superseded_by:
---

# Canonical Git Snapshot Manifest Ordering

## Summary

- Logical Git snapshot manifests use one deterministic full-path ordering across source, destination and expected override projections.
- This removes a false rejection for ordinary linked worktrees whose administrative directory names are prefix siblings, without weakening content or access-policy checks.
- Canonical compatibility fixtures use real fake-home ticket authority and identity-bound legacy names, so the generated consumer can preserve strict engine checks on both macOS and Linux.

## Decision And Reason

- Per-directory sorted traversal is depth-first, not globally path-sorted: `a/child` precedes sibling `a-file` during traversal, but follows it in a full-path sort.
- The source scan and expected override projection previously compared those different tuple orders. A real consumer rejected 788 identical path-keyed records with zero differing records.
- Normalize the logical manifest itself, retaining every record and all existing mode, length and digest fields. Do not replace equality with a lossy set/dictionary comparison or suppress a failed check.
- Object identity, content stability, access policy, complete inventory and mutation revalidation remain independently protected. Directory timestamps and link-count churn are not newly treated as content mutation.
- Historical Toolbox PR36 findings exposed two fixture defects. Its terminal-source budget fixture placed the ticket outside its fake home and fabricated its snapshot; the corrected fixture creates the ticket under the real pending-cleanup index and acquires its actual snapshot. The negative budget assertion still proves rejection before ticket or target reads.
- A legacy active-entry fixture assumed that real device/inode hexadecimal fields always make the name longer than 64 bytes. That is not portable. Assert the exact parsed parent and planned-entry identity instead; retain the actual rename/recovery checks and the separate explicit overlength-name fallback regression.
- Refresh only the two declared fixture hashes in `sync-source-lock.json`. Do not weaken engine authority, change the engine bytes, or manually patch the generated consumer tests.

## Validation Scope

- Regression coverage exercises prefix-sibling ordering and an actual linked-worktree Git snapshot.
- Existing destination corruption rejection remains part of focused validation; the complete source-lock suite is the local regression gate.
- The new integration regression reproduced the original rejection before the fix. The focused three-test positive/negative run passed after the fix.
- The complete source-lock module passed 282 tests with one skip in 1027.719 seconds in the native environment. The 11-source lock check, Ruff, compilation, diff whitespace and journal validation also passed.
- The two repaired fixture regressions passed, and the complete pending-agent compatibility module passed 27 tests. These fixture results are separate from the earlier ordering-only source-lock run.
- The complete uninstall/status module passed all 113 tests in 491.907 seconds in the native environment, including legacy active-entry recovery, explicit name-limit fallback, hard-link tamper rejection and read-only status invariants.
- After refreshing the two fixture hashes, the complete source-lock module passed 282 tests with one skip in 531.183 seconds. The 11-source lock check and native Python 3.13.0/Ruff 0.13.2 checks cover the corrected candidate.
- The generated engine remains unchanged; the source lock records the corrected fixture bytes. Consumers must still promote the complete declared group through the official generator and receipt-bound release chain.

## Evidence

- Baseline: `70231a104dbc8e77483985c3ab3e869f78f21e21`.
- Related canonical engine hardening: `b5f74b5c77814a2b3a68642b648311e92e300093`.
- Review route: current-head GitHub Codex, with required CI and complete conversation checks; no local reviewer.
