from __future__ import annotations

import contextlib
import io
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


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
