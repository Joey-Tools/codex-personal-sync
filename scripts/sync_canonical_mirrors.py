#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field, replace
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import select
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, NoReturn, Union
import unicodedata


REPOSITORY_ROOT = Path(os.path.abspath(Path(__file__).parent.parent))
LOCK_PATH = PurePosixPath("sync-source-lock.json")
GENERATOR_PATH = PurePosixPath("scripts/sync_canonical_mirrors.py")
RULES_PATH = PurePosixPath("AGENTS.md")
RECEIPT_PATH = PurePosixPath("generated-sync-source-lock.json")
TRANSACTION_PATH = PurePosixPath(".generated-sync-transaction.json")
TRANSACTION_TEMP_PATH = PurePosixPath(".generated-sync-transaction.pending")
TRANSACTION_COMPLETE_PATH = PurePosixPath(".generated-sync-transaction.complete")
LOCK_VERSION = 1
RECEIPT_VERSION = 1
SYNC_HISTORY_RECEIPT_VERSION = 2
GENERATOR_CONTRACT_VERSION = 2
RULES_CONTRACT_VERSION = 1
HASH_ALGORITHM = "sha256"
MAX_JSON_INTEGER_DIGITS = 4300
GIT_EXECUTABLE = Path("/usr/bin/git")
MACOS_GIT_LOCATOR_EXECUTABLE = Path("/usr/bin/xcrun")
MACOS_GIT_LOCATOR_TEMP_DIRECTORY = Path("/private/tmp")
MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY = (0o1777, 0, 0)
GIT_DERIVED_CACHE_DISABLE_ARGUMENTS = (
    "-c",
    "core.commitGraph=false",
    "-c",
    "core.multiPackIndex=false",
)
PRIVATE_CONTROL_PRIMARY_ROOT_ID = "primary-home-v1"
PRIVATE_CONTROL_LEGACY_ROOT_ID = "legacy-shared-v0"
PRIVATE_CONTROL_NAMESPACE_NAME = ".codex-sync-canonical-mirrors-v1"
PRIVATE_CONTROL_LEGACY_PARENT = Path(
    "/private/tmp" if sys.platform == "darwin" else "/var/tmp"
)
PRIVATE_CONTROL_REASON_LEGACY_PENDING = "legacy-recovery-pending"
PRIVATE_CONTROL_REASON_INCONCLUSIVE = "private-control-root-inconclusive"
PRIVATE_CONTROL_MAX_ANCESTORS = 256
PRIVATE_CONTROL_PREALLOCATION_ALLOWED_STATES = frozenset(
    {
        "absent",
        "adopted-retained-in-place",
        "duplicate",
        "foreign-unrelated",
        "same-uid-empty",
    }
)
PRIVATE_CONTROL_RECOVERY_CONTRACT = "codex-private-control-recovery"
PRIVATE_CONTROL_RECOVERY_VERSION = 1
PRIVATE_CONTROL_RECOVERY_DISPOSITION = "adopt-retained-in-place"
PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME = (
    ".legacy-shared-v0-retained-in-place-receipt-v1.json"
)
PRIVATE_CONTROL_RECOVERY_MARKER_NAME = (
    ".legacy-shared-v0-retained-in-place-cutover-v1.json"
)
PRIVATE_CONTROL_RECOVERY_LOCK_ORDER = ("tool-root", "quarantine")
PRIVATE_CONTROL_RECOVERY_LOCK_MODE = "LOCK_EX|LOCK_NB"
PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES = 100_000
PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES = 512 * 1024 * 1024
PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES = 1024 * 1024 * 1024
PRIVATE_CONTROL_RECOVERY_MAX_DEPTH = 256
PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES = 4 * 1024 * 1024
PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES = 64 * 1024 * 1024
PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES = 8
PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES = (
    8 * PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
)
PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class PrivateControlRootSpec:
    root_id: str
    parent_path: Path
    allocate: bool
    account_home: Path | None
    shared_parent: bool


def _private_control_legacy_ownership_state(
    children: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...],
    *,
    effective_uid: int | None = None,
) -> str:
    if not children:
        return "absent"
    selected_uid = os.geteuid() if effective_uid is None else effective_uid
    owners = {child[1][1] for child in children}
    if owners == {selected_uid}:
        return "same-uid"
    if selected_uid not in owners:
        return "foreign-unrelated"
    return "inconclusive"


def _private_control_preallocation_decision(
    legacy_states: tuple[str, ...],
) -> tuple[bool, str | None]:
    if PRIVATE_CONTROL_REASON_LEGACY_PENDING in legacy_states:
        return False, PRIVATE_CONTROL_REASON_LEGACY_PENDING
    if PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH in legacy_states:
        return False, PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
    if any(
        state not in PRIVATE_CONTROL_PREALLOCATION_ALLOWED_STATES
        for state in legacy_states
    ):
        return False, PRIVATE_CONTROL_REASON_INCONCLUSIVE
    return True, None


def _private_control_preflight_failure_reason(
    first_error: BaseException,
    coverage_states: tuple[str, ...],
) -> str:
    error_detail = str(first_error)
    if PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH in error_detail:
        return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
    if (
        PRIVATE_CONTROL_REASON_LEGACY_PENDING in error_detail
        or PRIVATE_CONTROL_REASON_LEGACY_PENDING in coverage_states
    ):
        return PRIVATE_CONTROL_REASON_LEGACY_PENDING
    return PRIVATE_CONTROL_REASON_INCONCLUSIVE


def _canonical_account_home_directory() -> Path:
    try:
        account = pwd.getpwuid(os.geteuid())
    except KeyError as error:
        raise RuntimeError(
            "cannot resolve the current account home directory"
        ) from error
    if account.pw_uid != os.geteuid() or not account.pw_dir:
        raise RuntimeError("cannot resolve the current account home directory")
    path = Path(account.pw_dir)
    if not path.is_absolute():
        raise RuntimeError("current account home directory must be absolute")
    return Path(os.path.abspath(path))


def _private_control_root_specs() -> tuple[PrivateControlRootSpec, ...]:
    account_home = _canonical_account_home_directory()
    return (
        PrivateControlRootSpec(
            root_id=PRIVATE_CONTROL_PRIMARY_ROOT_ID,
            parent_path=account_home / PRIVATE_CONTROL_NAMESPACE_NAME,
            allocate=True,
            account_home=account_home,
            shared_parent=False,
        ),
        PrivateControlRootSpec(
            root_id=PRIVATE_CONTROL_LEGACY_ROOT_ID,
            parent_path=PRIVATE_CONTROL_LEGACY_PARENT,
            allocate=False,
            account_home=None,
            shared_parent=True,
        ),
    )


PRIVATE_CONTROL_ROOT_SPECS = _private_control_root_specs()
PRIVATE_GIT_CONTROL_PARENT = PRIVATE_CONTROL_ROOT_SPECS[0].parent_path
PRIVATE_TOOL_ROOT_NAME = "codex-sync-canonical-mirrors"
PRIVATE_OWNER_RECORD_LEGACY_VERSION = 1
PRIVATE_OWNER_RECORD_VERSION = 2
PRIVATE_OWNER_RECORD_LEGACY_FIELDS = frozenset(
    {
        "version",
        "owner_pid",
        "owner_uid",
        "owner_gid",
        "owner_nonce",
        "phase",
        "private_name",
        "private_identity",
    }
)
PRIVATE_OWNER_RECORD_FIELDS = PRIVATE_OWNER_RECORD_LEGACY_FIELDS | {"root_id"}
PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH = "private-owner-root-mismatch"
PRIVATE_OWNER_RECORD_PHASES = frozenset({"building", "ready", "cleanup"})
MAX_PRIVATE_OWNER_RECORD_BYTES = 4096


def _private_owner_record_root_scope(
    record: object,
    expected_root_id: str,
) -> str:
    if not isinstance(record, dict):
        return "generic-invalid"
    version = record.get("version")
    fields = set(record)
    if type(version) is int and version == PRIVATE_OWNER_RECORD_LEGACY_VERSION:
        if fields != PRIVATE_OWNER_RECORD_LEGACY_FIELDS:
            return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
        if expected_root_id != PRIVATE_CONTROL_LEGACY_ROOT_ID:
            return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
        return "accepted-legacy"
    if type(version) is int and version == PRIVATE_OWNER_RECORD_VERSION:
        if fields != PRIVATE_OWNER_RECORD_FIELDS:
            return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
        root_id = record.get("root_id")
        if not isinstance(root_id, str) or root_id != expected_root_id:
            return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
        return "accepted-current"
    # Unknown record versions are not safe to rewrite or quarantine. A future
    # writer may have extended the ownership contract in ways this runtime does
    # not understand, so retain the record under the stable root-scope reason.
    if "version" in record or "root_id" in record:
        return PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH
    return "generic-invalid"


PRIVATE_GIT_EXECUTABLE_PREFIX = ".bound-git-executable."
MAX_PRIVATE_GIT_EXECUTABLE_ATTEMPTS = 32
DURABLE_QUARANTINE_ROOT_NAME = ".codex-sync-canonical-mirror-quarantine"
MAX_DURABLE_QUARANTINE_ENTRIES = 10_000
QUARANTINE_RECOVERY_KIND = "recovery"
QUARANTINE_TRANSIENT_KIND = "transient"
PRIVATE_OBJECTS_PATH = PurePosixPath("objects")
MAX_LOCK_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_GIT_STDOUT_BYTES = MAX_SOURCE_BYTES
MAX_GIT_STDERR_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
GIT_CLEANUP_TIMEOUT_SECONDS = 5
MINIMUM_GIT_VERSION = (2, 45, 0)
MAX_GIT_VERSION_STDOUT_BYTES = 1024
MAX_GIT_VERSION_STDERR_BYTES = 4096
MAX_GIT_LOCATOR_STDOUT_BYTES = 4096
MAX_GIT_SNAPSHOT_ENTRIES = 100_000
MAX_GIT_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_CONSUMER_TRACKED_ENTRIES = 100_000
MAX_SYNC_HISTORY_ALLOWED_PATH_BYTES = 1024 * 1024
MAX_SYNC_HISTORY_COMMITS = 256
MAX_SYNC_HISTORY_PARENTS_PER_COMMIT = 16
MAX_SYNC_HISTORY_PARENT_EDGES = 1024
MAX_SYNC_HISTORY_PATH_EVENTS = 100_000
MAX_SYNC_HISTORY_PATH_BYTES = 8 * 1024 * 1024
MAX_SYNC_HISTORY_BLOBS = 100_000
MAX_SYNC_HISTORY_BLOB_BYTES = 512 * 1024 * 1024
SYNC_HISTORY_SECRET_PATTERNS = (
    re.compile(
        rb"-----BEGIN (?:(?:ENCRYPTED|RSA|DSA|EC|OPENSSH) PRIVATE KEY|"
        rb"PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
    ),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,255}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{32,255}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b"),
)
MAX_ROOT_ANCESTOR_DEPTH = 256
MAX_TOOL_ROOT_ENTRIES = 256
MAX_STALE_SNAPSHOTS_PER_OPERATION = 8
MAX_OPERATION_BYTES = 16 * 1024 * 1024 * 1024
MAX_OPERATION_ENTRIES = 2_000_000
OPERATION_TIMEOUT_SECONDS = 300
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_VERSION_RE = re.compile(
    rb"git version "
    rb"([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})"
    rb"(?:[ .+()A-Za-z0-9_-]*)\n"
)
MODE_RE = re.compile(r"^0[0-7]{3}$")
SUPPORTED_SOURCE_MODES = frozenset({0o644, 0o755})
PRIVATE_SNAPSHOT_RE = re.compile(r"^sync-canonical-git-control\.[0-9]+\.[0-9a-f]{32}$")
QUARANTINE_FILE_RE = re.compile(
    r"^(recovery|transient)-file-"
    r"([0-9a-f]+)-([0-9a-f]+)-([0-7]{4})-"
    r"([0-9a-f]+)-([0-9a-f]+)-([0-9a-f]{16})-([0-9a-f]{32})$"
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_WRITE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class MirrorSyncError(RuntimeError):
    pass


def _bounded_json_integer(raw_value: str) -> int:
    digits = raw_value[1:] if raw_value.startswith("-") else raw_value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits")
    return int(raw_value)


def _reject_json_constant(raw_value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant is not allowed: {raw_value}")


class _PrivateControlRecoveryInitialAbsence(MirrorSyncError):
    pass


class MissingPathError(MirrorSyncError):
    pass


@dataclass
class _PrivateControlRecoveryBinding:
    label: str
    path: Path
    fd: int
    identity: tuple[int, int, int]
    access: tuple[int, int, int]
    locked: bool = False
    close_state: str = "open"


@dataclass
class _PrivateControlRecoveryPendingDescriptorCustody:
    label: str
    path: Path
    fd: int
    parent_identity: tuple[int, int, int]
    identity: tuple[int, int, int] | None
    access: tuple[int, int, int] | None
    state: str = "open"


_PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY: tuple[
    _PrivateControlRecoveryPendingDescriptorCustody, ...
] = ()
_PC_RECOVERY_RETAINED_CLOSE_FENCE: tuple[_PrivateControlRecoveryBinding, ...] = ()


_PRIVATE_CONTROL_RECOVERY_PLAN_FIELDS = frozenset(
    {
        "caps",
        "contract",
        "disposition",
        "inventory",
        "leases",
        "paths",
        "plan_digest",
        "primary_root_id",
        "root_id",
        "roots",
        "segment_locator",
        "version",
    }
)
_PRIVATE_CONTROL_RECOVERY_MARKER_FIELDS = frozenset(
    {
        "contract",
        "disposition",
        "plan_digest",
        "primary_receipt",
        "root_id",
        "status",
        "terminal_registry",
        "version",
    }
)
_PRIVATE_CONTROL_RECOVERY_PRIMARY_RECEIPT_FIELDS = frozenset(
    {
        "contract",
        "disposition",
        "plan",
        "plan_digest",
        "publication",
        "root_id",
        "version",
    }
)


def _pc_recovery_json_bytes(value: object, *, pretty: bool) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _pc_recovery_digest(value: object) -> str:
    return hashlib.sha256(_pc_recovery_json_bytes(value, pretty=False)).hexdigest()


def _pc_recovery_canonical_documents_equal(left: object, right: object) -> bool:
    try:
        return _pc_recovery_json_bytes(
            left,
            pretty=False,
        ) == _pc_recovery_json_bytes(right, pretty=False)
    except (TypeError, ValueError):
        return False


def _pc_recovery_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _pc_recovery_access(metadata: os.stat_result) -> tuple[int, int, int]:
    return stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid


def _pc_recovery_identity_document(
    identity: tuple[int, int, int],
) -> dict[str, int]:
    return {"dev": identity[0], "ino": identity[1], "type": identity[2]}


def _pc_recovery_access_document(
    access: tuple[int, int, int],
) -> dict[str, int | str]:
    return {"gid": access[2], "mode": f"{access[0]:04o}", "uid": access[1]}


def _pc_recovery_record(
    identity: tuple[int, int, int],
    access: tuple[int, int, int],
) -> dict[str, object]:
    return {
        "access": _pc_recovery_access_document(access),
        "identity": _pc_recovery_identity_document(identity),
    }


def _pc_recovery_root_specs() -> tuple[PrivateControlRootSpec, ...]:
    return PRIVATE_CONTROL_ROOT_SPECS


def _pc_recovery_specs(
    requested_root_id: str,
) -> tuple[PrivateControlRootSpec, PrivateControlRootSpec]:
    if requested_root_id != PRIVATE_CONTROL_LEGACY_ROOT_ID:
        raise MirrorSyncError(
            "recover-private-control supports only --root-id "
            f"{PRIVATE_CONTROL_LEGACY_ROOT_ID}"
        )
    specs = _pc_recovery_root_specs()
    root_ids = [spec.root_id for spec in specs]
    if len(root_ids) != len(set(root_ids)):
        raise MirrorSyncError("private-control root ids must be unique")
    primary = [spec for spec in specs if spec.allocate]
    legacy = [spec for spec in specs if spec.root_id == requested_root_id]
    if len(primary) != 1 or len(legacy) != 1:
        raise MirrorSyncError("private-control recovery registry is incomplete")
    primary_spec = primary[0]
    legacy_spec = legacy[0]
    if (
        primary_spec.shared_parent
        or primary_spec.account_home is None
        or primary_spec.parent_path
        != primary_spec.account_home / PRIVATE_CONTROL_NAMESPACE_NAME
        or legacy_spec.allocate
        or not legacy_spec.shared_parent
        or legacy_spec.account_home is not None
    ):
        raise MirrorSyncError("private-control recovery root schema is invalid")
    return primary_spec, legacy_spec


def _pc_recovery_close_bindings(
    bindings: tuple[_PrivateControlRecoveryBinding | None, ...],
) -> None:
    bindings_by_fd: dict[int, list[_PrivateControlRecoveryBinding]] = {}
    for binding in bindings:
        if binding is None or binding.fd < 0:
            continue
        bindings_by_fd.setdefault(binding.fd, []).append(binding)
    retained = tuple(
        binding
        for same_fd_bindings in bindings_by_fd.values()
        for binding in same_fd_bindings
    )
    if not retained:
        return
    if (
        _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
        or _PC_RECOVERY_RETAINED_CLOSE_FENCE
    ):
        _pc_recovery_retain_close_fence(retained)
        _pc_recovery_require_no_close_fence()
    _pc_recovery_retain_close_fence(retained)
    for file_fd, same_fd_bindings in bindings_by_fd.items():
        binding = same_fd_bindings[0]
        for item in same_fd_bindings:
            item.close_state = "close-uncertain"
        try:
            os.close(file_fd)
        except BaseException as error:
            if isinstance(error, OSError):
                raise MirrorSyncError(
                    f"cannot close {binding.label}: {error}"
                ) from error
            raise
        for item in same_fd_bindings:
            item.fd = -1
            item.locked = False
            item.close_state = "closed"
        _pc_recovery_release_close_fence(tuple(same_fd_bindings))


def _pc_recovery_prepare_pending_descriptor(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
    identity: tuple[int, int, int] | None,
    access: tuple[int, int, int] | None,
) -> _PrivateControlRecoveryPendingDescriptorCustody:
    global _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
    custody = _PrivateControlRecoveryPendingDescriptorCustody(
        label=label,
        path=parent.path / name,
        fd=-1,
        parent_identity=parent.identity,
        identity=identity,
        access=access,
        state="opening",
    )
    _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY += (custody,)
    return custody


def _pc_recovery_activate_pending_descriptor(
    custody: _PrivateControlRecoveryPendingDescriptorCustody,
    file_fd: int,
) -> None:
    custody.fd = file_fd
    custody.state = "open"


def _pc_recovery_release_pending_descriptor(
    custody: _PrivateControlRecoveryPendingDescriptorCustody,
) -> None:
    global _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
    if custody.fd < 0 or custody.state != "open":
        raise MirrorSyncError(
            f"pending {custody.label} descriptor cannot be handed off"
        )
    custody.state = "handed-off"
    _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = tuple(
        item
        for item in _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
        if item is not custody
    )


def _pc_recovery_open_pending_descriptor(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
    flags: int,
    mode: int | None,
    identity: tuple[int, int, int] | None,
    access: tuple[int, int, int] | None,
) -> _PrivateControlRecoveryPendingDescriptorCustody:
    global _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
    custody = _pc_recovery_prepare_pending_descriptor(
        parent,
        name,
        label,
        identity,
        access,
    )
    file_fd = -1
    try:
        if mode is None:
            file_fd = os.open(name, flags, dir_fd=parent.fd)
        else:
            file_fd = os.open(name, flags, mode, dir_fd=parent.fd)
        _pc_recovery_activate_pending_descriptor(custody, file_fd)
        return custody
    except BaseException:
        if file_fd < 0:
            custody.state = "open-result-uncertain"
        else:
            custody.fd = file_fd
            custody.state = "open"
            _pc_recovery_close_pending_descriptor(custody)
        raise


def _pc_recovery_close_pending_descriptor(
    custody: _PrivateControlRecoveryPendingDescriptorCustody,
) -> None:
    global _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
    custody.state = "close-uncertain"
    try:
        os.close(custody.fd)
    except BaseException as error:
        if isinstance(error, OSError):
            raise MirrorSyncError(
                f"cannot close pending {custody.label} descriptor: {error}"
            ) from error
        raise
    custody.state = "closed"
    _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY = tuple(
        item
        for item in _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
        if item is not custody
    )


def _pc_recovery_retain_close_fence(
    bindings: tuple[_PrivateControlRecoveryBinding | None, ...],
) -> None:
    global _PC_RECOVERY_RETAINED_CLOSE_FENCE
    retained = list(_PC_RECOVERY_RETAINED_CLOSE_FENCE)
    seen = {id(binding) for binding in retained}
    for binding in bindings:
        if binding is None or binding.fd < 0 or id(binding) in seen:
            continue
        seen.add(id(binding))
        retained.append(binding)
    _PC_RECOVERY_RETAINED_CLOSE_FENCE = tuple(retained)


def _pc_recovery_release_close_fence(
    bindings: tuple[_PrivateControlRecoveryBinding, ...],
) -> None:
    global _PC_RECOVERY_RETAINED_CLOSE_FENCE
    released = {id(binding) for binding in bindings}
    _PC_RECOVERY_RETAINED_CLOSE_FENCE = tuple(
        binding
        for binding in _PC_RECOVERY_RETAINED_CLOSE_FENCE
        if id(binding) not in released
    )


def _pc_recovery_require_no_close_fence() -> None:
    if (
        _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
        or _PC_RECOVERY_RETAINED_CLOSE_FENCE
    ):
        raise MirrorSyncError(
            "recovery descriptor close remains uncertain; "
            "restart the process before another recovery attempt"
        )


def _pc_recovery_bind_directory(
    path: Path,
    label: str,
) -> _PrivateControlRecoveryBinding:
    path = Path(os.path.abspath(path))
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {path}: {error}") from error
    if not stat.S_ISDIR(path_metadata.st_mode) or stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must be a non-symlink directory: {path}")
    try:
        directory_fd = os.open(path, _DIRECTORY_FLAGS)
        descriptor_metadata = os.fstat(directory_fd)
    except OSError as error:
        if "directory_fd" in locals():
            os.close(directory_fd)
        raise MirrorSyncError(f"cannot bind {label}: {path}: {error}") from error
    if _pc_recovery_identity(path_metadata) != _pc_recovery_identity(
        descriptor_metadata
    ) or _pc_recovery_access(path_metadata) != _pc_recovery_access(descriptor_metadata):
        os.close(directory_fd)
        raise MirrorSyncError(f"{label} changed while binding it: {path}")
    return _PrivateControlRecoveryBinding(
        label=label,
        path=path,
        fd=directory_fd,
        identity=_pc_recovery_identity(descriptor_metadata),
        access=_pc_recovery_access(descriptor_metadata),
    )


def _pc_recovery_bind_child_directory(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
) -> _PrivateControlRecoveryBinding:
    try:
        path_metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise _PrivateControlRecoveryInitialAbsence(
            f"cannot inspect {label}: {parent.path / name}: {error}"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inspect {label}: {parent.path / name}: {error}"
        ) from error
    if not stat.S_ISDIR(path_metadata.st_mode) or stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(
            f"{label} must be a non-symlink directory: {parent.path / name}"
        )
    try:
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.fd)
        descriptor_metadata = os.fstat(directory_fd)
    except OSError as error:
        if "directory_fd" in locals():
            os.close(directory_fd)
        raise MirrorSyncError(
            f"cannot bind {label}: {parent.path / name}: {error}"
        ) from error
    identity = _pc_recovery_identity(descriptor_metadata)
    access = _pc_recovery_access(descriptor_metadata)
    if (
        _pc_recovery_identity(path_metadata) != identity
        or _pc_recovery_access(path_metadata) != access
    ):
        os.close(directory_fd)
        raise MirrorSyncError(f"{label} changed while binding it")
    return _PrivateControlRecoveryBinding(
        label=label,
        path=parent.path / name,
        fd=directory_fd,
        identity=identity,
        access=access,
    )


def _pc_recovery_revalidate_directory(
    binding: _PrivateControlRecoveryBinding,
) -> None:
    try:
        path_metadata = os.stat(binding.path, follow_symlinks=False)
        descriptor_metadata = os.fstat(binding.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} became unavailable: {binding.path}: {error}"
        ) from error
    for metadata in (path_metadata, descriptor_metadata):
        if _pc_recovery_identity(metadata) != binding.identity:
            raise MirrorSyncError(f"{binding.label} was replaced")
        if _pc_recovery_access(metadata) != binding.access:
            raise MirrorSyncError(f"{binding.label} access policy changed")


def _pc_recovery_fsync_directory(
    binding: _PrivateControlRecoveryBinding,
) -> None:
    try:
        os.fsync(binding.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot durably bind {binding.label}: {error}"
        ) from error
    _pc_recovery_revalidate_directory(binding)


def _pc_recovery_acquire_exclusive(
    binding: _PrivateControlRecoveryBinding,
) -> None:
    try:
        fcntl.flock(binding.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise MirrorSyncError(f"{binding.label} is busy") from error
    except OSError as error:
        raise MirrorSyncError(
            f"cannot acquire exclusive {binding.label} lease: {error}"
        ) from error
    binding.locked = True


def _pc_recovery_capacity_bytes(metadata: os.stat_result) -> int:
    blocks = getattr(metadata, "st_blocks", 0)
    if type(blocks) is not int or blocks < 0:
        raise MirrorSyncError("filesystem returned an invalid allocated-byte count")
    return blocks * 512


def _pc_recovery_check_deadline(deadline: float, label: str) -> None:
    if time.monotonic() > deadline:
        raise MirrorSyncError(f"private-control recovery timed out while {label}")


def _pc_recovery_write_all(
    file_fd: int,
    payload: bytes,
    *,
    label: str,
) -> None:
    deadline = time.monotonic() + PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS
    remaining = memoryview(payload)
    while remaining:
        _pc_recovery_check_deadline(deadline, label)
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError("private-control recovery write made no progress")
        remaining = remaining[written:]


def _pc_recovery_read_file(
    parent_fd: int,
    name: str,
    path: Path,
    expected: os.stat_result,
    *,
    deadline: float,
    operation: OperationBudget | None = None,
    payload_limit: int | None,
) -> tuple[int, str, bytes | None, os.stat_result]:
    try:
        file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot bind recovery evidence file {path}: {error}"
        ) from error
    try:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or _pc_recovery_identity(before) != _pc_recovery_identity(expected)
            or _pc_recovery_access(before) != _pc_recovery_access(expected)
            or before.st_size != expected.st_size
        ):
            raise MirrorSyncError(
                f"recovery evidence file changed while binding: {path}"
            )

        if payload_limit is not None and before.st_size > payload_limit:
            raise MirrorSyncError(
                f"recovery evidence payload exceeds its capture limit: {path}"
            )

        def read_once(*, capture: bool) -> tuple[int, str, bytes | None]:
            _operation_checkpoint(operation, f"reading {path}")
            _pc_recovery_check_deadline(deadline, f"reading {path}")
            os.lseek(file_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            payload = bytearray() if capture else None
            size = 0
            while size <= before.st_size:
                _operation_checkpoint(operation, f"reading {path}")
                _pc_recovery_check_deadline(deadline, f"reading {path}")
                chunk = os.read(
                    file_fd,
                    min(1024 * 1024, before.st_size + 1 - size),
                )
                if not chunk:
                    break
                size += len(chunk)
                _consume_operation_budget(
                    operation,
                    byte_count=len(chunk),
                    label=f"reading private-control recovery evidence {path}",
                )
                digest.update(chunk)
                if payload is not None:
                    payload.extend(chunk)
                    if payload_limit is None or len(payload) > payload_limit:
                        raise MirrorSyncError(
                            f"recovery evidence payload exceeds its capture limit: {path}"
                        )
            if size != before.st_size:
                raise MirrorSyncError(f"recovery evidence file size changed: {path}")
            return (
                size,
                digest.hexdigest(),
                bytes(payload) if payload is not None else None,
            )

        first_size, first_digest, payload = read_once(capture=payload_limit is not None)
        second_size, second_digest, _second_payload = read_once(capture=False)
        after = os.fstat(file_fd)
        final_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            first_size != second_size
            or first_digest != second_digest
            or _pc_recovery_identity(after) != _pc_recovery_identity(before)
            or _pc_recovery_access(after) != _pc_recovery_access(before)
            or after.st_size != before.st_size
            or _pc_recovery_identity(final_path) != _pc_recovery_identity(before)
            or _pc_recovery_access(final_path) != _pc_recovery_access(before)
            or final_path.st_size != before.st_size
        ):
            raise MirrorSyncError(
                f"recovery evidence file changed while reading: {path}"
            )
        return first_size, first_digest, payload, after
    except OSError as error:
        raise MirrorSyncError(
            f"cannot read recovery evidence file {path}: {error}"
        ) from error
    finally:
        os.close(file_fd)


def _pc_recovery_owner_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate owner field: {key}")
        result[key] = value
    return result


def _pc_recovery_owner_candidate(relative_path: str) -> bool:
    if "/" in relative_path or not relative_path.endswith(".owner.json"):
        return False
    private_name = relative_path[: -len(".owner.json")]
    return PRIVATE_SNAPSHOT_RE.fullmatch(private_name) is not None


def _pc_recovery_decode_owner(
    relative_path: str,
    payload: bytes,
    access: tuple[int, int, int],
) -> dict[str, object] | None:
    if not _pc_recovery_owner_candidate(relative_path):
        return None
    private_name = relative_path[: -len(".owner.json")]
    if len(payload) > MAX_PRIVATE_OWNER_RECORD_BYTES:
        raise MirrorSyncError(
            f"legacy owner payload exceeds {MAX_PRIVATE_OWNER_RECORD_BYTES} bytes: "
            f"{relative_path}"
        )
    try:
        record = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pc_recovery_owner_pairs,
            parse_int=_bounded_json_integer,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise MirrorSyncError(
            f"legacy owner payload is invalid: {relative_path}: {error}"
        ) from error
    root_scope = _private_owner_record_root_scope(
        record,
        PRIVATE_CONTROL_LEGACY_ROOT_ID,
    )
    if not isinstance(record, dict) or root_scope not in {
        "accepted-legacy",
        "accepted-current",
    }:
        raise MirrorSyncError(
            f"legacy owner payload root/schema is unsupported: {relative_path}"
        )
    private_identity = record.get("private_identity")
    valid = (
        type(record.get("owner_pid")) is int
        and record["owner_pid"] > 0
        and type(record.get("owner_uid")) is int
        and record["owner_uid"] == os.geteuid()
        and type(record.get("owner_gid")) is int
        and record["owner_gid"] == os.getegid()
        and isinstance(record.get("owner_nonce"), str)
        and re.fullmatch(r"[0-9a-f]{32}", record["owner_nonce"]) is not None
        and isinstance(record.get("phase"), str)
        and record["phase"] in PRIVATE_OWNER_RECORD_PHASES
        and record.get("private_name") == private_name
        and isinstance(private_identity, list)
        and len(private_identity) == 3
        and all(type(item) is int and item >= 0 for item in private_identity)
        and access == (0o600, os.geteuid(), os.getegid())
    )
    if not valid:
        raise MirrorSyncError(
            f"legacy owner payload fields/policy are invalid: {relative_path}"
        )
    decoded = dict(record)
    decoded["root_scope"] = root_scope
    decoded["private_state"] = "unvalidated"
    return decoded


def _pc_recovery_protected_entry(entry: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in entry.items() if key != "allocated_bytes"}


def _pc_recovery_scan_directory(
    binding: _PrivateControlRecoveryBinding,
    segment: str,
    *,
    deadline: float,
    operation: OperationBudget | None,
    state: dict[str, int],
) -> tuple[list[dict[str, object]], str]:
    entries: list[dict[str, object]] = []

    def scan(
        directory_fd: int, relative_parent: str, depth: int
    ) -> tuple[list[dict[str, object]], str]:
        _operation_checkpoint(operation, f"inventorying {binding.path}")
        _pc_recovery_check_deadline(deadline, f"inventorying {binding.path}")
        if depth > PRIVATE_CONTROL_RECOVERY_MAX_DEPTH:
            raise MirrorSyncError(
                "private-control recovery manifest exceeds its depth cap"
            )
        names: list[str] = []
        collisions: dict[str, str] = {}
        remaining = PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES - state["entries"]
        try:
            with os.scandir(directory_fd) as iterator:
                for item in iterator:
                    _operation_checkpoint(
                        operation,
                        f"inventorying {binding.path}",
                    )
                    _pc_recovery_check_deadline(
                        deadline, f"inventorying {binding.path}"
                    )
                    name = item.name
                    if (
                        not isinstance(name, str)
                        or name in {"", ".", ".."}
                        or "/" in name
                        or "\x00" in name
                    ):
                        raise MirrorSyncError(
                            f"private-control recovery found an unsafe name: {name!r}"
                        )
                    try:
                        encoded_name = name.encode("utf-8", "strict")
                    except UnicodeEncodeError as error:
                        raise MirrorSyncError(
                            f"private-control recovery name is not strict UTF-8: {name!r}"
                        ) from error
                    collision_key = unicodedata.normalize("NFC", name).casefold()
                    prior = collisions.get(collision_key)
                    if prior is not None and prior != name:
                        raise MirrorSyncError(
                            "private-control recovery found a portable name alias: "
                            f"{prior!r} and {name!r}"
                        )
                    collisions[collision_key] = name
                    _consume_operation_budget(
                        operation,
                        byte_count=len(encoded_name),
                        entry_count=1,
                        label=(
                            "inventorying private-control recovery evidence "
                            f"{binding.path}"
                        ),
                    )
                    names.append(name)
                    if len(names) > remaining:
                        raise MirrorSyncError(
                            "private-control recovery manifest exceeds its entry cap"
                        )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inventory recovery evidence directory {binding.path}: {error}"
            ) from error
        names.sort()
        reserved_paths: list[tuple[str, str]] = []
        for name in names:
            relative_path = name if not relative_parent else f"{relative_parent}/{name}"
            byte_count = len(relative_path.encode("utf-8"))
            if (
                state["path_bytes"] + byte_count
                > PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES
            ):
                raise MirrorSyncError(
                    "private-control recovery manifest exceeds its path-byte cap"
                )
            state["path_bytes"] += byte_count
            state["entries"] += 1
            reserved_paths.append((name, relative_path))
        local: list[dict[str, object]] = []
        for name, relative_path in reserved_paths:
            _pc_recovery_check_deadline(deadline, f"inventorying {relative_path}")
            try:
                path_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot inspect recovery evidence {relative_path}: {error}"
                ) from error
            identity = _pc_recovery_identity(path_metadata)
            is_regular_file = stat.S_ISREG(path_metadata.st_mode)
            is_directory = stat.S_ISDIR(path_metadata.st_mode)
            if not is_regular_file and not is_directory:
                raise MirrorSyncError(
                    "private-control recovery rejects symlink/special evidence: "
                    f"{segment}/{relative_path}"
                )
            access = _pc_recovery_access(path_metadata)
            if access[1] != os.geteuid() or access[0] & 0o022:
                raise MirrorSyncError(
                    "recovery evidence owner/access policy is unsafe: "
                    f"{segment}/{relative_path}"
                )
            allocated = _pc_recovery_capacity_bytes(path_metadata)
            if (
                state["allocated_bytes"] + allocated
                > PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES
            ):
                raise MirrorSyncError(
                    "private-control recovery manifest exceeds its allocated-byte cap"
                )
            state["allocated_bytes"] += allocated
            if is_regular_file:
                if path_metadata.st_nlink != 1:
                    raise MirrorSyncError(
                        "private-control recovery rejects a hard-link alias: "
                        f"{segment}/{relative_path}"
                    )
                if path_metadata.st_size < 0 or (
                    state["logical_bytes"] + path_metadata.st_size
                    > PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES
                ):
                    raise MirrorSyncError(
                        "private-control recovery manifest exceeds its logical-byte cap"
                    )
                owner_candidate = segment == "tool-root" and (
                    _pc_recovery_owner_candidate(relative_path)
                )
                if (
                    owner_candidate
                    and path_metadata.st_size > MAX_PRIVATE_OWNER_RECORD_BYTES
                ):
                    raise MirrorSyncError(
                        "legacy owner payload exceeds "
                        f"{MAX_PRIVATE_OWNER_RECORD_BYTES} bytes: {relative_path}"
                    )
                size, digest, payload, final_metadata = _pc_recovery_read_file(
                    directory_fd,
                    name,
                    binding.path / relative_path,
                    path_metadata,
                    deadline=deadline,
                    operation=operation,
                    payload_limit=(
                        MAX_PRIVATE_OWNER_RECORD_BYTES if owner_candidate else None
                    ),
                )
                state["logical_bytes"] += size
                owner = (
                    _pc_recovery_decode_owner(relative_path, payload, access)
                    if owner_candidate and payload is not None
                    else None
                )
                record: dict[str, object] = {
                    "access": _pc_recovery_access_document(access),
                    "allocated_bytes": allocated,
                    "identity": _pc_recovery_identity_document(identity),
                    "locator": {"path": relative_path, "segment": segment},
                    "owner": owner,
                    "sha256": digest,
                    "size": size,
                    "type": "regular-file",
                }
                if _pc_recovery_identity(final_metadata) != identity:
                    raise MirrorSyncError(
                        f"recovery evidence identity changed: {segment}/{relative_path}"
                    )
            elif is_directory:
                child_fd = -1
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    child_metadata = os.fstat(child_fd)
                except OSError as error:
                    if child_fd >= 0:
                        os.close(child_fd)
                    raise MirrorSyncError(
                        f"cannot bind recovery evidence directory {relative_path}: {error}"
                    ) from error
                try:
                    if (
                        _pc_recovery_identity(child_metadata) != identity
                        or _pc_recovery_access(child_metadata) != access
                    ):
                        raise MirrorSyncError(
                            f"recovery evidence directory changed: {relative_path}"
                        )
                    descendants, directory_digest = scan(
                        child_fd,
                        relative_path,
                        depth + 1,
                    )
                    try:
                        final_path = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError as error:
                        raise MirrorSyncError(
                            "recovery evidence directory is missing during "
                            f"final revalidation: {segment}/{relative_path}"
                        ) from error
                    except OSError as error:
                        raise MirrorSyncError(
                            "cannot revalidate recovery evidence directory path "
                            f"{segment}/{relative_path}: {error}"
                        ) from error
                    try:
                        final_descriptor = os.fstat(child_fd)
                    except OSError as error:
                        raise MirrorSyncError(
                            "cannot revalidate recovery evidence directory "
                            f"descriptor {segment}/{relative_path}: {error}"
                        ) from error
                    for metadata in (final_path, final_descriptor):
                        if (
                            _pc_recovery_identity(metadata) != identity
                            or _pc_recovery_access(metadata) != access
                        ):
                            raise MirrorSyncError(
                                "recovery evidence directory identity/access changed: "
                                f"{relative_path}"
                            )
                finally:
                    os.close(child_fd)
                record = {
                    "access": _pc_recovery_access_document(access),
                    "allocated_bytes": allocated,
                    "identity": _pc_recovery_identity_document(identity),
                    "locator": {"path": relative_path, "segment": segment},
                    "owner": None,
                    "sha256": directory_digest,
                    "size": 0,
                    "type": "directory",
                }
                local.extend(descendants)
            else:
                raise MirrorSyncError(
                    "private-control recovery rejects symlink/special evidence: "
                    f"{segment}/{relative_path}"
                )
            local.append(record)
        protected_children = [
            _pc_recovery_protected_entry(item)
            for item in sorted(
                local,
                key=lambda item: (
                    str(item["locator"]["segment"]),
                    str(item["locator"]["path"]),
                ),
            )
            if str(item["locator"]["path"]).count("/")
            == relative_parent.count("/") + (1 if relative_parent else 0)
        ]
        return local, _pc_recovery_digest(protected_children)

    entries, root_digest = scan(binding.fd, "", 1)
    entries.sort(
        key=lambda item: (
            str(item["locator"]["segment"]),
            str(item["locator"]["path"]),
        )
    )
    return entries, root_digest


def _pc_recovery_validate_owner_links(entries: list[dict[str, object]]) -> None:
    by_locator = {
        (str(entry["locator"]["segment"]), str(entry["locator"]["path"])): entry
        for entry in entries
    }
    for entry in entries:
        owner = entry.get("owner")
        if not isinstance(owner, dict):
            continue
        private_name = owner.get("private_name")
        expected_identity = owner.get("private_identity")
        private_entry = by_locator.get(("tool-root", str(private_name)))
        if private_entry is None:
            owner["private_state"] = "missing"
            continue
        observed_identity = private_entry.get("identity")
        expected_document = {
            "dev": expected_identity[0],
            "ino": expected_identity[1],
            "type": expected_identity[2],
        }
        if (
            private_entry.get("type") != "directory"
            or observed_identity != expected_document
        ):
            raise MirrorSyncError(
                "legacy owner payload aliases or mismatches its private directory: "
                f"{entry['locator']['path']}"
            )
        owner["private_state"] = "matching"


def _pc_recovery_validate_topology(
    parent: _PrivateControlRecoveryBinding,
    tool: _PrivateControlRecoveryBinding,
    quarantine: _PrivateControlRecoveryBinding,
    primary_parent: _PrivateControlRecoveryBinding | None = None,
) -> None:
    identities = {parent.identity, tool.identity, quarantine.identity}
    if len(identities) != 3:
        raise MirrorSyncError("private-control recovery root roles alias")
    if tool.identity[0] != quarantine.identity[0]:
        raise MirrorSyncError(
            "private-control recovery tool and quarantine roots cross filesystems"
        )
    for child in (tool, quarantine):
        if not _directory_is_at_or_below(
            child.fd, parent.identity
        ) or _directory_is_at_or_below(parent.fd, child.identity):
            raise MirrorSyncError(
                f"{child.label} is not a strict child of the legacy parent"
            )
    if _directory_is_at_or_below(tool.fd, quarantine.identity) or (
        _directory_is_at_or_below(quarantine.fd, tool.identity)
    ):
        raise MirrorSyncError("private-control recovery fixed roots overlap")
    if primary_parent is not None:
        if primary_parent.identity in identities:
            raise MirrorSyncError("primary and legacy private-control roots alias")
        for legacy in (parent, tool, quarantine):
            if _directory_is_at_or_below(
                legacy.fd,
                primary_parent.identity,
            ) or _directory_is_at_or_below(
                primary_parent.fd,
                legacy.identity,
            ):
                raise MirrorSyncError(
                    "primary and legacy private-control roots overlap"
                )


def _pc_recovery_existing_primary_parent(
    primary_spec: PrivateControlRootSpec,
) -> _PrivateControlRecoveryBinding | None:
    assert primary_spec.account_home is not None
    home = _pc_recovery_bind_trusted_home(primary_spec.account_home)
    binding: _PrivateControlRecoveryBinding | None = None
    try:
        try:
            binding = _pc_recovery_bind_child_directory(
                home,
                PRIVATE_CONTROL_NAMESPACE_NAME,
                f"primary private-control parent [{primary_spec.root_id}]",
            )
        except _PrivateControlRecoveryInitialAbsence:
            _pc_recovery_revalidate_directory(home)
            _pc_recovery_close_bindings((home,))
            return None
        if binding.access[0] != 0o700 or binding.access[1] != os.geteuid():
            raise MirrorSyncError(
                "primary private-control parent must be mode 0700 and current-owned"
            )
        _pc_recovery_revalidate_directory(home)
        _pc_recovery_revalidate_directory(binding)
        _pc_recovery_close_bindings((home,))
        return binding
    except BaseException as error:
        try:
            _pc_recovery_close_bindings((binding, home))
        except MirrorSyncError as cleanup_error:
            raise MirrorSyncError(
                f"{error}; secondary primary-parent lookup cleanup failure: "
                f"{cleanup_error}"
            ) from error
        raise


def _pc_recovery_bind_legacy(
    requested_root_id: str,
    *,
    exclusive: bool,
) -> tuple[
    PrivateControlRootSpec,
    PrivateControlRootSpec,
    _PrivateControlRecoveryBinding,
    _PrivateControlRecoveryBinding,
    _PrivateControlRecoveryBinding,
    _PrivateControlRecoveryBinding | None,
]:
    primary_spec, legacy_spec = _pc_recovery_specs(requested_root_id)
    parent: _PrivateControlRecoveryBinding | None = None
    tool: _PrivateControlRecoveryBinding | None = None
    quarantine: _PrivateControlRecoveryBinding | None = None
    primary_parent: _PrivateControlRecoveryBinding | None = None
    try:
        parent = _pc_recovery_bind_directory(
            legacy_spec.parent_path,
            f"legacy private-control parent [{legacy_spec.root_id}]",
        )
        if not _legacy_shared_parent_policy_is_valid(parent.access):
            raise MirrorSyncError(
                "legacy private-control parent must be root-owned mode 1777"
            )
        tool = _pc_recovery_bind_child_directory(
            parent,
            PRIVATE_TOOL_ROOT_NAME,
            f"legacy private-control tool root [{legacy_spec.root_id}]",
        )
        if tool.access[0] != 0o700 or tool.access[1] != os.geteuid():
            raise MirrorSyncError(
                "legacy private-control tool root must be mode 0700 and "
                "current uid owned"
            )
        if exclusive:
            _pc_recovery_acquire_exclusive(tool)
        quarantine = _pc_recovery_bind_child_directory(
            parent,
            DURABLE_QUARANTINE_ROOT_NAME,
            f"legacy private-control quarantine [{legacy_spec.root_id}]",
        )
        if quarantine.access[0] != 0o700 or quarantine.access[1] != os.geteuid():
            raise MirrorSyncError(
                "legacy private-control quarantine must be mode 0700 and "
                "current uid owned"
            )
        if exclusive:
            _pc_recovery_acquire_exclusive(quarantine)
        primary_parent = _pc_recovery_existing_primary_parent(primary_spec)
        _pc_recovery_validate_topology(
            parent,
            tool,
            quarantine,
            primary_parent,
        )
        for binding in (parent, tool, quarantine):
            _pc_recovery_revalidate_directory(binding)
        return (
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
            primary_parent,
        )
    except BaseException as error:
        try:
            _pc_recovery_close_bindings((primary_parent, quarantine, tool, parent))
        except MirrorSyncError as cleanup_error:
            raise MirrorSyncError(
                f"{error}; secondary recovery bind cleanup failure: {cleanup_error}"
            ) from error
        raise


def _pc_recovery_caps_document() -> dict[str, int | float]:
    return {
        "max_allocated_bytes": PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES,
        "max_depth": PRIVATE_CONTROL_RECOVERY_MAX_DEPTH,
        "max_entries": PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES,
        "max_logical_bytes": PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES,
        "max_pending_bytes": PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES,
        "max_pending_entries": PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES,
        "max_path_bytes": PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES,
        "max_receipt_bytes": PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES,
        "timeout_seconds": PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS,
    }


def _pc_recovery_manifest(
    parent: _PrivateControlRecoveryBinding,
    tool: _PrivateControlRecoveryBinding,
    quarantine: _PrivateControlRecoveryBinding,
    *,
    operation: OperationBudget | None = None,
) -> dict[str, object]:
    _operation_checkpoint(operation, "starting private-control recovery manifest")
    deadline = time.monotonic() + PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS
    if operation is not None:
        deadline = min(deadline, operation.deadline)
    state = {
        "allocated_bytes": 0,
        "entries": 0,
        "logical_bytes": 0,
        "path_bytes": 0,
    }
    for binding in (tool, quarantine):
        metadata = os.fstat(binding.fd)
        allocated = _pc_recovery_capacity_bytes(metadata)
        if (
            state["allocated_bytes"] + allocated
            > PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES
        ):
            raise MirrorSyncError(
                "private-control recovery roots exceed the allocated-byte cap"
            )
        state["allocated_bytes"] += allocated
    tool_entries, tool_digest = _pc_recovery_scan_directory(
        tool,
        "tool-root",
        deadline=deadline,
        operation=operation,
        state=state,
    )
    quarantine_entries, quarantine_digest = _pc_recovery_scan_directory(
        quarantine,
        "quarantine",
        deadline=deadline,
        operation=operation,
        state=state,
    )
    entries = sorted(
        [*tool_entries, *quarantine_entries],
        key=lambda item: (
            str(item["locator"]["segment"]),
            str(item["locator"]["path"]),
        ),
    )
    _pc_recovery_validate_owner_links(entries)
    seen_identities = {parent.identity, tool.identity, quarantine.identity}
    for entry in entries:
        identity_document = entry["identity"]
        identity = (
            identity_document["dev"],
            identity_document["ino"],
            identity_document["type"],
        )
        if identity in seen_identities:
            raise MirrorSyncError(
                "private-control recovery manifest contains an object alias: "
                f"{entry['locator']['segment']}/{entry['locator']['path']}"
            )
        seen_identities.add(identity)
    roots = {
        "parent": _pc_recovery_record(parent.identity, parent.access),
        "quarantine": _pc_recovery_record(quarantine.identity, quarantine.access),
        "tool_root": _pc_recovery_record(tool.identity, tool.access),
    }
    protected = {
        "entries": [_pc_recovery_protected_entry(entry) for entry in entries],
        "roots": roots,
        "tree_digests": {
            "quarantine": quarantine_digest,
            "tool_root": tool_digest,
        },
    }
    return {
        "allocated_bytes": state["allocated_bytes"],
        "digest": _pc_recovery_digest(protected),
        "entries": entries,
        "entry_count": state["entries"],
        "logical_bytes": state["logical_bytes"],
        "path_bytes": state["path_bytes"],
        "tree_digests": protected["tree_digests"],
    }


def _pc_recovery_plan_from_bindings(
    primary_spec: PrivateControlRootSpec,
    legacy_spec: PrivateControlRootSpec,
    parent: _PrivateControlRecoveryBinding,
    tool: _PrivateControlRecoveryBinding,
    quarantine: _PrivateControlRecoveryBinding,
    *,
    operation: OperationBudget | None = None,
) -> dict[str, object]:
    inventory = _pc_recovery_manifest(
        parent,
        tool,
        quarantine,
        operation=operation,
    )
    primary_parent = Path(os.path.abspath(primary_spec.parent_path))
    legacy_parent = Path(os.path.abspath(legacy_spec.parent_path))
    plan: dict[str, object] = {
        "caps": _pc_recovery_caps_document(),
        "contract": PRIVATE_CONTROL_RECOVERY_CONTRACT,
        "disposition": PRIVATE_CONTROL_RECOVERY_DISPOSITION,
        "inventory": inventory,
        "leases": [
            {
                "mode": PRIVATE_CONTROL_RECOVERY_LOCK_MODE,
                "order": order,
                "role": role,
            }
            for order, role in enumerate(PRIVATE_CONTROL_RECOVERY_LOCK_ORDER)
        ],
        "paths": {
            "parent": str(legacy_parent),
            "primary_marker": str(
                primary_parent / PRIVATE_CONTROL_RECOVERY_MARKER_NAME
            ),
            "primary_parent": str(primary_parent),
            "primary_receipt": str(
                primary_parent / PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME
            ),
            "quarantine": str(legacy_parent / DURABLE_QUARANTINE_ROOT_NAME),
            "tool_root": str(legacy_parent / PRIVATE_TOOL_ROOT_NAME),
        },
        "primary_root_id": primary_spec.root_id,
        "root_id": legacy_spec.root_id,
        "roots": {
            "parent": _pc_recovery_record(parent.identity, parent.access),
            "quarantine": _pc_recovery_record(
                quarantine.identity,
                quarantine.access,
            ),
            "tool_root": _pc_recovery_record(tool.identity, tool.access),
        },
        "segment_locator": {
            "parent_path": str(legacy_parent),
            "quarantine_name": DURABLE_QUARANTINE_ROOT_NAME,
            "root_id": legacy_spec.root_id,
            "tool_root_name": PRIVATE_TOOL_ROOT_NAME,
        },
        "version": PRIVATE_CONTROL_RECOVERY_VERSION,
    }
    plan["plan_digest"] = _pc_recovery_digest(_pc_recovery_protected_plan(plan))
    return plan


def _pc_recovery_protected_plan(plan: dict[str, object]) -> dict[str, object]:
    protected = dict(plan)
    protected.pop("plan_digest", None)
    inventory = protected.get("inventory")
    if isinstance(inventory, dict):
        clean_inventory = dict(inventory)
        clean_inventory.pop("allocated_bytes", None)
        entries = clean_inventory.get("entries")
        if isinstance(entries, list):
            clean_inventory["entries"] = [
                _pc_recovery_protected_entry(entry)
                if isinstance(entry, dict)
                else entry
                for entry in entries
            ]
        protected["inventory"] = clean_inventory
    return protected


def _pc_recovery_expected_paths(
    primary_spec: PrivateControlRootSpec,
    legacy_spec: PrivateControlRootSpec,
) -> dict[str, str]:
    primary_parent = Path(os.path.abspath(primary_spec.parent_path))
    legacy_parent = Path(os.path.abspath(legacy_spec.parent_path))
    return {
        "parent": str(legacy_parent),
        "primary_marker": str(primary_parent / PRIVATE_CONTROL_RECOVERY_MARKER_NAME),
        "primary_parent": str(primary_parent),
        "primary_receipt": str(primary_parent / PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME),
        "quarantine": str(legacy_parent / DURABLE_QUARANTINE_ROOT_NAME),
        "tool_root": str(legacy_parent / PRIVATE_TOOL_ROOT_NAME),
    }


def _pc_recovery_validate_plan_document(
    raw: object,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != _PRIVATE_CONTROL_RECOVERY_PLAN_FIELDS:
        raise MirrorSyncError("private-control recovery plan schema is invalid")
    plan = raw
    primary_spec, legacy_spec = _pc_recovery_specs(str(plan.get("root_id")))
    digest = plan.get("plan_digest")
    expected_leases = [
        {
            "mode": PRIVATE_CONTROL_RECOVERY_LOCK_MODE,
            "order": order,
            "role": role,
        }
        for order, role in enumerate(PRIVATE_CONTROL_RECOVERY_LOCK_ORDER)
    ]
    expected_segment = {
        "parent_path": str(Path(os.path.abspath(legacy_spec.parent_path))),
        "quarantine_name": DURABLE_QUARANTINE_ROOT_NAME,
        "root_id": legacy_spec.root_id,
        "tool_root_name": PRIVATE_TOOL_ROOT_NAME,
    }
    if (
        plan.get("contract") != PRIVATE_CONTROL_RECOVERY_CONTRACT
        or type(plan.get("version")) is not int
        or plan["version"] != PRIVATE_CONTROL_RECOVERY_VERSION
        or plan.get("disposition") != PRIVATE_CONTROL_RECOVERY_DISPOSITION
        or plan.get("primary_root_id") != primary_spec.root_id
        or not _pc_recovery_canonical_documents_equal(
            plan.get("paths"),
            _pc_recovery_expected_paths(primary_spec, legacy_spec),
        )
        or not _pc_recovery_canonical_documents_equal(
            plan.get("caps"),
            _pc_recovery_caps_document(),
        )
        or not _pc_recovery_canonical_documents_equal(
            plan.get("leases"),
            expected_leases,
        )
        or not _pc_recovery_canonical_documents_equal(
            plan.get("segment_locator"),
            expected_segment,
        )
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or digest != _pc_recovery_digest(_pc_recovery_protected_plan(plan))
    ):
        raise MirrorSyncError("private-control recovery plan contract is invalid")
    inventory = plan.get("inventory")
    roots = plan.get("roots")
    if not isinstance(inventory, dict) or set(inventory) != {
        "allocated_bytes",
        "digest",
        "entries",
        "entry_count",
        "logical_bytes",
        "path_bytes",
        "tree_digests",
    }:
        raise MirrorSyncError("private-control recovery inventory schema is invalid")
    entries = inventory.get("entries")
    if (
        not isinstance(entries, list)
        or not isinstance(roots, dict)
        or set(roots) != {"parent", "quarantine", "tool_root"}
        or not all(
            _pc_recovery_record_document_is_valid(
                roots.get(role),
                expected_type=stat.S_IFDIR,
            )
            for role in ("parent", "quarantine", "tool_root")
        )
    ):
        raise MirrorSyncError("private-control recovery inventory is invalid")
    tree_digests = inventory.get("tree_digests")
    if (
        not isinstance(inventory.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", inventory["digest"]) is None
        or not isinstance(tree_digests, dict)
        or set(tree_digests) != {"quarantine", "tool_root"}
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in tree_digests.values()
        )
    ):
        raise MirrorSyncError("private-control recovery inventory digests are invalid")
    numeric_limits = (
        (inventory.get("entry_count"), PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES),
        (
            inventory.get("logical_bytes"),
            PRIVATE_CONTROL_RECOVERY_MAX_LOGICAL_BYTES,
        ),
        (
            inventory.get("allocated_bytes"),
            PRIVATE_CONTROL_RECOVERY_MAX_ALLOCATED_BYTES,
        ),
        (inventory.get("path_bytes"), PRIVATE_CONTROL_RECOVERY_MAX_PATH_BYTES),
    )
    if any(
        type(value) is not int or value < 0 or value > limit
        for value, limit in numeric_limits
    ):
        raise MirrorSyncError("private-control recovery inventory exceeds its caps")
    if inventory.get("entry_count") != len(entries):
        raise MirrorSyncError("private-control recovery entry count is inconsistent")
    locators: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "access",
            "allocated_bytes",
            "identity",
            "locator",
            "owner",
            "sha256",
            "size",
            "type",
        }:
            raise MirrorSyncError("private-control recovery entry schema is invalid")
        locator = entry.get("locator")
        if (
            not isinstance(locator, dict)
            or set(locator) != {"path", "segment"}
            or locator.get("segment") not in {"tool-root", "quarantine"}
            or not isinstance(locator.get("path"), str)
            or not locator["path"]
        ):
            raise MirrorSyncError("private-control recovery entry locator is invalid")
        locators.append((locator["segment"], locator["path"]))
        entry_type = entry.get("type")
        expected_entry_type = (
            stat.S_IFDIR if entry_type == "directory" else stat.S_IFREG
        )
        if (
            entry_type not in {"directory", "regular-file"}
            or type(entry.get("size")) is not int
            or entry["size"] < 0
            or type(entry.get("allocated_bytes")) is not int
            or entry["allocated_bytes"] < 0
            or not isinstance(entry.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
            or not _pc_recovery_record_document_is_valid(
                {
                    "access": entry.get("access"),
                    "identity": entry.get("identity"),
                },
                expected_type=expected_entry_type,
            )
            or not _pc_recovery_owner_document_is_valid(entry.get("owner"))
            or (
                entry.get("owner") is not None
                and (
                    entry_type != "regular-file"
                    or locator.get("segment") != "tool-root"
                )
            )
        ):
            raise MirrorSyncError("private-control recovery entry fields are invalid")
    if locators != sorted(locators) or len(locators) != len(set(locators)):
        raise MirrorSyncError(
            "private-control recovery entries are not uniquely sorted"
        )
    _pc_recovery_validate_primary_receipt_capacity(plan)
    return plan


def _pc_recovery_path_is_at_or_below(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def _pc_recovery_external_plan_path(
    path: Path,
    requested_root_id: str,
) -> Path:
    primary_spec, legacy_spec = _pc_recovery_specs(requested_root_id)
    candidate = Path(os.path.abspath(path))
    for protected in (
        Path(os.path.abspath(primary_spec.parent_path)),
        Path(os.path.abspath(legacy_spec.parent_path)) / PRIVATE_TOOL_ROOT_NAME,
        Path(os.path.abspath(legacy_spec.parent_path)) / DURABLE_QUARANTINE_ROOT_NAME,
    ):
        if _pc_recovery_path_is_at_or_below(candidate, protected):
            raise MirrorSyncError(
                "external recovery plan must be outside private-control roots"
            )
    return candidate


def _pc_recovery_bind_external_plan_parent(
    path: Path,
) -> _PrivateControlRecoveryBinding:
    path = Path(os.path.abspath(path))
    parent_path = path.parent
    if path == Path("/") or not path.name:
        raise MirrorSyncError("external recovery plan path must name a file")
    components = parent_path.parts[1:]
    if len(components) > PRIVATE_CONTROL_RECOVERY_MAX_DEPTH:
        raise MirrorSyncError("external recovery plan parent exceeds its depth cap")
    current_path = Path("/")
    try:
        current_fd = os.open(current_path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot bind external recovery plan root: {error}"
        ) from error
    try:
        for component in components:
            child_fd = -1
            try:
                path_metadata = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(path_metadata.st_mode) or stat.S_ISLNK(
                    path_metadata.st_mode
                ):
                    raise MirrorSyncError(
                        "external recovery plan parent contains a symlink or "
                        f"non-directory: {current_path / component}"
                    )
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                descriptor_metadata = os.fstat(child_fd)
                if _pc_recovery_identity(descriptor_metadata) != _pc_recovery_identity(
                    path_metadata
                ) or _pc_recovery_access(descriptor_metadata) != _pc_recovery_access(
                    path_metadata
                ):
                    raise MirrorSyncError(
                        "external recovery plan parent changed while binding: "
                        f"{current_path / component}"
                    )
            except BaseException:
                if child_fd >= 0:
                    os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
            current_path /= component
        metadata = os.fstat(current_fd)
        result = _PrivateControlRecoveryBinding(
            label="external recovery plan parent",
            path=parent_path,
            fd=current_fd,
            identity=_pc_recovery_identity(metadata),
            access=_pc_recovery_access(metadata),
        )
        current_fd = -1
        return result
    except OSError as error:
        raise MirrorSyncError(
            f"cannot bind external recovery plan parent {parent_path}: {error}"
        ) from error
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _pc_recovery_reject_plan_parent_overlap(
    parent: _PrivateControlRecoveryBinding,
    protected_bindings: tuple[_PrivateControlRecoveryBinding | None, ...],
) -> None:
    for protected in protected_bindings:
        if protected is None:
            continue
        if parent.identity == protected.identity or _directory_is_at_or_below(
            parent.fd,
            protected.identity,
        ):
            raise MirrorSyncError(
                "external recovery plan parent overlaps a private-control root"
            )


def _pc_recovery_write_plan(
    path: Path,
    plan: dict[str, object],
    protected_bindings: tuple[_PrivateControlRecoveryBinding | None, ...],
) -> None:
    payload = _pc_recovery_json_bytes(plan, pretty=True)
    if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
        raise MirrorSyncError("private-control recovery plan exceeds its byte cap")
    _pc_recovery_validate_primary_receipt_capacity(plan)
    parent = _pc_recovery_bind_external_plan_parent(path)
    try:
        _pc_recovery_reject_plan_parent_overlap(parent, protected_bindings)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_fd = os.open(path.name, flags, 0o600, dir_fd=parent.fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot create recovery plan {path}: {error}"
            ) from error
        try:
            _pc_recovery_write_all(
                file_fd,
                payload,
                label=f"writing recovery plan {path}",
            )
            os.fchmod(file_fd, 0o600)
            os.fsync(file_fd)
            metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _pc_recovery_access(metadata) != (0o600, os.geteuid(), os.getegid())
                or metadata.st_size != len(payload)
            ):
                raise MirrorSyncError(
                    "published recovery plan access/content is invalid"
                )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot write recovery plan {path}: {error}"
            ) from error
        finally:
            os.close(file_fd)
        try:
            os.fsync(parent.fd)
            _pc_recovery_revalidate_directory(parent)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot durably publish recovery plan {path}: {error}"
            ) from error
    finally:
        _pc_recovery_close_bindings((parent,))


def _pc_recovery_read_external_plan(
    path: Path,
    requested_root_id: str,
) -> dict[str, object]:
    path = _pc_recovery_external_plan_path(path, requested_root_id)
    parent = _pc_recovery_bind_external_plan_parent(path)
    file_fd = -1
    try:
        try:
            path_metadata = os.stat(
                path.name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(path_metadata.st_mode) or stat.S_ISLNK(
                path_metadata.st_mode
            ):
                raise MirrorSyncError(
                    "recovery plan must be a non-symlink regular file"
                )
            if (
                path_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(path_metadata.st_mode) & 0o022
                or path_metadata.st_size > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
            ):
                raise MirrorSyncError("recovery plan owner/access/size is invalid")
            file_fd = os.open(path.name, _FILE_READ_FLAGS, dir_fd=parent.fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot bind recovery plan {path}: {error}"
            ) from error
        descriptor_metadata = os.fstat(file_fd)
        if _pc_recovery_identity(descriptor_metadata) != _pc_recovery_identity(
            path_metadata
        ) or _pc_recovery_access(descriptor_metadata) != _pc_recovery_access(
            path_metadata
        ):
            raise MirrorSyncError("recovery plan changed while binding it")

        def read_once() -> bytes:
            os.lseek(file_fd, 0, os.SEEK_SET)
            payload = bytearray()
            while len(payload) <= PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                chunk = os.read(
                    file_fd,
                    min(
                        1024 * 1024,
                        PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES + 1 - len(payload),
                    ),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                raise MirrorSyncError("recovery plan exceeds its byte cap")
            return bytes(payload)

        first = read_once()
        second = read_once()
        final_descriptor = os.fstat(file_fd)
        final_path = os.stat(
            path.name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if (
            first != second
            or _pc_recovery_identity(final_descriptor)
            != _pc_recovery_identity(descriptor_metadata)
            or _pc_recovery_access(final_descriptor)
            != _pc_recovery_access(descriptor_metadata)
            or final_descriptor.st_size != descriptor_metadata.st_size
            or _pc_recovery_identity(final_path)
            != _pc_recovery_identity(descriptor_metadata)
            or _pc_recovery_access(final_path)
            != _pc_recovery_access(descriptor_metadata)
        ):
            raise MirrorSyncError("recovery plan changed while reading it")
        _pc_recovery_revalidate_directory(parent)
    except OSError as error:
        raise MirrorSyncError(f"cannot read recovery plan {path}: {error}") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        _pc_recovery_close_bindings((parent,))
    try:
        raw = json.loads(
            first.decode("utf-8"),
            object_pairs_hook=_pc_recovery_owner_pairs,
            parse_int=_bounded_json_integer,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise MirrorSyncError(f"recovery plan JSON is invalid: {error}") from error
    return _pc_recovery_validate_plan_document(raw)


def plan_private_control_recovery(
    requested_root_id: str,
    output_receipt: Path,
) -> dict[str, object]:
    _pc_recovery_require_no_close_fence()
    output_path = _pc_recovery_external_plan_path(
        output_receipt,
        requested_root_id,
    )
    bindings: tuple[_PrivateControlRecoveryBinding | None, ...] = ()
    try:
        (
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
            primary_parent,
        ) = _pc_recovery_bind_legacy(requested_root_id, exclusive=True)
        bindings = (primary_parent, quarantine, tool, parent)
        first = _pc_recovery_plan_from_bindings(
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
        )
        for binding in (parent, tool, quarantine):
            _pc_recovery_revalidate_directory(binding)
        second = _pc_recovery_plan_from_bindings(
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
        )
        if _pc_recovery_protected_plan(first) != _pc_recovery_protected_plan(second):
            raise MirrorSyncError(
                "private-control recovery evidence changed during dry-run"
            )
        _pc_recovery_write_plan(
            output_path,
            second,
            (primary_parent, quarantine, tool, parent),
        )
        return second
    finally:
        if bindings:
            _pc_recovery_close_bindings(bindings)


def _pc_recovery_bind_trusted_home(path: Path) -> _PrivateControlRecoveryBinding:
    path = Path(os.path.abspath(path))
    if not path.is_absolute() or path == Path("/"):
        raise MirrorSyncError("primary account home must be an absolute child path")
    components = path.parts[1:]
    if not components or len(components) > PRIVATE_CONTROL_MAX_ANCESTORS:
        raise MirrorSyncError("primary account home exceeds its ancestor cap")
    current_path = Path("/")
    current_fd = os.open(current_path, _DIRECTORY_FLAGS)
    try:
        for component in components:
            child_fd = -1
            try:
                path_metadata = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(path_metadata.st_mode) or stat.S_ISLNK(
                    path_metadata.st_mode
                ):
                    raise MirrorSyncError(
                        f"primary account-home ancestor is unsafe: {current_path / component}"
                    )
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                child_metadata = os.fstat(child_fd)
                if _pc_recovery_identity(child_metadata) != _pc_recovery_identity(
                    path_metadata
                ) or _pc_recovery_access(child_metadata) != _pc_recovery_access(
                    path_metadata
                ):
                    raise MirrorSyncError(
                        f"primary account-home ancestor changed: {current_path / component}"
                    )
                mode, uid, _gid = _pc_recovery_access(child_metadata)
                if uid not in {0, os.geteuid()} or mode & 0o022:
                    raise MirrorSyncError(
                        "primary account-home ancestors must be root/current-owned "
                        f"and not group/world writable: {current_path / component}"
                    )
            except BaseException:
                if child_fd >= 0:
                    os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
            current_path /= component
        metadata = os.fstat(current_fd)
        access = _pc_recovery_access(metadata)
        if access[1] != os.geteuid() or access[0] & 0o022:
            raise MirrorSyncError(
                "primary account home must be current-owned and not group/world writable"
            )
        result = _PrivateControlRecoveryBinding(
            label="primary account home",
            path=path,
            fd=current_fd,
            identity=_pc_recovery_identity(metadata),
            access=access,
        )
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _pc_recovery_directory_is_empty(directory_fd: int, label: str) -> bool:
    def scan() -> tuple[str, ...]:
        names: list[str] = []
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                names.append(entry.name)
                if len(names) > 1:
                    break
        return tuple(names)

    try:
        first = scan()
        second = scan()
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {error}") from error
    if first != second:
        raise MirrorSyncError(f"{label} namespace changed during inspection")
    return not first


def _pc_recovery_pending_read_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _pc_recovery_pending_publication_scope(
    requested_name: str,
) -> tuple[tuple[str, str], str, str]:
    prefixes = (
        f".{PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME}.pending-",
        f".{PRIVATE_CONTROL_RECOVERY_MARKER_NAME}.pending-",
    )
    requested_prefix: str | None = None
    requested_plan_digest: str | None = None
    for prefix in prefixes:
        if requested_name.startswith(prefix):
            suffix = requested_name[len(prefix) :]
            match = re.fullmatch(r"([0-9a-f]{64})-[0-9a-f]{32}", suffix)
            if match is not None:
                requested_prefix = prefix
                requested_plan_digest = match.group(1)
            break
    if requested_prefix is None or requested_plan_digest is None:
        raise MirrorSyncError("pending recovery publication name is invalid")
    return prefixes, requested_prefix, requested_plan_digest


def _pc_recovery_stable_pending_publications(
    parent: _PrivateControlRecoveryBinding,
    prefixes: tuple[str, str],
) -> tuple[tuple[str, tuple[int, int, int], tuple[int, int, int], int], ...]:
    deadline = time.monotonic() + PRIVATE_CONTROL_RECOVERY_TIMEOUT_SECONDS

    def snapshot() -> tuple[
        tuple[str, tuple[int, int, int], tuple[int, int, int], int], ...
    ]:
        names: list[str] = []
        scanned_entries = 0
        try:
            with os.scandir(parent.fd) as iterator:
                for entry in iterator:
                    _pc_recovery_check_deadline(
                        deadline,
                        "inventorying pending recovery publications",
                    )
                    scanned_entries += 1
                    if scanned_entries > PRIVATE_CONTROL_RECOVERY_MAX_ENTRIES:
                        raise MirrorSyncError(
                            "pending recovery publication inventory exceeds its "
                            "scan cap"
                        )
                    name = entry.name
                    if name.startswith(prefixes):
                        try:
                            name.encode("utf-8", "strict")
                        except UnicodeEncodeError as error:
                            raise MirrorSyncError(
                                "pending recovery publication name is not strict UTF-8"
                            ) from error
                        names.append(name)
        except MirrorSyncError:
            raise
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inventory pending recovery publications: {error}"
            ) from error
        names.sort()
        records: list[tuple[str, tuple[int, int, int], tuple[int, int, int], int]] = []
        for name in names:
            _pc_recovery_check_deadline(
                deadline,
                "inspecting pending recovery publications",
            )
            try:
                metadata = os.stat(
                    name,
                    dir_fd=parent.fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot inspect pending recovery publication {name}: {error}"
                ) from error
            identity = _pc_recovery_identity(metadata)
            access = _pc_recovery_access(metadata)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
                or access
                not in {
                    (0o400, os.geteuid(), os.getegid()),
                    (0o600, os.geteuid(), os.getegid()),
                }
                or metadata.st_size < 0
                or metadata.st_size > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
            ):
                raise MirrorSyncError(
                    f"pending recovery publication policy is invalid: {name}"
                )
            records.append((name, identity, access, metadata.st_size))
        return tuple(records)

    _pc_recovery_revalidate_directory(parent)
    first = snapshot()
    second = snapshot()
    _pc_recovery_revalidate_directory(parent)
    if first != second:
        raise MirrorSyncError(
            "pending recovery publication namespace changed during accounting"
        )
    return first


def _pc_recovery_select_pending_publication(
    parent: _PrivateControlRecoveryBinding,
    requested_name: str,
) -> tuple[
    str,
    int,
    tuple[tuple[int, int, int], tuple[int, int, int], int] | None,
]:
    if not parent.locked:
        raise MirrorSyncError(
            "pending recovery publication accounting requires the parent lease"
        )
    if (
        PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES < 1
        or PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES
        < PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
    ):
        raise MirrorSyncError("pending recovery publication caps are invalid")
    prefixes, requested_prefix, requested_plan_digest = (
        _pc_recovery_pending_publication_scope(requested_name)
    )
    first = _pc_recovery_stable_pending_publications(parent, prefixes)
    logical_bytes = sum(record[3] for record in first)
    same_plan_prefix = f"{requested_prefix}{requested_plan_digest}-"
    reusable = [
        record
        for record in first
        if record[0].startswith(same_plan_prefix)
        and re.fullmatch(r"[0-9a-f]{32}", record[0][len(same_plan_prefix) :])
        is not None
    ]
    if reusable:
        selected = max(reusable, key=lambda record: (record[3], record[0]))
        return (
            selected[0],
            logical_bytes,
            (selected[1], selected[2], selected[3]),
        )
    if len(first) >= PRIVATE_CONTROL_RECOVERY_MAX_PENDING_ENTRIES:
        raise MirrorSyncError(
            "pending recovery publication entry cap would be exceeded"
        )
    if (
        logical_bytes
        > PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES
        - PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
    ):
        raise MirrorSyncError(
            "pending recovery publication aggregate-byte cap would be exceeded"
        )
    return requested_name, logical_bytes, None


def _pc_recovery_remove_superseded_pending_publications(
    parent: _PrivateControlRecoveryBinding,
    requested_name: str,
    label: str,
) -> None:
    if not parent.locked:
        raise MirrorSyncError(
            "pending recovery publication cleanup requires the parent lease"
        )
    prefixes, requested_prefix, requested_plan_digest = (
        _pc_recovery_pending_publication_scope(requested_name)
    )
    records = _pc_recovery_stable_pending_publications(parent, prefixes)
    same_plan_prefix = f"{requested_prefix}{requested_plan_digest}-"
    targets = tuple(
        record
        for record in records
        if record[0].startswith(same_plan_prefix)
        and re.fullmatch(r"[0-9a-f]{32}", record[0][len(same_plan_prefix) :])
        is not None
    )
    for name, identity, access, size in targets:
        file_fd = -1
        custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
        try:
            try:
                path_metadata = os.stat(
                    name,
                    dir_fd=parent.fd,
                    follow_symlinks=False,
                )
                custody = _pc_recovery_open_pending_descriptor(
                    parent,
                    name,
                    label,
                    _pc_recovery_pending_read_flags(),
                    None,
                    identity,
                    access,
                )
                file_fd = custody.fd
                descriptor = os.fstat(file_fd)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot bind superseded pending {label} {name}: {error}"
                ) from error
            if (
                _pc_recovery_identity(path_metadata) != identity
                or _pc_recovery_identity(descriptor) != identity
                or _pc_recovery_access(path_metadata) != access
                or _pc_recovery_access(descriptor) != access
                or path_metadata.st_nlink != 1
                or descriptor.st_nlink != 1
                or path_metadata.st_size != size
                or descriptor.st_size != size
            ):
                raise MirrorSyncError(f"superseded pending {label} changed: {name}")
            try:
                os.unlink(name, dir_fd=parent.fd)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot remove superseded pending {label} {name}: {error}"
                ) from error
            try:
                os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot verify superseded pending {label} removal {name}: "
                    f"{error}"
                ) from error
            else:
                raise MirrorSyncError(
                    f"superseded pending {label} was replaced during removal: {name}"
                )
        finally:
            if custody is not None and custody.state == "open":
                _pc_recovery_close_pending_descriptor(custody)
    if targets:
        _pc_recovery_fsync_directory(parent)
        _pc_recovery_revalidate_directory(parent)


def _pc_recovery_remove_empty_staged_primary_parent(
    home: _PrivateControlRecoveryBinding,
    temporary: _PrivateControlRecoveryBinding,
    temporary_name: str,
) -> None:
    expected_path = home.path / temporary_name
    if temporary.path != expected_path:
        raise MirrorSyncError(
            "staged primary private-control parent cleanup path changed"
        )

    def revalidate_staging() -> None:
        try:
            path_metadata = os.stat(
                temporary_name,
                dir_fd=home.fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(temporary.fd)
        except FileNotFoundError as error:
            raise MirrorSyncError(
                "staged primary private-control parent is missing during cleanup"
            ) from error
        except OSError as error:
            raise MirrorSyncError(
                "cannot inspect staged primary private-control parent during "
                f"cleanup: {error}"
            ) from error
        if (
            _pc_recovery_identity(path_metadata) != temporary.identity
            or _pc_recovery_identity(descriptor_metadata) != temporary.identity
            or _pc_recovery_access(path_metadata) != temporary.access
            or _pc_recovery_access(descriptor_metadata) != temporary.access
        ):
            raise MirrorSyncError(
                "staged primary private-control parent changed during cleanup"
            )
        if temporary.access[0] != 0o700 or temporary.access[1] != os.geteuid():
            raise MirrorSyncError(
                "staged primary private-control parent cleanup policy is invalid"
            )

    _pc_recovery_revalidate_directory(home)
    revalidate_staging()
    if not _pc_recovery_directory_is_empty(
        temporary.fd,
        "staged primary private-control parent cleanup",
    ):
        raise MirrorSyncError(
            "staged primary private-control parent is not empty during cleanup"
        )
    revalidate_staging()
    _pc_recovery_revalidate_directory(home)
    try:
        os.rmdir(temporary_name, dir_fd=home.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot remove staged primary private-control parent: {error}"
        ) from error
    try:
        os.stat(temporary_name, dir_fd=home.fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise MirrorSyncError(
            "cannot verify staged primary private-control parent removal: "
            f"{error}"
        ) from error
    else:
        raise MirrorSyncError(
            "staged primary private-control parent was replaced during cleanup"
        )
    try:
        descriptor_metadata = os.fstat(temporary.fd)
    except OSError as error:
        raise MirrorSyncError(
            "cannot revalidate removed staged primary private-control parent: "
            f"{error}"
        ) from error
    if (
        _pc_recovery_identity(descriptor_metadata) != temporary.identity
        or _pc_recovery_access(descriptor_metadata) != temporary.access
    ):
        raise MirrorSyncError(
            "removed staged primary private-control parent changed during cleanup"
        )
    _pc_recovery_fsync_directory(home)


def _pc_recovery_open_or_create_primary_parent(
    primary_spec: PrivateControlRootSpec,
    plan_digest: str,
    *,
    allow_create: bool,
) -> tuple[_PrivateControlRecoveryBinding, _PrivateControlRecoveryBinding]:
    assert primary_spec.account_home is not None
    home = _pc_recovery_bind_trusted_home(primary_spec.account_home)
    parent: _PrivateControlRecoveryBinding | None = None
    temporary: _PrivateControlRecoveryBinding | None = None
    temporary_name = f".private-control-recovery-parent-{plan_digest}"
    try:
        try:
            parent = _pc_recovery_bind_child_directory(
                home,
                PRIVATE_CONTROL_NAMESPACE_NAME,
                f"primary private-control parent [{primary_spec.root_id}]",
            )
        except _PrivateControlRecoveryInitialAbsence:
            if not allow_create:
                raise MirrorSyncError(
                    "primary private-control parent disappeared after initial binding"
                )
            created_temporary = False
            try:
                os.mkdir(temporary_name, 0o700, dir_fd=home.fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot stage primary private-control parent: {error}"
                ) from error
            else:
                created_temporary = True
            temporary = _pc_recovery_bind_child_directory(
                home,
                temporary_name,
                "staged primary private-control parent",
            )
            if created_temporary:
                try:
                    os.fsync(home.fd)
                except OSError as error:
                    try:
                        _pc_recovery_remove_empty_staged_primary_parent(
                            home,
                            temporary,
                            temporary_name,
                        )
                    except MirrorSyncError as cleanup_error:
                        raise MirrorSyncError(
                            "cannot stage primary private-control parent: "
                            f"{error}; secondary staged primary-parent cleanup "
                            f"failure: {cleanup_error}"
                        ) from error
                    raise MirrorSyncError(
                        f"cannot stage primary private-control parent: {error}"
                    ) from error
            if temporary.access[0] != 0o700 or temporary.access[1] != os.geteuid():
                raise MirrorSyncError(
                    "staged primary private-control parent policy is invalid"
                )
            if not _pc_recovery_directory_is_empty(
                temporary.fd,
                "staged primary private-control parent",
            ):
                raise MirrorSyncError(
                    "staged primary private-control parent is not empty"
                )
            try:
                _rename_directory_entry_noreplace(
                    home.fd,
                    temporary_name,
                    PRIVATE_CONTROL_NAMESPACE_NAME,
                )
            except OSError as error:
                try:
                    _pc_recovery_remove_empty_staged_primary_parent(
                        home,
                        temporary,
                        temporary_name,
                    )
                except MirrorSyncError as cleanup_error:
                    raise MirrorSyncError(
                        "cannot publish primary private-control parent: "
                        f"{error}; secondary staged primary-parent cleanup "
                        f"failure: {cleanup_error}"
                    ) from error
                raise MirrorSyncError(
                    f"cannot publish primary private-control parent: {error}"
                ) from error
            temporary.path = home.path / PRIVATE_CONTROL_NAMESPACE_NAME
            temporary.label = f"primary private-control parent [{primary_spec.root_id}]"
            parent = temporary
            temporary = None
        else:
            if allow_create:
                raise MirrorSyncError(
                    "primary private-control parent appeared after initial absence"
                )
        if parent.access[0] != 0o700 or parent.access[1] != os.geteuid():
            raise MirrorSyncError(
                "primary private-control parent must be mode 0700 and current uid owned"
            )
        _pc_recovery_acquire_exclusive(parent)
        _pc_recovery_revalidate_directory(home)
        _pc_recovery_revalidate_directory(parent)
        _pc_recovery_fsync_directory(home)
        _pc_recovery_revalidate_directory(parent)
        return home, parent
    except BaseException as error:
        try:
            _pc_recovery_close_bindings((temporary, parent, home))
        except MirrorSyncError as cleanup_error:
            raise MirrorSyncError(
                f"{error}; secondary primary-parent cleanup failure: {cleanup_error}"
            ) from error
        raise


def _pc_recovery_read_bound_file(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
    *,
    operation: OperationBudget | None = None,
) -> tuple[_PrivateControlRecoveryBinding, bytes]:
    _operation_checkpoint(operation, f"binding {label}")
    try:
        path_metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {error}") from error
    if (
        not stat.S_ISREG(path_metadata.st_mode)
        or stat.S_ISLNK(path_metadata.st_mode)
        or path_metadata.st_size > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
    ):
        raise MirrorSyncError(f"{label} type/size is invalid")
    try:
        file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent.fd)
    except OSError as error:
        raise MirrorSyncError(f"cannot bind {label}: {error}") from error
    binding = _PrivateControlRecoveryBinding(
        label=label,
        path=parent.path / name,
        fd=file_fd,
        identity=(-1, -1, -1),
        access=(-1, -1, -1),
    )
    try:
        descriptor = os.fstat(file_fd)
        binding.identity = _pc_recovery_identity(descriptor)
        binding.access = _pc_recovery_access(descriptor)
        if (
            binding.identity != _pc_recovery_identity(path_metadata)
            or binding.access != _pc_recovery_access(path_metadata)
            or binding.access != (0o400, os.geteuid(), os.getegid())
        ):
            raise MirrorSyncError(f"{label} identity/access policy is invalid")

        def read_once() -> bytes:
            _operation_checkpoint(operation, f"reading {label}")
            os.lseek(file_fd, 0, os.SEEK_SET)
            payload = bytearray()
            while len(payload) <= PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                _operation_checkpoint(operation, f"reading {label}")
                chunk = os.read(
                    file_fd,
                    min(
                        1024 * 1024,
                        PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES + 1 - len(payload),
                    ),
                )
                if not chunk:
                    break
                _consume_operation_budget(
                    operation,
                    byte_count=len(chunk),
                    label=f"reading {label}",
                )
                payload.extend(chunk)
            if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                raise MirrorSyncError(f"{label} exceeds its byte cap")
            return bytes(payload)

        first = read_once()
        second = read_once()
        final_descriptor = os.fstat(file_fd)
        final_path = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if (
            first != second
            or _pc_recovery_identity(final_descriptor) != binding.identity
            or _pc_recovery_access(final_descriptor) != binding.access
            or final_descriptor.st_size != len(first)
            or _pc_recovery_identity(final_path) != binding.identity
            or _pc_recovery_access(final_path) != binding.access
            or final_path.st_size != len(first)
        ):
            raise MirrorSyncError(f"{label} changed while reading it")
        return binding, first
    except BaseException:
        os.close(file_fd)
        binding.fd = -1
        raise


def _pc_recovery_revalidate_bound_file(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    binding: _PrivateControlRecoveryBinding,
    expected_payload: bytes,
    *,
    operation: OperationBudget | None = None,
) -> None:
    def read_once() -> bytes:
        _operation_checkpoint(operation, f"revalidating {binding.label}")
        os.lseek(binding.fd, 0, os.SEEK_SET)
        payload = bytearray()
        while len(payload) <= PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
            _operation_checkpoint(operation, f"revalidating {binding.label}")
            chunk = os.read(
                binding.fd,
                min(
                    1024 * 1024,
                    PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES + 1 - len(payload),
                ),
            )
            if not chunk:
                break
            _consume_operation_budget(
                operation,
                byte_count=len(chunk),
                label=f"revalidating {binding.label}",
            )
            payload.extend(chunk)
        if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
            raise MirrorSyncError(f"{binding.label} exceeds its byte cap")
        return bytes(payload)

    try:
        first = read_once()
        second = read_once()
        descriptor = os.fstat(binding.fd)
        path_metadata = os.stat(
            name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise MirrorSyncError(f"cannot revalidate {binding.label}: {error}") from error
    if (
        first != expected_payload
        or second != expected_payload
        or _pc_recovery_identity(descriptor) != binding.identity
        or _pc_recovery_access(descriptor) != binding.access
        or descriptor.st_size != len(expected_payload)
        or _pc_recovery_identity(path_metadata) != binding.identity
        or _pc_recovery_access(path_metadata) != binding.access
        or path_metadata.st_size != len(expected_payload)
    ):
        raise MirrorSyncError(f"{binding.label} changed during verification")


def _pc_recovery_file_record(
    name: str,
    binding: _PrivateControlRecoveryBinding,
    payload: bytes,
) -> dict[str, object]:
    return {
        "access": _pc_recovery_access_document(binding.access),
        "identity": _pc_recovery_identity_document(binding.identity),
        "name": name,
        "path": str(binding.path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _pc_recovery_open_pending_publication_for_reuse(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    expected: tuple[tuple[int, int, int], tuple[int, int, int], int],
    label: str,
) -> _PrivateControlRecoveryPendingDescriptorCustody:
    expected_identity, expected_access, expected_size = expected
    read_custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
    custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
    handoff = False
    try:
        try:
            path_metadata = os.stat(
                name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            read_custody = _pc_recovery_open_pending_descriptor(
                parent,
                name,
                label,
                _pc_recovery_pending_read_flags(),
                None,
                expected_identity,
                expected_access,
            )
            read_fd = read_custody.fd
            descriptor = os.fstat(read_fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot bind pending {label} for reuse: {error}"
            ) from error
        if (
            _pc_recovery_identity(path_metadata) != expected_identity
            or _pc_recovery_identity(descriptor) != expected_identity
            or _pc_recovery_access(path_metadata) != expected_access
            or _pc_recovery_access(descriptor) != expected_access
            or path_metadata.st_nlink != 1
            or descriptor.st_nlink != 1
            or path_metadata.st_size != expected_size
            or descriptor.st_size != expected_size
        ):
            raise MirrorSyncError(f"pending {label} changed before reuse")
        if expected_access[0] == 0o400:
            try:
                os.fchmod(read_fd, 0o600)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot make pending {label} reusable: {error}"
                ) from error
        writable_access = (0o600, os.geteuid(), os.getegid())
        assert read_custody is not None
        read_custody.access = writable_access
        try:
            descriptor = os.fstat(read_fd)
            path_metadata = os.stat(
                name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot revalidate pending {label} for reuse: {error}"
            ) from error
        if (
            _pc_recovery_identity(path_metadata) != expected_identity
            or _pc_recovery_identity(descriptor) != expected_identity
            or _pc_recovery_access(path_metadata) != writable_access
            or _pc_recovery_access(descriptor) != writable_access
            or path_metadata.st_nlink != 1
            or descriptor.st_nlink != 1
            or path_metadata.st_size != expected_size
            or descriptor.st_size != expected_size
        ):
            raise MirrorSyncError(f"pending {label} changed while enabling reuse")
        flags = (
            os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            custody = _pc_recovery_open_pending_descriptor(
                parent,
                name,
                label,
                flags,
                None,
                expected_identity,
                writable_access,
            )
            file_fd = custody.fd
            descriptor = os.fstat(file_fd)
            path_metadata = os.stat(
                name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            read_descriptor = os.fstat(read_custody.fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot open pending {label} for reuse: {error}"
            ) from error
        if (
            _pc_recovery_identity(path_metadata) != expected_identity
            or _pc_recovery_identity(descriptor) != expected_identity
            or _pc_recovery_identity(read_descriptor) != expected_identity
            or _pc_recovery_access(path_metadata) != writable_access
            or _pc_recovery_access(descriptor) != writable_access
            or _pc_recovery_access(read_descriptor) != writable_access
            or path_metadata.st_nlink != 1
            or descriptor.st_nlink != 1
            or read_descriptor.st_nlink != 1
            or path_metadata.st_size != expected_size
            or descriptor.st_size != expected_size
            or read_descriptor.st_size != expected_size
        ):
            raise MirrorSyncError(f"pending {label} changed before rewrite")
        _pc_recovery_close_pending_descriptor(read_custody)
        handoff = True
        assert custody is not None
        return custody
    finally:
        if custody is not None and custody.state == "open" and not handoff:
            _pc_recovery_close_pending_descriptor(custody)
        if read_custody is not None and read_custody.state == "open":
            _pc_recovery_close_pending_descriptor(read_custody)


def _pc_recovery_handoff_pending_writer_to_reader(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
    custody: _PrivateControlRecoveryPendingDescriptorCustody,
    expected_payload: bytes,
) -> tuple[
    _PrivateControlRecoveryBinding,
    bytes,
    _PrivateControlRecoveryPendingDescriptorCustody,
]:
    binding: _PrivateControlRecoveryBinding | None = None
    reader_custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
    try:
        try:
            path_metadata = os.stat(
                name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            reader_custody = _pc_recovery_open_pending_descriptor(
                parent,
                name,
                f"{label} verifier",
                _pc_recovery_pending_read_flags(),
                None,
                custody.identity,
                custody.access,
            )
            descriptor = os.fstat(reader_custody.fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot bind pending {label}: {error}"
            ) from error
        binding = _PrivateControlRecoveryBinding(
            label=f"pending {label}",
            path=parent.path / name,
            fd=reader_custody.fd,
            identity=_pc_recovery_identity(descriptor),
            access=_pc_recovery_access(descriptor),
        )
        if (
            custody.identity is None
            or custody.access is None
            or binding.identity != custody.identity
            or binding.access != custody.access
            or _pc_recovery_identity(path_metadata) != custody.identity
            or _pc_recovery_access(path_metadata) != custody.access
            or descriptor.st_nlink != 1
            or path_metadata.st_nlink != 1
            or descriptor.st_size != len(expected_payload)
            or path_metadata.st_size != len(expected_payload)
        ):
            raise MirrorSyncError(
                f"pending {label} verifier policy is invalid"
            )

        def read_once() -> bytes:
            os.lseek(binding.fd, 0, os.SEEK_SET)
            payload = bytearray()
            while len(payload) <= PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                chunk = os.read(
                    binding.fd,
                    min(
                        1024 * 1024,
                        PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
                        + 1
                        - len(payload),
                    ),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                raise MirrorSyncError(f"pending {label} exceeds its byte cap")
            return bytes(payload)

        first = read_once()
        second = read_once()
        final_descriptor = os.fstat(binding.fd)
        final_path = os.stat(
            name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if (
            first != second
            or first != expected_payload
            or _pc_recovery_identity(final_descriptor) != binding.identity
            or _pc_recovery_access(final_descriptor) != binding.access
            or final_descriptor.st_nlink != 1
            or final_descriptor.st_size != len(first)
            or _pc_recovery_identity(final_path) != binding.identity
            or _pc_recovery_access(final_path) != binding.access
            or final_path.st_nlink != 1
            or final_path.st_size != len(first)
        ):
            raise MirrorSyncError(f"pending {label} changed while reading it")
        writer = os.fstat(custody.fd)
        path_metadata = os.stat(
            name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if (
            custody.parent_identity != parent.identity
            or custody.identity is None
            or custody.access is None
            or _pc_recovery_identity(writer) != custody.identity
            or _pc_recovery_access(writer) != custody.access
            or writer.st_nlink != 1
            or writer.st_size != len(expected_payload)
            or _pc_recovery_identity(path_metadata) != custody.identity
            or _pc_recovery_access(path_metadata) != custody.access
            or path_metadata.st_nlink != 1
            or path_metadata.st_size != len(expected_payload)
            or binding.identity != custody.identity
            or binding.access != custody.access
            or first != expected_payload
        ):
            raise MirrorSyncError(
                f"pending {label} changed before writer handoff"
            )
        _pc_recovery_close_pending_descriptor(custody)
        _pc_recovery_revalidate_bound_file(
            parent,
            name,
            binding,
            first,
        )
        return binding, first, reader_custody
    except BaseException:
        if custody.state == "open":
            _pc_recovery_close_pending_descriptor(custody)
        if custody.state == "close-uncertain":
            raise
        if reader_custody is not None and reader_custody.state == "open":
            _pc_recovery_close_pending_descriptor(reader_custody)
        raise


def _pc_recovery_publish_document(
    parent: _PrivateControlRecoveryBinding,
    final_name: str,
    pending_name: str,
    label: str,
    document_builder: Callable[[tuple[int, int, int]], dict[str, object]],
) -> tuple[_PrivateControlRecoveryBinding, bytes]:
    try:
        final_binding, final_payload = _pc_recovery_read_bound_file(
            parent,
            final_name,
            label,
        )
    except MirrorSyncError as final_error:
        try:
            os.stat(final_name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            raise final_error
        else:
            raise
    else:
        expected = _pc_recovery_json_bytes(
            document_builder(final_binding.identity),
            pretty=True,
        )
        if final_payload != expected:
            _pc_recovery_close_bindings((final_binding,))
            raise MirrorSyncError(f"existing {label} does not match this plan")
        try:
            _pc_recovery_fsync_directory(parent)
            _pc_recovery_revalidate_bound_file(
                parent,
                final_name,
                final_binding,
                final_payload,
            )
            _pc_recovery_remove_superseded_pending_publications(
                parent,
                pending_name,
                label,
            )
        except BaseException:
            _pc_recovery_close_bindings((final_binding,))
            raise
        return final_binding, final_payload

    pending_binding: _PrivateControlRecoveryBinding | None = None
    reader_custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
    try:
        try:
            pending_binding, pending_payload = _pc_recovery_read_bound_file(
                parent,
                pending_name,
                f"pending {label}",
            )
        except MirrorSyncError as pending_error:
            try:
                os.stat(pending_name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pending_payload = b""
            except OSError:
                raise pending_error
            else:
                raise
        if pending_binding is None:
            (
                pending_name,
                pending_logical_bytes,
                reusable,
            ) = _pc_recovery_select_pending_publication(parent, pending_name)
            identity: tuple[int, int, int] | None = None
            payload: bytes | None = None
            custody: _PrivateControlRecoveryPendingDescriptorCustody | None = None
            if reusable is None:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    custody = _pc_recovery_open_pending_descriptor(
                        parent,
                        pending_name,
                        label,
                        flags,
                        0o600,
                        None,
                        None,
                    )
                    file_fd = custody.fd
                except OSError as error:
                    raise MirrorSyncError(
                        f"cannot create pending {label}: {error}"
                    ) from error
            else:
                identity = reusable[0]
                payload = _pc_recovery_json_bytes(
                    document_builder(identity),
                    pretty=True,
                )
                if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                    raise MirrorSyncError(
                        f"reused pending {label} exceeds its byte cap"
                    )
                projected_logical_bytes = (
                    pending_logical_bytes - reusable[2] + len(payload)
                )
                if projected_logical_bytes > max(
                    PRIVATE_CONTROL_RECOVERY_MAX_PENDING_BYTES,
                    pending_logical_bytes,
                ):
                    raise MirrorSyncError(
                        "pending recovery publication aggregate-byte cap "
                        "would be exceeded"
                    )
                custody = _pc_recovery_open_pending_publication_for_reuse(
                    parent,
                    pending_name,
                    reusable,
                    label,
                )
                file_fd = custody.fd
            try:
                if identity is None:
                    metadata = os.fstat(file_fd)
                    identity = _pc_recovery_identity(metadata)
                    assert custody is not None
                    custody.identity = identity
                    custody.access = _pc_recovery_access(metadata)
                    payload = _pc_recovery_json_bytes(
                        document_builder(identity),
                        pretty=True,
                    )
                assert payload is not None
                if len(payload) > PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES:
                    raise MirrorSyncError(f"{label} exceeds its byte cap")
                if reusable is not None:
                    os.ftruncate(file_fd, 0)
                _pc_recovery_write_all(
                    file_fd,
                    payload,
                    label=f"writing pending {label}",
                )
                os.fchmod(file_fd, 0o400)
                assert custody is not None
                custody.access = (0o400, os.geteuid(), os.getegid())
                os.fsync(file_fd)
                pending_binding, pending_payload, reader_custody = (
                    _pc_recovery_handoff_pending_writer_to_reader(
                        parent,
                        pending_name,
                        label,
                        custody,
                        payload,
                    )
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot write pending {label}: {error}"
                ) from error
            finally:
                if custody is not None and custody.state == "open":
                    _pc_recovery_close_pending_descriptor(custody)
        expected_pending = _pc_recovery_json_bytes(
            document_builder(pending_binding.identity),
            pretty=True,
        )
        if pending_payload != expected_pending:
            raise MirrorSyncError(f"pending {label} does not match this plan")
        try:
            _rename_directory_entry_noreplace(
                parent.fd,
                pending_name,
                final_name,
            )
        except OSError as error:
            raise MirrorSyncError(f"cannot publish {label}: {error}") from error
        pending_binding.path = parent.path / final_name
        pending_binding.label = label
        if reader_custody is not None:
            reader_custody.path = pending_binding.path
            reader_custody.label = label
        _pc_recovery_fsync_directory(parent)
        final_metadata = os.stat(final_name, dir_fd=parent.fd, follow_symlinks=False)
        if (
            _pc_recovery_identity(final_metadata) != pending_binding.identity
            or _pc_recovery_access(final_metadata) != pending_binding.access
        ):
            raise MirrorSyncError(f"{label} changed during publication")
        _pc_recovery_remove_superseded_pending_publications(
            parent,
            pending_name,
            label,
        )
        if reader_custody is not None:
            _pc_recovery_release_pending_descriptor(reader_custody)
        return pending_binding, pending_payload
    except BaseException:
        other_pending_uncertainty = any(
            item is not reader_custody
            for item in _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
        )
        if (
            reader_custody is not None
            and reader_custody.state == "open"
            and not other_pending_uncertainty
        ):
            try:
                _pc_recovery_close_pending_descriptor(reader_custody)
            except BaseException:
                if pending_binding is not None and pending_binding.fd >= 0:
                    _pc_recovery_retain_close_fence((pending_binding,))
                    pending_binding = None
                raise
            if pending_binding is not None:
                pending_binding.fd = -1
        if pending_binding is not None and pending_binding.fd >= 0:
            if (
                _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
                or _PC_RECOVERY_RETAINED_CLOSE_FENCE
            ):
                _pc_recovery_retain_close_fence((pending_binding,))
            else:
                _pc_recovery_close_bindings((pending_binding,))
        raise


def _pc_recovery_load_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pc_recovery_owner_pairs,
            parse_int=_bounded_json_integer,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise MirrorSyncError(f"{label} JSON is invalid: {error}") from error
    if not isinstance(raw, dict):
        raise MirrorSyncError(f"{label} must be a JSON object")
    return raw


def _pc_recovery_optional_file(
    parent: _PrivateControlRecoveryBinding,
    name: str,
    label: str,
    *,
    operation: OperationBudget | None = None,
) -> tuple[_PrivateControlRecoveryBinding, bytes] | None:
    _operation_checkpoint(operation, f"inspecting {label}")
    try:
        os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {error}") from error
    return _pc_recovery_read_bound_file(
        parent,
        name,
        label,
        operation=operation,
    )


def _pc_recovery_primary_child_records(
    primary_parent: _PrivateControlRecoveryBinding,
    legacy_bindings: tuple[
        _PrivateControlRecoveryBinding,
        _PrivateControlRecoveryBinding,
        _PrivateControlRecoveryBinding,
    ],
) -> dict[str, object | None]:
    children: dict[str, object | None] = {}
    bound_children: list[_PrivateControlRecoveryBinding] = []
    try:
        for role, name in (
            ("tool_root", PRIVATE_TOOL_ROOT_NAME),
            ("quarantine", DURABLE_QUARANTINE_ROOT_NAME),
        ):
            try:
                os.stat(name, dir_fd=primary_parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                children[role] = None
                continue
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot inspect primary private-control {role}: {error}"
                ) from error
            child = _pc_recovery_bind_child_directory(
                primary_parent,
                name,
                f"primary private-control {role}",
            )
            bound_children.append(child)
            if child.access[0] != 0o700 or child.access[1] != os.geteuid():
                raise MirrorSyncError(
                    f"primary private-control {role} policy is invalid"
                )
            if child.identity in {binding.identity for binding in legacy_bindings}:
                raise MirrorSyncError(
                    f"primary private-control {role} aliases legacy evidence"
                )
            if not _directory_is_at_or_below(
                child.fd, primary_parent.identity
            ) or _directory_is_at_or_below(
                primary_parent.fd,
                child.identity,
            ):
                raise MirrorSyncError(
                    f"primary private-control {role} topology is invalid"
                )
            children[role] = _pc_recovery_record(child.identity, child.access)
        if (
            children.get("tool_root") is not None
            and children.get("quarantine") is not None
        ):
            tool_identity = bound_children[0].identity
            quarantine_identity = bound_children[1].identity
            if tool_identity == quarantine_identity or (
                tool_identity[0] != quarantine_identity[0]
            ):
                raise MirrorSyncError(
                    "primary private-control fixed children alias or cross filesystems"
                )
            if _directory_is_at_or_below(
                bound_children[0].fd,
                quarantine_identity,
            ) or _directory_is_at_or_below(
                bound_children[1].fd,
                tool_identity,
            ):
                raise MirrorSyncError("primary private-control fixed children overlap")
        return children
    finally:
        if bound_children:
            _pc_recovery_close_bindings(tuple(reversed(bound_children)))


def _pc_recovery_terminal_registry(
    plan: dict[str, object],
    primary_parent: _PrivateControlRecoveryBinding,
    receipt_record: dict[str, object],
    legacy_bindings: tuple[
        _PrivateControlRecoveryBinding,
        _PrivateControlRecoveryBinding,
        _PrivateControlRecoveryBinding,
    ],
) -> dict[str, object]:
    parent, tool, quarantine = legacy_bindings
    primary_children = _pc_recovery_primary_child_records(
        primary_parent,
        legacy_bindings,
    )
    inventory = plan["inventory"]
    roots = [
        {
            "fixed_children": primary_children,
            "parent": _pc_recovery_record(
                primary_parent.identity,
                primary_parent.access,
            ),
            "primary_receipt": receipt_record,
            "root_id": plan["primary_root_id"],
        },
        {
            "inventory": {
                "digest": inventory["digest"],
                "entry_count": inventory["entry_count"],
                "logical_bytes": inventory["logical_bytes"],
                "path_bytes": inventory["path_bytes"],
            },
            "parent": _pc_recovery_record(parent.identity, parent.access),
            "quarantine": _pc_recovery_record(
                quarantine.identity,
                quarantine.access,
            ),
            "root_id": plan["root_id"],
            "tool_root": _pc_recovery_record(tool.identity, tool.access),
        },
    ]
    return {"digest": _pc_recovery_digest(roots), "roots": roots}


def _pc_recovery_primary_receipt_document(
    plan: dict[str, object],
    receipt_identity: tuple[int, int, int],
) -> dict[str, object]:
    paths = plan["paths"]
    receipt_uid = os.geteuid()
    receipt_gid = os.getegid()
    late_bound_limit = (10**MAX_JSON_INTEGER_DIGITS) - 1
    if (
        not isinstance(receipt_identity, tuple)
        or len(receipt_identity) != 3
        or receipt_identity[2] != stat.S_IFREG
        or any(
            type(value) is not int or value < 0 or value > late_bound_limit
            for value in (
                receipt_identity[0],
                receipt_identity[1],
                receipt_uid,
                receipt_gid,
            )
        )
    ):
        raise MirrorSyncError(
            "primary recovery receipt late-bound identity is unsupported"
        )
    return {
        "contract": PRIVATE_CONTROL_RECOVERY_CONTRACT,
        "disposition": PRIVATE_CONTROL_RECOVERY_DISPOSITION,
        "plan": plan,
        "plan_digest": plan["plan_digest"],
        "publication": {
            "marker_name": PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            "receipt": {
                "access": _pc_recovery_access_document(
                    (0o400, receipt_uid, receipt_gid)
                ),
                "identity": _pc_recovery_identity_document(receipt_identity),
                "name": PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
                "path": paths["primary_receipt"],
            },
        },
        "root_id": plan["root_id"],
        "version": PRIVATE_CONTROL_RECOVERY_VERSION,
    }


def _pc_recovery_validate_primary_receipt_capacity(
    plan: dict[str, object],
) -> None:
    # The receipt identity and access record are not known until publication.
    # Reserve the builder-enforced digit bound for dev, ino, uid, and gid so a
    # plan accepted here fits for every later receipt binding.
    remaining = (
        PRIVATE_CONTROL_RECOVERY_MAX_RECEIPT_BYTES
        - 1
        - (4 * MAX_JSON_INTEGER_DIGITS)
    )
    if remaining < 0:
        raise MirrorSyncError("primary recovery receipt exceeds its byte cap")
    document = _pc_recovery_primary_receipt_document(
        plan,
        (0, 0, stat.S_IFREG),
    )
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    for chunk in encoder.iterencode(document):
        remaining -= len(chunk.encode("utf-8"))
        if remaining < 0:
            raise MirrorSyncError("primary recovery receipt exceeds its byte cap")


def _pc_recovery_marker_document(
    plan: dict[str, object],
    receipt_record: dict[str, object],
    terminal_registry: dict[str, object],
) -> dict[str, object]:
    return {
        "contract": PRIVATE_CONTROL_RECOVERY_CONTRACT,
        "disposition": PRIVATE_CONTROL_RECOVERY_DISPOSITION,
        "plan_digest": plan["plan_digest"],
        "primary_receipt": receipt_record,
        "root_id": plan["root_id"],
        "status": "committed",
        "terminal_registry": terminal_registry,
        "version": PRIVATE_CONTROL_RECOVERY_VERSION,
    }


def _pc_recovery_validate_primary_receipt(
    payload: bytes,
    binding: _PrivateControlRecoveryBinding,
) -> tuple[dict[str, object], dict[str, object]]:
    document = _pc_recovery_load_json(payload, "primary recovery receipt")
    if set(document) != _PRIVATE_CONTROL_RECOVERY_PRIMARY_RECEIPT_FIELDS:
        raise MirrorSyncError("primary recovery receipt schema is invalid")
    plan = _pc_recovery_validate_plan_document(document.get("plan"))
    expected = _pc_recovery_primary_receipt_document(plan, binding.identity)
    if not _pc_recovery_canonical_documents_equal(document, expected):
        raise MirrorSyncError("primary recovery receipt does not match its binding")
    return document, plan


def _pc_recovery_owner_document_is_valid(owner: object) -> bool:
    if owner is None:
        return True
    if not isinstance(owner, dict):
        return False
    version = owner.get("version")
    root_scope = owner.get("root_scope")
    fields = set(owner)
    if type(version) is not int:
        return False
    if version == PRIVATE_OWNER_RECORD_LEGACY_VERSION:
        expected_fields = PRIVATE_OWNER_RECORD_LEGACY_FIELDS | {
            "private_state",
            "root_scope",
        }
        if fields != expected_fields or root_scope != "accepted-legacy":
            return False
    elif version == PRIVATE_OWNER_RECORD_VERSION:
        expected_fields = PRIVATE_OWNER_RECORD_FIELDS | {
            "private_state",
            "root_scope",
        }
        if (
            fields != expected_fields
            or root_scope != "accepted-current"
            or owner.get("root_id") != PRIVATE_CONTROL_LEGACY_ROOT_ID
        ):
            return False
    else:
        return False
    private_identity = owner.get("private_identity")
    return bool(
        type(owner.get("owner_pid")) is int
        and owner["owner_pid"] > 0
        and type(owner.get("owner_uid")) is int
        and owner["owner_uid"] >= 0
        and type(owner.get("owner_gid")) is int
        and owner["owner_gid"] >= 0
        and isinstance(owner.get("owner_nonce"), str)
        and re.fullmatch(r"[0-9a-f]{32}", owner["owner_nonce"]) is not None
        and isinstance(owner.get("phase"), str)
        and owner.get("phase") in PRIVATE_OWNER_RECORD_PHASES
        and isinstance(owner.get("private_name"), str)
        and owner["private_name"]
        and isinstance(private_identity, list)
        and len(private_identity) == 3
        and all(type(item) is int and item >= 0 for item in private_identity)
        and isinstance(owner.get("private_state"), str)
        and owner.get("private_state") in {"matching", "missing"}
    )


def _pc_recovery_record_document_is_valid(
    record: object,
    *,
    expected_type: int,
) -> bool:
    if not isinstance(record, dict) or set(record) != {"access", "identity"}:
        return False
    access = record.get("access")
    identity = record.get("identity")
    return bool(
        isinstance(access, dict)
        and set(access) == {"gid", "mode", "uid"}
        and isinstance(access.get("mode"), str)
        and re.fullmatch(r"[0-7]{4}", access["mode"]) is not None
        and type(access.get("uid")) is int
        and access["uid"] >= 0
        and type(access.get("gid")) is int
        and access["gid"] >= 0
        and isinstance(identity, dict)
        and set(identity) == {"dev", "ino", "type"}
        and type(identity.get("dev")) is int
        and identity["dev"] >= 0
        and type(identity.get("ino")) is int
        and identity["ino"] >= 0
        and type(identity.get("type")) is int
        and identity["type"] == expected_type
    )


def _pc_recovery_file_record_document_is_valid(record: object) -> bool:
    if not isinstance(record, dict) or set(record) != {
        "access",
        "identity",
        "name",
        "path",
        "sha256",
        "size",
    }:
        return False
    return bool(
        _pc_recovery_record_document_is_valid(
            {
                "access": record.get("access"),
                "identity": record.get("identity"),
            },
            expected_type=stat.S_IFREG,
        )
        and isinstance(record.get("name"), str)
        and record["name"]
        and isinstance(record.get("path"), str)
        and record["path"]
        and isinstance(record.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None
        and type(record.get("size")) is int
        and record["size"] >= 0
    )


def _pc_recovery_validate_marker(
    payload: bytes,
) -> dict[str, object]:
    document = _pc_recovery_load_json(payload, "private-control cutover marker")
    if set(document) != _PRIVATE_CONTROL_RECOVERY_MARKER_FIELDS or (
        document.get("contract") != PRIVATE_CONTROL_RECOVERY_CONTRACT
        or type(document.get("version")) is not int
        or document["version"] != PRIVATE_CONTROL_RECOVERY_VERSION
        or document.get("disposition") != PRIVATE_CONTROL_RECOVERY_DISPOSITION
        or document.get("root_id") != PRIVATE_CONTROL_LEGACY_ROOT_ID
        or document.get("status") != "committed"
        or not isinstance(document.get("plan_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", document["plan_digest"]) is None
        or not _pc_recovery_file_record_document_is_valid(
            document.get("primary_receipt")
        )
    ):
        raise MirrorSyncError("private-control cutover marker schema is invalid")
    terminal = document.get("terminal_registry")
    roots = terminal.get("roots") if isinstance(terminal, dict) else None
    if (
        not isinstance(terminal, dict)
        or set(terminal) != {"digest", "roots"}
        or not isinstance(roots, list)
        or len(roots) != 2
        or not all(isinstance(root, dict) for root in roots)
        or set(roots[0]) != {"fixed_children", "parent", "primary_receipt", "root_id"}
        or set(roots[1])
        != {"inventory", "parent", "quarantine", "root_id", "tool_root"}
        or roots[0].get("root_id") != PRIVATE_CONTROL_PRIMARY_ROOT_ID
        or roots[1].get("root_id") != PRIVATE_CONTROL_LEGACY_ROOT_ID
        or not _pc_recovery_record_document_is_valid(
            roots[0].get("parent"),
            expected_type=stat.S_IFDIR,
        )
        or not _pc_recovery_record_document_is_valid(
            roots[1].get("parent"),
            expected_type=stat.S_IFDIR,
        )
        or not _pc_recovery_record_document_is_valid(
            roots[1].get("tool_root"),
            expected_type=stat.S_IFDIR,
        )
        or not _pc_recovery_record_document_is_valid(
            roots[1].get("quarantine"),
            expected_type=stat.S_IFDIR,
        )
        or not isinstance(roots[0].get("fixed_children"), dict)
        or set(roots[0]["fixed_children"]) != {"quarantine", "tool_root"}
        or any(
            child is not None
            and not _pc_recovery_record_document_is_valid(
                child,
                expected_type=stat.S_IFDIR,
            )
            for child in roots[0]["fixed_children"].values()
        )
        or not isinstance(roots[0].get("primary_receipt"), dict)
        or not _pc_recovery_file_record_document_is_valid(
            roots[0].get("primary_receipt")
        )
        or not isinstance(roots[1].get("inventory"), dict)
        or set(roots[1]["inventory"])
        != {"digest", "entry_count", "logical_bytes", "path_bytes"}
        or not isinstance(roots[1]["inventory"].get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", roots[1]["inventory"]["digest"])
        is None
        or any(
            type(roots[1]["inventory"].get(field)) is not int
            or roots[1]["inventory"][field] < 0
            for field in ("entry_count", "logical_bytes", "path_bytes")
        )
        or not isinstance(terminal.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", terminal["digest"]) is None
        or terminal.get("digest") != _pc_recovery_digest(roots)
    ):
        raise MirrorSyncError("cutover marker terminal registry is invalid")
    return document


def _pc_recovery_verify_adoption_locked(
    primary_parent: _PrivateControlRecoveryBinding,
    parent: _PrivateControlRecoveryBinding,
    tool: _PrivateControlRecoveryBinding,
    quarantine: _PrivateControlRecoveryBinding,
    *,
    expected_plan_digest: str | None = None,
    operation: OperationBudget | None = None,
) -> dict[str, object]:
    _operation_checkpoint(
        operation,
        "starting private-control adoption verification",
    )
    marker_result = _pc_recovery_optional_file(
        primary_parent,
        PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
        "private-control cutover marker",
        operation=operation,
    )
    if marker_result is None:
        receipt_result = _pc_recovery_optional_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            "primary recovery receipt",
            operation=operation,
        )
        if receipt_result is not None:
            _pc_recovery_close_bindings((receipt_result[0],))
            raise MirrorSyncError(
                "private-control recovery cutover is incomplete: receipt exists "
                "without its commit marker"
            )
        raise MirrorSyncError("private-control recovery cutover marker is absent")
    marker_binding, marker_payload = marker_result
    receipt_binding: _PrivateControlRecoveryBinding | None = None
    try:
        _operation_checkpoint(
            operation,
            "validating private-control cutover marker",
        )
        marker = _pc_recovery_validate_marker(marker_payload)
        receipt_result = _pc_recovery_optional_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            "primary recovery receipt",
            operation=operation,
        )
        if receipt_result is None:
            raise MirrorSyncError(
                "private-control recovery marker exists without its receipt"
            )
        receipt_binding, receipt_payload = receipt_result
        receipt_record = _pc_recovery_file_record(
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            receipt_binding,
            receipt_payload,
        )
        if not _pc_recovery_canonical_documents_equal(
            marker.get("primary_receipt"),
            receipt_record,
        ):
            raise MirrorSyncError(
                "private-control cutover marker receipt binding changed"
            )
        _operation_checkpoint(
            operation,
            "validating primary recovery receipt",
        )
        _receipt, plan = _pc_recovery_validate_primary_receipt(
            receipt_payload,
            receipt_binding,
        )
        if marker.get("plan_digest") != plan.get("plan_digest") or (
            expected_plan_digest is not None
            and plan.get("plan_digest") != expected_plan_digest
        ):
            raise MirrorSyncError("private-control recovery plan digest changed")
        primary_spec, legacy_spec = _pc_recovery_specs(str(plan["root_id"]))
        live_plan = _pc_recovery_plan_from_bindings(
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
            operation=operation,
        )
        if not _pc_recovery_canonical_documents_equal(
            _pc_recovery_protected_plan(plan),
            _pc_recovery_protected_plan(live_plan),
        ):
            raise MirrorSyncError(
                "private-control retained evidence no longer matches its receipt"
            )
        terminal = marker["terminal_registry"]
        roots = terminal["roots"]
        expected_legacy_inventory = {
            "digest": plan["inventory"]["digest"],
            "entry_count": plan["inventory"]["entry_count"],
            "logical_bytes": plan["inventory"]["logical_bytes"],
            "path_bytes": plan["inventory"]["path_bytes"],
        }
        if (
            roots[0].get("root_id") != plan["primary_root_id"]
            or roots[1].get("root_id") != plan["root_id"]
            or not _pc_recovery_canonical_documents_equal(
                roots[0].get("parent"),
                _pc_recovery_record(primary_parent.identity, primary_parent.access),
            )
            or not _pc_recovery_canonical_documents_equal(
                roots[0].get("primary_receipt"),
                receipt_record,
            )
            or not _pc_recovery_canonical_documents_equal(
                roots[1].get("parent"),
                _pc_recovery_record(parent.identity, parent.access),
            )
            or not _pc_recovery_canonical_documents_equal(
                roots[1].get("tool_root"),
                _pc_recovery_record(tool.identity, tool.access),
            )
            or not _pc_recovery_canonical_documents_equal(
                roots[1].get("quarantine"),
                _pc_recovery_record(quarantine.identity, quarantine.access),
            )
            or not _pc_recovery_canonical_documents_equal(
                roots[1].get("inventory"),
                expected_legacy_inventory,
            )
        ):
            raise MirrorSyncError(
                "private-control cutover terminal registry binding is invalid"
            )
        for binding in (parent, tool, quarantine, primary_parent):
            _pc_recovery_revalidate_directory(binding)
        _pc_recovery_revalidate_bound_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            marker_binding,
            marker_payload,
            operation=operation,
        )
        _pc_recovery_revalidate_bound_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            receipt_binding,
            receipt_payload,
            operation=operation,
        )
        return {
            "marker": _pc_recovery_file_record(
                PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                marker_binding,
                marker_payload,
            ),
            "plan": plan,
            "receipt": receipt_record,
            "terminal_registry": terminal,
        }
    finally:
        _pc_recovery_close_bindings((receipt_binding, marker_binding))


def _pc_recovery_execution_receipt(
    verification: dict[str, object],
    primary_parent: _PrivateControlRecoveryBinding,
    parent: _PrivateControlRecoveryBinding,
    tool: _PrivateControlRecoveryBinding,
    quarantine: _PrivateControlRecoveryBinding,
) -> dict[str, object]:
    plan = verification["plan"]
    current_terminal = {
        "digest": _pc_recovery_digest(
            [
                {
                    "parent": _pc_recovery_record(
                        primary_parent.identity,
                        primary_parent.access,
                    ),
                    "root_id": plan["primary_root_id"],
                },
                {
                    "inventory_digest": plan["inventory"]["digest"],
                    "parent": _pc_recovery_record(parent.identity, parent.access),
                    "quarantine": _pc_recovery_record(
                        quarantine.identity,
                        quarantine.access,
                    ),
                    "root_id": plan["root_id"],
                    "tool_root": _pc_recovery_record(tool.identity, tool.access),
                },
            ]
        ),
        "root_ids": [plan["primary_root_id"], plan["root_id"]],
        "status": "verified",
    }
    return {
        "contract": PRIVATE_CONTROL_RECOVERY_CONTRACT,
        "disposition": PRIVATE_CONTROL_RECOVERY_DISPOSITION,
        "execute": {
            "marker": verification["marker"],
            "receipt": verification["receipt"],
        },
        "plan_digest": plan["plan_digest"],
        "root_id": plan["root_id"],
        "status": "executed",
        "terminal_whole_registry_revalidation": current_terminal,
        "version": PRIVATE_CONTROL_RECOVERY_VERSION,
    }


def execute_private_control_recovery(
    requested_root_id: str,
    receipt_path: Path,
) -> dict[str, object]:
    _pc_recovery_require_no_close_fence()
    plan = _pc_recovery_read_external_plan(receipt_path, requested_root_id)
    legacy_bindings: tuple[_PrivateControlRecoveryBinding | None, ...] = ()
    primary_bindings: tuple[_PrivateControlRecoveryBinding | None, ...] = ()
    receipt_binding: _PrivateControlRecoveryBinding | None = None
    marker_binding: _PrivateControlRecoveryBinding | None = None
    try:
        (
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
            initial_primary_parent,
        ) = _pc_recovery_bind_legacy(requested_root_id, exclusive=True)
        legacy_bindings = (initial_primary_parent, quarantine, tool, parent)
        live_plan = _pc_recovery_plan_from_bindings(
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
        )
        if not _pc_recovery_canonical_documents_equal(
            _pc_recovery_protected_plan(plan),
            _pc_recovery_protected_plan(live_plan),
        ):
            raise MirrorSyncError(
                "private-control recovery plan no longer matches retained evidence"
            )
        home, primary_parent = _pc_recovery_open_or_create_primary_parent(
            primary_spec,
            str(plan["plan_digest"]),
            allow_create=initial_primary_parent is None,
        )
        primary_bindings = (primary_parent, home)
        if initial_primary_parent is not None and (
            initial_primary_parent.identity != primary_parent.identity
            or initial_primary_parent.access != primary_parent.access
        ):
            raise MirrorSyncError(
                "primary private-control parent changed after plan revalidation"
            )
        _pc_recovery_validate_topology(
            parent,
            tool,
            quarantine,
            primary_parent,
        )

        existing_marker = _pc_recovery_optional_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            "private-control cutover marker",
        )
        if existing_marker is not None:
            existing_marker_binding, existing_marker_payload = existing_marker
            try:
                existing_marker_record = _pc_recovery_file_record(
                    PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                    existing_marker_binding,
                    existing_marker_payload,
                )
                first_verification = _pc_recovery_verify_adoption_locked(
                    primary_parent,
                    parent,
                    tool,
                    quarantine,
                    expected_plan_digest=str(plan["plan_digest"]),
                )
                if not _pc_recovery_canonical_documents_equal(
                    first_verification["marker"],
                    existing_marker_record,
                ):
                    raise MirrorSyncError(
                        "private-control cutover marker changed before durability retry"
                    )
                _pc_recovery_fsync_directory(primary_parent)
                _pc_recovery_revalidate_bound_file(
                    primary_parent,
                    PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                    existing_marker_binding,
                    existing_marker_payload,
                )
                verification = _pc_recovery_verify_adoption_locked(
                    primary_parent,
                    parent,
                    tool,
                    quarantine,
                    expected_plan_digest=str(plan["plan_digest"]),
                )
                if (
                    not _pc_recovery_canonical_documents_equal(
                        verification,
                        first_verification,
                    )
                    or not _pc_recovery_canonical_documents_equal(
                        verification["marker"],
                        existing_marker_record,
                    )
                ):
                    raise MirrorSyncError(
                        "private-control cutover state changed during durability retry"
                    )
                for final_name, label in (
                    (
                        PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
                        "primary recovery receipt",
                    ),
                    (
                        PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                        "private-control cutover marker",
                    ),
                ):
                    _pc_recovery_remove_superseded_pending_publications(
                        primary_parent,
                        f".{final_name}.pending-{plan['plan_digest']}-{'0' * 32}",
                        label,
                    )
                return _pc_recovery_execution_receipt(
                    verification,
                    primary_parent,
                    parent,
                    tool,
                    quarantine,
                )
            finally:
                _pc_recovery_close_bindings((existing_marker_binding,))

        receipt_pending_name = (
            f".{PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME}.pending-"
            f"{plan['plan_digest']}-{secrets.token_hex(16)}"
        )
        receipt_binding, receipt_payload = _pc_recovery_publish_document(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            receipt_pending_name,
            "primary recovery receipt",
            lambda identity: _pc_recovery_primary_receipt_document(plan, identity),
        )
        receipt_record = _pc_recovery_file_record(
            PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
            receipt_binding,
            receipt_payload,
        )

        post_receipt_plan = _pc_recovery_plan_from_bindings(
            primary_spec,
            legacy_spec,
            parent,
            tool,
            quarantine,
        )
        if not _pc_recovery_canonical_documents_equal(
            _pc_recovery_protected_plan(plan),
            _pc_recovery_protected_plan(post_receipt_plan),
        ):
            raise MirrorSyncError(
                "private-control retained evidence changed after receipt publication"
            )
        terminal_registry = _pc_recovery_terminal_registry(
            plan,
            primary_parent,
            receipt_record,
            (parent, tool, quarantine),
        )
        marker_document = _pc_recovery_marker_document(
            plan,
            receipt_record,
            terminal_registry,
        )
        marker_pending_name = (
            f".{PRIVATE_CONTROL_RECOVERY_MARKER_NAME}.pending-"
            f"{plan['plan_digest']}-{secrets.token_hex(16)}"
        )
        marker_binding, marker_payload = _pc_recovery_publish_document(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            marker_pending_name,
            "private-control cutover marker",
            lambda _identity: marker_document,
        )
        marker_record = _pc_recovery_file_record(
            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            marker_binding,
            marker_payload,
        )
        _pc_recovery_close_bindings((receipt_binding,))
        receipt_binding = None

        verification = _pc_recovery_verify_adoption_locked(
            primary_parent,
            parent,
            tool,
            quarantine,
            expected_plan_digest=str(plan["plan_digest"]),
        )
        _pc_recovery_revalidate_bound_file(
            primary_parent,
            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
            marker_binding,
            marker_payload,
        )
        if verification["marker"] != marker_record:
            raise MirrorSyncError(
                "private-control cutover marker changed during final verification"
            )
        return _pc_recovery_execution_receipt(
            verification,
            primary_parent,
            parent,
            tool,
            quarantine,
        )
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        cleanup_groups = (
            (marker_binding, receipt_binding),
            primary_bindings,
            legacy_bindings,
        )
        cleanup_bindings = tuple(
            binding
            for bindings in cleanup_groups
            for binding in bindings
            if binding is not None
        )
        if (
            _PC_RECOVERY_PENDING_DESCRIPTOR_CUSTODY
            or _PC_RECOVERY_RETAINED_CLOSE_FENCE
        ):
            _pc_recovery_retain_close_fence(cleanup_bindings)
        elif cleanup_bindings:
            try:
                _pc_recovery_close_bindings(cleanup_bindings)
            except MirrorSyncError as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            cleanup_detail = "private-control recovery cleanup failed: " + "; ".join(
                cleanup_errors
            )
            if active_error is None:
                raise MirrorSyncError(cleanup_detail)
            print(f"warning: {cleanup_detail}", file=sys.stderr)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: PurePosixPath
    sha256: str
    mode: int


@dataclass(frozen=True)
class MirrorSpec:
    name: str
    repository: str
    files: dict[str, PurePosixPath]


@dataclass(frozen=True)
class SourceLock:
    canonical_repository: str
    sources: dict[str, SourceSpec]
    mirrors: dict[str, MirrorSpec]


@dataclass
class OperationBudget:
    deadline: float
    remaining_bytes: int
    remaining_entries: int


@dataclass(frozen=True)
class SyncHistoryLimits:
    max_commits: int = MAX_SYNC_HISTORY_COMMITS
    max_parents_per_commit: int = MAX_SYNC_HISTORY_PARENTS_PER_COMMIT
    max_parent_edges: int = MAX_SYNC_HISTORY_PARENT_EDGES
    max_path_events: int = MAX_SYNC_HISTORY_PATH_EVENTS
    max_path_bytes: int = MAX_SYNC_HISTORY_PATH_BYTES
    max_blobs: int = MAX_SYNC_HISTORY_BLOBS
    max_blob_bytes: int = MAX_SYNC_HISTORY_BLOB_BYTES


def _bounded_sync_history_limit(
    value: int,
    *,
    name: str,
    compiled_maximum: int,
) -> int:
    if value <= 0:
        raise MirrorSyncError(f"sync history {name} limit must be positive")
    if value > compiled_maximum:
        raise MirrorSyncError(
            f"sync history {name} limit cannot exceed the compiled "
            f"maximum {compiled_maximum}"
        )
    return value


def _sync_history_limits_from_args(args: argparse.Namespace) -> SyncHistoryLimits:
    return SyncHistoryLimits(
        max_commits=_bounded_sync_history_limit(
            args.max_commits,
            name="commit",
            compiled_maximum=MAX_SYNC_HISTORY_COMMITS,
        ),
        max_parents_per_commit=_bounded_sync_history_limit(
            args.max_parents_per_commit,
            name="parents-per-commit",
            compiled_maximum=MAX_SYNC_HISTORY_PARENTS_PER_COMMIT,
        ),
        max_parent_edges=_bounded_sync_history_limit(
            args.max_parent_edges,
            name="parent-edge",
            compiled_maximum=MAX_SYNC_HISTORY_PARENT_EDGES,
        ),
        max_path_events=_bounded_sync_history_limit(
            args.max_path_events,
            name="path-event",
            compiled_maximum=MAX_SYNC_HISTORY_PATH_EVENTS,
        ),
        max_path_bytes=_bounded_sync_history_limit(
            args.max_path_bytes,
            name="path-byte",
            compiled_maximum=MAX_SYNC_HISTORY_PATH_BYTES,
        ),
        max_blobs=_bounded_sync_history_limit(
            args.max_blobs,
            name="blob",
            compiled_maximum=MAX_SYNC_HISTORY_BLOBS,
        ),
        max_blob_bytes=_bounded_sync_history_limit(
            args.max_blob_bytes,
            name="blob-byte",
            compiled_maximum=MAX_SYNC_HISTORY_BLOB_BYTES,
        ),
    )


@dataclass
class BoundRoot:
    path: Path
    fd: int
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    exclusive: bool
    git_executable: ControlObjectBinding
    private_git_executable: PrivateGitExecutableBinding | None = None
    git_control: GitControlBinding | None = None
    git_capability_verified: bool = False
    operation: OperationBudget | None = None
    managed_ancestor_paths: set[PurePosixPath] = field(default_factory=set)
    managed_ancestors: dict[
        PurePosixPath,
        ControlObjectBinding,
    ] = field(default_factory=dict)


@dataclass
class ControlObjectBinding:
    label: str
    path: Path | None
    relative_path: PurePosixPath | None
    fd: int
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    content_digest: bytes | None
    root_id: str | None = None
    private_control_context: "PrimaryPrivateControlContext | None" = None


@dataclass
class PrimaryPrivateControlPrebinding:
    home: ControlObjectBinding
    parent: ControlObjectBinding | None
    tool_record: tuple[tuple[int, int, int], tuple[int, int, int]] | None
    quarantine_record: tuple[tuple[int, int, int], tuple[int, int, int]] | None

    @property
    def child_identities(self) -> set[tuple[int, int, int]]:
        identities: set[tuple[int, int, int]] = set()
        for record in (self.tool_record, self.quarantine_record):
            if record is not None:
                identities.add(record[0])
        return identities


@dataclass
class PrimaryPrivateControlContext:
    root_id: str
    home: ControlObjectBinding
    parent: ControlObjectBinding
    quarantine: ControlObjectBinding
    parent_record: tuple[tuple[int, int, int], tuple[int, int, int]]
    tool_record: tuple[tuple[int, int, int], tuple[int, int, int]]
    quarantine_record: tuple[tuple[int, int, int], tuple[int, int, int]]
    legacy_receipts: tuple[LegacyPrivateControlReceipt, ...] = ()


@dataclass(frozen=True)
class LegacyPrivateControlReceipt:
    root_id: str
    parent_path: Path
    parent_identity: tuple[int, int, int] | None
    parent_access_policy: tuple[int, int, int] | None
    tool_record: tuple[tuple[int, int, int], tuple[int, int, int]] | None
    quarantine_record: tuple[tuple[int, int, int], tuple[int, int, int]] | None
    state: str
    absence_anchor_path: Path | None = None
    absence_name: str | None = None
    absence_anchor_identity: tuple[int, int, int] | None = None
    absence_anchor_access_policy: tuple[int, int, int] | None = None
    absence_binding: ControlAbsenceBinding | None = field(
        default=None,
        compare=False,
    )
    parent_binding: ControlObjectBinding | None = field(default=None, compare=False)
    tool_binding: ControlObjectBinding | None = field(default=None, compare=False)
    quarantine_binding: ControlObjectBinding | None = field(
        default=None,
        compare=False,
    )
    adoption_plan_digest: str | None = None


@dataclass
class ControlAbsenceBinding:
    label: str
    parent: ControlObjectBinding
    name: str
    collision_key: tuple[str, ...] | None = None


@dataclass
class PrivateGitExecutableBinding:
    private_parent: ControlObjectBinding
    private: ControlObjectBinding
    private_name: str
    private_path: Path
    executable_name: str
    executable: ControlObjectBinding
    private_manifest: tuple[tuple[object, ...], ...]
    owner_record: ControlObjectBinding
    owner_record_name: str
    owner_nonce: str


@dataclass
class ControlNameSetBinding:
    label: str
    parent: ControlObjectBinding
    name: str
    expected_names: tuple[str, ...]


@dataclass
class GitControlBinding:
    marker: ControlObjectBinding
    admin: ControlObjectBinding
    commondir_file: ControlObjectBinding | None
    commondir_name_set: ControlNameSetBinding
    common: ControlObjectBinding
    objects: ControlObjectBinding
    source_files: tuple[ControlObjectBinding, ...]
    source_directories: tuple[ControlObjectBinding, ...]
    source_absences: tuple[ControlAbsenceBinding, ...]
    private_parent: ControlObjectBinding
    private: ControlObjectBinding
    private_objects: ControlObjectBinding
    private_git_executable: ControlObjectBinding
    private_git_executable_name: str
    private_name: str
    private_path: Path
    private_manifest: tuple[tuple[object, ...], ...]
    private_cleanup_manifest: tuple[tuple[object, ...], ...]
    private_objects_manifest: tuple[tuple[object, ...], ...]
    owner_record: ControlObjectBinding
    owner_record_name: str
    owner_nonce: str
    static_profile_verified: bool
    object_integrity_verified: bool


@dataclass(frozen=True)
class FileSnapshot:
    payload: bytes
    mode: int
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    size: int


@dataclass(frozen=True)
class TransactionJournalInspection:
    completion: FileSnapshot | None
    published: FileSnapshot | None
    temporary: FileSnapshot | None
    transaction: tuple[FileSnapshot, dict[str, Any]] | None


@dataclass(frozen=True)
class GitEntry:
    path: PurePosixPath
    mode: int
    object_id: str


Root = Union[Path, BoundRoot]


def _new_operation_budget() -> OperationBudget:
    return OperationBudget(
        deadline=time.monotonic() + OPERATION_TIMEOUT_SECONDS,
        remaining_bytes=MAX_OPERATION_BYTES,
        remaining_entries=MAX_OPERATION_ENTRIES,
    )


def _operation_checkpoint(
    operation: OperationBudget | None,
    label: str,
) -> None:
    if operation is not None and time.monotonic() > operation.deadline:
        raise MirrorSyncError(
            f"mirror operation exceeded {OPERATION_TIMEOUT_SECONDS} seconds "
            f"during {label}"
        )


def _consume_operation_budget(
    operation: OperationBudget | None,
    *,
    byte_count: int = 0,
    entry_count: int = 0,
    label: str,
) -> None:
    _operation_checkpoint(operation, label)
    if operation is None:
        return
    if byte_count < 0 or entry_count < 0:
        raise MirrorSyncError("operation budget consumption must be nonnegative")
    operation.remaining_bytes -= byte_count
    operation.remaining_entries -= entry_count
    if operation.remaining_bytes < 0:
        raise MirrorSyncError(
            f"mirror operation exceeds the {MAX_OPERATION_BYTES}-byte "
            f"aggregate budget during {label}"
        )
    if operation.remaining_entries < 0:
        raise MirrorSyncError(
            f"mirror operation exceeds the {MAX_OPERATION_ENTRIES}-entry "
            f"aggregate budget during {label}"
        )


def _bounded_sorted_directory_names(
    directory_fd: int,
    *,
    maximum_entries: int,
    label: str,
    limit_error: str,
    operation: OperationBudget | None = None,
) -> tuple[str, ...]:
    """Scan at most limit + 1 names, retaining no more than limit.

    The producer count is bounded independently from the retained collection:
    the first overflow entry proves the limit violation but is never appended
    or sorted. The scandir iterator is explicitly closed on every exit path.
    """
    if maximum_entries < 0:
        raise MirrorSyncError("directory scan limit must be nonnegative")
    _operation_checkpoint(operation, f"scanning {label}")
    effective_maximum = maximum_entries
    effective_limit_error = limit_error
    if operation is not None and operation.remaining_entries < effective_maximum:
        effective_maximum = max(0, operation.remaining_entries)
        effective_limit_error = (
            f"mirror operation exceeds the {MAX_OPERATION_ENTRIES}-entry "
            f"aggregate budget during scanning {label}"
        )
    names: list[str] = []
    scanned_count = 0
    entries: Any | None = None
    try:
        entries = os.scandir(directory_fd)
        for entry in entries:
            scanned_count += 1
            if scanned_count > effective_maximum:
                raise MirrorSyncError(
                    f"{effective_limit_error}; scanned at least "
                    f"{scanned_count} "
                    f"entries and retained {len(names)}"
                )
            name = entry.name
            if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
                raise MirrorSyncError(f"{label} has an unsafe entry: {name!r}")
            names.append(name)
            if scanned_count == 1 or scanned_count % 1024 == 0:
                _operation_checkpoint(operation, f"scanning {label}")
    except OSError as error:
        raise MirrorSyncError(f"cannot inventory {label}: {error}") from error
    finally:
        if entries is not None:
            active_error = sys.exc_info()[0] is not None
            try:
                entries.close()
            except OSError as error:
                close_error = MirrorSyncError(
                    f"failed to close directory scan for {label}: {error}"
                )
                if active_error:
                    print(f"warning: {close_error}", file=sys.stderr)
                else:
                    raise close_error from error
    _consume_operation_budget(
        operation,
        entry_count=len(names),
        label=f"scanning {label}",
    )
    return tuple(sorted(names))


def _require_nofollow_support() -> None:
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise MirrorSyncError(
            "this platform cannot enforce no-follow directory traversal"
        )


def _validate_relative_path(raw: object, field_name: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise MirrorSyncError(f"{field_name} must be a non-empty relative path")
    if "\0" in raw or "\\" in raw:
        raise MirrorSyncError(
            f"{field_name} must use safe POSIX path components: {raw!r}"
        )
    parts = raw.split("/")
    if raw.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise MirrorSyncError(
            f"{field_name} must be normalized and remain below its root: {raw!r}"
        )
    path = PurePosixPath(raw)
    if path.as_posix() != raw:
        raise MirrorSyncError(f"{field_name} is not normalized: {raw!r}")
    return path


def _object_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    # st_dev/st_ino/type identify the opened object. Directory timestamps are
    # intentionally excluded because benign child-entry churn does not replace it.
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
    )


def _file_stability(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    # The protected properties are object identity (device/inode/type), selected
    # access policy (mode/uid/gid), and content size. Content stability itself is
    # proved by two complete bounded reads and exact byte/hash comparison. mtime
    # and ctime are deliberately excluded because timestamp-only churn is benign.
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
    )


def _open_root(root: Path) -> int:
    _require_nofollow_support()
    root = Path(os.path.abspath(root))
    try:
        path_metadata = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect root {root}: {error}") from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(f"root must not be a symlink: {root}")
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise MirrorSyncError(f"root must be an existing directory: {root}")
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise MirrorSyncError(f"cannot safely open root {root}: {error}") from error
    opened_metadata = os.fstat(root_fd)
    if _object_identity(path_metadata) != _object_identity(opened_metadata):
        os.close(root_fd)
        raise MirrorSyncError(f"root was replaced while opening it: {root}")
    return root_fd


def _control_content_digest(
    file_fd: int,
    label: str,
) -> bytes:
    before = os.fstat(file_fd)
    if not stat.S_ISREG(before.st_mode):
        raise MirrorSyncError(f"{label} must be a regular file")
    if before.st_size > MAX_SOURCE_BYTES:
        raise MirrorSyncError(
            f"{label} exceeds the {MAX_SOURCE_BYTES}-byte control-file limit"
        )

    digests: list[bytes] = []
    for _pass in range(2):
        try:
            os.lseek(file_fd, 0, os.SEEK_SET)
        except OSError as error:
            raise MirrorSyncError(f"cannot rewind {label}: {error}") from error
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(
                    file_fd,
                    min(1024 * 1024, MAX_SOURCE_BYTES + 1 - total),
                )
            except OSError as error:
                raise MirrorSyncError(f"cannot read {label}: {error}") from error
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise MirrorSyncError(
                    f"{label} exceeds the {MAX_SOURCE_BYTES}-byte control-file limit"
                )
            digest.update(chunk)
        after = os.fstat(file_fd)
        if _file_stability(before) != _file_stability(after) or total != after.st_size:
            raise MirrorSyncError(f"{label} changed while binding it")
        digests.append(digest.digest())
    if digests[0] != digests[1]:
        raise MirrorSyncError(f"{label} content changed while binding it")
    return digests[0]


def _bind_absolute_control_object(
    path: Path,
    label: str,
    *,
    require_directory: bool,
    bind_content: bool = True,
) -> ControlObjectBinding:
    path = Path(os.path.abspath(path))
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must not be a symlink: {path}")
    if require_directory and not stat.S_ISDIR(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must be a directory: {path}")
    if not require_directory and not stat.S_ISREG(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must be a regular file: {path}")
    flags = _DIRECTORY_FLAGS if require_directory else _FILE_READ_FLAGS
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MirrorSyncError(f"cannot safely open {label} {path}: {error}") from error
    opened_metadata = os.fstat(descriptor)
    if _object_identity(path_metadata) != _object_identity(
        opened_metadata
    ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
        os.close(descriptor)
        raise MirrorSyncError(f"{label} was replaced while binding it: {path}")
    try:
        content_digest = (
            None
            if require_directory or not bind_content
            else _control_content_digest(descriptor, label)
        )
    except BaseException:
        os.close(descriptor)
        raise
    return ControlObjectBinding(
        label=label,
        path=path,
        relative_path=None,
        fd=descriptor,
        identity=_object_identity(opened_metadata),
        access_policy=_access_policy(opened_metadata),
        content_digest=content_digest,
    )


def _bind_git_marker(root: BoundRoot) -> ControlObjectBinding:
    relative_path = PurePosixPath(".git")
    try:
        path_metadata = os.stat(
            relative_path.as_posix(),
            dir_fd=root.fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inspect Git marker at {root.path}: {error}"
        ) from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(f"Git marker must not be a symlink: {root.path}")
    if stat.S_ISDIR(path_metadata.st_mode):
        flags = _DIRECTORY_FLAGS
        content_digest = None
    elif stat.S_ISREG(path_metadata.st_mode):
        flags = _FILE_READ_FLAGS
        content_digest = b""
    else:
        raise MirrorSyncError(
            f"Git marker must be a regular file or directory: {root.path}"
        )
    try:
        descriptor = os.open(
            relative_path.as_posix(),
            flags,
            dir_fd=root.fd,
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot safely open Git marker at {root.path}: {error}"
        ) from error
    opened_metadata = os.fstat(descriptor)
    if _object_identity(path_metadata) != _object_identity(
        opened_metadata
    ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
        os.close(descriptor)
        raise MirrorSyncError(f"Git marker was replaced while binding it: {root.path}")
    try:
        if content_digest is not None:
            content_digest = _control_content_digest(
                descriptor,
                f"Git marker at {root.path}",
            )
    except BaseException:
        os.close(descriptor)
        raise
    return ControlObjectBinding(
        label="Git marker",
        path=None,
        relative_path=relative_path,
        fd=descriptor,
        identity=_object_identity(opened_metadata),
        access_policy=_access_policy(opened_metadata),
        content_digest=content_digest,
    )


def _bound_control_payload(binding: ControlObjectBinding) -> bytes:
    if binding.content_digest is None:
        raise MirrorSyncError(f"{binding.label} is not a bound control file")
    before = os.fstat(binding.fd)
    try:
        os.lseek(binding.fd, 0, os.SEEK_SET)
    except OSError as error:
        raise MirrorSyncError(f"cannot rewind {binding.label}: {error}") from error
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(
            binding.fd,
            min(4096, MAX_SOURCE_BYTES + 1 - total),
        )
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise MirrorSyncError(f"{binding.label} exceeds the control-file limit")
        chunks.append(chunk)
    payload = b"".join(chunks)
    after = os.fstat(binding.fd)
    if _file_stability(before) != _file_stability(after):
        raise MirrorSyncError(f"{binding.label} changed while reading it")
    if hashlib.sha256(payload).digest() != binding.content_digest:
        raise MirrorSyncError(f"{binding.label} content changed after it was bound")
    return payload


def _duplicate_directory_control(
    source: ControlObjectBinding,
    path: Path,
    label: str,
) -> ControlObjectBinding:
    descriptor = os.dup(source.fd)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise MirrorSyncError(f"{label} must be a directory")
    return ControlObjectBinding(
        label=label,
        path=Path(os.path.abspath(path)),
        relative_path=None,
        fd=descriptor,
        identity=_object_identity(metadata),
        access_policy=_access_policy(metadata),
        content_digest=None,
    )


def _bind_relative_control_file(
    parent: ControlObjectBinding,
    name: str,
    label: str,
) -> ControlObjectBinding | None:
    assert parent.path is not None
    try:
        path_metadata = os.stat(
            name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISREG(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must be a regular file")
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent.fd)
    except OSError as error:
        raise MirrorSyncError(f"cannot safely open {label}: {error}") from error
    opened_metadata = os.fstat(descriptor)
    if _object_identity(path_metadata) != _object_identity(
        opened_metadata
    ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
        os.close(descriptor)
        raise MirrorSyncError(f"{label} was replaced while binding it")
    try:
        content_digest = _control_content_digest(descriptor, label)
    except BaseException:
        os.close(descriptor)
        raise
    return ControlObjectBinding(
        label=label,
        path=parent.path / name,
        relative_path=None,
        fd=descriptor,
        identity=_object_identity(opened_metadata),
        access_policy=_access_policy(opened_metadata),
        content_digest=content_digest,
    )


def _control_marker_collision_names(
    parent: ControlObjectBinding,
    name: str,
    label: str,
) -> tuple[str, ...]:
    marker_key = _path_collision_key(PurePosixPath(name))
    names = _bounded_sorted_directory_names(
        parent.fd,
        maximum_entries=MAX_GIT_SNAPSHOT_ENTRIES,
        label=f"{label} parent",
        limit_error=(
            f"{label} parent exceeds the {MAX_GIT_SNAPSHOT_ENTRIES}-entry limit"
        ),
    )
    collisions: list[str] = []
    for candidate in names:
        if _path_collision_key(PurePosixPath(candidate)) == marker_key:
            collisions.append(candidate)
    return tuple(sorted(collisions))


def _bind_unique_relative_control_file(
    parent: ControlObjectBinding,
    name: str,
    label: str,
) -> tuple[ControlObjectBinding | None, ControlNameSetBinding]:
    collisions = _control_marker_collision_names(parent, name, label)
    if collisions and collisions != (name,):
        raise MirrorSyncError(
            f"{label} has a case/canonical collision alias: "
            + ", ".join(repr(candidate) for candidate in collisions)
        )
    binding = _bind_relative_control_file(parent, name, label)
    confirmed = _control_marker_collision_names(parent, name, label)
    expected = (name,) if binding is not None else ()
    if confirmed != expected:
        if binding is not None:
            os.close(binding.fd)
        raise MirrorSyncError(f"{label} changed while binding its unique name")
    return (
        binding,
        ControlNameSetBinding(
            label=label,
            parent=parent,
            name=name,
            expected_names=expected,
        ),
    )


def _bind_collision_aware_control_absence(
    parent: ControlObjectBinding,
    name: str,
    label: str,
) -> ControlAbsenceBinding:
    binding = ControlAbsenceBinding(
        label=label,
        parent=parent,
        name=name,
        collision_key=_path_collision_key(PurePosixPath(name)),
    )
    collisions = _control_marker_collision_names(parent, name, label)
    if collisions:
        raise MirrorSyncError(
            f"{label} appeared before private Git materialization: "
            + ", ".join(repr(candidate) for candidate in collisions)
        )
    return binding


def _bind_relative_control_directory(
    parent: ControlObjectBinding,
    name: str,
    label: str,
) -> ControlObjectBinding | None:
    assert parent.path is not None
    try:
        path_metadata = os.stat(
            name,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label}: {error}") from error
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise MirrorSyncError(f"{label} must be a directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent.fd)
    except OSError as error:
        raise MirrorSyncError(f"cannot safely open {label}: {error}") from error
    opened_metadata = os.fstat(descriptor)
    if _object_identity(path_metadata) != _object_identity(
        opened_metadata
    ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
        os.close(descriptor)
        raise MirrorSyncError(f"{label} was replaced while binding it")
    return ControlObjectBinding(
        label=label,
        path=parent.path / name,
        relative_path=None,
        fd=descriptor,
        identity=_object_identity(opened_metadata),
        access_policy=_access_policy(opened_metadata),
        content_digest=None,
    )


def _parse_control_path(
    payload: bytes,
    *,
    prefix: bytes,
    base: Path,
    label: str,
) -> Path:
    value = payload
    if value.endswith(b"\n"):
        value = value[:-1]
    if (
        not value.startswith(prefix)
        or not value[len(prefix) :]
        or b"\0" in value
        or b"\n" in value
        or b"\r" in value
    ):
        raise MirrorSyncError(f"{label} has an invalid path record")
    raw_path = os.fsdecode(value[len(prefix) :])
    parsed = Path(raw_path)
    if not parsed.is_absolute():
        parsed = base / parsed
    return Path(os.path.abspath(parsed))


def _read_git_snapshot_file(
    file_fd: int,
    display_path: PurePosixPath,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(file_fd)
    if before.st_size > maximum_bytes:
        raise MirrorSyncError(
            f"Git control snapshot exceeds its byte budget: {display_path}"
        )
    payloads: list[bytes] = []
    for _pass in range(2):
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                file_fd,
                min(1024 * 1024, maximum_bytes + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise MirrorSyncError(
                    f"Git control snapshot exceeds its byte budget: {display_path}"
                )
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(file_fd)
        if _file_stability(before) != _file_stability(after):
            raise MirrorSyncError(
                f"Git control file changed while snapshotting: {display_path}"
            )
        payloads.append(payload)
    if payloads[0] != payloads[1]:
        raise MirrorSyncError(
            f"Git control file bytes changed while snapshotting: {display_path}"
        )
    return payloads[0], before


def _snapshot_git_directory_tree(
    source_fd: int,
    destination_fd: int | None,
    *,
    prefix: PurePosixPath,
    budget: dict[str, int],
    operation: OperationBudget | None = None,
    skip_descendants: frozenset[PurePosixPath] = frozenset(),
    skip_entries: frozenset[PurePosixPath] = frozenset(),
) -> list[tuple[object, ...]]:
    _operation_checkpoint(operation, f"snapshotting Git control tree {prefix}")
    scanned_entries = budget.setdefault("scanned_entries", 0)
    names = _bounded_sorted_directory_names(
        source_fd,
        maximum_entries=max(0, MAX_GIT_SNAPSHOT_ENTRIES - scanned_entries),
        label=f"Git control directory {prefix}",
        limit_error="Git control snapshot exceeds its entry limit",
        operation=operation,
    )
    budget["scanned_entries"] = scanned_entries + len(names)
    manifest: list[tuple[object, ...]] = []
    for name in names:
        relative_path = prefix / name
        if relative_path in skip_entries:
            continue
        budget["entries"] += 1
        if budget["entries"] > MAX_GIT_SNAPSHOT_ENTRIES:
            raise MirrorSyncError("Git control snapshot exceeds its entry limit")
        try:
            path_metadata = os.stat(
                name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inspect Git control entry {relative_path}: {error}"
            ) from error
        if stat.S_ISDIR(path_metadata.st_mode):
            try:
                child_source_fd = os.open(
                    name,
                    _DIRECTORY_FLAGS,
                    dir_fd=source_fd,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot open Git control directory {relative_path}: {error}"
                ) from error
            opened_metadata = os.fstat(child_source_fd)
            if _object_identity(path_metadata) != _object_identity(opened_metadata):
                os.close(child_source_fd)
                raise MirrorSyncError(
                    f"Git control directory was replaced: {relative_path}"
                )
            child_destination_fd = -1
            try:
                if destination_fd is not None:
                    os.mkdir(
                        name,
                        stat.S_IMODE(opened_metadata.st_mode),
                        dir_fd=destination_fd,
                    )
                    child_destination_fd = os.open(
                        name,
                        _DIRECTORY_FLAGS,
                        dir_fd=destination_fd,
                    )
                    os.fchmod(
                        child_destination_fd,
                        stat.S_IMODE(opened_metadata.st_mode),
                    )
                manifest.append(
                    (
                        "directory",
                        relative_path.as_posix(),
                        _object_identity(opened_metadata),
                        _access_policy(opened_metadata),
                    )
                )
                if relative_path not in skip_descendants:
                    manifest.extend(
                        _snapshot_git_directory_tree(
                            child_source_fd,
                            (
                                child_destination_fd
                                if child_destination_fd >= 0
                                else None
                            ),
                            prefix=relative_path,
                            budget=budget,
                            operation=operation,
                            skip_descendants=skip_descendants,
                            skip_entries=skip_entries,
                        )
                    )
            finally:
                if child_destination_fd >= 0:
                    os.close(child_destination_fd)
                os.close(child_source_fd)
        elif stat.S_ISREG(path_metadata.st_mode):
            remaining = MAX_GIT_SNAPSHOT_BYTES - budget["bytes"]
            if remaining <= 0:
                raise MirrorSyncError("Git control snapshot exceeds its byte limit")
            try:
                source_file_fd = os.open(
                    name,
                    _FILE_READ_FLAGS,
                    dir_fd=source_fd,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot open Git control file {relative_path}: {error}"
                ) from error
            try:
                opened_metadata = os.fstat(source_file_fd)
                if _object_identity(path_metadata) != _object_identity(opened_metadata):
                    raise MirrorSyncError(
                        f"Git control file was replaced: {relative_path}"
                    )
                payload, final_metadata = _read_git_snapshot_file(
                    source_file_fd,
                    relative_path,
                    remaining,
                )
            finally:
                os.close(source_file_fd)
            budget["bytes"] += len(payload)
            _consume_operation_budget(
                operation,
                byte_count=len(payload),
                label=f"snapshotting Git control file {relative_path}",
            )
            manifest.append(
                (
                    "file",
                    relative_path.as_posix(),
                    _object_identity(final_metadata),
                    _access_policy(final_metadata),
                    len(payload),
                    hashlib.sha256(payload).digest(),
                )
            )
            if destination_fd is not None:
                destination_file_fd = os.open(
                    name,
                    _FILE_WRITE_FLAGS,
                    stat.S_IMODE(final_metadata.st_mode),
                    dir_fd=destination_fd,
                )
                try:
                    os.fchmod(
                        destination_file_fd,
                        stat.S_IMODE(final_metadata.st_mode),
                    )
                    _write_all(
                        destination_file_fd,
                        payload,
                        relative_path,
                    )
                    os.fsync(destination_file_fd)
                finally:
                    os.close(destination_file_fd)
        else:
            raise MirrorSyncError(
                f"Git control entry is not a regular file/directory: {relative_path}"
            )
    return manifest


def _logical_git_snapshot_manifest(
    manifest: list[tuple[object, ...]],
) -> tuple[tuple[object, ...], ...]:
    logical: list[tuple[object, ...]] = []
    for record in manifest:
        if record[0] == "directory":
            access_policy = record[3]
            assert isinstance(access_policy, tuple)
            logical.append(("directory", record[1], access_policy[0]))
        elif record[0] == "file":
            access_policy = record[3]
            assert isinstance(access_policy, tuple)
            logical.append(
                (
                    "file",
                    record[1],
                    access_policy[0],
                    record[4],
                    record[5],
                )
            )
        else:
            raise MirrorSyncError("Git control snapshot produced an unknown record")
    return tuple(logical)


def _git_control_plane_manifest(
    manifest: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    object_prefix = PRIVATE_OBJECTS_PATH.as_posix() + "/"
    return tuple(
        record for record in manifest if not str(record[1]).startswith(object_prefix)
    )


def _scan_private_git_tree(
    private_fd: int,
    operation: OperationBudget | None = None,
    *,
    skip_descendants: frozenset[PurePosixPath] = frozenset(),
    skip_entries: frozenset[PurePosixPath] = frozenset(),
) -> tuple[tuple[object, ...], ...]:
    first_budget = {"entries": 0, "bytes": 0}
    first = _snapshot_git_directory_tree(
        private_fd,
        None,
        prefix=PurePosixPath(),
        budget=first_budget,
        operation=operation,
        skip_descendants=skip_descendants,
        skip_entries=skip_entries,
    )
    second_budget = {"entries": 0, "bytes": 0}
    second = _snapshot_git_directory_tree(
        private_fd,
        None,
        prefix=PurePosixPath(),
        budget=second_budget,
        operation=operation,
        skip_descendants=skip_descendants,
        skip_entries=skip_entries,
    )
    first_logical = _logical_git_snapshot_manifest(first)
    second_logical = _logical_git_snapshot_manifest(second)
    if first_logical != second_logical:
        raise MirrorSyncError(
            "private Git control snapshot changed while validating it"
        )
    return first_logical


def _scan_private_git_control(
    private_fd: int,
    operation: OperationBudget | None = None,
    *,
    skip_entries: frozenset[PurePosixPath] = frozenset(),
) -> tuple[tuple[object, ...], ...]:
    return _scan_private_git_tree(
        private_fd,
        operation,
        skip_descendants=frozenset({PRIVATE_OBJECTS_PATH}),
        skip_entries=skip_entries,
    )


def _private_object_content_signal(
    metadata: os.stat_result,
) -> tuple[object, ...]:
    # Unlike mutable directory timestamps, the selected timestamps on a
    # private object file are change signals for the protected content-stability
    # property. Git is only allowed to read this private snapshot. A timestamp
    # change therefore invalidates the proof even if a later read happens to
    # recover the original bytes; it is not reported as proof of a byte mismatch.
    return (
        _object_identity(metadata),
        _access_policy(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inventory_private_git_objects(
    objects_fd: int,
    operation: OperationBudget | None,
    *,
    bind_content: bool,
) -> tuple[tuple[object, ...], ...]:
    records: list[tuple[object, ...]] = []
    budget = {"entries": 0, "bytes": 0}

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        _operation_checkpoint(
            operation,
            f"binding private Git object snapshot {prefix}",
        )
        scanned_entries = budget.setdefault("scanned_entries", 0)
        names = _bounded_sorted_directory_names(
            directory_fd,
            maximum_entries=max(0, MAX_GIT_SNAPSHOT_ENTRIES - scanned_entries),
            label=f"private Git object directory {prefix}",
            limit_error="private Git object snapshot exceeds its entry limit",
            operation=operation,
        )
        budget["scanned_entries"] = scanned_entries + len(names)
        for name in names:
            relative_path = prefix / name
            budget["entries"] += 1
            if budget["entries"] > MAX_GIT_SNAPSHOT_ENTRIES:
                raise MirrorSyncError(
                    "private Git object snapshot exceeds its entry limit"
                )
            try:
                path_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot inspect private Git object {relative_path}: {error}"
                ) from error
            if stat.S_ISDIR(path_metadata.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        _DIRECTORY_FLAGS,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    raise MirrorSyncError(
                        f"cannot open private Git object directory "
                        f"{relative_path}: {error}"
                    ) from error
                try:
                    opened_metadata = os.fstat(child_fd)
                    if _object_identity(path_metadata) != _object_identity(
                        opened_metadata
                    ) or _access_policy(path_metadata) != _access_policy(
                        opened_metadata
                    ):
                        raise MirrorSyncError(
                            "private Git object directory was replaced while "
                            f"binding it: {relative_path}"
                        )
                    records.append(
                        (
                            "directory",
                            relative_path.as_posix(),
                            _object_identity(opened_metadata),
                            _access_policy(opened_metadata),
                        )
                    )
                    visit(child_fd, relative_path)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(path_metadata.st_mode):
                raise MirrorSyncError(
                    "private Git object entry is not a regular file/directory: "
                    f"{relative_path}"
                )
            try:
                file_fd = os.open(
                    name,
                    _FILE_READ_FLAGS,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot open private Git object file {relative_path}: {error}"
                ) from error
            try:
                opened_metadata = os.fstat(file_fd)
                if _object_identity(path_metadata) != _object_identity(
                    opened_metadata
                ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
                    raise MirrorSyncError(
                        "private Git object file was replaced while binding it: "
                        f"{relative_path}"
                    )
                signal = _private_object_content_signal(opened_metadata)
                digest: bytes | None = None
                if bind_content:
                    remaining = MAX_GIT_SNAPSHOT_BYTES - budget["bytes"]
                    if remaining <= 0:
                        raise MirrorSyncError(
                            "private Git object snapshot exceeds its byte limit"
                        )
                    payload, _bound_metadata = _read_git_snapshot_file(
                        file_fd,
                        PRIVATE_OBJECTS_PATH / relative_path,
                        remaining,
                    )
                    final_metadata = os.fstat(file_fd)
                    if _private_object_content_signal(final_metadata) != signal:
                        raise MirrorSyncError(
                            "private Git object content-stability proof was "
                            f"invalidated while binding it: {relative_path}"
                        )
                    budget["bytes"] += len(payload)
                    _consume_operation_budget(
                        operation,
                        byte_count=len(payload),
                        label=f"binding private Git object {relative_path}",
                    )
                    digest = hashlib.sha256(payload).digest()
                records.append(
                    (
                        "file",
                        relative_path.as_posix(),
                        *signal,
                        digest,
                    )
                )
            finally:
                os.close(file_fd)

    visit(objects_fd, PurePosixPath())
    return tuple(records)


def _object_manifest_without_digests(
    manifest: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        record[:-1] + (None,) if record[0] == "file" else record for record in manifest
    )


def _bind_private_git_objects_manifest(
    objects_fd: int,
    operation: OperationBudget | None,
) -> tuple[tuple[object, ...], ...]:
    manifest = _inventory_private_git_objects(
        objects_fd,
        operation,
        bind_content=True,
    )
    observed = _inventory_private_git_objects(
        objects_fd,
        operation,
        bind_content=False,
    )
    if observed != _object_manifest_without_digests(manifest):
        raise MirrorSyncError(
            "private Git object snapshot changed while binding its content"
        )
    return manifest


def _revalidate_private_git_objects(
    objects_fd: int,
    expected: tuple[tuple[object, ...], ...],
    operation: OperationBudget | None,
) -> None:
    observed = _inventory_private_git_objects(
        objects_fd,
        operation,
        bind_content=False,
    )
    if observed != _object_manifest_without_digests(expected):
        raise MirrorSyncError(
            "private Git object snapshot content-stability proof was "
            "invalidated before transaction completion"
        )


def _replace_private_control_file(
    source_parent: ControlObjectBinding,
    private_fd: int,
    name: str,
    *,
    required: bool,
) -> ControlObjectBinding | None:
    binding = _bind_relative_control_file(
        source_parent,
        name,
        f"Git {name} control file",
    )
    if binding is None:
        if required:
            raise MirrorSyncError(f"Git {name} control file is missing")
        try:
            os.unlink(name, dir_fd=private_fd)
        except FileNotFoundError:
            pass
        return None
    payload = _bound_control_payload(binding)
    try:
        os.unlink(name, dir_fd=private_fd)
    except FileNotFoundError:
        pass
    destination_fd = os.open(
        name,
        _FILE_WRITE_FLAGS,
        binding.access_policy[0],
        dir_fd=private_fd,
    )
    try:
        os.fchmod(destination_fd, binding.access_policy[0])
        _write_all(
            destination_fd,
            payload,
            PurePosixPath(name),
        )
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    return binding


def _directory_bindings_overlap(
    left_fd: int,
    right_fd: int,
) -> bool:
    left_identity = _object_identity(os.fstat(left_fd))
    right_identity = _object_identity(os.fstat(right_fd))
    return _directory_is_at_or_below(
        left_fd,
        right_identity,
    ) or _directory_is_at_or_below(
        right_fd,
        left_identity,
    )


def _validate_private_control_root_topology(
    *,
    root_id: str,
    parent: ControlObjectBinding,
    tool_root: ControlObjectBinding | None,
    quarantine: ControlObjectBinding | None,
    repository_root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    home: ControlObjectBinding | None = None,
    additional_protected: tuple[tuple[str, int], ...] = (),
) -> None:
    """Prove the directory relationships needed for safe private-control moves."""

    reason = f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]"
    if home is not None:
        if (
            parent.identity == home.identity
            or not _directory_is_at_or_below(parent.fd, home.identity)
            or _directory_is_at_or_below(home.fd, parent.identity)
        ):
            raise MirrorSyncError(
                f"{reason}: private-control allocation root is not a strict "
                "descendant of the bound account home"
            )
    children = tuple(
        (label, binding)
        for label, binding in (
            ("private Git tool root", tool_root),
            ("durable quarantine root", quarantine),
        )
        if binding is not None
    )
    for label, binding in children:
        if (
            binding.identity == parent.identity
            or not _directory_is_at_or_below(binding.fd, parent.identity)
            or _directory_is_at_or_below(parent.fd, binding.identity)
        ):
            raise MirrorSyncError(
                f"{reason}: {label} is not a strict descendant of its bound "
                "fixed-name parent"
            )
    if tool_root is not None and quarantine is not None:
        tool_metadata = os.fstat(tool_root.fd)
        quarantine_metadata = os.fstat(quarantine.fd)
        if tool_metadata.st_dev != quarantine_metadata.st_dev:
            raise MirrorSyncError(
                f"{reason}: private Git tool root and durable quarantine root "
                "must be on the same filesystem"
            )
        if _directory_bindings_overlap(tool_root.fd, quarantine.fd):
            raise MirrorSyncError(
                f"{reason}: private Git tool root and durable quarantine root "
                "must not overlap"
            )
    protected = (
        ("repository root", repository_root.fd),
        ("Git admin directory", admin.fd),
        ("Git common directory", common.fd),
        *additional_protected,
    )
    for child_label, child in children:
        for protected_label, protected_fd in protected:
            if _directory_bindings_overlap(child.fd, protected_fd):
                raise MirrorSyncError(
                    f"{reason}: {child_label} overlaps {protected_label}"
                )


def _close_control_bindings_best_effort(
    bindings: tuple[ControlObjectBinding | None, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    closed_fds: set[int] = set()
    for binding in bindings:
        if binding is None or binding.fd < 0:
            continue
        fd = binding.fd
        if fd in closed_fds:
            binding.fd = -1
            continue
        closed_fds.add(fd)
        try:
            os.close(fd)
        except OSError as error:
            errors.append(f"cannot close {binding.label}: {error}")
        finally:
            # A failed close leaves descriptor state unspecified; never retry.
            binding.fd = -1
    return tuple(errors)


def _close_raw_descriptors_best_effort(
    descriptors: tuple[tuple[str, int], ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    closed_fds: set[int] = set()
    for label, descriptor in descriptors:
        if descriptor < 0 or descriptor in closed_fds:
            continue
        closed_fds.add(descriptor)
        try:
            os.close(descriptor)
        except OSError as error:
            errors.append(f"cannot close {label}: {error}")
    return tuple(errors)


def _raise_with_secondary_cleanup(
    primary_error: BaseException,
    label: str,
    cleanup_errors: tuple[str, ...] | list[str],
) -> NoReturn:
    if cleanup_errors:
        raise MirrorSyncError(
            f"{primary_error}; secondary {label}: " + "; ".join(cleanup_errors)
        ) from primary_error
    raise primary_error


def _private_control_primary_spec() -> PrivateControlRootSpec:
    root_ids = [spec.root_id for spec in PRIVATE_CONTROL_ROOT_SPECS]
    if len(root_ids) != len(set(root_ids)):
        raise MirrorSyncError("private-control root ids must be unique")
    candidates = [spec for spec in PRIVATE_CONTROL_ROOT_SPECS if spec.allocate]
    if len(candidates) != 1:
        raise MirrorSyncError(
            "private-control registry must contain exactly one allocation root"
        )
    primary = candidates[0]
    if (
        primary.shared_parent
        or primary.account_home is None
        or primary.parent_path != primary.account_home / PRIVATE_CONTROL_NAMESPACE_NAME
    ):
        raise MirrorSyncError("private-control allocation root schema is invalid")
    return primary


def _bind_trusted_account_home(path: Path) -> ControlObjectBinding:
    path = Path(os.path.abspath(path))
    if not path.is_absolute() or path == Path("/"):
        raise MirrorSyncError("canonical account home must be an absolute child path")
    components = path.parts[1:]
    if not components or len(components) > PRIVATE_CONTROL_MAX_ANCESTORS:
        raise MirrorSyncError("canonical account home exceeds its ancestor limit")
    current_path = Path("/")
    current_fd = os.open(current_path, _DIRECTORY_FLAGS)
    try:
        for component in components:
            child_fd = -1
            try:
                path_metadata = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(path_metadata.st_mode):
                    raise MirrorSyncError(
                        "canonical account-home ancestors must be non-symlink "
                        f"directories: {current_path / component}"
                    )
                child_fd = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=current_fd,
                )
                child_metadata = os.fstat(child_fd)
                if _object_identity(path_metadata) != _object_identity(
                    child_metadata
                ) or _access_policy(path_metadata) != _access_policy(child_metadata):
                    raise MirrorSyncError(
                        "canonical account-home ancestor changed while binding it: "
                        f"{current_path / component}"
                    )
                mode, uid, _gid = _access_policy(child_metadata)
                if uid not in {0, os.geteuid()} or mode & 0o022:
                    raise MirrorSyncError(
                        "canonical account-home ancestors must be root/current-owned "
                        "and not group/world writable: "
                        f"{current_path / component}"
                    )
            except OSError as error:
                close_errors = _close_raw_descriptors_best_effort(
                    (("unadopted account-home ancestor", child_fd),)
                )
                child_fd = -1
                _raise_with_secondary_cleanup(
                    MirrorSyncError(
                        f"cannot bind canonical account-home ancestor "
                        f"{current_path / component}: {error}"
                    ),
                    "account-home child cleanup failures",
                    close_errors,
                )
            except BaseException as error:
                close_errors = _close_raw_descriptors_best_effort(
                    (("unadopted account-home ancestor", child_fd),)
                )
                child_fd = -1
                _raise_with_secondary_cleanup(
                    error,
                    "account-home child cleanup failures",
                    close_errors,
                )
            previous_fd = current_fd
            current_fd = child_fd
            child_fd = -1
            current_path /= component
            close_errors = _close_raw_descriptors_best_effort(
                (("previous account-home ancestor", previous_fd),)
            )
            if close_errors:
                raise MirrorSyncError("; ".join(close_errors))
        metadata = os.fstat(current_fd)
        mode, uid, _gid = _access_policy(metadata)
        if uid != os.geteuid() or mode & 0o022:
            raise MirrorSyncError(
                "canonical account home must be owned by the current uid and "
                "not group/world writable"
            )
        binding = ControlObjectBinding(
            label="canonical account home",
            path=path,
            relative_path=None,
            fd=current_fd,
            identity=_object_identity(metadata),
            access_policy=_access_policy(metadata),
            content_digest=None,
        )
        current_fd = -1
        return binding
    except BaseException as error:
        close_errors = _close_raw_descriptors_best_effort(
            (("current account-home ancestor", current_fd),)
        )
        current_fd = -1
        _raise_with_secondary_cleanup(
            error,
            "account-home cleanup failures",
            close_errors,
        )


def _bind_primary_private_control_parent_impl(
    spec: PrivateControlRootSpec,
    *,
    create: bool,
) -> tuple[ControlObjectBinding, ControlObjectBinding | None]:
    assert spec.account_home is not None
    home = _bind_trusted_account_home(spec.account_home)
    parent: ControlObjectBinding | None = None
    try:
        try:
            initial_metadata = os.stat(
                PRIVATE_CONTROL_NAMESPACE_NAME,
                dir_fd=home.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not create:
                return home, None
            parent = _create_private_control_directory_noreplace(
                home,
                PRIVATE_CONTROL_NAMESPACE_NAME,
                f"private-control allocation root [{spec.root_id}]",
                spec.root_id,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inspect private-control allocation root "
                f"[{spec.root_id}]: {error}"
            ) from error
        else:
            expected_record = (
                _object_identity(initial_metadata),
                _access_policy(initial_metadata),
            )
            parent = _bind_existing_private_control_directory(
                home,
                PRIVATE_CONTROL_NAMESPACE_NAME,
                f"private-control allocation root [{spec.root_id}]",
                spec.root_id,
                expected_record,
            )
        if parent.access_policy[0] != 0o700 or parent.access_policy[1] != os.geteuid():
            raise MirrorSyncError(
                f"private-control allocation root [{spec.root_id}] must be "
                "mode 0700 and owned by the current uid"
            )
        _revalidate_absolute_control_object(home)
        return home, parent
    except BaseException as error:
        close_errors = _close_control_bindings_best_effort((parent, home))
        _raise_with_secondary_cleanup(
            error,
            "primary parent bind cleanup failures",
            close_errors,
        )


def _bind_primary_private_control_parent(
    spec: PrivateControlRootSpec,
) -> tuple[ControlObjectBinding, ControlObjectBinding]:
    home, parent = _bind_primary_private_control_parent_impl(spec, create=True)
    assert parent is not None
    return home, parent


def _prebind_existing_primary_private_control_root(
    spec: PrivateControlRootSpec,
) -> PrimaryPrivateControlPrebinding:
    home, parent = _bind_primary_private_control_parent_impl(spec, create=False)
    if parent is None:
        return PrimaryPrivateControlPrebinding(
            home=home,
            parent=None,
            tool_record=None,
            quarantine_record=None,
        )
    try:
        child_records: dict[
            str,
            tuple[tuple[int, int, int], tuple[int, int, int]] | None,
        ] = {}
        for name, label in (
            (PRIVATE_TOOL_ROOT_NAME, "private Git tool root"),
            (DURABLE_QUARANTINE_ROOT_NAME, "durable quarantine root"),
        ):
            child_records[name] = _private_control_child_metadata(
                parent.fd,
                name,
                f"existing primary {label} [{spec.root_id}]",
            )
        observed = tuple(
            record for record in child_records.values() if record is not None
        )
        observed_identities = {record[0] for record in observed}
        if len(observed_identities) != len(observed):
            raise MirrorSyncError(
                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                "existing primary fixed child roles alias each other"
            )
        if parent.identity in observed_identities:
            raise MirrorSyncError(
                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                "existing primary fixed child aliases its own parent"
            )
        for name, label in (
            (PRIVATE_TOOL_ROOT_NAME, "private Git tool root"),
            (DURABLE_QUARANTINE_ROOT_NAME, "durable quarantine root"),
        ):
            expected_record = child_records[name]
            if expected_record is None:
                continue
            child = _bind_existing_private_control_directory(
                parent,
                name,
                f"existing primary {label} [{spec.root_id}]",
                spec.root_id,
                expected_record,
            )
            child_error: BaseException | None = None
            try:
                if (
                    child.access_policy[0] != 0o700
                    or child.access_policy[1] != os.geteuid()
                ):
                    raise MirrorSyncError(
                        f"existing primary {label} [{spec.root_id}] must be "
                        "mode 0700 and owned by the current uid"
                    )
                _revalidate_absolute_control_object(child)
            except BaseException as error:
                child_error = error
            close_errors = _close_control_bindings_best_effort((child,))
            if child_error is not None:
                _raise_with_secondary_cleanup(
                    child_error,
                    "primary child prebind cleanup failures",
                    close_errors,
                )
            if close_errors:
                raise MirrorSyncError(
                    "primary child prebind cleanup failures: " + "; ".join(close_errors)
                )
        _revalidate_absolute_control_object(parent)
        _revalidate_absolute_control_object(home)
        return PrimaryPrivateControlPrebinding(
            home=home,
            parent=parent,
            tool_record=child_records[PRIVATE_TOOL_ROOT_NAME],
            quarantine_record=child_records[DURABLE_QUARANTINE_ROOT_NAME],
        )
    except BaseException as error:
        close_errors = _close_control_bindings_best_effort((parent, home))
        _raise_with_secondary_cleanup(
            error,
            "primary prebind cleanup failures",
            close_errors,
        )


def _private_control_child_metadata(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MirrorSyncError(f"cannot inspect {label} {name}: {error}") from error
    return _object_identity(metadata), _access_policy(metadata)


def _legacy_child_metadata(
    parent_fd: int,
    name: str,
    root_id: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    return _private_control_child_metadata(
        parent_fd,
        name,
        f"legacy private-control child [{root_id}]",
    )


def _legacy_shared_parent_policy_is_valid(
    access_policy: tuple[int, int, int],
) -> bool:
    mode, uid, _gid = access_policy
    return mode == 0o1777 and uid == 0


def _capture_legacy_private_control_parent_absence(
    spec: PrivateControlRootSpec,
) -> ControlAbsenceBinding:
    parent_path = Path(os.path.abspath(spec.parent_path))
    if (
        spec.allocate
        or not spec.shared_parent
        or spec.account_home is not None
        or parent_path == Path("/")
        or parent_path.name in {"", ".", ".."}
    ):
        raise MirrorSyncError(
            f"private-control legacy root schema is invalid [{spec.root_id}]"
        )
    anchor_path = parent_path.parent
    anchor = _bind_absolute_control_object(
        anchor_path,
        f"legacy private-control absence anchor [{spec.root_id}]",
        require_directory=True,
    )
    try:
        try:
            os.stat(
                parent_path.name,
                dir_fd=anchor.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            raise MirrorSyncError(
                f"cannot verify absent legacy private-control parent "
                f"[{spec.root_id}]: {error}"
            ) from error
        else:
            raise MirrorSyncError(
                f"legacy private-control parent [{spec.root_id}] is not absent"
            )
        _revalidate_absolute_control_object(anchor)
        return ControlAbsenceBinding(
            label=f"legacy private-control parent absence [{spec.root_id}]",
            parent=anchor,
            name=parent_path.name,
        )
    except BaseException as error:
        close_errors = _close_control_bindings_best_effort((anchor,))
        _raise_with_secondary_cleanup(
            error,
            "legacy absence-anchor cleanup failures",
            close_errors,
        )


def _bind_prebound_primary_private_control_children(
    spec: PrivateControlRootSpec,
    prebinding: PrimaryPrivateControlPrebinding,
) -> tuple[ControlObjectBinding | None, ControlObjectBinding | None]:
    parent = prebinding.parent
    if parent is None:
        return None, None
    tool_root: ControlObjectBinding | None = None
    quarantine: ControlObjectBinding | None = None
    try:
        if prebinding.tool_record is not None:
            tool_root = _bind_existing_private_control_directory(
                parent,
                PRIVATE_TOOL_ROOT_NAME,
                f"preflight primary private Git tool root [{spec.root_id}]",
                spec.root_id,
                prebinding.tool_record,
            )
        if prebinding.quarantine_record is not None:
            quarantine = _bind_existing_private_control_directory(
                parent,
                DURABLE_QUARANTINE_ROOT_NAME,
                f"preflight primary durable quarantine root [{spec.root_id}]",
                spec.root_id,
                prebinding.quarantine_record,
            )
        return tool_root, quarantine
    except BaseException as binding_error:
        close_errors = _close_control_bindings_best_effort((quarantine, tool_root))
        if close_errors:
            raise MirrorSyncError(
                f"{binding_error}; secondary primary-child bind cleanup "
                f"failures: {'; '.join(close_errors)}"
            ) from binding_error
        raise


def _validate_prebound_primary_private_control_topology(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    spec: PrivateControlRootSpec,
    prebinding: PrimaryPrivateControlPrebinding,
) -> None:
    parent = prebinding.parent
    if parent is None:
        _revalidate_absolute_control_object(prebinding.home)
        return
    tool_root, quarantine = _bind_prebound_primary_private_control_children(
        spec,
        prebinding,
    )
    validation_error: BaseException | None = None
    try:
        _validate_private_control_root_topology(
            root_id=spec.root_id,
            parent=parent,
            tool_root=tool_root,
            quarantine=quarantine,
            repository_root=root,
            admin=admin,
            common=common,
            home=prebinding.home,
        )
        _revalidate_absolute_control_object(parent)
        _revalidate_absolute_control_object(prebinding.home)
    except BaseException as error:
        validation_error = error
    close_errors = _close_control_bindings_best_effort((quarantine, tool_root))
    if validation_error is not None:
        if close_errors:
            raise MirrorSyncError(
                f"{validation_error}; secondary primary topology cleanup "
                f"failures: {'; '.join(close_errors)}"
            ) from validation_error
        raise validation_error
    if close_errors:
        raise MirrorSyncError(
            "cannot close every primary topology preflight descriptor: "
            + "; ".join(close_errors)
        )


def _preflight_legacy_private_control_roots_once(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    primary_prebinding: PrimaryPrivateControlPrebinding,
    retained_receipts: list[LegacyPrivateControlReceipt] | None = None,
) -> tuple[tuple[str, ...], tuple[LegacyPrivateControlReceipt, ...]]:
    legacy_states: list[str] = []
    receipts = [] if retained_receipts is None else retained_receipts
    primary_spec = _private_control_primary_spec()
    seen_parent_identities = (
        set()
        if primary_prebinding.parent is None
        else {primary_prebinding.parent.identity}
    )
    seen_child_identities = primary_prebinding.child_identities

    def record_receipt(
        spec: PrivateControlRootSpec,
        parent: ControlObjectBinding | None,
        tool_record: tuple[tuple[int, int, int], tuple[int, int, int]] | None,
        quarantine_record: (tuple[tuple[int, int, int], tuple[int, int, int]] | None),
        state: str,
        *,
        absence_binding: ControlAbsenceBinding | None = None,
        parent_binding: ControlObjectBinding | None = None,
        tool_binding: ControlObjectBinding | None = None,
        quarantine_binding: ControlObjectBinding | None = None,
        adoption_plan_digest: str | None = None,
    ) -> None:
        receipts.append(
            LegacyPrivateControlReceipt(
                root_id=spec.root_id,
                parent_path=Path(os.path.abspath(spec.parent_path)),
                parent_identity=None if parent is None else parent.identity,
                parent_access_policy=(None if parent is None else parent.access_policy),
                tool_record=tool_record,
                quarantine_record=quarantine_record,
                state=state,
                absence_anchor_path=(
                    None if absence_binding is None else absence_binding.parent.path
                ),
                absence_name=(
                    None if absence_binding is None else absence_binding.name
                ),
                absence_anchor_identity=(
                    None if absence_binding is None else absence_binding.parent.identity
                ),
                absence_anchor_access_policy=(
                    None
                    if absence_binding is None
                    else absence_binding.parent.access_policy
                ),
                absence_binding=absence_binding,
                parent_binding=parent_binding,
                tool_binding=tool_binding,
                quarantine_binding=quarantine_binding,
                adoption_plan_digest=adoption_plan_digest,
            )
        )

    for spec in PRIVATE_CONTROL_ROOT_SPECS:
        if spec.allocate:
            continue
        if not spec.shared_parent or spec.account_home is not None:
            raise MirrorSyncError(
                f"private-control legacy root schema is invalid [{spec.root_id}]"
            )
        retain_same_uid_bindings = False
        try:
            parent = _bind_absolute_control_object(
                spec.parent_path,
                f"legacy shared private-control parent [{spec.root_id}]",
                require_directory=True,
            )
        except MirrorSyncError as error:
            try:
                absence_binding = _capture_legacy_private_control_parent_absence(spec)
            except MirrorSyncError as inspection_error:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "cannot classify unavailable legacy parent with an "
                    f"anchored absence receipt: {inspection_error}; "
                    f"initial bind failure: {error}"
                ) from inspection_error
            legacy_states.append("absent")
            record_receipt(
                spec,
                None,
                None,
                None,
                "absent",
                absence_binding=absence_binding,
            )
            continue
        try:
            if not _legacy_shared_parent_policy_is_valid(parent.access_policy):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "legacy shared parent must be root-owned mode 1777"
                )
            if parent.identity in seen_child_identities:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "legacy parent aliases an earlier fixed child role"
                )
            if parent.identity in seen_parent_identities:
                _revalidate_absolute_control_object(parent)
                legacy_states.append("duplicate")
                record_receipt(
                    spec,
                    parent,
                    None,
                    None,
                    "duplicate-parent",
                )
                continue
            first_tool = _legacy_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                spec.root_id,
            )
            first_quarantine = _legacy_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                spec.root_id,
            )
            observed = tuple(
                item for item in (first_tool, first_quarantine) if item is not None
            )
            observed_identities = {item[0] for item in observed}
            if len(observed_identities) != len(observed):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "legacy fixed child roles alias each other"
                )
            if parent.identity in observed_identities:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "legacy fixed child aliases its own parent"
                )
            if observed_identities & (seen_parent_identities | seen_child_identities):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "distinct legacy parent aliases an earlier control object"
                )
            ownership_state = _private_control_legacy_ownership_state(observed)
            if ownership_state == "foreign-unrelated":
                second_tool = _legacy_child_metadata(
                    parent.fd,
                    PRIVATE_TOOL_ROOT_NAME,
                    spec.root_id,
                )
                second_quarantine = _legacy_child_metadata(
                    parent.fd,
                    DURABLE_QUARANTINE_ROOT_NAME,
                    spec.root_id,
                )
                if (second_tool, second_quarantine) != (
                    first_tool,
                    first_quarantine,
                ):
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                        "foreign-unrelated legacy metadata changed during audit"
                    )
                _revalidate_absolute_control_object(parent)
                seen_parent_identities.add(parent.identity)
                seen_child_identities.update(observed_identities)
                legacy_states.append(ownership_state)
                record_receipt(
                    spec,
                    parent,
                    first_tool,
                    first_quarantine,
                    ownership_state,
                )
                continue
            if ownership_state == "absent":
                second_tool = _legacy_child_metadata(
                    parent.fd,
                    PRIVATE_TOOL_ROOT_NAME,
                    spec.root_id,
                )
                second_quarantine = _legacy_child_metadata(
                    parent.fd,
                    DURABLE_QUARANTINE_ROOT_NAME,
                    spec.root_id,
                )
                if (second_tool, second_quarantine) != (
                    first_tool,
                    first_quarantine,
                ):
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                        "absent legacy metadata changed during audit"
                    )
                _revalidate_absolute_control_object(parent)
                seen_parent_identities.add(parent.identity)
                legacy_states.append(ownership_state)
                record_receipt(
                    spec,
                    parent,
                    first_tool,
                    first_quarantine,
                    ownership_state,
                )
                continue
            if ownership_state == "inconclusive":
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "legacy children have mixed ownership"
                )
            if first_tool is None:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_LEGACY_PENDING} [{spec.root_id}]: "
                    "same-uid quarantine exists without its tool root"
                )
            if first_tool[0][2] != stat.S_IFDIR or first_tool[1][0] != 0o700:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "same-uid legacy tool root must be mode 0700 directory"
                )
            if first_quarantine is not None and (
                first_quarantine[0][2] != stat.S_IFDIR
                or first_quarantine[1][0] != 0o700
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "same-uid legacy quarantine must be mode 0700 directory"
                )
            tool_root = _bind_relative_control_directory(
                parent,
                PRIVATE_TOOL_ROOT_NAME,
                f"legacy private-control tool root [{spec.root_id}]",
            )
            if tool_root is None:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "same-uid legacy tool root disappeared"
                )
            quarantine: ControlObjectBinding | None = None
            tool_lease_attempted = False
            quarantine_lease_attempted = False
            try:
                if (
                    tool_root.identity,
                    tool_root.access_policy,
                ) != first_tool:
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                        "legacy tool root changed before recovery binding"
                    )
                tool_root.root_id = spec.root_id
                try:
                    tool_lease_attempted = True
                    fcntl.flock(tool_root.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                        f"legacy tool root is busy or unleaseable: {error}"
                    ) from error
                if first_quarantine is not None:
                    quarantine = _bind_relative_control_directory(
                        parent,
                        DURABLE_QUARANTINE_ROOT_NAME,
                        f"legacy durable quarantine [{spec.root_id}]",
                    )
                    if quarantine is None:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                            f"[{spec.root_id}]: legacy quarantine disappeared"
                        )
                    if (
                        quarantine.identity,
                        quarantine.access_policy,
                    ) != first_quarantine:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                            f"[{spec.root_id}]: legacy quarantine changed "
                            "before recovery binding"
                        )
                    quarantine.root_id = spec.root_id
                    try:
                        quarantine_lease_attempted = True
                        fcntl.flock(
                            quarantine.fd,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    except OSError as error:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                            f"[{spec.root_id}]: legacy quarantine is busy "
                            f"or unleaseable: {error}"
                        ) from error
                else:
                    current_quarantine = _legacy_child_metadata(
                        parent.fd,
                        DURABLE_QUARANTINE_ROOT_NAME,
                        spec.root_id,
                    )
                    if current_quarantine is not None:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                            f"[{spec.root_id}]: legacy quarantine appeared "
                            "before recovery"
                        )
                    initial_tool_names = _bounded_sorted_directory_names(
                        tool_root.fd,
                        maximum_entries=MAX_TOOL_ROOT_ENTRIES,
                        label=(
                            f"initial legacy private-control tool root [{spec.root_id}]"
                        ),
                        limit_error=(
                            f"legacy private-control tool root [{spec.root_id}] "
                            f"exceeds {MAX_TOOL_ROOT_ENTRIES} entries"
                        ),
                        operation=root.operation,
                    )
                    if initial_tool_names:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_LEGACY_PENDING} "
                            f"[{spec.root_id}]: legacy recovery evidence remains "
                            "but its durable quarantine was initially absent"
                        )
                primary_tool, primary_quarantine = (
                    _bind_prebound_primary_private_control_children(
                        primary_spec,
                        primary_prebinding,
                    )
                )
                topology_error: BaseException | None = None
                try:
                    primary_protected: list[tuple[str, int]] = [
                        ("primary account home", primary_prebinding.home.fd),
                    ]
                    if primary_prebinding.parent is not None:
                        primary_protected.append(
                            (
                                "primary private-control allocation root",
                                primary_prebinding.parent.fd,
                            )
                        )
                    if primary_tool is not None:
                        primary_protected.append(
                            ("primary private Git tool root", primary_tool.fd)
                        )
                    if primary_quarantine is not None:
                        primary_protected.append(
                            (
                                "primary durable quarantine root",
                                primary_quarantine.fd,
                            )
                        )
                    _validate_private_control_root_topology(
                        root_id=spec.root_id,
                        parent=parent,
                        tool_root=tool_root,
                        quarantine=quarantine,
                        repository_root=root,
                        admin=admin,
                        common=common,
                        additional_protected=tuple(primary_protected),
                    )
                    if primary_prebinding.parent is not None:
                        _revalidate_absolute_control_object(primary_prebinding.parent)
                    _revalidate_absolute_control_object(primary_prebinding.home)
                except BaseException as error:
                    topology_error = error
                primary_close_errors = _close_control_bindings_best_effort(
                    (primary_quarantine, primary_tool)
                )
                if topology_error is not None:
                    if primary_close_errors:
                        raise MirrorSyncError(
                            f"{topology_error}; secondary protected-primary "
                            "topology cleanup failures: "
                            + "; ".join(primary_close_errors)
                        ) from topology_error
                    raise topology_error
                if primary_close_errors:
                    raise MirrorSyncError(
                        "cannot close every protected primary topology "
                        "descriptor: " + "; ".join(primary_close_errors)
                    )
                if primary_prebinding.parent is not None:
                    try:
                        os.stat(
                            PRIVATE_CONTROL_RECOVERY_MARKER_NAME,
                            dir_fd=primary_prebinding.parent.fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        try:
                            os.stat(
                                PRIVATE_CONTROL_RECOVERY_RECEIPT_NAME,
                                dir_fd=primary_prebinding.parent.fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        except OSError as error:
                            raise MirrorSyncError(
                                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                                f"[{spec.root_id}]: cannot inspect retained-in-place "
                                f"receipt: {error}"
                            ) from error
                        else:
                            raise MirrorSyncError(
                                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                                f"[{spec.root_id}]: retained-in-place receipt exists "
                                "without its cutover marker"
                            )
                    except OSError as error:
                        raise MirrorSyncError(
                            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                            f"[{spec.root_id}]: cannot inspect retained-in-place "
                            f"cutover marker: {error}"
                        ) from error
                    else:
                        if quarantine is None:
                            raise MirrorSyncError(
                                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                                f"[{spec.root_id}]: retained-in-place cutover "
                                "requires its exact quarantine root"
                            )
                        recovery_parent = _PrivateControlRecoveryBinding(
                            label=parent.label,
                            path=parent.path,
                            fd=parent.fd,
                            identity=parent.identity,
                            access=parent.access_policy,
                        )
                        recovery_tool = _PrivateControlRecoveryBinding(
                            label=tool_root.label,
                            path=tool_root.path,
                            fd=tool_root.fd,
                            identity=tool_root.identity,
                            access=tool_root.access_policy,
                        )
                        recovery_quarantine = _PrivateControlRecoveryBinding(
                            label=quarantine.label,
                            path=quarantine.path,
                            fd=quarantine.fd,
                            identity=quarantine.identity,
                            access=quarantine.access_policy,
                        )
                        recovery_primary = _PrivateControlRecoveryBinding(
                            label=primary_prebinding.parent.label,
                            path=primary_prebinding.parent.path,
                            fd=primary_prebinding.parent.fd,
                            identity=primary_prebinding.parent.identity,
                            access=primary_prebinding.parent.access_policy,
                        )
                        verification = _pc_recovery_verify_adoption_locked(
                            recovery_primary,
                            recovery_parent,
                            recovery_tool,
                            recovery_quarantine,
                            operation=root.operation,
                        )
                        final_tool = _legacy_child_metadata(
                            parent.fd,
                            PRIVATE_TOOL_ROOT_NAME,
                            spec.root_id,
                        )
                        final_quarantine = _legacy_child_metadata(
                            parent.fd,
                            DURABLE_QUARANTINE_ROOT_NAME,
                            spec.root_id,
                        )
                        if (final_tool, final_quarantine) != (
                            first_tool,
                            first_quarantine,
                        ):
                            raise MirrorSyncError(
                                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                                f"[{spec.root_id}]: retained-in-place fixed roots "
                                "changed during verification"
                            )
                        _revalidate_absolute_control_object(parent)
                        seen_parent_identities.add(parent.identity)
                        seen_child_identities.update(observed_identities)
                        legacy_states.append("adopted-retained-in-place")
                        record_receipt(
                            spec,
                            parent,
                            first_tool,
                            first_quarantine,
                            "adopted-retained-in-place",
                            parent_binding=parent,
                            tool_binding=tool_root,
                            quarantine_binding=quarantine,
                            adoption_plan_digest=str(
                                verification["plan"]["plan_digest"]
                            ),
                        )
                        retain_same_uid_bindings = True
                        continue
                if quarantine is not None:
                    _bounded_sorted_directory_names(
                        tool_root.fd,
                        maximum_entries=MAX_TOOL_ROOT_ENTRIES,
                        label=(
                            f"initial legacy private-control tool root [{spec.root_id}]"
                        ),
                        limit_error=(
                            f"legacy private-control tool root [{spec.root_id}] "
                            f"exceeds {MAX_TOOL_ROOT_ENTRIES} entries"
                        ),
                        operation=root.operation,
                    )
                    _recover_stale_private_snapshots(
                        root,
                        tool_root,
                        quarantine=quarantine,
                        quarantine_locked=True,
                    )
                remaining = _bounded_sorted_directory_names(
                    tool_root.fd,
                    maximum_entries=MAX_TOOL_ROOT_ENTRIES,
                    label=f"legacy private-control tool root [{spec.root_id}]",
                    limit_error=(
                        f"legacy private-control tool root [{spec.root_id}] "
                        f"exceeds {MAX_TOOL_ROOT_ENTRIES} entries"
                    ),
                    operation=root.operation,
                )
                quarantine_names: tuple[str, ...] = ()
                if quarantine is not None:
                    quarantine_names = _bounded_sorted_directory_names(
                        quarantine.fd,
                        maximum_entries=MAX_DURABLE_QUARANTINE_ENTRIES,
                        label=f"legacy durable quarantine [{spec.root_id}]",
                        limit_error=(
                            f"legacy durable quarantine [{spec.root_id}] "
                            f"exceeds {MAX_DURABLE_QUARANTINE_ENTRIES} entries"
                        ),
                        operation=root.operation,
                    )
                    _revalidate_control_object(root, quarantine)
                if remaining or quarantine_names:
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_LEGACY_PENDING} [{spec.root_id}]: "
                        "same-uid legacy recovery evidence remains in its original root"
                    )
                _revalidate_control_object(root, tool_root)
                final_tool = _legacy_child_metadata(
                    parent.fd,
                    PRIVATE_TOOL_ROOT_NAME,
                    spec.root_id,
                )
                final_quarantine = _legacy_child_metadata(
                    parent.fd,
                    DURABLE_QUARANTINE_ROOT_NAME,
                    spec.root_id,
                )
                if (final_tool, final_quarantine) != (
                    first_tool,
                    first_quarantine,
                ):
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                        "legacy fixed-root metadata changed during recovery"
                    )
                _revalidate_absolute_control_object(parent)
                seen_parent_identities.add(parent.identity)
                seen_child_identities.update(observed_identities)
                legacy_states.append("same-uid-empty")
                record_receipt(
                    spec,
                    parent,
                    first_tool,
                    first_quarantine,
                    "same-uid-empty",
                    parent_binding=parent,
                    tool_binding=tool_root,
                    quarantine_binding=quarantine,
                )
                retain_same_uid_bindings = True
            finally:
                if not retain_same_uid_bindings:
                    active_error = sys.exc_info()[1]
                    cleanup_errors: list[str] = []
                    for binding, lease_attempted in (
                        (quarantine, quarantine_lease_attempted),
                        (tool_root, tool_lease_attempted),
                    ):
                        if binding is None or binding.fd < 0:
                            continue
                        if lease_attempted:
                            try:
                                fcntl.flock(binding.fd, fcntl.LOCK_UN)
                            except OSError as error:
                                cleanup_errors.append(
                                    f"cannot release {binding.label}: {error}"
                                )
                        try:
                            os.close(binding.fd)
                        except OSError as error:
                            cleanup_errors.append(
                                f"cannot close {binding.label}: {error}"
                            )
                        finally:
                            binding.fd = -1
                    if cleanup_errors:
                        detail = "; ".join(cleanup_errors)
                        if active_error is not None:
                            raise MirrorSyncError(
                                f"{active_error}; secondary same-uid legacy "
                                f"preflight cleanup failures: {detail}"
                            ) from active_error
                        raise MirrorSyncError(
                            "same-uid legacy preflight cleanup failures: " + detail
                        )
        finally:
            if not retain_same_uid_bindings:
                active_error = sys.exc_info()[1]
                close_errors = _close_control_bindings_best_effort((parent,))
                if close_errors:
                    detail = "; ".join(close_errors)
                    if active_error is not None:
                        raise MirrorSyncError(
                            f"{active_error}; secondary legacy parent cleanup "
                            f"failures: {detail}"
                        ) from active_error
                    raise MirrorSyncError("legacy parent cleanup failures: " + detail)
    return tuple(legacy_states), tuple(receipts)


def _release_legacy_private_control_receipts(
    receipts: tuple[LegacyPrivateControlReceipt, ...]
    | list[LegacyPrivateControlReceipt],
) -> None:
    errors: list[str] = []
    closed_fds: set[int] = set()

    def unlock_and_close(
        binding: ControlObjectBinding | None,
        *,
        unlock: bool,
    ) -> None:
        if binding is None or binding.fd < 0:
            return
        fd = binding.fd
        if fd in closed_fds:
            binding.fd = -1
            return
        closed_fds.add(fd)
        if unlock:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as error:
                errors.append(f"cannot release {binding.label}: {error}")
        try:
            os.close(fd)
        except OSError as error:
            errors.append(f"cannot close {binding.label}: {error}")
        finally:
            binding.fd = -1

    for receipt in reversed(receipts):
        unlock_and_close(receipt.quarantine_binding, unlock=True)
        unlock_and_close(receipt.tool_binding, unlock=True)
        unlock_and_close(receipt.parent_binding, unlock=False)
        if receipt.absence_binding is not None:
            unlock_and_close(receipt.absence_binding.parent, unlock=False)
    if errors:
        raise MirrorSyncError(
            "cannot release every retained legacy private-control lease: "
            + "; ".join(errors)
        )


def _release_primary_context_legacy_fences(
    context: PrimaryPrivateControlContext,
) -> None:
    receipts = context.legacy_receipts
    context.legacy_receipts = ()
    if not receipts:
        return
    _release_legacy_private_control_receipts(receipts)


def _audit_legacy_private_control_registry_coverage(
    root: BoundRoot,
    primary_prebinding: PrimaryPrivateControlPrebinding,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    states: list[str] = []
    details: list[str] = []
    seen_parent_identities = (
        set()
        if primary_prebinding.parent is None
        else {primary_prebinding.parent.identity}
    )
    seen_child_identities = primary_prebinding.child_identities
    for spec in PRIVATE_CONTROL_ROOT_SPECS:
        if spec.allocate:
            continue
        state_start = len(states)
        parent: ControlObjectBinding | None = None
        tool: ControlObjectBinding | None = None
        quarantine: ControlObjectBinding | None = None
        tool_lease_attempted = False
        quarantine_lease_attempted = False
        try:
            if not spec.shared_parent or spec.account_home is not None:
                raise MirrorSyncError("legacy root schema is invalid")
            try:
                parent = _bind_absolute_control_object(
                    spec.parent_path,
                    f"coverage legacy shared parent [{spec.root_id}]",
                    require_directory=True,
                )
            except MirrorSyncError as error:
                try:
                    absence = _capture_legacy_private_control_parent_absence(spec)
                except MirrorSyncError as inspection_error:
                    raise MirrorSyncError(
                        "cannot classify unavailable parent with an anchored "
                        f"absence receipt: {inspection_error}"
                    ) from error
                else:
                    parent = absence.parent
                    states.append("absent")
                    details.append(f"{spec.root_id}=absent")
                    continue
            if not _legacy_shared_parent_policy_is_valid(parent.access_policy):
                raise MirrorSyncError("shared parent policy is invalid")
            if parent.identity in seen_child_identities:
                raise MirrorSyncError("parent aliases an earlier fixed child")
            if parent.identity in seen_parent_identities:
                _revalidate_absolute_control_object(parent)
                states.append("duplicate")
                details.append(f"{spec.root_id}=duplicate")
                continue
            first_tool = _legacy_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                spec.root_id,
            )
            first_quarantine = _legacy_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                spec.root_id,
            )
            observed = tuple(
                item for item in (first_tool, first_quarantine) if item is not None
            )
            observed_identities = {item[0] for item in observed}
            if len(observed_identities) != len(observed):
                raise MirrorSyncError("fixed child roles alias each other")
            if parent.identity in observed_identities:
                raise MirrorSyncError("fixed child aliases its own parent")
            if observed_identities & (seen_parent_identities | seen_child_identities):
                raise MirrorSyncError(
                    "distinct parent aliases an earlier control object"
                )
            ownership_state = _private_control_legacy_ownership_state(observed)
            if ownership_state == "inconclusive":
                raise MirrorSyncError("fixed children have mixed ownership")
            if ownership_state in {"absent", "foreign-unrelated"}:
                state = ownership_state
            else:
                if first_tool is None:
                    state = PRIVATE_CONTROL_REASON_LEGACY_PENDING
                elif first_tool[0][2] != stat.S_IFDIR or first_tool[1][0] != 0o700:
                    raise MirrorSyncError("tool root type or policy is invalid")
                elif first_quarantine is not None and (
                    first_quarantine[0][2] != stat.S_IFDIR
                    or first_quarantine[1][0] != 0o700
                ):
                    raise MirrorSyncError("quarantine type or policy is invalid")
                else:
                    tool = _bind_existing_private_control_directory(
                        parent,
                        PRIVATE_TOOL_ROOT_NAME,
                        f"coverage legacy tool root [{spec.root_id}]",
                        spec.root_id,
                        first_tool,
                    )
                    tool_lease_attempted = True
                    fcntl.flock(tool.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    tool_names = _bounded_sorted_directory_names(
                        tool.fd,
                        maximum_entries=MAX_TOOL_ROOT_ENTRIES,
                        label=f"coverage legacy tool root [{spec.root_id}]",
                        limit_error="legacy tool root exceeds its coverage limit",
                        operation=root.operation,
                    )
                    quarantine_names: tuple[str, ...] = ()
                    if first_quarantine is not None:
                        quarantine = _bind_existing_private_control_directory(
                            parent,
                            DURABLE_QUARANTINE_ROOT_NAME,
                            f"coverage legacy quarantine [{spec.root_id}]",
                            spec.root_id,
                            first_quarantine,
                        )
                        quarantine_lease_attempted = True
                        fcntl.flock(
                            quarantine.fd,
                            fcntl.LOCK_SH | fcntl.LOCK_NB,
                        )
                        quarantine_names = _bounded_sorted_directory_names(
                            quarantine.fd,
                            maximum_entries=MAX_DURABLE_QUARANTINE_ENTRIES,
                            label=f"coverage legacy quarantine [{spec.root_id}]",
                            limit_error=(
                                "legacy quarantine exceeds its coverage limit"
                            ),
                            operation=root.operation,
                        )
                    state = (
                        PRIVATE_CONTROL_REASON_LEGACY_PENDING
                        if tool_names or quarantine_names
                        else "same-uid-empty"
                    )
            second_tool = _legacy_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                spec.root_id,
            )
            second_quarantine = _legacy_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                spec.root_id,
            )
            if (second_tool, second_quarantine) != (
                first_tool,
                first_quarantine,
            ):
                raise MirrorSyncError("fixed metadata changed during coverage audit")
            _revalidate_absolute_control_object(parent)
            seen_parent_identities.add(parent.identity)
            seen_child_identities.update(observed_identities)
            states.append(state)
            details.append(f"{spec.root_id}={state}")
        except (MirrorSyncError, OSError) as error:
            states.append(PRIVATE_CONTROL_REASON_INCONCLUSIVE)
            details.append(f"{spec.root_id}=inconclusive({error})")
        finally:
            cleanup_errors: list[str] = []
            for binding, lease_attempted in (
                (quarantine, quarantine_lease_attempted),
                (tool, tool_lease_attempted),
                (parent, False),
            ):
                if binding is not None and binding.fd >= 0:
                    if lease_attempted:
                        try:
                            fcntl.flock(binding.fd, fcntl.LOCK_UN)
                        except OSError as error:
                            cleanup_errors.append(
                                f"cannot release {binding.label}: {error}"
                            )
                    try:
                        os.close(binding.fd)
                    except OSError as error:
                        cleanup_errors.append(f"cannot close {binding.label}: {error}")
                    finally:
                        binding.fd = -1
            if cleanup_errors:
                cleanup_detail = "; ".join(cleanup_errors)
                if len(states) == state_start:
                    states.append(PRIVATE_CONTROL_REASON_INCONCLUSIVE)
                    details.append(f"{spec.root_id}=inconclusive({cleanup_detail})")
                else:
                    prior_detail = details[-1]
                    states[-1] = PRIVATE_CONTROL_REASON_INCONCLUSIVE
                    details[-1] = (
                        f"{spec.root_id}=inconclusive({prior_detail}; "
                        f"cleanup failures: {cleanup_detail})"
                    )
    return tuple(states), tuple(details)


def _preflight_legacy_private_control_roots(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    primary_prebinding: PrimaryPrivateControlPrebinding,
) -> tuple[tuple[str, ...], tuple[LegacyPrivateControlReceipt, ...]]:
    retained_receipts: list[LegacyPrivateControlReceipt] = []
    try:
        return _preflight_legacy_private_control_roots_once(
            root,
            admin,
            common,
            primary_prebinding,
            retained_receipts,
        )
    except BaseException as caught_error:
        first_error: BaseException = caught_error
        try:
            _release_legacy_private_control_receipts(retained_receipts)
        except MirrorSyncError as release_error:
            first_error = MirrorSyncError(
                f"{first_error}; secondary legacy lease release failure: "
                f"{release_error}"
            )
        if not isinstance(caught_error, (MirrorSyncError, OSError)):
            if first_error is not caught_error:
                raise first_error from caught_error
            raise
        coverage_states, coverage_details = (
            _audit_legacy_private_control_registry_coverage(
                root,
                primary_prebinding,
            )
        )
        reason = _private_control_preflight_failure_reason(
            first_error,
            coverage_states,
        )
        raise MirrorSyncError(
            f"{reason}: complete legacy registry coverage "
            f"[{'; '.join(coverage_details)}]; first failure: {first_error}"
        ) from first_error


def _terminal_legacy_directory_names(
    directory_fd: int,
    *,
    maximum_entries: int,
    label: str,
    limit_error: str,
    operation: OperationBudget | None,
) -> tuple[str, ...]:
    _operation_checkpoint(operation, f"terminally scanning {label}")
    if operation is not None:
        if operation.remaining_bytes <= 0:
            raise MirrorSyncError(
                f"mirror operation exceeds the {MAX_OPERATION_BYTES}-byte "
                f"aggregate budget before terminally scanning {label}"
            )
        if operation.remaining_entries <= 0:
            raise MirrorSyncError(
                f"mirror operation exceeds the {MAX_OPERATION_ENTRIES}-entry "
                f"aggregate budget before terminally scanning {label}"
            )
    names = _bounded_sorted_directory_names(
        directory_fd,
        maximum_entries=maximum_entries,
        label=label,
        limit_error=limit_error,
        operation=operation,
    )
    _consume_operation_budget(
        operation,
        byte_count=sum(len(os.fsencode(name)) for name in names),
        label=f"terminally scanning {label}",
    )
    return names


def _revalidate_legacy_private_control_receipts_once(
    receipts: tuple[LegacyPrivateControlReceipt, ...],
    *,
    operation: OperationBudget | None,
) -> None:
    for receipt in receipts:
        if receipt.tool_binding is not None:
            parent = receipt.parent_binding
            tool = receipt.tool_binding
            quarantine = receipt.quarantine_binding
            if parent is None or min(parent.fd, tool.fd) < 0:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "retained legacy publication fence is unavailable"
                )
            if (
                parent.identity != receipt.parent_identity
                or parent.access_policy != receipt.parent_access_policy
                or not _legacy_shared_parent_policy_is_valid(parent.access_policy)
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "retained legacy parent changed before primary allocation"
                )
            _revalidate_absolute_control_object(parent)
            _revalidate_absolute_control_object(tool)
            if quarantine is not None:
                _revalidate_absolute_control_object(quarantine)
            tool_record = _legacy_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                receipt.root_id,
            )
            quarantine_record = _legacy_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                receipt.root_id,
            )
            if (
                tool_record != receipt.tool_record
                or quarantine_record != receipt.quarantine_record
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "retained legacy fixed-root metadata changed before "
                    "primary allocation"
                )
            if receipt.state == "adopted-retained-in-place":
                if quarantine is None or receipt.adoption_plan_digest is None:
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                        f"[{receipt.root_id}]: retained-in-place publication "
                        "fence is incomplete"
                    )
                primary_spec = _private_control_primary_spec()
                primary_parent = _pc_recovery_existing_primary_parent(primary_spec)
                if primary_parent is None:
                    raise MirrorSyncError(
                        f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} "
                        f"[{receipt.root_id}]: retained-in-place primary receipt "
                        "parent disappeared"
                    )
                try:
                    _pc_recovery_verify_adoption_locked(
                        primary_parent,
                        _PrivateControlRecoveryBinding(
                            label=parent.label,
                            path=parent.path,
                            fd=parent.fd,
                            identity=parent.identity,
                            access=parent.access_policy,
                        ),
                        _PrivateControlRecoveryBinding(
                            label=tool.label,
                            path=tool.path,
                            fd=tool.fd,
                            identity=tool.identity,
                            access=tool.access_policy,
                        ),
                        _PrivateControlRecoveryBinding(
                            label=quarantine.label,
                            path=quarantine.path,
                            fd=quarantine.fd,
                            identity=quarantine.identity,
                            access=quarantine.access_policy,
                        ),
                        expected_plan_digest=receipt.adoption_plan_digest,
                        operation=operation,
                    )
                finally:
                    _pc_recovery_close_bindings((primary_parent,))
                _revalidate_absolute_control_object(parent)
                continue
            tool_names = _terminal_legacy_directory_names(
                tool.fd,
                maximum_entries=MAX_TOOL_ROOT_ENTRIES,
                label=f"terminal legacy tool root [{receipt.root_id}]",
                limit_error="legacy tool root exceeds its terminal coverage limit",
                operation=operation,
            )
            quarantine_names: tuple[str, ...] = ()
            if quarantine is not None:
                quarantine_names = _terminal_legacy_directory_names(
                    quarantine.fd,
                    maximum_entries=MAX_DURABLE_QUARANTINE_ENTRIES,
                    label=f"terminal legacy quarantine [{receipt.root_id}]",
                    limit_error=(
                        "legacy quarantine exceeds its terminal coverage limit"
                    ),
                    operation=operation,
                )
            if tool_names or quarantine_names:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_LEGACY_PENDING} [{receipt.root_id}]: "
                    "legacy evidence appeared while the cutover fence was held"
                )
            _revalidate_absolute_control_object(parent)
            continue
        absence_fields = (
            receipt.absence_anchor_path,
            receipt.absence_name,
            receipt.absence_anchor_identity,
            receipt.absence_anchor_access_policy,
        )
        if receipt.parent_identity is None:
            absence = receipt.absence_binding
            if (
                receipt.state != "absent"
                or receipt.parent_access_policy is not None
                or receipt.tool_record is not None
                or receipt.quarantine_record is not None
                or any(field is None for field in absence_fields)
                or absence is None
                or absence.parent.fd < 0
                or absence.parent.path != receipt.absence_anchor_path
                or absence.name != receipt.absence_name
                or absence.parent.identity != receipt.absence_anchor_identity
                or absence.parent.access_policy != receipt.absence_anchor_access_policy
                or receipt.parent_path
                != receipt.absence_anchor_path / receipt.absence_name
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "absent legacy parent receipt is incomplete"
                )
            _revalidate_absolute_control_object(absence.parent)
            try:
                os.stat(
                    absence.name,
                    dir_fd=absence.parent.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except OSError as error:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    f"cannot revalidate absent legacy parent: {error}"
                ) from error
            else:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "legacy parent appeared before primary allocation"
                )
            _revalidate_absolute_control_object(absence.parent)
            continue
        if any(field is not None for field in absence_fields) or (
            receipt.absence_binding is not None
        ):
            raise MirrorSyncError(
                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                "present legacy parent receipt has absence-anchor state"
            )
        parent = _bind_absolute_control_object(
            receipt.parent_path,
            f"terminal legacy shared private-control parent [{receipt.root_id}]",
            require_directory=True,
        )
        try:
            if (
                parent.identity != receipt.parent_identity
                or parent.access_policy != receipt.parent_access_policy
                or not _legacy_shared_parent_policy_is_valid(parent.access_policy)
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "legacy parent changed before primary allocation"
                )
            if receipt.state == "duplicate-parent":
                _revalidate_absolute_control_object(parent)
                continue
            tool_record = _legacy_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                receipt.root_id,
            )
            quarantine_record = _legacy_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                receipt.root_id,
            )
            if (
                tool_record != receipt.tool_record
                or quarantine_record != receipt.quarantine_record
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{receipt.root_id}]: "
                    "legacy fixed-root metadata changed before primary allocation"
                )
            _revalidate_absolute_control_object(parent)
        finally:
            os.close(parent.fd)


def _revalidate_legacy_private_control_receipts(
    receipts: tuple[LegacyPrivateControlReceipt, ...],
    *,
    operation: OperationBudget | None,
) -> None:
    errors: list[str] = []
    for receipt in receipts:
        try:
            _revalidate_legacy_private_control_receipts_once(
                (receipt,),
                operation=operation,
            )
        except (MirrorSyncError, OSError) as error:
            errors.append(f"{receipt.root_id}={error}")
    if errors:
        raise MirrorSyncError(
            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE}: terminal legacy registry "
            f"revalidation covered every root [{'; '.join(errors)}]"
        )


def _create_private_control_directory_noreplace(
    parent: ControlObjectBinding,
    name: str,
    label: str,
    root_id: str,
) -> ControlObjectBinding:
    if parent.path is None:
        raise MirrorSyncError(f"{label} parent has no absolute path")
    for _attempt in range(32):
        temporary_name = (
            f".private-control-create-{os.getpid()}-{secrets.token_hex(16)}"
        )
        temporary_fd = -1
        renamed = False
        temporary_identity: tuple[int, int, int] | None = None
        try:
            try:
                os.mkdir(temporary_name, 0o700, dir_fd=parent.fd)
            except FileExistsError:
                continue
            temporary_fd = os.open(
                temporary_name,
                _DIRECTORY_FLAGS,
                dir_fd=parent.fd,
            )
            os.fchmod(temporary_fd, 0o700)
            path_metadata = os.stat(
                temporary_name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(temporary_fd)
            temporary_identity = _object_identity(descriptor_metadata)
            if _object_identity(path_metadata) != temporary_identity or _access_policy(
                path_metadata
            ) != _access_policy(descriptor_metadata):
                raise MirrorSyncError(f"{label} temporary directory was replaced")
            try:
                os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot confirm {label} allocation absence: {error}"
                ) from error
            else:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]: "
                    f"{label} appeared before exclusive allocation"
                )
            try:
                _rename_directory_entry_noreplace(
                    parent.fd,
                    temporary_name,
                    name,
                )
                renamed = True
                os.fsync(parent.fd)
            except OSError as error:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]: "
                    f"cannot publish {label} exclusively: {error}"
                ) from error
            final_metadata = os.stat(
                name,
                dir_fd=parent.fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(temporary_fd)
            if (
                _object_identity(final_metadata) != temporary_identity
                or _object_identity(descriptor_metadata) != temporary_identity
                or _access_policy(final_metadata) != _access_policy(descriptor_metadata)
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]: "
                    f"{label} changed during exclusive allocation"
                )
            if (
                stat.S_IMODE(descriptor_metadata.st_mode) != 0o700
                or descriptor_metadata.st_uid != os.geteuid()
            ):
                raise MirrorSyncError(
                    f"{label} must be mode 0700 and owned by the current uid"
                )
            return ControlObjectBinding(
                label=label,
                path=parent.path / name,
                relative_path=None,
                fd=temporary_fd,
                identity=temporary_identity,
                access_policy=_access_policy(descriptor_metadata),
                content_digest=None,
                root_id=root_id,
            )
        except BaseException as error:
            cleanup_errors: list[str] = []
            if temporary_fd >= 0:
                try:
                    cleanup_name = name if renamed else temporary_name
                    cleanup_metadata = os.stat(
                        cleanup_name,
                        dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                    if (
                        temporary_identity is not None
                        and _object_identity(cleanup_metadata) == temporary_identity
                    ):
                        # Let the kernel perform the bounded emptiness check.
                        # ENOTEMPTY preserves attacker-added evidence without
                        # materializing an unbounded directory listing.
                        os.rmdir(cleanup_name, dir_fd=parent.fd)
                        os.fsync(parent.fd)
                except (OSError, MirrorSyncError) as cleanup_error:
                    cleanup_errors.append(
                        f"cannot clean temporary {label}: {cleanup_error}"
                    )
                cleanup_errors.extend(
                    _close_raw_descriptors_best_effort(
                        ((f"temporary {label}", temporary_fd),)
                    )
                )
                temporary_fd = -1
            _raise_with_secondary_cleanup(
                error,
                f"temporary {label} cleanup failures",
                cleanup_errors,
            )
    raise MirrorSyncError(f"cannot allocate a unique temporary {label}")


def _bind_existing_private_control_directory(
    parent: ControlObjectBinding,
    name: str,
    label: str,
    root_id: str,
    expected_record: tuple[tuple[int, int, int], tuple[int, int, int]],
) -> ControlObjectBinding:
    binding = _bind_relative_control_directory(parent, name, label)
    if binding is None:
        raise MirrorSyncError(
            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]: "
            f"{label} disappeared before final binding"
        )
    observed_record = (binding.identity, binding.access_policy)
    if observed_record != expected_record:
        error = MirrorSyncError(
            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{root_id}]: "
            f"{label} changed before final binding"
        )
        close_errors = _close_control_bindings_best_effort((binding,))
        _raise_with_secondary_cleanup(
            error,
            f"{label} bind cleanup failures",
            close_errors,
        )
    if binding.access_policy[0] != 0o700 or binding.access_policy[1] != os.geteuid():
        error = MirrorSyncError(
            f"{label} must be mode 0700 and owned by the current uid"
        )
        close_errors = _close_control_bindings_best_effort((binding,))
        _raise_with_secondary_cleanup(
            error,
            f"{label} bind cleanup failures",
            close_errors,
        )
    binding.root_id = root_id
    return binding


def _bind_private_tool_root(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
) -> ControlObjectBinding:
    spec = _private_control_primary_spec()
    prebinding = _prebind_existing_primary_private_control_root(spec)
    home = prebinding.home
    parent = prebinding.parent
    tool_root: ControlObjectBinding | None = None
    quarantine: ControlObjectBinding | None = None
    legacy_receipts: tuple[LegacyPrivateControlReceipt, ...] = ()
    try:
        _validate_prebound_primary_private_control_topology(
            root,
            admin,
            common,
            spec,
            prebinding,
        )
        legacy_states, legacy_receipts = _preflight_legacy_private_control_roots(
            root,
            admin,
            common,
            prebinding,
        )
        allocation_allowed, reason_code = _private_control_preallocation_decision(
            legacy_states
        )
        if not allocation_allowed:
            raise MirrorSyncError(
                f"{reason_code}: private-control primary allocation is blocked"
            )
        _revalidate_legacy_private_control_receipts(
            legacy_receipts,
            operation=root.operation,
        )
        if parent is None:
            _operation_checkpoint(
                root.operation,
                "allocating the primary private-control namespace",
            )
            parent = _create_private_control_directory_noreplace(
                home,
                PRIVATE_CONTROL_NAMESPACE_NAME,
                f"private-control allocation root [{spec.root_id}]",
                spec.root_id,
            )
        else:
            _revalidate_absolute_control_object(parent)
            _revalidate_absolute_control_object(home)
            current_tool_record = _private_control_child_metadata(
                parent.fd,
                PRIVATE_TOOL_ROOT_NAME,
                f"existing primary private Git tool root [{spec.root_id}]",
            )
            current_quarantine_record = _private_control_child_metadata(
                parent.fd,
                DURABLE_QUARANTINE_ROOT_NAME,
                f"existing primary durable quarantine root [{spec.root_id}]",
            )
            if (
                current_tool_record != prebinding.tool_record
                or current_quarantine_record != prebinding.quarantine_record
            ):
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                    "existing primary fixed-root metadata changed during "
                    "legacy preflight"
                )
        if prebinding.tool_record is None:
            _operation_checkpoint(
                root.operation,
                "allocating the primary private-control tool root",
            )
            tool_root = _create_private_control_directory_noreplace(
                parent,
                PRIVATE_TOOL_ROOT_NAME,
                f"private Git tool root [{spec.root_id}]",
                spec.root_id,
            )
        else:
            tool_root = _bind_existing_private_control_directory(
                parent,
                PRIVATE_TOOL_ROOT_NAME,
                f"private Git tool root [{spec.root_id}]",
                spec.root_id,
                prebinding.tool_record,
            )
        if prebinding.quarantine_record is None:
            _operation_checkpoint(
                root.operation,
                "allocating the primary private-control quarantine root",
            )
            quarantine = _create_private_control_directory_noreplace(
                parent,
                DURABLE_QUARANTINE_ROOT_NAME,
                f"durable quarantine root [{spec.root_id}]",
                spec.root_id,
            )
        else:
            quarantine = _bind_existing_private_control_directory(
                parent,
                DURABLE_QUARANTINE_ROOT_NAME,
                f"durable quarantine root [{spec.root_id}]",
                spec.root_id,
                prebinding.quarantine_record,
            )
        role_identities = {parent.identity, tool_root.identity, quarantine.identity}
        if len(role_identities) != 3:
            raise MirrorSyncError(
                f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{spec.root_id}]: "
                "primary parent/tool/quarantine roles alias"
            )
        _validate_private_control_root_topology(
            root_id=spec.root_id,
            parent=parent,
            tool_root=tool_root,
            quarantine=quarantine,
            repository_root=root,
            admin=admin,
            common=common,
            home=home,
        )
        _revalidate_absolute_control_object(parent)
        _revalidate_absolute_control_object(home)
        _revalidate_legacy_private_control_receipts(
            legacy_receipts,
            operation=root.operation,
        )
        context = PrimaryPrivateControlContext(
            root_id=spec.root_id,
            home=home,
            parent=parent,
            quarantine=quarantine,
            parent_record=(parent.identity, parent.access_policy),
            tool_record=(tool_root.identity, tool_root.access_policy),
            quarantine_record=(quarantine.identity, quarantine.access_policy),
            legacy_receipts=legacy_receipts,
        )
        legacy_receipts = ()
        tool_root.private_control_context = context
        _revalidate_primary_private_control_context(tool_root)
        return tool_root
    except BaseException as setup_error:
        cleanup_errors: list[str] = []
        if tool_root is not None and tool_root.private_control_context is not None:
            try:
                _close_private_tool_root(tool_root)
            except MirrorSyncError as close_error:
                cleanup_errors.append(str(close_error))
        else:
            try:
                _release_legacy_private_control_receipts(legacy_receipts)
            except MirrorSyncError as release_error:
                cleanup_errors.append(str(release_error))
            closed_fds: set[int] = set()
            for binding in (quarantine, tool_root, parent, home):
                if binding is None or binding.fd < 0 or binding.fd in closed_fds:
                    continue
                closed_fds.add(binding.fd)
                try:
                    os.close(binding.fd)
                except OSError as close_error:
                    cleanup_errors.append(
                        f"cannot close {binding.label}: {close_error}"
                    )
                finally:
                    binding.fd = -1
        if cleanup_errors:
            raise MirrorSyncError(
                f"{setup_error}; secondary private-control bind cleanup failures: "
                + "; ".join(cleanup_errors)
            ) from setup_error
        raise


def _revalidate_primary_private_control_context(
    tool_root: ControlObjectBinding,
) -> None:
    context = tool_root.private_control_context
    if context is None or tool_root.root_id != context.root_id:
        raise MirrorSyncError("primary private-control context is missing or invalid")
    _revalidate_absolute_control_object(context.home)
    parent_record = _private_control_child_metadata(
        context.home.fd,
        PRIVATE_CONTROL_NAMESPACE_NAME,
        f"primary private-control allocation root [{context.root_id}]",
    )
    if parent_record != context.parent_record:
        raise MirrorSyncError(
            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{context.root_id}]: "
            "primary allocation root fixed name changed"
        )
    _revalidate_absolute_control_object(context.parent)
    tool_record = _private_control_child_metadata(
        context.parent.fd,
        PRIVATE_TOOL_ROOT_NAME,
        f"primary private Git tool root [{context.root_id}]",
    )
    quarantine_record = _private_control_child_metadata(
        context.parent.fd,
        DURABLE_QUARANTINE_ROOT_NAME,
        f"primary durable quarantine root [{context.root_id}]",
    )
    if (
        tool_record != context.tool_record
        or quarantine_record != context.quarantine_record
    ):
        raise MirrorSyncError(
            f"{PRIVATE_CONTROL_REASON_INCONCLUSIVE} [{context.root_id}]: "
            "primary fixed-root metadata changed"
        )
    _revalidate_absolute_control_object(tool_root)
    _revalidate_absolute_control_object(context.quarantine)
    _revalidate_absolute_control_object(context.parent)
    _revalidate_absolute_control_object(context.home)


def _close_private_tool_root(tool_root: ControlObjectBinding) -> None:
    context = tool_root.private_control_context
    tool_root.private_control_context = None
    bindings = [tool_root]
    close_errors: list[str] = []
    if context is not None:
        try:
            _release_primary_context_legacy_fences(context)
        except MirrorSyncError as release_error:
            close_errors.append(str(release_error))
        bindings.extend((context.quarantine, context.parent, context.home))
    closed_fds: set[int] = set()
    for binding in bindings:
        if binding.fd < 0:
            continue
        fd = binding.fd
        if fd in closed_fds:
            binding.fd = -1
            continue
        if fd >= 0:
            closed_fds.add(fd)
            try:
                os.close(fd)
            except OSError as error:
                close_errors.append(f"{binding.label}: {error}")
            finally:
                # Never retry a failed close: the descriptor's state is
                # unspecified and its number may already have been reused.
                binding.fd = -1
    if close_errors:
        raise MirrorSyncError(
            "cannot close every retained private-control descriptor: "
            + "; ".join(close_errors)
        )


def _owner_record_payload(
    root_id: str,
    private_name: str,
    private_identity: tuple[int, int, int],
    owner_nonce: str,
    phase: str,
) -> bytes:
    return (
        json.dumps(
            {
                "version": PRIVATE_OWNER_RECORD_VERSION,
                "root_id": root_id,
                "owner_pid": os.getpid(),
                "owner_uid": os.geteuid(),
                "owner_gid": os.getegid(),
                "owner_nonce": owner_nonce,
                "phase": phase,
                "private_name": private_name,
                "private_identity": list(private_identity),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _create_owner_record(
    root: BoundRoot,
    tool_root: ControlObjectBinding,
    quarantine: ControlObjectBinding,
    private_name: str,
    private: ControlObjectBinding,
    *,
    quarantine_locked: bool,
) -> tuple[str, str, ControlObjectBinding]:
    if not quarantine_locked:
        raise MirrorSyncError(
            "private Git owner publication requires the caller-held "
            "durable quarantine lock"
        )
    if tool_root.root_id is None:
        raise MirrorSyncError("private Git tool root is missing its root id")
    owner_nonce = secrets.token_hex(16)
    owner_name = f"{private_name}.owner.json"
    payload = _owner_record_payload(
        tool_root.root_id,
        private_name,
        private.identity,
        owner_nonce,
        "building",
    )
    owner_fd = os.open(
        owner_name,
        _FILE_READ_WRITE_FLAGS,
        0o600,
        dir_fd=tool_root.fd,
    )
    try:
        os.fchmod(owner_fd, 0o600)
        os.fchown(owner_fd, os.geteuid(), os.getegid())
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_all(owner_fd, payload, PurePosixPath(owner_name))
        os.fsync(owner_fd)
        metadata = os.fstat(owner_fd)
        binding = ControlObjectBinding(
            label="private Git snapshot owner record",
            path=tool_root.path / owner_name,
            relative_path=None,
            fd=owner_fd,
            identity=_object_identity(metadata),
            access_policy=_access_policy(metadata),
            content_digest=hashlib.sha256(payload).digest(),
            root_id=tool_root.root_id,
        )
    except BaseException:
        try:
            owner_metadata = os.fstat(owner_fd)
            owner_snapshot = _safe_read_private_owner_record_snapshot(
                tool_root.fd,
                owner_name,
                PurePosixPath(owner_name),
                file_fd=owner_fd,
                operation=root.operation,
            )
            if owner_snapshot.identity == _object_identity(owner_metadata):
                _remove_stale_owner_record(
                    root,
                    tool_root,
                    owner_name,
                    owner_snapshot,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
        except (OSError, MirrorSyncError):
            # An ambiguous record is retained for bounded stale recovery.
            pass
        os.close(owner_fd)
        raise
    os.fsync(tool_root.fd)
    return owner_name, owner_nonce, binding


def _set_owner_record_phase(
    binding: ControlObjectBinding,
    private_name: str,
    private_identity: tuple[int, int, int],
    owner_nonce: str,
    phase: str,
) -> None:
    if binding.root_id is None:
        raise MirrorSyncError("private Git owner record is missing its root id")
    payload = _owner_record_payload(
        binding.root_id,
        private_name,
        private_identity,
        owner_nonce,
        phase,
    )
    metadata = os.fstat(binding.fd)
    if (
        _object_identity(metadata) != binding.identity
        or _access_policy(metadata) != binding.access_policy
    ):
        raise MirrorSyncError("private Git owner record changed before phase update")
    os.lseek(binding.fd, 0, os.SEEK_SET)
    os.ftruncate(binding.fd, 0)
    _write_all(binding.fd, payload, PurePosixPath(binding.label))
    os.fsync(binding.fd)
    final_metadata = os.fstat(binding.fd)
    if (
        _object_identity(final_metadata) != binding.identity
        or _access_policy(final_metadata) != binding.access_policy
        or final_metadata.st_size != len(payload)
    ):
        raise MirrorSyncError("private Git owner record changed during phase update")
    binding.content_digest = hashlib.sha256(payload).digest()


def _create_private_git_directory(
    private_parent: ControlObjectBinding,
) -> tuple[str, Path, ControlObjectBinding]:
    assert private_parent.path is not None
    for _attempt in range(32):
        private_name = (
            f"sync-canonical-git-control.{os.getpid()}.{secrets.token_hex(16)}"
        )
        try:
            os.mkdir(private_name, 0o700, dir_fd=private_parent.fd)
        except FileExistsError:
            continue
        except OSError as error:
            raise MirrorSyncError(
                f"cannot create private Git control snapshot: {error}"
            ) from error
        private_fd = -1
        try:
            private_fd = os.open(
                private_name,
                _DIRECTORY_FLAGS,
                dir_fd=private_parent.fd,
            )
            os.fchmod(private_fd, 0o700)
            path_metadata = os.stat(
                private_name,
                dir_fd=private_parent.fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(private_fd)
            if _object_identity(path_metadata) != _object_identity(descriptor_metadata):
                raise MirrorSyncError(
                    "private Git control directory was replaced while opening"
                )
            private_path = private_parent.path / private_name
            return (
                private_name,
                private_path,
                ControlObjectBinding(
                    label="private Git control snapshot",
                    path=private_path,
                    relative_path=None,
                    fd=private_fd,
                    identity=_object_identity(descriptor_metadata),
                    access_policy=_access_policy(descriptor_metadata),
                    content_digest=None,
                ),
            )
        except BaseException:
            if private_fd >= 0:
                os.close(private_fd)
            # The path may have been replaced after creation. Preserve an
            # ambiguous private directory instead of deleting by pathname.
            raise
    raise MirrorSyncError(
        "cannot allocate a unique private Git control snapshot directory"
    )


def _create_owned_private_directory(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
) -> tuple[
    ControlObjectBinding,
    str,
    Path,
    ControlObjectBinding,
    ControlObjectBinding,
    str,
    str,
]:
    private_parent = _bind_private_tool_root(
        root,
        admin,
        common,
    )
    private_name: str | None = None
    private: ControlObjectBinding | None = None
    owner_name: str | None = None
    owner_record: ControlObjectBinding | None = None
    root_locked = False
    quarantine_locked = False
    locks_reliable = True
    context = private_parent.private_control_context
    if context is None:
        _close_private_tool_root(private_parent)
        raise MirrorSyncError("primary private-control context is missing")
    try:
        try:
            fcntl.flock(
                private_parent.fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            root_locked = True
            fcntl.flock(
                context.quarantine.fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            quarantine_locked = True
        except OSError as error:
            raise MirrorSyncError(
                "private Git tool/quarantine root is busy or unleaseable with "
                "another bounded snapshot, recovery, or cleanup publication"
            ) from error
        _revalidate_primary_private_control_context(private_parent)
        _recover_stale_private_snapshots(
            root,
            private_parent,
            quarantine=context.quarantine,
            quarantine_locked=True,
        )
        _revalidate_primary_private_control_context(private_parent)
        private_name, private_path, private = _create_private_git_directory(
            private_parent
        )
        owner_name, owner_nonce, owner_record = _create_owner_record(
            root,
            private_parent,
            context.quarantine,
            private_name,
            private,
            quarantine_locked=True,
        )
        _revalidate_primary_private_control_context(private_parent)
        _revalidate_legacy_private_control_receipts(
            context.legacy_receipts,
            operation=root.operation,
        )
        _release_primary_context_legacy_fences(context)
        try:
            fcntl.flock(context.quarantine.fd, fcntl.LOCK_UN)
            quarantine_locked = False
            fcntl.flock(private_parent.fd, fcntl.LOCK_UN)
            root_locked = False
        except OSError as error:
            locks_reliable = False
            raise MirrorSyncError(
                f"cannot release private Git publication locks: {error}"
            ) from error
        return (
            private_parent,
            private_name,
            private_path,
            private,
            owner_record,
            owner_name,
            owner_nonce,
        )
    except BaseException as setup_error:
        cleanup_errors: list[str] = []
        private_cleanup_succeeded = private is None
        cleanup_can_mutate = root_locked and quarantine_locked and locks_reliable
        if cleanup_can_mutate and private is not None and private_name is not None:
            try:
                _remove_bound_private_directory(
                    root,
                    private_parent,
                    private,
                    private_name,
                    None,
                )
                private_cleanup_succeeded = True
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    "private snapshot cleanup: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            finally:
                try:
                    os.close(private.fd)
                except OSError as close_error:
                    cleanup_errors.append(
                        "private snapshot descriptor close: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                finally:
                    private.fd = -1
        elif private is not None:
            cleanup_errors.append(
                "private snapshot retained because both publication leases "
                "were not reliably held"
            )
            try:
                os.close(private.fd)
            except OSError as close_error:
                cleanup_errors.append(
                    "private snapshot descriptor close: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            finally:
                private.fd = -1
        if owner_record is not None:
            if (
                cleanup_can_mutate
                and private_cleanup_succeeded
                and owner_name is not None
            ):
                try:
                    _remove_bound_owner_record(
                        root,
                        private_parent,
                        owner_record,
                        owner_name,
                        context.quarantine,
                        quarantine_locked=True,
                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(
                        "owner record cleanup: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            try:
                os.close(owner_record.fd)
            except OSError as close_error:
                cleanup_errors.append(
                    "owner record descriptor close: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            finally:
                owner_record.fd = -1
        # Release publication locks before closing the retained descriptor
        # context. Every step is best-effort so a secondary cleanup failure
        # cannot leak the remaining home/parent/tool/quarantine descriptors.
        if quarantine_locked:
            try:
                fcntl.flock(context.quarantine.fd, fcntl.LOCK_UN)
            except OSError as unlock_error:
                cleanup_errors.append(
                    f"quarantine unlock: {type(unlock_error).__name__}: {unlock_error}"
                )
            finally:
                quarantine_locked = False
        if root_locked:
            try:
                fcntl.flock(private_parent.fd, fcntl.LOCK_UN)
            except OSError as unlock_error:
                cleanup_errors.append(
                    f"tool-root unlock: {type(unlock_error).__name__}: {unlock_error}"
                )
            finally:
                root_locked = False
        try:
            _close_private_tool_root(private_parent)
        except MirrorSyncError as close_error:
            cleanup_errors.append(f"retained context close: {close_error}")
        if cleanup_errors:
            raise MirrorSyncError(
                f"{setup_error}; secondary private-control cleanup failures: "
                + "; ".join(cleanup_errors)
            ) from setup_error
        raise


def _snapshot_bound_git_executable(
    root: BoundRoot,
    private: ControlObjectBinding,
) -> tuple[str, ControlObjectBinding]:
    if private.path is None:
        raise MirrorSyncError("private Git control snapshot must have an absolute path")
    source = root.git_executable
    if source.content_digest is None:
        raise MirrorSyncError("Git executable source bytes are not bound")

    _revalidate_control_object(root, source)
    payload = _bound_control_payload(source)
    _consume_operation_budget(
        root.operation,
        byte_count=len(payload),
        label="snapshotting the bound Git executable",
    )

    for _attempt in range(MAX_PRIVATE_GIT_EXECUTABLE_ATTEMPTS):
        name = f"{PRIVATE_GIT_EXECUTABLE_PREFIX}{secrets.token_hex(16)}"
        destination_fd = -1
        snapshot_fd = -1
        try:
            destination_fd = os.open(
                name,
                _FILE_WRITE_FLAGS,
                0o500,
                dir_fd=private.fd,
            )
            os.fchmod(destination_fd, 0o500)
            os.fchown(destination_fd, os.geteuid(), os.getegid())
            _write_all(
                destination_fd,
                payload,
                PurePosixPath(name),
            )
            os.fsync(destination_fd)
            written_metadata = os.fstat(destination_fd)
            if (
                not stat.S_ISREG(written_metadata.st_mode)
                or _access_policy(written_metadata)
                != (0o500, os.geteuid(), os.getegid())
                or written_metadata.st_size != len(payload)
            ):
                raise MirrorSyncError(
                    "private Git executable snapshot has invalid written state"
                )
            os.close(destination_fd)
            destination_fd = -1

            path_metadata = os.stat(
                name,
                dir_fd=private.fd,
                follow_symlinks=False,
            )
            snapshot_fd = os.open(
                name,
                _FILE_READ_FLAGS,
                dir_fd=private.fd,
            )
            opened_metadata = os.fstat(snapshot_fd)
            if (
                not stat.S_ISREG(path_metadata.st_mode)
                or _object_identity(path_metadata) != _object_identity(opened_metadata)
                or _access_policy(path_metadata) != _access_policy(opened_metadata)
                or _object_identity(opened_metadata)
                != _object_identity(written_metadata)
                or _access_policy(opened_metadata)
                != (0o500, os.geteuid(), os.getegid())
            ):
                raise MirrorSyncError(
                    "private Git executable snapshot changed while reopening it"
                )
            content_digest = _control_content_digest(
                snapshot_fd,
                "private Git executable snapshot",
            )
            if content_digest != source.content_digest:
                raise MirrorSyncError(
                    "private Git executable snapshot differs from bound source bytes"
                )
            binding = ControlObjectBinding(
                label="private Git executable snapshot",
                path=private.path / name,
                relative_path=None,
                fd=snapshot_fd,
                identity=_object_identity(opened_metadata),
                access_policy=_access_policy(opened_metadata),
                content_digest=content_digest,
            )
            os.fsync(private.fd)
            _revalidate_control_object(root, source)
            _revalidate_control_object(root, binding)
            snapshot_fd = -1
            return name, binding
        except FileExistsError:
            continue
        except BaseException:
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            raise
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
    raise MirrorSyncError("cannot allocate a unique private Git executable snapshot")


def _prepare_private_git_executable(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
) -> PrivateGitExecutableBinding:
    (
        private_parent,
        private_name,
        private_path,
        private,
        owner_record,
        owner_record_name,
        owner_nonce,
    ) = _create_owned_private_directory(root, admin, common)
    executable: ControlObjectBinding | None = None
    private_manifest: tuple[tuple[object, ...], ...] = ()
    try:
        executable_name, executable = _snapshot_bound_git_executable(
            root,
            private,
        )
        metadata = os.fstat(executable.fd)
        assert executable.content_digest is not None
        private_manifest = (
            (
                "file",
                executable_name,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                executable.content_digest,
            ),
        )
        names = _bounded_sorted_directory_names(
            private.fd,
            maximum_entries=1,
            label="private Git executable directory",
            limit_error=(
                "private Git executable directory contains unexpected entries"
            ),
            operation=root.operation,
        )
        if names != (executable_name,):
            raise MirrorSyncError(
                "private Git executable directory contains unexpected entries"
            )
        _set_owner_record_phase(
            owner_record,
            private_name,
            private.identity,
            owner_nonce,
            "ready",
        )
        os.fsync(private.fd)
        os.fsync(private_parent.fd)
        return PrivateGitExecutableBinding(
            private_parent=private_parent,
            private=private,
            private_name=private_name,
            private_path=private_path,
            executable_name=executable_name,
            executable=executable,
            private_manifest=private_manifest,
            owner_record=owner_record,
            owner_record_name=owner_record_name,
            owner_nonce=owner_nonce,
        )
    except BaseException as setup_error:
        cleanup_errors: list[str] = []
        try:
            _cleanup_private_git_control(
                root,
                private_parent,
                private,
                private_name,
                private_manifest or None,
                owner_record,
                owner_record_name,
                owner_nonce,
            )
        except BaseException as cleanup_error:
            cleanup_errors.append(
                "private Git executable cleanup: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        cleanup_errors.extend(
            _close_control_bindings_best_effort((executable, owner_record, private))
        )
        try:
            _close_private_tool_root(private_parent)
        except MirrorSyncError as close_error:
            cleanup_errors.append(f"retained context close: {close_error}")
        if cleanup_errors:
            raise MirrorSyncError(
                f"{setup_error}; secondary private Git executable cleanup "
                f"failures: {'; '.join(cleanup_errors)}"
            ) from setup_error
        raise


def _revalidate_private_git_executable(
    root: BoundRoot,
    binding: PrivateGitExecutableBinding,
) -> None:
    _revalidate_primary_private_control_context(binding.private_parent)
    _revalidate_control_object(root, binding.private_parent)
    _revalidate_control_object(root, binding.private)
    _revalidate_control_object(root, binding.owner_record)
    _revalidate_control_object(root, binding.executable)
    names = _bounded_sorted_directory_names(
        binding.private.fd,
        maximum_entries=1,
        label="private Git executable directory",
        limit_error="private Git executable directory contains unexpected entries",
        operation=root.operation,
    )
    if names != (binding.executable_name,):
        raise MirrorSyncError(
            "private Git executable directory namespace changed before "
            "transaction completion"
        )
    _revalidate_control_object(root, binding.private)
    _revalidate_control_object(root, binding.private_parent)
    _revalidate_primary_private_control_context(binding.private_parent)


def _expected_private_manifest(
    source_manifest: tuple[tuple[object, ...], ...],
    overrides: dict[str, ControlObjectBinding | None],
) -> tuple[tuple[object, ...], ...]:
    expected = {str(record[1]): record for record in source_manifest}
    for name, binding in overrides.items():
        if binding is None:
            expected.pop(name, None)
            continue
        metadata = os.fstat(binding.fd)
        payload = _bound_control_payload(binding)
        expected[name] = (
            "file",
            name,
            stat.S_IMODE(metadata.st_mode),
            len(payload),
            hashlib.sha256(payload).digest(),
        )
    return tuple(expected[path] for path in sorted(expected))


def _materialize_private_git_control(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    commondir_name_set: ControlNameSetBinding,
    common_commondir_absence: ControlAbsenceBinding,
    prepared: PrivateGitExecutableBinding,
) -> tuple[
    ControlObjectBinding,
    str,
    Path,
    ControlObjectBinding,
    tuple[ControlObjectBinding, ...],
    tuple[tuple[object, ...], ...],
    ControlObjectBinding,
    str,
    str,
]:
    if root.private_git_executable is not prepared:
        raise MirrorSyncError(
            "private Git executable snapshot ownership changed before "
            "repository materialization"
        )
    private_parent = prepared.private_parent
    private_name = prepared.private_name
    private_path = prepared.private_path
    private = prepared.private
    owner_record = prepared.owner_record
    owner_name = prepared.owner_record_name
    owner_nonce = prepared.owner_nonce
    source_files: list[ControlObjectBinding] = []
    final_manifest: tuple[tuple[object, ...], ...] = ()
    try:
        _set_owner_record_phase(
            owner_record,
            private_name,
            private.identity,
            owner_nonce,
            "building",
        )
        _revalidate_control_name_set(root, commondir_name_set)
        _revalidate_control_absence(root, common_commondir_absence)
        first_budget = {"entries": 0, "bytes": 0}
        first_manifest = _snapshot_git_directory_tree(
            common.fd,
            private.fd,
            prefix=PurePosixPath(),
            budget=first_budget,
            operation=root.operation,
        )
        second_budget = {"entries": 0, "bytes": 0}
        second_manifest = _snapshot_git_directory_tree(
            common.fd,
            None,
            prefix=PurePosixPath(),
            budget=second_budget,
            operation=root.operation,
        )
        if first_manifest != second_manifest:
            raise MirrorSyncError(
                "Git common control tree changed during private snapshot"
            )
        _revalidate_control_name_set(root, commondir_name_set)
        _revalidate_control_absence(root, common_commondir_absence)
        source_manifest = _logical_git_snapshot_manifest(first_manifest)
        copied_manifest = _scan_private_git_tree(
            private.fd,
            root.operation,
            skip_entries=frozenset({PurePosixPath(prepared.executable_name)}),
        )
        if copied_manifest != source_manifest:
            raise MirrorSyncError(
                "private Git control destination differs from its source"
            )
        overrides: dict[str, ControlObjectBinding | None] = {}
        if _object_identity(os.fstat(admin.fd)) != _object_identity(
            os.fstat(common.fd)
        ):
            for name, required in (
                ("HEAD", True),
                ("index", False),
                ("config.worktree", False),
            ):
                binding = _replace_private_control_file(
                    admin,
                    private.fd,
                    name,
                    required=required,
                )
                overrides[name] = binding
                if binding is not None:
                    source_files.append(binding)
        else:
            for name in ("HEAD", "index", "config"):
                binding = _bind_relative_control_file(
                    admin,
                    name,
                    f"Git {name} control file",
                )
                if binding is None:
                    if name != "index":
                        raise MirrorSyncError(f"Git {name} control file is missing")
                    continue
                source_files.append(binding)
        os.fsync(private.fd)
        final_tree_manifest = _scan_private_git_tree(
            private.fd,
            root.operation,
            skip_entries=frozenset({PurePosixPath(prepared.executable_name)}),
        )
        expected_manifest = _expected_private_manifest(
            source_manifest,
            overrides,
        )
        if final_tree_manifest != expected_manifest:
            raise MirrorSyncError(
                "private Git control snapshot differs from bound source bytes"
            )
        final_manifest = _scan_private_git_control(
            private.fd,
            root.operation,
            skip_entries=frozenset({PurePosixPath(prepared.executable_name)}),
        )
        if final_manifest != _git_control_plane_manifest(expected_manifest):
            raise MirrorSyncError(
                "private Git control plane differs from bound source bytes"
            )
        _revalidate_control_name_set(root, commondir_name_set)
        _revalidate_control_absence(root, common_commondir_absence)
        _set_owner_record_phase(
            owner_record,
            private_name,
            private.identity,
            owner_nonce,
            "ready",
        )
        os.fsync(private_parent.fd)
    except BaseException as setup_error:
        cleanup_errors = list(_close_control_bindings_best_effort(tuple(source_files)))
        try:
            _cleanup_private_git_control(
                root,
                private_parent,
                private,
                private_name,
                None,
                owner_record,
                owner_name,
                owner_nonce,
            )
        except BaseException as cleanup_error:
            cleanup_errors.append(
                "private Git materialization cleanup: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        cleanup_errors.extend(
            _close_control_bindings_best_effort(
                (prepared.executable, owner_record, private)
            )
        )
        try:
            _close_private_tool_root(private_parent)
        except MirrorSyncError as close_error:
            cleanup_errors.append(f"retained context close: {close_error}")
        root.private_git_executable = None
        if cleanup_errors:
            raise MirrorSyncError(
                f"{setup_error}; secondary private Git materialization "
                f"cleanup failures: {'; '.join(cleanup_errors)}"
            ) from setup_error
        raise
    return (
        private_parent,
        private_name,
        private_path,
        private,
        tuple(source_files),
        final_manifest,
        owner_record,
        owner_name,
        owner_nonce,
    )


def _parse_bound_head_reference(
    head: ControlObjectBinding,
) -> PurePosixPath | None:
    payload = _bound_control_payload(head)
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    try:
        detached = payload.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        detached = ""
    if GIT_SHA_RE.fullmatch(detached) is not None:
        return None
    prefix = b"ref: "
    if (
        not payload.startswith(prefix)
        or b"\0" in payload
        or b"\r" in payload
        or b"\n" in payload
    ):
        raise MirrorSyncError("Git HEAD has an invalid detached/symbolic record")
    try:
        raw_reference = payload[len(prefix) :].decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise MirrorSyncError(
            "Git HEAD symbolic reference must be valid UTF-8"
        ) from error
    reference = _validate_relative_path(
        raw_reference,
        "Git HEAD symbolic reference",
    )
    if reference.parts[0] != "refs":
        raise MirrorSyncError("Git HEAD symbolic reference must remain below refs/")
    return reference


def _require_private_source_controls(
    private_manifest: tuple[tuple[object, ...], ...],
    files: dict[PurePosixPath, ControlObjectBinding],
    directories: dict[PurePosixPath, ControlObjectBinding],
    absences: set[PurePosixPath],
) -> None:
    records = {str(record[1]): record for record in private_manifest}
    for relative_path, binding in files.items():
        payload = _bound_control_payload(binding)
        metadata = os.fstat(binding.fd)
        expected = (
            "file",
            relative_path.as_posix(),
            stat.S_IMODE(metadata.st_mode),
            len(payload),
            hashlib.sha256(payload).digest(),
        )
        if records.get(relative_path.as_posix()) != expected:
            raise MirrorSyncError(
                "private Git control snapshot differs from bound source "
                f"file: {relative_path}"
            )
    for relative_path, binding in directories.items():
        expected = (
            "directory",
            relative_path.as_posix(),
            binding.access_policy[0],
        )
        if records.get(relative_path.as_posix()) != expected:
            raise MirrorSyncError(
                "private Git control snapshot differs from bound source "
                f"directory: {relative_path}"
            )
    for relative_path in absences:
        prefix = relative_path.as_posix() + "/"
        if any(
            path == relative_path.as_posix() or path.startswith(prefix)
            for path in records
        ):
            raise MirrorSyncError(
                "private Git control snapshot contains a source path that "
                f"was bound absent: {relative_path}"
            )


def _bind_git_source_controls(
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
    initial_files: tuple[ControlObjectBinding, ...],
    initial_absences: tuple[ControlAbsenceBinding, ...],
    private_manifest: tuple[tuple[object, ...], ...],
    acquired: list[ControlObjectBinding],
) -> tuple[
    tuple[ControlObjectBinding, ...],
    tuple[ControlObjectBinding, ...],
    tuple[ControlAbsenceBinding, ...],
]:
    files = list(initial_files)
    directories: list[ControlObjectBinding] = []
    absences = list(initial_absences)
    private_files: dict[PurePosixPath, ControlObjectBinding] = {}
    private_directories: dict[PurePosixPath, ControlObjectBinding] = {}
    private_absences: set[PurePosixPath] = set()

    def existing_file(
        parent: ControlObjectBinding,
        name: str,
    ) -> ControlObjectBinding | None:
        assert parent.path is not None
        expected_path = parent.path / name
        return next(
            (binding for binding in files if binding.path == expected_path),
            None,
        )

    def bind_file(
        parent: ControlObjectBinding,
        name: str,
        label: str,
        private_path: PurePosixPath,
        *,
        required: bool,
    ) -> ControlObjectBinding | None:
        binding = existing_file(parent, name)
        if binding is None:
            binding = _bind_relative_control_file(parent, name, label)
            if binding is not None:
                files.append(binding)
                acquired.append(binding)
        if binding is None:
            if required:
                raise MirrorSyncError(f"{label} is missing")
            absences.append(
                ControlAbsenceBinding(
                    label=label,
                    parent=parent,
                    name=name,
                )
            )
            private_absences.add(private_path)
            return None
        private_files[private_path] = binding
        return binding

    # A private Git control plane must never inherit a commondir marker. The
    # source-side collision-aware absence bindings are carried separately for
    # descriptor-relative revalidation; this logical manifest check additionally
    # proves the exact spelling is absent from the copied snapshot.
    private_absences.add(PurePosixPath("commondir"))

    head = bind_file(
        admin,
        "HEAD",
        "Git HEAD control file",
        PurePosixPath("HEAD"),
        required=True,
    )
    bind_file(
        admin,
        "index",
        "Git index control file",
        PurePosixPath("index"),
        required=False,
    )
    bind_file(
        admin,
        "config.worktree",
        "Git worktree config control file",
        PurePosixPath("config.worktree"),
        required=False,
    )
    bind_file(
        common,
        "config",
        "Git common config control file",
        PurePosixPath("config"),
        required=True,
    )
    bind_file(
        common,
        "packed-refs",
        "Git packed-refs control file",
        PurePosixPath("packed-refs"),
        required=False,
    )
    assert head is not None
    reference = _parse_bound_head_reference(head)
    if reference is not None:
        current_parent = common
        traversed = PurePosixPath()
        missing_parent = False
        for component in reference.parts[:-1]:
            traversed /= component
            child = _bind_relative_control_directory(
                current_parent,
                component,
                f"Git symbolic-ref directory {traversed}",
            )
            if child is None:
                absences.append(
                    ControlAbsenceBinding(
                        label=f"Git symbolic-ref directory {traversed}",
                        parent=current_parent,
                        name=component,
                    )
                )
                private_absences.add(traversed)
                missing_parent = True
                break
            directories.append(child)
            acquired.append(child)
            private_directories[traversed] = child
            current_parent = child
        if not missing_parent:
            bind_file(
                current_parent,
                reference.name,
                f"Git resolved HEAD reference {reference}",
                reference,
                required=False,
            )

    _require_private_source_controls(
        private_manifest,
        private_files,
        private_directories,
        private_absences,
    )
    return tuple(files), tuple(directories), tuple(absences)


def _revalidate_absolute_control_object(
    binding: ControlObjectBinding,
) -> None:
    if binding.path is None or binding.relative_path is not None:
        raise MirrorSyncError(f"{binding.label} is not an absolute control object")
    try:
        path_metadata = os.stat(binding.path, follow_symlinks=False)
    except FileNotFoundError as error:
        raise MirrorSyncError(
            f"{binding.label} went missing before transaction completion"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} became unreadable before transaction completion: {error}"
        ) from error
    try:
        descriptor_metadata = os.fstat(binding.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} descriptor revalidation failed before "
            f"transaction completion: {error}"
        ) from error
    if (
        _object_identity(path_metadata) != binding.identity
        or _object_identity(descriptor_metadata) != binding.identity
    ):
        raise MirrorSyncError(
            f"{binding.label} was replaced before transaction completion"
        )
    if (
        _access_policy(path_metadata) != binding.access_policy
        or _access_policy(descriptor_metadata) != binding.access_policy
    ):
        raise MirrorSyncError(
            f"{binding.label} access policy changed before transaction completion"
        )
    if binding.content_digest is not None:
        observed_digest = _control_content_digest(binding.fd, binding.label)
        if observed_digest != binding.content_digest:
            raise MirrorSyncError(
                f"{binding.label} content changed before transaction completion"
            )


def _revalidate_control_object(
    root: BoundRoot,
    binding: ControlObjectBinding,
) -> None:
    if binding.relative_path is None:
        _revalidate_absolute_control_object(binding)
        return
    try:
        path_metadata = os.stat(
            binding.relative_path.as_posix(),
            dir_fd=root.fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise MirrorSyncError(
            f"{binding.label} went missing before transaction completion"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} became unreadable before transaction completion: {error}"
        ) from error
    try:
        descriptor_metadata = os.fstat(binding.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} descriptor revalidation failed before "
            f"transaction completion: {error}"
        ) from error
    if (
        _object_identity(path_metadata) != binding.identity
        or _object_identity(descriptor_metadata) != binding.identity
    ):
        raise MirrorSyncError(
            f"{binding.label} was replaced before transaction completion"
        )
    if (
        _access_policy(path_metadata) != binding.access_policy
        or _access_policy(descriptor_metadata) != binding.access_policy
    ):
        raise MirrorSyncError(
            f"{binding.label} access policy changed before transaction completion"
        )
    if binding.content_digest is not None:
        observed_digest = _control_content_digest(binding.fd, binding.label)
        if observed_digest != binding.content_digest:
            raise MirrorSyncError(
                f"{binding.label} content changed before transaction completion"
            )


def _revalidate_control_name_set(
    root: BoundRoot,
    binding: ControlNameSetBinding,
) -> None:
    _revalidate_control_object(root, binding.parent)
    observed = _control_marker_collision_names(
        binding.parent,
        binding.name,
        binding.label,
    )
    if observed != binding.expected_names:
        raise MirrorSyncError(
            f"{binding.label} portable collision set changed before "
            "transaction completion: expected "
            f"{binding.expected_names!r}, observed {observed!r}"
        )
    _revalidate_control_object(root, binding.parent)


def _revalidate_control_absence(
    root: BoundRoot,
    binding: ControlAbsenceBinding,
) -> None:
    _revalidate_control_object(root, binding.parent)
    if binding.collision_key is not None:
        names = _bounded_sorted_directory_names(
            binding.parent.fd,
            maximum_entries=MAX_GIT_SNAPSHOT_ENTRIES,
            label=f"{binding.label} absence parent",
            limit_error=(
                f"{binding.label} absence parent exceeds the "
                f"{MAX_GIT_SNAPSHOT_ENTRIES}-entry limit"
            ),
            operation=root.operation,
        )
        for name in names:
            if _path_collision_key(PurePosixPath(name)) == binding.collision_key:
                raise MirrorSyncError(
                    f"{binding.label} appeared before transaction completion"
                )
        _revalidate_control_object(root, binding.parent)
        return
    try:
        os.stat(
            binding.name,
            dir_fd=binding.parent.fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise MirrorSyncError(
            f"{binding.label} absence became unreadable: {error}"
        ) from error
    raise MirrorSyncError(f"{binding.label} appeared before transaction completion")


def _revalidate_private_git_control(
    root: BoundRoot,
    binding: GitControlBinding,
) -> None:
    _revalidate_primary_private_control_context(binding.private_parent)
    _revalidate_control_object(root, binding.private_parent)
    _revalidate_control_object(root, binding.private)
    _revalidate_control_object(root, binding.private_objects)
    _revalidate_control_object(root, binding.private_git_executable)
    _revalidate_private_git_objects(
        binding.private_objects.fd,
        binding.private_objects_manifest,
        root.operation,
    )
    observed = _scan_private_git_control(
        binding.private.fd,
        root.operation,
        skip_entries=frozenset({PurePosixPath(binding.private_git_executable_name)}),
    )
    if observed != binding.private_manifest:
        raise MirrorSyncError(
            "private Git control snapshot changed before transaction completion"
        )
    _revalidate_primary_private_control_context(binding.private_parent)


def _remove_private_tree_contents(
    directory_fd: int,
    display_path: PurePosixPath,
    operation: OperationBudget | None = None,
    budget: dict[str, int] | None = None,
) -> None:
    if budget is None:
        budget = {"entries": 0}
    _operation_checkpoint(
        operation,
        f"cleaning isolated private Git directory {display_path}",
    )
    scanned_entries = budget.setdefault("scanned_entries", 0)
    names = _bounded_sorted_directory_names(
        directory_fd,
        maximum_entries=max(0, MAX_GIT_SNAPSHOT_ENTRIES - scanned_entries),
        label=f"isolated private Git control directory {display_path}",
        limit_error="isolated private Git cleanup exceeds its entry limit",
        operation=operation,
    )
    budget["scanned_entries"] = scanned_entries + len(names)
    for name in names:
        budget["entries"] += 1
        if budget["entries"] > MAX_GIT_SNAPSHOT_ENTRIES:
            raise MirrorSyncError(
                "isolated private Git cleanup exceeds its entry limit"
            )
        child_path = display_path / name
        try:
            path_metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inspect isolated private Git control entry "
                f"{child_path}: {error}"
            ) from error
        if stat.S_ISDIR(path_metadata.st_mode):
            child_fd = -1
            try:
                child_fd = os.open(
                    name,
                    _DIRECTORY_FLAGS,
                    dir_fd=directory_fd,
                )
                opened_metadata = os.fstat(child_fd)
                if _object_identity(path_metadata) != _object_identity(opened_metadata):
                    raise MirrorSyncError(
                        f"isolated private Git control directory was replaced: "
                        f"{child_path}"
                    )
                _remove_private_tree_contents(
                    child_fd,
                    child_path,
                    operation,
                    budget,
                )
                final_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if _object_identity(final_metadata) != _object_identity(
                    opened_metadata
                ):
                    raise MirrorSyncError(
                        f"isolated private Git control directory changed "
                        f"before cleanup: {child_path}"
                    )
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
        elif stat.S_ISREG(path_metadata.st_mode):
            file_fd = -1
            try:
                file_fd = os.open(
                    name,
                    _FILE_READ_FLAGS,
                    dir_fd=directory_fd,
                )
                opened_metadata = os.fstat(file_fd)
                if _object_identity(path_metadata) != _object_identity(opened_metadata):
                    raise MirrorSyncError(
                        f"isolated private Git control file was replaced: {child_path}"
                    )
                final_metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if _object_identity(final_metadata) != _object_identity(
                    opened_metadata
                ):
                    raise MirrorSyncError(
                        f"isolated private Git control file changed before "
                        f"cleanup: {child_path}"
                    )
                os.unlink(name, dir_fd=directory_fd)
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
        else:
            raise MirrorSyncError(
                f"isolated private Git control entry has an unsafe type: {child_path}"
            )
    os.fsync(directory_fd)


def _quarantine_tool_entry(
    tool_root: ControlObjectBinding,
    name: str,
    expected_identity: tuple[int, int, int] | None = None,
    expected_access_policy: tuple[int, int, int] | None = None,
) -> str:
    try:
        before = os.stat(
            name,
            dir_fd=tool_root.fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot bind private Git quarantine entry {name}: {error}"
        ) from error
    if expected_identity is not None and _object_identity(before) != expected_identity:
        raise MirrorSyncError(f"private Git quarantine entry was replaced: {name}")
    if (
        expected_access_policy is not None
        and _access_policy(before) != expected_access_policy
    ):
        raise MirrorSyncError(
            f"private Git quarantine entry access policy changed: {name}"
        )
    quarantine_name = f".quarantine-{os.getpid()}-{secrets.token_hex(16)}"
    _rename_directory_entry_noreplace(
        tool_root.fd,
        name,
        quarantine_name,
    )
    moved = os.stat(
        quarantine_name,
        dir_fd=tool_root.fd,
        follow_symlinks=False,
    )
    if _object_identity(moved) != _object_identity(before) or _access_policy(
        moved
    ) != _access_policy(before):
        raise MirrorSyncError(
            f"private Git quarantine entry changed during isolation: {name}"
        )
    os.fsync(tool_root.fd)
    return quarantine_name


def _remove_stale_owner_record(
    root: BoundRoot,
    tool_root: ControlObjectBinding,
    owner_name: str,
    owner_snapshot: FileSnapshot,
    *,
    quarantine: ControlObjectBinding,
    quarantine_locked: bool,
) -> None:
    _isolate_and_remove_file(
        root,
        tool_root.fd,
        owner_name,
        owner_snapshot,
        PurePosixPath(owner_name),
        quarantine=quarantine,
        retention_kind=QUARANTINE_TRANSIENT_KIND,
        quarantine_locked=quarantine_locked,
    )


def _recover_stale_private_snapshots(
    root: BoundRoot,
    tool_root: ControlObjectBinding,
    *,
    quarantine: ControlObjectBinding,
    quarantine_locked: bool,
) -> None:
    if not quarantine_locked:
        raise MirrorSyncError(
            "private Git stale recovery requires the caller-held durable "
            "quarantine lock"
        )
    if tool_root.root_id is None:
        raise MirrorSyncError("private Git tool root is missing its root id")

    _operation_checkpoint(root.operation, "scanning private Git tool root")
    names = _bounded_sorted_directory_names(
        tool_root.fd,
        maximum_entries=MAX_TOOL_ROOT_ENTRIES,
        label="private Git tool root",
        limit_error=(f"private Git tool root exceeds {MAX_TOOL_ROOT_ENTRIES} entries"),
        operation=root.operation,
    )
    owner_names = [
        name
        for name in names
        if name.endswith(".owner.json")
        and PRIVATE_SNAPSHOT_RE.fullmatch(name[: -len(".owner.json")]) is not None
    ]
    cleaned = 0
    referenced: set[str] = set()
    for owner_name in owner_names:
        if cleaned >= MAX_STALE_SNAPSHOTS_PER_OPERATION:
            raise MirrorSyncError(
                "private Git tool root has too many stale snapshots "
                "for one bounded recovery"
            )
        private_name = owner_name[: -len(".owner.json")]
        referenced.add(private_name)
        owner_fd = -1
        try:
            owner_fd = os.open(
                owner_name,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=tool_root.fd,
            )
            try:
                fcntl.flock(
                    owner_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                continue
            locked_owner_metadata = os.fstat(owner_fd)
            owner_snapshot = _safe_read_private_owner_record_snapshot(
                tool_root.fd,
                owner_name,
                PurePosixPath(owner_name),
                file_fd=owner_fd,
                operation=root.operation,
            )
            if owner_snapshot.identity != _object_identity(
                locked_owner_metadata
            ) or owner_snapshot.access_policy != _access_policy(locked_owner_metadata):
                raise MirrorSyncError(
                    "private Git owner path was replaced after its lock "
                    f"was acquired: {owner_name}"
                )
            try:
                record = json.loads(
                    owner_snapshot.payload.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                RecursionError,
                MirrorSyncError,
            ):
                _quarantine_tool_entry(
                    tool_root,
                    owner_name,
                    owner_snapshot.identity,
                    owner_snapshot.access_policy,
                )
                try:
                    private_metadata = os.stat(
                        private_name,
                        dir_fd=tool_root.fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    _quarantine_tool_entry(
                        tool_root,
                        private_name,
                        _object_identity(private_metadata),
                        _access_policy(private_metadata),
                    )
                cleaned += 1
                continue
            root_scope = _private_owner_record_root_scope(
                record,
                tool_root.root_id,
            )
            if root_scope == PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH:
                raise MirrorSyncError(
                    f"{PRIVATE_OWNER_RECORD_REASON_ROOT_MISMATCH} "
                    f"[{tool_root.root_id}]: retaining owner record {owner_name}"
                )
            valid_record = (
                isinstance(record, dict)
                and len(owner_snapshot.payload) <= MAX_PRIVATE_OWNER_RECORD_BYTES
                and root_scope in {"accepted-legacy", "accepted-current"}
                and type(record["owner_uid"]) is int
                and record["owner_uid"] == os.geteuid()
                and type(record["owner_gid"]) is int
                and record["owner_gid"] == os.getegid()
                and type(record["owner_pid"]) is int
                and record["owner_pid"] > 0
                and isinstance(record["owner_nonce"], str)
                and re.fullmatch(
                    r"[0-9a-f]{32}",
                    record["owner_nonce"],
                )
                is not None
                and isinstance(record["phase"], str)
                and record["phase"] in PRIVATE_OWNER_RECORD_PHASES
                and record["private_name"] == private_name
                and isinstance(record["private_identity"], list)
                and len(record["private_identity"]) == 3
                and all(
                    type(item) is int and item >= 0
                    for item in record["private_identity"]
                )
                and owner_snapshot.mode == 0o600
                and owner_snapshot.access_policy[1:]
                == (
                    os.geteuid(),
                    os.getegid(),
                )
            )
            if not valid_record:
                _quarantine_tool_entry(
                    tool_root,
                    owner_name,
                    owner_snapshot.identity,
                    owner_snapshot.access_policy,
                )
                try:
                    private_metadata = os.stat(
                        private_name,
                        dir_fd=tool_root.fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    _quarantine_tool_entry(
                        tool_root,
                        private_name,
                        _object_identity(private_metadata),
                        _access_policy(private_metadata),
                    )
                cleaned += 1
                continue
            try:
                private_metadata = os.stat(
                    private_name,
                    dir_fd=tool_root.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                _remove_stale_owner_record(
                    root,
                    tool_root,
                    owner_name,
                    owner_snapshot,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
                cleaned += 1
                continue
            expected_identity = tuple(record["private_identity"])
            if _object_identity(
                private_metadata
            ) != expected_identity or not stat.S_ISDIR(private_metadata.st_mode):
                _quarantine_tool_entry(
                    tool_root,
                    owner_name,
                    owner_snapshot.identity,
                    owner_snapshot.access_policy,
                )
                _quarantine_tool_entry(
                    tool_root,
                    private_name,
                    _object_identity(private_metadata),
                    _access_policy(private_metadata),
                )
                cleaned += 1
                continue
            private_fd = os.open(
                private_name,
                _DIRECTORY_FLAGS,
                dir_fd=tool_root.fd,
            )
            try:
                if _object_identity(os.fstat(private_fd)) != expected_identity:
                    _quarantine_tool_entry(
                        tool_root,
                        owner_name,
                        owner_snapshot.identity,
                        owner_snapshot.access_policy,
                    )
                    _quarantine_tool_entry(
                        tool_root,
                        private_name,
                        _object_identity(private_metadata),
                        _access_policy(private_metadata),
                    )
                    cleaned += 1
                    continue
                assert tool_root.path is not None
                private = ControlObjectBinding(
                    label="stale private Git control snapshot",
                    path=tool_root.path / private_name,
                    relative_path=None,
                    fd=private_fd,
                    identity=expected_identity,
                    access_policy=_access_policy(private_metadata),
                    content_digest=None,
                )
                _remove_bound_private_directory(
                    root,
                    tool_root,
                    private,
                    private_name,
                    None,
                )
                _remove_stale_owner_record(
                    root,
                    tool_root,
                    owner_name,
                    owner_snapshot,
                    quarantine=quarantine,
                    quarantine_locked=True,
                )
            finally:
                os.close(private_fd)
            cleaned += 1
        finally:
            if owner_fd >= 0:
                os.close(owner_fd)
    orphan_names = [
        name
        for name in names
        if PRIVATE_SNAPSHOT_RE.fullmatch(name) is not None and name not in referenced
    ]
    for private_name in orphan_names:
        if cleaned >= MAX_STALE_SNAPSHOTS_PER_OPERATION:
            raise MirrorSyncError(
                "private Git tool root has too many orphan snapshots "
                "for one bounded quarantine"
            )
        try:
            private_metadata = os.stat(
                private_name,
                dir_fd=tool_root.fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot bind orphan private Git snapshot {private_name}: {error}"
            ) from error
        _quarantine_tool_entry(
            tool_root,
            private_name,
            _object_identity(private_metadata),
            _access_policy(private_metadata),
        )
        cleaned += 1


def _remove_bound_private_directory(
    root: BoundRoot,
    private_parent: ControlObjectBinding,
    private: ControlObjectBinding,
    private_name: str,
    expected_manifest: tuple[tuple[object, ...], ...] | None,
) -> None:
    _revalidate_control_object(root, private_parent)
    _revalidate_control_object(root, private)
    if (
        expected_manifest is not None
        and _scan_private_git_control(private.fd, root.operation) != expected_manifest
    ):
        raise MirrorSyncError("private Git control snapshot changed before cleanup")
    isolated_name = f".sync-private-remove-{os.getpid()}-{secrets.token_hex(16)}"
    try:
        _rename_directory_entry_noreplace(
            private_parent.fd,
            private_name,
            isolated_name,
        )
        os.fsync(private_parent.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot isolate private Git control snapshot before cleanup: {error}"
        ) from error
    moved_metadata = os.stat(
        isolated_name,
        dir_fd=private_parent.fd,
        follow_symlinks=False,
    )
    if (
        _object_identity(moved_metadata) != private.identity
        or _access_policy(moved_metadata) != private.access_policy
        or _object_identity(os.fstat(private.fd)) != private.identity
    ):
        try:
            _rename_directory_entry_noreplace(
                private_parent.fd,
                isolated_name,
                private_name,
            )
            os.fsync(private_parent.fd)
        except OSError as restore_error:
            raise MirrorSyncError(
                f"private Git control path changed during isolation; "
                f"preserving {isolated_name}: {restore_error}"
            ) from restore_error
        raise MirrorSyncError("private Git control path changed before cleanup")
    try:
        _remove_private_tree_contents(
            private.fd,
            PurePosixPath(isolated_name),
            root.operation,
        )
        final_metadata = os.stat(
            isolated_name,
            dir_fd=private_parent.fd,
            follow_symlinks=False,
        )
        if _object_identity(final_metadata) != private.identity:
            raise MirrorSyncError(
                "isolated private Git control directory was replaced "
                "before final cleanup"
            )
        os.rmdir(isolated_name, dir_fd=private_parent.fd)
        os.fsync(private_parent.fd)
    except BaseException as error:
        raise MirrorSyncError(
            f"cannot clean isolated private Git control snapshot; preserving "
            f"{isolated_name}: {error}"
        ) from error


def _remove_bound_owner_record(
    root: BoundRoot,
    private_parent: ControlObjectBinding,
    owner_record: ControlObjectBinding,
    owner_name: str,
    quarantine: ControlObjectBinding,
    *,
    quarantine_locked: bool,
) -> None:
    if not quarantine_locked:
        raise MirrorSyncError(
            "private Git owner cleanup requires the caller-held durable quarantine lock"
        )
    _revalidate_control_object(root, private_parent)
    _revalidate_control_object(root, owner_record)
    owner_snapshot = _safe_read_private_owner_record_snapshot(
        private_parent.fd,
        owner_name,
        PurePosixPath(owner_name),
        file_fd=owner_record.fd,
        operation=root.operation,
    )
    if (
        owner_snapshot.identity != owner_record.identity
        or owner_snapshot.access_policy != owner_record.access_policy
        or hashlib.sha256(owner_snapshot.payload).digest()
        != owner_record.content_digest
    ):
        raise MirrorSyncError("private Git owner record changed before cleanup")
    _isolate_and_remove_file(
        root,
        private_parent.fd,
        owner_name,
        owner_snapshot,
        PurePosixPath(owner_name),
        quarantine=quarantine,
        retention_kind=QUARANTINE_TRANSIENT_KIND,
        quarantine_locked=True,
    )
    os.fsync(private_parent.fd)


def _cleanup_private_git_control(
    root: BoundRoot,
    private_parent: ControlObjectBinding,
    private: ControlObjectBinding,
    private_name: str,
    expected_manifest: tuple[tuple[object, ...], ...] | None,
    owner_record: ControlObjectBinding,
    owner_name: str,
    owner_nonce: str,
) -> None:
    context = private_parent.private_control_context
    if context is None:
        raise MirrorSyncError("primary private-control context is missing at cleanup")
    acquire_errors: list[str] = []
    release_errors: list[str] = []
    attempted_leases: list[tuple[str, int]] = []
    for label, file_fd in (
        ("tool-root", private_parent.fd),
        ("quarantine", context.quarantine.fd),
    ):
        attempted_leases.append((label, file_fd))
        try:
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            acquire_errors.append(f"{label} acquire: {type(error).__name__}: {error}")

    cleanup_error: BaseException | None = None
    if not acquire_errors:
        try:
            _revalidate_primary_private_control_context(private_parent)
            _set_owner_record_phase(
                owner_record,
                private_name,
                private.identity,
                owner_nonce,
                "cleanup",
            )
            _remove_bound_private_directory(
                root,
                private_parent,
                private,
                private_name,
                expected_manifest,
            )
            _remove_bound_owner_record(
                root,
                private_parent,
                owner_record,
                owner_name,
                context.quarantine,
                quarantine_locked=True,
            )
            _revalidate_primary_private_control_context(private_parent)
        except BaseException as error:
            cleanup_error = error

    for label, file_fd in reversed(attempted_leases):
        try:
            # An acquire error can be reported after the kernel changed the
            # lease state. Always issue the matching release, but never mutate
            # unless both acquisitions completed without error.
            fcntl.flock(file_fd, fcntl.LOCK_UN)
        except OSError as error:
            release_errors.append(f"{label} unlock: {type(error).__name__}: {error}")

    lease_errors = (*acquire_errors, *release_errors)
    if cleanup_error is not None:
        if lease_errors:
            raise MirrorSyncError(
                f"{cleanup_error}; secondary private Git cleanup lease "
                f"failures: {'; '.join(lease_errors)}"
            ) from cleanup_error
        raise cleanup_error
    if lease_errors:
        raise MirrorSyncError(
            "cannot complete private Git cleanup lease actions: "
            + "; ".join(lease_errors)
        )


def _bind_root(root: Path, *, exclusive: bool = False) -> BoundRoot:
    path = Path(os.path.abspath(root))
    root_fd = _open_root(path)
    lock_operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(root_fd, lock_operation | fcntl.LOCK_NB)
    except OSError as error:
        os.close(root_fd)
        lock_name = "exclusive" if exclusive else "shared"
        raise MirrorSyncError(
            f"cannot acquire root-bound {lock_name} transaction lock: {path}: {error}"
        ) from error
    try:
        git_executable = _bind_absolute_control_object(
            GIT_EXECUTABLE,
            "Git executable",
            require_directory=False,
        )
    except BaseException:
        fcntl.flock(root_fd, fcntl.LOCK_UN)
        os.close(root_fd)
        raise
    root_metadata = os.fstat(root_fd)
    return BoundRoot(
        path=path,
        fd=root_fd,
        identity=_object_identity(root_metadata),
        access_policy=_access_policy(root_metadata),
        exclusive=exclusive,
        git_executable=git_executable,
    )


def _revalidate_bound_root_directory(root: BoundRoot) -> None:
    _operation_checkpoint(root.operation, f"revalidating root {root.path}")
    try:
        path_metadata = os.stat(root.path, follow_symlinks=False)
    except FileNotFoundError as error:
        raise MirrorSyncError(
            f"root went missing before transaction completion: {root.path}"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"root became unreadable before transaction completion: "
            f"{root.path}: {error}"
        ) from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(
            f"root became a symlink before transaction completion: {root.path}"
        )
    descriptor_metadata = os.fstat(root.fd)
    if (
        _object_identity(path_metadata) != root.identity
        or _object_identity(descriptor_metadata) != root.identity
    ):
        raise MirrorSyncError(
            f"root was replaced before transaction completion: {root.path}"
        )
    if (
        _access_policy(path_metadata) != root.access_policy
        or _access_policy(descriptor_metadata) != root.access_policy
    ):
        raise MirrorSyncError(
            f"root access policy changed before transaction completion: {root.path}"
        )


def _revalidate_bound_root(root: BoundRoot) -> None:
    git_control = root.git_control
    private_git_executable = root.private_git_executable
    try:
        _revalidate_bound_root_directory(root)
        if private_git_executable is None and git_control is None:
            # The source executable is an input only until its exact bytes
            # have been copied and rebound inside the private snapshot.
            _revalidate_control_object(root, root.git_executable)
        elif private_git_executable is not None:
            _revalidate_private_git_executable(
                root,
                private_git_executable,
            )
        for managed_ancestor in root.managed_ancestors.values():
            _revalidate_control_object(root, managed_ancestor)
        if git_control is not None:
            _revalidate_control_object(root, git_control.marker)
            _revalidate_control_object(root, git_control.admin)
            _revalidate_control_name_set(
                root,
                git_control.commondir_name_set,
            )
            if git_control.commondir_file is not None:
                _revalidate_control_object(
                    root,
                    git_control.commondir_file,
                )
            _revalidate_control_object(root, git_control.common)
            _revalidate_control_object(root, git_control.objects)
            for binding in git_control.source_directories:
                _revalidate_control_object(root, binding)
            for binding in git_control.source_files:
                _revalidate_control_object(root, binding)
            for binding in git_control.source_absences:
                _revalidate_control_absence(root, binding)
            _revalidate_control_object(root, git_control.owner_record)
            _revalidate_private_git_control(root, git_control)
    except BaseException:
        if git_control is not None:
            # The full fsck result belongs only to this exact, still-bound
            # private snapshot. Any failed source/private/root revalidation
            # invalidates that result even if a later observation looks stable.
            git_control.object_integrity_verified = False
        raise


def _close_bound_root(root: BoundRoot) -> None:
    cleanup_errors: list[MirrorSyncError] = []
    closed_fds: set[int] = set()

    def remember(error: MirrorSyncError) -> None:
        cleanup_errors.append(error)

    def remember_terminal_error(label: str, error: MirrorSyncError | OSError) -> None:
        if isinstance(error, MirrorSyncError):
            remember(error)
            return
        remember(MirrorSyncError(f"{label}: {error}"))

    def close_control(binding: ControlObjectBinding, label: str) -> None:
        if binding.fd < 0:
            return
        fd = binding.fd
        if fd in closed_fds:
            binding.fd = -1
            return
        closed_fds.add(fd)
        try:
            os.close(fd)
        except OSError as error:
            remember(MirrorSyncError(f"cannot close {label}: {error}"))
        finally:
            # A failed close has unspecified descriptor state and must not be
            # retried after the descriptor number may have been reused.
            binding.fd = -1

    if root.git_control is not None:
        git_control = root.git_control
        try:
            _cleanup_private_git_control(
                root,
                git_control.private_parent,
                git_control.private,
                git_control.private_name,
                git_control.private_cleanup_manifest,
                git_control.owner_record,
                git_control.owner_record_name,
                git_control.owner_nonce,
            )
        except (MirrorSyncError, OSError) as error:
            remember_terminal_error("private Git control cleanup failed", error)
        for binding in (
            git_control.marker,
            git_control.admin,
            git_control.commondir_file,
            git_control.common,
            git_control.objects,
            *git_control.source_files,
            *git_control.source_directories,
            git_control.private_objects,
            git_control.private_git_executable,
            git_control.private,
            git_control.owner_record,
        ):
            if binding is not None:
                close_control(binding, binding.label)
        try:
            _close_private_tool_root(git_control.private_parent)
        except (MirrorSyncError, OSError) as error:
            remember_terminal_error("private Git control context close failed", error)
        root.git_control = None
    if root.private_git_executable is not None:
        binding = root.private_git_executable
        try:
            _cleanup_private_git_control(
                root,
                binding.private_parent,
                binding.private,
                binding.private_name,
                binding.private_manifest,
                binding.owner_record,
                binding.owner_record_name,
                binding.owner_nonce,
            )
        except (MirrorSyncError, OSError) as error:
            remember_terminal_error("private Git executable cleanup failed", error)
        for control in (
            binding.executable,
            binding.private,
            binding.owner_record,
        ):
            close_control(control, control.label)
        try:
            _close_private_tool_root(binding.private_parent)
        except (MirrorSyncError, OSError) as error:
            remember_terminal_error(
                "private Git executable context close failed",
                error,
            )
        root.private_git_executable = None
    for binding in root.managed_ancestors.values():
        close_control(binding, binding.label)
    root.managed_ancestors.clear()
    root.managed_ancestor_paths.clear()
    close_control(root.git_executable, root.git_executable.label)
    if root.fd >= 0:
        root_fd = root.fd
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        except OSError as error:
            remember(MirrorSyncError(f"cannot release bound root lock: {error}"))
        try:
            os.close(root_fd)
        except OSError as error:
            remember(MirrorSyncError(f"cannot close bound root descriptor: {error}"))
        finally:
            root.fd = -1
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        first_error = cleanup_errors[0]
        raise MirrorSyncError(
            f"{first_error}; secondary root cleanup failures: "
            + "; ".join(str(error) for error in cleanup_errors[1:])
        ) from first_error


def _finish_bound_roots(*roots: BoundRoot) -> None:
    errors: list[tuple[str, MirrorSyncError]] = []

    def normalized_terminal_error(
        label: str,
        error: MirrorSyncError | OSError,
    ) -> MirrorSyncError:
        if isinstance(error, MirrorSyncError):
            return error
        return MirrorSyncError(f"{label}: {error}")

    for root in roots:
        try:
            _revalidate_bound_root(root)
        except (MirrorSyncError, OSError) as error:
            errors.append(
                (
                    f"revalidate {root.path}",
                    normalized_terminal_error(
                        "bound-root revalidation failed",
                        error,
                    ),
                )
            )
    for root in reversed(roots):
        try:
            _close_bound_root(root)
        except (MirrorSyncError, OSError) as error:
            errors.append(
                (
                    f"close {root.path}",
                    normalized_terminal_error(
                        "bound-root close failed",
                        error,
                    ),
                )
            )
    if len(errors) == 1:
        raise errors[0][1]
    if errors:
        first_label, first_error = errors[0]
        raise MirrorSyncError(
            f"{first_label}: {first_error}; secondary bound-root "
            "finalization failures: "
            + "; ".join(f"{label}: {error}" for label, error in errors[1:])
        ) from first_error


def _root_path(root: Root) -> Path:
    return root.path if isinstance(root, BoundRoot) else Path(root)


def _borrow_root_fd(root: Root) -> tuple[int, bool]:
    if isinstance(root, BoundRoot):
        if root.fd < 0:
            raise MirrorSyncError(f"bound root is already closed: {root.path}")
        _revalidate_bound_root(root)
        return root.fd, False
    return _open_root(Path(root)), True


def _directory_is_at_or_below(
    candidate_fd: int,
    ancestor_identity: tuple[int, int, int],
) -> bool:
    current_fd = os.dup(candidate_fd)
    result: bool | None = None
    primary_error: BaseException | None = None
    try:
        for _depth in range(MAX_ROOT_ANCESTOR_DEPTH):
            current_identity = _object_identity(os.fstat(current_fd))
            if current_identity == ancestor_identity:
                result = True
                break
            parent_fd = -1
            try:
                parent_fd = os.open("..", _DIRECTORY_FLAGS, dir_fd=current_fd)
                parent_identity = _object_identity(os.fstat(parent_fd))
            except OSError as error:
                close_errors = _close_raw_descriptors_best_effort(
                    (("unadopted ancestry parent", parent_fd),)
                )
                _raise_with_secondary_cleanup(
                    MirrorSyncError(f"cannot validate target root ancestry: {error}"),
                    "ancestry parent cleanup failures",
                    close_errors,
                )
            previous_fd = current_fd
            current_fd = parent_fd
            parent_fd = -1
            close_errors = _close_raw_descriptors_best_effort(
                (("previous ancestry directory", previous_fd),)
            )
            if close_errors:
                raise MirrorSyncError("; ".join(close_errors))
            if parent_identity == current_identity:
                result = False
                break
        else:
            raise MirrorSyncError(
                f"target root ancestry exceeds {MAX_ROOT_ANCESTOR_DEPTH} directories"
            )
    except BaseException as error:
        primary_error = error
    close_errors = _close_raw_descriptors_best_effort(
        (("current ancestry directory", current_fd),)
    )
    current_fd = -1
    if primary_error is not None:
        _raise_with_secondary_cleanup(
            primary_error,
            "ancestry cleanup failures",
            close_errors,
        )
    if close_errors:
        raise MirrorSyncError("; ".join(close_errors))
    assert result is not None
    return result


def _reject_canonical_target(
    repository_root: Root,
    target_root: Root,
) -> None:
    canonical_fd, close_canonical = _borrow_root_fd(repository_root)
    target_fd, close_target = _borrow_root_fd(target_root)
    try:
        canonical_identity = _object_identity(os.fstat(canonical_fd))
        target_identity = _object_identity(os.fstat(target_fd))
        if _directory_is_at_or_below(
            target_fd, canonical_identity
        ) or _directory_is_at_or_below(canonical_fd, target_identity):
            raise MirrorSyncError(
                "consumer target root must not be the canonical repository "
                "or overlap its directory ancestry"
            )
    finally:
        if close_target:
            os.close(target_fd)
        if close_canonical:
            os.close(canonical_fd)


def _open_directory_component(
    parent_fd: int,
    component: str,
    *,
    create: bool,
    display_path: PurePosixPath,
) -> int:
    try:
        path_metadata = os.stat(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if not create:
            raise MissingPathError(f"directory is missing: {display_path}")
        try:
            os.mkdir(component, mode=0o755, dir_fd=parent_fd)
            os.fsync(parent_fd)
            path_metadata = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot safely create directory {display_path}: {error}"
            ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inspect directory {display_path}: {error}"
        ) from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise MirrorSyncError(
            f"directory ancestor must not be a symlink: {display_path}"
        )
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise MirrorSyncError(f"directory ancestor is not a directory: {display_path}")
    try:
        child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot safely open directory {display_path}: {error}"
        ) from error
    opened_metadata = os.fstat(child_fd)
    if _object_identity(path_metadata) != _object_identity(opened_metadata):
        os.close(child_fd)
        raise MirrorSyncError(
            f"directory ancestor was replaced while opening it: {display_path}"
        )
    return child_fd


def _register_managed_ancestor(
    root: BoundRoot,
    relative_path: PurePosixPath,
    directory_fd: int,
) -> None:
    if relative_path not in root.managed_ancestor_paths:
        return
    existing = root.managed_ancestors.get(relative_path)
    metadata = os.fstat(directory_fd)
    if existing is not None:
        if _object_identity(metadata) != existing.identity:
            raise MirrorSyncError(
                f"managed ancestor was replaced during transaction: {relative_path}"
            )
        if _access_policy(metadata) != existing.access_policy:
            raise MirrorSyncError(
                f"managed ancestor access policy changed during transaction: "
                f"{relative_path}"
            )
        return
    descriptor = os.dup(directory_fd)
    root.managed_ancestors[relative_path] = ControlObjectBinding(
        label=f"managed ancestor {relative_path}",
        path=None,
        relative_path=relative_path,
        fd=descriptor,
        identity=_object_identity(metadata),
        access_policy=_access_policy(metadata),
        content_digest=None,
    )


def _managed_parent_paths(
    paths: list[PurePosixPath],
) -> set[PurePosixPath]:
    parents: set[PurePosixPath] = set()
    for path in paths:
        current = PurePosixPath()
        for component in path.parts[:-1]:
            current /= component
            parents.add(current)
    return parents


def _configure_managed_ancestors(
    root: BoundRoot,
    paths: list[PurePosixPath],
) -> None:
    expected = _managed_parent_paths(paths)
    if root.managed_ancestor_paths and root.managed_ancestor_paths != expected:
        raise MirrorSyncError("managed ancestor set changed during transaction")
    root.managed_ancestor_paths = expected
    for path in sorted(paths, key=PurePosixPath.as_posix):
        parent_fd = -1
        try:
            parent_fd = _open_parent_directory(
                root.fd,
                path,
                create=False,
                bound_root=root,
            )
        except MissingPathError:
            continue
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
    _revalidate_bound_root(root)


def _open_parent_directory(
    root_fd: int,
    relative_path: PurePosixPath,
    *,
    create: bool,
    bound_root: BoundRoot | None = None,
) -> int:
    current_fd = os.dup(root_fd)
    traversed = PurePosixPath()
    try:
        for component in relative_path.parts[:-1]:
            traversed /= component
            next_fd = _open_directory_component(
                current_fd,
                component,
                create=create,
                display_path=traversed,
            )
            if bound_root is not None:
                _register_managed_ancestor(
                    bound_root,
                    traversed,
                    next_fd,
                )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_all(file_fd: int, display_path: PurePosixPath) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(file_fd, min(1024 * 1024, MAX_SOURCE_BYTES + 1 - total))
        except OSError as error:
            raise MirrorSyncError(f"cannot read {display_path}: {error}") from error
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise MirrorSyncError(
                f"file exceeds the {MAX_SOURCE_BYTES}-byte source limit: {display_path}"
            )


def _access_policy(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _require_file_stability(
    expected: os.stat_result,
    observed: os.stat_result,
    display_path: PurePosixPath,
) -> None:
    if _file_stability(expected) == _file_stability(observed):
        return
    if _object_identity(expected) != _object_identity(observed):
        raise MirrorSyncError(f"file was replaced while reading it: {display_path}")
    if _access_policy(expected) != _access_policy(observed):
        raise MirrorSyncError(
            f"file access policy changed while reading it: {display_path}"
        )
    raise MirrorSyncError(f"file content size changed while reading it: {display_path}")


def _complete_file_read(
    file_fd: int,
    display_path: PurePosixPath,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(file_fd)
    if before.st_size > MAX_SOURCE_BYTES:
        raise MirrorSyncError(
            f"file exceeds the {MAX_SOURCE_BYTES}-byte source limit: {display_path}"
        )
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError as error:
        raise MirrorSyncError(f"cannot rewind {display_path}: {error}") from error
    payload = _read_all(file_fd, display_path)
    after = os.fstat(file_fd)
    _require_file_stability(before, after, display_path)
    if len(payload) != after.st_size:
        raise MirrorSyncError(
            f"file byte count changed while reading it: {display_path}"
        )
    return payload, after


def _safe_read_leaf_snapshot(
    parent_fd: int,
    name: str,
    display_path: PurePosixPath,
) -> FileSnapshot:
    file_fd = -1
    try:
        try:
            path_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise MissingPathError(f"file is missing: {display_path}") from error
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inspect file {display_path}: {error}"
            ) from error
        if stat.S_ISLNK(path_metadata.st_mode):
            raise MirrorSyncError(f"file target must not be a symlink: {display_path}")
        if not stat.S_ISREG(path_metadata.st_mode):
            raise MirrorSyncError(f"path is not a regular file: {display_path}")
        if path_metadata.st_size > MAX_SOURCE_BYTES:
            raise MirrorSyncError(
                f"file exceeds the {MAX_SOURCE_BYTES}-byte source limit: {display_path}"
            )
        try:
            file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot safely open file {display_path}: {error}"
            ) from error
        opened_metadata = os.fstat(file_fd)
        if _object_identity(path_metadata) != _object_identity(opened_metadata):
            raise MirrorSyncError(f"file was replaced while opening it: {display_path}")
        _require_file_stability(path_metadata, opened_metadata, display_path)
        first_payload, first_metadata = _complete_file_read(
            file_fd,
            display_path,
        )
        second_payload, final_metadata = _complete_file_read(
            file_fd,
            display_path,
        )
        _require_file_stability(opened_metadata, first_metadata, display_path)
        _require_file_stability(first_metadata, final_metadata, display_path)
        if (
            len(first_payload) != len(second_payload)
            or hashlib.sha256(first_payload).digest()
            != hashlib.sha256(second_payload).digest()
            or first_payload != second_payload
        ):
            raise MirrorSyncError(
                f"file content changed while reading it: {display_path}"
            )
        try:
            final_path_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise MirrorSyncError(
                f"file was removed while reading it: {display_path}"
            ) from error
        except OSError as error:
            raise MirrorSyncError(
                f"cannot revalidate file {display_path}: {error}"
            ) from error
        if stat.S_ISLNK(final_path_metadata.st_mode):
            raise MirrorSyncError(
                f"file was replaced by a symlink while reading it: {display_path}"
            )
        _require_file_stability(
            final_metadata,
            final_path_metadata,
            display_path,
        )
        return FileSnapshot(
            payload=first_payload,
            mode=stat.S_IMODE(final_metadata.st_mode),
            identity=_object_identity(final_metadata),
            access_policy=_access_policy(final_metadata),
            size=final_metadata.st_size,
        )
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def _safe_read_private_owner_record_snapshot(
    parent_fd: int,
    name: str,
    display_path: PurePosixPath,
    *,
    file_fd: int | None = None,
    operation: OperationBudget | None = None,
) -> FileSnapshot:
    """Read one owner record twice with a hard 4096+1 producer ceiling."""

    owned_fd = -1
    try:
        try:
            path_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise MissingPathError(f"file is missing: {display_path}") from error
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inspect private owner record {display_path}: {error}"
            ) from error
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(
            path_metadata.st_mode
        ):
            raise MirrorSyncError(
                f"private owner record must be a non-symlink regular file: "
                f"{display_path}"
            )
        if path_metadata.st_size > MAX_PRIVATE_OWNER_RECORD_BYTES:
            raise MirrorSyncError(
                f"private owner record exceeds {MAX_PRIVATE_OWNER_RECORD_BYTES} "
                f"bytes: {display_path}"
            )
        if file_fd is None:
            try:
                owned_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot safely open private owner record {display_path}: {error}"
                ) from error
            file_fd = owned_fd
        opened_metadata = os.fstat(file_fd)
        if _object_identity(path_metadata) != _object_identity(opened_metadata):
            raise MirrorSyncError(
                f"private owner record was replaced while opening it: {display_path}"
            )
        _require_file_stability(path_metadata, opened_metadata, display_path)

        def read_once() -> bytes:
            _operation_checkpoint(
                operation, f"reading private owner record {display_path}"
            )
            try:
                os.lseek(file_fd, 0, os.SEEK_SET)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot rewind private owner record {display_path}: {error}"
                ) from error
            payload = bytearray()
            while len(payload) <= MAX_PRIVATE_OWNER_RECORD_BYTES:
                _operation_checkpoint(
                    operation,
                    f"reading private owner record {display_path}",
                )
                try:
                    chunk = os.read(
                        file_fd,
                        min(
                            4096,
                            MAX_PRIVATE_OWNER_RECORD_BYTES + 1 - len(payload),
                        ),
                    )
                except OSError as error:
                    raise MirrorSyncError(
                        f"cannot read private owner record {display_path}: {error}"
                    ) from error
                if not chunk:
                    break
                _consume_operation_budget(
                    operation,
                    byte_count=len(chunk),
                    label=f"reading private owner record {display_path}",
                )
                payload.extend(chunk)
            if len(payload) > MAX_PRIVATE_OWNER_RECORD_BYTES:
                raise MirrorSyncError(
                    f"private owner record exceeds "
                    f"{MAX_PRIVATE_OWNER_RECORD_BYTES} bytes: {display_path}"
                )
            return bytes(payload)

        first_payload = read_once()
        first_metadata = os.fstat(file_fd)
        second_payload = read_once()
        final_metadata = os.fstat(file_fd)
        _require_file_stability(opened_metadata, first_metadata, display_path)
        _require_file_stability(first_metadata, final_metadata, display_path)
        if first_payload != second_payload:
            raise MirrorSyncError(
                f"private owner record content changed while reading it: {display_path}"
            )
        try:
            final_path_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise MirrorSyncError(
                f"cannot revalidate private owner record {display_path}: {error}"
            ) from error
        _require_file_stability(final_metadata, final_path_metadata, display_path)
        return FileSnapshot(
            payload=first_payload,
            mode=stat.S_IMODE(final_metadata.st_mode),
            identity=_object_identity(final_metadata),
            access_policy=_access_policy(final_metadata),
            size=final_metadata.st_size,
        )
    finally:
        if owned_fd >= 0:
            os.close(owned_fd)


def _safe_read_snapshot(
    root: Root,
    relative_path: PurePosixPath,
) -> FileSnapshot:
    root_fd, close_root = _borrow_root_fd(root)
    parent_fd = -1
    try:
        parent_fd = _open_parent_directory(
            root_fd,
            relative_path,
            create=False,
            bound_root=root if isinstance(root, BoundRoot) else None,
        )
        snapshot = _safe_read_leaf_snapshot(
            parent_fd,
            relative_path.name,
            relative_path,
        )
        if isinstance(root, BoundRoot):
            _consume_operation_budget(
                root.operation,
                byte_count=snapshot.size,
                entry_count=1,
                label=f"reading {relative_path}",
            )
        return snapshot
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        if close_root:
            os.close(root_fd)
        elif isinstance(root, BoundRoot):
            _revalidate_bound_root(root)


def _safe_read_relative(
    root: Root,
    relative_path: PurePosixPath,
) -> tuple[bytes, int]:
    snapshot = _safe_read_snapshot(root, relative_path)
    return snapshot.payload, snapshot.mode


def _optional_safe_read_snapshot(
    root: Root,
    relative_path: PurePosixPath,
) -> FileSnapshot | None:
    try:
        return _safe_read_snapshot(root, relative_path)
    except MissingPathError:
        return None


def _validate_target_leaf(parent_fd: int, relative_path: PurePosixPath) -> None:
    try:
        metadata = os.stat(
            relative_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inspect target {relative_path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise MirrorSyncError(f"file target must not be a symlink: {relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise MirrorSyncError(f"existing target is not a regular file: {relative_path}")


def _write_all(file_fd: int, payload: bytes, display_path: PurePosixPath) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(file_fd, payload[offset:])
        except OSError as error:
            raise MirrorSyncError(f"cannot write {display_path}: {error}") from error
        if written <= 0:
            raise MirrorSyncError(f"short write while generating {display_path}")
        offset += written


def _exchange_directory_entries(
    directory_fd: int,
    left_name: str,
    right_name: str,
) -> None:
    left = os.fsencode(left_name)
    right = os.fsencode(right_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename_exchange = libc.renameatx_np
        except AttributeError as error:
            raise MirrorSyncError(
                "this platform cannot atomically exchange present targets"
            ) from error
    elif sys.platform.startswith("linux"):
        try:
            rename_exchange = libc.renameat2
        except AttributeError as error:
            raise MirrorSyncError(
                "this platform cannot atomically exchange present targets"
            ) from error
    else:
        raise MirrorSyncError(
            "this platform cannot atomically exchange present targets"
        )
    rename_exchange.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_exchange.restype = ctypes.c_int
    result = rename_exchange(
        directory_fd,
        left,
        directory_fd,
        right,
        0x00000002,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{left_name} <-> {right_name}",
        )


def _rename_directory_entry_noreplace(
    directory_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    _rename_directory_entry_noreplace_between(
        directory_fd,
        source_name,
        directory_fd,
        destination_name,
    )


def _rename_directory_entry_noreplace_between(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename_noreplace = libc.renameatx_np
        except AttributeError as error:
            raise MirrorSyncError(
                "this platform cannot isolate entries without replacement"
            ) from error
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        try:
            rename_noreplace = libc.renameat2
        except AttributeError as error:
            raise MirrorSyncError(
                "this platform cannot isolate entries without replacement"
            ) from error
        flags = 0x00000001
    else:
        raise MirrorSyncError(
            "this platform cannot isolate entries without replacement"
        )
    rename_noreplace.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_noreplace.restype = ctypes.c_int
    result = rename_noreplace(
        source_directory_fd,
        source,
        destination_directory_fd,
        destination,
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{source_name} -> {destination_name}",
        )


def _quarantine_file_name(
    expected: FileSnapshot,
    retention_kind: str,
) -> str:
    if retention_kind not in {
        QUARANTINE_RECOVERY_KIND,
        QUARANTINE_TRANSIENT_KIND,
    }:
        raise MirrorSyncError(
            f"unsupported durable quarantine retention kind: {retention_kind}"
        )
    digest = hashlib.sha256(expected.payload).hexdigest()[:16]
    return (
        f"{retention_kind}-file-{expected.identity[0]:x}-"
        f"{expected.identity[1]:x}-{expected.access_policy[0]:04o}-"
        f"{expected.access_policy[1]:x}-{expected.access_policy[2]:x}-"
        f"{digest}-{secrets.token_hex(16)}"
    )


def _quarantine_file_name_matches(
    name: str,
    expected: FileSnapshot,
    retention_kind: str,
) -> bool:
    matched = QUARANTINE_FILE_RE.fullmatch(name)
    if matched is None or matched.group(1) != retention_kind:
        return False
    return (
        int(matched.group(2), 16) == expected.identity[0]
        and int(matched.group(3), 16) == expected.identity[1]
        and int(matched.group(4), 8) == expected.access_policy[0]
        and int(matched.group(5), 16) == expected.access_policy[1]
        and int(matched.group(6), 16) == expected.access_policy[2]
        and matched.group(7) == hashlib.sha256(expected.payload).hexdigest()[:16]
    )


def _durable_quarantine_entry_count(quarantine_fd: int) -> int:
    try:
        with os.scandir(quarantine_fd) as entries:
            count = 0
            for count, _entry in enumerate(entries, start=1):
                if count > MAX_DURABLE_QUARANTINE_ENTRIES:
                    raise MirrorSyncError(
                        "durable quarantine root exceeds its bounded entry "
                        "limit; manual identity-aware cleanup is required"
                    )
            return count
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inventory durable quarantine capacity: {error}"
        ) from error


def _require_new_durable_quarantine_entry(
    quarantine_fd: int,
    quarantine_name: str,
) -> None:
    try:
        os.stat(
            quarantine_name,
            dir_fd=quarantine_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    except OSError as error:
        raise MirrorSyncError(
            "cannot verify durable quarantine destination absence: "
            f"{quarantine_name}: {error}"
        ) from error
    else:
        raise MirrorSyncError(
            "durable quarantine destination already exists; preserving the "
            f"source artifact: {quarantine_name}"
        )
    if _durable_quarantine_entry_count(quarantine_fd) >= MAX_DURABLE_QUARANTINE_ENTRIES:
        raise MirrorSyncError(
            "durable quarantine root reached its bounded entry limit; "
            "preserving the source artifact for identity-aware recovery"
        )


def _bind_durable_quarantine_root(
    root: Root,
    *,
    quarantine_parent: Path | None = None,
    source_parent_fd: int | None = None,
    create: bool = True,
) -> ControlObjectBinding:
    root_path = _root_path(root)
    parent_path = (
        root_path.parent
        if quarantine_parent is None
        else Path(os.path.abspath(quarantine_parent))
    )
    parent = _bind_absolute_control_object(
        parent_path,
        "durable quarantine parent",
        require_directory=True,
    )
    quarantine_fd = -1
    try:
        if create:
            try:
                os.mkdir(
                    DURABLE_QUARANTINE_ROOT_NAME,
                    0o700,
                    dir_fd=parent.fd,
                )
                os.fsync(parent.fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot create durable quarantine root: {error}"
                ) from error
        else:
            try:
                os.stat(
                    DURABLE_QUARANTINE_ROOT_NAME,
                    dir_fd=parent.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                raise MirrorSyncError(
                    f"{PRIVATE_CONTROL_REASON_LEGACY_PENDING}: legacy durable "
                    "quarantine is absent and allocation is forbidden"
                ) from error
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot inspect non-allocating legacy durable quarantine: {error}"
                ) from error
        path_metadata = os.stat(
            DURABLE_QUARANTINE_ROOT_NAME,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(path_metadata.st_mode):
            raise MirrorSyncError("durable quarantine root must be a directory")
        quarantine_fd = os.open(
            DURABLE_QUARANTINE_ROOT_NAME,
            _DIRECTORY_FLAGS,
            dir_fd=parent.fd,
        )
        opened_metadata = os.fstat(quarantine_fd)
        if _object_identity(path_metadata) != _object_identity(
            opened_metadata
        ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
            raise MirrorSyncError(
                "durable quarantine root was replaced while binding it"
            )
        if (
            stat.S_IMODE(opened_metadata.st_mode) != 0o700
            or opened_metadata.st_uid != os.geteuid()
        ):
            raise MirrorSyncError(
                "durable quarantine root must be mode 0700 and owned by the current uid"
            )
        if isinstance(root, BoundRoot):
            # Quarantine placement protects only the target root directory's
            # identity/access policy and the non-overlap relationship. Cleanup
            # may intentionally follow a previously detected Git-control
            # content-stability failure, so it must not reaccept or recursively
            # revalidate that invalidated control plane merely to bind a
            # separate durable destination.
            _revalidate_bound_root_directory(root)
            root_fd = root.fd
            close_root = False
        else:
            root_fd = _open_root(Path(root))
            close_root = True
        try:
            if _directory_bindings_overlap(quarantine_fd, root_fd):
                raise MirrorSyncError(
                    "durable quarantine root overlaps the target repository"
                )
            if source_parent_fd is not None:
                source_metadata = os.fstat(source_parent_fd)
                if source_metadata.st_dev != opened_metadata.st_dev:
                    raise MirrorSyncError(
                        "durable quarantine root and source directory are on "
                        "different filesystems"
                    )
                if _directory_bindings_overlap(
                    quarantine_fd,
                    source_parent_fd,
                ):
                    raise MirrorSyncError(
                        "durable quarantine root overlaps the source directory"
                    )
        finally:
            if close_root:
                os.close(root_fd)
        _durable_quarantine_entry_count(quarantine_fd)
        quarantine_path = parent_path / DURABLE_QUARANTINE_ROOT_NAME
        binding = ControlObjectBinding(
            label="durable mirror quarantine root",
            path=quarantine_path,
            relative_path=None,
            fd=quarantine_fd,
            identity=_object_identity(opened_metadata),
            access_policy=_access_policy(opened_metadata),
            content_digest=None,
        )
        quarantine_fd = -1
        return binding
    except OSError as error:
        raise MirrorSyncError(
            f"cannot bind durable quarantine root: {error}"
        ) from error
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)
        os.close(parent.fd)


def _revalidate_durable_quarantine_root(
    quarantine: ControlObjectBinding,
) -> None:
    assert quarantine.path is not None
    try:
        path_metadata = os.stat(quarantine.path, follow_symlinks=False)
        descriptor_metadata = os.fstat(quarantine.fd)
    except OSError as error:
        raise MirrorSyncError(
            f"durable quarantine root became unavailable: {error}"
        ) from error
    if (
        _object_identity(path_metadata) != quarantine.identity
        or _object_identity(descriptor_metadata) != quarantine.identity
    ):
        raise MirrorSyncError("durable quarantine root was replaced")
    if (
        _access_policy(path_metadata) != quarantine.access_policy
        or _access_policy(descriptor_metadata) != quarantine.access_policy
    ):
        raise MirrorSyncError("durable quarantine root access policy changed")


def _persist_quarantined_file(
    quarantine: ControlObjectBinding,
    source_parent_fd: int,
    source_name: str,
    expected: FileSnapshot,
    display_path: PurePosixPath,
    *,
    retention_kind: str,
    quarantine_name: str | None = None,
    quarantine_locked: bool = False,
    capacity_checked: bool = False,
) -> Path:
    _revalidate_durable_quarantine_root(quarantine)
    if quarantine_name is None:
        quarantine_name = _quarantine_file_name(expected, retention_kind)
    elif not _quarantine_file_name_matches(
        quarantine_name,
        expected,
        retention_kind,
    ):
        raise MirrorSyncError(
            "preallocated durable quarantine name does not bind the expected "
            f"object/content: {display_path}"
        )
    assert quarantine.path is not None
    quarantine_path = quarantine.path / quarantine_name
    if not quarantine_locked:
        fcntl.flock(quarantine.fd, fcntl.LOCK_EX)
    try:
        if not capacity_checked:
            _require_new_durable_quarantine_entry(
                quarantine.fd,
                quarantine_name,
            )
        try:
            _rename_directory_entry_noreplace_between(
                source_parent_fd,
                source_name,
                quarantine.fd,
                quarantine_name,
            )
            os.fsync(source_parent_fd)
            os.fsync(quarantine.fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot persist isolated file in durable quarantine; "
                f"preserving {source_name}: {display_path}: {error}"
            ) from error
        moved = _safe_read_leaf_snapshot(
            quarantine.fd,
            quarantine_name,
            PurePosixPath(quarantine_name),
        )
        if moved != expected:
            raise MirrorSyncError(
                "isolated file changed before durable quarantine; "
                f"preserving evidence at {quarantine_path}: {display_path}"
            )
    finally:
        if not quarantine_locked:
            fcntl.flock(quarantine.fd, fcntl.LOCK_UN)
    _revalidate_durable_quarantine_root(quarantine)
    return quarantine_path


def _isolate_and_remove_file(
    root: Root,
    parent_fd: int,
    name: str,
    expected: FileSnapshot,
    display_path: PurePosixPath,
    *,
    quarantine: ControlObjectBinding | None = None,
    retention_kind: str = QUARANTINE_RECOVERY_KIND,
    quarantine_locked: bool = False,
) -> Path:
    # Bind and validate the durable destination before changing the source
    # pathname. A later bind through a BoundRoot would recursively revalidate
    # control paths, including an owner record that this operation has already
    # isolated.
    if quarantine_locked and quarantine is None:
        raise MirrorSyncError(
            "a caller-held durable quarantine lock requires a bound quarantine"
        )
    durable_quarantine = (
        quarantine
        if quarantine is not None
        else _bind_durable_quarantine_root(
            root,
            source_parent_fd=parent_fd,
        )
    )
    close_quarantine = quarantine is None
    source_metadata = os.fstat(parent_fd)
    quarantine_metadata = os.fstat(durable_quarantine.fd)
    if source_metadata.st_dev != quarantine_metadata.st_dev:
        if close_quarantine:
            os.close(durable_quarantine.fd)
        raise MirrorSyncError(
            "durable quarantine root and source directory are on different filesystems"
        )
    quarantine_name = _quarantine_file_name(expected, retention_kind)
    isolated_name = f".sync-remove-{os.getpid()}-{secrets.token_hex(16)}"
    acquired_quarantine_lock = False
    try:
        if not quarantine_locked:
            fcntl.flock(durable_quarantine.fd, fcntl.LOCK_EX)
            acquired_quarantine_lock = True
        _require_new_durable_quarantine_entry(
            durable_quarantine.fd,
            quarantine_name,
        )
        try:
            _rename_directory_entry_noreplace(
                parent_fd,
                name,
                isolated_name,
            )
            os.fsync(parent_fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot isolate recovery artifact {display_path}: {error}"
            ) from error
        try:
            moved = _safe_read_leaf_snapshot(
                parent_fd,
                isolated_name,
                display_path,
            )
        except MirrorSyncError as error:
            raise MirrorSyncError(
                f"isolated recovery artifact is unreadable; preserving "
                f"{isolated_name}: {display_path}: {error}"
            ) from error
        if moved != expected:
            try:
                _rename_directory_entry_noreplace(
                    parent_fd,
                    isolated_name,
                    name,
                )
                os.fsync(parent_fd)
            except OSError as restore_error:
                raise MirrorSyncError(
                    f"recovery artifact changed during isolation; preserving "
                    f"{isolated_name}: {display_path}: {restore_error}"
                ) from restore_error
            restored = _safe_read_leaf_snapshot(parent_fd, name, display_path)
            if restored != moved:
                raise MirrorSyncError(
                    f"recovery artifact changed while restoring it: {display_path}"
                )
            raise MirrorSyncError(
                f"recovery artifact changed before cleanup: {display_path}"
            )
        return _persist_quarantined_file(
            durable_quarantine,
            parent_fd,
            isolated_name,
            expected,
            display_path,
            retention_kind=retention_kind,
            quarantine_name=quarantine_name,
            quarantine_locked=True,
            capacity_checked=True,
        )
    finally:
        if acquired_quarantine_lock:
            fcntl.flock(durable_quarantine.fd, fcntl.LOCK_UN)
        if close_quarantine:
            os.close(durable_quarantine.fd)


def _restore_exchanged_target(
    root: Root,
    parent_fd: int,
    temporary_name: str,
    relative_path: PurePosixPath,
    displaced_snapshot: FileSnapshot | None,
) -> None:
    try:
        _exchange_directory_entries(
            parent_fd,
            temporary_name,
            relative_path.name,
        )
    except (MirrorSyncError, OSError) as error:
        raise MirrorSyncError(
            f"cannot restore concurrently changed target {relative_path}: {error}"
        ) from error
    if displaced_snapshot is not None:
        restored = _safe_read_snapshot(root, relative_path)
        if restored != displaced_snapshot:
            raise MirrorSyncError(
                f"restored target changed during recovery: {relative_path}"
            )


def _exchange_journal_name(relative_path: PurePosixPath) -> str:
    return f".{relative_path.name}.sync-exchange.json"


def _snapshot_record(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(snapshot.payload).hexdigest(),
        "mode": snapshot.mode,
        "identity": list(snapshot.identity),
        "access_policy": list(snapshot.access_policy),
        "size": snapshot.size,
    }


def _snapshot_matches_record(
    snapshot: FileSnapshot | None,
    record: object,
) -> bool:
    if snapshot is None or not isinstance(record, dict):
        return False
    expected = _snapshot_record(snapshot)
    return record == expected


def _write_exchange_journal(
    parent_fd: int,
    relative_path: PurePosixPath,
    temporary_name: str,
    expected_snapshot: FileSnapshot | None,
    replacement_snapshot: FileSnapshot | None,
    *,
    removal_quarantine_name: str | None = None,
) -> tuple[str, FileSnapshot]:
    journal_name = _exchange_journal_name(relative_path)
    removing_target = replacement_snapshot is None
    if removing_target:
        if (
            expected_snapshot is None
            or removal_quarantine_name is None
            or not _quarantine_file_name_matches(
                removal_quarantine_name,
                expected_snapshot,
                QUARANTINE_RECOVERY_KIND,
            )
        ):
            raise MirrorSyncError(
                f"exchange removal journal requires exact durable quarantine "
                f"evidence: {relative_path}"
            )
    elif removal_quarantine_name is not None:
        raise MirrorSyncError(
            f"exchange write journal must not name removal quarantine evidence: "
            f"{relative_path}"
        )
    document = {
        "version": 2 if removing_target else 1,
        "target_path": relative_path.as_posix(),
        "temporary_name": temporary_name,
        "expected": (
            None if expected_snapshot is None else _snapshot_record(expected_snapshot)
        ),
        "replacement": (
            None
            if replacement_snapshot is None
            else _snapshot_record(replacement_snapshot)
        ),
    }
    if removing_target:
        assert expected_snapshot is not None
        document["removal_quarantine"] = {
            "name": removal_quarantine_name,
            "expected": _snapshot_record(expected_snapshot),
            "phase_contract": "matching-durable-entry-proves-removal",
        }
    payload = (
        json.dumps(
            document,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    try:
        journal_fd = os.open(
            journal_name,
            _FILE_WRITE_FLAGS,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError as error:
        raise MirrorSyncError(
            f"exchange recovery journal already exists: {relative_path}"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"cannot create exchange recovery journal for {relative_path}: {error}"
        ) from error
    try:
        os.fchmod(journal_fd, 0o600)
        _write_all(journal_fd, payload, relative_path)
        os.fsync(journal_fd)
        journal_metadata = os.fstat(journal_fd)
        journal_snapshot = FileSnapshot(
            payload=payload,
            mode=stat.S_IMODE(journal_metadata.st_mode),
            identity=_object_identity(journal_metadata),
            access_policy=_access_policy(journal_metadata),
            size=journal_metadata.st_size,
        )
    finally:
        os.close(journal_fd)
    os.fsync(parent_fd)
    return journal_name, journal_snapshot


def _require_exact_removal_quarantine_evidence(
    quarantine: ControlObjectBinding,
    quarantine_name: str,
    expected: FileSnapshot,
    relative_path: PurePosixPath,
    phase: str,
) -> FileSnapshot:
    _revalidate_durable_quarantine_root(quarantine)
    try:
        observed = _safe_read_leaf_snapshot(
            quarantine.fd,
            quarantine_name,
            PurePosixPath(quarantine_name),
        )
    except MissingPathError as error:
        raise MirrorSyncError(
            "exchange removal durable quarantine evidence is missing "
            f"{phase}; preserving recovery artifacts: {relative_path}"
        ) from error
    if observed != expected or not _quarantine_file_name_matches(
        quarantine_name,
        observed,
        QUARANTINE_RECOVERY_KIND,
    ):
        raise MirrorSyncError(
            "exchange removal durable quarantine evidence changed "
            f"{phase}; preserving recovery artifacts: {relative_path}"
        )
    _revalidate_durable_quarantine_root(quarantine)
    return observed


def _remove_exchange_artifact(
    root: Root,
    parent_fd: int,
    name: str,
    relative_path: PurePosixPath,
    expected: FileSnapshot,
    *,
    quarantine: ControlObjectBinding | None = None,
    quarantine_locked: bool = False,
    required_removal_evidence: tuple[str, FileSnapshot] | None = None,
) -> Path | None:
    if required_removal_evidence is not None and (
        quarantine is None or not quarantine_locked
    ):
        raise MirrorSyncError(
            "exchange removal evidence cleanup requires the caller-held "
            f"durable quarantine lock: {relative_path}"
        )
    if required_removal_evidence is not None:
        assert quarantine is not None
        evidence_name, evidence_snapshot = required_removal_evidence
        _require_exact_removal_quarantine_evidence(
            quarantine,
            evidence_name,
            evidence_snapshot,
            relative_path,
            "immediately before journal cleanup",
        )
    try:
        retained_path = _isolate_and_remove_file(
            root,
            parent_fd,
            name,
            expected,
            relative_path.parent / name,
            quarantine=quarantine,
            retention_kind=QUARANTINE_TRANSIENT_KIND,
            quarantine_locked=quarantine_locked,
        )
    except FileNotFoundError:
        return None
    except MirrorSyncError as error:
        raise MirrorSyncError(
            f"cannot remove exchange recovery artifact for {relative_path}: {error}"
        ) from error
    if required_removal_evidence is not None:
        assert quarantine is not None
        evidence_name, evidence_snapshot = required_removal_evidence
        try:
            _require_exact_removal_quarantine_evidence(
                quarantine,
                evidence_name,
                evidence_snapshot,
                relative_path,
                "during journal cleanup",
            )
        except MirrorSyncError as error:
            raise MirrorSyncError(
                "exchange removal evidence changed while retiring the active "
                f"journal; recovery journal retained at {retained_path}: "
                f"{relative_path}"
            ) from error
    return retained_path


def _recover_exchange_journal(
    root: Root,
    relative_path: PurePosixPath,
) -> None:
    root_fd, close_root = _borrow_root_fd(root)
    parent_fd = -1
    removal_quarantine: ControlObjectBinding | None = None
    removal_quarantine_locked = False
    try:
        try:
            parent_fd = _open_parent_directory(
                root_fd,
                relative_path,
                create=False,
                bound_root=root if isinstance(root, BoundRoot) else None,
            )
        except MissingPathError:
            return
        journal_name = _exchange_journal_name(relative_path)
        journal_path = relative_path.parent / journal_name
        journal_snapshot = _optional_safe_read_snapshot(root, journal_path)
        if journal_snapshot is None:
            return
        if journal_snapshot.mode != 0o600:
            raise MirrorSyncError(
                f"exchange recovery journal has unsafe mode: {relative_path}"
            )
        try:
            document = json.loads(
                journal_snapshot.payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            MirrorSyncError,
        ) as error:
            raise MirrorSyncError(
                f"exchange recovery journal is invalid: {relative_path}"
            ) from error
        base_fields = {
            "version",
            "target_path",
            "temporary_name",
            "expected",
            "replacement",
        }
        legacy_document = (
            isinstance(document, dict)
            and set(document) == base_fields
            and document.get("version") == 1
        )
        evidence_document = (
            isinstance(document, dict)
            and set(document) == base_fields | {"removal_quarantine"}
            and document.get("version") == 2
        )
        if (
            not (legacy_document or evidence_document)
            or document["target_path"] != relative_path.as_posix()
            or not isinstance(document["temporary_name"], str)
            or re.fullmatch(
                rf"\.{re.escape(relative_path.name)}"
                r"\.tmp-[0-9]+-[0-9a-f]{16}",
                document["temporary_name"],
            )
            is None
        ):
            raise MirrorSyncError(
                f"exchange recovery journal is invalid: {relative_path}"
            )
        temporary_name = document["temporary_name"]
        temporary_path = relative_path.parent / temporary_name
        target_snapshot = _optional_safe_read_snapshot(root, relative_path)
        temporary_snapshot = _optional_safe_read_snapshot(root, temporary_path)
        expected_is_absent = document["expected"] is None
        target_is_expected = (
            target_snapshot is None
            if expected_is_absent
            else _snapshot_matches_record(
                target_snapshot,
                document["expected"],
            )
        )
        target_is_replacement = _snapshot_matches_record(
            target_snapshot,
            document["replacement"],
        )
        temporary_is_expected = (
            False
            if expected_is_absent
            else _snapshot_matches_record(
                temporary_snapshot,
                document["expected"],
            )
        )
        temporary_is_replacement = _snapshot_matches_record(
            temporary_snapshot,
            document["replacement"],
        )
        removing_target = document["replacement"] is None
        removal_quarantine_snapshot: FileSnapshot | None = None
        removal_quarantine_is_expected = False
        if evidence_document:
            if not removing_target or expected_is_absent:
                raise MirrorSyncError(
                    f"exchange recovery journal has invalid removal evidence: "
                    f"{relative_path}"
                )
            removal_record = document["removal_quarantine"]
            expected_record = document["expected"]
            quarantine_name_match = (
                QUARANTINE_FILE_RE.fullmatch(removal_record.get("name", ""))
                if isinstance(removal_record, dict)
                and isinstance(removal_record.get("name"), str)
                else None
            )
            if (
                not isinstance(removal_record, dict)
                or set(removal_record) != {"name", "expected", "phase_contract"}
                or removal_record["expected"] != expected_record
                or removal_record["phase_contract"]
                != "matching-durable-entry-proves-removal"
                or not isinstance(removal_record["name"], str)
                or quarantine_name_match is None
                or quarantine_name_match.group(1) != QUARANTINE_RECOVERY_KIND
                or not isinstance(expected_record, dict)
                or not isinstance(expected_record.get("identity"), list)
                or len(expected_record["identity"]) != 3
                or not all(
                    isinstance(item, int) for item in expected_record["identity"]
                )
                or not isinstance(expected_record.get("access_policy"), list)
                or len(expected_record["access_policy"]) != 3
                or not all(
                    isinstance(item, int) for item in expected_record["access_policy"]
                )
                or quarantine_name_match.group(2)
                != f"{expected_record['identity'][0]:x}"
                or quarantine_name_match.group(3)
                != f"{expected_record['identity'][1]:x}"
                or quarantine_name_match.group(4)
                != f"{expected_record['access_policy'][0]:04o}"
                or quarantine_name_match.group(5)
                != f"{expected_record['access_policy'][1]:x}"
                or quarantine_name_match.group(6)
                != f"{expected_record['access_policy'][2]:x}"
                or not isinstance(expected_record.get("sha256"), str)
                or quarantine_name_match.group(7) != expected_record["sha256"][:16]
            ):
                raise MirrorSyncError(
                    f"exchange removal journal has invalid durable quarantine "
                    f"record: {relative_path}"
                )
            removal_quarantine = _bind_durable_quarantine_root(
                root,
                source_parent_fd=parent_fd,
            )
            fcntl.flock(removal_quarantine.fd, fcntl.LOCK_EX)
            removal_quarantine_locked = True
            quarantine_name = removal_record["name"]
            try:
                removal_quarantine_snapshot = _safe_read_leaf_snapshot(
                    removal_quarantine.fd,
                    quarantine_name,
                    PurePosixPath(quarantine_name),
                )
            except MissingPathError:
                removal_quarantine_snapshot = None
            if removal_quarantine_snapshot is not None:
                removal_quarantine_is_expected = _snapshot_matches_record(
                    removal_quarantine_snapshot,
                    removal_record["expected"],
                ) and _quarantine_file_name_matches(
                    quarantine_name,
                    removal_quarantine_snapshot,
                    QUARANTINE_RECOVERY_KIND,
                )
                if not removal_quarantine_is_expected:
                    raise MirrorSyncError(
                        "exchange removal durable quarantine evidence is "
                        f"mismatched; preserving artifacts: {relative_path}"
                    )
        required_removal_evidence: tuple[str, FileSnapshot] | None = None
        if removing_target:
            if expected_is_absent:
                raise MirrorSyncError(
                    f"exchange removal journal has no expected target: {relative_path}"
                )
            if target_is_expected and temporary_snapshot is None:
                pass
            elif target_snapshot is None and temporary_is_expected:
                assert temporary_snapshot is not None
                _rename_directory_entry_noreplace(
                    parent_fd,
                    temporary_name,
                    relative_path.name,
                )
                os.fsync(parent_fd)
                if _safe_read_snapshot(root, relative_path) != temporary_snapshot:
                    raise MirrorSyncError(
                        f"removed target changed while restoring it: {relative_path}"
                    )
            elif target_snapshot is None and temporary_snapshot is None:
                if not evidence_document or not removal_quarantine_is_expected:
                    raise MirrorSyncError(
                        "exchange removal recovery has no exact durable "
                        f"quarantine evidence; preserving journal: {relative_path}"
                    )
                assert removal_quarantine_snapshot is not None
                required_removal_evidence = (
                    quarantine_name,
                    removal_quarantine_snapshot,
                )
            else:
                raise MirrorSyncError(
                    f"exchange removal recovery state is ambiguous; preserving "
                    f"artifacts: {relative_path}"
                )
        elif expected_is_absent and target_is_replacement and temporary_is_replacement:
            _remove_exchange_artifact(
                root,
                parent_fd,
                temporary_name,
                relative_path,
                temporary_snapshot,
            )
        elif expected_is_absent and target_is_expected and temporary_is_replacement:
            _remove_exchange_artifact(
                root,
                parent_fd,
                temporary_name,
                relative_path,
                temporary_snapshot,
            )
        elif target_is_replacement and temporary_is_expected:
            assert temporary_snapshot is not None
            _restore_exchanged_target(
                root,
                parent_fd,
                temporary_name,
                relative_path,
                temporary_snapshot,
            )
            _remove_exchange_artifact(
                root,
                parent_fd,
                temporary_name,
                relative_path,
                target_snapshot,
            )
        elif target_is_expected and temporary_is_replacement:
            _remove_exchange_artifact(
                root,
                parent_fd,
                temporary_name,
                relative_path,
                temporary_snapshot,
            )
        elif target_is_replacement and temporary_snapshot is None:
            pass
        elif target_is_expected and temporary_snapshot is None:
            pass
        else:
            raise MirrorSyncError(
                f"exchange recovery state is ambiguous; preserving artifacts: "
                f"{relative_path}"
            )
        _remove_exchange_artifact(
            root,
            parent_fd,
            journal_name,
            relative_path,
            journal_snapshot,
            quarantine=removal_quarantine,
            quarantine_locked=removal_quarantine_locked,
            required_removal_evidence=required_removal_evidence,
        )
        os.fsync(parent_fd)
    finally:
        if removal_quarantine is not None and removal_quarantine_locked:
            fcntl.flock(removal_quarantine.fd, fcntl.LOCK_UN)
        if removal_quarantine is not None:
            os.close(removal_quarantine.fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if close_root:
            os.close(root_fd)


def _atomic_write_relative(
    root: Root,
    relative_path: PurePosixPath,
    payload: bytes,
    mode: int,
    *,
    create_parents: bool,
    expected_snapshot: FileSnapshot | None = None,
    expected_absent: bool = False,
) -> None:
    if expected_snapshot is not None and expected_absent:
        raise MirrorSyncError(
            f"atomic write cannot expect both presence and absence: {relative_path}"
        )
    if expected_snapshot is None and not expected_absent:
        raise MirrorSyncError(
            f"atomic write requires an exact present or absent expectation: "
            f"{relative_path}"
        )
    _recover_exchange_journal(root, relative_path)
    if expected_snapshot is not None:
        observed_snapshot = _safe_read_snapshot(root, relative_path)
        if observed_snapshot != expected_snapshot:
            raise MirrorSyncError(
                f"conditional target changed before atomic write: {relative_path}"
            )
    elif (
        expected_absent
        and _optional_safe_read_snapshot(root, relative_path) is not None
    ):
        raise MirrorSyncError(
            f"conditional target appeared before atomic write: {relative_path}"
        )
    root_fd, close_root = _borrow_root_fd(root)
    parent_fd = -1
    temporary_name: str | None = None
    temporary_fd = -1
    journal_name: str | None = None
    journal_snapshot: FileSnapshot | None = None
    replacement_snapshot: FileSnapshot | None = None
    preserve_temporary = False
    try:
        parent_fd = _open_parent_directory(
            root_fd,
            relative_path,
            create=create_parents,
            bound_root=root if isinstance(root, BoundRoot) else None,
        )
        _validate_target_leaf(parent_fd, relative_path)
        temporary_name = (
            f".{relative_path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        )
        try:
            temporary_fd = os.open(
                temporary_name,
                _FILE_WRITE_FLAGS,
                mode & 0o777,
                dir_fd=parent_fd,
            )
            os.fchmod(temporary_fd, mode & 0o777)
            _write_all(temporary_fd, payload, relative_path)
            os.fsync(temporary_fd)
            temporary_metadata = os.fstat(temporary_fd)
            replacement_snapshot = FileSnapshot(
                payload=payload,
                mode=stat.S_IMODE(temporary_metadata.st_mode),
                identity=_object_identity(temporary_metadata),
                access_policy=_access_policy(temporary_metadata),
                size=temporary_metadata.st_size,
            )
            if replacement_snapshot.mode != mode or replacement_snapshot.size != len(
                payload
            ):
                raise MirrorSyncError(
                    f"temporary replacement differs from intended bytes/mode: "
                    f"{relative_path}"
                )
            os.close(temporary_fd)
            temporary_fd = -1
            # A target changed to a symlink is rejected before publication.
            # The atomic no-clobber/exchange operations below close the remaining
            # check-to-publication race.
            _validate_target_leaf(parent_fd, relative_path)
            if expected_snapshot is not None:
                observed_snapshot = _safe_read_snapshot(root, relative_path)
                if observed_snapshot != expected_snapshot:
                    raise MirrorSyncError(
                        f"conditional target changed before publication: "
                        f"{relative_path}"
                    )
            elif (
                expected_absent
                and _optional_safe_read_snapshot(root, relative_path) is not None
            ):
                raise MirrorSyncError(
                    f"conditional target appeared before publication: {relative_path}"
                )
            assert replacement_snapshot is not None
            quarantine_path = relative_path.parent / temporary_name
            if _safe_read_snapshot(root, quarantine_path) != replacement_snapshot:
                raise MirrorSyncError(
                    f"temporary replacement was replaced before journaling: "
                    f"{relative_path}"
                )
            journal_name, journal_snapshot = _write_exchange_journal(
                parent_fd,
                relative_path,
                temporary_name,
                expected_snapshot,
                replacement_snapshot,
            )
            journal_path = relative_path.parent / journal_name
            if (
                _safe_read_snapshot(root, journal_path) != journal_snapshot
                or _safe_read_snapshot(root, quarantine_path) != replacement_snapshot
            ):
                raise MirrorSyncError(
                    f"exchange journal or replacement changed before "
                    f"publication: {relative_path}"
                )
            if expected_absent:
                try:
                    os.link(
                        temporary_name,
                        relative_path.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as error:
                    _remove_exchange_artifact(
                        root,
                        parent_fd,
                        journal_name,
                        relative_path,
                        journal_snapshot,
                    )
                    journal_name = None
                    raise MirrorSyncError(
                        f"conditional target appeared before no-clobber "
                        f"publication: {relative_path}"
                    ) from error
                os.fsync(parent_fd)
                if _safe_read_snapshot(root, relative_path) != replacement_snapshot:
                    preserve_temporary = True
                    raise MirrorSyncError(
                        f"no-clobber publication changed unexpectedly: {relative_path}"
                    )
                _isolate_and_remove_file(
                    root,
                    parent_fd,
                    temporary_name,
                    replacement_snapshot,
                    quarantine_path,
                    retention_kind=QUARANTINE_TRANSIENT_KIND,
                )
                temporary_name = None
                os.fsync(parent_fd)
                if _safe_read_snapshot(root, journal_path) != journal_snapshot:
                    raise MirrorSyncError(
                        f"exchange journal changed before cleanup: {relative_path}"
                    )
                _remove_exchange_artifact(
                    root,
                    parent_fd,
                    journal_name,
                    relative_path,
                    journal_snapshot,
                )
                journal_name = None
            else:
                assert expected_snapshot is not None
                try:
                    _exchange_directory_entries(
                        parent_fd,
                        temporary_name,
                        relative_path.name,
                    )
                    os.fsync(parent_fd)
                except (MirrorSyncError, OSError):
                    _remove_exchange_artifact(
                        root,
                        parent_fd,
                        journal_name,
                        relative_path,
                        journal_snapshot,
                    )
                    journal_name = None
                    raise
                try:
                    displaced_snapshot = _safe_read_snapshot(
                        root,
                        quarantine_path,
                    )
                    published_snapshot = _safe_read_snapshot(
                        root,
                        relative_path,
                    )
                except MirrorSyncError as displaced_error:
                    try:
                        _restore_exchanged_target(
                            root,
                            parent_fd,
                            temporary_name,
                            relative_path,
                            None,
                        )
                    except MirrorSyncError:
                        preserve_temporary = True
                        raise
                    _remove_exchange_artifact(
                        root,
                        parent_fd,
                        journal_name,
                        relative_path,
                        journal_snapshot,
                    )
                    journal_name = None
                    raise MirrorSyncError(
                        f"conditional target became unsafe before exchange "
                        f"publication: {relative_path}: {displaced_error}"
                    ) from displaced_error
                if (
                    displaced_snapshot != expected_snapshot
                    or published_snapshot != replacement_snapshot
                ):
                    try:
                        _restore_exchanged_target(
                            root,
                            parent_fd,
                            temporary_name,
                            relative_path,
                            displaced_snapshot,
                        )
                    except MirrorSyncError:
                        preserve_temporary = True
                        raise
                    _remove_exchange_artifact(
                        root,
                        parent_fd,
                        journal_name,
                        relative_path,
                        journal_snapshot,
                    )
                    journal_name = None
                    raise MirrorSyncError(
                        f"conditional target or replacement changed before "
                        f"exchange "
                        f"publication: {relative_path}"
                    )
                assert journal_snapshot is not None
                if _safe_read_snapshot(root, journal_path) != journal_snapshot:
                    preserve_temporary = True
                    raise MirrorSyncError(
                        f"exchange journal changed before cleanup: {relative_path}"
                    )
                _isolate_and_remove_file(
                    root,
                    parent_fd,
                    temporary_name,
                    expected_snapshot,
                    quarantine_path,
                    retention_kind=QUARANTINE_TRANSIENT_KIND,
                )
                temporary_name = None
                os.fsync(parent_fd)
                _remove_exchange_artifact(
                    root,
                    parent_fd,
                    journal_name,
                    relative_path,
                    journal_snapshot,
                )
                journal_name = None
            os.fsync(parent_fd)
        except MirrorSyncError:
            raise
        except OSError as error:
            raise MirrorSyncError(
                f"cannot atomically generate {relative_path}: {error}"
            ) from error
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if (
            temporary_name is not None
            and parent_fd >= 0
            and not preserve_temporary
            and replacement_snapshot is not None
        ):
            try:
                observed_temporary = _safe_read_leaf_snapshot(
                    parent_fd,
                    temporary_name,
                    relative_path.parent / temporary_name,
                )
                if observed_temporary == replacement_snapshot:
                    _isolate_and_remove_file(
                        root,
                        parent_fd,
                        temporary_name,
                        replacement_snapshot,
                        relative_path.parent / temporary_name,
                        retention_kind=QUARANTINE_TRANSIENT_KIND,
                    )
            except MirrorSyncError:
                # Ambiguous recovery artifacts are intentionally preserved.
                pass
        if parent_fd >= 0:
            os.close(parent_fd)
        if close_root:
            os.close(root_fd)


def _atomic_remove_relative(
    root: Root,
    relative_path: PurePosixPath,
    expected_snapshot: FileSnapshot,
) -> None:
    _recover_exchange_journal(root, relative_path)
    if _safe_read_snapshot(root, relative_path) != expected_snapshot:
        raise MirrorSyncError(
            f"conditional retired target changed before removal: {relative_path}"
        )
    root_fd, close_root = _borrow_root_fd(root)
    parent_fd = -1
    quarantine: ControlObjectBinding | None = None
    quarantine_locked = False
    try:
        parent_fd = _open_parent_directory(
            root_fd,
            relative_path,
            create=False,
            bound_root=root if isinstance(root, BoundRoot) else None,
        )
        quarantine = _bind_durable_quarantine_root(
            root,
            source_parent_fd=parent_fd,
        )
        _validate_target_leaf(parent_fd, relative_path)
        temporary_name = (
            f".{relative_path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        )
        removal_quarantine_name = _quarantine_file_name(
            expected_snapshot,
            QUARANTINE_RECOVERY_KIND,
        )
        fcntl.flock(quarantine.fd, fcntl.LOCK_EX)
        quarantine_locked = True
        _require_new_durable_quarantine_entry(
            quarantine.fd,
            removal_quarantine_name,
        )
        journal_name, journal_snapshot = _write_exchange_journal(
            parent_fd,
            relative_path,
            temporary_name,
            expected_snapshot,
            None,
            removal_quarantine_name=removal_quarantine_name,
        )
        journal_path = relative_path.parent / journal_name
        if (
            _safe_read_snapshot(root, relative_path) != expected_snapshot
            or _safe_read_snapshot(root, journal_path) != journal_snapshot
        ):
            raise MirrorSyncError(
                f"retired target or removal journal changed before isolation: "
                f"{relative_path}"
            )
        try:
            _rename_directory_entry_noreplace(
                parent_fd,
                relative_path.name,
                temporary_name,
            )
            os.fsync(parent_fd)
        except OSError as error:
            raise MirrorSyncError(
                f"cannot isolate retired target before removal: "
                f"{relative_path}: {error}"
            ) from error
        temporary_path = relative_path.parent / temporary_name
        moved = _safe_read_snapshot(root, temporary_path)
        if (
            moved != expected_snapshot
            or _optional_safe_read_snapshot(root, relative_path) is not None
            or _safe_read_snapshot(root, journal_path) != journal_snapshot
        ):
            raise MirrorSyncError(
                f"retired target changed during conditional isolation: {relative_path}"
            )
        _persist_quarantined_file(
            quarantine,
            parent_fd,
            temporary_name,
            expected_snapshot,
            temporary_path,
            retention_kind=QUARANTINE_RECOVERY_KIND,
            quarantine_name=removal_quarantine_name,
            quarantine_locked=True,
            capacity_checked=True,
        )
        os.fsync(parent_fd)
        if _optional_safe_read_snapshot(root, relative_path) is not None:
            raise MirrorSyncError(
                f"retired target reappeared after removal: {relative_path}"
            )
        if _safe_read_snapshot(root, journal_path) != journal_snapshot:
            raise MirrorSyncError(
                f"retired target removal journal changed before cleanup: "
                f"{relative_path}"
            )
        _remove_exchange_artifact(
            root,
            parent_fd,
            journal_name,
            relative_path,
            journal_snapshot,
            quarantine=quarantine,
            quarantine_locked=True,
            required_removal_evidence=(
                removal_quarantine_name,
                expected_snapshot,
            ),
        )
        os.fsync(parent_fd)
    finally:
        if quarantine is not None and quarantine_locked:
            fcntl.flock(quarantine.fd, fcntl.LOCK_UN)
        if parent_fd >= 0:
            os.close(parent_fd)
        if quarantine is not None:
            os.close(quarantine.fd)
        if close_root:
            os.close(root_fd)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MirrorSyncError(f"JSON payload contains duplicate key: {key}")
        result[key] = value
    return result


def _expect_object(raw: object, field_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MirrorSyncError(f"{field_name} must be an object")
    return raw


def _check_fields(
    raw: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    field_name: str,
) -> None:
    unknown = sorted(set(raw) - allowed)
    missing = sorted(required - set(raw))
    if unknown:
        raise MirrorSyncError(
            f"{field_name} has unsupported field(s): {', '.join(unknown)}"
        )
    if missing:
        raise MirrorSyncError(f"{field_name} is missing field(s): {', '.join(missing)}")


def _validate_name(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or NAME_RE.fullmatch(raw) is None:
        raise MirrorSyncError(
            f"{field_name} must contain lowercase letters, numbers, and underscores"
        )
    return raw


def _validate_repository(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or REPOSITORY_RE.fullmatch(raw) is None:
        raise MirrorSyncError(f"{field_name} must be an owner/repository string")
    return raw


def _repository_identity(repository: str) -> str:
    """Return GitHub's ASCII case-insensitive repository identity."""
    return repository.casefold()


def _path_collision_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", component).casefold() for component in path.parts
    )


def _validate_target_layout(
    targets: list[PurePosixPath],
    mirror_name: str,
) -> None:
    reserved = {
        RECEIPT_PATH,
        TRANSACTION_PATH,
        TRANSACTION_TEMP_PATH,
        TRANSACTION_COMPLETE_PATH,
        *(
            path.parent / _exchange_journal_name(path)
            for path in (RECEIPT_PATH, *targets)
        ),
    }
    entries = [("target", target, _path_collision_key(target)) for target in targets]
    entries.extend(
        (
            "reserved",
            reserved_path,
            _path_collision_key(reserved_path),
        )
        for reserved_path in reserved
    )
    trie: dict[str, Any] = {}
    terminal_key = object()
    for kind, path, collision_key in sorted(
        entries,
        key=lambda item: (len(item[2]), item[2], item[0]),
    ):
        node = trie
        for component in collision_key:
            for prior_kind, prior_path in node.get(terminal_key, ()):
                if kind == "reserved" and prior_kind == "reserved":
                    continue
                if kind == "target" and prior_kind == "target":
                    raise MirrorSyncError(
                        f"mirror {mirror_name} targets overlap: {prior_path} and {path}"
                    )
                raise MirrorSyncError(
                    f"mirror {mirror_name} target overlaps reserved recovery "
                    f"metadata: {path} and {prior_path}"
                )
            node = node.setdefault(component, {})
        existing = node.setdefault(terminal_key, [])
        for prior_kind, prior_path in existing:
            if kind == "reserved" and prior_kind == "reserved":
                continue
            if kind == "target" and prior_kind == "target":
                raise MirrorSyncError(
                    f"mirror {mirror_name} has duplicate/colliding target "
                    f"paths: {prior_path} and {path}"
                )
            raise MirrorSyncError(
                f"mirror {mirror_name} target overlaps reserved recovery "
                f"metadata: {path} and {prior_path}"
            )
        existing.append((kind, path))


def _parse_source_lock(payload: bytes) -> SourceLock:
    if len(payload) > MAX_LOCK_BYTES:
        raise MirrorSyncError(f"source lock exceeds the {MAX_LOCK_BYTES}-byte limit")
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except MirrorSyncError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise MirrorSyncError(f"source lock is not valid JSON: {error}") from error
    root = _expect_object(raw, "source lock")
    _check_fields(
        root,
        allowed={
            "version",
            "hash_algorithm",
            "canonical_repository",
            "sources",
            "mirrors",
        },
        required={
            "version",
            "hash_algorithm",
            "canonical_repository",
            "sources",
            "mirrors",
        },
        field_name="source lock",
    )
    if type(root["version"]) is not int or root["version"] != LOCK_VERSION:
        raise MirrorSyncError(f"source lock version must be {LOCK_VERSION}")
    if root["hash_algorithm"] != HASH_ALGORITHM:
        raise MirrorSyncError(f"source lock hash_algorithm must be {HASH_ALGORITHM}")
    canonical_repository = _validate_repository(
        root["canonical_repository"],
        "canonical_repository",
    )

    raw_sources = _expect_object(root["sources"], "sources")
    if not raw_sources:
        raise MirrorSyncError("sources must not be empty")
    sources: dict[str, SourceSpec] = {}
    source_paths: dict[tuple[str, ...], PurePosixPath] = {}
    lock_path_identity = _path_collision_key(LOCK_PATH)
    for raw_name, raw_source in raw_sources.items():
        name = _validate_name(raw_name, "source name")
        source = _expect_object(raw_source, f"source {name}")
        _check_fields(
            source,
            allowed={"path", "sha256", "mode"},
            required={"path", "sha256", "mode"},
            field_name=f"source {name}",
        )
        path = _validate_relative_path(source["path"], f"source {name} path")
        path_identity = _path_collision_key(path)
        if path_identity == lock_path_identity:
            raise MirrorSyncError("source lock must not hash itself")
        prior_source_path = source_paths.get(path_identity)
        if prior_source_path is not None:
            raise MirrorSyncError(
                "duplicate/colliding canonical source path under NFC+casefold "
                f"portability rules: {prior_source_path} and {path}"
            )
        source_paths[path_identity] = path
        sha256 = source["sha256"]
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise MirrorSyncError(f"source {name} sha256 must be lowercase hex")
        raw_mode = source["mode"]
        if (
            not isinstance(raw_mode, str)
            or MODE_RE.fullmatch(raw_mode) is None
            or int(raw_mode, 8) not in SUPPORTED_SOURCE_MODES
        ):
            raise MirrorSyncError(f"source {name} mode must be 0644 or 0755")
        sources[name] = SourceSpec(
            name=name,
            path=path,
            sha256=sha256,
            mode=int(raw_mode, 8),
        )

    raw_mirrors = _expect_object(root["mirrors"], "mirrors")
    if not raw_mirrors:
        raise MirrorSyncError("mirrors must not be empty")
    mirrors: dict[str, MirrorSpec] = {}
    repositories: dict[str, str] = {}
    canonical_repository_identity = _repository_identity(canonical_repository)
    for raw_name, raw_mirror in raw_mirrors.items():
        name = _validate_name(raw_name, "mirror name")
        mirror = _expect_object(raw_mirror, f"mirror {name}")
        _check_fields(
            mirror,
            allowed={"repository", "files"},
            required={"repository", "files"},
            field_name=f"mirror {name}",
        )
        repository = _validate_repository(
            mirror["repository"],
            f"mirror {name} repository",
        )
        repository_identity = _repository_identity(repository)
        if repository_identity == canonical_repository_identity:
            raise MirrorSyncError(
                f"mirror {name} must not point back to the canonical repository"
            )
        prior_repository = repositories.get(repository_identity)
        if prior_repository is not None:
            raise MirrorSyncError(
                "duplicate/colliding mirror repository under GitHub "
                f"case-insensitive identity: {prior_repository} and {repository}"
            )
        repositories[repository_identity] = repository
        raw_files = _expect_object(mirror["files"], f"mirror {name} files")
        if not raw_files:
            raise MirrorSyncError(f"mirror {name} files must not be empty")
        files: dict[str, PurePosixPath] = {}
        for raw_source_name, raw_target in raw_files.items():
            source_name = _validate_name(
                raw_source_name,
                f"mirror {name} source name",
            )
            if source_name not in sources:
                raise MirrorSyncError(
                    f"mirror {name} references unknown source {source_name}"
                )
            target = _validate_relative_path(
                raw_target,
                f"mirror {name} target for {source_name}",
            )
            if target.parts[0].casefold() == ".git":
                raise MirrorSyncError(
                    f"mirror {name} target must not enter the Git control plane: "
                    f"{target}"
                )
            if target.name.endswith(".sync-exchange.json"):
                raise MirrorSyncError(
                    f"mirror {name} target collides with recovery metadata: {target}"
                )
            files[source_name] = target
        _validate_target_layout(list(files.values()), name)
        mirrors[name] = MirrorSpec(
            name=name,
            repository=repository,
            files=files,
        )
    return SourceLock(
        canonical_repository=canonical_repository,
        sources=sources,
        mirrors=mirrors,
    )


def _source_lock_payload(source_lock: SourceLock) -> bytes:
    payload = {
        "version": LOCK_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "canonical_repository": source_lock.canonical_repository,
        "sources": {
            name: {
                "path": spec.path.as_posix(),
                "sha256": spec.sha256,
                "mode": f"{spec.mode:04o}",
            }
            for name, spec in sorted(source_lock.sources.items())
        },
        "mirrors": {
            name: {
                "repository": mirror.repository,
                "files": {
                    source_name: path.as_posix()
                    for source_name, path in sorted(mirror.files.items())
                },
            }
            for name, mirror in sorted(source_lock.mirrors.items())
        },
    }
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def load_source_lock(repository_root: Root) -> SourceLock:
    payload, _mode = _safe_read_relative(repository_root, LOCK_PATH)
    return _parse_source_lock(payload)


def _verified_sources(
    repository_root: Root,
    source_lock: SourceLock,
) -> dict[str, tuple[bytes, int]]:
    verified: dict[str, tuple[bytes, int]] = {}
    for name, source in sorted(source_lock.sources.items()):
        payload, mode = _safe_read_relative(repository_root, source.path)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != source.sha256:
            raise MirrorSyncError(
                f"canonical source hash does not match the lock: {source.path}"
            )
        if mode != source.mode:
            raise MirrorSyncError(
                f"canonical source mode does not match the lock: {source.path} "
                f"({mode:#05o} != {source.mode:#05o})"
            )
        verified[name] = payload, mode
    return verified


def _source_group_snapshots(
    repository_root: Root,
    source_lock: SourceLock,
) -> dict[str, FileSnapshot]:
    return {
        name: _safe_read_snapshot(repository_root, source.path)
        for name, source in sorted(source_lock.sources.items())
    }


def _require_same_source_group(
    expected: dict[str, FileSnapshot],
    observed: dict[str, FileSnapshot],
) -> None:
    if expected.keys() != observed.keys():
        raise MirrorSyncError("canonical source group membership changed")
    for name in expected:
        if expected[name] != observed[name]:
            raise MirrorSyncError(
                f"canonical source group changed during transaction: {name}"
            )


def verify_source_lock(repository_root: Root) -> SourceLock:
    source_lock = load_source_lock(repository_root)
    _verified_sources(repository_root, source_lock)
    return source_lock


def _selected_mirror(source_lock: SourceLock, mirror_name: str) -> MirrorSpec:
    try:
        return source_lock.mirrors[mirror_name]
    except KeyError as error:
        supported = ", ".join(sorted(source_lock.mirrors))
        raise MirrorSyncError(
            f"unknown mirror {mirror_name!r}; expected one of: {supported}"
        ) from error


def _git_environment() -> dict[str, str]:
    return {
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "SSH_ASKPASS": "/usr/bin/false",
    }


_REAL_SUBPROCESS_POPEN = subprocess.Popen
_BOUNDED_PROCESS_SIGNALS = tuple(
    candidate
    for candidate in (
        signal.SIGINT,
        signal.SIGTERM,
        getattr(signal, "SIGHUP", None),
    )
    if candidate is not None
)


@dataclass
class _ProcessOwner:
    process: subprocess.Popen[bytes] | None = None
    cleanup_deadline: float | None = None
    cleanup_started: bool = False
    trusted_exec_profile: _TrustedProcessProfile | None = None

    def begin_cleanup(self) -> float:
        if self.cleanup_deadline is None:
            self.cleanup_deadline = time.monotonic() + GIT_CLEANUP_TIMEOUT_SECONDS
        self.cleanup_started = True
        return self.cleanup_deadline


class _TrustedProcessProfile(Enum):
    MACOS_XCRUN_LOCATOR = "macos-xcrun-locator"
    PRIVATE_GIT_SNAPSHOT = "private-git-snapshot"


class _OwnedPopen(_REAL_SUBPROCESS_POPEN):
    def __new__(cls, owner: _ProcessOwner, *args: Any, **kwargs: Any):
        instance = super().__new__(cls)
        # Publish before Popen.__init__ can fork. Pure Python cannot recover a
        # PID from an arbitrarily injected exception in the C-return to
        # self.pid assignment gap; ordinary errors and supported POSIX signal
        # paths remain supervised.
        instance.returncode = None
        instance.pid = None
        instance._child_created = False
        owner.process = instance
        return instance

    def __init__(
        self,
        owner: _ProcessOwner,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        del owner
        super().__init__(*args, **kwargs)


def _owned_popen(
    owner: _ProcessOwner,
    *args: Any,
    **kwargs: Any,
) -> subprocess.Popen[bytes]:
    # Existing tests replace subprocess.Popen with a mock factory. Production
    # always takes the pre-published owner-aware class above.
    if subprocess.Popen is not _REAL_SUBPROCESS_POPEN:
        process = subprocess.Popen(*args, **kwargs)
        owner.process = process
        return process
    return _OwnedPopen(owner, *args, **kwargs)


def _verify_trusted_same_uid_spawn_kwargs(kwargs: dict[str, Any]) -> None:
    if kwargs.get("start_new_session") is not True:
        raise MirrorSyncError("trusted same-UID process requires a new session")
    forbidden_transitions = (
        "user",
        "group",
        "extra_groups",
        "preexec_fn",
    )
    for name in forbidden_transitions:
        if kwargs.get(name) is not None:
            raise MirrorSyncError(
                f"trusted same-UID process forbids Popen {name} transitions"
            )
    if os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise MirrorSyncError(
            "trusted same-UID process forbids parent credential transitions"
        )


def _owned_macos_git_locator_popen(
    owner: _ProcessOwner,
    temporary_directory: str,
) -> subprocess.Popen[bytes]:
    environment = _git_environment()
    environment["TMPDIR"] = temporary_directory
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": environment,
        "start_new_session": True,
    }
    _verify_trusted_same_uid_spawn_kwargs(kwargs)
    owner.trusted_exec_profile = _TrustedProcessProfile.MACOS_XCRUN_LOCATOR
    return _owned_popen(
        owner,
        [MACOS_GIT_LOCATOR_EXECUTABLE.as_posix(), "--find", "git"],
        **kwargs,
    )


def _owned_private_git_popen(
    owner: _ProcessOwner,
    command: list[str],
    *,
    control_root: BoundRoot,
    executable_binding: ControlObjectBinding,
    **kwargs: Any,
) -> subprocess.Popen[bytes]:
    if (
        executable_binding.path is None
        or executable_binding.content_digest is None
        or control_root.git_executable.path is None
        or control_root.git_executable.content_digest is None
    ):
        raise MirrorSyncError(
            "trusted private Git process requires content-bound executables"
        )
    if not command or command[0] != control_root.git_executable.path.as_posix():
        raise MirrorSyncError(
            "trusted private Git argv0 does not match its bound source executable"
        )
    if kwargs.get("env") != _git_environment():
        raise MirrorSyncError("trusted private Git requires the closed environment")
    _verify_trusted_same_uid_spawn_kwargs(kwargs)
    owner.trusted_exec_profile = _TrustedProcessProfile.PRIVATE_GIT_SNAPSHOT
    return _owned_popen(
        owner,
        command,
        executable=executable_binding.path.as_posix(),
        **kwargs,
    )


@dataclass
class _DeferredProcessSignals:
    original_handlers: dict[int, Any]
    received: list[int] = field(default_factory=list)

    def handler(self, signum: int, _frame: Any) -> None:
        if signum not in self.received:
            self.received.append(signum)


def _install_deferred_process_signal_handlers() -> _DeferredProcessSignals:
    if not hasattr(signal, "pthread_sigmask"):
        raise MirrorSyncError(
            "bounded process launch requires pthread signal-mask support"
        )
    signals_to_defer = set(_BOUNDED_PROCESS_SIGNALS)
    try:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals_to_defer)
    except OSError as error:
        raise MirrorSyncError(
            f"cannot fence bounded process signal-handler installation: {error}"
        ) from error
    state = _DeferredProcessSignals(
        original_handlers={
            signum: signal.getsignal(signum) for signum in _BOUNDED_PROCESS_SIGNALS
        }
    )
    installed: list[int] = []
    errors: list[BaseException] = []
    try:
        for signum in _BOUNDED_PROCESS_SIGNALS:
            signal.signal(signum, state.handler)
            installed.append(signum)
    except BaseException as error:
        errors.append(error)
    if not errors:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            errors.append(error)
    if errors:
        # Even when the atomic mask transition itself failed, leave no
        # orphaned deferred handler behind. Retry the original mask only as a
        # best-effort rollback and retain every failure in the terminal error.
        for signum in reversed(installed):
            try:
                signal.signal(signum, state.original_handlers[signum])
            except BaseException as restore_error:
                errors.append(restore_error)
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as restore_error:
            errors.append(restore_error)
        raise MirrorSyncError(
            "cannot install bounded process deferred signal handlers: "
            + "; ".join(str(error) for error in errors)
        ) from errors[0]
    return state


def _restore_deferred_process_signal_handlers(
    state: _DeferredProcessSignals,
) -> list[BaseException]:
    errors: list[BaseException] = []
    signals_to_defer = set(_BOUNDED_PROCESS_SIGNALS)
    previous_mask: set[signal.Signals] | None = None
    try:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals_to_defer)
    except BaseException as error:
        errors.append(error)
    # A failed atomic fence weakens race exclusion but never justifies leaving
    # process-global handlers installed after the child is terminal.
    for signum in _BOUNDED_PROCESS_SIGNALS:
        try:
            signal.signal(signum, state.original_handlers[signum])
        except BaseException as error:
            errors.append(error)
    if previous_mask is not None:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            errors.append(error)
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except BaseException as retry_error:
                errors.append(retry_error)
    return errors


def _replay_deferred_process_signals(
    state: _DeferredProcessSignals,
    *,
    primary_error: BaseException | None,
) -> list[BaseException]:
    errors: list[BaseException] = []
    for signum in state.received:
        handler = state.original_handlers[signum]
        if handler == signal.SIG_IGN:
            continue
        if callable(handler):
            try:
                handler(signum, None)
            except BaseException as error:
                errors.append(error)
            continue
        if primary_error is not None:
            errors.append(
                MirrorSyncError(
                    f"deferred signal {signum} has a default action and was not "
                    "replayed over an existing bounded-process failure"
                )
            )
            continue
        signal.raise_signal(signum)
    return errors


def _launch_and_collect_bounded_process(
    launcher: Callable[[_ProcessOwner], subprocess.Popen[bytes]],
    collector: Callable[
        [subprocess.Popen[bytes], _ProcessOwner],
        tuple[int, bytes, bytes],
    ],
    *,
    label: str,
) -> tuple[int, bytes, bytes]:
    """Own one child from pre-construction publication through collection.

    Parent-only deferred handlers close the ordinary POSIX signal handoff
    windows without giving the child a blocked signal mask. The owner is
    published before Popen.__init__, and one owner-held deadline caps every
    cleanup path. Arbitrary runtime exception injection in Popen's internal
    C-return-to-pid-assignment gap is outside this pure-Python guarantee.
    """
    signal_state = _install_deferred_process_signal_handlers()
    owner = _ProcessOwner()
    result: tuple[int, bytes, bytes] | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        process = launcher(owner)
        if owner.process is None:
            owner.process = process
        elif owner.process is not process:
            raise MirrorSyncError(
                f"bounded {label} launcher returned a child it does not own"
            )
        result = collector(process, owner)
    except BaseException as error:
        primary_error = error
        cleanup_errors.extend(_cleanup_owned_process(owner))

    restore_errors = _restore_deferred_process_signal_handlers(signal_state)
    replay_errors = _replay_deferred_process_signals(
        signal_state,
        primary_error=primary_error,
    )
    secondary_errors = cleanup_errors + restore_errors + replay_errors
    if primary_error is not None:
        if secondary_errors:
            raise MirrorSyncError(
                f"{primary_error}; bounded {label} terminalization is "
                "inconclusive: "
                + "; ".join(
                    f"{type(error).__name__}: {error}" for error in secondary_errors
                )
            ) from primary_error
        raise primary_error.with_traceback(primary_error.__traceback__)
    if secondary_errors:
        if len(secondary_errors) == 1 and not (cleanup_errors or restore_errors):
            raise secondary_errors[0]
        raise MirrorSyncError(
            f"bounded {label} terminalization is inconclusive: "
            + "; ".join(
                f"{type(error).__name__}: {error}" for error in secondary_errors
            )
        ) from secondary_errors[0]
    if result is None:
        raise MirrorSyncError(f"bounded {label} produced no terminal result")
    return result


def _wait_for_git_leader_exit_without_reaping(
    process: subprocess.Popen[bytes],
    deadline: float,
    label: str,
) -> None:
    if process.returncode is not None:
        raise MirrorSyncError(
            f"bounded {label} leader was reaped before process-group cleanup"
        )
    process_id = getattr(process, "pid", None)
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise MirrorSyncError(f"bounded {label} has no valid leader identity")
    required_waitid = tuple(
        getattr(os, name, None) for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    )
    waitid = getattr(os, "waitid", None)
    if callable(waitid) and all(isinstance(value, int) for value in required_waitid):
        id_type, exited_flag, nohang_flag, nowait_flag = required_waitid
        assert isinstance(id_type, int)
        assert isinstance(exited_flag, int)
        assert isinstance(nohang_flag, int)
        assert isinstance(nowait_flag, int)
        while True:
            try:
                result = waitid(
                    id_type,
                    process_id,
                    exited_flag | nohang_flag | nowait_flag,
                )
            except ChildProcessError as error:
                raise MirrorSyncError(
                    f"bounded {label} leader was reaped outside its supervisor"
                ) from error
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot observe bounded {label} leader exit: {error}"
                ) from error
            if result is not None and result.si_pid == process_id:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MirrorSyncError(f"bounded {label} exceeded its deadline")
            time.sleep(min(0.01, remaining))

    if sys.platform != "darwin" or not hasattr(select, "kqueue"):
        raise MirrorSyncError(
            f"bounded {label} requires waitid(WNOWAIT) or Darwin kqueue "
            "process supervision"
        )
    try:
        queue = select.kqueue()
    except OSError as error:
        raise MirrorSyncError(
            f"cannot create bounded {label} Darwin process observer: {error}"
        ) from error
    observed = False
    observation_error: BaseException | None = None
    close_error: BaseException | None = None
    try:
        try:
            change = select.kevent(
                process_id,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                queue.control([change], 0, 0)
            except ProcessLookupError:
                # The unreaped Popen still reserves the numeric PID. ESRCH
                # during late registration therefore means this exact leader
                # already exited; it cannot describe a reused PID.
                observed = True
            while not observed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MirrorSyncError(f"bounded {label} exceeded its deadline")
                events = queue.control(None, 1, remaining)
                if not events:
                    continue
                event = events[0]
                event_error = getattr(select, "KQ_EV_ERROR", 0)
                if (
                    event.ident != process_id
                    or event.filter != select.KQ_FILTER_PROC
                    or not (event.fflags & select.KQ_NOTE_EXIT)
                    or (event_error and event.flags & event_error)
                ):
                    raise MirrorSyncError(
                        f"bounded {label} received an invalid Darwin exit event"
                    )
                observed = True
        except BaseException as error:
            observation_error = error
    finally:
        try:
            queue.close()
        except BaseException as error:
            close_error = error
    if observation_error is not None:
        if close_error is not None:
            raise MirrorSyncError(
                f"{observation_error}; cannot close bounded {label} Darwin "
                f"process observer: {close_error}"
            ) from observation_error
        if isinstance(observation_error, MirrorSyncError):
            raise observation_error
        raise MirrorSyncError(
            f"cannot observe bounded {label} Darwin leader exit: {observation_error}"
        ) from observation_error
    if close_error is not None:
        raise MirrorSyncError(
            f"cannot close bounded {label} Darwin process observer: {close_error}"
        ) from close_error
    if not observed:
        raise MirrorSyncError(f"bounded {label} produced no Darwin exit event")


def _terminate_git_process(
    process: subprocess.Popen[bytes],
    *,
    deadline: float | None = None,
    leader_exit_observed: bool = False,
    trusted_exec_profile: _TrustedProcessProfile | None = None,
) -> int:
    errors: list[str] = []
    if process.returncode is not None:
        raise MirrorSyncError(
            "bounded Git process leader was already reaped before process-group cleanup"
        )
    cleanup_deadline = (
        deadline
        if deadline is not None
        else time.monotonic() + GIT_CLEANUP_TIMEOUT_SECONDS
    )
    process_id = getattr(process, "pid", None)
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        errors.append("bounded Git process has no valid process-group identity")
    else:
        try:
            # Every caller launches with start_new_session=True. Signal the
            # process group while its unreaped leader still reserves the exact
            # numeric PGID. No numeric group operation is allowed after wait.
            os.killpg(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as group_error:
            # Darwin can report EPERM for an observed-exited zombie leader.
            # That is not a general absence proof: accept it only for the
            # fixed same-UID/no-privilege-transition execution profile above.
            # Every other group-signal failure remains inconclusive; a direct
            # child fallback would have to poll and can lose the PGID binding.
            darwin_trusted_zombie_fence_candidate = (
                sys.platform == "darwin"
                and leader_exit_observed
                and trusted_exec_profile
                in {
                    _TrustedProcessProfile.MACOS_XCRUN_LOCATOR,
                    _TrustedProcessProfile.PRIVATE_GIT_SNAPSHOT,
                }
                and isinstance(group_error, PermissionError)
            )
            if not darwin_trusted_zombie_fence_candidate:
                errors.append(f"cannot signal bounded Git process group: {group_error}")

    # This is the only reap. After it returns, the leader's numeric PID/PGID
    # can be reused, so this function performs no poll, kill, or group probe.
    return_code: int | None = None
    try:
        return_code = process.wait(
            timeout=max(0.0, cleanup_deadline - time.monotonic())
        )
    except subprocess.TimeoutExpired as error:
        errors.append(f"bounded Git process leader was not reaped: {error}")
    except OSError as error:
        errors.append(f"cannot reap bounded Git process leader: {error}")
    if errors:
        raise MirrorSyncError(
            "bounded Git process cleanup is inconclusive: " + "; ".join(errors)
        )
    if return_code is None:
        raise MirrorSyncError("bounded Git process cleanup produced no return code")
    return return_code


def _drain_and_close_git_process_output(
    process: subprocess.Popen[bytes],
    *,
    deadline: float | None = None,
) -> None:
    """Discard bounded pending pipe bytes when a launched process has no caller."""
    selector: selectors.BaseSelector | None = None
    errors: list[str] = []
    streams = {
        "stdout": getattr(process, "stdout", None),
        "stderr": getattr(process, "stderr", None),
    }
    try:
        try:
            selector = selectors.DefaultSelector()
        except (OSError, ValueError) as error:
            errors.append(f"cannot create Git output cleanup selector: {error}")
        for name, stream in streams.items():
            if stream is None:
                continue
            if selector is None:
                continue
            try:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            except (OSError, ValueError) as error:
                errors.append(f"cannot register Git {name} cleanup drain: {error}")
        cleanup_deadline = (
            deadline
            if deadline is not None
            else time.monotonic() + GIT_CLEANUP_TIMEOUT_SECONDS
        )
        while selector is not None and selector.get_map():
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                errors.append("Git output cleanup drain exceeded its deadline")
                break
            try:
                events = selector.select(remaining)
            except OSError as error:
                errors.append(f"cannot select Git cleanup output: {error}")
                break
            if not events:
                continue
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError as error:
                    errors.append(f"cannot drain Git {key.data}: {error}")
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, OSError, ValueError):
                        pass
                    continue
                if chunk:
                    continue
                try:
                    selector.unregister(key.fileobj)
                except (KeyError, OSError, ValueError):
                    pass
    finally:
        if selector is not None:
            try:
                selector.close()
            except (OSError, ValueError) as error:
                errors.append(f"cannot close Git output cleanup selector: {error}")
        for name, stream in streams.items():
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError) as error:
                errors.append(f"cannot close Git {name} after cleanup: {error}")
    if errors:
        raise MirrorSyncError("; ".join(errors))


def _owned_process_has_bound_pid(owner: _ProcessOwner) -> bool:
    process = owner.process
    if process is None or getattr(process, "returncode", None) is not None:
        return False
    process_id = getattr(process, "pid", None)
    return (
        not isinstance(process_id, bool)
        and isinstance(process_id, int)
        and process_id > 0
    )


def _cleanup_owned_process(
    owner: _ProcessOwner,
) -> list[BaseException]:
    if owner.cleanup_started or not _owned_process_has_bound_pid(owner):
        return []
    deadline = owner.begin_cleanup()
    process = owner.process
    assert process is not None
    errors: list[BaseException] = []
    try:
        _terminate_git_process(
            process,
            deadline=deadline,
            trusted_exec_profile=owner.trusted_exec_profile,
        )
    except BaseException as error:
        errors.append(error)
    try:
        _drain_and_close_git_process_output(process, deadline=deadline)
    except BaseException as error:
        errors.append(error)
    return errors


def _collect_bounded_process_output(
    process: subprocess.Popen[bytes],
    operation: OperationBudget | None,
    *,
    owner: _ProcessOwner | None = None,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float,
    label: str,
) -> tuple[int, bytes, bytes]:
    process_owner = owner if owner is not None else _ProcessOwner(process=process)
    if process_owner.process is None:
        process_owner.process = process
    elif process_owner.process is not process:
        raise MirrorSyncError("bounded process owner does not match its child")
    selector: selectors.BaseSelector | None = None
    buffers: dict[str, bytearray] = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    limits = {
        "stdout": stdout_limit,
        "stderr": stderr_limit,
    }
    deadline = time.monotonic() + timeout_seconds
    if operation is not None:
        deadline = min(deadline, operation.deadline)
    result: tuple[int, bytes, bytes] | None = None
    primary_error: BaseException | None = None
    primary_cause: BaseException | None = None
    cleanup_error: BaseException | None = None
    close_errors: list[str] = []
    try:
        if process.stdout is None or process.stderr is None:
            raise MirrorSyncError("bounded Git process pipes were not created")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MirrorSyncError(
                    f"bounded {label} exceeded {timeout_seconds} seconds"
                )
            events = selector.select(remaining)
            if not events:
                continue
            for key, _mask in events:
                stream_name = key.data
                current = buffers[stream_name]
                maximum = limits[stream_name]
                try:
                    chunk = os.read(
                        key.fileobj.fileno(),
                        min(64 * 1024, maximum + 1 - len(current)),
                    )
                except OSError as error:
                    raise MirrorSyncError(
                        f"cannot read bounded {label} {stream_name}: {error}"
                    ) from error
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                current.extend(chunk)
                _consume_operation_budget(
                    operation,
                    byte_count=len(chunk),
                    label=f"collecting Git {stream_name}",
                )
                if len(current) > maximum:
                    raise MirrorSyncError(
                        f"bounded {label} {stream_name} exceeds the "
                        f"{maximum}-byte limit"
                    )
        _wait_for_git_leader_exit_without_reaping(
            process,
            deadline,
            label,
        )
        cleanup_deadline = process_owner.begin_cleanup()
        return_code = _terminate_git_process(
            process,
            deadline=cleanup_deadline,
            leader_exit_observed=True,
            trusted_exec_profile=process_owner.trusted_exec_profile,
        )
        result = (
            return_code,
            bytes(buffers["stdout"]),
            bytes(buffers["stderr"]),
        )
    except BaseException as error:
        if isinstance(error, (OSError, ValueError)):
            primary_error = MirrorSyncError(
                f"cannot supervise bounded {label}: {error}"
            )
            primary_cause = error
        else:
            primary_error = error
        if not process_owner.cleanup_started:
            cleanup_deadline = process_owner.begin_cleanup()
            try:
                _terminate_git_process(
                    process,
                    deadline=cleanup_deadline,
                    trusted_exec_profile=process_owner.trusted_exec_profile,
                )
            except BaseException as error:
                cleanup_error = error
    finally:
        if selector is not None:
            try:
                selector.close()
            except (OSError, ValueError) as error:
                close_errors.append(f"cannot close bounded {label} selector: {error}")
        for name, stream in (
            ("stdout", getattr(process, "stdout", None)),
            ("stderr", getattr(process, "stderr", None)),
        ):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError) as error:
                close_errors.append(f"cannot close bounded {label} {name}: {error}")
    if primary_error is not None:
        cleanup_details = list(close_errors)
        if cleanup_error is not None:
            cleanup_details.insert(0, str(cleanup_error))
        if cleanup_details:
            raise MirrorSyncError(
                f"{primary_error}; bounded {label} cleanup is inconclusive: "
                + "; ".join(cleanup_details)
            ) from (primary_cause or primary_error)
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    if close_errors:
        raise MirrorSyncError(
            f"bounded {label} cleanup is inconclusive: " + "; ".join(close_errors)
        )
    if result is None:
        raise MirrorSyncError(f"bounded {label} produced no terminal result")
    return result


def _collect_bounded_git_output(
    process: subprocess.Popen[bytes],
    operation: OperationBudget | None = None,
    *,
    owner: _ProcessOwner | None = None,
) -> tuple[int, bytes, bytes]:
    return _collect_bounded_process_output(
        process,
        operation,
        owner=owner,
        stdout_limit=MAX_GIT_STDOUT_BYTES,
        stderr_limit=MAX_GIT_STDERR_BYTES,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        label="Git verification",
    )


def _bind_macos_git_locator_temp_directory(
    path: Path,
    expected_access_policy: tuple[int, int, int],
) -> ControlObjectBinding:
    binding = _bind_absolute_control_object(
        path,
        "macOS Git locator temporary directory",
        require_directory=True,
    )
    if binding.access_policy != expected_access_policy:
        os.close(binding.fd)
        raise MirrorSyncError(
            "macOS Git locator temporary directory must have access policy "
            f"{expected_access_policy}, observed {binding.access_policy}"
        )
    return binding


def _resolve_macos_git_executable() -> Path:
    # `/usr/bin/git` is an Apple platform shim whose copied bytes are not
    # executable outside the sealed system volume. Treat the fixed system
    # `xcrun` locator as a platform trust root, then content-bind and snapshot
    # the ordinary developer-tool Git binary that it selects.
    locator = _bind_absolute_control_object(
        MACOS_GIT_LOCATOR_EXECUTABLE,
        "macOS Git locator executable",
        require_directory=False,
    )
    try:
        temporary_directory = _bind_macos_git_locator_temp_directory(
            MACOS_GIT_LOCATOR_TEMP_DIRECTORY,
            MACOS_GIT_LOCATOR_TEMP_ACCESS_POLICY,
        )
    except BaseException:
        os.close(locator.fd)
        raise
    try:
        _revalidate_absolute_control_object(locator)
        _revalidate_absolute_control_object(temporary_directory)
        if temporary_directory.path is None:
            raise MirrorSyncError(
                "macOS Git locator temporary directory has no absolute path"
            )
        return_code, stdout, stderr = _launch_and_collect_bounded_process(
            lambda owner: _owned_macos_git_locator_popen(
                owner,
                temporary_directory.path.as_posix(),
            ),
            lambda process, owner: _collect_bounded_process_output(
                process,
                None,
                owner=owner,
                stdout_limit=MAX_GIT_LOCATOR_STDOUT_BYTES,
                stderr_limit=MAX_GIT_VERSION_STDERR_BYTES,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                label="macOS Git locator",
            ),
            label="macOS Git locator",
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot run bounded macOS Git locator: {error}"
        ) from error
    finally:
        try:
            try:
                _revalidate_absolute_control_object(temporary_directory)
            finally:
                os.close(temporary_directory.fd)
        finally:
            try:
                _revalidate_absolute_control_object(locator)
            finally:
                os.close(locator.fd)
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise MirrorSyncError(
            "macOS Git locator failed"
            + (f": {detail}" if detail else f" (exit {return_code})")
        )
    if stderr:
        raise MirrorSyncError("macOS Git locator returned unexpected stderr")
    try:
        rendered = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise MirrorSyncError("macOS Git locator returned a non-UTF-8 path") from error
    if not rendered.endswith("\n") or "\n" in rendered[:-1] or "\0" in rendered:
        raise MirrorSyncError("macOS Git locator returned a malformed path")
    path = Path(rendered[:-1])
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise MirrorSyncError(
            f"macOS Git locator returned a non-canonical path: {path}"
        )
    if path == Path("/usr/bin/git"):
        raise MirrorSyncError(
            "macOS Git locator returned the non-snapshot-capable system shim"
        )
    return path


def _parse_git_version_output(payload: bytes) -> tuple[int, int, int]:
    match = GIT_VERSION_RE.fullmatch(payload)
    if match is None:
        raise MirrorSyncError("Git capability probe returned malformed version output")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def _bound_private_git_executable(
    root: BoundRoot,
) -> ControlObjectBinding:
    if root.private_git_executable is not None:
        binding = root.private_git_executable.executable
    elif root.git_control is not None:
        binding = root.git_control.private_git_executable
    else:
        raise MirrorSyncError(
            "private Git executable snapshot must exist before Git process launch"
        )
    if binding.path is None or binding.content_digest is None:
        raise MirrorSyncError(
            "private Git executable snapshot must be an absolute content-bound file"
        )
    return binding


def _bound_git_argv0(root: BoundRoot) -> str:
    binding = root.git_executable
    if binding.path is None or binding.content_digest is None:
        raise MirrorSyncError(
            "Git executable source must be an absolute content-bound file"
        )
    return binding.path.as_posix()


def _popen_from_bound_directory(
    command: list[str],
    *,
    owner: _ProcessOwner,
    directory_fd: int,
    directory_identity: tuple[int, int, int],
    directory_access_policy: tuple[int, int, int],
    directory_label: str,
    control_root: BoundRoot | None = None,
    executable_binding: ControlObjectBinding | None = None,
    **kwargs: Any,
) -> subprocess.Popen[bytes]:
    if "cwd" in kwargs or "preexec_fn" in kwargs or "executable" in kwargs:
        raise MirrorSyncError(
            "bound-directory process launch does not accept cwd, preexec_fn, "
            "or executable overrides"
        )
    if (control_root is None) != (executable_binding is None):
        raise MirrorSyncError(
            "bound executable process launch requires its control root"
        )

    # The CLI is single-threaded. Change its current directory through the
    # already-bound descriptor before Popen so the child inherits that exact
    # directory object. A pathname replacement at the launch boundary cannot
    # redirect the child, and no Python executable has to be re-executed.
    saved_directory_fd = os.open(".", _DIRECTORY_FLAGS)
    process: subprocess.Popen[bytes] | None = None
    primary_error: BaseException | None = None
    try:
        executable_path: str | None = None
        if executable_binding is not None:
            assert control_root is not None
            if (
                executable_binding.path is None
                or executable_binding.content_digest is None
            ):
                raise MirrorSyncError(
                    "bound process executable must be an absolute content-bound file"
                )
            _revalidate_control_object(control_root, executable_binding)
            executable_path = executable_binding.path.as_posix()
        descriptor_metadata = os.fstat(directory_fd)
        if _object_identity(descriptor_metadata) != directory_identity:
            raise MirrorSyncError(
                f"{directory_label} descriptor was replaced before process launch"
            )
        if _access_policy(descriptor_metadata) != directory_access_policy:
            raise MirrorSyncError(
                f"{directory_label} descriptor access policy changed before "
                "process launch"
            )
        os.fchdir(directory_fd)
        inherited_metadata = os.stat(".", follow_symlinks=False)
        if _object_identity(inherited_metadata) != directory_identity:
            raise MirrorSyncError(
                f"{directory_label} current-directory identity changed before "
                "process launch"
            )
        if _access_policy(inherited_metadata) != directory_access_policy:
            raise MirrorSyncError(
                f"{directory_label} current-directory access policy changed "
                "before process launch"
            )
        trusted_private_git_launch = (
            executable_binding is not None
            and control_root is not None
            and kwargs.get("env") == _git_environment()
            and kwargs.get("start_new_session") is True
        )
        if trusted_private_git_launch:
            assert executable_binding is not None
            assert control_root is not None
            process = _owned_private_git_popen(
                owner,
                command,
                control_root=control_root,
                executable_binding=executable_binding,
                **kwargs,
            )
        else:
            process = _owned_popen(
                owner,
                command,
                executable=executable_path,
                **kwargs,
            )
        if executable_binding is not None:
            assert control_root is not None
            _revalidate_control_object(control_root, executable_binding)
    except BaseException as error:
        primary_error = error
    finally:
        cleanup_errors: list[MirrorSyncError] = []
        try:
            os.fchdir(saved_directory_fd)
        except OSError as error:
            cleanup_errors.append(
                MirrorSyncError(
                    "cannot restore the parent working directory after bounded "
                    f"{directory_label} process launch: {error}"
                )
            )
        try:
            os.close(saved_directory_fd)
        except OSError as error:
            cleanup_errors.append(
                MirrorSyncError(
                    "cannot close the saved parent-directory descriptor after "
                    f"bounded {directory_label} process launch: {error}"
                )
            )
        if process is not None and (primary_error is not None or cleanup_errors):
            process_cleanup_deadline = owner.begin_cleanup()
            try:
                _terminate_git_process(
                    process,
                    deadline=process_cleanup_deadline,
                    trusted_exec_profile=owner.trusted_exec_profile,
                )
            except BaseException as error:
                cleanup_errors.append(
                    MirrorSyncError(
                        "cannot recover the bounded process after parent-directory "
                        f"cleanup failed: {error}"
                    )
                )
            try:
                _drain_and_close_git_process_output(
                    process,
                    deadline=process_cleanup_deadline,
                )
            except BaseException as error:
                cleanup_errors.append(
                    MirrorSyncError(
                        "cannot drain and close the bounded process after "
                        f"parent-directory cleanup failed: {error}"
                    )
                )
        if cleanup_errors and primary_error is None:
            raise MirrorSyncError(
                "bounded process launch cleanup failed: "
                + "; ".join(str(error) for error in cleanup_errors)
            ) from cleanup_errors[0]
        if cleanup_errors and primary_error is not None:
            raise MirrorSyncError(
                f"{primary_error}; bounded process launch cleanup is "
                "inconclusive: " + "; ".join(str(error) for error in cleanup_errors)
            ) from primary_error
    if primary_error is not None:
        raise primary_error
    if process is None:
        raise MirrorSyncError(f"cannot start bounded {directory_label} process")
    return process


def _verify_git_capability(bound_root: BoundRoot) -> None:
    if bound_root.git_capability_verified:
        _revalidate_bound_root(bound_root)
        return
    command = [
        _bound_git_argv0(bound_root),
        "--no-lazy-fetch",
        "--version",
    ]
    try:
        _revalidate_bound_root(bound_root)
        return_code, stdout, stderr = _launch_and_collect_bounded_process(
            lambda owner: _popen_from_bound_directory(
                command,
                owner=owner,
                directory_fd=bound_root.fd,
                directory_identity=bound_root.identity,
                directory_access_policy=bound_root.access_policy,
                directory_label="repository root",
                control_root=bound_root,
                executable_binding=_bound_private_git_executable(bound_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_git_environment(),
                start_new_session=True,
            ),
            lambda process, owner: _collect_bounded_process_output(
                process,
                bound_root.operation,
                owner=owner,
                stdout_limit=MAX_GIT_VERSION_STDOUT_BYTES,
                stderr_limit=MAX_GIT_VERSION_STDERR_BYTES,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                label="Git capability probe",
            ),
            label="Git capability probe",
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot run bounded Git capability probe: {error}"
        ) from error
    finally:
        _revalidate_bound_root(bound_root)
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise MirrorSyncError(
            "Git 2.45.0 or newer with --no-lazy-fetch is required"
            + (f": {detail}" if detail else f" (exit {return_code})")
        )
    version = _parse_git_version_output(stdout)
    if version < MINIMUM_GIT_VERSION:
        rendered = ".".join(str(component) for component in version)
        raise MirrorSyncError(
            f"Git 2.45.0 or newer with --no-lazy-fetch is required; observed {rendered}"
        )
    bound_root.git_capability_verified = True


def _run_private_git_config_process(
    bound_root: BoundRoot,
    config_name: str,
) -> bytes:
    if bound_root.git_control is None:
        raise MirrorSyncError(
            "Git control plane must be bound before static config inspection"
        )
    if not bound_root.git_capability_verified:
        raise MirrorSyncError("Git capability gate has not completed")
    command = [
        _bound_git_argv0(bound_root),
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-replace-objects",
        *GIT_DERIVED_CACHE_DISABLE_ARGUMENTS,
        "config",
        f"--file={config_name}",
        "--no-includes",
        "--null",
        "--list",
    ]
    try:
        _revalidate_bound_root(bound_root)
        return_code, stdout, stderr = _launch_and_collect_bounded_process(
            lambda owner: _popen_from_bound_directory(
                command,
                owner=owner,
                directory_fd=bound_root.git_control.private.fd,
                directory_identity=bound_root.git_control.private.identity,
                directory_access_policy=bound_root.git_control.private.access_policy,
                directory_label="private Git control snapshot",
                control_root=bound_root,
                executable_binding=_bound_private_git_executable(bound_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_git_environment(),
                start_new_session=True,
            ),
            lambda process, owner: _collect_bounded_git_output(
                process,
                bound_root.operation,
                owner=owner,
            ),
            label="private Git config snapshot inspection",
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inspect private Git config snapshot: {error}"
        ) from error
    finally:
        _revalidate_bound_root(bound_root)
    if return_code == 0:
        return stdout
    detail = stderr.decode("utf-8", errors="replace").strip()
    if len(detail) > 1000:
        detail = detail[:1000] + "..."
    raise MirrorSyncError(
        "private Git config snapshot inspection failed"
        + (f": {detail}" if detail else f" (exit {return_code})")
    )


def _verify_static_git_profile(bound_root: BoundRoot) -> None:
    binding = bound_root.git_control
    if binding is None:
        raise MirrorSyncError(
            "Git control plane must be bound before static profile inspection"
        )
    if binding.static_profile_verified:
        _revalidate_bound_root(bound_root)
        return

    alternate_marker_keys = {
        _path_collision_key(PurePosixPath("info/alternates")),
        _path_collision_key(PurePosixPath("info/http-alternates")),
    }
    for record in binding.private_objects_manifest:
        if len(record) < 2 or not isinstance(record[1], str):
            raise MirrorSyncError(
                "private Git object snapshot has an invalid manifest record"
            )
        relative_path = PurePosixPath(record[1])
        if relative_path.name.casefold().endswith(".promisor"):
            raise MirrorSyncError(
                f"partial/promisor Git object state is not allowed: {relative_path}"
            )
        if _path_collision_key(relative_path) in alternate_marker_keys:
            raise MirrorSyncError(
                f"Git object alternates are not allowed: {relative_path}"
            )

    control_files = {
        record[1]
        for record in binding.private_manifest
        if (len(record) >= 2 and record[0] == "file" and isinstance(record[1], str))
    }
    for config_name in ("config", "config.worktree"):
        if config_name not in control_files:
            continue
        raw_config = _run_private_git_config_process(
            bound_root,
            config_name,
        )
        for record in raw_config.split(b"\0"):
            if not record:
                continue
            raw_key = record.split(b"\n", 1)[0]
            try:
                key = raw_key.decode("utf-8", errors="strict").casefold()
            except UnicodeDecodeError as error:
                raise MirrorSyncError(
                    "private Git config key is not valid UTF-8"
                ) from error
            if key == "include.path" or (
                key.startswith("includeif.") and key.endswith(".path")
            ):
                raise MirrorSyncError(
                    "repository-local Git include paths are not allowed"
                )
            if key.startswith("fsck."):
                raise MirrorSyncError(
                    f"repository-local Git fsck configuration is not allowed: {key}"
                )
            if key.startswith("url.") and (
                key.endswith(".insteadof") or key.endswith(".pushinsteadof")
            ):
                raise MirrorSyncError(
                    f"repository-local Git URL rewriting is not allowed: {key}"
                )
            if key == "extensions.partialclone" or (
                key.startswith("remote.")
                and (key.endswith(".promisor") or key.endswith(".partialclonefilter"))
            ):
                raise MirrorSyncError(
                    f"partial/promisor Git config state is not allowed: {key}"
                )
    binding.static_profile_verified = True
    _revalidate_bound_root(bound_root)


def _run_private_git_process(
    bound_root: BoundRoot,
    *arguments: str,
    stdin_payload: bytes | None = None,
) -> bytes:
    if bound_root.git_control is None:
        raise MirrorSyncError(
            "Git control plane must be bound before repository verification"
        )
    if not bound_root.git_capability_verified:
        raise MirrorSyncError("Git capability gate has not completed")
    if not bound_root.git_control.static_profile_verified:
        raise MirrorSyncError("Git static repository profile has not completed")
    git_command = [
        _bound_git_argv0(bound_root),
        "--no-lazy-fetch",
        "--git-dir=.",
        f"--work-tree={bound_root.path}",
        "--no-optional-locks",
        "--no-replace-objects",
        *GIT_DERIVED_CACHE_DISABLE_ARGUMENTS,
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *arguments,
    ]
    stdin_file: Any | None = None
    try:
        _revalidate_bound_root(bound_root)
        if stdin_payload is not None:
            _consume_operation_budget(
                bound_root.operation,
                byte_count=len(stdin_payload),
                label="preparing bounded Git stdin",
            )
            stdin_file = tempfile.TemporaryFile()
            stdin_file.write(stdin_payload)
            stdin_file.flush()
            stdin_file.seek(0)
        return_code, stdout, stderr = _launch_and_collect_bounded_process(
            lambda owner: _popen_from_bound_directory(
                git_command,
                owner=owner,
                directory_fd=bound_root.git_control.private.fd,
                directory_identity=bound_root.git_control.private.identity,
                directory_access_policy=bound_root.git_control.private.access_policy,
                directory_label="private Git control snapshot",
                control_root=bound_root,
                executable_binding=_bound_private_git_executable(bound_root),
                stdin=subprocess.DEVNULL if stdin_file is None else stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_git_environment(),
                start_new_session=True,
            ),
            lambda process, owner: _collect_bounded_git_output(
                process,
                bound_root.operation,
                owner=owner,
            ),
            label="Git verification",
        )
    except OSError as error:
        raise MirrorSyncError(
            f"cannot run bounded Git verification: {error}"
        ) from error
    finally:
        if stdin_file is not None:
            stdin_file.close()
        _revalidate_bound_root(bound_root)
    if return_code == 0:
        return stdout
    detail = stderr.decode("utf-8", errors="replace").strip()
    if len(detail) > 1000:
        detail = detail[:1000] + "..."
    raise MirrorSyncError(
        "Git verification failed"
        + (f": {detail}" if detail else f" (exit {return_code})")
    )


def _verify_private_git_object_integrity(bound_root: BoundRoot) -> None:
    binding = bound_root.git_control
    if binding is None:
        raise MirrorSyncError(
            "Git control plane must be bound before object integrity inspection"
        )
    if binding.object_integrity_verified:
        _revalidate_bound_root(bound_root)
        return
    if not binding.static_profile_verified:
        raise MirrorSyncError(
            "Git static repository profile must complete before object integrity "
            "inspection"
        )
    try:
        _run_private_git_process(
            bound_root,
            "fsck",
            "--full",
            "--strict",
            "--no-dangling",
            "--no-progress",
            "--no-reflogs",
        )
    except MirrorSyncError as error:
        raise MirrorSyncError(
            f"private Git full object integrity check failed: {error}"
        ) from error
    _revalidate_bound_root(bound_root)
    binding.object_integrity_verified = True


def _run_git_process(
    bound_root: BoundRoot,
    *arguments: str,
    stdin_payload: bytes | None = None,
) -> bytes:
    binding = bound_root.git_control
    if binding is None:
        raise MirrorSyncError(
            "Git control plane must be bound before repository verification"
        )
    if not binding.object_integrity_verified:
        raise MirrorSyncError(
            "private Git full object integrity check has not completed"
        )
    return _run_private_git_process(
        bound_root,
        *arguments,
        stdin_payload=stdin_payload,
    )


def _ensure_git_control_binding(root: BoundRoot) -> None:
    if root.git_control is not None:
        if not root.git_capability_verified:
            raise MirrorSyncError("Git capability gate has not completed")
        if not root.git_control.static_profile_verified:
            raise MirrorSyncError("Git static repository profile has not completed")
        if not root.git_control.object_integrity_verified:
            _verify_private_git_object_integrity(root)
        _revalidate_bound_root(root)
        return
    marker = _bind_git_marker(root)
    controls: list[ControlObjectBinding] = []
    commondir_file: ControlObjectBinding | None = None
    commondir_name_set: ControlNameSetBinding | None = None
    private_path: Path | None = None
    private: ControlObjectBinding | None = None
    private_objects: ControlObjectBinding | None = None
    private_parent: ControlObjectBinding | None = None
    private_name: str | None = None
    private_manifest: tuple[tuple[object, ...], ...] = ()
    private_cleanup_manifest: tuple[tuple[object, ...], ...] = ()
    private_objects_manifest: tuple[tuple[object, ...], ...] = ()
    source_files: tuple[ControlObjectBinding, ...] = ()
    source_directories: tuple[ControlObjectBinding, ...] = ()
    source_absences: tuple[ControlAbsenceBinding, ...] = ()
    acquired_source_controls: list[ControlObjectBinding] = []
    prepared_private_git_executable: PrivateGitExecutableBinding | None = None
    owner_record: ControlObjectBinding | None = None
    owner_record_name: str | None = None
    owner_nonce: str | None = None
    try:
        _revalidate_control_object(root, marker)
        marker_metadata = os.fstat(marker.fd)
        if stat.S_ISDIR(marker_metadata.st_mode):
            admin_path = root.path / ".git"
            admin = _duplicate_directory_control(
                marker,
                admin_path,
                "Git admin directory",
            )
        else:
            admin_path = _parse_control_path(
                _bound_control_payload(marker),
                prefix=b"gitdir: ",
                base=root.path,
                label="Git marker",
            )
            admin = _bind_absolute_control_object(
                admin_path,
                "Git admin directory",
                require_directory=True,
            )
        controls.append(admin)
        commondir_file, commondir_name_set = _bind_unique_relative_control_file(
            admin,
            "commondir",
            "Git commondir control file",
        )
        _revalidate_control_name_set(root, commondir_name_set)
        if commondir_file is None:
            common_path = admin_path
            common = _duplicate_directory_control(
                admin,
                common_path,
                "Git common directory",
            )
        else:
            common_path = _parse_control_path(
                _bound_control_payload(commondir_file),
                prefix=b"",
                base=admin_path,
                label="Git commondir control file",
            )
            common = _bind_absolute_control_object(
                common_path,
                "Git common directory",
                require_directory=True,
            )
        controls.append(common)
        common_commondir_absence = _bind_collision_aware_control_absence(
            common,
            "commondir",
            "Git common-directory commondir control file",
        )
        objects = _bind_absolute_control_object(
            common_path / "objects",
            "Git object directory",
            require_directory=True,
        )
        controls.append(objects)
        for binding in (marker, admin, common, objects):
            _revalidate_control_object(root, binding)
        _revalidate_control_name_set(root, commondir_name_set)
        _revalidate_control_absence(root, common_commondir_absence)
        if root.private_git_executable is None:
            root.private_git_executable = _prepare_private_git_executable(
                root,
                admin,
                common,
            )
        _verify_git_capability(root)
        for binding in (marker, admin, common, objects):
            _revalidate_control_object(root, binding)
        _revalidate_control_name_set(root, commondir_name_set)
        prepared_private_git_executable = root.private_git_executable
        assert prepared_private_git_executable is not None
        (
            private_parent,
            private_name,
            private_path,
            private,
            source_files,
            private_manifest,
            owner_record,
            owner_record_name,
            owner_nonce,
        ) = _materialize_private_git_control(
            root,
            admin,
            common,
            commondir_name_set,
            common_commondir_absence,
            prepared_private_git_executable,
        )
        private_cleanup_manifest = tuple(
            sorted(
                (
                    *private_manifest,
                    *prepared_private_git_executable.private_manifest,
                ),
                key=lambda record: str(record[1]),
            )
        )
        _revalidate_control_name_set(root, commondir_name_set)
        private_commondir_absence = _bind_collision_aware_control_absence(
            private,
            "commondir",
            "private Git commondir control file",
        )
        _revalidate_control_absence(root, common_commondir_absence)
        _revalidate_control_absence(root, private_commondir_absence)
        private_objects = _bind_relative_control_directory(
            private,
            PRIVATE_OBJECTS_PATH.as_posix(),
            "private Git object directory",
        )
        if private_objects is None:
            raise MirrorSyncError("private Git object directory is missing")
        private_objects_manifest = _bind_private_git_objects_manifest(
            private_objects.fd,
            root.operation,
        )
        (
            source_files,
            source_directories,
            source_absences,
        ) = _bind_git_source_controls(
            admin,
            common,
            source_files,
            (
                common_commondir_absence,
                private_commondir_absence,
            ),
            private_manifest,
            acquired_source_controls,
        )
        _revalidate_control_object(root, marker)
        assert private_parent is not None
        assert private_name is not None
        assert private_path is not None
        assert private is not None
        assert owner_record is not None
        assert owner_record_name is not None
        assert owner_nonce is not None
        root.git_control = GitControlBinding(
            marker=marker,
            admin=admin,
            commondir_file=commondir_file,
            commondir_name_set=commondir_name_set,
            common=common,
            objects=objects,
            source_files=source_files,
            source_directories=source_directories,
            source_absences=source_absences,
            private_parent=private_parent,
            private=private,
            private_objects=private_objects,
            private_git_executable=prepared_private_git_executable.executable,
            private_git_executable_name=(
                prepared_private_git_executable.executable_name
            ),
            private_name=private_name,
            private_path=private_path,
            private_manifest=private_manifest,
            private_cleanup_manifest=private_cleanup_manifest,
            private_objects_manifest=private_objects_manifest,
            owner_record=owner_record,
            owner_record_name=owner_record_name,
            owner_nonce=owner_nonce,
            static_profile_verified=False,
            object_integrity_verified=False,
        )
        root.private_git_executable = None
        _verify_static_git_profile(root)
        _verify_private_git_object_integrity(root)
        _revalidate_bound_root(root)
    except BaseException as setup_error:
        cleanup_errors: list[str] = []
        root.git_control = None
        if (
            private_parent is not None
            and private is not None
            and private_name is not None
        ):
            try:
                if (
                    owner_record is not None
                    and owner_record_name is not None
                    and owner_nonce is not None
                ):
                    _cleanup_private_git_control(
                        root,
                        private_parent,
                        private,
                        private_name,
                        private_cleanup_manifest or None,
                        owner_record,
                        owner_record_name,
                        owner_nonce,
                    )
                else:
                    _remove_bound_private_directory(
                        root,
                        private_parent,
                        private,
                        private_name,
                        private_cleanup_manifest or None,
                    )
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    "private Git control cleanup: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        cleanup_errors.extend(
            _close_control_bindings_best_effort(
                (
                    marker,
                    commondir_file,
                    *source_files,
                    *source_directories,
                    *acquired_source_controls,
                    private_objects,
                    owner_record,
                    (
                        None
                        if prepared_private_git_executable is None
                        else prepared_private_git_executable.executable
                    ),
                    private,
                    *controls,
                )
            )
        )
        if private_parent is not None:
            try:
                _close_private_tool_root(private_parent)
            except MirrorSyncError as close_error:
                cleanup_errors.append(f"retained context close: {close_error}")
        if root.private_git_executable is prepared_private_git_executable:
            root.private_git_executable = None
        if cleanup_errors:
            raise MirrorSyncError(
                f"{setup_error}; secondary Git control binding cleanup "
                f"failures: {'; '.join(cleanup_errors)}"
            ) from setup_error
        raise


def _run_git(repository_root: Root, *arguments: str) -> bytes:
    if isinstance(repository_root, BoundRoot):
        bound_root = repository_root
        close_root = False
    else:
        bound_root = _bind_root(Path(repository_root))
        close_root = True
    try:
        _ensure_git_control_binding(bound_root)
        return _run_git_process(
            bound_root,
            *arguments,
        )
    finally:
        if close_root:
            _finish_bound_roots(bound_root)


def _read_sync_history_allowed_paths(
    path: Path,
) -> tuple[frozenset[bytes], bytes]:
    path = Path(os.path.abspath(path))
    file_fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(path, flags)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > MAX_SYNC_HISTORY_ALLOWED_PATH_BYTES
        ):
            raise MirrorSyncError(
                "sync history allowed-path input must be a bounded "
                "current-user-owned regular file"
            )

        def read_payload() -> bytes:
            payload = bytearray()
            while len(payload) <= MAX_SYNC_HISTORY_ALLOWED_PATH_BYTES:
                chunk = os.read(
                    file_fd,
                    min(
                        64 * 1024,
                        MAX_SYNC_HISTORY_ALLOWED_PATH_BYTES + 1 - len(payload),
                    ),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_SYNC_HISTORY_ALLOWED_PATH_BYTES:
                raise MirrorSyncError(
                    "sync history allowed-path input exceeds its byte limit"
                )
            return bytes(payload)

        payload = read_payload()
        os.lseek(file_fd, 0, os.SEEK_SET)
        confirmed_payload = read_payload()
        after = os.fstat(file_fd)
        named = os.lstat(path)
        expected_metadata = (
            before.st_dev,
            before.st_ino,
            stat.S_IFMT(before.st_mode),
            stat.S_IMODE(before.st_mode),
            before.st_uid,
            before.st_gid,
            before.st_size,
        )
        if (
            payload != confirmed_payload
            or (
                after.st_dev,
                after.st_ino,
                stat.S_IFMT(after.st_mode),
                stat.S_IMODE(after.st_mode),
                after.st_uid,
                after.st_gid,
                after.st_size,
            )
            != expected_metadata
            or (
                named.st_dev,
                named.st_ino,
                stat.S_IFMT(named.st_mode),
                stat.S_IMODE(named.st_mode),
                named.st_uid,
                named.st_gid,
                named.st_size,
            )
            != expected_metadata
        ):
            raise MirrorSyncError("sync history allowed-path input changed during read")
    except OSError as error:
        raise MirrorSyncError(
            f"cannot read sync history allowed-path input: {error}"
        ) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)

    try:
        raw = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except MirrorSyncError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise MirrorSyncError(
            "sync history allowed-path input is not valid JSON"
        ) from error
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) for item in raw)
        or raw != sorted(set(raw))
    ):
        raise MirrorSyncError(
            "sync history allowed-path input must be one nonempty sorted "
            "unique string array"
        )
    encoded_paths: set[bytes] = set()
    for raw_path in raw:
        normalized = _validate_relative_path(
            raw_path,
            "sync history allowed path",
        )
        if normalized.as_posix() != raw_path:
            raise MirrorSyncError(
                f"sync history allowed path is not canonical: {raw_path!r}"
            )
        encoded = raw_path.encode("utf-8", errors="strict")
        if encoded in encoded_paths:
            raise MirrorSyncError("sync history allowed-path input is ambiguous")
        encoded_paths.add(encoded)
    canonical_payload = _canonical_json(raw, pretty=False)
    return frozenset(encoded_paths), hashlib.sha256(canonical_payload).digest()


def _sync_history_digest_record(
    digest: Any,
    label: bytes,
    *fields: bytes,
) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(fields).to_bytes(4, "big"))
    for field_payload in fields:
        digest.update(len(field_payload).to_bytes(8, "big"))
        digest.update(field_payload)


def _parse_sync_history_topology(
    payload: bytes,
    *,
    head_sha: str,
    base_sha: str,
    limits: SyncHistoryLimits,
) -> list[tuple[str, tuple[str, ...]]]:
    try:
        text = payload.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise MirrorSyncError(
            "sync branch topology output is not canonical ASCII"
        ) from error
    lines = text.splitlines()
    if len(lines) > limits.max_commits:
        raise MirrorSyncError(
            f"sync branch history exceeds the {limits.max_commits}-commit limit"
        )
    records: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    parent_edges = 0
    for line in lines:
        fields = line.split(" ")
        if not fields or any(GIT_SHA_RE.fullmatch(field) is None for field in fields):
            raise MirrorSyncError("sync branch topology output is malformed")
        commit_sha, *raw_parents = fields
        if commit_sha in seen:
            raise MirrorSyncError("sync branch topology repeats a commit")
        if not raw_parents:
            raise MirrorSyncError(
                "sync branch exclusive history contains an unrelated root commit"
            )
        if len(raw_parents) > limits.max_parents_per_commit:
            raise MirrorSyncError(
                "sync branch commit exceeds the "
                f"{limits.max_parents_per_commit}-parent limit"
            )
        parent_edges += len(raw_parents)
        if parent_edges > limits.max_parent_edges:
            raise MirrorSyncError(
                "sync branch history exceeds the "
                f"{limits.max_parent_edges}-parent-edge limit"
            )
        seen.add(commit_sha)
        records.append((commit_sha, tuple(raw_parents)))
    if head_sha == base_sha:
        if records:
            raise MirrorSyncError(
                "sync branch topology is nonempty for an identical base/head"
            )
    elif not records or records[-1][0] != head_sha or head_sha not in seen:
        raise MirrorSyncError(
            "sync branch topology does not terminate at the exact head"
        )
    return records


def _parse_sync_history_edge(
    payload: bytes,
    *,
    allowed_paths: frozenset[bytes],
    limits: SyncHistoryLimits,
    counters: dict[str, int],
    blobs: set[str],
    digest: Any,
    parent_sha: str,
    commit_sha: str,
) -> None:
    if payload and not payload.endswith(b"\0"):
        raise MirrorSyncError("sync branch edge output is not NUL terminated")
    records = payload.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if len(records) % 2:
        raise MirrorSyncError("sync branch edge output is malformed")
    zero_object = b"0" * 40
    supported_modes = {b"000000", b"100644", b"100755"}
    for index in range(0, len(records), 2):
        metadata = records[index]
        raw_path = records[index + 1]
        try:
            raw_old_mode, raw_new_mode, raw_old_oid, raw_new_oid, raw_status = (
                metadata.split(b" ", 4)
            )
        except ValueError as error:
            raise MirrorSyncError("sync branch edge metadata is malformed") from error
        if not raw_old_mode.startswith(b":"):
            raise MirrorSyncError("sync branch edge metadata is malformed")
        raw_old_mode = raw_old_mode[1:]
        if (
            raw_old_mode not in supported_modes
            or raw_new_mode not in supported_modes
            or len(raw_old_oid) != 40
            or len(raw_new_oid) != 40
            or (
                raw_old_oid != zero_object
                and re.fullmatch(rb"[0-9a-f]{40}", raw_old_oid) is None
            )
            or (
                raw_new_oid != zero_object
                and re.fullmatch(rb"[0-9a-f]{40}", raw_new_oid) is None
            )
            or raw_status not in {b"A", b"D", b"M"}
            or not raw_path
        ):
            raise MirrorSyncError("sync branch edge metadata is unsupported")
        if (
            (
                raw_status == b"A"
                and (
                    raw_old_mode != b"000000"
                    or raw_old_oid != zero_object
                    or raw_new_mode == b"000000"
                    or raw_new_oid == zero_object
                )
            )
            or (
                raw_status == b"D"
                and (
                    raw_old_mode == b"000000"
                    or raw_old_oid == zero_object
                    or raw_new_mode != b"000000"
                    or raw_new_oid != zero_object
                )
            )
            or (
                raw_status == b"M"
                and (
                    raw_old_mode == b"000000"
                    or raw_old_oid == zero_object
                    or raw_new_mode == b"000000"
                    or raw_new_oid == zero_object
                )
            )
        ):
            raise MirrorSyncError(
                "sync branch edge status is inconsistent with its object record"
            )
        if raw_path not in allowed_paths:
            preview = repr(raw_path[:256])
            raise MirrorSyncError(
                "sync branch history changes a path outside the generated "
                f"allowed set: {preview}"
            )
        counters["path_events"] += 1
        counters["path_bytes"] += len(raw_path)
        if counters["path_events"] > limits.max_path_events:
            raise MirrorSyncError(
                "sync branch history exceeds the "
                f"{limits.max_path_events}-path-event limit"
            )
        if counters["path_bytes"] > limits.max_path_bytes:
            raise MirrorSyncError(
                "sync branch history exceeds the "
                f"{limits.max_path_bytes}-path-byte limit"
            )
        _sync_history_digest_record(
            digest,
            b"edge-change",
            parent_sha.encode("ascii"),
            commit_sha.encode("ascii"),
            raw_old_mode,
            raw_new_mode,
            raw_old_oid,
            raw_new_oid,
            raw_status,
            raw_path,
        )
        if raw_new_mode != b"000000":
            blobs.add(raw_new_oid.decode("ascii"))
            if len(blobs) > limits.max_blobs:
                raise MirrorSyncError(
                    f"sync branch history exceeds the {limits.max_blobs}-blob limit"
                )


def _sync_history_blob_sizes(
    target_root: BoundRoot,
    blobs: set[str],
    *,
    limits: SyncHistoryLimits,
    digest: Any,
) -> int:
    if not blobs:
        return 0
    ordered = sorted(blobs)
    input_payload = b"".join(object_id.encode("ascii") + b"\n" for object_id in ordered)
    output = _run_git_process(
        target_root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        stdin_payload=input_payload,
    )
    try:
        lines = output.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise MirrorSyncError(
            "sync branch blob inventory is not canonical ASCII"
        ) from error
    if len(lines) != len(ordered):
        raise MirrorSyncError("sync branch blob inventory is incomplete")
    total_bytes = 0
    for expected_oid, line in zip(ordered, lines):
        fields = line.split(" ")
        if (
            len(fields) != 3
            or fields[0] != expected_oid
            or fields[1] != "blob"
            or not fields[2].isdecimal()
        ):
            raise MirrorSyncError("sync branch blob inventory is malformed")
        size = int(fields[2])
        total_bytes += size
        if total_bytes > limits.max_blob_bytes:
            raise MirrorSyncError(
                "sync branch history exceeds the "
                f"{limits.max_blob_bytes}-logical-blob-byte limit"
            )
        _sync_history_digest_record(
            digest,
            b"blob",
            expected_oid.encode("ascii"),
            str(size).encode("ascii"),
        )
        payload = _run_git(
            target_root,
            "cat-file",
            "blob",
            expected_oid,
        )
        if len(payload) != size:
            raise MirrorSyncError(
                "sync branch blob content does not match its declared size"
            )
        if any(pattern.search(payload) for pattern in SYNC_HISTORY_SECRET_PATTERNS):
            raise MirrorSyncError(
                "sync branch history contains a high-confidence secret marker "
                f"in introduced blob {expected_oid}"
            )
    return total_bytes


def _validate_sync_branch_history_bound(
    target_root: BoundRoot,
    base_sha: str,
    head_sha: str,
    worktree_head_sha: str,
    allowed_paths: frozenset[bytes],
    allowed_paths_sha256: bytes,
    *,
    limits: SyncHistoryLimits,
    require_fresh_head: bool,
) -> dict[str, object]:
    history_mirror = MirrorSpec(
        name="sync-history",
        repository="history-only",
        files={},
    )
    initial_index = _target_index_snapshot(
        target_root,
        history_mirror,
    )
    if _current_commit(target_root) != worktree_head_sha:
        raise MirrorSyncError(
            "sync branch worktree HEAD does not match the exact bound worktree head"
        )
    for label, value in (
        ("base", base_sha),
        ("head", head_sha),
        ("worktree head", worktree_head_sha),
    ):
        if GIT_SHA_RE.fullmatch(value) is None:
            raise MirrorSyncError(
                f"sync branch {label} must be one exact lowercase SHA-1"
            )
        resolved = _run_git(
            target_root,
            "rev-parse",
            "--verify",
            f"{value}^{{commit}}",
        )
        if resolved != value.encode("ascii") + b"\n":
            raise MirrorSyncError(
                f"sync branch {label} does not resolve to its exact commit"
            )
    merge_base_output = _run_git(
        target_root,
        "merge-base",
        "--all",
        base_sha,
        head_sha,
    )
    try:
        merge_bases = merge_base_output.decode(
            "ascii",
            errors="strict",
        ).splitlines()
    except UnicodeDecodeError as error:
        raise MirrorSyncError("sync branch merge-base output is malformed") from error
    if len(merge_bases) != 1 or GIT_SHA_RE.fullmatch(merge_bases[0]) is None:
        raise MirrorSyncError(
            "sync branch base/head must have one unambiguous merge base"
        )
    merge_base_sha = merge_bases[0]
    topology_payload = _run_git(
        target_root,
        "rev-list",
        "--reverse",
        "--topo-order",
        "--parents",
        f"{base_sha}..{head_sha}",
        "--",
    )
    topology = _parse_sync_history_topology(
        topology_payload,
        head_sha=head_sha,
        base_sha=base_sha,
        limits=limits,
    )
    if require_fresh_head:
        if merge_base_sha != base_sha:
            raise MirrorSyncError(
                "fresh generated sync head is not based on the exact target base"
            )
        if head_sha == base_sha:
            if topology:
                raise MirrorSyncError(
                    "fresh generated sync history is unexpectedly nonempty"
                )
        elif (
            len(topology) != 1
            or topology[0][0] != head_sha
            or topology[0][1] != (base_sha,)
        ):
            raise MirrorSyncError(
                "fresh generated sync history must contain exactly one "
                "single-parent commit from the exact target base"
            )

    digest = hashlib.sha256()
    _sync_history_digest_record(
        digest,
        b"sync-history-v2",
        base_sha.encode("ascii"),
        head_sha.encode("ascii"),
        worktree_head_sha.encode("ascii"),
        merge_base_sha.encode("ascii"),
        allowed_paths_sha256,
    )
    for commit_sha, parents in topology:
        _sync_history_digest_record(
            digest,
            b"commit",
            commit_sha.encode("ascii"),
            *(parent.encode("ascii") for parent in parents),
        )

    counters = {
        "path_events": 0,
        "path_bytes": 0,
    }
    blobs: set[str] = set()
    parent_edges = 0
    for commit_sha, parents in topology:
        for parent_sha in parents:
            parent_edges += 1
            edge_payload = _run_git(
                target_root,
                "diff-tree",
                "-r",
                "-z",
                "--raw",
                "--no-abbrev",
                "--no-commit-id",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                parent_sha,
                commit_sha,
                "--",
            )
            _parse_sync_history_edge(
                edge_payload,
                allowed_paths=allowed_paths,
                limits=limits,
                counters=counters,
                blobs=blobs,
                digest=digest,
                parent_sha=parent_sha,
                commit_sha=commit_sha,
            )
    blob_bytes = _sync_history_blob_sizes(
        target_root,
        blobs,
        limits=limits,
        digest=digest,
    )
    _require_same_target_index(
        target_root,
        history_mirror,
        initial_index,
    )
    return {
        "schema_version": SYNC_HISTORY_RECEIPT_VERSION,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "worktree_head_sha": worktree_head_sha,
        "merge_base_sha": merge_base_sha,
        "commit_count": len(topology),
        "parent_edge_count": parent_edges,
        "path_event_count": counters["path_events"],
        "path_bytes": counters["path_bytes"],
        "blob_count": len(blobs),
        "blob_bytes": blob_bytes,
        "allowed_paths_sha256": allowed_paths_sha256.hex(),
        "history_sha256": digest.hexdigest(),
        "profile": "fresh-single-commit" if require_fresh_head else "branch-exclusive",
    }


def validate_sync_branch_history(
    target_root: Path,
    base_sha: str,
    head_sha: str,
    allowed_paths_file: Path,
    *,
    limits: SyncHistoryLimits | None = None,
    require_fresh_head: bool = False,
    worktree_head_ref: str | None = None,
) -> dict[str, object]:
    selected_limits = limits or SyncHistoryLimits()
    allowed_paths, allowed_paths_sha256 = _read_sync_history_allowed_paths(
        allowed_paths_file
    )
    operation = _new_operation_budget()
    target = _bind_root(target_root)
    target.operation = operation
    try:
        _ensure_git_control_binding(target)
        initial_head = _current_commit(target)
        selected_worktree_head = worktree_head_ref or head_sha
        receipt = _validate_sync_branch_history_bound(
            target,
            base_sha,
            head_sha,
            selected_worktree_head,
            allowed_paths,
            allowed_paths_sha256,
            limits=selected_limits,
            require_fresh_head=require_fresh_head,
        )
        if _current_commit(target) != initial_head:
            raise MirrorSyncError(
                "sync branch worktree HEAD changed during history validation"
            )
        return receipt
    finally:
        _finish_bound_roots(target)


def _top_literal_pathspec(path: PurePosixPath) -> str:
    return f":(top,literal){path.as_posix()}"


def _verify_git_safety(repository_root: Root) -> None:
    raw_config = _run_git(
        repository_root,
        "config",
        "--local",
        "--no-includes",
        "--null",
        "--list",
    )
    for record in raw_config.split(b"\0"):
        if not record:
            continue
        raw_key = record.split(b"\n", 1)[0]
        key = raw_key.decode("utf-8", errors="strict").casefold()
        if key == "include.path" or (
            key.startswith("includeif.") and key.endswith(".path")
        ):
            raise MirrorSyncError("repository-local Git include paths are not allowed")
    replace_refs = _run_git(
        repository_root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace/",
    )
    if replace_refs.strip():
        raise MirrorSyncError("Git replace refs are not allowed")
    object_inventory = _run_git(repository_root, "count-objects", "-v")
    if any(line.startswith(b"alternate:") for line in object_inventory.splitlines()):
        raise MirrorSyncError("Git object alternates are not allowed")


def _verify_git_root(repository_root: Root) -> None:
    _verify_git_safety(repository_root)
    raw_top_level = _run_git(
        repository_root,
        "rev-parse",
        "--show-toplevel",
    )
    try:
        top_level_text = raw_top_level.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise MirrorSyncError("Git top-level path is not valid UTF-8") from error
    if (
        not top_level_text.endswith("\n")
        or "\n" in top_level_text[:-1]
        or "\0" in top_level_text
    ):
        raise MirrorSyncError("Git top-level query did not return one path")
    top_level_fd = _open_root(Path(top_level_text[:-1]))
    repository_fd, close_repository = _borrow_root_fd(repository_root)
    try:
        same_root = _object_identity(os.fstat(top_level_fd)) == _object_identity(
            os.fstat(repository_fd)
        )
    finally:
        os.close(top_level_fd)
        if close_repository:
            os.close(repository_fd)
    if not same_root:
        raise MirrorSyncError(
            f"target root is not the exact Git top-level: {_root_path(repository_root)}"
        )


def _current_commit(repository_root: Root) -> str:
    _verify_git_root(repository_root)
    raw_commit = (
        _run_git(
            repository_root,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if GIT_SHA_RE.fullmatch(raw_commit) is None:
        raise MirrorSyncError("canonical HEAD is not a full lowercase commit SHA")
    return raw_commit


def _git_mode(raw_mode: bytes, label: str) -> int:
    if raw_mode == b"100755":
        return 0o755
    if raw_mode == b"100644":
        return 0o644
    raise MirrorSyncError(
        f"{label} has unsupported Git mode {raw_mode.decode('ascii', errors='replace')}"
    )


def _git_tree_entry(
    repository_root: Root,
    source_commit: str,
    relative_path: PurePosixPath,
) -> GitEntry | None:
    raw_entry = _run_git(
        repository_root,
        "ls-tree",
        "--full-tree",
        "-z",
        source_commit,
        "--",
        _top_literal_pathspec(relative_path),
    )
    records = [record for record in raw_entry.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise MirrorSyncError(
            f"canonical control/source path is not committed exactly once: "
            f"{relative_path}"
        )
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        raw_mode, raw_kind, raw_object = metadata.split(b" ", 2)
    except ValueError as error:
        raise MirrorSyncError(
            f"cannot parse committed tree entry for {relative_path}"
        ) from error
    if raw_path != relative_path.as_posix().encode("utf-8") or raw_kind != b"blob":
        raise MirrorSyncError(
            f"canonical control/source path is not a regular committed blob: "
            f"{relative_path}"
        )
    mode = _git_mode(
        raw_mode,
        f"committed path {relative_path}",
    )
    object_id = raw_object.decode("ascii", errors="strict")
    if GIT_SHA_RE.fullmatch(object_id) is None:
        raise MirrorSyncError(
            f"committed path has an invalid object id: {relative_path}"
        )
    return GitEntry(
        path=relative_path,
        mode=mode,
        object_id=object_id,
    )


def _git_index_entry(
    repository_root: Root,
    relative_path: PurePosixPath,
) -> GitEntry | None:
    raw_stage = _run_git(
        repository_root,
        "ls-files",
        "--full-name",
        "--stage",
        "-z",
        "--",
        _top_literal_pathspec(relative_path),
    )
    records = [record for record in raw_stage.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise MirrorSyncError(f"index path has ambiguous entries: {relative_path}")
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        raw_mode, raw_object, raw_stage_number = metadata.split(b" ", 2)
    except ValueError as error:
        raise MirrorSyncError(f"cannot parse index entry: {relative_path}") from error
    if raw_path != relative_path.as_posix().encode("utf-8") or raw_stage_number != b"0":
        raise MirrorSyncError(
            f"index path is not an exact stage-0 entry: {relative_path}"
        )
    object_id = raw_object.decode("ascii", errors="strict")
    if GIT_SHA_RE.fullmatch(object_id) is None:
        raise MirrorSyncError(f"index path has an invalid object id: {relative_path}")
    return GitEntry(
        path=relative_path,
        mode=_git_mode(raw_mode, f"index path {relative_path}"),
        object_id=object_id,
    )


def _git_blob_payload(
    repository_root: Root,
    object_id: str,
    relative_path: PurePosixPath,
) -> bytes:
    raw_size = (
        _run_git(
            repository_root,
            "cat-file",
            "-s",
            object_id,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    try:
        blob_size = int(raw_size)
    except ValueError as error:
        raise MirrorSyncError(
            f"cannot parse committed blob size for {relative_path}"
        ) from error
    if blob_size < 0 or blob_size > MAX_SOURCE_BYTES:
        raise MirrorSyncError(
            f"committed blob exceeds the {MAX_SOURCE_BYTES}-byte source limit: "
            f"{relative_path}"
        )
    payload = _run_git(repository_root, "cat-file", "blob", object_id)
    if len(payload) != blob_size:
        raise MirrorSyncError(
            f"committed blob size changed while reading it: {relative_path}"
        )
    return payload


def _git_tree_blob(
    repository_root: Root,
    source_commit: str,
    relative_path: PurePosixPath,
) -> tuple[bytes, int]:
    entry = _git_tree_entry(
        repository_root,
        source_commit,
        relative_path,
    )
    if entry is None:
        raise MirrorSyncError(
            f"canonical control/source path is not committed: {relative_path}"
        )
    return (
        _git_blob_payload(
            repository_root,
            entry.object_id,
            relative_path,
        ),
        entry.mode,
    )


def _source_lock_at_commit(
    repository_root: Root,
    source_commit: str,
) -> SourceLock:
    payload, mode = _git_tree_blob(
        repository_root,
        source_commit,
        LOCK_PATH,
    )
    if mode != 0o644:
        raise MirrorSyncError(f"committed source lock has unsafe mode {mode:04o}")
    return _parse_source_lock(payload)


def _require_ancestral_commit(
    repository_root: Root,
    candidate_commit: str,
    descendant_commit: str,
    label: str,
) -> None:
    if (
        GIT_SHA_RE.fullmatch(candidate_commit) is None
        or GIT_SHA_RE.fullmatch(descendant_commit) is None
    ):
        raise MirrorSyncError(f"{label} must use exact full Git commit IDs")
    object_type = _run_git(
        repository_root,
        "cat-file",
        "-t",
        candidate_commit,
    )
    if object_type != b"commit\n":
        raise MirrorSyncError(f"{label} does not name a Git commit")
    try:
        _run_git(
            repository_root,
            "merge-base",
            "--is-ancestor",
            candidate_commit,
            descendant_commit,
        )
    except MirrorSyncError as error:
        raise MirrorSyncError(
            f"{label} is not an available ancestor of {descendant_commit}"
        ) from error


def _committed_sources(
    repository_root: Root,
    source_commit: str,
    source_lock: SourceLock,
) -> dict[str, tuple[bytes, int]]:
    if (
        not isinstance(source_commit, str)
        or GIT_SHA_RE.fullmatch(source_commit) is None
    ):
        raise MirrorSyncError(
            "source commit must be a full 40-character lowercase Git SHA"
        )
    committed_sources: dict[str, tuple[bytes, int]] = {}
    for name, source in sorted(source_lock.sources.items()):
        payload, mode = _git_tree_blob(
            repository_root,
            source_commit,
            source.path,
        )
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise MirrorSyncError(
                f"committed source hash does not match the lock: {source.path}"
            )
        if mode != source.mode:
            raise MirrorSyncError(
                f"committed source mode does not match the lock: {source.path}"
            )
        committed_sources[name] = payload, mode
    return committed_sources


def _verify_committed_sources(
    repository_root: Root,
    source_commit: str,
    source_lock: SourceLock,
) -> dict[str, tuple[bytes, int]]:
    if (
        not isinstance(source_commit, str)
        or GIT_SHA_RE.fullmatch(source_commit) is None
    ):
        raise MirrorSyncError(
            "source commit must be a full 40-character lowercase Git SHA"
        )
    head_commit = _current_commit(repository_root)
    if head_commit != source_commit:
        raise MirrorSyncError(
            f"source commit does not match canonical HEAD: "
            f"{source_commit} != {head_commit}"
        )
    controlled_paths = sorted(
        {
            LOCK_PATH,
            GENERATOR_PATH,
            RULES_PATH,
            *(source.path for source in source_lock.sources.values()),
        },
        key=PurePosixPath.as_posix,
    )
    for relative_path in controlled_paths:
        committed_entry = _git_tree_entry(
            repository_root,
            source_commit,
            relative_path,
        )
        if committed_entry is None:
            raise MirrorSyncError(
                f"canonical control/source path is dirty or untracked because "
                f"it is not committed: {relative_path}"
            )
        index_entry = _git_index_entry(
            repository_root,
            relative_path,
        )
        if index_entry != committed_entry:
            raise MirrorSyncError(
                f"canonical index differs from source commit: {relative_path}"
            )
        worktree_payload, worktree_mode = _safe_read_relative(
            repository_root,
            relative_path,
        )
        committed_payload = _git_blob_payload(
            repository_root,
            committed_entry.object_id,
            relative_path,
        )
        if (
            worktree_payload != committed_payload
            or worktree_mode != committed_entry.mode
        ):
            raise MirrorSyncError(
                f"canonical path is dirty or untracked relative to source "
                f"commit: {relative_path}"
            )
    return _committed_sources(
        repository_root,
        source_commit,
        source_lock,
    )


def _repository_from_remote(raw_url: str) -> str:
    url = raw_url.strip().rstrip("/")
    prefixes = (
        "git@github.com:",
        "https://github.com/",
        "ssh://git@github.com/",
    )
    for prefix in prefixes:
        if url.startswith(prefix):
            repository = url[len(prefix) :]
            if repository.endswith(".git"):
                repository = repository[:-4]
            return _validate_repository(repository, "origin repository")
    raise MirrorSyncError(
        "repository origin must be an exact github.com SSH or HTTPS repository URL"
    )


def _verify_repository_origin(
    repository_root: Root,
    expected_repository: str,
    role: str,
) -> None:
    _verify_git_root(repository_root)
    raw_remote = _run_git(
        repository_root,
        "remote",
        "get-url",
        "origin",
    ).decode("utf-8", errors="strict")
    actual_repository = _repository_from_remote(raw_remote)
    if _repository_identity(actual_repository) != _repository_identity(
        expected_repository
    ):
        raise MirrorSyncError(
            f"{role} repository does not match {expected_repository}: "
            f"{actual_repository}"
        )


def _verify_target_repository(
    target_root: Root,
    expected_repository: str,
) -> None:
    _verify_repository_origin(
        target_root,
        expected_repository,
        "target",
    )


def _verify_canonical_repository(
    repository_root: Root,
    expected_repository: str,
) -> None:
    _verify_repository_origin(
        repository_root,
        expected_repository,
        "canonical",
    )


def _paths_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    left_parts = _path_collision_key(left)
    right_parts = _path_collision_key(right)
    return (
        left_parts[: len(right_parts)] == right_parts
        or right_parts[: len(left_parts)] == left_parts
    )


def _control_path_below_root(
    root: BoundRoot,
    binding: ControlObjectBinding,
) -> PurePosixPath | None:
    if binding.relative_path is not None:
        return binding.relative_path
    assert binding.path is not None
    root_path = Path(os.path.realpath(root.path))
    control_path = Path(os.path.realpath(binding.path))
    try:
        relative = control_path.relative_to(root_path)
    except ValueError:
        return None
    if not relative.parts:
        return PurePosixPath(".")
    return PurePosixPath(*relative.parts)


def _reject_git_control_targets(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> None:
    _ensure_git_control_binding(target_root)
    assert target_root.git_control is not None
    controls = (
        target_root.git_control.marker,
        target_root.git_control.admin,
        target_root.git_control.common,
        target_root.git_control.objects,
    )
    protected = [
        path
        for binding in controls
        if (path := _control_path_below_root(target_root, binding)) is not None
    ]
    for target_path in {*mirror.files.values(), *additional_paths}:
        if target_path.parts[0].casefold() == ".git" or any(
            _paths_overlap(target_path, control_path) for control_path in protected
        ):
            raise MirrorSyncError(
                f"managed target overlaps the Git control plane: {target_path}"
            )


def _target_clean_snapshot(
    target_root: BoundRoot,
    path: PurePosixPath,
    head_commit: str,
) -> FileSnapshot | None:
    snapshot = _optional_safe_read_snapshot(target_root, path)
    index_entry = _git_index_entry(
        target_root,
        path,
    )
    head_entry = _git_tree_entry(
        target_root,
        head_commit,
        path,
    )
    if index_entry is None:
        if snapshot is not None or head_entry is not None:
            raise MirrorSyncError(
                f"target managed path is dirty or untracked relative to "
                f"HEAD/index/worktree: {path}"
            )
        return None
    if head_entry != index_entry:
        raise MirrorSyncError(f"target managed index differs from HEAD: {path}")
    if snapshot is None:
        raise MirrorSyncError(
            f"target managed path is dirty or untracked because its "
            f"stage-0 entry is missing from the worktree: {path}"
        )
    index_payload = _git_blob_payload(
        target_root,
        index_entry.object_id,
        path,
    )
    if snapshot.payload != index_payload or snapshot.mode != index_entry.mode:
        raise MirrorSyncError(
            f"target managed path is dirty or untracked relative to its "
            f"clean index entry: {path}"
        )
    return snapshot


def _target_managed_snapshots(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> dict[PurePosixPath, FileSnapshot | None]:
    paths = sorted(
        {RECEIPT_PATH, *mirror.files.values(), *additional_paths},
        key=PurePosixPath.as_posix,
    )
    head_commit = _current_commit(target_root)
    snapshots: dict[PurePosixPath, FileSnapshot | None] = {}
    for path in paths:
        snapshots[path] = _target_clean_snapshot(
            target_root,
            path,
            head_commit,
        )
    return snapshots


def _require_target_head_index_parity(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> None:
    head_commit = _current_commit(target_root)
    for path in _mirror_managed_paths(mirror, additional_paths):
        if _git_index_entry(target_root, path) != _git_tree_entry(
            target_root,
            head_commit,
            path,
        ):
            raise MirrorSyncError(
                f"target managed stage-0 index differs from HEAD: {path}"
            )


def _parse_consumer_tracked_entries(
    target_root: BoundRoot,
    payload: bytes,
    *,
    source: str,
) -> dict[bytes, tuple[bytes, bytes]]:
    if payload and not payload.endswith(b"\0"):
        raise MirrorSyncError(
            f"consumer {source} tracked-path inventory is not NUL terminated"
        )
    records = payload.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    if len(records) > MAX_CONSUMER_TRACKED_ENTRIES:
        raise MirrorSyncError(
            f"consumer {source} tracked-path inventory exceeds the "
            f"{MAX_CONSUMER_TRACKED_ENTRIES}-entry limit"
        )
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in records:
        _consume_operation_budget(
            target_root.operation,
            entry_count=1,
            label=f"parsing consumer {source} tracked paths",
        )
        try:
            metadata, raw_path = record.split(b"\t", 1)
            if source == "index":
                raw_mode, raw_object, raw_stage = metadata.split(b" ", 2)
                if raw_stage != b"0":
                    raise MirrorSyncError(
                        "consumer index contains unmerged/non-stage-0 entries"
                    )
                raw_kind = b"commit" if raw_mode == b"160000" else b"blob"
            else:
                raw_mode, raw_kind, raw_object = metadata.split(b" ", 2)
        except ValueError as error:
            raise MirrorSyncError(
                f"cannot parse consumer {source} tracked-path entry"
            ) from error
        if raw_mode not in {b"100644", b"100755", b"120000", b"160000"}:
            raise MirrorSyncError(
                f"consumer {source} tracked path has unsupported mode"
            )
        expected_kind = b"commit" if raw_mode == b"160000" else b"blob"
        try:
            object_id = raw_object.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise MirrorSyncError(
                f"consumer {source} tracked path has invalid object metadata"
            ) from error
        if raw_kind != expected_kind or GIT_SHA_RE.fullmatch(object_id) is None:
            raise MirrorSyncError(
                f"consumer {source} tracked path has invalid object metadata"
            )
        if not raw_path or raw_path in entries:
            raise MirrorSyncError(
                f"consumer {source} tracked-path inventory is ambiguous"
            )
        try:
            decoded_path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise MirrorSyncError(
                f"consumer {source} tracked path is not valid UTF-8"
            ) from error
        path = _validate_relative_path(
            decoded_path,
            f"consumer {source} tracked path",
        )
        if path.as_posix().encode("utf-8") != raw_path:
            raise MirrorSyncError(
                f"consumer {source} tracked path has ambiguous encoding"
            )
        if path.parts[0].casefold() == ".git":
            raise MirrorSyncError(
                f"consumer {source} tracked path enters the Git control plane: {path}"
            )
        entries[raw_path] = (raw_mode, raw_object)
    return entries


def _validate_consumer_portable_layout(
    managed_paths: set[PurePosixPath],
    tracked_paths: set[PurePosixPath],
    tracked_symlink_paths: set[PurePosixPath],
    mirror_name: str,
) -> None:
    protected_paths = {
        *managed_paths,
        TRANSACTION_PATH,
        TRANSACTION_TEMP_PATH,
        TRANSACTION_COMPLETE_PATH,
        *(path.parent / _exchange_journal_name(path) for path in managed_paths),
    }
    entries = [
        ("protected", path, _path_collision_key(path)) for path in protected_paths
    ]
    entries.extend(
        ("tracked", path, _path_collision_key(path))
        for path in tracked_paths
        if path not in managed_paths
    )
    trie: dict[str, Any] = {}
    terminal_key = object()

    def reject_cross_kind(
        kind: str,
        path: PurePosixPath,
        prior_kind: str,
        prior_path: PurePosixPath,
    ) -> None:
        if kind == prior_kind:
            return
        tracked = path if kind == "tracked" else prior_path
        protected = prior_path if kind == "tracked" else path
        if (
            tracked in tracked_symlink_paths
            and tracked != protected
            and protected.is_relative_to(tracked)
        ):
            raise MirrorSyncError(
                f"directory ancestor must not be a symlink: {tracked}"
            )
        raise MirrorSyncError(
            f"consumer tracked path collides with mirror {mirror_name} "
            f"managed/recovery path under NFC+casefold portability rules: "
            f"{tracked} and {protected}"
        )

    for kind, path, collision_key in sorted(
        entries,
        key=lambda item: (len(item[2]), item[2], item[0]),
    ):
        node = trie
        for component in collision_key:
            for prior_kind, prior_path in node.get(terminal_key, ()):
                reject_cross_kind(kind, path, prior_kind, prior_path)
            node = node.setdefault(component, {})
        existing = node.setdefault(terminal_key, [])
        for prior_kind, prior_path in existing:
            reject_cross_kind(kind, path, prior_kind, prior_path)
        existing.append((kind, path))


def _target_index_snapshot(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> bytes:
    head_before = _current_commit(target_root)
    raw_index = _run_git(
        target_root,
        "ls-files",
        "--full-name",
        "--stage",
        "-z",
    )
    raw_head = _run_git(
        target_root,
        "ls-tree",
        "--full-tree",
        "-r",
        "-z",
        head_before,
    )
    index_entries = _parse_consumer_tracked_entries(
        target_root,
        raw_index,
        source="index",
    )
    head_entries = _parse_consumer_tracked_entries(
        target_root,
        raw_head,
        source="HEAD",
    )
    if index_entries != head_entries:
        raise MirrorSyncError("consumer complete stage-0 index differs from HEAD")
    head_after = _current_commit(target_root)
    if head_after != head_before:
        raise MirrorSyncError("consumer HEAD changed during tracked-path inventory")
    try:
        tracked_entries = {
            _validate_relative_path(
                raw_path.decode("utf-8", errors="strict"),
                "consumer tracked path",
            ): metadata
            for raw_path, metadata in index_entries.items()
        }
    except UnicodeDecodeError as error:
        raise MirrorSyncError("consumer tracked path is not valid UTF-8") from error
    tracked_paths = set(tracked_entries)
    tracked_symlink_paths = {
        path
        for path, (mode, _object_id) in tracked_entries.items()
        if mode == b"120000"
    }
    managed_paths = {
        RECEIPT_PATH,
        *mirror.files.values(),
        *additional_paths,
    }
    _validate_consumer_portable_layout(
        managed_paths,
        tracked_paths,
        tracked_symlink_paths,
        mirror.name,
    )
    return b"".join(
        (
            b"consumer-tracked-namespace-v1\0",
            head_before.encode("ascii"),
            len(raw_index).to_bytes(8, "big"),
            raw_index,
            len(raw_head).to_bytes(8, "big"),
            raw_head,
        )
    )


def _require_same_target_index(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    expected: bytes,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> None:
    if _target_index_snapshot(target_root, mirror, additional_paths) != expected:
        raise MirrorSyncError("target managed stage-0 index changed during generation")


def _desired_file_record(payload: bytes, mode: int) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mode": mode,
        "size": len(payload),
    }


def _snapshot_matches_desired(
    snapshot: FileSnapshot | None,
    record: object,
) -> bool:
    if record is None:
        return snapshot is None
    if snapshot is None or not isinstance(record, dict):
        return False
    return record == _desired_file_record(snapshot.payload, snapshot.mode)


def _desired_path_record(
    desired: tuple[bytes, int] | None,
) -> dict[str, object] | None:
    return None if desired is None else _desired_file_record(*desired)


def _mirror_managed_paths(
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> list[PurePosixPath]:
    return sorted(
        {RECEIPT_PATH, *mirror.files.values(), *additional_paths},
        key=PurePosixPath.as_posix,
    )


def _mirror_operation_paths(
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> list[PurePosixPath]:
    managed = _mirror_managed_paths(mirror, additional_paths)
    return sorted(
        {
            *managed,
            TRANSACTION_PATH,
            TRANSACTION_TEMP_PATH,
            TRANSACTION_COMPLETE_PATH,
            *(path.parent / _exchange_journal_name(path) for path in managed),
        },
        key=PurePosixPath.as_posix,
    )


def _recover_mirror_exchange_journals(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> None:
    for path in _mirror_managed_paths(mirror, additional_paths):
        _recover_exchange_journal(target_root, path)


def _reject_pending_exchange_journals(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> None:
    if (
        _optional_safe_read_snapshot(target_root, TRANSACTION_PATH) is not None
        or _optional_safe_read_snapshot(
            target_root,
            TRANSACTION_TEMP_PATH,
        )
        is not None
        or _optional_safe_read_snapshot(
            target_root,
            TRANSACTION_COMPLETE_PATH,
        )
        is not None
    ):
        raise MirrorSyncError(
            "consumer mirror has a pending generation transaction; rerun "
            "generate before check"
        )
    for path in _mirror_managed_paths(mirror, additional_paths):
        journal_path = path.parent / _exchange_journal_name(path)
        if _optional_safe_read_snapshot(target_root, journal_path) is not None:
            raise MirrorSyncError(
                f"consumer mirror has a pending exchange journal: {path}"
            )


def _transaction_document(
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    initial_index: bytes,
    initial_snapshots: dict[PurePosixPath, FileSnapshot | None],
    desired_files: dict[PurePosixPath, tuple[bytes, int] | None],
    invalid_receipt: bytes,
    final_receipt: bytes,
) -> dict[str, object]:
    return {
        "version": 1,
        "canonical_repository": source_lock.canonical_repository,
        "canonical_commit": source_commit,
        "mirror": mirror.name,
        "mirror_repository": mirror.repository,
        "index_sha256": hashlib.sha256(initial_index).hexdigest(),
        "files": {
            path.as_posix(): {
                "initial": (
                    None
                    if initial_snapshots[path] is None
                    else _snapshot_record(initial_snapshots[path])
                ),
                "desired": _desired_path_record(desired_files[path]),
            }
            for path in sorted(desired_files, key=PurePosixPath.as_posix)
        },
        "receipt": {
            "initial": (
                None
                if initial_snapshots[RECEIPT_PATH] is None
                else _snapshot_record(initial_snapshots[RECEIPT_PATH])
            ),
            "invalid": _desired_file_record(invalid_receipt, 0o644),
            "final": _desired_file_record(final_receipt, 0o644),
        },
    }


def _write_transaction_journal(
    target_root: BoundRoot,
    document: dict[str, object],
) -> FileSnapshot:
    payload = (
        json.dumps(
            document,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_SOURCE_BYTES:
        raise MirrorSyncError("generation transaction journal is too large")
    if _optional_safe_read_snapshot(target_root, TRANSACTION_PATH) is not None:
        raise MirrorSyncError("generation transaction journal already exists")
    temporary_name = TRANSACTION_TEMP_PATH.as_posix()
    temporary_fd = -1
    temporary_snapshot: FileSnapshot | None = None
    try:
        temporary_fd = os.open(
            temporary_name,
            _FILE_WRITE_FLAGS,
            0o600,
            dir_fd=target_root.fd,
        )
        os.fchmod(temporary_fd, 0o600)
        _write_all(temporary_fd, payload, TRANSACTION_PATH)
        os.fsync(temporary_fd)
        temporary_metadata = os.fstat(temporary_fd)
        temporary_snapshot = FileSnapshot(
            payload=payload,
            mode=stat.S_IMODE(temporary_metadata.st_mode),
            identity=_object_identity(temporary_metadata),
            access_policy=_access_policy(temporary_metadata),
            size=temporary_metadata.st_size,
        )
        os.close(temporary_fd)
        temporary_fd = -1
        observed_temporary = _safe_read_snapshot(
            target_root,
            PurePosixPath(temporary_name),
        )
        if observed_temporary != temporary_snapshot:
            raise MirrorSyncError("generation transaction temporary file was replaced")
        try:
            os.link(
                temporary_name,
                TRANSACTION_PATH.as_posix(),
                src_dir_fd=target_root.fd,
                dst_dir_fd=target_root.fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise MirrorSyncError(
                "generation transaction journal appeared concurrently"
            ) from error
        os.fsync(target_root.fd)
        published = _safe_read_snapshot(target_root, TRANSACTION_PATH)
        if published != temporary_snapshot:
            raise MirrorSyncError(
                "generation transaction journal changed during publication"
            )
        _isolate_and_remove_file(
            target_root,
            target_root.fd,
            temporary_name,
            temporary_snapshot,
            TRANSACTION_TEMP_PATH,
            retention_kind=QUARANTINE_TRANSIENT_KIND,
        )
        temporary_name = ""
        os.fsync(target_root.fd)
        return published
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name and temporary_snapshot is not None:
            try:
                observed_temporary = _safe_read_leaf_snapshot(
                    target_root.fd,
                    temporary_name,
                    TRANSACTION_TEMP_PATH,
                )
                if observed_temporary == temporary_snapshot:
                    _isolate_and_remove_file(
                        target_root,
                        target_root.fd,
                        temporary_name,
                        temporary_snapshot,
                        TRANSACTION_TEMP_PATH,
                        retention_kind=QUARANTINE_TRANSIENT_KIND,
                    )
            except MirrorSyncError:
                pass


def _parse_transaction_journal_snapshot(
    snapshot: FileSnapshot,
) -> dict[str, Any]:
    if snapshot.mode != 0o600:
        raise MirrorSyncError("generation transaction journal has unsafe mode")
    try:
        raw = json.loads(
            snapshot.payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise MirrorSyncError("generation transaction journal is invalid") from error
    if not isinstance(raw, dict):
        raise MirrorSyncError("generation transaction journal must be an object")
    return raw


def _inspect_transaction_journal(
    target_root: BoundRoot,
) -> TransactionJournalInspection:
    """Classify transaction artifacts without mutating the consumer."""
    completion = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_COMPLETE_PATH,
    )
    published = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_PATH,
    )
    temporary = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_TEMP_PATH,
    )
    authoritative = published or completion
    if completion is not None and published is not None and completion != published:
        raise MirrorSyncError("generation transaction completion and journal differ")
    if (
        temporary is not None
        and authoritative is not None
        and temporary != authoritative
    ):
        raise MirrorSyncError(
            "generation transaction pending and published journals differ"
        )
    transaction = (
        None
        if authoritative is None
        else (
            authoritative,
            _parse_transaction_journal_snapshot(authoritative),
        )
    )
    return TransactionJournalInspection(
        completion=completion,
        published=published,
        temporary=temporary,
        transaction=transaction,
    )


def _load_transaction_journal(
    target_root: BoundRoot,
) -> tuple[FileSnapshot, dict[str, Any]] | None:
    """Read transaction state without performing recovery writes."""
    return _inspect_transaction_journal(target_root).transaction


def _recover_transaction_journal_artifacts(
    target_root: BoundRoot,
    inspection: TransactionJournalInspection,
) -> tuple[FileSnapshot, dict[str, Any]] | None:
    """Normalize the exact inspected artifacts after target/stage-0 proof."""
    completion = inspection.completion
    published = inspection.published
    temporary = inspection.temporary
    if completion is not None:
        if _safe_read_snapshot(target_root, TRANSACTION_COMPLETE_PATH) != completion:
            raise MirrorSyncError(
                "generation transaction completion changed before recovery"
            )
        if published is None:
            if _optional_safe_read_snapshot(target_root, TRANSACTION_PATH) is not None:
                raise MirrorSyncError(
                    "generation transaction journal appeared before recovery"
                )
            try:
                os.link(
                    TRANSACTION_COMPLETE_PATH.as_posix(),
                    TRANSACTION_PATH.as_posix(),
                    src_dir_fd=target_root.fd,
                    dst_dir_fd=target_root.fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise MirrorSyncError(
                    "generation transaction completion raced during recovery"
                ) from error
            os.fsync(target_root.fd)
            published = _safe_read_snapshot(target_root, TRANSACTION_PATH)
        elif _safe_read_snapshot(target_root, TRANSACTION_PATH) != published:
            raise MirrorSyncError(
                "generation transaction journal changed before recovery"
            )
        if published != completion:
            raise MirrorSyncError(
                "generation transaction completion and journal differ"
            )
        _isolate_and_remove_file(
            target_root,
            target_root.fd,
            TRANSACTION_COMPLETE_PATH.as_posix(),
            completion,
            TRANSACTION_COMPLETE_PATH,
            retention_kind=QUARANTINE_TRANSIENT_KIND,
        )
    elif published is not None and (
        _safe_read_snapshot(target_root, TRANSACTION_PATH) != published
    ):
        raise MirrorSyncError("generation transaction journal changed before recovery")
    if temporary is not None:
        if _safe_read_snapshot(target_root, TRANSACTION_TEMP_PATH) != temporary:
            raise MirrorSyncError(
                "generation transaction pending file changed before recovery"
            )
        _isolate_and_remove_file(
            target_root,
            target_root.fd,
            TRANSACTION_TEMP_PATH.as_posix(),
            temporary,
            TRANSACTION_TEMP_PATH,
            retention_kind=QUARANTINE_TRANSIENT_KIND,
        )
    if published is None:
        return None
    recovered = _safe_read_snapshot(target_root, TRANSACTION_PATH)
    if recovered != published:
        raise MirrorSyncError("generation transaction journal changed during recovery")
    document = _parse_transaction_journal_snapshot(recovered)
    if inspection.transaction is None or document != inspection.transaction[1]:
        raise MirrorSyncError("generation transaction document changed during recovery")
    return recovered, document


def _transaction_managed_paths(document: dict[str, Any]) -> set[PurePosixPath]:
    raw_files = document.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise MirrorSyncError("generation transaction has an invalid managed file set")
    paths: set[PurePosixPath] = set()
    for raw_path in raw_files:
        path = _validate_relative_path(
            raw_path,
            "generation transaction managed path",
        )
        if path.parts[0].casefold() == ".git":
            raise MirrorSyncError(
                f"generation transaction path enters the Git control plane: {path}"
            )
        if path.name.endswith(".sync-exchange.json"):
            raise MirrorSyncError(
                f"generation transaction path collides with recovery metadata: {path}"
            )
        paths.add(path)
    _validate_target_layout(sorted(paths, key=PurePosixPath.as_posix), "transaction")
    return paths


def _remove_transaction_journal(
    target_root: BoundRoot,
    expected: FileSnapshot,
) -> None:
    observed = _safe_read_snapshot(target_root, TRANSACTION_PATH)
    if observed != expected:
        raise MirrorSyncError(
            "generation transaction journal changed before completion"
        )
    try:
        _rename_directory_entry_noreplace(
            target_root.fd,
            TRANSACTION_PATH.as_posix(),
            TRANSACTION_COMPLETE_PATH.as_posix(),
        )
    except FileExistsError as error:
        raise MirrorSyncError(
            "generation transaction completion marker already exists"
        ) from error
    except OSError as error:
        raise MirrorSyncError(
            f"cannot create generation transaction completion marker: {error}"
        ) from error
    os.fsync(target_root.fd)
    completion = _safe_read_snapshot(
        target_root,
        TRANSACTION_COMPLETE_PATH,
    )
    if completion != expected:
        if _optional_safe_read_snapshot(target_root, TRANSACTION_PATH) is None:
            try:
                _rename_directory_entry_noreplace(
                    target_root.fd,
                    TRANSACTION_COMPLETE_PATH.as_posix(),
                    TRANSACTION_PATH.as_posix(),
                )
                os.fsync(target_root.fd)
            except OSError as restore_error:
                raise MirrorSyncError(
                    "generation transaction journal changed during isolation; "
                    "preserving completion marker: "
                    f"{restore_error}"
                ) from restore_error
            if _safe_read_snapshot(target_root, TRANSACTION_PATH) != completion:
                raise MirrorSyncError(
                    "generation transaction journal changed while restoring it"
                )
        raise MirrorSyncError("generation transaction completion marker changed")
    if _optional_safe_read_snapshot(target_root, TRANSACTION_PATH) is not None:
        raise MirrorSyncError(
            "generation transaction journal reappeared during completion"
        )
    if _safe_read_snapshot(target_root, TRANSACTION_COMPLETE_PATH) != expected:
        raise MirrorSyncError(
            "generation transaction completion marker changed before cleanup"
        )
    _isolate_and_remove_file(
        target_root,
        target_root.fd,
        TRANSACTION_COMPLETE_PATH.as_posix(),
        expected,
        TRANSACTION_COMPLETE_PATH,
        retention_kind=QUARANTINE_TRANSIENT_KIND,
    )


def _resume_transaction_state(
    target_root: BoundRoot,
    document: dict[str, Any],
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    current_index: bytes,
    desired_files: dict[PurePosixPath, tuple[bytes, int] | None],
    invalid_receipt: bytes,
    final_receipt: bytes,
) -> tuple[
    dict[PurePosixPath, FileSnapshot | None],
    set[PurePosixPath],
    str,
]:
    expected_fields = {
        "version",
        "canonical_repository",
        "canonical_commit",
        "mirror",
        "mirror_repository",
        "index_sha256",
        "files",
        "receipt",
    }
    if (
        set(document) != expected_fields
        or document["version"] != 1
        or document["canonical_repository"] != source_lock.canonical_repository
        or document["canonical_commit"] != source_commit
        or document["mirror"] != mirror.name
        or document["mirror_repository"] != mirror.repository
        or document["index_sha256"] != hashlib.sha256(current_index).hexdigest()
        or not isinstance(document["files"], dict)
        or not isinstance(document["receipt"], dict)
    ):
        raise MirrorSyncError(
            "generation transaction does not match this source/index/mirror"
        )
    expected_file_names = {path.as_posix() for path in desired_files}
    if set(document["files"]) != expected_file_names:
        raise MirrorSyncError("generation transaction managed file set changed")
    snapshots: dict[PurePosixPath, FileSnapshot | None] = {}
    already_desired: set[PurePosixPath] = set()
    for path, desired in sorted(
        desired_files.items(),
        key=lambda item: item[0].as_posix(),
    ):
        record = document["files"][path.as_posix()]
        if (
            not isinstance(record, dict)
            or set(record) != {"initial", "desired"}
            or record["desired"] != _desired_path_record(desired)
        ):
            raise MirrorSyncError(
                f"generation transaction desired file changed: {path}"
            )
        current = _optional_safe_read_snapshot(target_root, path)
        initial_matches = (
            current is None
            if record["initial"] is None
            else _snapshot_matches_record(current, record["initial"])
        )
        desired_matches = _snapshot_matches_desired(
            current,
            record["desired"],
        )
        if not initial_matches and not desired_matches:
            raise MirrorSyncError(
                f"consumer path changed outside pending generation: {path}"
            )
        snapshots[path] = current
        if desired_matches:
            already_desired.add(path)

    receipt_record = document["receipt"]
    expected_receipt_record = {
        "initial",
        "invalid",
        "final",
    }
    if (
        set(receipt_record) != expected_receipt_record
        or receipt_record["invalid"] != _desired_file_record(invalid_receipt, 0o644)
        or receipt_record["final"] != _desired_file_record(final_receipt, 0o644)
    ):
        raise MirrorSyncError("generation transaction receipt contract changed")
    receipt_snapshot = _optional_safe_read_snapshot(
        target_root,
        RECEIPT_PATH,
    )
    if _snapshot_matches_desired(
        receipt_snapshot,
        receipt_record["final"],
    ):
        receipt_state = "final"
    elif _snapshot_matches_desired(
        receipt_snapshot,
        receipt_record["invalid"],
    ):
        receipt_state = "invalid"
    elif (
        receipt_snapshot is None
        if receipt_record["initial"] is None
        else _snapshot_matches_record(
            receipt_snapshot,
            receipt_record["initial"],
        )
    ):
        receipt_state = "initial"
    else:
        raise MirrorSyncError("consumer receipt changed outside pending generation")
    snapshots[RECEIPT_PATH] = receipt_snapshot
    return snapshots, already_desired, receipt_state


def _canonical_json(value: object, *, pretty: bool) -> bytes:
    if pretty:
        encoded = json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    else:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
    return (encoded + ("\n" if pretty else "")).encode("utf-8")


def _deterministic_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value, pretty=False)).hexdigest()


def _receipt_document(
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
) -> dict[str, Any]:
    files = [
        {
            "source_name": source_name,
            "source_path": source_lock.sources[source_name].path.as_posix(),
            "target_path": target_path.as_posix(),
            "sha256": source_lock.sources[source_name].sha256,
            "mode": f"{source_lock.sources[source_name].mode:04o}",
        }
        for source_name, target_path in sorted(mirror.files.items())
    ]
    mapping = [
        {
            "source_name": item["source_name"],
            "source_path": item["source_path"],
            "target_path": item["target_path"],
        }
        for item in files
    ]
    file_set = sorted(item["target_path"] for item in files)
    tree = [
        {
            "target_path": item["target_path"],
            "sha256": item["sha256"],
            "mode": item["mode"],
        }
        for item in sorted(files, key=lambda item: item["target_path"])
    ]
    return {
        "receipt_version": RECEIPT_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "rules_contract_version": RULES_CONTRACT_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "canonical_repository": source_lock.canonical_repository,
        "canonical_commit": source_commit,
        "mirror": mirror.name,
        "mirror_repository": mirror.repository,
        "mapping_digest": _deterministic_digest(mapping),
        "file_set_digest": _deterministic_digest(file_set),
        "tree_digest": _deterministic_digest(tree),
        "files": files,
    }


def _receipt_payload(
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
) -> bytes:
    return _canonical_json(
        _receipt_document(source_lock, mirror, source_commit),
        pretty=True,
    )


def _parse_receipt(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_LOCK_BYTES:
        raise MirrorSyncError(
            f"provenance receipt exceeds the {MAX_LOCK_BYTES}-byte limit"
        )
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except MirrorSyncError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise MirrorSyncError(
            f"provenance receipt is not valid JSON: {error}"
        ) from error
    receipt = _expect_object(raw, "provenance receipt")
    fields = {
        "receipt_version",
        "generator_contract_version",
        "rules_contract_version",
        "hash_algorithm",
        "canonical_repository",
        "canonical_commit",
        "mirror",
        "mirror_repository",
        "mapping_digest",
        "file_set_digest",
        "tree_digest",
        "files",
    }
    _check_fields(
        receipt,
        allowed=fields,
        required=fields,
        field_name="provenance receipt",
    )
    if (
        type(receipt["receipt_version"]) is not int
        or receipt["receipt_version"] != RECEIPT_VERSION
    ):
        raise MirrorSyncError(f"provenance receipt version must be {RECEIPT_VERSION}")
    if (
        type(receipt["generator_contract_version"]) is not int
        or receipt["generator_contract_version"] != GENERATOR_CONTRACT_VERSION
    ):
        raise MirrorSyncError(
            "provenance receipt generator contract version must be "
            f"{GENERATOR_CONTRACT_VERSION}"
        )
    if (
        type(receipt["rules_contract_version"]) is not int
        or receipt["rules_contract_version"] != RULES_CONTRACT_VERSION
    ):
        raise MirrorSyncError(
            "provenance receipt rules contract version must be "
            f"{RULES_CONTRACT_VERSION}"
        )
    if receipt["hash_algorithm"] != HASH_ALGORITHM:
        raise MirrorSyncError(
            f"provenance receipt hash_algorithm must be {HASH_ALGORITHM}"
        )
    _validate_repository(
        receipt["canonical_repository"],
        "provenance receipt canonical_repository",
    )
    _validate_name(receipt["mirror"], "provenance receipt mirror")
    _validate_repository(
        receipt["mirror_repository"],
        "provenance receipt mirror_repository",
    )
    canonical_commit = receipt["canonical_commit"]
    if (
        not isinstance(canonical_commit, str)
        or GIT_SHA_RE.fullmatch(canonical_commit) is None
    ):
        raise MirrorSyncError(
            "provenance receipt canonical_commit must be a full Git SHA"
        )
    for digest_field in ("mapping_digest", "file_set_digest", "tree_digest"):
        digest = receipt[digest_field]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise MirrorSyncError(
                f"provenance receipt {digest_field} must be lowercase SHA-256"
            )
    if not isinstance(receipt["files"], list):
        raise MirrorSyncError("provenance receipt files must be an array")
    if not receipt["files"]:
        raise MirrorSyncError("provenance receipt files must not be empty")
    source_names: set[str] = set()
    target_paths: list[PurePosixPath] = []
    validated_files: list[dict[str, object]] = []
    for index, raw_file in enumerate(receipt["files"]):
        item = _expect_object(
            raw_file,
            f"provenance receipt files[{index}]",
        )
        item_fields = {
            "source_name",
            "source_path",
            "target_path",
            "sha256",
            "mode",
        }
        _check_fields(
            item,
            allowed=item_fields,
            required=item_fields,
            field_name=f"provenance receipt files[{index}]",
        )
        source_name = _validate_name(
            item["source_name"],
            f"provenance receipt files[{index}].source_name",
        )
        if source_name in source_names:
            raise MirrorSyncError(
                f"provenance receipt has duplicate source name: {source_name}"
            )
        source_names.add(source_name)
        source_path = _validate_relative_path(
            item["source_path"],
            f"provenance receipt files[{index}].source_path",
        )
        target_path = _validate_relative_path(
            item["target_path"],
            f"provenance receipt files[{index}].target_path",
        )
        if target_path.parts[0].casefold() == ".git":
            raise MirrorSyncError(
                "provenance receipt target must not enter the Git control "
                f"plane: {target_path}"
            )
        if target_path.name.endswith(".sync-exchange.json"):
            raise MirrorSyncError(
                "provenance receipt target collides with recovery metadata: "
                f"{target_path}"
            )
        sha256 = item["sha256"]
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise MirrorSyncError(
                f"provenance receipt files[{index}].sha256 must be lowercase hex"
            )
        mode = item["mode"]
        if (
            not isinstance(mode, str)
            or MODE_RE.fullmatch(mode) is None
            or int(mode, 8) not in {0o644, 0o755}
        ):
            raise MirrorSyncError(
                f"provenance receipt files[{index}].mode must be 0644 or 0755"
            )
        target_paths.append(target_path)
        validated_files.append(
            {
                "source_name": source_name,
                "source_path": source_path.as_posix(),
                "target_path": target_path.as_posix(),
                "sha256": sha256,
                "mode": mode,
            }
        )
    _validate_target_layout(target_paths, receipt["mirror"])
    mapping = [
        {
            "source_name": item["source_name"],
            "source_path": item["source_path"],
            "target_path": item["target_path"],
        }
        for item in validated_files
    ]
    file_set = sorted(str(item["target_path"]) for item in validated_files)
    tree = [
        {
            "target_path": item["target_path"],
            "sha256": item["sha256"],
            "mode": item["mode"],
        }
        for item in sorted(
            validated_files,
            key=lambda item: str(item["target_path"]),
        )
    ]
    expected_digests = {
        "mapping_digest": _deterministic_digest(mapping),
        "file_set_digest": _deterministic_digest(file_set),
        "tree_digest": _deterministic_digest(tree),
    }
    for field_name, expected in expected_digests.items():
        if receipt[field_name] != expected:
            raise MirrorSyncError(
                f"provenance receipt {field_name} does not match its files"
            )
    return receipt


def _load_receipt(target_root: Root) -> tuple[FileSnapshot, dict[str, Any]]:
    try:
        snapshot = _safe_read_snapshot(target_root, RECEIPT_PATH)
    except MirrorSyncError as error:
        raise MirrorSyncError(
            f"provenance receipt is missing or unsafe: {error}"
        ) from error
    if snapshot.mode != 0o644:
        raise MirrorSyncError(
            f"provenance receipt mode must be 0644, not {snapshot.mode:04o}"
        )
    return snapshot, _parse_receipt(snapshot.payload)


def _receipt_file_records(
    receipt: dict[str, Any],
) -> dict[PurePosixPath, dict[str, object]]:
    records: dict[PurePosixPath, dict[str, object]] = {}
    for item in receipt["files"]:
        path = PurePosixPath(item["target_path"])
        records[path] = {
            "sha256": item["sha256"],
            "mode": int(item["mode"], 8),
            "size": None,
        }
    return records


def _validated_prior_receipt_file_records(
    repository_root: BoundRoot,
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    receipt_payload: bytes,
    receipt_mode: int,
) -> dict[PurePosixPath, dict[str, object]]:
    if receipt_mode != 0o644:
        raise MirrorSyncError(
            f"prior provenance receipt mode must be 0644, not {receipt_mode:04o}"
        )
    receipt = _parse_receipt(receipt_payload)
    if (
        receipt["canonical_repository"] != source_lock.canonical_repository
        or receipt["mirror"] != mirror.name
        or receipt["mirror_repository"] != mirror.repository
    ):
        raise MirrorSyncError(
            "prior provenance receipt names the wrong canonical repository, "
            "mirror, or target repository"
        )
    historical_commit = receipt["canonical_commit"]
    _require_ancestral_commit(
        repository_root,
        historical_commit,
        source_commit,
        "prior provenance receipt canonical commit",
    )
    historical_lock = _source_lock_at_commit(
        repository_root,
        historical_commit,
    )
    if historical_lock.canonical_repository != source_lock.canonical_repository:
        raise MirrorSyncError(
            "prior provenance receipt historical source lock names the wrong "
            "canonical repository"
        )
    historical_mirror = _selected_mirror(
        historical_lock,
        mirror.name,
    )
    if historical_mirror.repository != mirror.repository:
        raise MirrorSyncError(
            "prior provenance receipt historical mirror names the wrong "
            "target repository"
        )
    _committed_sources(
        repository_root,
        historical_commit,
        historical_lock,
    )
    expected_payload = _receipt_payload(
        historical_lock,
        historical_mirror,
        historical_commit,
    )
    if receipt_payload != expected_payload:
        raise MirrorSyncError(
            "prior provenance receipt does not exactly match its historical "
            "canonical source lock"
        )
    return _receipt_file_records(receipt)


def _target_head_index_file(
    target_root: BoundRoot,
    path: PurePosixPath,
    head_commit: str,
) -> tuple[bytes, int] | None:
    index_entry = _git_index_entry(target_root, path)
    head_entry = _git_tree_entry(target_root, head_commit, path)
    if head_entry != index_entry:
        raise MirrorSyncError(f"target managed index differs from HEAD: {path}")
    if index_entry is None:
        return None
    return (
        _git_blob_payload(target_root, index_entry.object_id, path),
        index_entry.mode,
    )


def _prior_receipt_file_records(
    repository_root: BoundRoot,
    target_root: BoundRoot,
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    *,
    require_clean_worktree: bool,
) -> dict[PurePosixPath, dict[str, object]]:
    head_commit = _current_commit(target_root)
    if require_clean_worktree:
        snapshot = _target_clean_snapshot(
            target_root,
            RECEIPT_PATH,
            head_commit,
        )
        receipt_file = None if snapshot is None else (snapshot.payload, snapshot.mode)
    else:
        receipt_file = _target_head_index_file(
            target_root,
            RECEIPT_PATH,
            head_commit,
        )
    if receipt_file is None:
        return {}
    receipt_payload, receipt_mode = receipt_file
    return _validated_prior_receipt_file_records(
        repository_root,
        source_lock,
        mirror,
        source_commit,
        receipt_payload,
        receipt_mode,
    )


def _require_receipt_file_parity(
    snapshots: dict[PurePosixPath, FileSnapshot | None],
    receipt_records: dict[PurePosixPath, dict[str, object]],
) -> None:
    for path, record in sorted(
        receipt_records.items(),
        key=lambda item: item[0].as_posix(),
    ):
        snapshot = snapshots[path]
        if snapshot is None:
            raise MirrorSyncError(
                f"prior receipt managed path is missing from HEAD/index/worktree: "
                f"{path}"
            )
        if (
            hashlib.sha256(snapshot.payload).hexdigest() != record["sha256"]
            or snapshot.mode != record["mode"]
        ):
            raise MirrorSyncError(
                f"prior receipt managed path differs from its receipt: {path}"
            )


def _require_same_file_group(
    root: Root,
    expected: dict[PurePosixPath, FileSnapshot],
    group_name: str,
) -> None:
    for path, expected_snapshot in sorted(
        expected.items(),
        key=lambda item: item[0].as_posix(),
    ):
        observed = _safe_read_snapshot(root, path)
        if observed != expected_snapshot:
            raise MirrorSyncError(
                f"{group_name} changed before transaction completion: {path}"
            )


def _require_paths_absent(
    root: Root,
    paths: set[PurePosixPath] | frozenset[PurePosixPath],
    group_name: str,
) -> None:
    for path in sorted(paths, key=PurePosixPath.as_posix):
        if _optional_safe_read_snapshot(root, path) is not None:
            raise MirrorSyncError(
                f"{group_name} reappeared before transaction completion: {path}"
            )


def _check_mirror_bound(
    repository_root: BoundRoot,
    target_root: BoundRoot,
    mirror_name: str,
) -> int:
    _reject_canonical_target(repository_root, target_root)
    source_lock = load_source_lock(repository_root)
    mirror = _selected_mirror(source_lock, mirror_name)
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _verify_target_repository(target_root, mirror.repository)
    initial_index = _target_index_snapshot(target_root, mirror)
    _configure_managed_ancestors(
        target_root,
        _mirror_operation_paths(mirror),
    )
    _reject_git_control_targets(target_root, mirror)
    _reject_pending_exchange_journals(target_root, mirror)
    _require_target_head_index_parity(target_root, mirror)
    _require_same_target_index(target_root, mirror, initial_index)
    try:
        receipt_snapshot, receipt = _load_receipt(target_root)
    except MirrorSyncError as error:
        raise MirrorSyncError(
            "provenance receipt is stale or tampered for the current "
            f"mapping/rules/source tree: {error}"
        ) from error
    receipt_payload = receipt_snapshot.payload
    if (
        receipt.get("canonical_repository") != source_lock.canonical_repository
        or receipt.get("mirror") != mirror.name
        or receipt.get("mirror_repository") != mirror.repository
    ):
        raise MirrorSyncError(
            "provenance receipt names the wrong canonical repository, mirror, "
            "or target repository"
        )
    source_commit = _current_commit(repository_root)
    if receipt["canonical_commit"] != source_commit:
        raise MirrorSyncError(
            "provenance receipt is stale for the current canonical commit"
        )
    sources = _verify_committed_sources(
        repository_root,
        source_commit,
        source_lock,
    )
    expected_receipt = _receipt_payload(source_lock, mirror, source_commit)
    if receipt_payload != expected_receipt:
        raise MirrorSyncError(
            "provenance receipt is stale or tampered for the current "
            "mapping/rules/source tree"
        )
    target_group = {RECEIPT_PATH: receipt_snapshot}
    for source_name, target_path in sorted(mirror.files.items()):
        target_snapshot = _safe_read_snapshot(target_root, target_path)
        target_payload = target_snapshot.payload
        target_mode = target_snapshot.mode
        source_payload, source_mode = sources[source_name]
        if target_payload != source_payload:
            raise MirrorSyncError(
                f"mirror content differs from canonical source: {target_path}"
            )
        if target_mode != source_mode:
            raise MirrorSyncError(
                f"mirror mode differs from canonical source: {target_path} "
                f"({target_mode:#05o} != {source_mode:#05o})"
            )
        target_group[target_path] = target_snapshot
    final_sources = _verify_committed_sources(
        repository_root,
        source_commit,
        source_lock,
    )
    if final_sources != sources:
        raise MirrorSyncError("canonical source group changed during mirror check")
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _verify_target_repository(target_root, mirror.repository)
    _require_same_target_index(target_root, mirror, initial_index)
    _require_same_file_group(target_root, target_group, "consumer mirror group")
    return len(mirror.files)


def check_mirror(
    repository_root: Path,
    target_root: Path,
    mirror_name: str,
) -> int:
    operation = _new_operation_budget()
    _reject_canonical_target(repository_root, target_root)
    canonical = _bind_root(repository_root)
    canonical.operation = operation
    try:
        target = _bind_root(target_root)
        target.operation = operation
    except BaseException:
        _finish_bound_roots(canonical)
        raise
    try:
        _ensure_git_control_binding(canonical)
        _ensure_git_control_binding(target)
        return _check_mirror_bound(canonical, target, mirror_name)
    finally:
        _finish_bound_roots(canonical, target)


def _require_exact_commit(
    repository_root: BoundRoot,
    commit_sha: str,
    label: str,
) -> None:
    if GIT_SHA_RE.fullmatch(commit_sha) is None:
        raise MirrorSyncError(f"{label} must be one exact lowercase SHA-1")
    resolved = _run_git(
        repository_root,
        "rev-parse",
        "--verify",
        f"{commit_sha}^{{commit}}",
    )
    if resolved != commit_sha.encode("ascii") + b"\n":
        raise MirrorSyncError(f"{label} does not resolve to its exact commit")


def _target_commit_tree_entries(
    target_root: BoundRoot,
    target_commit: str,
) -> tuple[bytes, dict[bytes, tuple[bytes, bytes]]]:
    _require_exact_commit(target_root, target_commit, "target commit")
    raw_tree = _run_git(
        target_root,
        "ls-tree",
        "--full-tree",
        "-r",
        "-z",
        target_commit,
    )
    return raw_tree, _parse_consumer_tracked_entries(
        target_root,
        raw_tree,
        source="target commit",
    )


def _target_commit_prior_receipt_file_records(
    repository_root: BoundRoot,
    target_root: BoundRoot,
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    tree_entries: dict[bytes, tuple[bytes, bytes]],
) -> dict[PurePosixPath, dict[str, object]]:
    receipt_key = RECEIPT_PATH.as_posix().encode("utf-8")
    receipt_entry = tree_entries.get(receipt_key)
    if receipt_entry is None:
        return {}
    receipt_mode, raw_receipt_object = receipt_entry
    if receipt_mode != b"100644":
        raise MirrorSyncError(
            "prior provenance receipt in the target commit must have mode 100644"
        )
    receipt_object = raw_receipt_object.decode("ascii", errors="strict")
    receipt_payload = _git_blob_payload(
        target_root,
        receipt_object,
        RECEIPT_PATH,
    )
    records = _validated_prior_receipt_file_records(
        repository_root,
        source_lock,
        mirror,
        source_commit,
        receipt_payload,
        0o644,
    )
    for path, record in sorted(
        records.items(),
        key=lambda item: item[0].as_posix(),
    ):
        raw_path = path.as_posix().encode("utf-8")
        tree_entry = tree_entries.get(raw_path)
        if tree_entry is None:
            raise MirrorSyncError(
                f"prior receipt managed path is missing from target commit: {path}"
            )
        raw_mode, raw_object = tree_entry
        expected_mode = b"100755" if record["mode"] == 0o755 else b"100644"
        if raw_mode != expected_mode:
            raise MirrorSyncError(
                f"prior receipt managed path mode differs in target commit: {path}"
            )
        object_id = raw_object.decode("ascii", errors="strict")
        payload = _git_blob_payload(
            target_root,
            object_id,
            path,
        )
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise MirrorSyncError(
                f"prior receipt managed path digest differs in target commit: {path}"
            )
    return records


def _managed_mirror_paths_at_target_commit(
    repository_root: BoundRoot,
    target_root: BoundRoot,
    source_lock: SourceLock,
    mirror: MirrorSpec,
    source_commit: str,
    target_commit: str,
) -> list[PurePosixPath]:
    initial_index = _target_index_snapshot(target_root, mirror)
    raw_tree, tree_entries = _target_commit_tree_entries(
        target_root,
        target_commit,
    )
    receipt_records = _target_commit_prior_receipt_file_records(
        repository_root,
        target_root,
        source_lock,
        mirror,
        source_commit,
        tree_entries,
    )
    additional_paths = set(receipt_records)
    managed_targets = set(mirror.files.values()) | additional_paths
    _validate_target_layout(
        sorted(managed_targets, key=PurePosixPath.as_posix),
        mirror.name,
    )
    try:
        tracked_paths = {
            _validate_relative_path(
                raw_path.decode("utf-8", errors="strict"),
                "consumer target commit tracked path",
            )
            for raw_path in tree_entries
        }
    except UnicodeDecodeError as error:
        raise MirrorSyncError(
            "consumer target commit tracked path is not valid UTF-8"
        ) from error
    tracked_symlink_paths = {
        _validate_relative_path(
            raw_path.decode("utf-8", errors="strict"),
            "consumer target commit tracked symlink path",
        )
        for raw_path, (raw_mode, _raw_object) in tree_entries.items()
        if raw_mode == b"120000"
    }
    _validate_consumer_portable_layout(
        {RECEIPT_PATH, *managed_targets},
        tracked_paths,
        tracked_symlink_paths,
        mirror.name,
    )
    _reject_git_control_targets(
        target_root,
        mirror,
        additional_paths,
    )
    _require_same_target_index(
        target_root,
        mirror,
        initial_index,
        additional_paths,
    )
    final_raw_tree, _final_tree_entries = _target_commit_tree_entries(
        target_root,
        target_commit,
    )
    if final_raw_tree != raw_tree:
        raise MirrorSyncError(
            "target commit tree changed during object-only managed-path validation"
        )
    return _mirror_managed_paths(mirror, additional_paths)


def managed_mirror_paths(
    repository_root: Path,
    target_root: Path,
    mirror_name: str,
    target_commit: str | None = None,
) -> list[PurePosixPath]:
    operation = _new_operation_budget()
    _reject_canonical_target(repository_root, target_root)
    canonical = _bind_root(repository_root)
    canonical.operation = operation
    try:
        target = _bind_root(target_root)
        target.operation = operation
    except BaseException:
        _finish_bound_roots(canonical)
        raise
    try:
        _ensure_git_control_binding(canonical)
        _ensure_git_control_binding(target)
        source_lock = load_source_lock(canonical)
        mirror = _selected_mirror(source_lock, mirror_name)
        _verify_canonical_repository(
            canonical,
            source_lock.canonical_repository,
        )
        source_commit = _current_commit(canonical)
        _verify_committed_sources(
            canonical,
            source_commit,
            source_lock,
        )
        _verify_target_repository(target, mirror.repository)
        if target_commit is not None:
            return _managed_mirror_paths_at_target_commit(
                canonical,
                target,
                source_lock,
                mirror,
                source_commit,
                target_commit,
            )
        receipt_records = _prior_receipt_file_records(
            canonical,
            target,
            source_lock,
            mirror,
            source_commit,
            require_clean_worktree=True,
        )
        additional_paths = set(receipt_records)
        managed_targets = set(mirror.files.values()) | additional_paths
        _validate_target_layout(
            sorted(managed_targets, key=PurePosixPath.as_posix),
            mirror.name,
        )
        initial_index = _target_index_snapshot(
            target,
            mirror,
            additional_paths,
        )
        _configure_managed_ancestors(
            target,
            _mirror_operation_paths(mirror, additional_paths),
        )
        _reject_git_control_targets(
            target,
            mirror,
            additional_paths,
        )
        _reject_pending_exchange_journals(
            target,
            mirror,
            additional_paths,
        )
        snapshots = _target_managed_snapshots(
            target,
            mirror,
            additional_paths,
        )
        _require_receipt_file_parity(
            snapshots,
            receipt_records,
        )
        _require_target_head_index_parity(
            target,
            mirror,
            additional_paths,
        )
        _require_same_target_index(
            target,
            mirror,
            initial_index,
            additional_paths,
        )
        return _mirror_managed_paths(mirror, additional_paths)
    finally:
        _finish_bound_roots(canonical, target)


def _generate_mirror_bound(
    repository_root: BoundRoot,
    target_root: BoundRoot,
    mirror_name: str,
    source_commit: str,
) -> int:
    _reject_canonical_target(repository_root, target_root)
    requested_source_commit = source_commit
    transaction_inspection = _inspect_transaction_journal(target_root)
    pending_transaction = transaction_inspection.transaction
    recovering_prior_transaction = pending_transaction is not None
    transaction_paths: set[PurePosixPath] = set()
    canonical_head_commit = _current_commit(repository_root)
    if pending_transaction is None:
        source_lock = load_source_lock(repository_root)
    else:
        _transaction_snapshot, transaction_document = pending_transaction
        transaction_paths = _transaction_managed_paths(transaction_document)
        journal_commit = transaction_document.get("canonical_commit")
        journal_mirror = transaction_document.get("mirror")
        if (
            not isinstance(journal_commit, str)
            or GIT_SHA_RE.fullmatch(journal_commit) is None
            or journal_mirror != mirror_name
        ):
            raise MirrorSyncError(
                "pending generation transaction has invalid recovery identity"
            )
        source_commit = journal_commit
        _require_ancestral_commit(
            repository_root,
            source_commit,
            canonical_head_commit,
            "pending generation canonical commit",
        )
        source_lock = _source_lock_at_commit(
            repository_root,
            source_commit,
        )
    mirror = _selected_mirror(source_lock, mirror_name)
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _verify_target_repository(target_root, mirror.repository)
    prior_receipt_records = _prior_receipt_file_records(
        repository_root,
        target_root,
        source_lock,
        mirror,
        source_commit,
        require_clean_worktree=pending_transaction is None,
    )
    managed_additional_paths = set(prior_receipt_records)
    current_target_paths = set(mirror.files.values())
    allowed_transaction_paths = current_target_paths | managed_additional_paths
    if (
        pending_transaction is not None
        and transaction_paths != allowed_transaction_paths
    ):
        raise MirrorSyncError(
            "pending generation transaction managed paths exceed the "
            "HEAD/index-bound prior receipt"
        )
    _validate_target_layout(
        sorted(
            allowed_transaction_paths,
            key=PurePosixPath.as_posix,
        ),
        mirror.name,
    )
    retired_paths = managed_additional_paths - current_target_paths
    initial_index = _target_index_snapshot(
        target_root,
        mirror,
        managed_additional_paths,
    )
    # Recovery may link, quarantine, or remove journal files. Reprove the
    # semantic target identity and complete stage-0 namespace immediately
    # before allowing any such write.
    _verify_target_repository(target_root, mirror.repository)
    _require_same_target_index(
        target_root,
        mirror,
        initial_index,
        managed_additional_paths,
    )
    pending_transaction = _recover_transaction_journal_artifacts(
        target_root,
        transaction_inspection,
    )
    _configure_managed_ancestors(
        target_root,
        _mirror_operation_paths(mirror, managed_additional_paths),
    )
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _verify_target_repository(target_root, mirror.repository)
    _reject_git_control_targets(
        target_root,
        mirror,
        managed_additional_paths,
    )
    _recover_mirror_exchange_journals(
        target_root,
        mirror,
        managed_additional_paths,
    )

    def verify_active_sources() -> dict[str, tuple[bytes, int]]:
        if recovering_prior_transaction:
            return _committed_sources(
                repository_root,
                source_commit,
                source_lock,
            )
        return _verify_committed_sources(
            repository_root,
            source_commit,
            source_lock,
        )

    def completed_file_count() -> int:
        if recovering_prior_transaction and source_commit != requested_source_commit:
            raise MirrorSyncError(
                "recovered the prior generation transaction for "
                f"{source_commit}, but did not generate the requested source "
                f"commit {requested_source_commit}; review/commit the recovered "
                "target state before rerunning generate"
            )
        return len(mirror.files)

    sources = verify_active_sources()
    receipt_payload = _receipt_payload(source_lock, mirror, source_commit)
    invalid_receipt = _canonical_json(
        {
            "receipt_version": RECEIPT_VERSION,
            "status": "generating",
        },
        pretty=True,
    )
    desired_files: dict[PurePosixPath, tuple[bytes, int] | None] = {
        target_path: sources[source_name]
        for source_name, target_path in mirror.files.items()
    }
    desired_files.update({path: None for path in retired_paths})
    _require_same_target_index(
        target_root,
        mirror,
        initial_index,
        managed_additional_paths,
    )
    if pending_transaction is None:
        target_snapshots = _target_managed_snapshots(
            target_root,
            mirror,
            managed_additional_paths,
        )
        _require_receipt_file_parity(
            target_snapshots,
            prior_receipt_records,
        )
        _require_same_target_index(
            target_root,
            mirror,
            initial_index,
            managed_additional_paths,
        )
        all_targets_already_desired = all(
            _snapshot_matches_desired(
                target_snapshots[path],
                _desired_path_record(desired),
            )
            for path, desired in desired_files.items()
        )
        receipt_already_final = _snapshot_matches_desired(
            target_snapshots[RECEIPT_PATH],
            _desired_file_record(receipt_payload, 0o644),
        )
        if all_targets_already_desired and receipt_already_final:
            unchanged_group = {
                path: snapshot
                for path, snapshot in target_snapshots.items()
                if snapshot is not None and path not in retired_paths
            }
            unchanged_sources = verify_active_sources()
            if unchanged_sources != sources:
                raise MirrorSyncError(
                    "canonical source group changed during no-op generation"
                )
            _verify_canonical_repository(
                repository_root,
                source_lock.canonical_repository,
            )
            _verify_target_repository(target_root, mirror.repository)
            _require_same_target_index(
                target_root,
                mirror,
                initial_index,
                managed_additional_paths,
            )
            _require_same_file_group(
                target_root,
                unchanged_group,
                "unchanged consumer mirror group",
            )
            _require_paths_absent(
                target_root,
                retired_paths,
                "unchanged retired consumer mirror group",
            )
            return completed_file_count()
        transaction_document = _transaction_document(
            source_lock,
            mirror,
            source_commit,
            initial_index,
            target_snapshots,
            desired_files,
            invalid_receipt,
            receipt_payload,
        )
        transaction_snapshot = _write_transaction_journal(
            target_root,
            transaction_document,
        )
        already_desired = {
            path
            for path, desired in desired_files.items()
            if _snapshot_matches_desired(
                target_snapshots[path],
                _desired_path_record(desired),
            )
        }
        receipt_state = "initial"
    else:
        transaction_snapshot, transaction_document = pending_transaction
        (
            target_snapshots,
            already_desired,
            receipt_state,
        ) = _resume_transaction_state(
            target_root,
            transaction_document,
            source_lock,
            mirror,
            source_commit,
            initial_index,
            desired_files,
            invalid_receipt,
            receipt_payload,
        )
    _require_same_target_index(
        target_root,
        mirror,
        initial_index,
        managed_additional_paths,
    )
    if receipt_state == "initial":
        _atomic_write_relative(
            target_root,
            RECEIPT_PATH,
            invalid_receipt,
            0o644,
            create_parents=False,
            expected_snapshot=target_snapshots[RECEIPT_PATH],
            expected_absent=target_snapshots[RECEIPT_PATH] is None,
        )
        invalid_receipt_snapshot = _safe_read_snapshot(
            target_root,
            RECEIPT_PATH,
        )
    elif receipt_state == "invalid":
        invalid_receipt_snapshot = _safe_read_snapshot(
            target_root,
            RECEIPT_PATH,
        )
    else:
        if already_desired != set(desired_files):
            raise MirrorSyncError(
                "valid receipt exists for an incomplete pending generation"
            )
        final_group = {
            path: _safe_read_snapshot(target_root, path)
            for path, desired in desired_files.items()
            if desired is not None
        }
        final_group[RECEIPT_PATH] = _safe_read_snapshot(
            target_root,
            RECEIPT_PATH,
        )
        if (
            final_group[RECEIPT_PATH].payload != receipt_payload
            or final_group[RECEIPT_PATH].mode != 0o644
        ):
            raise MirrorSyncError(
                "resumed final receipt differs from the current contract"
            )
        final_sources = verify_active_sources()
        if final_sources != sources:
            raise MirrorSyncError(
                "canonical source group changed while resuming generation"
            )
        _verify_canonical_repository(
            repository_root,
            source_lock.canonical_repository,
        )
        _verify_target_repository(target_root, mirror.repository)
        _require_same_target_index(
            target_root,
            mirror,
            initial_index,
            managed_additional_paths,
        )
        _require_same_file_group(
            target_root,
            final_group,
            "resumed completed consumer mirror group",
        )
        _require_paths_absent(
            target_root,
            retired_paths,
            "resumed retired consumer mirror group",
        )
        _remove_transaction_journal(
            target_root,
            transaction_snapshot,
        )
        return completed_file_count()
    published_target_group: dict[PurePosixPath, FileSnapshot] = {}
    for source_name, target_path in sorted(mirror.files.items()):
        source_payload, source_mode = sources[source_name]
        if target_path in already_desired:
            published_target_group[target_path] = _safe_read_snapshot(
                target_root,
                target_path,
            )
            continue
        target_snapshot = target_snapshots[target_path]
        _require_same_target_index(
            target_root,
            mirror,
            initial_index,
            managed_additional_paths,
        )
        _atomic_write_relative(
            target_root,
            target_path,
            source_payload,
            source_mode,
            create_parents=True,
            expected_snapshot=target_snapshot,
            expected_absent=target_snapshot is None,
        )
        generated_snapshot = _safe_read_snapshot(target_root, target_path)
        if (
            generated_snapshot.payload != source_payload
            or generated_snapshot.mode != source_mode
        ):
            raise MirrorSyncError(
                f"generated mirror path changed after publication: {target_path}"
            )
        published_target_group[target_path] = generated_snapshot
    for retired_path in sorted(retired_paths, key=PurePosixPath.as_posix):
        if retired_path in already_desired:
            continue
        retired_snapshot = target_snapshots[retired_path]
        if retired_snapshot is None:
            raise MirrorSyncError(
                f"retired target has no exact initial snapshot: {retired_path}"
            )
        _require_same_target_index(
            target_root,
            mirror,
            initial_index,
            managed_additional_paths,
        )
        _atomic_remove_relative(
            target_root,
            retired_path,
            retired_snapshot,
        )
    _require_paths_absent(
        target_root,
        retired_paths,
        "pre-receipt retired target group",
    )
    pre_receipt_sources = verify_active_sources()
    if pre_receipt_sources != sources:
        raise MirrorSyncError(
            "canonical source group changed before receipt publication"
        )
    _require_same_file_group(
        target_root,
        published_target_group,
        "pre-receipt generated target group",
    )
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _require_same_target_index(
        target_root,
        mirror,
        initial_index,
        managed_additional_paths,
    )
    _atomic_write_relative(
        target_root,
        RECEIPT_PATH,
        receipt_payload,
        0o644,
        create_parents=False,
        expected_snapshot=invalid_receipt_snapshot,
    )
    published_receipt = _safe_read_snapshot(target_root, RECEIPT_PATH)
    _parse_receipt(published_receipt.payload)
    if published_receipt.payload != receipt_payload or published_receipt.mode != 0o644:
        raise MirrorSyncError("generated provenance receipt changed after publication")
    published_target_group[RECEIPT_PATH] = published_receipt
    try:
        post_receipt_sources = verify_active_sources()
        if post_receipt_sources != sources:
            raise MirrorSyncError(
                "canonical source group changed after receipt publication"
            )
        _verify_canonical_repository(
            repository_root,
            source_lock.canonical_repository,
        )
        _verify_target_repository(target_root, mirror.repository)
        _require_same_target_index(
            target_root,
            mirror,
            initial_index,
            managed_additional_paths,
        )
        _require_same_file_group(
            target_root,
            published_target_group,
            "generated consumer mirror group",
        )
        _require_paths_absent(
            target_root,
            retired_paths,
            "generated retired target group",
        )
    except MirrorSyncError as terminal_error:
        try:
            _atomic_write_relative(
                target_root,
                RECEIPT_PATH,
                invalid_receipt,
                0o644,
                create_parents=False,
                expected_snapshot=published_receipt,
            )
        except MirrorSyncError as invalidation_error:
            raise MirrorSyncError(
                "terminal source/index/target validation failed after receipt "
                "publication and "
                f"conditional receipt invalidation failed: {invalidation_error}"
            ) from invalidation_error
        raise terminal_error
    _remove_transaction_journal(
        target_root,
        transaction_snapshot,
    )
    return completed_file_count()


def generate_mirror(
    repository_root: Path,
    target_root: Path,
    mirror_name: str,
    source_commit: str,
) -> int:
    operation = _new_operation_budget()
    # Reject the same/below-canonical target before taking incompatible shared
    # and exclusive locks. The bound transaction repeats this check after lock.
    _reject_canonical_target(repository_root, target_root)
    canonical = _bind_root(repository_root)
    canonical.operation = operation
    try:
        target = _bind_root(target_root, exclusive=True)
        target.operation = operation
    except BaseException:
        _finish_bound_roots(canonical)
        raise
    try:
        _ensure_git_control_binding(canonical)
        _ensure_git_control_binding(target)
        return _generate_mirror_bound(
            canonical,
            target,
            mirror_name,
            source_commit,
        )
    finally:
        _finish_bound_roots(canonical, target)


def refresh_source_lock(repository_root: Path, *, check: bool) -> int:
    operation = _new_operation_budget()
    canonical = _bind_root(repository_root, exclusive=True)
    canonical.operation = operation
    try:
        _ensure_git_control_binding(canonical)
        lock_journal_path = LOCK_PATH.parent / _exchange_journal_name(LOCK_PATH)
        if check:
            if (
                _optional_safe_read_snapshot(
                    canonical,
                    lock_journal_path,
                )
                is not None
            ):
                raise MirrorSyncError(
                    "source lock has a pending exchange journal; run "
                    "refresh-lock without --check to recover"
                )
        else:
            _recover_exchange_journal(canonical, LOCK_PATH)
        initial_lock = _safe_read_snapshot(canonical, LOCK_PATH)
        source_lock = _parse_source_lock(initial_lock.payload)
        _verify_canonical_repository(
            canonical,
            source_lock.canonical_repository,
        )
        initial_sources = _source_group_snapshots(canonical, source_lock)
        for name, snapshot in initial_sources.items():
            if snapshot.mode not in SUPPORTED_SOURCE_MODES:
                raise MirrorSyncError(
                    f"canonical source {name} mode must be 0644 or 0755, "
                    f"not {snapshot.mode:04o}"
                )
        refreshed_sources = {
            name: replace(
                source,
                sha256=hashlib.sha256(initial_sources[name].payload).hexdigest(),
                mode=initial_sources[name].mode,
            )
            for name, source in sorted(source_lock.sources.items())
        }
        refreshed = replace(source_lock, sources=refreshed_sources)
        expected_payload = _source_lock_payload(refreshed)

        prepublication_lock = _safe_read_snapshot(canonical, LOCK_PATH)
        if prepublication_lock != initial_lock:
            raise MirrorSyncError(
                "source lock changed during refresh before publication"
            )
        prepublication_sources = _source_group_snapshots(canonical, source_lock)
        _require_same_source_group(initial_sources, prepublication_sources)

        if check:
            if initial_lock.payload != expected_payload or initial_lock.mode != 0o644:
                raise MirrorSyncError(
                    "source lock is stale; run refresh-lock after canonical changes"
                )
        else:
            _atomic_write_relative(
                canonical,
                LOCK_PATH,
                expected_payload,
                0o644,
                create_parents=False,
                expected_snapshot=initial_lock,
            )

        published_lock = _safe_read_snapshot(canonical, LOCK_PATH)
        if published_lock.payload != expected_payload or published_lock.mode != 0o644:
            raise MirrorSyncError(
                "source lock changed during or immediately after publication"
            )
        final_sources = _source_group_snapshots(canonical, source_lock)
        try:
            _require_same_source_group(initial_sources, final_sources)
        except MirrorSyncError:
            if not check:
                try:
                    _atomic_write_relative(
                        canonical,
                        LOCK_PATH,
                        initial_lock.payload,
                        initial_lock.mode,
                        create_parents=False,
                        expected_snapshot=published_lock,
                    )
                except MirrorSyncError as rollback_error:
                    raise MirrorSyncError(
                        "canonical sources changed after lock publication and "
                        f"conditional rollback failed: {rollback_error}"
                    ) from rollback_error
            raise
        if check:
            final_lock = _safe_read_snapshot(canonical, LOCK_PATH)
            if final_lock != initial_lock:
                raise MirrorSyncError("source lock changed during refresh check")
        return len(refreshed.sources)
    finally:
        _finish_bound_roots(canonical)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate read-only consumer mirrors from codex-personal-sync "
            "canonical sources."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "generate", "managed-paths"):
        subparser = subparsers.add_parser(
            command,
            help=f"{command.capitalize()} one explicitly selected consumer mirror",
        )
        subparser.add_argument(
            "--target-root",
            type=Path,
            required=True,
            help="Existing root of the selected consumer repository",
        )
        subparser.add_argument(
            "--mirror",
            required=True,
            help="Mirror name declared in sync-source-lock.json",
        )
        if command == "generate":
            subparser.add_argument(
                "--source-commit",
                required=True,
                help=(
                    "Exact committed canonical HEAD SHA whose clean locked "
                    "sources will be generated"
                ),
            )
        elif command == "managed-paths":
            subparser.add_argument(
                "--target-commit",
                help=(
                    "Read and validate managed paths from this exact committed "
                    "target tree without checking it out"
                ),
            )
    refresh_parser = subparsers.add_parser(
        "refresh-lock",
        help="Refresh canonical source hashes without reading any consumer",
    )
    refresh_parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the lock needs a refresh instead of writing it",
    )
    history_parser = subparsers.add_parser(
        "validate-branch-history",
        help=(
            "Validate every commit-parent edge in one exact generated "
            "branch-exclusive history"
        ),
    )
    history_parser.add_argument(
        "--target-root",
        type=Path,
        required=True,
        help="Existing generated-mirror repository checkout",
    )
    history_parser.add_argument(
        "--base-ref",
        required=True,
        help="Exact lowercase SHA-1 target-base commit",
    )
    history_parser.add_argument(
        "--head-ref",
        required=True,
        help="Exact lowercase SHA-1 generated-branch head commit",
    )
    history_parser.add_argument(
        "--worktree-head-ref",
        help=(
            "Exact lowercase SHA-1 that the unchanged worktree HEAD/index must "
            "match; defaults to --head-ref"
        ),
    )
    history_parser.add_argument(
        "--allowed-paths-file",
        type=Path,
        required=True,
        help="Current-user-owned canonical JSON array of generated paths",
    )
    history_parser.add_argument(
        "--require-fresh-head",
        action="store_true",
        help=(
            "Require either the exact base or one single-parent generated "
            "commit directly on that base"
        ),
    )
    history_limits = (
        (
            "--max-commits",
            MAX_SYNC_HISTORY_COMMITS,
            "Maximum branch-exclusive commits",
        ),
        (
            "--max-parents-per-commit",
            MAX_SYNC_HISTORY_PARENTS_PER_COMMIT,
            "Maximum parents on one commit",
        ),
        (
            "--max-parent-edges",
            MAX_SYNC_HISTORY_PARENT_EDGES,
            "Maximum validated commit-parent edges",
        ),
        (
            "--max-path-events",
            MAX_SYNC_HISTORY_PATH_EVENTS,
            "Maximum changed-path events across all edges",
        ),
        (
            "--max-path-bytes",
            MAX_SYNC_HISTORY_PATH_BYTES,
            "Maximum aggregate raw changed-path bytes",
        ),
        (
            "--max-blobs",
            MAX_SYNC_HISTORY_BLOBS,
            "Maximum distinct introduced blobs",
        ),
        (
            "--max-blob-bytes",
            MAX_SYNC_HISTORY_BLOB_BYTES,
            "Maximum aggregate logical introduced-blob bytes",
        ),
    )
    for option, default, help_text in history_limits:
        history_parser.add_argument(
            option,
            type=int,
            default=default,
            help=help_text,
        )
    recovery_parser = subparsers.add_parser(
        "recover-private-control",
        help="Adopt exact legacy private-control evidence without moving it",
    )
    recovery_parser.add_argument(
        "--root-id",
        required=True,
        choices=(PRIVATE_CONTROL_LEGACY_ROOT_ID,),
    )
    recovery_mode = recovery_parser.add_mutually_exclusive_group(required=True)
    recovery_mode.add_argument("--dry-run", action="store_true")
    recovery_mode.add_argument("--execute", action="store_true")
    recovery_parser.add_argument("--output-receipt", type=Path)
    recovery_parser.add_argument("--receipt", type=Path)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "check":
        count = check_mirror(REPOSITORY_ROOT, args.target_root, args.mirror)
        print(f"mirror {args.mirror} is synchronized ({count} files)")
    elif args.command == "managed-paths":
        paths = managed_mirror_paths(
            REPOSITORY_ROOT,
            args.target_root,
            args.mirror,
            args.target_commit,
        )
        sys.stdout.buffer.write(
            _canonical_json(
                [path.as_posix() for path in paths],
                pretty=True,
            )
        )
    elif args.command == "generate":
        count = generate_mirror(
            REPOSITORY_ROOT,
            args.target_root,
            args.mirror,
            args.source_commit,
        )
        print(f"generated mirror {args.mirror} ({count} files)")
    elif args.command == "refresh-lock":
        count = refresh_source_lock(REPOSITORY_ROOT, check=args.check)
        action = "verified" if args.check else "refreshed"
        print(f"{action} source lock ({count} sources)")
    elif args.command == "validate-branch-history":
        receipt = validate_sync_branch_history(
            args.target_root,
            args.base_ref,
            args.head_ref,
            args.allowed_paths_file,
            limits=_sync_history_limits_from_args(args),
            require_fresh_head=args.require_fresh_head,
            worktree_head_ref=args.worktree_head_ref,
        )
        sys.stdout.buffer.write(_canonical_json(receipt, pretty=True))
    elif args.command == "recover-private-control":
        if args.dry_run:
            if args.output_receipt is None or args.receipt is not None:
                raise MirrorSyncError(
                    "--dry-run requires --output-receipt and rejects --receipt"
                )
            receipt = plan_private_control_recovery(
                args.root_id,
                args.output_receipt,
            )
            sys.stdout.buffer.write(_pc_recovery_json_bytes(receipt, pretty=True))
        else:
            if args.receipt is None or args.output_receipt is not None:
                raise MirrorSyncError(
                    "--execute requires --receipt and rejects --output-receipt"
                )
            receipt = execute_private_control_recovery(
                args.root_id,
                args.receipt,
            )
            sys.stdout.buffer.write(_pc_recovery_json_bytes(receipt, pretty=True))
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except MirrorSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if sys.platform == "darwin":
    GIT_EXECUTABLE = _resolve_macos_git_executable()


if __name__ == "__main__":
    raise SystemExit(main())
