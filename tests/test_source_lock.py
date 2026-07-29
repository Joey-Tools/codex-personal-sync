from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
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


class MirrorGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="canonical-mirror-tests."
        )
        self.root = Path(self.temporary_directory.name)
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

    def test_repeated_exact_generate_is_a_quarantine_stable_no_op(self) -> None:
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
            recovered = MIRROR_MODULE._load_transaction_journal(bound_root)
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
                    directory_fd=binding.fd,
                    directory_identity=binding.identity,
                    directory_access_policy=binding.access_policy,
                    directory_label=binding.label,
                )
        finally:
            os.close(binding.fd)

    def test_run_git_executes_git_directly_without_python_launcher(self) -> None:
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
            self.assertEqual(captured["launch_cwd_identity"], private_identity)
            self.assertNotIn("preexec_fn", captured)
            self.assertNotIn("cwd", captured)
            self.assertNotIn("pass_fds", captured)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

    def test_git_output_collector_enforces_limits_and_reaps(self) -> None:
        output_process = subprocess.Popen(
            ["/usr/bin/printf", "123456789"],
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
            MIRROR_MODULE._collect_bounded_git_output(output_process)
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
            MIRROR_MODULE._collect_bounded_git_output(timeout_process)
        self.assertIsNotNone(timeout_process.poll())

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

        def assert_locked(root, tool_root, private_name, private):
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
                private_name,
                private,
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

    def test_private_cleanup_binds_quarantine_before_control_paths_move(
        self,
    ) -> None:
        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        MIRROR_MODULE._ensure_git_control_binding(bound_root)
        assert bound_root.git_control is not None
        private_path = bound_root.git_control.private.path
        owner_path = bound_root.git_control.owner_record.path
        assert private_path is not None
        assert owner_path is not None
        real_bind = MIRROR_MODULE._bind_durable_quarantine_root
        observed_pre_teardown_paths = False

        def assert_control_paths_still_exist(root, **kwargs):
            nonlocal observed_pre_teardown_paths
            self.assertTrue(private_path.exists())
            self.assertTrue(owner_path.exists())
            observed_pre_teardown_paths = True
            quarantine = real_bind(root, **kwargs)
            self.assertEqual(
                quarantine.path,
                MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT
                / MIRROR_MODULE.DURABLE_QUARANTINE_ROOT_NAME,
            )
            self.assertEqual(
                os.fstat(quarantine.fd).st_dev,
                os.fstat(bound_root.git_control.private_parent.fd).st_dev,
            )
            return quarantine

        with mock.patch.object(
            MIRROR_MODULE,
            "_bind_durable_quarantine_root",
            side_effect=assert_control_paths_still_exist,
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
        real_bind = MIRROR_MODULE._bind_durable_quarantine_root
        observed_private_parent = False

        def require_private_parent(root, **kwargs):
            nonlocal observed_private_parent
            self.assertEqual(
                kwargs.get("quarantine_parent"),
                MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT,
            )
            self.assertEqual(
                kwargs.get("source_parent_fd"),
                tool_root.fd,
            )
            observed_private_parent = True
            return real_bind(root, **kwargs)

        try:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            with mock.patch.object(
                MIRROR_MODULE,
                "_bind_durable_quarantine_root",
                side_effect=require_private_parent,
            ):
                MIRROR_MODULE._recover_stale_private_snapshots(
                    bound_root,
                    tool_root,
                )
        finally:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(observed_private_parent)
        self.assertFalse(owner_path.exists())

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
        real_bind = MIRROR_MODULE._bind_durable_quarantine_root
        real_remove_private = MIRROR_MODULE._remove_bound_private_directory
        quarantine_bound = False

        def observe_quarantine_bind(root, **kwargs):
            nonlocal quarantine_bound
            self.assertEqual(
                kwargs.get("quarantine_parent"),
                MIRROR_MODULE.PRIVATE_GIT_CONTROL_PARENT,
            )
            self.assertEqual(kwargs.get("source_parent_fd"), tool_root.fd)
            quarantine_bound = True
            return real_bind(root, **kwargs)

        def require_prebound_quarantine(*args, **kwargs):
            self.assertTrue(quarantine_bound)
            return real_remove_private(*args, **kwargs)

        try:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_EX)
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_bind_durable_quarantine_root",
                    side_effect=observe_quarantine_bind,
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
                )
        finally:
            MIRROR_MODULE.fcntl.flock(tool_root.fd, MIRROR_MODULE.fcntl.LOCK_UN)
            os.close(tool_root.fd)
            MIRROR_MODULE._finish_bound_roots(bound_root)

        self.assertTrue(quarantine_bound)
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
                private_name,
                MIRROR_MODULE._object_identity(private_metadata),
                "abcdef0123456789abcdef0123456789",
                "ready",
            )
        )
        owner_path.chmod(0o600)
        os.chown(owner_path, os.geteuid(), os.getegid())
        real_read = MIRROR_MODULE._safe_read_leaf_snapshot
        injected = False

        def replace_owner_after_lock(parent_fd, name, display_path):
            nonlocal injected
            if name == owner_name and not injected:
                injected = True
                os.rename(owner_path, saved_owner)
                owner_path.write_bytes(saved_owner.read_bytes())
                owner_path.chmod(0o600)
                os.chown(owner_path, os.geteuid(), os.getegid())
            return real_read(parent_fd, name, display_path)

        bound_root = MIRROR_MODULE._bind_root(self.target_root)
        try:
            with (
                mock.patch.object(
                    MIRROR_MODULE,
                    "_safe_read_leaf_snapshot",
                    side_effect=replace_owner_after_lock,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "owner path was replaced after its lock",
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

            def swap_live_index_while_child_runs(process, operation=None):
                os.rename(index_path, saved_index)
                index_path.write_bytes(b"transient malicious index\n")
                try:
                    return real_collect(process, operation)
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
        ):
            nonlocal scan_count
            observed = real_scan(private_fd, operation)
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

            def mutate_private_index_after_child(process, operation=None):
                result = real_collect(process, operation)
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

            def tamper_while_child_runs(process, operation=None):
                object_path.chmod(0o644)
                mutated = bytes([original[0] ^ 0x01]) + original[1:]
                object_path.write_bytes(mutated)
                try:
                    return real_collect(process, operation)
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

            def tamper_while_child_runs(process, operation=None):
                pack_path.chmod(0o644)
                mutated = bytes([original[0] ^ 0x01]) + original[1:]
                pack_path.write_bytes(mutated)
                try:
                    return real_collect(process, operation)
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
                    "PRIVATE_GIT_CONTROL_PARENT",
                    self.target_root,
                ),
                self.assertRaisesRegex(
                    MIRROR_MODULE.MirrorSyncError,
                    "overlaps repository root",
                ),
            ):
                MIRROR_MODULE._ensure_git_control_binding(overlapping_root)
        finally:
            MIRROR_MODULE._finish_bound_roots(overlapping_root)

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
        self.assertEqual(
            set(source_lock.mirrors["private"].files),
            {
                "engine",
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


if __name__ == "__main__":
    unittest.main()
