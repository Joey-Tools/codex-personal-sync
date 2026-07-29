from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import NoReturn
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "sync-toolbox.yml"
DOCUMENTATION_PATH = REPOSITORY_ROOT / "docs" / "automation" / "sync-toolbox.md"
CHECKOUT_COMMIT = "11d5960a326750d5838078e36cf38b85af677262"
SYNTHETIC_ACCESS_TOKEN_ID = "access-a"
SYNTHETIC_ACCESS_TOKEN = "codex_synth_v1_access_a"
FULL_COMMIT_ACTION_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
ISOLATED_GENERATOR_RUNNER_SOURCE = """\
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


sys.dont_write_bytecode = True
generator_path = Path(sys.argv[1])
private_control_parent = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location(
    "sync_canonical_mirrors_isolated_test_runner",
    generator_path,
)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load generator from {generator_path}")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.PRIVATE_GIT_CONTROL_PARENT = private_control_parent
raise SystemExit(module.main(sys.argv[3:]))
"""


def _isolated_generator_command(
    root: Path,
    generator_path: Path,
    interpreter: str,
    *arguments: str,
) -> list[str]:
    runner = root / "run-sync-canonical-mirrors.py"
    private_control_parent = root / "mirror-private-control"
    private_control_parent.mkdir(mode=0o700, exist_ok=True)
    runner.write_text(
        ISOLATED_GENERATOR_RUNNER_SOURCE,
        encoding="utf-8",
    )
    return [
        interpreter,
        str(runner),
        str(generator_path),
        str(private_control_parent),
        *arguments,
    ]


class StrictWorkflowYamlError(ValueError):
    pass


# This loader accepts only the workflow's deliberately small YAML subset.
# Unsupported YAML features fail closed so they cannot hide executable `uses`
# mappings from the pin audit.
class _StrictWorkflowFlowParser:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.offset = 0

    def parse(self) -> object:
        value = self._parse_value()
        self._skip_spaces()
        if self.offset != len(self.payload):
            self._fail("unexpected trailing flow content")
        return value

    def _fail(self, message: str) -> NoReturn:
        raise StrictWorkflowYamlError(f"{message} at flow offset {self.offset}")

    def _skip_spaces(self) -> None:
        while self.offset < len(self.payload) and self.payload[self.offset] == " ":
            self.offset += 1

    def _parse_value(self) -> object:
        self._skip_spaces()
        if self.offset >= len(self.payload):
            self._fail("missing flow value")
        current = self.payload[self.offset]
        if current == "{":
            return self._parse_mapping()
        if current == "[":
            return self._parse_sequence()
        if current in {"'", '"'}:
            return self._parse_quoted()
        return self._parse_plain_value()

    def _parse_mapping(self) -> dict[str, object]:
        self.offset += 1
        result: dict[str, object] = {}
        self._skip_spaces()
        if self._consume("}"):
            return result
        while True:
            key = self._parse_key()
            self._skip_spaces()
            if not self._consume(":"):
                self._fail("flow mapping key is missing ':'")
            value = self._parse_value()
            if key in result:
                self._fail(f"duplicate mapping key {key!r}")
            result[key] = value
            self._skip_spaces()
            if self._consume("}"):
                return result
            if not self._consume(","):
                self._fail("flow mapping entries must be comma-separated")
            self._skip_spaces()
            if self.offset >= len(self.payload):
                self._fail("unterminated flow mapping")

    def _parse_sequence(self) -> list[object]:
        self.offset += 1
        result: list[object] = []
        self._skip_spaces()
        if self._consume("]"):
            return result
        while True:
            result.append(self._parse_value())
            self._skip_spaces()
            if self._consume("]"):
                return result
            if not self._consume(","):
                self._fail("flow sequence items must be comma-separated")
            self._skip_spaces()
            if self.offset >= len(self.payload):
                self._fail("unterminated flow sequence")

    def _parse_key(self) -> str:
        self._skip_spaces()
        if self.offset >= len(self.payload):
            self._fail("missing flow mapping key")
        if self.payload[self.offset] in {"'", '"'}:
            key = self._parse_quoted()
        else:
            start = self.offset
            while self.offset < len(self.payload) and self.payload[self.offset] != ":":
                if self.payload[self.offset] in "{[}],":
                    self._fail("unsupported flow mapping key")
                self.offset += 1
            key = self.payload[start : self.offset].strip()
        return _strict_workflow_yaml_key(key)

    def _parse_plain_value(self) -> str:
        start = self.offset
        while (
            self.offset < len(self.payload) and self.payload[self.offset] not in ",]}"
        ):
            if self.payload[self.offset] in "{[":
                self._fail("nested flow collections must start a value")
            self.offset += 1
        value = self.payload[start : self.offset].strip()
        return _strict_workflow_yaml_scalar(value)

    def _parse_quoted(self) -> str:
        quote = self.payload[self.offset]
        self.offset += 1
        result: list[str] = []
        while self.offset < len(self.payload):
            current = self.payload[self.offset]
            self.offset += 1
            if current == quote:
                if (
                    quote == "'"
                    and self.offset < len(self.payload)
                    and self.payload[self.offset] == "'"
                ):
                    result.append("'")
                    self.offset += 1
                    continue
                return "".join(result)
            if current == "\\" and quote == '"':
                if self.offset >= len(self.payload):
                    self._fail("unterminated double-quoted escape")
                escaped = self.payload[self.offset]
                self.offset += 1
                replacements = {
                    '"': '"',
                    "\\": "\\",
                    "/": "/",
                    "b": "\b",
                    "f": "\f",
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                }
                if escaped not in replacements:
                    self._fail("unsupported double-quoted escape")
                result.append(replacements[escaped])
                continue
            result.append(current)
        self._fail("unterminated quoted scalar")

    def _consume(self, expected: str) -> bool:
        if self.offset < len(self.payload) and self.payload[self.offset] == expected:
            self.offset += 1
            return True
        return False


class _StrictWorkflowYamlLoader:
    _BLOCK_SCALAR_RE = re.compile(r"^[>|](?:[1-9][+-]?|[+-][1-9]?)?$")

    def __init__(self, payload: str) -> None:
        if "\r" in payload:
            raise StrictWorkflowYamlError("workflow YAML must use LF newlines")
        self.lines = payload.split("\n")
        self.index = 0
        for line_number, line in enumerate(self.lines, start=1):
            if "\t" in line:
                raise StrictWorkflowYamlError(
                    f"tabs are unsupported at line {line_number}"
                )
            if any(ord(character) < 0x20 and character != "\n" for character in line):
                raise StrictWorkflowYamlError(
                    f"control character at line {line_number}"
                )

    def load(self) -> dict[str, object]:
        self._skip_ignored()
        if self.index >= len(self.lines):
            raise StrictWorkflowYamlError("workflow YAML is empty")
        if self._line_indent(self.index) != 0:
            self._fail("root mapping must start at indentation zero")
        document = self._parse_node(0)
        self._skip_ignored()
        if self.index != len(self.lines):
            self._fail("unexpected trailing YAML content")
        if not isinstance(document, dict):
            raise StrictWorkflowYamlError("workflow YAML root must be a mapping")
        return document

    def _fail(self, message: str) -> NoReturn:
        raise StrictWorkflowYamlError(f"{message} at line {self.index + 1}")

    def _skip_ignored(self) -> None:
        while self.index < len(self.lines):
            content = self.lines[self.index].lstrip(" ")
            if not content or content.startswith("#"):
                self.index += 1
                continue
            if content in {"---", "..."} or content.startswith("%YAML"):
                self._fail("YAML directives and document markers are unsupported")
            return

    def _line_indent(self, index: int) -> int:
        line = self.lines[index]
        return len(line) - len(line.lstrip(" "))

    def _line_content(self, index: int) -> str:
        return self.lines[index][self._line_indent(index) :].rstrip(" ")

    def _parse_node(self, indent: int) -> object:
        self._skip_ignored()
        if self.index >= len(self.lines):
            self._fail("missing nested YAML value")
        if self._line_indent(self.index) != indent:
            self._fail("unexpected YAML indentation")
        content = self._line_content(self.index)
        if content == "-" or content.startswith("- "):
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_mapping(
        self,
        indent: int,
        initial: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = {} if initial is None else initial
        while True:
            self._skip_ignored()
            if self.index >= len(self.lines):
                return result
            current_indent = self._line_indent(self.index)
            if current_indent < indent:
                return result
            if current_indent > indent:
                self._fail("unexpected mapping indentation")
            content = self._line_content(self.index)
            if content == "-" or content.startswith("- "):
                self._fail("mapping and sequence entries cannot be mixed")
            key, raw_value = self._parse_mapping_entry(content)
            self.index += 1
            self._store_mapping_value(result, key, raw_value, indent)

    def _parse_sequence(self, indent: int) -> list[object]:
        result: list[object] = []
        while True:
            self._skip_ignored()
            if self.index >= len(self.lines):
                return result
            current_indent = self._line_indent(self.index)
            if current_indent < indent:
                return result
            if current_indent > indent:
                self._fail("unexpected sequence indentation")
            content = self._line_content(self.index)
            if not (content == "-" or content.startswith("- ")):
                return result
            raw_item = content[1:].lstrip(" ")
            self.index += 1
            if not raw_item:
                result.append(self._parse_nested_value(indent))
                continue
            if raw_item.startswith(("{", "[")):
                result.append(self._parse_inline_value(raw_item))
                continue
            if _strict_workflow_yaml_mapping_colon(raw_item) is None:
                result.append(self._parse_inline_value(raw_item))
                continue
            key, raw_value = self._parse_mapping_entry(raw_item)
            mapping_indent = indent + 2
            mapping: dict[str, object] = {}
            self._store_mapping_value(
                mapping,
                key,
                raw_value,
                mapping_indent,
            )
            result.append(self._parse_mapping(mapping_indent, mapping))

    def _parse_nested_value(self, parent_indent: int) -> object:
        self._skip_ignored()
        if self.index >= len(self.lines):
            return None
        child_indent = self._line_indent(self.index)
        if child_indent <= parent_indent:
            return None
        return self._parse_node(child_indent)

    def _store_mapping_value(
        self,
        mapping: dict[str, object],
        key: str,
        raw_value: str,
        indent: int,
    ) -> None:
        if key in mapping:
            self._fail(f"duplicate mapping key {key!r}")
        value_without_comment = _strict_workflow_yaml_strip_comment(raw_value)
        if not value_without_comment:
            value = self._parse_nested_value(indent)
        elif self._BLOCK_SCALAR_RE.fullmatch(value_without_comment):
            value = self._consume_block_scalar(indent)
        else:
            value = self._parse_inline_value(value_without_comment)
        mapping[key] = value

    def _consume_block_scalar(self, parent_indent: int) -> str:
        payload: list[str] = []
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if line and self._line_indent(self.index) <= parent_indent:
                break
            payload.append(line)
            self.index += 1
        return "\n".join(payload)

    def _parse_mapping_entry(self, content: str) -> tuple[str, str]:
        colon = _strict_workflow_yaml_mapping_colon(content)
        if colon is None:
            raise StrictWorkflowYamlError(
                f"mapping entry is missing ':' at line {self.index + 1}"
            )
        key = _strict_workflow_yaml_key(content[:colon].strip())
        return key, content[colon + 1 :].lstrip(" ")

    def _parse_inline_value(self, raw_value: str) -> object:
        value = _strict_workflow_yaml_strip_comment(raw_value)
        if not value:
            return None
        if value.startswith(("{", "[")):
            return _StrictWorkflowFlowParser(value).parse()
        return _strict_workflow_yaml_scalar(value)


def _strict_workflow_yaml_mapping_colon(value: str) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                    continue
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == ":" and (index + 1 == len(value) or value[index + 1].isspace()):
            return index
    if quote is not None:
        raise StrictWorkflowYamlError("unterminated quoted mapping key")
    return None


def _strict_workflow_yaml_strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
                    continue
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in "[{":
            depth += 1
            continue
        if character in "]}":
            depth -= 1
            if depth < 0:
                raise StrictWorkflowYamlError("unbalanced flow collection")
            continue
        if (
            character == "#"
            and depth == 0
            and (index == 0 or value[index - 1].isspace())
        ):
            value = value[:index]
            break
    if quote is not None or depth != 0:
        raise StrictWorkflowYamlError("unterminated quoted or flow value")
    return value.rstrip(" ")


def _strict_workflow_yaml_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise StrictWorkflowYamlError("mapping keys must be non-empty strings")
    key = _strict_workflow_yaml_scalar(value)
    if key == "<<":
        raise StrictWorkflowYamlError("YAML merge keys are unsupported")
    return key


def _strict_workflow_yaml_scalar(value: str) -> str:
    if not value:
        raise StrictWorkflowYamlError("empty scalar is unsupported")
    if value[0] in {"&", "*", "!"}:
        raise StrictWorkflowYamlError("YAML anchors, aliases, and tags are unsupported")
    if value[0] in {"'", '"'}:
        parser = _StrictWorkflowFlowParser(value)
        decoded = parser._parse_quoted()
        parser._skip_spaces()
        if parser.offset != len(value):
            raise StrictWorkflowYamlError(
                "quoted scalar has unexpected trailing content"
            )
        return decoded
    if any(character in "\n\r\0" for character in value):
        raise StrictWorkflowYamlError("scalar contains a control character")
    return value


def _load_strict_workflow_yaml(payload: str) -> dict[str, object]:
    return _StrictWorkflowYamlLoader(payload).load()


def _workflow_external_action_uses(
    document: dict[str, object],
) -> list[str]:
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise StrictWorkflowYamlError("workflow jobs must be a non-empty mapping")
    action_uses: list[str] = []
    for job_name, job in jobs.items():
        if not isinstance(job_name, str) or not isinstance(job, dict):
            raise StrictWorkflowYamlError("each workflow job must be a mapping")
        job_uses = job.get("uses")
        if job_uses is not None:
            if not isinstance(job_uses, str) or not job_uses:
                raise StrictWorkflowYamlError(
                    f"job {job_name!r} uses must be a non-empty string"
                )
            if not job_uses.startswith("./"):
                action_uses.append(job_uses)
        steps = job.get("steps")
        if steps is None:
            continue
        if not isinstance(steps, list):
            raise StrictWorkflowYamlError(f"job {job_name!r} steps must be a sequence")
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise StrictWorkflowYamlError(
                    f"job {job_name!r} step {step_index} must be a mapping"
                )
            step_uses = step.get("uses")
            if step_uses is None:
                continue
            if not isinstance(step_uses, str) or not step_uses:
                raise StrictWorkflowYamlError(
                    f"job {job_name!r} step {step_index} uses must be "
                    "a non-empty string"
                )
            if not step_uses.startswith("./"):
                action_uses.append(step_uses)
    return action_uses


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

    def test_privileged_action_uses_are_pinned_to_full_commit_shas(self) -> None:
        workflow = _load_strict_workflow_yaml(self.workflow)
        action_uses = _workflow_external_action_uses(workflow)
        self.assertTrue(action_uses)
        for action_ref in action_uses:
            with self.subTest(action_ref=action_ref):
                self.assertRegex(action_ref, FULL_COMMIT_ACTION_RE)
        checkout_ref = f"actions/checkout@{CHECKOUT_COMMIT}"
        checkout_uses = [
            action_ref
            for action_ref in action_uses
            if action_ref.startswith("actions/checkout@")
        ]
        self.assertEqual(
            checkout_uses,
            [checkout_ref, checkout_ref],
        )
        self.assertEqual(
            self.workflow.count(f"uses: {checkout_ref} # v4.4.0"),
            2,
        )

    def test_privileged_action_pin_parser_covers_yaml_structures(self) -> None:
        pinned = "owner/action@" + "a" * 40
        fixture = f"""
jobs:
  block:
    steps:
      - uses: owner/block@main
  flow:
    steps:
      - {{ uses: owner/flow@main }}
  quoted:
    steps:
      - "uses" : owner/quoted@main
  reusable:
    'uses' : owner/repository/.github/workflows/reuse.yml@main
  safe:
    steps:
      - {{ "uses": {pinned} }}
      - uses: ./local-action
"""
        action_uses = _workflow_external_action_uses(
            _load_strict_workflow_yaml(fixture)
        )
        self.assertEqual(
            action_uses,
            [
                "owner/block@main",
                "owner/flow@main",
                "owner/quoted@main",
                "owner/repository/.github/workflows/reuse.yml@main",
                pinned,
            ],
        )
        self.assertRegex(action_uses[-1], FULL_COMMIT_ACTION_RE)
        for action_ref in action_uses[:-1]:
            with self.subTest(action_ref=action_ref):
                self.assertNotRegex(action_ref, FULL_COMMIT_ACTION_RE)

    def test_privileged_action_pin_parser_rejects_ambiguous_yaml(self) -> None:
        fixtures = {
            "alias": """
shared: &shared
  uses: owner/action@main
jobs:
  unsafe: *shared
""",
            "merge-key": """
jobs:
  unsafe:
    <<: { uses: owner/action@main }
""",
            "duplicate-key": """
jobs:
  unsafe:
    steps:
      - uses: owner/first@main
        uses: owner/second@main
""",
            "malformed-flow": """
jobs:
  unsafe:
    steps: [{ uses: owner/action@main ]
""",
        }
        for name, fixture in fixtures.items():
            with self.subTest(name=name), self.assertRaises(StrictWorkflowYamlError):
                _load_strict_workflow_yaml(fixture)

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
        self.assertIn("validate-branch-history", prepare)
        self.assertIn(
            'git -C "${TARGET_ROOT}" switch --force-create "${SYNC_BRANCH}"',
            prepare,
        )
        self.assertNotIn(
            'git -C "${TARGET_ROOT}" merge --no-edit',
            prepare,
        )
        history = self._step_run("Validate fresh generated branch history")
        self.assertIn("validate-branch-history", history)
        self.assertIn("--require-fresh-head", history)
        self.assertIn(".merge_base_sha == $base", history)
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
        self.assertNotIn("merge-base --is-ancestor", push)
        self.assertIn("DESIRED_HISTORY_SHA256", push)
        self.assertIn("PREPARED_REMOTE_HISTORY_SHA256", push)
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
                "#!/usr/bin/python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "if 'validate-branch-history' in sys.argv:\n"
                "    print(json.dumps({\n"
                "        'schema_version': 1,\n"
                "        'base_sha': os.environ['BASE_SHA'],\n"
                "        'head_sha': os.environ['FETCHED_BRANCH_SHA'],\n"
                "        'profile': 'branch-exclusive',\n"
                "        'history_sha256': 'd' * 64,\n"
                "    }))\n"
                "else:\n"
                "    print('[]')\n",
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
                    _isolated_generator_command(
                        root,
                        canonical_scripts / "sync_canonical_mirrors.py",
                        python,
                        "generate",
                        "--target-root",
                        str(target_root),
                        "--mirror",
                        "toolbox",
                        "--source-commit",
                        source_commit,
                    ),
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
            second_branch_sha = commit_all(seed_root, "generated two")
            run_git(
                seed_root,
                "push",
                str(remote_root),
                "HEAD:refs/heads/automation/canonical-personal-sync",
            )

            base64 = fake_bin / "base64"
            base64.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
            base64.chmod(0o755)
            isolated_generator_command = _isolated_generator_command(
                root,
                canonical_scripts / "sync_canonical_mirrors.py",
                python,
            )
            python_shim = fake_bin / "python3"
            python_shim.write_text(
                (
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  */sync_canonical_mirrors.py)\n"
                    '    generator_path="$1"\n'
                    "    shift\n"
                    f"    exec {shlex.quote(python)} "
                    f"{shlex.quote(isolated_generator_command[1])} "
                    '"${generator_path}" '
                    f"{shlex.quote(isolated_generator_command[3])} "
                    '"$@"\n'
                    "    ;;\n"
                    "  *)\n"
                    f"    exec {shlex.quote(python)} "
                    '"$@"\n'
                    "    ;;\n"
                    "esac\n"
                ),
                encoding="utf-8",
            )
            python_shim.chmod(0o755)
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
            third_branch_sha = commit_all(rename_runner, "generated three")
            run_git(
                rename_runner,
                "push",
                (
                    "--force-with-lease=refs/heads/"
                    "automation/canonical-personal-sync:"
                    f"{second_branch_sha}"
                ),
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
                (
                    "--force-with-lease=refs/heads/"
                    "automation/canonical-personal-sync:"
                    f"{third_branch_sha}"
                ),
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
                        "DESIRED_HISTORY_SHA256": "5" * 64,
                        "DUPLICATE_BRANCH": case.get("duplicate", "0"),
                        "FAKE_GIT_LOG": str(git_log),
                        "PATH": f"{fake_bin}:/usr/bin:/bin",
                        "PREPARED_REMOTE_BRANCH_PRESENT": case["prepared_present"],
                        "PREPARED_REMOTE_BRANCH_SHA": case["prepared_value"],
                        "PREPARED_REMOTE_HISTORY_SHA256": "6" * 64,
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


class SyncBranchHistoryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="sync-branch-history."
        )
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.allowed_paths_file = self.root / "allowed-paths.json"
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.name", "History Fixture")
        self._git(
            "config",
            "user.email",
            "history-fixture@example.invalid",
        )
        self._git("config", "commit.gpgsign", "false")
        self._write("seed.txt", "seed\n")
        self.base_sha = self._commit("base")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            cwd=REPOSITORY_ROOT,
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
        return completed.stdout.strip()

    def _write(self, path: str, payload: str) -> None:
        target = self.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")

    def _commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "--no-gpg-sign", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _validate(
        self,
        base_sha: str,
        head_sha: str,
        allowed_paths: list[str],
        *extra_arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        self.allowed_paths_file.write_text(
            json.dumps(sorted(set(allowed_paths))) + "\n",
            encoding="utf-8",
        )
        return subprocess.run(
            _isolated_generator_command(
                self.root,
                REPOSITORY_ROOT / "scripts" / "sync_canonical_mirrors.py",
                "python3",
                "validate-branch-history",
                "--target-root",
                str(self.repository),
                "--base-ref",
                base_sha,
                "--head-ref",
                head_sha,
                "--allowed-paths-file",
                str(self.allowed_paths_file),
                *extra_arguments,
            ),
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_rejects_transient_add_delete_rename_and_secret_blobs(self) -> None:
        cases = ("add-delete", "rename-back", "secret-delete")
        for case in cases:
            with self.subTest(case=case):
                self._git("reset", "--hard", self.base_sha)
                if case == "add-delete":
                    self._write("outside.txt", "transient\n")
                    self._commit("add outside")
                    (self.repository / "outside.txt").unlink()
                    head_sha = self._commit("delete outside")
                    allowed = ["seed.txt"]
                    expected = "outside the generated allowed set"
                elif case == "rename-back":
                    self._git("mv", "seed.txt", "outside.txt")
                    self._commit("rename outside")
                    self._git("mv", "outside.txt", "seed.txt")
                    head_sha = self._commit("rename back")
                    allowed = ["seed.txt"]
                    expected = "outside the generated allowed set"
                else:
                    self._write(
                        "generated.txt",
                        "github_pat_" + "A" * 48 + "\n",
                    )
                    self._commit("add transient secret")
                    (self.repository / "generated.txt").unlink()
                    head_sha = self._commit("delete transient secret")
                    allowed = ["generated.txt", "seed.txt"]
                    expected = "high-confidence secret marker"

                rejected = self._validate(
                    self.base_sha,
                    head_sha,
                    allowed,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(
                    expected,
                    rejected.stdout + rejected.stderr,
                )

    def test_rejects_private_key_headers_across_transient_history_shapes(
        self,
    ) -> None:
        private_key_kinds = (
            "ENCRYPTED PRIVATE KEY",
            "DSA PRIVATE KEY",
            "PGP PRIVATE KEY BLOCK",
        )
        history_shapes = ("add-delete", "rename-back", "merge")
        for kind_index, private_key_kind in enumerate(private_key_kinds):
            marker = f"{'-' * 5}BEGIN {private_key_kind}{'-' * 5}\n"
            for shape in history_shapes:
                with self.subTest(kind=private_key_kind, shape=shape):
                    self._git("switch", "--detach", "-q", self.base_sha)
                    generated = "generated.txt"
                    transient = "transient.txt"
                    allowed = [generated, transient]
                    if shape == "add-delete":
                        self._write(generated, marker)
                        self._commit(f"add {private_key_kind}")
                        (self.repository / generated).unlink()
                        head_sha = self._commit(f"delete {private_key_kind}")
                    elif shape == "rename-back":
                        self._write(generated, marker)
                        self._commit(f"add {private_key_kind}")
                        self._git("mv", generated, transient)
                        self._commit(f"rename {private_key_kind} out")
                        self._git("mv", transient, generated)
                        self._write(generated, "safe generated content\n")
                        head_sha = self._commit(f"rename {private_key_kind} back")
                    else:
                        side = f"secret-side-{kind_index}"
                        generated_branch = f"secret-generated-{kind_index}"
                        self._git(
                            "switch",
                            "-q",
                            "-c",
                            side,
                            self.base_sha,
                        )
                        self._write(generated, marker)
                        self._commit(f"side add {private_key_kind}")
                        (self.repository / generated).unlink()
                        self._commit(f"side delete {private_key_kind}")
                        self._git(
                            "switch",
                            "-q",
                            "-c",
                            generated_branch,
                            self.base_sha,
                        )
                        self._write(generated, "safe generated content\n")
                        self._commit("generated branch change")
                        self._git(
                            "merge",
                            "--no-ff",
                            "-q",
                            "-m",
                            f"merge {private_key_kind} history",
                            side,
                        )
                        head_sha = self._git("rev-parse", "HEAD")

                    rejected = self._validate(
                        self.base_sha,
                        head_sha,
                        allowed,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(
                        "high-confidence secret marker",
                        rejected.stdout + rejected.stderr,
                    )

    def test_rejects_out_of_scope_merge_side_history(self) -> None:
        self._git("switch", "-q", "-c", "side", self.base_sha)
        self._write("outside.txt", "side branch\n")
        self._commit("side outside")
        self._git("switch", "-q", "-c", "generated", self.base_sha)
        self._write("seed.txt", "generated\n")
        self._commit("generated change")
        self._git("merge", "--no-ff", "-q", "-m", "merge side", "side")
        head_sha = self._git("rev-parse", "HEAD")

        rejected = self._validate(
            self.base_sha,
            head_sha,
            ["seed.txt"],
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "outside the generated allowed set",
            rejected.stdout + rejected.stderr,
        )

    def test_octopus_topology_passes_and_parent_cap_tightens(self) -> None:
        branches: list[str] = []
        allowed = ["seed.txt"]
        for index in range(3):
            branch = f"side-{index}"
            path = f"generated-{index}.txt"
            branches.append(branch)
            allowed.append(path)
            self._git("switch", "-q", "-c", branch, self.base_sha)
            self._write(path, f"{index}\n")
            self._commit(branch)
        self._git("switch", "-q", "master")
        self._git(
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "octopus",
            *branches,
        )
        head_sha = self._git("rev-parse", "HEAD")

        accepted = self._validate(self.base_sha, head_sha, allowed)
        self.assertEqual(
            accepted.returncode,
            0,
            accepted.stdout + accepted.stderr,
        )
        receipt = json.loads(accepted.stdout)
        self.assertEqual(receipt["head_sha"], head_sha)
        self.assertGreaterEqual(receipt["parent_edge_count"], 6)

        rejected = self._validate(
            self.base_sha,
            head_sha,
            allowed,
            "--max-parents-per-commit",
            "3",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("3-parent limit", rejected.stdout + rejected.stderr)

    def test_commit_blob_and_base_retarget_caps_fail_closed(self) -> None:
        self._write("generated.txt", "one\n")
        first = self._commit("generated one")
        self._write("generated.txt", "two\n")
        self._commit("generated two")
        self._write("generated.txt", "three\n")
        head_sha = self._commit("generated three")
        allowed = ["generated.txt", "seed.txt"]

        commit_cap = self._validate(
            self.base_sha,
            head_sha,
            allowed,
            "--max-commits",
            "2",
        )
        self.assertNotEqual(commit_cap.returncode, 0)
        self.assertIn("2-commit limit", commit_cap.stdout + commit_cap.stderr)

        self._git("switch", "--detach", "-q", first)
        blob_cap = self._validate(
            self.base_sha,
            first,
            allowed,
            "--max-blob-bytes",
            "1",
        )
        self.assertNotEqual(blob_cap.returncode, 0)
        self.assertIn(
            "logical-blob-byte limit",
            blob_cap.stdout + blob_cap.stderr,
        )

        fresh = self._validate(
            self.base_sha,
            first,
            allowed,
            "--require-fresh-head",
        )
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        first_receipt = json.loads(fresh.stdout)

        self._git("switch", "-q", "-c", "retarget", self.base_sha)
        self._write("retarget.txt", "retarget\n")
        retarget_sha = self._commit("retarget base")
        self._git("switch", "--detach", "-q", first)
        retargeted = self._validate(
            retarget_sha,
            first,
            [*allowed, "retarget.txt"],
        )
        self.assertEqual(
            retargeted.returncode,
            0,
            retargeted.stdout + retargeted.stderr,
        )
        retargeted_receipt = json.loads(retargeted.stdout)
        self.assertNotEqual(
            first_receipt["history_sha256"],
            retargeted_receipt["history_sha256"],
        )
        self.assertEqual(retargeted_receipt["base_sha"], retarget_sha)

        stale_fresh = self._validate(
            retarget_sha,
            first,
            [*allowed, "retarget.txt"],
            "--require-fresh-head",
        )
        self.assertNotEqual(stale_fresh.returncode, 0)
        self.assertIn(
            "not based on the exact target base",
            stale_fresh.stdout + stale_fresh.stderr,
        )

    def test_limit_overrides_cannot_expand_compiled_caps(self) -> None:
        self._write("generated.txt", "generated\n")
        head_sha = self._commit("generated")
        rejected = self._validate(
            self.base_sha,
            head_sha,
            ["generated.txt", "seed.txt"],
            "--max-commits",
            "257",
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "cannot exceed the compiled maximum",
            rejected.stdout + rejected.stderr,
        )


if __name__ == "__main__":
    unittest.main()
