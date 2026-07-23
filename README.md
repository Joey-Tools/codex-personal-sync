# Codex Personal Sync

Release installer, manifest validator, rollback command, and user-level scheduler for Codex configuration packages.

## Canonical ownership

This repository is the only editable source for the sync engine, its behavior
tests, and the sync-manifest JSON Schema:

- `scripts/codex_personal_sync.py`
- `tests/test_*.py`
- `schema/sync-manifest.schema.json`

`sync-source-lock.json` declares generated consumer mirrors in
`Joey-Tools/codex-toolbox` and `Joey-Tools/codex-private-workflows`. Treat those
copies as read-only. Changes flow from this repository to consumers and never
in the reverse direction.

Both consumers receive the engine, manifest Schema, and focused safety,
retention, and scheduler/doctor tests. The toolbox mirror additionally receives
the full `tests/test_codex_personal_sync.py` behavior suite.

Canonical `master` pushes can open or update the scoped toolbox mirror pull
request through `.github/workflows/sync-toolbox.yml`. The workflow requires the
explicit least-privilege `CODEX_TOOLBOX_SYNC_TOKEN` interface documented in
[`docs/automation/sync-toolbox.md`](docs/automation/sync-toolbox.md); it fails
before target checkout when the secret is absent and never creates a
credential.

After a canonical source change, refresh and verify the lock:

```bash
python3 scripts/sync_canonical_mirrors.py refresh-lock
python3 scripts/sync_canonical_mirrors.py refresh-lock --check
```

Commit the canonical engine, tests, Schema, source lock, generator, and
`AGENTS.md` before generating a consumer. Generation requires the exact clean
canonical `HEAD`:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
```

Mirror generation never discovers sibling repositories. Select the consumer
and its existing Git top-level explicitly. Its `origin` must identify the
repository declared by the selected mirror:

```bash
python3 scripts/sync_canonical_mirrors.py managed-paths \
  --mirror toolbox \
  --target-root /path/to/codex-toolbox
python3 scripts/sync_canonical_mirrors.py generate \
  --mirror toolbox \
  --target-root /path/to/codex-toolbox \
  --source-commit "$SOURCE_COMMIT"
python3 scripts/sync_canonical_mirrors.py check \
  --mirror toolbox \
  --target-root /path/to/codex-toolbox

python3 scripts/sync_canonical_mirrors.py generate \
  --mirror private \
  --target-root /path/to/codex-private-workflows \
  --source-commit "$SOURCE_COMMIT"
python3 scripts/sync_canonical_mirrors.py check \
  --mirror private \
  --target-root /path/to/codex-private-workflows
```

The generator binds canonical and consumer roots by directory file descriptor
for the complete transaction, uses cooperative root-bound locks, and rejects
symlink ancestors or targets. It also binds the fixed Git executable and each
repository's `.git` marker, admin/common directories, and object directory;
then it double-reads those bound control files into a parent-private frozen
snapshot before launching Git. The copied content-addressed object store is
fully hashed once into a path/identity/access/size/content manifest. Every Git
call revalidates that complete pack/idx/loose manifest before and after the
child; stable file change signals avoid rereading pack bytes while still
failing closed if content stability can no longer be proved. Git calls use only
that snapshot, are deadline/output bounded, and reject unsafe config includes,
replace refs, alternates, and lazy object fetching. Canonical and consumer
origins must match the lock, roots must not overlap in either ancestry
direction, and managed paths may never enter the Git control plane. Target
validation also rejects case-insensitive/NFC collisions and overlap in either
ancestry direction with the receipt, transaction markers, or per-target
exchange journals.

The live Git binding includes `HEAD`, its resolved loose ref when present,
`packed-refs`, common/worktree config, the index, and explicit absence records
for optional controls. A fixed isolated Python launcher changes directory by
the private Git directory descriptor before `execve`; no Python `preexec_fn`
or pathname cwd is used. Private snapshots live below a durable mode-0700,
current-owner tool root outside both repositories. Locked owner/phase records
make active snapshots distinguishable from bounded stale cleanup; invalid or
identity-ambiguous leftovers are quarantined rather than deleted by pathname.
Expected owner-record cleanup moves the exact file into a separate durable
quarantine beside the private-control parent, on the same verified filesystem;
stale-owner recovery uses that same explicit placement even when the consumer
repository is on another filesystem. It therefore cannot fill the active tool
root or cross a repository mount. Verified transaction temporaries, journals,
and owner records use identity/content-bound `transient-file-*` names and are
retained for identity-aware recovery alongside receipt-bound retired target
bytes under `recovery-file-*` names. The synchronizer never converts a filename
prefix alone into deletion authority; current and legacy unclassified entries
remain for manual inspection. Capacity is reserved under the quarantine lock
before a source pathname moves. At exactly 10,000 entries, the quarantine can
still be opened to validate and diagnose journal-bound recovery, but cleanup
that would retain another journal is blocked before that journal moves and the
active journal remains in place until identity-aware maintenance frees
capacity. The no-op path below prevents unchanged scheduled runs from creating
additional retained artifacts.
All source reads and Git stdout use the same 32 MiB per-file/per-command
contract, while one shared operation deadline and aggregate byte/entry budget
cover snapshotting, Git execution, recovery, generation, and cleanup.

Before every write it requires every existing managed target and receipt to
match the clean consumer stage-0 index; entirely absent paths are the only
bootstrap exception. A durable group transaction binds that index snapshot,
the desired present-or-absent file set, and every before/after object. A valid
prior receipt expands the managed set only after its exact bytes are
reconstructed from the locally available, reachable canonical commit named by
the receipt; every old target must also match clean `HEAD`, index, and worktree
state. A pending journal can describe only that independently derived set and
the current mapping. Paths retired or renamed by the new lock are conditionally
isolated under the same per-file and whole-generation recovery journals, then
atomically moved into a sibling mode-0700 durable quarantine. The generator
never pathname-unlinks receipt-bound retired target bytes; unrelated or edited
paths are preserved.
Removal journals preallocate and bind the exact recovery-quarantine name and
expected object/content record. Recovery accepts a double-missing
target/temporary state only when that exact durable entry still matches under
the quarantine lock immediately before and after active-journal cleanup; older
journals or missing/mismatched evidence remain ambiguous and are preserved.
Restart recovery runs
before ordinary dirty checks, so a crash cannot strand a partial generation or
turn generated bytes into an apparent user edit. The same bounded index
snapshot and complete target group are revalidated before receipt publication,
so consumer worktree or index-only edits are never silently overwritten.
Absent targets publish through a no-replace hard link; existing targets publish
through an atomic exchange that quarantines the displaced object. A durable
per-file journal restores the displaced bytes after a crash or detected race,
while fixed pending/main/completion transaction records make whole-generation
publication and cleanup resumable without clobbering unrelated files.
When every managed file, absence, mode, and final receipt already matches the
requested canonical commit, `generate` performs the same final source,
repository, index, and object revalidation but skips all transaction writes.

If recovery finds an older pending generation, its journal-bound canonical
commit and source lock are recovered before a newly requested source head is
considered. When the requested head differs, the command completes the old
recovery and then fails clearly so the recovered consumer state can be
reviewed/committed before a fresh generation; it never silently labels the old
receipt as the new request.

`generate` publishes `generated-sync-source-lock.json` last and atomically.
That receipt binds the canonical repository and commit, generator/rules
contract versions, selected mirror and target repository, every source
path/hash/mode, and deterministic mapping, file-set, and generated-tree
digests. `check` validates the receipt before accepting mirrored bytes.
Both `generate` and `check` revalidate the complete declared target group plus
the receipt at transaction completion; `generate` invalidates a just-published
receipt if that final validation fails.

`managed-paths` is read-only. It returns the sorted union of current lock
targets, the receipt, and clean prior-receipt targets after applying the same
repository, digest, HEAD/index/worktree, layout, and pending-journal gates.
Automation uses this output to stage both generated additions and proven
retirements without allowing arbitrary deletions.

`check` is read-only and requires the consumer stage-0 index to match `HEAD`
for every managed target and receipt. It therefore rejects staged generated
bytes even when the worktree happens to match the lock. `generate` may leave
its expected uncommitted generated diff for review, but it never accepts an
unrelated index or worktree edit as source material.

## Test

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m compileall -q scripts tests
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
```
