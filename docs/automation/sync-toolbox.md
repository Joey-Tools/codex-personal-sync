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
- The canonical checkout is detached at the exact triggering SHA with full
  local history. The generator never fetches implicitly: it reconstructs a
  prior receipt from the exact reachable canonical commit named by that
  receipt, and missing history fails closed. A `refresh-lock --check` gate runs
  before generation.
- Canonical and target checkouts are explicit sibling roots. Generation always
  supplies `--target-root`, `--mirror toolbox`, and the exact
  `--source-commit`, then runs `check` against the same target.
- The fixed target branch is `automation/canonical-personal-sync`. Updates use
  ordinary merge/fast-forward history and a normal push; there is no force
  push. Branch discovery accepts only zero records or one exact lowercase
  40-hex SHA plus the requested full ref. A fetched tracking ref must equal
  that prepared SHA before the branch is used.
- Before switching to the automation branch, the target is detached at the
  freshly fetched target-base SHA. The canonical `managed-paths` command then
  validates the prior committed receipt and returns the exact allowlist:
  current toolbox targets, the receipt, and any clean receipt-bound paths that
  the new mapping retires. The prior receipt must byte-match a deterministic
  reconstruction from its reachable canonical source-lock commit. Receipt
  digests plus target `HEAD`/stage-0 index/worktree parity must all agree; a
  consumer edit or forged/inconsistent receipt fails before generation.
- Existing branch history, working-tree changes, staged changes, and final PR
  diff are restricted to that generated allowlist. This permits an exact
  canonical rename/removal while preventing the workflow from staging an
  unrelated deletion.
- Generated regular files are staged from their exact worktree bytes with
  `git hash-object --no-filters` and `git update-index --cacheinfo`; missing
  allowed paths use an explicit index removal. The commit is assembled from
  that index with Git plumbing, so target/system attributes, clean/LFS
  filters, and working-tree encodings cannot rewrite the audited bytes. Mirror
  `check` runs again after the commit.
- An existing pull request is mutable only when its live lifecycle, repository
  owner, base, head branch, head OID, same-repository status, and automation
  marker all match the prepared branch evidence. The workflow repeats those
  checks immediately around publication rather than trusting an earlier PR
  listing.
- `pr_has_changes` and `branch_needs_update` are independent. A stale generated
  branch can become tree-equal to `master` after reconciliation while still
  requiring an ordinary fast-forward push. After that push, an exact owned
  open PR is revalidated and closed without deleting the branch. A clean run
  with no owned PR performs no PR mutation.
- The target `master` SHA and live sync-branch SHA are checked again before
  push. The push is skipped safely if another actor already published the
  exact desired SHA; every other base or branch drift fails without an
  overwrite. The post-push remote SHA must equal the generated commit.

## Limitations

- This repository currently has no configured sync credential. This change does
  not invent, commit, or install one; runs fail at the first validation step
  until an administrator provisions `CODEX_TOOLBOX_SYNC_TOKEN`.
- The token identity determines the push and pull-request actor. The generated
  commit uses `github-actions[bot]` as its author but is not GPG-signed.
- The workflow deliberately fails on target merge conflicts, unrelated changes
  on the scoped branch, malformed or ambiguous remote-ref output, a fetch/ref
  mismatch, ambiguous or unowned pull requests, missing credentials, or a
  moving target base/branch. It does not repair or overwrite those states.
- GitHub branch protection and required checks remain authoritative before the
  generated pull request can merge.
