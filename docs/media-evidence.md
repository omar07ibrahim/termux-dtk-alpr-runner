# Verified media boundary

The media evidence lane is deliberately separate from DTK recognition. Its job
is to exercise a repository-generated synthetic video through a real FFmpeg
process, consume fixed-size RGB frames through the production process
supervisor, and publish only bounded receipts and visuals derived from those
frames.

It does not load a DTK library, use a camera, recognize a plate, or measure
accuracy, throughput, or latency.

## Pinned evidence runtime

The reproducible capture profile is Linux x86_64 with CPython 3.12.3 and the
`imageio-ffmpeg` 0.6.0 manylinux wheel in
[`requirements-media-evidence.lock`](../requirements-media-evidence.lock).
The wheel is accepted only at its PyPI SHA-256:

```text
c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282
```

That wheel contains `ffmpeg-linux-x86_64-v7.0.2`. The capture verifier also
checks the extracted executable before use:

```text
version: 7.0.2-static
bytes: 79826272
sha256: e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99
```

The wheel and executable are third-party tools, not Omar-authored artifacts.
The Python wrapper is distributed under BSD-2-Clause; the bundled FFmpeg build
reports GPLv3 configuration. Neither binary is committed or redistributed by
this repository. See the
[`imageio-ffmpeg` project](https://github.com/imageio/imageio-ffmpeg), its
[PyPI release](https://pypi.org/project/imageio-ffmpeg/0.6.0/), and
[FFmpeg licensing guidance](https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md).

The lock intentionally supports only Linux x86_64 evidence capture. It is not
the Android/Termux installation contract and is never used to install or
activate the proprietary SDK.

## Local installation

Install into an ignored repository-local directory:

```bash
python3.12 -m pip install \
  --disable-pip-version-check \
  --require-hashes \
  --only-binary=:all: \
  --target .t/media-evidence-runtime \
  -r requirements-media-evidence.lock
```

No global AWS package, Docker configuration, Termux package, DTK binary,
license material, camera credential, or user media is changed by this step.

## Evidence and non-claims

The generated source video contains only deterministic geometric shapes. Its
closed, numeric-only recipe is
[`examples/synthetic-media-v1.json`](../examples/synthetic-media-v1.json);
`alpr_runner.synthetic_media` validates it and renders the exact YUV4MPEG2
bytes using only the Python standard library. The media probe may establish:

- the exact synthetic source bytes and recipe;
- the exact FFmpeg executable and sanitized command profile;
- complete fixed-size RGB frame delivery;
- per-frame byte identities and ordering;
- bounded buffering, stderr, timeouts, and process cleanup; and
- derivation of committed screenshots or animation from decoded frames.

It cannot establish:

- DTK execution or recognition quality;
- camera, RTSP, Android, ARM64, or hardware compatibility;
- production FPS, latency, memory, or power consumption;
- a license grant for the repository-owned source; or
- authenticity of any external scene.

The repository still has no top-level source license. Until Omar chooses one,
the code and generated media remain all-rights-reserved despite being
reproducible and free of third-party or personal input data.
