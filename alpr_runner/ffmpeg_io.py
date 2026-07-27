from __future__ import annotations

import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, NoReturn, Sequence

MAX_FRAME_WIDTH = 4096
MAX_FRAME_HEIGHT = 2160
MAX_FRAME_RATE = 240
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
_READ_CHUNK_BYTES = 64 * 1024
_SOURCE_KINDS = frozenset(
    {"device", "file", "pipe", "rtsp", "synthetic", "unknown"}
)
_SAFE_SOURCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_FAILURE_CODES = frozenset(
    {
        "cleanup_failed",
        "frame_idle_timeout",
        "io_failed",
        "io_setup_failed",
        "process_exit_nonzero",
        "reap_timeout",
        "spawn_failed",
        "startup_timeout",
        "stderr_limit_exceeded",
        "truncated_frame",
        "unsupported_platform",
    }
)


class FfmpegSupervisorError(RuntimeError):
    """A public-safe producer failure with a stable machine-readable code."""

    def __init__(self, code: str, receipt: dict[str, object]) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError("unsupported ffmpeg supervisor failure code")
        self.code = code
        self._receipt = _copy_receipt(receipt)
        super().__init__(f"ffmpeg supervisor failed: {code}")

    def receipt(self) -> dict[str, object]:
        """Return a detached, JSON-compatible failure receipt."""

        return _copy_receipt(self._receipt)


@dataclass(frozen=True)
class FrameSpec:
    """The closed RGB24 frame contract accepted by the supervisor."""

    width: int
    height: int
    fps: int
    pixel_format: str = "rgb24"

    def __post_init__(self) -> None:
        for name, value, upper in (
            ("width", self.width, MAX_FRAME_WIDTH),
            ("height", self.height, MAX_FRAME_HEIGHT),
            ("fps", self.fps, MAX_FRAME_RATE),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if not 1 <= value <= upper:
                raise ValueError(f"{name} is out of bounds")
        if type(self.pixel_format) is not str:
            raise TypeError("pixel_format must be text")
        if self.pixel_format != "rgb24":
            raise ValueError("pixel_format must be rgb24")
        if self.bytes_per_frame > MAX_FRAME_BYTES:
            raise ValueError("frame byte size is out of bounds")

    @property
    def bytes_per_frame(self) -> int:
        return self.width * self.height * 3

    def to_receipt(self) -> dict[str, object]:
        return {
            "bytes_per_frame": self.bytes_per_frame,
            "fps": self.fps,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "width": self.width,
        }


class FfmpegFrameSource:
    """Bounded POSIX supervision for one FFmpeg RGB24 stdout stream.

    Constructing the source is side-effect free. Call :meth:`start`, use it as
    a context manager, or simply call :meth:`read_frame` to start lazily.
    ``read_frame`` returns one exact immutable frame, and returns ``None`` only
    after a clean EOF or caller cancellation. Producer failures raise
    :class:`FfmpegSupervisorError`; the receipt explicitly reports the rare
    case where bounded cleanup could not yet confirm that the child was reaped.

    The instance is intentionally single-consumer. A ``threading.Event`` may
    be supplied to ``read_frame`` when another thread needs to request a
    bounded cancellation.
    """

    def __init__(
        self,
        command: Sequence[str],
        spec: FrameSpec,
        *,
        source_kind: str,
        source: str | None = None,
        startup_timeout: float = 10.0,
        idle_timeout: float = 5.0,
        stderr_limit: int = 64 * 1024,
        terminate_timeout: float = 2.0,
        kill_timeout: float = 2.0,
    ) -> None:
        self._command = _validate_command(command)
        if type(spec) is not FrameSpec:
            raise TypeError("spec must be a FrameSpec")
        self._spec = spec
        self._source_kind = _validate_source_kind(source_kind)
        if source is not None and type(source) is not str:
            raise TypeError("source must be text or None")
        # Kept only as a private redaction boundary. It is never copied into
        # an error, exception argument, receipt, or diagnostic field.
        self._source = source
        self._startup_timeout = _validate_timeout(
            "startup_timeout", startup_timeout
        )
        self._idle_timeout = _validate_timeout("idle_timeout", idle_timeout)
        self._terminate_timeout = _validate_timeout(
            "terminate_timeout", terminate_timeout
        )
        self._kill_timeout = _validate_timeout(
            "kill_timeout", kill_timeout
        )
        if type(stderr_limit) is not int:
            raise TypeError("stderr_limit must be an int")
        if not 1 <= stderr_limit <= MAX_STDERR_BYTES:
            raise ValueError("stderr_limit is out of bounds")
        self._stderr_limit = stderr_limit

        self._state = "new"
        self._failure_code: str | None = None
        self._cleanup_code: str | None = None
        self._exit_code: int | None = None
        self._process_reaped: bool | None = None
        self._process_group_closed: bool | None = None
        self._termination = "none"
        self._frames_delivered = 0
        self._stdout_bytes = 0
        self._stderr_bytes = 0
        self._frame_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._process: subprocess.Popen[bytes] | None = None
        self._pgid: int | None = None
        self._selector: selectors.BaseSelector | None = None
        self._stdout_eof = False
        self._stderr_eof = False
        self._started_at: float | None = None
        self._start_signal_mask: set[signal.Signals] | None = None

    @property
    def spec(self) -> FrameSpec:
        return self._spec

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> FfmpegFrameSource:
        """Start the child once using a new, stable POSIX process group."""

        if self._state == "running":
            return self
        if self._state != "new":
            raise RuntimeError("ffmpeg frame source cannot be restarted")
        if (
            os.name != "posix"
            or not hasattr(os, "killpg")
            or not hasattr(signal, "pthread_sigmask")
        ):
            self._raise_failure("unsupported_platform")

        start_requested_at = time.monotonic()
        try:
            return self._start_once(start_requested_at)
        except BaseException as error:
            mask_failure: BaseException | None = None
            try:
                self._restore_start_signal_mask()
            except BaseException as restore_error:
                mask_failure = restore_error
            if mask_failure is not None:
                error = BaseExceptionGroup(
                    "ffmpeg start and signal-mask restoration failures",
                    [error, mask_failure],
                )
            # Failures raised from the guarded helper after adoption cannot
            # unwind past an owned live child.
            if (
                self._process is not None
                and self._state in {"new", "running"}
            ):
                self._abort_started_process(error)
            if mask_failure is not None:
                raise error
            raise

    def _start_once(self, start_requested_at: float) -> FfmpegFrameSource:
        """Spawn, adopt, configure, and return under the caller's guard."""

        process: subprocess.Popen[bytes] | None = None
        failure_code: str | None = None
        interruption: BaseException | None = None
        try:
            # Block SIGINT only across spawn and setup ownership. Restoring the
            # calling thread's exact mask after the process, pipes, and selector
            # are all recorded delivers any pending KeyboardInterrupt inside
            # this protected try block, where bounded cleanup owns everything.
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(),
            )
            self._start_signal_mask = set(previous_mask)
            try:
                signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    {signal.SIGINT},
                )
                process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=True,
                    close_fds=True,
                )
                self._adopt_started_process(process, start_requested_at)
                process.args = ("<redacted-ffmpeg-command>",)

                if process.stdout is None or process.stderr is None:
                    raise RuntimeError("ffmpeg pipes were not created")
                os.set_blocking(process.stdout.fileno(), False)
                os.set_blocking(process.stderr.fileno(), False)
                selector = selectors.DefaultSelector()
                self._selector = selector
                selector.register(
                    process.stdout,
                    selectors.EVENT_READ,
                    "stdout",
                )
                selector.register(
                    process.stderr,
                    selectors.EVENT_READ,
                    "stderr",
                )
            finally:
                self._restore_start_signal_mask()
        except Exception:
            failure_code = (
                "spawn_failed" if process is None else "io_setup_failed"
            )
        except BaseException as error:
            interruption = error

        # Leave the originating exception handler before publishing any safe
        # supervisor error. That prevents private Popen diagnostics from
        # surviving as an exception context even though traceback display
        # would otherwise suppress them with ``from None``.
        if failure_code is not None:
            if process is None:
                self._raise_failure(failure_code)
            if self._process is None:
                self._adopt_started_process(process, start_requested_at)
            self._abort_started_process(None)
        if interruption is not None:
            if process is None:
                raise interruption
            if self._process is None:
                self._adopt_started_process(process, start_requested_at)
            self._abort_started_process(interruption)
        return self

    def _restore_start_signal_mask(self) -> None:
        previous_mask = self._start_signal_mask
        if previous_mask is None:
            return
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        # Clear ownership only after the exact restoration returns. An
        # asynchronous exception before this assignment leaves an idempotent
        # retry record for start()'s outer guard.
        self._start_signal_mask = None

    def _adopt_started_process(
        self,
        process: subprocess.Popen[bytes],
        started_at: float,
    ) -> None:
        """Record enough ownership to clean a child from every later path."""

        self._process = process
        # Publish ownership before reading any property on the returned
        # process. If even PID access raises, the outer guard and close() still
        # know that a child must be terminated rather than treating this as an
        # untouched source.
        self._state = "running"
        self._process_reaped = False
        self._process_group_closed = False
        self._started_at = started_at
        self._command = ()
        self._source = None
        self._capture_process_group_id(process)

    def _capture_process_group_id(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        """Capture the start_new_session PID/PGID invariant exactly once."""

        if self._pgid is not None:
            return
        pid = process.pid
        if type(pid) is not int or pid <= 0:
            raise ValueError("ffmpeg process PID is invalid")
        self._pgid = pid

    def _abort_started_process(
        self,
        original_error: BaseException | None,
        *,
        failure_code: str = "io_setup_failed",
    ) -> NoReturn:
        """Reap an adopted child before surfacing a safe primary failure."""

        self._failure_code = failure_code
        cleanup_code: str | None = None
        cleanup_failures: list[BaseException] = []
        process = self._process
        if process is not None:
            try:
                # An asynchronous exception may have interrupted the first
                # assignment itself. Retry without allowing redaction failure
                # to replace the original control-flow exception.
                process.args = ("<redacted-ffmpeg-command>",)
            except BaseException:
                pass
        if not (
            self._process_reaped is True
            and self._process_group_closed is True
        ):
            try:
                cleanup_code = self._terminate_and_reap()
                self._cleanup_code = cleanup_code
            except BaseException as cleanup_error:
                self._cleanup_code = "cleanup_failed"
                cleanup_failures.append(cleanup_error)
        else:
            # Never signal a numeric PGID again after both the leader and its
            # group were confirmed gone. Descriptor-only retries must not risk
            # targeting an unrelated group if the kernel has reused that ID.
            self._cleanup_code = None
        io_closed = False
        try:
            self._close_io()
            io_closed = True
        except BaseException as cleanup_error:
            self._cleanup_code = "cleanup_failed"
            cleanup_failures.append(cleanup_error)
        self._state = "failed"
        if (
            self._process_reaped is True
            and self._process_group_closed is True
            and io_closed
        ):
            self._drop_quiesced_process_reference()

        if cleanup_code is not None:
            cleanup_failures.insert(
                0,
                FfmpegSupervisorError(cleanup_code, self.receipt()),
            )
        primary_error = (
            original_error
            if original_error is not None
            else FfmpegSupervisorError(
                failure_code,
                self.receipt(),
            )
        )
        if cleanup_failures:
            raise BaseExceptionGroup(
                "ffmpeg setup and cleanup failures",
                [primary_error, *cleanup_failures],
            ) from None
        raise primary_error

    def read_frame(
        self, stop_event: threading.Event | None = None
    ) -> bytes | None:
        """Read exactly one bounded frame, clean EOF, or a safe failure."""

        if stop_event is not None and not isinstance(
            stop_event, threading.Event
        ):
            raise TypeError("stop_event must be a threading.Event or None")
        if self._state == "new":
            self.start()
        if self._state == "ended":
            return None
        if self._state == "closed":
            return None
        if self._state == "failed":
            assert self._failure_code is not None
            raise FfmpegSupervisorError(
                self._failure_code, self.receipt()
            ) from None
        if self._state != "running":
            raise RuntimeError("ffmpeg frame source is not readable")

        if self._frames_delivered == 0:
            wait_budget = self._startup_timeout
            timeout_code = "startup_timeout"
            assert self._started_at is not None
            deadline = self._started_at + wait_budget
        else:
            wait_budget = self._idle_timeout
            timeout_code = "frame_idle_timeout"
            # Caller processing time is outside the media-idle budget.
            deadline = time.monotonic() + wait_budget

        while True:
            if stop_event is not None and stop_event.is_set():
                self.close()
                return None

            if (
                timeout_code == "startup_timeout"
                and time.monotonic() >= deadline
            ):
                self._raise_failure(timeout_code)

            if len(self._frame_buffer) == self._spec.bytes_per_frame:
                frame = bytes(self._frame_buffer)
                self._frame_buffer.clear()
                self._frames_delivered += 1
                return frame

            now = time.monotonic()
            process = self._require_process()
            if self._stdout_eof and self._stderr_eof:
                if self._poll_or_fail(process) is None:
                    wait_seconds = min(max(0.0, deadline - now), 0.05)
                    wait_timed_out = False
                    wait_failed = False
                    try:
                        process.wait(timeout=wait_seconds)
                    except subprocess.TimeoutExpired:
                        wait_timed_out = True
                    except (OSError, subprocess.SubprocessError):
                        wait_failed = True
                    if wait_failed:
                        self._raise_failure("io_failed")
                    if wait_timed_out:
                        if time.monotonic() >= deadline:
                            self._raise_failure(timeout_code)
                        continue
                self._finish_after_eof()
                return None

            wait_seconds = max(0.0, deadline - now)
            if stop_event is not None:
                wait_seconds = min(wait_seconds, 0.05)
            selector = self._require_selector()
            select_failed = False
            try:
                events = selector.select(wait_seconds)
            except (OSError, ValueError):
                events = []
                select_failed = True
            if select_failed:
                self._raise_failure("io_failed")

            stdout_progress = False
            for key, _mask in events:
                if key.data == "stdout":
                    stdout_progress = (
                        self._read_stdout(key.fileobj) or stdout_progress
                    )
                elif key.data == "stderr":
                    self._read_stderr(key.fileobj)
                else:
                    self._raise_failure("io_failed")

            if stdout_progress and timeout_code == "frame_idle_timeout":
                deadline = time.monotonic() + wait_budget
            if self._stdout_eof and self._frame_buffer:
                self._raise_failure("truncated_frame")

            # poll() also reaps a child that has already exited. Pipe EOF still
            # must be consumed first so an oversized stderr cannot be ignored.
            self._poll_or_fail(process)
            if not events and time.monotonic() >= deadline:
                self._raise_failure(timeout_code)
            if (
                events
                and not stdout_progress
                and not self._stdout_eof
                and time.monotonic() >= deadline
            ):
                # select() has already given queued stdout a zero-wait chance;
                # stderr activity alone cannot keep a stalled producer alive.
                self._raise_failure(timeout_code)

    def frames(
        self, stop_event: threading.Event | None = None
    ) -> Iterator[bytes]:
        """Yield frames until clean EOF or requested cancellation."""

        while True:
            frame = self.read_frame(stop_event)
            if frame is None:
                return
            yield frame

    def __iter__(self) -> Iterator[bytes]:
        return self.frames()

    def receipt(self) -> dict[str, object]:
        """Return a deterministic source-safe lifecycle receipt.

        On Linux, ``process_group_closed=True`` means the original process
        group has no live non-zombie members. Kernel-visible ``Z``/``X``
        entries may remain until their adoptive parent reaps them; the field
        deliberately describes execution quiescence, not literal PID-table
        disappearance. Other POSIX platforms conservatively require
        ``killpg(pgid, 0)`` to report that the group no longer exists.
        """

        return {
            "cleanup_code": self._cleanup_code,
            "exit_code": self._exit_code,
            "failure_code": self._failure_code,
            "frame": self._spec.to_receipt(),
            "frames_delivered": self._frames_delivered,
            "process_group_closed": self._process_group_closed,
            "process_reaped": self._process_reaped,
            "schema_version": 1,
            "source": {"kind": self._source_kind},
            "state": self._state,
            "stderr_bytes": self._stderr_bytes,
            "stdout_bytes": self._stdout_bytes,
            "termination": self._termination,
        }

    def close(self) -> None:
        """Terminate, escalate if needed, reap, and close every descriptor."""

        try:
            self._close_once()
        except BaseException as error:
            if (
                isinstance(error, FfmpegSupervisorError)
                and self._state == "failed"
            ):
                raise
            if (
                self._process is not None
                and self._state != "closed"
            ):
                self._abort_started_process(
                    error,
                    failure_code=self._failure_code or "cleanup_failed",
                )
            raise

    def _close_once(self) -> None:
        """Perform one close attempt under :meth:`close`'s recovery guard."""

        if self._state == "closed":
            return
        if self._state == "new":
            if self._process is None:
                self._command = ()
                self._source = None
                # Publish the terminal state only after every private input
                # has been scrubbed. A control-flow exception before this
                # assignment leaves the source retryable.
                self._state = "closed"
                return
            # A BaseException may have interrupted process adoption between
            # storing the Popen owner and publishing the running state.
            self._state = "running"
        if self._state == "ended":
            self._close_io()
            self._drop_quiesced_process_reference()
            return
        if self._state == "failed":
            if (
                self._process is not None
                and (
                    self._process_reaped is not True
                    or self._process_group_closed is not True
                )
            ):
                cleanup_code = self._terminate_and_reap()
                self._cleanup_code = cleanup_code
                self._close_io()
                if cleanup_code is not None:
                    raise FfmpegSupervisorError(
                        cleanup_code, self.receipt()
                    ) from None
            else:
                self._close_io()
            self._cleanup_code = None
            self._drop_quiesced_process_reference()
            return

        cleanup_code = self._terminate_and_reap()
        self._cleanup_code = cleanup_code
        self._close_io()
        if cleanup_code is not None:
            self._failure_code = cleanup_code
            self._state = "failed"
            raise FfmpegSupervisorError(
                cleanup_code, self.receipt()
            ) from None
        self._drop_quiesced_process_reference()
        self._state = "closed"

    def _drop_quiesced_process_reference(self) -> None:
        """Drop private process metadata only after every cleanup stage."""

        if (
            self._process_reaped is True
            and self._process_group_closed is True
        ):
            # No retry is needed after confirmed quiescence and descriptor
            # cleanup. Dropping the Popen reference also removes any command
            # value that an unusual object refused to redact.
            self._process = None
            self._pgid = None

    def __enter__(self) -> FfmpegFrameSource:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del exc_type, traceback
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            raise BaseExceptionGroup(
                "ffmpeg body and cleanup failures",
                [exc, cleanup_error],
            ) from None
        return False

    def _read_stdout(self, stream: Any) -> bool:
        remaining = self._spec.bytes_per_frame - len(self._frame_buffer)
        if remaining <= 0:
            return False
        request = min(_READ_CHUNK_BYTES, remaining)
        read_failed = False
        try:
            chunk = os.read(stream.fileno(), request)
        except BlockingIOError:
            return False
        except OSError:
            chunk = b""
            read_failed = True
        if read_failed:
            self._raise_failure("io_failed")
        if not chunk:
            self._mark_eof(stream, stdout=True)
            return False
        self._frame_buffer.extend(chunk)
        self._stdout_bytes += len(chunk)
        return True

    def _poll_or_fail(
        self,
        process: subprocess.Popen[bytes],
    ) -> int | None:
        try:
            return process.poll()
        except (OSError, subprocess.SubprocessError):
            self._raise_failure("io_failed")

    def _read_stderr(self, stream: Any) -> None:
        remaining = self._stderr_limit - len(self._stderr_buffer)
        # Read one byte beyond the cap to detect overflow without retaining it.
        request = min(_READ_CHUNK_BYTES, remaining + 1)
        read_failed = False
        try:
            chunk = os.read(stream.fileno(), request)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
            read_failed = True
        if read_failed:
            self._raise_failure("io_failed")
        if not chunk:
            self._mark_eof(stream, stdout=False)
            return
        self._stderr_bytes += len(chunk)
        accepted = min(len(chunk), remaining)
        if accepted:
            self._stderr_buffer.extend(chunk[:accepted])
        if accepted != len(chunk):
            self._raise_failure("stderr_limit_exceeded")

    def _mark_eof(self, stream: Any, *, stdout: bool) -> None:
        selector = self._selector
        if selector is not None:
            try:
                selector.unregister(stream)
            except (KeyError, OSError, ValueError):
                pass
        if stdout:
            self._stdout_eof = True
        else:
            self._stderr_eof = True

    def _finish_after_eof(self) -> None:
        try:
            self._finish_after_eof_once()
        except BaseException as error:
            if (
                isinstance(error, FfmpegSupervisorError)
                and self._state == "failed"
            ):
                raise
            if self._process is not None:
                self._abort_started_process(
                    error,
                    failure_code=self._failure_code or "cleanup_failed",
                )
            self._failure_code = self._failure_code or "cleanup_failed"
            self._state = "failed"
            raise

    def _finish_after_eof_once(self) -> None:
        """Finalize clean EOF under :meth:`_finish_after_eof`'s guard."""

        process = self._require_process()
        return_code = self._poll_or_fail(process)
        if return_code is None:
            return
        self._exit_code = int(return_code)
        self._process_reaped = True
        if return_code != 0:
            self._raise_failure("process_exit_nonzero")
        cleanup_code = self._terminate_and_reap()
        self._cleanup_code = cleanup_code
        if cleanup_code is not None:
            self._failure_code = cleanup_code
            self._state = "failed"
            self._close_io()
            raise FfmpegSupervisorError(
                cleanup_code, self.receipt()
            ) from None
        self._state = "ended"
        self._close_io()
        self._drop_quiesced_process_reference()

    def _raise_failure(self, code: str) -> NoReturn:
        self._failure_code = code
        cleanup_failures: list[BaseException] = []
        if self._process is not None:
            try:
                cleanup_code = self._terminate_and_reap()
                self._cleanup_code = cleanup_code
            except BaseException as cleanup_error:
                self._cleanup_code = "cleanup_failed"
                cleanup_failures.append(cleanup_error)
        try:
            self._close_io()
        except BaseException as cleanup_error:
            self._cleanup_code = "cleanup_failed"
            cleanup_failures.append(cleanup_error)
        self._state = "failed"
        primary_error = FfmpegSupervisorError(code, self.receipt())
        if cleanup_failures:
            raise BaseExceptionGroup(
                "ffmpeg failure and cleanup failures",
                [primary_error, *cleanup_failures],
            ) from None
        raise primary_error from None

    def _terminate_and_reap(self) -> str | None:
        process = self._process
        if process is None:
            return None
        if self._pgid is None:
            try:
                self._capture_process_group_id(process)
            except BaseException:
                return self._terminate_without_process_group_id(process)
        try:
            return_code = process.poll()
        except (OSError, subprocess.SubprocessError):
            # A known start_new_session PGID is sufficient for bounded group
            # cleanup. Treat an unreliable leader poll as "status unknown"
            # instead of abandoning a live child before signalling it.
            return_code = None
        if return_code is not None:
            self._exit_code = int(return_code)
            self._process_reaped = True

        assert self._pgid is not None
        if self._process_group_closed is True:
            # Group quiescence is a terminal fact about this numeric PGID.
            # If only leader reaping remains, never signal the identifier
            # again: the kernel may already have reused it for another group.
            if return_code is None:
                try:
                    return_code = process.wait(timeout=self._kill_timeout)
                except subprocess.TimeoutExpired:
                    return "reap_timeout"
                except (OSError, subprocess.SubprocessError):
                    return "cleanup_failed"
                self._exit_code = int(return_code)
                self._process_reaped = True
            return None

        term_started = time.monotonic()
        try:
            os.killpg(self._pgid, signal.SIGTERM)
            self._termination = "term"
        except ProcessLookupError:
            self._process_group_closed = True
        except OSError:
            return "cleanup_failed"

        if return_code is None:
            term_timed_out = False
            try:
                return_code = process.wait(timeout=self._terminate_timeout)
            except subprocess.TimeoutExpired:
                term_timed_out = True
            except (OSError, subprocess.SubprocessError):
                # A failed leader wait does not revoke known PGID ownership.
                # Escalate and confirm the group before reporting cleanup.
                return self._kill_and_reap(process)
            if term_timed_out:
                return self._kill_and_reap(process)
            assert return_code is not None
            self._exit_code = int(return_code)
            self._process_reaped = True
        else:
            self._exit_code = int(return_code)
            self._process_reaped = True

        remaining_grace = max(
            0.0,
            self._terminate_timeout - (time.monotonic() - term_started),
        )
        group_status = self._wait_for_group_quiescence(remaining_grace)
        if group_status == "failed":
            return "cleanup_failed"
        if group_status == "quiescent":
            self._process_group_closed = True
        if group_status == "running":
            kill_status = self._send_sigkill()
            if kill_status is not None:
                return kill_status
            group_status = self._wait_for_group_quiescence(
                self._kill_timeout
            )
            if group_status == "failed":
                return "cleanup_failed"
            if group_status == "running":
                return "reap_timeout"
            self._process_group_closed = True

        self._exit_code = int(return_code)
        self._process_reaped = True
        return None

    def _terminate_without_process_group_id(
        self,
        process: subprocess.Popen[bytes],
    ) -> str:
        """Bound leader cleanup without claiming unknown group quiescence."""

        self._process_group_closed = False
        return_code: int | None = None
        try:
            polled = process.poll()
            if polled is not None:
                return_code = int(polled)
        except BaseException:
            pass

        if return_code is None:
            try:
                process.terminate()
                self._termination = "term"
            except BaseException:
                pass
            try:
                waited = process.wait(timeout=self._terminate_timeout)
                return_code = int(waited)
            except BaseException:
                try:
                    process.kill()
                    self._termination = "kill"
                except BaseException:
                    pass
                try:
                    waited = process.wait(timeout=self._kill_timeout)
                    return_code = int(waited)
                except BaseException:
                    return_code = None

        if return_code is not None:
            self._exit_code = return_code
            self._process_reaped = True
        # The leader may be reaped, but without the start_new_session PGID the
        # supervisor cannot honestly prove that descendants are gone. Retain
        # the owner and surface a safe cleanup failure for later retry.
        return "cleanup_failed"

    def _kill_and_reap(
        self, process: subprocess.Popen[bytes]
    ) -> str | None:
        kill_status = self._send_sigkill()
        if kill_status is not None:
            return kill_status
        kill_deadline = time.monotonic() + self._kill_timeout
        reap_status: str | None = None
        try:
            return_code = process.wait(
                timeout=max(0.0, kill_deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            return_code = None
            reap_status = "reap_timeout"
        except (OSError, subprocess.SubprocessError):
            return_code = None
            reap_status = "cleanup_failed"
        if return_code is not None:
            self._exit_code = int(return_code)
            self._process_reaped = True

        remaining = max(0.0, kill_deadline - time.monotonic())
        group_status = self._wait_for_group_quiescence(remaining)
        if group_status == "failed":
            return "cleanup_failed"
        if group_status == "running":
            return "reap_timeout"
        self._process_group_closed = True
        return reap_status

    def _send_sigkill(self) -> str | None:
        assert self._pgid is not None
        self._process_group_closed = False
        try:
            os.killpg(self._pgid, signal.SIGKILL)
            self._termination = "kill"
        except ProcessLookupError:
            self._process_group_closed = True
        except OSError:
            return "cleanup_failed"
        return None

    def _wait_for_group_quiescence(self, timeout: float) -> str:
        assert self._pgid is not None
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.killpg(self._pgid, 0)
            except ProcessLookupError:
                self._process_group_closed = True; return "quiescent"
            except OSError:
                return "failed"
            if sys.platform.startswith("linux"):
                live_members = _linux_group_has_live_members(self._pgid)
                if live_members is False:
                    self._process_group_closed = True; return "quiescent"
            if time.monotonic() >= deadline:
                return "running"
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def _close_io(self) -> None:
        close_failures: list[BaseException] = []
        selector = self._selector
        if selector is not None:
            # Retain ownership until close returns. In particular, an
            # asynchronous exception must leave the selector reachable so a
            # later fail-closed cleanup attempt can close its registrations
            # and underlying kernel descriptor.
            try:
                selector.close()
            except BaseException as error:
                close_failures.append(error)
            else:
                self._selector = None
        process = self._process
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        # Continue closing sibling descriptors, but retain the
                        # Popen owner until a later retry confirms that every
                        # stream has closed.
                        close_failures.append(error)
        self._frame_buffer.clear()
        self._stderr_buffer.clear()
        self._command = ()
        self._source = None
        if len(close_failures) == 1:
            raise close_failures[0]
        if close_failures:
            raise BaseExceptionGroup(
                "ffmpeg descriptor cleanup failures",
                close_failures,
            ) from None

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            self._raise_failure("io_failed")
        return self._process

    def _require_selector(self) -> selectors.BaseSelector:
        if self._selector is None:
            self._raise_failure("io_failed")
        return self._selector


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise TypeError("command must be a sequence of text arguments")
    normalized: list[str] = []
    for argument in command:
        if type(argument) is not str:
            raise TypeError("command arguments must be text")
        if not argument or "\0" in argument:
            raise ValueError("command contains an invalid argument")
        normalized.append(argument)
    if not normalized:
        raise ValueError("command must not be empty")
    return tuple(normalized)


def _validate_source_kind(source_kind: str) -> str:
    if type(source_kind) is not str:
        raise TypeError("source_kind must be text")
    if (
        source_kind not in _SOURCE_KINDS
        or _SAFE_SOURCE_KIND.fullmatch(source_kind) is None
    ):
        raise ValueError("source_kind is unsupported")
    return source_kind


def _validate_timeout(name: str, value: float) -> float:
    if type(value) not in {int, float}:
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0.0
        or normalized > MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{name} is out of bounds")
    return normalized


def _copy_receipt(receipt: dict[str, object]) -> dict[str, object]:
    frame = receipt.get("frame")
    source = receipt.get("source")
    copied = dict(receipt)
    if isinstance(frame, dict):
        copied["frame"] = dict(frame)
    if isinstance(source, dict):
        copied["source"] = dict(source)
    return copied


def _linux_group_has_live_members(pgid: int) -> bool | None:
    """Return whether ``pgid`` has a non-zombie Linux /proc member."""

    try:
        entries = os.scandir("/proc")
    except OSError:
        return None
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(
                    f"/proc/{entry.name}/stat",
                    encoding="ascii",
                    errors="replace",
                ) as stream:
                    record = stream.read(4096)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                return None
            except OSError:
                return None
            closing_parenthesis = record.rfind(")")
            if closing_parenthesis < 0:
                continue
            fields = record[closing_parenthesis + 1 :].split()
            if len(fields) < 3:
                continue
            state = fields[0]
            try:
                member_pgid = int(fields[2])
            except ValueError:
                continue
            if member_pgid == pgid and state not in {"X", "Z"}:
                return True
    return False
