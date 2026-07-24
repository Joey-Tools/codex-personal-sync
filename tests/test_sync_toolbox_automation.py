from __future__ import annotations

import hashlib
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

    def test_branch_push_and_pr_use_exact_lease_without_master_write(
        self,
    ) -> None:
        self.assertIn(
            "SYNC_BRANCH: automation/canonical-personal-sync",
            self.workflow,
        )
        self.assertIn(
            '"${DESIRED_HEAD_SHA}:refs/heads/${SYNC_BRANCH}"',
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
        self.assertIn(
            '"--force-with-lease=refs/heads/${SYNC_BRANCH}:'
            '${PREPARED_REMOTE_BRANCH_SHA}"',
            self.workflow,
        )
        self.assertIn(
            '"--force-with-lease=refs/heads/${SYNC_BRANCH}:"',
            self.workflow,
        )
        self.assertNotIn("--force-if-includes", self.workflow)
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
            'require_managed_paths \\\n  "The target base" "${base_allowed_paths_file}"'
        )
        self.assertIn(detached_base, prepare)
        self.assertIn(managed_paths, prepare)
        self.assertLess(prepare.index(detached_base), prepare.index(managed_paths))
        self.assertLess(prepare.index(managed_paths), prepare.index("remote_branch="))
        self.assertGreaterEqual(prepare.count("require_managed_paths"), 3)
        self.assertIn('"${base_allowed_paths_file}"', prepare)
        self.assertIn('"${branch_allowed_paths_file}"', prepare)
        self.assertIn("add | unique | sort", prepare)
        self.assertLess(
            prepare.index(
                'require_managed_paths \\\n    "The existing generated branch"'
            ),
            prepare.index(
                'assert_allowed_paths \\\n    "The existing generated branch"'
            ),
        )
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
        self.assertIn(
            'git -C "${TARGET_ROOT}" merge-base --is-ancestor',
            push,
        )
        self.assertIn(
            'git -C "${TARGET_ROOT}" rev-parse --verify',
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
        self.assertGreaterEqual(close.count('gh pr view "${EXISTING_PR}"'), 2)
        self.assertIn('.state == "CLOSED"', close)
        self.assertIn("Sync PR recovery required", close)

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
                "        if Path(os.environ['CREATED_STATE']).exists():\n"
                "            print(os.environ['LIVE_BASE_AFTER_CREATE'])\n"
                "        else:\n"
                "            print(os.environ['LIVE_BASE_SHA'])\n"
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
                "    if os.environ['CREATE_FAILURE'] == '1':\n"
                "        raise SystemExit(17)\n"
                "elif args[:2] == ['pr', 'view']:\n"
                "    if args[2] != os.environ['ACTUAL_CREATED_NUMBER']:\n"
                "        raise SystemExit(1)\n"
                "    if Path(os.environ['CLOSED_STATE']).exists():\n"
                "        payload = json.loads(os.environ['POST_CLOSE_PR_PAYLOAD'])\n"
                "    else:\n"
                "        payload = json.loads(os.environ['RECOVERY_PR_PAYLOAD'])\n"
                "    print(json.dumps(payload))\n"
                "elif args[:2] == ['pr', 'close']:\n"
                "    if args[2] != os.environ['ACTUAL_CREATED_NUMBER']:\n"
                "        raise SystemExit(1)\n"
                "    if os.environ['CLOSE_FAILURE'] == '1':\n"
                "        raise SystemExit(1)\n"
                "    Path(os.environ['CLOSED_STATE']).write_text('closed', encoding='ascii')\n"
                "    if os.environ['CLOSE_RESPONSE_FAILURE'] == '1':\n"
                "        raise SystemExit(19)\n",
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
            recovery_payload = exact_payload[0]
            closed_recovery_payload = {**recovery_payload, "state": "CLOSED"}
            identity_mutations = (
                ("number", {"number": 30}, {"number": 30}),
                (
                    "marker",
                    {"body": "ordinary pull request\n"},
                    {"body": "ordinary pull request\n"},
                ),
                ("state", {"state": "CLOSED"}, {"state": "OPEN"}),
                (
                    "base name",
                    {"baseRefName": "other"},
                    {"baseRefName": "other"},
                ),
                (
                    "base OID",
                    {"baseRefOid": "7" * 40},
                    {"baseRefOid": "7" * 40},
                ),
                (
                    "head name",
                    {"headRefName": "other"},
                    {"headRefName": "other"},
                ),
                (
                    "head OID",
                    {"headRefOid": "6" * 40},
                    {"headRefOid": "6" * 40},
                ),
                (
                    "cross repository",
                    {"isCrossRepository": True},
                    {"isCrossRepository": True},
                ),
                (
                    "owner",
                    {"headRepositoryOwner": {"login": "someone-else"}},
                    {"headRepositoryOwner": {"login": "someone-else"}},
                ),
            )
            recovery_identity_cases = tuple(
                {
                    "name": f"recovery {label} drift",
                    "after_payload": [{**exact_payload[0], **before_updates}],
                    "recovery_payload": {
                        **recovery_payload,
                        **before_updates,
                    },
                }
                for label, before_updates, _post_updates in identity_mutations
            )
            post_close_identity_cases = tuple(
                {
                    "name": f"recovery {label} drift after close",
                    "after_payload": drifted_payload,
                    "close_attempted": True,
                    "close_recorded": True,
                    "post_close_payload": {
                        **closed_recovery_payload,
                        **post_updates,
                    },
                }
                for label, _before_updates, post_updates in identity_mutations
            )
            malformed_candidate_cases = tuple(
                {
                    "name": f"create recovery rejects {label} number",
                    "after_payload": [{**exact_payload[0], "number": value}],
                    "broad_recovery": True,
                    "create_failure": True,
                }
                for label, value in (
                    ("fractional", 29.5),
                    ("string", "29"),
                    ("null", None),
                    ("zero", 0),
                    ("negative", -1),
                )
            )
            cases = (
                (
                    {"name": "exact", "expected_failure": 0},
                    {
                        "name": "head drift before create",
                        "head_before": "6" * 40,
                        "head_after": "6" * 40,
                        "after_payload": drifted_payload,
                        "should_create": False,
                    },
                    {
                        "name": "head drift after create",
                        "head_after": "6" * 40,
                        "after_payload": drifted_payload,
                        "close_attempted": True,
                        "close_recorded": True,
                    },
                    {
                        "name": "base drift before create",
                        "base_before": "7" * 40,
                        "base_after": "7" * 40,
                        "should_create": False,
                    },
                    {
                        "name": "base drift after create",
                        "base_after": "7" * 40,
                        "close_attempted": True,
                        "close_recorded": True,
                    },
                    {
                        "name": "create command failed after server creation",
                        "create_failure": True,
                    },
                    {
                        "name": "create response format invalid",
                        "created_url": "created pull request",
                    },
                    {
                        "name": "returned number mismatch",
                        "created_url": (
                            "https://github.com/Joey-Tools/codex-toolbox/pull/30"
                        ),
                    },
                )
                + malformed_candidate_cases
                + recovery_identity_cases
                + post_close_identity_cases
                + (
                    {
                        "name": "recovery close failure",
                        "head_after": "6" * 40,
                        "after_payload": drifted_payload,
                        "close_attempted": True,
                        "close_failure": True,
                    },
                    {
                        "name": "recovery close response lost",
                        "head_after": "6" * 40,
                        "after_payload": drifted_payload,
                        "close_attempted": True,
                        "close_recorded": True,
                        "close_response_failure": True,
                    },
                )
            )
            for case in cases:
                name = str(case["name"])
                expected_failure = int(case.get("expected_failure", 1))
                should_create = bool(case.get("should_create", True))
                close_attempted = bool(case.get("close_attempted", False))
                close_recorded = bool(case.get("close_recorded", False))
                with self.subTest(name=name):
                    gh_log = root / f"gh-log-{name.replace(' ', '-')}"
                    created_state = root / f"created-{name.replace(' ', '-')}"
                    closed_state = root / f"closed-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "ACTUAL_CREATED_NUMBER": "29",
                        "AFTER_PR_PAYLOAD": json.dumps(
                            case.get("after_payload", exact_payload)
                        ),
                        "CANONICAL_SHA": "3" * 40,
                        "CLOSED_STATE": str(closed_state),
                        "CLOSE_FAILURE": ("1" if case.get("close_failure") else "0"),
                        "CLOSE_RESPONSE_FAILURE": (
                            "1" if case.get("close_response_failure") else "0"
                        ),
                        "CREATE_FAILURE": ("1" if case.get("create_failure") else "0"),
                        "CREATED_PR_URL": str(
                            case.get(
                                "created_url",
                                "https://github.com/Joey-Tools/codex-toolbox/pull/29",
                            )
                        ),
                        "CREATED_STATE": str(created_state),
                        "DESIRED_HEAD_SHA": desired_sha,
                        "EXISTING_PR": "",
                        "FAKE_GH_LOG": str(gh_log),
                        "GH_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "GITHUB_REPOSITORY": "Joey-Tools/codex-personal-sync",
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_WORKFLOW": "Sync toolbox mirror",
                        "LIVE_BASE_AFTER_CREATE": str(
                            case.get("base_after", prepared_base_sha)
                        ),
                        "LIVE_BASE_SHA": str(
                            case.get("base_before", prepared_base_sha)
                        ),
                        "LIVE_HEAD_AFTER_CREATE": str(
                            case.get("head_after", desired_sha)
                        ),
                        "LIVE_HEAD_SHA": str(case.get("head_before", desired_sha)),
                        "MIRROR_NAME": "toolbox",
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "PREPARED_TARGET_BASE_SHA": prepared_base_sha,
                        "POST_CLOSE_PR_PAYLOAD": json.dumps(
                            case.get(
                                "post_close_payload",
                                closed_recovery_payload,
                            )
                        ),
                        "RECOVERY_PR_PAYLOAD": json.dumps(
                            case.get("recovery_payload", recovery_payload)
                        ),
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
                    self.assertEqual("pr close" in commands, close_attempted)
                    self.assertEqual(closed_state.exists(), close_recorded)
                    if name in {
                        "head drift before create",
                        "base drift before create",
                    }:
                        expected_reason = (
                            "Sync branch advanced"
                            if name.startswith("head")
                            else "Toolbox master advanced"
                        )
                        self.assertIn(expected_reason, completed.stdout)
                    if name in {
                        "head drift after create",
                        "base drift after create",
                    }:
                        self.assertIn("Rejected created sync PR", completed.stdout)
                    if (
                        name.startswith("recovery ")
                        and name != "recovery close response lost"
                    ) or name in {
                        "create command failed after server creation",
                        "create response format invalid",
                        "returned number mismatch",
                    }:
                        self.assertIn("Sync PR recovery required", completed.stdout)
                    if name in {
                        "create command failed after server creation",
                        "create response format invalid",
                    }:
                        self.assertIn(
                            "Joey-Tools/codex-toolbox#29",
                            completed.stdout,
                        )
                    if case.get("broad_recovery"):
                        self.assertIn(
                            "base=master head=automation/canonical-personal-sync",
                            completed.stdout,
                        )
                    if name == "recovery close response lost":
                        self.assertIn(
                            "Sync PR closure response lost",
                            completed.stdout,
                        )

    def test_existing_pr_edit_revalidates_refs_and_ownership(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("jq is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-edit-pr."
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
                "log = Path(os.environ['FAKE_GH_LOG'])\n"
                "with log.open('a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "edited = Path(os.environ['EDITED_STATE']).exists()\n"
                "if args[:1] == ['api']:\n"
                "    if '/heads/master' in args[1]:\n"
                "        key = 'LIVE_BASE_AFTER_EDIT' if edited else 'LIVE_BASE_SHA'\n"
                "    else:\n"
                "        key = 'LIVE_HEAD_AFTER_EDIT' if edited else 'LIVE_HEAD_SHA'\n"
                "    print(os.environ[key])\n"
                "elif args[:2] == ['pr', 'list']:\n"
                "    key = 'AFTER_PR_PAYLOAD' if edited else 'BEFORE_PR_PAYLOAD'\n"
                "    print(os.environ[key])\n"
                "elif args[:2] == ['pr', 'edit']:\n"
                "    if args[2] != '17':\n"
                "        raise SystemExit(1)\n"
                "    Path(os.environ['EDITED_STATE']).write_text('edited', encoding='ascii')\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            desired_sha = "5" * 40
            prepared_base_sha = "4" * 40
            exact_payload = [
                {
                    "number": 17,
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
            identity_mutations = (
                ("number", {"number": 18}),
                ("marker", {"body": "ordinary pull request\n"}),
                ("state", {"state": "CLOSED"}),
                ("base name", {"baseRefName": "other"}),
                ("base OID", {"baseRefOid": "7" * 40}),
                ("head name", {"headRefName": "other"}),
                ("head OID", {"headRefOid": "6" * 40}),
                ("cross repository", {"isCrossRepository": True}),
                (
                    "owner",
                    {"headRepositoryOwner": {"login": "someone-else"}},
                ),
            )
            identity_cases = tuple(
                case
                for label, updates in identity_mutations
                for case in (
                    {
                        "name": f"PR {label} drift before edit",
                        "before_payload": [{**exact_payload[0], **updates}],
                        "should_edit": False,
                    },
                    {
                        "name": f"PR {label} drift after edit",
                        "after_payload": [{**exact_payload[0], **updates}],
                    },
                )
            )
            cases = (
                {"name": "exact", "expected_failure": 0},
                {
                    "name": "base drift before edit",
                    "base_before": "7" * 40,
                    "should_edit": False,
                },
                {
                    "name": "head drift before edit",
                    "head_before": "6" * 40,
                    "should_edit": False,
                },
                {
                    "name": "base drift after edit",
                    "base_after": "7" * 40,
                },
                {
                    "name": "head drift after edit",
                    "head_after": "6" * 40,
                },
            ) + identity_cases
            for case in cases:
                name = str(case["name"])
                expected_failure = int(case.get("expected_failure", 1))
                should_edit = bool(case.get("should_edit", True))
                with self.subTest(name=name):
                    edited_state = root / f"edited-{name.replace(' ', '-')}"
                    gh_log = root / f"gh-log-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "AFTER_PR_PAYLOAD": json.dumps(
                            case.get("after_payload", exact_payload)
                        ),
                        "BEFORE_PR_PAYLOAD": json.dumps(
                            case.get("before_payload", exact_payload)
                        ),
                        "CANONICAL_SHA": "3" * 40,
                        "DESIRED_HEAD_SHA": desired_sha,
                        "EDITED_STATE": str(edited_state),
                        "EXISTING_PR": "17",
                        "FAKE_GH_LOG": str(gh_log),
                        "GH_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "GITHUB_REPOSITORY": "Joey-Tools/codex-personal-sync",
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_WORKFLOW": "Sync toolbox mirror",
                        "LIVE_BASE_AFTER_EDIT": str(
                            case.get("base_after", prepared_base_sha)
                        ),
                        "LIVE_BASE_SHA": str(
                            case.get("base_before", prepared_base_sha)
                        ),
                        "LIVE_HEAD_AFTER_EDIT": str(
                            case.get("head_after", desired_sha)
                        ),
                        "LIVE_HEAD_SHA": str(case.get("head_before", desired_sha)),
                        "MIRROR_NAME": "toolbox",
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "PREPARED_TARGET_BASE_SHA": prepared_base_sha,
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
                    self.assertEqual("pr edit 17" in commands, should_edit)
                    self.assertNotIn("pr create", commands)

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

    def test_prepare_recovers_consecutive_unmerged_rename_and_removal_scope(
        self,
    ) -> None:
        jq = shutil.which("jq")
        python = shutil.which("python3")
        if jq is None or python is None:
            self.skipTest("jq and python3 are required")

        with tempfile.TemporaryDirectory(
            prefix="sync-toolbox-branch-receipt."
        ) as temporary_directory:
            root = Path(temporary_directory)
            canonical_root = root / "canonical"
            seed_root = root / "seed"
            remote_root = root / "toolbox.git"
            fake_bin = root / "bin"
            fixture_home = root / "home"
            canonical_root.mkdir()
            seed_root.mkdir()
            fake_bin.mkdir()
            fixture_home.mkdir()

            def run_git(
                repository: Path,
                *arguments: str,
                environment: dict[str, str] | None = None,
            ) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(repository), *arguments],
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                return completed.stdout.strip()

            def initialize_repository(repository: Path) -> None:
                run_git(repository, "init", "-q", "-b", "master")
                run_git(repository, "config", "user.name", "Sync Fixture")
                run_git(
                    repository,
                    "config",
                    "user.email",
                    "sync-fixture@example.invalid",
                )
                run_git(repository, "config", "commit.gpgsign", "false")

            def commit_all(repository: Path, message: str) -> str:
                run_git(repository, "add", "-A")
                run_git(
                    repository,
                    "commit",
                    "--no-gpg-sign",
                    "-q",
                    "-m",
                    message,
                )
                return run_git(repository, "rev-parse", "HEAD")

            initialize_repository(canonical_root)
            initialize_repository(seed_root)
            subprocess.run(
                ["git", "init", "--bare", "-q", str(remote_root)],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            canonical_scripts = canonical_root / "scripts"
            canonical_scripts.mkdir()
            shutil.copy2(
                REPOSITORY_ROOT / "scripts" / "sync_canonical_mirrors.py",
                canonical_scripts / "sync_canonical_mirrors.py",
            )
            (canonical_scripts / "sync_canonical_mirrors.py").chmod(0o755)
            (canonical_root / "AGENTS.md").write_text(
                "# Fixture rules\n",
                encoding="utf-8",
            )
            anchor_source = canonical_scripts / "anchor.py"
            engine_source = canonical_scripts / "engine.py"
            anchor_source.write_text("anchor = True\n", encoding="utf-8")
            engine_source.write_text("engine = True\n", encoding="utf-8")

            lock_path = canonical_root / "sync-source-lock.json"

            def write_lock(engine_target: str | None) -> None:
                files = {"anchor": "mirror/anchor.py"}
                if engine_target is not None:
                    files["engine"] = engine_target
                payload = {
                    "version": 1,
                    "hash_algorithm": "sha256",
                    "canonical_repository": ("Joey-Tools/codex-personal-sync"),
                    "sources": {
                        "anchor": {
                            "path": "scripts/anchor.py",
                            "sha256": hashlib.sha256(
                                anchor_source.read_bytes()
                            ).hexdigest(),
                            "mode": "0644",
                        },
                        "engine": {
                            "path": "scripts/engine.py",
                            "sha256": hashlib.sha256(
                                engine_source.read_bytes()
                            ).hexdigest(),
                            "mode": "0644",
                        },
                    },
                    "mirrors": {
                        "toolbox": {
                            "repository": "Joey-Tools/codex-toolbox",
                            "files": files,
                        }
                    },
                }
                lock_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            def generate(target_root: Path, source_commit: str) -> None:
                completed = subprocess.run(
                    [
                        python,
                        str(canonical_scripts / "sync_canonical_mirrors.py"),
                        "generate",
                        "--target-root",
                        str(target_root),
                        "--mirror",
                        "toolbox",
                        "--source-commit",
                        source_commit,
                    ],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )

            write_lock("mirror/one.py")
            first_canonical = commit_all(
                canonical_root,
                "canonical one",
            )
            run_git(
                canonical_root,
                "remote",
                "add",
                "origin",
                "https://github.com/Joey-Tools/codex-personal-sync.git",
            )
            (seed_root / "README.md").write_text(
                "# Toolbox fixture\n",
                encoding="utf-8",
            )
            commit_all(seed_root, "toolbox seed")
            run_git(
                seed_root,
                "remote",
                "add",
                "origin",
                "https://github.com/Joey-Tools/codex-toolbox.git",
            )
            generate(seed_root, first_canonical)
            commit_all(seed_root, "generated one")
            run_git(
                seed_root,
                "push",
                str(remote_root),
                "HEAD:refs/heads/master",
            )

            run_git(
                seed_root,
                "switch",
                "-q",
                "-c",
                "automation/canonical-personal-sync",
            )
            write_lock("mirror/two.py")
            second_canonical = commit_all(
                canonical_root,
                "canonical two",
            )
            generate(seed_root, second_canonical)
            commit_all(seed_root, "generated two")
            run_git(
                seed_root,
                "push",
                str(remote_root),
                "HEAD:refs/heads/automation/canonical-personal-sync",
            )

            base64 = fake_bin / "base64"
            base64.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            base64.chmod(0o755)
            fixture_config = fixture_home / ".gitconfig"
            fixture_config.write_text(
                (
                    '[url "file://'
                    f'{remote_root.as_posix()}"]\n'
                    "  insteadOf = "
                    "https://github.com/Joey-Tools/codex-toolbox.git\n"
                    '[protocol "file"]\n'
                    "  allow = always\n"
                ),
                encoding="utf-8",
            )
            workflow_path = os.pathsep.join(
                [
                    str(fake_bin),
                    str(Path(jq).parent),
                    str(Path(python).parent),
                    os.environ.get("PATH", ""),
                ]
            )

            def run_prepare(
                target_root: Path,
                runner_temp: Path,
                *,
                expect_success: bool,
            ) -> subprocess.CompletedProcess[str]:
                runner_temp.mkdir()
                github_output = runner_temp / "github-output"
                environment = {
                    **os.environ,
                    "CANONICAL_ROOT": str(canonical_root),
                    "CODEX_TOOLBOX_SYNC_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                    "GITHUB_OUTPUT": str(github_output),
                    "HOME": str(fixture_home),
                    "MIRROR_NAME": "toolbox",
                    "PATH": workflow_path,
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
                    timeout=120,
                )
                if expect_success:
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )
                else:
                    self.assertNotEqual(completed.returncode, 0)
                return completed

            def clone_master(destination: Path) -> None:
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "-q",
                        "--branch",
                        "master",
                        str(remote_root),
                        str(destination),
                    ],
                    cwd=REPOSITORY_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                run_git(
                    destination,
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/Joey-Tools/codex-toolbox.git",
                )
                run_git(destination, "config", "user.name", "Sync Fixture")
                run_git(
                    destination,
                    "config",
                    "user.email",
                    "sync-fixture@example.invalid",
                )
                run_git(destination, "config", "commit.gpgsign", "false")

            write_lock("mirror/three.py")
            third_canonical = commit_all(
                canonical_root,
                "canonical three",
            )
            rename_runner = root / "rename-runner"
            clone_master(rename_runner)
            rename_temp = root / "rename-temp"
            run_prepare(rename_runner, rename_temp, expect_success=True)
            self.assertEqual(
                json.loads(
                    (rename_temp / "toolbox-sync-allowed-paths.json").read_text(
                        encoding="utf-8"
                    )
                ),
                [
                    "generated-sync-source-lock.json",
                    "mirror/anchor.py",
                    "mirror/one.py",
                    "mirror/three.py",
                    "mirror/two.py",
                ],
            )
            generate(rename_runner, third_canonical)
            commit_all(rename_runner, "generated three")
            run_git(
                rename_runner,
                "push",
                str(remote_root),
                "HEAD:refs/heads/automation/canonical-personal-sync",
            )

            write_lock(None)
            fourth_canonical = commit_all(
                canonical_root,
                "canonical removal",
            )
            removal_runner = root / "removal-runner"
            clone_master(removal_runner)
            removal_temp = root / "removal-temp"
            run_prepare(removal_runner, removal_temp, expect_success=True)
            self.assertEqual(
                json.loads(
                    (removal_temp / "toolbox-sync-allowed-paths.json").read_text(
                        encoding="utf-8"
                    )
                ),
                [
                    "generated-sync-source-lock.json",
                    "mirror/anchor.py",
                    "mirror/one.py",
                    "mirror/three.py",
                ],
            )
            generate(removal_runner, fourth_canonical)
            commit_all(removal_runner, "remove generated engine")
            self.assertFalse((removal_runner / "mirror" / "three.py").exists())
            run_git(
                removal_runner,
                "push",
                str(remote_root),
                "HEAD:refs/heads/automation/canonical-personal-sync",
            )

            (removal_runner / "unrelated.txt").write_text(
                "unowned branch drift\n",
                encoding="utf-8",
            )
            commit_all(removal_runner, "inject unrelated branch drift")
            run_git(
                removal_runner,
                "push",
                str(remote_root),
                "HEAD:refs/heads/automation/canonical-personal-sync",
            )
            drift_runner = root / "drift-runner"
            clone_master(drift_runner)
            drift = run_prepare(
                drift_runner,
                root / "drift-temp",
                expect_success=False,
            )
            self.assertIn(
                "Out-of-scope toolbox branch",
                drift.stdout + drift.stderr,
            )
            self.assertIn(
                "unrelated.txt",
                drift.stdout + drift.stderr,
            )

    def test_push_uses_exact_lease_and_rejects_pre_push_ref_races(self) -> None:
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
                "absent = 'absent'\n"
                "if 'ls-remote' in args:\n"
                "    reference = args[-1]\n"
                "    if reference.endswith('/master'):\n"
                "        print(f\"{os.environ['BASE_SHA']}\\t{reference}\")\n"
                "    else:\n"
                "        value = state.read_text(encoding='ascii')\n"
                "        if value != absent:\n"
                "            if os.environ.get('DUPLICATE_BRANCH') == '1':\n"
                "                print(f'{value}\\t{reference}')\n"
                "            print(f'{value}\\t{reference}')\n"
                "elif 'rev-parse' in args and '--verify' in args:\n"
                "    print(os.environ['DESIRED_HEAD_SHA'])\n"
                "elif 'merge-base' in args and '--is-ancestor' in args:\n"
                "    if os.environ.get('DESIRED_IS_DESCENDANT') != '1':\n"
                "        raise SystemExit(1)\n"
                "elif 'push' in args:\n"
                "    race = os.environ.get('RACE_BEFORE_PUSH', 'none')\n"
                "    race_marker = Path(os.environ['RACE_MARKER'])\n"
                "    if race != 'none' and not race_marker.exists():\n"
                "        if race == 'delete':\n"
                "            state.write_text(absent, encoding='ascii')\n"
                "        elif race in {'rollback', 'appear'}:\n"
                "            state.write_text(os.environ['RACE_SHA'], encoding='ascii')\n"
                "        elif race == 'desired':\n"
                "            state.write_text(os.environ['DESIRED_HEAD_SHA'], encoding='ascii')\n"
                "        else:\n"
                "            raise SystemExit(f'unknown race: {race}')\n"
                "        race_marker.write_text('applied', encoding='ascii')\n"
                "    leases = [\n"
                "        item for item in args\n"
                "        if item.startswith('--force-with-lease=')\n"
                "    ]\n"
                "    if len(leases) != 1:\n"
                "        raise SystemExit('missing exact lease')\n"
                "    lease = leases[0].split('=', 1)[1]\n"
                "    reference, expected = lease.rsplit(':', 1)\n"
                "    if reference != f\"refs/heads/{os.environ['SYNC_BRANCH']}\":\n"
                "        raise SystemExit('wrong leased ref')\n"
                "    observed = state.read_text(encoding='ascii')\n"
                "    if (expected == '' and observed != absent) or (\n"
                "        expected != '' and observed != expected\n"
                "    ):\n"
                "        raise SystemExit('stale lease')\n"
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
            drift_sha = "4" * 40
            existing_lease = (
                "--force-with-lease=refs/heads/"
                f"automation/canonical-personal-sync:{prepared_sha}"
            )
            absent_lease = (
                "--force-with-lease=refs/heads/automation/canonical-personal-sync:"
            )
            cases = (
                {
                    "name": "matching prepared state",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "expected_final": desired_sha,
                    "expected_lease": existing_lease,
                },
                {
                    "name": "same desired no-op",
                    "live_sha": desired_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "expected_final": desired_sha,
                    "message": "already points",
                },
                {
                    "name": "absent branch creation",
                    "live_sha": "absent",
                    "prepared_present": "false",
                    "prepared_value": "",
                    "expected_final": desired_sha,
                    "expected_lease": absent_lease,
                },
                {
                    "name": "remote drift before push preflight",
                    "live_sha": drift_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "expected_failure": True,
                    "expected_final": drift_sha,
                    "message": "Sync branch advanced",
                },
                {
                    "name": "branch appeared before push preflight",
                    "live_sha": drift_sha,
                    "prepared_present": "false",
                    "prepared_value": "",
                    "expected_failure": True,
                    "expected_final": drift_sha,
                    "message": "Sync branch appeared",
                },
                {
                    "name": "duplicate branch records",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "duplicate": "1",
                    "expected_failure": True,
                    "expected_final": prepared_sha,
                    "message": "Ambiguous remote ref",
                },
                {
                    "name": "non-descendant generated head",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "descendant": "0",
                    "expected_failure": True,
                    "expected_final": prepared_sha,
                    "message": "Non-descendant sync update",
                },
                {
                    "name": "branch deleted after preflight",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "race": "delete",
                    "expected_failure": True,
                    "expected_final": "absent",
                    "expected_lease": existing_lease,
                },
                {
                    "name": "branch rolled back after preflight",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "race": "rollback",
                    "expected_failure": True,
                    "expected_final": drift_sha,
                    "expected_lease": existing_lease,
                },
                {
                    "name": "branch moved to desired after preflight",
                    "live_sha": prepared_sha,
                    "prepared_present": "true",
                    "prepared_value": prepared_sha,
                    "race": "desired",
                    "expected_failure": True,
                    "expected_final": desired_sha,
                    "expected_lease": existing_lease,
                },
                {
                    "name": "branch appeared after absent preflight",
                    "live_sha": "absent",
                    "prepared_present": "false",
                    "prepared_value": "",
                    "race": "appear",
                    "expected_failure": True,
                    "expected_final": drift_sha,
                    "expected_lease": absent_lease,
                },
            )
            for case in cases:
                name = case["name"]
                with self.subTest(name=name):
                    state = root / f"state-{name.replace(' ', '-')}"
                    state.write_text(case["live_sha"], encoding="ascii")
                    git_log = root / f"log-{name.replace(' ', '-')}"
                    race_marker = root / f"race-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "BASE_SHA": base_sha,
                        "CODEX_TOOLBOX_SYNC_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "DESIRED_HEAD_SHA": desired_sha,
                        "DESIRED_IS_DESCENDANT": case.get("descendant", "1"),
                        "DUPLICATE_BRANCH": case.get("duplicate", "0"),
                        "FAKE_GIT_LOG": str(git_log),
                        "PATH": f"{fake_bin}:/usr/bin:/bin",
                        "PREPARED_REMOTE_BRANCH_PRESENT": case["prepared_present"],
                        "PREPARED_REMOTE_BRANCH_SHA": case["prepared_value"],
                        "PREPARED_TARGET_BASE_SHA": base_sha,
                        "RACE_BEFORE_PUSH": case.get("race", "none"),
                        "RACE_MARKER": str(race_marker),
                        "RACE_SHA": drift_sha,
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
                    if case.get("expected_failure", False):
                        self.assertNotEqual(completed.returncode, 0)
                    else:
                        self.assertEqual(
                            completed.returncode,
                            0,
                            completed.stdout + completed.stderr,
                        )
                    self.assertEqual(
                        state.read_text(encoding="ascii"),
                        case["expected_final"],
                    )
                    if message := case.get("message"):
                        self.assertIn(
                            message,
                            completed.stdout + completed.stderr,
                        )
                    commands = git_log.read_text(encoding="utf-8")
                    push_lines = [
                        line
                        for line in commands.splitlines()
                        if " push " in f" {line} "
                    ]
                    expected_lease = case.get("expected_lease")
                    self.assertEqual(
                        len(push_lines),
                        1 if expected_lease is not None else 0,
                    )
                    if expected_lease is not None:
                        self.assertIn(expected_lease, push_lines[0])
                        self.assertIn(
                            f"{desired_sha}:refs/heads/"
                            "automation/canonical-personal-sync",
                            push_lines[0],
                        )
                    self.assertNotIn("--force-if-includes", commands)
                    self.assertNotIn(
                        "refs/heads/master",
                        "\n".join(push_lines),
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
                "import json\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "with Path(os.environ['FAKE_GH_LOG']).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(' '.join(args) + '\\n')\n"
                "if args[:2] == ['pr', 'view']:\n"
                "    key = 'POST_CLOSE_PR_PAYLOAD' if Path(os.environ['CLOSED_STATE']).exists() else 'PR_PAYLOAD'\n"
                "    print(os.environ[key])\n"
                "elif args[:2] == ['pr', 'close']:\n"
                "    if os.environ['CLOSE_FAILURE'] == '1':\n"
                "        raise SystemExit(1)\n"
                "    Path(os.environ['CLOSED_STATE']).write_text('closed', encoding='ascii')\n"
                "    if os.environ['CLOSE_RESPONSE_FAILURE'] == '1':\n"
                "        raise SystemExit(19)\n"
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
            closed_payload = {**exact_payload, "state": "CLOSED"}
            identity_mutations = (
                ("number", {"number": 18}, {"number": 18}),
                (
                    "marker",
                    {"body": "ordinary pull request\n"},
                    {"body": "ordinary pull request\n"},
                ),
                ("state", {"state": "CLOSED"}, {"state": "OPEN"}),
                (
                    "base name",
                    {"baseRefName": "other"},
                    {"baseRefName": "other"},
                ),
                (
                    "base OID",
                    {"baseRefOid": "7" * 40},
                    {"baseRefOid": "7" * 40},
                ),
                (
                    "head name",
                    {"headRefName": "other"},
                    {"headRefName": "other"},
                ),
                (
                    "head OID",
                    {"headRefOid": "6" * 40},
                    {"headRefOid": "6" * 40},
                ),
                (
                    "cross repository",
                    {"isCrossRepository": True},
                    {"isCrossRepository": True},
                ),
                (
                    "owner",
                    {"headRepositoryOwner": {"login": "someone-else"}},
                    {"headRepositoryOwner": {"login": "someone-else"}},
                ),
            )
            identity_cases = tuple(
                case
                for label, before_updates, after_updates in identity_mutations
                for case in (
                    {
                        "name": f"{label} drift before close",
                        "payload": {**exact_payload, **before_updates},
                        "should_close": False,
                    },
                    {
                        "name": f"{label} drift after close",
                        "post_close_payload": {
                            **closed_payload,
                            **after_updates,
                        },
                        "should_close": True,
                    },
                )
            )
            cases = (
                {"name": "exact", "expected_failure": 0},
                {
                    "name": "live base drift before close",
                    "live_base_sha": "8" * 40,
                    "should_close": False,
                },
                {
                    "name": "live head drift before close",
                    "live_head_sha": "9" * 40,
                    "should_close": False,
                },
                {
                    "name": "close command failure",
                    "close_failure": True,
                    "should_close": True,
                },
                {
                    "name": "close response lost after server closure",
                    "close_response_failure": True,
                    "expected_failure": 0,
                    "should_close": True,
                },
                {
                    "name": "close remains open",
                    "post_close_payload": exact_payload,
                    "should_close": True,
                },
            )
            cases += identity_cases
            for case in cases:
                name = str(case["name"])
                expected_failure = int(case.get("expected_failure", 1))
                should_close = bool(case.get("should_close", True))
                with self.subTest(name=name):
                    gh_log = root / f"gh-log-{name.replace(' ', '-')}"
                    closed_state = root / f"closed-{name.replace(' ', '-')}"
                    environment = {
                        **os.environ,
                        "CLOSED_STATE": str(closed_state),
                        "CLOSE_FAILURE": ("1" if case.get("close_failure") else "0"),
                        "CLOSE_RESPONSE_FAILURE": (
                            "1" if case.get("close_response_failure") else "0"
                        ),
                        "DESIRED_HEAD_SHA": desired_sha,
                        "EXISTING_PR": "17",
                        "FAKE_GH_LOG": str(gh_log),
                        "GH_TOKEN": SYNTHETIC_ACCESS_TOKEN,
                        "LIVE_BASE_SHA": str(
                            case.get("live_base_sha", prepared_base_sha)
                        ),
                        "LIVE_HEAD_SHA": str(case.get("live_head_sha", desired_sha)),
                        "PATH": (f"{fake_bin}:{Path(jq).parent}:/usr/bin:/bin"),
                        "POST_CLOSE_PR_PAYLOAD": json.dumps(
                            case.get("post_close_payload", closed_payload)
                        ),
                        "PREPARED_TARGET_BASE_SHA": prepared_base_sha,
                        "PR_PAYLOAD": json.dumps(case.get("payload", exact_payload)),
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
                    self.assertEqual(
                        closed_state.exists(),
                        should_close and not case.get("close_failure", False),
                    )
                    self.assertNotIn("--delete-branch", commands)
                    if expected_failure and should_close:
                        self.assertIn(
                            "Sync PR recovery required",
                            completed.stdout + completed.stderr,
                        )
                    if name == "close response lost after server closure":
                        self.assertIn(
                            "Sync PR closure response lost",
                            completed.stdout + completed.stderr,
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
