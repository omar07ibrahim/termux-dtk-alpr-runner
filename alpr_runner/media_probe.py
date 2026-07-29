from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from alpr_runner.ffmpeg_io import (
    FfmpegFrameSource,
    FfmpegSupervisorError,
    FrameSpec,
    InheritedFdArgument,
)
from alpr_runner.synthetic_media import (
    FRAME_COUNT,
    FRAMES_PER_SECOND,
    HEIGHT,
    WIDTH,
    canonical_recipe_bytes,
    decode_recipe,
    render_y4m,
)

REPOSITORY = Path(__file__).resolve().parents[1]
RECIPE_RELATIVE = Path("examples/synthetic-media-v1.json")
FFMPEG_RELATIVE = Path(
    ".t/media-evidence-runtime/imageio_ffmpeg/binaries/"
    "ffmpeg-linux-x86_64-v7.0.2"
)

FFMPEG_VERSION = "7.0.2-static"
FFMPEG_BYTES = 79_826_272
FFMPEG_SHA256 = "e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99"
RECIPE_BYTES = 1_288
RECIPE_SHA256 = "ab8d7a7518d3d952d725ee96f0de9b6ecfdef7102f4b78ddcb125cab062466f6"
Y4M_BYTES = 414_869
Y4M_SHA256 = "7614365d1342f1786ab82bb0d3fe07f1d5215cb6159bba40674234346ca7cfeb"
RGB_BYTES = 829_440
RGB_SHA256 = "51ccea55540f6c8e8e67ecd8ea11ba5a8f16f75f2b8cff438146090b171cc21a"
RGB_UNIQUE_COLORS = 34

STARTUP_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 5.0
STDERR_LIMIT_BYTES = 4_096
TERMINATE_TIMEOUT_SECONDS = 2.0
KILL_TIMEOUT_SECONDS = 2.0

NORMALIZED_COMMAND = (
    "PINNED-FFMPEG-7.0.2",
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "error",
    "-cpuflags",
    "0",
    "-cpucount",
    "1",
    "-threads",
    "1",
    "-filter_threads",
    "1",
    "-fflags",
    "+bitexact",
    "-f",
    "yuv4mpegpipe",
    "-i",
    "PRIVATE-SYNTHETIC-Y4M",
    "-map",
    "0:v:0",
    "-an",
    "-sn",
    "-dn",
    "-fps_mode",
    "passthrough",
    "-threads",
    "1",
    "-flags:v",
    "+bitexact",
    "-pix_fmt",
    "rgb24",
    "-f",
    "rawvideo",
    "pipe:1",
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
_WORKSPACE_ROOT_NAME = "media-probe-workspaces"
_REQUIRED_SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)


class MediaProbeError(RuntimeError):
    """A public-safe failure in the closed synthetic-media evidence probe."""


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    """The two-run public receipt plus the first run's exact RGB frames."""

    frames: tuple[bytes, ...]
    receipt: dict[str, Any]

    def canonical_receipt(self) -> bytes:
        return canonical_json(self.receipt)


@dataclass(frozen=True, slots=True)
class _FileRecord:
    size: int
    sha256: str
    payload: bytes | None


@dataclass(frozen=True, slots=True)
class _Run:
    frames: tuple[bytes, ...]
    supervisor: dict[str, object]


@dataclass(slots=True)
class _SealedFile:
    fd: int
    record: _FileRecord
    mode: int
    _closed: bool = False

    def __enter__(self) -> _SealedFile:
        self.assert_intact()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def assert_intact(self) -> None:
        if self._closed:
            raise MediaProbeError("sealed media descriptor is closed")
        try:
            status = os.fstat(self.fd)
            seals = fcntl.fcntl(self.fd, fcntl.F_GET_SEALS)
        except OSError:
            raise MediaProbeError("sealed media descriptor is unavailable") from None
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != self.mode
            or status.st_size != self.record.size
            or seals & _REQUIRED_SEALS != _REQUIRED_SEALS
        ):
            raise MediaProbeError("sealed media descriptor identity is invalid")

    def close(self) -> None:
        if self._closed:
            return
        os.close(self.fd)
        self._closed = True


class _SignalAwareEvent(threading.Event):
    """Cancellation whose signal path is one reentrant plain assignment."""

    def __init__(self) -> None:
        super().__init__()
        self._signal_requested = False

    def is_set(self) -> bool:
        return self._signal_requested or super().is_set()


class _ProbeSignalScope:
    """Temporarily turn main-thread SIGINT/SIGTERM into bounded cancellation."""

    def __init__(self) -> None:
        self.event = _SignalAwareEvent()
        self._previous: dict[signal.Signals, Any] = {}

    def __enter__(self) -> _SignalAwareEvent:
        if threading.current_thread() is not threading.main_thread():
            raise MediaProbeError("media evidence must run on the main thread")
        try:
            for selected in (signal.SIGINT, signal.SIGTERM):
                self._previous[selected] = signal.getsignal(selected)
                signal.signal(selected, self._request_stop)
        except (OSError, RuntimeError, ValueError):
            self._restore_with_retry()
            raise MediaProbeError(
                "media evidence signal handlers could not be installed"
            ) from None
        return self.event

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, traceback
        restore_errors = self._restore_with_retry()
        if restore_errors:
            if exc_value is None:
                if len(restore_errors) == 1:
                    raise restore_errors[0]
                raise ExceptionGroup(
                    "media evidence signal-handler restoration failures",
                    restore_errors,
                )
            raise BaseExceptionGroup(
                "media probe and signal-handler restoration failures",
                [exc_value, *restore_errors],
            ) from None
        if exc_value is None and self.event.is_set():
            raise MediaProbeError("media evidence was interrupted safely")

    def _request_stop(self, signum: int, frame: object) -> None:
        del signum, frame
        self.event._signal_requested = True

    def _restore(self) -> None:
        failures = False
        for selected in reversed(tuple(self._previous)):
            previous = self._previous[selected]
            try:
                signal.signal(selected, previous)
            except (OSError, RuntimeError, ValueError):
                failures = True
            else:
                del self._previous[selected]
        if failures:
            raise MediaProbeError(
                "media evidence signal handlers could not be restored"
            )

    def _restore_with_retry(self) -> list[MediaProbeError]:
        errors: list[MediaProbeError] = []
        for _attempt in range(2):
            if not self._previous:
                break
            try:
                self._restore()
            except MediaProbeError as error:
                errors.append(error)
        return errors


@dataclass(slots=True)
class _Workspace:
    parent_fd: int
    name: str
    path: Path
    fd: int
    identity: tuple[int, int, int]

    def assert_visible(self) -> None:
        opened = os.fstat(self.fd)
        visible = os.stat(
            self.name,
            dir_fd=self.parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(opened) != self.identity
            or _identity(visible) != self.identity
            or not stat.S_ISDIR(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise MediaProbeError("private media workspace changed while in use")


class _PrivateWorkspacePair:
    """Two 0700 workspaces beneath a dedicated pinned private directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._repository_fd: int | None = None
        self._temporary_parent_fd: int | None = None
        self._temporary_parent_identity: tuple[int, int, int] | None = None
        self._temporary_fd: int | None = None
        self._temporary_identity: tuple[int, int, int] | None = None
        self._workspaces: list[_Workspace] = []

    def __enter__(self) -> tuple[_Workspace, _Workspace]:
        try:
            self._repository_fd = os.open(self.root, _DIRECTORY_FLAGS)
            try:
                status = os.stat(
                    ".t",
                    dir_fd=self._repository_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(".t", 0o700, dir_fd=self._repository_fd)
                status = os.stat(
                    ".t",
                    dir_fd=self._repository_fd,
                    follow_symlinks=False,
                )
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_mode & 0o022
            ):
                raise MediaProbeError("private media root is not a real directory")
            self._temporary_parent_fd = os.open(
                ".t",
                _DIRECTORY_FLAGS,
                dir_fd=self._repository_fd,
            )
            opened_parent = os.fstat(self._temporary_parent_fd)
            visible = os.stat(
                ".t",
                dir_fd=self._repository_fd,
                follow_symlinks=False,
            )
            if (
                _identity(opened_parent) != _identity(visible)
                or opened_parent.st_uid != os.geteuid()
                or opened_parent.st_mode & 0o022
            ):
                raise MediaProbeError("private media root changed while opening")
            self._temporary_parent_identity = _identity(opened_parent)

            created = False
            try:
                private_status = os.stat(
                    _WORKSPACE_ROOT_NAME,
                    dir_fd=self._temporary_parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(
                    _WORKSPACE_ROOT_NAME,
                    0o700,
                    dir_fd=self._temporary_parent_fd,
                )
                created = True
                private_status = os.stat(
                    _WORKSPACE_ROOT_NAME,
                    dir_fd=self._temporary_parent_fd,
                    follow_symlinks=False,
                )
            if not stat.S_ISDIR(private_status.st_mode):
                raise MediaProbeError(
                    "dedicated media workspace root is not a real directory"
                )
            self._temporary_fd = os.open(
                _WORKSPACE_ROOT_NAME,
                _DIRECTORY_FLAGS,
                dir_fd=self._temporary_parent_fd,
            )
            if created:
                os.fchmod(self._temporary_fd, 0o700)
            opened = os.fstat(self._temporary_fd)
            visible_private = os.stat(
                _WORKSPACE_ROOT_NAME,
                dir_fd=self._temporary_parent_fd,
                follow_symlinks=False,
            )
            if (
                _identity(opened) != _identity(visible_private)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise MediaProbeError(
                    "dedicated media workspace root changed while opening"
                )
            self._temporary_identity = _identity(opened)
            first = self._make_workspace()
            second = self._make_workspace()
            return first, second
        except MediaProbeError:
            self._cleanup()
            raise
        except OSError:
            self._cleanup()
            raise MediaProbeError(
                "private media workspaces could not be prepared"
            ) from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, traceback
        try:
            self._cleanup()
        except MediaProbeError as cleanup_error:
            if exc_value is None:
                raise
            raise BaseExceptionGroup(
                "media probe and private-workspace cleanup failures",
                [exc_value, cleanup_error],
            ) from None

    def _assert_root(self) -> None:
        if (
            self._repository_fd is None
            or self._temporary_parent_fd is None
            or self._temporary_parent_identity is None
            or self._temporary_fd is None
            or self._temporary_identity is None
        ):
            raise MediaProbeError("private media root is closed")
        opened_parent = os.fstat(self._temporary_parent_fd)
        visible_parent = os.stat(
            ".t",
            dir_fd=self._repository_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(self._temporary_fd)
        visible = os.stat(
            _WORKSPACE_ROOT_NAME,
            dir_fd=self._temporary_parent_fd,
            follow_symlinks=False,
        )
        if (
            _identity(opened_parent) != self._temporary_parent_identity
            or _identity(visible_parent) != self._temporary_parent_identity
            or _identity(opened) != self._temporary_identity
            or _identity(visible) != self._temporary_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise MediaProbeError("private media root changed while in use")

    def _make_workspace(self) -> _Workspace:
        self._assert_root()
        assert self._temporary_fd is not None
        for _attempt in range(128):
            name = f"media-probe-{secrets.token_hex(8)}"
            try:
                os.mkdir(name, 0o700, dir_fd=self._temporary_fd)
            except FileExistsError:
                continue
            child: int | None = None
            try:
                child = os.open(
                    name,
                    _DIRECTORY_FLAGS,
                    dir_fd=self._temporary_fd,
                )
                os.fchmod(child, 0o700)
                opened = os.fstat(child)
                visible = os.stat(
                    name,
                    dir_fd=self._temporary_fd,
                    follow_symlinks=False,
                )
                identity = _identity(opened)
                if (
                    identity != _identity(visible)
                    or not stat.S_ISDIR(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o700
                ):
                    raise MediaProbeError(
                        "private media workspace changed while opening"
                    )
                workspace = _Workspace(
                    parent_fd=self._temporary_fd,
                    name=name,
                    path=(
                        self.root
                        / ".t"
                        / _WORKSPACE_ROOT_NAME
                        / name
                    ),
                    fd=child,
                    identity=identity,
                )
                workspace.assert_visible()
                self._workspaces.append(workspace)
                return workspace
            except BaseException:
                if child is not None:
                    os.close(child)
                try:
                    shutil.rmtree(name, dir_fd=self._temporary_fd)
                except OSError:
                    pass
                raise
        raise MediaProbeError("private media workspace allocation was exhausted")

    def _cleanup(self) -> None:
        cleanup_failed = False
        temporary_fd = self._temporary_fd
        for workspace in reversed(self._workspaces):
            try:
                workspace.assert_visible()
            except (MediaProbeError, OSError):
                cleanup_failed = True
                try:
                    os.close(workspace.fd)
                except OSError:
                    pass
                continue
            try:
                os.close(workspace.fd)
                shutil.rmtree(workspace.name, dir_fd=workspace.parent_fd)
                try:
                    os.stat(
                        workspace.name,
                        dir_fd=workspace.parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    cleanup_failed = True
            except (MediaProbeError, OSError):
                cleanup_failed = True
        self._workspaces.clear()
        if temporary_fd is not None:
            try:
                self._assert_root()
            except (MediaProbeError, OSError):
                cleanup_failed = True
            os.close(temporary_fd)
            self._temporary_fd = None
            self._temporary_identity = None
        if self._temporary_parent_fd is not None:
            os.close(self._temporary_parent_fd)
            self._temporary_parent_fd = None
            self._temporary_parent_identity = None
        if self._repository_fd is not None:
            os.close(self._repository_fd)
            self._repository_fd = None
        if cleanup_failed:
            raise MediaProbeError("private media workspace cleanup failed")


def canonical_json(value: object) -> bytes:
    """Serialize one timestamp-free, ASCII JSON receipt."""

    return (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def run_reproducible_probe(root: Path = REPOSITORY) -> MediaProbeResult:
    """Run the closed FFmpeg profile twice and require byte-identical RGB."""

    if sys.version_info[:2] != (3, 12):
        raise MediaProbeError("media evidence requires CPython 3.12")
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise MediaProbeError("media evidence requires Linux x86_64")
    selected_root = Path(os.path.abspath(os.fspath(root)))
    with _ProbeSignalScope() as stop_event:
        return _run_reproducible_probe(selected_root, stop_event)


def _run_reproducible_probe(
    selected_root: Path,
    stop_event: _SignalAwareEvent,
) -> MediaProbeResult:
    recipe_record = _read_pinned_regular_file(
        selected_root,
        RECIPE_RELATIVE,
        maximum_bytes=RECIPE_BYTES,
        capture=True,
        executable=False,
        missing_message="canonical media recipe is unavailable",
    )
    if (
        recipe_record.size != RECIPE_BYTES
        or recipe_record.sha256 != RECIPE_SHA256
        or recipe_record.payload is None
    ):
        raise MediaProbeError("canonical media recipe identity is invalid")
    recipe = decode_recipe(recipe_record.payload)
    if canonical_recipe_bytes(recipe) != recipe_record.payload:
        raise MediaProbeError("canonical media recipe bytes are invalid")

    _raise_if_stopped(stop_event)
    first_y4m = render_y4m(recipe)
    second_y4m = render_y4m(recipe)
    if (
        first_y4m != second_y4m
        or len(first_y4m) != Y4M_BYTES
        or _sha256(first_y4m) != Y4M_SHA256
    ):
        raise MediaProbeError("synthetic Y4M identity is invalid")

    _raise_if_stopped(stop_event)
    with _prepare_sealed_ffmpeg(selected_root) as ffmpeg:
        ffmpeg_before = ffmpeg.record
        with _PrivateWorkspacePair(selected_root) as (first, second):
            _write_private_y4m(first, first_y4m)
            _write_private_y4m(second, second_y4m)
            with _sealed_payload(
                first_y4m,
                name="synthetic-y4m-run-1",
                mode=0o400,
            ) as first_input:
                first_run = _run_once(
                    ffmpeg,
                    first_input,
                    stop_event,
                )
            first.assert_visible()
            _raise_if_stopped(stop_event)
            with _sealed_payload(
                second_y4m,
                name="synthetic-y4m-run-2",
                mode=0o400,
            ) as second_input:
                second_run = _run_once(
                    ffmpeg,
                    second_input,
                    stop_event,
                )
            second.assert_visible()
        ffmpeg.assert_intact()

    recipe_after = _read_pinned_regular_file(
        selected_root,
        RECIPE_RELATIVE,
        maximum_bytes=RECIPE_BYTES,
        capture=False,
        executable=False,
        missing_message="canonical media recipe is unavailable",
    )
    ffmpeg_after = _inspect_ffmpeg(selected_root)
    if recipe_after != _FileRecord(
        size=recipe_record.size,
        sha256=recipe_record.sha256,
        payload=None,
    ):
        raise MediaProbeError("canonical media recipe changed during the probe")
    if ffmpeg_after != ffmpeg_before:
        raise MediaProbeError("pinned FFmpeg executable changed during the probe")
    if first_run.frames != second_run.frames:
        raise MediaProbeError("decoded RGB runs are not byte-identical")
    if first_run.supervisor != second_run.supervisor:
        raise MediaProbeError("supervisor receipts are not byte-identical")
    _raise_if_stopped(stop_event)

    raw_rgb = b"".join(first_run.frames)
    frame_hashes = tuple(_sha256(frame) for frame in first_run.frames)
    if len(raw_rgb) != RGB_BYTES or _sha256(raw_rgb) != RGB_SHA256:
        raise MediaProbeError("decoded RGB identity is invalid")
    unique_colors = len(
        {
            raw_rgb[offset : offset + 3]
            for offset in range(0, len(raw_rgb), 3)
        }
    )
    if unique_colors != RGB_UNIQUE_COLORS:
        raise MediaProbeError("decoded RGB palette identity is invalid")

    receipt: dict[str, Any] = {
        "boundary": {
            "camera_used": False,
            "external_sdk_used": False,
            "performance_measured": False,
            "recognition_accuracy": "not_evaluated",
            "recognition_performed": False,
            "synthetic_media_used": True,
        },
        "decoded_rgb": {
            "bytes": len(raw_rgb),
            "frame_count": len(first_run.frames),
            "frame_sha256": list(frame_hashes),
            "sha256": _sha256(raw_rgb),
            "unique_colors": unique_colors,
        },
        "determinism": {
            "byte_identical_rgb_runs": 2,
            "byte_identical_supervisor_receipts": 2,
            "byte_identical_y4m_renders": 2,
            "host_dispatch_disabled": True,
            "probe_runs": 2,
            "timestamp_fields": 0,
            "verified_bytes_reopened_by_path": False,
        },
        "runtime": {
            "command": list(NORMALIZED_COMMAND),
            "ffmpeg": {
                "bytes": ffmpeg_before.size,
                "execution_binding": "write-sealed-memfd",
                "redistributed": False,
                "sha256": ffmpeg_before.sha256,
                "version": FFMPEG_VERSION,
            },
            "profile": "linux-x86_64-cpuflags0-bitexact-v1",
            "python": "3.12",
        },
        "schema_version": 1,
        "source": {
            "frame": {
                "bytes_per_frame": WIDTH * HEIGHT * 3,
                "duration_seconds": FRAME_COUNT // FRAMES_PER_SECOND,
                "fps": FRAMES_PER_SECOND,
                "height": HEIGHT,
                "pixel_format": "rgb24",
                "width": WIDTH,
            },
            "recipe": {
                "bytes": recipe_record.size,
                "path": RECIPE_RELATIVE.as_posix(),
                "sha256": recipe_record.sha256,
            },
            "y4m": {
                "bytes": len(first_y4m),
                "delivery_binding": "write-sealed-inherited-fd",
                "sha256": _sha256(first_y4m),
            },
        },
        "supervisor": first_run.supervisor,
        "supervisor_limits": {
            "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
            "kill_timeout_seconds": KILL_TIMEOUT_SECONDS,
            "startup_timeout_seconds": STARTUP_TIMEOUT_SECONDS,
            "stderr_limit_bytes": STDERR_LIMIT_BYTES,
            "terminate_timeout_seconds": TERMINATE_TIMEOUT_SECONDS,
        },
    }
    canonical_json(receipt)
    return MediaProbeResult(frames=first_run.frames, receipt=receipt)


def _inspect_ffmpeg(root: Path) -> _FileRecord:
    record = _read_pinned_regular_file(
        root,
        FFMPEG_RELATIVE,
        maximum_bytes=FFMPEG_BYTES,
        capture=False,
        executable=True,
        missing_message=(
            "pinned FFmpeg runtime is unavailable; install the evidence lock"
        ),
    )
    if record.size != FFMPEG_BYTES or record.sha256 != FFMPEG_SHA256:
        raise MediaProbeError("pinned FFmpeg executable identity is invalid")
    return record


def _prepare_sealed_ffmpeg(root: Path) -> _SealedFile:
    descriptor = _create_memfd("pinned-ffmpeg-7.0.2")
    try:
        record = _read_pinned_regular_file(
            root,
            FFMPEG_RELATIVE,
            maximum_bytes=FFMPEG_BYTES,
            capture=False,
            executable=True,
            missing_message=(
                "pinned FFmpeg runtime is unavailable; install the evidence lock"
            ),
            copy_to_fd=descriptor,
        )
        if record.size != FFMPEG_BYTES or record.sha256 != FFMPEG_SHA256:
            raise MediaProbeError("pinned FFmpeg executable identity is invalid")
        return _seal_descriptor(
            descriptor,
            record=record,
            mode=0o500,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _sealed_payload(payload: bytes, *, name: str, mode: int) -> _SealedFile:
    descriptor = _create_memfd(name)
    try:
        _write_all(descriptor, payload)
        record = _FileRecord(
            size=len(payload),
            sha256=_sha256(payload),
            payload=None,
        )
        return _seal_descriptor(
            descriptor,
            record=record,
            mode=mode,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _create_memfd(name: str) -> int:
    try:
        descriptor = os.memfd_create(
            name,
            flags=os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        if descriptor < 3:
            replacement = fcntl.fcntl(
                descriptor,
                fcntl.F_DUPFD_CLOEXEC,
                3,
            )
            os.close(descriptor)
            descriptor = replacement
        return descriptor
    except (AttributeError, OSError):
        raise MediaProbeError("sealed media descriptors are unavailable") from None


def _seal_descriptor(
    descriptor: int,
    *,
    record: _FileRecord,
    mode: int,
) -> _SealedFile:
    try:
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, _REQUIRED_SEALS)
    except OSError:
        raise MediaProbeError("media descriptor could not be sealed") from None
    sealed = _SealedFile(
        fd=descriptor,
        record=record,
        mode=mode,
    )
    sealed.assert_intact()
    return sealed


def _run_once(
    ffmpeg: _SealedFile,
    input_media: _SealedFile,
    stop_event: _SignalAwareEvent,
) -> _Run:
    _raise_if_stopped(stop_event)
    ffmpeg.assert_intact()
    input_media.assert_intact()
    source = FfmpegFrameSource(
        _real_command(ffmpeg, input_media),
        FrameSpec(
            width=WIDTH,
            height=HEIGHT,
            fps=FRAMES_PER_SECOND,
        ),
        source_kind="synthetic",
        startup_timeout=STARTUP_TIMEOUT_SECONDS,
        idle_timeout=IDLE_TIMEOUT_SECONDS,
        stderr_limit=STDERR_LIMIT_BYTES,
        terminate_timeout=TERMINATE_TIMEOUT_SECONDS,
        kill_timeout=KILL_TIMEOUT_SECONDS,
    )
    frames: list[bytes] = []
    try:
        with source:
            for _index in range(FRAME_COUNT + 1):
                frame = source.read_frame(stop_event)
                if frame is None:
                    break
                frames.append(frame)
    except FfmpegSupervisorError as error:
        raise MediaProbeError(
            f"pinned FFmpeg supervisor failed safely ({error.code})"
        ) from None
    except MediaProbeError:
        raise
    except Exception:
        raise MediaProbeError("pinned FFmpeg execution failed safely") from None

    _raise_if_stopped(stop_event)
    receipt = source.receipt()
    expected_receipt: dict[str, object] = {
        "cleanup_code": None,
        "exit_code": 0,
        "failure_code": None,
        "frame": {
            "bytes_per_frame": WIDTH * HEIGHT * 3,
            "fps": FRAMES_PER_SECOND,
            "height": HEIGHT,
            "pixel_format": "rgb24",
            "width": WIDTH,
        },
        "frames_delivered": FRAME_COUNT,
        "process_group_closed": True,
        "process_reaped": True,
        "schema_version": 1,
        "source": {"kind": "synthetic"},
        "state": "ended",
        "stderr_bytes": 0,
        "stdout_bytes": RGB_BYTES,
        "termination": "none",
    }
    if len(frames) != FRAME_COUNT:
        raise MediaProbeError("pinned FFmpeg delivered an invalid frame count")
    if any(len(frame) != WIDTH * HEIGHT * 3 for frame in frames):
        raise MediaProbeError("pinned FFmpeg delivered an invalid frame size")
    if receipt != expected_receipt:
        raise MediaProbeError("pinned FFmpeg lifecycle receipt is invalid")
    ffmpeg.assert_intact()
    input_media.assert_intact()
    return _Run(frames=tuple(frames), supervisor=receipt)


def _real_command(
    ffmpeg: _SealedFile,
    input_media: _SealedFile,
) -> tuple[str | InheritedFdArgument, ...]:
    values = {
        "PINNED-FFMPEG-7.0.2": InheritedFdArgument(
            ffmpeg.fd,
            "/proc/self/fd/{fd}",
        ),
        "PRIVATE-SYNTHETIC-Y4M": InheritedFdArgument(
            input_media.fd,
            "pipe:{fd}",
        ),
    }
    return tuple(values.get(argument, argument) for argument in NORMALIZED_COMMAND)


def _write_private_y4m(workspace: _Workspace, payload: bytes) -> None:
    workspace.assert_visible()
    name = "synthetic.y4m"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            _WRITE_FLAGS,
            0o600,
            dir_fd=workspace.fd,
        )
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise MediaProbeError("private Y4M write made no progress")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        visible = os.stat(
            name,
            dir_fd=workspace.fd,
            follow_symlinks=False,
        )
        if (
            _snapshot(opened) != _snapshot(visible)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != len(payload)
        ):
            raise MediaProbeError("private Y4M changed while being written")
    except MediaProbeError:
        raise
    except OSError:
        raise MediaProbeError("private Y4M could not be written") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    workspace.assert_visible()


def _raise_if_stopped(stop_event: _SignalAwareEvent) -> None:
    if stop_event.is_set():
        raise MediaProbeError("media evidence was interrupted safely")


def _read_pinned_regular_file(
    root: Path,
    relative: Path,
    *,
    maximum_bytes: int,
    capture: bool,
    executable: bool,
    missing_message: str,
    copy_to_fd: int | None = None,
) -> _FileRecord:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise MediaProbeError("pinned media path is invalid")
    descriptors: list[int] = []
    directory_snapshots: list[tuple[int, str, tuple[int, int, int, int, int, int]]] = []
    file_descriptor: int | None = None
    try:
        descriptors.append(os.open(root, _DIRECTORY_FLAGS))
        for component in relative.parts[:-1]:
            parent = descriptors[-1]
            before = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(before.st_mode):
                raise MediaProbeError("pinned media path is not a real directory")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
            after = os.fstat(child)
            if _snapshot(before) != _snapshot(after):
                os.close(child)
                raise MediaProbeError("pinned media path changed while opening")
            descriptors.append(child)
            directory_snapshots.append((parent, component, _snapshot(after)))

        parent = descriptors[-1]
        leaf = relative.parts[-1]
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise MediaProbeError("pinned media input is not a regular file")
        if before.st_size > maximum_bytes:
            raise MediaProbeError("pinned media input exceeds its byte limit")
        if executable and (
            before.st_mode & 0o111 == 0 or before.st_mode & 0o022 != 0
        ):
            raise MediaProbeError("pinned FFmpeg executable mode is unsafe")
        file_descriptor = os.open(leaf, _FILE_FLAGS, dir_fd=parent)
        if _snapshot(before) != _snapshot(os.fstat(file_descriptor)):
            raise MediaProbeError("pinned media input changed while opening")

        digest = hashlib.sha256()
        captured: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise MediaProbeError("pinned media input exceeds its byte limit")
            digest.update(chunk)
            if copy_to_fd is not None:
                _write_all(copy_to_fd, chunk)
            if capture:
                captured.append(chunk)
        after = os.fstat(file_descriptor)
        visible = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            _snapshot(before) != _snapshot(after)
            or _snapshot(before) != _snapshot(visible)
            or total != before.st_size
        ):
            raise MediaProbeError("pinned media input changed while being read")
        for parent, component, expected in directory_snapshots:
            visible_directory = os.stat(
                component,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if _snapshot(visible_directory) != expected:
                raise MediaProbeError(
                    "pinned media path changed while being read"
                )
        return _FileRecord(
            size=total,
            sha256=digest.hexdigest(),
            payload=b"".join(captured) if capture else None,
        )
    except FileNotFoundError:
        raise MediaProbeError(missing_message) from None
    except MediaProbeError:
        raise
    except (OSError, ValueError):
        raise MediaProbeError("pinned media input could not be read safely") from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except OSError:
            raise MediaProbeError("sealed media copy could not be written") from None
        if written <= 0:
            raise MediaProbeError("sealed media copy made no progress")
        offset += written


def _identity(status: os.stat_result) -> tuple[int, int, int]:
    return status.st_dev, status.st_ino, stat.S_IFMT(status.st_mode)


def _snapshot(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def iter_rgb_pixels(frames: tuple[bytes, ...]) -> Iterator[bytes]:
    """Yield exact three-byte pixels for independent format validators."""

    for frame in frames:
        for offset in range(0, len(frame), 3):
            yield frame[offset : offset + 3]
