from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .dtk import DtkLpr, Plate
from .video import DtkVideoLibrary, PIXFMT_RGB24, atomic_json
from .zoom import ZoomController, plate_to_target


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
        self.args = args
        self.out_dir = Path(args.out).expanduser().resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dtk_dir = Path(args.dtk_dir).expanduser().resolve()
        os.chdir(self.dtk_dir)

        self.video_lib = DtkVideoLibrary(self.dtk_dir)
        self.zoom = ZoomController(max_zoom=args.max_zoom)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.frame_count = 0
        self.plate_count = 0
        self.completed_count = 0
        self.dropped_count = 0
        self.last_status: dict[str, Any] = {}
        self.buffer_refs: dict[int, ctypes.Array] = {}
        self.buffer_order: deque[int] = deque(maxlen=max(8, args.buffer_retain))
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
        print("FFmpeg input:", self.args.rtsp)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
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
                        "source": self.args.rtsp,
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
            self._stop_process(process)
            self.lpr.close()
        return 0

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
        with self.lock:
            self.buffer_refs[frame_id] = buffer
            self.buffer_order.append(frame_id)
            while len(self.buffer_refs) > self.buffer_order.maxlen:
                old = self.buffer_order.popleft()
                self.buffer_refs.pop(old, None)

        frame = self.video_lib.lib.VideoFrame_CreateFromImageBuffer(
            ctypes.cast(buffer, ctypes.c_void_p),
            self.args.width,
            self.args.height,
            self.args.width * 3,
            PIXFMT_RGB24,
            frame_id,
        )
        if not frame:
            with self.lock:
                self.buffer_refs.pop(frame_id, None)
            return

        if self.args.preview_every > 0 and frame_id % self.args.preview_every == 0:
            self._save_raw_frame(data, self.out_dir / "latest_frame.jpg")

        ret = self.lpr.lib.LPREngine_PutFrame(self.lpr.engine, frame, frame_id)
        if ret != 0:
            self.video_lib.lib.VideoFrame_Release(frame)
            with self.lock:
                self.dropped_count += 1
                self.buffer_refs.pop(frame_id, None)
            print(f"LPREngine_PutFrame returned {ret}")

    def _on_frame_completed(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, status: int) -> None:
        frame_id = int(self.video_lib.lib.VideoFrame_Timestamp(frame))
        with self.lock:
            self.completed_count += 1
            if status != 0:
                self.dropped_count += 1
            self.buffer_refs.pop(frame_id, None)

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
            image = Image.frombytes("RGB", (self.args.width, self.args.height), latest)
            preview_path = self._save_annotated(image, self.out_dir / "latest.jpg", plate=plate, target=target)
            zoomed = self.zoom.crop_image(image, command)
            zoomed.save(self.out_dir / "latest_zoom.jpg", quality=88)
            zoom_path = self.out_dir / "latest_zoom.jpg"

        status = {
            "last_plate_event": {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "plate": plate.to_json(),
                "target": target.to_json(),
                "zoom": command.to_json(),
                "latest_preview": str(preview_path) if preview_path else None,
                "latest_zoom_preview": str(zoom_path) if zoom_path else None,
            }
        }
        with self.lock:
            self.last_status = status
        atomic_json(self.out_dir / "plate_event.json", status["last_plate_event"])
        atomic_json(self.out_dir / "zoom_command.json", command.to_json())
        print(f"{status['last_plate_event']['time']} | {plate.text} | zoom={command.zoom_ratio:.2f}")

    def _save_raw_frame(self, data: bytes, path: Path) -> None:
        image = Image.frombytes("RGB", (self.args.width, self.args.height), data)
        image.save(path, quality=85)

    def _save_annotated(self, image: Image.Image, path: Path, plate: Plate, target: Any) -> Path:
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
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=2)
        except Exception:
            process.kill()


def main() -> int:
    return FfmpegVideoAlprRunner(parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
