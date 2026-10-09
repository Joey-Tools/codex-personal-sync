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
- A legacy batch may contain both original metadata names. Bind every existing name before mutation, rather than selecting one preferred name and later misclassifying the surviving compatibility name as an expansion. Recovery is independent of directory enumeration order; do not retroactively bind a name omitted from an older receipt.
- Budget regressions assert that each verification happens only after its batch is charged to the shared action budget, retaining exact cleanup counts and final-state checks. An additional authority revalidation must not fail a test merely because a verifier ran five times instead of four.
- A directory's `(device, inode)` pair is not a durable generation identifier after it is removed and all descriptors close. Linux may reuse the same inode for a new directory; ordinary child-entry churn also makes timestamps unsuitable as an unconditional identity signal.
- Before deleting any receipt-bound terminal directory, including control-only namespace directories and the batch root, append and fsync consumption of its logical slot while the old directory descriptor remains pinned. Across a crash, a consumed slot may be absent but may not reappear, even with the same numeric inode. Reject consumed active paths before foreign-child enumeration; an intact old descriptor is only an invocation-local lease, not cross-process authority.
- Bind the required progress control to the original ticket and immutable terminal receipt. Missing, unreadable, replaced or incomplete progress is not an empty history. An older regular receipt without this binding cannot gain historical authority by inspecting the retry's current namespace; retain insufficient legacy evidence with explicit recovery guidance. Marker-only tickets with no managed targets remain a distinct compatibility path.
- Retire progress evidence only after both canonical and isolated batch-root names are proven absent and final managed targets remain valid; preserve the existing receipt/ticket/empty-proof retirement fences. Ambiguous related representations retain the last recovery authorities.
- Protect progress identity, content and owner-only access policy, including invocation-local monotonic append observation. Benign GID changes under mode 0600 are not content mutation. This cooperative sole-UID-writer protocol does not claim hostile same-UID rollback resistance.
- Receipt and progress capacity are checked before staging; slot-header capacity is distinct from the bounded per-record line limit. The new tests stay inside the existing mapped module, retaining exactly 11 sources and their original mirror mappings.

## Publication Follow-up

- The current-head review of `349b72ff56fa0884a9a76cbc992a17ef3d55d26a` identified an ordinary crash after durable progress publication but before receipt publication. Recovery must preserve the original ticket and original namespace/control authority while resuming this pre-consumption publication phase; an incomplete, foreign or already-consumed orphan must never become an empty history. This is a code-recovery defect, not a retryable provider outage.
- Resume only a single canonical, exact header-only progress object after independently proving original ticket-bound target links, the complete live namespace, pointer/metadata and marker/commit-evidence contents. Bind its unchanged inode, payload and owner-only policy before and after receipt publication. Reject old tickets without explicit commit-evidence authority rather than manufacture historical content evidence.
- Keep the ordinary global new-mutation fence unchanged. Only the install preflight and cleanup entrypoint perform a budgeted full-ticket recovery attempt before that fence; no caller-wide orphan exemption is introduced. A successful recovery charges the shared action budget once and retires the complete authorized batch.
- Directory progress capacity must count actual directory slots, including the batch root and authorized alias ancestors, rather than every projected regular file or symlink. Keep complete namespace-entry and receipt-byte limits separate; neither a large flat file set nor a real directory overflow should be misclassified.
- Repair evidence: [publication crash finding](https://github.com/Joey-Tools/codex-personal-sync/pull/34#discussion_r4225738543) and [directory projection finding](https://github.com/Joey-Tools/codex-personal-sync/pull/34#discussion_r4225738552). The successor requires paired regressions, refreshed source hashes, a newly frozen range and fresh current-head review/CI before acceptance.

## Validation And Delivery Boundary

- Prefix-sibling and real linked-worktree regressions reproduced the original ordering rejection before the fix; destination corruption rejection remains covered.
- The `349b72f` candidate's native combined gate passed 443 tests in 1236.684 seconds: 127 uninstall/status tests, 27 pending-agent tests, 282 source-lock tests, six runner tests, and the capacity boundary. Its one skip requires a Darwin Python without waitid(WNOWAIT), unavailable in this runtime. That result does not validate the publication/projection successor.
- The independent twelve-method progress class plus five initially failing existing cases passed all 17 tests in 82.321 seconds, including actual native benign GID drift. Coverage pairs successful interrupted retirement with same-identity reappearance, missing/partial/replaced progress, observed payload rollback, policy tampering and ambiguity rejection.
- Current-source locking, compilation, Ruff, shell checks, whitespace and journal validation accompany the frozen landing candidate. Raw unittest results, not a check's green label alone, establish test completion.
- The predecessor's apparent green Linux CI actually ended with 1801 tests, three failures, one error and 27 skips; neither that status nor its clean provider artifact validates the successor.
- The subsequent `349b72f` Linux run correctly failed after 1817 tests with two marker-only retry errors and two obsolete verifier-call-count assertions. Deterministic metadata-first regressions reproduce both retry boundaries independently of filesystem enumeration order. The repaired nine-test set passed in 38.943 seconds, including same-inode content tampering, external hard links and control reappearance rejection.
- The publication/projection focused set passed all 15 tests in 55.779 seconds. The deep-backup capacity test now unpacks the projection's `(entries, directories)` pair, retaining its alias, evidence and capacity assertions. The cursor fault-injection test isolates the unrelated recovery preflight so its synthetic fd reaches only the intended cursor cleanup path; production authority is unchanged.
- The final six-module native gate passed 759 tests in 1886.570 seconds, with one skip requiring a Darwin Python without waitid(WNOWAIT). Its engine, three changed test-module and source-lock hashes were unchanged throughout. Interrupted and failed predecessor attempts remain separate evidence and never supply a passing full-gate result.
- Native Linux and genuine legacy scenarios, current-head GitHub Codex, complete conversations and actual full CI are pre-merge gates recorded with exact-head PR evidence. Consumers promote the complete declared group only through the official generator and the receipt-bound Toolbox public-release/private-overlay chain; this canonical workstream does not claim that separate promotion or machine installation is complete.

## Evidence

- Baseline: `70231a104dbc8e77483985c3ab3e869f78f21e21`.
- Related canonical engine hardening: `b5f74b5c77814a2b3a68642b648311e92e300093`.
- Review route: current-head GitHub Codex, with required CI and complete conversation checks; no local reviewer.
