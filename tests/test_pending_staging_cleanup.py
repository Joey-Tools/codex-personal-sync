from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_regular_agent_materialization import (
    MODULE,
    ROLE_TARGET,
    SHA_A,
    SHA_B,
    install,
    write_release,
)


class PendingStagingCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.first_release = self.root / "first-release"
        self.next_release = self.root / "next-release"
        write_release(self.first_release, role_payload='name = "first"\n')
        write_release(self.next_release, role_payload='name = "next"\n')
        install(self.first_release, self.home, SHA_A)
        self.target = self.home / ROLE_TARGET

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_existing_quarantine_directory_is_opened_once(self) -> None:
        batch_root = self.root / "batch-existing-directory"
        child = batch_root / "pending"
        child.mkdir(parents=True)
        root_fd = os.open(
            batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        real_open = MODULE.os.open
        opened_child_fds: list[int] = []

        def track_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == child.name:
                opened_child_fds.append(fd)
            return fd

        returned_fd = -1
        try:
            with mock.patch.object(MODULE.os, "open", side_effect=track_open):
                returned_fd = MODULE._create_quarantine_batch_directory_at(
                    root_fd,
                    (child.name,),
                )
            self.assertEqual(opened_child_fds, [returned_fd])
        finally:
            if returned_fd >= 0:
                os.close(returned_fd)
            os.close(root_fd)

    def test_quarantine_directory_open_closes_child_on_fsync_error(self) -> None:
        batch_root = self.root / "batch-directory-fsync-error"
        batch_root.mkdir()
        root_fd = os.open(
            batch_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        real_open = MODULE.os.open
        opened_child_fds: list[int] = []

        def track_open(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == "pending":
                opened_child_fds.append(fd)
            return fd

        try:
            with (
                mock.patch.object(MODULE.os, "open", side_effect=track_open),
                mock.patch.object(
                    MODULE.os,
                    "fsync",
                    side_effect=OSError("injected directory fsync failure"),
                ),
                self.assertRaisesRegex(OSError, "injected directory fsync failure"),
            ):
                MODULE._create_quarantine_batch_directory_at(
                    root_fd,
                    ("pending",),
                )
            self.assertEqual(len(opened_child_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_child_fds[0])
        finally:
            os.close(root_fd)

    def _fail_after_live_preimage_hardlink(self):
        real_publish = MODULE._publish_regular_hardlink_beneath
        tripped = False

        def publish_then_fail(
            home: Path,
            source: Path,
            destination: Path,
            expected_source,
        ):
            nonlocal tripped
            published = real_publish(
                home,
                source,
                destination,
                expected_source,
            )
            if (
                not tripped
                and source == self.target
                and destination.parent.name == "before"
                and destination.parent.parent.name == "pending"
            ):
                tripped = True
                raise MODULE.SyncError(
                    "injected failure after durable live preimage hardlink"
                )
            return published

        return mock.patch.object(
            MODULE,
            "_publish_regular_hardlink_beneath",
            side_effect=publish_then_fail,
        )

    def _only_cleanup_ticket(self):
        tickets = list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        self.assertEqual(len(tickets), 1)
        ticket = MODULE._read_pending_cleanup_ticket(self.home, tickets[0])
        self.assertIsNotNone(ticket)
        assert ticket is not None
        return ticket

    def _alternate_gid(self, current_gid: int) -> int:
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != current_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        assert alternate_gid is not None
        return alternate_gid

    def _legacy_cleanup_ticket_payload(
        self,
        batch_root: Path,
        *,
        version: int,
    ) -> bytes:
        self.assertIn(version, {1, 2, 3, 4})
        marker_path = {
            1: MODULE.PENDING_STATE_COMMIT_MARKER,
            2: MODULE.PENDING_STATE_ROLLBACK_MARKER,
            3: MODULE.PENDING_STATE_STAGING_MARKER,
            4: MODULE.PENDING_STATE_COMMIT_MARKER,
        }[version]
        marker_parent = batch_root / Path(*marker_path.parent.parts)
        marker_parent.mkdir(parents=True, exist_ok=True)
        marker = MODULE._write_exclusive_internal_file(
            self.home,
            marker_parent / marker_path.name,
            b"legacy cleanup marker\n",
        )
        self.assertIsNotNone(marker.parent_identity)
        self.assertIsNotNone(marker.file_identity)
        assert marker.parent_identity is not None
        assert marker.file_identity is not None
        batch_identity = (batch_root.stat().st_dev, batch_root.stat().st_ino)
        digest = hashlib.sha256(marker.payload or b"").hexdigest()
        if version == 1:
            return MODULE._pending_cleanup_ticket_payload(
                batch_root,
                batch_identity,
                marker.parent_identity,
                marker.file_identity,
                0o600,
                digest,
            )
        payload: dict[str, object] = {
            "version": version,
            "batch": batch_root.name,
            "batch_root_identity": MODULE._identity_payload(batch_identity),
            "finalization_marker": {
                "phase": {2: "before", 3: "staging", 4: "after"}[version],
                "path": marker_path.as_posix(),
                "parent_identity": MODULE._identity_payload(marker.parent_identity),
                "file_identity": MODULE._identity_payload(marker.file_identity),
                "mode": 0o600,
                "sha256": digest,
            },
        }
        if version == 4:
            payload["terminal_regular_targets"] = []
        return MODULE._bounded_json_document(
            payload,
            max_bytes=MODULE.MAX_PENDING_TERMINAL_CLEANUP_TICKET_BYTES,
            overflow_error="pending cleanup ticket exceeds the size limit",
        )

    def _publish_legacy_cleanup_ticket(
        self,
        *,
        version: int,
    ) -> MODULE.PendingBatchCleanupTicket:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        payload = self._legacy_cleanup_ticket_payload(batch_root, version=version)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            MODULE._pending_cleanup_index_path(self.home),
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        MODULE._publish_pending_cleanup_ticket(self.home, ticket_path, payload)
        ticket = MODULE._read_pending_cleanup_ticket(self.home, ticket_path)
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.version, version)
        return ticket

    def _stage_legacy_symlink_pointer(self, metadata_version: int):
        legacy_home = self.root / f"legacy-home-v{metadata_version}"
        first_release = self.root / f"legacy-first-v{metadata_version}"
        next_release = self.root / f"legacy-next-v{metadata_version}"
        write_release(first_release)
        write_release(next_release)
        install(first_release, legacy_home, SHA_A)
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected active pointer retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, legacy_home, SHA_B)

        batch = MODULE._load_pending_link_batch(legacy_home)
        self.assertIsNotNone(batch)
        assert batch is not None
        MODULE._clear_pending_link_pointer(legacy_home, batch, phase="before")
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        data["version"] = metadata_version
        for field in ("state_before", "state_after", "commit_evidence"):
            data[field].pop("uid")
            data[field].pop("gid")
        data.pop("terminal_regular_before", None)
        data.pop("terminal_regular_after", None)
        for record in data["records"]:
            record.pop("publication_cleanup")
        if metadata_version < 6:
            for record in data["records"]:
                for field in (
                    "materialization",
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    record.pop(field)
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    record["planned_before"].pop(field)
        metadata_path.write_bytes(
            MODULE._bounded_json_document(
                data,
                max_bytes=MODULE.MAX_MANAGED_STATE_BYTES,
                overflow_error="pending link transaction metadata exceeds the size limit",
            )
        )
        os.link(
            metadata_path,
            MODULE._pending_link_pointer_path(legacy_home),
            follow_symlinks=False,
        )
        restored_batch = MODULE._load_pending_link_batch(legacy_home)
        self.assertIsNotNone(restored_batch)
        assert restored_batch is not None
        ticket_path = MODULE._pending_cleanup_ticket_path(
            legacy_home,
            restored_batch.batch_root.name,
        )
        ticket_path.unlink()
        ticket_path.parent.rmdir()
        return legacy_home, restored_batch

    def test_staging_failure_immediately_removes_live_preimage_hardlink(self) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected failure after durable live preimage hardlink",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "first"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_marker_only_staging_batch_is_discovered_on_next_run(self) -> None:
        real_publish = MODULE._publish_pending_batch_cleanup_ticket_for_root
        tripped = False

        def fail_after_marker(
            home: Path,
            batch_root: Path,
            payload: bytes,
        ) -> None:
            nonlocal tripped
            marker = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
            if not tripped and marker.is_file():
                tripped = True
                raise MODULE.SyncError("injected failure before staging ticket")
            real_publish(home, batch_root, payload)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
                side_effect=fail_after_marker,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected failure before staging ticket",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(ticket_root.exists() and list(ticket_root.glob("*.json")))
        quarantine = MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        marker_batches = [
            batch
            for batch in quarantine.iterdir()
            if (batch / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)).is_file()
        ]
        self.assertEqual(len(marker_batches), 1)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(marker_batches[0].exists())

    def test_no_ticket_batch_with_symlinked_marker_parent_fails_closed(self) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        outside = self.root / "outside-marker"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")
        (batch_root / "pending").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending staging marker changed",
        ):
            MODULE._discover_pending_staging_markers(self.home)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_foreign_hardlink_race_is_not_adopted_as_transaction_state(self) -> None:
        real_publish = MODULE._publish_regular_hardlink_beneath
        foreign_alias = self.root / "foreign-live-alias"
        tripped = False

        def publish_then_add_foreign_alias(
            home: Path,
            source: Path,
            destination: Path,
            expected_source,
        ):
            nonlocal tripped
            published = real_publish(
                home,
                source,
                destination,
                expected_source,
            )
            if (
                not tripped
                and source == self.target
                and destination.parent.name == "before"
                and destination.parent.parent.name == "pending"
            ):
                self.assertEqual(source.stat().st_nlink, 2)
                os.link(source, foreign_alias, follow_symlinks=False)
                self.assertEqual(source.stat().st_nlink, 3)
                tripped = True
            return published

        with (
            mock.patch.object(
                MODULE,
                "_publish_regular_hardlink_beneath",
                side_effect=publish_then_add_foreign_alias,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending live regular preimage changed during staging",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertTrue(foreign_alias.is_file())
        self.assertEqual(self.target.stat().st_nlink, 2)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_next_run_cleans_retained_staging_ticket_before_nlink_validation(
        self,
    ) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=MODULE.SyncError("injected interrupted cleanup"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "exact cleanup was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        self.assertEqual(ticket.phase, "staging")
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertEqual(self.target.stat().st_nlink, 2)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_moved_batch_without_empty_proof_retains_cleanup_authority(self) -> None:
        with (
            self._fail_after_live_preimage_hardlink(),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=MODULE.SyncError("injected interrupted cleanup"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "exact cleanup was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        moved_batch = ticket.batch_root.with_name(ticket.batch_root.name + "-moved")
        ticket.batch_root.rename(moved_batch)

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)

        self.assertIn("missing without an exact empty proof", stdout.getvalue())
        self.assertTrue(ticket.path.is_file())
        self.assertTrue(moved_batch.is_dir())
        self.assertEqual(self.target.stat().st_nlink, 2)

        moved_batch.rename(ticket.batch_root)
        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(ticket.path.exists())

    def test_pointer_recovery_retires_staging_ticket_before_rollback(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected staging authority retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self.assertEqual(self.target.stat().st_nlink, 2)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_pointer_recovers_after_ticket_deleted_before_marker(self) -> None:
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def fail_marker_delete(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if label == "pending staging cleanup marker":
                raise MODULE.SyncError("injected staging marker retention")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=fail_marker_delete,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_active_pointer_recovers_staging_ticket_tombstone(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_retire_pending_staging_cleanup_authority",
                side_effect=MODULE.SyncError("injected staging authority retention"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 3)
        marker = ticket.batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def crash_after_ticket_isolation(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if path == ticket.path:
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                raise SystemExit("injected staging ticket tombstone crash")
            real_delete(
                home,
                path,
                parent_fd,
                expected,
                label=label,
            )

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=crash_after_ticket_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "tombstone crash"),
        ):
            install(self.next_release, self.home, SHA_B)

        self.assertFalse(ticket.path.exists())
        self.assertTrue(marker.is_file())
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        retained = list(
            ticket.path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{ticket.path.name}-*"
            )
        )
        self.assertEqual(len(retained), 1)

        install(self.next_release, self.home, SHA_B)

        self.assertEqual(self.target.read_text(encoding="utf-8"), 'name = "next"\n')
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertFalse(retained[0].exists())

    def test_publisher_recovers_retained_ticket_temp(self) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        metadata = batch_root.stat()
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        temp_path = ticket_path.with_name(
            batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
        )
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"{\n")
        temp_path.chmod(0o600)
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def crash_after_temp_isolation(
            home: Path,
            path: Path,
            parent_fd: int,
            expected,
            *,
            label: str,
        ) -> None:
            if path == temp_path:
                retained = next(MODULE._retained_pending_cleanup_names(path))
                MODULE._rename_noreplace_at(
                    parent_fd,
                    path.name,
                    parent_fd,
                    retained,
                )
                os.fsync(parent_fd)
                raise SystemExit("injected temp tombstone crash")
            real_delete(home, path, parent_fd, expected, label=label)

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=crash_after_temp_isolation,
            ),
            self.assertRaisesRegex(SystemExit, "temp tombstone crash"),
        ):
            MODULE._discard_incomplete_pending_cleanup_ticket(self.home, temp_path)

        retained = list(
            temp_path.parent.glob(
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{temp_path.name}-*"
            )
        )
        self.assertEqual(len(retained), 1)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))
        payload = MODULE._pending_cleanup_ticket_payload(
            batch_root,
            (metadata.st_dev, metadata.st_ino),
            (1, 1),
            (1, 2),
            0o600,
            "a" * 64,
        )

        MODULE._publish_pending_cleanup_ticket(self.home, ticket_path, payload)

        self.assertEqual(ticket_path.read_bytes(), payload)
        self.assertFalse(retained[0].exists())
        self.assertFalse(os.path.lexists(temp_path))

    def test_legacy_active_pointer_without_cleanup_directory_recovers_only_before_v6(
        self,
    ) -> None:
        for metadata_version in (4, 5, 6):
            with self.subTest(metadata_version=metadata_version):
                legacy_home, batch = self._stage_legacy_symlink_pointer(
                    metadata_version
                )
                state, state_snapshot = MODULE._load_managed_state_with_snapshot(
                    legacy_home
                )
                if metadata_version < 6:
                    recovered, _snapshot, did_recover = (
                        MODULE._recover_pending_link_transaction(
                            legacy_home,
                            state,
                            state_snapshot,
                            dry_run=False,
                        )
                    )
                    self.assertTrue(did_recover)
                    self.assertEqual(recovered, state)
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(legacy_home))
                    )
                else:
                    with self.assertRaises((MODULE.SyncError, OSError)):
                        MODULE._recover_pending_link_transaction(
                            legacy_home,
                            state,
                            state_snapshot,
                            dry_run=False,
                        )
                    self.assertTrue(
                        MODULE._pending_link_pointer_path(legacy_home).is_file()
                    )
                self.assertTrue(batch.batch_root.is_dir())

    def test_rmdir_to_ticket_delete_crash_without_proof_only_recovers_legacy_tickets(
        self,
    ) -> None:
        for version in (1, 2, 3, 4):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"empty-proof-home-v{version}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    ticket = self._publish_legacy_cleanup_ticket(version=version)
                    proof_path = MODULE._pending_cleanup_empty_proof_path(
                        self.home,
                        ticket.batch_root.name,
                    )

                    def crash_after_rmdir(
                        _home: Path,
                        _ticket: MODULE.PendingBatchCleanupTicket,
                    ) -> None:
                        self.assertTrue(proof_path.is_file())
                        proof_path.unlink()
                        raise SystemExit("injected rmdir-to-ticket-delete crash")

                    with (
                        mock.patch.object(
                            MODULE,
                            "_delete_pending_cleanup_ticket",
                            side_effect=crash_after_rmdir,
                        ),
                        self.assertRaisesRegex(SystemExit, "ticket-delete crash"),
                    ):
                        MODULE._remove_cleanup_ready_batch(self.home, ticket)

                    self.assertFalse(ticket.batch_root.exists())
                    self.assertFalse(proof_path.exists())
                    self.assertTrue(ticket.path.is_file())
                    if version < 3:
                        self.assertEqual(
                            MODULE._cleanup_ready_pending_batches(self.home),
                            1,
                        )
                        self.assertFalse(ticket.path.exists())
                    elif version == 3:
                        with contextlib.redirect_stdout(io.StringIO()) as stdout:
                            self.assertEqual(
                                MODULE._cleanup_ready_pending_batches(self.home),
                                0,
                            )
                        self.assertIn("missing without an exact empty proof", stdout.getvalue())
                        self.assertTrue(ticket.path.is_file())
                    else:
                        with self.assertRaises(MODULE.SyncError):
                            MODULE._cleanup_ready_pending_batches(self.home)
                        self.assertTrue(ticket.path.is_file())
                finally:
                    self.home = original_home

    def test_partial_canonical_cleanup_authority_is_never_overwritten(self) -> None:
        for version, label in ((2, "rollback"), (3, "staging")):
            with self.subTest(label=label):
                case_home = self.root / f"partial-ticket-home-{label}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    batch_root = MODULE._quarantine_batch_root(self.home, [])
                    payload = self._legacy_cleanup_ticket_payload(
                        batch_root,
                        version=version,
                    )
                    ticket_path = MODULE._pending_cleanup_ticket_path(
                        self.home,
                        batch_root.name,
                    )
                    ticket_path.parent.mkdir(parents=True, exist_ok=True)
                    partial = payload[: max(1, len(payload) // 2)]
                    ticket_path.write_bytes(partial)
                    ticket_path.chmod(0o600)

                    with self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared with changed content",
                    ):
                        MODULE._publish_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                            payload,
                        )
                    self.assertEqual(ticket_path.read_bytes(), partial)
                finally:
                    self.home = original_home

        ticket = self._publish_legacy_cleanup_ticket(version=3)
        quarantine_root = MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        proof_path = MODULE._pending_cleanup_empty_proof_path(
            self.home,
            ticket.batch_root.name,
        )
        proof_path.write_bytes(b"{\n")
        proof_path.chmod(0o600)
        with self.assertRaisesRegex(MODULE.SyncError, "empty proof changed"):
            MODULE._publish_pending_cleanup_empty_proof(
                self.home,
                ticket,
                (quarantine_root.stat().st_dev, quarantine_root.stat().st_ino),
            )
        self.assertEqual(proof_path.read_bytes(), b"{\n")

    def test_cleanup_authority_readers_reject_foreign_owner_uid(self) -> None:
        foreign_uid = os.geteuid() + 1
        ticket = self._publish_legacy_cleanup_ticket(version=3)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_root_identity = (
            quarantine_root.stat().st_dev,
            quarantine_root.stat().st_ino,
        )
        proof = MODULE._publish_pending_cleanup_empty_proof(
            self.home,
            ticket,
            quarantine_root_identity,
        )

        staging_root = MODULE._quarantine_batch_root(self.home, [])
        staging_identity = (
            staging_root.stat().st_dev,
            staging_root.stat().st_ino,
        )
        staging_path = staging_root / Path(
            *MODULE.PENDING_STATE_STAGING_MARKER.parts
        )
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        marker = MODULE._write_exclusive_internal_file(
            self.home,
            staging_path,
            MODULE._pending_staging_marker_payload(
                staging_root,
                staging_identity,
            ),
        )
        self.assertEqual(ticket.snapshot.uid, os.geteuid())
        self.assertEqual(proof.uid, os.geteuid())
        self.assertEqual(marker.uid, os.geteuid())

        with mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket owner changed",
            ):
                MODULE._read_pending_cleanup_ticket(self.home, ticket.path)
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup empty proof changed",
            ):
                MODULE._read_pending_cleanup_empty_proof(
                    self.home,
                    ticket,
                    quarantine_root_identity,
                )
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "pending staging cleanup marker changed",
            ):
                MODULE._pending_staging_marker_snapshot(
                    self.home,
                    staging_root,
                    staging_identity,
                )

    def test_atomic_authority_publisher_rejects_foreign_owner_uid(self) -> None:
        authority_root = self.home / "atomic-authority-owner-tests"
        authority_root.mkdir()
        payload = b'{"authority": true}\n'
        foreign_uid = os.geteuid() + 1

        existing_path = authority_root / "existing.json"
        existing_path.write_bytes(payload)
        existing_path.chmod(0o600)
        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "atomic internal authority is incomplete or changed",
            ),
        ):
            MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                existing_path,
                payload,
            )

        raced_path = authority_root / "raced.json"

        def publish_foreign_owner_race(*_args) -> None:
            raced_path.write_bytes(payload)
            raced_path.chmod(0o600)
            raise FileExistsError("injected authority publication race")

        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=publish_foreign_owner_race,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "atomic internal authority appeared with changed content",
            ),
        ):
            MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                raced_path,
                payload,
            )

    def test_cleanup_ticket_file_exists_race_rejects_foreign_owner_uid(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            "20260901T000000Z-12-0",
        )
        payload = b'{"ticket": true}\n'
        foreign_uid = os.geteuid() + 1

        def publish_foreign_owner_race(*_args) -> None:
            ticket_path.write_bytes(payload)
            ticket_path.chmod(0o600)
            raise FileExistsError("injected cleanup ticket publication race")

        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=foreign_uid),
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=publish_foreign_owner_race,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending cleanup ticket appeared with changed content",
            ),
        ):
            MODULE._publish_pending_cleanup_ticket(
                self.home,
                ticket_path,
                payload,
            )

    def test_ticket_temp_publisher_recovers_truncated_rollback_and_staging_temps(
        self,
    ) -> None:
        for version, label in ((2, "rollback"), (3, "staging")):
            with self.subTest(label=label):
                case_home = self.root / f"temp-ticket-home-{label}"
                case_home.mkdir()
                original_home = self.home
                self.home = case_home
                try:
                    batch_root = MODULE._quarantine_batch_root(self.home, [])
                    payload = self._legacy_cleanup_ticket_payload(
                        batch_root,
                        version=version,
                    )
                    index_fd = MODULE._open_or_create_directory_beneath(
                        self.home,
                        MODULE._pending_cleanup_index_path(self.home),
                        mode=0o700,
                    )
                    MODULE._close_fd_quietly(index_fd)
                    ticket_path = MODULE._pending_cleanup_ticket_path(
                        self.home,
                        batch_root.name,
                    )
                    temp_path = ticket_path.with_name(
                        batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                    )
                    temp_path.write_bytes(payload[:1])
                    temp_path.chmod(0o600)

                    MODULE._publish_pending_cleanup_ticket(
                        self.home,
                        ticket_path,
                        payload,
                    )

                    self.assertEqual(ticket_path.read_bytes(), payload)
                    self.assertFalse(temp_path.exists())
                finally:
                    self.home = original_home

    def test_ticket_temp_rechecks_tolerate_mode_0600_gid_churn(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        payload = b'{"ticket": true}\n'
        real_write = MODULE._write_exclusive_internal_file

        def write_then_churn_gid(
            home: Path,
            path: Path,
            written_payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            staged = real_write(home, path, written_payload)
            assert staged.gid is not None
            os.chown(path, -1, self._alternate_gid(staged.gid))
            return staged

        for batch_name, preexisting in (
            ("20260901T000000Z-10-0", False),
            ("20260901T000000Z-10-1", True),
        ):
            with self.subTest(preexisting=preexisting):
                ticket_path = MODULE._pending_cleanup_ticket_path(
                    self.home,
                    batch_name,
                )
                if preexisting:
                    ticket_path.write_bytes(payload)
                    ticket_path.chmod(0o600)
                with mock.patch.object(
                    MODULE,
                    "_write_exclusive_internal_file",
                    side_effect=write_then_churn_gid,
                ):
                    published = MODULE._publish_pending_cleanup_ticket(
                        self.home,
                        ticket_path,
                        payload,
                    )

                self.assertEqual(published.payload, payload)
                self.assertEqual(ticket_path.read_bytes(), payload)
                self.assertFalse(
                    ticket_path.with_name(ticket_path.name + ".tmp").exists()
                )

    def test_ticket_rechecks_and_cleanup_tolerate_mode_0600_gid_churn(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        payload = self._legacy_cleanup_ticket_payload(batch_root, version=3)
        published_before_churn: MODULE.ManagedStateFileSnapshot | None = None
        real_publish = MODULE._publish_pending_cleanup_ticket

        def publish_then_churn_gid(
            home: Path,
            ticket_path: Path,
            ticket_payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal published_before_churn
            published_before_churn = real_publish(
                home,
                ticket_path,
                ticket_payload,
            )
            assert published_before_churn.gid is not None
            os.chown(
                ticket_path,
                -1,
                self._alternate_gid(published_before_churn.gid),
            )
            return published_before_churn

        with mock.patch.object(
            MODULE,
            "_publish_pending_cleanup_ticket",
            side_effect=publish_then_churn_gid,
        ):
            MODULE._publish_pending_batch_cleanup_ticket_for_root(
                self.home,
                batch_root,
                payload,
            )

        self.assertIsNotNone(published_before_churn)
        assert published_before_churn is not None
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        current_ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            ticket_path,
        )
        self.assertIsNotNone(current_ticket)
        assert current_ticket is not None
        self.assertNotEqual(
            current_ticket.snapshot.gid,
            published_before_churn.gid,
        )
        expected_ticket = MODULE.replace(
            current_ticket,
            snapshot=published_before_churn,
        )

        MODULE._verify_pending_cleanup_ticket_durable(
            self.home,
            expected_ticket,
        )
        self.assertTrue(
            MODULE._remove_cleanup_ready_batch(
                self.home,
                expected_ticket,
            )
        )

        self.assertFalse(ticket_path.exists())
        self.assertFalse(batch_root.exists())

    def test_cursor_writer_tolerates_mode_0600_temp_gid_churn(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        ticket_name = "20260901T000000Z-11-0.json"
        real_write = MODULE._write_exclusive_internal_file
        alternate_gid: int | None = None

        def write_then_churn_gid(
            home: Path,
            path: Path,
            payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal alternate_gid
            staged = real_write(home, path, payload)
            assert staged.gid is not None
            alternate_gid = self._alternate_gid(staged.gid)
            os.chown(path, -1, alternate_gid)
            return staged

        with mock.patch.object(
            MODULE,
            "_write_exclusive_internal_file",
            side_effect=write_then_churn_gid,
        ):
            self.assertEqual(
                MODULE._write_pending_cleanup_cursor(
                    self.home,
                    index_root,
                    ticket_name,
                ),
                0,
            )

        cursor_path = index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME
        self.assertEqual(
            MODULE._read_pending_cleanup_cursor(self.home, index_root),
            ticket_name,
        )
        self.assertEqual(cursor_path.stat().st_gid, alternate_gid)

    def test_retained_ticket_temp_cleanup_is_bounded_per_run(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        retained_paths: list[Path] = []
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN + 2):
            batch_name = f"20260901T000000Z-1-{index}"
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{batch_name}"
                f"{MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"temporary\n")
            retained.chmod(0o600)
            retained_paths.append(retained)

        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home),
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(sum(path.exists() for path in retained_paths), 2)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 2)
        self.assertFalse(any(path.exists() for path in retained_paths))
        self.assertFalse(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

    def test_cursor_temp_residue_uses_shared_budget_before_ticket_selection(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        tickets = {}
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20260901T000000Z-2-{index}"
            ticket_path = index_root / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            )
            ticket_path.write_bytes(b"{}\n")
            ticket_path.chmod(0o600)
            tickets[ticket_path.name] = mock.Mock(version=3)
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000001"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)

        def read_ticket(_home: Path, path: Path, **_kwargs):
            return tickets.get(path.name)

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                side_effect=read_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                return_value=True,
            ) as remove_batch,
        ):
            self.assertEqual(
                MODULE._cleanup_ready_pending_batches(self.home),
                MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
            )

        self.assertEqual(
            remove_batch.call_count,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN - 1,
        )
        self.assertFalse(retained_cursor.exists())
        self.assertTrue((index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME).is_file())

    def test_cursor_progress_reaches_ninth_v4_after_deferred_prefix(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        terminal_ticket = self._only_cleanup_ticket()
        self.assertEqual(terminal_ticket.version, 4)
        index_root = MODULE._pending_cleanup_index_path(self.home)
        deferred_tickets = {}
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20000101T000000Z-1-{index}"
            ticket_path = index_root / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            )
            ticket_path.write_bytes(b"{}\n")
            ticket_path.chmod(0o600)
            deferred_tickets[ticket_path.name] = mock.Mock(version=3)
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000003"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)
        real_read = MODULE._read_pending_cleanup_ticket
        real_remove = MODULE._remove_cleanup_ready_batch
        real_verify = MODULE._verify_final_regular_targets

        def read_ticket(home: Path, path: Path, **kwargs):
            deferred = deferred_tickets.get(path.name)
            if deferred is not None:
                return deferred
            return real_read(home, path, **kwargs)

        def defer_prefix(home: Path, ticket):
            if ticket in deferred_tickets.values():
                raise MODULE.SyncError("injected persistent deferred cleanup")
            return real_remove(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                side_effect=read_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=defer_prefix,
            ),
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                wraps=real_verify,
            ) as verify,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        verify.assert_called_once()
        self.assertFalse(retained_cursor.exists())
        self.assertFalse(terminal_ticket.path.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_cursor_temp_forms_are_observed(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        cursor_temp = index_root / MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME
        cursor_temp.write_bytes(b"cursor\n")
        cursor_temp.chmod(0o600)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

        cursor_temp.unlink()
        retained_cursor = index_root / (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
            f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
            "0000000000000002"
        )
        retained_cursor.write_bytes(b"cursor\n")
        retained_cursor.chmod(0o600)
        self.assertTrue(MODULE._pending_cleanup_ready_batch_is_observed(self.home))

    def test_cursor_writer_skips_publication_while_residue_remains(self) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        retained_paths: list[Path] = []
        for index in range(2):
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}"
                f"{MODULE.PENDING_CLEANUP_CURSOR_TEMP_NAME}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"cursor\n")
            retained.chmod(0o600)
            retained_paths.append(retained)

        self.assertEqual(
            MODULE._write_pending_cleanup_cursor(
                self.home,
                index_root,
                "20260901T000000Z-5-0.json",
                max_temp_cleanup_actions=1,
            ),
            1,
        )

        self.assertEqual(sum(path.exists() for path in retained_paths), 1)
        self.assertFalse((index_root / MODULE.PENDING_CLEANUP_CURSOR_NAME).exists())

    def test_restored_v4_ticket_is_finalized_after_budget_is_charged(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.next_release, self.home, SHA_B)

        ticket = self._only_cleanup_ticket()
        self.assertEqual(ticket.version, 4)
        retained_ticket = ticket.path.with_name(
            next(MODULE._retained_pending_cleanup_names(ticket.path))
        )
        ticket.path.rename(retained_ticket)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN - 1):
            batch_name = f"20260901T000000Z-3-{index}"
            temp_path = ticket.path.parent / (
                batch_name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
            )
            temp_path.write_bytes(b"temporary\n")
            temp_path.chmod(0o600)

        real_verify = MODULE._verify_final_regular_targets
        with mock.patch.object(
            MODULE,
            "_verify_final_regular_targets",
            wraps=real_verify,
        ) as verify:
            self.assertEqual(
                MODULE._cleanup_ready_pending_batches(self.home),
                MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
            )

        verify.assert_called_once()
        self.assertFalse(ticket.path.exists())
        self.assertFalse(retained_ticket.exists())
        self.assertEqual(self.target.stat().st_nlink, 1)

    def test_retained_control_ambiguity_is_checked_beyond_selected_slice(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        retained_paths: list[Path] = []
        for batch_index, retained_count in ((0, 1), (1, 2)):
            canonical = (
                f"20260901T000000Z-4-{batch_index}"
                f"{MODULE.PENDING_CLEANUP_TICKET_SUFFIX}"
            )
            for retained_index in range(retained_count):
                retained = index_root / (
                    f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{canonical}-123-"
                    f"{retained_index:016x}"
                )
                retained.write_bytes(b"retained\n")
                retained.chmod(0o600)
                retained_paths.append(retained)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "multiple retained files",
        ):
            MODULE._restore_pending_cleanup_control_tombstones(self.home, limit=1)

        self.assertTrue(all(path.is_file() for path in retained_paths))

    def test_top_level_install_reuses_one_cleanup_budget_across_passes(
        self,
    ) -> None:
        release_expectation = MODULE._source_release_identity(
            self.next_release,
            None,
        )
        release = mock.Mock(
            release_root=self.next_release,
            release_expectation=release_expectation,
        )
        release.assets.sha = SHA_B
        workspace = mock.Mock()
        workspace.path = self.root / "download-workspace"
        seen_budgets: list[MODULE.PendingCleanupActionBudget] = []
        phases: list[str] = []

        def preflight(
            _home: Path,
            *,
            dry_run: bool,
            cleanup_budget: MODULE.PendingCleanupActionBudget,
        ) -> bool:
            self.assertFalse(dry_run)
            phases.append("preflight")
            seen_budgets.append(cleanup_budget)
            cleanup_budget.consume_control_actions(2)
            return False

        def install_set(
            _home: Path,
            _releases,
            *,
            dry_run: bool,
            preflight_only: bool = False,
            cleanup_budget: MODULE.PendingCleanupActionBudget,
            **_kwargs,
        ) -> None:
            phases.append(
                "dry preflight" if dry_run and preflight_only else "locked install"
            )
            seen_budgets.append(cleanup_budget)
            cleanup_budget.consume_control_actions(2)

        with (
            mock.patch.object(
                MODULE,
                "temporary_archive_workspace",
                return_value=contextlib.nullcontext(workspace),
            ),
            mock.patch.object(
                MODULE,
                "download_and_extract_release",
                return_value=release,
            ),
            mock.patch.object(
                MODULE,
                "_preflight_pending_recovery",
                side_effect=preflight,
            ),
            mock.patch.object(
                MODULE,
                "_install_release_set_unlocked",
                side_effect=install_set,
            ),
            mock.patch.object(
                MODULE,
                "installation_lock",
                return_value=contextlib.nullcontext(),
            ),
        ):
            MODULE.install_from_github("Joey-Tools/example", self.home, dry_run=False)

        self.assertEqual(
            phases,
            ["preflight", "preflight", "dry preflight", "locked install"],
        )
        self.assertTrue(all(budget is seen_budgets[0] for budget in seen_budgets))
        self.assertEqual(
            seen_budgets[0].limit,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(
            seen_budgets[0].consumed,
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(seen_budgets[0].remaining, 0)

    def test_shared_cleanup_helper_reports_zero_delta_after_budget_is_spent(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            index_root,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN):
            batch_name = f"20260901T000000Z-6-{index}"
            retained = index_root / (
                f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{batch_name}"
                f"{MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX}-123-"
                f"{index:016x}"
            )
            retained.write_bytes(b"temporary\n")
            retained.chmod(0o600)

        budget = MODULE.PendingCleanupActionBudget(
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN
        )
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home, budget=budget),
            MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN,
        )
        self.assertEqual(budget.remaining, 0)
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(self.home, budget=budget),
            0,
        )

    def test_orphan_empty_proof_scan_skips_paired_prefix_before_budget(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        for index in range(MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN + 1):
            batch_name = f"20000101T000000Z-1-{index}"
            (index_root / f"{batch_name}.empty-proof").write_bytes(b"paired proof\n")
            (index_root / f"{batch_name}.json").write_bytes(b"paired ticket\n")
        orphan_batch = "20990101T000000Z-1-0"
        orphan_path = index_root / f"{orphan_batch}.empty-proof"
        orphan_path.write_bytes(b"orphan proof\n")
        proof = mock.Mock()

        def isolate(
            _home: Path,
            path: Path,
            _parent_fd: int,
            _expected,
            *,
            label: str,
        ) -> None:
            self.assertEqual(path, orphan_path)
            self.assertIn(orphan_batch, label)
            path.unlink()

        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=None,
            ) as read_ticket,
            mock.patch.object(
                MODULE,
                "_read_orphan_pending_cleanup_empty_proof",
                return_value=proof,
            ) as read_proof,
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=isolate,
            ) as delete_proof,
        ):
            self.assertEqual(
                MODULE._cleanup_orphan_pending_cleanup_empty_proofs(
                    self.home,
                    limit=1,
                ),
                1,
            )

        read_ticket.assert_called_once_with(
            self.home,
            index_root / f"{orphan_batch}.json",
        )
        read_proof.assert_called_once_with(self.home, orphan_path)
        delete_proof.assert_called_once()
        self.assertFalse(orphan_path.exists())

    def test_orphan_empty_proof_budget_is_charged_before_content_reads(
        self,
    ) -> None:
        index_root = MODULE._pending_cleanup_index_path(self.home)
        index_root.mkdir(parents=True, exist_ok=True)
        batch_name = "20260901T000000Z-7-0"
        proof_path = index_root / f"{batch_name}.empty-proof"
        proof_path.write_bytes(b"not read\n")

        for limit in (0, 1):
            with self.subTest(limit=limit):
                budget = MODULE.PendingCleanupActionBudget(limit)
                ticket = None if limit == 0 else mock.Mock()
                with (
                    mock.patch.object(
                        MODULE,
                        "_read_pending_cleanup_ticket",
                        return_value=ticket,
                    ) as read_ticket,
                    mock.patch.object(
                        MODULE,
                        "_read_orphan_pending_cleanup_empty_proof",
                    ) as read_proof,
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                    ) as delete_proof,
                ):
                    self.assertEqual(
                        MODULE._cleanup_orphan_pending_cleanup_empty_proofs(
                            self.home,
                            budget=budget,
                        ),
                        0,
                    )

                if limit == 0:
                    read_ticket.assert_not_called()
                    self.assertEqual(budget.consumed, 0)
                else:
                    read_ticket.assert_called_once()
                    self.assertEqual(budget.consumed, 1)
                read_proof.assert_not_called()
                delete_proof.assert_not_called()

    def test_staging_initial_skeleton_uses_fixed_structural_scan_bounds(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        for relative_path in (
            Path("pending/before"),
            Path("pending/stage"),
            Path("pending/evidence"),
            Path("pending/state"),
            Path("pending/cleanup"),
            Path("pending/claims/before"),
            Path("pending/claims/after"),
        ):
            directory_fd = MODULE._open_or_create_directory_beneath(
                self.home,
                batch_root / relative_path,
                mode=0o700,
            )
            MODULE._close_fd_quietly(directory_fd)
        marker_path = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        marker_temp = marker_path.with_name(
            marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        marker_temp.write_bytes(b"staging marker temp\n")
        marker_temp.chmod(0o600)
        maximum_entries: list[int | None] = []
        real_member_names = MODULE._directory_member_names

        def bounded_member_names(directory_fd: int, **kwargs):
            maximum_entries.append(kwargs.get("maximum_entries"))
            return real_member_names(directory_fd, **kwargs)

        with mock.patch.object(
            MODULE,
            "_directory_member_names",
            side_effect=bounded_member_names,
        ):
            MODULE._require_pending_staging_initial_skeleton(
                self.home,
                batch_root,
            )

        self.assertEqual(
            maximum_entries,
            [3, 7, 3, 1, 1, 1, 1, 1, 1, 2],
        )

    def test_cursor_temp_failures_do_not_hold_index_fd(self) -> None:
        for label, cleanup_result, budget_limit in (
            ("cleanup", MODULE.SyncError("injected cursor cleanup failure"), 1),
            ("consume", 2, 1),
        ):
            with self.subTest(failure=label):
                with (
                    mock.patch.object(
                        MODULE,
                        "_pending_link_pointer_is_absent",
                        return_value=True,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_restore_pending_cleanup_control_tombstones",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_pending_cleanup_ticket_temps",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_publish_discovered_staging_cleanup_tickets",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_orphan_pending_cleanup_empty_proofs",
                    ),
                    mock.patch.object(
                        MODULE,
                        "_cleanup_pending_cleanup_cursor_temp",
                        side_effect=(
                            cleanup_result
                            if isinstance(cleanup_result, BaseException)
                            else None
                        ),
                        return_value=(
                            cleanup_result
                            if isinstance(cleanup_result, int)
                            else mock.DEFAULT
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_open_directory_beneath",
                        return_value=123,
                    ) as open_index,
                    mock.patch.object(MODULE, "_close_fd_quietly") as close_index,
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._cleanup_ready_pending_batches(
                        self.home,
                        budget=MODULE.PendingCleanupActionBudget(budget_limit),
                    )
                open_index.assert_called_once_with(
                    self.home,
                    MODULE._pending_cleanup_index_path(self.home),
                )
                close_index.assert_called_once_with(123)

    def test_cleanup_ready_batches_returns_zero_when_index_is_absent(self) -> None:
        fresh_home = self.root / "fresh-home"
        fresh_home.mkdir()
        self.assertEqual(
            MODULE._cleanup_ready_pending_batches(fresh_home),
            0,
        )

    def test_exhausted_budget_blocks_v3_v4_authority_before_new_mutation(
        self,
    ) -> None:
        release_expectation = MODULE._source_release_identity(
            self.next_release,
            None,
        )
        manifest = release_expectation[0][1]
        releases = [(self.next_release, SHA_B, manifest, release_expectation)]

        for version in (3, 4):
            with self.subTest(ticket_version=version):
                case_home = self.root / f"terminal-authority-home-v{version}"
                write_release(case_home / "first-release", role_payload='name = "first"\n')
                install(case_home / "first-release", case_home, SHA_A)
                case_target = case_home / ROLE_TARGET
                case_state_path = MODULE._state_path(case_home)
                target_before = case_target.read_bytes()
                state_before = case_state_path.read_bytes()
                original_home = self.home
                self.home = case_home
                try:
                    if version == 3:
                        batch_root = MODULE._quarantine_batch_root(self.home, [])
                        batch_identity = (
                            batch_root.stat().st_dev,
                            batch_root.stat().st_ino,
                        )
                        marker_path = batch_root / Path(
                            *MODULE.PENDING_STATE_STAGING_MARKER.parts
                        )
                        marker_path.parent.mkdir(parents=True, exist_ok=True)
                        marker = MODULE._write_exclusive_internal_file(
                            self.home,
                            marker_path,
                            MODULE._pending_staging_marker_payload(
                                batch_root,
                                batch_identity,
                            ),
                        )
                        ticket_path = MODULE._pending_cleanup_ticket_path(
                            self.home,
                            batch_root.name,
                        )
                        index_fd = MODULE._open_or_create_directory_beneath(
                            self.home,
                            ticket_path.parent,
                            mode=0o700,
                        )
                        MODULE._close_fd_quietly(index_fd)
                        MODULE._publish_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                            MODULE._pending_staging_cleanup_ticket_payload(
                                batch_root,
                                batch_identity,
                                marker,
                            ),
                        )
                        ticket = MODULE._read_pending_cleanup_ticket(
                            self.home,
                            ticket_path,
                        )
                        self.assertIsNotNone(ticket)
                        assert ticket is not None
                    else:
                        ticket = self._publish_legacy_cleanup_ticket(version=version)
                    budget = MODULE.PendingCleanupActionBudget(
                        MODULE.MAX_PENDING_CLEANUP_BATCHES_PER_RUN
                    )
                    budget.consume_control_actions(budget.limit)
                    with (
                        mock.patch.object(
                            MODULE,
                            "_cleanup_ready_pending_batches",
                            return_value=0,
                        ) as cleanup,
                        mock.patch.object(
                            MODULE,
                            "_stage_release_tree_for_install",
                        ) as stage,
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            rf"ticket v{version}",
                        ),
                    ):
                        MODULE._install_release_set_unlocked(
                            case_home,
                            releases,
                            dry_run=False,
                            allow_cross_owner=False,
                            cleanup_budget=budget,
                        )

                    cleanup.assert_called_once_with(case_home, budget=budget)
                    stage.assert_not_called()
                    self.assertTrue(ticket.path.is_file())
                    self.assertEqual(case_target.read_bytes(), target_before)
                    self.assertEqual(case_state_path.read_bytes(), state_before)
                finally:
                    self.home = original_home

    def test_truncated_staging_marker_publish_temp_recovers_and_cleans_batch(
        self,
    ) -> None:
        batch_root = MODULE._quarantine_batch_root(self.home, [])
        for relative_path in (
            Path("pending/before"),
            Path("pending/stage"),
            Path("pending/evidence"),
            Path("pending/state"),
            Path("pending/cleanup"),
            Path("pending/claims/before"),
            Path("pending/claims/after"),
        ):
            directory_fd = MODULE._open_or_create_directory_beneath(
                self.home,
                batch_root / relative_path,
                mode=0o700,
            )
            MODULE._close_fd_quietly(directory_fd)
        marker_path = batch_root / Path(*MODULE.PENDING_STATE_STAGING_MARKER.parts)
        temp_path = marker_path.with_name(
            marker_path.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        temp_path.write_bytes(b"{\n")
        temp_path.chmod(0o600)
        batch_identity = (batch_root.stat().st_dev, batch_root.stat().st_ino)
        expected_marker = MODULE._pending_staging_marker_payload(
            batch_root,
            batch_identity,
        )
        ticket_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_root.name,
        )
        real_publish = MODULE._publish_pending_batch_cleanup_ticket_for_root

        def publish_ticket(home: Path, root: Path, payload: bytes) -> None:
            self.assertEqual(root, batch_root)
            self.assertEqual(marker_path.read_bytes(), expected_marker)
            real_publish(home, root, payload)
            self.assertTrue(ticket_path.is_file())

        with mock.patch.object(
            MODULE,
            "_publish_pending_batch_cleanup_ticket_for_root",
            side_effect=publish_ticket,
        ):
            self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 1)

        self.assertFalse(temp_path.exists())
        self.assertFalse(marker_path.exists())
        self.assertFalse(ticket_path.exists())
        self.assertFalse(batch_root.exists())

    def test_v1_v2_cleanup_backlog_does_not_block_terminal_authority_gate(
        self,
    ) -> None:
        for version in (1, 2):
            with self.subTest(ticket_version=version):
                ticket = self._publish_legacy_cleanup_ticket(version=version)
                MODULE._require_no_pending_terminal_mutation_authority(self.home)
                self.assertTrue(ticket.path.is_file())


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
