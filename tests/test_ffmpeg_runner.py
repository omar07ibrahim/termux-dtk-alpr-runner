from __future__ import annotations

import json
import inspect
import os
import signal
import sys
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable
from unittest.mock import Mock, patch

import alpr_runner.ffmpeg_video as ffmpeg_video
from alpr_runner.ffmpeg_io import FfmpegSupervisorError, FrameSpec
from alpr_runner.ffmpeg_video import (
    PLATE_CALLBACK_FAILURE,
    FfmpegVideoAlprRunner,
)


PRIVATE_RTSP = "rtsp://viewer:synthetic-secret@192.0.2.44/live?token=private"


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


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rtsp": PRIVATE_RTSP,
        "dtk_dir": "vendor/arm64",
        "out": "runtime-video",
        "width": 1280,
        "height": 720,
        "fps": 20,
        "countries": "",
        "min_plate_width": 60,
        "max_plate_width": 500,
        "threads": 0,
        "fps_limit": 0,
        "confirmations": 1,
        "accumulation_ms": 0,
        "duplicate_timeout_ms": 600,
        "max_zoom": 4.0,
        "preview_every": 0,
        "status_print_interval": 0.0,
        "buffer_retain": 16,
        "max_inflight_frame_mib": 96,
        "ffmpeg_startup_timeout": 10.0,
        "ffmpeg_idle_timeout": 5.0,
        "ffmpeg_stderr_kib": 64,
        "ffmpeg_terminate_timeout": 2.0,
        "ffmpeg_kill_timeout": 2.0,
        "allow_unlicensed": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeFrameSource:
    def __init__(
        self,
        outcomes: list[bytes | None | BaseException | Callable[[], bytes | None]],
        events: list[str],
        *,
        close_error: BaseException | None = None,
        receipt: dict[str, object] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._events = events
        self._close_error = close_error
        self._receipt = receipt or {
            "failure_code": None,
            "frame": {
                "bytes_per_frame": 3,
                "fps": 1,
                "height": 1,
                "pixel_format": "rgb24",
                "width": 1,
            },
            "frames_delivered": 0,
            "schema_version": 1,
            "source": {"kind": "rtsp"},
            "state": "ended",
            "stderr_bytes": 0,
            "stdout_bytes": 0,
        }
        self.stop_events: list[threading.Event] = []

    def read_frame(self, stop_event: threading.Event) -> bytes | None:
        self.stop_events.append(stop_event)
        self._events.append("source-read")
        if not self._outcomes:
            raise AssertionError("unexpected source read")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    def close(self) -> None:
        self._events.append("source-close")
        if self._close_error is not None:
            raise self._close_error

    def receipt(self) -> dict[str, object]:
        return dict(self._receipt)


def _runner_for_run(
    source: _FakeFrameSource,
    events: list[str],
    *,
    version_error: BaseException | None = None,
    lpr_close_error: BaseException | None = None,
    on_lpr_close: Callable[[], None] | None = None,
    status_print_interval: float = 0.0,
) -> tuple[FfmpegVideoAlprRunner, list[bytes]]:
    runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
    frames: list[bytes] = []

    def version() -> str:
        events.append("lpr-version")
        if version_error is not None:
            raise version_error
        return "synthetic-version"

    def close_lpr() -> None:
        events.append("lpr-close")
        if on_lpr_close is not None:
            on_lpr_close()
        if lpr_close_error is not None:
            raise lpr_close_error

    runner.args = SimpleNamespace(
        rtsp=PRIVATE_RTSP,
        width=1,
        height=1,
        status_print_interval=status_print_interval,
    )
    runner.out_dir = Path("runtime-video")
    runner.frame_source = source
    runner.lpr = SimpleNamespace(version=version, close=close_lpr)
    runner.frame_leases = SimpleNamespace(
        close=lambda: events.append("leases-close")
    )
    runner.stop_event = ffmpeg_video._SignalAwareEvent()
    runner.lock = threading.RLock()
    runner.callback_failure = None
    runner.frame_count = 0
    runner.plate_count = 0
    runner.completed_count = 0
    runner.dropped_count = 0
    runner.last_status = {}
    runner._source_shutdown = False
    runner._lpr_shutdown_started = False
    runner._lpr_shutdown = False
    runner._leases_shutdown = False
    runner._run_body_finished = False
    runner._run_body_failure = None
    runner._run_cleanup_failures = []
    runner._run_selected_error = None
    runner._run_outcome_ready = False
    runner._previous_signal_handlers = None

    def put_frame(data: bytes) -> None:
        frames.append(data)
        runner.frame_count += 1

    runner._put_raw_frame = put_frame  # type: ignore[method-assign]
    return runner, frames


class FfmpegRunnerConstructionTests(unittest.TestCase):
    def test_cli_exposes_all_bounded_supervisor_controls(self) -> None:
        argv = [
            "ffmpeg-video",
            "--rtsp",
            "rtsp://127.0.0.1/live",
            "--ffmpeg-startup-timeout",
            "3.5",
            "--ffmpeg-idle-timeout",
            "1.25",
            "--ffmpeg-stderr-kib",
            "32",
            "--ffmpeg-terminate-timeout",
            "0.75",
            "--ffmpeg-kill-timeout",
            "0.5",
        ]
        with patch.object(sys, "argv", argv):
            args = ffmpeg_video.parse_args()

        self.assertEqual(args.ffmpeg_startup_timeout, 3.5)
        self.assertEqual(args.ffmpeg_idle_timeout, 1.25)
        self.assertEqual(args.ffmpeg_stderr_kib, 32)
        self.assertEqual(args.ffmpeg_terminate_timeout, 0.75)
        self.assertEqual(args.ffmpeg_kill_timeout, 0.5)

    def test_constructor_wires_private_rtsp_to_side_effect_free_source(
        self,
    ) -> None:
        captured: dict[str, object] = {}
        source = object()

        def build_source(
            command: list[str],
            spec: FrameSpec,
            **kwargs: object,
        ) -> object:
            captured["command"] = command
            captured["spec"] = spec
            captured["kwargs"] = kwargs
            return source

        fake_lpr_api = SimpleNamespace(
            LPREngine_SetFrameProcessingCompletedCallback=lambda *_args: None
        )
        fake_lpr = SimpleNamespace(lib=fake_lpr_api, engine=object())
        fake_video_module = ModuleType("alpr_runner.video")
        fake_video_module.PIXFMT_RGB24 = 2  # type: ignore[attr-defined]
        fake_video_module.DtkVideoLibrary = (  # type: ignore[attr-defined]
            lambda _path: SimpleNamespace()
        )
        with (
            patch.object(
                ffmpeg_video,
                "FfmpegFrameSource",
                side_effect=build_source,
            ),
            patch.object(
                ffmpeg_video,
                "prepare_private_directory",
                return_value=Path("runtime-video"),
            ),
            patch.object(ffmpeg_video.os, "chdir"),
            patch.dict(
                sys.modules,
                {"alpr_runner.video": fake_video_module},
            ),
            patch.object(ffmpeg_video, "DtkLpr", return_value=fake_lpr),
        ):
            runner = FfmpegVideoAlprRunner(_args())

        command = captured["command"]
        self.assertIsInstance(command, list)
        if not isinstance(command, list):
            self.fail("captured command is not a list")
        self.assertEqual(command[:3], ["ffmpeg", "-nostdin", "-hide_banner"])
        self.assertEqual(command[command.index("-i") + 1], PRIVATE_RTSP)
        self.assertNotIn("shell", captured["kwargs"])
        self.assertEqual(captured["spec"], FrameSpec(1280, 720, 20))
        self.assertEqual(
            captured["kwargs"],
            {
                "source_kind": "rtsp",
                "source": PRIVATE_RTSP,
                "startup_timeout": 10.0,
                "idle_timeout": 5.0,
                "stderr_limit": 64 * 1024,
                "terminate_timeout": 2.0,
                "kill_timeout": 2.0,
            },
        )
        self.assertIs(runner.frame_source, source)

    def test_callback_registration_failure_closes_source_and_native_owner(
        self,
    ) -> None:
        source = SimpleNamespace(close=Mock())
        registration_failure = RuntimeError(
            "synthetic callback registration failure"
        )
        fake_lpr_api = SimpleNamespace(
            LPREngine_SetFrameProcessingCompletedCallback=Mock(
                side_effect=registration_failure
            )
        )
        fake_lpr = SimpleNamespace(
            lib=fake_lpr_api,
            engine=object(),
            close=Mock(),
        )
        fake_video_module = ModuleType("alpr_runner.video")
        fake_video_module.PIXFMT_RGB24 = 2  # type: ignore[attr-defined]
        fake_video_module.DtkVideoLibrary = (  # type: ignore[attr-defined]
            lambda _path: SimpleNamespace()
        )

        with (
            patch.object(
                ffmpeg_video,
                "FfmpegFrameSource",
                return_value=source,
            ),
            patch.object(
                ffmpeg_video,
                "prepare_private_directory",
                return_value=Path("runtime-video"),
            ),
            patch.object(ffmpeg_video.os, "chdir"),
            patch.dict(
                sys.modules,
                {"alpr_runner.video": fake_video_module},
            ),
            patch.object(
                ffmpeg_video,
                "DtkLpr",
                return_value=fake_lpr,
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            FfmpegVideoAlprRunner(_args())

        self.assertIs(raised.exception, registration_failure)
        source.close.assert_called_once_with()
        fake_lpr.close.assert_called_once_with()

    def test_media_and_supervisor_validation_precede_filesystem_side_effects(
        self,
    ) -> None:
        cases = (
            ({"width": 4097}, ValueError),
            ({"height": 2161}, ValueError),
            ({"fps": 0}, ValueError),
            ({"ffmpeg_startup_timeout": 0.0}, ValueError),
            ({"ffmpeg_idle_timeout": True}, TypeError),
            ({"ffmpeg_stderr_kib": 0}, ValueError),
            ({"ffmpeg_stderr_kib": 1025}, ValueError),
            ({"ffmpeg_terminate_timeout": 0.0}, ValueError),
            ({"ffmpeg_kill_timeout": 0.0}, ValueError),
        )
        for changes, error_type in cases:
            with self.subTest(changes=changes):
                with patch.object(
                    ffmpeg_video,
                    "prepare_private_directory",
                ) as prepare:
                    with self.assertRaises(error_type):
                        FfmpegVideoAlprRunner(_args(**changes))
                prepare.assert_not_called()


class FfmpegRunnerLifecycleTests(unittest.TestCase):
    def test_exact_frames_use_the_identical_stop_event_and_clean_eof(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([b"rgb", b"123", None], events)
        runner, frames = _runner_for_run(source, events)

        with redirect_stdout(StringIO()):
            result = runner.run()

        self.assertEqual(result, 0)
        self.assertEqual(frames, [b"rgb", b"123"])
        self.assertEqual(len(source.stop_events), 3)
        self.assertTrue(
            all(event is runner.stop_event for event in source.stop_events)
        )
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_supervisor_failure_propagates_without_private_source(self) -> None:
        events: list[str] = []
        receipt = {
            "failure_code": "startup_timeout",
            "frame": FrameSpec(1, 1, 1).to_receipt(),
            "source": {"kind": "rtsp"},
            "state": "failed",
        }
        failure = FfmpegSupervisorError("startup_timeout", receipt)
        source = _FakeFrameSource([failure], events)
        runner, _frames = _runner_for_run(source, events)
        output = StringIO()

        with redirect_stdout(output), self.assertRaises(
            FfmpegSupervisorError
        ) as raised:
            runner.run()

        self.assertIs(raised.exception, failure)
        public_text = output.getvalue() + str(raised.exception)
        self.assertNotIn(PRIVATE_RTSP, public_text)
        self.assertNotIn("synthetic-secret", public_text)
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_source_cleanup_failure_preserves_adapter_and_leases(self) -> None:
        events: list[str] = []
        cleanup_failure = RuntimeError("synthetic source cleanup failure")
        source = _FakeFrameSource(
            [None],
            events,
            close_error=cleanup_failure,
        )
        runner, _frames = _runner_for_run(source, events)

        with redirect_stdout(StringIO()), self.assertRaises(RuntimeError) as raised:
            runner.run()

        self.assertIs(raised.exception, cleanup_failure)
        self.assertEqual(events[-1], "source-close")
        self.assertNotIn("lpr-close", events)
        self.assertNotIn("leases-close", events)

    def test_adapter_cleanup_failure_preserves_leases(self) -> None:
        events: list[str] = []
        lpr_failure = RuntimeError("synthetic adapter cleanup failure")
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(
            source,
            events,
            lpr_close_error=lpr_failure,
        )

        with redirect_stdout(StringIO()), self.assertRaises(RuntimeError) as raised:
            runner.run()

        self.assertIs(raised.exception, lpr_failure)
        self.assertEqual(events[-2:], ["source-close", "lpr-close"])
        self.assertNotIn("leases-close", events)

    def test_body_and_cleanup_failures_are_grouped_without_loss(self) -> None:
        events: list[str] = []
        body_failure = RuntimeError("synthetic frame body failure")
        cleanup_failure = RuntimeError("synthetic source cleanup failure")
        source = _FakeFrameSource(
            [body_failure],
            events,
            close_error=cleanup_failure,
        )
        runner, _frames = _runner_for_run(source, events)

        with redirect_stdout(StringIO()), self.assertRaises(
            BaseExceptionGroup
        ) as raised:
            runner.run()

        self.assertEqual(
            raised.exception.message,
            "ffmpeg runner body and cleanup failures",
        )
        self.assertEqual(
            raised.exception.exceptions,
            (body_failure, cleanup_failure),
        )
        self.assertNotIn("lpr-close", events)
        self.assertNotIn("leases-close", events)

    def test_cleanup_and_final_callback_check_failures_are_all_preserved(
        self,
    ) -> None:
        events: list[str] = []
        body_failure = RuntimeError("synthetic frame body failure")
        cleanup_failure = RuntimeError("synthetic source cleanup failure")
        final_check_failure = KeyboardInterrupt()
        source = _FakeFrameSource(
            [body_failure],
            events,
            close_error=cleanup_failure,
        )
        runner, _frames = _runner_for_run(source, events)
        runner._raise_for_callback_failure = Mock(  # type: ignore[method-assign]
            side_effect=[None, final_check_failure]
        )

        with redirect_stdout(StringIO()), self.assertRaises(
            BaseExceptionGroup
        ) as raised:
            runner.run()

        self.assertIs(raised.exception.exceptions[0], body_failure)
        cleanup_group = raised.exception.exceptions[1]
        self.assertIsInstance(cleanup_group, BaseExceptionGroup)
        if not isinstance(cleanup_group, BaseExceptionGroup):
            self.fail("cleanup failures were not grouped")
        self.assertEqual(
            cleanup_group.exceptions,
            (cleanup_failure, final_check_failure),
        )

    def test_stop_signal_failure_cannot_skip_ordered_teardown(self) -> None:
        events: list[str] = []
        body_failure = RuntimeError("synthetic frame body failure")
        stop_failure = RuntimeError("synthetic stop signal failure")
        source = _FakeFrameSource([body_failure], events)
        runner, _frames = _runner_for_run(source, events)
        runner.stop_event = SimpleNamespace(
            set=lambda: (_ for _ in ()).throw(stop_failure),
            is_set=lambda: False,
        )  # type: ignore[assignment]

        with redirect_stdout(StringIO()), self.assertRaises(
            BaseExceptionGroup
        ) as raised:
            runner.run()

        self.assertEqual(
            raised.exception.exceptions,
            (body_failure, stop_failure),
        )
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_pre_read_native_failure_still_runs_ordered_cleanup(self) -> None:
        events: list[str] = []
        version_failure = RuntimeError("synthetic version failure")
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(
            source,
            events,
            version_error=version_failure,
        )

        with redirect_stdout(StringIO()), self.assertRaises(RuntimeError) as raised:
            runner.run()

        self.assertIs(raised.exception, version_failure)
        self.assertNotIn("source-read", events)
        self.assertEqual(
            events,
            ["lpr-version", "source-close", "lpr-close", "leases-close"],
        )

    def test_keyboard_interrupt_is_graceful_and_still_cleans_up(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([KeyboardInterrupt()], events)
        runner, _frames = _runner_for_run(source, events)

        with redirect_stdout(StringIO()):
            result = runner.run()

        self.assertEqual(result, 0)
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_entry_and_body_cleanup_transition_interruptions_resume_teardown(
        self,
    ) -> None:
        cases = (
            (
                FfmpegVideoAlprRunner.run,
                "try: return self._run_once()",
                [],
                True,
            ),
            (
                FfmpegVideoAlprRunner._run_once,
                "return self._finish_run()",
                ["lpr-version", "source-read"],
                False,
            ),
        )

        for function, source_text, prefix, graceful in cases:
            with self.subTest(boundary=source_text):
                events: list[str] = []
                source = _FakeFrameSource([None], events)
                runner, _frames = _runner_for_run(source, events)
                target_code = function.__code__
                target_line = _source_line(function, source_text)
                interruption = KeyboardInterrupt(
                    f"synthetic boundary interrupt: {source_text}"
                )

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
                    if graceful:
                        with redirect_stdout(StringIO()):
                            sys.settrace(interrupt_once)
                            result = runner.run()
                    else:
                        with (
                            redirect_stdout(StringIO()),
                            self.assertRaises(KeyboardInterrupt) as raised,
                        ):
                            sys.settrace(interrupt_once)
                            runner.run()
                finally:
                    sys.settrace(None)

                if graceful:
                    self.assertEqual(result, 0)
                else:
                    self.assertIs(raised.exception, interruption)
                self.assertEqual(events[: len(prefix)], prefix)
                self.assertEqual(
                    events[-3:],
                    ["source-close", "lpr-close", "leases-close"],
                )

    def test_cleanup_entry_and_each_stage_resume_after_interrupt(
        self,
    ) -> None:
        cases = (
            (
                FfmpegVideoAlprRunner._finish_run,
                "self._shutdown()",
            ),
            (
                FfmpegVideoAlprRunner._shutdown,
                "self.frame_source.close()",
            ),
            (
                FfmpegVideoAlprRunner._shutdown,
                "self._lpr_shutdown_started = True; self.lpr.close()",
            ),
            (
                FfmpegVideoAlprRunner._shutdown,
                "self.frame_leases.close()",
            ),
        )

        for function, source_text in cases:
            with self.subTest(boundary=source_text):
                events: list[str] = []
                source = _FakeFrameSource([None], events)
                runner, _frames = _runner_for_run(source, events)
                target_code = function.__code__
                target_line = _source_line(function, source_text)
                interruption = KeyboardInterrupt(
                    f"synthetic teardown interrupt: {source_text}"
                )

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
                    with (
                        redirect_stdout(StringIO()),
                        self.assertRaises(KeyboardInterrupt) as raised,
                    ):
                        sys.settrace(interrupt_once)
                        runner.run()
                finally:
                    sys.settrace(None)

                self.assertIs(raised.exception, interruption)
                self.assertEqual(
                    events[-3:],
                    ["source-close", "lpr-close", "leases-close"],
                )
                self.assertTrue(runner._source_shutdown)
                self.assertTrue(runner._lpr_shutdown)
                self.assertTrue(runner._leases_shutdown)

    def test_ambiguous_native_destroy_is_never_retried(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)
        interruption = KeyboardInterrupt(
            "synthetic post-destroy interrupt"
        )
        native_calls = 0

        def destroy_then_interrupt() -> None:
            nonlocal native_calls
            native_calls += 1
            events.append("native-destroy-side-effect")
            raise interruption

        runner.lpr.close = destroy_then_interrupt
        with (
            redirect_stdout(StringIO()),
            self.assertRaises(BaseExceptionGroup) as raised,
        ):
            runner.run()

        self.assertEqual(native_calls, 1)
        self.assertIs(raised.exception.exceptions[0], interruption)
        self.assertIsInstance(
            raised.exception.exceptions[1],
            ffmpeg_video._NativeShutdownAmbiguous,
        )
        self.assertTrue(runner._source_shutdown)
        self.assertTrue(runner._lpr_shutdown_started)
        self.assertFalse(runner._lpr_shutdown)
        self.assertFalse(runner._leases_shutdown)
        self.assertEqual(
            events[-2:],
            ["source-close", "native-destroy-side-effect"],
        )
        self.assertNotIn("leases-close", events)

    def test_late_finish_interrupt_cannot_erase_body_failure(self) -> None:
        cases = (
            "self._run_outcome_ready = True",
            "return self._resolve_run_outcome()",
        )
        for source_text in cases:
            with self.subTest(boundary=source_text):
                events: list[str] = []
                body_failure = RuntimeError("synthetic body failure")
                source = _FakeFrameSource([body_failure], events)
                runner, _frames = _runner_for_run(source, events)
                target_code = FfmpegVideoAlprRunner._finish_run.__code__
                target_line = _source_line(
                    FfmpegVideoAlprRunner._finish_run,
                    source_text,
                )
                interruption = KeyboardInterrupt(
                    f"synthetic late interrupt: {source_text}"
                )

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
                    with (
                        redirect_stdout(StringIO()),
                        self.assertRaises(BaseException) as raised,
                    ):
                        sys.settrace(interrupt_once)
                        runner.run()
                finally:
                    sys.settrace(None)

                if source_text == "self._run_outcome_ready = True":
                    self.assertIsInstance(
                        raised.exception,
                        BaseExceptionGroup,
                    )
                    group = raised.exception
                    if not isinstance(group, BaseExceptionGroup):
                        self.fail("late outcome failure was not grouped")
                    self.assertIs(group.exceptions[0], body_failure)
                    self.assertIs(group.exceptions[1], interruption)
                else:
                    self.assertIs(raised.exception, body_failure)
                self.assertEqual(
                    events.count("source-close"),
                    1,
                )
                self.assertEqual(events.count("lpr-close"), 1)
                self.assertEqual(events.count("leases-close"), 1)

    def test_signal_handler_uses_only_reentrant_flag_assignment(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)

        with patch.object(
            runner.stop_event,
            "set",
            side_effect=AssertionError("Event.set must not run in handler"),
        ):
            for _attempt in range(1_000):
                runner._handle_termination_signal(signal.SIGTERM, None)

        self.assertTrue(runner.stop_event.is_set())

    def test_interrupted_signal_install_restores_every_handler(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        original_signal = signal.signal
        interruption = KeyboardInterrupt(
            "synthetic post-install interrupt"
        )
        calls = 0

        def install_then_interrupt(
            signum: int,
            handler: object,
        ) -> object:
            nonlocal calls
            calls += 1
            result = original_signal(signum, handler)
            if calls == 2:
                raise interruption
            return result

        with (
            patch.object(
                ffmpeg_video.signal,
                "signal",
                side_effect=install_then_interrupt,
            ),
            redirect_stdout(StringIO()),
        ):
            result = runner.run()

        self.assertEqual(result, 0)
        self.assertIsNone(runner._previous_signal_handlers)
        for signum, handler in previous.items():
            self.assertIs(signal.getsignal(signum), handler)
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_partial_signal_restore_is_retried_to_completion(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        original_signal = signal.signal
        interruption = KeyboardInterrupt(
            "synthetic post-restore interrupt"
        )
        calls = 0

        def restore_then_interrupt(
            signum: int,
            handler: object,
        ) -> object:
            nonlocal calls
            calls += 1
            result = original_signal(signum, handler)
            if calls == 3:
                raise interruption
            return result

        with (
            patch.object(
                ffmpeg_video.signal,
                "signal",
                side_effect=restore_then_interrupt,
            ),
            redirect_stdout(StringIO()),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            runner.run()

        self.assertIs(raised.exception, interruption)
        self.assertEqual(calls, 5)
        self.assertIsNone(runner._previous_signal_handlers)
        for signum, handler in previous.items():
            self.assertIs(signal.getsignal(signum), handler)
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_sigterm_requests_stop_and_restores_previous_handler(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([], events)
        runner, frames = _runner_for_run(source, events)
        previous_handler = signal.getsignal(signal.SIGTERM)

        def terminate_and_return_frame() -> bytes:
            os.kill(os.getpid(), signal.SIGTERM)
            return b"rgb"

        source._outcomes.append(terminate_and_return_frame)
        with redirect_stdout(StringIO()):
            result = runner.run()

        self.assertEqual(result, 0)
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(frames, [])
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_external_stop_during_read_drops_just_returned_frame(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([], events)
        runner, frames = _runner_for_run(source, events)

        def stop_and_return_frame() -> bytes:
            runner.stop_event.set()
            return b"rgb"

        source._outcomes.append(stop_and_return_frame)
        with redirect_stdout(StringIO()):
            self.assertEqual(runner.run(), 0)

        self.assertEqual(frames, [])
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_callback_failure_before_read_is_safe_and_stops_ingestion(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)
        runner.callback_failure = PLATE_CALLBACK_FAILURE

        with redirect_stdout(StringIO()), self.assertRaisesRegex(
            RuntimeError,
            f"native callback failed: {PLATE_CALLBACK_FAILURE}",
        ):
            runner.run()

        self.assertNotIn("source-read", events)
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_callback_failure_during_read_prevents_one_more_handoff(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([], events)
        runner, frames = _runner_for_run(source, events)

        def fail_and_return_frame() -> bytes:
            runner.callback_failure = PLATE_CALLBACK_FAILURE
            runner.stop_event.set()
            return b"rgb"

        source._outcomes.append(fail_and_return_frame)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(
            RuntimeError,
            f"native callback failed: {PLATE_CALLBACK_FAILURE}",
        ):
            runner.run()

        self.assertEqual(frames, [])
        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_callback_failure_predicate_is_idempotent_after_interrupt(self) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)
        runner.callback_failure = PLATE_CALLBACK_FAILURE

        for _attempt in range(2):
            with self.assertRaisesRegex(
                RuntimeError,
                f"native callback failed: {PLATE_CALLBACK_FAILURE}",
            ):
                runner._raise_for_callback_failure()

        original_error = ffmpeg_video._NativeCallbackFailure
        with patch.object(
            ffmpeg_video,
            "_NativeCallbackFailure",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner._raise_for_callback_failure()
        with self.assertRaises(original_error):
            runner._raise_for_callback_failure()

    def test_last_callback_failure_is_checked_after_adapter_quiescence(
        self,
    ) -> None:
        events: list[str] = []
        source = _FakeFrameSource([None], events)
        runner, _frames = _runner_for_run(source, events)

        def fail_during_close() -> None:
            runner.callback_failure = PLATE_CALLBACK_FAILURE

        runner.lpr = SimpleNamespace(
            version=lambda: "synthetic-version",
            close=lambda: (
                events.append("lpr-close"),
                fail_during_close(),
            ),
        )

        with redirect_stdout(StringIO()), self.assertRaisesRegex(
            RuntimeError,
            f"native callback failed: {PLATE_CALLBACK_FAILURE}",
        ):
            runner.run()

        self.assertEqual(
            events[-3:],
            ["source-close", "lpr-close", "leases-close"],
        )

    def test_status_contains_only_source_safe_supervisor_receipt(self) -> None:
        events: list[str] = []
        safe_receipt = {
            "failure_code": None,
            "frame": FrameSpec(1, 1, 1).to_receipt(),
            "frames_delivered": 1,
            "schema_version": 1,
            "source": {"kind": "rtsp"},
            "state": "running",
            "stderr_bytes": 0,
            "stdout_bytes": 3,
        }
        source = _FakeFrameSource(
            [b"rgb", None],
            events,
            receipt=safe_receipt,
        )
        runner, _frames = _runner_for_run(
            source,
            events,
            status_print_interval=0.001,
        )
        statuses: list[dict[str, object]] = []

        def capture_status(_path: Path, value: dict[str, object]) -> None:
            statuses.append(value)

        with (
            patch.object(ffmpeg_video, "atomic_json", side_effect=capture_status),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(runner.run(), 0)

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["ffmpeg"], safe_receipt)
        serialized = json.dumps(statuses[0], sort_keys=True)
        self.assertNotIn(PRIVATE_RTSP, serialized)
        self.assertNotIn("synthetic-secret", serialized)
        self.assertNotIn("token=private", serialized)


if __name__ == "__main__":
    unittest.main()
