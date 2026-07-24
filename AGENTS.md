# Codex Personal Sync Guidelines

- This repository is the canonical owner of `scripts/codex_personal_sync.py`,
  its `tests/test_*.py` suites, and `schema/sync-manifest.schema.json`.
- Copies declared in `sync-source-lock.json` are generated, read-only consumer
  mirrors. Never import changes from `codex-toolbox` or
  `codex-private-workflows` back into this repository.
- After changing a locked canonical source, run
  `python3 scripts/sync_canonical_mirrors.py refresh-lock`, then run the source
  lock tests.
- Mirror operations must name both `--mirror` and `--target-root`. Do not add
  consumer auto-discovery or implicit sibling paths.
- `generate` must also receive the full committed canonical `HEAD` through
  `--source-commit`. Locked sources, the lock, generator, and this rules file
  must exactly match that commit.
- Existing consumer receipt/managed paths must be clean tracked files; only
  wholly absent bootstrap paths may be created. Never overwrite consumer edits.
- Preserve bidirectional root-overlap rejection, root-bound transaction
  locking, FD-relative IO, fixed/bound Git executable and control-plane
  identities, the pre-snapshot Git 2.45+ `--no-lazy-fetch` capability gate,
  parent-frozen private Git control snapshots, bounded Git subprocesses, and
  pre-object partial/promisor, alternate, include, and replace rejection.
- Preserve the bounded stage-0 consumer index snapshot before every write,
  no-clobber conditional publication, durable per-file and whole-generation
  crash recovery, terminal whole-target-group validation, and final
  `generated-sync-source-lock.json` provenance receipt.
- Generate each consumer, review its resulting diff, and use `check` to prove
  receipt and byte parity. Make semantic changes only in this repository.
