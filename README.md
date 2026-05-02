# Termux DTK ALPR Runner

This runner is for the DTK **Linux ARM64** package from `/Users/omar.ibrahim/Downloads/arm64`.
Those files are glibc Linux AArch64 binaries, so the correct phone path is:

```text
Termux -> proot-distro Ubuntu -> Python -> libDTKLPR.so
```

It is not an Android APK and it is not trying to load Linux `.so` files through Android JNI.

## What It Does

- Runs DTK LPR inside Ubuntu using `libDTKLPR.so`.
- Runs DTK **video mode** using `libDTKVID.so` for RTSP/IP-camera streams.
- Runs **multi-camera mode** with one DTK video engine per stream.
- Deduplicates the same plate text across all cameras and increments `count` instead of creating spam events.
- Selects the best visible target.
- Performs software zoom/crop around that target.
- Provides `- / Auto / +` zoom controls in the dashboard.
- Writes `latest.jpg`, `latest_zoom.jpg`, `status.json`, and `zoom_command.json`.
- Serves a small local dashboard at `http://127.0.0.1:8765/`.

The high-performance path is video stream mode, not `termux-camera-photo`.
Termux:API only gives photo snapshots, so it is not acceptable for maximum LPR performance.

The current zoom is software zoom. Termux/proot does not expose Camera2 hardware zoom controls. The emitted `zoom_command.json` is designed so a later motor/PTZ controller can consume the target center and pan/tilt error.

## Phone Install

On Android:

1. Install Termux from F-Droid.
2. Install Termux:API from F-Droid.
3. Put the ARM64 DTK package in one of these places:

```text
/sdcard/Download/arm64/
/sdcard/Download/arm64.zip
/sdcard/Download/Telegram/arm64/
/sdcard/Download/Telegram/arm64.zip
```

4. Copy this `termux-dtk-alpr` folder to the phone, then run in Termux:

```bash
cd ~/termux-dtk-alpr
bash termux/install.sh
```

5. Start the high-performance video runner from a real camera stream:

```bash
bash ~/dtk-alpr/app/termux/run_camera_stream.sh rtsp://127.0.0.1:8554/live
```

For three cameras, expose three RTSP/H.264 streams and run:

```bash
bash ~/dtk-alpr/app/termux/run_multi_camera_streams.sh \
  rtsp://camera1/live \
  rtsp://camera2/live \
  rtsp://camera3/live
```

That starts three independent DTK engines: `cam1`, `cam2`, and `cam3`.
The shared plate table is written to:

```text
~/dtk-alpr/app/runtime-multi/plate_counts.json
~/dtk-alpr/app/runtime-multi/status.json
```

If the same plate appears again, the runner updates the same entry:

```json
{
  "key": "AB1234",
  "text": "AB-1234",
  "count": 17,
  "cameras": {
    "cam1": 11,
    "cam3": 6
  }
}
```

Use a phone camera streamer that can output RTSP/H.264. Recommended camera profile:

```text
1280x720
15-25 FPS
H.264
fixed focus / continuous video focus
disable beauty/HDR/stabilization if latency matters
```

For three cameras, start with `THREADS_PER_ENGINE=1`. If the phone has CPU headroom,
try `THREADS_PER_ENGINE=2`. Do not use heavy previews while tuning performance:

```bash
THREADS_PER_ENGINE=1 PREVIEW_EVERY=0 bash ~/dtk-alpr/app/termux/run_multi_camera_streams.sh ...
```

Fallback snapshot mode exists, but it is not the performance path:

```bash
bash ~/dtk-alpr/app/termux/run_phone.sh
```

Open:

```text
http://127.0.0.1:8765/
```

## Local Still-Image Test In Ubuntu

Inside Ubuntu/proot:

```bash
cd /data/data/com.termux/files/home/dtk-alpr/app
bash ubuntu/run_ubuntu.sh --source file --input /path/to/sample.jpg --once
```

## Local Video Test In Ubuntu

```bash
bash ubuntu/run_video.sh --file /path/to/video.mp4 --repeat 1 --preview-every 0
```

For RTSP camera:

```bash
bash ubuntu/run_video.sh --rtsp rtsp://user:pass@host:554/stream1 --preview-every 0
```

For three video streams:

```bash
bash ubuntu/run_multi_video.sh \
  --rtsp rtsp://user:pass@host1:554/stream1 \
  --rtsp rtsp://user:pass@host2:554/stream1 \
  --rtsp rtsp://user:pass@host3:554/stream1 \
  --threads 1 \
  --preview-every 0
```

## Verified On This Mac Through ARM64 Linux

The included ARM64 Linux runtime was smoke-tested in an ARM64 Ubuntu container:

```text
DTK version: 6.0.1
sample1.jpg -> GK-3713, FV-2382, zoom=1.28
sample2.jpg -> JC-6294, zoom=1.28
sample.mp4 -> DTKVID consumed 412/414 frames at about 25 FPS
```

That proves the runner loads `libDTKLPR.so` and uses the ARM64 DTK engine, not the Windows DLL.

## License

The runner does not patch or bypass DTK licensing. If `LPREngine_IsLicensed()` returns an error, the program reports it and stops.
