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


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
