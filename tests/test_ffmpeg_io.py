from __future__ import annotations

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

from alpr_runner.ffmpeg_io import (
    MAX_FRAME_BYTES,
    FfmpegFrameSource,
    FfmpegSupervisorError,
    FrameSpec,
)


def _python_child(source: str) -> list[str]:
    return [sys.executable, "-S", "-c", source]


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


class _RecordingWaitProcess:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        self.wait_timeouts: list[float] = []

    def wait(self, *, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        return self.return_code


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

        source.close()

        self.assertEqual(source.receipt()["termination"], "kill")
        self.assertEqual(source.receipt()["exit_code"], -signal.SIGKILL)
        self.assertIs(source.receipt()["process_group_closed"], True)
        self.assertEqual(source.state, "closed")
        self.assertIsNotNone(source._process)
        self.assertEqual(source._process.poll(), -signal.SIGKILL)

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
        self.assertEqual(raised.exception.code, "spawn_failed")
        self.assertNotIn(private, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(source.state, "failed")

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


if __name__ == "__main__":
    unittest.main()
