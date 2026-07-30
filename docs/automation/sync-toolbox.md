# Toolbox mirror sync automation

`.github/workflows/sync-toolbox.yml` opens or updates a generated pull request
from canonical `master` to `Joey-Tools/codex-toolbox`. It never writes directly
to the target `master` branch.

## Credential interface

The source repository must define one Actions secret:

- `CODEX_TOOLBOX_SYNC_TOKEN`: a fine-grained personal access token restricted
  to the single `Joey-Tools/codex-toolbox` repository with:
  - Contents: Read and write
  - Pull requests: Read and write
  - Metadata: Read-only (the implicit GitHub minimum)

No broader organization or workflow permission is required. The workflow does
not create or install persistent credentials, does not fall back to the source
repository's `GITHUB_TOKEN`, and fails before checking out the target when the
secret is absent. Target checkout disables credential persistence; authenticated
Git commands receive an in-memory HTTP header for that process only. Repository
administrators must provision and rotate the secret outside this workflow.

## Trust and mutation boundaries

- Triggers are restricted to canonical `master` pushes and trusted manual
  dispatches on canonical `master`; the workflow does not run for pull-request
  events.
- Every third-party action in this token-bearing workflow is pinned to a full
  40-hex commit SHA. Both canonical and target checkout steps use the
  GitHub-verified `actions/checkout` `v4.4.0` commit
  `11d5960a326750d5838078e36cf38b85af677262`. A strict structural YAML loader
  traverses every job-level reusable workflow and step-level action `uses`
  mapping, including block, flow, and quoted-key forms, and rejects unsupported
  aliases, anchors, merge keys, duplicate keys, or malformed structures before
  enforcing full-SHA external references.
- The canonical checkout is detached at the exact triggering SHA with full
  local history. The generator never fetches implicitly: it reconstructs a
  prior receipt from the exact reachable canonical commit named by that
  receipt, and missing history fails closed. A `refresh-lock --check` gate runs
  before generation.
- Canonical and target checkouts are explicit sibling roots. Generation always
  supplies `--target-root`, `--mirror toolbox`, and the exact
  `--source-commit`, then runs `check` against the same target.
- The fixed target branch is `automation/canonical-personal-sync`. Updates use
  a fresh one-commit profile on the exact current target base. Before replacing
  an existing automation branch, the workflow validates every commit-parent
  edge in its complete branch-exclusive history, including merge side
  histories. It rejects paths outside the union of independently proven
  managed paths, high-confidence credential markers in every introduced blob,
  ambiguous topology, and hard commit/parent/path/blob budget overruns. The
  receipt binds the exact base, head, merge base, topology, introduced objects,
  allowlist, and history digest. Publication uses an exact
  `--force-with-lease=<ref>:<prepared-sha>` compare-and-swap; the prepared SHA
  cryptographically binds the validated prior topology, and the generated head
  is separately required to be the target base or exactly one single-parent
  commit on that base. The refspec names the exact desired commit OID instead
  of a moving local `HEAD`. First publication uses the exact absent-ref lease
  `<ref>:`. Branch discovery accepts only zero records or one exact lowercase
  40-hex SHA plus the requested full ref. A fetched tracking ref must equal
  that prepared SHA before the branch is used.
- Before switching to the automation branch, the target is detached at the
  freshly fetched target-base SHA. The canonical `managed-paths` command first
  validates that base's prior committed receipt. When an automation branch
  exists, the workflow fetches its exact prepared SHA, switches to it, and runs
  `managed-paths` again before inspecting its diff. Only the sorted union of
  those two independently proven lists becomes the allowlist: current toolbox
  targets, the receipt, and clean receipt-bound paths from the base or unmerged
  generated branch that the new mapping retires. The old branch is inspected
  but never merged into the new generated branch. Each prior receipt must
  byte-match a deterministic reconstruction from its reachable canonical
  source-lock commit. Receipt digests plus target `HEAD`/stage-0
  index/worktree parity must all agree; a consumer edit, arbitrary branch path,
  or forged/inconsistent receipt fails before generation. This preserves
  consecutive rename/removal runs on one not-yet-merged sync PR while
  republishing a bounded fresh branch instead of retaining arbitrary history.
- Existing branch history is checked edge-by-edge without rename collapsing;
  add-then-delete, rename-out-and-back, side-merge, and transient-secret
  sequences cannot hide behind a clean net diff. High-confidence private-key
  headers include generic, encrypted, RSA, DSA, EC, OpenSSH, and PGP private
  key block forms; the scanner deliberately does not widen this gate to vague
  key-related prose. Working-tree changes, staged changes, and the final PR
  diff remain restricted to the generated allowlist.
- Generated regular files are staged from their exact worktree bytes with
  `git hash-object --no-filters` and `git update-index --cacheinfo`; missing
  allowed paths use an explicit index removal. The commit is assembled from
  that index with Git plumbing, so target/system attributes, clean/LFS
  filters, and working-tree encodings cannot rewrite the audited bytes. Mirror
  `check` runs again after the commit.
- An existing pull request is mutable only when its live lifecycle, repository
  owner, base name and exact base OID, head branch and exact head OID,
  same-repository status, and automation marker all match the prepared branch
  evidence. The workflow also queries the live target-base ref immediately
  before every create, edit, or close mutation, requires the live sync branch
  to equal the generated head, and verifies both refs plus PR evidence again
  after create/edit. A newly created PR is also rebound to the exact positive
  PR number returned by `gh pr create`; a different open PR cannot satisfy the
  postcondition. If a post-create ref or ownership check fails, the workflow
  closes only that returned-number PR after revalidating its repository,
  lifecycle, branch names, owner, and automation marker, then verifies the
  closed state. Ambiguous cleanup preserves the exact PR locator as recovery
  evidence. The workflow does not trust an earlier PR listing or assume that
  an unchanged branch name implies unchanged base/head object identity.
- `pr_has_changes` and `branch_needs_update` are independent. A stale generated
  branch can become tree-equal to `master` after reconciliation while still
  requiring an exact leased branch replacement. After that push, an exact owned
  open PR is revalidated and closed without deleting the branch. A clean run
  with no owned PR performs no PR mutation.
- The target `master` SHA and live sync-branch SHA are checked again before
  push. The push is skipped safely if another actor already published the
  exact desired SHA; every other base or branch drift fails without an
  overwrite. The exact lease makes a deletion, rollback, appearance, or drift
  between the final `ls-remote` and push fail at the server even when the
  racing ref happens to land on the desired SHA. The post-push remote SHA must
  equal the generated commit.

## Limitations

- The sync credential is externally provisioned deployment state that source
  code cannot attest. The workflow does not invent, commit, install, or fall
  back from it; runs fail at the first validation step unless an administrator
  has made `CODEX_TOOLBOX_SYNC_TOKEN` available to this repository.
- The token identity determines the push and pull-request actor. The generated
  commit uses `github-actions[bot]` as its author but is not GPG-signed.
- The workflow deliberately fails on unsafe or oversized branch history,
  unrelated changes on the scoped branch, malformed or ambiguous remote-ref
  output, a fetch/ref
  mismatch, ambiguous or unowned pull requests, missing credentials, or a
  moving target base/branch. It does not repair or overwrite those states.
- GitHub does not offer this workflow an atomic "mutate only if base ref still
  equals this OID" PR operation. The immediate precondition checks and
  post-create/edit validation narrow and detect base movement, but they do not
  claim to eliminate a server-side ref change in the final request window.
- GitHub branch protection and required checks remain authoritative before the
  generated pull request can merge.
