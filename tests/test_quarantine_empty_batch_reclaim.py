from __future__ import annotations

import gc
import importlib.util
import os
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

    def _assert_created_leaf_cleanup_failure(
        self,
        name: str,
        *,
        original_failure: str,
        cleanup_failure: str,
    ) -> None:
        if callable(getattr(MODULE.SyncError("probe"), "add_note", None)):
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "private isolation failed",
            ) as raised:
                self._evacuate_created_leaf(name)
            original_error = raised.exception.__cause__
            self.assertIsInstance(original_error, MODULE.SyncError)
            assert original_error is not None
            self.assertIn(original_failure, str(original_error))
            notes = "\n".join(getattr(original_error, "__notes__", ()))
            self.assertIn("could not be safely reclaimed", notes)
            self.assertIn(cleanup_failure, notes)
        else:
            with self.assertRaisesRegex(
                MODULE.SyncError,
                "could not be safely reclaimed",
            ) as raised:
                self._evacuate_created_leaf(name)
            self.assertIn(original_failure, str(raised.exception))
            self.assertIn(cleanup_failure, str(raised.exception))
            cleanup_error = raised.exception.__cause__
            self.assertIsInstance(cleanup_error, MODULE.SyncError)
            assert cleanup_error is not None
            self.assertIn(cleanup_failure, str(cleanup_error))

    def _evacuate_created_leaf(self, name: str) -> None:
        target = self.source_parent / name
        payload = b'role = "reviewer"\n'
        target.write_bytes(payload)
        target.chmod(0o600)
        target_stat = target.stat()
        target_identity = (target_stat.st_dev, target_stat.st_ino)
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            MODULE._evacuate_created_regular_leaf_after_failure(
                self.home,
                target,
                parent_fd,
                MODULE._directory_identity(parent_fd),
                target_identity,
                payload,
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def _allocate_unowned_scaffold(
        self,
    ) -> tuple[
        Path,
        MODULE.EphemeralQuarantineBatchBinding,
    ]:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        self.assertIsInstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        assert isinstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        batch_root = allocation.batch_root
        binding = allocation.binding
        allocation.revoke_reclaim()
        allocation.close()
        return batch_root, binding

    def _resume_scaffold_cleanup(self, batch_root: Path) -> None:
        MODULE._cleanup_pending_cleanup_ticket_temps(self.home, limit=8)
        ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            MODULE._pending_cleanup_ticket_path(self.home, batch_root.name),
        )
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.version, 7)
        self.assertTrue(MODULE._remove_cleanup_ready_batch(self.home, ticket))
        self.assertFalse(batch_root.exists())
        index = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(list(index.glob(f"{batch_root.name}*")))

    def _write_v7_ticket_temp(self) -> Path:
        batch_name = f"20260905T010101Z-{os.getpid()}-{os.getpid()}"
        temp_path = MODULE._pending_cleanup_ticket_path(
            self.home,
            batch_name,
        ).with_name(batch_name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            temp_path.parent,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        payload = MODULE._bounded_json_document(
            {
                "version": 7,
                "kind": "ephemeral-quarantine-scaffold",
                "batch": batch_name,
                "batch_root_identity": [101, 201],
                "quarantine_root_identity": [102, 202],
                "isolated_name": MODULE._pending_cleanup_isolated_batch_name(
                    batch_name
                ),
                "leaf": None,
                "metadata": {
                    "path": "metadata.json",
                    "file_identity": [103, 203],
                    "mode": 0o600,
                    "size": 0,
                    "sha256": MODULE.hashlib.sha256(b"").hexdigest(),
                },
            },
            max_bytes=MODULE.MAX_PENDING_CLEANUP_TICKET_BYTES,
            overflow_error="test payload too large",
        )
        MODULE._write_exclusive_internal_file(self.home, temp_path, payload)
        return temp_path

    def _exercise_metadata_cleanup_boundary_mutation(
        self,
        *,
        boundary_call: int,
        mutation: str,
    ) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        real_boundary = MODULE._require_pending_ephemeral_metadata_cleanup_boundary
        boundary_calls = 0
        evidence_batch_root = batch_root

        def mutate_boundary() -> None:
            nonlocal evidence_batch_root
            if mutation == "leaf":
                (evidence_batch_root / "leaf").mkdir(mode=0o700)
                return
            if mutation == "ticket":
                ticket.path.unlink()
                ticket.path.write_bytes(b"{}\n")
                ticket.path.chmod(0o600)
                return
            if mutation == "batch-policy":
                evidence_batch_root.chmod(0o755)
                return
            if mutation == "root-identity":
                quarantine_root = evidence_batch_root.parent
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                evidence_batch_root = moved_root / batch_root.name
                return
            raise AssertionError(f"unsupported mutation: {mutation}")

        def inject_at_internal_boundary(*args: object, **kwargs: object) -> None:
            nonlocal boundary_calls
            boundary_calls += 1
            if boundary_calls == boundary_call:
                mutate_boundary()
            real_boundary(*args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_ephemeral_metadata_cleanup_boundary",
                side_effect=inject_at_internal_boundary,
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertEqual(boundary_calls, boundary_call)
        if boundary_call == 1:
            self.assertTrue((evidence_batch_root / "metadata.json").is_file())
        else:
            self.assertFalse((evidence_batch_root / "metadata.json").exists())
            retained = list(
                evidence_batch_root.glob(
                    MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-*"
                )
            )
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].is_file())

    def test_parent_policy_precheck_failure_reclaims_empty_batch(self) -> None:
        for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
            self.source_parent.chmod(0o700)
            self._move_after_real_parent_policy_drift()
            self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

        self._assert_source_was_not_moved()

    def test_regular_move_rejects_root_replacement_before_private_rename(
        self,
    ) -> None:
        real_create_directory = MODULE._create_ephemeral_quarantine_leaf_at
        relocated_batch: Path | None = None

        def relocate_bound_batch_after_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
        ) -> None:
            nonlocal relocated_batch
            real_create_directory(allocation)
            if relocated_batch is None:
                quarantine_root = (
                    MODULE._personal_sync_root(self.home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                batch_root = next(quarantine_root.iterdir())
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                relocated_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(relocated_batch)

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_create_ephemeral_quarantine_leaf_at",
                    side_effect=relocate_bound_batch_after_leaf_creation,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "namespace changed before private rename",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="root-replacement",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()
        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(list((relocated_batch / "leaf").iterdir()), [])

    def test_created_move_rejects_root_replacement_before_private_rename(
        self,
    ) -> None:
        real_create_directory = MODULE._create_ephemeral_quarantine_leaf_at
        relocated_batch: Path | None = None

        def relocate_bound_batch_after_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
        ) -> None:
            nonlocal relocated_batch
            real_create_directory(allocation)
            if relocated_batch is None:
                quarantine_root = (
                    MODULE._personal_sync_root(self.home)
                    / MODULE.QUARANTINE_RELATIVE_PATH
                )
                batch_root = next(quarantine_root.iterdir())
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                relocated_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(relocated_batch)

        with mock.patch.object(
            MODULE,
            "_create_ephemeral_quarantine_leaf_at",
            side_effect=relocate_bound_batch_after_leaf_creation,
        ):
            self._assert_created_leaf_cleanup_failure(
                "created-root-replacement.toml",
                original_failure=(
                    "ephemeral quarantine namespace changed before private rename"
                ),
                cleanup_failure="ephemeral quarantine root changed",
            )

        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(list((relocated_batch / "leaf").iterdir()), [])
        aliases = tuple(self.source_parent.glob(".codex-created-leaf-*.evidence"))
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0].read_bytes(), b'role = "reviewer"\n')

    def test_private_boundary_rechecks_metadata_after_namespace_inventory(
        self,
    ) -> None:
        real_members = MODULE._directory_member_names
        leaf_inventory_calls = 0

        def mutate_after_final_namespace_inventory(
            directory_fd: int,
            *args: object,
            **kwargs: object,
        ) -> tuple[str, ...]:
            nonlocal leaf_inventory_calls
            names = real_members(directory_fd, *args, **kwargs)
            if names == ("leaf", "metadata.json"):
                leaf_inventory_calls += 1
                if leaf_inventory_calls == 2:
                    quarantine_root = (
                        MODULE._personal_sync_root(self.home)
                        / MODULE.QUARANTINE_RELATIVE_PATH
                    )
                    batch_root = next(quarantine_root.iterdir())
                    metadata = batch_root / "metadata.json"
                    metadata.write_bytes(b'{"replaced": true}\n')
                    metadata.chmod(0o600)
            return names

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    side_effect=mutate_after_final_namespace_inventory,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "ephemeral quarantine metadata changed",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="metadata-final-boundary",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()

    def test_private_boundary_rechecks_leaf_policy_after_namespace_inventory(
        self,
    ) -> None:
        real_members = MODULE._directory_member_names
        leaf_inventory_calls = 0

        def relax_policy_after_final_namespace_inventory(
            directory_fd: int,
            *args: object,
            **kwargs: object,
        ) -> tuple[str, ...]:
            nonlocal leaf_inventory_calls
            names = real_members(directory_fd, *args, **kwargs)
            if names == ("leaf", "metadata.json"):
                leaf_inventory_calls += 1
                if leaf_inventory_calls == 2:
                    quarantine_root = (
                        MODULE._personal_sync_root(self.home)
                        / MODULE.QUARANTINE_RELATIVE_PATH
                    )
                    batch_root = next(quarantine_root.iterdir())
                    (batch_root / "leaf").chmod(0o755)
            return names

        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    side_effect=relax_policy_after_final_namespace_inventory,
                ),
                self.assertRaisesRegex(
                    MODULE.SyncError,
                    "pending cleanup access policy mismatch",
                ),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="policy-final-boundary",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self._assert_source_was_not_moved()

    def test_created_leaf_quarantine_leaf_creation_failure_does_not_leak_capacity(
        self,
    ) -> None:
        def fail_leaf_creation(
            allocation: MODULE.EphemeralQuarantineBatchAllocation,
            *args: object,
            **kwargs: object,
        ) -> None:
            del allocation, args, kwargs
            raise OSError("injected quarantine leaf creation failure")

        with mock.patch.object(
            MODULE,
            "_create_ephemeral_quarantine_leaf_at",
            side_effect=fail_leaf_creation,
        ):
            for attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "private quarantine setup failed",
                ):
                    self._evacuate_created_leaf(f"leaf-create-failure-{attempt}.toml")
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_created_leaf_pre_isolation_failure_does_not_leak_capacity(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        failed_batches: set[Path] = set()

        def fail_each_leaf_once(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            if (
                display_path.name == "leaf"
                and display_path.parent not in failed_batches
            ):
                failed_batches.add(display_path.parent)
                raise MODULE.SyncError("injected pre-isolation validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_require_pending_cleanup_fd_access_policy",
            side_effect=fail_each_leaf_once,
        ):
            for attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "private isolation failed",
                ):
                    self._evacuate_created_leaf(f"validation-failure-{attempt}.toml")
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

        self.assertEqual(
            len(failed_batches),
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1,
        )

    def test_created_leaf_replacement_with_content_is_retained(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        replaced_batch: Path | None = None
        replacement_identity: tuple[int, int] | None = None

        def replace_leaf_then_fail(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal replaced_batch, replacement_identity
            if display_path.name == "leaf" and replaced_batch is None:
                display_path.rmdir()
                display_path.mkdir(mode=0o700)
                foreign = display_path / "foreign-evidence"
                foreign.write_bytes(b"foreign\n")
                replacement_stat = display_path.stat()
                replacement_identity = (
                    replacement_stat.st_dev,
                    replacement_stat.st_ino,
                )
                replaced_batch = display_path.parent
                raise MODULE.SyncError("injected replaced-leaf validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_require_pending_cleanup_fd_access_policy",
            side_effect=replace_leaf_then_fail,
        ):
            self._assert_created_leaf_cleanup_failure(
                "replacement-race.toml",
                original_failure="injected replaced-leaf validation failure",
                cleanup_failure="ephemeral quarantine leaf changed",
            )

        self.assertIsNotNone(replaced_batch)
        self.assertIsNotNone(replacement_identity)
        assert replaced_batch is not None
        replacement = replaced_batch / "leaf"
        replacement_stat = replacement.stat()
        self.assertEqual(
            (replacement_stat.st_dev, replacement_stat.st_ino),
            replacement_identity,
        )
        self.assertEqual(
            (replacement / "foreign-evidence").read_bytes(),
            b"foreign\n",
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_metadata_replacement_after_allocation_is_retained(
        self,
    ) -> None:
        real_allocate = MODULE._quarantine_batch_root
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        allocated_batch: Path | None = None
        allocation_metadata_fd = -1
        original_metadata_identity: tuple[int, int] | None = None
        replacement_metadata_identity: tuple[int, int] | None = None
        leaf_validation_failures = 0
        discard_calls = 0

        def replace_metadata_after_allocation(
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal allocated_batch, allocation_metadata_fd
            nonlocal original_metadata_identity
            nonlocal replacement_metadata_identity
            allocation = real_allocate(*args, **kwargs)
            if not kwargs.get("retain_scaffold_binding"):
                return allocation
            assert isinstance(
                allocation,
                MODULE.EphemeralQuarantineBatchAllocation,
            )
            allocated_batch = allocation.batch_root
            allocation_metadata_fd = allocation.metadata_fd
            metadata_snapshot = allocation.binding.metadata
            original_metadata_identity = metadata_snapshot.file_identity
            metadata_path = allocated_batch / "metadata.json"
            original_stat = os.fstat(allocation_metadata_fd)
            self.assertEqual(
                (original_stat.st_dev, original_stat.st_ino),
                original_metadata_identity,
            )
            metadata_path.unlink()
            metadata_path.write_bytes(b'{"foreign": true}\n')
            metadata_path.chmod(0o600)
            replacement_stat = metadata_path.stat()
            replacement_metadata_identity = (
                replacement_stat.st_dev,
                replacement_stat.st_ino,
            )
            return allocation

        def fail_if_leaf_validation_is_reached(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal leaf_validation_failures
            if display_path.name == "leaf":
                leaf_validation_failures += 1
                raise MODULE.SyncError("injected pre-isolation validation failure")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_root",
                side_effect=replace_metadata_after_allocation,
            ),
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=fail_if_leaf_validation_is_reached,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "private quarantine setup failed",
            ),
        ):
            self._evacuate_created_leaf("metadata-replacement.toml")

        self.assertEqual(leaf_validation_failures, 0)
        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(allocated_batch)
        self.assertIsNotNone(original_metadata_identity)
        self.assertIsNotNone(replacement_metadata_identity)
        self.assertNotEqual(
            original_metadata_identity,
            replacement_metadata_identity,
        )
        with self.assertRaises(OSError):
            os.fstat(allocation_metadata_fd)
        assert allocated_batch is not None
        metadata_path = allocated_batch / "metadata.json"
        self.assertEqual(metadata_path.read_bytes(), b'{"foreign": true}\n')
        self.assertEqual(
            {member.name for member in allocated_batch.iterdir()},
            {"metadata.json"},
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_quarantine_root_replacement_is_retained(self) -> None:
        real_require_access = MODULE._require_pending_cleanup_fd_access_policy
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        original_root_identity: tuple[int, int] | None = None
        replacement_root_identity: tuple[int, int] | None = None
        bound_root_identity: tuple[int, int] | None = None
        replaced_batch: Path | None = None
        discard_calls = 0

        def replace_quarantine_root_then_fail(
            directory_fd: int,
            display_path: Path,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal original_root_identity, replacement_root_identity
            nonlocal replaced_batch
            if display_path.name == "leaf" and replaced_batch is None:
                batch_root = display_path.parent
                quarantine_root = batch_root.parent
                original_metadata = quarantine_root.stat()
                original_root_identity = (
                    original_metadata.st_dev,
                    original_metadata.st_ino,
                )
                moved_root = quarantine_root.with_name(
                    quarantine_root.name + ".original"
                )
                quarantine_root.rename(moved_root)
                quarantine_root.mkdir(mode=0o700)
                replacement_metadata = quarantine_root.stat()
                replacement_root_identity = (
                    replacement_metadata.st_dev,
                    replacement_metadata.st_ino,
                )
                replaced_batch = quarantine_root / batch_root.name
                (moved_root / batch_root.name).rename(replaced_batch)
                raise MODULE.SyncError("injected quarantine root replacement")
            return real_require_access(directory_fd, display_path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal bound_root_identity, discard_calls
            discard_calls += 1
            bound_root_identity = binding.quarantine_root_identity
            real_discard(home, binding)

        with (
            mock.patch.object(
                MODULE,
                "_require_pending_cleanup_fd_access_policy",
                side_effect=replace_quarantine_root_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
        ):
            self._assert_created_leaf_cleanup_failure(
                "root-replacement.toml",
                original_failure="injected quarantine root replacement",
                cleanup_failure="ephemeral quarantine root changed",
            )

        self.assertEqual(discard_calls, 1)
        self.assertIsNotNone(original_root_identity)
        self.assertIsNotNone(replacement_root_identity)
        self.assertNotEqual(original_root_identity, replacement_root_identity)
        self.assertEqual(bound_root_identity, original_root_identity)
        self.assertIsNotNone(replaced_batch)
        assert replaced_batch is not None
        self.assertEqual(
            {member.name for member in replaced_batch.iterdir()},
            {"leaf", "metadata.json"},
        )
        self.assertEqual(list((replaced_batch / "leaf").iterdir()), [])
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_created_leaf_ambiguous_private_rename_never_reclaims_batch(
        self,
    ) -> None:
        real_rename = MODULE._rename_noreplace_at
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        moved_evidence: Path | None = None
        discard_calls = 0

        def rename_move_private_leaf_then_throw(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal moved_evidence
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name.startswith(".codex-created-leaf-"):
                moved_name = ".codex-moved-private-created-leaf.evidence"
                real_rename(
                    destination_parent_fd,
                    destination_name,
                    source_parent_fd,
                    moved_name,
                )
                moved_evidence = self.source_parent / moved_name
                raise SystemExit("injected post-rename signal")

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=rename_move_private_leaf_then_throw,
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "private isolation failed"),
        ):
            self._evacuate_created_leaf("ambiguous-rename.toml")

        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(moved_evidence)
        assert moved_evidence is not None
        self.assertEqual(moved_evidence.read_bytes(), b'role = "reviewer"\n')
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        batches = tuple(quarantine_root.iterdir())
        self.assertEqual(len(batches), 1)
        self.assertEqual(
            {member.name for member in batches[0].iterdir()},
            {"leaf", "metadata.json"},
        )
        self.assertEqual(list((batches[0] / "leaf").iterdir()), [])

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
            original_leaf_fd = os.open(
                leaf,
                MODULE._directory_open_flags(nofollow=True),
            )
            try:
                self.assertEqual(
                    MODULE._directory_identity(original_leaf_fd),
                    binding.leaf_identity,
                )
                leaf.rmdir()
                leaf.mkdir(mode=0o700)
                metadata = leaf.stat()
                replacement_identity = (metadata.st_dev, metadata.st_ino)
                self.assertNotEqual(replacement_identity, binding.leaf_identity)
            finally:
                os.close(original_leaf_fd)
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

    def test_regular_move_ambiguous_private_rename_never_reclaims_batch(
        self,
    ) -> None:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        real_rename = MODULE._rename_noreplace_at
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        moved_evidence: Path | None = None
        discard_calls = 0

        def rename_move_private_leaf_then_throw(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal moved_evidence
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == self.source.name:
                moved_name = ".codex-moved-private-regular.evidence"
                real_rename(
                    destination_parent_fd,
                    destination_name,
                    source_parent_fd,
                    moved_name,
                )
                moved_evidence = self.source_parent / moved_name
                raise SystemExit("injected post-rename signal")

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_rename_noreplace_at",
                    side_effect=rename_move_private_leaf_then_throw,
                ),
                mock.patch.object(
                    MODULE,
                    "_discard_empty_ephemeral_quarantine_batch",
                    side_effect=observe_discard,
                ),
                self.assertRaisesRegex(SystemExit, "post-rename signal"),
            ):
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="ambiguous-regular",
                    expected_identity=self.source_identity,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(moved_evidence)
        assert moved_evidence is not None
        self.assertEqual(moved_evidence.read_bytes(), b'role = "reviewer"\n')
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_abandoned_allocation_resource_reclaims_and_closes_descriptors(
        self,
    ) -> None:
        descriptors: list[int] = []

        def interrupt_handoff() -> None:
            allocation = MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )
            assert isinstance(
                allocation,
                MODULE.EphemeralQuarantineBatchAllocation,
            )
            descriptors.extend(
                [
                    allocation.quarantine_fd,
                    allocation.batch_fd,
                    allocation.metadata_fd,
                ]
            )
            raise SystemExit("injected allocation handoff failure")

        with self.assertRaisesRegex(SystemExit, "handoff failure") as raised:
            interrupt_handoff()
        del raised
        gc.collect()

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_allocation_close_detaches_fds_before_post_close_baseexception(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        self.assertIsInstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        assert isinstance(
            allocation,
            MODULE.EphemeralQuarantineBatchAllocation,
        )
        allocation.revoke_reclaim()
        metadata_fd = allocation.metadata_fd
        probe_fd = os.open(self.source, os.O_RDONLY)
        reused_fd = -1
        real_close = MODULE._close_fd_quietly

        class PostCloseAbort(BaseException):
            pass

        def close_then_reuse(file_descriptor: int) -> None:
            nonlocal reused_fd
            if file_descriptor == metadata_fd and reused_fd < 0:
                real_close(file_descriptor)
                os.dup2(probe_fd, file_descriptor)
                reused_fd = file_descriptor
                raise PostCloseAbort("injected failure after successful close")
            real_close(file_descriptor)

        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_close_fd_quietly",
                    side_effect=close_then_reuse,
                ),
                self.assertRaisesRegex(PostCloseAbort, "successful close"),
            ):
                allocation.close()

            self.assertEqual(reused_fd, metadata_fd)
            self.assertEqual(
                (
                    allocation.leaf_fd,
                    allocation.metadata_fd,
                    allocation.batch_fd,
                    allocation.quarantine_fd,
                ),
                (-1, -1, -1, -1),
            )
            allocation.close()
            allocation.__del__()
            self.assertEqual(os.fstat(reused_fd).st_ino, self.source.stat().st_ino)
        finally:
            if reused_fd >= 0:
                os.close(reused_fd)
            os.close(probe_fd)

    def test_reclaim_keeps_allocation_descriptors_through_batch_removal(
        self,
    ) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.create_leaf()
        directory_descriptors = (
            allocation.quarantine_fd,
            allocation.batch_fd,
            allocation.leaf_fd,
        )
        metadata_fd = allocation.metadata_fd
        expected_identities = (
            allocation.binding.quarantine_root_identity,
            allocation.binding.batch_identity,
            allocation.binding.leaf_identity,
        )
        real_remove = MODULE._remove_cleanup_ready_batch
        observed_removal = False

        def observe_removal(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> bool:
            nonlocal observed_removal
            observed_removal = True
            self.assertEqual(
                tuple(MODULE._directory_identity(fd) for fd in directory_descriptors),
                expected_identities,
            )
            metadata = os.fstat(metadata_fd)
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                allocation.binding.metadata.file_identity,
            )
            return real_remove(home, ticket)

        with mock.patch.object(
            MODULE,
            "_remove_cleanup_ready_batch",
            side_effect=observe_removal,
        ):
            allocation.reclaim_empty()

        self.assertTrue(observed_removal)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        for descriptor in (*directory_descriptors, metadata_fd):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_allocator_constructor_baseexception_reclaims_bound_scaffold(
        self,
    ) -> None:
        with (
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=SystemExit("injected constructor handoff failure"),
            ),
            self.assertRaisesRegex(SystemExit, "constructor handoff failure"),
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_allocator_exception_keeps_original_fds_through_removal(
        self,
    ) -> None:
        real_dup = os.dup
        original_batch_fd = -1
        original_metadata_fd = -1
        expected_metadata_identity: tuple[int, int] | None = None
        duplication_sources: dict[int, int] = {}
        real_remove = MODULE._remove_cleanup_ready_batch
        observed_removal = False

        def track_dup(file_descriptor: int) -> int:
            duplicate = real_dup(file_descriptor)
            duplication_sources[duplicate] = file_descriptor
            return duplicate

        def interrupt_constructor(
            *,
            batch_fd: int,
            metadata_fd: int,
            binding: MODULE.EphemeralQuarantineBatchBinding,
            **_kwargs: object,
        ) -> object:
            nonlocal original_batch_fd, original_metadata_fd
            nonlocal expected_metadata_identity
            original_batch_fd = duplication_sources[batch_fd]
            original_metadata_fd = duplication_sources[metadata_fd]
            expected_metadata_identity = binding.metadata.file_identity
            raise SystemExit("injected constructor handoff failure")

        def observe_removal(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> bool:
            nonlocal observed_removal
            observed_removal = True
            self.assertEqual(
                MODULE._directory_identity(original_batch_fd),
                ticket.batch_root_identity,
            )
            metadata = os.fstat(original_metadata_fd)
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                expected_metadata_identity,
            )
            return real_remove(home, ticket)

        with (
            mock.patch.object(os, "dup", side_effect=track_dup),
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=interrupt_constructor,
            ),
            mock.patch.object(
                MODULE,
                "_remove_cleanup_ready_batch",
                side_effect=observe_removal,
            ),
            self.assertRaisesRegex(
                SystemExit,
                "constructor handoff failure",
            ) as raised,
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

        self.assertTrue(observed_removal)
        self.assertEqual(getattr(raised.exception, "__notes__", []), [])
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)
        with self.assertRaises(OSError):
            os.fstat(original_batch_fd)
        with self.assertRaises(OSError):
            os.fstat(original_metadata_fd)

    def test_allocator_exception_cleanup_retains_scaffold_after_root_drift(
        self,
    ) -> None:
        relocated_batch: Path | None = None

        def replace_root_then_interrupt(
            *,
            home: Path,
            quarantine_root: Path,
            quarantine_fd: int,
            batch_fd: int,
            metadata_fd: int,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> object:
            del home, quarantine_fd, batch_fd, metadata_fd
            nonlocal relocated_batch
            moved_root = quarantine_root.with_name(quarantine_root.name + ".original")
            quarantine_root.rename(moved_root)
            quarantine_root.mkdir(mode=0o700)
            relocated_batch = quarantine_root / binding.batch_root.name
            (moved_root / binding.batch_root.name).rename(relocated_batch)
            raise SystemExit("injected allocator root drift")

        with mock.patch.object(
            MODULE,
            "EphemeralQuarantineBatchAllocation",
            side_effect=replace_root_then_interrupt,
        ):
            if callable(getattr(SystemExit(), "add_note", None)):
                with self.assertRaisesRegex(
                    SystemExit,
                    "allocator root drift",
                ) as raised:
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                notes = "\n".join(getattr(raised.exception, "__notes__", ()))
                self.assertIn("bound empty scaffold was retained", notes)
                self.assertIn("ephemeral quarantine root changed", notes)
            else:
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "bound empty scaffold was retained",
                ) as raised:
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                self.assertIn("allocator root drift", str(raised.exception))
                self.assertIn(
                    "ephemeral quarantine root changed",
                    str(raised.exception),
                )
                cleanup_error = raised.exception.__cause__
                self.assertIsInstance(cleanup_error, MODULE.SyncError)
                assert cleanup_error is not None
                self.assertIn(
                    "ephemeral quarantine root changed",
                    str(cleanup_error),
                )

        self.assertIsNotNone(relocated_batch)
        assert relocated_batch is not None
        self.assertEqual(
            {member.name for member in relocated_batch.iterdir()},
            {"metadata.json"},
        )
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_leaf_open_failure_after_replacement_retains_scaffold(self) -> None:
        real_allocate = MODULE._quarantine_batch_root
        real_open = os.open
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        allocation: MODULE.EphemeralQuarantineBatchAllocation | None = None
        failure_armed = False
        discard_calls = 0
        original_identity: tuple[int, int] | None = None
        replacement_identity: tuple[int, int] | None = None

        def capture_allocation(*args: object, **kwargs: object) -> object:
            nonlocal allocation
            result = real_allocate(*args, **kwargs)
            if kwargs.get("retain_scaffold_binding"):
                assert isinstance(
                    result,
                    MODULE.EphemeralQuarantineBatchAllocation,
                )
                allocation = result
            return result

        def fail_first_leaf_open(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal failure_armed, original_identity, replacement_identity
            if path == "leaf" and failure_armed:
                failure_armed = False
                assert allocation is not None
                old_leaf_fd = real_open(path, *args, **kwargs)
                try:
                    original_identity = MODULE._directory_identity(old_leaf_fd)
                    self.assertEqual(
                        original_identity,
                        allocation.binding.leaf_identity,
                    )
                    dir_fd = kwargs.get("dir_fd")
                    assert isinstance(dir_fd, int)
                    os.rmdir(path, dir_fd=dir_fd)
                    os.mkdir(path, mode=0o700, dir_fd=dir_fd)
                    replacement_identity = MODULE._named_entry_identity(dir_fd, path)
                finally:
                    os.close(old_leaf_fd)
                raise OSError("injected post-mkdir leaf open failure")
            return real_open(path, *args, **kwargs)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        failure_armed = True
        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_root",
                side_effect=capture_allocation,
            ),
            mock.patch.object(os, "open", side_effect=fail_first_leaf_open),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "post-mkdir leaf open failure",
            ),
        ):
            self._evacuate_created_leaf("leaf-open-replacement.toml")

        self.assertFalse(failure_armed)
        self.assertEqual(discard_calls, 0)
        self.assertIsNotNone(allocation)
        assert allocation is not None
        self.assertEqual(allocation.leaf_fd, -1)
        self.assertIsNotNone(original_identity)
        self.assertIsNotNone(replacement_identity)
        self.assertNotEqual(original_identity, replacement_identity)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        self.assertEqual(
            {entry.name for entry in allocation.batch_root.iterdir()},
            {"leaf", "metadata.json"},
        )

    def test_leaf_initialization_fsync_failure_retains_unbound_leaf(self) -> None:
        real_fsync = os.fsync
        failure_armed = False
        real_discard = MODULE._discard_empty_ephemeral_quarantine_batch
        discard_calls = 0

        def fail_first_bound_leaf_fsync(file_descriptor: int) -> None:
            nonlocal failure_armed
            try:
                names = MODULE._directory_member_names(
                    file_descriptor,
                    maximum_entries=3,
                )
            except (OSError, MODULE.SyncError):
                real_fsync(file_descriptor)
                return
            if names == ("leaf", "metadata.json") and failure_armed:
                failure_armed = False
                raise OSError("injected post-mkdir leaf fsync failure")
            real_fsync(file_descriptor)

        def observe_discard(
            home: Path,
            binding: MODULE.EphemeralQuarantineBatchBinding,
        ) -> None:
            nonlocal discard_calls
            discard_calls += 1
            real_discard(home, binding)

        failure_armed = True
        with (
            mock.patch.object(os, "fsync", side_effect=fail_first_bound_leaf_fsync),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=observe_discard,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "post-mkdir leaf fsync failure",
            ),
        ):
            self._evacuate_created_leaf("leaf-fsync.toml")

        self.assertFalse(failure_armed)
        self.assertEqual(discard_calls, 0)
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)

    def test_metadata_post_write_fsync_failure_reclaims_repeatedly(self) -> None:
        real_open = os.open
        real_fsync = os.fsync
        failure_armed = False
        metadata_fd_to_fail: int | None = None

        def track_metadata_create(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> int:
            nonlocal metadata_fd_to_fail
            file_descriptor = real_open(path, *args, **kwargs)
            flags = args[0] if args else 0
            if failure_armed and path == "metadata.json" and int(flags) & os.O_CREAT:
                metadata_fd_to_fail = file_descriptor
            return file_descriptor

        def commit_then_fail_metadata_fsync(file_descriptor: int) -> None:
            nonlocal failure_armed, metadata_fd_to_fail
            metadata = os.fstat(file_descriptor)
            if (
                failure_armed
                and file_descriptor == metadata_fd_to_fail
                and metadata.st_size > 0
            ):
                failure_armed = False
                metadata_fd_to_fail = None
                real_fsync(file_descriptor)
                raise OSError("injected post-write metadata fsync failure")
            real_fsync(file_descriptor)

        with (
            mock.patch.object(os, "open", side_effect=track_metadata_create),
            mock.patch.object(
                os,
                "fsync",
                side_effect=commit_then_fail_metadata_fsync,
            ),
        ):
            for _attempt in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES + 1):
                failure_armed = True
                metadata_fd_to_fail = None
                with self.assertRaisesRegex(
                    OSError,
                    "post-write metadata fsync failure",
                ):
                    MODULE._quarantine_batch_root(
                        self.home,
                        [],
                        retain_binding=True,
                        retain_scaffold_binding=True,
                    )
                self.assertFalse(failure_armed)
                self.assertIsNone(metadata_fd_to_fail)
                self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_python39_allocator_cleanup_error_chains_both_failures(self) -> None:
        class LegacyBaseException(BaseException):
            add_note = None

        with (
            mock.patch.object(
                MODULE,
                "EphemeralQuarantineBatchAllocation",
                side_effect=LegacyBaseException("legacy allocation failure"),
            ),
            mock.patch.object(
                MODULE,
                "_discard_empty_ephemeral_quarantine_batch",
                side_effect=MODULE.SyncError("bound cleanup failure"),
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "bound cleanup failure.*legacy allocation failure",
            ),
        ):
            MODULE._quarantine_batch_root(
                self.home,
                [],
                retain_binding=True,
                retain_scaffold_binding=True,
            )

    def test_v8_ticket_only_recovery_retires_reservation(self) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        try:
            batch_name = f"20260905T000000Z-{os.getpid()}-{os.getpid()}"
            allocation = MODULE._publish_pending_quarantine_allocation_ticket(
                self.home,
                quarantine_root,
                quarantine_fd,
                batch_name,
                b"{}\n",
            )
        finally:
            MODULE._close_fd_quietly(quarantine_fd)

        self.assertEqual(MODULE._quarantine_batch_count(self.home), 1)
        budget = MODULE.PendingCleanupActionBudget(4)
        self.assertEqual(
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=budget,
            ),
            1,
        )
        self.assertFalse(allocation.path.exists())
        self.assertEqual(MODULE._quarantine_batch_count(self.home), 0)

    def test_private_move_retires_v8_fence_after_double_verification(self) -> None:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            destination, moved = MODULE._move_regular_leaf_to_unique_quarantine(
                self.home,
                self.source_parent,
                parent_fd,
                self.source.name,
                label="v8-retirement",
                expected_identity=self.source_identity,
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertFalse(self.source.exists())
        self.assertEqual(moved.file_identity, self.source_identity)
        self.assertTrue(destination.is_file())
        index = MODULE._pending_cleanup_index_path(self.home)
        self.assertFalse(list(index.glob("*.allocation.json*")))

    def test_private_move_cannot_republish_reclaim_after_v8_retirement(self) -> None:
        parent_fd = MODULE._open_directory_beneath(self.home, self.source_parent)
        try:
            destination, moved, binding = (
                MODULE._move_regular_leaf_to_unique_quarantine(
                    self.home,
                    self.source_parent,
                    parent_fd,
                    self.source.name,
                    label="v8-no-fallback",
                    expected_identity=self.source_identity,
                    retain_batch_binding=True,
                )
            )
        finally:
            MODULE._close_fd_quietly(parent_fd)

        self.assertFalse(self.source.exists())
        self.assertEqual(moved.file_identity, self.source_identity)
        self.assertTrue(destination.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join exact cleanup authority",
        ):
            MODULE._publish_pending_ephemeral_quarantine_cleanup_ticket(
                self.home,
                binding,
            )
        self.assertTrue(destination.is_file())

    def test_v8_to_v7_join_rejects_metadata_size_drift(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        assert binding.allocation_ticket is not None
        changed_ticket = MODULE.replace(
            binding.allocation_ticket,
            metadata_size=binding.allocation_ticket.metadata_size + 1,
        )
        changed_binding = MODULE.replace(
            binding,
            allocation_ticket=changed_ticket,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "does not join exact cleanup authority",
        ):
            MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
                self.home,
                changed_binding,
            )

        self.assertTrue((batch_root / "metadata.json").is_file())
        self.assertTrue(binding.allocation_ticket.path.is_file())

    def test_v8_entity_without_cleanup_authority_blocks_recovery(self) -> None:
        allocation = MODULE._quarantine_batch_root(
            self.home,
            [],
            retain_binding=True,
            retain_scaffold_binding=True,
        )
        assert isinstance(allocation, MODULE.EphemeralQuarantineBatchAllocation)
        allocation.revoke_reclaim()
        allocation.close()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "allocation remains incomplete",
        ):
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            )

        self.assertTrue(allocation.batch_root.is_dir())
        assert allocation.binding.allocation_ticket is not None
        self.assertTrue(allocation.binding.allocation_ticket.path.is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending quarantine allocation must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

    def test_v7_ticket_temp_blocks_mutation_gate(self) -> None:
        temp_path = self._write_v7_ticket_temp()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "ticket temp must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

        self.assertTrue(temp_path.is_file())

    def test_retained_v7_ticket_temp_blocks_mutation_gate(self) -> None:
        temp_path = self._write_v7_ticket_temp()
        retained_name = next(MODULE._retained_pending_cleanup_names(temp_path))
        temp_path.rename(temp_path.with_name(retained_name))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "ticket temp must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)

        self.assertTrue(temp_path.with_name(retained_name).is_file())

    def test_v8_retained_temp_is_promoted_then_ticket_only_retired(self) -> None:
        quarantine_root = (
            MODULE._personal_sync_root(self.home) / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            quarantine_root,
            mode=0o700,
        )
        batch_name = f"20260905T020202Z-{os.getpid()}-{os.getpid()}"
        payload = MODULE._pending_quarantine_allocation_payload(
            batch_name,
            MODULE._directory_identity(quarantine_fd),
            b"{}\n",
        )
        MODULE._close_fd_quietly(quarantine_fd)
        path = MODULE._pending_quarantine_allocation_path(self.home, batch_name)
        index_fd = MODULE._open_or_create_directory_beneath(
            self.home,
            path.parent,
            mode=0o700,
        )
        MODULE._close_fd_quietly(index_fd)
        temp_path = path.with_name(
            batch_name + MODULE.PENDING_QUARANTINE_ALLOCATION_TEMP_SUFFIX
        )
        MODULE._write_exclusive_internal_file(self.home, temp_path, payload)
        retained_name = next(MODULE._retained_pending_cleanup_names(temp_path))
        temp_path.rename(temp_path.with_name(retained_name))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending quarantine allocation must be reconciled",
        ):
            MODULE._require_no_pending_terminal_mutation_authority(self.home)
        self.assertEqual(
            MODULE._cleanup_pending_cleanup_ticket_temps(self.home, limit=4),
            0,
        )
        allocation = MODULE._read_pending_quarantine_allocation_ticket(
            self.home,
            path,
        )
        self.assertIsNotNone(allocation)
        self.assertEqual(
            MODULE._cleanup_pending_quarantine_allocations(
                self.home,
                budget=MODULE.PendingCleanupActionBudget(4),
            ),
            1,
        )
        self.assertFalse(path.exists())

    def test_metadata_tombstone_boundary_rejects_leaf_appearance(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="leaf",
        )

    def test_metadata_tombstone_boundary_rejects_ticket_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="ticket",
        )

    def test_metadata_tombstone_boundary_rejects_batch_policy_drift(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="batch-policy",
        )

    def test_metadata_tombstone_boundary_rejects_root_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=1,
            mutation="root-identity",
        )

    def test_metadata_unlink_boundary_rejects_leaf_appearance(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="leaf",
        )

    def test_metadata_unlink_boundary_rejects_ticket_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="ticket",
        )

    def test_metadata_unlink_boundary_rejects_batch_policy_drift(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="batch-policy",
        )

    def test_metadata_unlink_boundary_rejects_root_replacement(self) -> None:
        self._exercise_metadata_cleanup_boundary_mutation(
            boundary_call=2,
            mutation="root-identity",
        )

    def test_leafless_cleanup_recovers_after_metadata_deletion_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_delete = MODULE._isolate_and_delete_pending_cleanup_file

        def delete_metadata_then_crash(
            home: Path,
            path: Path,
            parent_fd: int,
            snapshot: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
            **kwargs: object,
        ) -> None:
            real_delete(home, path, parent_fd, snapshot, label=label, **kwargs)
            if path == batch_root / "metadata.json":
                raise SystemExit("injected metadata deletion crash")

        with (
            mock.patch.object(
                MODULE,
                "_isolate_and_delete_pending_cleanup_file",
                side_effect=delete_metadata_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "metadata deletion crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse((batch_root / "metadata.json").exists())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_metadata_tombstone_after_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at

        def isolate_metadata_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == "metadata.json" and destination_name.startswith(
                MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-"
            ):
                raise SystemExit("injected metadata tombstone crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_metadata_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "metadata tombstone crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse((batch_root / "metadata.json").exists())
        self.assertEqual(
            len(
                list(
                    batch_root.glob(
                        MODULE.PENDING_CLEANUP_RETAINED_PREFIX + "metadata.json-*"
                    )
                )
            ),
            1,
        )
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_after_batch_isolation_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(batch_root.name)

        def isolate_batch_then_crash(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if source_name == batch_root.name and destination_name == isolated_name:
                raise SystemExit("injected batch isolation crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=isolate_batch_then_crash,
            ),
            self.assertRaisesRegex(SystemExit, "batch isolation crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertTrue(batch_root.with_name(isolated_name).is_dir())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_cleanup_recovers_after_batch_rmdir_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rmdir = os.rmdir
        isolated_name = MODULE._pending_cleanup_isolated_batch_name(batch_root.name)

        def remove_batch_then_crash(
            path: str | bytes,
            *args: object,
            **kwargs: object,
        ) -> None:
            real_rmdir(path, *args, **kwargs)
            if path == isolated_name:
                raise SystemExit("injected batch rmdir crash")

        with (
            mock.patch.object(os, "rmdir", side_effect=remove_batch_then_crash),
            self.assertRaisesRegex(SystemExit, "batch rmdir crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        self.assertFalse(batch_root.exists())
        self.assertFalse(batch_root.with_name(isolated_name).exists())
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_ticket_temp_is_promoted_after_publication_crash(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        real_rename = MODULE._rename_noreplace_at

        def crash_before_ticket_publication(
            source_parent_fd: int,
            source_name: str,
            destination_parent_fd: int,
            destination_name: str,
        ) -> None:
            if (
                source_name
                == batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX
                and destination_name
                == batch_root.name + MODULE.PENDING_CLEANUP_TICKET_SUFFIX
            ):
                raise SystemExit("injected ticket publication crash")
            real_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_before_ticket_publication,
            ),
            self.assertRaisesRegex(SystemExit, "ticket publication crash"),
        ):
            MODULE._discard_empty_ephemeral_quarantine_batch(self.home, binding)

        index = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(
            (
                index / (batch_root.name + MODULE.PENDING_CLEANUP_TICKET_TEMP_SUFFIX)
            ).is_file()
        )
        self._resume_scaffold_cleanup(batch_root)

    def test_leafless_ticket_rejects_leaf_appearance(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        (batch_root / "leaf").mkdir(mode=0o700)

        with self.assertRaisesRegex(MODULE.SyncError, "scaffold leaf appeared"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((batch_root / "leaf").is_dir())
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_metadata_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        metadata = batch_root / "metadata.json"
        metadata.unlink()
        metadata.write_bytes(b'{"foreign": true}\n')
        metadata.chmod(0o600)

        with self.assertRaisesRegex(MODULE.SyncError, "changed before read"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertEqual(metadata.read_bytes(), b'{"foreign": true}\n')
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_batch_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        original = batch_root.with_name(batch_root.name + ".original")
        batch_root.rename(original)
        batch_root.mkdir(mode=0o700)
        (batch_root / "foreign").write_bytes(b"foreign\n")

        with self.assertRaisesRegex(MODULE.SyncError, "batch changed"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((original / "metadata.json").is_file())
        self.assertEqual((batch_root / "foreign").read_bytes(), b"foreign\n")
        self.assertTrue(ticket.path.is_file())

    def test_leafless_ticket_rejects_quarantine_root_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        quarantine_root = batch_root.parent
        original = quarantine_root.with_name(quarantine_root.name + ".original")
        quarantine_root.rename(original)
        quarantine_root.mkdir(mode=0o700)

        with self.assertRaisesRegex(MODULE.SyncError, "quarantine root changed"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((original / batch_root.name / "metadata.json").is_file())
        self.assertTrue(ticket.path.is_file())

    def test_leafless_cleanup_rejects_ticket_replacement(self) -> None:
        batch_root, binding = self._allocate_unowned_scaffold()
        ticket = MODULE._publish_pending_ephemeral_quarantine_scaffold_cleanup_ticket(
            self.home,
            binding,
        )
        ticket.path.unlink()
        ticket.path.write_bytes(b"{}\n")
        ticket.path.chmod(0o600)

        with self.assertRaisesRegex(MODULE.SyncError, "changed before read"):
            MODULE._remove_cleanup_ready_batch(self.home, ticket)

        self.assertTrue((batch_root / "metadata.json").is_file())
        self.assertEqual(ticket.path.read_bytes(), b"{}\n")

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
