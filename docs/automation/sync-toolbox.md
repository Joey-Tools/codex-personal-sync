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
  ordinary merge/fast-forward history. Publication uses an exact
  `--force-with-lease=<ref>:<prepared-sha>` compare-and-swap after proving the
  desired commit descends from that prepared SHA. The refspec names the exact
  desired commit OID instead of a moving local `HEAD`. First publication uses
  the exact absent-ref lease `<ref>:`. Branch discovery accepts only zero
  records or one exact lowercase 40-hex SHA plus the requested full ref. A
  fetched tracking ref must equal that prepared SHA before the branch is used.
- Before switching to the automation branch, the target is detached at the
  freshly fetched target-base SHA. The canonical `managed-paths` command first
  validates that base's prior committed receipt. When an automation branch
  exists, the workflow fetches its exact prepared SHA, switches to it, and runs
  `managed-paths` again before inspecting its diff. Only the sorted union of
  those two independently proven lists becomes the allowlist: current toolbox
  targets, the receipt, and clean receipt-bound paths from the base or unmerged
  generated branch that the new mapping retires. Each prior receipt must
  byte-match a deterministic reconstruction from its reachable canonical
  source-lock commit. Receipt digests plus target `HEAD`/stage-0
  index/worktree parity must all agree; a consumer edit, arbitrary branch path,
  or forged/inconsistent receipt fails before generation. This preserves
  consecutive rename/removal runs on one not-yet-merged sync PR without
  trusting arbitrary branch history.
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
  requiring an ordinary fast-forward push. After that push, an exact owned
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

- This repository currently has no configured sync credential. This change does
  not invent, commit, or install one; runs fail at the first validation step
  until an administrator provisions `CODEX_TOOLBOX_SYNC_TOKEN`.
- The token identity determines the push and pull-request actor. The generated
  commit uses `github-actions[bot]` as its author but is not GPG-signed.
- The workflow deliberately fails on target merge conflicts, unrelated changes
  on the scoped branch, malformed or ambiguous remote-ref output, a fetch/ref
  mismatch, ambiguous or unowned pull requests, missing credentials, or a
  moving target base/branch. It does not repair or overwrite those states.
- GitHub does not offer this workflow an atomic "mutate only if base ref still
  equals this OID" PR operation. The immediate precondition checks and
  post-create/edit validation narrow and detect base movement, but they do not
  claim to eliminate a server-side ref change in the final request window.
- GitHub branch protection and required checks remain authoritative before the
  generated pull request can merge.
