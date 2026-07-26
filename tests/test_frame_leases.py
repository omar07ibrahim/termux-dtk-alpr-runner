from __future__ import annotations

import ctypes
import gc
import signal
import subprocess
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from alpr_runner.ffmpeg_video import FrameLeasePool, FfmpegVideoAlprRunner


def _buffer(value: int) -> ctypes.Array:
    return ctypes.create_string_buffer(bytes([value % 256]) * 3)


class FrameLeasePoolTests(unittest.TestCase):
    def test_ninth_frame_applies_backpressure_without_evicting_inflight_buffers(
        self,
    ) -> None:
        pool = FrameLeasePool(8)
        leased_refs: list[weakref.ReferenceType[ctypes.Array]] = []

        for frame_id in range(1, 9):
            buffer = _buffer(frame_id)
            leased_refs.append(weakref.ref(buffer))
            self.assertTrue(pool.try_acquire(frame_id, buffer))
        del buffer

        ninth = _buffer(9)
        ninth_ref = weakref.ref(ninth)
        self.assertFalse(pool.try_acquire(9, ninth))
        del ninth
        gc.collect()

        self.assertEqual(pool.active_ids, tuple(range(1, 9)))
        self.assertEqual(len(pool), 8)
        self.assertTrue(all(reference() is not None for reference in leased_refs))
        self.assertIsNone(ninth_ref())

    def test_out_of_order_completion_releases_only_the_matching_buffer(
        self,
    ) -> None:
        pool = FrameLeasePool(3)
        refs: dict[int, weakref.ReferenceType[ctypes.Array]] = {}
        for frame_id in (10, 20, 30):
            buffer = _buffer(frame_id)
            refs[frame_id] = weakref.ref(buffer)
            self.assertTrue(pool.try_acquire(frame_id, buffer))
        del buffer

        self.assertTrue(pool.acknowledge(20))
        gc.collect()

        self.assertEqual(pool.active_ids, (10, 30))
        self.assertIsNone(refs[20]())
        self.assertIsNotNone(refs[10]())
        self.assertIsNotNone(refs[30]())

        replacement = _buffer(40)
        replacement_ref = weakref.ref(replacement)
        self.assertTrue(pool.try_acquire(40, replacement))
        del replacement

        self.assertEqual(pool.active_ids, (10, 30, 40))
        self.assertTrue(pool.acknowledge(30))
        gc.collect()
        self.assertIsNone(refs[30]())
        self.assertIsNotNone(refs[10]())
        self.assertIsNotNone(replacement_ref())

    def test_duplicate_and_unknown_acknowledgements_are_noops(self) -> None:
        pool = FrameLeasePool(2)
        buffer = _buffer(1)
        reference = weakref.ref(buffer)
        self.assertTrue(pool.try_acquire(1, buffer))
        del buffer

        self.assertFalse(pool.acknowledge(999))
        self.assertEqual(pool.active_ids, (1,))
        self.assertIsNotNone(reference())

        self.assertTrue(pool.acknowledge(1))
        self.assertFalse(pool.acknowledge(1))
        self.assertFalse(pool.acknowledge(-1))
        self.assertEqual(pool.active_ids, ())
        gc.collect()
        self.assertIsNone(reference())

    def test_close_releases_all_buffers_and_is_idempotent(self) -> None:
        pool = FrameLeasePool(2)
        first = _buffer(1)
        second = _buffer(2)
        first_ref = weakref.ref(first)
        second_ref = weakref.ref(second)
        self.assertTrue(pool.try_acquire(1, first))
        self.assertTrue(pool.try_acquire(2, second))
        del first, second

        self.assertEqual(pool.close(), 2)
        self.assertEqual(pool.close(), 0)
        self.assertTrue(pool.closed)
        self.assertEqual(pool.active_ids, ())
        self.assertFalse(pool.acknowledge(1))
        gc.collect()
        self.assertIsNone(first_ref())
        self.assertIsNone(second_ref())

        with self.assertRaisesRegex(RuntimeError, "pool is closed"):
            pool.try_acquire(3, _buffer(3))

    def test_constructor_and_operations_enforce_strict_types(self) -> None:
        for invalid in (True, 1.0, "1", None):
            with self.subTest(capacity=invalid):
                with self.assertRaises(TypeError):
                    FrameLeasePool(invalid)  # type: ignore[arg-type]
        for invalid in (0, -1):
            with self.subTest(capacity=invalid):
                with self.assertRaises(ValueError):
                    FrameLeasePool(invalid)

        pool = FrameLeasePool(1)
        buffer = _buffer(1)
        for invalid in (True, 1.0, "1", None):
            with self.subTest(frame_id=invalid):
                with self.assertRaises(TypeError):
                    pool.try_acquire(invalid, buffer)  # type: ignore[arg-type]
                with self.assertRaises(TypeError):
                    pool.acknowledge(invalid)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            pool.try_acquire(0, buffer)
        with self.assertRaises(TypeError):
            pool.try_acquire(1, b"not-a-ctypes-array")  # type: ignore[arg-type]

        self.assertTrue(pool.try_acquire(1, buffer))
        with self.assertRaisesRegex(ValueError, "already has an active lease"):
            pool.try_acquire(1, _buffer(2))
        self.assertEqual(pool.active_ids, (1,))

    def test_concurrent_acquisition_never_exceeds_capacity(self) -> None:
        capacity = 4
        contender_count = 16
        pool = FrameLeasePool(capacity)
        barrier = threading.Barrier(contender_count + 1)

        def contend(frame_id: int) -> tuple[int, bool]:
            buffer = _buffer(frame_id)
            barrier.wait()
            return frame_id, pool.try_acquire(frame_id, buffer)

        with ThreadPoolExecutor(max_workers=contender_count) as executor:
            futures = [
                executor.submit(contend, frame_id)
                for frame_id in range(1, contender_count + 1)
            ]
            barrier.wait()
            outcomes = [future.result() for future in futures]

        accepted = {frame_id for frame_id, result in outcomes if result}
        self.assertEqual(len(accepted), capacity)
        self.assertEqual(set(pool.active_ids), accepted)
        self.assertEqual(len(pool), capacity)
        for frame_id in accepted:
            self.assertTrue(pool.acknowledge(frame_id))
        self.assertEqual(len(pool), 0)


class _TrackingFrameLeasePool(FrameLeasePool):
    def __init__(self, capacity: int) -> None:
        super().__init__(capacity)
        self.references: dict[int, weakref.ReferenceType[ctypes.Array]] = {}

    def try_acquire(self, frame_id: int, buffer: ctypes.Array) -> bool:
        acquired = super().try_acquire(frame_id, buffer)
        if acquired:
            self.references[frame_id] = weakref.ref(buffer)
        return acquired


class _FakeVideoApi:
    def __init__(self) -> None:
        self.created: list[int] = []
        self.released: list[int] = []
        self.create_failures: set[int] = set()
        self.create_error_ids: set[int] = set()
        self.release_failures: set[int] = set()
        self.release_error_ids: set[int] = set()

    def VideoFrame_CreateFromImageBuffer(
        self,
        _address: ctypes.c_void_p,
        _width: int,
        _height: int,
        _stride: int,
        _pixel_format: int,
        frame_id: int,
    ) -> int:
        self.created.append(frame_id)
        if frame_id in self.create_error_ids:
            raise RuntimeError("synthetic create error")
        return 0 if frame_id in self.create_failures else frame_id

    def VideoFrame_Release(self, frame: int) -> int:
        self.released.append(frame)
        if frame in self.release_error_ids:
            raise RuntimeError("synthetic release error")
        return 1 if frame in self.release_failures else 0

    @staticmethod
    def VideoFrame_Timestamp(frame: int) -> int:
        return frame


class _FakeLprApi:
    def __init__(self) -> None:
        self.accepted: list[int] = []
        self.rejections: set[int] = set()
        self.error_ids: set[int] = set()

    def LPREngine_PutFrame(
        self,
        _engine: object,
        _frame: int,
        frame_id: int,
    ) -> int:
        self.accepted.append(frame_id)
        if frame_id in self.error_ids:
            raise RuntimeError("synthetic handoff error")
        return 1 if frame_id in self.rejections else 0


def _runner(capacity: int = 8) -> tuple[FfmpegVideoAlprRunner, _FakeVideoApi, _FakeLprApi]:
    video_api = _FakeVideoApi()
    lpr_api = _FakeLprApi()
    runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
    runner.args = SimpleNamespace(width=1, height=1, preview_every=0)
    runner.out_dir = Path(".")
    runner.lock = threading.RLock()
    runner.frame_count = 0
    runner.completed_count = 0
    runner.dropped_count = 0
    runner.latest_frame_bytes = None
    runner.frame_leases = _TrackingFrameLeasePool(capacity)
    runner.pixel_format = 2
    runner.video_lib = SimpleNamespace(lib=video_api)
    runner.lpr = SimpleNamespace(lib=lpr_api, engine=object())
    return runner, video_api, lpr_api


def _lease_reference(
    runner: FfmpegVideoAlprRunner,
    frame_id: int,
) -> weakref.ReferenceType[ctypes.Array]:
    pool = runner.frame_leases
    if not isinstance(pool, _TrackingFrameLeasePool):
        raise AssertionError("runner does not use the tracking test pool")
    return pool.references[frame_id]


class FfmpegRunnerLeaseIntegrationTests(unittest.TestCase):
    def test_full_pool_rejects_ninth_native_handoff_and_recovers_after_ack(
        self,
    ) -> None:
        runner, video_api, lpr_api = _runner()

        for _ in range(9):
            runner._put_raw_frame(b"rgb")

        self.assertEqual(video_api.created, list(range(1, 9)))
        self.assertEqual(lpr_api.accepted, list(range(1, 9)))
        self.assertEqual(runner.frame_leases.active_ids, tuple(range(1, 9)))
        self.assertEqual(runner.frame_count, 9)
        self.assertEqual(runner.dropped_count, 1)

        runner._on_frame_completed(None, 4, 0)
        runner._on_frame_completed(None, 4, 7)
        runner._on_frame_completed(None, 999, 7)
        self.assertEqual(runner.completed_count, 1)
        self.assertEqual(runner.dropped_count, 1)
        self.assertEqual(runner.frame_leases.active_ids, (1, 2, 3, 5, 6, 7, 8))

        runner._put_raw_frame(b"rgb")
        self.assertEqual(video_api.created[-1], 10)
        self.assertEqual(lpr_api.accepted[-1], 10)
        self.assertEqual(runner.frame_leases.active_ids, (1, 2, 3, 5, 6, 7, 8, 10))

    def test_definite_create_and_handoff_rejections_cancel_exact_lease(self) -> None:
        runner, video_api, lpr_api = _runner()
        video_api.create_failures.add(1)
        lpr_api.rejections.add(2)

        runner._put_raw_frame(b"rgb")
        create_reference = _lease_reference(runner, 1)
        with redirect_stdout(StringIO()):
            runner._put_raw_frame(b"rgb")
        handoff_reference = _lease_reference(runner, 2)

        self.assertEqual(runner.frame_leases.active_ids, ())
        self.assertEqual(video_api.released, [2])
        self.assertEqual(runner.dropped_count, 1)
        gc.collect()
        self.assertIsNone(create_reference())
        self.assertIsNone(handoff_reference())

    def test_create_exception_retains_ambiguous_buffer_until_shutdown(self) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        video_api.create_error_ids.add(1)

        with self.assertRaisesRegex(RuntimeError, "synthetic create error"):
            runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)

        gc.collect()
        self.assertIsNotNone(reference())
        self.assertEqual(runner.frame_leases.active_ids, (1,))
        self.assertEqual(video_api.created, [1])
        self.assertEqual(video_api.released, [])
        self.assertEqual(lpr_api.accepted, [])

        runner._put_raw_frame(b"rgb")
        self.assertEqual(video_api.created, [1])
        self.assertEqual(runner.dropped_count, 1)
        self.assertEqual(runner.frame_leases.close(), 1)
        gc.collect()
        self.assertIsNone(reference())

    def test_after_accept_handoff_exception_retains_buffer_until_shutdown(
        self,
    ) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        lpr_api.error_ids.add(1)

        with self.assertRaisesRegex(RuntimeError, "synthetic handoff error"):
            runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)

        gc.collect()
        self.assertIsNotNone(reference())
        self.assertEqual(runner.frame_leases.active_ids, (1,))
        self.assertEqual(video_api.created, [1])
        self.assertEqual(lpr_api.accepted, [1])
        self.assertEqual(video_api.released, [])

        runner._put_raw_frame(b"rgb")
        self.assertEqual(video_api.created, [1])
        self.assertEqual(runner.dropped_count, 1)
        self.assertEqual(runner.frame_leases.close(), 1)
        gc.collect()
        self.assertIsNone(reference())

    def test_preview_failure_releases_definitely_unsubmitted_frame(self) -> None:
        runner, video_api, lpr_api = _runner()
        runner.args.preview_every = 1

        def fail_preview(_data: bytes, _path: Path) -> None:
            raise RuntimeError("synthetic preview error")

        runner._save_raw_frame = fail_preview  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "synthetic preview error"):
            runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)

        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(runner.frame_leases.active_ids, ())
        self.assertEqual(video_api.created, [1])
        self.assertEqual(video_api.released, [1])
        self.assertEqual(lpr_api.accepted, [])

    def test_nonzero_native_release_retains_buffer_and_backpressure(self) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        lpr_api.rejections.add(1)
        video_api.release_failures.add(1)

        with redirect_stdout(StringIO()):
            runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)

        gc.collect()
        self.assertIsNotNone(reference())
        self.assertEqual(runner.frame_leases.active_ids, (1,))
        self.assertEqual(video_api.released, [1])
        self.assertEqual(runner.dropped_count, 1)

        runner._put_raw_frame(b"rgb")
        self.assertEqual(video_api.created, [1])
        self.assertEqual(lpr_api.accepted, [1])
        self.assertEqual(runner.frame_leases.active_ids, (1,))
        self.assertEqual(runner.dropped_count, 2)
        self.assertEqual(runner.frame_leases.close(), 1)
        gc.collect()
        self.assertIsNone(reference())

    def test_rejected_handoff_release_exception_retains_until_shutdown(self) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        lpr_api.rejections.add(1)
        video_api.release_error_ids.add(1)

        with self.assertRaisesRegex(RuntimeError, "synthetic release error"):
            runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)

        gc.collect()
        self.assertIsNotNone(reference())
        self.assertEqual(runner.frame_leases.active_ids, (1,))
        self.assertEqual(video_api.released, [1])
        self.assertEqual(lpr_api.accepted, [1])
        self.assertEqual(runner.dropped_count, 1)

        self.assertEqual(runner.frame_leases.close(), 1)
        gc.collect()
        self.assertIsNone(reference())

    def test_shutdown_destroys_adapter_before_releasing_leases(self) -> None:
        runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
        events: list[str] = []
        runner._stop_process = lambda _process: events.append("producer-stopped")
        runner.lpr = SimpleNamespace(close=lambda: events.append("adapter-destroyed"))
        runner.frame_leases = SimpleNamespace(
            close=lambda: events.append("leases-released")
        )

        runner._shutdown(object())  # type: ignore[arg-type]

        self.assertEqual(
            events,
            ["producer-stopped", "adapter-destroyed", "leases-released"],
        )

    def test_failed_adapter_destruction_keeps_outstanding_lease(self) -> None:
        runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
        pool = _TrackingFrameLeasePool(1)
        buffer = _buffer(1)
        self.assertTrue(pool.try_acquire(1, buffer))
        reference = pool.references[1]
        del buffer

        runner._stop_process = lambda _process: None

        def fail_close() -> None:
            raise RuntimeError("synthetic destroy error")

        runner.lpr = SimpleNamespace(close=fail_close)
        runner.frame_leases = pool

        with self.assertRaisesRegex(RuntimeError, "synthetic destroy error"):
            runner._shutdown(object())  # type: ignore[arg-type]

        gc.collect()
        self.assertFalse(pool.closed)
        self.assertEqual(pool.active_ids, (1,))
        self.assertIsNotNone(reference())
        self.assertEqual(pool.close(), 1)

    def test_forced_process_stop_is_reaped_before_adapter_destruction(
        self,
    ) -> None:
        events: list[str] = []

        class Process:
            pid = 7001

            @staticmethod
            def poll() -> None:
                events.append("poll")
                return None

            @staticmethod
            def wait(*, timeout: int) -> int:
                events.append(f"wait:{timeout}")
                if events.count("wait:2") == 1:
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                events.append("producer-reaped")
                return -signal.SIGKILL

        def kill_group(group_id: int, sent_signal: signal.Signals) -> None:
            events.append(f"signal:{group_id}:{sent_signal.name}")

        runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
        runner.lpr = SimpleNamespace(
            close=lambda: events.append("adapter-destroyed")
        )
        runner.frame_leases = SimpleNamespace(
            close=lambda: events.append("leases-released")
        )

        with (
            patch(
                "alpr_runner.ffmpeg_video.os.getpgid",
                return_value=7001,
            ),
            patch(
                "alpr_runner.ffmpeg_video.os.killpg",
                side_effect=kill_group,
            ),
        ):
            runner._shutdown(Process())  # type: ignore[arg-type]

        self.assertEqual(
            events,
            [
                "poll",
                "signal:7001:SIGTERM",
                "wait:2",
                "signal:7001:SIGKILL",
                "wait:2",
                "producer-reaped",
                "adapter-destroyed",
                "leases-released",
            ],
        )

    def test_unconfirmed_process_stop_keeps_adapter_and_leases_alive(
        self,
    ) -> None:
        events: list[str] = []
        pool = _TrackingFrameLeasePool(1)
        buffer = _buffer(1)
        self.assertTrue(pool.try_acquire(1, buffer))
        reference = pool.references[1]
        del buffer

        class Process:
            pid = 7002

            @staticmethod
            def poll() -> None:
                return None

            @staticmethod
            def wait(*, timeout: int) -> int:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)

        runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
        runner.lpr = SimpleNamespace(
            close=lambda: events.append("adapter-destroyed")
        )
        runner.frame_leases = pool

        with (
            patch(
                "alpr_runner.ffmpeg_video.os.getpgid",
                return_value=7002,
            ),
            patch("alpr_runner.ffmpeg_video.os.killpg"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            runner._shutdown(Process())  # type: ignore[arg-type]

        gc.collect()
        self.assertEqual(events, [])
        self.assertFalse(pool.closed)
        self.assertEqual(pool.active_ids, (1,))
        self.assertIsNotNone(reference())
        self.assertEqual(pool.close(), 1)

    def test_exit_during_process_group_lookup_is_reaped_before_destroy(
        self,
    ) -> None:
        events: list[str] = []

        class Process:
            pid = 7003

            @staticmethod
            def poll() -> None:
                events.append("poll")
                return None

            @staticmethod
            def wait(*, timeout: int) -> int:
                events.append(f"wait:{timeout}")
                events.append("producer-reaped")
                return 0

        runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
        runner.lpr = SimpleNamespace(
            close=lambda: events.append("adapter-destroyed")
        )
        runner.frame_leases = SimpleNamespace(
            close=lambda: events.append("leases-released")
        )

        with patch(
            "alpr_runner.ffmpeg_video.os.getpgid",
            side_effect=ProcessLookupError,
        ):
            runner._shutdown(Process())  # type: ignore[arg-type]

        self.assertEqual(
            events,
            [
                "poll",
                "wait:2",
                "producer-reaped",
                "adapter-destroyed",
                "leases-released",
            ],
        )
