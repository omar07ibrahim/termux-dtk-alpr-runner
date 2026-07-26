from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .dtk import DtkLpr, Plate
from .runtime_io import (
    atomic_json,
    prepare_private_directory,
    private_relative_path,
    protect_runtime_file,
    source_descriptor,
)
from .zoom import ZoomController, plate_to_target


class FrameLeasePool:
    """Bounded, thread-safe ownership for buffers exposed to native code.

    A successful :meth:`try_acquire` keeps the exact ``ctypes`` array strongly
    referenced until the adapter acknowledges that frame ID. The pool never
    evicts an in-flight lease: when capacity is exhausted, ``try_acquire``
    returns ``False`` and the caller must not hand that buffer to native code.

    ``acknowledge`` and ``cancel`` remove only the matching frame ID. Unknown,
    duplicate, and out-of-order acknowledgements are deterministic no-ops.
    ``cancel`` is reserved for paths where native ownership was never accepted.
    Call ``close`` only after the native adapter has been stopped and cannot
    access previously handed-off addresses.
    """

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int:
            raise TypeError("capacity must be an int")
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")

        self._capacity = capacity
        self._leases: dict[int, ctypes.Array] = {}
        self._closed = False
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

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
            return len(self._leases)

    def try_acquire(self, frame_id: int, buffer: ctypes.Array) -> bool:
        """Lease ``buffer`` to ``frame_id`` without blocking or eviction."""

        self._validate_frame_id(frame_id, require_positive=True)
        if not isinstance(buffer, ctypes.Array):
            raise TypeError("buffer must be a ctypes array")

        with self._lock:
            if self._closed:
                raise RuntimeError("frame lease pool is closed")
            if frame_id in self._leases:
                raise ValueError(f"frame_id {frame_id} already has an active lease")
            if len(self._leases) >= self._capacity:
                return False
            self._leases[frame_id] = buffer
            return True

    def acknowledge(self, frame_id: int) -> bool:
        """Release one matching completed lease; return ``False`` if absent."""

        self._validate_frame_id(frame_id)
        return self._release(frame_id)

    def cancel(self, frame_id: int) -> bool:
        """Release a lease whose native handoff failed or was rejected."""

        self._validate_frame_id(frame_id)
        return self._release(frame_id)

    def close(self) -> int:
        """Release every lease after adapter shutdown and reject future work."""

        with self._lock:
            if self._closed:
                return 0
            released = len(self._leases)
            self._leases.clear()
            self._closed = True
            return released

    def _release(self, frame_id: int) -> bool:
        with self._lock:
            if frame_id not in self._leases:
                return False
            del self._leases[frame_id]
            return True

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
    parser.add_argument("--buffer-retain", type=int, default=96)
    parser.add_argument("--allow-unlicensed", action="store_true")
    return parser.parse_args()


class FfmpegVideoAlprRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        # Keep the pure-Python orchestration and lease contract importable
        # without loading the optional DTK video/Pillow runtime.
        from .video import PIXFMT_RGB24, DtkVideoLibrary

        self.args = args
        self.out_dir = prepare_private_directory(args.out)
        self.dtk_dir = Path(args.dtk_dir).expanduser().resolve()
        os.chdir(self.dtk_dir)

        self.video_lib = DtkVideoLibrary(self.dtk_dir)
        self.pixel_format = PIXFMT_RGB24
        self.zoom = ZoomController(max_zoom=args.max_zoom)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.frame_count = 0
        self.plate_count = 0
        self.completed_count = 0
        self.dropped_count = 0
        self.last_status: dict[str, Any] = {}
        self.frame_leases = FrameLeasePool(max(8, args.buffer_retain))
        self.latest_frame_bytes: bytes | None = None

        self.plate_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            self._on_plate_detected
        )
        self.completed_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(
            self._on_frame_completed
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
        self.lpr._completed_callback_ref = self.completed_callback
        self.lpr.lib.LPREngine_SetFrameProcessingCompletedCallback(self.lpr.engine, self.completed_callback)

    def run(self) -> int:
        print(f"DTK version: {self.lpr.version()}")
        print("RTSP capture: ffmpeg rawvideo -> DTK VideoFrame_CreateFromImageBuffer")
        command = self._ffmpeg_command()
        print("FFmpeg input: <redacted RTSP source>")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=os.name == "posix",
        )
        stderr_thread = threading.Thread(target=self._drain_stderr, args=(process,), daemon=True)
        stderr_thread.start()

        frame_size = self.args.width * self.args.height * 3
        started = time.time()
        last_print = 0.0
        last_print_frames = 0
        try:
            while not self.stop_event.is_set():
                data = self._read_exact(process.stdout, frame_size) if process.stdout else b""
                if len(data) != frame_size:
                    if process.poll() is not None:
                        raise RuntimeError(f"ffmpeg exited with code {process.returncode}")
                    continue
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
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            self._shutdown(process)
        return 0

    def _shutdown(self, process: subprocess.Popen) -> None:
        """Stop producers, destroy the adapter, then release backing buffers."""

        self._stop_process(process)
        self.lpr.close()
        # LPREngine_Destroy has returned, so this runner treats the adapter
        # as quiesced before clearing outstanding leases. If producer stop or
        # destruction raises, this line is deliberately not reached.
        self.frame_leases.close()

    def _ffmpeg_command(self) -> list[str]:
        vf = f"fps={self.args.fps},scale={self.args.width}:{self.args.height}:flags=fast_bilinear"
        return [
            "ffmpeg",
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
            self.args.rtsp,
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
        with self.lock:
            self.frame_count += 1
            frame_id = self.frame_count
            self.latest_frame_bytes = data

        buffer = ctypes.create_string_buffer(data)
        if not self.frame_leases.try_acquire(frame_id, buffer):
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

    def _on_frame_completed(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, status: int) -> None:
        frame_id = int(self.video_lib.lib.VideoFrame_Timestamp(frame))
        if not self.frame_leases.acknowledge(frame_id):
            # Duplicate, unknown, or late-after-close callbacks must not alter
            # counters or release any other frame's backing storage.
            return
        with self.lock:
            self.completed_count += 1
            if status != 0:
                self.dropped_count += 1

    def _on_plate_detected(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, plate_handle: ctypes.c_void_p) -> None:
        plate = self.lpr._extract_plate(plate_handle)
        self.lpr.lib.LicensePlate_Destroy(plate_handle)
        width = max(1, self.video_lib.lib.VideoFrame_GetWidth(frame))
        height = max(1, self.video_lib.lib.VideoFrame_GetHeight(frame))
        target = plate_to_target(plate, width, height)
        command = self.zoom.next([target])
        with self.lock:
            self.plate_count += 1
            latest = self.latest_frame_bytes

        preview_path = None
        zoom_path = None
        if latest:
            try:
                from PIL import Image
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "Pillow is required only when writing frame previews"
                ) from error
            image = Image.frombytes("RGB", (self.args.width, self.args.height), latest)
            preview_path = self._save_annotated(image, self.out_dir / "latest.jpg", plate=plate, target=target)
            zoomed = self.zoom.crop_image(image, command)
            zoomed.save(self.out_dir / "latest_zoom.jpg", quality=88)
            protect_runtime_file(self.out_dir / "latest_zoom.jpg")
            zoom_path = self.out_dir / "latest_zoom.jpg"

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
        atomic_json(self.out_dir / "plate_event.json", status["last_plate_event"])
        atomic_json(self.out_dir / "zoom_command.json", command.to_json())
        print(f"{status['last_plate_event']['time']} | {plate.text} | zoom={command.zoom_ratio:.2f}")

    def _save_raw_frame(self, data: bytes, path: Path) -> None:
        try:
            from PIL import Image
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Pillow is required only when writing frame previews"
            ) from error
        image = Image.frombytes("RGB", (self.args.width, self.args.height), data)
        image.save(path, quality=85)
        protect_runtime_file(path)

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
        result.save(path, quality=88)
        protect_runtime_file(path)
        return path

    @staticmethod
    def _read_exact(stream: Any, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                print(f"ffmpeg: {text}")

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return

        def send(sig: signal.Signals) -> None:
            if os.name == "posix" and hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

        try:
            send(signal.SIGTERM)
        except ProcessLookupError:
            # The producer may have exited between poll() and getpgid().
            # Confirm and reap it before native teardown.
            process.wait(timeout=2)
            return
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            send(signal.SIGKILL)
        except ProcessLookupError:
            process.wait(timeout=2)
            return
        # Native teardown cannot begin until the producer is reaped. A second
        # timeout or signal failure propagates and keeps adapter leases alive.
        process.wait(timeout=2)


def main() -> int:
    return FfmpegVideoAlprRunner(parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
