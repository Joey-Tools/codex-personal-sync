from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "sync-toolbox.yml"
DOCUMENTATION_PATH = REPOSITORY_ROOT / "docs" / "automation" / "sync-toolbox.md"
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

    def test_canonical_checkout_retains_history_for_receipt_validation(self) -> None:
        canonical_checkout = re.search(
            r"(?ms)- name: Check out exact canonical commit.*?"
            r"(?=^      - name: )",
            self.workflow,
        )
        self.assertIsNotNone(canonical_checkout)
        assert canonical_checkout is not None
        checkout = canonical_checkout.group(0)
        self.assertIn("ref: ${{ github.sha }}", checkout)
        self.assertIn("fetch-depth: 0", checkout)
        self.assertIn("persist-credentials: false", checkout)

    def test_missing_secret_fails_before_target_checkout(self) -> None:
        validation_index = self.workflow.index("- name: Validate sync credential")
        target_checkout_index = self.workflow.index("- name: Check out toolbox master")
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
        commit_script = self._step_run("Commit scoped generated changes")
        self.assertNotIn('git -C "${TARGET_ROOT}" add', commit_script)
        self.assertIn(
            'git -C "${TARGET_ROOT}" hash-object \\\n'
            '        --no-filters -w --stdin <"${target_path}"',
            commit_script,
        )
        self.assertIn(
            'git -C "${TARGET_ROOT}" update-index --add --cacheinfo',
            commit_script,
        )
        self.assertIn(
            'git -C "${TARGET_ROOT}" update-index --force-remove -- "${path}"',
            commit_script,
        )
        self.assertIn(check, commit_script)
        self.assertGreater(
            commit_script.index(check),
            commit_script.index("commit-tree"),
        )
        self.assertIn(
            'python3 "${CANONICAL_ROOT}/scripts/sync_canonical_mirrors.py" '
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
            "managed-paths",
            self.workflow,
        )
        self.assertIn(
            "clean retired paths proven by the prior committed",
            self.workflow,
        )
        prepare = self._step_run("Prepare scoped sync branch")
        detached_base = (
            'git -C "${TARGET_ROOT}" switch --detach \\\n'
            '  "refs/remotes/origin/${TARGET_BASE}"'
        )
        managed_paths = (
            'sync_canonical_mirrors.py" \\\n'
            "  managed-paths \\\n"
            '  --target-root "${TARGET_ROOT}" \\\n'
            '  --mirror "${MIRROR_NAME}" >"${allowed_paths_file}"'
        )
        self.assertIn(detached_base, prepare)
        self.assertIn(managed_paths, prepare)
        self.assertLess(prepare.index(detached_base), prepare.index(managed_paths))
        self.assertLess(prepare.index(managed_paths), prepare.index("remote_branch="))
        self.assertIn(
            "jq -j '.[] | ., \"\\u0000\"'",
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
        self.assertIn(
            'echo "remote_branch_present=${remote_branch_present}"',
            prepare,
        )
        self.assertIn(
            'echo "remote_branch_sha=${remote_branch_sha}"',
            prepare,
        )
        self.assertIn(
            '"${fetched_remote_branch_sha}" != "${remote_branch_sha}"',
            prepare,
        )
        push = self._step_run("Push scoped sync branch")
        self.assertNotIn("awk ", push)
        self.assertIn(
            '"${REMOTE_REF_SHA}" != "${PREPARED_REMOTE_BRANCH_SHA}"',
            push,
        )
        self.assertIn(
            '"${REMOTE_REF_SHA}" == "${DESIRED_HEAD_SHA}"',
            push,
        )

    def test_pr_body_and_update_require_automation_ownership(self) -> None:
        marker = "<!-- codex-personal-sync-toolbox-automation -->"
        self.assertGreaterEqual(self.workflow.count(marker), 2)
        self.assertIn(
            "The existing PR does not exactly match the prepared repository, "
            "base, branch, head commit, owner, and canonical automation marker",
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
        self.assertIn("query_sync_prs", publish)
        self.assertIn("require_target_base_sha", publish)
        self.assertIn("require_sync_branch_sha", publish)
        self.assertIn('published_pr_number="${created_pr_url#', publish)
        self.assertIn('.state == "OPEN"', publish)
        self.assertIn(".baseRefOid == $base_oid", publish)
        self.assertIn(".headRefOid == $head_oid", publish)
        close = self._step_run("Close clean owned toolbox sync PR")
        self.assertIn('gh pr view "${EXISTING_PR}"', close)
        self.assertIn('gh pr close "${EXISTING_PR}"', close)
        self.assertNotIn("--delete-branch", close)
        self.assertIn(".baseRefOid == $base_oid", close)
        self.assertIn(".headRefOid == $head_oid", close)

    def test_pr_create_binds_live_head_and_returned_number(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-create-pr."
        ) as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/usr/bin/python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "log = Path(os.environ['FAKE_GH_LOG'])\n"
                "with log.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "if args[:1] == ['api']:\n"
                "    if '/heads/master' in args[1]:\n"
                "        print(os.environ['LIVE_BASE_SHA'])\n"
                "    elif Path(os.environ['CREATED_STATE']).exists():\n"
                "        print(os.environ['LIVE_HEAD_AFTER_CREATE'])\n"
                "    else:\n"
                "        print(os.environ['LIVE_HEAD_SHA'])\n"
                "elif args[:2] == ['pr', 'list']:\n"
                "    if Path(os.environ['CREATED_STATE']).exists():\n"
                "        print(os.environ['AFTER_PR_PAYLOAD'])\n"
                "    else:\n"
                "        print('[]')\n"
                "elif args[:2] == ['pr', 'create']:\n"
                "    Path(os.environ['CREATED_STATE']).write_text('created', encoding='ascii')\n"
                "    print(os.environ['CREATED_PR_URL'])\n"
                "elif args[:2] == ['pr', 'view']:\n"
                "    if args[2] != os.environ['ACTUAL_CREATED_NUMBER']:\n"
                "        raise SystemExit(1)\n"
                "    payload = json.loads(os.environ['RECOVERY_PR_PAYLOAD'])\n"
                "    if Path(os.environ['CLOSED_STATE']).exists():\n"
                "        payload['state'] = 'CLOSED'\n"
                "    print(json.dumps(payload))\n"
                "elif args[:2] == ['pr', 'close']:\n"
                "    if args[2] != os.environ['ACTUAL_CREATED_NUMBER']:\n"
                "        raise SystemExit(1)\n"
                "    Path(os.environ['CLOSED_STATE']).write_text('closed', encoding='ascii')\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            desired_sha = "5" * 40
            prepared_base_sha = "4" * 40
            exact_payload = [
                {
                    "number": 29,
                    "body": ("<!-- codex-personal-sync-toolbox-automation -->\n"),
                    "state": "OPEN",
                    "baseRefName": "master",
                    "baseRefOid": prepared_base_sha,
                    "headRefName": "automation/canonical-personal-sync",
                    "headRefOid": desired_sha,
                    "headRepositoryOwner": {"login": "Joey-Tools"},
                    "isCrossRepository": False,
                }
            ]
            drifted_payload = [{**exact_payload[0], "headRefOid": "6" * 40}]
            recovery_payload = {
                key: value
                for key, value in exact_payload[0].items()
                if key not in {"baseRefOid", "headRefOid"}
            }
            cases = (
                (
                    "exact",
                    desired_sha,
                    desired_sha,
                    "https://github.com/Joey-Tools/codex-toolbox/pull/29",
                    exact_payload,
                    0,
                    True,
                    False,
                ),
                (
                    "head drift before create",
                    "6" * 40,
                    "6" * 40,
                    "https://github.com/Joey-Tools/codex-toolbox/pull/29",
                    drifted_payload,
                    1,
                    False,
                    False,
                ),
                (
                    "head drift after create",
                    desired_sha,
                    "6" * 40,
                    "https://github.com/Joey-Tools/codex-toolbox/pull/29",
                    drifted_payload,
                    1,
                    True,
                    True,
                ),
                (
                    "returned number mismatch",
                    desired_sha,
                    desired_sha,
                    "https://github.com/Joey-Tools/codex-toolbox/pull/30",
                    exact_payload,
                    1,
                    True,
                    False,
                ),
            )
            for (
                name,
                live_head_sha,
                live_head_after_create,
                created_pr_url,
                after_pr_payload,
                expected_failure,
                should_create,
                should_close,
            ) in cases:
                with self.subTest(name=name):
                    gh_log = root / f"gh-log-{name.replace(' ', '-')}"
                    created_state = root / f"created-{name.replace(' ', '-')}"
                    closed_state = root / f"closed-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "ACTUAL_CREATED_NUMBER": "29",
                        "AFTER_PR_PAYLOAD": json.dumps(after_pr_payload),
                        "CANONICAL_SHA": "3" * 40,
                        "CLOSED_STATE": str(closed_state),
                        "CREATED_PR_URL": created_pr_url,
                        "CREATED_STATE": str(created_state),
                        "DESIRED_HEAD_SHA": desired_sha,
                        "EXISTING_PR": "",
                        "FAKE_GH_LOG": str(gh_log),
                        "GH_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "GITHUB_REPOSITORY": "Joey-Tools/codex-personal-sync",
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_WORKFLOW": "Sync toolbox mirror",
                        "LIVE_BASE_SHA": prepared_base_sha,
                        "LIVE_HEAD_AFTER_CREATE": live_head_after_create,
                        "LIVE_HEAD_SHA": live_head_sha,
                        "MIRROR_NAME": "toolbox",
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "PREPARED_TARGET_BASE_SHA": prepared_base_sha,
                        "RECOVERY_PR_PAYLOAD": json.dumps(recovery_payload),
                        "RUNNER_TEMP": str(root),
                        "SYNC_BRANCH": "automation/canonical-personal-sync",
                        "TARGET_BASE": "master",
                        "TARGET_OWNER": "Joey-Tools",
                        "TARGET_REPOSITORY": "Joey-Tools/codex-toolbox",
                    }
                    completed = subprocess.run(
                        [
                            "/bin/bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            self._step_run("Publish or update toolbox sync PR"),
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if expected_failure:
                        self.assertNotEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                    else:
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                    commands = gh_log.read_text(encoding="utf-8")
                    self.assertEqual("pr create" in commands, should_create)
                    self.assertEqual("pr close" in commands, should_close)
                    self.assertEqual(closed_state.exists(), should_close)
                    if name == "head drift before create":
                        self.assertIn("Sync branch advanced", completed.stdout)
                    if name == "head drift after create":
                        self.assertIn("Rejected created sync PR", completed.stdout)
                    if name == "returned number mismatch":
                        self.assertIn("Sync PR recovery required", completed.stdout)

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

    def test_commit_step_stages_raw_bytes_without_running_clean_filter(
        self,
    ) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-filter-free."
        ) as temporary_directory:
            root = Path(temporary_directory)
            canonical_root = root / "canonical"
            target_root = root / "toolbox"
            runner_temp = root / "runner"
            canonical_root.mkdir()
            target_root.mkdir()
            runner_temp.mkdir()

            def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git", "-C", str(target_root), *arguments],
                    check=check,
                    capture_output=True,
                    env={
                        **os.environ,
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_TERMINAL_PROMPT": "0",
                        "LC_ALL": "C",
                    },
                )

            git("init", "-q")
            git("switch", "-q", "-c", "master")
            git("config", "user.name", "Toolbox Fixture")
            git("config", "user.email", "toolbox@example.invalid")
            git("config", "commit.gpgsign", "false")
            (target_root / "scripts").mkdir()
            engine_path = target_root / "scripts" / "engine.py"
            receipt_path = target_root / "generated-sync-source-lock.json"
            obsolete_path = target_root / "obsolete.txt"
            engine_path.write_bytes(b"old engine\n")
            receipt_path.write_bytes(b'{"old":true}\n')
            obsolete_path.write_bytes(b"obsolete\n")
            (target_root / ".gitattributes").write_text(
                "scripts/engine.py filter=fixture\n"
                "generated-sync-source-lock.json filter=fixture\n"
                "obsolete.txt filter=fixture\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "--no-gpg-sign", "-q", "-m", "fixture base")
            git("update-ref", "refs/remotes/origin/master", "HEAD")

            filter_marker = root / "clean-filter-ran"
            filter_script = root / "clean-filter.sh"
            filter_script.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "printf 'invoked\\n' >>\"${FILTER_MARKER:?}\"\n"
                "exec /usr/bin/sed 's/canonical/filtered/g'\n",
                encoding="utf-8",
            )
            filter_script.chmod(0o755)
            git("config", "filter.fixture.clean", str(filter_script))
            git("config", "filter.fixture.required", "true")

            canonical_engine = b"canonical engine bytes\n"
            canonical_receipt = b'{"canonical":"receipt"}\n'
            engine_path.write_bytes(canonical_engine)
            engine_path.chmod(0o755)
            receipt_path.write_bytes(canonical_receipt)
            obsolete_path.unlink()

            expected_root = canonical_root / "expected"
            expected_root.mkdir()
            (expected_root / "engine.py").write_bytes(canonical_engine)
            (expected_root / "receipt.json").write_bytes(canonical_receipt)
            generator_path = canonical_root / "scripts" / "sync_canonical_mirrors.py"
            generator_path.parent.mkdir()
            generator_path.write_text(
                "from pathlib import Path\n"
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('command')\n"
                "parser.add_argument('--target-root', required=True)\n"
                "parser.add_argument('--mirror', required=True)\n"
                "arguments = parser.parse_args()\n"
                "if arguments.command != 'check':\n"
                "    raise SystemExit('unexpected command')\n"
                "root = Path(arguments.target_root)\n"
                "expected = Path(__file__).resolve().parents[1] / 'expected'\n"
                "if (root / 'scripts/engine.py').read_bytes() != "
                "(expected / 'engine.py').read_bytes():\n"
                "    raise SystemExit('engine mismatch')\n"
                "if (root / 'generated-sync-source-lock.json').read_bytes() != "
                "(expected / 'receipt.json').read_bytes():\n"
                "    raise SystemExit('receipt mismatch')\n",
                encoding="utf-8",
            )

            allowed_paths = [
                "generated-sync-source-lock.json",
                "obsolete.txt",
                "scripts/engine.py",
            ]
            (runner_temp / "toolbox-sync-allowed-paths.json").write_text(
                json.dumps(allowed_paths),
                encoding="utf-8",
            )
            github_output = root / "github-output"
            environment = {
                **os.environ,
                "CANONICAL_ROOT": str(canonical_root),
                "CANONICAL_SHA": "1" * 40,
                "FILTER_MARKER": str(filter_marker),
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_REPOSITORY": "Joey-Tools/codex-personal-sync",
                "MIRROR_NAME": "toolbox",
                "PREPARED_REMOTE_BRANCH_PRESENT": "false",
                "PREPARED_REMOTE_BRANCH_SHA": "",
                "RUNNER_TEMP": str(runner_temp),
                "TARGET_BASE": "master",
                "TARGET_ROOT": str(target_root),
            }
            completed = subprocess.run(
                [
                    "/bin/bash",
                    "-euo",
                    "pipefail",
                    "-c",
                    self._step_run("Commit scoped generated changes"),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertFalse(filter_marker.exists())
            self.assertEqual(
                git("show", "HEAD:scripts/engine.py").stdout,
                canonical_engine,
            )
            self.assertEqual(
                git(
                    "show",
                    "HEAD:generated-sync-source-lock.json",
                ).stdout,
                canonical_receipt,
            )
            self.assertEqual(
                git(
                    "ls-tree",
                    "HEAD",
                    "scripts/engine.py",
                ).stdout.split(maxsplit=1)[0],
                b"100755",
            )
            self.assertNotEqual(
                git(
                    "cat-file",
                    "-e",
                    "HEAD:obsolete.txt",
                    check=False,
                ).returncode,
                0,
            )
            self.assertIn(
                "pr_has_changes=true",
                github_output.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "branch_needs_update=true",
                github_output.read_text(encoding="utf-8"),
            )

    def test_clean_stale_branch_still_requires_a_branch_update(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-stale-clean."
        ) as temporary_directory:
            root = Path(temporary_directory)
            canonical_root = root / "canonical"
            target_root = root / "toolbox"
            runner_temp = root / "runner"
            canonical_root.mkdir()
            target_root.mkdir()
            runner_temp.mkdir()

            def git(*arguments: str) -> bytes:
                return subprocess.run(
                    ["git", "-C", str(target_root), *arguments],
                    check=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_TERMINAL_PROMPT": "0",
                        "LC_ALL": "C",
                    },
                ).stdout

            git("init", "-q")
            git("switch", "-q", "-c", "master")
            git("config", "user.name", "Toolbox Fixture")
            git("config", "user.email", "toolbox@example.invalid")
            git("config", "commit.gpgsign", "false")
            (target_root / "README.md").write_text(
                "# Fixture\n",
                encoding="utf-8",
            )
            git("add", "-A")
            git("commit", "--no-gpg-sign", "-q", "-m", "base")
            base_sha = git("rev-parse", "HEAD").decode("ascii").strip()
            git("update-ref", "refs/remotes/origin/master", base_sha)
            git("switch", "-q", "-c", "automation/canonical-personal-sync")
            stale_path = target_root / "scripts" / "engine.py"
            stale_path.parent.mkdir()
            stale_path.write_text("stale\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "--no-gpg-sign", "-q", "-m", "stale mirror")
            stale_sha = git("rev-parse", "HEAD").decode("ascii").strip()
            stale_path.unlink()
            git("add", "-A")
            git("commit", "--no-gpg-sign", "-q", "-m", "reconcile mirror")
            clean_sha = git("rev-parse", "HEAD").decode("ascii").strip()

            generator_path = canonical_root / "scripts" / "sync_canonical_mirrors.py"
            generator_path.parent.mkdir()
            generator_path.write_text(
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('command')\n"
                "parser.add_argument('--target-root', required=True)\n"
                "parser.add_argument('--mirror', required=True)\n"
                "arguments = parser.parse_args()\n"
                "if arguments.command != 'check':\n"
                "    raise SystemExit('unexpected command')\n",
                encoding="utf-8",
            )
            (runner_temp / "toolbox-sync-allowed-paths.json").write_text(
                json.dumps(["scripts/engine.py"]),
                encoding="utf-8",
            )
            github_output = root / "github-output"
            environment = {
                **os.environ,
                "CANONICAL_ROOT": str(canonical_root),
                "CANONICAL_SHA": "1" * 40,
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_REPOSITORY": "Joey-Tools/codex-personal-sync",
                "MIRROR_NAME": "toolbox",
                "PREPARED_REMOTE_BRANCH_PRESENT": "true",
                "PREPARED_REMOTE_BRANCH_SHA": stale_sha,
                "RUNNER_TEMP": str(runner_temp),
                "TARGET_BASE": "master",
                "TARGET_ROOT": str(target_root),
            }
            completed = subprocess.run(
                [
                    "/bin/bash",
                    "-euo",
                    "pipefail",
                    "-c",
                    self._step_run("Commit scoped generated changes"),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            outputs = github_output.read_text(encoding="utf-8")
            self.assertIn("pr_has_changes=false", outputs)
            self.assertIn("branch_needs_update=true", outputs)
            self.assertIn(f"head_sha={clean_sha}", outputs)

    def test_prepare_rejects_ambiguous_or_mismatched_remote_branch(
        self,
    ) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-prepare-remote."
        ) as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            canonical_root = root / "canonical"
            target_root = root / "toolbox"
            runner_temp = root / "runner"
            fake_bin.mkdir()
            canonical_root.mkdir()
            target_root.mkdir()
            runner_temp.mkdir()
            (canonical_root / "sync-source-lock.json").write_text(
                json.dumps(
                    {
                        "mirrors": {
                            "toolbox": {
                                "repository": "Joey-Tools/codex-toolbox",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/usr/bin/python3\n"
                "import os\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['FAKE_GIT_LOG'], 'a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "if 'ls-remote' in args:\n"
                "    reference = args[-1]\n"
                "    if reference.endswith(os.environ['SYNC_BRANCH']):\n"
                "        sys.stdout.write(os.environ.get('REMOTE_BRANCH_RECORD', ''))\n"
                "elif 'rev-parse' in args:\n"
                "    reference = args[-1]\n"
                "    if reference.endswith(os.environ['SYNC_BRANCH']):\n"
                "        print(os.environ['FETCHED_BRANCH_SHA'])\n"
                "    else:\n"
                "        print(os.environ['BASE_SHA'])\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\nprintf '[]\\n'\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            fake_base64 = fake_bin / "base64"
            fake_base64.write_text(
                "#!/bin/sh\ncat\n",
                encoding="utf-8",
            )
            fake_base64.chmod(0o755)

            branch_sha = "a" * 40
            base_sha = "b" * 40
            cases = (
                (
                    "duplicate records",
                    (
                        f"{branch_sha}\trefs/heads/"
                        "automation/canonical-personal-sync\n"
                        f"{branch_sha}\trefs/heads/"
                        "automation/canonical-personal-sync\n"
                    ),
                    branch_sha,
                    "Ambiguous sync branch",
                ),
                (
                    "fetch mismatch",
                    (f"{branch_sha}\trefs/heads/automation/canonical-personal-sync\n"),
                    "c" * 40,
                    "Sync branch fetch mismatch",
                ),
                (
                    "exact record",
                    (f"{branch_sha}\trefs/heads/automation/canonical-personal-sync\n"),
                    branch_sha,
                    None,
                ),
            )
            for name, remote_record, fetched_sha, expected_error in cases:
                with self.subTest(name=name):
                    github_output = root / f"github-output-{name.replace(' ', '-')}"
                    git_log = root / f"git-log-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "BASE_SHA": base_sha,
                        "CANONICAL_ROOT": str(canonical_root),
                        "CODEX_TOOLBOX_SYNC_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "FAKE_GIT_LOG": str(git_log),
                        "FETCHED_BRANCH_SHA": fetched_sha,
                        "GITHUB_OUTPUT": str(github_output),
                        "MIRROR_NAME": "toolbox",
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "REMOTE_BRANCH_RECORD": remote_record,
                        "RUNNER_TEMP": str(runner_temp),
                        "SYNC_BRANCH": "automation/canonical-personal-sync",
                        "TARGET_BASE": "master",
                        "TARGET_OWNER": "Joey-Tools",
                        "TARGET_REPOSITORY": "Joey-Tools/codex-toolbox",
                        "TARGET_ROOT": str(target_root),
                    }
                    completed = subprocess.run(
                        [
                            "/bin/bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            self._step_run("Prepare scoped sync branch"),
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if expected_error is not None:
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertIn(
                            expected_error,
                            completed.stdout + completed.stderr,
                        )
                        self.assertFalse(github_output.exists())
                    else:
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                        outputs = github_output.read_text(encoding="utf-8")
                        self.assertIn(f"target_base_sha={base_sha}", outputs)
                        self.assertIn("remote_branch_present=true", outputs)
                        self.assertIn(
                            f"remote_branch_sha={branch_sha}",
                            outputs,
                        )

    def test_push_revalidates_remote_state_without_force(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-push-remote."
        ) as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/usr/bin/python3\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "log = Path(os.environ['FAKE_GIT_LOG'])\n"
                "with log.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "state = Path(os.environ['REMOTE_BRANCH_STATE'])\n"
                "if 'ls-remote' in args:\n"
                "    reference = args[-1]\n"
                "    if reference.endswith('/master'):\n"
                "        print(f\"{os.environ['BASE_SHA']}\\t{reference}\")\n"
                "    else:\n"
                "        value = state.read_text(encoding='ascii')\n"
                "        if value != 'absent':\n"
                "            if os.environ.get('DUPLICATE_BRANCH') == '1':\n"
                "                print(f'{value}\\t{reference}')\n"
                "            print(f'{value}\\t{reference}')\n"
                "elif 'push' in args:\n"
                "    state.write_text(os.environ['DESIRED_HEAD_SHA'], encoding='ascii')\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_base64 = fake_bin / "base64"
            fake_base64.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            fake_base64.chmod(0o755)

            base_sha = "1" * 40
            prepared_sha = "2" * 40
            desired_sha = "3" * 40
            cases = (
                (
                    "matching prepared state",
                    prepared_sha,
                    "true",
                    prepared_sha,
                    "0",
                    0,
                    True,
                    "",
                ),
                (
                    "same desired no-op",
                    desired_sha,
                    "true",
                    prepared_sha,
                    "0",
                    0,
                    False,
                    "already points",
                ),
                (
                    "absent branch creation",
                    "absent",
                    "false",
                    "",
                    "0",
                    0,
                    True,
                    "",
                ),
                (
                    "remote drift",
                    "4" * 40,
                    "true",
                    prepared_sha,
                    "0",
                    1,
                    False,
                    "Sync branch advanced",
                ),
                (
                    "branch appeared",
                    "4" * 40,
                    "false",
                    "",
                    "0",
                    1,
                    False,
                    "Sync branch appeared",
                ),
                (
                    "duplicate branch records",
                    prepared_sha,
                    "true",
                    prepared_sha,
                    "1",
                    1,
                    False,
                    "Ambiguous remote ref",
                ),
            )
            for (
                name,
                live_sha,
                prepared_present,
                prepared_value,
                duplicate,
                expected_failure,
                should_push,
                expected_message,
            ) in cases:
                with self.subTest(name=name):
                    state = root / f"state-{name.replace(' ', '-')}"
                    state.write_text(live_sha, encoding="ascii")
                    git_log = root / f"log-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "BASE_SHA": base_sha,
                        "CODEX_TOOLBOX_SYNC_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "DESIRED_HEAD_SHA": desired_sha,
                        "DUPLICATE_BRANCH": duplicate,
                        "FAKE_GIT_LOG": str(git_log),
                        "PATH": f"{fake_bin}:/usr/bin:/bin",
                        "PREPARED_REMOTE_BRANCH_PRESENT": prepared_present,
                        "PREPARED_REMOTE_BRANCH_SHA": prepared_value,
                        "PREPARED_TARGET_BASE_SHA": base_sha,
                        "REMOTE_BRANCH_STATE": str(state),
                        "SYNC_BRANCH": "automation/canonical-personal-sync",
                        "TARGET_BASE": "master",
                        "TARGET_ROOT": str(root / "toolbox"),
                    }
                    completed = subprocess.run(
                        [
                            "/bin/bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            self._step_run("Push scoped sync branch"),
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if expected_failure:
                        self.assertNotEqual(completed.returncode, 0)
                    else:
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                        self.assertEqual(
                            state.read_text(encoding="ascii"),
                            desired_sha,
                        )
                    if expected_message:
                        self.assertIn(
                            expected_message,
                            completed.stdout + completed.stderr,
                        )
                    commands = git_log.read_text(encoding="utf-8")
                    push_line = (
                        "push origin HEAD:refs/heads/automation/canonical-personal-sync"
                    )
                    self.assertEqual(push_line in commands, should_push)
                    self.assertNotIn("--force", commands)
                    self.assertNotIn(
                        "refs/heads/master",
                        "\n".join(
                            line
                            for line in commands.splitlines()
                            if " push " in f" {line} "
                        ),
                    )

    def test_clean_pr_close_requires_exact_live_ownership_and_head(
        self,
    ) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-close-pr."
        ) as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/usr/bin/python3\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "with Path(os.environ['FAKE_GH_LOG']).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "if args[:2] == ['pr', 'view']:\n"
                "    print(os.environ['PR_PAYLOAD'])\n"
                "elif args[:1] == ['api']:\n"
                "    if '/heads/master' in args[1]:\n"
                "        print(os.environ['LIVE_BASE_SHA'])\n"
                "    else:\n"
                "        print(os.environ['LIVE_HEAD_SHA'])\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            desired_sha = "5" * 40
            prepared_base_sha = "4" * 40
            exact_payload = {
                "number": 17,
                "body": "<!-- codex-personal-sync-toolbox-automation -->\n",
                "state": "OPEN",
                "baseRefName": "master",
                "baseRefOid": prepared_base_sha,
                "headRefName": "automation/canonical-personal-sync",
                "headRefOid": desired_sha,
                "headRepositoryOwner": {"login": "Joey-Tools"},
                "isCrossRepository": False,
            }
            cases = (
                (
                    "exact",
                    exact_payload,
                    prepared_base_sha,
                    desired_sha,
                    0,
                    True,
                ),
                (
                    "head drift",
                    {**exact_payload, "headRefOid": "6" * 40},
                    prepared_base_sha,
                    desired_sha,
                    1,
                    False,
                ),
                (
                    "owner drift",
                    {
                        **exact_payload,
                        "headRepositoryOwner": {"login": "someone-else"},
                    },
                    prepared_base_sha,
                    desired_sha,
                    1,
                    False,
                ),
                (
                    "pr base drift",
                    {**exact_payload, "baseRefOid": "7" * 40},
                    prepared_base_sha,
                    desired_sha,
                    1,
                    False,
                ),
                (
                    "live base drift",
                    exact_payload,
                    "8" * 40,
                    desired_sha,
                    1,
                    False,
                ),
                (
                    "live head drift",
                    exact_payload,
                    prepared_base_sha,
                    "9" * 40,
                    1,
                    False,
                ),
            )
            for (
                name,
                payload,
                live_base_sha,
                live_head_sha,
                expected_failure,
                should_close,
            ) in cases:
                with self.subTest(name=name):
                    gh_log = root / f"gh-log-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "DESIRED_HEAD_SHA": desired_sha,
                        "EXISTING_PR": "17",
                        "FAKE_GH_LOG": str(gh_log),
                        "GH_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "LIVE_BASE_SHA": live_base_sha,
                        "LIVE_HEAD_SHA": live_head_sha,
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "PREPARED_TARGET_BASE_SHA": prepared_base_sha,
                        "PR_PAYLOAD": json.dumps(payload),
                        "SYNC_BRANCH": "automation/canonical-personal-sync",
                        "TARGET_BASE": "master",
                        "TARGET_OWNER": "Joey-Tools",
                        "TARGET_REPOSITORY": "Joey-Tools/codex-toolbox",
                    }
                    completed = subprocess.run(
                        [
                            "/bin/bash",
                            "-euo",
                            "pipefail",
                            "-c",
                            self._step_run("Close clean owned toolbox sync PR"),
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if expected_failure:
                        self.assertNotEqual(completed.returncode, 0)
                    else:
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                    commands = gh_log.read_text(encoding="utf-8")
                    self.assertEqual("pr close 17" in commands, should_close)
                    self.assertNotIn("--delete-branch", commands)

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
