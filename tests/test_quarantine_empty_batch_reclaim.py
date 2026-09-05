from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_quarantine_empty_batch_reclaim",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class QuarantineEmptyBatchReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name) / "home"
        self.home.mkdir(mode=0o700)
        self.source_parent = self.home / "agents"
        self.source_parent.mkdir(mode=0o700)
        self.source = self.source_parent / "reviewer.toml"
        self.source.write_bytes(b'role = "reviewer"\n')
        self.source.chmod(0o600)
        metadata = self.source.stat()
        self.source_identity = (metadata.st_dev, metadata.st_ino)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _move_after_real_parent_policy_drift(self) -> None:
        source_parent_fd = MODULE._open_directory_beneath(
            self.home,
            self.source_parent,
        )
        real_quarantine_batch_root = MODULE._quarantine_batch_root

        def make_source_parent_group_writable(*args: object, **kwargs: object):
            allocation = real_quarantine_batch_root(*args, **kwargs)
            self.source_parent.chmod(0o770)
            return allocation

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_quarantine_batch_root",
                    side_effect=make_source_parent_group_writable,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "managed regular-file parent access policy mismatch",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    source_parent_fd,
                    self.source.name,
                    label="empty-batch-reclaim-test",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(source_parent_fd)

    def _assert_source_was_not_moved(self) -> None:
        self.assertEqual(self.source.read_bytes(), b'role = "reviewer"\n')
        metadata = self.source.stat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), self.source_identity)

    def test_parent_policy_precheck_failure_reclaims_empty_batch(self) -> None:
        for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
            self.source_parent.chmod(0o700)
            self._move_after_real_parent_policy_drift()
            self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

        self._assert_source_was_not_moved()

    def test_foreign_leaf_sibling_prevents_empty_batch_reclaim(self) -> None:
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        mutated_batch: Path | None = None

        def add_foreign_sibling(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal mutated_batch
            mutated_batch = binding.batch_root
            foreign = binding.batch_root / "leaf" / "foreign-evidence"
            foreign.write_bytes(b"foreign\n")
            real_discard(home, binding)

        with mock.patch.object(
            MODULE,
            "_discard_empty_ephemeral_quarantine_batch",
            side_effect=add_foreign_sibling,
        ):
            self._move_after_real_parent_policy_drift()

        self.assertIsNotNone(mutated_batch)
        assert mutated_batch is not None
        self.assertEqual(
            (mutated_batch / "leaf" / "foreign-evidence").read_bytes(),
            b"foreign\n",
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self._assert_source_was_not_moved()

    def test_leaf_replacement_prevents_empty_batch_reclaim(self) -> None:
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        mutated_batch: Path | None = None
        replacement_identity: tuple[int, int] | None = None

        def replace_leaf_directory(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal mutated_batch, replacement_identity
            mutated_batch = binding.batch_root
            leaf = binding.batch_root / "leaf"
            leaf.rmdir()
            leaf.mkdir(mode=0o700)
            metadata = leaf.stat()
            replacement_identity = (metadata.st_dev, metadata.st_ino)
            self.assertNotEqual(replacement_identity, binding.leaf_identity)
            real_discard(home, binding)

        with mock.patch.object(
            MODULE,
            "_discard_empty_ephemeral_quarantine_batch",
            side_effect=replace_leaf_directory,
        ):
            self._move_after_real_parent_policy_drift()

        self.assertIsNotNone(mutated_batch)
        self.assertIsNotNone(replacement_identity)
        assert mutated_batch is not None
        leaf_metadata = (mutated_batch / "leaf").stat()
        self.assertEqual(
            (leaf_metadata.st_dev, leaf_metadata.st_ino),
            replacement_identity,
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self._assert_source_was_not_moved()

    def test_receiptless_cleanup_does_not_allocate_a_quarantine_batch(self) -> None:
        for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1):
            MODULE._quarantine_batch_root(self.home, [])
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            self.source,
            require_managed_access=False,
        )

        with mock.patch.object(
            MODULE,
            "_quarantine_batch_root",
            side_effect=AssertionError("receiptless cleanup allocated a batch"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                self.home,
                self.source,
                expected,
            )

        self.assertFalse(self.source.exists())
        self.assertEqual(
            MODULE._quarantine_batch_count(self.home),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES - 1,
        )
        self.assertFalse(
            list(MODULE._pending_cleanup_index_path(self.home).glob("*.json"))
        )

    def test_ticket_publication_failure_leaves_no_batch_scaffold(self) -> None:
        expected = MODULE._read_regular_file_snapshot_beneath(
            self.home,
            self.source,
            require_managed_access=False,
        )
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
                side_effect=SystemExit("injected pre-publication crash"),
            ),
            self.assertRaisesRegex(SystemExit, "pre-publication crash"),
        ):
            MODULE._delete_exact_regular_publication_without_pending_receipt(
                self.home,
                self.source,
                expected,
            )

        self._assert_source_was_not_moved()
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)


if __name__ == "__main__":
    unittest.main()
