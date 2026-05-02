from __future__ import annotations

import argparse
import ctypes
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .dtk import DtkLpr, Plate
from .video import DtkVideoLibrary, ERR_CAPTURE_EOF, PIXFMT_RGB24, atomic_json
from .zoom import ZoomController, plate_to_target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtsp", action="append", default=[], help="RTSP/IP camera URL. Repeat for each camera.")
    parser.add_argument("--file", action="append", default=[], help="Video file for testing. Repeat for each stream.")
    parser.add_argument("--device", action="append", type=int, default=[], help="Linux /dev/video index if available.")
    parser.add_argument("--rtsp-over-tcp", dest="rtsp_over_tcp", action="store_true", default=True)
    parser.add_argument("--rtsp-over-udp", dest="rtsp_over_tcp", action="store_false")
    parser.add_argument("--stream-name", action="append", default=[], help="Optional camera name. Repeat in source order.")
    parser.add_argument("--device-width", type=int, default=1280)
    parser.add_argument("--device-height", type=int, default=720)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--dtk-dir", default="vendor/arm64")
    parser.add_argument("--out", default="runtime-multi")
    parser.add_argument("--countries", default="")
    parser.add_argument("--min-plate-width", type=int, default=60)
    parser.add_argument("--max-plate-width", type=int, default=500)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--fps-limit", type=int, default=0)
    parser.add_argument("--confirmations", type=int, default=1)
    parser.add_argument("--accumulation-ms", type=int, default=0)
    parser.add_argument("--duplicate-timeout-ms", type=int, default=600)
    parser.add_argument("--max-zoom", type=float, default=4.0)
    parser.add_argument("--preview-every", type=int, default=0)
    parser.add_argument("--status-interval", type=float, default=0.5)
    parser.add_argument("--print-every", type=int, default=10)
    parser.add_argument("--print-min-seconds", type=float, default=15.0)
    parser.add_argument("--allow-unlicensed", action="store_true")
    return parser.parse_args()


@dataclass(frozen=True)
class VideoSource:
    name: str
    kind: str
    value: str | int

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "value": self.value}


def build_sources(args: argparse.Namespace) -> list[VideoSource]:
    raw_sources: list[tuple[str, str | int]] = []
    raw_sources.extend(("rtsp", value) for value in args.rtsp)
    raw_sources.extend(("file", value) for value in args.file)
    raw_sources.extend(("device", value) for value in args.device)
    if not raw_sources:
        raise SystemExit("Provide at least one --rtsp, --file, or --device source.")

    sources: list[VideoSource] = []
    used_names: set[str] = set()
    for index, (kind, value) in enumerate(raw_sources, start=1):
        requested_name = args.stream_name[index - 1] if index <= len(args.stream_name) else f"cam{index}"
        name = sanitize_name(requested_name) or f"cam{index}"
        if name in used_names:
            name = f"{name}-{index}"
        used_names.add(name)
        sources.append(VideoSource(name=name, kind=kind, value=value))
    return sources


def sanitize_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return clean.strip("-._")


def normalize_plate_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def local_time(timestamp: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp or time.time()))


class PlateRegistry:
    def __init__(self, print_every: int, print_min_seconds: float) -> None:
        self.print_every = max(0, print_every)
        self.print_min_seconds = max(0.0, print_min_seconds)
        self.lock = threading.RLock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.last_event: dict[str, Any] | None = None
        self.last_print: dict[str, dict[str, float | int]] = {}

    def record(
        self,
        camera_id: str,
        plate: Plate,
        target: dict[str, Any],
        zoom: dict[str, Any],
        frame_size: dict[str, int],
    ) -> tuple[dict[str, Any] | None, bool]:
        key = normalize_plate_text(plate.text)
        if not key:
            return None, False

        now = time.time()
        now_text = local_time(now)
        with self.lock:
            entry = self.entries.get(key)
            is_new = entry is None
            if entry is None:
                entry = {
                    "key": key,
                    "text": plate.text,
                    "country": plate.country,
                    "count": 0,
                    "first_seen": now_text,
                    "last_seen": now_text,
                    "last_camera": camera_id,
                    "cameras": {},
                    "best_confidence": plate.confidence,
                    "vehicle_make": plate.vehicle_make,
                    "vehicle_model": plate.vehicle_model,
                    "vehicle_confidence": plate.vehicle_confidence,
                    "last_target": target,
                    "last_zoom": zoom,
                    "last_frame_size": frame_size,
                }
                self.entries[key] = entry

            entry["count"] += 1
            entry["last_seen"] = now_text
            entry["last_camera"] = camera_id
            entry["cameras"][camera_id] = entry["cameras"].get(camera_id, 0) + 1
            entry["last_target"] = target
            entry["last_zoom"] = zoom
            entry["last_frame_size"] = frame_size

            if plate.confidence >= int(entry.get("best_confidence", 0)):
                entry["text"] = plate.text
                entry["country"] = plate.country
                entry["best_confidence"] = plate.confidence
                entry["vehicle_make"] = plate.vehicle_make
                entry["vehicle_model"] = plate.vehicle_model
                entry["vehicle_confidence"] = plate.vehicle_confidence

            event = {
                "time": now_text,
                "camera": camera_id,
                "key": key,
                "is_new": is_new,
                "count": entry["count"],
                "plate": plate.to_json(),
                "target": target,
                "zoom": zoom,
                "frame_size": frame_size,
                "cameras": dict(entry["cameras"]),
            }
            self.last_event = event
            should_print = self._should_print_locked(key, int(entry["count"]), now, is_new)
            if should_print:
                self.last_print[key] = {"count": int(entry["count"]), "time": now}
            return event, should_print

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        plates = sorted(
            (dict(entry, cameras=dict(entry["cameras"])) for entry in self.entries.values()),
            key=lambda item: (-int(item["count"]), str(item["key"])),
        )
        return {
            "total_unique_plates": len(plates),
            "total_recognitions": sum(int(item["count"]) for item in plates),
            "last_event": dict(self.last_event) if self.last_event else None,
            "plates": plates,
        }

    def _should_print_locked(self, key: str, count: int, now: float, is_new: bool) -> bool:
        if is_new:
            return True
        if self.print_every > 0 and count % self.print_every == 0:
            return True
        previous = self.last_print.get(key)
        if previous and self.print_min_seconds > 0 and now - float(previous["time"]) >= self.print_min_seconds:
            return True
        return False


class StreamWorker:
    def __init__(
        self,
        source: VideoSource,
        args: argparse.Namespace,
        video_lib: DtkVideoLibrary,
        registry: PlateRegistry,
        root_out_dir: Path,
        stop_event: threading.Event,
    ) -> None:
        self.source = source
        self.args = args
        self.video_lib = video_lib
        self.registry = registry
        self.stop_event = stop_event
        self.done_event = threading.Event()
        self.out_dir = root_out_dir / source.name
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.zoom = ZoomController(max_zoom=args.max_zoom)
        self.lock = threading.Lock()
        self.frame_count = 0
        self.plate_count = 0
        self.last_error: dict[str, Any] | None = None
        self.last_status: dict[str, Any] = {}

        self.plate_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            self._on_plate_detected
        )
        self.frame_callback = self.video_lib.FrameCallback(self._on_frame)
        self.error_callback = self.video_lib.ErrorCallback(self._on_error)

        self.lpr = DtkLpr(
            args.dtk_dir,
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
        self.capture = self.video_lib.lib.VideoCapture_Create(
            self.frame_callback,
            self.error_callback,
            None,
        )
        if not self.capture:
            raise RuntimeError(f"{self.source.name}: VideoCapture_Create failed")

    def start(self) -> None:
        if self.source.kind == "file":
            err = self.video_lib.lib.VideoCapture_StartCaptureFromFile(
                self.capture,
                str(Path(str(self.source.value)).expanduser().resolve()).encode("utf-8"),
                self.args.repeat,
            )
        elif self.source.kind == "rtsp":
            self.video_lib.lib.VideoCapture_SetRtspOverTcp(self.capture, bool(self.args.rtsp_over_tcp))
            err = self.video_lib.lib.VideoCapture_StartCaptureFromIPCamera(
                self.capture,
                str(self.source.value).encode("utf-8"),
            )
        elif self.source.kind == "device":
            err = self.video_lib.lib.VideoCapture_StartCaptureFromDevice(
                self.capture,
                int(self.source.value),
                self.args.device_width,
                self.args.device_height,
            )
        else:
            raise RuntimeError(f"{self.source.name}: unknown source kind {self.source.kind}")

        if err != 0:
            raise RuntimeError(f"{self.source.name}: VideoCapture start failed: {err}")
        print(f"{self.source.name}: started {self.source.kind} source")

    def close(self) -> None:
        capture = getattr(self, "capture", None)
        if capture:
            self.video_lib.lib.VideoCapture_StopCapture(capture)
            self.video_lib.lib.VideoCapture_Destroy(capture)
            self.capture = None
        self.lpr.close()

    def status(self, started: float) -> dict[str, Any]:
        elapsed = max(0.001, time.time() - started)
        with self.lock:
            status = dict(self.last_status)
            status.update(
                {
                    "name": self.source.name,
                    "source": self.source.to_json(),
                    "frames_seen": self.frame_count,
                    "plate_callbacks": self.plate_count,
                    "runtime_seconds": round(elapsed, 2),
                    "callback_fps": round(self.frame_count / elapsed, 2),
                    "done": self.done_event.is_set(),
                    "last_error": self.last_error,
                }
            )
            return status

    def _on_frame(self, _capture: ctypes.c_void_p, frame: ctypes.c_void_p, _custom: ctypes.c_void_p) -> None:
        if self.stop_event.is_set():
            self.video_lib.lib.VideoFrame_Release(frame)
            return

        with self.lock:
            self.frame_count += 1
            frame_id = self.frame_count

        if self.args.preview_every > 0 and frame_id % self.args.preview_every == 0:
            self._save_frame(frame, self.out_dir / "latest_frame.jpg")

        ret = self.lpr.lib.LPREngine_PutFrame(self.lpr.engine, frame, frame_id)
        if ret != 0:
            self.video_lib.lib.VideoFrame_Release(frame)

    def _on_error(self, _capture: ctypes.c_void_p, error_code: int, _custom: ctypes.c_void_p) -> None:
        error = {"time": local_time(), "code": int(error_code)}
        with self.lock:
            self.last_error = error
        meanings = {
            1: "ERR_CAPTURE_OPEN_VIDEO",
            2: "ERR_CAPTURE_READ_FRAME",
            3: "ERR_CAPTURE_EOF",
        }
        print(f"{self.source.name}: VideoCapture error: {error_code} ({meanings.get(error_code, 'unknown')})")
        if error_code == ERR_CAPTURE_EOF:
            self.done_event.set()

    def _on_plate_detected(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, plate_handle: ctypes.c_void_p) -> None:
        plate = self.lpr._extract_plate(plate_handle)
        self.lpr.lib.LicensePlate_Destroy(plate_handle)

        width = max(1, self.video_lib.lib.VideoFrame_GetWidth(frame))
        height = max(1, self.video_lib.lib.VideoFrame_GetHeight(frame))
        frame_size = {"width": width, "height": height}
        target = plate_to_target(plate, width, height)
        command = self.zoom.next([target])
        target_json = target.to_json()
        zoom_json = command.to_json()
        event, should_print = self.registry.record(
            self.source.name,
            plate,
            target_json,
            zoom_json,
            frame_size,
        )
        if event is None:
            return

        with self.lock:
            self.plate_count += 1
            self.last_status = {
                "last_plate": event,
                "last_zoom": zoom_json,
            }

        atomic_json(self.out_dir / "zoom_command.json", zoom_json)
        atomic_json(self.out_dir / "plate_event.json", event)

        if event["is_new"] or should_print:
            preview_path = self._save_frame(frame, self.out_dir / "latest.jpg", plate=plate, target=target)
            zoom_path = self._save_zoom(frame, self.out_dir / "latest_zoom.jpg", command)
            if preview_path:
                event["latest_preview"] = str(preview_path)
            if zoom_path:
                event["latest_zoom_preview"] = str(zoom_path)
            atomic_json(self.out_dir / "plate_event.json", event)

        if should_print:
            vehicle = " ".join(part for part in (plate.vehicle_make, plate.vehicle_model) if part).strip()
            vehicle_part = f" | {vehicle}" if vehicle else ""
            cameras = ",".join(sorted(event["cameras"].keys()))
            print(
                f"{event['time']} | {self.source.name} | {plate.text} | count={event['count']} "
                f"| cameras={cameras}{vehicle_part} | zoom={command.zoom_ratio:.2f}"
            )

    def _frame_to_image(self, frame: ctypes.c_void_p) -> Image.Image | None:
        buf = ctypes.c_void_p()
        width = ctypes.c_int()
        height = ctypes.c_int()
        stride = ctypes.c_int()
        self.video_lib.lib.VideoFrame_GetImageBuffer(
            frame,
            PIXFMT_RGB24,
            ctypes.byref(buf),
            ctypes.byref(width),
            ctypes.byref(height),
            ctypes.byref(stride),
        )
        if not buf.value or width.value <= 0 or height.value <= 0:
            return None
        try:
            data = ctypes.string_at(buf, stride.value * height.value)
            image = Image.frombytes("RGB", (width.value, height.value), data, "raw", "RGB", stride.value)
            return image.copy()
        finally:
            self.video_lib.lib.VideoFrame_FreeImageBuffer(buf)

    def _save_frame(
        self,
        frame: ctypes.c_void_p,
        path: Path,
        plate: Plate | None = None,
        target: Any | None = None,
    ) -> Path | None:
        image = self._frame_to_image(frame)
        if image is None:
            return None
        draw = ImageDraw.Draw(image)
        if target is not None:
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
        if plate is not None:
            draw.rectangle(
                (plate.x, plate.y, plate.x + plate.width, plate.y + plate.height),
                outline=(242, 201, 76),
                width=3,
            )
            draw.text((plate.x, max(0, plate.y - 16)), f"{plate.text} {plate.confidence}", fill=(255, 255, 255))
        image.save(path, quality=88)
        return path

    def _save_zoom(self, frame: ctypes.c_void_p, path: Path, command: Any) -> Path | None:
        image = self._frame_to_image(frame)
        if image is None:
            return None
        zoomed = self.zoom.crop_image(image, command)
        zoomed.save(path, quality=88)
        return path


class MultiVideoAlprRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out_dir = Path(args.out).expanduser().resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dtk_dir = Path(args.dtk_dir).expanduser().resolve()
        args.dtk_dir = str(self.dtk_dir)
        os.chdir(self.dtk_dir)
        self.sources = build_sources(args)
        self.video_lib = DtkVideoLibrary(self.dtk_dir)
        self.registry = PlateRegistry(args.print_every, args.print_min_seconds)
        self.stop_event = threading.Event()
        self.workers: list[StreamWorker] = []

    def run(self) -> int:
        print(f"Streams: {len(self.sources)}")
        started = time.time()
        try:
            for source in self.sources:
                worker = StreamWorker(source, self.args, self.video_lib, self.registry, self.out_dir, self.stop_event)
                self.workers.append(worker)

            if self.workers:
                print(f"DTK version: {self.workers[0].lpr.version()}")

            for worker in self.workers:
                worker.start()

            while not self.stop_event.is_set():
                self._write_status(started)
                if all(worker.done_event.is_set() for worker in self.workers):
                    break
                time.sleep(max(0.1, self.args.status_interval))
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            for worker in self.workers:
                worker.close()
            self._write_status(started)
        return 0

    def _write_status(self, started: float) -> None:
        plates = self.registry.snapshot()
        status = {
            "time": local_time(),
            "mode": "dtk-video-multi",
            "dtk_dir": str(self.dtk_dir),
            "streams_count": len(self.workers),
            "streams": [worker.status(started) for worker in self.workers],
            "plates": plates,
            "performance_profile": {
                "threads_per_engine": self.args.threads,
                "fps_limit": self.args.fps_limit,
                "confirmations": self.args.confirmations,
                "accumulation_ms": self.args.accumulation_ms,
                "duplicate_timeout_ms": self.args.duplicate_timeout_ms,
                "preview_every": self.args.preview_every,
                "status_interval": self.args.status_interval,
            },
        }
        atomic_json(self.out_dir / "status.json", status)
        atomic_json(self.out_dir / "plate_counts.json", plates)


def main() -> int:
    args = parse_args()
    runner = MultiVideoAlprRunner(args)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
