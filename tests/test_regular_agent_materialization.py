from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_regular_agent_materialization",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40
ROLE_TARGET = PurePosixPath("agents/reviewer.toml")


def write_release(
    root: Path,
    *,
    owner: str = MODULE.PUBLIC_OWNER,
    role_payload: str | None = None,
    target: str = "agents/reviewer.toml",
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    links: list[dict[str, object]] = []
    if role_payload is not None:
        source = root / "personal_codex" / "agents" / "reviewer.toml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(role_payload, encoding="utf-8")
        entry: dict[str, object] = {
            "source": "personal_codex/agents/reviewer.toml",
            "target": target,
            "kind": "file",
            "owner": owner,
        }
        if override:
            entry["override"] = True
        links.append(entry)
    else:
        source = root / "personal_codex" / "skills" / "base" / "SKILL.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# Base\n", encoding="utf-8")
        links.append(
            {
                "source": "personal_codex/skills/base",
                "target": "skills/base",
                "kind": "skill",
                "owner": owner,
            }
        )
    payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": links,
    }
    if base_sha is not None:
        payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


def status_is_unhealthy(home: Path) -> bool:
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            return not MODULE.status(home)
        except MODULE.SyncError:
            return True


class PublicRegularAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        self.payload = 'name = "reviewer"\n'
        write_release(self.release, role_payload=self.payload)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self) -> Path:
        install(self.release, self.home, SHA_A)
        return self.home / ROLE_TARGET

    def test_public_install_and_status_accept_exact_independent_file(self) -> None:
        target = self._install()

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), self.payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(MODULE.status(self.home))

    def test_status_fails_closed_for_missing_type_content_mode_and_nlink_drift(
        self,
    ) -> None:
        mutations = ("missing", "type", "content", "mode", "nlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                case_home = self.root / f"home-{mutation}"
                install(self.release, case_home, SHA_A)
                target = case_home / ROLE_TARGET
                if mutation == "missing":
                    target.unlink()
                elif mutation == "type":
                    target.unlink()
                    target.symlink_to("../foreign/reviewer.toml")
                elif mutation == "content":
                    target.write_text("modified = true\n", encoding="utf-8")
                elif mutation == "mode":
                    target.chmod(0o644)
                else:
                    os.link(target, target.with_name("reviewer-copy.toml"))

                self.assertTrue(status_is_unhealthy(case_home))

    def test_public_manifest_transition_removes_regular_role_and_ledger_claim(
        self,
    ) -> None:
        target = self._install()
        next_release = self.root / "next-release"
        write_release(next_release)

        install(next_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(target))
        state = MODULE._load_managed_state(self.home)
        self.assertNotIn(ROLE_TARGET, state.links)


class PrivateRegularAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install_public_base(self, *, role_payload: str | None = None) -> None:
        public = self.root / "public"
        write_release(public, role_payload=role_payload)
        install(public, self.home, SHA_A)

    def _install_private(
        self,
        *,
        role_payload: str = 'provider = "private"\n',
        override: bool = False,
    ) -> Path:
        private = self.root / "private"
        write_release(
            private,
            owner="private",
            role_payload=role_payload,
            override=override,
            base_sha=SHA_A,
        )
        install(private, self.home, SHA_B)
        return self.home / ROLE_TARGET

    def test_private_overlay_verify_accepts_healthy_regular_role(self) -> None:
        self._install_public_base()
        target = self._install_private()

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.verify_overlay(self.home, "private")

    def test_private_overlay_verify_rejects_missing_ledger_claim(self) -> None:
        self._install_public_base()
        self._install_private()
        state = MODULE._load_managed_state(self.home)
        state.links.pop(ROLE_TARGET)
        MODULE._write_managed_state(self.home, state)

        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "overlay verification failed",
            ),
        ):
            MODULE.verify_overlay(self.home, "private")

    def test_uninstall_removes_private_only_regular_role(self) -> None:
        self._install_public_base()
        target = self._install_private()

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(os.path.lexists(target))
        self.assertNotIn(ROLE_TARGET, MODULE._load_managed_state(self.home).links)

    def test_uninstall_private_override_restores_public_regular_role(self) -> None:
        public_payload = 'provider = "public"\n'
        self._install_public_base(role_payload=public_payload)
        target = self._install_private(override=True)
        self.assertEqual(target.read_text(encoding="utf-8"), 'provider = "private"\n')

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), public_payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)
        record = MODULE._load_managed_state(self.home).links[ROLE_TARGET]
        self.assertEqual(record.owner, MODULE.PUBLIC_OWNER)


class RegularAgentPendingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        write_release(self.release, role_payload='name = "reviewer"\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_recovered_install(self) -> None:
        target = self.home / ROLE_TARGET
        install(self.release, self.home, SHA_A)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_precommit_crash_recovery_retries_regular_publication(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_precommit_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("injected precommit pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_precommit_pointer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        ticket = MODULE._read_pending_cleanup_ticket(
            self.home,
            MODULE._pending_cleanup_ticket_path(
                self.home,
                batch.batch_root.name,
            ),
        )
        self.assertIsNotNone(ticket)
        assert ticket is not None
        self.assertEqual(ticket.version, 4)
        self.assertEqual(ticket.phase, "before")
        self.assertEqual(ticket.marker_path, MODULE.PENDING_STATE_ROLLBACK_MARKER)
        self.assertEqual(ticket.terminal_regular_targets, ())
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
        self.assertTrue(batch.batch_root.is_dir())
        self._assert_recovered_install()

    def test_post_clear_cleanup_failure_is_retried_from_durable_ticket(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(any(ticket_root.glob("*.json")))
        self._assert_recovered_install()

    def test_regular_update_rollback_restores_old_file_and_final_link_count(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        real_clear = MODULE._clear_pending_link_pointer

        def retain_precommit_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "before":
                raise MODULE.SyncError("injected precommit pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_precommit_pointer,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, self.home, SHA_B)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertGreater(target.stat().st_nlink, 1)

        install(self.release, self.home, SHA_A)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_committed_crash_recovery_finalizes_regular_publication(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self._assert_recovered_install()

    def test_committed_recovery_tolerates_non_access_bearing_gid_churn(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)

        self._assert_recovered_install()
        self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_terminal_ticket_validates_complete_regular_target_group(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        secondary_source = (
            self.release / "personal_codex" / "agents" / "security-reviewer.toml"
        )
        secondary_source.write_text('name = "security-reviewer"\n', encoding="utf-8")
        manifest_path = self.release / MODULE.MANIFEST_RELATIVE_PATH
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["links"].append(
            {
                "source": "personal_codex/agents/security-reviewer.toml",
                "target": secondary_target.as_posix(),
                "kind": "file",
                "owner": MODULE.PUBLIC_OWNER,
            }
        )
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        real_verify = MODULE._verify_final_regular_targets
        captured: list[MODULE.PendingBatchCleanupTicket] = []

        def tamper_second_then_verify(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            captured.append(ticket)
            target = home / Path(*secondary_target.parts)
            target.write_text("tampered = true\n", encoding="utf-8")
            target.chmod(0o600)
            real_verify(home, ticket)

        with (
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=tamper_second_then_verify,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            tuple(
                expectation.target
                for expectation in captured[0].terminal_regular_targets
            ),
            (ROLE_TARGET, secondary_target),
        )
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertEqual(len(list(ticket_root.glob("*.json"))), 1)
        self.assertEqual(len(list(ticket_root.glob("*.empty-proof"))), 1)


class PendingMetadataCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projected_planned_snapshot_covers_every_v6_field(self) -> None:
        target = self.home / ROLE_TARGET
        absent = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            ancestor_identity=(1, 2),
        )
        projected_absent = MODULE._projected_pending_snapshot_payload(
            self.home,
            target,
            absent,
        )
        actual_absent = MODULE._planned_snapshot_payload(absent)
        self.assertEqual(set(projected_absent), set(actual_absent))
        for field in (
            "regular_sha256",
            "regular_size",
            "regular_mode",
            "regular_uid",
            "regular_gid",
            "regular_link_count",
        ):
            self.assertIsNone(projected_absent[field])

        regular = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            link_identity=(3, 4),
            ancestor_identity=(1, 2),
            regular_sha256="a" * 64,
            regular_size=99,
            regular_mode=0o600,
            regular_uid=501,
            regular_gid=20,
            regular_link_count=9,
        )
        projected_regular = MODULE._projected_pending_snapshot_payload(
            self.home,
            target,
            regular,
        )
        actual_regular = MODULE._planned_snapshot_payload(regular)
        self.assertEqual(set(projected_regular), set(actual_regular))
        self.assertGreaterEqual(
            MODULE._projected_json_size(projected_regular, trailing_newline=False),
            MODULE._projected_json_size(actual_regular, trailing_newline=False),
        )

    def test_access_bearing_regular_snapshot_protects_gid(self) -> None:
        expected = MODULE.RegularFileSnapshot(
            parent_identity=(1, 2),
            file_identity=(3, 4),
            sha256="a" * 64,
            size=10,
            mode=0o640,
            uid=501,
            gid=20,
            link_count=1,
        )
        actual = MODULE.RegularFileSnapshot(
            parent_identity=expected.parent_identity,
            file_identity=expected.file_identity,
            sha256=expected.sha256,
            size=expected.size,
            mode=expected.mode,
            uid=expected.uid,
            gid=80,
            link_count=expected.link_count,
        )

        self.assertFalse(
            MODULE._regular_snapshot_matches(
                actual,
                expected.parent_identity,
                expected,
                expected_link_count=1,
                protect_gid=True,
            )
        )
        self.assertTrue(
            MODULE._regular_snapshot_matches(
                actual,
                expected.parent_identity,
                expected,
                expected_link_count=1,
                protect_gid=False,
            )
        )

    def _retain_committed_batch(self, release: Path) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_committed_pointer(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            if phase == "after":
                raise MODULE.SyncError("injected committed pointer retention")
            real_clear(home, batch, phase=phase)

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=retain_committed_pointer,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
        ):
            install(release, self.home, SHA_A)
        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        return batch

    def _rewrite_linked_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        mutate,
    ) -> None:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        mutate(payload)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_v5_symlink_pending_metadata_remains_readable(self) -> None:
        release = self.root / "symlink-release"
        write_release(release)
        batch = self._retain_committed_batch(release)

        def downgrade(payload: dict[str, object]) -> None:
            payload["version"] = 5
            records = payload["records"]
            assert isinstance(records, list)
            for raw_record in records:
                assert isinstance(raw_record, dict)
                for field in (
                    "materialization",
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    raw_record.pop(field)
                planned = raw_record["planned_before"]
                assert isinstance(planned, dict)
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    planned.pop(field)

        self._rewrite_linked_metadata(batch, downgrade)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(all(record.materialization == "symlink" for record in parsed.records))

    def test_v6_pending_metadata_rejects_unknown_closed_field(self) -> None:
        release = self.root / "regular-release"
        write_release(release, role_payload='name = "reviewer"\n')
        batch = self._retain_committed_batch(release)

        def add_unknown_field(payload: dict[str, object]) -> None:
            records = payload["records"]
            assert isinstance(records, list)
            record = records[-1]
            assert isinstance(record, dict)
            record["unexpected_regular_field"] = True

        self._rewrite_linked_metadata(batch, add_unknown_field)

        with self.assertRaisesRegex(MODULE.SyncError, "record .* is invalid"):
            MODULE._load_pending_link_batch(self.home)


if __name__ == "__main__":
    unittest.main()
