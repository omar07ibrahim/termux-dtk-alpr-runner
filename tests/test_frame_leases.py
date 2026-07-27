from __future__ import annotations

import ctypes
import gc
import signal
import subprocess
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from alpr_runner.dtk import Plate
from alpr_runner.ffmpeg_video import (
    COMPLETED_CALLBACK_FAILURE,
    DEFAULT_BUFFER_RETAIN,
    DEFAULT_MAX_INFLIGHT_FRAME_MIB,
    MEBIBYTE,
    PLATE_CALLBACK_FAILURE,
    FrameLease,
    FrameLeasePool,
    FfmpegVideoAlprRunner,
)
from alpr_runner.zoom import ZoomController


def _buffer(value: int) -> ctypes.Array:
    payload = bytes([value % 256]) * 3
    return ctypes.create_string_buffer(payload, len(payload))


def _payload(value: int) -> bytes:
    return bytes([value % 256]) * 3


class FrameLeasePoolTests(unittest.TestCase):
    def test_lease_is_frozen_and_reuses_the_same_immutable_payload(self) -> None:
        pool = FrameLeasePool(1, 6)
        payload = b"rgb"
        buffer = ctypes.create_string_buffer(payload, len(payload))

        self.assertTrue(pool.try_acquire(1, buffer, payload))
        self.assertEqual(pool.retained_bytes, 6)
        self.assertIs(pool.copy_payload(1, 3), payload)
        self.assertIsNone(pool.copy_payload(1, 4))

        lease = pool.acknowledge(1)
        self.assertIsInstance(lease, FrameLease)
        if lease is None:
            self.fail("live lease unexpectedly absent")
        self.assertIs(lease.native_buffer, buffer)
        self.assertIs(lease.payload, payload)
        self.assertEqual(ctypes.sizeof(lease.native_buffer), len(payload))
        self.assertEqual(pool.retained_bytes, 6)
        self.assertTrue(pool.release_payload(1, payload))
        self.assertEqual(pool.retained_bytes, 0)
        with self.assertRaises(FrozenInstanceError):
            lease.payload = b"changed"  # type: ignore[misc]

    def test_byte_budget_applies_without_corrupting_exact_accounting(self) -> None:
        pool = FrameLeasePool(3, 12)
        first = _buffer(1)
        second = _buffer(2)
        third = _buffer(3)

        self.assertTrue(pool.try_acquire(1, first, _payload(1)))
        self.assertTrue(pool.try_acquire(2, second, _payload(2)))
        self.assertEqual(pool.retained_bytes, 12)
        self.assertFalse(pool.try_acquire(3, third, _payload(3)))
        self.assertEqual(pool.active_ids, (1, 2))
        self.assertEqual(pool.retained_bytes, 12)

        released = pool.cancel(1)
        self.assertIsInstance(released, FrameLease)
        self.assertEqual(pool.retained_bytes, 6)
        self.assertIsNone(pool.cancel(999))
        self.assertEqual(pool.retained_bytes, 6)
        self.assertTrue(pool.try_acquire(3, third, _payload(3)))
        self.assertEqual(pool.retained_bytes, 12)
        self.assertEqual(pool.close(), 2)
        self.assertEqual(pool.retained_bytes, 0)

    def test_retired_borrows_remain_charged_and_block_admission(self) -> None:
        pool = FrameLeasePool(1, 6)
        payload = b"rgb"
        self.assertTrue(
            pool.try_acquire(
                1,
                ctypes.create_string_buffer(payload, len(payload)),
                payload,
            )
        )
        self.assertIs(pool.copy_payload(1, 3), payload)
        self.assertIsInstance(pool.acknowledge(1), FrameLease)

        self.assertEqual(pool.active_ids, ())
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.retained_bytes, 6)
        self.assertFalse(pool.try_acquire(2, _buffer(2), _payload(2)))
        equal_but_distinct = bytes(bytearray(payload))
        self.assertIsNot(equal_but_distinct, payload)
        self.assertFalse(pool.release_payload(1, equal_but_distinct))
        self.assertFalse(pool.release_payload(999, payload))
        self.assertEqual(pool.retained_bytes, 6)

        self.assertTrue(pool.release_payload(1, payload))
        self.assertEqual(len(pool), 0)
        self.assertEqual(pool.retained_bytes, 0)
        self.assertFalse(pool.release_payload(1, payload))
        self.assertTrue(pool.try_acquire(2, _buffer(2), _payload(2)))

    def test_multiple_borrows_debit_retired_lease_only_after_last_release(
        self,
    ) -> None:
        pool = FrameLeasePool(1, 6)
        payload = b"rgb"
        self.assertTrue(
            pool.try_acquire(
                1,
                ctypes.create_string_buffer(payload, len(payload)),
                payload,
            )
        )
        self.assertIs(pool.copy_payload(1, 3), payload)
        self.assertIs(pool.copy_payload(1, 3), payload)
        self.assertIsInstance(pool.acknowledge(1), FrameLease)

        self.assertTrue(pool.release_payload(1, payload))
        self.assertEqual(pool.retained_bytes, 6)
        self.assertFalse(pool.try_acquire(2, _buffer(2), _payload(2)))
        self.assertTrue(pool.release_payload(1, payload))
        self.assertEqual(pool.retained_bytes, 0)
        self.assertTrue(pool.try_acquire(2, _buffer(2), _payload(2)))

    def test_cancel_and_close_handle_borrowed_leases_without_underflow(
        self,
    ) -> None:
        pool = FrameLeasePool(2, 12)
        first = _payload(1)
        second = _payload(2)
        self.assertTrue(pool.try_acquire(1, _buffer(1), first))
        self.assertTrue(pool.try_acquire(2, _buffer(2), second))
        self.assertIs(pool.copy_payload(1, 3), first)
        self.assertIs(pool.copy_payload(2, 3), second)

        self.assertIsInstance(pool.cancel(1), FrameLease)
        self.assertEqual(pool.retained_bytes, 12)
        self.assertTrue(pool.release_payload(1, first))
        self.assertEqual(pool.retained_bytes, 6)
        self.assertIsInstance(pool.acknowledge(2), FrameLease)
        self.assertEqual(pool.retained_bytes, 6)
        self.assertEqual(pool.close(), 1)
        self.assertEqual(pool.retained_bytes, 0)
        self.assertFalse(pool.release_payload(2, second))
        self.assertFalse(pool.cancel(2))
        self.assertFalse(pool.acknowledge(2))

    def test_payload_lookup_enforces_strict_size_types(self) -> None:
        pool = FrameLeasePool(1, 6)
        payload = _payload(1)
        self.assertTrue(pool.try_acquire(1, _buffer(1), payload))

        for invalid in (True, 3.0, "3", None):
            with self.subTest(expected_size=invalid):
                with self.assertRaises(TypeError):
                    pool.copy_payload(1, invalid)  # type: ignore[arg-type]
        for invalid in (0, -1):
            with self.subTest(expected_size=invalid):
                with self.assertRaises(ValueError):
                    pool.copy_payload(1, invalid)
        self.assertIs(pool.copy_payload(1, 3), payload)
        self.assertTrue(pool.release_payload(1, payload))

    def test_completion_race_returns_exact_payload_or_none_only(self) -> None:
        for frame_id in range(1, 101):
            pool = FrameLeasePool(1, 6)
            payload = _payload(frame_id)
            self.assertTrue(
                pool.try_acquire(
                    frame_id,
                    ctypes.create_string_buffer(payload, len(payload)),
                    payload,
                )
            )
            barrier = threading.Barrier(3)

            def read_payload() -> bytes | None:
                barrier.wait()
                return pool.copy_payload(frame_id, len(payload))

            def acknowledge() -> FrameLease | None:
                barrier.wait()
                return pool.acknowledge(frame_id)

            with ThreadPoolExecutor(max_workers=2) as executor:
                read_future = executor.submit(read_payload)
                ack_future = executor.submit(acknowledge)
                barrier.wait()
                copied = read_future.result()
                released = ack_future.result()

            self.assertTrue(copied is None or copied is payload)
            self.assertIsInstance(released, FrameLease)
            if copied is not None:
                self.assertTrue(pool.release_payload(frame_id, copied))
            self.assertEqual(pool.retained_bytes, 0)

    def test_ninth_frame_applies_backpressure_without_evicting_inflight_buffers(
        self,
    ) -> None:
        pool = FrameLeasePool(8, 48)
        leased_refs: list[weakref.ReferenceType[ctypes.Array]] = []

        for frame_id in range(1, 9):
            buffer = _buffer(frame_id)
            leased_refs.append(weakref.ref(buffer))
            self.assertTrue(pool.try_acquire(frame_id, buffer, _payload(frame_id)))
        del buffer

        ninth = _buffer(9)
        ninth_ref = weakref.ref(ninth)
        self.assertFalse(pool.try_acquire(9, ninth, _payload(9)))
        del ninth
        gc.collect()

        self.assertEqual(pool.active_ids, tuple(range(1, 9)))
        self.assertEqual(len(pool), 8)
        self.assertTrue(all(reference() is not None for reference in leased_refs))
        self.assertIsNone(ninth_ref())

    def test_out_of_order_completion_releases_only_the_matching_buffer(
        self,
    ) -> None:
        pool = FrameLeasePool(3, 18)
        refs: dict[int, weakref.ReferenceType[ctypes.Array]] = {}
        for frame_id in (10, 20, 30):
            buffer = _buffer(frame_id)
            refs[frame_id] = weakref.ref(buffer)
            self.assertTrue(pool.try_acquire(frame_id, buffer, _payload(frame_id)))
        del buffer

        self.assertTrue(pool.acknowledge(20))
        gc.collect()

        self.assertEqual(pool.active_ids, (10, 30))
        self.assertIsNone(refs[20]())
        self.assertIsNotNone(refs[10]())
        self.assertIsNotNone(refs[30]())

        replacement = _buffer(40)
        replacement_ref = weakref.ref(replacement)
        self.assertTrue(pool.try_acquire(40, replacement, _payload(40)))
        del replacement

        self.assertEqual(pool.active_ids, (10, 30, 40))
        self.assertTrue(pool.acknowledge(30))
        gc.collect()
        self.assertIsNone(refs[30]())
        self.assertIsNotNone(refs[10]())
        self.assertIsNotNone(replacement_ref())

    def test_duplicate_and_unknown_acknowledgements_are_noops(self) -> None:
        pool = FrameLeasePool(2, 12)
        buffer = _buffer(1)
        reference = weakref.ref(buffer)
        self.assertTrue(pool.try_acquire(1, buffer, _payload(1)))
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
        pool = FrameLeasePool(2, 12)
        first = _buffer(1)
        second = _buffer(2)
        first_ref = weakref.ref(first)
        second_ref = weakref.ref(second)
        self.assertTrue(pool.try_acquire(1, first, _payload(1)))
        self.assertTrue(pool.try_acquire(2, second, _payload(2)))
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
            pool.try_acquire(3, _buffer(3), _payload(3))

    def test_constructor_and_operations_enforce_strict_types(self) -> None:
        for invalid in (True, 1.0, "1", None):
            with self.subTest(capacity=invalid):
                with self.assertRaises(TypeError):
                    FrameLeasePool(invalid, 100)  # type: ignore[arg-type]
        for invalid in (0, -1):
            with self.subTest(capacity=invalid):
                with self.assertRaises(ValueError):
                    FrameLeasePool(invalid, 100)

        for invalid in (True, 1.0, "1", None):
            with self.subTest(max_bytes=invalid):
                with self.assertRaises(TypeError):
                    FrameLeasePool(1, invalid)  # type: ignore[arg-type]
        for invalid in (0, -1):
            with self.subTest(max_bytes=invalid):
                with self.assertRaises(ValueError):
                    FrameLeasePool(1, invalid)

        pool = FrameLeasePool(1, 6)
        buffer = _buffer(1)
        for invalid in (True, 1.0, "1", None):
            with self.subTest(frame_id=invalid):
                with self.assertRaises(TypeError):
                    pool.try_acquire(  # type: ignore[arg-type]
                        invalid,
                        buffer,
                        _payload(1),
                    )
                with self.assertRaises(TypeError):
                    pool.acknowledge(invalid)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            pool.try_acquire(0, buffer, _payload(1))
        with self.assertRaises(TypeError):
            pool.try_acquire(  # type: ignore[arg-type]
                1,
                b"not-a-ctypes-array",
                _payload(1),
            )
        with self.assertRaises(TypeError):
            pool.try_acquire(1, buffer, bytearray(b"rgb"))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            pool.try_acquire(1, buffer, b"")
        with self.assertRaises(ValueError):
            pool.try_acquire(1, ctypes.create_string_buffer(b"rgb"), b"rgb")
        with self.assertRaises(TypeError):
            pool.release_payload(1, bytearray(b"rgb"))  # type: ignore[arg-type]

        self.assertTrue(pool.try_acquire(1, buffer, _payload(1)))
        with self.assertRaisesRegex(ValueError, "already has a retained lease"):
            pool.try_acquire(1, _buffer(2), _payload(2))
        self.assertEqual(pool.active_ids, (1,))

    def test_concurrent_acquisition_never_exceeds_capacity(self) -> None:
        capacity = 4
        contender_count = 16
        pool = FrameLeasePool(capacity, capacity * 6)
        barrier = threading.Barrier(contender_count + 1)

        def contend(frame_id: int) -> tuple[int, bool]:
            buffer = _buffer(frame_id)
            barrier.wait()
            return frame_id, pool.try_acquire(
                frame_id,
                buffer,
                _payload(frame_id),
            )

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
        self.assertEqual(pool.retained_bytes, capacity * 6)
        for frame_id in accepted:
            self.assertTrue(pool.acknowledge(frame_id))
        self.assertEqual(len(pool), 0)
        self.assertEqual(pool.retained_bytes, 0)


class _TrackingFrameLeasePool(FrameLeasePool):
    def __init__(self, capacity: int, max_bytes: int | None = None) -> None:
        super().__init__(capacity, max_bytes if max_bytes is not None else capacity * 6)
        self.references: dict[int, weakref.ReferenceType[ctypes.Array]] = {}

    def try_acquire(
        self,
        frame_id: int,
        buffer: ctypes.Array,
        payload: bytes,
    ) -> bool:
        acquired = super().try_acquire(frame_id, buffer, payload)
        if acquired:
            self.references[frame_id] = weakref.ref(buffer)
        return acquired


class _FakeVideoApi:
    def __init__(self) -> None:
        self.created: list[int] = []
        self.released: list[int] = []
        self.handles_by_frame_id: dict[int, int] = {}
        self.timestamp_by_handle: dict[int, int] = {}
        self.create_failures: set[int] = set()
        self.create_error_ids: set[int] = set()
        self.release_failures: set[int] = set()
        self.release_error_ids: set[int] = set()
        self.widths: dict[int, int] = {}
        self.heights: dict[int, int] = {}

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
        if frame_id in self.create_failures:
            return 0
        handle = 10_000 + frame_id * 17
        self.handles_by_frame_id[frame_id] = handle
        self.timestamp_by_handle[handle] = frame_id
        return handle

    def VideoFrame_Release(self, frame: int) -> int:
        self.released.append(frame)
        frame_id = self.timestamp_by_handle[frame]
        if frame_id in self.release_error_ids:
            raise RuntimeError("synthetic release error")
        return 1 if frame_id in self.release_failures else 0

    def VideoFrame_Timestamp(self, frame: int) -> int:
        return self.timestamp_by_handle[frame]

    def VideoFrame_GetWidth(self, frame: int) -> int:
        return self.widths.get(frame, 1)

    def VideoFrame_GetHeight(self, frame: int) -> int:
        return self.heights.get(frame, 1)


class _FakeLprApi:
    def __init__(self) -> None:
        self.accepted: list[int] = []
        self.destroyed_plates: list[int] = []
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

    def LicensePlate_Destroy(self, plate_handle: int) -> None:
        self.destroyed_plates.append(plate_handle)


def _runner(capacity: int = 8) -> tuple[FfmpegVideoAlprRunner, _FakeVideoApi, _FakeLprApi]:
    video_api = _FakeVideoApi()
    lpr_api = _FakeLprApi()
    runner = FfmpegVideoAlprRunner.__new__(FfmpegVideoAlprRunner)
    runner.args = SimpleNamespace(width=1, height=1, preview_every=0)
    runner.out_dir = Path(".")
    runner.lock = threading.RLock()
    runner.callback_lock = threading.Lock()
    runner.stop_event = threading.Event()
    runner.callback_failure = None
    runner.frame_count = 0
    runner.plate_count = 0
    runner.completed_count = 0
    runner.dropped_count = 0
    runner.last_status = {}
    runner.frame_leases = _TrackingFrameLeasePool(capacity)
    runner.pixel_format = 2
    runner.video_lib = SimpleNamespace(lib=video_api)
    runner.lpr = SimpleNamespace(
        lib=lpr_api,
        engine=object(),
        _extract_plate=lambda _handle: Plate(
            text="TEST",
            country="",
            confidence=90,
            x=0,
            y=0,
            width=1,
            height=1,
        ),
    )
    runner.zoom = ZoomController()
    return runner, video_api, lpr_api


def _lease_reference(
    runner: FfmpegVideoAlprRunner,
    frame_id: int,
) -> weakref.ReferenceType[ctypes.Array]:
    pool = runner.frame_leases
    if not isinstance(pool, _TrackingFrameLeasePool):
        raise AssertionError("runner does not use the tracking test pool")
    return pool.references[frame_id]


def _frame_handle(video_api: _FakeVideoApi, frame_id: int) -> int:
    return video_api.handles_by_frame_id[frame_id]


class FfmpegRunnerLeaseIntegrationTests(unittest.TestCase):
    def test_default_720p_lease_budget_is_84_375_mib_within_limit(self) -> None:
        retained = DEFAULT_BUFFER_RETAIN * (2 * 1280 * 720 * 3)

        self.assertEqual(DEFAULT_BUFFER_RETAIN, 16)
        self.assertEqual(retained / MEBIBYTE, 84.375)
        self.assertLess(
            retained,
            DEFAULT_MAX_INFLIGHT_FRAME_MIB * MEBIBYTE,
        )

        args = SimpleNamespace(
            width=1280,
            height=720,
            buffer_retain=DEFAULT_BUFFER_RETAIN,
            max_inflight_frame_mib=5,
        )
        with self.assertRaisesRegex(
            ValueError,
            "cannot retain one RGB buffer and payload",
        ):
            FfmpegVideoAlprRunner(args)

    def test_runner_lease_configuration_uses_strict_positive_integers(
        self,
    ) -> None:
        cases = (
            ("width", True, TypeError),
            ("width", 0, ValueError),
            ("height", 0, ValueError),
            ("buffer_retain", True, TypeError),
            ("buffer_retain", 0, ValueError),
            ("max_inflight_frame_mib", True, TypeError),
            ("max_inflight_frame_mib", 0, ValueError),
        )
        for field, value, error_type in cases:
            with self.subTest(field=field, value=value):
                values = {
                    "width": 1280,
                    "height": 720,
                    "buffer_retain": DEFAULT_BUFFER_RETAIN,
                    "max_inflight_frame_mib": (
                        DEFAULT_MAX_INFLIGHT_FRAME_MIB
                    ),
                }
                values[field] = value
                with self.assertRaises(error_type):
                    FfmpegVideoAlprRunner(SimpleNamespace(**values))

    def test_wrong_type_and_size_fail_before_counters_or_native_calls(self) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)

        for invalid in (bytearray(b"rgb"), memoryview(b"rgb"), "rgb", None):
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaisesRegex(TypeError, "frame data must be bytes"):
                    runner._put_raw_frame(invalid)  # type: ignore[arg-type]
        for invalid in (b"", b"r", b"rg", b"rgbx"):
            with self.subTest(size=len(invalid)):
                with self.assertRaisesRegex(
                    ValueError,
                    "exactly 3 RGB24 bytes",
                ):
                    runner._put_raw_frame(invalid)

        self.assertEqual(runner.frame_count, 0)
        self.assertEqual(runner.dropped_count, 0)
        self.assertEqual(runner.frame_leases.active_ids, ())
        self.assertEqual(video_api.created, [])
        self.assertEqual(video_api.released, [])
        self.assertEqual(lpr_api.accepted, [])

    def test_native_buffer_is_exact_and_handle_mapping_is_non_circular(self) -> None:
        runner, video_api, lpr_api = _runner()
        payload = b"rgb"

        runner._put_raw_frame(payload)

        handle = _frame_handle(video_api, 1)
        reference = _lease_reference(runner, 1)
        native_buffer = reference()
        self.assertIsNotNone(native_buffer)
        if native_buffer is None:
            self.fail("leased native buffer was released")
        self.assertNotEqual(handle, 1)
        self.assertEqual(video_api.VideoFrame_Timestamp(handle), 1)
        self.assertEqual(ctypes.sizeof(native_buffer), len(payload))
        self.assertEqual(bytes(native_buffer), payload)
        self.assertIs(runner.frame_leases.copy_payload(1, 3), payload)
        self.assertTrue(runner.frame_leases.release_payload(1, payload))
        self.assertEqual(video_api.created, [1])
        self.assertEqual(lpr_api.accepted, [1])

    def test_opaque_handles_bind_out_of_order_plate_previews_by_timestamp(
        self,
    ) -> None:
        runner, video_api, lpr_api = _runner()
        first = b"one"
        second = b"two"
        runner._put_raw_frame(first)
        runner._put_raw_frame(second)
        observed: list[bytes] = []

        def record(
            payload: bytes,
            _plate: Plate,
            _target: Any,
            _command: Any,
        ) -> tuple[None, None]:
            observed.append(payload)
            return None, None

        runner._write_plate_previews = record  # type: ignore[method-assign]
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(StringIO()),
        ):
            runner._plate_callback_boundary(
                None,
                _frame_handle(video_api, 2),
                202,
            )
            runner._plate_callback_boundary(
                None,
                _frame_handle(video_api, 1),
                101,
            )
            runner._plate_callback_boundary(
                None,
                _frame_handle(video_api, 1),
                102,
            )

        self.assertEqual(observed, [second, first, first])
        self.assertIs(observed[0], second)
        self.assertIs(observed[1], first)
        self.assertIs(observed[2], first)
        self.assertEqual(lpr_api.destroyed_plates, [202, 101, 102])
        self.assertIsNone(runner.callback_failure)

        runner._completed_callback_boundary(
            None,
            _frame_handle(video_api, 2),
            0,
        )
        runner._completed_callback_boundary(
            None,
            _frame_handle(video_api, 1),
            0,
        )
        self.assertEqual(runner.completed_count, 2)
        self.assertEqual(runner.frame_leases.active_ids, ())

    def test_dropped_and_completed_frames_never_fall_back_to_newer_payload(
        self,
    ) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        old_payload = b"old"
        runner._put_raw_frame(old_payload)
        runner._put_raw_frame(b"new")
        self.assertEqual(runner.dropped_count, 1)

        dropped_handle = 88_888
        video_api.timestamp_by_handle[dropped_handle] = 2
        observed: list[bytes] = []

        def record(
            payload: bytes,
            _plate: Plate,
            _target: Any,
            _command: Any,
        ) -> tuple[None, None]:
            observed.append(payload)
            return None, None

        runner._write_plate_previews = record  # type: ignore[method-assign]
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(StringIO()),
        ):
            runner._plate_callback_boundary(None, dropped_handle, 201)
            runner._plate_callback_boundary(
                None,
                _frame_handle(video_api, 1),
                101,
            )
        self.assertEqual(observed, [old_payload])

        runner._completed_callback_boundary(
            None,
            _frame_handle(video_api, 1),
            0,
        )
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(StringIO()),
        ):
            runner._plate_callback_boundary(
                None,
                _frame_handle(video_api, 1),
                102,
            )
        self.assertEqual(observed, [old_payload])
        self.assertEqual(lpr_api.destroyed_plates, [201, 101, 102])
        self.assertIsNone(runner.callback_failure)

    def test_dimension_mismatch_fails_closed_including_zero_and_negative(
        self,
    ) -> None:
        for width, height in ((0, 1), (-1, 1), (1, 0), (1, -1), (2, 1)):
            with self.subTest(width=width, height=height):
                runner, video_api, lpr_api = _runner()
                runner._put_raw_frame(b"rgb")
                handle = _frame_handle(video_api, 1)
                video_api.widths[handle] = width
                video_api.heights[handle] = height
                output = StringIO()

                with redirect_stdout(output), redirect_stderr(output):
                    runner._plate_callback_boundary(None, handle, 77)

                self.assertEqual(
                    runner.callback_failure,
                    PLATE_CALLBACK_FAILURE,
                )
                self.assertTrue(runner.stop_event.is_set())
                self.assertEqual(lpr_api.destroyed_plates, [77])
                self.assertEqual(runner.plate_count, 0)
                self.assertEqual(runner.last_status, {})
                self.assertEqual(output.getvalue(), "")

    def test_two_started_plate_callbacks_keep_payload_through_completion(
        self,
    ) -> None:
        runner, video_api, lpr_api = _runner(capacity=1)
        payload = b"rgb"
        runner._put_raw_frame(payload)
        handle = _frame_handle(video_api, 1)
        pool = runner.frame_leases
        original_copy = pool.copy_payload
        copied_twice = threading.Event()
        copy_count = 0
        copy_count_lock = threading.Lock()

        def tracked_copy(frame_id: int, expected_size: int) -> bytes | None:
            nonlocal copy_count
            copied = original_copy(frame_id, expected_size)
            with copy_count_lock:
                copy_count += 1
                if copy_count == 2:
                    copied_twice.set()
            return copied

        pool.copy_payload = tracked_copy  # type: ignore[method-assign]
        first_extract_entered = threading.Event()
        release_first_extract = threading.Event()
        extract_count = 0

        def extract(_handle: int) -> Plate:
            nonlocal extract_count
            extract_count += 1
            if extract_count == 1:
                first_extract_entered.set()
                if not release_first_extract.wait(2):
                    raise RuntimeError("test synchronization timeout")
            return Plate("TEST", "", 90, 0, 0, 1, 1)

        runner.lpr._extract_plate = extract
        observed: list[bytes] = []

        def record(
            exact_payload: bytes,
            _plate: Plate,
            _target: Any,
            _command: Any,
        ) -> tuple[None, None]:
            observed.append(exact_payload)
            return None, None

        runner._write_plate_previews = record  # type: ignore[method-assign]
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(StringIO()),
        ):
            first_thread = threading.Thread(
                target=runner._plate_callback_boundary,
                args=(None, handle, 101),
            )
            second_thread = threading.Thread(
                target=runner._plate_callback_boundary,
                args=(None, handle, 102),
            )
            first_thread.start()
            try:
                self.assertTrue(first_extract_entered.wait(2))
                second_thread.start()
                self.assertTrue(copied_twice.wait(2))
                runner._completed_callback_boundary(None, handle, 0)
                self.assertEqual(runner.frame_leases.retained_bytes, 6)
                runner._put_raw_frame(b"new")
                self.assertEqual(video_api.created, [1])
                self.assertEqual(runner.dropped_count, 1)
            finally:
                release_first_extract.set()
                first_thread.join(2)
                if second_thread.ident is not None:
                    second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertIsNone(runner.callback_failure)
        self.assertEqual(observed, [payload, payload])
        self.assertIs(observed[0], payload)
        self.assertIs(observed[1], payload)
        self.assertEqual(lpr_api.destroyed_plates, [101, 102])
        self.assertEqual(runner.completed_count, 1)
        self.assertEqual(runner.frame_leases.retained_bytes, 0)
        runner._put_raw_frame(b"new")
        self.assertEqual(video_api.created, [1, 3])
        self.assertEqual(runner.frame_leases.active_ids, (3,))

    def test_plate_payload_local_survives_concurrent_acknowledgement(
        self,
    ) -> None:
        runner, video_api, _lpr_api = _runner()
        payload = b"rgb"
        runner._put_raw_frame(payload)
        handle = _frame_handle(video_api, 1)
        preview_entered = threading.Event()
        release_preview = threading.Event()
        observed: list[bytes] = []

        def record(
            exact_payload: bytes,
            _plate: Plate,
            _target: Any,
            _command: Any,
        ) -> tuple[None, None]:
            preview_entered.set()
            if not release_preview.wait(2):
                raise RuntimeError("test synchronization timeout")
            observed.append(exact_payload)
            return None, None

        runner._write_plate_previews = record  # type: ignore[method-assign]
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(StringIO()),
        ):
            callback_thread = threading.Thread(
                target=runner._plate_callback_boundary,
                args=(None, handle, 101),
            )
            callback_thread.start()
            self.assertTrue(preview_entered.wait(2))
            runner._completed_callback_boundary(None, handle, 0)
            self.assertEqual(runner.frame_leases.active_ids, ())
            release_preview.set()
            callback_thread.join(2)

        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(observed, [payload])
        self.assertIs(observed[0], payload)
        self.assertIsNone(runner.callback_failure)

    def test_completion_boundary_holds_native_buffer_until_its_return(
        self,
    ) -> None:
        runner, video_api, _lpr_api = _runner()
        runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)
        observed_during_boundary: list[bool] = []

        class ObservingLock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> None:
                gc.collect()
                observed_during_boundary.append(reference() is not None)

        runner.lock = ObservingLock()  # type: ignore[assignment]
        runner._completed_callback_boundary(
            None,
            _frame_handle(video_api, 1),
            0,
        )

        self.assertEqual(observed_during_boundary, [True])
        gc.collect()
        self.assertIsNone(reference())

    def test_completion_boundary_holds_lease_on_post_pop_failure(self) -> None:
        runner, video_api, _lpr_api = _runner()
        runner._put_raw_frame(b"rgb")
        reference = _lease_reference(runner, 1)
        observed_during_failure: list[bool] = []

        class BrokenLock:
            def __enter__(self) -> None:
                raise RuntimeError("PRIVATE-LOCK-DETAIL")

            def __exit__(self, *_args: object) -> None:
                return None

        runner.lock = BrokenLock()  # type: ignore[assignment]
        original_record = runner._record_callback_failure

        def observe_and_record(failure: str) -> None:
            gc.collect()
            observed_during_failure.append(reference() is not None)
            original_record(failure)

        runner._record_callback_failure = observe_and_record  # type: ignore[method-assign]
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            runner._completed_callback_boundary(
                None,
                _frame_handle(video_api, 1),
                0,
            )

        self.assertEqual(observed_during_failure, [True])
        self.assertEqual(
            runner.callback_failure,
            COMPLETED_CALLBACK_FAILURE,
        )
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(output.getvalue(), "")
        gc.collect()
        self.assertIsNone(reference())

    def test_callback_failures_are_sanitized_and_destroy_plate_once(self) -> None:
        secret = "rtsp://private-user:PRIVATE-PASSWORD@camera/PLATE-SECRET"
        runner, video_api, lpr_api = _runner()
        runner._put_raw_frame(b"rgb")
        handle = _frame_handle(video_api, 1)

        def fail_extract(_handle: int) -> Plate:
            raise RuntimeError(secret)

        runner.lpr._extract_plate = fail_extract
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            runner._plate_callback_boundary(None, handle, 701)

        self.assertEqual(runner.callback_failure, PLATE_CALLBACK_FAILURE)
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(lpr_api.destroyed_plates, [701])
        self.assertEqual(output.getvalue(), "")
        with self.assertRaises(RuntimeError) as raised:
            runner._raise_for_callback_failure()
        self.assertEqual(
            str(raised.exception),
            f"native callback failed: {PLATE_CALLBACK_FAILURE}",
        )
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertNotIn("PLATE-SECRET", str(raised.exception))

        downstream, downstream_video, downstream_lpr = _runner()
        downstream._put_raw_frame(b"rgb")

        def fail_preview(
            _payload: bytes,
            _plate: Plate,
            _target: Any,
            _command: Any,
        ) -> tuple[None, None]:
            raise RuntimeError(secret)

        downstream._write_plate_previews = fail_preview  # type: ignore[method-assign]
        output = StringIO()
        with (
            patch("alpr_runner.ffmpeg_video.atomic_json"),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            downstream._plate_callback_boundary(
                None,
                _frame_handle(downstream_video, 1),
                702,
            )
        self.assertEqual(
            downstream.callback_failure,
            PLATE_CALLBACK_FAILURE,
        )
        self.assertEqual(downstream_lpr.destroyed_plates, [702])
        self.assertEqual(output.getvalue(), "")

        lock_failure, lock_video, lock_lpr = _runner()
        lock_failure._put_raw_frame(b"rgb")

        class BrokenCallbackLock:
            def __enter__(self) -> None:
                raise RuntimeError(secret)

            def __exit__(self, *_args: object) -> None:
                return None

        lock_failure.callback_lock = BrokenCallbackLock()  # type: ignore[assignment]
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            lock_failure._plate_callback_boundary(
                None,
                _frame_handle(lock_video, 1),
                703,
            )
        self.assertEqual(
            lock_failure.callback_failure,
            PLATE_CALLBACK_FAILURE,
        )
        self.assertEqual(lock_lpr.destroyed_plates, [703])
        self.assertEqual(output.getvalue(), "")

    def test_actual_ctypes_callback_contains_python_failure(self) -> None:
        runner, video_api, lpr_api = _runner()
        runner._put_raw_frame(b"rgb")
        handle = _frame_handle(video_api, 1)
        secret = "PRIVATE-CTYPES-CALLBACK-DETAIL"

        def fail_extract(_handle: int) -> Plate:
            raise RuntimeError(secret)

        runner.lpr._extract_plate = fail_extract
        callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        callback = callback_type(runner._plate_callback_boundary)
        unraisable: list[object] = []
        output = StringIO()

        with (
            patch(
                "sys.unraisablehook",
                side_effect=lambda value: unraisable.append(value),
            ),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            callback(None, handle, 704)

        self.assertEqual(unraisable, [])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(runner.callback_failure, PLATE_CALLBACK_FAILURE)
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(lpr_api.destroyed_plates, [704])
        with self.assertRaises(RuntimeError) as raised:
            runner._raise_for_callback_failure()
        self.assertNotIn(secret, str(raised.exception))

    def test_completed_callback_failure_is_contained_and_stable(self) -> None:
        runner, video_api, _lpr_api = _runner()
        secret = "PRIVATE-COMPLETION-SOURCE"

        def fail_timestamp(_frame: int) -> int:
            raise RuntimeError(secret)

        video_api.VideoFrame_Timestamp = fail_timestamp  # type: ignore[method-assign]
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            runner._completed_callback_boundary(None, 55_555, 0)

        self.assertEqual(
            runner.callback_failure,
            COMPLETED_CALLBACK_FAILURE,
        )
        self.assertTrue(runner.stop_event.is_set())
        self.assertEqual(output.getvalue(), "")
        with self.assertRaises(RuntimeError) as raised:
            runner._raise_for_callback_failure()
        self.assertEqual(
            str(raised.exception),
            f"native callback failed: {COMPLETED_CALLBACK_FAILURE}",
        )
        self.assertNotIn(secret, str(raised.exception))

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

        fourth_handle = _frame_handle(video_api, 4)
        runner._completed_callback_boundary(None, fourth_handle, 0)
        runner._completed_callback_boundary(None, fourth_handle, 7)
        video_api.timestamp_by_handle[99_999] = 999
        runner._completed_callback_boundary(None, 99_999, 7)
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
        self.assertEqual(video_api.released, [_frame_handle(video_api, 2)])
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
        self.assertEqual(video_api.released, [_frame_handle(video_api, 1)])
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
        self.assertEqual(video_api.released, [_frame_handle(video_api, 1)])
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
        self.assertEqual(video_api.released, [_frame_handle(video_api, 1)])
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
        self.assertTrue(pool.try_acquire(1, buffer, _payload(1)))
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
        self.assertTrue(pool.try_acquire(1, buffer, _payload(1)))
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
