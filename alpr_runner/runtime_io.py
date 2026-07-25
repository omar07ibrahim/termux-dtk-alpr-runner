from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

MAX_RUNTIME_RECORD_BYTES = 16 * 1024 * 1024


class RuntimeStorageError(RuntimeError):
    """Raised when runtime output cannot be stored at the private boundary."""


def prepare_private_directory(path: str | Path) -> Path:
    """Create one runtime directory and require a private, non-symlink leaf."""

    requested = Path(path).expanduser()
    try:
        leaf_status = os.lstat(requested)
    except FileNotFoundError:
        requested.mkdir(mode=0o700, parents=True, exist_ok=False)
        leaf_status = os.lstat(requested)
    if stat.S_ISLNK(leaf_status.st_mode) or not stat.S_ISDIR(leaf_status.st_mode):
        raise RuntimeStorageError("runtime output must be a real directory, not a link")
    resolved = requested.resolve(strict=True)
    os.chmod(resolved, 0o700)
    return resolved


def source_descriptor(kind: str, value: str | int) -> dict[str, Any]:
    """Return a status-safe source description without paths or URL authority."""

    if kind == "device":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeStorageError("device source must use a non-negative index")
        return {"kind": "device", "device_index": value}
    if kind not in {"file", "rtsp", "watch-file", "dir"}:
        raise RuntimeStorageError(f"unsupported source kind: {kind}")
    if not isinstance(value, str):
        raise RuntimeStorageError(f"{kind} source must be text")
    return {"kind": kind}


def private_relative_path(path: Path, directory: Path) -> str:
    """Describe a runtime artifact without publishing its host directory."""

    resolved_directory = directory.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        relative = resolved_path.relative_to(resolved_directory)
    except ValueError as error:
        raise RuntimeStorageError(
            "runtime artifact escaped its output directory"
        ) from error
    if relative == Path("."):
        raise RuntimeStorageError("runtime artifact must be a file")
    return relative.as_posix()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_bytes(path, payload)


def atomic_text(path: Path, text: str) -> None:
    atomic_bytes(path, text.encode("utf-8"))


def atomic_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace a bounded regular file at mode 0600."""

    if len(payload) > MAX_RUNTIME_RECORD_BYTES:
        raise RuntimeStorageError(
            f"runtime record exceeds {MAX_RUNTIME_RECORD_BYTES} bytes"
        )
    if not path.name or path.name in {".", ".."} or path.parent == path:
        raise RuntimeStorageError("runtime artifact name is invalid")

    directory_flags = os.O_RDONLY
    for flag_name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flag = getattr(os, flag_name, None)
        if flag is None:
            raise RuntimeStorageError(
                "private runtime storage requires POSIX open flags"
            )
        directory_flags |= flag
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as error:
        raise RuntimeStorageError("cannot open private runtime directory") from error

    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        _reject_nonregular_destination(directory_fd, path.name)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for flag_name in ("O_CLOEXEC", "O_NOFOLLOW"):
            file_flags |= getattr(os, flag_name)
        descriptor = os.open(
            temporary_name,
            file_flags,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise RuntimeStorageError("runtime artifact write made no progress")
                written += count
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
        _require_private_regular_file(directory_fd, path.name)
    except OSError as error:
        raise RuntimeStorageError("cannot replace private runtime artifact") from error
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def protect_runtime_file(path: Path) -> None:
    """Require an existing regular non-symlink file and set mode 0600."""

    flags = os.O_RDONLY
    for flag_name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flag = getattr(os, flag_name, None)
        if flag is None:
            raise RuntimeStorageError(
                "private runtime storage requires POSIX open flags"
            )
        flags |= flag
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeStorageError("cannot open runtime artifact") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeStorageError("runtime artifact must be a regular file")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_nonregular_destination(directory_fd: int, name: str) -> None:
    try:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(status.st_mode):
        raise RuntimeStorageError("runtime destination must be a regular file")


def _require_private_regular_file(directory_fd: int, name: str) -> None:
    status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode):
        raise RuntimeStorageError("written runtime artifact is not a regular file")
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise RuntimeStorageError("written runtime artifact does not have mode 0600")
