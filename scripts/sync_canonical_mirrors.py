#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any
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
GENERATOR_CONTRACT_VERSION = 2
RULES_CONTRACT_VERSION = 1
HASH_ALGORITHM = "sha256"
GIT_EXECUTABLE = Path("/usr/bin/git")
LAUNCHER_EXECUTABLE = Path("/usr/bin/python3")
LAUNCHER_PROGRAM = (
    "import os,sys;"
    "os.fchdir(int(sys.argv[1]));"
    "os.execve(sys.argv[2],sys.argv[2:],os.environ)"
)
PRIVATE_GIT_CONTROL_PARENT = Path(
    "/private/tmp" if sys.platform == "darwin" else "/var/tmp"
)
PRIVATE_TOOL_ROOT_NAME = "codex-sync-canonical-mirrors"
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
MAX_GIT_SNAPSHOT_ENTRIES = 100_000
MAX_GIT_SNAPSHOT_BYTES = 512 * 1024 * 1024
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
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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


class MissingPathError(MirrorSyncError):
    pass


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


@dataclass
class BoundRoot:
    path: Path
    fd: int
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    exclusive: bool
    git_executable: ControlObjectBinding
    launcher_executable: ControlObjectBinding
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


@dataclass
class ControlAbsenceBinding:
    label: str
    parent: ControlObjectBinding
    name: str


@dataclass
class GitControlBinding:
    marker: ControlObjectBinding
    admin: ControlObjectBinding
    commondir_file: ControlObjectBinding | None
    common: ControlObjectBinding
    objects: ControlObjectBinding
    source_files: tuple[ControlObjectBinding, ...]
    source_directories: tuple[ControlObjectBinding, ...]
    source_absences: tuple[ControlAbsenceBinding, ...]
    private_parent: ControlObjectBinding
    private: ControlObjectBinding
    private_objects: ControlObjectBinding
    private_name: str
    private_path: Path
    private_manifest: tuple[tuple[object, ...], ...]
    private_objects_manifest: tuple[tuple[object, ...], ...]
    owner_record: ControlObjectBinding
    owner_record_name: str
    owner_nonce: str
    static_profile_verified: bool


@dataclass(frozen=True)
class FileSnapshot:
    payload: bytes
    mode: int
    identity: tuple[int, int, int]
    access_policy: tuple[int, int, int]
    size: int


@dataclass(frozen=True)
class GitEntry:
    path: PurePosixPath
    mode: int
    object_id: str


Root = Path | BoundRoot


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
) -> list[tuple[object, ...]]:
    _operation_checkpoint(operation, f"snapshotting Git control tree {prefix}")
    try:
        names = sorted(os.listdir(source_fd))
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inventory Git control directory {prefix}: {error}"
        ) from error
    manifest: list[tuple[object, ...]] = []
    for name in names:
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
            raise MirrorSyncError(
                f"Git control directory has an unsafe entry: {name!r}"
            )
        relative_path = prefix / name
        budget["entries"] += 1
        _consume_operation_budget(
            operation,
            entry_count=1,
            label=f"snapshotting Git control entry {relative_path}",
        )
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
) -> tuple[tuple[object, ...], ...]:
    first_budget = {"entries": 0, "bytes": 0}
    first = _snapshot_git_directory_tree(
        private_fd,
        None,
        prefix=PurePosixPath(),
        budget=first_budget,
        operation=operation,
        skip_descendants=skip_descendants,
    )
    second_budget = {"entries": 0, "bytes": 0}
    second = _snapshot_git_directory_tree(
        private_fd,
        None,
        prefix=PurePosixPath(),
        budget=second_budget,
        operation=operation,
        skip_descendants=skip_descendants,
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
) -> tuple[tuple[object, ...], ...]:
    return _scan_private_git_tree(
        private_fd,
        operation,
        skip_descendants=frozenset({PRIVATE_OBJECTS_PATH}),
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
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            raise MirrorSyncError(
                f"cannot inventory private Git object directory {prefix}: {error}"
            ) from error
        for name in names:
            if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
                raise MirrorSyncError(
                    f"private Git object directory has an unsafe entry: {name!r}"
                )
            relative_path = prefix / name
            budget["entries"] += 1
            _consume_operation_budget(
                operation,
                entry_count=1,
                label=f"binding private Git object entry {relative_path}",
            )
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


def _bind_private_tool_root(
    root: BoundRoot,
    admin: ControlObjectBinding,
    common: ControlObjectBinding,
) -> ControlObjectBinding:
    parent = _bind_absolute_control_object(
        PRIVATE_GIT_CONTROL_PARENT,
        "private Git control parent",
        require_directory=True,
    )
    try:
        for label, directory_fd in (
            ("repository root", root.fd),
            ("Git admin directory", admin.fd),
            ("Git common directory", common.fd),
        ):
            if _directory_bindings_overlap(parent.fd, directory_fd):
                raise MirrorSyncError(
                    f"private Git control parent overlaps {label}: "
                    f"{PRIVATE_GIT_CONTROL_PARENT}"
                )
        try:
            os.mkdir(PRIVATE_TOOL_ROOT_NAME, 0o700, dir_fd=parent.fd)
            os.fsync(parent.fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise MirrorSyncError(
                f"cannot create private Git tool root: {error}"
            ) from error
        path_metadata = os.stat(
            PRIVATE_TOOL_ROOT_NAME,
            dir_fd=parent.fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(path_metadata.st_mode):
            raise MirrorSyncError("private Git tool root must be a directory")
        tool_fd = os.open(
            PRIVATE_TOOL_ROOT_NAME,
            _DIRECTORY_FLAGS,
            dir_fd=parent.fd,
        )
        opened_metadata = os.fstat(tool_fd)
        if _object_identity(path_metadata) != _object_identity(
            opened_metadata
        ) or _access_policy(path_metadata) != _access_policy(opened_metadata):
            os.close(tool_fd)
            raise MirrorSyncError("private Git tool root was replaced while binding it")
        if (
            stat.S_IMODE(opened_metadata.st_mode) != 0o700
            or opened_metadata.st_uid != os.geteuid()
        ):
            os.close(tool_fd)
            raise MirrorSyncError(
                "private Git tool root must be mode 0700 and owned by the current uid"
            )
        return ControlObjectBinding(
            label="private Git durable tool root",
            path=Path(os.path.abspath(PRIVATE_GIT_CONTROL_PARENT))
            / PRIVATE_TOOL_ROOT_NAME,
            relative_path=None,
            fd=tool_fd,
            identity=_object_identity(opened_metadata),
            access_policy=_access_policy(opened_metadata),
            content_digest=None,
        )
    finally:
        os.close(parent.fd)


def _owner_record_payload(
    private_name: str,
    private_identity: tuple[int, int, int],
    owner_nonce: str,
    phase: str,
) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
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
    private_name: str,
    private: ControlObjectBinding,
) -> tuple[str, str, ControlObjectBinding]:
    owner_nonce = secrets.token_hex(16)
    owner_name = f"{private_name}.owner.json"
    payload = _owner_record_payload(
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
        )
    except BaseException:
        try:
            owner_metadata = os.fstat(owner_fd)
            owner_snapshot = _safe_read_leaf_snapshot(
                tool_root.fd,
                owner_name,
                PurePosixPath(owner_name),
            )
            if owner_snapshot.identity == _object_identity(owner_metadata):
                _remove_stale_owner_record(
                    root,
                    tool_root,
                    owner_name,
                    owner_snapshot,
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
    payload = _owner_record_payload(
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
    private_parent = _bind_private_tool_root(
        root,
        admin,
        common,
    )
    private_name: str | None = None
    private: ControlObjectBinding | None = None
    owner_record: ControlObjectBinding | None = None
    root_locked = False
    try:
        fcntl.flock(
            private_parent.fd,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
        root_locked = True
    except BlockingIOError as error:
        os.close(private_parent.fd)
        raise MirrorSyncError(
            "private Git tool root is busy with another bounded "
            "snapshot/recovery publication"
        ) from error
    try:
        _recover_stale_private_snapshots(root, private_parent)
        private_name, private_path, private = _create_private_git_directory(
            private_parent
        )
        owner_name, owner_nonce, owner_record = _create_owner_record(
            root,
            private_parent,
            private_name,
            private,
        )
    except BaseException:
        if private is not None and private_name is not None:
            try:
                _remove_bound_private_directory(
                    root,
                    private_parent,
                    private,
                    private_name,
                    None,
                )
            finally:
                os.close(private.fd)
        if root_locked:
            fcntl.flock(private_parent.fd, fcntl.LOCK_UN)
            root_locked = False
        os.close(private_parent.fd)
        raise
    finally:
        if root_locked and private_parent.fd >= 0:
            fcntl.flock(private_parent.fd, fcntl.LOCK_UN)
    assert private is not None
    assert private_name is not None
    assert owner_record is not None
    source_files: list[ControlObjectBinding] = []
    final_manifest: tuple[tuple[object, ...], ...] = ()
    try:
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
        source_manifest = _logical_git_snapshot_manifest(first_manifest)
        copied_manifest = _scan_private_git_tree(
            private.fd,
            root.operation,
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
        )
        if final_manifest != _git_control_plane_manifest(expected_manifest):
            raise MirrorSyncError(
                "private Git control plane differs from bound source bytes"
            )
        _set_owner_record_phase(
            owner_record,
            private_name,
            private.identity,
            owner_nonce,
            "ready",
        )
        os.fsync(private_parent.fd)
    except BaseException:
        for binding in source_files:
            os.close(binding.fd)
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
        finally:
            os.close(owner_record.fd)
            os.close(private.fd)
            os.close(private_parent.fd)
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
    commondir_file: ControlObjectBinding | None,
    initial_files: tuple[ControlObjectBinding, ...],
    private_manifest: tuple[tuple[object, ...], ...],
    acquired: list[ControlObjectBinding],
) -> tuple[
    tuple[ControlObjectBinding, ...],
    tuple[ControlObjectBinding, ...],
    tuple[ControlAbsenceBinding, ...],
]:
    files = list(initial_files)
    directories: list[ControlObjectBinding] = []
    absences: list[ControlAbsenceBinding] = []
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

    if commondir_file is None:
        absences.append(
            ControlAbsenceBinding(
                label="Git commondir control file",
                parent=admin,
                name="commondir",
            )
        )
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


def _revalidate_control_object(
    root: BoundRoot,
    binding: ControlObjectBinding,
) -> None:
    try:
        if binding.relative_path is not None:
            path_metadata = os.stat(
                binding.relative_path.as_posix(),
                dir_fd=root.fd,
                follow_symlinks=False,
            )
        else:
            assert binding.path is not None
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


def _revalidate_control_absence(
    root: BoundRoot,
    binding: ControlAbsenceBinding,
) -> None:
    _revalidate_control_object(root, binding.parent)
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
    _revalidate_control_object(root, binding.private_parent)
    _revalidate_control_object(root, binding.private)
    _revalidate_control_object(root, binding.private_objects)
    _revalidate_private_git_objects(
        binding.private_objects.fd,
        binding.private_objects_manifest,
        root.operation,
    )
    observed = _scan_private_git_control(
        binding.private.fd,
        root.operation,
    )
    if observed != binding.private_manifest:
        raise MirrorSyncError(
            "private Git control snapshot changed before transaction completion"
        )


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
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inventory isolated private Git control directory "
            f"{display_path}: {error}"
        ) from error
    for name in names:
        budget["entries"] += 1
        if budget["entries"] > MAX_GIT_SNAPSHOT_ENTRIES:
            raise MirrorSyncError(
                "isolated private Git cleanup exceeds its entry limit"
            )
        _consume_operation_budget(
            operation,
            entry_count=1,
            label=f"cleaning isolated private Git entry {display_path / name}",
        )
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
            raise MirrorSyncError(
                f"isolated private Git control directory has an unsafe entry: {name!r}"
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
    quarantine: ControlObjectBinding | None = None,
) -> None:
    durable_quarantine = (
        quarantine
        if quarantine is not None
        else _bind_durable_quarantine_root(
            root,
            quarantine_parent=PRIVATE_GIT_CONTROL_PARENT,
            source_parent_fd=tool_root.fd,
        )
    )
    close_quarantine = quarantine is None
    try:
        _isolate_and_remove_file(
            root,
            tool_root.fd,
            owner_name,
            owner_snapshot,
            PurePosixPath(owner_name),
            quarantine=durable_quarantine,
            retention_kind=QUARANTINE_TRANSIENT_KIND,
        )
    finally:
        if close_quarantine:
            os.close(durable_quarantine.fd)


def _recover_stale_private_snapshots(
    root: BoundRoot,
    tool_root: ControlObjectBinding,
) -> None:
    _operation_checkpoint(root.operation, "scanning private Git tool root")
    try:
        names = sorted(os.listdir(tool_root.fd))
    except OSError as error:
        raise MirrorSyncError(
            f"cannot inventory private Git tool root: {error}"
        ) from error
    if len(names) > MAX_TOOL_ROOT_ENTRIES:
        raise MirrorSyncError(
            f"private Git tool root exceeds {MAX_TOOL_ROOT_ENTRIES} entries"
        )
    _consume_operation_budget(
        root.operation,
        entry_count=len(names),
        label="scanning private Git tool root",
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
            owner_snapshot = _safe_read_leaf_snapshot(
                tool_root.fd,
                owner_name,
                PurePosixPath(owner_name),
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
            expected_fields = {
                "version",
                "owner_pid",
                "owner_uid",
                "owner_gid",
                "owner_nonce",
                "phase",
                "private_name",
                "private_identity",
            }
            valid_record = (
                isinstance(record, dict)
                and set(record) == expected_fields
                and record["version"] == 1
                and record["owner_uid"] == os.geteuid()
                and record["owner_gid"] == os.getegid()
                and isinstance(record["owner_pid"], int)
                and isinstance(record["owner_nonce"], str)
                and re.fullmatch(
                    r"[0-9a-f]{32}",
                    record["owner_nonce"],
                )
                is not None
                and record["phase"] in {"building", "ready", "cleanup"}
                and record["private_name"] == private_name
                and isinstance(record["private_identity"], list)
                and len(record["private_identity"]) == 3
                and all(isinstance(item, int) for item in record["private_identity"])
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
                quarantine = _bind_durable_quarantine_root(
                    root,
                    quarantine_parent=PRIVATE_GIT_CONTROL_PARENT,
                    source_parent_fd=tool_root.fd,
                )
                try:
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
                    )
                finally:
                    os.close(quarantine.fd)
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
) -> None:
    _revalidate_control_object(root, private_parent)
    _revalidate_control_object(root, owner_record)
    owner_snapshot = _safe_read_leaf_snapshot(
        private_parent.fd,
        owner_name,
        PurePosixPath(owner_name),
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
    # Bind the durable destination while every Git control path still exists.
    # Removing the private snapshot first intentionally invalidates its path,
    # so a later BoundRoot-based bind would fail recursive control-plane
    # revalidation before the owner record could be preserved.
    quarantine = _bind_durable_quarantine_root(
        root,
        quarantine_parent=PRIVATE_GIT_CONTROL_PARENT,
        source_parent_fd=private_parent.fd,
    )
    try:
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
            quarantine,
        )
    finally:
        os.close(quarantine.fd)


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
            bind_content=False,
        )
    except BaseException:
        fcntl.flock(root_fd, fcntl.LOCK_UN)
        os.close(root_fd)
        raise
    try:
        launcher_executable = _bind_absolute_control_object(
            LAUNCHER_EXECUTABLE,
            "Git launcher executable",
            require_directory=False,
            bind_content=False,
        )
    except BaseException:
        os.close(git_executable.fd)
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
        launcher_executable=launcher_executable,
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
    _revalidate_bound_root_directory(root)
    _revalidate_control_object(root, root.git_executable)
    _revalidate_control_object(root, root.launcher_executable)
    for binding in root.managed_ancestors.values():
        _revalidate_control_object(root, binding)
    if root.git_control is not None:
        _revalidate_control_object(root, root.git_control.marker)
        _revalidate_control_object(root, root.git_control.admin)
        if root.git_control.commondir_file is not None:
            _revalidate_control_object(
                root,
                root.git_control.commondir_file,
            )
        _revalidate_control_object(root, root.git_control.common)
        _revalidate_control_object(root, root.git_control.objects)
        for binding in root.git_control.source_directories:
            _revalidate_control_object(root, binding)
        for binding in root.git_control.source_files:
            _revalidate_control_object(root, binding)
        for binding in root.git_control.source_absences:
            _revalidate_control_absence(root, binding)
        _revalidate_control_object(root, root.git_control.owner_record)
        _revalidate_private_git_control(root, root.git_control)


def _close_bound_root(root: BoundRoot) -> None:
    cleanup_error: MirrorSyncError | None = None
    if root.git_control is not None:
        try:
            _cleanup_private_git_control(
                root,
                root.git_control.private_parent,
                root.git_control.private,
                root.git_control.private_name,
                root.git_control.private_manifest,
                root.git_control.owner_record,
                root.git_control.owner_record_name,
                root.git_control.owner_nonce,
            )
        except MirrorSyncError as error:
            cleanup_error = error
        for binding in (
            root.git_control.marker,
            root.git_control.admin,
            root.git_control.commondir_file,
            root.git_control.common,
            root.git_control.objects,
            *root.git_control.source_files,
            *root.git_control.source_directories,
            root.git_control.private_objects,
            root.git_control.private,
            root.git_control.private_parent,
            root.git_control.owner_record,
        ):
            if binding is not None:
                os.close(binding.fd)
        root.git_control = None
    for binding in root.managed_ancestors.values():
        os.close(binding.fd)
    root.managed_ancestors.clear()
    root.managed_ancestor_paths.clear()
    os.close(root.git_executable.fd)
    root.git_executable.fd = -1
    os.close(root.launcher_executable.fd)
    root.launcher_executable.fd = -1
    fcntl.flock(root.fd, fcntl.LOCK_UN)
    os.close(root.fd)
    root.fd = -1
    if cleanup_error is not None:
        raise cleanup_error


def _finish_bound_roots(*roots: BoundRoot) -> None:
    first_error: MirrorSyncError | None = None
    for root in roots:
        try:
            _revalidate_bound_root(root)
        except MirrorSyncError as error:
            if first_error is None:
                first_error = error
    for root in reversed(roots):
        try:
            _close_bound_root(root)
        except MirrorSyncError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


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
    try:
        for _depth in range(MAX_ROOT_ANCESTOR_DEPTH):
            current_identity = _object_identity(os.fstat(current_fd))
            if current_identity == ancestor_identity:
                return True
            try:
                parent_fd = os.open("..", _DIRECTORY_FLAGS, dir_fd=current_fd)
            except OSError as error:
                raise MirrorSyncError(
                    f"cannot validate target root ancestry: {error}"
                ) from error
            parent_identity = _object_identity(os.fstat(parent_fd))
            os.close(current_fd)
            current_fd = parent_fd
            if parent_identity == current_identity:
                return False
    finally:
        os.close(current_fd)
    raise MirrorSyncError(
        f"target root ancestry exceeds {MAX_ROOT_ANCESTOR_DEPTH} directories"
    )


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
    source_paths: set[PurePosixPath] = set()
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
        if path == LOCK_PATH:
            raise MirrorSyncError("source lock must not hash itself")
        if path in source_paths:
            raise MirrorSyncError(f"duplicate canonical source path: {path}")
        source_paths.add(path)
        sha256 = source["sha256"]
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise MirrorSyncError(f"source {name} sha256 must be lowercase hex")
        raw_mode = source["mode"]
        if not isinstance(raw_mode, str) or MODE_RE.fullmatch(raw_mode) is None:
            raise MirrorSyncError(
                f"source {name} mode must be a four-digit octal string"
            )
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
    repositories: set[str] = set()
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
        if repository == canonical_repository:
            raise MirrorSyncError(
                f"mirror {name} must not point back to the canonical repository"
            )
        if repository in repositories:
            raise MirrorSyncError(f"duplicate mirror repository: {repository}")
        repositories.add(repository)
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


def _terminate_git_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # Some sandboxes deny killpg even for a new child session. Kill the
        # direct child as a fail-closed fallback; hooks, prompts, and network
        # helpers are disabled for this Git profile.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as error:
            raise MirrorSyncError(
                f"cannot terminate bounded Git process: {error}"
            ) from error
    try:
        process.wait(timeout=GIT_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise MirrorSyncError(
            "bounded Git process group did not terminate after SIGKILL"
        ) from error


def _collect_bounded_process_output(
    process: subprocess.Popen[bytes],
    operation: OperationBudget | None,
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout_seconds: float,
    label: str,
) -> tuple[int, bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        _terminate_git_process(process)
        raise MirrorSyncError("bounded Git process pipes were not created")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
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
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_git_process(process)
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
                    _terminate_git_process(process)
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
                    _terminate_git_process(process)
                    raise MirrorSyncError(
                        f"bounded {label} {stream_name} exceeds the "
                        f"{maximum}-byte limit"
                    )
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _terminate_git_process(process)
            raise MirrorSyncError(
                f"bounded {label} exceeded {timeout_seconds} seconds"
            ) from error
        return return_code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
    except BaseException:
        _terminate_git_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _collect_bounded_git_output(
    process: subprocess.Popen[bytes],
    operation: OperationBudget | None = None,
) -> tuple[int, bytes, bytes]:
    return _collect_bounded_process_output(
        process,
        operation,
        stdout_limit=MAX_GIT_STDOUT_BYTES,
        stderr_limit=MAX_GIT_STDERR_BYTES,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        label="Git verification",
    )


def _parse_git_version_output(payload: bytes) -> tuple[int, int, int]:
    match = GIT_VERSION_RE.fullmatch(payload)
    if match is None:
        raise MirrorSyncError("Git capability probe returned malformed version output")
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def _verify_git_capability(bound_root: BoundRoot) -> None:
    if bound_root.git_capability_verified:
        _revalidate_bound_root(bound_root)
        return
    command = [
        LAUNCHER_EXECUTABLE.as_posix(),
        "-I",
        "-B",
        "-S",
        "-c",
        LAUNCHER_PROGRAM,
        str(bound_root.fd),
        GIT_EXECUTABLE.as_posix(),
        "--no-lazy-fetch",
        "--version",
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        _revalidate_bound_root(bound_root)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            pass_fds=(bound_root.fd,),
            start_new_session=True,
        )
        return_code, stdout, stderr = _collect_bounded_process_output(
            process,
            bound_root.operation,
            stdout_limit=MAX_GIT_VERSION_STDOUT_BYTES,
            stderr_limit=MAX_GIT_VERSION_STDERR_BYTES,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            label="Git capability probe",
        )
    except OSError as error:
        if process is not None:
            _terminate_git_process(process)
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
        LAUNCHER_EXECUTABLE.as_posix(),
        "-I",
        "-B",
        "-S",
        "-c",
        LAUNCHER_PROGRAM,
        str(bound_root.git_control.private.fd),
        GIT_EXECUTABLE.as_posix(),
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-replace-objects",
        "config",
        f"--file={config_name}",
        "--no-includes",
        "--null",
        "--list",
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        _revalidate_bound_root(bound_root)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            pass_fds=(
                bound_root.fd,
                bound_root.git_control.private.fd,
            ),
            start_new_session=True,
        )
        return_code, stdout, stderr = _collect_bounded_git_output(
            process,
            bound_root.operation,
        )
    except OSError as error:
        if process is not None:
            _terminate_git_process(process)
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
        if relative_path.as_posix() in {
            "info/alternates",
            "info/http-alternates",
        }:
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
            if key == "extensions.partialclone" or (
                key.startswith("remote.")
                and (key.endswith(".promisor") or key.endswith(".partialclonefilter"))
            ):
                raise MirrorSyncError(
                    f"partial/promisor Git config state is not allowed: {key}"
                )
    binding.static_profile_verified = True
    _revalidate_bound_root(bound_root)


def _run_git_process(
    bound_root: BoundRoot,
    *arguments: str,
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
        GIT_EXECUTABLE.as_posix(),
        "--no-lazy-fetch",
        "--git-dir=.",
        f"--work-tree={bound_root.path}",
        "--no-optional-locks",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *arguments,
    ]
    command = [
        LAUNCHER_EXECUTABLE.as_posix(),
        "-I",
        "-B",
        "-S",
        "-c",
        LAUNCHER_PROGRAM,
        str(bound_root.git_control.private.fd),
        *git_command,
    ]

    process: subprocess.Popen[bytes] | None = None
    try:
        _revalidate_bound_root(bound_root)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            pass_fds=(
                bound_root.fd,
                bound_root.git_control.private.fd,
            ),
            start_new_session=True,
        )
        return_code, stdout, stderr = _collect_bounded_git_output(
            process,
            bound_root.operation,
        )
    except OSError as error:
        if process is not None:
            _terminate_git_process(process)
        raise MirrorSyncError(
            f"cannot run bounded Git verification: {error}"
        ) from error
    finally:
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


def _ensure_git_control_binding(root: BoundRoot) -> None:
    if root.git_control is not None:
        if not root.git_capability_verified:
            raise MirrorSyncError("Git capability gate has not completed")
        if not root.git_control.static_profile_verified:
            raise MirrorSyncError("Git static repository profile has not completed")
        _revalidate_bound_root(root)
        return
    marker = _bind_git_marker(root)
    controls: list[ControlObjectBinding] = []
    commondir_file: ControlObjectBinding | None = None
    private_path: Path | None = None
    private: ControlObjectBinding | None = None
    private_objects: ControlObjectBinding | None = None
    private_parent: ControlObjectBinding | None = None
    private_name: str | None = None
    private_manifest: tuple[tuple[object, ...], ...] = ()
    private_objects_manifest: tuple[tuple[object, ...], ...] = ()
    source_files: tuple[ControlObjectBinding, ...] = ()
    source_directories: tuple[ControlObjectBinding, ...] = ()
    source_absences: tuple[ControlAbsenceBinding, ...] = ()
    acquired_source_controls: list[ControlObjectBinding] = []
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
        commondir_file = _bind_relative_control_file(
            admin,
            "commondir",
            "Git commondir control file",
        )
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
        objects = _bind_absolute_control_object(
            common_path / "objects",
            "Git object directory",
            require_directory=True,
        )
        controls.append(objects)
        for binding in (marker, admin, common, objects):
            _revalidate_control_object(root, binding)
        _verify_git_capability(root)
        for binding in (marker, admin, common, objects):
            _revalidate_control_object(root, binding)
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
        )
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
            commondir_file,
            source_files,
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
            common=common,
            objects=objects,
            source_files=source_files,
            source_directories=source_directories,
            source_absences=source_absences,
            private_parent=private_parent,
            private=private,
            private_objects=private_objects,
            private_name=private_name,
            private_path=private_path,
            private_manifest=private_manifest,
            private_objects_manifest=private_objects_manifest,
            owner_record=owner_record,
            owner_record_name=owner_record_name,
            owner_nonce=owner_nonce,
            static_profile_verified=False,
        )
        _verify_static_git_profile(root)
        _revalidate_bound_root(root)
    except BaseException:
        root.git_control = None
        os.close(marker.fd)
        if commondir_file is not None:
            os.close(commondir_file.fd)
        closed_source_fds: set[int] = set()
        for binding in (
            *source_files,
            *source_directories,
            *acquired_source_controls,
        ):
            if binding.fd not in closed_source_fds:
                os.close(binding.fd)
                closed_source_fds.add(binding.fd)
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
                        private_manifest or None,
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
                        private_manifest or None,
                    )
            finally:
                if owner_record is not None:
                    os.close(owner_record.fd)
                os.close(private.fd)
                os.close(private_parent.fd)
        if private_objects is not None:
            os.close(private_objects.fd)
        for binding in controls:
            os.close(binding.fd)
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
    if actual_repository != expected_repository:
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


def _target_index_snapshot(
    target_root: BoundRoot,
    mirror: MirrorSpec,
    additional_paths: set[PurePosixPath] | frozenset[PurePosixPath] = frozenset(),
) -> bytes:
    paths = sorted(
        {RECEIPT_PATH, *mirror.files.values(), *additional_paths},
        key=PurePosixPath.as_posix,
    )
    return _run_git(
        target_root,
        "ls-files",
        "--full-name",
        "--stage",
        "-z",
        "--",
        *(_top_literal_pathspec(path) for path in paths),
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


def _load_transaction_journal(
    target_root: BoundRoot,
) -> tuple[FileSnapshot, dict[str, Any]] | None:
    completion = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_COMPLETE_PATH,
    )
    published = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_PATH,
    )
    if completion is not None:
        if published is None:
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
            published = _safe_read_snapshot(
                target_root,
                TRANSACTION_PATH,
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
    temporary = _optional_safe_read_snapshot(
        target_root,
        TRANSACTION_TEMP_PATH,
    )
    if temporary is not None:
        if published is None:
            # Target mutation starts only after the complete pending file is
            # hard-linked as the authoritative journal. A lone pending file is
            # therefore a pre-mutation crash and can be discarded, even if its
            # JSON write was interrupted.
            _isolate_and_remove_file(
                target_root,
                target_root.fd,
                TRANSACTION_TEMP_PATH.as_posix(),
                temporary,
                TRANSACTION_TEMP_PATH,
                retention_kind=QUARANTINE_TRANSIENT_KIND,
            )
            temporary = None
        elif published != temporary:
            raise MirrorSyncError(
                "generation transaction pending and published journals differ"
            )
        else:
            _isolate_and_remove_file(
                target_root,
                target_root.fd,
                TRANSACTION_TEMP_PATH.as_posix(),
                temporary,
                TRANSACTION_TEMP_PATH,
                retention_kind=QUARANTINE_TRANSIENT_KIND,
            )
    snapshot = published
    if snapshot is None:
        return None
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
    return snapshot, raw


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
    _configure_managed_ancestors(
        target_root,
        _mirror_operation_paths(mirror),
    )
    _verify_canonical_repository(
        repository_root,
        source_lock.canonical_repository,
    )
    _verify_target_repository(target_root, mirror.repository)
    _reject_git_control_targets(target_root, mirror)
    _reject_pending_exchange_journals(target_root, mirror)
    initial_index = _target_index_snapshot(target_root, mirror)
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


def managed_mirror_paths(
    repository_root: Path,
    target_root: Path,
    mirror_name: str,
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
        initial_index = _target_index_snapshot(
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
    pending_transaction = _load_transaction_journal(target_root)
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
    initial_index = _target_index_snapshot(
        target_root,
        mirror,
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
    refresh_parser = subparsers.add_parser(
        "refresh-lock",
        help="Refresh canonical source hashes without reading any consumer",
    )
    refresh_parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the lock needs a refresh instead of writing it",
    )
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


if __name__ == "__main__":
    raise SystemExit(main())
