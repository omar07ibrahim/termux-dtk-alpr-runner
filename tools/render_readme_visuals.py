#!/usr/bin/env python3
"""Rebuild and verify the README's vendor-independent evidence bundle.

The renderer deliberately uses only the Python standard library.  It executes
the public synthetic-event CLI twice, compares the real artifacts byte for
byte, derives the runtime figures from that result, and publishes through a
staging directory with the manifest replaced last.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))
GENERATED_RELATIVE = Path("docs/visuals/generated")
GENERATED_DIRECTORY = REPOSITORY / GENERATED_RELATIVE
FIXTURE_RELATIVE = Path("examples/synthetic-events-v1.json")
TEMP_RELATIVE = Path(".t")
MANIFEST_NAME = "manifest.sha256.json"

RUNTIME_RESULT = "synthetic-result.json"
TERMINAL_TRANSCRIPT = "terminal-transcript.txt"
TERMINAL_SVG = "terminal-evidence.svg"
EVENT_FLOW_SVG = "event-flow.svg"
ZOOM_GEOMETRY_SVG = "zoom-geometry.svg"
ARCHITECTURE_SVG = "architecture-boundary.svg"
SETUP_SVG = "setup-workflow.svg"

OUTPUT_KINDS: dict[str, str] = {
    RUNTIME_RESULT: "runtime-derived",
    TERMINAL_TRANSCRIPT: "runtime-derived-normalized",
    TERMINAL_SVG: "runtime-derived-normalized",
    EVENT_FLOW_SVG: "runtime-derived",
    ZOOM_GEOMETRY_SVG: "runtime-derived",
    ARCHITECTURE_SVG: "architecture-only",
    SETUP_SVG: "workflow-only",
}
EXPECTED_GENERATED_NAMES = frozenset({*OUTPUT_KINDS, MANIFEST_NAME})
SVG_NAMES = frozenset(
    {
        TERMINAL_SVG,
        EVENT_FLOW_SVG,
        ZOOM_GEOMETRY_SVG,
        ARCHITECTURE_SVG,
        SETUP_SVG,
    }
)

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024
MAX_STREAM_BYTES = 32 * 1024
MAX_SVG_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 10.0
PROCESS_CLEANUP_SECONDS = 1.0
PYTHON_PATH = "/usr/local/bin:/usr/bin:/bin"
COMMAND_DISPLAY = (
    "python3.12",
    "-S",
    "-m",
    "alpr_runner.synthetic",
    "--trace",
    FIXTURE_RELATIVE.as_posix(),
    "--out",
    "PRIVATE-RUNTIME",
)

_SYNTH_TOKEN = re.compile(r"SYNTH-[0-9]{2}\Z")
_SYNTH_CAMERA = re.compile(r"SYNTH-CAM-[0-9]{2}\Z")
_EMAIL = re.compile(
    rb"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    rb"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    rb"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b"
)
_SECRET_MARKERS = (
    re.compile(rb"(?i)\bapi[_-]?key\s*[:=]"),
    re.compile(rb"(?i)\bpassword\s*[:=]"),
    re.compile(rb"(?i)\bauthorization\s*[:=]"),
    re.compile(rb"(?i)\bbearer\s+[a-z0-9._~-]+"),
    re.compile(rb"(?i)\blicense[_-]?key\s*[:=]"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_FORBIDDEN_PUBLISHED_BYTES = (
    b"/home/",
    b"/Users/",
    b"\\Users\\",
    b"rtsp://",
    b"RTSP://",
    b'"confidence"',
    b'"vehicle_',
)


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be reproduced or safely published."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class DemoRun:
    stdout: bytes
    artifact: bytes
    result: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def capture(cls, metadata: os.stat_result) -> _FileSnapshot:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )

    def unchanged(self, metadata: os.stat_result) -> bool:
        return self == _FileSnapshot.capture(metadata)

    def same_identity(self, metadata: os.stat_result) -> bool:
        return (
            self.device == metadata.st_dev
            and self.inode == metadata.st_ino
            and self.mode == metadata.st_mode
        )


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_TEMP_PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _input_paths(root: Path = REPOSITORY) -> tuple[Path, ...]:
    fixed = {
        Path(".gitignore"),
        Path(".github/workflows/ci.yml"),
        Path("README.md"),
        Path("docs/charter.md"),
        Path("docs/threat-model.md"),
        FIXTURE_RELATIVE,
        Path("tools/render_readme_visuals.py"),
    }
    fixed.update(path.relative_to(root) for path in (root / "alpr_runner").glob("*.py"))
    fixed.update(path.relative_to(root) for path in (root / "tests").glob("test_*.py"))
    return tuple(sorted(fixed, key=lambda item: item.as_posix()))


def _safe_os_error(error: OSError) -> str:
    if error.errno is None:
        return type(error).__name__
    return f"errno {error.errno}"


def _open_directory_chain(
    path: Path,
) -> tuple[
    list[int],
    list[tuple[int, str, int, _FileSnapshot]],
]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptors: list[int] = []
    links: list[tuple[int, str, int, _FileSnapshot]] = []
    try:
        descriptors.append(os.open(absolute.anchor, _DIRECTORY_FLAGS))
        for component in absolute.parts[1:]:
            parent = descriptors[-1]
            before = os.stat(component, dir_fd=parent, follow_symlinks=False)
            snapshot = _FileSnapshot.capture(before)
            if not stat.S_ISDIR(before.st_mode):
                raise EvidenceError("directory path contains a non-directory component")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            if not snapshot.unchanged(os.fstat(child)):
                os.close(child)
                raise EvidenceError("directory component changed while being opened")
            descriptors.append(child)
            links.append((parent, component, child, snapshot))
    except EvidenceError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except FileNotFoundError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise EvidenceError(
            f"directory path could not be pinned ({_safe_os_error(error)})"
        ) from None
    return descriptors, links


def _assert_directory_links(
    links: Iterable[tuple[int, str, int, _FileSnapshot]],
) -> None:
    try:
        for parent, component, child, snapshot in links:
            visible = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if not snapshot.same_identity(visible) or not snapshot.same_identity(
                os.fstat(child)
            ):
                raise EvidenceError(
                    "pinned directory ancestry changed during evidence access"
                )
    except EvidenceError:
        raise
    except OSError as error:
        raise EvidenceError(
            "pinned directory ancestry changed during evidence access"
        ) from error


def _relative_parts(relative: Path) -> tuple[str, ...]:
    if relative.is_absolute() or not relative.parts:
        raise EvidenceError("evidence input path is invalid")
    parts = tuple(relative.parts)
    if any(
        part in {"", ".", ".."}
        or "/" in part
        or "\\" in part
        or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in parts
    ):
        raise EvidenceError("evidence input path is invalid")
    return parts


def _require_relative_file(
    root: Path,
    relative: Path,
    *,
    maximum_bytes: int,
) -> bytes:
    if maximum_bytes < 0:
        raise EvidenceError("evidence input byte limit is invalid")
    parts = _relative_parts(relative)
    try:
        root_descriptors, root_links = _open_directory_chain(root)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise EvidenceError("required evidence input is unavailable") from error
    directory_descriptors: list[int] = []
    relative_links: list[tuple[int, str, int, _FileSnapshot]] = []
    descriptor: int | None = None
    try:
        directory_descriptors.append(os.dup(root_descriptors[-1]))
        for component in parts[:-1]:
            parent = directory_descriptors[-1]
            before = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
            snapshot = _FileSnapshot.capture(before)
            if not stat.S_ISDIR(before.st_mode):
                raise EvidenceError("evidence input path is not a real directory")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            if not snapshot.unchanged(os.fstat(child)):
                os.close(child)
                raise EvidenceError("evidence input path changed while being opened")
            directory_descriptors.append(child)
            relative_links.append((parent, component, child, snapshot))

        parent = directory_descriptors[-1]
        leaf = parts[-1]
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        snapshot = _FileSnapshot.capture(before)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceError("evidence input must be a regular file")
        if before.st_size > maximum_bytes:
            raise EvidenceError("evidence input exceeds its byte limit")
        descriptor = os.open(leaf, _FILE_FLAGS, dir_fd=parent)
        if not snapshot.unchanged(os.fstat(descriptor)):
            raise EvidenceError("evidence input changed while being opened")

        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            try:
                chunk = os.read(descriptor, min(65_536, remaining))
            except BlockingIOError:
                raise EvidenceError("evidence input must not block") from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        visible = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            not snapshot.unchanged(after)
            or not snapshot.unchanged(visible)
            or len(payload) != snapshot.size
        ):
            raise EvidenceError("evidence input changed while being read")
        _assert_directory_links(relative_links)
        _assert_directory_links(root_links)
    except OSError as error:
        raise EvidenceError("evidence input could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
        for root_descriptor in reversed(root_descriptors):
            os.close(root_descriptor)
    if len(payload) > maximum_bytes:
        raise EvidenceError("evidence input exceeds its byte limit")
    return payload


def _snapshot_inputs(root: Path = REPOSITORY) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for relative in _input_paths(root):
        payload = _require_relative_file(
            root,
            relative,
            maximum_bytes=MAX_SOURCE_BYTES,
        )
        records.append(
            {
                "bytes": len(payload),
                "path": relative.as_posix(),
                "sha256": _sha256(payload),
            }
        )
    return tuple(records)


class _PinnedTemporaryDirectory:
    """One private temporary directory held beneath a pinned parent fd."""

    def __init__(
        self,
        *,
        parent: _PinnedTempRoot,
        name: str,
        descriptor: int,
        snapshot: _FileSnapshot,
    ) -> None:
        self.parent = parent
        self.name = name
        self.path = parent.path / name
        self.fd = descriptor
        self.snapshot = snapshot
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.remove()

    def assert_visible(self) -> None:
        if self._closed:
            raise EvidenceError("temporary evidence directory is already closed")
        self.parent.assert_visible()
        try:
            visible = os.stat(
                self.name,
                dir_fd=self.parent.fd,
                follow_symlinks=False,
            )
            opened = os.fstat(self.fd)
        except OSError as error:
            raise EvidenceError(
                "temporary evidence directory changed while in use"
            ) from error
        if (
            not self.snapshot.same_identity(visible)
            or not self.snapshot.same_identity(opened)
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise EvidenceError("temporary evidence directory changed while in use")
        self.parent.assert_visible()

    def remove(self) -> None:
        if self._closed:
            return
        self.assert_visible()
        os.close(self.fd)
        self._closed = True
        try:
            shutil.rmtree(self.name, dir_fd=self.parent.fd)
        except OSError as error:
            raise EvidenceError(
                "temporary evidence directory could not be removed safely"
            ) from error
        try:
            os.stat(
                self.name,
                dir_fd=self.parent.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise EvidenceError("temporary evidence directory still exists")
        self.parent.assert_visible()


class _PinnedTempRoot:
    """Private `.t` root kept open for mkdirat and fd-relative cleanup."""

    def __init__(self, root: Path) -> None:
        self.path = root / TEMP_RELATIVE
        self._root_descriptors, self._root_links = _open_directory_chain(root)
        self._root_descriptor = self._root_descriptors[-1]
        self.fd: int | None = None
        self.snapshot: _FileSnapshot | None = None
        try:
            try:
                status = os.stat(
                    TEMP_RELATIVE.name,
                    dir_fd=self._root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(
                    TEMP_RELATIVE.name,
                    0o700,
                    dir_fd=self._root_descriptor,
                )
                status = os.stat(
                    TEMP_RELATIVE.name,
                    dir_fd=self._root_descriptor,
                    follow_symlinks=False,
                )
            if not stat.S_ISDIR(status.st_mode):
                raise EvidenceError("temporary workspace must be a real directory")
            descriptor = os.open(
                TEMP_RELATIVE.name,
                _DIRECTORY_FLAGS,
                dir_fd=self._root_descriptor,
            )
            self.fd = descriptor
            if not _FileSnapshot.capture(status).unchanged(os.fstat(descriptor)):
                raise EvidenceError("temporary workspace changed while being opened")
            os.fchmod(descriptor, 0o700)
            self.snapshot = _FileSnapshot.capture(os.fstat(descriptor))
            self.assert_visible()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def assert_visible(self) -> None:
        if self.fd is None or self.snapshot is None:
            raise EvidenceError("private temporary workspace is closed")
        try:
            visible = os.stat(
                TEMP_RELATIVE.name,
                dir_fd=self._root_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(self.fd)
        except OSError as error:
            raise EvidenceError(
                "private temporary workspace changed while in use"
            ) from error
        if (
            not self.snapshot.same_identity(visible)
            or not self.snapshot.same_identity(opened)
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise EvidenceError("private temporary workspace changed while in use")
        _assert_directory_links(self._root_links)

    def make_directory(self, prefix: str) -> _PinnedTemporaryDirectory:
        if _TEMP_PREFIX.fullmatch(prefix) is None or not prefix.endswith("-"):
            raise EvidenceError("temporary evidence prefix is invalid")
        self.assert_visible()
        assert self.fd is not None
        for _attempt in range(128):
            name = f"{prefix}{secrets.token_hex(8)}"
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                continue
            except OSError as error:
                raise EvidenceError(
                    "temporary evidence directory could not be created"
                ) from error
            descriptor: int | None = None
            try:
                status = os.stat(
                    name,
                    dir_fd=self.fd,
                    follow_symlinks=False,
                )
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.fd)
                if not _FileSnapshot.capture(status).unchanged(os.fstat(descriptor)):
                    raise EvidenceError(
                        "temporary evidence directory changed while opening"
                    )
                os.fchmod(descriptor, 0o700)
                snapshot = _FileSnapshot.capture(os.fstat(descriptor))
                visible = os.stat(
                    name,
                    dir_fd=self.fd,
                    follow_symlinks=False,
                )
                if not snapshot.unchanged(visible):
                    raise EvidenceError(
                        "temporary evidence directory changed while opening"
                    )
                child = _PinnedTemporaryDirectory(
                    parent=self,
                    name=name,
                    descriptor=descriptor,
                    snapshot=snapshot,
                )
                child.assert_visible()
                descriptor = None
                return child
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    shutil.rmtree(name, dir_fd=self.fd)
                except OSError as cleanup_error:
                    raise EvidenceError(
                        "failed temporary allocation could not be cleaned safely"
                    ) from cleanup_error
                raise
        raise EvidenceError(
            "temporary evidence directory name allocation was exhausted"
        )

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        for descriptor in reversed(self._root_descriptors):
            os.close(descriptor)
        self._root_descriptors.clear()


def _prepare_private_temp_root(root: Path | None = None) -> _PinnedTempRoot:
    selected_root = REPOSITORY if root is None else root
    try:
        return _PinnedTempRoot(selected_root)
    except EvidenceError:
        raise
    except OSError as error:
        raise EvidenceError("private temporary workspace is unavailable") from error


def _signal_process_group(process_group: int, selected: signal.Signals) -> None:
    try:
        os.killpg(process_group, selected)
    except ProcessLookupError:
        return
    except OSError as error:
        raise EvidenceError(
            f"synthetic process group could not be signalled ({_safe_os_error(error)})"
        ) from None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    # The original process group can outlive its leader while a descendant
    # retains our pipes. Signal the PGID even after poll()/wait() reaps the
    # leader. Deliberate setsid()/daemon detachment is outside this fixed
    # no-detach command contract.
    _signal_process_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=PROCESS_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise EvidenceError("synthetic evidence process could not be reaped") from error


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    fixed_command_no_detach: bool,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    stream_limit: int = MAX_STREAM_BYTES,
) -> CommandResult:
    """Run one fixed no-detach command with bounded original-group cleanup."""

    if fixed_command_no_detach is not True:
        raise EvidenceError(
            "bounded runner requires a fixed no-detach command contract"
        )
    if not command or any(
        type(argument) is not str
        or not argument
        or "\x00" in argument
        or "\n" in argument
        or "\r" in argument
        for argument in command
    ):
        raise EvidenceError("synthetic evidence command is invalid")
    if timeout_seconds <= 0 or stream_limit <= 0:
        raise EvidenceError("command limits must be positive")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
            close_fds=True,
        )
    except OSError as error:
        raise EvidenceError("synthetic evidence command could not start") from error

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_seconds
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvidenceError("synthetic evidence command timed out")
            events = selector.select(min(remaining, 0.25))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except OSError as error:
                    raise EvidenceError(
                        "synthetic evidence stream could not be read"
                    ) from error
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[str(key.data)]
                buffer.extend(chunk)
                if len(buffer) > stream_limit:
                    raise EvidenceError(
                        "synthetic evidence command exceeded its output limit"
                    )
        try:
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise EvidenceError("synthetic evidence command timed out") from error
    finally:
        try:
            selector.close()
        finally:
            try:
                process.stdout.close()
            finally:
                try:
                    process.stderr.close()
                finally:
                    _terminate_process_group(process)
    return CommandResult(
        returncode=returncode,
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
    )


def _demo_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": PYTHON_PATH,
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
    }


def _validate_runtime_result(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise EvidenceError("synthetic runtime result must be an object")
    if result.get("backend") != "synthetic-events":
        raise EvidenceError("synthetic runtime backend is not explicit")
    if result.get("input_kind") != "validated-synthetic-event-trace":
        raise EvidenceError("synthetic runtime input boundary is not explicit")
    if result.get("recognition_accuracy") != "not_evaluated":
        raise EvidenceError("synthetic runtime made an accuracy claim")
    if result.get("recognition_performed") is not False:
        raise EvidenceError("synthetic runtime made a recognition claim")
    aggregation = result.get("aggregation")
    trace = result.get("trace")
    events = result.get("events")
    if not isinstance(aggregation, dict) or not isinstance(trace, dict):
        raise EvidenceError("synthetic runtime summary is incomplete")
    if not isinstance(events, list) or len(events) != 5:
        raise EvidenceError("synthetic runtime event count changed")
    if aggregation.get("event_count") != 5:
        raise EvidenceError("synthetic aggregation event count changed")
    if aggregation.get("unique_token_count") != 2:
        raise EvidenceError("synthetic aggregation token count changed")
    if trace.get("camera_count") != 2 or trace.get("event_count") != 5:
        raise EvidenceError("synthetic trace dimensions changed")
    for event in events:
        if not isinstance(event, dict):
            raise EvidenceError("synthetic runtime event is malformed")
        token = event.get("token")
        camera = event.get("camera")
        if not isinstance(token, str) or _SYNTH_TOKEN.fullmatch(token) is None:
            raise EvidenceError("runtime result contains a non-synthetic token")
        if not isinstance(camera, str) or _SYNTH_CAMERA.fullmatch(camera) is None:
            raise EvidenceError("runtime result contains a non-synthetic camera")
    from alpr_runner.synthetic import default_trace, run_synthetic

    if result != run_synthetic(default_trace()):
        raise EvidenceError(
            "synthetic runtime result differs from the canonical production run"
        )
    return result


def _run_demo_once(
    workspace: _PinnedTemporaryDirectory,
    output_name: str,
) -> DemoRun:
    if output_name not in {"run-1", "run-2"}:
        raise EvidenceError("synthetic runtime directory name is invalid")
    workspace.assert_visible()
    try:
        os.mkdir(output_name, 0o700, dir_fd=workspace.fd)
        output_descriptor = os.open(
            output_name,
            _DIRECTORY_FLAGS,
            dir_fd=workspace.fd,
        )
    except OSError as error:
        raise EvidenceError(
            "synthetic runtime directory could not be created safely"
        ) from error
    try:
        os.fchmod(output_descriptor, 0o700)
        output_snapshot = _FileSnapshot.capture(os.fstat(output_descriptor))
        output_visible = os.stat(
            output_name,
            dir_fd=workspace.fd,
            follow_symlinks=False,
        )
        if not output_snapshot.unchanged(output_visible):
            raise EvidenceError(
                "synthetic runtime directory changed while being opened"
            )
        output = workspace.path / output_name
        command = [
            *COMMAND_DISPLAY[:-1],
            str(output),
        ]
        completed = _run_bounded(
            command,
            cwd=REPOSITORY,
            env=_demo_environment(),
            fixed_command_no_detach=True,
        )
        if completed.returncode != 0:
            raise EvidenceError("synthetic evidence command failed")
        if completed.stderr:
            raise EvidenceError("synthetic evidence command wrote to stderr")
        workspace.assert_visible()
        output_visible = os.stat(
            output_name,
            dir_fd=workspace.fd,
            follow_symlinks=False,
        )
        if (
            not output_snapshot.same_identity(output_visible)
            or not output_snapshot.same_identity(os.fstat(output_descriptor))
            or stat.S_IMODE(output_visible.st_mode) != 0o700
        ):
            raise EvidenceError("synthetic runtime directory changed while in use")
        inventory = _inventory_generated_tree(
            output,
            expected_names={RUNTIME_RESULT},
            allow_missing=False,
        )
        status = inventory[RUNTIME_RESULT]
        if stat.S_IMODE(status.st_mode) != 0o600:
            raise EvidenceError("synthetic result mode is not 0600")
        if status.st_size > MAX_RESULT_BYTES:
            raise EvidenceError("synthetic result exceeds its byte limit")
        artifact = _require_relative_file(
            output,
            Path(RUNTIME_RESULT),
            maximum_bytes=MAX_RESULT_BYTES,
        )
        if len(artifact) > MAX_RESULT_BYTES:
            raise EvidenceError("synthetic result exceeds its byte limit")
        final_inventory = _inventory_generated_tree(
            output,
            expected_names={RUNTIME_RESULT},
            allow_missing=False,
        )
        if _FileSnapshot.capture(status) != _FileSnapshot.capture(
            final_inventory[RUNTIME_RESULT]
        ):
            raise EvidenceError("synthetic runtime output changed during validation")
        try:
            decoded = json.loads(artifact)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise EvidenceError(
                "synthetic result is not valid bounded UTF-8 JSON"
            ) from error
        result = _validate_runtime_result(decoded)
        if artifact != _canonical_json(result):
            raise EvidenceError("synthetic result is not canonical JSON")
        from alpr_runner.synthetic import (
            decode_trace,
            render_ascii_summary,
            run_synthetic,
        )

        fixture = _require_relative_file(
            REPOSITORY,
            FIXTURE_RELATIVE,
            maximum_bytes=MAX_SOURCE_BYTES,
        )
        expected_artifact = _canonical_json(run_synthetic(decode_trace(fixture)))
        if artifact != expected_artifact:
            raise EvidenceError(
                "synthetic CLI artifact differs from the bound production modules"
            )
        expected_stdout = render_ascii_summary(
            result,
            RUNTIME_RESULT,
        ).encode("ascii")
        if completed.stdout != expected_stdout:
            raise EvidenceError(
                "synthetic CLI stdout differs from the canonical result summary"
            )
        output_visible = os.stat(
            output_name,
            dir_fd=workspace.fd,
            follow_symlinks=False,
        )
        if not output_snapshot.same_identity(
            output_visible
        ) or not output_snapshot.same_identity(os.fstat(output_descriptor)):
            raise EvidenceError("synthetic runtime directory changed during validation")
        workspace.assert_visible()
        return DemoRun(
            stdout=completed.stdout,
            artifact=artifact,
            result=result,
        )
    finally:
        os.close(output_descriptor)


def _run_deterministic_pair() -> DemoRun:
    with (
        _prepare_private_temp_root() as temporary_root,
        temporary_root.make_directory("readme-evidence-") as workspace,
    ):
        first = _run_demo_once(workspace, "run-1")
        second = _run_demo_once(workspace, "run-2")
        if first.stdout != second.stdout:
            raise EvidenceError("synthetic CLI output is not deterministic")
        if first.artifact != second.artifact:
            raise EvidenceError("synthetic result is not deterministic")
        workspace.assert_visible()
        return first


def _svg_document(
    *,
    slug: str,
    title: str,
    description: str,
    width: int,
    height: int,
    body: str,
) -> bytes:
    title_id = f"{slug}-title"
    description_id = f"{slug}-description"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="{title_id} {description_id}">
  <title id="{title_id}">{html.escape(title)}</title>
  <desc id="{description_id}">{html.escape(description)}</desc>
  <rect width="{width}" height="{height}" rx="18" fill="#08111f"/>
{body}
</svg>
"""
    return document.encode("utf-8")


def _text(
    x: float,
    y: float,
    value: object,
    *,
    size: int = 16,
    fill: str = "#dce8f7",
    weight: int = 400,
    anchor: str = "start",
    family: str = "Inter, system-ui, sans-serif",
) -> str:
    return (
        f'  <text x="{x:.2f}" y="{y:.2f}" font-family="{family}" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}">{html.escape(str(value))}</text>'
    )


def _terminal_svg(transcript: str) -> bytes:
    lines = transcript.rstrip("\n").splitlines()
    body = [
        '  <rect x="30" y="30" width="1060" height="360" rx="12" fill="#101a2a" stroke="#31415c"/>',
        '  <circle cx="55" cy="53" r="6" fill="#ff6b6b"/>',
        '  <circle cx="76" cy="53" r="6" fill="#ffd166"/>',
        '  <circle cx="97" cy="53" r="6" fill="#5bd68b"/>',
        _text(
            550,
            58,
            "verified synthetic-event CLI",
            size=14,
            fill="#9fb3cc",
            anchor="middle",
        ),
    ]
    for index, line in enumerate(lines):
        fill = "#8ce99a" if index == 0 else "#d9e6f5"
        body.append(
            _text(
                52,
                91 + index * 25,
                line,
                size=14,
                fill=fill,
                family="ui-monospace, SFMono-Regular, Menlo, monospace",
            )
        )
    body.append(
        _text(
            550,
            423,
            "Exact stdout · normalized command/exit framing · no recognition engine",
            size=15,
            fill="#86b7ff",
            weight=600,
            anchor="middle",
        )
    )
    return _svg_document(
        slug="terminal",
        title="Synthetic-event CLI transcript",
        description=(
            "A path-normalized command rendering whose stdout exactly matches "
            "the canonical five-event result summary. The command and exit "
            "marker are renderer-added; no image recognition is performed."
        ),
        width=1120,
        height=455,
        body="\n".join(body),
    )


def _event_flow_svg(result: Mapping[str, Any]) -> bytes:
    events = result["events"]
    body = [
        _text(48, 48, "Runtime-derived event flow", size=24, weight=700),
        _text(
            48,
            76,
            "5 validated events → cross-camera registry → 2 aggregate tokens",
            size=15,
            fill="#9fb3cc",
        ),
        '  <line x1="125" y1="185" x2="995" y2="185" stroke="#4b6382" stroke-width="4"/>',
    ]
    colors = {"SYNTH-01": "#58d6a8", "SYNTH-02": "#7aa7ff"}
    for index, event in enumerate(events):
        x = 125 + index * 217.5
        token = str(event["token"])
        camera = str(event["camera"])
        time_label = str(event["time"])[11:23]
        count = int(event["aggregate_count"])
        color = colors[token]
        body.extend(
            [
                f'  <circle cx="{x:.2f}" cy="185" r="18" fill="{color}" stroke="#08111f" stroke-width="5"/>',
                f'  <rect x="{x - 91:.2f}" y="112" width="182" height="52" rx="9" fill="#14243a" stroke="{color}"/>',
                _text(
                    x, 134, camera, size=13, fill="#dce8f7", weight=600, anchor="middle"
                ),
                _text(x, 153, time_label, size=12, fill="#9fb3cc", anchor="middle"),
                f'  <rect x="{x - 82:.2f}" y="218" width="164" height="82" rx="10" fill="#101a2a" stroke="#31415c"/>',
                _text(x, 244, token, size=16, fill=color, weight=700, anchor="middle"),
                _text(
                    x,
                    269,
                    f"aggregate count {count}",
                    size=13,
                    fill="#dce8f7",
                    anchor="middle",
                ),
                _text(
                    x,
                    289,
                    "new token" if event["first_for_token"] else "deduplicated",
                    size=12,
                    fill="#9fb3cc",
                    anchor="middle",
                ),
            ]
        )
    token_items = result["aggregation"]["tokens"]
    body.extend(
        [
            '  <rect x="250" y="342" width="620" height="92" rx="14" fill="#10253a" stroke="#3a5878"/>',
            _text(
                560,
                372,
                "Final cross-camera aggregation",
                size=17,
                weight=700,
                anchor="middle",
            ),
        ]
    )
    for index, token in enumerate(token_items):
        x = 405 + index * 310
        cameras = " + ".join(sorted(token["cameras"]))
        body.append(
            _text(
                x,
                402,
                f"{token['token']}: {token['event_count']} events",
                size=15,
                fill=colors[str(token["token"])],
                weight=700,
                anchor="middle",
            )
        )
        body.append(_text(x, 422, cameras, size=12, fill="#9fb3cc", anchor="middle"))
    body.append(
        _text(
            560,
            473,
            "Derived from synthetic-result.json · orchestration evidence, not recognition evidence",
            size=14,
            fill="#86b7ff",
            anchor="middle",
        )
    )
    return _svg_document(
        slug="event-flow",
        title="Runtime-derived synthetic event flow",
        description=(
            "Five deterministic fake events pass through two synthetic cameras "
            "and aggregate into two tokens."
        ),
        width=1120,
        height=505,
        body="\n".join(body),
    )


def _zoom_geometry_svg(result: Mapping[str, Any]) -> bytes:
    states = result["camera_zoom_state"]
    body = [
        _text(48, 48, "Runtime-derived software zoom geometry", size=24, weight=700),
        _text(
            48,
            76,
            "Normalized frame, selected target, and final crop — geometry only; no pixels",
            size=15,
            fill="#9fb3cc",
        ),
    ]
    for panel_index, camera in enumerate(sorted(states)):
        state = states[camera]
        frame_x = 55 + panel_index * 545
        frame_y = 125
        frame_width = 470
        frame_height = 264.375
        crop = state["crop"]
        target = state["target"]
        crop_x = frame_x + float(crop["left"]) * frame_width
        crop_y = frame_y + float(crop["top"]) * frame_height
        crop_width = (float(crop["right"]) - float(crop["left"])) * frame_width
        crop_height = (float(crop["bottom"]) - float(crop["top"])) * frame_height
        target_x = frame_x + float(target["left"]) * frame_width
        target_y = frame_y + float(target["top"]) * frame_height
        target_width = (float(target["right"]) - float(target["left"])) * frame_width
        target_height = (float(target["bottom"]) - float(target["top"])) * frame_height
        body.extend(
            [
                _text(frame_x, 108, camera, size=17, weight=700),
                f'  <rect x="{frame_x}" y="{frame_y}" width="{frame_width}" height="{frame_height}" fill="#101a2a" stroke="#59708f" stroke-width="2"/>',
                f'  <rect x="{crop_x:.2f}" y="{crop_y:.2f}" width="{crop_width:.2f}" height="{crop_height:.2f}" fill="#58d6a8" fill-opacity="0.10" stroke="#58d6a8" stroke-width="3"/>',
                f'  <rect x="{target_x:.2f}" y="{target_y:.2f}" width="{target_width:.2f}" height="{target_height:.2f}" fill="#7aa7ff" fill-opacity="0.14" stroke="#7aa7ff" stroke-width="3" stroke-dasharray="8 5"/>',
                _text(
                    frame_x,
                    417,
                    f"zoom ratio {float(state['zoom_ratio']):.2f}×",
                    size=16,
                    fill="#58d6a8",
                    weight=700,
                ),
                _text(
                    frame_x + 185,
                    417,
                    "solid = crop",
                    size=13,
                    fill="#58d6a8",
                ),
                _text(
                    frame_x + 305,
                    417,
                    "dashed = target",
                    size=13,
                    fill="#7aa7ff",
                ),
            ]
        )
    body.append(
        _text(
            560,
            467,
            "Coordinates and ratios are read from the actual deterministic CLI artifact",
            size=14,
            fill="#86b7ff",
            anchor="middle",
        )
    )
    return _svg_document(
        slug="zoom-geometry",
        title="Runtime-derived zoom geometry",
        description=(
            "Two normalized frame diagrams show the final target and software "
            "crop produced for each synthetic camera. No image pixels are used."
        ),
        width=1120,
        height=500,
        body="\n".join(body),
    )


def _architecture_svg() -> bytes:
    boxes = [
        (40, 158, 190, "Validated fake events", "#58d6a8"),
        (270, 158, 190, "Production geometry", "#7aa7ff"),
        (500, 158, 190, "Per-camera zoom", "#7aa7ff"),
        (730, 158, 190, "Shared registry", "#7aa7ff"),
        (960, 158, 120, "Evidence", "#58d6a8"),
    ]
    body = [
        _text(48, 48, "Architecture boundary", size=24, weight=700),
        _text(
            48,
            78,
            "ARCHITECTURE — explanatory diagram, not runtime proof",
            size=15,
            fill="#ffd166",
            weight=700,
        ),
        '  <rect x="30" y="110" width="1070" height="142" rx="16" fill="#0d2132" stroke="#355575"/>',
    ]
    for index, (x, y, width, label, color) in enumerate(boxes):
        body.append(
            f'  <rect x="{x}" y="{y}" width="{width}" height="54" rx="10" fill="#14243a" stroke="{color}" stroke-width="2"/>'
        )
        body.append(
            _text(
                x + width / 2,
                y + 33,
                label,
                size=14,
                fill="#e7f0fb",
                weight=650,
                anchor="middle",
            )
        )
        if index < len(boxes) - 1:
            next_x = boxes[index + 1][0]
            body.append(
                f'  <line x1="{x + width + 8}" y1="185" x2="{next_x - 10}" y2="185" stroke="#9fb3cc" stroke-width="2"/>'
            )
            body.append(
                f'  <path d="M {next_x - 15} 179 L {next_x - 5} 185 L {next_x - 15} 191" fill="none" stroke="#9fb3cc" stroke-width="2"/>'
            )
    body.extend(
        [
            _text(
                45,
                294,
                "Verified lane",
                size=17,
                fill="#58d6a8",
                weight=700,
            ),
            _text(
                200,
                294,
                "strict trace → real orchestration modules → deterministic JSON/SVG",
                size=15,
                fill="#dce8f7",
            ),
            '  <line x1="45" y1="318" x2="1075" y2="318" stroke="#31415c"/>',
            _text(
                45,
                355,
                "External lane",
                size=17,
                fill="#ff8e8e",
                weight=700,
            ),
            _text(
                200,
                355,
                "camera/media → proprietary SDK → recognition results",
                size=15,
                fill="#dce8f7",
            ),
            _text(
                200,
                383,
                "Not executed, benchmarked, captured, or claimed by this evidence bundle",
                size=14,
                fill="#ffb4b4",
            ),
        ]
    )
    return _svg_document(
        slug="architecture",
        title="Synthetic evidence architecture boundary",
        description=(
            "An explanatory architecture diagram separating the verified "
            "synthetic orchestration lane from the unverified external camera "
            "and proprietary recognition lane."
        ),
        width=1120,
        height=430,
        body="\n".join(body),
    )


def _setup_workflow_svg() -> bytes:
    steps = [
        ("1", "Python 3.12", "standard library only"),
        ("2", "Canonical fixture", "5 fake events"),
        ("3", "Run twice", "private mode 0700"),
        ("4", "Validate", "privacy + SVG + hashes"),
        ("5", "Check", "byte-current bundle"),
    ]
    body = [
        _text(48, 48, "Evidence reproduction workflow", size=24, weight=700),
        _text(
            48,
            78,
            "WORKFLOW — explanatory setup guide, not runtime proof",
            size=15,
            fill="#ffd166",
            weight=700,
        ),
    ]
    for index, (number, title, subtitle) in enumerate(steps):
        x = 42 + index * 216
        body.extend(
            [
                f'  <rect x="{x}" y="130" width="180" height="126" rx="14" fill="#12243a" stroke="#4a6688" stroke-width="2"/>',
                f'  <circle cx="{x + 28}" cy="158" r="17" fill="#58d6a8"/>',
                _text(
                    x + 28,
                    164,
                    number,
                    size=15,
                    fill="#07131f",
                    weight=800,
                    anchor="middle",
                ),
                _text(x + 16, 202, title, size=16, weight=700),
                _text(x + 16, 229, subtitle, size=13, fill="#9fb3cc"),
            ]
        )
        if index < len(steps) - 1:
            body.extend(
                [
                    f'  <line x1="{x + 184}" y1="193" x2="{x + 207}" y2="193" stroke="#86b7ff" stroke-width="3"/>',
                    f'  <path d="M {x + 200} 186 L {x + 210} 193 L {x + 200} 200" fill="none" stroke="#86b7ff" stroke-width="3"/>',
                ]
            )
    body.extend(
        [
            '  <rect x="120" y="307" width="880" height="68" rx="12" fill="#0d2132" stroke="#355575"/>',
            _text(
                560,
                335,
                "python3.12 -S tools/render_readme_visuals.py --check",
                size=17,
                fill="#8ce99a",
                weight=650,
                anchor="middle",
                family="ui-monospace, SFMono-Regular, Menlo, monospace",
            ),
            _text(
                560,
                359,
                "No SDK, camera, model, network access, or third-party Python package",
                size=14,
                fill="#dce8f7",
                anchor="middle",
            ),
        ]
    )
    return _svg_document(
        slug="setup",
        title="Evidence reproduction workflow",
        description=(
            "An explanatory five-step workflow for reproducing and checking "
            "the synthetic event evidence with Python 3.12."
        ),
        width=1120,
        height=420,
        body="\n".join(body),
    )


def _terminal_transcript(stdout: bytes) -> bytes:
    try:
        output = stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise EvidenceError("synthetic CLI output is not ASCII") from error
    command = " ".join(COMMAND_DISPLAY)
    return (f"$ {command}\n{output}# exit=0\n").encode("ascii")


def _validate_svg(name: str, payload: bytes) -> None:
    if len(payload) > MAX_SVG_BYTES:
        raise EvidenceError(f"{name} exceeds the SVG byte limit")
    try:
        text = payload.decode("utf-8")
        declaration = '<?xml version="1.0" encoding="UTF-8"?>\n'
        if not text.startswith(declaration) or "<?" in text[len(declaration) :]:
            raise EvidenceError(
                f"{name} contains a forbidden XML processing instruction"
            )
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise EvidenceError(f"{name} contains a forbidden XML declaration")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as error:
        raise EvidenceError(f"{name} is not valid UTF-8 SVG") from error
    namespace = "{http://www.w3.org/2000/svg}"
    if root.tag != f"{namespace}svg":
        raise EvidenceError(f"{name} does not have an SVG root")
    if root.get("role") != "img":
        raise EvidenceError(f"{name} is missing role=img")
    labelled = root.get("aria-labelledby", "").split()
    if len(labelled) != 2:
        raise EvidenceError(f"{name} is missing accessible labels")
    ids = {element.get("id") for element in root.iter() if element.get("id")}
    if not set(labelled).issubset(ids):
        raise EvidenceError(f"{name} accessible labels are unresolved")
    titles = root.findall(f"{namespace}title")
    descriptions = root.findall(f"{namespace}desc")
    if len(titles) != 1 or len(descriptions) != 1:
        raise EvidenceError(f"{name} must have one title and description")
    if not (titles[0].text or "").strip() or not (descriptions[0].text or "").strip():
        raise EvidenceError(f"{name} has empty accessible labels")
    view_box = root.get("viewBox", "").split()
    try:
        values = [float(item) for item in view_box]
    except ValueError as error:
        raise EvidenceError(f"{name} has an invalid viewBox") from error
    if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
        raise EvidenceError(f"{name} has an invalid viewBox")
    allowed_attributes = {
        f"{namespace}svg": {
            "width",
            "height",
            "viewBox",
            "role",
            "aria-labelledby",
        },
        f"{namespace}title": {"id"},
        f"{namespace}desc": {"id"},
        f"{namespace}rect": {
            "x",
            "y",
            "width",
            "height",
            "rx",
            "fill",
            "fill-opacity",
            "stroke",
            "stroke-width",
            "stroke-dasharray",
        },
        f"{namespace}circle": {
            "cx",
            "cy",
            "r",
            "fill",
            "stroke",
            "stroke-width",
        },
        f"{namespace}line": {
            "x1",
            "y1",
            "x2",
            "y2",
            "stroke",
            "stroke-width",
            "stroke-dasharray",
        },
        f"{namespace}path": {
            "d",
            "fill",
            "stroke",
            "stroke-width",
            "stroke-dasharray",
        },
        f"{namespace}text": {
            "x",
            "y",
            "font-family",
            "font-size",
            "font-weight",
            "fill",
            "text-anchor",
        },
    }
    for element in root.iter():
        if element.tag not in allowed_attributes:
            raise EvidenceError(f"{name} contains unsafe embedded content")
        for attribute, value in element.attrib.items():
            if "}" in attribute or attribute not in allowed_attributes[element.tag]:
                raise EvidenceError(f"{name} contains an unsafe SVG attribute")
            lowered = attribute.lower()
            if (
                lowered.startswith("on")
                or lowered.endswith("href")
                or "url(" in value.lower()
                or "@import" in value.lower()
            ):
                raise EvidenceError(f"{name} contains an external reference")


def _privacy_scan(name: str, payload: bytes) -> None:
    lowered = payload.lower()
    for marker in _FORBIDDEN_PUBLISHED_BYTES:
        if marker.lower() in lowered:
            raise EvidenceError(f"{name} contains forbidden published metadata")
    if _EMAIL.search(payload):
        raise EvidenceError(f"{name} contains an email address")
    for pattern in _SECRET_MARKERS:
        if pattern.search(payload):
            raise EvidenceError(f"{name} contains a secret-like value")
    environment_values = {
        os.environ.get("USER", ""),
        os.environ.get("LOGNAME", ""),
        os.uname().nodename,
    }
    sensitive_environment_name = re.compile(
        r"(?i)(?:user|login|host|token|secret|password|credential|api[_-]?key|"
        r"access[_-]?key)"
    )
    environment_values.update(
        value
        for key, value in os.environ.items()
        if sensitive_environment_name.search(key)
    )
    for environment_value in environment_values:
        encoded = environment_value.encode("utf-8", "ignore")
        if len(encoded) >= 6 and encoded.lower() in lowered:
            raise EvidenceError(f"{name} contains host identity metadata")


def _build_nonmanifest_outputs(run: DemoRun) -> dict[str, bytes]:
    transcript = _terminal_transcript(run.stdout)
    transcript_text = transcript.decode("ascii")
    outputs = {
        RUNTIME_RESULT: run.artifact,
        TERMINAL_TRANSCRIPT: transcript,
        TERMINAL_SVG: _terminal_svg(transcript_text),
        EVENT_FLOW_SVG: _event_flow_svg(run.result),
        ZOOM_GEOMETRY_SVG: _zoom_geometry_svg(run.result),
        ARCHITECTURE_SVG: _architecture_svg(),
        SETUP_SVG: _setup_workflow_svg(),
    }
    if set(outputs) != set(OUTPUT_KINDS):
        raise EvidenceError("renderer output allowlist is inconsistent")
    for name, payload in outputs.items():
        _privacy_scan(name, payload)
        if name in SVG_NAMES:
            _validate_svg(name, payload)
    return outputs


def _build_manifest(
    inputs: tuple[dict[str, object], ...],
    outputs: Mapping[str, bytes],
) -> bytes:
    output_records = []
    for name in sorted(outputs):
        payload = outputs[name]
        output_records.append(
            {
                "bytes": len(payload),
                "evidence_kind": OUTPUT_KINDS[name],
                "path": (GENERATED_RELATIVE / name).as_posix(),
                "sha256": _sha256(payload),
            }
        )
    manifest = {
        "boundary": {
            "camera_or_media_used": False,
            "external_sdk_used": False,
            "executes": [
                "one canonical validated synthetic event trace",
                "production cross-camera aggregation for that trace",
                "production plate-to-target geometry for that trace",
                "production per-camera software zoom geometry for that trace",
                "deterministic evidence rendering for that trace",
            ],
            "recognition_accuracy": "not_evaluated",
            "recognition_performed": False,
            "throughput": "not_measured",
        },
        "determinism": {
            "byte_identical_cli_runs": 2,
            "fixed_command": list(COMMAND_DISPLAY),
            "python_contract": "3.12",
            "source_hashes_stable_before_and_after": True,
            "stdout_matches_canonical_summary": True,
            "transcript_framing": (
                "renderer-added path-normalized command and exit marker"
            ),
        },
        "generated_tree": sorted(EXPECTED_GENERATED_NAMES),
        "inputs": list(inputs),
        "manifest": {
            "path": (GENERATED_RELATIVE / MANIFEST_NAME).as_posix(),
            "self_hash": "excluded-by-design",
            "written_last": True,
        },
        "outputs": output_records,
        "schema_version": 1,
    }
    payload = _canonical_json(manifest)
    _privacy_scan(MANIFEST_NAME, payload)
    return payload


def _inventory_generated_tree(
    directory: Path,
    *,
    expected_names: Iterable[str] = EXPECTED_GENERATED_NAMES,
    allow_missing: bool,
) -> dict[str, os.stat_result]:
    expected = set(expected_names)
    try:
        directory_descriptors, directory_links = _open_directory_chain(directory)
    except FileNotFoundError:
        if allow_missing:
            return {}
        raise EvidenceError("generated evidence directory is missing")
    directory_descriptor = directory_descriptors[-1]
    try:
        names_before = sorted(os.listdir(directory_descriptor))
    except OSError as error:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)
        raise EvidenceError("generated evidence directory cannot be read") from error
    try:
        names = set(names_before)
        unexpected = names - expected
        missing = expected - names
        if unexpected:
            raise EvidenceError("generated evidence tree contains unexpected entries")
        if missing and not allow_missing:
            raise EvidenceError("generated evidence tree is incomplete")
        inventory: dict[str, os.stat_result] = {}
        for name in names_before:
            if (
                not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in name
                )
            ):
                raise EvidenceError("generated evidence tree contains an unsafe name")
            entry_status = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(entry_status.st_mode):
                raise EvidenceError("generated evidence entries must be regular files")
            inventory[name] = entry_status
        if sorted(os.listdir(directory_descriptor)) != names_before:
            raise EvidenceError("generated evidence tree changed during inventory")
        _assert_directory_links(directory_links)
        return inventory
    except OSError as error:
        raise EvidenceError(
            "generated evidence directory cannot be inspected safely"
        ) from error
    finally:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _ensure_generated_directory() -> None:
    root_descriptors, root_links = _open_directory_chain(REPOSITORY)
    created_descriptors = [os.dup(root_descriptors[-1])]
    created_links: list[tuple[int, str, int, _FileSnapshot]] = []
    try:
        for component in GENERATED_RELATIVE.parts:
            parent = created_descriptors[-1]
            try:
                status = os.stat(
                    component,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=parent)
                status = os.stat(
                    component,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            snapshot = _FileSnapshot.capture(status)
            if not stat.S_ISDIR(status.st_mode):
                raise EvidenceError("generated evidence path must be a real directory")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            if not snapshot.unchanged(os.fstat(child)):
                os.close(child)
                raise EvidenceError(
                    "generated evidence path changed while being opened"
                )
            created_descriptors.append(child)
            created_links.append((parent, component, child, snapshot))
        os.fchmod(created_descriptors[-1], 0o755)
        link_parent, link_name, link_child, _link_snapshot = created_links[-1]
        created_links[-1] = (
            link_parent,
            link_name,
            link_child,
            _FileSnapshot.capture(os.fstat(link_child)),
        )
        _assert_directory_links(created_links)
        _assert_directory_links(root_links)
    except OSError as error:
        raise EvidenceError(
            "generated evidence directory cannot be prepared"
        ) from error
    finally:
        for descriptor in reversed(created_descriptors):
            os.close(descriptor)
        for descriptor in reversed(root_descriptors):
            os.close(descriptor)


def _write_stage(
    stage: _PinnedTemporaryDirectory,
    outputs: Mapping[str, bytes],
) -> None:
    stage.assert_visible()
    for name in sorted(outputs):
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o644,
            dir_fd=stage.fd,
        )
        try:
            view = memoryview(outputs[name])
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise EvidenceError("staged evidence write made no progress")
                written += count
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.fsync(stage.fd)
    stage.assert_visible()
    _inventory_generated_tree(
        stage.path,
        expected_names=outputs,
        allow_missing=False,
    )
    stage.assert_visible()


def _publish(outputs: Mapping[str, bytes]) -> None:
    if set(outputs) != set(EXPECTED_GENERATED_NAMES):
        raise EvidenceError("publication output inventory is incomplete")
    _ensure_generated_directory()
    _inventory_generated_tree(GENERATED_DIRECTORY, allow_missing=True)
    with (
        _prepare_private_temp_root() as temporary_root,
        temporary_root.make_directory("readme-stage-") as stage,
    ):
        _write_stage(stage, outputs)
        destination_descriptors, destination_links = _open_directory_chain(
            GENERATED_DIRECTORY
        )
        destination_descriptor = destination_descriptors[-1]
        try:
            for name in sorted(set(outputs) - {MANIFEST_NAME}):
                os.replace(
                    name,
                    name,
                    src_dir_fd=stage.fd,
                    dst_dir_fd=destination_descriptor,
                )
            os.fsync(destination_descriptor)
            os.replace(
                MANIFEST_NAME,
                MANIFEST_NAME,
                src_dir_fd=stage.fd,
                dst_dir_fd=destination_descriptor,
            )
            os.fsync(destination_descriptor)
            stage.assert_visible()
            _assert_directory_links(destination_links)
        finally:
            for descriptor in reversed(destination_descriptors):
                os.close(descriptor)
    inventory = _inventory_generated_tree(
        GENERATED_DIRECTORY,
        allow_missing=False,
    )
    for name, status in inventory.items():
        if stat.S_IMODE(status.st_mode) != 0o644:
            raise EvidenceError(f"{name} does not have mode 0644")
    _check(outputs)


def _check(outputs: Mapping[str, bytes]) -> None:
    inventory = _inventory_generated_tree(
        GENERATED_DIRECTORY,
        allow_missing=False,
    )
    if set(inventory) != set(outputs):
        raise EvidenceError("generated evidence inventory does not match renderer")
    for name in sorted(outputs):
        if stat.S_IMODE(inventory[name].st_mode) != 0o644:
            raise EvidenceError(f"{name} does not have mode 0644")
        actual = _require_relative_file(
            GENERATED_DIRECTORY,
            Path(name),
            maximum_bytes=max(
                MAX_RESULT_BYTES,
                MAX_SVG_BYTES,
                MAX_SOURCE_BYTES,
            ),
        )
        if actual != outputs[name]:
            raise EvidenceError(f"{name} is stale; run the renderer with --write")
    final_inventory = _inventory_generated_tree(
        GENERATED_DIRECTORY,
        allow_missing=False,
    )
    if {name: _FileSnapshot.capture(status) for name, status in inventory.items()} != {
        name: _FileSnapshot.capture(status) for name, status in final_inventory.items()
    }:
        raise EvidenceError("generated evidence changed during verification")


def _verify_fixture_contract() -> None:
    from alpr_runner.synthetic import canonical_trace_bytes, default_trace

    fixture = _require_relative_file(
        REPOSITORY,
        FIXTURE_RELATIVE,
        maximum_bytes=MAX_SOURCE_BYTES,
    )
    if fixture != canonical_trace_bytes(default_trace()):
        raise EvidenceError(
            "canonical fixture bytes differ from synthetic.default_trace"
        )


def render(*, write: bool) -> None:
    if sys.version_info[:2] != (3, 12):
        raise EvidenceError("evidence renderer requires Python 3.12")
    _verify_fixture_contract()
    before = _snapshot_inputs()
    run = _run_deterministic_pair()
    nonmanifest = _build_nonmanifest_outputs(run)
    after = _snapshot_inputs()
    if before != after:
        raise EvidenceError("evidence source inputs changed during rendering")
    manifest = _build_manifest(before, nonmanifest)
    outputs = {**nonmanifest, MANIFEST_NAME: manifest}
    for name, payload in outputs.items():
        _privacy_scan(name, payload)
        if name in SVG_NAMES:
            _validate_svg(name, payload)
    if write:
        _publish(outputs)
    else:
        _check(outputs)
    if _snapshot_inputs() != before:
        raise EvidenceError(
            "evidence source inputs changed before verification completed"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild or verify deterministic vendor-independent README evidence."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the allowlisted evidence bundle.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Reproduce evidence and require every committed byte to match.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        render(write=args.write)
    except EvidenceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    action = "wrote" if args.write else "verified"
    print(f"{action} {len(EXPECTED_GENERATED_NAMES)} deterministic evidence files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
