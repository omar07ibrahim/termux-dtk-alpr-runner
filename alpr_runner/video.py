from __future__ import annotations

import argparse
import ctypes
import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .dtk import DtkLpr, Plate
from .zoom import ZoomController, plate_to_target


PIXFMT_RGB24 = 2
ERR_CAPTURE_EOF = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--rtsp")
    source.add_argument("--device", type=int)
    parser.add_argument("--rtsp-over-tcp", dest="rtsp_over_tcp", action="store_true", default=True)
    parser.add_argument("--rtsp-over-udp", dest="rtsp_over_tcp", action="store_false")
    parser.add_argument("--device-width", type=int, default=1280)
    parser.add_argument("--device-height", type=int, default=720)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--dtk-dir", default="vendor/arm64")
    parser.add_argument("--out", default="runtime-video")
    parser.add_argument("--countries", default="")
    parser.add_argument("--min-plate-width", type=int, default=60)
    parser.add_argument("--max-plate-width", type=int, default=500)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--fps-limit", type=int, default=0)
    parser.add_argument("--confirmations", type=int, default=1)
    parser.add_argument("--accumulation-ms", type=int, default=0)
    parser.add_argument("--duplicate-timeout-ms", type=int, default=1000)
    parser.add_argument("--max-zoom", type=float, default=4.0)
    parser.add_argument("--preview-every", type=int, default=0)
    parser.add_argument("--status-print-interval", type=float, default=2.0)
    parser.add_argument("--allow-unlicensed", action="store_true")
    return parser.parse_args()


class DtkVideoLibrary:
    def __init__(self, lib_dir: str | Path) -> None:
        self.lib_dir = Path(lib_dir).expanduser().resolve()
        self.lib = ctypes.CDLL(str(self.lib_dir / "libDTKVID.so"), mode=ctypes.RTLD_GLOBAL)
        self._bind()

    def _bind(self) -> None:
        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p
        c_int = ctypes.c_int
        c_bool = ctypes.c_bool

        self.FrameCallback = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
        self.ErrorCallback = ctypes.CFUNCTYPE(None, c_void_p, c_int, c_void_p)

        self.lib.VideoCapture_Create.argtypes = [self.FrameCallback, self.ErrorCallback, c_void_p]
        self.lib.VideoCapture_Create.restype = c_void_p
        self.lib.VideoCapture_Destroy.argtypes = [c_void_p]
        self.lib.VideoCapture_StartCaptureFromFile.argtypes = [c_void_p, c_char_p, c_int]
        self.lib.VideoCapture_StartCaptureFromFile.restype = c_int
        self.lib.VideoCapture_StartCaptureFromIPCamera.argtypes = [c_void_p, c_char_p]
        self.lib.VideoCapture_StartCaptureFromIPCamera.restype = c_int
        self.lib.VideoCapture_StartCaptureFromDevice.argtypes = [c_void_p, c_int, c_int, c_int]
        self.lib.VideoCapture_StartCaptureFromDevice.restype = c_int
        self.lib.VideoCapture_StopCapture.argtypes = [c_void_p]
        self.lib.VideoCapture_StopCapture.restype = c_int
        self.lib.VideoCapture_SetRtspOverTcp.argtypes = [c_void_p, c_bool]
        self.lib.VideoCapture_SetRtspOverTcp.restype = None
        self.lib.VideoCapture_GetRtspOverTcp.argtypes = [c_void_p]
        self.lib.VideoCapture_GetRtspOverTcp.restype = c_bool
        self.lib.VideoFrame_Release.argtypes = [c_void_p]
        self.lib.VideoFrame_Release.restype = c_int
        self.lib.VideoFrame_GetWidth.argtypes = [c_void_p]
        self.lib.VideoFrame_GetWidth.restype = c_int
        self.lib.VideoFrame_GetHeight.argtypes = [c_void_p]
        self.lib.VideoFrame_GetHeight.restype = c_int
        self.lib.VideoFrame_GetImageBuffer.argtypes = [
            c_void_p,
            c_int,
            ctypes.POINTER(c_void_p),
            ctypes.POINTER(c_int),
            ctypes.POINTER(c_int),
            ctypes.POINTER(c_int),
        ]
        self.lib.VideoFrame_GetImageBuffer.restype = None
        self.lib.VideoFrame_FreeImageBuffer.argtypes = [c_void_p]


class VideoAlprRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out_dir = Path(args.out).expanduser().resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dtk_dir = Path(args.dtk_dir).expanduser().resolve()
        os.chdir(self.dtk_dir)

        self.video_lib = DtkVideoLibrary(self.dtk_dir)
        self.zoom = ZoomController(max_zoom=args.max_zoom)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.frame_count = 0
        self.plate_count = 0
        self.last_status: dict[str, Any] = {}

        self.plate_callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            self._on_plate_detected
        )
        self.frame_callback = self.video_lib.FrameCallback(self._on_frame)
        self.error_callback = self.video_lib.ErrorCallback(self._on_error)

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
        self.capture = self.video_lib.lib.VideoCapture_Create(
            self.frame_callback,
            self.error_callback,
            None,
        )
        if not self.capture:
            raise RuntimeError("VideoCapture_Create failed")

    def run(self) -> int:
        print(f"DTK version: {self.lpr.version()}")
        started = time.time()
        if self.args.file:
            err = self.video_lib.lib.VideoCapture_StartCaptureFromFile(
                self.capture,
                str(Path(self.args.file).expanduser().resolve()).encode("utf-8"),
                self.args.repeat,
            )
        elif self.args.rtsp:
            self.video_lib.lib.VideoCapture_SetRtspOverTcp(self.capture, bool(self.args.rtsp_over_tcp))
            print(f"RTSP transport: {'TCP' if self.args.rtsp_over_tcp else 'UDP'}")
            err = self.video_lib.lib.VideoCapture_StartCaptureFromIPCamera(
                self.capture,
                self.args.rtsp.encode("utf-8"),
            )
        else:
            err = self.video_lib.lib.VideoCapture_StartCaptureFromDevice(
                self.capture,
                self.args.device,
                self.args.device_width,
                self.args.device_height,
            )
        if err != 0:
            raise RuntimeError(f"VideoCapture start failed: {err}")

        last_print = 0.0
        last_print_frames = 0
        try:
            while not self.stop_event.is_set():
                time.sleep(0.25)
                elapsed = max(0.001, time.time() - started)
                with self.lock:
                    status = dict(self.last_status)
                    status.update(
                        {
                            "frames_seen": self.frame_count,
                            "plates_seen": self.plate_count,
                            "runtime_seconds": round(elapsed, 2),
                            "callback_fps": round(self.frame_count / elapsed, 2),
                        }
                    )
                atomic_json(self.out_dir / "status.json", status)
                now = time.time()
                if self.args.status_print_interval > 0 and now - last_print >= self.args.status_print_interval:
                    frame_delta = status["frames_seen"] - last_print_frames
                    interval = max(0.001, now - last_print) if last_print > 0 else self.args.status_print_interval
                    live_fps = frame_delta / interval
                    print(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} | live frames={status['frames_seen']} "
                        f"| fps={live_fps:.1f} | plates={status['plates_seen']}"
                    )
                    last_print = now
                    last_print_frames = status["frames_seen"]
        except KeyboardInterrupt:
            pass
        finally:
            self.video_lib.lib.VideoCapture_StopCapture(self.capture)
            self.video_lib.lib.VideoCapture_Destroy(self.capture)
            self.lpr.close()
        return 0

    def _on_frame(self, _capture: ctypes.c_void_p, frame: ctypes.c_void_p, _custom: ctypes.c_void_p) -> None:
        self.frame_count += 1
        if self.args.preview_every > 0 and self.frame_count % self.args.preview_every == 0:
            # Save before handing the frame to LPREngine_PutFrame. Once PutFrame
            # accepts it, DTK owns the handle.
            self._save_frame(frame, self.out_dir / "latest_frame.jpg")
        ret = self.lpr.lib.LPREngine_PutFrame(self.lpr.engine, frame, self.frame_count)
        if ret != 0:
            # If the engine rejects the frame, release the capture-owned handle.
            self.video_lib.lib.VideoFrame_Release(frame)

    def _on_error(self, _capture: ctypes.c_void_p, error_code: int, _custom: ctypes.c_void_p) -> None:
        meanings = {
            1: "ERR_CAPTURE_OPEN_VIDEO",
            2: "ERR_CAPTURE_READ_FRAME",
            3: "ERR_CAPTURE_EOF",
        }
        print(f"VideoCapture error: {error_code} ({meanings.get(error_code, 'unknown')})")
        if error_code == ERR_CAPTURE_EOF:
            self.stop_event.set()

    def _on_plate_detected(self, _engine: ctypes.c_void_p, frame: ctypes.c_void_p, plate_handle: ctypes.c_void_p) -> None:
        plate = self.lpr._extract_plate(plate_handle)
        self.lpr.lib.LicensePlate_Destroy(plate_handle)
        width = max(1, self.video_lib.lib.VideoFrame_GetWidth(frame))
        height = max(1, self.video_lib.lib.VideoFrame_GetHeight(frame))
        target = plate_to_target(plate, width, height)
        command = self.zoom.next([target])
        self.plate_count += 1

        preview_path = None
        if self.args.preview_every >= 0:
            preview_path = self._save_frame(frame, self.out_dir / "latest.jpg", plate=plate, target=target)
            if preview_path:
                self._save_zoom(frame, self.out_dir / "latest_zoom.jpg", command)

        status = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "dtk-video",
            "frame_size": {"width": width, "height": height},
            "plate": plate.to_json(),
            "target": target.to_json(),
            "zoom": command.to_json(),
            "latest_preview": str(preview_path) if preview_path else None,
            "performance_profile": {
                "fps_limit": self.args.fps_limit,
                "confirmations": self.args.confirmations,
                "accumulation_ms": self.args.accumulation_ms,
                "duplicate_timeout_ms": self.args.duplicate_timeout_ms,
                "preview_every": self.args.preview_every,
            },
        }
        with self.lock:
            self.last_status = status
        atomic_json(self.out_dir / "plate_event.json", status)
        atomic_json(self.out_dir / "zoom_command.json", command.to_json())
        print(f"{status['time']} | {plate.text} | {plate.vehicle_make} {plate.vehicle_model} | zoom={command.zoom_ratio:.2f}")

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
        target=None,
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

    def _save_zoom(self, frame: ctypes.c_void_p, path: Path, command) -> Path | None:
        image = self._frame_to_image(frame)
        if image is None:
            return None
        zoomed = self.zoom.crop_image(image, command)
        zoomed.save(path, quality=88)
        return path


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    args = parse_args()
    runner = VideoAlprRunner(args)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
