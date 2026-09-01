from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_personal_sync.py"
SPEC = importlib.util.spec_from_file_location(
    "codex_personal_sync_pending_agent_claim_compatibility",
    SCRIPT_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
ROLE_TARGET = PurePosixPath("agents/reviewer.toml")


def write_agent_release(root: Path) -> None:
    source = root / "personal_codex" / "agents" / "reviewer.toml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('name = "reviewer"\n', encoding="utf-8")
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": MODULE.PUBLIC_OWNER,
                "links": [
                    {
                        "source": "personal_codex/agents/reviewer.toml",
                        "target": ROLE_TARGET.as_posix(),
                        "kind": "file",
                        "owner": MODULE.PUBLIC_OWNER,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def install(root: Path, home: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, SHA_A, dry_run=False)


class PendingAgentClaimCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _retain_batch(
        self,
        home: Path,
        release: Path,
        *,
        committed: bool,
        legacy_symlink: bool,
    ) -> MODULE.PendingLinkBatch:
        real_clear = MODULE._clear_pending_link_pointer

        def retain_pointer(
            selected_home: Path,
            batch: MODULE.PendingLinkBatch,
            *,
            phase: str = "before",
        ) -> None:
            retained_phase = "after" if committed else "before"
            if phase == retained_phase:
                raise MODULE.SyncError("injected pending pointer retention")
            real_clear(selected_home, batch, phase=phase)

        expected_error = (
            "committed managed state but finalization failed"
            if committed
            else "rollback was incomplete"
        )
        with contextlib.ExitStack() as stack:
            if legacy_symlink:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_entry_materializes_regular_file",
                        return_value=False,
                    )
                )
            if not committed:
                stack.enter_context(
                    mock.patch.object(
                        MODULE,
                        "_publish_pending_commit_marker",
                        side_effect=MODULE.SyncError("injected precommit crash"),
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "_clear_pending_link_pointer",
                    side_effect=retain_pointer,
                )
            )
            with self.assertRaisesRegex(MODULE.SyncError, expected_error):
                install(release, home)
            batch = MODULE._load_pending_link_batch(home)
            self.assertIsNotNone(batch)
            assert batch is not None
            return batch

    def _downgrade_metadata(
        self,
        batch: MODULE.PendingLinkBatch,
        version: int,
    ) -> None:
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["version"] = version
        if version < 7:
            payload.pop("terminal_regular_before")
            payload.pop("terminal_regular_after")
        if version < 6:
            for raw_record in payload["records"]:
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
                for field in (
                    "regular_sha256",
                    "regular_size",
                    "regular_mode",
                    "regular_uid",
                    "regular_gid",
                    "regular_link_count",
                ):
                    planned.pop(field)
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_v4_v5_agent_symlink_claims_parse_and_recover(self) -> None:
        for version in (4, 5):
            for committed in (False, True):
                with self.subTest(version=version, committed=committed):
                    home = self.root / f"home-v{version}-{committed}"
                    release = self.root / f"release-v{version}-{committed}"
                    write_agent_release(release)
                    batch = self._retain_batch(
                        home,
                        release,
                        committed=committed,
                        legacy_symlink=True,
                    )
                    self._downgrade_metadata(batch, version)

                    parsed = MODULE._load_pending_link_batch(home)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertEqual(parsed.metadata_version, version)
                    self.assertIn(
                        ROLE_TARGET,
                        {
                            claim.target
                            for claim in parsed.claims_after
                            if claim.scope == "managed"
                        },
                    )

                    state, snapshot = MODULE._load_managed_state_with_snapshot(home)
                    with contextlib.redirect_stdout(io.StringIO()):
                        _state, _snapshot, recovered = (
                            MODULE._recover_pending_link_transaction(
                                home,
                                state,
                                snapshot,
                                dry_run=False,
                            )
                        )
                    self.assertTrue(recovered)
                    self.assertFalse(
                        os.path.lexists(MODULE._pending_link_pointer_path(home))
                    )
                    target = home / ROLE_TARGET
                    self.assertEqual(target.is_symlink(), committed)

    def test_v6_omits_regular_agent_claim_and_rejects_legacy_extra_claim(
        self,
    ) -> None:
        home = self.root / "home-v6"
        release = self.root / "release-v6"
        write_agent_release(release)
        batch = self._retain_batch(
            home,
            release,
            committed=True,
            legacy_symlink=False,
        )
        self._downgrade_metadata(batch, 6)

        parsed = MODULE._load_pending_link_batch(home)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.metadata_version, 6)
        self.assertNotIn(
            ROLE_TARGET,
            {
                claim.target
                for claim in parsed.claims_after
                if claim.scope == "managed"
            },
        )

        state_record = parsed.state_after_value.links[ROLE_TARGET]
        metadata = batch.batch_root / MODULE.PENDING_LINK_METADATA_NAME
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        claims_after = payload["claims_after"]
        claims_after.append(
            {
                "index": len(claims_after),
                "scope": "managed",
                "target": ROLE_TARGET.as_posix(),
                "kind": state_record.kind,
                "source": state_record.source.as_posix(),
                "owner": state_record.owner,
                "link_target": state_record.link_target,
                "release_sha": state_record.release_sha,
                "parent_identity": [0, 0],
                "link_identity": [0, 0],
                "evidence": (
                    f"pending/claims/after/{len(claims_after) - 1:08d}"
                ),
            }
        )
        metadata.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            MODULE.SyncError,
            "after claims do not exactly match state",
        ):
            MODULE._load_pending_link_batch(home)


if __name__ == "__main__":
    unittest.main()
