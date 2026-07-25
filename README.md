# Termux DTK ALPR Runner

This repository is an experimental edge-vision integration for running the
proprietary DTK **Linux ARM64** SDK inside a Termux-managed Ubuntu environment.
The SDK files and license are deliberately not part of this repository. Place
an official ARM64 SDK distribution in the ignored `vendor/arm64/` directory.

Those files are glibc Linux AArch64 binaries, so the intended phone path is:

```text
Termux -> proot-distro Ubuntu -> Python -> libDTKLPR.so
```

It is not an Android APK and it is not trying to load Linux `.so` files through Android JNI.

> **Current boundary:** the Python orchestration, multi-camera aggregation,
> frame handoff, and software zoom code are present. A complete run additionally
> requires a separately licensed SDK, compatible ARM64 hardware, FFmpeg, and a
> camera source. The repository does not currently publish a reproducible
> end-to-end benchmark or independently verifiable recognition-accuracy result.
> See the [project charter](docs/charter.md) and
> [threat model](docs/threat-model.md).

## What It Does

- Runs DTK LPR inside Ubuntu using `libDTKLPR.so`.
- Runs DTK **video mode** using `libDTKVID.so` for RTSP/IP-camera streams.
- Uses `ffmpeg` as the default RTSP decoder and feeds DTK video frames through `VideoFrame_CreateFromImageBuffer`.
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
bash ~/dtk-alpr/app/termux/run_camera_stream.sh
```

The Android APK starts a local H.264 RTSP camera stream at:

```text
rtsp://127.0.0.1:8554/live
```

Termux reads that local stream with `ffmpeg` and feeds raw frames to DTK video mode.
This avoids DTKVID's fragile IP-camera opener while still using DTK's video engine.

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

From normal Termux:

```bash
bash ~/dtk-alpr/app/termux/run_photo_test.sh /sdcard/Download/alpr-samples/sample1.jpg
```

## Local Video Test In Ubuntu

```bash
bash ubuntu/run_video.sh --file /path/to/video.mp4 --repeat 1 --preview-every 0
```

## DTK License

If DTK prints error `2`, it usually means the engine has no activated
recognition channel. Check the license from Termux:

```bash
bash ~/dtk-alpr/app/termux/activate_license.sh
```

Online activation:

```bash
bash ~/dtk-alpr/app/termux/activate_license.sh YOUR_LICENSE_KEY
```

Offline activation:

```bash
bash ~/dtk-alpr/app/termux/activate_license.sh getactlink YOUR_LICENSE_KEY
bash ~/dtk-alpr/app/termux/activate_license.sh setactcode YOUR_ACTIVATION_CODE
```

For RTSP camera:

```bash
bash ubuntu/run_video.sh --rtsp rtsp://camera.invalid/stream1 --preview-every 0
```

Do not put camera usernames or passwords directly in a command-line URL. They
can be exposed through shell history, process listings, logs, and runtime
status files. The current runner has no secret-provider integration; use only
credential-free local or otherwise isolated streams until that boundary is
implemented.

For the companion Android APK local stream:

```bash
bash ubuntu/run_ffmpeg_video.sh --rtsp rtsp://127.0.0.1:8554/live --preview-every 20
```

The old DTKVID capture backend is still available for comparison:

```bash
CAPTURE_BACKEND=dtkvid bash ~/dtk-alpr/app/termux/run_camera_stream.sh
```

For three video streams:

```bash
bash ubuntu/run_multi_video.sh \
  --rtsp rtsp://camera1.invalid/stream1 \
  --rtsp rtsp://camera2.invalid/stream1 \
  --rtsp rtsp://camera3.invalid/stream1 \
  --threads 1 \
  --preview-every 0
```

## Evidence status

Earlier development runs exercised the proprietary SDK with still images and a
video stream. Their media, license state, host environment, and raw detections
are not committed, so those observations are not presented as reproducible
portfolio evidence. Future published evidence must use redistributable
synthetic media, record the exact runner and environment contract, and clearly
separate throughput from recognition quality.

## Privacy and deployment safety

License plates, camera URLs, frames, timestamps, device identifiers, and
vehicle metadata can be sensitive. The current implementation writes raw
runtime JSON and preview images. Runtime directories are tightened to mode
`0700`; JSON, HTML, and image artifacts are written or tightened to mode
`0600`; source paths and RTSP authorities are excluded from status records.
These controls do not provide retention, encryption, authentication, or plate
redaction. Use only footage you are authorized to process, keep the output
directory private, and do not expose the loopback dashboard through a reverse
proxy or port-forward.

`127.0.0.1` binding limits the default dashboard listener to the local network
namespace; it is not an authentication or authorization mechanism. Review
[`docs/threat-model.md`](docs/threat-model.md) before using any non-synthetic
camera source.

## License

No DTK binaries, models, activation material, or license rights are distributed
here. The runner does not patch or bypass DTK licensing. If
`LPREngine_IsLicensed()` returns an error, the program reports it and stops.
The repository itself does not yet declare an open-source license; treat the
source as all-rights-reserved until an explicit license file is added.
