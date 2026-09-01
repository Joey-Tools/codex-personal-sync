from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_pending_agent_claim_compatibility",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40
ROLE_TARGET = PurePosixPath("agents/reviewer.toml")
UNCHANGED_ROLE_TARGET = PurePosixPath("agents/worker.toml")


def write_agent_release(
    root: Path,
    *,
    payload: str = 'name = "reviewer"\n',
    unchanged_payload: str | None = None,
    owner: str = MODULE.PUBLIC_OWNER,
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    source = root / "personal_codex" / "agents" / "reviewer.toml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(payload, encoding="utf-8")
    links: list[dict[str, object]] = [
        {
            "source": "personal_codex/agents/reviewer.toml",
            "target": ROLE_TARGET.as_posix(),
            "kind": "file",
            "owner": owner,
        }
    ]
    if override:
        links[0]["override"] = True
    if unchanged_payload is not None:
        unchanged_source = root / "personal_codex" / "agents" / "worker.toml"
        unchanged_source.write_text(unchanged_payload, encoding="utf-8")
        links.append(
            {
                "source": "personal_codex/agents/worker.toml",
                "target": UNCHANGED_ROLE_TARGET.as_posix(),
                "kind": "file",
                "owner": owner,
            }
        )
    manifest_payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": links,
    }
    if base_sha is not None:
        manifest_payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str = SHA_A) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


class PendingAgentClaimCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retain_batch(
        self,
        home: Path,
        release: Path,
        *,
        committed: bool,
        legacy_symlink: bool,
        sha: str = SHA_A,
    ) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_pointer(
            selected_home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            retained_phase = "after" if committed else "before"
            if phase == retained_phase:
                raise MODULE.SyncError("injected pending pointer retention")
            real_clear(selected_home, batch, phase=phase)

        expected_error = (
            "committed managed state but finalization failed"
            if committed
            else "rollback was incomplete"
        )
        with contextlib.ExitStack() as stack:
            if legacy_symlink:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_entry_materializes_regular_file",
                        return_value=False,
                    )
                )
            if not committed:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_publish_pending_commit_marker",
                        side_effect=MODULE.SyncError("injected precommit crash"),
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "_clear_pending_link_pointer",
                    side_effect=retain_pointer,
                )
            )
            with self.assertRaisesRegex(MODULE.SyncError, expected_error):
                install(release, home, sha)
            batch = MODULE._load_pending_link_batch(home)
            self.assertIsNotNone(batch)
            assert batch is not None
            return batch

    def _downgrade_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
    ) -> None:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["version"] = version
        if version < 7:
            payload.pop("terminal_regular_before")
            payload.pop("terminal_regular_after")
        if version < 6:
            for raw_record in payload["records"]:
                for field in (
                    "materialization",
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    raw_record.pop(field)
                planned = raw_record["planned_before"]
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    planned.pop(field)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _drop_cleanup_index(self, home: Path) -> None:
        index = MODULE._pending_cleanup_index_path(home)
        for child in index.iterdir():
            child.unlink()
        index.rmdir()

    def _terminal_regular_budget_fixture(
        self,
        label: str,
    ) -> tuple[Path, MODULE.ManagedState, int]:
        home = self.root / f"home-terminal-budget-{label}"
        reviewer_payload = 'name = "reviewer-budget"\n'
        worker_payload = 'name = "worker-budget"\n'
        sources = {
            ROLE_TARGET: (
                PurePosixPath("personal_codex/agents/reviewer.toml"),
                reviewer_payload,
            ),
            UNCHANGED_ROLE_TARGET: (
                PurePosixPath("personal_codex/agents/worker.toml"),
                worker_payload,
            ),
        }
        links: dict[PurePosixPath, MODULE.ManagedLinkRecord] = {}
        for target, (source, payload) in sources.items():
            release_source = (
                MODULE._releases_root(home, MODULE.PUBLIC_OWNER)
                / SHA_A
                / Path(*source.parts)
            )
            release_source.parent.mkdir(parents=True, exist_ok=True)
            release_source.write_text(payload, encoding="utf-8")
            installed_target = home / Path(*target.parts)
            installed_target.parent.mkdir(parents=True, exist_ok=True)
            installed_target.write_text(payload, encoding="utf-8")
            installed_target.chmod(0o600)
            links[target] = MODULE.ManagedLinkRecord(
                source=source,
                target=target,
                kind="file",
                owner=MODULE.PUBLIC_OWNER,
                link_target=MODULE._relative_managed_link_target(
                    source,
                    target,
                    MODULE.PUBLIC_OWNER,
                ),
                release_sha=SHA_A,
            )
        return (
            home,
            MODULE.ManagedState(
                owners={MODULE.PUBLIC_OWNER: SHA_A},
                links=links,
            ),
            len(reviewer_payload.encode("utf-8"))
            + len(worker_payload.encode("utf-8")),
        )

    def test_v4_v5_agent_symlink_claims_parse_and_recover(self) -> None:
        for version in (4, 5):
            for committed in (False, True):
                with self.subTest(version=version, committed=committed):
                    home = self.root / f"home-v{version}-{committed}"
                    release = self.root / f"release-v{version}-{committed}"
                    write_agent_release(release)
                    batch = self._retain_batch(
                        home,
                        release,
                        committed=committed,
                        legacy_symlink=True,
                    )
                    self._downgrade_metadata(batch, version)

                    parsed = MODULE._load_pending_link_batch(home)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertEqual(parsed.metadata_version, version)
                    self.assertIn(
                        ROLE_TARGET,
                        {
                            claim.target
                            for claim in parsed.claims_after
                            if claim.scope == "managed"
                        },
                    )

                    state, snapshot = MODULE._load_managed_state_with_snapshot(home)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _state, _snapshot, recovered = (
                            MODULE._recover_pending_link_transaction(
                                home,
                                state,
                                snapshot,
                                dry_run=False,
                            )
                        )
                    self.assertTrue(recovered)
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(home))
                    )
                    target = home / ROLE_TARGET
                    self.assertEqual(target.is_symlink(), committed)

    def test_v6_omits_regular_agent_claim_and_rejects_legacy_extra_claim(
        self,
    ) -> None:
        home = self.root / "home-v6"
        release = self.root / "release-v6"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 6)

        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, 6)
        self.assertNotIn(
            ROLE_TARGET,
            {
                claim.target
                for claim in parsed.claims_after
                if claim.scope == "managed"
            },
        )

        state_record = parsed.state_after_value.links[ROLE_TARGET]
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        claims_after = payload["claims_after"]
        claims_after.append(
            {
                "index": len(claims_after),
                "scope": "managed",
                "target": ROLE_TARGET.as_posix(),
                "kind": state_record.kind,
                "source": state_record.source.as_posix(),
                "owner": state_record.owner,
                "link_target": state_record.link_target,
                "release_sha": state_record.release_sha,
                "parent_identity": [0, 0],
                "link_identity": [0, 0],
                "evidence": (
                    f"pending/claims/after/{len(claims_after) - 1:08d}"
                ),
            }
        )
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "after claims do not exactly match state",
        ):
            MODULE._load_pending_link_batch(home)

    def test_legacy_agent_symlink_migrates_to_regular_on_update(self) -> None:
        home = self.root / "home-legacy-update"
        first = self.root / "release-legacy-update-a"
        second = self.root / "release-legacy-update-b"
        write_agent_release(first, payload='name = "legacy"\n')
        write_agent_release(second, payload='name = "regular"\n')

        with mock.patch.object(
            MODULE,
            "_entry_materializes_regular_file",
            return_value=False,
        ):
            install(first, home, SHA_A)
        self.assertTrue((home / ROLE_TARGET).is_symlink())

        install(second, home, SHA_B)

        target = home / ROLE_TARGET
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "regular"\n')

    def test_legacy_overlay_symlink_migrates_to_regular_on_uninstall(self) -> None:
        home = self.root / "home-legacy-uninstall"
        public = self.root / "release-legacy-uninstall-public"
        private = self.root / "release-legacy-uninstall-private"
        write_agent_release(public, payload='provider = "public"\n')
        write_agent_release(
            private,
            payload='provider = "private"\n',
            owner="private",
            override=True,
            base_sha=SHA_A,
        )

        with mock.patch.object(
            MODULE,
            "_entry_materializes_regular_file",
            return_value=False,
        ):
            install(public, home, SHA_A)
            install(private, home, SHA_B)
        self.assertTrue((home / ROLE_TARGET).is_symlink())

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(home, "private", dry_run=False)

        target = home / ROLE_TARGET
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'provider = "public"\n')

    def test_v6_terminal_regular_authority_is_action_scoped(self) -> None:
        home = self.root / "home-v6-action-scoped"
        first = self.root / "release-v6-action-scoped-a"
        second = self.root / "release-v6-action-scoped-b"
        unchanged_payload = 'name = "worker"\n'
        write_agent_release(
            first,
            payload='name = "reviewer-a"\n',
            unchanged_payload=unchanged_payload,
        )
        write_agent_release(
            second,
            payload='name = "reviewer-b"\n',
            unchanged_payload=unchanged_payload,
        )
        install(first, home, SHA_A)
        batch = self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )
        self._downgrade_metadata(batch, 6)

        parsed = MODULE._load_pending_link_batch(home)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(
            {target.target for target in parsed.terminal_regular_before},
            {ROLE_TARGET},
        )
        self.assertEqual(
            {target.target for target in parsed.terminal_regular_after},
            {ROLE_TARGET},
        )

    def test_terminal_regular_staging_preflights_each_whole_phase_group(
        self,
    ) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("stage")

        for phase in ("before", "after"):
            with self.subTest(phase=phase, limit="L-1"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size - 1,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_file_snapshot_beneath",
                        wraps=MODULE._read_regular_file_snapshot_beneath,
                    ) as read_snapshot,
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "terminal regular.*aggregate size limit",
                    ),
                ):
                    MODULE._stage_pending_terminal_regular_targets(
                        home,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_snapshot.call_count, 0)
                self.assertEqual(read_source.call_count, 0)

            with self.subTest(phase=phase, limit="L"):
                with mock.patch.object(
                    MODULE,
                    "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                    group_size,
                ):
                    expectations = MODULE._stage_pending_terminal_regular_targets(
                        home,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(
                    sum(expectation.size for expectation in expectations),
                    group_size,
                )

    def test_terminal_regular_source_validation_preflights_whole_group(
        self,
    ) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("parse")

        for phase in ("before", "after"):
            expectations = MODULE._stage_pending_terminal_regular_targets(
                home,
                state,
                (),
                phase=phase,
            )
            with self.subTest(phase=phase, limit="L-1"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size - 1,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "terminal regular.*aggregate size limit",
                    ),
                ):
                    MODULE._validate_pending_terminal_regular_targets_for_state(
                        home,
                        expectations,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_source.call_count, 0)

            with self.subTest(phase=phase, limit="L"):
                with (
                    mock.patch.object(
                        MODULE,
                        "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                        group_size,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_regular_source_payload",
                        wraps=MODULE._read_regular_source_payload,
                    ) as read_source,
                ):
                    MODULE._validate_pending_terminal_regular_targets_for_state(
                        home,
                        expectations,
                        state,
                        (),
                        phase=phase,
                    )
                self.assertEqual(read_source.call_count, len(expectations))

    def test_final_regular_verification_preflights_three_pass_group(self) -> None:
        home, state, group_size = self._terminal_regular_budget_fixture("final")
        expectations = MODULE._stage_pending_terminal_regular_targets(
            home,
            state,
            (),
            phase="after",
        )
        ticket = MODULE.PendingBatchCleanupTicket(
            version=4,
            phase="after",
            path=self.root / "terminal-budget-ticket.json",
            snapshot=MODULE.ManagedStateFileSnapshot(
                exists=True,
                file_identity=(1, 2),
            ),
            batch_root=self.root / "terminal-budget-batch",
            batch_root_identity=(3, 4),
            marker_path=PurePosixPath("pending/state/commit.json"),
            marker_parent_identity=(5, 6),
            marker_file_identity=(7, 8),
            marker_mode=0o600,
            marker_sha256="f" * 64,
            terminal_regular_targets=expectations,
        )

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                group_size - 1,
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=ticket,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                wraps=MODULE._read_regular_file_snapshot_beneath,
            ) as read_snapshot,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "terminal regular.*aggregate size limit",
            ),
        ):
            MODULE._verify_final_regular_targets(home, ticket)
        self.assertEqual(read_ticket.call_count, 0)
        self.assertEqual(read_snapshot.call_count, 0)

        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                group_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=ticket,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                wraps=MODULE._read_regular_file_snapshot_beneath,
            ) as read_snapshot,
        ):
            MODULE._verify_final_regular_targets(home, ticket)
        self.assertEqual(read_ticket.call_count, 4)
        self.assertEqual(read_snapshot.call_count, 3 * len(expectations))

    def test_v6_active_pointer_recovers_without_legacy_staging_authority(
        self,
    ) -> None:
        home = self.root / "home-v6-no-staging-authority"
        release = self.root / "release-v6-no-staging-authority"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 6)
        self._drop_cleanup_index(home)

        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        state, snapshot = MODULE._load_managed_state_with_snapshot(home)
        with contextlib.redirect_stdout(io.StringIO()):
            _state, _snapshot, recovered = MODULE._recover_pending_link_transaction(
                home,
                state,
                snapshot,
                dry_run=False,
            )

        self.assertTrue(recovered)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(home)))

    def test_v6_missing_cleanup_index_rejects_partial_staging_authority(
        self,
    ) -> None:
        for residue in ("marker", "temp", "retained"):
            with self.subTest(residue=residue):
                home = self.root / f"home-v6-partial-{residue}"
                release = self.root / f"release-v6-partial-{residue}"
                write_agent_release(release)
                batch = self._retain_batch(
                    home,
                    release,
                    committed=True,
                    legacy_symlink=False,
                )
                self._downgrade_metadata(batch, 6)
                self._drop_cleanup_index(home)
                parsed = MODULE._load_pending_link_batch(home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                marker = batch.batch_root / Path(
                    *MODULE.PENDING_STATE_STAGING_MARKER.parts
                )
                if residue == "marker":
                    residue_path = marker
                elif residue == "temp":
                    residue_path = marker.with_name(
                        marker.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
                    )
                else:
                    residue_path = marker.with_name(
                        f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{marker.name}-"
                        "123-0123456789abcdef"
                    )
                residue_path.write_bytes(b"partial\n")
                residue_path.chmod(0o600)

                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "partial legacy staging cleanup authority",
                ):
                    MODULE._read_or_restore_active_pointer_cleanup_ticket(
                        home,
                        parsed,
                    )

    def test_v7_active_pointer_rejects_missing_cleanup_index(self) -> None:
        home = self.root / "home-v7-no-cleanup-index"
        release = self.root / "release-v7-no-cleanup-index"
        write_agent_release(release)
        self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._drop_cleanup_index(home)
        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "cleanup index is missing: metadata v7",
        ):
            MODULE._read_or_restore_active_pointer_cleanup_ticket(home, parsed)

    def test_pending_backup_keeps_unreadable_distinct_from_absent(self) -> None:
        home = self.root / "home-backup-tristate"
        first = self.root / "release-backup-tristate-a"
        second = self.root / "release-backup-tristate-b"
        write_agent_release(first, payload='name = "reviewer-a"\n')
        write_agent_release(second, payload='name = "reviewer-b"\n')
        install(first, home, SHA_A)
        batch = self._retain_batch(
            home,
            second,
            committed=True,
            legacy_symlink=False,
            sha=SHA_B,
        )
        record = next(record for record in batch.records if record.backup is not None)
        assert record.backup is not None
        backup = batch.batch_root / Path(*record.backup.parts)
        real_stat = os.stat

        def deny_backup(name, *args, dir_fd=None, **kwargs):
            if dir_fd is not None and name == backup.name:
                raise PermissionError("injected unreadable backup")
            return real_stat(name, *args, dir_fd=dir_fd, **kwargs)

        with (
            mock.patch.object(MODULE.os, "stat", side_effect=deny_backup),
            self.assertRaisesRegex(MODULE.SyncError, "pending backup is unreadable"),
        ):
            MODULE._pending_record_backup_snapshot(home, batch, record)

        self.assertTrue(os.path.lexists(backup))
        self.assertTrue(os.path.lexists(MODULE._pending_link_pointer_path(home)))

        backup.rename(backup.with_name(backup.name + ".missing"))
        self.assertIsNone(
            MODULE._pending_record_backup_snapshot(home, batch, record)
        )

    def test_private_regular_files_override_a_fully_restrictive_umask(self) -> None:
        home = self.root / "home-umask-0777"
        target_parent = home / "agents"
        target_parent.mkdir(parents=True, mode=0o700)
        source = home / "source.toml"
        source.write_text('name = "reviewer"\n', encoding="utf-8")
        target = target_parent / "reviewer.toml"
        internal = home / "authority.json"
        real_mkdir = os.mkdir

        def create_safe_directory(name, mode=0o777, *, dir_fd=None):
            real_mkdir(name, mode, dir_fd=dir_fd)
            os.chmod(name, mode, dir_fd=dir_fd)

        prior_umask = os.umask(0o777)
        try:
            MODULE._create_regular_file_beneath(home, source, target)
            MODULE._write_exclusive_internal_file(home, internal, b"authority\n")
            with mock.patch.object(
                MODULE.os,
                "mkdir",
                side_effect=create_safe_directory,
            ):
                batch_root = MODULE._quarantine_batch_root(home, [])
        finally:
            os.umask(prior_umask)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(internal.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((batch_root / "metadata.json").stat().st_mode),
            0o600,
        )

    def test_full_install_still_succeeds_under_the_normal_umask(self) -> None:
        home = self.root / "home-normal-umask"
        release = self.root / "release-normal-umask"
        write_agent_release(release)

        install(release, home, SHA_A)

        target = home / ROLE_TARGET
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_internal_file_fchmod_failure_clears_the_final_name(self) -> None:
        home = self.root / "home-internal-fchmod-failure"
        home.mkdir()
        path = home / "authority.json"

        with (
            mock.patch.object(
                MODULE.os,
                "fchmod",
                side_effect=OSError("injected fchmod failure"),
            ),
            self.assertRaisesRegex(OSError, "injected fchmod failure"),
        ):
            MODULE._write_exclusive_internal_file(home, path, b"authority\n")

        self.assertFalse(os.path.lexists(path))


if __name__ == "__main__":
    unittest.main()
