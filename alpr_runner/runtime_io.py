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
    """Create and pin a real directory path, then require mode 0700."""

    requested = _absolute_runtime_path(path)
    if requested == Path("/"):
        raise RuntimeStorageError("runtime output must not be the filesystem root")
    absolute, descriptors, entries = _open_pinned_directory(
        requested,
        create=True,
        error_message="cannot prepare private runtime directory",
    )
    try:
        os.fchmod(descriptors[-1], 0o700)
        _require_private_directory(descriptors[-1])
        _validate_directory_chain(entries)
    except RuntimeStorageError:
        raise
    except OSError as error:
        raise RuntimeStorageError(
            "cannot prepare private runtime directory"
        ) from error
    finally:
        _close_descriptors(descriptors)
    return absolute


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

    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        relative = resolved_path.relative_to(resolved_directory)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeStorageError(
            "runtime artifact escaped or cannot be resolved inside its output directory"
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
    absolute = _absolute_runtime_path(path)
    if (
        not absolute.name
        or absolute.name in {".", ".."}
        or absolute.parent == absolute
    ):
        raise RuntimeStorageError("runtime artifact name is invalid")

    _directory, descriptors, entries = _open_pinned_directory(
        absolute.parent,
        create=False,
        error_message="cannot open private runtime directory",
    )
    directory_fd = descriptors[-1]

    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        _require_private_directory(directory_fd)
        _reject_nonregular_destination(directory_fd, absolute.name)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for flag_name in ("O_CLOEXEC", "O_NOFOLLOW"):
            flag = getattr(os, flag_name, None)
            if flag is None:
                raise RuntimeStorageError(
                    "private runtime storage requires POSIX open flags"
                )
            file_flags |= flag
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
            _close_descriptor(descriptor)
        os.replace(
            temporary_name,
            absolute.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
        _require_private_regular_file(directory_fd, absolute.name)
        _validate_directory_chain(entries)
    except RuntimeStorageError:
        raise
    except OSError as error:
        raise RuntimeStorageError("cannot replace private runtime artifact") from error
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        _close_descriptors(descriptors)


def protect_runtime_file(path: Path) -> None:
    """Require an existing regular non-symlink file and set mode 0600."""

    absolute = _absolute_runtime_path(path)
    if not absolute.name or absolute.parent == absolute:
        raise RuntimeStorageError("runtime artifact name is invalid")
    _directory, descriptors, entries = _open_pinned_directory(
        absolute.parent,
        create=False,
        error_message="cannot open private runtime directory",
    )
    directory_fd = descriptors[-1]
    flags = os.O_RDONLY
    for flag_name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flag = getattr(os, flag_name, None)
        if flag is None:
            _close_descriptors(descriptors)
            raise RuntimeStorageError(
                "private runtime storage requires POSIX open flags"
            )
        flags |= flag
    descriptor: int | None = None
    try:
        _require_private_directory(directory_fd)
        descriptor = os.open(absolute.name, flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeStorageError("runtime artifact must be a regular file")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if _entry_identity(before) != _entry_identity(after):
            raise RuntimeStorageError("runtime artifact changed while protected")
        _require_private_regular_file(directory_fd, absolute.name)
        _validate_directory_chain(entries)
    except RuntimeStorageError:
        raise
    except OSError as error:
        raise RuntimeStorageError("cannot open runtime artifact") from error
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor)
        _close_descriptors(descriptors)


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


def _absolute_runtime_path(path: str | Path) -> Path:
    try:
        requested = Path(path).expanduser()
        absolute = Path(os.path.abspath(os.fspath(requested)))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeStorageError("runtime path is invalid") from error
    parts = absolute.parts
    if not parts or parts[0] != "/":
        raise RuntimeStorageError("runtime path must be absolute after normalization")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise RuntimeStorageError("runtime path is invalid")
    return absolute


def _required_directory_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        value = getattr(os, name, None)
        if value is None:
            raise RuntimeStorageError(
                "private runtime storage requires POSIX open flags"
            )
        flags |= value
    return flags


def _open_pinned_directory(
    path: Path,
    *,
    create: bool,
    error_message: str,
) -> tuple[
    Path,
    list[int],
    list[tuple[int, str, tuple[int, int, int]]],
]:
    absolute = _absolute_runtime_path(path)
    descriptors: list[int] = []
    entries: list[tuple[int, str, tuple[int, int, int]]] = []
    try:
        flags = _required_directory_flags()
        descriptors.append(os.open("/", flags))
        for component in absolute.parts[1:]:
            parent = descriptors[-1]
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=parent)
                child = os.open(component, flags, dir_fd=parent)
                os.fchmod(child, 0o700)
            descriptors.append(child)
            status = os.fstat(child)
            if not stat.S_ISDIR(status.st_mode):
                raise RuntimeStorageError(
                    "runtime path contains a non-directory component"
                )
            expected = _entry_identity(status)
            _require_visible_directory(parent, component, expected)
            entries.append((parent, component, expected))
    except RuntimeStorageError:
        _close_descriptors(descriptors)
        raise
    except (OSError, RuntimeError, ValueError) as error:
        _close_descriptors(descriptors)
        raise RuntimeStorageError(error_message) from error
    return absolute, descriptors, entries


def _entry_identity(status: os.stat_result) -> tuple[int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        stat.S_IFMT(status.st_mode),
    )


def _require_visible_directory(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int, int],
) -> None:
    try:
        visible = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeStorageError(
            "runtime directory path changed during access"
        ) from error
    if not stat.S_ISDIR(visible.st_mode):
        raise RuntimeStorageError(
            "runtime directory path changed during access"
        )
    if _entry_identity(visible) != expected:
        raise RuntimeStorageError(
            "runtime directory path changed during access"
        )


def _validate_directory_chain(
    entries: list[tuple[int, str, tuple[int, int, int]]],
) -> None:
    for parent, name, expected in entries:
        _require_visible_directory(parent, name, expected)


def _require_private_directory(descriptor: int) -> None:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise RuntimeStorageError("runtime output must be a real directory")
    if status.st_uid != os.geteuid():
        raise RuntimeStorageError(
            "runtime output directory must belong to the current user"
        )
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise RuntimeStorageError("runtime output directory must have mode 0700")


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        _close_descriptor(descriptor)


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
