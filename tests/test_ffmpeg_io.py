from __future__ import annotations

import errno
import gc
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from alpr_runner import ffmpeg_io
from alpr_runner.ffmpeg_io import (
    MAX_FRAME_BYTES,
    FfmpegFrameSource,
    FfmpegSupervisorError,
    FrameSpec,
    InheritedFdArgument,
)


def _python_child(source: str) -> list[str]:
    return [sys.executable, "-S", "-c", source]


def _source_line(
    function: object,
    text: str,
    *,
    occurrence: int = 0,
) -> int:
    lines, first_line = inspect.getsourcelines(function)
    matches = [
        first_line + index
        for index, line in enumerate(lines)
        if line.strip() == text
    ]
    if not 0 <= occurrence < len(matches):
        raise AssertionError(
            f"missing occurrence {occurrence} for {text!r}: {matches!r}"
        )
    return matches[occurrence]


def _source(
    script: str,
    *,
    spec: FrameSpec | None = None,
    startup_timeout: float = 1.0,
    idle_timeout: float = 1.0,
    stderr_limit: int = 1024,
    terminate_timeout: float = 0.2,
    kill_timeout: float = 0.2,
    private_source: str = "rtsp://viewer@192.0.2.44/live",
) -> FfmpegFrameSource:
    return FfmpegFrameSource(
        _python_child(script),
        spec or FrameSpec(1, 1, 1),
        source_kind="rtsp",
        source=private_source,
        startup_timeout=startup_timeout,
        idle_timeout=idle_timeout,
        stderr_limit=stderr_limit,
        terminate_timeout=terminate_timeout,
        kill_timeout=kill_timeout,
    )


def _private_command_source(private: str) -> FfmpegFrameSource:
    return FfmpegFrameSource(
        ["ffmpeg", "-i", private],
        FrameSpec(1, 1, 1),
        source_kind="rtsp",
        source=private,
        startup_timeout=1.0,
        idle_timeout=1.0,
        stderr_limit=1024,
        terminate_timeout=0.2,
        kill_timeout=0.2,
    )


def _sleeping_child() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _python_child("import time; time.sleep(10)"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
        close_fds=True,
    )


def _kill_if_running(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


class _RecordingWaitProcess:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        self.wait_timeouts: list[float] = []

    def wait(self, *, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        return self.return_code


class _InterruptingArgsProcess:
    """Delegate to a real child but interrupt its first args redaction."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        interrupt_first_redaction: bool = True,
        first_redaction_error: BaseException | None = None,
    ) -> None:
        self._process = process
        self._interrupt_first_redaction = interrupt_first_redaction
        self._first_redaction_error = (
            first_redaction_error or KeyboardInterrupt()
        )
        self.redaction_attempts = 0

    @property
    def args(self) -> object:
        return self._process.args

    @args.setter
    def args(self, value: object) -> None:
        self.redaction_attempts += 1
        if (
            self._interrupt_first_redaction
            and self.redaction_attempts == 1
        ):
            raise self._first_redaction_error
        self._process.args = value

    def __getattr__(self, name: str) -> object:
        return getattr(self._process, name)


class _InterruptingPidProcess:
    """Delegate to a real child while faulting PID capture."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        always_interrupt: bool,
    ) -> None:
        self._process = process
        self._always_interrupt = always_interrupt
        self.pid_reads = 0
        self.interruption = KeyboardInterrupt(
            "synthetic PID capture interrupt"
        )

    @property
    def args(self) -> object:
        return self._process.args

    @args.setter
    def args(self, value: object) -> None:
        self._process.args = value

    @property
    def pid(self) -> int:
        self.pid_reads += 1
        if self._always_interrupt or self.pid_reads == 1:
            raise self.interruption
        return self._process.pid

    def __getattr__(self, name: str) -> object:
        return getattr(self._process, name)


class FrameSpecTests(unittest.TestCase):
    def test_rgb24_contract_and_receipt_are_exact(self) -> None:
        spec = FrameSpec(160, 96, 6)

        self.assertEqual(spec.bytes_per_frame, 46_080)
        self.assertEqual(
            spec.to_receipt(),
            {
                "bytes_per_frame": 46_080,
                "fps": 6,
                "height": 96,
                "pixel_format": "rgb24",
                "width": 160,
            },
        )

    def test_dimensions_rate_format_and_byte_size_are_strictly_bounded(
        self,
    ) -> None:
        for field in ("width", "height", "fps"):
            values = {"width": 1, "height": 1, "fps": 1}
            values[field] = True
            with self.subTest(field=field, value=True):
                with self.assertRaises(TypeError):
                    FrameSpec(**values)

        for values in (
            {"width": 0, "height": 1, "fps": 1},
            {"width": 1, "height": 0, "fps": 1},
            {"width": 1, "height": 1, "fps": 0},
            {"width": 4097, "height": 1, "fps": 1},
            {"width": 1, "height": 2161, "fps": 1},
            {"width": 1, "height": 1, "fps": 241},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    FrameSpec(**values)

        with self.assertRaisesRegex(ValueError, "pixel_format"):
            FrameSpec(1, 1, 1, pixel_format="bgr24")
        with self.assertRaisesRegex(TypeError, "pixel_format"):
            FrameSpec(1, 1, 1, pixel_format=24)  # type: ignore[arg-type]
        self.assertLessEqual(
            FrameSpec(4096, 2160, 1).bytes_per_frame,
            MAX_FRAME_BYTES,
        )


class FfmpegFrameSourceTests(unittest.TestCase):
    def test_clean_eof_yields_exact_frames_and_deterministic_receipt(
        self,
    ) -> None:
        source = _source(
            "import os; os.write(1, b'abcdef')",
        )

        with source:
            self.assertEqual(list(source), [b"abc", b"def"])

        self.assertEqual(
            source.receipt(),
            {
                "cleanup_code": None,
                "exit_code": 0,
                "failure_code": None,
                "frame": {
                    "bytes_per_frame": 3,
                    "fps": 1,
                    "height": 1,
                    "pixel_format": "rgb24",
                    "width": 1,
                },
                "frames_delivered": 2,
                "process_group_closed": True,
                "process_reaped": True,
                "schema_version": 1,
                "source": {"kind": "rtsp"},
                "state": "ended",
                "stderr_bytes": 0,
                "stdout_bytes": 6,
                "termination": "none",
            },
        )
        self.assertIsNone(source.read_frame())
        self.assertIsNone(source._process)

    def test_truncated_frame_fails_closed_and_reaps(self) -> None:
        source = _source("import os; os.write(1, b'ab')")

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "truncated_frame")
        self.assertEqual(source.state, "failed")
        self.assertEqual(source.receipt()["stdout_bytes"], 2)
        self.assertIsNotNone(source._process)
        self.assertIsNotNone(source._process.returncode)

    def test_nonzero_exit_is_reported_after_bounded_stderr_is_drained(
        self,
    ) -> None:
        source = _source(
            "import os; os.write(2, b'ordinary diagnostic'); raise SystemExit(7)"
        )

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "process_exit_nonzero")
        receipt = raised.exception.receipt()
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(receipt["stderr_bytes"], len(b"ordinary diagnostic"))
        self.assertEqual(receipt["termination"], "none")

    def test_pipe_eof_before_process_exit_is_polled_without_misclassification(
        self,
    ) -> None:
        source = _source(
            "import os, time; "
            "os.close(1); os.close(2); "
            "time.sleep(0.02); "
            "raise SystemExit(0)"
        )

        self.assertIsNone(source.read_frame())
        self.assertEqual(source.state, "ended")
        self.assertEqual(source.receipt()["exit_code"], 0)
        self.assertEqual(source.receipt()["failure_code"], None)

    def test_startup_and_between_frame_timeouts_have_distinct_codes(
        self,
    ) -> None:
        startup = _source(
            "import time; time.sleep(2)",
            startup_timeout=0.05,
        )
        with self.assertRaises(FfmpegSupervisorError) as first:
            startup.read_frame()
        self.assertEqual(first.exception.code, "startup_timeout")
        self.assertIn(startup.receipt()["termination"], {"term", "kill"})
        self.assertIsNotNone(startup._process)
        self.assertIsNotNone(startup._process.returncode)

        idle = _source(
            "import os, time; os.write(1, b'abc'); time.sleep(2)",
            idle_timeout=0.05,
        )
        self.assertEqual(idle.read_frame(), b"abc")
        with self.assertRaises(FfmpegSupervisorError) as second:
            idle.read_frame()
        self.assertEqual(second.exception.code, "frame_idle_timeout")
        self.assertEqual(idle.receipt()["frames_delivered"], 1)

    def test_caller_processing_time_does_not_consume_idle_budget(self) -> None:
        source = _source(
            "import os, time; "
            "os.write(1, b'abcdef'); "
            "time.sleep(2)",
            idle_timeout=0.05,
        )

        self.assertEqual(source.read_frame(), b"abc")
        time.sleep(0.08)
        self.assertEqual(source.read_frame(), b"def")
        source.close()

    def test_partial_stdout_cannot_extend_absolute_startup_deadline(
        self,
    ) -> None:
        source = _source(
            "import os, time; "
            "os.write(1, b'a'); time.sleep(0.04); "
            "os.write(1, b'b'); time.sleep(0.04); "
            "os.write(1, b'c')",
            startup_timeout=0.06,
        )

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "startup_timeout")
        self.assertGreater(source.receipt()["stdout_bytes"], 0)
        self.assertLess(source.receipt()["stdout_bytes"], 3)

    def test_explicit_start_delay_rejects_even_a_queued_complete_frame(
        self,
    ) -> None:
        source = _source(
            "import os, time; os.write(1, b'abc'); time.sleep(2)",
            startup_timeout=0.05,
        )
        source.start()
        selector = source._selector
        self.assertIsNotNone(selector)
        ready = selector.select(1.0)  # type: ignore[union-attr]
        self.assertTrue(any(key.data == "stdout" for key, _mask in ready))
        assert source._started_at is not None
        delay = source._started_at + 0.07 - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "startup_timeout")
        self.assertEqual(source.receipt()["frames_delivered"], 0)
        self.assertEqual(source.receipt()["stdout_bytes"], 0)

    def test_stdout_progress_renews_between_frame_idle_deadline(self) -> None:
        source = _source(
            "import os, time; "
            "os.write(1, b'abcd'); time.sleep(0.03); "
            "os.write(1, b'e'); time.sleep(0.03); "
            "os.write(1, b'f')",
            idle_timeout=0.05,
        )

        self.assertEqual(source.read_frame(), b"abc")
        self.assertEqual(source.read_frame(), b"def")
        self.assertIsNone(source.read_frame())

    def test_stderr_cap_detects_one_excess_byte_without_unbounded_buffer(
        self,
    ) -> None:
        limit = 32
        source = _source(
            f"import os, time; os.write(2, b'x' * {limit + 1}); time.sleep(2)",
            stderr_limit=limit,
        )

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "stderr_limit_exceeded")
        self.assertEqual(source.receipt()["stderr_bytes"], limit + 1)
        self.assertLessEqual(len(source._stderr_buffer), limit)

    def test_stdout_buffer_never_reads_beyond_one_fixed_frame(self) -> None:
        frame = b"x" * 24
        source = _source(
            f"import os; os.write(1, {frame!r} * 3)",
            spec=FrameSpec(4, 2, 1),
        )

        self.assertEqual(source.read_frame(), frame)
        self.assertEqual(source.receipt()["stdout_bytes"], len(frame))
        self.assertEqual(len(source._frame_buffer), 0)
        self.assertEqual(source.read_frame(), frame)
        self.assertEqual(source.receipt()["stdout_bytes"], len(frame) * 2)
        self.assertEqual(source.read_frame(), frame)
        self.assertIsNone(source.read_frame())

    def test_private_source_stderr_and_host_paths_never_reach_public_data(
        self,
    ) -> None:
        private_userinfo = "viewer" + ":" + "fixture-value"
        secret_url = f"rtsp://{private_userinfo}@192.0.2.45/private"
        private_path = "/mnt/private-source/input.mp4"
        script = (
            "import os; "
            f"os.write(2, {f'{secret_url} {private_path}'.encode()!r}); "
            "raise SystemExit(9)"
        )
        source = _source(script, private_source=secret_url)

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        public = str(raised.exception) + json.dumps(
            raised.exception.receipt(), sort_keys=True
        )
        for forbidden in (
            "viewer",
            "fixture-value",
            "192.0.2.45",
            "/mnt/",
            "private-source",
        ):
            self.assertNotIn(forbidden, public)
        self.assertEqual(
            str(raised.exception),
            "ffmpeg supervisor failed: process_exit_nonzero",
        )

    def test_sigterm_is_escalated_to_sigkill_and_child_is_reaped(self) -> None:
        source = _source(
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "os.write(1, b'abc'); "
            "time.sleep(10)",
            terminate_timeout=0.05,
            kill_timeout=1.0,
        )
        self.assertEqual(source.read_frame(), b"abc")
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")

        source.close()

        self.assertEqual(source.receipt()["termination"], "kill")
        self.assertEqual(source.receipt()["exit_code"], -signal.SIGKILL)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertEqual(source.state, "closed")
        self.assertIsNone(source._process)
        self.assertEqual(process.poll(), -signal.SIGKILL)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and hasattr(os, "fork"),
        "Linux process-group semantics required",
    )
    def test_cleanup_reaches_descendant_after_group_leader_exits(self) -> None:
        script = """
import os
import signal
import time

ready_read, ready_write = os.pipe()
descendant = os.fork()
if descendant == 0:
    os.close(ready_read)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b"1")
    os.close(ready_write)
    time.sleep(10)
    os._exit(0)

os.close(ready_write)
os.read(ready_read, 1)
os.close(ready_read)
os.write(1, f"{descendant:024d}".encode("ascii"))
os._exit(0)
"""
        source = _source(
            script,
            spec=FrameSpec(8, 1, 1),
            idle_timeout=0.05,
            terminate_timeout=0.05,
            kill_timeout=1.0,
        )
        frame = source.read_frame()
        self.assertIsNotNone(frame)
        descendant_pid = int(frame.decode("ascii"))  # type: ignore[union-attr]

        with self.assertRaises(FfmpegSupervisorError) as raised:
            source.read_frame()

        self.assertEqual(raised.exception.code, "frame_idle_timeout")
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertEqual(source.receipt()["termination"], "kill")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status_path = Path(f"/proc/{descendant_pid}/stat")
            try:
                process_state = status_path.read_text(
                    encoding="ascii"
                ).split()[2]
            except FileNotFoundError:
                break
            if process_state in {"X", "Z"}:
                break
            time.sleep(0.01)
        else:
            self.fail("descendant remained live after process-group escalation")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and hasattr(os, "fork"),
        "Linux process-group semantics required",
    )
    def test_clean_eof_closes_descendant_that_closed_inherited_pipes(
        self,
    ) -> None:
        script = """
import os
import signal
import time

ready_read, ready_write = os.pipe()
descendant = os.fork()
if descendant == 0:
    os.close(ready_read)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b"1")
    os.close(ready_write)
    for descriptor in (0, 1, 2):
        try:
            os.close(descriptor)
        except OSError:
            pass
    time.sleep(10)
    os._exit(0)

os.close(ready_write)
os.read(ready_read, 1)
os.close(ready_read)
os.write(1, f"{descendant:024d}".encode("ascii"))
os._exit(0)
"""
        source = _source(
            script,
            spec=FrameSpec(8, 1, 1),
            terminate_timeout=0.05,
            kill_timeout=1.0,
        )
        frame = source.read_frame()
        self.assertIsNotNone(frame)
        descendant_pid = int(frame.decode("ascii"))  # type: ignore[union-attr]

        self.assertIsNone(source.read_frame())

        self.assertEqual(source.state, "ended")
        self.assertEqual(source.receipt()["termination"], "kill")
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status_path = Path(f"/proc/{descendant_pid}/stat")
            try:
                process_state = status_path.read_text(
                    encoding="ascii"
                ).split()[2]
            except FileNotFoundError:
                break
            if process_state in {"X", "Z"}:
                break
            time.sleep(0.01)
        else:
            self.fail("closed-pipe descendant survived clean EOF cleanup")

    def test_cleanup_timeout_is_reported_and_a_later_close_retries_reap(
        self,
    ) -> None:
        source = _source(
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "os.write(1, b'abc'); "
            "time.sleep(10)",
            terminate_timeout=0.05,
            kill_timeout=0.05,
        )
        self.assertEqual(source.read_frame(), b"abc")
        process = source._process
        self.assertIsNotNone(process)
        timeout = subprocess.TimeoutExpired(("<redacted>",), 0.05)
        with (
            patch.object(process, "wait", side_effect=[timeout, timeout]),
            self.assertRaises(FfmpegSupervisorError) as raised,
        ):
            source.close()

        self.assertEqual(raised.exception.code, "reap_timeout")
        self.assertEqual(source.state, "failed")
        self.assertEqual(source.receipt()["cleanup_code"], "reap_timeout")
        self.assertIs(source.receipt()["process_reaped"], False)

        source.close()
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertEqual(source.state, "failed")

    def test_context_manager_surfaces_body_and_cleanup_failures_together(
        self,
    ) -> None:
        source = _source(
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "os.write(1, b'abc'); "
            "time.sleep(10)",
            terminate_timeout=0.05,
            kill_timeout=0.05,
        )
        self.assertEqual(source.read_frame(), b"abc")
        process = source._process
        self.assertIsNotNone(process)
        timeout = subprocess.TimeoutExpired(("<redacted>",), 0.05)

        with patch.object(
            process,
            "wait",
            side_effect=[timeout, timeout],
        ):
            with self.assertRaises(BaseExceptionGroup) as raised:
                with source:
                    raise ValueError("synthetic body failure")

        errors = raised.exception.exceptions
        self.assertEqual(len(errors), 2)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIsInstance(errors[1], FfmpegSupervisorError)
        self.assertEqual(errors[1].code, "reap_timeout")  # type: ignore[union-attr]
        self.assertIs(source.receipt()["process_reaped"], False)

        source.close()
        self.assertIs(source.receipt()["process_reaped"], True)

    def test_context_manager_preserves_nonstandard_cleanup_failure(
        self,
    ) -> None:
        source = _source("raise SystemExit(0)")
        body_failure = ValueError("synthetic body failure")
        cleanup_failure = KeyboardInterrupt("synthetic cleanup interrupt")

        with (
            patch.object(source, "close", side_effect=cleanup_failure),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            source.__exit__(
                type(body_failure),
                body_failure,
                None,
            )

        self.assertEqual(
            raised.exception.exceptions,
            (body_failure, cleanup_failure),
        )

        with (
            patch.object(source, "close", side_effect=cleanup_failure),
            self.assertRaises(KeyboardInterrupt) as standalone,
        ):
            source.__exit__(None, None, None)
        self.assertIs(standalone.exception, cleanup_failure)

    def test_detected_failure_preserves_interrupted_cleanup_and_fails_closed(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.52/private"
        source = _source(
            "import time; time.sleep(10)",
            private_source=private,
        )
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        cleanup_failure = KeyboardInterrupt("synthetic cleanup interrupt")

        with (
            patch.object(
                source,
                "_terminate_and_reap",
                side_effect=cleanup_failure,
            ),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            source._raise_failure("startup_timeout")

        primary, preserved_cleanup = raised.exception.exceptions
        self.assertIsInstance(primary, FfmpegSupervisorError)
        self.assertEqual(primary.code, "startup_timeout")  # type: ignore[union-attr]
        self.assertIs(preserved_cleanup, cleanup_failure)
        self.assertEqual(source.state, "failed")
        self.assertEqual(source.receipt()["failure_code"], "startup_timeout")
        self.assertEqual(source.receipt()["cleanup_code"], "cleanup_failed")
        self.assertIsNone(process.poll())
        public = str(raised.exception) + json.dumps(
            source.receipt(),
            sort_keys=True,
        )
        self.assertNotIn(private, public)
        self.assertNotIn("fixture-value", public)

        source.close()
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertIsNotNone(process.poll())

    def test_interrupted_close_retries_cleanup_before_propagating(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.53/private"
        source = _source(
            "import time; time.sleep(10)",
            private_source=private,
        )
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        original_cleanup = source._terminate_and_reap
        cleanup_failure = KeyboardInterrupt("synthetic cleanup interrupt")
        calls = 0

        def interrupt_once() -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise cleanup_failure
            return original_cleanup()

        with (
            patch.object(
                source,
                "_terminate_and_reap",
                side_effect=interrupt_once,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            source.close()

        receipt = source.receipt()
        self.assertIs(raised.exception, cleanup_failure)
        self.assertEqual(calls, 2)
        self.assertEqual(source.state, "failed")
        self.assertEqual(receipt["failure_code"], "cleanup_failed")
        self.assertIs(receipt["process_reaped"], True)
        self.assertIs(receipt["process_group_closed"], True)
        self.assertIsNone(source._process)
        self.assertIsNotNone(process.poll())
        public = str(raised.exception) + json.dumps(
            receipt,
            sort_keys=True,
        )
        self.assertNotIn(private, public)
        self.assertNotIn("fixture-value", public)

        source.close()
        self.assertIsNotNone(process.poll())

    def test_interrupted_eof_finalization_fails_closed_after_retry(
        self,
    ) -> None:
        source = _source("import os; os.write(1, b'abc')")
        self.assertEqual(source.read_frame(), b"abc")
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        original_cleanup = source._terminate_and_reap
        cleanup_failure = KeyboardInterrupt("synthetic EOF cleanup interrupt")
        calls = 0

        def interrupt_once() -> str | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise cleanup_failure
            return original_cleanup()

        with (
            patch.object(
                source,
                "_terminate_and_reap",
                side_effect=interrupt_once,
            ),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            source.read_frame()

        receipt = source.receipt()
        self.assertIs(raised.exception, cleanup_failure)
        self.assertEqual(calls, 2)
        self.assertEqual(source.state, "failed")
        self.assertEqual(receipt["failure_code"], "cleanup_failed")
        self.assertIs(receipt["process_reaped"], True)
        self.assertIs(receipt["process_group_closed"], True)
        self.assertIsNone(source._process)
        self.assertIsNotNone(process.poll())
        with self.assertRaises(FfmpegSupervisorError) as failed_closed:
            source.read_frame()
        self.assertEqual(failed_closed.exception.code, "cleanup_failed")

        source.close()
        self.assertIsNotNone(process.poll())

    def test_interrupted_descriptor_cleanup_retains_handle_for_retry(
        self,
    ) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        self.assertIsNotNone(process.stdout)
        self.assertIsNotNone(process.stderr)
        cleanup_failure = KeyboardInterrupt("synthetic descriptor interrupt")
        original_failure = RuntimeError("synthetic primary failure")

        with (
            patch.object(
                source,
                "_close_io",
                side_effect=cleanup_failure,
            ),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            source._abort_started_process(original_failure)

        self.assertEqual(
            raised.exception.exceptions,
            (original_failure, cleanup_failure),
        )
        self.assertEqual(source.state, "failed")
        self.assertIs(source._process, process)
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertFalse(process.stdout.closed)  # type: ignore[union-attr]
        self.assertFalse(process.stderr.closed)  # type: ignore[union-attr]

        source.close()
        self.assertTrue(process.stdout.closed)  # type: ignore[union-attr]
        self.assertTrue(process.stderr.closed)  # type: ignore[union-attr]
        self.assertIsNone(source._process)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertIsNotNone(process.poll())

    def test_interrupted_selector_close_retains_handle_for_retry(
        self,
    ) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        selector = source._selector
        self.assertIsNotNone(process)
        self.assertIsNotNone(selector)
        if process is None or selector is None:
            self.fail("started source has incomplete cleanup ownership")
        self.assertEqual(len(selector.get_map()), 2)
        cleanup_failure = KeyboardInterrupt("synthetic selector interrupt")
        original_failure = RuntimeError("synthetic primary failure")

        with (
            patch.object(
                selector,
                "close",
                side_effect=cleanup_failure,
            ),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            source._abort_started_process(original_failure)

        self.assertEqual(
            raised.exception.exceptions,
            (original_failure, cleanup_failure),
        )
        self.assertEqual(source.state, "failed")
        self.assertIs(source._process, process)
        self.assertIs(source._selector, selector)
        self.assertEqual(len(selector.get_map()), 2)
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)

        source.close()
        self.assertIsNone(source._selector)
        self.assertIsNone(selector.get_map())
        self.assertIsNone(source._process)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertIsNotNone(process.poll())

    def test_pipe_close_error_attempts_sibling_and_retains_owner(
        self,
    ) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if (
            process is None
            or process.stdout is None
            or process.stderr is None
        ):
            self.fail("started source has incomplete pipe ownership")
        stdout = process.stdout
        stderr = process.stderr
        cleanup_failure = OSError("synthetic stdout close failure")
        original_failure = RuntimeError("synthetic primary failure")
        original_stderr_close = stderr.close

        with (
            patch.object(
                stdout,
                "close",
                side_effect=cleanup_failure,
            ),
            patch.object(
                stderr,
                "close",
                wraps=original_stderr_close,
            ) as stderr_close,
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            source._abort_started_process(original_failure)

        self.assertEqual(
            raised.exception.exceptions,
            (original_failure, cleanup_failure),
        )
        stderr_close.assert_called_once_with()
        self.assertEqual(source.state, "failed")
        self.assertIs(source._process, process)
        self.assertFalse(stdout.closed)
        self.assertTrue(stderr.closed)
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertEqual(
            source.receipt()["cleanup_code"],
            "cleanup_failed",
        )

        source.close()
        self.assertTrue(stdout.closed)
        self.assertTrue(stderr.closed)
        self.assertIsNone(source._process)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertIsNotNone(process.poll())

    def test_descriptor_retry_never_resignals_confirmed_process_group(
        self,
    ) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None or process.stdout is None:
            self.fail("started source has no stdout ownership")
        stdout = process.stdout
        original_close = stdout.close
        original_killpg = os.killpg
        cleanup_failure = KeyboardInterrupt(
            "synthetic stdout close interrupt"
        )
        close_calls = 0

        def interrupt_close_once() -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise cleanup_failure
            original_close()

        with (
            patch.object(
                stdout,
                "close",
                side_effect=interrupt_close_once,
            ),
            patch(
                "alpr_runner.ffmpeg_io.os.killpg",
                wraps=original_killpg,
            ) as killpg,
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            source.close()

        self.assertIs(raised.exception, cleanup_failure)
        term_calls = [
            call
            for call in killpg.call_args_list
            if len(call.args) == 2 and call.args[1] == signal.SIGTERM
        ]
        self.assertEqual(len(term_calls), 1)
        self.assertEqual(close_calls, 2)
        self.assertEqual(source.state, "failed")
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertIsNone(source._process)
        self.assertTrue(stdout.closed)
        self.assertIsNotNone(process.poll())

    def test_quiescence_publication_prevents_process_group_resignal(
        self,
    ) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        original_killpg = os.killpg
        target = FfmpegFrameSource._terminate_and_reap
        target_code = target.__code__
        target_line = _source_line(
            target,
            'if group_status == "failed":',
        )
        interruption = KeyboardInterrupt(
            "synthetic post-quiescence interrupt"
        )

        def interrupt_after_quiescence(
            frame: object,
            event: str,
            _argument: object,
        ) -> object:
            if (
                getattr(frame, "f_code", None) is target_code
                and event == "line"
                and getattr(frame, "f_lineno", None) == target_line
            ):
                sys.settrace(None)
                raise interruption
            return interrupt_after_quiescence

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.os.killpg",
                    wraps=original_killpg,
                ) as killpg,
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                sys.settrace(interrupt_after_quiescence)
                source.close()
        finally:
            sys.settrace(None)

        self.assertIs(raised.exception, interruption)
        term_calls = [
            call
            for call in killpg.call_args_list
            if len(call.args) == 2 and call.args[1] == signal.SIGTERM
        ]
        self.assertEqual(len(term_calls), 1)
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertIsNone(source._process)
        self.assertIsNotNone(process.poll())

    def test_poll_error_cannot_prevent_known_group_cleanup(self) -> None:
        source = _source("import time; time.sleep(10)")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")

        with patch.object(
            process,
            "poll",
            side_effect=OSError("synthetic persistent poll failure"),
        ):
            source.close()

        receipt = source.receipt()
        self.assertEqual(source.state, "closed")
        self.assertIs(receipt["process_reaped"], True)
        self.assertIs(receipt["process_group_closed"], True)
        self.assertIsNone(receipt["failure_code"])
        self.assertIsNone(receipt["cleanup_code"])
        self.assertEqual(receipt["termination"], "term")
        self.assertIsNone(source._process)
        self.assertIsNotNone(process.returncode)

    def test_confirmed_closed_group_retry_only_reaps_leader(
        self,
    ) -> None:
        source = _source(
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(10)",
            terminate_timeout=0.05,
            kill_timeout=1.0,
        )
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        original_wait = process.wait
        wait_calls = 0

        def fail_first_two_waits(*, timeout: float) -> int:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls <= 2:
                raise OSError("synthetic reap failure")
            return original_wait(timeout=timeout)

        with (
            patch.object(
                process,
                "wait",
                side_effect=fail_first_two_waits,
            ),
            self.assertRaises(FfmpegSupervisorError) as raised,
        ):
            source.close()

        self.assertEqual(raised.exception.code, "cleanup_failed")
        self.assertIs(source.receipt()["process_reaped"], False)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertEqual(source.receipt()["termination"], "kill")
        self.assertIs(source._process, process)

        original_killpg = os.killpg
        with patch(
            "alpr_runner.ffmpeg_io.os.killpg",
            wraps=original_killpg,
        ) as killpg:
            source.close()

        term_calls = [
            call
            for call in killpg.call_args_list
            if len(call.args) == 2 and call.args[1] == signal.SIGTERM
        ]
        self.assertEqual(term_calls, [])
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertIsNone(source.receipt()["cleanup_code"])
        self.assertIsNone(source._process)
        self.assertIsNotNone(process.returncode)

    def test_read_poll_error_fails_safe_and_reaps_known_group(self) -> None:
        source = _source(
            "import os, time; os.write(2, b'x'); time.sleep(10)"
        )
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")

        with (
            patch.object(
                process,
                "poll",
                side_effect=OSError("synthetic persistent poll failure"),
            ),
            self.assertRaises(FfmpegSupervisorError) as raised,
        ):
            source.read_frame()

        receipt = source.receipt()
        self.assertEqual(raised.exception.code, "io_failed")
        self.assertEqual(source.state, "failed")
        self.assertIs(receipt["process_reaped"], True)
        self.assertIs(receipt["process_group_closed"], True)
        self.assertIsNone(receipt["cleanup_code"])
        self.assertNotEqual(receipt["termination"], "none")
        self.assertIsNotNone(process.returncode)
        self.assertNotIn(
            "synthetic persistent poll failure",
            str(raised.exception),
        )

        source.close()
        self.assertIsNone(source._process)

    def test_eof_finalization_poll_error_is_source_safe(self) -> None:
        source = _source("pass")
        source.start()
        process = source._process
        self.assertIsNotNone(process)
        if process is None:
            self.fail("started source has no process")
        process.wait(timeout=1)
        source._stdout_eof = True
        source._stderr_eof = True
        private_diagnostic = "synthetic private poll diagnostic"

        with (
            patch.object(
                process,
                "poll",
                side_effect=[
                    0,
                    OSError(private_diagnostic),
                    0,
                ],
            ),
            self.assertRaises(FfmpegSupervisorError) as raised,
        ):
            source.read_frame()

        self.assertEqual(raised.exception.code, "io_failed")
        self.assertEqual(source.state, "failed")
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertNotIn(private_diagnostic, str(raised.exception))

        source.close()
        self.assertIsNone(source._process)

    def test_new_source_close_scrubs_private_inputs_before_terminal_state(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.57/private"
        source = _private_command_source(private)
        cleanup_failure = KeyboardInterrupt("synthetic scrub interrupt")
        target_code = FfmpegFrameSource._close_once.__code__
        source_lines, first_line = inspect.getsourcelines(
            FfmpegFrameSource._close_once
        )
        state_assignment_line = next(
            first_line + index
            for index, line in enumerate(source_lines)
            if line.strip() == 'self._state = "closed"'
        )

        def interrupt_before_terminal_state(
            frame: object,
            event: str,
            _argument: object,
        ) -> object:
            if (
                getattr(frame, "f_code", None) is target_code
                and event == "line"
                and getattr(frame, "f_lineno", None)
                == state_assignment_line
            ):
                sys.settrace(None)
                raise cleanup_failure
            return interrupt_before_terminal_state

        try:
            with self.assertRaises(KeyboardInterrupt) as raised:
                sys.settrace(interrupt_before_terminal_state)
                source.close()
        finally:
            sys.settrace(None)

        self.assertIs(raised.exception, cleanup_failure)
        self.assertEqual(source.state, "new")
        self.assertEqual(source._command, ())
        self.assertIsNone(source._source)
        self.assertNotIn(
            private,
            json.dumps(source.receipt(), sort_keys=True),
        )

        source.close()
        self.assertEqual(source.state, "closed")

    def test_sigkill_requires_bounded_group_quiescence_confirmation(
        self,
    ) -> None:
        source = _source(
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "os.write(1, b'abc'); "
            "time.sleep(10)",
            terminate_timeout=0.05,
            kill_timeout=0.05,
        )
        self.assertEqual(source.read_frame(), b"abc")

        with (
            patch.object(
                source,
                "_wait_for_group_quiescence",
                return_value="running",
            ),
            self.assertRaises(FfmpegSupervisorError) as raised,
        ):
            source.close()

        self.assertEqual(raised.exception.code, "reap_timeout")
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], False)
        self.assertEqual(source.receipt()["cleanup_code"], "reap_timeout")

        source.close()
        self.assertIs(source.receipt()["process_group_closed"], True)

    def test_sigkill_uses_one_deadline_for_reap_and_group_confirmation(
        self,
    ) -> None:
        source = FfmpegFrameSource(
            ["ffmpeg"],
            FrameSpec(1, 1, 1),
            source_kind="synthetic",
            kill_timeout=1.0,
        )
        source._pgid = 73_101
        process = _RecordingWaitProcess(-signal.SIGKILL)

        with (
            patch.object(source, "_send_sigkill", return_value=None),
            patch.object(
                source,
                "_wait_for_group_quiescence",
                return_value="quiescent",
            ) as group_wait,
            patch(
                "alpr_runner.ffmpeg_io.time.monotonic",
                side_effect=[100.0, 100.25, 100.70],
            ),
        ):
            result = source._kill_and_reap(process)  # type: ignore[arg-type]

        self.assertIsNone(result)
        self.assertEqual(process.wait_timeouts, [0.75])
        group_wait.assert_called_once()
        self.assertAlmostEqual(group_wait.call_args.args[0], 0.30)
        self.assertIs(source.receipt()["process_reaped"], True)
        self.assertIs(source.receipt()["process_group_closed"], True)

    def test_stop_event_requests_bounded_close(self) -> None:
        stop = threading.Event()
        source = _source("import time; time.sleep(10)")
        source.start()
        stop.set()

        self.assertIsNone(source.read_frame(stop))
        self.assertEqual(source.state, "closed")
        self.assertIn(source.receipt()["termination"], {"term", "kill"})

    def test_spawn_contract_uses_closed_stdin_session_and_file_descriptors(
        self,
    ) -> None:
        userinfo = "reader" + ":" + "fixture-value"
        private = f"rtsp://{userinfo}@192.0.2.46/live"
        source = _source(
            "raise SystemExit(0)",
            private_source=private,
        )
        with patch(
            "alpr_runner.ffmpeg_io.subprocess.Popen",
            side_effect=OSError(f"cannot open {private} /mnt/source/input"),
        ) as popen:
            with self.assertRaises(FfmpegSupervisorError) as raised:
                source.start()

        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(kwargs["bufsize"], 0)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIs(kwargs["close_fds"], True)
        self.assertEqual(kwargs["pass_fds"], ())
        self.assertEqual(raised.exception.code, "spawn_failed")
        self.assertNotIn(private, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(source.state, "failed")

    def test_descriptor_bound_argument_is_inherited_without_entering_receipt(
        self,
    ) -> None:
        read_descriptor, write_descriptor = os.pipe()
        try:
            os.write(write_descriptor, b"abc")
            os.close(write_descriptor)
            write_descriptor = -1
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys; os.write(1, os.read(int(sys.argv[1]), 3))"
                )
                + [InheritedFdArgument(read_descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )

            self.assertEqual(source.read_frame(), b"abc")
            self.assertIsNone(source.read_frame())
            receipt = source.receipt()
            self.assertEqual(receipt["source"], {"kind": "pipe"})
            self.assertNotIn("pass_fds", receipt)
        finally:
            os.close(read_descriptor)
            if write_descriptor >= 0:
                os.close(write_descriptor)

    def test_successful_spawn_redacts_retained_popen_arguments(self) -> None:
        private = "rtsp://viewer@192.0.2.47/private"
        source = _source(
            "import os; os.write(1, b'abc')",
            private_source=private,
        )

        self.assertEqual(source.read_frame(), b"abc")

        self.assertIsNotNone(source._process)
        self.assertEqual(
            source._process.args,
            ("<redacted-ffmpeg-command>",),
        )
        self.assertEqual(source._command, ())
        self.assertIsNone(source._source)
        self.assertNotIn(
            private,
            json.dumps(source.receipt(), sort_keys=True),
        )
        self.assertIsNone(source.read_frame())

    def test_interrupt_after_spawn_reaps_the_adopted_process_group(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.48/private"
        source = _private_command_source(private)
        process = _sleeping_child()
        proxy = _InterruptingArgsProcess(process)

        def return_spawned_process(
            command: object,
            **_kwargs: object,
        ) -> _InterruptingArgsProcess:
            process.args = command
            return proxy

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.subprocess.Popen",
                    side_effect=return_spawned_process,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                source.start()

            receipt = source.receipt()
            self.assertEqual(proxy.redaction_attempts, 2)
            self.assertEqual(source.state, "failed")
            self.assertEqual(receipt["failure_code"], "io_setup_failed")
            self.assertIsNone(receipt["cleanup_code"])
            self.assertIs(receipt["process_reaped"], True)
            self.assertIs(receipt["process_group_closed"], True)
            self.assertIn(receipt["termination"], {"term", "kill"})
            self.assertIsNotNone(process.poll())
            self.assertEqual(source._command, ())
            self.assertIsNone(source._source)
            self.assertIsNone(source._process)
            self.assertEqual(
                proxy.args,
                ("<redacted-ffmpeg-command>",),
            )
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(proxy.args))
            self.assertNotIn(private, json.dumps(receipt, sort_keys=True))

            source.close()
            self.assertIsNotNone(process.poll())
        finally:
            _kill_if_running(process)

    def test_one_shot_pid_interrupt_preserves_original_and_reaps_group(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.58/private"
        source = _private_command_source(private)
        process = _sleeping_child()
        proxy = _InterruptingPidProcess(
            process,
            always_interrupt=False,
        )

        def return_spawned_process(
            command: object,
            **_kwargs: object,
        ) -> _InterruptingPidProcess:
            process.args = command
            return proxy

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.subprocess.Popen",
                    side_effect=return_spawned_process,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                source.start()

            receipt = source.receipt()
            self.assertIs(raised.exception, proxy.interruption)
            self.assertEqual(proxy.pid_reads, 2)
            self.assertEqual(source.state, "failed")
            self.assertIs(receipt["process_reaped"], True)
            self.assertIs(receipt["process_group_closed"], True)
            self.assertIsNone(receipt["cleanup_code"])
            self.assertIsNone(source._process)
            self.assertEqual(source._command, ())
            self.assertIsNone(source._source)
            self.assertIsNotNone(process.poll())
            self.assertNotIn(private, repr(process.args))
            self.assertNotIn(
                private,
                json.dumps(receipt, sort_keys=True),
            )
        finally:
            _kill_if_running(process)

    def test_persistent_pid_interrupt_reaps_leader_without_group_claim(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.59/private"
        source = _private_command_source(private)
        process = _sleeping_child()
        proxy = _InterruptingPidProcess(
            process,
            always_interrupt=True,
        )

        def return_spawned_process(
            command: object,
            **_kwargs: object,
        ) -> _InterruptingPidProcess:
            process.args = command
            return proxy

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.subprocess.Popen",
                    side_effect=return_spawned_process,
                ),
                self.assertRaises(BaseExceptionGroup) as raised,
            ):
                source.start()

            primary, cleanup = raised.exception.exceptions
            receipt = source.receipt()
            self.assertIs(primary, proxy.interruption)
            self.assertIsInstance(cleanup, FfmpegSupervisorError)
            self.assertEqual(cleanup.code, "cleanup_failed")  # type: ignore[union-attr]
            self.assertGreaterEqual(proxy.pid_reads, 2)
            self.assertEqual(source.state, "failed")
            self.assertIs(receipt["process_reaped"], True)
            self.assertIs(receipt["process_group_closed"], False)
            self.assertEqual(
                receipt["cleanup_code"],
                "cleanup_failed",
            )
            self.assertIs(source._process, proxy)
            self.assertEqual(source._command, ())
            self.assertIsNone(source._source)
            self.assertIsNotNone(process.poll())
            public = str(raised.exception) + json.dumps(
                receipt,
                sort_keys=True,
            )
            self.assertNotIn(private, public)
            self.assertNotIn("fixture-value", public)
        finally:
            _kill_if_running(process)

    def test_pending_sigint_is_delivered_only_after_process_adoption(
        self,
    ) -> None:
        if not hasattr(signal, "pthread_kill"):
            self.skipTest("pthread_kill is unavailable")
        original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        if signal.SIGINT in original_mask:
            self.skipTest("SIGINT was already blocked by the test host")

        private = "rtsp://viewer:fixture-value@192.0.2.49/private"
        source = _private_command_source(private)
        process = _sleeping_child()
        proxy = _InterruptingArgsProcess(
            process,
            interrupt_first_redaction=False,
        )
        observed_mask: set[signal.Signals] | None = None

        def spawn_and_queue_sigint(
            command: object,
            **_kwargs: object,
        ) -> _InterruptingArgsProcess:
            nonlocal observed_mask
            process.args = command
            observed_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(),
            )
            signal.pthread_kill(threading.get_ident(), signal.SIGINT)
            return proxy

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.subprocess.Popen",
                    side_effect=spawn_and_queue_sigint,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                source.start()

            restored_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(),
            )
            receipt = source.receipt()
            self.assertIsNotNone(observed_mask)
            self.assertIn(signal.SIGINT, observed_mask or set())
            self.assertEqual(restored_mask, original_mask)
            self.assertEqual(proxy.redaction_attempts, 2)
            self.assertEqual(
                proxy.args,
                ("<redacted-ffmpeg-command>",),
            )
            self.assertIsNone(source._process)
            self.assertIs(receipt["process_reaped"], True)
            self.assertIs(receipt["process_group_closed"], True)
            self.assertIsNotNone(process.poll())
            self.assertNotIn(private, repr(proxy.args))
            self.assertNotIn(private, json.dumps(receipt, sort_keys=True))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
            _kill_if_running(process)

    def test_signal_mask_is_restored_if_blocking_call_raises_after_mutation(
        self,
    ) -> None:
        original_call = signal.pthread_sigmask
        original_mask = original_call(signal.SIG_BLOCK, set())
        calls = 0

        def mutate_then_fail(
            how: signal.Sigmasks,
            mask: set[signal.Signals],
        ) -> set[signal.Signals]:
            nonlocal calls
            calls += 1
            result = original_call(how, mask)
            if calls == 2:
                raise RuntimeError("synthetic post-mutation failure")
            return result

        source = _source("raise SystemExit(0)")
        restored_mask: set[signal.Signals] | None = None
        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.signal.pthread_sigmask",
                    side_effect=mutate_then_fail,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()
            restored_mask = original_call(signal.SIG_BLOCK, set())
        finally:
            original_call(signal.SIG_SETMASK, original_mask)

        self.assertEqual(calls, 3)
        self.assertEqual(restored_mask, original_mask)
        self.assertEqual(raised.exception.code, "spawn_failed")
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(source.state, "failed")

    def test_interrupted_signal_mask_restoration_is_retried_exactly(
        self,
    ) -> None:
        cases = (
            (
                FfmpegFrameSource._start_once,
                "self._restore_start_signal_mask()",
            ),
            (
                FfmpegFrameSource._restore_start_signal_mask,
                "self._start_signal_mask = None",
            ),
        )
        original_call = signal.pthread_sigmask

        for function, source_text in cases:
            with self.subTest(boundary=source_text):
                original_mask = original_call(signal.SIG_BLOCK, set())
                source = _source("import time; time.sleep(10)")
                target_code = function.__code__
                target_line = _source_line(function, source_text)
                interruption = SystemExit(
                    f"synthetic mask interrupt: {source_text}"
                )
                restored_mask: set[signal.Signals] | None = None

                def interrupt_once(
                    frame: object,
                    event: str,
                    _argument: object,
                ) -> object:
                    if (
                        getattr(frame, "f_code", None) is target_code
                        and event == "line"
                        and getattr(frame, "f_lineno", None) == target_line
                    ):
                        sys.settrace(None)
                        raise interruption
                    return interrupt_once

                try:
                    with self.assertRaises(SystemExit) as raised:
                        sys.settrace(interrupt_once)
                        source.start()
                    restored_mask = original_call(
                        signal.SIG_BLOCK,
                        set(),
                    )
                finally:
                    sys.settrace(None)
                    original_call(signal.SIG_SETMASK, original_mask)

                self.assertIs(raised.exception, interruption)
                self.assertEqual(restored_mask, original_mask)
                self.assertIsNone(source._start_signal_mask)
                self.assertEqual(source.state, "failed")
                self.assertIs(
                    source.receipt()["process_reaped"],
                    True,
                )
                self.assertIs(
                    source.receipt()["process_group_closed"],
                    True,
                )
                self.assertIsNone(source._process)

    def test_interrupt_on_successful_start_return_cannot_orphan_child(
        self,
    ) -> None:
        injected_errors = (
            ("keyboard", KeyboardInterrupt()),
            (
                "supervisor",
                FfmpegSupervisorError(
                    "io_setup_failed",
                    {"source": {"kind": "rtsp"}},
                ),
            ),
        )
        target_code = FfmpegFrameSource._start_once.__code__

        for suffix, injected_error in injected_errors:
            with self.subTest(error=suffix):
                private = (
                    "rtsp://viewer:fixture-value@192.0.2.51/"
                    f"private-{suffix}"
                )
                source = _private_command_source(private)
                process = _sleeping_child()

                def return_spawned_process(
                    command: object,
                    **_kwargs: object,
                ) -> subprocess.Popen[bytes]:
                    process.args = command
                    return process

                def interrupt_return(
                    frame: object,
                    event: str,
                    _argument: object,
                ) -> object:
                    if (
                        getattr(frame, "f_code", None) is target_code
                        and event == "return"
                    ):
                        sys.settrace(None)
                        raise injected_error
                    return interrupt_return

                try:
                    with self.assertRaises(type(injected_error)) as raised:
                        with patch(
                            "alpr_runner.ffmpeg_io.subprocess.Popen",
                            side_effect=return_spawned_process,
                        ):
                            sys.settrace(interrupt_return)
                            source.start()

                    receipt = source.receipt()
                    self.assertIs(raised.exception, injected_error)
                    self.assertEqual(source.state, "failed")
                    self.assertIs(receipt["process_reaped"], True)
                    self.assertIs(receipt["process_group_closed"], True)
                    self.assertIsNone(source._process)
                    self.assertIsNotNone(process.poll())
                    self.assertNotIn(
                        private,
                        json.dumps(receipt, sort_keys=True),
                    )
                    self.assertNotIn(private, repr(process.args))
                finally:
                    sys.settrace(None)
                    _kill_if_running(process)

    def test_redaction_exception_is_sanitized_after_bounded_cleanup(
        self,
    ) -> None:
        private = "rtsp://viewer:fixture-value@192.0.2.50/private"
        source = _private_command_source(private)
        process = _sleeping_child()
        proxy = _InterruptingArgsProcess(
            process,
            first_redaction_error=RuntimeError(
                f"cannot redact {private}"
            ),
        )

        def return_spawned_process(
            command: object,
            **_kwargs: object,
        ) -> _InterruptingArgsProcess:
            process.args = command
            return proxy

        try:
            with (
                patch(
                    "alpr_runner.ffmpeg_io.subprocess.Popen",
                    side_effect=return_spawned_process,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            receipt = raised.exception.receipt()
            self.assertEqual(raised.exception.code, "io_setup_failed")
            self.assertIsNone(raised.exception.__context__)
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(proxy.redaction_attempts, 2)
            self.assertEqual(
                proxy.args,
                ("<redacted-ffmpeg-command>",),
            )
            self.assertIsNone(source._process)
            self.assertIs(receipt["process_reaped"], True)
            self.assertIs(receipt["process_group_closed"], True)
            self.assertIsNotNone(process.poll())
            public = (
                str(raised.exception)
                + json.dumps(receipt, sort_keys=True)
                + repr(proxy.args)
            )
            self.assertNotIn(private, public)
            self.assertNotIn("fixture-value", public)
        finally:
            _kill_if_running(process)

    def test_constructor_rejects_unsafe_or_ambiguous_configuration(
        self,
    ) -> None:
        spec = FrameSpec(1, 1, 1)
        for command in ([], "ffmpeg", [b"ffmpeg"], ["ffmpeg", ""]):
            with self.subTest(command=command):
                with self.assertRaises((TypeError, ValueError)):
                    FfmpegFrameSource(  # type: ignore[arg-type]
                        command,
                        spec,
                        source_kind="file",
                    )
        for invalid in (True, 0, float("inf"), 3601):
            with self.subTest(timeout=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    FfmpegFrameSource(
                        ["ffmpeg"],
                        spec,
                        source_kind="file",
                        startup_timeout=invalid,  # type: ignore[arg-type]
                    )
        with self.assertRaises(ValueError):
            FfmpegFrameSource(
                ["ffmpeg"],
                spec,
                source_kind="private/path",
            )
        with self.assertRaises(TypeError):
            FfmpegFrameSource(
                ["ffmpeg"],
                spec,
                source_kind="file",
                source=True,  # type: ignore[arg-type]
            )
        for descriptor, template in (
            (True, "{fd}"),
            (2, "{fd}"),
            (3, "missing-placeholder"),
            (3, "{fd}{fd}"),
        ):
            with self.subTest(descriptor=descriptor, template=template):
                with self.assertRaises((TypeError, ValueError)):
                    InheritedFdArgument(  # type: ignore[arg-type]
                        descriptor,
                        template,
                    )

    def test_bound_descriptor_cannot_be_replaced_before_lazy_spawn(
        self,
    ) -> None:
        original = os.memfd_create("bound-old", flags=os.MFD_CLOEXEC)
        replacement: int | None = None
        try:
            original_number = original
            os.write(original, b"OLD")
            os.lseek(original, 0, os.SEEK_SET)
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys;"
                    "fd=int(sys.argv[1]);"
                    "os.lseek(fd,0,0);"
                    "os.write(1,os.read(fd,3))"
                )
                + [InheritedFdArgument(original)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )
            self.assertIsNone(source._pending_fd_binding)

            os.close(original)
            original = -1
            replacement = os.memfd_create(
                "bound-new",
                flags=os.MFD_CLOEXEC,
            )
            self.assertEqual(replacement, original_number)
            os.write(replacement, b"NEW")

            with self.assertRaises(FfmpegSupervisorError) as raised:
                source.read_frame()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertIsNone(source._pending_fd_binding)
            os.lseek(replacement, 0, os.SEEK_SET)
            self.assertEqual(os.read(replacement, 3), b"NEW")
        finally:
            if original >= 0:
                os.close(original)
            if replacement is not None:
                os.close(replacement)

    def test_abandoned_new_source_does_not_own_a_duplicate_descriptor(
        self,
    ) -> None:
        descriptor = os.memfd_create("borrowed", flags=os.MFD_CLOEXEC)
        try:
            before = set(os.listdir("/proc/self/fd"))
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )

            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)

            del source
            gc.collect()
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_missing_later_descriptor_cannot_alias_an_earlier_duplicate(
        self,
    ) -> None:
        first = os.memfd_create("first-borrowed", flags=os.MFD_CLOEXEC)
        second = os.memfd_create("second-borrowed", flags=os.MFD_CLOEXEC)
        try:
            first_argument = InheritedFdArgument(first)
            second_argument = InheritedFdArgument(second)
            os.close(second)
            second = -1
            before = set(os.listdir("/proc/self/fd"))
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)")
                + [first_argument, second_argument],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )

            with self.assertRaises(FfmpegSupervisorError) as raised:
                source.start()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            os.fstat(first)
        finally:
            os.close(first)
            if second >= 0:
                os.close(second)

    def test_later_descriptor_disappearance_cannot_alias_first_binding(
        self,
    ) -> None:
        first = os.memfd_create("first-race-source", flags=os.MFD_CLOEXEC)
        second = os.memfd_create("second-race-source", flags=os.MFD_CLOEXEC)
        real_socket = ffmpeg_io.socket.socket
        reservation_targets: list[int] = []
        second_closed = False
        try:
            first_argument = InheritedFdArgument(first)
            second_argument = InheritedFdArgument(second)
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)")
                + [first_argument, second_argument],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            before_count = len(os.listdir("/proc/self/fd"))

            def close_second_before_first_reservation(
                *args: object,
                **kwargs: object,
            ) -> ffmpeg_io.socket.socket:
                nonlocal second, second_closed
                if not second_closed:
                    os.close(second)
                    second = -1
                    second_closed = True
                reservation = real_socket(*args, **kwargs)
                reservation_targets.append(reservation.fileno())
                return reservation

            with (
                patch.object(
                    ffmpeg_io.socket,
                    "socket",
                    side_effect=close_second_before_first_reservation,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertTrue(second_closed)
            self.assertGreaterEqual(len(reservation_targets), 2)
            self.assertEqual(
                reservation_targets[0],
                second_argument.descriptor,
            )
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(
                len(os.listdir("/proc/self/fd")),
                before_count - 1,
            )
            for target in reservation_targets:
                with self.assertRaises(OSError) as closed:
                    os.fstat(target)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            os.fstat(first)
        finally:
            os.close(first)
            if second >= 0:
                os.close(second)

    def test_binding_interrupt_after_acquire_closes_owned_fd_and_retries(
        self,
    ) -> None:
        descriptor = os.memfd_create("retry-borrowed", flags=os.MFD_CLOEXEC)
        captured: list[int] = []
        interruption = KeyboardInterrupt("synthetic post-acquire interrupt")
        real_acquire = ffmpeg_io._PendingFdBinding.acquire
        try:
            os.write(descriptor, b"OLD")
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys;"
                    "fd=int(sys.argv[1]);"
                    "os.lseek(fd,0,0);"
                    "os.write(1,os.read(fd,3))"
                )
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )
            before = set(os.listdir("/proc/self/fd"))

            def acquire_then_interrupt(
                binding: ffmpeg_io._PendingFdBinding,
                command: tuple[str | InheritedFdArgument, ...],
            ) -> None:
                real_acquire(binding, command)
                captured.extend(binding.pass_fds)
                raise interruption

            with (
                patch.object(
                    ffmpeg_io._PendingFdBinding,
                    "acquire",
                    new=acquire_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                source.start()

            self.assertIs(raised.exception, interruption)
            self.assertEqual(source.state, "new")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            for owned in captured:
                with self.assertRaises(OSError) as closed:
                    os.fstat(owned)
                self.assertEqual(closed.exception.errno, errno.EBADF)

            self.assertEqual(source.read_frame(), b"OLD")
            self.assertIsNone(source.read_frame())
            self.assertIsNone(source._pending_fd_binding)
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_raising_dup2_wrapper_leaves_reserved_target_owned(
        self,
    ) -> None:
        descriptor = os.memfd_create("hidden-duplicate", flags=os.MFD_CLOEXEC)
        created: list[int] = []
        interruption = KeyboardInterrupt("synthetic post-syscall interrupt")
        real_dup2 = os.dup2
        try:
            os.write(descriptor, b"OLD")
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys;"
                    "fd=int(sys.argv[1]);"
                    "os.lseek(fd,0,0);"
                    "os.write(1,os.read(fd,3))"
                )
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )
            before = set(os.listdir("/proc/self/fd"))

            def duplicate_then_interrupt(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                result = real_dup2(
                    source_fd,
                    target_fd,
                    inheritable=inheritable,
                )
                created.append(target_fd)
                raise interruption

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=duplicate_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                source.start()

            self.assertIs(raised.exception, interruption)
            self.assertEqual(len(created), 1)
            self.assertEqual(source.state, "new")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            with self.assertRaises(OSError) as closed:
                os.fstat(created[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)

            self.assertEqual(source.read_frame(), b"OLD")
            self.assertIsNone(source.read_frame())
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_failed_dup2_never_claims_concurrent_foreign_duplicate(
        self,
    ) -> None:
        descriptor = os.memfd_create("foreign-source", flags=os.MFD_CLOEXEC)
        foreign: int | None = None
        try:
            os.write(descriptor, b"OLD")
            os.lseek(descriptor, 0, os.SEEK_SET)
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            before = set(os.listdir("/proc/self/fd"))

            def create_foreign_then_fail(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                nonlocal foreign
                del target_fd, inheritable
                foreign = os.dup(source_fd)
                raise OSError(errno.EMFILE, "synthetic primary dup2 failure")

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=create_foreign_then_fail,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertIsNone(source._pending_fd_binding)
            self.assertIsNotNone(foreign)
            assert foreign is not None
            os.lseek(foreign, 0, os.SEEK_SET)
            self.assertEqual(os.read(foreign, 3), b"OLD")
            os.close(foreign)
            foreign = None
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
        finally:
            os.close(descriptor)
            if foreign is not None:
                os.close(foreign)

    def test_same_inode_reopen_with_changed_offset_fails_closed(
        self,
    ) -> None:
        original = os.memfd_create("same-inode", flags=os.MFD_CLOEXEC)
        alias = os.dup(original)
        reopened: int | None = None
        try:
            os.write(original, b"OLDNEW")
            os.lseek(original, 0, os.SEEK_SET)
            argument = InheritedFdArgument(original)
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys;"
                    "fd=int(sys.argv[1]);"
                    "os.write(1,os.read(fd,3))"
                )
                + [argument],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            original_number = original
            os.close(original)
            original = -1
            reopened = os.open(
                f"/proc/self/fd/{alias}",
                os.O_RDWR | os.O_CLOEXEC,
            )
            self.assertEqual(reopened, original_number)
            os.lseek(reopened, 3, os.SEEK_SET)
            self.assertEqual(
                ffmpeg_io._descriptor_identity(os.fstat(reopened)),
                argument._identity,
            )

            with self.assertRaises(FfmpegSupervisorError) as raised:
                source.start()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(os.lseek(reopened, 0, os.SEEK_CUR), 3)
            self.assertEqual(os.read(reopened, 3), b"NEW")
        finally:
            if original >= 0:
                os.close(original)
            os.close(alias)
            if reopened is not None:
                os.close(reopened)

    def test_partial_binding_failure_retries_one_shot_close_failure(
        self,
    ) -> None:
        first = os.memfd_create("partial-first", flags=os.MFD_CLOEXEC)
        second = os.memfd_create("partial-second", flags=os.MFD_CLOEXEC)
        real_dup2 = os.dup2
        real_close = os.close
        real_close_reservation = (
            ffmpeg_io._close_inherited_reservation
        )
        duplicates: list[int] = []
        duplicate_calls = 0
        close_failed = False
        try:
            arguments = [
                InheritedFdArgument(first),
                InheritedFdArgument(second),
            ]
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)") + arguments,
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            before = set(os.listdir("/proc/self/fd"))

            def fail_second_duplicate(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                nonlocal duplicate_calls
                duplicate_calls += 1
                if duplicate_calls == 2:
                    raise OSError(errno.EMFILE, "synthetic duplicate failure")
                result = real_dup2(
                    source_fd,
                    target_fd,
                    inheritable=inheritable,
                )
                duplicates.append(target_fd)
                return result

            def fail_first_owned_close(
                reservation: ffmpeg_io.socket.socket,
            ) -> None:
                nonlocal close_failed
                descriptor = reservation.fileno()
                if descriptor in duplicates and not close_failed:
                    close_failed = True
                    raise OSError(errno.EINTR, "synthetic close failure")
                real_close_reservation(reservation)

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=fail_second_duplicate,
                ),
                patch.object(
                    ffmpeg_io,
                    "_close_inherited_reservation",
                    side_effect=fail_first_owned_close,
                ),
                self.assertRaises(BaseExceptionGroup),
            ):
                source.start()

            self.assertEqual(duplicate_calls, 2)
            self.assertTrue(close_failed)
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            for duplicate in duplicates:
                with self.assertRaises(OSError) as closed:
                    os.fstat(duplicate)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            os.fstat(first)
            os.fstat(second)
        finally:
            real_close(first)
            real_close(second)

    def test_render_failure_after_duplication_leaves_no_owned_descriptor(
        self,
    ) -> None:
        descriptor = os.memfd_create("render-borrowed", flags=os.MFD_CLOEXEC)
        try:
            source = FfmpegFrameSource(
                _python_child("raise SystemExit(0)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            before = set(os.listdir("/proc/self/fd"))

            with (
                patch.object(
                    InheritedFdArgument,
                    "render",
                    side_effect=MemoryError("synthetic render failure"),
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertEqual(source.state, "failed")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_popen_interrupt_keeps_template_for_safe_retry(
        self,
    ) -> None:
        descriptor = os.memfd_create("popen-retry", flags=os.MFD_CLOEXEC)
        interruption = KeyboardInterrupt("synthetic pre-spawn interrupt")
        try:
            os.write(descriptor, b"OLD")
            source = FfmpegFrameSource(
                _python_child(
                    "import os,sys;"
                    "fd=int(sys.argv[1]);"
                    "os.lseek(fd,0,0);"
                    "os.write(1,os.read(fd,3))"
                )
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )
            before = set(os.listdir("/proc/self/fd"))

            with (
                patch.object(
                    ffmpeg_io.subprocess,
                    "Popen",
                    side_effect=interruption,
                ),
                self.assertRaises(KeyboardInterrupt) as raised,
            ):
                source.start()

            self.assertIs(raised.exception, interruption)
            self.assertEqual(source.state, "new")
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)

            self.assertEqual(source.read_frame(), b"OLD")
            self.assertIsNone(source.read_frame())
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_duplicate_layout_uses_unique_reserved_slots(
        self,
    ) -> None:
        first = os.memfd_create("layout-first", flags=os.MFD_CLOEXEC)
        second = os.memfd_create("layout-second", flags=os.MFD_CLOEXEC)
        try:
            source = FfmpegFrameSource(
                [
                    "ffmpeg",
                    InheritedFdArgument(first, "first:{fd}"),
                    InheritedFdArgument(first, "again:{fd}"),
                    InheritedFdArgument(second, "second:{fd}"),
                ],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
            )
            before = set(os.listdir("/proc/self/fd"))

            with (
                patch.object(
                    ffmpeg_io.subprocess,
                    "Popen",
                    side_effect=OSError("synthetic spawn failure"),
                ) as popen,
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            command = popen.call_args.args[0]
            inherited = popen.call_args.kwargs["pass_fds"]
            first_rendered = int(command[1].split(":", 1)[1])
            repeated = int(command[2].split(":", 1)[1])
            second_rendered = int(command[3].split(":", 1)[1])
            self.assertEqual(raised.exception.code, "spawn_failed")
            self.assertEqual(len(inherited), 2)
            self.assertEqual(first_rendered, repeated)
            self.assertNotEqual(first_rendered, second_rendered)
            self.assertEqual(
                set(inherited),
                {first_rendered, second_rendered},
            )
            self.assertTrue(
                all(item not in {first, second} for item in inherited)
            )
            self.assertTrue(all(item >= 3 for item in inherited))
            self.assertIsNone(source._pending_fd_binding)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            os.fstat(first)
            os.fstat(second)
        finally:
            os.close(first)
            os.close(second)

    def test_closed_stdout_cannot_be_selected_as_inherited_target(
        self,
    ) -> None:
        script = """
import fcntl
import os
import sys
from alpr_runner.ffmpeg_io import (
    FfmpegFrameSource,
    FrameSpec,
    InheritedFdArgument,
)
os.close(1)
low = os.memfd_create("closed-stdout", flags=os.MFD_CLOEXEC)
high = fcntl.fcntl(low, fcntl.F_DUPFD_CLOEXEC, 3)
os.close(low)
os.write(high, b"abc")
os.lseek(high, 0, os.SEEK_SET)
source = FfmpegFrameSource(
    [
        sys.executable,
        "-S",
        "-c",
        "import os,sys;os.write(1,os.read(int(sys.argv[1]),3))",
        InheritedFdArgument(high),
    ],
    FrameSpec(1, 1, 1),
    source_kind="pipe",
    startup_timeout=1.0,
    idle_timeout=1.0,
    stderr_limit=1024,
    terminate_timeout=0.2,
    kill_timeout=0.2,
)
assert source.read_frame() == b"abc"
assert source.read_frame() is None
assert source._pending_fd_binding is None
os.fstat(high)
os.write(2, b"OK")
os.close(high)
"""
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"OK")

    def test_transient_owned_fstat_failure_is_retried_before_release(
        self,
    ) -> None:
        descriptor = os.memfd_create("fstat-borrowed", flags=os.MFD_CLOEXEC)
        real_dup2 = os.dup2
        real_fstat = os.fstat
        duplicates: list[int] = []
        duplicate_fstats = 0
        try:
            source = FfmpegFrameSource(
                _python_child("import time; time.sleep(10)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )
            before = set(os.listdir("/proc/self/fd"))

            def track_duplicate(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                result = real_dup2(
                    source_fd,
                    target_fd,
                    inheritable=inheritable,
                )
                duplicates.append(target_fd)
                return result

            def fail_first_cleanup_fstat(
                target: int,
            ) -> os.stat_result:
                nonlocal duplicate_fstats
                if target in duplicates:
                    duplicate_fstats += 1
                    if duplicate_fstats == 2:
                        raise OSError(errno.EIO, "synthetic fstat failure")
                return real_fstat(target)

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=track_duplicate,
                ),
                patch.object(
                    ffmpeg_io.os,
                    "fstat",
                    side_effect=fail_first_cleanup_fstat,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            self.assertEqual(raised.exception.code, "io_setup_failed")
            self.assertEqual(duplicate_fstats, 3)
            self.assertIsNone(source._pending_fd_binding)
            self.assertIs(source.receipt()["process_reaped"], True)
            self.assertIs(source.receipt()["process_group_closed"], True)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)
            for duplicate in duplicates:
                with self.assertRaises(OSError) as closed:
                    os.fstat(duplicate)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def test_failed_start_close_retries_persistent_descriptor_cleanup(
        self,
    ) -> None:
        descriptor = os.memfd_create(
            "persistent-close-source",
            flags=os.MFD_CLOEXEC,
        )
        real_dup2 = os.dup2
        targets: list[int] = []
        source: FfmpegFrameSource | None = None
        try:
            source = FfmpegFrameSource(
                _python_child("import time; time.sleep(10)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )

            def track_duplicate(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                result = real_dup2(
                    source_fd,
                    target_fd,
                    inheritable=inheritable,
                )
                targets.append(target_fd)
                return result

            def fail_owned_close(
                reservation: ffmpeg_io.socket.socket,
            ) -> None:
                del reservation
                raise OSError(errno.EIO, "synthetic persistent close failure")

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=track_duplicate,
                ),
                patch.object(
                    ffmpeg_io,
                    "_close_inherited_reservation",
                    side_effect=fail_owned_close,
                ),
                self.assertRaises(BaseExceptionGroup),
            ):
                source.start()

            self.assertEqual(source.state, "failed")
            self.assertIsNotNone(source._pending_fd_binding)
            self.assertEqual(len(targets), 1)
            os.fstat(targets[0])
            self.assertIs(source.receipt()["process_reaped"], True)
            self.assertIs(source.receipt()["process_group_closed"], True)

            source.close()

            self.assertIsNone(source._pending_fd_binding)
            with self.assertRaises(OSError) as closed:
                os.fstat(targets[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)
            os.fstat(descriptor)
        finally:
            if source is not None:
                source.close()
            os.close(descriptor)

    def test_close_hook_reuse_never_closes_replacement_descriptor(
        self,
    ) -> None:
        descriptor = os.memfd_create("reuse-borrowed", flags=os.MFD_CLOEXEC)
        real_dup2 = os.dup2
        real_close = os.close
        real_close_reservation = (
            ffmpeg_io._close_inherited_reservation
        )
        duplicates: list[int] = []
        replacement: int | None = None
        try:
            source = FfmpegFrameSource(
                _python_child("import time; time.sleep(10)")
                + [InheritedFdArgument(descriptor)],
                FrameSpec(1, 1, 1),
                source_kind="pipe",
                startup_timeout=1.0,
                idle_timeout=1.0,
                stderr_limit=1024,
                terminate_timeout=0.2,
                kill_timeout=0.2,
            )

            def track_duplicate(
                source_fd: int,
                target_fd: int,
                *,
                inheritable: bool = True,
            ) -> int:
                result = real_dup2(
                    source_fd,
                    target_fd,
                    inheritable=inheritable,
                )
                duplicates.append(target_fd)
                return result

            def close_reopen_then_fail(
                reservation: ffmpeg_io.socket.socket,
            ) -> None:
                nonlocal replacement
                target = reservation.fileno()
                if target in duplicates and replacement is None:
                    real_close_reservation(reservation)
                    replacement = os.memfd_create(
                        "replacement",
                        flags=os.MFD_CLOEXEC,
                    )
                    self.assertEqual(replacement, target)
                    os.write(replacement, b"NEW")
                    raise OSError(errno.EIO, "synthetic post-close failure")
                real_close_reservation(reservation)

            with (
                patch.object(
                    ffmpeg_io.os,
                    "dup2",
                    side_effect=track_duplicate,
                ),
                patch.object(
                    ffmpeg_io,
                    "_close_inherited_reservation",
                    side_effect=close_reopen_then_fail,
                ),
                self.assertRaises(FfmpegSupervisorError) as raised,
            ):
                source.start()

            self.assertEqual(raised.exception.code, "io_setup_failed")
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertIsNone(source._pending_fd_binding)
            os.lseek(replacement, 0, os.SEEK_SET)
            self.assertEqual(os.read(replacement, 3), b"NEW")
            os.fstat(descriptor)
        finally:
            real_close(descriptor)
            if replacement is not None:
                real_close(replacement)


if __name__ == "__main__":
    unittest.main()
