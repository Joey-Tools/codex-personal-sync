from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_scheduler_doctor_tests",
    SCRIPT_PATH,
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PUBLIC_SHA = "1" * 40
PRIVATE_SHA = "2" * 40


def snapshot_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str | None], ...]:
    entries: list[tuple[str, str, int, bytes | str | None]] = []

    def visit(path: Path) -> None:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
            return
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, None))
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        if stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "file", mode, path.read_bytes()))
            return
        entries.append((relative, "other", mode, None))

    visit(root)
    return tuple(entries)


class SchedulerDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="scheduler-doctor.")
        self.root = Path(self.tmpdir.name)
        self.user_home = self.root / "home"
        self.home = self.user_home / ".codex"
        self.home.mkdir(parents=True)
        self.path_home_patch = mock.patch.object(
            MODULE.Path,
            "home",
            return_value=self.user_home,
        )
        self.path_home_patch.start()

    def tearDown(self) -> None:
        self.path_home_patch.stop()
        self.tmpdir.cleanup()

    def write_runner(self) -> Path:
        runner = self.home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        return runner

    def install_scheduler_quietly(
        self,
        repo: str,
        interval_minutes: int | None,
        platform_name: str,
        *,
        enable: bool = False,
        mode: str = "public",
        base_repo: str = MODULE.DEFAULT_PUBLIC_RELEASE_REPO,
        owner: str = "private",
    ) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.install_scheduler(
                self.home,
                repo,
                interval_minutes,
                platform_name,
                None,
                dry_run=False,
                enable=enable,
                mode=mode,
                base_repo=base_repo,
                owner=owner,
            )

    def write_skill(self, name: str, frontmatter_name: str) -> Path:
        skill_root = self.home / "skills" / name
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\n---\n",
            encoding="utf-8",
        )
        return skill_root

    def test_macos_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        paths.launchd_plist.write_bytes(
            plistlib.dumps(
                MODULE._launchd_plist(
                    self.home,
                    "owner/private-sync",
                    23,
                    runner,
                    mode="private",
                    base_repo="owner/public-sync",
                    owner="private",
                ),
                sort_keys=True,
            )
        )

        config = MODULE._load_macos_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "macos")
        self.assertEqual(config.config_paths, (paths.launchd_plist,))
        self.assertEqual(config.interval_minutes, 23)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")

    def test_linux_scheduler_config_parses_private_run_scheduled(self) -> None:
        runner = self.home / "bin" / "runner with spaces"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(47),
            encoding="utf-8",
        )

        config = MODULE._load_linux_scheduler_config(paths)

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.platform, "linux")
        self.assertEqual(
            config.config_paths,
            (paths.systemd_service, paths.systemd_timer),
        )
        self.assertEqual(config.interval_minutes, 47)
        self.assertEqual(config.runner, runner)
        self.assertEqual(config.home, self.home)
        self.assertEqual(config.command, "run-scheduled")
        self.assertEqual(config.mode, "private")
        self.assertEqual(config.repo, "owner/private-sync")
        self.assertEqual(config.base_repo, "owner/public-sync")
        self.assertEqual(config.owner, "private")

    def test_scheduler_config_read_tolerates_mtime_only_churn(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        touched = False

        def read_then_touch(file_fd: int, size: int) -> bytes:
            nonlocal touched
            payload = real_read(file_fd, size)
            if not payload and not touched:
                touched = True
                metadata = paths.systemd_service.stat()
                os.utime(
                    paths.systemd_service,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000_000,
                    ),
                )
            return payload

        with mock.patch.object(MODULE.os, "read", side_effect=read_then_touch):
            config = MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(touched)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.interval_minutes, 37)

    def test_scheduler_config_read_rejects_same_inode_byte_drift(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        original = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        )
        paths.systemd_service.write_text(original, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(37),
            encoding="utf-8",
        )
        real_read = MODULE.os.read
        mutated = False

        def read_then_mutate(file_fd: int, size: int) -> bytes:
            nonlocal mutated
            payload = real_read(file_fd, size)
            if not payload and not mutated:
                mutated = True
                paths.systemd_service.write_text(
                    original.replace("Type=oneshot", "Type=onefail"),
                    encoding="utf-8",
                )
            return payload

        with (
            mock.patch.object(MODULE.os, "read", side_effect=read_then_mutate),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "content changed during read",
            ),
        ):
            MODULE._load_linux_scheduler_config(paths)

        self.assertTrue(mutated)

    def test_scheduler_install_args_use_stable_run_scheduled_entrypoint(self) -> None:
        runner = Path("/opt/codex/bin/codex-personal-sync")

        public_args = MODULE._scheduler_install_args(
            runner,
            "owner/public-sync",
            self.home,
        )
        private_args = MODULE._scheduler_install_args(
            runner,
            "owner/private-sync",
            self.home,
            mode="private",
            base_repo="owner/public-sync",
            owner="private",
        )

        self.assertEqual(
            public_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/public-sync",
                "--home",
                str(self.home),
            ],
        )
        self.assertEqual(
            private_args,
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "private",
                "--repo",
                "owner/private-sync",
                "--base-repo",
                "owner/public-sync",
                "--owner",
                "private",
                "--home",
                str(self.home),
            ],
        )

    def test_install_preserves_audited_existing_interval_when_omitted(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        legacy_payload = MODULE._launchd_plist(
            self.home,
            "owner/old-sync",
            17,
            runner,
        )
        legacy_payload["ProgramArguments"] = [
            str(runner),
            "install",
            "--repo",
            "owner/old-sync",
            "--home",
            str(self.home),
        ]
        paths.launchd_plist.write_bytes(
            plistlib.dumps(legacy_payload, sort_keys=True)
        )

        self.install_scheduler_quietly(
            "owner/new-sync",
            None,
            "macos",
        )

        with paths.launchd_plist.open("rb") as stream:
            installed = plistlib.load(stream)
        self.assertEqual(installed["StartInterval"], 17 * 60)
        self.assertEqual(
            installed["ProgramArguments"],
            [
                str(runner),
                "run-scheduled",
                "--mode",
                "public",
                "--repo",
                "owner/new-sync",
                "--home",
                str(self.home),
            ],
        )

    def test_exact_audited_config_avoids_reinstall_and_rewrite(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            29,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        before = (
            paths.systemd_service.read_bytes(),
            paths.systemd_timer.read_bytes(),
            paths.systemd_service.stat().st_mtime_ns,
            paths.systemd_timer.stat().st_mtime_ns,
        )

        output = io.StringIO()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            mock.patch.object(MODULE, "_run_native_command") as run_native,
            contextlib.redirect_stdout(output),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                None,
                "linux",
                None,
                dry_run=False,
                enable=True,
            )

        write_text.assert_not_called()
        self.assertEqual(
            run_native.call_args_list,
            [
                mock.call(
                    ["systemctl", "--user", "daemon-reload"],
                    dry_run=False,
                ),
                mock.call(
                    [
                        "systemctl",
                        "--user",
                        "enable",
                        f"{MODULE.SYSTEMD_UNIT}.timer",
                    ],
                    dry_run=False,
                ),
                mock.call(
                    [
                        "systemctl",
                        "--user",
                        "start",
                        f"{MODULE.SYSTEMD_UNIT}.timer",
                    ],
                    dry_run=False,
                ),
            ],
        )
        self.assertIn("already matches audited configuration", output.getvalue())
        self.assertIn("preserved 29-minute interval", output.getvalue())
        self.assertEqual(
            (
                paths.systemd_service.read_bytes(),
                paths.systemd_timer.read_bytes(),
                paths.systemd_service.stat().st_mtime_ns,
                paths.systemd_timer.stat().st_mtime_ns,
            ),
            before,
        )

    def test_status_report_includes_config_releases_and_runtime_health(self) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/private-sync",
                runner,
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        runtime_state = {
            "version": 1,
            "last_attempt": "2026-07-23T09:15:00+00:00",
            "last_success": "2026-07-23T08:15:00+00:00",
            "success": False,
            "failure_reason": "network unavailable",
            "mode": "private",
            "repo": "owner/private-sync",
            "base_repo": "owner/public-sync",
            "owner": "private",
        }
        output = io.StringIO()

        with (
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(
                    (MODULE.PUBLIC_OWNER, PUBLIC_SHA),
                    ("private", PRIVATE_SHA),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            contextlib.redirect_stdout(output),
        ):
            report = MODULE.status_scheduler(
                self.home,
                "linux",
                json_output=True,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(report.platform, "linux")
        self.assertTrue(report.installed)
        self.assertEqual(
            payload,
            {
                "platform": "linux",
                "installed": True,
                "enabled": True,
                "config": [
                    str(paths.systemd_service),
                    str(paths.systemd_timer),
                ],
                "interval_minutes": 31,
                "runner": str(runner),
                "stable_runner": False,
                "mode": "private",
                "base_repo": "owner/public-sync",
                "private_repo": "owner/private-sync",
                "last_attempt": "2026-07-23T09:15:00+00:00",
                "recent_success": "2026-07-23T08:15:00+00:00",
                "current_release": {
                    MODULE.PUBLIC_OWNER: PUBLIC_SHA,
                    "private": PRIVATE_SHA,
                },
                "release_integrity": [],
                "quarantine_batches": 0,
                "quarantine_limit": MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
                "failure_code": None,
                "failure_reason": "network unavailable",
            },
        )

    def test_run_scheduled_persists_success_state(self) -> None:
        with (
            mock.patch.object(MODULE, "install_from_github") as install,
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored-base",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/public-sync",
            self.home,
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(
            initial["failure_reason"],
            "scheduled sync did not complete",
        )
        self.assertTrue(final["success"])
        self.assertIsNone(final["failure_reason"])
        self.assertEqual(final["last_attempt"], final["last_success"])
        self.assertEqual(final["mode"], "public")
        self.assertEqual(final["repo"], "owner/public-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], MODULE.PUBLIC_OWNER)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )
        self.assertEqual(
            stat.S_IMODE(MODULE._scheduler_status_path(self.home).stat().st_mode),
            0o600,
        )

    def test_run_scheduled_persists_failure_and_previous_success(self) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "private",
                "repo": "owner/private-sync",
                "base_repo": "owner/public-sync",
                "owner": "private",
            },
        )

        with (
            mock.patch.object(
                MODULE,
                "install_private_from_github",
                side_effect=MODULE.SyncError("network unavailable"),
            ) as install,
            mock.patch.object(
                MODULE,
                "_write_scheduler_runtime_state",
                wraps=MODULE._write_scheduler_runtime_state,
            ) as write_state,
            self.assertRaisesRegex(MODULE.SyncError, "network unavailable"),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/private-sync",
                mode="private",
                base_repo="owner/public-sync",
                owner="private",
            )

        install.assert_called_once_with(
            "owner/private-sync",
            self.home,
            base_repo="owner/public-sync",
            owner="private",
            dry_run=False,
        )
        self.assertEqual(write_state.call_count, 2)
        initial = write_state.call_args_list[0].args[1]
        final = write_state.call_args_list[1].args[1]
        self.assertFalse(initial["success"])
        self.assertEqual(initial["last_success"], previous_success)
        self.assertFalse(final["success"])
        self.assertEqual(final["last_success"], previous_success)
        self.assertEqual(final["failure_reason"], "network unavailable")
        self.assertEqual(final["mode"], "private")
        self.assertEqual(final["repo"], "owner/private-sync")
        self.assertEqual(final["base_repo"], "owner/public-sync")
        self.assertEqual(final["owner"], "private")
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            final,
        )

    def test_overlapping_scheduled_completion_cannot_replace_newer_failure(
        self,
    ) -> None:
        older_install_entered = threading.Event()
        release_older_install = threading.Event()
        errors: dict[str, BaseException] = {}

        def interleaved_install(
            repo: str,
            home: Path,
            *,
            dry_run: bool,
        ) -> None:
            self.assertEqual(repo, "owner/public-sync")
            self.assertEqual(home, self.home)
            self.assertFalse(dry_run)
            if threading.current_thread().name == "older-scheduled-run":
                older_install_entered.set()
                if not release_older_install.wait(5):
                    raise AssertionError("older scheduled run was not released")
                return
            raise MODULE.SyncError(
                "newer scheduled run failed",
                code="newer-attempt-failed",
            )

        def run(name: str) -> None:
            try:
                MODULE.run_scheduled(
                    self.home,
                    "owner/public-sync",
                    mode="public",
                    base_repo="owner/ignored",
                    owner="private",
                )
            except BaseException as error:
                errors[name] = error

        older = threading.Thread(
            target=run,
            args=("older",),
            name="older-scheduled-run",
            daemon=True,
        )
        newer = threading.Thread(
            target=run,
            args=("newer",),
            name="newer-scheduled-run",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=interleaved_install,
            ),
            mock.patch.object(
                MODULE,
                "_capture_scheduler_release_trees",
                return_value={},
            ),
        ):
            try:
                older.start()
                self.assertTrue(older_install_entered.wait(5))
                older_incomplete = MODULE._read_scheduler_runtime_state(self.home)
                assert older_incomplete is not None

                newer.start()
                newer.join(5)
                self.assertFalse(newer.is_alive())
                newer_failure = MODULE._read_scheduler_runtime_state(self.home)
                assert newer_failure is not None
                self.assertIsInstance(errors.get("newer"), MODULE.SyncError)
                self.assertFalse(newer_failure["success"])
                self.assertEqual(
                    newer_failure["failure_code"],
                    "newer-attempt-failed",
                )
                self.assertGreater(
                    datetime.fromisoformat(newer_failure["last_attempt"]),
                    datetime.fromisoformat(older_incomplete["last_attempt"]),
                )
            finally:
                release_older_install.set()
                if older.ident is not None:
                    older.join(5)
                if newer.ident is not None:
                    newer.join(5)
            self.assertFalse(older.is_alive())

        self.assertNotIn("older", errors)
        self.assertEqual(
            MODULE._read_scheduler_runtime_state(self.home),
            newer_failure,
        )

    def test_scheduler_attempt_recovers_from_unbounded_or_noncanonical_time(
        self,
    ) -> None:
        invalid_attempts = (
            "9999-12-31T23:59:59.999999+00:00",
            "9999-12-31T23:59:59.999999-23:59",
            "2026-07-23T09:15:00Z",
            "2026-07-23T09:15:00",
            "not-a-timestamp",
        )

        for previous_attempt in invalid_attempts:
            with self.subTest(previous_attempt=previous_attempt):
                attempt = MODULE._next_scheduler_attempt(
                    {"last_attempt": previous_attempt}
                )
                parsed = datetime.fromisoformat(attempt)
                self.assertEqual(parsed.utcoffset(), timedelta(0))
                self.assertEqual(parsed.isoformat(), attempt)
                self.assertNotEqual(attempt, previous_attempt)
                self.assertLess(parsed.year, 9999)

    def test_scheduler_attempt_rejects_far_future_but_preserves_cas_order(
        self,
    ) -> None:
        far_future = (
            datetime.now(timezone.utc)
            + MODULE.MAX_SCHEDULER_ATTEMPT_FUTURE_SKEW
            + timedelta(days=1)
        ).isoformat()
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 2,
                "last_attempt": far_future,
                "last_success": far_future,
                "success": True,
                "failure_reason": "injected future state",
                "failure_code": "injected-future-state",
                "release_trees": {
                    MODULE.PUBLIC_OWNER: {
                        "sha": PUBLIC_SHA,
                        "tree_sha256": "a" * 64,
                    }
                },
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        with self.assertRaises(MODULE.SyncError) as invalid_state:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            invalid_state.exception.code,
            "scheduler-state-timestamp-invalid",
        )

        replacement_attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertNotEqual(replacement_attempt, far_future)
        self.assertFalse(
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )
        )
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertEqual(state["last_attempt"], replacement_attempt)
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["release_trees"], {})

    def test_scheduler_runtime_cas_preserves_identity_replacement(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        replacement = b"foreign newer scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def replace_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.unlink()
            status_path.write_bytes(replacement)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=replace_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), replacement)

    def test_scheduler_runtime_cas_rolls_back_a_late_identity_replacement(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        newer = b"foreign state that won the late race\n"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0

        def replace_live_then_exchange(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                status_path.unlink()
                status_path.write_bytes(newer)
                status_path.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=replace_live_then_exchange,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "restored the displaced state without overwriting it",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 2)
        self.assertEqual(status_path.read_bytes(), newer)
        self.assertEqual(
            list(status_path.parent.glob(f".{status_path.name}.personal-sync-write-*")),
            [],
        )

    def test_scheduler_runtime_cas_retains_recovery_on_rollback_uncertainty(
        self,
    ) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        newer = b"foreign state that won the late race\n"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0

        def fail_rollback_exchange(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count
            exchange_count += 1
            if exchange_count == 1:
                status_path.unlink()
                status_path.write_bytes(newer)
                status_path.chmod(0o600)
                real_exchange(
                    first_parent_fd,
                    first_name,
                    second_parent_fd,
                    second_name,
                )
                return
            raise OSError("injected rollback uncertainty")

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=fail_rollback_exchange,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery evidence is retained",
            ),
        ):
            MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(exchange_count, 2)
        recovery_paths = list(
            status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), newer)

    def test_scheduler_runtime_cas_rejects_same_inode_content_and_access_drift(
        self,
    ) -> None:
        for drift in ("content", "access"):
            with self.subTest(drift=drift):
                case_home = self.home / f"runtime-{drift}"
                case_home.mkdir()
                attempt = MODULE._begin_scheduler_attempt(
                    case_home,
                    mode="public",
                    repo="owner/public-sync",
                    base_repo="owner/public-sync",
                    owner=MODULE.PUBLIC_OWNER,
                )
                status_path = MODULE._scheduler_status_path(case_home)
                original_identity = (
                    status_path.stat().st_dev,
                    status_path.stat().st_ino,
                )
                real_publish = MODULE._atomic_write_scheduler_config

                def drift_before_publish(
                    path: Path,
                    payload: bytes,
                    *,
                    expected_snapshot: (MODULE.ManagedStateFileSnapshot | None) = None,
                    mode: int = 0o600,
                    gid: int | None = None,
                    rollback_displaced_conflict: bool = False,
                ) -> None:
                    if drift == "content":
                        with status_path.open("r+b") as stream:
                            stream.seek(0)
                            stream.write(b"foreign same-inode state\n")
                            stream.truncate()
                            stream.flush()
                            os.fsync(stream.fileno())
                    else:
                        status_path.chmod(0o640)
                    real_publish(
                        path,
                        payload,
                        expected_snapshot=expected_snapshot,
                        mode=mode,
                        gid=gid,
                        rollback_displaced_conflict=rollback_displaced_conflict,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_atomic_write_scheduler_config",
                        side_effect=drift_before_publish,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed before conditional publication",
                    ),
                ):
                    MODULE._complete_scheduler_attempt(
                        case_home,
                        attempt=attempt,
                        success=True,
                        failure_reason=None,
                        failure_code=None,
                        mode="public",
                        repo="owner/public-sync",
                        base_repo="owner/public-sync",
                        owner=MODULE.PUBLIC_OWNER,
                    )

                self.assertEqual(
                    (
                        status_path.stat().st_dev,
                        status_path.stat().st_ino,
                    ),
                    original_identity,
                )
                if drift == "content":
                    self.assertEqual(
                        status_path.read_bytes(),
                        b"foreign same-inode state\n",
                    )
                else:
                    self.assertEqual(
                        stat.S_IMODE(status_path.stat().st_mode),
                        0o640,
                    )

    def test_scheduler_runtime_cas_preserves_absent_to_appeared_state(
        self,
    ) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        appeared = b"foreign concurrently-created scheduler state\n"
        real_publish = MODULE._atomic_write_scheduler_config

        def appear_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.write_bytes(appeared)
            status_path.chmod(0o600)
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with (
            mock.patch.object(
                MODULE,
                "_atomic_write_scheduler_config",
                side_effect=appear_before_publish,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            MODULE._begin_scheduler_attempt(
                self.home,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertEqual(status_path.read_bytes(), appeared)

    def test_scheduler_runtime_cas_allows_mtime_only_transition(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        real_publish = MODULE._atomic_write_scheduler_config

        def touch_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            metadata = status_path.stat()
            os.utime(
                status_path,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )
            real_publish(
                path,
                payload,
                expected_snapshot=expected_snapshot,
                mode=mode,
                gid=gid,
                rollback_displaced_conflict=rollback_displaced_conflict,
            )

        with mock.patch.object(
            MODULE,
            "_atomic_write_scheduler_config",
            side_effect=touch_before_publish,
        ):
            completed = MODULE._complete_scheduler_attempt(
                self.home,
                attempt=attempt,
                success=True,
                failure_reason=None,
                failure_code=None,
                mode="public",
                repo="owner/public-sync",
                base_repo="owner/public-sync",
                owner=MODULE.PUBLIC_OWNER,
            )

        self.assertTrue(completed)
        state = MODULE._read_scheduler_runtime_state(self.home)
        assert state is not None
        self.assertTrue(state["success"])
        self.assertEqual(state["last_attempt"], attempt)

    def test_timestamp_recovery_does_not_accept_unsafe_runtime_file(self) -> None:
        status_path = MODULE._scheduler_status_path(self.home)
        status_path.parent.mkdir(parents=True)
        status_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "last_attempt": "not-a-timestamp",
                    "last_success": None,
                    "success": False,
                    "failure_reason": None,
                    "failure_code": None,
                    "release_trees": {},
                    "mode": "public",
                    "repo": "owner/public-sync",
                    "base_repo": "owner/public-sync",
                    "owner": MODULE.PUBLIC_OWNER,
                }
            ),
            encoding="utf-8",
        )
        status_path.chmod(0o644)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "scheduler runtime state is invalid",
        ):
            MODULE._read_scheduler_runtime_state(
                self.home,
                recover_timestamps=True,
            )

    def test_audit_and_doctor_detect_skill_issues_without_deletion(self) -> None:
        duplicate_one = self.write_skill("duplicate-one", "duplicate-name")
        duplicate_two = self.write_skill("duplicate-two", "duplicate-name")
        managed_drift = self.write_skill("managed-drift", "managed-name")
        cache_entry = self.home / "skills" / ".cache"
        cache_entry.mkdir()
        backup_entry = (
            self.home
            / "skills"
            / "bug-triage-playbook.bak-20260312-145916"
        )
        backup_entry.mkdir()
        system_backup_entry = (
            self.home
            / "skills"
            / ".system"
            / "legacy-system-skill.bak-20260723"
        )
        system_backup_entry.mkdir(parents=True)
        broken_link = self.home / "skills" / "broken-link"
        broken_link.symlink_to(self.root / "missing-skill", target_is_directory=True)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("personal_codex/skills/managed-drift"),
            target=PurePosixPath("skills/managed-drift"),
            kind="skill",
            owner=MODULE.PUBLIC_OWNER,
            link_target="../personal-sync/releases/expected/managed-drift",
            release_sha=PUBLIC_SHA,
        )
        managed_state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        healthy_report = MODULE.SchedulerReport(
            platform="linux",
            installed=True,
            enabled=True,
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            stable_runner=True,
            mode="public",
            base_repo="owner/public-sync",
            private_repo=None,
            last_attempt=None,
            recent_success=None,
            current_releases=(),
            failure_reason=None,
        )
        before = snapshot_tree(self.home / "skills")

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=managed_state,
        ):
            audit_issues = MODULE.audit_active_skills(self.home)
        audit_codes = {issue.code for issue in audit_issues}
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset(audit_codes)
        )
        self.assertEqual(
            {
                issue.path
                for issue in audit_issues
                if issue.code == "unmanaged-skill"
            },
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {
                issue.path
                for issue in audit_issues
                if issue.code == "duplicate-skill-name"
            },
            {duplicate_one, duplicate_two},
        )
        self.assertEqual(
            {
                issue.path
                for issue in audit_issues
                if issue.code == "cache-or-backup"
            },
            {cache_entry, backup_entry, system_backup_entry},
        )
        self.assertIn(
            ("broken-link", broken_link),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertIn(
            ("generated-drift", managed_drift),
            {(issue.code, issue.path) for issue in audit_issues},
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_load_managed_state",
                return_value=managed_state,
            ),
            mock.patch.object(
                MODULE,
                "scheduler_report",
                return_value=healthy_report,
            ),
            contextlib.redirect_stdout(output),
        ):
            report, doctor_issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertIs(report, healthy_report)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            {issue["code"] for issue in payload["issues"]},
            {issue.code for issue in doctor_issues},
        )
        self.assertTrue(
            {
                "unmanaged-skill",
                "broken-link",
                "duplicate-skill-name",
                "cache-or-backup",
                "generated-drift",
            }.issubset({issue.code for issue in doctor_issues})
        )
        self.assertEqual(snapshot_tree(self.home / "skills"), before)

    def test_macos_loader_rejects_program_override(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        paths.launchd_plist.parent.mkdir(parents=True)
        payload = MODULE._launchd_plist(
            self.home,
            "owner/public-sync",
            19,
            runner,
        )
        payload["Program"] = "/tmp/attacker"
        paths.launchd_plist.write_bytes(plistlib.dumps(payload, sort_keys=True))

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_macos_scheduler_config(paths)

    def test_linux_loader_rejects_extra_execution_semantics_and_dropins(
        self,
    ) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        service = MODULE._systemd_service(
            self.home,
            "owner/public-sync",
            runner,
        ).replace(
            "ExecStart=",
            "ExecStartPre=\"/tmp/attacker\"\nExecStart=",
        )
        paths.systemd_service.write_text(service, encoding="utf-8")
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(23),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unsupported execution semantics",
        ):
            MODULE._load_linux_scheduler_config(paths)

        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        drop_in = paths.systemd_service.with_name(
            paths.systemd_service.name + ".d"
        )
        drop_in.mkdir()
        (drop_in / "override.conf").write_text(
            "[Service]\nExecStartPre=/tmp/attacker\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "drop-ins are unsupported",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_linux_interval_parser_is_bounded(self) -> None:
        runner = self.home / "bin" / "runner"
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/public-sync",
                runner,
            ),
            encoding="utf-8",
        )
        timer = MODULE._systemd_timer(1).replace(
            "OnUnitActiveSec=1min",
            f"OnUnitActiveSec={'9' * 4301}min",
        )
        paths.systemd_timer.write_text(timer, encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "interval must use whole minutes",
        ):
            MODULE._load_linux_scheduler_config(paths)

    def test_install_rejects_invalid_inputs_before_writing(self) -> None:
        self.write_runner()
        with (
            mock.patch.object(MODULE, "_write_text") as write_text,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "owner/repo",
            ),
        ):
            MODULE.install_scheduler(
                self.home,
                "not-a-repository",
                30,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )
        write_text.assert_not_called()

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "must not exceed",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/repository",
                MODULE.MAX_SCHEDULER_INTERVAL_MINUTES + 1,
                "linux",
                None,
                dry_run=True,
                enable=False,
            )

    def test_runner_validation_rejects_relative_and_directory_paths(self) -> None:
        with self.assertRaisesRegex(MODULE.SyncError, "absolute path"):
            MODULE._validate_scheduler_runner(
                Path("relative-runner"),
                dry_run=True,
            )
        runner_directory = self.home / "bin" / "runner-directory"
        runner_directory.mkdir(parents=True)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "regular executable",
        ):
            MODULE._validate_scheduler_runner(
                runner_directory,
                dry_run=False,
            )

    def test_stable_runner_requires_managed_symlink_claim(self) -> None:
        plain_runner = self.write_runner()
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "timer",),
            interval_minutes=60,
            runner=plain_runner,
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertFalse(
            MODULE._stable_scheduler_runner_matches(self.home, config)
        )

        plain_runner.unlink()
        managed_runner = (
            self.home
            / "personal-sync"
            / "releases"
            / PUBLIC_SHA
            / "scripts"
            / "codex_personal_sync.py"
        )
        managed_runner.parent.mkdir(parents=True)
        managed_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_runner.chmod(0o755)
        link_target = os.path.relpath(managed_runner, plain_runner.parent)
        plain_runner.symlink_to(link_target)
        record = MODULE.ManagedLinkRecord(
            source=PurePosixPath("scripts/codex_personal_sync.py"),
            target=PurePosixPath("bin/codex-personal-sync"),
            kind="file",
            owner=MODULE.PUBLIC_OWNER,
            link_target=link_target,
            release_sha=PUBLIC_SHA,
        )
        state = MODULE.ManagedState(
            owners={MODULE.PUBLIC_OWNER: PUBLIC_SHA},
            links={record.target: record},
        )
        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            return_value=state,
        ):
            self.assertTrue(
                MODULE._stable_scheduler_runner_matches(self.home, config)
            )

        alternate = self.home / "alternate-runner"
        alternate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        alternate.chmod(0o755)

        def replace_link_then_return_state(
            _home: Path,
        ) -> MODULE.ManagedState:
            plain_runner.unlink()
            plain_runner.symlink_to(
                os.path.relpath(alternate, plain_runner.parent)
            )
            return state

        with mock.patch.object(
            MODULE,
            "_load_managed_state",
            side_effect=replace_link_then_return_state,
        ):
            self.assertFalse(
                MODULE._stable_scheduler_runner_matches(self.home, config)
            )

    def test_runtime_target_mismatch_is_unhealthy_and_not_carried_forward(
        self,
    ) -> None:
        runner = self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        paths.systemd_service.write_text(
            MODULE._systemd_service(
                self.home,
                "owner/current",
                runner,
            ),
            encoding="utf-8",
        )
        paths.systemd_timer.write_text(
            MODULE._systemd_timer(31),
            encoding="utf-8",
        )
        stale_runtime = {
            "version": 1,
            "last_attempt": "2026-07-23T08:00:00+00:00",
            "last_success": "2026-07-23T08:00:00+00:00",
            "success": False,
            "failure_reason": "old target failed",
            "mode": "public",
            "repo": "owner/old",
            "base_repo": "owner/old",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with mock.patch.object(
            MODULE,
            "_read_scheduler_runtime_state",
            return_value=stale_runtime,
        ):
            report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_reason,
            "scheduler runtime state belongs to a different configured target",
        )
        self.assertEqual(report.failure_code, "scheduler-target-mismatch")
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)
        next_payload = MODULE._scheduler_runtime_payload(
            previous=stale_runtime,
            attempt="2026-07-23T09:00:00+00:00",
            success=False,
            failure_reason="failed",
            mode="public",
            repo="owner/current",
            base_repo="owner/current",
            owner=MODULE.PUBLIC_OWNER,
        )
        self.assertIsNone(next_payload["last_success"])

    def test_linux_pair_transaction_recovers_crash_and_preserves_interval(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/old",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        real_write = MODULE._write_text
        injected = False

        def fail_before_timer(
            path: Path,
            content: str,
            *,
            dry_run: bool,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> None:
            nonlocal injected
            if path == paths.systemd_timer and not injected:
                injected = True
                raise MODULE.SyncError("injected pair crash")
            real_write(
                path,
                content,
                dry_run=dry_run,
                expected_snapshot=expected_snapshot,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text",
                side_effect=fail_before_timer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "pair crash"),
        ):
            self.install_scheduler_quietly(
                "owner/new",
                None,
                "linux",
            )

        self.assertTrue(
            MODULE._scheduler_pair_transaction_path(paths).is_file()
        )
        self.install_scheduler_quietly(
            "owner/new",
            None,
            "linux",
        )
        self.assertFalse(
            MODULE._scheduler_pair_transaction_path(paths).exists()
        )
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/new")
        self.assertEqual(config.interval_minutes, 17)

    def test_concurrent_linux_installs_serialize_recovery_through_cleanup(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        first_publication_paused = threading.Event()
        release_first_publication = threading.Event()
        second_install_started = threading.Event()
        second_recovery_entered = threading.Event()
        errors: dict[str, BaseException] = {}
        real_write = MODULE._write_text
        real_recover = MODULE._recover_scheduler_pair_transaction

        def pause_first_publication(
            path: Path,
            content: str,
            *,
            dry_run: bool,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> None:
            if (
                threading.current_thread().name == "first-scheduler-install"
                and path == paths.systemd_service
            ):
                first_publication_paused.set()
                if not release_first_publication.wait(5):
                    raise AssertionError(
                        "first scheduler publication was not released"
                    )
            real_write(
                path,
                content,
                dry_run=dry_run,
                expected_snapshot=expected_snapshot,
            )

        def observe_recovery(
            selected_paths: MODULE.SchedulerPaths,
            *,
            dry_run: bool,
        ) -> bool:
            if threading.current_thread().name == "second-scheduler-install":
                second_recovery_entered.set()
            return real_recover(selected_paths, dry_run=dry_run)

        def install(name: str, repo: str, interval: int) -> None:
            if name == "second":
                second_install_started.set()
            try:
                MODULE.install_scheduler(
                    self.home,
                    repo,
                    interval,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            except BaseException as error:
                errors[name] = error

        first = threading.Thread(
            target=install,
            args=("first", "owner/first", 17),
            name="first-scheduler-install",
            daemon=True,
        )
        second = threading.Thread(
            target=install,
            args=("second", "owner/second", 29),
            name="second-scheduler-install",
            daemon=True,
        )
        with (
            mock.patch.object(
                MODULE,
                "_write_text",
                side_effect=pause_first_publication,
            ),
            mock.patch.object(
                MODULE,
                "_recover_scheduler_pair_transaction",
                side_effect=observe_recovery,
            ),
        ):
            try:
                first.start()
                self.assertTrue(first_publication_paused.wait(5))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )

                second.start()
                self.assertTrue(second_install_started.wait(5))
                self.assertFalse(second_recovery_entered.wait(0.2))
                self.assertTrue(
                    MODULE._scheduler_pair_transaction_path(paths).is_file()
                )
            finally:
                release_first_publication.set()
                if first.ident is not None:
                    first.join(5)
                if second.ident is not None:
                    second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, {})
        self.assertTrue(second_recovery_entered.is_set())
        self.assertFalse(
            MODULE._scheduler_pair_transaction_path(paths).exists()
        )
        config = MODULE._load_linux_scheduler_config(paths)
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repo, "owner/second")
        self.assertEqual(config.interval_minutes, 29)

    def test_linux_pair_transaction_refuses_concurrent_editor_state(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_write = MODULE._write_text
        injected = False

        def edit_before_conditional_write(
            path: Path,
            content: str,
            *,
            dry_run: bool,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
        ) -> None:
            nonlocal injected
            if path == paths.systemd_service and not injected:
                injected = True
                path.write_text("user edit\n", encoding="utf-8")
                path.chmod(0o600)
            real_write(
                path,
                content,
                dry_run=dry_run,
                expected_snapshot=expected_snapshot,
            )

        with (
            mock.patch.object(
                MODULE,
                "_write_text",
                side_effect=edit_before_conditional_write,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "service changed during pending pair transaction",
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")
        self.assertEqual(
            paths.systemd_service.read_text(encoding="utf-8"),
            "user edit\n",
        )

    def test_macos_install_binds_semantically_audited_snapshot(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "macos")
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited launchd config\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.launchd_plist.write_bytes(replacement)
            paths.launchd_plist.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "macos")

        self.assertEqual(paths.launchd_plist.read_bytes(), replacement)

    def test_linux_install_binds_semantically_audited_pair(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly("owner/old", 17, "linux")
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        original_timer = paths.systemd_timer.read_bytes()
        real_audit = MODULE._audit_scheduler_config
        replacement = b"user edited systemd service\n"

        def audit_then_edit(
            selected_paths: MODULE.SchedulerPaths,
        ) -> MODULE.SchedulerConfigAudit:
            audit = real_audit(selected_paths)
            paths.systemd_service.write_bytes(replacement)
            paths.systemd_service.chmod(0o600)
            return audit

        with (
            mock.patch.object(
                MODULE,
                "_audit_scheduler_config",
                side_effect=audit_then_edit,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "changed before conditional publication",
            ),
        ):
            self.install_scheduler_quietly("owner/new", None, "linux")

        self.assertEqual(paths.systemd_service.read_bytes(), replacement)
        self.assertEqual(paths.systemd_timer.read_bytes(), original_timer)
        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())

    def test_scheduler_exchange_never_restores_a_replaced_displaced_path(
        self,
    ) -> None:
        paths = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist",
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service",
        )
        for config_path in paths:
            with self.subTest(path=config_path):
                config_path.parent.mkdir(parents=True, exist_ok=True)
                original = f"original:{config_path.name}\n".encode()
                replacement = f"replacement:{config_path.name}\n".encode()
                attacker = f"attacker:{config_path.name}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                original_identity = (
                    config_path.stat().st_dev,
                    config_path.stat().st_ino,
                )
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_exchange = MODULE._rename_exchange_at
                exchange_count = 0

                def exchange_then_replace_displaced(
                    first_parent_fd: int,
                    first_name: str,
                    second_parent_fd: int,
                    second_name: str,
                ) -> None:
                    nonlocal exchange_count
                    real_exchange(
                        first_parent_fd,
                        first_name,
                        second_parent_fd,
                        second_name,
                    )
                    exchange_count += 1
                    if exchange_count == 1:
                        displaced_path = config_path.with_name(first_name)
                        displaced_path.unlink()
                        displaced_path.write_bytes(attacker)
                        displaced_path.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE,
                        "_rename_exchange_at",
                        side_effect=exchange_then_replace_displaced,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "trusted staged config remains live.*exact original "
                        "is preserved",
                    ),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertEqual(exchange_count, 1)
                self.assertEqual(config_path.read_bytes(), replacement)
                self.assertNotEqual(config_path.read_bytes(), attacker)
                recovery_paths = list(
                    config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*.original"
                    )
                )
                self.assertEqual(len(recovery_paths), 1)
                self.assertEqual(recovery_paths[0].read_bytes(), original)
                self.assertEqual(
                    (
                        recovery_paths[0].stat().st_dev,
                        recovery_paths[0].stat().st_ino,
                    ),
                    original_identity,
                )
                displaced_paths = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced_paths), 1)
                self.assertEqual(displaced_paths[0].read_bytes(), attacker)

    def test_matching_scheduler_revalidates_semantic_audit_before_daemon(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/current",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                real_audit = MODULE._audit_scheduler_config
                replacement = f"user edited {platform_name} config\n".encode()

                def audit_then_edit(
                    selected_paths: MODULE.SchedulerPaths,
                ) -> MODULE.SchedulerConfigAudit:
                    audit = real_audit(selected_paths)
                    target.write_bytes(replacement)
                    target.chmod(0o600)
                    return audit

                with (
                    mock.patch.object(
                        MODULE,
                        "_audit_scheduler_config",
                        side_effect=audit_then_edit,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                    ) as run_native,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "changed after semantic audit",
                    ),
                ):
                    self.install_scheduler_quietly(
                        "owner/current",
                        None,
                        platform_name,
                        enable=True,
                    )

                run_native.assert_not_called()
                self.assertEqual(target.read_bytes(), replacement)

    def test_scheduler_after_snapshot_ignores_non_access_bearing_group(self) -> None:
        payload = b"service\n"
        wrong_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 2),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid() + 1,
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                wrong_group,
                payload,
            )
        )
        expected_group = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=payload,
            mode=0o600,
            file_identity=(1, 3),
            file_type=stat.S_IFREG,
            size=len(payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )
        self.assertTrue(
            MODULE._scheduler_snapshot_matches_after_payload(
                expected_group,
                payload,
            )
        )

    def test_scheduler_rollback_restores_before_group_in_setgid_parent(
        self,
    ) -> None:
        caller_groups = {os.getegid(), *os.getgroups()}
        alternate_groups = sorted(caller_groups - {os.getegid()})
        if not alternate_groups:
            self.skipTest("caller has no alternate group for setgid regression")
        parent_gid = alternate_groups[0]
        config_parent = self.home / "scheduler-group-rollback"
        config_parent.mkdir()
        try:
            os.chown(config_parent, -1, parent_gid)
            config_parent.chmod(0o2755)
        except PermissionError as error:
            self.skipTest(f"cannot prepare setgid regression directory: {error}")
        if config_parent.stat().st_gid != parent_gid:
            self.skipTest("filesystem did not preserve requested parent group")

        config = config_parent / "service.conf"
        config.write_bytes(b"after\n")
        config.chmod(0o600)
        current = MODULE._scheduler_config_snapshot(config)
        if current.gid != parent_gid:
            self.skipTest("filesystem did not inherit setgid parent group")
        desired_payload = b"before\n"
        desired = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=desired_payload,
            mode=0o640,
            file_identity=(1, 1),
            file_type=stat.S_IFREG,
            size=len(desired_payload),
            uid=os.geteuid(),
            gid=os.getegid(),
        )

        MODULE._restore_scheduler_config_snapshot(
            config,
            current=current,
            desired=desired,
        )

        restored = MODULE._scheduler_config_snapshot(config)
        self.assertTrue(
            MODULE._scheduler_file_logical_state_matches(restored, desired)
        )

    def test_scheduler_report_requires_every_configured_current(self) -> None:
        public_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "public.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        private_config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "private.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="private",
            repo="owner/private-sync",
            base_repo="owner/public-sync",
            owner="private",
        )

        cases = (
            (
                "public",
                public_config,
                lambda _home, _owner: None,
                MODULE.PUBLIC_OWNER,
            ),
            (
                "private",
                private_config,
                lambda _home, owner: (
                    PUBLIC_SHA if owner == MODULE.PUBLIC_OWNER else None
                ),
                "private",
            ),
        )
        for label, config, current_sha, expected_owner in cases:
            with (
                self.subTest(label=label),
                mock.patch.object(
                    MODULE,
                    "_load_scheduler_config",
                    return_value=config,
                ),
                mock.patch.object(
                    MODULE,
                    "_read_scheduler_runtime_state",
                    return_value=None,
                ),
                mock.patch.object(
                    MODULE,
                    "_current_sha",
                    side_effect=current_sha,
                ),
                mock.patch.object(
                    MODULE,
                    "_quarantine_batch_count",
                    return_value=0,
                ),
                mock.patch.object(
                    MODULE,
                    "_scheduler_daemon_enabled",
                    return_value=True,
                ),
            ):
                report = MODULE.scheduler_report(self.home, "linux")

            self.assertEqual(report.failure_code, "current-release-missing")
            self.assertIn(expected_owner, report.failure_reason or "")
            self.assertEqual(report.current_releases, ())

    def test_quarantine_audit_preserves_prior_codeless_runtime_failure(
        self,
    ) -> None:
        config = MODULE.SchedulerConfig(
            platform="linux",
            config_paths=(self.root / "scheduler.timer",),
            interval_minutes=60,
            runner=self.home / "bin" / "codex-personal-sync",
            home=self.home,
            command="run-scheduled",
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-23T09:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "legacy failure without a code",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        with (
            mock.patch.object(
                MODULE,
                "_load_scheduler_config",
                return_value=config,
            ),
            mock.patch.object(
                MODULE,
                "_read_scheduler_runtime_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                MODULE,
                "_current_releases_for_scheduler",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_release_integrity_issues",
                return_value=(),
            ),
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                side_effect=MODULE.SyncError("audit unavailable"),
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_daemon_enabled",
                return_value=True,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            report.failure_reason,
            "legacy failure without a code",
        )
        self.assertIsNone(report.failure_code)

    def test_runtime_v1_is_normalized_and_failure_code_is_persisted(
        self,
    ) -> None:
        previous_success = "2026-07-22T08:00:00+00:00"
        MODULE._write_scheduler_runtime_state(
            self.home,
            {
                "version": 1,
                "last_attempt": previous_success,
                "last_success": previous_success,
                "success": True,
                "failure_reason": None,
                "mode": "public",
                "repo": "owner/public-sync",
                "base_repo": "owner/public-sync",
                "owner": MODULE.PUBLIC_OWNER,
            },
        )
        normalized = MODULE._read_scheduler_runtime_state(self.home)
        assert normalized is not None
        self.assertEqual(normalized["version"], 2)
        self.assertIsNone(normalized["failure_code"])

        with (
            mock.patch.object(
                MODULE,
                "install_from_github",
                side_effect=MODULE.SyncError(
                    "existing release tree differs",
                    code="immutable-release-drift",
                ),
            ),
            self.assertRaises(MODULE.SyncError),
        ):
            MODULE.run_scheduled(
                self.home,
                "owner/public-sync",
                mode="public",
                base_repo="owner/ignored",
                owner="private",
            )
        failed = MODULE._read_scheduler_runtime_state(self.home)
        assert failed is not None
        self.assertEqual(failed["version"], 2)
        self.assertEqual(failed["failure_code"], "immutable-release-drift")
        self.assertEqual(failed["last_success"], previous_success)

    def test_doctor_reports_quarantine_saturation_without_mutation(self) -> None:
        quarantine = (
            self.home
            / "personal-sync"
            / MODULE.QUARANTINE_RELATIVE_PATH
        )
        quarantine.mkdir(parents=True)
        for index in range(MODULE.MAX_RETAINED_QUARANTINE_BATCHES):
            prefix = (
                MODULE.PENDING_CLEANUP_ISOLATED_BATCH_PREFIX
                if index % 2
                else ""
            )
            (
                quarantine
                / f"{prefix}20260723T000000Z-{index + 1}-{index + 1}"
            ).mkdir()
        before = snapshot_tree(quarantine)

        with contextlib.redirect_stdout(io.StringIO()):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=True,
            )

        self.assertEqual(
            report.quarantine_batches,
            MODULE.MAX_RETAINED_QUARANTINE_BATCHES,
        )
        saturated = [
            issue for issue in issues if issue.code == "quarantine-saturated"
        ]
        self.assertEqual(len(saturated), 1)
        self.assertIn(
            f">= {MODULE.MAX_RETAINED_QUARANTINE_BATCHES}",
            saturated[0].detail,
        )
        self.assertEqual(snapshot_tree(quarantine), before)

    def test_native_scheduler_commands_use_closed_environment(self) -> None:
        completed = MODULE.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        injected = {
            "LD_PRELOAD": "/tmp/injected.so",
            "LD_LIBRARY_PATH": "/tmp/injected",
            "DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib",
            "BASH_ENV": "/tmp/bash-env",
            "ENV": "/tmp/shell-env",
            "PYTHONPATH": "/tmp/python",
        }
        with (
            mock.patch.dict(MODULE.os.environ, injected, clear=False),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                return_value=["/usr/bin/systemctl", "--user", "daemon-reload"],
            ),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            MODULE._run_native_command(
                ["systemctl", "--user", "daemon-reload"],
                dry_run=False,
            )

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LC_ALL"], "C")
        for name in injected:
            self.assertNotIn(name, environment)

    def test_active_skill_audit_detects_root_replacement_from_bound_fd(
        self,
    ) -> None:
        skills = self.home / "skills"
        original = skills / "original"
        original.mkdir(parents=True)
        (original / "SKILL.md").write_text(
            "---\nname: original\n---\n",
            encoding="utf-8",
        )
        replacement = self.home / "replacement-skills"
        injected = replacement / "injected"
        injected.mkdir(parents=True)
        (injected / "SKILL.md").write_text(
            "---\nname: injected\n---\n",
            encoding="utf-8",
        )
        retained = self.home / "retained-skills"
        real_names = MODULE._bounded_skill_child_names
        swapped = False

        def list_then_swap(directory_fd: int) -> tuple[str, ...]:
            nonlocal swapped
            names = real_names(directory_fd)
            if not swapped:
                swapped = True
                skills.rename(retained)
                replacement.rename(skills)
            return names

        with mock.patch.object(
            MODULE,
            "_bounded_skill_child_names",
            side_effect=list_then_swap,
        ):
            issues = MODULE.audit_active_skills(self.home)

        self.assertTrue(swapped)
        self.assertIn("skills-root-unsafe", {issue.code for issue in issues})
        self.assertFalse(
            any("injected" in issue.detail for issue in issues)
        )

    def test_uninstall_refuses_scheduler_symlink_without_deleting_target(
        self,
    ) -> None:
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        paths.systemd_service.parent.mkdir(parents=True)
        target = self.root / "outside-service"
        target.write_text("keep\n", encoding="utf-8")
        paths.systemd_service.symlink_to(target)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "non-file sync state",
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=False,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(paths.systemd_service.is_symlink())

    def test_active_skill_scan_is_bounded_and_requires_closed_frontmatter(
        self,
    ) -> None:
        skills = self.home / "skills"
        skills.mkdir()
        for name in ("one", "two", "three"):
            root = skills / name
            root.mkdir()
            (root / "SKILL.md").write_text(
                f"---\nname: {name}\n---\n",
                encoding="utf-8",
            )
        with mock.patch.object(MODULE, "MAX_ACTIVE_SKILL_ENTRIES", 2):
            issues = MODULE.audit_active_skills(self.home)
        self.assertIn("skills-root-overflow", {issue.code for issue in issues})

        shutil.rmtree(skills)
        malformed = skills / "malformed"
        malformed.mkdir(parents=True)
        (malformed / "SKILL.md").write_text(
            "---\nname: missing-close\n",
            encoding="utf-8",
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("invalid-frontmatter", {issue.code for issue in issues})

        shutil.rmtree(skills)
        large = skills / "large-body"
        large.mkdir(parents=True)
        (large / "SKILL.md").write_bytes(
            b"---\nname: large-body\n---\n"
            + b"x" * (MODULE.MAX_SKILL_FRONTMATTER_BYTES + 1024)
            + b"\xff"
        )
        issues = MODULE.audit_active_skills(self.home)
        self.assertIn("unmanaged-skill", {issue.code for issue in issues})
        self.assertNotIn("invalid-frontmatter", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
