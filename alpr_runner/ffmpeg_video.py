from __future__ import annotations

import argparse
import ctypes
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dtk import DtkLpr, Plate
from .ffmpeg_io import FfmpegFrameSource, FrameSpec
from .runtime_io import (
    atomic_jpeg,
    atomic_json,
    prepare_private_directory,
    private_relative_path,
    source_descriptor,
)
from .zoom import ZoomController, plate_to_target


MEBIBYTE = 1024 * 1024
DEFAULT_BUFFER_RETAIN = 16
DEFAULT_MAX_INFLIGHT_FRAME_MIB = 96
PLATE_CALLBACK_FAILURE = "dtk_plate_callback_failed"
COMPLETED_CALLBACK_FAILURE = "dtk_completed_callback_failed"


class _NativeCallbackFailure(RuntimeError):
    """A source-free callback failure that remains idempotently observable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"native callback failed: {code}")


class _NativeShutdownAmbiguous(RuntimeError):
    """Native destruction started but did not publish safe completion."""

    def __init__(self) -> None:
        super().__init__(
            "native shutdown completion is ambiguous; refusing retry"
        )


class _SignalAwareEvent(threading.Event):
    """Event whose signal-path request is a reentrant plain assignment."""

    def __init__(self) -> None:
        super().__init__()
        self._signal_requested = False

    def is_set(self) -> bool:
        return self._signal_requested or super().is_set()


def _rgb24_frame_size(width: int, height: int) -> int:
    if type(width) is not int:
        raise TypeError("width must be an int")
    if type(height) is not int:
        raise TypeError("height must be an int")
    if width <= 0:
        raise ValueError("width must be greater than zero")
    if height <= 0:
        raise ValueError("height must be greater than zero")
    return width * height * 3


@dataclass(frozen=True)
class FrameLease:
    """One immutable Python payload and its exact native backing allocation."""

    native_buffer: ctypes.Array
    payload: bytes


@dataclass
class _FrameLeaseState:
    lease: FrameLease
    payload_borrows: int = 0


class FrameLeasePool:
    """Count- and byte-bounded ownership for data exposed to native code.

    A successful :meth:`try_acquire` keeps the exact ``ctypes`` array strongly
    referenced with the same immutable ``bytes`` payload until the adapter
    acknowledges that frame ID. The pool never evicts an in-flight lease: when
    capacity or ``max_bytes`` is exhausted, ``try_acquire`` returns ``False``
    and the caller must not hand that buffer to native code.

    ``copy_payload`` borrows the same immutable payload object. If native
    completion races with a borrow, the lease moves to a retired set and stays
    charged against both bounds until the final exact ``release_payload``.
    Unknown, duplicate, and out-of-order operations cannot release another
    frame. ``cancel`` is reserved for paths where native ownership was never
    accepted. Call ``close`` only after the native adapter has been stopped and
    cannot access previously handed-off addresses.

    The byte budget covers only these two Python-owned copies. It deliberately
    excludes allocations inside DTK, Pillow, JPEG encoders, and other runtime
    components.
    """

    def __init__(self, capacity: int, max_bytes: int) -> None:
        if type(capacity) is not int:
            raise TypeError("capacity must be an int")
        if type(max_bytes) is not int:
            raise TypeError("max_bytes must be an int")
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero")

        self._capacity = capacity
        self._max_bytes = max_bytes
        self._retained_bytes = 0
        self._leases: dict[int, _FrameLeaseState] = {}
        self._retired: dict[int, _FrameLeaseState] = {}
        self._closed = False
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._retained_bytes

    @property
    def active_ids(self) -> tuple[int, ...]:
        """Return an immutable insertion-ordered snapshot for diagnostics."""

        with self._lock:
            return tuple(self._leases)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def __len__(self) -> int:
        with self._lock:
            return len(self._leases) + len(self._retired)

    def try_acquire(
        self,
        frame_id: int,
        buffer: ctypes.Array,
        payload: bytes,
    ) -> bool:
        """Lease one exact buffer/payload pair without blocking or eviction."""

        self._validate_frame_id(frame_id, require_positive=True)
        if not isinstance(buffer, ctypes.Array):
            raise TypeError("buffer must be a ctypes array")
        if type(payload) is not bytes:
            raise TypeError("payload must be bytes")
        if not payload:
            raise ValueError("payload must not be empty")
        buffer_bytes = ctypes.sizeof(buffer)
        if buffer_bytes != len(payload):
            raise ValueError("buffer size must exactly match payload size")
        lease_bytes = buffer_bytes + len(payload)

        with self._lock:
            if self._closed:
                raise RuntimeError("frame lease pool is closed")
            if frame_id in self._leases or frame_id in self._retired:
                raise ValueError(
                    f"frame_id {frame_id} already has a retained lease"
                )
            if (
                len(self._leases) + len(self._retired) >= self._capacity
                or lease_bytes > self._max_bytes - self._retained_bytes
            ):
                return False
            self._leases[frame_id] = _FrameLeaseState(
                FrameLease(buffer, payload)
            )
            self._retained_bytes += lease_bytes
            return True

    def copy_payload(self, frame_id: int, expected_size: int) -> bytes | None:
        """Return the exact immutable payload for one live matching frame.

        Every non-``None`` result owns one borrow that the caller must release
        exactly once with :meth:`release_payload`, using the same frame ID and
        object. The borrow stays charged against both pool limits if completion
        retires the frame before that release.
        """

        self._validate_frame_id(frame_id)
        if type(expected_size) is not int:
            raise TypeError("expected_size must be an int")
        if expected_size <= 0:
            raise ValueError("expected_size must be greater than zero")
        with self._lock:
            state = self._leases.get(frame_id)
            if (
                state is None
                or len(state.lease.payload) != expected_size
            ):
                return None
            state.payload_borrows += 1
            return state.lease.payload

    def release_payload(self, frame_id: int, payload: bytes) -> bool:
        """Release one exact payload borrow without affecting another frame."""

        self._validate_frame_id(frame_id)
        if type(payload) is not bytes:
            raise TypeError("payload must be bytes")
        with self._lock:
            state = self._leases.get(frame_id)
            retired = False
            if state is None:
                state = self._retired.get(frame_id)
                retired = state is not None
            if (
                state is None
                or state.payload_borrows <= 0
                or state.lease.payload is not payload
            ):
                return False
            state.payload_borrows -= 1
            if retired and state.payload_borrows == 0:
                del self._retired[frame_id]
                self._debit(state.lease)
            return True

    def acknowledge(self, frame_id: int) -> FrameLease | None:
        """Pop and return one completed lease, or ``None`` if it is absent.

        Returning the lease lets a native completion callback keep the backing
        allocation alive locally until that callback itself returns.
        """

        self._validate_frame_id(frame_id)
        return self._release(frame_id)

    def cancel(self, frame_id: int) -> FrameLease | None:
        """Release a lease whose native handoff failed or was rejected."""

        self._validate_frame_id(frame_id)
        return self._release(frame_id)

    def close(self) -> int:
        """Release every lease after adapter shutdown and reject future work."""

        with self._lock:
            if self._closed:
                return 0
            released = len(self._leases) + len(self._retired)
            self._leases.clear()
            self._retired.clear()
            self._retained_bytes = 0
            self._closed = True
            return released

    def _release(self, frame_id: int) -> FrameLease | None:
        with self._lock:
            state = self._leases.pop(frame_id, None)
            if state is None:
                return None
            if state.payload_borrows > 0:
                self._retired[frame_id] = state
            else:
                self._debit(state.lease)
            return state.lease

    def _debit(self, lease: FrameLease) -> None:
        self._retained_bytes -= ctypes.sizeof(lease.native_buffer) + len(
            lease.payload
        )

    @staticmethod
    def _validate_frame_id(frame_id: int, *, require_positive: bool = False) -> None:
        if type(frame_id) is not int:
            raise TypeError("frame_id must be an int")
        if require_positive and frame_id <= 0:
            raise ValueError("frame_id must be greater than zero")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp", required=True)
    parser.add_argument("--dtk-dir", default="vendor/arm64")
    parser.add_argument("--out", default="runtime-video")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--countries", default="")
    parser.add_argument("--min-plate-width", type=int, default=60)
    parser.add_argument("--max-plate-width", type=int, default=500)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--fps-limit", type=int, default=0)
    parser.add_argument("--confirmations", type=int, default=1)
    parser.add_argument("--accumulation-ms", type=int, default=0)
    parser.add_argument("--duplicate-timeout-ms", type=int, default=600)
    parser.add_argument("--max-zoom", type=float, default=4.0)
    parser.add_argument("--preview-every", type=int, default=20)
    parser.add_argument("--status-print-interval", type=float, default=2.0)
    parser.add_argument(
        "--ffmpeg-startup-timeout",
        type=float,
        default=10.0,
        help="seconds allowed from FFmpeg spawn request to the first RGB frame",
    )
    parser.add_argument(
        "--ffmpeg-idle-timeout",
        type=float,
        default=5.0,
        help="seconds allowed between progress on consecutive RGB frames",
    )
    parser.add_argument(
        "--ffmpeg-stderr-kib",
        type=int,
        default=64,
        help="maximum FFmpeg stderr retained privately before failing closed",
    )
    parser.add_argument(
        "--ffmpeg-terminate-timeout",
        type=float,
        default=2.0,
        help="seconds allowed for supervised FFmpeg process-group termination",
    )
    parser.add_argument(
        "--ffmpeg-kill-timeout",
        type=float,
        default=2.0,
        help="seconds allowed for supervised FFmpeg process-group kill/reap",
    )
    parser.add_argument(
        "--buffer-retain",
        type=int,
        default=DEFAULT_BUFFER_RETAIN,
        help="maximum number of RGB frame leases retained for DTK (default: 16)",
    )
    parser.add_argument(
        "--max-inflight-frame-mib",
        type=int,
        default=DEFAULT_MAX_INFLIGHT_FRAME_MIB,
        help=(
            "MiB budget for in-flight RGB ctypes buffers plus immutable payloads; "
            "excludes DTK, Pillow, and JPEG allocations (default: 96)"
        ),
    )
    parser.add_argument("--allow-unlicensed", action="store_true")
    return parser.parse_args()


class FfmpegVideoAlprRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        # Validate the closed media and supervisor contracts before creating a
        # runtime directory, resolving a vendor path, changing cwd, or loading
        # either proprietary native library.
        frame_spec = FrameSpec(args.width, args.height, args.fps)
        if type(args.buffer_retain) is not int:
            raise TypeError("buffer_retain must be an int")
        if args.buffer_retain <= 0:
            raise ValueError("buffer_retain must be greater than zero")
        if type(args.max_inflight_frame_mib) is not int:
            raise TypeError("max_inflight_frame_mib must be an int")
        if args.max_inflight_frame_mib <= 0:
            raise ValueError("max_inflight_frame_mib must be greater than zero")
        frame_budget = args.max_inflight_frame_mib * MEBIBYTE
        if frame_spec.bytes_per_frame * 2 > frame_budget:
            raise ValueError(
                "max_inflight_frame_mib cannot retain one RGB buffer and payload"
            )
        if type(args.ffmpeg_stderr_kib) is not int:
            raise TypeError("ffmpeg_stderr_kib must be an int")
        command = self._build_ffmpeg_command(args, frame_spec)
        frame_source = FfmpegFrameSource(
            command,
            frame_spec,
            source_kind="rtsp",
            source=args.rtsp,
            startup_timeout=args.ffmpeg_startup_timeout,
            idle_timeout=args.ffmpeg_idle_timeout,
            stderr_limit=args.ffmpeg_stderr_kib * 1024,
            terminate_timeout=args.ffmpeg_terminate_timeout,
            kill_timeout=args.ffmpeg_kill_timeout,
        )

        # Keep the pure-Python orchestration and lease contract importable
        # without loading the optional DTK video/Pillow runtime.
        from .video import PIXFMT_RGB24, DtkVideoLibrary

        self.args = args
        self.frame_spec = frame_spec
        self.frame_source = frame_source
        self.out_dir = prepare_private_directory(args.out)
        self.dtk_dir = Path(args.dtk_dir).expanduser().resolve()
        os.chdir(self.dtk_dir)

        self.video_lib = DtkVideoLibrary(self.dtk_dir)
        self.pixel_format = PIXFMT_RGB24
        self.zoom = ZoomController(max_zoom=args.max_zoom)
        self.stop_event = _SignalAwareEvent()
        self.lock = threading.RLock()
        self.frame_count = 0
        self.plate_count = 0
        self.completed_count = 0
        self.dropped_count = 0
        self.last_status: dict[str, Any] = {}
        self.callback_failure: str | None = None
        self.callback_lock = threading.Lock()
        self.frame_leases = FrameLeasePool(args.buffer_retain, frame_budget)

        self.plate_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            self._plate_callback_boundary
        )
        self.completed_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(
            self._completed_callback_boundary
        )

        self.lpr = DtkLpr(
            self.dtk_dir,
            countries=args.countries,
            min_plate_width=args.min_plate_width,
            max_plate_width=args.max_plate_width,
            require_license=not args.allow_unlicensed,
            video=True,
            plate_callback=self.plate_callback,
            num_threads=args.threads,
            fps_limit=args.fps_limit,
            result_confirmations=args.confirmations,
            result_accumulation_ms=args.accumulation_ms,
            duplicate_timeout_ms=args.duplicate_timeout_ms,
        )
        try:
            self.lpr._completed_callback_ref = self.completed_callback
            self.lpr.lib.LPREngine_SetFrameProcessingCompletedCallback(
                self.lpr.engine,
                self.completed_callback,
            )
        except BaseException as error:
            cleanup_failures: list[BaseException] = []
            try:
                self.frame_source.close()
            except BaseException as cleanup_error:
                cleanup_failures.append(cleanup_error)
            try:
                self.lpr.close()
            except BaseException as cleanup_error:
                cleanup_failures.append(cleanup_error)
            if cleanup_failures:
                raise BaseExceptionGroup(
                    "runner initialization and cleanup failures",
                    [error, *cleanup_failures],
                ) from None
            raise
        self._source_shutdown = False
        self._lpr_shutdown_started = False
        self._lpr_shutdown = False
        self._leases_shutdown = False
        self._run_body_finished = False
        self._run_body_failure: BaseException | None = None
        self._run_cleanup_failures: list[BaseException] = []
        self._run_selected_error: BaseException | None = None
        self._run_outcome_ready = False
        self._previous_signal_handlers: dict[int, Any] | None = None

    def run(self) -> int:
        try: return self._run_once()
        except BaseException as error:
            # A control-flow failure at the guarded call boundary resumes the
            # same ordered, idempotent teardown before propagation.
            if getattr(self, "_run_outcome_ready", False):
                return self._resolve_run_outcome()
            if getattr(self, "_run_body_finished", False):
                self._append_run_cleanup_failure(error)
            elif not isinstance(error, KeyboardInterrupt):
                self._run_body_failure = error
            self._run_body_finished = True
            return self._finish_run()

    def _run_once(self) -> int:
        try:
            self._install_termination_signal_handlers()
            print(f"DTK version: {self.lpr.version()}")
            print(
                "RTSP capture: ffmpeg rawvideo -> "
                "DTK VideoFrame_CreateFromImageBuffer"
            )
            print("FFmpeg input: <redacted RTSP source>")

            started = time.time()
            last_print = 0.0
            last_print_frames = 0
            while True:
                self._raise_for_callback_failure()
                data = self.frame_source.read_frame(self.stop_event)
                if data is None:
                    break
                # A native callback or caller cancellation can race with a
                # bounded producer read. Never hand off the just-read frame
                # after either stop condition has become observable.
                self._raise_for_callback_failure()
                if self.stop_event.is_set():
                    break
                self._put_raw_frame(data)

                elapsed = max(0.001, time.time() - started)
                now = time.time()
                if self.args.status_print_interval > 0 and now - last_print >= self.args.status_print_interval:
                    with self.lock:
                        frames = self.frame_count
                        plates = self.plate_count
                        completed = self.completed_count
                        dropped = self.dropped_count
                    frame_delta = frames - last_print_frames
                    interval = max(0.001, now - last_print) if last_print > 0 else self.args.status_print_interval
                    live_fps = frame_delta / interval
                    status = {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "mode": "ffmpeg-video",
                        "source": source_descriptor("rtsp", self.args.rtsp),
                        "frames_seen": frames,
                        "frames_completed": completed,
                        "frames_dropped": dropped,
                        "plates_seen": plates,
                        "runtime_seconds": round(elapsed, 2),
                        "input_fps": round(frames / elapsed, 2),
                        "live_fps": round(live_fps, 2),
                        "frame_size": {"width": self.args.width, "height": self.args.height},
                        "ffmpeg": self.frame_source.receipt(),
                    }
                    with self.lock:
                        status.update(self.last_status)
                    atomic_json(self.out_dir / "status.json", status)
                    print(
                        f"{status['time']} | ffmpeg frames={frames} | fps={live_fps:.1f} "
                        f"| completed={completed} | dropped={dropped} | plates={plates}"
                    )
                    last_print = now
                    last_print_frames = frames
            self._raise_for_callback_failure()
        except KeyboardInterrupt:
            pass
        except BaseException as error:
            self._run_body_failure = error
        self._run_body_finished = True
        return self._finish_run()

    def _finish_run(self) -> int:
        cleanup_failures = self._run_cleanup_failures
        body_failure = self._run_body_failure
        try:
            self.stop_event.set()
        except BaseException as error:
            self._append_run_cleanup_failure(error)
        try:
            self._shutdown()
        except BaseException as error:
            self._append_run_cleanup_failure(error)
            # KeyboardInterrupt/SystemExit can land immediately before or
            # after a stage call. Retry once from explicit stage state; normal
            # operational Exceptions remain fail-stop and are not hidden.
            if not isinstance(error, Exception):
                try:
                    self._shutdown()
                except BaseException as retry_error:
                    self._append_run_cleanup_failure(retry_error)

        # Check independently even if a prior teardown stage failed. If LPR
        # destruction succeeded, all leases have already been safely released
        # and no last in-flight callback can race past this point. A callback
        # failure already raised by the body is the same persistent condition,
        # not a second cleanup failure.
        try:
            self._raise_for_callback_failure()
        except BaseException as error:
            if not (
                isinstance(body_failure, _NativeCallbackFailure)
                and isinstance(error, _NativeCallbackFailure)
                and body_failure.code == error.code
            ):
                self._append_run_cleanup_failure(error)
        try:
            self._restore_termination_signal_handlers()
        except BaseException as error:
            self._append_run_cleanup_failure(error)
            try:
                self._restore_termination_signal_handlers()
            except BaseException as retry_error:
                self._append_run_cleanup_failure(retry_error)

        cleanup_failure: BaseException | None
        if len(cleanup_failures) > 1:
            cleanup_failure = BaseExceptionGroup(
                "ffmpeg runner cleanup failures",
                cleanup_failures,
            )
        elif cleanup_failures:
            cleanup_failure = cleanup_failures[0]
        else:
            cleanup_failure = None

        selected_error: BaseException | None
        if body_failure is not None and cleanup_failure is not None:
            selected_error = BaseExceptionGroup(
                "ffmpeg runner body and cleanup failures",
                [body_failure, cleanup_failure],
            )
        elif cleanup_failure is not None:
            selected_error = cleanup_failure
        else:
            selected_error = body_failure
        self._run_selected_error = selected_error
        self._run_outcome_ready = True
        return self._resolve_run_outcome()

    def _resolve_run_outcome(self) -> int:
        if not self._run_outcome_ready:
            raise RuntimeError("ffmpeg runner outcome is not ready")
        if self._run_selected_error is not None:
            raise self._run_selected_error
        return 0

    def _append_run_cleanup_failure(
        self,
        error: BaseException,
    ) -> None:
        for existing in self._run_cleanup_failures:
            if existing is error:
                return
            if (
                isinstance(existing, _NativeCallbackFailure)
                and isinstance(error, _NativeCallbackFailure)
                and existing.code == error.code
            ):
                return
        self._run_cleanup_failures.append(error)

    def _shutdown(self) -> None:
        """Resume ordered teardown from the first unfinished stage."""

        if not getattr(self, "_source_shutdown", False):
            self.frame_source.close()
            self._source_shutdown = True
        if not getattr(self, "_lpr_shutdown", False):
            if getattr(self, "_lpr_shutdown_started", False):
                raise _NativeShutdownAmbiguous()
            # Keep intent publication and the native call on one trace line:
            # an exception before the line is retryable; after publication it
            # is deliberately ambiguous and must never double-destroy.
            self._lpr_shutdown_started = True; self.lpr.close()
            self._lpr_shutdown = True
        if not getattr(self, "_leases_shutdown", False):
            # LPREngine_Destroy has returned, so this runner treats the
            # adapter as quiesced before clearing outstanding leases.
            self.frame_leases.close()
            self._leases_shutdown = True

    def _install_termination_signal_handlers(self) -> None:
        """Convert main-thread SIGINT/SIGTERM into an ordered stop request."""

        if (
            threading.current_thread() is not threading.main_thread()
            or self._previous_signal_handlers is not None
        ):
            return
        # Publish rollback ownership before the first global mutation. Each
        # entry is recorded before installing its replacement, so an
        # interruption at any later bytecode remains safely restorable.
        self._previous_signal_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            self._previous_signal_handlers[signum] = previous
            signal.signal(signum, self._handle_termination_signal)

    def _restore_termination_signal_handlers(self) -> None:
        previous = self._previous_signal_handlers
        if previous is None:
            return
        restore_failures: list[BaseException] = []
        for signum, handler in tuple(previous.items()):
            try:
                signal.signal(signum, handler)
            except BaseException as error:
                restore_failures.append(error)
            else:
                # Delete only after restoration returns. An interruption
                # before deletion keeps an idempotent retry record.
                del previous[signum]
        if not previous:
            self._previous_signal_handlers = None
        if len(restore_failures) == 1:
            raise restore_failures[0]
        if restore_failures:
            raise BaseExceptionGroup(
                "signal handler restoration failures",
                restore_failures,
            ) from None

    def _handle_termination_signal(
        self,
        _signum: int,
        _frame: Any,
    ) -> None:
        stop_event = self.stop_event
        if isinstance(stop_event, _SignalAwareEvent):
            # Python signal handlers can re-enter between arbitrary bytecodes.
            # Do not acquire Event's non-reentrant Condition lock here.
            stop_event._signal_requested = True

    @staticmethod
    def _build_ffmpeg_command(
        args: argparse.Namespace,
        frame_spec: FrameSpec,
    ) -> list[str]:
        vf = (
            f"fps={frame_spec.fps},"
            f"scale={frame_spec.width}:{frame_spec.height}:flags=fast_bilinear"
        )
        return [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "1000000",
            "-i",
            args.rtsp,
            "-an",
            "-vf",
            vf,
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]

    def _put_raw_frame(self, data: bytes) -> None:
        if type(data) is not bytes:
            raise TypeError("frame data must be bytes")
        expected_size = _rgb24_frame_size(self.args.width, self.args.height)
        if len(data) != expected_size:
            raise ValueError(
                f"frame data must be exactly {expected_size} RGB24 bytes"
            )

        with self.lock:
            self.frame_count += 1
            frame_id = self.frame_count

        buffer = ctypes.create_string_buffer(data, len(data))
        if not self.frame_leases.try_acquire(frame_id, buffer, data):
            # Backpressure is a dropped input frame, not permission to evict a
            # buffer that the native adapter may still be reading.
            with self.lock:
                self.dropped_count += 1
            return

        try:
            frame = self.video_lib.lib.VideoFrame_CreateFromImageBuffer(
                ctypes.cast(buffer, ctypes.c_void_p),
                self.args.width,
                self.args.height,
                self.args.width * 3,
                self.pixel_format,
                frame_id,
            )
        except BaseException:
            # An exception crossing native code does not prove whether it kept
            # the address. Retain the lease until adapter destruction.
            raise
        if not frame:
            self.frame_leases.cancel(frame_id)
            return

        if self.args.preview_every > 0 and frame_id % self.args.preview_every == 0:
            try:
                self._save_raw_frame(data, self.out_dir / "latest_frame.jpg")
            except BaseException:
                # This frame is definitely not submitted. Even here, release
                # the lease only after native release confirms success.
                self._release_unaccepted_frame(frame, frame_id)
                raise

        try:
            ret = self.lpr.lib.LPREngine_PutFrame(self.lpr.engine, frame, frame_id)
        except BaseException:
            # The call may have accepted ownership before the exception crossed
            # the boundary. Do not release either the frame or its backing data.
            raise
        if ret != 0:
            try:
                release_status = self._release_unaccepted_frame(frame, frame_id)
            finally:
                with self.lock:
                    self.dropped_count += 1
            release_suffix = (
                ""
                if release_status == 0
                else f"; VideoFrame_Release returned {release_status}, lease retained"
            )
            print(f"LPREngine_PutFrame returned {ret}{release_suffix}")

    def _release_unaccepted_frame(self, frame: Any, frame_id: int) -> int:
        """Release a definitely unsubmitted frame without failing open."""

        release_status = int(self.video_lib.lib.VideoFrame_Release(frame))
        if release_status == 0:
            self.frame_leases.cancel(frame_id)
        return release_status

    def _record_callback_failure(self, failure: str) -> None:
        try:
            with self.lock:
                if self.callback_failure is None:
                    self.callback_failure = failure
        except BaseException:
            # A callback boundary must never reflect a secondary bookkeeping
            # failure back through ctypes. Preserve the stable code best-effort
            # without retaining the original exception.
            try:
                if self.callback_failure is None:
                    self.callback_failure = failure
            except BaseException:
                pass
        try:
            self.stop_event.set()
        except BaseException:
            pass

    def _raise_for_callback_failure(self) -> None:
        with self.lock:
            failure = self.callback_failure
        if failure is not None:
            raise _NativeCallbackFailure(failure)

    def _completed_callback_boundary(
        self,
        engine: ctypes.c_void_p,
        frame: ctypes.c_void_p,
        status: int,
    ) -> None:
        completed_lease = None
        try:
            completed_lease = self._on_frame_completed(engine, frame, status)
            if completed_lease is None:
                return
            with self.lock:
                self.completed_count += 1
                if status != 0:
                    self.dropped_count += 1
        except BaseException:
            self._record_callback_failure(COMPLETED_CALLBACK_FAILURE)
        finally:
            if completed_lease is not None:
                # This local belongs to the actual CFUNCTYPE target, keeping
                # native backing storage alive until the boundary returns.
                _ = completed_lease.native_buffer

    def _plate_callback_boundary(
        self,
        engine: ctypes.c_void_p,
        frame: ctypes.c_void_p,
        plate_handle: ctypes.c_void_p,
    ) -> None:
        try:
            self._on_plate_detected(engine, frame, plate_handle)
        except BaseException:
            self._record_callback_failure(PLATE_CALLBACK_FAILURE)

    def _on_frame_completed(
        self,
        _engine: ctypes.c_void_p,
        frame: ctypes.c_void_p,
        _status: int,
    ) -> FrameLease | None:
        frame_id = int(self.video_lib.lib.VideoFrame_Timestamp(frame))
        lease = self.frame_leases.acknowledge(frame_id)
        if lease is None:
            # Duplicate, unknown, or late-after-close callbacks must not alter
            # counters or release any other frame's backing storage.
            return None
        return lease

    def _on_plate_detected(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, plate_handle: ctypes.c_void_p) -> None:
        destroy_attempted = False
        borrowed_frame_id: int | None = None
        payload: bytes | None = None
        try:
            # Resolve the exact payload before waiting for the serialized plate
            # workflow. Multiple plate callbacks can each retain this immutable
            # payload even if completion acknowledges the pool lease meanwhile.
            frame_id = int(self.video_lib.lib.VideoFrame_Timestamp(frame))
            width = int(self.video_lib.lib.VideoFrame_GetWidth(frame))
            height = int(self.video_lib.lib.VideoFrame_GetHeight(frame))
            if width != self.args.width or height != self.args.height:
                raise RuntimeError(
                    "native frame dimensions do not match RGB contract"
                )
            expected_size = _rgb24_frame_size(
                self.args.width,
                self.args.height,
            )
            payload = self.frame_leases.copy_payload(
                frame_id,
                expected_size,
            )
            if payload is not None:
                borrowed_frame_id = frame_id

            with self.callback_lock:
                try:
                    plate = self.lpr._extract_plate(plate_handle)
                finally:
                    # The Python Plate is detached. Release the native handle
                    # before image encoding or durable I/O.
                    destroy_attempted = True
                    self.lpr.lib.LicensePlate_Destroy(plate_handle)

                target = plate_to_target(plate, width, height)
                command = self.zoom.next([target])
                with self.lock:
                    self.plate_count += 1

                preview_path = None
                zoom_path = None
                if payload is not None:
                    preview_path, zoom_path = self._write_plate_previews(
                        payload,
                        plate,
                        target,
                        command,
                    )

                status = {
                    "last_plate_event": {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "plate": plate.to_json(),
                        "target": target.to_json(),
                        "zoom": command.to_json(),
                        "latest_preview": (
                            private_relative_path(preview_path, self.out_dir)
                            if preview_path
                            else None
                        ),
                        "latest_zoom_preview": (
                            private_relative_path(zoom_path, self.out_dir)
                            if zoom_path
                            else None
                        ),
                    }
                }
                with self.lock:
                    self.last_status = status
                atomic_json(
                    self.out_dir / "plate_event.json",
                    status["last_plate_event"],
                )
                atomic_json(
                    self.out_dir / "zoom_command.json",
                    command.to_json(),
                )
                print(
                    f"{status['last_plate_event']['time']} | {plate.text} "
                    f"| zoom={command.zoom_ratio:.2f}"
                )
        finally:
            try:
                if not destroy_attempted:
                    # Metadata validation and lock acquisition failures still
                    # own the plate handle. Prefer serialized destruction; if
                    # lock acquisition fails, destroy directly exactly once.
                    try:
                        with self.callback_lock:
                            destroy_attempted = True
                            self.lpr.lib.LicensePlate_Destroy(plate_handle)
                    except BaseException:
                        if destroy_attempted:
                            raise
                        destroy_attempted = True
                        self.lpr.lib.LicensePlate_Destroy(plate_handle)
            finally:
                if borrowed_frame_id is not None and payload is not None:
                    if not self.frame_leases.release_payload(
                        borrowed_frame_id,
                        payload,
                    ):
                        raise RuntimeError(
                            "exact frame payload borrow release failed"
                        )

    def _write_plate_previews(
        self,
        payload: bytes,
        plate: Plate,
        target: Any,
        command: Any,
    ) -> tuple[Path, Path]:
        try:
            from PIL import Image
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Pillow is required only when writing frame previews"
            ) from error
        image = Image.frombytes(
            "RGB",
            (self.args.width, self.args.height),
            payload,
        )
        preview_path = self._save_annotated(
            image,
            self.out_dir / "latest.jpg",
            plate=plate,
            target=target,
        )
        zoomed = self.zoom.crop_image(image, command)
        zoom_path = self.out_dir / "latest_zoom.jpg"
        atomic_jpeg(
            zoom_path,
            zoomed,
            quality=88,
        )
        return preview_path, zoom_path

    def _save_raw_frame(self, data: bytes, path: Path) -> None:
        try:
            from PIL import Image
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Pillow is required only when writing frame previews"
            ) from error
        image = Image.frombytes("RGB", (self.args.width, self.args.height), data)
        atomic_jpeg(path, image, quality=85)

    def _save_annotated(self, image: Any, path: Path, plate: Plate, target: Any) -> Path:
        try:
            from PIL import ImageDraw
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Pillow is required only when writing frame previews"
            ) from error
        result = image.copy()
        draw = ImageDraw.Draw(result)
        draw.rectangle(
            (plate.x, plate.y, plate.x + plate.width, plate.y + plate.height),
            outline=(242, 201, 76),
            width=3,
        )
        draw.text((plate.x, max(0, plate.y - 16)), f"{plate.text} {plate.confidence}", fill=(255, 255, 255))
        draw.rectangle(
            (
                target.left * image.width,
                target.top * image.height,
                target.right * image.width,
                target.bottom * image.height,
            ),
            outline=(39, 174, 96),
            width=4,
        )
        atomic_jpeg(path, result, quality=88)
        return path

def main() -> int:
    return FfmpegVideoAlprRunner(parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
