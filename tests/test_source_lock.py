from __future__ import annotations

from contextlib import ExitStack, redirect_stderr
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MIRROR_MODULE = _load_module(
    "sync_canonical_mirrors",
    REPOSITORY_ROOT / "scripts" / "sync_canonical_mirrors.py",
)
ENGINE_MODULE = _load_module(
    "codex_personal_sync_schema_parity",
    REPOSITORY_ROOT / "scripts" / "codex_personal_sync.py",
)


class CountingScandir:
    class Entry:
        def __init__(self, owner: CountingScandir, value: object) -> None:
            self.owner = owner
            self.value = value

        @property
        def name(self) -> object:
            self.owner.name_reads += 1
            return self.value

    def __init__(
        self,
        names: list[object],
        *,
        error_at_next: int | None = None,
        close_error: bool = False,
    ) -> None:
        self.names = names
        self.error_at_next = error_at_next
        self.close_error = close_error
        self.index = 0
        self.next_calls = 0
        self.yielded_count = 0
        self.name_reads = 0
        self.closed = False

    def __enter__(self) -> CountingScandir:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __iter__(self) -> CountingScandir:
        return self

    def __next__(self) -> Entry:
        self.next_calls += 1
        if self.error_at_next == self.next_calls:
            raise OSError("simulated scandir producer failure")
        if self.index >= len(self.names):
            raise StopIteration
        value = self.names[self.index]
        self.index += 1
        self.yielded_count += 1
        return self.Entry(self, value)

    def close(self) -> None:
        self.closed = True
        if self.close_error:
            raise OSError("simulated scandir close failure")


class MirrorGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-tests."
        )
        self.root = Path(os.path.realpath(self.temporary_directory.name))
        self.host_private_git_control_parent = MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
        self.host_private_control_root_specs = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS
        self.private_git_control_parent = (
            self.root / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
        )
        self.private_git_control_parent.mkdir(mode=0o700)
        self.private_control_parent_patch = mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_GIT_CONTROL_PARENT",
            self.private_git_control_parent,
        )
        self.private_control_parent_patch.start()
        self.addCleanup(self.private_control_parent_patch.stop)
        self.private_control_root_specs_patch = mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_CONTROL_ROOT_SPECS",
            (
                MIRROR_MODULE.PrivateControlRootSpec(
                    root_id="test-primary-home-v1",
                    parent_path=self.private_git_control_parent,
                    allocate=True,
                    account_home=self.root,
                    shared_parent=False,
                ),
            ),
        )
        self.private_control_root_specs_patch.start()
        self.addCleanup(self.private_control_root_specs_patch.stop)
        self.canonical_root = self.root / "canonical"
        self.target_root = self.root / "consumer"
        self.canonical_root.mkdir()
        self.target_root.mkdir()
        self._init_git_repository(
            self.canonical_root,
            "Joey-Tools/canonical-fixture",
        )
        self._init_git_repository(
            self.target_root,
            "Joey-Tools/toolbox-fixture",
        )
        self.source_path = self.canonical_root / "scripts" / "engine.py"
        self.source_path.parent.mkdir()
        self.source_path.write_bytes(b"canonical engine\n")
        self.source_path.chmod(0o755)
        (self.canonical_root / MIRROR_MODULE.GENERATOR_PATH.as_posix()).write_bytes(
            b"fixture generator contract\n"
        )
        (self.canonical_root / MIRROR_MODULE.GENERATOR_PATH.as_posix()).chmod(0o755)
        (self.canonical_root / MIRROR_MODULE.RULES_PATH.as_posix()).write_text(
            "# Fixture rules\n",
            encoding="utf-8",
        )
        self._write_lock()
        self.source_commit = self._commit(self.canonical_root, "fixture sources")
        (self.target_root / "README.md").write_text(
            "# Consumer fixture\n",
            encoding="utf-8",
        )
        self._commit(self.target_root, "consumer fixture")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, root: Path, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            },
            timeout=30,
        )
        return completed.stdout

    def _git_with_input(
        self,
        root: Path,
        payload: bytes,
        *arguments: str,
    ) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            input=payload,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            },
            timeout=30,
        )
        return completed.stdout

    def _open_process_liveness_pipe(self) -> tuple[int, int]:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        return read_fd, write_fd

    def _assert_directory_lock_contended(self, path: Path) -> None:
        competitor_fd = os.open(path, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            with self.assertRaises(BlockingIOError):
                MIRROR_MODULE.fcntl.flock(
                    competitor_fd,
                    MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB,
                )
        finally:
            os.close(competitor_fd)

    def _assert_directory_lock_available(self, path: Path) -> None:
        competitor_fd = os.open(path, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            MIRROR_MODULE.fcntl.flock(
                competitor_fd,
                MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB,
            )
            MIRROR_MODULE.fcntl.flock(competitor_fd, MIRROR_MODULE.fcntl.LOCK_UN)
        finally:
            os.close(competitor_fd)

    def _assert_process_liveness_pipe_closed(
        self,
        read_fd: int,
        *,
        timeout: float = 1,
    ) -> None:
        selector = selectors.DefaultSelector()
        try:
            selector.register(read_fd, selectors.EVENT_READ)
            events = selector.select(timeout)
            self.assertTrue(
                events,
                "a launched process still retains the liveness writer",
            )
            self.assertEqual(
                os.read(read_fd, 1),
                b"",
                "the liveness pipe contained unexpected payload bytes",
            )
        finally:
            selector.close()

    def _finish_process_group_fixture(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def _macos_git_locator_fixture(
        self,
        suffix: str = "",
    ) -> tuple[Path, Path, Path, tuple[int, int, int]]:
        locator = self.root / f"xcrun-fixture{suffix}"
        shutil.copyfile("/usr/bin/true", locator)
        locator.chmod(0o755)
        temporary_directory = self.root / f"locator-temporary-directory{suffix}"
        temporary_directory.mkdir(mode=0o700)
        developer_git = self.root / f"developer-git{suffix}"
        shutil.copyfile(MIRROR_MODULE.GIT_EXECUTABLE, developer_git)
        developer_git.chmod(0o755)
        expected_access_policy = MIRROR_MODULE._access_policy(
            temporary_directory.stat()
        )
        return (
            locator,
            temporary_directory,
            developer_git,
            expected_access_policy,
        )

    def _init_git_repository(self, root: Path, repository: str) -> None:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.name", "Mirror Fixture")
        self._git(root, "config", "user.email", "mirror@example.invalid")
        self._git(root, "config", "commit.gpgsign", "false")
        self._git(
            root,
            "remote",
            "add",
            "origin",
            f"https://github.com/{repository}.git",
        )

    def _commit(self, root: Path, message: str) -> str:
        self._git(root, "add", "-A")
        self._git(root, "commit", "--no-gpg-sign", "-q", "-m", message)
        return self._git(root, "rev-parse", "HEAD").decode("ascii").strip()

    def _build_forgeable_history(self) -> tuple[str, str, str, str]:
        root_commit = self.source_commit
        self.source_path.write_bytes(b"first ancestry change\n")
        first_commit = self._commit(self.canonical_root, "first ancestry change")
        self.source_path.write_bytes(b"review head\n")
        head_commit = self._commit(self.canonical_root, "review head")
        side_commit = (
            self._git_with_input(
                self.canonical_root,
                b"forged side\n",
                "commit-tree",
                f"{root_commit}^{{tree}}",
                "-p",
                root_commit,
            )
            .decode("ascii")
            .strip()
        )
        self._git(
            self.canonical_root,
            "update-ref",
            "refs/heads/forged-side",
            side_commit,
        )
        return root_commit, first_commit, head_commit, side_commit

    def _git_cache_chunks(
        self,
        payload: bytes | bytearray,
        *,
        magic: bytes,
        header_size: int,
    ) -> dict[bytes, tuple[int, int]]:
        self.assertEqual(payload[:4], magic)
        self.assertEqual(payload[4], 1)
        self.assertEqual(payload[5], 1)
        chunk_count = payload[6]
        table_end = header_size + (chunk_count + 1) * 12
        self.assertLessEqual(table_end, len(payload))
        entries: list[tuple[bytes, int]] = []
        for index in range(chunk_count + 1):
            offset = header_size + index * 12
            chunk_id = bytes(payload[offset : offset + 4])
            chunk_offset = int.from_bytes(
                payload[offset + 4 : offset + 12],
                "big",
            )
            entries.append((chunk_id, chunk_offset))
        self.assertEqual(entries[-1][0], b"\0\0\0\0")
        return {
            chunk_id: (chunk_offset, entries[index + 1][1])
            for index, (chunk_id, chunk_offset) in enumerate(entries[:-1])
        }

    def _cache_oid_index(
        self,
        payload: bytes | bytearray,
        chunks: dict[bytes, tuple[int, int]],
        object_id: str,
    ) -> int:
        fanout_start, fanout_end = chunks[b"OIDF"]
        self.assertEqual(fanout_end - fanout_start, 256 * 4)
        object_count = int.from_bytes(payload[fanout_end - 4 : fanout_end], "big")
        lookup_start, lookup_end = chunks[b"OIDL"]
        self.assertEqual(lookup_end - lookup_start, object_count * 20)
        raw_object_id = bytes.fromhex(object_id)
        object_ids = [
            bytes(payload[offset : offset + 20])
            for offset in range(lookup_start, lookup_end, 20)
        ]
        return object_ids.index(raw_object_id)

    def _pack_index_v2_layout(
        self,
        payload: bytes | bytearray,
    ) -> tuple[list[bytes], int, int]:
        self.assertEqual(payload[:4], b"\xfftOc")
        self.assertEqual(int.from_bytes(payload[4:8], "big"), 2)
        fanout_start = 8
        fanout_end = fanout_start + 256 * 4
        object_count = int.from_bytes(
            payload[fanout_end - 4 : fanout_end],
            "big",
        )
        object_ids_start = fanout_end
        object_ids_end = object_ids_start + object_count * 20
        object_ids = [
            bytes(payload[offset : offset + 20])
            for offset in range(object_ids_start, object_ids_end, 20)
        ]
        crc_start = object_ids_end
        offsets_start = crc_start + object_count * 4
        offsets_end = offsets_start + object_count * 4
        self.assertLessEqual(offsets_end + 40, len(payload))
        return object_ids, crc_start, offsets_start

    def _generate(self, mirror: str = "toolbox") -> int:
        return MIRROR_MODULE.generate_mirror(
            self.canonical_root,
            self.target_root,
            mirror,
            self.source_commit,
        )

    def _source_record(self) -> dict[str, str]:
        payload = self.source_path.read_bytes()
        mode = stat.S_IMODE(self.source_path.stat().st_mode)
        return {
            "path": "scripts/engine.py",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": f"{mode:04o}",
        }

    def _write_lock(self) -> None:
        payload = {
            "version": 1,
            "hash_algorithm": "sha256",
            "canonical_repository": "Joey-Tools/canonical-fixture",
            "sources": {
                "engine": self._source_record(),
            },
            "mirrors": {
                "private": {
                    "repository": "Joey-Tools/private-fixture",
                    "files": {
                        "engine": "scripts/engine.py",
                    },
                },
                "toolbox": {
                    "repository": "Joey-Tools/toolbox-fixture",
                    "files": {
                        "engine": "scripts/engine.py",
                    },
                },
            },
        }
        (self.canonical_root / "sync-source-lock.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_bounded_directory_scan_stops_at_limit_plus_one(self) -> None:
        producer = CountingScandir(["c", "b", "a", "overflow", "unread"])

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "scandir",
                return_value=producer,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "scanned at least 4 entries and retained 3",
            ),
        ):
            MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=3,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
            )

        self.assertEqual(producer.next_calls, 4)
        self.assertEqual(producer.yielded_count, 4)
        self.assertEqual(producer.name_reads, 3)
        self.assertTrue(producer.closed)

    def test_bounded_directory_scan_sorts_only_bounded_names(self) -> None:
        producer = CountingScandir(["c", "a", "b"])

        with mock.patch.object(
            MIRROR_MODULE.os,
            "scandir",
            return_value=producer,
        ):
            names = MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=3,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
            )

        self.assertEqual(names, ("a", "b", "c"))
        self.assertEqual(producer.yielded_count, 3)
        self.assertEqual(producer.name_reads, 3)
        self.assertTrue(producer.closed)

    def test_bounded_directory_scan_closes_iterator_after_producer_error(
        self,
    ) -> None:
        producer = CountingScandir(
            ["first", "unread"],
            error_at_next=2,
        )

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "scandir",
                return_value=producer,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "cannot inventory test directory: simulated scandir producer failure",
            ),
        ):
            MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=3,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
            )

        self.assertEqual(producer.yielded_count, 1)
        self.assertEqual(producer.name_reads, 1)
        self.assertTrue(producer.closed)

    def test_bounded_directory_scan_honors_remaining_operation_budget(
        self,
    ) -> None:
        producer = CountingScandir(["a", "b", "overflow", "unread"])
        operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() + 30,
            remaining_bytes=1024,
            remaining_entries=2,
        )

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "scandir",
                return_value=producer,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "aggregate budget.*scanned at least 3 entries and retained 2",
            ),
        ):
            MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=100,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
                operation=operation,
            )

        self.assertEqual(producer.yielded_count, 3)
        self.assertEqual(producer.name_reads, 2)
        self.assertTrue(producer.closed)

    def test_bounded_directory_scan_reports_close_failure_distinctly(self) -> None:
        producer = CountingScandir(["entry"], close_error=True)

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "scandir",
                return_value=producer,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "failed to close directory scan for test directory: "
                "simulated scandir close failure",
            ),
        ):
            MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=1,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
            )

        self.assertTrue(producer.closed)

    def test_bounded_directory_scan_close_failure_preserves_cap_error(
        self,
    ) -> None:
        producer = CountingScandir(["retained", "overflow"], close_error=True)
        stderr = io.StringIO()

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "scandir",
                return_value=producer,
            ),
            redirect_stderr(stderr),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "test directory exceeds its entry limit; "
                "scanned at least 2 entries and retained 1",
            ),
        ):
            MIRROR_MODULE._bounded_sorted_directory_names(
                123,
                maximum_entries=1,
                label="test directory",
                limit_error="test directory exceeds its entry limit",
            )

        self.assertTrue(producer.closed)
        self.assertIn(
            "warning: failed to close directory scan for test directory",
            stderr.getvalue(),
        )

    def test_private_owner_reader_charges_both_bounded_reads(self) -> None:
        owner_path = self.root / "bounded-owner-record.json"
        owner_path.write_bytes(b"{}\n")
        parent_fd = os.open(self.root, MIRROR_MODULE._DIRECTORY_FLAGS)
        owner_fd = os.open(owner_path, MIRROR_MODULE._FILE_READ_FLAGS)
        operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() + 30,
            remaining_bytes=100,
            remaining_entries=10,
        )
        try:
            snapshot = MIRROR_MODULE._safe_read_private_owner_record_snapshot(
                parent_fd,
                owner_path.name,
                PurePosixPath(owner_path.name),
                file_fd=owner_fd,
                operation=operation,
            )
        finally:
            os.close(owner_fd)
            os.close(parent_fd)
        self.assertEqual(snapshot.payload, b"{}\n")
        self.assertEqual(operation.remaining_bytes, 94)

    def test_private_owner_reader_stops_at_hard_limit_plus_one(self) -> None:
        owner_path = self.root / "producer-bounded-owner-record.json"
        owner_path.write_bytes(b"")
        parent_fd = os.open(self.root, MIRROR_MODULE._DIRECTORY_FLAGS)
        owner_fd = os.open(owner_path, MIRROR_MODULE._FILE_READ_FLAGS)
        operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() + 30,
            remaining_bytes=10_000,
            remaining_entries=10,
        )
        requested_sizes = []

        def produce_full_chunk(_fd, requested):
            requested_sizes.append(requested)
            return b"x" * requested

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "read",
                    side_effect=produce_full_chunk,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private owner record exceeds 4096 bytes",
                ),
            ):
                MIRROR_MODULE._safe_read_private_owner_record_snapshot(
                    parent_fd,
                    owner_path.name,
                    PurePosixPath(owner_path.name),
                    file_fd=owner_fd,
                    operation=operation,
                )
        finally:
            os.close(owner_fd)
            os.close(parent_fd)
        self.assertEqual(requested_sizes, [4096, 1])
        self.assertEqual(operation.remaining_bytes, 10_000 - 4097)

    def test_git_snapshot_scan_applies_remaining_entry_cap_before_sort(
        self,
    ) -> None:
        directory = self.root / "bounded-git-snapshot"
        directory.mkdir()
        for name in ("a", "b", "c"):
            (directory / name).write_bytes(name.encode("ascii"))
        directory_fd = os.open(directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_GIT_SNAPSHOT_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "Git control snapshot exceeds its entry limit; "
                    "scanned at least 3 entries and retained 2",
                ),
            ):
                MIRROR_MODULE._snapshot_git_directory_tree(
                    directory_fd,
                    None,
                    prefix=PurePosixPath("git"),
                    budget={"entries": 0, "bytes": 0},
                )
        finally:
            os.close(directory_fd)

    def test_private_object_scan_applies_remaining_entry_cap_before_sort(
        self,
    ) -> None:
        directory = self.root / "bounded-object-snapshot"
        directory.mkdir()
        for name in ("a", "b", "c"):
            (directory / name).write_bytes(name.encode("ascii"))
        directory_fd = os.open(directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_GIT_SNAPSHOT_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private Git object snapshot exceeds its entry limit; "
                    "scanned at least 3 entries and retained 2",
                ),
            ):
                MIRROR_MODULE._inventory_private_git_objects(
                    directory_fd,
                    None,
                    bind_content=False,
                )
        finally:
            os.close(directory_fd)

    def test_git_snapshot_scan_reserves_siblings_before_recursing(self) -> None:
        directory = self.root / "recursive-git-snapshot"
        (directory / "a").mkdir(parents=True)
        (directory / "a" / "child").write_bytes(b"child\n")
        (directory / "z").write_bytes(b"sibling\n")
        directory_fd = os.open(directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        budget = {"entries": 0, "bytes": 0}
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_GIT_SNAPSHOT_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "Git control snapshot exceeds its entry limit; "
                    "scanned at least 1 entries and retained 0",
                ),
            ):
                MIRROR_MODULE._snapshot_git_directory_tree(
                    directory_fd,
                    None,
                    prefix=PurePosixPath("git"),
                    budget=budget,
                )
        finally:
            os.close(directory_fd)

        self.assertEqual(budget["scanned_entries"], 2)
        self.assertEqual(budget["entries"], 1)

    def test_private_object_scan_reserves_siblings_before_recursing(self) -> None:
        directory = self.root / "recursive-object-snapshot"
        (directory / "a").mkdir(parents=True)
        (directory / "a" / "child").write_bytes(b"child\n")
        (directory / "z").write_bytes(b"sibling\n")
        directory_fd = os.open(directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_GIT_SNAPSHOT_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private Git object snapshot exceeds its entry limit; "
                    "scanned at least 1 entries and retained 0",
                ),
            ):
                MIRROR_MODULE._inventory_private_git_objects(
                    directory_fd,
                    None,
                    bind_content=False,
                )
        finally:
            os.close(directory_fd)

    def test_private_cleanup_scan_reserves_siblings_before_recursing(self) -> None:
        directory = self.root / "recursive-cleanup-snapshot"
        (directory / "a").mkdir(parents=True)
        (directory / "a" / "child").write_bytes(b"child\n")
        (directory / "z").write_bytes(b"sibling\n")
        directory_fd = os.open(directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        budget = {"entries": 0}
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_GIT_SNAPSHOT_ENTRIES",
                    2,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "isolated private Git cleanup exceeds its entry limit; "
                    "scanned at least 1 entries and retained 0",
                ),
            ):
                MIRROR_MODULE._remove_private_tree_contents(
                    directory_fd,
                    PurePosixPath("cleanup"),
                    budget=budget,
                )
        finally:
            os.close(directory_fd)

        self.assertEqual(budget["scanned_entries"], 2)
        self.assertEqual(budget["entries"], 1)
        self.assertTrue((directory / "a" / "child").is_file())
        self.assertTrue((directory / "z").is_file())

    def test_private_tool_root_scan_applies_cap_before_sort(self) -> None:
        tool_root_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        tool_root_path.mkdir(mode=0o700)
        quarantine_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine_path.mkdir(mode=0o700)
        for name in ("a", "b", "c"):
            (tool_root_path / name).write_bytes(name.encode("ascii"))
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        tool_root = MIRROR_MODULE._bind_absolute_control_object(
            tool_root_path,
            "test private Git tool root",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            quarantine_path,
            "test private Git quarantine root",
            require_directory=True,
        )
        root_id = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id
        tool_root.root_id = root_id
        quarantine.root_id = root_id
        try:
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            with (
                mock.patch.object(MIRROR_MODULE, "MAX_TOOL_ROOT_ENTRIES", 2),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private Git tool root exceeds 2 entries; "
                    "scanned at least 3 entries and retained 2",
                ),
            ):
                MIRROR_MODULE._recover_stale_private_snapshots(
                    bound_root,
                    tool_root,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
        finally:
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(quarantine.fd)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_generate_and_check_are_strictly_one_way(self) -> None:
        target = self.target_root / "scripts" / "engine.py"
        target.parent.mkdir()
        target.write_bytes(b"consumer-only change\n")
        self._commit(self.target_root, "tracked old mirror")
        original_source = self.source_path.read_bytes()

        generated = self._generate()

        self.assertEqual(generated, 1)
        self.assertEqual(target.read_bytes(), original_source)
        self.assertEqual(self.source_path.read_bytes(), original_source)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            ),
            1,
        )

        target.write_bytes(b"drifted consumer\n")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "content differs",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        self.assertEqual(self.source_path.read_bytes(), original_source)

    def test_repeated_exact_generate_only_stabilizes_target_quarantine(
        self,
    ) -> None:
        self.assertEqual(self._generate(), 1)
        self._commit(self.target_root, "track generated mirror")
        target = self.target_root / "scripts" / "engine.py"
        receipt = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        receipt_identity = (receipt.stat().st_dev, receipt.stat().st_ino)
        quarantine = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine_names = (
            sorted(path.name for path in quarantine.iterdir())
            if quarantine.exists()
            else []
        )
        private_quarantine = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        private_quarantine_names = {path.name for path in private_quarantine.iterdir()}

        for _index in range(5):
            self.assertEqual(self._generate(), 1)

        self.assertEqual((target.stat().st_dev, target.stat().st_ino), target_identity)
        self.assertEqual(
            (receipt.stat().st_dev, receipt.stat().st_ino),
            receipt_identity,
        )
        self.assertEqual(
            (
                sorted(path.name for path in quarantine.iterdir())
                if quarantine.exists()
                else []
            ),
            quarantine_names,
        )
        final_private_quarantine_names = {
            path.name for path in private_quarantine.iterdir()
        }
        self.assertLess(
            len(private_quarantine_names),
            len(final_private_quarantine_names),
        )
        self.assertTrue(
            private_quarantine_names.issubset(final_private_quarantine_names)
        )
        self.assertEqual(
            self._git(self.target_root, "status", "--porcelain"),
            b"",
        )
        for marker in (
            MIRROR_MODULE.TRANSACTION_PATH,
            MIRROR_MODULE.TRANSACTION_TEMP_PATH,
            MIRROR_MODULE.TRANSACTION_COMPLETE_PATH,
        ):
            self.assertFalse((self.target_root / marker.as_posix()).exists())

    def test_new_commit_with_unchanged_source_does_not_replace_target(self) -> None:
        self.assertEqual(self._generate(), 1)
        self._commit(self.target_root, "track generated mirror")
        target = self.target_root / "scripts" / "engine.py"
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        (self.canonical_root / "UNRELATED.md").write_text(
            "Unrelated canonical metadata.\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "advance canonical commit without source changes",
        )

        self.assertEqual(self._generate(), 1)

        self.assertEqual((target.stat().st_dev, target.stat().st_ino), target_identity)
        receipt = json.loads(
            (self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["canonical_commit"], self.source_commit)

    def test_generate_conditionally_retires_a_renamed_receipt_target(self) -> None:
        self._generate()
        self._commit(self.target_root, "track first generated mirror")
        old_target = self.target_root / "scripts" / "engine.py"
        new_target = self.target_root / "generated" / "engine.py"

        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["mirrors"]["toolbox"]["files"]["engine"] = "generated/engine.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "rename generated target",
        )

        self.assertEqual(
            {
                path.as_posix()
                for path in MIRROR_MODULE.managed_mirror_paths(
                    self.canonical_root,
                    self.target_root,
                    "toolbox",
                )
            },
            {
                "generated-sync-source-lock.json",
                "generated/engine.py",
                "scripts/engine.py",
            },
        )
        self.assertEqual(self._generate(), 1)
        self.assertFalse(old_target.exists())
        self.assertEqual(new_target.read_bytes(), self.source_path.read_bytes())
        status = self._git(
            self.target_root,
            "status",
            "--short",
            "--untracked-files=all",
        )
        self.assertIn(b" D scripts/engine.py", status)
        self.assertIn(b"?? generated/engine.py", status)
        self._commit(self.target_root, "track renamed generated mirror")
        self.assertEqual(
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            ),
            1,
        )

    def test_managed_paths_reads_target_commit_without_checkout(self) -> None:
        base_sha = (
            self._git(self.target_root, "rev-parse", "HEAD").decode("ascii").strip()
        )
        self._generate()
        generated_sha = self._commit(self.target_root, "generated target commit")
        (self.target_root / ".gitattributes").write_text(
            "scripts/engine.py filter=sentinel\n",
            encoding="utf-8",
        )
        target_commit = self._commit(
            self.target_root,
            "untrusted target attributes",
        )
        self._git(self.target_root, "switch", "--detach", "-q", base_sha)
        marker = self.root / "smudge-filter-ran"
        self._git(
            self.target_root,
            "config",
            "filter.sentinel.smudge",
            f"touch {marker}",
        )
        self._git(
            self.target_root,
            "config",
            "filter.sentinel.required",
            "true",
        )
        head_before = self._git(self.target_root, "rev-parse", "HEAD")
        index_before = self._git(
            self.target_root,
            "ls-files",
            "--stage",
            "-z",
        )

        paths = MIRROR_MODULE.managed_mirror_paths(
            self.canonical_root,
            self.target_root,
            "toolbox",
            target_commit=target_commit,
        )

        self.assertEqual(
            [path.as_posix() for path in paths],
            ["generated-sync-source-lock.json", "scripts/engine.py"],
        )
        self.assertFalse(marker.exists())
        self.assertEqual(self._git(self.target_root, "rev-parse", "HEAD"), head_before)
        self.assertEqual(
            self._git(self.target_root, "ls-files", "--stage", "-z"),
            index_before,
        )
        self.assertEqual(
            self._git(self.target_root, "rev-parse", f"{generated_sha}^{{commit}}"),
            generated_sha.encode("ascii") + b"\n",
        )

    def test_managed_paths_target_commit_rejects_receipt_blob_drift(self) -> None:
        base_sha = (
            self._git(self.target_root, "rev-parse", "HEAD").decode("ascii").strip()
        )
        self._generate()
        self._commit(self.target_root, "generated target commit")
        (self.target_root / "scripts" / "engine.py").write_bytes(
            b"tampered target commit payload\n"
        )
        tampered_sha = self._commit(self.target_root, "tamper generated payload")
        self._git(self.target_root, "switch", "--detach", "-q", base_sha)
        head_before = self._git(self.target_root, "rev-parse", "HEAD")
        index_before = self._git(
            self.target_root,
            "ls-files",
            "--stage",
            "-z",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "managed path digest differs in target commit",
        ):
            MIRROR_MODULE.managed_mirror_paths(
                self.canonical_root,
                self.target_root,
                "toolbox",
                target_commit=tampered_sha,
            )

        self.assertEqual(self._git(self.target_root, "rev-parse", "HEAD"), head_before)
        self.assertEqual(
            self._git(self.target_root, "ls-files", "--stage", "-z"),
            index_before,
        )

    def test_generate_conditionally_retires_a_removed_receipt_target(self) -> None:
        extra_source = self.canonical_root / "scripts" / "extra.py"
        extra_source.write_bytes(b"canonical extra\n")
        extra_source.chmod(0o644)
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["sources"]["extra"] = {
            "path": "scripts/extra.py",
            "sha256": hashlib.sha256(extra_source.read_bytes()).hexdigest(),
            "mode": "0644",
        }
        for mirror in lock["mirrors"].values():
            mirror["files"]["extra"] = "scripts/extra.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "add second generated target",
        )
        self._generate()
        self._commit(self.target_root, "track two generated targets")
        removed_target = self.target_root / "scripts" / "extra.py"

        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        del lock["mirrors"]["toolbox"]["files"]["extra"]
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "retire second generated target",
        )

        self.assertEqual(self._generate(), 1)
        self.assertFalse(removed_target.exists())
        self.assertTrue((self.target_root / "scripts" / "engine.py").exists())

    def test_generate_rejects_self_consistent_forged_prior_receipt(self) -> None:
        self._generate()
        user_file = self.target_root / "user-owned.txt"
        user_file.write_bytes(b"user-owned data\n")
        receipt_path = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["files"].append(
            {
                "source_name": "forged_user_file",
                "source_path": "README.md",
                "target_path": "user-owned.txt",
                "sha256": hashlib.sha256(user_file.read_bytes()).hexdigest(),
                "mode": "0644",
            }
        )
        receipt["files"].sort(key=lambda item: item["source_name"])
        receipt["mapping_digest"] = MIRROR_MODULE._deterministic_digest(
            [
                {
                    "source_name": item["source_name"],
                    "source_path": item["source_path"],
                    "target_path": item["target_path"],
                }
                for item in receipt["files"]
            ]
        )
        receipt["file_set_digest"] = MIRROR_MODULE._deterministic_digest(
            sorted(item["target_path"] for item in receipt["files"])
        )
        receipt["tree_digest"] = MIRROR_MODULE._deterministic_digest(
            [
                {
                    "target_path": item["target_path"],
                    "sha256": item["sha256"],
                    "mode": item["mode"],
                }
                for item in sorted(
                    receipt["files"],
                    key=lambda item: item["target_path"],
                )
            ]
        )
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._commit(self.target_root, "forge self-consistent receipt")

        self.source_path.write_bytes(b"next canonical engine\n")
        MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=False)
        self.source_commit = self._commit(
            self.canonical_root,
            "advance canonical engine",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "does not exactly match its historical canonical source lock",
        ):
            self._generate()
        self.assertEqual(user_file.read_bytes(), b"user-owned data\n")

    def test_generate_rejects_mode_0600_journal_path_expansion(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        user_file = self.target_root / "user-owned.txt"
        user_file.write_bytes(b"user-owned data\n")
        self._commit(self.target_root, "track user-owned file")
        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        crashed = False

        def crash_after_target_write(
            root,
            relative_path,
            payload,
            mode,
            **kwargs,
        ):
            nonlocal crashed
            result = real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )
            if (
                relative_path == MIRROR_MODULE.PurePosixPath("scripts/engine.py")
                and not crashed
            ):
                crashed = True
                raise SimulatedCrash()
            return result

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=crash_after_target_write,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()

        journal_path = self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()
        document = json.loads(journal_path.read_text(encoding="utf-8"))
        document["files"]["user-owned.txt"] = {
            "initial": MIRROR_MODULE._desired_file_record(
                user_file.read_bytes(),
                0o644,
            ),
            "desired": None,
        }
        source_lock = MIRROR_MODULE.load_source_lock(self.canonical_root)
        mirror = MIRROR_MODULE._selected_mirror(source_lock, "toolbox")
        bound_target = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_target)
            index_snapshot = MIRROR_MODULE._target_index_snapshot(
                bound_target,
                mirror,
                {MIRROR_MODULE.PurePosixPath("user-owned.txt")},
            )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_target)
        document["index_sha256"] = hashlib.sha256(index_snapshot).hexdigest()
        journal_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        journal_path.chmod(0o600)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "managed paths exceed the HEAD/index-bound prior receipt",
        ):
            self._generate()
        self.assertEqual(user_file.read_bytes(), b"user-owned data\n")
        self.assertTrue(journal_path.exists())
        self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)

    def test_generate_preserves_a_racing_retired_target_replacement(self) -> None:
        self._generate()
        self._commit(self.target_root, "track first generated mirror")
        old_target = self.target_root / "scripts" / "engine.py"
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["mirrors"]["toolbox"]["files"]["engine"] = "generated/engine.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "rename generated target",
        )
        real_remove = MIRROR_MODULE._atomic_remove_relative

        def replace_before_remove(root, relative_path, expected_snapshot):
            old_target.write_bytes(b"racing consumer replacement\n")
            return real_remove(root, relative_path, expected_snapshot)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_remove_relative",
                side_effect=replace_before_remove,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "conditional retired target changed",
            ),
        ):
            self._generate()
        self.assertEqual(
            old_target.read_bytes(),
            b"racing consumer replacement\n",
        )

    def test_isolated_cleanup_quarantines_final_move_replacement(self) -> None:
        artifact = self.target_root / "cleanup-artifact"
        artifact.write_bytes(b"expected cleanup artifact\n")
        artifact.chmod(0o600)
        saved = self.target_root / "saved-original"
        replacement_payload = b"concurrent replacement\n"
        bound_target = MIRROR_MODULE._bind_root(
            self.target_root,
            exclusive=True,
        )
        real_persist = MIRROR_MODULE._persist_quarantined_file
        injected = False

        def replace_before_durable_quarantine(
            quarantine,
            source_parent_fd,
            source_name,
            expected,
            display_path,
            **kwargs,
        ):
            nonlocal injected
            if not injected:
                injected = True
                os.rename(
                    source_name,
                    saved.name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=source_parent_fd,
                )
                replacement_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    expected.mode,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(replacement_fd, replacement_payload)
                    os.fsync(replacement_fd)
                finally:
                    os.close(replacement_fd)
            return real_persist(
                quarantine,
                source_parent_fd,
                source_name,
                expected,
                display_path,
                **kwargs,
            )

        try:
            snapshot = MIRROR_MODULE._safe_read_snapshot(
                bound_target,
                MIRROR_MODULE.PurePosixPath(artifact.name),
            )
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_persist_quarantined_file",
                    side_effect=replace_before_durable_quarantine,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "changed before durable quarantine",
                ),
            ):
                MIRROR_MODULE._isolate_and_remove_file(
                    bound_target,
                    bound_target.fd,
                    artifact.name,
                    snapshot,
                    MIRROR_MODULE.PurePosixPath(artifact.name),
                )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_target)

        self.assertEqual(saved.read_bytes(), b"expected cleanup artifact\n")
        quarantine = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        self.assertIn(
            replacement_payload,
            [path.read_bytes() for path in quarantine.iterdir()],
        )

    def test_retired_target_quarantines_final_move_replacement(self) -> None:
        retired = self.target_root / "retired.txt"
        retired.write_bytes(b"expected retired target\n")
        retired.chmod(0o644)
        saved = self.target_root / "saved-retired-original"
        replacement_payload = b"retired replacement sentinel\n"
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            MIRROR_MODULE.PurePosixPath(retired.name),
        )
        real_persist = MIRROR_MODULE._persist_quarantined_file
        injected = False

        def replace_before_durable_quarantine(
            quarantine,
            source_parent_fd,
            source_name,
            expected_snapshot,
            display_path,
            **kwargs,
        ):
            nonlocal injected
            if not injected:
                injected = True
                os.rename(
                    source_name,
                    saved.name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=source_parent_fd,
                )
                replacement_fd = os.open(
                    source_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    expected_snapshot.mode,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(replacement_fd, replacement_payload)
                    os.fsync(replacement_fd)
                finally:
                    os.close(replacement_fd)
            return real_persist(
                quarantine,
                source_parent_fd,
                source_name,
                expected_snapshot,
                display_path,
                **kwargs,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_persist_quarantined_file",
                side_effect=replace_before_durable_quarantine,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "changed before durable quarantine",
            ),
        ):
            MIRROR_MODULE._atomic_remove_relative(
                self.target_root,
                MIRROR_MODULE.PurePosixPath(retired.name),
                expected,
            )

        self.assertEqual(saved.read_bytes(), b"expected retired target\n")
        self.assertTrue(
            (
                self.target_root
                / MIRROR_MODULE._exchange_journal_name(
                    MIRROR_MODULE.PurePosixPath(retired.name)
                )
            ).exists()
        )
        quarantine = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        self.assertIn(
            replacement_payload,
            [path.read_bytes() for path in quarantine.iterdir()],
        )

    def test_generate_recovers_after_a_retired_target_removal_crash(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        self._generate()
        self._commit(self.target_root, "track first generated mirror")
        old_target = self.target_root / "scripts" / "engine.py"
        new_target = self.target_root / "generated" / "engine.py"
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["mirrors"]["toolbox"]["files"]["engine"] = "generated/engine.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "rename generated target",
        )
        real_remove = MIRROR_MODULE._atomic_remove_relative
        crashed = False

        def crash_after_remove(root, relative_path, expected_snapshot):
            nonlocal crashed
            result = real_remove(root, relative_path, expected_snapshot)
            if not crashed:
                crashed = True
                raise SimulatedCrash()
            return result

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_remove_relative",
                side_effect=crash_after_remove,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()
        self.assertFalse(old_target.exists())
        self.assertTrue(new_target.exists())
        self.assertTrue(
            (self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()).exists()
        )

        self.assertEqual(self._generate(), 1)
        self.assertFalse(old_target.exists())
        self.assertFalse(
            (self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()).exists()
        )

    def test_generate_preserves_dirty_consumer_mirror_edits(self) -> None:
        self._generate()
        self._commit(self.target_root, "track generated mirror")
        target = self.target_root / "scripts" / "engine.py"
        target.write_bytes(b"user mirror edit\n")
        source_before = self.source_path.read_bytes()

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "dirty or untracked",
        ):
            self._generate()

        self.assertEqual(target.read_bytes(), b"user mirror edit\n")
        self.assertEqual(self.source_path.read_bytes(), source_before)

    def test_generate_preserves_a_dirty_existing_receipt(self) -> None:
        self._generate()
        self._commit(self.target_root, "track generated receipt")
        receipt_path = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()
        receipt_path.write_bytes(b'{"user":"receipt edit"}\n')
        target_before = (self.target_root / "scripts" / "engine.py").read_bytes()

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "dirty or untracked",
        ):
            self._generate()

        self.assertEqual(receipt_path.read_bytes(), b'{"user":"receipt edit"}\n')
        self.assertEqual(
            (self.target_root / "scripts" / "engine.py").read_bytes(),
            target_before,
        )

    def test_generate_rejects_index_only_drift_before_a_write(self) -> None:
        real_require_index = MIRROR_MODULE._require_same_target_index
        checks = 0

        def inject_index_drift(
            target_root,
            mirror,
            expected,
            additional_paths=frozenset(),
        ):
            nonlocal checks
            checks += 1
            if checks == 2:
                object_id = (
                    self._git_with_input(
                        self.target_root,
                        b"index-only drift\n",
                        "hash-object",
                        "-w",
                        "--stdin",
                    )
                    .decode("ascii")
                    .strip()
                )
                self._git(
                    self.target_root,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100755,{object_id},scripts/engine.py",
                )
            return real_require_index(
                target_root,
                mirror,
                expected,
                additional_paths,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_target_index",
                side_effect=inject_index_drift,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "stage-0 index changed|Git index control file was replaced",
            ),
        ):
            self._generate()

        self.assertFalse((self.target_root / "scripts" / "engine.py").exists())
        self.assertTrue(
            self._git(
                self.target_root,
                "ls-files",
                "--stage",
                "--",
                "scripts/engine.py",
            )
        )

    def test_generate_requires_the_target_transaction_lock(self) -> None:
        held_target = MIRROR_MODULE._bind_root(
            self.target_root,
            exclusive=True,
        )
        try:
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "transaction lock",
            ):
                self._generate()
        finally:
            MIRROR_MODULE._finish_bound_roots(held_target)

        self.assertFalse(
            (self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()).exists()
        )

    def test_generated_receipt_binds_commit_contract_mapping_and_tree(self) -> None:
        self._generate()

        receipt_path = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        source_lock = MIRROR_MODULE.load_source_lock(self.canonical_root)
        mirror = source_lock.mirrors["toolbox"]

        self.assertEqual(
            receipt,
            MIRROR_MODULE._receipt_document(
                source_lock,
                mirror,
                self.source_commit,
            ),
        )
        self.assertEqual(receipt["canonical_commit"], self.source_commit)
        self.assertEqual(
            receipt["generator_contract_version"],
            MIRROR_MODULE.GENERATOR_CONTRACT_VERSION,
        )
        self.assertEqual(
            receipt["rules_contract_version"],
            MIRROR_MODULE.RULES_CONTRACT_VERSION,
        )
        self.assertEqual(receipt["mirror"], "toolbox")
        self.assertEqual(
            receipt["mirror_repository"],
            "Joey-Tools/toolbox-fixture",
        )
        for field_name in (
            "mapping_digest",
            "file_set_digest",
            "tree_digest",
        ):
            self.assertRegex(receipt[field_name], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [path for path in self.target_root.rglob("*") if ".tmp-" in path.name],
            [],
        )

    def test_generate_invalidates_receipt_on_terminal_target_group_drift(
        self,
    ) -> None:
        real_require_group = MIRROR_MODULE._require_same_file_group
        target = self.target_root / "scripts" / "engine.py"

        def mutate_before_group_check(root, expected, group_name):
            if group_name == "generated consumer mirror group":
                target.write_bytes(b"late consumer drift\n")
            return real_require_group(root, expected, group_name)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_file_group",
                side_effect=mutate_before_group_check,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "generated consumer mirror group changed",
            ),
        ):
            self._generate()

        receipt = json.loads(
            (self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "generating")

    def test_generate_resumes_a_partial_crash_before_git_clean_checks(
        self,
    ) -> None:
        class SimulatedCrash(BaseException):
            pass

        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        crashed = False

        def crash_after_target_write(
            root,
            relative_path,
            payload,
            mode,
            **kwargs,
        ):
            nonlocal crashed
            result = real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )
            if (
                relative_path == MIRROR_MODULE.PurePosixPath("scripts/engine.py")
                and not crashed
            ):
                crashed = True
                raise SimulatedCrash()
            return result

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=crash_after_target_write,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()

        transaction_path = self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()
        self.assertTrue(transaction_path.exists())
        before_check = {
            path.relative_to(self.target_root): path.read_bytes()
            for path in self.target_root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "pending generation transaction",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        after_check = {
            path.relative_to(self.target_root): path.read_bytes()
            for path in self.target_root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        self.assertEqual(after_check, before_check)

        self.assertEqual(self._generate(), 1)
        self.assertFalse(transaction_path.exists())
        self.assertEqual(
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            ),
            1,
        )

    def test_generate_recovers_journal_commit_before_a_new_source_head(
        self,
    ) -> None:
        class SimulatedCrash(BaseException):
            pass

        prior_commit = self.source_commit
        prior_payload = self.source_path.read_bytes()
        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        crashed = False

        def crash_after_target_write(
            root,
            relative_path,
            payload,
            mode,
            **kwargs,
        ):
            nonlocal crashed
            result = real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )
            if (
                relative_path == MIRROR_MODULE.PurePosixPath("scripts/engine.py")
                and not crashed
            ):
                crashed = True
                raise SimulatedCrash()
            return result

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=crash_after_target_write,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()

        self.source_path.write_bytes(b"next canonical engine\n")
        self.source_path.chmod(0o755)
        MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=False)
        self.source_commit = self._commit(
            self.canonical_root,
            "advance canonical head",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "recovered the prior generation transaction.*did not generate",
        ):
            self._generate()

        self.assertFalse(
            (self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()).exists()
        )
        self.assertEqual(
            (self.target_root / "scripts" / "engine.py").read_bytes(),
            prior_payload,
        )
        receipt = json.loads(
            (self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["canonical_commit"], prior_commit)

    def test_missing_prior_lock_object_preserves_pending_journal(
        self,
    ) -> None:
        class SimulatedCrash(BaseException):
            pass

        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        crashed = False

        def crash_after_target_write(
            root,
            relative_path,
            payload,
            mode,
            **kwargs,
        ):
            nonlocal crashed
            result = real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )
            if (
                relative_path == MIRROR_MODULE.PurePosixPath("scripts/engine.py")
                and not crashed
            ):
                crashed = True
                raise SimulatedCrash()
            return result

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=crash_after_target_write,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()

        self.source_path.write_bytes(b"next canonical engine\n")
        self.source_path.chmod(0o755)
        MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=False)
        self.source_commit = self._commit(
            self.canonical_root,
            "advance canonical head",
        )
        transaction_path = self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()
        target_before = (self.target_root / "scripts" / "engine.py").read_bytes()

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_source_lock_at_commit",
                side_effect=MIRROR_MODULE.MirrorSyncError("missing prior lock object"),
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "missing prior lock object",
            ),
        ):
            self._generate()

        self.assertTrue(transaction_path.exists())
        self.assertEqual(
            (self.target_root / "scripts" / "engine.py").read_bytes(),
            target_before,
        )

    def test_generate_resumes_after_valid_receipt_before_journal_cleanup(
        self,
    ) -> None:
        class SimulatedCrash(BaseException):
            pass

        real_remove = MIRROR_MODULE._remove_transaction_journal
        crashed = False

        def crash_before_remove(target_root, expected):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise SimulatedCrash()
            return real_remove(target_root, expected)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_remove_transaction_journal",
                side_effect=crash_before_remove,
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self._generate()

        self.assertTrue(
            (self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()).exists()
        )
        self.assertEqual(self._generate(), 1)
        self.assertFalse(
            (self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()).exists()
        )

    def test_transaction_publication_and_completion_restart_recovery(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(
            self.target_root,
            exclusive=True,
        )
        try:
            partial_path = (
                self.target_root / MIRROR_MODULE.TRANSACTION_TEMP_PATH.as_posix()
            )
            partial_path.write_bytes(b'{"partial":')
            partial_path.chmod(0o600)
            self.assertIsNone(MIRROR_MODULE._load_transaction_journal(bound_root))
            self.assertTrue(partial_path.exists())
            inspection = MIRROR_MODULE._inspect_transaction_journal(bound_root)
            self.assertIsNone(
                MIRROR_MODULE._recover_transaction_journal_artifacts(
                    bound_root,
                    inspection,
                )
            )
            self.assertFalse(partial_path.exists())

            expected = MIRROR_MODULE._write_transaction_journal(
                bound_root,
                {"version": 1},
            )
            os.link(
                MIRROR_MODULE.TRANSACTION_PATH.as_posix(),
                MIRROR_MODULE.TRANSACTION_COMPLETE_PATH.as_posix(),
                src_dir_fd=bound_root.fd,
                dst_dir_fd=bound_root.fd,
                follow_symlinks=False,
            )
            os.unlink(
                MIRROR_MODULE.TRANSACTION_PATH.as_posix(),
                dir_fd=bound_root.fd,
            )
            inspection = MIRROR_MODULE._inspect_transaction_journal(bound_root)
            recovered = MIRROR_MODULE._recover_transaction_journal_artifacts(
                bound_root,
                inspection,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered[0], expected)
            self.assertFalse(
                (
                    self.target_root
                    / MIRROR_MODULE.TRANSACTION_COMPLETE_PATH.as_posix()
                ).exists()
            )
            MIRROR_MODULE._remove_transaction_journal(
                bound_root,
                recovered[0],
            )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_generate_performs_no_journal_recovery_write_before_target_proof(
        self,
    ) -> None:
        partial_path = self.target_root / MIRROR_MODULE.TRANSACTION_TEMP_PATH.as_posix()
        partial_payload = b'{"partial":'
        partial_path.write_bytes(partial_payload)
        partial_path.chmod(0o600)
        target_origin_error = MIRROR_MODULE.MirrorSyncError("wrong target origin")

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_verify_target_repository",
                side_effect=(None, target_origin_error),
            ) as verify_target,
            mock.patch.object(
                MIRROR_MODULE,
                "_recover_transaction_journal_artifacts",
                wraps=MIRROR_MODULE._recover_transaction_journal_artifacts,
            ) as recover,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "wrong target origin",
            ),
        ):
            self._generate()

        self.assertEqual(verify_target.call_count, 2)
        recover.assert_not_called()
        self.assertEqual(partial_path.read_bytes(), partial_payload)

    def test_generate_performs_no_journal_recovery_write_before_stage_zero_reproof(
        self,
    ) -> None:
        partial_path = self.target_root / MIRROR_MODULE.TRANSACTION_TEMP_PATH.as_posix()
        partial_payload = b'{"partial":'
        partial_path.write_bytes(partial_payload)
        partial_path.chmod(0o600)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_target_index",
                side_effect=MIRROR_MODULE.MirrorSyncError("stage-0 target drift"),
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_recover_transaction_journal_artifacts",
                wraps=MIRROR_MODULE._recover_transaction_journal_artifacts,
            ) as recover,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "stage-0 target drift",
            ),
        ):
            self._generate()

        recover.assert_not_called()
        self.assertEqual(partial_path.read_bytes(), partial_payload)

    def test_transaction_cleanup_preserves_a_racing_replacement(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(
            self.target_root,
            exclusive=True,
        )
        transaction_path = self.target_root / MIRROR_MODULE.TRANSACTION_PATH.as_posix()
        saved_path = self.target_root / ".transaction-original"
        real_rename = MIRROR_MODULE._rename_directory_entry_noreplace
        injected = False

        def replace_before_isolation(
            directory_fd,
            source_name,
            destination_name,
        ):
            nonlocal injected
            if (
                not injected
                and source_name == MIRROR_MODULE.TRANSACTION_PATH.as_posix()
            ):
                injected = True
                os.rename(transaction_path, saved_path)
                transaction_path.write_bytes(b"concurrent replacement\n")
                transaction_path.chmod(0o600)
            return real_rename(
                directory_fd,
                source_name,
                destination_name,
            )

        try:
            expected = MIRROR_MODULE._write_transaction_journal(
                bound_root,
                {"version": 1},
            )
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_rename_directory_entry_noreplace",
                    side_effect=replace_before_isolation,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "completion marker changed",
                ),
            ):
                MIRROR_MODULE._remove_transaction_journal(
                    bound_root,
                    expected,
                )
            self.assertEqual(
                transaction_path.read_bytes(),
                b"concurrent replacement\n",
            )
            self.assertEqual(saved_path.read_bytes(), expected.payload)
            self.assertFalse(
                (
                    self.target_root
                    / MIRROR_MODULE.TRANSACTION_COMPLETE_PATH.as_posix()
                ).exists()
            )
        finally:
            for path in (
                transaction_path,
                saved_path,
                self.target_root / MIRROR_MODULE.TRANSACTION_COMPLETE_PATH.as_posix(),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_generate_revalidates_target_group_before_valid_receipt(
        self,
    ) -> None:
        real_require_group = MIRROR_MODULE._require_same_file_group
        target = self.target_root / "scripts" / "engine.py"

        def mutate_before_pre_receipt(root, expected, group_name):
            if group_name == "pre-receipt generated target group":
                target.write_bytes(b"pre-receipt drift\n")
            return real_require_group(root, expected, group_name)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_file_group",
                side_effect=mutate_before_pre_receipt,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "pre-receipt generated target group changed",
            ),
        ):
            self._generate()

        receipt = json.loads(
            (self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "generating")

    def test_generate_refuses_a_mismatched_source_commit(self) -> None:
        old_commit = self.source_commit
        (self.canonical_root / "unrelated.txt").write_text(
            "new commit\n",
            encoding="utf-8",
        )
        self._commit(self.canonical_root, "move canonical HEAD")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "does not match canonical HEAD",
        ):
            MIRROR_MODULE.generate_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
                old_commit,
            )

    def test_generate_rejects_git_replace_refs(self) -> None:
        original_object = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "HEAD:scripts/engine.py",
            )
            .decode("ascii")
            .strip()
        )
        replacement_object = (
            self._git_with_input(
                self.canonical_root,
                b"replacement object\n",
                "hash-object",
                "-w",
                "--stdin",
            )
            .decode("ascii")
            .strip()
        )
        self._git(
            self.canonical_root,
            "replace",
            original_object,
            replacement_object,
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "replace refs",
        ):
            self._generate()

    def test_generate_rejects_git_config_includes_and_object_alternates(
        self,
    ) -> None:
        include_path = self.root / "included.gitconfig"
        include_path.write_text("[core]\n\tfsmonitor = true\n", encoding="utf-8")
        self._git(
            self.canonical_root,
            "config",
            "include.path",
            str(include_path),
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "include paths",
        ):
            self._generate()

        self._git(
            self.canonical_root,
            "config",
            "--unset-all",
            "include.path",
        )
        alternate_objects = self.root / "alternate-objects"
        alternate_objects.mkdir()
        alternates_path = (
            self.canonical_root / ".git" / "objects" / "info" / "alternates"
        )
        alternates_path.parent.mkdir(exist_ok=True)
        alternates_path.write_text(
            str(alternate_objects) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "object alternates",
        ):
            self._generate()

    def test_git_capability_probe_fails_before_private_materialization(
        self,
    ) -> None:
        cases = (
            (
                "unsupported option",
                (129, b"", b"unknown option: no-lazy-fetch\n"),
                "Git 2.45.0 or newer",
            ),
            (
                "old version",
                (0, b"git version 2.44.0\n", b""),
                "observed 2.44.0",
            ),
            (
                "malformed version",
                (0, b"git 2.45.0\n", b""),
                "malformed version output",
            ),
            (
                "multiple version lines",
                (0, b"git version 2.45.0\ngit version 2.45.0\n", b""),
                "malformed version output",
            ),
        )
        for name, result, expected_error in cases:
            with self.subTest(name=name):
                bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE.subprocess,
                            "Popen",
                            return_value=mock.Mock(),
                        ) as popen,
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_collect_bounded_process_output",
                            return_value=result,
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_materialize_private_git_control",
                        ) as materialize,
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            expected_error,
                        ),
                    ):
                        MIRROR_MODULE._ensure_git_control_binding(bound_root)
                    materialize.assert_not_called()
                    self.assertFalse(bound_root.git_capability_verified)
                    self.assertIsNone(bound_root.git_control)
                    command = popen.call_args.args[0]
                    self.assertIn("--no-lazy-fetch", command)
                    self.assertEqual(command[-1], "--version")
                finally:
                    MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_macos_git_locator_uses_bound_deterministic_tmpdir(self) -> None:
        (
            locator,
            temporary_directory,
            developer_git,
            expected_access_policy,
        ) = self._macos_git_locator_fixture()
        captured: dict[str, object] = {}
        temporary_binding = None
        real_bind_temporary = MIRROR_MODULE._bind_macos_git_locator_temp_directory

        def capture_temporary_binding(path, access_policy):
            nonlocal temporary_binding
            temporary_binding = real_bind_temporary(path, access_policy)
            return temporary_binding

        def capture_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return mock.Mock()

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_EXECUTABLE",
                locator,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                temporary_directory,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                expected_access_policy,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_bind_macos_git_locator_temp_directory",
                side_effect=capture_temporary_binding,
            ),
            mock.patch.object(
                MIRROR_MODULE.subprocess,
                "Popen",
                side_effect=capture_popen,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_collect_bounded_process_output",
                return_value=(0, f"{developer_git}\n".encode(), b""),
            ),
            mock.patch.dict(os.environ, {"TMPDIR": "/untrusted/ambient"}),
        ):
            resolved = MIRROR_MODULE._resolve_macos_git_executable()

        self.assertEqual(resolved, developer_git)
        self.assertEqual(
            captured["command"],
            [locator.as_posix(), "--find", "git"],
        )
        environment = captured["env"]
        self.assertIsInstance(environment, dict)
        self.assertEqual(environment["TMPDIR"], temporary_directory.as_posix())
        self.assertNotEqual(environment["TMPDIR"], "/untrusted/ambient")
        self.assertIsNotNone(temporary_binding)
        with self.assertRaises(OSError):
            os.fstat(temporary_binding.fd)

    def test_macos_git_locator_keeps_stderr_fail_closed(self) -> None:
        (
            locator,
            temporary_directory,
            developer_git,
            expected_access_policy,
        ) = self._macos_git_locator_fixture()
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_EXECUTABLE",
                locator,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                temporary_directory,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                expected_access_policy,
            ),
            mock.patch.object(
                MIRROR_MODULE.subprocess,
                "Popen",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_collect_bounded_process_output",
                return_value=(
                    0,
                    f"{developer_git}\n".encode(),
                    b"xcrun: warning: simulated warning\n",
                ),
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "returned unexpected stderr",
            ),
        ):
            MIRROR_MODULE._resolve_macos_git_executable()

    def test_macos_git_locator_rejects_invalid_results(self) -> None:
        (
            locator,
            temporary_directory,
            developer_git,
            expected_access_policy,
        ) = self._macos_git_locator_fixture()
        cases = (
            (
                "nonzero",
                (1, b"", b"xcrun failed\n"),
                "macOS Git locator failed: xcrun failed",
            ),
            (
                "ambiguous stdout",
                (0, f"{developer_git}\n{developer_git}\n".encode(), b""),
                "returned a malformed path",
            ),
            (
                "relative stdout",
                (0, b"relative/git\n", b""),
                "returned a non-canonical path",
            ),
            (
                "system shim",
                (0, b"/usr/bin/git\n", b""),
                "returned the non-snapshot-capable system shim",
            ),
        )
        for name, result, expected_error in cases:
            with (
                self.subTest(name=name),
                mock.patch.object(
                    MIRROR_MODULE,
                    "MACOS_GIT_LOCATOR_EXECUTABLE",
                    locator,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                    temporary_directory,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                    expected_access_policy,
                ),
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    return_value=mock.Mock(),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_process_output",
                    return_value=result,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    expected_error,
                ),
            ):
                MIRROR_MODULE._resolve_macos_git_executable()

    def test_macos_git_locator_rejects_temp_symlink_and_initial_policy(
        self,
    ) -> None:
        locator, temporary_directory, _developer_git, _policy = (
            self._macos_git_locator_fixture()
        )
        real_temporary_directory = self.root / "real-locator-temporary-directory"
        real_temporary_directory.mkdir(mode=0o700)
        temporary_directory.rmdir()
        temporary_directory.symlink_to(real_temporary_directory)
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_EXECUTABLE",
                locator,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                temporary_directory,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                (0o700, os.geteuid(), os.getegid()),
            ),
            mock.patch.object(MIRROR_MODULE.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "temporary directory must not be a symlink",
            ),
        ):
            MIRROR_MODULE._resolve_macos_git_executable()
        popen.assert_not_called()

        temporary_directory.unlink()
        temporary_directory.mkdir(mode=0o755)
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_EXECUTABLE",
                locator,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                temporary_directory,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                (0o700, os.geteuid(), os.getegid()),
            ),
            mock.patch.object(MIRROR_MODULE.subprocess, "Popen") as popen,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "must have access policy",
            ),
        ):
            MIRROR_MODULE._resolve_macos_git_executable()
        popen.assert_not_called()

    def test_macos_git_locator_rejects_temp_replacement_and_policy_drift(
        self,
    ) -> None:
        for mutation in ("replacement", "policy"):
            with self.subTest(mutation=mutation):
                (
                    locator,
                    temporary_directory,
                    developer_git,
                    expected_access_policy,
                ) = self._macos_git_locator_fixture(f"-{mutation}")
                moved = temporary_directory.with_name(
                    temporary_directory.name + f"-{mutation}-preserved"
                )

                def mutate_temporary_directory(_command, **_kwargs):
                    if mutation == "replacement":
                        temporary_directory.rename(moved)
                        temporary_directory.mkdir(mode=0o700)
                    else:
                        temporary_directory.chmod(0o755)
                    return mock.Mock()

                expected_error = (
                    "temporary directory was replaced"
                    if mutation == "replacement"
                    else "temporary directory access policy changed"
                )
                with (
                    mock.patch.object(
                        MIRROR_MODULE,
                        "MACOS_GIT_LOCATOR_EXECUTABLE",
                        locator,
                    ),
                    mock.patch.object(
                        MIRROR_MODULE,
                        "MACOS_GIT_LOCATOR_TEMP_DIRECTORY",
                        temporary_directory,
                    ),
                    mock.patch.object(
                        MIRROR_MODULE,
                        "MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY",
                        expected_access_policy,
                    ),
                    mock.patch.object(
                        MIRROR_MODULE.subprocess,
                        "Popen",
                        side_effect=mutate_temporary_directory,
                    ),
                    mock.patch.object(
                        MIRROR_MODULE,
                        "_collect_bounded_process_output",
                        return_value=(0, f"{developer_git}\n".encode(), b""),
                    ),
                    self.assertRaisesRegex(
                        MIRROR_MODULE.MirrorSyncError,
                        expected_error,
                    ),
                ):
                    MIRROR_MODULE._resolve_macos_git_executable()

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS xcrun")
    def test_macos_git_locator_cli_help_with_sanitized_environment(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPOSITORY_ROOT / "scripts" / "sync_canonical_mirrors.py"),
                "--help",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={
                "HOME": "/",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        self.assertIn(b"refresh-lock", completed.stdout)

    def test_git_capability_probe_bounds_output_and_runtime_before_snapshot(
        self,
    ) -> None:
        for error_text in (
            "bounded Git capability probe stdout exceeds the 1024-byte limit",
            "bounded Git capability probe exceeded 30 seconds",
        ):
            with self.subTest(error=error_text):
                bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE.subprocess,
                            "Popen",
                            return_value=mock.Mock(),
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_collect_bounded_process_output",
                            side_effect=MIRROR_MODULE.MirrorSyncError(error_text),
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_materialize_private_git_control",
                        ) as materialize,
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            re.escape(error_text),
                        ),
                    ):
                        MIRROR_MODULE._ensure_git_control_binding(bound_root)
                    materialize.assert_not_called()
                    self.assertFalse(bound_root.git_capability_verified)
                    self.assertIsNone(bound_root.git_control)
                finally:
                    MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_static_git_profile_rejects_partial_clone_config_before_object_git(
        self,
    ) -> None:
        cases = (
            ("extensions.partialClone", "origin"),
            ("remote.origin.promisor", "true"),
            ("remote.origin.promisor", "false"),
            ("remote.origin.promisor", ""),
            ("remote.origin.partialCloneFilter", "blob:none"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self._git(self.canonical_root, "config", key, value)
                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_run_git_process",
                        ) as run_git_process,
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            "partial/promisor Git config state",
                        ),
                    ):
                        self._generate()
                    run_git_process.assert_not_called()
                finally:
                    self._git(
                        self.canonical_root,
                        "config",
                        "--unset-all",
                        key,
                    )

    def test_static_git_profile_rejects_fsck_and_url_rewrite_config(
        self,
    ) -> None:
        cases = (
            ("fsck.missingObject", "ignore", "fsck configuration"),
            (
                "fsck.skipList",
                (self.root / "external-skip-list").as_posix(),
                "fsck configuration",
            ),
            (
                "url.https://attacker.invalid/.insteadOf",
                "https://github.com/Joey-Tools/",
                "URL rewriting",
            ),
            (
                "url.ssh://helper@attacker.invalid/.pushInsteadOf",
                "https://github.com/Joey-Tools/",
                "URL rewriting",
            ),
        )
        for key, value, expected in cases:
            with self.subTest(key=key):
                self._git(self.canonical_root, "config", key, value)
                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_run_git_process",
                        ) as run_git_process,
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            expected,
                        ),
                    ):
                        self._generate()
                    run_git_process.assert_not_called()
                finally:
                    self._git(
                        self.canonical_root,
                        "config",
                        "--unset-all",
                        key,
                    )

        self._git(
            self.canonical_root,
            "config",
            "extensions.worktreeConfig",
            "true",
        )
        worktree_key = "URL.https://attacker.invalid/.pushInsteadOf"
        self._git(
            self.canonical_root,
            "config",
            "--worktree",
            worktree_key,
            "https://github.com/Joey-Tools/",
        )
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_run_git_process",
                ) as run_git_process,
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "URL rewriting",
                ),
            ):
                self._generate()
            run_git_process.assert_not_called()
        finally:
            self._git(
                self.canonical_root,
                "config",
                "--worktree",
                "--unset-all",
                worktree_key,
            )
            self._git(
                self.canonical_root,
                "config",
                "--unset-all",
                "extensions.worktreeConfig",
            )

    def test_static_git_profile_rejects_promisor_and_alternate_markers(
        self,
    ) -> None:
        cases = (
            Path("pack") / "fixture.PrOmIsOr",
            Path("info") / "alternates",
            Path("info") / "http-alternates",
            Path("InFo") / "AlTeRnAtEs",
            Path("INFO") / "HTTP-AlTeRnAtEs",
        )
        for relative_path in cases:
            with self.subTest(path=relative_path.as_posix()):
                marker = self.canonical_root / ".git" / "objects" / relative_path
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("fixture\n", encoding="utf-8")
                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_run_private_git_config_process",
                        ) as inspect_config,
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_run_git_process",
                        ) as run_git_process,
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            "partial/promisor|object alternates",
                        ),
                    ):
                        self._generate()
                    inspect_config.assert_not_called()
                    run_git_process.assert_not_called()
                finally:
                    marker.unlink()

    def test_static_git_profile_does_not_invoke_configured_helpers(self) -> None:
        marker = self.root / "credential-helper-invoked"
        helper = self.root / "credential-helper"
        helper.write_text(
            f"#!/bin/sh\nprintf invoked > {marker}\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        self._git(
            self.canonical_root,
            "config",
            "credential.helper",
            helper.as_posix(),
        )
        self._git(
            self.canonical_root,
            "config",
            "remote.origin.promisor",
            "false",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "partial/promisor Git config state",
        ):
            self._generate()

        self.assertFalse(marker.exists())

    def test_static_git_profile_failure_cleans_snapshot_and_can_retry(
        self,
    ) -> None:
        self._git(
            self.canonical_root,
            "config",
            "remote.origin.promisor",
            "false",
        )
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_cleanup_private_git_control",
                    wraps=MIRROR_MODULE._cleanup_private_git_control,
                ) as cleanup,
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "partial/promisor Git config state",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            cleanup.assert_called_once()
            self.assertTrue(bound_root.git_capability_verified)
            self.assertIsNone(bound_root.git_control)

            self._git(
                self.canonical_root,
                "config",
                "--unset-all",
                "remote.origin.promisor",
            )
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(bound_root.git_control.static_profile_verified)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_run_git_rejects_promisor_state_before_lazy_fetch(self) -> None:
        donor = self.root / "donor"
        donor.mkdir()
        self._init_git_repository(donor, "Joey-Tools/donor-fixture")
        promised_path = donor / "promised.txt"
        promised_path.write_bytes(b"promised-only blob\n")
        self._commit(donor, "promised object")
        promised_object = (
            self._git(
                donor,
                "rev-parse",
                "HEAD:promised.txt",
            )
            .decode("ascii")
            .strip()
        )
        self._git(
            self.canonical_root,
            "remote",
            "set-url",
            "origin",
            str(donor),
        )
        self._git(
            self.canonical_root,
            "config",
            "remote.origin.promisor",
            "true",
        )
        self._git(
            self.canonical_root,
            "config",
            "core.repositoryFormatVersion",
            "1",
        )
        self._git(
            self.canonical_root,
            "config",
            "extensions.partialClone",
            "origin",
        )
        local_object = (
            self.canonical_root
            / ".git"
            / "objects"
            / promised_object[:2]
            / promised_object[2:]
        )
        self.assertFalse(local_object.exists())

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_run_git_process",
            ) as run_git_process,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "partial/promisor Git config state",
            ),
        ):
            MIRROR_MODULE._run_git(
                self.canonical_root,
                "cat-file",
                "blob",
                promised_object,
            )

        run_git_process.assert_not_called()
        self.assertFalse(local_object.exists())

    def test_run_git_sanitizes_ambient_git_environment(self) -> None:
        captured: dict[str, object] = {}

        def capture_popen(command, **kwargs):
            captured["command"] = command
            captured["launch_cwd_identity"] = MIRROR_MODULE._object_identity(
                os.stat(".", follow_symlinks=False)
            )
            captured.update(kwargs)
            return mock.Mock()

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            private_control_identity = bound_root.git_control.private.identity
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "GIT_DIR": "/tmp/ambient-git-dir",
                        "GIT_WORK_TREE": "/tmp/ambient-work-tree",
                        "GIT_OBJECT_DIRECTORY": "/tmp/ambient-objects",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/ambient-alternates",
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "core.fsmonitor",
                        "GIT_CONFIG_VALUE_0": "true",
                        "LD_PRELOAD": "/tmp/ambient-preload",
                        "LD_LIBRARY_PATH": "/tmp/ambient-library",
                        "DYLD_INSERT_LIBRARIES": "/tmp/ambient-dyld",
                        "BASH_ENV": "/tmp/ambient-bash-env",
                        "ENV": "/tmp/ambient-env",
                        "PYTHONPATH": "/tmp/ambient-python",
                        "PATH": "/tmp/ambient-path",
                    },
                ),
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_popen,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    return_value=(0, b"", b""),
                ),
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        environment = captured["env"]
        self.assertIsInstance(environment, dict)
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(environment["GIT_ASKPASS"], "/usr/bin/false")
        for name in (
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "BASH_ENV",
            "ENV",
            "PYTHONPATH",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(
            captured["command"][0],
            MIRROR_MODULE.GIT_EXECUTABLE.as_posix(),
        )
        self.assertIn("--no-lazy-fetch", captured["command"])
        self.assertIn("--git-dir=.", captured["command"])
        self.assertIn("core.commitGraph=false", captured["command"])
        self.assertIn("core.multiPackIndex=false", captured["command"])
        self.assertEqual(
            captured["launch_cwd_identity"],
            private_control_identity,
        )
        self.assertNotIn("cwd", captured)
        self.assertNotIn("preexec_fn", captured)
        self.assertNotIn("pass_fds", captured)

    def test_private_config_git_disables_derived_object_caches(self) -> None:
        captured: dict[str, object] = {}

        def capture_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return mock.Mock()

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            with (
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_popen,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    return_value=(0, b"", b""),
                ),
            ):
                MIRROR_MODULE._run_private_git_config_process(
                    bound_root,
                    "config",
                )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        command = captured["command"]
        self.assertIsInstance(command, list)
        self.assertIn("core.commitGraph=false", command)
        self.assertIn("core.multiPackIndex=false", command)
        self.assertLess(
            command.index("core.commitGraph=false"),
            command.index("config"),
        )
        self.assertLess(
            command.index("core.multiPackIndex=false"),
            command.index("config"),
        )

    def test_private_git_ignores_forged_commit_graph_ancestry(self) -> None:
        object_format = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "--show-object-format",
            )
            .decode("ascii")
            .strip()
        )
        if object_format != "sha1":
            self.skipTest("forged cache fixture currently covers SHA-1 repositories")
        _root_commit, first_commit, head_commit, side_commit = (
            self._build_forgeable_history()
        )
        self._git(
            self.canonical_root,
            "commit-graph",
            "write",
            "--reachable",
            "--no-changed-paths",
        )
        graph_path = self.canonical_root / ".git" / "objects" / "info" / "commit-graph"
        graph_payload = bytearray(graph_path.read_bytes())
        chunks = self._git_cache_chunks(
            graph_payload,
            magic=b"CGPH",
            header_size=8,
        )
        head_index = self._cache_oid_index(
            graph_payload,
            chunks,
            head_commit,
        )
        side_index = self._cache_oid_index(
            graph_payload,
            chunks,
            side_commit,
        )
        commit_data_start, commit_data_end = chunks[b"CDAT"]
        object_count = (commit_data_end - commit_data_start) // 36
        self.assertEqual(commit_data_end - commit_data_start, object_count * 36)
        parent_offset = commit_data_start + head_index * 36 + 20
        graph_payload[parent_offset : parent_offset + 4] = side_index.to_bytes(
            4,
            "big",
        )
        graph_payload[-20:] = hashlib.sha1(
            graph_payload[:-20],
            usedforsecurity=False,
        ).digest()
        graph_path.chmod(0o600)
        graph_path.write_bytes(graph_payload)

        forged = self._git(
            self.canonical_root,
            "-c",
            "core.commitGraph=true",
            "-c",
            "core.multiPackIndex=false",
            "rev-list",
            "--parents",
            "--max-count=1",
            head_commit,
        )
        uncached = self._git(
            self.canonical_root,
            "-c",
            "core.commitGraph=false",
            "-c",
            "core.multiPackIndex=false",
            "rev-list",
            "--parents",
            "--max-count=1",
            head_commit,
        )
        self.assertEqual(
            forged,
            f"{head_commit} {side_commit}\n".encode("ascii"),
        )
        self.assertEqual(
            uncached,
            f"{head_commit} {first_commit}\n".encode("ascii"),
        )

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None
            private_graph = (
                bound_root.git_control.private_path
                / "objects"
                / "info"
                / "commit-graph"
            )
            self.assertEqual(private_graph.read_bytes(), graph_payload)
            observed = MIRROR_MODULE._run_git(
                bound_root,
                "rev-list",
                "--parents",
                "--max-count=1",
                head_commit,
            )
            self.assertEqual(observed, uncached)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_ignores_forged_midx_object_offsets(self) -> None:
        object_format = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "--show-object-format",
            )
            .decode("ascii")
            .strip()
        )
        if object_format != "sha1":
            self.skipTest("forged cache fixture currently covers SHA-1 repositories")
        root_commit, first_commit, head_commit, side_commit = (
            self._build_forgeable_history()
        )
        self._git(
            self.canonical_root,
            "-c",
            "gc.writeCommitGraph=false",
            "repack",
            "-a",
            "-d",
            "-f",
            "--window=0",
            "--no-write-bitmap-index",
        )
        self._git(
            self.canonical_root,
            "multi-pack-index",
            "write",
        )
        midx_path = (
            self.canonical_root / ".git" / "objects" / "pack" / "multi-pack-index"
        )
        midx_payload = bytearray(midx_path.read_bytes())
        chunks = self._git_cache_chunks(
            midx_payload,
            magic=b"MIDX",
            header_size=12,
        )
        head_index = self._cache_oid_index(
            midx_payload,
            chunks,
            head_commit,
        )
        side_index = self._cache_oid_index(
            midx_payload,
            chunks,
            side_commit,
        )
        offsets_start, offsets_end = chunks[b"OOFF"]
        object_count = (offsets_end - offsets_start) // 8
        self.assertEqual(offsets_end - offsets_start, object_count * 8)
        head_offset = offsets_start + head_index * 8
        side_offset = offsets_start + side_index * 8
        midx_payload[head_offset : head_offset + 8] = midx_payload[
            side_offset : side_offset + 8
        ]
        midx_payload[-20:] = hashlib.sha1(
            midx_payload[:-20],
            usedforsecurity=False,
        ).digest()
        midx_path.chmod(0o600)
        midx_path.write_bytes(midx_payload)

        forged = self._git(
            self.canonical_root,
            "-c",
            "core.commitGraph=false",
            "-c",
            "core.multiPackIndex=true",
            "rev-list",
            "--parents",
            "--max-count=1",
            head_commit,
        )
        uncached = self._git(
            self.canonical_root,
            "-c",
            "core.commitGraph=false",
            "-c",
            "core.multiPackIndex=false",
            "rev-list",
            "--parents",
            "--max-count=1",
            head_commit,
        )
        self.assertEqual(
            forged,
            f"{head_commit} {root_commit}\n".encode("ascii"),
        )
        self.assertEqual(
            uncached,
            f"{head_commit} {first_commit}\n".encode("ascii"),
        )

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None
            private_midx = (
                bound_root.git_control.private_path
                / "objects"
                / "pack"
                / "multi-pack-index"
            )
            self.assertEqual(private_midx.read_bytes(), midx_payload)
            observed = MIRROR_MODULE._run_git(
                bound_root,
                "rev-list",
                "--parents",
                "--max-count=1",
                head_commit,
            )
            self.assertEqual(observed, uncached)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_full_fsck_rejects_forged_pack_index_offsets(
        self,
    ) -> None:
        object_format = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "--show-object-format",
            )
            .decode("ascii")
            .strip()
        )
        if object_format != "sha1":
            self.skipTest("forged pack-index fixture currently covers SHA-1")
        root_commit, first_commit, head_commit, side_commit = (
            self._build_forgeable_history()
        )
        self._git(
            self.canonical_root,
            "-c",
            "gc.writeCommitGraph=false",
            "repack",
            "-a",
            "-d",
            "-f",
            "--window=0",
            "--no-write-bitmap-index",
        )
        pack_directory = self.canonical_root / ".git" / "objects" / "pack"
        index_paths = sorted(pack_directory.glob("*.idx"))
        self.assertEqual(len(index_paths), 1)
        index_path = index_paths[0]
        index_payload = bytearray(index_path.read_bytes())
        object_ids, crc_start, offsets_start = self._pack_index_v2_layout(index_payload)
        head_index = object_ids.index(bytes.fromhex(head_commit))
        side_index = object_ids.index(bytes.fromhex(side_commit))
        head_crc = crc_start + head_index * 4
        side_crc = crc_start + side_index * 4
        head_offset = offsets_start + head_index * 4
        side_offset = offsets_start + side_index * 4
        self.assertEqual(
            int.from_bytes(index_payload[head_offset : head_offset + 4], "big") >> 31,
            0,
        )
        self.assertEqual(
            int.from_bytes(index_payload[side_offset : side_offset + 4], "big") >> 31,
            0,
        )
        index_payload[head_crc : head_crc + 4] = index_payload[side_crc : side_crc + 4]
        index_payload[head_offset : head_offset + 4] = index_payload[
            side_offset : side_offset + 4
        ]
        index_payload[-20:] = hashlib.sha1(
            index_payload[:-20],
            usedforsecurity=False,
        ).digest()
        index_path.chmod(0o600)
        index_path.write_bytes(index_payload)

        forged = self._git(
            self.canonical_root,
            "-c",
            "core.commitGraph=false",
            "-c",
            "core.multiPackIndex=false",
            "rev-list",
            "--parents",
            "--max-count=1",
            head_commit,
        )
        self.assertEqual(
            forged,
            f"{head_commit} {root_commit}\n".encode("ascii"),
        )
        self.assertNotEqual(
            forged,
            f"{head_commit} {first_commit}\n".encode("ascii"),
        )

        commands: list[list[str]] = []
        real_popen = subprocess.Popen

        def capture_popen(command, **kwargs):
            commands.append(command)
            return real_popen(command, **kwargs)

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_popen,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private Git full object integrity check failed",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertIsNone(bound_root.git_control)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        private_git_commands = [
            command
            for command in commands
            if MIRROR_MODULE.GIT_EXECUTABLE.as_posix() in command
            and "--git-dir=." in command
        ]
        fsck_commands = [
            command for command in private_git_commands if "fsck" in command
        ]
        self.assertEqual(len(fsck_commands), 1)
        fsck_command = fsck_commands[0]
        self.assertEqual(
            fsck_command[0],
            MIRROR_MODULE.GIT_EXECUTABLE.as_posix(),
        )
        self.assertIn("--full", fsck_command)
        self.assertIn("--strict", fsck_command)
        self.assertIn("--no-dangling", fsck_command)
        self.assertIn("--no-progress", fsck_command)
        self.assertIn("--no-reflogs", fsck_command)
        for command in private_git_commands:
            self.assertNotIn("cat-file", command)
            self.assertNotIn("rev-list", command)

    def test_run_git_uses_bound_root_during_swap_and_restore(self) -> None:
        real_popen = subprocess.Popen
        moved_root = self.root / "canonical-during-git"

        def swap_popen(command, **kwargs):
            os.rename(self.canonical_root, moved_root)
            self.canonical_root.mkdir()
            try:
                return real_popen(command, **kwargs)
            finally:
                os.rmdir(self.canonical_root)
                os.rename(moved_root, self.canonical_root)

        with mock.patch.object(
            MIRROR_MODULE.subprocess,
            "Popen",
            side_effect=swap_popen,
        ):
            observed_head = (
                MIRROR_MODULE._run_git(
                    self.canonical_root,
                    "rev-parse",
                    "HEAD",
                )
                .decode("ascii")
                .strip()
            )

        self.assertEqual(observed_head, self.source_commit)

    def test_module_import_accepts_symlinked_python_argv(self) -> None:
        launcher = self.root / "python-symlink"
        launcher.symlink_to(Path(sys.executable))
        program = (
            "import runpy,sys;"
            "runpy.run_path(sys.argv[1]);"
            "print('loaded sync generator')"
        )
        completed = subprocess.run(
            [
                launcher.as_posix(),
                "-I",
                "-B",
                "-S",
                "-c",
                program,
                (REPOSITORY_ROOT / MIRROR_MODULE.GENERATOR_PATH.as_posix()).as_posix(),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(completed.stdout, "loaded sync generator\n")

    def test_bound_directory_launch_uses_descriptor_across_path_replacement(
        self,
    ) -> None:
        launch_directory = self.root / "launch-directory"
        launch_directory.mkdir(mode=0o700)
        preserved_directory = self.root / "launch-preserved"
        binding = MIRROR_MODULE._bind_absolute_control_object(
            launch_directory,
            "test launch directory",
            require_directory=True,
        )
        real_popen = subprocess.Popen
        captured: dict[str, object] = {}
        parent_identity = MIRROR_MODULE._object_identity(
            os.stat(".", follow_symlinks=False)
        )

        def replace_path_at_popen(command, **kwargs):
            launch_directory.rename(preserved_directory)
            launch_directory.mkdir(mode=0o700)
            captured.update(kwargs)
            return real_popen(command, **kwargs)

        try:
            with mock.patch.object(
                MIRROR_MODULE.subprocess,
                "Popen",
                side_effect=replace_path_at_popen,
            ):
                process = MIRROR_MODULE._popen_from_bound_directory(
                    ["/bin/pwd"],
                    owner=MIRROR_MODULE._ProcessOwner(),
                    directory_fd=binding.fd,
                    directory_identity=binding.identity,
                    directory_access_policy=binding.access_policy,
                    directory_label=binding.label,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={"PATH": "/usr/bin:/bin"},
                    start_new_session=True,
                )
                stdout, stderr = process.communicate(timeout=30)

            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(
                Path(stdout.decode().strip()).resolve(),
                preserved_directory.resolve(),
            )
            self.assertEqual(
                MIRROR_MODULE._object_identity(os.stat(".", follow_symlinks=False)),
                parent_identity,
            )
            self.assertNotIn("cwd", captured)
            self.assertNotIn("preexec_fn", captured)
        finally:
            os.close(binding.fd)

    def test_bound_directory_launch_reaps_child_when_saved_fd_close_fails(
        self,
    ) -> None:
        launch_directory = self.root / "close-failure-launch-directory"
        launch_directory.mkdir(mode=0o700)
        binding = MIRROR_MODULE._bind_absolute_control_object(
            launch_directory,
            "test launch directory",
            require_directory=True,
        )
        real_open = MIRROR_MODULE.os.open
        real_close = MIRROR_MODULE.os.close
        real_popen = subprocess.Popen
        saved_directory_fd = -1
        process: subprocess.Popen[bytes] | None = None
        parent_identity = MIRROR_MODULE._object_identity(
            os.stat(".", follow_symlinks=False)
        )

        def capture_saved_directory(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal saved_directory_fd
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "." and dir_fd is None:
                saved_directory_fd = descriptor
            return descriptor

        def fail_saved_directory_close(file_descriptor: int) -> None:
            if file_descriptor == saved_directory_fd:
                time.sleep(0.05)
                real_close(file_descriptor)
                raise OSError("simulated saved-directory close failure")
            real_close(file_descriptor)

        def capture_process(command, **kwargs):
            nonlocal process
            process = real_popen(command, **kwargs)
            return process

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "open",
                    side_effect=capture_saved_directory,
                ),
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "close",
                    side_effect=fail_saved_directory_close,
                ),
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_process,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "saved parent-directory descriptor.*simulated",
                ),
            ):
                MIRROR_MODULE._popen_from_bound_directory(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,time;"
                            "os.write(1,b'x'*8192);"
                            "os.write(2,b'y'*8192);"
                            "time.sleep(30)"
                        ),
                    ],
                    owner=MIRROR_MODULE._ProcessOwner(),
                    directory_fd=binding.fd,
                    directory_identity=binding.identity,
                    directory_access_policy=binding.access_policy,
                    directory_label=binding.label,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

            self.assertIsNotNone(process)
            assert process is not None
            self.assertIsNotNone(process.poll())
            assert process.stdout is not None
            assert process.stderr is not None
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertEqual(
                MIRROR_MODULE._object_identity(os.stat(".", follow_symlinks=False)),
                parent_identity,
            )
            with self.assertRaises(OSError):
                os.fstat(saved_directory_fd)
        finally:
            os.close(binding.fd)

    def test_bound_directory_launch_cleans_unhanded_process_group(
        self,
    ) -> None:
        launch_directory = self.root / "post-launch-revalidation-directory"
        launch_directory.mkdir(mode=0o700)
        directory_binding = MIRROR_MODULE._bind_absolute_control_object(
            launch_directory,
            "post-launch revalidation directory",
            require_directory=True,
        )
        executable_binding = MIRROR_MODULE._bind_absolute_control_object(
            Path(os.path.realpath(sys.executable)),
            "post-launch executable",
            require_directory=False,
        )
        control_root = MIRROR_MODULE._bind_root(self.canonical_root)
        real_revalidate = MIRROR_MODULE._revalidate_control_object
        real_popen = subprocess.Popen
        validation_count = 0
        launched: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()

        def fail_post_launch_revalidation(root, binding):
            nonlocal validation_count
            if binding is executable_binding:
                validation_count += 1
                if validation_count == 2:
                    raise MIRROR_MODULE.MirrorSyncError(
                        "simulated post-launch executable drift"
                    )
            return real_revalidate(root, binding)

        def capture_process(command, **kwargs):
            nonlocal launched, liveness_write
            kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), liveness_write)
            launched = real_popen(command, **kwargs)
            os.close(liveness_write)
            liveness_write = -1
            return launched

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_revalidate_control_object",
                    side_effect=fail_post_launch_revalidation,
                ),
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_process,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "simulated post-launch executable drift",
                ),
            ):
                MIRROR_MODULE._popen_from_bound_directory(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,time;"
                            "child=os.fork();"
                            "time.sleep(5) if child == 0 else "
                            f"(os.close({liveness_write}),time.sleep(5))"
                        ),
                    ],
                    owner=MIRROR_MODULE._ProcessOwner(),
                    directory_fd=directory_binding.fd,
                    directory_identity=directory_binding.identity,
                    directory_access_policy=directory_binding.access_policy,
                    directory_label=directory_binding.label,
                    control_root=control_root,
                    executable_binding=executable_binding,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

            self.assertIsNotNone(launched)
            assert launched is not None
            self._assert_process_liveness_pipe_closed(liveness_read)
            self.assertIsNotNone(launched.poll())
            assert launched.stdout is not None
            assert launched.stderr is not None
            self.assertTrue(launched.stdout.closed)
            self.assertTrue(launched.stderr.closed)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if launched is not None:
                self._finish_process_group_fixture(launched)
            os.close(liveness_read)
            os.close(executable_binding.fd)
            os.close(directory_binding.fd)
            MIRROR_MODULE._finish_bound_roots(control_root)

    def test_bound_directory_launch_cleanup_shares_one_deadline(self) -> None:
        launch_directory = self.root / "shared-cleanup-deadline-directory"
        launch_directory.mkdir(mode=0o700)
        directory_binding = MIRROR_MODULE._bind_absolute_control_object(
            launch_directory,
            "shared cleanup deadline directory",
            require_directory=True,
        )
        executable_binding = MIRROR_MODULE._bind_absolute_control_object(
            Path(os.path.realpath(sys.executable)),
            "shared cleanup deadline executable",
            require_directory=False,
        )
        control_root = MIRROR_MODULE._bind_root(self.canonical_root)
        launched = mock.Mock()
        validation_count = 0

        def fail_post_launch_revalidation(root, binding):
            nonlocal validation_count
            if binding is executable_binding:
                validation_count += 1
                if validation_count == 2:
                    raise MIRROR_MODULE.MirrorSyncError(
                        "simulated shared-deadline post-launch drift"
                    )

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_revalidate_control_object",
                    side_effect=fail_post_launch_revalidation,
                ),
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    return_value=launched,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_terminate_git_process",
                ) as terminate,
                mock.patch.object(
                    MIRROR_MODULE,
                    "_drain_and_close_git_process_output",
                ) as drain,
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "simulated shared-deadline post-launch drift",
                ),
            ):
                MIRROR_MODULE._popen_from_bound_directory(
                    [sys.executable, "-c", "pass"],
                    owner=MIRROR_MODULE._ProcessOwner(),
                    directory_fd=directory_binding.fd,
                    directory_identity=directory_binding.identity,
                    directory_access_policy=directory_binding.access_policy,
                    directory_label=directory_binding.label,
                    control_root=control_root,
                    executable_binding=executable_binding,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

            terminate.assert_called_once_with(
                launched,
                deadline=terminate.call_args.kwargs["deadline"],
                trusted_exec_profile=None,
            )
            drain.assert_called_once_with(
                launched,
                deadline=terminate.call_args.kwargs["deadline"],
            )
        finally:
            os.close(executable_binding.fd)
            os.close(directory_binding.fd)
            MIRROR_MODULE._finish_bound_roots(control_root)

    def test_bound_directory_launch_rejects_access_policy_change(self) -> None:
        launch_directory = self.root / "launch-directory"
        launch_directory.mkdir(mode=0o700)
        binding = MIRROR_MODULE._bind_absolute_control_object(
            launch_directory,
            "test launch directory",
            require_directory=True,
        )
        launch_directory.chmod(0o755)
        try:
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "descriptor access policy changed",
            ):
                MIRROR_MODULE._popen_from_bound_directory(
                    [MIRROR_MODULE.GIT_EXECUTABLE.as_posix(), "--version"],
                    owner=MIRROR_MODULE._ProcessOwner(),
                    directory_fd=binding.fd,
                    directory_identity=binding.identity,
                    directory_access_policy=binding.access_policy,
                    directory_label=binding.label,
                )
        finally:
            os.close(binding.fd)

    def test_run_git_uses_private_snapshot_without_python_launcher(self) -> None:
        captured: dict[str, object] = {}

        def capture_popen(command, **kwargs):
            captured["command"] = command
            captured["launch_cwd_identity"] = MIRROR_MODULE._object_identity(
                os.stat(".", follow_symlinks=False)
            )
            captured.update(kwargs)
            return mock.Mock()

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            private_identity = bound_root.git_control.private.identity
            with (
                mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=capture_popen,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    return_value=(0, b"", b""),
                ),
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            self.assertEqual(
                captured["command"][0],
                MIRROR_MODULE.GIT_EXECUTABLE.as_posix(),
            )
            self.assertEqual(
                captured["executable"],
                bound_root.git_control.private_git_executable.path.as_posix(),
            )
            self.assertEqual(captured["launch_cwd_identity"], private_identity)
            self.assertNotIn("preexec_fn", captured)
            self.assertNotIn("cwd", captured)
            self.assertNotIn("pass_fds", captured)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_snapshot_survives_source_replace_restore_at_popen(
        self,
    ) -> None:
        source_git_parent = self.root / "git-source"
        source_git_parent.mkdir()
        source_git = source_git_parent / "git"
        shutil.copyfile(MIRROR_MODULE.GIT_EXECUTABLE, source_git)
        source_git.chmod(0o755)
        preserved_git = source_git_parent / "git-preserved"
        replacement_git = self.root / "git-replacement"
        shutil.copyfile("/usr/bin/false", replacement_git)
        replacement_git.chmod(0o755)
        real_popen = subprocess.Popen
        captured: dict[str, object] = {}

        with mock.patch.object(MIRROR_MODULE, "GIT_EXECUTABLE", source_git):
            bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
            try:
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
                snapshot_path = (
                    bound_root.git_control.private_git_executable.path.as_posix()
                )

                def replace_restore_source(command, **kwargs):
                    source_git.rename(preserved_git)
                    shutil.copyfile(replacement_git, source_git)
                    source_git.chmod(0o755)
                    captured["executable"] = kwargs.get("executable")
                    try:
                        return real_popen(command, **kwargs)
                    finally:
                        source_git.unlink()
                        preserved_git.rename(source_git)

                with mock.patch.object(
                    MIRROR_MODULE.subprocess,
                    "Popen",
                    side_effect=replace_restore_source,
                ):
                    observed = (
                        MIRROR_MODULE._run_git_process(
                            bound_root,
                            "rev-parse",
                            "HEAD",
                        )
                        .decode("ascii")
                        .strip()
                    )
                self.assertEqual(observed, self.source_commit)
                self.assertEqual(captured["executable"], snapshot_path)
            finally:
                MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_snapshot_access_policy_drift_fails_before_popen(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            snapshot = bound_root.git_control.private_git_executable
            assert snapshot.path is not None
            snapshot.path.chmod(0o400)
            try:
                with (
                    mock.patch.object(
                        MIRROR_MODULE.subprocess,
                        "Popen",
                    ) as popen,
                    self.assertRaisesRegex(
                        MIRROR_MODULE.MirrorSyncError,
                        "private Git executable snapshot access policy changed",
                    ),
                ):
                    MIRROR_MODULE._run_git_process(
                        bound_root,
                        "rev-parse",
                        "HEAD",
                    )
                popen.assert_not_called()
            finally:
                snapshot.path.chmod(0o500)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_snapshot_rejects_source_mutation_during_copy(
        self,
    ) -> None:
        source_git = self.root / "git-source"
        shutil.copyfile(MIRROR_MODULE.GIT_EXECUTABLE, source_git)
        source_git.chmod(0o755)
        private_path = self.root / "private-executable-snapshot"
        private_path.mkdir(mode=0o700)
        private = MIRROR_MODULE._bind_absolute_control_object(
            private_path,
            "test private executable snapshot",
            require_directory=True,
        )
        with mock.patch.object(MIRROR_MODULE, "GIT_EXECUTABLE", source_git):
            bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        real_bound_control_payload = MIRROR_MODULE._bound_control_payload

        def mutate_source_after_read(binding):
            payload = real_bound_control_payload(binding)
            if binding is bound_root.git_executable:
                mutated = bytearray(payload)
                mutated[0] ^= 0x01
                source_git.write_bytes(mutated)
                source_git.chmod(0o755)
            return payload

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bound_control_payload",
                    side_effect=mutate_source_after_read,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "Git executable content changed",
                ),
            ):
                MIRROR_MODULE._snapshot_bound_git_executable(
                    bound_root,
                    private,
                )
        finally:
            os.close(private.fd)
            MIRROR_MODULE._close_bound_root(bound_root)

    def test_private_git_snapshot_runs_real_version_and_rev_parse(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            snapshot = bound_root.git_control.private_git_executable
            self.assertNotEqual(snapshot.path, bound_root.git_executable.path)
            self.assertEqual(
                snapshot.content_digest,
                bound_root.git_executable.content_digest,
            )
            self.assertEqual(snapshot.access_policy[0], 0o500)
            version = MIRROR_MODULE._run_private_git_process(
                bound_root,
                "--version",
            )
            self.assertIsNotNone(MIRROR_MODULE.GIT_VERSION_RE.fullmatch(version))
            observed = (
                MIRROR_MODULE._run_git_process(
                    bound_root,
                    "rev-parse",
                    "HEAD",
                )
                .decode("ascii")
                .strip()
            )
            self.assertEqual(observed, self.source_commit)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_git_output_collector_enforces_limits_and_reaps(self) -> None:
        output_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,time;os.write(1,b'123456789');time.sleep(30)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MAX_GIT_STDOUT_BYTES",
                8,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "stdout exceeds",
            ),
        ):
            MIRROR_MODULE._collect_bounded_git_output(
                output_process,
                owner=MIRROR_MODULE._ProcessOwner(process=output_process),
            )
        self.assertIsNotNone(output_process.poll())

        timeout_process = subprocess.Popen(
            ["/bin/sleep", "1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "GIT_TIMEOUT_SECONDS",
                0.01,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "exceeded",
            ),
        ):
            MIRROR_MODULE._collect_bounded_git_output(
                timeout_process,
                owner=MIRROR_MODULE._ProcessOwner(process=timeout_process),
            )
        self.assertIsNotNone(timeout_process.poll())

    def test_process_liveness_pipe_requires_child_exit_after_parent_close(
        self,
    ) -> None:
        process: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            with self.assertRaisesRegex(
                AssertionError,
                "still retains the liveness writer",
            ):
                self._assert_process_liveness_pipe_closed(
                    liveness_read,
                    timeout=0.01,
                )
            self._finish_process_group_fixture(process)
            self._assert_process_liveness_pipe_closed(liveness_read)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if process is not None:
                self._finish_process_group_fixture(process)
            os.close(liveness_read)

    def test_git_output_collector_terminates_leader_first_exit_groups(self) -> None:
        programs = (
            (
                "inherited-pipe-timeout",
                (
                    "import os,time;"
                    "child=os.fork();"
                    "time.sleep(5) if child == 0 else "
                    "(os.close({liveness_fd}),os._exit(0))"
                ),
                "exceeded",
                0.05,
                True,
            ),
            (
                "closed-pipe-residual-group",
                (
                    "import os,time;"
                    "child=os.fork();"
                    "(os.close(1),os.close(2),time.sleep(5)) "
                    "if child == 0 else "
                    "(os.close({liveness_fd}),os._exit(0))"
                ),
                None,
                2.0,
                False,
            ),
        )
        for name, program_template, expected, timeout_seconds, should_fail in programs:
            with self.subTest(name=name):
                process: subprocess.Popen[bytes] | None = None
                liveness_read, liveness_write = self._open_process_liveness_pipe()
                try:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            program_template.format(liveness_fd=liveness_write),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                        pass_fds=(liveness_write,),
                    )
                    os.close(liveness_write)
                    liveness_write = -1
                    if should_fail:
                        assert expected is not None
                        with self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            expected,
                        ):
                            MIRROR_MODULE._collect_bounded_process_output(
                                process,
                                None,
                                stdout_limit=1024,
                                stderr_limit=1024,
                                timeout_seconds=timeout_seconds,
                                label="leader-first-exit fixture",
                            )
                    else:
                        result = MIRROR_MODULE._collect_bounded_process_output(
                            process,
                            None,
                            stdout_limit=1024,
                            stderr_limit=1024,
                            timeout_seconds=timeout_seconds,
                            label="leader-first-exit fixture",
                        )
                        self.assertEqual(result, (0, b"", b""))
                    self._assert_process_liveness_pipe_closed(liveness_read)
                    self.assertIsNotNone(process.poll())
                    assert process.stdout is not None
                    assert process.stderr is not None
                    self.assertTrue(process.stdout.closed)
                    self.assertTrue(process.stderr.closed)
                finally:
                    if liveness_write >= 0:
                        os.close(liveness_write)
                    if process is not None:
                        self._finish_process_group_fixture(process)
                    os.close(liveness_read)

    @unittest.skipUnless(
        sys.platform == "darwin" and not hasattr(os, "waitid"),
        "requires the Darwin Python without waitid(WNOWAIT)",
    )
    def test_git_output_collector_uses_darwin_kqueue_without_waitid(self) -> None:
        process: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,time;"
                        "child=os.fork();"
                        "(os.close(1),os.close(2),time.sleep(5)) "
                        "if child == 0 else "
                        f"(os.close({liveness_write}),os._exit(0))"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            result = MIRROR_MODULE._collect_bounded_process_output(
                process,
                None,
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=2,
                label="Darwin kqueue fixture",
            )
            self.assertEqual(result, (0, b"", b""))
            self._assert_process_liveness_pipe_closed(liveness_read)
            self.assertIsNotNone(process.poll())
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if process is not None:
                self._finish_process_group_fixture(process)
            os.close(liveness_read)

    def test_bounded_process_defers_keyboard_interrupt_until_collection(
        self,
    ) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("requires pthread signal-mask support")
        original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        if signal.SIGINT in original_mask:
            self.skipTest("SIGINT is already blocked by the test runner")
        launched: subprocess.Popen[bytes] | None = None
        lifecycle: list[str] = []
        liveness_read, liveness_write = self._open_process_liveness_pipe()

        def launch_then_interrupt(
            owner: object,
        ) -> subprocess.Popen[bytes]:
            nonlocal launched, liveness_write
            launched = MIRROR_MODULE._owned_popen(
                owner,
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,time;child=os.fork();"
                        "(os.close(1),os.close(2),time.sleep(5)) "
                        "if child == 0 else "
                        f"(os.close({liveness_write}),time.sleep(0.05))"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            lifecycle.append("launched")
            signal.raise_signal(signal.SIGINT)
            lifecycle.append("interrupt-pending")
            return launched

        def collect(
            process: subprocess.Popen[bytes],
            owner: object,
        ) -> tuple[int, bytes, bytes]:
            lifecycle.append("collecting")
            result = MIRROR_MODULE._collect_bounded_process_output(
                process,
                None,
                owner=owner,
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=2,
                label="deferred KeyboardInterrupt fixture",
            )
            lifecycle.append("collected")
            return result

        try:
            with self.assertRaises(KeyboardInterrupt):
                MIRROR_MODULE._launch_and_collect_bounded_process(
                    launch_then_interrupt,
                    collect,
                    label="deferred KeyboardInterrupt fixture",
                )
            self.assertEqual(
                lifecycle,
                ["launched", "interrupt-pending", "collecting", "collected"],
            )
            self.assertIsNotNone(launched)
            assert launched is not None
            self._assert_process_liveness_pipe_closed(liveness_read)
            self.assertIsNotNone(launched.poll())
            assert launched.stdout is not None
            assert launched.stderr is not None
            self.assertTrue(launched.stdout.closed)
            self.assertTrue(launched.stderr.closed)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if launched is not None:
                self._finish_process_group_fixture(launched)
            os.close(liveness_read)

    def test_bounded_process_child_inherits_no_deferred_signal_mask(self) -> None:
        child_mask: tuple[int, bytes, bytes] | None = None

        def launch(owner: object) -> subprocess.Popen[bytes]:
            return MIRROR_MODULE._owned_popen(
                owner,
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,signal,time;"
                        "blocked=signal.pthread_sigmask(signal.SIG_BLOCK,set());"
                        "selected=sorted(int(item) for item in blocked "
                        "if int(item) in (1,2,15));"
                        "print(','.join(str(item) for item in selected),flush=True);"
                        "child=os.fork();"
                        "(os.close(1),os.close(2),time.sleep(5)) "
                        "if child == 0 else os._exit(0)"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

        def collect(
            process: subprocess.Popen[bytes],
            owner: object,
        ) -> tuple[int, bytes, bytes]:
            return MIRROR_MODULE._collect_bounded_process_output(
                process,
                None,
                owner=owner,
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=2,
                label="child signal-mask fixture",
            )

        child_mask = MIRROR_MODULE._launch_and_collect_bounded_process(
            launch,
            collect,
            label="child signal-mask fixture",
        )
        self.assertEqual(child_mask, (0, b"\n", b""))

    def test_owned_popen_cleans_post_init_base_exception(self) -> None:
        class InjectedAfterInit(BaseException):
            pass

        real_init = MIRROR_MODULE._REAL_SUBPROCESS_POPEN.__init__
        launched: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()

        def initialize_then_raise(process, *args, **kwargs):
            nonlocal launched, liveness_write
            real_init(process, *args, **kwargs)
            launched = process
            os.close(liveness_write)
            liveness_write = -1
            raise InjectedAfterInit("injected after Popen.__init__")

        def launch(owner: object) -> subprocess.Popen[bytes]:
            return MIRROR_MODULE._owned_popen(
                owner,
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE._REAL_SUBPROCESS_POPEN,
                    "__init__",
                    new=initialize_then_raise,
                ),
                self.assertRaisesRegex(
                    InjectedAfterInit,
                    "injected after Popen.__init__",
                ),
            ):
                MIRROR_MODULE._launch_and_collect_bounded_process(
                    launch,
                    mock.Mock(),
                    label="post-init BaseException fixture",
                )
            self.assertIsNotNone(launched)
            assert launched is not None
            self._assert_process_liveness_pipe_closed(liveness_read)
            self.assertIsNotNone(launched.returncode)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if launched is not None:
                self._finish_process_group_fixture(launched)
            os.close(liveness_read)

    def test_owned_popen_cleans_launcher_post_spawn_base_exception(self) -> None:
        class InjectedAfterSpawn(BaseException):
            pass

        launched: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()

        def launch(owner: object) -> subprocess.Popen[bytes]:
            nonlocal launched, liveness_write
            launched = MIRROR_MODULE._owned_popen(
                owner,
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            raise InjectedAfterSpawn("injected after owned spawn")

        try:
            with self.assertRaisesRegex(
                InjectedAfterSpawn,
                "injected after owned spawn",
            ):
                MIRROR_MODULE._launch_and_collect_bounded_process(
                    launch,
                    mock.Mock(),
                    label="post-spawn BaseException fixture",
                )
            self.assertIsNotNone(launched)
            assert launched is not None
            self._assert_process_liveness_pipe_closed(liveness_read)
            self.assertIsNotNone(launched.returncode)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if launched is not None:
                self._finish_process_group_fixture(launched)
            os.close(liveness_read)

    def test_git_output_collector_cleans_group_when_selector_setup_fails(
        self,
    ) -> None:
        process: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,time;"
                        "child=os.fork();"
                        "time.sleep(5) if child == 0 else "
                        f"(os.close({liveness_write}),os._exit(0))"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            time.sleep(0.05)
            with (
                mock.patch.object(
                    MIRROR_MODULE.selectors,
                    "DefaultSelector",
                    side_effect=OSError("simulated selector setup failure"),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "cannot supervise.*simulated selector setup failure",
                ) as raised,
            ):
                MIRROR_MODULE._collect_bounded_process_output(
                    process,
                    None,
                    stdout_limit=1024,
                    stderr_limit=1024,
                    timeout_seconds=1,
                    label="selector setup fixture",
                )
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self._assert_process_liveness_pipe_closed(liveness_read)
            assert process.stdout is not None
            assert process.stderr is not None
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if process is not None:
                self._finish_process_group_fixture(process)
            os.close(liveness_read)

    def test_git_output_collector_closes_partial_selector_setup(self) -> None:
        class FailSecondRegister:
            def __init__(self) -> None:
                self.inner = selectors.DefaultSelector()
                self.register_count = 0
                self.closed = False

            def register(self, *args, **kwargs):
                self.register_count += 1
                if self.register_count == 2:
                    raise ValueError("simulated second register failure")
                return self.inner.register(*args, **kwargs)

            def get_map(self):
                return self.inner.get_map()

            def select(self, *args, **kwargs):
                return self.inner.select(*args, **kwargs)

            def unregister(self, *args, **kwargs):
                return self.inner.unregister(*args, **kwargs)

            def close(self) -> None:
                self.closed = True
                self.inner.close()

        process: subprocess.Popen[bytes] | None = None
        liveness_read, liveness_write = self._open_process_liveness_pipe()
        selector = FailSecondRegister()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os,time;"
                        "child=os.fork();"
                        "time.sleep(5) if child == 0 else "
                        f"(os.close({liveness_write}),os._exit(0))"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(liveness_write,),
            )
            os.close(liveness_write)
            liveness_write = -1
            time.sleep(0.05)
            with (
                mock.patch.object(
                    MIRROR_MODULE.selectors,
                    "DefaultSelector",
                    return_value=selector,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "cannot supervise.*simulated second register failure",
                ),
            ):
                MIRROR_MODULE._collect_bounded_process_output(
                    process,
                    None,
                    stdout_limit=1024,
                    stderr_limit=1024,
                    timeout_seconds=1,
                    label="partial selector fixture",
                )
            self.assertTrue(selector.closed)
            self._assert_process_liveness_pipe_closed(liveness_read)
            assert process.stdout is not None
            assert process.stderr is not None
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
        finally:
            if liveness_write >= 0:
                os.close(liveness_write)
            if process is not None:
                self._finish_process_group_fixture(process)
            os.close(liveness_read)

    def test_git_cleanup_never_signals_an_already_reaped_group(self) -> None:
        process = mock.Mock()
        process.returncode = 0
        process.pid = 424242
        with (
            mock.patch.object(MIRROR_MODULE.os, "killpg") as kill_group,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "already reaped",
            ),
        ):
            MIRROR_MODULE._terminate_git_process(process)
        kill_group.assert_not_called()

    def test_git_cleanup_never_touches_group_after_wait(self) -> None:
        process = mock.Mock()
        process.returncode = None
        process.pid = 424242
        lifecycle: list[str] = []

        def wait_once(*, timeout):
            self.assertGreaterEqual(timeout, 0)
            lifecycle.append("wait")
            process.returncode = -signal.SIGKILL
            return process.returncode

        process.wait.side_effect = wait_once

        def signal_group(process_group, signum):
            self.assertEqual(process_group, process.pid)
            self.assertEqual(signum, signal.SIGKILL)
            lifecycle.append("killpg")

        with mock.patch.object(
            MIRROR_MODULE.os,
            "killpg",
            side_effect=signal_group,
        ) as kill_group:
            return_code = MIRROR_MODULE._terminate_git_process(process)

        self.assertEqual(return_code, -signal.SIGKILL)
        self.assertEqual(lifecycle, ["killpg", "wait"])
        kill_group.assert_called_once()
        process.wait.assert_called_once()
        process.poll.assert_not_called()

    def test_git_cleanup_uses_narrow_darwin_eperm_contract(self) -> None:
        def make_process():
            process = mock.Mock()
            process.returncode = None
            process.pid = 424242

            def wait_once(*, timeout):
                self.assertGreaterEqual(timeout, 0)
                process.returncode = 0
                return 0

            process.wait.side_effect = wait_once
            return process

        rejected_profiles = (
            (
                "linux",
                MIRROR_MODULE._TrustedProcessProfile.PRIVATE_GIT_SNAPSHOT,
            ),
            ("darwin", None),
        )
        for platform, trusted_profile in rejected_profiles:
            with self.subTest(
                platform=platform,
                trusted_profile=trusted_profile,
            ):
                process = make_process()
                with (
                    mock.patch.object(MIRROR_MODULE.sys, "platform", platform),
                    mock.patch.object(
                        MIRROR_MODULE.os,
                        "killpg",
                        side_effect=PermissionError("simulated EPERM"),
                    ),
                    self.assertRaisesRegex(
                        MIRROR_MODULE.MirrorSyncError,
                        "cannot signal.*EPERM",
                    ),
                ):
                    MIRROR_MODULE._terminate_git_process(
                        process,
                        leader_exit_observed=True,
                        trusted_exec_profile=trusted_profile,
                    )
                process.wait.assert_called_once()
                process.poll.assert_not_called()

        accepted = make_process()
        with (
            mock.patch.object(MIRROR_MODULE.sys, "platform", "darwin"),
            mock.patch.object(
                MIRROR_MODULE.os,
                "killpg",
                side_effect=PermissionError("simulated EPERM"),
            ),
        ):
            return_code = MIRROR_MODULE._terminate_git_process(
                accepted,
                leader_exit_observed=True,
                trusted_exec_profile=(
                    MIRROR_MODULE._TrustedProcessProfile.PRIVATE_GIT_SNAPSHOT
                ),
            )
        self.assertEqual(return_code, 0)
        accepted.wait.assert_called_once()
        accepted.poll.assert_not_called()

    def test_bounded_process_cleanup_uses_one_owner_deadline(self) -> None:
        process = mock.Mock()
        process.returncode = None
        process.pid = 424242
        process.stdout = mock.Mock()
        process.stderr = mock.Mock()

        def launch(owner):
            owner.process = process
            return process

        def collect(child, owner):
            return MIRROR_MODULE._collect_bounded_process_output(
                child,
                None,
                owner=owner,
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=30,
                label="single owner deadline fixture",
            )

        with (
            mock.patch.object(
                MIRROR_MODULE.time,
                "monotonic",
                return_value=100.0,
            ),
            mock.patch.object(
                MIRROR_MODULE.selectors,
                "DefaultSelector",
                side_effect=OSError("simulated selector failure"),
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_terminate_git_process",
                side_effect=MIRROR_MODULE.MirrorSyncError(
                    "simulated terminalization failure"
                ),
            ) as terminate,
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "simulated selector failure.*simulated terminalization failure",
            ),
        ):
            MIRROR_MODULE._launch_and_collect_bounded_process(
                launch,
                collect,
                label="single owner deadline fixture",
            )

        terminate.assert_called_once_with(
            process,
            deadline=100.0 + MIRROR_MODULE.GIT_CLEANUP_TIMEOUT_SECONDS,
            trusted_exec_profile=None,
        )

    def test_bounded_process_primary_survives_restore_failure(self) -> None:
        class PrimaryFailure(BaseException):
            pass

        primary = PrimaryFailure("primary launcher failure")
        real_restore = MIRROR_MODULE._restore_deferred_process_signal_handlers

        def launch(_owner):
            signal.raise_signal(signal.SIGINT)
            raise primary

        def restore_with_failure(state):
            errors = real_restore(state)
            errors.append(OSError("simulated handler restore failure"))
            return errors

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_restore_deferred_process_signal_handlers",
                side_effect=restore_with_failure,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "primary launcher failure.*handler restore failure",
            ) as raised,
        ):
            MIRROR_MODULE._launch_and_collect_bounded_process(
                launch,
                mock.Mock(),
                label="primary and restore failure fixture",
            )
        self.assertIs(raised.exception.__cause__, primary)

    def test_restore_mask_fence_failure_still_restores_handlers(self) -> None:
        state = MIRROR_MODULE._install_deferred_process_signal_handlers()
        with mock.patch.object(
            MIRROR_MODULE.signal,
            "pthread_sigmask",
            side_effect=OSError("simulated restore mask fence failure"),
        ):
            errors = MIRROR_MODULE._restore_deferred_process_signal_handlers(state)
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "restore mask fence failure")
        for signum, original_handler in state.original_handlers.items():
            self.assertEqual(signal.getsignal(signum), original_handler)

    def test_restore_unmask_failure_retries_exact_parent_mask(self) -> None:
        state = MIRROR_MODULE._install_deferred_process_signal_handlers()
        real_sigmask = signal.pthread_sigmask
        original_mask = real_sigmask(signal.SIG_BLOCK, set())
        call_count = 0

        def fail_first_unmask(how, mask):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("simulated restore unmask failure")
            return real_sigmask(how, mask)

        with mock.patch.object(
            MIRROR_MODULE.signal,
            "pthread_sigmask",
            side_effect=fail_first_unmask,
        ):
            errors = MIRROR_MODULE._restore_deferred_process_signal_handlers(state)

        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "restore unmask failure")
        self.assertEqual(
            real_sigmask(signal.SIG_BLOCK, set()),
            original_mask,
        )
        for signum, original_handler in state.original_handlers.items():
            self.assertEqual(signal.getsignal(signum), original_handler)

    def test_install_unmask_failure_rolls_back_handlers_and_mask(self) -> None:
        original_handlers = {
            signum: signal.getsignal(signum)
            for signum in MIRROR_MODULE._BOUNDED_PROCESS_SIGNALS
        }
        real_sigmask = signal.pthread_sigmask
        original_mask = real_sigmask(signal.SIG_BLOCK, set())
        call_count = 0

        def fail_first_unmask(how, mask):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("simulated install unmask failure")
            return real_sigmask(how, mask)

        with (
            mock.patch.object(
                MIRROR_MODULE.signal,
                "pthread_sigmask",
                side_effect=fail_first_unmask,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "install unmask failure",
            ),
        ):
            MIRROR_MODULE._install_deferred_process_signal_handlers()

        self.assertEqual(
            real_sigmask(signal.SIG_BLOCK, set()),
            original_mask,
        )
        for signum, original_handler in original_handlers.items():
            self.assertEqual(signal.getsignal(signum), original_handler)

    def test_trusted_process_profiles_reject_credential_transitions(self) -> None:
        transitions = {
            "user": 1234,
            "group": 1234,
            "extra_groups": [1234],
            "preexec_fn": lambda: None,
        }
        for name, value in transitions.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    f"forbids Popen {name} transitions",
                ),
            ):
                MIRROR_MODULE._verify_trusted_same_uid_spawn_kwargs(
                    {
                        "start_new_session": True,
                        name: value,
                    }
                )

    def test_darwin_kqueue_observer_validates_lifecycle(self) -> None:
        process = mock.Mock()
        process.returncode = None
        process.pid = 424242
        kq_filter_proc = -5
        kq_note_exit = 0x80000000
        kq_ev_add = 0x0001
        kq_ev_enable = 0x0004
        kq_ev_oneshot = 0x0010
        kq_ev_error = 0x4000
        valid_event = mock.Mock()
        valid_event.ident = process.pid
        valid_event.filter = kq_filter_proc
        valid_event.fflags = kq_note_exit
        valid_event.flags = 0

        def run_with_queue(queue):
            with (
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "waitid",
                    None,
                    create=True,
                ),
                mock.patch.object(MIRROR_MODULE.sys, "platform", "darwin"),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "kqueue",
                    return_value=queue,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "kevent",
                    return_value=mock.Mock(),
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_FILTER_PROC",
                    kq_filter_proc,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_NOTE_EXIT",
                    kq_note_exit,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_EV_ADD",
                    kq_ev_add,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_EV_ENABLE",
                    kq_ev_enable,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_EV_ONESHOT",
                    kq_ev_oneshot,
                    create=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE.select,
                    "KQ_EV_ERROR",
                    kq_ev_error,
                    create=True,
                ),
            ):
                MIRROR_MODULE._wait_for_git_leader_exit_without_reaping(
                    process,
                    time.monotonic() + 1,
                    "mock kqueue fixture",
                )

        late_queue = mock.Mock()
        late_queue.control.side_effect = ProcessLookupError("late ESRCH")
        run_with_queue(late_queue)
        late_queue.close.assert_called_once()

        valid_queue = mock.Mock()
        valid_queue.control.side_effect = [[], [valid_event]]
        run_with_queue(valid_queue)
        valid_queue.close.assert_called_once()

        invalid_event = mock.Mock()
        invalid_event.ident = process.pid + 1
        invalid_event.filter = kq_filter_proc
        invalid_event.fflags = kq_note_exit
        invalid_event.flags = 0
        invalid_queue = mock.Mock()
        invalid_queue.control.side_effect = [[], [invalid_event]]
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "invalid Darwin exit event",
        ):
            run_with_queue(invalid_queue)
        invalid_queue.close.assert_called_once()

        registration_queue = mock.Mock()
        registration_queue.control.side_effect = OSError(
            "simulated kqueue registration failure"
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "registration failure",
        ) as raised:
            run_with_queue(registration_queue)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        registration_queue.close.assert_called_once()

        failing_queue = mock.Mock()
        failing_queue.control.side_effect = [
            [],
            OSError("simulated kqueue control failure"),
        ]
        failing_queue.close.side_effect = OSError("simulated kqueue close failure")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "control failure.*close failure",
        ) as raised:
            run_with_queue(failing_queue)
        self.assertIsInstance(raised.exception.__cause__, OSError)

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "waitid",
                None,
                create=True,
            ),
            mock.patch.object(MIRROR_MODULE.sys, "platform", "darwin"),
            mock.patch.object(
                MIRROR_MODULE.select,
                "kqueue",
                side_effect=OSError("simulated kqueue creation failure"),
                create=True,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "create.*kqueue creation failure",
            ) as raised,
        ):
            MIRROR_MODULE._wait_for_git_leader_exit_without_reaping(
                process,
                time.monotonic() + 1,
                "mock kqueue fixture",
            )
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_source_and_operation_budgets_are_shared_and_bounded(self) -> None:
        self.assertEqual(
            MIRROR_MODULE.MAX_SOURCE_BYTES,
            32 * 1024 * 1024,
        )
        self.assertEqual(
            MIRROR_MODULE.MAX_GIT_STDOUT_BYTES,
            MIRROR_MODULE.MAX_SOURCE_BYTES,
        )

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        bound_root.operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() - 1,
            remaining_bytes=MIRROR_MODULE.MAX_OPERATION_BYTES,
            remaining_entries=MIRROR_MODULE.MAX_OPERATION_ENTRIES,
        )
        try:
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "mirror operation exceeded",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            bound_root.operation = None
        finally:
            bound_root.operation = None
            MIRROR_MODULE._finish_bound_roots(bound_root)

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        bound_root.operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() + 30,
            remaining_bytes=0,
            remaining_entries=MIRROR_MODULE.MAX_OPERATION_ENTRIES,
        )
        try:
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "byte aggregate budget",
            ):
                MIRROR_MODULE._safe_read_snapshot(
                    bound_root,
                    MIRROR_MODULE.PurePosixPath("scripts/engine.py"),
                )
            bound_root.operation = None
        finally:
            bound_root.operation = None
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_generate_and_check_bound_a_realistic_packed_object_store_once(
        self,
    ) -> None:
        packed_payload = self.canonical_root / "packed-fixture.bin"
        with packed_payload.open("wb") as stream:
            for _chunk in range(24):
                stream.write(os.urandom(1024 * 1024))
        self.source_commit = self._commit(
            self.canonical_root,
            "add realistic packed fixture",
        )
        self._git(
            self.canonical_root,
            "gc",
            "--prune=now",
        )
        pack_sizes = [
            path.stat().st_size
            for path in (self.canonical_root / ".git" / "objects" / "pack").glob(
                "*.pack"
            )
        ]
        self.assertTrue(pack_sizes)
        self.assertGreater(max(pack_sizes), 20 * 1024 * 1024)

        operation_limit = 384 * 1024 * 1024
        operations: list[MIRROR_MODULE.OperationBudget] = []

        def new_operation_budget() -> MIRROR_MODULE.OperationBudget:
            operation = MIRROR_MODULE.OperationBudget(
                deadline=time.monotonic() + MIRROR_MODULE.OPERATION_TIMEOUT_SECONDS,
                remaining_bytes=operation_limit,
                remaining_entries=MIRROR_MODULE.MAX_OPERATION_ENTRIES,
            )
            operations.append(operation)
            return operation

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MAX_OPERATION_BYTES",
                operation_limit,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_new_operation_budget",
                side_effect=new_operation_budget,
            ),
        ):
            self.assertEqual(self._generate(), 1)
            self.assertEqual(
                MIRROR_MODULE.check_mirror(
                    self.canonical_root,
                    self.target_root,
                    "toolbox",
                ),
                1,
            )

        self.assertEqual(len(operations), 2)
        for operation in operations:
            consumed_bytes = operation_limit - operation.remaining_bytes
            self.assertGreater(consumed_bytes, max(pack_sizes))
            self.assertLess(consumed_bytes, 256 * 1024 * 1024)

    def test_bound_git_control_rejects_marker_replacement(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        moved_marker = self.canonical_root / ".git-bound-original"
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            os.rename(self.canonical_root / ".git", moved_marker)
            (self.canonical_root / ".git").mkdir()
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "Git marker was replaced",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            os.rmdir(self.canonical_root / ".git")
            os.rename(moved_marker, self.canonical_root / ".git")
        finally:
            if moved_marker.exists():
                if (self.canonical_root / ".git").exists():
                    os.rmdir(self.canonical_root / ".git")
                os.rename(moved_marker, self.canonical_root / ".git")
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_bound_git_control_revalidates_config_ref_and_absences(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        config_path = self.canonical_root / ".git" / "config"
        packed_refs_path = self.canonical_root / ".git" / "packed-refs"
        commondir_path = self.canonical_root / ".git" / "commondir"
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            original_config = config_path.read_bytes()
            try:
                config_path.write_bytes(original_config + b"\n")
                with self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "Git config control file content changed",
                ):
                    MIRROR_MODULE._run_git(
                        bound_root,
                        "rev-parse",
                        "HEAD",
                    )
            finally:
                config_path.write_bytes(original_config)

            packed_refs_path.write_bytes(b"# pack-refs with: peeled\n")
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "packed-refs control file appeared",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            packed_refs_path.unlink()

            commondir_path.write_bytes(b".\n")
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "commondir control file portable collision set changed",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            commondir_path.unlink()

            head_payload = (self.canonical_root / ".git" / "HEAD").read_text(
                encoding="utf-8"
            )
            self.assertTrue(head_payload.startswith("ref: "))
            ref_path = (
                self.canonical_root
                / ".git"
                / head_payload.removeprefix("ref: ").strip()
            )
            saved_ref = ref_path.with_name(ref_path.name + ".saved")
            os.rename(ref_path, saved_ref)
            ref_path.write_bytes(saved_ref.read_bytes())
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "resolved HEAD reference.*replaced",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            ref_path.unlink()
            os.rename(saved_ref, ref_path)
        finally:
            for path in (packed_refs_path, commondir_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_linked_worktree_rejects_common_commondir_escape_aliases_before_copy(
        self,
    ) -> None:
        linked_root = self.root / "linked-canonical"
        self._git(
            self.canonical_root,
            "worktree",
            "add",
            "--detach",
            str(linked_root),
            self.source_commit,
        )
        common_git = self.canonical_root / ".git"
        try:
            for marker_name in ("commondir", "CoMmOnDiR"):
                with self.subTest(marker_name=marker_name):
                    marker = common_git / marker_name
                    marker.write_text("../../escape.git\n", encoding="utf-8")
                    bound_root = MIRROR_MODULE._bind_root(linked_root)
                    try:
                        with (
                            mock.patch.object(
                                MIRROR_MODULE,
                                "_materialize_private_git_control",
                            ) as materialize,
                            self.assertRaisesRegex(
                                MIRROR_MODULE.MirrorSyncError,
                                "common-directory commondir control file "
                                "appeared before private Git materialization",
                            ),
                        ):
                            MIRROR_MODULE._ensure_git_control_binding(bound_root)
                        materialize.assert_not_called()
                    finally:
                        marker.unlink(missing_ok=True)
                        MIRROR_MODULE._finish_bound_roots(bound_root)
        finally:
            for marker_name in ("commondir", "CoMmOnDiR"):
                (common_git / marker_name).unlink(missing_ok=True)
            self._git(
                self.canonical_root,
                "worktree",
                "remove",
                "--force",
                str(linked_root),
            )

    def test_linked_worktree_revalidates_common_and_private_commondir_aliases(
        self,
    ) -> None:
        linked_root = self.root / "linked-canonical"
        self._git(
            self.canonical_root,
            "worktree",
            "add",
            "--detach",
            str(linked_root),
            self.source_commit,
        )
        common_marker = self.canonical_root / ".git" / "COMMOnDir"
        bound_root = MIRROR_MODULE._bind_root(linked_root)
        private_marker: Path | None = None
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None

            common_marker.write_text("../../escape.git\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "common-directory commondir control file appeared",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            common_marker.unlink()

            private_marker = bound_root.git_control.private_path / "COMMOnDir"
            private_marker.write_text("../../escape.git\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "private Git commondir control file appeared",
            ):
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            private_marker.unlink()
        finally:
            common_marker.unlink(missing_ok=True)
            if private_marker is not None:
                private_marker.unlink(missing_ok=True)
            MIRROR_MODULE._finish_bound_roots(bound_root)
            self._git(
                self.canonical_root,
                "worktree",
                "remove",
                "--force",
                str(linked_root),
            )

    def test_linked_worktree_revalidates_commondir_collision_set_after_bind(
        self,
    ) -> None:
        linked_root = self.root / "linked-canonical"
        self._git(
            self.canonical_root,
            "worktree",
            "add",
            "--detach",
            str(linked_root),
            self.source_commit,
        )
        bound_root = MIRROR_MODULE._bind_root(linked_root)
        alias_marker: Path | None = None
        original_marker: Path | None = None
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None
            assert bound_root.git_control.admin.path is not None
            admin = bound_root.git_control.admin
            original_marker = admin.path / "commondir"
            alias_marker = admin.path / "CoMmOnDiR"

            if not alias_marker.exists():
                alias_marker.write_text("../escape.git\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "portable collision set changed",
                ):
                    MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
                alias_marker.unlink()

                original_marker.rename(alias_marker)
                with self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "portable collision set changed",
                ):
                    MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
                alias_marker.rename(original_marker)

            self.assertEqual(
                MIRROR_MODULE._path_collision_key(PurePosixPath("Café")),
                MIRROR_MODULE._path_collision_key(
                    PurePosixPath("CAFE\u0301"),
                ),
            )
            real_inventory = MIRROR_MODULE._control_marker_collision_names
            for observed in (
                ("CoMmOnDiR", "commondir"),
                ("CoMmOnDiR",),
            ):
                with self.subTest(simulated_collision_set=observed):

                    def simulated_inventory(parent, name, label):
                        if parent.fd == admin.fd and name == "commondir":
                            return observed
                        return real_inventory(parent, name, label)

                    with (
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_control_marker_collision_names",
                            side_effect=simulated_inventory,
                        ),
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            "portable collision set changed",
                        ),
                    ):
                        MIRROR_MODULE._run_git(
                            bound_root,
                            "rev-parse",
                            "HEAD",
                        )
        finally:
            if (
                alias_marker is not None
                and original_marker is not None
                and alias_marker.exists()
                and not original_marker.exists()
            ):
                alias_marker.rename(original_marker)
            MIRROR_MODULE._finish_bound_roots(bound_root)
            self._git(
                self.canonical_root,
                "worktree",
                "remove",
                "--force",
                str(linked_root),
            )

    def test_private_tool_root_lock_covers_owner_publication(self) -> None:
        real_create_owner = MIRROR_MODULE._create_owner_record
        observed_lock = False

        def assert_locked(
            root,
            tool_root,
            quarantine,
            private_name,
            private,
            *,
            quarantine_locked,
        ):
            nonlocal observed_lock
            assert tool_root.path is not None
            competitor_fd = os.open(
                tool_root.path,
                MIRROR_MODULE._DIRECTORY_FLAGS,
            )
            try:
                with self.assertRaises(BlockingIOError):
                    MIRROR_MODULE.fcntl.flock(
                        competitor_fd,
                        MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB,
                    )
                observed_lock = True
            finally:
                os.close(competitor_fd)
            return real_create_owner(
                root,
                tool_root,
                quarantine,
                private_name,
                private,
                quarantine_locked=quarantine_locked,
            )

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_create_owner_record",
                side_effect=assert_locked,
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(observed_lock)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_primary_stale_recovery_retains_quarantine_lease_until_outer_release(
        self,
    ) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        tool_root_path = (
            self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        quarantine_path = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.11111111111111111111111111111111"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root_path / owner_name
        owner_path.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                private_name,
                (123, 456, stat.S_IFDIR),
                "11111111111111111111111111111111",
                "cleanup",
            )
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())
        real_remove_owner = MIRROR_MODULE._remove_stale_owner_record
        observed_nested_cleanup = False

        def remove_then_probe(*args, **kwargs):
            nonlocal observed_nested_cleanup
            result = real_remove_owner(*args, **kwargs)
            self.assertTrue(kwargs["quarantine_locked"])
            self._assert_directory_lock_contended(quarantine_path)
            observed_nested_cleanup = True
            return result

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_remove_stale_owner_record",
                side_effect=remove_then_probe,
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(observed_nested_cleanup)
            self._assert_directory_lock_available(quarantine_path)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_legacy_stale_recovery_retains_quarantine_lease_until_outer_release(
        self,
    ) -> None:
        legacy_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-legacy-lock-lifetime."
        )
        self.addCleanup(legacy_directory.cleanup)
        shared_parent = Path(os.path.realpath(legacy_directory.name))
        tool_root_path = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine_path = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool_root_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.22222222222222222222222222222222"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root_path / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_VERSION,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "22222222222222222222222222222222",
                    "phase": "cleanup",
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_remove_owner = MIRROR_MODULE._remove_stale_owner_record
        observed_nested_cleanup = False

        def remove_then_probe(*args, **kwargs):
            nonlocal observed_nested_cleanup
            result = real_remove_owner(*args, **kwargs)
            self.assertTrue(kwargs["quarantine_locked"])
            self._assert_directory_lock_contended(quarantine_path)
            observed_nested_cleanup = True
            return result

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_remove_stale_owner_record",
                    side_effect=remove_then_probe,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(observed_nested_cleanup)
            self._assert_directory_lock_available(quarantine_path)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_legacy_cutover_fence_spans_owner_publication_and_releases(self) -> None:
        legacy_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-legacy-publication."
        )
        self.addCleanup(legacy_directory.cleanup)
        shared_parent = Path(os.path.realpath(legacy_directory.name))
        tool_root_path = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine_path = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool_root_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-legacy-publication-v1",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_create_owner = MIRROR_MODULE._create_owner_record
        retained_receipts = []
        publication_count = 0

        def create_owner_then_probe(*args, **kwargs):
            nonlocal publication_count
            private_parent = args[1]
            context = private_parent.private_control_context
            assert context is not None
            self.assertEqual(len(context.legacy_receipts), 1)
            self._assert_directory_lock_contended(tool_root_path)
            self._assert_directory_lock_contended(quarantine_path)
            result = real_create_owner(*args, **kwargs)
            self._assert_directory_lock_contended(tool_root_path)
            self._assert_directory_lock_contended(quarantine_path)
            retained_receipts.extend(context.legacy_receipts)
            publication_count += 1
            return result

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_create_owner_record",
                    side_effect=create_owner_then_probe,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertEqual(publication_count, 1)
            self.assertTrue(retained_receipts)
            for receipt in retained_receipts:
                assert receipt.parent_binding is not None
                assert receipt.tool_binding is not None
                assert receipt.quarantine_binding is not None
                self.assertEqual(receipt.parent_binding.fd, -1)
                self.assertEqual(receipt.tool_binding.fd, -1)
                self.assertEqual(receipt.quarantine_binding.fd, -1)
            assert bound_root.git_control is not None
            context = bound_root.git_control.private_parent.private_control_context
            assert context is not None
            self.assertEqual(context.legacy_receipts, ())
            self._assert_directory_lock_available(tool_root_path)
            self._assert_directory_lock_available(quarantine_path)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_legacy_cutover_fence_releases_after_owner_publication_failure(
        self,
    ) -> None:
        legacy_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-legacy-publication-failure."
        )
        self.addCleanup(legacy_directory.cleanup)
        shared_parent = Path(os.path.realpath(legacy_directory.name))
        tool_root_path = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine_path = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool_root_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-legacy-publication-failure-v1",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        retained_receipts = []

        def fail_owner_publication(*args, **_kwargs):
            private_parent = args[1]
            context = private_parent.private_control_context
            assert context is not None
            retained_receipts.extend(context.legacy_receipts)
            self._assert_directory_lock_contended(tool_root_path)
            self._assert_directory_lock_contended(quarantine_path)
            raise MIRROR_MODULE.MirrorSyncError("injected owner publication failure")

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_create_owner_record",
                    side_effect=fail_owner_publication,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "injected owner publication failure",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(retained_receipts)
            for receipt in retained_receipts:
                assert receipt.parent_binding is not None
                assert receipt.tool_binding is not None
                assert receipt.quarantine_binding is not None
                self.assertEqual(receipt.parent_binding.fd, -1)
                self.assertEqual(receipt.tool_binding.fd, -1)
                self.assertEqual(receipt.quarantine_binding.fd, -1)
            self._assert_directory_lock_available(tool_root_path)
            self._assert_directory_lock_available(quarantine_path)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_legacy_terminal_evidence_after_owner_publication_blocks_cutover(
        self,
    ) -> None:
        legacy_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-legacy-terminal-evidence."
        )
        self.addCleanup(legacy_directory.cleanup)
        shared_parent = Path(os.path.realpath(legacy_directory.name))
        tool_root_path = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine_path = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool_root_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-legacy-terminal-evidence-v1",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_create_owner = MIRROR_MODULE._create_owner_record
        sentinel = tool_root_path / "late-recovery-evidence"
        injected = False

        def create_owner_then_inject(*args, **kwargs):
            nonlocal injected
            result = real_create_owner(*args, **kwargs)
            sentinel.write_bytes(b"late legacy evidence\n")
            injected = True
            return result

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_create_owner_record",
                    side_effect=create_owner_then_inject,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertTrue(injected)
            self.assertEqual(sentinel.read_bytes(), b"late legacy evidence\n")
            self._assert_directory_lock_available(tool_root_path)
            self._assert_directory_lock_available(quarantine_path)
            primary_tool = (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            )
            self.assertEqual(tuple(primary_tool.iterdir()), ())
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_terminal_legacy_scan_budget_blocks_all_primary_allocation(
        self,
    ) -> None:
        cases = (
            "expired",
            "expired-after-terminal",
            "zero-bytes",
            "zero-entries",
        )
        real_decision = MIRROR_MODULE._private_control_preallocation_decision
        real_terminal = MIRROR_MODULE._revalidate_legacy_private_control_receipts
        for case in cases:
            with self.subTest(case=case):
                primary_home = self.root / f"budget-primary-home-{case}"
                primary_home.mkdir(mode=0o700)
                primary_parent = (
                    primary_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
                )
                legacy_parent = self.root / f"budget-legacy-parent-{case}"
                legacy_parent.mkdir(mode=0o700)
                legacy_tool = legacy_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                legacy_quarantine = (
                    legacy_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
                )
                legacy_tool.mkdir(mode=0o700)
                legacy_quarantine.mkdir(mode=0o700)
                before = (
                    legacy_tool.stat().st_ino,
                    legacy_quarantine.stat().st_ino,
                )
                primary_spec = MIRROR_MODULE.PrivateControlRootSpec(
                    root_id=f"budget-primary-{case}",
                    parent_path=primary_parent,
                    allocate=True,
                    account_home=primary_home,
                    shared_parent=False,
                )
                legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
                    root_id=f"budget-legacy-{case}",
                    parent_path=legacy_parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
                bound_root = MIRROR_MODULE._bind_root(self.target_root)
                bound_root.operation = MIRROR_MODULE.OperationBudget(
                    deadline=time.monotonic() + 30,
                    remaining_bytes=10_000_000,
                    remaining_entries=10_000,
                )

                def exhaust_terminal_budget(states):
                    decision = real_decision(states)
                    assert bound_root.operation is not None
                    if case == "expired":
                        bound_root.operation.deadline = time.monotonic() - 1
                    elif case == "zero-bytes":
                        bound_root.operation.remaining_bytes = 0
                    elif case == "zero-entries":
                        bound_root.operation.remaining_entries = 0
                    return decision

                def expire_after_terminal(receipts, *, operation):
                    real_terminal(receipts, operation=operation)
                    if case == "expired-after-terminal":
                        assert operation is not None
                        operation.deadline = time.monotonic() - 1

                try:
                    with (
                        mock.patch.object(
                            MIRROR_MODULE,
                            "PRIVATE_CONTROL_ROOT_SPECS",
                            (primary_spec, legacy_spec),
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_legacy_shared_parent_policy_is_valid",
                            return_value=True,
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_private_control_preallocation_decision",
                            side_effect=exhaust_terminal_budget,
                        ),
                        mock.patch.object(
                            MIRROR_MODULE,
                            "_revalidate_legacy_private_control_receipts",
                            side_effect=expire_after_terminal,
                        ),
                        self.assertRaisesRegex(
                            MIRROR_MODULE.MirrorSyncError,
                            "mirror operation exceeded|aggregate budget",
                        ),
                    ):
                        MIRROR_MODULE._ensure_git_control_binding(bound_root)
                finally:
                    bound_root.operation = None
                    MIRROR_MODULE._finish_bound_roots(bound_root)

                self.assertFalse(primary_parent.exists())
                self.assertEqual(
                    (
                        legacy_tool.stat().st_ino,
                        legacy_quarantine.stat().st_ino,
                    ),
                    before,
                )
                self.assertEqual(tuple(legacy_tool.iterdir()), ())
                self.assertEqual(tuple(legacy_quarantine.iterdir()), ())

    def test_absent_legacy_receipt_rejects_anchor_replacement(self) -> None:
        primary_home = self.root / "absence-primary-home"
        primary_home.mkdir(mode=0o700)
        primary_parent = primary_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
        anchor = self.root / "absence-anchor"
        anchor.mkdir(mode=0o700)
        saved_anchor = self.root / "saved-absence-anchor"
        legacy_parent = anchor / "custom-legacy-parent"
        primary_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="custom-primary-absence-v2",
            parent_path=primary_parent,
            allocate=True,
            account_home=primary_home,
            shared_parent=False,
        )
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="custom-legacy-absence-v1",
            parent_path=legacy_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_preflight = MIRROR_MODULE._preflight_legacy_private_control_roots
        real_decision = MIRROR_MODULE._private_control_preallocation_decision
        captured_receipts = []
        replaced = False

        def capture_preflight(*args, **kwargs):
            states, receipts = real_preflight(*args, **kwargs)
            captured_receipts.extend(receipts)
            return states, receipts

        def replace_anchor_after_preflight(states):
            nonlocal replaced
            decision = real_decision(states)
            anchor.rename(saved_anchor)
            anchor.mkdir(mode=0o700)
            replaced = True
            return decision

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_preflight_legacy_private_control_roots",
                    side_effect=capture_preflight,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_private_control_preallocation_decision",
                    side_effect=replace_anchor_after_preflight,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "absence anchor.*replaced before transaction completion",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(replaced)
        self.assertEqual(len(captured_receipts), 1)
        receipt = captured_receipts[0]
        self.assertEqual(receipt.root_id, legacy_spec.root_id)
        self.assertEqual(receipt.parent_path, legacy_parent)
        self.assertEqual(receipt.absence_anchor_path, anchor)
        self.assertEqual(receipt.absence_name, legacy_parent.name)
        self.assertIsNotNone(receipt.absence_anchor_identity)
        self.assertIsNotNone(receipt.absence_anchor_access_policy)
        self.assertIsNotNone(receipt.absence_binding)
        assert receipt.absence_binding is not None
        self.assertEqual(receipt.absence_binding.parent.fd, -1)
        self.assertFalse(legacy_parent.exists())
        self.assertFalse((saved_anchor / legacy_parent.name).exists())
        self.assertFalse(primary_parent.exists())

    def test_owner_creation_error_cleanup_retains_lease_and_closes_context(
        self,
    ) -> None:
        real_write_all = MIRROR_MODULE._write_all
        real_remove_owner = MIRROR_MODULE._remove_stale_owner_record
        observed_nested_cleanup = False
        captured_context = None

        def fail_after_owner_write(file_fd, payload, display_path):
            real_write_all(file_fd, payload, display_path)
            if display_path.name.endswith(".owner.json"):
                raise MIRROR_MODULE.MirrorSyncError(
                    "injected owner publication failure"
                )

        def remove_then_probe(*args, **kwargs):
            nonlocal observed_nested_cleanup, captured_context
            tool_root = args[1]
            captured_context = tool_root.private_control_context
            result = real_remove_owner(*args, **kwargs)
            self.assertTrue(kwargs["quarantine_locked"])
            assert kwargs["quarantine"].path is not None
            self._assert_directory_lock_contended(kwargs["quarantine"].path)
            observed_nested_cleanup = True
            return result

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_write_all",
                    side_effect=fail_after_owner_write,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_remove_stale_owner_record",
                    side_effect=remove_then_probe,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_remove_bound_private_directory",
                    side_effect=MIRROR_MODULE.MirrorSyncError(
                        "injected secondary snapshot cleanup failure"
                    ),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "injected owner publication failure; secondary "
                    "private-control cleanup failures: .*injected secondary "
                    "snapshot cleanup failure",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(observed_nested_cleanup)
        self.assertIsNotNone(captured_context)
        assert captured_context is not None
        self.assertEqual(captured_context.home.fd, -1)
        self.assertEqual(captured_context.parent.fd, -1)
        self.assertEqual(captured_context.quarantine.fd, -1)
        self._assert_directory_lock_available(captured_context.quarantine.path)

    def test_post_publication_revalidation_failure_closes_owner_and_can_retry(
        self,
    ) -> None:
        real_create_owner = MIRROR_MODULE._create_owner_record
        real_revalidate = MIRROR_MODULE._revalidate_primary_private_control_context
        captured_owner = None
        owner_published = False
        injected = False

        def capture_owner(*args, **kwargs):
            nonlocal captured_owner, owner_published
            result = real_create_owner(*args, **kwargs)
            captured_owner = result[2]
            owner_published = True
            return result

        def fail_after_publication(tool_root):
            nonlocal injected
            if owner_published and not injected:
                injected = True
                raise MIRROR_MODULE.MirrorSyncError(
                    "injected post-publication context failure"
                )
            return real_revalidate(tool_root)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_create_owner_record",
                    side_effect=capture_owner,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_revalidate_primary_private_control_context",
                    side_effect=fail_after_publication,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "injected post-publication context failure",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)

            self.assertTrue(injected)
            self.assertIsNotNone(captured_owner)
            assert captured_owner is not None
            self.assertEqual(captured_owner.fd, -1)
            assert captured_owner.path is not None
            self.assertFalse(captured_owner.path.exists())

            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertIsNotNone(bound_root.git_control)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_second_publication_lock_error_closes_retained_context(self) -> None:
        real_bind_tool_root = MIRROR_MODULE._bind_private_tool_root
        real_flock = MIRROR_MODULE.fcntl.flock
        captured_tool_root = None
        injected = False

        def capture_tool_root(*args, **kwargs):
            nonlocal captured_tool_root
            captured_tool_root = real_bind_tool_root(*args, **kwargs)
            return captured_tool_root

        def fail_quarantine_lock(file_fd, operation):
            nonlocal injected
            if captured_tool_root is not None:
                context = captured_tool_root.private_control_context
                if (
                    context is not None
                    and file_fd == context.quarantine.fd
                    and operation
                    == MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB
                    and not injected
                ):
                    injected = True
                    raise OSError("injected quarantine lease failure")
            return real_flock(file_fd, operation)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_private_tool_root",
                    side_effect=capture_tool_root,
                ),
                mock.patch.object(
                    MIRROR_MODULE.fcntl,
                    "flock",
                    side_effect=fail_quarantine_lock,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "busy or unleaseable",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertIsNotNone(captured_tool_root)
        assert captured_tool_root is not None
        context = captured_tool_root.private_control_context
        self.assertIsNone(context)
        self.assertEqual(captured_tool_root.fd, -1)
        tool_path = (
            self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        quarantine_path = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        self._assert_directory_lock_available(tool_path)
        self._assert_directory_lock_available(quarantine_path)

    def test_retained_context_close_error_does_not_interrupt_root_cleanup(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        context = bound_root.git_control.private_parent.private_control_context
        self.assertIsNotNone(context)
        assert context is not None
        failing_fd = context.parent.fd
        real_close = MIRROR_MODULE.os.close
        injected = False

        def close_then_report_error(file_fd):
            nonlocal injected
            if file_fd == failing_fd and not injected:
                injected = True
                real_close(file_fd)
                raise OSError("injected retained parent close failure")
            return real_close(file_fd)

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "close",
                side_effect=close_then_report_error,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "cannot close every retained private-control descriptor",
            ),
        ):
            MIRROR_MODULE._close_bound_root(bound_root)

        self.assertTrue(injected)
        self.assertIsNone(bound_root.git_control)
        self.assertEqual(bound_root.fd, -1)
        self.assertEqual(bound_root.git_executable.fd, -1)
        self.assertEqual(bound_root.managed_ancestors, {})
        self.assertEqual(context.home.fd, -1)
        self.assertEqual(context.parent.fd, -1)
        self.assertEqual(context.quarantine.fd, -1)

    def test_normal_owner_cleanup_retains_quarantine_lease_until_outer_release(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        context = bound_root.git_control.private_parent.private_control_context
        self.assertIsNotNone(context)
        assert context is not None
        quarantine_path = context.quarantine.path
        assert quarantine_path is not None
        real_remove_owner = MIRROR_MODULE._remove_bound_owner_record
        observed_nested_cleanup = False

        def remove_then_probe(*args, **kwargs):
            nonlocal observed_nested_cleanup
            result = real_remove_owner(*args, **kwargs)
            self.assertTrue(kwargs["quarantine_locked"])
            self._assert_directory_lock_contended(quarantine_path)
            observed_nested_cleanup = True
            return result

        with mock.patch.object(
            MIRROR_MODULE,
            "_remove_bound_owner_record",
            side_effect=remove_then_probe,
        ):
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(observed_nested_cleanup)
        self._assert_directory_lock_available(quarantine_path)

    def test_private_cleanup_aggregates_raw_acquire_errors_and_closes_all(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        tool_root = bound_root.git_control.private_parent
        context = tool_root.private_control_context
        assert context is not None
        lease_labels = {
            tool_root.fd: "tool-root",
            context.quarantine.fd: "quarantine",
        }
        real_flock = MIRROR_MODULE.fcntl.flock
        acquire_attempts = []

        def fail_each_cleanup_acquire(file_fd, operation):
            label = lease_labels.get(file_fd)
            if (
                label is not None
                and operation
                == MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB
            ):
                acquire_attempts.append(label)
                raise OSError(f"injected raw {label} acquire failure")
            return real_flock(file_fd, operation)

        with (
            mock.patch.object(
                MIRROR_MODULE.fcntl,
                "flock",
                side_effect=fail_each_cleanup_acquire,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "tool-root acquire: OSError: injected raw tool-root acquire "
                "failure.*quarantine acquire: OSError: injected raw quarantine "
                "acquire failure",
            ),
        ):
            MIRROR_MODULE._close_bound_root(bound_root)

        self.assertEqual(acquire_attempts, ["tool-root", "quarantine"])
        self.assertIsNone(bound_root.git_control)
        self.assertEqual(bound_root.fd, -1)
        self.assertEqual(bound_root.git_executable.fd, -1)
        self.assertEqual(tool_root.fd, -1)
        self.assertEqual(context.quarantine.fd, -1)
        self.assertEqual(context.parent.fd, -1)
        self.assertEqual(context.home.fd, -1)

    def test_private_cleanup_first_unlock_failure_does_not_skip_second(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        tool_root = bound_root.git_control.private_parent
        context = tool_root.private_control_context
        assert context is not None
        lease_labels = {
            tool_root.fd: "tool-root",
            context.quarantine.fd: "quarantine",
        }
        real_flock = MIRROR_MODULE.fcntl.flock
        unlock_attempts = []
        injected = False

        def fail_first_unlock_after_release(file_fd, operation):
            nonlocal injected
            label = lease_labels.get(file_fd)
            if label is not None and operation == MIRROR_MODULE.fcntl.LOCK_UN:
                unlock_attempts.append(label)
                result = real_flock(file_fd, operation)
                if not injected:
                    injected = True
                    raise OSError("injected first cleanup unlock failure")
                return result
            return real_flock(file_fd, operation)

        with (
            mock.patch.object(
                MIRROR_MODULE.fcntl,
                "flock",
                side_effect=fail_first_unlock_after_release,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "quarantine unlock: OSError: injected first cleanup unlock failure",
            ),
        ):
            MIRROR_MODULE._close_bound_root(bound_root)

        self.assertTrue(injected)
        self.assertEqual(unlock_attempts, ["quarantine", "tool-root"])
        self.assertIsNone(bound_root.git_control)
        self.assertEqual(bound_root.fd, -1)
        self.assertEqual(tool_root.fd, -1)
        self.assertEqual(context.quarantine.fd, -1)

    def test_private_cleanup_failure_covers_and_aggregates_later_roots(
        self,
    ) -> None:
        first_root = MIRROR_MODULE._bind_root(self.canonical_root)
        second_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(first_root)
        MIRROR_MODULE._ensure_git_control_binding(second_root)
        assert first_root.git_control is not None
        assert second_root.git_control is not None
        tool_roots = {
            first_root.git_control.private_parent.fd: "first-root",
            second_root.git_control.private_parent.fd: "second-root",
        }
        real_flock = MIRROR_MODULE.fcntl.flock
        covered_roots = []

        def fail_each_root_cleanup_acquire(file_fd, operation):
            label = tool_roots.get(file_fd)
            if (
                label is not None
                and operation
                == MIRROR_MODULE.fcntl.LOCK_EX | MIRROR_MODULE.fcntl.LOCK_NB
            ):
                covered_roots.append(label)
                raise OSError(f"injected {label} cleanup acquire failure")
            return real_flock(file_fd, operation)

        with (
            mock.patch.object(
                MIRROR_MODULE.fcntl,
                "flock",
                side_effect=fail_each_root_cleanup_acquire,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "injected second-root cleanup acquire failure.*secondary "
                "bound-root finalization failures: .*injected first-root "
                "cleanup acquire failure",
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(first_root, second_root)

        self.assertEqual(covered_roots, ["second-root", "first-root"])
        for bound_root in (first_root, second_root):
            self.assertIsNone(bound_root.git_control)
            self.assertEqual(bound_root.fd, -1)
            self.assertEqual(bound_root.git_executable.fd, -1)

    def test_private_cleanup_body_raw_oserror_covers_later_roots(self) -> None:
        first_root = MIRROR_MODULE._bind_root(self.canonical_root)
        second_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(first_root)
        MIRROR_MODULE._ensure_git_control_binding(second_root)
        contexts = []
        for bound_root in (first_root, second_root):
            assert bound_root.git_control is not None
            context = bound_root.git_control.private_parent.private_control_context
            assert context is not None
            contexts.append(context)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_set_owner_record_phase",
                side_effect=OSError("injected raw cleanup body failure"),
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "injected raw cleanup body failure.*secondary bound-root "
                "finalization failures: .*injected raw cleanup body failure",
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(first_root, second_root)

        for bound_root, context in zip(
            (first_root, second_root),
            contexts,
        ):
            self.assertIsNone(bound_root.git_control)
            self.assertEqual(bound_root.fd, -1)
            self.assertEqual(bound_root.git_executable.fd, -1)
            self.assertEqual(context.home.fd, -1)
            self.assertEqual(context.parent.fd, -1)
            self.assertEqual(context.quarantine.fd, -1)

    def test_private_executable_cleanup_body_raw_oserror_closes_all(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        admin = MIRROR_MODULE._bind_absolute_control_object(
            self.target_root / ".git",
            "test Git admin directory",
            require_directory=True,
        )
        common = MIRROR_MODULE._duplicate_directory_control(
            admin,
            self.target_root / ".git",
            "test Git common directory",
        )
        try:
            binding = MIRROR_MODULE._prepare_private_git_executable(
                bound_root,
                admin,
                common,
            )
        finally:
            os.close(common.fd)
            common.fd = -1
            os.close(admin.fd)
            admin.fd = -1
        bound_root.private_git_executable = binding
        context = binding.private_parent.private_control_context
        assert context is not None

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_set_owner_record_phase",
                side_effect=OSError("injected raw executable cleanup failure"),
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "private Git executable cleanup failed: injected raw "
                "executable cleanup failure",
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertIsNone(bound_root.private_git_executable)
        self.assertEqual(bound_root.fd, -1)
        self.assertEqual(bound_root.git_executable.fd, -1)
        self.assertEqual(binding.executable.fd, -1)
        self.assertEqual(binding.private.fd, -1)
        self.assertEqual(binding.owner_record.fd, -1)
        self.assertEqual(binding.private_parent.fd, -1)
        self.assertEqual(context.quarantine.fd, -1)
        self.assertEqual(context.parent.fd, -1)
        self.assertEqual(context.home.fd, -1)

    def test_finish_bound_roots_aggregates_raw_revalidation_oserrors(self) -> None:
        first_root = MIRROR_MODULE._bind_root(self.canonical_root)
        second_root = MIRROR_MODULE._bind_root(self.target_root)
        revalidated = []

        def fail_raw_revalidation(bound_root):
            revalidated.append(bound_root.path)
            raise OSError(f"injected raw revalidation failure for {bound_root.path}")

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_revalidate_bound_root",
                side_effect=fail_raw_revalidation,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "injected raw revalidation failure.*secondary bound-root "
                "finalization failures: .*injected raw revalidation failure",
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(first_root, second_root)

        self.assertEqual(
            revalidated,
            [first_root.path, second_root.path],
        )
        for bound_root in (first_root, second_root):
            self.assertEqual(bound_root.fd, -1)
            self.assertEqual(bound_root.git_executable.fd, -1)

    def test_private_control_walkers_transfer_fd_before_effectful_close_error(
        self,
    ) -> None:
        real_close = MIRROR_MODULE.os.close

        for label, invoke in (
            (
                "account-home",
                lambda: MIRROR_MODULE._bind_trusted_account_home(self.root),
            ),
            (
                "ancestry",
                lambda: self._invoke_ancestry_with_open_candidate(),
            ),
        ):
            with self.subTest(label=label):
                close_calls: list[int] = []
                failed_fd: int | None = None

                def fail_first_close_after_effect(descriptor: int) -> None:
                    nonlocal failed_fd
                    close_calls.append(descriptor)
                    real_close(descriptor)
                    if failed_fd is None:
                        failed_fd = descriptor
                        raise OSError(f"injected {label} close-after-effect")

                with (
                    mock.patch.object(
                        MIRROR_MODULE.os,
                        "close",
                        side_effect=fail_first_close_after_effect,
                    ),
                    self.assertRaisesRegex(
                        MIRROR_MODULE.MirrorSyncError,
                        f"injected {label} close-after-effect",
                    ),
                ):
                    invoke()

                assert failed_fd is not None
                self.assertEqual(close_calls.count(failed_fd), 1)
                self.assertGreaterEqual(len(set(close_calls)), 2)

    def _invoke_ancestry_with_open_candidate(self) -> None:
        candidate_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            MIRROR_MODULE._directory_is_at_or_below(
                candidate_fd,
                (-1, -1, stat.S_IFDIR),
            )
        finally:
            os.close(candidate_fd)

    def test_primary_bind_and_prebind_cleanup_cover_every_owned_descriptor(
        self,
    ) -> None:
        spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        real_close = MIRROR_MODULE.os.close
        cleanup_enabled = False
        close_calls: list[int] = []
        failed_fd: int | None = None

        def fail_revalidation(_binding) -> None:
            nonlocal cleanup_enabled
            cleanup_enabled = True
            raise MIRROR_MODULE.MirrorSyncError("injected primary bind body failure")

        def fail_first_cleanup_close_after_effect(descriptor: int) -> None:
            nonlocal failed_fd
            if cleanup_enabled:
                close_calls.append(descriptor)
            real_close(descriptor)
            if cleanup_enabled and failed_fd is None:
                failed_fd = descriptor
                raise OSError("injected primary bind close-after-effect")

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_revalidate_absolute_control_object",
                side_effect=fail_revalidation,
            ),
            mock.patch.object(
                MIRROR_MODULE.os,
                "close",
                side_effect=fail_first_cleanup_close_after_effect,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "injected primary bind body failure.*injected primary bind "
                "close-after-effect",
            ),
        ):
            MIRROR_MODULE._bind_primary_private_control_parent_impl(
                spec,
                create=False,
            )

        assert failed_fd is not None
        self.assertEqual(close_calls.count(failed_fd), 1)
        self.assertGreaterEqual(len(set(close_calls)), 2)

        tool = self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        tool.mkdir(mode=0o700)
        tool.chmod(0o755)
        tool_identity = MIRROR_MODULE._object_identity(tool.stat())
        close_calls = []
        failed_fd = None

        def fail_tool_close_after_effect(descriptor: int) -> None:
            nonlocal failed_fd
            descriptor_identity = MIRROR_MODULE._object_identity(os.fstat(descriptor))
            if failed_fd is not None or descriptor_identity == tool_identity:
                close_calls.append(descriptor)
            real_close(descriptor)
            if descriptor_identity == tool_identity and failed_fd is None:
                failed_fd = descriptor
                raise OSError("injected primary child close-after-effect")

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "close",
                side_effect=fail_tool_close_after_effect,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "must be mode 0700.*injected primary child close-after-effect",
            ),
        ):
            MIRROR_MODULE._prebind_existing_primary_private_control_root(spec)

        assert failed_fd is not None
        self.assertEqual(close_calls.count(failed_fd), 1)
        self.assertGreaterEqual(len(set(close_calls)), 3)

    def test_absence_and_temporary_allocation_preserve_cleanup_failures(
        self,
    ) -> None:
        real_close = MIRROR_MODULE.os.close
        legacy_parent = self.root / "legacy-present-for-absence"
        legacy_parent.mkdir(mode=0o700)
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="legacy-absence-cleanup",
            parent_path=legacy_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        anchor_identity = MIRROR_MODULE._object_identity(self.root.stat())
        failed_fds: list[int] = []

        def fail_anchor_close_after_effect(descriptor: int) -> None:
            descriptor_identity = MIRROR_MODULE._object_identity(os.fstat(descriptor))
            real_close(descriptor)
            if descriptor_identity == anchor_identity and not failed_fds:
                failed_fds.append(descriptor)
                raise OSError("injected absence-anchor close-after-effect")

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "close",
                side_effect=fail_anchor_close_after_effect,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "is not absent.*injected absence-anchor close-after-effect",
            ),
        ):
            MIRROR_MODULE._capture_legacy_private_control_parent_absence(legacy_spec)

        self.assertEqual(len(failed_fds), 1)

        parent = MIRROR_MODULE._bind_absolute_control_object(
            self.private_git_control_parent,
            "allocation-test parent",
            require_directory=True,
        )
        cleanup_enabled = False
        failed_fds = []

        def fail_publish(*_args, **_kwargs) -> None:
            nonlocal cleanup_enabled
            cleanup_enabled = True
            raise OSError("injected allocation publish failure")

        def fail_temporary_close_after_effect(descriptor: int) -> None:
            real_close(descriptor)
            if cleanup_enabled and not failed_fds:
                failed_fds.append(descriptor)
                raise OSError("injected temporary close-after-effect")

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_rename_directory_entry_noreplace",
                    side_effect=fail_publish,
                ),
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "close",
                    side_effect=fail_temporary_close_after_effect,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "injected allocation publish failure.*injected temporary "
                    "close-after-effect",
                ),
            ):
                MIRROR_MODULE._create_private_control_directory_noreplace(
                    parent,
                    "allocation-target",
                    "allocation-test directory",
                    "allocation-test-root",
                )
        finally:
            os.close(parent.fd)
            parent.fd = -1

        self.assertEqual(len(failed_fds), 1)
        self.assertFalse(
            any(
                child.name.startswith(".private-control-create-")
                for child in self.private_git_control_parent.iterdir()
            )
        )

    def test_private_cleanup_uses_retained_quarantine_before_paths_move(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        private_path = bound_root.git_control.private.path
        owner_path = bound_root.git_control.owner_record.path
        assert private_path is not None
        assert owner_path is not None
        real_remove = MIRROR_MODULE._remove_bound_private_directory
        observed_pre_teardown_paths = False

        def assert_control_paths_still_exist(*args, **kwargs):
            nonlocal observed_pre_teardown_paths
            self.assertTrue(private_path.exists())
            self.assertTrue(owner_path.exists())
            context = bound_root.git_control.private_parent.private_control_context
            self.assertIsNotNone(context)
            assert context is not None
            self.assertGreaterEqual(context.quarantine.fd, 0)
            observed_pre_teardown_paths = True
            return real_remove(*args, **kwargs)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_bind_durable_quarantine_root",
                side_effect=AssertionError("lexical quarantine bind is forbidden"),
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_remove_bound_private_directory",
                side_effect=assert_control_paths_still_exist,
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(observed_pre_teardown_paths)
        self.assertFalse(private_path.exists())
        self.assertFalse(owner_path.exists())

    def test_stale_owner_recovery_uses_private_control_filesystem_quarantine(
        self,
    ) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        tool_root_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.0123456789abcdef0123456789abcdef"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root_path / owner_name
        owner_path.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                private_name,
                (123, 456, stat.S_IFDIR),
                "abcdef0123456789abcdef0123456789",
                "cleanup",
            )
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        tool_root = MIRROR_MODULE._bind_absolute_control_object(
            tool_root_path,
            "test private Git tool root",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME,
            "test retained private Git quarantine",
            require_directory=True,
        )
        root_id = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id
        tool_root.root_id = root_id
        quarantine.root_id = root_id

        try:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            with mock.patch.object(
                MIRROR_MODULE,
                "_bind_durable_quarantine_root",
                side_effect=AssertionError("lexical quarantine bind is forbidden"),
            ):
                MIRROR_MODULE._recover_stale_private_snapshots(
                    bound_root,
                    tool_root,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
        finally:
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(quarantine.fd)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertFalse(owner_path.exists())

    def test_stale_owner_recovery_quarantines_unhashable_phase_schema(
        self,
    ) -> None:
        tool_root_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        tool_root_path.mkdir(mode=0o700)
        quarantine_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine_path.mkdir(mode=0o700)
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.abcdefabcdefabcdefabcdefabcdefab"
        )
        owner_name = f"{private_name}.owner.json"
        owner_path = tool_root_path / owner_name
        owner_path.write_text(
            json.dumps(
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                    "owner_pid": os.getpid(),
                    "owner_uid": os.geteuid(),
                    "owner_gid": os.getegid(),
                    "owner_nonce": "abcdef0123456789abcdef0123456789",
                    "phase": [],
                    "private_name": private_name,
                    "private_identity": [123, 456, stat.S_IFDIR],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        tool_root = MIRROR_MODULE._bind_absolute_control_object(
            tool_root_path,
            "test private Git tool root",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            quarantine_path,
            "test retained private Git quarantine",
            require_directory=True,
        )
        root_id = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id
        tool_root.root_id = root_id
        quarantine.root_id = root_id
        try:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            MIRROR_MODULE._recover_stale_private_snapshots(
                bound_root,
                tool_root,
                quarantine=quarantine,
                quarantine_locked=True,
            )
        finally:
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(quarantine.fd)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertFalse(owner_path.exists())
        self.assertEqual(
            len(
                [
                    path
                    for path in tool_root_path.iterdir()
                    if path.name.startswith(".quarantine-")
                ]
            ),
            1,
        )

    def test_stale_owner_recovery_binds_quarantine_before_private_removal(
        self,
    ) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        tool_root_path = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.fedcba9876543210fedcba9876543210"
        )
        owner_name = f"{private_name}.owner.json"
        private_path = tool_root_path / private_name
        owner_path = tool_root_path / owner_name
        private_path.mkdir(mode=0o700)
        private_identity = MIRROR_MODULE._object_identity(private_path.stat())
        owner_path.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                private_name,
                private_identity,
                "fedcba9876543210fedcba9876543210",
                "cleanup",
            )
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        tool_root = MIRROR_MODULE._bind_absolute_control_object(
            tool_root_path,
            "test private Git tool root",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME,
            "test retained private Git quarantine",
            require_directory=True,
        )
        root_id = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id
        tool_root.root_id = root_id
        quarantine.root_id = root_id
        real_remove_private = MIRROR_MODULE._remove_bound_private_directory
        retained_quarantine_verified = False

        def require_prebound_quarantine(*args, **kwargs):
            nonlocal retained_quarantine_verified
            self.assertGreaterEqual(quarantine.fd, 0)
            retained_quarantine_verified = True
            return real_remove_private(*args, **kwargs)

        try:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_durable_quarantine_root",
                    side_effect=AssertionError("lexical quarantine bind is forbidden"),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_remove_bound_private_directory",
                    side_effect=require_prebound_quarantine,
                ),
            ):
                MIRROR_MODULE._recover_stale_private_snapshots(
                    bound_root,
                    tool_root,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
        finally:
            MIRROR_MODULE.fcntl.flock(quarantine.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(quarantine.fd)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(retained_quarantine_verified)
        self.assertFalse(private_path.exists())
        self.assertFalse(owner_path.exists())

    def test_quarantine_rejects_cross_filesystem_source_before_isolation(
        self,
    ) -> None:
        source_directory = self.target_root / "nested-source"
        source_directory.mkdir()
        source_fd = os.open(source_directory, MIRROR_MODULE._DIRECTORY_FLAGS)
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        real_fstat = os.fstat

        def report_distinct_source_device(fd):
            metadata = real_fstat(fd)
            if fd != source_fd:
                return metadata
            values = list(metadata)
            values[2] = metadata.st_dev + 1
            return os.stat_result(values)

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "fstat",
                    side_effect=report_distinct_source_device,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "different filesystems",
                ),
            ):
                MIRROR_MODULE._bind_durable_quarantine_root(
                    bound_root,
                    source_parent_fd=source_fd,
                )
        finally:
            os.close(source_fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_quarantine_retains_transient_and_recovery_artifacts(self) -> None:
        parent_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            transient = self.target_root / "transaction-temporary"
            transient.write_bytes(b"internal transaction bytes\n")
            transient_snapshot = MIRROR_MODULE._safe_read_leaf_snapshot(
                parent_fd,
                transient.name,
                MIRROR_MODULE.PurePosixPath(transient.name),
            )
            MIRROR_MODULE._isolate_and_remove_file(
                self.target_root,
                parent_fd,
                transient.name,
                transient_snapshot,
                MIRROR_MODULE.PurePosixPath(transient.name),
                retention_kind=MIRROR_MODULE.QUARANTINE_TRANSIENT_KIND,
            )

            recovery = self.target_root / "retired-user-target"
            recovery.write_bytes(b"receipt-bound recovery bytes\n")
            recovery_snapshot = MIRROR_MODULE._safe_read_leaf_snapshot(
                parent_fd,
                recovery.name,
                MIRROR_MODULE.PurePosixPath(recovery.name),
            )
            MIRROR_MODULE._isolate_and_remove_file(
                self.target_root,
                parent_fd,
                recovery.name,
                recovery_snapshot,
                MIRROR_MODULE.PurePosixPath(recovery.name),
                retention_kind=MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
            )

            quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                self.target_root,
                source_parent_fd=parent_fd,
            )
            os.close(quarantine.fd)
        finally:
            os.close(parent_fd)

        quarantine_path = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        names = sorted(path.name for path in quarantine_path.iterdir())
        transient_paths = [
            quarantine_path / name
            for name in names
            if name.startswith("transient-file-")
        ]
        recovery_paths = [
            quarantine_path / name
            for name in names
            if name.startswith("recovery-file-")
        ]
        self.assertEqual(
            [path.read_bytes() for path in transient_paths],
            [transient_snapshot.payload],
        )
        self.assertEqual(
            [path.read_bytes() for path in recovery_paths],
            [recovery_snapshot.payload],
        )

    def test_quarantine_name_binds_identity_content_and_access_policy(self) -> None:
        parent_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            source = self.target_root / "quarantine-name-source"
            source.write_bytes(b"name-bound bytes\n")
            expected = MIRROR_MODULE._safe_read_leaf_snapshot(
                parent_fd,
                source.name,
                MIRROR_MODULE.PurePosixPath(source.name),
            )
        finally:
            os.close(parent_fd)

        name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_TRANSIENT_KIND,
        )
        self.assertTrue(
            MIRROR_MODULE._quarantine_file_name_matches(
                name,
                expected,
                MIRROR_MODULE.QUARANTINE_TRANSIENT_KIND,
            )
        )
        changed_identity = MIRROR_MODULE.FileSnapshot(
            payload=expected.payload,
            mode=expected.mode,
            identity=(
                expected.identity[0],
                expected.identity[1] + 1,
                expected.identity[2],
            ),
            access_policy=expected.access_policy,
            size=expected.size,
        )
        changed_content = MIRROR_MODULE.FileSnapshot(
            payload=b"same-size changed\n",
            mode=expected.mode,
            identity=expected.identity,
            access_policy=expected.access_policy,
            size=expected.size,
        )
        changed_policy = MIRROR_MODULE.FileSnapshot(
            payload=expected.payload,
            mode=expected.mode ^ 0o020,
            identity=expected.identity,
            access_policy=(
                expected.access_policy[0] ^ 0o020,
                expected.access_policy[1],
                expected.access_policy[2],
            ),
            size=expected.size,
        )
        for changed in (changed_identity, changed_content, changed_policy):
            self.assertFalse(
                MIRROR_MODULE._quarantine_file_name_matches(
                    name,
                    changed,
                    MIRROR_MODULE.QUARANTINE_TRANSIENT_KIND,
                )
            )

    def test_quarantine_retains_legacy_unclassified_transient_name(self) -> None:
        quarantine_path = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        quarantine_path.mkdir(mode=0o700)
        placeholder = quarantine_path / "legacy-transient-placeholder"
        placeholder.write_bytes(b"legacy transient bytes\n")
        metadata = placeholder.stat()
        digest = hashlib.sha256(placeholder.read_bytes()).hexdigest()[:16]
        legacy_name = (
            f"transient-file-{metadata.st_dev:x}-{metadata.st_ino:x}-"
            f"{digest}-0123456789abcdef0123456789abcdef"
        )
        legacy_path = placeholder.with_name(legacy_name)
        placeholder.rename(legacy_path)
        parent_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                self.target_root,
                source_parent_fd=parent_fd,
            )
            os.close(quarantine.fd)
        finally:
            os.close(parent_fd)

        self.assertEqual(legacy_path.read_bytes(), b"legacy transient bytes\n")

    def test_quarantine_capacity_is_checked_before_source_isolation(self) -> None:
        parent_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        source = self.target_root / "capacity-bound-temporary"
        source.write_bytes(b"capacity-preserved bytes\n")
        expected = MIRROR_MODULE._safe_read_leaf_snapshot(
            parent_fd,
            source.name,
            MIRROR_MODULE.PurePosixPath(source.name),
        )
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_durable_quarantine_entry_count",
                    return_value=MIRROR_MODULE.MAX_DURABLE_QUARANTINE_ENTRIES,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "reached its bounded entry limit",
                ),
            ):
                MIRROR_MODULE._isolate_and_remove_file(
                    self.target_root,
                    parent_fd,
                    source.name,
                    expected,
                    MIRROR_MODULE.PurePosixPath(source.name),
                    retention_kind=MIRROR_MODULE.QUARANTINE_TRANSIENT_KIND,
                )
        finally:
            os.close(parent_fd)

        self.assertEqual(source.read_bytes(), expected.payload)

    def test_quarantine_at_capacity_can_still_be_bound_for_recovery(self) -> None:
        parent_fd = os.open(self.target_root, MIRROR_MODULE._DIRECTORY_FLAGS)
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_durable_quarantine_entry_count",
                return_value=MIRROR_MODULE.MAX_DURABLE_QUARANTINE_ENTRIES,
            ):
                quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                    self.target_root,
                    source_parent_fd=parent_fd,
                )
            os.close(quarantine.fd)
        finally:
            os.close(parent_fd)

    def test_stale_recovery_rejects_owner_path_replacement_after_lock(
        self,
    ) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        tool_root = (
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
            / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.0123456789abcdef0123456789abcdef"
        )
        owner_name = f"{private_name}.owner.json"
        private_path = tool_root / private_name
        owner_path = tool_root / owner_name
        saved_owner = tool_root / f"{owner_name}.saved"
        private_path.mkdir(mode=0o700)
        private_metadata = private_path.stat()
        owner_path.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id,
                private_name,
                MIRROR_MODULE._object_identity(private_metadata),
                "abcdef0123456789abcdef0123456789",
                "ready",
            )
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())
        real_read = MIRROR_MODULE._safe_read_private_owner_record_snapshot
        injected = False

        def replace_owner_after_lock(parent_fd, name, display_path, **kwargs):
            nonlocal injected
            if name == owner_name and not injected:
                injected = True
                os.rename(owner_path, saved_owner)
                owner_path.write_bytes(saved_owner.read_bytes())
                owner_path.chmod(0o600)
                os.chown(owner_path, os.geteuid(), os.getegid())
            return real_read(parent_fd, name, display_path, **kwargs)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_safe_read_private_owner_record_snapshot",
                    side_effect=replace_owner_after_lock,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private owner record was replaced while opening it",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)
            for path in (owner_path, saved_owner):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            if private_path.exists():
                private_path.rmdir()

    def test_git_child_uses_private_control_during_transient_index_swap(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        index_path = self.target_root / ".git" / "index"
        saved_index = self.target_root / ".git" / "index.saved"
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            expected = MIRROR_MODULE._run_git(
                bound_root,
                "ls-files",
                "--full-name",
                "--stage",
                "-z",
                "--",
                MIRROR_MODULE._top_literal_pathspec(
                    MIRROR_MODULE.PurePosixPath("README.md")
                ),
            )
            real_collect = MIRROR_MODULE._collect_bounded_git_output

            def swap_live_index_while_child_runs(
                process,
                operation=None,
                *,
                owner=None,
            ):
                os.rename(index_path, saved_index)
                index_path.write_bytes(b"transient malicious index\n")
                try:
                    return real_collect(process, operation, owner=owner)
                finally:
                    index_path.unlink()
                    os.rename(saved_index, index_path)

            with mock.patch.object(
                MIRROR_MODULE,
                "_collect_bounded_git_output",
                side_effect=swap_live_index_while_child_runs,
            ):
                observed = MIRROR_MODULE._run_git(
                    bound_root,
                    "ls-files",
                    "--full-name",
                    "--stage",
                    "-z",
                    "--",
                    MIRROR_MODULE._top_literal_pathspec(
                        MIRROR_MODULE.PurePosixPath("README.md")
                    ),
                )
            self.assertEqual(observed, expected)
        finally:
            if saved_index.exists():
                if index_path.exists():
                    index_path.unlink()
                os.rename(saved_index, index_path)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_destination_is_read_back_and_bound(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        real_scan = MIRROR_MODULE._scan_private_git_tree
        scan_count = 0

        def corrupt_after_initial_destination_scan(
            private_fd,
            operation=None,
            *,
            skip_entries=frozenset(),
        ):
            nonlocal scan_count
            observed = real_scan(
                private_fd,
                operation,
                skip_entries=skip_entries,
            )
            scan_count += 1
            if scan_count == 1:
                index_fd = os.open(
                    "index",
                    os.O_WRONLY | os.O_TRUNC,
                    dir_fd=private_fd,
                )
                try:
                    os.write(index_fd, b"corrupt private index\n")
                    os.fsync(index_fd)
                finally:
                    os.close(index_fd)
            return observed

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_scan_private_git_tree",
                    side_effect=corrupt_after_initial_destination_scan,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "differs from bound source bytes",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_git_child_revalidates_private_control_after_execution(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            private_index = bound_root.git_control.private_path / "index"
            original_index = private_index.read_bytes()
            real_collect = MIRROR_MODULE._collect_bounded_git_output

            def mutate_private_index_after_child(
                process,
                operation=None,
                *,
                owner=None,
            ):
                result = real_collect(process, operation, owner=owner)
                private_index.write_bytes(b"post-child private drift\n")
                return result

            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    side_effect=mutate_private_index_after_child,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "private Git control snapshot changed",
                ),
            ):
                MIRROR_MODULE._run_git(
                    bound_root,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                )
            self.assertFalse(bound_root.git_control.object_integrity_verified)
            private_index.write_bytes(original_index)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_git_child_rejects_transient_loose_object_tampering(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None
            private_objects = bound_root.git_control.private_path / "objects"
            loose_objects = [
                path
                for path in private_objects.rglob("*")
                if (
                    path.is_file()
                    and re.fullmatch(r"[0-9a-f]{2}", path.parent.name)
                    and re.fullmatch(r"[0-9a-f]{38}", path.name)
                )
            ]
            self.assertTrue(loose_objects)
            object_path = loose_objects[0]
            original = object_path.read_bytes()
            original_mode = stat.S_IMODE(object_path.stat().st_mode)
            real_collect = MIRROR_MODULE._collect_bounded_git_output

            def tamper_while_child_runs(
                process,
                operation=None,
                *,
                owner=None,
            ):
                object_path.chmod(0o644)
                mutated = bytes([original[0] ^ 0x01]) + original[1:]
                object_path.write_bytes(mutated)
                try:
                    return real_collect(process, operation, owner=owner)
                finally:
                    object_path.write_bytes(original)
                    object_path.chmod(original_mode)

            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    side_effect=tamper_while_child_runs,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "object snapshot content-stability proof was invalidated",
                ),
            ):
                MIRROR_MODULE._run_git(
                    bound_root,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                )
            self.assertFalse(bound_root.git_control.object_integrity_verified)
        finally:
            # The deliberately invalidated binding must not be reaccepted merely
            # because the bytes were restored after the child. Close directly so
            # the private snapshot can be removed without claiming revalidation.
            MIRROR_MODULE._close_bound_root(bound_root)

    def test_git_child_rejects_transient_packed_object_tampering(self) -> None:
        self._git(self.target_root, "gc", "--prune=now")
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            assert bound_root.git_control is not None
            pack_files = sorted(
                (bound_root.git_control.private_path / "objects" / "pack").glob(
                    "*.pack"
                )
            )
            self.assertTrue(pack_files)
            pack_path = pack_files[0]
            original = pack_path.read_bytes()
            original_mode = stat.S_IMODE(pack_path.stat().st_mode)
            real_collect = MIRROR_MODULE._collect_bounded_git_output

            def tamper_while_child_runs(
                process,
                operation=None,
                *,
                owner=None,
            ):
                pack_path.chmod(0o644)
                mutated = bytes([original[0] ^ 0x01]) + original[1:]
                pack_path.write_bytes(mutated)
                try:
                    return real_collect(process, operation, owner=owner)
                finally:
                    pack_path.write_bytes(original)
                    pack_path.chmod(original_mode)

            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_collect_bounded_git_output",
                    side_effect=tamper_while_child_runs,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "object snapshot content-stability proof was invalidated",
                ),
            ):
                MIRROR_MODULE._run_git(
                    bound_root,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                )
            self.assertFalse(bound_root.git_control.object_integrity_verified)
        finally:
            MIRROR_MODULE._close_bound_root(bound_root)

    def test_private_git_snapshot_ignores_ambient_tmpdir_and_rejects_overlap(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with mock.patch.dict(
                os.environ,
                {"TMPDIR": str(self.target_root / ".git")},
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertEqual(
                bound_root.git_control.private_path.parent,
                (
                    MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
                    / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                ),
            )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        overlapping_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (
                        MIRROR_MODULE.PrivateControlRootSpec(
                            root_id="test-overlapping-primary-home-v1",
                            parent_path=(
                                self.target_root
                                / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
                            ),
                            allocate=True,
                            account_home=self.target_root,
                            shared_parent=False,
                        ),
                    ),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "overlaps repository root",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(overlapping_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(overlapping_root)

    def test_primary_cross_filesystem_quarantine_fails_before_recovery(self) -> None:
        tool = self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        tool_sentinel = tool / "tool-sentinel"
        quarantine_sentinel = quarantine / "quarantine-sentinel"
        tool_sentinel.write_bytes(b"tool bytes\n")
        quarantine_sentinel.write_bytes(b"quarantine bytes\n")
        primary_root_id = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0].root_id
        real_validate = MIRROR_MODULE._validate_private_control_root_topology
        real_fstat = os.fstat
        recovery_calls = 0

        def inject_cross_device(*args, **kwargs):
            quarantine_binding = kwargs["quarantine"]
            if kwargs["root_id"] != primary_root_id or quarantine_binding is None:
                return real_validate(*args, **kwargs)

            def report_distinct_quarantine_device(fd):
                metadata = real_fstat(fd)
                if fd != quarantine_binding.fd:
                    return metadata
                values = list(metadata)
                values[2] = metadata.st_dev + 1
                return os.stat_result(values)

            with mock.patch.object(
                MIRROR_MODULE.os,
                "fstat",
                side_effect=report_distinct_quarantine_device,
            ):
                return real_validate(*args, **kwargs)

        def reject_recovery(*_args, **_kwargs):
            nonlocal recovery_calls
            recovery_calls += 1
            self.fail("primary recovery ran before topology validation")

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_validate_private_control_root_topology",
                    side_effect=inject_cross_device,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_recover_stale_private_snapshots",
                    side_effect=reject_recovery,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "must be on the same filesystem",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)
        self.assertEqual(recovery_calls, 0)
        self.assertEqual(tool_sentinel.read_bytes(), b"tool bytes\n")
        self.assertEqual(quarantine_sentinel.read_bytes(), b"quarantine bytes\n")

    def test_legacy_child_overlap_with_primary_home_is_zero_mutation(self) -> None:
        shared_parent = self.root / "legacy-primary-home-overlap"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        sentinel = tool / "recovery-sentinel"
        sentinel.write_bytes(b"must not be recovered\n")
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-legacy-primary-home-overlap-v1",
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_recover_stale_private_snapshots",
                    side_effect=AssertionError(
                        "legacy recovery ran before topology validation"
                    ),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "overlaps primary account home",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)
        self.assertEqual(sentinel.read_bytes(), b"must not be recovered\n")
        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )

    def test_private_control_topology_rejects_reciprocal_child_ancestry(
        self,
    ) -> None:
        topology_parent = self.root / "reciprocal-topology-parent"
        topology_parent.mkdir(mode=0o700)
        tool_path = topology_parent / "tool"
        quarantine_path = topology_parent / "quarantine"
        tool_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        parent = MIRROR_MODULE._bind_absolute_control_object(
            topology_parent,
            "test topology parent",
            require_directory=True,
        )
        tool = MIRROR_MODULE._bind_absolute_control_object(
            tool_path,
            "test topology tool",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            quarantine_path,
            "test topology quarantine",
            require_directory=True,
        )
        admin = MIRROR_MODULE._bind_absolute_control_object(
            self.target_root / ".git",
            "test topology Git admin",
            require_directory=True,
        )
        real_is_below = MIRROR_MODULE._directory_is_at_or_below

        def report_parent_below_tool(candidate_fd, ancestor_identity):
            candidate_identity = MIRROR_MODULE._object_identity(os.fstat(candidate_fd))
            if (
                candidate_identity == parent.identity
                and ancestor_identity == tool.identity
            ):
                return True
            return real_is_below(candidate_fd, ancestor_identity)

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_directory_is_at_or_below",
                    side_effect=report_parent_below_tool,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "not a strict descendant",
                ),
            ):
                MIRROR_MODULE._validate_private_control_root_topology(
                    root_id="test-reciprocal-topology-v1",
                    parent=parent,
                    tool_root=tool,
                    quarantine=quarantine,
                    repository_root=bound_root,
                    admin=admin,
                    common=admin,
                )
        finally:
            os.close(admin.fd)
            os.close(quarantine.fd)
            os.close(tool.fd)
            os.close(parent.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_control_topology_rejects_child_root_overlap(self) -> None:
        topology_parent = self.root / "overlap-topology-parent"
        topology_parent.mkdir(mode=0o700)
        tool_path = topology_parent / "tool"
        quarantine_path = topology_parent / "quarantine"
        tool_path.mkdir(mode=0o700)
        quarantine_path.mkdir(mode=0o700)
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        parent = MIRROR_MODULE._bind_absolute_control_object(
            topology_parent,
            "test topology parent",
            require_directory=True,
        )
        tool = MIRROR_MODULE._bind_absolute_control_object(
            tool_path,
            "test topology tool",
            require_directory=True,
        )
        quarantine = MIRROR_MODULE._bind_absolute_control_object(
            quarantine_path,
            "test topology quarantine",
            require_directory=True,
        )
        admin = MIRROR_MODULE._bind_absolute_control_object(
            self.target_root / ".git",
            "test topology Git admin",
            require_directory=True,
        )
        real_overlap = MIRROR_MODULE._directory_bindings_overlap

        def report_child_overlap(left_fd, right_fd):
            if {left_fd, right_fd} == {tool.fd, quarantine.fd}:
                return True
            return real_overlap(left_fd, right_fd)

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_directory_bindings_overlap",
                    side_effect=report_child_overlap,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "must not overlap",
                ),
            ):
                MIRROR_MODULE._validate_private_control_root_topology(
                    root_id="test-child-overlap-topology-v1",
                    parent=parent,
                    tool_root=tool,
                    quarantine=quarantine,
                    repository_root=bound_root,
                    admin=admin,
                    common=admin,
                )
        finally:
            os.close(admin.fd)
            os.close(quarantine.fd)
            os.close(tool.fd)
            os.close(parent.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_private_git_snapshot_never_binds_the_default_host_parent(self) -> None:
        observed_paths = []
        real_bind = MIRROR_MODULE._bind_absolute_control_object

        def reject_default_host_parent(path, *args, **kwargs):
            candidate = Path(os.path.abspath(path))
            self.assertFalse(
                candidate == self.host_private_git_control_parent
                or self.host_private_git_control_parent in candidate.parents,
                f"default host private-control path was accessed: {candidate}",
            )
            observed_paths.append(candidate)
            return real_bind(path, *args, **kwargs)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_bind_absolute_control_object",
                side_effect=reject_default_host_parent,
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertEqual(
                bound_root.git_control.private_path.parent,
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME,
            )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)
        self.assertTrue(observed_paths)

    def test_foreign_preclaimed_legacy_leaf_cannot_block_primary_home_root(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-shared-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        preclaimed = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        preclaimed.mkdir(mode=0o700)
        preclaimed.chmod(0o000)
        before = preclaimed.lstat()
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_metadata = MIRROR_MODULE._legacy_child_metadata
        real_bind_child = MIRROR_MODULE._bind_relative_control_directory
        legacy_child_opens = []

        def classify_preclaim_as_foreign(parent_fd, name, root_id):
            observed = real_metadata(parent_fd, name, root_id)
            if name == MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME and observed is not None:
                identity, access_policy = observed
                return identity, (
                    access_policy[0],
                    os.geteuid() + 1,
                    access_policy[2],
                )
            return observed

        def reject_legacy_child_open(parent, name, label):
            if parent.path == shared_parent:
                legacy_child_opens.append(name)
                self.fail(f"foreign legacy child was opened: {name}")
            return real_bind_child(parent, name, label)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        observed_after = None
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_child_metadata",
                    side_effect=classify_preclaim_as_foreign,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_relative_control_directory",
                    side_effect=reject_legacy_child_open,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            self.assertEqual(
                bound_root.git_control.private_path.parent,
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME,
            )
            observed_after = preclaimed.lstat()
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)
            preclaimed.chmod(0o700)

        assert observed_after is not None
        self.assertEqual(
            (
                observed_after.st_dev,
                observed_after.st_ino,
                stat.S_IMODE(observed_after.st_mode),
            ),
            (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode)),
        )
        self.assertEqual(legacy_child_opens, [])

    def test_primary_home_root_rejects_unsafe_ancestor_and_parent_policy(
        self,
    ) -> None:
        unsafe_ancestor = self.root / "unsafe-ancestor"
        unsafe_ancestor.mkdir(mode=0o700)
        unsafe_ancestor.chmod(0o777)
        unsafe_home = unsafe_ancestor / "home"
        unsafe_home.mkdir(mode=0o700)
        unsafe_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-unsafe-primary-home-v1",
            parent_path=(unsafe_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME),
            allocate=True,
            account_home=unsafe_home,
            shared_parent=False,
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "ancestors must be root/current-owned and not group/world writable",
        ):
            MIRROR_MODULE._bind_primary_private_control_parent(unsafe_spec)
        self.assertFalse(unsafe_spec.parent_path.exists())

        safe_home = self.root / "safe-home"
        safe_home.mkdir(mode=0o700)
        unsafe_parent = safe_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
        unsafe_parent.mkdir(mode=0o755)
        unsafe_parent_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="test-unsafe-parent-primary-home-v1",
            parent_path=unsafe_parent,
            allocate=True,
            account_home=safe_home,
            shared_parent=False,
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "must be mode 0700 and owned by the current uid",
        ):
            MIRROR_MODULE._bind_primary_private_control_parent(unsafe_parent_spec)

    def test_legacy_evidence_without_initial_quarantine_is_zero_mutation_pending(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-without-quarantine"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        tool.mkdir(mode=0o700)
        evidence = tool / "recovery-evidence"
        evidence.write_bytes(b"must remain in the original root\n")
        before = (tool.lstat().st_ino, evidence.lstat().st_ino, evidence.read_bytes())
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertEqual(
            (tool.lstat().st_ino, evidence.lstat().st_ino, evidence.read_bytes()),
            before,
        )
        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )
        self.assertFalse(
            (
                self.private_git_control_parent
                / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            ).exists()
        )

    def test_legacy_tool_replacement_before_binding_preserves_both_objects(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-tool-replacement"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        saved_tool = shared_parent / "saved-tool-root"
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_bind = MIRROR_MODULE._bind_relative_control_directory
        injected = False

        def replace_tool(parent, name, label):
            nonlocal injected
            if (
                parent.path == shared_parent
                and name == MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                and not injected
            ):
                tool.rename(saved_tool)
                tool.mkdir(mode=0o700)
                injected = True
            return real_bind(parent, name, label)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_relative_control_directory",
                    side_effect=replace_tool,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "tool root changed before recovery binding",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertTrue(tool.is_dir())
        self.assertTrue(saved_tool.is_dir())
        self.assertEqual(tuple(tool.iterdir()), ())
        self.assertEqual(tuple(saved_tool.iterdir()), ())
        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )

    def test_terminal_registry_revalidation_covers_later_legacy_roots(
        self,
    ) -> None:
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = []
        for index in range(2):
            parent = self.root / f"terminal-legacy-{index}"
            parent.mkdir(mode=0o700)
            legacy_specs.append(
                MIRROR_MODULE.PrivateControlRootSpec(
                    root_id=f"terminal-legacy-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        real_metadata = MIRROR_MODULE._legacy_child_metadata
        calls: dict[tuple[str, str], int] = {}
        injected = False

        def mutate_first_root_on_terminal_pass(parent_fd, name, root_id):
            nonlocal injected
            key = (root_id, name)
            calls[key] = calls.get(key, 0) + 1
            if (
                root_id == legacy_specs[0].root_id
                and name == MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                and calls[key] == 3
            ):
                (legacy_specs[0].parent_path / name).mkdir(mode=0o700)
                injected = True
            return real_metadata(parent_fd, name, root_id)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, *legacy_specs),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_child_metadata",
                    side_effect=mutate_first_root_on_terminal_pass,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "terminal legacy registry revalidation covered every root",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertGreaterEqual(
            calls[(legacy_specs[1].root_id, MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME)],
            3,
        )
        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )

    def test_terminal_registry_revalidation_aggregates_uncertain_parent_close(
        self,
    ) -> None:
        receipts = []
        parent_identities = []
        for index in range(2):
            parent = self.root / f"terminal-close-parent-{index}"
            parent.mkdir(mode=0o700)
            metadata = parent.stat()
            identity = MIRROR_MODULE._object_identity(metadata)
            parent_identities.append(identity)
            receipts.append(
                MIRROR_MODULE.LegacyPrivateControlReceipt(
                    root_id=f"terminal-close-root-{index}",
                    parent_path=parent,
                    parent_identity=identity,
                    parent_access_policy=MIRROR_MODULE._access_policy(metadata),
                    tool_record=None,
                    quarantine_record=None,
                    state="absent",
                )
            )
        real_close = os.close
        real_fstat = os.fstat
        second_root_metadata_calls = 0
        injected = False
        real_metadata = MIRROR_MODULE._legacy_child_metadata

        def close_then_report_uncertain(fd):
            nonlocal injected
            identity = MIRROR_MODULE._object_identity(real_fstat(fd))
            real_close(fd)
            if identity == parent_identities[0] and not injected:
                injected = True
                raise OSError("injected close uncertainty")

        def record_later_root(parent_fd, name, root_id):
            nonlocal second_root_metadata_calls
            if root_id == receipts[1].root_id:
                second_root_metadata_calls += 1
            return real_metadata(parent_fd, name, root_id)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_legacy_shared_parent_policy_is_valid",
                return_value=True,
            ),
            mock.patch.object(
                MIRROR_MODULE,
                "_legacy_child_metadata",
                side_effect=record_later_root,
            ),
            mock.patch.object(
                MIRROR_MODULE.os,
                "close",
                side_effect=close_then_report_uncertain,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "terminal legacy registry revalidation covered every root.*"
                "injected close uncertainty",
            ),
        ):
            MIRROR_MODULE._revalidate_legacy_private_control_receipts(
                tuple(receipts),
                operation=None,
            )

        self.assertTrue(injected)
        self.assertEqual(second_root_metadata_calls, 2)

    def test_legacy_quarantine_replacement_before_recovery_is_not_mutated(
        self,
    ) -> None:
        shared_parent = self.root / "legacy-quarantine-replacement"
        shared_parent.mkdir(mode=0o700)
        tool = shared_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = shared_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        saved_quarantine = shared_parent / "saved-quarantine-root"
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=shared_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        real_bind = MIRROR_MODULE._bind_relative_control_directory
        injected = False

        def replace_quarantine(parent, name, label):
            nonlocal injected
            if (
                parent.path == shared_parent
                and name == MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
                and not injected
            ):
                quarantine.rename(saved_quarantine)
                quarantine.mkdir(mode=0o700)
                injected = True
            return real_bind(parent, name, label)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_relative_control_directory",
                    side_effect=replace_quarantine,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "quarantine changed before recovery binding",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertTrue(quarantine.is_dir())
        self.assertTrue(saved_quarantine.is_dir())
        self.assertEqual(tuple(quarantine.iterdir()), ())
        self.assertEqual(tuple(saved_quarantine.iterdir()), ())
        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )

    def test_distinct_legacy_parent_child_alias_is_inconclusive(self) -> None:
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-distinct-legacy-alias."
        )
        self.addCleanup(legacy_directory.cleanup)
        legacy_root = Path(os.path.realpath(legacy_directory.name))
        parents = []
        legacy_specs = []
        for index in range(2):
            parent = legacy_root / f"alias-legacy-{index}"
            parent.mkdir(mode=0o700)
            (parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME).mkdir(mode=0o700)
            parents.append(parent)
            legacy_specs.append(
                MIRROR_MODULE.PrivateControlRootSpec(
                    root_id=f"alias-legacy-{index}",
                    parent_path=parent,
                    allocate=False,
                    account_home=None,
                    shared_parent=True,
                )
            )
        real_metadata = MIRROR_MODULE._legacy_child_metadata
        first_tool_record = None

        def alias_second_tool(parent_fd, name, root_id):
            nonlocal first_tool_record
            observed = real_metadata(parent_fd, name, root_id)
            if name != MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME:
                return observed
            if root_id == legacy_specs[0].root_id:
                first_tool_record = observed
                return observed
            assert first_tool_record is not None
            return first_tool_record

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, *legacy_specs),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_child_metadata",
                    side_effect=alias_second_tool,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "distinct legacy parent aliases an earlier control object",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertFalse(
            (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ).exists()
        )

    def test_existing_primary_alias_matrix_is_rejected_before_child_open(
        self,
    ) -> None:
        tool = self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        parent_identity = MIRROR_MODULE._object_identity(
            self.private_git_control_parent.stat()
        )
        real_metadata = MIRROR_MODULE._private_control_child_metadata
        real_bind = MIRROR_MODULE._bind_existing_private_control_directory

        for scenario in ("parent-child", "child-child"):
            with self.subTest(scenario=scenario):
                tool_record = None
                child_opens: list[str] = []

                def aliased_metadata(parent_fd, name, label):
                    nonlocal tool_record
                    observed = real_metadata(parent_fd, name, label)
                    assert observed is not None
                    if name == MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME:
                        tool_record = observed
                        if scenario == "parent-child":
                            return parent_identity, observed[1]
                        return observed
                    if scenario == "child-child":
                        assert tool_record is not None
                        return tool_record
                    return observed

                def reject_child_open(parent, name, label, root_id, expected_record):
                    if name in {
                        MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME,
                        MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME,
                    }:
                        child_opens.append(name)
                        self.fail(f"aliased primary child was opened: {name}")
                    return real_bind(parent, name, label, root_id, expected_record)

                with (
                    mock.patch.object(
                        MIRROR_MODULE,
                        "_private_control_child_metadata",
                        side_effect=aliased_metadata,
                    ),
                    mock.patch.object(
                        MIRROR_MODULE,
                        "_bind_existing_private_control_directory",
                        side_effect=reject_child_open,
                    ),
                    self.assertRaisesRegex(
                        MIRROR_MODULE.MirrorSyncError,
                        "fixed child roles alias|fixed child aliases its own parent",
                    ),
                ):
                    MIRROR_MODULE._prebind_existing_primary_private_control_root(spec)
                self.assertEqual(child_opens, [])

    def test_existing_primary_root_is_validated_before_legacy_or_allocation(
        self,
    ) -> None:
        unsafe_tool_root = (
            self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        )
        unsafe_tool_root.mkdir(mode=0o700)
        unsafe_tool_root.chmod(0o755)
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_preflight_legacy_private_control_roots",
                ) as legacy_preflight,
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "existing primary private Git tool root.*must be mode 0700",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
            legacy_preflight.assert_not_called()
        finally:
            unsafe_tool_root.chmod(0o700)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_generator_deduplicates_legacy_roots_by_bound_parent_identity(
        self,
    ) -> None:
        shared_parent = self.root / "duplicate-legacy-shared-parent"
        shared_parent.mkdir(mode=0o700)
        shared_parent.chmod(0o1777)
        primary_spec = MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS[0]
        legacy_specs = tuple(
            MIRROR_MODULE.PrivateControlRootSpec(
                root_id=f"test-legacy-duplicate-{index}",
                parent_path=shared_parent,
                allocate=False,
                account_home=None,
                shared_parent=True,
            )
            for index in range(2)
        )
        real_metadata = MIRROR_MODULE._legacy_child_metadata
        metadata_calls = []

        def record_metadata(parent_fd, name, root_id):
            metadata_calls.append((name, root_id))
            return real_metadata(parent_fd, name, root_id)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, *legacy_specs),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_child_metadata",
                    side_effect=record_metadata,
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(metadata_calls)
        self.assertEqual(
            {root_id for _name, root_id in metadata_calls},
            {legacy_specs[0].root_id},
        )
        self.assertEqual(
            [name for name, _root_id in metadata_calls].count(
                MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            ),
            5,
        )
        self.assertEqual(
            [name for name, _root_id in metadata_calls].count(
                MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            ),
            5,
        )

    def test_primary_namespace_appearance_after_absence_is_not_adopted(
        self,
    ) -> None:
        self.private_git_control_parent.rmdir()
        sentinel_payload = b"attacker-owned namespace\n"
        real_terminal = MIRROR_MODULE._revalidate_legacy_private_control_receipts
        injected = False

        def create_competing_namespace(receipts, *, operation):
            nonlocal injected
            real_terminal(receipts, operation=operation)
            self.private_git_control_parent.mkdir(mode=0o700)
            tool = (
                self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            )
            quarantine = (
                self.private_git_control_parent
                / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            )
            tool.mkdir(mode=0o700)
            quarantine.mkdir(mode=0o700)
            (tool / "sentinel").write_bytes(sentinel_payload)
            injected = True

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_revalidate_legacy_private_control_receipts",
                    side_effect=create_competing_namespace,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "appeared before exclusive allocation",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertEqual(
            (
                self.private_git_control_parent
                / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                / "sentinel"
            ).read_bytes(),
            sentinel_payload,
        )
        self.assertFalse(
            any(
                path.name.startswith(".private-control-create-")
                for path in self.root.iterdir()
            )
        )

    def test_failed_private_control_allocation_removes_exact_empty_temporary(
        self,
    ) -> None:
        parent = MIRROR_MODULE._bind_absolute_control_object(
            self.private_git_control_parent,
            "test private-control parent",
            require_directory=True,
        )
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_rename_directory_entry_noreplace",
                    side_effect=OSError("injected publication failure"),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "cannot publish test private-control child exclusively",
                ),
            ):
                MIRROR_MODULE._create_private_control_directory_noreplace(
                    parent,
                    "test-child",
                    "test private-control child",
                    "test-primary-home-v1",
                )
        finally:
            os.close(parent.fd)

        self.assertFalse(
            any(
                path.name.startswith(".private-control-create-")
                for path in self.private_git_control_parent.iterdir()
            )
        )

    def test_failed_private_control_allocation_retains_nonempty_without_listing(
        self,
    ) -> None:
        parent = MIRROR_MODULE._bind_absolute_control_object(
            self.private_git_control_parent,
            "test private-control parent",
            require_directory=True,
        )

        def fill_temporary_then_fail(parent_fd, temporary_name, final_name):
            temporary = self.private_git_control_parent / temporary_name
            for index in range(129):
                (temporary / f"entry-{index:03d}").write_bytes(b"retained\n")
            raise OSError("injected nonempty publication failure")

        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_rename_directory_entry_noreplace",
                    side_effect=fill_temporary_then_fail,
                ),
                mock.patch.object(
                    MIRROR_MODULE.os,
                    "listdir",
                    side_effect=AssertionError("unbounded listing is forbidden"),
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "cannot publish test private-control child exclusively",
                ),
            ):
                MIRROR_MODULE._create_private_control_directory_noreplace(
                    parent,
                    "test-child",
                    "test private-control child",
                    "test-primary-home-v1",
                )
        finally:
            os.close(parent.fd)

        retained = [
            path
            for path in self.private_git_control_parent.iterdir()
            if path.name.startswith(".private-control-create-")
        ]
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(tuple(retained[0].iterdir())), 129)

    def test_primary_recovery_uses_retained_namespace_not_lexical_decoy(
        self,
    ) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        original_parent = self.private_git_control_parent
        original_tool = original_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        malformed_owner = original_tool / (
            "sync-canonical-git-control.123.0123456789abcdef0123456789abcdef.owner.json"
        )
        malformed_owner.write_bytes(b"{not-json\n")
        malformed_owner.chmod(0o600)
        saved_parent = self.root / "saved-primary-control"
        decoy_sentinel = b"lexical decoy must remain untouched\n"
        real_recover = MIRROR_MODULE._recover_stale_private_snapshots
        injected = False

        def replace_namespace_before_recovery(
            root,
            tool_root,
            *,
            quarantine,
            quarantine_locked,
        ):
            nonlocal injected
            original_parent.rename(saved_parent)
            original_parent.mkdir(mode=0o700)
            decoy_tool = original_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
            decoy_quarantine = (
                original_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            )
            decoy_tool.mkdir(mode=0o700)
            decoy_quarantine.mkdir(mode=0o700)
            (decoy_tool / "sentinel").write_bytes(decoy_sentinel)
            injected = True
            return real_recover(
                root,
                tool_root,
                quarantine=quarantine,
                quarantine_locked=quarantine_locked,
            )

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_recover_stale_private_snapshots",
                    side_effect=replace_namespace_before_recovery,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "allocation root fixed name changed|was replaced",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(injected)
        self.assertEqual(
            (
                original_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME / "sentinel"
            ).read_bytes(),
            decoy_sentinel,
        )
        self.assertEqual(
            sorted(
                path.name
                for path in (
                    original_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                ).iterdir()
            ),
            ["sentinel"],
        )
        self.assertFalse(malformed_owner.exists())
        self.assertTrue(
            any(
                path.name.startswith(".quarantine-")
                for path in (
                    saved_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
                ).iterdir()
            )
        )

    def test_legacy_owner_root_mismatch_precedes_pending_without_mutation(
        self,
    ) -> None:
        account_home = self.root / "mismatch-primary-home"
        account_home.mkdir(mode=0o700)
        primary_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="mismatch-primary-home-v1",
            parent_path=(account_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME),
            allocate=True,
            account_home=account_home,
            shared_parent=False,
        )
        legacy_parent = self.root / "mismatch-legacy-parent"
        legacy_parent.mkdir(mode=0o700)
        legacy_spec = MIRROR_MODULE.PrivateControlRootSpec(
            root_id="mismatch-legacy-v0",
            parent_path=legacy_parent,
            allocate=False,
            account_home=None,
            shared_parent=True,
        )
        tool = legacy_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = legacy_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        tool.mkdir(mode=0o700)
        quarantine.mkdir(mode=0o700)
        private_name = "sync-canonical-git-control.123.33333333333333333333333333333333"
        private = tool / private_name
        private.mkdir(mode=0o700)
        private_identity = MIRROR_MODULE._object_identity(private.stat())
        owner = tool / f"{private_name}.owner.json"
        owner.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                "wrong-legacy-root-v9",
                private_name,
                private_identity,
                "44444444444444444444444444444444",
                "cleanup",
            )
        )
        owner.chmod(0o600)
        owner_before = (owner.lstat().st_ino, owner.read_bytes())
        private_before = private.lstat().st_ino
        tool_before = tuple(sorted(path.name for path in tool.iterdir()))
        quarantine_before = tuple(sorted(path.name for path in quarantine.iterdir()))

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "PRIVATE_CONTROL_ROOT_SPECS",
                    (primary_spec, legacy_spec),
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_legacy_shared_parent_policy_is_valid",
                    return_value=True,
                ),
                self.assertRaises(MIRROR_MODULE.MirrorSyncError) as caught,
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        reason = str(caught.exception).partition(":")[0]
        self.assertEqual(
            reason,
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        self.assertIn(
            MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
            str(caught.exception),
        )
        self.assertEqual(
            MIRROR_MODULE._private_control_preallocation_decision((reason,)),
            (
                False,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
        )
        self.assertEqual(
            ENGINE_MODULE._mirror_private_control_preallocation_decision((reason,)),
            (
                False,
                ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
        )
        self.assertFalse(primary_spec.parent_path.exists())
        self.assertEqual((owner.lstat().st_ino, owner.read_bytes()), owner_before)
        self.assertEqual(private.lstat().st_ino, private_before)
        self.assertEqual(
            tuple(sorted(path.name for path in tool.iterdir())),
            tool_before,
        )
        self.assertEqual(
            tuple(sorted(path.name for path in quarantine.iterdir())),
            quarantine_before,
        )

    def test_primary_owner_root_mismatch_is_retained_without_mutation(self) -> None:
        initial_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(initial_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(initial_root)

        tool = self.private_git_control_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        quarantine = (
            self.private_git_control_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        private_name = "sync-canonical-git-control.123.fedcba9876543210fedcba9876543210"
        private = tool / private_name
        private.mkdir(mode=0o700)
        private_identity = MIRROR_MODULE._object_identity(private.stat())
        owner = tool / f"{private_name}.owner.json"
        owner.write_bytes(
            MIRROR_MODULE._owner_record_payload(
                "wrong-primary-root-v9",
                private_name,
                private_identity,
                "fedcba9876543210fedcba9876543210",
                "cleanup",
            )
        )
        owner.chmod(0o600)
        owner_before = (owner.lstat().st_ino, owner.read_bytes())
        private_before = private.lstat().st_ino
        quarantine_before = tuple(sorted(path.name for path in quarantine.iterdir()))

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ):
                MIRROR_MODULE._ensure_git_control_binding(bound_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertEqual((owner.lstat().st_ino, owner.read_bytes()), owner_before)
        self.assertEqual(private.lstat().st_ino, private_before)
        self.assertEqual(
            tuple(sorted(path.name for path in quarantine.iterdir())),
            quarantine_before,
        )

    def test_private_git_cleanup_preserves_a_replaced_top_directory(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        control = bound_root.git_control
        self.assertIsNotNone(control)
        private_path = control.private_path
        moved_path = private_path.with_name(private_path.name + ".saved")
        real_remove = MIRROR_MODULE._remove_bound_private_directory

        def replace_before_cleanup(
            root,
            private_parent,
            private,
            private_name,
            expected_manifest,
        ):
            os.rename(private_path, moved_path)
            private_path.mkdir(mode=0o700)
            return real_remove(
                root,
                private_parent,
                private,
                private_name,
                expected_manifest,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_remove_bound_private_directory",
                side_effect=replace_before_cleanup,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "private Git control snapshot was replaced",
            ),
        ):
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(private_path.is_dir())
        self.assertTrue(moved_path.is_dir())
        private_path.rmdir()
        shutil.rmtree(moved_path)

    def test_generate_refuses_an_untracked_locked_source(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        extra_payload = b"untracked locked source\n"
        lock["sources"]["extra"] = {
            "path": "tests/extra.py",
            "sha256": hashlib.sha256(extra_payload).hexdigest(),
            "mode": "0644",
        }
        lock["mirrors"]["toolbox"]["files"]["extra"] = "tests/extra.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "declare missing fixture source",
        )
        extra_path = self.canonical_root / "tests" / "extra.py"
        extra_path.parent.mkdir()
        extra_path.write_bytes(extra_payload)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "dirty or untracked",
        ):
            self._generate()

    def test_generate_verifies_the_canonical_origin(self) -> None:
        self._git(
            self.canonical_root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/Joey-Tools/wrong-canonical.git",
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "canonical repository does not match",
        ):
            self._generate()

    def test_lock_rejects_an_absent_git_control_target(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["mirrors"]["toolbox"]["files"]["engine"] = ".git/generated-config"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "Git control plane",
        ):
            MIRROR_MODULE.load_source_lock(self.canonical_root)

    def test_lock_uses_github_case_insensitive_repository_identity(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        base = json.loads(lock_path.read_text(encoding="utf-8"))

        canonical_alias = json.loads(json.dumps(base))
        canonical_alias["mirrors"]["toolbox"]["repository"] = (
            "joey-tools/CANONICAL-FIXTURE"
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "must not point back",
        ):
            MIRROR_MODULE._parse_source_lock(
                (json.dumps(canonical_alias) + "\n").encode("utf-8")
            )

        duplicate_alias = json.loads(json.dumps(base))
        duplicate_alias["mirrors"]["private"]["repository"] = (
            "joey-tools/TOOLBOX-FIXTURE"
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "duplicate/colliding mirror repository",
        ):
            MIRROR_MODULE._parse_source_lock(
                (json.dumps(duplicate_alias) + "\n").encode("utf-8")
            )

    def test_repository_origin_accepts_github_case_alias(self) -> None:
        self._git(
            self.canonical_root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/joey-tools/CANONICAL-FIXTURE.git",
        )
        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            MIRROR_MODULE._verify_canonical_repository(
                bound_root,
                "Joey-Tools/canonical-fixture",
            )
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_lock_rejects_portable_source_aliases_and_self_alias(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        base = json.loads(lock_path.read_text(encoding="utf-8"))
        extra_record = {
            "path": "Scripts/Engine.py",
            "sha256": "0" * 64,
            "mode": "0644",
        }
        collision = json.loads(json.dumps(base))
        collision["sources"]["extra"] = extra_record
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "NFC\\+casefold",
        ):
            MIRROR_MODULE._parse_source_lock(
                (json.dumps(collision) + "\n").encode("utf-8")
            )

        self_alias = json.loads(json.dumps(base))
        self_alias["sources"]["engine"]["path"] = "SYNC-SOURCE-LOCK.JSON"
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "must not hash itself",
        ):
            MIRROR_MODULE._parse_source_lock(
                (json.dumps(self_alias) + "\n").encode("utf-8")
            )

    def test_lock_and_refresh_reject_unsupported_source_modes(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        for raw_mode in ("0600", "0700"):
            with self.subTest(parser_mode=raw_mode):
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                lock["sources"]["engine"]["mode"] = raw_mode
                with self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "mode must be 0644 or 0755",
                ):
                    MIRROR_MODULE._parse_source_lock(
                        (json.dumps(lock) + "\n").encode("utf-8")
                    )

        self.source_path.chmod(0o600)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "canonical source engine mode must be 0644 or 0755",
        ):
            MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=False)

    def test_lock_rejects_bidirectional_reserved_and_casefold_overlap(
        self,
    ) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        base = json.loads(lock_path.read_text(encoding="utf-8"))

        def parse_with_targets(targets: dict[str, str]):
            lock = json.loads(json.dumps(base))
            lock["sources"]["extra"] = {
                "path": "scripts/extra.py",
                "sha256": "0" * 64,
                "mode": "0644",
            }
            lock["mirrors"]["toolbox"]["files"] = targets
            return MIRROR_MODULE._parse_source_lock(
                (json.dumps(lock) + "\n").encode("utf-8")
            )

        rejected = (
            {"engine": "generated-sync-source-lock.json/child"},
            {"engine": ".generated-sync-transaction.json/child"},
            {"engine": ".generated-sync-transaction.pending/child"},
            {"engine": ".generated-sync-transaction.complete/child"},
            {"engine": (".generated-sync-source-lock.json.sync-exchange.json/child")},
            {"engine": "Generated-Sync-Source-Lock.json"},
            {
                "engine": "scripts/engine.py",
                "extra": "Scripts/Engine.py",
            },
            {
                "engine": "a/file.py",
                "extra": "a/.file.py.sync-exchange.json/child",
            },
        )
        for targets in rejected:
            with (
                self.subTest(targets=targets),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "overlap|colliding",
                ),
            ):
                parse_with_targets(targets)

        parsed = parse_with_targets(
            {
                "engine": "state/generated-sync-source-lock.json.backup",
                "extra": "scripts/extra.py",
            }
        )
        self.assertEqual(
            parsed.mirrors["toolbox"].files["engine"],
            MIRROR_MODULE.PurePosixPath("state/generated-sync-source-lock.json.backup"),
        )

    def test_generate_rejects_casefold_alias_of_consumer_tracked_path(
        self,
    ) -> None:
        existing = self.target_root / "Scripts" / "Engine.py"
        existing.parent.mkdir()
        existing.write_bytes(b"consumer-owned case alias\n")
        self._commit(self.target_root, "add consumer case alias")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "tracked path collides.*NFC\\+casefold",
        ):
            self._generate()

        self.assertEqual(existing.read_bytes(), b"consumer-owned case alias\n")

    def test_generate_rejects_nfd_alias_of_consumer_tracked_path(
        self,
    ) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["mirrors"]["toolbox"]["files"]["engine"] = "docs/Caf\u00e9.py"
        lock_path.write_text(
            json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.source_commit = self._commit(
            self.canonical_root,
            "use NFC generated target",
        )
        existing = self.target_root / "docs" / "Cafe\u0301.py"
        existing.parent.mkdir()
        existing.write_bytes(b"consumer-owned NFD alias\n")
        object_id = self._git_with_input(
            self.target_root,
            existing.read_bytes(),
            "hash-object",
            "-w",
            "--stdin",
        ).strip()
        self._git_with_input(
            self.target_root,
            b"100644 " + object_id + b" 0\tdocs/Cafe\xcc\x81.py\0",
            "update-index",
            "-z",
            "--index-info",
        )
        self._git(
            self.target_root,
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "add consumer NFD alias",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "tracked path collides.*NFC\\+casefold",
        ):
            self._generate()

        self.assertEqual(existing.read_bytes(), b"consumer-owned NFD alias\n")

    def test_generate_rejects_tracked_ancestor_of_new_target(self) -> None:
        existing = self.target_root / "scripts"
        existing.write_bytes(b"consumer-owned ancestor\n")
        self._commit(self.target_root, "add consumer ancestor")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "tracked path collides.*managed/recovery",
        ):
            self._generate()

        self.assertEqual(existing.read_bytes(), b"consumer-owned ancestor\n")

    def test_generate_rejects_tracked_descendant_of_new_target(self) -> None:
        existing = self.target_root / "scripts" / "engine.py" / "child.txt"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"consumer-owned descendant\n")
        self._commit(self.target_root, "add consumer descendant")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "tracked path collides.*managed/recovery",
        ):
            self._generate()

        self.assertEqual(existing.read_bytes(), b"consumer-owned descendant\n")

    def test_generate_allows_the_exact_existing_managed_target(self) -> None:
        target = self.target_root / "scripts" / "engine.py"
        target.parent.mkdir()
        target.write_bytes(b"tracked prior managed content\n")
        sibling = target.with_name("consumer-helper.py")
        sibling.write_bytes(b"tracked consumer sibling\n")
        self._commit(self.target_root, "track exact managed target")

        self.assertEqual(self._generate(), 1)
        self.assertEqual(target.read_bytes(), self.source_path.read_bytes())
        self.assertEqual(sibling.read_bytes(), b"tracked consumer sibling\n")

    def test_generate_rejects_unmerged_consumer_index_entries(self) -> None:
        object_id = self._git_with_input(
            self.target_root,
            b"conflict fixture\n",
            "hash-object",
            "-w",
            "--stdin",
        ).strip()
        conflict_records = b"".join(
            b"100644 "
            + object_id
            + b" "
            + str(stage).encode("ascii")
            + b"\tconflict.txt\0"
            for stage in (1, 2, 3)
        )
        self._git_with_input(
            self.target_root,
            conflict_records,
            "update-index",
            "-z",
            "--index-info",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "unmerged/non-stage-0",
        ):
            self._generate()

    def test_generate_rejects_complete_index_head_disagreement(self) -> None:
        readme = self.target_root / "README.md"
        readme.write_text("# Staged consumer change\n", encoding="utf-8")
        self._git(self.target_root, "add", "--", "README.md")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "complete stage-0 index differs from HEAD",
        ):
            self._generate()

    def test_generate_rejects_invalid_raw_consumer_path_bytes(self) -> None:
        object_id = self._git_with_input(
            self.target_root,
            b"invalid path fixture\n",
            "hash-object",
            "-w",
            "--stdin",
        ).strip()
        self._git_with_input(
            self.target_root,
            b"100644 " + object_id + b" 0\tinvalid-\xff.txt\0",
            "update-index",
            "-z",
            "--index-info",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "tracked path is not valid UTF-8",
        ):
            self._generate()

    def test_generate_rejects_consumer_tracked_path_cap_exhaustion(self) -> None:
        (self.target_root / "SECOND.md").write_text(
            "Second tracked path.\n",
            encoding="utf-8",
        )
        self._commit(self.target_root, "add second consumer path")

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "MAX_CONSUMER_TRACKED_ENTRIES",
                1,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "tracked-path inventory exceeds the 1-entry limit",
            ),
        ):
            self._generate()

    def test_generate_rejects_nonmanaged_index_drift_before_write(self) -> None:
        real_require_index = MIRROR_MODULE._require_same_target_index
        checks = 0

        def inject_index_drift(
            target_root,
            mirror,
            expected,
            additional_paths=frozenset(),
        ):
            nonlocal checks
            checks += 1
            if checks == 2:
                readme = self.target_root / "README.md"
                readme.write_text("# Racing staged change\n", encoding="utf-8")
                self._git(self.target_root, "add", "--", "README.md")
            return real_require_index(
                target_root,
                mirror,
                expected,
                additional_paths,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_target_index",
                side_effect=inject_index_drift,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "complete stage-0 index differs from HEAD|"
                "Git index control file was replaced",
            ),
        ):
            self._generate()

        self.assertFalse((self.target_root / "scripts" / "engine.py").exists())

    def test_generate_rejects_consumer_head_drift_before_write(self) -> None:
        real_require_index = MIRROR_MODULE._require_same_target_index
        checks = 0

        def inject_head_drift(
            target_root,
            mirror,
            expected,
            additional_paths=frozenset(),
        ):
            nonlocal checks
            checks += 1
            if checks == 2:
                self._git(
                    self.target_root,
                    "commit",
                    "--allow-empty",
                    "--no-gpg-sign",
                    "-q",
                    "-m",
                    "racing target head",
                )
            return real_require_index(
                target_root,
                mirror,
                expected,
                additional_paths,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_target_index",
                side_effect=inject_head_drift,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "HEAD|Git .*control|tracked namespace|stage-0 index changed",
            ),
        ):
            self._generate()

        self.assertFalse((self.target_root / "scripts" / "engine.py").exists())

    def test_check_rejects_missing_and_tampered_receipts(self) -> None:
        self._generate()
        receipt_path = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()
        original_receipt = receipt_path.read_bytes()
        receipt_path.unlink()

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "receipt is missing",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        receipt_path.write_bytes(original_receipt)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["tree_digest"] = "0" * 64
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "stale or tampered",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

    def test_check_revalidates_the_whole_consumer_group_at_completion(
        self,
    ) -> None:
        self._generate()
        real_require_group = MIRROR_MODULE._require_same_file_group
        target = self.target_root / "scripts" / "engine.py"

        def mutate_before_group_check(root, expected, group_name):
            if group_name == "consumer mirror group":
                target.write_bytes(b"late check drift\n")
            return real_require_group(root, expected, group_name)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_require_same_file_group",
                side_effect=mutate_before_group_check,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "consumer mirror group changed",
            ),
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

    def test_check_rejects_staged_add_modify_and_delete(self) -> None:
        self._generate()
        target = self.target_root / "scripts" / "engine.py"
        receipt = self.target_root / MIRROR_MODULE.RECEIPT_PATH.as_posix()

        self._git(self.target_root, "add", "--", "scripts/engine.py")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "stage-0 index differs from HEAD",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        self._git(
            self.target_root,
            "restore",
            "--staged",
            "--",
            "scripts/engine.py",
        )

        self._commit(self.target_root, "track generated mirror")
        target.write_bytes(b"staged target drift\n")
        self._git(self.target_root, "add", "--", "scripts/engine.py")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "stage-0 index differs from HEAD",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        self._git(
            self.target_root,
            "restore",
            "--staged",
            "--worktree",
            "--",
            "scripts/engine.py",
        )

        self._git(
            self.target_root,
            "rm",
            "--cached",
            "--",
            receipt.name,
        )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "stage-0 index differs from HEAD",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

    def test_check_rejects_a_stale_receipt_after_new_canonical_commit(self) -> None:
        self._generate()
        self.source_path.write_bytes(b"next canonical engine\n")
        self.source_path.chmod(0o755)
        MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=False)
        self.source_commit = self._commit(
            self.canonical_root,
            "change canonical engine",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "receipt is stale",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

    def test_check_rejects_mapping_and_rules_contract_drift(self) -> None:
        self._generate()
        source_lock = MIRROR_MODULE.load_source_lock(self.canonical_root)
        drifted_mirrors = dict(source_lock.mirrors)
        drifted_mirrors["toolbox"] = MIRROR_MODULE.MirrorSpec(
            name="toolbox",
            repository="Joey-Tools/toolbox-fixture",
            files={
                "engine": MIRROR_MODULE.PurePosixPath("lib/engine.py"),
            },
        )
        drifted_lock = MIRROR_MODULE.SourceLock(
            canonical_repository=source_lock.canonical_repository,
            sources=source_lock.sources,
            mirrors=drifted_mirrors,
        )
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "load_source_lock",
                return_value=drifted_lock,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "mapping/rules/source tree",
            ),
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "RULES_CONTRACT_VERSION",
                MIRROR_MODULE.RULES_CONTRACT_VERSION + 1,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "mapping/rules/source tree",
            ),
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )

    def test_check_rejects_wrong_target_repository_and_mirror(self) -> None:
        self._generate()
        self._git(
            self.target_root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/Joey-Tools/private-fixture.git",
        )

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "target repository does not match",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "toolbox",
            )
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "receipt names the wrong",
        ):
            MIRROR_MODULE.check_mirror(
                self.canonical_root,
                self.target_root,
                "private",
            )

    def test_generate_refuses_a_dirty_locked_source(self) -> None:
        target = self.target_root / "scripts" / "engine.py"
        target.parent.mkdir()
        target.write_bytes(b"keep me\n")
        self.source_path.write_bytes(b"unlocked canonical change\n")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "dirty or untracked",
        ):
            self._generate()

        self.assertEqual(target.read_bytes(), b"keep me\n")

    def test_generate_uses_raw_bytes_despite_a_custom_clean_filter(self) -> None:
        attributes = self.canonical_root / ".gitattributes"
        attributes.write_text(
            "scripts/engine.py filter=fixture_mutator\n",
            encoding="utf-8",
        )
        self._git(
            self.canonical_root,
            "config",
            "filter.fixture_mutator.clean",
            "/usr/bin/sed s/canonical/filtered/",
        )
        self._git(self.canonical_root, "add", "--", ".gitattributes")
        self._git(
            self.canonical_root,
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "add custom clean filter",
        )
        self.source_commit = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "HEAD",
            )
            .decode("ascii")
            .strip()
        )

        filtered_object = (
            self._git(
                self.canonical_root,
                "hash-object",
                "--path",
                "scripts/engine.py",
                "--",
                "scripts/engine.py",
            )
            .decode("ascii")
            .strip()
        )
        committed_object = (
            self._git(
                self.canonical_root,
                "rev-parse",
                "HEAD:scripts/engine.py",
            )
            .decode("ascii")
            .strip()
        )
        self.assertNotEqual(filtered_object, committed_object)
        self.assertEqual(self._generate(), 1)
        self.assertEqual(
            (self.target_root / "scripts" / "engine.py").read_bytes(),
            b"canonical engine\n",
        )

    def test_safe_read_tolerates_mtime_only_churn(self) -> None:
        real_read_all = MIRROR_MODULE._read_all
        read_count = 0

        def read_then_touch(file_descriptor, display_path):
            nonlocal read_count
            payload = real_read_all(file_descriptor, display_path)
            read_count += 1
            if read_count == 1:
                metadata = self.source_path.stat()
                os.utime(
                    self.source_path,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            return payload

        with mock.patch.object(
            MIRROR_MODULE,
            "_read_all",
            side_effect=read_then_touch,
        ):
            payload, mode = MIRROR_MODULE._safe_read_relative(
                self.canonical_root,
                MIRROR_MODULE.PurePosixPath("scripts/engine.py"),
            )

        self.assertEqual(payload, b"canonical engine\n")
        self.assertEqual(mode, 0o755)

    def test_safe_read_rejects_same_size_content_drift(self) -> None:
        real_read_all = MIRROR_MODULE._read_all
        read_count = 0

        def read_then_mutate(file_descriptor, display_path):
            nonlocal read_count
            payload = real_read_all(file_descriptor, display_path)
            read_count += 1
            if read_count == 1:
                self.source_path.write_bytes(b"x" * len(payload))
                self.source_path.chmod(0o755)
            return payload

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_read_all",
                side_effect=read_then_mutate,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "content changed",
            ),
        ):
            MIRROR_MODULE._safe_read_relative(
                self.canonical_root,
                MIRROR_MODULE.PurePosixPath("scripts/engine.py"),
            )

    def test_safe_read_rejects_path_replacement(self) -> None:
        real_read_all = MIRROR_MODULE._read_all
        read_count = 0

        def read_then_replace(file_descriptor, display_path):
            nonlocal read_count
            payload = real_read_all(file_descriptor, display_path)
            read_count += 1
            if read_count == 1:
                replacement = self.source_path.with_name("replacement.py")
                replacement.write_bytes(payload)
                replacement.chmod(0o755)
                os.replace(replacement, self.source_path)
            return payload

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_read_all",
                side_effect=read_then_replace,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "replaced",
            ),
        ):
            MIRROR_MODULE._safe_read_relative(
                self.canonical_root,
                MIRROR_MODULE.PurePosixPath("scripts/engine.py"),
            )

    def test_managed_ancestor_binding_distinguishes_churn_policy_and_identity(
        self,
    ) -> None:
        scripts_path = self.target_root / "scripts"
        scripts_path.mkdir()
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        operation_path = MIRROR_MODULE.PurePosixPath("scripts/engine.py")
        try:
            MIRROR_MODULE._configure_managed_ancestors(
                bound_root,
                [operation_path],
            )
            churn = scripts_path / "benign-child"
            churn.write_bytes(b"temporary\n")
            churn.unlink()
            MIRROR_MODULE._revalidate_bound_root(bound_root)

            scripts_path.chmod(0o700)
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "managed ancestor.*access policy changed",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            scripts_path.chmod(0o755)

            saved = self.target_root / "scripts.saved"
            os.rename(scripts_path, saved)
            scripts_path.mkdir()
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "managed ancestor.*replaced",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            scripts_path.rmdir()
            os.rename(saved, scripts_path)
        finally:
            if scripts_path.exists():
                scripts_path.chmod(0o755)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_created_managed_ancestor_is_bound_before_publication(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        operation_path = MIRROR_MODULE.PurePosixPath("created/engine.py")
        saved = self.target_root / "created.saved"
        created = self.target_root / "created"
        try:
            MIRROR_MODULE._configure_managed_ancestors(
                bound_root,
                [operation_path],
            )
            parent_fd = MIRROR_MODULE._open_parent_directory(
                bound_root.fd,
                operation_path,
                create=True,
                bound_root=bound_root,
            )
            os.close(parent_fd)
            created.chmod(0o700)
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "managed ancestor.*access policy changed",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            created.chmod(0o755)
            os.rename(created, saved)
            created.mkdir()
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "managed ancestor.*replaced",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            created.rmdir()
            os.rename(saved, created)
        finally:
            if saved.exists():
                if created.exists():
                    created.rmdir()
                os.rename(saved, created)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_bound_root_rejects_access_policy_change(self) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        original_mode = stat.S_IMODE(self.target_root.stat().st_mode)
        changed_mode = 0o700 if original_mode != 0o700 else 0o755
        try:
            self.target_root.chmod(changed_mode)
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "root access policy changed",
            ):
                MIRROR_MODULE._revalidate_bound_root(bound_root)
            self.target_root.chmod(original_mode)
        finally:
            self.target_root.chmod(original_mode)
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_check_and_generate_reject_canonical_target_roots(self) -> None:
        nested_target = self.canonical_root / "consumer"
        nested_target.mkdir()

        for command, target_root in (
            ("check", self.canonical_root),
            ("generate", self.canonical_root),
            ("check", nested_target),
            ("generate", nested_target),
            ("check", self.root),
            ("generate", self.root),
        ):
            with (
                self.subTest(
                    command=command,
                    target_root=target_root,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "must not be the canonical repository",
                ),
            ):
                if command == "check":
                    MIRROR_MODULE.check_mirror(
                        self.canonical_root,
                        target_root,
                        "toolbox",
                    )
                else:
                    MIRROR_MODULE.generate_mirror(
                        self.canonical_root,
                        target_root,
                        "toolbox",
                        self.source_commit,
                    )

    def test_refresh_lock_updates_only_canonical_source_metadata(self) -> None:
        self.source_path.write_bytes(b"new canonical bytes\n")
        target = self.target_root / "scripts" / "engine.py"
        target.parent.mkdir()
        target.write_bytes(b"consumer bytes must not be imported\n")

        refreshed = MIRROR_MODULE.refresh_source_lock(
            self.canonical_root,
            check=False,
        )

        self.assertEqual(refreshed, 1)
        MIRROR_MODULE.refresh_source_lock(self.canonical_root, check=True)
        lock = json.loads(
            (self.canonical_root / "sync-source-lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            lock["sources"]["engine"]["sha256"],
            hashlib.sha256(b"new canonical bytes\n").hexdigest(),
        )
        self.assertEqual(target.read_bytes(), b"consumer bytes must not be imported\n")

    def test_refresh_lock_rejects_a_lost_update_to_the_initial_lock(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        self.source_path.write_bytes(b"new canonical bytes\n")
        injected_payload = lock_path.read_bytes() + b" "
        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        injected = False

        def inject_lock_update(root, relative_path, payload, mode, **kwargs):
            nonlocal injected
            if relative_path == MIRROR_MODULE.LOCK_PATH and not injected:
                injected = True
                lock_path.write_bytes(injected_payload)
            return real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=inject_lock_update,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "conditional target changed",
            ),
        ):
            MIRROR_MODULE.refresh_source_lock(
                self.canonical_root,
                check=False,
            )

        self.assertEqual(lock_path.read_bytes(), injected_payload)

    def test_refresh_lock_rejects_initial_lock_identity_replacement(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        self.source_path.write_bytes(b"new canonical bytes\n")
        original_payload = lock_path.read_bytes()
        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        injected = False

        def replace_lock_identity(root, relative_path, payload, mode, **kwargs):
            nonlocal injected
            if relative_path == MIRROR_MODULE.LOCK_PATH and not injected:
                injected = True
                replacement = lock_path.with_name("replacement-lock.json")
                replacement.write_bytes(original_payload)
                replacement.chmod(0o644)
                os.replace(replacement, lock_path)
            return real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=replace_lock_identity,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "conditional target changed",
            ),
        ):
            MIRROR_MODULE.refresh_source_lock(
                self.canonical_root,
                check=False,
            )

        self.assertEqual(lock_path.read_bytes(), original_payload)

    def test_refresh_lock_rejects_initial_lock_policy_change(self) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        self.source_path.write_bytes(b"new canonical bytes\n")
        real_atomic_write = MIRROR_MODULE._atomic_write_relative
        injected = False

        def change_lock_mode(root, relative_path, payload, mode, **kwargs):
            nonlocal injected
            if relative_path == MIRROR_MODULE.LOCK_PATH and not injected:
                injected = True
                lock_path.chmod(0o600)
            return real_atomic_write(
                root,
                relative_path,
                payload,
                mode,
                **kwargs,
            )

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_atomic_write_relative",
                side_effect=change_lock_mode,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "conditional target changed",
            ),
        ):
            MIRROR_MODULE.refresh_source_lock(
                self.canonical_root,
                check=False,
            )

        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_refresh_lock_rolls_back_when_source_group_changes_after_publish(
        self,
    ) -> None:
        lock_path = self.canonical_root / MIRROR_MODULE.LOCK_PATH.as_posix()
        original_lock = lock_path.read_bytes()
        self.source_path.write_bytes(b"new canonical bytes\n")
        real_snapshots = MIRROR_MODULE._source_group_snapshots
        snapshot_count = 0

        def mutate_before_final_snapshot(root, source_lock):
            nonlocal snapshot_count
            snapshot_count += 1
            if snapshot_count == 3:
                self.source_path.write_bytes(b"raced canonical bytes\n")
                self.source_path.chmod(0o755)
            return real_snapshots(root, source_lock)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_source_group_snapshots",
                side_effect=mutate_before_final_snapshot,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "source group changed",
            ),
        ):
            MIRROR_MODULE.refresh_source_lock(
                self.canonical_root,
                check=False,
            )

        self.assertEqual(lock_path.read_bytes(), original_lock)

    def test_refresh_lock_detects_root_replacement_before_completion(self) -> None:
        real_snapshots = MIRROR_MODULE._source_group_snapshots
        snapshot_count = 0

        def replace_root_before_final_snapshot(root, source_lock):
            nonlocal snapshot_count
            snapshot_count += 1
            if snapshot_count == 3:
                moved_root = self.root / "canonical-moved"
                os.rename(self.canonical_root, moved_root)
                self.canonical_root.mkdir()
            return real_snapshots(root, source_lock)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_source_group_snapshots",
                side_effect=replace_root_before_final_snapshot,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "root was replaced",
            ),
        ):
            MIRROR_MODULE.refresh_source_lock(
                self.canonical_root,
                check=False,
            )

    def test_atomic_failure_preserves_existing_target_and_cleans_temporary_file(
        self,
    ) -> None:
        target = self.target_root / "scripts" / "engine.py"
        target.parent.mkdir()
        target.write_bytes(b"existing target\n")
        self._commit(self.target_root, "track existing target")

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_exchange_directory_entries",
                side_effect=OSError("injected replace failure"),
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "atomically generate",
            ),
        ):
            self._generate()

        self.assertEqual(target.read_bytes(), b"existing target\n")
        self.assertEqual(
            sorted(path.name for path in target.parent.iterdir()),
            ["engine.py"],
        )
        self.assertEqual(
            [path for path in self.target_root.rglob("*") if ".tmp-" in path.name],
            [],
        )

    def test_atomic_absent_publication_never_clobbers_a_racing_creator(
        self,
    ) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("raced-absent.txt")
        target = self.target_root / target_path.as_posix()
        real_link = MIRROR_MODULE.os.link
        raced = False

        def create_before_link(source, destination, **kwargs):
            nonlocal raced
            if destination == target_path.name and not raced:
                raced = True
                target.write_bytes(b"concurrent creator\n")
            return real_link(source, destination, **kwargs)

        with (
            mock.patch.object(
                MIRROR_MODULE.os,
                "link",
                side_effect=create_before_link,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "no-clobber publication",
            ),
        ):
            MIRROR_MODULE._atomic_write_relative(
                self.target_root,
                target_path,
                b"generated bytes\n",
                0o644,
                create_parents=False,
                expected_absent=True,
            )

        self.assertEqual(target.read_bytes(), b"concurrent creator\n")

    def test_atomic_present_exchange_restores_a_racing_editor(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("raced-present.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"original bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        real_exchange = MIRROR_MODULE._exchange_directory_entries
        raced = False

        def edit_before_exchange(directory_fd, left_name, right_name):
            nonlocal raced
            if not raced:
                raced = True
                target.write_bytes(b"concurrent editor\n")
            return real_exchange(directory_fd, left_name, right_name)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_exchange_directory_entries",
                side_effect=edit_before_exchange,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "conditional target or replacement changed",
            ),
        ):
            MIRROR_MODULE._atomic_write_relative(
                self.target_root,
                target_path,
                b"generated bytes\n",
                0o644,
                create_parents=False,
                expected_snapshot=expected,
            )

        self.assertEqual(target.read_bytes(), b"concurrent editor\n")

    def test_exchange_journal_recovers_present_and_absent_crash_states(
        self,
    ) -> None:
        present_path = MIRROR_MODULE.PurePosixPath("present-crash.txt")
        present_target = self.target_root / present_path.as_posix()
        present_target.write_bytes(b"user bytes\n")
        present_expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            present_path,
        )
        present_temp_name = f".{present_path.name}.tmp-{os.getpid()}-0123456789abcdef"
        present_temp = self.target_root / present_temp_name
        present_temp.write_bytes(b"replacement bytes\n")
        present_replacement = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            MIRROR_MODULE.PurePosixPath(present_temp_name),
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        try:
            MIRROR_MODULE._write_exchange_journal(
                root_fd,
                present_path,
                present_temp_name,
                present_expected,
                present_replacement,
            )
            MIRROR_MODULE._exchange_directory_entries(
                root_fd,
                present_temp_name,
                present_path.name,
            )
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

        MIRROR_MODULE._recover_exchange_journal(
            self.target_root,
            present_path,
        )
        self.assertEqual(present_target.read_bytes(), b"user bytes\n")
        self.assertFalse(present_temp.exists())

        absent_path = MIRROR_MODULE.PurePosixPath("absent-crash.txt")
        absent_temp_name = f".{absent_path.name}.tmp-{os.getpid()}-fedcba9876543210"
        absent_temp = self.target_root / absent_temp_name
        absent_temp.write_bytes(b"new bytes\n")
        absent_replacement = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            MIRROR_MODULE.PurePosixPath(absent_temp_name),
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        try:
            MIRROR_MODULE._write_exchange_journal(
                root_fd,
                absent_path,
                absent_temp_name,
                None,
                absent_replacement,
            )
            os.link(
                absent_temp_name,
                absent_path.name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

        MIRROR_MODULE._recover_exchange_journal(
            self.target_root,
            absent_path,
        )
        self.assertEqual(
            (self.target_root / absent_path.as_posix()).read_bytes(),
            b"new bytes\n",
        )
        self.assertFalse(absent_temp.exists())

    def test_exchange_journal_recovers_retired_target_removal_states(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-crash.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"receipt-bound bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        temporary_name = f".{target_path.name}.tmp-{os.getpid()}-0123456789abcdef"
        quarantine_name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        try:
            MIRROR_MODULE._write_exchange_journal(
                root_fd,
                target_path,
                temporary_name,
                expected,
                None,
                removal_quarantine_name=quarantine_name,
            )
            MIRROR_MODULE._rename_directory_entry_noreplace(
                root_fd,
                target_path.name,
                temporary_name,
            )
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

        MIRROR_MODULE._recover_exchange_journal(
            self.target_root,
            target_path,
        )
        self.assertEqual(target.read_bytes(), expected.payload)
        self.assertFalse((self.target_root / temporary_name).exists())

        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        quarantine_name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        try:
            MIRROR_MODULE._write_exchange_journal(
                root_fd,
                target_path,
                temporary_name,
                expected,
                None,
                removal_quarantine_name=quarantine_name,
            )
            MIRROR_MODULE._rename_directory_entry_noreplace(
                root_fd,
                target_path.name,
                temporary_name,
            )
            os.unlink(temporary_name, dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "no exact durable quarantine evidence",
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )
        self.assertFalse(target.exists())
        self.assertTrue(
            (
                self.target_root / MIRROR_MODULE._exchange_journal_name(target_path)
            ).exists()
        )

    def test_exchange_journal_accepts_exact_durable_removal_evidence(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-durable-crash.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"receipt-bound durable bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        temporary_name = f".{target_path.name}.tmp-{os.getpid()}-abcdef0123456789"
        quarantine_name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        quarantine = None
        try:
            quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                self.target_root,
                source_parent_fd=root_fd,
            )
            MIRROR_MODULE._write_exchange_journal(
                root_fd,
                target_path,
                temporary_name,
                expected,
                None,
                removal_quarantine_name=quarantine_name,
            )
            MIRROR_MODULE._rename_directory_entry_noreplace(
                root_fd,
                target_path.name,
                temporary_name,
            )
            MIRROR_MODULE._persist_quarantined_file(
                quarantine,
                root_fd,
                temporary_name,
                expected,
                MIRROR_MODULE.PurePosixPath(temporary_name),
                retention_kind=MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
                quarantine_name=quarantine_name,
            )
        finally:
            if quarantine is not None:
                os.close(quarantine.fd)
            os.close(root_fd)

        MIRROR_MODULE._recover_exchange_journal(
            self.target_root,
            target_path,
        )
        self.assertFalse(target.exists())
        self.assertFalse(
            (
                self.target_root / MIRROR_MODULE._exchange_journal_name(target_path)
            ).exists()
        )
        quarantine_path = (
            self.target_root.parent
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            / quarantine_name
        )
        self.assertEqual(quarantine_path.read_bytes(), expected.payload)

    def test_exchange_recovery_rechecks_evidence_before_journal_cleanup(
        self,
    ) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-evidence-race.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"receipt-bound race bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        temporary_name = f".{target_path.name}.tmp-{os.getpid()}-1234567890abcdef"
        quarantine_name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        quarantine = None
        try:
            quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                self.target_root,
                source_parent_fd=root_fd,
            )
            journal_name, journal_snapshot = MIRROR_MODULE._write_exchange_journal(
                root_fd,
                target_path,
                temporary_name,
                expected,
                None,
                removal_quarantine_name=quarantine_name,
            )
            MIRROR_MODULE._rename_directory_entry_noreplace(
                root_fd,
                target_path.name,
                temporary_name,
            )
            MIRROR_MODULE._persist_quarantined_file(
                quarantine,
                root_fd,
                temporary_name,
                expected,
                MIRROR_MODULE.PurePosixPath(temporary_name),
                retention_kind=MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
                quarantine_name=quarantine_name,
            )
        finally:
            if quarantine is not None:
                os.close(quarantine.fd)
            os.close(root_fd)

        quarantine_root = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        evidence_path = quarantine_root / quarantine_name
        saved_evidence_path = quarantine_root / "saved-racing-removal-evidence"
        journal_path = self.target_root / journal_name
        real_safe_read_leaf = MIRROR_MODULE._safe_read_leaf_snapshot
        evidence_reads = 0

        def remove_evidence_before_revalidation(
            parent_fd,
            name,
            display_path,
        ):
            nonlocal evidence_reads
            if name == quarantine_name:
                evidence_reads += 1
                if evidence_reads == 2:
                    evidence_path.rename(saved_evidence_path)
            return real_safe_read_leaf(parent_fd, name, display_path)

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_safe_read_leaf_snapshot",
                side_effect=remove_evidence_before_revalidation,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "missing immediately before journal cleanup",
            ),
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )

        self.assertEqual(evidence_reads, 2)
        self.assertEqual(journal_path.read_bytes(), journal_snapshot.payload)
        self.assertEqual(saved_evidence_path.read_bytes(), expected.payload)
        self.assertFalse(target.exists())

    def test_atomic_remove_rechecks_evidence_during_journal_cleanup(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-live-race.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"receipt-bound live race bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        journal_name = MIRROR_MODULE._exchange_journal_name(target_path)
        quarantine_root = (
            self.target_root.parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        real_isolate = MIRROR_MODULE._isolate_and_remove_file
        retained_journal_path = None
        saved_evidence_path = quarantine_root / "saved-live-removal-evidence"

        def replace_evidence_during_journal_cleanup(*args, **kwargs):
            nonlocal retained_journal_path
            retained_path = real_isolate(*args, **kwargs)
            if args[2] == journal_name:
                evidence_paths = sorted(
                    quarantine_root.glob(
                        f"{MIRROR_MODULE.QUARANTINE_RECOVERY_KIND}-file-*"
                    )
                )
                self.assertEqual(len(evidence_paths), 1)
                evidence_paths[0].rename(saved_evidence_path)
                retained_journal_path = retained_path
            return retained_path

        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_isolate_and_remove_file",
                side_effect=replace_evidence_during_journal_cleanup,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "recovery journal retained at",
            ),
        ):
            MIRROR_MODULE._atomic_remove_relative(
                self.target_root,
                target_path,
                expected,
            )

        self.assertFalse(target.exists())
        self.assertFalse((self.target_root / journal_name).exists())
        self.assertIsNotNone(retained_journal_path)
        assert retained_journal_path is not None
        retained_document = json.loads(
            retained_journal_path.read_text(encoding="utf-8")
        )
        self.assertEqual(retained_document["version"], 2)
        self.assertEqual(retained_document["target_path"], target_path.as_posix())
        self.assertEqual(saved_evidence_path.read_bytes(), expected.payload)

    def test_exchange_recovery_at_exact_capacity_preserves_active_journal(
        self,
    ) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-capacity-crash.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"receipt-bound capacity bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        temporary_name = f".{target_path.name}.tmp-{os.getpid()}-abcdef1234567890"
        quarantine_name = MIRROR_MODULE._quarantine_file_name(
            expected,
            MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
        )
        root_fd = os.open(self.target_root, os.O_RDONLY)
        quarantine = None
        try:
            quarantine = MIRROR_MODULE._bind_durable_quarantine_root(
                self.target_root,
                source_parent_fd=root_fd,
            )
            journal_name, journal_snapshot = MIRROR_MODULE._write_exchange_journal(
                root_fd,
                target_path,
                temporary_name,
                expected,
                None,
                removal_quarantine_name=quarantine_name,
            )
            MIRROR_MODULE._rename_directory_entry_noreplace(
                root_fd,
                target_path.name,
                temporary_name,
            )
            MIRROR_MODULE._persist_quarantined_file(
                quarantine,
                root_fd,
                temporary_name,
                expected,
                MIRROR_MODULE.PurePosixPath(temporary_name),
                retention_kind=MIRROR_MODULE.QUARANTINE_RECOVERY_KIND,
                quarantine_name=quarantine_name,
            )
        finally:
            if quarantine is not None:
                os.close(quarantine.fd)
            os.close(root_fd)

        journal_path = self.target_root / journal_name
        evidence_path = (
            self.target_root.parent
            / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
            / quarantine_name
        )
        with (
            mock.patch.object(
                MIRROR_MODULE,
                "_durable_quarantine_entry_count",
                return_value=MIRROR_MODULE.MAX_DURABLE_QUARANTINE_ENTRIES,
            ),
            self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "reached its bounded entry limit",
            ),
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )

        self.assertEqual(journal_path.read_bytes(), journal_snapshot.payload)
        self.assertEqual(evidence_path.read_bytes(), expected.payload)
        self.assertFalse(target.exists())
        self.assertFalse((self.target_root / temporary_name).exists())

    def test_legacy_removal_journal_double_missing_is_ambiguous(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("retired-legacy-crash.txt")
        target = self.target_root / target_path.as_posix()
        target.write_bytes(b"legacy receipt-bound bytes\n")
        expected = MIRROR_MODULE._safe_read_snapshot(
            self.target_root,
            target_path,
        )
        temporary_name = f".{target_path.name}.tmp-{os.getpid()}-fedcba9876543210"
        journal_path = self.target_root / MIRROR_MODULE._exchange_journal_name(
            target_path
        )
        journal_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "target_path": target_path.as_posix(),
                    "temporary_name": temporary_name,
                    "expected": MIRROR_MODULE._snapshot_record(expected),
                    "replacement": None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        journal_path.chmod(0o600)
        os.rename(target, self.target_root / temporary_name)
        os.unlink(self.target_root / temporary_name)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "no exact durable quarantine evidence",
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )
        self.assertTrue(journal_path.exists())

    def test_exchange_recovery_rejects_duplicate_journal_keys(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("duplicate-journal.txt")
        journal_path = self.target_root / MIRROR_MODULE._exchange_journal_name(
            target_path
        )
        journal_path.write_text(
            '{"version":1,"version":2}\n',
            encoding="utf-8",
        )
        journal_path.chmod(0o600)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "exchange recovery journal is invalid",
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )

        self.assertTrue(journal_path.exists())

    def test_exchange_recovery_rejects_duplicate_nested_removal_keys(self) -> None:
        target_path = MIRROR_MODULE.PurePosixPath("duplicate-removal-journal.txt")
        journal_path = self.target_root / MIRROR_MODULE._exchange_journal_name(
            target_path
        )
        journal_path.write_text(
            (
                '{"version":2,'
                f'"target_path":"{target_path.as_posix()}",'
                '"temporary_name":".duplicate-removal-journal.txt.'
                'tmp-1-0123456789abcdef",'
                '"expected":{},"replacement":null,'
                '"removal_quarantine":{"name":"first","name":"second"}}\n'
            ),
            encoding="utf-8",
        )
        journal_path.chmod(0o600)

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "exchange recovery journal is invalid",
        ):
            MIRROR_MODULE._recover_exchange_journal(
                self.target_root,
                target_path,
            )

        self.assertTrue(journal_path.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_generate_rejects_symlink_ancestor(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.target_root / "scripts").symlink_to(outside, target_is_directory=True)
        self._commit(self.target_root, "track unsafe ancestor")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "ancestor must not be a symlink",
        ):
            self._generate()

        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_generate_rejects_symlink_target(self) -> None:
        outside = self.root / "outside.py"
        outside.write_bytes(b"outside\n")
        target_parent = self.target_root / "scripts"
        target_parent.mkdir()
        (target_parent / "engine.py").symlink_to(outside)
        self._commit(self.target_root, "track unsafe target")

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "target must not be a symlink",
        ):
            self._generate()

        self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_mirror_commands_require_explicit_target_and_mirror(self) -> None:
        parser = MIRROR_MODULE.build_parser()
        for argv in (
            ["check", "--mirror", "toolbox"],
            ["check", "--target-root", str(self.target_root)],
            ["managed-paths", "--mirror", "toolbox"],
            ["managed-paths", "--target-root", str(self.target_root)],
            ["generate", "--mirror", "toolbox"],
            ["generate", "--target-root", str(self.target_root)],
            [
                "generate",
                "--target-root",
                str(self.target_root),
                "--mirror",
                "toolbox",
            ],
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                parser.parse_args(argv)
            self.assertEqual(raised.exception.code, 2)


class RepositorySourceLockTests(unittest.TestCase):
    def test_repository_source_lock_is_current(self) -> None:
        source_lock = MIRROR_MODULE.verify_source_lock(REPOSITORY_ROOT)

        self.assertEqual(
            set(source_lock.sources),
            {
                "engine",
                "engine_tests",
                "manifest_schema",
                "reconciliation_safety_tests",
                "release_retention_tests",
                "scheduler_doctor_tests",
            },
        )
        self.assertEqual(set(source_lock.mirrors), {"toolbox"})
        self.assertEqual(
            set(source_lock.mirrors["toolbox"].files),
            {
                "engine",
                "engine_tests",
                "manifest_schema",
                "reconciliation_safety_tests",
                "release_retention_tests",
                "scheduler_doctor_tests",
            },
        )

    def test_repository_source_lock_has_canonical_serialization(self) -> None:
        source_lock = MIRROR_MODULE.load_source_lock(REPOSITORY_ROOT)
        actual = (REPOSITORY_ROOT / "sync-source-lock.json").read_bytes()

        self.assertEqual(actual, MIRROR_MODULE._source_lock_payload(source_lock))


class PrivateControlRetainedRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="private-control-recovery."
        )
        self.root = Path(os.path.realpath(self.temporary_directory.name))
        self.account_home = self.root / "home"
        self.account_home.mkdir(mode=0o700)
        self.legacy_parent = self.root / "legacy-parent"
        self.legacy_parent.mkdir(mode=0o700)
        self.tool_root = self.legacy_parent / MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME
        self.quarantine = (
            self.legacy_parent / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME
        )
        self.tool_root.mkdir(mode=0o700)
        self.quarantine.mkdir(mode=0o700)
        self.primary_parent = (
            self.account_home / MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME
        )
        self.root_specs = (
            MIRROR_MODULE.PrivateControlRootSpec(
                root_id=MIRROR_MODULE.PRIVATE_CONTROL_PRIMARY_ROOT_ID,
                parent_path=self.primary_parent,
                allocate=True,
                account_home=self.account_home,
                shared_parent=False,
            ),
            MIRROR_MODULE.PrivateControlRootSpec(
                root_id=MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                parent_path=self.legacy_parent,
                allocate=False,
                account_home=None,
                shared_parent=True,
            ),
        )
        self.spec_patch = mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_CONTROL_ROOT_SPECS",
            self.root_specs,
        )
        self.policy_patch = mock.patch.object(
            MIRROR_MODULE,
            "_legacy_shared_parent_policy_is_valid",
            side_effect=lambda access: access == (0o700, os.geteuid(), os.getegid()),
        )
        self.spec_patch.start()
        self.policy_patch.start()
        self.addCleanup(self.spec_patch.stop)
        self.addCleanup(self.policy_patch.stop)
        self._populate_legacy_fixture()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _populate_legacy_fixture(self) -> None:
        for index in range(17):
            private_name = f"sync-canonical-git-control.{1000 + index}.{index:032x}"
            private_path = self.tool_root / private_name
            private_path.mkdir(mode=0o700)
            metadata = os.stat(private_path, follow_symlinks=False)
            owner = {
                "owner_gid": os.getegid(),
                "owner_nonce": f"{index + 100:032x}",
                "owner_pid": 1000 + index,
                "owner_uid": os.geteuid(),
                "phase": "cleanup",
                "private_identity": [
                    metadata.st_dev,
                    metadata.st_ino,
                    stat.S_IFMT(metadata.st_mode),
                ],
                "private_name": private_name,
                "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_VERSION,
            }
            owner_path = self.tool_root / f"{private_name}.owner.json"
            owner_path.write_text(
                json.dumps(owner, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            owner_path.chmod(0o600)
        saved = self.quarantine / ".saved"
        saved.mkdir(mode=0o700)
        nested = saved / "unclassified"
        nested.mkdir(mode=0o700)
        payload = nested / "evidence.bin"
        payload.write_bytes(b"retained evidence\x00\xff")
        payload.chmod(0o600)

    def _snapshot(self) -> tuple[tuple[object, ...], ...]:
        records: list[tuple[object, ...]] = []
        for root, segment in (
            (self.tool_root, "tool-root"),
            (self.quarantine, "quarantine"),
        ):
            pending = [(root, "")]
            while pending:
                directory, parent = pending.pop()
                for entry in sorted(os.scandir(directory), key=lambda item: item.name):
                    relative = entry.name if not parent else f"{parent}/{entry.name}"
                    metadata = entry.stat(follow_symlinks=False)
                    entry_type = stat.S_IFMT(metadata.st_mode)
                    digest = None
                    if stat.S_ISREG(metadata.st_mode):
                        digest = hashlib.sha256(
                            Path(entry.path).read_bytes()
                        ).hexdigest()
                    elif stat.S_ISDIR(metadata.st_mode):
                        pending.append((Path(entry.path), relative))
                    records.append(
                        (
                            segment,
                            relative,
                            metadata.st_dev,
                            metadata.st_ino,
                            entry_type,
                            stat.S_IMODE(metadata.st_mode),
                            metadata.st_uid,
                            metadata.st_gid,
                            metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
                            digest,
                        )
                    )
        return tuple(sorted(records))

    def _plan(self, name: str = "PLAN.json") -> tuple[Path, dict[str, object]]:
        path = self.root / name
        plan = MIRROR_MODULE.plan_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            path,
        )
        return path, plan

    def _engine_root_specs(self) -> tuple[object, object]:
        return (
            ENGINE_MODULE.MirrorPrivateControlRootSpec(
                root_id=ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_PRIMARY_ROOT_ID,
                parent_path=self.primary_parent,
                allocate=True,
                account_home=self.account_home,
                shared_parent=False,
            ),
            ENGINE_MODULE.MirrorPrivateControlRootSpec(
                root_id=ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                parent_path=self.legacy_parent,
                allocate=False,
                account_home=None,
                shared_parent=True,
            ),
        )

    def _recovery_module_scope(self, module: object) -> ExitStack:
        stack = ExitStack()
        if module is ENGINE_MODULE:
            stack.enter_context(
                mock.patch.object(
                    ENGINE_MODULE,
                    "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                    self._engine_root_specs(),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ENGINE_MODULE,
                    "_mirror_legacy_shared_parent_policy_is_valid",
                    side_effect=lambda access: access
                    == (0o700, os.geteuid(), os.getegid()),
                )
            )
        return stack

    def _recovery_module_contract(
        self,
        module: object,
    ) -> tuple[object, str, str, str, type[Exception]]:
        if module is MIRROR_MODULE:
            return (
                self.root_specs[0],
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
                MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                MIRROR_MODULE.MirrorSyncError,
            )
        return (
            self._engine_root_specs()[0],
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            ENGINE_MODULE.SyncError,
        )

    def test_recovery_leaf_fifo_swap_is_nonblocking_and_fail_closed(self) -> None:
        nonblocking_flag = getattr(os, "O_NONBLOCK", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        self.assertNotEqual(nonblocking_flag, 0)

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)

                def assert_fifo_swap_rejected(
                    target_path: Path,
                    replacement_mode: int,
                    invoke: object,
                    error_pattern: str,
                ) -> None:
                    real_open = os.open
                    injected = False

                    def replace_after_stat(
                        path: object,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal injected
                        if (
                            not injected
                            and dir_fd is not None
                            and path == target_path.name
                        ):
                            target_path.unlink()
                            os.mkfifo(target_path, replacement_mode)
                            target_path.chmod(replacement_mode)
                            injected = True
                            if flags & nonblocking_flag != nonblocking_flag:
                                raise AssertionError(
                                    "test refused a potentially blocking FIFO open"
                                )
                            if (
                                nofollow_flag
                                and flags & nofollow_flag != nofollow_flag
                            ):
                                raise AssertionError(
                                    "recovery FIFO open omitted O_NOFOLLOW"
                                )
                        return real_open(path, flags, mode, dir_fd=dir_fd)

                    with mock.patch.object(
                        module.os,
                        "open",
                        side_effect=replace_after_stat,
                    ):
                        with self.assertRaisesRegex(error_type, error_pattern):
                            invoke()
                    self.assertTrue(injected)

                with self._recovery_module_scope(module):
                    plan_path = self.root / f"{module.__name__}-fifo-plan.json"
                    module.plan_private_control_recovery(root_id, plan_path)
                    assert_fifo_swap_rejected(
                        plan_path,
                        0o600,
                        lambda: module._pc_recovery_read_external_plan(
                            plan_path,
                            root_id,
                        ),
                        "changed while binding",
                    )

                for role, payload_limit in (
                    ("evidence", None),
                    (
                        "owner",
                        (
                            module.MAX_PRIVATE_OWNER_RECORD_BYTES
                            if module is MIRROR_MODULE
                            else module.MAX_MIRROR_PRIVATE_OWNER_RECORD_BYTES
                        ),
                    ),
                ):
                    leaf_parent = self.root / f"{module.__name__}-{role}-parent"
                    leaf_parent.mkdir(mode=0o700)
                    leaf_name = (
                        "sync-canonical-git-control.1234."
                        "0123456789abcdef0123456789abcdef.owner.json"
                        if role == "owner"
                        else "evidence.bin"
                    )
                    leaf_path = leaf_parent / leaf_name
                    leaf_path.write_bytes(b"{}\n")
                    leaf_path.chmod(0o600)
                    leaf_metadata = os.stat(leaf_path, follow_symlinks=False)
                    parent_fd = os.open(
                        leaf_parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        assert_fifo_swap_rejected(
                            leaf_path,
                            0o600,
                            lambda: module._pc_recovery_read_file(
                                parent_fd,
                                leaf_name,
                                leaf_path,
                                leaf_metadata,
                                deadline=time.monotonic() + 5,
                                payload_limit=payload_limit,
                            ),
                            "changed while binding",
                        )
                    finally:
                        os.close(parent_fd)

                receipt_parent_path = (
                    self.root / f"{module.__name__}-receipt-parent"
                )
                receipt_parent_path.mkdir(mode=0o700)
                receipt_path = receipt_parent_path / receipt_name
                receipt_path.write_bytes(b"{}\n")
                receipt_path.chmod(0o400)
                receipt_parent = module._pc_recovery_bind_directory(
                    receipt_parent_path,
                    "recovery receipt parent",
                )
                try:
                    assert_fifo_swap_rejected(
                        receipt_path,
                        0o400,
                        lambda: module._pc_recovery_read_bound_file(
                            receipt_parent,
                            receipt_name,
                            "primary recovery receipt",
                        ),
                        "identity/access policy is invalid",
                    )
                finally:
                    module._pc_recovery_close_bindings((receipt_parent,))

    def test_recovery_directory_final_revalidation_wraps_os_errors(self) -> None:
        scenarios = (
            (
                "missing-path",
                "path",
                lambda: FileNotFoundError(
                    errno.ENOENT,
                    "simulated terminal path disappearance",
                ),
                "is missing during final revalidation",
                FileNotFoundError,
            ),
            (
                "unreadable-path",
                "path",
                lambda: PermissionError(
                    errno.EACCES,
                    "simulated terminal path unreadability",
                ),
                "cannot revalidate recovery evidence directory path",
                PermissionError,
            ),
            (
                "failed-descriptor",
                "descriptor",
                lambda: OSError(
                    errno.EIO,
                    "simulated terminal descriptor failure",
                ),
                "cannot revalidate recovery evidence directory descriptor",
                OSError,
            ),
        )
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            (
                _primary_spec,
                root_id,
                _receipt_name,
                _marker_name,
                error_type,
            ) = self._recovery_module_contract(module)
            for scenario, target, error_factory, expected, cause_type in scenarios:
                with self.subTest(module=module.__name__, scenario=scenario):
                    probe = self.quarantine / (
                        f"{module.__name__}-{scenario}-revalidation"
                    )
                    probe.mkdir(mode=0o700)
                    probe_metadata = os.stat(probe, follow_symlinks=False)
                    probe_identity = module._pc_recovery_identity(probe_metadata)
                    real_stat = os.stat
                    real_fstat = os.fstat
                    path_calls = 0
                    descriptor_calls = 0

                    def inject_stat(
                        path: object,
                        *args: object,
                        dir_fd: int | None = None,
                        follow_symlinks: bool = True,
                    ) -> os.stat_result:
                        nonlocal path_calls
                        metadata = real_stat(
                            path,
                            *args,
                            dir_fd=dir_fd,
                            follow_symlinks=follow_symlinks,
                        )
                        if (
                            path == probe.name
                            and dir_fd is not None
                            and not follow_symlinks
                        ):
                            path_calls += 1
                            if target == "path" and path_calls == 2:
                                raise error_factory()
                        return metadata

                    def inject_fstat(file_fd: int) -> os.stat_result:
                        nonlocal descriptor_calls
                        metadata = real_fstat(file_fd)
                        if module._pc_recovery_identity(metadata) == probe_identity:
                            descriptor_calls += 1
                            if target == "descriptor" and descriptor_calls == 2:
                                raise error_factory()
                        return metadata

                    plan_path = self.root / (
                        f"{module.__name__}-{scenario}-revalidation-plan.json"
                    )
                    with (
                        self._recovery_module_scope(module),
                        mock.patch.object(
                            module.os,
                            "stat",
                            side_effect=inject_stat,
                        ),
                        mock.patch.object(
                            module.os,
                            "fstat",
                            side_effect=inject_fstat,
                        ),
                        self.assertRaisesRegex(error_type, expected) as raised,
                    ):
                        module.plan_private_control_recovery(root_id, plan_path)
                    self.assertIsInstance(raised.exception.__cause__, cause_type)
                    self.assertEqual(path_calls, 2)
                    if target == "descriptor":
                        self.assertEqual(descriptor_calls, 2)
                    self.assertFalse(plan_path.exists())
                    probe.rmdir()

    def test_recovery_write_all_times_out_continuous_short_writes(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                target = self.root / f"{module.__name__}-short-write.bin"
                file_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                real_write = os.write
                timeout = (
                    module.PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS
                    if module is MIRROR_MODULE
                    else module.MIRROR_PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS
                )
                try:
                    with (
                        mock.patch.object(
                            module.time,
                            "monotonic",
                            side_effect=(100.0, 100.0, 101.0 + timeout),
                        ),
                        mock.patch.object(
                            module.os,
                            "write",
                            side_effect=lambda fd, payload: real_write(
                                fd,
                                payload[:1],
                            ),
                        ) as write,
                        self.assertRaisesRegex(
                            (
                                MIRROR_MODULE.MirrorSyncError
                                if module is MIRROR_MODULE
                                else ENGINE_MODULE.SyncError
                            ),
                            "timed out while writing synthetic recovery document",
                        ),
                    ):
                        module._pc_recovery_write_all(
                            file_fd,
                            b"ab",
                            label="writing synthetic recovery document",
                        )
                    self.assertEqual(write.call_count, 1)
                finally:
                    os.close(file_fd)
                self.assertEqual(target.read_bytes(), b"a")

    def test_recovery_plan_and_pending_writes_reject_zero_progress(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__), self._recovery_module_scope(
                module
            ):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                seed_path = self.root / f"{module.__name__}-zero-seed.json"
                plan = module.plan_private_control_recovery(root_id, seed_path)
                seed_path.unlink()

                def assert_zero_progress(invoke: object, expected: str) -> None:
                    write_calls = 0

                    def zero_then_fail(*_args: object) -> int:
                        nonlocal write_calls
                        write_calls += 1
                        if write_calls > 1:
                            raise AssertionError(
                                "zero-progress write loop was retried"
                            )
                        return 0

                    with (
                        mock.patch.object(
                            module.os,
                            "write",
                            side_effect=zero_then_fail,
                        ),
                        self.assertRaisesRegex(error_type, expected),
                    ):
                        invoke()
                    self.assertEqual(write_calls, 1)

                plan_path = self.root / f"{module.__name__}-zero-plan.json"
                assert_zero_progress(
                    lambda: module._pc_recovery_write_plan(plan_path, plan, ()),
                    "cannot write recovery plan .*write made no progress",
                )
                self.assertEqual(plan_path.read_bytes(), b"")
                plan_path.unlink()

                self.primary_parent.mkdir(mode=0o700)
                parent = module._pc_recovery_bind_directory(
                    self.primary_parent,
                    "zero-progress pending publication parent",
                )
                module._pc_recovery_acquire_exclusive(parent)
                pending_name = (
                    f".{receipt_name}.pending-{'a' * 64}-{'b' * 32}"
                )
                try:
                    assert_zero_progress(
                        lambda: module._pc_recovery_publish_document(
                            parent,
                            receipt_name,
                            pending_name,
                            "primary recovery receipt",
                            lambda identity: {"identity": list(identity)},
                        ),
                        "cannot write pending primary recovery receipt: "
                        ".*write made no progress",
                    )
                finally:
                    module._pc_recovery_close_bindings((parent,))
                self.assertEqual(
                    (self.primary_parent / pending_name).read_bytes(),
                    b"",
                )
                shutil.rmtree(self.primary_parent)

    def test_recovery_json_limits_match_generator_and_runtime(self) -> None:
        self.assertEqual(
            MIRROR_MODULE.MAX_JSON_INTEGER_DIGITS,
            ENGINE_MODULE.MAX_JSON_INTEGER_DIGITS,
        )
        owner_name = (
            "sync-canonical-git-control.1234."
            "0123456789abcdef0123456789abcdef.owner.json"
        )
        scenarios = (
            (
                "oversized-integer",
                "9" * 65,
                "JSON integer exceeds 64 digits",
            ),
            (
                "nan",
                "NaN",
                "non-standard JSON constant is not allowed: NaN",
            ),
            (
                "positive-infinity",
                "Infinity",
                "non-standard JSON constant is not allowed: Infinity",
            ),
            (
                "negative-infinity",
                "-Infinity",
                "non-standard JSON constant is not allowed: -Infinity",
            ),
        )

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            (
                _primary_spec,
                root_id,
                _receipt_name,
                _marker_name,
                error_type,
            ) = self._recovery_module_contract(module)
            with (
                self.subTest(module=module.__name__),
                self._recovery_module_scope(module),
                mock.patch.object(module, "MAX_JSON_INTEGER_DIGITS", 64),
            ):
                for scenario_name, raw_value, expected_error in scenarios:
                    payload = f'{{"value":{raw_value}}}'.encode("ascii")
                    for loader in ("owner", "plan", "receipt"):
                        with self.subTest(
                            module=module.__name__,
                            scenario=scenario_name,
                            loader=loader,
                        ):
                            with self.assertRaisesRegex(
                                error_type,
                                re.escape(expected_error),
                            ):
                                if loader == "owner":
                                    module._pc_recovery_decode_owner(
                                        owner_name,
                                        payload,
                                        (0o600, os.geteuid(), os.getegid()),
                                    )
                                elif loader == "plan":
                                    plan_path = (
                                        self.root
                                        / f"{module.__name__}-{scenario_name}.json"
                                    )
                                    plan_path.write_bytes(payload)
                                    plan_path.chmod(0o600)
                                    module._pc_recovery_read_external_plan(
                                        plan_path,
                                        root_id,
                                    )
                                else:
                                    module._pc_recovery_load_json(
                                        payload,
                                        "primary recovery receipt",
                                    )

    def test_recovery_json_numeric_types_match_generator_and_runtime(self) -> None:
        def value_at(document: object, path: tuple[object, ...]) -> object:
            current = document
            for component in path:
                current = current[component]
            return current

        def replace_value(
            document: object,
            path: tuple[object, ...],
            value: object,
        ) -> None:
            current = document
            for component in path[:-1]:
                current = current[component]
            current[path[-1]] = value

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            if self.primary_parent.exists():
                shutil.rmtree(self.primary_parent)
            (
                _primary_spec,
                root_id,
                _receipt_name,
                marker_name,
                error_type,
            ) = self._recovery_module_contract(module)
            plan_path = self.root / f"{module.__name__}-numeric-types.json"
            with self.subTest(module=module.__name__), self._recovery_module_scope(
                module
            ):
                plan = module.plan_private_control_recovery(root_id, plan_path)
                module._pc_recovery_validate_plan_document(plan)
                entries = plan["inventory"]["entries"]
                owner_index = next(
                    index
                    for index, entry in enumerate(entries)
                    if entry["owner"] is not None
                )
                plan_cases = (
                    ("version-bool", ("version",), True),
                    (
                        "caps-integer-float",
                        ("caps", "max_entries"),
                        float(plan["caps"]["max_entries"]),
                    ),
                    (
                        "caps-timeout-integer",
                        ("caps", "timeout_seconds"),
                        int(plan["caps"]["timeout_seconds"]),
                    ),
                    ("lease-order-bool", ("leases", 0, "order"), False),
                    (
                        "root-identity-float",
                        ("roots", "parent", "identity", "type"),
                        float(plan["roots"]["parent"]["identity"]["type"]),
                    ),
                    (
                        "entry-access-float",
                        ("inventory", "entries", 0, "access", "uid"),
                        float(entries[0]["access"]["uid"]),
                    ),
                    (
                        "owner-pid-float",
                        (
                            "inventory",
                            "entries",
                            owner_index,
                            "owner",
                            "owner_pid",
                        ),
                        float(entries[owner_index]["owner"]["owner_pid"]),
                    ),
                    (
                        "owner-phase-list",
                        (
                            "inventory",
                            "entries",
                            owner_index,
                            "owner",
                            "phase",
                        ),
                        [entries[owner_index]["owner"]["phase"]],
                    ),
                    (
                        "owner-state-object",
                        (
                            "inventory",
                            "entries",
                            owner_index,
                            "owner",
                            "private_state",
                        ),
                        {
                            "value": entries[owner_index]["owner"][
                                "private_state"
                            ]
                        },
                    ),
                )
                for case_name, path, replacement in plan_cases:
                    with self.subTest(
                        module=module.__name__,
                        document="plan",
                        case=case_name,
                    ):
                        candidate = json.loads(json.dumps(plan))
                        if case_name not in {
                            "owner-phase-list",
                            "owner-state-object",
                        }:
                            self.assertEqual(value_at(candidate, path), replacement)
                        replace_value(candidate, path, replacement)
                        candidate["plan_digest"] = module._pc_recovery_digest(
                            module._pc_recovery_protected_plan(candidate)
                        )
                        with self.assertRaises(error_type):
                            module._pc_recovery_validate_plan_document(candidate)

                module.execute_private_control_recovery(root_id, plan_path)
                marker_path = self.primary_parent / marker_name
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                module._pc_recovery_validate_marker(
                    module._pc_recovery_json_bytes(marker, pretty=True)
                )
                marker_cases = (
                    ("version-bool", ("version",), True),
                    (
                        "receipt-size-float",
                        ("primary_receipt", "size"),
                        float(marker["primary_receipt"]["size"]),
                    ),
                    (
                        "terminal-identity-float",
                        (
                            "terminal_registry",
                            "roots",
                            0,
                            "primary_receipt",
                            "identity",
                            "dev",
                        ),
                        float(
                            marker["terminal_registry"]["roots"][0][
                                "primary_receipt"
                            ]["identity"]["dev"]
                        ),
                    ),
                    (
                        "terminal-inventory-float",
                        (
                            "terminal_registry",
                            "roots",
                            1,
                            "inventory",
                            "entry_count",
                        ),
                        float(
                            marker["terminal_registry"]["roots"][1]["inventory"][
                                "entry_count"
                            ]
                        ),
                    ),
                )
                for case_name, path, replacement in marker_cases:
                    with self.subTest(
                        module=module.__name__,
                        document="marker",
                        case=case_name,
                    ):
                        candidate = json.loads(json.dumps(marker))
                        self.assertEqual(value_at(candidate, path), replacement)
                        replace_value(candidate, path, replacement)
                        terminal = candidate["terminal_registry"]
                        terminal["digest"] = module._pc_recovery_digest(
                            terminal["roots"]
                        )
                        with self.assertRaises(error_type):
                            module._pc_recovery_validate_marker(
                                module._pc_recovery_json_bytes(
                                    candidate,
                                    pretty=True,
                                )
                            )

    def test_deterministic_plan_and_execute_preserve_exact_legacy_evidence(
        self,
    ) -> None:
        before = self._snapshot()
        first_path, first = self._plan("PLAN-1.json")
        second_path, second = self._plan("PLAN-2.json")
        self.assertEqual(first, second)
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        self.assertEqual(before, self._snapshot())
        entries = first["inventory"]["entries"]
        owners = [entry["owner"] for entry in entries if entry["owner"] is not None]
        self.assertEqual(len(owners), 17)
        self.assertTrue(all(owner["private_state"] == "matching" for owner in owners))
        self.assertIn(
            ("quarantine", ".saved/unclassified/evidence.bin"),
            {
                (entry["locator"]["segment"], entry["locator"]["path"])
                for entry in entries
            },
        )

        executed = MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            first_path,
        )
        self.assertEqual(executed["status"], "executed")
        self.assertEqual(
            executed["terminal_whole_registry_revalidation"]["status"],
            "verified",
        )
        self.assertEqual(before, self._snapshot())
        self.assertTrue(
            (
                self.primary_parent
                / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ).is_file()
        )
        self.assertTrue(
            (
                self.primary_parent / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ).is_file()
        )
        retried = MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            first_path,
        )
        self.assertEqual(executed, retried)
        self.assertEqual(before, self._snapshot())

    def test_generator_and_runtime_share_plan_and_execute_contract(self) -> None:
        generator_path, generator_plan = self._plan("generator-PLAN.json")
        engine_path = self.root / "engine-PLAN.json"
        with (
            mock.patch.object(
                ENGINE_MODULE,
                "MIRROR_PRIVATE_CONTROL_ROOT_SPECS",
                self._engine_root_specs(),
            ),
            mock.patch.object(
                ENGINE_MODULE,
                "_mirror_legacy_shared_parent_policy_is_valid",
                side_effect=lambda access: access
                == (0o700, os.geteuid(), os.getegid()),
            ),
        ):
            engine_plan = ENGINE_MODULE.plan_private_control_recovery(
                ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                engine_path,
            )
            self.assertEqual(generator_plan, engine_plan)
            self.assertEqual(generator_path.read_bytes(), engine_path.read_bytes())
            engine_result = ENGINE_MODULE.execute_private_control_recovery(
                ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_ROOT_ID,
                generator_path,
            )
            generator_result = MIRROR_MODULE.execute_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                engine_path,
            )
        self.assertEqual(engine_result, generator_result)
        self.assertEqual(engine_result["status"], "executed")

    def test_recovery_regular_file_reads_are_bounded_streams(self) -> None:
        evidence = self.quarantine / ".saved" / "streaming-evidence.bin"
        payload = (b"0123456789abcdef" * (128 * 1024)) + b"tail"
        evidence.write_bytes(payload)
        evidence.chmod(0o600)
        expected = os.stat(evidence, follow_symlinks=False)
        parent_fd = os.open(
            evidence.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            for module in (MIRROR_MODULE, ENGINE_MODULE):
                with self.subTest(module=module.__name__):
                    requested_sizes: list[int] = []
                    real_read = os.read

                    def tracked_read(file_fd: int, size: int) -> bytes:
                        requested_sizes.append(size)
                        return real_read(file_fd, size)

                    with mock.patch.object(
                        module.os,
                        "read",
                        side_effect=tracked_read,
                    ):
                        size, digest, captured, final_metadata = (
                            module._pc_recovery_read_file(
                                parent_fd,
                                evidence.name,
                                evidence,
                                expected,
                                deadline=time.monotonic() + 30,
                                payload_limit=None,
                            )
                        )
                    self.assertEqual(size, len(payload))
                    self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
                    self.assertIsNone(captured)
                    self.assertEqual(
                        module._pc_recovery_identity(final_metadata),
                        module._pc_recovery_identity(expected),
                    )
                    self.assertGreaterEqual(len(requested_sizes), 6)
                    self.assertLessEqual(max(requested_sizes), 1024 * 1024)
        finally:
            os.close(parent_fd)

    def test_primary_parent_binding_failures_do_not_become_absence(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    primary_spec,
                    _root_id,
                    _receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                namespace_name = (
                    module.PRIVATE_CONTROL_NAMESPACE_NAME
                    if module is MIRROR_MODULE
                    else module.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME
                )
                real_stat = os.stat
                removed = False

                def remove_after_observation(
                    path: object,
                    *args: object,
                    **kwargs: object,
                ) -> os.stat_result:
                    nonlocal removed
                    metadata = real_stat(path, *args, **kwargs)
                    is_target = path == namespace_name or path == self.primary_parent
                    if is_target and not removed:
                        self.primary_parent.rmdir()
                        removed = True
                    return metadata

                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(
                        module.os,
                        "stat",
                        side_effect=remove_after_observation,
                    ),
                ):
                    with self.assertRaisesRegex(error_type, "cannot bind"):
                        module._pc_recovery_existing_primary_parent(primary_spec)
                self.assertTrue(removed)
                self.assertFalse(self.primary_parent.exists())

                self.primary_parent.mkdir(mode=0o700)
                removed = False
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(
                        module.os,
                        "stat",
                        side_effect=remove_after_observation,
                    ),
                ):
                    with self.assertRaisesRegex(error_type, "cannot bind"):
                        module._pc_recovery_open_or_create_primary_parent(
                            primary_spec,
                            "0" * 64,
                            allow_create=True,
                        )
                self.assertTrue(removed)
                self.assertFalse(self.primary_parent.exists())
                self.assertFalse(
                    (
                        self.account_home
                        / f".private-control-recovery-parent-{'0' * 64}"
                    ).exists()
                )

    def test_primary_parent_publish_failure_cleans_only_exact_empty_staging(
        self,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__), self._recovery_module_scope(
                module
            ):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    primary_spec,
                    _root_id,
                    _receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                rename_name = (
                    "_rename_directory_entry_noreplace"
                    if module is MIRROR_MODULE
                    else "_rename_noreplace_at"
                )

                post_mkdir_digest = "1" * 64
                post_mkdir_staging = self.account_home / (
                    f".private-control-recovery-parent-{post_mkdir_digest}"
                )
                home_identity = module._pc_recovery_identity(
                    os.stat(self.account_home, follow_symlinks=False)
                )
                real_fsync = os.fsync
                real_fstat = os.fstat
                fsync_calls = 0
                fsync_injected = False

                def fail_first_post_mkdir_fsync(file_fd: int) -> None:
                    nonlocal fsync_calls, fsync_injected
                    fsync_calls += 1
                    if (
                        not fsync_injected
                        and module._pc_recovery_identity(real_fstat(file_fd))
                        == home_identity
                        and post_mkdir_staging.is_dir()
                        and not self.primary_parent.exists()
                    ):
                        fsync_injected = True
                        raise OSError(
                            errno.EIO,
                            "simulated post-mkdir durability failure",
                        )
                    real_fsync(file_fd)

                with mock.patch.object(
                    module.os,
                    "fsync",
                    side_effect=fail_first_post_mkdir_fsync,
                ):
                    with self.assertRaisesRegex(
                        error_type,
                        "cannot stage primary private-control parent: .*"
                        "post-mkdir durability failure",
                    ):
                        module._pc_recovery_open_or_create_primary_parent(
                            primary_spec,
                            post_mkdir_digest,
                            allow_create=True,
                        )
                self.assertTrue(fsync_injected)
                self.assertEqual(fsync_calls, 2)
                self.assertFalse(post_mkdir_staging.exists())
                self.assertFalse(self.primary_parent.exists())

                for digit in ("2", "3"):
                    plan_digest = digit * 64
                    staging = self.account_home / (
                        f".private-control-recovery-parent-{plan_digest}"
                    )
                    with mock.patch.object(
                        module,
                        rename_name,
                        side_effect=OSError(
                            errno.EIO,
                            "simulated rename-before-effect failure",
                        ),
                    ):
                        with self.assertRaisesRegex(
                            error_type,
                            "cannot publish primary private-control parent",
                        ):
                            module._pc_recovery_open_or_create_primary_parent(
                                primary_spec,
                                plan_digest,
                                allow_create=True,
                            )
                    self.assertFalse(staging.exists())
                    self.assertFalse(self.primary_parent.exists())

                nonempty_digest = "4" * 64
                nonempty_staging = self.account_home / (
                    f".private-control-recovery-parent-{nonempty_digest}"
                )

                def fail_with_nonempty_staging(*_args: object) -> None:
                    retained = nonempty_staging / "retained-evidence"
                    retained.write_bytes(b"retain\n")
                    retained.chmod(0o600)
                    raise OSError(errno.EIO, "simulated nonempty staging failure")

                with mock.patch.object(
                    module,
                    rename_name,
                    side_effect=fail_with_nonempty_staging,
                ):
                    with self.assertRaisesRegex(
                        error_type,
                        "secondary staged primary-parent cleanup failure: .*not empty",
                    ):
                        module._pc_recovery_open_or_create_primary_parent(
                            primary_spec,
                            nonempty_digest,
                            allow_create=True,
                        )
                self.assertEqual(
                    (nonempty_staging / "retained-evidence").read_bytes(),
                    b"retain\n",
                )
                shutil.rmtree(nonempty_staging)

                replacement_digest = "5" * 64
                replacement_staging = self.account_home / (
                    f".private-control-recovery-parent-{replacement_digest}"
                )
                held_staging = self.account_home / (
                    f".held-private-control-recovery-parent-{replacement_digest}"
                )

                def fail_with_replacement(*_args: object) -> None:
                    os.rename(replacement_staging, held_staging)
                    replacement_staging.mkdir(mode=0o700)
                    raise OSError(errno.EIO, "simulated staging replacement")

                with mock.patch.object(
                    module,
                    rename_name,
                    side_effect=fail_with_replacement,
                ):
                    with self.assertRaisesRegex(
                        error_type,
                        "secondary staged primary-parent cleanup failure: .*changed",
                    ):
                        module._pc_recovery_open_or_create_primary_parent(
                            primary_spec,
                            replacement_digest,
                            allow_create=True,
                        )
                self.assertTrue(replacement_staging.is_dir())
                self.assertTrue(held_staging.is_dir())
                self.assertNotEqual(
                    module._pc_recovery_identity(
                        os.stat(replacement_staging, follow_symlinks=False)
                    ),
                    module._pc_recovery_identity(
                        os.stat(held_staging, follow_symlinks=False)
                    ),
                )
                replacement_staging.rmdir()
                held_staging.rmdir()

                after_effect_digest = "6" * 64
                after_effect_staging = self.account_home / (
                    f".private-control-recovery-parent-{after_effect_digest}"
                )
                real_rename = getattr(module, rename_name)
                published_identity: tuple[int, int, int] | None = None

                def fail_after_rename(*args: object) -> None:
                    nonlocal published_identity
                    published_identity = module._pc_recovery_identity(
                        os.stat(after_effect_staging, follow_symlinks=False)
                    )
                    real_rename(*args)
                    raise OSError(errno.EIO, "simulated rename-after-effect failure")

                with mock.patch.object(
                    module,
                    rename_name,
                    side_effect=fail_after_rename,
                ):
                    with self.assertRaisesRegex(
                        error_type,
                        "secondary staged primary-parent cleanup failure: .*missing",
                    ):
                        module._pc_recovery_open_or_create_primary_parent(
                            primary_spec,
                            after_effect_digest,
                            allow_create=True,
                        )
                self.assertIsNotNone(published_identity)
                self.assertFalse(after_effect_staging.exists())
                self.assertEqual(
                    module._pc_recovery_identity(
                        os.stat(self.primary_parent, follow_symlinks=False)
                    ),
                    published_identity,
                )
                self.primary_parent.rmdir()

    def test_primary_parent_appearance_after_initial_absence_is_not_adopted(
        self,
    ) -> None:
        def identity_and_access(path: Path) -> tuple[int, int, int, int, int, int]:
            metadata = os.stat(path, follow_symlinks=False)
            return (
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
            )

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                plan_path = self.root / f"{module.__name__}-appearance-plan.json"
                sentinel_name = "competing-namespace-sentinel"
                sentinel_payload = f"{module.__name__} competing namespace\n".encode()
                real_manifest = module._pc_recovery_manifest
                injected = False
                parent_record: tuple[int, int, int, int, int, int] | None = None
                sentinel_record: tuple[int, int, int, int, int, int] | None = None

                def create_competing_namespace(
                    parent: object,
                    tool: object,
                    quarantine: object,
                    **kwargs: object,
                ) -> dict[str, object]:
                    nonlocal injected, parent_record, sentinel_record
                    inventory = real_manifest(
                        parent,
                        tool,
                        quarantine,
                        **kwargs,
                    )
                    if not injected:
                        self.primary_parent.mkdir(mode=0o700)
                        sentinel_path = self.primary_parent / sentinel_name
                        sentinel_path.write_bytes(sentinel_payload)
                        sentinel_path.chmod(0o600)
                        parent_record = identity_and_access(self.primary_parent)
                        sentinel_record = identity_and_access(sentinel_path)
                        injected = True
                    return inventory

                with self._recovery_module_scope(module):
                    module.plan_private_control_recovery(root_id, plan_path)
                    self.assertFalse(self.primary_parent.exists())
                    with (
                        mock.patch.object(
                            module,
                            "_pc_recovery_manifest",
                            side_effect=create_competing_namespace,
                        ),
                        self.assertRaisesRegex(
                            error_type,
                            "appeared after initial absence",
                        ),
                    ):
                        module.execute_private_control_recovery(root_id, plan_path)

                self.assertTrue(injected)
                self.assertIsNotNone(parent_record)
                self.assertIsNotNone(sentinel_record)
                self.assertEqual(
                    identity_and_access(self.primary_parent),
                    parent_record,
                )
                sentinel_path = self.primary_parent / sentinel_name
                self.assertEqual(identity_and_access(sentinel_path), sentinel_record)
                self.assertEqual(sentinel_path.read_bytes(), sentinel_payload)
                self.assertEqual(
                    sorted(path.name for path in self.primary_parent.iterdir()),
                    [sentinel_name],
                )
                self.assertFalse((self.primary_parent / receipt_name).exists())
                self.assertFalse((self.primary_parent / marker_name).exists())
                self.assertFalse(
                    any(
                        path.name.startswith(".private-control-recovery-parent-")
                        for path in self.account_home.iterdir()
                    )
                )

    def test_previously_bound_primary_parent_cannot_be_recreated(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    primary_spec,
                    _root_id,
                    _receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                with self._recovery_module_scope(module):
                    binding = module._pc_recovery_existing_primary_parent(primary_spec)
                    self.assertIsNotNone(binding)
                    try:
                        self.primary_parent.rmdir()
                        with self.assertRaisesRegex(error_type, "disappeared"):
                            module._pc_recovery_open_or_create_primary_parent(
                                primary_spec,
                                "1" * 64,
                                allow_create=False,
                            )
                    finally:
                        module._pc_recovery_close_bindings((binding,))
                self.assertFalse(self.primary_parent.exists())
                self.assertFalse(
                    (
                        self.account_home
                        / f".private-control-recovery-parent-{'1' * 64}"
                    ).exists()
                )

    def test_namespace_fsync_after_effect_is_repaired_on_retry(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                plan_path = self.root / f"{module.__name__}-namespace-fsync.json"
                with self._recovery_module_scope(module):
                    module.plan_private_control_recovery(root_id, plan_path)
                    real_fsync = os.fsync
                    home_identity = module._pc_recovery_identity(
                        os.stat(self.account_home, follow_symlinks=False)
                    )
                    injected = False

                    def fail_after_namespace_publish(file_fd: int) -> None:
                        nonlocal injected
                        identity = module._pc_recovery_identity(os.fstat(file_fd))
                        if (
                            not injected
                            and self.primary_parent.exists()
                            and identity == home_identity
                        ):
                            injected = True
                            raise OSError(errno.EIO, "simulated home fsync failure")
                        real_fsync(file_fd)

                    with mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=fail_after_namespace_publish,
                    ):
                        with self.assertRaisesRegex(error_type, "cannot durably bind"):
                            module.execute_private_control_recovery(root_id, plan_path)
                    self.assertTrue(injected)
                    self.assertTrue(self.primary_parent.is_dir())
                    self.assertFalse((self.primary_parent / receipt_name).exists())
                    self.assertFalse((self.primary_parent / marker_name).exists())
                    namespace_identity = module._pc_recovery_identity(
                        os.stat(self.primary_parent, follow_symlinks=False)
                    )
                    retry_fsyncs = 0

                    def track_home_fsync(file_fd: int) -> None:
                        nonlocal retry_fsyncs
                        if (
                            module._pc_recovery_identity(os.fstat(file_fd))
                            == home_identity
                        ):
                            retry_fsyncs += 1
                        real_fsync(file_fd)

                    with mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=track_home_fsync,
                    ):
                        result = module.execute_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                    self.assertEqual(result["status"], "executed")
                    self.assertGreaterEqual(retry_fsyncs, 1)
                    self.assertEqual(
                        module._pc_recovery_identity(
                            os.stat(self.primary_parent, follow_symlinks=False)
                        ),
                        namespace_identity,
                    )

    def test_marker_fsync_after_effect_is_repaired_on_retry(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                plan_path = self.root / f"{module.__name__}-marker-fsync.json"
                marker_path = self.primary_parent / marker_name
                with self._recovery_module_scope(module):
                    module.plan_private_control_recovery(root_id, plan_path)
                    real_fsync = os.fsync
                    injected = False

                    def fail_after_marker_publish(file_fd: int) -> None:
                        nonlocal injected
                        if marker_path.exists():
                            parent_identity = module._pc_recovery_identity(
                                os.stat(self.primary_parent, follow_symlinks=False)
                            )
                            if (
                                not injected
                                and module._pc_recovery_identity(os.fstat(file_fd))
                                == parent_identity
                            ):
                                injected = True
                                raise OSError(
                                    errno.EIO,
                                    "simulated marker-parent fsync failure",
                                )
                        real_fsync(file_fd)

                    with mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=fail_after_marker_publish,
                    ):
                        with self.assertRaisesRegex(error_type, "cannot durably bind"):
                            module.execute_private_control_recovery(root_id, plan_path)
                    self.assertTrue(injected)
                    self.assertTrue((self.primary_parent / receipt_name).is_file())
                    self.assertTrue(marker_path.is_file())
                    marker_metadata = os.stat(marker_path, follow_symlinks=False)
                    marker_payload = marker_path.read_bytes()
                    parent_identity = module._pc_recovery_identity(
                        os.stat(self.primary_parent, follow_symlinks=False)
                    )
                    retry_fsyncs = 0

                    def track_parent_fsync(file_fd: int) -> None:
                        nonlocal retry_fsyncs
                        if (
                            module._pc_recovery_identity(os.fstat(file_fd))
                            == parent_identity
                        ):
                            retry_fsyncs += 1
                        real_fsync(file_fd)

                    with mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=track_parent_fsync,
                    ):
                        result = module.execute_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                    self.assertEqual(result["status"], "executed")
                    self.assertGreaterEqual(retry_fsyncs, 1)
                    final_marker = os.stat(marker_path, follow_symlinks=False)
                    self.assertEqual(
                        module._pc_recovery_identity(final_marker),
                        module._pc_recovery_identity(marker_metadata),
                    )
                    self.assertEqual(
                        module._pc_recovery_access(final_marker),
                        module._pc_recovery_access(marker_metadata),
                    )
                    self.assertEqual(marker_path.read_bytes(), marker_payload)

    def test_marker_retry_rejects_identical_content_replacement(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    _receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                plan_path = self.root / f"{module.__name__}-marker-replace.json"
                marker_path = self.primary_parent / marker_name
                with self._recovery_module_scope(module):
                    module.plan_private_control_recovery(root_id, plan_path)
                    module.execute_private_control_recovery(root_id, plan_path)
                    original_helper = module._pc_recovery_fsync_directory
                    replaced = False

                    def replace_after_parent_fsync(binding: object) -> None:
                        nonlocal replaced
                        original_helper(binding)
                        if binding.path == self.primary_parent and not replaced:
                            payload = marker_path.read_bytes()
                            original_identity = module._pc_recovery_identity(
                                os.stat(marker_path, follow_symlinks=False)
                            )
                            replacement = marker_path.with_name(
                                f".{marker_path.name}.replacement"
                            )
                            replacement.write_bytes(payload)
                            replacement.chmod(0o400)
                            os.replace(replacement, marker_path)
                            replacement_identity = module._pc_recovery_identity(
                                os.stat(marker_path, follow_symlinks=False)
                            )
                            self.assertNotEqual(
                                replacement_identity,
                                original_identity,
                            )
                            replaced = True

                    with mock.patch.object(
                        module,
                        "_pc_recovery_fsync_directory",
                        side_effect=replace_after_parent_fsync,
                    ):
                        with self.assertRaisesRegex(error_type, "changed"):
                            module.execute_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                    self.assertTrue(replaced)

    def _assert_initial_marker_replacement_is_rejected(
        self,
        *,
        different_content: bool,
        restore_original_after_verification: bool,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    _receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                variant = "different" if different_content else "same"
                plan_path = self.root / f"{module.__name__}-initial-{variant}.json"
                marker_path = self.primary_parent / marker_name
                held_path = marker_path.with_name(f".{marker_name}.held")
                replacement_path = marker_path.with_name(
                    f".{marker_name}.replacement"
                )
                with self._recovery_module_scope(module):
                    module.plan_private_control_recovery(root_id, plan_path)
                    original_verify = module._pc_recovery_verify_adoption_locked
                    replaced = False
                    verification_completed = False

                    def replace_before_verification(
                        *args: object,
                        **kwargs: object,
                    ) -> dict[str, object]:
                        nonlocal replaced, verification_completed
                        if not replaced:
                            payload = marker_path.read_bytes()
                            original_identity = module._pc_recovery_identity(
                                os.stat(marker_path, follow_symlinks=False)
                            )
                            if restore_original_after_verification:
                                os.rename(marker_path, held_path)
                            replacement_path.write_bytes(
                                payload + (b" " if different_content else b"")
                            )
                            replacement_path.chmod(0o400)
                            os.replace(replacement_path, marker_path)
                            replacement_identity = module._pc_recovery_identity(
                                os.stat(marker_path, follow_symlinks=False)
                            )
                            self.assertNotEqual(
                                replacement_identity,
                                original_identity,
                            )
                            replaced = True
                        try:
                            result = original_verify(*args, **kwargs)
                            verification_completed = True
                            return result
                        finally:
                            if (
                                restore_original_after_verification
                                and held_path.exists()
                            ):
                                os.replace(held_path, marker_path)

                    with mock.patch.object(
                        module,
                        "_pc_recovery_verify_adoption_locked",
                        side_effect=replace_before_verification,
                    ):
                        with self.assertRaisesRegex(error_type, "marker changed"):
                            module.execute_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                    self.assertTrue(replaced)
                    self.assertTrue(verification_completed)

    def test_initial_publish_rejects_same_content_marker_replacement(self) -> None:
        self._assert_initial_marker_replacement_is_rejected(
            different_content=False,
            restore_original_after_verification=False,
        )

    def test_initial_publish_rejects_different_content_marker_replacement(
        self,
    ) -> None:
        self._assert_initial_marker_replacement_is_rejected(
            different_content=True,
            restore_original_after_verification=True,
        )

    def test_receipt_only_crash_is_retryable_and_not_accepted(self) -> None:
        plan_path, _plan = self._plan()
        original_publish = MIRROR_MODULE._pc_recovery_publish_document

        def fail_before_marker(*args: object, **kwargs: object):
            if args[1] == MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME:
                raise MIRROR_MODULE.MirrorSyncError("simulated marker crash")
            return original_publish(*args, **kwargs)

        with mock.patch.object(
            MIRROR_MODULE,
            "_pc_recovery_publish_document",
            side_effect=fail_before_marker,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "simulated marker crash",
            ):
                MIRROR_MODULE.execute_private_control_recovery(
                    MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
        self.assertTrue(
            (
                self.primary_parent
                / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ).is_file()
        )
        self.assertFalse(
            (
                self.primary_parent / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ).exists()
        )
        executed = MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        self.assertEqual(executed["status"], "executed")

    def test_partial_pending_receipt_write_is_retryable(self) -> None:
        plan_path, _plan = self._plan()
        real_write = os.write
        injected = False

        def fail_after_partial_write(file_fd: int, payload: bytes) -> int:
            nonlocal injected
            if not injected:
                injected = True
                real_write(file_fd, payload[: max(1, len(payload) // 2)])
                raise OSError(errno.EIO, "simulated interrupted receipt write")
            return real_write(file_fd, payload)

        with mock.patch.object(
            MIRROR_MODULE.os,
            "write",
            side_effect=fail_after_partial_write,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "cannot write pending primary recovery receipt",
            ):
                MIRROR_MODULE.execute_private_control_recovery(
                    MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )
        self.assertFalse(
            (
                self.primary_parent
                / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ).exists()
        )
        self.assertFalse(
            (
                self.primary_parent / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ).exists()
        )
        self.assertEqual(
            len(
                tuple(
                    self.primary_parent.glob(
                        f".{MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME}"
                        ".pending-*"
                    )
                )
            ),
            1,
        )
        executed = MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        self.assertEqual(executed["status"], "executed")

    def test_repeated_pending_publication_failures_remain_bounded(self) -> None:
        def pending_snapshot(
            receipt_name: str,
            marker_name: str,
        ) -> tuple[tuple[object, ...], ...]:
            prefixes = (
                f".{receipt_name}.pending-",
                f".{marker_name}.pending-",
            )
            records: list[tuple[object, ...]] = []
            for path in sorted(self.primary_parent.iterdir()):
                if not path.name.startswith(prefixes):
                    continue
                metadata = os.stat(path, follow_symlinks=False)
                records.append(
                    (
                        path.name,
                        metadata.st_dev,
                        metadata.st_ino,
                        stat.S_IFMT(metadata.st_mode),
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_uid,
                        metadata.st_gid,
                        metadata.st_nlink,
                        metadata.st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
            return tuple(records)

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                entry_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                )
                byte_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                )
                receipt_cap = getattr(
                    module,
                    (
                        "PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                        if module is MIRROR_MODULE
                        else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                    ),
                )
                plan_path = self.root / f"{module.__name__}-pending-count.json"
                real_write = os.write
                write_calls = 0

                def fail_after_partial_write(file_fd: int, payload: bytes) -> int:
                    nonlocal write_calls
                    write_calls += 1
                    real_write(file_fd, payload[:8])
                    raise OSError(errno.EIO, "simulated repeated write failure")

                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 1),
                    mock.patch.object(module, byte_cap_name, 8 * receipt_cap),
                ):
                    module.plan_private_control_recovery(root_id, plan_path)
                    with mock.patch.object(
                        module.os,
                        "write",
                        side_effect=fail_after_partial_write,
                    ):
                        for _attempt in range(2):
                            with self.assertRaisesRegex(
                                error_type,
                                "cannot write pending primary recovery receipt",
                            ):
                                module.execute_private_control_recovery(
                                    root_id,
                                    plan_path,
                                )
                            current = pending_snapshot(receipt_name, marker_name)
                            self.assertEqual(len(current), 1)
                            if _attempt == 0:
                                retained = current
                            else:
                                self.assertEqual(current, retained)
                        self.assertEqual(write_calls, 2)

                    retained_path = self.primary_parent / str(retained[0][0])
                    retained_path.chmod(0o400)
                    executed = module.execute_private_control_recovery(
                        root_id,
                        plan_path,
                    )
                    self.assertEqual(executed["status"], "executed")
                    self.assertEqual(
                        pending_snapshot(receipt_name, marker_name),
                        (),
                    )

                shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                plan_path = self.root / f"{module.__name__}-pending-entry.json"
                marker_pending = (
                    self.primary_parent
                    / f".{marker_name}.pending-{'0' * 64}-{'1' * 32}"
                )
                marker_pending.write_bytes(b"12345678")
                marker_pending.chmod(0o600)
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 1),
                    mock.patch.object(module, byte_cap_name, 8 * receipt_cap),
                ):
                    module.plan_private_control_recovery(root_id, plan_path)
                    retained = pending_snapshot(receipt_name, marker_name)
                    self.assertEqual(len(retained), 1)
                    with self.assertRaisesRegex(error_type, "entry cap"):
                        module.execute_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                    self.assertEqual(
                        pending_snapshot(receipt_name, marker_name),
                        retained,
                    )

                shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                plan_path = self.root / f"{module.__name__}-pending-bytes.json"
                marker_pending = (
                    self.primary_parent
                    / f".{marker_name}.pending-{'0' * 64}-{'1' * 32}"
                )
                marker_pending.write_bytes(b"12345678")
                marker_pending.chmod(0o600)
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 8),
                    mock.patch.object(
                        module,
                        byte_cap_name,
                        receipt_cap + 7,
                    ),
                ):
                    module.plan_private_control_recovery(root_id, plan_path)
                    retained = pending_snapshot(receipt_name, marker_name)
                    self.assertEqual(len(retained), 1)
                    with self.assertRaisesRegex(error_type, "aggregate-byte cap"):
                        module.execute_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                    self.assertEqual(
                        pending_snapshot(receipt_name, marker_name),
                        retained,
                    )

    def test_pre_cap_same_plan_pending_history_is_drained(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    _error_type,
                ) = self._recovery_module_contract(module)
                entry_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                )
                byte_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                )
                receipt_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                )

                def pending_names() -> tuple[str, ...]:
                    prefixes = (
                        f".{receipt_name}.pending-",
                        f".{marker_name}.pending-",
                    )
                    return tuple(
                        sorted(
                            path.name
                            for path in self.primary_parent.iterdir()
                            if path.name.startswith(prefixes)
                        )
                    )

                plan_path = self.root / f"{module.__name__}-pre-cap-entry.json"
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 8),
                ):
                    plan = module.plan_private_control_recovery(root_id, plan_path)
                    for index in range(9):
                        pending = (
                            self.primary_parent
                            / f".{receipt_name}.pending-{plan['plan_digest']}-"
                            f"{index:032x}"
                        )
                        pending.write_bytes(b"partial\n")
                        pending.chmod(0o600)
                    self.assertEqual(len(pending_names()), 9)
                    executed = module.execute_private_control_recovery(
                        root_id,
                        plan_path,
                    )
                    self.assertEqual(executed["status"], "executed")
                    self.assertEqual(pending_names(), ())

                shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                plan_path = self.root / f"{module.__name__}-pre-cap-bytes.json"
                small_receipt_cap = 1024 * 1024
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 8),
                    mock.patch.object(module, receipt_cap_name, small_receipt_cap),
                    mock.patch.object(module, byte_cap_name, small_receipt_cap),
                ):
                    plan = module.plan_private_control_recovery(root_id, plan_path)
                    for index in range(2):
                        pending = (
                            self.primary_parent
                            / f".{receipt_name}.pending-{plan['plan_digest']}-"
                            f"{index:032x}"
                        )
                        pending.write_bytes(b"x" * (600 * 1024))
                        pending.chmod(0o600)
                    self.assertGreater(
                        sum(
                            path.stat().st_size
                            for path in self.primary_parent.iterdir()
                        ),
                        small_receipt_cap,
                    )
                    executed = module.execute_private_control_recovery(
                        root_id,
                        plan_path,
                    )
                    self.assertEqual(executed["status"], "executed")
                    self.assertEqual(pending_names(), ())

    def test_plan_capacity_includes_primary_receipt_before_publication(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                (
                    _primary_spec,
                    root_id,
                    _receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                )
                seed_path = self.root / f"{module.__name__}-receipt-cap-seed.json"
                with self._recovery_module_scope(module):
                    plan = module.plan_private_control_recovery(root_id, seed_path)
                seed_path.unlink()

                baseline_receipt = module._pc_recovery_json_bytes(
                    module._pc_recovery_primary_receipt_document(
                        plan,
                        (0, 0, stat.S_IFREG),
                    ),
                    pretty=True,
                )
                receipt_upper_bound = len(baseline_receipt) + (
                    4 * module.MAX_JSON_INTEGER_DIGITS
                )
                plan_payload = module._pc_recovery_json_bytes(plan, pretty=True)
                self.assertLessEqual(len(plan_payload), receipt_upper_bound - 1)
                with self.assertRaisesRegex(
                    error_type,
                    "late-bound identity is unsupported",
                ):
                    module._pc_recovery_primary_receipt_document(
                        plan,
                        (10**module.MAX_JSON_INTEGER_DIGITS, 0, stat.S_IFREG),
                    )

                rejected_path = self.root / (
                    f"{module.__name__}-receipt-cap-rejected.json"
                )
                with (
                    mock.patch.object(module, cap_name, receipt_upper_bound - 1),
                    mock.patch.object(
                        module,
                        "_pc_recovery_bind_external_plan_parent",
                        side_effect=AssertionError(
                            "receipt-cap rejection reached plan publication"
                        ),
                    ) as bind_parent,
                    self.assertRaisesRegex(
                        error_type,
                        "primary recovery receipt exceeds its byte cap",
                    ),
                ):
                    module._pc_recovery_write_plan(rejected_path, plan, ())
                bind_parent.assert_not_called()
                self.assertFalse(rejected_path.exists())
                self.assertFalse(self.primary_parent.exists())

                exact_path = self.root / f"{module.__name__}-receipt-cap-exact.json"
                with mock.patch.object(module, cap_name, receipt_upper_bound):
                    module._pc_recovery_write_plan(exact_path, plan, ())
                self.assertEqual(exact_path.read_bytes(), plan_payload)
                self.assertFalse(self.primary_parent.exists())

                unsafe_plan = json.loads(json.dumps(plan))
                unsafe_cap = len(baseline_receipt) - 1
                for _attempt in range(8):
                    unsafe_plan["caps"]["max_receipt_bytes"] = unsafe_cap
                    unsafe_plan["plan_digest"] = module._pc_recovery_digest(
                        module._pc_recovery_protected_plan(unsafe_plan)
                    )
                    next_receipt_size = len(
                        module._pc_recovery_json_bytes(
                            module._pc_recovery_primary_receipt_document(
                                unsafe_plan,
                                (0, 0, stat.S_IFREG),
                            ),
                            pretty=True,
                        )
                    )
                    next_cap = next_receipt_size - 1
                    if next_cap == unsafe_cap:
                        break
                    unsafe_cap = next_cap
                else:
                    self.fail("primary receipt capacity fixture did not stabilize")
                unsafe_payload = module._pc_recovery_json_bytes(
                    unsafe_plan,
                    pretty=True,
                )
                self.assertLessEqual(len(unsafe_payload), unsafe_cap)
                unsafe_receipt = module._pc_recovery_json_bytes(
                    module._pc_recovery_primary_receipt_document(
                        unsafe_plan,
                        (0, 0, stat.S_IFREG),
                    ),
                    pretty=True,
                )
                self.assertLess(unsafe_cap, len(unsafe_receipt))
                unsafe_path = self.root / f"{module.__name__}-receipt-cap-unsafe.json"
                unsafe_path.write_bytes(unsafe_payload)
                unsafe_path.chmod(0o600)
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, cap_name, unsafe_cap),
                    mock.patch.object(
                        module,
                        "_pc_recovery_open_or_create_primary_parent",
                        side_effect=AssertionError(
                            "unsafe plan reached primary-parent publication"
                        ),
                    ) as open_primary,
                    mock.patch.object(
                        module,
                        "_pc_recovery_publish_document",
                        side_effect=AssertionError(
                            "unsafe plan reached pending receipt publication"
                        ),
                    ) as publish_document,
                    self.assertRaisesRegex(
                        error_type,
                        "primary recovery receipt exceeds its byte cap",
                    ),
                ):
                    module.execute_private_control_recovery(root_id, unsafe_path)
                open_primary.assert_not_called()
                publish_document.assert_not_called()
                self.assertFalse(self.primary_parent.exists())

    def test_reused_pending_growth_is_accounted_before_mutation(self) -> None:
        padding = "x" * 4096

        def document_builder(identity):
            return {
                "identity": list(identity),
                "padding": padding,
            }

        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    _primary_spec,
                    _root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                entry_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES"
                )
                byte_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES"
                )
                receipt_cap_name = (
                    "PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                    if module is MIRROR_MODULE
                    else "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES"
                )
                receipt_cap = 1024 * 1024
                byte_cap = 2 * receipt_cap
                plan_digest = "a" * 64
                receipt_prefix = f".{receipt_name}.pending-"
                marker_prefix = f".{marker_name}.pending-"
                reusable_path = self.primary_parent / (
                    f"{receipt_prefix}{plan_digest}-{'b' * 32}"
                )
                other_paths = (
                    self.primary_parent
                    / f"{receipt_prefix}{'c' * 64}-{'d' * 32}",
                    self.primary_parent
                    / f"{marker_prefix}{'e' * 64}-{'f' * 32}",
                )
                reusable_path.write_bytes(b"x")
                reusable_path.chmod(0o600)
                other_size = (byte_cap - 1) // 2
                for path in other_paths:
                    path.write_bytes(b"y" * other_size)
                    path.chmod(0o600)
                requested_name = (
                    f"{receipt_prefix}{plan_digest}-{'0' * 32}"
                )
                before = tuple(
                    (
                        path.name,
                        path.read_bytes(),
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path in (reusable_path, *other_paths)
                )
                self.assertLessEqual(
                    sum(len(record[1]) for record in before),
                    byte_cap,
                )

                parent = None
                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 2),
                    mock.patch.object(module, byte_cap_name, byte_cap),
                    mock.patch.object(module, receipt_cap_name, receipt_cap),
                    mock.patch.object(
                        module,
                        "_pc_recovery_open_pending_publication_for_reuse",
                        side_effect=AssertionError(
                            "over-cap reuse reached effectful open"
                        ),
                    ) as open_reuse,
                ):
                    parent = module._pc_recovery_bind_directory(
                        self.primary_parent,
                        "test pending publication parent",
                    )
                    module._pc_recovery_acquire_exclusive(parent)
                    try:
                        with self.assertRaisesRegex(
                            error_type,
                            "aggregate-byte cap",
                        ):
                            module._pc_recovery_publish_document(
                                parent,
                                receipt_name,
                                requested_name,
                                "primary recovery receipt",
                                document_builder,
                            )
                        open_reuse.assert_not_called()
                    finally:
                        module._pc_recovery_close_bindings((parent,))
                after = tuple(
                    (
                        path.name,
                        path.read_bytes(),
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path in (reusable_path, *other_paths)
                )
                self.assertEqual(after, before)

                shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                receipt_cap = 1024 * 1024
                byte_cap = receipt_cap
                reusable_path = self.primary_parent / (
                    f"{receipt_prefix}{plan_digest}-{'b' * 32}"
                )
                reusable_path.write_bytes(b"x")
                reusable_path.chmod(0o600)
                reusable_metadata = reusable_path.stat()
                reusable_identity = (
                    reusable_metadata.st_dev,
                    reusable_metadata.st_ino,
                    stat.S_IFMT(reusable_metadata.st_mode),
                )
                payload = module._pc_recovery_json_bytes(
                    document_builder(reusable_identity),
                    pretty=True,
                )
                other_path = self.primary_parent / (
                    f"{marker_prefix}{'e' * 64}-{'f' * 32}"
                )
                other_path.write_bytes(b"y" * (byte_cap - len(payload)))
                other_path.chmod(0o600)
                parent = None
                real_fsync = os.fsync
                injected = False

                def fail_pending_fsync(file_fd: int) -> None:
                    nonlocal injected
                    metadata = os.fstat(file_fd)
                    if stat.S_ISREG(metadata.st_mode) and not injected:
                        injected = True
                        raise OSError(
                            errno.EIO,
                            "simulated pending publication failure",
                        )
                    real_fsync(file_fd)

                with (
                    self._recovery_module_scope(module),
                    mock.patch.object(module, entry_cap_name, 2),
                    mock.patch.object(module, byte_cap_name, byte_cap),
                    mock.patch.object(module, receipt_cap_name, receipt_cap),
                    mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=fail_pending_fsync,
                    ),
                ):
                    parent = module._pc_recovery_bind_directory(
                        self.primary_parent,
                        "test pending publication parent",
                    )
                    module._pc_recovery_acquire_exclusive(parent)
                    try:
                        with self.assertRaisesRegex(
                            error_type,
                            "cannot write pending primary recovery receipt",
                        ):
                            module._pc_recovery_publish_document(
                                parent,
                                receipt_name,
                                requested_name,
                                "primary recovery receipt",
                                document_builder,
                            )
                    finally:
                        module._pc_recovery_close_bindings((parent,))
                self.assertTrue(injected)
                self.assertEqual(reusable_path.read_bytes(), payload)
                self.assertEqual(
                    sum(
                        path.stat().st_size
                        for path in (reusable_path, other_path)
                    ),
                    byte_cap,
                )

    def test_pending_writer_close_uncertainty_retains_transaction_fence(
        self,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for reuse_existing in (False, True):
                for close_after_effect in (False, True):
                    with self.subTest(
                        module=module.__name__,
                        reuse_existing=reuse_existing,
                        close_after_effect=close_after_effect,
                    ):
                        if self.primary_parent.exists():
                            shutil.rmtree(self.primary_parent)
                        self.primary_parent.mkdir(mode=0o700)
                        (
                            _primary_spec,
                            root_id,
                            receipt_name,
                            _marker_name,
                            error_type,
                        ) = self._recovery_module_contract(module)
                        plan_path = self.root / (
                            f"{module.__name__}-pending-close-"
                            f"{int(reuse_existing)}-{int(close_after_effect)}.json"
                        )
                        real_open = os.open
                        real_close = os.close
                        injected_fd = -1
                        injected_close_calls = 0
                        sentinel_fd = -1
                        sentinel_identity: tuple[int, int, int] | None = None
                        expected_reuse_identity: tuple[int, int, int] | None = None
                        expected_writer_path: Path | None = None

                        def inject_pending_writer_close(file_fd: int) -> None:
                            nonlocal injected_fd
                            nonlocal injected_close_calls
                            nonlocal sentinel_fd
                            nonlocal sentinel_identity
                            if injected_fd >= 0 and file_fd == injected_fd:
                                injected_close_calls += 1
                                raise AssertionError(
                                    "close-uncertain descriptor was closed again"
                                )
                            flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                            if (
                                injected_fd < 0
                                and flags & os.O_ACCMODE == os.O_WRONLY
                            ):
                                injected_fd = file_fd
                                injected_close_calls += 1
                                matching = tuple(
                                    custody
                                    for custody in module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                                    if custody.fd == file_fd
                                )
                                self.assertEqual(len(matching), 1)
                                self.assertEqual(matching[0].state, "close-uncertain")
                                if expected_writer_path is None:
                                    self.assertEqual(
                                        matching[0].path.parent,
                                        self.primary_parent,
                                    )
                                    self.assertTrue(
                                        matching[0].path.name.startswith(
                                            f".{receipt_name}.pending-"
                                        )
                                    )
                                else:
                                    self.assertEqual(
                                        matching[0].path,
                                        expected_writer_path,
                                    )
                                if expected_reuse_identity is not None:
                                    self.assertEqual(
                                        matching[0].identity,
                                        expected_reuse_identity,
                                    )
                                if close_after_effect:
                                    real_close(file_fd)
                                    sentinel_path = self.root / (
                                        f"{module.__name__}-pending-close-sentinel-"
                                        f"{int(reuse_existing)}"
                                    )
                                    sentinel_path.write_bytes(
                                        b"unique close-after-effect sentinel\n"
                                    )
                                    sentinel_path.chmod(0o600)
                                    replacement_fd = real_open(
                                        sentinel_path,
                                        os.O_RDONLY,
                                    )
                                    sentinel_path.unlink()
                                    if replacement_fd != file_fd:
                                        os.dup2(replacement_fd, file_fd)
                                        real_close(replacement_fd)
                                    sentinel_fd = file_fd
                                    metadata = os.fstat(sentinel_fd)
                                    sentinel_identity = (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                        stat.S_IFMT(metadata.st_mode),
                                    )
                                raise OSError(
                                    errno.EIO,
                                    "simulated pending writer close uncertainty",
                                )
                            real_close(file_fd)

                        retained: tuple[object, ...] = ()
                        custody: tuple[object, ...] = ()
                        try:
                            with self._recovery_module_scope(module):
                                plan = module.plan_private_control_recovery(
                                    root_id,
                                    plan_path,
                                )
                                if reuse_existing:
                                    pending = self.primary_parent / (
                                        f".{receipt_name}.pending-"
                                        f"{plan['plan_digest']}-{'a' * 32}"
                                    )
                                    pending.write_bytes(b"retained partial bytes\n")
                                    pending.chmod(0o400)
                                    metadata = pending.stat()
                                    expected_reuse_identity = (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                        stat.S_IFMT(metadata.st_mode),
                                    )
                                    expected_writer_path = pending
                                with mock.patch.object(
                                    module.os,
                                    "close",
                                    side_effect=inject_pending_writer_close,
                                ):
                                    with self.assertRaisesRegex(
                                        error_type,
                                        "pending primary recovery receipt descriptor",
                                    ):
                                        module.execute_private_control_recovery(
                                            root_id,
                                            plan_path,
                                        )
                                    custody = tuple(
                                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                                    )
                                    retained = tuple(
                                        module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                                    )
                                    self.assertEqual(len(custody), 2)
                                    writer_custody = tuple(
                                        item
                                        for item in custody
                                        if item.fd == injected_fd
                                    )
                                    reader_custody = tuple(
                                        item
                                        for item in custody
                                        if item.fd != injected_fd
                                    )
                                    self.assertEqual(len(writer_custody), 1)
                                    self.assertEqual(len(reader_custody), 1)
                                    self.assertEqual(
                                        writer_custody[0].state,
                                        "close-uncertain",
                                    )
                                    self.assertEqual(
                                        reader_custody[0].state,
                                        "open",
                                    )
                                    self.assertEqual(
                                        reader_custody[0].path,
                                        writer_custody[0].path,
                                    )
                                    self.assertEqual(
                                        reader_custody[0].identity,
                                        writer_custody[0].identity,
                                    )
                                    self.assertEqual(injected_close_calls, 1)
                                    self.assertGreaterEqual(len(retained), 4)
                                    for binding in retained:
                                        os.fstat(binding.fd)
                                    if close_after_effect:
                                        self.assertEqual(sentinel_fd, injected_fd)
                                        metadata = os.fstat(sentinel_fd)
                                        self.assertEqual(
                                            (
                                                metadata.st_dev,
                                                metadata.st_ino,
                                                stat.S_IFMT(metadata.st_mode),
                                            ),
                                            sentinel_identity,
                                        )
                                    else:
                                        flags = fcntl.fcntl(
                                            injected_fd,
                                            fcntl.F_GETFL,
                                        )
                                        self.assertEqual(
                                            flags & os.O_ACCMODE,
                                            os.O_WRONLY,
                                        )
                                    with mock.patch.object(
                                        module.os,
                                        "open",
                                        side_effect=AssertionError(
                                            "close fence gate must precede any open"
                                        ),
                                    ):
                                        with self.assertRaisesRegex(
                                            error_type,
                                            "close remains uncertain",
                                        ):
                                            module.plan_private_control_recovery(
                                                root_id,
                                                self.root / "blocked-plan.json",
                                            )
                                        with self.assertRaisesRegex(
                                            error_type,
                                            "close remains uncertain",
                                        ):
                                            module.execute_private_control_recovery(
                                                root_id,
                                                plan_path,
                                            )
                                    self.assertEqual(injected_close_calls, 1)
                                    if close_after_effect:
                                        metadata = os.fstat(sentinel_fd)
                                        self.assertEqual(
                                            (
                                                metadata.st_dev,
                                                metadata.st_ino,
                                                stat.S_IFMT(metadata.st_mode),
                                            ),
                                            sentinel_identity,
                                        )
                        finally:
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                            module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                            if injected_fd >= 0 and not close_after_effect:
                                real_close(injected_fd)
                            if sentinel_fd >= 0:
                                real_close(sentinel_fd)
                            for item in custody:
                                if item.fd >= 0 and item.fd != injected_fd:
                                    real_close(item.fd)
                            if retained:
                                module._pc_recovery_close_bindings(retained)

    def test_root_binding_close_uncertainty_retains_transaction_fence(
        self,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for operation in ("plan", "execute"):
                for close_after_effect in (False, True):
                    with self.subTest(
                        module=module.__name__,
                        operation=operation,
                        close_after_effect=close_after_effect,
                    ):
                        if self.primary_parent.exists():
                            shutil.rmtree(self.primary_parent)
                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                        module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                        (
                            _primary_spec,
                            root_id,
                            _receipt_name,
                            _marker_name,
                            error_type,
                        ) = self._recovery_module_contract(module)
                        plan_path = self.root / (
                            f"{module.__name__}-root-close-{operation}-"
                            f"{int(close_after_effect)}.json"
                        )
                        real_bind_legacy = module._pc_recovery_bind_legacy
                        real_open_primary = (
                            module._pc_recovery_open_or_create_primary_parent
                        )
                        real_open = os.open
                        real_close = os.close
                        real_fstat = os.fstat
                        real_flock = fcntl.flock
                        captured: dict[str, tuple[object, ...]] = {}
                        target_binding: object | None = None
                        injected_fd = -1
                        injected_close_calls = 0
                        sentinel_identity: tuple[int, int, int] | None = None
                        retained: tuple[object, ...] = ()
                        unlock_fds: list[int] = []

                        def capture_legacy(*args: object, **kwargs: object):
                            nonlocal target_binding
                            result = real_bind_legacy(*args, **kwargs)
                            captured["legacy"] = tuple(
                                binding
                                for binding in (
                                    result[5],
                                    result[4],
                                    result[3],
                                    result[2],
                                )
                                if binding is not None
                            )
                            if operation == "plan":
                                target_binding = result[4]
                            return result

                        def capture_primary(*args: object, **kwargs: object):
                            nonlocal target_binding
                            result = real_open_primary(*args, **kwargs)
                            captured["primary"] = (result[1], result[0])
                            if operation == "execute":
                                target_binding = result[1]
                            return result

                        def expected_roots() -> tuple[object, ...]:
                            roots = (
                                *captured.get("primary", ()),
                                *captured.get("legacy", ()),
                            )
                            return tuple(
                                binding
                                for index, binding in enumerate(roots)
                                if id(binding)
                                not in {id(item) for item in roots[:index]}
                            )

                        def observe_flock(file_fd: int, flags: int) -> None:
                            if flags == fcntl.LOCK_UN:
                                unlock_fds.append(file_fd)
                            real_flock(file_fd, flags)

                        def inject_root_close(file_fd: int) -> None:
                            nonlocal injected_fd
                            nonlocal injected_close_calls
                            nonlocal sentinel_identity
                            if (
                                target_binding is not None
                                and file_fd == target_binding.fd
                            ):
                                if injected_fd >= 0:
                                    injected_close_calls += 1
                                    raise AssertionError(
                                        "close-uncertain root binding was closed again"
                                    )
                                injected_fd = file_fd
                                injected_close_calls = 1
                                self.assertNotIn(file_fd, unlock_fds)
                                fence = tuple(
                                    module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                                )
                                expected = expected_roots()
                                self.assertTrue(expected)
                                self.assertTrue(
                                    {id(binding) for binding in expected}
                                    <= {id(binding) for binding in fence}
                                )
                                self.assertTrue(target_binding.locked)
                                self.assertEqual(
                                    target_binding.close_state,
                                    "close-uncertain",
                                )
                                if close_after_effect:
                                    real_close(file_fd)
                                    sentinel_path = self.root / (
                                        f"{module.__name__}-root-close-sentinel-"
                                        f"{operation}"
                                    )
                                    sentinel_path.write_bytes(
                                        b"unique root close-after-effect sentinel\n"
                                    )
                                    sentinel_path.chmod(0o600)
                                    replacement_fd = real_open(
                                        sentinel_path,
                                        os.O_RDONLY,
                                    )
                                    sentinel_path.unlink()
                                    if replacement_fd != file_fd:
                                        os.dup2(replacement_fd, file_fd)
                                        real_close(replacement_fd)
                                    metadata = real_fstat(file_fd)
                                    sentinel_identity = (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                        stat.S_IFMT(metadata.st_mode),
                                    )
                                raise OSError(
                                    errno.EIO,
                                    "simulated root binding close uncertainty",
                                )
                            real_close(file_fd)

                        def assert_lock_contended(binding: object) -> None:
                            competitor_fd = real_open(
                                binding.path,
                                os.O_RDONLY
                                | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_CLOEXEC", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                            )
                            try:
                                with self.assertRaises(BlockingIOError):
                                    real_flock(
                                        competitor_fd,
                                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                                    )
                            finally:
                                real_close(competitor_fd)

                        try:
                            with self._recovery_module_scope(module):
                                if operation == "execute":
                                    module.plan_private_control_recovery(
                                        root_id,
                                        plan_path,
                                    )
                                with (
                                    mock.patch.object(
                                        module,
                                        "_pc_recovery_bind_legacy",
                                        side_effect=capture_legacy,
                                    ),
                                    mock.patch.object(
                                        module,
                                        "_pc_recovery_open_or_create_primary_parent",
                                        side_effect=capture_primary,
                                    ),
                                    mock.patch.object(
                                        module.fcntl,
                                        "flock",
                                        side_effect=observe_flock,
                                    ),
                                    mock.patch.object(
                                        module.os,
                                        "close",
                                        side_effect=inject_root_close,
                                    ),
                                    self.assertRaisesRegex(
                                        error_type,
                                        "cannot close .*private-control",
                                    ),
                                ):
                                    if operation == "plan":
                                        module.plan_private_control_recovery(
                                            root_id,
                                            plan_path,
                                        )
                                    else:
                                        module.execute_private_control_recovery(
                                            root_id,
                                            plan_path,
                                        )

                                self.assertGreaterEqual(injected_fd, 0)
                                self.assertEqual(injected_close_calls, 1)
                                self.assertEqual(
                                    module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY,
                                    (),
                                )
                                retained = tuple(
                                    module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                                )
                                expected = expected_roots()
                                self.assertEqual(unlock_fds, [])
                                self.assertEqual(
                                    len({id(binding) for binding in retained}),
                                    len(retained),
                                )
                                self.assertTrue(
                                    {id(binding) for binding in expected}
                                    <= {id(binding) for binding in retained}
                                )
                                for binding in expected:
                                    metadata = real_fstat(binding.fd)
                                    identity = (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                        stat.S_IFMT(metadata.st_mode),
                                    )
                                    if binding is target_binding and close_after_effect:
                                        self.assertEqual(identity, sentinel_identity)
                                        self.assertNotEqual(identity, binding.identity)
                                    else:
                                        self.assertEqual(identity, binding.identity)
                                    if binding is target_binding:
                                        self.assertEqual(
                                            binding.close_state,
                                            "close-uncertain",
                                        )
                                    else:
                                        self.assertEqual(binding.close_state, "open")

                                locked = tuple(
                                    binding
                                    for binding in expected
                                    if binding.locked
                                    and not (
                                        binding is target_binding
                                        and close_after_effect
                                    )
                                )
                                self.assertTrue(locked)
                                for binding in locked:
                                    assert_lock_contended(binding)

                                with (
                                    mock.patch.object(module.os, "stat") as blocked_stat,
                                    mock.patch.object(module.os, "open") as blocked_open,
                                    mock.patch.object(module.os, "close") as blocked_close,
                                    mock.patch.object(module.os, "fstat") as blocked_fstat,
                                    mock.patch.object(
                                        module.fcntl,
                                        "flock",
                                    ) as blocked_flock,
                                ):
                                    for retry in ("plan", "execute"):
                                        with self.subTest(retry=retry):
                                            with self.assertRaisesRegex(
                                                error_type,
                                                "close remains uncertain",
                                            ):
                                                if retry == "plan":
                                                    module.plan_private_control_recovery(
                                                        root_id,
                                                        self.root
                                                        / "blocked-root-close-plan.json",
                                                    )
                                                else:
                                                    module.execute_private_control_recovery(
                                                        root_id,
                                                        plan_path,
                                                    )
                                    blocked_stat.assert_not_called()
                                    blocked_open.assert_not_called()
                                    blocked_close.assert_not_called()
                                    blocked_fstat.assert_not_called()
                                    blocked_flock.assert_not_called()
                                self.assertEqual(injected_close_calls, 1)
                                if close_after_effect:
                                    metadata = real_fstat(injected_fd)
                                    self.assertEqual(
                                        (
                                            metadata.st_dev,
                                            metadata.st_ino,
                                            stat.S_IFMT(metadata.st_mode),
                                        ),
                                        sentinel_identity,
                                    )
                        finally:
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                            module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                            cleanup_bindings = (
                                *captured.get("primary", ()),
                                *captured.get("legacy", ()),
                                *retained,
                            )
                            closed: set[int] = set()
                            for binding in cleanup_bindings:
                                if binding.fd < 0 or binding.fd in closed:
                                    continue
                                closed.add(binding.fd)
                                try:
                                    real_close(binding.fd)
                                except OSError:
                                    pass
                            for binding in cleanup_bindings:
                                binding.fd = -1

    def test_pending_writer_is_registered_before_first_fstat(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for reuse_existing in (False, True):
                with self.subTest(
                    module=module.__name__,
                    reuse_existing=reuse_existing,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        receipt_name,
                        _marker_name,
                        _error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-fstat-"
                        f"{int(reuse_existing)}.json"
                    )
                    real_fstat = os.fstat
                    injected = False
                    expected_reuse_identity: tuple[int, int, int] | None = None
                    expected_writer_path: Path | None = None

                    def interrupt_first_writer_fstat(file_fd: int) -> os.stat_result:
                        nonlocal injected
                        flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                        if not injected and flags & os.O_ACCMODE == os.O_WRONLY:
                            injected = True
                            custody = tuple(
                                module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                            )
                            matching = tuple(
                                item for item in custody if item.fd == file_fd
                            )
                            self.assertEqual(len(matching), 1)
                            self.assertEqual(matching[0].state, "open")
                            if expected_writer_path is None:
                                self.assertEqual(
                                    matching[0].path.parent,
                                    self.primary_parent,
                                )
                                self.assertTrue(
                                    matching[0].path.name.startswith(
                                        f".{receipt_name}.pending-"
                                    )
                                )
                            else:
                                self.assertEqual(
                                    matching[0].path,
                                    expected_writer_path,
                                )
                            if expected_reuse_identity is not None:
                                self.assertEqual(
                                    matching[0].identity,
                                    expected_reuse_identity,
                                )
                                read_custody = tuple(
                                    item
                                    for item in custody
                                    if item.path == expected_writer_path
                                    and item.fd != file_fd
                                    and item.state == "open"
                                )
                                self.assertEqual(len(read_custody), 1)
                                self.assertEqual(
                                    read_custody[0].identity,
                                    expected_reuse_identity,
                                )
                            raise KeyboardInterrupt(
                                "simulated observation failure after writer open"
                            )
                        return real_fstat(file_fd)

                    with self._recovery_module_scope(module):
                        plan = module.plan_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                        if reuse_existing:
                            pending = self.primary_parent / (
                                f".{receipt_name}.pending-"
                                f"{plan['plan_digest']}-{'b' * 32}"
                            )
                            pending.write_bytes(b"retained partial bytes\n")
                            pending.chmod(0o400)
                            metadata = pending.stat()
                            expected_reuse_identity = (
                                metadata.st_dev,
                                metadata.st_ino,
                                stat.S_IFMT(metadata.st_mode),
                            )
                            expected_writer_path = pending
                        with (
                            mock.patch.object(
                                module.os,
                                "fstat",
                                side_effect=interrupt_first_writer_fstat,
                            ),
                            self.assertRaises(KeyboardInterrupt),
                        ):
                            module.execute_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                        self.assertTrue(injected)
                        self.assertEqual(
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY,
                            (),
                        )
                        self.assertEqual(module._PC_RECOVERY_RETAINED_CLOSE_FENCE, ())

    def test_pending_reuse_keeps_inode_pinned_through_writer_open(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                plan_path = self.root / f"{module.__name__}-pending-replace.json"
                real_open = os.open
                real_close = os.close
                replacement_identity: tuple[int, int, int] | None = None
                injected = False

                with self._recovery_module_scope(module):
                    plan = module.plan_private_control_recovery(root_id, plan_path)
                    pending = self.primary_parent / (
                        f".{receipt_name}.pending-"
                        f"{plan['plan_digest']}-{'c' * 32}"
                    )
                    partial_payload = b"retained partial bytes\n"
                    replacement_payload = b"replacement must remain untouched\n"
                    pending.write_bytes(partial_payload)
                    pending.chmod(0o400)
                    initial_metadata = pending.stat()
                    initial_identity = (
                        initial_metadata.st_dev,
                        initial_metadata.st_ino,
                        stat.S_IFMT(initial_metadata.st_mode),
                    )

                    def replace_before_writer_open(
                        path: object,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal injected
                        nonlocal replacement_identity
                        if (
                            not injected
                            and path == pending.name
                            and dir_fd is not None
                            and flags & os.O_ACCMODE == os.O_WRONLY
                        ):
                            injected = True
                            custodies = tuple(
                                module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                            )
                            read_custody = tuple(
                                custody
                                for custody in custodies
                                if custody.state == "open" and custody.fd >= 0
                            )
                            opening_custody = tuple(
                                custody
                                for custody in custodies
                                if custody.state == "opening" and custody.fd < 0
                            )
                            self.assertEqual(len(read_custody), 1)
                            self.assertEqual(len(opening_custody), 1)
                            pinned = os.fstat(read_custody[0].fd)
                            self.assertEqual(
                                (
                                    pinned.st_dev,
                                    pinned.st_ino,
                                    stat.S_IFMT(pinned.st_mode),
                                ),
                                initial_identity,
                            )
                            pending.unlink()
                            replacement_fd = real_open(
                                path,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=dir_fd,
                            )
                            try:
                                os.write(replacement_fd, replacement_payload)
                                os.fchmod(replacement_fd, 0o600)
                                replacement = os.fstat(replacement_fd)
                                replacement_identity = (
                                    replacement.st_dev,
                                    replacement.st_ino,
                                    stat.S_IFMT(replacement.st_mode),
                                )
                            finally:
                                real_close(replacement_fd)
                            self.assertNotEqual(replacement_identity, initial_identity)
                        return real_open(path, flags, mode, dir_fd=dir_fd)

                    with (
                        mock.patch.object(
                            module.os,
                            "open",
                            side_effect=replace_before_writer_open,
                        ),
                        self.assertRaisesRegex(error_type, "changed before rewrite"),
                    ):
                        module.execute_private_control_recovery(root_id, plan_path)
                    self.assertTrue(injected)
                    self.assertNotEqual(replacement_identity, initial_identity)
                    replacement_metadata = pending.stat()
                    self.assertEqual(
                        (
                            replacement_metadata.st_dev,
                            replacement_metadata.st_ino,
                            stat.S_IFMT(replacement_metadata.st_mode),
                        ),
                        replacement_identity,
                    )
                    self.assertEqual(pending.read_bytes(), replacement_payload)
                    self.assertEqual(stat.S_IMODE(replacement_metadata.st_mode), 0o600)
                    self.assertFalse((self.primary_parent / receipt_name).exists())
                    self.assertFalse((self.primary_parent / marker_name).exists())
                    self.assertEqual(
                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY,
                        (),
                    )
                    self.assertEqual(module._PC_RECOVERY_RETAINED_CLOSE_FENCE, ())

    def test_pending_open_result_uncertainty_installs_process_fence(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for reuse_existing in (False, True):
                with self.subTest(
                    module=module.__name__,
                    reuse_existing=reuse_existing,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        receipt_name,
                        _marker_name,
                        error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-open-result-"
                        f"{int(reuse_existing)}.json"
                    )
                    real_open = os.open
                    real_close = os.close
                    leaked_raw_fd = -1

                    def interrupt_after_open_result(
                        path: object,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        nonlocal leaked_raw_fd
                        if (
                            leaked_raw_fd < 0
                            and dir_fd is not None
                            and flags & os.O_ACCMODE == os.O_WRONLY
                        ):
                            leaked_raw_fd = real_open(
                                path,
                                flags,
                                mode,
                                dir_fd=dir_fd,
                            )
                            raise KeyboardInterrupt(
                                "simulated async exception after open result"
                            )
                        return real_open(path, flags, mode, dir_fd=dir_fd)

                    retained: tuple[object, ...] = ()
                    try:
                        with self._recovery_module_scope(module):
                            plan = module.plan_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                            if reuse_existing:
                                pending = self.primary_parent / (
                                    f".{receipt_name}.pending-"
                                    f"{plan['plan_digest']}-{'d' * 32}"
                                )
                                pending.write_bytes(b"retained partial bytes\n")
                                pending.chmod(0o400)
                            with (
                                mock.patch.object(
                                    module.os,
                                    "open",
                                    side_effect=interrupt_after_open_result,
                                ),
                                self.assertRaises(KeyboardInterrupt),
                            ):
                                module.execute_private_control_recovery(
                                    root_id,
                                    plan_path,
                                )
                            self.assertGreaterEqual(leaked_raw_fd, 0)
                            flags = fcntl.fcntl(leaked_raw_fd, fcntl.F_GETFL)
                            self.assertEqual(flags & os.O_ACCMODE, os.O_WRONLY)
                            custody = tuple(
                                module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                            )
                            retained = tuple(
                                module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                            )
                            self.assertEqual(len(custody), 1)
                            self.assertEqual(custody[0].fd, -1)
                            self.assertEqual(
                                custody[0].state,
                                "open-result-uncertain",
                            )
                            self.assertGreaterEqual(len(retained), 4)
                            with mock.patch.object(
                                module.os,
                                "open",
                                side_effect=AssertionError(
                                    "open-result fence gate must precede any open"
                                ),
                            ):
                                with self.assertRaisesRegex(
                                    error_type,
                                    "close remains uncertain",
                                ):
                                    module.plan_private_control_recovery(
                                        root_id,
                                        self.root / "blocked-open-result-plan.json",
                                    )
                                with self.assertRaisesRegex(
                                    error_type,
                                    "close remains uncertain",
                                ):
                                    module.execute_private_control_recovery(
                                        root_id,
                                        plan_path,
                                    )
                            fcntl.fcntl(leaked_raw_fd, fcntl.F_GETFL)
                    finally:
                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                        module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                        if leaked_raw_fd >= 0:
                            real_close(leaked_raw_fd)
                        if retained:
                            module._pc_recovery_close_bindings(retained)

    def test_pending_open_oserror_after_effect_installs_process_fence(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            with self.subTest(module=module.__name__):
                if self.primary_parent.exists():
                    shutil.rmtree(self.primary_parent)
                self.primary_parent.mkdir(mode=0o700)
                (
                    _primary_spec,
                    root_id,
                    receipt_name,
                    _marker_name,
                    error_type,
                ) = self._recovery_module_contract(module)
                real_open = os.open
                real_close = os.close
                leaked_raw_fd = -1
                parent = None

                def fail_after_open_result(
                    path: object,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal leaked_raw_fd
                    leaked_raw_fd = real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    raise OSError(
                        errno.EIO,
                        "simulated OSError after open returned a descriptor",
                    )

                try:
                    with self._recovery_module_scope(module):
                        parent = module._pc_recovery_bind_directory(
                            self.primary_parent,
                            "test pending publication parent",
                        )
                        pending_name = (
                            f".{receipt_name}.pending-{'f' * 64}-{'0' * 32}"
                        )
                        flags = (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_CLOEXEC", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                        )
                        with (
                            mock.patch.object(
                                module.os,
                                "open",
                                side_effect=fail_after_open_result,
                            ),
                            self.assertRaisesRegex(
                                OSError,
                                "after open returned a descriptor",
                            ),
                        ):
                            module._pc_recovery_open_pending_descriptor(
                                parent,
                                pending_name,
                                "primary recovery receipt",
                                flags,
                                0o600,
                                None,
                                None,
                            )
                        self.assertGreaterEqual(leaked_raw_fd, 0)
                        custody = tuple(
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                        )
                        self.assertEqual(len(custody), 1)
                        self.assertEqual(custody[0].fd, -1)
                        self.assertEqual(
                            custody[0].state,
                            "open-result-uncertain",
                        )
                        with mock.patch.object(
                            module.os,
                            "open",
                            side_effect=AssertionError(
                                "open-result fence gate must precede any open"
                            ),
                        ):
                            with self.assertRaisesRegex(
                                error_type,
                                "close remains uncertain",
                            ):
                                module.plan_private_control_recovery(
                                    root_id,
                                    self.root / "blocked-oserror-plan.json",
                                )
                finally:
                    module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                    module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                    if leaked_raw_fd >= 0:
                        real_close(leaked_raw_fd)
                    if parent is not None:
                        module._pc_recovery_close_bindings((parent,))

    def test_pending_writer_reader_handoff_rejects_close_hook_replacement(
        self,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for reuse_existing in (False, True):
                with self.subTest(
                    module=module.__name__,
                    reuse_existing=reuse_existing,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        receipt_name,
                        marker_name,
                        error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-close-replace-"
                        f"{int(reuse_existing)}.json"
                    )
                    real_open = os.open
                    real_close = os.close
                    injected = False
                    expected_writer_path: Path | None = None
                    replacement_path: Path | None = None
                    replacement_identity: tuple[int, int, int] | None = None
                    replacement_payload: bytes | None = None

                    with self._recovery_module_scope(module):
                        plan = module.plan_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                        if reuse_existing:
                            pending = self.primary_parent / (
                                f".{receipt_name}.pending-"
                                f"{plan['plan_digest']}-{'1' * 32}"
                            )
                            pending.write_bytes(b"retained partial bytes\n")
                            pending.chmod(0o400)
                            expected_writer_path = pending

                        def replace_after_writer_close(file_fd: int) -> None:
                            nonlocal injected
                            nonlocal replacement_identity
                            nonlocal replacement_path
                            nonlocal replacement_payload
                            flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                            matching = tuple(
                                item
                                for item in module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                                if item.fd == file_fd
                            )
                            if (
                                not injected
                                and flags & os.O_ACCMODE == os.O_WRONLY
                                and len(matching) == 1
                                and matching[0].state == "close-uncertain"
                            ):
                                injected = True
                                writer_path = matching[0].path
                                replacement_path = writer_path
                                if expected_writer_path is not None:
                                    self.assertEqual(
                                        writer_path,
                                        expected_writer_path,
                                    )
                                real_close(file_fd)
                                writer_path.unlink()
                                replacement_fd = real_open(
                                    writer_path,
                                    os.O_WRONLY
                                    | os.O_CREAT
                                    | os.O_EXCL
                                    | getattr(os, "O_CLOEXEC", 0)
                                    | getattr(os, "O_NOFOLLOW", 0),
                                    0o600,
                                )
                                try:
                                    metadata = os.fstat(replacement_fd)
                                    replacement_identity = (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                        stat.S_IFMT(metadata.st_mode),
                                    )
                                    replacement_payload = module._pc_recovery_json_bytes(
                                        module._pc_recovery_primary_receipt_document(
                                            plan,
                                            replacement_identity,
                                        ),
                                        pretty=True,
                                    )
                                    offset = 0
                                    while offset < len(replacement_payload):
                                        offset += os.write(
                                            replacement_fd,
                                            replacement_payload[offset:],
                                        )
                                    os.fchmod(replacement_fd, 0o400)
                                    os.fsync(replacement_fd)
                                finally:
                                    real_close(replacement_fd)
                                return
                            real_close(file_fd)

                        with (
                            mock.patch.object(
                                module.os,
                                "close",
                                side_effect=replace_after_writer_close,
                            ),
                            self.assertRaises(error_type),
                        ):
                            module.execute_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                        self.assertTrue(injected)
                        self.assertIsNotNone(replacement_identity)
                        self.assertIsNotNone(replacement_path)
                        self.assertIsNotNone(replacement_payload)
                        replacement_metadata = replacement_path.stat()
                        self.assertEqual(
                            (
                                replacement_metadata.st_dev,
                                replacement_metadata.st_ino,
                                stat.S_IFMT(replacement_metadata.st_mode),
                            ),
                            replacement_identity,
                        )
                        self.assertEqual(
                            replacement_path.read_bytes(),
                            replacement_payload,
                        )
                        self.assertEqual(
                            stat.S_IMODE(replacement_metadata.st_mode),
                            0o400,
                        )
                        self.assertFalse(
                            (self.primary_parent / receipt_name).exists()
                        )
                        self.assertFalse(
                            (self.primary_parent / marker_name).exists()
                        )
                        self.assertEqual(
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY,
                            (),
                        )
                        self.assertEqual(
                            module._PC_RECOVERY_RETAINED_CLOSE_FENCE,
                            (),
                        )

    def test_pending_verifier_close_uncertainty_retains_process_fence(
        self,
    ) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for close_after_effect in (False, True):
                with self.subTest(
                    module=module.__name__,
                    close_after_effect=close_after_effect,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        receipt_name,
                        _marker_name,
                        error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-verifier-close-"
                        f"{int(close_after_effect)}.json"
                    )
                    real_open = os.open
                    real_close = os.close
                    writer_close_seen = False
                    verifier_fd = -1
                    verifier_close_calls = 0
                    sentinel_fd = -1
                    sentinel_identity: tuple[int, int, int] | None = None
                    custody: tuple[object, ...] = ()
                    retained: tuple[object, ...] = ()

                    def inject_verifier_close(file_fd: int) -> None:
                        nonlocal writer_close_seen
                        nonlocal verifier_fd
                        nonlocal verifier_close_calls
                        nonlocal sentinel_fd
                        nonlocal sentinel_identity
                        matching = tuple(
                            item
                            for item in module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                            if item.fd == file_fd
                        )
                        flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                        access_mode = flags & os.O_ACCMODE
                        if (
                            not writer_close_seen
                            and access_mode == os.O_WRONLY
                            and len(matching) == 1
                            and matching[0].state == "close-uncertain"
                        ):
                            writer_close_seen = True
                            writer_path = matching[0].path
                            real_close(file_fd)
                            writer_path.unlink()
                            replacement_fd = real_open(
                                writer_path,
                                os.O_WRONLY
                                | os.O_CREAT
                                | os.O_EXCL
                                | getattr(os, "O_CLOEXEC", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                                0o600,
                            )
                            try:
                                metadata = os.fstat(replacement_fd)
                                replacement_identity = (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                    stat.S_IFMT(metadata.st_mode),
                                )
                                payload = module._pc_recovery_json_bytes(
                                    module._pc_recovery_primary_receipt_document(
                                        plan,
                                        replacement_identity,
                                    ),
                                    pretty=True,
                                )
                                offset = 0
                                while offset < len(payload):
                                    offset += os.write(
                                        replacement_fd,
                                        payload[offset:],
                                    )
                                os.fchmod(replacement_fd, 0o400)
                                os.fsync(replacement_fd)
                            finally:
                                real_close(replacement_fd)
                            return
                        if (
                            writer_close_seen
                            and access_mode == os.O_RDONLY
                            and len(matching) == 1
                            and matching[0].state == "close-uncertain"
                        ):
                            if verifier_fd >= 0 and file_fd == verifier_fd:
                                verifier_close_calls += 1
                                raise AssertionError(
                                    "close-uncertain verifier was closed again"
                                )
                            verifier_fd = file_fd
                            verifier_close_calls = 1
                            if close_after_effect:
                                real_close(file_fd)
                                sentinel_path = self.root / (
                                    f"{module.__name__}-verifier-sentinel-"
                                    f"{int(close_after_effect)}"
                                )
                                sentinel_path.write_bytes(
                                    b"unique verifier close sentinel\n"
                                )
                                sentinel_path.chmod(0o600)
                                replacement_fd = real_open(
                                    sentinel_path,
                                    os.O_RDONLY,
                                )
                                sentinel_path.unlink()
                                if replacement_fd != file_fd:
                                    os.dup2(replacement_fd, file_fd)
                                    real_close(replacement_fd)
                                sentinel_fd = file_fd
                                metadata = os.fstat(sentinel_fd)
                                sentinel_identity = (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                    stat.S_IFMT(metadata.st_mode),
                                )
                            raise OSError(
                                errno.EIO,
                                "simulated pending verifier close uncertainty",
                            )
                        real_close(file_fd)

                    try:
                        with self._recovery_module_scope(module):
                            plan = module.plan_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                            with mock.patch.object(
                                module.os,
                                "close",
                                side_effect=inject_verifier_close,
                            ):
                                with self.assertRaisesRegex(
                                    error_type,
                                    "pending .* verifier descriptor",
                                ):
                                    module.execute_private_control_recovery(
                                        root_id,
                                        plan_path,
                                    )
                                custody = tuple(
                                    module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                                )
                                retained = tuple(
                                    module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                                )
                                self.assertTrue(writer_close_seen)
                                self.assertGreaterEqual(verifier_fd, 0)
                                self.assertEqual(verifier_close_calls, 1)
                                self.assertEqual(len(custody), 1)
                                self.assertEqual(custody[0].fd, verifier_fd)
                                self.assertEqual(
                                    custody[0].state,
                                    "close-uncertain",
                                )
                                self.assertGreaterEqual(len(retained), 4)
                                with mock.patch.object(
                                    module.os,
                                    "open",
                                    side_effect=AssertionError(
                                        "verifier close fence must precede any open"
                                    ),
                                ):
                                    with self.assertRaisesRegex(
                                        error_type,
                                        "close remains uncertain",
                                    ):
                                        module.plan_private_control_recovery(
                                            root_id,
                                            self.root / "blocked-verifier-plan.json",
                                        )
                                    with self.assertRaisesRegex(
                                        error_type,
                                        "close remains uncertain",
                                    ):
                                        module.execute_private_control_recovery(
                                            root_id,
                                            plan_path,
                                        )
                                self.assertEqual(verifier_close_calls, 1)
                                if close_after_effect:
                                    metadata = os.fstat(sentinel_fd)
                                    self.assertEqual(
                                        (
                                            metadata.st_dev,
                                            metadata.st_ino,
                                            stat.S_IFMT(metadata.st_mode),
                                        ),
                                        sentinel_identity,
                                    )
                    finally:
                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                        module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                        for binding in retained:
                            if binding.fd == verifier_fd:
                                binding.fd = -1
                        if verifier_fd >= 0 and not close_after_effect:
                            real_close(verifier_fd)
                        if sentinel_fd >= 0:
                            real_close(sentinel_fd)
                        if retained:
                            module._pc_recovery_close_bindings(retained)

    def test_pending_reader_custody_survives_rename_failure(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for close_after_effect in (False, True):
                with self.subTest(
                    module=module.__name__,
                    close_after_effect=close_after_effect,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        _receipt_name,
                        _marker_name,
                        error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-rename-close-"
                        f"{int(close_after_effect)}.json"
                    )
                    rename_helper = (
                        "_rename_directory_entry_noreplace"
                        if module is MIRROR_MODULE
                        else "_rename_noreplace_at"
                    )
                    real_open = os.open
                    real_close = os.close
                    verifier_fd = -1
                    verifier_close_calls = 0
                    sentinel_fd = -1
                    sentinel_identity: tuple[int, int, int] | None = None
                    custody: tuple[object, ...] = ()
                    retained: tuple[object, ...] = ()

                    def inject_reader_close(file_fd: int) -> None:
                        nonlocal verifier_fd
                        nonlocal verifier_close_calls
                        nonlocal sentinel_fd
                        nonlocal sentinel_identity
                        matching = tuple(
                            item
                            for item in module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                            if item.fd == file_fd
                        )
                        flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                        if (
                            flags & os.O_ACCMODE == os.O_RDONLY
                            and len(matching) == 1
                            and matching[0].state == "close-uncertain"
                        ):
                            if verifier_fd >= 0 and file_fd == verifier_fd:
                                verifier_close_calls += 1
                                raise AssertionError(
                                    "rename-failure reader was closed again"
                                )
                            verifier_fd = file_fd
                            verifier_close_calls = 1
                            if close_after_effect:
                                real_close(file_fd)
                                sentinel_path = self.root / (
                                    f"{module.__name__}-rename-reader-sentinel-"
                                    f"{int(close_after_effect)}"
                                )
                                sentinel_path.write_bytes(
                                    b"unique rename reader close sentinel\n"
                                )
                                sentinel_path.chmod(0o600)
                                replacement_fd = real_open(
                                    sentinel_path,
                                    os.O_RDONLY,
                                )
                                sentinel_path.unlink()
                                if replacement_fd != file_fd:
                                    os.dup2(replacement_fd, file_fd)
                                    real_close(replacement_fd)
                                sentinel_fd = file_fd
                                metadata = os.fstat(sentinel_fd)
                                sentinel_identity = (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                    stat.S_IFMT(metadata.st_mode),
                                )
                            raise OSError(
                                errno.EIO,
                                "simulated rename-failure reader close uncertainty",
                            )
                        real_close(file_fd)

                    try:
                        with self._recovery_module_scope(module):
                            module.plan_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                            with (
                                mock.patch.object(
                                    module,
                                    rename_helper,
                                    side_effect=OSError(
                                        errno.EIO,
                                        "simulated pending publication rename failure",
                                    ),
                                ),
                                mock.patch.object(
                                    module.os,
                                    "close",
                                    side_effect=inject_reader_close,
                                ),
                            ):
                                with self.assertRaisesRegex(
                                    error_type,
                                    "pending .* verifier descriptor",
                                ):
                                    module.execute_private_control_recovery(
                                        root_id,
                                        plan_path,
                                    )
                                custody = tuple(
                                    module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                                )
                                retained = tuple(
                                    module._PC_RECOVERY_RETAINED_CLOSE_FENCE
                                )
                                self.assertEqual(verifier_close_calls, 1)
                                self.assertEqual(len(custody), 1)
                                self.assertEqual(custody[0].fd, verifier_fd)
                                self.assertEqual(
                                    custody[0].state,
                                    "close-uncertain",
                                )
                                self.assertGreaterEqual(len(retained), 4)
                                with mock.patch.object(
                                    module.os,
                                    "open",
                                    side_effect=AssertionError(
                                        "rename close fence must precede any open"
                                    ),
                                ):
                                    with self.assertRaisesRegex(
                                        error_type,
                                        "close remains uncertain",
                                    ):
                                        module.plan_private_control_recovery(
                                            root_id,
                                            self.root / "blocked-rename-plan.json",
                                        )
                                    with self.assertRaisesRegex(
                                        error_type,
                                        "close remains uncertain",
                                    ):
                                        module.execute_private_control_recovery(
                                            root_id,
                                            plan_path,
                                        )
                                self.assertEqual(verifier_close_calls, 1)
                                if close_after_effect:
                                    metadata = os.fstat(sentinel_fd)
                                    self.assertEqual(
                                        (
                                            metadata.st_dev,
                                            metadata.st_ino,
                                            stat.S_IFMT(metadata.st_mode),
                                        ),
                                        sentinel_identity,
                                    )
                    finally:
                        module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = ()
                        module._PC_RECOVERY_RETAINED_CLOSE_FENCE = ()
                        for binding in retained:
                            if binding.fd == verifier_fd:
                                binding.fd = -1
                        if verifier_fd >= 0 and not close_after_effect:
                            real_close(verifier_fd)
                        if sentinel_fd >= 0:
                            real_close(sentinel_fd)
                        if retained:
                            module._pc_recovery_close_bindings(retained)

    def test_pending_activation_failure_closes_known_writer(self) -> None:
        for module in (MIRROR_MODULE, ENGINE_MODULE):
            for reuse_existing in (False, True):
                with self.subTest(
                    module=module.__name__,
                    reuse_existing=reuse_existing,
                ):
                    if self.primary_parent.exists():
                        shutil.rmtree(self.primary_parent)
                    self.primary_parent.mkdir(mode=0o700)
                    (
                        _primary_spec,
                        root_id,
                        receipt_name,
                        _marker_name,
                        _error_type,
                    ) = self._recovery_module_contract(module)
                    plan_path = self.root / (
                        f"{module.__name__}-pending-activation-"
                        f"{int(reuse_existing)}.json"
                    )
                    real_activate = module._pc_recovery_activate_pending_descriptor
                    interrupted_fd = -1

                    def interrupt_writer_activation(custody: object, file_fd: int):
                        nonlocal interrupted_fd
                        flags = fcntl.fcntl(file_fd, fcntl.F_GETFL)
                        if (
                            interrupted_fd < 0
                            and flags & os.O_ACCMODE == os.O_WRONLY
                        ):
                            interrupted_fd = file_fd
                            raise KeyboardInterrupt(
                                "simulated activation failure after open"
                            )
                        return real_activate(custody, file_fd)

                    with self._recovery_module_scope(module):
                        plan = module.plan_private_control_recovery(
                            root_id,
                            plan_path,
                        )
                        if reuse_existing:
                            pending = self.primary_parent / (
                                f".{receipt_name}.pending-"
                                f"{plan['plan_digest']}-{'e' * 32}"
                            )
                            pending.write_bytes(b"retained partial bytes\n")
                            pending.chmod(0o400)
                        with (
                            mock.patch.object(
                                module,
                                "_pc_recovery_activate_pending_descriptor",
                                side_effect=interrupt_writer_activation,
                            ),
                            self.assertRaises(KeyboardInterrupt),
                        ):
                            module.execute_private_control_recovery(
                                root_id,
                                plan_path,
                            )
                        self.assertGreaterEqual(interrupted_fd, 0)
                        with self.assertRaises(OSError):
                            os.fstat(interrupted_fd)
                        self.assertEqual(
                            module._PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY,
                            (),
                        )
                        self.assertEqual(module._PC_RECOVERY_RETAINED_CLOSE_FENCE, ())

    def test_terminal_verification_rejects_in_place_receipt_content_drift(
        self,
    ) -> None:
        plan_path, _plan = self._plan()
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        receipt_path = (
            self.primary_parent / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
        )
        original_plan = MIRROR_MODULE._pc_recovery_plan_from_bindings
        call_count = 0

        def mutate_after_manifest(*args: object, **kwargs: object):
            nonlocal call_count
            result = original_plan(*args, **kwargs)
            call_count += 1
            if call_count == 2:
                payload = bytearray(receipt_path.read_bytes())
                payload[len(payload) // 2] ^= 1
                receipt_path.chmod(0o600)
                receipt_path.write_bytes(payload)
                receipt_path.chmod(0o400)
            return result

        with mock.patch.object(
            MIRROR_MODULE,
            "_pc_recovery_plan_from_bindings",
            side_effect=mutate_after_manifest,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "receipt changed during verification",
            ):
                MIRROR_MODULE.execute_private_control_recovery(
                    MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    plan_path,
                )

    def test_terminal_verification_rejects_marker_schema_expansion(self) -> None:
        plan_path, _plan = self._plan()
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        marker_path = (
            self.primary_parent / MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["terminal_registry"]["roots"][0]["future"] = True
        marker["terminal_registry"]["digest"] = MIRROR_MODULE._pc_recovery_digest(
            marker["terminal_registry"]["roots"]
        )
        marker_path.chmod(0o600)
        marker_path.write_bytes(
            MIRROR_MODULE._pc_recovery_json_bytes(marker, pretty=True)
        )
        marker_path.chmod(0o400)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "terminal registry is invalid",
        ):
            MIRROR_MODULE.execute_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )

    def test_generator_preflight_accepts_only_verified_retained_cutover(self) -> None:
        plan_path, _plan = self._plan()
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        before = self._snapshot()
        prebinding = MIRROR_MODULE._prebind_existing_primary_private_control_root(
            self.root_specs[0]
        )
        receipts = ()
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_validate_private_control_root_topology",
            ):
                states, receipts = (
                    MIRROR_MODULE._preflight_legacy_private_control_roots_once(
                        mock.Mock(operation=None),
                        mock.Mock(),
                        mock.Mock(),
                        prebinding,
                    )
                )
            self.assertEqual(states, ("adopted-retained-in-place",))
            self.assertEqual(receipts[0].state, "adopted-retained-in-place")
            self.assertTrue(receipts[0].adoption_plan_digest)
            MIRROR_MODULE._revalidate_legacy_private_control_receipts(
                receipts,
                operation=None,
            )
            self.assertEqual(
                MIRROR_MODULE._private_control_preallocation_decision(states),
                (True, None),
            )
            self.assertEqual(before, self._snapshot())
        finally:
            if receipts:
                MIRROR_MODULE._release_legacy_private_control_receipts(receipts)
            MIRROR_MODULE._close_control_bindings_best_effort(
                (prebinding.parent, prebinding.home)
            )

    def test_generator_preflight_verifies_adoption_above_ordinary_cap(self) -> None:
        existing_count = len(tuple(os.scandir(self.tool_root)))
        target_count = MIRROR_MODULE.MAX_TOOL_ROOT_ENTRIES + 1
        for index in range(target_count - existing_count):
            filler = self.tool_root / f"retained-extra-{index:04d}.bin"
            filler.write_bytes(f"retained-{index}\n".encode())
            filler.chmod(0o600)
        self.assertEqual(len(tuple(os.scandir(self.tool_root))), target_count)
        plan_path, plan = self._plan("above-ordinary-cap.json")
        self.assertGreater(
            plan["inventory"]["entry_count"],
            MIRROR_MODULE.MAX_TOOL_ROOT_ENTRIES,
        )
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        before = self._snapshot()
        prebinding = MIRROR_MODULE._prebind_existing_primary_private_control_root(
            self.root_specs[0]
        )
        receipts = ()
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_validate_private_control_root_topology",
            ):
                states, receipts = (
                    MIRROR_MODULE._preflight_legacy_private_control_roots_once(
                        mock.Mock(operation=None),
                        mock.Mock(),
                        mock.Mock(),
                        prebinding,
                    )
                )
            self.assertEqual(states, ("adopted-retained-in-place",))
            self.assertEqual(receipts[0].state, "adopted-retained-in-place")
            self.assertEqual(
                receipts[0].adoption_plan_digest,
                plan["plan_digest"],
            )
            MIRROR_MODULE._revalidate_legacy_private_control_receipts(
                receipts,
                operation=None,
            )
            self.assertEqual(
                MIRROR_MODULE._private_control_preallocation_decision(states),
                (True, None),
            )
            self.assertEqual(before, self._snapshot())
        finally:
            if receipts:
                MIRROR_MODULE._release_legacy_private_control_receipts(receipts)
            MIRROR_MODULE._close_control_bindings_best_effort(
                (prebinding.parent, prebinding.home)
            )

    def test_generator_adoption_rejects_expired_operation_budget(self) -> None:
        plan_path, _plan = self._plan("expired-operation-budget.json")
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        prebinding = MIRROR_MODULE._prebind_existing_primary_private_control_root(
            self.root_specs[0]
        )
        operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() - 1,
            remaining_bytes=MIRROR_MODULE.MAX_OPERATION_BYTES,
            remaining_entries=MIRROR_MODULE.MAX_OPERATION_ENTRIES,
        )
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_validate_private_control_root_topology",
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_pc_recovery_read_bound_file",
                    side_effect=AssertionError(
                        "expired operation reached fixed-document I/O"
                    ),
                ) as read_bound_file,
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "mirror operation exceeded",
                ),
            ):
                MIRROR_MODULE._preflight_legacy_private_control_roots_once(
                    mock.Mock(operation=operation),
                    mock.Mock(),
                    mock.Mock(),
                    prebinding,
                )
            read_bound_file.assert_not_called()
        finally:
            MIRROR_MODULE._close_control_bindings_best_effort(
                (prebinding.parent, prebinding.home)
            )

    def test_generator_adoption_revalidations_share_operation_budget(self) -> None:
        plan_path, plan = self._plan("shared-operation-budget.json")
        MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        inventory = plan["inventory"]
        self.assertIsInstance(inventory, dict)
        entries = inventory["entries"]
        self.assertIsInstance(entries, list)
        scan_name_bytes = sum(
            len(os.fsencode(PurePosixPath(entry["locator"]["path"]).name))
            for entry in entries
        )
        manifest_read_bytes = 2 * inventory["logical_bytes"]
        fixed_document_read_bytes = 4 * sum(
            os.stat(
                self.primary_parent / name,
                follow_symlinks=False,
            ).st_size
            for name in (
                MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                MIRROR_MODULE.PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            )
        )
        operation = MIRROR_MODULE.OperationBudget(
            deadline=time.monotonic() + 60,
            remaining_bytes=(
                scan_name_bytes
                + manifest_read_bytes
                + fixed_document_read_bytes
            ),
            remaining_entries=inventory["entry_count"],
        )
        prebinding = MIRROR_MODULE._prebind_existing_primary_private_control_root(
            self.root_specs[0]
        )
        receipts = ()
        try:
            with mock.patch.object(
                MIRROR_MODULE,
                "_validate_private_control_root_topology",
            ):
                states, receipts = (
                    MIRROR_MODULE._preflight_legacy_private_control_roots_once(
                        mock.Mock(operation=operation),
                        mock.Mock(),
                        mock.Mock(),
                        prebinding,
                    )
                )
            self.assertEqual(states, ("adopted-retained-in-place",))
            self.assertEqual(operation.remaining_bytes, 0)
            self.assertEqual(operation.remaining_entries, 0)
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "mirror operation exceeds.*aggregate budget",
            ):
                MIRROR_MODULE._revalidate_legacy_private_control_receipts(
                    receipts,
                    operation=operation,
                )
        finally:
            if receipts:
                MIRROR_MODULE._release_legacy_private_control_receipts(receipts)
            MIRROR_MODULE._close_control_bindings_best_effort(
                (prebinding.parent, prebinding.home)
            )

    def test_generator_ordinary_cap_still_precedes_stale_recovery(self) -> None:
        prebinding = MIRROR_MODULE._prebind_existing_primary_private_control_root(
            self.root_specs[0]
        )
        recover = mock.Mock(
            side_effect=AssertionError("stale recovery ran before the ordinary cap")
        )
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_validate_private_control_root_topology",
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "MAX_TOOL_ROOT_ENTRIES",
                    1,
                ),
                mock.patch.object(
                    MIRROR_MODULE,
                    "_recover_stale_private_snapshots",
                    recover,
                ),
            ):
                with self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "exceeds 1 entries",
                ):
                    MIRROR_MODULE._preflight_legacy_private_control_roots_once(
                        mock.Mock(operation=None),
                        mock.Mock(),
                        mock.Mock(),
                        prebinding,
                    )
            recover.assert_not_called()
        finally:
            MIRROR_MODULE._close_control_bindings_best_effort(
                (prebinding.parent, prebinding.home)
            )

    def test_execute_rejects_content_and_policy_drift_before_publication(self) -> None:
        plan_path, _plan = self._plan()
        evidence = self.quarantine / ".saved" / "unclassified" / "evidence.bin"
        evidence.write_bytes(b"changed")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "no longer matches|does not match",
        ):
            MIRROR_MODULE.execute_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
        self.assertFalse(self.primary_parent.exists())

    def test_execute_allows_timestamp_only_churn(self) -> None:
        plan_path, _plan = self._plan()
        evidence = self.quarantine / ".saved" / "unclassified" / "evidence.bin"
        future = time.time_ns() + 5_000_000_000
        os.utime(evidence, ns=(future, future), follow_symlinks=False)
        os.utime(evidence.parent, ns=(future, future), follow_symlinks=False)
        executed = MIRROR_MODULE.execute_private_control_recovery(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            plan_path,
        )
        self.assertEqual(executed["status"], "executed")

    def test_execute_rejects_add_remove_rename_and_replacement_drift(self) -> None:
        scenarios = ("add", "remove", "rename", "replace")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                plan_path, _plan = self._plan(f"{scenario}.json")
                target = self.quarantine / ".saved" / "unclassified" / "evidence.bin"
                if scenario == "add":
                    added = self.quarantine / ".saved" / "added.bin"
                    added.write_bytes(b"added")
                    added.chmod(0o600)
                elif scenario == "remove":
                    target.unlink()
                elif scenario == "rename":
                    target.rename(target.with_name("renamed.bin"))
                else:
                    replacement = target.with_name("replacement.bin")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(0o600)
                    target.unlink()
                    replacement.rename(target)
                with self.assertRaises(MIRROR_MODULE.MirrorSyncError):
                    MIRROR_MODULE.execute_private_control_recovery(
                        MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                        plan_path,
                    )
                self.assertFalse(self.primary_parent.exists())
                # Each subtest owns a fresh fixture shape reconstructed in place.
                if scenario == "add":
                    added.unlink()
                elif scenario == "remove":
                    target.write_bytes(b"retained evidence\x00\xff")
                    target.chmod(0o600)
                elif scenario == "rename":
                    target.with_name("renamed.bin").rename(target)
                else:
                    target.write_bytes(b"retained evidence\x00\xff")
                    target.chmod(0o600)

    def test_execute_rejects_chmod_and_observed_chown_drift(self) -> None:
        plan_path, _plan = self._plan("chmod.json")
        target = self.quarantine / ".saved" / "unclassified" / "evidence.bin"
        target.chmod(0o640)
        with self.assertRaises(MIRROR_MODULE.MirrorSyncError):
            MIRROR_MODULE.execute_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                plan_path,
            )
        target.chmod(0o600)

        chown_plan, _plan = self._plan("chown.json")
        target_inode = os.stat(target, follow_symlinks=False).st_ino
        original_access = MIRROR_MODULE._pc_recovery_access

        def changed_group(metadata: os.stat_result) -> tuple[int, int, int]:
            access = original_access(metadata)
            if metadata.st_ino == target_inode:
                return access[0], access[1], access[2] + 1
            return access

        with mock.patch.object(
            MIRROR_MODULE,
            "_pc_recovery_access",
            side_effect=changed_group,
        ):
            with self.assertRaises(MIRROR_MODULE.MirrorSyncError):
                MIRROR_MODULE.execute_private_control_recovery(
                    MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                    chown_plan,
                )

    def test_planning_rejects_busy_writer_and_hardlink_alias(self) -> None:
        tool_fd = os.open(
            self.tool_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            import fcntl

            fcntl.flock(tool_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "busy",
            ):
                self._plan("BUSY.json")
        finally:
            fcntl.flock(tool_fd, fcntl.LOCK_UN)
            os.close(tool_fd)

        source = self.quarantine / ".saved" / "unclassified" / "evidence.bin"
        alias = source.with_name("evidence-alias.bin")
        os.link(source, alias)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "hard-link alias|object alias",
        ):
            self._plan("ALIAS.json")

        alias.unlink()
        external_alias = self.root / "external-evidence-alias.bin"
        os.link(source, external_alias)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "hard-link alias",
        ):
            self._plan("EXTERNAL-ALIAS.json")

    def test_planning_rejects_cross_filesystem_topology_signal(self) -> None:
        original_bind = MIRROR_MODULE._pc_recovery_bind_child_directory

        def cross_device(*args: object, **kwargs: object):
            binding = original_bind(*args, **kwargs)
            if "quarantine" in binding.label:
                binding.identity = (
                    binding.identity[0] + 1,
                    binding.identity[1],
                    binding.identity[2],
                )
            return binding

        with mock.patch.object(
            MIRROR_MODULE,
            "_pc_recovery_bind_child_directory",
            side_effect=cross_device,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "cross filesystems",
            ):
                self._plan("CROSS-FS.json")

    def test_external_plan_io_rejects_symlink_ancestor_aliases(self) -> None:
        write_alias = self.root / "write-alias"
        write_alias.symlink_to(self.quarantine, target_is_directory=True)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "symlink or non-directory",
        ):
            MIRROR_MODULE.plan_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                write_alias / "PLAN.json",
            )
        self.assertFalse((self.quarantine / "PLAN.json").exists())

        plan_path, _plan = self._plan("external-plan.json")
        read_alias = self.root / "read-alias"
        read_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "symlink or non-directory",
        ):
            MIRROR_MODULE.execute_private_control_recovery(
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
                read_alias / plan_path.name,
            )
        self.assertFalse(self.primary_parent.exists())

    def test_planning_rejects_symlink_special_and_caps(self) -> None:
        link = self.quarantine / "unsafe-link"
        link.symlink_to(".saved")
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "symlink/special",
        ):
            self._plan()
        link.unlink()
        fifo = self.quarantine / "unsafe-fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "symlink/special",
        ):
            self._plan("FIFO.json")
        fifo.unlink()
        with mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES",
            1,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "entry cap",
            ):
                self._plan("CAP.json")
        with mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES",
            1,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "logical-byte cap",
            ):
                self._plan("LOGICAL-CAP.json")
        with mock.patch.object(
            MIRROR_MODULE,
            "PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES",
            1,
        ):
            with self.assertRaisesRegex(
                MIRROR_MODULE.MirrorSyncError,
                "allocated-byte cap",
            ):
                self._plan("ALLOCATED-CAP.json")


class MirrorQuarantineContractParityTests(unittest.TestCase):
    def test_owner_record_root_scope_matrix_is_equivalent(self) -> None:
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_VERSION,
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_LEGACY_VERSION,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_FIELDS,
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_LEGACY_FIELDS,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
        )
        common = {
            "owner_pid": 123,
            "owner_uid": 501,
            "owner_gid": 20,
            "owner_nonce": "0123456789abcdef0123456789abcdef",
            "phase": "cleanup",
            "private_name": (
                "sync-canonical-git-control.123.0123456789abcdef0123456789abcdef"
            ),
            "private_identity": [1, 2, stat.S_IFDIR],
        }
        legacy_root = MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID
        primary_root = MIRROR_MODULE.PRIVATE_CONTROL_PRIMARY_ROOT_ID
        scenarios = (
            (
                "v1-legacy",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_VERSION,
                    **common,
                },
                legacy_root,
                "accepted-legacy",
            ),
            (
                "v1-primary",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_LEGACY_VERSION,
                    **common,
                },
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
            (
                "v2-primary-exact",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": primary_root,
                    **common,
                },
                primary_root,
                "accepted-current",
            ),
            (
                "v2-legacy-exact",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": legacy_root,
                    **common,
                },
                legacy_root,
                "accepted-current",
            ),
            (
                "v2-cross-root",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": legacy_root,
                    **common,
                },
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
            (
                "v2-unknown-root",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": "unknown-root-v9",
                    **common,
                },
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
            (
                "v2-missing-root",
                {"version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION, **common},
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
            (
                "v2-extra-field",
                {
                    "version": MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
                    "root_id": primary_root,
                    "future": True,
                    **common,
                },
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
            (
                "unknown-version",
                {"version": 99, **common},
                primary_root,
                MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
            ),
        )
        for label, record, expected_root, expected in scenarios:
            with self.subTest(label=label):
                generator_result = MIRROR_MODULE._private_owner_record_root_scope(
                    record,
                    expected_root,
                )
                engine_result = ENGINE_MODULE._mirror_private_owner_record_root_scope(
                    record,
                    expected_root,
                )
                self.assertEqual(generator_result, expected)
                self.assertEqual(engine_result, expected)
                self.assertEqual(generator_result, engine_result)

    def test_private_control_root_registry_and_scenario_matrix_are_equivalent(
        self,
    ) -> None:
        self.assertEqual(
            tuple(MIRROR_MODULE.PrivateControlRootSpec.__dataclass_fields__),
            tuple(ENGINE_MODULE.MirrorPrivateControlRootSpec.__dataclass_fields__),
        )
        generator_roots = tuple(
            (
                spec.root_id,
                spec.parent_path,
                spec.allocate,
                spec.account_home,
                spec.shared_parent,
            )
            for spec in MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS
        )
        engine_roots = tuple(
            (
                spec.root_id,
                spec.parent_path,
                spec.allocate,
                spec.account_home,
                spec.shared_parent,
            )
            for spec in ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_ROOT_SPECS
        )
        self.assertEqual(generator_roots, engine_roots)
        self.assertEqual(
            [spec.root_id for spec in MIRROR_MODULE.PRIVATE_CONTROL_ROOT_SPECS],
            [
                MIRROR_MODULE.PRIVATE_CONTROL_PRIMARY_ROOT_ID,
                MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_ROOT_ID,
            ],
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_NAMESPACE_NAME,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_NAMESPACE_NAME,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_LEGACY_PARENT,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_LEGACY_PARENT,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_REASON_LEGACY_PENDING,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_REASON_INCONCLUSIVE,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_REASON_INCONCLUSIVE,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_MAX_ANCESTORS,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_MAX_ANCESTORS,
        )
        self.assertEqual(
            MIRROR_MODULE.PRIVATE_CONTROL_PREALLOCATION_ALLOWED_STATES,
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_PREALLOCATION_ALLOWED_STATES,
        )

        selected_uid = 501
        shared_parent_policies = (
            ((0o1777, 0, 0), True),
            ((0o1777, selected_uid, 0), False),
            ((0o0777, 0, 0), False),
            ((0o0700, 0, 0), False),
        )
        for access_policy, expected in shared_parent_policies:
            with self.subTest(access_policy=access_policy, expected=expected):
                self.assertEqual(
                    MIRROR_MODULE._legacy_shared_parent_policy_is_valid(access_policy),
                    expected,
                )
                self.assertEqual(
                    ENGINE_MODULE._mirror_legacy_shared_parent_policy_is_valid(
                        access_policy
                    ),
                    expected,
                )

        def child(owner: int, seed: int):
            return (
                (1, seed, stat.S_IFDIR),
                (0o700, owner, 20),
            )

        ownership_scenarios = (
            ((), "absent"),
            ((child(777, 1),), "foreign-unrelated"),
            ((child(777, 1), child(778, 2)), "foreign-unrelated"),
            ((child(selected_uid, 1),), "same-uid"),
            (
                (child(selected_uid, 1), child(selected_uid, 2)),
                "same-uid",
            ),
            ((child(selected_uid, 1), child(777, 2)), "inconclusive"),
        )
        for children, expected in ownership_scenarios:
            with self.subTest(children=children, expected=expected):
                self.assertEqual(
                    MIRROR_MODULE._private_control_legacy_ownership_state(
                        children,
                        effective_uid=selected_uid,
                    ),
                    expected,
                )
                self.assertEqual(
                    ENGINE_MODULE._mirror_private_control_legacy_ownership_state(
                        children,
                        effective_uid=selected_uid,
                    ),
                    expected,
                )

        allocation_scenarios = (
            ((), (True, None)),
            (("absent",), (True, None)),
            (("adopted-retained-in-place",), (True, None)),
            (("duplicate", "foreign-unrelated"), (True, None)),
            (("same-uid-empty",), (True, None)),
            (
                (MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,),
                (
                    False,
                    MIRROR_MODULE.PRIVATE_CONTROL_REASON_LEGACY_PENDING,
                ),
            ),
            (
                (MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,),
                (
                    False,
                    MIRROR_MODULE.PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH,
                ),
            ),
            (
                ("inconclusive",),
                (False, MIRROR_MODULE.PRIVATE_CONTROL_REASON_INCONCLUSIVE),
            ),
            (("unknown",), (False, MIRROR_MODULE.PRIVATE_CONTROL_REASON_INCONCLUSIVE)),
        )
        for states, expected in allocation_scenarios:
            with self.subTest(states=states, expected=expected):
                self.assertEqual(
                    MIRROR_MODULE._private_control_preallocation_decision(states),
                    expected,
                )
                self.assertEqual(
                    ENGINE_MODULE._mirror_private_control_preallocation_decision(
                        states
                    ),
                    expected,
                )

    def test_retained_recovery_machine_contract_is_equivalent(self) -> None:
        constant_pairs = (
            (
                "PRIVATE_CONTROL_RECOVERY_CONTRACT",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_CONTRACT",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_VERSION",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_VERSION",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_DISPOSITION",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_DISPOSITION",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MARKER_NAME",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_NAME",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_LOCK_ORDER",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_LOCK_ORDER",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_LOCK_MODE",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_LOCK_MODE",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_DEPTH",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_DEPTH",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES",
            ),
            (
                "PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS",
                "MIRROR_PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS",
            ),
        )
        for generator_name, engine_name in constant_pairs:
            with self.subTest(generator=generator_name, engine=engine_name):
                self.assertEqual(
                    getattr(MIRROR_MODULE, generator_name),
                    getattr(ENGINE_MODULE, engine_name),
                )
        self.assertEqual(
            MIRROR_MODULE._PRIVATE_CONTROL_RECOVERY_PLAN_FIELDS,
            ENGINE_MODULE._MIRROR_PRIVATE_CONTROL_RECOVERY_PLAN_FIELDS,
        )
        self.assertEqual(
            MIRROR_MODULE._PRIVATE_CONTROL_RECOVERY_MARKER_FIELDS,
            ENGINE_MODULE._MIRROR_PRIVATE_CONTROL_RECOVERY_MARKER_FIELDS,
        )
        self.assertEqual(
            MIRROR_MODULE._PRIVATE_CONTROL_RECOVERY_PRIMARY_RECEIPT_FIELDS,
            ENGINE_MODULE._MIRROR_PRIVATE_CONTROL_RECOVERY_PRIMARY_RECEIPT_FIELDS,
        )

    def test_scheduler_probe_matches_generator_recovery_contract(self) -> None:
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_CONTROL_PARENT,
            MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_TOOL_ROOT_NAME,
            MIRROR_MODULE.PRIVATE_TOOL_ROOT_NAME,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_DURABLE_QUARANTINE_ROOT_NAME,
            MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_DURABLE_QUARANTINE_ENTRY_LIMIT,
            MIRROR_MODULE.MAX_DURABLE_QUARANTINE_ENTRIES,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_TOOL_ROOT_ENTRY_LIMIT,
            MIRROR_MODULE.MAX_TOOL_ROOT_ENTRIES,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_VERSION,
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_VERSION,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_FIELDS,
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_FIELDS,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_OWNER_RECORD_PHASES,
            MIRROR_MODULE.PRIVATE_OWNER_RECORD_PHASES,
        )
        self.assertEqual(
            ENGINE_MODULE.MAX_MIRROR_PRIVATE_OWNER_RECORD_BYTES,
            MIRROR_MODULE.MAX_PRIVATE_OWNER_RECORD_BYTES,
        )
        self.assertEqual(
            ENGINE_MODULE.MIRROR_PRIVATE_SNAPSHOT_RE.pattern,
            MIRROR_MODULE.PRIVATE_SNAPSHOT_RE.pattern,
        )


class ManifestSchemaParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (REPOSITORY_ROOT / "schema" / "sync-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_schema_tracks_runtime_field_allowlists(self) -> None:
        self.assertEqual(
            set(self.schema["properties"]),
            set(ENGINE_MODULE.MANIFEST_FIELDS),
        )
        self.assertEqual(
            set(self.schema["$defs"]["link"]["properties"]),
            set(ENGINE_MODULE.MANIFEST_LINK_FIELDS),
        )
        self.assertEqual(
            set(self.schema["$defs"]["removedLink"]["properties"]),
            set(ENGINE_MODULE.REMOVED_LINK_FIELDS),
        )
        self.assertEqual(
            set(self.schema["properties"]["base_release"]["properties"]),
            set(ENGINE_MODULE.BASE_RELEASE_FIELDS),
        )

    def test_schema_tracks_runtime_values_and_limits(self) -> None:
        self.assertEqual(self.schema["properties"]["version"]["const"], 1)
        self.assertEqual(
            self.schema["properties"]["links"]["maxItems"],
            ENGINE_MODULE.MAX_MANIFEST_ACTIVE_LINKS,
        )
        self.assertEqual(
            self.schema["$defs"]["owner"]["pattern"],
            ENGINE_MODULE.OWNER_RE.pattern,
        )
        self.assertEqual(
            self.schema["$defs"]["repository"]["pattern"],
            ENGINE_MODULE.REPOSITORY_RE.pattern,
        )
        self.assertEqual(
            self.schema["$defs"]["removedLink"]["properties"]["id"]["pattern"],
            ENGINE_MODULE.REMOVED_LINK_ID_RE.pattern,
        )
        expected_kinds = {"file", "directory", "skill"}
        self.assertEqual(
            set(self.schema["$defs"]["link"]["properties"]["kind"]["enum"]),
            expected_kinds,
        )
        self.assertEqual(
            set(self.schema["$defs"]["removedLink"]["properties"]["kind"]["enum"]),
            expected_kinds,
        )

    def test_schema_required_fields_match_runtime_defaults(self) -> None:
        self.assertEqual(set(self.schema["required"]), {"version", "links"})
        self.assertEqual(
            set(self.schema["$defs"]["link"]["required"]),
            {"source", "target", "kind"},
        )
        self.assertEqual(
            set(self.schema["$defs"]["removedLink"]["required"]),
            {"id", "source", "target", "kind"},
        )

    def test_schema_matches_runtime_nullable_base_release_fields(self) -> None:
        base_release = self.schema["properties"]["base_release"]
        self.assertEqual(set(base_release["type"]), {"object", "null"})
        repo_options = base_release["properties"]["repo"]["anyOf"]
        self.assertIn({"type": "null"}, repo_options)
        self.assertEqual(
            set(base_release["properties"]["sha"]["type"]),
            {"string", "null"},
        )

        manifest = ENGINE_MODULE._parse_manifest_data(
            {
                "version": 1,
                "links": [
                    {
                        "source": "personal_codex/AGENTS.md",
                        "target": "AGENTS.md",
                        "kind": "file",
                    }
                ],
                "base_release": {"repo": None, "sha": None},
            },
            lambda _path: "file",
        )
        self.assertIsNone(manifest.base_release_repo)
        self.assertIsNone(manifest.base_release_sha)

    def test_schema_and_runtime_both_reject_backslash_paths(self) -> None:
        pattern = self.schema["$defs"]["relativePath"]["pattern"]
        self.assertIsNone(re.fullmatch(pattern, "skills\\example"))
        with self.assertRaisesRegex(ENGINE_MODULE.SyncError, "safe POSIX"):
            ENGINE_MODULE._validate_relative_path("skills\\example", "target")


if __name__ == "__main__":
    unittest.main()
