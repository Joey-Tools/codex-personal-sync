from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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

        def inject_index_drift(target_root, mirror, expected):
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
            return real_require_index(target_root, mirror, expected)

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

    def test_run_git_disables_promisor_lazy_fetch(self) -> None:
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

        with self.assertRaisesRegex(
            MIRROR_MODULE.MirrorSyncError,
            "Git verification failed",
        ):
            MIRROR_MODULE._run_git(
                self.canonical_root,
                "cat-file",
                "blob",
                promised_object,
            )

        self.assertFalse(local_object.exists())

    def test_run_git_sanitizes_ambient_git_environment(self) -> None:
        captured: dict[str, object] = {}

        def capture_popen(command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)
            return mock.Mock()

        bound_root = MIRROR_MODULE._bind_root(self.canonical_root)
        try:
            MIRROR_MODULE._ensure_git_control_binding(bound_root)
            private_control_fd = bound_root.git_control.private.fd
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
            MIRROR_MODULE.LAUNCHER_EXECUTABLE.as_posix(),
        )
        self.assertIn(MIRROR_MODULE.LAUNCHER_PROGRAM, captured["command"])
        self.assertIn(MIRROR_MODULE.GIT_EXECUTABLE.as_posix(), captured["command"])
        self.assertIn("--git-dir=.", captured["command"])
        self.assertNotIn("cwd", captured)
        self.assertNotIn("preexec_fn", captured)
        self.assertIn(
            private_control_fd,
            captured["pass_fds"],
        )

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

    def test_run_git_uses_a_fixed_launcher_without_preexec_fn(self) -> None:
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
                MIRROR_MODULE._run_git(bound_root, "rev-parse", "HEAD")
            self.assertEqual(
                captured["command"][0],
                MIRROR_MODULE.LAUNCHER_EXECUTABLE.as_posix(),
            )
            self.assertIn(
                MIRROR_MODULE.LAUNCHER_PROGRAM,
                captured["command"],
            )
            self.assertNotIn("preexec_fn", captured)
            self.assertNotIn("cwd", captured)
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
                "commondir control file appeared",
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

    def test_private_tool_root_lock_covers_owner_publication(self) -> None:
        real_create_owner = MIRROR_MODULE._create_owner_record
        observed_lock = False

        def assert_locked(tool_root, private_name, private):
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
            private_index.write_bytes(original_index)
        finally:
            MIRROR_MODULE._finish_bound_roots(bound_root)

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

    def test_check_and_generate_require_explicit_target_and_mirror(self) -> None:
        parser = MIRROR_MODULE.build_parser()
        for argv in (
            ["check", "--mirror", "toolbox"],
            ["check", "--target-root", str(self.target_root)],
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
