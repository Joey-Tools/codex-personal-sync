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
from types import SimpleNamespace
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


def append_regular_link(
    root: Path,
    *,
    target: str,
    source: str = "personal_codex/agents/reviewer.toml",
    payload: str | None = None,
) -> None:
    if payload is not None:
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(payload, encoding="utf-8")
    manifest_path = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["links"].append(
        {
            "source": source,
            "target": target,
            "kind": "file",
            "owner": manifest["owner"],
        }
    )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


def legacy_v6_writer_metadata_payload(
    batch: MODULE.PendingLinkBatch,
) -> tuple[dict[str, object], int | None]:
    metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["version"] == 8
    payload["version"] = 6
    payload.pop("terminal_regular_before")
    payload.pop("terminal_regular_after")
    legacy_gid: int | None = None
    records = payload["records"]
    assert isinstance(records, list)
    for raw_record in records:
        assert isinstance(raw_record, dict)
        raw_record.pop("publication_cleanup")
        if (
            raw_record["materialization"] == "regular"
            and raw_record["action"]
            in {"create", "replace", "quarantine-replace"}
        ):
            stage = raw_record["stage"]
            evidence = raw_record["evidence"]
            assert isinstance(stage, str)
            assert isinstance(evidence, str)
            stage_metadata = os.stat(batch.batch_root / stage)
            evidence_metadata = os.stat(batch.batch_root / evidence)
            assert (stage_metadata.st_dev, stage_metadata.st_ino) == tuple(
                raw_record["stage_identity"]
            )
            assert (evidence_metadata.st_dev, evidence_metadata.st_ino) == tuple(
                raw_record["evidence_identity"]
            )
            assert (stage_metadata.st_dev, stage_metadata.st_ino) == (
                evidence_metadata.st_dev,
                evidence_metadata.st_ino,
            )
            assert stat.S_IMODE(stage_metadata.st_mode) == raw_record["regular_mode"]
            assert stage_metadata.st_uid == raw_record["regular_uid"]
            assert stage_metadata.st_nlink in {
                raw_record["regular_link_count"],
                raw_record["regular_link_count"] + 1,
            }
            assert evidence_metadata.st_nlink == stage_metadata.st_nlink
            assert raw_record["regular_gid"] is None
            raw_record["regular_gid"] = stage_metadata.st_gid
            legacy_gid = stage_metadata.st_gid
        elif raw_record["materialization"] == "regular":
            assert raw_record["action"] in {"remove", "quarantine-remove"}
            assert raw_record["regular_gid"] is None
            planned = raw_record["planned_before"]
            assert isinstance(planned, dict)
            planned_gid = planned["regular_gid"]
            assert isinstance(planned_gid, int)
            assert not isinstance(planned_gid, bool)
            assert planned_gid >= 0
    return payload, legacy_gid


def write_pending_metadata_payload(
    batch: MODULE.PendingLinkBatch,
    payload: dict[str, object],
) -> None:
    metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
    metadata_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


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

    def test_desired_entry_verification_rejects_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        target.unlink()
        target.symlink_to(MODULE._desired_link_target(self.home, entry))

        with mock.patch.object(
            MODULE,
            "_read_optional_symlink_target_beneath",
            wraps=MODULE._read_optional_symlink_target_beneath,
        ) as read_symlink, self.assertRaisesRegex(
            MODULE.SyncError,
            "managed link verification failed",
        ):
            MODULE._verify_desired_entries(self.home, [entry])

        read_symlink.assert_not_called()

    def test_committed_state_rejects_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        target.unlink()
        target.symlink_to(MODULE._desired_link_target(self.home, entry))

        with mock.patch.object(
            MODULE,
            "_read_optional_symlink_target_beneath",
            wraps=MODULE._read_optional_symlink_target_beneath,
        ) as read_symlink, self.assertRaisesRegex(
            MODULE.SyncError,
            "mandatory desired regular file drifted",
        ):
            MODULE._committed_state(
                self.home,
                [entry],
                {MODULE.PUBLIC_OWNER: SHA_A},
            )

        read_symlink.assert_not_called()

    def test_exact_noop_rejects_raced_exact_target_symlink_for_regular_file(
        self,
    ) -> None:
        target = self._install()
        entry = MODULE.load_manifest(self.release)[0]
        real_verify = MODULE._verify_desired_entries
        raced = False

        def replace_before_noop_verification(
            home: Path,
            desired_entries: list[MODULE.LinkEntry],
            *,
            pending_batch: MODULE.PendingLinkBatch | None = None,
        ) -> None:
            nonlocal raced
            if not raced:
                target.unlink()
                target.symlink_to(MODULE._desired_link_target(home, entry))
                raced = True
            real_verify(home, desired_entries, pending_batch=pending_batch)

        with (
            mock.patch.object(
                MODULE,
                "_verify_desired_entries",
                side_effect=replace_before_noop_verification,
            ),
            mock.patch.object(MODULE, "_stage_pending_link_batch") as stage,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed link verification failed",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(raced)
        stage.assert_not_called()
        self.assertTrue(target.is_symlink())

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

    def test_portable_agent_target_aliases_materialize_regular_files(self) -> None:
        aliases = ("Agents/reviewer.toml", "agents/REVIEWER.TOML")
        for index, alias in enumerate(aliases):
            with self.subTest(alias=alias):
                release = self.root / f"alias-release-{index}"
                home = self.root / f"alias-home-{index}"
                write_release(release, role_payload=self.payload, target=alias)

                install(release, home, SHA_A)

                target = home / Path(alias)
                self.assertTrue(target.is_file())
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_text(encoding="utf-8"), self.payload)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class RegularAgentMaterializationBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.release = self.root / "release"
        self.payload = 'name = "shared"\n'
        write_release(self.release, role_payload=self.payload)
        append_regular_link(
            self.release,
            target="agents/security-reviewer.toml",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan_initial_actions(self) -> list[MODULE.ReconcileAction]:
        entries = MODULE.load_manifest(self.release)
        incoming_sources = {
            (entry.owner, entry.target): self.release / Path(*entry.source.parts)
            for entry in entries
            if MODULE._entry_materializes_regular_file(entry)
        }
        return MODULE._plan_reconciliation(
            self.home,
            entries,
            [],
            [],
            MODULE.ManagedState(owners={}, links={}),
            allow_cross_owner=False,
            owner_shas={MODULE.PUBLIC_OWNER: SHA_A},
            incoming_regular_sources=incoming_sources,
        )

    def test_budget_charges_each_producing_target_even_for_shared_source(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                len(self.payload.encode("utf-8")),
            ),
            mock.patch.object(
                MODULE,
                "_create_regular_file_beneath",
                wraps=MODULE._create_regular_file_beneath,
            ) as create_regular,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(create_regular.call_count, 0)
        self.assertFalse(os.path.lexists(self.home / ROLE_TARGET))
        self.assertFalse(
            os.path.lexists(self.home / "agents" / "security-reviewer.toml")
        )

        with mock.patch.object(
            MODULE,
            "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
            2 * len(self.payload.encode("utf-8")),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual((self.home / ROLE_TARGET).read_text(), self.payload)
        self.assertEqual(
            (self.home / "agents" / "security-reviewer.toml").read_text(),
            self.payload,
        )

    def test_dry_run_uses_the_same_materialization_capacity_gate(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                len(self.payload.encode("utf-8")),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            MODULE.install_release_tree(
                self.release,
                self.home,
                SHA_A,
                dry_run=True,
            )

    def test_absent_create_rejects_before_reading_regular_payload(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                0,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular.*materialization.*(limit|budget)|materialization.*bytes",
            ),
        ):
            self._plan_initial_actions()

        self.assertEqual(read_payload.call_count, 0)

    def test_shared_source_is_read_once_under_independent_evidence_budget(self) -> None:
        payload_size = len(self.payload.encode("utf-8"))
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                2 * payload_size,
            ),
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                payload_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
        ):
            actions = self._plan_initial_actions()

        self.assertEqual(len(actions), 2)
        self.assertEqual(read_payload.call_count, 1)

    def test_distinct_source_evidence_reads_have_an_aggregate_budget(self) -> None:
        second_source = "personal_codex/agents/security-reviewer.toml"
        append_regular_link(
            self.release,
            target="agents/security-reviewer-alt.toml",
            source=second_source,
            payload=self.payload,
        )
        payload_size = len(self.payload.encode("utf-8"))
        with (
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
                3 * payload_size,
            ),
            mock.patch.object(
                MODULE,
                "MAX_PENDING_REGULAR_EVIDENCE_READ_BYTES",
                payload_size,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_source_payload",
                wraps=MODULE._read_regular_source_payload,
            ) as read_payload,
            self.assertRaisesRegex(
                MODULE.SyncError,
                "regular-file evidence reads.*aggregate.*size limit",
            ),
        ):
            self._plan_initial_actions()

        self.assertEqual(read_payload.call_count, 1)

    def test_exact_noop_does_not_consume_materialization_budget(self) -> None:
        install(self.release, self.home, SHA_A)

        with mock.patch.object(
            MODULE,
            "MAX_PENDING_REGULAR_MATERIALIZATION_BYTES",
            0,
        ):
            install(self.release, self.home, SHA_A)


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

    def _interrupt_regular_publication_cleanup(
        self,
        release: Path,
        sha: str,
    ) -> MODULE.PendingLinkBatch:
        real_unlink = os.unlink

        def fail_after_active_rename(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> None:
            name = os.fsdecode(path)
            if name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX):
                raise MODULE.SyncError("injected active publication cleanup crash")
            real_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(MODULE.os, "unlink", side_effect=fail_after_active_rename),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(release, self.home, sha)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        self.assertEqual(batch.metadata_version, 8)
        return batch

    def _assert_active_publication_journal(
        self,
        batch: MODULE.PendingLinkBatch,
    ) -> tuple[MODULE.PendingLinkRecord, Path]:
        record = next(
            candidate
            for candidate in batch.records
            if candidate.is_regular()
            and candidate.action in {"create", "replace", "quarantine-replace"}
        )
        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        journal_snapshot, active_name, _phase, _expected = journal
        self.assertEqual(journal_snapshot.mode, 0o600)
        active = (self.home / Path(*record.target.parts)).with_name(active_name)
        evidence = batch.batch_root / Path(*record.evidence.parts)
        self.assertFalse(os.path.lexists(self.home / Path(*record.target.parts)))
        self.assertEqual(
            (active.stat().st_dev, active.stat().st_ino),
            (evidence.stat().st_dev, evidence.stat().st_ino),
        )
        return record, active

    def _interrupt_uncommitted_regular_publication(
        self,
        release: Path,
        sha: str,
    ) -> MODULE.PendingLinkBatch:
        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                side_effect=MODULE.SyncError("injected hard rollback crash"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(release, self.home, sha)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        return batch

    def _downgrade_pending_regular_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
    ) -> MODULE.PendingLinkBatch:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        if version == 6:
            payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
            assert _legacy_gid is not None
        else:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["version"] = version
        records = payload["records"]
        assert isinstance(records, list)
        for raw_record in records:
            assert isinstance(raw_record, dict)
            raw_record.pop("publication_cleanup", None)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, version)
        return parsed

    def _isolate_legacy_regular_publication(
        self,
        batch: MODULE.PendingLinkBatch,
        record: MODULE.PendingLinkRecord,
        target: Path,
    ) -> Path:
        parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            metadata = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            planned = MODULE._pending_cleanup_entry_plan(metadata)
            active_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            MODULE._rename_noreplace_at(
                parent_fd,
                target.name,
                parent_fd,
                active_name,
            )
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)
        active = target.with_name(active_name)
        self.assertFalse(os.path.lexists(target))
        self.assertTrue(active.is_file())
        return active

    def test_create_rollback_recovers_durable_active_publication_cleanup(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        _record, active = self._assert_active_publication_journal(batch)
        self.assertGreater(active.stat().st_nlink, 2)

        self._assert_recovered_install()
        self.assertFalse(os.path.lexists(active))
        cleanup_path = MODULE._pending_regular_publication_cleanup_path(
            batch,
            next(record for record in batch.records if record.is_regular()),
            "produced",
        )
        self.assertTrue(cleanup_path.is_file())

    def test_replace_rollback_recovers_durable_active_publication_cleanup(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')

        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        _record, active = self._assert_active_publication_journal(batch)

        install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(active))
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_recovery_accepts_receipt_after_preimage_restoration(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record = next(candidate for candidate in batch.records if candidate.is_regular())
        _record, active = self._assert_active_publication_journal(batch)
        real_restore = MODULE._restore_pending_record_before
        restored = False

        def fail_after_preimage_restoration(
            home: Path,
            pending_batch: MODULE.PendingLinkBatch,
            pending_record: MODULE.PendingLinkRecord,
            before_evidence: MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ) -> None:
            nonlocal restored
            real_restore(home, pending_batch, pending_record, before_evidence)
            restored = True
            raise MODULE.SyncError("injected crash after preimage restoration")

        with (
            mock.patch.object(
                MODULE,
                "_restore_pending_record_before",
                side_effect=fail_after_preimage_restoration,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected crash after preimage restoration",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(restored)
        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertTrue(
            MODULE._pending_regular_publication_cleanup_path(
                batch,
                record,
                "produced",
            ).is_file()
        )
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

        install(self.release, self.home, SHA_A)

        self.assertFalse(MODULE._pending_link_pointer_path(self.home).exists())
        self.assertFalse(batch.batch_root.exists())
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_replace_recovery_recovers_interrupted_before_publication_cleanup(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_regular_publication_cleanup(next_release, SHA_B)
        record = next(candidate for candidate in batch.records if candidate.is_regular())
        source = batch.batch_root / Path(*record.before_evidence.parts)
        real_bound = MODULE._bound_directory_matches
        real_unlink = os.unlink
        before_publish_started = False

        def fail_after_before_publication(
            home: Path,
            path: Path,
            directory_fd: int,
        ) -> bool:
            nonlocal before_publish_started
            if path == source.parent and os.path.lexists(target):
                before_publish_started = True
                return False
            return real_bound(home, path, directory_fd)

        def fail_before_active_unlink(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> None:
            name = os.fsdecode(path)
            if (
                before_publish_started
                and name.startswith(MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX)
            ):
                raise MODULE.SyncError("injected before publication cleanup crash")
            real_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(
                MODULE,
                "_bound_directory_matches",
                side_effect=fail_after_before_publication,
            ),
            mock.patch.object(MODULE.os, "unlink", side_effect=fail_before_active_unlink),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "exact cleanup could not be verified",
            ),
        ):
            install(self.release, self.home, SHA_A)

        journal = MODULE._read_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "before",
        )
        self.assertIsNotNone(journal)
        assert journal is not None
        _snapshot, active_name, phase, _expected = journal
        self.assertEqual(phase, "before")
        active = target.with_name(active_name)
        self.assertEqual((active.stat().st_dev, active.stat().st_ino), old_identity)

        install(self.release, self.home, SHA_A)
        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_active_publication_foreign_replacement_is_retained(self) -> None:
        batch = self._interrupt_regular_publication_cleanup(self.release, SHA_A)
        _record, active = self._assert_active_publication_journal(batch)
        active.unlink()
        active.write_text("foreign = true\n", encoding="utf-8")
        active.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending regular publication active entry changed",
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(active.read_text(encoding="utf-8"), "foreign = true\n")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        self.assertTrue(batch.batch_root.is_dir())

    def test_v6_v7_uncommitted_create_and_replace_recover_without_v8_receipts(
        self,
    ) -> None:
        for version in (6, 7):
            for action in ("create", "replace"):
                with self.subTest(version=version, action=action):
                    self.home = self.root / f"home-v{version}-{action}"
                    release = self.release
                    sha = SHA_A
                    old_identity: tuple[int, int] | None = None
                    if action == "replace":
                        install(self.release, self.home, SHA_A)
                        target = self.home / ROLE_TARGET
                        old_identity = (target.stat().st_dev, target.stat().st_ino)
                        release = self.root / f"release-v{version}-{action}"
                        write_release(release, role_payload='name = "updated"\n')
                        sha = SHA_B

                    batch = self._interrupt_uncommitted_regular_publication(
                        release,
                        sha,
                    )
                    parsed = self._downgrade_pending_regular_metadata(batch, version)
                    record = next(
                        candidate
                        for candidate in parsed.records
                        if candidate.is_regular()
                    )
                    self.assertEqual(record.action, action)

                    install(self.release, self.home, SHA_A)

                    target = self.home / ROLE_TARGET
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        'name = "reviewer"\n',
                    )
                    self.assertEqual(target.stat().st_nlink, 1)
                    if old_identity is not None:
                        self.assertEqual(
                            (target.stat().st_dev, target.stat().st_ino),
                            old_identity,
                        )
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                    )

    def test_legacy_v6_writer_gid_recovers_uncommitted_create_and_replace(
        self,
    ) -> None:
        for action in ("create", "replace"):
            with self.subTest(action=action):
                self.home = self.root / f"home-v6-writer-{action}"
                release = self.release
                sha = SHA_A
                old_identity: tuple[int, int] | None = None
                if action == "replace":
                    install(self.release, self.home, SHA_A)
                    target = self.home / ROLE_TARGET
                    old_identity = (target.stat().st_dev, target.stat().st_ino)
                    release = self.root / "release-v6-writer-replace"
                    write_release(release, role_payload='name = "updated"\n')
                    sha = SHA_B

                batch = self._interrupt_uncommitted_regular_publication(release, sha)
                payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
                assert legacy_gid is not None
                write_pending_metadata_payload(batch, payload)
                regular_record = next(
                    record
                    for record in batch.records
                    if record.is_regular()
                    and record.action in {"create", "replace"}
                )
                assert regular_record.stage is not None
                stage = batch.batch_root / Path(*regular_record.stage.parts)
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != legacy_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                os.chown(stage, -1, alternate_gid)

                parsed = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                parsed_record = next(
                    record for record in parsed.records if record.is_regular()
                )
                self.assertEqual(parsed.metadata_version, 6)
                self.assertEqual(parsed_record.regular_gid, legacy_gid)
                self.assertEqual(stage.stat().st_gid, alternate_gid)

                install(self.release, self.home, SHA_A)

                target = self.home / ROLE_TARGET
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    'name = "reviewer"\n',
                )
                self.assertEqual(target.stat().st_nlink, 1)
                if old_identity is not None:
                    self.assertEqual(
                        (target.stat().st_dev, target.stat().st_ino),
                        old_identity,
                    )
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )

    def test_legacy_v6_writer_remove_recovers_uncommitted_rollback(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        original_identity = (target.stat().st_dev, target.stat().st_ino)
        original_gid = target.stat().st_gid
        removal_release = self.root / "release-v6-writer-remove-uncommitted"
        write_release(removal_release)

        batch = self._interrupt_uncommitted_regular_publication(
            removal_release,
            SHA_B,
        )
        payload, producing_gid = legacy_v6_writer_metadata_payload(batch)
        self.assertIsNone(producing_gid)
        write_pending_metadata_payload(batch, payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(record.action, "remove")
        self.assertIsNone(record.regular_gid)
        self.assertEqual(record.planned_snapshot.regular_gid, original_gid)
        self.assertFalse(os.path.lexists(target))

        install(self.release, self.home, SHA_A)

        self.assertTrue(target.is_file())
        self.assertEqual(
            (target.stat().st_dev, target.stat().st_ino),
            original_identity,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_legacy_v6_foreign_regular_gid_recovers_uncommitted_relinquishment(
        self,
    ) -> None:
        initial_release = self.root / "release-v6-foreign-initial"
        next_release = self.root / "release-v6-foreign-next"
        write_release(
            initial_release,
            role_payload='name = "managed"\n',
            target="AGENTS.md",
        )
        write_release(
            next_release,
            role_payload='name = "next"\n',
            target="AGENTS.md",
        )
        install(initial_release, self.home, SHA_A)
        target = self.home / "AGENTS.md"
        target.unlink()
        target.write_text('name = "foreign"\n', encoding="utf-8")
        target.chmod(0o600)
        historical_snapshot = MODULE._capture_reconcile_target_snapshot(
            self.home,
            target,
            capture_regular_content=True,
        )
        historical_gid = historical_snapshot.regular_gid
        self.assertIsNotNone(historical_snapshot.regular_sha256)
        self.assertIsNotNone(historical_gid)
        assert historical_gid is not None

        with (
            mock.patch.object(
                MODULE,
                "_publish_pending_commit_marker",
                side_effect=MODULE.SyncError("injected precommit crash"),
            ),
            mock.patch.object(
                MODULE,
                "_rollback_reconcile_transaction",
                side_effect=MODULE.SyncError("injected hard rollback crash"),
            ),
            self.assertRaisesRegex(MODULE.SyncError, "rollback was incomplete"),
        ):
            install(next_release, self.home, SHA_B)

        batch = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(batch)
        assert batch is not None
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["version"] = 6
        payload.pop("terminal_regular_before")
        payload.pop("terminal_regular_after")
        records = payload["records"]
        assert isinstance(records, list)
        foreign_record = None
        for raw_record in records:
            assert isinstance(raw_record, dict)
            raw_record.pop("publication_cleanup")
            if raw_record["action"] == MODULE.PENDING_RELINQUISH_FOREIGN_ACTION:
                foreign_record = raw_record
        self.assertIsNotNone(foreign_record)
        assert isinstance(foreign_record, dict)
        planned_before = foreign_record["planned_before"]
        assert isinstance(planned_before, dict)
        planned_before.update(
            {
                "regular_sha256": historical_snapshot.regular_sha256,
                "regular_size": historical_snapshot.regular_size,
                "regular_mode": historical_snapshot.regular_mode,
                "regular_uid": historical_snapshot.regular_uid,
                "regular_gid": historical_gid,
                "regular_link_count": historical_snapshot.regular_link_count,
            }
        )
        write_pending_metadata_payload(batch, payload)

        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != historical_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        parsed_record = next(
            record
            for record in parsed.records
            if record.action == MODULE.PENDING_RELINQUISH_FOREIGN_ACTION
        )
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(parsed_record.materialization, "symlink")
        self.assertEqual(parsed_record.planned_snapshot.regular_gid, historical_gid)
        self.assertEqual(target.stat().st_gid, alternate_gid)

        install(next_release, self.home, SHA_B)

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "foreign"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)
        self.assertNotIn(
            PurePosixPath("AGENTS.md"),
            MODULE._load_managed_state(self.home).links,
        )
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_v6_v7_produced_active_alias_residue_recovers_exact_inode(self) -> None:
        for version in (6, 7):
            with self.subTest(version=version):
                self.home = self.root / f"home-v{version}-active-residue"
                batch = self._interrupt_uncommitted_regular_publication(
                    self.release,
                    SHA_A,
                )
                parsed = self._downgrade_pending_regular_metadata(batch, version)
                record = next(
                    candidate for candidate in parsed.records if candidate.is_regular()
                )
                target = self.home / Path(*record.target.parts)
                active = self._isolate_legacy_regular_publication(
                    parsed,
                    record,
                    target,
                )

                install(self.release, self.home, SHA_A)

                self.assertFalse(os.path.lexists(active))
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    'name = "reviewer"\n',
                )
                self.assertEqual(target.stat().st_nlink, 1)

    def test_v7_legacy_active_entries_share_one_parent_scan_and_alias_index(
        self,
    ) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security"\n',
        )
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        records = tuple(record for record in parsed.records if record.is_regular())
        self.assertEqual({record.target for record in records}, {ROLE_TARGET, secondary_target})
        with mock.patch.object(
            MODULE,
            "_build_pending_regular_alias_authority_index",
            wraps=MODULE._build_pending_regular_alias_authority_index,
        ) as build_alias_index:
            for record in records:
                MODULE._pending_record_evidence_snapshot(self.home, parsed, record)
        self.assertEqual(build_alias_index.call_count, 1)
        active_paths = [
            self._isolate_legacy_regular_publication(
                parsed,
                record,
                self.home / Path(*record.target.parts),
            )
            for record in records
        ]
        parent_identity = (
            self.home / Path(*ROLE_TARGET.parts)
        ).parent.stat()
        expected_identity = (parent_identity.st_dev, parent_identity.st_ino)
        real_scandir = MODULE.os.scandir
        agent_parent_scans = 0

        def count_agent_parent_scans(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        ) -> object:
            nonlocal agent_parent_scans
            if isinstance(path, int) and MODULE._directory_identity(path) == expected_identity:
                agent_parent_scans += 1
            return real_scandir(path)

        with (
            mock.patch.object(
                MODULE.os,
                "scandir",
                side_effect=count_agent_parent_scans,
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(agent_parent_scans, 1)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))
        for active in active_paths:
            self.assertFalse(os.path.lexists(active))
        for target in (ROLE_TARGET, secondary_target):
            installed = self.home / Path(*target.parts)
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.stat().st_nlink, 1)

    def test_v7_legacy_active_entry_duplicate_candidates_fail_closed(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(
            candidate for candidate in parsed.records if candidate.is_regular()
        )
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        assert record.planned_snapshot.parent_identity is not None
        assert record.evidence_identity is not None
        duplicate = active.with_name(
            MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                record.planned_snapshot.parent_identity,
                (
                    record.evidence_identity[0],
                    record.evidence_identity[1],
                    stat.S_IFREG,
                ),
            )
        )
        os.link(active, duplicate, follow_symlinks=False)
        index = MODULE._build_legacy_pending_regular_publication_active_entry_index(
            self.home,
            parsed,
        )

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending legacy regular publication cleanup is ambiguous",
        ):
            MODULE._recover_legacy_pending_regular_publication_active_entry(
                self.home,
                parsed,
                record,
                "produced",
                active_entry_index=index,
            )

        self.assertTrue(active.is_file())
        self.assertTrue(duplicate.is_file())
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v7_legacy_active_entry_batch_scan_budget_fails_before_unlink(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security"\n',
        )
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        active_paths = [
            self._isolate_legacy_regular_publication(
                parsed,
                record,
                self.home / Path(*record.target.parts),
            )
            for record in parsed.records
            if record.is_regular()
        ]

        with (
            mock.patch.object(MODULE, "MAX_PENDING_CLEANUP_CONTROL_ENTRIES", 1),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "pending legacy regular publication active-entry scan exceeds the "
                "batch limit",
            ),
        ):
            MODULE._build_legacy_pending_regular_publication_active_entry_index(
                self.home,
                parsed,
            )

        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())
        for active in active_paths:
            self.assertTrue(active.is_file())

    def test_v7_produced_active_alias_foreign_replacement_fails_closed(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(candidate for candidate in parsed.records if candidate.is_regular())
        target = self.home / Path(*record.target.parts)
        active = self._isolate_legacy_regular_publication(parsed, record, target)
        active.unlink()
        active.write_text("foreign = true\n", encoding="utf-8")
        active.chmod(0o600)

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "pending legacy regular publication .* changed",
        ):
            install(self.release, self.home, SHA_A)

        self.assertEqual(active.read_text(encoding="utf-8"), "foreign = true\n")
        self.assertTrue(MODULE._pending_link_pointer_path(self.home).is_file())

    def test_v7_before_active_alias_residue_recovers_exact_preimage(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        old_identity = (target.stat().st_dev, target.stat().st_ino)
        next_release = self.root / "next-release-v7-before"
        write_release(next_release, role_payload='name = "updated"\n')
        batch = self._interrupt_uncommitted_regular_publication(next_release, SHA_B)
        parsed = self._downgrade_pending_regular_metadata(batch, 7)
        record = next(candidate for candidate in parsed.records if candidate.is_regular())
        assert record.before_evidence is not None
        before = parsed.batch_root / Path(*record.before_evidence.parts)
        parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
        try:
            parent_identity = MODULE._directory_identity(parent_fd)
            before_metadata = before.stat()
            planned = MODULE._pending_cleanup_entry_plan(before_metadata)
            active_name = MODULE._pending_cleanup_entry_name(
                MODULE.PENDING_CLEANUP_ACTIVE_ENTRY_PREFIX,
                parent_identity,
                planned,
            )
            active = target.with_name(active_name)
            os.link(before, active, follow_symlinks=False)
            os.fsync(parent_fd)
        finally:
            MODULE._close_fd_quietly(parent_fd)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "updated"\n')
        self.assertEqual((active.stat().st_dev, active.stat().st_ino), old_identity)

        install(self.release, self.home, SHA_A)

        self.assertFalse(os.path.lexists(active))
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), old_identity)
        self.assertEqual(target.stat().st_nlink, 1)

    def test_publication_receipt_recovers_truncated_atomic_temp(self) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(candidate for candidate in batch.records if candidate.is_regular())
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        journal = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        temp = journal.with_name(
            journal.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        )
        cleanup_parent_identity = (
            batch.batch_root / "pending" / "cleanup"
        ).stat()
        parent_identity = (
            cleanup_parent_identity.st_dev,
            cleanup_parent_identity.st_ino,
        )
        retained_temp_name = (
            MODULE.PENDING_CLEANUP_RETAINED_PREFIX
            + temp.name
            + "-1-"
            + "a" * 16
        )
        self.assertTrue(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                temp.name,
                parent_identity,
            )
        )
        self.assertTrue(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                retained_temp_name,
                parent_identity,
            )
        )
        self.assertFalse(
            MODULE._pending_batch_cleanup_name_is_authorized(
                ("pending", "cleanup"),
                "foreign.json.publish-tmp",
                parent_identity,
            )
        )
        temp.write_bytes(b"{")
        temp.chmod(0o600)

        MODULE._delete_pending_regular_publication_beneath(
            self.home,
            batch,
            record,
            target,
            expected,
            phase="produced",
        )

        self.assertFalse(os.path.lexists(temp))
        self.assertFalse(os.path.lexists(target))
        self.assertIsNotNone(
            MODULE._read_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )
        )

    def test_publication_receipt_is_complete_after_atomic_rename_boundary_crash(
        self,
    ) -> None:
        batch = self._interrupt_uncommitted_regular_publication(self.release, SHA_A)
        record = next(candidate for candidate in batch.records if candidate.is_regular())
        target = self.home / Path(*record.target.parts)
        expected, exists = MODULE._pending_target_snapshot(self.home, target)
        self.assertTrue(exists)
        assert isinstance(expected, MODULE.RegularFileSnapshot)
        journal = MODULE._pending_regular_publication_cleanup_path(
            batch,
            record,
            "produced",
        )
        temp_name = journal.name + MODULE.PENDING_ATOMIC_PUBLICATION_TEMP_SUFFIX
        real_rename = MODULE._rename_noreplace_at
        crashed = False

        def crash_after_atomic_publication(
            source_fd: int,
            source_name: str,
            destination_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal crashed
            real_rename(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
            if source_name == temp_name and destination_name == journal.name:
                crashed = True
                raise MODULE.SyncError("injected atomic rename boundary crash")

        with (
            mock.patch.object(
                MODULE,
                "_rename_noreplace_at",
                side_effect=crash_after_atomic_publication,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "atomic rename boundary crash"),
        ):
            MODULE._delete_pending_regular_publication_beneath(
                self.home,
                batch,
                record,
                target,
                expected,
                phase="produced",
            )

        self.assertTrue(crashed)
        self.assertTrue(target.is_file())
        self.assertTrue(journal.is_file())
        self.assertFalse(os.path.lexists(journal.with_name(temp_name)))
        self.assertIsNotNone(
            MODULE._read_pending_regular_publication_cleanup(
                self.home,
                batch,
                record,
                "produced",
            )
        )

        MODULE._recover_pending_regular_publication_cleanup(
            self.home,
            batch,
            record,
            "produced",
        )
        self.assertFalse(os.path.lexists(target))

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
        # A failed first install has no before-state regular target, so v7 does
        # not mint terminal cleanup authority for the rolled-back phase.
        self.assertIsNone(ticket)
        self.assertEqual(MODULE._cleanup_ready_pending_batches(self.home), 0)
        self.assertTrue(batch.batch_root.is_dir())
        self._assert_recovered_install()

    def test_post_clear_cleanup_failure_is_retried_from_durable_ticket(self) -> None:
        real_clear = MODULE._clear_pending_link_pointer

        def fail_after_pointer_clear(
            home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            real_clear(home, batch, phase=phase)
            if phase == "after":
                raise MODULE.SyncError("injected post-clear cleanup failure")

        with (
            mock.patch.object(
                MODULE,
                "_clear_pending_link_pointer",
                side_effect=fail_after_pointer_clear,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed managed state but finalization failed",
            ),
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

    def test_live_transaction_tolerates_mode_0600_gid_churn(self) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        append_regular_link(
            next_release,
            target="agents/security-reviewer.toml",
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        real_capture = MODULE._capture_managed_state_link_snapshots
        captures = 0
        churned = False

        def churn_gid_after_live_baseline(
            home: Path,
            state: MODULE.ManagedState,
        ) -> dict[
            PurePosixPath,
            MODULE.SymlinkSnapshot | MODULE.RegularFileSnapshot,
        ]:
            nonlocal captures, churned
            snapshots = real_capture(home, state)
            captures += 1
            # install_release_tree performs an unlocked preflight first. Drift
            # only after the locked transaction's baseline has been captured.
            if captures == 2:
                os.chown(target, -1, alternate_gid)
                churned = True
            return snapshots

        with mock.patch.object(
            MODULE,
            "_capture_managed_state_link_snapshots",
            side_effect=churn_gid_after_live_baseline,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_replace_tolerates_mode_0600_gid_churn_during_destructive_staging(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "updated"\n')
        real_move = MODULE._atomic_move_beneath_home
        churned = False

        def churn_gid_before_move(*args: object, **kwargs: object) -> None:
            nonlocal churned
            if args[1] == target:
                os.chown(target, -1, alternate_gid)
                churned = True
            real_move(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=churn_gid_before_move,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "updated"\n')

    def test_removal_tolerates_mode_0600_gid_churn_during_destructive_staging(
        self,
    ) -> None:
        install(self.release, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        next_release = self.root / "next-release"
        write_release(next_release)
        real_move = MODULE._atomic_move_beneath_home
        churned = False

        def churn_gid_before_move(*args: object, **kwargs: object) -> None:
            nonlocal churned
            if args[1] == target:
                os.chown(target, -1, alternate_gid)
                churned = True
            real_move(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_atomic_move_beneath_home",
            side_effect=churn_gid_before_move,
        ):
            install(next_release, self.home, SHA_B)

        self.assertTrue(churned)
        self.assertFalse(os.path.lexists(target))

    def test_replace_rollback_tolerates_mode_0600_backup_gid_churn(self) -> None:
        target = self.home / ROLE_TARGET
        backup = (
            self.home
            / "personal-sync"
            / "quarantine"
            / "rollback"
            / ROLE_TARGET
        )
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o600)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        MODULE._atomic_move_beneath_home(
            self.home,
            target,
            backup,
            planned,
            backup_parent_identity,
        )
        os.chown(backup, -1, alternate_gid)
        action = MODULE.ReconcileAction(
            action="replace",
            target=target,
            link_target="",
            kind="file",
            planned_snapshot=planned,
            materialization="regular",
        )
        MODULE._rollback_reconcile_transaction(
            self.home,
            MODULE.ReconcileTransaction(
                batch_root=None,
                mutations=[MODULE.ReconcileMutation(action=action, backup=backup)],
            ),
        )

        self.assertEqual(target.read_text(encoding="utf-8"), 'name = "reviewer"\n')
        self.assertEqual(target.stat().st_gid, alternate_gid)
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_move_rejects_gid_churn_when_group_access_is_granted(
        self,
    ) -> None:
        target = self.home / ROLE_TARGET
        backup = self.home / "personal-sync" / "quarantine" / "reviewer.toml"
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o640)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        os.chown(target, -1, alternate_gid)
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )

        with self.assertRaisesRegex(MODULE.SyncError, "source changed after planning"):
            MODULE._atomic_move_beneath_home(
                self.home,
                target,
                backup,
                planned,
                backup_parent_identity,
            )

        self.assertTrue(target.is_file())
        self.assertFalse(os.path.lexists(backup))

    def test_destructive_backup_rejects_gid_churn_when_group_access_is_granted(
        self,
    ) -> None:
        target = self.home / ROLE_TARGET
        backup = self.home / "personal-sync" / "quarantine" / "reviewer.toml"
        target.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        target.write_text('name = "reviewer"\n', encoding="utf-8")
        target.chmod(0o640)
        planned = MODULE._capture_reconcile_target_snapshot(self.home, target)
        alternate_gid = next(
            (gid for gid in os.getgroups() if gid != target.stat().st_gid),
            None,
        )
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group is available")
        backup_parent_identity = (
            backup.parent.stat().st_dev,
            backup.parent.stat().st_ino,
        )
        MODULE._atomic_move_beneath_home(
            self.home,
            target,
            backup,
            planned,
            backup_parent_identity,
        )
        os.chown(backup, -1, alternate_gid)
        action = MODULE.ReconcileAction(
            action="remove",
            target=target,
            link_target="",
            kind="file",
            planned_snapshot=planned,
            materialization="regular",
        )

        with self.assertRaisesRegex(MODULE.SyncError, "target changed after preflight"):
            MODULE._verify_reconcile_backup(self.home, action, backup)

        self.assertFalse(os.path.lexists(target))
        self.assertTrue(backup.is_file())

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

    def test_terminal_validation_rechecks_first_target_after_later_member(self) -> None:
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            self.release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        first_target = self.home / ROLE_TARGET
        real_verify = MODULE._verify_final_regular_targets
        real_read = MODULE._read_regular_file_snapshot_beneath
        validating_terminal_group = False
        drifted = False

        def drift_first_while_reading_later(
            home: Path,
            path: Path,
            *,
            require_managed_access: bool,
        ) -> MODULE.RegularFileSnapshot:
            nonlocal drifted
            snapshot = real_read(
                home,
                path,
                require_managed_access=require_managed_access,
            )
            if (
                validating_terminal_group
                and not drifted
                and path == home / Path(*secondary_target.parts)
            ):
                first_target.write_text("tampered = true\n", encoding="utf-8")
                first_target.chmod(0o600)
                drifted = True
            return snapshot

        def verify_with_mid_pass_drift(
            home: Path,
            ticket: MODULE.PendingBatchCleanupTicket,
        ) -> None:
            nonlocal validating_terminal_group
            validating_terminal_group = True
            try:
                real_verify(home, ticket)
            finally:
                validating_terminal_group = False

        with (
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                side_effect=drift_first_while_reading_later,
            ),
            mock.patch.object(
                MODULE,
                "_verify_final_regular_targets",
                side_effect=verify_with_mid_pass_drift,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            install(self.release, self.home, SHA_A)

        self.assertTrue(drifted)


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

    def test_v8_metadata_projection_includes_empty_terminal_regular_arrays(
        self,
    ) -> None:
        payload = MODULE._projected_pending_metadata_payload(
            state_before_exists=False,
            records=[],
            claims_before=[],
            claims_after=[],
            releases_before=[],
            releases_after=[],
            terminal_regular_before=[],
            terminal_regular_after=[],
        )

        self.assertEqual(payload["version"], 8)
        self.assertEqual(payload["terminal_regular_before"], [])
        self.assertEqual(payload["terminal_regular_after"], [])

    def test_runtime_metadata_capacity_projects_terminal_regular_states(self) -> None:
        profile = MODULE._manifest_transition_capacity_profile(
            MODULE.PUBLIC_OWNER,
            {
                ROLE_TARGET.as_posix(): {
                    "source": "personal_codex/agents/reviewer.toml",
                    "target": ROLE_TARGET.as_posix(),
                    "kind": "file",
                }
            },
            {},
        )
        capacity = MODULE.PendingLinkCapacityPlan(
            ordered_groups=(),
            flattened_actions=(),
            retired_absence_specs=(),
        )
        with mock.patch.object(
            MODULE,
            "_bounded_json_document",
            wraps=MODULE._bounded_json_document,
        ) as encode:
            MODULE._validate_pending_link_metadata_capacity(
                self.home,
                capacity,
                MODULE.ManagedStateFileSnapshot(exists=True),
                profile.state,
                profile.state,
                profile.state,
            )

        projected = encode.call_args.args[0]
        self.assertEqual(
            [item["target"] for item in projected["terminal_regular_before"]],
            [ROLE_TARGET.as_posix()],
        )
        self.assertEqual(
            [item["target"] for item in projected["terminal_regular_after"]],
            [ROLE_TARGET.as_posix()],
        )

    def test_manifest_transition_capacity_counts_terminal_regular_arrays(self) -> None:
        profile = MODULE._manifest_transition_capacity_profile(
            MODULE.PUBLIC_OWNER,
            {
                ROLE_TARGET.as_posix(): {
                    "source": "personal_codex/agents/reviewer.toml",
                    "target": ROLE_TARGET.as_posix(),
                    "kind": "file",
                }
            },
            {},
        )
        without_terminal_regular = MODULE.replace(
            profile,
            terminal_regular_size_sum=0,
            terminal_regular_count=0,
        )

        self.assertEqual(profile.terminal_regular_count, 1)
        self.assertGreater(
            MODULE._manifest_transition_metadata_size(profile, profile),
            MODULE._manifest_transition_metadata_size(
                without_terminal_regular,
                without_terminal_regular,
            ),
        )

    def test_regular_snapshot_gid_comparison_follows_group_access(self) -> None:
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
            )
        )
        non_access_bearing_expected = MODULE.replace(expected, mode=0o600)
        non_access_bearing_actual = MODULE.replace(actual, mode=0o600)
        self.assertTrue(
            MODULE._regular_snapshot_matches(
                non_access_bearing_actual,
                non_access_bearing_expected.parent_identity,
                non_access_bearing_expected,
                expected_link_count=1,
            )
        )

    def test_reconcile_target_gid_revalidation_follows_group_access(self) -> None:
        expected = MODULE.ReconcileTargetSnapshot(
            parent_identity=(1, 2),
            link_identity=(3, 4),
            ancestor_identity=(1, 2),
            regular_sha256="a" * 64,
            regular_size=10,
            regular_mode=0o600,
            regular_uid=501,
            regular_gid=20,
            regular_link_count=1,
        )
        actual = MODULE.replace(expected, regular_gid=80)
        target = self.home / ROLE_TARGET

        with mock.patch.object(
            MODULE,
            "_capture_reconcile_target_snapshot",
            return_value=actual,
        ):
            MODULE._require_reconcile_target_snapshot(self.home, target, expected)

        group_expected = MODULE.replace(expected, regular_mode=0o640)
        group_actual = MODULE.replace(actual, regular_mode=0o640)
        with (
            mock.patch.object(
                MODULE,
                "_capture_reconcile_target_snapshot",
                return_value=group_actual,
            ),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "managed target changed after planning",
            ),
        ):
            MODULE._require_reconcile_target_snapshot(
                self.home,
                target,
                group_expected,
            )

    def test_managed_file_evidence_gid_comparison_follows_group_access(self) -> None:
        expected = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"abc",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=3,
            uid=501,
            gid=20,
        )
        actual = MODULE.replace(expected, gid=80)

        self.assertTrue(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                actual,
                expected,
            )
        )
        group_expected = MODULE.replace(expected, mode=0o640)
        group_actual = MODULE.replace(actual, mode=0o640)
        self.assertFalse(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                group_actual,
                group_expected,
            )
        )
        self.assertFalse(
            MODULE._managed_state_snapshot_matches_bound_file_evidence(
                MODULE.replace(actual, parent_identity=(9, 9)),
                expected,
            )
        )
        for field, value in (
            ("file_identity", (9, 9)),
            ("payload", b"abd"),
            ("mode", 0o640),
            ("uid", 502),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    MODULE._managed_state_snapshot_matches_bound_file_evidence(
                        MODULE.replace(actual, **{field: value}),
                        expected,
                    )
                )

    def test_control_snapshot_rechecks_tolerate_owner_only_gid_churn(self) -> None:
        snapshot = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"control\n",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=8,
            uid=os.geteuid(),
            gid=20,
        )
        churned = MODULE.replace(snapshot, gid=80)
        absent = MODULE.ManagedStateFileSnapshot(exists=False)
        control_path = self.home / "control" / "marker"

        with (
            mock.patch.object(MODULE, "_open_directory_beneath", return_value=10),
            mock.patch.object(MODULE, "_close_fd_quietly"),
            mock.patch.object(
                MODULE,
                "_read_managed_state_file_snapshot",
                side_effect=(absent, churned, churned),
            ),
            mock.patch.object(
                MODULE,
                "_write_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(MODULE, "_discard_incomplete_pending_cleanup_ticket"),
            mock.patch.object(
                MODULE,
                "_pending_cleanup_temp_residue_is_observed",
                return_value=False,
            ),
            mock.patch.object(MODULE, "_rename_noreplace_at"),
            mock.patch.object(MODULE.os, "fsync"),
        ):
            published = MODULE._publish_atomic_exclusive_internal_file(
                self.home,
                control_path,
                snapshot.payload,
            )
        self.assertEqual(published, churned)

        state_before = MODULE.ManagedStateFileSnapshot(
            exists=False,
            parent_identity=(5, 6),
        )
        rollback_batch = SimpleNamespace(
            batch_root=self.home / "20000101T000000Z-1-1",
            state_before=state_before,
        )
        with (
            mock.patch.object(
                MODULE,
                "_pending_rollback_marker_snapshot",
                side_effect=(None, churned),
            ),
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
        ):
            self.assertEqual(
                MODULE._publish_pending_rollback_marker(
                    self.home,
                    rollback_batch,
                ),
                churned,
            )

        batch_root = self.home / "20000101T000000Z-1-2"
        staging_ticket = SimpleNamespace(
            version=3,
            phase="staging",
            batch_root_identity=(7, 8),
            snapshot=SimpleNamespace(payload=b"ticket\n"),
        )
        with (
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(
                MODULE,
                "_pending_staging_marker_snapshot",
                return_value=churned,
            ),
            mock.patch.object(
                MODULE,
                "_pending_staging_cleanup_ticket_payload",
                return_value=b"ticket\n",
            ),
            mock.patch.object(
                MODULE,
                "_publish_pending_batch_cleanup_ticket_for_root",
            ),
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=staging_ticket,
            ),
        ):
            self.assertIs(
                MODULE._mark_pending_batch_staging_cleanup_ready(
                    self.home,
                    batch_root,
                    (7, 8),
                ),
                staging_ticket,
            )

        proof_ticket = SimpleNamespace(
            batch_root=self.home / "20000101T000000Z-1-3"
        )
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_empty_proof",
                side_effect=(None, churned),
            ),
            mock.patch.object(
                MODULE,
                "_publish_atomic_exclusive_internal_file",
                return_value=snapshot,
            ),
            mock.patch.object(
                MODULE,
                "_pending_cleanup_empty_proof_payload",
                return_value=snapshot.payload,
            ),
        ):
            self.assertEqual(
                MODULE._publish_pending_cleanup_empty_proof(
                    self.home,
                    proof_ticket,
                    (9, 10),
                ),
                churned,
            )

    def test_terminal_ticket_recheck_uses_access_policy_semantics(self) -> None:
        ticket_snapshot = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b"ticket\n",
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=7,
            uid=os.geteuid(),
            gid=20,
        )
        target_expectation = MODULE.PendingRegularTargetExpectation(
            target=ROLE_TARGET,
            parent_identity=(5, 6),
            file_identity=(7, 8),
            sha256="a" * 64,
            size=10,
            mode=0o600,
            uid=os.geteuid(),
        )
        ticket = MODULE.PendingBatchCleanupTicket(
            version=4,
            phase="after",
            path=self.home / "ticket.json",
            snapshot=ticket_snapshot,
            batch_root=self.home / "batch",
            batch_root_identity=(9, 10),
            marker_path=MODULE.PENDING_STATE_COMMIT_MARKER,
            marker_parent_identity=(11, 12),
            marker_file_identity=(13, 14),
            marker_mode=0o600,
            marker_sha256="b" * 64,
            terminal_regular_targets=(target_expectation,),
        )
        churned_ticket = MODULE.replace(
            ticket,
            snapshot=MODULE.replace(ticket_snapshot, gid=80),
        )
        target_snapshot = MODULE.RegularFileSnapshot(
            parent_identity=target_expectation.parent_identity,
            file_identity=target_expectation.file_identity,
            sha256=target_expectation.sha256,
            size=target_expectation.size,
            mode=target_expectation.mode,
            uid=target_expectation.uid,
            gid=80,
            link_count=1,
        )
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=churned_ticket,
            ),
            mock.patch.object(
                MODULE,
                "_read_regular_file_snapshot_beneath",
                return_value=target_snapshot,
            ),
        ):
            MODULE._verify_final_regular_targets(self.home, ticket)

        changed_ticket = MODULE.replace(churned_ticket, marker_sha256="c" * 64)
        with (
            mock.patch.object(
                MODULE,
                "_read_pending_cleanup_ticket",
                return_value=changed_ticket,
            ),
            self.assertRaisesRegex(MODULE.SyncError, "cleanup ticket changed"),
        ):
            MODULE._verify_final_regular_targets(self.home, ticket)

    def test_stat_metadata_gid_comparison_follows_group_access(self) -> None:
        def metadata(mode: int, gid: int) -> SimpleNamespace:
            return SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_mode=stat.S_IFREG | mode,
                st_uid=501,
                st_gid=gid,
                st_size=10,
                st_nlink=1,
            )

        self.assertTrue(
            MODULE._regular_stat_metadata_matches(
                metadata(0o600, 80),
                metadata(0o600, 20),
            )
        )
        self.assertFalse(
            MODULE._regular_stat_metadata_matches(
                metadata(0o640, 80),
                metadata(0o640, 20),
            )
        )

    def test_managed_state_file_match_gid_follows_group_access(self) -> None:
        expected = MODULE.ManagedStateFileSnapshot(
            exists=True,
            payload=b'{"version": 1}\n',
            mode=0o600,
            parent_identity=(1, 2),
            file_identity=(3, 4),
            file_type=stat.S_IFREG,
            size=15,
            uid=501,
            gid=20,
        )
        actual = MODULE.replace(expected, gid=80)
        target = self.home / "state" / "managed.json"

        with mock.patch.object(
            MODULE,
            "_read_managed_state_file_snapshot",
            return_value=actual,
        ):
            self.assertTrue(
                MODULE._managed_state_file_matches(
                    self.home,
                    target,
                    expected,
                    parent_fd=10,
                )
            )

        group_expected = MODULE.replace(expected, mode=0o640)
        group_actual = MODULE.replace(actual, mode=0o640)
        with mock.patch.object(
            MODULE,
            "_read_managed_state_file_snapshot",
            return_value=group_actual,
        ):
            self.assertFalse(
                MODULE._managed_state_file_matches(
                    self.home,
                    target,
                    group_expected,
                    parent_fd=10,
                )
            )

    def test_regular_file_snapshot_named_open_gid_churn_follows_group_access(
        self,
    ) -> None:
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                target = self.home / f"regular-snapshot-{mode:o}.toml"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('name = "reviewer"\n', encoding="utf-8")
                target.chmod(mode)
                original_gid = target.stat().st_gid
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != original_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
                real_open = MODULE.os.open
                churned = False

                def churn_gid_before_open(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode_bits: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal churned
                    if path == target.name and dir_fd == parent_fd and not churned:
                        os.chown(target, -1, alternate_gid)
                        churned = True
                    return real_open(path, flags, mode_bits, dir_fd=dir_fd)

                try:
                    with mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=churn_gid_before_open,
                    ):
                        if mode == 0o600:
                            snapshot = MODULE._regular_file_snapshot_at(
                                parent_fd,
                                target.name,
                                target,
                            )
                            self.assertEqual(snapshot.gid, original_gid)
                        else:
                            with self.assertRaisesRegex(
                                MODULE.SyncError,
                                "changed before read",
                            ):
                                MODULE._regular_file_snapshot_at(
                                    parent_fd,
                                    target.name,
                                    target,
                                )
                finally:
                    MODULE._close_fd_quietly(parent_fd)
                self.assertTrue(churned)
                self.assertEqual(target.stat().st_gid, alternate_gid)

    def test_managed_state_reader_named_open_gid_churn_follows_group_access(
        self,
    ) -> None:
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                target = self.home / "state" / f"managed-{mode:o}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"version": 1}\n', encoding="utf-8")
                target.chmod(mode)
                original_gid = target.stat().st_gid
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != original_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                parent_fd = MODULE._open_directory_beneath(self.home, target.parent)
                real_open = MODULE.os.open
                churned = False

                def churn_gid_before_open(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode_bits: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    nonlocal churned
                    if path == target.name and dir_fd == parent_fd and not churned:
                        os.chown(target, -1, alternate_gid)
                        churned = True
                    return real_open(path, flags, mode_bits, dir_fd=dir_fd)

                try:
                    with mock.patch.object(
                        MODULE.os,
                        "open",
                        side_effect=churn_gid_before_open,
                    ):
                        if mode == 0o600:
                            snapshot = MODULE._read_managed_state_file_snapshot(
                                self.home,
                                target,
                                parent_fd,
                            )
                            self.assertEqual(snapshot.gid, alternate_gid)
                        else:
                            with self.assertRaisesRegex(
                                MODULE.SyncError,
                                "changed before read",
                            ):
                                MODULE._read_managed_state_file_snapshot(
                                    self.home,
                                    target,
                                    parent_fd,
                                )
                finally:
                    MODULE._close_fd_quietly(parent_fd)
                self.assertTrue(churned)
                self.assertEqual(target.stat().st_gid, alternate_gid)

    def _retain_committed_batch(
        self,
        release: Path,
        *,
        sha: str = SHA_A,
    ) -> MODULE.PendingLinkBatch:
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
            install(release, self.home, sha)
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

    def test_legacy_v6_writer_gid_recovers_committed_create_and_replace(
        self,
    ) -> None:
        for action in ("create", "replace"):
            with self.subTest(action=action):
                self.home = self.root / f"home-committed-v6-writer-{action}"
                release = self.root / f"release-committed-v6-writer-{action}"
                sha = SHA_A
                expected_payload = 'name = "reviewer"\n'
                if action == "replace":
                    initial = self.root / "release-committed-v6-writer-initial"
                    write_release(initial, role_payload='name = "initial"\n')
                    install(initial, self.home, SHA_A)
                    expected_payload = 'name = "updated"\n'
                    sha = SHA_B
                write_release(release, role_payload=expected_payload)

                batch = self._retain_committed_batch(release, sha=sha)
                payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
                assert legacy_gid is not None
                write_pending_metadata_payload(batch, payload)
                target = self.home / ROLE_TARGET
                alternate_gid = next(
                    (gid for gid in os.getgroups() if gid != legacy_gid),
                    None,
                )
                if alternate_gid is None:
                    self.skipTest("no alternate supplementary group is available")
                os.chown(target, -1, alternate_gid)

                parsed = MODULE._load_pending_link_batch(self.home)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                parsed_record = next(
                    record for record in parsed.records if record.is_regular()
                )
                self.assertEqual(parsed.metadata_version, 6)
                self.assertEqual(parsed_record.regular_gid, legacy_gid)
                self.assertEqual(target.stat().st_gid, alternate_gid)

                install(release, self.home, sha)

                self.assertEqual(target.read_text(encoding="utf-8"), expected_payload)
                self.assertEqual(target.stat().st_gid, alternate_gid)
                self.assertEqual(target.stat().st_nlink, 1)
                self.assertFalse(
                    os.path.lexists(MODULE._pending_link_pointer_path(self.home))
                )

    def test_legacy_v6_writer_remove_recovers_committed_finalization(self) -> None:
        initial = self.root / "release-committed-v6-writer-remove-initial"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        target = self.home / ROLE_TARGET
        original_gid = target.stat().st_gid
        removal_release = self.root / "release-committed-v6-writer-remove"
        write_release(removal_release)

        batch = self._retain_committed_batch(removal_release, sha=SHA_B)
        payload, producing_gid = legacy_v6_writer_metadata_payload(batch)
        self.assertIsNone(producing_gid)
        write_pending_metadata_payload(batch, payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        record = next(candidate for candidate in parsed.records if candidate.is_regular())
        self.assertEqual(parsed.metadata_version, 6)
        self.assertEqual(record.action, "remove")
        self.assertIsNone(record.regular_gid)
        self.assertEqual(record.planned_snapshot.regular_gid, original_gid)
        self.assertFalse(os.path.lexists(target))

        install(removal_release, self.home, SHA_B)

        self.assertFalse(os.path.lexists(target))
        self.assertNotIn(ROLE_TARGET, MODULE._load_managed_state(self.home).links)
        self.assertFalse(os.path.lexists(MODULE._pending_link_pointer_path(self.home)))

    def test_pending_regular_gid_version_validation(self) -> None:
        release = self.root / "release-regular-gid-validation"
        write_release(release, role_payload='name = "reviewer"\n')
        batch = self._retain_committed_batch(release)
        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        current_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        legacy_payload, legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert legacy_gid is not None

        for invalid_gid in (None, True, "20", -1):
            with self.subTest(version=6, regular_gid=invalid_gid):
                payload = json.loads(json.dumps(legacy_payload))
                regular_record = next(
                    record
                    for record in payload["records"]
                    if record["materialization"] == "regular"
                )
                regular_record["regular_gid"] = invalid_gid
                write_pending_metadata_payload(batch, payload)
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "invalid regular-file evidence",
                ):
                    MODULE._load_pending_link_batch(self.home)

        for version in (7, 8):
            with self.subTest(version=version, regular_gid=legacy_gid):
                payload = json.loads(json.dumps(current_payload))
                payload["version"] = version
                if version == 7:
                    for raw_record in payload["records"]:
                        raw_record.pop("publication_cleanup")
                regular_record = next(
                    record
                    for record in payload["records"]
                    if record["materialization"] == "regular"
                )
                regular_record["regular_gid"] = legacy_gid
                write_pending_metadata_payload(batch, payload)
                with self.assertRaisesRegex(
                    MODULE.SyncError,
                    "invalid regular-file evidence",
                ):
                    MODULE._load_pending_link_batch(self.home)

    def test_v5_symlink_pending_metadata_remains_readable(self) -> None:
        release = self.root / "symlink-release"
        write_release(release)
        batch = self._retain_committed_batch(release)

        def downgrade(payload: dict[str, object]) -> None:
            payload["version"] = 5
            payload.pop("terminal_regular_before", None)
            payload.pop("terminal_regular_after", None)
            records = payload["records"]
            assert isinstance(records, list)
            for raw_record in records:
                assert isinstance(raw_record, dict)
                raw_record.pop("publication_cleanup")
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
        payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert _legacy_gid is not None
        records = payload["records"]
        assert isinstance(records, list)
        record = records[-1]
        assert isinstance(record, dict)
        record["unexpected_regular_field"] = True
        write_pending_metadata_payload(batch, payload)

        with self.assertRaisesRegex(MODULE.SyncError, "record .* is invalid"):
            MODULE._load_pending_link_batch(self.home)

    def test_v8_terminal_regular_sets_cover_all_state_targets(self) -> None:
        initial = self.root / "initial-release"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        secondary_target = PurePosixPath("agents/security-reviewer.toml")
        append_regular_link(
            next_release,
            target=secondary_target.as_posix(),
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )

        batch = self._retain_committed_batch(next_release, sha=SHA_B)

        metadata_path = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 8)
        self.assertEqual(
            [item["target"] for item in metadata["terminal_regular_before"]],
            [ROLE_TARGET.as_posix()],
        )
        self.assertEqual(
            [item["target"] for item in metadata["terminal_regular_after"]],
            [ROLE_TARGET.as_posix(), secondary_target.as_posix()],
        )
        self.assertEqual(
            tuple(item.target for item in batch.terminal_regular_before),
            (ROLE_TARGET,),
        )
        self.assertEqual(
            tuple(item.target for item in batch.terminal_regular_after),
            (ROLE_TARGET, secondary_target),
        )
        acted_regular_targets = {
            record.target
            for record in batch.records
            if record.is_regular()
            and record.action in {"create", "replace", "quarantine-replace"}
        }
        self.assertNotIn(ROLE_TARGET, acted_regular_targets)
        self.assertIn(secondary_target, acted_regular_targets)

    def test_v6_action_scoped_metadata_recovers_unchanged_regular_target(
        self,
    ) -> None:
        initial = self.root / "initial-release"
        write_release(initial, role_payload='name = "reviewer"\n')
        install(initial, self.home, SHA_A)
        next_release = self.root / "next-release"
        write_release(next_release, role_payload='name = "reviewer"\n')
        append_regular_link(
            next_release,
            target="agents/security-reviewer.toml",
            source="personal_codex/agents/security-reviewer.toml",
            payload='name = "security-reviewer"\n',
        )
        batch = self._retain_committed_batch(next_release, sha=SHA_B)
        legacy_payload, _legacy_gid = legacy_v6_writer_metadata_payload(batch)
        assert _legacy_gid is not None
        write_pending_metadata_payload(batch, legacy_payload)

        parsed = MODULE._load_pending_link_batch(self.home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, 6)
        self.assertNotIn(
            ROLE_TARGET,
            {
                record.target
                for record in parsed.records
                if record.is_regular()
                and record.action in {"create", "replace", "quarantine-replace"}
            },
        )

        install(next_release, self.home, SHA_B)

        self.assertEqual(
            (self.home / ROLE_TARGET).read_text(encoding="utf-8"),
            'name = "reviewer"\n',
        )
        self.assertEqual(
            (self.home / "agents" / "security-reviewer.toml").read_text(
                encoding="utf-8"
            ),
            'name = "security-reviewer"\n',
        )


if __name__ == "__main__":
    unittest.main()
