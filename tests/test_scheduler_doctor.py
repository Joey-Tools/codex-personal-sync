from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
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

    def write_pending_systemd_pair(
        self,
        user_home: Path,
        home: Path,
    ) -> MODULE.SchedulerPaths:
        runner = home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        with mock.patch.object(MODULE.Path, "home", return_value=user_home):
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.install_scheduler(
                    home,
                    "owner/old",
                    17,
                    "linux",
                    None,
                    dry_run=False,
                    enable=False,
                )
            paths = MODULE._scheduler_paths("linux", home)
            assert paths.systemd_service is not None
            assert paths.systemd_timer is not None
            service_before = MODULE._scheduler_config_snapshot(paths.systemd_service)
            timer_before = MODULE._scheduler_config_snapshot(paths.systemd_timer)
            service_after = MODULE._systemd_service(
                home,
                "owner/new",
                runner,
            ).encode("utf-8")
            timer_after = MODULE._systemd_timer(29).encode("utf-8")
            marker = MODULE._scheduler_pair_transaction_path(paths)
            marker_before = MODULE._scheduler_config_snapshot(
                marker,
                MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
            )
            MODULE._atomic_write_scheduler_config(
                marker,
                MODULE._scheduler_pair_transaction_payload(
                    service_before=service_before,
                    timer_before=timer_before,
                    service_after=service_after,
                    timer_after=timer_after,
                ),
                expected_snapshot=marker_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_service,
                service_after,
                expected_snapshot=service_before,
            )
            MODULE._atomic_write_scheduler_config(
                paths.systemd_timer,
                timer_after,
                expected_snapshot=timer_before,
            )
        return paths

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
                "daemon_query": {
                    "classification": "enabled",
                    "reason": None,
                },
                "failures": [
                    {
                        "code": None,
                        "reason": "network unavailable",
                    },
                    {
                        "code": "scheduler-runner-drift",
                        "reason": (
                            "scheduler does not use the stable installed runner path"
                        ),
                    },
                ],
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

    def test_scheduler_runtime_cas_does_not_restore_unproven_late_replacement(
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
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0
        reader_blocked = False

        def replace_live_then_exchange(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count, reader_blocked
            exchange_count += 1
            if exchange_count == 1:
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state while publication marker "
                        "was active"
                    )
                marker_path.unlink()
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
                "could not prove that the temporary object is the exact state "
                "displaced",
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

        self.assertEqual(exchange_count, 1)
        self.assertTrue(reader_blocked)
        self.assertNotEqual(status_path.read_bytes(), newer)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
        displaced_paths = [
            candidate
            for candidate in status_path.parent.glob(
                f".{status_path.name}.personal-sync-write-*"
            )
            if not candidate.name.endswith(".original")
        ]
        self.assertEqual(len(displaced_paths), 1)
        self.assertEqual(displaced_paths[0].read_bytes(), newer)
        self.assertTrue(marker_path.is_file())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "unresolved publication marker",
        ) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        report = MODULE.scheduler_report(self.home, "linux")
        self.assertEqual(
            report.failure_code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIsNone(report.last_attempt)
        self.assertIsNone(report.recent_success)

    def test_scheduler_runtime_residue_blocks_when_marker_rebuild_fails(
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
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        newer = b"foreign state that won the late race\n"
        real_create = MODULE._create_scheduler_runtime_publication_marker
        real_exchange = MODULE._rename_exchange_at
        create_calls = 0

        def create_once_then_fail(
            user_home: Path,
            path: Path,
            parent_fd: int,
            payload: bytes,
        ) -> MODULE.ManagedStateFileSnapshot:
            nonlocal create_calls
            create_calls += 1
            if create_calls == 1:
                return real_create(
                    user_home,
                    path,
                    parent_fd,
                    payload,
                )
            raise MODULE.SyncError("injected marker recreation failure")

        def delete_marker_and_replace_live(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            marker_path.unlink()
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
                "_create_scheduler_runtime_publication_marker",
                side_effect=create_once_then_fail,
            ),
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=delete_marker_and_replace_live,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "marker recreation failure",
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

        self.assertEqual(create_calls, 3)
        self.assertFalse(marker_path.exists())
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_cas_never_swaps_unproven_temp_back_to_live(
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
        attacker = b"attacker-controlled temporary replacement\n"
        saved_displaced = status_path.parent / "saved-displaced-status"
        real_exchange = MODULE._rename_exchange_at
        exchange_count = 0

        def swap_displaced_temp(
            first_parent_fd: int,
            first_name: str,
            second_parent_fd: int,
            second_name: str,
        ) -> None:
            nonlocal exchange_count
            exchange_count += 1
            real_exchange(
                first_parent_fd,
                first_name,
                second_parent_fd,
                second_name,
            )
            if exchange_count != 1:
                return
            saved_displaced.write_bytes(attacker)
            saved_displaced.chmod(0o600)
            real_exchange(
                first_parent_fd,
                first_name,
                first_parent_fd,
                saved_displaced.name,
            )

        with (
            mock.patch.object(
                MODULE,
                "_rename_exchange_at",
                side_effect=swap_displaced_temp,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "could not prove that the temporary object is the exact state "
                "displaced",
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

        self.assertEqual(exchange_count, 1)
        self.assertNotEqual(status_path.read_bytes(), attacker)
        self.assertEqual(
            json.loads(status_path.read_text(encoding="utf-8"))["last_attempt"],
            attempt,
        )
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
        self.assertEqual(displaced_paths[0].read_bytes(), attacker)
        self.assertEqual(saved_displaced.read_bytes(), original)
        self.assertTrue(
            MODULE._scheduler_runtime_publication_marker_path(status_path).is_file()
        )

    def test_scheduler_runtime_reader_rejects_marker_appearing_after_read(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_check = MODULE._scheduler_runtime_publication_is_incomplete
        checks = 0

        def appear_on_post_check(
            home: Path,
            checked_status_path: Path,
            parent_fd: int,
        ) -> bool:
            nonlocal checks
            checks += 1
            if checks == 2:
                marker_path.write_text(
                    '{"version":1,"status":"incomplete"}\n',
                    encoding="utf-8",
                )
                marker_path.chmod(0o600)
            return real_check(home, checked_status_path, parent_fd)

        with (
            mock.patch.object(
                MODULE,
                "_scheduler_runtime_publication_is_incomplete",
                side_effect=appear_on_post_check,
            ),
            self.assertRaises(MODULE.SyncError) as error_context,
        ):
            MODULE._read_scheduler_runtime_state(self.home)

        self.assertEqual(checks, 2)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

    def test_scheduler_runtime_reader_directly_stats_fixed_marker(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(status_path.name,),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_casefold_aliases(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        transaction_prefix = f".{status_path.name}.personal-sync-write-"
        retained_marker_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{marker_path.name}-"
        )
        retained_transaction_prefix = (
            f"{MODULE.PENDING_CLEANUP_RETAINED_PREFIX}{transaction_prefix}"
        )
        aliases = (
            marker_path.name.swapcase(),
            f"{transaction_prefix.swapcase()}case-alias",
            f"{retained_marker_prefix.swapcase()}case-alias",
            f"{retained_transaction_prefix.swapcase()}case-alias",
        )
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            for alias in aliases:
                with (
                    self.subTest(alias=alias),
                    mock.patch.object(
                        MODULE,
                        "_directory_member_names",
                        return_value=(status_path.name, alias),
                    ),
                ):
                    self.assertTrue(
                        MODULE._scheduler_runtime_publication_is_incomplete(
                            self.home,
                            status_path,
                            parent_fd,
                        )
                    )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_nfd_residue_alias(self) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-nfd-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with mock.patch.object(
                MODULE,
                "_directory_member_names",
                return_value=(canonical_status_path.name, nfd_residue),
            ):
                self.assertTrue(
                    MODULE._scheduler_runtime_publication_is_incomplete(
                        self.home,
                        status_path,
                        parent_fd,
                    )
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)

    def test_scheduler_runtime_reader_rejects_normalized_alias_collision(
        self,
    ) -> None:
        MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        canonical_status_path = MODULE._scheduler_status_path(self.home)
        status_path = canonical_status_path.with_name("scheduler-Café.json")
        nfc_residue = ".scheduler-Café.json.personal-sync-write-normalized-alias"
        nfd_residue = ".scheduler-Cafe\u0301.json.personal-sync-write-normalized-alias"
        parent_fd = MODULE._open_directory_beneath(
            self.home,
            status_path.parent,
        )
        try:
            with (
                mock.patch.object(
                    MODULE,
                    "_directory_member_names",
                    return_value=(
                        canonical_status_path.name,
                        nfc_residue,
                        nfd_residue,
                    ),
                ),
                self.assertRaises(MODULE.SyncError) as error_context,
            ):
                MODULE._scheduler_runtime_publication_is_incomplete(
                    self.home,
                    status_path,
                    parent_fd,
                )
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertIn(
            "ambiguous portable aliases",
            str(error_context.exception),
        )

    def test_scheduler_runtime_writer_refuses_preexisting_marker(self) -> None:
        attempt = MODULE._begin_scheduler_attempt(
            self.home,
            mode="public",
            repo="owner/public-sync",
            base_repo="owner/public-sync",
            owner=MODULE.PUBLIC_OWNER,
        )
        status_path = MODULE._scheduler_status_path(self.home)
        original = status_path.read_bytes()
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        marker_path.write_text(
            '{"version":1,"status":"incomplete"}\n',
            encoding="utf-8",
        )
        marker_path.chmod(0o600)

        with self.assertRaises(MODULE.SyncError) as error_context:
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

        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )
        self.assertEqual(status_path.read_bytes(), original)

    def test_scheduler_runtime_marker_cleanup_failure_remains_fail_closed(
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
        marker_path = MODULE._scheduler_runtime_publication_marker_path(status_path)
        real_commit = MODULE._commit_scheduler_runtime_publication_marker
        injected = False
        reader_blocked = False

        def replace_marker_before_commit(
            user_home: Path,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> None:
            nonlocal injected, reader_blocked
            if not injected:
                injected = True
                try:
                    MODULE._read_scheduler_runtime_state(self.home)
                except MODULE.SyncError as error:
                    reader_blocked = (
                        error.code == "scheduler-state-publication-incomplete"
                    )
                else:
                    self.fail(
                        "runtime reader accepted state before the marker commit "
                        "linearization point"
                    )
                marker_path.unlink()
                marker_path.write_bytes(b"foreign marker replacement\n")
                marker_path.chmod(0o600)
            real_commit(
                user_home,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE,
                "_commit_scheduler_runtime_publication_marker",
                side_effect=replace_marker_before_commit,
            ),
            self.assertRaises(MODULE.SyncError),
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

        self.assertTrue(injected)
        self.assertTrue(reader_blocked)
        self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["success"])
        self.assertTrue(marker_path.exists())
        with self.assertRaises(MODULE.SyncError) as error_context:
            MODULE._read_scheduler_runtime_state(self.home)
        self.assertEqual(
            error_context.exception.code,
            "scheduler-state-publication-incomplete",
        )

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
        self.assertFalse(
            MODULE._scheduler_runtime_publication_marker_path(status_path).exists()
        )

    def test_scheduler_runtime_cas_rejects_parent_rotation_with_same_file_inode(
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
        original_payload = status_path.read_bytes()
        original_identity = (
            status_path.stat().st_dev,
            status_path.stat().st_ino,
        )
        rotated_parent = status_path.parent.with_name(
            status_path.parent.name + "-rotated"
        )
        real_publish = MODULE._atomic_write_scheduler_config

        def rotate_parent_before_publish(
            path: Path,
            payload: bytes,
            *,
            expected_snapshot: MODULE.ManagedStateFileSnapshot | None = None,
            mode: int = 0o600,
            gid: int | None = None,
            rollback_displaced_conflict: bool = False,
        ) -> None:
            status_path.parent.rename(rotated_parent)
            status_path.parent.mkdir(mode=0o700)
            os.link(
                rotated_parent / status_path.name,
                status_path,
                follow_symlinks=False,
            )
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
                side_effect=rotate_parent_before_publish,
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

        self.assertEqual(status_path.read_bytes(), original_payload)
        self.assertEqual(
            (status_path.stat().st_dev, status_path.stat().st_ino),
            original_identity,
        )
        self.assertEqual(
            (
                (rotated_parent / status_path.name).stat().st_dev,
                (rotated_parent / status_path.name).stat().st_ino,
            ),
            original_identity,
        )
        self.assertEqual(
            list(status_path.parent.glob(f".{status_path.name}.personal-sync-write-*")),
            [],
        )

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
        ) -> MODULE.ManagedStateFileSnapshot | None:
            nonlocal injected
            if path == paths.systemd_timer and not injected:
                injected = True
                raise MODULE.SyncError("injected pair crash")
            return real_write(
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

        self.assertTrue(MODULE._scheduler_pair_transaction_path(paths).is_file())
        self.install_scheduler_quietly(
            "owner/new",
            None,
            "linux",
        )
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
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
        ) -> MODULE.ManagedStateFileSnapshot | None:
            if (
                threading.current_thread().name == "first-scheduler-install"
                and path == paths.systemd_service
            ):
                first_publication_paused.set()
                if not release_first_publication.wait(5):
                    raise AssertionError("first scheduler publication was not released")
            return real_write(
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
        self.assertFalse(MODULE._scheduler_pair_transaction_path(paths).exists())
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
        ) -> MODULE.ManagedStateFileSnapshot | None:
            nonlocal injected
            if path == paths.systemd_service and not injected:
                injected = True
                path.write_text("user edit\n", encoding="utf-8")
                path.chmod(0o600)
            return real_write(
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

    def test_systemd_pair_recovery_retains_marker_on_member_replacement(
        self,
    ) -> None:
        real_parse = MODULE._parse_scheduler_pair_transaction
        for target_kind in ("service", "timer"):
            with self.subTest(target=target_kind):
                case_user_home = self.root / f"pair-replacement-{target_kind}" / "home"
                case_home = case_user_home / ".codex"
                paths = self.write_pending_systemd_pair(
                    case_user_home,
                    case_home,
                )
                target = (
                    paths.systemd_service
                    if target_kind == "service"
                    else paths.systemd_timer
                )
                assert target is not None
                marker = MODULE._scheduler_pair_transaction_path(paths)
                replaced = False

                def parse_then_replace(
                    payload: bytes,
                    path: Path,
                ) -> tuple[
                    MODULE.ManagedStateFileSnapshot,
                    MODULE.ManagedStateFileSnapshot,
                    bytes,
                    bytes,
                ]:
                    nonlocal replaced
                    parsed = real_parse(payload, path)
                    replacement = target.with_name(target.name + ".replacement")
                    replacement.write_bytes(target.read_bytes())
                    replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                    os.replace(replacement, target)
                    replaced = True
                    return parsed

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_parse_scheduler_pair_transaction",
                        side_effect=parse_then_replace,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ),
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(replaced)
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_revalidates_absent_group_after_parent_sync(
        self,
    ) -> None:
        for mutation in ("during-fsync", "during-second-pass"):
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"pair-absent-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths("linux", case_home)
                assert paths.systemd_service is not None
                assert paths.systemd_timer is not None
                paths.systemd_service.parent.mkdir(parents=True)
                marker = MODULE._scheduler_pair_transaction_path(paths)
                absent = MODULE.ManagedStateFileSnapshot(exists=False)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    marker_before = MODULE._scheduler_config_snapshot(
                        marker,
                        MODULE.MAX_SCHEDULER_PAIR_TRANSACTION_BYTES,
                    )
                    MODULE._atomic_write_scheduler_config(
                        marker,
                        MODULE._scheduler_pair_transaction_payload(
                            service_before=absent,
                            timer_before=absent,
                            service_after=b"future service\n",
                            timer_after=b"future timer\n",
                        ),
                        expected_snapshot=marker_before,
                    )

                real_fsync = MODULE.os.fsync
                real_member_check = MODULE._revalidate_systemd_pair_recovery_member
                armed = False
                injected = False
                timer_checks = 0

                def reappear_service() -> None:
                    nonlocal injected
                    paths.systemd_service.write_bytes(b"concurrent service\n")
                    paths.systemd_service.chmod(0o600)
                    injected = True

                def sync_then_arm_or_reappear(file_fd: int) -> None:
                    nonlocal armed
                    real_fsync(file_fd)
                    if mutation == "during-fsync":
                        reappear_service()
                    else:
                        armed = True

                def reappear_after_earlier_absence_check(
                    group: MODULE.SystemdPairRecoveryGroup,
                    member: MODULE.SystemdPairRecoveryMember,
                ) -> None:
                    nonlocal timer_checks
                    if (
                        mutation == "during-second-pass"
                        and armed
                        and member.path == paths.systemd_timer
                    ):
                        timer_checks += 1
                        if timer_checks == 2:
                            reappear_service()
                    real_member_check(group, member)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "fsync",
                        side_effect=sync_then_arm_or_reappear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_revalidate_systemd_pair_recovery_member",
                        side_effect=reappear_after_earlier_absence_check,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "recovery object group changed",
                    ) as raised,
                ):
                    MODULE._recover_scheduler_pair_transaction(
                        paths,
                        dry_run=False,
                    )

                self.assertTrue(injected)
                self.assertIn(str(paths.systemd_service), str(raised.exception))
                self.assertIn(
                    "after parent sync before transaction marker commit",
                    str(raised.exception),
                )
                self.assertTrue(paths.systemd_service.is_file())
                self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_rechecks_earlier_member_after_later_member(
        self,
    ) -> None:
        case_user_home = self.root / "pair-later-member-race" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        real_matches = MODULE._scheduler_recovery_binding_matches
        armed = False
        replaced = False

        def parse_and_arm(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal armed
            parsed = real_parse(payload, path)
            armed = True
            return parsed

        def replace_service_while_timer_is_checked(
            home: Path,
            file_fd: int,
            path: Path,
            parent_fd: int,
            expected: MODULE.ManagedStateFileSnapshot,
        ) -> bool:
            nonlocal replaced
            if armed and not replaced and path == paths.systemd_timer:
                replacement = paths.systemd_service.with_name(
                    paths.systemd_service.name + ".replacement"
                )
                replacement.write_bytes(paths.systemd_service.read_bytes())
                replacement.chmod(
                    stat.S_IMODE(paths.systemd_service.stat().st_mode)
                )
                os.replace(replacement, paths.systemd_service)
                replaced = True
            return real_matches(
                home,
                file_fd,
                path,
                parent_fd,
                expected,
            )

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_and_arm,
            ),
            mock.patch.object(
                MODULE,
                "_scheduler_recovery_binding_matches",
                side_effect=replace_service_while_timer_is_checked,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(replaced)
        self.assertTrue(marker.is_file())

    def test_systemd_pair_recovery_closes_partial_bindings(self) -> None:
        case_user_home = self.root / "pair-partial-bind" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        real_bind = MODULE._bind_systemd_pair_recovery_member
        bound_fds: list[int] = []

        def bind_marker_then_fail(
            home: Path,
            path: Path,
            parent_fd: int,
            *,
            maximum_bytes: int = 1024 * 1024,
        ) -> MODULE.SystemdPairRecoveryMember:
            if path == paths.systemd_service:
                raise MODULE.SyncError("simulated service binding failure")
            member = real_bind(
                home,
                path,
                parent_fd,
                maximum_bytes=maximum_bytes,
            )
            if member.file_fd >= 0:
                bound_fds.append(member.file_fd)
            return member

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_bind_systemd_pair_recovery_member",
                side_effect=bind_marker_then_fail,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "simulated service binding failure",
            ),
        ):
            with MODULE._retain_systemd_pair_recovery_group(paths):
                self.fail("partial recovery binding unexpectedly succeeded")

        self.assertTrue(bound_fds)
        for file_fd in bound_fds:
            with self.assertRaises(OSError):
                os.fstat(file_fd)

    def test_systemd_pair_recovery_retains_marker_across_parent_aba(
        self,
    ) -> None:
        case_user_home = self.root / "pair-parent-aba" / "home"
        case_home = case_user_home / ".codex"
        paths = self.write_pending_systemd_pair(case_user_home, case_home)
        assert paths.systemd_service is not None
        unit_parent = paths.systemd_service.parent
        displaced_parent = unit_parent.with_name(unit_parent.name + ".displaced")
        transient_parent = unit_parent.with_name(unit_parent.name + ".transient")
        marker = MODULE._scheduler_pair_transaction_path(paths)
        real_parse = MODULE._parse_scheduler_pair_transaction
        rotated = False

        def parse_then_rotate_parent(
            payload: bytes,
            path: Path,
        ) -> tuple[
            MODULE.ManagedStateFileSnapshot,
            MODULE.ManagedStateFileSnapshot,
            bytes,
            bytes,
        ]:
            nonlocal rotated
            parsed = real_parse(payload, path)
            unit_parent.rename(displaced_parent)
            unit_parent.mkdir()
            for source in displaced_parent.iterdir():
                if not source.is_file():
                    continue
                destination = unit_parent / source.name
                destination.write_bytes(source.read_bytes())
                destination.chmod(stat.S_IMODE(source.stat().st_mode))
            rotated = True
            return parsed

        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "_parse_scheduler_pair_transaction",
                side_effect=parse_then_rotate_parent,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "recovery object group changed",
            ),
        ):
            MODULE._recover_scheduler_pair_transaction(
                paths,
                dry_run=False,
            )

        self.assertTrue(rotated)
        self.assertTrue((displaced_parent / marker.name).is_file())
        unit_parent.rename(transient_parent)
        displaced_parent.rename(unit_parent)
        self.assertTrue(marker.is_file())
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(
                MODULE._recover_scheduler_pair_transaction(
                    paths,
                    dry_run=False,
                )
            )
        self.assertFalse(marker.exists())

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

    def test_macos_install_revalidates_bound_plist_at_every_native_boundary(
        self,
    ) -> None:
        action_names = (
            "legacy-bootout",
            "legacy-disable",
            "current-bootout",
            "bootstrap",
            "enable",
        )
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        for action_index, action_name in enumerate(action_names):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_name, mutation=mutation):
                    case_user_home = self.root / f"{action_index}-{mutation}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    assert paths.launchd_plist is not None
                    native_calls = 0
                    force_unreadable = False
                    real_read = MODULE._read_managed_state_bytes

                    def mutate_during_native(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        plist = paths.launchd_plist
                        if mutation == "replacement":
                            replacement = plist.with_name(plist.name + ".replacement")
                            replacement.write_bytes(plist.read_bytes())
                            replacement.chmod(0o600)
                            os.replace(replacement, plist)
                        elif mutation == "content":
                            payload = bytearray(plist.read_bytes())
                            payload[len(payload) // 2] ^= 1
                            plist.write_bytes(payload)
                            plist.chmod(0o600)
                        elif mutation == "mode":
                            plist.chmod(0o644)
                        elif mutation == "parent":
                            old_parent = plist.parent.with_name(
                                plist.parent.name + ".replaced"
                            )
                            plist.parent.rename(old_parent)
                            plist.parent.mkdir()
                            replacement = plist.parent / plist.name
                            replacement.write_bytes(
                                (old_parent / plist.name).read_bytes()
                            )
                            replacement.chmod(0o600)
                        elif mutation == "missing":
                            plist.unlink()
                        else:
                            force_unreadable = True

                    def fail_bound_read(
                        file_fd: int,
                        path: Path,
                        maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                    ) -> bytes:
                        if force_unreadable and path == paths.launchd_plist:
                            raise MODULE.SyncError("injected read failure")
                        return real_read(file_fd, path, maximum_bytes)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_during_native,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_read_managed_state_bytes",
                            side_effect=fail_bound_read,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_prebinds_every_legacy_before_cleanup(self) -> None:
        case_user_home = self.root / "install-all-legacy-bindings" / "home"
        case_home = case_user_home / ".codex"
        runner = case_home / "bin" / "codex-personal-sync"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        labels = (
            "com.joeyteng.codex-personal-sync.one",
            "com.joeyteng.codex-personal-sync.two",
        )
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
        ):
            paths = MODULE._scheduler_paths("macos", case_home)
            second_legacy = MODULE._legacy_launchd_plist(paths, labels[1])
        second_legacy.parent.mkdir(parents=True)
        second_legacy.write_bytes(b"original second legacy\n")
        second_legacy.chmod(0o600)
        replacement = b"replacement second legacy\n"
        native_calls = 0

        def replace_second_legacy_during_first_cleanup_action(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            candidate = second_legacy.with_name(second_legacy.name + ".replacement")
            candidate.write_bytes(replacement)
            candidate.chmod(0o600)
            os.replace(candidate, second_legacy)

        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE.Path,
                "home",
                return_value=case_user_home,
            ),
            mock.patch.object(
                MODULE,
                "LEGACY_LAUNCHD_LABELS",
                labels,
            ),
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=replace_second_legacy_during_first_cleanup_action,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "object identity changed",
            ),
        ):
            MODULE.install_scheduler(
                case_home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(native_calls, 1)
        self.assertEqual(second_legacy.read_bytes(), replacement)
        assert paths.launchd_plist is not None
        self.assertTrue(paths.launchd_plist.exists())
        self.assertNotIn(
            "installed macOS launchd scheduler",
            output.getvalue(),
        )

    def test_macos_install_retains_legacy_absence_through_current_actions(
        self,
    ) -> None:
        legacy_action_count = len(MODULE.LEGACY_LAUNCHD_LABELS) * 2
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for initial_legacy_exists in (False, True):
            for current_action_offset in range(3):
                case_index += 1
                with self.subTest(
                    initial_legacy_exists=initial_legacy_exists,
                    current_action=current_action_offset,
                ):
                    case_user_home = (
                        self.root / f"install-legacy-retained-{case_index}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    if initial_legacy_exists:
                        legacy.write_bytes(b"original legacy scheduler\n")
                        legacy.chmod(0o600)
                    payload = (
                        f"new legacy {initial_legacy_exists} {current_action_offset}\n"
                    ).encode("utf-8")
                    mutation_action = legacy_action_count + current_action_offset
                    native_calls = 0

                    def reappear_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != mutation_action:
                            return
                        legacy.write_bytes(payload)
                        legacy.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=reappear_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            (
                                "reappeared after conditional removal"
                                if initial_legacy_exists
                                else "appeared after initial absence"
                            ),
                        ),
                    ):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=True,
                        )

                    self.assertEqual(native_calls, mutation_action + 1)
                    self.assertEqual(legacy.read_bytes(), payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn(
                        "installed macOS launchd scheduler",
                        output.getvalue(),
                    )

    def test_macos_install_allows_mtime_only_churn_at_native_boundaries(
        self,
    ) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        assert paths.launchd_plist is not None
        native_calls = 0

        def touch_during_native(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            nonlocal native_calls
            native_calls += 1
            metadata = paths.launchd_plist.stat()
            os.utime(
                paths.launchd_plist,
                ns=(
                    metadata.st_atime_ns,
                    metadata.st_mtime_ns + 1_000_000,
                ),
            )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_during_native,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertEqual(
            native_calls,
            len(MODULE.LEGACY_LAUNCHD_LABELS) * 2 + 3,
        )
        self.assertIsNotNone(MODULE._load_macos_scheduler_config(paths))

    def test_legacy_launchd_cleanup_binds_file_across_native_actions(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for mutation, expected_error in mutations:
            with self.subTest(mutation=mutation):
                case_user_home = self.root / f"legacy-{mutation}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                paths = MODULE.SchedulerPaths(
                    platform="macos",
                    launchd_plist=(
                        case_user_home
                        / "Library"
                        / "LaunchAgents"
                        / f"{MODULE.LAUNCHD_LABEL}.plist"
                    ),
                )
                legacy = MODULE._legacy_launchd_plist(paths, label)
                legacy.parent.mkdir(parents=True)
                original = b"legacy launchd config\n"
                replacement = b"user replacement config\n"
                legacy.write_bytes(original)
                legacy.chmod(0o600)
                force_unreadable = False
                real_read = MODULE._read_managed_state_bytes

                def mutate_legacy(
                    _args: list[str],
                    *,
                    dry_run: bool,
                    allow_fail: bool = False,
                ) -> None:
                    del dry_run, allow_fail
                    nonlocal force_unreadable
                    if mutation == "replacement":
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)
                    elif mutation == "content":
                        legacy.write_bytes(replacement)
                        legacy.chmod(0o600)
                    elif mutation == "mode":
                        legacy.chmod(0o644)
                    elif mutation == "parent":
                        displaced_parent = legacy.parent.with_name(
                            legacy.parent.name + ".displaced"
                        )
                        legacy.parent.rename(displaced_parent)
                        legacy.parent.mkdir()
                        candidate = legacy.parent / legacy.name
                        candidate.write_bytes(replacement)
                        candidate.chmod(0o600)
                    elif mutation == "missing":
                        legacy.unlink()
                    else:
                        force_unreadable = True

                def fail_legacy_read(
                    file_fd: int,
                    path: Path,
                    maximum_bytes: int = MODULE.MAX_MANAGED_STATE_BYTES,
                ) -> bytes:
                    if force_unreadable and path == legacy:
                        raise MODULE.SyncError("injected legacy read failure")
                    return real_read(file_fd, path, maximum_bytes)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=mutate_legacy,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_read_managed_state_bytes",
                        side_effect=fail_legacy_read,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(MODULE.SyncError, expected_error),
                ):
                    MODULE.install_scheduler(
                        case_home,
                        "owner/public-sync",
                        17,
                        "macos",
                        None,
                        dry_run=False,
                        enable=True,
                    )

                if mutation in {"replacement", "content"}:
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "mode":
                    self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o644)
                elif mutation == "parent":
                    displaced = legacy.parent.with_name(
                        legacy.parent.name + ".displaced"
                    )
                    self.assertEqual(
                        (displaced / legacy.name).read_bytes(),
                        original,
                    )
                    self.assertEqual(legacy.read_bytes(), replacement)
                elif mutation == "missing":
                    self.assertFalse(legacy.exists())
                else:
                    self.assertEqual(legacy.read_bytes(), original)
    def test_legacy_launchd_cleanup_allows_mtime_only_churn(self) -> None:
        self.write_runner()
        paths = MODULE._scheduler_paths("macos", self.home)
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        legacy = MODULE._legacy_launchd_plist(paths, label)
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy launchd config\n")
        legacy.chmod(0o600)

        def touch_legacy(
            _args: list[str],
            *,
            dry_run: bool,
            allow_fail: bool = False,
        ) -> None:
            del dry_run, allow_fail
            if legacy.exists():
                metadata = legacy.stat()
                os.utime(
                    legacy,
                    ns=(
                        metadata.st_atime_ns,
                        metadata.st_mtime_ns + 1_000_000,
                    ),
                )

        with (
            mock.patch.object(
                MODULE,
                "_run_native_command",
                side_effect=touch_legacy,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "macos",
                None,
                dry_run=False,
                enable=True,
            )

        self.assertFalse(legacy.exists())

    def test_absent_legacy_launchd_cleanup_retains_parent_and_absence(self) -> None:
        mutations = (
            ("appearance", "appeared after initial absence"),
            ("parent", "parent chain changed"),
            ("unreadable", "parent descriptor became unreadable"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        for action_index in range(2):
            for mutation, expected_error in mutations:
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = (
                        self.root / f"legacy-absent-{action_index}-{mutation}" / "home"
                    )
                    paths = MODULE.SchedulerPaths(
                        platform="macos",
                        launchd_plist=(
                            case_user_home
                            / "Library"
                            / "LaunchAgents"
                            / f"{MODULE.LAUNCHD_LABEL}.plist"
                        ),
                    )
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    legacy.parent.mkdir(parents=True)
                    parent_identity = (
                        legacy.parent.stat().st_dev,
                        legacy.parent.stat().st_ino,
                    )
                    real_directory_identity = MODULE._directory_identity
                    native_calls = 0
                    force_unreadable = False

                    def mutate_absence(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal force_unreadable, native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(b"new user scheduler config\n")
                            legacy.chmod(0o600)
                        elif mutation == "parent":
                            displaced = legacy.parent.with_name(
                                legacy.parent.name + ".displaced"
                            )
                            legacy.parent.rename(displaced)
                            legacy.parent.mkdir()
                        else:
                            force_unreadable = True

                    def directory_identity(
                        directory_fd: int,
                    ) -> tuple[int, int]:
                        identity = real_directory_identity(directory_fd)
                        if force_unreadable and identity == parent_identity:
                            raise OSError("injected parent read failure")
                        return identity

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_absence,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_directory_identity",
                            side_effect=directory_identity,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE._cleanup_legacy_launchd_schedulers(
                            paths,
                            dry_run=False,
                            disable=True,
                            remove=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    if mutation == "appearance":
                        self.assertEqual(
                            legacy.read_bytes(),
                            b"new user scheduler config\n",
                        )

    def test_uninstall_prebinds_legacy_before_current_launchd_actions(self) -> None:
        cases = (
            ("appearance", "appeared after initial absence"),
            ("replacement", "object identity changed"),
        )
        label = MODULE.LEGACY_LAUNCHD_LABELS[0]
        case_index = 0
        for action_index in range(2):
            for mutation, expected_error in cases:
                case_index += 1
                with self.subTest(action=action_index, mutation=mutation):
                    case_user_home = self.root / f"legacy-current-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "macos",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("macos", case_home)
                    assert paths.launchd_plist is not None
                    legacy = MODULE._legacy_launchd_plist(paths, label)
                    if mutation == "replacement":
                        legacy.write_bytes(b"original legacy config\n")
                        legacy.chmod(0o600)
                    replacement_payload = (
                        f"new user config {action_index} {mutation}\n".encode("utf-8")
                    )
                    native_calls = 0

                    def mutate_legacy_during_current_action(
                        _args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        current_call = native_calls
                        native_calls += 1
                        if current_call != action_index:
                            return
                        if mutation == "appearance":
                            legacy.write_bytes(replacement_payload)
                            legacy.chmod(0o600)
                            return
                        candidate = legacy.with_name(legacy.name + ".replacement")
                        candidate.write_bytes(replacement_payload)
                        candidate.chmod(0o600)
                        os.replace(candidate, legacy)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=mutate_legacy_during_current_action,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            expected_error,
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "macos",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(native_calls, action_index + 1)
                    self.assertEqual(legacy.read_bytes(), replacement_payload)
                    self.assertTrue(paths.launchd_plist.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_revalidates_legacy_after_current_removal(
        self,
    ) -> None:
        for initial_legacy_exists in (False, True):
            with self.subTest(initial_legacy_exists=initial_legacy_exists):
                suffix = "present" if initial_legacy_exists else "absent"
                case_user_home = (
                    self.root / f"legacy-current-removal-{suffix}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            "macos",
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths("macos", case_home)
                assert paths.launchd_plist is not None
                legacy = MODULE._legacy_launchd_plist(
                    paths,
                    MODULE.LEGACY_LAUNCHD_LABELS[0],
                )
                if initial_legacy_exists:
                    legacy.write_bytes(b"original legacy config\n")
                    legacy.chmod(0o600)
                payload = (
                    f"concurrent {suffix} legacy config during "
                    "current removal\n"
                ).encode("utf-8")
                isolate = MODULE._isolate_and_delete_pending_cleanup_file

                def appear_during_current_removal(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    expected: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    isolate(
                        home,
                        path,
                        parent_fd,
                        expected,
                        label=label,
                    )
                    if path != paths.launchd_plist:
                        self.assertEqual(path, legacy)
                        return
                    legacy.write_bytes(payload)
                    legacy.chmod(0o600)

                output = io.StringIO()
                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=appear_during_current_removal,
                    ),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        (
                            "reappeared after conditional removal"
                            if initial_legacy_exists
                            else "appeared after initial absence"
                        ),
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        "macos",
                        dry_run=False,
                        disable=False,
                    )

                self.assertFalse(paths.launchd_plist.exists())
                self.assertEqual(legacy.read_bytes(), payload)
                self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_missing_config_parent_is_idempotent_noop(self) -> None:
        for platform_name in ("macos", "linux"):
            for disable in (False, True):
                with self.subTest(platform=platform_name, disable=disable):
                    case_user_home = (
                        self.root
                        / f"missing-parent-{platform_name}-{disable}"
                        / "home"
                    )
                    case_user_home.mkdir(parents=True)
                    case_home = case_user_home / ".codex"
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "installation_lock",
                            side_effect=AssertionError(
                                "missing-parent no-op acquired the install lock"
                            ),
                        ) as install_lock,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=AssertionError(
                                "missing-parent no-op ran a native command"
                            ),
                        ) as native_command,
                        contextlib.redirect_stdout(output),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=disable,
                        )

                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    self.assertFalse(MODULE._scheduler_config_parent(paths).exists())
                    self.assertFalse(case_home.exists())
                    install_lock.assert_not_called()
                    native_command.assert_not_called()
                    self.assertIn("scheduler already absent", output.getvalue())

    def test_uninstall_missing_parent_race_fails_closed_and_preserves_appearance(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"missing-parent-race-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_service
                )
                assert target is not None
                payload = f"concurrent {platform_name} config\n".encode("utf-8")
                first_component = "Library" if platform_name == "macos" else ".config"
                real_open = MODULE.os.open
                injected = False

                def observe_missing_then_appear(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        parent.mkdir(parents=True)
                        target.write_bytes(payload)
                        target.chmod(0o600)
                        raise FileNotFoundError(first_component)
                    return real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=observe_missing_then_appear,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "missing-parent no-op acquired the install lock"
                        ),
                    ),
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "missing-parent no-op ran a native command"
                        ),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "appeared after a missing observation",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), payload)

    def test_uninstall_config_parent_uncertainty_fails_closed(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation, expected_error in (
                ("file", "is not a directory"),
                ("symlink", "is a symlink"),
                ("unreadable", "is unreadable"),
            ):
                with self.subTest(platform=platform_name, mutation=mutation):
                    case_user_home = (
                        self.root
                        / f"uncertain-parent-{platform_name}-{mutation}"
                        / "home"
                    )
                    case_home = case_user_home / ".codex"
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(platform_name, case_home)
                    parent = MODULE._scheduler_config_parent(paths)
                    parent.parent.mkdir(parents=True)
                    if mutation == "file":
                        parent.write_bytes(b"foreign parent object\n")
                    elif mutation == "symlink":
                        target = parent.with_name(parent.name + ".target")
                        target.mkdir()
                        parent.symlink_to(target, target_is_directory=True)
                    else:
                        parent.mkdir()

                    real_open = MODULE.os.open
                    real_stat = MODULE.os.stat

                    def reject_parent_open(
                        path: os.PathLike[str] | str,
                        flags: int,
                        mode: int = 0o777,
                        *,
                        dir_fd: int | None = None,
                    ) -> int:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and dir_fd is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_open(
                            path,
                            flags,
                            mode,
                            dir_fd=dir_fd,
                        )

                    def reject_parent_read(
                        path: os.PathLike[str] | str,
                        *args: object,
                        **kwargs: object,
                    ) -> os.stat_result:
                        if (
                            mutation == "unreadable"
                            and os.fspath(path) == parent.name
                            and kwargs.get("dir_fd") is not None
                        ):
                            raise PermissionError(parent.name)
                        return real_stat(path, *args, **kwargs)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "open",
                            side_effect=reject_parent_open,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "stat",
                            side_effect=reject_parent_read,
                        ),
                        mock.patch.object(
                            MODULE,
                            "installation_lock",
                            side_effect=AssertionError(
                                "uncertain parent acquired the install lock"
                            ),
                        ) as install_lock,
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=AssertionError(
                                "uncertain parent ran a native command"
                            ),
                        ) as native_command,
                        self.assertRaisesRegex(MODULE.SyncError, expected_error),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                    install_lock.assert_not_called()
                    native_command.assert_not_called()

    def test_uninstall_intermediate_config_parent_symlink_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"intermediate-symlink-{platform_name}" / "home"
                )
                case_user_home.mkdir(parents=True)
                case_home = case_user_home / ".codex"
                if platform_name == "macos":
                    link = case_user_home / "Library"
                    link_target = case_user_home / "foreign-library"
                    config_parent = link_target / "LaunchAgents"
                    config_name = f"{MODULE.LAUNCHD_LABEL}.plist"
                else:
                    config_root = case_user_home / ".config"
                    config_root.mkdir()
                    link = config_root / "systemd"
                    link_target = case_user_home / "foreign-systemd"
                    config_parent = link_target / "user"
                    config_name = f"{MODULE.SYSTEMD_UNIT}.service"
                config_parent.mkdir(parents=True)
                link.symlink_to(link_target, target_is_directory=True)
                foreign_config = config_parent / config_name
                payload = f"foreign {platform_name} scheduler\n".encode("utf-8")
                foreign_config.write_bytes(payload)
                foreign_config.chmod(0o600)

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "intermediate symlink acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "intermediate symlink ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component is a symlink",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertEqual(foreign_config.read_bytes(), payload)

    def test_uninstall_config_parent_component_replacement_fails_closed(
        self,
    ) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = (
                    self.root / f"component-replacement-{platform_name}" / "home"
                )
                case_home = case_user_home / ".codex"
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                parent = MODULE._scheduler_config_parent(paths)
                parent.mkdir(parents=True)
                first_component = "Library" if platform_name == "macos" else ".config"
                original_component = case_user_home / first_component
                displaced_component = original_component.with_name(
                    original_component.name + ".displaced"
                )
                real_open = MODULE.os.open
                injected = False

                def replace_opened_component(
                    path: os.PathLike[str] | str,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal injected
                    file_descriptor = real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )
                    if (
                        not injected
                        and os.fspath(path) == first_component
                        and dir_fd is not None
                    ):
                        injected = True
                        original_component.rename(displaced_component)
                        original_component.mkdir()
                    return file_descriptor

                with (
                    mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ),
                    mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=replace_opened_component,
                    ),
                    mock.patch.object(
                        MODULE,
                        "installation_lock",
                        side_effect=AssertionError(
                            "component replacement acquired the install lock"
                        ),
                    ) as install_lock,
                    mock.patch.object(
                        MODULE,
                        "_run_native_command",
                        side_effect=AssertionError(
                            "component replacement ran a native command"
                        ),
                    ) as native_command,
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        "component identity changed",
                    ),
                ):
                    MODULE.uninstall_scheduler(
                        case_home,
                        platform_name,
                        dry_run=False,
                        disable=True,
                    )

                self.assertTrue(injected)
                install_lock.assert_not_called()
                native_command.assert_not_called()
                self.assertTrue(displaced_component.exists())

    def test_uninstall_binds_original_scheduler_configs_across_native_calls(
        self,
    ) -> None:
        mutations = (
            ("replacement", "object identity changed"),
            ("content", "content changed"),
            ("mode", "access policy changed"),
            ("owner", "access policy changed"),
            ("parent", "parent chain changed"),
            ("missing", "is missing"),
            ("unreadable", "is unreadable"),
        )
        case_index = 0
        for platform_name in ("macos", "linux"):
            targets = ("plist",) if platform_name == "macos" else ("service", "timer")
            action_indexes = range(2) if platform_name == "macos" else range(1)
            for target_kind in targets:
                for action_index in action_indexes:
                    for mutation, expected_error in mutations:
                        case_index += 1
                        with self.subTest(
                            platform=platform_name,
                            target=target_kind,
                            action=action_index,
                            mutation=mutation,
                        ):
                            case_user_home = (
                                self.root / f"uninstall-{case_index}" / "home"
                            )
                            case_home = case_user_home / ".codex"
                            runner = case_home / "bin" / "codex-personal-sync"
                            runner.parent.mkdir(parents=True)
                            runner.write_text(
                                "#!/bin/sh\nexit 0\n",
                                encoding="utf-8",
                            )
                            runner.chmod(0o755)
                            with mock.patch.object(
                                MODULE.Path,
                                "home",
                                return_value=case_user_home,
                            ):
                                with contextlib.redirect_stdout(io.StringIO()):
                                    MODULE.install_scheduler(
                                        case_home,
                                        "owner/public-sync",
                                        17,
                                        platform_name,
                                        None,
                                        dry_run=False,
                                        enable=False,
                                    )
                                paths = MODULE._scheduler_paths(
                                    platform_name,
                                    case_home,
                                )
                            if target_kind == "plist":
                                target = paths.launchd_plist
                            elif target_kind == "service":
                                target = paths.systemd_service
                            else:
                                target = paths.systemd_timer
                            assert target is not None
                            original = target.read_bytes()
                            original_metadata = target.stat()
                            original_identity = (
                                original_metadata.st_dev,
                                original_metadata.st_ino,
                            )
                            original_mode = stat.S_IMODE(original_metadata.st_mode)
                            real_read = MODULE._read_managed_state_bytes
                            real_fstat = MODULE.os.fstat
                            native_calls = 0
                            force_owner = False
                            force_unreadable = False

                            def mutate_during_uninstall(
                                _args: list[str],
                                *,
                                dry_run: bool,
                                allow_fail: bool = False,
                            ) -> None:
                                del dry_run, allow_fail
                                nonlocal force_owner
                                nonlocal force_unreadable
                                nonlocal native_calls
                                current_call = native_calls
                                native_calls += 1
                                if current_call != action_index:
                                    return
                                if mutation == "replacement":
                                    candidate = target.with_name(
                                        target.name + ".replacement"
                                    )
                                    candidate.write_bytes(original)
                                    candidate.chmod(original_mode)
                                    os.replace(candidate, target)
                                elif mutation == "content":
                                    payload = bytearray(original)
                                    payload[len(payload) // 2] ^= 1
                                    target.write_bytes(payload)
                                    target.chmod(original_mode)
                                elif mutation == "mode":
                                    target.chmod(
                                        0o644 if original_mode != 0o644 else 0o600
                                    )
                                elif mutation == "owner":
                                    force_owner = True
                                elif mutation == "parent":
                                    displaced = target.parent.with_name(
                                        target.parent.name + ".displaced"
                                    )
                                    target.parent.rename(displaced)
                                    target.parent.mkdir()
                                elif mutation == "missing":
                                    target.unlink()
                                else:
                                    force_unreadable = True

                            def fstat_with_owner(
                                file_fd: int,
                            ) -> os.stat_result:
                                metadata = real_fstat(file_fd)
                                if (
                                    force_owner
                                    and (metadata.st_dev, metadata.st_ino)
                                    == original_identity
                                ):
                                    fields = list(metadata)
                                    fields[4] = metadata.st_uid + 1
                                    return os.stat_result(fields)
                                return metadata

                            def fail_bound_read(
                                file_fd: int,
                                path: Path,
                                maximum_bytes: int = (MODULE.MAX_MANAGED_STATE_BYTES),
                            ) -> bytes:
                                if force_unreadable and path == target:
                                    raise MODULE.SyncError(
                                        "injected bound read failure"
                                    )
                                return real_read(
                                    file_fd,
                                    path,
                                    maximum_bytes,
                                )

                            output = io.StringIO()
                            with (
                                mock.patch.object(
                                    MODULE.Path,
                                    "home",
                                    return_value=case_user_home,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_run_native_command",
                                    side_effect=mutate_during_uninstall,
                                ),
                                mock.patch.object(
                                    MODULE.os,
                                    "fstat",
                                    side_effect=fstat_with_owner,
                                ),
                                mock.patch.object(
                                    MODULE,
                                    "_read_managed_state_bytes",
                                    side_effect=fail_bound_read,
                                ),
                                contextlib.redirect_stdout(output),
                                self.assertRaisesRegex(
                                    MODULE.SyncError,
                                    expected_error,
                                ),
                            ):
                                MODULE.uninstall_scheduler(
                                    case_home,
                                    platform_name,
                                    dry_run=False,
                                    disable=True,
                                )

                            self.assertEqual(
                                native_calls,
                                action_index + 1,
                            )
                            self.assertNotIn(
                                "removed ",
                                output.getvalue(),
                            )
                            if mutation == "replacement":
                                self.assertEqual(target.read_bytes(), original)

    def test_linux_uninstall_revalidates_unit_absence_around_daemon_reload(
        self,
    ) -> None:
        case_index = 0
        for target_kind in ("service", "timer"):
            for appearance_phase in ("before", "during"):
                case_index += 1
                with self.subTest(
                    target=target_kind,
                    phase=appearance_phase,
                ):
                    case_user_home = self.root / f"reload-absence-{case_index}" / "home"
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                "linux",
                                None,
                                dry_run=False,
                                enable=False,
                            )
                        paths = MODULE._scheduler_paths("linux", case_home)
                    target = (
                        paths.systemd_service
                        if target_kind == "service"
                        else paths.systemd_timer
                    )
                    other = (
                        paths.systemd_timer
                        if target_kind == "service"
                        else paths.systemd_service
                    )
                    assert target is not None
                    assert other is not None
                    payload = (f"reappeared {target_kind} {appearance_phase}\n").encode(
                        "utf-8"
                    )
                    native_calls = 0
                    real_report = MODULE._report_preserved_systemd_drop_ins

                    def appear_before_reload(
                        selected_paths: MODULE.SchedulerPaths,
                    ) -> None:
                        real_report(selected_paths)
                        if appearance_phase == "before":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    def appear_during_reload(
                        args: list[str],
                        *,
                        dry_run: bool,
                        allow_fail: bool = False,
                    ) -> None:
                        del dry_run, allow_fail
                        nonlocal native_calls
                        native_calls += 1
                        if appearance_phase == "during" and args[-1] == "daemon-reload":
                            target.write_bytes(payload)
                            target.chmod(0o600)

                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_report_preserved_systemd_drop_ins",
                            side_effect=appear_before_reload,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_run_native_command",
                            side_effect=appear_during_reload,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            "linux",
                            dry_run=False,
                            disable=True,
                        )

                    self.assertEqual(
                        native_calls,
                        1 if appearance_phase == "before" else 2,
                    )
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertFalse(other.exists())
                    self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_commit_revalidates_shared_parent_group(self) -> None:
        for platform_name in ("macos", "linux"):
            for mutation in ("during-fsync", "during-second-pass"):
                with self.subTest(
                    platform=platform_name,
                    mutation=mutation,
                ):
                    case_user_home = (
                        self.root / f"commit-group-{platform_name}-{mutation}" / "home"
                    )
                    case_home = case_user_home / ".codex"
                    runner = case_home / "bin" / "codex-personal-sync"
                    runner.parent.mkdir(parents=True)
                    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    runner.chmod(0o755)
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            MODULE.install_scheduler(
                                case_home,
                                "owner/public-sync",
                                17,
                                platform_name,
                                None,
                                dry_run=False,
                                enable=False,
                            )
                    with mock.patch.object(
                        MODULE.Path,
                        "home",
                        return_value=case_user_home,
                    ):
                        paths = MODULE._scheduler_paths(
                            platform_name,
                            case_home,
                        )
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    marker = MODULE._scheduler_uninstall_transaction_path(paths)
                    original = target.read_bytes()
                    real_commit = MODULE._commit_scheduler_uninstall_transaction
                    real_fsync = MODULE.os.fsync
                    real_revalidate = MODULE._revalidate_launchd_activation_binding
                    commit_active = False
                    injected = False
                    marker_checks = 0
                    commit_parent_fds: list[int] = []

                    def reappear_target() -> None:
                        nonlocal injected
                        target.write_bytes(original)
                        target.chmod(0o600)
                        injected = True

                    def commit_and_arm(
                        marker_binding: MODULE.SchedulerActivationBinding,
                        *,
                        related_bindings: tuple[
                            MODULE.SchedulerActivationBinding,
                            ...,
                        ],
                    ) -> None:
                        nonlocal commit_active
                        commit_active = True
                        real_commit(
                            marker_binding,
                            related_bindings=related_bindings,
                        )

                    def sync_then_reappear(file_fd: int) -> None:
                        real_fsync(file_fd)
                        if (
                            commit_active
                            and mutation == "during-fsync"
                            and not injected
                        ):
                            reappear_target()

                    def revalidate_and_interleave(
                        binding: MODULE.SchedulerActivationBinding,
                        *,
                        boundary: str,
                    ) -> None:
                        nonlocal marker_checks
                        if commit_active and boundary == (
                            "after parent sync before uninstall transaction commit"
                        ):
                            commit_parent_fds.append(binding.parent_fd)
                            if (
                                mutation == "during-second-pass"
                                and binding.path == marker
                            ):
                                marker_checks += 1
                                if marker_checks == 2:
                                    reappear_target()
                        real_revalidate(binding, boundary=boundary)

                    with (
                        mock.patch.object(
                            MODULE.Path,
                            "home",
                            return_value=case_user_home,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_commit_scheduler_uninstall_transaction",
                            side_effect=commit_and_arm,
                        ),
                        mock.patch.object(
                            MODULE.os,
                            "fsync",
                            side_effect=sync_then_reappear,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_revalidate_launchd_activation_binding",
                            side_effect=revalidate_and_interleave,
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(
                            MODULE.SyncError,
                            "reappeared after conditional removal",
                        ),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=False,
                        )

                    self.assertTrue(injected)
                    self.assertTrue(marker.is_file())
                    self.assertTrue(target.is_file())
                    self.assertTrue(commit_parent_fds)
                    self.assertEqual(len(set(commit_parent_fds)), 1)

    def test_uninstall_native_failures_retain_transaction_and_configs(
        self,
    ) -> None:
        success = subprocess.CompletedProcess(
            ["scheduler-action"],
            0,
            "",
            "",
        )
        failures: tuple[
            tuple[str, str, int, BaseException | subprocess.CompletedProcess[str]],
            ...,
        ] = (
            (
                "macos",
                "timeout",
                0,
                subprocess.TimeoutExpired(["launchctl", "bootout"], 30),
            ),
            (
                "macos",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["launchctl", "bootout"],
                    1,
                    "",
                    "Operation not permitted",
                ),
            ),
            (
                "macos",
                "unknown",
                1,
                subprocess.CompletedProcess(
                    ["launchctl", "disable"],
                    1,
                    "",
                    "Input/output error",
                ),
            ),
            (
                "linux",
                "timeout",
                0,
                subprocess.TimeoutExpired(["systemctl", "disable"], 30),
            ),
            (
                "linux",
                "permission",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Permission denied",
                ),
            ),
            (
                "linux",
                "unknown",
                0,
                subprocess.CompletedProcess(
                    ["systemctl", "disable"],
                    1,
                    "",
                    "Unit operation failed",
                ),
            ),
        )
        for platform_name, failure_kind, failure_call, failure in failures:
            with self.subTest(
                platform=platform_name,
                failure=failure_kind,
            ):
                case_user_home = (
                    self.root / f"native-{platform_name}-{failure_kind}" / "home"
                )
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(
                        platform_name,
                        case_home,
                    )
                    native_results: list[
                        BaseException | subprocess.CompletedProcess[str]
                    ] = [success] * failure_call + [failure]
                    output = io.StringIO()
                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE.subprocess,
                            "run",
                            side_effect=native_results,
                        ),
                        contextlib.redirect_stdout(output),
                        self.assertRaises(MODULE.SyncError),
                    ):
                        MODULE.uninstall_scheduler(
                            case_home,
                            platform_name,
                            dry_run=False,
                            disable=True,
                        )

                config_paths = (
                    (paths.launchd_plist,)
                    if platform_name == "macos"
                    else (paths.systemd_service, paths.systemd_timer)
                )
                self.assertTrue(
                    all(path is not None and path.is_file() for path in config_paths)
                )
                self.assertTrue(
                    MODULE._scheduler_uninstall_transaction_path(paths).is_file()
                )
                self.assertNotIn("removed ", output.getvalue())

    def test_uninstall_accepts_only_precise_absence_evidence(self) -> None:
        accepted = (
            (
                [
                    "launchctl",
                    "bootout",
                    "gui/501",
                    "/tmp/scheduler.plist",
                ],
                "Boot-out failed: 3: No such process",
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Could not find specified service",
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
        )
        for args, stderr in accepted:
            with self.subTest(args=args):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        return_value=completed,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

        rejected = (
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Failed to disable unit: Unit file "
                    f"{MODULE.SYSTEMD_UNIT}.timer does not exist."
                ),
            ),
            (
                ["launchctl", "disable", "gui/501/example"],
                "Permission denied: Could not find specified service",
            ),
            (
                [
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{MODULE.SYSTEMD_UNIT}.timer",
                ],
                (
                    "Permission denied: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
        )
        for args, stderr in rejected:
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    stderr,
                )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda selected: selected,
                    ),
                    mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        return_value=completed,
                    ),
                    self.assertRaisesRegex(
                        MODULE.SyncError,
                        re.escape(stderr),
                    ),
                ):
                    MODULE._run_native_command(
                        args,
                        dry_run=False,
                        allow_fail=MODULE.NATIVE_FAILURE_ALREADY_ABSENT,
                    )

    def test_linux_uninstall_reload_failure_is_reported_and_recoverable(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        paths = MODULE._scheduler_paths("linux", self.home)
        assert paths.systemd_service is not None
        assert paths.systemd_timer is not None
        marker = MODULE._scheduler_uninstall_transaction_path(paths)
        failed_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                0,
                "",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                1,
                "",
                "Permission denied",
            ),
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=failed_results,
            ),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(MODULE.SyncError, "Permission denied"),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )

        self.assertFalse(paths.systemd_service.exists())
        self.assertFalse(paths.systemd_timer.exists())
        self.assertTrue(marker.is_file())
        self.assertNotIn("removed ", output.getvalue())
        with (
            mock.patch.object(
                MODULE,
                "_quarantine_batch_count",
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )
        self.assertEqual(
            report.failure_code,
            "scheduler-uninstall-incomplete",
        )
        uninstall_issues = [
            issue for issue in issues if issue.code == "scheduler-uninstall-incomplete"
        ]
        self.assertEqual(len(uninstall_issues), 1)
        self.assertEqual(uninstall_issues[0].path, marker)
        with self.assertRaisesRegex(
            MODULE.SyncError,
            "incomplete uninstall transaction",
        ):
            MODULE.install_scheduler(
                self.home,
                "owner/public-sync",
                17,
                "linux",
                None,
                dry_run=False,
                enable=False,
            )

        recovered_results = (
            subprocess.CompletedProcess(
                ["systemctl", "disable"],
                1,
                "",
                (
                    "Failed to disable unit: Unit "
                    f"{MODULE.SYSTEMD_UNIT}.timer not loaded."
                ),
            ),
            subprocess.CompletedProcess(
                ["systemctl", "daemon-reload"],
                0,
                "",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=recovered_results,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            MODULE.uninstall_scheduler(
                self.home,
                "linux",
                dry_run=False,
                disable=True,
            )
        self.assertFalse(marker.exists())

    def test_scheduler_daemon_query_classifies_only_explicit_state_evidence(
        self,
    ) -> None:
        cases = (
            ("macos", 0, "", "", "enabled", None),
            (
                "macos",
                113,
                "",
                "Could not find service in domain",
                "disabled",
                "not loaded",
            ),
            (
                "macos",
                1,
                "",
                "Operation not permitted",
                "unavailable",
                "denied",
            ),
            ("linux", 0, "enabled\n", "", "enabled", None),
            (
                "linux",
                0,
                "enabled-runtime\n",
                "",
                "disabled",
                "runtime-only enablement",
            ),
            (
                "linux",
                1,
                "disabled\n",
                "",
                "disabled",
                "state disabled",
            ),
            (
                "linux",
                1,
                "",
                "Failed to connect to bus",
                "unavailable",
                "user bus",
            ),
            (
                "linux",
                1,
                "unexpected\n",
                "",
                "unavailable",
                "no recognized",
            ),
        )
        for (
            platform_name,
            returncode,
            stdout,
            stderr,
            classification,
            reason,
        ) in cases:
            with self.subTest(
                platform=platform_name,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            ):
                completed = subprocess.CompletedProcess(
                    ["scheduler-query"],
                    returncode,
                    stdout,
                    stderr,
                )
                results = [completed]
                if platform_name == "linux":
                    results.append(
                        subprocess.CompletedProcess(
                            ["scheduler-activity-query"],
                            0,
                            "active\n",
                            "",
                        )
                    )
                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        side_effect=results,
                    ),
                ):
                    query = MODULE._scheduler_daemon_enabled(
                        MODULE.SchedulerPaths(platform=platform_name)
                    )
                self.assertEqual(query.classification, classification)
                self.assertEqual(
                    query.enabled,
                    (
                        True
                        if classification == "enabled"
                        else False
                        if classification == "disabled"
                        else None
                    ),
                )
                if reason is not None:
                    self.assertIn(reason, query.reason or "")

    def test_linux_status_requires_enabled_and_active_timer(self) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        query_results = (
            subprocess.CompletedProcess(
                ["systemctl", "is-enabled"],
                0,
                "enabled\n",
                "",
            ),
            subprocess.CompletedProcess(
                ["systemctl", "is-active"],
                3,
                "failed\n",
                "",
            ),
        )
        with (
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                side_effect=query_results,
            ) as run,
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
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
                return_value=0,
            ),
        ):
            report = MODULE.scheduler_report(self.home, "linux")

        self.assertEqual(
            [call.args[0][2] for call in run.call_args_list],
            ["is-enabled", "is-active"],
        )
        self.assertFalse(report.enabled)
        self.assertEqual(
            report.failure_code,
            "scheduler-daemon-disabled",
        )
        assert report.daemon_query is not None
        self.assertEqual(report.daemon_query.classification, "disabled")
        self.assertIn(
            "enabled but not active (state failed)",
            report.daemon_query.reason or "",
        )

    def test_scheduler_report_and_doctor_preserve_runtime_and_daemon_failures(
        self,
    ) -> None:
        self.write_runner()
        self.install_scheduler_quietly(
            "owner/public-sync",
            17,
            "linux",
        )
        runtime_state = {
            "version": 2,
            "last_attempt": "2026-07-24T12:00:00+00:00",
            "last_success": None,
            "success": False,
            "failure_reason": "scheduled sync failed",
            "failure_code": None,
            "release_trees": {},
            "mode": "public",
            "repo": "owner/public-sync",
            "base_repo": "owner/public-sync",
            "owner": MODULE.PUBLIC_OWNER,
        }
        completed = subprocess.CompletedProcess(
            ["systemctl"],
            1,
            "disabled\n",
            "",
        )
        with (
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
                return_value=0,
            ),
            mock.patch.object(
                MODULE,
                "_stable_scheduler_runner_matches",
                return_value=True,
            ),
            mock.patch.object(
                MODULE,
                "_native_scheduler_argv",
                side_effect=lambda args: args,
            ),
            mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=completed,
            ),
            mock.patch.object(
                MODULE,
                "audit_active_skills",
                return_value=[],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report = MODULE.scheduler_report(self.home, "linux")
            _doctor_report, issues = MODULE.doctor(
                self.home,
                "linux",
                json_output=False,
            )

        self.assertEqual(report.failure_reason, "scheduled sync failed")
        self.assertIsNone(report.failure_code)
        self.assertFalse(report.enabled)
        assert report.daemon_query is not None
        self.assertEqual(report.daemon_query.classification, "disabled")
        self.assertIn(
            (None, "scheduled sync failed"),
            report.failures,
        )
        self.assertIn(
            (
                "scheduler-daemon-disabled",
                "systemd reports scheduler unit state disabled",
            ),
            report.failures,
        )
        issue_codes = {issue.code for issue in issues}
        self.assertIn("scheduler-failure", issue_codes)
        self.assertIn("scheduler-daemon-disabled", issue_codes)

    def test_scheduler_status_binds_config_across_native_query(self) -> None:
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                case_user_home = self.root / f"status-{platform_name}" / "home"
                case_home = case_user_home / ".codex"
                runner = case_home / "bin" / "codex-personal-sync"
                runner.parent.mkdir(parents=True)
                runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                runner.chmod(0o755)
                with mock.patch.object(
                    MODULE.Path,
                    "home",
                    return_value=case_user_home,
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        MODULE.install_scheduler(
                            case_home,
                            "owner/public-sync",
                            17,
                            platform_name,
                            None,
                            dry_run=False,
                            enable=False,
                        )
                    paths = MODULE._scheduler_paths(platform_name, case_home)
                    target = (
                        paths.launchd_plist
                        if platform_name == "macos"
                        else paths.systemd_service
                    )
                    assert target is not None
                    original = target.read_text(encoding="utf-8")

                    def mutate_during_status(
                        args: list[str],
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[str]:
                        target.write_text(
                            original.replace(
                                "owner/public-sync",
                                "owner/changed-sync",
                            ),
                            encoding="utf-8",
                        )
                        target.chmod(0o600)
                        return subprocess.CompletedProcess(
                            args,
                            0,
                            "enabled\n",
                            "",
                        )

                    with (
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        ),
                        mock.patch.object(
                            MODULE.subprocess,
                            "run",
                            side_effect=mutate_during_status,
                        ),
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        ),
                    ):
                        report = MODULE.scheduler_report(
                            case_home,
                            platform_name,
                        )

                self.assertEqual(report.failure_code, "scheduler-config-drift")
                self.assertIsNone(report.enabled)
                self.assertIn("content changed", report.failure_reason or "")
                assert report.daemon_query is not None
                self.assertEqual(
                    report.daemon_query.classification,
                    "unavailable",
                )
                self.assertIn(
                    "scheduler-config-drift",
                    {code for code, _reason in report.failures},
                )
                self.assertIn(
                    "scheduler-daemon-unavailable",
                    {code for code, _reason in report.failures},
                )

    def test_scheduler_status_allows_mtime_churn_and_reports_unavailable(
        self,
    ) -> None:
        self.write_runner()
        for platform_name in ("macos", "linux"):
            with self.subTest(platform=platform_name):
                self.install_scheduler_quietly(
                    "owner/public-sync",
                    17,
                    platform_name,
                )
                paths = MODULE._scheduler_paths(platform_name, self.home)
                target = (
                    paths.launchd_plist
                    if platform_name == "macos"
                    else paths.systemd_timer
                )
                assert target is not None

                def touch_during_status(
                    args: list[str],
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    metadata = target.stat()
                    os.utime(
                        target,
                        ns=(
                            metadata.st_atime_ns,
                            metadata.st_mtime_ns + 1_000_000,
                        ),
                    )
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        (
                            "active\n"
                            if len(args) > 2 and args[2] == "is-active"
                            else "enabled\n"
                        ),
                        "",
                    )

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_native_scheduler_argv",
                            side_effect=lambda args: args,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_stable_scheduler_runner_matches",
                            return_value=True,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_current_releases_for_scheduler",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_scheduler_release_integrity_issues",
                            return_value=(),
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE,
                            "_quarantine_batch_count",
                            return_value=0,
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            MODULE.subprocess,
                            "run",
                            side_effect=touch_during_status,
                        )
                    )
                    healthy = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertTrue(healthy.enabled)
                self.assertIsNone(healthy.failure_code)

                with (
                    mock.patch.object(
                        MODULE,
                        "_native_scheduler_argv",
                        side_effect=lambda args: args,
                    ),
                    mock.patch.object(
                        MODULE,
                        "_stable_scheduler_runner_matches",
                        return_value=True,
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
                        return_value=0,
                    ),
                    mock.patch.object(
                        MODULE.subprocess,
                        "run",
                        side_effect=OSError("status unavailable"),
                    ),
                ):
                    unavailable = MODULE.scheduler_report(
                        self.home,
                        platform_name,
                    )
                self.assertIsNone(unavailable.enabled)
                self.assertEqual(
                    unavailable.failure_code,
                    "scheduler-daemon-unavailable",
                )
                assert unavailable.daemon_query is not None
                self.assertEqual(
                    unavailable.daemon_query.classification,
                    "unavailable",
                )

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

    def test_scheduler_cleanup_removes_recovery_before_displaced_preimage(
        self,
    ) -> None:
        config_path = (
            self.user_home
            / "Library"
            / "LaunchAgents"
            / "com.openai.codex-personal-sync.plist"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        replacement = b"replacement scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        cleanup_labels: list[str] = []
        real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file

        def observe_cleanup(
            home: Path,
            path: Path,
            parent_fd: int,
            snapshot: MODULE.ManagedStateFileSnapshot,
            *,
            label: str,
        ) -> None:
            cleanup_labels.append(label)
            if "recovery evidence" in label:
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)
            real_cleanup(
                home,
                path,
                parent_fd,
                snapshot,
                label=label,
            )

        with mock.patch.object(
            MODULE,
            "_isolate_and_delete_pending_cleanup_file",
            side_effect=observe_cleanup,
        ):
            installed = MODULE._atomic_write_scheduler_config(
                config_path,
                replacement,
                expected_snapshot=expected,
            )

        self.assertEqual(installed.payload, replacement)
        self.assertEqual(config_path.read_bytes(), replacement)
        scheduler_cleanup_labels = [
            label for label in cleanup_labels if label.startswith("scheduler config ")
        ]
        self.assertEqual(
            scheduler_cleanup_labels,
            [
                f"scheduler config recovery evidence {config_path}",
                f"scheduler config backup {config_path}",
            ],
        )

    def test_scheduler_recovery_cleanup_races_preserve_displaced_preimage(
        self,
    ) -> None:
        for race in ("unlink", "replace"):
            with self.subTest(race=race):
                config_path = self.user_home / race / "codex-personal-sync.service"
                config_path.parent.mkdir(parents=True)
                original = f"original:{race}\n".encode()
                replacement = f"replacement:{race}\n".encode()
                config_path.write_bytes(original)
                config_path.chmod(0o600)
                expected = MODULE._scheduler_config_snapshot(config_path)
                real_cleanup = MODULE._isolate_and_delete_pending_cleanup_file
                injected = False

                def race_recovery_cleanup(
                    home: Path,
                    path: Path,
                    parent_fd: int,
                    snapshot: MODULE.ManagedStateFileSnapshot,
                    *,
                    label: str,
                ) -> None:
                    nonlocal injected
                    if "recovery evidence" in label and not injected:
                        injected = True
                        path.unlink()
                        if race == "replace":
                            # Preserve content and access while replacing the
                            # named object identity.
                            path.write_bytes(original)
                            path.chmod(0o600)
                    real_cleanup(
                        home,
                        path,
                        parent_fd,
                        snapshot,
                        label=label,
                    )

                with (
                    mock.patch.object(
                        MODULE,
                        "_isolate_and_delete_pending_cleanup_file",
                        side_effect=race_recovery_cleanup,
                    ),
                    self.assertRaises(MODULE.SyncError),
                ):
                    MODULE._atomic_write_scheduler_config(
                        config_path,
                        replacement,
                        expected_snapshot=expected,
                    )

                self.assertTrue(injected)
                self.assertEqual(config_path.read_bytes(), replacement)
                displaced = [
                    candidate
                    for candidate in config_path.parent.glob(
                        f".{config_path.name}.personal-sync-write-*"
                    )
                    if not candidate.name.endswith(".original")
                ]
                self.assertEqual(len(displaced), 1)
                self.assertEqual(displaced[0].read_bytes(), original)

    def test_scheduler_recovery_link_survives_parent_fsync_failure(self) -> None:
        config_path = (
            self.user_home
            / ".config"
            / "systemd"
            / "user"
            / "codex-personal-sync.service"
        )
        config_path.parent.mkdir(parents=True)
        original = b"original scheduler config\n"
        config_path.write_bytes(original)
        config_path.chmod(0o600)
        expected = MODULE._scheduler_config_snapshot(config_path)
        real_fsync = MODULE.os.fsync
        parent_fsyncs = 0

        def fail_recovery_parent_fsync(file_fd: int) -> None:
            nonlocal parent_fsyncs
            if stat.S_ISDIR(os.fstat(file_fd).st_mode):
                parent_fsyncs += 1
                if parent_fsyncs == 2:
                    raise OSError("injected recovery parent fsync failure")
            real_fsync(file_fd)

        with (
            mock.patch.object(
                MODULE.os,
                "fsync",
                side_effect=fail_recovery_parent_fsync,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "failed to publish scheduler config",
            ),
        ):
            MODULE._atomic_write_scheduler_config(
                config_path,
                b"replacement scheduler config\n",
                expected_snapshot=expected,
            )

        recovery_paths = list(
            config_path.parent.glob(
                f".{config_path.name}.personal-sync-write-*.original"
            )
        )
        self.assertEqual(len(recovery_paths), 1)
        self.assertEqual(recovery_paths[0].read_bytes(), original)
        self.assertEqual(config_path.read_bytes(), original)

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
                    "_audit_scheduler_config",
                    return_value=MODULE.SchedulerConfigAudit(
                        config=config,
                        snapshots=(),
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "_retain_scheduler_config_audit_bindings",
                    return_value=contextlib.nullcontext(()),
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
                "_audit_scheduler_config",
                return_value=MODULE.SchedulerConfigAudit(
                    config=config,
                    snapshots=(),
                ),
            ),
            mock.patch.object(
                MODULE,
                "_retain_scheduler_config_audit_bindings",
                return_value=contextlib.nullcontext(()),
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
