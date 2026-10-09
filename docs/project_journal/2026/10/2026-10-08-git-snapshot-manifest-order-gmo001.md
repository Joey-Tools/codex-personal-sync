---
id: 20261008-gmo001
title: Canonical Sync Ordering And Terminal Recovery
status: completed
created: 2026-10-08
updated: 2026-10-09
branch: codex/git-snapshot-manifest-order-20261008
pr: https://github.com/Joey-Tools/codex-personal-sync/pull/34
supersedes: []
superseded_by:
---

# Canonical Sync Ordering And Terminal Recovery

## Summary

- Logical Git snapshot manifests use one deterministic full-path ordering across source, destination and expected override projections.
- This removes a false rejection for ordinary linked worktrees whose administrative directory names are prefix siblings, without weakening content or access-policy checks.
- Canonical compatibility fixtures use real fake-home ticket authority and identity-bound legacy names, so the generated consumer can preserve strict engine checks on both macOS and Linux.
- Terminal cleanup now binds durable directory-consumption progress to receipt v5, and marker-only cleanup accepts only authorized monotonic contraction. The CI runner preserves failed child exit status.

## Decision And Reason

- Per-directory sorted traversal is depth-first, not globally path-sorted: `a/child` precedes sibling `a-file` during traversal, but follows it in a full-path sort.
- The source scan and expected override projection previously compared those different tuple orders. A real consumer rejected 788 identical path-keyed records with zero differing records.
- Normalize the logical manifest itself, retaining every record and all existing mode, length and digest fields. Do not replace equality with a lossy set/dictionary comparison or suppress a failed check.
- Object identity, content stability, access policy, complete inventory and mutation revalidation remain independently protected. Directory timestamps and link-count churn are not newly treated as content mutation.
- Historical Toolbox PR36 findings exposed two fixture defects. Its terminal-source budget fixture placed the ticket outside its fake home and fabricated its snapshot; the corrected fixture creates the ticket under the real pending-cleanup index and acquires its actual snapshot. The negative budget assertion still proves rejection before ticket or target reads.
- A legacy active-entry fixture assumed that real device/inode hexadecimal fields always make the name longer than 64 bytes. That is not portable. Assert the exact parsed parent and planned-entry identity instead; retain the actual rename/recovery checks and the separate explicit overlength-name fallback regression.
- The first fixture-only repair refreshed only the two declared fixture hashes. Its raw CI log contradicted the green status and exposed actual recovery defects. Repair engine authority at its canonical source and regenerate the complete declared group; do not manually patch generated consumer tests or accept the predecessor's false green.

## Recovery Contract And Reason

- The shell runner captures failed child exit status before executing any other command in its EXIT trap. Retained-temp diagnostics do not replace an already nonzero child status.
- A retry may have fewer surviving marker-only control paths after an authorized unlink. Accept only a subset of the original receipt-authorized paths, reject new names and reappearance during the current invocation, and continue validating remaining file identity, content, access policy and exact hard-link authority.
- A directory's `(device, inode)` pair is not a durable generation identifier after it is removed and all descriptors close. Linux may reuse the same inode for a new directory; ordinary child-entry churn also makes timestamps unsuitable as an unconditional identity signal.
- Before deleting any receipt-bound terminal directory, including control-only namespace directories and the batch root, append and fsync consumption of its logical slot while the old directory descriptor remains pinned. Across a crash, a consumed slot may be absent but may not reappear, even with the same numeric inode. Reject consumed active paths before foreign-child enumeration; an intact old descriptor is only an invocation-local lease, not cross-process authority.
- Bind the required progress control to the original ticket and immutable terminal receipt. Missing, unreadable, replaced or incomplete progress is not an empty history. An older regular receipt without this binding cannot gain historical authority by inspecting the retry's current namespace; retain insufficient legacy evidence with explicit recovery guidance. Marker-only tickets with no managed targets remain a distinct compatibility path.
- Retire progress evidence only after both canonical and isolated batch-root names are proven absent and final managed targets remain valid; preserve the existing receipt/ticket/empty-proof retirement fences. Ambiguous related representations retain the last recovery authorities.
- Protect progress identity, content and owner-only access policy, including invocation-local monotonic append observation. Benign GID changes under mode 0600 are not content mutation. This cooperative sole-UID-writer protocol does not claim hostile same-UID rollback resistance.
- Receipt and progress capacity are checked before staging; slot-header capacity is distinct from the bounded per-record line limit. The new tests stay inside the existing mapped module, retaining exactly 11 sources and their original mirror mappings.

## Validation And Delivery Boundary

- Prefix-sibling and real linked-worktree regressions reproduced the original ordering rejection before the fix; destination corruption rejection remains covered.
- The final native combined gate passed 443 tests in 1236.684 seconds: 127 uninstall/status tests, 27 pending-agent tests, 282 source-lock tests, six runner tests, and the capacity boundary. Its one skip requires a Darwin Python without waitid(WNOWAIT), unavailable in this runtime.
- The independent twelve-method progress class plus five initially failing existing cases passed all 17 tests in 82.321 seconds, including actual native benign GID drift. Coverage pairs successful interrupted retirement with same-identity reappearance, missing/partial/replaced progress, observed payload rollback, policy tampering and ambiguity rejection.
- Current-source locking, compilation, Ruff, shell checks, whitespace and journal validation accompany the frozen landing candidate. Raw unittest results, not a check's green label alone, establish test completion.
- The predecessor's apparent green Linux CI actually ended with 1801 tests, three failures, one error and 27 skips; neither that status nor its clean provider artifact validates the successor.
- Native Linux and genuine legacy scenarios, current-head GitHub Codex, complete conversations and actual full CI are pre-merge gates recorded with exact-head PR evidence. Consumers promote the complete declared group only through the official generator and the receipt-bound Toolbox public-release/private-overlay chain; this canonical workstream does not claim that separate promotion or machine installation is complete.

## Evidence

- Baseline: `70231a104dbc8e77483985c3ab3e869f78f21e21`.
- Related canonical engine hardening: `b5f74b5c77814a2b3a68642b648311e92e300093`.
- Review route: current-head GitHub Codex, with required CI and complete conversation checks; no local reviewer.
