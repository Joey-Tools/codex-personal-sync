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
    "codex_personal_sync_regular_overlay_uninstall_status_regressions",
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
PUBLIC_PAYLOAD = 'provider = "public"\n'
PRIVATE_PAYLOAD = 'provider = "private"\n'


def write_regular_release(
    root: Path,
    *,
    owner: str = MODULE.PUBLIC_OWNER,
    payload: str,
    override: bool = False,
    base_sha: str | None = None,
) -> None:
    source = root / "personal_codex" / "agents" / "reviewer.toml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(payload, encoding="utf-8")
    entry: dict[str, object] = {
        "source": "personal_codex/agents/reviewer.toml",
        "target": ROLE_TARGET.as_posix(),
        "kind": "file",
        "owner": owner,
    }
    if override:
        entry["override"] = True
    manifest_payload: dict[str, object] = {
        "version": 1,
        "owner": owner,
        "links": [entry],
    }
    if base_sha is not None:
        manifest_payload["base_release"] = {
            "repo": "Joey-Tools/codex-toolbox",
            "sha": base_sha,
        }
    manifest = root / MODULE.MANIFEST_RELATIVE_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_payload) + "\n", encoding="utf-8")


def install(root: Path, home: Path, sha: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        MODULE.install_release_tree(root, home, sha, dry_run=False)


class RegularOverlayUninstallFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        public = self.root / "public"
        private = self.root / "private"
        write_regular_release(public, payload=PUBLIC_PAYLOAD)
        write_regular_release(
            private,
            owner="private",
            payload=PRIVATE_PAYLOAD,
            override=True,
            base_sha=SHA_A,
        )
        install(public, self.home, SHA_A)
        install(private, self.home, SHA_B)
        self.target = self.home / ROLE_TARGET

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_precommit_rollback_uses_regular_batch_finalizer(self) -> None:
        real_finalizer = MODULE._finalize_rolled_back_pending_batch
        real_verify_releases = MODULE._verify_install_release_identities
        failed = False

        def fail_after_pending_publication(
            home: Path,
            bindings,
            *,
            phase: str,
            verify_current: bool,
        ) -> None:
            nonlocal failed
            if phase == "before overlay uninstall" and not failed:
                failed = True
                raise MODULE.SyncError("injected precommit failure")
            real_verify_releases(
                home,
                bindings,
                phase=phase,
                verify_current=verify_current,
            )

        with (
            mock.patch.object(
                MODULE,
                "_verify_install_release_identities",
                side_effect=fail_after_pending_publication,
            ),
            mock.patch.object(
                MODULE,
                "_finalize_rolled_back_pending_batch",
                wraps=real_finalizer,
            ) as finalizer,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "injected precommit failure",
            ) as raised,
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(finalizer.call_count, 1, str(raised.exception))
        self.assertEqual(self.target.read_text(encoding="utf-8"), PRIVATE_PAYLOAD)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )
        state = MODULE._load_managed_state(self.home)
        self.assertEqual(state.owners["private"], SHA_B)
        self.assertEqual(state.links[ROLE_TARGET].owner, "private")

    def test_committed_cleanup_deferral_fails_closed_and_retries(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                return_value=False,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "committed regular-file evidence cleanup was deferred",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertFalse(
            os.path.lexists(MODULE._pending_link_pointer_path(self.home))
        )
        self.assertGreater(self.target.stat().st_nlink, 1)
        ticket_root = MODULE._pending_cleanup_index_path(self.home)
        self.assertTrue(any(ticket_root.glob("*.json")))

        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)

        self.assertEqual(self.target.read_text(encoding="utf-8"), PUBLIC_PAYLOAD)
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertEqual(
            MODULE._load_managed_state(self.home).owners,
            {MODULE.PUBLIC_OWNER: SHA_A},
        )

    def test_final_regular_target_is_verified_after_cleanup(self) -> None:
        real_cleanup = MODULE._try_cleanup_finalized_pending_batch

        def cleanup_then_tamper(
            home: Path,
            batch: MODULE.PendingLinkBatch,
        ) -> bool:
            cleaned = real_cleanup(home, batch)
            if cleaned:
                self.target.write_text("tampered = true\n", encoding="utf-8")
            return cleaned

        with (
            mock.patch.object(
                MODULE,
                "_try_cleanup_finalized_pending_batch",
                side_effect=cleanup_then_tamper,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                MODULE.SyncError,
                "final managed regular file changed",
            ),
        ):
            MODULE.uninstall_overlay(self.home, "private", dry_run=False)


class RegularStatusLedgerTests(unittest.TestCase):
    def test_status_rejects_missing_mandatory_regular_state_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = root / "release"
            write_regular_release(release, payload=PUBLIC_PAYLOAD)
            install(release, home, SHA_A)
            state = MODULE._load_managed_state(home)
            state.links.pop(ROLE_TARGET)
            MODULE._write_managed_state(home, state)
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                healthy = MODULE.status(home)

            self.assertFalse(healthy)
            self.assertIn(
                "regular file is missing its managed state claim",
                output.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
