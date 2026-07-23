from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "sync-toolbox.yml"
DOCUMENTATION_PATH = (
    REPOSITORY_ROOT / "docs" / "automation" / "sync-toolbox.md"
)
SYNTHETIC_ACCESS_TOKEN_ID = "access-a"
SYNTHETIC_ACCESS_TOKEN = "codex_synth_v1_access_a"


class SyncToolboxAutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")

    def _top_level_block(self, start: str, end: str) -> str:
        match = re.search(
            rf"(?ms)^{re.escape(start)}:\n(.*?)^{re.escape(end)}:\n",
            self.workflow,
        )
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def _step_run(self, name: str) -> str:
        lines = self.workflow.splitlines()
        marker = f"      - name: {name}"
        try:
            start = lines.index(marker)
        except ValueError as error:
            self.fail(f"workflow step is missing: {name}")
            raise AssertionError from error
        run_index = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index] == "        run: |"
            ),
            None,
        )
        self.assertIsNotNone(run_index)
        assert run_index is not None
        body: list[str] = []
        for line in lines[run_index + 1 :]:
            if line.startswith("      - name: "):
                break
            if line.startswith("          "):
                body.append(line[10:])
            elif not line:
                body.append("")
            else:
                break
        return "\n".join(body).rstrip() + "\n"

    def test_trigger_excludes_untrusted_pull_request_events(self) -> None:
        trigger = self._top_level_block("on", "permissions")
        self.assertRegex(trigger, r"(?m)^  push:\n    branches:\n      - master$")
        self.assertRegex(trigger, r"(?m)^  workflow_dispatch:$")
        self.assertNotIn("pull_request", trigger)
        self.assertNotIn("pull_request_target", trigger)

    def test_source_repository_permissions_are_read_only(self) -> None:
        permissions = self._top_level_block("permissions", "concurrency")
        self.assertEqual(permissions.strip(), "contents: read")
        self.assertNotRegex(
            permissions,
            r"\b(?:actions|checks|issues|pull-requests|statuses):\s*write\b",
        )

    def test_missing_secret_fails_before_target_checkout(self) -> None:
        validation_index = self.workflow.index(
            "- name: Validate sync credential"
        )
        target_checkout_index = self.workflow.index(
            "- name: Check out toolbox master"
        )
        self.assertLess(validation_index, target_checkout_index)
        self.assertIn(
            "token: ${{ secrets.CODEX_TOOLBOX_SYNC_TOKEN }}",
            self.workflow,
        )
        self.assertNotIn("secrets.GITHUB_TOKEN", self.workflow)
        self.assertNotIn("github.token", self.workflow)
        target_checkout = re.search(
            r"(?ms)- name: Check out toolbox master.*?"
            r"(?=^      - name: )",
            self.workflow,
        )
        self.assertIsNotNone(target_checkout)
        assert target_checkout is not None
        self.assertIn("persist-credentials: false", target_checkout.group(0))
        self.assertNotIn("persist-credentials: true", self.workflow)
        script = self._step_run("Validate sync credential")
        environment = dict(os.environ)
        environment["CODEX_TOOLBOX_SYNC_TOKEN"] = ""
        failed = subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", script],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Missing CODEX_TOOLBOX_SYNC_TOKEN", failed.stdout)
        self.assertIn("Contents read/write", failed.stdout)
        self.assertIn("Pull requests read/write", failed.stdout)

        self.assertEqual(SYNTHETIC_ACCESS_TOKEN_ID, "access-a")
        environment["CODEX_TOOLBOX_SYNC_TOKEN"] = SYNTHETIC_ACCESS_TOKEN
        passed = subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", script],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertNotIn(
            SYNTHETIC_ACCESS_TOKEN,
            passed.stdout + passed.stderr,
        )

    def test_generator_uses_exact_commit_explicit_target_then_check(self) -> None:
        script = self._step_run("Generate and verify exact toolbox mirror")
        self.assertIn(
            'observed_sha="$(git -C "${CANONICAL_ROOT}" rev-parse --verify HEAD)"',
            script,
        )
        self.assertIn(
            'if [[ "${observed_sha}" != "${CANONICAL_SHA}" ]]',
            script,
        )
        generate = (
            'sync_canonical_mirrors.py" generate \\\n'
            '  --target-root "${TARGET_ROOT}" \\\n'
            '  --mirror "${MIRROR_NAME}" \\\n'
            '  --source-commit "${CANONICAL_SHA}"'
        )
        check = (
            'sync_canonical_mirrors.py" check \\\n'
            '  --target-root "${TARGET_ROOT}" \\\n'
            '  --mirror "${MIRROR_NAME}"'
        )
        self.assertIn(generate, script)
        self.assertIn(check, script)
        self.assertLess(script.index(generate), script.index(check))
        self.assertIn(
            "python3 \"${CANONICAL_ROOT}/scripts/sync_canonical_mirrors.py\" "
            "refresh-lock --check",
            self._step_run("Verify canonical master and source lock"),
        )

    def test_branch_push_and_pr_are_scoped_without_force_or_master_write(
        self,
    ) -> None:
        self.assertIn(
            "SYNC_BRANCH: automation/canonical-personal-sync",
            self.workflow,
        )
        self.assertIn(
            '"HEAD:refs/heads/${SYNC_BRANCH}"',
            self.workflow,
        )
        self.assertIn(
            "GIT_CONFIG_KEY_0=http.https://github.com/.extraheader",
            self.workflow,
        )
        self.assertIn(
            'GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic_token}"',
            self.workflow,
        )
        self.assertNotRegex(self.workflow, r"(?m)^\s+.*git .*push .*--force")
        self.assertNotRegex(
            self.workflow,
            r"(?m)^\s+.*git .*push .*refs/heads/master",
        )
        self.assertIn(
            '"origin/${TARGET_BASE}...HEAD"',
            self.workflow,
        )
        self.assertIn(
            '.mirrors[$mirror].files[], "generated-sync-source-lock.json"',
            self.workflow,
        )
        self.assertIn(
            'jq -j \'.[] | ., "\\u0000"\'',
            self.workflow,
        )
        self.assertGreaterEqual(
            self.workflow.count("assert_allowed_paths"),
            5,
        )
        self.assertIn(
            '--base "${TARGET_BASE}"',
            self.workflow,
        )
        self.assertIn(
            '--head "${SYNC_BRANCH}"',
            self.workflow,
        )

    def test_pr_body_and_update_require_automation_ownership(self) -> None:
        marker = "<!-- codex-personal-sync-toolbox-automation -->"
        self.assertGreaterEqual(self.workflow.count(marker), 2)
        self.assertIn(
            "The existing PR is cross-repository, has the wrong owner, or "
            "lacks the canonical automation marker",
            self.workflow,
        )
        self.assertGreaterEqual(
            self.workflow.count(".isCrossRepository == false"),
            2,
        )
        self.assertGreaterEqual(
            self.workflow.count(".headRepositoryOwner.login == $owner"),
            2,
        )
        publish = self._step_run("Publish or update toolbox sync PR")
        self.assertIn("generate --target-root <explicit-toolbox-checkout>", publish)
        self.assertIn("check --target-root <explicit-toolbox-checkout>", publish)
        self.assertIn("never writes directly", publish)
        self.assertIn('gh pr edit "${EXISTING_PR}"', publish)
        self.assertIn("gh pr create", publish)
        self.assertIn("gh pr view", publish)
        self.assertIn(".state == \"OPEN\"", publish)

    def test_allowed_path_stream_is_nul_delimited(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        completed = subprocess.run(
            [jq, "-j", '.[] | ., "\\u0000"'],
            input=b'["one path","two\\nlines"]',
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.split(b"\0"),
            [b"one path", b"two\nlines", b""],
        )

    def test_documented_secret_interface_is_least_privilege_and_explicit(
        self,
    ) -> None:
        documentation = re.sub(r"\s+", " ", self.documentation)
        self.assertIn("`CODEX_TOOLBOX_SYNC_TOKEN`", documentation)
        self.assertIn("Joey-Tools/codex-toolbox", documentation)
        self.assertIn("Contents: Read and write", documentation)
        self.assertIn("Pull requests: Read and write", documentation)
        self.assertIn("Metadata: Read-only", documentation)
        self.assertIn(
            "does not create or install persistent credentials",
            documentation,
        )
        self.assertIn("does not run for pull-request events", documentation)


if __name__ == "__main__":
    unittest.main()
