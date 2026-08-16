# Codex Personal Sync

Release installer, manifest validator, rollback command, and user-level scheduler for Codex configuration packages.

## Canonical ownership

This repository is the only editable source for the sync engine, its behavior
tests, and the sync-manifest JSON Schema:

- `scripts/codex_personal_sync.py`
- `tests/test_*.py`
- `schema/sync-manifest.schema.json`

`sync-source-lock.json` declares the generated consumer mirror in
`Joey-Tools/codex-toolbox`. Treat that copy as read-only. Changes flow from this
repository to toolbox and never in the reverse direction.

The toolbox mirror receives the engine, manifest schema, full behavior suite,
and focused safety, retention, and scheduler/doctor tests.
`Joey-Tools/codex-private-workflows` consumes the synchronizer only from a
receipt-bound exact toolbox commit and its complete immutable public release;
there is no direct canonical-to-private mirror.

The installed synchronizer remains a standalone one-file runtime, while the
canonical mirror generator is a separate controller that is not installed on
consumer hosts. Their shared private-control policy is therefore intentionally
implemented twice, but it is one contract rather than two ownership sources.
Machine parity tests bind the ordered root registry, schema fields, platform
paths, reason codes, ancestor cap, ownership classification, shared-parent
policy, primary-allocation scenario matrix, and retained-recovery document,
publication, lease, and resource-cap constants. A change to that contract must
update both implementations and the parity matrix in the same commit; an
independent one-sided edit is unsupported.

Canonical `master` pushes can open or update the scoped toolbox mirror pull
request through `.github/workflows/sync-toolbox.yml`. The workflow requires the
explicit least-privilege `CODEX_TOOLBOX_SYNC_TOKEN` interface documented in
[`docs/automation/sync-toolbox.md`](docs/automation/sync-toolbox.md); it fails
before target checkout when the secret is absent and never creates a
credential. Repeated runs against one unmerged sync branch reconstruct
managed-path authority from both the target base and the branch's exact
historical receipt, and branch publication uses an exact compare-and-swap
lease rather than an unguarded ref update.

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
```

The generator binds canonical and consumer roots by directory file descriptor
for the complete transaction, uses cooperative root-bound locks, and rejects
symlink ancestors or targets. It also binds the fixed Git executable and each
repository's `.git` marker, admin/common directories, and object directory;
proves that fixed executable is Git 2.45 or newer and accepts the explicit
`--no-lazy-fetch` option, then double-reads those bound control files into a
parent-private frozen snapshot before any repository/object Git command. The
copied content-addressed object store is fully hashed once into a
path/identity/access/size/content manifest. Before repository Git runs, a
bounded `--file`/`--no-includes` parse of only the private `config` and optional
`config.worktree` snapshot rejects include directives and every partial-clone
or promisor key by presence, while direct object inventory rejects promisor and
alternate markers. Every later Git call carries `--no-lazy-fetch` and forces
`core.commitGraph=false` plus `core.multiPackIndex=false`, so copied derived
caches cannot redefine commit ancestry or object-to-pack offsets. It also
revalidates the complete pack/idx/loose manifest before and after the child;
stable file change signals avoid rereading pack bytes while still failing
closed if content stability can no longer be proved. Git calls use only that
snapshot, are deadline/output bounded, and retain defense-in-depth rejection
of replace refs and alternates. Canonical and consumer origins must match the
lock, roots must not overlap in either ancestry direction, and managed paths
may never enter the Git control plane. Target validation also rejects
case-insensitive/NFC collisions and overlap in either ancestry direction with
the receipt, transaction markers, or per-target exchange journals.

Directory inventories for Git controls, private objects, cleanup trees, and
the private tool root use incremental `scandir` collection. They request at
most `limit + 1` producer entries, retain at most `limit`, stop immediately on
the first overflow entry, and sort only the bounded retained set. Iterator
cleanup is guaranteed on overflow and producer errors; an entry cap is never
implemented by first allocating an unbounded `listdir` result. Each successful
scan reserves its retained names against both the recursive scan budget and
the operation-wide entry budget before child recursion begins, so unprocessed
sibling names cannot be multiplied by a deep first child.

The live Git binding includes `HEAD`, its resolved loose ref when present,
`packed-refs`, common/worktree config, the index, and explicit absence records
for optional controls. The source Git executable is identity/access-bound and
double-read into a SHA-256 content binding, then copied with exclusive creation
and rebound as a mode-0500 executable inside the private snapshot. Git launches
use that verified snapshot as `Popen(executable=...)` while the single-threaded
parent changes directory through the retained repository/private-snapshot
descriptor; no Python re-exec, `preexec_fn`, or pathname cwd is used. On macOS,
the sealed-system `/usr/bin/xcrun` locator selects the ordinary developer-tool
Git binary because the `/usr/bin/git` platform shim cannot execute from a byte
copy. The protected launch property is the exact snapshot byte sequence, not
macOS descriptor execution. Snapshot identity, bytes, and access policy are
revalidated immediately around each spawn. Private snapshots live below the
passwd-derived stable account-home namespace
`~/.codex-sync-canonical-mirrors-v1/`. The controller binds every component
from `/` to that exact account home with no-follow descriptor traversal: each
ancestor must be root/current-owned and not group/world writable, the home must
be current-owned, and the namespace parent must be current-owned mode `0700`.
It retains the home, namespace-parent, tool-root, and quarantine descriptors
through stale recovery, owner publication, and final owner cleanup. An absent
fixed directory is created under a random mode-0700 name and published with a
no-replace rename; a fixed name that appears after the initial absence receipt
is never adopted. The namespace excludes unauthorized other users but is not
claimed to stop a hostile same-UID process. Version-2 owner/phase records bind
the allocating root id. Version-1 records are accepted only in
`legacy-shared-v0`; a cross-root, missing, extra, or unknown-version root scope
is retained under `private-owner-root-mismatch` without moving either the
record or its private directory.

The old shared parent (`/private/tmp` on macOS, `/var/tmp` on Linux) is retained
as the non-allocating `legacy-shared-v0` recovery root. Before the primary root
is allocated, the controller validates that shared parent as exact root-owned
mode `1777` and audits both fixed children. A foreign-owned preclaim receives
two metadata-only identity/policy passes and is never opened or traversed, so
it cannot deny service to the stable account-home root. Same-UID legacy state
is leased and recovered only inside its original root; no evidence is moved or
copied across roots. If that root initially lacks a durable quarantine, any
tool-root entry blocks with zero recovery mutation; the controller does not
claim that in-tool-only cleanup is available at that boundary. Any retained
owner, journal, tool-root, or quarantine
entry—or an audit that cannot prove emptiness—fails closed as
`legacy-recovery-pending` or `private-control-root-inconclusive` before primary
allocation. Existing primary objects are descriptor-bound before that legacy
gate and revalidated afterward. Only exact bound-parent identity deduplicates a
whole registry root; a child alias under a distinct parent, a swapped role, or
a partial alias is inconclusive. The terminal registry pass covers every
legacy root even when an earlier root changed. Same-UID legacy tool/quarantine
leases are transferred into the primary retained context, remain held across
primary allocation and owner-record publication, and are released only after a
second whole-registry terminal revalidation at that publication boundary.
Every mutating root must prove strict parent-to-child containment without the
reciprocal ancestor relation; tool and quarantine must be disjoint and on the
same filesystem. Primary children must not overlap the repository, Git admin,
or Git common roots, while legacy children additionally must not overlap the
bound primary home, namespace, tool, or quarantine objects. These gates run
before stale recovery or owner/private-snapshot mutation.

An explicit retained-in-place recovery is the only exception to the legacy
evidence allocation block. It is limited to the registered
`legacy-shared-v0` root and never moves, copies, deletes, rewrites, or purges an
object in that root. Dry-run holds nonblocking exclusive leases in the normal
tool-root-to-quarantine order, rejects symlinks, special objects, hard-link
aliases, unsafe access policy, topology overlap, and configured resource caps,
then records every retained directory and regular file. Regular files use two
bounded streaming passes to bind size and the SHA-256 of all bytes without
retaining the file payload in memory; only recognized owner-record candidates
may capture their separately capped 4 KiB payload for closed-schema decoding.
Directories bind identity, access policy, and a recursive digest. Recognized
version-1 and version-2 owner records are linked to their exact retained private
directory, while unknown `.saved` content remains ordinary bound evidence
rather than being reclassified or discarded. The external plan is created once
as mode `0600` through a
no-follow parent walk and is deterministic for the same complete scan inputs,
including capacity signals. Allocated-block counts are capacity signals, not
protected mutation signals.

Review the dry-run plan before execution:

```bash
python3 scripts/codex_personal_sync.py recover-private-control \
  --root-id legacy-shared-v0 \
  --dry-run \
  --output-receipt /owner-private/recovery/PLAN.json

python3 scripts/codex_personal_sync.py recover-private-control \
  --root-id legacy-shared-v0 \
  --execute \
  --receipt /owner-private/recovery/PLAN.json
```

Execution reacquires the same exclusive leases and requires an exact protected
plan match before it creates the primary namespace. It durably publishes the
fixed primary receipt first and the fixed cutover marker second; marker
publication is the commit point. A receipt without a marker remains blocked
and retryable. Each publication attempt uses a nonce-bearing pending name, so
a short write, `fchmod`, or file `fsync` can leave an untrusted primary-side
pending artifact. If rename succeeds before its parent-directory `fsync`
fails, the fixed namespace, receipt, or marker may already be visible. Retry
does not overwrite it: it reopens the exact binding, re-`fsync`s the containing
directory, and revalidates identity, access policy, and content. An existing
marker additionally requires complete verification both before and after that
durability repair. A marker is accepted only after marker → exact receipt →
full legacy manifest verification plus a whole-registry terminal revalidation.
After that verification, new runtimes classify the legacy root as
`adopted-retained-in-place` and may allocate only in `primary-home-v1`; old
runtimes, which do not understand the marker, continue to stop at
`legacy-recovery-pending`. Repeated execution is idempotent. Physical purge of
the retained legacy tree is deliberately outside this command and still needs
a separately reviewed deletion contract.

Expected owner-record cleanup moves the exact file into the already retained
durable quarantine beside its bound private-control parent, on the same
verified filesystem. Recovery and cleanup never rebind that quarantine through
the namespace's lexical path.
Verified transaction temporaries, journals, and owner records use
identity/content-bound `transient-file-*` names and are retained for
identity-aware recovery alongside receipt-bound retired target bytes under
`recovery-file-*` names. The synchronizer never converts a filename prefix
alone into deletion authority; current and legacy unclassified entries remain
for manual inspection. Capacity is reserved under the quarantine lock before a
source pathname moves. At exactly 10,000 entries, the quarantine can still be
opened to validate and diagnose journal-bound recovery, but cleanup that would
retain another journal is blocked before that journal moves and the active
journal remains in place until identity-aware maintenance frees capacity.
Every operation that reaches private Git binding retains the exact owner-
cleanup evidence, including `refresh-lock --check` and a target-level
`generate` no-op. The no-op path below prevents only target transaction and
target-sibling quarantine churn; it does not suppress private-control evidence
growth.

`status-scheduler` and `doctor` audit the ordered `primary-home-v1`, then
`legacy-shared-v0` registry without modifying either root. They aggregate every
root result instead of stopping at the first problem, label exact parent
duplicates without traversing their children, and JSON
reports each root's id, reason, parent identity, and bounded evidence. Within a
same-UID root they bind the current-owner tool root first, take a nonblocking
shared lease, then bind and lease the durable quarantine in the generator's
tool-root-to-quarantine order. Foreign legacy children remain metadata-only.
If a quarantine exists without its coordination root, the audit reports
inconclusive without taking a quarantine lease. A concurrent writer, unstable namespace,
replacement, access-policy change, or unreadable evidence reports
`mirror-quarantine-audit-inconclusive`; an exact-capacity segment reports
`mirror-quarantine-saturated`. JSON includes the exact segment path, name,
identity, access policy, count/cap, and bounded owner recovery records. Strict
status and doctor exit unhealthy for either classification. Before aggregate
classification, a second read-only pass revalidates the absence or exact
identity/access-policy receipt for every registry root and reports coverage for
all roots even after an earlier failure. For same-UID roots it also enforces the
generator's shared topology invariants: strict one-way parent-to-child
containment plus same-filesystem, non-overlapping tool/quarantine roots.

The current flat quarantine has no automatic rollover. Existing version-2
exchange journals store only a quarantine basename, so searching that name
across new segments would make recovery ambiguous. Safe segmented retention
requires a separately reviewed journal-locator migration plus global
entry/logical/allocated-byte ceilings. Until that work lands, a saturated
production quarantine remains a blocking maintenance condition; status
reporting does not claim to repair it.
Canonical source reads and Git stdout use the same 32 MiB
per-file/per-command contract. Private owner records instead use a dedicated
4096+1-byte producer ceiling; both descriptor-stability reads consume the same
operation deadline and aggregate byte budget. The shared byte/entry/deadline
budgets cover snapshotting, Git execution, recovery, generation, and cleanup.

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

On macOS, a scheduler process without `TMPDIR` may receive `/tmp` from Python.
The synchronizer recognizes only the platform's exact `/tmp -> /private/tmp`
alias, normalizes it to `/private/tmp`, and opens that real directory with
no-follow semantics. Before yielding a temporary archive workspace it binds
and revalidates the alias object, exact link value and access policy together
with the target directory's object identity and access policy. Arbitrary leaf
symlinks and replacement races still fail closed. Directory timestamps and
ordinary child-entry churn are not treated as replacement or access-policy
changes.

## Scheduler operator runbook

Audit an existing host before changing it:

```bash
~/.codex/bin/codex-personal-sync status-scheduler --json --strict
~/.codex/bin/codex-personal-sync doctor --json --strict
```

The JSON report includes the audited `command`, `mode`, `repo`, `base_repo`,
`owner`, `interval_minutes`, and `migration_needed`, so the installed command
can be reconstructed without guessing. A legacy `install` or
`install-private` command sets `migration_needed: true`.

A bare repair preserves an existing audited mode, repository, base repository,
owner, and interval while migrating the command to the stable
`run-scheduled` entrypoint. On macOS it also migrates an exact historical
Background or loose GUI LaunchAgent to the canonical per-user Aqua
LaunchAgent:

```bash
~/.codex/bin/codex-personal-sync install-scheduler
```

The canonical macOS plist declares `LimitLoadToSessionType=Aqua` while keeping
the hardened `ProcessType=Background`, account-home `HOME` and
`WorkingDirectory`, `Umask=077`, `ThrottleInterval=60`, and `LowPriorityIO`
settings. Activation enables and bootstraps the exact label in `gui/$UID`.
`ProcessType=Background` controls launchd process policy; it does not move the
job out of the Aqua session.

This scheduler is therefore for a Mac with a live GUI login. Do not install it
on a headless macOS host: a role-aware controller may synchronize such a host,
but host inventory, role activation, and SSH fanout belong outside this
canonical repository. Linux keeps the existing per-user systemd behavior.

Migration accepts only exact historical profiles: the hardened Background job
in `user/$UID` and the earlier loose GUI job in `gui/$UID`. Activation first
removes or disables the exact Background identity, then replaces the exact GUI
identity with the hardened Aqua job. Status and uninstall continue to inspect
both domains for the canonical label and every managed legacy label, so stale
registration cannot hide a second instance. A healthy macOS scheduler has the
canonical job loaded only in `gui/$UID`.

Use explicit arguments only for an intentional target change or a first
installation. For example:

```bash
~/.codex/bin/codex-personal-sync install-scheduler \
  --mode private \
  --repo Joey-Tools/codex-private-workflows \
  --base-repo Joey-Tools/codex-toolbox \
  --owner private \
  --interval-minutes 60
```

Activation recovery is roll-forward: a failed native activation retains its
durable incomplete marker and the published configuration; it does not
silently restore the prior config. Re-run the audited install with the same
intent after resolving the reported failure. Successful Linux uninstall
removes only the owned service/timer files and preserves foreign `.d`
drop-ins, reporting them for manual disposition. Ordinary release install,
mirror automation, and retention dry-runs never install, reconfigure, or
remove a scheduler. Do not blindly reinstall hosts or delete a saturated
private-control quarantine; use the report evidence and the separately
reviewed recovery procedure.

To obtain a machine-readable identity for the active immutable releases, use:

```bash
~/.codex/bin/codex-personal-sync release-identities \
  --mode private \
  --owner private \
  --home ~/.codex \
  --json
```

The versioned response reports `sha` and `tree_sha256` for both the public and
private owners. Public mode reports only the public owner. The command holds
the installation lock without creating missing state, validates each current
release manifest and full tree, and rejects a `current` pointer change across
the validation boundary. It is read-only and does not infer a host role,
install a scheduler, or perform synchronization.

On Darwin, the active-release and release-inventory trust chain treats
access-policy safety, rather than raw ACL serialization, as the protected
property. Every bound directory descriptor is revalidated at installation and
final admission for its expected UID, a POSIX mode with no group/world-write
bits (`mode & 0o022 == 0`), and the absence of any extended-ACL `ALLOW` entry
that grants access to a non-owner. The mode gate runs before ACL parsing and
fails closed on any group/world-write bit. Safe group/world read and execute
bits remain acceptable. No extended ACL, a deny-only ACL, and owner-only
`ALLOW` entries are accepted; unknown or unreadable ACL state fails closed. A
retrieved ACL must pass `acl_valid` before enumeration; an invalid-argument
result is accepted as normal exhaustion only while requesting the next entry,
never while requesting the first entry. Once acquired, both the ACL object and
each qualifier object are always offered to `acl_free`. A cleanup failure does
not replace an existing primary admission or qualifier-decoding error; without
an existing primary, the cleanup failure itself fails admission closed.

Each Darwin access-policy decision uses one coherent sample on the same bound
descriptor: pre-ACL `fstat`, semantic ACL validation with complete cleanup, and
post-ACL `fstat`. Only a successful ACL phase proceeds to the post-ACL stat. If
object identity (device, inode, and type) and expected UID remain stable and
both observed modes remain safe, a `ctime` or safe-mode change triggers at most
one complete resample of that same sequence on the same descriptor; the change
is a retry trigger, not proof of an unsafe policy. Any observed object-identity
or expected-UID mismatch, group/world-write mode, or non-owner `ALLOW` entry
rejects admission. Continued drift after the resample, an ACL API failure, a
cleanup-only failure, or a post-ACL `fstat` failure fails closed as
unverifiable. Non-Darwin behavior remains unchanged.

A safe-to-safe change in raw ACL text or ordering, `ctime`, or other extended
attributes does not by itself prove that the access policy is unsafe because
each boundary repeats the semantic policy check. A `ctime` change is
nevertheless always a revalidation trigger and is never ignored as content
proof. For a regular file with only
`ctime` drift, the same retained descriptor performs at most one bounded rehash
of the retained `size || SHA-256`, then terminally binds ACL safety, object
metadata, the settled `ctime`, and the canonical name. This once-only budget is
per retained-FD stage: the capture `file_fd` and a later reopened verification
`entry_fd` are independent stages and each has its own single allowance. A
second `ctime` drift within one stage fails closed before another content read.
A directory with only `ctime` drift is not byte-hashed. Its retained parent
descriptor must reproduce the exact member names and, for every immediate
child, the exact device, inode, type, mode, size, `mtime`, and `ctime`;
applying that rule at every directory binds the tree recursively. Initial
snapshot construction performs terminal and stable member rescans on that
retained directory descriptor after visiting its children, including the
immediate-child identity check. Later verification adds a deepest-first
postorder directory admission pass, so each parent is finalized after its
children. Each directory uses a constant number of member-count-bounded scans
under the existing tree limits; the protocol neither retains an unbounded FD
set nor rehashes file content merely because a directory `ctime` changed. This
ACL contract does not relax independent bound-directory change signals,
including directory `mtime`; create-and-remove child-entry churn can still
reject revalidation.

Every successful Darwin installation retains the complete descriptor chain
from the synchronization home through each next-current release and
revalidates that chain's identity and access-policy safety at final admission.
This includes an exact no-op and a managed-link-only update; an unchanged
`current` pointer does not bypass the chain gate. The install directory-chain
container owns the deduplicated set of unique strict-ancestor descriptors
across owners. Each release binding's `releases_fd` and `release_fd` remain
borrowed rather than transferring into that container, and are revalidated
separately.

New immutable release content may be staged and canonically published before
the activation headroom probe. If the later probe fails, that inactive
candidate may remain installed. Earlier valid retention or pending recovery,
ready-batch cleanup, and lock, root, or state-directory scaffolding may also
precede the probe and remain. After initial validation of the ancestor chains
and borrowed release bindings, every non-no-op Darwin install, including a
managed-link-only update, performs a point-in-time 80-FD `dup` headroom probe
before starting this attempt's new pending activation transaction. On probe
failure, its pending records, pointer publication, `current` and managed-link
changes, `managed-links.json` publication, and commit marker have not begun.
Every probe descriptor is closed on both success and failure. An exact no-op
still validates its chains but skips this mutation-only probe; a
managed-link-only update does not. The probe demonstrates capacity only at
that instant and does not claim protection from concurrent same-process FD
churn. Probe failure leaves semantic activation state unchanged; preceding
valid recovery, cleanup, scaffolding, and inactive candidate publication are
not rolled back. A same-SHA retry revalidates and may reuse the inactive
candidate, while ordinary retention or pruning may remove an unreferenced
candidate. If managed-state prepublication detects ACL drift, the stable error
remains `current-release-unverifiable`. Non-Darwin behavior is unchanged. An
unsafe inherited ACL can therefore block installation or final
active-inventory admission even when POSIX mode bits look restrictive; have
an administrator remove the unsafe inherited ACL and retry.

## Test

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m compileall -q scripts tests
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
```
