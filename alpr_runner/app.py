from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path

from PIL import Image, ImageDraw

from .car_detector import OptionalYoloCarDetector
from .dtk import DtkLpr, DtkError, DtkLicenseError
from .zoom import Box, ZoomController, plate_to_target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dtk-dir", default="vendor/arm64")
    parser.add_argument("--source", choices=["file", "dir", "watch-file", "rtsp"], default="watch-file")
    parser.add_argument("--input", default="")
    parser.add_argument("--out", default="runtime")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--serve", type=int, default=8765)
    parser.add_argument("--countries", default="")
    parser.add_argument("--min-plate-width", type=int, default=60)
    parser.add_argument("--max-plate-width", type=int, default=900)
    parser.add_argument("--no-make-model", action="store_true")
    parser.add_argument("--max-zoom", type=float, default=4.0)
    parser.add_argument("--car-model", default="models/car_yolo.onnx")
    parser.add_argument("--allow-unlicensed", action="store_true")
    return parser.parse_args()


class FrameSource:
    def __init__(self, args: argparse.Namespace, out_dir: Path) -> None:
        self.args = args
        self.out_dir = out_dir
        self.last_mtime = 0.0
        self.dir_index = 0
        self.dir_files = []
        if args.source == "dir":
            self.dir_files = sorted(Path(args.input).expanduser().glob("*"))

    def next_frame(self) -> Path | None:
        if self.args.source == "file":
            return Path(self.args.input).expanduser()
        if self.args.source == "dir":
            if not self.dir_files:
                return None
            path = self.dir_files[self.dir_index % len(self.dir_files)]
            self.dir_index += 1
            return path
        if self.args.source == "watch-file":
            return self._watch_file()
        if self.args.source == "rtsp":
            return self._rtsp_snapshot()
        return None

    def _watch_file(self) -> Path | None:
        path = Path(self.args.input).expanduser()
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        if mtime <= self.last_mtime:
            return None
        self.last_mtime = mtime
        copy_path = self.out_dir / "frame_in.jpg"
        shutil.copyfile(path, copy_path)
        return copy_path

    def _rtsp_snapshot(self) -> Path | None:
        path = self.out_dir / "rtsp_frame.jpg"
        command = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.args.input,
            "-frames:v",
            "1",
            str(path),
        ]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            return None
        return path


class ControlState:
    def __init__(self) -> None:
        self.auto_zoom = True
        self._delta = 0.0
        self._lock = threading.Lock()

    def set_auto(self, value: bool) -> None:
        with self._lock:
            self.auto_zoom = value

    def add_delta(self, value: float) -> None:
        with self._lock:
            self.auto_zoom = False
            self._delta += value

    def snapshot(self) -> dict:
        with self._lock:
            return {"auto_zoom": self.auto_zoom}

    def consume_delta(self) -> float:
        with self._lock:
            value = self._delta
            self._delta = 0.0
            return value


def start_server(out_dir: Path, port: int, control: ControlState) -> ThreadingHTTPServer | None:
    if port <= 0:
        return None
    write_index(out_dir)

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/control":
                params = parse_qs(parsed.query)
                zoom = params.get("zoom", [""])[0]
                if zoom == "plus":
                    control.add_delta(0.25)
                elif zoom == "minus":
                    control.add_delta(-0.25)
                if params.get("auto", [""])[0] == "1":
                    control.set_auto(True)
                if params.get("auto", [""])[0] == "0":
                    control.set_auto(False)
                body = json.dumps(control.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(out_dir), **kwargs)

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_index(out_dir: Path) -> None:
    (out_dir / "index.html").write_text(
        """<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>DTK ALPR Zoom</title>
  <style>
    body{margin:0;background:#080b10;color:#edf2f7;font-family:system-ui,sans-serif}
    header{padding:12px 16px;background:#111827;position:sticky;top:0}
    main{display:grid;gap:10px;padding:10px}
    button{font-size:18px;padding:8px 14px;margin-right:8px;border:0;border-radius:6px;background:#f2c94c;color:#111827}
    img{width:100%;background:#000;border:1px solid #243041}
    pre{white-space:pre-wrap;background:#111827;padding:12px;border-radius:6px}
    @media(min-width:900px){main{grid-template-columns:1fr 1fr}.status{grid-column:1/3}}
  </style>
</head>
<body>
<header><strong>DTK ALPR Zoom</strong> <span id="headline"></span></header>
<main>
  <section class="status">
    <button onclick="control('minus')">-</button>
    <button onclick="control('auto')">Auto</button>
    <button onclick="control('plus')">+</button>
  </section>
  <section><h3>Wide</h3><img id="wide" src="latest.jpg"></section>
  <section><h3>Zoom</h3><img id="zoom" src="latest_zoom.jpg"></section>
  <section class="status"><h3>Status</h3><pre id="status">{}</pre></section>
</main>
<script>
async function control(action){
  if(action === 'plus') await fetch('/control?zoom=plus&auto=0');
  if(action === 'minus') await fetch('/control?zoom=minus&auto=0');
  if(action === 'auto') await fetch('/control?auto=1');
  tick();
}
async function tick(){
  const t = Date.now();
  document.getElementById('wide').src = 'latest.jpg?t=' + t;
  document.getElementById('zoom').src = 'latest_zoom.jpg?t=' + t;
  try {
    const status = await fetch('status.json?t=' + t).then(r => r.json());
    document.getElementById('status').textContent = JSON.stringify(status, null, 2);
    document.getElementById('headline').textContent =
      ` zoom=${status.zoom?.zoom_ratio?.toFixed?.(2) ?? '-'} plates=${status.plates?.length ?? 0}`;
  } catch(e) {}
}
setInterval(tick, 800); tick();
</script>
</body>
</html>
""",
        encoding="utf-8",
    )


def annotate(image: Image.Image, plates, targets: list[Box], command) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    for plate in plates:
        box = (plate.x, plate.y, plate.x + plate.width, plate.y + plate.height)
        draw.rectangle(box, outline=(242, 201, 76), width=4)
        draw.text((plate.x, max(0, plate.y - 16)), f"{plate.text} {plate.confidence}", fill=(255, 255, 255))
    for target in targets:
        box = (
            target.left * image.width,
            target.top * image.height,
            target.right * image.width,
            target.bottom * image.height,
        )
        draw.rectangle(box, outline=(86, 204, 242), width=3)
    if command.target:
        target = command.target
        box = (
            target.left * image.width,
            target.top * image.height,
            target.right * image.width,
            target.bottom * image.height,
        )
        draw.rectangle(box, outline=(39, 174, 96), width=5)
    return result


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run() -> int:
    args = parse_args()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    control = ControlState()
    server = start_server(out_dir, args.serve, control)
    if server:
        print(f"Dashboard: http://127.0.0.1:{args.serve}/")

    source = FrameSource(args, out_dir)
    zoom = ZoomController(max_zoom=args.max_zoom)
    car_detector = OptionalYoloCarDetector(args.car_model)

    try:
        dtk = DtkLpr(
            args.dtk_dir,
            countries=args.countries,
            min_plate_width=args.min_plate_width,
            max_plate_width=args.max_plate_width,
            recognize_make_model=not args.no_make_model,
            require_license=not args.allow_unlicensed,
        )
    except DtkLicenseError as error:
        print(str(error), file=sys.stderr)
        return 2
    except DtkError as error:
        print(f"DTK startup failed: {error}", file=sys.stderr)
        return 2

    with dtk:
        print(f"DTK version: {dtk.version()}")
        print(f"DTK system id: {dtk.system_id()}")
        while True:
            frame_path = source.next_frame()
            if frame_path is None:
                time.sleep(args.interval)
                if args.once:
                    break
                continue

            started = time.time()
            image = Image.open(frame_path).convert("RGB")
            plates, processing_ms = dtk.read_file(frame_path)
            yolo_targets = car_detector.detect(image)
            plate_targets = [plate_to_target(plate, image.width, image.height) for plate in plates]
            targets = yolo_targets or plate_targets
            manual_delta = control.consume_delta()
            if control.snapshot()["auto_zoom"]:
                command = zoom.next(targets)
            else:
                command = zoom.manual(targets, manual_delta)
            zoomed = zoom.crop_image(image, command)
            annotated = annotate(image, plates, targets, command)

            annotated.save(out_dir / "latest.jpg", quality=90)
            zoomed.save(out_dir / "latest_zoom.jpg", quality=90)
            status = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": str(frame_path),
                "dtk_processing_ms": processing_ms,
                "loop_ms": int((time.time() - started) * 1000),
                "control": control.snapshot(),
                "car_detector": "yolo" if car_detector.enabled else "plate-roi-fallback",
                "plates": [plate.to_json() for plate in plates],
                "targets": [target.to_json() for target in targets],
                "zoom": command.to_json(),
            }
            atomic_json(out_dir / "status.json", status)
            atomic_json(out_dir / "zoom_command.json", command.to_json())
            plate_text = ", ".join(plate.text for plate in plates) or "no plates"
            print(f"{status['time']} | {plate_text} | zoom={command.zoom_ratio:.2f} | {command.reason}")

            if args.once:
                break
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
